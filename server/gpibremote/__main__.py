"""Run the server:  python -m gpibremote [--config FILE] [--backend linuxgpib|sim] [--port 9000]"""

import argparse
import asyncio
import logging
import os
import signal
import sys

from . import __version__, config
from .backends import open_backend
from .backends.sim import SimBackend, TypeSimInstrument
from .bus import Bus
from .protocol import Dispatcher
from .registry import Registry
from .runtime import Hub, Manager
from .tcp import serve
from .web import serve_web

DEFAULT_CONFIG = "/etc/gpibremote/server.yaml"


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="gpibremote", description="GPIB-Remote server")
    ap.add_argument("--config", help="YAML config (default %s if it exists)" % DEFAULT_CONFIG)
    ap.add_argument("--backend", choices=["linuxgpib", "sim"], help="override every board's backend")
    ap.add_argument("--host", help="listen address (plain TCP, LAN/VPN only)")
    ap.add_argument("--port", type=int)
    ap.add_argument("--web-port", type=int, help="web client port (default 8080)")
    ap.add_argument("--no-web", action="store_true", help="don't serve the web client")
    ap.add_argument("--data-dir", help="where instances and added types are kept (a git repo)")
    ap.add_argument("--no-git", action="store_true", help="don't version the data directory")
    ap.add_argument("--log-level")
    return ap.parse_args(argv)


def build_config(args):
    path = args.config or (DEFAULT_CONFIG if os.path.exists(DEFAULT_CONFIG) else None)
    over = {}
    if args.host or args.port:
        over["listen"] = {k: v for k, v in (("host", args.host), ("port", args.port)) if v}
    if args.web_port or args.no_web:
        over["web"] = {k: v for k, v in (("port", args.web_port), ("enabled", False if args.no_web else None))
                       if v is not None}
    if args.data_dir:
        over["data_dir"] = args.data_dir
    if args.no_git:
        over["git"] = {"enabled": False}
    if args.log_level:
        over["log_level"] = args.log_level
    cfg = config.load(path, over)
    if args.backend:
        for b in cfg["boards"]:
            b["backend"] = args.backend
    return cfg, path


def make_bus(bcfg, registry):
    board = bcfg.get("board", 0)
    if bcfg.get("backend") == "sim":
        sims = bcfg.get("instruments") or config.DEFAULT_SIM
        return Bus(SimBackend(board, {int(p): TypeSimInstrument(registry.get_type(t)) for p, t in sims.items()}))
    opts = {k: v for k, v in bcfg.items() if k not in ("board", "backend")}
    return Bus(open_backend(bcfg.get("backend", "linuxgpib"), board, **opts))


async def run(cfg):
    registry = Registry(cfg["data_dir"], cfg["builtin_types"], cfg["git"].get("enabled", True))
    await asyncio.get_running_loop().run_in_executor(None, registry.load)
    buses = {b.get("board", 0): make_bus(b, registry) for b in cfg["boards"]}
    hub = Hub()
    manager = Manager(registry, buses, hub, cfg["locks"]["idle_timeout"])
    dispatcher = Dispatcher(buses, registry, manager, hub)
    server = await serve(dispatcher, cfg["listen"]["host"], cfg["listen"]["port"], hub, manager)
    web = None
    if cfg["web"].get("enabled", True):
        w = cfg["web"]
        web = await serve_web(dispatcher, w["host"], w["port"], w["root"], hub, manager,
                              w.get("allowed_origins") or ())
        logging.info("web client on http://%s:%d/ (files %s)", w["host"], w["port"], w["root"])
    manager.start()
    logging.info("gpibremote %s listening on %s:%d; boards %s; data %s", __version__,
                 cfg["listen"]["host"], cfg["listen"]["port"],
                 ", ".join("%d=%s" % (b.get("board", 0), b.get("backend")) for b in cfg["boards"]),
                 cfg["data_dir"])
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
        if web is not None:
            web.close()
        await manager.shutdown()
        for bus in buses.values():
            bus.close()


def main(argv=None):
    args = parse_args(argv)
    cfg, path = build_config(args)
    logging.basicConfig(level=str(cfg["log_level"]).upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.info("config: %s", path or "built-in defaults")
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
