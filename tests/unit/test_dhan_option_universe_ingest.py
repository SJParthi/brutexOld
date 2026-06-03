"""Tests for `brutex.adapters.dhan.option_universe_ingest` — the rolling fan-out.

100% line+branch via injected fakes (no network, no lake): the strike-window
generator (incl. the 0 and negative-offset edges), the full fan-out cardinality
(strikes × flags × codes × types), the **per-kind expiry set** (index
WEEK+MONTH vs stock MONTH-only, DEC-DHAN-STOCK-MONTHLY-ONLY-001) + the explicit
uniform override, per-series terminal-error quarantine, the report aggregates,
the empty-universe case, and custom fan-out axes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

import pytest
from brutex.adapters.dhan.errors import DhanBadRequestError
from brutex.adapters.dhan.option_universe_ingest import (
    DEFAULT_EXPIRY_CODES,
    INDEX_STRIKE_OFFSETS,
    STOCK_EXPIRY_FLAGS,
    STOCK_STRIKE_OFFSETS,
    OptionsUniverseReport,
    OptionUnderlying,
    run_dhan_options_ingest,
    strike_window,
)
from brutex.data.models import OptionBar

# 1609732800 = 2021-01-04 04:00:00 UTC = 09:30 IST → in session [09:15, 15:30).
_TS_IN = 1609732800


def _payload() -> dict[str, object]:
    """A one-in-session-bar CE side; PE null (skipped) — the rolling shape."""
    return {
        "data": {
            "ce": {
                "timestamp": [_TS_IN],
                "open": [100],
                "high": [110],
                "low": [90],
                "close": [105],
                "volume": [250],
                "oi": [1200],
                "strike": [17500],
                "spot": [17480.5],
                "iv": [0.15],
            },
            "pe": None,
        }
    }


class _OkClient:
    """A JSON poster that returns the same one-bar payload for every call."""

    def __init__(self) -> None:
        self.calls = 0

    def post_json(self, endpoint: str, body: Mapping[str, object]) -> dict[str, object]:
        del endpoint, body
        self.calls += 1
        return _payload()


class _FailClient:
    """A JSON poster that always raises a terminal (non-retryable) Dhan error."""

    def post_json(self, endpoint: str, body: Mapping[str, object]) -> dict[str, object]:
        del endpoint, body
        raise DhanBadRequestError("boom")


class _Sink:
    """Counts appended bars per series (the OptionBarSink seam)."""

    def __init__(self) -> None:
        self.series = 0

    def append_option_bars(
        self,
        *,
        security_id: str,
        exch_id: str,
        underlying_symbol: str,
        exchange_segment: str,
        expiry_flag: str,
        expiry_code: int,
        strike: str,
        drv_option_type: str,
        bars: Sequence[OptionBar],
    ) -> int:
        del security_id, exch_id, underlying_symbol, exchange_segment
        del expiry_flag, expiry_code, strike, drv_option_type
        self.series += 1
        return len(bars)


def _index(security_id: str = "13", offsets: int = INDEX_STRIKE_OFFSETS) -> OptionUnderlying:
    # No expiry_flags passed → the OptionUnderlying default EXPIRY_FLAGS
    # (WEEK+MONTH) — the index set (DEC-DHAN-STOCK-MONTHLY-ONLY-001).
    return OptionUnderlying(
        security_id=security_id,
        exch_id="NSE",
        underlying_symbol="NIFTY",
        exchange_segment="NSE_FNO",
        instrument="OPTIDX",
        strike_offsets=offsets,
    )


def _stock(security_id: str = "1333", offsets: int = STOCK_STRIKE_OFFSETS) -> OptionUnderlying:
    # A single-stock underlying — MONTH-only expiry set (NSE single stocks have
    # no weekly option series; DEC-DHAN-STOCK-MONTHLY-ONLY-001).
    return OptionUnderlying(
        security_id=security_id,
        exch_id="NSE",
        underlying_symbol="HDFCBANK",
        exchange_segment="NSE_FNO",
        instrument="OPTSTK",
        strike_offsets=offsets,
        expiry_flags=STOCK_EXPIRY_FLAGS,
    )


# ── strike_window ────────────────────────────────────────────────────────


def test_strike_window_zero_is_atm_only() -> None:
    assert strike_window(0) == ["ATM"]


def test_strike_window_three_is_seven_ordered() -> None:
    assert strike_window(3) == [
        "ATM",
        "ATM+1",
        "ATM-1",
        "ATM+2",
        "ATM-2",
        "ATM+3",
        "ATM-3",
    ]


def test_strike_window_ten_has_21() -> None:
    assert len(strike_window(INDEX_STRIKE_OFFSETS)) == 21
    assert len(strike_window(STOCK_STRIKE_OFFSETS)) == 7


def test_strike_window_negative_raises() -> None:
    with pytest.raises(ValueError, match="offsets must be >= 0"):
        strike_window(-1)


# ── run_dhan_options_ingest ────────────────────────────────────────────────


def test_full_fanout_cardinality_and_bars() -> None:
    client = _OkClient()
    sink = _Sink()
    report = run_dhan_options_ingest(
        client,
        sink,
        underlyings=[_index(offsets=1)],  # 3 strikes
        start=date(2021, 1, 4),
        end=date(2021, 1, 5),
    )
    # 3 strikes × 2 flags × 3 codes (1,2,3) × 2 types = 36 series.
    expected = 3 * 2 * len(DEFAULT_EXPIRY_CODES) * 2
    assert report.series_count == expected
    assert report.failed_series == 0
    assert client.calls == expected  # one window per series for a 1-day range
    assert report.bars_appended == expected  # one in-session bar per series
    assert report.windows == expected
    assert report.transient_failures == 0


def test_per_kind_expiry_flags_stock_month_only_index_week_month() -> None:
    """A stock fans MONTH-only; an index keeps WEEK+MONTH → the stock count HALVES.

    The per-kind reduction the live-pull-log bug needed: a stock previously
    fanned WEEK+MONTH, but Indian single stocks are monthly-only, so the WEEK
    leg only ever returned empty + burned Dhan quota
    (DEC-DHAN-STOCK-MONTHLY-ONLY-001).
    """
    report = run_dhan_options_ingest(
        _OkClient(),
        _Sink(),
        underlyings=[_index(offsets=1), _stock(offsets=1)],  # identical 3-strike window
        start=date(2021, 1, 4),
        end=date(2021, 1, 5),
    )
    index_series = {k for k in report.series if k[0] == "13"}
    stock_series = {k for k in report.series if k[0] == "1333"}
    # Index: 3 strikes × 2 flags (WEEK+MONTH) × 3 codes × 2 types.
    assert len(index_series) == 3 * 2 * len(DEFAULT_EXPIRY_CODES) * 2
    # Stock: 3 strikes × 1 flag (MONTH) × 3 codes × 2 types — strictly fewer.
    assert len(stock_series) == 3 * 1 * len(DEFAULT_EXPIRY_CODES) * 2
    assert len(stock_series) < len(index_series)
    # No WEEK leg is fanned for the stock (the elided, always-empty series).
    assert {k[1] for k in stock_series} == {"MONTH"}
    assert {k[1] for k in index_series} == {"WEEK", "MONTH"}


def test_explicit_expiry_flags_override_beats_per_kind() -> None:
    """An explicit ``expiry_flags`` argument overrides each underlying's per-kind set."""
    report = run_dhan_options_ingest(
        _OkClient(),
        _Sink(),
        underlyings=[_stock(offsets=0)],  # per-kind set = MONTH-only
        start=date(2021, 1, 4),
        end=date(2021, 1, 5),
        expiry_flags=("WEEK", "MONTH"),  # force BOTH flags onto the stock
        expiry_codes=(1,),
        option_types=("CALL",),
    )
    # The uniform override wins: the stock fans both flags (1 × 2 × 1 × 1).
    assert report.series_count == 2
    assert {k[1] for k in report.series} == {"WEEK", "MONTH"}


