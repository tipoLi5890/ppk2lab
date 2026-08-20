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
        "compare",
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

    monkeypatch.setattr(ppk2lab.device, "discover", lambda **_kw: [])
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


def test_enabling_dut_power_says_it_will_not_outlive_the_command(capsys):
    """The device drops VOUT once the host closes the port.

    Measured on hardware at under half a second, so `configure --dut-power on
    --apply` cannot leave a DUT powered for a later `capture`. The result
    reports `dut_power: true`, which stops being true moments after the
    process exits — the warning is what keeps that from being a false promise.
    """
    code, payload = run_json(
        capsys, "--simulate", "--json", "configure", "--dut-power", "on", "--apply"
    )
    assert code == 0
    assert "W_DUT_POWER_TRANSIENT" in {w["code"] for w in payload["warnings"]}

    # A dry run changed nothing, and turning power off has nothing to lose.
    _, dry = run_json(capsys, "--simulate", "--json", "configure", "--dut-power", "on")
    assert "W_DUT_POWER_TRANSIENT" not in {w["code"] for w in dry["warnings"]}
    _, off = run_json(capsys, "--simulate", "--json", "configure", "--dut-power", "off", "--apply")
    assert "W_DUT_POWER_TRANSIENT" not in {w["code"] for w in off["warnings"]}


# -- exit 6 end to end ---------------------------------------------------
#
# The library decides `incomplete`; the CLI turns it into exit 6, and a CI
# gate reads that number and nothing else. That mapping had no end-to-end
# test, so nothing stopped a refactor from reporting an unevaluated rule as
# a pass.


def _gappy_capture(tmp_path):
    from ppk2lab.testing.profiles import ConstantProfile

    from .conftest import capture_of

    capture = capture_of(ConstantProfile(50.0), samples=2000, gaps={900: 40})
    path = tmp_path / "gappy.ppk2a"
    capture.save(str(path))
    return path


def test_assert_over_a_gap_exits_six_and_names_the_reason(tmp_path, capsys):
    path = _gappy_capture(tmp_path)
    code, payload = run_json(capsys, "--json", "assert", str(path), "--rule", "avg_current < 1A")
    assert code == 6, "an unevaluated rule must not leave the gate green"
    jsonschema.validate(payload["result"], get_schema("assert-result"))
    outcome = payload["result"]["outcomes"][0]
    assert outcome["status"] == "incomplete"
    assert outcome["observations"][0]["reason_code"] == "sample_gaps"
    assert payload["result"]["passed"] is False


def test_incomplete_outranks_a_real_failure_in_the_exit_code(tmp_path, capsys):
    """A build that is both unevaluated and failing reports the harder one."""
    path = _gappy_capture(tmp_path)
    code, _ = run_json(
        capsys,
        "--json",
        "assert",
        str(path),
        "--rule",
        "avg_current < 1A",
        "--rule",
        "avg_current < 1nA",
    )
    assert code == 6


def test_inspect_of_a_gappy_capture_still_exits_zero(tmp_path, capsys):
    """Looking at a damaged capture is what inspect is for; it must not fail."""
    path = _gappy_capture(tmp_path)
    code, payload = run_json(capsys, "--json", "inspect", str(path))
    assert code == 0
    assert payload["result"]["complete"] is False
    assert payload["result"]["gap_count"] == 1


def _interrupted_but_gap_free_capture(tmp_path):
    from ppk2lab.capture.model import Capture
    from ppk2lab.testing.profiles import ConstantProfile

    from .conftest import capture_of

    base = capture_of(ConstantProfile(50.0), samples=2000)
    capture = Capture(
        base.meta,
        base.words,
        [],
        complete=False,
        interruption={"reason": "keyboard_interrupt"},
    )
    path = tmp_path / "interrupted.ppk2a"
    capture.save(str(path))
    return path


def _codes_of(payload):
    return {w["code"] for w in payload["warnings"]}


def test_an_interrupted_capture_is_not_reported_as_having_sample_gaps(tmp_path, capsys):
    """A code names one thing; branching on it must not be misled."""
    path = _interrupted_but_gap_free_capture(tmp_path)
    _, payload = run_json(capsys, "--json", "measure", str(path))
    assert "W_INTERRUPTED" in _codes_of(payload)
    assert "W_SAMPLE_GAPS" not in _codes_of(payload)

    _, payload = run_json(capsys, "--json", "decode", str(path), "--uart", "D0", "--baud", "9600")
    assert "W_INTERRUPTED" in _codes_of(payload)
    assert "W_SAMPLE_GAPS" not in _codes_of(payload)


