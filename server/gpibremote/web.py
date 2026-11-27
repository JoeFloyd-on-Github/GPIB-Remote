"""Plain HTTP and WebSocket transport (port 8080, no TLS; LAN or VPN only).

- GET/HEAD serve the web client's files from `web.root` (index.html for directories).
- `/ws` upgrades to a WebSocket (RFC 6455) carrying exactly the JSON messages of port 9000, one
  per text message, through the same dispatcher, so the browser has the CLI's full control.

Standard library only (the Pi's Python has no ssl and no compiled packages).

Browsers send an Origin header with WebSocket requests. A page from another site must not be able
to drive the bench through a user's browser, so `/ws` is refused unless the Origin's host:port is
this server (the Host header) or listed in `web.allowed_origins`. Clients that send no Origin
(scripts) are allowed.
"""

import asyncio
import base64
import email.utils
import hashlib
import json
import logging
import os
import posixpath
import struct
import time
import urllib.parse

from .tcp import MAX_BACKLOG, MAX_LINE, Session

log = logging.getLogger(__name__)
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_HEAD = 32 << 10
KEEPALIVE = 60.0
PING_EVERY = 30.0
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png",
    ".ico": "image/x-icon", ".txt": "text/plain; charset=utf-8", ".yaml": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
}
REASONS = {200: "OK", 101: "Switching Protocols", 301: "Moved Permanently", 304: "Not Modified",
           400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
           426: "Upgrade Required", 431: "Request Header Fields Too Large", 500: "Internal Server Error"}


class WsClosed(Exception):
    def __init__(self, code=1000, reason=""):
        super().__init__(reason)
        self.code = code
        self.reason = reason


def ws_frame(opcode, payload=b""):
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", 0x80 | opcode, n)
    elif n < 1 << 16:
        head = struct.pack("!BBH", 0x80 | opcode, 126, n)
    else:
        head = struct.pack("!BBQ", 0x80 | opcode, 127, n)
    return head + payload


def ws_accept(key):
    return base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")


def unmask(data, mask):
    n = len(data)
    if not n:
        return b""
    m = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(m, "big")).to_bytes(n, "big")


async def read_frame(reader):
    b1, b2 = await reader.readexactly(2)
    if b1 & 0x70:
        raise WsClosed(1002, "reserved bits set")
    fin, op, n = bool(b1 & 0x80), b1 & 0x0F, b2 & 0x7F
    if n == 126:
        n = struct.unpack("!H", await reader.readexactly(2))[0]
    elif n == 127:
        n = struct.unpack("!Q", await reader.readexactly(8))[0]
    if not b2 & 0x80:
        raise WsClosed(1002, "client frames must be masked")
    if op >= 8 and (n > 125 or not fin):
        raise WsClosed(1002, "bad control frame")
    if n > MAX_LINE:
        raise WsClosed(1009, "message too big")
    mask = await reader.readexactly(4)
    return fin, op, unmask(await reader.readexactly(n), mask)


class WsSession(Session):
    def send(self, obj):
        if self.closed or self.writer is None:
            return
        t = self.writer.transport
        if obj.get("event") and t is not None and t.get_write_buffer_size() > MAX_BACKLOG:
            self.dropped += 1
            return
        self.writer.write(ws_frame(0x1, json.dumps(obj, separators=(",", ":")).encode("utf-8")))


