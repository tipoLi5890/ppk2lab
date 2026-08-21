"""Turning a library failure into something the console can say.

The console raises ``ControlRejected(code, args)`` and translates ``code``
directly through its own catalogues, so every code that can reach it has to be
a message key that exists in all four -- otherwise the operator reads an
identifier where a sentence belongs, which is the failure the shipped console
already had once.

So the mapping lives here, in one table, next to the reason each entry exists.
Nothing invents an error vocabulary: the codes come from
``ppk2lab.errors.ERROR_CLASSES`` and the sentence and remediation come from the
exception itself, so a caller that wants the library's own words still has them
under ``detail``.
"""

from __future__ import annotations

from typing import Any

from ppk2lab.errors import (
    DeviceNotFoundError,
    PortBusyError,
    Ppk2labError,
    StreamStalledError,
    TransportError,
    UnsafeOperationError,
    UsageError,
    VoltageRangeError,
)

#: Message keys the console defines for control rejections. Every value here
#: must exist in `webui/src/i18n/en.ts`; a test asserts it by parsing the
#: catalogue rather than trusting this comment.
ER_RANGE = "er_range"
ER_CEILING = "er_ceiling"
ER_LOCKED = "er_locked"
ER_HELD = "er_held"
ER_OFFLINE = "er_offline"
ER_BUSY = "er_busy"
ER_STATE_MOVED = "er_state_moved"
ER_RECORDING = "er_recording"
ER_UNKNOWN = "er_unknown"


class ControlRefused(Exception):
    """A refusal the server generates itself, already carrying a message key.

    Distinct from a library exception: these are decisions this process made --
    control is locked, another connection holds the lease -- and they never
    reached the device.
    """

    def __init__(self, key: str, args: list[Any] | None = None) -> None:
        super().__init__(key)
        self.key = key
        self.args_list: list[Any] = args or []


class DeviceRefused(Exception):
    """A library refusal, carrying the number the request was about.

    ``VoltageRangeError`` covers both the device's own limits and this
    session's ceiling and records neither value, so the requested millivolts
    have to travel with the failure or the console cannot be told which limit
    it hit -- and only one of the two has a remediation that resolves it.
    """

    def __init__(self, cause: BaseException, *, requested_mv: int | None = None) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.requested_mv = requested_mv


def rejection(
    exc: BaseException,
    *,
    requested_mv: int | None = None,
    ceiling_mv: int | None = None,
) -> dict[str, Any]:
    """Describe a failure in the console's vocabulary.

    Returns ``{code, messageKey, args, detail}``. ``detail`` is the library's
    own ``to_json()`` where there is one, so the console can show the
    remediation the library wrote under the translated sentence rather than the
    server paraphrasing it.

    ``requested_mv`` and ``ceiling_mv`` are supplied by the caller because
    ``VoltageRangeError`` covers two refusals -- past the device's limits, and
    past this session's ceiling -- and carries neither number. The caller
    issued the request and knows both.
    """
    if isinstance(exc, DeviceRefused):
        # The caller that issued the request knows the number; prefer it over
        # anything the caller of this function happened to pass.
        return rejection(
            exc.cause,
            requested_mv=exc.requested_mv if exc.requested_mv is not None else requested_mv,
            ceiling_mv=ceiling_mv,
        )
    if isinstance(exc, ControlRefused):
        return {
            "code": "CONTROL_REFUSED",
            "messageKey": exc.key,
            "args": exc.args_list,
            "detail": None,
        }

    detail = exc.to_json() if isinstance(exc, Ppk2labError) else None
    key, args = _key_for(exc, requested_mv, ceiling_mv)
    code = exc.code if isinstance(exc, Ppk2labError) else type(exc).__name__
    return {"code": code, "messageKey": key, "args": args, "detail": detail}


def _key_for(
    exc: BaseException, requested_mv: int | None, ceiling_mv: int | None
) -> tuple[str, list[Any]]:
    if isinstance(exc, VoltageRangeError):
        # One class covers two refusals and only one of them has an actionable
        # remediation. They are told apart by which limit the request breached,
        # not by matching on the message text: a sentence is for a person, and
        # matching one turns any rewording into a silent behaviour change.
        # The device checks the ceiling first, so this order matches it.
        if ceiling_mv is not None and requested_mv is not None and requested_mv > ceiling_mv:
            return ER_CEILING, [requested_mv, ceiling_mv]
        return ER_RANGE, []
    if isinstance(exc, PortBusyError):
        return ER_BUSY, [str(exc)]
    if isinstance(exc, StreamStalledError | TransportError | DeviceNotFoundError):
        return ER_OFFLINE, [str(exc)]
    if isinstance(exc, UnsafeOperationError | UsageError):
        return ER_UNKNOWN, [_code_of(exc), str(exc)]
    return ER_UNKNOWN, [_code_of(exc), str(exc)]


def _code_of(exc: BaseException) -> str:
    return exc.code if isinstance(exc, Ppk2labError) else type(exc).__name__
