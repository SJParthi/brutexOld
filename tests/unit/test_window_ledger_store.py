"""Unit tests for :mod:`brutex.data.window_ledger_store` (DEC-WINDOW-LEDGER-001, PR-2).

100% line+branch on the durable Parquet backing: the save→load row fixed point,
the never-raise degradation (missing / corrupt / wrong-shape), the one-shot
migration that folds the legacy status-log sidecar (the coverage sidecar fold was
retired in Stage e), the PLANNED upsert (`plan_windows`), the throttled live
flush, and the begin/end dry-run rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from brutex.data.fetch_coverage import WindowKey
from brutex.data.window_ledger import (
    EMPTY_TERMINAL_THRESHOLD,
    SKIPPED,
    SUCCESS,
    WindowLedger,
)
from brutex.data.window_ledger_store import (
    begin_pull_ledger,
    end_pull_ledger,
    ledger_dir,
    ledger_path,
    load_or_migrate_window_ledger,
    load_window_ledger,
    make_live_ledger_flush,
    make_locked_flush,
    plan_windows,
    reconcile_ledger_windows,
    reconcile_ledger_with_lake,
    save_window_ledger,
)
from brutex.data.window_status import WindowStatusLog
from brutex.data.window_status_store import save_window_status
from structlog.testing import capture_logs

_NOW = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
_START = date(2021, 1, 1)
_END = date(2021, 8, 1)


def _key(security_id: str = "NIFTY", interval: str = "1minute") -> WindowKey:
    return WindowKey(
        market="dhan",
        security_id=security_id,
        interval=interval,
        start=date(2021, 1, 1),
        end=date(2021, 4, 1),
    )


@dataclass(frozen=True)
class _FakeInstrument:
    """The minimal shape `plan_windows` reads (`symbol_name`)."""

    symbol_name: str


def test_paths(tmp_path: Path) -> None:
    assert ledger_dir(tmp_path) == tmp_path / "window_ledger"
    assert ledger_path(tmp_path) == tmp_path / "window_ledger" / "ledger.parquet"


def test_save_then_load_is_a_row_fixed_point(tmp_path: Path) -> None:
    ledger = WindowLedger()
    ledger.record(_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
    ledger.plan(_key("SENSEX"), now=_NOW)  # last_attempt_at None arm
    ledger.record(
        _key("BANKNIFTY"), raw_bars=0, error_type="DhanRateLimitError", bars_appended=0, now=_NOW
    )
    save_window_ledger(ledger, tmp_path)
    restored = load_window_ledger(tmp_path)
    assert restored.to_rows() == ledger.to_rows()
    assert restored.is_skippable(_key("NIFTY")) is True
    assert restored.is_skippable(_key("BANKNIFTY")) is False


def test_save_empty_then_load_empty(tmp_path: Path) -> None:
    save_window_ledger(WindowLedger(), tmp_path)
    assert load_window_ledger(tmp_path).count == 0


def test_load_missing_is_empty(tmp_path: Path) -> None:
    assert load_window_ledger(tmp_path).count == 0


def test_load_corrupt_parquet_degrades_to_empty(tmp_path: Path) -> None:
    path = ledger_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a parquet file at all")
    assert load_window_ledger(tmp_path).count == 0  # WARNING, never raises


def test_load_wrong_shape_parquet_degrades_to_empty(tmp_path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    path = ledger_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A valid Parquet that is MISSING the ledger columns → read with the fixed
    # column list raises → degrade to empty.
    pq.write_table(pa.table({"unexpected": [1, 2, 3]}), path)
    assert load_window_ledger(tmp_path).count == 0


def test_load_honours_threshold(tmp_path: Path) -> None:
    ledger = WindowLedger(_threshold=2)
    ledger.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
    save_window_ledger(ledger, tmp_path)
    restored = load_window_ledger(tmp_path, threshold=2)
    # The injected threshold of 2 → one more EMPTY promotes to SKIPPED.
    assert (
        restored.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW) == SKIPPED
    )


class TestMigration:
    """The one-shot fold of the legacy status-log sidecar (coverage fold retired)."""

    def test_migrate_folds_status_log_sidecar(self, tmp_path: Path) -> None:
        # Status log: a SUCCESS (NIFTY) + a terminal EMPTY (SENSEX, at the threshold).
        log = WindowStatusLog()
        log.record(_key("NIFTY"), raw_bars=9, error_type=None, bars_appended=9, now=_NOW)
        for _ in range(EMPTY_TERMINAL_THRESHOLD):
            log.record(_key("SENSEX"), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        save_window_status(log, tmp_path)

        ledger = load_or_migrate_window_ledger(tmp_path, now=_NOW)
        by_symbol = {r.security_id: r for r in ledger.records()}
        assert by_symbol["NIFTY"].status == SUCCESS
        assert by_symbol["NIFTY"].bars == 9  # status-log row folded
        assert by_symbol["SENSEX"].status == SKIPPED  # EMPTY-at-threshold → SKIPPED
        # The legacy status log carries no in-pull fetch count — each migrated row
        # derives the honest floor of 1 (DEC-WINDOW-INPULL-FETCHES-001).
        assert by_symbol["NIFTY"].in_pull_fetches == 1
        assert by_symbol["SENSEX"].in_pull_fetches == 1

    def test_migrate_empty_sidecars_is_empty_ledger(self, tmp_path: Path) -> None:
        assert load_or_migrate_window_ledger(tmp_path, now=_NOW).count == 0

    def test_existing_parquet_is_loaded_not_remigrated(self, tmp_path: Path) -> None:
        ledger = WindowLedger()
        ledger.record(_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        save_window_ledger(ledger, tmp_path)
        # A stale status-log sidecar must be IGNORED once the Parquet exists.
        log = WindowStatusLog()
        log.record(_key("BANKNIFTY"), raw_bars=7, error_type=None, bars_appended=7, now=_NOW)
        save_window_status(log, tmp_path)
        restored = load_or_migrate_window_ledger(tmp_path, now=_NOW)
        assert {r.security_id for r in restored.records()} == {"NIFTY"}

    def test_migrate_defaults_now(self, tmp_path: Path) -> None:
        log = WindowStatusLog()
        log.record(_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        save_window_status(log, tmp_path)
        ledger = load_or_migrate_window_ledger(tmp_path)  # now=None → datetime.now
        assert ledger.records()[0].status == SUCCESS


class TestPlanWindows:
    def test_plans_every_tile_idempotently(self, tmp_path: Path) -> None:
        from brutex.adapters.ingest_common import tile_windows
        from brutex.adapters.ingest_common import window_days_for_interval as _window_days_for

        def _windows(interval: str, start: date, end: date) -> list[tuple[date, date]]:
            return tile_windows(interval, start, end, window_days_for=_window_days_for)

        ledger = WindowLedger()
        instruments = [_FakeInstrument("NIFTY"), _FakeInstrument("SENSEX")]
        intervals = ["1minute", "1day"]
        planned = plan_windows(
            ledger, instruments=instruments, intervals=intervals, start=_START, end=_END, now=_NOW
        )
        expected_per_symbol = len(list(_windows("1minute", _START, _END))) + len(
            list(_windows("1day", _START, _END))
        )
        assert planned == expected_per_symbol * 2
        assert ledger.count == planned
        # Idempotent: a second plan is a no-op (no new rows).
        plan_windows(
            ledger, instruments=instruments, intervals=intervals, start=_START, end=_END, now=_NOW
        )
        assert ledger.count == planned

    def test_plan_windows_defaults_now(self, tmp_path: Path) -> None:
        ledger = WindowLedger()
        plan_windows(
            ledger,
            instruments=[_FakeInstrument("NIFTY")],
            intervals=["1day"],
            start=_START,
            end=_END,
        )
        assert ledger.count >= 1


class TestPlanWindowsSymbolWindows:
    """Per-symbol opt-in sub-daily planning (plan_windows ``symbol_windows``).

    The SPOT pull paths pass ``None`` since DEC-SPOT-INTERVAL-DECOUPLE-001 (MIGRATION.md
    #851 — the 1-minute fetch is fully decoupled from the 1-day coverage), but the
    ledger still accepts the opt-in map (the never-narrow fallback) — covered here.
    """

    def _minute_tiles(self, start: date, end: date) -> list[tuple[date, date]]:
        from brutex.adapters.ingest_common import tile_windows
        from brutex.adapters.ingest_common import window_days_for_interval as _wdf

        return tile_windows("1minute", start, end, window_days_for=_wdf)

    def test_bounded_sub_daily_symbol_tiles_from_its_window(self) -> None:
        # A symbol in the map → its SUB-DAILY (1minute) windows are tiled from the
        # mapped ``(start, end)``, NOT the global range (lines 443-450).
        bound = (date(2026, 4, 1), date(2026, 6, 1))
        ledger = WindowLedger()
        # ``now_ist`` past the bound's end so the daily/intraday clamp is inert.
        plan_windows(
            ledger,
            instruments=[_FakeInstrument("NIFTY")],
            intervals=["1minute"],
            start=_START,
            end=_END,
            now=_NOW,
            now_ist=datetime(2026, 6, 8, tzinfo=ZoneInfo("Asia/Kolkata")),
            symbol_windows={"NIFTY": bound},
        )
        starts = {date.fromisoformat(r.window_start) for r in ledger.records()}
        # Every planned window starts at/after the BOUNDED start — never the 2021 floor.
        assert min(starts) == bound[0]
        assert all(s >= bound[0] for s in starts)
        # The count matches the bounded tiling, not the (far larger) global one.
        clamped_end = min(bound[1], date(2026, 6, 8) + timedelta(days=1))
        assert ledger.count == len(self._minute_tiles(bound[0], clamped_end))

    def test_daily_interval_is_never_bounded(self) -> None:
        # Even with the symbol in the map, the DAILY interval tiles the GLOBAL range
        # (it produces the coverage + must advance to the requested end).
        bound = (date(2026, 4, 1), date(2026, 6, 1))
        bounded = WindowLedger()
        plan_windows(
            bounded,
            instruments=[_FakeInstrument("NIFTY")],
            intervals=["1day"],
            start=_START,
            end=_END,
            now=_NOW,
            symbol_windows={"NIFTY": bound},
        )
        unbounded = WindowLedger()
        plan_windows(
            unbounded,
            instruments=[_FakeInstrument("NIFTY")],
            intervals=["1day"],
            start=_START,
            end=_END,
            now=_NOW,
        )
        # The bounded map made NO difference to the daily plan.
        assert bounded.to_rows() == unbounded.to_rows()

    def test_unbounded_symbol_falls_back_to_global_tiles(self) -> None:
        # A symbol ABSENT from the map (BANKNIFTY) uses the shared global tiles, while
        # the bounded NIFTY uses its narrow window — both in one plan.
        bound = (date(2026, 4, 1), date(2026, 6, 1))
        ledger = WindowLedger()
        plan_windows(
            ledger,
            instruments=[_FakeInstrument("NIFTY"), _FakeInstrument("BANKNIFTY")],
            intervals=["1minute"],
            start=_START,
            end=_END,
            now=_NOW,
            symbol_windows={"NIFTY": bound},
        )
        nifty_starts = {
            date.fromisoformat(r.window_start) for r in ledger.records() if r.security_id == "NIFTY"
        }
        bank_starts = {
            date.fromisoformat(r.window_start)
            for r in ledger.records()
            if r.security_id == "BANKNIFTY"
        }
        assert min(nifty_starts) == bound[0]  # NIFTY bounded
        assert min(bank_starts) == _START  # BANKNIFTY global (the 2021 floor)

    def test_empty_bounded_window_plans_nothing_for_that_symbol(self) -> None:
        # A degenerate bound where ``sym_start >= clamped_end`` → ZERO tiles for that
        # symbol's sub-daily interval (the ``else []`` arm of _tiles_for).
        bound = (date(2026, 6, 5), date(2026, 6, 5))  # start == end → empty tiling
        ledger = WindowLedger()
        planned = plan_windows(
            ledger,
            instruments=[_FakeInstrument("NIFTY")],
            intervals=["1minute"],
            start=_START,
            end=_END,
            now=_NOW,
            symbol_windows={"NIFTY": bound},
        )
        assert planned == 0
        assert ledger.count == 0


class TestPruneOnPlan:
    """DEC-WINDOW-TRAILING-GC-001 — plan_windows prunes superseded trailing rows.

    The daily ``end`` advance re-keys the clipped trailing window (1minute) and
    the single whole-range window (1day); yesterday's rows are orphans the tiler
    can never re-emit. Planning today's tiling must remove them — and ONLY them.
    """

    def test_trailing_orphans_of_both_intervals_are_pruned(self) -> None:
        ledger = WindowLedger()
        instruments = [_FakeInstrument("NIFTY")]
        intervals = ["1minute", "1day"]
        # Yesterday's pull: end one day short — its trailing keys orphan today.
        yesterday_end = _END - timedelta(days=1)
        plan_windows(
            ledger,
            instruments=instruments,
            intervals=intervals,
            start=_START,
            end=yesterday_end,
            now=_NOW,
        )
        # Today's pull: end advanced — yesterday's trailing 1minute partial AND
        # the whole-range 1day window are superseded; the full tiles are not.
        planned_today = plan_windows(
            ledger,
            instruments=instruments,
            intervals=intervals,
            start=_START,
            end=_END,
            now=_NOW,
        )
        assert ledger.count == planned_today  # zero orphans survive
        ends = {(r.interval, r.window_end) for r in ledger.records()}
        assert ("1day", yesterday_end.isoformat()) not in ends
        assert ("1day", _END.isoformat()) in ends
        assert ("1minute", yesterday_end.isoformat()) not in ends

    def test_settled_equal_end_rows_survive_a_same_day_replan(self) -> None:
        # Run `make pull` twice the same day: the second plan must keep the
        # first pull's settled rows (equal-end keys) so the skip still fires.
        ledger = WindowLedger()
        plan_windows(
            ledger,
            instruments=[_FakeInstrument("NIFTY")],
            intervals=["1day"],
            start=_START,
            end=_END,
            now=_NOW,
        )
        key = WindowKey(market="dhan", security_id="NIFTY", interval="1day", start=_START, end=_END)
        ledger.record(key, raw_bars=9, error_type=None, bars_appended=9, now=_NOW)
        plan_windows(
            ledger,
            instruments=[_FakeInstrument("NIFTY")],
            intervals=["1day"],
            start=_START,
            end=_END,
            now=_NOW,
        )
        assert ledger.is_skippable(key) is True

    def test_prune_event_logged_only_when_rows_pruned(self) -> None:
        ledger = WindowLedger()
        with capture_logs() as clean_logs:
            plan_windows(
                ledger,
                instruments=[_FakeInstrument("NIFTY")],
                intervals=["1day"],
                start=_START,
                end=_END,
                now=_NOW,
            )
        assert not [e for e in clean_logs if e["event"] == "window_ledger.pruned_superseded"]
        with capture_logs() as logs:
            plan_windows(
                ledger,
                instruments=[_FakeInstrument("NIFTY")],
                intervals=["1day"],
                start=_START,
                end=_END + timedelta(days=1),
                now=_NOW,
            )
        events = [e for e in logs if e["event"] == "window_ledger.pruned_superseded"]
        assert len(events) == 1
        assert events[0]["pruned"] == 1

    def test_reconcile_never_reseeds_a_pruned_key(self) -> None:
        # DEC-WINDOW-TRAILING-GC-001 hostile F5: the ONLY deletion→insert
        # coupling. reconcile seeds ABSENT windows — but only over the CURRENT
        # plan's tiling, whose keys the prune always keeps (equal-end rule).
        # Pin that after plan→prune→reconcile, no pruned (shorter-end) key has
        # been re-seeded and every current-plan key survived the prune.
        from brutex.adapters.ingest_common import tile_windows
        from brutex.adapters.ingest_common import window_days_for_interval as _window_days_for

        def _windows(interval: str, start: date, end: date) -> list[tuple[date, date]]:
            return tile_windows(interval, start, end, window_days_for=_window_days_for)

        ledger = WindowLedger()
        instrument = _inst(symbol="NIFTY")
        yesterday_end = _END - timedelta(days=1)
        plan_windows(
            ledger,
            instruments=[instrument],
            intervals=["1day"],
            start=_START,
            end=yesterday_end,
            now=_NOW,
        )
        plan_windows(
            ledger,
            instruments=[instrument],
            intervals=["1day"],
            start=_START,
            end=_END,
            now=_NOW,
        )
        reconcile_ledger_windows(
            ledger,
            [_span(interval="1day", last="2026-06-03")],  # encloses both tilings
            instruments=[instrument],
            intervals=["1day"],
            start=_START,
            end=_END,
            now=_NOW,
        )
        ends = {r.window_end for r in ledger.records()}
        assert yesterday_end.isoformat() not in ends  # pruned key NOT re-seeded
        current = {we.isoformat() for _ws, we in _windows("1day", _START, _END)}
        assert current <= ends  # every current-plan key present


class TestPlanClampParity:
    """DEC-PLAN-CLAMP-PARITY-001 — the planner tiles the SAME clamped end the fetch does.

    The live 2026-06-10 ghost class: `plan_windows` tiled the RAW pull end while
    `ingest_spot` tiled the `clamp_window_end` bound (DEC-DAILY-CANDLE-CLAMP-001),
    so every (instrument, 1day) pull minted a PLANNED key the fetch could never
    record — 783 permanently-pending rows on /coverage/windows after one
    universe pull. The planner now clamps per interval with an injected clock.
    """

    _IST = ZoneInfo("Asia/Kolkata")

    def test_daily_plan_is_clamped_to_today(self) -> None:
        ledger = WindowLedger()
        plan_windows(
            ledger,
            instruments=[_FakeInstrument("NIFTY")],
            intervals=["1day"],
            start=_START,
            end=date(2026, 6, 11),
            now=_NOW,
            now_ist=datetime(2026, 6, 10, 21, 51, tzinfo=self._IST),  # post-close
        )
        # The planned key is the FETCH key (end-exclusive yesterday's candle),
        # never the raw tomorrow-ending ghost.
        assert {r.window_end for r in ledger.records()} == {"2026-06-10"}

    def test_intraday_clamp_follows_the_1530_close(self) -> None:
        pre = WindowLedger()
        plan_windows(
            pre,
            instruments=[_FakeInstrument("NIFTY")],
            intervals=["1minute"],
            start=date(2026, 6, 9),
            end=date(2026, 6, 11),
            now=_NOW,
            now_ist=datetime(2026, 6, 10, 14, 0, tzinfo=self._IST),  # mid-session
        )
        assert max(r.window_end for r in pre.records()) == "2026-06-10"
        post = WindowLedger()
        plan_windows(
            post,
            instruments=[_FakeInstrument("NIFTY")],
            intervals=["1minute"],
            start=date(2026, 6, 9),
            end=date(2026, 6, 11),
            now=_NOW,
            now_ist=datetime(2026, 6, 10, 15, 30, tzinfo=self._IST),  # at close
        )
        assert max(r.window_end for r in post.records()) == "2026-06-11"

    def test_fully_clamped_interval_plans_nothing(self) -> None:
        # start == clamped end (a same-day 1day-only pull) → zero tiles, zero
        # rows — the same `start < clamped_end` empty-range guard as ingest_spot.
        ledger = WindowLedger()
        planned = plan_windows(
            ledger,
            instruments=[_FakeInstrument("NIFTY")],
            intervals=["1day"],
            start=date(2026, 6, 10),
            end=date(2026, 6, 11),
            now=_NOW,
            now_ist=datetime(2026, 6, 10, 21, 0, tzinfo=self._IST),
        )
        assert planned == 0
        assert ledger.count == 0

    def test_operator_ghost_self_heals_on_next_plan(self) -> None:
        # The LIVE lake state this decision fixes: a PLANNED ghost at the raw
        # end (attempts 0) beside the settled clamped row. The next pull's
        # plan→prune cycle deletes the ghost and keeps the history skippable.
        ledger = WindowLedger()
        ghost = WindowKey(
            market="spot",
            security_id="NIFTY",
            interval="1day",
            start=_START,
            end=date(2026, 6, 11),
        )
        settled = WindowKey(
            market="spot",
            security_id="NIFTY",
            interval="1day",
            start=_START,
            end=date(2026, 6, 10),
        )
        ledger.plan(ghost, now=_NOW)
        ledger.record(settled, raw_bars=9, error_type=None, bars_appended=9, now=_NOW)
        plan_windows(
            ledger,
            instruments=[_FakeInstrument("NIFTY")],
            intervals=["1day"],
            start=_START,
            end=date(2026, 6, 11),
            now=_NOW,
            now_ist=datetime(2026, 6, 10, 21, 51, tzinfo=self._IST),
        )
        assert "2026-06-11" not in {r.window_end for r in ledger.records()}  # ghost gone
        assert ledger.is_skippable(settled) is True  # settled history untouched


class TestLiveFlush:
    def test_none_ledger_is_a_noop(self, tmp_path: Path) -> None:
        flush = make_live_ledger_flush(None, tmp_path)
        flush()  # no crash, no file
        assert not ledger_path(tmp_path).exists()

    def test_throttles_writes(self, tmp_path: Path) -> None:
        ledger = WindowLedger()
        ledger.record(_key(), raw_bars=1, error_type=None, bars_appended=1, now=_NOW)
        clock = [100.0]
        flush = make_live_ledger_flush(
            ledger, tmp_path, min_interval_s=10.0, clock=lambda: clock[0]
        )
        flush()  # first write
        assert ledger_path(tmp_path).is_file()
        ledger_path(tmp_path).unlink()
        clock[0] = 105.0  # within the interval → skipped
        flush()
        assert not ledger_path(tmp_path).exists()
        clock[0] = 120.0  # past the interval → writes again
        flush()
        assert ledger_path(tmp_path).is_file()

    def test_default_clock_first_call_writes(self, tmp_path: Path) -> None:
        ledger = WindowLedger()
        ledger.record(_key(), raw_bars=1, error_type=None, bars_appended=1, now=_NOW)
        make_live_ledger_flush(ledger, tmp_path)()  # clock=None → monotonic branch
        assert ledger_path(tmp_path).is_file()

    def test_adaptive_throttle_scales_with_save_duration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Hostile C2 (DEC-OPTION-WINDOW-PLAN-001): at a pre-planned universe
        # scale a save measured ~3.94 s > the fixed 2 s interval, degenerating
        # the throttle to flush-per-tick (~13.7 h of pure Parquet rewriting on
        # a 12.5k-outcome drain). The interval must scale to
        # _FLUSH_DUTY_FACTOR × the LAST save duration.
        from brutex.data import window_ledger_store as store_module

        ledger = WindowLedger()
        ledger.record(_key(), raw_bars=1, error_type=None, bars_appended=1, now=_NOW)
        clock = [100.0]
        saves: list[float] = []

        def _slow_counting_save(_ledger: WindowLedger, _root: Path) -> None:
            saves.append(clock[0])
            clock[0] += 4.0  # the save itself takes 4 simulated seconds

        monkeypatch.setattr(store_module, "save_window_ledger", _slow_counting_save)
        flush = make_live_ledger_flush(ledger, tmp_path, min_interval_s=2.0, clock=lambda: clock[0])

        flush()  # first write — measures the 4 s save
        assert len(saves) == 1
        clock[0] += 3.0  # 3 s later: past the FIXED 2 s, but < 5 × 4 s = 20 s
        flush()
        assert len(saves) == 1  # adaptive interval held — no flush-per-tick
        clock[0] += 25.0  # well past 20 s → writes again
        flush()
        assert len(saves) == 2


class TestMakeLockedFlush:
    """``make_locked_flush`` — the thread-safe single-flight wrapper for the CLI
    per-window ledger checkpoint (DEC-CLI-LEDGER-CHECKPOINT-001)."""

    def test_forwards_every_call_to_the_wrapped_flush(self) -> None:
        """Each fire forwards once to the wrapped 0-arg flush (no swallowing)."""
        calls = {"n": 0}

        def _flush() -> None:
            calls["n"] += 1

        locked = make_locked_flush(_flush)
        locked()
        locked()
        assert calls["n"] == 2

    def test_concurrent_fires_never_overlap(self) -> None:
        """Under parallel worker-thread fires the wrapped flush runs strictly serially.

        Models the ingest engine firing ``on_window`` from many worker threads at
        once: the single-flight lock must guarantee the (non-thread-safe) ledger
        write never has two fires in flight simultaneously.
        """
        import threading
        import time

        depth = {"now": 0, "max": 0}
        barrier = threading.Barrier(8)

        def _flush() -> None:
            depth["now"] += 1
            depth["max"] = max(depth["max"], depth["now"])
            time.sleep(0.0005)  # widen the window so an unserialised fire would overlap
            depth["now"] -= 1

        locked = make_locked_flush(_flush)

        def _worker() -> None:
            barrier.wait()  # release all threads together to maximise contention
            for _ in range(25):
                locked()

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert depth["max"] == 1  # the lock kept every flush single-flight


class TestBeginEnd:
    def test_begin_dry_run_is_none(self, tmp_path: Path) -> None:
        assert begin_pull_ledger(tmp_path, dry_run=True) is None

    def test_begin_live_loads_or_migrates(self, tmp_path: Path) -> None:
        ledger = begin_pull_ledger(tmp_path, dry_run=False, now=_NOW)
        assert ledger is not None
        assert ledger.count == 0

    def test_end_persists_live(self, tmp_path: Path) -> None:
        ledger = WindowLedger()
        ledger.record(_key(), raw_bars=1, error_type=None, bars_appended=1, now=_NOW)
        end_pull_ledger(ledger, tmp_path, dry_run=False)
        assert ledger_path(tmp_path).is_file()

    def test_end_dry_run_is_noop(self, tmp_path: Path) -> None:
        ledger = WindowLedger()
        ledger.record(_key(), raw_bars=1, error_type=None, bars_appended=1, now=_NOW)
        end_pull_ledger(ledger, tmp_path, dry_run=True)
        assert not ledger_path(tmp_path).exists()

    def test_end_none_ledger_is_noop(self, tmp_path: Path) -> None:
        end_pull_ledger(None, tmp_path, dry_run=False)
        assert not ledger_path(tmp_path).exists()


def test_load_failed_logs_warning(tmp_path: Path) -> None:
    # The ImportError/degradation path also covers when bars/attempts coerce fails:
    # write a parquet whose `bars` column is non-int-coercible text.
    import pyarrow as pa
    import pyarrow.parquet as pq

    path = ledger_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = {
        "market": ["dhan"],
        "security_id": ["NIFTY"],
        "interval": ["1minute"],
        "window_start": ["2021-01-01"],
        "window_end": ["2021-04-01"],
        "status": ["SUCCESS"],
        "bars": ["not-an-int"],
        "attempts": ["1"],
        "last_error": [None],
        "first_seen_at": ["2026-06-08T12:00:00+00:00"],
        "last_attempt_at": ["2026-06-08T12:00:00+00:00"],
    }
    pq.write_table(pa.table(cols), path)
    assert load_window_ledger(tmp_path).count == 0  # int() raises → degrade


def _write_legacy_ledger_parquet(tmp_path: Path, rows: dict) -> None:
    """Write a PRE-R1 ledger Parquet (the core 11 columns, NO ``empty_streak``)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    path = ledger_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(rows), path)


