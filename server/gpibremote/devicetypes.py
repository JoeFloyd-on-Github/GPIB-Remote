"""Device types: the human-editable model of an instrument (see docs/device-type-format.md).

A type file is YAML. This module validates it, and turns values into command strings and
instrument responses into values. It does no I/O.
"""

import hashlib
import re

import yaml

SCHEMA = 1
KINDS = ("number", "bool", "enum", "text")
PARSERS = ("number", "int", "bool", "text", "enum", "list")
TYPE_NAME = re.compile(r"^[a-z0-9][a-z0-9_\-]*(\.[a-z0-9][a-z0-9_\-]*)+$")
VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_FLOAT = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_TRUE = {"1", "ON", "TRUE", "YES"}
_FALSE = {"0", "OFF", "FALSE", "NO"}


class TypeError_(ValueError):
    """Invalid type file; `errors` is a list of 'path: message' strings."""

    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


class ValueRejected(ValueError):
    """A value a client tried to set is not acceptable (wrong kind, out of range)."""


def version_key(v):
    m = VERSION.match(v)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def content_hash(text):
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def parse_seconds(v, default=None):
    """Accept 2, 2.5, '2s', '500ms', '1min'."""
    if v is None:
        return default
    if isinstance(v, bool):
        raise ValueError("not a duration: %r" % v)
    if isinstance(v, (int, float)):
        return float(v)
    m = re.match(r"^\s*([0-9.]+)\s*(ms|s|min)?\s*$", str(v))
    if not m:
        raise ValueError("not a duration: %r" % v)
    n = float(m.group(1))
    return n / 1000 if m.group(2) == "ms" else n * 60 if m.group(2) == "min" else n


