"""gpibctl (phase 0): raw access through the server.

  gpibctl [--host H] [--port P] info
  gpibctl listeners
  gpibctl query 5 "*IDN?"
  gpibctl write 5 "VOLT 1.0"
  gpibctl read 5
  gpibctl poll 5 | clear 5 | local 5 | trigger 5
"""

import argparse
import json
import os
import sys

from . import Client, ServerError


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gpibctl", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("GPIBREMOTE_HOST", "localhost"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("GPIBREMOTE_PORT", "9000")))
    ap.add_argument("--board", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=3.0, help="GPIB timeout, seconds")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info")
    sub.add_parser("listeners")
    for name in ("query", "write"):
        p = sub.add_parser(name)
        p.add_argument("pad", type=int)
        p.add_argument("data")
    for name in ("read", "poll", "clear", "local", "trigger"):
        sub.add_parser(name).add_argument("pad", type=int)
    args = ap.parse_args(argv)

    client = Client(args.host, args.port)
    try:
        client.hello()
        common = {"board": args.board}
        if args.cmd == "info":
            print(json.dumps(client.request("server.info"), indent=2))
        elif args.cmd == "listeners":
            print(" ".join(str(p) for p in client.request("bus.listeners", **common)["pads"]))
        elif args.cmd == "query":
            print(client.request("raw.query", pad=args.pad, data=args.data, timeout=args.timeout, **common)["data"])
        elif args.cmd == "write":
            client.request("raw.write", pad=args.pad, data=args.data, timeout=args.timeout, **common)
        elif args.cmd == "read":
            print(client.request("raw.read", pad=args.pad, timeout=args.timeout, **common)["data"])
        elif args.cmd == "poll":
            print(client.request("bus.serial_poll", pad=args.pad, **common)["status"])
        else:
            client.request("bus." + args.cmd, pad=args.pad, **common)
    except ServerError as exc:
        print("error:", exc, file=sys.stderr)
        return 1
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
