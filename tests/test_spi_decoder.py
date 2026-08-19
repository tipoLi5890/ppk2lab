"""SPI decoder golden vectors (self-generated) across modes and options."""

import pytest

from ppk2lab.decoders.base import LogicChunk
from ppk2lab.decoders.feasibility import Tier, spi_feasibility
from ppk2lab.decoders.spi import SPIDecoder
from ppk2lab.errors import DecoderRateError, UsageError
from ppk2lab.testing.signals import merge_logic, spi_wave
from ppk2lab.types import GapEvent

# channel assignment used across the tests: D1=SCLK D2=MOSI D3=MISO D4=CS
CH = {"sclk": 1, "mosi": 2, "miso": 3, "cs": 4}


def logic_of(waves: dict[str, list[int]]) -> bytes:
    mapping = {CH[name]: wave for name, wave in waves.items()}
    return merge_logic(mapping, idle_levels={CH["cs"]: 1})


def make_decoder(**kw):
    defaults = {"sclk": "D1", "mosi": "D2", "miso": "D3", "cs": "D4"}
    defaults.update(kw)
    return SPIDecoder(**defaults)


def run(decoder, logic, chunk=None):
    annotations = []
    if chunk is None:
        annotations += decoder.feed(LogicChunk(0, logic))
    else:
        for i in range(0, len(logic), chunk):
            annotations += decoder.feed(LogicChunk(i, logic[i : i + chunk]))
    annotations += decoder.flush()
    return annotations


def words_of(annotations):
    return [a for a in annotations if a.kind == "word"]


def transactions_of(annotations):
    return [a for a in annotations if a.kind == "transaction"]


@pytest.mark.parametrize("mode", [0, 1, 2, 3])
def test_all_modes_clean_transaction(mode):
    mosi = [0x9F, 0x00, 0xA7]
    miso = [0x00, 0x52, 0x18]
    waves = spi_wave(mosi, miso, clock_hz=10_000, mode=mode)
    decoder = make_decoder(mode=mode, expected_clock_hz=10_000)
    annotations = run(decoder, logic_of(waves))
    words = words_of(annotations)
    assert [w.fields["mosi"] for w in words] == mosi
    assert [w.fields["miso"] for w in words] == miso
    assert all(not w.errors and w.confidence == 1.0 for w in words)
    txns = transactions_of(annotations)
    assert len(txns) == 1
    assert txns[0].fields["word_count"] == 3
    assert txns[0].fields["mosi_words"] == mosi


@pytest.mark.parametrize("chunk", [1, 5, 33, 256])
def test_chunk_boundaries_do_not_change_results(chunk):
    mosi = [0x12, 0x34, 0x56]
    waves = spi_wave(mosi, clock_hz=10_000, mode=0)
    decoder = make_decoder(mode=0)
    words = words_of(run(decoder, logic_of(waves), chunk=chunk))
    assert [w.fields["mosi"] for w in words] == mosi


def test_lsb_first():
    waves = spi_wave([0x01], clock_hz=10_000, mode=0, msb_first=False)
    decoder = make_decoder(mode=0, msb_first=False)
    words = words_of(run(decoder, logic_of(waves)))
    assert words[0].fields["mosi"] == 0x01


def test_wide_words():
    value = 0x1F2E3D
    waves = spi_wave([value], clock_hz=10_000, mode=0, word_bits=24)
    decoder = make_decoder(mode=0, word_bits=24)
    words = words_of(run(decoder, logic_of(waves)))
    assert words[0].fields["mosi"] == value
    assert words[0].fields["bits"] == 24


def test_cs_active_high():
    waves = spi_wave([0xC3], clock_hz=10_000, mode=0, cs_active_low=False)
    decoder = make_decoder(mode=0, cs_active_low=False)
    logic = merge_logic({CH[k]: v for k, v in waves.items()}, idle_levels={CH["cs"]: 0})
    words = words_of(run(decoder, logic))
    assert words[0].fields["mosi"] == 0xC3


def test_without_cs_idle_grouping():
    waves = spi_wave([0xAA, 0xBB], clock_hz=10_000, mode=0, with_cs=False)
    decoder = SPIDecoder(sclk="D1", mosi="D2", miso="D3", cs=None, mode=0, idle_timeout_samples=100)
    logic = merge_logic(
        {CH["sclk"]: waves["sclk"], CH["mosi"]: waves["mosi"], CH["miso"]: waves["miso"]}
    )
    annotations = run(decoder, logic)
    words = words_of(annotations)
    assert [w.fields["mosi"] for w in words] == [0xAA, 0xBB]
    assert len(transactions_of(annotations)) == 1