# ---------------------------------------------------------------------------------------------
class DeviceType:
    """A validated device type. `raw` is the parsed YAML; `text` the original file contents."""

    def __init__(self, text, builtin=False):
        self.text = text
        self.hash = content_hash(text)
        self.builtin = builtin
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise TypeError_(["yaml: %s" % exc])
        errors = validate(raw)
        if errors:
            raise TypeError_(errors)
        self.raw = raw
        self.name = raw["type"]
        self.version = raw["version"]
        self.ref = "%s@%s" % (self.name, self.version)
        self.title = raw.get("title", self.name)
        self.verified = bool(raw.get("verified", False))
        conn = raw.get("connection") or {}
        self.eos = conn.get("eos", "\n")
        self.write_termination = conn.get("write_termination", "\n")
        self.timeout = parse_seconds(conn.get("timeout"), 3.0)
        self.delay = parse_seconds(conn.get("delay"), 0.0)
        ident = raw.get("identify") or {}
        self.probe = ident.get("probe")
        self.match = ident.get("match")
        errs = raw.get("errors") or {}
        self.error_query = errs.get("query")
        self.error_none = errs.get("none")
        self.check_errors = bool(errs.get("check_after_set", False))
        ch = raw.get("channels") or {}
        self.channel_select = ch.get("select")
        self.channels = dict(ch.get("list") or {})
        self.controls = dict(raw.get("controls") or {})
        self.readouts = dict(raw.get("readouts") or {})
        self.actions = dict(raw.get("actions") or {})
        self.safe_off = list(raw.get("safe_off") or [])
        # readout name -> controls whose value is decoded from it (controls.*.infer / readback)
        self.inferred_by = {}
        for cname, d in self.controls.items():
            spec = d.get("readback") or d.get("infer")
            if spec:
                self.inferred_by.setdefault(spec["readout"], []).append(cname)
        self.restore = bool(raw.get("restore", False))
        self.display = raw.get("display") or None
        self.init = list(raw.get("init") or [])

    # -- keys ---------------------------------------------------------------
    def split_key(self, key):
        """'P6V.voltage' -> ('P6V', 'voltage'); 'output' -> (None, 'output')."""
        if "." in key:
            ch, name = key.split(".", 1)
            return ch, name
        return None, key

    def item(self, key):
        """Return (section, name, channel, definition) for a control/readout key."""
        ch, name = self.split_key(key)
        for section in ("controls", "readouts"):
            d = getattr(self, section).get(name)
            if d is not None:
                if bool(d.get("per_channel")) != (ch is not None):
                    raise KeyError("%s %r %s a channel" % (
                        section[:-1], name, "needs" if d.get("per_channel") else "takes no"))
                if ch is not None and ch not in self.channels:
                    raise KeyError("unknown channel %r" % ch)
                return section, name, ch, d
        raise KeyError("unknown key %r" % key)

    def keys(self, section):
        """All concrete keys of a section, expanding per-channel items."""
        out = []
        for name, d in getattr(self, section).items():
            if d.get("per_channel"):
                out += ["%s.%s" % (ch, name) for ch in self.channels]
            else:
                out.append(name)
        return out

    # -- commands -------------------------------------------------------------
    def select_cmd(self, channel):
        if channel is None or not self.channel_select:
            return None
        return self.render(self.channel_select, channel)

    def render(self, template, channel=None, value=None):
        """Fill a command template: {value}, {channel} and channel attributes {channel.attr}."""
        if channel is not None and "{channel." in template:
            attrs = self.channels.get(channel) or {}
            template = re.sub(r"\{channel\.([A-Za-z_]\w*)\}",
                              lambda m: str(attrs.get(m.group(1), "")).replace("{", "{{").replace("}", "}}"),
                              template)
        return template.format(channel=channel, value=value)

    def bound(self, d, which, channel):
        """Resolve a min/max that may reference a channel attribute ('{channel.vmax}')."""
        v = d.get(which)
        if isinstance(v, str):
            m = re.match(r"^\{channel\.([A-Za-z_][A-Za-z0-9_]*)\}$", v.strip())
            if not m:
                raise ValueError("bad bound %r" % v)
            v = (self.channels.get(channel) or {}).get(m.group(1))
        return None if v is None else float(v)

    def status_spec(self, cname):
        """(spec, source) for a control decoded from a status readout, else (None, None)."""
        d = self.controls[cname]
        if d.get("readback"):
            return d["readback"], "read"
        if d.get("infer"):
            return d["infer"], "inferred"
        return None, None

    def status_value(self, cname, reading):
        """Decode a control's value from its status readout's value (None if it can't be told).

        Each case tests one byte of a text readout (`byte`, counted from 1) or the integer value:
        `equals` the whole value, `set` bits all 1, `clear` bits all 0, `any` at least one bit 1.
        The first case that matches gives the value; otherwise `default`."""
        spec, _ = self.status_spec(cname)
        d = self.controls[cname]
        cases = spec.get("cases")
        if cases is None:   # infer: {readout, any_bits} (on while any of those bits is set)
            cases, default = [{"any": spec["any_bits"], "value": True}], False
        else:
            default = spec.get("default")
        for case in cases:
            b = case.get("byte", spec.get("byte"))
            if b:
                if not isinstance(reading, str) or len(reading) < b:
                    return None
                v = ord(reading[b - 1])
            elif isinstance(reading, bool) or not isinstance(reading, int):
                return None
            else:
                v = reading
            if "equals" in case and v != case["equals"]:
                continue
            if "set" in case and v & case["set"] != case["set"]:
                continue
            if "clear" in case and v & case["clear"]:
                continue
            if "any" in case and not v & case["any"]:
                continue
            return self.coerce(d, case["value"])
        return None if default is None else self.coerce(d, default)

    def coerce(self, d, value, channel=None, limits=None):
        """Check a client value against a control definition; return the canonical value."""
        kind = d.get("kind", "text")
        if kind == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                try:
                    value = float(str(value))
                except ValueError:
                    raise ValueRejected("expected a number, got %r" % (value,))
            value = float(value)
            lo, hi = self.bound(d, "min", channel), self.bound(d, "max", channel)
            if limits:
                lo = limits[0] if lo is None else max(lo, limits[0])
                hi = limits[1] if hi is None else min(hi, limits[1])
            if (lo is not None and value < lo) or (hi is not None and value > hi):
                raise ValueRejected("%g outside %s..%s%s" % (
                    value, "-inf" if lo is None else "%g" % lo, "inf" if hi is None else "%g" % hi,
                    " (instance limit)" if limits else ""))
            return value
        if kind == "bool":
            if isinstance(value, bool):
                return value
            s = str(value).strip().upper()
            if s in _TRUE:
                return True
            if s in _FALSE:
                return False
            raise ValueRejected("expected on/off, got %r" % (value,))
        if kind == "enum":
            opts = d.get("options") or {}
            for name in opts:
                if str(value).upper() == name.upper():
                    return name
            raise ValueRejected("expected one of %s, got %r" % (", ".join(opts), value))
        return str(value)

    def format_value(self, d, value):
        """Canonical value -> the token substituted for {value} in the set template."""
        kind = d.get("kind", "text")
        if kind == "number":
            fmt = d.get("format")
            if fmt:
                return fmt % value
            return ("%.10g" % value) if value != int(value) or abs(value) >= 1e15 else str(int(value))
        if kind == "bool":
            tokens = d.get("values") or {}
            # YAML 1.1 reads unquoted on/off keys as booleans, so accept either spelling.
            tok = tokens.get("on" if value else "off", tokens.get(bool(value), "1" if value else "0"))
            return {True: "1", False: "0"}.get(tok, str(tok)) if isinstance(tok, bool) else str(tok)
        if kind == "enum":
            opt = (d.get("options") or {}).get(value) or {}
            return str(opt.get("token", value))
        return str(value)

    def parse(self, d, text):
        """Instrument response text -> value, using the item's `parse` (default from its kind)."""
        p = d.get("parse") or {"number": "number", "bool": "bool", "enum": "enum"}.get(d.get("kind"), "number")
        text = text.strip()
        if isinstance(p, dict):  # {regex: '...'} then the kind's own parser on group 1
            m = re.search(p["regex"], text)
            if not m:
                raise ValueError("response %r does not match %r" % (text, p["regex"]))
            text = m.group(1) if m.groups() else m.group(0)
            p = p.get("as", "number")
        if p == "list":     # comma-separated numbers (a trace)
            try:
                return [float(x) for x in text.split(",") if x.strip()]
            except ValueError:
                raise ValueError("not a list of numbers: %r..." % text[:40])
        if p in ("number", "int"):
            m = _FLOAT.search(text)
            if not m:
                raise ValueError("no number in response %r" % text)
            return int(float(m.group(0))) if p == "int" else float(m.group(0))
        if p == "bool":
            s = text.upper()
            if s in _TRUE or _FLOAT.fullmatch(s) and float(s) != 0:
                return True
            if s in _FALSE or _FLOAT.fullmatch(s):
                return False
            raise ValueError("not a boolean response: %r" % text)
        if p == "enum":
            for name, opt in (d.get("options") or {}).items():
                opt = opt or {}
                if "match" in opt and re.search(opt["match"], text):
                    return name
                if text.upper() == str(opt.get("response", opt.get("token", name))).upper():
                    return name
            raise ValueError("unknown enum response %r" % text)
        return text

    def describe(self):
        """The model sent to clients (everything needed to render menus and panels)."""
        return {"type": self.name, "version": self.version, "ref": self.ref, "hash": self.hash,
                "title": self.title, "verified": self.verified, "builtin": self.builtin,
                "manufacturer": self.raw.get("manufacturer"), "model": self.raw.get("model"),
                "description": self.raw.get("description"),
                "channels": self.channels, "readouts": self.readouts,
                # enum options keep the type's order (clients' JSON objects put "1", "10" first)
                "controls": {k: dict(v, order=list(v["options"])) if isinstance(v.get("options"), dict) else v
                             for k, v in self.controls.items()},
                "actions": {k: {kk: vv for kk, vv in v.items() if kk != "send"}
                            for k, v in self.actions.items()},
                "menu": self.raw.get("menu") or [], "panel": self.raw.get("panel"),
                "identify": {"probe": self.probe, "match": self.match},
                "has_safe_off": bool(self.safe_off), "roles": self.raw.get("roles") or {},
                "display": self.display, "restore": self.restore}


