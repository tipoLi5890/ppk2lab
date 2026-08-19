"""In-memory PPK2 simulator and mock transport.

``SimulatedPPK2`` implements the documented command surface (start/stop,
DUT power, source voltage, mode, metadata, reset, user gain) and produces a
sample stream from a pluggable current/logic profile, with configurable
counter gaps and hot-unplug injection. It exists so that the full pipeline —
device API, capture artifacts, decoders, CLI — is testable without hardware.
Simulated data is for testing the toolchain, never a substitute for real
measurements.
"""

from __future__ import annotations

import sys
import time
from array import array

from ..calibration import ADC_REFERENCE_FACTOR, MICROAMP_PER_AMP
from ..errors import ProtocolError, TransportError
from ..protocol.commands import Opcode
from ..protocol.samples import ADC_MASK, ADC_MULTIPLIER, pack_sample
from ..testing.profiles import ConstantProfile, Profile
from ..types import VOLTAGE_MAX_MV, VOLTAGE_MIN_MV, Mode
from .base import Transport

#: Simulated full-scale current per range, following the official range spans.
FULL_SCALE_UA = (50.0, 500.0, 5_000.0, 50_000.0, 1_000_000.0)

_COMMAND_LENGTHS = {
    Opcode.START_MEASURING: 1,
    Opcode.STOP_MEASURING: 1,
    Opcode.SET_DUT_POWER: 2,
    Opcode.SET_SOURCE_VOLTAGE: 3,
    Opcode.SET_MODE: 2,
    Opcode.GET_METADATA: 1,
    Opcode.RESET: 1,
    Opcode.SET_USER_GAIN: 6,
}


def simulated_range_resistor(range_index: int) -> float:
    """Resistor constant that makes ``adc=full scale`` convert to the range max."""
    full_scale_amps = FULL_SCALE_UA[range_index] / MICROAMP_PER_AMP
    return ADC_MASK * ADC_MULTIPLIER * ADC_REFERENCE_FACTOR / full_scale_amps


def current_to_range_adc(current_ua: float) -> tuple[int, int]:
    """Choose the smallest range covering ``current_ua`` and quantize the ADC field."""
    current = max(current_ua, 0.0)
    for r, full_scale in enumerate(FULL_SCALE_UA):
        if current <= full_scale or r == len(FULL_SCALE_UA) - 1:
            adc = min(ADC_MASK, round(current / full_scale * ADC_MASK))
            return r, adc
    raise AssertionError("unreachable")


