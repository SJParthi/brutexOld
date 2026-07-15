"""Unit tests for :mod:`brutex.dashboard.pull_controller` (Groww model).

Offline only — exercises the dry-run synthetic path (no creds / network)
and the injected-factory path. The live Groww builder
(:func:`_build_live_pull_adapter`) is ``# pragma: no cover`` because it needs
a real TOTP token + the Groww SDK, both absent in CI (DEC-VENDOR-PIVOT-001).

PR-J4b vendor cutover: the SPOT pull now runs on the Groww spine
(``run_groww_spot_ingest`` → ``market="groww"`` ledger rows), which — unlike the
retired Dhan spine — has NO ``DH-905`` known-gap concept (Golden Rule #1: no
invented twin). A terminal Groww error is a uniform quarantine → ``failed``; a
clean 0-bar fetch is the only ``no_data`` source (recorded ``EMPTY``, never a
settled ``SKIPPED``). The token-lifecycle + options seams stay on ``dhan`` (J4c).
"""

# Whitebox test of the module-private `_maybe_renew_adapter_token` (sanctioned per pyproject.toml).
# pyright: reportPrivateUsage=none

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import cast

import pytest
from brutex.adapters.groww.adapter import GrowwMarketAdapter
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.constants import INSTRUMENT_CSV_COLUMNS
from brutex.adapters.groww.errors import GrowwAuthError, GrowwBadRequestError
from brutex.adapters.groww.fetch_pipeline import RawFetchResult
from brutex.adapters.groww.instruments import load_index_from_csv
from brutex.adapters.groww.vendor_floor import VendorFloorLearner, vendor_floor_key
from brutex.adapters.ingest_common import (
    SETTLED_SKIPPED_OPERATOR_THRESHOLD,
    SyntheticSpotAdapter,
    synthetic_spot_bars,
)
from brutex.adapters.protocol import MarketAdapter
from brutex.dashboard import pull_controller
from brutex.dashboard.pull_controller import (
    GROWW_FETCH_INTERVALS,
    GROWW_HISTORY_FLOOR,
    BulkAdapterFactory,
    BulkSpotReport,
    PullCandlesReport,
    SessionRateController,
    TimeframeResult,
    _emit_pull_pool_size,
    default_date_range,
    make_live_sync,
    parse_symbol_list,
    pull_all_spot,
    pull_candles,
    register_pull_meter,
    resolve_candle_range,
    resolve_fetch_intervals,
    resolve_pull_symbols,
    resolve_pull_targets,
)
from brutex.data.instrument_store import open_instrument_store
from brutex.data.models import CandleBar
from brutex.data.sink import BarSink, NullBarSink
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.orchestrator.quota_tracker import DailyQuotaExceeded
from structlog.testing import capture_logs


def _index(symbol: str = "13") -> Instrument:
    """Build a minimal spot index :class:`Instrument` keyed by *symbol* (``symbol_name``)."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=1,
        symbol_name=symbol,
        display_name=None,
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


#: Both fetch intervals — passed EXPLICITLY by the machinery tests that exercise
#: per-(symbol, interval) mechanics (per-pair progress, mixed 1m-lands/1d-gap
#: outcomes, both-interval failure/quarantine). The PRODUCTION default
#: (``GROWW_FETCH_INTERVALS``) is **1-minute only** (DEC-1MINUTE-ONLY-DERIVE-1DAY-001:
#: 1-minute is the ONLY granularity fetched; 1-day is derived from it). These tests opt
#: INTO the two-interval ("both") shape so the multi-interval machinery stays covered
#: while the default-behaviour tests assert the 1-minute-only default.
_BOTH_INTERVALS: tuple[str, ...] = ("1minute", "1day")


class TestResolveFetchIntervals:
    """The spot fetch-interval resolver — ALWAYS 1-minute (DEC-1MINUTE-ONLY-DERIVE-1DAY-001)."""

    def test_default_is_one_minute_only(self) -> None:
        # 1-minute is the only granularity fetched; 1-day is derived from it.
        assert resolve_fetch_intervals(None) == ("1minute",)
        assert GROWW_FETCH_INTERVALS == ("1minute",)

    def test_minute_choice(self) -> None:
        assert resolve_fetch_intervals("1minute") == ("1minute",)

    def test_legacy_day_choice_is_ignored(self) -> None:
        # The legacy ``1day`` token is IGNORED — the dropdown is retired; always 1-minute.
        assert resolve_fetch_intervals("1day") == ("1minute",)

    def test_legacy_both_choice_is_ignored(self) -> None:
        assert resolve_fetch_intervals("both") == ("1minute",)

    def test_any_value_resolves_to_one_minute(self) -> None:
        # Whitespace / unknown / blank — every input resolves to 1-minute only.
        assert resolve_fetch_intervals("  both  ") == ("1minute",)
        assert resolve_fetch_intervals("5minute") == ("1minute",)
        assert resolve_fetch_intervals("") == ("1minute",)


class _EmptyAdapter(SyntheticSpotAdapter):
    """A :class:`MarketAdapter` whose every fetch returns no bars (NO_DATA).

    Drives the per-interval ``no_data`` branch — the vendor ran cleanly but
    returned an empty candle set, so ``bars_appended == 0`` and the UI shows
    "no data" rather than a green ✓.
    """

    def fetch_ohlcv(
        self, *, symbol: str, exchange: Exchange, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        """Return an empty list — the NO_DATA outcome for every window."""
        del symbol, exchange, interval, start, end
        return []


class _FailingAdapter(SyntheticSpotAdapter):
    """A :class:`MarketAdapter` whose every fetch raises a terminal Groww error.

    ``GrowwBadRequestError`` is non-retryable (``is_retryable`` → False), so
    :func:`ingest_spot` re-raises and :func:`run_groww_spot_ingest` quarantines
    the ``(symbol, interval)`` pair into ``failures`` → the ``failed`` branch.
    """

    def fetch_ohlcv(
        self, *, symbol: str, exchange: Exchange, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        """Always raise a terminal Groww error — drives the failure branch."""
        del symbol, exchange, start, end
        raise GrowwBadRequestError(f"boom for {interval}")


class _DailyGapAdapter(SyntheticSpotAdapter):
    """The EXACT operator case: ``1minute`` returns bars, ``1day`` returns NO bars.

    Mirrors NIFTY NEXT 50 (daily history starts after 2021-01-01). Under the Groww
    spine there is no ``DH-905`` known-gap marker (Golden Rule #1): the vendor's
    "no data for this range" answer is simply a clean 0-bar fetch, recorded
    ``EMPTY``. The symbol must finish ``completed=1`` (1m) + ``no_data=1`` (1d
    empty) → verdict ``clean``, never ``transient_residual`` (the 1day miss is a
    settled empty, not a quarantined failure).
    """

    def fetch_ohlcv(
        self, *, symbol: str, exchange: Exchange, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        """Return bars for ``1minute``; return an empty list for the ``1day`` series."""
        if interval == "1day":
            return []
        return super().fetch_ohlcv(
            symbol=symbol, exchange=exchange, interval=interval, start=start, end=end
        )


def _real_sink(lake_root: Path) -> BarSink:
    """A real catalog-free ``ParquetBarSink`` (the dashboard/spot-pull reality).

    The machinery tests that exercise BOTH intervals use this real sink so the
    landed bars are written to disk and the window ledger reflects the planned +
    fetched windows (DEC-SPOT-INTERVAL-DECOUPLE-001 — both phases pull the full range
    independently; the minute phase no longer reads the lake for a 1-day anchor).
    """
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    return ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))


def _real_bulk_factory(
    adapter: MarketAdapter,
) -> BulkAdapterFactory:
    """Return a bulk ``(adapter, real-sink)`` factory over *adapter* (Phase-1 lake writes)."""

    def _factory(lake_root: Path, _insts: Sequence[Instrument]) -> tuple[MarketAdapter, BarSink]:
        return adapter, _real_sink(lake_root)

    return _factory


# ────────────────────────────────────────────────────────────────────────
# Dataclass surface
# ────────────────────────────────────────────────────────────────────────


class TestPullCandlesReport:
    def test_totals_and_to_dict(self) -> None:
        report = PullCandlesReport(
            symbol="13",
            year=2026,
            dry_run=True,
            timeframes=[
                TimeframeResult(interval="1m", completed=3, failed=0, no_data=1, skipped=2),
                TimeframeResult(interval="1d", completed=2, failed=1, no_data=4, skipped=5),
            ],
        )
        assert report.total_completed == 5
        assert report.total_failed == 1
        assert report.total_no_data == 5
        assert report.total_skipped == 7
        d = report.to_dict()
        assert d["symbol"] == "13"
        assert d["year"] == 2026
        assert d["dry_run"] is True
        assert d["total_completed"] == 5
        assert d["total_failed"] == 1
        assert d["total_no_data"] == 5
        assert d["total_skipped"] == 7
        assert d["timeframes"][1] == {
            "interval": "1d",
            "completed": 2,
            "failed": 1,
            "no_data": 4,
            "skipped": 5,
        }

    def test_empty_timeframes_default(self) -> None:
        report = PullCandlesReport(symbol="X", year=2026, dry_run=True)
        assert report.timeframes == []
        assert report.total_completed == 0
        assert report.total_failed == 0
        assert report.total_no_data == 0
        assert report.total_skipped == 0


# ────────────────────────────────────────────────────────────────────────
# pull_candles — dry-run (default factory exercises ``_build_dry_run_adapter``)
# ────────────────────────────────────────────────────────────────────────


class TestPullCandlesDryRun:
    def test_default_no_dates_no_year_pulls_full_history(self, tmp_path: Path) -> None:
        # No dates, no year → the FULL Track-1 history (2020-01-01 → previous
        # day), so the report's start year is the 2020 floor, NOT the current
        # year (FIX 4 — blank pickers default to the full history floor).
        report = pull_candles(_index(), lake_root=tmp_path, dry_run=True)
        assert report.year == GROWW_HISTORY_FLOOR.year
        assert report.start_date == GROWW_HISTORY_FLOOR.isoformat()
        assert report.dry_run is True
        assert report.symbol == "13"

    def test_pull_candles_drains_fetch_pipeline_before_write_consumer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MAJOR-4 regression (pull_candles twin): pipeline drained BEFORE the write consumer."""
        order: list[str] = []
        monkeypatch.setattr(
            "brutex.dashboard.pull_controller._drain_fetch_pipeline",
            lambda _p: order.append("fetch_pipeline"),
        )
        monkeypatch.setattr(
            "brutex.dashboard.pull_controller._drain_write_consumer",
            lambda _c: order.append("write_consumer"),
        )
        pull_candles(_index(), lake_root=tmp_path, year=2024, dry_run=True)
        assert order == ["fetch_pipeline", "write_consumer"]

    def test_covers_fetched_intervals(self, tmp_path: Path) -> None:
        report = pull_candles(_index(), lake_root=tmp_path, year=2024, dry_run=True)
        # The PRODUCTION default fetches 1-minute ONLY (DEC-1MINUTE-ONLY-DERIVE-1DAY-001);
        # 1-day + the 19 intraday TFs are derived from it — so the grid shows the single
        # 1m cell (not two).
        assert [tf.interval for tf in report.timeframes] == ["1m"]
        assert GROWW_FETCH_INTERVALS == ("1minute",)
        # Synthetic adapter always returns bars → some completed, zero failed.
        assert report.total_failed == 0
        assert report.total_completed > 0

    def test_explicit_year(self, tmp_path: Path) -> None:
        report = pull_candles(_index("25"), lake_root=tmp_path, year=2024, dry_run=True)
        assert report.year == 2024

    def test_blank_symbol_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="non-empty engine symbol"):
            pull_candles(_index("   "), lake_root=tmp_path, dry_run=True)

    def test_no_data_propagates(self, tmp_path: Path) -> None:
        # An adapter that returns no rows surfaces no_data, and zero completed
        # → the UI shows "no data" rather than a green ✓.
        report = pull_candles(
            _index(),
            lake_root=tmp_path,
            year=2024,
            dry_run=True,
            intervals=_BOTH_INTERVALS,
            adapter_factory=lambda _root, _inst: (_EmptyAdapter(), NullBarSink()),
        )
        assert report.total_completed == 0
        assert report.total_failed == 0
        # DEC-SPOT-INTERVAL-DECOUPLE-001: BOTH phases run the full range over the symbol
        # (the 1minute is no longer skipped on absent 1-day coverage); the empty adapter
        # returns no bars for each → two genuine vendor no_data cells, zero skipped.
        assert report.total_no_data == 2  # both 1day + 1minute fetched + returned empty
        assert report.total_skipped == 0  # nothing skipped — the minute phase ran
        d = report.to_dict()
        assert d["total_no_data"] == report.total_no_data
        assert d["total_skipped"] == 0

    def test_on_progress_forwarded_per_pair(self, tmp_path: Path) -> None:
        """pull_candles threads on_progress → fires once per (symbol, interval) pair.

        The single-symbol twin of the bulk on_progress test: the dashboard's
        background-job percent ticks per completed pair, so the hook must fire
        for both fetched intervals (set-asserted — the parallel fan-out's
        completion order is non-deterministic). Uses a real sink (``dry_run=False`` +
        injected adapter) so both the 1day + 1minute phases land real bars
        (DEC-SPOT-INTERVAL-DECOUPLE-001 — both phases run the full range independently).
        """
        events: list[tuple[str, int, bool]] = []
        pull_candles(
            _index(),
            lake_root=tmp_path,
            dry_run=False,
            intervals=_BOTH_INTERVALS,
            from_date="2021-01-04",
            to_date="2021-01-08",
            adapter_factory=lambda root, _inst: (SyntheticSpotAdapter(), _real_sink(root)),
            on_progress=lambda label, bars, failed: events.append((label, bars, failed)),
        )
        assert {e[0] for e in events} == {"13 1minute", "13 1day"}
        assert all(failed is False for _, _, failed in events)


# ────────────────────────────────────────────────────────────────────────
# pull_candles — injected factory (covers the non-default-factory branch)
# ────────────────────────────────────────────────────────────────────────


class TestPullCandlesInjectedFactory:
    def test_factory_overrides_dry_run_false(self, tmp_path: Path) -> None:
        seen: dict[str, object] = {}

        def factory(
            lake_root: Path, instrument: Instrument
        ) -> tuple[SyntheticSpotAdapter, BarSink]:
            seen["lake_root"] = lake_root
            seen["symbol_name"] = instrument.symbol_name
            return SyntheticSpotAdapter(), NullBarSink()

        report = pull_candles(
            _index(),
            lake_root=tmp_path,
            year=2024,
            dry_run=False,
            adapter_factory=factory,
        )
        # Factory got the lake root + the instrument; dry_run flag recorded as
        # passed (False).
        assert seen["lake_root"] == tmp_path
        assert seen["symbol_name"] == "13"
        assert report.dry_run is False
        assert report.total_completed > 0

    def test_failing_adapter_reports_failures(self, tmp_path: Path) -> None:
        # DEC-SPOT-INTERVAL-DECOUPLE-001: Phase 1 (1day) quarantines (a terminal
        # BadRequest, not an auth death) and the 1-minute phase STILL RUNS over the full
        # range (no longer skipped on absent 1-day coverage). The failing adapter raises
        # for BOTH intervals → both pairs quarantined → 2 failed, 0 skipped, 0 no_data.
        report = pull_candles(
            _index(),
            lake_root=tmp_path,
            year=2024,
            dry_run=False,
            intervals=_BOTH_INTERVALS,
            adapter_factory=lambda root, _inst: (_FailingAdapter(), _real_sink(root)),
        )
        assert report.total_completed == 0
        assert report.total_failed == 2  # BOTH the 1day + 1minute phases quarantined
        assert report.total_skipped == 0  # nothing skipped — the minute phase ran
        assert report.total_no_data == 0  # both raised → quarantined, not vendor-empty


# ────────────────────────────────────────────────────────────────────────
# resolve_candle_range — pure [start, end) resolver (the from/to picker core)
# ────────────────────────────────────────────────────────────────────────

_NOW = datetime(2026, 5, 30, 12, 0, tzinfo=UTC)  # the context "today"


class TestResolveCandleRange:
    """The pure, O(1) window resolver behind the from/to-date candle picker."""

    def test_no_dates_no_year_pulls_full_history_to_yesterday(self) -> None:
        # Default mode 3 — neither date nor year → the FULL Track-1 history
        # (2020-01-01 → previous day), NOT the current year (FIX 4). The end is
        # the previous day (05-29), exclusive-end midnight 05-30, so the current
        # session (today, 05-30) is excluded by default (FIX 5).
        start, end = resolve_candle_range(from_date=None, to_date=None, year=None, now=_NOW)
        assert start == datetime(2020, 1, 1, tzinfo=UTC)  # the 2020 floor, not 2026
        assert end == datetime(2026, 5, 30, tzinfo=UTC)  # yesterday (05-29) + 1 day

    def test_default_date_range_is_the_picker_prefill(self) -> None:
        """DEC-PULL-FLOOR-2020-001: the canonical picker default = [2020-01-01,
        previous day] — the SAME window resolve_candle_range applies when blank, so
        the pre-filled UI dates can never drift from what a blank pull fetches."""
        assert default_date_range(now=_NOW) == ("2020-01-01", "2026-05-29")

    def test_no_dates_explicit_past_year_is_full_year(self) -> None:
        # Mode 2 — a PAST year, no dates → that full calendar year (the tail
        # is before today, so the clamp is a no-op).
        start, end = resolve_candle_range(from_date=None, to_date=None, year=2023, now=_NOW)
        assert start == datetime(2023, 1, 1, tzinfo=UTC)
        assert end == datetime(2024, 1, 1, tzinfo=UTC)

    def test_no_dates_current_year_clamps_to_previous_day(self) -> None:
        # Mode 2 — the CURRENT year explicitly → clamped to the previous day
        # (no explicit to_date), never the rest of the year and never today's
        # in-progress session (year=2026 on 2026-05-30 stops at 05-29, not
        # 2026-12-31 and not 05-30).
        start, end = resolve_candle_range(from_date=None, to_date=None, year=2026, now=_NOW)
        assert start == datetime(2026, 1, 1, tzinfo=UTC)
        assert end == datetime(2026, 5, 30, tzinfo=UTC)  # yesterday (05-29) + 1 day

    def test_no_dates_future_year_raises(self) -> None:
        # Mode 2 — a wholly-future year has nothing to pull (start > clamped
        # end) → refused, never a future request to the vendor.
        with pytest.raises(ValueError, match="nothing to pull"):
            resolve_candle_range(from_date=None, to_date=None, year=2027, now=_NOW)

    def test_no_dates_pre_floor_year_raises(self) -> None:
        # Mode 2 — a year wholly before the 2020 Groww floor (DEC-PULL-FLOOR-2020-001):
        # start clamps up to the floor, end stays in the pre-floor year → start > end →
        # refused. 2019 is now BEFORE the floor (the operator pulls the full 2020 depth).
        with pytest.raises(ValueError, match="nothing to pull"):
            resolve_candle_range(from_date=None, to_date=None, year=2019, now=_NOW)

    def test_explicit_from_and_to_are_day_inclusive(self) -> None:
        # Mode 1 — both ends inclusive; end is the midnight after to_date.
        start, end = resolve_candle_range(
            from_date="2024-02-01", to_date="2024-02-15", year=None, now=_NOW
        )
        assert start == datetime(2024, 2, 1, tzinfo=UTC)
        assert end == datetime(2024, 2, 16, tzinfo=UTC)

    def test_from_only_runs_to_previous_day(self) -> None:
        # from_date given, to_date omitted → default end is the previous day
        # (today's in-progress session excluded by default; FIX 5).
        start, end = resolve_candle_range(from_date="2026-05-01", to_date=None, year=None, now=_NOW)
        assert start == datetime(2026, 5, 1, tzinfo=UTC)
        assert end == datetime(2026, 5, 30, tzinfo=UTC)  # yesterday (05-29) + 1 day

    def test_to_only_starts_at_history_floor(self) -> None:
        # to_date must be on/after the 2020 floor or there's nothing to pull.
        start, end = resolve_candle_range(from_date=None, to_date="2021-03-10", year=None, now=_NOW)
        assert start == datetime(GROWW_HISTORY_FLOOR.year, 1, 1, tzinfo=UTC)  # 2020-01-01
        assert end == datetime(2021, 3, 11, tzinfo=UTC)

    def test_from_before_floor_is_clamped_up(self) -> None:
        start, _ = resolve_candle_range(
            from_date="2018-06-01", to_date="2021-06-01", year=None, now=_NOW
        )
        assert start == datetime(2020, 1, 1, tzinfo=UTC)  # clamped up to the 2020 floor

    def test_to_after_today_is_clamped_down(self) -> None:
        # An EXPLICIT to_date may reach today, so the clamp ceiling is today
        # (not the previous day) → 2099 clamps down to today (FIX 5 exception).
        _, end = resolve_candle_range(
            from_date="2026-01-01", to_date="2099-12-31", year=None, now=_NOW
        )
        assert end == datetime(2026, 5, 31, tzinfo=UTC)  # today (05-30) + 1 day

    def test_explicit_today_to_date_includes_today(self) -> None:
        # FIX 5 exception: an explicit to_date reaching today IS honoured — the
        # operator can deliberately include the current session. Here to_date ==
        # today (05-30) → exclusive end is 05-31, so today's day is fetched.
        start, end = resolve_candle_range(
            from_date="2026-05-01", to_date="2026-05-30", year=None, now=_NOW
        )
        assert start == datetime(2026, 5, 1, tzinfo=UTC)
        assert end == datetime(2026, 5, 31, tzinfo=UTC)  # today (05-30) + 1 day

    def test_year_is_ignored_when_a_date_is_given(self) -> None:
        # A date present → mode 1; the year argument must not leak in. to_date
        # omitted → default end is the previous day (FIX 5).
        start, end = resolve_candle_range(from_date="2024-07-01", to_date=None, year=2099, now=_NOW)
        assert start == datetime(2024, 7, 1, tzinfo=UTC)
        assert end == datetime(2026, 5, 30, tzinfo=UTC)  # yesterday (05-29) + 1 day

    def test_from_after_to_raises(self) -> None:
        with pytest.raises(ValueError, match="after to_date"):
            resolve_candle_range(from_date="2025-06-01", to_date="2025-05-01", year=None, now=_NOW)

    def test_future_from_date_raises(self) -> None:
        # from_date in the future + to=None (today) → from > to → refused.
        with pytest.raises(ValueError, match="after to_date"):
            resolve_candle_range(from_date="2027-01-01", to_date=None, year=None, now=_NOW)

    def test_malformed_from_date_raises(self) -> None:
        with pytest.raises(ValueError, match="from_date must be ISO"):
            resolve_candle_range(from_date="2024/02/01", to_date=None, year=None, now=_NOW)

    def test_malformed_to_date_raises(self) -> None:
        with pytest.raises(ValueError, match="to_date must be ISO"):
            resolve_candle_range(from_date=None, to_date="not-a-date", year=None, now=_NOW)

    def test_empty_string_dates_behave_as_omitted(self) -> None:
        # A bare ?from_date=&to_date= (or blank pickers) must behave exactly
        # like omitted bounds → fall through to year/default mode, NOT raise
        # a malformed-date error. Worst-case input hardening.
        start, end = resolve_candle_range(from_date="", to_date="", year=2023, now=_NOW)
        assert start == datetime(2023, 1, 1, tzinfo=UTC)
        assert end == datetime(2024, 1, 1, tzinfo=UTC)

    def test_whitespace_from_date_is_treated_as_omitted(self) -> None:
        # Whitespace-only from_date → floor; explicit mode still entered
        # because to_date is a real bound.
        start, end = resolve_candle_range(
            from_date="   ", to_date="2024-02-15", year=None, now=_NOW
        )
        assert start == datetime(GROWW_HISTORY_FLOOR.year, 1, 1, tzinfo=UTC)
        assert end == datetime(2024, 2, 16, tzinfo=UTC)


