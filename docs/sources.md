# Official references

PPK2 functions, limits, device behavior, and compatibility in this project
are based on Nordic Semiconductor official materials. This project is not
affiliated with or endorsed by Nordic Semiconductor ASA. No source code from
other projects is copied, translated, or incorporated; test vectors are
self-generated (see CONTRIBUTING.md).

Referenced materials (last reviewed 2026-08-19):

| Reference | Used for |
|---|---|
| [PPK2 product page / Get Started](https://www.nordicsemi.com/Products/Development-hardware/Power-Profiler-Kit-2/GetStarted) | product capabilities, official app pointers |
| [PPK2 User Guide](https://docs.nordicsemi.com/r/bundle/ug_ppk2/) | modes, source voltage range, usage |
| [Logic Port](https://docs.nordicsemi.com/r/bundle/ug_ppk2/page/ug/ppk/logic_port.html) | D0-D7 pinout, Logic VCC 1.65-5.5 V, level shifting |
| [Digital input resolution](https://docs.nordicsemi.com/r/bundle/ug_ppk2/page/ug/ppk/digital_input_resolution.html) | 100 kHz digital sampling, ~50 kHz bandwidth |
| [Measurement resolution](https://docs.nordicsemi.com/r/bundle/ug_ppk2/page/ug/ppk/ppk_measure_resolution.html) | per-range resolution table |
| [Measurement accuracy](https://docs.nordicsemi.com/r/bundle/ug_ppk2/page/ug/ppk/ppk_measure_accuracy.html) | per-range accuracy table |
| [Official Power Profiler app repository](https://github.com/NordicSemiconductor/pc-nrfconnect-ppk) | product behavior and compatibility reference |
| [Official app changelog](https://github.com/NordicSemiconductor/pc-nrfconnect-ppk/blob/main/Changelog.md) | firmware/app compatibility timeline |
| [Official app license](https://github.com/NordicSemiconductor/pc-nrfconnect-ppk/blob/main/LICENSE) | licensing boundary awareness |
| [PPK2 hardware downloads](https://www.nordicsemi.com/Products/Development-hardware/Power-Profiler-Kit-2/Download) | schematics (Logic Port pinout, FXMA108 level shifter) |

Facts recorded from these sources are summarized in `docs/protocol-spec.md`
(protocol and sample format), `docs/calibration.md` (conversion), and
`docs/logic-port.md` (wiring and limits).

Everything this project knows falls into one of three buckets, kept apart on
purpose:

- **Sourced** — from the materials above, cited where used.
- **Measured here** — recorded in `docs/protocol-spec.md` under "Hardware
  observations", with the firmware fingerprint of the unit it was seen on.
  These are this project's own measurements, not Nordic figures.
- **Neither** — listed in `docs/protocol-spec.md` under "Unverified items".
  Nothing is promoted out of that list without evidence.

This project does not distribute Nordic firmware binaries; firmware updates
go through Nordic's official tools.
