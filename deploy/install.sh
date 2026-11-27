#!/bin/sh
# Install or update the GPIB-Remote server on the Pi. Run from a checkout of the repo:
#   sudo deploy/install.sh
# Needs: Python built by deploy/build-python.sh in /opt/gpibremote/python, linux-gpib installed.
# Python packages are installed offline from deploy/packages/ (fill it on a PC with
# deploy/fetch_packages.py and copy it over; pip on the Pi has no ssl to reach PyPI).
set -eu

SRC=$(cd "$(dirname "$0")/.." && pwd)
BASE=/opt/gpibremote
PY=$BASE/python/bin/python3
DATA=/var/lib/gpibremote

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
[ -x "$PY" ] || { echo "$PY missing: run deploy/build-python.sh first"; exit 1; }

getent group gpib >/dev/null || groupadd gpib
id gpibremote >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --no-create-home \
    --gid gpib --shell /usr/sbin/nologin gpibremote
mkdir -p "$BASE/app" "$DATA"
chown gpibremote:gpib "$DATA"

# Application files (copied, so the service doesn't run from someone's working tree).
rsync -a --delete --exclude .git --exclude .venv --exclude deploy/packages --exclude __pycache__ \
    "$SRC/" "$BASE/app/"

# Configuration: created once from the example, never overwritten.
mkdir -p /etc/gpibremote
[ -e /etc/gpibremote/server.yaml ] || install -m 644 "$SRC/deploy/server.yaml.example" /etc/gpibremote/server.yaml

# Virtual environment and packages.
[ -x "$BASE/venv/bin/python" ] || "$PY" -m venv "$BASE/venv"
if [ -s "$SRC/deploy/requirements.txt" ] && grep -qv '^\s*#' "$SRC/deploy/requirements.txt"; then
    "$BASE/venv/bin/pip" install --no-index --find-links "$SRC/deploy/packages" \
        --no-build-isolation -r "$SRC/deploy/requirements.txt"
fi

install -m 644 "$SRC/deploy/gpibremote.service" /etc/systemd/system/gpibremote.service
systemctl daemon-reload
systemctl enable gpibremote.service
systemctl restart gpibremote.service
sleep 2
systemctl --no-pager --lines=5 status gpibremote.service || true
