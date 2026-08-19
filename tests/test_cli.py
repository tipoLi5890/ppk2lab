"""CLI contract tests: envelopes, exit codes, and the full simulated flow."""

import json

import jsonschema
import pytest

from ppk2lab.cli.main import main
from ppk2lab.schemas import get_schema


def run_json(capsys, *argv):
    code = main([*argv])
    out = capsys.readouterr().out
    payload = json.loads(out)
    jsonschema.validate(payload, get_schema("envelope"))
    return code, payload


def test_discover_simulated(capsys):
    code, payload = run_json(capsys, "--simulate", "--json", "discover")
    assert code == 0 and payload["ok"]
    jsonschema.validate(payload["result"], get_schema("discover-result"))
    assert payload["result"]["devices"][0]["serial_number"] == "SIM0001"
    assert payload["result"]["devices"][0]["simulated"] is True


def test_json_flag_after_subcommand(capsys):
    code, payload = run_json(capsys, "--simulate", "discover", "--json")
    assert code == 0 and payload["ok"]


def test_info_simulated(capsys):
    code, payload = run_json(capsys, "--simulate", "--json", "info")
    assert code == 0
    jsonschema.validate(payload["result"], get_schema("info-result"))
    assert payload["result"]["state"]["source_voltage_mv"] == 3000


def test_capabilities_lists_every_command(capsys):
    code, payload = run_json(capsys, "--json", "capabilities")
    assert code == 0
    jsonschema.validate(payload["result"], get_schema("capabilities-result"))
    names = {c["name"] for c in payload["result"]["commands"]}
    assert names == {
        "discover",
        "info",
        "capabilities",
        "schema",
        "doctor",
        "configure",
        "capture",
        "inspect",
        "decode",
        "measure",
        "assert",
        "export",
    }
    configure = next(c for c in payload["result"]["commands"] if c["name"] == "configure")
    assert configure["state_changing"] is True
    capture = next(c for c in payload["result"]["commands"] if c["name"] == "capture")
    assert capture["state_changing"] is False
    assert {d["name"] for d in payload["result"]["decoders"]} == {"uart", "spi"}


def test_schema_command(capsys):
    code = main(["schema", "envelope"])
    out = capsys.readouterr().out
    assert code == 0
    schema = json.loads(out)
    assert schema["$id"] == "ppk2lab:envelope"
    code = main(["schema", "nonexistent"])
    assert code == 2


def test_doctor_simulated(capsys):
    code, payload = run_json(capsys, "--simulate", "--json", "doctor")
    assert code == 0
    jsonschema.validate(payload["result"], get_schema("doctor-result"))
    statuses = {c["name"]: c["status"] for c in payload["result"]["checks"]}
    assert statuses["devices_found"] == "pass"
    assert statuses["stream_rate"] == "skip"  # opt-in only


def test_configure_dry_run_default(capsys):
    code, payload = run_json(capsys, "--simulate", "--json", "configure", "--voltage-mv", "3300")
    assert code == 0
    jsonschema.validate(payload["result"], get_schema("configure-result"))
    assert payload["result"]["dry_run"] is True
    assert payload["result"]["changes"][0]["applied"] is False
    assert any(w["code"] == "W_DRY_RUN" for w in payload["warnings"])


def test_configure_voltage_refused(capsys):
    code, payload = run_json(
        capsys, "--simulate", "--json", "configure", "--voltage-mv", "9000", "--apply"
    )
    assert code == 8
    assert payload["error"]["code"] == "VOLTAGE_OUT_OF_RANGE"
    assert payload["error"]["remediation"]


def test_device_not_found_exit_code(capsys, monkeypatch):
    import ppk2lab.device

    monkeypatch.setattr(ppk2lab.device, "discover", lambda: [])
    code, payload = run_json(capsys, "--json", "info")
    assert code == 3
    assert payload["error"]["code"] == "DEVICE_NOT_FOUND"


def test_usage_error_envelope(capsys):
    code, payload = run_json(capsys, "--json", "capture")  # no duration/samples/trigger
    assert code == 2
    assert payload["error"]["code"] == "INVALID_ARGUMENT"