# ────────────────────────────────────────────────────────────────────────
# pull_candles — from/to date range threading (the picker, end to end)
# ────────────────────────────────────────────────────────────────────────


class TestPullCandlesDateRange:
    def test_year_mode_stamps_start_and_end_dates(self, tmp_path: Path) -> None:
        # Even in legacy year mode the report now carries the resolved window.
        report = pull_candles(_index(), lake_root=tmp_path, year=2024, dry_run=True)
        assert report.start_date == "2024-01-01"
        assert report.end_date == "2024-12-31"
        d = report.to_dict()
        assert d["start_date"] == "2024-01-01"
        assert d["end_date"] == "2024-12-31"

    def test_explicit_range_is_pulled_and_reported(self, tmp_path: Path) -> None:
        report = pull_candles(
            _index(),
            lake_root=tmp_path,
            from_date="2024-03-01",
            to_date="2024-03-31",
            dry_run=True,
        )
        assert report.start_date == "2024-03-01"
        assert report.end_date == "2024-03-31"
        assert report.year == 2024
        assert report.total_completed > 0

    def test_inverted_range_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="after to_date"):
            pull_candles(
                _index(),
                lake_root=tmp_path,
                from_date="2024-05-01",
                to_date="2024-04-01",
                dry_run=True,
            )


# ────────────────────────────────────────────────────────────────────────
# pull_all_spot — the one-click bulk-spot button core (DEC-DHAN-PULL-SCOPE-001)
# ────────────────────────────────────────────────────────────────────────

_BULK_NOW = datetime(2026, 5, 30, 12, 0, tzinfo=UTC)


class TestPullAllSpot:
    def test_dry_run_bulk_pulls_every_symbol(self, tmp_path: Path) -> None:
        report = pull_all_spot(
            [_index("13"), _index("25")], lake_root=tmp_path, dry_run=True, now=_BULK_NOW
        )
        assert isinstance(report, BulkSpotReport)
        assert report.dry_run is True
        assert report.total_symbols == 2
        # full 2020 → previous day window (DEC-PULL-FLOOR-2020-001); the bulk
        # pull passes no explicit to_date so the current session is excluded.
        assert report.start_date == "2020-01-01"
        assert report.end_date == "2026-05-29"  # previous day (now=05-30)
        # Verify-first default fetches 1-day ONLY (DEC-PULL-1DAY-FIRST-001) →
        # one interval × 2 symbols = 2 completed pairs (was 4 when 1m+1d).
        assert report.total_completed == 2
        assert report.total_failed == 0
        assert report.total_no_data == 0

    def test_empty_universe_is_clean_noop(self, tmp_path: Path) -> None:
        report = pull_all_spot([], lake_root=tmp_path, dry_run=True, now=_BULK_NOW)
        assert report.total_symbols == 0
        assert report.total_completed == 0
        assert report.to_dict()["symbols"] == []

    def test_drains_fetch_pipeline_before_write_consumer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MAJOR-4 regression: the fetch pipeline is drained BEFORE the write consumer.

        A pipeline consumer's tail FEEDS the write consumer, so draining the write consumer
        first could lose a late write a still-running pipeline consumer emits. The pull's
        ``finally`` must drain the pipeline (every queued tail run) FIRST, then the write
        consumer. Spy on the two module-private drain helpers + assert the call order.
        """
        order: list[str] = []
        monkeypatch.setattr(
            "brutex.dashboard.pull_controller._drain_fetch_pipeline",
            lambda _p: order.append("fetch_pipeline"),
        )
        monkeypatch.setattr(
            "brutex.dashboard.pull_controller._drain_write_consumer",
            lambda _c: order.append("write_consumer"),
        )
        pull_all_spot([_index("13")], lake_root=tmp_path, dry_run=True, now=_BULK_NOW)
        assert order == ["fetch_pipeline", "write_consumer"]

    def test_auto_retry_on_clean_dry_run_is_noop(self, tmp_path: Path) -> None:
        # The "Retry failed windows" button path (auto_retry=True): a clean
        # synthetic pull leaves no transient residual, so the wrapper runs no
        # extra rounds and the per-symbol rollup is identical to the single-pass
        # default — same engine, no behaviour change on a clean pull
        # (DEC-INGEST-AUTO-RETRY-001). DEC-SPOT-INTERVAL-DECOUPLE-001: a real sink so
        # BOTH phases run the full range (1day + 1minute, decoupled).
        report = pull_all_spot(
            [_index("13"), _index("25")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            intervals=_BOTH_INTERVALS,
            adapter_factory=_real_bulk_factory(SyntheticSpotAdapter()),
            auto_retry=True,
        )
        assert report.total_symbols == 2
        assert report.total_completed == 4  # 2 symbols × (1day + 1minute)
        assert report.total_failed == 0

    def test_explicit_range_is_honoured(self, tmp_path: Path) -> None:
        # DEC-DHAN-SPOT-MULTI-SATURATE-001: the bulk flatten now accepts the
        # multi-select date picker's from/to range (instead of always full
        # history), so routing the multi-select "Pull spot" through this
        # saturating path never silently widens the window. The report's
        # [start, end] must reflect the explicit range, not the 2021 floor.
        report = pull_all_spot(
            [_index("13"), _index("25")],
            lake_root=tmp_path,
            dry_run=True,
            now=_BULK_NOW,
            from_date="2026-02-01",
            to_date="2026-02-15",
        )
        assert report.start_date == "2026-02-01"
        assert report.end_date == "2026-02-15"  # inclusive end label
        assert report.total_symbols == 2

    def test_explicit_year_is_honoured(self, tmp_path: Path) -> None:
        report = pull_all_spot(
            [_index("13")], lake_root=tmp_path, dry_run=True, now=_BULK_NOW, year=2024
        )
        assert report.start_date == "2024-01-01"
        assert report.end_date == "2024-12-31"

    def test_no_range_defaults_to_full_history(self, tmp_path: Path) -> None:
        # Regression guard for the range-param addition: the ?kind= "Pull ALL"
        # callers pass no range → the full Track-1 history default is UNCHANGED.
        report = pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=True,
            now=_BULK_NOW,
            from_date=None,
            to_date=None,
            year=None,
        )
        assert report.start_date == "2020-01-01"
        assert report.end_date == "2026-05-29"  # previous day (now=05-30)

    def test_no_data_and_failure_branches(self, tmp_path: Path) -> None:
        # DEC-SPOT-INTERVAL-DECOUPLE-001: BOTH phases run the full range over the symbol
        # (the 1minute is no longer skipped on absent 1-day coverage). An EMPTY adapter
        # returns no bars for each interval → two settled no-data cells (1day + 1minute).
        # The verdict is still the known-gap (no operator action).
        report = pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            intervals=_BOTH_INTERVALS,
            adapter_factory=lambda _root, _insts: (_EmptyAdapter(), NullBarSink()),
        )
        assert report.total_no_data == 2  # both the 1day + 1minute phases ran + empty
        assert report.total_completed == 0
        assert report.symbols[0].symbol == "13"
        # Nothing completed + only no-data → the known-gap verdict…
        assert report.verdict.kind == "known_gap_only"
        # …but this full-range all-empty pull fast-settled ~90 old windows SKIPPED —
        # a mass write-off ABOVE the settled-skipped threshold, so needs_operator
        # flips True (minor M-4): a human confirms a write-off this large once.
        assert report.verdict.settled_skipped > SETTLED_SKIPPED_OPERATOR_THRESHOLD
        assert report.verdict.needs_operator is True
        assert report.to_dict()["verdict"]["kind"] == "known_gap_only"
        # A failing adapter → BOTH phases quarantine (terminal, not auth): the 1-minute
        # phase now runs the full range too, and the failing adapter raises for it as
        # well → 2 quarantined pairs.
        failed = pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            intervals=_BOTH_INTERVALS,
            adapter_factory=lambda _root, _insts: (_FailingAdapter(), NullBarSink()),
        )
        assert failed.total_failed == 2  # both the 1day + 1minute phases quarantined
        assert failed.dry_run is False
        # Quarantined pairs with NO retryable windows (token alive) → quarantined_symbols:
        # a re-pull can NEVER recover a symbol that isn't on Groww, so it's a COMPLETE pull
        # (grey, needs_operator False — no false "re-pull to finish"), DEC-EMPTY-RESOLVED-
        # REPORTING-001. The quarantined pairs ride in failed_pairs, NOT pending_windows.
        assert failed.verdict.kind == "quarantined_symbols"
        assert failed.verdict.needs_operator is False
        assert failed.verdict.pending_windows == 0
        assert failed.verdict.failed_pairs == 2
        assert failed.to_dict()["verdict"]["needs_operator"] is False

    def test_empty_fetch_is_no_data_not_a_failure(self, tmp_path: Path) -> None:
        # Groww has NO DH-905 known-gap marker (Golden Rule #1: no invented twin):
        # the vendor's "no bars for this range" answer is a clean 0-bar fetch,
        # counted as no_data (recorded EMPTY), NEVER a failed/pending window — so it
        # can never pin the pull at transient_residual / a manual "Re-pull to
        # finish" (the operator's NIFTY NEXT 50 daily trap stays solved).
        all_gap = pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            intervals=_BOTH_INTERVALS,
            adapter_factory=lambda _root, _insts: (_EmptyAdapter(), NullBarSink()),
        )
        assert all_gap.total_failed == 0  # NOT counted as a failure
        # DEC-SPOT-INTERVAL-DECOUPLE-001: BOTH phases run the full range over the symbol;
        # the empty adapter returns no bars for each → two settled no-data cells.
        assert all_gap.total_no_data == 2  # both 1day + 1minute are settled no-data (EMPTY)
        # Nothing completed + only no-data → known_gap_only,
        # NOT transient_residual — zero manual re-pull is asked. The mass fast-settle
        # (~90 old windows SKIPPED) crosses the settled-skipped threshold, so
        # needs_operator flips True (minor M-4) — the action names the count + the
        # reopen-skipped re-verify path.
        assert all_gap.verdict.kind == "known_gap_only"
        assert all_gap.verdict.settled_skipped > SETTLED_SKIPPED_OPERATOR_THRESHOLD
        assert all_gap.verdict.needs_operator is True
        assert f"{all_gap.verdict.settled_skipped} window(s)" in all_gap.verdict.action
        assert all_gap.verdict.pending_windows == 0

    def test_daily_gap_with_intraday_data_pulls_minute(self, tmp_path: Path) -> None:
        # DEC-SPOT-INTERVAL-DECOUPLE-001: the operator's "1day gap but 1minute lands"
        # case — the 1minute fill is now FULLY DECOUPLED from the 1-day coverage, so a
        # symbol with NO 1-day data STILL pulls its full-range 1-minute series (never
        # skipped). The 1minute completes (bars land) and the 1day empty is a settled
        # no-data; the pull is CLEAN (no failure, no pending window).
        report = pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            intervals=_BOTH_INTERVALS,
            adapter_factory=lambda _root, _insts: (_DailyGapAdapter(), NullBarSink()),
        )
        assert report.total_completed == 1  # 1minute LANDS (no longer skipped)
        assert report.total_failed == 0  # the 1day empty is NOT a failure
        assert report.total_no_data == 1  # only the 1day settled no-data
        assert report.verdict.kind == "clean"
        assert report.verdict.needs_operator is False
        assert report.verdict.pending_windows == 0

    def test_until_clean_dry_run_reaches_clean(self, tmp_path: Path) -> None:
        """``until_clean=True`` drives the bounded loop; a clean synthetic pull ends clean.

        The synthetic adapter always succeeds, so the FIRST round leaves zero
        transient residual → the until-clean loop stops on round 1 (no inter-round
        backoff, no real sleep) and the aggregate residual is 0.
        """
        report = pull_all_spot(
            [_index("13"), _index("25")],
            lake_root=tmp_path,
            dry_run=True,
            now=_BULK_NOW,
            until_clean=True,
            until_clean_rounds=3,
        )
        assert report.auth_aborted is False
        assert report.still_failed_windows == 0
        assert report.total_completed > 0
        assert report.verdict.kind == "clean"

    def test_factory_receives_the_full_instrument_list(self, tmp_path: Path) -> None:
        seen: dict[str, int] = {}

        def factory(lake_root: Path, instruments: object) -> tuple[SyntheticSpotAdapter, BarSink]:
            seen["count"] = len(list(instruments))  # type: ignore[arg-type]
            return SyntheticSpotAdapter(), NullBarSink()

        report = pull_all_spot(
            [_index("13"), _index("25"), _index("99")],
            lake_root=tmp_path,
            dry_run=True,
            now=_BULK_NOW,
            adapter_factory=factory,
        )
        assert seen["count"] == 3  # the live adapter must carry every symbol
        assert report.total_symbols == 3
        d = report.to_dict()
        assert d["total_symbols"] == 3 and len(d["symbols"]) == 3

    def test_on_progress_forwarded_per_pair(self, tmp_path: Path) -> None:
        """pull_all_spot threads on_progress → fires once per (symbol, interval) pair.

        Under the parallel fan-out (DEC-DHAN-PARALLEL-INGEST-001) on_progress
        fires in worker-COMPLETION order, which is non-deterministic, so the set
        of pairs is asserted (not a fixed order) — every pair fires exactly once
        and all are clean. (The deterministic, order-stable surface is the
        ``ingest.underlying`` rollup, covered separately.)
        """
        events: list[tuple[str, int, bool]] = []
        # DEC-SPOT-INTERVAL-DECOUPLE-001: a real sink so both the 1day + 1minute phases
        # land real bars over the full range (decoupled).
        pull_all_spot(
            [_index("13"), _index("25")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            intervals=_BOTH_INTERVALS,
            adapter_factory=_real_bulk_factory(SyntheticSpotAdapter()),
            on_progress=lambda label, bars, failed: events.append((label, bars, failed)),
        )
        # 2 symbols × 2 fetched intervals (1m + 1d) = 4 progress events, all clean.
        assert {e[0] for e in events} == {
            "13 1minute",
            "13 1day",
            "25 1minute",
            "25 1day",
        }
        assert len(events) == 4
        assert all(failed is False for _, _, failed in events)


def _events(logs: Sequence[Mapping[str, object]], name: str) -> list[Mapping[str, object]]:
    """All captured events with ``event == name`` (in emission order)."""
    return [e for e in logs if e["event"] == name]


_LOG_NOW = datetime(2021, 1, 6, tzinfo=UTC)  # tiny window so the synthetic run is fast


class TestPullControllerCorrelation:
    """One ``pull_id`` ties a whole pull together (DEC-DHAN-INGEST-LOGGING-001).

    The controller opens a ``pull_id_scope``, so the start/done summary AND the
    ingest spine's ``ingest.*`` events (emitted by the real dry-run spine) all
    carry the same id — the operator's one grep key for a pull.
    """

    def test_pull_candles_correlates_start_done_and_spine(self) -> None:
        with capture_logs() as logs:
            pull_candles(
                _index("13"),
                lake_root=Path("/unused"),
                from_date="2021-01-01",
                to_date="2021-01-05",
                dry_run=True,
                pull_id="ctlpull00001",
            )
        (start,) = _events(logs, "universe.pull.start")
        (done,) = _events(logs, "universe.pull.done")
        assert start["pull_id"] == "ctlpull00001"
        assert done["pull_id"] == "ctlpull00001"
        # The spine inherited the SAME id (no plumbing — via the bound scope).
        underlying = _events(logs, "ingest.underlying")
        assert underlying and all(e["pull_id"] == "ctlpull00001" for e in underlying)
        windows = _events(logs, "ingest.window")
        assert windows and all(e["pull_id"] == "ctlpull00001" for e in windows)

    def test_pull_candles_mints_a_pull_id_when_omitted(self) -> None:
        with capture_logs() as logs:
            pull_candles(
                _index("13"),
                lake_root=Path("/unused"),
                from_date="2021-01-01",
                to_date="2021-01-05",
                dry_run=True,
            )
        (start,) = _events(logs, "universe.pull.start")
        (done,) = _events(logs, "universe.pull.done")
        pid = start["pull_id"]
        assert isinstance(pid, str) and len(pid) == 12  # a freshly minted id
        assert done["pull_id"] == pid  # start + done share it

    def test_pull_candles_inherits_an_ambient_run_id(self) -> None:
        """DEC-PULL-BEAT-PERRUN-001 (the deliberate journal-grouping change): under an
        outer run scope the entrypoint INHERITS the run id instead of shadow-minting —
        so the journal/summary records and every spine worker's capture-then-rebind
        carry the JOB's run id (the registered beat-channel key)."""
        from brutex.observability.correlation import pull_id_scope

        with capture_logs() as logs, pull_id_scope("cafe00000001"):
            pull_candles(
                _index("13"),
                lake_root=Path("/unused"),
                from_date="2021-01-01",
                to_date="2021-01-05",
                dry_run=True,
            )
        (start,) = _events(logs, "universe.pull.start")
        (done,) = _events(logs, "universe.pull.done")
        assert start["pull_id"] == "cafe00000001"  # inherited, NOT a shadow mint
        assert done["pull_id"] == "cafe00000001"
        underlying = _events(logs, "ingest.underlying")
        assert underlying and all(e["pull_id"] == "cafe00000001" for e in underlying)

    def test_pull_candles_explicit_pull_id_still_wins_over_ambient(self) -> None:
        from brutex.observability.correlation import pull_id_scope

        with capture_logs() as logs, pull_id_scope("cafe00000001"):
            pull_candles(
                _index("13"),
                lake_root=Path("/unused"),
                from_date="2021-01-01",
                to_date="2021-01-05",
                dry_run=True,
                pull_id="ctlpull00009",
            )
        (start,) = _events(logs, "universe.pull.start")
        assert start["pull_id"] == "ctlpull00009"

    def test_pull_all_spot_inherits_an_ambient_run_id(self) -> None:
        """The bulk-spot twin of the inherit pin (the load-bearing entrypoint: the spot
        fan-out workers' rebinds must carry the job's run id or their seam beats drop)."""
        from brutex.observability.correlation import pull_id_scope

        with capture_logs() as logs, pull_id_scope("cafe00000002"):
            pull_all_spot(
                [_index("13")],
                lake_root=Path("/unused"),
                dry_run=True,
                now=_LOG_NOW,
            )
        (start,) = _events(logs, "universe.pull_all.start")
        (done,) = _events(logs, "universe.pull_all.done")
        assert start["pull_id"] == "cafe00000002"
        assert done["pull_id"] == "cafe00000002"
        underlying = _events(logs, "ingest.underlying")
        assert underlying and all(e["pull_id"] == "cafe00000002" for e in underlying)

    def test_pull_all_spot_correlates_under_one_pull_id(self) -> None:
        with capture_logs() as logs:
            pull_all_spot(
                [_index("13")],
                lake_root=Path("/unused"),
                dry_run=True,
                now=_LOG_NOW,
                pull_id="ctlpull00003",
            )
        (start,) = _events(logs, "universe.pull_all.start")
        (done,) = _events(logs, "universe.pull_all.done")
        assert start["pull_id"] == "ctlpull00003"
        assert done["pull_id"] == "ctlpull00003"
        # The coverage lake-reconcile (and its `reconciled_windows` stamp) was
        # retired in DEC-WINDOW-LEDGER-001 Stage d — the ledger drives skip now.
        assert "reconciled_windows" not in start
        underlying = _events(logs, "ingest.underlying")
        assert underlying and all(e["pull_id"] == "ctlpull00003" for e in underlying)


class TestSessionRateController:
    """The session-scoped rate-controller holder (DEC-DHAN-PERSIST-RATE-001).

    Persists ONE controller across pulls so the learned rate carries over instead
    of every pull restarting at the operator-DIRECTED 8 r/s ceiling
    (RAISED 5 → 8 on 2026-06-24, DEC-GROWW-RATE-CEILING-8-002).
    """

    def test_get_returns_same_instance_and_builds_once(self) -> None:
        from brutex.adapters.rate_common import RateController

        built: list[RateController] = []

        def _factory() -> RateController:
            ctl = RateController()
            built.append(ctl)
            return ctl

        holder = SessionRateController(factory=_factory)
        first = holder.get()
        second = holder.get()
        assert second is first  # SAME controller across pulls — the learned rate persists
        assert len(built) == 1  # built ONCE, not per pull

    def test_peek_returns_none_before_first_build(self) -> None:
        """peek() NEVER builds — it returns None until a real get() has built the controller.

        DEC-PULL-RATE-WIDGET-VISIBILITY-001: the observe-only rate-panel sampler reads the
        controller via peek() so a dry-run / pre-pull tick conjures no live controller.
        """
        from brutex.adapters.rate_common import RateController

        built: list[RateController] = []

        def _factory() -> RateController:
            ctl = RateController()
            built.append(ctl)
            return ctl

        holder = SessionRateController(factory=_factory)
        # peek() before any get() builds NOTHING and reads None.
        assert holder.peek() is None
        assert built == []

    def test_peek_returns_built_controller_without_rebuilding(self) -> None:
        """After get() builds the controller, peek() returns that SAME instance, no rebuild."""
        from brutex.adapters.rate_common import RateController

        built: list[RateController] = []

        def _factory() -> RateController:
            ctl = RateController()
            built.append(ctl)
            return ctl

        holder = SessionRateController(factory=_factory)
        live = holder.get()
        assert holder.peek() is live  # the same built controller, observed not rebuilt
        assert len(built) == 1

    def test_default_factory_builds_a_real_controller(self) -> None:
        # DEC-GROWW-NO-DAILY-QUOTA-001: the dashboard controller is built with NO
        # DailyQuotaTracker (Groww backtesting has no daily quota, operator-confirmed),
        # matching the CLI Groww path — so it never halts on a phantom daily cap, while
        # the 500/min per-minute governor stays wired. This is THE production inertness
        # pin the quota-verdict MOOT decision rests on (DEC-QUOTA-VERDICT-MOOT-001,
        # with the AST zero-construction pin in test_quota_verdict_moot.py): no
        # tracker ⇒ quota_exhausted() constant False ⇒ the stop_quota brake + the
        # quota_exhausted verdict are unreachable from any live dashboard pull.
        from brutex.adapters.groww.constants import (
            GROWW_RATE_LIMIT_PER_MIN,
            GROWW_RATE_LIMIT_RPS_CEILING,
        )
        from brutex.adapters.rate_common import RateController

        holder = SessionRateController()  # default factory = _build_live_controller
        ctl = holder.get()
        assert isinstance(ctl, RateController)
        assert holder.get() is ctl  # cached on subsequent calls
        # No daily quota wired → quota_exhausted is always False + the snapshot omits
        # the quota-gauge keys (the rate panel then hides the gauge via its -1 sentinel).
        assert ctl.quota_exhausted() is False
        snap = ctl.snapshot()
        assert "quota_limit" not in snap and "quota_used" not in snap
        # The per-minute 500/min governor IS still wired.
        assert snap["ceiling_per_min"] == GROWW_RATE_LIMIT_PER_MIN
        # The controller starts at the operator-DIRECTED 8 r/s per-second ceiling
        # (RAISED 5 → 8 on 2026-06-24, DEC-GROWW-RATE-CEILING-8-002), and 8×60 = 480/min
        # stays under the 500/min governor.
        assert ctl.current_rps == GROWW_RATE_LIMIT_RPS_CEILING == 8.0
        assert snap["current_rps"] == 8.0
        assert GROWW_RATE_LIMIT_RPS_CEILING * 60 <= GROWW_RATE_LIMIT_PER_MIN


