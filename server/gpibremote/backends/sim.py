"""Simulated GPIB bus for development and tests.

A simulated instrument is a simple SCPI-style state machine: "HDR value" stores a value,
"HDR?" returns it. Phase 1 replaces the per-instrument tables with ones derived from the
device-type files.
"""

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
