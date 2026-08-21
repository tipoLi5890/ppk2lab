"""The wire contract between the supervisor and the browser console.

One WebSocket carries both planes. **Text frames are JSON** and carry
everything a person acts on: device state, the session log, control commands
and their replies. **Binary frames are samples**, and they are binary for three
reasons, in order of weight:

1. ``Calibration.convert_block`` produces ``nan`` for a range with no constants
   and for an unknown source voltage. JSON has no NaN -- ``json.dumps`` emits a
   token no conformant parser accepts, and every workaround (a null, a
   sentinel, a parallel mask) is an encoding invented to paper over a format
   that cannot say what happened. IEEE-754 carries it natively.
2. Tier 0 is 1000 buckets a second. JSON is roughly 130 bytes and one object
   allocation per bucket; the layout below is 24 bytes and none.
3. The console stores buckets in parallel typed arrays, so a struct-of-arrays
   frame decodes into views over the received buffer rather than through a
   thousand JavaScript numbers per second.

Everything is little-endian, stated rather than assumed: the frames are packed
with an explicit byte order and the console asserts its host matches once
rather than silently reading a garbled trace.

**Nothing here talks to a device or to a socket.** That is what makes the wire
format reviewable in one file and testable without either.
"""

from __future__ import annotations

import struct
import sys
from array import array
from typing import TYPE_CHECKING, Any

from ppk2lab._version import SCHEMA_VERSION
from ppk2lab.types import SAMPLE_RATE_HZ, VOLTAGE_MAX_MV, VOLTAGE_MIN_MV

from .buckets import BUCKET_MS, HIST_BINS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from ppk2lab.calibration import Calibration
    from ppk2lab.protocol.metadata import Metadata
    from ppk2lab.types import DeviceInfo

    from .buckets import Bucket

#: Bumped whenever a frame changes shape. The console refuses to run against a
#: protocol it does not speak, and refuses *before* any binary frame arrives:
#: a changed layout read as numbers is a fabricated current trace, which is the
#: one failure this project exists to prevent. The bundle ships inside the
#: wheel and nothing rebuilds it at install time, so an old console meeting a
#: new supervisor is an ordinary event, not an exotic one.
PROTOCOL_VERSION = 1

TAG_BUCKETS = 0x01
TAG_HISTOGRAM = 0x02

#: Set on a batch whose first bucket does not continue the one before it --
#: after a stream stop, an applied mode change, or a resumed connection. The
#: console breaks its mean line and its edge continuity there rather than
#: drawing across a join that was never measured.
FLAG_DISCONTINUITY = 1 << 0

_BUCKET_HEADER = struct.Struct("<BBHIQ")
_HISTOGRAM_HEADER = struct.Struct("<BBHI")

#: Bytes per bucket in the payload, for the size assertions on both sides.
BUCKET_STRIDE = 8 + 4 + 4 + 2 + 1 * 6
_LITTLE_ENDIAN = sys.byteorder == "little"


def _packed(typecode: str, values: Sequence[Any]) -> bytes:
    """A typed array as little-endian bytes, whatever the host is."""
    buf = array(typecode, values)
    if not _LITTLE_ENDIAN:  # pragma: no cover - no big-endian CI runner
        buf.byteswap()
    return buf.tobytes()


