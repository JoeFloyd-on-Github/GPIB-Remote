"""gpibctl commands. Each handler takes (ctx, args); output goes to ctx.out.

The same parser serves one-shot use (`gpibctl set ...`) and the interactive shell, where one
connection is kept so a control lock lasts until you release it or leave.

Everything goes through the test-script API (gpibctl.api, docs/api.md): `ctx.bench` is a
Bench, instruments are Device handles. What the CLI can do, a script can do the same way.
"""

import argparse
import json
import sys
import time

from . import ServerError, fmt

EPILOG = """examples:
  gpibctl ls                                   instruments, their types and lock state
  gpibctl state bench-psu                      every setting and reading
  gpibctl set bench-psu P6V.voltage 5          change a setting (refused if someone else holds the lock)
  gpibctl get bench-psu P6V.v_meas             read a measurement now
  gpibctl watch bench-psu                      live readings and changes (Ctrl-C to stop)
  gpibctl menu bench-psu                       menu-driven control, with the lock
  gpibctl shell                                interactive session (keeps locks)
  gpibctl type show agilent.e3631a             what a type offers
  gpibctl inst create psu2 hp.6632a@1.0.0 6    add an instrument
  gpibctl query 5 "*IDN?"                      raw GPIB
"""


class Context:
    """The CLI's connection: `bench` (a gpibctl.api.Bench, opened on first use) plus where
    output goes and where answers come from."""

    def __init__(self, connect, out=None, inp=None, interactive=False):
        self._connect = connect
        self._bench = None
        self.out = out or sys.stdout
        self.inp = inp or input
        self.interactive = interactive

    @property
    def bench(self):
        if self._bench is None:
            self._bench = self._connect()
        return self._bench

    def dev(self, iid):
        return self.bench.device(iid)

    def say(self, *parts):
        print(*parts, file=self.out)

    def model(self, ref):
        return self.bench.type_model(ref)

    def instance_model(self, iid):
        st = self.dev(iid).snapshot()
        return st, self.model(st["type"])

    def notices(self):
        """Print lock/status notices that arrived between commands; drop the rest."""
        if self._bench is None:
            return
        for ev in self._bench.events():
            kind = ev.get("event")
            if kind == "lock_broken":
                self.say("!! your lock on %s was broken by %s: %s" % (ev["instance"], ev["by"], ev.get("reason", "")))
            elif kind == "lock_expired":
                self.say("!! your lock on %s expired (idle)" % ev["instance"])
            elif kind == "status" and ev.get("online") is False:
                self.say("!! %s is not responding: %s" % (ev["instance"], ev.get("error")))

    def close(self):
        if self._bench is not None:
            self._bench.close()
            self._bench = None


# -- server and listing ---------------------------------------------------------------------
def cmd_info(ctx, a):
    info = ctx.bench.info()
    ctx.say("server %s, protocol %s, up %s" % (info["version"], info["protocol"], fmt.age(time.time() - info["uptime"]).replace(" ago", "")))
    for b in info["boards"]:
        ctx.say("  board %d: %s" % (b["board"], b["backend"]))
    for s in info.get("sessions", []):
        ctx.say("  session #%d %s %s" % (s["id"], s["who"], s["peer"]))


def cmd_ls(ctx, a):
    rows = []
    for i in ctx.bench.instances():
        ad = i["address"]
        addr = "%d:%d" % (ad["board"], ad["pad"]) + (":%d" % ad["sad"] if ad["sad"] else "")
        online = {True: "online", False: "OFFLINE", None: "-"}[i["online"]]
        rows.append([i["id"], i["type"], addr, online, fmt.lock_text(i["lock"]), i["name"]])
    ctx.say(fmt.table(rows, ["INSTANCE", "TYPE", "ADDR", "STATUS", "LOCK", "NAME"]) if rows else "no instances")


def cmd_types(ctx, a):
    rows = [[t["type"], ", ".join(t["versions"]), "yes" if t["verified"] else "no",
             "built-in" if t["builtin"] else "added", t["title"]] for t in ctx.bench.types()]
    ctx.say(fmt.table(rows, ["TYPE", "VERSIONS", "VERIFIED", "SOURCE", "TITLE"]))


