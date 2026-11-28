"""Phase 3 tests: the HTTP/WebSocket transport (web client files and /ws) against a simulated bench.

    python -m unittest discover -s tests
"""

import asyncio
import base64
import http.client
import io
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile

from test_phase1 import PSU, ROOT, Stack  # noqa: F401  (sets sys.path)

from gpibremote.protocol import Dispatcher  # noqa: E402
from gpibremote.web import serve_web, ws_accept  # noqa: E402

WEB = os.path.join(ROOT, "web")


class WsClient:
    """Minimal RFC 6455 client: masked text frames, JSON requests, events kept aside."""

    def __init__(self, port, origin=None, user="wsuser"):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        head = ("GET /ws HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n" % (port, key))
        if origin:
            head += "Origin: %s\r\n" % origin
        self.sock.sendall((head + "\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            resp += chunk
        self.status = int(resp.split(b" ", 2)[1])
        self.headers = resp.decode("latin-1")
        self.buf = resp.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in resp else b""
        self.key = key
        self.next_id = 1
        self.events = []
        if self.status == 101 and user:
            self.request("hello", client="test", user=user, host="wshost")

    def send_frame(self, op, payload, fin=True):
        mask = os.urandom(4)
        n = len(payload)
        b1 = (0x80 if fin else 0) | op
        if n < 126:
            head = struct.pack("!BB", b1, 0x80 | n)
        elif n < 1 << 16:
            head = struct.pack("!BBH", b1, 0x80 | 126, n)
        else:
            head = struct.pack("!BBQ", b1, 0x80 | 127, n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(head + mask + masked)

    def _recv(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def read_frame(self):
        b1, b2 = self._recv(2)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack("!H", self._recv(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self._recv(8))[0]
        assert not b2 & 0x80, "server frames must not be masked"
        return b1 & 0x0F, self._recv(n)

    def read_msg(self):
        while True:
            op, data = self.read_frame()
            if op == 0x1:
                return json.loads(data)
            if op == 0x8:
                raise ConnectionError("closed %s" % (struct.unpack("!H", data[:2])[0] if data else None))

    def request(self, op, **params):
        rid = self.next_id
        self.next_id += 1
        self.send_frame(0x1, json.dumps(dict(params, id=rid, op=op)).encode())
        while True:
            msg = self.read_msg()
            if msg.get("id") == rid:
                return msg
            if "event" in msg:
                self.events.append(msg)

    def wait_event(self, pred, timeout=15):
        self.sock.settimeout(timeout)
        try:
            for ev in self.events:
                if pred(ev):
                    return ev
            while True:
                msg = self.read_msg()
                if "event" in msg and pred(msg):
                    return msg
        finally:
            self.sock.settimeout(10)

    def close(self):
        try:
            self.send_frame(0x8, struct.pack("!H", 1000))
        except OSError:
            pass
        self.sock.close()


class Phase3(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gpibremote-test-")
        self.stack = s = Stack(os.path.join(self.tmp, "data"))

        async def start():
            d = Dispatcher({0: s.bus}, s.registry, s.manager, s.hub)
            return await serve_web(d, "127.0.0.1", 0, WEB, s.hub, s.manager, ["http://allowed.example:8000"])
        self.web = asyncio.run_coroutine_threadsafe(start(), s.loop).result(10)
        self.port = self.web.sockets[0].getsockname()[1]
        self.a = s.client("alice")
        self.a.request("instances.create", instance=dict(PSU, address=dict(PSU["address"])))
        self.ws = []

    def tearDown(self):
        for w in self.ws:
            w.close()
        self.a.close()

        async def stop():
            self.web.close()
            await self.web.wait_closed()
        asyncio.run_coroutine_threadsafe(stop(), self.stack.loop).result(10)
        self.stack.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wsc(self, **kw):
        w = WsClient(self.port, **kw)
        self.ws.append(w)
        return w

    def get(self, path, method="GET", headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, headers=headers or {})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r, body

    # ------------------------------------------------------------------------------------------
    def test_static_files(self):
        r, body = self.get("/")
        self.assertEqual(r.status, 200)
        self.assertIn("text/html", r.getheader("Content-Type"))
        self.assertIn(b"js/app.js", body)
        r, body = self.get("/js/app.js")
        self.assertEqual((r.status, r.getheader("Content-Type")), (200, "text/javascript; charset=utf-8"))
        r2, body2 = self.get("/js/app.js", headers={"If-Modified-Since": r.getheader("Last-Modified")})
        self.assertEqual((r2.status, body2), (304, b""))
        r, body = self.get("/js/app.js", method="HEAD")
        self.assertEqual((r.status, body), (200, b""))
        for bad in ("/../README.md", "/%2e%2e/README.md", "/js/../../server/gpibremote/web.py", "/nope.html"):
            self.assertEqual(self.get(bad)[0].status, 404, bad)
        self.assertEqual(self.get("/js")[0].status, 301)
        self.assertEqual(self.get("/", method="DELETE")[0].status, 405)

    def test_cli_download(self):
        r, body = self.get("/download/gpibctl.zip", headers={"Host": "bench.lan:8080"})
        self.assertEqual((r.status, r.getheader("Content-Type")), (200, "application/zip"))
        name = re.search(r'filename="([^"]+)"', r.getheader("Content-Disposition")).group(1)
        self.assertRegex(name, r"^gpibctl-[0-9.]+\.zip$")
        top = ""   # files at the top: Windows' Extract All already makes a folder named after the zip
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            names = set(z.namelist())
            for f in ("cli/gpibctl/__init__.py", "cli/gpibctl/__main__.py", "cli/gpibctl/api.py",
                      "scripts/nickel_plating.py", "docs/api.md", "README.txt", "gpibctl.ini", "gpibctl.cmd"):
                self.assertIn(top + f, names)
            self.assertEqual({n.split("/")[0] for n in names if "/" in n}, {"cli", "scripts", "docs"})
            cmd = z.read("gpibctl.cmd")
            self.assertIn(b"\r\n", cmd)
            self.assertNotIn(b"\n", cmd.replace(b"\r\n", b""))
            self.assertIn(b"cli\\gpibctl", cmd)
            self.assertFalse([n for n in names if "__pycache__" in n or n.endswith(".pyc")])
            ini = z.read(top + "gpibctl.ini").decode()
            self.assertIn("host = bench.lan", ini)
            self.assertIn("port = 9000", ini)
            self.assertIn("--host bench.lan config", z.read(top + "README.txt").decode())
            with open(os.path.join(ROOT, "cli", "gpibctl", "api.py"), "rb") as fh:
                self.assertEqual(z.read(top + "cli/gpibctl/api.py"), fh.read())
            out = os.path.join(self.tmp, "dl")
            z.extractall(out)
        # The unzipped CLI runs on its own.
        p = subprocess.run([sys.executable, os.path.join(out, top, "cli", "gpibctl"), "--help"],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=self.tmp, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout)
        if os.name == "nt":   # the Windows launcher, from another folder
            p = subprocess.run(["cmd", "/c", os.path.join(out, "gpibctl.cmd"), "--help"],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=self.tmp, timeout=60)
            self.assertEqual(p.returncode, 0, p.stdout)
            self.assertIn(b"usage", p.stdout.lower())
        # HEAD, and a Host header that isn't a plain host name.
        r, body = self.get("/download/gpibctl.zip", method="HEAD")
        self.assertEqual((r.status, body), (200, b""))
        r, body = self.get("/download/gpibctl.zip", headers={"Host": "x\"y"})
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            self.assertIn("host = localhost", z.read(top + "gpibctl.ini").decode())

    def test_keepalive(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        for path in ("/", "/css/app.css", "/js/client.js"):
            c.request("GET", path)
            r = c.getresponse()
            r.read()
            self.assertEqual(r.status, 200, path)
        c.close()

    def test_handshake_and_ops(self):
        w = self.wsc()
        self.assertEqual(w.status, 101)
        self.assertIn("Sec-WebSocket-Accept: %s" % ws_accept(w.key), w.headers)
        r = w.request("instances.list")
        self.assertTrue(r["ok"])
        self.assertEqual([i["id"] for i in r["result"]["instances"]], ["bench-psu"])
        info = w.request("server.info")["result"]
        mine = [s for s in info["sessions"] if s["transport"] == "ws"]
        self.assertEqual(mine[0]["who"], "wsuser@wshost")
        r = w.request("dev.set", instance="bench-psu", key="P6V.voltage", value="2.5")
        self.assertAlmostEqual(r["result"]["value"], 2.5)
        r = w.request("dev.set", instance="bench-psu", key="P6V.voltage", value=99)
        self.assertEqual(r["error"]["code"], "rejected")
        w.send_frame(0x1, b"{not json")
        self.assertEqual(w.read_msg()["error"]["code"], "bad_json")

    def test_events_reach_browser(self):
        w = self.wsc()
        st = w.request("subscribe", instance="bench-psu")["result"]["state"]
        self.assertIn("P6V.v_meas", st)
        ev = w.wait_event(lambda e: e.get("event") == "reading" and e["key"].endswith("v_meas"))
        self.assertEqual(ev["instance"], "bench-psu")
        self.a.request("dev.set", instance="bench-psu", key="P25V.voltage", value=12)
        ev = w.wait_event(lambda e: e.get("event") == "state" and e["key"] == "P25V.voltage")
        self.assertAlmostEqual(ev["value"], 12)

    def test_lock_across_transports(self):
        w = self.wsc()
        self.assertTrue(w.request("dev.lock", instance="bench-psu")["ok"])
        with self.assertRaises(Exception) as cm:
            self.a.request("dev.set", instance="bench-psu", key="output", value="off")
        self.assertEqual(cm.exception.code, "locked")
        self.assertIn("wsuser", cm.exception.error["msg"])
        w.close()
        self.ws.remove(w)
        ev = self.a.wait_event(lambda e: e.get("event") == "lock" and e["lock"] is None)
        self.assertEqual(ev["instance"], "bench-psu")
        self.a.request("dev.set", instance="bench-psu", key="output", value="off")

    def test_origin_check(self):
        self.assertEqual(self.wsc(origin="http://evil.example").status, 403)
        self.assertEqual(self.wsc(origin="http://127.0.0.1:%d" % self.port, user=None).status, 101)
        self.assertEqual(self.wsc(origin="http://allowed.example:8000", user=None).status, 101)
        self.assertEqual(self.wsc(user=None).status, 101)      # scripts send no Origin

    def test_framing(self):
        w = self.wsc()
        # fragmented text message, with a ping between the fragments
        msg = json.dumps({"id": 50, "op": "ping"}).encode()
        w.send_frame(0x1, msg[:5], fin=False)
        w.send_frame(0x9, b"hi")
        w.send_frame(0x0, msg[5:])
        op, data = w.read_frame()
        self.assertEqual((op, data), (0xA, b"hi"))
        self.assertEqual(w.read_msg()["id"], 50)
        # 64-bit length frame (> 65535 bytes) both ways
        big = "x" * 70000
        r = w.request("types.validate", yaml="schema: 1\ntype: lab.big\n# " + big + "\n")
        self.assertTrue(r["ok"])
        # binary frames are refused with close code 1003
        w.send_frame(0x2, b"\x00\x01")
        op, data = w.read_frame()
        self.assertEqual((op, struct.unpack("!H", data[:2])[0]), (0x8, 1003))

    def test_unmasked_frame_closes(self):
        w = self.wsc()
        w.sock.sendall(struct.pack("!BB", 0x81, 2) + b"{}")
        op, data = w.read_frame()
        self.assertEqual((op, struct.unpack("!H", data[:2])[0]), (0x8, 1002))


if __name__ == "__main__":
    unittest.main()
