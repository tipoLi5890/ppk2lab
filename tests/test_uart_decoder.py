"""UART decoder golden vectors (self-generated) and edge cases."""

import pytest

from ppk2lab.decoders.base import LogicChunk
from ppk2lab.decoders.feasibility import Tier, uart_feasibility
from ppk2lab.decoders.uart import UARTDecoder, uart_bytes, uart_runs
from ppk2lab.errors import DecoderRateError
from ppk2lab.testing.signals import uart_wave
from ppk2lab.types import SAMPLE_RATE_HZ, GapEvent


def confirmed_idle(baud, *, data_bits=8, parity=None, stop_bits=1):
    """Samples of continuous idle the decoder needs before it claims alignment.

    One whole frame time: every bit of a frame except the start bit can be
    high at once, so a shorter high run can occur with no idle at all.
    """
    frame_bits = 1 + data_bits + (1 if parity else 0) + stop_bits
    return round(frame_bits * SAMPLE_RATE_HZ / baud)


def decode_wave(wave, decoder, chunk=None):
    logic = bytes(wave)  # channel D0 carries the wave (bit 0)
    annotations = []
    if chunk is None:
        annotations += decoder.feed(LogicChunk(0, logic))
    else:
        for i in range(0, len(logic), chunk):
            annotations += decoder.feed(LogicChunk(i, logic[i : i + chunk]))
    annotations += decoder.flush()
    return annotations


def frames_of(annotations):
    return [a for a in annotations if a.kind == "frame"]


@pytest.mark.parametrize("baud", [1200, 2400, 4800, 9600])
def test_clean_bytes_all_validated_bauds(baud):
    payload = bytes(range(256))
    wave = uart_wave(payload, baud, idle_before=confirmed_idle(baud))
    decoder = UARTDecoder(rx="D0", baud=baud)
    annotations = decode_wave(wave, decoder)
    data, ends = uart_bytes(annotations)
    assert data == payload
    assert all(not f.errors for f in frames_of(annotations))
    assert all(f.confidence == 1.0 for f in frames_of(annotations))
    assert ends == sorted(ends)


def test_long_stream_no_phase_drift():
    # 500 bytes at 9600 baud (10.417 samples/bit) must decode without drift
    payload = bytes((i * 37 + 11) & 0xFF for i in range(500))
    wave = uart_wave(payload, 9600, idle_before=confirmed_idle(9600))
    decoder = UARTDecoder(rx="D0", baud=9600)
    data, _ = uart_bytes(decode_wave(wave, decoder))
    assert data == payload


@pytest.mark.parametrize("chunk", [1, 3, 7, 64, 1024])
def test_chunk_boundaries_do_not_change_results(chunk):
    payload = b"chunk boundary test \x00\xff\x55"
    wave = uart_wave(payload, 9600, idle_before=confirmed_idle(9600))
    decoder = UARTDecoder(rx="D0", baud=9600)
    data, _ = uart_bytes(decode_wave(wave, decoder, chunk=chunk))
    assert data == payload


@pytest.mark.parametrize("chunk", [1, 5, 37, 4096])
def test_idle_run_split_across_chunks_still_confirms_sync(chunk):
    """The confirmed-idle run is counted over the timeline, not per feed."""
    payload = b"split"
    wave = uart_wave(payload, 9600, idle_before=confirmed_idle(9600))
    decoder = UARTDecoder(rx="D0", baud=9600)
    annotations = decode_wave(wave, decoder, chunk=chunk)
    assert uart_bytes(annotations)[0] == payload
    assert all(not f.errors for f in frames_of(annotations))


@pytest.mark.parametrize("parity", ["even", "odd"])
def test_parity_decodes_and_detects_errors(parity):
    payload = b"PARITY"
    idle = confirmed_idle(9600, parity=parity)
    wave = uart_wave(payload, 9600, parity=parity, idle_before=idle)
    decoder = UARTDecoder(rx="D0", baud=9600, parity=parity)
    data, _ = uart_bytes(decode_wave(wave, decoder))
    assert data == payload

    # decoding with the opposite parity flags errors on odd-weight bytes
    wrong = "odd" if parity == "even" else "even"
    decoder = UARTDecoder(rx="D0", baud=9600, parity=wrong)
    annotations = decode_wave(uart_wave(payload, 9600, parity=parity, idle_before=idle), decoder)
    bad = [f for f in frames_of(annotations) if "parity" in f.errors]
    assert bad
    assert all(f.confidence == 0.0 for f in bad)


