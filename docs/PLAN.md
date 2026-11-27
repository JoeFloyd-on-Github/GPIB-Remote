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
- **Locking:** many observers, one controller per instance. A client takes a lease (`lock`) to change
  settings; it expires after N minutes idle or on disconnect. Admins can break locks. **[decide: needed from day one, or phase 5?]**

## 4. Data: device types and instances

All stored as YAML under a data directory that is itself a git repository (the server commits every
change with the user's name and a message). Built-in types live in this repo under `devices/types/`
and are copied/merged into the data dir on install.

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
init/shutdown sequences; error query; non-SCPI instruments (e.g. HP 3478A's terse commands) through
the same templates.

### 4.2 Versioning
- A type is identified by `type@version` (semver). A published version is **immutable**; edits produce a
  new version (patch for fixes, minor for added controls, major for incompatible changes).
- Each file also has a content hash, so clients can cache and detect changes.
- Instances pin a type version. The server reports when a newer compatible version exists; upgrading is an explicit step.
- History: every change to types or instances is a git commit in the data directory (who, when, why).
  Optionally pushed to a GitHub repo. **[decide: push the data dir to this repo, a separate repo, or keep local?]**

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
safe_state: { on_server_stop: leave, on_error: leave }   # or: output_off
notes: Rack 2, feeds test fixture A
revision: 3
```

### 4.4 Built-in types (initial set)
Only the E3631A can be verified on hardware here; the rest would be written from manuals and marked
`verified: false` until someone tests them. **[decide: which instruments do you actually have?]**
- Power supplies: Agilent E3631A (verified), E3632A/E3633A/E3634A, HP 6632B/66332A.
- DMMs: HP/Agilent 34401A, Keithley 2000, HP 3478A (non-SCPI).
- Sources/SMUs: Keithley 2400.
- Generators: HP/Agilent 33120A.
- Generic: "SCPI instrument" (IDN, reset, error queue, raw I/O) and "Raw GPIB device".
- Oscilloscopes and analyzers (waveform transfer, binary blocks) later; they need more format work.

## 5. Protocol (JSON Lines, same on TCP and WebSocket)

Request `{"id": 7, "op": "...", ...}`; response `{"id": 7, "ok": true, "result": ...}` or
`{"id": 7, "ok": false, "error": {"code": "...", "msg": "..."}}`; server events have no id:
`{"event": "reading", "instance": "bench-psu-1", "key": "P6V.v_meas", "value": 5.001, "t": ...}`.

| Area | Operations |
|---|---|
| Session | `hello` (client name, protocol version, optional token), `ping` |
| Types | `types.list`, `types.get {type, version?}`, `types.put {yaml}` (validate + store new version), `types.validate`, `types.history` |
| Instances | `instances.list`, `instances.get`, `instances.create`, `instances.update`, `instances.delete`, `instances.upgrade_type` |
| Control | `dev.get {instance, key}`, `dev.set {instance, key, value}`, `dev.action`, `dev.state` (all cached values), `dev.lock` / `dev.unlock` |
| Streams | `subscribe {instance, keys?}`, `unsubscribe` |
| Raw | `raw.write`, `raw.query`, `raw.read`, `bus.clear`, `bus.serial_poll`, `bus.local` (permission-gated) |
| Discovery | `bus.scan` (find listeners on addresses 1-30, read `*IDN?`, suggest matching types) |

`docs/protocol.md` will define every message.

## 6. Clients

### 6.1 CLI (`gpibctl`, pure Python 3.9+, runs on the Pi, Windows or Linux)
- Command mode: `gpibctl ls`, `gpibctl get bench-psu-1 P6V.voltage`, `gpibctl set bench-psu-1 P6V.voltage 5`,
  `gpibctl watch bench-psu-1`, `gpibctl raw 5 "*IDN?"`, `gpibctl type push my_dmm.yaml`, `gpibctl scan`.
- Menu mode: `gpibctl menu bench-psu-1` walks the type's `menu` tree; shows live readouts; prompts with
  ranges and units; confirms dangerous actions.
- Connects to `host:9000`; config file for default host and token.

### 6.2 Web client (served on 8080)
- Plain JavaScript modules, no build step (editable in place, nothing to compile on the Pi).
- Views: instrument list with live status; **front panel** rendered from the type's `panel` (SVG:
  displays, buttons, knobs, switches; mouse drag/wheel on knobs, keyboard shortcuts, arrow keys to
  step digits); a generic **controls table** for any type without a panel; live charts of readouts;
  instance editor; type editor (YAML with validation errors shown inline, save as new version);
  bus scan; raw console.

## 7. Security and safety
- **LAN or VPN use only.** Traffic is cleartext TCP (no TLS by design), so the server must not be
  exposed to the internet; remote users come in over a VPN.
- **Optional per-client access token**, to guard against accidental control rather than to resist
  attack (a cleartext token can be sniffed). Sent in `hello`; tokens map to roles `observer`,
  `operator`, `admin` (admin: raw commands, type edits, lock breaking). Can be switched off in the
  server config, in which case every client is an operator.
- Server-side limit checks on every set, using type ranges narrowed by instance overrides.
- Every set/action/raw command logged (who, what, when) to an audit log.

## 8. Repository layout
```
server/gpibremote/   backends/ (linuxgpib.py ctypes, sim.py), bus.py, registry.py, runtime.py,
                     protocol.py, tcp.py, web.py, schema/
cli/gpibctl/
web/                 index.html, js/, css/
devices/types/       built-in type files
deploy/              systemd unit, install script, Python/OpenSSL build script for the Pi
docs/                PLAN.md, pi-setup.md, protocol.md, device-type-format.md
scripts/             test_e3631a.py and other hardware checks
tests/
```

## 9. Phases

| Phase | Content | Done when |
|---|---|---|
| 0. Platform | Build Python 3.11 (no `ssl`) on the Pi; offline package install from the PC; ctypes GPIB wrapper; repo skeleton; systemd unit; CI-less test run | `python3.11` on the Pi reads `*IDN?` through the wrapper; server starts at boot |
| 1. Core server | Type schema + loader + validator; E3631A type; simulator; bus worker; instance runtime with polling and limits; registry with git history; JSON Lines on 9000 | Over `nc`, list types, create an instance, set/read the E3631A, receive reading events |
| 2. CLI | Command mode, watch, raw, menu mode | All E3631A functions usable from `gpibctl` on Windows against the Pi |
| 3. Web client | WebSocket transport, instrument list, generic controls, live readouts, charts | Full E3631A control from a browser |
| 4. Front panels and authoring | Panel renderer; type editor; `types.put` versioning; bus scan with IDN matching; instance upgrade | A new type written in the browser is usable from the CLI immediately |
| 5. Multi-user and library | Locks/leases, tokens and roles, audit log, more built-in types, docs | Two users can share the bench safely |

Each phase ends with a hardware test on the Pi and an update to the docs.

## 10. Open questions (summary)
1. Which other instruments (models, addresses) will be on the bus? This sets the built-in library order.
2. ~~Network exposure~~ Decided: plain TCP/HTTP/WS, no TLS, LAN or VPN only.
3. Access tokens (optional, can be switched off) and control locking: needed from the start, or later?
4. Where should versioned instance/type data be pushed (this repo, separate repo, local only)?
5. Default safe state: should outputs be switched off when the server stops or loses a client? (Proposed: leave as is, configurable per instance.)
6. OK to spend ~1.5 h of Pi compile time building Python 3.11 without `ssl` (phase 0)?