def pack_buckets(
    buckets: Sequence[Bucket],
    *,
    discontinuity: bool = False,
) -> bytes:
    """Pack closed tier-0 buckets as one binary frame.

    Sections run in descending alignment after a 16-byte header, so every one
    of them starts at a natural boundary for any ``count`` and the console can
    build typed-array views over the frame without copying.

    ``start_index`` is deliberately not sent per bucket. Buckets are contiguous
    on the timeline -- a gap consumes bucket space rather than shifting later
    buckets -- so the console derives it by accumulating each bucket's own
    width, ``n + gap + excluded``. That keeps the arithmetic in integers all the
    way to the screen: an accumulated float clock drifts, and the drift shows up
    as a trace that slowly disagrees with the timestamps beside it.
    """
    if not buckets:
        raise ValueError("a bucket frame carries at least one bucket")
    count = len(buckets)
    if count > 0xFFFF:
        raise ValueError(f"a bucket frame carries at most 65535 buckets, not {count}")

    header = _BUCKET_HEADER.pack(
        TAG_BUCKETS,
        PROTOCOL_VERSION,
        count,
        FLAG_DISCONTINUITY if discontinuity else 0,
        buckets[0].start_index,
    )
    return b"".join(
        (
            header,
            _packed("d", [b.sum_ua for b in buckets]),
            _packed("f", [b.min_ua for b in buckets]),
            _packed("f", [b.max_ua for b in buckets]),
            _packed("H", [b.edges for b in buckets]),
            bytes(b.n for b in buckets),
            bytes(b.gap for b in buckets),
            bytes(b.excluded for b in buckets),
            bytes(b.range_mask for b in buckets),
            bytes(b.logic_any for b in buckets),
            bytes(b.logic_all for b in buckets),
        )
    )


def unpack_buckets(frame: bytes) -> dict[str, Any]:
    """Decode a bucket frame. For tests and for anyone reading the wire.

    Raises rather than returning numbers when the frame is not one this build
    wrote. A decoder that guesses turns a protocol mismatch into a plausible
    trace, which is worse than no trace at all.
    """
    if len(frame) < _BUCKET_HEADER.size:
        raise ValueError("bucket frame shorter than its header")
    tag, version, count, flags, first_index = _BUCKET_HEADER.unpack_from(frame)
    if tag != TAG_BUCKETS:
        raise ValueError(f"not a bucket frame: tag 0x{tag:02x}")
    if version != PROTOCOL_VERSION:
        raise ValueError(f"bucket frame speaks protocol {version}, not {PROTOCOL_VERSION}")
    expected = _BUCKET_HEADER.size + BUCKET_STRIDE * count
    if len(frame) != expected:
        raise ValueError(f"bucket frame is {len(frame)} bytes, expected {expected}")

    off = _BUCKET_HEADER.size

    def take(typecode: str, width: int) -> list[Any]:
        nonlocal off
        buf = array(typecode)
        buf.frombytes(frame[off : off + width * count])
        if not _LITTLE_ENDIAN:  # pragma: no cover
            buf.byteswap()
        off += width * count
        return list(buf)

    sums = take("d", 8)
    mins = take("f", 4)
    maxs = take("f", 4)
    edges = take("H", 2)
    ns = list(frame[off : off + count])
    off += count
    gaps = list(frame[off : off + count])
    off += count
    excluded = list(frame[off : off + count])
    off += count
    range_mask = list(frame[off : off + count])
    off += count
    logic_any = list(frame[off : off + count])
    off += count
    logic_all = list(frame[off : off + count])

    start = first_index
    starts = []
    for i in range(count):
        starts.append(start)
        start += ns[i] + gaps[i] + excluded[i]

    return {
        "count": count,
        "discontinuity": bool(flags & FLAG_DISCONTINUITY),
        "first_index": first_index,
        "start_index": starts,
        "sum_ua": sums,
        "min_ua": mins,
        "max_ua": maxs,
        "edges": edges,
        "n": ns,
        "gap": gaps,
        "excluded": excluded,
        "range_mask": range_mask,
        "logic_any": logic_any,
        "logic_all": logic_all,
    }


def pack_histogram(histogram: Sequence[float]) -> bytes:
    """Pack the distribution grid as one binary frame.

    Absolute counts, never deltas. A dropped delta corrupts a cumulative
    distribution permanently and silently; an absolute snapshot repairs itself
    on the next one, which is the same reason the counters are sent absolute.
    Sent as float64 because an eight-hour session is 2.9e9 samples and float32
    stops counting exactly above 2^24.
    """
    header = _HISTOGRAM_HEADER.pack(TAG_HISTOGRAM, PROTOCOL_VERSION, len(histogram), 0)
    return header + _packed("d", list(histogram))