class TestLegacyBackwardCompat:
    """A pre-R1 Parquet (no ``empty_streak``) loads cleanly with a derived streak (never a re-pull)."""

    def test_legacy_rows_derive_empty_streak(self, tmp_path: Path) -> None:
        # One row per derive_empty_streak branch: SUCCESS→0, EMPTY→attempts, SKIPPED→threshold.
        _write_legacy_ledger_parquet(
            tmp_path,
            {
                "market": ["dhan", "dhan", "dhan"],
                "security_id": ["NIFTY", "SENSEX", "BANKNIFTY"],
                "interval": ["1minute", "1minute", "1minute"],
                "window_start": ["2021-01-01", "2021-01-01", "2021-01-01"],
                "window_end": ["2021-04-01", "2021-04-01", "2021-04-01"],
                "status": [SUCCESS, "EMPTY", SKIPPED],
                "bars": [5, 0, 0],
                "attempts": [1, 2, 3],
                "last_error": [None, None, None],
                "first_seen_at": ["2026-06-08T12:00:00+00:00"] * 3,
                "last_attempt_at": ["2026-06-08T12:00:00+00:00"] * 3,
            },
        )
        ledger = load_window_ledger(tmp_path)
        by_symbol = {r.security_id: r for r in ledger.records()}
        assert ledger.count == 3  # loaded, NOT degraded to empty
        assert by_symbol["NIFTY"].empty_streak == 0  # SUCCESS → 0
        assert by_symbol["SENSEX"].empty_streak == 2  # EMPTY → attempts (still below threshold)
        assert by_symbol["BANKNIFTY"].empty_streak == 3  # SKIPPED → threshold
        # A legacy EMPTY-below-threshold row is still fetched (not masked).
        assert ledger.is_skippable(_key("SENSEX")) is False
        assert ledger.is_skippable(_key("BANKNIFTY")) is True
        # The pre-feature file ALSO lacks ``in_pull_fetches`` — each non-PLANNED row
        # derives the honest floor of 1 (DEC-WINDOW-INPULL-FETCHES-001).
        assert by_symbol["NIFTY"].in_pull_fetches == 1
        assert by_symbol["SENSEX"].in_pull_fetches == 1
        assert by_symbol["BANKNIFTY"].in_pull_fetches == 1

    def test_legacy_rows_derive_in_pull_fetches_both_branches(self, tmp_path: Path) -> None:
        # Both derive_in_pull_fetches branches: PLANNED→0 (never fetched), SUCCESS→1.
        _write_legacy_ledger_parquet(
            tmp_path,
            {
                "market": ["dhan", "dhan"],
                "security_id": ["NIFTY", "SENSEX"],
                "interval": ["1minute", "1minute"],
                "window_start": ["2021-01-01", "2021-01-01"],
                "window_end": ["2021-04-01", "2021-04-01"],
                "status": [SUCCESS, "PLANNED"],
                "bars": [5, 0],
                "attempts": [1, 0],
                "last_error": [None, None],
                "first_seen_at": ["2026-06-08T12:00:00+00:00"] * 2,
                "last_attempt_at": ["2026-06-08T12:00:00+00:00", None],
            },
        )
        by_symbol = {r.security_id: r for r in load_window_ledger(tmp_path).records()}
        assert by_symbol["NIFTY"].in_pull_fetches == 1  # SUCCESS → fetched at least once
        assert by_symbol["SENSEX"].in_pull_fetches == 0  # PLANNED → never fetched

    def test_partial_legacy_streak_present_fetches_absent(self, tmp_path: Path) -> None:
        # A post-R1, pre-in_pull_fetches file: empty_streak IS a column, in_pull_fetches
        # is NOT — the two optional columns are read independently (streak from the
        # column, fetches derived).
        _write_legacy_ledger_parquet(
            tmp_path,
            {
                "market": ["dhan"],
                "security_id": ["NIFTY"],
                "interval": ["1minute"],
                "window_start": ["2021-01-01"],
                "window_end": ["2021-04-01"],
                "status": ["EMPTY"],
                "bars": [0],
                "attempts": [2],
                "last_error": [None],
                "first_seen_at": ["2026-06-08T12:00:00+00:00"],
                "last_attempt_at": ["2026-06-08T12:00:00+00:00"],
                "empty_streak": [2],
            },
        )
        record = load_window_ledger(tmp_path).records()[0]
        assert record.empty_streak == 2  # read from the present column
        assert record.in_pull_fetches == 1  # derived (EMPTY → fetched)

    def test_empty_streak_round_trips(self, tmp_path: Path) -> None:
        ledger = WindowLedger()
        # Build a streak of 2 (below threshold 3) so it persists as EMPTY, streak 2.
        ledger.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        ledger.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        save_window_ledger(ledger, tmp_path)
        restored = load_window_ledger(tmp_path)
        assert restored.to_rows() == ledger.to_rows()
        assert restored.records()[0].empty_streak == 2

    def test_in_pull_fetches_round_trips(self, tmp_path: Path) -> None:
        ledger = WindowLedger()
        # A 3× in-pull empty retry must survive save→load on the real Parquet column.
        ledger.record(
            _key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW, in_pull_fetches=3
        )
        save_window_ledger(ledger, tmp_path)
        restored = load_window_ledger(tmp_path)
        assert restored.to_rows() == ledger.to_rows()
        assert restored.records()[0].in_pull_fetches == 3


