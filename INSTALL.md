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

> The package is not yet published to PyPI; until the `0.1.0` release, install
> from a source checkout (below).

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
discover` classifies the measurement port by USB interface number.

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

### Windows

Windows 10+ binds the built-in CDC driver automatically; the device appears
as two `COMx` ports. If open fails with a permission error, close the
official Power Profiler app (it holds the port exclusively).

## Diagnosing problems

```bash
ppk2lab doctor --json
```

runs read-only checks (Python, pyserial, enumeration, device metadata,
calibration) and prints machine-readable remediation for each failure.
`ppk2lab doctor --stream-check 1s` optionally starts a one-second measurement
to verify the 100 kS/s stream rate; it never touches DUT power.

Common failures:

| Symptom | Likely cause | Remediation |
|---|---|---|
| `DEVICE_NOT_FOUND` | cable/enumeration | reconnect; check `ppk2lab discover --json` |
| `PORT_BUSY` | official app or another process holds the port | close it and retry |
| `PERMISSION_DENIED` | Linux group membership | see udev/group instructions above |
| `METADATA_INVALID` | firmware/parse mismatch | retry; file a compatibility report |

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

Open the Codex plugin browser with `/plugins`, add the repository
marketplace, and install `ppk2lab`. Exact non-interactive commands will be
verified against the current Codex release before the `0.1.0` release notes
are frozen.

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
ppk2lab --simulate capture --duration 200ms --output smoke.ppk2a
ppk2lab assert smoke.ppk2a --rule "max_current < 1A" --format junit
```

Hardware-in-the-loop jobs should run on a self-hosted runner with a PPK2 and
a fixture MCU attached; see ROADMAP.md for the compatibility matrix gates.
