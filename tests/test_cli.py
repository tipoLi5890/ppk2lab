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