@pytest.fixture
def flow(tmp_path, capsys):
    """A captured artifact + decoded annotations from the simulated device."""
    cap = tmp_path / "demo.ppk2a"
    ann = tmp_path / "uart.jsonl"
    code, payload = run_json(
        capsys,
        "--simulate",
        "--json",
        "capture",
        "--duration",
        "300ms",
        "--output",
        str(cap),
    )
    assert code == 0 and payload["result"]["complete"]
    code, _ = run_json(
        capsys,
        "--json",
        "decode",
        str(cap),
        "--uart",
        "D0",
        "--baud",
        "9600",
        "--output",
        str(ann),
    )
    assert code == 0
    return cap, ann


def test_full_flow_capture_decode_measure_assert(flow, tmp_path, capsys):
    cap, ann = flow

    code, payload = run_json(
        capsys, "--json", "measure", str(cap), "--annotations", str(ann), "--group-by", "kind"
    )
    assert code == 0
    jsonschema.validate(payload["result"], get_schema("measure-result"))
    assert payload["result"]["groups"][0]["kind"] == "frame"

    code, payload = run_json(
        capsys,
        "--json",
        "assert",
        str(cap),
        "--rule",
        'after uart("TX_DONE"), within 20ms, avg_current < 10uA',
    )
    assert code == 0
    jsonschema.validate(payload["result"], get_schema("assert-result"))
    assert payload["result"]["passed"] is True

    code, payload = run_json(
        capsys,
        "--json",
        "assert",
        str(cap),
        "--rule",
        'after uart("TX_DONE"), within 20ms, avg_current < 1uA',
    )
    assert code == 1
    assert payload["result"]["outcomes"][0]["status"] == "failed"


def test_capture_result_schema(flow, capsys, tmp_path):
    _cap, _ = flow
    code, payload = run_json(
        capsys,
        "--simulate",
        "--json",
        "capture",
        "--duration",
        "50ms",
    )
    assert code == 0
    jsonschema.validate(payload["result"], get_schema("capture-result"))


def test_export_formats(flow, tmp_path, capsys):
    cap, _ = flow
    for fmt, name in (("csv", "o.csv"), ("vcd", "o.vcd"), ("jsonl", "o.jsonl")):
        out = tmp_path / name
        code, payload = run_json(
            capsys, "--json", "export", str(cap), "--format", fmt, "--output", str(out)
        )
        assert code == 0
        jsonschema.validate(payload["result"], get_schema("export-result"))
        assert out.exists()
    # refusing to overwrite is part of the data-safety contract
    code, payload = run_json(
        capsys, "--json", "export", str(cap), "--format", "csv", "--output", str(tmp_path / "o.csv")
    )
    assert code == 2
    assert payload["error"]["code"] == "OUTPUT_EXISTS"


def test_capture_never_touches_dut_power(tmp_path):
    """The capture path must send no power/voltage/mode commands."""
    from ppk2lab.device import PPK2
    from ppk2lab.protocol.commands import Opcode
    from ppk2lab.transport.mock import MockTransport, SimulatedPPK2

    simulator = SimulatedPPK2()
    device = PPK2.open(transport=MockTransport(simulator), simulate=True)
    device.capture(duration_s=0.01)
    device.close()
    opcodes = {op for op, _ in simulator.command_log}
    assert Opcode.SET_DUT_POWER not in opcodes
    assert Opcode.SET_SOURCE_VOLTAGE not in opcodes
    assert Opcode.SET_MODE not in opcodes


def test_junit_report(flow, capsys):
    cap, _ = flow
    code = main(["assert", str(cap), "--rule", "max_current < 1A", "--format", "junit"])
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("<?xml")
    assert "testsuite" in out


# ---------------------------------------------------------------------------
# inspect: reading a capture without materializing it


def test_inspect_never_reads_a_chunk(flow, capsys, monkeypatch):
    """inspect answers from the manifest alone, so file length cannot defeat it."""
    cap, _ = flow
    from ppk2lab.capture.artifact import ArtifactReader

    def forbidden(*args, **kwargs):
        raise AssertionError("inspect must not read sample chunks")

    monkeypatch.setattr(ArtifactReader, "iter_raw_words", forbidden)
    monkeypatch.setattr(ArtifactReader, "verify_sha256", forbidden)
    code, payload = run_json(capsys, "--json", "inspect", str(cap))
    assert code == 0 and payload["ok"]
    result = payload["result"]
    assert result["samples"]["stored"] == 30000
    assert result["duration_s"] == pytest.approx(0.3)
    assert result["complete"] is True
    assert result["gap_count"] == 0 and result["gaps_truncated"] == 0
    assert result["timeline"]["sample_rate_hz"] == 100000


