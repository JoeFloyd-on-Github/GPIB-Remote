"""The test-script API (gpibctl.api) against a simulated bench: E3631A at 5, 6632A at 6, 3457A at 22.

    python -m unittest discover -s tests
"""

import os
import shutil
import tempfile
import unittest

from test_phase1 import PSU, Stack  # noqa: F401  (sets sys.path)

from gpibctl import api  # noqa: E402
from gpibctl.api import ApiError, Bench, DataLog, ServerError  # noqa: E402
from gpibremote.devicetypes import DeviceType, TypeError_  # noqa: E402


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gpibremote-test-")
        self.stack = Stack(os.path.join(self.tmp, "data"))
        self.bench = Bench("127.0.0.1", self.stack.port, user="tester")
        self.bench.request("instances.create", instance=dict(PSU, address=dict(PSU["address"])))

    def tearDown(self):
        self.bench.close()
        self.stack.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scan_add_and_handles(self):
        found = {f.pad: f for f in self.bench.scan()}
        self.assertEqual(sorted(found), [5, 6, 16, 17, 22])
        self.assertIn("hp.8714es", found[16].types)
        self.assertEqual(found[5].instance, "bench-psu")
        self.assertEqual(found[6].types, ["hp.6632a"])
        self.assertEqual(found[22].types, ["hp.3457a"])
        self.assertEqual(found[22].probe, "ID?")          # *IDN? got no answer, so ID? was tried
        dmm = self.bench.add("dmm-ohms", "hp.3457a", 22, name="Ohms meter", notes="Kelvin probe")
        self.assertEqual(dmm.type, "hp.3457a@1.1.0")
        self.assertIs(self.bench["dmm-ohms"], dmm)
        self.assertEqual(sorted(d.id for d in self.bench.devices()), ["bench-psu", "dmm-ohms"])
        self.assertIn("offset_comp", dmm.describe())
        self.assertEqual(dmm.identify()["match"], True)
        dmm.update(notes="moved")
        self.assertEqual(dmm.reload().config["notes"], "moved")
        self.assertTrue(dmm.history())
        self.bench.remove("dmm-ohms")
        self.assertEqual([d.id for d in self.bench.devices()], ["bench-psu"])

    def test_supply_role_and_conventions(self):
        old = self.bench.supply("bench-psu")          # e3631a@1.0.0: no roles, by convention
        self.assertEqual(old.channel, "P6V")
        self.assertEqual(old.key("voltage"), "P6V.voltage")
        self.assertAlmostEqual(old.max_voltage, 6.18)
        self.bench.add("psu2", "hp.6632a", 6)
        psu = self.bench.supply("psu2")
        self.assertTrue(psu.device.roles["supply"])
        self.assertEqual(psu.key("output"), "output")
        psu.device.set_limit("voltage", 0, 8)
        self.assertEqual(psu.max_voltage, 8)
        with self.assertRaises(ServerError) as cm:
            psu.set(voltage=9)
        self.assertEqual(cm.exception.code, "rejected")
        with psu.lock("test"), psu.guard():
            psu.set(voltage=3.0, current=0.5)
            psu.on()
            self.assertTrue(psu.is_on())
            self.assertAlmostEqual(psu.measure_voltage(), 3.0)
        self.assertFalse(psu.is_on())
        self.assertAlmostEqual(psu.setpoints()["current"], 0.0)
        self.assertIsNone(psu.device.lock_holder())
        with self.assertRaises(ApiError):
            self.bench.supply("bench-psu", channel="P99V")

    def test_bench_close_turns_supplies_off(self):
        other = Bench("127.0.0.1", self.stack.port, user="other")
        s = other.supply("bench-psu")
        s.set(voltage=1.0, current=0.1)
        s.on()
        other.close()
        self.assertFalse(self.bench.supply("bench-psu").is_on())

    def test_meter(self):
        meter = self.bench.meter(self.bench.add("dmm", "hp.3457a", 22).id)
        self.assertEqual(meter.functions["ohm4"], "OHMF")
        meter.configure("ohm4", range=30, nplc=100, offset_comp=True, autozero="ON")
        log = self.stack.sim(22).log
        for cmd in ("OHMF", "RANGE 30", "NPLC 100", "OCOMP ON", "AZERO ON"):
            self.assertIn(cmd, log)
        st = meter.measure(4)
        self.assertEqual(st.n, 4)
        self.assertAlmostEqual(st.mean, 1.2345678, places=5)
        meter.null(3)
        self.assertAlmostEqual(meter.read(), 0.0, places=5)
        meter.configure("dcv", range="auto")
        self.assertEqual(meter.zero, 0.0)
        self.assertIn("ARANGE ON", log)
        meter.autocal(wait=0)
        self.assertIn("ACAL OHMS", log)
        with self.assertRaises(ApiError):
            meter.configure("temp")
        with self.assertRaises(ApiError):
            self.bench.meter("bench-psu")
        raw = self.bench.raw(22)
        self.assertEqual(raw.query("ID?").strip(), "HP3457A")
        self.assertAlmostEqual(meter.device.raw.query_float("TRIG SGL"), 1.2345678, places=5)

    def test_thermocouple_meter(self):
        self.bench.add("tc", "hp.3457a", 22)
        with self.assertRaises(ApiError):
            self.bench.thermometer("tc")
        th = self.bench.thermometer("tc", thermocouple="K", cjc=25.0).setup()
        self.assertIn("DCV", self.stack.sim(22).log)
        # sim reads 1.2345678 V: far beyond a thermocouple, but the arithmetic is what's tested
        self.stack.sim(22).sim_readouts["reading"] = {"value": 0.001023}   # 1.023 mV over a 25 C junction
        self.assertAlmostEqual(th.read(), 50.0, delta=0.1)
        off = th.calibrate(49.5, n=2)
        self.assertAlmostEqual(off, -0.5, delta=0.1)
        self.assertAlmostEqual(th.read(), 49.5, delta=0.01)


