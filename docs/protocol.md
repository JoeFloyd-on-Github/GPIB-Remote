# GPIB-Remote protocol (version 1, phase 0 subset)

Transport: plain TCP port 9000 (no TLS). Each message is one JSON object on one line,
UTF-8, terminated by `\n`. Phase 3 adds the same messages over a plain `ws://` WebSocket.

## Requests and responses

```
-> {"id": 7, "op": "raw.query", "pad": 5, "data": "*IDN?"}
<- {"id": 7, "ok": true, "result": {"data": "HEWLETT-PACKARD,E3631A,0,1.6-5.0-1.0", "bytes": 37}}
<- {"id": 8, "ok": false, "error": {"code": "timeout", "msg": "...", "gpib": "EABO"}}
```

- `id` is chosen by the client and echoed back. Requests on one connection are answered in order.
- Server-pushed events (phase 1) carry `"event"` and no `id`.
- Error codes: `bad_json`, `bad_request`, `unknown_op`, `timeout`, `gpib` (with the NI-488 error
  mnemonic in `gpib`, e.g. `ENOL` = no listener), `internal`.

## Common parameters

| Name | Meaning |
|---|---|
| `board` | GPIB board (minor) number, default 0 |
| `pad` | Primary address 0-30 |
| `sad` | Secondary address: 0 (none, default) or 0x60-0x7e |
| `timeout` | Seconds for the bus operation, default 3 |
| `data` | Text to send. A `\n` is appended unless it already ends with one or `raw_terminator` is true |
| `encoding` | `text` (default; latin-1) or `base64` for binary data, both for `data` sent and returned |
| `max_bytes` | Read limit, default 1 MiB, maximum 16 MiB |

## Operations (phase 0)

| op | Parameters | Result |
|---|---|---|
| `hello` | `client`, `user`, `host` (for display and the audit log; not authentication) | `server`, `version`, `protocol`, `host` |
| `ping` | | `t` (server time) |
| `server.info` | | `version`, `protocol`, `uptime`, `boards` |
| `bus.listeners` | `board` | `pads`: addresses with a device present (GPIB listener check, no device commands) |
| `bus.ifc` | `board` | Interface clear |
| `raw.write` | `pad`, `sad`, `data`, `encoding`, `timeout` | `bytes` |
| `raw.read` | `pad`, `sad`, `max_bytes`, `encoding`, `timeout` | `data`, `bytes` (trailing CR/LF stripped in text mode) |
| `raw.query` | as write + read, atomic on the bus | as `raw.read` |
| `bus.clear` | `pad`, `sad` | Selected device clear |
| `bus.local` | `pad`, `sad` | Go to local (front-panel control) |
| `bus.trigger` | `pad`, `sad` | Group execute trigger |
| `bus.serial_poll` | `pad`, `sad` | `status` (status byte) |

Every write, query, clear, trigger and IFC is written to the audit log with the client's name and address.
