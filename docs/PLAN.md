# GPIB-Remote: implementation plan (draft for review)

Status 2026-11-26: the Pi talks to the E3631A over the NI GPIB-USB-HS (see `pi-setup.md`).
Nothing below is built yet. Items marked **[decide]** need your answer before or during the phase named.

---

## 1. Goals

- A server on the Pi owns the GPIB bus and exposes instruments to remote users.
- Clients: a CLI (raw TCP, port 9000) and a browser client served by the server, with the same capabilities.
- Users operate instruments through a model of their controls and displays (menus in the CLI,
  a front panel in the browser), not by typing GPIB commands. Raw commands remain available.
- Device knowledge is data, not code: human-readable, editable, versioned files.
- The server ships a library of common device types; users create instances (type + address + config)
  and can add new types at runtime. Any client can then fetch and use them.

## 2. Key decisions (proposed)

### 2.1 Where device knowledge lives: the server
The server holds every **device type** and **device instance**. Clients are generic: they download a
type's model and render it. Reasons:
- One source of truth; a type added by one user is immediately usable by everyone, with no client update.
- The server must understand the type anyway to poll readings, enforce limits and serialise bus access.
- Clients stay thin enough to run anywhere (CLI on Windows/Linux, any browser).

Clients may cache types by `name@version` + content hash.

### 2.2 Runtime on the Jessie Pi
Python 3.4 cannot run modern asyncio code (`async`/`await` arrived in 3.5) or current libraries. Plan:
- Build **Python 3.11** from source into `/opt/gpibremote/` (does not touch the system Python),
  **without the `ssl` module**. The system uses no TLS anywhere (see 2.4), so Jessie's OpenSSL 1.0.1k
  doesn't matter and no newer OpenSSL is built. Estimated ~1.5 h of compile on the Pi 2, unattended.
  Fallback if gcc 4.9 can't build 3.11: Python 3.9.
- Access GPIB through our own small **ctypes wrapper over `/usr/local/lib/libgpib.so`**, so we don't
  depend on the 3.4-only `gpib` extension.
- Dependencies: the standard library plus pure-Python packages only (no wheels exist for this
  platform). Without `ssl`, pip on the Pi can't reach PyPI, so packages are downloaded on the PC
  (`pip download --no-binary :all:` or pure-Python wheels), copied to the Pi and installed from
  local files (`pip install --no-index --find-links`). Candidates: `PyYAML` (pure mode) and a pure-Python
  HTTP/WebSocket stack (e.g. `aiohttp` with `AIOHTTP_NO_EXTENSIONS=1`), or a small stdlib-only
  HTTP + WebSocket implementation on asyncio. **[decide in phase 0 after a build test]**

### 2.3 Development environment
- Develop here on Windows (or any Linux box) against a **simulated GPIB backend**. Simulated
  instruments are driven by the same type files (they keep state for set/query pairs), so most of
  the server, CLI and web client can be built and tested without hardware.
- Deploy to the Pi by `git pull` from this repo (or `scp` during development) and test against the E3631A.
- Tests: `pytest` on the dev machine with the simulator; a hardware test set run on the Pi.

### 2.4 Ports and protocol
**Plain TCP, no SSL/TLS anywhere** (decided). All traffic is cleartext.
- **9000/tcp:** raw TCP, newline-delimited JSON (one JSON object per line). Easy for a CLI, `nc`, or scripts.
- **8080/tcp:** plain `http://`: serves the web client, plus a plain `ws://` WebSocket at `/ws` carrying
  **the same JSON messages** as port 9000. One protocol handler, two transports, so CLI and web have identical power.
- A small REST read-only mirror (`GET /api/types`, `/api/instances/...`) for scripts is optional.

## 3. Architecture

```
            +-------------------- Pi: gpibremote server (asyncio) ----------------------+
 CLI  --TCP 9000-->  Session  \                                                         |
 Web  --WS 8080 -->  Session  --> Dispatcher --> Registry (types, instances; YAML+git)  |
 Browser <--HTTP 8080-- static web client         |                                      |
                                                  v                                      |
                               Instance runtime (state cache, pollers, limits, locks)    |
                                                  |                                      |
                                                  v                                      |
                               Bus worker (one thread per board, priority queue)         |
                                                  |                                      |
                               Backend: linux-gpib (ctypes)  |  simulator                |
            +---------------------------------------------------------------------------+
```

