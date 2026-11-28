# Test-script API (`gpibctl.api`)

A Python layer for experiment scripts: find instruments, give them handles, configure them and
take measurements, without writing protocol messages. It talks to the GPIB-Remote server like
the CLI does (TCP 9000, docs/protocol.md), so a script runs on any PC that can reach the Pi.
Standard library only, Python 3.6+. The code is `cli/gpibctl/api.py`.

The `gpibctl` command-line client is built on this API (no protocol calls of its own), so
anything the CLI does, a script can do the same way; `cli/gpibctl/commands.py` shows how.
`scripts/nickel_plating.py` is a complete experiment.

```python
import sys; sys.path.insert(0, "path/to/GPIB-Remote/cli")
from gpibctl.api import Bench

with Bench("192.168.1.157") as bench:
    psu = bench.supply("plating-psu")
    dmm = bench.meter("dmm-ohms")
    dmm.configure("ohm4", range=30, nplc=100, offset_comp=True)
    print(dmm.measure(10))                       # <Stats n=10 mean=... sd=...>
    with psu.lock("run 12"), psu.guard():        # exclusive control; output off when the block ends
        psu.set(voltage=3.0, current=0.48)       # constant current, 3 V compliance
        psu.on()
        print(psu.measure_current())
```

## Three levels

| Level | Get it with | Speaks | Use it for |
|---|---|---|---|
| Raw GPIB | `bench.raw(pad)` or `device.raw` | the instrument's own commands | anything the type doesn't model |
| Device handle | `bench.device(id)` | the type's key names (`P6V.voltage`, `function`) | everything the type models, configuration, locks |
| Roles | `bench.supply(id)`, `bench.meter(id)`, `bench.thermometer(id)` | standard methods | scripts that should work with any supply or meter |