# ---------------------------------------------------------------------------------------------
def _check_template(errors, path, tpl, allowed):
    if not isinstance(tpl, str):
        errors.append("%s: must be a string" % path)
        return
    for field in re.findall(r"\{([^{}:!]*)", tpl):
        if field.split(".")[0] not in allowed:
            errors.append("%s: unknown placeholder {%s} (allowed: %s)" % (path, field, ", ".join(allowed)))


# Roles: what a type can stand in for in a test script (see docs/device-type-format.md, "Roles").
# Each field names one of the type's controls (c), readouts (r) or actions (a).
ROLE_FIELDS = {
    "supply": {"voltage": "c", "current": "c", "output": "c", "v_meas": "r", "i_meas": "r",
               "ovp": "c", "channel": None},
    "meter": {"function": "c", "functions": None, "ranges": None, "reading": "r", "range": "c", "autorange": "c",
              "nplc": "c", "autozero": "c", "offset_comp": "c", "autocal": "a", "reset": "a"},
    "thermometer": {"temperature": "r", "unit": None},
}
ROLE_REQUIRED = {"supply": ("voltage", "current", "output"), "meter": ("reading",),
                 "thermometer": ("temperature",)}
METER_FUNCTIONS = ("dcv", "acv", "acdcv", "dci", "aci", "acdci", "ohm2", "ohm4", "freq", "period",
                   "temp", "cap", "diode", "cont")