class TestSessionRateControllerMeter:
    """``register_meter`` / ``peek_meter`` — publish the pool's MEASURED-rate meter.

    DEC-RATE-WIDGET-FNO-MEASURED-001: the pull build sites register the live client pool's
    shared transport ``CallMeter`` so the observe-only ``RatePanelSampler`` (which holds
    only the session controller) can read the REAL completed-rate during the metadata + F&O
    phases, not the admission average.
    """

    def test_peek_meter_none_before_registration(self) -> None:
        """peek_meter() reads None until a real pull registers a meter — never builds one."""
        holder = SessionRateController()
        assert holder.peek_meter() is None  # never built a controller; never a meter either

    def test_register_then_peek_returns_same_meter(self) -> None:
        """A registered meter is returned VERBATIM by peek_meter (the same instance)."""
        from brutex.adapters.call_meter import CallMeter

        holder = SessionRateController()
        meter = CallMeter()
        holder.register_meter(meter)
        assert holder.peek_meter() is meter

    def test_register_none_is_ignored_leaving_prior_meter_intact(self) -> None:
        """Registering None NEVER blanks a prior live meter (a defensive best-effort guard).

        A client that (defensively) exposed no ``.meter`` would register ``None``; that must
        not clear a meter a real pool already published, so a transient factory oddity never
        blinds the sampler mid-pull.
        """
        from brutex.adapters.call_meter import CallMeter

        holder = SessionRateController()
        meter = CallMeter()
        holder.register_meter(meter)
        holder.register_meter(None)  # a no-meter client → ignored, prior meter kept
        assert holder.peek_meter() is meter

    def test_register_overwrites_with_new_pools_meter(self) -> None:
        """A second pull's pool meter OVERWRITES the first (the pool holds ONE aggregate)."""
        from brutex.adapters.call_meter import CallMeter

        holder = SessionRateController()
        first = CallMeter()
        second = CallMeter()
        holder.register_meter(first)
        holder.register_meter(second)
        assert holder.peek_meter() is second

    def test_clear_meter_evicts_and_is_idempotent(self) -> None:
        """``clear_meter`` — the paired DEATH of the register lifecycle (the C0 fix).

        DEC-RATE-METER-LIFECYCLE-001. MUTANT KILLED: the ``clear_meter`` body
        severed (``self._meter = None`` dropped) → the registered meter would
        survive the clear and a later sampler tick reads the DEAD pull's meter.
        """
        from brutex.adapters.call_meter import CallMeter

        holder = SessionRateController()
        holder.clear_meter()  # idempotent on an EMPTY holder — a no-op, never raises
        assert holder.peek_meter() is None
        meter = CallMeter()
        holder.register_meter(meter)
        assert holder.peek_meter() is meter
        holder.clear_meter()
        assert holder.peek_meter() is None, "the clear must evict the registered handle"
        holder.clear_meter()  # idempotent on an already-cleared holder
        assert holder.peek_meter() is None
        # The holder stays usable — a later pull re-registers as normal.
        holder.register_meter(meter)
        assert holder.peek_meter() is meter

    def test_clear_meter_expected_match_clears(self) -> None:
        """``clear_meter(expected=…)`` with the EXACT stored handle evicts it (round 4)."""
        from brutex.adapters.call_meter import CallMeter

        holder = SessionRateController()
        meter = CallMeter()
        holder.register_meter(meter)
        holder.clear_meter(expected=meter)
        assert holder.peek_meter() is None

    def test_clear_meter_expected_mismatch_keeps_stored_meter(self) -> None:
        """The identity-gated compare-and-clear NEVER evicts a handle it did not register.

        DEC-RATE-METER-LIFECYCLE-001 round 4. MUTANT KILLED: the ``expected is None or
        self._meter is expected`` gate severed back to the unconditional clear — a
        finished pull's exit would then evict a CONCURRENT pull's later registration
        (the round-4 MAJOR's eviction class).
        """
        from brutex.adapters.call_meter import CallMeter

        holder = SessionRateController()
        mine = CallMeter()
        concurrent = CallMeter()
        holder.register_meter(mine)
        holder.register_meter(concurrent)  # a concurrent pull re-registered LATER
        holder.clear_meter(expected=mine)  # my exit must NOT evict the newer handle
        assert holder.peek_meter() is concurrent, (
            "compare-and-clear must keep a concurrent pull's later registration"
        )
        # …and the expected-gated clear on an EMPTY holder is an idempotent no-op.
        holder.clear_meter(expected=mine)
        assert holder.peek_meter() is concurrent

    def test_clear_meter_default_stays_unconditional(self) -> None:
        """``expected=None`` (the job-end hook / dry-run gate shape) clears whatever
        is stored — the pre-round-4 contract those zero-arg callers rely on."""
        from brutex.adapters.call_meter import CallMeter

        holder = SessionRateController()
        holder.register_meter(CallMeter())
        holder.clear_meter()  # no handle to compare → unconditional
        assert holder.peek_meter() is None

    def test_concurrent_register_peek_and_meter_writes_never_tear(self) -> None:
        """Racing register_meter/peek_meter against live meter start/stop is torn-read-free.

        DEC-RATE-WIDGET-FNO-MEASURED-001 (concurrency): the sampler thread reads
        ``peek_meter()`` then ``snapshot()`` while the fetch worker races ``start()``/
        ``stop()`` on the SAME meter and a rebuild races ``register_meter()``. A lightweight
        port of the verification stress test: every read returns a fresh scalar dict with a
        non-negative ``effective_rps`` and no exception is raised on any thread — the holder
        lock guards the single-slot handle and the meter lock guards its own gauges, so the
        reader's snapshot can never crash on a mutation-during-iteration or observe a torn /
        negative rate.
        """
        import threading

        from brutex.adapters.call_meter import CallMeter

        holder = SessionRateController()
        meter = CallMeter()
        holder.register_meter(meter)
        stop_flag = threading.Event()
        errors: list[Exception] = []

        def _writer() -> None:
            try:
                while not stop_flag.is_set():
                    meter.start()
                    meter.stop(elapsed_ms=1.0, completed=True)
            except Exception as exc:  # pragma: no cover - only on a real race bug
                errors.append(exc)

        def _rebuilder() -> None:
            try:
                while not stop_flag.is_set():
                    holder.register_meter(CallMeter())
                    holder.register_meter(meter)
            except Exception as exc:  # pragma: no cover - only on a real race bug
                errors.append(exc)

        def _reader() -> None:
            try:
                while not stop_flag.is_set():
                    live = holder.peek_meter()
                    if live is not None:
                        snap = live.snapshot()
                        assert snap["effective_rps"] >= 0.0
            except Exception as exc:  # pragma: no cover - only on a real race bug
                errors.append(exc)

        threads = [
            threading.Thread(target=_writer),
            threading.Thread(target=_rebuilder),
            threading.Thread(target=_reader),
            threading.Thread(target=_reader),
        ]
        for thread in threads:
            thread.start()
        stop_flag.wait(0.5)
        stop_flag.set()
        for thread in threads:
            thread.join(timeout=5.0)
            assert not thread.is_alive()
        assert errors == []


class TestRegisterPullMeter:
    """``register_pull_meter`` — the COMMON extract-and-register seam the build sites call."""

    def test_none_session_is_a_noop(self) -> None:
        """A None session_rate (dry-run / injected factory) → no-op, never raises."""
        from brutex.adapters.call_meter import CallMeter

        class _Client:
            meter = CallMeter()

        # No session → nothing to register into; must simply return without error.
        register_pull_meter(None, _Client())

    def test_registers_meter_from_a_client_pool_directly(self) -> None:
        """A source with a ``.meter`` (a ClientPool / GrowwClient) registers it directly."""
        from brutex.adapters.call_meter import CallMeter

        meter = CallMeter()

        class _Pool:
            meter = None  # placeholder overridden below

        pool = _Pool()
        pool.meter = meter  # type: ignore[assignment]
        holder = SessionRateController()
        register_pull_meter(holder, pool)
        assert holder.peek_meter() is meter

    def test_registers_meter_from_an_adapters_client(self) -> None:
        """A source with no ``.meter`` but a ``.client.meter`` (an adapter) registers that."""
        from brutex.adapters.call_meter import CallMeter

        meter = CallMeter()

        class _Client:
            def __init__(self, m: CallMeter) -> None:
                self.meter = m

        class _Adapter:
            def __init__(self, c: _Client) -> None:
                self.client = c

        holder = SessionRateController()
        register_pull_meter(holder, _Adapter(_Client(meter)))
        assert holder.peek_meter() is meter

    def test_source_with_no_meter_anywhere_is_a_noop(self) -> None:
        """A source with neither ``.meter`` nor ``.client.meter`` leaves the holder empty."""

        class _Bare:
            pass

        holder = SessionRateController()
        register_pull_meter(holder, _Bare())
        assert holder.peek_meter() is None

    def test_dry_run_clears_stale_and_never_registers(self) -> None:
        """A dry-run DEREGISTERS — even against a REAL-metered source (the D1 fix).

        DEC-RATE-METER-LIFECYCLE-001. MUTANTS KILLED: the ``if dry_run:`` branch
        severed → the metered source would register (peek non-None); the
        ``clear_meter()`` dropped from the branch → the stale prior-pull meter
        would persist (peek is the stale meter).
        """
        from brutex.adapters.call_meter import CallMeter

        class _Client:
            def __init__(self) -> None:
                self.meter = CallMeter()

        holder = SessionRateController()
        stale = CallMeter()
        holder.register_meter(stale)  # a prior real pull's meter, never evicted pre-#1593
        register_pull_meter(holder, _Client(), dry_run=True)
        assert holder.peek_meter() is None, (
            "a dry-run must evict the stale meter AND never register the synthetic one"
        )

    def test_dry_run_with_none_session_is_noop(self) -> None:
        """``dry_run=True`` with no session holder returns without touching anything."""
        from brutex.adapters.call_meter import CallMeter

        class _Client:
            meter = CallMeter()

        register_pull_meter(None, _Client(), dry_run=True)  # must simply return

    def test_live_meterless_source_keeps_prior_and_metered_registers(self) -> None:
        """The LIVE path is byte-unchanged: meterless keeps prior; metered registers.

        Re-pins the hard constraint: the intra-pull ``register_meter(None)``
        ignore-guard survives the dry-run gate. MUTANTS KILLED: the clear widened
        onto the live path (``if meter is None: clear_meter()`` — the prior meter
        would vanish); the gate inverted (``if not dry_run: clear``).
        """
        from brutex.adapters.call_meter import CallMeter

        class _Bare:
            pass

        holder = SessionRateController()
        prior = CallMeter()
        holder.register_meter(prior)
        register_pull_meter(holder, _Bare(), dry_run=False)
        assert holder.peek_meter() is prior, "a live meterless source must KEEP the prior meter"

        fresh = CallMeter()

        class _Pool:
            def __init__(self) -> None:
                self.meter = fresh

        register_pull_meter(holder, _Pool(), dry_run=False)
        assert holder.peek_meter() is fresh, "a live metered source registers as before"


# ── the pull build sites' register_pull_meter WIRING (DEC-RATE-METER-LIFECYCLE-001,
#    audit C1/C2: all four sites severed left 1,329 tests green — these kill the
#    two SPOT-spine mutants by driving the REAL pull functions) ─────────────────


class _MeteredClient:
    """A minimal ``.meter``-carrying client stub (the ClientPool/GrowwClient shape)."""

    def __init__(self, meter: object) -> None:
        self.meter = meter


class _MeteredSpotAdapter:
    """A synthetic spot adapter whose ``.client.meter`` is a KNOWN CallMeter.

    Delegates every protocol call to a wrapped :class:`SyntheticSpotAdapter`
    (a slotted dataclass that cannot grow a ``client`` attribute) while carrying
    the live-adapter ``.client.meter`` shape ``register_pull_meter`` probes.
    """

    def __init__(self, meter: object) -> None:
        self._inner = SyntheticSpotAdapter()
        self.client = _MeteredClient(meter)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


class _RecordingHolder(SessionRateController):
    """A :class:`SessionRateController` spy recording every non-``None`` registration.

    Round 4: ``pull_candles`` pairs its registration with an identity-gated clear on
    exit, so the post-return ``peek_meter()`` is honestly ``None`` — the C1 wiring
    oracle therefore observes the IN-pull registration through this spy instead.
    """

    def __init__(self) -> None:
        super().__init__()
        self.registered: list[object] = []

    def register_meter(self, meter: object) -> None:  # type: ignore[override]
        if meter is not None:
            self.registered.append(meter)
        super().register_meter(meter)  # type: ignore[arg-type]


class TestPullMeterWiring:
    """The spot spines' ``register_pull_meter`` call sites, driven END-TO-END."""

    def test_pull_all_spot_registers_exact_adapter_meter(self, tmp_path: Path) -> None:
        """MUTANT KILLED: ``register_pull_meter(...)`` at the ``pull_all_spot`` build
        site severed to ``pass`` → ``peek_meter()`` stays ``None`` (audit C1 spine 1)."""
        from brutex.adapters.call_meter import CallMeter

        meter = CallMeter()
        adapter = cast(MarketAdapter, _MeteredSpotAdapter(meter))
        holder = SessionRateController()
        pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            adapter_factory=lambda _root, _insts: (adapter, NullBarSink()),
            session_rate=holder,
        )
        assert holder.peek_meter() is meter, (
            "the bulk-spot build site must register the adapter's EXACT client meter"
        )

    def test_pull_candles_registers_exact_adapter_meter(self, tmp_path: Path) -> None:
        """MUTANT KILLED: the ``pull_candles`` build-site call severed (C1 spine 2).

        Round 4 moved the spine onto the PAIRED ``_pull_meter_scope`` (register on
        enter, identity-gated clear on exit), so the registration is observed DURING
        the pull via a recording holder — the exact adapter meter must be registered
        mid-run — and the post-return state asserts the paired clear (below).
        """
        from brutex.adapters.call_meter import CallMeter

        meter = CallMeter()
        adapter = cast(MarketAdapter, _MeteredSpotAdapter(meter))
        holder = _RecordingHolder()
        pull_candles(
            _index(),
            lake_root=tmp_path,
            year=2024,
            dry_run=False,
            adapter_factory=lambda _root, _inst: (adapter, NullBarSink()),
            session_rate=holder,
        )
        assert holder.registered == [meter], (
            "the pull_candles build site must register the adapter's EXACT client meter"
        )
        # Round 4: the spine's paired identity-gated clear — no in-process pull
        # running ⇒ no meter registered (MUTANT KILLED: `_pull_meter_scope` dropped
        # from the `with` → the finished off-job pull's meter stays readable).
        assert holder.peek_meter() is None, (
            "pull_candles must clear its OWN registration on exit (round 4)"
        )

    def test_pull_candles_raise_before_register_keeps_foreign_meter(self, tmp_path: Path) -> None:
        """The round-4 MAJOR, at the spine: a raise BEFORE registration clears NOTHING.

        A malformed ``from_date`` raises in ``resolve_candle_range`` before the
        adapter factory / registration ever run — a concurrent JOB pull's LIVE
        registered meter must survive this pull's failure. MUTANT KILLED: any
        register-site-independent unconditional clear on the failure path (the
        pre-round-4 route-level ``finally: clear_meter()`` shape).
        """
        from brutex.adapters.call_meter import CallMeter

        holder = SessionRateController()
        job_meter = CallMeter()
        holder.register_meter(job_meter)  # a mid-F&O-phase job pull's live meter

        def _never_called(_root: Path, _inst: Instrument) -> tuple[MarketAdapter, BarSink]:
            raise AssertionError("the factory must never run — the date raises first")

        with pytest.raises(ValueError):
            pull_candles(
                _index(),
                lake_root=tmp_path,
                from_date="2026-13-99",
                dry_run=False,
                adapter_factory=_never_called,
                session_rate=holder,
            )
        assert holder.peek_meter() is job_meter, (
            "a raise BEFORE registration must never evict a concurrent job pull's "
            "LIVE registered meter (round-4 MAJOR)"
        )

    def test_pull_candles_raise_after_register_still_clears_own_meter(self, tmp_path: Path) -> None:
        """The failure arm of the paired scope: registered-then-raised clears its own.

        The adapter registers its meter at the build site, then the 1-minute phase
        dies on ``DailyQuotaExceeded`` (a global stop ``pull_candles`` re-raises).
        The dead pull's meter must NOT linger (the C0 residual, now spine-paired).
        """
        from brutex.adapters.call_meter import CallMeter

        class _QuotaDeath(_MeteredSpotAdapter):
            def fetch_ohlcv(
                self, *, symbol: str, exchange: Exchange, interval: str, start: date, end: date
            ) -> list[CandleBar]:
                del symbol, exchange, interval, start, end
                raise DailyQuotaExceeded("daily quota spent mid-pull")

        meter = CallMeter()
        adapter = cast(MarketAdapter, _QuotaDeath(meter))
        holder = SessionRateController()
        with pytest.raises(DailyQuotaExceeded):
            pull_candles(
                _index(),
                lake_root=tmp_path,
                year=2024,
                dry_run=False,
                adapter_factory=lambda _root, _inst: (adapter, NullBarSink()),
                session_rate=holder,
            )
        assert holder.peek_meter() is None, (
            "the paired scope must clear the dead pull's OWN registration on a raise"
        )

    def test_pull_candles_exit_never_evicts_a_later_registration(self, tmp_path: Path) -> None:
        """The compare-and-clear at the spine: a CONCURRENT pull's later registration
        survives this pull's exit.

        The adapter's first fetch simulates a concurrent pull's build site firing
        mid-run (it overwrites the holder with a FOREIGN meter). MUTANT KILLED: the
        scope's ``clear_meter(expected=…)`` degraded to an unconditional clear —
        the foreign live meter would be evicted by the finished pull's exit.
        """
        from brutex.adapters.call_meter import CallMeter

        foreign = CallMeter()
        holder = SessionRateController()

        class _ConcurrentRegistration(_MeteredSpotAdapter):
            def fetch_ohlcv(
                self, *, symbol: str, exchange: Exchange, interval: str, start: date, end: date
            ) -> list[CandleBar]:
                # A concurrent pull's build site re-registers mid-run.
                holder.register_meter(foreign)
                return self._inner.fetch_ohlcv(
                    symbol=symbol, exchange=exchange, interval=interval, start=start, end=end
                )

        adapter = cast(MarketAdapter, _ConcurrentRegistration(CallMeter()))
        pull_candles(
            _index(),
            lake_root=tmp_path,
            year=2024,
            dry_run=False,
            adapter_factory=lambda _root, _inst: (adapter, NullBarSink()),
            session_rate=holder,
        )
        assert holder.peek_meter() is foreign, (
            "the identity-gated exit clear must keep a concurrent pull's later "
            "registration (compare-and-clear, round 4)"
        )

    def test_spot_dry_run_clears_stale_meter(self, tmp_path: Path) -> None:
        """The C0 headline pin (FAILS on pre-#1593 HEAD): a spot dry-run with the
        DEFAULT synthetic adapter EVICTS a prior real pull's registered meter.

        MUTANT KILLED: the ``dry_run=dry_run`` kwarg dropped at the ``pull_all_spot``
        site — the meterless synthetic adapter would then ride the
        ``register_meter(None)`` ignore-guard and KEEP the stale meter readable.
        """
        from brutex.adapters.call_meter import CallMeter

        holder = SessionRateController()
        stale = CallMeter()
        holder.register_meter(stale)  # pull #1's meter, job long finished
        pull_all_spot(
            [_index("13")], lake_root=tmp_path, dry_run=True, now=_BULK_NOW, session_rate=holder
        )
        assert holder.peek_meter() is None, (
            "the dry-run bulk pull must deregister — never leave a dead pull's meter"
        )

    def test_pull_candles_dry_run_clears_stale_meter(self, tmp_path: Path) -> None:
        """The ``pull_candles`` twin of the C0 pin (kwarg-dropped mutant, spine 2)."""
        from brutex.adapters.call_meter import CallMeter

        holder = SessionRateController()
        holder.register_meter(CallMeter())
        pull_candles(_index(), lake_root=tmp_path, year=2024, dry_run=True, session_rate=holder)
        assert holder.peek_meter() is None


# ── parse_symbol_list + resolve_pull_symbols — the multi-select pull ──
# (DEC-UNIVERSE-OPTIONS-MULTIPULL-001) — the ?symbols= backend resolution


class TestParseSymbolList:
    """``parse_symbol_list`` — split ?symbols= into a clean, ordered, de-duped list."""

    def test_splits_trims_and_preserves_order(self) -> None:
        assert parse_symbol_list("NIFTY, BANKNIFTY ,SENSEX") == ["NIFTY", "BANKNIFTY", "SENSEX"]

    def test_drops_blanks_and_dedups_first_wins(self) -> None:
        # empty segments + a repeat are removed; first occurrence keeps its place.
        assert parse_symbol_list("NIFTY,,BANKNIFTY, ,NIFTY") == ["NIFTY", "BANKNIFTY"]

    def test_empty_string_is_empty_list(self) -> None:
        assert parse_symbol_list("") == []
        assert parse_symbol_list("  , ,") == []