def cmd_type_show(ctx, a):
    m = ctx.model(a.type)
    ctx.say("%s  %s%s" % (m["ref"], m["title"], "" if m["verified"] else "  (not verified on hardware)"))
    if m.get("description"):
        ctx.say("  " + " ".join(m["description"].split()))
    if m["channels"]:
        ctx.say("channels: " + ", ".join("%s (%s)" % (k, v.get("label", k)) for k, v in m["channels"].items()))
    rows = []
    for name, d in m["controls"].items():
        kind = d.get("kind")
        if kind == "enum":
            kind = "enum: " + "|".join(d.get("options") or {})
        rng = fmt.range_text(m, d, next(iter(m["channels"]), None)) if d.get("kind") == "number" else ""
        rows.append(["control", ("<ch>." if d.get("per_channel") else "") + name, kind, rng,
                     "" if d.get("get") else "set-only", d.get("label", "")])
    for name, d in m["readouts"].items():
        rows.append(["readout", ("<ch>." if d.get("per_channel") else "") + name, d.get("unit", ""),
                     ("poll %s" % d["poll"]) if d.get("poll") else "", "", d.get("label", "")])
    for name, d in m["actions"].items():
        rows.append(["action", name, "", "", "confirm" if d.get("confirm") else "", d.get("label", "")])
    if rows:
        ctx.say(fmt.table(rows, ["", "KEY", "KIND/UNIT", "RANGE", "", "LABEL"]))


