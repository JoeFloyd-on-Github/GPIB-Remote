# HP 8714ES RF network analyzer (300 kHz - 3 GHz)

Type `hp.8714es` 1.3.0 (1.2.0 before the Programmer's Guide). Bench unit at GPIB address 16, firmware E.06.02.

## Sources

- Agilent 8712ET/ES and 8714ET/ES Programmer's Guide, part 08714-90015, June 2000, for firmware
  E.06.00 and above (`manuals/`, Keysight document 9018-40026). It has a text layer. Added after
  the type was written: type 1.2.0 was built without it.
- The bench analyzer: every command below was checked by sending it and reading `SYST:ERR?`.
  Error messages quoted are the analyzer's own.

## Identity and errors

- `*IDN?` answers in quotes: `"HEWLETT-PACKARD,8714ES,US39230371,E.06.02"`.
- `SYST:ERR?` answers `+0,"No error"` or `<code>,"<message>"`. The queue holds a limited number
  of entries; past that it reports `-350,"Too many errors"`. `*CLS` empties it.
- `FORM:DATA?` is `ASC`; the hardcopy language is PCL.
- The status registers hold no configuration; every setting has its own query.

## Commands used (every one also has a `?` query)

| Setting | Command |
|---|---|
| RF output | `OUTP ON/OFF` |
| Continuous sweep | `INIT1:CONT ON/OFF`; single sweep `ABOR;INIT1` |
| Channel on | `SENS<n>:STAT ON/OFF` (n = 1 or 2) |
| Measurement | `SENS<n>:FUNC "XFR:S 1,1"` (S11), `"XFR:S 2,1"` (S21), `"XFR:S 1,2"` (S12), `"XFR:S 2,2"` (S22) |
| Format | `CALC<n>:FORM MLOG/MLIN/PHAS/GDEL/SWR/SMIT/POL/REAL/IMAG` |
| Frequency | `SENS<n>:FREQ:STAR/STOP/CENT/SPAN` |
| Power | `SOUR<n>:POW` |
| Points | `SENS<n>:SWE:POIN` |
| IF bandwidth | `SENS<n>:BWID` |
| Averaging | `SENS<n>:AVER ON/OFF`, `SENS<n>:AVER:COUN` |
| Correction | `SENS<n>:CORR:STAT` |
| Scale | `DISP:WIND<n>:TRAC:Y:PDIV`, `:RLEV`, `:RPOS`; autoscale `:AUTO ONCE` |
| Marker 1 | `CALC<n>:MARK1 ON/OFF`, `:X`, `:Y?`, `:MAX`, `:MIN` |
| Sweep time | `SENS<n>:SWE:TIME?` |
| Preset | `SYST:PRES` |

## Traces

- `TRAC? CH<n>FDATA`: formatted data, one value per point (1601 points in about 1.2 s).
- `TRAC? CH<n>SDATA`: complex data, real and imaginary pairs (3202 values in about 1.9 s).
- Reading a trace doesn't start a sweep; with the sweep held (`INIT1:CONT OFF`) it returns the
  last one.

## Behaviour found on the bench

- **Smith chart only on reflection.** `CALC<n>:FORM SMIT` on S21 or S12 is refused:
  `-221,"Settings conflict; Smith Chart format is only valid when measuring Reflection. Use
  Polar format wh..."` (the analyzer cuts the message off). Polar works on both.
- **The format follows the measurement.** The analyzer keeps one format for reflection and one
  for transmission; changing the measurement brings back the one for its kind (S21 -> MLOG,
  then S11 -> SMIT). So change the measurement first, then the format; the type re-reads the
  format after a measurement change.
- **No marker value in Smith format.** `CALC<n>:MARK1:Y?` answers 0 and queues
  `-221,"Settings conflict; Marker value query not valid on this type of measurement."` In polar
  format it answers 0 with no error (an earlier note here said it worked there; it doesn't). It
  works in phase and the other rectangular formats. Polling it in Smith format filled the error queue, which made later sets
  look as if they had failed; the type no longer polls it in Smith format, and the server empties
  the error queue before each set.
- `CALC<n>:MARK1:FUNC:RES?` in Smith format: `-221,"Marker result query not valid on this type
  of marker search."`; `CALC<n>:MARK1:DATA?` is an undefined header (-113).
- Compound commands with `;:` work and are handy for testing without other traffic in between,
  e.g. `*CLS;:CALC2:FORM SMIT;:SYST:ERR?;:CALC2:FORM?`.

## From the Programmer's Guide

Chapter references are to the guide.

- **Smith and polar traces are pairs in both arrays** (ch. 6). In those formats
  `TRAC? CH<n>FDATA` returns real, imaginary pairs, like `SDATA`; on the bench, 401 points gave
  802 values from both. `FDATA` is the displayed data: after trace math (data/memory) and
  electrical delay. `SDATA` is the corrected data before them, so the two differ when either is on.
- **Binary transfers are much faster** (Table 6-1): 1601 points take 82 ms with
  `FORM:DATA REAL,32` against 1200 ms in ASCII (201 points: 23 vs 164 ms). The answer is a
  definite-length block (`#<n><length><bytes>`); `FORM:BORD SWAP` gives little-endian. `FORM:DATA`
  is a global setting, so a controller that uses it should set it before each transfer.
  `CALC<n>:DATA?` is the same as `TRAC? CH<n>FDATA`.
- **Overlapped commands** (ch. 2): the commands that change frequency, measurement, points, IF
  bandwidth, averaging, power, channel state, correction, `OUTP`, `INIT`, `ABOR` and `SYST:PRES` don't
  hold off the commands after them. The guide says to follow them with `*OPC?` (or `*WAI`). To take a
  complete sweep: `ABOR;:INIT1:CONT OFF;:INIT1;*OPC?`, then read the trace; reading during a
  continuous sweep works but may mix two sweeps.
- **Marker readings in Smith and polar** (Table 11-2): `CALC<n>:MARK1:Y:RES?`, `:REAC?` and
  `:IND?` (Smith); `:Y:MAGN?` and `:Y:PHAS?` (polar). `CALC<n>:MARK1:GDEL?` reads group delay.
  `CALC<n>:MARK:BWID -3` then `CALC<n>:MARK:FUNC:RES?` returns bandwidth, centre, Q and loss.
- **IF bandwidth takes six values**: 15 (fine), 250 (narrow), 1200 (medium narrow), 3700
  (medium), 4000 (medium wide), 6500 (wide) Hz.
- **Points**: 3, 5, 11, 21, 51, 101, 201, 401, 801 or 1601.
- **Formats**: also `MIMP` (impedance magnitude). `CALC<n>:FORM:UNIT:MLOG` and `:MLIN` choose dBm,
  dBV, ... and W, V, ...
- **Status registers** (ch. 5), all with `:CONDition?`, `[:EVENt]?`, `:ENABle`,
  `:PTRansition` and `:NTRansition`:
  - `STAT:OPER:MEAS` bit 0 / 1: channel 1 / 2 measuring.
  - `STAT:OPER:AVER` bit 0 / 1: channel 1 / 2 has reached its averaging factor.
  - `STAT:OPER`: 0 calibrating, 1 settling, 4 measuring, 7 correcting, 8 averaging,
    9 hardcopy, 10 test, 14 IBASIC program running.
  - `STAT:QUES`: 9 limit fail, 10 data questionable (a setting changed; new data needed).
  - `STAT:QUES:LIM`: 0 / 1 channel 1 / 2 limit failed, 2 / 3 marker limit failed.
  - Status byte: 2 device, 3 questionable, 4 message available, 5 standard event, 6 RQS / MSS,
    7 operational. `*ESR?` bits: 0 operation complete, 2 query, 3 device, 4 execution and
    5 command error, 7 power on.
- **Other settings**: `DISP:FORM SING|ULOW` (overlaid or split channels), `SENS<n>:SWE:TIME:AUTO`,
  `SENS<n>:CORR:IMP:INP:MAGN` (Smith chart Z0), `CALC<n>:MATH (IMPL/CH<n>SMEM)` (data / memory),
  `DISP:WIND<n>:TRAC1|2` (data and memory traces on or off), limit lines (`CALC<n>:LIM:...`).

## What type 1.3.0 does with it

- Waits for `*OPC?` after every overlapped setting before reading it back, and after a single
  sweep (`ABOR`, `INIT1:CONT OFF`, `INIT1`).
- Reads traces in binary: `FORM:DATA REAL,32;:FORM:BORD NORM;:TRAC? CH<n>FDATA;:FORM:DATA ASC`,
  so the analyzer is back in ASCII after each transfer.
- The web display draws Smith and polar charts from the formatted trace (what the analyzer shows),
  and shows the marker as R + jX (Smith) or magnitude and phase (polar), read from the analyzer.
- IF bandwidth and points are lists of the allowed values; impedance magnitude (`MIMP`) format.
- Status readouts: measuring, averaging complete, data questionable / limit fail, limit test.

Checked on the bench unit (E.06.02) with type 1.3.0:

- Binary traces: 1601 points in 0.4 s through the server (about 1.2 s in ASCII); 401 Smith
  points as 802 values; `FORM:DATA?` is `ASC,+12` afterwards and the error queue is empty.
- `*OPC?` after a setting answers in 0.1-0.6 s, also in continuous sweep with averaging on.
- A single sweep (`ABOR;:INIT1:CONT OFF;:INIT1;*OPC?`) took 5.1 s with both channels on (2.3 s
  sweeps), with averaging factors 64 and 16: it takes one sweep, not a full average. A much slower
  sweep (15 Hz IF bandwidth, 1601 points) could take longer than the 30 s `complete` timeout; the
  bus is busy while it waits.
- Marker in Smith: `Y:RES?`, `Y:REAC?`, `Y:IND?`; in polar: `Y:MAGN?`, `Y:PHAS?`.

