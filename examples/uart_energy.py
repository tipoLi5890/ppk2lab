"""Decode UART frames and measure per-frame charge/energy.

Run without hardware:  python examples/uart_energy.py --simulate
"""

import argparse

import ppk2lab
from ppk2lab.analysis import measure_annotations
from ppk2lab.decoders import UARTDecoder, decode_capture

parser = argparse.ArgumentParser()
parser.add_argument("--simulate", action="store_true")
parser.add_argument("--device", help="device serial number")
parser.add_argument("--rx", default="D0")
parser.add_argument("--baud", type=int, default=9600)
args = parser.parse_args()

with ppk2lab.PPK2.open(serial_number=args.device, simulate=args.simulate) as dev:
    result = dev.capture(duration_s=0.5)

capture = result.capture
decoder = UARTDecoder(rx=args.rx, baud=args.baud)
annotations = decode_capture(capture, decoder)
frames = [a for a in annotations if a.kind == "frame"]
print(f"{len(frames)} UART frames decoded")

for entry in measure_annotations(capture, annotations, kinds=["frame"]):
    ann = entry["annotation"]
    meas = entry["measurement"]
    char = ann["fields"].get("char") or ann["fields"].get("byte")
    print(
        f"  {char!r:>6} samples {ann['start_sample']}-{ann['end_sample']}: "
        f"charge {meas['charge_uc'] * 1000:.3f} nC, "
        f"mean {meas['current_ua']['mean']:.2f} uA"
    )