A **handle** is an instance id on the server (`plating-psu`, `dmm-ohms`): a name, a device type
and a GPIB address, stored with its history. Create one once (`bench.add`, `gpibctl inst create`,
or the web client's Admin page) and every script and client uses the name.

Roles come from the type's `roles:` section (docs/device-type-format.md). A type without one is
used by convention if its keys have the usual names.

## Bench: the connection

| | |
|---|---|
| `Bench(host=None, port=None, user=None, client="gpibctl.api", timeout=30)` | Connect. host/port default to `GPIBREMOTE_HOST`/`GPIBREMOTE_PORT`, then `~/.gpibctl.ini`, then `localhost:9000`. Use as a context manager. |
| `close()` | Turn off supplies this Bench turned on, release its locks, disconnect. |
| `info()` | Server version, uptime, boards, sessions. |
| `instances()` | The server's listing: `[{id, name, type, title, address, revision, online, lock, poll_mode}]`. |
| `devices()` | Handles for every configured instrument. |
| `device(id)`, `bench[id]` | The handle for one (the same object each time; nothing is fetched until it is used). |
| `locks()` | Held control locks: `{id: {holder, session, since, idle}}`. |
| `listeners(board=0)` | Addresses where a device answers (no commands sent). |
| `scan(board=0, probes=("*IDN?", "ID?"), timeout=1)` | Every address answering on the board: `Found(pad, instance, type, probe, response, types)`. Unconfigured addresses are asked each probe in turn; `types` lists the device types whose identify pattern matches. |
| `add(id, type, pad, board=0, sad=0, name=, notes=, limits={key: (lo, hi)}, overrides=, safe_state=, poll_mode=, off_on_disconnect=False, restore=None, verify=True)` | Configure an instrument and return its handle. `type` is `"hp.3457a"` (latest) or `"hp.3457a@1.1.0"`. The identify probe's answer is in the handle's `identify_result`. `restore=False` stops the server reapplying settings when the instrument comes back (types with `restore`). |
| `remove(id)` | Delete a configuration (history stays). |
| `types()`, `type_model(ref)`, `type_ref(name)` | Device types, one type's full model, a name's latest `type@version`. |
| `type_yaml(ref)`, `validate_type(text)`, `push_type(text, message)`, `type_history(type=None, limit=30)` | Type files: read, check, store a new type or version, history. |
| `raw(pad, board=0)` | A `RawDevice`. |
| `supply(id, channel=None)`, `meter(id)`, `thermometer(id, thermocouple=None, cjc=None)` | Role views. |
| `request(op, **params)` | Any protocol operation directly. |
| `events(match=None)` | Take the events received so far (readings, state and lock changes, status); with `match`, only those it accepts. |
| `wait_event(match=None, timeout=10)` | The next matching event, or `TimeoutError`. |

## Device: a handle

| | |
|---|---|
| `id`, `name`, `type`, `address` (board, pad), `config`, `model`, `channels`, `roles`, `online` | Facts (`config` and `model` are fetched on first use; `reload()` re-reads them). |
| `summary()` | Live status: address, online, lock. |
| `keys()`, `actions()`, `describe()` | What it offers; `describe()` is a readable list with units and ranges. |
| `get(key, fresh=True)`, `get_float(key)` | A value; a readout takes a new measurement. |
| `get_entry(key, fresh=True)` | `{value, t, source}`: `source` is `read`, `inferred` (judged from a status reading), `assumed` (last value set; the instrument can't report it) or `unknown`. |
| `set(key, value)`, `set_entry(key, value)` | Set a control; returns the value read back (or the entry). Out-of-range values raise `ServerError` with code `rejected`. |
| `configure(**settings)` | Several `set`s in order. |
| `action(name, channel=None)` | Run an action (`reset`, `autocal_ohms`, ...). |
| `state()`, `snapshot()`, `refresh()` | Last known values; the same with the type and live status; read every setting back. |
| `subscribe()`, `unsubscribe()` | Receive this instrument's readings and changes as events (`Bench.events`, `wait_event`). |
| `identify()` | `{probe, response, match}`: is the right instrument at the address? |
| `lock(reason="", force=False)`, `unlock()`, `lock_holder()` | Exclusive control. `with dev.lock("why"):` releases at the end; the returned object's `info` is the lock. Others can still watch. `force` breaks someone else's lock (they are told why). |
| `update(revision=None, **changes)` | Change the stored configuration: `name`, `notes`, `overrides`, `safe_state`, `poll_mode`, `restore`, `type`, `address`. Kept in the instance's history. With `revision`, refused (`conflict`) if someone changed it since. |
| `set_limit(key, lo, hi)` | Narrow a control's range for everyone (e.g. a fixture's voltage cap). |
| `history()` | Configuration changes. |
| `raw`, `write(cmd)`, `query(cmd)` | Raw GPIB at this instrument's address. |

## Supply

`voltage`, `current` and `output` are required in the type; measuring needs `v_meas`/`i_meas`.

| | |
|---|---|
| `set(voltage=None, current=None)` | Settings. For constant current, set the current and use the voltage as the compliance limit. |
| `on(keep_on=False)`, `off()`, `is_on()` | Output. Unless `keep_on`, the Bench turns it off again on close. |
| `guard()` | Context manager: output off and current limit 0 when the block ends, however it ends. |
| `measure_voltage()`, `measure_current()`, `setpoints()` | Readings. |
| `max_voltage`, `max_current` | From the type, narrowed by the instance's limits. |
| `channel`, `key(field)` | The channel used and the concrete key for a field. |

## Meter

Functions use standard names whatever the instrument calls them: `dcv acv acdcv dci aci acdci
ohm2 ohm4 freq period temp`. `meter.functions` shows what this one offers.

| | |
|---|---|
| `configure(function=None, range=None, nplc=None, autozero=None, offset_comp=None)` | `range` is the largest expected value, or `"auto"`. A setting the instrument lacks raises `ApiError`. |
| `read()` | One fresh reading, less the null offset. |
| `read_many(n, interval=0)`, `measure(n, interval=0)` | Readings; `measure` returns `Stats` (`mean`, `stdev`, `sem`, `min`, `max`, `n`, `values`). |
| `null(n=5)` | Take the present input as zero (a shorted 4-wire probe) and subtract it from later readings of this function. |
| `autocal(wait=60)` | Run the self-calibration action and wait for it. |
| `reset()` | Run the reset action. |

## Thermometer

`bench.thermometer(id)` uses an instrument's thermometer role. For a DMM reading a type K
thermocouple on DC volts: `bench.thermometer(id, thermocouple="K", cjc=23.0)`, where `cjc` is
the reference-junction temperature (where the thermocouple wires meet copper, usually the DMM
terminals), as a number or a function returning one. `setup()` puts the meter on DC volts.

| | |
|---|---|
| `read()` | Temperature in C, with the calibration offset. |
| `calibrate(true_c, n=5)` | Offset so the reading equals a reference (ice bath, reference thermometer). |
| `type_k_emf(t)`, `type_k_temperature(mv)` | NIST ITS-90 type K polynomials (module functions). |

## RawDevice

`write(cmd)`, `read()`, `query(cmd)`, `query_float(cmd)`, `clear()` (SDC), `trigger()` (GET),
`local()`, `serial_poll()`. All take `timeout` in seconds where it applies. Raw I/O to an
address is refused while someone else holds the lock on an instance there.

## DataLog

CSV logging with time codes: `DataLog(path, columns, meta={...})` writes `# key: value` lines,
then a header `time,t_s,<columns>`. `record(**values)` adds a row with the local ISO 8601 time
(milliseconds, UTC offset) and the elapsed seconds, and flushes it, so the file can be watched
while the run goes on. `note(text)` adds a time-stamped comment; `restart_clock()` zeroes `t_s`.

## Errors

- `ServerError`: the server refused or the instrument failed. `.code` is one of `locked`,
  `rejected` (out of range), `not_found`, `timeout`, `gpib` (e.g. nothing at the address),
  `device_error` (the instrument's error queue), ... (docs/protocol.md).
- `ApiError`: the instrument's type can't do what was asked (no such role, function or setting).
- `OSError`: the server can't be reached.

## Safety

- `Supply.guard()` and `Bench.close()` turn outputs off. Locks end when the script disconnects,
  even if it crashes.
- For protection that doesn't depend on the script, create supplies with
  `off_on_disconnect=True`: the server turns the outputs off if the controlling client's
  connection drops.
- `set_limit` caps a control for every client, so a fixture's limits can't be exceeded by mistake.
