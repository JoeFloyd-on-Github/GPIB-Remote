"""Meters with settings read back from status bytes (3455A) or registers (3456A), settings
restored when an instrument appears, and network-analyzer traces, on the simulated bench.

    python -m unittest discover -s tests
"""

import copy
import json
import os
import shutil
import tempfile
import time
import unittest

import yaml

from test_phase1 import Stack, BUILTIN, _read  # noqa: F401  (sets sys.path)

from gpibctl.api import ApiError, Bench                       # noqa: E402
from gpibremote import runtime                                # noqa: E402
from gpibremote.backends.sim import TypeSimInstrument         # noqa: E402
from gpibremote.devicetypes import validate                   # noqa: E402


def _wait(cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class Phase4(unittest.TestCase):
    def setUp(self):
        self.saved_every = runtime.PRESENCE_EVERY
        runtime.PRESENCE_EVERY = 0.2
        self.tmp = tempfile.mkdtemp(prefix="gpibremote-test-")
        self.stack = Stack(os.path.join(self.tmp, "data"))
        self.a = self.stack.client("alice")

    def tearDown(self):
        runtime.PRESENCE_EVERY = self.saved_every
        self.a.close()
        self.stack.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def saved_file(self, iid):
        path = os.path.join(self.tmp, "data", "state", iid + ".json")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)["settings"]

    def test_3455a_setup_read_back(self):
        self.a.request("instances.create", instance={"id": "dvm", "type": "hp.3455a@1.0.0", "address": {"pad": 1}})
        sim = self.stack.sim(1)
        # Bytes as read from the bench unit: math off, internal trigger, autorange, auto-cal on, DC volts.
        r = self.a.request("dev.get", instance="dvm", key="function")
        self.assertEqual((r["value"], r["source"]), ("DCV", "read"))
        state = self.a.request("dev.refresh", instance="dvm")["state"]
        self.assertEqual({k: state[k]["value"] for k in ("range", "trigger", "high_res", "autocal", "math")},
                         {"range": "AUTO", "trigger": "INT", "high_res": False, "autocal": True, "math": "OFF"})
        self.assertEqual(state["autocal"]["source"], "read")
        # A set is confirmed by reading the status bytes again (here the meter takes it: bit 3 low).
        sim.sim_readouts["setup"] = {"value": ";F=>"}
        r = self.a.request("dev.set", instance="dvm", key="high_res", value="on")
        self.assertEqual((r["value"], r["source"]), (True, "read"))
        self.assertEqual(sim.log[-2:], ["H1", "B"])
        # ...and if the meter didn't take it, the page shows what the meter reports.
        r = self.a.request("dev.set", instance="dvm", key="math", value="scale")
        self.assertEqual((r["value"], r["source"]), ("OFF", "read"))
        self.assertAlmostEqual(self.a.request("dev.get", instance="dvm", key="reading")["value"], -0.15, places=1)
        self.assertEqual(self.saved_file("dvm"), {"high_res": True, "math": "SCALE"})

    def test_restore_when_the_meter_reappears(self):
        self.a.request("instances.create", instance={"id": "dvm", "type": "hp.3455a@1.0.0", "address": {"pad": 1}})
        self.a.request("dev.set", instance="dvm", key="range", value="10")
        self.a.request("dev.set", instance="dvm", key="high_res", value="on")
        self.a.request("dev.set", instance="dvm", key="math_y", value=2.5)
        self.assertEqual(self.saved_file("dvm"), {"range": "10", "high_res": True, "math_y": 2.5})
        rt = self.stack.manager.get("dvm")
        # Switched off: the presence check (a serial poll) finds nobody there.
        backend = self.stack.bus.backend
        backend.instruments.pop(1)
        self.assertTrue(_wait(lambda: rt.online is False))
        # Switched on again, at its power-on settings: the server reapplies its settings, in the
        # type's control order.
        fresh = TypeSimInstrument(self.stack.registry.get_type("hp.3455a"))
        backend.instruments[1] = fresh
        self.assertTrue(_wait(lambda: "EY2.5SY" in fresh.log), fresh.log)
        sets = [c for c in fresh.log if c != "B"]
        self.assertEqual(sets, ["R3", "H1", "EY2.5SY"])
        # Nothing is restored while it stays on.
        n = len(fresh.log)
        time.sleep(0.8)
        self.assertEqual([c for c in fresh.log[n:] if c != "B"], [])
        # An instance can opt out.
        self.a.request("instances.update", instance="dvm", changes={"restore": False})
        backend.instruments.pop(1)
        time.sleep(0.8)
        again = TypeSimInstrument(self.stack.registry.get_type("hp.3455a"))
        backend.instruments[1] = again
        time.sleep(0.8)
        self.assertEqual(again.log, [])

    def test_3456a_registers_and_ranges(self):
        bench = Bench("127.0.0.1", self.stack.port, user="tester")
        try:
            dev = bench.add("dvm6", "hp.3456a", 17)
            dev.set("nplc", 100)
            self.assertEqual(dev.get_entry("nplc", fresh=False)["source"], "read")   # registers are read back (REI)
            self.assertIn("100STI", self.stack.sim(17).log)
            dev.set("function", "DCV")
            self.assertEqual(dev.get_entry("function", fresh=False)["source"], "assumed")
            meter = bench.meter("dvm6")
            meter.configure("dcv", range=5)
            self.assertEqual(dev.get("range"), "R4")       # smallest range covering 5 V: 10 V
            meter.configure(range=0.05)
            self.assertEqual(dev.get("range"), "R2")
            meter.configure("ohm4", range=2000)
            self.assertEqual(dev.get("range"), "R4")       # 10 kohm
            meter.configure(range="auto")
            self.assertEqual(dev.get("range"), "AUTO")
            with self.assertRaises(ApiError):
                meter.configure("dcv", range=2000)
            self.assertAlmostEqual(meter.read(), -4.034, places=2)
            # Home returns it to power-on settings: nothing is left to restore.
            self.assertTrue(self.saved_file("dvm6"))
            dev.action("home")
            self.assertEqual(self.saved_file("dvm6"), {})
        finally:
            bench.close()

    def test_network_analyzer_traces(self):
        self.a.request("instances.create", instance={"id": "vna", "type": "hp.8714es@1.1.0", "address": {"pad": 16}})
        r = self.a.request("dev.get", instance="vna", key="CH1.trace")
        self.assertEqual(len(r["value"]), 201)
        self.assertIn("TRAC? CH1FDATA", self.stack.sim(16).log)
        self.a.request("dev.set", instance="vna", key="CH2.points", value=51)
        r = self.a.request("dev.get", instance="vna", key="CH2.complex")
        self.assertEqual(len(r["value"]), 102)            # real, imaginary pairs
        self.assertTrue(all(abs(v) <= 1 for v in r["value"]))
        model = self.a.request("types.get", type="hp.8714es@1.1.0")
        self.assertEqual(model["model"]["display"]["kind"], "network_analyzer")