def test_terminal_error_quarantines_every_series() -> None:
    report = run_dhan_options_ingest(
        _FailClient(),
        _Sink(),
        underlyings=[_index(offsets=1)],
        start=date(2021, 1, 4),
        end=date(2021, 1, 5),
    )
    expected = 3 * 2 * len(DEFAULT_EXPIRY_CODES) * 2
    assert report.series_count == 0
    assert report.failed_series == expected
    assert report.bars_appended == 0
    # The failure carries the full series coordinates.
    first = report.failures[0]
    assert first.security_id == "13"
    assert first.expiry_flag in {"WEEK", "MONTH"}
    assert first.drv_option_type in {"CALL", "PUT"}
    assert "boom" in first.error


def test_empty_universe_is_empty_report() -> None:
    report = run_dhan_options_ingest(
        _OkClient(), _Sink(), underlyings=[], start=date(2021, 1, 4), end=date(2021, 1, 5)
    )
    assert report.series_count == 0
    assert report.failed_series == 0
    assert report.bars_appended == 0
    assert isinstance(report, OptionsUniverseReport)


def test_custom_axes_narrow_the_fanout() -> None:
    client = _OkClient()
    report = run_dhan_options_ingest(
        client,
        _Sink(),
        underlyings=[_index(offsets=0)],  # ATM only
        start=date(2021, 1, 4),
        end=date(2021, 1, 5),
        expiry_flags=("MONTH",),
        expiry_codes=(0,),
        option_types=("CALL",),
    )
    # 1 strike × 1 flag × 1 code × 1 type = 1 series.
    assert report.series_count == 1
    assert client.calls == 1
    key = ("13", "MONTH", 0, "ATM", "CALL")
    assert key in report.series