def unpack_histogram(frame: bytes) -> list[float]:
    if len(frame) < _HISTOGRAM_HEADER.size:
        raise ValueError("histogram frame shorter than its header")
    tag, version, bins, _reserved = _HISTOGRAM_HEADER.unpack_from(frame)
    if tag != TAG_HISTOGRAM:
        raise ValueError(f"not a histogram frame: tag 0x{tag:02x}")
    if version != PROTOCOL_VERSION:
        raise ValueError(f"histogram frame speaks protocol {version}, not {PROTOCOL_VERSION}")
    expected = _HISTOGRAM_HEADER.size + 8 * bins
    if len(frame) != expected:
        raise ValueError(f"histogram frame is {len(frame)} bytes, expected {expected}")
    buf = array("d")
    buf.frombytes(frame[_HISTOGRAM_HEADER.size :])
    if not _LITTLE_ENDIAN:  # pragma: no cover
        buf.byteswap()
    return list(buf)


# ---------------------------------------------------------------------------
# JSON messages
#
# Every message is a flat object with a `type`. `type` is an open string rather
# than an enum, following the same rule as the warning and interruption
# catalogues: a reader that meets a kind it does not know must carry it as
# unknown rather than fail closed, so adding one later is an addition.


def _envelope(_type: str, /, **fields: Any) -> dict[str, Any]:
    # Positional-only: a message that carries its own `kind` field --
    # `event` does -- would otherwise collide with this parameter.
    return {"type": _type, **fields}


def device_json(info: DeviceInfo, fingerprint: str | None) -> dict[str, Any]:
    """The device identity, shaped as the console's `DeviceInfo`.

    Not `DeviceInfo.to_json()` verbatim: the console shows the measurement port
    and the firmware fingerprint, which are a property and a method rather than
    fields. Everything else is the library's own value.
    """
    return {
        "serial_number": info.serial_number,
        "vid": info.vid,
        "pid": info.pid,
        "firmware_version": info.firmware_version,
        "simulated": info.simulated,
        "measurement_port": info.measurement_port.path if info.measurement_port else None,
        "fingerprint": fingerprint,
    }


def calibration_json(calibration: Calibration | None, metadata: Metadata | None) -> dict[str, Any]:
    """The calibration table, shaped as the console's `Calibration`.

    A range with no constants is `null`, never a zero-filled row: `O[r] = 0`
    alone biases every reading in that range, and a table that looks complete
    is worse than one that says what is missing.
    """
    if calibration is None:
        return {
            "calibrated": None,
            "hw": None,
            "ia": None,
            "vdd_mv": None,
            "ranges": [],
            "missing_ranges": [],
            "terminated": False,
        }
    ranges = [
        None
        if r is None
        else {"r": r.r, "gs": r.gs, "gi": r.gi, "o": r.o, "s": r.s, "i": r.i, "ug": r.ug}
        for r in calibration.ranges
    ]
    return {
        "calibrated": calibration.calibrated,
        "hw": metadata.hw if metadata else None,
        "ia": metadata.ia if metadata else None,
        "vdd_mv": calibration.vdd_mv,
        "ranges": ranges,
        "missing_ranges": calibration.missing_ranges(),
        "terminated": metadata.terminated if metadata else False,
    }