def test_framing_error_detected():
    # A stop bit forced low: build 0x41 then overwrite its stop-bit samples
    idle = confirmed_idle(9600)
    wave = uart_wave(b"\x41", 9600, idle_before=idle, idle_after=40)
    spb = 100000 / 9600
    stop_start = idle + round(9 * spb)
    stop_end = idle + round(10 * spb)
    for i in range(stop_start, stop_end):
        wave[i] = 0
    decoder = UARTDecoder(rx="D0", baud=9600)
    annotations = decode_wave(wave, decoder)
    frames = frames_of(annotations)
    assert frames and frames[0].errors == ["framing"]
    assert frames[0].confidence == 0.0
    data, _ = uart_bytes(annotations)
    assert data == b""  # frames with errors never join the byte stream


def test_inverted_signal():
    payload = b"inv"
    wave = uart_wave(payload, 9600, invert=True, idle_before=confirmed_idle(9600))
    decoder = UARTDecoder(rx="D0", baud=9600, invert=True)
    data, _ = uart_bytes(decode_wave(wave, decoder))
    assert data == payload


def test_msb_first_and_word_sizes():
    wave = uart_wave(
        [0x2A], 9600, data_bits=7, msb_first=True, idle_before=confirmed_idle(9600, data_bits=7)
    )
    decoder = UARTDecoder(rx="D0", baud=9600, data_bits=7, msb_first=True)
    frames = frames_of(decode_wave(wave, decoder))
    assert frames[0].fields["value"] == 0x2A
    assert not frames[0].errors

    idle = confirmed_idle(9600, data_bits=9, stop_bits=2)
    wave = uart_wave([0x1AB], 9600, data_bits=9, stop_bits=2, idle_before=idle)
    decoder = UARTDecoder(rx="D0", baud=9600, data_bits=9, stop_bits=2)
    frames = frames_of(decode_wave(wave, decoder))
    assert frames[0].fields["value"] == 0x1AB
    assert not frames[0].errors


def test_break_detected():
    spb = 100000 / 9600
    wave = [1] * 30 + [0] * round(15 * spb) + [1] * 30
    decoder = UARTDecoder(rx="D0", baud=9600)
    annotations = decode_wave(wave, decoder)
    breaks = [a for a in annotations if a.kind == "break"]
    assert len(breaks) == 1
    assert breaks[0].start_sample == 30
    assert not frames_of(annotations)


def test_break_clears_sync():
    """A break holds the line low across a frame; alignment does not survive it."""
    spb = 100000 / 9600
    idle = confirmed_idle(9600)
    head = uart_wave(b"A", 9600, idle_before=idle, idle_after=0)
    tail = uart_wave(b"B", 9600, idle_before=20, idle_after=idle)
    wave = list(head) + [0] * round(15 * spb) + list(tail)
    decoder = UARTDecoder(rx="D0", baud=9600)
    annotations = decode_wave(wave, decoder)
    frames = frames_of(annotations)
    assert frames[0].fields["char"] == "A" and not frames[0].errors
    after_break = [f for f in frames if f.start_sample > len(head)]
    assert after_break and all("unsynced" in f.errors for f in after_break)
    assert uart_bytes(annotations)[0] == b"A"


def test_gap_mid_frame_invalidates_frame():
    idle = confirmed_idle(9600)
    wave = uart_wave(b"AB", 9600, idle_before=idle)
    logic = bytes(wave)
    decoder = UARTDecoder(rx="D0", baud=9600)
    annotations = []
    # samples [150, 180) of the timeline are lost inside frame 'A'; frame 'B'
    # follows back-to-back with only a stop bit of idle
    annotations += decoder.feed(LogicChunk(0, logic[:150]))
    annotations += decoder.notify_gap(GapEvent(index=150, missing=30))
    annotations += decoder.feed(LogicChunk(180, logic[180:]))
    annotations += decoder.flush()
    errors = [a for a in annotations if a.kind == "error"]
    assert errors and "gap" in errors[0].errors
    assert errors[0].confidence == 0.0
    data, _ = uart_bytes(annotations)
    # frame 'A' was cut by the gap and frame 'B' follows without a confirmed
    # idle run: the decoder must decode NOTHING rather than fabricate bytes
    assert data == b""


