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

import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.adapters.groww.errors import GrowwBadRequestError
from brutex.adapters.ingest_common import SyntheticSpotAdapter
from brutex.adapters.protocol import MarketAdapter
from brutex.dashboard.pull_controller import (
    GROWW_FETCH_INTERVALS,
    GROWW_HISTORY_FLOOR,
    BulkAdapterFactory,
    BulkSpotReport,
    PullCandlesReport,
    SessionRateController,
    TimeframeResult,
    default_date_range,
    make_live_sync,
    parse_symbol_list,
    pull_all_spot,
    pull_candles,
    resolve_candle_range,
    resolve_fetch_intervals,
    resolve_pull_symbols,
)
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
#: (``GROWW_FETCH_INTERVALS``) is **1-day only** — the verify-first default of the
#: ``/universe`` granularity dropdown (DEC-PULL-1DAY-FIRST-001: 1day / 1minute /
#: both). These tests opt INTO the two-interval ("both") shape so the multi-interval
#: machinery stays covered while the default-behaviour tests assert the 1-day default.
_BOTH_INTERVALS: tuple[str, ...] = ("1minute", "1day")


class TestResolveFetchIntervals:
    """The spot-interval dropdown resolver (DEC-PULL-1DAY-FIRST-001)."""

    def test_default_is_one_day_only(self) -> None:
        # The verify-first default: a blank/None choice NEVER widens to 1-minute.
        assert resolve_fetch_intervals(None) == ("1day",)
        assert GROWW_FETCH_INTERVALS == ("1day",)

    def test_day_choice(self) -> None:
        assert resolve_fetch_intervals("1day") == ("1day",)

    def test_minute_choice(self) -> None:
        assert resolve_fetch_intervals("1minute") == ("1minute",)

    def test_both_choice_keeps_display_order(self) -> None:
        # ``both`` fetches 1m then 1d (the canonical display order).
        assert resolve_fetch_intervals("both") == ("1minute", "1day")

    def test_whitespace_is_stripped(self) -> None:
        assert resolve_fetch_intervals("  both  ") == ("1minute", "1day")

    def test_unknown_choice_falls_back_to_one_day(self) -> None:
        # An unrecognised value safely defaults to 1-day (never crashes, never
        # silently widens to 1-minute — Golden Rule #1).
        assert resolve_fetch_intervals("5minute") == ("1day",)
        assert resolve_fetch_intervals("") == ("1day",)


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

    def test_covers_fetched_intervals(self, tmp_path: Path) -> None:
        report = pull_candles(_index(), lake_root=tmp_path, year=2024, dry_run=True)
        # Verify-first phase: the PRODUCTION default fetches 1-day ONLY
        # (DEC-PULL-1DAY-FIRST-001) — 1-minute + the 19 resampled intraday TFs
        # return once the OHLCV-vs-bhavcopy check passes — so the grid shows the
        # single 1d cell (not two).
        assert [tf.interval for tf in report.timeframes] == ["1d"]
        assert GROWW_FETCH_INTERVALS == ("1day",)
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
        assert report.total_no_data == 2  # both fetched intervals empty
        d = report.to_dict()
        assert d["total_no_data"] == report.total_no_data
        assert d["total_skipped"] == 0

    def test_on_progress_forwarded_per_pair(self, tmp_path: Path) -> None:
        """pull_candles threads on_progress → fires once per (symbol, interval) pair.

        The single-symbol twin of the bulk on_progress test: the dashboard's
        background-job percent ticks per completed pair, so the hook must fire
        for both fetched intervals (set-asserted — the parallel fan-out's
        completion order is non-deterministic).
        """
        events: list[tuple[str, int, bool]] = []
        pull_candles(
            _index(),
            lake_root=tmp_path,
            dry_run=True,
            intervals=_BOTH_INTERVALS,
            from_date="2021-01-04",
            to_date="2021-01-08",
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
        report = pull_candles(
            _index(),
            lake_root=tmp_path,
            year=2024,
            dry_run=True,
            intervals=_BOTH_INTERVALS,
            adapter_factory=lambda _root, _inst: (_FailingAdapter(), NullBarSink()),
        )
        assert report.total_completed == 0
        assert report.total_failed == 2  # both fetched intervals quarantined


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

    def test_auto_retry_on_clean_dry_run_is_noop(self, tmp_path: Path) -> None:
        # The "Retry failed windows" button path (auto_retry=True): a clean
        # synthetic pull leaves no transient residual, so the wrapper runs no
        # extra rounds and the per-symbol rollup is identical to the single-pass
        # default — same engine, no behaviour change on a clean pull
        # (DEC-INGEST-AUTO-RETRY-001).
        report = pull_all_spot(
            [_index("13"), _index("25")],
            lake_root=tmp_path,
            dry_run=True,
            now=_BULK_NOW,
            intervals=_BOTH_INTERVALS,
            auto_retry=True,
        )
        assert report.total_symbols == 2
        assert report.total_completed == 4
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
        # An adapter returning no rows → no_data for both intervals of the symbol.
        report = pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=True,
            now=_BULK_NOW,
            intervals=_BOTH_INTERVALS,
            adapter_factory=lambda _root, _insts: (_EmptyAdapter(), NullBarSink()),
        )
        assert report.total_no_data == 2
        assert report.total_completed == 0
        assert report.symbols[0].symbol == "13"
        # Nothing completed + only no-data → the known-gap verdict (no operator action).
        assert report.verdict.kind == "known_gap_only"
        assert report.verdict.needs_operator is False
        assert report.to_dict()["verdict"]["kind"] == "known_gap_only"
        # A failing adapter → both intervals quarantined.
        failed = pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            intervals=_BOTH_INTERVALS,
            adapter_factory=lambda _root, _insts: (_FailingAdapter(), NullBarSink()),
        )
        assert failed.total_failed == 2
        assert failed.dry_run is False
        # Quarantined pairs (token alive) → transient-residual: re-pull/auto-retry
        # finishes. needs_operator True drives the loud action-required WARNING.
        assert failed.verdict.kind == "transient_residual"
        assert failed.verdict.needs_operator is True
        assert failed.verdict.pending_windows == 2
        assert failed.to_dict()["verdict"]["needs_operator"] is True

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
        assert all_gap.total_no_data == 2  # both intervals are settled no-data (EMPTY)
        # Nothing completed + only no-data → known_gap_only (no operator action),
        # NOT transient_residual — zero manual re-pull is asked.
        assert all_gap.verdict.kind == "known_gap_only"
        assert all_gap.verdict.needs_operator is False
        assert all_gap.verdict.pending_windows == 0

    def test_daily_gap_with_intraday_data_is_clean(self, tmp_path: Path) -> None:
        # The EXACT operator case (NIFTY NEXT 50): 1minute completes, the 1day
        # series is a DH-905 known gap. The pull must be CLEAN (no pending window,
        # no manual button), because the only "miss" is a settled no-data gap
        # (DEC-DHAN-NODATA-SETTLED-001).
        report = pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            intervals=_BOTH_INTERVALS,
            adapter_factory=lambda _root, _insts: (_DailyGapAdapter(), NullBarSink()),
        )
        assert report.total_completed == 1  # the 1minute series landed bars
        assert report.total_failed == 0  # the 1day DH-905 is NOT a failure
        assert report.total_no_data == 1  # the 1day gap is settled no-data
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
        pull_all_spot(
            [_index("13"), _index("25")],
            lake_root=tmp_path,
            dry_run=True,
            now=_BULK_NOW,
            intervals=_BOTH_INTERVALS,
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
    (DEC-GROWW-RATE-CEILING-8-001).
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

    def test_default_factory_builds_a_real_controller(self) -> None:
        # DEC-GROWW-NO-DAILY-QUOTA-001: the dashboard controller is built with NO
        # DailyQuotaTracker (Groww backtesting has no daily quota, operator-confirmed),
        # matching the CLI Groww path — so it never halts on a phantom daily cap, while
        # the 500/min per-minute governor stays wired.
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
        # DEC-GROWW-RATE-CEILING-8-001: the controller starts at the operator-DIRECTED
        # 8 r/s per-second ceiling (was the implicit 5 via rate_common.CEILING_RPS),
        # and 8×60 = 480/min stays just under the 500/min governor.
        assert ctl.current_rps == GROWW_RATE_LIMIT_RPS_CEILING == 8.0
        assert snap["current_rps"] == 8.0
        assert GROWW_RATE_LIMIT_RPS_CEILING * 60 < GROWW_RATE_LIMIT_PER_MIN


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


class TestPullPopulatesLedger:
    """DEC-WINDOW-LEDGER-001 (PR-4): a live pull records EVERY window in the ledger DB."""

    def test_live_pull_writes_success_rows(self, tmp_path: Path) -> None:
        from brutex.data.window_ledger_store import ledger_path, load_window_ledger

        report = pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
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

    def test_empty_windows_are_recorded_empty(self, tmp_path: Path) -> None:
        # Groww has no DH-905 known-gap → no SKIPPED settling (Golden Rule #1). A
        # clean 0-bar fetch is recorded EMPTY in the ledger so every window is
        # captured in the DB (nothing silently missing) with its true outcome, not
        # a perpetual "pending" PLANNED row. (An EMPTY seen EMPTY_TERMINAL_THRESHOLD
        # separate pulls later promotes to SKIPPED — out of scope for one pull.)
        from brutex.data.window_ledger_store import ledger_path, load_window_ledger

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
        assert {r.status for r in ledger.records()} == {"EMPTY"}  # settled empty, never SKIPPED

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
    def _quota_death_scaffold() -> tuple[threading.Event, BulkAdapterFactory]:
        """Build the deterministic mid-pull quota-death fake.

        The ``1day`` fetch raises ``DailyQuotaExceeded`` — the NON-Dhan exception
        the pace bucket raises inside a window worker when the 100k/day quota dies
        mid-pass (never quarantined; the spine re-raises it). It first WAITS on the
        returned event, which the test's ``on_progress`` hook sets when the
        ``1minute`` pair has fully completed — so under the parallel fan-out the
        success is always committed + recorded BEFORE the death, deterministically.
        """
        minute_done = threading.Event()

        class _QuotaDiesOnDaily(SyntheticSpotAdapter):
            def fetch_ohlcv(
                self, *, symbol: str, exchange: Exchange, interval: str, start: date, end: date
            ) -> list[CandleBar]:
                if interval == "1day":
                    minute_done.wait(timeout=30.0)
                    raise DailyQuotaExceeded("daily quota of 100000 calls spent")
                return super().fetch_ohlcv(
                    symbol=symbol, exchange=exchange, interval=interval, start=start, end=end
                )

        def factory(_root: Path, _insts: Sequence[Instrument]) -> tuple[MarketAdapter, BarSink]:
            return _QuotaDiesOnDaily(), NullBarSink()

        return minute_done, factory

    def _pull(
        self,
        tmp_path: Path,
        *,
        until_clean: bool,
    ) -> tuple[BulkSpotReport, Sequence[Mapping[str, object]]]:
        """One quota-dying live-pattern pull over a tiny 5-day window (2 windows)."""
        minute_done, factory = self._quota_death_scaffold()

        def _on_progress(label: str, bars: int, failed: bool) -> None:
            del bars, failed
            if "1minute" in label:
                minute_done.set()

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
                on_progress=_on_progress,
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
        # No pass COMPLETED, so the per-pair rollup claims nothing (the verdict
        # carries the actionable state) — committed work lives in the ledger.
        assert report.total_completed == 0
        assert report.total_failed == 0
        assert report.total_no_data == 0
        # The finally-guaranteed persist: the settled 1minute window survives as
        # SUCCESS; the never-run 1day window stays PLANNED — nothing discarded.
        assert ledger_path(tmp_path).is_file()
        # DEC-STATUS-SIDECAR-WRITE-RETIRE-001: the legacy window_status.json is
        # NEVER written any more — the ledger is the sole persisted store.
        assert not status_path(tmp_path).is_file()
        ledger = load_window_ledger(tmp_path)
        assert ledger.count == 2
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
        # No round COMPLETED — the rollup claims nothing; the ledger keeps the truth.
        assert report.total_completed == 0
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

        The ``passes[-1]`` arm (hostile-review finding): round 1 completes (1minute
        SUCCESS, 1day quarantined terminally — a FAILED window is never skippable,
        so round 2 re-attempts exactly it), then round 2's only worker call dies on
        the quota. The real driver would need round-1 transient residual + real
        inter-round backoff sleeps to reach round 2, so the loop driver is faked to
        force the second round — the quota exception itself still originates INSIDE
        a real worker pass and propagates through the real except/finally path.
        """
        from brutex.adapters.ingest_common import UntilCleanOutcome, UntilCleanRound
        from brutex.data.window_ledger_store import ledger_path

        state = {"round": 1}

        class _DiesInRoundTwo(SyntheticSpotAdapter):
            def fetch_ohlcv(
                self, *, symbol: str, exchange: Exchange, interval: str, start: date, end: date
            ) -> list[CandleBar]:
                if interval == "1day":
                    if state["round"] >= 2:
                        raise DailyQuotaExceeded("daily quota of 100000 calls spent")
                    raise GrowwBadRequestError("boom 1day")  # round 1: terminal quarantine
                return super().fetch_ohlcv(
                    symbol=symbol, exchange=exchange, interval=interval, start=start, end=end
                )

        def _force_second_round(
            round_runner: Callable[[], UntilCleanRound], **_kwargs: object
        ) -> UntilCleanOutcome:
            round_runner()  # round 1 — a REAL full pass, recorded in ``passes``
            state["round"] = 2
            round_runner()  # round 2 — the 1day worker raises DailyQuotaExceeded
            raise AssertionError("round 2 must die on the quota")

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
                adapter_factory=lambda _root, _insts: (_DiesInRoundTwo(), NullBarSink()),
                until_clean=True,
            )
        assert report.quota_exhausted is True
        assert report.verdict.kind == "quota_exhausted"  # quota outranks the failed pair
        # passes[-1] = round 1's COMPLETED pass: zero transient residual, live token.
        assert report.still_failed_windows == 0
        assert report.auth_aborted is False
        assert report.auth_cause is None
        # The per-symbol rollup is round 1's full report — committed work shown.
        assert report.total_completed == 1  # the 1minute pair landed in round 1
        assert report.total_failed == 1  # the 1day terminal quarantine from round 1
        assert ledger_path(tmp_path).is_file()
        events = _events(logs, "universe.pull_all.quota_exhausted")
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
        report = pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
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
        # An EMPTY-returning adapter settles 0 windows as SUCCESS, but the final push must
        # STILL fire (in the finally) so the operator sees how far the pull got — the
        # honest partial-progress signal behind the crash screenshot's "5,819 settled".
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
        assert completed == 0  # EMPTY windows are pending, not settled — honest

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
