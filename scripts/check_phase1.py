#!/usr/bin/env python3
"""Phase 1 hardware check against a running server with the E3631A at address 5.

Creates instance 'bench-psu' if missing, then: identify, refresh, subscribe to readings, lock,
set/readback/restore a voltage, and confirms a second client is refused while locked.
The output is never switched on; the script stops if it finds it on.

    python scripts/check_phase1.py --host 192.168.1.157
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cli"))
from gpibctl import Client, ServerError  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.1.157")
    ap.add_argument("--port", type=int, default=9000)
    args = ap.parse_args()

    a = Client(args.host, args.port)
    a.hello("check_phase1", user="check-a")
    b = Client(args.host, args.port)
    b.hello("check_phase1", user="check-b")

    names = [t["type"] for t in a.request("types.list")["types"]]
    print("types:", ", ".join(names))
    ids = [i["id"] for i in a.request("instances.list")["instances"]]
    if "bench-psu" not in ids:
        r = a.request("instances.create", instance={
            "id": "bench-psu", "name": "Bench PSU (E3631A)", "type": "agilent.e3631a@1.0.0",
            "address": {"board": 0, "pad": 5}, "notes": "Bench supply used for development"})
        print("created bench-psu; identify:", r.get("identify"))
    else:
        print("identify:", a.request("instances.identify", instance="bench-psu"))

    state = a.request("dev.refresh", instance="bench-psu")["state"]
    for k in ("output", "tracking", "P6V.voltage", "P6V.current", "P25V.voltage", "N25V.voltage"):
        print("  %-14s %r (%s)" % (k, state[k]["value"], state[k]["source"]))
    if state["output"]["value"]:
        print("output is ON: stopping without changing anything")
        return 1

    b.request("subscribe", instance="bench-psu")
    ev = b.wait_event(lambda e: e.get("event") == "reading", timeout=15)
    print("reading event:", ev["key"], ev["value"])

    a.request("dev.lock", instance="bench-psu")
    old = state["P6V.voltage"]["value"]
    r = a.request("dev.set", instance="bench-psu", key="P6V.voltage", value=1.234)
    print("set P6V.voltage 1.234 ->", r["value"], r["source"])
    ev = b.wait_event(lambda e: e.get("event") == "state" and e["key"] == "P6V.voltage")
    print("observer saw state event:", ev["value"], "by", ev["by"])
    try:
        b.request("dev.set", instance="bench-psu", key="P6V.voltage", value=2)
        print("FAIL: second client was allowed to set")
        return 1
    except ServerError as exc:
        print("second client refused:", exc.code, "-", exc.error.get("msg"))
    try:
        a.request("dev.set", instance="bench-psu", key="P6V.voltage", value=7)
        print("FAIL: out-of-range value accepted")
        return 1
    except ServerError as exc:
        print("out of range refused:", exc.error.get("msg"))
    r = a.request("dev.set", instance="bench-psu", key="P6V.voltage", value=old)
    print("restored P6V.voltage ->", r["value"])
    a.request("dev.unlock", instance="bench-psu")
    a.request("bus.local", pad=5)
    print("output still", a.request("dev.get", instance="bench-psu", key="output")["value"])
    a.close()
    b.close()
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