class SimulatedPPK2:
    """Behavioral model of one PPK2 measurement port."""

    def __init__(
        self,
        *,
        serial_number: str = "SIM0001",
        firmware_version: str = "1.2.4-sim",
        profile: Profile | None = None,
        gaps: dict[int, int] | None = None,
        rate_limit_hz: float | None = None,
        metadata_chunk_bytes: int | None = None,
        initial_vdd_mv: int = 3000,
        # Source Meter is what real hardware reported during validation
        # (metadata `mode: 2`), and it is the mode in which energy is
        # defensible, so the simulator models it by default.
        initial_mode: Mode = Mode.SOURCE,
        max_samples_per_read: int = 4096,
    ) -> None:
        self.serial_number = serial_number
        self.firmware_version = firmware_version
        self.profile = profile if profile is not None else ConstantProfile(100.0)
        #: timeline index -> number of missing samples starting there
        self.gaps = dict(gaps or {})
        #: Emit at most this many samples per second of wall clock. ``None``
        #: (the default) runs as fast as the consumer asks, which keeps tests
        #: quick; a value models a device streaming in real time, and a value
        #: below 100 kS/s models a starved host losing samples.
        self.rate_limit_hz = rate_limit_hz
        #: Split metadata replies into chunks of this size, reproducing the
        #: multi-read delivery seen on real serial stacks.
        if metadata_chunk_bytes is not None and metadata_chunk_bytes < 1:
            raise ValueError("metadata_chunk_bytes must be at least 1")
        self.metadata_chunk_bytes = metadata_chunk_bytes
        self._stream_started_at: float | None = None
        self._samples_emitted = 0
        self.vdd_mv = initial_vdd_mv
        self.mode = initial_mode
        self.dut_power = False
        self.measuring = False
        self.max_samples_per_read = max_samples_per_read
        self.command_log: list[tuple[Opcode, bytes]] = []
        self.unplugged = False
        self._sample_index = 0
        self._counter = 0
        self._pending = bytearray()
        self._out = bytearray()

    # -- host-side API -----------------------------------------------------
    def metadata_text(self) -> str:
        lines = [f"Calibrated: {1}"]
        for r in range(5):
            lines.append(f"R{r}: {simulated_range_resistor(r)!r}")
        for r in range(5):
            lines.append(f"GS{r}: 0.0")
        for r in range(5):
            lines.append(f"GI{r}: 1.0")
        for r in range(5):
            lines.append(f"O{r}: 0.0")
        for r in range(5):
            lines.append(f"S{r}: 0.0")
        for r in range(5):
            lines.append(f"I{r}: 0.0")
        for r in range(5):
            lines.append(f"UG{r}: 1.0")
        lines.append(f"VDD: {self.vdd_mv}")
        lines.append("HW: 2")
        lines.append(f"mode: {int(self.mode)}")
        lines.append("IA: 0")
        lines.append("END")
        return "\n".join(lines) + "\n"

    def unplug(self) -> None:
        """Simulate hot USB removal: all later I/O raises TransportError."""
        self.unplugged = True

    def handle_write(self, data: bytes) -> None:
        if self.unplugged:
            raise TransportError("simulated device was unplugged")
        self._pending.extend(data)
        while self._pending:
            opcode_byte = self._pending[0]
            try:
                opcode = Opcode(opcode_byte)
            except ValueError as exc:
                raise ProtocolError(
                    f"simulator received unknown opcode 0x{opcode_byte:02x}"
                ) from exc
            length = _COMMAND_LENGTHS[opcode]
            if len(self._pending) < length:
                return  # wait for the rest of the command
            payload = bytes(self._pending[1:length])
            del self._pending[:length]
            self._execute(opcode, payload)

    def _execute(self, opcode: Opcode, payload: bytes) -> None:
        self.command_log.append((opcode, payload))
        if opcode is Opcode.START_MEASURING:
            self.measuring = True
            self._stream_started_at = None
            self._samples_emitted = 0
        elif opcode is Opcode.STOP_MEASURING:
            self.measuring = False
        elif opcode is Opcode.SET_DUT_POWER:
            self.dut_power = payload[0] == 1
        elif opcode is Opcode.SET_SOURCE_VOLTAGE:
            mv = (payload[0] << 8) | payload[1]
            if not VOLTAGE_MIN_MV <= mv <= VOLTAGE_MAX_MV:
                raise ProtocolError(f"simulator got out-of-range voltage {mv} mV")
            self.vdd_mv = mv
        elif opcode is Opcode.SET_MODE:
            self.mode = Mode(payload[0])
        elif opcode is Opcode.GET_METADATA:
            self._out.extend(self.metadata_text().encode("ascii"))
        elif opcode is Opcode.RESET:
            self.measuring = False
            self.dut_power = False
            self._sample_index = 0
            self._counter = 0
            self._out.clear()
        elif opcode is Opcode.SET_USER_GAIN:
            pass  # accepted; simulator keeps unity gains

    def read(self, max_bytes: int, timeout_s: float = 0.1) -> bytes:
        if self.unplugged:
            raise TransportError("simulated device was unplugged")
        if self._out:
            take = min(max_bytes, len(self._out))
            if self.metadata_chunk_bytes is not None:
                take = min(take, self.metadata_chunk_bytes)
            chunk = bytes(self._out[:take])
            del self._out[:take]
            return chunk
        if not self.measuring:
            return b""
        n_samples = min(max_bytes // 4, self.max_samples_per_read)
        if self.rate_limit_hz is not None:
            now = time.monotonic()
            if self._stream_started_at is None:
                self._stream_started_at = now
            allowance = int((now - self._stream_started_at) * self.rate_limit_hz)
            n_samples = min(n_samples, max(0, allowance - self._samples_emitted))
            if n_samples <= 0:
                time.sleep(min(timeout_s, 0.01))
                return b""
        if n_samples <= 0:
            return b""
        self._samples_emitted += n_samples
        return self._generate(n_samples)

    def _generate(self, n_samples: int) -> bytes:
        words = array("I")
        for _ in range(n_samples):
            missing = self.gaps.pop(self._sample_index, None)
            if missing:
                self._sample_index += missing
                self._counter = (self._counter + missing) & 0x3F
            current_ua, logic = self.profile.sample(self._sample_index)
            range_index, adc = current_to_range_adc(current_ua)
            words.append(pack_sample(adc, range_index, self._counter, logic & 0xFF))
            self._counter = (self._counter + 1) & 0x3F
            self._sample_index += 1
        if sys.byteorder == "big":
            words.byteswap()
        return words.tobytes()


class MockTransport(Transport):
    """Transport bound to a :class:`SimulatedPPK2`."""

    def __init__(self, simulator: SimulatedPPK2 | None = None) -> None:
        self.simulator = simulator if simulator is not None else SimulatedPPK2()
        self._open = False

    def open(self) -> None:
        if self.simulator.unplugged:
            raise TransportError("simulated device was unplugged")
        self._open = True

    def close(self) -> None:
        self._open = False

    def write(self, data: bytes) -> None:
        if not self._open:
            raise TransportError("mock transport is not open")
        self.simulator.handle_write(data)

    def read(self, max_bytes: int, timeout_s: float = 0.1) -> bytes:
        if not self._open:
            raise TransportError("mock transport is not open")
        return self.simulator.read(max_bytes, timeout_s)

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def description(self) -> str:
        return f"simulated:{self.simulator.serial_number}"
