# GPIB-Remote

Network server for GPIB instruments on a Raspberry Pi with an NI GPIB-USB-HS adapter.
Remote users control instruments from a CLI or a browser client, through device models
(menus and front panels) defined in versioned, human-readable YAML files.
Plain TCP (no TLS): for LAN or VPN use only.

- [docs/PLAN.md](docs/PLAN.md): design and implementation plan
- [docs/pi-setup.md](docs/pi-setup.md): how the Pi, linux-gpib and the server runtime were set up
- [docs/protocol.md](docs/protocol.md): the JSON Lines protocol on port 9000

## Status: phase 0 (platform)

The server runs on the Pi as a systemd service on TCP port 9000 and offers raw GPIB access
(query/write/read, serial poll, clear, local, trigger, listener scan). Device types, instances,
locks, the full CLI and the web client come in later phases.

## Layout

```
server/gpibremote/   server: backends/ (linux-gpib via ctypes, simulator), bus worker, protocol, TCP
cli/gpibctl/         command-line client
deploy/              Python build script, offline packages, install script, systemd unit
devices/types/       built-in device types (phase 1)
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

Or with no client at all: `nc 192.168.1.157 9000` and type
`{"id":1,"op":"raw.query","pad":5,"data":"*IDN?"}`.

## Development

Run the server against the simulator and the tests, on any OS:

```
python -m unittest discover -s tests
cd server && python -m gpibremote --backend sim --port 9000
```

## Deploying to the Pi

```
git -C ~/GPIB-Remote pull                        # on the Pi
python deploy/fetch_packages.py                  # on a PC, then copy deploy/packages/ to the Pi
sudo ~/GPIB-Remote/deploy/install.sh             # on the Pi
```