@pytest.mark.parametrize("cut", range(110, 930, 7))
def test_no_clean_frame_between_a_gap_and_the_next_confirmed_idle(cut):
    """Wherever a gap lands in a back-to-back train, nothing after it is clean.

    A gap erases the evidence that located the frame boundaries, and the
    train never idles again, so no post-gap frame may claim to be a byte.
    """
    payload = b"ABCDEFGH"
    wave = bytes(uart_wave(payload, 9600, idle_before=confirmed_idle(9600), idle_after=0))
    decoder = UARTDecoder(rx="D0", baud=9600)
    annotations = decoder.feed(LogicChunk(0, wave[:cut]))
    annotations += decoder.notify_gap(GapEvent(index=cut, missing=30))
    annotations += decoder.feed(LogicChunk(cut + 30, wave[cut:]))
    annotations += decoder.flush()
    post_gap = [a for a in frames_of(annotations) if a.start_sample >= cut]
    assert all(f.errors for f in post_gap)
    assert all(f.confidence == 0.0 for f in post_gap)


@pytest.mark.parametrize("offset", [1, 17, 60, 111, 205, 400, 617])
def test_stream_start_mid_frame_never_yields_a_clean_byte(offset):
    """Stream start is the gap case with no gap: alignment is simply unknown.

    ``capture --trigger`` hits this on every run, so it has to be closed
    independently of any gap handling.
    """
    payload = b"ABCDEFGH"
    wave = bytes(uart_wave(payload, 9600, idle_before=confirmed_idle(9600), idle_after=0))
    decoder = UARTDecoder(rx="D0", baud=9600)
    annotations = decoder.feed(LogicChunk(offset, wave[offset:]))
    annotations += decoder.flush()
    assert uart_bytes(annotations)[0] == b""


def test_unsynced_frame_is_kept_as_evidence():
    """The frame is still reported — as raw evidence, never as a clean byte."""
    wave = bytes(uart_wave(b"UUUU", 9600, idle_before=20, idle_after=0))
    decoder = UARTDecoder(rx="D0", baud=9600)
    annotations = decoder.feed(LogicChunk(0, wave))
    annotations += decoder.flush()
    frames = frames_of(annotations)
    assert frames, "the candidate frame must be preserved, not dropped"
    assert frames[0].errors == ["unsynced"]
    assert frames[0].confidence == 0.0
    assert "value" in frames[0].fields
    assert uart_bytes(annotations)[0] == b""


@pytest.mark.parametrize(
    ("idle_before", "clean"),
    [(40, False), (103, False), (104, True), (120, True)],
)
def test_confirmed_idle_boundary_after_a_gap(idle_before, clean):
    """One frame time (104 samples at 9600 8N1) is where sync is re-established.

    Below it the high run is one a frame can produce on its own, so the
    following falling edge is not provably a start bit.
    """
    wave_a = uart_wave(b"A", 9600, idle_after=0)
    wave_b = uart_wave(b"B", 9600, idle_before=idle_before)
    decoder = UARTDecoder(rx="D0", baud=9600)
    annotations = []
    cut = len(wave_a) - 20  # gap begins inside frame 'A'
    annotations += decoder.feed(LogicChunk(0, bytes(wave_a[:cut])))
    annotations += decoder.notify_gap(GapEvent(index=cut, missing=25))
    annotations += decoder.feed(LogicChunk(cut + 25, bytes(wave_b)))
    annotations += decoder.flush()
    data, _ = uart_bytes(annotations)
    assert data == (b"B" if clean else b"")


def test_implicit_discontinuity_treated_as_gap():
    wave = uart_wave(b"Z", 9600, idle_before=confirmed_idle(9600))
    logic = bytes(wave)
    decoder = UARTDecoder(rx="D0", baud=9600)
    annotations = decoder.feed(LogicChunk(0, logic[:130]))
    # jumping forward without notify_gap must still be handled as a gap
    annotations += decoder.feed(LogicChunk(200, logic[130:]))
    annotations += decoder.flush()
    assert any("gap" in a.errors for a in annotations)
    assert uart_bytes(annotations)[0] == b""


def test_truncated_frame_at_end_reported():
    wave = uart_wave(b"Q", 9600, idle_before=confirmed_idle(9600), idle_after=0)
    logic = bytes(wave)[:-40]  # cut inside the frame
    decoder = UARTDecoder(rx="D0", baud=9600)
    annotations = decoder.feed(LogicChunk(0, logic))
    annotations += decoder.flush()
    errors = [a for a in annotations if a.kind == "error"]
    assert errors and "truncated" in errors[0].errors