def _check_roles(e, raw, channels):
    roles = raw.get("roles")
    if roles is None:
        return
    if not isinstance(roles, dict):
        e.append("roles: must be a mapping")
        return
    sections = {"c": raw.get("controls") or {}, "r": raw.get("readouts") or {}, "a": raw.get("actions") or {}}
    what = {"c": "control", "r": "readout", "a": "action"}
    for role, spec in roles.items():
        p = "roles.%s" % role
        fields = ROLE_FIELDS.get(role)
        if fields is None:
            e.append("%s: unknown role (one of %s)" % (p, ", ".join(ROLE_FIELDS)))
            continue
        if not isinstance(spec, dict):
            e.append("%s: must be a mapping" % p)
            continue
        for f in ROLE_REQUIRED[role]:
            if f not in spec:
                e.append("%s.%s: required" % (p, f))
        for f, v in spec.items():
            if f not in fields:
                e.append("%s.%s: unknown field (one of %s)" % (p, f, ", ".join(fields)))
                continue
            sec = fields[f]
            if sec:
                if not isinstance(v, str) or v not in (sections[sec] if isinstance(sections[sec], dict) else {}):
                    e.append("%s.%s: must name one of the type's %ss" % (p, f, what[sec]))
            elif f == "channel":
                if v not in channels:
                    e.append("%s.channel: not a listed channel" % p)
            elif f == "unit":
                if v not in ("C", "F", "K"):
                    e.append("%s.unit: C, F or K" % p)
            elif f == "functions":
                fn = (raw.get("controls") or {}).get(spec.get("function")) or {}
                opts = fn.get("options") or {}
                if not isinstance(v, dict):
                    e.append("%s.functions: a mapping of standard name to option" % p)
                    continue
                for std, opt in v.items():
                    if std not in METER_FUNCTIONS:
                        e.append("%s.functions.%s: not a standard function (%s)" % (p, std, ", ".join(METER_FUNCTIONS)))
                    elif opt not in opts:
                        e.append("%s.functions.%s: %r is not an option of %s" % (p, std, opt, spec.get("function")))
        if role == "meter" and isinstance(spec.get("ranges"), dict):
            rc = (raw.get("controls") or {}).get(spec.get("range")) or {}
            ropts = rc.get("options") or {}
            if rc.get("kind") != "enum":
                e.append("%s.ranges: needs range, an enum control" % p)
            for std, table in spec["ranges"].items():
                if std not in METER_FUNCTIONS:
                    e.append("%s.ranges.%s: not a standard function" % (p, std))
                elif not isinstance(table, dict) or not table:
                    e.append("%s.ranges.%s: a mapping of range option to full scale" % (p, std))
                else:
                    for opt, fs in table.items():
                        if opt not in ropts:
                            e.append("%s.ranges.%s: %r is not an option of %s" % (p, std, opt, spec.get("range")))
                        elif isinstance(fs, bool) or not isinstance(fs, (int, float)) or fs <= 0:
                            e.append("%s.ranges.%s.%s: a positive full-scale value" % (p, std, opt))
        elif role == "meter" and "ranges" in spec:
            e.append("%s.ranges: a mapping of standard function to {range option: full scale}" % p)
        if role == "meter" and "functions" in spec and "function" not in spec:
            e.append("%s.function: required with functions" % p)


