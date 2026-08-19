"""Reproducible power assertion, as used in CI.

Run without hardware:  python examples/power_assertion.py --simulate
Exit code 0 = passed, 1 = failed (matching the CLI contract).
"""

import argparse
import sys

import ppk2lab
from ppk2lab.analysis import evaluate_assertion, parse_rule

parser = argparse.ArgumentParser()
parser.add_argument("--simulate", action="store_true")
parser.add_argument("--device", help="device serial number")
parser.add_argument(
    "--rule",
    default='after uart("TX_DONE"), within 20ms, avg_current < 10uA',
)
args = parser.parse_args()

with ppk2lab.PPK2.open(serial_number=args.device, simulate=args.simulate) as dev:
    result = dev.capture(duration_s=0.5)

rule = parse_rule(args.rule)
outcome = evaluate_assertion(result.capture, rule)
print(f"rule: {rule.source}")
print(f"status: {outcome.status}  events: {outcome.events_found}")
for obs in outcome.observations:
    print(
        f"  window {obs['window']['start_sample']}-{obs['window']['end_sample']}: "
        f"observed {obs['observed']} {obs['op']} {obs['threshold']} -> {obs['status']}"
    )
print(f"evidence: capture {outcome.capture_id} sha256 {outcome.capture_sha256[:16]}...")
sys.exit(0 if outcome.passed else 1)