def _inst(symbol: str = "NIFTY", *, exchange: str = "NSE", segment: str = "CASH") -> object:
    """A minimal instrument shape `reconcile_ledger_windows` reads."""
    from types import SimpleNamespace

    return SimpleNamespace(
        symbol_name=symbol,
        exch_id=SimpleNamespace(value=exchange),
        segment=SimpleNamespace(value=segment),
    )


def _span(
    symbol: str = "NIFTY",
    interval: str = "1day",
    *,
    bars: int = 100,
    first: str | None = "2021-01-01",
    last: str | None = "2026-06-03",
    exchange: str = "NSE",
    segment: str = "CASH",
) -> dict:
    return {
        "exchange": exchange,
        "segment": segment,
        "symbol": symbol,
        "interval": interval,
        "bars": bars,
        "first_date": first,
        "last_date": last,
    }


class TestReconcileLedgerWithLake:
    """DEC-WINDOW-LEDGER-001 cold-start: seed SUCCESS for lake-enclosed absent windows."""

    def test_seeds_an_enclosed_window(self) -> None:
        ledger = WindowLedger()
        seeded = reconcile_ledger_windows(
            ledger,
            [_span(interval="1day")],
            instruments=[_inst()],
            intervals=["1day"],
            start=_START,
            end=_END,
            now=_NOW,
        )
        assert seeded == 1
        rec = ledger.records()[0]
        assert rec.status == "SUCCESS"
        assert rec.bars == 100
        assert ledger.is_skippable(
            WindowKey(market="spot", security_id="NIFTY", interval="1day", start=_START, end=_END)
        )

    def test_does_not_seed_unenclosed_window(self) -> None:
        # Span ends well before the range end → the 1day window is NOT enclosed.
        ledger = WindowLedger()
        seeded = reconcile_ledger_windows(
            ledger,
            [_span(interval="1day", last="2021-02-01")],
            instruments=[_inst()],
            intervals=["1day"],
            start=_START,
            end=_END,
            now=_NOW,
        )
        assert seeded == 0
        assert ledger.count == 0

    def test_skips_zero_bar_and_null_and_missing_spans(self) -> None:
        ledger = WindowLedger()
        # bars=0, null dates, and an interval with no span all seed nothing.
        assert (
            reconcile_ledger_windows(
                ledger,
                [_span(bars=0)],
                instruments=[_inst()],
                intervals=["1day"],
                start=_START,
                end=_END,
                now=_NOW,
            )
            == 0
        )
        assert (
            reconcile_ledger_windows(
                ledger,
                [_span(first=None, last=None)],
                instruments=[_inst()],
                intervals=["1day"],
                start=_START,
                end=_END,
                now=_NOW,
            )
            == 0
        )
        assert (
            reconcile_ledger_windows(
                ledger,
                [],
                instruments=[_inst()],
                intervals=["1day"],
                start=_START,
                end=_END,
                now=_NOW,
            )
            == 0
        )
        assert ledger.count == 0

    def test_never_overwrites_an_existing_failed_row(self) -> None:
        ledger = WindowLedger()
        key = WindowKey(market="spot", security_id="NIFTY", interval="1day", start=_START, end=_END)
        ledger.record(key, raw_bars=0, error_type="DhanRateLimitError", bars_appended=0, now=_NOW)
        seeded = reconcile_ledger_windows(
            ledger,
            [_span(interval="1day")],
            instruments=[_inst()],
            intervals=["1day"],
            start=_START,
            end=_END,
            now=_NOW,
        )
        assert seeded == 0  # the FAILED row is left for a re-pull, never masked
        assert ledger.records()[0].status == "FAILED"

    def test_with_lake_delegates_and_defaults_now(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            "brutex.dashboard.coverage_reconcile.load_lake_spans",
            lambda _root: [_span(interval="1day")],
        )
        ledger = WindowLedger()
        seeded = reconcile_ledger_with_lake(
            ledger,
            tmp_path,
            instruments=[_inst()],
            intervals=["1day"],
            start=_START,
            end=_END,
        )  # now=None → datetime.now
        assert seeded == 1
        assert ledger.records()[0].status == "SUCCESS"