class TestResolvePullSymbols:
    """``resolve_pull_symbols`` — resolve the ?symbols= list to instruments, fail closed."""

    def test_resolves_each_in_input_order(self) -> None:
        store = {"NIFTY": _index("NIFTY"), "BANKNIFTY": _index("BANKNIFTY")}
        out = resolve_pull_symbols("BANKNIFTY,NIFTY", store.get)
        assert [i.symbol_name for i in out] == ["BANKNIFTY", "NIFTY"]

    def test_empty_selection_raises(self) -> None:
        with pytest.raises(ValueError, match="symbols= was empty"):
            resolve_pull_symbols("  , ,", {}.get)

    def test_any_unknown_id_fails_closed_naming_offenders(self) -> None:
        store = {"NIFTY": _index("NIFTY")}
        with pytest.raises(ValueError, match="unknown security_id.*NOPE.*ZZZ"):
            resolve_pull_symbols("NIFTY,NOPE,ZZZ", store.get)

    def test_single_known_id_resolves(self) -> None:
        store = {"NIFTY": _index("NIFTY")}
        (only,) = resolve_pull_symbols("NIFTY", store.get)
        assert only.symbol_name == "NIFTY"


class TestResolvePullTargets:
    """``resolve_pull_targets`` — the SHARED selection resolver (DEC-PULL-SELECTED-PIPELINE-001)."""

    def test_symbols_selection_wins_and_labels_selection(self) -> None:
        store = {"A": _index("A"), "B": _index("B")}
        universe_called = False

        def _universe() -> list[Instrument]:
            nonlocal universe_called
            universe_called = True
            return [_index("Z")]

        out, source = resolve_pull_targets(
            symbols="B,A", symbol="A", lookup=store.get, whole_universe=_universe
        )
        assert [i.symbol_name for i in out] == ["B", "A"]  # symbols precedence + order
        assert source == "selection"
        assert universe_called is False  # the fallback is never reached

    def test_single_symbol_when_no_symbols(self) -> None:
        store = {"A": _index("A")}
        out, source = resolve_pull_targets(
            symbols=None, symbol="A", lookup=store.get, whole_universe=lambda: []
        )
        assert [i.symbol_name for i in out] == ["A"]
        assert source == "single"

    def test_unknown_single_symbol_fails_closed(self) -> None:
        with pytest.raises(ValueError, match="unknown security_id 'NOPE'"):
            resolve_pull_targets(
                symbols=None, symbol="NOPE", lookup={}.get, whole_universe=lambda: []
            )

    def test_empty_symbols_fails_closed(self) -> None:
        with pytest.raises(ValueError, match="symbols= was empty"):
            resolve_pull_targets(
                symbols="  , ,", symbol=None, lookup={}.get, whole_universe=lambda: []
            )

    def test_no_selection_falls_back_to_whole_universe(self) -> None:
        out, source = resolve_pull_targets(
            symbols=None,
            symbol=None,
            lookup={}.get,
            whole_universe=lambda: [_index("X"), _index("Y")],
        )
        assert [i.symbol_name for i in out] == ["X", "Y"]
        assert source == "universe"


class TestPullPopulatesLedger:
    """DEC-WINDOW-LEDGER-001 (PR-4): a live pull records EVERY window in the ledger DB."""

    def test_live_pull_writes_success_rows(self, tmp_path: Path) -> None:
        from brutex.data.window_ledger_store import ledger_path, load_window_ledger

        # Scope to a narrow 1-day window (DEC-INTERIOR-GAP-DETECT-001 / DEC-PARTIAL-WINDOW-REPULL-001):
        # the production pull now OPTS IN to the data-completeness re-pull demotion, and the
        # SyntheticSpotAdapter returns only a few bars on each window's FIRST day — so a WIDE
        # 1-minute window would (correctly) classify as truncated and demote to FAILED. The
        # ``1day`` interval is never demotable (a derived daily window), and a narrow range keeps
        # the synthetic windows complete, so this pins the clean SUCCESS-ledger intent without the
        # synthetic-truncation artifact (mirrors TestPullAllSpotDecouple).
        report = pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            from_date="2024-01-01",
            to_date="2024-01-31",
            intervals=("1day",),
            adapter_factory=lambda _root, _insts: (SyntheticSpotAdapter(), NullBarSink()),
        )
        assert report.dry_run is False
        assert ledger_path(tmp_path).is_file()  # durable Parquet written
        ledger = load_window_ledger(tmp_path)
        statuses = {r.status for r in ledger.records()}
        assert ledger.count > 0
        assert statuses == {"SUCCESS"}  # every planned window fetched + recorded
        # The ledger now backs /coverage/windows (every window has a row). The
        # Groww spot pull stamps the "groww" namespace on both the PLANNED seeds
        # and the recorded SUCCESS rows, so they reconcile (PR-J4b).
        assert all(r.market == "groww" and r.security_id == "13" for r in ledger.records())

    def test_empty_windows_are_recorded_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A clean 0-bar fetch is recorded with its true outcome (never a perpetual
        # "pending" PLANNED row), so every window is captured in the DB.
        # DEC-OLD-GAP-FAST-SETTLE-001: this 2020→2026 backfill yields BOTH outcomes —
        # OLD genuine-gap windows (end > 60 days before now) settle SKIPPED in ONE pull
        # (vendor data can never appear), while RECENT empty windows (within the 60-day
        # horizon) stay EMPTY and are re-checked on a later pull. So the statuses are
        # exactly {EMPTY, SKIPPED} — never a stuck PLANNED, never a FAILED.
        # The spot age-gate reads the REAL wall clock in production (a window's "old"-ness
        # is judged against the real today, by design — there is no production reason to
        # inject a fake now into the age decision). Freeze that clock to _BULK_NOW here so
        # the boundary assertion is DETERMINISTIC and can NEVER become a wall-clock
        # time-bomb (Golden Rule #7) — hostile-review fix.
        from brutex.data.window_ledger_store import ledger_path, load_window_ledger

        monkeypatch.setattr("brutex.adapters.groww.ingest._now_ist", lambda: _BULK_NOW)

        pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            adapter_factory=lambda _root, _insts: (_EmptyAdapter(), NullBarSink()),
        )
        assert ledger_path(tmp_path).is_file()
        ledger = load_window_ledger(tmp_path)
        assert ledger.count > 0
        statuses = {r.status for r in ledger.records()}
        # OLD windows fast-settled SKIPPED, recent ones stayed EMPTY — both present, and
        # nothing else (no stuck PLANNED / no FAILED on a clean empty fetch).
        assert statuses == {"EMPTY", "SKIPPED"}
        # Every SKIPPED row is an OLD window (end strictly > 60 days before the clock);
        # every EMPTY row is RECENT (within the horizon) — the fast-settle is age-gated.
        cutoff = (_BULK_NOW.date() - timedelta(days=60)).isoformat()
        for record in ledger.records():
            if record.status == "SKIPPED":
                assert record.window_end < cutoff  # only old gaps fast-settle
            else:
                assert record.window_end >= cutoff  # recent empties stay EMPTY

    def test_dry_run_writes_no_ledger(self, tmp_path: Path) -> None:
        from brutex.data.window_ledger_store import ledger_path

        pull_all_spot([_index("13")], lake_root=tmp_path, dry_run=True, now=_BULK_NOW)
        assert not ledger_path(tmp_path).exists()  # dry-run never pollutes the ledger


