# GPIB-Remote

Network server for GPIB instruments on a Raspberry Pi with an NI GPIB-USB-HS adapter.
Remote users control instruments from a CLI (TCP port 9000) or a browser client, through
device models (menus and front panels) defined in versioned, human-readable YAML files.

- [docs/PLAN.md](docs/PLAN.md): design and implementation plan (draft, under review)
- [docs/pi-setup.md](docs/pi-setup.md): how the Pi and linux-gpib were set up
- [scripts/test_e3631a.py](scripts/test_e3631a.py): hardware smoke test for the Agilent E3631A at address 5
