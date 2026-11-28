# Device type format (schema 1)

A device type is one YAML file describing an instrument model: how to talk to it, its
controls (settings), readouts (measurements), actions, and how clients should present it.
Built-in types live in `devices/types/<type>/<version>.yaml`; types added at run time are
stored by the server in its data directory under the same layout.

Examples: [`agilent.e3631a`](../devices/types/agilent.e3631a/1.0.0.yaml) (SCPI, channels),
[`hp.6632a`](../devices/types/hp.6632a/1.0.0.yaml) (pre-SCPI, readable settings),
[`hp.3457a`](../devices/types/hp.3457a/1.0.0.yaml) (pre-SCPI, settings not readable),
[`hp.8714es`](../devices/types/hp.8714es/1.0.0.yaml) (channels numbered in the commands),
[`generic.talk-meter`](../devices/types/generic.talk-meter/1.0.0.yaml) (sends nothing, only reads).

Check a file without storing it: `types.validate` (protocol) returns every problem with its path.

## Identity and versioning

| Key | Meaning |
|---|---|
| `schema` | Always `1` |
| `type` | Lowercase dotted name, `vendor.model` (e.g. `hp.3457a`) |
| `version` | Quoted semantic version, `'1.0.0'`. A published version never changes: edit by raising it (patch: fixes; minor: additions; major: incompatible changes) |
| `title`, `manufacturer`, `model`, `description`, `references`, `notes` | Text for people |
| `verified` | `true` once the type has been exercised on a real instrument |

Instances name the exact version they use (`agilent.e3631a@1.0.0`).

## Talking to the instrument

```yaml
connection:
  eos: "\n"              # byte that ends a response; null = end on EOI only
  write_termination: "\n"
  timeout: 3s            # per bus operation; also 500ms, 1min, or a number of seconds
  delay: 0               # pause between commands in one operation (slow instruments)
identify:
  probe: '*IDN?'         # safe query that identifies the model; null if there is none
  match: '^HEWLETT-PACKARD,E3631A,'   # regex the answer must match
errors:
  query: 'SYST:ERR?'     # error-queue query
  none: '^\+?0,'         # regex for "no error"
  check_after_set: true  # read the queue after every set; an error fails the set
init: ['END ALWAYS']     # sent once before the first command after the server starts
safe_off: ['OUTP OFF']   # sent when an instance's safe_state asks for outputs off
```

Older HP-IB instruments may misread characters they don't expect, so give them a probe they
understand (`ID?`) or `probe: null`.

## Channels

```yaml
channels:
  select: 'INST:SEL {channel}'
  list:
    P6V:  {label: '+6V', vmin: 0, vmax: 6.18, imax: 5.15}
    N25V: {label: '-25V', vmin: -25.75, vmax: 0, imax: 1.03}
```

A control, readout or action with `per_channel: true` exists once per channel; its key is
`<channel>.<name>` (e.g. `P6V.voltage`). Before its command the server sends the `select`
command, unless the command itself names the channel (e.g. `MEAS:VOLT? {channel}`).
Channel entries can carry any attributes; bounds refer to them as `'{channel.vmax}'`, and
commands can use them too. Channel names are identifiers, so an instrument that numbers its
channels gets an attribute for the number:

```yaml
channels:
  list:
    CH1: {label: 'Channel 1', n: 1}
    CH2: {label: 'Channel 2', n: 2}
controls:
  start: {kind: number, per_channel: true, set: 'SENS{channel.n}:FREQ:STAR {value}', ...}
```

`select` is needed only when some per-channel command doesn't name its channel.

## Controls (settings)

```yaml
controls:
  voltage:
    kind: number          # number | bool | enum | text
    per_channel: true
    label: Voltage
    unit: V
    resolution: 0.001     # display/step hint for clients
    min: '{channel.vmin}' # number, or a channel attribute
    max: '{channel.vmax}'
    format: '%.3f'        # optional printf format for {value}; default: shortest exact form
    set: 'VOLT {value}'   # required
    get: 'VOLT?'          # optional; without it the server reports the last value it set as "assumed"
    parse: number         # optional; how to read the get response (default from kind)
```

- **bool:** `values: {'on': 'ON', 'off': 'OFF'}` gives the tokens for `{value}` (quote `on`/`off`:
  unquoted they are YAML booleans). Responses `1/0/ON/OFF/TRUE/FALSE` or any number are understood.