def test_a_gappy_capture_still_reports_sample_gaps(tmp_path, capsys):
    path = _gappy_capture(tmp_path)
    _, payload = run_json(capsys, "--json", "measure", str(path))
    assert "W_SAMPLE_GAPS" in _codes_of(payload)
    assert "W_INTERRUPTED" not in _codes_of(payload)


# -- provenance on the command line --------------------------------------


def test_capture_tags_round_trip_through_inspect(tmp_path, capsys):
    path = tmp_path / "tagged.ppk2a"
    code, _ = run_json(
        capsys,
        "--simulate",
        "--json",
        "capture",
        "--duration",
        "100ms",
        "--output",
        str(path),
        "--tag",
        "sn=POD01",
        "--tag",
        "fw=0.3.0+g1a2b3c",
        "--tag",
        "note=has=an=equals",
    )
    assert code == 0
    code, payload = run_json(capsys, "--json", "inspect", str(path))
    assert code == 0
    assert payload["result"]["user_tags"] == {
        "sn": "POD01",
        "fw": "0.3.0+g1a2b3c",
        "note": "has=an=equals",
    }


@pytest.mark.parametrize("bad", ["justkey", "=novalue"])
def test_a_malformed_tag_is_a_usage_error(tmp_path, capsys, bad):
    code, payload = run_json(
        capsys,
        "--simulate",
        "--json",
        "capture",
        "--duration",
        "50ms",
        "--output",
        str(tmp_path / "x.ppk2a"),
        "--tag",
        bad,
    )
    assert code == 2
    assert not payload["ok"]


def test_the_same_tag_key_twice_is_refused(tmp_path, capsys):
    code, _ = run_json(
        capsys,
        "--simulate",
        "--json",
        "capture",
        "--duration",
        "50ms",
        "--output",
        str(tmp_path / "x.ppk2a"),
        "--tag",
        "sn=A",
        "--tag",
        "sn=B",
    )
    assert code == 2


def _capture_file(tmp_path, name, current_ua):
    from ppk2lab.testing.profiles import ConstantProfile

    from .conftest import capture_of

    path = tmp_path / name
    capture_of(ConstantProfile(current_ua), samples=3000).save(str(path))
    return path


def test_compare_reports_a_same_range_delta_with_a_tighter_bar(tmp_path, capsys):
    a = _capture_file(tmp_path, "a.ppk2a", 197.0)
    b = _capture_file(tmp_path, "b.ppk2a", 251.0)
    code, payload = run_json(capsys, "--json", "compare", str(a), str(b))
    assert code == 0
    jsonschema.validate(payload["result"], get_schema("compare-result"))
    result = payload["result"]
    assert result["basis"] == "same_range"
    assert result["uncertainty"]["gain_error_cancels"] is True
    assert result["delta"] == pytest.approx(54.0, abs=1.0)
    assert result["a"]["path"].endswith("a.ppk2a")


def test_compare_carries_both_sides_provenance_and_loss(tmp_path, capsys):
    a = _capture_file(tmp_path, "a.ppk2a", 197.0)
    gappy = _gappy_capture(tmp_path)
    code, payload = run_json(capsys, "--json", "compare", str(a), str(gappy))
    assert code == 0
    assert payload["result"]["b"]["complete"] is False
    assert payload["result"]["b"]["gap_count"] == 1
    assert "W_SAMPLE_GAPS" in _codes_of(payload)


def test_compare_refuses_an_unknown_metric(tmp_path, capsys):
    a = _capture_file(tmp_path, "a.ppk2a", 197.0)
    code, _ = run_json(capsys, "--json", "compare", str(a), str(a), "--metric", "temperature")
    assert code == 2


# -- compare: what each side is worth before its difference means anything --


def _capture_file_of(tmp_path, name, profile, *, samples=2000, **simulator):
    """A saved capture from a simulator configured for this test.

    ``_capture_file`` covers the constant-current case; this one exists for the
    comparisons whose point is that the two sides ran on different *settings* —
    a different supply voltage, a different mode — which only the simulator's
    own constructor can vary.
    """
    from .conftest import open_simulated

    device = open_simulated(profile, **simulator)
    try:
        result = device.capture(sample_limit=samples)
    finally:
        device.close()
    path = tmp_path / name
    result.capture.save(str(path))
    return path


