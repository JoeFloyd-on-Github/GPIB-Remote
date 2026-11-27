"""Plain TCP transport: newline-delimited JSON, one request or response per line. No TLS."""

import asyncio
import itertools
import json
import logging

log = logging.getLogger(__name__)
MAX_LINE = 16 << 20
_ids = itertools.count(1)


class Session:
    def __init__(self, peer, transport):
        self.id = next(_ids)
        self.peer = peer
        self.transport = transport
        self.client = self.user = self.host = ""

    def __str__(self):
        who = self.user or "?"
        if self.host:
            who += "@" + self.host
        return "[%s#%d %s %s]" % (self.transport, self.id, who, self.peer)


def dumps(obj):
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")


async def serve(dispatcher, host, port):
    async def on_client(reader, writer):
        peer = writer.get_extra_info("peername")
        session = Session("%s:%s" % peer[:2] if peer else "?", "tcp")
        log.info("%s connected", session)
        try:
            while True:
                try:
                    line = await reader.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    writer.write(dumps({"id": None, "ok": False,
                                        "error": {"code": "bad_request", "msg": "line too long"}}))
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
                writer.write(dumps(resp))
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            log.info("%s disconnected", session)
            writer.close()

    return await asyncio.start_server(on_client, host, port, limit=MAX_LINE, reuse_address=True)
