"""Async streaming with a rolling current summary.

Run without hardware:  python examples/async_stream.py --simulate
"""

import argparse
import asyncio

from ppk2lab import AsyncPPK2, GapEvent


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--device", help="device serial number")
    args = parser.parse_args()

    adev = await AsyncPPK2.open(serial_number=args.device, simulate=args.simulate)
    async with adev:
        calibration = adev.device.calibration
        vdd = adev.device.state.source_voltage_mv
        total = 0.0
        count = 0
        async for event in adev.stream(duration_s=1.0):
            if isinstance(event, GapEvent):
                print(f"! gap at {event.index}: {event.missing} samples missing")
                continue
            currents = calibration.convert_block(event, vdd)
            total += sum(currents)
            count += len(currents)
            if count and count % 20000 < len(currents):
                print(f"{count:>7} samples  running mean {total / count:9.3f} uA")
        print(f"done: {count} samples, mean {total / count:.3f} uA")


asyncio.run(main())
