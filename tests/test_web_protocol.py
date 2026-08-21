"""The wire format, and the vocabulary the console has to be able to say.

Nothing here touches a device or a socket. The point of keeping the protocol in
a module with no I/O is that its whole contract can be checked like arithmetic.
"""

from __future__ import annotations

import math
import pathlib
import re
import struct

import pytest

from ppk2lab.errors import (
    DeviceNotFoundError,
    PortBusyError,
    Ppk2labError,
    StreamStalledError,
    UsageError,
    VoltageRangeError,
)
from ppk2lab_web import codes
from ppk2lab_web.buckets import HIST_BINS, Bucket, Tier0Accumulator, new_histogram
from ppk2lab_web.protocol import (
    BUCKET_STRIDE,
    PROTOCOL_VERSION,
    TAG_BUCKETS,
    TAG_HISTOGRAM,
    pack_buckets,
    pack_histogram,
    unpack_buckets,
    unpack_histogram,
)

EN_CATALOGUE = pathlib.Path(__file__).parents[1] / "webui" / "src" / "i18n" / "en.ts"


def _bucket(i: int, **over) -> Bucket:
    fields = {
        "start_index": i * 100,
        "t0": i * 0.001,
        "min_ua": -0.25,
        "max_ua": 12_000.5,
        "sum_ua": 1234.5,
        "n": 100,
        "gap": 0,
        "excluded": 0,
        "range_mask": 0b0000_1001,
        "logic_any": 0xA5,
        "logic_all": 0x05,
        "edges": 7,
    }
    fields.update(over)
    return Bucket(**fields)


# ---------------------------------------------------------------------------
# Bucket frames


@pytest.mark.parametrize("count", [1, 2, 3, 7, 64, 1000])
def test_a_bucket_frame_is_exactly_its_declared_size(count):
    frame = pack_buckets([_bucket(i) for i in range(count)])
    assert len(frame) == 16 + BUCKET_STRIDE * count


@pytest.mark.parametrize("count", [1, 2, 3, 7, 64, 1000])
def test_every_section_starts_naturally_aligned(count):
    """The console builds typed-array views straight over the received buffer.
    A section that does not start on its own boundary cannot be viewed at all,
    and the failure would be a runtime error in a browser rather than here."""
    # Sections in the order they are packed, with their element widths.
    widths = [8, 4, 4, 2, 1, 1, 1, 1, 1, 1]
    offset = 16
    for width in widths:
        assert offset % width == 0, (count, offset, width)
        offset += width * count
    assert offset == 16 + BUCKET_STRIDE * count


def test_a_bucket_frame_round_trips_every_field():
    buckets = [_bucket(0), _bucket(1, n=40, gap=60, edges=65535, logic_all=0)]
    got = unpack_buckets(pack_buckets(buckets))
    assert got["count"] == 2
    assert got["sum_ua"] == [b.sum_ua for b in buckets]
    assert got["n"] == [100, 40]
    assert got["gap"] == [0, 60]
    assert got["edges"] == [7, 65535]
    assert got["range_mask"] == [0b1001, 0b1001]
    assert got["logic_any"] == [0xA5, 0xA5]
    assert got["logic_all"] == [0x05, 0]
    assert not got["discontinuity"]


def test_min_and_max_survive_as_float32():
    """They are float32 on the wire because the console stores them in a
    Float32Array; sending float64 would only be rounded on arrival."""
    got = unpack_buckets(pack_buckets([_bucket(0, min_ua=-0.25, max_ua=12000.5)]))
    assert got["min_ua"] == [-0.25]
    assert got["max_ua"] == [12000.5]


def test_the_sum_stays_float64():
    """A bucket sums a hundred samples and the console keeps it in a
    Float64Array; narrowing it here would quietly round the evidence."""
    exact = 0.1 + 0.2  # not representable, and must arrive unchanged
    got = unpack_buckets(pack_buckets([_bucket(0, sum_ua=exact)]))
    assert got["sum_ua"][0] == exact


def test_nan_crosses_the_wire_as_nan():
    """The reason the sample plane is binary at all. `convert_block` produces
    NaN for a range with no constants, and JSON cannot carry it."""
    got = unpack_buckets(pack_buckets([_bucket(0, sum_ua=math.nan, min_ua=math.nan)]))
    assert math.isnan(got["sum_ua"][0])
    assert math.isnan(got["min_ua"][0])


def test_a_negative_reading_is_not_clipped():
    """A shunt legitimately reads slightly negative near zero, and the console
    draws it below the axis rather than pretending it was zero."""
    got = unpack_buckets(pack_buckets([_bucket(0, min_ua=-0.2477)]))
    assert got["min_ua"][0] == pytest.approx(-0.2477, rel=1e-6)
    assert got["min_ua"][0] < 0


def test_start_index_is_derived_from_each_bucket_width():
    """Buckets are contiguous, so only the first index is sent -- but a bucket
    flushed at a discontinuity is short, and the derivation has to follow the
    real widths rather than assume a hundred every time."""
    acc = Tier0Accumulator()
    acc.add_samples([1.0] * 30, bytes(30), bytes(30))
    (short,) = acc.flush()
    full = acc.add_samples([1.0] * 100, bytes(100), bytes(100))
    got = unpack_buckets(pack_buckets([short, *full]))
    assert got["start_index"] == [0, 30]
    assert [b.start_index for b in (short, *full)] == got["start_index"]


