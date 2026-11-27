#!/usr/bin/env python3
"""Smoke test for the Agilent E3631A over linux-gpib (Python 3.4+).

Reads identity, output state and per-channel settings/measurements.
With --write, sets P6V to 1.234 V and back to 0 V; refuses if the output is on.
"""
import argparse
import sys

import Gpib


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", type=int, default=0)
    ap.add_argument("--pad", type=int, default=5)
    ap.add_argument("--write", action="store_true", help="set/readback test (output must be off)")
    args = ap.parse_args()

    dev = Gpib.Gpib(args.board, args.pad)

    def query(cmd):
        dev.write(cmd)
        return dev.read(256).decode().strip()

    print("IDN   ", query("*IDN?"))
    output = query("OUTP?")
    print("OUTPUT", "on" if output == "1" else "off")
    for ch in ("P6V", "P25V", "N25V"):
        dev.write("INST:SEL " + ch)
        print("%-5s set %s V, %s A; measured %s V, %s A" % (
            ch, query("VOLT?"), query("CURR?"), query("MEAS:VOLT?"), query("MEAS:CURR?")))

    if args.write:
        if output != "0":
            print("output is on; skipping write test")
            return 1
        dev.write("INST:SEL P6V")
        dev.write("VOLT 1.234")
        readback = query("VOLT?")
        dev.write("VOLT 0")
        print("write test: readback", readback, "->", "OK" if abs(float(readback) - 1.234) < 1e-6 else "FAIL")

    print("ERROR ", query("SYST:ERR?"))
    dev.ibloc()  # GPIB go-to-local; SYST:LOC is RS-232 only on the E3631A
    return 0


if __name__ == "__main__":
    sys.exit(main())
