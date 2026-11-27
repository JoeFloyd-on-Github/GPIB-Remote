"""Phase 2 tests: the gpibctl CLI (one-shot commands, shell and menu) against a simulated bench.

    python -m unittest discover -s tests
"""

import io
import os
import shutil
import tempfile
import unittest

from test_phase1 import BUILTIN, PSU, ROOT, Stack  # noqa: F401  (sets sys.path)

from gpibctl.__main__ import main  # noqa: E402


def feeder(lines):
    it = iter(lines)

    def inp(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    return inp


class Phase2(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gpibremote-test-")
        self.stack = Stack(os.path.join(self.tmp, "data"))
        self.a = self.stack.client("alice")
        self.a.request("instances.create", instance=dict(PSU, address=dict(PSU["address"])))

    def tearDown(self):
        self.a.close()
        self.stack.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *argv, lines=(), status=0):
        out = io.StringIO()
        rc = main(["--host", "127.0.0.1", "--port", str(self.stack.port)] + list(argv), out=out, inp=feeder(lines))
        self.assertEqual(rc, status, out.getvalue())
        return out.getvalue()

    # ------------------------------------------------------------------------------------------
    def test_listing(self):
        self.assertIn("bench-psu", self.cli("ls"))
        self.assertIn("agilent.e3631a", self.cli("types"))
        info = self.cli("info")
        self.assertIn("board", info.lower())
        show = self.cli("type", "show", "agilent.e3631a")
        for word in ("P6V", "voltage", "v_meas", "reset"):
            self.assertIn(word, show)
        self.assertIn("type: agilent.e3631a", self.cli("type", "yaml", "agilent.e3631a"))

    def test_set_get_state(self):
        self.cli("set", "bench-psu", "P6V.voltage", "3.25", "P6V.current", "0.5")
        self.assertEqual(self.cli("get", "bench-psu", "P6V.voltage", "--bare").strip(), "3.25")
        v = self.a.request("dev.get", instance="bench-psu", key="P6V.voltage")["value"]
        self.assertAlmostEqual(v, 3.25)
        state = self.cli("state", "bench-psu")
        self.assertIn("P6V.voltage", state)
        self.assertIn("3.25", state)
        err = self.cli("set", "bench-psu", "P6V.voltage", "99", status=1)
        self.assertIn("error", err)
        self.assertEqual(self.cli("get", "bench-psu", "output", "--bare").strip(), "off")

    def test_lock_refuses_others(self):
        self.a.request("dev.lock", instance="bench-psu")
        out = self.cli("set", "bench-psu", "P6V.voltage", "1", status=1)
        self.assertIn("locked", out)
        self.assertIn("alice", self.cli("locks"))
        self.a.request("dev.unlock", instance="bench-psu")
        self.cli("set", "bench-psu", "P6V.voltage", "1")

    def test_instances_and_types(self):
        out = self.cli("inst", "create", "psu2", "hp.6632a", "6", "--name", "Second PSU", "--limit", "voltage=0:10")
        self.assertIn("psu2", out)
        self.assertIn("Second PSU", self.cli("inst", "show", "psu2"))
        self.cli("inst", "update", "psu2", "--notes", "on the shelf", "--off-on-disconnect")
        inst = self.a.request("instances.get", instance="psu2")["instance"]
        self.assertEqual(inst["notes"], "on the shelf")
        self.assertIn("psu2", self.cli("inst", "history", "psu2"))
        self.cli("inst", "delete", "psu2", "-y")
        self.assertNotIn("psu2", self.cli("ls"))

        path = os.path.join(self.tmp, "meter.yaml")
        with open(os.path.join(BUILTIN, "generic.scpi", "1.0.0.yaml"), encoding="utf-8") as fh:
            text = fh.read().replace("type: generic.scpi", "type: lab.meter")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.assertIn("valid", self.cli("type", "validate", path))
        self.assertIn("lab.meter@1.0.0", self.cli("type", "push", path, "-m", "my meter"))
        self.assertIn("lab.meter", self.cli("types"))

    def test_actions_watch_raw(self):
        self.cli("do", "bench-psu", "reset", "-y")
        self.assertIn("cancelled", self.cli("do", "bench-psu", "reset", lines=["n"], status=1).lower())
        out = self.cli("watch", "bench-psu", "-n", "2", "-t", "15")
        self.assertIn("v_meas", out)
        self.assertIn("E3631A", self.cli("query", "5", "*IDN?"))
        self.assertIn("5", self.cli("listeners"))

    def test_shell_keeps_lock(self):
        out = self.cli("shell", lines=["lock bench-psu", "set bench-psu P6V.voltage 2", "locks",
                                       "bogus", "get bench-psu P6V.voltage --bare", "quit"])
        self.assertIn("2.0", out)
        self.assertIn("bench-psu", out)
        self.assertIn("invalid choice", out)
        # The shell's connection is closed, so the lock is gone.
        self.cli("set", "bench-psu", "P6V.voltage", "1")

    def test_menu(self):
        # Main: 1 Output, 2-4 channels (P6V first), 5 System. In P6V: 1 voltage, 2 current, 3 v_meas.
        out = self.cli("menu", "bench-psu", lines=["2", "1", "4.5", "3", "b", "1", "1", "", "q"])
        self.assertIn("you have the lock", out)
        self.assertIn("4.5", out)
        v = self.a.request("dev.get", instance="bench-psu", key="P6V.voltage")["value"]
        self.assertAlmostEqual(v, 4.5)
        self.assertFalse(self.a.request("dev.locks")["locks"])
        # Output was left alone (Enter keeps the value).
        self.assertFalse(self.a.request("dev.get", instance="bench-psu", key="output")["value"])
        # Someone else holds the lock: decline to break it, then changes are refused.
        self.a.request("dev.lock", instance="bench-psu")
        out = self.cli("menu", "bench-psu", lines=["n", "2", "1", "1", "q"])
        self.assertIn("locked by alice", out)
        self.assertIn("not set", out)


if __name__ == "__main__":
    unittest.main()
