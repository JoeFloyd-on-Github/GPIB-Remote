# Raspberry Pi setup (as built, 2026-11-26)

Host: `192.168.1.157`, user `pi` (SSH key login).

| Item | Value |
|---|---|
| Board | Raspberry Pi 2 Model B Rev 1.1 (armv7l, 4 cores, 1 GB) |
| OS | Raspbian 8 "Jessie", kernel `4.1.18-v7+ #846` (Feb 2016), kept by choice |
| Python | 3.4.2 (system) |
| GPIB adapter | NI GPIB-USB-HS, USB `3923:709b`, driver `ni_usb_gpib`, board type `ni_usb_b` |
| GPIB library | linux-gpib 4.3.6 (kernel + user), installed to `/usr/local` |
| Instruments | Agilent E3631A at primary address 5 (`HEWLETT-PACKARD,E3631A,0,1.6-5.0-1.0`) |

## Steps that were needed

1. **Expand the root filesystem.** It was 3.7 GB and 99% full on a 14.5 GB card.
   `sudo raspi-config --expand-rootfs`, reboot.
2. **apt sources.** `mirrordirector.raspbian.org` no longer serves Jessie. `/etc/apt/sources.list`
   now points at `http://legacy.raspbian.org/raspbian/` (original saved as `sources.list.orig`).
   `archive.raspberrypi.org/debian jessie` still works.
3. **Build tools:** `apt-get install bc flex bison autoconf automake libtool pkg-config python3-dev`.
4. **Kernel headers for 4.1.18-v7+ #846.** No package exists, and the firmware repo's history was
   rewritten so the bootloader changelog hash (`abf7e13...`) no longer resolves. Used instead:
   - Kernel source: `raspberrypi/linux` commit `bf0bc44d74d6afd714642f6052c011ceb43375df`
     (last rpi-4.1.y commit before the #846 build time), unpacked to `/usr/src/linux-src/linux`.
   - `.config` from `/proc/config.gz` (`sudo modprobe configs`).
   - `Module.symvers` from firmware commit `35282eba1adeed529bed32ba7f32f6b9361679af`
     (`extra/Module7.symvers`, build #845; symbol CRCs match well enough that modules load).
   - `echo + > .scmversion` so the release string is `4.1.18-v7+`, then `make modules_prepare`.
   - `/lib/modules/4.1.18-v7+/build` and `source` symlink to that tree.
5. **linux-gpib 4.3.6 kernel part** (`~/src/linux-gpib-4.3.6/linux-gpib-kernel-4.3.6`):
   the LPVO driver uses `usb_find_common_endpoints` (kernel 4.12+), so its line was removed from
   `drivers/gpib/Makefile`. Then `make && sudo make install && sudo depmod -a`.
6. **linux-gpib user part:** `./configure --sysconfdir=/etc --disable-documentation
   --disable-perl-binding --disable-tcl-binding --disable-guile-binding --disable-php-binding
   PYTHON=python3`, `make`, `sudo make install`, `sudo ldconfig`. Installs `libgpib.so`,
   `ibtest`, `ibterm`, `/usr/local/sbin/gpib_config`, udev rules and the Python 3.4 `gpib`/`Gpib` modules.
7. **`/etc/gpib.conf`:** interface minor 0, `board_type = "ni_usb_b"`, name `gpib0`, pad 0;
   device `e3631a` at pad 5. The stock example is kept as `/etc/gpib.conf.example`.
8. **Permissions:** group `gpib` created, `pi` added. The installed udev rules load the module,
   run `gpib_config` when the adapter appears and make `/dev/gpib0` `root:gpib 0660`.

Verified after a reboot: `/dev/gpib0` appears, and `pi` (no sudo) can query `*IDN?`.

## Server runtime (phase 0, 2026-11-27)

9. **Python 3.11.18** built from source by `deploy/build-python.sh` into `/opt/gpibremote/python`
   (tarball downloaded on the PC and copied over). Extra build deps: `libffi-dev libbz2-dev
   liblzma-dev libreadline-dev libncursesw5-dev libsqlite3-dev libgdbm-dev uuid-dev`.
   gcc 4.9.2 handles `-std=c11` fine. Took ~1 h 20 min. Not built, as intended: `_ssl`,
   `_hashlib` (hashlib uses its built-in implementations) and `_tkinter`.
10. **Packages offline:** `python deploy/fetch_packages.py` on the PC fills `deploy/packages/`
    (pure wheels or sdists, sha256-checked); `install.sh` installs them with
    `pip --no-index --find-links`. PyYAML 6.0.3 builds in pure-Python mode.
11. **Service:** `sudo deploy/install.sh` from a checkout (`~/GPIB-Remote`):
    - system user `gpibremote` (group `gpib`), app copied to `/opt/gpibremote/app`,
      venv in `/opt/gpibremote/venv`, data dir `/var/lib/gpibremote` (phase 1);
    - `gpibremote.service` enabled; it waits up to 30 s for `/dev/gpib0`, then listens on TCP 9000.
    - Since phase 3 it also serves the web client on HTTP port 8080 (`web:` in
      `/etc/gpibremote/server.yaml`; the defaults apply when that section is missing):
      open `http://192.168.1.157:8080/`. Plain HTTP and WebSocket, no TLS: LAN or VPN only.
    - Logs, including the audit log: `journalctl -u gpibremote`.

Verified: `*IDN?` from the PC through the service, and again after a reboot with no manual steps.
To update the server: `git -C ~/GPIB-Remote pull && sudo ~/GPIB-Remote/deploy/install.sh`.

The service has no GitHub access (no keys, no remote). Device data in `/var/lib/gpibremote/data`
is saved to the repo's `device-data` branch from a PC with `scripts/save_device_data.py`.

## Quick test

```
python3 -c "import Gpib; d=Gpib.Gpib(0,5); d.write('*IDN?'); print(d.read(100))"
```

`scripts/test_e3631a.py` does a fuller check (reads settings, and with `--write` sets and
restores a voltage while the output stays off).

## Caveats

- If the kernel is ever upgraded (`apt-get upgrade` of `raspberrypi-kernel`/`raspberrypi-bootloader`),
  the GPIB modules must be rebuilt against the new kernel. Consider `sudo apt-mark hold raspberrypi-bootloader`.
- Jessie's Python is 3.4; a newer Python is built without `ssl` (the system uses plain TCP, no TLS),
  so Python packages are copied from the PC rather than fetched with pip. See `docs/PLAN.md`.
