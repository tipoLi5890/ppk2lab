"""Decode SPI transactions and measure their energy.

Run without hardware:  python examples/spi_transaction.py --simulate
(the simulated device emits one mode-0 transaction per 100 ms activity cycle
on D3=SCLK, D4=MOSI, D5=MISO, D6=CS)
"""

import argparse

import ppk2lab
from ppk2lab.analysis import measure_annotations
from ppk2lab.decoders import SPIDecoder, decode_capture

parser = argparse.ArgumentParser()
parser.add_argument("--simulate", action="store_true")
parser.add_argument("--device", help="device serial number")
args = parser.parse_args()

with ppk2lab.PPK2.open(serial_number=args.device, simulate=args.simulate) as dev:
    result = dev.capture(duration_s=0.3)

capture = result.capture
decoder = SPIDecoder(
    sclk="D3", mosi="D4", miso="D5", cs="D6", mode=0, expected_clock_hz=5000
)
annotations = decode_capture(capture, decoder)

for entry in measure_annotations(capture, annotations, kinds=["transaction"]):
    ann = entry["annotation"]
    meas = entry["measurement"]
    mosi = [f"0x{v:02x}" for v in ann["fields"]["mosi_words"]]
    miso = [f"0x{v:02x}" for v in ann["fields"]["miso_words"]]
    print(
        f"transaction {ann['start_sample']}-{ann['end_sample']}: "
        f"mosi {mosi} miso {miso} "
        f"charge {meas['charge_uc']:.4f} uC energy {meas['energy_uj']:.4f} uJ"
    )
