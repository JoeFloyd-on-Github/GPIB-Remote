"""Python API for test scripts: instruments on a GPIB-Remote server as objects.

    from gpibctl.api import Bench

    with Bench("192.168.1.157") as bench:
        for d in bench.devices():                      # configured instruments
            print(d.id, d.type, d.online)
        psu = bench.supply("bench-psu")                # role view: voltage/current/output
        dmm = bench.meter("dmm-ohms")                  # role view: function/range/nplc/read
        dmm.configure("ohm4", range=30, nplc=100, offset_comp=True)
        with psu.lock("plating run"), psu.guard():     # exclusive control; output off on exit
            psu.set(voltage=3.0, current=0.48)
            psu.on()
            print(psu.measure_current(), dmm.read())

Layers, from low to high (all over the server's JSON protocol, docs/protocol.md):

- `Bench.raw(pad)`: plain GPIB I/O at an address (write, query, read, clear, trigger, poll).
  The commands are the instrument's own; nothing is checked.
- `Device` (from `bench.device(id)`): a configured instance, i.e. a handle. `get`/`set` named
  keys from its device type (`P6V.voltage`, `function`), `action`, `state`, `lock`, `update`
  (configuration: name, notes, limits, safe state), plus raw I/O at its address.
- Roles: `Supply`, `Meter`, `Thermometer` give the same methods for any type whose file declares
  that role (`roles:` in the type, docs/device-type-format.md). Types without a `roles` section
  are mapped by convention (keys named voltage/current/output/v_meas/i_meas; function/reading).

Values come back as plain Python values. Server refusals raise `ServerError` (`.code` is
`locked`, `rejected`, `timeout`, `gpib`, ...); misuse of a role raises `ApiError`.

Safety: a Bench remembers supplies it turned on and turns them off on `close()` (and so on
leaving `with Bench(...)`), unless `keep_on=True` was passed to `on()`. Locks are released on
close; the server also releases them when the connection drops.

Standard library only; Python 3.6 or newer.
"""

import contextlib
import csv
import datetime
import math
import os
import re
import statistics
import time

from . import Client, ServerError
from . import config as _config

__all__ = ["Bench", "Device", "Supply", "Meter", "Thermometer", "RawDevice", "Found", "Stats",
           "DataLog", "ServerError", "ApiError", "type_k_temperature", "type_k_emf"]


class ApiError(RuntimeError):
    """The API was used in a way the instrument's type doesn't support (no such role or key)."""


def _onoff(v):
    if isinstance(v, str):
        return v.lower()
    return "on" if v else "off"


