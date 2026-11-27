"""Run the server:  python -m gpibremote [--backend linuxgpib|sim] [--port 9000]"""

import argparse
import asyncio
import logging
import signal
import sys

from . import __version__
from .backends import open_backend
from .bus import Bus
from .protocol import Dispatcher
from .tcp import serve


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="gpibremote", description="GPIB-Remote server")
    ap.add_argument("--backend", choices=["linuxgpib", "sim"], default="linuxgpib")
    ap.add_argument("--board", type=int, default=0, help="GPIB board (minor) number")
    ap.add_argument("--host", default="0.0.0.0", help="listen address (plain TCP, LAN/VPN only)")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--log-level", default="INFO")
    return ap.parse_args(argv)


async def run(args):
    bus = Bus(open_backend(args.backend, args.board))
    dispatcher = Dispatcher({args.board: bus})
    server = await serve(dispatcher, args.host, args.port)
    logging.info("gpibremote %s listening on %s:%d (backend %s, board %d)",
                 __version__, args.host, args.port, args.backend, args.board)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, AttributeError):  # Windows
            pass
    try:
        await stop.wait()
    finally:
        logging.info("shutting down")
        server.close()
        await server.wait_closed()
        bus.close()


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