def test_inspect_does_not_claim_verified_integrity(flow, capsys):
    """A manifest-only read verifies no sample byte and must say so."""
    cap, _ = flow
    _code, payload = run_json(capsys, "--json", "inspect", str(cap))
    samples = payload["result"]["samples"]
    assert samples["sha256"]  # the recorded digest is still reported
    assert samples["sha256_verified"] is False


def test_inspect_surfaces_gaps_and_truncated_gap_table(tmp_path, capsys):
    """Loss the gap table could not enumerate is still reported, never hidden."""
    from array import array

    from ppk2lab.capture.artifact import ArtifactWriter
    from ppk2lab.capture.model import CaptureMeta
    from ppk2lab.protocol.samples import GapEvent, SampleBlock

    path = tmp_path / "gappy.ppk2a"
    writer = ArtifactWriter(path, CaptureMeta())
    writer.add_block(SampleBlock(0, array("I", [0] * 100)))
    writer.add_gap(GapEvent(index=100, missing=64, reason="counter_skip", ambiguous=True))
    writer.add_block(SampleBlock(164, array("I", [0] * 100)))
    writer._gaps_truncated = 7  # a desync storm past MAX_GAPS
    writer.finalize(complete=False)

    code, payload = run_json(capsys, "--json", "inspect", str(path))
    assert code == 0
    result = payload["result"]
    assert result["gap_count"] == 1
    assert result["gaps_truncated"] == 7
    assert result["samples"]["missing_known"] == 64
    assert result["complete"] is False
    assert result["duration_is_lower_bound"] is True
    codes = [w["code"] for w in payload["warnings"]]
    assert codes.count("W_SAMPLE_GAPS") == 1  # the gaps themselves
    assert codes.count("W_GAP_TABLE_TRUNCATED") == 1  # gaps counted but not enumerated


def test_inspect_rejects_a_missing_file(capsys, tmp_path):
    code, payload = run_json(capsys, "--json", "inspect", str(tmp_path / "nope.ppk2a"))
    assert code == 2
    assert payload["error"]["code"] == "CAPTURE_FILE_INVALID"


# ---------------------------------------------------------------------------
# --max-samples: a long capture is refused with an actionable error, not a lie


@pytest.mark.parametrize("command", ["decode", "measure", "assert", "export"])
def test_max_samples_refuses_oversized_load(flow, capsys, tmp_path, command):
    """Too big to load is CAPTURE_TOO_LARGE, not 'this file is corrupt'."""
    cap, _ = flow
    extra = {
        "decode": ["--uart", "D0"],
        "measure": [],
        "assert": ["--rule", "max_current < 1A"],
        "export": ["--format", "csv", "--output", str(tmp_path / f"{command}.csv")],
    }[command]
    code, payload = run_json(capsys, "--json", command, str(cap), "--max-samples", "1", *extra)
    assert code == 2
    assert payload["error"]["code"] == "CAPTURE_TOO_LARGE"
    # The remediation must name flags this CLI actually has.
    assert "--max-samples" in payload["error"]["remediation"]
    assert "ppk2lab inspect" in payload["error"]["remediation"]


def test_max_samples_none_removes_the_ceiling(flow, capsys):
    cap, _ = flow
    code, payload = run_json(capsys, "--json", "measure", str(cap), "--max-samples", "none")
    assert code == 0
    assert payload["result"]["window"]["samples"]["stored"] == 30000


def test_max_samples_rejects_a_nonsense_value(flow, capsys):
    cap, _ = flow
    code, payload = run_json(capsys, "--json", "measure", str(cap), "--max-samples", "0")
    assert code == 2
    assert payload["error"]["code"] == "INVALID_ARGUMENT"


# ---------------------------------------------------------------------------
# doctor: a failing check is a nonzero exit


def test_doctor_simulated_exits_zero(capsys):
    """`ppk2lab --simulate doctor --json` is the CI smoke step; it must stay 0."""
    code, payload = run_json(capsys, "--simulate", "--json", "doctor")
    assert code == 0
    assert payload["result"]["summary"]["fail"] == 0
    assert payload["result"]["exit_code"] == 0
    assert all(c["exit_code"] == 0 for c in payload["result"]["checks"])