# =================================================================================================
class Bench:
    """A connection to a GPIB-Remote server.

    host/port default to the CLI's settings: GPIBREMOTE_HOST/GPIBREMOTE_PORT, then
    ~/.gpibctl.ini, then localhost:9000. `user` and `client` name you in locks and the audit log.
    """

    def __init__(self, host=None, port=None, user=None, client="gpibctl.api", timeout=30.0):
        dh, dp = _config.defaults()
        self.host, self.port = host or dh, port or dp
        self.client = Client(self.host, self.port, timeout)
        try:
            self.hello = self.client.hello(client, user=user)
        except Exception:
            self.client.close()
            raise
        self._types = {}
        self._handles = {}
        self._energized = []          # supplies this Bench turned on

    # -- connection ------------------------------------------------------------------------------
    def request(self, op, **params):
        """Send one protocol request (docs/protocol.md) and return its result."""
        return self.client.request(op, **params)

    def close(self):
        """Turn off supplies this Bench turned on, release its locks, disconnect."""
        try:
            for s in list(self._energized):
                try:
                    s.off()
                except Exception:
                    pass
            for d in self._handles.values():
                if d._locked:
                    try:
                        d.unlock()
                    except Exception:
                        pass
        finally:
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def info(self):
        """Server version, uptime, boards and connected sessions."""
        return self.request("server.info")

    def events(self, match=None):
        """Take the events received so far (readings, state and lock changes, status), oldest
        first. With `match`, only those for which match(event) is true; the rest stay queued."""
        q = self.client.events
        out, keep = [], []
        while q:
            ev = q.popleft()
            (out if match is None or match(ev) else keep).append(ev)
        q.extend(keep)
        return out

    def wait_event(self, match=None, timeout=10.0):
        """The next event for which match(event) is true; TimeoutError if none arrives in time.
        Events that don't match are dropped. Use Device.subscribe() to receive an instrument's."""
        return self.client.wait_event(match, timeout)

    def locks(self):
        """Held control locks: {instance id: {holder, session, since, idle}}."""
        return self.request("dev.locks")["locks"]

    def listeners(self, board=0):
        """Addresses where a device answers (no commands are sent to them)."""
        return self.request("bus.listeners", board=board)["pads"]

    # -- types ---------------------------------------------------------------------------------
    def types(self):
        """Every device type: [{type, versions, latest, title, verified, builtin}]."""
        return self.request("types.list")["types"]

    def type_model(self, ref):
        """The model of a type ('hp.3457a' for the latest, or 'hp.3457a@1.1.0'). Cached."""
        if ref not in self._types:
            self._types[ref] = self.request("types.get", type=ref)["model"]
        return self._types[ref]

    def type_ref(self, name):
        """'hp.3457a' -> its latest 'hp.3457a@1.1.0'; a ref with a version is returned as is."""
        return name if "@" in name else self.request("types.get", type=name)["ref"]

    def type_yaml(self, ref):
        """A type's YAML file, exactly as stored."""
        return self.request("types.get", type=ref, format="yaml")["yaml"]

    def validate_type(self, yaml_text):
        """Check a type file without storing it: {valid, errors, ref, exists}."""
        return self.request("types.validate", yaml=yaml_text)

    def push_type(self, yaml_text, message=None):
        """Store a new type or version: {ref, hash, added}. A different file under an existing
        version is refused (code `conflict`): bump the version instead."""
        return self.request("types.put", yaml=yaml_text, message=message)

    def type_history(self, type_name=None, limit=30):
        """Changes to one type, or to all of them: [{rev, author, date, message}]."""
        params = {"type": type_name} if type_name else {}
        return self.request("types.history", limit=limit, **params)["commits"]

    # -- instruments ---------------------------------------------------------------------------
    def instances(self):
        """The configured instruments as the server lists them:
        [{id, name, type, title, address, revision, online, lock, poll_mode}]."""
        return self.request("instances.list")["instances"]

    def devices(self):
        """Configured instruments, as Device handles."""
        return [self.device(i["id"]) for i in self.instances()]

    def device(self, instance_id):
        """The handle for a configured instrument (one per id per Bench)."""
        d = self._handles.get(instance_id)
        if d is None:
            d = Device(self, instance_id)
            self._handles[instance_id] = d
        return d

    __getitem__ = device

    def add(self, instance_id, type_ref, pad, board=0, sad=0, name=None, notes=None, verify=True,
            limits=None, off_on_disconnect=False, poll_mode=None, overrides=None, safe_state=None):
        """Configure a new instrument and return its handle.

        type_ref: 'hp.3457a' (latest version) or 'hp.3457a@1.1.0'. With verify the server sends
        the type's identify probe; the answer is in the handle's `identify_result`
        ({probe, response, match} or {error}). limits: {key: (min, max)}, merged into
        overrides. off_on_disconnect: the server turns the outputs off when the controlling
        client drops (shorthand for safe_state={"on_client_disconnect": "outputs_off"}).
        """
        type_ref = self.type_ref(type_ref)
        inst = {"id": instance_id, "name": name or instance_id, "type": type_ref,
                "address": {"board": board, "pad": pad, "sad": sad}}
        if notes:
            inst["notes"] = notes
        ov = dict(overrides or {})
        if limits:
            ov["limits"] = dict(ov.get("limits") or {}, **{k: list(v) for k, v in limits.items()})
        if ov:
            inst["overrides"] = ov
        ss = dict(safe_state or {})
        if off_on_disconnect:
            ss["on_client_disconnect"] = "outputs_off"
        if ss:
            inst["safe_state"] = ss
        if poll_mode:
            inst["poll_mode"] = poll_mode
        r = self.request("instances.create", instance=inst, verify=verify)
        d = self.device(instance_id)
        d.identify_result = r.get("identify")
        return d

    def remove(self, instance_id):
        """Delete an instrument's configuration (its history stays on the server)."""
        self.request("instances.delete", instance=instance_id)
        self._handles.pop(instance_id, None)

    def raw(self, pad, board=0, sad=0):
        """Plain GPIB access to an address, configured or not."""
        return RawDevice(self, pad, board, sad)

    def scan(self, board=0, probes=("*IDN?", "ID?"), timeout=1.0, probe_configured=False):
        """Find instruments on a board: [Found(pad, instance, response, types)].

        Addresses already configured report their instance; the others are asked each probe in
        turn (SCPI '*IDN?', then HP's older 'ID?'), and the answer is matched against every
        type's identify pattern. Probing sends commands, so an instrument that doesn't know a
        probe may show an error on its front panel.
        """
        configured = {(i["address"].get("board", 0), i["address"]["pad"]): i
                      for i in self.request("instances.list")["instances"]}
        models = [self.type_model(t["type"]) for t in self.types()]
        found = []
        for pad in self.request("bus.listeners", board=board)["pads"]:
            inst = configured.get((board, pad))
            f = Found(pad, board, inst["id"] if inst else None, inst["type"] if inst else None)
            if inst is None or probe_configured:
                raw = self.raw(pad, board)
                for probe in probes:
                    try:
                        resp = raw.query(probe, timeout=timeout).strip()
                    except ServerError:
                        try:
                            raw.clear()
                        except ServerError:
                            pass
                        continue
                    if resp:
                        f.probe, f.response = probe, resp
                        break
                if f.response:
                    for m in models:
                        ident = m.get("identify") or {}
                        if ident.get("match") and (ident.get("probe") or "").upper() == f.probe.upper() \
                                and re.search(ident["match"], f.response):
                            f.types.append(m["type"])
            found.append(f)
        return found

    # -- roles ---------------------------------------------------------------------------------
    def supply(self, instance_id, channel=None):
        """A supply view of an instrument (its type must have a supply role or the usual keys)."""
        return Supply(self.device(instance_id), channel)

    def meter(self, instance_id):
        """A multimeter view of an instrument."""
        return Meter(self.device(instance_id))

    def thermometer(self, instance_id, thermocouple=None, cjc=None):
        """A thermometer: an instrument with a thermometer role, or a meter reading a
        thermocouple's EMF on DC volts (thermocouple='K'; cjc is the reference-junction
        temperature in C, a number or a callable)."""
        return Thermometer(self.device(instance_id), thermocouple, cjc)


