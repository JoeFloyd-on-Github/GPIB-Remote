"""Menu mode: operate an instrument through its type's menu tree, without knowing any commands.

Takes the control lock on entry (unless --no-lock), subscribes so readings stay current, and
releases both on exit. Keys: a number picks an entry; b back; r re-read from the instrument;
l take/release the lock; q quit.
"""

from . import ServerError, fmt

CHANNEL = "{channel}"


class Menu:
    def __init__(self, ctx, iid, lock=True):
        self.ctx = ctx
        self.iid = iid
        self.want_lock = lock
        self.have_lock = False
        self.state = {}

    # -- helpers ------------------------------------------------------------
    def say(self, *a):
        self.ctx.say(*a)

    def ask(self, prompt):
        return self.ctx.inp(prompt).strip()

    def pull_events(self):
        for ev in self.ctx.bench.events(lambda e: e.get("instance") == self.iid):
            kind = ev.get("event")
            if kind in ("reading", "state"):
                self.state[ev["key"]] = {"value": ev["value"], "t": ev["t"], "source": ev.get("source", "read")}
            elif kind == "lock":
                self.lock = ev["lock"]
            elif kind in ("lock_broken", "lock_expired"):
                self.have_lock = False
                self.say("!! your lock was %s%s" % ("broken by " + ev["by"] if kind == "lock_broken" else "released (idle)",
                                                    (": " + ev["reason"]) if ev.get("reason") else ""))
            elif kind == "status" and not ev.get("online"):
                self.say("!! instrument not responding: %s" % ev.get("error"))
            elif kind == "restored":
                self.say("!! instrument came back; the server reapplied: %s" % ", ".join(ev.get("keys") or []))

    def default_menu(self):
        m = self.model
        tree = []
        glob = [n for n, d in m["controls"].items() if not d.get("per_channel")] + \
               [n for n, d in m["readouts"].items() if not d.get("per_channel")]
        chan = [n for n, d in m["controls"].items() if d.get("per_channel")] + \
               [n for n, d in m["readouts"].items() if d.get("per_channel")]
        if glob:
            tree.append({"Settings and readings": glob})
        if chan:
            tree.append({CHANNEL: chan})
        if m["actions"]:
            tree.append({"Actions": list(m["actions"])})
        return tree

    def expand(self, entries, channel):
        """Menu entries -> list of (label, kind, payload)."""
        out = []
        for ent in entries:
            if isinstance(ent, dict):
                label, children = next(iter(ent.items()))
                if CHANNEL in label:    # '{channel}' or e.g. 'Marker {channel}': once per channel
                    for ch, cd in self.model["channels"].items():
                        out.append((label.replace(CHANNEL, cd.get("label", ch)), "menu", (children, ch)))
                else:
                    out.append((label, "menu", (children, channel)))
                continue
            for section in ("controls", "readouts", "actions"):
                d = self.model[section].get(ent)
                if d is not None:
                    out.append((d.get("label", ent), section, (ent, d, channel)))
                    break
        return out

    def key(self, name, d, channel):
        if not d.get("per_channel"):
            return name
        if channel is None:
            chans = list(self.model["channels"])
            ans = self.ask("channel (%s): " % ", ".join(chans))
            match = [c for c in chans if c.upper() == ans.upper()]
            if not match:
                self.say("no such channel")
                return None
            channel = match[0]
        return "%s.%s" % (channel, name)

    def show_value(self, name, d, channel):
        if d.get("per_channel") and channel is None:
            return ""
        e = self.state.get(self.key(name, d, channel) if d.get("per_channel") else name) or {}
        return fmt.value(e.get("value"), d, e.get("source"))

    # -- actions on entries ---------------------------------------------------
    def edit(self, name, d, channel):
        key = self.key(name, d, channel)
        if key is None:
            return
        cur = self.state.get(key, {})
        kind = d.get("kind")
        label = d.get("label", name) + (" (%s)" % key.split(".")[0] if "." in key else "")
        self.say("%s: now %s" % (label, fmt.value(cur.get("value"), d, cur.get("source"))))
        if d.get("description"):
            self.say("  " + " ".join(d["description"].split()))
        if kind == "enum":
            opts = list(d.get("options") or {})
            for i, o in enumerate(opts, 1):
                self.say("  %d) %s%s" % (i, o, (" - " + d["options"][o]["label"]) if (d["options"][o] or {}).get("label") else ""))
            ans = self.ask("choice (Enter to keep): ")
            if ans.isdigit() and 1 <= int(ans) <= len(opts):
                ans = opts[int(ans) - 1]
        elif kind == "bool":
            ans = self.ask("on/off (Enter to keep): ")
        else:
            rng = fmt.range_text(self.model, d, key.split(".")[0] if "." in key else None)
            ans = self.ask("new value%s (Enter to keep): " % (" [%s]" % rng if rng else ""))
        if not ans:
            return
        try:
            e = self.dev.set_entry(key, ans)
            self.state[key] = e
            self.say("%s = %s" % (label, fmt.value(e["value"], d, e["source"])))
        except ServerError as exc:
            self.say("not set: %s" % exc.error.get("msg", exc))

    def read(self, name, d, channel):
        key = self.key(name, d, channel)
        if key is None:
            return
        try:
            e = self.dev.get_entry(key)
            self.state[key] = e
            self.say("%s = %s" % (d.get("label", name), fmt.value(e["value"], d, e["source"])))
        except ServerError as exc:
            self.say("read failed: %s" % exc.error.get("msg", exc))

    def act(self, name, d, channel):
        ch = None
        if d.get("per_channel"):
            k = self.key(name, d, channel)
            if k is None:
                return
            ch = k.split(".")[0]
        if d.get("confirm") and self.ask("%s? [y/N] " % d.get("label", name)).lower() not in ("y", "yes"):
            self.say("cancelled")
            return
        try:
            self.dev.action(name, ch)
            self.say("done: %s" % d.get("label", name))
            if d.get("refresh"):
                self.refresh(quiet=True)
        except ServerError as exc:
            self.say("failed: %s" % exc.error.get("msg", exc))

    def refresh(self, entries=None, channel=None, quiet=False):
        try:
            self.state.update(self.dev.refresh())
            for label, kind, payload in self.expand(entries or [], channel):
                if kind == "readouts":
                    name, d, ch = payload
                    if not d.get("per_channel") or ch:
                        self.state[self.key(name, d, ch)] = self.dev.get_entry(self.key(name, d, ch))
            if not quiet:
                self.say("re-read from the instrument")
        except ServerError as exc:
            self.say("refresh failed: %s" % exc.error.get("msg", exc))

    def toggle_lock(self):
        try:
            if self.have_lock:
                self.dev.unlock()
                self.have_lock = False
                self.say("lock released: you are observing")
            else:
                self.take_lock()
        except ServerError as exc:
            self.say("error: %s" % exc.error.get("msg", exc))

    def take_lock(self):
        try:
            self.dev.lock()
            self.have_lock = True
        except ServerError as exc:
            if exc.code != "locked":
                raise
            holder = exc.error.get("details", {}).get("holder", "someone")
            self.say("%s is locked by %s." % (self.iid, holder))
            if self.ask("break their lock? [y/N] ").lower() in ("y", "yes"):
                reason = self.ask("reason (sent to them): ") or "taken over from gpibctl menu"
                self.dev.lock(reason, force=True)
                self.have_lock = True
            else:
                self.say("observing only; changes will be refused while they hold the lock")

    # -- main loop ----------------------------------------------------------------
    def run(self):
        self.dev = self.ctx.dev(self.iid)
        st = self.dev.snapshot()
        self.model = self.ctx.model(st["type"])
        self.lock = st["summary"]["lock"]
        self.state = dict(self.dev.subscribe())
        if self.want_lock:
            self.take_lock()
        tree = self.model.get("menu") or self.default_menu()
        stack = [("Main", tree, None)]
        try:
            while stack:
                title, entries, channel = stack[-1]
                self.pull_events()
                items = self.expand(entries, channel)
                path = " > ".join(t for t, _, _ in stack)
                self.say("")
                self.say("== %s: %s ==  [%s]" % (self.iid, path,
                         "you have the lock" if self.have_lock else "observing; " + fmt.lock_text(self.lock)))
                for i, (label, kind, payload) in enumerate(items, 1):
                    if kind == "menu":
                        self.say("  %2d) %s >" % (i, label))
                    else:
                        name, d, ch = payload
                        val = self.show_value(name, d, ch) if kind != "actions" else ("(asks first)" if d.get("confirm") else "")
                        self.say("  %2d) %-28s %s" % (i, label, val))
                self.say("   b) back  r) re-read  l) %s  q) quit" % ("release lock" if self.have_lock else "take lock"))
                try:
                    ans = self.ask("> ").lower()
                except EOFError:
                    break
                self.pull_events()
                if ans in ("q", "quit", "exit"):
                    break
                if ans == "b":
                    if len(stack) > 1:
                        stack.pop()
                    continue
                if ans == "r":
                    self.refresh(entries, channel)
                    continue
                if ans == "l":
                    self.toggle_lock()
                    continue
                if not ans.isdigit() or not 1 <= int(ans) <= len(items):
                    if ans:
                        self.say("pick 1-%d, b, r, l or q" % len(items))
                    continue
                label, kind, payload = items[int(ans) - 1]
                if kind == "menu":
                    children, ch = payload
                    stack.append((label, children, ch))
                elif kind == "controls":
                    self.edit(*payload)
                elif kind == "readouts":
                    self.read(*payload)
                else:
                    self.act(*payload)
        except KeyboardInterrupt:
            self.say("")
        finally:
            for release in ((self.dev.unlock,) if self.have_lock else ()) + (self.dev.unsubscribe,):
                try:
                    release()
                except Exception:
                    pass
        return 0
