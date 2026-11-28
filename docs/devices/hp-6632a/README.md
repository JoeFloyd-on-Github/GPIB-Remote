# HP 6632A system DC power supply (20 V, 5 A)

Type `hp.6632a` 1.4.0. Bench unit at GPIB address 5, ROM `B01 B01`.

## Sources

- HP 6632A/6633A/6634A Operating Manual (`manuals/`): Section III (turn-on, front-panel
  annunciators), Section IV (output characteristic, unregulated state, NORMAL/FAST), Section V
  (front panel), Section VI (remote programming: Tables 6-1, 6-2, 6-4, 6-5, 6-6, 6-7). The scan
  is stored upside down (`/Rotate 180`) and has no text layer.
- The bench unit, for answers to queries and the status values it reports.

## Instruction set (Table 6-1)

| Command | Meaning | Range | Power-on |
|---|---|---|---|
| `VSET v` | voltage | 0-20.475 V (0.005 V) | 0 |
| `ISET a` | current | 0.02-5.1188 A (0.00125 A); lower values become 0.02 A, no error | 0.02 A |
| `OVSET v` | overvoltage trip | 0-22 V (0.1 V) | 22 V |
| `OCP 0/1` | overcurrent protection | | off |
| `OUT 0/1` | output | | on |
| `RST` | reset OV / OC after a trip | | |
| `CLR` | power-on state (same as GPIB device clear) | | |
| `UNMASK n` | fault mask | 0-4095 | 0 |
| `SRQ 0/1` | service request on fault | | off |
| `PON 0/1` | SRQ at power-on; non-volatile, once per power cycle | | last stored |
| `DLY s` | reprogramming delay, 4 ms steps | 0-32.767 s | 8 ms FAST, 80 ms NORMAL |
| `DSP 0/1` | front-panel display | | on |
| `CMODE`, `CDATA`, `CSAVE` | calibration (CSAVE once per power cycle) | | |

Queries: `VOUT?`, `IOUT?`, `STS?`, `ASTS?`, `FAULT?`, `ERR?`, `TEST?`, `ID?` (`HP6632A`),
`ROM?` (`B01 B01`). Register values come back right-aligned with leading spaces.

There is no query for any setting (`VSET?`, `ISET?`, `OVSET?`, `OCP?`, `OUT?`): on the bench
they return nothing and set error 8. The server shows settings as last sent ("assumed").

A GPIB device clear or `CLR` returns the power-on state, **which turns the output on** at 0 V.

## Status, accumulated status, fault and mask registers (Table 6-4)

| Bit | Weight | Name | Meaning |
|---|---|---|---|
| 0 | 1 | CV | constant voltage |
| 1 | 2 | +CC | constant current |
| 2 | 4 | UNR | unregulated: neither CV nor CC holds (low line, too much lead drop) |
| 3 | 8 | OV | overvoltage protection tripped (crowbar fired, output disabled) |
| 4 | 16 | OT | over-temperature; output comes back with its settings when cool |
| 5 | 32 | | not used |
| 6 | 64 | OC | overcurrent protection tripped (output disabled) |
| 7 | 128 | ERR | programming error; cleared by `ERR?` |
| 8 | 256 | INH | remote inhibit (option) |
| 9 | 512 | -CC | negative constant current (sinking) |
| 10 | 1024 | FAST | rear-panel switch in FAST |
| 11 | 2048 | NORM | rear-panel switch in NORMAL |

- `STS?` is the present status. CV, +CC or -CC is set only while the output is on and
  regulating, so the server infers the output state from it. On the bench: 1024 with the
  output off, 1025 (CV, FAST) with it on.
- `ASTS?` has every bit that was on since the last `ASTS?`, then resets to the present status.
- A fault bit is set when the status bit goes from 0 to 1 while the same mask bit is set;
  `FAULT?` reads and clears the fault register.
- `DLY` keeps CV, +CC, -CC and UNR out of the fault register and the OCP for that long after
  VSET, ISET, CLR, RST or OUT.

The type decodes `STS?` into an operating mode, one reading per bit and a list of bit names, and
`ASTS?` and `FAULT?` into bit names.

## Serial poll register (Table 6-5)

1 FAU (a fault register bit is set; cleared by `FAULT?`), 2 PON (set at power-on, cleared by
`CLR` / device clear), 16 RDY, 32 ERR, 64 RQS. A serial poll clears RQS and the SRQ annunciator.
The bench unit polls 16 (ready) normally.

## Error codes (Table 6-6)

0 none; 1 EEPROM save failed; 2 second PON after power-on; 4 second DC PON; 5 no relay option;
8 addressed to talk with nothing to say; 10 header expected; 11 unrecognized header; 20 number
expected; 21 number syntax; 22 number out of internal range; 30 comma expected; 31 terminator
expected; 41 parameter out of range; 42 voltage; 43 current; 44 overvoltage; 45 delay; 46 mask;
50 multiple CSAVE; 51 EEPROM checksum; 52 calibration mode disabled; 53 calibration channel;
54 calibration full scale; 55 calibration offset; 59 calibration disable jumper in.

## Self-test (Table 6-7)

`TEST?` runs the self-test without changing the output. 0 passed; 1-5 HP-IB circuit (ROM, RAM,
HP-IB chip, timer slow/fast); 11, 12, 14-24 power-supply interface (ROM, RAM, timer, A/D, CV/CC
zero, CV and CC reference, DAC); 51 EEPROM checksum. The bench unit answers 0.

## Notes

- Two quadrants: it sinks current as well as sourcing it (-CC); the negative limit tracks ISET.
- OVP senses at the output terminals, not the sense leads.
- OCP trips when the output goes from CV into CC while OCP is on. Clear the cause, then `RST`.
- FAST mode: faster programming, more noise, no internal output capacitor; `DSP 0` and fewer
  `VOUT?`/`IOUT?` lower the noise. The manual recommends NORMAL. The bench unit is in FAST.
- Syntax: commands end with LF, CR LF, `;` or EOI; spaces are allowed anywhere; negative
  numbers are an error except during calibration.