_CASE_KEYS = {"equals", "set", "clear", "any", "byte", "value"}


def _check_status(e, p, d, readouts, key):
    """controls.X.infer / readback: a setting decoded from a status readout (see status_value)."""
    spec = d[key]
    path = "%s.%s" % (p, key)
    if d.get("get"):
        e.append("%s: not with get (the instrument reports it)" % path)
    if "infer" in d and "readback" in d:
        e.append("%s: use infer or readback, not both" % p)
    if not isinstance(spec, dict) or not isinstance(spec.get("readout"), str):
        e.append("%s: needs readout" % path)
        return
    rd = readouts.get(spec["readout"])
    if not isinstance(rd, dict):
        e.append("%s.readout: no such readout %r" % (path, spec["readout"]))
        rd = None
    elif bool(rd.get("per_channel")) != bool(d.get("per_channel")):
        e.append("%s.readout: per_channel must match the control's" % path)
    if "any_bits" in spec:     # the original form: an on/off setting, on while any bit is set
        if key != "infer" or set(spec) != {"readout", "any_bits"}:
            e.append("%s: needs exactly readout and any_bits (or readout and cases)" % path)
        if d.get("kind") != "bool":
            e.append("%s: only for kind bool" % path)
        if rd is not None and rd.get("parse") != "int":
            e.append("%s.readout: %r must have parse: int" % (path, spec["readout"]))
        bits = spec["any_bits"]
        if isinstance(bits, bool) or not isinstance(bits, int) or bits <= 0:
            e.append("%s.any_bits: a positive integer (a bit mask)" % path)
        return
    extra = set(spec) - {"readout", "byte", "cases", "default"}
    if extra:
        e.append("%s: unknown keys %s" % (path, ", ".join(sorted(extra))))
    if d.get("kind") not in ("bool", "enum"):
        e.append("%s: only for kind bool or enum" % path)
    cases = spec.get("cases")
    if not isinstance(cases, list) or not cases:
        e.append("%s.cases: a non-empty list" % path)
        return
    dt = DeviceType.__new__(DeviceType)        # just for coerce()
    uses_byte = False
    for i, c in enumerate(cases):
        cp = "%s.cases[%d]" % (path, i)
        if not isinstance(c, dict) or "value" not in c or not (set(c) & {"equals", "set", "clear", "any"}):
            e.append("%s: a mapping with value and at least one of equals, set, clear, any" % cp)
            continue
        if set(c) - _CASE_KEYS:
            e.append("%s: unknown keys %s" % (cp, ", ".join(sorted(set(c) - _CASE_KEYS))))
        for k in ("equals", "set", "clear", "any", "byte"):
            if k in c and (isinstance(c[k], bool) or not isinstance(c[k], int) or c[k] < 0):
                e.append("%s.%s: a non-negative integer" % (cp, k))
        uses_byte = uses_byte or bool(c.get("byte", spec.get("byte")))
        try:
            dt.coerce(d, c["value"])
        except ValueRejected as exc:
            e.append("%s.value: %s" % (cp, exc))
    if "default" in spec:
        try:
            dt.coerce(d, spec["default"])
        except ValueRejected as exc:
            e.append("%s.default: %s" % (path, exc))
    if rd is not None:
        want = "text" if uses_byte else "int"
        if rd.get("parse") != want:
            e.append("%s.readout: %r must have parse: %s (%s)" % (
                path, spec["readout"], want,
                "cases test bytes of its text" if uses_byte else "cases test its integer value"))


