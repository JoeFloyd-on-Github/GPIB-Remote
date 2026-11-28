"""The Pi installer (deploy/build_installer.py): contents, modes, line endings, the
self-extracting header. CI also installs it for real (.github/workflows/installer.yml).

    python -m unittest discover -s tests -p test_installer.py
"""

import hashlib
import importlib.util
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("build_installer", os.path.join(ROOT, "deploy", "build_installer.py"))
bi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bi)

PACKAGES = os.path.join(ROOT, "deploy", "packages")


@unittest.skipUnless(shutil.which("git"), "needs git")
class Installer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.isdir(PACKAGES) or not os.listdir(PACKAGES):
            raise unittest.SkipTest("deploy/packages is empty (run deploy/fetch_packages.py)")
        cls.tmp = tempfile.mkdtemp(prefix="gpibremote-inst-")
        try:
            cls.r = bi.build("9.9.9-test", cls.tmp, fetch=False)
        except SystemExit as exc:   # packages folder incomplete
            shutil.rmtree(cls.tmp, ignore_errors=True)
            raise unittest.SkipTest(str(exc))
        with open(cls.r["run"], "rb") as fh:
            cls.run_bytes = fh.read()
        with open(cls.r["tar"], "rb") as fh:
            cls.tgz = fh.read()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def members(self):
        with tarfile.open(fileobj=io.BytesIO(self.tgz), mode="r:gz") as tar:
            return {m.name: m for m in tar.getmembers()}, {m.name: tar.extractfile(m).read()
                                                          for m in tar.getmembers() if m.isfile()}

    def test_contents(self):
        m, data = self.members()
        top = "gpibremote-9.9.9-test/"
        for f in ("server/gpibremote/__main__.py", "server/gpibremote/web.py", "cli/gpibctl/api.py",
                  "web/index.html", "devices/types/agilent.e3631a/1.1.0.yaml", "deploy/install.sh",
                  "deploy/uninstall.sh", "deploy/gpibremote.service", "deploy/server.yaml.example",
                  "deploy/requirements.txt", "scripts/nickel_plating.py", "docs/api.md", "VERSION",
                  "BUILD-INFO"):
            self.assertIn(top + f, m, f)
        self.assertTrue([n for n in m if n.startswith(top + "deploy/packages/pyyaml-")])
        self.assertTrue([n for n in m if n.startswith(top + "deploy/packages/setuptools-")])
        self.assertFalse([n for n in m if "/tests/" in n or "__pycache__" in n or "/.git" in n])
        self.assertEqual(data[top + "VERSION"], b"9.9.9-test\n")
        self.assertEqual(m[top + "deploy/install.sh"].mode, 0o755)
        self.assertEqual(m[top + "deploy/uninstall.sh"].mode, 0o755)
        self.assertEqual(m[top + "server/gpibremote/web.py"].mode, 0o644)
        for name, body in data.items():
            if "/deploy/packages/" not in name:
                self.assertNotIn(b"\r\n", body, name)
        self.assertIn(b"WantedBy=multi-user.target", data[top + "deploy/gpibremote.service"])

    def test_self_extracting_header(self):
        head_end = self.run_bytes.index(b"\nexit $?\n") + len(b"\nexit $?\n")
        head = self.run_bytes[:head_end].decode("ascii")
        self.assertTrue(head.startswith("#!/bin/sh\n"))
        self.assertNotRegex(head, r"@[A-Z0-9]+@")   # every placeholder filled in
        skip = int(head.split("SKIP=")[1].split("\n")[0])
        self.assertEqual(skip, head.count("\n") + 1)
        self.assertEqual(self.run_bytes[head_end:], self.tgz)
        self.assertIn("SUM=%s\n" % hashlib.sha256(self.tgz).hexdigest(), head)
        with open(os.path.join(self.tmp, "SHA256SUMS")) as fh:
            self.assertIn(hashlib.sha256(self.run_bytes).hexdigest(), fh.read())

    @unittest.skipUnless(shutil.which("sh") and shutil.which("tar") and shutil.which("tail"), "needs sh, tar, tail")
    def test_run_unpacks(self):
        out = os.path.join(self.tmp, "x")
        sh = shutil.which("sh")
        p = subprocess.run([sh, self.r["run"], "--version"], stdout=subprocess.PIPE, timeout=60)
        self.assertEqual(p.stdout.strip(), b"9.9.9-test")
        p = subprocess.run([sh, self.r["run"], "--extract", out.replace("\\", "/")],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertTrue(os.path.isfile(os.path.join(out, "gpibremote-9.9.9-test", "deploy", "install.sh")))


if __name__ == "__main__":
    unittest.main()