def test_a_gap_between_frames_is_still_reported():
    """A gap with no frame in flight leaves no partial frame — mark it anyway.

    Without a marker the loss would be invisible in the annotation stream and
    the bytes either side of it would look back to back.
    """
    idle = confirmed_idle(9600)
    head = uart_wave(b"TX_", 9600, idle_before=idle, idle_after=0)
    decoder = UARTDecoder(rx="D0", baud=9600)
    annotations = decoder.feed(LogicChunk(0, bytes(head)))
    annotations += decoder.notify_gap(GapEvent(index=len(head), missing=50))
    annotations += decoder.flush()
    markers = [a for a in annotations if "gap" in a.errors]
    assert len(markers) == 1
    assert markers[0].fields["reason"] == "sample_gap"
    assert (markers[0].start_sample, markers[0].end_sample) == (len(head), len(head) + 50)
    assert markers[0].confidence == 0.0


def test_uart_runs_split_at_every_discontinuity():
    """Runs are the unit of contiguity; uart_bytes is their concatenation."""
    idle = confirmed_idle(9600)
    head = uart_wave(b"TX_", 9600, idle_before=idle, idle_after=0)
    tail = uart_wave(b"DONE", 9600, idle_before=idle, idle_after=idle)
    decoder = UARTDecoder(rx="D0", baud=9600)
    cut = len(head)
    annotations = decoder.feed(LogicChunk(0, bytes(head)))
    annotations += decoder.notify_gap(GapEvent(index=cut, missing=50))
    annotations += decoder.feed(LogicChunk(cut + 50, bytes(tail)))
    annotations += decoder.flush()

    runs = uart_runs(annotations)
    assert [data for data, _ in runs] == [b"TX_", b"DONE"]
    data, ends = uart_bytes(annotations)
    assert data == b"TX_DONE"  # the concatenation loses the boundary...
    assert ends == [e for _, run_ends in runs for e in run_ends]
    assert not any(b"TX_DONE" in run for run, _ in runs)  # ...the runs keep it


def test_errors_imply_zero_confidence():
    """Cross-decoder invariant, checked on every annotation UART can emit."""
    idle = confirmed_idle(9600)
    spb = 100000 / 9600
    wave = list(uart_wave(b"\x41", 9600, idle_before=20, idle_after=0))  # unsynced
    wave += [0] * round(15 * spb)  # break
    framed = len(wave)
    wave += list(uart_wave(b"\x41", 9600, idle_before=idle, idle_after=40))
    for i in range(framed + idle + round(9 * spb), framed + idle + round(10 * spb)):
        wave[i] = 0  # stop bit forced low: framing error
    wave += list(uart_wave(b"Q", 9600, idle_before=idle, idle_after=0))[:-30]  # truncated
    decoder = UARTDecoder(rx="D0", baud=9600)
    annotations = decode_wave(wave, decoder)
    seen = {e for a in annotations for e in a.errors}
    assert {"unsynced", "framing", "truncated"} <= seen
    assert all(a.confidence == 0.0 for a in annotations if a.errors)


def test_rate_tiers():
    assert uart_feasibility(9600).tier is Tier.VALIDATED
    assert uart_feasibility(19200).tier is Tier.CONDITIONAL
    assert uart_feasibility(38400).tier is Tier.EXPERIMENTAL
    assert uart_feasibility(57600).tier is Tier.UNSUPPORTED
    assert uart_feasibility(115200).tier is Tier.UNSUPPORTED


def test_conditional_rate_decodes_with_warning_and_lower_confidence():
    decoder = UARTDecoder(rx="D0", baud=19200)
    assert decoder.feasibility.warnings
    wave = uart_wave(b"OK", 19200, idle_before=confirmed_idle(19200))
    frames = frames_of(decode_wave(wave, decoder))
    assert [f.fields["value"] for f in frames] == [0x4F, 0x4B]
    assert all(f.confidence == pytest.approx(0.7) for f in frames)


def test_experimental_rate_requires_opt_in():
    with pytest.raises(DecoderRateError):
        UARTDecoder(rx="D0", baud=38400)
    decoder = UARTDecoder(rx="D0", baud=38400, allow_experimental=True)
    assert decoder.feasibility.tier is Tier.EXPERIMENTAL


def test_unsupported_rate_always_refused():
    with pytest.raises(DecoderRateError):
        UARTDecoder(rx="D0", baud=115200, allow_experimental=True)