class TestQuotaDeathMidRound:
    """DEC-SPOT-QUOTA-MIDROUND-001: a mid-round ``DailyQuotaExceeded`` maps to the
    loud ``quota_exhausted`` verdict with committed work kept — never a generic
    job error that discards the settled-window history (the spot arm of the
    options DEC-OPTIONS-UNTIL-CLEAN-001 mapping, the entry #625 noted follow-up).
    """

    @staticmethod
    def _quota_death_scaffold() -> BulkAdapterFactory:
        """Build the deterministic phased quota-death fake.

        Phase ordering: the ``1day`` phase (Phase 1) SUCCEEDS and commits real bars to
        the lake; the ``1minute`` phase (Phase 2) then raises ``DailyQuotaExceeded`` —
        the exception the pace bucket raises inside a window worker when the daily quota
        dies mid-pass (never quarantined; the spine re-raises it). A REAL sink so
        Phase 1's 1-day bars land. The committed 1-day work survives in the ledger; the
        death maps to the loud quota verdict, not a crash. (DEC-SPOT-INTERVAL-DECOUPLE-001:
        the minute phase runs the full range — the quota death is unaffected.)
        """

        class _QuotaDiesOnMinute(SyntheticSpotAdapter):
            def fetch_ohlcv(
                self, *, symbol: str, exchange: Exchange, interval: str, start: date, end: date
            ) -> list[CandleBar]:
                if interval == "1minute":
                    raise DailyQuotaExceeded("daily quota of 100000 calls spent")
                return super().fetch_ohlcv(
                    symbol=symbol, exchange=exchange, interval=interval, start=start, end=end
                )

        def factory(lake_root: Path, _insts: Sequence[Instrument]) -> tuple[MarketAdapter, BarSink]:
            return _QuotaDiesOnMinute(), _real_sink(lake_root)

        return factory

    def _pull(
        self,
        tmp_path: Path,
        *,
        until_clean: bool,
    ) -> tuple[BulkSpotReport, Sequence[Mapping[str, object]]]:
        """One phased quota-dying live-pattern pull over a tiny 5-day window."""
        factory = self._quota_death_scaffold()

        with capture_logs() as logs:
            report = pull_all_spot(
                [_index("13")],
                lake_root=tmp_path,
                dry_run=False,
                now=_BULK_NOW,
                from_date="2021-01-04",
                to_date="2021-01-08",
                intervals=_BOTH_INTERVALS,
                adapter_factory=factory,
                until_clean=until_clean,
            )
        return report, logs

    def test_quota_death_mid_pull_maps_verdict_and_keeps_ledger(self, tmp_path: Path) -> None:
        """Single-pass Pull-ALL: the death becomes the quota verdict, not a crash."""
        from brutex.data.window_ledger_store import ledger_path, load_window_ledger
        from brutex.data.window_status_store import status_path

        report, logs = self._pull(tmp_path, until_clean=False)
        assert report.quota_exhausted is True
        assert report.verdict.kind == "quota_exhausted"
        assert report.auth_aborted is False
        assert report.still_failed_windows == 0
        # Phase 1 (1day) COMMITTED before the Phase-2 (1minute) quota death, so its
        # 1-day pull is shown as completed — committed work is never hidden. The
        # 1-minute phase died → its outcome is UNKNOWN (claim nothing).
        assert report.total_completed == 1  # the committed 1day phase
        assert report.total_failed == 0
        assert report.total_no_data == 0
        # The finally-guaranteed persist: the settled 1day windows survive as SUCCESS;
        # the never-fetched 1minute windows stay PLANNED — nothing discarded.
        assert ledger_path(tmp_path).is_file()
        # DEC-STATUS-SIDECAR-WRITE-RETIRE-001: the legacy window_status.json is
        # NEVER written any more — the ledger is the sole persisted store.
        assert not status_path(tmp_path).is_file()
        ledger = load_window_ledger(tmp_path)
        assert ledger.count >= 2
        assert {r.status for r in ledger.records()} == {"SUCCESS", "PLANNED"}
        # The loud warning — a quota death is never silent in /logs.
        assert _events(logs, "universe.pull_all.quota_exhausted")

    def test_quota_death_mid_round_until_clean_maps_verdict(self, tmp_path: Path) -> None:
        """until_clean: a round-1 mid-round death maps identically (no doomed rounds)."""
        from brutex.data.window_ledger_store import ledger_path, load_window_ledger

        report, logs = self._pull(tmp_path, until_clean=True)
        assert report.quota_exhausted is True
        assert report.verdict.kind == "quota_exhausted"
        assert report.auth_aborted is False
        assert report.auth_cause is None
        assert report.still_failed_windows == 0
        # Phase 1 (1day) committed before the Phase-2 quota death → completed shown.
        assert report.total_completed == 1
        assert report.total_failed == 0
        assert ledger_path(tmp_path).is_file()
        assert {r.status for r in load_window_ledger(tmp_path).records()} == {
            "SUCCESS",
            "PLANNED",
        }
        assert _events(logs, "universe.pull_all.quota_exhausted")

    def test_quota_death_in_round_two_keeps_round_one_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A round-≥2 death reports the LAST COMPLETED round's state + rollup.

        The ``passes[-1]`` arm of the in-phase ``DailyQuotaExceeded`` handler: the
        1-day phase (Phase 1) commits first; the 1-minute phase (Phase 2) then runs the
        until-clean loop where round 1 quarantines the 1-minute pair terminally (a
        FAILED window is never skippable, so round 2 re-attempts exactly it), then
        round 2's worker dies on the quota. The real driver would need round-1 transient
        residual + real inter-round backoff sleeps to reach round 2, so the loop driver
        is faked to force the second round — the quota exception itself still originates
        INSIDE a real worker pass and propagates through the real except/finally path.
        A REAL sink so Phase 1's 1-day bars land. (DEC-SPOT-INTERVAL-DECOUPLE-001: the
        minute phase runs the full range — this round-2 path is unaffected.)
        """
        from brutex.adapters.ingest_common import UntilCleanOutcome, UntilCleanRound
        from brutex.data.window_ledger_store import ledger_path

        state = {"round": 1, "phase": 0}

        class _MinuteDiesInRoundTwo(SyntheticSpotAdapter):
            def fetch_ohlcv(
                self, *, symbol: str, exchange: Exchange, interval: str, start: date, end: date
            ) -> list[CandleBar]:
                if interval == "1minute":
                    if state["round"] >= 2:
                        raise DailyQuotaExceeded("daily quota of 100000 calls spent")
                    raise GrowwBadRequestError("boom 1minute")  # round 1: terminal quarantine
                return super().fetch_ohlcv(
                    symbol=symbol, exchange=exchange, interval=interval, start=start, end=end
                )

        # The faked until-clean loop: Phase 1 (the 1-day loop) cleans on round 1 → it
        # delegates to the REAL loop; Phase 2 (the 1-minute loop) is forced into a
        # round 2 that dies on the quota — reaching the in-phase quota-death arm.
        real_run_until_clean = __import__(
            "brutex.adapters.ingest_common", fromlist=["run_until_clean"]
        ).run_until_clean

        def _force_second_round(
            round_runner: Callable[[], UntilCleanRound], **kwargs: object
        ) -> UntilCleanOutcome:
            state["phase"] += 1
            if state["phase"] == 1:
                # Phase 1 (1-day) — run the REAL loop (it cleans on round 1).
                return real_run_until_clean(round_runner, **kwargs)  # type: ignore[arg-type]
            round_runner()  # Phase 2 round 1 — 1minute quarantines terminally
            state["round"] = 2
            round_runner()  # Phase 2 round 2 — the 1minute worker raises DailyQuotaExceeded
            raise AssertionError("round 2 must die on the quota")  # pragma: no cover

        monkeypatch.setattr("brutex.dashboard.pull_controller.run_until_clean", _force_second_round)
        with capture_logs() as logs:
            report = pull_all_spot(
                [_index("13")],
                lake_root=tmp_path,
                dry_run=False,
                now=_BULK_NOW,
                from_date="2021-01-04",
                to_date="2021-01-08",
                intervals=_BOTH_INTERVALS,
                adapter_factory=lambda root, _insts: (_MinuteDiesInRoundTwo(), _real_sink(root)),
                until_clean=True,
            )
        assert report.quota_exhausted is True
        assert report.verdict.kind == "quota_exhausted"  # quota outranks the failed pair
        # passes[-1] = round 1's COMPLETED pass: a terminal 1minute quarantine, live token.
        assert report.auth_aborted is False
        assert report.auth_cause is None
        # The per-symbol rollup folds BOTH phases: the 1day landed (Phase 1) +
        # the 1minute quarantined terminally in round 1 (Phase 2).
        assert report.total_completed == 1  # the 1day phase landed
        assert report.total_failed == 1  # the 1minute terminal quarantine from round 1
        assert ledger_path(tmp_path).is_file()
        events = _events(logs, "universe.pull_all.quota_exhausted")
        # rounds_completed counts the 1minute phase's recorded passes (round 1).
        assert events and events[0]["rounds_completed"] == 1

    def test_until_clean_zero_round_budget_is_a_safe_noop(self, tmp_path: Path) -> None:
        """``until_clean_rounds=0`` returns the empty-report rollup, never IndexError.

        ``run_until_clean(max_rounds=0)`` runs ZERO rounds (the disabled no-op), so
        no pass exists — the pre-DEC-SPOT-QUOTA-MIDROUND-001 ``passes[0]`` crashed
        here; the empty-report fallback now rolls every symbol up as zeros.
        """
        report = pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=True,
            now=_BULK_NOW,
            until_clean=True,
            until_clean_rounds=0,
        )
        assert report.total_completed == 0
        assert report.total_failed == 0
        assert report.total_no_data == 0
        assert report.quota_exhausted is False
        assert report.auth_aborted is False
        assert report.verdict.kind == "clean"  # nothing ran, nothing failed


class TestStatusSidecarWriteRetired:
    """DEC-STATUS-SIDECAR-WRITE-RETIRE-001 — the legacy sidecar's write path is dead.

    The unified ledger (DEC-WINDOW-LEDGER-001) is the sole store a pull writes;
    ``window_status.json`` is stop-write-not-delete: pre-existing files stay
    byte-identical on disk (the read fallback + one-shot migration source) and a
    fresh lake never grows one.
    """

    def test_live_pull_writes_ledger_and_never_the_status_sidecar(self, tmp_path: Path) -> None:
        from brutex.data.window_ledger_store import ledger_path
        from brutex.data.window_status_store import status_path

        report = pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            intervals=_BOTH_INTERVALS,
            adapter_factory=lambda _root, _insts: (_DailyGapAdapter(), NullBarSink()),
        )
        # DEC-SPOT-INTERVAL-DECOUPLE-001: the 1minute phase runs the full range and lands
        # (decoupled from the empty 1day coverage) → the symbol completes → clean verdict.
        assert report.verdict.kind == "clean"
        assert ledger_path(tmp_path).is_file()
        assert not status_path(tmp_path).is_file()  # never written again

    def test_preexisting_sidecar_is_untouched_and_still_migrates(self, tmp_path: Path) -> None:
        from brutex.data.fetch_coverage import WindowKey
        from brutex.data.window_ledger_store import load_window_ledger
        from brutex.data.window_status import WindowStatusLog
        from brutex.data.window_status_store import save_window_status, status_path

        # A pre-ledger lake: the legacy sidecar holds one settled legacy window.
        log = WindowStatusLog()
        log.record(
            WindowKey(
                market="dhan",
                security_id="LEGACY",
                interval="1minute",
                start=date(2020, 1, 6),
                end=date(2020, 1, 7),
            ),
            raw_bars=5,
            error_type=None,
            bars_appended=5,
            now=_BULK_NOW,
        )
        save_window_status(log, tmp_path)
        before = status_path(tmp_path).read_bytes()

        pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            adapter_factory=lambda _root, _insts: (_DailyGapAdapter(), NullBarSink()),
        )
        # Stop-write, not delete: the legacy file is byte-identical...
        assert status_path(tmp_path).read_bytes() == before
        # ...and its rows were folded into the ledger by the one-shot migration
        # (the rollback net DEC-WINDOW-LEDGER-001 keeps until Stage 6).
        legacy = [r for r in load_window_ledger(tmp_path).records() if r.security_id == "LEGACY"]
        assert len(legacy) == 1
        assert legacy[0].status == "SUCCESS"


# ────────────────────────────────────────────────────────────────────────
# DEC-SPOT-WINDOW-PROGRESS-001 — per-WINDOW live sync
# ────────────────────────────────────────────────────────────────────────


class _ListClock:
    """A clock returning a fixed sequence of monotonic values (deterministic throttle)."""

    def __init__(self, times: Sequence[float]) -> None:
        self._times = list(times)
        self._i = 0

    def __call__(self) -> float:
        value = self._times[self._i]
        self._i += 1
        return value


class TestMakeLiveSync:
    """The throttled, idempotent per-window live-sync callback."""

    def test_none_ledger_is_a_noop(self) -> None:
        calls: list[int] = []
        sync = make_live_sync(None, lambda: calls.append(1), lambda *_a: calls.append(2))
        sync()
        assert calls == []  # nothing flushed, nothing pushed

    def test_none_on_ledger_sync_is_a_noop(self) -> None:
        from brutex.data.window_ledger import WindowLedger

        calls: list[int] = []
        sync = make_live_sync(WindowLedger(), lambda: calls.append(1), None)
        sync()
        assert calls == []

    def test_throttles_flush_and_push(self) -> None:
        from brutex.data.fetch_coverage import WindowKey
        from brutex.data.window_ledger import WindowLedger

        ledger = WindowLedger()
        ledger.record(
            WindowKey(
                market="groww",
                security_id="NIFTY",
                interval="1day",
                start=date(2020, 1, 1),
                end=date(2020, 6, 29),
            ),
            raw_bars=120,
            error_type=None,
            bars_appended=120,
            now=_BULK_NOW,
        )
        flushes: list[int] = []
        pushes: list[tuple[int, int, int, int, str]] = []
        sync = make_live_sync(
            ledger,
            lambda: flushes.append(1),
            lambda c, t, b, f, cur: pushes.append((c, t, b, f, cur)),
            min_interval_s=1.0,
            clock=_ListClock([0.0, 0.5, 1.5]),
        )
        sync()  # t=0.0 → first fire (no prior)
        sync()  # t=0.5 → within interval → throttled
        sync()  # t=1.5 → interval elapsed → fires again
        assert len(flushes) == 2
        assert pushes == [(1, 1, 120, 0, "NIFTY 1day"), (1, 1, 120, 0, "NIFTY 1day")]


class TestPullAllSpotLedgerSync:
    """`pull_all_spot(on_ledger_sync=...)` drives the bar from the live ledger."""

    def test_final_sync_reports_complete_ledger(self, tmp_path: Path) -> None:
        pushes: list[tuple[int, int, int, int, str]] = []
        # Scope to a narrow 1-day window (DEC-INTERIOR-GAP-DETECT-001 / DEC-PARTIAL-WINDOW-REPULL-001):
        # the production pull opts in to the completeness re-pull demotion, and the synthetic
        # adapter's first-day-only bars would (correctly) classify a WIDE 1-minute window as
        # truncated → FAILED. ``1day`` is never demotable + a narrow range keeps windows
        # complete, so this pins the "completed == total, failed == 0" intent cleanly.
        report = pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            from_date="2024-01-01",
            to_date="2024-01-31",
            intervals=("1day",),
            adapter_factory=lambda _root, _insts: (SyntheticSpotAdapter(), NullBarSink()),
            on_ledger_sync=lambda c, t, b, f, cur: pushes.append((c, t, b, f, cur)),
        )
        assert report.dry_run is False
        # The FINAL authoritative push (in the finally) lands the complete end state: every
        # planned window settled (completed == total), zero failed, bars accrued, and the
        # current label names the symbol — so the bar reaches 100% honestly.
        assert pushes, "on_ledger_sync must receive at least the final push"
        completed, total, bars, failed, current = pushes[-1]
        assert total > 0
        assert completed == total  # all windows settled SUCCESS
        assert failed == 0
        assert bars > 0
        assert current.startswith("13 ")

    def test_final_sync_lands_even_on_partial_progress(self, tmp_path: Path) -> None:
        # An EMPTY-returning adapter records every window as EMPTY (no data). The final push
        # must STILL fire (in the finally) AND — per DEC-EMPTY-RESOLVED-REPORTING-001 — an
        # EMPTY window is RESOLVED (no-data), NOT "pending": a wholly-empty historical pull
        # reads 100% resolved, never the old misleading "0 settled / re-pull to finish".
        pushes: list[tuple[int, int, int, int, str]] = []
        pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            adapter_factory=lambda _root, _insts: (_EmptyAdapter(), NullBarSink()),
            on_ledger_sync=lambda c, t, b, f, cur: pushes.append((c, t, b, f, cur)),
        )
        assert pushes
        completed, total, _bars, _failed, _current = pushes[-1]
        assert total > 0
        assert completed == total  # every EMPTY window is RESOLVED (no-data), not pending

    def test_dry_run_does_not_push(self, tmp_path: Path) -> None:
        # Dry-run has no ledger (begin_pull_ledger → None), so the live sync is a no-op.
        pushes: list[object] = []
        pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=True,
            now=_BULK_NOW,
            on_ledger_sync=lambda *a: pushes.append(a),
        )
        assert pushes == []


# ── SPOT interval DECOUPLE phasing (DEC-SPOT-INTERVAL-DECOUPLE-001) ──────────
#
# The SPOT 1-minute pull is FULLY DECOUPLED from the 1-day coverage (MIGRATION.md
# #851): the 1day phase pulls its full [start, end] range and the 1minute phase
# INDEPENDENTLY pulls its full [start, end] range for EVERY requested symbol — no
# coverage discovery, no per-symbol filtering, no minute-start bounding. The
# verify-first 1-day-only DEFAULT (the operator picks granularity via the dropdown)
# is RETAINED; only the 1-minute↔1-day COUPLING is removed.


class _AuthFailAdapter(SyntheticSpotAdapter):
    """A :class:`MarketAdapter` whose every fetch raises a Groww AUTH failure.

    ``GrowwAuthError`` quarantines the pair AND sets ``auth_aborted`` (derived) — the
    global-stop signal the 1-minute phase must honour (skip Phase 2, token is dead).
    """

    def fetch_ohlcv(
        self, *, symbol: str, exchange: Exchange, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        """Always raise a Groww auth error — drives the global-stop guard."""
        del symbol, exchange, start, end, interval
        raise GrowwAuthError("token expired")


_SPOT_FROM = "2024-01-01"
_SPOT_TO = "2024-01-31"


class TestPullAllSpotDecouple:
    """Phasing of the BULK pull (``pull_all_spot``) under interval decoupling."""

    def test_one_day_only_is_single_phase_unchanged(self, tmp_path: Path) -> None:
        # The verify-first default (``intervals=("1day",)``) runs ONLY the 1day phase —
        # no minute phase, byte-identical to the pre-feature behaviour.
        from brutex.data.window_ledger_store import load_window_ledger

        report = pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            from_date=_SPOT_FROM,
            to_date=_SPOT_TO,
            intervals=("1day",),
            adapter_factory=_real_bulk_factory(SyntheticSpotAdapter()),
        )
        assert report.total_completed == 1
        assert report.verdict.kind == "clean"
        # ONLY 1-day windows in the ledger (no 1-minute rows planned).
        intervals = {r.interval for r in load_window_ledger(tmp_path).records()}
        assert intervals == {"1day"}

    def test_both_runs_one_day_and_minute_full_range(self, tmp_path: Path) -> None:
        # ``both`` → the 1day phase + the 1minute phase, EACH over the full range for
        # EVERY symbol. Both intervals land + are planned.
        from brutex.data.window_ledger_store import load_window_ledger

        report = pull_all_spot(
            [_index("13"), _index("25")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            from_date=_SPOT_FROM,
            to_date=_SPOT_TO,
            intervals=_BOTH_INTERVALS,
            adapter_factory=_real_bulk_factory(SyntheticSpotAdapter()),
        )
        assert report.total_completed == 4  # 2 symbols × (1day + 1minute)
        assert report.verdict.kind == "clean"
        intervals = {r.interval for r in load_window_ledger(tmp_path).records()}
        assert intervals == {"1day", "1minute"}

    def test_minute_full_window_planned_for_every_symbol(self, tmp_path: Path) -> None:
        # DECOUPLE: the 1-minute windows planned in the ledger span the FULL
        # [start, end] range for every symbol — no per-symbol bounding to a 1-day
        # listing date. The first planned 1minute window starts at the global start.
        from brutex.data.window_ledger_store import load_window_ledger

        pull_all_spot(
            [_index("13"), _index("25")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            from_date=_SPOT_FROM,
            to_date=_SPOT_TO,
            intervals=("1minute",),
            adapter_factory=_real_bulk_factory(SyntheticSpotAdapter()),
        )
        minute_rows = [r for r in load_window_ledger(tmp_path).records() if r.interval == "1minute"]
        # Every requested symbol has 1-minute windows (none filtered out).
        assert {r.security_id for r in minute_rows} == {"13", "25"}
        # The full range is planned: the earliest planned window starts at the global
        # start date for EACH symbol (no minute-start bounded to 1-day coverage).
        for symbol in ("13", "25"):
            sym_starts = [r.window_start for r in minute_rows if r.security_id == symbol]
            assert min(sym_starts) == "2024-01-01"

    def test_no_one_day_symbol_still_pulls_full_minute(self, tmp_path: Path) -> None:
        # The decouple linchpin: a symbol with ZERO 1-day coverage (the 1day fetch
        # returns empty) is NO LONGER skipped from the minute pull — its full-range
        # 1-minute series lands. The 1day empty is a settled no-data; the 1minute
        # completes; the pull is CLEAN.
        report = pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            from_date=_SPOT_FROM,
            to_date=_SPOT_TO,
            intervals=_BOTH_INTERVALS,
            adapter_factory=_real_bulk_factory(_DailyGapAdapter()),
        )
        assert report.total_completed == 1  # the 1minute landed (NOT skipped)
        assert report.total_no_data == 1  # the 1day is a settled empty
        assert report.total_failed == 0
        assert report.verdict.kind == "clean"

    def test_no_skip_log_emitted(self, tmp_path: Path) -> None:
        # The decoupled minute phase NEVER emits the retired 3c skip log — every symbol
        # is pulled, none filtered. (Belt-and-suspenders against the coupling regressing.)
        with capture_logs() as logs:
            pull_all_spot(
                [_index("13")],
                lake_root=tmp_path,
                dry_run=False,
                now=_BULK_NOW,
                from_date=_SPOT_FROM,
                to_date=_SPOT_TO,
                intervals=("1minute",),
                adapter_factory=_real_bulk_factory(_DailyGapAdapter()),
            )
        assert _events(logs, "universe.pull_all.minute_skipped_no_1day") == []

    def test_global_stop_auth_skips_minute_phase(self, tmp_path: Path) -> None:
        # The 1-day phase dies on a Groww auth error → the GLOBAL-STOP guard SKIPS the
        # 1-minute phase entirely (the token is dead — no point), reported loudly. This
        # guard is RETAINED by the decouple (it is not the 1-day-coverage coupling).
        # DEC-GROWW-AUTH-HALT-001: the spine now RAISES the terminal auth error as a
        # pull-wide global stop; pull_all_spot CATCHES it and maps it to this SAME
        # graceful auth_expired verdict (the pre-halt shape), firing the CRITICAL
        # pull-halt alert at the catch site (the halt never escapes to the job layer).
        with capture_logs() as logs:
            report = pull_all_spot(
                [_index("13")],
                lake_root=tmp_path,
                dry_run=False,
                now=_BULK_NOW,
                from_date=_SPOT_FROM,
                to_date=_SPOT_TO,
                intervals=_BOTH_INTERVALS,
                adapter_factory=lambda _root, _insts: (_AuthFailAdapter(), NullBarSink()),
            )
        assert report.auth_aborted is True
        assert report.verdict.kind == "auth_expired"
        stop = _events(logs, "universe.pull_all.minute_skipped_global_stop")
        assert stop and stop[0]["auth_aborted"] is True
        # The 1-minute phase never ran — only the 1day quarantine surfaces.
        assert report.total_failed == 1  # the 1day auth quarantine

    def test_retryable_auth_shape_escaping_spine_propagates_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # DEC-GROWW-AUTH-HALT-001 carve-out: the graceful auth-abort catch in
        # pull_all_spot maps ONLY the TERMINAL shape; a RETRYABLE auth-family error
        # (a stale read-only re-read) that somehow escapes the spine re-raises
        # UNCHANGED — never a fake auth_expired verdict, never a halt alert.
        from brutex.adapters.groww.errors import GrowwTokenStillStaleError

        def _raise_still_stale(*_a: object, **_k: object) -> object:
            raise GrowwTokenStillStaleError("same dead token", safe_message="stale")

        monkeypatch.setattr(
            "brutex.dashboard.pull_controller.run_groww_spot_ingest_with_retry",
            _raise_still_stale,
        )
        with pytest.raises(GrowwTokenStillStaleError):
            pull_all_spot(
                [_index("13")],
                lake_root=tmp_path,
                dry_run=False,
                now=_BULK_NOW,
                from_date=_SPOT_FROM,
                to_date=_SPOT_TO,
                intervals=("1day",),
                adapter_factory=_real_bulk_factory(SyntheticSpotAdapter()),
            )

    def test_dry_run_minute_pulls_full_range(self, tmp_path: Path) -> None:
        # A dry-run ``both`` pull: the minute phase runs the full range too (decoupled —
        # no lake read, no skip). The synthetic dry-run adapter lands both intervals.
        report = pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=True,
            now=_BULK_NOW,
            from_date=_SPOT_FROM,
            to_date=_SPOT_TO,
            intervals=_BOTH_INTERVALS,
        )
        assert report.dry_run is True
        assert report.total_completed == 2  # both 1day + 1minute landed
        assert report.total_failed == 0


class TestPullCandlesDecouple:
    """Phasing of the single-symbol pull (``pull_candles``) under interval decoupling."""

    def test_one_day_only_is_single_phase(self, tmp_path: Path) -> None:
        # The default 1-day-only request runs ONLY the 1day phase (the verify-first
        # default — retained by the decouple).
        report = pull_candles(
            _index(),
            lake_root=tmp_path,
            dry_run=False,
            from_date=_SPOT_FROM,
            to_date=_SPOT_TO,
            intervals=("1day",),
            adapter_factory=lambda root, _inst: (SyntheticSpotAdapter(), _real_sink(root)),
        )
        assert [tf.interval for tf in report.timeframes] == ["1d"]
        assert report.total_completed == 1

    def test_both_runs_one_day_and_minute_full_range(self, tmp_path: Path) -> None:
        # ``both`` → the 1day phase + the 1minute phase, each over the full range. Both
        # cells complete.
        report = pull_candles(
            _index(),
            lake_root=tmp_path,
            dry_run=False,
            from_date=_SPOT_FROM,
            to_date=_SPOT_TO,
            intervals=_BOTH_INTERVALS,
            adapter_factory=lambda root, _inst: (SyntheticSpotAdapter(), _real_sink(root)),
        )
        labels = {tf.interval for tf in report.timeframes}
        assert labels == {"1m", "1d"}
        assert report.total_completed == 2

    def test_minute_only_no_one_day_lake_still_pulls_full_range(self, tmp_path: Path) -> None:
        # DECOUPLE: a 1-minute-only request with NO pre-existing 1-day lake is NO LONGER
        # skipped — the full-range 1-minute series lands (never a skip, never queried as
        # vendor-empty), and the retired 3c skip log is never emitted.
        with capture_logs() as logs:
            report = pull_candles(
                _index(),
                lake_root=tmp_path,
                dry_run=False,
                from_date=_SPOT_FROM,
                to_date=_SPOT_TO,
                intervals=("1minute",),
                adapter_factory=lambda root, _inst: (SyntheticSpotAdapter(), _real_sink(root)),
            )
        assert [tf.interval for tf in report.timeframes] == ["1m"]
        assert report.total_completed == 1  # the 1-minute series LANDS (not skipped)
        assert report.total_skipped == 0
        assert report.total_no_data == 0
        assert _events(logs, "universe.pull.minute_skipped_no_1day") == []

    def test_minute_only_lands_independently_of_one_day(self, tmp_path: Path) -> None:
        # A 1-minute-only request lands its full range regardless of whether a 1-day lake
        # exists (the decouple's whole point — no coverage dependency).
        report = pull_candles(
            _index(),
            lake_root=tmp_path,
            dry_run=False,
            from_date=_SPOT_FROM,
            to_date=_SPOT_TO,
            intervals=("1minute",),
            adapter_factory=lambda root, _inst: (SyntheticSpotAdapter(), _real_sink(root)),
        )
        assert [tf.interval for tf in report.timeframes] == ["1m"]
        assert report.total_completed == 1

    def test_global_stop_auth_skips_minute_phase(self, tmp_path: Path) -> None:
        # The 1-day phase dies on auth → the global-stop guard skips the 1-minute phase
        # (RETAINED — not the coverage coupling); the 1day cell is the quarantine, the
        # 1minute cell a SKIPPED cell (we refused to query — token dead), NOT vendor-empty.
        with capture_logs() as logs:
            report = pull_candles(
                _index(),
                lake_root=tmp_path,
                dry_run=False,
                from_date=_SPOT_FROM,
                to_date=_SPOT_TO,
                intervals=_BOTH_INTERVALS,
                adapter_factory=lambda _root, _inst: (_AuthFailAdapter(), NullBarSink()),
            )
        assert _events(logs, "universe.pull.minute_skipped_global_stop")
        assert report.total_failed == 1  # the 1day auth quarantine
        assert report.total_skipped == 1  # the 1minute phase skipped → SKIPPED cell
        assert report.total_no_data == 0  # never queried → NOT labeled vendor-empty

    def test_retryable_auth_shape_escaping_spine_propagates_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # DEC-GROWW-AUTH-HALT-001 carve-out (single-symbol twin): a RETRYABLE
        # auth-family error escaping the spine re-raises UNCHANGED out of
        # pull_candles — never mapped to the graceful terminal-auth abort.
        from brutex.adapters.groww.errors import GrowwTokenStillStaleError

        def _raise_still_stale(*_a: object, **_k: object) -> object:
            raise GrowwTokenStillStaleError("same dead token", safe_message="stale")

        monkeypatch.setattr(
            "brutex.dashboard.pull_controller.run_groww_spot_ingest", _raise_still_stale
        )
        with pytest.raises(GrowwTokenStillStaleError):
            pull_candles(
                _index(),
                lake_root=tmp_path,
                dry_run=False,
                from_date=_SPOT_FROM,
                to_date=_SPOT_TO,
                intervals=("1day",),
                adapter_factory=lambda root, _inst: (SyntheticSpotAdapter(), _real_sink(root)),
            )

    def test_dry_run_minute_request_pulls_full_range(self, tmp_path: Path) -> None:
        # A dry-run ``both`` request: the minute phase runs the full range too (decoupled
        # — no lake read, no skip). The synthetic dry-run adapter lands both cells.
        report = pull_candles(
            _index(),
            lake_root=tmp_path,
            dry_run=True,
            from_date=_SPOT_FROM,
            to_date=_SPOT_TO,
            intervals=_BOTH_INTERVALS,
        )
        assert report.dry_run is True
        labels = {tf.interval for tf in report.timeframes}
        assert labels == {"1m", "1d"}
        # Both cells complete (synthetic dry-run lands every interval) — nothing skipped.
        assert report.total_completed == 2
        assert report.total_failed == 0
        assert report.total_skipped == 0
        assert report.total_no_data == 0


class TestObserveRate:
    """The live rate-observer + pull-health-monitor push seam (DEC-PULL-HEALTH-MONITOR-001).

    Covers every branch of ``_observe_rate`` (the CI per-module coverage gate runs ONLY
    this file + ``test_dashboard_pull_pool_sizing.py`` for ``pull_controller.py``, so the
    monitor-wiring branches must live HERE, not in ``test_dashboard_rate_panel.py``).
    """

    @staticmethod
    def _controller() -> object:
        from brutex.adapters.rate_common import RateController

        class _FakeController:
            def snapshot(self) -> dict[str, object]:
                return {"current_rps": 5.0, "recent_hits": 0}

        return cast(RateController, _FakeController())

    @staticmethod
    def _adapter_with_meter() -> object:
        class _FakeMeter:
            def snapshot(self) -> dict[str, object]:
                return {"effective_rps": 3.2, "latency_ms_p95": 200.0, "in_flight": 2}

        class _FakeClient:
            meter = _FakeMeter()

        class _FakeAdapter:
            client = _FakeClient()

        return cast(MarketAdapter, _FakeAdapter())

    def test_skips_when_no_controller(self) -> None:
        """Dry-run / injected-factory pull (no controller) → early return, nothing called."""
        from brutex.dashboard.pull_controller import _observe_rate

        calls: list[object] = []
        _observe_rate(
            lambda r, m: calls.append((r, m)),
            None,
            cast(MarketAdapter, object()),
        )
        assert calls == []

    def test_skips_when_no_observer_and_no_monitor(self) -> None:
        """Controller present but neither observer nor monitor wired → early return."""
        from brutex.dashboard.pull_controller import _observe_rate

        # A controller whose snapshot must NEVER be called on the skip path.
        class _ExplodingController:
            def snapshot(self) -> dict[str, object]:
                raise AssertionError("snapshot must not be read on the skip path")

        from brutex.adapters.rate_common import RateController

        _observe_rate(
            None,
            cast(RateController, _ExplodingController()),
            cast(MarketAdapter, object()),
            None,
        )

    def test_pushes_to_observer_and_monitor(self) -> None:
        """Active path: builds both snapshots ONCE and feeds the observer + the monitor."""
        from brutex.dashboard.pull_controller import _observe_rate
        from brutex.observability.pull_health import PullHealthMonitor

        class _RecordingMonitor:
            def __init__(self) -> None:
                self.observed: list[tuple[object, object]] = []

            def observe(self, ctrl: object, meter: object) -> None:
                self.observed.append((ctrl, meter))

        obs_calls: list[tuple[object, object]] = []
        monitor = _RecordingMonitor()
        _observe_rate(
            lambda r, m: obs_calls.append((r, m)),
            self._controller(),  # type: ignore[arg-type]
            self._adapter_with_meter(),  # type: ignore[arg-type]
            cast(PullHealthMonitor, monitor),
        )
        expected = (
            {"current_rps": 5.0, "recent_hits": 0},
            {"effective_rps": 3.2, "latency_ms_p95": 200.0, "in_flight": 2},
        )
        assert obs_calls == [expected]
        assert monitor.observed == [expected]

    def test_observer_only_no_monitor(self) -> None:
        """Observer wired, monitor None → the observer fires, the monitor branch is skipped."""
        from brutex.dashboard.pull_controller import _observe_rate

        obs_calls: list[tuple[object, object]] = []
        _observe_rate(
            lambda r, m: obs_calls.append((r, m)),
            self._controller(),  # type: ignore[arg-type]
            self._adapter_with_meter(),  # type: ignore[arg-type]
            None,
        )
        assert obs_calls == [
            (
                {"current_rps": 5.0, "recent_hits": 0},
                {"effective_rps": 3.2, "latency_ms_p95": 200.0, "in_flight": 2},
            )
        ]

    def test_monitor_only_no_meter(self) -> None:
        """Monitor wired, observer None, adapter has no meter → empty meter snapshot."""
        from brutex.dashboard.pull_controller import _observe_rate
        from brutex.observability.pull_health import PullHealthMonitor

        class _RecordingMonitor:
            def __init__(self) -> None:
                self.observed: list[tuple[object, object]] = []

            def observe(self, ctrl: object, meter: object) -> None:
                self.observed.append((ctrl, meter))

        monitor = _RecordingMonitor()
        # A bare object() adapter → no `.client.meter` → meter snapshot defaults to {}.
        _observe_rate(
            None,
            self._controller(),  # type: ignore[arg-type]
            cast(MarketAdapter, object()),
            cast(PullHealthMonitor, monitor),
        )
        assert monitor.observed == [({"current_rps": 5.0, "recent_hits": 0}, {})]


class TestEmitPullPoolSize:
    """`_emit_pull_pool_size` — the once-per-pull `pull.pool_size` telemetry.

    DEC-LAKE-WRITE-INSTRUMENT-001: the 28-hour pull-log forensic could not see the
    resolved connection-pool width (it inferred a wrong "pool=2" from `in_flight`
    peaks that actually reached 7), so the resolved pool size is now logged once at
    pull start to pair with the per-partition `lake.partition.write` latency.
    """

    @staticmethod
    def _pool_events(logs: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
        return [e for e in logs if e.get("event") == "pull.pool_size"]

    def test_emits_resolved_pool_size(self) -> None:
        """One `pull.pool_size` event carrying the real `resolve_pool_size()`."""
        from brutex.adapters.groww.constants import resolve_pool_size

        with capture_logs() as logs:
            _emit_pull_pool_size("pull-xyz")

        events = self._pool_events(logs)
        assert len(events) == 1
        assert events[0]["log_level"] == "info"
        assert events[0]["pull_id"] == "pull-xyz"
        assert events[0]["pool_size"] == resolve_pool_size()

    def test_respects_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The emitted size tracks `BRUTEX_GROWW_POOL_SIZE` (the env knob)."""
        monkeypatch.setenv("BRUTEX_GROWW_POOL_SIZE", "3")
        with capture_logs() as logs:
            _emit_pull_pool_size("pull-env")
        events = self._pool_events(logs)
        assert len(events) == 1
        assert events[0]["pool_size"] == 3

    def test_failure_is_swallowed_and_logged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unexpected error during resolution NEVER crashes the pull — it is logged.

        `resolve_pool_size` is a pure env-read but the call is guarded; a forced
        raise must yield a `pull.pool_size_emit_failed` log instead of propagating.
        """

        def _boom() -> int:
            raise RuntimeError("simulated pool-size resolution failure")

        # Patch the symbol the helper imports lazily inside its try block.
        monkeypatch.setattr("brutex.adapters.groww.constants.resolve_pool_size", _boom)
        with capture_logs() as logs:
            _emit_pull_pool_size("pull-boom")  # must not raise

        assert self._pool_events(logs) == []  # no success event
        failed = [e for e in logs if e.get("event") == "pull.pool_size_emit_failed"]
        assert len(failed) == 1
        assert failed[0]["pull_id"] == "pull-boom"


# ────────────────────────────────────────────────────────────────────────
# Forensic-audit #7 — cold-start lake reconcile BEFORE plan (absent-only seed)
# Forensic-audit #15 — the 1-day derive runs AFTER the pipeline/consumer drains
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _CountingAdapter(SyntheticSpotAdapter):
    """Counts every ``fetch_ohlcv`` call — proves a lake-seeded window is never re-fetched."""

    calls: list[tuple[str, str, str]] = field(default_factory=list)

    def fetch_ohlcv(
        self, *, symbol: str, exchange: Exchange, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        """Record the call, then return the parent's synthetic bars (no zero-arg super —
        ``dataclass(slots=True)`` recreates the class, breaking the implicit cell)."""
        del exchange, end
        self.calls.append((symbol, interval, start.isoformat()))
        return synthetic_spot_bars(start, count=self.bars_per_window)


def _seed_lake_bars(lake_root: Path, inst: Instrument, interval: str, days: Sequence[date]) -> None:
    """Write real lake bars for *days* so the stored span ENCLOSES the pull's window."""
    sink = _real_sink(lake_root)
    for day in days:
        sink.append(
            exchange=inst.exch_id.value,
            segment=inst.segment.value,
            symbol=inst.symbol_name,
            interval=interval,
            bars=synthetic_spot_bars(day),
        )


