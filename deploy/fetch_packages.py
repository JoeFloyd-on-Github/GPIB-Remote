#!/usr/bin/env python3
"""Run on a PC with internet access. Downloads the packages pinned in deploy/requirements.txt
into deploy/packages/ for an offline install on the Pi (whose Python has no ssl).

For each `name==version` it takes a pure-Python wheel (py3-none-any) if PyPI has one, otherwise
the source distribution. Nothing is built here. Then copy the folder to the Pi, e.g.:
    scp -r deploy/packages pi@192.168.1.157:GPIB-Remote/deploy/
"""

import hashlib
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def pick(files):
    wheels = [f for f in files if f["packagetype"] == "bdist_wheel" and f["filename"].endswith("-py3-none-any.whl")]
    sdists = [f for f in files if f["packagetype"] == "sdist"]
    return (wheels or sdists or [None])[0]


def main():
    out = os.path.join(HERE, "packages")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(HERE, "requirements.txt")) as fh:
        reqs = [line.split("#")[0].strip() for line in fh]
    for req in filter(None, reqs):
        m = re.match(r"^([A-Za-z0-9_.\-]+)==([A-Za-z0-9_.\-]+)$", req)
        if not m:
            sys.exit("requirements must be pinned as name==version: %r" % req)
        name, version = m.groups()
        with urllib.request.urlopen("https://pypi.org/pypi/%s/%s/json" % (name, version)) as r:
            info = json.load(r)
        f = pick(info["urls"])
        if f is None:
            sys.exit("%s: no pure wheel or sdist on PyPI" % req)
        path = os.path.join(out, f["filename"])
        with urllib.request.urlopen(f["url"]) as r:
            data = r.read()
        if hashlib.sha256(data).hexdigest() != f["digests"]["sha256"]:
            sys.exit("%s: sha256 mismatch" % f["filename"])
        with open(path, "wb") as fh:
            fh.write(data)
        print("%-40s %8d bytes  sha256 ok" % (f["filename"], len(data)))


if __name__ == "__main__":
    main()