def test_doctor_exits_device_not_found_when_no_device(capsys, monkeypatch):
    """`ppk2lab doctor --json || abort` has to be able to abort."""
    import ppk2lab.cli.commands as impl

    monkeypatch.setattr(impl, "discover", lambda **kw: [])
    code, payload = run_json(capsys, "--json", "doctor")
    assert code == 3
    checks = {c["name"]: c for c in payload["result"]["checks"]}
    assert checks["devices_found"]["status"] == "fail"
    assert checks["devices_found"]["exit_code"] == 3
    assert payload["result"]["summary"]["fail"] == 1
    # Nothing to select between, so the selection check did not run.
    assert checks["device_selection"]["status"] == "skip"


def test_doctor_warn_and_skip_stay_non_blocking(capsys, monkeypatch):
    """warn/skip carry no exit code by contract; only 'fail' blocks."""
    code, payload = run_json(capsys, "--simulate", "--json", "doctor")
    assert code == 0
    statuses = {c["name"]: c for c in payload["result"]["checks"]}
    assert statuses["stream_rate"]["status"] == "skip"
    assert statuses["stream_rate"]["exit_code"] == 0


def test_doctor_warns_when_the_device_is_ambiguous(capsys, monkeypatch):
    """Two units attached and no --device: the report must name what it measured."""
    import ppk2lab.cli.commands as impl
    from ppk2lab.discovery import simulated_device_info

    two = [simulated_device_info("SIM0001"), simulated_device_info("SIM0002")]
    monkeypatch.setattr(impl, "discover", lambda **kw: two)
    code, payload = run_json(capsys, "--simulate", "--json", "doctor")
    assert code == 0  # a warn never blocks
    checks = {c["name"]: c for c in payload["result"]["checks"]}
    assert checks["device_selection"]["status"] == "warn"
    assert "--device" in checks["device_selection"]["remediation"]


def test_doctor_single_device_does_not_warn(capsys):
    _code, payload = run_json(capsys, "--simulate", "--json", "doctor")
    checks = {c["name"]: c for c in payload["result"]["checks"]}
    assert checks["device_selection"]["status"] == "pass"


# ---------------------------------------------------------------------------
# firmware fingerprint: the key every compatibility-matrix row is filed under


def test_info_publishes_the_firmware_fingerprint(capsys):
    code, payload = run_json(capsys, "--simulate", "--json", "info")
    assert code == 0
    fingerprint = payload["result"]["firmware_fingerprint"]
    assert set(fingerprint) == {
        "hw",
        "ia",
        "metadata_keys",
        "metadata_key_count",
        "port_count",
    }
    assert fingerprint["metadata_key_count"] == len(fingerprint["metadata_keys"])


def test_doctor_publishes_the_same_fingerprint_as_info(capsys):
    _code, info = run_json(capsys, "--simulate", "--json", "info")
    _code, doctor = run_json(capsys, "--simulate", "--json", "doctor")
    assert doctor["result"]["firmware_fingerprint"] == info["result"]["firmware_fingerprint"]
    names = {c["name"] for c in doctor["result"]["checks"]}
    assert "firmware_fingerprint" in names


def test_fingerprint_port_count_is_null_when_the_device_was_not_enumerated():
    """A --port-opened unit has no port list; unknown must not read as zero.

    Publishing 0 would make the same hardware produce a different matrix row
    depending on how it was opened, and the rows would silently never match.
    """
    from ppk2lab.cli.commands import _cli_fingerprint
    from ppk2lab.device import PPK2
    from ppk2lab.transport.mock import MockTransport, SimulatedPPK2

    device = PPK2.open(transport=MockTransport(SimulatedPPK2()))
    try:
        assert device.info.ports == ()
        assert device.firmware_fingerprint()["port_count"] == 0
        assert _cli_fingerprint(device)["port_count"] is None
    finally:
        device.close()


# ---------------------------------------------------------------------------
# capabilities legibility: an agent must be able to build a call from it alone


