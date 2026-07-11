"""Tests for `brutex.adapters.ingest_common` — the vendor-neutral ingest helpers.

100% line+branch on the moved-out shared utilities (PR-J1):

* :func:`~brutex.adapters.ingest_common.plan_windows` + :class:`DateWindow` — the
  generic ``[start, end)`` tiler (single window, exact multiples, clipped tail,
  empty range, the positive-window guard),
* :func:`~brutex.adapters.ingest_common.tile_windows` — the per-interval builder
  over an injected width resolver (a finite width tiles; ``None`` emits one
  un-tiled window),
* :func:`~brutex.adapters.ingest_common.clamp_window_end` +
  :func:`~brutex.adapters.ingest_common.now_ist` — the current-day settled-data
  clamp,
* :func:`~brutex.adapters.ingest_common.summarize_conflicts` — the keep-first
  conflict aggregation.

The auto-retry round loop has its own file (``test_ingest_common_auto_retry.py``).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import pairwise

import pytest
from brutex.adapters.ingest_common import (
    DEFAULT_EMPTY_RETRY_ATTEMPTS,
    DateWindow,
    clamp_window_end,
    drop_bars_after,
    fetch_until_nonempty,
    now_ist,
    plan_windows,
    summarize_conflicts,
    tile_windows,
    window_days_for_interval,
)
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import BarConflict


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 9, 15, tzinfo=UTC)


class TestPlanWindows:
    def test_single_window_when_range_under_limit(self) -> None:
        windows = plan_windows(_dt(2021, 1, 1), _dt(2021, 1, 10), window_days=90)
        assert windows == [
            DateWindow(start=_dt(2021, 1, 1).isoformat(), end=_dt(2021, 1, 10).isoformat())
        ]

    def test_exact_multiple_tiles_without_overlap(self) -> None:
        windows = plan_windows(_dt(2021, 1, 1), _dt(2021, 1, 31), window_days=10)
        assert len(windows) == 3
        # Contiguous + non-overlapping: each window's end == next window's start.
        for a, b in pairwise(windows):
            assert a.end == b.start
        assert windows[0].start == _dt(2021, 1, 1).isoformat()
        assert windows[-1].end == _dt(2021, 1, 31).isoformat()

    def test_final_window_clipped_to_end(self) -> None:
        windows = plan_windows(_dt(2021, 1, 1), _dt(2021, 1, 25), window_days=10)
        assert windows[-1].end == _dt(2021, 1, 25).isoformat()
        # Last span is shorter than the limit (clipped).
        assert windows[-1].start == _dt(2021, 1, 21).isoformat()

    def test_empty_when_start_not_before_end(self) -> None:
        assert plan_windows(_dt(2021, 1, 10), _dt(2021, 1, 10), window_days=90) == []
        assert plan_windows(_dt(2021, 1, 20), _dt(2021, 1, 10), window_days=90) == []

    @pytest.mark.parametrize("bad", [0, -1, -90])
    def test_non_positive_window_raises(self, bad: int) -> None:
        with pytest.raises(ValueError, match="window_days must be positive"):
            plan_windows(_dt(2021, 1, 1), _dt(2021, 1, 10), window_days=bad)


class TestTileWindows:
    def test_finite_width_tiles(self) -> None:
        """A positive width resolver tiles via plan_windows, decoded back to date."""
        windows = tile_windows(
            "1minute", date(2021, 1, 1), date(2021, 4, 1), window_days_for=lambda _i: 30
        )
        assert windows[0][0] == date(2021, 1, 1)
        assert windows[-1][1] == date(2021, 4, 1)
        # Contiguous: each window's end == next window's start.
        for a, b in pairwise(windows):
            assert a[1] == b[0]

    def test_none_width_emits_single_untiled_window(self) -> None:
        """A ``None`` width (the Dhan daily case) emits ONE window over the whole range."""
        windows = tile_windows(
            "1day", date(2021, 1, 1), date(2025, 1, 1), window_days_for=lambda _i: None
        )
        assert windows == [(date(2021, 1, 1), date(2025, 1, 1))]


class TestClampWindowEnd:
    def test_daily_clamped_to_today(self) -> None:
        """The current-day 1-day candle is unsettled → daily bound clamps to today."""
        ist = datetime(2026, 6, 12, 10, 0, tzinfo=UTC)
        assert clamp_window_end("1day", date(2026, 6, 30), now_ist=ist) == date(2026, 6, 12)

    def test_daily_not_clamped_when_already_past(self) -> None:
        """A daily bound already at/before today is left untouched (min keeps it)."""
        ist = datetime(2026, 6, 12, 10, 0, tzinfo=UTC)
        assert clamp_window_end("1day", date(2026, 6, 1), now_ist=ist) == date(2026, 6, 1)

    def test_intraday_before_close_clamps_to_today(self) -> None:
        """Before 15:30 IST the current day is partial → intraday bound clamps to today."""
        ist = datetime(2026, 6, 12, 9, 0, tzinfo=UTC)  # before 15:30
        assert clamp_window_end("1minute", date(2026, 6, 30), now_ist=ist) == date(2026, 6, 12)

    def test_intraday_after_close_allows_today_plus_one(self) -> None:
        """From 15:30 IST the full session exists → intraday bound allows today + 1."""
        ist = datetime(2026, 6, 12, 16, 0, tzinfo=UTC)  # after 15:30
        assert clamp_window_end("1minute", date(2026, 6, 30), now_ist=ist) == date(2026, 6, 13)

    # ── the exact at-close instant (DEC-SESSION-CLOSE-BOUNDARY-PIN-001) ──
    # The previously-untested boundary: exactly 15:30:00.000000 counts as
    # CLOSED here (`>=`) — the request-side verdict, deliberately opposite to
    # daily_derive_cutoff's finalize-side strict `>` (see the cross-module
    # drift-pin in tests/unit/test_session_close_boundary_pin.py).

    def test_intraday_exactly_at_close_allows_today_plus_one(self) -> None:
        ist = datetime(2026, 6, 12, 15, 30, 0, 0, tzinfo=UTC)
        assert clamp_window_end("1minute", date(2026, 6, 30), now_ist=ist) == date(2026, 6, 13)

    def test_daily_exactly_at_close_still_clamps_to_today(self) -> None:
        # The daily arm is close-blind: today's 1day candle publishes only the
        # NEXT day, so even the at-close instant keeps the today clamp.
        ist = datetime(2026, 6, 12, 15, 30, 0, 0, tzinfo=UTC)
        assert clamp_window_end("1day", date(2026, 6, 30), now_ist=ist) == date(2026, 6, 12)

    def test_intraday_one_second_before_close_clamps_to_today(self) -> None:
        ist = datetime(2026, 6, 12, 15, 29, 59, tzinfo=UTC)
        assert clamp_window_end("1minute", date(2026, 6, 30), now_ist=ist) == date(2026, 6, 12)

    def test_intraday_one_second_after_close_allows_today_plus_one(self) -> None:
        ist = datetime(2026, 6, 12, 15, 30, 1, tzinfo=UTC)
        assert clamp_window_end("1minute", date(2026, 6, 30), now_ist=ist) == date(2026, 6, 13)


class TestDropBarsAfter:
    """DEC-CANDLE-DROP-FUTURE-BAR-001: drop a vendor bar timestamped STRICTLY AFTER the
    clamped window end (a future phantom) while KEEPING an on-boundary bar (behaviour-preserving)."""

    @staticmethod
    def _bar(d: date, hour_utc: int = 4) -> CandleBar:
        # 04:00 UTC = 09:30 IST (in-session) on the given IST trading date.
        return CandleBar(
            timestamp=datetime(d.year, d.month, d.day, hour_utc, 0, tzinfo=UTC),
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("90"),
            close=Decimal("105"),
            volume=1,
            open_interest=None,
        )

    def test_keeps_bars_on_or_before_the_window_end(self) -> None:
        end = date(2024, 1, 10)
        bars = [self._bar(date(2024, 1, 8)), self._bar(date(2024, 1, 9)), self._bar(end)]
        kept, dropped = drop_bars_after(bars, end)
        assert kept == bars  # boundary bar (on the end date) is KEPT — byte-unchanged
        assert dropped == 0  # nothing past the boundary — no count surfaced

    def test_drops_a_future_phantom_bar(self) -> None:
        end = date(2024, 1, 10)
        future = self._bar(date(2024, 1, 15))  # a clock-skew / out-of-window phantom
        good = self._bar(date(2024, 1, 9))
        kept, dropped = drop_bars_after([good, future], end)
        assert future not in kept
        assert kept == [good]  # the good bar is byte-identical (KEPT)
        assert len(kept) == 1
        assert dropped == 1  # the mixed-in future phantom is COUNTED, not silent

    def test_empty_input_is_empty(self) -> None:
        assert drop_bars_after([], date(2024, 1, 10)) == ([], 0)

    def test_uses_ist_trading_date_not_utc_date(self) -> None:
        # 20:00 UTC on the end date = 01:30 IST the NEXT day → IST date is end+1 → DROPPED.
        end = date(2024, 1, 10)
        late_utc = self._bar(end, hour_utc=20)  # 20:00 UTC Jan-10 = 01:30 IST Jan-11
        kept, dropped = drop_bars_after([late_utc], end)
        assert kept == []  # the IST date (Jan-11) is strictly after the end
        assert dropped == 1


class TestNowIst:
    def test_now_ist_is_kolkata_tz(self) -> None:
        """The production clock default carries the Asia/Kolkata tz."""
        assert now_ist().tzinfo is not None
        assert "Kolkata" in str(now_ist().tzinfo)


def _make_conflict(
    *,
    ts_epoch: int = 1609733100,
    stored_close: str = "105.00",
    vendor_close: str = "104.50",
) -> BarConflict:
    """Build a minimal :class:`BarConflict` for direct summarize_conflicts cover."""
    ts = datetime.fromtimestamp(ts_epoch, tz=UTC)
    stored = CandleBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("110.00"),
        low=Decimal("90.00"),
        close=Decimal(stored_close),
        volume=1,
        open_interest=None,
    )
    vendor = CandleBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("110.00"),
        low=Decimal("90.00"),
        close=Decimal(vendor_close),
        volume=1,
        open_interest=None,
    )
    return BarConflict(timestamp=ts, stored=stored, vendor=vendor)


class TestSummarizeConflicts:
    """Direct unit cover for the pure ``summarize_conflicts`` helper
    (DEC-LAKE-CONFLICT-LOG-AGG-001)."""

    def test_empty_is_zero_and_none(self) -> None:
        """No conflicts → zero count, empty fields, zero delta, null timestamps."""
        assert summarize_conflicts([]) == {
            "conflicts": 0,
            "differing_fields": "",
            "max_close_delta": "0",
            "first_ts": None,
            "last_ts": None,
        }

    def test_first_and_last_ts_are_order_independent(self) -> None:
        """``first_ts`` / ``last_ts`` are the min / max regardless of input order."""
        early = datetime.fromtimestamp(1609733100, tz=UTC)
        late = datetime.fromtimestamp(1609740000, tz=UTC)
        # Feed LATE first, EARLY second — min/max must still bracket correctly.
        c_late = _make_conflict(ts_epoch=int(late.timestamp()))
        c_early = _make_conflict(ts_epoch=int(early.timestamp()))
        summary = summarize_conflicts([c_late, c_early])
        assert summary["conflicts"] == 2
        assert summary["first_ts"] == early.isoformat()
        assert summary["last_ts"] == late.isoformat()

    def test_differing_fields_and_max_close_delta(self) -> None:
        """The differing OHLCV+OI field set + max abs close delta are reported."""
        summary = summarize_conflicts(
            [
                _make_conflict(stored_close="105.00", vendor_close="104.50"),
                _make_conflict(ts_epoch=1609740000, stored_close="100.00", vendor_close="102.00"),
            ]
        )
        assert summary["differing_fields"] == "close"
        assert summary["max_close_delta"] == "2.00"


class TestWindowDaysForInterval:
    """The vendor-neutral interval→tiling-width policy (the back-compat default)."""

    def test_daily_is_a_single_untiled_window(self) -> None:
        """A ``1day`` pull takes the whole range in ONE call → ``None`` width."""
        assert window_days_for_interval("1day") is None

    def test_intraday_uses_the_flat_90_day_cap(self) -> None:
        """Every intraday interval tiles at the flat 90-day per-call cap."""
        assert window_days_for_interval("1minute") == 90
        assert window_days_for_interval("5minute") == 90


class TestFetchUntilNonempty:
    """The COMMON pull-until-successful primitive (DEC-EMPTY-RETRY-EVERYWHERE-001)."""

    def test_non_empty_on_first_call_no_retry(self) -> None:
        calls: list[int] = []

        def _fetch() -> list[int]:
            calls.append(1)
            return [42]

        assert fetch_until_nonempty(_fetch, is_empty=lambda r: not r) == [42]
        assert len(calls) == 1  # happy path → exactly one call

    def test_glitchy_empty_recovers_on_second_call(self) -> None:
        seq: list[list[int]] = [[], [7]]  # empty glitch, then real data

        def _fetch() -> list[int]:
            return seq.pop(0)

        result = fetch_until_nonempty(_fetch, is_empty=lambda r: not r, attempts=3)
        assert result == [7]  # the glitch was recovered — "pull until successful"

    def test_genuine_empty_bounded_to_attempts(self) -> None:
        calls: list[int] = []

        def _fetch() -> list[int]:
            calls.append(1)
            return []  # always empty

        result = fetch_until_nonempty(_fetch, is_empty=lambda r: not r, attempts=3)
        assert result == []  # confidently empty
        assert len(calls) == 3  # bounded — exactly `attempts`, never loops forever

    def test_default_attempts_is_three(self) -> None:
        calls: list[int] = []

        def _fetch() -> list[int]:
            calls.append(1)
            return []

        fetch_until_nonempty(_fetch, is_empty=lambda r: not r)
        assert len(calls) == DEFAULT_EMPTY_RETRY_ATTEMPTS == 3

    def test_attempts_one_disables_retry(self) -> None:
        calls: list[int] = []

        def _fetch() -> list[int]:
            calls.append(1)
            return []

        assert fetch_until_nonempty(_fetch, is_empty=lambda r: not r, attempts=1) == []
        assert len(calls) == 1  # a single call, no retry

    def test_backoff_fires_before_each_refetch(self) -> None:
        backoffs: list[int] = []

        def _fetch() -> list[int]:
            return []

        fetch_until_nonempty(_fetch, is_empty=lambda r: not r, attempts=3, backoff=backoffs.append)
        # backoff runs before re-fetch on attempts 2 and 3 (not before the first call)
        assert backoffs == [2, 3]


class TestMaybeAudit:
    """`maybe_audit` — the ONE COMMON audit guard both ingest spines share
    (DEC-INGEST-AUDIT-WIRING-001, Golden Rule #8)."""

    def test_none_is_a_noop(self) -> None:
        from brutex.adapters.ingest_common import maybe_audit

        maybe_audit(None, "INGEST_RUN_START", "pull:x", {"jobs": 1})  # must not raise

    def test_wired_writer_receives_one_hash_chained_line(self, tmp_path) -> None:
        from brutex.adapters.ingest_common import maybe_audit
        from brutex.orchestrator.audit import AuditWriter, read_entries, verify_chain

        path = tmp_path / "audit.jsonl"
        with AuditWriter.open(path) as writer:
            maybe_audit(writer, "INGEST_PAIR_DONE", "NIFTY:1minute", {"bars_appended": 3})
        assert verify_chain(path) == 1
        entry = read_entries(path)[0]
        assert entry.action == "INGEST_PAIR_DONE"
        assert entry.subject == "NIFTY:1minute"
        assert entry.payload == {"bars_appended": 3}
