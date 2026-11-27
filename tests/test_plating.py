"""scripts/nickel_plating.py: the calculations, and the whole guided procedure against a simulated
bench (E3631A at 5; 3457As at 22 ohmmeter, 23 thermocouple, 24 shunt, 25 cell).

    python -m unittest discover -s tests
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest

from test_phase1 import PSU, ROOT, Stack  # noqa: F401  (sets sys.path)

from gpibremote.backends.sim import TypeSimInstrument  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "scripts"))
import nickel_plating as npl  # noqa: E402


class Calculations(unittest.TestCase):
    def test_faraday_and_thickness(self):
        # 4 x 4 cm, one side, 480 mA for 30 min: 864 C
        s = npl.plating_summary(864.0, 16.0, 10.0, 0.25e-3, 4.0)
        self.assertAlmostEqual(s["mass"], 864 * 58.69 / (2 * 96485), places=9)
        self.assertAlmostEqual(s["mass"], 0.26278, places=4)
        self.assertAlmostEqual(s["t_ni"] * 1e4, 18.45, places=1)          # um
        self.assertAlmostEqual(s["final_weight"], 10.26278, places=4)
        cu = npl.copper_estimate(0.25e-3, 4.0)
        self.assertAlmostEqual(s["r_2d"] * npl.C2D, 1 / (1 / cu["rs_cu"] + 1 / s["rs_ni"]))
        self.assertLess(s["r_1d"], 0.25e-3)

    def test_copper_models(self):
        cu = npl.copper_estimate(0.25e-3, 4.0)
        self.assertAlmostEqual(npl.C2D, 4.53236, places=5)
        self.assertAlmostEqual(cu["t_cu_1d"] * 1e4, 34.4, places=1)         # 1.72e-6 * 2 / (4 * 0.25e-3)


class Operator:
    """Scripted operator: answers prompts by matching text, and changes the simulated bench the
    way the real one would change (probe on the shorting bar, on the coupon, output on)."""

    def __init__(self, test, answers):
        self.test, self.answers, self.prompts = test, answers, []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        for text, answer in self.answers:
            if text in prompt:
                if callable(answer):
                    answer = answer()
                return "" if answer is None else answer
        raise AssertionError("unexpected prompt: %r" % prompt)


class Procedure(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gpibremote-test-")
        self.stack = s = Stack(os.path.join(self.tmp, "data"))
        dmm = s.registry.get_type("hp.3457a")
        for pad in (23, 24, 25):
            s.bus.backend.instruments[pad] = TypeSimInstrument(dmm)
        self.set_reading(22, 0.0)
        self.set_reading(23, 0.001)        # 1.000 mV + 23 C junction: about 47.6 C
        self.set_reading(24, 0.0)          # shunt, output off
        self.set_reading(25, 2.2)          # cell voltage
        self.a = s.client("alice")
        self.a.request("instances.create", instance=dict(PSU, address=dict(PSU["address"])))
        self.bench_file = os.path.join(self.tmp, "bench.json")

    def tearDown(self):
        self.a.close()
        self.stack.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def set_reading(self, pad, value):
        self.stack.sim(pad).sim_readouts["reading"] = {"value": value}

    def script(self, argv, answers):
        out = io.StringIO()
        op = Operator(self, answers)
        argv = ["--host", "127.0.0.1", "--port", str(self.stack.port), "--bench", self.bench_file,
                "--log-dir", os.path.join(self.tmp, "logs")] + argv
        rc = npl.main(argv, inp=op, out=out)
        return rc, out.getvalue(), op

    def setup_bench(self):
        roles = iter(["bench-psu", "add", "add", "add", "none"])
        pads = iter(["22", "23", "24"])
        answers = [
            ("handle", None),                       # before "instance id": it says "(instance id)"
            ("instance id", lambda: next(roles)),
            ("GPIB address", lambda: next(pads)),
            ("device type", None), ("name", None), ("channel", None),
            ("thermocouple type", None), ("shunt resistance", "0.1"),
            ("window, low", "45"), ("window, high", "60"),
        ]
        rc, out, op = self.script(["--setup"], answers)
        self.assertEqual(rc, 0, out)
        return out

    def test_setup_then_run(self):
        out = self.setup_bench()
        self.assertIn("(not configured)", out)
        with open(self.bench_file) as fh:
            cfg = json.load(fh)
        self.assertEqual(cfg["supply"], {"id": "bench-psu", "channel": "P6V"})
        self.assertEqual(cfg["ohmmeter"]["id"], "ohmmeter-22")
        self.assertEqual(cfg["thermometer"], {"id": "thermometer-23", "thermocouple": "K"})
        self.assertEqual(cfg["shunt"], {"id": "shunt-24", "ohms": 0.1})
        self.assertIsNone(cfg["cell"])

        def probe(value):
            return lambda: self.set_reading(22, value)

        def start():
            self.set_reading(24, 0.048)     # 480 mA through 0.1 ohm once the output is on
            return "y"
        answers = [
            ("self-calibration", "y"),
            ("Re-zero", "n"),                       # before "shorting bar": it mentions the bar
            ("shorting bar", probe(0.00002)),
            ("Reference resistor value", "0.001"),
            ("on the reference resistor", probe(0.00102)),
            ("reference-junction", "23"),
            ("Reference temperature now", "47.0"),
            ("Probe in the reference", None),
            ("Sample ID", "T/1"), ("Initial weight", "12.0"), ("Plated length", None),
            ("Plated width", None), ("Sides", None),
            ("Initial: probe", probe(0.00027)),
            ("pH", "4.1"), ("Use 480.0 mA", "y"), ("Compliance", "4.5"),
            ("Duration", "0.05"), ("Sample interval", "1"),
            ("Cell ready", start),
            ("Rinse", None), ("Final weight", "12.0025"),
            ("Final: probe", probe(0.000245)),
        ]
        rc, out, op = self.script(["--autocal-wait", "0", "--positions", "2", "--readings", "2"], answers)
        self.assertEqual(rc, 0, out)
        self.assertIn("ACAL OHMS", self.stack.sim(22).log)
        for cmd in ("OHMF", "RANGE 30", "NPLC 100", "OCOMP ON"):
            self.assertIn(cmd, self.stack.sim(22).log)
        self.assertIn("error +0.000%", out)                    # reference resistor, after the zero
        self.assertIn("thermometer offset", out)
        self.assertIn("Initial resistance 0.2500 mohm", out)
        self.assertIn("480.00", out)                           # current from the shunt
        self.assertIn("AT-COMPLIANCE", out)                    # sim supply sits at its voltage setting
        self.assertIn("Output OFF (completed", out)
        self.assertIn("measured final: 0.2250 mohm", out)
        self.assertIn("current efficiency", out)
        # supply ended off with the current at 0, and nothing is left locked
        get = lambda k: self.a.request("dev.get", instance="bench-psu", key=k)["value"]  # noqa: E731
        self.assertFalse(get("output"))
        self.assertAlmostEqual(get("P6V.current"), 0.0)
        self.assertFalse(self.a.request("dev.locks")["locks"])
        logs = sorted(os.listdir(os.path.join(self.tmp, "logs")))
        self.assertEqual(len(logs), 3, logs)                    # csv, json, summary
        csv_name = next(n for n in logs if n.endswith(".csv"))
        self.assertTrue(csv_name.startswith("T_1_"))
        with open(os.path.join(self.tmp, "logs", csv_name)) as fh:
            text = fh.read()
        self.assertIn("time,t_s,i_a,v_shunt_v,v_cell_v,v_supply_v,r_cell_ohm,q_c,temp_c,alarm", text)
        self.assertIn("# sample: T/1", text)
        self.assertIn("output off: completed", text)
        rows = [ln for ln in text.splitlines() if ln and not ln.startswith(("#", "time"))]
        self.assertGreaterEqual(len(rows), 3)
        self.assertRegex(rows[0], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d,0\.\d{3},0\.48,")

    def test_declining_leaves_output_off(self):
        self.setup_bench()
        answers = [
            ("self-calibration", "n"), ("shorting bar", lambda: self.set_reading(22, 0.0)),
            ("Reference resistor value", None), ("reference-junction", None), ("Reference temperature now", None),
            ("Sample ID", None), ("Initial weight", "12"), ("Plated", None), ("Sides", None),
            ("Initial: probe", lambda: self.set_reading(22, 0.00025)),
            ("pH", None), ("Use", "y"), ("Compliance", None), ("Duration", None), ("Sample interval", None),
            ("Cell ready", "n"),
        ]
        rc, out, op = self.script(["--positions", "1", "--readings", "1"], answers)
        self.assertEqual(rc, 1)
        self.assertIn("output left off", out)
        self.assertNotIn("OUTP ON", [c.upper() for c in self.stack.sim(5).log])
        self.assertFalse(self.a.request("dev.locks")["locks"])

    def test_locked_instrument_is_refused(self):
        self.setup_bench()
        self.a.request("dev.lock", instance="shunt-24", reason="someone else")
        rc, out, op = self.script([], [])
        self.assertEqual(rc, 1)
        self.assertIn("cannot take control of shunt-24", out)
        self.assertNotIn("OUTP ON", [c.upper() for c in self.stack.sim(5).log])
        self.assertEqual(list(self.a.request("dev.locks")["locks"]), ["shunt-24"])

    def test_no_bench_file(self):
        rc, out, op = self.script([], [])
        self.assertEqual(rc, 1)
        self.assertIn("run with --setup first", out)


if __name__ == "__main__":
    unittest.main()
