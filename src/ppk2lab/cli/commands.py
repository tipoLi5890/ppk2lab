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
from ..analysis.measure import (
    load_annotations_jsonl,
    measure_annotations,
    measure_window,
    save_annotations_jsonl,
)
from ..capture.model import Capture
from ..capture.runner import DEFAULT_IN_MEMORY_LIMIT_SAMPLES
from ..decoders.base import decode_capture
from ..decoders.registry import decoder_capabilities
from ..decoders.spi import SPIDecoder
from ..decoders.uart import UARTDecoder
from ..device import PPK2
from ..diagnostics import (
    W_CALIBRATION_INCOMPLETE,
    W_DECODER_RATE,
    W_DEVICE_NOT_FOUND,
    W_DRY_RUN,
    W_GENERIC,
    W_METADATA,
    W_NOT_CALIBRATED,
    W_SAMPLE_GAPS,
    W_SESSION_RECOVERED,
    W_STATE_UNVERIFIED,
    Diagnostic,
    warn,
    warning_catalog,
)
from ..discovery import discover
from ..errors import (
    EXIT_ASSERTION_FAILED,
    EXIT_CAPTURE_INCOMPLETE,
    CaptureIncompleteError,
    OutputExistsError,
    UsageError,
    error_catalog,
)
from ..exports import export_csv, export_samples_jsonl, export_vcd
from ..schemas import get_schema, list_schemas
from ..triggers.engine import TriggerEngine, parse_trigger_spec
from ..types import (
    DIGITAL_CHANNELS,
    SAMPLE_RATE_HZ,
    VOLTAGE_MAX_MV,
    VOLTAGE_MIN_MV,
    Mode,
)
from ..units import format_si, parse_channel_set, parse_duration_s
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


def _load_capture(path: str) -> Capture:
    return Capture.load(path)