def hello(
    *,
    session_id: str,
    control_allowed: bool,
    control_token: str,
    attach_index: int,
    history_seconds: float,
    device: dict[str, Any],
    state: dict[str, Any],
    calibration: dict[str, Any],
    max_voltage_mv: int | None,
    counters: dict[str, Any],
    streaming: bool,
    state_seq: int,
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    """The first message on every connection, and the only version gate.

    A console that does not speak `protocol` must stop here rather than decode
    a binary frame whose layout it is guessing at.
    """
    return _envelope(
        "hello",
        protocol=PROTOCOL_VERSION,
        schema_version=SCHEMA_VERSION,
        session={
            "id": session_id,
            "simulated": device["simulated"],
            "control_allowed": control_allowed,
            "control_token": control_token,
            "bucket_us": BUCKET_MS * 1000,
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "histogram_bins": HIST_BINS + 1,
            "attach_index": attach_index,
            "history_seconds": history_seconds,
        },
        device=device,
        state=state,
        calibration=calibration,
        limits={
            "max_voltage_mv": max_voltage_mv,
            "voltage_min_mv": VOLTAGE_MIN_MV,
            "voltage_max_mv": VOLTAGE_MAX_MV,
        },
        counters=counters,
        streaming=streaming,
        state_seq=state_seq,
        warnings=warnings,
    )


def counters(
    *,
    total_stored: int,
    total_missing: int,
    total_excluded: int,
    gap_count: int,
    timeline_degraded: bool,
    peak_queued_bytes: int,
    index: int,
) -> dict[str, Any]:
    """Absolute session totals, not deltas.

    `Decimator.ingestBucket` leaves these to the caller by design, so the
    console assigns rather than accumulates them. Assignment is idempotent,
    survives a dropped frame, and -- unlike accumulation -- still tells the
    truth after a reconnection that missed part of the stream.
    """
    return _envelope(
        "counters",
        total_stored=total_stored,
        total_missing=total_missing,
        total_excluded=total_excluded,
        gap_count=gap_count,
        timeline_degraded=timeline_degraded,
        peak_queued_bytes=peak_queued_bytes,
        index=index,
    )


def state(
    *,
    device_state: dict[str, Any],
    change: dict[str, Any] | None,
    state_seq: int,
    config: dict[str, Any],
) -> dict[str, Any]:
    return _envelope("state", state=device_state, change=change, state_seq=state_seq, config=config)


def event(
    *,
    kind: str,
    operation: str,
    message_key: str,
    args: list[Any],
    index: int,
    delta: dict[str, Any] | None = None,
    observed: bool | None = None,
) -> dict[str, Any]:
    """One line for the session log.

    The server sends a message *key* and its arguments, never a sentence: the
    console owns four catalogues and picks the reader's language. `operation`
    travels untranslated on purpose -- it is an identifier like `set_dut_power`,
    and translating it would make the log harder to match against the CLI.
    """
    return _envelope(
        "event",
        kind=kind,
        operation=operation,
        messageKey=message_key,
        args=args,
        index=index,
        delta=delta,
        observed=observed,
    )


def gap(*, index: int, missing: int | None, reason: str, ambiguous: bool) -> dict[str, Any]:
    return _envelope("gap", index=index, missing=missing, reason=reason, ambiguous=ambiguous)


def stream(*, running: bool, phase: str, reason: str | None = None) -> dict[str, Any]:
    """`phase` is an open string: running, stopped, applying, recording, stalled."""
    return _envelope("stream", running=running, phase=phase, reason=reason)


def desync(*, from_index: int, to_index: int, buckets_dropped: int) -> dict[str, Any]:
    """This connection fell behind and the server discarded what it could not
    send, rather than buffering without limit.

    Named apart from a sample gap on purpose, and the console keeps the two
    counts apart: "the instrument never delivered it" and "your browser did not
    receive it" are different facts, and conflating them would let a slow link
    look like a lossy instrument.
    """
    return _envelope(
        "desync", from_index=from_index, to_index=to_index, buckets_dropped=buckets_dropped
    )


def result(*, request_id: str, ok: bool, **fields: Any) -> dict[str, Any]:
    return _envelope("result", id=request_id, ok=ok, **fields)


def error(rejection: dict[str, Any]) -> dict[str, Any]:
    """Wrap what ``codes.rejection()`` produced.

    It takes the dict rather than its fields so the two cannot drift apart: the
    mapping from a failure to a message key lives in one module, and this one
    only puts an envelope around it.
    """
    return _envelope("error", **rejection)


def bye(*, reason: str) -> dict[str, Any]:
    return _envelope("bye", reason=reason)