def _floor_capture_file(tmp_path, name):
    """A capture straddling the 200 nA grid floor, so its low quantiles are bounds.

    Straddling is the load-bearing part: with the whole distribution below the
    floor the [min, max] clamp pulls the quantile back to a measured value, and
    nothing would be at the floor to report.
    """
    from ppk2lab.testing.profiles import StepProfile

    return _capture_file_of(tmp_path, name, StepProfile([(1400, 0.05, 0), (600, 0.5, 0)]))


def test_compare_names_the_measurement_floor_on_each_side(tmp_path, capsys):
    """Two floor-served quantiles differ by 0.0 for a reason that is not the DUT.

    The delta is the grid floor minus the grid floor. Without both warnings a
    reader sees "no change" and has nothing to tell it apart from a measured
    agreement between two sleeping boards.
    """
    a = _floor_capture_file(tmp_path, "a.ppk2a")
    b = _floor_capture_file(tmp_path, "b.ppk2a")
    code, payload = run_json(capsys, "--json", "compare", str(a), str(b), "--metric", "p5_current")
    assert code == 0
    jsonschema.validate(payload["result"], get_schema("compare-result"))
    floor = [w for w in payload["warnings"] if w["code"] == "W_BELOW_MEASUREMENT_FLOOR"]
    assert len(floor) == 2, "one warning cannot say which side it is about"
    # Each side is named, so two identically worded sentences stay distinguishable.
    assert floor[0]["message"].startswith(f"{a}: ")
    assert floor[1]["message"].startswith(f"{b}: ")
    assert payload["result"]["delta"] == 0.0
    assert payload["result"]["a"]["quantiles_at_floor"] == ["p5", "p50"]


def test_compare_says_when_a_charge_operand_is_only_a_lower_bound(tmp_path, capsys):
    """Two saturated captures also difference to 0.0, and for the same reason.

    Both integrals stop at the same ceiling, so a 1.5x load difference reports
    as no difference at all.
    """
    from ppk2lab.testing.profiles import ConstantProfile

    a = _capture_file_of(tmp_path, "a.ppk2a", ConstantProfile(2_000_000.0))
    b = _capture_file_of(tmp_path, "b.ppk2a", ConstantProfile(3_000_000.0))
    code, payload = run_json(capsys, "--json", "compare", str(a), str(b), "--metric", "charge")
    assert code == 0
    assert "W_CLIPPED" in _codes_of(payload)
    assert payload["result"]["b"]["charge_is_lower_bound"] is True
    assert payload["result"]["b"]["saturated_samples"] > 0
    assert payload["result"]["delta"] == 0.0


def test_an_unclipped_compare_claims_no_lower_bound(tmp_path, capsys):
    """False-alarm guard: a caveat that always fires carries no information."""
    a = _capture_file(tmp_path, "a.ppk2a", 197.0)
    b = _capture_file(tmp_path, "b.ppk2a", 251.0)
    _code, payload = run_json(capsys, "--json", "compare", str(a), str(b), "--metric", "charge")
    assert "W_CLIPPED" not in _codes_of(payload)
    assert payload["result"]["a"]["charge_is_lower_bound"] is False
    assert payload["result"]["b"]["charge_is_lower_bound"] is False