def _fmt_stats(stats: dict[str, Any]) -> str:
    current = stats["current_ua"]
    lines = [
        f"  duration: {stats['duration_s']:.6g} s "
        f"({stats['samples']['stored']} samples stored, "
        f"{stats['samples']['missing_known']} missing)",
    ]
    if current["mean"] is not None:
        lines.append(
            f"  current: mean {format_si(current['mean'], 'A')}, "
            f"min {format_si(current['min'], 'A')}, max {format_si(current['max'], 'A')}"
        )
    if stats.get("charge_uc") is not None:
        lines.append(f"  charge: {format_si(stats['charge_uc'], 'C')}")
    if stats.get("energy_uj") is not None:
        lines.append(f"  energy: {format_si(stats['energy_uj'], 'J')}")
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
        result = {
            "device": device.info.to_json(),
            "state": device.state.to_json(),
            "metadata": metadata.to_json(),
            "calibration_missing_ranges": missing,
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
        human = "\n".join(
            [
                f"device: {device.info.serial_number}"
                + (" (simulated)" if device.info.simulated else ""),
                f"mode: {device.state.to_json()['mode']}",
                f"source_voltage_mv: {device.state.source_voltage_mv}",
                f"calibrated: {metadata.calibrated}",
                f"metadata terminated: {metadata.terminated}",
            ]
        )
        return Outcome(result, warnings, human)
    finally:
        device.close()


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
                if sub_action.dest in ("help",):
                    continue
                options.append(
                    {
                        "flags": list(sub_action.option_strings) or [sub_action.dest],
                        "help": sub_action.help or "",
                        "required": bool(getattr(sub_action, "required", False)),
                    }
                )
            commands.append(
                {
                    "name": name,
                    "state_changing": name in STATE_CHANGING_COMMANDS,
                    "category": COMMAND_CATEGORIES.get(name, "unknown"),
                    "description": subparser.description or "",
                    "options": options,
                }
            )
    result = {
        "device": {
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "digital_channels": list(DIGITAL_CHANNELS),
            "modes": [m.name.lower() for m in Mode],
            "source_voltage_mv": {"min": VOLTAGE_MIN_MV, "max": VOLTAGE_MAX_MV},
        },
        "commands": commands,
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


def cmd_doctor(args: Any) -> Outcome:
    checks: list[dict[str, Any]] = []

    def check(name: str, status: str, detail: str, remediation: str | None = None) -> None:
        checks.append(
            {"name": name, "status": status, "detail": detail, "remediation": remediation}
        )

    py = sys.version_info
    check(
        "python_version",
        "pass" if py >= (3, 11) else "fail",
        f"{py.major}.{py.minor}.{py.micro} on {platform.system()}",
        None if py >= (3, 11) else "ppk2lab requires Python 3.11 or newer",
    )
    check("ppk2lab_version", "pass", __version__)
    try:
        import serial

        check("pyserial", "pass", f"pyserial {serial.__version__}")
    except ImportError:
        check(
            "pyserial", "fail", "pyserial is not importable", "reinstall with `pip install ppk2lab`"
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
        )
        devices, target = [], None

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
                assert metadata is not None
                check(
                    "metadata_read",
                    "pass" if metadata.terminated else "warn",
                    "terminated by END" if metadata.terminated else "reply not terminated by END",
                    None if metadata.terminated else "retry; report if it persists",
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
            check(
                "port_open",
                "fail",
                str(exc),
                "run `ppk2lab discover --json`; close other apps using the port",
            )

    summary = {
        status: sum(1 for c in checks if c["status"] == status)
        for status in ("pass", "fail", "warn", "skip")
    }
    icons = {"pass": "ok  ", "fail": "FAIL", "warn": "warn", "skip": "skip"}
    lines = [f"[{icons[c['status']]}] {c['name']}: {c['detail']}" for c in checks]
    lines.append(
        f"{summary['pass']} pass, {summary['fail']} fail, {summary['warn']} warn, "
        f"{summary['skip']} skip"
    )
    return Outcome({"checks": checks, "summary": summary}, [], "\n".join(lines))


# ---------------------------------------------------------------------------
# state-changing / measurement commands


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
        if dry_run:
            warnings.append(
                warn(
                    W_DRY_RUN,
                    "dry run: no hardware state was changed; re-run with --apply to apply",
                )
            )
        for change in changes:
            for text in change.warnings:
                # Only a genuine readback failure earns the code an agent
                # branches on; explanatory notes are not state uncertainty.
                unverified = (
                    change.applied
                    and not change.observed_after
                    and ("read back" in text or "readback" in text)
                )
                warnings.append(warn(W_STATE_UNVERIFIED if unverified else W_GENERIC, text))
        result = {
            "dry_run": dry_run,
            "changes": [c.to_json() for c in changes],
            "state": device.state.to_json(),
        }
        lines = []
        for change in changes:
            verb = "would apply" if dry_run else "applied"
            lines.append(f"{verb} {change.operation}: {change.requested}")
        lines.append(f"state: {device.state.to_json()}")
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


def cmd_capture(args: Any) -> Outcome:
    parse_channel_set(args.digital)  # validate the channel spec early
    duration_s = parse_duration_s(args.duration) if args.duration else None
    if duration_s is None and args.samples is None and args.trigger is None:
        raise UsageError("capture needs --duration, --samples, or --trigger")

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
    capture = _load_capture(args.capture)
    decoder = _build_decoder(args)
    feasibility = getattr(decoder, "feasibility", None)
    warnings: list[Diagnostic | str] = (
        [warn(W_DECODER_RATE, text) for text in feasibility.warnings] if feasibility else []
    )
    if not capture.complete:
        warnings.append(
            warn(
                W_SAMPLE_GAPS,
                "capture is incomplete; events touching missing data carry gap errors and "
                "zero confidence",
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
    capture = _load_capture(args.capture)
    result: dict[str, Any] = {
        "capture_id": capture.meta.capture_id,
        "capture_sha256": capture.sha256(),
        "window": None,
        "annotations": None,
        "groups": None,
    }
    warnings: list[Diagnostic | str] = []
    if not capture.complete:
        warnings.append(
            warn(W_SAMPLE_GAPS, "capture is incomplete; windows overlapping gaps are marked")
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
    elif args.window:
        try:
            start_text, end_text = args.window.split(":", 1)
            start_s, end_s = float(start_text), float(end_text)
        except ValueError as exc:
            raise UsageError(
                f"invalid --window {args.window!r}; expected START:END seconds"
            ) from exc
        stats = measure_window(
            capture,
            start_s=start_s,
            end_s=end_s,
            filtered=args.filtered,
            assume_voltage_mv=args.assume_voltage_mv,
        )
        result["window"] = stats.to_json()
        human_parts.append(_fmt_stats(result["window"]))
    else:
        stats = measure_window(
            capture, filtered=args.filtered, assume_voltage_mv=args.assume_voltage_mv
        )
        result["window"] = stats.to_json()
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
    capture = _load_capture(args.capture)
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


def cmd_export(args: Any) -> Outcome:
    capture = _load_capture(args.capture)
    warnings: list[Diagnostic | str] = []
    if args.export_format == "csv":
        records = export_csv(
            capture, args.output, include_filtered=args.filtered, overwrite=args.overwrite
        )
    elif args.export_format == "vcd":
        import os

        if os.path.exists(args.output) and not args.overwrite:
            raise OutputExistsError(f"output file exists: {args.output}")
        channels = parse_channel_set(args.channels)
        records = export_vcd(capture.iter_events(), args.output, channels)
    else:
        records = export_samples_jsonl(capture, args.output, overwrite=args.overwrite)
    result = {
        "format": args.export_format,
        "output": args.output,
        "records": records,
        "capture_id": capture.meta.capture_id,
        "capture_sha256": capture.sha256(),
    }
    return Outcome(result, warnings, f"{records} records written to {args.output}")
