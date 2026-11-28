# Device documentation

The sources behind each device type in `devices/types/`: the manuals used, where they came from,
and what was learned on the bench where a manual was missing or wrong. Each device has its own
folder with a `README.md` (GPIB notes) and, where a manual was used, a `manuals/` folder with the
file as downloaded.

| Device | Type | Bench address | Sources | Page |
|---|---|---|---|---|
| HP 3455A digital voltmeter | `hp.3455a` 1.0.0 | 1 | Operating and Service Manual (two scans); status bytes decoded on the bench | [hp-3455a](hp-3455a/README.md) |
| HP 3456A digital voltmeter | `hp.3456a` 1.0.0 | 17 | Quick reference card; Operating and Service Manual (read online); bench | [hp-3456a](hp-3456a/README.md) |
| HP 6632A system DC power supply | `hp.6632a` 1.4.0 | 5 | Operating Manual, Sections III to VI; bench | [hp-6632a](hp-6632a/README.md) |
| HP 8714ES RF network analyzer | `hp.8714es` 1.2.0 | 16 | No manual: every command checked on the analyzer itself | [hp-8714es](hp-8714es/README.md) |

Types made in earlier sessions name their manuals in the type file (`references:`) but no copy
was kept: Agilent E3631A (User's Guide, chapter 4) and HP 3457A (Operating, Programming and
Configuration Manual).

## Sources

| File | Device | Downloaded from |
|---|---|---|
| `hp-3455a/manuals/HP_3455A_Operating_and_Service_Manual_archive-org.pdf` | 3455A | https://dn760001.eu.archive.org/0/items/hp_3455a/3455a_text.pdf |
| `hp-3455a/manuals/HP_3455A_Operating_and_Service_Manual_qsl-net.pdf` | 3455A | https://www.qsl.net/n9zia/test/HP3455A_Service_Manual.pdf |
| `hp-3456a/manuals/HP_3456A_Quick_Reference_Hepperle_2016.pdf` | 3456A | http://www.mh-aerotools.de/hp/documents/HP%203456A%20-%20Quick-Reference.pdf |
| (read online, not downloaded) | 3456A | https://www.manualslib.com/manual/1303474/Hp-3456a.html (pages 57 and 307) |
| `hp-6632a/manuals/HP_6632A_33A_34A_Operating_Manual.pdf` | 6632A | https://xn--jyvskyl-7wae.hacklab.fi/wp-content/uploads/2025/10/HP_6632A_33A_34A_Operating_manual.pdf |
| (not downloaded; the scan above is clearer) | 6632A | https://www.keysight.com/tr/en/assets/9018-05839/user-manuals/9018-05839.pdf |

The manuals are copyright Hewlett-Packard / Keysight Technologies; the quick reference card is
by Martin Hepperle. They are kept here as references for this project's device types.

## Bench instruments

All on GPIB board 0 of the Raspberry Pi server. `bus.listeners` finds 1, 5, 16 and 17.

- 1: HP 3455A (DVM, instance `dmm1`)
- 5: HP 6632A (instance `bench-psu`)
- 16: HP 8714ES (instance `vna`)
- 17: HP 3456A (DVM, instance `dmm17`)

Safety rules followed when testing: no outputs turned on or energised by tests, and the meters
are never switched to ohms (the ohms functions push a test current into whatever is on the
input; on the 3455A that disturbed the connected circuit).