- **Bus worker:** GPIB is half-duplex and slow; every bus operation goes through one queue per board
  (user commands at high priority, polling at low). Write+read pairs are atomic. Timeouts, `ibclr`
  and serial poll handled here. A bad device can't hang the server: per-operation timeout, then device clear.
- **Instance runtime:** for each instance, a cache of the last known value of every control and readout
  (with timestamp), the pollers (per-readout intervals), limit checks, and the control lock.
- **Sessions:** a connected client. Subscribes to instances and receives change/reading events.
- **Locking (decided: from phase 1, no access tokens):** many observers, one controller per instance.
  A client takes a lease (`dev.lock`) before changing settings, running actions or sending raw commands
  to that instrument; it expires after N minutes idle or on disconnect. Any client can see who holds a
  lock and can force-break it (with a reason that is logged and sent to the holder). Clients identify
  themselves in `hello` (user and host name) for display and the audit log; nothing is authenticated.
  Bus-wide operations (`bus.scan`, interface clear) need every instance on that board to be unlocked or held by the caller.

## 4. Data: device types and instances

All stored as YAML under a data directory that is itself a git repository (the server commits every
change with the client's name and a message). Built-in types live in this repo under `devices/types/`
on `main` and are copied/merged into the data dir on install. The data dir is pushed to this repo
(see 4.2).

### 4.1 Device type (example, abridged)
`devices/types/agilent/e3631a/1.0.0.yaml`
```yaml
schema: 1
type: agilent.e3631a
version: 1.0.0
title: Agilent / HP E3631A Triple Output DC Power Supply
match: { idn: '^(HEWLETT-PACKARD|Agilent Technologies),E3631A,' }
connection: { eos: "\n", timeout: 3s, local_cmd: gpib_local }   # SYST:LOC is RS-232 only
error_query: { cmd: 'SYST:ERR?', none: '^\+0,' }
init: ['*CLS']
channels:
  P6V:  { select: 'INST:SEL P6V',  vmax: 6.18,  imax: 5.15 }
  P25V: { select: 'INST:SEL P25V', vmax: 25.75, imax: 1.03 }
  N25V: { select: 'INST:SEL N25V', vmax: -25.75, imax: 1.03 }
controls:
  output:   { kind: bool, label: Output, set: 'OUTP {v:onoff}', get: 'OUTP?' }
  tracking: { kind: bool, label: Track +/-25V, set: 'OUTP:TRAC {v:onoff}', get: 'OUTP:TRAC?' }
  voltage:  { kind: number, per_channel: true, unit: V, step: 0.001,
              range: [0, '{ch.vmax}'], set: 'VOLT {v}', get: 'VOLT?' }
  current:  { kind: number, per_channel: true, unit: A, step: 0.001,
              range: [0, '{ch.imax}'], set: 'CURR {v}', get: 'CURR?' }
readouts:
  v_meas: { per_channel: true, unit: V, query: 'MEAS:VOLT?', poll: 2s }
  i_meas: { per_channel: true, unit: A, query: 'MEAS:CURR?', poll: 2s }
actions:
  reset: { label: Reset, cmds: ['*RST'], confirm: true }
menu:            # CLI menu tree
  - Output: [output, tracking]
  - '{channel}': [voltage, current, v_meas, i_meas]
panel:           # front-panel model for the web client
  size: [480, 200]
  elements:
    - { display: vfd, x: 20, y: 20, w: 260, h: 60, shows: [v_meas, i_meas], channel: selected }
    - { buttons: channel_select, x: 300, y: 20 }
    - { knob: voltage_or_current, x: 380, y: 110 }
    - { switch: output, x: 300, y: 150 }
```
Field details go in `docs/device-type-format.md` together with a JSON Schema used for validation.
Constructs needed: per-channel controls with a select command; number/enum/bool/text/action kinds;
value formatting and response parsing (number, regex, enum map, bit fields for status bytes);
dependencies (`enabled_if`, `range` from another control); poll intervals; safety limits;
init/shutdown sequences; error query.

The instrument list (4.4) adds requirements beyond SCPI power supplies:
- **Pre-SCPI instruments** (3455A, 3456A, 3457A, 6632A, 5334B) use terse HP-IB program codes
  (e.g. 6632A `VSET 5;ISET 0.1;OUT 1`, 3456A `F1R3T1`). Templates must allow any command string,
  multi-command strings, and instruments whose state can't be queried: the server then keeps the
  last value it set (marked `assumed`) instead of reading it back.
- **Talk-only readings:** several of these return a reading just by being addressed to talk, with no
  query. A readout can be `read: true` (no write) and can be triggered (`trigger: gpib_get`).
- **Identity without `*IDN?`:** 3455A/3456A have no ID query and the older HP-IB parsers can
  misread stray characters as program codes, so a type declares its own safe identity probe
  (`ID?` for 3457A/6632A) or `probe: none` (identified by the user, not by the server).
- **Status and SRQ:** serial-poll status bytes with named bits (data ready, error, overload) and
  waiting on SRQ for slow measurements instead of polling.
- **Termination quirks:** per-type EOS/EOI settings, timeouts and inter-command delays.
- **Traces and waveforms** (TDS684B, TDS754C, 8713B/8714ES/8714ET, 3589A): a `trace` readout kind
  returning arrays plus scaling, from IEEE 488.2 definite-length blocks (`#4...`), ASCII lists, or Tek
  `CURVE?` with the `WFMPRE?` preamble; x-axis from frequency start/stop or time base.
  Each format is a named built-in parser the type refers to.
- **Softkey-driven front panels** (network/spectrum analyzers, scopes): the type's menu tree mirrors
  the instrument's hardkeys/softkeys, so the CLI menu and web panel follow the manual's structure.

Escape hatch: if a type needs logic the YAML can't express, it may name a small Python hook file
stored and versioned next to it (server-side only). Used sparingly; YAML stays the source of truth.

### 4.2 Versioning
- A type is identified by `type@version` (semver). A published version is **immutable**; edits produce a
  new version (patch for fixes, minor for added controls, major for incompatible changes).
- Each file also has a content hash, so clients can cache and detect changes.
- Instances pin a type version. The server reports when a newer compatible version exists; upgrading is an explicit step.
- History: every change to types or instances is a git commit in the data directory (who, when, why).
- **Pushed to this repo (decided)**, on a dedicated branch `device-data`, so server commits never
  collide with code on `main`. The Pi pushes over SSH with a deploy key that has write access to
  this repo only (the Pi's git doesn't depend on our ssl-less Python). Pushes are batched (a few
  seconds after the last change) and retried when the network is down; the local repo stays authoritative.
- Promoting a user-made type to a built-in is a normal merge from `device-data` into `devices/types/` on `main`.

### 4.3 Device instance
`data/instances/bench-psu-1.yaml`
```yaml
id: bench-psu-1
name: Bench PSU (E3631A)
type: agilent.e3631a@1.0.0
address: { board: 0, pad: 5, sad: null }
overrides:
  limits: { P6V.voltage: [0, 5.5] }   # tighter than the instrument allows
  readouts: { v_meas: { poll: 1s } }
  labels: { P25V: 'Op-amp +V' }
safe_state: { on_server_stop: leave, on_client_disconnect: leave }   # or: outputs_off (decided: default leave)
notes: Rack 2, feeds test fixture A
revision: 3
```

### 4.4 Built-in types (initial set)
The instruments on this bench come first. Each type is written from its programming manual and
marked `verified: false` until it has been exercised on the real instrument. GPIB addresses are not
known yet except the E3631A (5); they'll be set per instance.

| Instrument | Kind | Command set | Notes |
|---|---|---|---|
| Agilent E3631A | Triple power supply | SCPI | Verified at address 5 |
| HP 6632A | System power supply (20 V / 5 A) | HP-IB (`VSET`, `ISET`, `OUT`), `ID?` | Pre-SCPI; status via serial poll |
| HP 3457A | 7.5-digit DMM | HP-IB (HP 3457 language), `ID?` | Pre-SCPI |
| HP 3456A | 6.5-digit DMM | HP-IB program codes | No ID query; state not readable |
| HP 3455A | 5.5-digit DMM | HP-IB program codes | No ID query; oldest command set |
| HP 5334B | Universal counter | HP-IB codes, `ID` | Talk-only readings |
| HP 8713B | RF network analyzer | SCPI | Traces |
| HP 8714ES / 8714ET | RF network analyzers | SCPI | Traces; S-param (ES) vs T/R (ET) |
| HP 3589A | Spectrum/network analyzer | SCPI (HP-IB) | Traces |
| Tektronix TDS684B | Oscilloscope | Tek GPIB (SCPI-like) | Waveforms via `CURVE?` |
| Tektronix TDS754C | Oscilloscope | Tek GPIB (SCPI-like) | Shares a TDS family base with the 684B |
| "8519C" | **[unclear: please confirm the model]** | | Possibly HP 8591C spectrum analyzer? |

Also built in: "Generic SCPI instrument" (IDN, reset, error queue, raw I/O) and "Raw GPIB device"
(raw I/O only), so any instrument can be used before it has a type.

Order: E3631A, then 6632A and 3457A (prove the pre-SCPI path), 3456A/3455A/5334B, then one
analyzer and one scope (prove traces), then the rest of their families.

## 5. Protocol (JSON Lines, same on TCP and WebSocket)

Request `{"id": 7, "op": "...", ...}`; response `{"id": 7, "ok": true, "result": ...}` or
`{"id": 7, "ok": false, "error": {"code": "...", "msg": "..."}}`; server events have no id:
`{"event": "reading", "instance": "bench-psu-1", "key": "P6V.v_meas", "value": 5.001, "t": ...}`.

| Area | Operations |
|---|---|
| Session | `hello` (client user/host name, protocol version), `ping` |
| Types | `types.list`, `types.get {type, version?}`, `types.put {yaml}` (validate + store new version), `types.validate`, `types.history` |
| Instances | `instances.list`, `instances.get`, `instances.create`, `instances.update`, `instances.delete`, `instances.upgrade_type` |
| Control | `dev.get {instance, key}`, `dev.set {instance, key, value}`, `dev.action`, `dev.state` (all cached values), `dev.lock` / `dev.unlock` |
| Streams | `subscribe {instance, keys?}`, `unsubscribe` |
| Traces | `dev.trace {instance, key}` (one array with axis/scaling), traces also delivered as `trace` events to subscribers at their poll rate |
| Raw | `raw.write`, `raw.query`, `raw.read`, `bus.clear`, `bus.serial_poll`, `bus.local`, `bus.trigger` (need the instance lock, or for an address with no instance, a bus lock) |
| Discovery | `bus.scan` (find listeners on addresses 1-30 with a GPIB listener check, which sends no device commands; then, only if asked, run each candidate type's safe identity probe and suggest matches. Never sends `*IDN?` blindly, since pre-SCPI instruments may misparse it) |

`docs/protocol.md` will define every message.

## 6. Clients

### 6.1 CLI (`gpibctl`, pure Python 3.9+, runs on the Pi, Windows or Linux)
- Command mode: `gpibctl ls`, `gpibctl get bench-psu-1 P6V.voltage`, `gpibctl set bench-psu-1 P6V.voltage 5`,
  `gpibctl watch bench-psu-1`, `gpibctl raw 5 "*IDN?"`, `gpibctl type push my_dmm.yaml`, `gpibctl scan`.
- Menu mode: `gpibctl menu bench-psu-1` walks the type's `menu` tree; shows live readouts; prompts with
  ranges and units; confirms dangerous actions.
- `gpibctl lock bench-psu-1` / `unlock`; `set` takes the lock automatically for one command if free.
- `gpibctl trace na-1 S21 --csv out.csv` saves a trace; `watch` prints scalar readouts.
- Connects to `host:9000`; config file for the default host.

### 6.2 Web client (served on 8080)
- Plain JavaScript modules, no build step (editable in place, nothing to compile on the Pi).
- Views: instrument list with live status; **front panel** rendered from the type's `panel` (SVG:
  displays, buttons, knobs, switches; mouse drag/wheel on knobs, keyboard shortcuts, arrow keys to
  step digits); a generic **controls table** for any type without a panel; live charts of readouts;
  a **trace display** (scope graticule / analyzer grid, markers, CSV/PNG export) for scopes and analyzers;
  softkey column for softkey-driven instruments; lock status and take/release/break buttons;
  instance editor; type editor (YAML with validation errors shown inline, save as new version);
  bus scan; raw console.

## 7. Security and safety
- **LAN or VPN use only.** Traffic is cleartext TCP (no TLS by design), so the server must not be
  exposed to the internet; remote users come in over a VPN.
- **No access tokens or roles (decided).** Anyone who can reach the ports has full control. Accidental
  interference between users is prevented by the per-instrument **control locks** (section 3).
- Server-side limit checks on every set, using type ranges narrowed by instance overrides.
- Every set/action/raw command and every lock break logged (client name, host, time) to an audit log.
- **Safe state (decided):** by default outputs are left as they are when the server stops or the
  controlling client disconnects; each instance can opt in to switching outputs off instead.

## 8. Repository layout
```
server/gpibremote/   backends/ (linuxgpib.py ctypes, sim.py), bus.py, registry.py, runtime.py,
                     protocol.py, tcp.py, web.py, schema/
cli/gpibctl/
web/                 index.html, js/, css/
devices/types/       built-in type files
deploy/              systemd unit, install script, Python build script, offline package bundle for the Pi
docs/                PLAN.md, pi-setup.md, protocol.md, device-type-format.md
scripts/             test_e3631a.py and other hardware checks
tests/
```

## 9. Phases

| Phase | Content | Done when |
|---|---|---|
| 0. Platform | Build Python 3.11 (no `ssl`) on the Pi; offline package install from the PC; ctypes GPIB wrapper; repo skeleton; systemd unit; CI-less test run | `python3.11` on the Pi reads `*IDN?` through the wrapper; server starts at boot |
| 1. Core server | Type schema + loader + validator; E3631A, 6632A and 3457A types (SCPI and pre-SCPI); simulator; bus worker with SRQ/serial poll; instance runtime with polling, limits and **control locks**; registry with git history and push to `device-data`; audit log; JSON Lines on 9000 | Over `nc`, list types, create instances, lock, set/read the E3631A and 6632A, receive reading events; a second client is refused while locked |
| 2. CLI | Command mode, watch, lock, raw, menu mode | All functions of the phase-1 instruments usable from `gpibctl` on Windows against the Pi |
| 3. Web client | WebSocket transport, instrument list, generic controls, live readouts, charts, lock UI | Full control of the phase-1 instruments from a browser |
| 4. Traces | `trace` readouts and block/CURVE parsers; types for one scope (TDS754C) and one analyzer (8714ET); trace display in web and CSV export in CLI | A live scope waveform and an analyzer sweep in the browser |
| 5. Front panels and authoring | Panel renderer; softkey menus; type editor; `types.put` versioning; safe bus scan with identity probes; instance upgrade | A new type written in the browser is usable from the CLI immediately |
| 6. Library | Remaining bench types (3456A, 3455A, 5334B, 8713B, 8714ES, 3589A, TDS684B, the "8519C"), each verified on its instrument; docs | Every bench instrument usable without raw commands |

Each phase ends with a hardware test on the Pi and an update to the docs.

**Progress**
- Phase 0: **done 2026-11-27.** Python 3.11.18 (no ssl) in `/opt/gpibremote`; ctypes linux-gpib
  backend; simulator; bus worker; JSON Lines server with raw operations (`docs/protocol.md`);
  minimal `gpibctl`; offline package install; systemd service verified across a reboot.
  The HTTP/WebSocket stack choice (2.2) is still open; it's needed in phase 3.
- Phase 1: **done 2026-11-27**, except pushing `device-data` to GitHub (waits for the deploy key).
  Type format (`docs/device-type-format.md`) with validator; types for the E3631A (verified on the
  bench), 6632A and 3457A (simulator only, `verified: false`), generic SCPI and raw; git-versioned
  registry; instance runtime with polling, limits, error-queue checks, locks and safe state;
  protocol and events (`docs/protocol.md`); type-driven simulator; 17 tests on PC and Pi;
  hardware check `scripts/check_phase1.py`. Not done from the phase-1 list: SRQ-driven reads
  (serial poll exists; waiting on SRQ comes with the slow-measurement types).

## 10. Decisions and open questions

Decided (2026-11-26):
- Plain TCP/HTTP/WS, no TLS; LAN or VPN only.
- Control locks from phase 1; no access tokens or roles.
- Versioned types/instances pushed to this repo, branch `device-data`.
- Outputs left as they are on server stop/disconnect by default; per-instance opt-in to switch off.
- Bench instruments: see 4.4.

Open:
1. "8519C": which instrument is it (HP 8591C spectrum analyzer?)
2. GPIB addresses of the other instruments, and when they'll be connected to this bus.
3. A deploy key for the Pi to push `device-data` (you add it in the GitHub repo settings; phase 1).
4. OK to spend ~1.5 h of Pi compile time building Python 3.11 without `ssl` (phase 0)?