#: The cold-start window: an explicit past range (no live clamp) whose single
#: 30-day 1-minute tile / 180-day 1-day tile is (2024-01-01, 2024-01-06).
_COLD_DAYS = [date(2024, 1, 1) + timedelta(days=n) for n in range(6)]


class TestColdStartLakeReconcile:
    """Forensic-audit #7: ``reconcile_ledger_with_lake`` must run BEFORE ``plan_windows``.

    The cold-start seed (``WindowLedger.seed_lake_success``) is ABSENT-ONLY — it never
    overwrites an existing row, a ``PLANNED`` one included — so planning first made the
    seed a permanent no-op: on a lost/corrupt ledger over a fully-populated lake EVERY
    window was re-fetched. These regressions pin the fixed order at all four call sites
    (``pull_all_spot`` + ``pull_candles``, minute + dormant daily blocks): an enclosed
    window is seeded (settled rows > 0) and the vendor is never asked again (0 fetches).
    """

    def test_pull_all_spot_cold_start_minute_seeded_not_refetched(self, tmp_path: Path) -> None:
        inst = _index("13")
        _seed_lake_bars(tmp_path, inst, "1minute", _COLD_DAYS)
        adapter = _CountingAdapter()
        pull_all_spot(
            [inst],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            from_date="2024-01-01",
            to_date="2024-01-05",
            adapter_factory=lambda _root, _insts: (adapter, _real_sink(_root)),
        )
        # The lake encloses the whole tiling → the seed settles every window BEFORE the
        # plan, so the pull SKIPS them all — zero vendor fetches on a cold start.
        assert adapter.calls == []
        from brutex.data.window_ledger_store import load_window_ledger

        prog = load_window_ledger(tmp_path).progress_counts()
        assert prog.total > 0  # the seed created rows (seed count > 0)
        assert prog.settled == prog.total  # every one settled WITHOUT a fetch

    def test_pull_all_spot_cold_start_daily_seeded_not_refetched(self, tmp_path: Path) -> None:
        # The dormant daily block (a defensive caller injecting intervals=("1day",)).
        inst = _index("13")
        _seed_lake_bars(tmp_path, inst, "1day", _COLD_DAYS)
        adapter = _CountingAdapter()
        pull_all_spot(
            [inst],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            from_date="2024-01-01",
            to_date="2024-01-05",
            intervals=("1day",),
            adapter_factory=lambda _root, _insts: (adapter, _real_sink(_root)),
        )
        assert adapter.calls == []
        from brutex.data.window_ledger_store import load_window_ledger

        prog = load_window_ledger(tmp_path).progress_counts()
        assert prog.total > 0
        assert prog.settled == prog.total

    def test_pull_candles_cold_start_minute_seeded_not_refetched(self, tmp_path: Path) -> None:
        inst = _index("13")
        _seed_lake_bars(tmp_path, inst, "1minute", _COLD_DAYS)
        adapter = _CountingAdapter()
        pull_candles(
            inst,
            lake_root=tmp_path,
            from_date="2024-01-01",
            to_date="2024-01-05",
            dry_run=False,
            adapter_factory=lambda _root, _inst: (adapter, _real_sink(_root)),
        )
        assert adapter.calls == []
        from brutex.data.window_ledger_store import load_window_ledger

        prog = load_window_ledger(tmp_path).progress_counts()
        assert prog.total > 0
        assert prog.settled == prog.total

    def test_pull_candles_cold_start_daily_seeded_not_refetched(self, tmp_path: Path) -> None:
        inst = _index("13")
        _seed_lake_bars(tmp_path, inst, "1day", _COLD_DAYS)
        adapter = _CountingAdapter()
        pull_candles(
            inst,
            lake_root=tmp_path,
            from_date="2024-01-01",
            to_date="2024-01-05",
            dry_run=False,
            intervals=("1day",),
            adapter_factory=lambda _root, _inst: (adapter, _real_sink(_root)),
        )
        assert adapter.calls == []
        from brutex.data.window_ledger_store import load_window_ledger

        prog = load_window_ledger(tmp_path).progress_counts()
        assert prog.total > 0
        assert prog.settled == prog.total


class _DeferredPipeline:
    """A ``FetchPipeline`` stand-in that defers EVERY submitted tail until ``drain()``.

    The deterministic twin of the real (default-ON) async pool's worst-case timing:
    the producer's ``submit`` only queues, so NO bar reaches the lake until ``drain``
    runs the queued tails — exactly the window forensic-audit #15 exploits (a pre-drain
    resample derived the 1-day anchor from an INCOMPLETE 1-minute lake).
    """

    def __init__(self) -> None:
        self.pending: list[RawFetchResult] = []
        self.drain_count = 0

    def submit(self, result: RawFetchResult) -> None:
        """Queue the tail — never process it on the producer's clock."""
        self.pending.append(result)

    def fold_totals(
        self,
        *,
        bars_appended: int,
        conflicts: int,
        series_key: tuple[str, str],
        vendor_dup_rows_dropped: int = 0,
        max_close_delta: object = None,
    ) -> None:
        """Accept the tail's totals (unused — the test asserts on the lake itself)."""

    def fold_tail_transient(self, series_key: tuple[str, str]) -> None:
        """Accept a deferred-window tally (unused)."""

    def per_series_totals(self) -> dict[tuple[str, str], object]:
        """No folded totals — the producer reports stand (the lake is the oracle)."""
        return {}

    def drain(self) -> None:
        """Run every queued tail NOW — the durable writes land here, not before."""
        self.drain_count += 1
        pending, self.pending = self.pending, []
        for result in pending:
            result.tail.process(result.tail)


class TestNoMidPullDerivedWrites:
    """DEC-PIPELINE-AUTOPREBUILD-001: the pull writes NO derived data mid-pull.

    REGRESSION FLIP of the old ``TestDailyDeriveAfterDrain`` (forensic-audit
    #15): the always-on post-drain 1-day derive (``_resample_daily_after_pull``)
    is REMOVED — derived timeframes (incl. the ``1day`` anchor) are written ONLY
    by the pipeline's final prebuild phase, never by ``pull_all_spot`` /
    ``pull_candles``. These tests pin that a real pull lands the 1-minute bars
    but leaves NO ``1day`` (or any other derived) partition behind.
    """

    def test_pull_all_spot_writes_no_daily_partition(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pipeline = _DeferredPipeline()
        monkeypatch.setattr(
            "brutex.dashboard.pull_controller._build_fetch_pipeline",
            lambda **_kw: pipeline,
        )
        pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            from_date="2024-01-01",
            to_date="2024-01-05",
            adapter_factory=_real_bulk_factory(SyntheticSpotAdapter()),
        )
        assert pipeline.drain_count >= 1
        assert pipeline.pending == []  # every queued tail was drained durably
        minute_files = sorted(tmp_path.glob("bars/*/*/*/1minute/*/*.parquet"))
        assert minute_files  # the drained tails DID land 1-minute bars
        # The decree's pin: NO mid-pull derived write — the 1day partition is
        # written only by the pipeline prebuild, which did not run here.
        assert not list(tmp_path.glob("bars/*/*/*/1day/**")), (
            "pull_all_spot must not write a derived 1day partition mid-pull "
            "(DEC-PIPELINE-AUTOPREBUILD-001)"
        )

    def test_pull_candles_writes_no_daily_partition(self, tmp_path: Path) -> None:
        pull_candles(
            _index("13"),
            lake_root=tmp_path,
            from_date="2024-01-01",
            to_date="2024-01-05",
            dry_run=False,
            adapter_factory=lambda _root, _inst: (SyntheticSpotAdapter(), _real_sink(tmp_path)),
        )
        assert sorted(tmp_path.glob("bars/*/*/*/1minute/*/*.parquet"))
        assert not list(tmp_path.glob("bars/*/*/*/1day/**")), (
            "pull_candles must not write a derived 1day partition mid-pull "
            "(DEC-PIPELINE-AUTOPREBUILD-001)"
        )


# ────────────────────────────────────────────────────────────────────────
# DEC-WINDOW-LEDGER-RESCUE-001 — visible skips + loud ledger-reload failure (spot)
# ────────────────────────────────────────────────────────────────────────


class TestPullAllLedgerRescueSurface:
    """The spot bulk pull reports its settled-window skips + a failed ledger reload."""

    def test_second_pull_reports_skipped_settled_windows(self, tmp_path: Path) -> None:
        # Run 1 settles the range SUCCESS (real sink + ledger, dry_run=False with an
        # injected synthetic adapter); run 2 skips the settled window WITHOUT a
        # vendor hit and SAYS so — in the report, its dict, the spine INFO line,
        # and the pull-done INFO. The range is bounded to 2026-02-06 so the
        # synthetic adapter's first-day-only bars stay WITHIN the 5-session
        # truncation tolerance: a live bulk pull now arms
        # ``detect_completeness_gaps`` (DEC-BULK-SPOT-COMPLETENESS-FLAG-001), and
        # the original 2026-02-13 range (9 missing sessions) would legitimately
        # DEMOTE the index window (SUSPECTED_PARTIAL) instead of settling it —
        # this test pins the SETTLED-window skip law, so the fixture must settle.
        first = pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            from_date="2026-02-02",
            to_date="2026-02-06",
            adapter_factory=_real_bulk_factory(SyntheticSpotAdapter()),
        )
        assert first.windows_skipped == 0  # a fresh lake has nothing skippable
        with capture_logs() as logs:
            second = pull_all_spot(
                [_index("13")],
                lake_root=tmp_path,
                dry_run=False,
                now=_BULK_NOW,
                from_date="2026-02-02",
                to_date="2026-02-06",
                adapter_factory=_real_bulk_factory(SyntheticSpotAdapter()),
            )
        # One symbol × the 1-minute default interval × one 30-day tile = 1 window.
        assert second.windows_skipped == 1
        assert second.to_dict()["windows_skipped"] == 1
        spine = [e for e in logs if e["event"] == "ingest.windows_skipped_settled"]
        assert spine and spine[-1]["windows_skipped_settled"] == 1
        done = [e for e in logs if e["event"] == "universe.pull_all.done"]
        assert done and done[0]["windows_skipped"] == 1

    def test_ledger_reload_failure_is_loud(self, tmp_path: Path) -> None:
        # A corrupt (unreadable) persisted ledger: the pull still runs (full
        # re-verify — never a crash) but the report + WARNING announce it.
        from brutex.data.window_ledger_store import ledger_path

        ledger_file = ledger_path(tmp_path)
        ledger_file.parent.mkdir(parents=True, exist_ok=True)
        ledger_file.write_bytes(b"not a parquet file at all")
        with capture_logs() as logs:
            report = pull_all_spot(
                [_index("13")],
                lake_root=tmp_path,
                dry_run=False,
                now=_BULK_NOW,
                from_date="2026-02-02",
                to_date="2026-02-03",
                adapter_factory=_real_bulk_factory(SyntheticSpotAdapter()),
            )
        assert report.ledger_load_failed is True
        assert report.to_dict()["ledger_load_failed"] is True
        assert any(e["event"] == "universe.pull_all.ledger_reload_failed" for e in logs)
        assert report.total_completed == 1  # the full re-verify pull still ran

    def test_dry_run_never_reads_the_ledger(self, tmp_path: Path) -> None:
        # A dry-run has NO ledger (begin_pull_ledger → None), so a corrupt file on
        # disk is never read and neither rescue field fires (the honest default).
        from brutex.data.window_ledger_store import ledger_path

        ledger_file = ledger_path(tmp_path)
        ledger_file.parent.mkdir(parents=True, exist_ok=True)
        ledger_file.write_bytes(b"junk")
        report = pull_all_spot([_index("13")], lake_root=tmp_path, dry_run=True, now=_BULK_NOW)
        assert report.ledger_load_failed is False
        assert report.windows_skipped == 0


class TestMassEmptyCircuitBreaker:
    """DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001 — the bulk pull's mass-empty halt + honest verdicts."""

    def test_trip_halts_bulk_pull_with_needs_operator_verdict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An all-empty vendor (the R2 throttle-from-START shape) with a trip-at-the-
        # 2nd-observation FRACTION breaker (env-injected — the DYNAMIC config seam;
        # an all-empty fresh pull can only trip the fraction rule — the consecutive
        # rule requires a post-data empty, pinned in test_empty_circuit_breaker.py):
        # the 1day phase trips after two confirmed-empty windows → the pull HALTS
        # with the loud, needs-operator mass_empty_halt verdict naming the counts,
        # the minute phase is SKIPPED (the global-stop guard), the windows settled
        # before the trip are surfaced, AND the CRITICAL Telegram pull-halt alert
        # fires from the catch site (minor M-3 — the halt never escapes to the job
        # layer's alerting path, so parity is wired here).
        monkeypatch.setenv("BRUTEX_EMPTY_BREAKER_CONSECUTIVE", "0")
        monkeypatch.setenv("BRUTEX_EMPTY_BREAKER_MIN_OBSERVATIONS", "2")
        monkeypatch.setenv("BRUTEX_EMPTY_BREAKER_FRACTION", "1.0")
        # A single-symbol pull is one series per phase — arm the fraction floor at 1
        # for the test (production's 10-series floor is what keeps a small targeted
        # pull from halting; pinned in test_empty_circuit_breaker.py).
        monkeypatch.setenv("BRUTEX_EMPTY_BREAKER_FRACTION_MIN_SERIES", "1")
        alerts: list[Exception] = []
        monkeypatch.setattr("brutex.dashboard.pull_controller.emit_pull_halt_alert", alerts.append)
        report = pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            intervals=_BOTH_INTERVALS,
            adapter_factory=lambda _root, _insts: (_EmptyAdapter(), NullBarSink()),
        )
        verdict = report.verdict
        assert verdict.kind == "mass_empty_halt"
        assert verdict.needs_operator is True
        assert report.mass_empty_cause is not None
        # The halt's own count-naming message is relayed verbatim into the action
        # (exact counts vary with worker completion order under the real pool — the
        # in-flight workers keep raising once tripped, which is the design).
        assert re.search(r"\d+ of \d+ fetched windows", verdict.action)
        assert "reopen-skipped" in verdict.action  # the re-verify path
        # Whether a window settled BEFORE the trip depends on worker completion order
        # under the real pool (after the trip the record-time suppression blocks every
        # settle — pinned deterministically at the spine level). The invariant here is
        # HONESTY: any settle that did land is counted AND named on the verdict.
        if report.settled_skipped:
            assert f"{report.settled_skipped} window(s)" in verdict.action
        assert report.to_dict()["verdict"]["kind"] == "mass_empty_halt"
        # Minor M-3: the dashboard spot halt fires the SAME CRITICAL Telegram alert
        # the escaping-exception job path fires — exactly once per halt.
        assert len(alerts) == 1
        from brutex.adapters.empty_circuit_breaker import MassEmptySettleHalt

        assert isinstance(alerts[0], MassEmptySettleHalt)

    def test_below_threshold_settles_produce_loud_verdict_not_silence(self, tmp_path: Path) -> None:
        # Mixed real+empty BELOW the default thresholds (the register's "mixed" case):
        # the pull completes — but because windows were permanently settled SKIPPED,
        # the verdict must SAY so (count + how to re-verify), never a bare
        # "No action needed" (honest-verdict requirement 3).
        report = pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            intervals=_BOTH_INTERVALS,
            adapter_factory=lambda _root, _insts: (_DailyGapAdapter(), NullBarSink()),
        )
        verdict = report.verdict
        assert verdict.kind == "clean"  # completes — no halt, no failure
        assert report.settled_skipped > 0  # the old 1day empties fast-settled
        assert f"{report.settled_skipped} window(s)" in verdict.action
        assert "reopen-skipped" in verdict.action
        assert verdict.action != "No action needed."
        assert verdict.to_dict()["settled_skipped"] == report.settled_skipped

    def test_no_settles_keeps_clean_verdict_byte_identical(self, tmp_path: Path) -> None:
        # A pull that settles nothing keeps the exact prior clean verdict — the
        # honesty note appears ONLY when settles happened.
        report = pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            intervals=_BOTH_INTERVALS,
            adapter_factory=_real_bulk_factory(SyntheticSpotAdapter()),
        )
        assert report.verdict.kind == "clean"
        assert report.settled_skipped == 0
        assert report.verdict.action == "No action needed."


class TestWrittenSettledCensusWired:
    """DEC-WRITTEN-SETTLED-CENSUS-001 (spot spine): the observe-only census is
    LIVE in ``pull_all_spot`` — a pre-pull ledger↔lake disagreement surfaces as
    exactly one ``pull.written_settled_mismatch`` WARNING; the pull itself is
    untouched (DETECT+WARN only)."""

    def test_spot_pull_emits_mismatch_warning(self, tmp_path: Path) -> None:
        from brutex.data.fetch_coverage import WindowKey
        from brutex.data.window_ledger import WindowLedger
        from brutex.data.window_ledger_store import save_window_ledger
        from structlog.testing import capture_logs

        # The durable ledger claims symbol 13's 1-minute series settled SUCCESS
        # while the lake holds NO bars for it (the lost-partition shape).
        seeded = WindowLedger()
        seeded.record(
            WindowKey(
                market="groww",
                security_id="13",
                interval="1minute",
                start=date(2024, 1, 1),
                end=date(2024, 1, 31),
            ),
            raw_bars=5,
            error_type=None,
            bars_appended=5,
            now=_BULK_NOW,
        )
        save_window_ledger(seeded, tmp_path)

        with capture_logs() as logs:
            pull_all_spot(
                [_index("13")],
                lake_root=tmp_path,
                dry_run=False,
                now=_BULK_NOW,
                from_date="2024-01-01",
                to_date="2024-01-31",
                intervals=("1minute",),
                adapter_factory=lambda _root, _insts: (SyntheticSpotAdapter(), NullBarSink()),
            )
        warnings = [log for log in logs if log["event"] == "pull.written_settled_mismatch"]
        assert len(warnings) == 1
        assert warnings[0]["source"] == "spot"
        assert warnings[0]["settled_unwritten"] == 1
        assert warnings[0]["samples"] == ["settled-unwritten: '13' '1minute'"]

    def test_agreeing_pull_stays_silent(self, tmp_path: Path) -> None:
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            pull_all_spot(
                [_index("13")],
                lake_root=tmp_path,
                dry_run=False,
                now=_BULK_NOW,
                from_date="2024-01-01",
                to_date="2024-01-31",
                intervals=("1minute",),
                adapter_factory=lambda _root, _insts: (SyntheticSpotAdapter(), NullBarSink()),
            )
        assert not [log for log in logs if log["event"] == "pull.written_settled_mismatch"]


class TestSkippedAuditWired:
    """DEC-SKIPPED-AUDIT-ROUNDROBIN-001: the mechanized ≤25-window round-robin
    re-audit of terminally-SKIPPED windows is LIVE in ``pull_all_spot`` — once
    per pull_id, cursor-persisted, best-effort."""

    @staticmethod
    def _seed_skipped_option_row(tmp_path: Path) -> None:
        """A terminally-SKIPPED OPTION-market row: audited by the spot pull's
        seam but never re-fetched by the spot spine — so the demotion is
        directly observable in the persisted ledger after the pull."""
        from brutex.data.fetch_coverage import WindowKey
        from brutex.data.window_ledger import WindowLedger
        from brutex.data.window_ledger_store import save_window_ledger

        seeded = WindowLedger()
        seeded.record(
            WindowKey(
                market="groww-option",
                security_id="NSE-NIFTY-25Jan24-21000-CE",
                interval="1minute",
                start=date(2024, 1, 1),
                end=date(2024, 1, 26),
            ),
            raw_bars=0,
            error_type=None,
            bars_appended=0,
            now=_BULK_NOW,
            settle_empty=True,
        )
        save_window_ledger(seeded, tmp_path)

    def _pull(self, tmp_path: Path) -> None:
        pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            from_date="2024-01-01",
            to_date="2024-01-31",
            intervals=("1minute",),
            adapter_factory=lambda _root, _insts: (SyntheticSpotAdapter(), NullBarSink()),
        )

    def test_pull_reopens_a_skipped_window_and_persists_the_cursor(self, tmp_path: Path) -> None:
        from brutex.data.window_ledger import REOPENED_MARKER
        from brutex.data.window_ledger_store import (
            load_skipped_audit_cursor,
            load_window_ledger,
        )
        from structlog.testing import capture_logs

        self._seed_skipped_option_row(tmp_path)
        with capture_logs() as logs:
            self._pull(tmp_path)
        audits = [log for log in logs if log["event"] == "pull.skipped_audit"]
        assert len(audits) == 1 and audits[0]["reopened"] == 1
        persisted = load_window_ledger(tmp_path)
        row = next(r for r in persisted.records() if r.security_id == "NSE-NIFTY-25Jan24-21000-CE")
        # Demoted FAILED + REOPENED (breaker-exempt) and NOT resurrected by the
        # pull-end merge-on-save (the reopened tombstone) — re-fetchable by the
        # NEXT F&O phase. Stored bars untouched by construction (DATA-SAFE).
        assert row.status == "FAILED"
        assert row.last_error == REOPENED_MARKER
        cursor = load_skipped_audit_cursor(tmp_path)
        assert cursor is not None and cursor[1] is not None
        assert cursor[1][1] == "NSE-NIFTY-25Jan24-21000-CE"

    def test_second_pull_re_audits_under_its_own_pull_id(self, tmp_path: Path) -> None:
        """Each pull mints its OWN pull_id, so a NEW pull re-runs the audit —
        and having demoted the only SKIPPED row on pull 1, pull 2 honestly
        re-opens 0 (the audit converges; the once-per-pull_id short-circuit
        itself is unit-pinned in TestSkippedAuditStore)."""
        from structlog.testing import capture_logs

        self._seed_skipped_option_row(tmp_path)
        with capture_logs() as logs:
            self._pull(tmp_path)
            self._pull(tmp_path)  # a NEW pull_id → audits again, nothing left
        audits = [log for log in logs if log["event"] == "pull.skipped_audit"]
        assert [a["reopened"] for a in audits] == [1, 0]