def test_the_discontinuity_flag_survives():
    got = unpack_buckets(pack_buckets([_bucket(0)], discontinuity=True))
    assert got["discontinuity"] is True


def test_an_empty_frame_is_refused_rather_than_sent():
    with pytest.raises(ValueError, match="at least one bucket"):
        pack_buckets([])


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda f: b"\x09" + f[1:], "not a bucket frame"),
        (lambda f: f[:1] + bytes([PROTOCOL_VERSION + 1]) + f[2:], "protocol"),
        (lambda f: f[:-1], "expected"),
        (lambda f: f + b"\x00", "expected"),
        # Shorter than the 16-byte header, so it never reaches the size check.
        (lambda f: f[:8], "shorter than its header"),
        (lambda f: b"", "shorter than its header"),
    ],
)
def test_a_frame_this_build_did_not_write_raises_instead_of_decoding(mutate, match):
    """A decoder that guesses turns a protocol mismatch into a plausible
    current trace, which is the one failure this project exists to prevent.
    Every malformed frame has to raise, never return numbers."""
    frame = pack_buckets([_bucket(0), _bucket(1)])
    with pytest.raises(ValueError, match=match):
        unpack_buckets(mutate(frame))


def test_the_header_says_what_it_is_without_decoding_the_body():
    frame = pack_buckets([_bucket(0)])
    tag, version = struct.unpack_from("<BB", frame)
    assert tag == TAG_BUCKETS
    assert version == PROTOCOL_VERSION


# ---------------------------------------------------------------------------
# Histogram frames


def test_a_histogram_frame_round_trips_exactly():
    hist = new_histogram()
    hist[0] = 2.0
    hist[37] = 1e9  # an 8-hour session is 2.9e9 samples; float32 would round
    hist[HIST_BINS] = 5.0
    got = unpack_histogram(pack_histogram(hist))
    assert got == list(hist)
    assert got[37] == 1e9


def test_a_histogram_frame_is_absolute_sized():
    frame = pack_histogram(new_histogram())
    assert len(frame) == 8 + 8 * (HIST_BINS + 1)
    tag, version = struct.unpack_from("<BB", frame)
    assert (tag, version) == (TAG_HISTOGRAM, PROTOCOL_VERSION)


def test_a_bucket_frame_is_not_mistaken_for_a_histogram():
    with pytest.raises(ValueError, match="not a histogram frame"):
        unpack_histogram(pack_buckets([_bucket(0)]))
    with pytest.raises(ValueError, match="not a bucket frame"):
        unpack_buckets(pack_histogram(new_histogram()))


# ---------------------------------------------------------------------------
# The console's vocabulary


def _catalogue_keys() -> set[str]:
    return set(re.findall(r"^  ([a-z][A-Za-z0-9_]*):", EN_CATALOGUE.read_text(), re.M))


def test_every_code_the_server_can_send_exists_in_the_console():
    """The console translates a rejection code directly. One that is not a real
    key renders as its own name -- an identifier where a sentence belongs, in
    front of someone deciding whether to energise a board.

    Checked by parsing the catalogue rather than by trusting a comment, the
    same way the CLI's documented flags are pinned to the parser.
    """
    keys = _catalogue_keys()
    assert keys, "could not parse the English catalogue"
    for name in dir(codes):
        if not name.startswith("ER_"):
            continue
        value = getattr(codes, name)
        assert value in keys, f"{name} = {value!r} is not in webui/src/i18n/en.ts"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (PortBusyError("busy"), codes.ER_BUSY),
        (StreamStalledError("quiet"), codes.ER_OFFLINE),
        (DeviceNotFoundError("gone"), codes.ER_OFFLINE),
        (UsageError("nope"), codes.ER_UNKNOWN),
        (Ppk2labError("boom"), codes.ER_UNKNOWN),
        (RuntimeError("not ours"), codes.ER_UNKNOWN),
    ],
)
def test_every_failure_maps_to_a_key_the_console_can_render(exc, expected):
    out = codes.rejection(exc)
    assert out["messageKey"] == expected
    assert out["messageKey"] in _catalogue_keys()


def test_a_voltage_refusal_says_which_limit_it_hit():
    """One class covers the device's own limits and this session's ceiling, and
    only the ceiling message carries the remediation that resolves it."""
    exc = VoltageRangeError("above the ceiling")
    ceiling = codes.rejection(exc, requested_mv=4200, ceiling_mv=3600)
    assert ceiling["messageKey"] == codes.ER_CEILING
    assert ceiling["args"] == [4200, 3600]

    device = codes.rejection(exc, requested_mv=6000, ceiling_mv=None)
    assert device["messageKey"] == codes.ER_RANGE


def test_a_rejection_carries_the_library_s_own_remediation():
    """The console shows the translated sentence and the library's remediation
    under it, rather than the server paraphrasing what the library said."""
    out = codes.rejection(PortBusyError("held"))
    assert out["detail"]["code"] == "PORT_BUSY"
    assert out["detail"]["remediation"]
    assert out["code"] == "PORT_BUSY"


def test_a_refusal_this_server_made_is_marked_as_its_own():
    """Control being locked is a decision this process made, not something a
    device said, and it has no library error to carry."""
    out = codes.rejection(codes.ControlRefused(codes.ER_LOCKED))
    assert out["code"] == "CONTROL_REFUSED"
    assert out["messageKey"] == codes.ER_LOCKED
    assert out["detail"] is None
