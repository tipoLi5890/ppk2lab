"""Capture current + D0-D7 and save a canonical artifact.

Run without hardware:  python examples/basic_capture.py --simulate
With hardware:         python examples/basic_capture.py [--device SERIAL]

The capture never enables DUT power; configure the device explicitly first
(see the safety notes in README.md) if your DUT is powered by the PPK2.
"""

import argparse

import ppk2lab

parser = argparse.ArgumentParser()
parser.add_argument("--simulate", action="store_true")
parser.add_argument("--device", help="device serial number")
parser.add_argument("--output", default="basic_capture.ppk2a")
args = parser.parse_args()

with ppk2lab.PPK2.open(serial_number=args.device, simulate=args.simulate) as dev:
    print(f"device: {dev.info.serial_number}  state: {dev.state.to_json()}")
    result = dev.capture(duration_s=1.0, output=args.output, overwrite=True)

stats = result.stats
print(f"complete: {result.complete}")
print(f"stored samples: {stats.stored_samples}  missing: {stats.missing_samples_known}")
print(f"mean current: {stats.mean_ua:.3f} uA  peak: {stats.max_ua:.3f} uA")
energy = "n/a (no supply voltage known)" if stats.energy_uj is None else f"{stats.energy_uj:.3f} uJ"
print(f"charge: {stats.charge_uc:.3f} uC  energy: {energy}  ({stats.voltage_basis})")
print(f"artifact: {result.path}  sha256: {result.capture_sha256[:16]}...")
