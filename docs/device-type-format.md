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
  check_after_set: true  # empty the queue before every set, read it after; an error fails the set
  codes: {42: voltage out of range}   # optional: meanings added to reported errors
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
  output is on. `infer` also takes the `cases` form below.
- **readback** (bool and enum controls without `get`): the setting is encoded in a status
  readout, and decoding it is the real setting, reported with source `read`. A get reads the
  readout; a set is followed by reading it again, so the reply shows what the instrument took.

  ```yaml
  readback:
    readout: setup        # parse: text (bytes of a status string) or int
    byte: 2               # 1-based character of a text readout; omit to use the int value
    cases:                # first match wins; each case may give its own byte
      - {clear: 16, value: AUTO}          # all these bits clear
      - {byte: 3, equals: 62, value: '1'} # exactly this value
      - {set: 64, value: 'on'}            # all these bits set;  any: n = any of them set
    default: 'off'        # optional; otherwise no match leaves the value unknown
  ```

  Values must be options of the enum (or `on`/`off`). The HP 3455A decodes its whole setup from
  the four bytes `B` makes it send.
- **rereads**: `[format, marker_value]` reads those settings or readouts again (same channel)
  after a set, for an instrument that changes them along with this one. The 8714ES brings back
  the format last used for a kind of measurement when the measurement changes.
- **restore** (per control, default true): `restore: false` keeps a control out of the saved
  settings (see *Restoring settings*).
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
    parse: number         # number (first number in the text) | int | bool | text | enum | list | {regex, as}
    poll: 3s              # polled at this interval while someone is subscribed; omit for on demand
  reading:
    read: true            # talk-only: no command, just address the instrument to talk
    trigger: get          # optional: send GPIB GET first
```

`parse: {regex: 'VOUT\s+(\S+)', as: number}` applies the regex first and parses group 1.
A readout can be **decoded** from another one instead of queried, for status registers whose
bits mean different things. Decoding happens on every reading of the source (polled ones too);
a get reads the source.

```yaml
  mode:
    parse: text           # text or bool
    decode:
      readout: status     # the source; parse: int (or text, with byte)
      cases:              # as in readback: equals / set / clear / any, first match wins
        - {set: 8, value: 'OV tripped'}
        - {set: 1, value: CV}
      default: 'Off'
  cv: {parse: bool, decode: {readout: status, cases: [{set: 1, value: 'on'}], default: 'off'}}
  status_bits:
    parse: text
    decode: {readout: status, bits: {1: CV, 2: +CC, 1024: FAST}}   # names of the set bits: "CV, FAST"
```

`unless: {format: [SMIT]}` skips a readout while a control (same channel) has one of those values:
it isn't polled, and a get is refused with a message, for a query the instrument refuses in that
mode (8714ES marker value in Smith format).
`parse: list` splits on commas into a list of numbers (a trace). `clear_srq: true` follows the
query with a serial poll, for an instrument that raises SRQ when asked for its status (3455A `B`).

## Actions

```yaml
actions:
  reset:
    label: Reset (*RST)
    send: ['*RST']        # one or more commands, may use {channel} on per-channel actions
    confirm: true         # clients ask before running it
    refresh: true         # read all settings back afterwards
    resets: true          # returns the instrument to power-on settings: forget the saved settings
    assumes: {voltage: 0, output: 'on'}   # settings known afterwards (shown as assumed)
```

## Restoring settings

```yaml
restore: true             # top level; default false
```

With `restore: true` the server remembers the last value set through it for each control (in
`<data_dir>/state/<instance>.json`). It checks every 5 s that the instrument is there with a serial
poll (which doesn't put it in remote), and when it comes back after being off or absent the
server reapplies the remembered settings in the type's control order, then sends a `restored`
event. Use it for instruments that forget their setup at power-off and can't report it. An action
with `resets: true` clears the saved settings; an instance can opt out with `restore: false`.

## Display

A type can describe a graphical display for the web client. The only kind so far is a network
analyzer (the HP 8714ES):

```yaml
display:
  kind: network_analyzer
  formatted: trace        # readout, parse: list: one value per point, in the display format
  complex: complex        # readout, parse: list: real, imaginary pairs (Smith and polar)
  start: start            # controls giving the sweep (per channel)
  stop: stop
  points: points
  format: format          # enum: MLOG, PHAS, SMIT, POL, ... picks the plot
  enabled: enabled        # optional: which channels are shown
  scale: scale            # optional: per division, reference value and position
  ref_level: ref_level
  ref_position: ref_position
  measurement: measurement
  marker: marker          # optional marker 1: on/off, frequency, value
  marker_freq: marker_freq
  marker_value: marker_value
```

Every name refers to one of the type's own controls or readouts, and the server checks them.

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
    ranges:                     # named ranges: {function: {range option: full scale}} (optional)
      dcv: {R2: 0.1, R3: 1, R4: 10}
    autocal: autocal_ohms       # action: self-calibration
    reset: reset                # action
  thermometer:                  # Thermometer: read() in C
    temperature: temp           # readout (required)
    unit: C                     # C, F or K (default C)
```

With `ranges`, `configure(range=5)` picks the smallest range that covers 5 in the present
function, and `range="auto"` uses an `AUTO` range option when there is no autorange control.

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
    trace: {sweep: points}                 # a list, one value per point of the `points` control
    complex: {sweep: points, complex: true}   # real, imaginary pairs
  state: {points: 201, format: MLOG}       # starting values of controls
```
