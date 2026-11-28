"""Simulated GPIB bus for development and tests.

TypeSimInstrument simulates any instrument from its device-type file: set commands store
values, get commands return them, readouts follow the type's `sim:` hints. SimInstrument is
the older hand-written SCPI state machine, kept for the phase-0 tests.
"""

import random
import re
import threading

from .base import Backend, GpibError, GpibTimeout


class SimInstrument:
    def __init__(self, idn, values=None, aliases=None, select=None):
        self.idn = idn
        self.values = dict(values or {})      # canonical header -> value string
        self.aliases = dict(aliases or {})    # header spelling -> canonical header
        self.select = select                  # header whose value scopes other headers (e.g. INST:SEL)
        self.outq = []
        self.errors = []
        self.status_byte = 0
        self.remote = False
        self.triggers = 0

    def _key(self, header):
        """Storage key: global headers as is, others scoped to the selected channel."""
        header = self.aliases.get(header.upper(), header.upper())
        if not self.select or header == self.select or header in self.values:
            return header
        return "%s@%s" % (header, self.values.get(self.select, ""))

    def handle(self, line):
        self.remote = True
        for cmd in line.split(";"):
            cmd = cmd.strip()
            if not cmd:
                continue
            header, _, arg = cmd.partition(" ")
            up = header.upper()
            if up == "*IDN?":
                self.outq.append(self.idn)
            elif up == "*CLS":
                self.errors = []
            elif up == "*RST":
                pass
            elif up in ("SYST:ERR?", "SYSTEM:ERROR?"):
                self.outq.append(self.errors.pop(0) if self.errors else '+0,"No error"')
            elif up.endswith("?"):
                key = self._key(up[:-1])
                if key in self.values:
                    self.outq.append(self.values[key])
                else:
                    self.errors.append('-113,"Undefined header"')
            else:
                key = self._key(up)
                self.values[key] = arg.strip().upper() if key == self.select else arg.strip()

    def take_output(self):
        return self.outq.pop(0) if self.outq else None


def e3631a():
    """A simulated Agilent E3631A with the settings seen on the real unit."""
    values = {"INST:SEL": "P6V", "OUTP": "0", "OUTP:TRAC": "0"}
    for ch, imax in (("P6V", "+5.00000000E+00"), ("P25V", "+1.00000000E+00"), ("N25V", "+1.00000000E+00")):
        values["VOLT@" + ch] = "+0.00000000E+00"
        values["CURR@" + ch] = imax
        values["MEAS:VOLT@" + ch] = "+0.00000000E+00"
        values["MEAS:CURR@" + ch] = "+0.00000000E+00"
    aliases = {"INST": "INST:SEL", "INSTRUMENT:SELECT": "INST:SEL", "OUTPUT": "OUTP",
               "VOLTAGE": "VOLT", "CURRENT": "CURR", "INST:NSEL": "INST:SEL"}
    return SimInstrument("HEWLETT-PACKARD,E3631A,0,1.6-5.0-1.0", values, aliases, select="INST:SEL")


def _template_regex(tpl):
    """'VOLT {value}' -> regex with named groups for {value} and {channel}."""
    out, pos = "", 0
    for m in re.finditer(r"\{(value|channel)[^}]*\}", tpl):
        out += re.escape(tpl[pos:m.start()])
        out += r"(?P<value>.+?)" if m.group(1) == "value" else r"(?P<channel>[A-Za-z0-9_]+)"
        pos = m.end()
    return re.compile("^" + out + re.escape(tpl[pos:]) + "$", re.I)


