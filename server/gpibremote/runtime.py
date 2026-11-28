"""Instance runtime: live state, polling, limits and control locks for each device instance.

Locks: any number of observers, at most one controller. Changing operations (set, action, raw
I/O to the instance's address) are refused while another session holds the lock; when nobody
holds it they are allowed (the CLI takes the lock for longer work). Locks expire after an idle
period and are released when the holder disconnects. Anyone may break a lock with a reason;
that is logged and the holder is told.
"""

import asyncio
import json
import logging
import os
import re
import time

from .devicetypes import ValueRejected, parse_seconds

log = logging.getLogger(__name__)
audit = logging.getLogger("gpibremote.audit")

OVERLOAD = "OVLD"     # published in place of a reading at or beyond a readout's `overload` value


def _same_number(a, b):
    """True when a and b are the same number (an enum option '10' and a value 10.0)."""
    try:
        x, y = float(a), float(b)
    except (TypeError, ValueError):
        return False
    return abs(x - y) <= 1e-9 * max(abs(x), abs(y))


class DeviceError(Exception):
    """An operation reached the instrument but failed there (error queue, bad response)."""

    def __init__(self, code, msg, details=None):
        super().__init__(msg)
        self.code = code
        self.details = details


class Hub:
    """Connected sessions and event fan-out."""

    def __init__(self):
        self.sessions = set()

    def broadcast(self, event, instance=None):
        """Send to every session, or only to subscribers of `instance` when given."""
        for s in list(self.sessions):
            if instance is None or instance in s.subscriptions:
                s.send(event)


class Lock:
    def __init__(self, session):
        self.session = session
        self.since = self.last = time.time()

    def info(self):
        return {"holder": str(self.session.who), "session": self.session.id,
                "since": self.since, "idle": round(time.time() - self.last, 1)}


PRESENCE_EVERY = 5.0      # seconds between presence checks of an instrument whose settings are restored
PRESENCE_TIMEOUT = 1.0    # serial-poll timeout for that check