def _check_display(e, raw):
    """display: what a client draws as the instrument's screen (kind network_analyzer)."""
    disp = raw.get("display")
    if disp is None:
        return
    if not isinstance(disp, dict) or disp.get("kind") != "network_analyzer":
        e.append("display.kind: network_analyzer (the only kind so far)")
        return
    controls, readouts = raw.get("controls") or {}, raw.get("readouts") or {}
    need = {"formatted": readouts, "complex": readouts, "start": controls, "stop": controls,
            "points": controls, "format": controls}
    optional = {"enabled": controls, "scale": controls, "ref_level": controls, "ref_position": controls,
                "measurement": controls, "marker": controls, "marker_freq": controls,
                "marker_value": readouts}
    for k in set(disp) - set(need) - set(optional) - {"kind"}:
        e.append("display.%s: unknown key" % k)
    for k, sec in list(need.items()) + list(optional.items()):
        if k not in disp:
            if k in need:
                e.append("display.%s: required" % k)
            continue
        if disp[k] not in sec:
            e.append("display.%s: %r is not one of the type's %s" % (
                k, disp[k], "readouts" if sec is readouts else "controls"))
        elif not (sec[disp[k]] or {}).get("per_channel") and raw.get("channels"):
            e.append("display.%s: %r must be per_channel" % (k, disp[k]))
    for k in ("formatted", "complex"):
        if disp.get(k) in readouts and (readouts[disp[k]] or {}).get("parse") != "list":
            e.append("display.%s: %r must have parse: list" % (k, disp[k]))


