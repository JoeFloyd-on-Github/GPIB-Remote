"""Request dispatch for the JSON Lines protocol (shared by every transport). See docs/protocol.md.

Request:  {"id": 7, "op": "dev.set", "instance": "bench-psu", "key": "P6V.voltage", "value": 5}
Response: {"id": 7, "ok": true, "result": {...}}
          {"id": 7, "ok": false, "error": {"code": "...", "msg": "...", ...}}
Events:   {"event": "reading", ...}  (no id)
"""

import asyncio
import base64
import logging
import socket
import time

from . import PROTOCOL_VERSION, __version__
from .backends import GpibError, GpibTimeout
from .devicetypes import TypeError_, ValueRejected
from .registry import RegistryError
from .runtime import DeviceError

audit = logging.getLogger("gpibremote.audit")
log = logging.getLogger(__name__)


class ProtocolError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def _int(req, name, lo, hi, default=None):
    value = req.get(name, default)
    if value is None:
        raise ProtocolError("bad_request", "missing %r" % name)
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise ProtocolError("bad_request", "%r must be an integer %d..%d" % (name, lo, hi))
    return value


def _str(req, name, default=None):
    value = req.get(name, default)
    if not isinstance(value, str) or not value:
        raise ProtocolError("bad_request", "%r must be a non-empty string" % name)
    return value


def _addr(req):
    pad = _int(req, "pad", 0, 30)
    sad = _int(req, "sad", 0, 0x7E, default=0)
    if sad and sad < 0x60:
        raise ProtocolError("bad_request", "'sad' must be 0 (none) or 0x60..0x7e")
    return pad, sad


def _timeout(req):
    t = req.get("timeout", 3.0)
    if not isinstance(t, (int, float)) or isinstance(t, bool) or not 0 < t <= 1000:
        raise ProtocolError("bad_request", "'timeout' must be seconds, 0 < t <= 1000")
    return float(t)


def _encode_in(req):
    data = req.get("data")
    if not isinstance(data, str):
        raise ProtocolError("bad_request", "'data' must be a string")
    if req.get("encoding", "text") == "base64":
        return base64.b64decode(data)
    if not req.get("raw_terminator", False) and not data.endswith("\n"):
        data += "\n"
    return data.encode("latin-1")


def _encode_out(req, data):
    if req.get("encoding", "text") == "base64":
        return {"data": base64.b64encode(data).decode("ascii"), "encoding": "base64", "bytes": len(data)}
    return {"data": data.decode("latin-1").rstrip("\r\n"), "bytes": len(data)}


async def _blocking(fn, *args):
    """Run registry work (file writes, git) off the event loop."""
    return await asyncio.get_running_loop().run_in_executor(None, lambda: fn(*args))