class InstanceRuntime:
    def __init__(self, raw, dtype, bus, hub, state_dir=None):
        self.hub = hub
        self.bus = bus
        self.state_dir = state_dir
        self._last_ok = 0.0        # monotonic time of the last successful bus operation
        self._last_presence = 0.0
        self._restoring = False
        self.poll_task = None
        self.lock = None
        self.cache = {}            # key -> {"value", "t", "source"}
        self._order = {}           # control key -> count of the set that last changed it
        self.online = None         # None = not yet contacted
        self.last_error = None
        self._last_error_event = 0.0
        self._ready = False
        self._next_poll = {}
        self.saved = {}            # key -> value: settings to restore (types with restore: true)
        self.update(raw, dtype)
        self.saved = self._load_saved()

    def update(self, raw, dtype):
        self.raw = raw
        self.id = raw["id"]
        self.t = dtype
        a = raw["address"]
        self.board, self.pad, self.sad = a.get("board", 0), a["pad"], a.get("sad", 0)
        ov = raw.get("overrides") or {}
        self.limits = dict(ov.get("limits") or {})
        self.poll_overrides = dict(ov.get("poll") or {})
        self.poll_mode = raw.get("poll_mode", "subscribed")
        self.safe_state = raw.get("safe_state") or {}
        self._ready = False
        self._next_poll = {}
        self.cache = {k: v for k, v in self.cache.items() if self._known(k)}
        self.saved = {k: v for k, v in self.saved.items() if self._known(k)}

    # -- remembered settings (restored when the instrument appears) -----------------
    @property
    def restore_enabled(self):
        return self.t.restore and self.raw.get("restore", True) is not False

    def _saved_path(self):
        return os.path.join(self.state_dir, "%s.json" % self.id) if self.state_dir else None

    def _load_saved(self):
        path = self._saved_path()
        if not path or not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return {k: v for k, v in (data.get("settings") or {}).items() if self._known(k)}
        except (OSError, ValueError) as exc:
            log.warning("%s: can't read saved settings %s: %s", self.id, path, exc)
            return {}

    def _write_saved(self):
        path = self._saved_path()
        if not path:
            return
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"instance": self.id, "type": self.t.ref, "settings": self.saved}, f, indent=1)
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("%s: can't save settings: %s", self.id, exc)

    def _remember(self, key, d, value):
        if self.restore_enabled and d.get("restore", True) is not False:
            if self.saved.get(key, object()) != value:
                self.saved[key] = value
                self._write_saved()

    def forget_saved(self):
        if self.saved:
            self.saved = {}
            self._write_saved()

    async def _restore(self):
        """Reapply the remembered settings, in the type's control order (function before range)."""
        if self._restoring or not self.saved:
            return
        self._restoring = True
        applied = []
        try:
            for key in self.t.keys("controls"):
                if key not in self.saved:
                    continue
                section, name, ch, d = self.t.item(key)
                if d.get("restore", True) is False:
                    continue
                await self._apply(key, d, ch, self.t.coerce(d, self.saved[key], ch), "server: restore")
                self._mark_set(key)
                applied.append(key)
            audit.info("restored %s: %s", self.id, ", ".join("%s=%r" % (k, self.saved[k]) for k in applied))
            self.hub.broadcast({"event": "restored", "instance": self.id, "keys": applied}, instance=self.id)
        except Exception as exc:
            log.warning("%s: restoring settings stopped after %s: %s", self.id, applied, exc)
        finally:
            self._restoring = False

    def _known(self, key):
        try:
            self.t.item(key)
            return True
        except KeyError:
            return False

    @property
    def subscribers(self):
        return [s for s in self.hub.sessions if self.id in s.subscriptions]

    def summary(self):
        return {"id": self.id, "name": self.raw.get("name", self.id), "type": self.t.ref,
                "title": self.t.title, "address": {"board": self.board, "pad": self.pad, "sad": self.sad},
                "revision": self.raw.get("revision"), "online": self.online,
                "lock": self.lock.info() if self.lock else None, "poll_mode": self.poll_mode}

    # -- locks --------------------------------------------------------------
    def check_control(self, session):
        if self.lock and self.lock.session is not session:
            raise DeviceError("locked", "%s is locked by %s" % (self.id, self.lock.session.who),
                              self.lock.info())
        if self.lock:
            self.lock.last = time.time()

    def take_lock(self, session, force=False, reason=""):
        if self.lock and self.lock.session is not session:
            if not force:
                raise DeviceError("locked", "%s is locked by %s" % (self.id, self.lock.session.who),
                                  self.lock.info())
            old = self.lock.session
            audit.info("%s broke lock on %s held by %s: %s", session, self.id, old, reason)
            old.send({"event": "lock_broken", "instance": self.id, "by": str(session.who), "reason": reason})
        if not self.lock or self.lock.session is not session:
            self.lock = Lock(session)
            audit.info("%s locked %s", session, self.id)
            self._lock_event()
        self.lock.last = time.time()
        return self.lock.info()

    def release_lock(self, session, why="released"):
        if self.lock and self.lock.session is session:
            self.lock = None
            audit.info("%s %s lock on %s", session, why, self.id)
            self._lock_event(why)
            return True
        return False

    def _lock_event(self, why=None):
        ev = {"event": "lock", "instance": self.id, "lock": self.lock.info() if self.lock else None}
        if why:
            ev["reason"] = why
        self.hub.broadcast(ev)

    # -- bus access ---------------------------------------------------------
    def _enc(self, cmd):
        return (cmd + (self.t.write_termination or "")).encode("latin-1")

    async def _transact(self, steps, timeout=None, ready=True):
        """steps: list of (kind, command string or None); returns decoded responses."""
        if ready:
            await self._ensure_ready()
        raw = [(k, c if k == "e" else None if c is None else self._enc(c)) for k, c in steps]
        if timeout is None:     # a step that waits for the instrument to finish gets the longer timeout
            timeout = max(self.t.timeout, self.t.complete_timeout) if ("q", self.t.complete_query) in steps \
                else self.t.timeout
        try:
            out = await self.bus.call("transaction", self.pad, self.sad, raw, timeout, self.t.delay)
        except Exception as exc:
            self._set_online(False, str(exc))
            raise
        self._last_ok = time.monotonic()
        self._set_online(True)
        kinds = [k for k, _ in steps if k in ("q", "r", "e", "b")]
        return [b if k == "b" else b.decode("latin-1").strip() for k, b in zip(kinds, out)]

    def _wait_step(self):
        return ("q", self.t.complete_query)

    async def check_presence(self):
        """Serial poll: answers when the instrument is on, without putting it in remote."""
        self._last_presence = time.monotonic()
        await self._transact([("p", None)], timeout=PRESENCE_TIMEOUT, ready=False)

    async def _ensure_ready(self):
        if self._ready:
            return
        eos = None if self.t.eos is None else self.t.eos.encode("latin-1")[-1:]
        await self.bus.call("configure", self.pad, self.sad, eos)
        self._ready = True
        if self.t.init:
            try:
                await self._transact([("w", c) for c in self.t.init])
            except Exception:
                self._ready = False
                raise

    def _set_online(self, online, error=None):
        changed = online != self.online
        if online and changed:
            self._ready = False         # it may have been switched off: send init again
            if self.restore_enabled and self.saved and not self._restoring:
                asyncio.ensure_future(self._restore())
        self.online = online
        self.last_error = error
        now = time.time()
        if changed or (error and now - self._last_error_event > 30):
            self._last_error_event = now if error else self._last_error_event
            self.hub.broadcast({"event": "status", "instance": self.id, "online": online, "error": error})

    def _item_steps(self, template, d, channel, value=None):
        steps = []
        if channel is not None and "{channel" not in template:
            steps.append(("w", self.t.select_cmd(channel)))
        return steps, self.t.render(template, channel, value)

    def _store(self, key, value, source):
        entry = {"value": value, "t": time.time(), "source": source}
        self.cache[key] = entry
        return entry

    def _mark_set(self, key):
        """Note that a control was just set (by a client or an action), so which of two settings
        was changed last is known at any clock resolution. Reading a value back doesn't count."""
        self._seq = getattr(self, "_seq", 0) + 1
        self._order[key] = self._seq

    # -- operations -----------------------------------------------------------
    async def get(self, key, fresh=True):
        section, name, ch, d = self.t.item(key)
        template = d.get("get") if section == "controls" else d.get("query")
        if section == "readouts" and (d.get("decode") or d.get("accuracy")):
            # Decoded from another readout (status bits, an uncertainty): read that, which
            # publishes this one.
            if fresh:
                src = (d.get("decode") or d.get("accuracy"))["readout"]
                await self.get("%s.%s" % (ch, src) if ch else src)
            return self.cache.get(key) or {"value": None, "t": None, "source": "unknown"}
        if section == "readouts" and fresh and (d.get("unless") or d.get("only")):
            why = self._unavailable(key)
            if why:
                raise ValueRejected(why)
        if section == "controls" and (d.get("infer") or d.get("readback")) and fresh:
            # Can't be asked; read the status readout it is decoded from (which publishes it).
            await self.get(self._infer_source(name, ch))
            return self.cache.get(key) or {"value": None, "t": None, "source": "unknown"}
        if section == "readouts" and not template:      # talk-only readout
            steps = ([("t", None)] if d.get("trigger") == "get" else []) + [("r", None)]
            resp = (await self._transact(steps))[-1]
            try:
                return self._publish_reading(key, self.t.parse(d, resp))
            except ValueError as exc:
                raise DeviceError("bad_response", str(exc))
        if not template or not fresh:
            entry = self.cache.get(key)
            if entry is None:
                return {"value": None, "t": None, "source": "unknown"}
            return entry
        steps, cmd = self._item_steps(template, d, ch)
        if section == "readouts" and d.get("trigger") == "get":
            steps += [("t", None), ("w", cmd), ("r", None)]
        elif section == "readouts" and d.get("block"):      # binary trace
            steps.append(("b", cmd))
        else:
            steps.append(("q", cmd))
        # A second query whose number is added to the reading (the 3457A's extra digit, HIRES).
        add = d.get("add") if section == "readouts" else None
        if add and self._blocked(ch, add):
            add = None
        if add:
            steps.append(("q", self.t.render(add["query"], ch)))
        if section == "readouts" and d.get("clear_srq"):
            steps.append(("p", None))   # the query raises SRQ (e.g. 3455A "B"); a serial poll clears it
        resps = await self._transact(steps)
        resp = resps[-2] if add else resps[-1]
        try:
            value = self.t.parse_block(d, resp) if isinstance(resp, bytes) else self.t.parse(d, resp)
        except ValueError as exc:
            raise DeviceError("bad_response", str(exc))
        if section == "readouts":
            if d.get("overload") and isinstance(value, float) and abs(value) >= 0.99 * float(d["overload"]):
                return self._publish_reading(key, OVERLOAD)
            if add:
                try:
                    # Round away binary noise: 110.2066 + 0.00003 is 110.20663, not ...62999999999.
                    value = float("%.10g" % (value + self.t.parse({"parse": "number"}, resps[-1])))
                except ValueError as exc:
                    log.debug("%s: %s: ignoring the added reading: %s", self.id, key, exc)
            return self._publish_reading(key, value)
        return self._publish_state(key, value, "read")

    def _publish_reading(self, key, value):
        entry = self._store(key, value, "read")
        self.hub.broadcast({"event": "reading", "instance": self.id, "key": key,
                            "value": value, "t": entry["t"]}, instance=self.id)
        ch, rname = self.t.split_key(key)
        for cname in self.t.inferred_by.get(rname, ()):
            try:
                decoded = self.t.status_value(cname, value)
            except ValueRejected:
                decoded = None
            if decoded is not None:
                ckey = "%s.%s" % (ch, cname) if ch else cname
                self._publish_state(ckey, decoded, self.t.status_spec(cname)[1], by="server: from %s" % key)
        for dname in self.t.decoded_by.get(rname, ()):   # readings decoded from this one (status bits)
            dkey = "%s.%s" % (ch, dname) if ch else dname
            self._publish_reading(dkey, self.t.decode_value(dname, value, lambda c: self._setting(c, ch)))
        return entry

    def _setting(self, cname, ch=None):
        """A control's state entry (channel-scoped when it is per channel) with `order`, the count of
        the set that last changed it (0 if it was never set), or None."""
        key = "%s.%s" % (ch, cname) if ch and self.t.item_per_channel(cname) else cname
        e = self.cache.get(key)
        return None if e is None else dict(e, order=self._order.get(key, 0))

    def _infer_source(self, cname, ch):
        rname = self.t.status_spec(cname)[0]["readout"]
        return "%s.%s" % (ch, rname) if ch else rname

    def _publish_state(self, key, value, source, by=None):
        old = self.cache.get(key)
        entry = self._store(key, value, source)
        if old is None or old["value"] != value or old["source"] != source:
            ev = {"event": "state", "instance": self.id, "key": key, "value": value,
                  "source": source, "t": entry["t"]}
            if by:
                ev["by"] = by
            self.hub.broadcast(ev, instance=self.id)
        return entry

    async def set(self, key, value, session):
        section, name, ch, d = self.t.item(key)
        if section != "controls":
            raise ValueRejected("%s is a readout and can't be set" % key)
        self.check_control(session)
        value = self.t.coerce(d, value, ch, self.limits.get(key))
        audit.info("%s set %s %s = %r", session, self.id, key, value)
        entry = await self._apply(key, d, ch, value, str(session.who))
        self._mark_set(key)
        self._remember(key, d, value)
        return entry

    ERROR_DRAIN = 32    # most error-queue entries read in one go

    def _drain_step(self):
        return ("e", (self._enc(self.t.error_query), (self.t.error_none or "^$").encode("latin-1"),
                      self.ERROR_DRAIN))

    async def _apply(self, key, d, ch, value, by):
        check = bool(self.t.check_errors and self.t.error_query)
        steps, cmd = self._item_steps(d["set"], d, ch, self.t.format_value(d, value))
        # Empty the error queue first, so errors left by earlier commands (a poll the instrument
        # refused in its present mode, say) aren't blamed on this set.
        steps = ([self._drain_step()] if check else []) + steps + [("w", cmd)]
        if d.get("wait"):   # an overlapped command: let it finish before reading back
            steps.append(self._wait_step())
        if d.get("get"):
            gsteps, gcmd = self._item_steps(d["get"], d, ch)
            steps.append(("q", gcmd))
        if check:
            steps.append(self._drain_step())
        resp = await self._transact(steps)
        if check:
            stale = resp.pop(0)
            if stale:
                log.info("%s: cleared earlier errors before setting %s: %s", self.id, key, stale.replace("\n", "; "))
            self._check_error(resp.pop())
        entry = None
        if d.get("get"):
            try:
                value = self.t.parse(d, resp[-1])
            except ValueError as exc:
                raise DeviceError("bad_response", str(exc))
            entry = self._publish_state(key, value, "read", by=by)
        elif d.get("readback"):     # read the status it is decoded from, to show what the instrument took
            try:
                await self.get(self._infer_source(self.t.split_key(key)[1], ch))
            except (DeviceError, ValueError) as exc:
                log.debug("%s: read back %s: %s", self.id, key, exc)
            cached = self.cache.get(key)
            if cached is not None and cached["source"] == "read":
                entry = cached
        if entry is None:
            entry = self._publish_state(key, value, "assumed", by=by)
        # Settings the instrument changes along with this one (the 8714ES brings back the format
        # last used for a measurement).
        for other in d.get("rereads") or ():
            okey = "%s.%s" % (ch, other) if ch and self.t.item_per_channel(other) else other
            try:
                await self.get(okey)
            except (DeviceError, ValueRejected, ValueError) as exc:
                log.debug("%s: re-read %s after %s: %s", self.id, okey, key, exc)
        return entry

    def _check_error(self, resp):
        if resp and self.t.error_none and not re.search(self.t.error_none, resp):
            raise DeviceError("device_error", "instrument reported: %s" % "; ".join(
                self.t.error_text(r) for r in resp.split("\n")))

    def _unavailable(self, key):
        """Why a readout can't be read in the present settings (its `unless`), or None."""
        ch, name = self.t.split_key(key)
        why = self._blocked(ch, self.t.readouts.get(name) or {})
        return "%s isn't available while %s" % (key, why) if why else None

    def _blocked(self, ch, d):
        """'control is value' when d's `unless` or `only` rules it out now, else None. A control
        whose value isn't known doesn't rule anything out."""
        for cond in ("unless", "only"):
            for cname, values in (d.get(cond) or {}).items():
                cur = (self._setting(cname, ch) or {}).get("value")
                if cur is None:
                    continue
                listed = any(str(cur).upper() == str(v).upper() or _same_number(cur, v) for v in values)
                if listed == (cond == "unless"):
                    return "%s is %s" % (cname, cur)
        return None

    async def action(self, name, session, channel=None):
        a = self.t.actions.get(name)
        if a is None:
            raise KeyError("unknown action %r" % name)
        if bool(a.get("per_channel")) != (channel is not None):
            raise KeyError("action %r %s a channel" % (name, "needs" if a.get("per_channel") else "takes no"))
        self.check_control(session)
        steps = []
        if channel is not None and not any("{channel" in c for c in a["send"]):
            steps.append(("w", self.t.select_cmd(channel)))
        steps += [("w", self.t.render(c, channel)) for c in a["send"]]
        if a.get("wait"):   # e.g. a single sweep: answer once it has finished
            steps.append(self._wait_step())
        audit.info("%s action %s %s%s", session, self.id, name, " " + channel if channel else "")
        await self._transact(steps)
        if a.get("resets"):     # back to power-on settings: nothing left to restore
            self.forget_saved()
        if a.get("refresh"):
            for key in list(self.cache):
                if self.cache[key]["source"] == "assumed":
                    del self.cache[key]
        for cname, v in (a.get("assumes") or {}).items():   # known afterwards (power-on defaults)
            d = self.t.controls[cname]
            chans = ([channel] if channel else list(self.t.channels)) if d.get("per_channel") else [None]
            for c in chans:
                ckey = "%s.%s" % (c, cname) if c else cname
                self._publish_state(ckey, self.t.coerce(d, v, c), "assumed", by="server: after %s" % name)
                self._mark_set(ckey)
        if a.get("refresh"):
            await self.refresh()
        return {}

    async def refresh(self):
        """Read back every control that has a query, or is inferred from a readout."""
        out = {}
        for key in self.t.keys("controls"):
            d = self.t.item(key)[3]
            if d.get("get") or d.get("infer") or d.get("readback"):
                out[key] = await self.get(key)
        return out

    async def identify(self):
        if not self.t.probe:
            return {"probe": None, "response": None, "match": None}
        resp = (await self._transact([("q", self.t.probe)]))[0]
        ok = bool(self.t.match and re.search(self.t.match, resp))
        return {"probe": self.t.probe, "response": resp, "match": ok}

    async def safe_off(self, why):
        if not self.t.safe_off:
            return False
        audit.info("safe_off %s (%s)", self.id, why)
        await self._transact([("w", c) for c in self.t.safe_off])
        for key in self.t.keys("controls"):
            d = self.t.item(key)[3]
            if any(c == self.t.render(d["set"], value=self.t.format_value(d, False)) for c in self.t.safe_off) \
                    and d.get("kind") == "bool":
                self._publish_state(key, False, "assumed", by="server: " + why)
        return True

    def state(self):
        out = {}
        for section in ("controls", "readouts"):
            for key in self.t.keys(section):
                out[key] = self.cache.get(key, {"value": None, "t": None, "source": "unknown"})
        return out

    # -- polling --------------------------------------------------------------
    def poll_interval(self, name, d):
        v = self.poll_overrides.get(name, d.get("poll"))
        if v in (None, "off"):
            return None
        return parse_seconds(v)

    def start(self):
        if self.poll_task is None:
            self.poll_task = asyncio.ensure_future(self._poll_loop())

    async def stop(self):
        if self.poll_task:
            self.poll_task.cancel()
            try:
                await self.poll_task
            except asyncio.CancelledError:
                pass
            self.poll_task = None

    async def _poll_loop(self):
        while True:
            try:
                await asyncio.sleep(0.2)
                if self.restore_enabled:
                    now = time.monotonic()
                    every = PRESENCE_EVERY if self.online else 2 * PRESENCE_EVERY
                    if now - max(self._last_ok, self._last_presence) > every:
                        await self.check_presence()
                if self.poll_mode == "off" or (self.poll_mode == "subscribed" and not self.subscribers):
                    continue
                now = time.monotonic()
                for key in self.t.keys("readouts"):
                    name = self.t.split_key(key)[1]
                    interval = self.poll_interval(name, self.t.readouts[name])
                    if interval is None or self._next_poll.get(key, 0) > now:
                        continue
                    self._next_poll[key] = now + interval
                    if self._unavailable(key):      # e.g. marker value in Smith chart format
                        continue
                    await self.get(key)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # instrument off, timeout: back off and keep trying
                log.debug("poll %s: %s", self.id, exc)
                await asyncio.sleep(5)


