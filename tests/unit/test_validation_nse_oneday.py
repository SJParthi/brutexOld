# pyright: reportPrivateUsage=none
"""Unit tests for :mod:`brutex.data.validation.nse_oneday` (DEC-NSE-1DAY-VALIDATION-001).

100% line+branch on the module EXCEPT the two ``# pragma: no cover`` live
fetchers (``_default_groww_fetcher`` / ``_default_official_opener``) — this build
environment network-blocks + bot-blocks NSE/BSE, so the live fetch is
UNVERIFIABLE in CI. Every other path is exercised here with PURE fixtures + an
injected fake Groww fetcher + fake NSE/BSE opener (no network). The IST-vs-UTC
bar-selection trap and the degrade-never-crash boundary are pinned explicitly.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from brutex.calendars import IST
from brutex.data.models import CandleBar
from brutex.data.validation import nse_oneday as m
from brutex.data.validation.nse_oneday import (
    DEFAULT_TOLERANCE_RUPEES_UNVERIFIED,
    BatchReport,
    CsvQuoteSpec,
    IndexSource,
    OhlcQuote,
    OneDayVerdict,
    VerdictStatus,
    compare_quotes,
    official_csv_url,
    parse_official_quote,
    quote_from_groww_candles,
    render_report,
    resolve_index_source,
    validate_brute_force_indices,
    validate_index_oneday,
)
from brutex.models import BruteXError

_DATE = date(2025, 9, 24)


# ────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers (pure — no network)
# ────────────────────────────────────────────────────────────────────────────


def _bar(ts_utc: datetime, open_: str, high: str, low: str, close: str) -> CandleBar:
    """Build a valid index CandleBar (UTC tz, Decimal prices, no volume)."""
    return CandleBar(
        timestamp=ts_utc,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=0,
        open_interest=None,
    )


def _ist_bar(
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int,
    *,
    open_: str,
    high: str,
    low: str,
    close: str,
) -> CandleBar:
    """Build a bar from an IST wall-clock time (stored UTC, like the Groww parser)."""
    ts_utc = datetime(year, month, day, hour, minute, tzinfo=IST).astimezone(UTC)
    return _bar(ts_utc, open_, high, low, close)


def _quote(symbol: str, o: str, h: str, low: str, c: str, *, source: str = "groww") -> OhlcQuote:
    """Build a pre-snapped OhlcQuote for compare tests."""
    return OhlcQuote(
        symbol=symbol,
        trade_date=_DATE,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        source=source,
    )


_NSE_CSV = (
    "Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value\n"
    "Nifty 50,24-09-2025,25000.10,25100.20,24900.05,25050.75\n"
    "Nifty Bank,24-09-2025,54000.50,54250.90,53800.10,54100.25\n"
    "Nifty IT,24-09-2025,38000.00,38100.00,37900.00,38050.00\n"
)


# ────────────────────────────────────────────────────────────────────────────
# OhlcQuote / dataclasses
# ────────────────────────────────────────────────────────────────────────────


class TestOhlcQuote:
    def test_accepts_finite_prices(self) -> None:
        q = _quote("NIFTY", "1.00", "2.00", "0.50", "1.50")
        assert q.open == Decimal("1.00")
        assert q.source == "groww"

    @pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
    def test_rejects_non_finite_price(self, bad: str) -> None:
        with pytest.raises(ValueError, match="must be a finite Decimal"):
            OhlcQuote(
                symbol="NIFTY",
                trade_date=_DATE,
                open=Decimal(bad),
                high=Decimal("2.00"),
                low=Decimal("0.50"),
                close=Decimal("1.50"),
                source="groww",
            )


class TestVerdictProperties:
    def test_matched_property_true_and_false(self) -> None:
        groww = _quote("NIFTY", "100.00", "101.00", "99.00", "100.50")
        matched = compare_quotes(groww, groww)
        assert matched.matched is True
        assert matched.mismatched_fields == ()
        other = _quote("NIFTY", "100.00", "101.00", "99.00", "200.00", source="nse")
        verdict = compare_quotes(groww, other)
        assert verdict.matched is False
        assert {f.field for f in verdict.mismatched_fields} == {"close"}


class TestBatchReportCounts:
    def test_counts_by_status(self) -> None:
        def verdict(status: VerdictStatus) -> OneDayVerdict:
            return OneDayVerdict(
                symbol="X",
                trade_date=_DATE,
                status=status,
                tolerance=DEFAULT_TOLERANCE_RUPEES_UNVERIFIED,
                fields=(),
                groww_source=None,
                official_source=None,
                note=None,
            )

        report = BatchReport(
            trade_date=_DATE,
            verdicts=(
                verdict(VerdictStatus.MATCHED),
                verdict(VerdictStatus.MISMATCHED),
                verdict(VerdictStatus.UNAVAILABLE),
                verdict(VerdictStatus.MATCHED),
            ),
        )
        assert (report.matched, report.mismatched, report.unavailable) == (2, 1, 1)


# ────────────────────────────────────────────────────────────────────────────
# index → source mapping
# ────────────────────────────────────────────────────────────────────────────


class TestResolveIndexSource:
    def test_known_engine_symbols(self) -> None:
        nifty = resolve_index_source("NIFTY")
        assert nifty is not None
        assert nifty.exchange == "NSE"
        assert nifty.groww_symbol == "NSE-NIFTY"
        assert nifty.official_url_template is not None

    def test_groww_form_resolves(self) -> None:
        assert resolve_index_source("NSE-BANKNIFTY") == resolve_index_source("banknifty")

    def test_sensex_has_no_default_url(self) -> None:
        # P0-2: SENSEX is a BSE index — no baked NSE-style source.
        sensex = resolve_index_source("BSE-SENSEX")
        assert sensex is not None
        assert sensex.exchange == "BSE"
        assert sensex.official_url_template is None

    def test_unknown_index_returns_none(self) -> None:
        assert resolve_index_source("NIFTY IT") is None

    def test_override_adds_sensex_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            m.INDEX_SOURCE_OVERRIDE_ENV,
            json.dumps({"SENSEX": "https://bse.example/{ddmmyyyy}.csv"}),
        )
        sensex = resolve_index_source("SENSEX")
        assert sensex is not None
        assert sensex.exchange == "BSE"  # inherited from the built-in default
        assert sensex.official_url_template == "https://bse.example/{ddmmyyyy}.csv"

    def test_override_wins_over_builtin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            m.INDEX_SOURCE_OVERRIDE_ENV,
            json.dumps(
                {
                    "NIFTY": {
                        "official_url_template": "https://x/{iso}.csv",
                        "official_symbol": "NIFTY50",
                    }
                }
            ),
        )
        nifty = resolve_index_source("NIFTY")
        assert nifty is not None
        assert nifty.official_url_template == "https://x/{iso}.csv"
        assert nifty.official_symbol == "NIFTY50"

    def test_override_full_new_symbol(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            m.INDEX_SOURCE_OVERRIDE_ENV,
            json.dumps(
                {
                    "FINNIFTY": {
                        "exchange": "NSE",
                        "groww_symbol": "NSE-FINNIFTY",
                        "official_symbol": "Nifty Fin Service",
                        "official_url_template": "https://x/{ddmmyyyy}.csv",
                    }
                }
            ),
        )
        fin = resolve_index_source("FINNIFTY")
        assert fin is not None
        assert fin.groww_symbol == "NSE-FINNIFTY"


class TestOverrideSourcesParsing:
    def test_absent_env_is_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(m.INDEX_SOURCE_OVERRIDE_ENV, raising=False)
        assert m._override_sources() == {}

    def test_empty_env_is_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(m.INDEX_SOURCE_OVERRIDE_ENV, "")
        assert m._override_sources() == {}

    def test_malformed_json_is_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(m.INDEX_SOURCE_OVERRIDE_ENV, "not json {{{")
        assert m._override_sources() == {}

    def test_non_object_json_is_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(m.INDEX_SOURCE_OVERRIDE_ENV, json.dumps([1, 2, 3]))
        assert m._override_sources() == {}

    def test_bad_entries_dropped_good_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            m.INDEX_SOURCE_OVERRIDE_ENV,
            json.dumps(
                {
                    "NIFTY": "https://ok/{ddmmyyyy}.csv",  # good (string)
                    "  ": "https://blank-key/{ddmmyyyy}.csv",  # blank engine → dropped
                    "BANKNIFTY": 123,  # non-str/non-dict value → dropped
                }
            ),
        )
        out = m._override_sources()
        assert set(out) == {"NIFTY"}

    def test_non_string_key_dropped(self) -> None:
        # _parse_source_entry guards a non-string key (json keys are always str,
        # but the pure function is total over any input).
        assert m._parse_source_entry(123, "x") is None


class TestOfficialCsvUrl:
    def test_none_template_returns_none(self) -> None:
        source = resolve_index_source("SENSEX")
        assert source is not None
        assert official_csv_url(source, _DATE) is None

    def test_all_placeholders_substituted(self) -> None:
        source = IndexSource(
            engine_symbol="X",
            exchange="NSE",
            groww_symbol="NSE-X",
            official_symbol="X",
            official_url_template="d={ddmmyyyy} y={yyyymmdd} i={iso}",
        )
        assert official_csv_url(source, _DATE) == "d=24092025 y=20250924 i=2025-09-24"


class TestOverrideStrHelpers:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(" foo ", "foo"), ("", "DEF"), ("   ", "DEF"), (123, "DEF"), (None, "DEF")],
    )
    def test_override_str(self, value: object, expected: str) -> None:
        assert m._override_str(value, "DEF") == expected

    def test_override_optional_str_default_none(self) -> None:
        assert m._override_optional_str(None, None) is None
        assert m._override_optional_str("x", None) == "x"


# ────────────────────────────────────────────────────────────────────────────
# official CSV parsing
# ────────────────────────────────────────────────────────────────────────────


class TestCsvQuoteSpec:
    def test_no_env_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(m.CSV_QUOTE_SPEC_OVERRIDE_ENV, raising=False)
        assert m._csv_quote_spec() == m.DEFAULT_CSV_QUOTE_SPEC

    def test_malformed_json_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(m.CSV_QUOTE_SPEC_OVERRIDE_ENV, "{{ bad")
        assert m._csv_quote_spec() == m.DEFAULT_CSV_QUOTE_SPEC

    def test_non_object_json_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(m.CSV_QUOTE_SPEC_OVERRIDE_ENV, json.dumps("scalar"))
        assert m._csv_quote_spec() == m.DEFAULT_CSV_QUOTE_SPEC

    def test_partial_override_merges(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(m.CSV_QUOTE_SPEC_OVERRIDE_ENV, json.dumps({"close_column": "CLOSE"}))
        spec = m._csv_quote_spec()
        assert spec.close_column == "CLOSE"
        assert spec.open_column == m.DEFAULT_CSV_QUOTE_SPEC.open_column


class TestParseOfficialQuote:
    def _source(self, symbol: str = "NIFTY") -> IndexSource:
        source = resolve_index_source(symbol)
        assert source is not None
        return source

    def test_happy_path_nifty(self) -> None:
        q = parse_official_quote(_NSE_CSV, source=self._source(), trade_date=_DATE)
        assert q is not None
        assert (q.open, q.high, q.low, q.close) == (
            Decimal("25000.10"),
            Decimal("25100.20"),
            Decimal("24900.05"),
            Decimal("25050.75"),
        )
        assert q.source == "nse"

    def test_case_insensitive_row_match(self) -> None:
        source = IndexSource("NIFTY", "NSE", "NSE-NIFTY", "nifty 50", None)
        q = parse_official_quote(_NSE_CSV, source=source, trade_date=_DATE)
        assert q is not None
        assert q.close == Decimal("25050.75")

    def test_header_whitespace_case_drift_tolerated(self) -> None:
        csv_text = (
            " index name , OPEN INDEX VALUE ,High Index Value,low index value,Closing Index Value\n"
            "Nifty 50,100.00,200.00,50.00,150.00\n"
        )
        q = parse_official_quote(csv_text, source=self._source(), trade_date=_DATE)
        assert q is not None
        assert q.open == Decimal("100.00")

    def test_empty_text_returns_none(self) -> None:
        assert parse_official_quote("", source=self._source(), trade_date=_DATE) is None

    def test_missing_column_returns_none(self) -> None:
        csv_text = "Index Name,Open Index Value\nNifty 50,100.00\n"  # no high/low/close
        assert parse_official_quote(csv_text, source=self._source(), trade_date=_DATE) is None

    def test_row_absent_returns_none(self) -> None:
        source = self._source("SENSEX")  # "SENSEX" row not present in the NSE fixture
        assert parse_official_quote(_NSE_CSV, source=source, trade_date=_DATE) is None

    def test_malformed_price_in_matched_row_returns_none(self) -> None:
        csv_text = (
            "Index Name,Open Index Value,High Index Value,Low Index Value,Closing Index Value\n"
            "Nifty 50,not-a-number,200.00,50.00,150.00\n"
        )
        assert parse_official_quote(csv_text, source=self._source(), trade_date=_DATE) is None

    def test_explicit_spec_overrides_columns(self) -> None:
        csv_text = "name,o,h,l,c\nNifty 50,1.00,2.00,0.50,1.50\n"
        spec = CsvQuoteSpec("name", "o", "h", "l", "c")
        q = parse_official_quote(
            _NSE_CSV[:0] + csv_text, source=self._source(), trade_date=_DATE, spec=spec
        )
        assert q is not None
        assert q.high == Decimal("2.00")


# ────────────────────────────────────────────────────────────────────────────
# Groww candles → quote (IST date selection)
# ────────────────────────────────────────────────────────────────────────────


class TestQuoteFromGrowwCandles:
    def test_single_bar_snapped(self) -> None:
        bar = _ist_bar(
            2025,
            9,
            24,
            9,
            15,
            open_="14020.7999",
            high="14100.00",
            low="13900.00",
            close="14050.10",
        )
        q = quote_from_groww_candles([bar], symbol="NIFTY", trade_date=_DATE)
        assert q is not None
        # 2-dp snap (DEC-SPOT-PRICE-2DP-001) — vendor float-noise removed.
        assert q.open == Decimal("14020.80")
        assert q.close == Decimal("14050.10")

    def test_midnight_ist_bar_selected_by_ist_date_not_utc(self) -> None:
        # P0-3: an IST-midnight daily bar is stored on the PREVIOUS UTC date.
        bar = _ist_bar(
            2025, 9, 24, 0, 0, open_="100.00", high="200.00", low="50.00", close="150.00"
        )
        assert bar.timestamp.astimezone(UTC).date() == date(2025, 9, 23)  # stored UTC = prior day
        q = quote_from_groww_candles([bar], symbol="NIFTY", trade_date=_DATE)
        assert q is not None  # selected by IST date 2025-09-24, not the UTC 2025-09-23

    def test_no_matching_bar_returns_none(self) -> None:
        bar = _ist_bar(2025, 9, 23, 9, 15, open_="1.00", high="2.00", low="0.50", close="1.50")
        assert quote_from_groww_candles([bar], symbol="NIFTY", trade_date=_DATE) is None
        assert quote_from_groww_candles([], symbol="NIFTY", trade_date=_DATE) is None

    def test_ambiguous_multiple_bars_returns_none(self) -> None:
        b1 = _ist_bar(2025, 9, 24, 0, 0, open_="1.00", high="2.00", low="0.50", close="1.50")
        b2 = _ist_bar(2025, 9, 24, 9, 15, open_="1.00", high="2.00", low="0.50", close="1.50")
        assert quote_from_groww_candles([b1, b2], symbol="NIFTY", trade_date=_DATE) is None


# ────────────────────────────────────────────────────────────────────────────
# compare → verdict
# ────────────────────────────────────────────────────────────────────────────


class TestCompareQuotes:
    def test_all_matched(self) -> None:
        groww = _quote("NIFTY", "100.00", "101.00", "99.00", "100.50")
        official = _quote("NIFTY", "100.00", "101.00", "99.00", "100.50", source="nse")
        verdict = compare_quotes(groww, official)
        assert verdict.status is VerdictStatus.MATCHED
        assert verdict.note is None
        assert all(f.delta_paisa == 0 for f in verdict.fields)
        assert verdict.groww_source == "groww"
        assert verdict.official_source == "nse"

    def test_float_noise_matches_after_snap(self) -> None:
        groww = _quote("NIFTY", "14020.80", "14100.00", "13900.00", "14050.10")
        official = _quote("NIFTY", "14020.80", "14100.00", "13900.00", "14050.10", source="nse")
        assert compare_quotes(groww, official).status is VerdictStatus.MATCHED

    def test_mismatch_reports_signed_paisa_and_note(self) -> None:
        groww = _quote("NIFTY", "100.10", "101.00", "99.00", "100.50")
        official = _quote("NIFTY", "100.00", "101.00", "99.00", "100.50", source="nse")
        verdict = compare_quotes(groww, official)
        assert verdict.status is VerdictStatus.MISMATCHED
        open_field = next(f for f in verdict.fields if f.field == "open")
        assert open_field.delta == Decimal("0.10")
        assert open_field.delta_paisa == 10
        assert verdict.note is not None and "open" in verdict.note and "UNVERIFIED" in verdict.note

    def test_negative_delta_paisa(self) -> None:
        groww = _quote("NIFTY", "99.90", "101.00", "99.00", "100.50")
        official = _quote("NIFTY", "100.00", "101.00", "99.00", "100.50", source="nse")
        open_field = next(f for f in compare_quotes(groww, official).fields if f.field == "open")
        assert open_field.delta_paisa == -10

    def test_tolerance_boundary_inclusive(self) -> None:
        groww = _quote("NIFTY", "100.05", "101.00", "99.00", "100.50")
        official = _quote("NIFTY", "100.00", "101.00", "99.00", "100.50", source="nse")
        # |Δ| == tolerance → MATCHED (inclusive).
        assert compare_quotes(groww, official, tolerance=Decimal("0.05")).matched is True
        # just over → MISMATCHED.
        assert compare_quotes(groww, official, tolerance=Decimal("0.04")).matched is False


class TestResolveTolerance:
    def test_explicit_wins(self) -> None:
        assert m._resolve_tolerance(Decimal("0.5")) == Decimal("0.5")

    def test_env_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(m.TOLERANCE_OVERRIDE_ENV, "0.25")
        assert m._resolve_tolerance(None) == Decimal("0.25")

    def test_env_malformed_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(m.TOLERANCE_OVERRIDE_ENV, "abc")
        assert m._resolve_tolerance(None) == DEFAULT_TOLERANCE_RUPEES_UNVERIFIED

    @pytest.mark.parametrize("raw", ["-1", "NaN", "Infinity"])
    def test_env_non_finite_or_negative_falls_back(
        self, raw: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(m.TOLERANCE_OVERRIDE_ENV, raw)
        assert m._resolve_tolerance(None) == DEFAULT_TOLERANCE_RUPEES_UNVERIFIED

    def test_env_absent_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(m.TOLERANCE_OVERRIDE_ENV, raising=False)
        assert m._resolve_tolerance(None) == DEFAULT_TOLERANCE_RUPEES_UNVERIFIED


class TestRenderReport:
    def test_matched_and_unavailable_rendered(self) -> None:
        groww = _quote("NIFTY", "100.00", "101.00", "99.00", "100.50")
        matched = compare_quotes(groww, groww)
        unavailable = m._unavailable("SENSEX", _DATE, DEFAULT_TOLERANCE_RUPEES_UNVERIFIED, "no url")
        text = render_report(BatchReport(_DATE, (matched, unavailable)))
        assert "matched=1 mismatched=0 unavailable=1" in text
        assert "[MATCHED] NIFTY" in text
        assert "open" in text and "[ok]" in text
        assert "[UNAVAILABLE] SENSEX" in text
        assert "note: no url" in text

    def test_mismatch_flag_rendered(self) -> None:
        groww = _quote("NIFTY", "100.10", "101.00", "99.00", "100.50")
        official = _quote("NIFTY", "100.00", "101.00", "99.00", "100.50", source="nse")
        text = render_report(BatchReport(_DATE, (compare_quotes(groww, official),)))
        assert "[DIFF]" in text
        assert "(+10p)" in text


class TestVerdictToDict:
    """``FieldComparison.to_dict`` + ``OneDayVerdict.to_dict`` — the JSON wire shape.

    Backs the dashboard ``verify-nse`` route (DEC-NSE-VERIFY-ROUTE-001): Decimals
    serialise as exact 2-dp strings (never floats), ``delta_paisa`` stays an int,
    the whole payload round-trips through ``json``.
    """

    def test_mismatched_verdict_to_dict_is_json_safe(self) -> None:
        groww = _quote("NIFTY", "100.10", "101.00", "99.00", "100.50")
        official = _quote("NIFTY", "100.00", "101.00", "99.00", "100.50", source="nse")
        verdict = compare_quotes(groww, official)
        payload = verdict.to_dict()
        # The whole verdict survives a JSON round-trip (Decimals are strings).
        assert json.loads(json.dumps(payload)) == payload
        assert payload["symbol"] == "NIFTY"
        assert payload["trade_date"] == _DATE.isoformat()
        assert payload["status"] == "mismatched"
        assert payload["tolerance"] == str(DEFAULT_TOLERANCE_RUPEES_UNVERIFIED)
        assert payload["groww_source"] == "groww"
        assert payload["official_source"] == "nse"
        assert payload["note"] is not None  # a mismatch carries an honest reason
        fields = payload["fields"]
        assert isinstance(fields, list)
        open_field = next(f for f in fields if f["field"] == "open")
        assert open_field["groww"] == "100.10"
        assert open_field["official"] == "100.00"
        assert open_field["delta"] == "0.10"
        assert open_field["delta_paisa"] == 10
        assert open_field["matched"] is False

    def test_unavailable_verdict_to_dict_has_empty_fields(self) -> None:
        verdict = m._unavailable("SENSEX", _DATE, DEFAULT_TOLERANCE_RUPEES_UNVERIFIED, "no url")
        payload = verdict.to_dict()
        assert payload["status"] == "unavailable"
        assert payload["fields"] == []
        assert payload["note"] == "no url"
        assert payload["groww_source"] is None
        assert json.loads(json.dumps(payload)) == payload


# ────────────────────────────────────────────────────────────────────────────
# live entrypoints (injected fakes — degrade-never-crash)
# ────────────────────────────────────────────────────────────────────────────


# Groww OHLC that EXACTLY matches the official ``_NSE_CSV`` rows, per symbol, so
# an injected happy-path fetch yields a MATCHED verdict for NIFTY + BANKNIFTY.
_GROWW_BY_SYMBOL: dict[str, tuple[str, str, str, str]] = {
    "NIFTY": ("25000.10", "25100.20", "24900.05", "25050.75"),
    "BANKNIFTY": ("54000.50", "54250.90", "53800.10", "54100.25"),
}


def _ok_groww(source: IndexSource, _trade_date: date) -> list[CandleBar]:
    open_, high, low, close = _GROWW_BY_SYMBOL.get(source.engine_symbol, _GROWW_BY_SYMBOL["NIFTY"])
    return [_ist_bar(2025, 9, 24, 9, 15, open_=open_, high=high, low=low, close=close)]


def _ok_opener(_url: str) -> str:
    return _NSE_CSV


class TestValidateIndexOneday:
    def test_matched(self) -> None:
        verdict = validate_index_oneday(
            "NIFTY", _DATE, groww_fetcher=_ok_groww, official_opener=_ok_opener
        )
        assert verdict.status is VerdictStatus.MATCHED

    def test_mismatch(self) -> None:
        def hi_groww(_s: IndexSource, _d: date) -> list[CandleBar]:
            return [
                _ist_bar(
                    2025,
                    9,
                    24,
                    9,
                    15,
                    open_="25001.00",
                    high="25100.20",
                    low="24900.05",
                    close="25050.75",
                )
            ]

        verdict = validate_index_oneday(
            "NIFTY", _DATE, groww_fetcher=hi_groww, official_opener=_ok_opener
        )
        assert verdict.status is VerdictStatus.MISMATCHED

    def test_unknown_index_unavailable(self) -> None:
        verdict = validate_index_oneday(
            "NIFTY IT", _DATE, groww_fetcher=_ok_groww, official_opener=_ok_opener
        )
        assert verdict.status is VerdictStatus.UNAVAILABLE
        assert verdict.note is not None and "unknown index" in verdict.note

    def test_no_url_unavailable_sensex(self) -> None:
        verdict = validate_index_oneday(
            "SENSEX", _DATE, groww_fetcher=_ok_groww, official_opener=_ok_opener
        )
        assert verdict.status is VerdictStatus.UNAVAILABLE
        assert verdict.note is not None and "no official source URL" in verdict.note
        assert verdict.official_source == "bse"

    @pytest.mark.parametrize(
        "exc", [OSError("blocked"), BruteXError("groww down"), ValueError("x")]
    )
    def test_groww_fetch_error_degrades(self, exc: Exception) -> None:
        def boom(_s: IndexSource, _d: date) -> list[CandleBar]:
            raise exc

        verdict = validate_index_oneday(
            "NIFTY", _DATE, groww_fetcher=boom, official_opener=_ok_opener
        )
        assert verdict.status is VerdictStatus.UNAVAILABLE
        assert verdict.note is not None and "Groww fetch failed" in verdict.note

    def test_groww_no_bar_unavailable(self) -> None:
        verdict = validate_index_oneday(
            "NIFTY", _DATE, groww_fetcher=lambda _s, _d: [], official_opener=_ok_opener
        )
        assert verdict.status is VerdictStatus.UNAVAILABLE
        assert verdict.note is not None and "no Groww 1-day bar" in verdict.note

    def test_groww_ambiguous_bars_unavailable(self) -> None:
        def two_bars(_s: IndexSource, _d: date) -> list[CandleBar]:
            return [
                _ist_bar(2025, 9, 24, 0, 0, open_="1.00", high="2.00", low="0.50", close="1.50"),
                _ist_bar(2025, 9, 24, 9, 15, open_="1.00", high="2.00", low="0.50", close="1.50"),
            ]

        verdict = validate_index_oneday(
            "NIFTY", _DATE, groww_fetcher=two_bars, official_opener=_ok_opener
        )
        assert verdict.status is VerdictStatus.UNAVAILABLE
        assert verdict.note is not None and "ambiguous: 2" in verdict.note

    @pytest.mark.parametrize("exc", [OSError("403"), ValueError("decode")])
    def test_official_fetch_error_degrades(self, exc: Exception) -> None:
        def boom(_url: str) -> str:
            raise exc

        verdict = validate_index_oneday(
            "NIFTY", _DATE, groww_fetcher=_ok_groww, official_opener=boom
        )
        assert verdict.status is VerdictStatus.UNAVAILABLE
        assert verdict.note is not None and "official fetch failed" in verdict.note
        assert verdict.groww_source == "groww"

    def test_official_parse_none_unavailable(self) -> None:
        verdict = validate_index_oneday(
            "NIFTY",
            _DATE,
            groww_fetcher=_ok_groww,
            official_opener=lambda _url: "garbage,body\n1,2\n",
        )
        assert verdict.status is VerdictStatus.UNAVAILABLE
        assert verdict.note is not None and "not found in source" in verdict.note

    def test_uses_default_openers_when_not_injected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Covers the ``else _default_*`` arcs without touching the network: patch
        # the module-global default fetchers, then call WITHOUT injecting.
        monkeypatch.setattr(m, "_default_groww_fetcher", _ok_groww)
        monkeypatch.setattr(m, "_default_official_opener", _ok_opener)
        verdict = validate_index_oneday("NIFTY", _DATE)
        assert verdict.status is VerdictStatus.MATCHED


class TestValidateBruteForceIndices:
    def test_default_three_symbols(self) -> None:
        report = validate_brute_force_indices(
            _DATE, groww_fetcher=_ok_groww, official_opener=_ok_opener
        )
        assert len(report.verdicts) == 3
        # NIFTY + BANKNIFTY matched (the CSV carries both); SENSEX has no URL.
        assert report.unavailable == 1
        assert {v.symbol for v in report.verdicts} == {"NIFTY", "BANKNIFTY", "SENSEX"}

    def test_custom_symbols(self) -> None:
        report = validate_brute_force_indices(
            _DATE, symbols=["NIFTY"], groww_fetcher=_ok_groww, official_opener=_ok_opener
        )
        assert len(report.verdicts) == 1
        assert report.matched == 1

    def test_one_blocked_source_does_not_sink_batch(self) -> None:
        calls = {"n": 0}

        def flaky(_url: str) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("blocked")
            return _NSE_CSV

        report = validate_brute_force_indices(
            _DATE, symbols=["NIFTY", "BANKNIFTY"], groww_fetcher=_ok_groww, official_opener=flaky
        )
        # First index degrades; second still produces a real verdict.
        assert report.unavailable == 1
        assert report.matched == 1


def test_module_exports_are_public() -> None:
    for name in m.__all__:
        assert hasattr(m, name)
