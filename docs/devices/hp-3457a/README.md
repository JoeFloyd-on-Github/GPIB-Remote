# HP 3457A multimeter (7.5 digits)

Type `hp.3457a` 1.2.0. Bench instance `dmm3457` at GPIB address 23. It has been offline so far,
so nothing in the type has been tried on the meter yet (`verified: false`).

## Sources

- **HP 3457A Operating Manual**, part 03457-90003, May 1986 (`manuals/`). It is a scan with an OCR
  text layer, copied from https://github.com/MikeDombo/HP3457A-GPIB, where it is named
  `HP 3457A Operating.pdf`. It has the command reference (chapter 4) and an abbreviated
  specification table (table 1-4).
- **HP 3457A Service Manual**, part 03457-90012, edition 3, February 1988. This is also an OCR'd
  scan from the same repository (`HP 3457A Service.pdf`, 37.5 MB). It isn't kept here because of
  its size. Its table 1-1 has the full specifications, at every NPLC, for 24 hours, 90 days and
  1 year. These are what the uncertainty readout uses.
- **MikeDombo/HP3457A-GPIB.** A 2018 Python program (wxPython GUI) that drives the 3457A through
  a Prologix adapter. It showed three things that type 1.2.0 now uses:
  - `NPLC?` works.
  - `RMATH HIRES` returns the extra digit.
  - An overload reads `1E+38`.

  Its accuracy tables aren't used. Its DC tables are the 1-year figures, several lookups have
  typos, its ACV 20-45 Hz figure is 0.62% where the manual has 0.56%, and the AC frequency is
  fixed at 60 Hz. The repository has no license.

## Commands (from the manual)

- **Identity:** `ID?` answers `HP3457A`. `END ALWAYS` makes the meter assert EOI on every reading.
- **Functions:** `DCV`, `ACV`, `ACDCV`, `OHM`, `OHMF`, `DCI`, `ACI`, `ACDCI`, `FREQ` and `PER`.
  Choosing a function without a range parameter turns autorange on.
- **Range:** `RANGE <max expected value>` picks a fixed range. `ARANGE ON/OFF` turns autorange on
  or off.
- **Integration:** `NPLC` takes 0.0005, 0.005, 0.1, 1, 10 or 100, which give 3.5, 4.5, 5.5, 6.5,
  7.5 and 7.5 digits. The meter rounds any other value up. At power-on it is 10, after `PRESET`
  1.
- **Extra digit:** after a reading at NPLC 10 or 100, `RMATH HIRES` returns the extra digit, and
  adding it to the reading gives 7.5 digits. This works for every function except extended ohms
  (300 Mohm and 3 Gohm), frequency and period.
- **Expected AC frequency:** `ACBAND <Hz>` gives the signal frequency for AC, frequency and
  period measurements. Below 400 Hz the meter uses its slow filter (1 s delay), from 400 Hz the
  fast one (100 ms). After a reset it is 20.
- **Overload:** over HP-IB, an overload reads `1E+38`, shown as `OVLD` on the front panel.
- **Other settings:**
  - `AZERO ON/OFF/ONCE`: autozero.
  - `TERM FRONT/REAR`: input terminals (also 0, 1, 2).
  - `OCOMP ON/OFF`: offset compensation, on the 30 ohm to 3 kohm ranges only.
  - `DISP ON/OFF`: the front display.
  - `ACAL ALL/OHMS/AC`: auto-calibration.
- **Triggering:** `TRIG SGL` takes one reading and returns it, then leaves the meter in
  trigger-hold. `TRIG AUTO` runs freely again.
- **Queries, not yet used by the type:** `FUNC?`, `NPLC?`, `RANGE?`, `AZERO?`, `OCOMP?` and
  `TERM?`. The manual documents these query forms; they could replace the assumed values once
  their answers have been seen on the meter.
- **Reset:** `RESET` restores the power-on state. That is DCV, autorange, NPLC 10, ACBAND 20,
  autozero on, front terminals, OCOMP off and the display on; it also sets `END OFF`, so the type
  sends `END ALWAYS` again.

## Uncertainty readout (type 1.2.0)

`uncertainty` is the 90-day accuracy of each reading, taken from the Service Manual's table 1-1.
That is Tcal +/- 5 C, after a 1 hour warm-up. It is % of reading plus counts, where a count is
the last digit at the resolution in use: range / 3,000,000 at 6.5 digits (NPLC 1, 10 and 100),
range / 300,000 at 5.5 digits (NPLC 0.1), and so on.

**Examples:**
- 2.5 V on the 3 V range: 0.0017% x 2.5 V + 6 x 1 uV = 48.5 uV at NPLC 100, and 49.5 uV at NPLC
  10.
- 1 V AC at 60 Hz, NPLC 1: 0.21% + 1120 x 1 uV = 3.22 mV.

**What the tables say:**
- **DC volts, DC current and ohms:** one column per NPLC. The Operating Manual's table 1-4 has
  the NPLC 100 column only, and it agrees with this one.
- **2-wire ohms:** the counts include a 200 mohm lead allowance. That is 20315 counts of 10 uohm
  on the 30 ohm range, against 315 for 4-wire.
- **Extended ohms (300 Mohm and 3 Gohm):** 2-wire only, after ohms auto-cal, and the table has
  no figure at NPLC 0.0005. There the readout is blank.
- **AC:** the figures assume AC auto-cal (`ACAL AC`) within the last week and sine waves above
  10% of range. Without auto-cal, add 0.6% of reading (and, for AC+DC, 39000 counts at 6.5
  digits). Between 3% and 10% of range, below 20 kHz, add 0.7% for ACV.
- **AC rows:** the row is picked by *Expected AC frequency* (`ac_band`). From 400 Hz the meter is
  in its fast mode, which has its own row up to 20 kHz. AC ranges read to 108% (32.5 mV on the
  30 mV range).
- **Frequency and period:** 0.05% from 10 to 400 Hz (100 ms to 2.5 ms), and 0.01% from 400 Hz to
  1.5 MHz (2.5 ms to 667 ns). The Operating Manual calls these 1-year figures.
- **1 year:** the 1-year tables have the same counts and a larger % of reading. For example, DCV
  on the 3 V range is 0.0025% against 0.0017%, and DCI is 0.04% against 0.02%. The readout gives
  the 90-day figures only.
- **The two manuals disagree on AC volts:** the Operating Manual's table 1-4 has, for example,
  0.75% + 2800 counts for ACV at 20-45 Hz, where the Service Manual has 0.56% + 1120. The type
  uses the Service Manual, which is the later edition. AC current agrees in both.

**How the range is found:** the type uses the range set through the server while it was set
after the function and after autorange was last turned on. Otherwise it uses the smallest range
that holds the reading. With autorange on, the meter may sit on the next range up for a while
after the input changes, so the figure can be a little optimistic then.

**Reading the OCR:** the OCR text needed care. Where it was unclear, the figures were checked
against the pattern between tables. For example, the 90-day ACV figures are the 24-hour ones
plus 0.06%, which confirms 0.56 for 20-45 Hz. The OCR's "0.2.1" is 0.25 and ".9.1" is 0.95.

## Not yet done

- Try the type on the meter: settings, `TRIG SGL` timing at NPLC 100, HIRES, `ACBAND`, and the
  query forms.