class Found:
    """One address found by Bench.scan()."""

    def __init__(self, pad, board, instance=None, type=None):
        self.pad, self.board, self.instance, self.type = pad, board, instance, type
        self.probe = self.response = None
        self.types = []           # types whose identify pattern matches the response

    def __repr__(self):
        if self.instance:
            return "<%d:%d %s (%s)>" % (self.board, self.pad, self.instance, self.type)
        return "<%d:%d %r -> %s>" % (self.board, self.pad, self.response, ", ".join(self.types) or "no type")


# =================================================================================================
class RawDevice:
    """Plain GPIB I/O at one address. The server refuses it while someone else holds the lock
    on an instance at that address."""

    def __init__(self, bench, pad, board=0, sad=0):
        self.bench, self.pad, self.board, self.sad = bench, pad, board, sad

    def _addr(self, **kw):
        return dict(kw, board=self.board, pad=self.pad, sad=self.sad)

    def write(self, data, timeout=3.0):
        """Send a command ('\\n' is appended unless present)."""
        self.bench.request("raw.write", **self._addr(data=data, timeout=timeout))

    def read(self, timeout=3.0, max_bytes=1 << 20):
        """Read one response (up to EOI or the board's end-of-string character)."""
        return self.bench.request("raw.read", **self._addr(timeout=timeout, max_bytes=max_bytes))["data"]

    def query(self, data, timeout=3.0, max_bytes=1 << 20):
        """Write, then read the response."""
        return self.bench.request("raw.query", **self._addr(data=data, timeout=timeout, max_bytes=max_bytes))["data"]

    def query_float(self, data, timeout=3.0):
        return float(self.query(data, timeout).strip().split(",")[0])

    def clear(self):
        """Selected device clear (SDC): aborts a pending command or output."""
        self.bench.request("bus.clear", **self._addr())

    def trigger(self):
        """Group execute trigger (GET)."""
        self.bench.request("bus.trigger", **self._addr())

    def local(self):
        """Return the instrument to front-panel control."""
        self.bench.request("bus.local", **self._addr())

    def serial_poll(self):
        """The status byte."""
        return self.bench.request("bus.serial_poll", **self._addr())["status"]


