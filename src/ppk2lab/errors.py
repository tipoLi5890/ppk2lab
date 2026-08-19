"""Stable error codes, exit codes, and remediation guidance.

Every error that can cross the CLI/JSON boundary carries:

- ``code``: a stable machine-readable string (frozen per schema version),
- ``remediation``: a short machine-readable hint on how to recover,
- ``exit_code``: the CLI process exit code.

Exit code contract (frozen before 0.1.0):

==== =======================================
0    success
1    assertion failed
2    usage / schema / invalid input
3    device not found
4    permission denied or port busy
5    protocol / firmware / metadata mismatch
6    capture incomplete (data loss)
7    optional capability missing
8    unsafe state change refused
9    internal error
==== =======================================
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_ASSERTION_FAILED = 1
EXIT_USAGE = 2
EXIT_DEVICE_NOT_FOUND = 3
EXIT_PERMISSION = 4
EXIT_PROTOCOL = 5
EXIT_CAPTURE_INCOMPLETE = 6
EXIT_CAPABILITY_MISSING = 7
EXIT_UNSAFE_REFUSED = 8
EXIT_INTERNAL = 9


class Ppk2labError(Exception):
    """Base class for all ppk2lab errors with a stable code and remediation."""

    code = "INTERNAL_ERROR"
    exit_code = EXIT_INTERNAL
    default_remediation = (
        "This is an unexpected internal error; report it with the full output at "
        "https://github.com/tipoLi5890/ppk2lab/issues"
    )

    def __init__(self, message: str, *, remediation: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.remediation = remediation or self.default_remediation

    def to_json(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "remediation": self.remediation,
            "exit_code": self.exit_code,
        }


class UsageError(Ppk2labError):
    code = "INVALID_ARGUMENT"
    exit_code = EXIT_USAGE
    default_remediation = "Check the command arguments against `ppk2lab <command> --help`."


class SchemaNotFoundError(UsageError):
    code = "SCHEMA_NOT_FOUND"
    default_remediation = "List available schemas with `ppk2lab schema --list`."


class OutputExistsError(UsageError):
    code = "OUTPUT_EXISTS"
    default_remediation = "Choose a different output path or pass --overwrite explicitly."


class CaptureFileError(UsageError):
    code = "CAPTURE_FILE_INVALID"
    default_remediation = (
        "The capture file is missing, truncated, or not a ppk2lab capture artifact. "
        "Re-run the capture; do not repair artifacts by hand."
    )


class DeviceNotFoundError(Ppk2labError):
    code = "DEVICE_NOT_FOUND"
    exit_code = EXIT_DEVICE_NOT_FOUND
    default_remediation = (
        "Check the USB connection, then run `ppk2lab discover --json`. If the device is "
        "listed there, pass its serial number with --device."
    )


class PortBusyError(Ppk2labError):
    code = "PORT_BUSY"
    exit_code = EXIT_PERMISSION
    default_remediation = (
        "Another process (often the official Power Profiler app) holds the serial port. "
        "Close it and retry, or run `ppk2lab doctor --json`."
    )


class PermissionDeniedError(Ppk2labError):
    code = "PERMISSION_DENIED"
    exit_code = EXIT_PERMISSION
    default_remediation = (
        "The OS refused access to the serial port. On Linux add your user to the "
        "dialout/uucp group or install udev rules; see INSTALL.md."
    )


class ProtocolError(Ppk2labError):
    code = "PROTOCOL_ERROR"
    exit_code = EXIT_PROTOCOL
    default_remediation = (
        "The device response did not match the documented PPK2 protocol. Verify the "
        "firmware version with `ppk2lab info` and report incompatibilities."
    )


class MetadataError(ProtocolError):
    code = "METADATA_INVALID"
    default_remediation = (
        "Device metadata could not be parsed. Retry once; if it persists, capture the raw "
        "metadata with `ppk2lab info --json` and file a compatibility report."
    )


class FirmwareUnsupportedError(ProtocolError):
    code = "FIRMWARE_UNSUPPORTED"
    default_remediation = (
        "This firmware version is not in the validated compatibility matrix. Update the "
        "PPK2 firmware with Nordic's official tools, or file a compatibility report."
    )


class CaptureIncompleteError(Ppk2labError):
    code = "CAPTURE_INCOMPLETE"
    exit_code = EXIT_CAPTURE_INCOMPLETE
    default_remediation = (
        "The capture contains sample gaps or was interrupted. The artifact preserves the "
        "loss markers; reduce system load or capture duration and retry if a complete "
        "capture is required."
    )


class CapabilityMissingError(Ppk2labError):
    code = "CAPABILITY_MISSING"
    exit_code = EXIT_CAPABILITY_MISSING
    default_remediation = (
        "An optional dependency or capability is not available. See `ppk2lab capabilities "
        "--json` for what is supported in this installation."
    )


class UnsafeOperationError(Ppk2labError):
    code = "UNSAFE_STATE_CHANGE_REFUSED"
    exit_code = EXIT_UNSAFE_REFUSED
    default_remediation = (
        "The requested hardware state change was refused by the safety contract. Re-run "
        "with explicit, in-range parameters and the --apply flag if you intend the change."
    )


class VoltageRangeError(UnsafeOperationError):
    code = "VOLTAGE_OUT_OF_RANGE"
    default_remediation = (
        "Source voltage must be given in millivolts via voltage_mv/--voltage-mv and lie "
        "within the device capability range (800-5000 mV)."
    )


class DecoderRateError(UsageError):
    code = "DECODER_RATE_UNSUPPORTED"
    default_remediation = (
        "The requested protocol rate cannot be decoded reliably from a 100 kS/s capture. "
        "See `ppk2lab capabilities --json` for supported tiers; pass --allow-experimental "
        "only if you accept unreliable results."
    )


class TransportError(Ppk2labError):
    code = "TRANSPORT_ERROR"
    exit_code = EXIT_PERMISSION
    default_remediation = (
        "The serial transport failed (USB disconnect or I/O error). Reconnect the device "
        "and run `ppk2lab doctor --json`."
    )


#: Registry of all stable error codes, used by `ppk2lab capabilities` so the
#: surface is generated from live definitions instead of a hand-written table.
ERROR_CLASSES: tuple[type[Ppk2labError], ...] = (
    Ppk2labError,
    UsageError,
    SchemaNotFoundError,
    OutputExistsError,
    CaptureFileError,
    DeviceNotFoundError,
    PortBusyError,
    PermissionDeniedError,
    ProtocolError,
    MetadataError,
    FirmwareUnsupportedError,
    CaptureIncompleteError,
    CapabilityMissingError,
    UnsafeOperationError,
    VoltageRangeError,
    DecoderRateError,
    TransportError,
)


def error_catalog() -> list[dict[str, object]]:
    """Machine-readable catalog of every stable error code."""
    return [
        {
            "code": cls.code,
            "exit_code": cls.exit_code,
            "remediation": cls.default_remediation,
        }
        for cls in ERROR_CLASSES
    ]
