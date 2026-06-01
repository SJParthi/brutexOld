"""Tests for `brutex.adapters.dhan.option_universe_ingest` — the rolling fan-out.

100% line+branch via injected fakes (no network, no lake): the strike-window
generator (incl. the 0 and negative-offset edges), the full fan-out cardinality
(strikes × flags × codes × types), per-series terminal-error quarantine, the
report aggregates, the empty-universe case, and custom fan-out axes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

import pytest
from brutex.adapters.dhan.errors import DhanBadRequestError
from brutex.adapters.dhan.option_universe_ingest import (
    DEFAULT_EXPIRY_CODES,
    INDEX_STRIKE_OFFSETS,
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
        exchange_segment: str,
        expiry_flag: str,
        expiry_code: int,
        strike: str,
        drv_option_type: str,
        bars: Sequence[OptionBar],
    ) -> int:
        del security_id, exchange_segment, expiry_flag, expiry_code, strike, drv_option_type
        self.series += 1
        return len(bars)


def _index(security_id: str = "13", offsets: int = INDEX_STRIKE_OFFSETS) -> OptionUnderlying:
    return OptionUnderlying(
        security_id=security_id,
        exchange_segment="NSE_FNO",
        instrument="OPTIDX",
        strike_offsets=offsets,
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