class Manager:
    """All instance runtimes, kept in step with the registry."""

    def __init__(self, registry, buses, hub, lock_idle=900.0):
        self.registry = registry
        self.state_dir = os.path.join(registry.data_dir, "state")
        self.buses = buses
        self.hub = hub
        self.lock_idle = lock_idle
        self.runtimes = {}
        self._expiry = None

    def start(self):
        for raw in self.registry.instances.values():
            self._add(raw)
        self._expiry = asyncio.ensure_future(self._expire_locks())

    def _add(self, raw):
        bus = self.buses.get(raw["address"].get("board", 0))
        if bus is None:
            log.error("instance %s: no board %s", raw["id"], raw["address"].get("board", 0))
            return None
        rt = InstanceRuntime(raw, self.registry.get_type(raw["type"]), bus, self.hub, self.state_dir)
        self.runtimes[rt.id] = rt
        rt.start()
        return rt

    def get(self, iid):
        rt = self.runtimes.get(iid)
        if rt is None:
            raise KeyError("no instance %r" % iid)
        return rt

    def by_address(self, board, pad, sad):
        for rt in self.runtimes.values():
            if (rt.board, rt.pad, rt.sad) == (board, pad, sad):
                return rt
        return None

    def created(self, raw):
        rt = self._add(raw)
        self.hub.broadcast({"event": "instance", "op": "created", "id": raw["id"], "revision": raw["revision"]})
        return rt

    def updated(self, raw):
        rt = self.runtimes[raw["id"]]
        rt.update(raw, self.registry.get_type(raw["type"]))
        self.hub.broadcast({"event": "instance", "op": "updated", "id": raw["id"], "revision": raw["revision"]})

    async def deleted(self, iid):
        rt = self.runtimes.pop(iid, None)
        if rt:
            await rt.stop()
        self.hub.broadcast({"event": "instance", "op": "deleted", "id": iid})

    async def session_closed(self, session):
        for rt in list(self.runtimes.values()):
            if rt.lock and rt.lock.session is session:
                rt.release_lock(session, "disconnected")
                if rt.safe_state.get("on_client_disconnect") == "outputs_off":
                    try:
                        await rt.safe_off("controlling client disconnected")
                    except Exception as exc:
                        log.error("safe_off %s failed: %s", rt.id, exc)

    async def _expire_locks(self):
        while True:
            await asyncio.sleep(5)
            now = time.time()
            for rt in list(self.runtimes.values()):
                if rt.lock and now - rt.lock.last > self.lock_idle:
                    s = rt.lock.session
                    rt.release_lock(s, "expired (idle)")
                    s.send({"event": "lock_expired", "instance": rt.id})

    async def shutdown(self):
        if self._expiry:
            self._expiry.cancel()
        for rt in list(self.runtimes.values()):
            await rt.stop()
            if rt.safe_state.get("on_server_stop") == "outputs_off":
                try:
                    await rt.safe_off("server stopping")
                except Exception as exc:
                    log.error("safe_off %s failed: %s", rt.id, exc)
