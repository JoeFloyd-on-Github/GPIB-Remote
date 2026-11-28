# HP 8714ES RF network analyzer (300 kHz - 3 GHz)

Type `hp.8714es` 1.2.0. Bench unit at GPIB address 16, firmware E.06.02.

## Sources

No manual was used. The type cites the HP 8712ES/8714ES Programmer's Guide, but every command
below was checked by sending it to the bench analyzer and reading `SYST:ERR?`. Error messages
quoted are the analyzer's own.

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
  `-221,"Settings conflict; Marker value query not valid on this type of measurement."` It works
  in polar and phase. Polling it in Smith format filled the error queue, which made later sets
  look as if they had failed; the type no longer polls it in Smith format, and the server empties
  the error queue before each set.
- `CALC<n>:MARK1:FUNC:RES?` in Smith format: `-221,"Marker result query not valid on this type
  of marker search."`; `CALC<n>:MARK1:DATA?` is an undefined header (-113).
- Compound commands with `;:` work and are handy for testing without other traffic in between,
  e.g. `*CLS;:CALC2:FORM SMIT;:SYST:ERR?;:CALC2:FORM?`.