def test_capabilities_publishes_usable_option_metadata(capsys):
    _code, payload = run_json(capsys, "--json", "capabilities")
    commands = payload["result"]["commands"]
    assert all(c["description"] for c in commands)
    options = [o for c in commands for o in c["options"]]
    assert options
    assert not [o for o in options if o["help"] in ("", "==SUPPRESS==")]
    export = next(c for c in commands if c["name"] == "export")
    fmt = next(o for o in export["options"] if o["flags"] == ["--format"])
    assert fmt["choices"] == ["csv", "vcd", "jsonl"] and fmt["required"] is True
    # The one state-changing command must publish what its values may be.
    configure = next(c for c in commands if c["name"] == "configure")
    mode = next(o for o in configure["options"] if o["flags"] == ["--mode"])
    assert mode["choices"] == ["ampere", "source"]
    # Integer choices are stringified so the list has one type.
    decode = next(c for c in commands if c["name"] == "decode")
    spi_mode = next(o for o in decode["options"] if o["flags"] == ["--spi-mode"])
    assert spi_mode["choices"] == ["0", "1", "2", "3"]


def test_capabilities_publishes_global_options(capsys):
    """--json/--simulate are suppressed per subcommand; they are published once."""
    _code, payload = run_json(capsys, "--json", "capabilities")
    flags = {f for o in payload["result"]["global_options"] for f in o["flags"]}
    assert {"--json", "--simulate", "--version"} <= flags


def test_capabilities_publishes_open_reason_catalogs(capsys):
    """gap/interruption reasons are catalogs, not enums: adding one is not breaking."""
    _code, payload = run_json(capsys, "--json", "capabilities")
    result = payload["result"]
    gap = {r["code"] for r in result["gap_reasons"]}
    interruption = {r["code"] for r in result["interruption_reasons"]}
    assert {"counter_skip", "host_overflow", "usb_stall", "stream_desync"} <= gap
    assert {"stream_stalled", "timeline_compression"} <= interruption
    # Same shape as the warning catalog, so one reader handles all three.
    warning_keys = {frozenset(w) for w in result["warning_codes"]}
    assert {frozenset(r) for r in result["gap_reasons"]} == warning_keys
    assert {frozenset(r) for r in result["interruption_reasons"]} == warning_keys


# ---------------------------------------------------------------------------
# library errors that reach the CLI boundary


def test_annotation_with_an_unrepresentable_number_is_a_typed_error(flow, tmp_path, capsys):
    """A JSON int too large for a double raises OverflowError, not ValueError."""
    cap, _ = flow
    bad = tmp_path / "bad.jsonl"
    bad.write_text(
        json.dumps(
            {
                "decoder": "uart",
                "kind": "frame",
                "start_sample": 0,
                "end_sample": 1,
                "confidence": 10**400,
            }
        )
        + "\n"
    )
    code, payload = run_json(capsys, "--json", "measure", str(cap), "--annotations", str(bad))
    assert code == 2
    assert payload["error"]["code"] == "CAPTURE_FILE_INVALID"


def test_measure_annotations_rejects_an_unknown_group_by():
    """Public entry points raise Ppk2labError, the documented contract."""
    from ppk2lab.analysis.measure import measure_annotations
    from ppk2lab.errors import Ppk2labError

    with pytest.raises(Ppk2labError):
        measure_annotations(object(), [], group_by="whatever")


# ---------------------------------------------------------------------------
# Windowed and decimated exports; distribution statistics


def test_windowed_export_reports_the_digest_it_did_not_verify(flow, tmp_path, capsys):
    """A window CRC-checks only the chunks it touched, so the file digest is null."""
    cap, _ = flow
    out = tmp_path / "window.csv"
    code, payload = run_json(
        capsys,
        "--json",
        "export",
        str(cap),
        "--format",
        "csv",
        "--output",
        str(out),
        "--window",
        "0.05:0.1",
    )
    assert code == 0
    jsonschema.validate(payload["result"], get_schema("export-result"))
    assert payload["result"]["capture_sha256"] is None
    assert payload["result"]["window"]["start_s"] == 0.05
    assert "W_PARTIAL_INTEGRITY" in {w["code"] for w in payload["warnings"]}
    # Time is anchored to the artifact, not to the window: two exports of one
    # capture must not disagree about when anything happened.
    rows = out.read_text().splitlines()
    assert rows[1].split(",")[1].startswith("0.05")


