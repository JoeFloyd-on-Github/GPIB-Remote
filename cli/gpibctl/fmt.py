"""Text formatting for the CLI (ASCII only, so it works in any console)."""

import math
import time


def decimals(resolution):
    if not resolution or resolution <= 0:
        return None
    return max(0, int(-math.floor(math.log10(resolution) + 1e-9)))


def value(v, item=None, source=None):
    """A value for display, using the item's unit and resolution, marked when assumed/unknown."""
    item = item or {}
    if v is None:
        return "?"
    if isinstance(v, list):     # a trace: say how big it is (get --full prints it)
        nums = [x for x in v if isinstance(x, (int, float)) and not isinstance(x, bool)]
        text = "%d values" % len(v)
        if nums:
            text += ", %.6g .. %.6g" % (min(nums), max(nums))
    elif isinstance(v, bool):
        text = "ON" if v else "OFF"
    elif isinstance(v, (int, float)):
        d = decimals(item.get("resolution"))
        text = ("%.*f" % (d, v)) if d is not None and isinstance(v, float) else ("%.7g" % v)
        if item.get("unit"):
            text += " " + item["unit"]
    else:
        text = str(v)
        opt = (item.get("options") or {}).get(v) if item.get("kind") == "enum" else None
        if opt and opt.get("label"):
            text += " (%s)" % opt["label"]
    if source in ("assumed", "inferred"):
        text += "  [%s]" % source
    return text


def age(t):
    if not t:
        return ""
    s = time.time() - t
    if s < 60:
        return "%ds ago" % s
    if s < 3600:
        return "%dm ago" % (s // 60)
    return "%dh ago" % (s // 3600)


def clock(t=None):
    return time.strftime("%H:%M:%S", time.localtime(t or time.time()))


def table(rows, headers=None):
    """Left-aligned columns."""
    rows = [[str(c) for c in r] for r in rows]
    if headers:
        rows.insert(0, list(headers))
    if not rows:
        return ""
    widths = [max(len(r[i]) for r in rows if i < len(r)) for i in range(max(len(r) for r in rows))]
    lines = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip() for r in rows]
    if headers:
        lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def item_of(model, key):
    """(section, definition, channel) for a key such as 'P6V.voltage', or (None, {}, None)."""
    ch, name = (key.split(".", 1) if "." in key else (None, key))
    for section in ("controls", "readouts", "actions"):
        d = (model.get(section) or {}).get(name)
        if d is not None:
            return section, d, ch
    return None, {}, ch


def range_text(model, d, channel):
    def bound(b):
        v = d.get(b)
        if isinstance(v, str) and v.startswith("{channel."):
            v = (model.get("channels", {}).get(channel) or {}).get(v[len("{channel."):-1])
        return v
    lo, hi = bound("min"), bound("max")
    if lo is None and hi is None:
        return ""
    unit = d.get("unit", "")
    return "%s..%s%s" % ("" if lo is None else "%g" % lo, "" if hi is None else "%g" % hi,
                         " " + unit if unit else "")


def lock_text(lock):
    if not lock:
        return "free"
    return "locked by %s (%s)" % (lock["holder"], age(lock["since"]).replace(" ago", ""))