class WebServer:
    def __init__(self, dispatcher, root, hub=None, manager=None, allowed_origins=()):
        self.dispatcher = dispatcher
        self.root = os.path.realpath(root)
        self.hub = hub
        self.manager = manager
        self.allowed = {o.rstrip("/").lower() for o in allowed_origins or ()}

    # -- HTTP -----------------------------------------------------------------------------------
    async def on_client(self, reader, writer):
        peer = writer.get_extra_info("peername")
        peer = "%s:%s" % peer[:2] if peer else "?"
        try:
            while True:
                try:
                    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), KEEPALIVE)
                except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
                    break
                except asyncio.LimitOverrunError:
                    await self.respond(writer, 431, b"request head too large\n", close=True)
                    break
                try:
                    lines = head.decode("latin-1").split("\r\n")
                    method, target, version = lines[0].split(" ", 2)
                    headers = {}
                    for line in lines[1:]:
                        if ":" in line:
                            k, v = line.split(":", 1)
                            headers[k.strip().lower()] = v.strip()
                except ValueError:
                    await self.respond(writer, 400, b"bad request\n", close=True)
                    break
                path = urllib.parse.urlsplit(target).path
                if path == "/ws":
                    await self.websocket(reader, writer, headers, peer)
                    return
                if headers.get("content-length", "0") not in ("", "0") or "transfer-encoding" in headers:
                    await self.respond(writer, 405, b"only GET and HEAD\n", close=True)
                    break
                keep = version == "HTTP/1.1" and headers.get("connection", "").lower() != "close"
                if method not in ("GET", "HEAD"):
                    await self.respond(writer, 405, b"only GET and HEAD\n", close=True, extra={"Allow": "GET, HEAD"})
                    break
                await self.static(writer, path, headers, method == "HEAD", not keep)
                if not keep:
                    break
        except ConnectionError:
            pass
        except Exception:
            log.exception("http %s", peer)
        finally:
            writer.close()

    async def respond(self, writer, status, body=b"", ctype="text/plain; charset=utf-8", close=False,
                      extra=None, head_only=False):
        hdrs = {"Content-Type": ctype, "Content-Length": str(len(body)), "Cache-Control": "no-cache",
                "X-Content-Type-Options": "nosniff", "Connection": "close" if close else "keep-alive"}
        hdrs.update(extra or {})
        out = ["HTTP/1.1 %d %s" % (status, REASONS.get(status, ""))]
        out += ["%s: %s" % kv for kv in hdrs.items()]
        writer.write(("\r\n".join(out) + "\r\n\r\n").encode("latin-1") + (b"" if head_only else body))
        await writer.drain()

    async def static(self, writer, path, headers, head_only, close):
        rel = posixpath.normpath(urllib.parse.unquote(path)).lstrip("/")
        if rel in (".", ""):
            rel = "index.html"
        full = os.path.realpath(os.path.join(self.root, *rel.split("/")))
        if full != self.root and not full.startswith(self.root + os.sep):
            return await self.respond(writer, 404, b"not found\n", close=close, head_only=head_only)
        if os.path.isdir(full):
            if not path.endswith("/"):
                return await self.respond(writer, 301, b"", close=close, extra={"Location": path + "/"})
            full = os.path.join(full, "index.html")
        try:
            st = os.stat(full)
            mtime = email.utils.formatdate(st.st_mtime, usegmt=True)
            if headers.get("if-modified-since") == mtime:
                return await self.respond(writer, 304, b"", close=close, extra={"Last-Modified": mtime})
            with open(full, "rb") as fh:
                body = fh.read()
        except OSError:
            return await self.respond(writer, 404, b"not found\n", close=close, head_only=head_only)
        ctype = CONTENT_TYPES.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
        await self.respond(writer, 200, body, ctype, close=close, head_only=head_only,
                           extra={"Last-Modified": mtime})

    # -- WebSocket ------------------------------------------------------------------------------
    def origin_ok(self, headers):
        origin = headers.get("origin")
        if not origin:
            return True
        o = urllib.parse.urlsplit(origin)
        here = headers.get("host", "").lower()
        return (o.netloc.lower() == here and o.scheme in ("http", "https")) or origin.rstrip("/").lower() in self.allowed

    async def websocket(self, reader, writer, headers, peer):
        key = headers.get("sec-websocket-key", "")
        if "websocket" not in headers.get("upgrade", "").lower() or not key:
            return await self.respond(writer, 426, b"WebSocket only\n", close=True)
        if headers.get("sec-websocket-version") != "13":
            return await self.respond(writer, 426, b"WebSocket version 13 only\n", close=True,
                                      extra={"Sec-WebSocket-Version": "13"})
        if not self.origin_ok(headers):
            log.warning("ws from %s refused: origin %s", peer, headers.get("origin"))
            return await self.respond(writer, 403, b"origin not allowed\n", close=True)
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      "Sec-WebSocket-Accept: %s\r\n\r\n" % ws_accept(key)).encode("latin-1"))
        await writer.drain()

        session = WsSession(peer, "ws", writer)
        if self.hub is not None:
            self.hub.sessions.add(session)
        log.info("%s connected", session)
        pinger = asyncio.ensure_future(self.keepalive(writer))
        code, reason = 1000, ""
        try:
            parts, total = [], 0
            while True:
                fin, op, data = await read_frame(reader)
                if op == 0x8:
                    code = struct.unpack("!H", data[:2])[0] if len(data) >= 2 else 1000
                    break
                if op == 0x9:
                    writer.write(ws_frame(0xA, data))
                    continue
                if op == 0xA:
                    continue
                if op == 0x2 or (op == 0x0 and not parts) or (op == 0x1 and parts) or op not in (0x0, 0x1):
                    raise WsClosed(1003 if op == 0x2 else 1002, "text messages only")
                parts.append(data)
                total += len(data)
                if total > MAX_LINE:
                    raise WsClosed(1009, "message too big")
                if not fin:
                    continue
                msg, parts, total = b"".join(parts), [], 0
                try:
                    req = json.loads(msg.decode("utf-8"))
                    if not isinstance(req, dict):
                        raise ValueError("request must be a JSON object")
                except UnicodeDecodeError:
                    raise WsClosed(1007, "invalid UTF-8")
                except ValueError as exc:
                    resp = {"id": None, "ok": False, "error": {"code": "bad_json", "msg": str(exc)}}
                else:
                    resp = await self.dispatcher.handle(session, req)
                session.send(resp)
                await writer.drain()
        except WsClosed as exc:
            code, reason = exc.code, exc.reason
        except (asyncio.IncompleteReadError, ConnectionError):
            code = None
        finally:
            pinger.cancel()
            session.closed = True
            if self.hub is not None:
                self.hub.sessions.discard(session)
            if self.manager is not None:
                await self.manager.session_closed(session)
            if code is not None:
                try:
                    writer.write(ws_frame(0x8, struct.pack("!H", code) + reason.encode("utf-8")[:120]))
                    await writer.drain()
                except Exception:
                    pass
            writer.close()
            log.info("%s disconnected", session)

    async def keepalive(self, writer):
        """Ping idle browsers so NAT/VPN links don't drop the connection silently."""
        try:
            while True:
                await asyncio.sleep(PING_EVERY)
                writer.write(ws_frame(0x9, str(int(time.time())).encode()))
        except (asyncio.CancelledError, ConnectionError, RuntimeError):
            pass


async def serve_web(dispatcher, host, port, root, hub=None, manager=None, allowed_origins=()):
    web = WebServer(dispatcher, root, hub, manager, allowed_origins)
    return await asyncio.start_server(web.on_client, host, port, limit=MAX_HEAD, reuse_address=True)