class TestReconcileSymbolWindows:
    """Per-symbol opt-in sub-daily cold-start seeding (reconcile ``symbol_windows``).

    The SPOT pull paths pass ``None`` since DEC-SPOT-INTERVAL-DECOUPLE-001 (MIGRATION.md
    #851), but the reconcile still accepts the opt-in map (the never-narrow fallback) —
    covered here.
    """

    def _minute_tiles(self, start: date, end: date) -> list[tuple[date, date]]:
        from brutex.adapters.ingest_common import tile_windows
        from brutex.adapters.ingest_common import window_days_for_interval as _wdf

        return tile_windows("1minute", start, end, window_days_for=_wdf)

    def test_bounded_sub_daily_seeds_from_its_window(self) -> None:
        # The bounded SUB-DAILY tiling (lines 547-549): a 1minute symbol in the map is
        # tiled from its mapped ``(start, end)`` so a seeded SUCCESS key matches a
        # bounded planned key exactly (no orphan). A span enclosing the bound seeds it.
        bound = (date(2026, 4, 1), date(2026, 6, 1))
        ledger = WindowLedger()
        seeded = reconcile_ledger_windows(
            ledger,
            [_span(interval="1minute", first="2026-03-01", last="2026-07-01", bars=500)],
            instruments=[_inst()],
            intervals=["1minute"],
            start=_START,
            end=_END,
            now=_NOW,
            symbol_windows={"NIFTY": bound},
        )
        # Every seeded window lies WITHIN the bounded range — never the 2021 floor.
        starts = {date.fromisoformat(r.window_start) for r in ledger.records()}
        assert seeded == ledger.count == len(self._minute_tiles(*bound))
        assert min(starts) == bound[0]

    def test_daily_interval_is_never_bounded_on_reconcile(self) -> None:
        # The DAILY interval tiles the GLOBAL range even with the symbol in the map —
        # the bounded/None reconcile must agree for daily (the never-bound guard).
        bound = (date(2026, 4, 1), date(2026, 6, 1))
        span = _span(interval="1day", first="2020-12-01", last="2026-06-30", bars=900)
        bounded = WindowLedger()
        b_seeded = reconcile_ledger_windows(
            bounded,
            [span],
            instruments=[_inst()],
            intervals=["1day"],
            start=_START,
            end=_END,
            now=_NOW,
            symbol_windows={"NIFTY": bound},
        )
        unbounded = WindowLedger()
        u_seeded = reconcile_ledger_windows(
            unbounded,
            [span],
            instruments=[_inst()],
            intervals=["1day"],
            start=_START,
            end=_END,
            now=_NOW,
        )
        assert b_seeded == u_seeded
        assert bounded.to_rows() == unbounded.to_rows()

    def test_unbounded_symbol_uses_global_reconcile_tiles(self) -> None:
        # A symbol ABSENT from the map tiles the global range (the never-narrow
        # fallback) — proven by an identical seed vs the None-map call.
        span = _span(interval="1minute", first="2020-12-01", last="2026-07-01", bars=500)
        with_map = WindowLedger()
        m_seeded = reconcile_ledger_windows(
            with_map,
            [span],
            instruments=[_inst()],
            intervals=["1minute"],
            start=_START,
            end=_END,
            now=_NOW,
            symbol_windows={"OTHER": (date(2026, 4, 1), date(2026, 6, 1))},
        )
        without_map = WindowLedger()
        n_seeded = reconcile_ledger_windows(
            without_map,
            [span],
            instruments=[_inst()],
            intervals=["1minute"],
            start=_START,
            end=_END,
            now=_NOW,
        )
        assert m_seeded == n_seeded
        assert with_map.to_rows() == without_map.to_rows()