# =================================================================================================
class Device:
    """A configured instrument (an instance on the server), addressed by its id.

    Keys are the names in the device type: `output`, `function`, or `P6V.voltage` for a
    per-channel item. `describe()` lists them with their units and limits.
    """

    def __init__(self, bench, instance_id):
        self.bench = bench
        self.id = instance_id
        self._locked = False
        self._config = self._model = self._summary = None
        self.identify_result = None

    def reload(self):
        """Re-read the instance configuration (and its type, when next needed)."""
        r = self.bench.request("instances.get", instance=self.id)
        self._config, self._summary, self._model = r["instance"], r["summary"], None
        return self

    @property
    def config(self):
        """The stored instance document (fetched on first use)."""
        if self._config is None:
            self.reload()
        return self._config

    @property
    def model(self):
        """The device type's model (fetched on first use)."""
        if self._model is None:
            self._model = self.bench.type_model(self.config["type"])
        return self._model

    def summary(self):
        """Live status from the server: {address, online, lock, ...}."""
        self.reload()
        return self._summary

    # -- facts -----------------------------------------------------------------------------------
    @property
    def type(self):
        """The type reference, e.g. 'hp.3457a@1.1.0'."""
        return self.config["type"]

    @property
    def name(self):
        return self.config.get("name") or self.id

    @property
    def address(self):
        a = self.config["address"]
        return a.get("board", 0), a["pad"]

    @property
    def online(self):
        for i in self.bench.request("instances.list")["instances"]:
            if i["id"] == self.id:
                return bool(i.get("online"))
        return False

    @property
    def roles(self):
        return self.model.get("roles") or {}

    @property
    def channels(self):
        return list(self.model.get("channels") or {})

    def keys(self, section=None):
        """Concrete keys of the controls and readouts (or only `section`: 'controls'/'readouts')."""
        out = []
        for sec in ([section] if section else ["controls", "readouts"]):
            for name, d in (self.model.get(sec) or {}).items():
                out += ["%s.%s" % (c, name) for c in self.channels] if d.get("per_channel") else [name]
        return out

    def actions(self):
        return list(self.model.get("actions") or {})

    def describe(self):
        """Readable summary: every key with its kind, unit, range or options."""
        lines = ["%s: %s (%s) at %d:%d" % (self.id, self.model.get("title"), self.type, *self.address)]
        for sec in ("controls", "readouts"):
            for name, d in (self.model.get(sec) or {}).items():
                extra = []
                if d.get("unit"):
                    extra.append(d["unit"])
                if d.get("options"):
                    extra.append("|".join(d["options"]))
                if d.get("min") is not None or d.get("max") is not None:
                    extra.append("%s..%s" % (d.get("min"), d.get("max")))
                kind = d.get("kind", "reading") if sec == "controls" else "reading"
                ch = " (per channel: %s)" % ", ".join(self.channels) if d.get("per_channel") else ""
                lines.append("  %-14s %-8s %s%s  %s" % (name, kind, " ".join(extra), ch, d.get("label", "")))
        for name, d in (self.model.get("actions") or {}).items():
            lines.append("  %-14s action   %s" % (name, d.get("label", "")))
        if self.roles:
            lines.append("  roles: " + ", ".join(self.roles))
        return "\n".join(lines)

    # -- values ----------------------------------------------------------------------------------
    def get(self, key, fresh=True):
        """A value; fresh=True asks the instrument (a readout takes a new measurement)."""
        return self.get_entry(key, fresh)["value"]

    def get_entry(self, key, fresh=True):
        """{value, t, source}: source is 'read' (from the instrument), 'inferred' (judged from a
        status reading; the instrument can't report it directly), 'assumed' (the last value set;
        the instrument can't report it) or 'unknown'; t is the time (Unix seconds)."""
        return self.bench.request("dev.get", instance=self.id, key=key, fresh=fresh)

    def get_float(self, key, fresh=True):
        return float(self.get(key, fresh))

    def set(self, key, value):
        """Set a control; returns the value read back (or as set, if the type can't read it)."""
        return self.set_entry(key, value)["value"]

    def set_entry(self, key, value):
        """Like set(), returning {value, t, source}."""
        if isinstance(value, bool):
            value = _onoff(value)
        return self.bench.request("dev.set", instance=self.id, key=key, value=value)

    def configure(self, **settings):
        """Set several controls in the order given: dev.configure(function='OHMF', nplc=100)."""
        return {k: self.set(k, v) for k, v in settings.items()}

    def action(self, name, channel=None):
        """Run one of the type's actions (reset, autocal, ...)."""
        return self.bench.request("dev.action", instance=self.id, action=name, channel=channel)

    def state(self):
        """Every key's last known value: {key: {value, t, source}}."""
        return self.snapshot()["state"]

    def snapshot(self):
        """{type, state, summary}: the type reference, every key's last known value and the
        live status, in one request."""
        return self.bench.request("dev.state", instance=self.id)

    def subscribe(self):
        """Receive this instrument's events (readings as they are polled, setting changes, lock
        and status changes) through Bench.events()/wait_event(); polling starts if needed.
        Returns the present state."""
        return self.bench.request("subscribe", instance=self.id)["state"]

    def unsubscribe(self):
        self.bench.request("unsubscribe", instance=self.id)

    def refresh(self):
        """Read back every readable setting from the instrument; returns the state."""
        return self.bench.request("dev.refresh", instance=self.id)["state"]

    def identify(self):
        """Ask the instrument its identity: {probe, response, match}."""
        return self.bench.request("instances.identify", instance=self.id)

    # -- control lock ----------------------------------------------------------------------------
    def lock(self, reason="", force=False):
        """Take exclusive control (others can watch but not change it). Usable as a context
        manager: `with dev.lock("run 12"): ...` releases it at the end."""
        r = self.bench.request("dev.lock", instance=self.id, reason=reason, force=force)
        self._locked = True
        return _LockContext(self, r["lock"])

    def unlock(self):
        self.bench.request("dev.unlock", instance=self.id)
        self._locked = False

    def lock_holder(self):
        """Who holds the lock, or None."""
        lk = self.bench.locks().get(self.id)
        return lk["holder"] if lk else None

    # -- configuration ---------------------------------------------------------------------------
    def update(self, revision=None, **changes):
        """Change the stored configuration: name, notes, overrides, safe_state, poll_mode, type
        (another version), address. Every change is kept in the instance's history on the
        server. With `revision`, the change is refused (`conflict`) if someone changed the
        instance since that revision."""
        params = {"revision": revision} if revision is not None else {}
        self._config = self.bench.request("instances.update", instance=self.id, changes=changes,
                                          **params)["instance"]
        self._model = None
        return self._config

    def set_limit(self, key, lo, hi):
        """Narrow a control's allowed range for everyone (e.g. a voltage cap for a fixture)."""
        ov = dict(self.config.get("overrides") or {})
        limits = dict(ov.get("limits") or {})
        limits[key] = [lo, hi]
        ov["limits"] = limits
        return self.update(overrides=ov)

    def history(self, limit=20):
        return self.bench.request("instances.history", instance=self.id, limit=limit)["commits"]

    # -- low level -------------------------------------------------------------------------------
    @property
    def raw(self):
        """Plain GPIB I/O at this instrument's address (its own command language)."""
        board, pad = self.address
        return RawDevice(self.bench, pad, board, self.config["address"].get("sad", 0))

    def write(self, data, timeout=3.0):
        self.raw.write(data, timeout)

    def query(self, data, timeout=3.0):
        return self.raw.query(data, timeout)

    # -- role helpers ----------------------------------------------------------------------------
    def role(self, name, conventions):
        """The type's mapping for a role, or one built from the conventional key names."""
        spec = self.roles.get(name)
        if spec:
            return dict(spec)
        items = dict(self.model.get("controls") or {}, **(self.model.get("readouts") or {}))
        items.update(self.model.get("actions") or {})
        spec = {f: k for f, k in conventions.items() if k in items}
        return spec

    def bound(self, key, which):
        """A control's min or max for a key, with per-channel values and instance limits applied."""
        ch, name = key.split(".", 1) if "." in key else (None, key)
        d = (self.model.get("controls") or {}).get(name) or {}
        v = d.get(which)
        if isinstance(v, str):
            attr = v.strip()[len("{channel."):-1]
            v = (self.model["channels"].get(ch) or {}).get(attr)
        lim = ((self.config.get("overrides") or {}).get("limits") or {}).get(key)
        if lim:
            lv = lim[0] if which == "min" else lim[1]
            if lv is not None:
                v = lv if v is None else (max(v, lv) if which == "min" else min(v, lv))
        return v

    def __repr__(self):
        return "<Device %s %s>" % (self.id, self.type)


