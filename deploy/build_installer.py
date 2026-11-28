#!/usr/bin/env python3
"""Build the Pi installer: everything the server needs on the Pi in one self-extracting file.

    python deploy/build_installer.py [--version V] [--out dist] [--no-fetch]

Writes to dist/:
    gpibremote-<version>.run      self-extracting installer: sudo sh gpibremote-<version>.run
    gpibremote-<version>.tar.gz   the same files, for unpacking by hand (then sudo deploy/install.sh)
    SHA256SUMS

The archive holds the tracked files of server/, cli/, web/, devices/, scripts/, docs/ and
deploy/ (with the systemd unit, install.sh and uninstall.sh), the Python packages pinned in
deploy/requirements.txt and build-requirements.txt (fetched into deploy/packages/ first unless
--no-fetch), README.md, and VERSION and BUILD-INFO files. Not the tests or the PDF manuals
(docs/devices/*/manuals). Python itself and linux-gpib are built on the Pi (docs/pi-setup.md);
install.sh checks for them.

Standard library only; runs on Windows, Linux or macOS (GitHub Actions uses ubuntu-latest).
"""

import argparse
import gzip
import hashlib
import io
import os
import re
import subprocess
import sys
import tarfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INCLUDE = ("server/", "cli/", "web/", "devices/", "scripts/", "docs/", "deploy/", "README.md", "LICENSE")
EXECUTABLE = re.compile(r"(\.sh|^deploy/[^/]+\.py|^scripts/[^/]+\.py)$")

HEADER = r"""#!/bin/sh
# GPIB-Remote server @VERSION@: self-extracting installer for the Raspberry Pi.
#
#   sudo sh @NAME@              install or update; enable start at boot; start the service
#   sudo sh @NAME@ --no-enable  don't enable start at boot
#   sudo sh @NAME@ --no-start   don't start or restart the service now
#   sh @NAME@ --extract DIR     only unpack (then: sudo DIR/@TOP@/deploy/install.sh)
#   sh @NAME@ --list | --version | --check
#
# Needs Python 3.9+ (/opt/gpibremote/python from deploy/build-python.sh, or the system python3)
# and, for real instruments, linux-gpib. Built from commit @COMMIT@.
set -eu
SKIP=@SKIP@
SUM=@SHA256@
TOP=@TOP@
payload() { tail -n +$SKIP "$0"; }
check() {
    if command -v sha256sum >/dev/null 2>&1; then
        [ "$(payload | sha256sum | cut -d' ' -f1)" = "$SUM" ] || { echo "installer is damaged (checksum mismatch); download it again" >&2; exit 1; }
    fi
}
case "${1:-}" in
    -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    --version) echo "@VERSION@"; exit 0 ;;
    --list) payload | tar tzf -; exit 0 ;;
    --check) check; echo "ok"; exit 0 ;;
    --extract)
        DIR=${2:?usage: --extract DIR}
        check
        mkdir -p "$DIR"
        payload | tar xzf - -C "$DIR"
        echo "unpacked to $DIR/$TOP; install with: sudo $DIR/$TOP/deploy/install.sh"
        exit 0 ;;
esac
[ "$(id -u)" -eq 0 ] || { echo "run with sudo: sudo sh $0 $*" >&2; exit 1; }
check
TMP=$(mktemp -d /tmp/gpibremote-install.XXXXXX)
trap 'rm -rf "$TMP"' EXIT
trap 'exit 130' INT TERM
payload | tar xzf - -C "$TMP"
sh "$TMP/$TOP/deploy/install.sh" "$@"
exit $?
"""


def git(*args):
    return subprocess.check_output(("git",) + args, cwd=ROOT).decode().strip()


def default_version():
    with open(os.path.join(ROOT, "server", "gpibremote", "__init__.py"), encoding="utf-8") as fh:
        base = re.search(r"^__version__\s*=\s*[\"']([^\"']+)", fh.read(), re.M).group(1)
    try:
        exact = git("tag", "--points-at", "HEAD")
        if "v" + base in exact.split():
            return base
        return "%s-g%s" % (base, git("rev-parse", "--short=7", "HEAD"))
    except (OSError, subprocess.CalledProcessError):
        return base


def tracked_files():
    """Files git tracks under INCLUDE, as they are in the working tree (paths with '/')."""
    out = subprocess.check_output(["git", "ls-files", "-z", "--"] + [p.rstrip("/") for p in INCLUDE], cwd=ROOT)
    files = [f for f in out.decode("utf-8").split("\0") if f]
    return sorted(f for f in files if os.path.isfile(os.path.join(ROOT, f)) and "__pycache__" not in f
                  and not f.lower().endswith(".pdf"))   # the instrument manuals stay in the repo


