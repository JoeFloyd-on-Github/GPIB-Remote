"""Phase 0 tests against the simulator (stdlib unittest; runs on the PC and the Pi).

    python -m unittest discover -s tests      (from the repo root)
"""

import asyncio
import os
import sys
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "server"), os.path.join(ROOT, "cli")]

from gpibremote.backends import timeout_code                 # noqa: E402
from gpibremote.backends.sim import SimBackend                # noqa: E402
from gpibremote.bus import Bus                                # noqa: E402
from gpibremote.protocol import Dispatcher                    # noqa: E402
from gpibremote.tcp import serve                              # noqa: E402
from gpibctl import Client                                    # noqa: E402


class SimTests(unittest.TestCase):
    def test_e3631a_channels(self):
        b = SimBackend()
        self.assertTrue(b.query(5, 0, b"*IDN?\n").startswith(b"HEWLETT-PACKARD,E3631A"))
        b.write(5, 0, b"INST:SEL P25V;VOLT 12.5\n")
        self.assertEqual(b.query(5, 0, b"VOLT?\n"), b"12.5\n")
        b.write(5, 0, b"INST P6V\n")
        self.assertEqual(b.query(5, 0, b"VOLT?\n"), b"+0.00000000E+00\n")
        self.assertEqual(b.query(5, 0, b"INST:SEL?\n"), b"P6V\n")
        self.assertEqual(b.listeners(), [5])

    def test_timeout_codes(self):
        self.assertEqual(timeout_code(3), 12)
        self.assertEqual(timeout_code(0.5), 11)
        self.assertEqual(timeout_code(0), 0)
        self.assertEqual(timeout_code(5000), 17)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(cls.loop)
            cls.bus = Bus(SimBackend())
            cls.server = cls.loop.run_until_complete(serve(Dispatcher({0: cls.bus}), "127.0.0.1", 0))
            cls.port = cls.server.sockets[0].getsockname()[1]
            ready.set()
            cls.loop.run_forever()

        cls.thread = threading.Thread(target=run, daemon=True)
        cls.thread.start()
        ready.wait(10)

    @classmethod
    def tearDownClass(cls):
        async def shutdown():
            cls.server.close()
            await cls.server.wait_closed()
            others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            await asyncio.wait_for(asyncio.gather(*others, return_exceptions=True), 5)

        asyncio.run_coroutine_threadsafe(shutdown(), cls.loop).result(10)
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.thread.join(10)
        cls.loop.close()
        cls.bus.close()

    def setUp(self):
        self.c = Client("127.0.0.1", self.port)
        self.c.hello("test")

    def tearDown(self):
        self.c.close()

    def test_query_write_read(self):
        self.assertTrue(self.c.request("raw.query", pad=5, data="*IDN?")["data"].startswith("HEWLETT-PACKARD"))
        self.c.request("raw.write", pad=5, data="INST:SEL N25V;VOLT -3")
        self.c.request("raw.write", pad=5, data="VOLT?")
        self.assertEqual(self.c.request("raw.read", pad=5)["data"], "-3")

    def test_base64(self):
        r = self.c.request("raw.query", pad=5, data="KklETj8K", encoding="base64")
        self.assertEqual(r["encoding"], "base64")

    def test_errors(self):
        with self.assertRaisesRegex(RuntimeError, "^gpib:"):
            self.c.request("raw.query", pad=9, data="*IDN?")
        with self.assertRaisesRegex(RuntimeError, "^timeout:"):
            self.c.request("raw.read", pad=5)
        with self.assertRaisesRegex(RuntimeError, "^bad_request:"):
            self.c.request("raw.query", pad=31, data="*IDN?")
        with self.assertRaisesRegex(RuntimeError, "^unknown_op:"):
            self.c.request("no.such.op")

    def test_bus_ops(self):
        self.assertEqual(self.c.request("bus.listeners")["pads"], [5])
        self.assertEqual(self.c.request("bus.serial_poll", pad=5)["status"], 0)
        self.c.request("bus.local", pad=5)
        info = self.c.request("server.info")
        self.assertEqual(info["boards"][0]["backend"], "sim")


if __name__ == "__main__":
    unittest.main()