class _LockContext:
    """Returned by Device.lock(): `info` is the lock ({holder, session, since, idle})."""

    def __init__(self, dev, info=None):
        self.dev = dev
        self.info = info or {}

    def __enter__(self):
        return self.dev

    def __exit__(self, *exc):
        if self.dev._locked:
            try:
                self.dev.unlock()
            except ServerError:
                pass


# =================================================================================================
class Supply:
    """A power supply channel: set voltage and current, switch the output, measure."""

    CONVENTIONS = {"voltage": "voltage", "current": "current", "output": "output",
                   "v_meas": "v_meas", "i_meas": "i_meas", "ovp": "ovp"}

    def __init__(self, device, channel=None):
        self.device = device
        self.spec = device.role("supply", self.CONVENTIONS)
        for f in ("voltage", "current", "output"):
            if f not in self.spec:
                raise ApiError("%s (%s) has no supply %s" % (device.id, device.type, f))
        self.channel = channel or self.spec.get("channel") or (device.channels[0] if device.channels else None)
        if device.channels and self.channel not in device.channels:
            raise ApiError("%s has no channel %r (%s)" % (device.id, self.channel, ", ".join(device.channels)))

    def key(self, field):
        name = self.spec.get(field)
        if not name:
            raise ApiError("%s (%s) has no supply %s" % (self.device.id, self.device.type, field))
        d = (self.device.model.get("controls") or {}).get(name) or (self.device.model.get("readouts") or {}).get(name) or {}
        return "%s.%s" % (self.channel, name) if d.get("per_channel") else name

    @property
    def id(self):
        return self.device.id

    def lock(self, reason=""):
        return self.device.lock(reason)

    @property
    def max_voltage(self):
        return self.device.bound(self.key("voltage"), "max")

    @property
    def max_current(self):
        return self.device.bound(self.key("current"), "max")

    def set(self, voltage=None, current=None):
        """Set the voltage and/or the current limit (constant-current operation: set the
        current, and the voltage as the compliance limit)."""
        out = {}
        if voltage is not None:
            out["voltage"] = self.device.set(self.key("voltage"), voltage)
        if current is not None:
            out["current"] = self.device.set(self.key("current"), current)
        return out

    def on(self, keep_on=False):
        """Output on. Unless keep_on, the Bench turns it off again when it closes."""
        self.device.set(self.key("output"), "on")
        if not keep_on and self not in self.device.bench._energized:
            self.device.bench._energized.append(self)

    def off(self):
        self.device.set(self.key("output"), "off")
        if self in self.device.bench._energized:
            self.device.bench._energized.remove(self)

    def is_on(self):
        return bool(self.device.get(self.key("output")))

    @contextlib.contextmanager
    def guard(self, zero_current=True):
        """Output off (and the current limit to 0) when the block ends, however it ends."""
        try:
            yield self
        finally:
            self.off()
            if zero_current:
                try:
                    self.set(current=0)
                except ServerError:
                    pass

    def setpoints(self):
        return {"voltage": self.device.get_float(self.key("voltage")),
                "current": self.device.get_float(self.key("current"))}

    def measure_voltage(self):
        return self.device.get_float(self.key("v_meas"))

    def measure_current(self):
        return self.device.get_float(self.key("i_meas"))

    def __repr__(self):
        return "<Supply %s%s>" % (self.device.id, "." + self.channel if self.channel else "")


