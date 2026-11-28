#!/bin/sh
# Install or update the GPIB-Remote server on the Pi.
#
#   sudo sh gpibremote-<version>.run [options]    the installer built by CI (runs this script)
#   sudo deploy/install.sh [options]              from a checkout of the repository
#
# By default the service is enabled to start at boot and (re)started now.
#   --no-enable   don't enable start at boot (an existing setting is left as it is)
#   --no-start    don't start or restart the service now
#
# Installs to /opt/gpibremote (app/, venv/, uninstall.sh, VERSION); data in /var/lib/gpibremote;
# configuration in /etc/gpibremote/server.yaml (created once, never overwritten).
#
# Not included, because they are built on the Pi for the Pi (docs/pi-setup.md):
#   - Python 3.9 or later: /opt/gpibremote/python (deploy/build-python.sh), or the system
#     python3 if it is new enough, or PYTHON=/path/to/python3.
#   - linux-gpib (kernel module, libgpib, udev rules) for real instruments. Without it only the
#     simulated backend works.
set -eu

ENABLE=1
START=1
for a in "$@"; do
    case $a in
        --no-enable) ENABLE=0 ;;
        --no-start) START=0 ;;
        -h|--help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $a (see --help)" >&2; exit 2 ;;
    esac
done

SRC=$(cd "$(dirname "$0")/.." && pwd)
BASE=/opt/gpibremote
DATA=/var/lib/gpibremote
UNIT=/etc/systemd/system/gpibremote.service
VERSION=$(cat "$SRC/VERSION" 2>/dev/null || git -C "$SRC" describe --always --dirty 2>/dev/null || echo unknown)

