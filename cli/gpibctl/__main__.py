"""gpibctl: command-line client for the GPIB-Remote server. `gpibctl -h` lists the commands."""

import os
import sys

if not __package__:  # run as `python cli/gpibctl ...`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "gpibctl"

from gpibctl import commands  # noqa: E402
from gpibctl.api import Bench  # noqa: E402


def main(argv=None, out=None, inp=None):
    parser = commands.build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            print("try `gpibctl -h`", file=sys.stderr)
            return 2
        return exc.code or 0

    def connect():
        return Bench(args.host, args.port, client="gpibctl")

    ctx = commands.Context(connect, out=out, inp=inp)
    try:
        return commands.run(ctx, args)
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return 1
        return exc.code or 0
    except OSError as exc:
        print("cannot reach the server at %s:%d: %s" % (args.host, args.port, exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        ctx.close()


if __name__ == "__main__":
    sys.exit(main())