def test_on_progress_fires_per_underlying_success() -> None:
    """on_progress fires once per underlying with (id, bars, any_failed=False)."""
    events: list[tuple[str, int, bool]] = []
    run_dhan_options_ingest(
        _OkClient(),
        _Sink(),
        underlyings=[_index("13", offsets=0)],
        start=date(2021, 1, 4),
        end=date(2021, 1, 5),
        expiry_flags=("WEEK",),
        expiry_codes=(1,),
        option_types=("CALL",),
        on_progress=lambda label, bars, failed: events.append((label, bars, failed)),
    )
    # 1 series × 1 in-session CE bar → 1 bar appended, nothing failed.
    assert events == [("13", 1, False)]


def test_skipped_windows_aggregate_after_repull() -> None:
    """A re-pull with the same coverage ledger skips every window (aggregate)."""
    from brutex.data.fetch_coverage import FetchCoverage

    cov = FetchCoverage()
    underlyings = [_index("13", offsets=0)]  # 1 strike → 1 series
    kwargs: dict[str, object] = {
        "underlyings": underlyings,
        "start": date(2021, 1, 4),
        "end": date(2021, 1, 5),  # one window per series
        "expiry_flags": ("WEEK",),
        "expiry_codes": (1,),
        "option_types": ("CALL",),
    }
    first = run_dhan_options_ingest(_OkClient(), _Sink(), coverage=cov, **kwargs)  # type: ignore[arg-type]
    assert first.skipped_windows == 0
    # Re-pull → the one window is already complete → skipped across the aggregate.
    second = run_dhan_options_ingest(_OkClient(), _Sink(), coverage=cov, **kwargs)  # type: ignore[arg-type]
    assert second.skipped_windows == 1
    assert second.bars_appended == 0


def test_on_progress_flags_failed_underlying() -> None:
    """A wholly-failing underlying fires on_progress with (id, 0, True)."""
    events: list[tuple[str, int, bool]] = []
    run_dhan_options_ingest(
        _FailClient(),
        _Sink(),
        underlyings=[_index("13", offsets=0)],
        start=date(2021, 1, 4),
        end=date(2021, 1, 5),
        expiry_flags=("WEEK",),
        expiry_codes=(1,),
        option_types=("CALL",),
        on_progress=lambda label, bars, failed: events.append((label, bars, failed)),
    )
    assert events == [("13", 0, True)]
