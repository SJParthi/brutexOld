"""Pure-core tests for the equity OHLCV+Volume NSE verify (DEC-NSE-EQUITY-VERIFY-001).

100% line + branch coverage of :mod:`brutex.data.validation.nse_equity_oneday` —
the bhavcopy parse (every degrade arm), the Groww-candle → quote IST selection,
the OHLC+Volume compare (reusing the index harness), the env-tolerance resolvers,
and the degrade-safe :func:`validate_equity_oneday` orchestration (both openers
injected → fully offline). The two ``# pragma: no cover`` live fetchers are NOT
exercised (real Groww client / real NSE urllib).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from brutex.data.models import CandleBar
from brutex.data.validation.nse_equity_oneday import (
    BHAVCOPY_URL_OVERRIDE_ENV,
    EQUITY_CSV_SPEC_OVERRIDE_ENV,
    EQUITY_SERIES_OVERRIDE_ENV,
    PRICE_TOLERANCE_OVERRIDE_ENV,
    VOLUME_TOLERANCE_OVERRIDE_ENV,
    EquityCsvSpec,
    EquityQuote,
    VolumeComparison,
    _parse_volume,
    _resolve_price_tolerance,
    _resolve_volume_tolerance,
    _volume_note,
    bhavcopy_url,
    compare_equity_quotes,
    normalize_equity_symbol,
    parse_bhavcopy_quote,
    quote_from_groww_equity_candles,
    validate_equity_oneday,
)
from brutex.data.validation.nse_oneday import VerdictStatus
from brutex.models import BruteXError

_DAY = date(2024, 6, 3)

# A real "sec_bhavdata_full" header subset (with the NSE leading-space quirk on
# SERIES, proving the whitespace-tolerant column resolver).
_HEADER = "SYMBOL, SERIES,OPEN_PRICE,HIGH_PRICE,LOW_PRICE,CLOSE_PRICE,TTL_TRD_QNTY"


def _bhavcopy(rows: list[str]) -> str:
    return "\n".join([_HEADER, *rows])


def _bar(
    *,
    day: date = _DAY,
    open_: str = "2500.00",
    high: str = "2550.00",
    low: str = "2490.00",
    close: str = "2530.00",
    volume: int = 1_000_000,
    hour: int = 4,
) -> CandleBar:
    """A daily CandleBar whose IST-local date is *day* (04:00 UTC ⇒ 09:30 IST)."""
    return CandleBar(
        timestamp=datetime(day.year, day.month, day.day, hour, 0, tzinfo=UTC),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
        open_interest=None,
    )


# ── normalize + url + series ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("reliance", "RELIANCE"),
        ("NSE-RELIANCE", "RELIANCE"),
        ("  infy  ", "INFY"),
        # DEC-GROWW-HYPHEN-SYMBOL-RESOLVE-001 — a hyphenated ticker is kept whole; only
        # a leading exchange prefix is stripped (the prior split-on-first-hyphen mangled
        # BAJAJ-AUTO → AUTO).
        ("BAJAJ-AUTO", "BAJAJ-AUTO"),
        ("NSE-BAJAJ-AUTO", "BAJAJ-AUTO"),
        ("bse-m&m", "M&M"),
    ],
)
def test_normalize_equity_symbol(raw: str, expected: str) -> None:
    assert normalize_equity_symbol(raw) == expected


def test_bhavcopy_url_default_substitutes_ddmmyyyy() -> None:
    url = bhavcopy_url(_DAY)
    assert "sec_bhavdata_full_03062024.csv" in url


def test_bhavcopy_url_override_all_placeholders(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BHAVCOPY_URL_OVERRIDE_ENV, "https://m/{yyyymmdd}_{ddmmyyyy}_{iso}.csv")
    assert bhavcopy_url(_DAY) == "https://m/20240603_03062024_2024-06-03.csv"


def test_series_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(EQUITY_SERIES_OVERRIDE_ENV, "BE")
    text = _bhavcopy(["RELIANCE,BE,1,2,0.5,1.5,10"])
    quote = parse_bhavcopy_quote(text, symbol="RELIANCE", trade_date=_DAY)
    assert quote is not None
    assert quote.volume == 10


# ── _parse_volume ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1000", 1000), (" 2,500 ", 2500), ("0", 0)],
)
def test_parse_volume_ok(raw: str, expected: int) -> None:
    assert _parse_volume(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "abc", "12.5", "-5", 1000])
def test_parse_volume_rejects(raw: object) -> None:
    assert _parse_volume(raw) is None


# ── EquityCsvSpec override resolution (via parse) ─────────────────────────────


def test_csv_spec_override_renamed_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        EQUITY_CSV_SPEC_OVERRIDE_ENV,
        '{"volume_column": "QTY", "symbol_column": "TICKER"}',
    )
    text = "\n".join(
        ["TICKER,SERIES,OPEN_PRICE,HIGH_PRICE,LOW_PRICE,CLOSE_PRICE,QTY", "TCS,EQ,1,2,0.5,1.5,42"]
    )
    quote = parse_bhavcopy_quote(text, symbol="TCS", trade_date=_DAY)
    assert quote is not None
    assert quote.volume == 42


@pytest.mark.parametrize("bad", ["not-json", "[1,2,3]"])
def test_csv_spec_override_malformed_falls_back(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv(EQUITY_CSV_SPEC_OVERRIDE_ENV, bad)
    text = _bhavcopy(["RELIANCE,EQ,1,2,0.5,1.5,10"])
    assert parse_bhavcopy_quote(text, symbol="RELIANCE", trade_date=_DAY) is not None


# ── parse_bhavcopy_quote degrade arms ────────────────────────────────────────


def test_parse_happy() -> None:
    text = _bhavcopy(["RELIANCE,EQ,2500.005,2550.00,2490.00,2530.00,1234567"])
    quote = parse_bhavcopy_quote(text, symbol="nse-reliance", trade_date=_DAY)
    assert quote is not None
    assert quote.symbol == "RELIANCE"
    assert quote.open == Decimal("2500.01")  # ROUND_HALF_UP to 2dp
    assert quote.volume == 1_234_567
    assert quote.source == "nse"


def test_parse_real_nse_bhavcopy_row() -> None:
    """Golden: parse a BYTE-REAL ``sec_bhavdata_full`` header + row (verified 2026-06-13).

    Locks the parser against the ACTUAL live NSE format: every column after SYMBOL
    has a leading space (`` SERIES``, `` OPEN_PRICE``) and every value is
    leading-space-padded with no thousands commas — sourced from the
    chartiny/nse-sec-bhavdata-full mirror (2024-09-26).
    """
    real_header = (
        "SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, "
        "LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, "
        "NO_OF_TRADES, DELIV_QTY, DELIV_PER"
    )
    real_row = (
        "20MICRONS, EQ, 26-Sep-2024, 310.55, 311.15, 313.80, 301.50, 303.00, "
        "302.80, 304.71, 133716, 407.45, 6260, 68759, 51.42"
    )
    quote = parse_bhavcopy_quote(
        "\n".join([real_header, real_row]), symbol="20MICRONS", trade_date=_DAY
    )
    assert quote is not None
    assert quote.open == Decimal("311.15")
    assert quote.high == Decimal("313.80")
    assert quote.low == Decimal("301.50")
    assert quote.close == Decimal("302.80")
    assert quote.volume == 133716  # leading-space value, no comma


def test_parse_no_header_returns_none() -> None:
    assert parse_bhavcopy_quote("", symbol="RELIANCE", trade_date=_DAY) is None


def test_parse_missing_column_returns_none() -> None:
    text = "SYMBOL,SERIES,OPEN_PRICE\nRELIANCE,EQ,2500"
    assert parse_bhavcopy_quote(text, symbol="RELIANCE", trade_date=_DAY) is None


def test_parse_symbol_not_found_returns_none() -> None:
    text = _bhavcopy(["TCS,EQ,1,2,0.5,1.5,10"])
    assert parse_bhavcopy_quote(text, symbol="RELIANCE", trade_date=_DAY) is None


def test_parse_series_mismatch_returns_none() -> None:
    text = _bhavcopy(["RELIANCE,BE,1,2,0.5,1.5,10"])  # default series is EQ
    assert parse_bhavcopy_quote(text, symbol="RELIANCE", trade_date=_DAY) is None


def test_parse_malformed_volume_returns_none() -> None:
    text = _bhavcopy(["RELIANCE,EQ,1,2,0.5,1.5,notanumber"])
    assert parse_bhavcopy_quote(text, symbol="RELIANCE", trade_date=_DAY) is None


def test_parse_malformed_price_returns_none() -> None:
    text = _bhavcopy(["RELIANCE,EQ,N/A,2,0.5,1.5,10"])
    assert parse_bhavcopy_quote(text, symbol="RELIANCE", trade_date=_DAY) is None


def test_parse_explicit_series_and_spec_args() -> None:
    spec = EquityCsvSpec(
        symbol_column="SYMBOL",
        series_column="SERIES",
        open_column="OPEN_PRICE",
        high_column="HIGH_PRICE",
        low_column="LOW_PRICE",
        close_column="CLOSE_PRICE",
        volume_column="TTL_TRD_QNTY",
    )
    text = _bhavcopy(["RELIANCE,XX,1,2,0.5,1.5,99"])
    quote = parse_bhavcopy_quote(text, symbol="RELIANCE", trade_date=_DAY, series="XX", spec=spec)
    assert quote is not None
    assert quote.volume == 99


# ── quote_from_groww_equity_candles ──────────────────────────────────────────


def test_groww_quote_single_bar_keeps_volume() -> None:
    quote = quote_from_groww_equity_candles([_bar(volume=555)], symbol="RELIANCE", trade_date=_DAY)
    assert quote is not None
    assert quote.volume == 555
    assert quote.source == "groww"


def test_groww_quote_no_bar_returns_none() -> None:
    other = _bar(day=date(2024, 6, 4))
    assert quote_from_groww_equity_candles([other], symbol="RELIANCE", trade_date=_DAY) is None


def test_groww_quote_ambiguous_returns_none() -> None:
    bars = [_bar(hour=4), _bar(hour=5)]
    assert quote_from_groww_equity_candles(bars, symbol="RELIANCE", trade_date=_DAY) is None


# ── EquityQuote validation + projection ──────────────────────────────────────


def test_equity_quote_rejects_non_finite_price() -> None:
    with pytest.raises(ValueError, match="finite Decimal"):
        EquityQuote(
            symbol="X",
            trade_date=_DAY,
            open=Decimal("NaN"),
            high=Decimal("2"),
            low=Decimal("1"),
            close=Decimal("1.5"),
            volume=1,
            source="groww",
        )


def test_equity_quote_rejects_negative_volume() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        EquityQuote(
            symbol="X",
            trade_date=_DAY,
            open=Decimal("1"),
            high=Decimal("2"),
            low=Decimal("1"),
            close=Decimal("1.5"),
            volume=-1,
            source="groww",
        )


def test_as_ohlc_quote_drops_volume() -> None:
    eq = EquityQuote(
        symbol="X",
        trade_date=_DAY,
        open=Decimal("1"),
        high=Decimal("2"),
        low=Decimal("1"),
        close=Decimal("1.5"),
        volume=7,
        source="groww",
    )
    ohlc = eq.as_ohlc_quote()
    assert ohlc.open == Decimal("1")
    assert not hasattr(ohlc, "volume")


# ── VolumeComparison / EquityVerdict to_dict ─────────────────────────────────


def test_volume_comparison_to_dict() -> None:
    vc = VolumeComparison(groww=10, official=8, delta=2, matched=False)
    assert vc.to_dict() == {"groww": 10, "official": 8, "delta": 2, "matched": False}


def _equity_quote(volume: int, *, source: str, close: str = "2530.00") -> EquityQuote:
    return EquityQuote(
        symbol="RELIANCE",
        trade_date=_DAY,
        open=Decimal("2500.00"),
        high=Decimal("2550.00"),
        low=Decimal("2490.00"),
        close=Decimal(close),
        volume=volume,
        source=source,
    )


def test_verdict_to_dict_matched() -> None:
    verdict = compare_equity_quotes(
        _equity_quote(1000, source="groww"), _equity_quote(1000, source="nse")
    )
    d = verdict.to_dict()
    assert d["status"] == "matched"
    assert d["symbol"] == "RELIANCE"
    assert d["trade_date"] == "2024-06-03"
    assert d["volume"] == {"groww": 1000, "official": 1000, "delta": 0, "matched": True}
    assert len(d["fields"]) == 4  # type: ignore[arg-type]
    assert verdict.matched is True
    assert verdict.mismatched_fields == ()


def test_verdict_to_dict_unavailable_has_null_volume() -> None:
    verdict = validate_equity_oneday(
        "RELIANCE",
        _DAY,
        groww_fetcher=lambda _s, _d: [],  # no bars → unavailable
        official_opener=lambda _u: _bhavcopy([]),
    )
    d = verdict.to_dict()
    assert d["status"] == "unavailable"
    assert d["volume"] is None
    assert d["fields"] == []


# ── compare_equity_quotes status matrix ──────────────────────────────────────


def test_compare_all_matched() -> None:
    verdict = compare_equity_quotes(
        _equity_quote(1000, source="groww"), _equity_quote(1000, source="nse")
    )
    assert verdict.status is VerdictStatus.MATCHED
    assert verdict.note is None


def test_compare_volume_mismatch() -> None:
    verdict = compare_equity_quotes(
        _equity_quote(1000, source="groww"), _equity_quote(900, source="nse")
    )
    assert verdict.status is VerdictStatus.MISMATCHED
    assert verdict.volume is not None
    assert verdict.volume.delta == 100
    assert "volume" in (verdict.note or "")


def test_compare_ohlc_mismatch() -> None:
    verdict = compare_equity_quotes(
        _equity_quote(1000, source="groww", close="2530.00"),
        _equity_quote(1000, source="nse", close="2500.00"),
    )
    assert verdict.status is VerdictStatus.MISMATCHED
    assert "OHLC" in (verdict.note or "")
    assert any(not f.matched for f in verdict.mismatched_fields)


def test_compare_volume_within_tolerance() -> None:
    verdict = compare_equity_quotes(
        _equity_quote(1000, source="groww"),
        _equity_quote(950, source="nse"),
        volume_tolerance=100,
    )
    assert verdict.status is VerdictStatus.MATCHED


def test_volume_note_both_axes() -> None:
    """Directly exercise the both-OHLC-and-volume branch of the note builder."""
    verdict = compare_equity_quotes(
        _equity_quote(1000, source="groww", close="2530.00"),
        _equity_quote(900, source="nse", close="2500.00"),
    )
    note = verdict.note or ""
    assert "OHLC" in note
    assert "volume" in note


def test_volume_note_helper_volume_only() -> None:
    """_volume_note with matched OHLC + bad volume names only the volume axis."""
    vc = VolumeComparison(groww=10, official=5, delta=5, matched=False)
    assert "volume" in _volume_note(vc, ())
    assert "OHLC" not in _volume_note(vc, ())


# ── tolerance resolvers ──────────────────────────────────────────────────────


def test_price_tolerance_explicit_wins() -> None:
    assert _resolve_price_tolerance(Decimal("0.5")) == Decimal("0.5")


def test_price_tolerance_env_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PRICE_TOLERANCE_OVERRIDE_ENV, "0.25")
    assert _resolve_price_tolerance(None) == Decimal("0.25")


@pytest.mark.parametrize("bad", ["garbage", "-1", "NaN"])
def test_price_tolerance_env_bad_falls_back(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv(PRICE_TOLERANCE_OVERRIDE_ENV, bad)
    assert _resolve_price_tolerance(None) == Decimal("0.01")


def test_price_tolerance_no_env_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PRICE_TOLERANCE_OVERRIDE_ENV, raising=False)
    assert _resolve_price_tolerance(None) == Decimal("0.01")


def test_volume_tolerance_explicit_wins() -> None:
    assert _resolve_volume_tolerance(50) == 50


def test_volume_tolerance_env_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(VOLUME_TOLERANCE_OVERRIDE_ENV, "100")
    assert _resolve_volume_tolerance(None) == 100


@pytest.mark.parametrize("bad", ["x", "-3"])
def test_volume_tolerance_env_bad_falls_back(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv(VOLUME_TOLERANCE_OVERRIDE_ENV, bad)
    assert _resolve_volume_tolerance(None) == 0


def test_volume_tolerance_no_env_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(VOLUME_TOLERANCE_OVERRIDE_ENV, raising=False)
    assert _resolve_volume_tolerance(None) == 0


# ── validate_equity_oneday orchestration (degrade-safe, injected) ────────────


def _good_bhavcopy() -> str:
    return _bhavcopy(["RELIANCE,EQ,2500.00,2550.00,2490.00,2530.00,1000000"])


def test_validate_happy_matched() -> None:
    verdict = validate_equity_oneday(
        "RELIANCE",
        _DAY,
        groww_fetcher=lambda _s, _d: [_bar(volume=1_000_000)],
        official_opener=lambda _u: _good_bhavcopy(),
    )
    assert verdict.status is VerdictStatus.MATCHED
    assert verdict.volume is not None
    assert verdict.volume.delta == 0


def test_validate_groww_fetch_raises_brutex_error() -> None:
    def boom(_s: str, _d: date) -> Sequence[CandleBar]:
        raise BruteXError("token dead")

    verdict = validate_equity_oneday(
        "RELIANCE", _DAY, groww_fetcher=boom, official_opener=lambda _u: _good_bhavcopy()
    )
    assert verdict.status is VerdictStatus.UNAVAILABLE
    assert "Groww fetch failed" in (verdict.note or "")


def test_validate_no_groww_bar() -> None:
    verdict = validate_equity_oneday(
        "RELIANCE",
        _DAY,
        groww_fetcher=lambda _s, _d: [],
        official_opener=lambda _u: _good_bhavcopy(),
    )
    assert verdict.status is VerdictStatus.UNAVAILABLE
    assert "no Groww 1-day bar" in (verdict.note or "")


def test_validate_ambiguous_groww_bars() -> None:
    verdict = validate_equity_oneday(
        "RELIANCE",
        _DAY,
        groww_fetcher=lambda _s, _d: [_bar(hour=4), _bar(hour=5)],
        official_opener=lambda _u: _good_bhavcopy(),
    )
    assert verdict.status is VerdictStatus.UNAVAILABLE
    assert "ambiguous: 2" in (verdict.note or "")


def test_validate_official_opener_raises() -> None:
    def boom(_u: str) -> str:
        raise OSError("network blocked")

    verdict = validate_equity_oneday(
        "RELIANCE", _DAY, groww_fetcher=lambda _s, _d: [_bar()], official_opener=boom
    )
    assert verdict.status is VerdictStatus.UNAVAILABLE
    assert "official bhavcopy fetch failed" in (verdict.note or "")


def test_validate_official_row_not_found() -> None:
    verdict = validate_equity_oneday(
        "RELIANCE",
        _DAY,
        groww_fetcher=lambda _s, _d: [_bar()],
        official_opener=lambda _u: _bhavcopy(["TCS,EQ,1,2,0.5,1.5,10"]),
    )
    assert verdict.status is VerdictStatus.UNAVAILABLE
    assert "not found" in (verdict.note or "")


def test_validate_passes_through_tolerances_and_mismatches() -> None:
    """Injected tolerances flow through; a real volume gap surfaces as MISMATCHED."""
    verdict = validate_equity_oneday(
        "RELIANCE",
        _DAY,
        price_tolerance=Decimal("0.01"),
        volume_tolerance=0,
        groww_fetcher=lambda _s, _d: [_bar(volume=1_000_500)],
        official_opener=lambda _u: _good_bhavcopy(),
    )
    assert verdict.status is VerdictStatus.MISMATCHED
    assert verdict.volume is not None
    assert verdict.volume.delta == 500