def test_mosi_only_and_miso_only():
    waves = spi_wave([0x77], clock_hz=10_000, mode=0)
    decoder = SPIDecoder(sclk="D1", mosi="D2", cs="D4", mode=0)
    words = words_of(run(decoder, logic_of(waves)))
    assert words[0].fields["mosi"] == 0x77
    assert "miso" not in words[0].fields
    with pytest.raises(UsageError):
        SPIDecoder(sclk="D1")  # neither mosi nor miso


def test_partial_word_at_cs_deassert_flagged():
    waves = spi_wave([0xF0], clock_hz=10_000, mode=0, word_bits=8)
    # cut CS short: deassert after ~5 clock cycles
    cs = list(waves["cs"])
    cut = 30 + 5 * 10  # idle_before + cs_setup + 5 cycles at 10 samples
    for i in range(cut, len(cs)):
        cs[i] = 1
    waves["cs"] = cs
    decoder = make_decoder(mode=0)
    annotations = run(decoder, logic_of(waves))
    words = words_of(annotations)
    assert words and "partial_word" in words[0].errors
    assert words[0].confidence == 0.0


def test_gap_mid_transaction_closes_with_gap_error():
    waves = spi_wave([0x9F, 0x00], clock_hz=10_000, mode=0)
    logic = logic_of(waves)
    decoder = make_decoder(mode=0)
    split = len(logic) // 2
    annotations = decoder.feed(LogicChunk(0, logic[:split]))
    annotations += decoder.notify_gap(GapEvent(index=split, missing=40))
    annotations += decoder.feed(LogicChunk(split + 40, logic[split:]))
    annotations += decoder.flush()
    txns = transactions_of(annotations)
    assert any("gap" in t.errors for t in txns)
    assert all(t.confidence == 0.0 for t in txns if "gap" in t.errors)


def test_errors_imply_zero_confidence():
    """Cross-decoder invariant: an annotation with errors is never decoded data.

    UART now matches this; pinning it on both sides keeps a consumer able to
    filter on ``errors`` alone and get the same answer as ``confidence``.
    """
    waves = spi_wave([0x9F, 0x00, 0xA7], clock_hz=10_000, mode=0)
    logic = logic_of(waves)
    decoder = make_decoder(mode=0)
    split = len(logic) // 2
    annotations = decoder.feed(LogicChunk(0, logic[:split]))
    annotations += decoder.notify_gap(GapEvent(index=split, missing=40))
    annotations += decoder.feed(LogicChunk(split + 40, logic[split : split + 30]))
    annotations += decoder.flush()
    seen = {e for a in annotations for e in a.errors}
    assert {"gap", "truncated"} & seen
    assert all(a.confidence == 0.0 for a in annotations if a.errors)


def test_rate_tiers():
    assert spi_feasibility(10_000).tier is Tier.VALIDATED
    assert spi_feasibility(20_000).tier is Tier.CONDITIONAL
    assert spi_feasibility(40_000).tier is Tier.EXPERIMENTAL
    assert spi_feasibility(50_000).tier is Tier.UNSUPPORTED
    with pytest.raises(DecoderRateError):
        make_decoder(expected_clock_hz=25_000)  # experimental needs opt-in
    decoder = make_decoder(expected_clock_hz=25_000, allow_experimental=True)
    assert decoder.feasibility.tier is Tier.EXPERIMENTAL
    with pytest.raises(DecoderRateError):
        make_decoder(expected_clock_hz=60_000, allow_experimental=True)


def test_conditional_20khz_still_decodes():
    mosi = [0x5A, 0x3C]
    waves = spi_wave(mosi, clock_hz=20_000, mode=0)
    decoder = make_decoder(mode=0, expected_clock_hz=20_000)
    words = words_of(run(decoder, logic_of(waves)))
    assert [w.fields["mosi"] for w in words] == mosi
    # at 20 kHz the sample grid yields alternating 6/4-sample cycles; the
    # per-word confidence honestly reflects the worst observed cycle
    assert all(0.0 < w.confidence <= 0.7 for w in words)
    assert all(not w.errors for w in words)