# =================================================================================================
class Stats:
    """Summary of repeated readings."""

    def __init__(self, values):
        self.values = list(values)
        self.n = len(self.values)
        self.mean = statistics.mean(self.values)
        self.stdev = statistics.stdev(self.values) if self.n > 1 else 0.0
        self.min, self.max = min(self.values), max(self.values)

    @property
    def sem(self):
        """Standard error of the mean."""
        return self.stdev / math.sqrt(self.n) if self.n else 0.0

    def __repr__(self):
        return "<Stats n=%d mean=%.7g sd=%.3g>" % (self.n, self.mean, self.stdev)


# Option names the convention mapping recognises, for types without a meter role.
_FUNCTION_ALIASES = {
    "dcv": ("DCV", "VOLT:DC", "VOLT", "DC V"), "acv": ("ACV", "VOLT:AC"),
    "dci": ("DCI", "CURR:DC", "CURR"), "aci": ("ACI", "CURR:AC"),
    "ohm2": ("OHM", "OHMS", "RES"), "ohm4": ("OHMF", "FRES", "OHM4"),
    "freq": ("FREQ",), "period": ("PER", "PERIOD"), "temp": ("TEMP",),
}


class Meter:
    """A multimeter: choose a function, range and integration time, take readings.

    Functions use standard names whatever the instrument calls them: dcv, acv, acdcv, dci, aci,
    acdci, ohm2, ohm4 (4-wire), freq, period, temp (see `functions`).
    """

    CONVENTIONS = {"function": "function", "reading": "reading", "range": "range", "autorange": "autorange",
                   "nplc": "nplc", "autozero": "autozero", "offset_comp": "offset_comp",
                   "autocal": "autocal", "reset": "reset"}

    def __init__(self, device):
        self.device = device
        self.spec = device.role("meter", self.CONVENTIONS)
        if "reading" not in self.spec:
            raise ApiError("%s (%s) has no meter reading" % (device.id, device.type))
        fmap = self.spec.get("functions")
        if fmap is None and self.spec.get("function"):
            opts = ((device.model.get("controls") or {}).get(self.spec["function"]) or {}).get("options") or {}
            fmap = {}
            for std, aliases in _FUNCTION_ALIASES.items():
                hit = next((o for o in opts if o.upper() in aliases), None)
                if hit:
                    fmap[std] = hit
        self.functions = fmap or {}
        self.function = None
        self.zero = 0.0               # subtracted from every reading (see null())

    @property
    def id(self):
        return self.device.id

    def lock(self, reason=""):
        return self.device.lock(reason)

    def _has(self, field):
        return bool(self.spec.get(field))

    def configure(self, function=None, range=None, nplc=None, autozero=None, offset_comp=None):
        """Set up a measurement. range: a number (the largest expected value) or 'auto'.
        Settings the instrument doesn't have raise ApiError. Clears the null offset when the
        function changes."""
        if function is not None:
            if function not in self.functions:
                raise ApiError("%s can't measure %r (it has: %s)" % (self.id, function, ", ".join(self.functions)))
            self.device.set(self.spec["function"], self.functions[function])
            if function != self.function:
                self.zero = 0.0
            self.function = function
        if range is not None:
            if range == "auto":
                self._need("autorange")
                self.device.set(self.spec["autorange"], "on")
            else:
                self._need("range")
                self.device.set(self.spec["range"], range)
        for field, value in (("nplc", nplc), ("autozero", autozero), ("offset_comp", offset_comp)):
            if value is not None:
                self._need(field)
                self.device.set(self.spec[field], value)
        return self

    def _need(self, field):
        if not self._has(field):
            raise ApiError("%s (%s) has no meter %s" % (self.id, self.device.type, field))

    def read(self):
        """One fresh reading, less the null offset."""
        return self.device.get_float(self.spec["reading"]) - self.zero

    def read_many(self, n, interval=0.0):
        """n readings, `interval` seconds apart (start to start)."""
        out = []
        t0 = time.monotonic()
        for k in range(n):
            if k and interval:
                wait = t0 + k * interval - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
            out.append(self.read())
        return out

    def measure(self, n=1, interval=0.0):
        """Stats over n readings."""
        return Stats(self.read_many(n, interval))

    def null(self, n=5):
        """Take the present input as zero (e.g. shorted 4-wire probe): average n readings and
        subtract that from later readings of this function. Returns the offset."""
        self.zero = 0.0
        self.zero = Stats(self.read_many(n)).mean
        return self.zero

    def autocal(self, wait=60.0):
        """Run the instrument's self-calibration action and wait `wait` seconds for it."""
        self._need("autocal")
        self.device.action(self.spec["autocal"])
        if wait:
            time.sleep(wait)

    def reset(self):
        self._need("reset")
        self.device.action(self.spec["reset"])
        self.function = None
        self.zero = 0.0

    def __repr__(self):
        return "<Meter %s %s>" % (self.device.id, self.function or "")