def cmd_type_yaml(ctx, a):
    text = ctx.bench.type_yaml(a.type)
    if a.output:
        with open(a.output, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        ctx.say("saved %s" % a.output)
    else:
        ctx.out.write(text)


def _read_file(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def cmd_type_validate(ctx, a):
    r = ctx.bench.validate_type(_read_file(a.file))
    if r["valid"]:
        ctx.say("valid: %s%s" % (r["ref"], " (this version already exists on the server)" if r["exists"] else ""))
    else:
        ctx.say("invalid:")
        for e in r["errors"]:
            ctx.say("  " + e)
        return 1


def cmd_type_push(ctx, a):
    r = ctx.bench.push_type(_read_file(a.file), a.message)
    ctx.say("%s %s" % ("added" if r["added"] else "unchanged (identical version already stored):", r["ref"]))


def cmd_history(ctx, commits):
    for c in commits:
        ctx.say("%s  %s  %-24s %s" % (c["rev"], c["date"][:16], c["author"], c["message"]))


# -- instances ------------------------------------------------------------------------------------
def _overrides(a, base=None):
    ov = json.loads(json.dumps(base or {}))
    for spec in a.limit or []:
        try:
            key, rng = spec.split("=", 1)
            lo, hi = (float(x) for x in rng.split(":", 1))
        except ValueError:
            raise SystemExit("--limit expects KEY=MIN:MAX, e.g. P6V.voltage=0:5.5")
        ov.setdefault("limits", {})[key] = [lo, hi]
    for key in a.unlimit or []:
        (ov.get("limits") or {}).pop(key, None)
    for spec in a.poll or []:
        name, _, iv = spec.partition("=")
        if not iv:
            raise SystemExit("--poll expects READOUT=INTERVAL, e.g. v_meas=1s or v_meas=off")
        ov.setdefault("poll", {})[name] = iv
    return {k: v for k, v in ov.items() if v}


def _instance_fields(a, inst):
    if a.name is not None:
        inst["name"] = a.name
    if a.notes is not None:
        inst["notes"] = a.notes
    if a.poll_mode:
        inst["poll_mode"] = a.poll_mode
    ss = dict(inst.get("safe_state") or {})
    if a.off_on_disconnect is not None:
        ss["on_client_disconnect"] = "outputs_off" if a.off_on_disconnect else "leave"
    if a.off_on_stop is not None:
        ss["on_server_stop"] = "outputs_off" if a.off_on_stop else "leave"
    if ss:
        inst["safe_state"] = ss
    ov = _overrides(a, inst.get("overrides"))
    if ov or inst.get("overrides"):
        inst["overrides"] = ov
    return inst


def cmd_inst_show(ctx, a):
    d = ctx.dev(a.instance)
    s = d.summary()
    ctx.out.write(json.dumps(d.config, indent=2) + "\n")
    ctx.say("status: %s; lock: %s" % ({True: "online", False: "OFFLINE", None: "not contacted yet"}[s["online"]],
                                       fmt.lock_text(s["lock"])))


def cmd_inst_create(ctx, a):
    f = _instance_fields(a, {})
    d = ctx.bench.add(a.id, a.type, a.pad, board=a.board, sad=a.sad, name=f.get("name"), notes=f.get("notes"),
                      verify=not a.no_verify, poll_mode=f.get("poll_mode"), overrides=f.get("overrides"),
                      safe_state=f.get("safe_state"))
    ctx.say("created %s (%s at %d:%d)" % (a.id, d.type, a.board, a.pad))
    ident = d.identify_result
    if ident:
        if ident.get("error"):
            ctx.say("  identify: no answer (%s)" % ident["error"])
        else:
            ctx.say("  identify: %s -> %r %s" % (ident["probe"], ident["response"],
                                                 "(matches)" if ident["match"] else "(DOES NOT MATCH the type)"))


def cmd_inst_update(ctx, a):
    d = ctx.dev(a.instance)
    inst = d.reload().config
    changes = _instance_fields(a, dict(inst))
    if a.type:
        changes["type"] = ctx.bench.type_ref(a.type)
    if a.pad is not None or a.board is not None or a.sad is not None:
        ad = dict(inst["address"])
        for k in ("board", "pad", "sad"):
            if getattr(a, k) is not None:
                ad[k] = getattr(a, k)
        changes["address"] = ad
    changes = {k: v for k, v in changes.items() if inst.get(k) != v and k != "revision"}
    if not changes:
        ctx.say("nothing to change")
        return
    new = d.update(revision=inst["revision"], **changes)
    ctx.say("updated %s (revision %d): %s" % (a.instance, new["revision"], ", ".join(sorted(changes))))


def cmd_inst_delete(ctx, a):
    if not a.yes and not confirm(ctx, "delete instance %s?" % a.instance):
        return 1
    ctx.bench.remove(a.instance)
    ctx.say("deleted %s" % a.instance)


def cmd_inst_identify(ctx, a):
    r = ctx.dev(a.instance).identify()
    if not r["probe"]:
        ctx.say("this type has no identity query")
    else:
        ctx.say("%s -> %r %s" % (r["probe"], r["response"], "(matches)" if r["match"] else "(DOES NOT MATCH)"))


# -- device control -----------------------------------------------------------------------------
def show_state(ctx, iid, keys=None):
    st, m = ctx.instance_model(iid)
    s = st["summary"]
    ctx.say("%s  %s at %d:%d  %s  %s" % (iid, st["type"], s["address"]["board"], s["address"]["pad"],
                                       {True: "online", False: "OFFLINE", None: "-"}[s["online"]],
                                       fmt.lock_text(s["lock"])))
    rows = []
    for key, e in st["state"].items():
        if keys and key not in keys:
            continue
        section, d, ch = fmt.item_of(m, key)
        rows.append([key, fmt.value(e["value"], d, e["source"]),
                     "setting" if section == "controls" else "reading", fmt.age(e["t"]), d.get("label", "")])
    ctx.say(fmt.table(rows, ["KEY", "VALUE", "", "UPDATED", "LABEL"]))


def cmd_state(ctx, a):
    if a.refresh:
        d = ctx.dev(a.instance)
        d.refresh()
        st, m = ctx.instance_model(a.instance)
        for key in st["state"]:
            if fmt.item_of(m, key)[0] == "readouts":
                try:
                    d.get_entry(key)
                except ServerError:
                    pass
    show_state(ctx, a.instance)


def cmd_get(ctx, a):
    st, m = ctx.instance_model(a.instance)
    for key in a.keys:
        e = ctx.dev(a.instance).get_entry(key, fresh=not a.cached)
        if a.bare:
            v = e["value"]
            ctx.say("" if v is None else ("on" if v else "off") if isinstance(v, bool) else v)
        else:
            ctx.say("%s = %s" % (key, fmt.value(e["value"], fmt.item_of(m, key)[1], e["source"])))


def cmd_set(ctx, a):
    st, m = ctx.instance_model(a.instance)
    pairs = list(a.pairs)
    if len(pairs) % 2:
        raise SystemExit("set expects KEY VALUE pairs")
    for key, val in zip(pairs[::2], pairs[1::2]):
        e = ctx.dev(a.instance).set_entry(key, val)
        ctx.say("%s = %s" % (key, fmt.value(e["value"], fmt.item_of(m, key)[1], e["source"])))


def confirm(ctx, question):
    try:
        return ctx.inp("%s [y/N] " % question).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cmd_do(ctx, a):
    st, m = ctx.instance_model(a.instance)
    d = (m.get("actions") or {}).get(a.action)
    if d is None:
        raise SystemExit("no action %r; actions: %s" % (a.action, ", ".join(m.get("actions") or {}) or "none"))
    if d.get("confirm") and not a.yes and not confirm(ctx, "%s: %s?" % (a.instance, d.get("label", a.action))):
        ctx.say("cancelled")
        return 1
    ctx.dev(a.instance).action(a.action, a.channel)
    ctx.say("done: %s" % d.get("label", a.action))


def cmd_lock(ctx, a):
    if not ctx.interactive:
        ctx.say("note: a lock lasts only while this connection is open; use it in `gpibctl shell` or `gpibctl menu`")
    lk = ctx.dev(a.instance).lock(a.reason or "", force=a.force)
    ctx.say("%s: locked by you (%s)" % (a.instance, lk.info["holder"]))


def cmd_unlock(ctx, a):
    ctx.dev(a.instance).unlock()
    ctx.say("%s: released" % a.instance)


def cmd_locks(ctx, a):
    locks = ctx.bench.locks()
    rows = [[iid, l["holder"], fmt.age(l["since"]), "%ds" % l["idle"]] for iid, l in locks.items()]
    ctx.say(fmt.table(rows, ["INSTANCE", "HOLDER", "SINCE", "IDLE"]) if rows else "no locks held")


def cmd_watch(ctx, a):
    st, m = ctx.instance_model(a.instance)
    keys = set(a.keys or [])
    dev = ctx.dev(a.instance)
    ctx.bench.events()                      # drop anything queued before the watch
    dev.subscribe()
    ctx.say("watching %s (Ctrl-C to stop)" % a.instance)
    end = time.monotonic() + a.seconds if a.seconds else None
    count = 0
    try:
        while a.count is None or count < a.count:
            left = None if end is None else end - time.monotonic()
            if left is not None and left <= 0:
                break
            try:
                ev = ctx.bench.wait_event(timeout=min(1.0, left) if left else 1.0)
            except TimeoutError:
                continue
            if ev.get("instance") != a.instance and ev.get("event") not in ("lock_broken",):
                continue
            kind = ev.get("event")
            if kind in ("reading", "state"):
                if keys and ev["key"] not in keys:
                    continue
                d = fmt.item_of(m, ev["key"])[1]
                extra = ("  (set by %s)" % ev["by"]) if ev.get("by") else ""
                ctx.say("%s  %-16s %s%s" % (fmt.clock(ev["t"]), ev["key"], fmt.value(ev["value"], d, ev.get("source")), extra))
                count += 1
            elif kind == "lock":
                ctx.say("%s  lock: %s" % (fmt.clock(), fmt.lock_text(ev["lock"])))
            elif kind == "status":
                ctx.say("%s  %s" % (fmt.clock(), "online" if ev["online"] else "NOT RESPONDING: %s" % ev.get("error")))
    except KeyboardInterrupt:
        pass
    finally:
        try:
            dev.unsubscribe()
        except Exception:
            pass


def cmd_menu(ctx, a):
    from .menu import Menu
    return Menu(ctx, a.instance, lock=not a.no_lock).run()


def cmd_shell(ctx, a):
    from .shell import Shell
    Shell(ctx).cmdloop()


def cmd_config(ctx, a):
    from . import config
    ctx.say("saved %s:%d to %s" % (a.host, a.port, config.save(a.host, a.port)))


# -- raw ------------------------------------------------------------------------------------------
def cmd_raw(ctx, a):
    if a.cmd == "listeners":
        ctx.say(" ".join(str(p) for p in ctx.bench.listeners(a.board)) or "none")
        return
    raw = ctx.bench.raw(a.pad, a.board)
    if a.cmd == "query":
        ctx.say(raw.query(a.data, timeout=a.timeout))
    elif a.cmd == "write":
        raw.write(a.data, timeout=a.timeout)
    elif a.cmd == "read":
        ctx.say(raw.read(timeout=a.timeout))
    elif a.cmd == "poll":
        ctx.say(raw.serial_poll())
    else:
        {"clear": raw.clear, "local": raw.local, "trigger": raw.trigger}[a.cmd]()


# -- parser ---------------------------------------------------------------------------------------
class Parser(argparse.ArgumentParser):
    """In the shell, errors raise instead of exiting the program."""

    def error(self, message):
        raise SystemExit("%s: %s" % (self.prog, message))


def build_parser(prog="gpibctl", with_connection=True):
    p = Parser(prog=prog, description="Command-line client for the GPIB-Remote server.",
               epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    if with_connection:
        from . import config
        host, port = config.defaults()
        p.add_argument("--host", default=host, help="server (default %(default)s; see `gpibctl config`)")
        p.add_argument("--port", type=int, default=port)
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND", parser_class=Parser)
    sub.required = True

    def add(name, fn, help, aliases=()):
        sp = sub.add_parser(name, help=help, description=help, aliases=list(aliases))
        if fn is not None:
            sp.set_defaults(fn=fn)
        return sp

    add("info", cmd_info, "server version, boards and connected clients")
    add("ls", cmd_ls, "list instruments (instances)", aliases=["list"])
    add("types", cmd_types, "list device types")
    sp = add("state", cmd_state, "show every setting and reading of an instrument")
    sp.add_argument("instance")
    sp.add_argument("-r", "--refresh", action="store_true", help="read everything from the instrument first")
    sp = add("get", cmd_get, "read settings or measurements from the instrument")
    sp.add_argument("instance")
    sp.add_argument("keys", nargs="+", metavar="KEY")
    sp.add_argument("--cached", action="store_true", help="last known value, without asking the instrument")
    sp.add_argument("--bare", action="store_true", help="print only the value (for scripts)")
    sp = add("set", cmd_set, "change settings: set INSTANCE KEY VALUE [KEY VALUE ...]")
    sp.add_argument("instance")
    sp.add_argument("pairs", nargs="+", metavar="KEY VALUE")
    sp = add("do", cmd_do, "run an action (reset, ...)")
    sp.add_argument("instance")
    sp.add_argument("action")
    sp.add_argument("--channel")
    sp.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    sp = add("watch", cmd_watch, "print live readings and changes")
    sp.add_argument("instance")
    sp.add_argument("keys", nargs="*", metavar="KEY", help="only these keys")
    sp.add_argument("-n", "--count", type=int, help="stop after N updates")
    sp.add_argument("-t", "--seconds", type=float, help="stop after this many seconds")
    sp = add("menu", cmd_menu, "menu-driven control of an instrument")
    sp.add_argument("instance")
    sp.add_argument("--no-lock", action="store_true", help="observe only; take the lock later with 'l'")
    add("shell", cmd_shell, "interactive session on one connection (locks persist)")
    sp = add("lock", cmd_lock, "take the control lock (useful in the shell)")
    sp.add_argument("instance")
    sp.add_argument("--force", action="store_true", help="break someone else's lock")
    sp.add_argument("--reason")
    sp = add("unlock", cmd_unlock, "release the control lock")
    sp.add_argument("instance")
    add("locks", cmd_locks, "list held locks")

    sp = add("type", None, "device types: show, yaml, validate, push, history")
    tsub = sp.add_subparsers(dest="sub", metavar="SUBCOMMAND", parser_class=Parser)
    tsub.required = True
    t = tsub.add_parser("show", help="controls, readouts and actions of a type")
    t.add_argument("type")
    t.set_defaults(fn=cmd_type_show)
    t = tsub.add_parser("yaml", help="print or save a type's YAML file")
    t.add_argument("type")
    t.add_argument("-o", "--output")
    t.set_defaults(fn=cmd_type_yaml)
    t = tsub.add_parser("validate", help="check a type file without storing it")
    t.add_argument("file")
    t.set_defaults(fn=cmd_type_validate)
    t = tsub.add_parser("push", help="store a new type (or new version) on the server")
    t.add_argument("file")
    t.add_argument("-m", "--message")
    t.set_defaults(fn=cmd_type_push)
    t = tsub.add_parser("history", help="change history of a type")
    t.add_argument("type", nargs="?")
    t.add_argument("--limit", type=int, default=30)
    t.set_defaults(fn=lambda ctx, a: cmd_history(ctx, ctx.bench.type_history(a.type, a.limit)))

    sp = add("inst", None, "instances: show, create, update, delete, identify, history")
    isub = sp.add_subparsers(dest="sub", metavar="SUBCOMMAND", parser_class=Parser)
    isub.required = True

    def inst_options(q, update):
        q.add_argument("--name")
        q.add_argument("--notes")
        q.add_argument("--limit", action="append", metavar="KEY=MIN:MAX", help="narrow a setting's range (repeatable)")
        q.add_argument("--unlimit", action="append", metavar="KEY", help="remove a limit (repeatable)")
        q.add_argument("--poll", action="append", metavar="READOUT=INTERVAL", help="poll interval, e.g. v_meas=1s or off")
        q.add_argument("--poll-mode", choices=["subscribed", "always", "off"])
        q.add_argument("--off-on-disconnect", dest="off_on_disconnect", action="store_true", default=None,
                       help="switch outputs off when the controlling client disconnects")
        q.add_argument("--leave-on-disconnect", dest="off_on_disconnect", action="store_false")
        q.add_argument("--off-on-stop", dest="off_on_stop", action="store_true", default=None,
                       help="switch outputs off when the server stops")
        q.add_argument("--leave-on-stop", dest="off_on_stop", action="store_false")

    i = isub.add_parser("show", help="an instance's stored settings")
    i.add_argument("instance")
    i.set_defaults(fn=cmd_inst_show)
    i = isub.add_parser("create", help="add an instrument: create ID TYPE PAD")
    i.add_argument("id")
    i.add_argument("type", help="type, or type@version (default: latest)")
    i.add_argument("pad", type=int, help="GPIB primary address")
    i.add_argument("--board", type=int, default=0)
    i.add_argument("--sad", type=int, default=0)
    i.add_argument("--no-verify", action="store_true", help="don't send the identity query")
    inst_options(i, False)
    i.set_defaults(fn=cmd_inst_create)
    i = isub.add_parser("update", help="change an instance's settings")
    i.add_argument("instance")
    i.add_argument("--type", help="move to another type version, e.g. agilent.e3631a@1.1.0")
    i.add_argument("--pad", type=int)
    i.add_argument("--board", type=int)
    i.add_argument("--sad", type=int)
    inst_options(i, True)
    i.set_defaults(fn=cmd_inst_update)
    i = isub.add_parser("delete", help="remove an instance (the instrument is not touched)")
    i.add_argument("instance")
    i.add_argument("-y", "--yes", action="store_true")
    i.set_defaults(fn=cmd_inst_delete)
    i = isub.add_parser("identify", help="send the type's identity query")
    i.add_argument("instance")
    i.set_defaults(fn=cmd_inst_identify)
    i = isub.add_parser("history", help="change history of an instance")
    i.add_argument("instance")
    i.add_argument("--limit", type=int, default=30)
    i.set_defaults(fn=lambda ctx, a: cmd_history(ctx, ctx.dev(a.instance).history(a.limit)))

    for name, hlp in (("listeners", "addresses with a device present"),):
        sp = add(name, cmd_raw, "raw: " + hlp)
        sp.add_argument("--board", type=int, default=0)
    for name, hlp in (("query", "raw: write a command and read the answer"), ("write", "raw: write a command")):
        sp = add(name, cmd_raw, hlp)
        sp.add_argument("pad", type=int)
        sp.add_argument("data")
        sp.add_argument("--board", type=int, default=0)
        sp.add_argument("--timeout", type=float, default=3.0)
    for name, hlp in (("read", "read from a device"), ("poll", "serial poll (status byte)"), ("clear", "device clear"),
                      ("local", "return to front-panel control"), ("trigger", "group execute trigger")):
        sp = add(name, cmd_raw, "raw: " + hlp)
        sp.add_argument("pad", type=int)
        sp.add_argument("--board", type=int, default=0)
        sp.add_argument("--timeout", type=float, default=3.0)
    if with_connection:
        sp = add("config", cmd_config, "save --host/--port as the default server")
    return p


def run(ctx, args):
    """Run one parsed command; returns an exit status."""
    try:
        return args.fn(ctx, args) or 0
    except ServerError as exc:
        ctx.say("error: %s" % exc)
        return 1
    finally:
        ctx.notices()