def test_decimated_export_shares_no_column_with_the_raw_one(flow, tmp_path, capsys):
    """The guarantee that a summary cannot be mistaken for raw data is the header."""
    cap, _ = flow
    raw, buckets = tmp_path / "raw.csv", tmp_path / "buckets.csv"
    run_json(capsys, "--json", "export", str(cap), "--format", "csv", "--output", str(raw))
    code, payload = run_json(
        capsys,
        "--json",
        "export",
        str(cap),
        "--format",
        "csv",
        "--output",
        str(buckets),
        "--decimate",
        "1000",
    )
    assert code == 0
    assert payload["result"]["decimation"]["bucket_samples"] == 1000
    assert payload["result"]["records"] == payload["result"]["decimation"]["buckets"]
    assert "W_DECIMATED" in {w["code"] for w in payload["warnings"]}
    raw_columns = set(raw.read_text().splitlines()[0].split(","))
    bucket_columns = set(buckets.read_text().splitlines()[0].split(","))
    assert raw_columns & bucket_columns == set()


def test_decimation_is_refused_for_vcd(flow, tmp_path, capsys):
    """A bucket has no meaning for lines that only ever change state."""
    cap, _ = flow
    code, payload = run_json(
        capsys,
        "--json",
        "export",
        str(cap),
        "--format",
        "vcd",
        "--output",
        str(tmp_path / "o.vcd"),
        "--decimate",
        "100",
    )
    assert code == 2 and payload["error"]["code"] == "INVALID_ARGUMENT"


def test_export_estimates_its_own_size_before_writing_a_bigger_one(flow, tmp_path, capsys):
    """An agent has to be able to decide before committing to a huge write."""
    cap, _ = flow
    code, payload = run_json(
        capsys,
        "--json",
        "export",
        str(cap),
        "--format",
        "csv",
        "--output",
        str(tmp_path / "o.csv"),
    )
    assert code == 0
    result = payload["result"]
    assert result["bytes_per_record"] > 0
    assert result["estimated_bytes"] > 0
    assert "measured" in result["size_basis"]


def test_measure_reports_quantiles_and_a_state_split(flow, capsys):
    """The demo load is bimodal: its mean is a value it never draws."""
    cap, _ = flow
    code, payload = run_json(capsys, "--json", "measure", str(cap), "--state-threshold", "50uA")
    assert code == 0
    window = payload["result"]["window"]
    jsonschema.validate(window, get_schema("window-stats"))
    current = window["current_ua"]
    assert current["min"] <= current["p50"] <= current["p99"] <= current["max"]
    split = window["state_split"]
    assert split["threshold_ua"] == 50.0
    assert split["below"]["mean_ua"] < 50.0 < split["above"]["mean_ua"]
    assert split["below"]["duration_s"] + split["above"]["duration_s"] > 0
    # Without the flag the split is absent rather than defaulted to a boundary
    # nobody chose.
    _, plain = run_json(capsys, "--json", "measure", str(cap))
    assert plain["result"]["window"]["state_split"] is None


def test_measure_publishes_a_typical_uncertainty_never_a_guaranteed_one(flow, capsys):
    cap, _ = flow
    _, payload = run_json(capsys, "--json", "measure", str(cap))
    uncertainty = payload["result"]["window"]["uncertainty"]
    assert uncertainty["guaranteed"] is False
    assert uncertainty["mean_ua_typical"] > 0
    # The systematic figure and the statistical one are separate numbers and
    # must never be added: one is a gain spec, the other is how settled this
    # particular mean is.
    assert uncertainty["mean_ua_batch_stderr"] != uncertainty["mean_ua_typical"]


def test_percentile_assertion_metrics_parse_and_evaluate(flow, capsys):
    """`p99_current` was unparseable: the metric pattern excluded digits."""
    cap, _ = flow
    code, payload = run_json(capsys, "--json", "assert", str(cap), "--rule", "p99_current < 1A")
    assert code == 0 and payload["result"]["outcomes"][0]["status"] == "passed"
    assert payload["result"]["outcomes"][0]["rule"]["metric"] == "p99_current"


def test_window_and_annotations_are_refused_rather_than_one_ignored(flow, capsys):
    """Silently dropping a flag is a measurement over a range nobody asked for."""
    cap, ann = flow
    code, payload = run_json(
        capsys, "--json", "measure", str(cap), "--annotations", str(ann), "--window", "0:0.1"
    )
    assert code == 2
    assert payload["error"]["code"] == "INVALID_ARGUMENT"
    assert "cannot be combined" in payload["error"]["message"]
