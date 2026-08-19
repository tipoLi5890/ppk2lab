"""Trigger detectors and the pre/post windowing engine."""

import pytest

from ppk2lab.decoders.base import decode_capture
from ppk2lab.decoders.spi import SPIDecoder
from ppk2lab.decoders.uart import UARTDecoder
from ppk2lab.errors import UsageError
from ppk2lab.protocol.samples import SampleBlock
from ppk2lab.testing.profiles import StepProfile
from ppk2lab.testing.signals import merge_logic, spi_wave, uart_wave
from ppk2lab.triggers.engine import (
    CurrentThresholdTrigger,
    DigitalEdgeTrigger,
    DigitalPatternTrigger,
    SpiContentTrigger,
    TriggerEngine,
    UartContentTrigger,
    parse_trigger_spec,
)

from .conftest import open_simulated, wave_capture

SPB = 100000 / 9600
SPI_CH = {"sclk": 1, "mosi": 2, "miso": 3, "cs": 4}


def run_trigger(profile, detector, *, pre=0, post=100, samples=5000, gaps=None):
    device = open_simulated(profile, gaps=gaps)
    try:
        engine = TriggerEngine(
            detector,
            pre_samples=pre,
            post_samples=post,
            calibration=device.calibration,
            vdd_mv=device.state.source_voltage_mv,
        )
        out: list = []
        for event in device.stream(sample_limit=samples):
            out.extend(engine.process(event))
            if engine.done:
                break
        return engine, out
    finally:
        device.close()


def stored_range(events):
    blocks = [e for e in events if isinstance(e, SampleBlock)]
    return blocks[0].start_index, blocks[-1].end_index


def test_current_trigger_with_pre_post_window():
    profile = StepProfile([(1000, 10.0, 0), (1000, 20000.0, 0)], repeat=True)
    detector = CurrentThresholdTrigger(1000.0, direction="above", hold_samples=3)
    engine, events = run_trigger(profile, detector, pre=200, post=400)
    assert engine.fired and engine.fire_index == 1000
    lo, hi = stored_range(events)
    assert (lo, hi) == (800, 1400)


def test_current_trigger_below_direction():
    profile = StepProfile([(500, 5000.0, 0), (500, 5.0, 0)], repeat=True)
    detector = CurrentThresholdTrigger(100.0, direction="below")
    engine, _ = run_trigger(profile, detector, post=50)
    assert engine.fire_index == 500


def test_digital_edge_trigger():
    profile = StepProfile([(300, 100.0, 0), (300, 100.0, 0x08)], repeat=True)
    detector = DigitalEdgeTrigger(3, rising=True)
    engine, _ = run_trigger(profile, detector, post=50)
    assert engine.fire_index == 300


def test_digital_pattern_trigger_with_hold():
    profile = StepProfile([(100, 100.0, 0x00), (5, 100.0, 0x05), (100, 100.0, 0x00)], repeat=True)
    detector = DigitalPatternTrigger(mask=0x0F, value=0x05, hold_samples=3)
    engine, _ = run_trigger(profile, detector, post=20)
    assert engine.fire_index == 100


def drive(capture, detector, *, pre=100, post=100):
    engine = TriggerEngine(
        detector,
        pre_samples=pre,
        post_samples=post,
        calibration=capture.calibration,
        vdd_mv=capture.source_voltage_mv,
    )
    for event in capture.iter_events():
        engine.process(event)
        if engine.done:
            break
    return engine


def test_uart_content_trigger():
    wave = uart_wave(b"BOOT DONE", 9600, idle_before=500)
    logic = bytes(b | 0x00 for b in wave)  # D0 carries UART
    detector = UartContentTrigger(UARTDecoder(rx="D0", baud=9600), b"DONE")
    engine = drive(wave_capture(logic), detector)
    assert engine.fired
    # fires at the end of the final 'E' frame
    assert engine.fire_index > 500 + 8 * 104


def test_uart_trigger_does_not_fire_before_confirmed_idle():
    """``capture --trigger`` starts mid-stream, with no gap involved.

    Until a whole frame time of idle proves where the frames are, every
    candidate frame is a guess and must not arm a trigger.
    """
    wave = uart_wave(b"BOOT DONE", 9600, idle_before=0, idle_after=0)
    capture = wave_capture(bytes(wave))
    decoder = UARTDecoder(rx="D0", baud=9600)
    engine = drive(capture, UartContentTrigger(decoder, b"DONE"), pre=0)
    assert not engine.fired
    # nothing the decoder produced here is offered as a byte at all
    frames = [
        a for a in decode_capture(capture, UARTDecoder(rx="D0", baud=9600)) if a.kind == "frame"
    ]
    assert frames and all("unsynced" in f.errors for f in frames)