# ────────────────────────────────────────────────────────────────────────
# Vendor-floor learner wiring (DEC-VENDOR-FLOOR-LAZY-PROBE-001 review folds)
# ────────────────────────────────────────────────────────────────────────
#
# BLOCKER pin: the live pool leaves carry a NO-OP pace (`build_live_pool`
# without a `pace`), and the session RateController is bound only via
# `run_ingest.with_paced_client` INSIDE the spine — so a learner built over the
# RAW adapter probed UNPACED (outside the 8 r/s ceiling + 500/min governor).
# Both build sites must construct the learner over
# `with_paced_client(adapter, pace)`; these tests pin that BEHAVIORALLY (the
# storm-breaker arming-wiring pattern: patch the maker in the site's namespace,
# capture the adapter it receives, assert the pace identity), because the
# build-site ternary is a single statement — 100% line+branch held even when
# the paced wrap was missing.
#
# MINOR pin: the load→clamp→plan composition over a REAL InstrumentStore
# (`schema_meta` set_meta/get_meta — runtime Protocol conformance, not just
# mypy) end-to-end through a real pull: the clamped symbol's 1-minute fetches
# start at the learned floor, the other symbol keeps the full range, and the
# 1-day phase stays unclamped.

_VF_HEADER = ",".join(INSTRUMENT_CSV_COLUMNS)


def _vf_csv_row(**over: str) -> str:
    cells = dict.fromkeys(INSTRUMENT_CSV_COLUMNS, "")
    cells.update(over)
    return ",".join(cells[c] for c in INSTRUMENT_CSV_COLUMNS)


_VF_NIFTY_ROW = _vf_csv_row(
    exchange="NSE",
    exchange_token="NIFTY",
    trading_symbol="NIFTY",
    groww_symbol="NSE-NIFTY",
    name="NIFTY 50",
    instrument_type="IDX",
    segment="CASH",
)
_VF_INDEX = load_index_from_csv("\n".join([_VF_HEADER, _VF_NIFTY_ROW]) + "\n")


class _VFEmptySDK:
    """``GrowwSDK``-shaped fake — every historical-candle window clean-empty."""

    def get_historical_candles(self, **_kwargs: object) -> Mapping[str, object]:
        return {"candles": []}

    def get_expiries(self, **_kwargs: object) -> Mapping[str, object]:
        return {}

    def get_contracts(self, **_kwargs: object) -> Mapping[str, object]:
        return {}


def _vf_groww_adapter() -> GrowwMarketAdapter:
    return GrowwMarketAdapter(
        client=GrowwClient(sdk=_VFEmptySDK(), sleep=lambda _s: None),
        index=_VF_INDEX,
    )


def _vf_fake_runtime(pace_hook: Callable[[], None]) -> Callable[..., object]:
    """A `_live_rate_runtime` stand-in returning a REAL pace hook (live-pull shape)."""

    def _runtime(_use_live_rate: bool, _session_rate: object = None) -> object:
        return ((pace_hook, None, None, None), None, None)

    return _runtime


