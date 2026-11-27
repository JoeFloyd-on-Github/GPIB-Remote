"""Instance runtime: live state, polling, limits and control locks for each device instance.

Locks: any number of observers, at most one controller. Changing operations (set, action, raw
I/O to the instance's address) are refused while another session holds the lock; when nobody
holds it they are allowed (the CLI takes the lock for longer work). Locks expire after an idle
period and are released when the holder disconnects. Anyone may break a lock with a reason;
that is logged and the holder is told.
"""

import asyncio
import logging
import re
import time

from .devicetypes import ValueRejected, parse_seconds

log = logging.getLogger(__name__)
audit = logging.getLogger("gpibremote.audit")


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


class InstanceRuntime:
    def __init__(self, raw, dtype, bus, hub):
        self.hub = hub
        self.bus = bus
        self.poll_task = None
        self.lock = None
        self.cache = {}            # key -> {"value", "t", "source"}
        self.online = None         # None = not yet contacted
        self.last_error = None
        self._last_error_event = 0.0
        self._ready = False
        self._next_poll = {}
        self.update(raw, dtype)

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

    async def _transact(self, steps):
        """steps: list of (kind, command string or None); returns decoded responses."""
        await self._ensure_ready()
        raw = [(k, None if c is None else self._enc(c)) for k, c in steps]
        try:
            out = await self.bus.call("transaction", self.pad, self.sad, raw, self.t.timeout, self.t.delay)
        except Exception as exc:
            self._set_online(False, str(exc))
            raise
        self._set_online(True)
        return [b.decode("latin-1").strip() for b in out]

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

    # -- operations -----------------------------------------------------------
    async def get(self, key, fresh=True):
        section, name, ch, d = self.t.item(key)
        template = d.get("get") if section == "controls" else d.get("query")
        if section == "readouts" and not template:      # talk-only readout
            steps = ([("t", None)] if d.get("trigger") == "get" else []) + [("r", None)]
            resp = (await self._transact(steps))[-1]
            return self._publish_reading(key, self.t.parse(d, resp))
        if not template or not fresh:
            entry = self.cache.get(key)
            if entry is None:
                return {"value": None, "t": None, "source": "unknown"}
            return entry
        steps, cmd = self._item_steps(template, d, ch)
        if section == "readouts" and d.get("trigger") == "get":
            steps += [("t", None), ("w", cmd), ("r", None)]
        else:
            steps.append(("q", cmd))
        resp = (await self._transact(steps))[-1]
        try:
            value = self.t.parse(d, resp)
        except ValueError as exc:
            raise DeviceError("bad_response", str(exc))
        if section == "readouts":
            return self._publish_reading(key, value)
        return self._publish_state(key, value, "read")

    def _publish_reading(self, key, value):
        entry = self._store(key, value, "read")
        self.hub.broadcast({"event": "reading", "instance": self.id, "key": key,
                            "value": value, "t": entry["t"]}, instance=self.id)
        return entry

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
        steps, cmd = self._item_steps(d["set"], d, ch, self.t.format_value(d, value))
        steps.append(("w", cmd))
        if d.get("get"):
            gsteps, gcmd = self._item_steps(d["get"], d, ch)
            steps.append(("q", gcmd))
        if self.t.check_errors and self.t.error_query:
            steps.append(("q", self.t.error_query))
        audit.info("%s set %s %s = %r", session, self.id, key, value)
        resp = await self._transact(steps)
        if self.t.check_errors and self.t.error_query:
            self._check_error(resp.pop())
        if d.get("get"):
            try:
                value = self.t.parse(d, resp[-1])
            except ValueError as exc:
                raise DeviceError("bad_response", str(exc))
            return self._publish_state(key, value, "read", by=str(session.who))
        return self._publish_state(key, value, "assumed", by=str(session.who))

    def _check_error(self, resp):
        if self.t.error_none and not re.search(self.t.error_none, resp):
            raise DeviceError("device_error", "instrument reported: %s" % resp)

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
        audit.info("%s action %s %s%s", session, self.id, name, " " + channel if channel else "")
        await self._transact(steps)
        if a.get("refresh"):
            for key in list(self.cache):
                if self.cache[key]["source"] == "assumed":
                    del self.cache[key]
            await self.refresh()
        return {}

    async def refresh(self):
        """Read back every control that has a query."""
        out = {}
        for key in self.t.keys("controls"):
            d = self.t.item(key)[3]
            if d.get("get"):
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
                if self.poll_mode == "off" or (self.poll_mode == "subscribed" and not self.subscribers):
                    continue
                now = time.monotonic()
                for key in self.t.keys("readouts"):
                    name = self.t.split_key(key)[1]
                    interval = self.poll_interval(name, self.t.readouts[name])
                    if interval is None or self._next_poll.get(key, 0) > now:
                        continue
                    self._next_poll[key] = now + interval
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
        rt = InstanceRuntime(raw, self.registry.get_type(raw["type"]), bus, self.hub)
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