- **infer** (bool controls without `get`): judge the setting from a status readout the instrument
  can report. `infer: {readout: status, any_bits: 515}` makes the control on while any of those
  bits is set in the readout's value (the readout needs `parse: int`), and off otherwise. A get of
  the control reads the readout, every reading of it (polled ones too) updates the control, and
  the value is reported with source `inferred`. A set is still reported as `assumed` until the
  next reading. The HP 6632A's output uses it: `STS?` has CV, +CC or -CC set only while the
  output is on.
- **enum:** `options` maps each value to `{label, token, response, match}`; `token` (default: the
  name) goes into `{value}`; a get response is matched against `response` (default: the token)
  or the `match` regex. Clients send the option name (case-insensitive).
- Instance overrides can narrow `min`/`max` further (`overrides.limits`).

## Readouts (measurements)

```yaml
readouts:
  v_meas:
    per_channel: true
    label: Measured voltage
    unit: V
    query: 'MEAS:VOLT? {channel}'
    parse: number         # number (first number in the text) | int | bool | text | enum | {regex, as}
    poll: 3s              # polled at this interval while someone is subscribed; omit for on demand
  reading:
    read: true            # talk-only: no command, just address the instrument to talk
    trigger: get          # optional: send GPIB GET first
```

`parse: {regex: 'VOUT\s+(\S+)', as: number}` applies the regex first and parses group 1.

## Actions

```yaml
actions:
  reset:
    label: Reset (*RST)
    send: ['*RST']        # one or more commands, may use {channel} on per-channel actions
    confirm: true         # clients ask before running it
    refresh: true         # read all settings back afterwards
```

## Presentation

```yaml
menu:                     # CLI menu tree: item names, or {label: [items]}
  - Output: [output, tracking]
  - '{channel}': [voltage, current, v_meas, i_meas]   # repeated for each channel
panel: {...}              # front-panel layout for the web client (phase 5, not validated yet)
```

## Roles (for test scripts)

`roles` says what a type can stand in for, so a test script written against the API
(`docs/api.md`) works with any instrument whose type declares the role. Each field names one of
the type's own controls, readouts or actions; the server checks that they exist.

```yaml
roles:
  supply:                       # Supply: set(voltage, current), on(), off(), measure_*()
    voltage: voltage            # control: voltage setting (the compliance limit in CC use)
    current: current            # control: current limit
    output: output              # control: output on/off
    v_meas: v_meas              # readout: measured voltage   (optional)
    i_meas: i_meas              # readout: measured current   (optional)
    ovp: ovp                    # control: overvoltage trip   (optional)
    channel: P6V                # default channel for per-channel keys (optional)
  meter:                        # Meter: configure(function, range, nplc, ...), read(), measure(n)
    function: function          # enum control choosing the function
    functions: {dcv: DCV, ohm4: OHMF}   # standard name -> option of that control
    reading: reading            # readout: one fresh reading (required)
    range: range                # control: fixed range (largest expected value)
    autorange: autorange        # control: autorange on/off
    nplc: nplc                  # control: integration time in power-line cycles
    autozero: autozero          # control
    offset_comp: offset_comp    # control: offset-compensated ohms
    autocal: autocal_ohms       # action: self-calibration
    reset: reset                # action
  thermometer:                  # Thermometer: read() in C
    temperature: temp           # readout (required)
    unit: C                     # C, F or K (default C)
```

Standard meter function names: `dcv acv acdcv dci aci acdci ohm2 ohm4 freq period temp cap diode
cont`. Required fields: supply `voltage current output`; meter `reading`; thermometer
`temperature`. Types without `roles` still work with the API when their keys use the usual
names (`voltage`, `current`, `output`, `v_meas`, `i_meas`; `function`, `reading`, ...).

Adding a role changes the type, so it is a new version (the built-in E3631A, 6632A and 3457A
gained theirs in 1.1.0; instances created on 1.0.0 keep working by convention).

## Simulation

`sim` gives the simulator what it can't infer, so the type can be tested without hardware:

```yaml
sim:
  identity: 'HEWLETT-PACKARD,E3631A,0,1.6-5.0-1.0'   # answer to the identify probe
  readouts:
    v_meas: {follows: voltage, when: output}         # same channel's setting while `output` is on
    reading: {value: 1.2345678, noise: 0.0000005}
    status: {value: 1024, value_on: 1025, when: output}   # value_on while `output` is on
```
