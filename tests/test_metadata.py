import math

from ppk2lab.protocol.metadata import parse_metadata

FULL = (
    "Calibrated: 1\n"
    "R0: 1003.35\nR1: 101.5\nR2: 10.2\nR3: 1.1\nR4: 0.05\n"
    "GS0: 0.0\nGS1: 0.0\nGS2: 0.0\nGS3: 0.0\nGS4: 0.0\n"
    "GI0: 1.0\nGI1: 1.0\nGI2: 1.0\nGI3: 1.0\nGI4: 1.0\n"
    "O0: 100.0\nO1: 100.0\nO2: 100.0\nO3: 100.0\nO4: 100.0\n"
    "S0: 0.0\nS1: 0.0\nS2: 0.0\nS3: 0.0\nS4: 0.0\n"
    "I0: 0.0\nI1: 0.0\nI2: 0.0\nI3: 0.0\nI4: 0.0\n"
    "UG0: 1.0\nUG1: 1.0\nUG2: 1.0\nUG3: 1.0\nUG4: 1.0\n"
    "VDD: 3300\nHW: 2\nmode: 2\nIA: 0\nEND\n"
)


def test_parse_full_metadata():
    meta = parse_metadata(FULL)
    assert meta.terminated
    assert meta.calibrated is True
    assert meta.vdd_mv == 3300
    assert meta.hw == 2
    assert meta.mode == 2
    assert meta.cal["R"][0] == 1003.35
    assert meta.cal["O"][4] == 100.0
    assert meta.missing_cal_ranges() == []
    assert not meta.warnings


def test_crlf_and_reordering_tolerated():
    text = FULL.replace("\n", "\r\n")
    meta = parse_metadata(text)
    assert meta.terminated
    assert meta.vdd_mv == 3300


def test_unknown_keys_preserved_not_fatal():
    meta = parse_metadata("NEWFIELD: hello\nVDD: 3000\nEND\n")
    assert meta.extras == {"NEWFIELD": "hello"}
    assert meta.vdd_mv == 3000
    assert meta.terminated


def test_nan_values_preserved():
    meta = parse_metadata("R0: NaN\nVDD: 3000\nEND\n")
    assert math.isnan(meta.cal["R"][0])
    # NaN calibration means the range is unusable, not defaulted.
    assert 0 in meta.missing_cal_ranges()
    assert meta.to_json()["calibration"]["R"][0] is None


def test_truncated_reply_reported():
    meta = parse_metadata("Calibrated: 1\nR0: 1003.3")
    assert not meta.terminated
    assert any("truncated" in w or "END" in w for w in meta.warnings)
    assert meta.cal["R"][0] == 1003.3  # partial data still preserved


def test_garbage_line_warned_not_fatal():
    meta = parse_metadata("@@@garbage@@@\nVDD: 3000\nEND\n")
    assert meta.vdd_mv == 3000
    assert any("unparseable" in w for w in meta.warnings)


def test_missing_values_stay_none():
    meta = parse_metadata("END\n")
    assert meta.vdd_mv is None
    assert meta.calibrated is None
    assert meta.missing_cal_ranges() == [0, 1, 2, 3, 4]