class TypeSimInstrument:
    """An instrument simulated from its device type: sets store values, gets return them."""

    def __init__(self, dtype):
        self.t = dtype
        sim = dtype.raw.get("sim") or {}
        self.identity = sim.get("identity", dtype.title)
        self.sim_readouts = dict(sim.get("readouts") or {})   # per instrument, so tests can change one
        self.channel = next(iter(dtype.channels), None)
        self.state = {}
        for name, d in dtype.controls.items():
            for ch in (list(dtype.channels) if d.get("per_channel") else [None]):
                self.state[(name, ch)] = self._default(d, ch)
        self.outq, self.errors = [], []
        self.status_byte = 0
        self.remote = False
        self.triggers = 0
        self.log = []  # commands received, for tests
        # set templates, most specific (longest literal text) first so '{value}' alone is tried last
        sets = [(name, d, _template_regex(d["set"])) for name, d in dtype.controls.items()]
        self._sets = sorted(sets, key=lambda s: -len(re.sub(r"\{[^}]*\}", "", s[1]["set"])))
        self._select = _template_regex(dtype.channel_select) if dtype.channel_select else None

    def _default(self, d, ch):
        kind = d.get("kind")
        if kind == "number":
            lo = self.t.bound(d, "min", ch)
            return 0.0 if lo is None or lo < 0 else lo
        if kind == "bool":
            return False
        if kind == "enum":
            return next(iter(d.get("options") or {}), "")
        return ""

    def _respond(self, d, value):
        kind = d.get("kind")
        if kind == "number":
            return "%+.8E" % value
        if kind == "bool":
            return "1" if value else "0"
        if kind == "enum":
            return self.t.format_value(d, value)
        return str(value)

    def _readout(self, name, ch):
        spec = self.sim_readouts.get(name) or {}
        if "follows" in spec:
            on = self.state.get((spec["when"], None), True) if spec.get("when") else True
            value = self.state.get((spec["follows"], ch), 0.0) if on else 0.0
        elif "value_on" in spec and spec.get("when"):   # e.g. a status register while output is on
            on = self.state.get((spec["when"], None), False)
            value = spec["value_on"] if on else spec.get("value", 0.0)
        else:
            value = spec.get("value", 0.0)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if spec.get("noise"):
                value += random.uniform(-spec["noise"], spec["noise"])
            return "%+.8E" % value if isinstance(value, float) else str(value)
        return str(value)

    def _query(self, cmd):
        t = self.t
        if t.probe and cmd.upper() == t.probe.upper():
            return self.identity
        if t.error_query and cmd.upper() == t.error_query.upper():
            if self.errors:
                return self.errors.pop(0)
            return '+0,"No error"' if "," in (t.error_none or ",") else "0"
        for name, d in t.readouts.items():
            for ch in (list(t.channels) if d.get("per_channel") else [None]):
                q = d.get("query")
                if q and cmd.upper() == t.render(q, ch).upper() and (
                        "{channel" in q or ch is None or ch == self.channel):
                    return self._readout(name, ch)
        for name, d in t.controls.items():
            g = d.get("get")
            if not g:
                continue
            for ch in (list(t.channels) if d.get("per_channel") else [None]):
                if cmd.upper() == t.render(g, ch).upper() and (
                        "{channel" in g or ch is None or ch == self.channel):
                    return self._respond(d, self.state[(name, ch)])
        return None

    def handle(self, line):
        self.remote = True
        for cmd in line.split(";"):
            cmd = cmd.strip()
            if not cmd:
                continue
            self.log.append(cmd)
            resp = self._query(cmd)
            if resp is not None:
                self.outq.append(resp)
                continue
            if self._select:
                m = self._select.match(cmd)
                if m and m.group("channel").upper() in {c.upper() for c in self.t.channels}:
                    self.channel = next(c for c in self.t.channels if c.upper() == m.group("channel").upper())
                    continue
            if any(cmd.upper() == c.upper() for a in self.t.actions.values() for c in a["send"]) \
                    or any(cmd.upper() == c.upper() for c in self.t.init):
                continue
            if not self._set(cmd):
                self.errors.append('-113,"Undefined header"' if "," in (self.t.error_none or ",") else "1")

    def _set(self, cmd):
        for name, d, rx in self._sets:
            m = rx.match(cmd)
            if not m:
                continue
            ch = m.groupdict().get("channel")
            if ch is not None:    # a channel name, or a channel attribute such as {channel.n}
                ch = next((c for c, a in self.t.channels.items() if c.upper() == ch.upper()
                           or ch in (str(v) for v in (a or {}).values())), None)
                if ch is None:
                    continue
            elif d.get("per_channel"):
                ch = self.channel
            token = m.group("value").strip() if "value" in m.groupdict() else ""
            kind = d.get("kind")
            try:
                if kind == "number":
                    value = float(token)
                elif kind == "bool":
                    tokens = {str(self.t.format_value(d, True)).upper(): True,
                              str(self.t.format_value(d, False)).upper(): False}
                    value = tokens[token.upper()]
                elif kind == "enum":
                    value = next(n for n in d["options"] if self.t.format_value(d, n).upper() == token.upper())
                else:
                    value = token
            except (ValueError, KeyError, StopIteration):
                continue
            self.state[(name, ch)] = value
            return True
        return False

    def take_output(self):
        if self.outq:
            return self.outq.pop(0)
        # A free-running meter: addressed to talk with nothing asked, it sends its present reading.
        talk = next((n for n, d in self.t.readouts.items() if d.get("read") and not d.get("query")), None)
        return self._readout(talk, None) if talk else None


class SimBackend(Backend):
    name = "sim"

    def __init__(self, board=0, instruments=None):
        super().__init__(board)
        self.instruments = dict(instruments) if instruments is not None else {5: e3631a()}
        self._lock = threading.Lock()

    def _inst(self, pad):
        inst = self.instruments.get(pad)
        if inst is None:
            raise GpibError("no listener at %d" % pad, code="ENOL")
        return inst

    def write(self, pad, sad, data, timeout=3.0):
        with self._lock:
            self._inst(pad).handle(data.decode("latin-1").strip())

    def read(self, pad, sad, max_bytes=1 << 20, timeout=3.0):
        with self._lock:
            out = self._inst(pad).take_output()
        if out is None:
            raise GpibTimeout("read from %d failed: EABO (timeout)" % pad, code="EABO")
        return (out + "\n").encode("latin-1")[:max_bytes]

    def clear(self, pad, sad):
        with self._lock:
            self._inst(pad).outq = []

    def local(self, pad, sad):
        with self._lock:
            self._inst(pad).remote = False

    def trigger(self, pad, sad):
        with self._lock:
            self._inst(pad).triggers += 1

    def serial_poll(self, pad, sad):
        with self._lock:
            return self._inst(pad).status_byte

    def listeners(self, pads=range(1, 31)):
        return [p for p in pads if p in self.instruments]

    def interface_clear(self):
        pass
