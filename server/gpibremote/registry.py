"""Registry: device types and instances stored as YAML files in a git repository.

Layout of the data directory (its own git repo, branch `device-data`):
    types/<type>/<version>.yaml      types added at run time (built-ins live in the app)
    instances/<id>.yaml              device instances
Every change is a commit whose author is the client that made it. The server never talks to
GitHub: the history is copied off the Pi from a PC with scripts/save_device_data.py.
"""

import logging
import os
import re
import subprocess
import threading

import yaml

from .devicetypes import DeviceType, TypeError_, parse_seconds, version_key

log = logging.getLogger(__name__)
INSTANCE_ID = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,47}$")
SAFE = ("leave", "outputs_off")
POLL_MODES = ("subscribed", "always", "off")


class RegistryError(ValueError):
    def __init__(self, code, msg, errors=None):
        super().__init__(msg)
        self.code = code
        self.errors = errors or []


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


class Git:
    def __init__(self, root, branch="device-data", enabled=True):
        self.root = root
        self.branch = branch
        self.enabled = enabled
        self._lock = threading.Lock()

    def run(self, *args, check=True):
        p = subprocess.run(("git",) + args, cwd=self.root, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, universal_newlines=True)
        if check and p.returncode != 0:
            raise RuntimeError("git %s: %s" % (" ".join(args), p.stdout.strip()))
        return p.stdout

    def init(self):
        if not self.enabled or os.path.isdir(os.path.join(self.root, ".git")):
            return
        self.run("init", "-q")
        self.run("symbolic-ref", "HEAD", "refs/heads/" + self.branch)
        _write(os.path.join(self.root, "README.md"),
               "# GPIB-Remote device data\n\nDevice types and instances managed by the GPIB-Remote "
               "server. Every change is committed by the server; edit through a client so the "
               "server validates and versions it.\n")
        self.commit(["README.md"], "gpibremote", "Initialise device data")

    def commit(self, paths, who, message):
        if not self.enabled:
            return None
        with self._lock:
            self.run("add", "-A", "--", *paths)
            if not self.run("status", "--porcelain", "--", *paths).strip():
                return None
            name = re.sub(r"[<>\n]", "", who or "unknown")[:80]
            self.run("-c", "user.name=gpibremote", "-c", "user.email=gpibremote@localhost",
                     "commit", "-q", "--author", "%s <gpibremote@localhost>" % name, "-m", message)
            return self.run("rev-parse", "--short", "HEAD").strip()

    def history(self, path=None, limit=50):
        if not self.enabled:
            return []
        args = ["log", "-n", str(limit), "--format=%h%x09%an%x09%ai%x09%s"]  # %aI needs git 2.2; Jessie has 2.1
        if path:
            args += ["--", path]
        out = []
        for line in self.run(*args, check=False).splitlines():
            parts = line.split("\t", 3)
            if len(parts) == 4:
                out.append({"rev": parts[0], "author": parts[1], "date": parts[2], "message": parts[3]})
        return out


