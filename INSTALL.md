# Installation

`ppk2lab` requires Python 3.11 or newer. The only required runtime dependency
is `pyserial`.

## Python package

```bash
pipx install ppk2lab        # recommended for CLI use
# or inside a virtual environment:
python -m venv .venv && source .venv/bin/activate
pip install ppk2lab
```

> `0.1.0.dev0` is the only build published on PyPI today; install it with an
> exact pin: `pip install ppk2lab==0.1.0.dev0`. Plain `pip install ppk2lab`
> resolves nothing, because pre-releases are excluded by default and no stable
> release has been cut — the first will be `0.2.0`, after the hardware gates in
> `ROADMAP.md`. This repository is at `0.2.0.dev0` and carries work the
> published preview does not, so install from a source checkout (below) to
> follow it.

From a development checkout:

```bash
git clone https://github.com/tipoLi5890/ppk2lab
cd ppk2lab
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Verify the installation (no hardware needed):

```bash
ppk2lab --version
ppk2lab --simulate doctor
ppk2lab capabilities --json
```

`--simulate` runs every command against a built-in simulated PPK2. It is a
toolchain test aid, never a substitute for real measurements.

## USB and serial-port access

The PPK2 enumerates as a USB CDC ACM device (VID `0x1915`, PID `0xC00A`).
Firmware 1.2.0 and newer exposes a second (shell) serial port; `ppk2lab
discover` classifies the measurement port by USB interface number where the OS
reports one. Some platforms do not — macOS is the observed case — and there the
role stays `unknown` and `PPK2.open()` identifies the measurement port with a
read-only metadata probe instead of refusing to open. `--port PATH` skips both.

### Linux

Add your user to the serial group and re-login:

```bash
sudo usermod -aG dialout "$USER"    # Debian/Ubuntu
sudo usermod -aG uucp "$USER"       # Arch
```

Or install a udev rule:

```text
# /etc/udev/rules.d/71-ppk2.rules
SUBSYSTEM=="tty", ATTRS{idVendor}=="1915", ATTRS{idProduct}=="c00a", MODE="0666"
```

then `sudo udevadm control --reload && sudo udevadm trigger`.

### macOS

No driver is needed; the device appears as `/dev/cu.usbmodem*`. If the port
seems missing, check `System Information > USB` for the device and try
another cable.

`discover` shows both ports as `role: unknown` here, because macOS reports no
USB interface numbers for this device. That is expected, not a failure: the
measurement port is the one the read-only metadata probe answers on. Nothing
in the port name identifies it, so do not infer the role from the path.

### Windows

Windows 10+ binds the built-in CDC driver automatically; the device appears
as two `COMx` ports. If open fails with a permission error, close the
official Power Profiler app (it holds the port exclusively).

## Diagnosing problems

```bash
ppk2lab doctor --json
```

runs thirteen read-only checks (Python, `ppk2lab` and pyserial versions,
enumeration, device selection, port open, interrupted-session recovery, device
metadata, firmware fingerprint, the `Calibrated` flag, user gains, calibration
constants, and stream rate) and prints machine-readable remediation for each
failure. The stream-rate check is skipped unless you ask for it:
`ppk2lab doctor --stream-check 1s` starts a one-second measurement to verify
the 100 kS/s rate, and it never touches DUT power.

`doctor` exits with the exit code of the first failing check, so
`ppk2lab doctor --json || exit 1` is a working pre-flight. Checks that report
`warn` or `skip` stay non-blocking, and `ppk2lab --simulate doctor` always
exits 0.

After a capture process is killed, the next open drains whatever the device
was still streaming and `doctor` reports `session_recovery` as a warning
naming the byte count — 17,412 stale bytes after one SIGKILL mid-capture.
That is recovery working, not a fault.

Common failures:

| Symptom | Likely cause | Remediation |
|---|---|---|
| `DEVICE_NOT_FOUND` | cable/enumeration | reconnect; check `ppk2lab discover --json` |
| `PORT_BUSY` | official app or another process holds the port | close it and retry |
| `PERMISSION_DENIED` | Linux group membership | see udev/group instructions above |
| `METADATA_INVALID` | firmware/parse mismatch | retry; file a compatibility report |
| `CAPTURE_TOO_LARGE` | a long capture will not fit in RAM (the file is intact) | `ppk2lab inspect FILE.ppk2a`, `measure --window START:END`, or raise `--max-samples` |
| both ports show `role: unknown` (macOS) | the OS reports no USB interface numbers to classify them by | not an error; the measurement port is found by a read-only metadata probe. `--port PATH` pins one explicitly |
| `doctor` warns `calibrated_flag`: `device reports Calibrated: 0` | seen on a working unit whose five ranges all carry constants; unexplained | not blocking — conversion runs and the flag travels into every capture. Send the firmware fingerprint in a compatibility report |
| a capture reads near zero, with `W_DUT_POWER_UNKNOWN` | the DUT is not powered through the meter, and DUT power does not survive the command that enabled it | enable power and capture inside one open session (`ppk2lab.PPK2`); see `docs/cli-reference.md` |

## Claude Code plugin

After installing the CLI, add this repository as a marketplace inside
Claude Code and install the plugin:

```text
/plugin marketplace add tipoLi5890/ppk2lab
/plugin install ppk2lab@ppk2lab
```

During development, load the checkout directly with `claude --plugin-dir ./`.
Repository-scoped loose skills also work from `.claude/skills/` (symlink or
copy `skills/*` there).

## Codex and loose skills

`skills/` holds two skills — `ppk2lab-operate` (measuring with a PPK2) and
`ppk2lab-maintain` (working on this repository) — each a plain `SKILL.md` next
to a `references/` directory it loads from on demand. Codex can load them
directly. A Codex plugin manifest is not published yet — the loose-skill path
below is the supported route, and a manifest will be added once its format is
verified against the current Codex release.

For repository-scoped loose skills, copy `skills/*` into `.agents/skills/`.
For a user-wide installation:

```bash
mkdir -p ~/.agents/skills
cp -R skills/* ~/.agents/skills/
```

Installing a plugin or skill never enables DUT power, changes voltage, or
starts a capture; the skills only teach the agent how to use the CLI safely.

## CI usage

Unit tests and offline analysis need no hardware:

```bash
pip install -e ".[dev]"
pytest
ppk2lab --simulate doctor --json
ppk2lab --simulate capture --duration 200ms --output smoke.ppk2a
ppk2lab assert smoke.ppk2a --rule "p99_current < 1A" --format junit
```

Every one of those exits nonzero on failure, so no `|| exit 1` is needed. For
a real threshold prefer a percentile to `max_current`: range switches
accumulate as a capture runs longer, so a maximum drifts upward with capture
length while `p99_current` does not.

Hardware-in-the-loop jobs need a self-hosted runner with a PPK2 and a fixture
MCU attached. There is no such workflow in this repository: the release gates
that need real hardware are run by the maintainer on the bench, and
`ROADMAP.md` records which configurations have been covered.
