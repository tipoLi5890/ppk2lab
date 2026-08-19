"""Device API over the simulator: state changes, readback, restoration."""

import dataclasses
import gc

import pytest

from ppk2lab.errors import TransportError, UsageError, VoltageRangeError
from ppk2lab.protocol.samples import SampleBlock
from ppk2lab.types import DeviceState, GapEvent, Mode, VoltageBasis

from .conftest import open_simulated


def test_open_reads_metadata_and_state(sim_device):
    assert sim_device.metadata is not None
    assert sim_device.metadata.terminated
    assert sim_device.state.mode is Mode.SOURCE
    assert sim_device.state.source_voltage_mv == 3000
    # A voltage learned from metadata is the regulator setpoint, not a
    # value this session chose.
    assert sim_device.state.source_voltage_basis is VoltageBasis.DEVICE_METADATA
    assert sim_device.calibration is not None
    assert sim_device.calibration.missing_ranges() == []


def test_set_mode_with_readback(sim_device):
    change = sim_device.set_mode(Mode.AMPERE)
    assert change.applied and change.observed_after
    assert change.before["mode"] == "source"
    assert change.after["mode"] == "ampere"
    assert sim_device.transport.simulator.mode is Mode.AMPERE
    # Ampere mode has a precondition users get wrong; say it out loud.
    assert any("powered from its own supply" in w for w in change.warnings)


def test_set_voltage_with_readback(sim_device):
    change = sim_device.set_source_voltage_mv(3300)
    assert change.applied and change.observed_after
    assert change.after["source_voltage_mv"] == 3300
    assert sim_device.transport.simulator.vdd_mv == 3300
    # Configuring the voltage is a stronger basis than reading it back, and
    # the readback inside set_source_voltage_mv must not downgrade it.
    assert sim_device.state.source_voltage_basis is VoltageBasis.CONFIGURED_SOURCE


def test_voltage_validated_before_any_write(sim_device):
    log_before = list(sim_device.transport.simulator.command_log)
    with pytest.raises(VoltageRangeError):
        sim_device.set_source_voltage_mv(6000)
    assert sim_device.transport.simulator.command_log == log_before


def test_dry_run_changes_nothing(sim_device):
    simulator = sim_device.transport.simulator
    log_before = list(simulator.command_log)
    change = sim_device.set_mode(Mode.AMPERE, dry_run=True)
    assert not change.applied
    assert simulator.command_log == log_before
    assert simulator.mode is Mode.SOURCE
    change = sim_device.set_dut_power(True, dry_run=True)
    assert not change.applied
    assert simulator.dut_power is False


def test_dut_power_reports_unobservable_readback(sim_device):
    change = sim_device.set_dut_power(True)
    assert change.applied
    assert not change.observed_after
    assert any("read back" in w for w in change.warnings)
    assert sim_device.transport.simulator.dut_power is True


def test_close_restores_power_with_failsafe_warning():
    device = open_simulated()
    simulator = device.transport.simulator
    device.set_dut_power(True)
    changes = device.close()
    # initial power state was unknown -> fail-safe OFF with explicit warning
    assert simulator.dut_power is False
    assert any(any("fail-safe" in w for w in c.warnings) for c in changes)


def test_close_without_power_change_restores_nothing():
    device = open_simulated()
    simulator = device.transport.simulator
    log_len = len(simulator.command_log)
    device.close()
    assert len(simulator.command_log) == log_len


def test_metadata_refresh_refused_while_measuring(sim_device):
    sim_device.start_measuring()
    with pytest.raises(UsageError):
        sim_device.refresh_metadata()
    sim_device.stop_measuring()


def test_stream_respects_sample_limit(sim_device):
    events = list(sim_device.stream(sample_limit=1000))
    blocks = [e for e in events if isinstance(e, SampleBlock)]
    assert sum(len(b) for b in blocks) >= 1000
    assert not sim_device.state.measuring  # stopped after streaming


def test_stream_reports_injected_gaps():
    device = open_simulated(gaps={500: 7})
    try:
        events = list(device.stream(sample_limit=1200))
    finally:
        device.close()
    gaps = [e for e in events if isinstance(e, GapEvent)]
    assert len(gaps) == 1
    assert gaps[0].index == 500
    assert gaps[0].missing == 7


def test_unplug_interrupts_capture_preserving_data():
    device = open_simulated()
    simulator = device.transport.simulator
    original_read = simulator.read
    reads = {"n": 0}

    def flaky_read(max_bytes, timeout_s=0.1):
        reads["n"] += 1
        if reads["n"] > 3:
            raise TransportError("simulated device was unplugged")
        return original_read(max_bytes, timeout_s)

    simulator.read = flaky_read
    result = device.capture(duration_s=10.0)
    assert not result.complete
    assert result.interruption is not None
    assert result.capture is not None
    assert result.capture.stored_count > 0
    simulator.read = original_read
    device.close()


