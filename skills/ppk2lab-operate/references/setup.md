# Setup and environment repair (read-only)

Nothing here changes hardware state.

1. `ppk2lab --version` — if missing, install per the repo's INSTALL.md.
   Only a development preview is on PyPI today, so plain
   `pip install ppk2lab` resolves nothing; an exact pin
   (`pip install ppk2lab==0.1.0.dev0`) or an editable checkout
   (`pip install -e ".[dev]"`) is what works. Check the version before
   trusting anything here: this skill describes the current repository, and
   commands it names — `inspect`, `--max-samples`, `--window`,
   `--state-threshold` — do not exist in the `0.1.0.dev0` preview. On an
   older build they fail with a usage error rather than doing something
   surprising.
2. `ppk2lab doctor --json` — diagnose first; apply each failing check's
   `remediation`, then re-run doctor to confirm. Doctor **exits nonzero on
   a failing check** (`warn` and `skip` stay non-blocking, and
   `--simulate doctor` still exits 0), so it works as a pre-flight gate in
   a script rather than something whose output has to be parsed. Each
   failing check also carries the `exit_code` it maps to.
3. Checks worth knowing by name:
   - `device_selection` — `warn` when several devices are attached and no
     `--device`/`--port` was given. The run is still valid evidence, but it
     is evidence about one unit; name which.
   - `metadata_read` — `fail` (exit 5) when the device's metadata cannot be
     read at all. Without it there are no calibration constants, so raw
     samples cannot become current.
   - `firmware_fingerprint` — HW revision, IA, metadata key count, port
     count. The measurement port reports no version string, so this
     fingerprint is the only identity a result can be attributed to; record
     it with every compatibility report. Also in `ppk2lab info --json`.
   - `calibrated_flag`, `user_gain`, `calibration` — whether absolute
     accuracy is confirmed, whether a non-unity gain is scaling every
     reading, and which ranges have constants.
4. Common repairs:
   - `PERMISSION_DENIED` (Linux): add user to dialout/uucp or install the
     udev rule for VID 1915 / PID c00a, then replug.
   - `PORT_BUSY`: close the official Power Profiler app or other serial
     clients.
   - `DEVICE_NOT_FOUND`: reseat cable, then `ppk2lab discover --json`.
   - Port roles `unknown` (common on macOS): the driver probes the
     measurement port automatically with a read-only metadata query; only
     pass `--port` explicitly if the probe fails.
5. Interrupted previous sessions (device left streaming) are recovered
   automatically at open (stop + drain); doctor reports it as
   `session_recovery`.
6. Learn the vocabulary from the tool, not from memory:
   `ppk2lab capabilities --json` publishes every command and error code
   plus three coded catalogs in one shape — `warning_codes` (`W_*`),
   `gap_reasons` (each one's `category` says where the loss happened), and
   `interruption_reasons` (why a capture ended early). All three are open,
   so an unfamiliar code means "look it up", not "ignore".
7. Optional stream verification (measurement only, never DUT power):
   `ppk2lab doctor --stream-check 1s --json` — adds `stream_rate` and
   `stream_gaps`; expect both to pass.

Done when: doctor exits 0 (explain any warnings), or every remaining
failure is reported with its remediation. Don't retry more than twice
without changing something.