say() { echo "gpibremote: $*"; }
die() { echo "gpibremote: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo"
[ -d "$SRC/server/gpibremote" ] || die "$SRC doesn't look like GPIB-Remote (no server/gpibremote)"
say "installing $VERSION from $SRC"

# ---- Prerequisites -------------------------------------------------------------------------
py_ok() { [ -x "$1" ] && "$1" -c 'import sys, ctypes, asyncio, venv; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; }
PY=${PYTHON:-}
if [ -z "$PY" ]; then
    for c in "$BASE/python/bin/python3" "$(command -v python3 || true)"; do
        if [ -n "$c" ] && py_ok "$c"; then PY=$c; break; fi
    done
fi
{ [ -n "$PY" ] && py_ok "$PY"; } || die "no Python 3.9+ with ctypes and venv found. Build one with deploy/build-python.sh (into $BASE/python) or set PYTHON=/path/to/python3"
say "python: $PY ($("$PY" -c 'import sys; print(sys.version.split()[0])'))"

if [ -e /usr/local/lib/libgpib.so.0 ] || [ -e /usr/lib/libgpib.so.0 ] || ldconfig -p 2>/dev/null | grep -q 'libgpib\.so'; then
    say "linux-gpib: found"
else
    say "WARNING: linux-gpib (libgpib) not found: real instruments won't work, the simulated backend will (docs/pi-setup.md)"
fi

HAVE_SYSTEMD=0
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then HAVE_SYSTEMD=1; fi

# ---- Service account and directories -------------------------------------------------------
getent group gpib >/dev/null || groupadd gpib
id gpibremote >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --no-create-home \
    --gid gpib --shell /usr/sbin/nologin gpibremote
mkdir -p "$BASE" "$DATA"
chown gpibremote:gpib "$DATA"

# ---- Application files ---------------------------------------------------------------------
# Copied (so the service never runs from someone's working tree) into app.new, then swapped in.
rm -rf "$BASE/app.new" "$BASE/app.old"
mkdir -p "$BASE/app.new"
for item in server cli web devices scripts docs deploy README.md LICENSE; do
    if [ -e "$SRC/$item" ]; then cp -R "$SRC/$item" "$BASE/app.new/"; fi
done
rm -rf "$BASE/app.new/deploy/packages"
find "$BASE/app.new" -name __pycache__ -type d -prune -exec rm -rf {} +
echo "$VERSION" > "$BASE/app.new/VERSION"
chown -R root:root "$BASE/app.new"
chmod -R u+rwX,go+rX,go-w "$BASE/app.new"
if [ -d "$BASE/app" ]; then mv "$BASE/app" "$BASE/app.old"; fi
mv "$BASE/app.new" "$BASE/app"
rm -rf "$BASE/app.old"
echo "$VERSION" > "$BASE/VERSION"
install -m 755 "$SRC/deploy/uninstall.sh" "$BASE/uninstall.sh"

# ---- Configuration: created once from the example, never overwritten -----------------------
mkdir -p /etc/gpibremote
if [ -e /etc/gpibremote/server.yaml ]; then
    say "keeping /etc/gpibremote/server.yaml"
else
    install -m 644 "$SRC/deploy/server.yaml.example" /etc/gpibremote/server.yaml
    say "created /etc/gpibremote/server.yaml"
fi

# ---- Virtual environment and Python packages (offline) -------------------------------------
if [ -e "$BASE/venv" ] && ! "$BASE/venv/bin/python" -c 'import sys' >/dev/null 2>&1; then
    rm -rf "$BASE/venv"    # its Python is gone
fi
[ -x "$BASE/venv/bin/python" ] || "$PY" -m venv "$BASE/venv"
if grep -qv '^[[:space:]]*\(#\|$\)' "$SRC/deploy/requirements.txt" 2>/dev/null; then
    if [ -n "$(ls "$SRC/deploy/packages" 2>/dev/null)" ]; then
        # The sdists are built here without network: that needs setuptools in the venv, which
        # venvs of Python 3.12+ don't have.
        if ! "$BASE/venv/bin/python" -c 'import setuptools' 2>/dev/null && [ -e "$SRC/deploy/build-requirements.txt" ]; then
            "$BASE/venv/bin/pip" install -q --disable-pip-version-check --no-index \
                --find-links "$SRC/deploy/packages" -r "$SRC/deploy/build-requirements.txt"
        fi
        "$BASE/venv/bin/pip" install -q --disable-pip-version-check --no-index \
            --find-links "$SRC/deploy/packages" --no-build-isolation -r "$SRC/deploy/requirements.txt"
    elif "$BASE/venv/bin/python" -c 'import yaml' 2>/dev/null; then
        say "deploy/packages is empty; keeping the packages already in $BASE/venv"
    else
        die "deploy/packages is empty: run deploy/fetch_packages.py on a PC and copy it over, or use the installer"
    fi
fi

# ---- Service --------------------------------------------------------------------------------
install -m 644 "$SRC/deploy/gpibremote.service" "$UNIT"
if [ "$HAVE_SYSTEMD" -eq 0 ]; then
    say "systemd isn't running here: installed $UNIT but can't enable or start it"
    exit 0
fi
systemctl daemon-reload
if [ "$ENABLE" -eq 1 ]; then
    systemctl enable gpibremote.service
    say "enabled: starts at boot"
else
    say "start at boot: $(systemctl is-enabled gpibremote.service 2>/dev/null || echo disabled) (left as it was)"
fi
if [ "$START" -eq 1 ]; then
    systemctl restart gpibremote.service
    # It may wait up to 30 s for /dev/gpib0; then give it a few seconds to load and report.
    i=0
    while [ $i -lt 40 ] && [ "$(systemctl is-active gpibremote.service 2>/dev/null)" = activating ]; do
        sleep 1; i=$((i + 1))
    done
    sleep 3
    systemctl --no-pager --lines=8 status gpibremote.service || true
    if [ "$(systemctl is-active gpibremote.service 2>/dev/null)" = active ]; then
        say "running $VERSION: TCP port 9000, web client http://$(hostname -I 2>/dev/null | cut -d' ' -f1):8080/"
    else
        die "the service didn't stay up; see the status above"
    fi
else
    if [ "$(systemctl is-active gpibremote.service 2>/dev/null)" = active ]; then
        say "not restarted (--no-start): the running service keeps the old version until: sudo systemctl restart gpibremote"
    else
        say "not started (--no-start): sudo systemctl start gpibremote"
    fi
fi
