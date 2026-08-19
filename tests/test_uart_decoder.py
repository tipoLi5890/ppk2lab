"""UART decoder golden vectors (self-generated) and edge cases."""

import pytest

from ppk2lab.decoders.base import LogicChunk
from ppk2lab.decoders.feasibility import Tier, uart_feasibility
from ppk2lab.decoders.uart import UARTDecoder, uart_bytes
from ppk2lab.errors import DecoderRateError
from ppk2lab.testing.signals import uart_wave
from ppk2lab.types import GapEvent


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
    wave = uart_wave(payload, baud)
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
    wave = uart_wave(payload, 9600)
    decoder = UARTDecoder(rx="D0", baud=9600)
    data, _ = uart_bytes(decode_wave(wave, decoder))
    assert data == payload


@pytest.mark.parametrize("chunk", [1, 3, 7, 64, 1024])
def test_chunk_boundaries_do_not_change_results(chunk):
    payload = b"chunk boundary test \x00\xff\x55"
    wave = uart_wave(payload, 9600)
    decoder = UARTDecoder(rx="D0", baud=9600)
    data, _ = uart_bytes(decode_wave(wave, decoder, chunk=chunk))
    assert data == payload


@pytest.mark.parametrize("parity", ["even", "odd"])
def test_parity_decodes_and_detects_errors(parity):
    payload = b"PARITY"
    wave = uart_wave(payload, 9600, parity=parity)
    decoder = UARTDecoder(rx="D0", baud=9600, parity=parity)
    data, _ = uart_bytes(decode_wave(wave, decoder))
    assert data == payload

    # decoding with the opposite parity flags errors on odd-weight bytes
    wrong = "odd" if parity == "even" else "even"
    decoder = UARTDecoder(rx="D0", baud=9600, parity=wrong)
    annotations = decode_wave(uart_wave(payload, 9600, parity=parity), decoder)
    assert any("parity" in f.errors for f in frames_of(annotations))


def test_framing_error_detected():
    # A stop bit forced low: build 0x41 then overwrite its stop-bit samples
    wave = uart_wave(b"\x41", 9600, idle_after=40)
    spb = 100000 / 9600
    stop_start = 20 + round(9 * spb)
    stop_end = 20 + round(10 * spb)
    for i in range(stop_start, stop_end):
        wave[i] = 0
    decoder = UARTDecoder(rx="D0", baud=9600)
    annotations = decode_wave(wave, decoder)
    frames = frames_of(annotations)
    assert frames and "framing" in frames[0].errors
    data, _ = uart_bytes(annotations)
    assert data == b""  # frames with errors never join the byte stream


def test_inverted_signal():
    payload = b"inv"
    wave = uart_wave(payload, 9600, invert=True)
    decoder = UARTDecoder(rx="D0", baud=9600, invert=True)
    data, _ = uart_bytes(decode_wave(wave, decoder))
    assert data == payload


def test_msb_first_and_word_sizes():
    wave = uart_wave([0x2A], 9600, data_bits=7, msb_first=True)
    decoder = UARTDecoder(rx="D0", baud=9600, data_bits=7, msb_first=True)
    frames = frames_of(decode_wave(wave, decoder))
    assert frames[0].fields["value"] == 0x2A

    wave = uart_wave([0x1AB], 9600, data_bits=9, stop_bits=2)
    decoder = UARTDecoder(rx="D0", baud=9600, data_bits=9, stop_bits=2)
    frames = frames_of(decode_wave(wave, decoder))
    assert frames[0].fields["value"] == 0x1AB


def test_break_detected():
    spb = 100000 / 9600
    wave = [1] * 30 + [0] * round(15 * spb) + [1] * 30
    decoder = UARTDecoder(rx="D0", baud=9600)
    annotations = decode_wave(wave, decoder)
    breaks = [a for a in annotations if a.kind == "break"]
    assert len(breaks) == 1
    assert breaks[0].start_sample == 30
    assert not frames_of(annotations)


def test_gap_mid_frame_invalidates_frame():
    payload = b"AB"
    wave = uart_wave(payload, 9600, idle_before=20)
    logic = bytes(wave)
    decoder = UARTDecoder(rx="D0", baud=9600)
    annotations = []
    # samples [50, 80) of the timeline are lost inside frame 'A'; frame 'B'
    # (starting near sample 124) is entirely after the gap
    annotations += decoder.feed(LogicChunk(0, logic[:50]))
    annotations += decoder.notify_gap(GapEvent(index=50, missing=30))
    annotations += decoder.feed(LogicChunk(80, logic[80:]))
    annotations += decoder.flush()
    errors = [a for a in annotations if a.kind == "error"]
    assert errors and "gap" in errors[0].errors
    assert errors[0].confidence == 0.0
    data, _ = uart_bytes(annotations)
    # frame 'A' was cut by the gap and frame 'B' follows back-to-back without
    # enough idle to re-synchronize: the decoder must decode NOTHING rather
    # than fabricate bytes from misaligned bits
    assert data == b""


def test_frame_after_gap_and_idle_decodes():
    wave_a = uart_wave(b"A", 9600, idle_after=0)
    wave_b = uart_wave(b"B", 9600, idle_before=40)  # ~4 bit times of idle
    decoder = UARTDecoder(rx="D0", baud=9600)
    annotations = []
    cut = len(wave_a) - 20  # gap begins inside frame 'A'
    annotations += decoder.feed(LogicChunk(0, bytes(wave_a[:cut])))
    annotations += decoder.notify_gap(GapEvent(index=cut, missing=25))
    annotations += decoder.feed(LogicChunk(cut + 25, bytes(wave_b)))
    annotations += decoder.flush()
    data, _ = uart_bytes(annotations)
    assert data == b"B"  # idle after the gap re-synchronizes honestly


def test_implicit_discontinuity_treated_as_gap():
    wave = uart_wave(b"Z", 9600)
    logic = bytes(wave)
    decoder = UARTDecoder(rx="D0", baud=9600)
    decoder.feed(LogicChunk(0, logic[:30]))
    # jumping forward without notify_gap must still be handled as a gap
    annotations = decoder.feed(LogicChunk(100, logic[30:]))
    annotations += decoder.flush()
    assert not any(a.kind == "frame" and not a.errors and a.start_sample < 100 for a in annotations)


def test_truncated_frame_at_end_reported():
    wave = uart_wave(b"Q", 9600, idle_after=0)
    logic = bytes(wave)[:-40]  # cut inside the frame
    decoder = UARTDecoder(rx="D0", baud=9600)
    annotations = decoder.feed(LogicChunk(0, logic))
    annotations += decoder.flush()
    errors = [a for a in annotations if a.kind == "error"]
    assert errors and "truncated" in errors[0].errors


def test_rate_tiers():
    assert uart_feasibility(9600).tier is Tier.VALIDATED
    assert uart_feasibility(19200).tier is Tier.CONDITIONAL
    assert uart_feasibility(38400).tier is Tier.EXPERIMENTAL
    assert uart_feasibility(57600).tier is Tier.UNSUPPORTED
    assert uart_feasibility(115200).tier is Tier.UNSUPPORTED


def test_conditional_rate_decodes_with_warning_and_lower_confidence():
    decoder = UARTDecoder(rx="D0", baud=19200)
    assert decoder.feasibility.warnings
    wave = uart_wave(b"OK", 19200)
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