def _restamp_serial_number(path, serial_number):
    """Rewrite a stored artifact's recorded instrument.

    Two simulated captures otherwise carry one serial, and the claim under test
    is about what the manifests say, not about which device object produced
    them.
    """
    import zipfile

    with zipfile.ZipFile(path) as archive:
        members = [(info, archive.read(info.filename)) for info in archive.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for info, data in members:
            if info.filename == "manifest.json":
                manifest = json.loads(data)
                manifest["device"]["serial_number"] = serial_number
                data = json.dumps(manifest).encode()
            archive.writestr(info, data)


def test_compare_names_the_instrument_on_each_side(tmp_path, capsys):
    """The gain-error cancellation is one physical shunt's unknown.

    Same range index on two units is not the same shunt, so the basis stays
    `same_range` — a true statement about the ranges — while the cancellation
    that would have halved the error bar is withdrawn.
    """
    a = _capture_file(tmp_path, "a.ppk2a", 197.0)
    b = _capture_file(tmp_path, "b.ppk2a", 251.0)
    _restamp_serial_number(b, "PPK2-OTHER")
    code, payload = run_json(capsys, "--json", "compare", str(a), str(b))
    assert code == 0
    result = payload["result"]
    assert result["a"]["device"]["serial_number"] != result["b"]["device"]["serial_number"]
    assert "W_INSTRUMENT_MISMATCH" in _codes_of(payload)
    assert result["basis"] == "same_range"
    assert result["same_instrument"] is False
    assert result["uncertainty"]["gain_error_cancels"] is False


def test_compare_of_energy_says_when_the_two_supplies_disagree(tmp_path, capsys):
    """Energy is charge x V, and V came from each capture's own supply.

    The two currents are identical here, so every microjoule of the delta is
    the setpoint change. Reporting that as a result about the DUT is the
    failure this warning exists to prevent.
    """
    from ppk2lab.testing.profiles import ConstantProfile

    a = _capture_file_of(tmp_path, "a.ppk2a", ConstantProfile(200.0), initial_vdd_mv=3000)
    b = _capture_file_of(tmp_path, "b.ppk2a", ConstantProfile(200.0), initial_vdd_mv=1800)
    code, payload = run_json(capsys, "--json", "compare", str(a), str(b), "--metric", "energy")
    assert code == 0
    jsonschema.validate(payload["result"], get_schema("compare-result"))
    assert "W_VOLTAGE_ASSUMED" in _codes_of(payload)
    voltage = payload["result"]["voltage"]
    assert voltage["differs"] is True
    assert (voltage["a"], voltage["b"]) == (3000, 1800)


def test_compare_of_energy_explains_itself_when_energy_is_not_computable(tmp_path, capsys):
    """A null delta must say why, or it reads as a bug rather than a refusal.

    In ampere mode the device's voltage field is a source setpoint the DUT
    never ran from, so there is no supply to multiply charge by.
    """
    from ppk2lab.testing.profiles import ConstantProfile
    from ppk2lab.types import Mode

    a = _capture_file_of(tmp_path, "a.ppk2a", ConstantProfile(200.0), initial_mode=Mode.AMPERE)
    b = _capture_file_of(tmp_path, "b.ppk2a", ConstantProfile(250.0), initial_mode=Mode.AMPERE)
    code, payload = run_json(capsys, "--json", "compare", str(a), str(b), "--metric", "energy")
    assert code == 0
    assert payload["result"]["delta"] is None
    assert payload["result"]["voltage"]["differs"] is False
    explanations = [w["message"] for w in payload["warnings"] if w["code"] == "W_VOLTAGE_ASSUMED"]
    assert explanations, "a null delta with no warning is silence"
    assert "ampere mode" in explanations[0]


def test_compare_reports_an_interrupted_side_without_calling_it_a_gap(tmp_path, capsys):
    """A stopped capture lost no sample it recorded; a gappy one did.

    `compare` reports both sides' loss, and it must keep the two apart for the
    same reason `measure` does: an agent branches on the code.
    """
    a = _capture_file(tmp_path, "a.ppk2a", 197.0)
    interrupted = _interrupted_but_gap_free_capture(tmp_path)
    code, payload = run_json(capsys, "--json", "compare", str(a), str(interrupted))
    assert code == 0
    assert "W_INTERRUPTED" in _codes_of(payload)
    assert "W_SAMPLE_GAPS" not in _codes_of(payload)
    assert payload["result"]["b"]["complete"] is False


def test_compare_prints_the_deltas_own_stderr_beside_the_typical_bar(tmp_path, capsys):
    """The two numbers answer different questions and can differ by orders.

    `delta_typical` is a gain specification; the batch stderr says how settled
    these two particular means are. Printing only the first showed
    `delta: -300 +/- 37 uA` for a delta whose own stderr was 299 uA — a result
    indistinguishable from zero, rendered as a confident one.
    """
    a = _capture_file(tmp_path, "a.ppk2a", 197.0)
    b = _capture_file(tmp_path, "b.ppk2a", 251.0)
    code = main(["compare", str(a), str(b), "--metric", "mean_current"])
    out = capsys.readouterr().out
    assert code == 0
    assert "not guaranteed" in out
    assert "batch stderr" in out


# -- the printed statistics line -----------------------------------------


def _stats_json(profile, samples=2000):
    from ppk2lab.analysis.measure import measure_window

    from .conftest import capture_of

    return measure_window(capture_of(profile, samples=samples)).to_json()


def test_a_floor_served_quantile_is_marked_in_the_printed_distribution():
    """`quantiles_at_floor` is monotone in rank, so the marking has to be too.

    With 30% of samples under the floor, p5 is the floor and p50 is measured.
    While the printed line was a fixed p50/p90/p99 tuple there was a whole band
    in which the warning named a quantile the line never showed.
    """
    from ppk2lab.cli.commands import _fmt_stats
    from ppk2lab.testing.profiles import StepProfile

    stats = _stats_json(StepProfile([(600, 0.05, 0), (1400, 100.0, 0)]))
    assert stats["distribution"]["quantiles_at_floor"] == ["p5"]
    text = _fmt_stats(stats)
    assert "p5 <=" in text
    assert "p50 <=" not in text


def test_a_measured_distribution_is_printed_without_a_bound_marker():
    """False-alarm guard: an always-on marker would say nothing at all."""
    from ppk2lab.cli.commands import _fmt_stats
    from ppk2lab.testing.profiles import ConstantProfile

    stats = _stats_json(ConstantProfile(1000.0))
    assert stats["distribution"]["quantiles_at_floor"] == []
    assert "<=" not in _fmt_stats(stats)


# -- export provenance comments ------------------------------------------


def test_export_csv_writes_the_provenance_comments_above_the_header(flow, tmp_path, capsys):
    """A derived file usually outlives the session that made it.

    Asserted on bytes because the line ending is the point: csv.writer runs
    under `newline=""` and emits CRLF, so a comment terminated with a bare LF
    left a file an RFC 4180 reader parses as one record — the whole preamble
    glued onto the header.
    """
    cap, _ = flow
    out = tmp_path / "annotated.csv"
    code, payload = run_json(
        capsys,
        "--json",
        "export",
        str(cap),
        "--format",
        "csv",
        "--output",
        str(out),
        "--comment",
        "sn: POD01",
        "--comment",
        "build: 42",
    )
    assert code == 0
    jsonschema.validate(payload["result"], get_schema("export-result"))
    data = out.read_bytes()
    lines = data.split(b"\r\n")
    assert lines[0] == b"# sn: POD01"
    assert lines[1] == b"# build: 42"
    assert lines[2].startswith(b"timeline_index,")
    # No bare LF anywhere: comment lines and data rows end alike.
    assert data.count(b"\n") == data.count(b"\r\n")


@pytest.mark.parametrize("fmt", ["vcd", "jsonl"])
def test_a_comment_is_refused_by_a_format_that_has_no_comment_line(flow, tmp_path, capsys, fmt):
    """Dropping the flag silently would hand back a file the caller believes
    is annotated, and the refusal has to come before anything is written."""
    cap, _ = flow
    out = tmp_path / f"o.{fmt}"
    code, payload = run_json(
        capsys,
        "--json",
        "export",
        str(cap),
        "--format",
        fmt,
        "--output",
        str(out),
        "--comment",
        "sn: POD01",
    )
    assert code == 2
    assert payload["ok"] is False
    assert payload["error"]["code"] == "INVALID_ARGUMENT"
    assert not out.exists()


# -- provenance the capture hands straight back ---------------------------


def test_a_capture_result_carries_the_tags_it_was_given(tmp_path, capsys):
    """Tagging a capture should not force a reopen to read the tags back.

    docs/api-baseline.md lists `user_tags` on capture-result; the field was
    absent, so the documented contract and the payload disagreed.
    """
    path = tmp_path / "tagged.ppk2a"
    code, payload = run_json(
        capsys,
        "--simulate",
        "--json",
        "capture",
        "--duration",
        "50ms",
        "--output",
        str(path),
        "--tag",
        "sn=POD01",
        "--tag",
        "build=42",
    )
    assert code == 0
    jsonschema.validate(payload["result"], get_schema("capture-result"))
    assert payload["result"]["user_tags"] == {"sn": "POD01", "build": "42"}


def test_a_warning_has_the_same_shape_live_stored_and_on_inspect(tmp_path, capsys):
    """One warning, three readers, one shape.

    `category` was stripped on the way into the manifest, so a capture and a
    later `inspect` of the same file reported differently shaped warnings and a
    reader keying on `category` saw it appear and disappear.
    """
    import zipfile

    path = tmp_path / "warned.ppk2a"
    code, payload = run_json(
        capsys, "--simulate", "--json", "capture", "--duration", "50ms", "--output", str(path)
    )
    assert code == 0
    live = payload["result"]["warnings"]
    assert live, "this test says nothing unless the capture actually warned"

    with zipfile.ZipFile(path) as archive:
        stored = json.loads(archive.read("manifest.json"))["warnings"]

    _code, inspected = run_json(capsys, "--json", "inspect", str(path))

    assert live == stored == inspected["result"]["warnings"]
    for warning in live:
        assert set(warning) == {"code", "message", "category"}
