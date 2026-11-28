# HP 3456A digital voltmeter

Type `hp.3456a` 1.0.0. Bench unit at GPIB address 17.

## Sources

- HP 3456A quick reference card, Martin Hepperle, June 2016 (`manuals/`).
- HP 3456A Operating and Service Manual, Section III, Table 3-9 (program codes), read online at
  manualslib (pages 57 and 307); not downloaded. See [../README.md](../README.md) for URLs.
- The bench unit, for register read-back and triggered readings.

## Remote control

Addressing it as a listener puts it in remote; `gpibctl local 17` returns it to the front panel.
There is no identity query. After a register recall (`RE<x>`) it stops sending readings until it
is triggered, so the type always triggers (GET) and then reads.

## Program codes

| Setting | Codes |
|---|---|
| Function | S0F1 DCV, S0F2 ACV, S0F3 AC+DC V, S0F4 2-wire kohm, S0F5 4-wire kohm; S1F1-S1F3 ratios (DC/DC, AC/DC, AC+DC/DC), S1F4 / S1F5 offset-compensated ohms |
| Range | R1 autorange; R2-R6 100 mV-1000 V; R2-R9 100 ohm-1 Gohm |
| Trigger | T1 internal, T2 external, T3 single, T4 hold |
| Autozero | Z0 off, Z1 on |
| AC filter | FL0 off, FL1 on |
| Math | M0 off, M1 pass/fail, M2 statistics, M3 null, M4 thermistor F, M5 thermistor C, M6 scale, M7 % error, M8 dB |
| Display | D0 off, D1 on |
| Home (power-on state) | H |
| Front/rear switch | SW1 (answers 1 or 0) |

Registers: `<value>ST<reg>` stores, `RE<reg>` recalls (answers the value). Registers: N readings
per trigger, G digits, I integration (power-line cycles), D delay, Y and Z math, L and U limits,
R, M, V, C (statistics).

## What can be read back

- The registers: yes (`RE<reg>`). On the bench: N 1, G 5, I 10, D 0, Y 1, Z 0, L -1.999999E15,
  U 1.999999E15.
- Function, range, trigger, autozero, filter and math: no. The serial poll status byte holds no
  configuration (bits: 1 front-panel SRQ, 2 program memory done, 4 data ready, 8 trigger too
  fast, 16 syntax / illegal state, 32 program memory error, 64 RQS, 128 limit failure). The
  server shows these as the last value set ("assumed") and reapplies them when the meter comes
  back (`restore: true`).
