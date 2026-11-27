"""Phase 1 tests: types, instances, control, locks and events, against a simulated bench.

    python -m unittest discover -s tests      (needs PyYAML and git)
"""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "server"), os.path.join(ROOT, "cli")]

from gpibremote import config                                     # noqa: E402
from gpibremote.__main__ import make_bus                          # noqa: E402
from gpibremote.devicetypes import DeviceType                     # noqa: E402
from gpibremote.protocol import Dispatcher                        # noqa: E402
from gpibremote.registry import Registry                          # noqa: E402
from gpibremote.runtime import Hub, Manager                       # noqa: E402
from gpibremote.tcp import serve                                  # noqa: E402
from gpibctl import Client, ServerError                           # noqa: E402

BUILTIN = os.path.join(ROOT, "devices", "types")
def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


PSU = {"id": "bench-psu", "name": "Bench PSU", "type": "agilent.e3631a@1.0.0", "address": {"pad": 5}}


class Stack:
    """The whole server (registry, sim bus, runtimes, TCP) on its own event-loop thread."""

    def __init__(self, data_dir, lock_idle=900.0):
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(self.loop)

            async def start():
                self.registry = Registry(data_dir, BUILTIN, push_delay=0)
                self.registry.load()
                self.bus = make_bus({"board": 0, "backend": "sim"}, self.registry)
                self.hub = Hub()
                self.manager = Manager(self.registry, {0: self.bus}, self.hub, lock_idle)
                d = Dispatcher({0: self.bus}, self.registry, self.manager, self.hub)
                self.server = await serve(d, "127.0.0.1", 0, self.hub, self.manager)
                self.manager.start()
                self.port = self.server.sockets[0].getsockname()[1]
            self.loop.run_until_complete(start())
            ready.set()
            self.loop.run_forever()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        if not ready.wait(20):
            raise RuntimeError("server did not start")

    def sim(self, pad):
        return self.bus.backend.instruments[pad]

    def client(self, user="alice"):
        c = Client("127.0.0.1", self.port)
        c.hello("test", user=user, host="testhost")
        return c

    def stop(self):
        async def shutdown():
            self.server.close()
            await self.server.wait_closed()
            await self.manager.shutdown()
            others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for t in others:
                t.cancel()
            await asyncio.gather(*others, return_exceptions=True)
        asyncio.run_coroutine_threadsafe(shutdown(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)
        self.loop.close()
        self.bus.close()


class Phase1(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gpibremote-test-")
        self.stack = Stack(os.path.join(self.tmp, "data"))
        self.a = self.stack.client("alice")

    def tearDown(self):
        self.a.close()
        self.stack.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def create_psu(self, **extra):
        inst = dict(PSU, address=dict(PSU["address"]), **extra)
        return self.a.request("instances.create", instance=inst)

    def assertCode(self, code, fn, *args, **kw):
        with self.assertRaises(ServerError) as cm:
            fn(*args, **kw)
        self.assertEqual(cm.exception.code, code, str(cm.exception))
        return cm.exception

    # ------------------------------------------------------------------------------------------
    def test_types(self):
        names = {t["type"] for t in self.a.request("types.list")["types"]}
        self.assertTrue({"agilent.e3631a", "hp.6632a", "hp.3457a", "generic.scpi", "generic.raw"} <= names)
        m = self.a.request("types.get", type="agilent.e3631a")["model"]
        self.assertEqual(sorted(m["channels"]), ["N25V", "P25V", "P6V"])
        self.assertNotIn("send", m["actions"]["reset"])

    def test_type_versions_are_immutable(self):
        text = _read(os.path.join(BUILTIN, "generic.scpi", "1.0.0.yaml"))
        new = text.replace("type: generic.scpi", "type: lab.my-meter").replace("version: '1.0.0'", "version: '1.0.0'")
        r = self.a.request("types.put", yaml=new, message="my meter")
        self.assertEqual((r["ref"], r["added"]), ("lab.my-meter@1.0.0", True))
        self.assertFalse(self.a.request("types.put", yaml=new)["added"])         # identical: no-op
        self.assertCode("conflict", self.a.request, "types.put", yaml=new.replace("Generic SCPI", "Changed"))
        bumped = new.replace("version: '1.0.0'", "version: '1.1.0'")
        self.assertTrue(self.a.request("types.put", yaml=bumped)["added"])
        self.assertEqual(self.a.request("types.get", type="lab.my-meter")["ref"], "lab.my-meter@1.1.0")
        hist = self.a.request("types.history", type="lab.my-meter")["commits"]
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist[-1]["author"], "alice@testhost")
        bad = self.a.request("types.validate", yaml="schema: 1\ntype: Bad Name\n")
        self.assertFalse(bad["valid"])
        self.assertCode("invalid", self.a.request, "types.put", yaml="schema: 1\ntype: x\n")

    def test_instance_lifecycle(self):
        r = self.create_psu()
        self.assertTrue(r["identify"]["match"])
        self.assertEqual(r["instance"]["revision"], 1)
        self.assertCode("conflict", self.a.request, "instances.create",
                        instance=dict(PSU, id="other", address={"pad": 5}))
        e = self.assertCode("invalid", self.a.request, "instances.create",
                            instance={"id": "Bad Id", "type": "nope@1", "address": {"pad": 40}})
        self.assertTrue(any("address.pad" in x for x in e.error["errors"]))
        r = self.a.request("instances.update", instance="bench-psu",
                           changes={"overrides": {"limits": {"P6V.voltage": [0, 5.5]}}})
        self.assertEqual(r["instance"]["revision"], 2)
        path = os.path.join(self.tmp, "data", "instances", "bench-psu.yaml")
        self.assertIn("P6V.voltage", _read(path))
        log = subprocess.run(["git", "log", "--format=%an|%s"], cwd=os.path.join(self.tmp, "data"),
                             stdout=subprocess.PIPE, universal_newlines=True).stdout
        self.assertIn("alice@testhost|Update instance bench-psu (rev 2)", log)
        # reloading the registry from disk gives the same instance
        reg = Registry(os.path.join(self.tmp, "data"), BUILTIN, git_enabled=False)
        reg.load()
        self.assertEqual(reg.instances["bench-psu"]["revision"], 2)
        self.a.request("instances.delete", instance="bench-psu")
        self.assertEqual(self.a.request("instances.list")["instances"], [])

    def test_control_and_limits(self):
        self.create_psu(overrides={"limits": {"P6V.voltage": [0, 5.5]}})
        r = self.a.request("dev.set", instance="bench-psu", key="P6V.voltage", value=5)
        self.assertEqual((r["value"], r["source"]), (5.0, "read"))
        self.assertIn("VOLT 5", self.stack.sim(5).log)
        self.assertCode("rejected", self.a.request, "dev.set", instance="bench-psu", key="P6V.voltage", value=5.8)
        self.assertCode("rejected", self.a.request, "dev.set", instance="bench-psu", key="P25V.voltage", value=30)
        r = self.a.request("dev.set", instance="bench-psu", key="N25V.voltage", value=-12)
        self.assertEqual(r["value"], -12.0)
        self.assertCode("bad_request", self.a.request, "dev.set", instance="bench-psu", key="voltage", value=1)
        self.assertCode("bad_request", self.a.request, "dev.set", instance="bench-psu", key="X.voltage", value=1)
        r = self.a.request("dev.get", instance="bench-psu", key="P6V.voltage")
        self.assertEqual(r["value"], 5.0)
        st = self.a.request("dev.state", instance="bench-psu")["state"]
        self.assertEqual(st["N25V.voltage"]["value"], -12.0)
        self.assertEqual(st["P6V.v_meas"]["source"], "unknown")
        self.a.request("dev.action", instance="bench-psu", action="clear_status")
        self.assertIn("*CLS", self.stack.sim(5).log)

    def test_subscribe_events(self):
        self.create_psu()
        b = self.stack.client("bob")
        try:
            b.request("subscribe", instance="bench-psu")
            self.a.request("dev.set", instance="bench-psu", key="P6V.voltage", value=3.3)
            ev = b.wait_event(lambda e: e.get("event") == "state" and e["key"] == "P6V.voltage")
            self.assertEqual((ev["value"], ev["by"]), (3.3, "alice@testhost"))
            self.a.request("dev.set", instance="bench-psu", key="output", value="on")
            ev = b.wait_event(lambda e: e.get("event") == "reading" and e["key"] == "P6V.v_meas"
                              and e["value"] > 3, timeout=15)
            self.assertAlmostEqual(ev["value"], 3.3)
        finally:
            b.close()

    def test_locks(self):
        self.create_psu()
        b = self.stack.client("bob")
        try:
            self.a.request("dev.lock", instance="bench-psu")
            ev = b.wait_event(lambda e: e.get("event") == "lock")
            self.assertEqual(ev["lock"]["holder"], "alice@testhost")
            e = self.assertCode("locked", b.request, "dev.set", instance="bench-psu", key="output", value=True)
            self.assertEqual(e.error["details"]["holder"], "alice@testhost")
            self.assertCode("locked", b.request, "raw.query", pad=5, data="*IDN?")
            self.assertCode("locked", b.request, "dev.action", instance="bench-psu", action="reset")
            self.assertCode("locked", b.request, "instances.delete", instance="bench-psu")
            b.request("dev.get", instance="bench-psu", key="output")      # observers can still read
            self.a.request("dev.set", instance="bench-psu", key="output", value=True)
            b.request("dev.lock", instance="bench-psu", force=True, reason="need the supply")
            ev = self.a.wait_event(lambda e: e.get("event") == "lock_broken")
            self.assertEqual((ev["by"], ev["reason"]), ("bob@testhost", "need the supply"))
            self.assertCode("locked", self.a.request, "dev.set", instance="bench-psu", key="output", value=False)
        finally:
            b.close()
        ev = self.a.wait_event(lambda e: e.get("event") == "lock" and e["lock"] is None)
        self.assertEqual(ev["reason"], "disconnected")
        self.a.request("dev.set", instance="bench-psu", key="output", value=False)

    def test_lock_expiry(self):
        self.a.close()
        self.stack.stop()
        self.stack = Stack(os.path.join(self.tmp, "data2"), lock_idle=1)
        self.a = self.stack.client("alice")
        self.create_psu()
        self.a.request("dev.lock", instance="bench-psu")
        self.a.wait_event(lambda e: e.get("event") == "lock_expired", timeout=15)

    def test_safe_state_on_disconnect(self):
        self.create_psu(safe_state={"on_client_disconnect": "outputs_off"})
        b = self.stack.client("bob")
        b.request("dev.lock", instance="bench-psu")
        b.request("dev.set", instance="bench-psu", key="output", value=True)
        self.assertTrue(self.stack.sim(5).state[("output", None)])
        b.close()
        deadline = time.time() + 5
        while self.stack.sim(5).state[("output", None)] and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(self.stack.sim(5).state[("output", None)])

    def test_pre_scpi_types(self):
        self.a.request("instances.create", instance={"id": "psu2", "type": "hp.6632a@1.0.0", "address": {"pad": 6}})
        r = self.a.request("dev.set", instance="psu2", key="voltage", value=12.5)
        self.assertEqual((r["value"], r["source"]), (12.5, "read"))
        self.assertIn("VSET 12.5", self.stack.sim(6).log)
        self.a.request("dev.set", instance="psu2", key="output", value=True)
        self.assertIn("OUT 1", self.stack.sim(6).log)
        self.assertAlmostEqual(self.a.request("dev.get", instance="psu2", key="v_meas")["value"], 12.5)
        r = self.a.request("instances.create", instance={"id": "dmm", "type": "hp.3457a@1.0.0", "address": {"pad": 22}})
        self.assertTrue(r["identify"]["match"])
        r = self.a.request("dev.set", instance="dmm", key="function", value="ohmf")
        self.assertEqual((r["value"], r["source"]), ("OHMF", "assumed"))
        self.assertEqual(self.stack.sim(22).log[:2], ["END ALWAYS", "ID?"])
        self.assertAlmostEqual(self.a.request("dev.get", instance="dmm", key="reading")["value"], 1.2345678, places=4)
        self.assertCode("rejected", self.a.request, "dev.set", instance="dmm", key="function", value="VOLTS")

    def test_offline_instrument(self):
        r = self.a.request("instances.create", instance={"id": "ghost", "type": "generic.scpi@1.0.0", "address": {"pad": 9}})
        self.assertIn("error", r["identify"])
        self.assertCode("gpib", self.a.request, "dev.get", instance="ghost", key="identity")
        self.assertFalse([i for i in self.a.request("instances.list")["instances"] if i["id"] == "ghost"][0]["online"])


class TypeUnits(unittest.TestCase):
    def test_parsers(self):
        t = DeviceType(_read(os.path.join(BUILTIN, "hp.6632a", "1.0.0.yaml")))
        self.assertEqual(t.parse(t.readouts["v_meas"], "VOUT  5.0012"), 5.0012)
        self.assertEqual(t.parse(t.readouts["status"], " 1"), 1)
        self.assertEqual(t.format_value(t.controls["output"], True), "1")
        t = DeviceType(_read(os.path.join(BUILTIN, "hp.3457a", "1.0.0.yaml")))
        self.assertEqual(t.format_value(t.controls["autozero"], "ONCE"), "ONCE")
        self.assertEqual(t.coerce(t.controls["autozero"], "once"), "ONCE")


if __name__ == "__main__":
    unittest.main()