# =================================================================================================
class Thermometer:
    """Temperature in C, from an instrument with a thermometer role, or from a meter reading a
    thermocouple on DC volts with reference-junction (cold-junction) compensation."""

    def __init__(self, device, thermocouple=None, cjc=None):
        self.device = device
        self.offset = 0.0              # added to every reading (set by calibrate())
        self.thermocouple = thermocouple
        spec = device.roles.get("thermometer")
        if thermocouple is None and spec:
            self.key, self.unit, self.meter = spec["temperature"], spec.get("unit", "C"), None
        elif thermocouple:
            if thermocouple.upper() != "K":
                raise ApiError("only type K thermocouples are built in")
            if cjc is None:
                raise ApiError("a thermocouple needs the reference-junction temperature (cjc)")
            self.meter = Meter(device)
            self.cjc = cjc
            self.key = None
        else:
            raise ApiError("%s (%s) has no thermometer role; give thermocouple='K' and cjc to read a"
                           " thermocouple on DC volts" % (device.id, device.type))

    @property
    def id(self):
        return self.device.id

    def setup(self, nplc=10):
        """For a thermocouple: DC volts, a fixed low range where the meter has one."""
        if self.meter:
            rng = None
            if self.meter._has("range"):
                rng = 0.1
            self.meter.configure("dcv", range=rng, nplc=nplc if self.meter._has("nplc") else None)
        return self

    def raw_celsius(self):
        if self.meter:
            cj = self.cjc() if callable(self.cjc) else self.cjc
            emf_mv = self.meter.read() * 1e3
            return type_k_temperature(emf_mv + type_k_emf(cj))
        v = self.device.get_float(self.key)
        return {"C": v, "F": (v - 32) / 1.8, "K": v - 273.15}[self.unit]

    def read(self):
        """Temperature in C, with the calibration offset applied."""
        return self.raw_celsius() + self.offset

    def calibrate(self, true_c, n=5):
        """Set the offset so the present reading (mean of n) equals true_c (a reference
        thermometer or an ice bath). Returns the offset."""
        self.offset = 0.0
        self.offset = true_c - statistics.mean(self.raw_celsius() for _ in range(n))
        return self.offset


