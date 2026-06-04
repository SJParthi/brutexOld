"""Unit tests for :mod:`brutex.dashboard.pull_controller` (Dhan model).

Offline only — exercises the dry-run synthetic path (no creds / network)
and the injected-factory path. The live Dhan builder
(:func:`_build_live_pull_adapter`) is ``# pragma: no cover`` because it needs
a real token + the ``urllib`` transport, both absent in CI
(DEC-VENDOR-PIVOT-001).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.adapters.dhan.errors import DhanBadRequestError
from brutex.adapters.dhan.synthetic_adapter import SyntheticDhanAdapter
from brutex.dashboard.pull_controller import (
    DHAN_FETCH_INTERVALS,
    DHAN_HISTORY_FLOOR,
    BulkSpotReport,
    PullCandlesReport,
    SessionRateController,
    TimeframeResult,
    dhan_creds_present,
    pull_all_spot,
    pull_candles,
    resolve_candle_range,
)
from brutex.data.models import CandleBar
from brutex.data.sink import BarSink, NullBarSink
from brutex.models import Exchange, Instrument, InstrumentType, Segment
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


class _EmptyAdapter(SyntheticDhanAdapter):
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


class _FailingAdapter(SyntheticDhanAdapter):
    """A :class:`MarketAdapter` whose every fetch raises a terminal Dhan error.

    ``DhanBadRequestError`` is non-retryable (``is_retryable`` → False), so
    :func:`ingest_spot` re-raises and :func:`run_dhan_spot_ingest` quarantines
    the ``(symbol, interval)`` pair into ``failures`` → the ``failed`` branch.
    """

    def fetch_ohlcv(
        self, *, symbol: str, exchange: Exchange, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        """Always raise a terminal Dhan error — drives the failure branch."""
        del symbol, exchange, start, end
        raise DhanBadRequestError(f"boom for {interval}")


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
# dhan_creds_present — testable creds / dry-run safety gate (re-export, H1)
# ────────────────────────────────────────────────────────────────────────


class TestDhanCredsPresent:
    """The dry-run gate re-exported from ``brutex.adapters.dhan.auth``.

    Reads ``BRUTEX_DHAN_ACCESS_TOKEN`` + ``BRUTEX_DHAN_CLIENT_ID`` directly
    from the env mapping; never raises (DEC-VENDOR-PIVOT-001).
    """

    def test_both_present_is_true(self) -> None:
        env = {"BRUTEX_DHAN_ACCESS_TOKEN": "t", "BRUTEX_DHAN_CLIENT_ID": "c"}
        assert dhan_creds_present(env) is True

    def test_both_absent_is_false(self) -> None:
        assert dhan_creds_present({}) is False

    def test_token_missing_is_false(self) -> None:
        assert dhan_creds_present({"BRUTEX_DHAN_CLIENT_ID": "c"}) is False

    def test_client_missing_is_false(self) -> None:
        assert dhan_creds_present({"BRUTEX_DHAN_ACCESS_TOKEN": "t"}) is False

    def test_blank_value_is_false(self) -> None:
        env = {"BRUTEX_DHAN_ACCESS_TOKEN": "t", "BRUTEX_DHAN_CLIENT_ID": "   "}
        assert dhan_creds_present(env) is False

    def test_defaults_to_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Default env=None reads os.environ.
        monkeypatch.delenv("BRUTEX_DHAN_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("BRUTEX_DHAN_CLIENT_ID", raising=False)
        assert dhan_creds_present() is False
        monkeypatch.setenv("BRUTEX_DHAN_ACCESS_TOKEN", "t")
        monkeypatch.setenv("BRUTEX_DHAN_CLIENT_ID", "c")
        assert dhan_creds_present() is True


# ────────────────────────────────────────────────────────────────────────
# pull_candles — dry-run (default factory exercises ``_build_dry_run_adapter``)
# ────────────────────────────────────────────────────────────────────────


class TestPullCandlesDryRun:
    def test_default_no_dates_no_year_pulls_full_history(self, tmp_path: Path) -> None:
        # No dates, no year → the FULL Track-1 history (2021-01-01 → previous
        # day), so the report's start year is the 2021 floor, NOT the current
        # year (FIX 4 — blank pickers default to the full history floor).
        report = pull_candles(_index(), lake_root=tmp_path, dry_run=True)
        assert report.year == DHAN_HISTORY_FLOOR.year
        assert report.start_date == DHAN_HISTORY_FLOOR.isoformat()
        assert report.dry_run is True
        assert report.symbol == "13"

    def test_covers_fetched_intervals(self, tmp_path: Path) -> None:
        report = pull_candles(_index(), lake_root=tmp_path, year=2024, dry_run=True)
        # Dhan fetches 1m + 1d only (the 19 derived TFs are resampled, not
        # fetched — DEC-DHAN-1M-ONLY-001); the grid shows two cells.
        assert [tf.interval for tf in report.timeframes] == ["1m", "1d"]
        assert len(DHAN_FETCH_INTERVALS) == 2
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
            adapter_factory=lambda _root, _inst: (_EmptyAdapter(), NullBarSink()),
        )
        assert report.total_completed == 0
        assert report.total_failed == 0
        assert report.total_no_data == 2  # both fetched intervals empty
        d = report.to_dict()
        assert d["total_no_data"] == report.total_no_data
        assert d["total_skipped"] == 0


# ────────────────────────────────────────────────────────────────────────
# pull_candles — injected factory (covers the non-default-factory branch)
# ────────────────────────────────────────────────────────────────────────


class TestPullCandlesInjectedFactory:
    def test_factory_overrides_dry_run_false(self, tmp_path: Path) -> None:
        seen: dict[str, object] = {}

        def factory(
            lake_root: Path, instrument: Instrument
        ) -> tuple[SyntheticDhanAdapter, BarSink]:
            seen["lake_root"] = lake_root
            seen["symbol_name"] = instrument.symbol_name
            return SyntheticDhanAdapter(), NullBarSink()

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
        # (2021-01-01 → previous day), NOT the current year (FIX 4). The end is
        # the previous day (05-29), exclusive-end midnight 05-30, so the current
        # session (today, 05-30) is excluded by default (FIX 5).
        start, end = resolve_candle_range(from_date=None, to_date=None, year=None, now=_NOW)
        assert start == datetime(2021, 1, 1, tzinfo=UTC)  # the 2021 floor, not 2026
        assert end == datetime(2026, 5, 30, tzinfo=UTC)  # yesterday (05-29) + 1 day

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
        # Mode 2 — a year wholly before the 2021 Track-1 floor (DEC-DHAN-PULL-SCOPE-001):
        # start clamps up to the floor, end stays in the pre-floor year → start > end →
        # refused. 2020 is now BEFORE the floor (the operator starts at 2021 for safety).
        with pytest.raises(ValueError, match="nothing to pull"):
            resolve_candle_range(from_date=None, to_date=None, year=2020, now=_NOW)

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
        # to_date must be on/after the 2021 floor or there's nothing to pull.
        start, end = resolve_candle_range(from_date=None, to_date="2021-03-10", year=None, now=_NOW)
        assert start == datetime(DHAN_HISTORY_FLOOR.year, 1, 1, tzinfo=UTC)  # 2021-01-01
        assert end == datetime(2021, 3, 11, tzinfo=UTC)

    def test_from_before_floor_is_clamped_up(self) -> None:
        start, _ = resolve_candle_range(
            from_date="2018-06-01", to_date="2021-06-01", year=None, now=_NOW
        )
        assert start == datetime(2021, 1, 1, tzinfo=UTC)  # clamped up to the 2021 floor

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
        assert start == datetime(DHAN_HISTORY_FLOOR.year, 1, 1, tzinfo=UTC)
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
        # full 2021 → previous day window (DEC-DHAN-PULL-SCOPE-001); the bulk
        # pull passes no explicit to_date so the current session is excluded.
        assert report.start_date == "2021-01-01"
        assert report.end_date == "2026-05-29"  # previous day (now=05-30)
        # both fetched intervals (1m + 1d) completed for each symbol → 4 pairs.
        assert report.total_completed == 4
        assert report.total_failed == 0
        assert report.total_no_data == 0

    def test_empty_universe_is_clean_noop(self, tmp_path: Path) -> None:
        report = pull_all_spot([], lake_root=tmp_path, dry_run=True, now=_BULK_NOW)
        assert report.total_symbols == 0
        assert report.total_completed == 0
        assert report.to_dict()["symbols"] == []

    def test_no_data_and_failure_branches(self, tmp_path: Path) -> None:
        # An adapter returning no rows → no_data for both intervals of the symbol.
        report = pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=True,
            now=_BULK_NOW,
            adapter_factory=lambda _root, _insts: (_EmptyAdapter(), NullBarSink()),
        )
        assert report.total_no_data == 2
        assert report.total_completed == 0
        assert report.symbols[0].symbol == "13"
        # A failing adapter → both intervals quarantined.
        failed = pull_all_spot(
            [_index("13")],
            lake_root=tmp_path,
            dry_run=False,
            now=_BULK_NOW,
            adapter_factory=lambda _root, _insts: (_FailingAdapter(), NullBarSink()),
        )
        assert failed.total_failed == 2
        assert failed.dry_run is False

    def test_factory_receives_the_full_instrument_list(self, tmp_path: Path) -> None:
        seen: dict[str, int] = {}

        def factory(lake_root: Path, instruments: object) -> tuple[SyntheticDhanAdapter, BarSink]:
            seen["count"] = len(list(instruments))  # type: ignore[arg-type]
            return SyntheticDhanAdapter(), NullBarSink()

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
        underlying = _events(logs, "ingest.underlying")
        assert underlying and all(e["pull_id"] == "ctlpull00003" for e in underlying)


class TestSessionRateController:
    """The session-scoped rate-controller holder (DEC-DHAN-PERSIST-RATE-001).

    Persists ONE controller across pulls so the learned rate carries over instead
    of every pull restarting at the 5 r/s ceiling.
    """

    def test_get_returns_same_instance_and_builds_once(self) -> None:
        from brutex.adapters.dhan.rate_control import DhanRateController

        built: list[DhanRateController] = []

        def _factory() -> DhanRateController:
            ctl = DhanRateController()
            built.append(ctl)
            return ctl

        holder = SessionRateController(factory=_factory)
        first = holder.get()
        second = holder.get()
        assert second is first  # SAME controller across pulls — the learned rate persists
        assert len(built) == 1  # built ONCE, not per pull

    def test_default_factory_builds_a_real_controller(self) -> None:
        from brutex.adapters.dhan.rate_control import DhanRateController

        holder = SessionRateController()  # default factory = _build_live_controller
        ctl = holder.get()
        assert isinstance(ctl, DhanRateController)
        assert holder.get() is ctl  # cached on subsequent calls
