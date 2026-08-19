"""CLI entry point, argument parsing, JSON envelope, and error mapping.

Contract (frozen with the schemas):

- every command supports ``--json`` and emits the ``envelope`` schema;
- errors carry stable codes and remediation, and map to the documented exit
  codes (`ppk2lab.errors`);
- read-only commands never change hardware state; ``configure`` is dry-run
  unless ``--apply`` is passed; ``capture`` never enables DUT power.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from collections.abc import Sequence
from typing import Any, NoReturn

from .._version import SCHEMA_VERSION, __version__
from ..diagnostics import Diagnostic, as_json
from ..errors import EXIT_OK, Ppk2labError, UsageError

#: Category per command, surfaced in capabilities and docs. "measurement"
#: starts/stops the sample stream but never touches DUT power or voltage.
COMMAND_CATEGORIES: dict[str, str] = {
    "discover": "read-only",
    "info": "read-only",
    "capabilities": "read-only",
    "schema": "read-only",
    "doctor": "read-only (stream check is opt-in measurement)",
    "configure": "state-changing (dry-run by default; requires --apply)",
    "capture": "measurement (never enables DUT power)",
    "inspect": "offline",
    "decode": "offline",
    "measure": "offline",
    "assert": "offline",
    "export": "offline",
}

#: One sentence per command, used as both argparse ``help`` (the subcommand
#: list) and ``description`` (the per-command page). It is the same string in
#: both places deliberately: ``capabilities --json`` publishes ``description``,
#: and an empty description is what an agent reading the surface sees instead
#: of what the command does.
COMMAND_SUMMARIES: dict[str, str] = {
    "discover": "list connected PPK2 devices (read-only)",
    "info": "device metadata, calibration, firmware fingerprint, and state (read-only)",
    "capabilities": "machine-readable command/decoder surface (read-only)",
    "schema": "print a JSON schema (read-only)",
    "doctor": (
        "diagnose environment and devices; exits nonzero on a failing check (read-only by default)"
    ),
    "configure": "set mode / source voltage / DUT power (dry-run unless --apply)",
    "capture": "record current + D0-D7 to a .ppk2a artifact (never enables DUT power)",
    "inspect": "read a capture's manifest without loading its samples (offline)",
    "decode": "decode UART/SPI from a capture (offline)",
    "measure": "current/charge/energy for windows or events (offline)",
    "assert": "evaluate power/protocol assertions against a capture (offline)",
    "export": "derived views: CSV, VCD, JSONL (offline)",
}

STATE_CHANGING_COMMANDS = frozenset({"configure"})


class CliParser(argparse.ArgumentParser):
    """argparse that raises structured errors instead of exiting."""

    def error(self, message: str) -> NoReturn:
        raise UsageError(message)


def _add_device_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--device", metavar="SERIAL", help="device serial number")
    parser.add_argument("--port", metavar="PATH", help="explicit measurement port path")


def _add_max_samples_option(parser: argparse.ArgumentParser) -> None:
    """Ceiling on how much of a capture an offline command materializes.

    Loading costs roughly 8 bytes of RAM per stored sample, so an unbounded
    default would turn a long soak artifact into an OOM kill instead of an
    error message. ``none`` removes the ceiling for a machine that has the
    memory; ``ppk2lab inspect`` reads the manifest without any of this.
    """
    parser.add_argument(
        "--max-samples",
        metavar="N|none",
        help="refuse to load more than N samples (default ~25 M, about 200 MB of RAM); "
        "'none' removes the ceiling",
    )


def _add_spi_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--spi-sclk", metavar="Dn", help="SPI clock channel")
    parser.add_argument("--spi-mosi", metavar="Dn", help="SPI MOSI channel")
    parser.add_argument("--spi-miso", metavar="Dn", help="SPI MISO channel")
    parser.add_argument("--spi-cs", metavar="Dn", help="SPI chip-select channel")
    parser.add_argument(
        "--spi-mode",
        type=int,
        default=0,
        choices=(0, 1, 2, 3),
        help="SPI mode (CPOL/CPHA), default 0",
    )
    parser.add_argument(
        "--spi-word-bits",
        type=int,
        default=8,
        metavar="N",
        help="SPI word size in bits (4-32, default 8)",
    )
    parser.add_argument(
        "--spi-lsb-first", action="store_true", help="decode words LSB-first (default MSB-first)"
    )
    parser.add_argument(
        "--spi-cs-active-high",
        action="store_true",
        help="treat CS as active-high (default active-low)",
    )
    parser.add_argument(
        "--spi-clock-hz",
        type=float,
        metavar="HZ",
        help="expected SPI clock for rate-tier validation",
    )


def build_parser() -> CliParser:
    parser = CliParser(
        prog="ppk2lab",
        description="Control, capture, decode, and test Nordic PPK2 hardware.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"ppk2lab {__version__}",
        help="print the ppk2lab version and exit",
    )
    parser.add_argument("--json", action="store_true", help="emit the JSON envelope contract")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="use a simulated PPK2 (testing the toolchain only; not a measurement)",
    )
    # The global flags are also accepted after the subcommand; SUPPRESS keeps
    # an absent subcommand-level flag from overriding the global one.
    common = CliParser(add_help=False)
    common.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )
    common.add_argument(
        "--simulate", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", parser_class=CliParser)

    def add_command(name: str, **kwargs: Any) -> argparse.ArgumentParser:
        summary = COMMAND_SUMMARIES[name]
        return sub.add_parser(name, parents=[common], help=summary, description=summary, **kwargs)

    p = add_command("discover")

    p = add_command("info")
    _add_device_options(p)

    add_command("capabilities")

    p = add_command("schema")
    p.add_argument("name", nargs="?", help="schema name; omit with --list")
    p.add_argument("--list", action="store_true", help="list available schemas")

    p = add_command("doctor")
    _add_device_options(p)
    p.add_argument(
        "--stream-check",
        metavar="DURATION",
        help="opt-in: run a short measurement (e.g. 1s) to verify stream rate and gaps; "
        "starts/stops measuring but never touches DUT power",
    )

    p = add_command("configure")
    _add_device_options(p)
    p.add_argument("--mode", choices=("ampere", "source"), help="measurement mode")
    p.add_argument(
        "--voltage-mv", type=int, metavar="MV", help="source voltage in millivolts (800-5000)"
    )
    p.add_argument("--dut-power", choices=("on", "off"), help="DUT power output")
    p.add_argument(
        "--max-voltage-mv",
        type=int,
        metavar="MV",
        help="refuse any source voltage above this ceiling (DUT protection); also "
        "settable via PPK2LAB_MAX_VOLTAGE_MV",
    )
    p.add_argument(
        "--apply", action="store_true", help="actually apply the changes (default is a dry run)"
    )

    p = add_command("capture")
    _add_device_options(p)
    p.add_argument("--duration", metavar="DUR", help="capture length, e.g. 5s, 500ms")
    p.add_argument("--samples", type=int, metavar="N", help="capture length in samples")
    p.add_argument(
        "--digital",
        default="D0-D7",
        metavar="CHANNELS",
        help="digital channels of interest (informational; all 8 are stored)",
    )
    p.add_argument("--output", metavar="FILE.ppk2a", help="write the capture artifact")
    p.add_argument("--overwrite", action="store_true", help="allow replacing the output file")
    p.add_argument(
        "--trigger",
        metavar="SPEC",
        help="software trigger, e.g. 'current>10mA', 'digital D3 rising', "
        "'uart D0 9600 \"BOOT\"', 'spi 0x9f'",
    )
    p.add_argument("--pre", metavar="DUR", help="pre-trigger window (default 0)")
    p.add_argument("--post", metavar="DUR", help="post-trigger window (default 1s)")
    p.add_argument(
        "--trigger-timeout",
        metavar="DUR",
        help="abort if the trigger has not fired after this long",
    )
    p.add_argument(
        "--trigger-hold",
        type=int,
        default=1,
        metavar="N",
        help="samples the trigger condition must hold (default 1)",
    )
    p.add_argument(
        "--allow-experimental",
        action="store_true",
        help="allow experimental-tier protocol rates in triggers",
    )
    p.add_argument(
        "--assume-voltage-mv",
        type=int,
        metavar="MV",
        help="DUT supply voltage to use for energy when the meter cannot know it "
        "(ampere mode); recorded as an explicit assumption",
    )
    p.add_argument(
        "--in-memory",
        action="store_true",
        help="allow buffering a long capture in RAM instead of requiring --output",
    )
    _add_spi_options(p)

    p = add_command("inspect")
    p.add_argument("capture", help="capture artifact (.ppk2a)")

    p = add_command("decode")
    p.add_argument("capture", help="capture artifact (.ppk2a)")
    _add_max_samples_option(p)
    p.add_argument("--uart", metavar="Dn", help="decode UART on this channel")
    p.add_argument("--baud", type=int, default=9600, help="UART baud rate (default 9600)")
    p.add_argument("--data-bits", type=int, default=8, help="UART data bits 5-9 (default 8)")
    p.add_argument(
        "--parity",
        choices=("none", "even", "odd"),
        default="none",
        help="UART parity (default none)",
    )
    p.add_argument(
        "--stop-bits", type=int, default=1, choices=(1, 2), help="UART stop bits (default 1)"
    )
    p.add_argument("--invert", action="store_true", help="UART signal is inverted")
    p.add_argument("--msb-first", action="store_true", help="UART data bits MSB-first")
    _add_spi_options(p)
    p.add_argument("--output", metavar="FILE.jsonl", help="write annotations as JSON Lines")
    p.add_argument("--overwrite", action="store_true", help="allow replacing the output file")
    p.add_argument(
        "--allow-experimental",
        action="store_true",
        help="allow experimental-tier protocol rates (low confidence)",
    )

    p = add_command("measure")
    p.add_argument("capture", help="capture artifact (.ppk2a)")
    _add_max_samples_option(p)
    p.add_argument("--window", metavar="START:END", help="time window in seconds, e.g. 0.1:0.25")
    p.add_argument("--annotations", metavar="FILE.jsonl", help="measure decoded events")
    p.add_argument(
        "--group-by",
        choices=("annotation", "kind", "frame", "transaction"),
        default="annotation",
        help="one result per annotation, or aggregated per kind (default annotation)",
    )
    p.add_argument(
        "--filtered",
        action="store_true",
        help="also apply range-switch spike filtering (raw stats are default)",
    )
    p.add_argument(
        "--state-threshold",
        metavar="CURRENT",
        help="split the window at this current (e.g. 1mA) and report time, mean, "
        "charge, and run count for each side; the threshold is echoed in the result",
    )
    p.add_argument(
        "--assume-voltage-mv",
        type=int,
        metavar="MV",
        help="DUT supply voltage for energy when the capture cannot know it (ampere mode)",
    )

    p = add_command("assert")
    p.add_argument("capture", help="capture artifact (.ppk2a)")
    _add_max_samples_option(p)
    p.add_argument(
        "--rule",
        action="append",
        default=[],
        metavar="RULE",
        help='assertion DSL, e.g. \'after uart("TX_DONE"), within 20ms, '
        "avg_current < 10uA' (repeatable)",
    )
    p.add_argument(
        "--rules-file", metavar="FILE.json", help="JSON array of rules (objects or DSL strings)"
    )
    p.add_argument(
        "--assume-voltage-mv",
        type=int,
        metavar="MV",
        help="DUT supply voltage for energy when the capture cannot know it (ampere mode)",
    )
    p.add_argument(
        "--uart-rx", default="D0", metavar="Dn", help="UART channel for uart() events (default D0)"
    )
    p.add_argument("--baud", type=int, default=9600, help="UART baud for uart() events")
    _add_spi_options(p)
    p.add_argument(
        "--format",
        choices=("json", "junit"),
        default="json",
        dest="report_format",
        help="report format (default json)",
    )
    p.add_argument("--output", metavar="FILE", help="write the report to a file")
    p.add_argument(
        "--allow-experimental",
        action="store_true",
        help="allow experimental-tier protocol rates in uart()/spi() events",
    )

    p = add_command("export")
    p.add_argument("capture", help="capture artifact (.ppk2a)")
    _add_max_samples_option(p)
    p.add_argument(
        "--format",
        required=True,
        choices=("csv", "vcd", "jsonl"),
        dest="export_format",
        help="export format",
    )
    p.add_argument("--output", required=True, metavar="FILE", help="destination file")
    p.add_argument("--overwrite", action="store_true", help="allow replacing the output file")
    p.add_argument(
        "--filtered", action="store_true", help="CSV: add the spike-filtered current column"
    )
    p.add_argument(
        "--channels",
        default="D0-D7",
        metavar="CHANNELS",
        help="VCD: channels to export (default D0-D7)",
    )
    p.add_argument(
        "--window",
        metavar="START:END",
        help="export only this time window in seconds, e.g. 0.1:0.25 (either side "
        "may be empty); only the chunks it falls in are read",
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument(
        "--decimate",
        type=int,
        metavar="N",
        help="CSV/JSONL: summarize into buckets of N samples instead of one record "
        "per sample. Opt-in; the output has a different header (docs/decimation.md)",
    )
    group.add_argument(
        "--bucket-ms",
        type=float,
        metavar="MS",
        help="CSV/JSONL: same as --decimate, with the bucket width given in milliseconds",
    )

    return parser


def _envelope(
    command: str,
    *,
    ok: bool,
    result: dict[str, Any] | None,
    warnings: Sequence[Diagnostic | str],
    error: dict[str, Any] | None,
) -> str:
    return json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "command": command,
            "ok": ok,
            "result": result,
            "warnings": as_json(warnings),
            "error": error,
        },
        indent=2,
        sort_keys=True,
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    json_mode = "--json" in argv
    parser = build_parser()
    command = "ppk2lab"
    try:
        args = parser.parse_args(argv)
        if not args.command:
            parser.print_help()
            return EXIT_OK
        command = args.command
        if os.environ.get("PPK2LAB_SIMULATE") == "1":
            args.simulate = True
        json_mode = args.json

        from . import commands as impl

        handlers = {
            "discover": impl.cmd_discover,
            "info": impl.cmd_info,
            "capabilities": impl.cmd_capabilities,
            "schema": impl.cmd_schema,
            "doctor": impl.cmd_doctor,
            "configure": impl.cmd_configure,
            "capture": impl.cmd_capture,
            "inspect": impl.cmd_inspect,
            "decode": impl.cmd_decode,
            "measure": impl.cmd_measure,
            "assert": impl.cmd_assert,
            "export": impl.cmd_export,
        }
        outcome = handlers[command](args)
        if outcome.raw_output is not None:
            print(outcome.raw_output, end="")
            return outcome.exit_code
        if json_mode:
            print(
                _envelope(
                    command,
                    ok=outcome.error is None,
                    result=outcome.result,
                    warnings=outcome.warnings,
                    error=outcome.error,
                )
            )
        else:
            if outcome.human:
                print(outcome.human)
            for warning in as_json(outcome.warnings):
                print(f"warning [{warning['code']}]: {warning['message']}", file=sys.stderr)
            if outcome.error is not None:
                print(
                    f"error [{outcome.error['code']}]: {outcome.error['message']}\n"
                    f"  remediation: {outcome.error['remediation']}",
                    file=sys.stderr,
                )
        return outcome.exit_code
    except Ppk2labError as exc:
        if json_mode:
            print(_envelope(command, ok=False, result=None, warnings=[], error=exc.to_json()))
        else:
            print(
                f"error [{exc.code}]: {exc.message}\n  remediation: {exc.remediation}",
                file=sys.stderr,
            )
        return exc.exit_code
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        if os.environ.get("PPK2LAB_DEBUG"):
            traceback.print_exc()
        wrapped = Ppk2labError(f"internal error: {exc}")
        if json_mode:
            print(_envelope(command, ok=False, result=None, warnings=[], error=wrapped.to_json()))
        else:
            print(
                f"error [{wrapped.code}]: {wrapped.message}\n  remediation: {wrapped.remediation}",
                file=sys.stderr,
            )
        return wrapped.exit_code


def entrypoint() -> None:
    sys.exit(main())


if __name__ == "__main__":
    entrypoint()
