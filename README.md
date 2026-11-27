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
cli/gpibctl/         command-line client
web/                 browser client (plain JavaScript, no build step), served on port 8080
deploy/              Python build script, offline packages, install script, systemd unit
devices/types/       built-in device types (YAML, one folder per type, one file per version)
scripts/             hardware checks (test_e3631a.py)
tests/               unittest suite (runs against the simulator)
```

## Quick use

From any PC with Python 3.9+ (no packages needed):

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

## Development

Needs Python 3.9+, PyYAML and git. Run the tests and a server with a simulated bench
(E3631A at 5, 6632A at 6, 3457A at 22), on any OS:

```
python -m venv .venv && .venv/bin/pip install pyyaml      # .venv\Scripts\pip on Windows
python -m unittest discover -s tests
cd server && python -m gpibremote --backend sim --data-dir ../data --port 9000   # web client on :8080
```

## Deploying to the Pi

```
git -C ~/GPIB-Remote pull                        # on the Pi
python deploy/fetch_packages.py                  # on a PC, then copy deploy/packages/ to the Pi
sudo ~/GPIB-Remote/deploy/install.sh             # on the Pi
python scripts/save_device_data.py               # on a PC: copy the Pi's types/instances history to GitHub
```