class Phase4Types(unittest.TestCase):
    def raw(self, t, v):
        return yaml.safe_load(_read(os.path.join(BUILTIN, t, v + ".yaml")))

    def test_new_types_valid(self):
        for t, v in (("hp.3455a", "1.0.0"), ("hp.3456a", "1.0.0"), ("hp.8714es", "1.1.0")):
            self.assertEqual(validate(self.raw(t, v)), [], t)

    def bad(self, raw, msg):
        errs = validate(raw)
        self.assertTrue([x for x in errs if msg in x], errs)

    def test_readback_validation(self):
        raw = self.raw("hp.3455a", "1.0.0")
        r = copy.deepcopy(raw)
        r["controls"]["function"]["readback"]["cases"][0]["value"] = "VOLTS"
        self.bad(r, "expected one of")
        r = copy.deepcopy(raw)
        r["readouts"]["setup"]["parse"] = "int"
        self.bad(r, "must have parse: text")
        r = copy.deepcopy(raw)
        r["controls"]["high_res"]["readback"]["cases"] = [{"value": "on"}]
        self.bad(r, "at least one of equals")
        r = copy.deepcopy(raw)
        r["controls"]["math_y"]["readback"] = {"readout": "setup", "byte": 1, "cases": [{"equals": 1, "value": 1}]}
        self.bad(r, "only for kind bool or enum")
        r = copy.deepcopy(raw)
        r["roles"]["meter"]["ranges"]["dcv"]["5"] = 5
        self.bad(r, "'5' is not an option of range")
        r = copy.deepcopy(raw)
        r["restore"] = "yes"
        self.bad(r, "restore: true or false")

    def test_display_validation(self):
        raw = self.raw("hp.8714es", "1.1.0")
        r = copy.deepcopy(raw)
        del r["display"]["start"]
        self.bad(r, "display.start: required")
        r = copy.deepcopy(raw)
        r["display"]["formatted"] = "sweep_time"
        self.bad(r, "must have parse: list")
        r = copy.deepcopy(raw)
        r["display"]["kind"] = "oscilloscope"
        self.bad(r, "display.kind")


if __name__ == "__main__":
    unittest.main()