# -- stream lifecycle ------------------------------------------------------
def test_stream_iterator_is_iterable_without_a_with_block(sim_device):
    """Bare iteration keeps working; the context manager is an addition."""
    events = sim_device.stream(sample_limit=500)
    assert iter(events) is events
    blocks = [e for e in events if isinstance(e, SampleBlock)]
    assert sum(len(b) for b in blocks) >= 500
    assert not sim_device._stream_active


def test_with_block_releases_the_device_when_the_body_raises():
    """A stream owns the instrument, so leaving the block must release it
    even when the caller's own code fails and stores the traceback."""
    device = open_simulated()
    errors = []
    try:
        with device.stream(sample_limit=1_000_000) as events:
            for _event in events:
                raise RuntimeError("consumer failed")
    except RuntimeError as exc:
        errors.append(exc)  # a stored traceback keeps the iterator alive

    assert errors and device._stream_active is False
    assert device.state.measuring is False
    # The handle is usable again while the traceback is still referenced.
    assert device.capture(sample_limit=500).capture.stored_count >= 500
    device.close()


def test_named_iterator_held_by_a_traceback_is_released_by_close():
    """A device left holding a stream still closes cleanly and releases it."""
    device = open_simulated()
    errors = []

    def consume():
        events = device.stream(sample_limit=1_000_000)
        for _event in events:
            raise RuntimeError("consumer failed")

    try:
        consume()
    except RuntimeError as exc:
        errors.append(exc)

    assert device._stream_active is True  # the traceback still owns it
    device.close()
    assert device._stream_active is False
    errors.clear()
    gc.collect()  # finalizing the stale iterator afterwards must not raise


def test_iterator_dropped_before_the_first_event_releases_the_claim():
    """stream() claims the device before the first iteration, so an iterator
    that is abandoned unused must give the claim back rather than refuse
    every later command for a stream that never started."""
    device = open_simulated()
    events = device.stream(sample_limit=500)
    del events
    gc.collect()
    assert device._stream_active is False
    assert device.capture(sample_limit=200).capture.stored_count >= 200
    device.close()


def test_explicit_close_on_an_unstarted_iterator_starts_no_measurement():
    device = open_simulated()
    simulator = device.transport.simulator
    log_len = len(simulator.command_log)
    events = device.stream(sample_limit=500)
    events.close()
    events.close()  # idempotent
    assert simulator.command_log[log_len:] == []
    assert device._stream_active is False
    device.close()


def test_closing_a_spent_iterator_does_not_release_a_newer_stream():
    """Closing (or dropping) an iterator that is already finished must not
    hand away the device a later stream is holding."""
    device = open_simulated()
    spent = device.stream(sample_limit=200)
    assert list(spent)
    current = device.stream(sample_limit=1_000_000)
    next(current)
    spent.close()
    assert device._stream_active is True
    with pytest.raises(UsageError):
        device.capture(sample_limit=100)
    current.close()
    assert device._stream_active is False
    device.close()


def test_unplug_during_stream_reports_the_stream_interruption():
    """The hot-unplug error must reach the caller, not the drain failure it
    causes: a dead handle cannot be drained, and that symptom says nothing."""
    device = open_simulated()
    simulator = device.transport.simulator
    with pytest.raises(TransportError) as excinfo:
        for seen, _event in enumerate(device.stream(duration_s=2.0), start=1):
            if seen == 2:
                simulator.unplug()
    assert "stream interrupted" in str(excinfo.value)
    assert "Partial data was preserved" in excinfo.value.remediation
    assert device._stream_active is False
    # None, not 0: the port was gone, so nothing was drained and nothing was
    # verified quiet. 0 is reserved for a drain that actually ran.
    assert device.stale_bytes_after_stop is None


def test_drain_failure_after_a_clean_stream_is_reported():
    """With nothing in flight there is no error to protect, so a drain that
    fails on its own must surface instead of vanishing."""
    device = open_simulated()
    original = device.transport.read

    def read(max_bytes, timeout_s=0.1):
        # 65536 is the post-stop drain; the reader thread asks for less.
        if max_bytes == 65536:
            raise TransportError("port went away after stop")
        return original(max_bytes, timeout_s)

    device.transport.read = read
    with pytest.raises(TransportError, match="port went away after stop"):
        list(device.stream(sample_limit=500))
    assert device.stale_bytes_after_stop is None
    device.transport.read = original
    device.close()


