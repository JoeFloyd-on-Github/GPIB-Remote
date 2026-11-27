# GPIB-Remote

Network server for GPIB instruments on a Raspberry Pi with an NI GPIB-USB-HS adapter.
Remote users control instruments from a CLI or a browser client, through device models
(menus and front panels) defined in versioned, human-readable YAML files.
Plain TCP (no TLS): for LAN or VPN use only.

- [docs/PLAN.md](docs/PLAN.md): design and implementation plan
- [docs/pi-setup.md](docs/pi-setup.md): how the Pi, linux-gpib and the server runtime were set up
- [docs/protocol.md](docs/protocol.md): the JSON Lines protocol on port 9000

## Status: phase 1 (core server)

The server runs on the Pi as a systemd service on TCP port 9000. It holds versioned device
types (YAML) and device instances, controls instruments through their type (settings with
limits, readouts polled for subscribers, actions), gives one client at a time a control lock,
and pushes events to clients. Raw GPIB access is still available. The full CLI (phase 2) and the
web client (phase 3) come next.

- [docs/device-type-format.md](docs/device-type-format.md): how to write a device type

## Layout

```
server/gpibremote/   server: backends/ (linux-gpib via ctypes, simulator), bus worker, protocol, TCP
cli/gpibctl/         command-line client
deploy/              Python build script, offline packages, install script, systemd unit
devices/types/       built-in device types (YAML, one folder per type, one file per version)
scripts/             hardware checks (test_e3631a.py)
tests/               unittest suite (runs against the simulator)
```

## Quick use

From any PC with Python 3.9+ (no packages needed):

```
cd cli
python -m gpibctl --host 192.168.1.157 query 5 "*IDN?"
python -m gpibctl --host 192.168.1.157 listeners
```

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
cd server && python -m gpibremote --backend sim --data-dir ../data --port 9000
```

## Deploying to the Pi

```
git -C ~/GPIB-Remote pull                        # on the Pi
python deploy/fetch_packages.py                  # on a PC, then copy deploy/packages/ to the Pi
sudo ~/GPIB-Remote/deploy/install.sh             # on the Pi
```