class TestVendorFloorPacedWiring:
    def test_bulk_learner_is_built_over_the_paced_adapter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BLOCKER regression (pull_all_spot): the learner's adapter is the PACED
        replica — its client's `pace` IS the session hook, never the raw adapter."""
        captured: list[MarketAdapter] = []

        def _pace_hook() -> None:  # identity-comparable (a plain function, not a bound method)
            return None

        def _recording_maker(
            adapter: MarketAdapter,
            *,
            history_floor: date,
            today: date,
            store: object = None,
        ) -> VendorFloorLearner | None:
            del history_floor, today, store
            captured.append(adapter)
            return None  # spine accepts a None learner — the probe itself is off

        raw = _vf_groww_adapter()
        monkeypatch.setattr(pull_controller, "make_vendor_floor_learner", _recording_maker)
        monkeypatch.setattr(pull_controller, "_live_rate_runtime", _vf_fake_runtime(_pace_hook))
        pull_all_spot(
            [_index("NIFTY")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            from_date="2026-02-01",
            to_date="2026-02-15",
            adapter_factory=lambda _root, _insts: (raw, NullBarSink()),
        )
        assert len(captured) == 1
        paced = captured[0]
        assert paced is not raw  # a rebuilt paced replica, never the raw pool adapter
        assert isinstance(paced, GrowwMarketAdapter)
        assert isinstance(paced.client, GrowwClient)
        assert paced.client.pace is _pace_hook  # every probe fetch admits through the bucket

    def test_bulk_dry_run_never_builds_a_learner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The arming gate: a dry-run threads None and never even calls the maker."""

        def _never_maker(*_a: object, **_kw: object) -> VendorFloorLearner:
            raise AssertionError("make_vendor_floor_learner must not run for a dry-run")

        monkeypatch.setattr(pull_controller, "make_vendor_floor_learner", _never_maker)
        report = pull_all_spot([_index("13")], lake_root=tmp_path, dry_run=True, now=_BULK_NOW)
        assert report.total_symbols == 1

    def test_single_symbol_learner_is_built_over_the_paced_adapter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BLOCKER regression (pull_candles): the single-symbol twin of the bulk pin."""
        captured: list[MarketAdapter] = []

        def _pace_hook() -> None:
            return None

        def _recording_maker(
            adapter: MarketAdapter,
            *,
            history_floor: date,
            today: date,
            store: object = None,
        ) -> VendorFloorLearner | None:
            del history_floor, today, store
            captured.append(adapter)
            return None

        raw = _vf_groww_adapter()
        monkeypatch.setattr(pull_controller, "make_vendor_floor_learner", _recording_maker)
        monkeypatch.setattr(pull_controller, "_live_rate_runtime", _vf_fake_runtime(_pace_hook))
        pull_candles(
            _index("NIFTY"),
            lake_root=tmp_path,
            from_date="2026-02-01",
            to_date="2026-02-15",
            dry_run=False,
            adapter_factory=lambda _root, _inst: (raw, NullBarSink()),
        )
        assert len(captured) == 1
        paced = captured[0]
        assert paced is not raw
        assert isinstance(paced, GrowwMarketAdapter)
        assert isinstance(paced.client, GrowwClient)
        assert paced.client.pace is _pace_hook


class _WindowRecordingAdapter(SyntheticSpotAdapter):
    """A synthetic adapter recording every fetched ``(symbol, interval, start, end)``."""

    calls: list[tuple[str, str, date, date]]

    def __init__(self) -> None:
        super().__init__()
        # The parent is a FROZEN slots dataclass — its generated __setattr__
        # refuses plain assignment, so the recorder list is attached via
        # object.__setattr__ (the dataclass-sanctioned escape hatch).
        object.__setattr__(self, "calls", [])

    def fetch_ohlcv(
        self, *, symbol: str, exchange: Exchange, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        self.calls.append((symbol, interval, start, end))  # list.append is GIL-atomic
        return super().fetch_ohlcv(
            symbol=symbol, exchange=exchange, interval=interval, start=start, end=end
        )


class TestVendorFloorPlanningClampEndToEnd:
    def test_persisted_floor_clamps_minute_fetches_via_a_real_instrument_store(
        self, tmp_path: Path
    ) -> None:
        """MINOR regression: the load→clamp→plan→fetch composition runs END-TO-END
        with a REAL ``InstrumentStore`` as the ``floor_store`` (runtime Protocol
        conformance + the symbol_name keying proven, not just statically):

        * the symbol with a persisted ``groww.vendor_floor.<symbol_name>`` plans +
          FETCHES its 1-minute range from the learned floor;
        * the other symbol keeps the full requested range;
        * the 1-DAY phase stays UNCLAMPED (the ``symbol_windows`` seam is
          sub-daily-only per ``_resolve_spot_window``).
        """
        adapter = _WindowRecordingAdapter()
        lake = tmp_path / "lake"
        lake.mkdir()
        with open_instrument_store(tmp_path / "master.duckdb") as store:
            store.set_meta(vendor_floor_key("13"), "2026-02-08")
            report = pull_all_spot(
                [_index("13"), _index("25")],
                lake_root=lake,
                dry_run=False,
                now=_BULK_NOW,
                from_date="2026-02-01",
                to_date="2026-02-15",
                intervals=_BOTH_INTERVALS,
                adapter_factory=_real_bulk_factory(adapter),
                floor_store=store,
            )
        minute_13 = [(s, e) for sym, iv, s, e in adapter.calls if sym == "13" and iv == "1minute"]
        minute_25 = [(s, e) for sym, iv, s, e in adapter.calls if sym == "25" and iv == "1minute"]
        daily_13 = [(s, e) for sym, iv, s, e in adapter.calls if sym == "13" and iv == "1day"]
        assert minute_13 and minute_25 and daily_13
        # The learned floor clamps ONLY symbol 13's 1-minute planning start.
        assert min(s for s, _ in minute_13) == date(2026, 2, 8)
        # The clamp narrows ONLY the start — both symbols share the tiler's
        # (to_date + 1 day) fetch end.
        assert max(e for _, e in minute_13) == max(e for _, e in minute_25) == date(2026, 2, 16)
        # The other symbol keeps the full requested range.
        assert min(s for s, _ in minute_25) == date(2026, 2, 1)
        # The 1-day phase is NEVER clamped (daily anchor stays full-range).
        assert min(s for s, _ in daily_13) == date(2026, 2, 1)
        assert report.total_failed == 0


# ────────────────────────────────────────────────────────────────────────
# Observed-sessions overlay arming (DEC-CALENDAR-OBSERVED-SESSIONS-001)
# ────────────────────────────────────────────────────────────────────────


def _write_overlay_sidecar(lake_root: Path, day: str = "2026-05-12") -> frozenset[date]:
    """Write a minimal valid overlay sidecar; return the expected loaded set."""
    import json

    from brutex.calendars.observed_sessions import (
        OBSERVED_LABEL,
        OBSERVED_OVERLAY_VERSION,
        overlay_path,
    )

    path = overlay_path(lake_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": OBSERVED_OVERLAY_VERSION,
                "entries": [
                    {
                        "date": day,
                        "detected_at": "2026-07-04T00:00:00+00:00",
                        "month": day[:7],
                        "label": OBSERVED_LABEL,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return frozenset({date.fromisoformat(day)})


class TestObservedHolidaysArming:
    """The overlay is loaded ONCE per pull and threaded into the ingest spine —
    live pull only (a dry-run threads the empty overlay, mirroring the
    ``detect_completeness_gaps`` gating pattern)."""

    def test_pull_all_spot_dry_run_threads_empty_overlay(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.dashboard import pull_controller as pc

        expected = _write_overlay_sidecar(tmp_path)
        assert expected  # the sidecar IS present — the dry-run must still ignore it
        captured: list[object] = []
        real = pc.run_groww_spot_ingest_with_retry

        def _spy(*args: object, **kwargs: object) -> object:
            captured.append(kwargs.get("observed_holidays"))
            return real(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(pc, "run_groww_spot_ingest_with_retry", _spy)
        pull_all_spot([_index("13")], lake_root=tmp_path, dry_run=True, now=_BULK_NOW)
        assert captured
        assert all(observed == frozenset() for observed in captured)

    def test_pull_all_spot_live_threads_loaded_overlay(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.dashboard import pull_controller as pc

        expected = _write_overlay_sidecar(tmp_path)
        captured: list[object] = []
        real = pc.run_groww_spot_ingest_with_retry

        def _spy(*args: object, **kwargs: object) -> object:
            captured.append(kwargs.get("observed_holidays"))
            return real(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(pc, "run_groww_spot_ingest_with_retry", _spy)
        pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            adapter_factory=_real_bulk_factory(SyntheticSpotAdapter()),
        )
        assert captured
        assert all(observed == expected for observed in captured)

    def test_pull_candles_live_threads_loaded_overlay(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.dashboard import pull_controller as pc

        expected = _write_overlay_sidecar(tmp_path)
        captured: list[object] = []
        real = pc.run_groww_spot_ingest

        def _spy(*args: object, **kwargs: object) -> object:
            captured.append(kwargs.get("observed_holidays"))
            return real(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(pc, "run_groww_spot_ingest", _spy)
        pull_candles(
            _index(),
            lake_root=tmp_path,
            dry_run=False,
            from_date="2021-01-04",
            to_date="2021-01-08",
            adapter_factory=lambda root, _inst: (SyntheticSpotAdapter(), _real_sink(root)),
        )
        assert captured
        assert all(observed == expected for observed in captured)

    def test_pull_candles_dry_run_threads_empty_overlay(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.dashboard import pull_controller as pc

        _write_overlay_sidecar(tmp_path)
        captured: list[object] = []
        real = pc.run_groww_spot_ingest

        def _spy(*args: object, **kwargs: object) -> object:
            captured.append(kwargs.get("observed_holidays"))
            return real(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(pc, "run_groww_spot_ingest", _spy)
        pull_candles(_index(), lake_root=tmp_path, dry_run=True)
        assert captured
        assert all(observed == frozenset() for observed in captured)


class TestMeterLifecycleInvariantDocPin:
    """Grep-based doc pin (DEC-RATE-METER-LIFECYCLE-001 round 3, Golden Rule #1).

    Review rounds 2-3 proved the ORIGINAL #1593 claim — "no job running ⇒ no meter
    registered" — FALSE: the direct off-job ``POST /api/v1/universe/pull`` route
    registers the session meter with no ``BackgroundPullJob`` involved, so "no job"
    never implied "no meter". The corrected, route-clear-backed invariant is scoped
    to "no in-process pull running ⇒ no meter registered". The false wording had
    replicated across FIVE committed claim sites (``rate_sampler`` module +
    ``sample_once`` docstrings, ``pull_jobs``' ``on_run_end`` param doc, ``app``'s
    job-construction comment, ``pull_controller``'s holder field comment) beyond the
    two the fix originally swept — this pin greps the WHOLE dashboard package so the
    over-claim can never re-drift into any module.
    """

    @staticmethod
    def _dashboard_sources() -> dict[str, str]:
        """Each module's source, whitespace/comment-prefix NORMALIZED for phrase greps.

        Docstring re-wraps and ``#``-prefixed comment continuations split the pinned
        phrases across lines (e.g. ``no in-process pull\n        running``), so every
        run of whitespace / ``#`` collapses to one space before matching — the pin is
        wrap-robust in BOTH directions (a re-wrapped re-drift is still caught).
        """
        from brutex import dashboard as dashboard_pkg

        pkg_dir = Path(dashboard_pkg.__file__).resolve().parent
        sources = {
            path.name: re.sub(r"[\s#]+", " ", path.read_text(encoding="utf-8"))
            for path in pkg_dir.glob("*.py")
        }
        assert sources, "dashboard package sources must be readable"
        return sources

    def test_false_unscoped_invariant_absent_everywhere(self) -> None:
        """No dashboard module may claim the falsified job-scoped invariant again."""
        # Built by concatenation so this pin never matches its own docstring source.
        false_job_claim = "no job running" + " ⇒ " + "no meter registered"
        false_between_jobs_claim = "between jobs" + " is ``None`` by invariant"
        offenders = {
            name
            for name, text in self._dashboard_sources().items()
            if false_job_claim in text or false_between_jobs_claim in text
        }
        assert offenders == set(), (
            f"falsified meter-lifecycle over-claim re-drifted into {sorted(offenders)}: "
            "the off-job POST /api/v1/universe/pull route registers a meter with NO job "
            "running — scope the claim to 'no in-process pull running ⇒ no meter "
            "registered' (DEC-RATE-METER-LIFECYCLE-001 rounds 2-3)"
        )

    def test_scoped_invariant_present_at_every_claim_site(self) -> None:
        """The corrected scoped wording anchors each of the swept claim sites."""
        sources = self._dashboard_sources()
        scoped = "no in-process pull running"
        for name in ("pull_controller.py", "app.py", "pull_jobs.py", "rate_sampler.py"):
            assert scoped in sources[name], (
                f"{name} lost the corrected scoped meter-lifecycle invariant wording "
                "(DEC-RATE-METER-LIFECYCLE-001 rounds 2-3)"
            )


# ────────────────────────────────────────────────────────────────────────
# Golden Rule #8 — the dashboard spot pulls write the hash-chained ingest
# audit trail (DEC-INGEST-AUDIT-WIRING-001; the CLI-parity regression)
# ────────────────────────────────────────────────────────────────────────


class TestIngestAuditWiring:
    """The dashboard `/pull-pipeline` spot pull — the PRIMARY pull UI — must
    write the SAME hash-chained ``<lake>/audit/spot-ingest.jsonl`` trail the
    CLI ``brutex fetch`` writes (Golden Rule #8; before this fix it wrote
    ZERO audit — no caller could even wire one)."""

    def _walk_chain(self, path: Path) -> list[str]:
        """Verify the chain end-to-end + link-by-link; return the action list."""
        from brutex.orchestrator.audit import GENESIS_PREV_HASH, read_entries, verify_chain

        entries = read_entries(path)
        assert verify_chain(path) == len(entries)
        assert entries[0].prev_hash == GENESIS_PREV_HASH
        for prev, cur in pairwise(entries):
            assert cur.prev_hash == prev.entry_hash
            assert cur.seq == prev.seq + 1
        return [e.action for e in entries]

    def test_pull_all_spot_writes_verifiable_hash_chain(self, tmp_path: Path) -> None:
        report = pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            adapter_factory=_real_bulk_factory(SyntheticSpotAdapter()),
        )
        assert report.total_failed == 0
        actions = self._walk_chain(tmp_path / "audit" / "spot-ingest.jsonl")
        # One 1-minute phase (GROWW_FETCH_INTERVALS) over one symbol:
        # RUN_START → PAIR_DONE → RUN_COMPLETE, exactly the CLI chain shape.
        assert actions == ["INGEST_RUN_START", "INGEST_PAIR_DONE", "INGEST_RUN_COMPLETE"]

    def test_pull_all_spot_second_pull_resumes_the_chain(self, tmp_path: Path) -> None:
        """A later pull into the same lake CONTINUES the chain (resume, one history)."""
        for _ in range(2):
            pull_all_spot(
                [_index("13")],
                lake_root=tmp_path,
                dry_run=False,
                now=_BULK_NOW,
                adapter_factory=_real_bulk_factory(SyntheticSpotAdapter()),
            )
        actions = self._walk_chain(tmp_path / "audit" / "spot-ingest.jsonl")
        assert len(actions) == 6
        assert actions.count("INGEST_RUN_START") == 2

    def test_pull_all_spot_quarantine_lands_on_the_chain(self, tmp_path: Path) -> None:
        """A quarantined symbol writes INGEST_PAIR_FAILED — errors are material actions."""
        report = pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            adapter_factory=lambda root, _insts: (_FailingAdapter(), _real_sink(root)),
        )
        assert report.total_failed == 1
        actions = self._walk_chain(tmp_path / "audit" / "spot-ingest.jsonl")
        assert actions == ["INGEST_RUN_START", "INGEST_PAIR_FAILED", "INGEST_RUN_COMPLETE"]

    def test_pull_all_spot_dry_run_writes_no_audit(self, tmp_path: Path) -> None:
        """A dry-run writes no real data → no audit file, no audit dir (CLI parity)."""
        pull_all_spot([_index("13")], lake_root=tmp_path, dry_run=True, now=_BULK_NOW)
        assert not (tmp_path / "audit").exists()

    def test_pull_candles_writes_verifiable_hash_chain(self, tmp_path: Path) -> None:
        pull_candles(
            _index(),
            lake_root=tmp_path,
            dry_run=False,
            from_date="2021-01-04",
            to_date="2021-01-08",
            adapter_factory=lambda root, _inst: (SyntheticSpotAdapter(), _real_sink(root)),
        )
        actions = self._walk_chain(tmp_path / "audit" / "spot-ingest.jsonl")
        assert actions == ["INGEST_RUN_START", "INGEST_PAIR_DONE", "INGEST_RUN_COMPLETE"]

    def test_pull_candles_dry_run_writes_no_audit(self, tmp_path: Path) -> None:
        pull_candles(_index(), lake_root=tmp_path, dry_run=True)
        assert not (tmp_path / "audit").exists()




class TestCooperativeCancel:
    """DEC-JOB-COOPERATIVE-CANCEL-001: a cooperative cancel maps to the honest partial verdict."""

    def test_cancel_at_window_boundary_sets_cancelled_and_keeps_ledger(
        self, tmp_path: Path
    ) -> None:
        """A should_stop that fires stops the spot pull gracefully — cancelled=True, no crash.

        The spine raises ``PullCancelled`` at the first window boundary (before any
        vendor call); ``pull_all_spot`` CATCHES it like the quota / mass-empty global
        stops, latches ``final_cancelled``, halts the remaining step groups, and the
        report reads ``cancelled=True`` — an honest partial, NOT an ``error`` and with
        NO Telegram alert (the operator asked for it).
        """
        with capture_logs() as logs:
            report = pull_all_spot(
                [_index("13")],
                lake_root=tmp_path,
                dry_run=True,
                now=_BULK_NOW,
                should_stop=lambda: True,
            )
        assert report.cancelled is True
        assert report.auth_aborted is False
        assert report.quota_exhausted is False
        assert report.to_dict()["cancelled"] is True
        events = {log["event"] for log in logs}
        assert "universe.pull_all.cancelled" in events

    def test_cancelled_verdict_is_never_a_fabricated_green_clean(self, tmp_path: Path) -> None:
        """Continuation-v3 MAJOR (fails-before): a cancelled pull's verdict kind is `cancelled`.

        Pre-fix ``BulkSpotReport.verdict`` never passed the ``cancelled`` latch
        into :func:`classify_pull`, so an operator Stop fell through to the
        GREEN ``clean`` kind — the "✅ Pull complete. / No action needed."
        banner stamped into ``_last_pull_verdict`` on all three cancel-wired
        surfaces AND the durable G5 pull journal (``record_pull_finish``
        stamps ``verdict=verdict.kind``). The honest verdict is the amber
        ``cancelled`` partial naming the operator stop + the re-run remedy.
        """
        report = pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=True,
            now=_BULK_NOW,
            should_stop=lambda: True,
        )
        assert report.cancelled is True
        verdict = report.verdict
        # A dry-run verdict is mark_synthetic-relabeled; the KIND is preserved
        # (the classifier stays the one authority) — pin the kind itself.
        assert verdict.kind == "cancelled"
        assert verdict.needs_operator is True
        assert "CANCELLED at operator request" in verdict.headline
        assert "✅ Pull complete." not in verdict.headline
        assert "Re-run the pull to resume the remainder" in verdict.action
        d = report.to_dict()
        assert d["verdict"]["kind"] == "cancelled"
        # The emit-on-cancel note: pull_jobs._finish_detail reads a Mapping's
        # non-empty "note" as the job's completion headline, so a cancelled
        # pull never finishes with the bare back-compat "done".
        assert "CANCELLED at operator request" in d["note"]

    def test_no_probe_is_not_cancelled(self, tmp_path: Path) -> None:
        """The default (no should_stop) pull is never cancelled — byte-compatible."""
        report = pull_all_spot([_index("13")], lake_root=tmp_path, dry_run=True, now=_BULK_NOW)
        assert report.cancelled is False
        d = report.to_dict()
        assert d["cancelled"] is False
        # No emit-on-cancel note on an uncancelled payload (byte-identical wire;
        # the job's completion headline stays the back-compat "done").
        assert "note" not in d
        assert d["verdict"]["kind"] != "cancelled"

    def test_cancelled_group_settle_event_reads_clean_false(self, tmp_path: Path) -> None:
        """Continuation-v2 fix (fails-before): a cancelled group's settle is NOT clean.

        ``PullCancelled`` is consumed inside ``_run_phase``, so the truncated
        group still emits its ``pull.step.settled`` event with the unissued
        windows left PLANNED (``group_failed == 0``) — pre-fix the event read
        a false ``clean=True``. The ``final_cancelled`` latch now folds into
        the ``clean=`` conjunction exactly like the adjacent market-hours /
        auth / quota / mass-empty latches.
        """
        with capture_logs() as logs:
            report = pull_all_spot(
                [_index("13")],
                lake_root=tmp_path,
                dry_run=True,
                now=_BULK_NOW,
                should_stop=lambda: True,
            )
        assert report.cancelled is True
        settled = [log for log in logs if log["event"] == "pull.step.settled"]
        assert settled, "the cancelled group must still emit its settle event"
        assert all(log["clean"] is False for log in settled)

    def test_uncancelled_group_settle_event_stays_clean(self, tmp_path: Path) -> None:
        """The clean-run settle event stays ``clean=True`` — the latch fold is additive."""
        with capture_logs() as logs:
            pull_all_spot([_index("13")], lake_root=tmp_path, dry_run=True, now=_BULK_NOW)
        settled = [log for log in logs if log["event"] == "pull.step.settled"]
        assert settled
        assert all(log["clean"] is True for log in settled)

    def test_cancel_withholds_post_drain_heal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Continuation-v2 fix (fails-before): a cancelled pull skips the L2 heal, loudly.

        ``PullCancelled`` is consumed inside ``_run_phase`` so execution reaches
        ``fetch_phases_ok = True``, and the finally's O(pulled-scope)
        ``_heal_after_pull`` previously ran AFTER the operator's Stop —
        contradicting the "finishing current window, then stop" latency
        contract (the F&O twin was fixed in combined-PR round 2 via
        ``cancelled_tail``; this is its spot mirror). The heal is now WITHHELD
        with ONE ``pull.cancel_heal_skipped`` WARN naming the next-pull cover.
        """
        calls: list[str] = []
        monkeypatch.setattr(pull_controller, "_heal_after_pull", lambda **_kw: calls.append("heal"))
        with capture_logs() as logs:
            report = pull_all_spot(
                [_index("13")],
                lake_root=tmp_path,
                dry_run=False,
                now=_BULK_NOW,
                adapter_factory=_real_bulk_factory(SyntheticSpotAdapter()),
                should_stop=lambda: True,
            )
        assert report.cancelled is True
        assert calls == []
        events = {log["event"] for log in logs}
        assert "pull.cancel_heal_skipped" in events

    def test_uncancelled_pull_still_runs_post_drain_heal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The no-cancel pull keeps the L2 heal — the withholding is cancel-gated only."""
        calls: list[str] = []
        monkeypatch.setattr(pull_controller, "_heal_after_pull", lambda **_kw: calls.append("heal"))
        with capture_logs() as logs:
            report = pull_all_spot(
                [_index("13")],
                lake_root=tmp_path,
                dry_run=False,
                now=_BULK_NOW,
                adapter_factory=_real_bulk_factory(SyntheticSpotAdapter()),
            )
        assert report.cancelled is False
        assert calls == ["heal"]
        events = {log["event"] for log in logs}
        assert "pull.cancel_heal_skipped" not in events


# ────────────────────────────────────────────────────────────────────────
# DEC-LEDGER-LOAD-SPACE-V1-001 — the shared pipeline ledger seam
# ────────────────────────────────────────────────────────────────────────


class TestSharedLedgerSeam:
    """`ledger=` injection + the production `shared_pull_ledger_scope` engagement.

    Mutant-killing wiring pins (the register-site precedent): the injected /
    scope-shared instance must be the EXACT object the spot phase records into
    and persists — a severed kwarg or a dropped scope consult fails by identity.
    """

    def _capture_end(self, monkeypatch: pytest.MonkeyPatch) -> list[object]:
        """Capture the ledger handed to `end_pull_ledger` (then save for real)."""
        real_end = pull_controller.end_pull_ledger
        captured: list[object] = []

        def _spy(ledger: object, lake_root: Path, *, dry_run: bool) -> None:
            captured.append(ledger)
            real_end(ledger, lake_root, dry_run=dry_run)  # type: ignore[arg-type]

        monkeypatch.setattr(pull_controller, "end_pull_ledger", _spy)
        return captured

    def test_injected_ledger_is_used_and_begin_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.data.window_ledger import WindowLedger

        mine = WindowLedger()
        captured = self._capture_end(monkeypatch)

        def _never(*args: object, **kwargs: object) -> None:
            raise AssertionError("begin_pull_ledger must be SKIPPED for an injected ledger")

        monkeypatch.setattr(pull_controller, "begin_pull_ledger", _never)
        report = pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            adapter_factory=lambda _root, _insts: (_EmptyAdapter(), NullBarSink()),
            now=_BULK_NOW,
            ledger=mine,
        )
        assert report.total_symbols == 1
        # The spot phase recorded into + persisted EXACTLY the injected instance.
        assert captured and captured[-1] is mine
        assert mine.count > 0  # the phase genuinely planned/recorded on it

    def test_injected_ledger_is_ignored_under_dry_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.data.window_ledger import WindowLedger

        mine = WindowLedger()
        captured = self._capture_end(monkeypatch)
        pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=True,
            now=_BULK_NOW,
            ledger=mine,
        )
        # A dry-run NEVER records/persists — the injected instance is ignored.
        assert captured and captured[-1] is None
        assert mine.count == 0

    def test_scope_shared_instance_reaches_spot_phase(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Production engagement: under an active scope, the spot phase's own
        `begin_pull_ledger` (no kwarg passed) returns the scope's ONE instance."""
        from brutex.data.window_ledger_store import (
            begin_pull_ledger,
            shared_pull_ledger_scope,
        )

        captured = self._capture_end(monkeypatch)
        with shared_pull_ledger_scope():
            first = begin_pull_ledger(tmp_path, dry_run=False)
            pull_all_spot(
                [_index("13")],
                lake_root=tmp_path,
                dry_run=False,
                adapter_factory=lambda _root, _insts: (_EmptyAdapter(), NullBarSink()),
                now=_BULK_NOW,
            )
        assert captured and captured[-1] is first


class TestTodayCompletenessGateArming:
    """DEC-PULL-TODAY-COMPLETENESS-GATE-001: both dashboard spot arm sites resolve the
    env kill switch ONCE per pull and thread the flag — live pull only (a dry-run
    threads ``False``, the ``detect_completeness_gaps`` gating pattern). The gate
    rides its OWN parameter on the bulk ``pull_all_spot`` phase — which now ALSO
    arms ``detect_completeness_gaps`` itself (DEC-BULK-SPOT-COMPLETENESS-FLAG-001,
    pinned by :class:`TestCompletenessDemotionArming`)."""

    def test_pull_all_spot_arms_today_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.dashboard import pull_controller as pc
        from brutex.data.today_completeness import TODAY_COMPLETENESS_GATE_ENV

        monkeypatch.setenv(TODAY_COMPLETENESS_GATE_ENV, "1")
        captured: list[object] = []
        real = pc.run_groww_spot_ingest_with_retry

        def _spy(*args: object, **kwargs: object) -> object:
            captured.append(kwargs.get("today_completeness_gate"))
            return real(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(pc, "run_groww_spot_ingest_with_retry", _spy)
        pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            adapter_factory=_real_bulk_factory(SyntheticSpotAdapter()),
        )
        assert captured
        assert all(flag is True for flag in captured)

    def test_pull_all_spot_dry_run_threads_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.dashboard import pull_controller as pc
        from brutex.data.today_completeness import TODAY_COMPLETENESS_GATE_ENV

        # Junk env MUST NOT crash a dry-run: the resolve is short-circuited away
        # (dry-run ⇒ False without touching the env).
        monkeypatch.setenv(TODAY_COMPLETENESS_GATE_ENV, "junk-token")
        captured: list[object] = []
        real = pc.run_groww_spot_ingest_with_retry

        def _spy(*args: object, **kwargs: object) -> object:
            captured.append(kwargs.get("today_completeness_gate"))
            return real(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(pc, "run_groww_spot_ingest_with_retry", _spy)
        pull_all_spot([_index("13")], lake_root=tmp_path, dry_run=True, now=_BULK_NOW)
        assert captured
        assert all(flag is False for flag in captured)

    def test_pull_candles_arms_today_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.dashboard import pull_controller as pc
        from brutex.data.today_completeness import TODAY_COMPLETENESS_GATE_ENV

        monkeypatch.setenv(TODAY_COMPLETENESS_GATE_ENV, "1")
        captured: list[object] = []
        real = pc.run_groww_spot_ingest

        def _spy(*args: object, **kwargs: object) -> object:
            captured.append(kwargs.get("today_completeness_gate"))
            return real(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(pc, "run_groww_spot_ingest", _spy)
        pull_candles(
            _index(),
            lake_root=tmp_path,
            dry_run=False,
            from_date="2021-01-04",
            to_date="2021-01-08",
            adapter_factory=lambda root, _inst: (SyntheticSpotAdapter(), _real_sink(root)),
        )
        assert captured
        assert all(flag is True for flag in captured)

    #: Thread-name families owned by the pull's thread-owning resources — a junk
    #: kill-switch raise must never orphan any of these (round-1 R3-MAJOR-1: the
    #: pre-hoist ordering leaked groww-fetch-consumer-0..3 + groww-fetch-watchdog
    #: + volume-keepalive per attempt, the keepalive fsync-ing the lake forever).
    _LEAKABLE_THREAD_PREFIXES = ("groww-fetch-consumer", "groww-fetch-watchdog", "volume-keepalive")

    def _leaked_pull_threads(self, before: set[str]) -> set[str]:
        import threading

        after = {t.name for t in threading.enumerate()}
        return {name for name in after - before if name.startswith(self._LEAKABLE_THREAD_PREFIXES)}

    def test_pull_all_spot_live_junk_env_fails_loud_at_pull_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A LIVE pull with a junk kill-switch value fails LOUD at pull start
        # (the resolve_market_closed posture) — never a silent coercion, never
        # a mid-run crash — and CLEAN: the resolve is hoisted ABOVE the
        # thread-owning resource builds, so the raise leaks ZERO pipeline /
        # consumer / keepalive threads (name-set delta, not absolute counts —
        # the suite process owns unrelated threads).
        import threading

        from brutex.data.today_completeness import TODAY_COMPLETENESS_GATE_ENV

        monkeypatch.setenv(TODAY_COMPLETENESS_GATE_ENV, "junk-token")
        before = {t.name for t in threading.enumerate()}
        with pytest.raises(ValueError, match=TODAY_COMPLETENESS_GATE_ENV):
            pull_all_spot(
                [_index("13")],
                lake_root=tmp_path,
                dry_run=False,
                now=_BULK_NOW,
                adapter_factory=_real_bulk_factory(SyntheticSpotAdapter()),
            )
        assert self._leaked_pull_threads(before) == set()

    def test_pull_candles_live_junk_env_fails_loud_and_leaks_no_threads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The single-symbol twin of the bulk no-leak pin (round-1 R3-MAJOR-1):
        # pull_candles resolves the kill switch BEFORE building its fetch
        # pipeline / write consumer, so a junk env raises with zero orphans.
        import threading

        from brutex.data.today_completeness import TODAY_COMPLETENESS_GATE_ENV

        monkeypatch.setenv(TODAY_COMPLETENESS_GATE_ENV, "junk-token")
        before = {t.name for t in threading.enumerate()}
        with pytest.raises(ValueError, match=TODAY_COMPLETENESS_GATE_ENV):
            pull_candles(
                _index(),
                lake_root=tmp_path,
                dry_run=False,
                from_date="2021-01-04",
                to_date="2021-01-08",
                adapter_factory=lambda root, _inst: (SyntheticSpotAdapter(), _real_sink(root)),
            )
        assert self._leaked_pull_threads(before) == set()

    def test_pull_candles_kill_switch_off_threads_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.dashboard import pull_controller as pc
        from brutex.data.today_completeness import TODAY_COMPLETENESS_GATE_ENV

        monkeypatch.setenv(TODAY_COMPLETENESS_GATE_ENV, "0")
        captured: list[object] = []
        real = pc.run_groww_spot_ingest

        def _spy(*args: object, **kwargs: object) -> object:
            captured.append(kwargs.get("today_completeness_gate"))
            return real(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(pc, "run_groww_spot_ingest", _spy)
        pull_candles(
            _index(),
            lake_root=tmp_path,
            dry_run=False,
            from_date="2021-01-04",
            to_date="2021-01-08",
            adapter_factory=lambda root, _inst: (SyntheticSpotAdapter(), _real_sink(root)),
        )
        assert captured
        assert all(flag is False for flag in captured)


class TestCompletenessDemotionArming:
    """DEC-BULK-SPOT-COMPLETENESS-FLAG-001: the bulk ``pull_all_spot`` phase arms
    ``detect_completeness_gaps`` on a LIVE pull (``not dry_run`` — the cli.py
    ``cmd_fetch`` idiom), so the #1031 interior-gap / trailing-truncation
    demotions run on every production bulk path; a dry-run threads ``False``
    (the synthetic adapter's first-day-only bars would false-red every wide
    window). The exact spy template of :class:`TestTodayCompletenessGateArming`."""

    def test_pull_all_spot_live_arms_detect_completeness_gaps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.dashboard import pull_controller as pc

        captured: list[object] = []
        real = pc.run_groww_spot_ingest_with_retry

        def _spy(*args: object, **kwargs: object) -> object:
            captured.append(kwargs.get("detect_completeness_gaps"))
            return real(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(pc, "run_groww_spot_ingest_with_retry", _spy)
        pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            from_date="2026-02-02",
            to_date="2026-02-06",
            adapter_factory=_real_bulk_factory(SyntheticSpotAdapter()),
        )
        assert captured
        assert all(flag is True for flag in captured)

    def test_pull_all_spot_dry_run_threads_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A dry-run is double-protected: the flag is False (never a demotion of
        # the synthetic first-day-only shape) AND begin_pull_ledger(dry_run=True)
        # returns None, so a demotion would be record-inert anyway.
        from brutex.dashboard import pull_controller as pc

        captured: list[object] = []
        real = pc.run_groww_spot_ingest_with_retry

        def _spy(*args: object, **kwargs: object) -> object:
            captured.append(kwargs.get("detect_completeness_gaps"))
            return real(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(pc, "run_groww_spot_ingest_with_retry", _spy)
        pull_all_spot([_index("13")], lake_root=tmp_path, dry_run=True, now=_BULK_NOW)
        assert captured
        assert all(flag is False for flag in captured)


def _equity(symbol: str = "RELIANCE") -> Instrument:
    """A minimal non-index NSE cash equity (the ~750 REFERENCE_ONLY class)."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=2,
        symbol_name=symbol,
        display_name=None,
        instrument=InstrumentType.EQ,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=1,  # EQ requires a positive lot size
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


class _InteriorGapAdapter(SyntheticSpotAdapter):
    """Bars at the window's START + END with a >5-session interior hole between.

    2024-03-04 (Mon) then 2024-03-27/28 (Wed/Thu): the contiguous bar-less run
    2024-03-05 → 2024-03-26 spans exactly 14 real NSE sessions (the 03-08
    Maha Shivaratri + 03-25 Holi holidays excluded) — far past the
    INTERIOR_GAP_TOLERANCE_SESSIONS=5 tolerance — while the last stored bar sits
    ON the window's final session (2024-03-28; 2024-03-29 is Good Friday), so the
    interior-gap detector fires and the truncation detector does not. The
    interior-gap classifier is CLOCK-FREE (its coverage abstention keys only on
    the covered bar dates, all 2024), so this fixture is deterministic forever.
    """

    def fetch_ohlcv(
        self, *, symbol: str, exchange: Exchange, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        """Return bars bookending a 14-session interior hole (never truncated)."""
        del symbol, exchange, interval, start, end
        return (
            synthetic_spot_bars(date(2024, 3, 4), count=self.bars_per_window)
            + synthetic_spot_bars(date(2024, 3, 27), count=self.bars_per_window)
            + synthetic_spot_bars(date(2024, 3, 28), count=self.bars_per_window)
        )


class TestBulkSpotCompletenessDemotionE2E:
    """DEC-BULK-SPOT-COMPLETENESS-FLAG-001 end-to-end: the bulk twin of
    ``TestIngestSpotCompletenessRepull`` — a LIVE ``pull_all_spot`` over a real
    tmp lake demotes an INDEX window with a confirmed interior hole to a
    re-pullable FAILED + ``INTERIOR_GAP``, while a non-index equity on the SAME
    pull with the SAME holey shape stays observe-only SUCCESS (the spine's
    ``is_index`` gate — the #1031 illiquid-name re-pull-storm protection)."""

    def test_live_bulk_pull_demotes_holey_index_and_keeps_equity_success(
        self, tmp_path: Path
    ) -> None:
        from brutex.data.window_ledger_store import load_window_ledger

        with capture_logs() as logs:
            report = pull_all_spot(
                [_index("13"), _equity("RELIANCE")],
                lake_root=tmp_path,
                dry_run=False,
                now=_BULK_NOW,
                from_date="2024-03-04",
                to_date="2024-03-28",
                adapter_factory=_real_bulk_factory(_InteriorGapAdapter()),
            )
        assert report.dry_run is False
        ledger = load_window_ledger(tmp_path)
        rows = {r.security_id: r for r in ledger.records()}
        assert set(rows) == {"13", "RELIANCE"}
        # The INDEX window is DEMOTED — re-pullable FAILED with the honest reason,
        # never a silently-settled SUCCESS the skip rule would freeze forever.
        index_row = rows["13"]
        assert index_row.status == "FAILED"
        assert index_row.status not in {"SUCCESS", "SKIPPED"}  # the skip-authority set
        assert index_row.last_error == "INTERIOR_GAP"
        # The non-index equity with the IDENTICAL holey shape is observed but
        # NEVER demoted (legitimately sparse names must not re-pull-storm).
        equity_row = rows["RELIANCE"]
        assert equity_row.status == "SUCCESS"
        assert equity_row.last_error is None
        # The observe-only WARNING fired for BOTH symbols (visibility is never
        # gated — only the re-pull action is index-gated).
        gap_events = [e for e in logs if e["event"] == "window.interior_gap"]
        assert {e["symbol"] for e in gap_events} == {"'13'", "'RELIANCE'"}

    def test_dry_run_same_shape_is_never_demoted(self, tmp_path: Path) -> None:
        # The dry-run counterfactual: the SAME holey shape through the SAME bulk
        # entry with dry_run=True threads detect_completeness_gaps=False AND a
        # None ledger (begin_pull_ledger dry-run contract) — no demotion, no
        # ledger rows, nothing persisted (the false-red protection this flag's
        # ``not dry_run`` gating exists for).
        from brutex.data.window_ledger_store import ledger_path

        report = pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=True,
            now=_BULK_NOW,
            from_date="2024-03-04",
            to_date="2024-03-28",
        )
        assert report.dry_run is True
        assert not ledger_path(tmp_path).is_file()  # record-inert — no ledger written


class TestSeedObservedOverlayOrdering:
    """DEC-SEED-OBSERVED-OVERLAY-001 — the overlay load is HOISTED above the
    cold-start reconcile and ONE snapshot feeds BOTH the seed gate and the
    fetch-time detectors (single source of truth; exactly one load per pull)."""

    def test_pull_all_spot_loads_once_and_threads_the_seed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.dashboard import pull_controller as pc

        expected = _write_overlay_sidecar(tmp_path)
        order: list[str] = []
        seed_observed: list[object] = []
        ingest_observed: list[object] = []
        real_load = pc.load_observed_holidays
        real_reconcile = pc.reconcile_ledger_windows  # the bulk MINUTE-phase seed
        real_ingest = pc.run_groww_spot_ingest_with_retry

        def _load_spy(root: Path) -> frozenset:
            order.append("load")
            return real_load(root)

        def _reconcile_spy(*args: object, **kwargs: object) -> int:
            order.append("reconcile")
            seed_observed.append(kwargs.get("observed"))
            return real_reconcile(*args, **kwargs)  # type: ignore[arg-type]

        def _ingest_spy(*args: object, **kwargs: object) -> object:
            ingest_observed.append(kwargs.get("observed_holidays"))
            return real_ingest(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(pc, "load_observed_holidays", _load_spy)
        monkeypatch.setattr(pc, "reconcile_ledger_windows", _reconcile_spy)
        monkeypatch.setattr(pc, "run_groww_spot_ingest_with_retry", _ingest_spy)
        pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            adapter_factory=_real_bulk_factory(SyntheticSpotAdapter()),
        )
        # ONE load per pull, strictly BEFORE the reconcile (the hoist).
        assert order.count("load") == 1
        assert "reconcile" in order
        assert order.index("load") < order.index("reconcile")
        # The SAME snapshot feeds the seed gate AND the fetch-time detectors.
        assert seed_observed and all(o == expected for o in seed_observed)
        assert ingest_observed and all(o == expected for o in ingest_observed)

    def test_pull_candles_loads_once_and_threads_both_reconciles(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.dashboard import pull_controller as pc

        expected = _write_overlay_sidecar(tmp_path)
        order: list[str] = []
        seed_observed: list[object] = []
        real_load = pc.load_observed_holidays
        real_reconcile = pc.reconcile_ledger_with_lake

        def _load_spy(root: Path) -> frozenset:
            order.append("load")
            return real_load(root)

        def _reconcile_spy(*args: object, **kwargs: object) -> int:
            order.append("reconcile")
            seed_observed.append(kwargs.get("observed"))
            return real_reconcile(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(pc, "load_observed_holidays", _load_spy)
        monkeypatch.setattr(pc, "reconcile_ledger_with_lake", _reconcile_spy)
        pull_candles(
            _index(),
            lake_root=tmp_path,
            dry_run=False,
            from_date="2021-01-04",
            to_date="2021-01-08",
            adapter_factory=lambda root, _inst: (SyntheticSpotAdapter(), _real_sink(root)),
        )
        assert order.count("load") == 1
        assert "reconcile" in order  # daily and/or minute — every one post-load
        assert order.index("load") < order.index("reconcile")
        assert seed_observed and all(o == expected for o in seed_observed)

    def test_pull_all_spot_dry_run_never_loads_the_overlay(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.dashboard import pull_controller as pc

        _write_overlay_sidecar(tmp_path)
        loads: list[Path] = []
        real_load = pc.load_observed_holidays
        monkeypatch.setattr(
            pc, "load_observed_holidays", lambda root: loads.append(root) or real_load(root)
        )
        pull_all_spot([_index("13")], lake_root=tmp_path, dry_run=True, now=_BULK_NOW)
        assert loads == []  # the frozenset()-if-dry_run guard survived the hoist
