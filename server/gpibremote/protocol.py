"""Request dispatch for the JSON Lines protocol (shared by every transport).

Request:  {"id": 7, "op": "raw.query", "pad": 5, "data": "*IDN?"}
Response: {"id": 7, "ok": true, "result": {...}}
          {"id": 7, "ok": false, "error": {"code": "...", "msg": "..."}}

Phase 0 operations: hello, ping, server.info, bus.listeners, bus.ifc, raw.write, raw.read,
raw.query, bus.clear, bus.local, bus.trigger, bus.serial_poll. No control locks yet (phase 1).
"""

import base64
import logging
import socket
import time

from . import PROTOCOL_VERSION, __version__
from .backends import GpibError, GpibTimeout

audit = logging.getLogger("gpibremote.audit")


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


class Dispatcher:
    def __init__(self, buses):
        self.buses = buses            # board number -> Bus
        self.started = time.time()

    def _bus(self, req):
        board = req.get("board", 0)
        bus = self.buses.get(board)
        if bus is None:
            raise ProtocolError("bad_request", "no board %r" % board)
        return bus

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
            return {"id": rid, "ok": False, "error": {"code": exc.code, "msg": str(exc)}}
        except GpibTimeout as exc:
            return {"id": rid, "ok": False, "error": {"code": "timeout", "msg": str(exc), "gpib": exc.code}}
        except GpibError as exc:
            return {"id": rid, "ok": False, "error": {"code": "gpib", "msg": str(exc), "gpib": exc.code}}
        except Exception as exc:  # never drop the connection on a bug
            logging.getLogger(__name__).exception("op %s failed", op)
            return {"id": rid, "ok": False, "error": {"code": "internal", "msg": repr(exc)}}

    # -- session ----------------------------------------------------------
    async def op_hello(self, session, req):
        session.client = str(req.get("client", ""))[:64]
        session.user = str(req.get("user", ""))[:64]
        session.host = str(req.get("host", ""))[:64]
        return {"server": "gpibremote", "version": __version__, "protocol": PROTOCOL_VERSION,
                "host": socket.gethostname()}

    async def op_ping(self, session, req):
        return {"t": time.time()}

    async def op_server_info(self, session, req):
        return {"version": __version__, "protocol": PROTOCOL_VERSION,
                "uptime": round(time.time() - self.started, 1),
                "boards": [b.backend.info() for b in self.buses.values()]}

    # -- bus ----------------------------------------------------------------
    async def op_bus_listeners(self, session, req):
        return {"pads": await self._bus(req).call("listeners")}

    async def op_bus_ifc(self, session, req):
        audit.info("%s ifc board=%s", session, req.get("board", 0))
        await self._bus(req).call("interface_clear")
        return {}

    async def op_bus_clear(self, session, req):
        pad, sad = _addr(req)
        audit.info("%s clear %d", session, pad)
        await self._bus(req).call("clear", pad, sad)
        return {}

    async def op_bus_local(self, session, req):
        pad, sad = _addr(req)
        await self._bus(req).call("local", pad, sad)
        return {}

    async def op_bus_trigger(self, session, req):
        pad, sad = _addr(req)
        audit.info("%s trigger %d", session, pad)
        await self._bus(req).call("trigger", pad, sad)
        return {}

    async def op_bus_serial_poll(self, session, req):
        pad, sad = _addr(req)
        return {"status": await self._bus(req).call("serial_poll", pad, sad)}

    # -- raw I/O ------------------------------------------------------------
    async def op_raw_write(self, session, req):
        pad, sad = _addr(req)
        data = _encode_in(req)
        audit.info("%s write %d %r", session, pad, data[:200])
        await self._bus(req).call("write", pad, sad, data, _timeout(req))
        return {"bytes": len(data)}

    async def op_raw_read(self, session, req):
        pad, sad = _addr(req)
        max_bytes = _int(req, "max_bytes", 1, 16 << 20, default=1 << 20)
        data = await self._bus(req).call("read", pad, sad, max_bytes, _timeout(req))
        return _encode_out(req, data)

    async def op_raw_query(self, session, req):
        pad, sad = _addr(req)
        data = _encode_in(req)
        max_bytes = _int(req, "max_bytes", 1, 16 << 20, default=1 << 20)
        audit.info("%s query %d %r", session, pad, data[:200])
        out = await self._bus(req).call("query", pad, sad, data, max_bytes, _timeout(req))
        return _encode_out(req, out)
