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

    def test_stale_errors_are_not_blamed_on_a_set(self):
        self.a.request("instances.create", instance={"id": "vna", "type": "hp.8714es@1.2.0", "address": {"pad": 16}})
        sim = self.stack.sim(16)
        # A poll the analyzer refused earlier filled its error queue.
        sim.errors[:] = ['-221,"Settings conflict; Marker value query not valid on this type of measurement."'] * 5 \
            + ['-350,"Too many errors"']
        r = self.a.request("dev.set", instance="vna", key="CH1.measurement", value="S11")
        self.assertEqual((r["value"], r["source"]), ("S11", "read"))
        self.assertEqual(sim.errors, [])
        # The set's own error is still reported, and the setting shows what the analyzer kept.
        sim.refuse["CALC2:FORM SMIT"] = '-221,"Settings conflict; Smith Chart format is only valid when measuring Reflection."'
        with self.assertRaises(Exception) as cm:
            self.a.request("dev.set", instance="vna", key="CH2.format", value="SMIT")
        self.assertIn("only valid when measuring Reflection", str(cm.exception))
        self.assertEqual(sim.errors, [])

    def test_measurement_rereads_format_and_marker_skipped_in_smith(self):
        self.a.request("instances.create", instance={"id": "vna", "type": "hp.8714es@1.2.0", "address": {"pad": 16}})
        sim = self.stack.sim(16)
        self.a.request("dev.set", instance="vna", key="CH1.measurement", value="S11")
        i = sim.log.index('SENS1:FUNC "XFR:S 1,1"')
        self.assertIn("CALC1:FORM?", sim.log[i:])               # the analyzer may have changed format with it
        self.a.request("dev.set", instance="vna", key="CH1.format", value="SMIT")
        n = len(sim.log)
        with self.assertRaises(Exception) as cm:
            self.a.request("dev.get", instance="vna", key="CH1.marker_value")
        self.assertIn("while format is SMIT", str(cm.exception))
        self.assertNotIn("CALC1:MARK1:Y?", sim.log[n:])
        self.a.request("dev.set", instance="vna", key="CH1.format", value="POL")
        self.assertIsNotNone(self.a.request("dev.get", instance="vna", key="CH1.marker_value")["value"])

    def test_8714es_1_3_binary_traces_waits_and_smith_markers(self):
        self.a.request("instances.create", instance={"id": "vna", "type": "hp.8714es@1.3.0", "address": {"pad": 16}})
        sim = self.stack.sim(16)
        # Traces come as binary blocks, with the data format set back to ASCII in the same message.
        r = self.a.request("dev.get", instance="vna", key="CH1.trace")
        self.assertEqual(len(r["value"]), 201)
        self.assertIn("FORM:DATA REAL,32;:FORM:BORD NORM;:TRAC? CH1FDATA;:FORM:DATA ASC", sim.log)
        # Overlapped settings wait for *OPC? before they are read back.
        n = len(sim.log)
        r = self.a.request("dev.set", instance="vna", key="CH1.points", value="51")
        self.assertEqual((r["value"], r["source"]), ("51", "read"))
        log = sim.log[n:]
        self.assertLess(log.index("SENS1:SWE:POIN 51"), log.index("*OPC?"))
        self.assertLess(log.index("*OPC?"), log.index("SENS1:SWE:POIN?"))
        r = self.a.request("dev.set", instance="vna", key="CH1.if_bandwidth", value="250")
        self.assertEqual(r["value"], "250")
        with self.assertRaises(Exception):
            self.a.request("dev.set", instance="vna", key="CH1.if_bandwidth", value="300")
        # Smith format: the formatted trace has real, imaginary pairs; the marker reads as R + jX.
        self.a.request("dev.set", instance="vna", key="CH1.measurement", value="S11")
        self.a.request("dev.set", instance="vna", key="CH1.format", value="SMIT")
        self.assertEqual(len(self.a.request("dev.get", instance="vna", key="CH1.trace")["value"]), 102)
        self.assertIsNotNone(self.a.request("dev.get", instance="vna", key="CH1.marker_resistance")["value"])
        with self.assertRaises(Exception) as cm:
            self.a.request("dev.get", instance="vna", key="CH1.marker_magnitude")
        self.assertIn("while format is SMIT", str(cm.exception))
        # A single sweep holds the sweep and answers once it has finished.
        n = len(sim.log)
        self.a.request("dev.action", instance="vna", action="single_sweep")
        self.assertEqual(sim.log[n:], ["ABOR", "INIT1:CONT OFF", "INIT1", "*OPC?"])
        self.assertIs(self.a.request("dev.get", instance="vna", key="continuous", fresh=False)["value"], False)
        # Status registers decoded by bit.
        self.assertEqual(self.a.request("dev.get", instance="vna", key="measuring_bits")["value"], "CH1")
        self.assertEqual(self.a.request("dev.get", instance="vna", key="questionable_bits")["value"], "ok")


