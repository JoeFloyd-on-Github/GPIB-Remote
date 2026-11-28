# GPIB-Remote

Network server for GPIB instruments on a Raspberry Pi with an NI GPIB-USB-HS adapter.
Remote users control instruments from a CLI or a browser client, through device models
(menus and front panels) defined in versioned, human-readable YAML files.
Plain TCP (no TLS): for LAN or VPN use only.

- [docs/PLAN.md](docs/PLAN.md): design and implementation plan
- [docs/pi-setup.md](docs/pi-setup.md): how the Pi, linux-gpib and the server runtime were set up
- [docs/protocol.md](docs/protocol.md): the JSON protocol (TCP port 9000, WebSocket on 8080)

## Status: phase 3 (web client)

The server runs on the Pi as a systemd service on TCP port 9000. It holds versioned device
types (YAML) and device instances, controls instruments through their type (settings with
limits, readouts polled for subscribers, actions), gives one client at a time a control lock,
and pushes events to clients. Raw GPIB access is still available. The `gpibctl` CLI does all of this from
Windows, Linux or the Pi, including a menu mode and an interactive shell. The web client at
**http://192.168.1.157:8080/** does the same from a browser, with live charts of the readings.
Traces from scopes and analyzers (phase 4) come next.

- [docs/device-type-format.md](docs/device-type-format.md): how to write a device type

## Layout

```
server/gpibremote/   server: backends/ (linux-gpib via ctypes, simulator), bus worker, protocol, TCP
cli/gpibctl/         command-line client, and the test-script API (api.py); zipped at /download/gpibctl.zip
web/                 browser client (plain JavaScript, no build step), served on port 8080
deploy/              installer builder, install/uninstall scripts, systemd unit, Python build, offline packages
.github/workflows/    CI: tests, Pi installer build, install test, releases
devices/types/       built-in device types (YAML, one folder per type, one file per version)
scripts/             hardware checks (test_e3631a.py), nickel_plating.py (plating experiment)
tests/               unittest suite (runs against the simulator)
```

## Quick use

From any PC with Python 3.9+ (no packages needed). No copy of the repository? The web client's
**Get CLI** link (or http://192.168.1.157:8080/download/gpibctl.zip) downloads the CLI, the
test-script API, its docs and the example scripts as a zip, built from the server's own copy:


```
python cli/gpibctl --host 192.168.1.157 config      # remember the server (~/.gpibctl.ini)
python cli/gpibctl ls                                # instruments, types, lock state
python cli/gpibctl state bench-psu -r                # every setting and reading, read fresh
python cli/gpibctl set bench-psu P6V.voltage 5 P6V.current 0.2
python cli/gpibctl get bench-psu P6V.v_meas
python cli/gpibctl watch bench-psu                   # live readings (Ctrl-C to stop)
python cli/gpibctl menu bench-psu                    # menu-driven control, with the lock
python cli/gpibctl shell                             # one connection; `lock` lasts until you quit
python cli/gpibctl type show agilent.e3631a          # what a type offers
python cli/gpibctl inst create psu2 hp.6632a 6       # add an instrument
python cli/gpibctl query 5 "*IDN?"                   # raw GPIB
python cli/gpibctl -h                                # everything else
```

On the Pi: `/opt/gpibremote/venv/bin/python /opt/gpibremote/app/cli/gpibctl ls`.
`--host`/`--port`, then `GPIBREMOTE_HOST`/`GPIBREMOTE_PORT`, then `~/.gpibctl.ini` choose the server.

Or with no client at all: `nc 192.168.1.157 9000` and type, one per line:

```
{"id":1,"op":"hello","user":"me"}
{"id":2,"op":"instances.list"}
{"id":3,"op":"subscribe","instance":"bench-psu"}
{"id":4,"op":"dev.set","instance":"bench-psu","key":"P6V.voltage","value":1.5}
```

## Test scripts

`cli/gpibctl/api.py` is a Python API for experiment scripts (docs/api.md): scan the bus, give
instruments handles, configure them, lock them, and use them through standard supply, meter and
thermometer roles, or raw GPIB. The `gpibctl` CLI itself is built on it.

```python
from gpibctl.api import Bench
with Bench("192.168.1.157") as bench:
    dmm = bench.meter("dmm-ohms")
    dmm.configure("ohm4", range=30, nplc=100, offset_comp=True)
    print(dmm.measure(10).mean)
```

`scripts/nickel_plating.py` is a guided bright-nickel plating experiment built on it:

```
python scripts/nickel_plating.py --setup      # scan the bus, assign instruments to roles (plating-bench.json)
python scripts/nickel_plating.py              # calibrate, measure, plate with live logging, measure, report
```

Roles: supply, ohmmeter (4-wire Kelvin probe), thermometer (thermometer role, or a DMM on a type K
thermocouple), and optionally a shunt DMM and a cell DMM. Steps: instrument check and locks;
calibration (self-cal, probe zero, reference resistor, thermometer offset, current zero); sample
and initial resistance at several probe positions; bath temperature wait and current choice;
plating with time-coded CSV logging and alarms (output off at the end, on Ctrl-C or on error);
final weight and resistance; a report comparing Faraday and 2D/1D resistance predictions with
the measurements.

## Development

Needs Python 3.9+, PyYAML and git. Run the tests and a server with a simulated bench
(E3631A at 5, 6632A at 6, 3457A at 22), on any OS:

```
python -m venv .venv && .venv/bin/pip install pyyaml      # .venv\Scripts\pip on Windows
python -m unittest discover -s tests
cd server && python -m gpibremote --backend sim --data-dir ../data --port 9000   # web client on :8080
```

## Deploying to the Pi

GitHub Actions (`.github/workflows/installer.yml`) runs the tests on every push, builds the
installer `gpibremote-<version>.run`, installs it on an Ubuntu runner under systemd with a
simulated bench (enabled, started, web client and CLI checked, updated, uninstalled) and keeps it
as the run's `pi-installer` artifact. Pushing a tag `v<version>` (the version in
`server/gpibremote/__init__.py`) also publishes it as a GitHub release.

The installer holds everything from this repository the Pi needs (server, web client, CLI,
device types, scripts, docs, the systemd unit, install and uninstall scripts) plus the Python
packages, so it installs offline. Python 3.9+ and linux-gpib are built on the Pi once
(docs/pi-setup.md); the installer checks for them.

```
scp gpibremote-0.3.0.run pi@192.168.1.157:          # from a PC
sudo sh gpibremote-0.3.0.run                         # on the Pi: install, enable at boot, start
sudo sh gpibremote-0.3.0.run --no-enable --no-start  # install only
sh gpibremote-0.3.0.run --help                       # also --extract DIR, --list, --check
sudo /opt/gpibremote/uninstall.sh                    # remove (keeps data and configuration)
python deploy/build_installer.py                     # build one locally, into dist/
```

From a checkout instead:

```
git -C ~/GPIB-Remote pull                        # on the Pi
python deploy/fetch_packages.py                  # on a PC, then copy deploy/packages/ to the Pi
sudo ~/GPIB-Remote/deploy/install.sh             # on the Pi (same options)
python scripts/save_device_data.py               # on a PC: copy the Pi's types/instances history to GitHub
```
