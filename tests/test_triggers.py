"""Trigger detectors and the pre/post windowing engine."""

import pytest

from ppk2lab.decoders.uart import UARTDecoder
from ppk2lab.errors import UsageError
from ppk2lab.protocol.samples import SampleBlock
from ppk2lab.testing.profiles import StepProfile
from ppk2lab.testing.signals import uart_wave
from ppk2lab.triggers.engine import (
    CurrentThresholdTrigger,
    DigitalEdgeTrigger,
    DigitalPatternTrigger,
    TriggerEngine,
    UartContentTrigger,
    parse_trigger_spec,
)

from .conftest import open_simulated, wave_capture


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


def test_uart_content_trigger():
    wave = uart_wave(b"BOOT DONE", 9600, idle_before=500)
    logic = bytes(b | 0x00 for b in wave)  # D0 carries UART
    profile_capture = wave_capture(logic)
    decoder = UARTDecoder(rx="D0", baud=9600)
    detector = UartContentTrigger(decoder, b"DONE")
    engine = TriggerEngine(
        detector,
        pre_samples=100,
        post_samples=100,
        calibration=profile_capture.calibration,
        vdd_mv=profile_capture.source_voltage_mv,
    )
    fired = []
    for event in profile_capture.iter_events():
        fired.extend(engine.process(event))
        if engine.done:
            break
    assert engine.fired
    # fires at the end of the final 'E' frame
    assert engine.fire_index > 500 + 8 * 104


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