class Dispatcher:
    def __init__(self, buses, registry=None, manager=None, hub=None):
        self.buses = buses            # board number -> Bus
        self.registry = registry
        self.manager = manager
        self.hub = hub
        self.started = time.time()

    def _bus(self, req):
        board = req.get("board", 0)
        bus = self.buses.get(board)
        if bus is None:
            raise ProtocolError("bad_request", "no board %r" % board)
        return bus

    def _need_registry(self):
        if self.registry is None:
            raise ProtocolError("unavailable", "this server runs without a registry")

    def _rt(self, req):
        self._need_registry()
        try:
            return self.manager.get(_str(req, "instance"))
        except KeyError as exc:
            raise ProtocolError("not_found", str(exc).strip("'\""))

    def _guard_address(self, session, req):
        """Raw access to an address that belongs to an instance obeys that instance's lock."""
        if self.manager is None:
            return
        pad, sad = _addr(req)
        rt = self.manager.by_address(req.get("board", 0), pad, sad)
        if rt is not None:
            rt.check_control(session)

    async def handle(self, session, req):
        """Return the response dict for one request dict."""
        rid = req.get("id")
        op = req.get("op")
        try:
            fn = getattr(self, "op_" + str(op).replace(".", "_"), None)
            if fn is None:
                raise ProtocolError("unknown_op", "unknown op %r" % op)
            result = await fn(session, req)
            return {"id": rid, "ok": True, "result": result}
        except ProtocolError as exc:
            err = {"code": exc.code, "msg": str(exc)}
        except DeviceError as exc:
            err = {"code": exc.code, "msg": str(exc)}
            if exc.details:
                err["details"] = exc.details
        except RegistryError as exc:
            err = {"code": exc.code, "msg": str(exc)}
            if exc.errors:
                err["errors"] = exc.errors
        except TypeError_ as exc:
            err = {"code": "invalid", "msg": "invalid device type", "errors": exc.errors}
        except ValueRejected as exc:
            err = {"code": "rejected", "msg": str(exc)}
        except KeyError as exc:
            err = {"code": "bad_request", "msg": str(exc).strip("'\"")}
        except GpibTimeout as exc:
            err = {"code": "timeout", "msg": str(exc), "gpib": exc.code}
        except GpibError as exc:
            err = {"code": "gpib", "msg": str(exc), "gpib": exc.code}
        except Exception as exc:  # never drop the connection on a bug
            log.exception("op %s failed", op)
            err = {"code": "internal", "msg": repr(exc)}
        return {"id": rid, "ok": False, "error": err}

    # -- session ----------------------------------------------------------
    async def op_hello(self, session, req):
        session.client = str(req.get("client", ""))[:64]
        session.user = str(req.get("user", ""))[:64]
        session.host = str(req.get("host", ""))[:64]
        return {"server": "gpibremote", "version": __version__, "protocol": PROTOCOL_VERSION,
                "host": socket.gethostname(), "session": session.id}

    async def op_ping(self, session, req):
        return {"t": time.time()}

    async def op_server_info(self, session, req):
        info = {"version": __version__, "protocol": PROTOCOL_VERSION,
                "uptime": round(time.time() - self.started, 1),
                "boards": [b.backend.info() for b in self.buses.values()]}
        if self.registry is not None:
            info["sessions"] = [{"id": s.id, "who": str(s.who), "peer": s.peer} for s in self.hub.sessions]
            info["data_push"] = self.registry.git.last_push
        return info

    # -- types --------------------------------------------------------------
    async def op_types_list(self, session, req):
        self._need_registry()
        return {"types": self.registry.list_types()}

    async def op_types_get(self, session, req):
        self._need_registry()
        t = self.registry.get_type(_str(req, "type"), req.get("version"))
        fmt = req.get("format", "model")
        out = {"ref": t.ref, "hash": t.hash}
        if fmt in ("model", "both"):
            out["model"] = t.describe()
        if fmt in ("yaml", "both"):
            out["yaml"] = t.text
        return out

    async def op_types_validate(self, session, req):
        self._need_registry()
        from .devicetypes import DeviceType
        try:
            t = DeviceType(_str(req, "yaml"))
        except TypeError_ as exc:
            return {"valid": False, "errors": exc.errors}
        exists = t.version in self.registry.types.get(t.name, {})
        return {"valid": True, "errors": [], "ref": t.ref, "exists": exists}

    async def op_types_put(self, session, req):
        self._need_registry()
        t, added = await _blocking(self.registry.put_type, _str(req, "yaml"), str(session.who), req.get("message"))
        if added:
            audit.info("%s added type %s", session, t.ref)
            self.hub.broadcast({"event": "type", "op": "added", "ref": t.ref})
        return {"ref": t.ref, "hash": t.hash, "added": added}

    async def op_types_history(self, session, req):
        self._need_registry()
        path = "types/%s" % req["type"] if req.get("type") else None
        return {"commits": await _blocking(self.registry.git.history, path, int(req.get("limit", 50)))}

    # -- instances ----------------------------------------------------------
    async def op_instances_list(self, session, req):
        self._need_registry()
        return {"instances": [rt.summary() for rt in self.manager.runtimes.values()]}

    async def op_instances_get(self, session, req):
        rt = self._rt(req)
        return {"instance": rt.raw, "summary": rt.summary()}

    async def op_instances_create(self, session, req):
        self._need_registry()
        raw = req.get("instance")
        if not isinstance(raw, dict):
            raise ProtocolError("bad_request", "'instance' must be an object")
        raw = await _blocking(self.registry.save_instance, raw, str(session.who), True)
        audit.info("%s created instance %s", session, raw["id"])
        rt = self.manager.created(raw)
        out = {"instance": raw}
        if req.get("verify", True) and rt.t.probe:
            try:
                out["identify"] = await rt.identify()
            except GpibError as exc:
                out["identify"] = {"error": str(exc)}
        return out

    async def op_instances_update(self, session, req):
        rt = self._rt(req)
        raw = req.get("changes")
        if not isinstance(raw, dict):
            raise ProtocolError("bad_request", "'changes' must be an object (fields to replace)")
        if "id" in raw and raw["id"] != rt.id:
            raise ProtocolError("bad_request", "an instance can't be renamed; create a new one")
        if req.get("revision") is not None and req["revision"] != rt.raw.get("revision"):
            raise ProtocolError("conflict", "instance changed (now revision %s)" % rt.raw.get("revision"))
        rt.check_control(session)
        merged = dict(rt.raw, **raw)
        merged = await _blocking(self.registry.save_instance, merged, str(session.who), False)
        audit.info("%s updated instance %s", session, rt.id)
        self.manager.updated(merged)
        return {"instance": merged}

    async def op_instances_delete(self, session, req):
        rt = self._rt(req)
        rt.check_control(session)
        await _blocking(self.registry.delete_instance, rt.id, str(session.who))
        audit.info("%s deleted instance %s", session, rt.id)
        await self.manager.deleted(rt.id)
        return {}

    async def op_instances_identify(self, session, req):
        return await self._rt(req).identify()

    async def op_instances_history(self, session, req):
        rt = self._rt(req)
        return {"commits": await _blocking(self.registry.git.history, "instances/%s.yaml" % rt.id,
                                           int(req.get("limit", 50)))}

    # -- device control ------------------------------------------------------
    async def op_dev_state(self, session, req):
        rt = self._rt(req)
        return {"instance": rt.id, "type": rt.t.ref, "state": rt.state(), "summary": rt.summary()}

    async def op_dev_get(self, session, req):
        rt = self._rt(req)
        return await rt.get(_str(req, "key"), fresh=req.get("fresh", True))

    async def op_dev_set(self, session, req):
        rt = self._rt(req)
        if "value" not in req:
            raise ProtocolError("bad_request", "missing 'value'")
        return await rt.set(_str(req, "key"), req["value"], session)

    async def op_dev_action(self, session, req):
        rt = self._rt(req)
        return await rt.action(_str(req, "action"), session, req.get("channel"))

    async def op_dev_refresh(self, session, req):
        rt = self._rt(req)
        await rt.refresh()
        return {"state": rt.state()}

    async def op_dev_lock(self, session, req):
        rt = self._rt(req)
        return {"lock": rt.take_lock(session, bool(req.get("force")), str(req.get("reason", "")))}

    async def op_dev_unlock(self, session, req):
        rt = self._rt(req)
        if not rt.release_lock(session):
            raise ProtocolError("not_locked", "you don't hold the lock on %s" % rt.id)
        return {}

    async def op_dev_locks(self, session, req):
        self._need_registry()
        return {"locks": {rt.id: rt.lock.info() for rt in self.manager.runtimes.values() if rt.lock}}

    async def op_subscribe(self, session, req):
        rt = self._rt(req)
        session.subscriptions.add(rt.id)
        return {"instance": rt.id, "state": rt.state()}

    async def op_unsubscribe(self, session, req):
        session.subscriptions.discard(_str(req, "instance"))
        return {}

    # -- bus ----------------------------------------------------------------
    async def op_bus_listeners(self, session, req):
        return {"pads": await self._bus(req).call("listeners")}

    async def op_bus_ifc(self, session, req):
        if self.manager is not None:
            board = req.get("board", 0)
            for rt in self.manager.runtimes.values():
                if rt.board == board:
                    rt.check_control(session)
        audit.info("%s ifc board=%s", session, req.get("board", 0))
        await self._bus(req).call("interface_clear")
        return {}

    async def op_bus_clear(self, session, req):
        pad, sad = _addr(req)
        self._guard_address(session, req)
        audit.info("%s clear %d", session, pad)
        await self._bus(req).call("clear", pad, sad)
        return {}

    async def op_bus_local(self, session, req):
        pad, sad = _addr(req)
        await self._bus(req).call("local", pad, sad)
        return {}

    async def op_bus_trigger(self, session, req):
        pad, sad = _addr(req)
        self._guard_address(session, req)
        audit.info("%s trigger %d", session, pad)
        await self._bus(req).call("trigger", pad, sad)
        return {}

    async def op_bus_serial_poll(self, session, req):
        pad, sad = _addr(req)
        return {"status": await self._bus(req).call("serial_poll", pad, sad)}

    # -- raw I/O ------------------------------------------------------------
    async def op_raw_write(self, session, req):
        pad, sad = _addr(req)
        self._guard_address(session, req)
        data = _encode_in(req)
        audit.info("%s write %d %r", session, pad, data[:200])
        await self._bus(req).call("write", pad, sad, data, _timeout(req))
        return {"bytes": len(data)}

    async def op_raw_read(self, session, req):
        pad, sad = _addr(req)
        self._guard_address(session, req)
        max_bytes = _int(req, "max_bytes", 1, 16 << 20, default=1 << 20)
        data = await self._bus(req).call("read", pad, sad, max_bytes, _timeout(req))
        return _encode_out(req, data)

    async def op_raw_query(self, session, req):
        pad, sad = _addr(req)
        self._guard_address(session, req)
        data = _encode_in(req)
        max_bytes = _int(req, "max_bytes", 1, 16 << 20, default=1 << 20)
        audit.info("%s query %d %r", session, pad, data[:200])
        out = await self._bus(req).call("query", pad, sad, data, max_bytes, _timeout(req))
        return _encode_out(req, out)