def validate(raw):
    """Return a list of problems with a parsed type file (empty when valid)."""
    e = []
    if not isinstance(raw, dict):
        return ["top level must be a mapping"]
    if raw.get("schema") != SCHEMA:
        e.append("schema: must be %d" % SCHEMA)
    if not isinstance(raw.get("type"), str) or not TYPE_NAME.match(raw["type"]):
        e.append("type: required, lowercase dotted name like 'agilent.e3631a'")
    if not isinstance(raw.get("version"), str) or not VERSION.match(raw["version"]):
        e.append("version: required, quoted semantic version like '1.0.0'")
    known = {"schema", "type", "version", "title", "manufacturer", "model", "description", "verified",
             "connection", "identify", "errors", "init", "channels", "controls", "readouts", "actions",
             "safe_off", "menu", "panel", "sim", "notes", "references", "roles", "restore", "display"}
    for k in raw:
        if k not in known:
            e.append("%s: unknown top-level key" % k)

    conn = raw.get("connection") or {}
    if not isinstance(conn, dict):
        e.append("connection: must be a mapping")
        conn = {}
    for k in ("eos", "write_termination"):
        v = conn.get(k)
        if v is not None and not (isinstance(v, str) and len(v) <= 2):
            e.append("connection.%s: a 1-2 character string or null" % k)
    for k in ("timeout", "delay"):
        try:
            parse_seconds(conn.get(k))
        except ValueError as exc:
            e.append("connection.%s: %s" % (k, exc))

    ident = raw.get("identify") or {}
    if ident.get("match") is not None:
        try:
            re.compile(ident["match"])
        except re.error as exc:
            e.append("identify.match: %s" % exc)

    ch = raw.get("channels") or {}
    channels = (ch.get("list") or {}) if isinstance(ch, dict) else {}
    if ch and not isinstance(channels, dict):
        e.append("channels.list: must be a mapping")
        channels = {}
    for name in channels:
        if not KEY.match(str(name)):
            e.append("channels.list.%s: bad channel name" % name)
    if ch.get("select"):
        _check_template(e, "channels.select", ch["select"], ["channel"])

    names = set()
    unnamed = []    # per-channel commands that don't name their channel, so need channels.select
    for section in ("controls", "readouts", "actions"):
        items = raw.get(section) or {}
        if not isinstance(items, dict):
            e.append("%s: must be a mapping" % section)
            continue
        for name, d in items.items():
            p = "%s.%s" % (section, name)
            if not KEY.match(str(name)):
                e.append("%s: bad name (letters, digits, _)" % p)
            if name in names:
                e.append("%s: name used twice" % p)
            names.add(name)
            if not isinstance(d, dict):
                e.append("%s: must be a mapping" % p)
                continue
            per = bool(d.get("per_channel"))
            if per and not channels:
                e.append("%s: per_channel but the type has no channels" % p)
            allowed = ["channel", "value"] if section == "controls" else ["channel"]
            cmds = [d.get(k) for k in ("set", "get", "query") if isinstance(d.get(k), str)]
            cmds += [c for c in (d.get("send") or []) if isinstance(c, str)] if isinstance(d.get("send"), list) else []
            for c in cmds:
                if per and "{channel" not in c:
                    unnamed.append(p)
                for attr in re.findall(r"\{channel\.([A-Za-z_]\w*)\}", c):
                    if not per:
                        e.append("%s: {channel.%s} in a command that isn't per_channel" % (p, attr))
                    elif any(attr not in (v or {}) for v in channels.values()):
                        e.append("%s: not every channel has %r" % (p, attr))
            if section == "controls":
                kind = d.get("kind")
                if kind not in KINDS:
                    e.append("%s.kind: one of %s" % (p, ", ".join(KINDS)))
                if not d.get("set"):
                    e.append("%s.set: required" % p)
                else:
                    _check_template(e, p + ".set", d["set"], allowed)
                if d.get("get"):
                    _check_template(e, p + ".get", d["get"], ["channel"])
                for key in ("infer", "readback"):
                    if key in d:
                        _check_status(e, p, d, raw.get("readouts") or {}, key)
                if "restore" in d and not isinstance(d["restore"], bool):
                    e.append("%s.restore: true or false" % p)
                if kind == "enum" and not isinstance(d.get("options"), dict):
                    e.append("%s.options: required mapping for enum" % p)
                for b in ("min", "max"):
                    v = d.get(b)
                    if isinstance(v, str):
                        m = re.match(r"^\{channel\.([A-Za-z_]\w*)\}$", v.strip())
                        if not m or not per:
                            e.append("%s.%s: a number, or '{channel.attr}' on a per-channel control" % (p, b))
                        elif any(m.group(1) not in (c or {}) for c in channels.values()):
                            e.append("%s.%s: not every channel has %r" % (p, b, m.group(1)))
                    elif v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
                        e.append("%s.%s: must be a number" % (p, b))
            elif section == "readouts":
                if not d.get("query") and not d.get("read"):
                    e.append("%s: needs 'query' or 'read: true'" % p)
                if d.get("query"):
                    _check_template(e, p + ".query", d["query"], allowed)
                if "clear_srq" in d and not isinstance(d["clear_srq"], bool):
                    e.append("%s.clear_srq: true or false" % p)
                if d.get("trigger") not in (None, "get"):
                    e.append("%s.trigger: only 'get' is supported" % p)
                try:
                    parse_seconds(d.get("poll"))
                except ValueError as exc:
                    e.append("%s.poll: %s" % (p, exc))
            else:
                send = d.get("send")
                if not isinstance(send, list) or not send:
                    e.append("%s.send: required non-empty list of commands" % p)
                else:
                    for i, c in enumerate(send):
                        _check_template(e, "%s.send[%d]" % (p, i), c, allowed)
            parse = d.get("parse")
            if isinstance(parse, dict):
                try:
                    re.compile(parse.get("regex", ""))
                except re.error as exc:
                    e.append("%s.parse.regex: %s" % (p, exc))
                if parse.get("as", "number") not in PARSERS:
                    e.append("%s.parse.as: one of %s" % (p, ", ".join(PARSERS)))
            elif parse is not None and parse not in PARSERS:
                e.append("%s.parse: one of %s, or {regex, as}" % (p, ", ".join(PARSERS)))

    def check_menu(entries, path):
        if not isinstance(entries, list):
            e.append("%s: must be a list" % path)
            return
        for i, ent in enumerate(entries):
            if isinstance(ent, str):
                if ent not in names:
                    e.append("%s[%d]: unknown item %r" % (path, i, ent))
            elif isinstance(ent, dict) and len(ent) == 1:
                label, children = next(iter(ent.items()))
                check_menu(children, "%s[%d].%s" % (path, i, label))
            else:
                e.append("%s[%d]: an item name or {label: [items]}" % (path, i))
    if raw.get("menu") is not None:
        check_menu(raw["menu"], "menu")
    if unnamed and channels and not ch.get("select"):
        e.append("channels.select: required, since %s doesn't name its channel" % unnamed[0])
    _check_roles(e, raw, channels)
    _check_display(e, raw)
    if "restore" in raw and not isinstance(raw["restore"], bool):
        e.append("restore: true or false")
    for k in ("init", "safe_off"):
        v = raw.get(k)
        if v is not None and (not isinstance(v, list) or not all(isinstance(c, str) for c in v)):
            e.append("%s: must be a list of command strings" % k)
    return e