class Registry:
    def __init__(self, data_dir, builtin_dir, git_enabled=True):
        self.data_dir = data_dir
        self.builtin_dir = builtin_dir
        self.types = {}          # name -> {version: DeviceType}
        self.instances = {}      # id -> dict
        self._lock = threading.RLock()
        os.makedirs(data_dir, exist_ok=True)
        self.git = Git(data_dir, enabled=git_enabled)

    # -- loading ------------------------------------------------------------
    def load(self):
        self.git.init()
        problems = []
        for base, builtin in ((self.builtin_dir, True), (os.path.join(self.data_dir, "types"), False)):
            if not os.path.isdir(base):
                continue
            for tname in sorted(os.listdir(base)):
                tdir = os.path.join(base, tname)
                if not os.path.isdir(tdir):
                    continue
                for fn in sorted(os.listdir(tdir)):
                    if not fn.endswith(".yaml"):
                        continue
                    path = os.path.join(tdir, fn)
                    try:
                        t = DeviceType(_read(path), builtin=builtin)
                        if t.name != tname or t.version + ".yaml" != fn:
                            raise TypeError_(["file must be %s/%s.yaml" % (t.name, t.version)])
                        old = self.types.get(t.name, {}).get(t.version)
                        if old is not None and old.hash != t.hash:
                            raise TypeError_(["conflicts with the built-in %s" % t.ref])
                        self.types.setdefault(t.name, {})[t.version] = old or t
                    except TypeError_ as exc:
                        problems.append("%s: %s" % (path, exc))
        idir = os.path.join(self.data_dir, "instances")
        if os.path.isdir(idir):
            for fn in sorted(os.listdir(idir)):
                if fn.endswith(".yaml"):
                    path = os.path.join(idir, fn)
                    try:
                        raw = yaml.safe_load(_read(path))
                        errs = self.validate_instance(raw)
                        if errs:
                            raise RegistryError("invalid", "; ".join(errs))
                        self.instances[raw["id"]] = raw
                    except (yaml.YAMLError, RegistryError) as exc:
                        problems.append("%s: %s" % (path, exc))
        for p in problems:
            log.error("skipped %s", p)
        log.info("loaded %d types (%d versions), %d instances",
                 len(self.types), sum(len(v) for v in self.types.values()), len(self.instances))
        return problems

    # -- types --------------------------------------------------------------
    def get_type(self, name, version=None):
        if "@" in name and version is None:
            name, version = name.split("@", 1)
        versions = self.types.get(name)
        if not versions:
            raise RegistryError("not_found", "unknown type %r" % name)
        if version is None:
            version = max(versions, key=version_key)
        t = versions.get(version)
        if t is None:
            raise RegistryError("not_found", "type %s has no version %s" % (name, version))
        return t

    def list_types(self):
        out = []
        for name in sorted(self.types):
            versions = sorted(self.types[name], key=version_key)
            latest = self.types[name][versions[-1]]
            out.append({"type": name, "versions": versions, "latest": latest.version,
                        "title": latest.title, "verified": latest.verified, "builtin": latest.builtin})
        return out

    def put_type(self, text, who, message=None):
        """Store a new type version. Published versions are immutable."""
        t = DeviceType(text)
        with self._lock:
            old = self.types.get(t.name, {}).get(t.version)
            if old is not None:
                if old.hash == t.hash:
                    return t, False
                raise RegistryError("conflict", "%s already exists with different content; "
                                    "published versions can't change, so raise the version" % t.ref)
            rel = "types/%s/%s.yaml" % (t.name, t.version)
            _write(os.path.join(self.data_dir, rel), text)
            self.types.setdefault(t.name, {})[t.version] = t
            self.git.commit([rel], who, message or "Add type %s" % t.ref)
        return t, True

    # -- instances ----------------------------------------------------------
    def validate_instance(self, raw):
        e = []
        if not isinstance(raw, dict):
            return ["instance must be a mapping"]
        known = {"id", "name", "type", "address", "overrides", "safe_state", "poll_mode", "notes", "revision"}
        e += ["%s: unknown key" % k for k in raw if k not in known]
        if not isinstance(raw.get("id"), str) or not INSTANCE_ID.match(raw["id"]):
            e.append("id: lowercase letters, digits, '-' or '_', up to 48 characters")
        t = None
        ref = raw.get("type")
        if not isinstance(ref, str) or "@" not in ref:
            e.append("type: 'name@version', e.g. 'agilent.e3631a@1.0.0'")
        else:
            try:
                t = self.get_type(ref)
            except RegistryError as exc:
                e.append("type: %s" % exc)
        a = raw.get("address")
        if not isinstance(a, dict):
            e.append("address: {board, pad, sad}")
        else:
            for k, lo, hi in (("board", 0, 15), ("pad", 0, 30), ("sad", 0, 0x7E)):
                v = a.get(k, 0)
                if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
                    e.append("address.%s: integer %d..%d" % (k, lo, hi))
            sad = a.get("sad", 0)
            if isinstance(sad, int) and 0 < sad < 0x60:
                e.append("address.sad: 0 (none) or 0x60..0x7e")
            if a.get("pad") == 0 and a.get("board", 0) == 0:
                e.append("address.pad: 0 is the controller's own address")
        ov = raw.get("overrides") or {}
        if not isinstance(ov, dict):
            e.append("overrides: must be a mapping")
            ov = {}
        if t is not None:
            for key, lim in (ov.get("limits") or {}).items():
                try:
                    section, name, ch, d = t.item(key)
                    if section != "controls" or d.get("kind") != "number":
                        raise KeyError("not a numeric control")
                    if not (isinstance(lim, list) and len(lim) == 2
                            and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in lim)
                            and lim[0] <= lim[1]):
                        raise KeyError("must be [min, max]")
                except KeyError as exc:
                    e.append("overrides.limits.%s: %s" % (key, str(exc).strip("'\"")))
            for name, v in (ov.get("poll") or {}).items():
                if name not in t.readouts:
                    e.append("overrides.poll.%s: unknown readout" % name)
                elif v != "off":
                    try:
                        if parse_seconds(v) < 0.2:
                            e.append("overrides.poll.%s: at least 0.2 s" % name)
                    except ValueError as exc:
                        e.append("overrides.poll.%s: %s" % (name, exc))
        ss = raw.get("safe_state") or {}
        for k in ("on_server_stop", "on_client_disconnect"):
            v = ss.get(k, "leave")
            if v not in SAFE:
                e.append("safe_state.%s: one of %s" % (k, ", ".join(SAFE)))
            elif v == "outputs_off" and t is not None and not t.safe_off:
                e.append("safe_state.%s: type %s defines no safe_off commands" % (k, t.ref))
        if raw.get("poll_mode", "subscribed") not in POLL_MODES:
            e.append("poll_mode: one of %s" % ", ".join(POLL_MODES))
        return e

    def address_owner(self, board, pad, sad, exclude=None):
        for iid, raw in self.instances.items():
            a = raw["address"]
            if iid != exclude and (a.get("board", 0), a["pad"], a.get("sad", 0)) == (board, pad, sad):
                return iid
        return None

    def save_instance(self, raw, who, create):
        raw = dict(raw)
        with self._lock:
            exists = raw.get("id") in self.instances
            if create and exists:
                raise RegistryError("conflict", "instance %r already exists" % raw["id"])
            if not create and not exists:
                raise RegistryError("not_found", "no instance %r" % raw.get("id"))
            raw["revision"] = (self.instances[raw["id"]].get("revision", 0) + 1) if exists else 1
            errs = self.validate_instance(raw)
            if errs:
                raise RegistryError("invalid", "invalid instance", errs)
            a = raw["address"]
            a.setdefault("board", 0)
            a.setdefault("sad", 0)
            other = self.address_owner(a["board"], a["pad"], a["sad"], exclude=raw["id"])
            if other:
                raise RegistryError("conflict", "address %d:%d is already used by %r" % (a["board"], a["pad"], other))
            rel = "instances/%s.yaml" % raw["id"]
            _write(os.path.join(self.data_dir, rel),
                   yaml.safe_dump(raw, sort_keys=False, default_flow_style=False, allow_unicode=False))
            self.instances[raw["id"]] = raw
            self.git.commit([rel], who, "%s instance %s (rev %d)" % (
                "Create" if create else "Update", raw["id"], raw["revision"]))
        return raw

    def delete_instance(self, iid, who):
        with self._lock:
            if iid not in self.instances:
                raise RegistryError("not_found", "no instance %r" % iid)
            rel = "instances/%s.yaml" % iid
            os.remove(os.path.join(self.data_dir, rel))
            del self.instances[iid]
            self.git.commit([rel], who, "Delete instance %s" % iid)