class Supply6632A(unittest.TestCase):
    """hp.6632a 1.4.0: status register decoded bit by bit (manual Table 6-4)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gpibremote-test-")
        self.stack = Stack(os.path.join(self.tmp, "data"))
        self.a = self.stack.client("alice")
        self.a.request("instances.create", instance={"id": "psu", "type": "hp.6632a@1.4.0", "address": {"pad": 6}})
        self.sim = self.stack.sim(6)

    def tearDown(self):
        self.a.close()
        self.stack.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def get(self, key):
        return self.a.request("dev.get", instance="psu", key=key)["value"]

    def status(self, value):
        self.sim.sim_readouts["status"] = {"value": value}
        self.a.request("dev.get", instance="psu", key="status")
        state = self.a.request("dev.state", instance="psu")["state"]
        return {k: state[k]["value"] for k in state}

    def test_status_bits_decoded(self):
        s = self.status(1024)                      # output off, FAST switch (the bench unit)
        self.assertEqual((s["mode"], s["status_bits"], s["response"], s["output"], s["cv"]),
                         ("Off", "FAST", "FAST", False, False))
        s = self.status(1 + 2048)                  # CV, NORMAL
        self.assertEqual((s["mode"], s["status_bits"], s["response"], s["output"], s["cv"]),
                         ("CV", "CV, NORM", "NORMAL", True, True))
        s = self.status(512 + 1024)                # sinking current
        self.assertEqual((s["mode"], s["cc_neg"], s["output"]), ("-CC (sinking)", True, True))
        s = self.status(8 + 128 + 1024)            # OV trip with a programming error: output disabled
        self.assertEqual((s["mode"], s["ov_tripped"], s["prog_error"], s["output"]),
                         ("OV tripped (output disabled)", True, True, False))
        self.assertEqual(self.get("unregulated"), False)       # a get reads STS? again
        self.sim.sim_readouts["astatus"] = {"value": 2 + 4 + 1024}
        self.assertEqual(self.get("astatus_bits"), "+CC, UNR, FAST")
        self.sim.sim_readouts["fault"] = {"value": 0}
        self.assertEqual(self.get("fault_bits"), "none")

    def test_error_codes_explained(self):
        self.sim.refuse["VSET 20"] = "42"
        with self.assertRaises(Exception) as cm:
            self.a.request("dev.set", instance="psu", key="voltage", value=20)
        self.assertIn("42 (voltage out of range)", str(cm.exception))

    def test_clear_gives_power_on_values(self):
        self.a.request("dev.set", instance="psu", key="voltage", value=5)
        self.a.request("dev.set", instance="psu", key="ocp", value="on")
        self.a.request("dev.action", instance="psu", action="clear")
        state = self.a.request("dev.state", instance="psu")["state"]
        self.assertEqual({k: state[k]["value"] for k in ("voltage", "current", "ovp", "ocp", "srq", "display")},
                         {"voltage": 0, "current": 0.02, "ovp": 22, "ocp": False, "srq": False, "display": True})
        self.assertEqual(state["voltage"]["source"], "assumed")


class Phase4Types(unittest.TestCase):
    def raw(self, t, v):
        return yaml.safe_load(_read(os.path.join(BUILTIN, t, v + ".yaml")))

    def test_new_types_valid(self):
        for t, v in (("hp.3455a", "1.0.0"), ("hp.3456a", "1.0.0"), ("hp.8714es", "1.1.0"),
                     ("hp.8714es", "1.2.0"), ("hp.8714es", "1.3.0"), ("hp.6632a", "1.4.0")):
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

    def test_decode_validation(self):
        raw = self.raw("hp.6632a", "1.4.0")
        r = copy.deepcopy(raw)
        r["readouts"]["cv"]["parse"] = "int"
        self.bad(r, "bool or text for a decoded readout")
        r = copy.deepcopy(raw)
        r["readouts"]["status_bits"]["decode"]["readout"] = "model"
        self.bad(r, "must have parse: int")
        r = copy.deepcopy(raw)
        r["readouts"]["mode"]["poll"] = "1s"
        self.bad(r, "not with query, read or poll")
        r = copy.deepcopy(raw)
        r["errors"]["codes"]["x"] = "bad"
        self.bad(r, "errors.codes")
        r = copy.deepcopy(raw)
        r["actions"]["clear"]["assumes"]["nonesuch"] = 1
        self.bad(r, "unknown control 'nonesuch'")
        r = copy.deepcopy(raw)
        r["actions"]["clear"]["assumes"]["voltage"] = 99
        self.bad(r, "assumes.voltage")

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


class BlockRead(unittest.TestCase):
    """Binary definite-length blocks read through reads that stop at every EOS byte."""

    def backend(self, payload):
        from gpibremote.backends.base import Backend, GpibTimeout

        class Chunky(Backend):
            name = "chunky"

            def __init__(self):
                super().__init__(0)
                self.buf = payload

            def read(self, pad, sad, max_bytes=1 << 20, timeout=3.0):
                if not self.buf:
                    raise GpibTimeout("timeout")
                i = self.buf.find(b"\n")
                cut = len(self.buf) if i < 0 else i + 1
                out, self.buf = self.buf[:cut], self.buf[cut:]
                return out
        return Chunky()

    def test_block_with_newlines_in_the_data(self):
        import struct
        values = [1.0, 2.5e-9, -3.0, struct.unpack(">f", b"\x0a\x0a\x0a\x0a")[0]]
        data = struct.pack(">4f", *values)
        b = self.backend(b"#2%02d" % len(data) + data + b"\n")
        self.assertEqual(b.read_block(16, 0), data)
        self.assertEqual(b.buf, b"")          # the terminator was read too
        b = self.backend(b"#2%02d" % len(data) + data + b"\n+1\n")
        b.read_block(16, 0)
        self.assertEqual(b.buf, b"+1\n")      # nothing after it was taken
        b = self.backend(b"#2%02d" % len(data) + data[:-1] + b"\n" + b"\n")   # data ends with the EOS byte
        self.assertEqual(len(b.read_block(16, 0)), len(data))
        self.assertEqual(b.buf, b"")


if __name__ == "__main__":
    unittest.main()
