#!/usr/bin/env python3
"""Save the Pi's device data (types and instances, with their history) to GitHub.

Run on a PC that has a clone of GPIB-Remote, SSH access to the Pi, and push rights on GitHub.
The server on the Pi never contacts GitHub; this copies its data repository from the Pi into
the local branch `device-data` and pushes that branch. Also reports whether the Pi's server
config differs from deploy/server.yaml.example.

    python scripts/save_device_data.py [--pi pi@192.168.1.157] [--no-push]
"""

import argparse
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = "/var/lib/gpibremote/data"
BRANCH = "device-data"


def git(*args, capture=False):
    r = subprocess.run(("git",) + args, cwd=REPO, universal_newlines=True,
                       stdout=subprocess.PIPE if capture else None)
    if r.returncode != 0:
        sys.exit("git %s failed" % " ".join(args))
    return r.stdout if capture else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pi", default="pi@192.168.1.157", help="SSH destination of the Pi")
    ap.add_argument("--remote", default="origin", help="git remote to push to")
    ap.add_argument("--no-push", action="store_true", help="only fetch from the Pi")
    args = ap.parse_args()

    before = subprocess.run(["git", "rev-parse", "--verify", "-q", BRANCH], cwd=REPO,
                            stdout=subprocess.PIPE, universal_newlines=True).stdout.strip()
    # Fast-forward only: if the branch was changed somewhere else, this stops instead of losing history.
    git("fetch", "%s:%s" % (args.pi, DATA), "%s:%s" % (BRANCH, BRANCH))
    after = git("rev-parse", BRANCH, capture=True).strip()
    if before == after:
        print("device-data: no new changes on the Pi (%s)" % after[:7])
    else:
        print("device-data: new commits from the Pi:")
        print(git("log", "--format=  %h %ad %an: %s", "--date=short",
                  ("%s..%s" % (before, after)) if before else after, capture=True), end="")

    if not args.no_push:
        git("push", args.remote, "%s:%s" % (BRANCH, BRANCH))
        print("pushed %s to %s" % (BRANCH, args.remote))

    cfg = subprocess.run(["ssh", args.pi, "cat /etc/gpibremote/server.yaml"],
                         stdout=subprocess.PIPE, universal_newlines=True)
    if cfg.returncode == 0:
        with open(os.path.join(REPO, "deploy", "server.yaml.example"), encoding="utf-8") as fh:
            same = fh.read().strip() == cfg.stdout.strip()
        print("server config on the Pi: %s" % ("same as deploy/server.yaml.example" if same else
              "DIFFERS from deploy/server.yaml.example (copy the changes into the repo if they should be kept)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