def test_a_completed_drain_records_a_count_rather_than_an_absence():
    """0 must keep meaning "the channel was read until it stayed quiet". A
    drain that could not run records None, so no future consumer can read the
    absence of drained bytes as a positive verification."""
    device = open_simulated()
    assert device.stale_bytes_after_stop is None  # no stream has stopped yet
    assert list(device.stream(sample_limit=500))
    assert isinstance(device.stale_bytes_after_stop, int)
    device.close()


def test_capture_survives_unplug_and_replug_on_the_same_handle():
    """Hot unplug mid-capture, then replug: no hang, a typed interruption
    naming the real cause, and a usable handle afterwards."""
    device = open_simulated()
    simulator = device.transport.simulator
    assert list(device.stream(sample_limit=400))
    simulator.unplug()

    interrupted = device.capture(sample_limit=1_000_000)
    assert not interrupted.complete
    assert interrupted.interruption == {
        "reason": "transport_error",
        "detail": "simulated device was unplugged",
    }

    simulator.unplugged = False
    again = device.capture(sample_limit=500)
    assert again.capture.stored_count >= 500
    device.close()


# -- device state ----------------------------------------------------------
def test_device_state_cannot_be_written_from_outside(sim_device):
    """dut_power can never be read back from the device, so a host-side write
    to it would enter the capture manifest as a hardware claim nobody made."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        sim_device.state.dut_power = True
    with pytest.raises(AttributeError):
        sim_device.state = DeviceState(dut_power=True)
    assert sim_device.state.dut_power is None


def test_uncommanded_power_stays_unknown_in_the_capture_record(sim_device):
    result = sim_device.capture(sample_limit=1000)
    assert result.capture.meta.configuration["dut_power"] is None
    assert [w.code for w in result.warnings] == ["W_DUT_POWER_UNKNOWN"]


def test_confirmed_commands_still_update_state(sim_device):
    sim_device.set_dut_power(True)
    assert sim_device.state.dut_power is True
    sim_device.set_mode(Mode.AMPERE)
    assert sim_device.state.mode is Mode.AMPERE
    sim_device.set_source_voltage_mv(3300)
    assert sim_device.state.source_voltage_mv == 3300
    sim_device.start_measuring()
    assert sim_device.state.measuring is True
    sim_device.stop_measuring()
    assert sim_device.state.measuring is False


def test_reset_clears_state(sim_device):
    sim_device.set_dut_power(True)
    change = sim_device.reset()
    assert change.applied
    simulator = sim_device.transport.simulator
    assert simulator.dut_power is False


def test_open_recovers_interrupted_session():
    """Stale stream bytes from a previous session are drained at open."""
    from ppk2lab.device import PPK2
    from ppk2lab.transport.mock import MockTransport, SimulatedPPK2

    simulator = SimulatedPPK2()
    simulator._out.extend(b"\x55" * 1000)  # stale bytes left in the channel
    simulator.measuring = True  # device left measuring by a previous session
    device = PPK2.open(transport=MockTransport(simulator), simulate=True)
    try:
        assert device.recovered_stale_bytes >= 1000
        assert simulator.measuring is False  # stop was sent before metadata
        assert device.metadata is not None and device.metadata.terminated
    finally:
        device.close()


def test_open_probes_ambiguous_measurement_port(monkeypatch):
    """When port roles are unknown (e.g. macOS), open() identifies the
    measurement port with the read-only metadata probe."""
    import ppk2lab.device as device_mod
    from ppk2lab.device import PPK2
    from ppk2lab.transport.base import Transport
    from ppk2lab.transport.mock import MockTransport, SimulatedPPK2
    from ppk2lab.types import DeviceInfo, PortInfo, PortRole

    class ShellPortTransport(Transport):
        """Accepts writes, never answers — like the PPK2 shell port."""

        def __init__(self, path):
            self.path = path
            self._open = False

        def open(self):
            self._open = True

        def close(self):
            self._open = False

        def write(self, data):
            pass

        def read(self, max_bytes, timeout_s=0.1):
            return b""

        @property
        def is_open(self):
            return self._open

        @property
        def description(self):
            return self.path

    simulator = SimulatedPPK2()

    def factory(path):
        if path == "/dev/fake-shell":
            return ShellPortTransport(path)
        return MockTransport(simulator)

    info = DeviceInfo(
        serial_number="REAL01",
        vid=0x1915,
        pid=0xC00A,
        ports=(
            PortInfo(path="/dev/fake-shell", role=PortRole.UNKNOWN),
            PortInfo(path="/dev/fake-measure", role=PortRole.UNKNOWN),
        ),
    )
    monkeypatch.setattr(device_mod, "_transport_factory", factory)
    monkeypatch.setattr(device_mod, "discover", lambda: [info])

    device = PPK2.open(serial_number="REAL01")
    try:
        assert device.transport.description == "simulated:SIM0001"
        assert device.metadata is not None and device.metadata.terminated
    finally:
        device.close()
