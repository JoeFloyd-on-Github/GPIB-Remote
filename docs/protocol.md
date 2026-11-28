# GPIB-Remote protocol (version 1)

Transport: plain TCP port 9000 (no TLS; LAN or VPN only). Each message is one JSON object on one
line, UTF-8, terminated by `\n`.
You can try it with `nc 192.168.1.157 9000`.

The same messages also travel over a plain WebSocket at `ws://192.168.1.157:8080/ws`
(RFC 6455, version 13), one JSON object per text message; binary messages are refused. A
browser's request is refused unless its `Origin` is the server itself or listed in
`web.allowed_origins`; clients that send no `Origin` are accepted. Port 8080 also serves the
web client's files (GET/HEAD only), and `/download/gpibctl.zip`: the CLI and test-script API
zipped from the server's copy. Requests on one WebSocket are answered in order, as on TCP.

## Requests, responses, events

```
-> {"id": 7, "op": "dev.set", "instance": "bench-psu", "key": "P6V.voltage", "value": 5}
<- {"id": 7, "ok": true, "result": {"value": 5.0, "t": 1764205409.2, "source": "read"}}
<- {"id": 8, "ok": false, "error": {"code": "locked", "msg": "bench-psu is locked by bob@lab", "details": {...}}}
<- {"event": "reading", "instance": "bench-psu", "key": "P6V.v_meas", "value": 4.9993, "t": ...}
```

- `id` is chosen by the client and echoed back. Requests on one connection are answered in order.
  Events (no `id`) can arrive at any time between responses.
- Start with `hello` so your name appears in locks, history and the audit log (not authentication).
  If `host` is empty, the server uses the client's IP address.
- Error codes: `bad_json`, `bad_request`, `unknown_op`, `not_found`, `invalid` (with `errors`, a
  list of problems), `conflict`, `rejected` (value out of range or wrong kind), `locked` (with the
  holder in `details`), `not_locked`, `device_error` (the instrument's error queue reported one),
  `bad_response`, `timeout`, `gpib` (NI-488 mnemonic in `gpib`, e.g. `ENOL` = nothing at that
  address), `internal`.

## Values and keys

A **key** names a control or readout of an instance: `output`, or `P6V.voltage` for a
per-channel item. Each value is reported as `{"value", "t", "source"}` where `source` is
`read` (read back from the instrument), `inferred` (the instrument can't be asked for it, so
it's judged from a status reading: the type's `infer`), `assumed` (last value set; the
instrument can't be queried), or `unknown` (not yet known).

## Control locks

Many clients may watch an instance; one may control it. `dev.lock` takes the lock; while
someone else holds it, `dev.set`, `dev.action`, `instances.update/delete` and raw I/O to that
instance's address are refused with `locked`. When nobody holds it, those operations are
allowed (a client takes the lock for longer work). Locks are released on `dev.unlock`, when
the holder disconnects, or after the server's idle timeout (default 15 min). `dev.lock` with
`"force": true, "reason": "..."` breaks someone else's lock; they get a `lock_broken` event.

## Operations

### Session
| op | Parameters | Result |
|---|---|---|
| `hello` | `client`, `user`, `host` | `server`, `version`, `protocol`, `host`, `session` |
| `ping` | | `t` |
| `server.info` | | `version`, `protocol`, `uptime`, `boards`, `sessions` (`id`, `who`, `peer`, `client`, `transport` `tcp`/`ws`) |

### Types
| op | Parameters | Result |
|---|---|---|
| `types.list` | | `types`: `[{type, versions, latest, title, verified, builtin}]` |
| `types.get` | `type` (or `type@version`), `version`, `format`: `model` (default), `yaml`, `both` | `ref`, `hash`, `model` (includes `roles`, `display`, `restore`) and/or `yaml` |
| `types.validate` | `yaml` | `valid`, `errors`, `ref`, `exists` |
| `types.put` | `yaml`, `message` | `ref`, `hash`, `added` (false if identical to the stored version). Different content under an existing version is a `conflict` |
| `types.history` | `type`, `limit` | `commits`: `[{rev, author, date, message}]` |

### Instances
| op | Parameters | Result |
|---|---|---|
| `instances.list` | | `instances`: `[{id, name, type, title, address, revision, online, lock, poll_mode}]` |
| `instances.get` | `instance` | `instance` (the stored document), `summary` |
| `instances.create` | `instance`: `{id, name, type, address: {board, pad, sad}, overrides, safe_state, poll_mode, notes}`, `verify` (default true) | `instance`, `identify` (probe result) |
| `instances.update` | `instance`, `changes` (fields to replace), `revision` (optional check) | `instance` |
| `instances.delete` | `instance` | |
| `instances.identify` | `instance` | `probe`, `response`, `match` |
| `instances.history` | `instance`, `limit` | `commits` |

Instance document fields: `overrides.limits` (`{key: [min, max]}`), `overrides.poll`
(`{readout: interval or "off"}`), `overrides.labels`, `safe_state.on_server_stop` and
`safe_state.on_client_disconnect` (`leave` default, or `outputs_off`), `poll_mode`
(`subscribed` default: poll only while someone is subscribed; `always`; `off`), `restore`
(false stops the server reapplying saved settings for a type with `restore: true`), `notes`.

### Device control
| op | Parameters | Result |
|---|---|---|
| `dev.state` | `instance` | `state`: every key's last value, `summary` |
| `dev.get` | `instance`, `key`, `fresh` (default true: ask the instrument) | value |
| `dev.set` | `instance`, `key`, `value` | value as read back (`read`, or `assumed`) |
| `dev.action` | `instance`, `action`, `channel` | |
| `dev.refresh` | `instance` | `state` after reading back every readable setting |
| `dev.lock` | `instance`, `force`, `reason` | `lock`: `{holder, session, since, idle}` |
| `dev.unlock` | `instance` | |
| `dev.locks` | | `locks`: `{instance: lock}` |
| `subscribe` | `instance` | `state`; then `state` and `reading` events for it, and polling starts |
| `unsubscribe` | `instance` | |

### Raw bus access
Parameters: `board` (default 0), `pad` 0-30, `sad` (0 or 0x60-0x7e), `timeout` seconds (default 3),
`data` (a `\n` is appended unless present or `raw_terminator` is true), `encoding` `text` (latin-1)
or `base64`, `max_bytes` (default 1 MiB, max 16 MiB).

| op | Result |
|---|---|
| `bus.listeners` | `pads` with a device present (listener check only, no device commands) |
| `raw.write` / `raw.read` / `raw.query` | `bytes`; reads return `data` |
| `bus.clear` / `bus.local` / `bus.trigger` | selected device clear, go to local, group execute trigger |
| `bus.serial_poll` | `status` byte |
| `bus.ifc` | interface clear (refused if anyone else holds a lock on that board) |

## Events
| event | Sent to | Fields |
|---|---|---|
| `reading` | subscribers | `instance`, `key`, `value`, `t` |
| `state` | subscribers | `instance`, `key`, `value`, `source`, `t`, `by` |
| `lock` | everyone | `instance`, `lock` (null when free), `reason` |
| `lock_broken`, `lock_expired` | the former holder | `instance`, `by`, `reason` |
| `status` | everyone | `instance`, `online`, `error` |
| `restored` | subscribers | `instance`, `keys`: the settings reapplied after the instrument came back |
| `instance` | everyone | `op` (`created`/`updated`/`deleted`), `id`, `revision` |
| `type` | everyone | `op` (`added`), `ref` |
