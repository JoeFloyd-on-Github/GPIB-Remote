"""Device types: the human-editable model of an instrument (see docs/device-type-format.md).

A type file is YAML. This module validates it, and turns values into command strings and
instrument responses into values. It does no I/O.
"""

import hashlib
import re

import yaml

SCHEMA = 1
KINDS = ("number", "bool", "enum", "text")
PARSERS = ("number", "int", "bool", "text", "enum")
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
        # readout name -> controls whose on/off state is inferred from it (controls.*.infer)
        self.inferred_by = {}
        for cname, d in self.controls.items():
            if d.get("infer"):
                self.inferred_by.setdefault(d["infer"]["readout"], []).append(cname)
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
                "channels": self.channels, "controls": self.controls, "readouts": self.readouts,
                "actions": {k: {kk: vv for kk, vv in v.items() if kk != "send"}
                            for k, v in self.actions.items()},
                "menu": self.raw.get("menu") or [], "panel": self.raw.get("panel"),
                "identify": {"probe": self.probe, "match": self.match},
                "has_safe_off": bool(self.safe_off), "roles": self.raw.get("roles") or {}}


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
    "meter": {"function": "c", "functions": None, "reading": "r", "range": "c", "autorange": "c",
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
        if role == "meter" and "functions" in spec and "function" not in spec:
            e.append("%s.function: required with functions" % p)


def _check_infer(e, p, d, readouts):
    """controls.X.infer: an on/off setting the instrument can't report, judged from a status
    readout: on when any of `any_bits` is set in the readout's (integer) value, otherwise off."""
    inf = d["infer"]
    if d.get("kind") != "bool":
        e.append("%s.infer: only for kind bool" % p)
    if d.get("get"):
        e.append("%s.infer: not with get (the instrument reports it)" % p)
    if not isinstance(inf, dict) or set(inf) != {"readout", "any_bits"}:
        e.append("%s.infer: needs exactly readout and any_bits" % p)
        return
    rd = readouts.get(inf["readout"])
    if not isinstance(rd, dict):
        e.append("%s.infer.readout: no such readout %r" % (p, inf["readout"]))
    else:
        if rd.get("parse") != "int":
            e.append("%s.infer.readout: %r must have parse: int" % (p, inf["readout"]))
        if bool(rd.get("per_channel")) != bool(d.get("per_channel")):
            e.append("%s.infer.readout: per_channel must match the control's" % p)
    bits = inf["any_bits"]
    if isinstance(bits, bool) or not isinstance(bits, int) or bits <= 0:
        e.append("%s.infer.any_bits: a positive integer (a bit mask)" % p)


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
             "safe_off", "menu", "panel", "sim", "notes", "references", "roles"}
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
                if "infer" in d:
                    _check_infer(e, p, d, raw.get("readouts") or {})
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
    for k in ("init", "safe_off"):
        v = raw.get(k)
        if v is not None and (not isinstance(v, list) or not all(isinstance(c, str) for c in v)):
            e.append("%s: must be a list of command strings" % k)
    return e
