# HP 3455A digital voltmeter

Type `hp.3455a` 1.0.0. Bench unit at GPIB address 1.

## Sources

- HP 3455A Operating and Service Manual, Section III: Table 3-4 (program codes) and Table 3-5
  (binary program codes). Two scans in `manuals/`: archive.org (`3455a_text.pdf`, has a text
  layer) and qsl.net (`HP3455A_Service_Manual.pdf`). See [../README.md](../README.md) for URLs.
- The bench unit, for the status byte layout (the manual's table is hard to read in both scans).

## Remote control

No front-panel step is needed: addressing the meter as a listener (with REN) puts it in remote.
`gpibctl local 1` returns it to the front panel. There is no identity query.

Readings: address it to talk; with a GET first the reading is fresh. Readings end in CR LF.

## Program codes (Table 3-4)

| Setting | Codes |
|---|---|
| Function | F1 DC volts, F2 AC volts, F3 fast AC volts, F4 2-wire kohm, F5 4-wire kohm, F6 test |
| Range | R1 0.1, R2 1, R3 10, R4 100, R5 1000, R6 10000 (kohm only), R7 autorange |
| Trigger | T1 internal, T2 external, T3 hold / manual |
| Math | M1 scale (X-Z)/Y, M2 % error (X-Y)/Y, M3 off |
| Auto-cal | A0 off, A1 on |
| High resolution | H0 off, H1 on (6.5 digits, about 1 s per reading) |
| Data-ready SRQ | D0 off, D1 on |
| Math constants | `EY<n>SY` stores n in Y, `EZ<n>SZ` stores n in Z |

## Reading the setup back: B (binary program)

Sending `B` makes the meter send four status characters once, then CR LF. It also raises SRQ
(serial poll 68, binary program error); a serial poll clears it. The type follows the query with
a serial poll (`clear_srq: true`).

Decoded on the bench unit by changing one setting at a time:

| Byte | Meaning | Values |
|---|---|---|
| 1 | Math | 59 `;` off, 62 `>` scale, 61 `=` % error |
| 2 | Bits, active low | bit 0 internal trigger, bit 1 external, bit 2 hold, bit 3 high resolution on, bit 4 autorange on; bit 6 (64) set while auto-cal is on (bit 5 set when it is off) |
| 3 | Range (one bit low) | 62 0.1, 61 1, 59 10, 55 100, 47 1000, 95 10000 |
| 4 | Function (one bit low) | 62 DCV, 61 ACV, 59 fast ACV, 55 2-wire kohm, 47 4-wire kohm |

Examples from the bench unit: `;N=>` = math off; internal trigger, autorange, auto-cal on, high
resolution off; range 1; DC volts. `;F=>` is the same with high resolution on (bit 3 low).
`>C=>` has math scale and trigger hold.

So the whole configuration can be read back, and the server shows it as read, not assumed.

## Server behaviour

- `restore: true`: the server keeps the last settings it sent and reapplies them when the meter
  is found again after being off (presence checked by serial poll every 5 s).
- Ohms functions put a test current through whatever is connected; tests stay in DC volts.
