"""CLI command handlers.

Each handler returns an :class:`Outcome`; ``main`` renders it as the JSON
envelope or human-readable text and maps it to the documented exit codes.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .._version import __version__
from ..analysis.assertions import (
    AssertionOutcome,
    AssertionRule,
    evaluate_assertion,
    junit_report,
    parse_rule,
)
from ..analysis.compare import compare_stats, summarize_side
from ..analysis.measure import (
    load_annotations_jsonl,
    measure_annotations,
    measure_window,
    save_annotations_jsonl,
)
from ..capture.artifact import ArtifactReader, read_window
from ..capture.model import MAX_LOAD_SAMPLES, Capture
from ..capture.runner import DEFAULT_IN_MEMORY_LIMIT_SAMPLES
from ..capture.stats import WindowStats, format_with_uncertainty
from ..decoders.base import decode_capture
from ..decoders.registry import decoder_capabilities
from ..decoders.spi import SPIDecoder
from ..decoders.uart import UARTDecoder
from ..device import PPK2
from ..diagnostics import (
    W_CALIBRATION_INCOMPLETE,
    W_DECIMATED,
    W_DECODER_RATE,
    W_DEVICE_NOT_FOUND,
    W_DRY_RUN,
    W_DUT_POWER_TRANSIENT,
    W_GAP_TABLE_TRUNCATED,
    W_GENERIC,
    W_INSTRUMENT_MISMATCH,
    W_INTERRUPTED,
    W_METADATA,
    W_NOT_CALIBRATED,
    W_PARTIAL_INTEGRITY,
    W_SAMPLE_GAPS,
    W_SESSION_RECOVERED,
    W_STATE_UNVERIFIED,
    W_VOLTAGE_ASSUMED,
    Diagnostic,
    as_json,
    gap_reason_catalog,
    interruption_reason_catalog,
    warn,
    warning_catalog,
)
from ..discovery import discover
from ..errors import (
    EXIT_ASSERTION_FAILED,
    EXIT_CAPTURE_INCOMPLETE,
    EXIT_DEVICE_NOT_FOUND,
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_PERMISSION,
    EXIT_PROTOCOL,
    EXIT_USAGE,
    CaptureIncompleteError,
    OutputExistsError,
    UsageError,
    WebExtraMissingError,
    error_catalog,
)
from ..exports import (
    bucket_samples_for_ms,
    estimate_export_size,
    export_csv,
    export_decimated_csv,
    export_decimated_jsonl,
    export_samples_jsonl,
    export_vcd,
)
from ..schemas import get_schema, list_schemas
from ..triggers.engine import TriggerEngine, parse_trigger_spec
from ..types import (
    DIGITAL_CHANNELS,
    SAMPLE_RATE_HZ,
    VOLTAGE_MAX_MV,
    VOLTAGE_MIN_MV,
    Mode,
    StateChange,
    VoltageBasis,
)
from ..units import format_si, parse_channel_set, parse_current_ua, parse_duration_s
from .main import COMMAND_CATEGORIES, STATE_CHANGING_COMMANDS, build_parser


@dataclass
class Outcome:
    result: dict[str, Any] | None
    warnings: Sequence[Diagnostic | str] = field(default_factory=list)
    human: str = ""
    exit_code: int = 0
    error: dict[str, Any] | None = None
    raw_output: str | None = None


def _open_device(args: Any) -> PPK2:
    return PPK2.open(
        serial_number=getattr(args, "device", None),
        port=getattr(args, "port", None),
        simulate=args.simulate,
        max_voltage_mv=getattr(args, "max_voltage_mv", None),
    )


def _parse_max_samples(text: str | None) -> int | None:
    """``None`` (flag absent) keeps the default ceiling; ``"none"`` removes it."""
    if text is None:
        return MAX_LOAD_SAMPLES
    if text.strip().lower() == "none":
        return None
    try:
        value = int(text)
    except ValueError as exc:
        raise UsageError(
            f"invalid --max-samples {text!r}; expected a positive integer or 'none'"
        ) from exc
    if value <= 0:
        raise UsageError(f"--max-samples must be positive, got {value}")
    return value


def _load_capture(args: Any) -> Capture:
    return Capture.load(
        args.capture, max_samples=_parse_max_samples(getattr(args, "max_samples", None))
    )


def _parse_window(text: str) -> tuple[float | None, float | None]:
    """``START:END`` in seconds; either side may be empty for an open bound."""
    start_text, sep, end_text = text.partition(":")
    if not sep:
        raise UsageError(f"invalid --window {text!r}; expected START:END seconds")
    try:
        start = float(start_text) if start_text.strip() else None
        end = float(end_text) if end_text.strip() else None
    except ValueError as exc:
        raise UsageError(f"invalid --window {text!r}; expected START:END seconds") from exc
    if start is None and end is None:
        raise UsageError(f"invalid --window {text!r}; at least one bound is required")
    return start, end


def _load_window(args: Any, start_s: float | None, end_s: float | None) -> Capture:
    """Read only the chunks a window falls in, rather than the whole file.

    Peak memory then follows the window rather than the capture, which is what
    makes an hour-scale artifact usable at all. The floor is one chunk: a chunk
    is CRC-checked whole before it can be sliced.
    """
    return read_window(
        args.capture,
        start_s=start_s,
        end_s=end_s,
        max_samples=_parse_max_samples(getattr(args, "max_samples", None)),
    )


def _cli_fingerprint(device: PPK2) -> dict[str, Any]:
    """Firmware fingerprint with the port count reported honestly.

    ``--port PATH`` opens a device the discovery scan may never have listed,
    so its port tuple is empty. Publishing that as ``port_count: 0`` would
    make the row differ from the same unit opened with ``--device``, and the
    compatibility matrix is keyed on exactly this dict: unknown must read as
    unknown, not as zero.
    """
    fingerprint = device.firmware_fingerprint()
    if not fingerprint.get("port_count"):
        fingerprint["port_count"] = None
    return fingerprint


def _fmt_fingerprint(fingerprint: dict[str, Any]) -> str:
    """One-line form for the compatibility-matrix row a human transcribes."""
    ports = fingerprint.get("port_count")
    return (
        f"HW={fingerprint.get('hw')} IA={fingerprint.get('ia')} "
        f"keys={fingerprint.get('metadata_key_count')} "
        f"ports={'unknown' if ports is None else ports}"
    )


#: Where a source voltage came from, in words. Keyed on the JSON value rather
#: than the enum member so a state dict carried in a change record renders the
#: same as a live one, and so an unknown value can be printed as it arrived
#: instead of being mapped onto a phrasing that would misstate it.
_VOLTAGE_BASIS_TEXT = {
    VoltageBasis.CALLER_OVERRIDE.value: "supplied by the caller",
    VoltageBasis.CONFIGURED_SOURCE.value: "configured this session",
    VoltageBasis.DEVICE_METADATA.value: "read from device metadata",
    VoltageBasis.UNKNOWN.value: "basis unknown",
}


def _fmt_state_value(value: Any) -> str:
    """One state field for a human; ``None`` prints as unknown, never as a
    plausible-looking default."""
    if value is None:
        return "UNKNOWN"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _fmt_state_lines(state: dict[str, Any]) -> list[str]:
    """Device state as indented lines, the voltage carrying its basis.

    The basis is a parenthetical on the voltage rather than a field of its
    own: it is the only thing that says whether that number describes the DUT
    or is a leftover regulator setpoint, and a separate line invites reading
    the voltage without it.
    """
    voltage = _fmt_state_value(state.get("source_voltage_mv"))
    basis = state.get("source_voltage_basis")
    if basis is not None:
        voltage += f" ({_VOLTAGE_BASIS_TEXT.get(str(basis), str(basis))})"
    return [
        f"  mode: {_fmt_state_value(state.get('mode'))}",
        f"  source_voltage_mv: {voltage}",
        f"  dut_power: {_fmt_state_value(state.get('dut_power'))}",
        f"  measuring: {_fmt_state_value(state.get('measuring'))}",
    ]


def _fmt_change_lines(change: StateChange) -> list[str]:
    """One state change as requested, before -> after, and what was confirmed.

    The readback marker is the part that cannot be inferred from the values:
    ``after`` is the requested state whenever ``observed_after`` is false, and
    it looks identical to a state the device reported back.
    """
    verb = "applied" if change.applied else "would apply"
    requested = ", ".join(f"{k}={_fmt_state_value(v)}" for k, v in change.requested.items())
    lines = [f"{change.operation} ({verb}):", f"  requested: {requested or '(no arguments)'}"]
    lines.extend(
        [
            f"  {field}: {_fmt_state_value(change.before.get(field))} -> {_fmt_state_value(value)}"
            for field, value in change.after.items()
            if change.before.get(field) != value
        ]
        or ["  no state field changed"]
    )
    if change.applied:
        lines[-1] += (
            "   [verified on device]"
            if change.observed_after
            else "   [not read back — 'after' reflects the request]"
        )
    return lines


def _fmt_stats(stats: dict[str, Any]) -> str:
    current = stats["current_ua"]
    uncertainty = stats.get("uncertainty") or {}
    lines = [
        f"  duration: {stats['duration_s']:.6g} s "
        f"({stats['samples']['stored']} samples stored, "
        f"{stats['samples']['missing_known']} missing)",
    ]
    if current["mean"] is not None:
        # Printed to the digits the interval supports: an uncertainty of
        # +/- 190 uA does not justify thirteen significant figures. The JSON
        # keeps full precision.
        mean = format_with_uncertainty(current["mean"], uncertainty.get("mean_ua_typical"))
        lines.append(
            f"  current: mean {mean} uA, "
            f"min {format_si(current['min'], 'A')}, max {format_si(current['max'], 'A')}"
        )
        # A quantile served from the grid floor bounds the true value from
        # above; the sign says so rather than letting it read as measured.
        # Driven by QUANTILE_LEVELS rather than a hardcoded tuple: the list was
        # p50/p90/p99, and quantiles_at_floor is monotone in rank, so once p5
        # was published there was a whole band -- 5% to 50% below floor -- in
        # which the warning named a quantile the line never printed.
        at_floor = set((stats.get("distribution") or {}).get("quantiles_at_floor") or ())
        parts = [
            f"{name} {'<=' if name in at_floor else ''}{format_si(current[name], 'A')}"
            for name, _level in WindowStats.QUANTILE_LEVELS
            if current.get(name) is not None
        ]
        if parts:
            lines.append("  distribution: " + ", ".join(parts))
    if stats.get("charge_uc") is not None:
        charge = format_with_uncertainty(stats["charge_uc"], uncertainty.get("charge_uc_typical"))
        lines.append(f"  charge: {charge} uC")
    if stats.get("energy_uj") is not None:
        energy = format_with_uncertainty(stats["energy_uj"], uncertainty.get("energy_uj_typical"))
        lines.append(f"  energy: {energy} uJ")
    if uncertainty:
        stderr = uncertainty.get("mean_ua_batch_stderr")
        note = "typical, per-range; not guaranteed"
        if stderr is not None:
            # Never folded into the figure above: one is a systematic gain
            # specification, the other is how settled this particular mean is.
            note += f"; batch stderr of the mean {format_si(stderr, 'A')}"
        lines.append(f"  uncertainty: {note}")
    split = stats.get("state_split")
    if split:
        below, above = split["below"], split["above"]
        lines.append(f"  states at {format_si(split['threshold_ua'], 'A')}:")
        for name, part in (("below", below), ("above", above)):
            mean_text = "n/a" if part["mean_ua"] is None else format_si(part["mean_ua"], "A")
            lines.append(
                f"    {name}: {part['duration_s']:.6g} s, mean {mean_text}, "
                f"charge {format_si(part['charge_uc'], 'C')}, {part['runs']} run(s)"
            )
    lines.append(f"  complete: {stats['complete']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# read-only commands


def cmd_discover(args: Any) -> Outcome:
    devices = discover(simulate=args.simulate)
    result = {"devices": [d.to_json() for d in devices]}
    warnings: list[Diagnostic | str] = []
    if not devices:
        warnings.append(
            warn(
                W_DEVICE_NOT_FOUND,
                "no PPK2 devices found; check the USB connection and permissions, then run "
                "`ppk2lab doctor --json`",
            )
        )
    lines = []
    for dev in devices:
        tag = " (simulated)" if dev.simulated else ""
        lines.append(f"{dev.serial_number}{tag}")
        for port in dev.ports:
            lines.append(f"  {port.role.value:<12} {port.path}")
    human = "\n".join(lines) if lines else "no PPK2 devices found"
    return Outcome(result, warnings, human)


def cmd_info(args: Any) -> Outcome:
    device = _open_device(args)
    try:
        metadata = device.metadata
        assert metadata is not None
        missing = device.calibration.missing_ranges() if device.calibration else []
        fingerprint = _cli_fingerprint(device)
        result = {
            "device": device.info.to_json(),
            "state": device.state.to_json(),
            "metadata": metadata.to_json(),
            "calibration_missing_ranges": missing,
            "firmware_fingerprint": fingerprint,
        }
        warnings: list[Diagnostic | str] = [warn(W_METADATA, m) for m in metadata.warnings]
        if device.recovered_stale_bytes:
            warnings.append(
                warn(
                    W_SESSION_RECOVERED,
                    f"recovered an interrupted session: discarded "
                    f"{device.recovered_stale_bytes} stale stream bytes at open",
                )
            )
        if missing:
            warnings.append(
                warn(
                    W_CALIBRATION_INCOMPLETE,
                    f"calibration constants missing for range(s) {missing}; samples in "
                    "those ranges cannot be converted to current",
                )
            )
        if metadata.calibrated is False:
            warnings.append(
                warn(
                    W_NOT_CALIBRATED,
                    "device metadata reports Calibrated: 0; absolute accuracy is "
                    "unconfirmed (the flag's meaning is not hardware-verified)",
                )
            )
        state = device.state.to_json()
        human = "\n".join(
            [
                f"device: {device.info.serial_number}"
                + (" (simulated)" if device.info.simulated else ""),
                f"mode: {state['mode']}",
                f"source_voltage_mv: {device.state.source_voltage_mv}",
                # Printed because the JSON has carried them all along and the
                # human form did not: `dut_power` unknown is the state a
                # near-zero reading is explained by, and the voltage's basis
                # is what says whether that number describes the DUT at all.
                f"dut_power: {_fmt_state_value(state['dut_power'])}",
                f"measuring: {_fmt_state_value(state['measuring'])}",
                f"source_voltage_basis: {state['source_voltage_basis']}",
                f"calibrated: {metadata.calibrated}",
                f"metadata terminated: {metadata.terminated}",
                f"firmware fingerprint: {_fmt_fingerprint(fingerprint)}",
            ]
        )
        return Outcome(result, warnings, human)
    finally:
        device.close()


def _option_json(action: argparse.Action) -> dict[str, Any]:
    """One option as an agent needs it: the flags, what it does, what it accepts.

    ``choices`` are stringified because the JSON has to be uniform and some
    are integers (``--spi-mode`` is ``0-3``): an agent building a command line
    writes strings either way.
    """
    entry: dict[str, Any] = {
        "flags": list(action.option_strings) or [action.dest],
        "help": action.help or "",
        "required": bool(getattr(action, "required", False)),
    }
    if action.choices is not None:
        entry["choices"] = [str(choice) for choice in action.choices]
    return entry


def cmd_capabilities(args: Any) -> Outcome:
    parser = build_parser()
    commands = []
    subparser_actions = [
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    ]
    for action in subparser_actions:
        for name, subparser in action.choices.items():
            options = []
            for sub_action in subparser._actions:
                # argparse.SUPPRESS marks the per-subcommand copies of the
                # global flags; publishing the sentinel string as help text
                # tells a reader nothing. They appear under global_options.
                if sub_action.dest in ("help",) or sub_action.help is argparse.SUPPRESS:
                    continue
                options.append(_option_json(sub_action))
            commands.append(
                {
                    "name": name,
                    "state_changing": name in STATE_CHANGING_COMMANDS,
                    "category": COMMAND_CATEGORIES.get(name, "unknown"),
                    "description": subparser.description or "",
                    "options": options,
                }
            )
    global_options = [
        _option_json(a)
        for a in parser._actions
        if a.option_strings and a.help is not argparse.SUPPRESS and a.dest != "help"
    ]
    result = {
        "device": {
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "digital_channels": list(DIGITAL_CHANNELS),
            "modes": [m.name.lower() for m in Mode],
            "source_voltage_mv": {"min": VOLTAGE_MIN_MV, "max": VOLTAGE_MAX_MV},
        },
        "commands": commands,
        "global_options": global_options,
        "decoders": decoder_capabilities(),
        "exit_codes": {
            "0": "success",
            "1": "assertion failed",
            "2": "usage / schema / invalid input",
            "3": "device not found",
            "4": "permission denied or port busy",
            "5": "protocol / firmware / metadata mismatch",
            "6": "capture incomplete (data loss)",
            "7": "optional capability missing",
            "8": "unsafe state change refused",
            "9": "internal error",
        },
        "error_codes": error_catalog(),
        "warning_codes": warning_catalog(),
        "gap_reasons": gap_reason_catalog(),
        "interruption_reasons": interruption_reason_catalog(),
        "schemas": list_schemas(),
    }
    human = (
        f"ppk2lab {__version__}: {len(commands)} commands, "
        f"{len(result['decoders'])} decoders, {len(result['schemas'])} schemas "
        "(use --json for the machine-readable manifest)"
    )
    return Outcome(result, [], human)


def cmd_schema(args: Any) -> Outcome:
    if args.list or not args.name:
        names = list_schemas()
        return Outcome({"schemas": names}, [], "\n".join(names))
    schema = get_schema(args.name)
    return Outcome(None, raw_output=json.dumps(schema, indent=2, sort_keys=True) + "\n")


def _doctor_outcome(checks: list[dict[str, Any]], fingerprint: dict[str, Any] | None) -> Outcome:
    """Render doctor's checks and map them onto the frozen exit-code table.

    The exit code is the *first* failing check's, not the worst: the checks
    run in dependency order, so the earliest failure is the one to act on.
    ``ppk2lab doctor --json || exit`` is the pre-flight reflex this exists
    for, and it was a no-op while every run returned 0.
    """
    summary = {
        status: sum(1 for c in checks if c["status"] == status)
        for status in ("pass", "fail", "warn", "skip")
    }
    exit_code = next((c["exit_code"] for c in checks if c["status"] == "fail"), EXIT_OK)
    icons = {"pass": "ok  ", "fail": "FAIL", "warn": "warn", "skip": "skip"}
    lines = [f"[{icons[c['status']]}] {c['name']}: {c['detail']}" for c in checks]
    lines.append(
        f"{summary['pass']} pass, {summary['fail']} fail, {summary['warn']} warn, "
        f"{summary['skip']} skip"
    )
    result = {
        "checks": checks,
        "summary": summary,
        "exit_code": exit_code,
        "firmware_fingerprint": fingerprint,
    }
    return Outcome(result, [], "\n".join(lines), exit_code=exit_code)


def cmd_doctor(args: Any) -> Outcome:
    checks: list[dict[str, Any]] = []
    fingerprint: dict[str, Any] | None = None

    def check(
        name: str,
        status: str,
        detail: str,
        remediation: str | None = None,
        *,
        exit_code: int = EXIT_INTERNAL,
    ) -> None:
        # Every check names the exit code its failure maps to, so a caller can
        # tell a missing device (3) from a busy port (4) without parsing prose.
        # warn/skip stay non-blocking by contract: they never carry a code.
        checks.append(
            {
                "name": name,
                "status": status,
                "detail": detail,
                "remediation": remediation,
                "exit_code": exit_code if status == "fail" else EXIT_OK,
            }
        )

    py = sys.version_info
    check(
        "python_version",
        "pass" if py >= (3, 11) else "fail",
        f"{py.major}.{py.minor}.{py.micro} on {platform.system()}",
        None if py >= (3, 11) else "ppk2lab requires Python 3.11 or newer",
        exit_code=EXIT_USAGE,
    )
    check("ppk2lab_version", "pass", __version__)
    try:
        import serial

        check("pyserial", "pass", f"pyserial {serial.__version__}")
    except ImportError:
        check(
            "pyserial",
            "fail",
            "pyserial is not importable",
            "reinstall with `pip install ppk2lab`",
            exit_code=EXIT_USAGE,
        )

    try:
        devices = discover(simulate=args.simulate)
        if devices:
            serials = ", ".join(str(d.serial_number) for d in devices)
            check("devices_found", "pass", f"{len(devices)} device(s): {serials}")
        else:
            check(
                "devices_found",
                "fail",
                "no PPK2 devices detected",
                "check the USB cable and, on Linux, serial-port group membership (see INSTALL.md)",
                exit_code=EXIT_DEVICE_NOT_FOUND,
            )
        target = None
        if devices:
            target = args.device or devices[0].serial_number
    except Exception as exc:
        check(
            "devices_found",
            "fail",
            f"discovery failed: {exc}",
            "check USB permissions; see INSTALL.md",
            exit_code=EXIT_DEVICE_NOT_FOUND,
        )
        devices, target = [], None

    if not devices:
        check("device_selection", "skip", "no device to choose between")
    elif len(devices) > 1 and not args.device and not args.port:
        # Warn, not fail: the run below is still valid evidence, but it is
        # evidence about one unit and the report has to say which.
        check(
            "device_selection",
            "warn",
            f"{len(devices)} devices attached; checking {target} because no "
            "--device/--port was given",
            "pass --device SERIAL (or --port PATH) so the report names the unit it describes",
        )
    else:
        check("device_selection", "pass", "one device, or an explicit --device/--port")

    if target is not None:
        try:
            device = PPK2.open(
                serial_number=None if args.simulate else target,
                port=args.port,
                simulate=args.simulate,
            )
            try:
                check("port_open", "pass", device.transport.description)
                if device.recovered_stale_bytes:
                    check(
                        "session_recovery",
                        "warn",
                        f"discarded {device.recovered_stale_bytes} stale stream bytes "
                        "left by a previous session",
                        "a previous session was interrupted while measuring; recovery is automatic",
                    )
                else:
                    check("session_recovery", "pass", "no stale stream data at open")
                metadata = device.metadata
                if metadata is None:
                    check(
                        "metadata_read",
                        "fail",
                        "device returned no parsable metadata",
                        "retry once; if it persists, file a compatibility report with "
                        "`ppk2lab info --json`",
                        exit_code=EXIT_PROTOCOL,
                    )
                    return _doctor_outcome(checks, fingerprint)
                check(
                    "metadata_read",
                    "pass" if metadata.terminated else "warn",
                    "terminated by END" if metadata.terminated else "reply not terminated by END",
                    None if metadata.terminated else "retry; report if it persists",
                    exit_code=EXIT_PROTOCOL,
                )
                fingerprint = _cli_fingerprint(device)
                check(
                    "firmware_fingerprint",
                    "pass",
                    _fmt_fingerprint(fingerprint),
                    "record this with every compatibility-matrix row; the PPK2 reports no "
                    "firmware version over the measurement port",
                )
                cal = device.calibration
                gains = (
                    [
                        (i, rc.ug)
                        for i, rc in enumerate(cal.ranges)
                        if rc is not None and rc.ug != 1.0
                    ]
                    if cal
                    else []
                )
                check(
                    "calibrated_flag",
                    "pass" if metadata.calibrated else "warn",
                    f"device reports Calibrated: {int(bool(metadata.calibrated))}",
                    None
                    if metadata.calibrated
                    else "absolute accuracy is unconfirmed; the flag's meaning is not "
                    "hardware-verified",
                )
                check(
                    "user_gain",
                    "pass" if not gains else "warn",
                    "all user gains are unity"
                    if not gains
                    else "non-unity user gain: " + ", ".join(f"range {i}: {g:g}" for i, g in gains),
                    None if not gains else "every reading in those ranges is scaled by it",
                )
                missing = cal.missing_ranges() if cal else list(range(5))
                check(
                    "calibration",
                    "pass" if not missing else "warn",
                    "all 5 ranges calibrated"
                    if not missing
                    else f"missing constants for range(s) {missing}",
                    None if not missing else "currents in those ranges will be NaN",
                )
                if args.stream_check:
                    duration = parse_duration_s(args.stream_check)
                    result = device.capture(duration_s=duration)
                    stats = result.stats
                    expected = int(duration * SAMPLE_RATE_HZ)
                    stored = stats.stored_samples if stats else 0
                    missing_n = stats.missing_samples_known if stats else 0
                    rate_ok = stored + missing_n >= expected
                    gap_free = (stats.gap_count == 0) if stats else False
                    check(
                        "stream_rate",
                        "pass" if rate_ok else "fail",
                        f"{stored} stored + {missing_n} missing of {expected} expected",
                        None if rate_ok else "stream stalled; check USB and system load",
                        exit_code=EXIT_CAPTURE_INCOMPLETE,
                    )
                    check(
                        "stream_gaps",
                        "pass" if gap_free else "warn",
                        "no sample gaps"
                        if gap_free
                        else f"{stats.gap_count if stats else '?'} gap(s) during the check",
                        None
                        if gap_free
                        else "reduce system load; gaps are always marked in captures",
                    )
                else:
                    check("stream_rate", "skip", "not run (opt-in via --stream-check 1s)")
            finally:
                device.close()
        except Exception as exc:
            # A typed error already knows what kind of failure it is (busy
            # port 4, wrong firmware 5, ...); anything else really is internal.
            check(
                "port_open",
                "fail",
                str(exc),
                getattr(exc, "remediation", None)
                or "run `ppk2lab discover --json`; close other apps using the port",
                exit_code=getattr(exc, "exit_code", EXIT_INTERNAL),
            )

    return _doctor_outcome(checks, fingerprint)


# ---------------------------------------------------------------------------
# state-changing / measurement commands


#: The third-party names `ppk2lab[web]` installs.
#:
#: Checked by name rather than by catching ImportError around the server
#: import: an ImportError raised from inside the server for an unrelated reason
#: would otherwise be reported as "install the extra", sending a maintainer to
#: pip to fix a typo. Naming `websockets` individually matters most -- base
#: uvicorn ships no WebSocket implementation, so someone who installed the
#: other two by hand would otherwise get a console that renders and never
#: connects.
_WEB_REQUIREMENTS = ("starlette", "uvicorn", "websockets")


def _require_web_extra() -> None:
    import importlib.util

    missing = [name for name in _WEB_REQUIREMENTS if importlib.util.find_spec(name) is None]
    if missing:
        raise WebExtraMissingError(
            "`ppk2lab web` needs the web extra, which is not installed "
            f"(missing: {', '.join(missing)})"
        )


def cmd_web(args: Any) -> Outcome:
    """Serve the browser console against one open device.

    The server owns that device for its whole lifetime, because a serial port
    is exclusive and DUT power does not survive it closing. While it runs, any
    other `ppk2lab` command against the same unit gets PORT_BUSY -- and closing
    the browser tab does not de-energise VOUT. Only stopping the server does.
    """
    if args.no_token and args.allow_control:
        raise UsageError(
            "--no-token cannot be combined with --allow-control",
            remediation="Loopback is not an authorization boundary: any local process can "
            "connect, and the token is what separates the console this server handed out "
            "from anything else on the machine. Drop one of the two flags.",
        )
    host = args.host
    if args.allow_control and not _is_loopback(host):
        raise UsageError(
            f"--allow-control is only available on a loopback address, not on {host!r}",
            remediation="Serve the viewer on that address if you need to, and reach the "
            "controls over an authenticated tunnel instead: "
            "`ssh -L 8765:127.0.0.1:8765 <bench-host>`.",
        )

    _require_web_extra()
    # Imported here, never at module scope: this module is loaded for all
    # fourteen commands, and `ppk2lab discover` must not pay for a web server.
    from ppk2lab_web.server import serve

    def open_device() -> PPK2:
        if args.simulate:
            # Explicitly rate-limited. `PPK2.open(simulate=True)` builds a
            # simulator with no limit, which streams as fast as the consumer
            # asks -- it would pin a core and run the "live" trace ahead of
            # wall-clock time.
            from ..testing.profiles import DemoActivityProfile
            from ..transport.mock import MockTransport, SimulatedPPK2
            from ..types import SAMPLE_RATE_HZ

            simulator = SimulatedPPK2(
                profile=DemoActivityProfile(), rate_limit_hz=float(SAMPLE_RATE_HZ)
            )
            return PPK2.open(
                transport=MockTransport(simulator),
                simulate=True,
                max_voltage_mv=getattr(args, "max_voltage_mv", None),
            )
        return _open_device(args)

    result = serve(
        open_device=open_device,
        host=host,
        port=args.http_port,
        allow_control=args.allow_control,
        require_token=not args.no_token,
        allow_origin=tuple(args.allow_origin),
        open_browser=args.open_browser,
        autostart=not args.no_autostart,
    )
    payload = result.to_json()
    warnings: list[Diagnostic | str] = []
    if result.simulated:
        warnings.append(
            warn(
                W_GENERIC,
                "the console served a simulated device; nothing on screen was measured",
            )
        )
    if result.shutdown_reason == "device_lost":
        return Outcome(
            result=payload,
            warnings=warnings,
            human=f"served {result.url} for {result.listened_s:.0f} s; the device was lost",
            exit_code=EXIT_PERMISSION,
        )
    return Outcome(
        result=payload,
        warnings=warnings,
        human=f"served {result.url} for {result.listened_s:.0f} s",
    )


def _is_loopback(host: str) -> bool:
    import ipaddress

    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("localhost",)


def cmd_configure(args: Any) -> Outcome:
    if args.mode is None and args.voltage_mv is None and args.dut_power is None:
        raise UsageError(
            "nothing to configure; pass at least one of --mode, --voltage-mv, --dut-power"
        )
    dry_run = not args.apply
    device = _open_device(args)
    try:
        changes = []
        if args.mode is not None:
            mode = Mode.AMPERE if args.mode == "ampere" else Mode.SOURCE
            changes.append(device.set_mode(mode, dry_run=dry_run))
        if args.voltage_mv is not None:
            changes.append(device.set_source_voltage_mv(args.voltage_mv, dry_run=dry_run))
        if args.dut_power is not None:
            changes.append(device.set_dut_power(args.dut_power == "on", dry_run=dry_run))
        warnings: list[Diagnostic | str] = []
        if args.dut_power == "on" and not dry_run:
            # The device de-energizes VOUT once the host closes the port —
            # measured at under half a second — so this command cannot leave a
            # DUT powered for a later, separate one. Saying so is the whole
            # point: the result reports dut_power true, and that stops being
            # true moments after the process exits.
            warnings.append(
                warn(
                    W_DUT_POWER_TRANSIENT,
                    "DUT power is enabled now, but the device drops VOUT shortly after this "
                    "command closes the port, so a later `capture` would measure an "
                    "unpowered DUT. Take a powered measurement inside one open session "
                    "(ppk2lab.PPK2 in Python), and verify it from the current itself — "
                    "this hardware cannot report its power state back",
                )
            )
        if dry_run:
            warnings.append(
                warn(
                    W_DRY_RUN,
                    "dry run: no hardware state was changed; re-run with --apply to apply",
                )
            )
        for change in changes:
            # A change's own warnings are prose about the operation; whether
            # the device confirmed the result is a separate fact the record
            # already states, so it is read from `observed_after` rather than
            # pattern-matched out of a sentence anyone could reword.
            warnings.extend(warn(W_GENERIC, text) for text in change.warnings)
            if change.applied and not change.observed_after:
                warnings.append(
                    warn(
                        W_STATE_UNVERIFIED,
                        f"{change.operation}: applied but the resulting state was not "
                        "read back from the device",
                    )
                )
        result = {
            "dry_run": dry_run,
            "changes": [c.to_json() for c in changes],
            "state": device.state.to_json(),
        }
        lines: list[str] = []
        for change in changes:
            lines.extend(_fmt_change_lines(change))
        # A dry run's trailing state is the state the device is still in, not
        # the projected one; calling it "after" would read as the projection.
        lines.append("state (unchanged):" if dry_run else "state after:")
        lines.extend(_fmt_state_lines(device.state.to_json()))
        return Outcome(result, warnings, "\n".join(lines))
    finally:
        # configure intentionally leaves the applied state in place
        device.close(restore_power=False)


def _spi_config_from_args(args: Any) -> dict[str, Any]:
    config: dict[str, Any] = {}
    if getattr(args, "spi_sclk", None):
        config["sclk"] = args.spi_sclk
    if getattr(args, "spi_mosi", None):
        config["mosi"] = args.spi_mosi
    if getattr(args, "spi_miso", None):
        config["miso"] = args.spi_miso
    if getattr(args, "spi_cs", None):
        config["cs"] = args.spi_cs
    config["mode"] = getattr(args, "spi_mode", 0)
    config["word_bits"] = getattr(args, "spi_word_bits", 8)
    if getattr(args, "spi_lsb_first", False):
        config["msb_first"] = False
    if getattr(args, "spi_cs_active_high", False):
        config["cs_active_low"] = False
    if getattr(args, "spi_clock_hz", None):
        config["expected_clock_hz"] = args.spi_clock_hz
    return config


def _parse_tags(pairs: Sequence[str] | None) -> dict[str, str]:
    """Turn repeated ``KEY=VALUE`` arguments into a tag mapping.

    Splits on the first ``=`` only, so a value may contain one.
    """
    tags: dict[str, str] = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise UsageError(
                f"--tag expects KEY=VALUE, got {pair!r}",
                remediation="Write it as --tag sn=POD01. Quote the whole argument when the "
                "value contains spaces.",
            )
        if key in tags:
            raise UsageError(
                f"--tag {key}= was given more than once",
                remediation="A key records one fact. Use distinct keys.",
            )
        tags[key] = value
    return tags


def cmd_capture(args: Any) -> Outcome:
    parse_channel_set(args.digital)  # validate the channel spec early
    duration_s = parse_duration_s(args.duration) if args.duration else None
    if duration_s is None and args.samples is None and args.trigger is None:
        raise UsageError("capture needs --duration, --samples, or --trigger")
    # Parsed before the device is touched: a malformed tag must not cost a run.
    tags = _parse_tags(getattr(args, "tag", None))

    device = _open_device(args)
    try:
        trigger_engine = None
        trigger_timeout_s = None
        if args.trigger:
            detector = parse_trigger_spec(
                args.trigger,
                hold_samples=args.trigger_hold,
                spi_config=_spi_config_from_args(args),
                allow_experimental=args.allow_experimental,
            )
            pre_s = parse_duration_s(args.pre) if args.pre else 0.0
            post_s = parse_duration_s(args.post) if args.post else (duration_s or 1.0)
            trigger_engine = TriggerEngine(
                detector,
                pre_samples=round(pre_s * SAMPLE_RATE_HZ),
                post_samples=round(post_s * SAMPLE_RATE_HZ),
                calibration=device.calibration,
                vdd_mv=device.state.source_voltage_mv,
            )
            if args.trigger_timeout:
                trigger_timeout_s = parse_duration_s(args.trigger_timeout)

        result = device.capture(
            duration_s=None if trigger_engine else duration_s,
            sample_limit=args.samples,
            output=args.output,
            overwrite=args.overwrite,
            trigger_engine=trigger_engine,
            trigger_timeout_s=trigger_timeout_s,
            assume_voltage_mv=args.assume_voltage_mv,
            in_memory_limit_samples=None if args.in_memory else DEFAULT_IN_MEMORY_LIMIT_SAMPLES,
            tags=tags,
        )
    finally:
        device.close()

    payload = result.to_json()
    lines = [f"capture {result.capture_id}"]
    if result.path:
        lines.append(f"  written to: {result.path}")
    if result.stats:
        lines.append(_fmt_stats(result.stats.to_json()))
    outcome = Outcome(payload, list(result.warnings), "\n".join(lines))
    if not result.complete:
        err = CaptureIncompleteError(
            "capture is incomplete (interrupted, trigger missed, or sample gaps); "
            "partial data and loss markers were preserved"
        )
        outcome.error = err.to_json()
        outcome.exit_code = EXIT_CAPTURE_INCOMPLETE
    return outcome


# ---------------------------------------------------------------------------
# offline commands


def cmd_inspect(args: Any) -> Outcome:
    """Read a capture's manifest without materializing a single sample.

    Every other offline command loads the whole sample array, so the files
    most in need of a first look — long soak runs — were the ones nothing
    could open. This touches ``manifest.json`` only: no chunk is read, and
    therefore neither the per-chunk CRC32s nor the whole-file SHA-256 are
    verified. That is reported rather than glossed over.
    """
    with ArtifactReader(args.capture) as reader:
        manifest = reader.manifest
        samples = manifest.get("samples", {})
        gaps = manifest.get("gaps", [])
        gaps_truncated = int(manifest.get("gaps_truncated", 0) or 0)
        stored = int(samples.get("stored_count", 0))
        missing_known = sum(g["missing"] for g in gaps if isinstance(g.get("missing"), int))
        unknown_gaps = sum(1 for g in gaps if g.get("missing") is None)
        # The enumerated gap table is the only loss the manifest can price, so
        # a truncated table or an unknown-size gap makes the span a floor.
        span_is_lower_bound = bool(gaps_truncated or unknown_gaps)
        interruption = manifest.get("interruption")
        meta = reader.read_meta()
        result: dict[str, Any] = {
            "path": str(reader.path),
            "format": manifest.get("format"),
            "format_version": manifest.get("format_version"),
            "capture_id": manifest.get("capture_id"),
            "created_utc": manifest.get("created_utc"),
            "device": manifest.get("device", {}),
            "configuration": manifest.get("configuration", {}),
            "user_tags": meta.user_tags,
            "scheduled_actions": meta.scheduled_actions,
            "timeline": manifest.get("timeline", {}),
            "duration_s": (stored + missing_known) / reader.sample_rate_hz,
            "duration_is_lower_bound": span_is_lower_bound,
            "samples": {
                "encoding": samples.get("encoding"),
                "stored": stored,
                "missing_known": missing_known,
                "invalid_range_count": samples.get("invalid_range_count"),
                "chunk_count": len(samples.get("chunks", [])),
                "sha256": samples.get("sha256"),
                # A manifest-only read verifies nothing about the sample bytes.
                "sha256_verified": False,
            },
            "gap_count": len(gaps),
            "gaps_truncated": gaps_truncated,
            "gaps_with_unknown_size": unknown_gaps,
            "complete": bool(manifest.get("complete", False)),
            "interruption": interruption,
            "calibration": manifest.get("calibration"),
            "stats": manifest.get("stats"),
            # Normalized rather than passed through: an artifact written
            # before `category` existed must not report a different warning
            # shape than one written today.
            "warnings": as_json(manifest.get("warnings", [])),
        }

    warnings: list[Diagnostic | str] = []
    if result["gap_count"]:
        warnings.append(
            warn(
                W_SAMPLE_GAPS,
                f"capture records {result['gap_count']} sample gap(s) "
                f"({missing_known:,} samples known missing); integrals over them are "
                "lower bounds",
            )
        )
    if gaps_truncated:
        warnings.append(
            warn(
                W_GAP_TABLE_TRUNCATED,
                f"the gap table stops at {len(gaps):,} entries and {gaps_truncated:,} further "
                "gap(s) were counted but not enumerated; loss is understated per-location, "
                "never in total",
            )
        )
    if interruption:
        warnings.append(
            warn(
                W_INTERRUPTED,
                f"capture was interrupted ({interruption.get('reason', 'unknown')}); "
                "partial data was preserved",
            )
        )

    lines = [
        f"capture {result['capture_id']} ({result['created_utc']})",
        f"  samples: {stored:,} stored, {missing_known:,} known missing, "
        f"{result['samples']['chunk_count']} chunk(s)",
        f"  duration: {result['duration_s']:.6g} s"
        + (" (lower bound)" if span_is_lower_bound else ""),
        f"  gaps: {result['gap_count']}"
        + (f" (+{gaps_truncated} not enumerated)" if gaps_truncated else ""),
        f"  complete: {result['complete']}",
        "  sample integrity: not verified (manifest-only read)",
    ]
    return Outcome(result, warnings, "\n".join(lines))


def _build_decoder(args: Any):
    if args.uart and getattr(args, "spi_sclk", None):
        raise UsageError("choose one decoder per run: --uart or --spi-sclk")
    if args.uart:
        return UARTDecoder(
            rx=args.uart,
            baud=args.baud,
            data_bits=args.data_bits,
            parity=None if args.parity == "none" else args.parity,
            stop_bits=args.stop_bits,
            msb_first=args.msb_first,
            invert=args.invert,
            allow_experimental=args.allow_experimental,
        )
    if getattr(args, "spi_sclk", None):
        config = _spi_config_from_args(args)
        sclk = config.pop("sclk")
        return SPIDecoder(sclk=sclk, allow_experimental=args.allow_experimental, **config)
    raise UsageError("select a decoder: --uart Dn or --spi-sclk Dn (with --spi-mosi/miso)")


def cmd_decode(args: Any) -> Outcome:
    capture = _load_capture(args)
    decoder = _build_decoder(args)
    feasibility = getattr(decoder, "feasibility", None)
    warnings: list[Diagnostic | str] = (
        [warn(W_DECODER_RATE, text) for text in feasibility.warnings] if feasibility else []
    )
    # A code names one thing. An interrupted but gap-free capture is not a
    # capture with sample gaps, and a caller branching on the code rather than
    # reading the prose would have been told the wrong thing.
    if capture.gaps:
        warnings.append(
            warn(
                W_SAMPLE_GAPS,
                f"capture records {len(capture.gaps)} sample gap(s); events touching missing "
                "data carry gap errors and zero confidence",
            )
        )
    if capture.interruption:
        warnings.append(
            warn(
                W_INTERRUPTED,
                "capture was interrupted; it ends earlier than requested and events near the "
                "end may be truncated",
            )
        )
    annotations = decode_capture(capture, decoder)
    kinds: dict[str, int] = {}
    error_count = 0
    for ann in annotations:
        kinds[ann.kind] = kinds.get(ann.kind, 0) + 1
        if ann.errors:
            error_count += 1
    result: dict[str, Any] = {
        "decoder": decoder.describe(),
        "annotation_count": len(annotations),
        "summary": {"kinds": kinds, "with_errors": error_count},
        "capture_id": capture.meta.capture_id,
        "capture_sha256": capture.sha256(),
        "output": None,
        "annotations": None,
    }
    if args.output:
        if not args.overwrite:
            import os

            if os.path.exists(args.output):
                raise OutputExistsError(f"output file exists: {args.output}")
        save_annotations_jsonl(annotations, args.output)
        result["output"] = args.output
    else:
        result["annotations"] = [a.to_json() for a in annotations]
    human = f"{len(annotations)} annotations ({kinds}); {error_count} with errors" + (
        f"\nwritten to: {args.output}" if args.output else ""
    )
    return Outcome(result, warnings, human)


def cmd_measure(args: Any) -> Outcome:
    state_threshold_ua = (
        parse_current_ua(args.state_threshold) if getattr(args, "state_threshold", None) else None
    )
    warnings: list[Diagnostic | str] = []
    if args.window and args.annotations:
        # Annotations carry their own sample windows and can point anywhere in
        # the capture, so there is nothing coherent for --window to mean here.
        # Ignoring it silently would be a measurement over a range nobody asked
        # for, reported without saying so.
        raise UsageError(
            "--window and --annotations cannot be combined; each annotation already "
            "carries the exact sample window it is measured over",
            remediation="Measure the annotations, or measure a window — not both. To "
            "restrict which annotations are measured, filter the JSONL first.",
        )
    # A window is read from the chunks it falls in, so measuring a slice of an
    # hour-scale artifact costs the window rather than the file. Annotations
    # can point anywhere in the capture, so that path still reads it whole.
    bounds = _parse_window(args.window) if args.window else None
    if bounds is not None:
        capture = _load_window(args, *bounds)
        warnings.append(
            warn(
                W_PARTIAL_INTEGRITY,
                "only the chunks this window falls in were read and CRC-checked; "
                "capture_sha256 is null because the manifest's whole-file digest was "
                "not verified",
            )
        )
    else:
        capture = _load_capture(args)
    result: dict[str, Any] = {
        "capture_id": capture.meta.capture_id,
        "capture_sha256": capture.artifact_sha256,
        "window": None,
        "annotations": None,
        "groups": None,
    }
    if capture.gaps:
        warnings.append(
            warn(
                W_SAMPLE_GAPS,
                f"capture records {len(capture.gaps)} sample gap(s); a window that overlaps "
                "missing data reports complete=false and its charge/energy are lower bounds",
            )
        )
    if capture.interruption:
        warnings.append(
            warn(
                W_INTERRUPTED,
                "capture was interrupted; a window that extends past the stored samples "
                "reports complete=false and its charge/energy are lower bounds",
            )
        )
    human_parts: list[str] = []
    if args.annotations:
        annotations = load_annotations_jsonl(args.annotations)
        group_by = args.group_by
        kinds = None
        if group_by in ("frame", "transaction"):
            kinds = [group_by]
            group_by = "annotation"
        entries = measure_annotations(
            capture,
            annotations,
            kinds=kinds,
            group_by=group_by,
            filtered=args.filtered,
            assume_voltage_mv=args.assume_voltage_mv,
        )
        if group_by == "kind":
            result["groups"] = entries
            for group in entries:
                energy = group["total_energy_uj"]
                energy_text = "unknown" if energy is None else f"{energy:.4g} uJ"
                human_parts.append(
                    f"{group['kind']}: n={group['count']} "
                    f"charge={group['total_charge_uc']:.4g} uC energy={energy_text}"
                )
        else:
            result["annotations"] = entries
            human_parts.append(f"{len(entries)} measured events")
    elif bounds is not None:
        start_s, end_s = bounds
        stats = measure_window(
            capture,
            start_s=start_s,
            end_s=end_s,
            filtered=args.filtered,
            assume_voltage_mv=args.assume_voltage_mv,
            state_threshold_ua=state_threshold_ua,
        )
        result["window"] = stats.to_json()
        warnings.extend(stats.diagnostics())
        human_parts.append(_fmt_stats(result["window"]))
    else:
        stats = measure_window(
            capture,
            filtered=args.filtered,
            assume_voltage_mv=args.assume_voltage_mv,
            state_threshold_ua=state_threshold_ua,
        )
        result["window"] = stats.to_json()
        # The window's own findings — an unpopulated span, clipped samples, a
        # wall-clock deficit the gap table cannot localize — are coded on the
        # stats object; surfacing them here is what lets an agent branch on
        # them instead of re-deriving them from the numbers.
        warnings.extend(stats.diagnostics())
        human_parts.append(_fmt_stats(result["window"]))
    return Outcome(result, warnings, "\n".join(human_parts))


def _load_rules(args: Any) -> list[AssertionRule]:
    rules: list[AssertionRule] = [parse_rule(text) for text in args.rule]
    if args.rules_file:
        try:
            with open(args.rules_file, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError as exc:
            raise UsageError(f"rules file not found: {args.rules_file}") from exc
        except json.JSONDecodeError as exc:
            raise UsageError(f"rules file is not valid JSON: {exc}") from exc
        if not isinstance(data, list):
            raise UsageError("rules file must contain a JSON array")
        for item in data:
            if isinstance(item, str):
                rules.append(parse_rule(item))
            elif isinstance(item, dict):
                rules.append(AssertionRule.from_json(item))
            else:
                raise UsageError("rules file entries must be strings or objects")
    if not rules:
        raise UsageError("no rules given; pass --rule or --rules-file")
    return rules


def cmd_assert(args: Any) -> Outcome:
    capture = _load_capture(args)
    rules = _load_rules(args)
    decoder_config: dict[str, Any] = {"uart": {"rx": args.uart_rx, "baud": args.baud}}
    spi_config = _spi_config_from_args(args)
    if "sclk" in spi_config:
        decoder_config["spi"] = spi_config
    outcomes: list[AssertionOutcome] = [
        evaluate_assertion(
            capture,
            rule,
            decoder_config=decoder_config,
            allow_experimental=args.allow_experimental,
            assume_voltage_mv=args.assume_voltage_mv,
        )
        for rule in rules
    ]
    passed = all(o.passed for o in outcomes)
    result = {"outcomes": [o.to_json() for o in outcomes], "passed": passed}

    if any(o.status == "incomplete" for o in outcomes):
        exit_code = EXIT_CAPTURE_INCOMPLETE
    elif not passed:
        exit_code = EXIT_ASSERTION_FAILED
    else:
        exit_code = 0

    if args.report_format == "junit":
        report = junit_report(outcomes)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as fh:
                fh.write(report)
            return Outcome(
                result, [], f"junit report written to {args.output}", exit_code=exit_code
            )
        return Outcome(None, raw_output=report, exit_code=exit_code)

    lines = []
    for outcome in outcomes:
        lines.append(f"[{outcome.status}] {outcome.rule.source or outcome.rule.metric_name}")
        for obs in outcome.observations:
            lines.append(
                f"  window {obs['window']['start_sample']}..{obs['window']['end_sample']}: "
                f"observed {obs['observed']} {obs['op']} {obs['threshold']} -> {obs['status']}"
            )
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, sort_keys=True)
    warnings = [w for o in outcomes for w in o.warnings]
    return Outcome(result, warnings, "\n".join(lines), exit_code=exit_code)


def cmd_compare(args: Any) -> Outcome:
    """Difference two captures on one metric, and say what the number is worth."""
    max_samples = _parse_max_samples(getattr(args, "max_samples", None))
    warnings: list[Diagnostic | str] = []
    sides = {}
    for key, path in (("a", args.baseline), ("b", args.candidate)):
        capture = Capture.load(path, max_samples=max_samples)
        stats = measure_window(capture, assume_voltage_mv=args.assume_voltage_mv)
        sides[key] = (capture, stats)
        # A floor-served quantile, a clipped maximum or an unenumerated gap
        # table makes an operand a bound rather than a measurement, and a
        # difference of two bounds is not a measurement either. `measure`
        # surfaces these for exactly that reason; dropping them here let
        # compare report a delta of 0.0 between two medians that differ 3x.
        # Re-wrapped with the path because two sides at the floor would
        # otherwise emit the same sentence twice with nothing to tell them
        # apart.
        warnings.extend(warn(d.code, f"{path}: {d.message}") for d in stats.diagnostics())
        if capture.gaps:
            warnings.append(
                warn(
                    W_SAMPLE_GAPS,
                    f"{path} records {len(capture.gaps)} sample gap(s); its statistics "
                    "describe the samples that survived",
                )
            )
        if capture.interruption:
            warnings.append(warn(W_INTERRUPTED, f"{path} was interrupted before it finished"))

    (cap_a, stats_a), (cap_b, stats_b) = sides["a"], sides["b"]
    # The gain-error cancellation is a claim about one physical shunt, so it
    # needs one physical instrument. Unknown is not the same as different: an
    # unidentified pair keeps the tighter bar but says the premise is
    # unverified, rather than being priced as two independent units.
    serial_a = (cap_a.meta.device or {}).get("serial_number")
    serial_b = (cap_b.meta.device or {}).get("serial_number")
    same_instrument = serial_a == serial_b if serial_a and serial_b else None
    if same_instrument is False:
        warnings.append(
            warn(
                W_INSTRUMENT_MISMATCH,
                f"the two captures came from different instruments ({serial_a} and "
                f"{serial_b}); their gain errors are independent, so the delta gets no "
                "cancellation and the bar is no tighter than the two absolute figures",
            )
        )
    comparison = compare_stats(
        stats_a, stats_b, metric=args.metric, same_instrument=same_instrument
    )
    voltage = comparison.get("voltage")
    if voltage and voltage["differs"]:
        warnings.append(
            warn(
                W_VOLTAGE_ASSUMED,
                f"energy was computed at {voltage['a']} mV on one side and "
                f"{voltage['b']} mV on the other ({voltage['basis']['a']} / "
                f"{voltage['basis']['b']}), so the delta includes the supply change and "
                "not only the DUT's; pass --assume-voltage-mv to price both sides alike",
            )
        )
    if voltage and comparison["delta"] is None:
        notes = " / ".join(str(n) for n in (voltage["note"]["a"], voltage["note"]["b"]) if n)
        if notes:
            warnings.append(warn(W_VOLTAGE_ASSUMED, f"energy is not available here: {notes}"))
    result: dict[str, Any] = {
        "a": {"path": str(args.baseline), **summarize_side(cap_a, stats_a)},
        "b": {"path": str(args.candidate), **summarize_side(cap_b, stats_b)},
        **comparison,
    }

    unit = comparison["unit"]
    delta = comparison["delta"]
    lines = [f"compare {args.metric} (candidate - baseline)"]
    if delta is None:
        lines.append("  delta: not computable from these captures")
    else:
        bar = (comparison["uncertainty"] or {}).get("delta_typical")
        lines.append(f"  delta: {format_with_uncertainty(delta, bar)} {unit}")
        if comparison["relative"] is not None:
            lines.append(f"  relative: {comparison['relative'] * 100:+.2f}%")
    lines.append(f"  basis: {comparison['basis']}")
    if comparison["uncertainty"]:
        lines.append(f"  {comparison['uncertainty']['note']}")
        # Mirrors _fmt_stats. delta_typical is a systematic gain specification;
        # the stderr says how settled these two particular means are, and they
        # can disagree by an order of magnitude. Printing only the first made a
        # delta indistinguishable from zero read as a confident result.
        stderr = comparison["uncertainty"].get("delta_batch_stderr")
        note = "typical, per-range; not guaranteed"
        if stderr is not None:
            note += f"; batch stderr of the delta {format_si(stderr, 'A')}"
        lines.append(f"  uncertainty: {note}")
    elif comparison.get("uncertainty_note"):
        lines.append(f"  {comparison['uncertainty_note']}")
    return Outcome(result=result, warnings=warnings, human="\n".join(lines))


def cmd_export(args: Any) -> Outcome:
    warnings: list[Diagnostic | str] = []
    window = None
    if args.window:
        start_s, end_s = _parse_window(args.window)
        capture = _load_window(args, start_s, end_s)
        window = {
            "start_s": start_s,
            "end_s": end_s,
            "start_index": capture.start_index,
            "end_index": capture.end_index,
        }
        warnings.append(
            warn(
                W_PARTIAL_INTEGRITY,
                "a windowed export reads only the chunks the window falls in, so only "
                "those had their CRC32 checked; capture_sha256 is null because the "
                "manifest's whole-file digest was not verified",
            )
        )
    else:
        capture = _load_capture(args)

    bucket_samples: int | None = None
    if args.decimate is not None:
        if args.decimate < 1:
            raise UsageError(f"--decimate must be at least 1 sample, got {args.decimate}")
        bucket_samples = args.decimate
    elif args.bucket_ms is not None:
        bucket_samples = bucket_samples_for_ms(
            args.bucket_ms, sample_rate_hz=capture.sample_rate_hz
        )
    comments = list(getattr(args, "comment", None) or [])
    if comments and args.export_format != "csv":
        # Dropping them silently would produce a file the caller believes is
        # annotated. VCD has its own comment syntax and JSONL has no comment
        # line at all; neither is a `# ` prefix.
        raise UsageError(
            f"--comment is CSV only; {args.export_format} has no comment line",
            remediation="Export CSV, or record the provenance beside the file.",
        )
    if bucket_samples is not None and args.export_format == "vcd":
        raise UsageError(
            "--decimate/--bucket-ms summarize the current series; VCD carries only the "
            "digital lines, where a bucket has no meaning",
            remediation="Export VCD without a bucket width, or use --format csv/jsonl.",
        )

    if args.export_format == "vcd":
        import os

        if os.path.exists(args.output) and not args.overwrite:
            raise OutputExistsError(f"output file exists: {args.output}")
        channels = parse_channel_set(args.channels)
        records = export_vcd(capture.iter_events(), args.output, channels)
    elif bucket_samples is not None:
        if args.export_format == "csv":
            records = export_decimated_csv(
                capture,
                args.output,
                bucket_samples=bucket_samples,
                overwrite=args.overwrite,
                comments=comments,
            )
        else:
            records = export_decimated_jsonl(
                capture, args.output, bucket_samples=bucket_samples, overwrite=args.overwrite
            )
        warnings.append(
            warn(
                W_DECIMATED,
                f"decimated view: {records} bucket(s) of {bucket_samples} samples. This is "
                "a derived summary with its own record shape, not the raw series; the "
                "artifact remains the evidence (docs/decimation.md)",
            )
        )
    elif args.export_format == "csv":
        records = export_csv(
            capture,
            args.output,
            include_filtered=args.filtered,
            overwrite=args.overwrite,
            comments=comments,
        )
    else:
        records = export_samples_jsonl(capture, args.output, overwrite=args.overwrite)

    size = estimate_export_size(
        capture,
        export_format=args.export_format,
        include_filtered=args.filtered,
        bucket_samples=bucket_samples,
    )
    result = {
        "format": args.export_format,
        "output": args.output,
        "records": records,
        "capture_id": capture.meta.capture_id,
        # The window read only some chunks, so the artifact's own digest was
        # never checked; reporting it would assert a check that did not run.
        "capture_sha256": capture.artifact_sha256,
        "window": window,
        "decimation": (
            None
            if bucket_samples is None
            else {
                "bucket_samples": bucket_samples,
                "bucket_ms": args.bucket_ms,
                "buckets": records,
            }
        ),
        "estimated_bytes": size.get("estimated_bytes"),
        "bytes_per_record": size.get("bytes_per_record"),
        # How the estimate was arrived at, including the cases where it could
        # not be: a VCD's record count is a property of the DUT's firmware.
        "size_basis": size.get("basis"),
    }
    return Outcome(result, warnings, f"{records} records written to {args.output}")
