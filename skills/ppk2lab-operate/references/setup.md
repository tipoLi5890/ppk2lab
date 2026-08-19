# Setup and environment repair (read-only)

Nothing here changes hardware state.

1. `ppk2lab --version` — if missing, install: `pipx install ppk2lab`, or from
   a checkout `pip install -e ".[dev]"` (see the repo's INSTALL.md).
2. `ppk2lab doctor --json` — diagnose first; apply each failing check's
   `remediation`, then re-run doctor to confirm.
3. Common repairs:
   - `PERMISSION_DENIED` (Linux): add user to dialout/uucp or install the
     udev rule for VID 1915 / PID c00a, then replug.
   - `PORT_BUSY`: close the official Power Profiler app or other serial
     clients.
   - `DEVICE_NOT_FOUND`: reseat cable, then `ppk2lab discover --json`.
   - Port roles `unknown` (common on macOS): the driver probes the
     measurement port automatically with a read-only metadata query; only
     pass `--port` explicitly if the probe fails.
4. Interrupted previous sessions (device left streaming) are recovered
   automatically at open (stop + drain); doctor reports it as
   `session_recovery`.
5. Optional stream verification (measurement only, never DUT power):
   `ppk2lab doctor --stream-check 1s --json` — expect `stream_rate` and
   `stream_gaps` to pass.

Done when: doctor has 0 failing checks (explain any warnings), or every
remaining failure is reported with its remediation. Don't retry more than
twice without changing something.
