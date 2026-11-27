"""Server configuration: YAML file (default /etc/gpibremote/server.yaml) plus command-line overrides."""

import copy
import os

import yaml

from .devicetypes import parse_seconds

APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULTS = {
    "listen": {"host": "0.0.0.0", "port": 9000},
    "boards": [{"board": 0, "backend": "linuxgpib"}],
    "data_dir": "/var/lib/gpibremote/data",
    "builtin_types": os.path.join(APP_DIR, "devices", "types"),
    "git": {"enabled": True, "remote": None, "branch": "device-data", "push_delay": 10},
    "locks": {"idle_timeout": "15min"},
    "log_level": "INFO",
}

# Simulated bench used by `--backend sim` when the config names no instruments.
DEFAULT_SIM = {5: "agilent.e3631a", 6: "hp.6632a", 22: "hp.3457a"}


def _merge(base, over):
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load(path=None, overrides=None):
    cfg = copy.deepcopy(DEFAULTS)
    if path:
        with open(path, encoding="utf-8") as fh:
            _merge(cfg, yaml.safe_load(fh) or {})
    _merge(cfg, overrides or {})
    cfg["git"]["push_delay"] = parse_seconds(cfg["git"].get("push_delay"), 10.0)
    cfg["locks"]["idle_timeout"] = parse_seconds(cfg["locks"].get("idle_timeout"), 900.0)
    return cfg