def test_uart_trigger_pattern_must_be_consecutive_frames():
    """A corrupted frame between the pattern bytes is a discontinuity.

    The trigger may not stitch ``DO`` and ``NE`` around the frame the line
    actually carried between them.
    """
    idle = 116  # one frame time at 9600 8E1 (11 bit times)
    bad = list(uart_wave(b"X", 9600, parity="even", idle_before=0, idle_after=0))
    for i in range(round(9 * SPB), round(10 * SPB)):
        bad[i] ^= 1  # parity bit inverted; framing and length untouched
    wave = list(uart_wave(b"DO", 9600, parity="even", idle_before=idle, idle_after=0))
    wave += bad
    wave += list(uart_wave(b"NE", 9600, parity="even", idle_before=0, idle_after=idle))

    decoder = UARTDecoder(rx="D0", baud=9600, parity="even")
    engine = drive(wave_capture(bytes(wave)), UartContentTrigger(decoder, b"DONE"))
    assert not engine.fired

    # the same four bytes with nothing between them do fire
    clean = uart_wave(b"DONE", 9600, parity="even", idle_before=idle, idle_after=idle)
    decoder = UARTDecoder(rx="D0", baud=9600, parity="even")
    assert drive(wave_capture(bytes(clean)), UartContentTrigger(decoder, b"DONE")).fired


def spi_logic(*transactions):
    """One CS transaction per argument, back to back."""
    waves: dict[str, list[int]] = {"sclk": [], "mosi": [], "miso": [], "cs": []}
    for words in transactions:
        part = spi_wave(list(words), clock_hz=10_000, mode=0)
        for name, wave in waves.items():
            wave.extend(part[name])
    return merge_logic(
        {SPI_CH[name]: wave for name, wave in waves.items()},
        idle_levels={SPI_CH["cs"]: 1},
    )


def test_spi_trigger_pattern_must_be_one_transaction():
    """0x9f then 0x00 in two exchanges is not the JEDEC ID read it looks like."""
    detector = SpiContentTrigger(SPIDecoder(sclk="D1", mosi="D2", cs="D4"), [0x9F, 0x00])
    engine = drive(wave_capture(spi_logic([0x9F], [0x00])), detector, pre=0)
    assert not engine.fired

    detector = SpiContentTrigger(SPIDecoder(sclk="D1", mosi="D2", cs="D4"), [0x9F, 0x00])
    engine = drive(wave_capture(spi_logic([0x9F, 0x00])), detector, pre=0)
    assert engine.fired


def test_gap_resets_hold_and_edge_state():
    profile = StepProfile([(1000, 10.0, 0), (1000, 20000.0, 0)], repeat=True)
    detector = CurrentThresholdTrigger(1000.0, direction="above", hold_samples=50)
    engine, _events = run_trigger(profile, detector, post=100, gaps={1010: 30})
    # the hold run restarts after the gap: fire at 1040, not 1000
    assert engine.fire_index == 1040


def test_engine_validates_configuration():
    detector = CurrentThresholdTrigger(1000.0)
    with pytest.raises(UsageError):
        TriggerEngine(detector, pre_samples=0, post_samples=100, calibration=None)
    with pytest.raises(UsageError):
        TriggerEngine(DigitalEdgeTrigger(0), pre_samples=-1, post_samples=10)


def test_parse_trigger_specs():
    assert parse_trigger_spec("current>10mA").describe()["type"] == "current"
    assert parse_trigger_spec("current<5uA").describe()["direction"] == "below"
    assert parse_trigger_spec("digital D3 rising").describe()["channel"] == "D3"
    spec = parse_trigger_spec("digital mask=0x0f value=0x05").describe()
    assert (spec["mask"], spec["value"]) == (15, 5)
    uart = parse_trigger_spec('uart D0 9600 "BOOT"').describe()
    assert uart["pattern"] == "BOOT"
    spi = parse_trigger_spec("spi 0x9f,0x00", spi_config={"sclk": "D1", "mosi": "D2"})
    assert spi.describe()["pattern"] == ["0x9f", "0x00"]
    with pytest.raises(UsageError):
        parse_trigger_spec("whenever something happens")
    with pytest.raises(UsageError):
        parse_trigger_spec("spi 0x9f")  # missing channel config