def packages(fetch):
    d = os.path.join(ROOT, "deploy", "packages")
    wanted = []
    for req in ("build-requirements.txt", "requirements.txt"):
        path = os.path.join(ROOT, "deploy", req)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                wanted += [line.split("#")[0].strip() for line in fh]
    wanted = [w for w in wanted if w]

    def missing():
        names = [n.lower().replace("_", "-") for n in (sorted(os.listdir(d)) if os.path.isdir(d) else [])]
        return [w for w in wanted
                if not any(n.startswith(w.split("==")[0].lower().replace("_", "-") + "-" + w.split("==")[1]) for n in names)]

    if missing() and fetch:
        print("fetching packages: %s" % ", ".join(missing()))
        subprocess.check_call([sys.executable, os.path.join(ROOT, "deploy", "fetch_packages.py")])
    if missing():
        sys.exit("deploy/packages lacks %s: run deploy/fetch_packages.py (or drop --no-fetch)" % ", ".join(missing()))
    have = sorted(os.listdir(d))
    return ["deploy/packages/" + n for n in have if os.path.isfile(os.path.join(d, n))]


def build(version, out_dir, fetch=True, mtime=None):
    top = "gpibremote-%s" % version
    try:
        commit = git("rev-parse", "HEAD")
        dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
    except (OSError, subprocess.CalledProcessError):
        commit, dirty = "unknown", True
    if mtime is None:   # the commit time, so the same commit gives the same archive
        if os.environ.get("SOURCE_DATE_EPOCH"):
            mtime = int(os.environ["SOURCE_DATE_EPOCH"])
        elif commit != "unknown":
            mtime = int(git("log", "-1", "--format=%ct"))
        else:
            mtime = int(time.time())

    files = tracked_files() + packages(fetch)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        def add_bytes(name, data, mode):
            ti = tarfile.TarInfo(top + "/" + name)
            ti.size, ti.mode, ti.mtime = len(data), mode, mtime
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = "root"
            tar.addfile(ti, io.BytesIO(data))

        dirs = set()
        for f in files:
            parts = f.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                dirs.add("/".join(parts[:i]))
        for dname in [""] + sorted(dirs):
            ti = tarfile.TarInfo(top + ("/" + dname if dname else ""))
            ti.type, ti.mode, ti.mtime = tarfile.DIRTYPE, 0o755, mtime
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = "root"
            tar.addfile(ti)
        for f in files:
            with open(os.path.join(ROOT, *f.split("/")), "rb") as fh:
                data = fh.read()
            text = not f.startswith("deploy/packages/") and b"\0" not in data[:8192]
            if text and b"\r\n" in data:
                data = data.replace(b"\r\n", b"\n")   # a Windows checkout; the Pi needs LF
            add_bytes(f, data, 0o755 if EXECUTABLE.search(f) else 0o644)
        info = "%s\ncommit %s%s\n" % (version, commit, " (with uncommitted changes)" if dirty else "")
        add_bytes("VERSION", version.encode() + b"\n", 0o644)
        add_bytes("BUILD-INFO", info.encode(), 0o644)
    gz = io.BytesIO()
    with gzip.GzipFile(fileobj=gz, mode="wb", compresslevel=9, mtime=mtime, filename="") as g:
        g.write(raw.getvalue())
    payload = gz.getvalue()

    name = top + ".run"
    head = HEADER
    for k, v in (("@VERSION@", version), ("@NAME@", name), ("@TOP@", top),
                 ("@COMMIT@", commit[:12] + ("+changes" if dirty else "")),
                 ("@SHA256@", hashlib.sha256(payload).hexdigest())):
        head = head.replace(k, v)
    head = head.replace("@SKIP@", str(head.count("\n") + 1))

    os.makedirs(out_dir, exist_ok=True)
    run_path = os.path.join(out_dir, name)
    tgz_path = os.path.join(out_dir, top + ".tar.gz")
    with open(run_path, "wb") as fh:
        fh.write(head.encode("ascii") + payload)
    os.chmod(run_path, 0o755)
    with open(tgz_path, "wb") as fh:
        fh.write(payload)
    sums = []
    for p in (run_path, tgz_path):
        with open(p, "rb") as fh:
            sums.append("%s  %s\n" % (hashlib.sha256(fh.read()).hexdigest(), os.path.basename(p)))
    with open(os.path.join(out_dir, "SHA256SUMS"), "w", newline="\n") as fh:
        fh.writelines(sums)
    return {"version": version, "run": run_path, "tar": tgz_path, "files": len(files) + 2,
            "size": os.path.getsize(run_path), "commit": commit, "dirty": dirty}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--version", help="version string (default: server version, plus -g<commit> unless HEAD is tagged v<version>)")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"), help="output folder (default dist/)")
    ap.add_argument("--no-fetch", action="store_true", help="don't download missing packages")
    args = ap.parse_args(argv)
    r = build(args.version or default_version(), args.out, fetch=not args.no_fetch)
    print("built %s (%d files, %d bytes, commit %s%s)" % (r["run"], r["files"], r["size"], r["commit"][:12],
                                                          ", uncommitted changes" if r["dirty"] else ""))
    print("      %s" % r["tar"])
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write("version=%s\nrun=%s\ntar=%s\n" % (r["version"], r["run"], r["tar"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