class TypeK(unittest.TestCase):
    TABLE = [(-100, -3.554), (0, 0.0), (25, 1.000), (50, 2.023), (100, 4.096), (200, 8.138),
             (300, 12.209), (500, 20.644)]    # NIST ITS-90 type K reference table, mV

    def test_table(self):
        for t, mv in self.TABLE:
            self.assertAlmostEqual(api.type_k_emf(t), mv, delta=0.002, msg=t)
            self.assertAlmostEqual(api.type_k_temperature(mv), t, delta=0.06, msg=t)

    def test_round_trip(self):
        for t in range(-150, 501, 25):
            self.assertAlmostEqual(api.type_k_temperature(api.type_k_emf(t)), t, delta=0.06)


class Roles(unittest.TestCase):
    BASE = ("schema: 1\ntype: lab.x\nversion: '1.0.0'\n"
            "controls:\n  f: {kind: enum, set: 'F {value}', options: {A: {}, B: {}}}\n"
            "  v: {kind: number, set: 'V {value}'}\n"
            "readouts:\n  r: {query: 'R?'}\n")

    def test_valid_and_invalid(self):
        DeviceType(self.BASE + "roles:\n  meter: {function: f, functions: {dcv: A, ohm4: B}, reading: r}\n")
        with self.assertRaises(TypeError_) as cm:
            DeviceType(self.BASE + "roles:\n  meter: {function: f, functions: {dcv: C, xx: A}, reading: q}\n"
                                   "  supply: {voltage: v}\n  robot: {}\n")
        text = "\n".join(cm.exception.errors)
        for msg in ("'C' is not an option", "xx: not a standard function", "reading: must name",
                    "supply.current: required", "robot: unknown role"):
            self.assertIn(msg, text)


class DataLogTest(unittest.TestCase):
    def test_log(self):
        tmp = tempfile.mkdtemp()
        try:
            t = [100.0]
            with DataLog(os.path.join(tmp, "a", "run.csv"), ["i", "v"], {"sample": "S1"}, clock=lambda: t[0]) as log:
                t[0] = 101.5
                log.record(i=0.48, v=2.5)
                log.note("output off")
            with open(os.path.join(tmp, "a", "run.csv")) as fh:
                text = fh.read().splitlines()
            self.assertEqual(text[0], "# sample: S1")
            self.assertEqual(text[1], "time,t_s,i,v")
            self.assertTrue(text[2].split(",")[1] == "1.500" and text[2].endswith(",0.48,2.5"))
            self.assertIn("output off", text[3])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
