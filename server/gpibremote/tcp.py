"""Plain TCP transport: newline-delimited JSON, one request, response or event per line. No TLS."""

import asyncio
import itertools
import json
import logging

log = logging.getLogger(__name__)
MAX_LINE = 16 << 20
MAX_BACKLOG = 4 << 20   # drop events for a client that stops reading
_ids = itertools.count(1)


def dumps(obj):
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")


class Session:
    def __init__(self, peer, transport, writer=None):
        self.id = next(_ids)
        self.peer = peer
        self.transport = transport
        self.writer = writer
        self.client = self.user = self.host = ""
        self.subscriptions = set()
        self.closed = False
        self.dropped = 0

    @property
    def who(self):
        who = self.user or "?"
        return who + ("@" + self.host if self.host else "")

    def __str__(self):
        return "[%s#%d %s %s]" % (self.transport, self.id, self.who, self.peer)

    def send(self, obj):
        """Queue an event or response line (never blocks)."""
        if self.closed or self.writer is None:
            return
        t = self.writer.transport
        if obj.get("event") and t is not None and t.get_write_buffer_size() > MAX_BACKLOG:
            self.dropped += 1
            return
        self.writer.write(dumps(obj))


async def serve(dispatcher, host, port, hub=None, manager=None):
    async def on_client(reader, writer):
        peer = writer.get_extra_info("peername")
        session = Session("%s:%s" % peer[:2] if peer else "?", "tcp", writer)
        if hub is not None:
            hub.sessions.add(session)
        log.info("%s connected", session)
        try:
            while True:
                try:
                    line = await reader.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    session.send({"id": None, "ok": False,
                                  "error": {"code": "bad_request", "msg": "line too long"}})
                    break
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                    if not isinstance(req, dict):
                        raise ValueError("request must be a JSON object")
                except ValueError as exc:
                    resp = {"id": None, "ok": False, "error": {"code": "bad_json", "msg": str(exc)}}
                else:
                    resp = await dispatcher.handle(session, req)
                session.send(resp)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            session.closed = True
            if hub is not None:
                hub.sessions.discard(session)
            if manager is not None:
                await manager.session_closed(session)
            log.info("%s disconnected", session)
            writer.close()

    return await asyncio.start_server(on_client, host, port, limit=MAX_LINE, reuse_address=True)
