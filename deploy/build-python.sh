#!/bin/sh
# Build Python 3.11 for the GPIB-Remote server on the Pi (Raspbian Jessie, gcc 4.9).
# Built WITHOUT ssl: the system uses plain TCP only, and Jessie's OpenSSL 1.0.1 is too old anyway.
# Usage: build-python.sh /path/to/Python-3.11.x.tgz   (download the tarball on a PC and copy it over)
# Installs to /opt/gpibremote/python (system Python untouched). Takes ~1.5 h on a Pi 2.
set -eu

TARBALL=${1:?usage: build-python.sh Python-3.11.x.tgz}
PREFIX=${PREFIX:-/opt/gpibremote/python}
WORK=${WORK:-$HOME/build/python}

# Build dependencies (Jessie, via legacy.raspbian.org):
#   sudo apt-get install build-essential libffi-dev libbz2-dev liblzma-dev libreadline-dev \
#     libncursesw5-dev libsqlite3-dev libgdbm-dev uuid-dev zlib1g-dev

mkdir -p "$WORK"
cd "$WORK"
tar xzf "$TARBALL"
cd "$(basename "$TARBALL" .tgz)"

./configure --prefix="$PREFIX" --with-ensurepip=install
make -j4
sudo mkdir -p "$PREFIX"
sudo make install   # 'install' also creates python3 and pip3 links in $PREFIX/bin

"$PREFIX/bin/python3" - <<'EOF'
import sys, ctypes, asyncio, sqlite3, json
print("python", sys.version.split()[0], "ok: ctypes asyncio sqlite3 json")
try:
    import ssl
    print("ssl present (unexpected but harmless)")
except ImportError:
    print("ssl absent (expected)")
EOF