# NIST ITS-90 type K: EMF (mV) from temperature (C), 0..1372 C, and the inverse, 0..500 C.
_K_C = (-1.7600413686e-2, 3.8921204975e-2, 1.8558770032e-5, -9.9457592874e-8, 3.1840945719e-10,
        -5.6072844889e-13, 5.6075059059e-16, -3.2020720003e-19, 9.7151147152e-23, -1.2104721275e-26)
_K_C_NEG = (0.0, 3.9450128025e-2, 2.3622373598e-5, -3.2858906784e-7, -4.9904828777e-9,
            -6.7509059173e-11, -5.7410327428e-13, -3.1088872894e-15, -1.0451609365e-17,
            -1.9889266878e-20, -1.6322697486e-23)
_K_A = (0.118597600000, -0.118343200000e-3, 126.9686)
_K_D = (0.0, 2.508355e1, 7.860106e-2, -2.503131e-1, 8.315270e-2, -1.228034e-2, 9.804036e-4,
        -4.413030e-5, 1.057734e-6, -1.052755e-8)
_K_D_NEG = (0.0, 2.5173462e1, -1.1662878, -1.0833638, -8.9773540e-1, -3.7342377e-1, -8.6632643e-2,
            -1.0450598e-2, -5.1920577e-4)


def _poly(coef, x):
    return sum(c * x ** i for i, c in enumerate(coef))


def type_k_emf(t_c):
    """Type K thermocouple EMF in mV for a junction at t_c (C), reference at 0 C."""
    if t_c < 0:
        return _poly(_K_C_NEG, t_c)
    a0, a1, a2 = _K_A
    return _poly(_K_C, t_c) + a0 * math.exp(a1 * (t_c - a2) ** 2)


def type_k_temperature(emf_mv):
    """Temperature in C for a type K EMF in mV (reference at 0 C); -200..500 C."""
    if emf_mv < 0:
        return _poly(_K_D_NEG, emf_mv)
    return _poly(_K_D, emf_mv)


# =================================================================================================
class DataLog:
    """A CSV log for a run: '# key: value' header lines, then one row per record with the wall
    clock time (ISO 8601, local, with UTC offset) and the elapsed seconds, flushed each row so
    the file can be watched or plotted while the run goes on.

        log = DataLog("runs/S1.csv", ["i_ma", "v_cell"], meta={"sample": "S1"})
        log.record(i_ma=480.1, v_cell=2.41)
        log.note("output off")
        log.close()
    """

    def __init__(self, path, columns, meta=None, clock=time.monotonic):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.path = path
        self.columns = list(columns)
        self.clock = clock
        self.t0 = clock()
        self.fh = open(path, "w", newline="")
        for k, v in (meta or {}).items():
            self.fh.write("# %s: %s\n" % (k, v))
        self.w = csv.writer(self.fh)
        self.w.writerow(["time", "t_s"] + self.columns)
        self.fh.flush()

    def restart_clock(self):
        self.t0 = self.clock()

    def elapsed(self):
        return self.clock() - self.t0

    def record(self, **values):
        now = datetime.datetime.now().astimezone().isoformat(timespec="milliseconds")
        t = self.elapsed()
        row = [now, "%.3f" % t]
        for c in self.columns:
            v = values.get(c)
            row.append("" if v is None else ("%.7g" % v if isinstance(v, float) else v))
        self.w.writerow(row)
        self.fh.flush()
        return now, t

    def note(self, text):
        self.fh.write("# %s %s\n" % (datetime.datetime.now().astimezone().isoformat(timespec="seconds"), text))
        self.fh.flush()

    def close(self):
        if not self.fh.closed:
            self.fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
