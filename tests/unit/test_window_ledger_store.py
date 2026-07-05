"""Unit tests for :mod:`brutex.data.window_ledger_store` (DEC-WINDOW-LEDGER-001, PR-2).

100% line+branch on the durable Parquet backing: the save→load row fixed point,
the never-raise degradation (missing / corrupt / wrong-shape), the one-shot
migration that folds the legacy status-log sidecar (the coverage sidecar fold was
retired in Stage e), the PLANNED upsert (`plan_windows`), the throttled live
flush, and the begin/end dry-run rule.
"""

from __future__ import annotations

import errno
import fcntl
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from brutex.data.errors import LakeWriteHalt
from brutex.data.fetch_coverage import WindowKey
from brutex.data.partition_lock import partition_lock_path
from brutex.data.window_ledger import (
    EMPTY_TERMINAL_THRESHOLD,
    FAILED,
    PLANNED,
    SKIPPED,
    SUCCESS,
    WindowLedger,
)
from brutex.data.window_ledger_store import (
    DEFAULT_LEDGER_SAVE_SLOW_WARN_S,
    LEDGER_SAVE_SLOW_WARN_ENV,
    LedgerRow,
    begin_pull_ledger,
    end_pull_ledger,
    index_lake_spans,
    ledger_dir,
    ledger_path,
    load_or_migrate_window_ledger,
    load_window_ledger,
    make_live_ledger_flush,
    make_locked_flush,
    merge_ledger_rows,
    plan_windows,
    reconcile_ledger_windows,
    reconcile_ledger_with_lake,
    resolve_ledger_save_slow_warn_seconds,
    save_window_ledger,
    seed_enclosed_windows,
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


class TestSaveDiskFullHalts:
    """DEC-WINDOW-LEDGER-WRITE-HALT-001 — a disk-full / OOM during the ledger save is a
    pull-wide :class:`LakeWriteHalt`, not a raw error (the ledger is a LAKE write, so a
    full disk fails every remaining window identically and must halt the pull, never be
    mis-quarantined per-symbol). A non-ENOSPC OSError still propagates UNCHANGED (the
    guard never over-escalates). ``pyarrow.parquet.write_table`` is the boundary that
    raises in the real failure (the temp's first byte write on a full volume), so we
    patch it to drive the deterministic regression.
    """

    @staticmethod
    def _ledger() -> WindowLedger:
        ledger = WindowLedger()
        ledger.record(_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        return ledger

    def test_disk_full_maps_to_lake_write_halt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import pyarrow.parquet as pq

        def _boom(*_a: object, **_k: object) -> None:
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(pq, "write_table", _boom)
        with pytest.raises(LakeWriteHalt) as exc_info:
            save_window_ledger(self._ledger(), tmp_path)
        assert exc_info.value.reason == "disk_full"
        # The half-written temp is cleaned up before the halt — no .tmp leak.
        assert list((tmp_path / "window_ledger").glob("*.tmp")) == []

    def test_out_of_memory_maps_to_lake_write_halt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import pyarrow.parquet as pq

        def _boom(*_a: object, **_k: object) -> None:
            raise MemoryError("heap exhausted")

        monkeypatch.setattr(pq, "write_table", _boom)
        with pytest.raises(LakeWriteHalt) as exc_info:
            save_window_ledger(self._ledger(), tmp_path)
        assert exc_info.value.reason == "out_of_memory"

    def test_other_oserror_propagates_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-ENOSPC OSError (e.g. EACCES) is NOT escalated to a halt — propagates raw."""
        import pyarrow.parquet as pq

        def _boom(*_a: object, **_k: object) -> None:
            raise OSError(errno.EACCES, "Permission denied")

        monkeypatch.setattr(pq, "write_table", _boom)
        with pytest.raises(OSError) as exc_info:
            save_window_ledger(self._ledger(), tmp_path)
        assert not isinstance(exc_info.value, LakeWriteHalt)
        assert exc_info.value.errno == errno.EACCES


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
        # One row per _derive_empty_streak branch: SUCCESS→0, EMPTY→attempts, SKIPPED→threshold.
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
        # Both _derive_in_pull_fetches branches: PLANNED→0 (never fetched), SUCCESS→1.
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
    session_dates: frozenset | None = None,
) -> dict:
    span: dict[str, object] = {
        "exchange": exchange,
        "segment": segment,
        "symbol": symbol,
        "interval": interval,
        "bars": bars,
        "first_date": first,
        "last_date": last,
    }
    if session_dates is not None:
        # The DEC-COLDSTART-SEED-INTERIOR-GAP-001 density enrichment — attached by
        # `summarize_spot_datasets(session_date_segments=...)` ONLY on the dense
        # INDEX `1minute` spans; every legacy/F&O/CASH span omits the key.
        span["session_dates"] = session_dates
    return span


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
        seen_kwargs: dict = {}

        def _fake_spans(_root, **kwargs):
            seen_kwargs.update(kwargs)
            return [_span(interval="1day")]

        monkeypatch.setattr(
            "brutex.dashboard.coverage_reconcile.load_lake_spans",
            _fake_spans,
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
        # The SPOT wrapper requests the dense-INDEX session dates so the seed's
        # interior-gap density cross-check has its input
        # (DEC-COLDSTART-SEED-INTERIOR-GAP-001).
        assert seen_kwargs == {"session_date_segments": frozenset({"INDEX"})}


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


# --- DEC-WINDOW-LEDGER-RESCUE-001: fetch-exclusive enclosure + live-edge guard --------


def _seed_one(
    *,
    last: str,
    end: date,
    first: str = "2020-01-01",
    now: datetime = _NOW,
    today: date | None = None,
) -> int:
    """Run the COMMON seed core over ONE candidate window against one 1day span."""
    ledger = WindowLedger()
    span_index = index_lake_spans([_span(interval="1day", first=first, last=last)])
    key = WindowKey(market="spot", security_id="NIFTY", interval="1day", start=_START, end=end)
    return seed_enclosed_windows(
        ledger,
        span_index,
        [(key, ("NSE", "CASH", "NIFTY", "1day"))],
        now=now,
        today_ist=today,
    )


class TestSeedEnclosureBoundary:
    """DEC-WINDOW-LEDGER-RESCUE-001 — the off-by-one fix + the live-edge guard.

    A window key's ``end`` is fetch-EXCLUSIVE (the last requested bar is on
    ``end − 1 day``) while a lake span's ``last_date`` is bar-INCLUSIVE, so the
    enclosure test is ``(end − 1) <= last_date``. The prior ``end <= last_date``
    form made the default-window F&O rescue DEAD CODE (``expiry + 1 <= expiry``
    is always false). Boundary contract pinned exactly as specified:
    ``end − 1 == last_date`` seeds; ``end − 1 == last_date + 1`` does not.
    """

    def test_end_minus_one_on_last_date_seeds(self) -> None:
        # FAILS-BEFORE: the old rule required end <= last_date (2021-08-01 <= 2021-07-31).
        assert _seed_one(last="2021-07-31", end=date(2021, 8, 1), today=date(2026, 6, 8)) == 1

    def test_end_minus_one_past_last_date_does_not_seed(self) -> None:
        # end − 1 == last_date + 1 → the last covered day is NOT stored → never seeded.
        assert _seed_one(last="2021-07-30", end=date(2021, 8, 1), today=date(2026, 6, 8)) == 0

    def test_live_edge_window_including_today_never_seeds(self) -> None:
        # FAILS-BEFORE (over-skip): a window whose coverage INCLUDES the current IST
        # day must NEVER seed — a mid-session partial store puts a bar on `today`
        # (span last_date == today) without the session being complete, and the old
        # rule would have seeded it settled (end <= last_date), masking the rest of
        # the day forever.  end − 1 == today → guarded.
        assert _seed_one(last="2021-08-02", end=date(2021, 8, 2), today=date(2021, 8, 1)) == 0

    def test_live_edge_today_derived_from_now_ist(self) -> None:
        # today_ist=None derives the IST calendar day from `now`: at 10:00 UTC the
        # IST date equals the UTC date, so a window whose last covered day IS that
        # date is guarded; one day later the same window seeds.
        end = date(2021, 8, 1)  # last covered day = 2021-07-31
        on_the_edge = datetime(2021, 7, 31, 10, 0, tzinfo=UTC)
        past_the_edge = datetime(2021, 8, 1, 10, 0, tzinfo=UTC)
        assert _seed_one(last="2021-07-31", end=end, now=on_the_edge) == 0
        assert _seed_one(last="2021-07-31", end=end, now=past_the_edge) == 1

    def test_empty_below_threshold_still_refetches_and_settles_at_three(
        self, tmp_path: Path
    ) -> None:
        # PIN (no over-skip): the EMPTY-below-threshold behaviour is UNCHANGED by the
        # enclosure fix + the merge-on-save — a genuine EMPTY window still re-fetches
        # on every pull and settles SKIPPED only at the 3rd clean empty
        # (EMPTY_TERMINAL_THRESHOLD), with the save→load round-trip (now a
        # read-MERGE-write) preserving the streak exactly.
        key = _key()
        first = WindowLedger()
        first.record(key, raw_bars=0, error_type=None, bars_appended=0, now=_NOW)  # EMPTY #1
        save_window_ledger(first, tmp_path)
        second = load_window_ledger(tmp_path)
        assert second.is_skippable(key) is False  # still re-fetched
        second.record(key, raw_bars=0, error_type=None, bars_appended=0, now=_NOW)  # EMPTY #2
        save_window_ledger(second, tmp_path)
        third = load_window_ledger(tmp_path)
        assert third.is_skippable(key) is False  # still re-fetched
        assert third.records()[0].empty_streak == 2
        # The 3rd genuine empty settles SKIPPED — exactly EMPTY_TERMINAL_THRESHOLD.
        assert EMPTY_TERMINAL_THRESHOLD == 3
        assert third.record(key, raw_bars=0, error_type=None, bars_appended=0, now=_NOW) == SKIPPED


# --- DEC-COLDSTART-SEED-INTERIOR-GAP-001: the seed's density cross-check --------------


def _interior_trading_bounds(first_covered: date, last_covered: date) -> tuple[date, date]:
    """First + last NSE trading session STRICTLY between two covered dates (real calendar)."""
    from brutex.calendars.nse import is_trading_day

    sessions = [
        first_covered + timedelta(days=i)
        for i in range(1, (last_covered - first_covered).days)
        if is_trading_day(first_covered + timedelta(days=i))
    ]
    return sessions[0], sessions[-1]


def _march_trading_days() -> frozenset[date]:
    """Every NSE trading day in [2024-03-01, 2024-03-28] — a CONTINUOUS covered set."""
    from brutex.calendars.nse import is_trading_day

    out: set[date] = set()
    cursor = date(2024, 3, 1)
    while cursor <= date(2024, 3, 28):
        if is_trading_day(cursor):
            out.add(cursor)
        cursor += timedelta(days=1)
    return frozenset(out)


def _seed_density(
    *,
    session_dates: frozenset | None,
    interval: str = "1minute",
) -> tuple[int, WindowLedger, list]:
    """Run the COMMON seed core over ONE enclosed 2024-03 window against one INDEX span."""
    ledger = WindowLedger()
    span_index = index_lake_spans(
        [
            _span(
                interval=interval,
                segment="INDEX",
                first="2024-03-01",
                last="2024-03-28",
                session_dates=session_dates,
            )
        ]
    )
    key = WindowKey(
        market="spot",
        security_id="NIFTY",
        interval=interval,
        start=date(2024, 3, 1),
        end=date(2024, 3, 29),  # fetch-exclusive → last covered day 2024-03-28
    )
    with capture_logs() as logs:
        seeded = seed_enclosed_windows(
            ledger,
            span_index,
            [(key, ("NSE", "INDEX", "NIFTY", interval))],
            now=_NOW,
            today_ist=date(2026, 6, 8),
        )
    return seeded, ledger, logs


class TestSeedInteriorGapDensityGate:
    """DEC-COLDSTART-SEED-INTERIOR-GAP-001 — cold-start span-seeding must not stamp
    ``SUCCESS`` over a fetched ``1minute`` INDEX window whose lake span contains an
    interior multi-session hole (the density cross-check REUSES
    ``classify_interior_gap_dates`` — CLAUDE.md §11 COMMON)."""

    def test_gappy_1minute_window_is_not_span_seeded_regression(self) -> None:
        # FAILS-BEFORE / PASSES-AFTER: bars at both ends of the window with a
        # ~17-trading-session interior hole between — the span test alone seeded
        # SUCCESS (the window became skippable and the hole was masked FOREVER on a
        # lost-ledger cold-start); the density gate now leaves it ABSENT so the next
        # pull PLANs + fetches it (self-heal via the fetch-time detector).
        gappy = frozenset({date(2024, 3, 1), date(2024, 3, 28)})
        seeded, ledger, logs = _seed_density(session_dates=gappy)
        assert seeded == 0
        assert ledger.count == 0  # no row minted — stays PLANNED-able, never masked
        skips = [e for e in logs if e["event"] == "window_ledger.seed_interior_gap_skip"]
        assert len(skips) == 1
        assert skips[0]["log_level"] == "warning"
        assert skips[0]["window_start"] == "2024-03-01"
        assert skips[0]["window_end"] == "2024-03-29"
        # DEC-DATA-HONESTY-ALLFILTERED-SEED-001: the WARNING names the confirmed
        # run's INCLUSIVE bar-less bounds — the first/last trading session
        # strictly between the two covered endpoints (computed from the real NSE
        # calendar so the pin never hardcodes an unverifiable date).
        gap_first, gap_last = _interior_trading_bounds(date(2024, 3, 1), date(2024, 3, 28))
        assert skips[0]["gap_first"] == gap_first.isoformat()
        assert skips[0]["gap_last"] == gap_last.isoformat()

    def test_continuous_1minute_window_still_seeds(self) -> None:
        # No false-skip: a window covered on EVERY trading session seeds exactly as
        # before (the density gate abstains on a continuous set).
        seeded, ledger, logs = _seed_density(session_dates=_march_trading_days())
        assert seeded == 1
        assert ledger.records()[0].status == SUCCESS
        assert [e for e in logs if "seed_interior_gap" in str(e.get("event"))] == []

    def test_span_without_session_dates_seeds_byte_identical(self) -> None:
        # The legacy / F&O / CASH span shape (no ``session_dates`` key) is untouched:
        # the SAME gappy window seeds exactly as today — pins that only the opted-in
        # INDEX `1minute` read changes behaviour.
        seeded, ledger, _logs = _seed_density(session_dates=None)
        assert seeded == 1
        assert ledger.records()[0].status == SUCCESS

    def test_too_few_in_range_sessions_abstains_and_seeds(self) -> None:
        # A single in-window covered session has no interior to bracket — the COMMON
        # classifier honestly ABSTAINS (too_few_sessions) and the seed proceeds.
        seeded, ledger, logs = _seed_density(session_dates=frozenset({date(2024, 3, 15)}))
        assert seeded == 1
        assert ledger.records()[0].status == SUCCESS
        assert [e for e in logs if "seed_interior_gap" in str(e.get("event"))] == []

    def test_malformed_session_dates_fail_closed_not_seeded(self) -> None:
        # FAIL-CLOSED: a malformed enrichment (strings, not dates) raises inside the
        # check → ONE seed_interior_gap_error WARNING and the seed is SKIPPED (a
        # re-pull is always data-safe; a bad signal must never mint SUCCESS).
        malformed = frozenset({"2024-03-01", "2024-03-28"})
        seeded, ledger, logs = _seed_density(session_dates=malformed)
        assert seeded == 0
        assert ledger.count == 0
        errors = [e for e in logs if e["event"] == "window_ledger.seed_interior_gap_error"]
        assert len(errors) == 1
        assert errors[0]["log_level"] == "warning"
        assert errors[0]["window_start"] == "2024-03-01"

    def test_gappy_1day_window_still_seeds_revision1(self) -> None:
        # REVISION-1 pin: a DERIVED `1day` window is NEVER density-checked even when
        # its span (wrongly) carries a gappy ``session_dates`` — un-seeding it could
        # never self-heal (1day is derived, never fetched —
        # DEC-1MINUTE-ONLY-DERIVE-1DAY-001), so only the `1minute` twin is un-seeded.
        gappy = frozenset({date(2024, 3, 1), date(2024, 3, 28)})
        seeded, ledger, logs = _seed_density(session_dates=gappy, interval="1day")
        assert seeded == 1
        assert ledger.records()[0].status == SUCCESS
        assert [e for e in logs if "seed_interior_gap" in str(e.get("event"))] == []


def _march_2024_sessions() -> list[date]:
    """Every NSE trading day in [2024-03-01, 2024-03-28], ascending (real calendar)."""
    return sorted(_march_trading_days())


def _key_march(start: date, end: date) -> WindowKey:
    return WindowKey(market="spot", security_id="NIFTY", interval="1minute", start=start, end=end)


_MARCH_SPAN_KEY = ("NSE", "INDEX", "NIFTY", "1minute")


def _seed_march(
    session_dates: frozenset | None,
    candidates: list[tuple[WindowKey, tuple[str, str, str, str]]],
    *,
    ledger: WindowLedger | None = None,
) -> tuple[int, WindowLedger, list]:
    """Run the COMMON seed core over ONE March-2024 INDEX 1-minute span."""
    led = ledger if ledger is not None else WindowLedger()
    span_index = index_lake_spans(
        [
            _span(
                interval="1minute",
                segment="INDEX",
                first="2024-03-01",
                last="2024-03-28",
                session_dates=session_dates,
            )
        ]
    )
    with capture_logs() as logs:
        seeded = seed_enclosed_windows(
            led, span_index, candidates, now=_NOW, today_ist=date(2026, 6, 8)
        )
    return seeded, led, logs


class TestSeedContinuityGateDatasetRuns:
    """DEC-DATA-HONESTY-ALLFILTERED-SEED-001 — the continuity gate is DATASET-wide:
    the confirmed >tolerance bar-less runs are computed once per span and a window
    INTERSECTING a run is refused, closing the two per-window blind spots the
    original DEC-COLDSTART-SEED-INTERIOR-GAP-001 check documented as non-goals
    (a hole straddling a window boundary; a window entirely inside a hole)."""

    def test_hole_straddling_window_boundary_refuses_both_windows(self) -> None:
        # FAILS-BEFORE / PASSES-AFTER (blind spot 1): an ~11-session hole crossing
        # the boundary between two windows reads, PER WINDOW, as a trailing edge
        # (window A) and a leading edge (window B) — the old per-window classifier
        # seeded BOTH; the dataset-run intersection refuses BOTH.
        t = _march_2024_sessions()
        hole = t[4:15]  # 11 bar-less sessions > tolerance 5
        covered = frozenset(t[:4]) | frozenset(t[15:])
        mid = hole[5]  # a boundary INSIDE the hole
        win_a = _key_march(date(2024, 3, 1), mid)
        win_b = _key_march(mid, date(2024, 3, 29))
        seeded, led, logs = _seed_march(
            covered, [(win_a, _MARCH_SPAN_KEY), (win_b, _MARCH_SPAN_KEY)]
        )
        assert seeded == 0
        assert led.count == 0  # both stay PLANNED-able — the hole is never masked
        skips = [e for e in logs if e["event"] == "window_ledger.seed_interior_gap_skip"]
        assert len(skips) == 2
        assert {s["window_start"] for s in skips} == {"2024-03-01", mid.isoformat()}
        for s in skips:
            assert s["gap_first"] == hole[0].isoformat()
            assert s["gap_last"] == hole[-1].isoformat()

    def test_window_entirely_inside_hole_is_refused(self) -> None:
        # FAILS-BEFORE / PASSES-AFTER (blind spot 2): a window with ZERO covered
        # dates inside a giant hole — the old per-window classifier could only
        # abstain (too_few_sessions) and seeded it; the run intersection refuses.
        t = _march_2024_sessions()
        hole = t[4:15]
        covered = frozenset(t[:4]) | frozenset(t[15:])
        inside = _key_march(hole[2], hole[7] + timedelta(days=1))
        seeded, led, logs = _seed_march(covered, [(inside, _MARCH_SPAN_KEY)])
        assert seeded == 0
        assert led.count == 0
        skips = [e for e in logs if e["event"] == "window_ledger.seed_interior_gap_skip"]
        assert len(skips) == 1

    def test_window_ending_exactly_on_run_first_is_refused(self) -> None:
        # BOUNDARY GEOMETRY (mutation teeth for the intersection's LEFT `<=`):
        # a window whose FINAL calendar day IS the run's first bar-less session
        # (last_covered == run_first — a single-day touch) must be refused.
        # Mutating `r[0] <= last_covered` to `<` seeds this window SUCCESS —
        # permanently skippable over a confirmed >tolerance hole, the exact
        # blind-spot class the dataset-wide gate closes.
        t = _march_2024_sessions()
        hole = t[4:15]  # 11 bar-less sessions > tolerance 5
        covered = frozenset(t[:4]) | frozenset(t[15:])
        win = _key_march(date(2024, 3, 1), hole[0] + timedelta(days=1))
        seeded, led, logs = _seed_march(covered, [(win, _MARCH_SPAN_KEY)])
        assert seeded == 0
        assert led.count == 0  # stays PLANNED-able — the touch is never masked
        skips = [e for e in logs if e["event"] == "window_ledger.seed_interior_gap_skip"]
        assert len(skips) == 1
        assert skips[0]["gap_first"] == hole[0].isoformat()

    def test_window_starting_exactly_on_run_last_is_refused(self) -> None:
        # BOUNDARY GEOMETRY (mutation teeth for the intersection's RIGHT `<=`):
        # a window whose START is the run's LAST bar-less session (start ==
        # run_last) must be refused. Mutating `window_key.start <= r[1]` to `<`
        # seeds it SUCCESS with no test failing before this pin.
        t = _march_2024_sessions()
        hole = t[4:15]
        covered = frozenset(t[:4]) | frozenset(t[15:])
        win = _key_march(hole[-1], date(2024, 3, 29))
        seeded, led, logs = _seed_march(covered, [(win, _MARCH_SPAN_KEY)])
        assert seeded == 0
        assert led.count == 0
        skips = [e for e in logs if e["event"] == "window_ledger.seed_interior_gap_skip"]
        assert len(skips) == 1
        assert skips[0]["gap_last"] == hole[-1].isoformat()

    def test_window_not_intersecting_the_hole_still_seeds(self) -> None:
        # No over-refusal: a window covering only the DENSE region of a holed
        # dataset seeds exactly as before (the run does not intersect it).
        t = _march_2024_sessions()
        covered = frozenset(t[:4]) | frozenset(t[15:])
        dense = _key_march(t[0], t[3] + timedelta(days=1))
        seeded, led, logs = _seed_march(covered, [(dense, _MARCH_SPAN_KEY)])
        assert seeded == 1
        assert led.records()[0].status == SUCCESS
        assert [e for e in logs if "seed_interior_gap" in str(e.get("event"))] == []

    def test_sub_tolerance_hole_still_seeds(self) -> None:
        # Envelope pin: a ≤5-session hole is NOT a confirmed run — seeded (ONE
        # tolerance knob shared with the fetch-time detector).
        t = _march_2024_sessions()
        covered = frozenset(t) - frozenset(t[6:10])  # a 4-session hole
        win = _key_march(date(2024, 3, 1), date(2024, 3, 29))
        seeded, _led, logs = _seed_march(covered, [(win, _MARCH_SPAN_KEY)])
        assert seeded == 1
        assert [e for e in logs if "seed_interior_gap" in str(e.get("event"))] == []

    def test_existing_row_skips_gate_and_never_rewarns(self) -> None:
        # Refuter F-4 fold: once a refused window HAS a row (fetched → FAILED /
        # settled / anything), the steady-state reconcile must not re-WARN about
        # it on every pull — the existence probe short-circuits the gate, and the
        # existing row is bit-identical after the call.
        t = _march_2024_sessions()
        covered = frozenset(t[:4]) | frozenset(t[15:])
        win = _key_march(date(2024, 3, 1), date(2024, 3, 29))
        led = WindowLedger()
        led.record(
            win, raw_bars=0, error_type="GrowwTimeoutError", bars_appended=0, now=_NOW
        )  # a FAILED row from a prior fetch
        before = led.records()
        seeded, led, logs = _seed_march(covered, [(win, _MARCH_SPAN_KEY)], ledger=led)
        assert seeded == 0
        assert led.records() == before  # bit-identical — a probe/refusal never writes
        assert [e for e in logs if "seed_interior_gap" in str(e.get("event"))] == []

    def test_error_memo_still_warns_per_window_and_refuses(self) -> None:
        # FAIL-CLOSED with the per-span memo: a malformed enrichment refuses EVERY
        # window of that span with one error WARNING each (never a crash, never a
        # minted SUCCESS) — the memo caches the failure, not a verdict.
        malformed = frozenset({"2024-03-01", "2024-03-28"})
        t = _march_2024_sessions()
        mid = t[9]
        win_a = _key_march(date(2024, 3, 1), mid)
        win_b = _key_march(mid, date(2024, 3, 29))
        seeded, led, logs = _seed_march(
            malformed, [(win_a, _MARCH_SPAN_KEY), (win_b, _MARCH_SPAN_KEY)]
        )
        assert seeded == 0
        assert led.count == 0
        errors = [e for e in logs if e["event"] == "window_ledger.seed_interior_gap_error"]
        assert len(errors) == 2

    def test_out_of_coverage_dataset_abstains_and_seeds(self) -> None:
        # Honest abstain (Golden Rule #1): a dataset whose covered ends run past
        # the NSE holiday-list coverage returns None from the runs API → the gate
        # is inert and the span seed proceeds (consistent with the fetch-time
        # detector's abstain).
        covered = frozenset({date(2024, 3, 1), date(2028, 1, 10)})
        win = _key_march(date(2024, 3, 1), date(2024, 3, 29))
        seeded, led, logs = _seed_march(covered, [(win, _MARCH_SPAN_KEY)])
        assert seeded == 1
        assert led.records()[0].status == SUCCESS
        assert [e for e in logs if "seed_interior_gap" in str(e.get("event"))] == []


# --- DEC-WINDOW-LEDGER-RESCUE-001: merge_ledger_rows (the pure conflict core) ---------


def _row(
    security: str = "NIFTY",
    *,
    status: str = SUCCESS,
    attempts: int = 1,
    last_attempt: str | None = "2026-06-08T12:00:00+00:00",
    bars: int = 5,
    bars_net_new: int | None = None,
    last_error: str | None = None,
) -> LedgerRow:
    """One serialised ledger row (the exact 14-field ``to_rows`` shape)."""
    return (
        "dhan",
        security,
        "1minute",
        "2021-01-01",
        "2021-04-01",
        status,
        bars,
        attempts,
        last_error,
        "2026-06-01T00:00:00+00:00",
        last_attempt,
        0,
        1,
        bars_net_new,
    )


class TestMergeLedgerRows:
    """The per-key merge rule: settledness > newer timestamp > attempts > deterministic."""

    def test_disjoint_rows_union_no_loss(self) -> None:
        a, b = _row("NIFTY"), _row("SENSEX")
        merged = merge_ledger_rows([a], [b])
        assert merged == sorted([a, b], key=lambda r: r[:5])
        # Order-independent: swapping memory/disk yields the SAME union.
        assert merge_ledger_rows([b], [a]) == merged

    def test_more_settled_status_wins_both_directions(self) -> None:
        settled = _row(status=SUCCESS, attempts=1)
        planned = _row(status=PLANNED, attempts=0, last_attempt=None)
        assert merge_ledger_rows([planned], [settled]) == [settled]
        assert merge_ledger_rows([settled], [planned]) == [settled]

    def test_equal_rank_newer_timestamp_wins(self) -> None:
        older = _row(last_attempt="2026-06-08T12:00:00+00:00", bars=1)
        newer = _row(last_attempt="2026-06-08T13:00:00+00:00", bars=2)
        assert merge_ledger_rows([older], [newer]) == [newer]
        assert merge_ledger_rows([newer], [older]) == [newer]

    def test_equal_rank_and_timestamp_higher_attempts_wins(self) -> None:
        fewer = _row(attempts=1, bars=1)
        more = _row(attempts=4, bars=2)
        assert merge_ledger_rows([fewer], [more]) == [more]
        assert merge_ledger_rows([more], [fewer]) == [more]

    def test_full_tie_is_deterministic_regardless_of_side(self) -> None:
        # Same rank / stamp / attempts, different payload → the lexicographically
        # greater full row wins on EITHER side (writer-order independence).
        low = _row(bars=5)
        high = _row(bars=7)
        assert merge_ledger_rows([low], [high]) == merge_ledger_rows([high], [low])

    def test_identical_rows_collapse_to_one(self) -> None:
        row = _row()
        assert merge_ledger_rows([row], [row]) == [row]

    def test_pruned_disk_only_row_is_dropped(self) -> None:
        kept, pruned = _row("NIFTY"), _row("SENSEX")
        pruned_key = (pruned[0], pruned[1], pruned[2], pruned[3], pruned[4])
        merged = merge_ledger_rows([kept], [pruned], pruned_keys=frozenset({pruned_key}))
        assert merged == [kept]

    def test_pruned_key_recreated_in_memory_is_kept(self) -> None:
        # A tombstone applies ONLY to disk-only rows — a key re-created in memory
        # (a shrunk-range replan) always survives its own tombstone.
        row = _row("NIFTY")
        key = (row[0], row[1], row[2], row[3], row[4])
        assert merge_ledger_rows([row], [row], pruned_keys=frozenset({key})) == [row]

    def test_unknown_status_never_displaces_a_real_row(self) -> None:
        weird = _row(status="WEIRD", attempts=99)
        planned = _row(status=PLANNED, attempts=0, last_attempt=None)
        assert merge_ledger_rows([weird], [planned]) == [planned]

    def test_reopened_key_memory_demotion_wins_over_settled_disk(self) -> None:
        # DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001 × the merge-on-save: a deliberate
        # reopen_skipped demotion (SKIPPED → FAILED in memory) must NOT be undone
        # by the on-disk SKIPPED copy's more-settled rank — for a reopened key the
        # in-memory row wins outright.
        demoted = _row(status=FAILED, attempts=2)
        settled = _row(status=SKIPPED, attempts=2)
        key = (demoted[0], demoted[1], demoted[2], demoted[3], demoted[4])
        # WITHOUT the tombstone the settled disk copy wins (the resurrect bug):
        assert merge_ledger_rows([demoted], [settled]) == [settled]
        # WITH the tombstone the deliberate demotion survives the merge:
        assert merge_ledger_rows([demoted], [settled], reopened_keys=frozenset({key})) == [demoted]

    def test_merge_keeps_degraded_over_disk_success(self) -> None:
        # BUG-REGRESSION (DEC-WINDOW-LEDGER-DEGRADED-TOMBSTONE-001, FAIL-BEFORE/
        # PASS-AFTER): a `record(degraded=True)` interior-gap demotion (SUCCESS →
        # FAILED in memory) must NOT be undone by a CONCURRENT pull's on-disk
        # SUCCESS copy's more-settled rank — for a degraded key the in-memory row
        # wins outright, exactly as a reopened key does.
        demoted = _row(status=FAILED, attempts=2, last_error="INTERIOR_GAP")
        settled = _row(status=SUCCESS, attempts=2)
        key = (demoted[0], demoted[1], demoted[2], demoted[3], demoted[4])
        # TEETH — WITHOUT the tombstone the disk SUCCESS wins (the silent-override bug):
        assert merge_ledger_rows([demoted], [settled]) == [settled]
        # WITH the tombstone the demotion survives the merge (reopened stays empty —
        # this pins the `or`'s second operand: reopened False → degraded True):
        assert merge_ledger_rows(
            [demoted],
            [settled],
            reopened_keys=frozenset(),
            degraded_keys=frozenset({key}),
        ) == [demoted]

    def test_merge_reopened_still_wins(self) -> None:
        # The `or`'s FIRST operand short-circuits: a reopened key keeps the
        # in-memory row even with an EMPTY degraded set alongside it.
        demoted = _row(status=FAILED, attempts=2)
        settled = _row(status=SKIPPED, attempts=2)
        key = (demoted[0], demoted[1], demoted[2], demoted[3], demoted[4])
        assert merge_ledger_rows(
            [demoted],
            [settled],
            reopened_keys=frozenset({key}),
            degraded_keys=frozenset(),
        ) == [demoted]

    def test_merge_no_tombstone_uses_preference(self) -> None:
        # BOTH tombstone sets explicitly empty → the ordinary more-settled
        # preference resolves the conflict (the disk SUCCESS wins).
        demoted = _row(status=FAILED, attempts=2, last_error="INTERIOR_GAP")
        settled = _row(status=SUCCESS, attempts=2)
        assert merge_ledger_rows(
            [demoted],
            [settled],
            reopened_keys=frozenset(),
            degraded_keys=frozenset(),
        ) == [settled]

    def test_merge_default_degraded_keys_omitted(self) -> None:
        # The new kwarg defaults to an empty frozenset — a call that omits it is
        # byte-identical to the pre-fix merge (back-compat pin).
        demoted = _row(status=FAILED, attempts=2, last_error="INTERIOR_GAP")
        settled = _row(status=SUCCESS, attempts=2)
        assert merge_ledger_rows([demoted], [settled]) == [settled]
        assert merge_ledger_rows([settled], [demoted]) == [settled]


# --- DEC-WINDOW-LEDGER-RESCUE-001: cross-process-safe save (flock + read-merge-write) --


class TestSaveMergeCrossProcess:
    """Two concurrent savers on one lake never clobber each other's rows."""

    def test_two_writer_sequence_no_row_loss(self, tmp_path: Path) -> None:
        # FAILS-BEFORE: the simulated second-process sequence — A and B both load the
        # (empty) ledger, A records+saves NIFTY, B records+saves SENSEX; the old
        # whole-file replace made B's save CLOBBER A's row (last-writer-wins).
        a = load_window_ledger(tmp_path)
        b = load_window_ledger(tmp_path)
        a.record(_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        save_window_ledger(a, tmp_path)
        b.record(_key("SENSEX"), raw_bars=3, error_type=None, bars_appended=3, now=_NOW)
        save_window_ledger(b, tmp_path)
        final = load_window_ledger(tmp_path)
        assert final.is_skippable(_key("NIFTY")) is True  # A's row SURVIVES B's save
        assert final.is_skippable(_key("SENSEX")) is True
        assert final.count == 2

    def test_threaded_savers_no_row_loss(self, tmp_path: Path) -> None:
        # 8 interleaved savers, each with a distinct row: the flock serialises the
        # read-merge-write so ALL 8 rows land (no lost update, any interleaving).
        n = 8
        barrier = threading.Barrier(n)
        errors: list[Exception] = []

        def _save_one(i: int) -> None:
            ledger = WindowLedger()
            ledger.record(_key(f"S{i}"), raw_bars=1, error_type=None, bars_appended=1, now=_NOW)
            barrier.wait(timeout=10.0)
            try:
                save_window_ledger(ledger, tmp_path)
            except Exception as exc:
                # Any save failure (LakeWriteHalt / OSError / pyarrow) is an Exception
                # subclass — captured here and surfaced to the main thread's
                # ``errors == []`` assert (never BaseException; CodeQL py/catch-base-exception).
                errors.append(exc)

        threads = [threading.Thread(target=_save_one, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)
        assert errors == []
        final = load_window_ledger(tmp_path)
        assert final.count == n
        assert all(final.is_skippable(_key(f"S{i}")) for i in range(n))

    def test_same_key_conflict_prefers_more_settled(self, tmp_path: Path) -> None:
        # FAILS-BEFORE: A settles a window SUCCESS and saves; B (loaded before A's
        # save) only PLANNED the same key and saves LAST — the old replace demoted
        # the settled row back to PLANNED (a lost settle → a needless re-fetch).
        a = load_window_ledger(tmp_path)
        b = load_window_ledger(tmp_path)
        a.record(_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        save_window_ledger(a, tmp_path)
        b.plan(_key("NIFTY"), now=_NOW)
        save_window_ledger(b, tmp_path)
        final = load_window_ledger(tmp_path)
        assert final.count == 1
        assert final.records()[0].status == SUCCESS  # the settled row wins the merge

    def test_reopened_demotion_survives_the_merge_on_save(self, tmp_path: Path) -> None:
        # DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001 × DEC-WINDOW-LEDGER-RESCUE-001: the
        # `brutex reopen-skipped` load→demote→save round-trip. FAILS-BEFORE the
        # reopened-keys tombstone: the on-disk SKIPPED copy out-ranked the in-memory
        # FAILED demotion in the merge and the re-open was silently undone.
        seeded = WindowLedger(_threshold=1)
        assert seeded.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW) == (
            SKIPPED
        )
        save_window_ledger(seeded, tmp_path)
        reopener = load_window_ledger(tmp_path)
        assert reopener.reopen_skipped() == 1
        save_window_ledger(reopener, tmp_path)
        restored = load_window_ledger(tmp_path)
        assert restored.count == 1
        assert restored.records()[0].status == FAILED  # the demotion persisted
        assert restored.is_skippable(_key()) is False  # the next pull re-fetches it

    def test_save_preserves_degraded_demotion(self, tmp_path: Path) -> None:
        # BUG-REGRESSION (DEC-WINDOW-LEDGER-DEGRADED-TOMBSTONE-001, FAIL-BEFORE/
        # PASS-AFTER): a CONCURRENT pull has already written a clean SUCCESS for W
        # to disk; THIS pull's gap detector demotes W (record(degraded=True) →
        # FAILED "INTERIOR_GAP") and saves. Before the fix the merge-on-save let
        # the disk SUCCESS (more-settled rank) silently override the demotion, so
        # W read skippable forever WITH the confirmed gap; with the degraded-keys
        # tombstone the demotion survives its own save durably.
        concurrent = WindowLedger()
        concurrent.record(_key(), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        save_window_ledger(concurrent, tmp_path)  # the disk SUCCESS copy
        demoter = load_window_ledger(tmp_path)
        status = demoter.record(
            _key(),
            raw_bars=5,
            error_type=None,
            bars_appended=5,
            now=_NOW,
            degraded=True,
            degraded_reason="INTERIOR_GAP",
        )
        assert status == FAILED
        save_window_ledger(demoter, tmp_path)  # exercises the degraded_keys= wire line
        restored = load_window_ledger(tmp_path)
        assert restored.count == 1
        rec = restored.records()[0]
        assert rec.status == FAILED  # the demotion persisted over the disk SUCCESS
        assert rec.last_error == "INTERIOR_GAP"
        assert restored.is_skippable(_key()) is False  # the next pull re-fetches it

    def test_save_lock_timeout_skips_and_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A peer wedged past the bounded deadline: the save is SKIPPED with a loud
        # WARNING (never a crash mid-flush); after the peer releases, a retry lands.
        monkeypatch.setenv("BRUTEX_PARTITION_LOCK_TIMEOUT_S", "0.05")
        path = ledger_path(tmp_path)
        lock_path = partition_lock_path(tmp_path, path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        ledger = WindowLedger()
        ledger.record(_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        try:
            with capture_logs() as logs:
                save_window_ledger(ledger, tmp_path)  # returns — never raises
            assert any(log["event"] == "window_ledger.save_lock_timeout" for log in logs)
            assert not path.exists()  # this save was skipped (in-memory state intact)
        finally:
            os.close(fd)  # releases the flock
        save_window_ledger(ledger, tmp_path)  # the retry lands once the peer is gone
        assert load_window_ledger(tmp_path).count == 1

    def test_save_merges_over_corrupt_disk_file(self, tmp_path: Path) -> None:
        # A corrupt on-disk ledger merges as EMPTY (never-raise) — the save self-heals
        # the file with the in-memory rows.
        path = ledger_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"corrupt")
        ledger = WindowLedger()
        ledger.record(_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        save_window_ledger(ledger, tmp_path)
        restored = load_window_ledger(tmp_path)
        assert restored.count == 1 and restored.load_failed is False

    def test_pruned_rows_never_resurrect_across_flushes(self, tmp_path: Path) -> None:
        # DEC-WINDOW-TRAILING-GC-001 preserved: a superseded trailing row pruned at
        # plan time must NOT resurrect from the disk copy on the next merge-save.
        short_key = WindowKey(
            market="spot",
            security_id="NIFTY",
            interval="1minute",
            start=date(2026, 1, 1),
            end=date(2026, 1, 10),
        )
        first = WindowLedger()
        first.record(short_key, raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        save_window_ledger(first, tmp_path)  # the soon-superseded row is ON DISK
        loaded = load_window_ledger(tmp_path)
        plan_windows(
            loaded,
            instruments=[_FakeInstrument("NIFTY")],
            intervals=["1minute"],
            start=date(2026, 1, 1),
            end=date(2026, 1, 20),
            now=_NOW,
            now_ist=datetime(2026, 6, 8, 17, 0, tzinfo=ZoneInfo("Asia/Kolkata")),
        )
        assert loaded.pruned_keys()  # the shorter-end row was pruned by the replan
        save_window_ledger(loaded, tmp_path)
        ends = {r.window_end for r in load_window_ledger(tmp_path).records()}
        assert "2026-01-10" not in ends  # the orphan stayed dead — not resurrected


# --- DEC-WINDOW-LEDGER-RESCUE-001: LOUD load failure ----------------------------------


class TestLoadFailedFlag:
    """A ledger that EXISTED but failed to reload is announced, never silent."""

    def test_corrupt_file_sets_load_failed(self, tmp_path: Path) -> None:
        path = ledger_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not a parquet file at all")
        assert load_window_ledger(tmp_path).load_failed is True

    def test_wrong_shape_file_sets_load_failed(self, tmp_path: Path) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        path = ledger_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table({"unexpected": [1, 2, 3]}), path)
        assert load_window_ledger(tmp_path).load_failed is True

    def test_missing_file_is_a_fresh_lake_not_a_failure(self, tmp_path: Path) -> None:
        # An absent file is a genuinely fresh lake — honest to keep silent.
        assert load_window_ledger(tmp_path).load_failed is False

    def test_clean_load_is_not_flagged(self, tmp_path: Path) -> None:
        ledger = WindowLedger()
        ledger.record(_key(), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        save_window_ledger(ledger, tmp_path)
        assert load_window_ledger(tmp_path).load_failed is False

    def test_migrate_path_is_not_flagged(self, tmp_path: Path) -> None:
        # No parquet at all → the one-shot sidecar migration, not a reload failure.
        assert load_or_migrate_window_ledger(tmp_path).load_failed is False


def _two_step_clock(start: float, end: float) -> Callable[[], float]:
    """A deterministic monotonic source: first call → *start*, second → *end*.

    ``save_window_ledger`` reads its clock exactly twice (body start / body end), so a
    two-value script pins the measured duration bit-exactly.
    """
    values = iter((start, end))
    return lambda: next(values)


class TestSaveTelemetry:
    """DEC-TELEMETRY-RECONCILE-LEDGERSAVE-001 — save-duration telemetry on the ONE common
    save chokepoint (``save_window_ledger``). Observe-only: logging is the only delta; the
    stored rows, control flow and error contracts are pinned byte-identical."""

    def test_save_completed_emitted_with_rows_and_duration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(LEDGER_SAVE_SLOW_WARN_ENV, raising=False)
        ledger = WindowLedger()
        ledger.record(_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        ledger.record(_key("BANKNIFTY"), raw_bars=3, error_type=None, bars_appended=3, now=_NOW)
        with capture_logs() as logs:
            save_window_ledger(ledger, tmp_path, clock=_two_step_clock(100.0, 100.5))
        completed = [e for e in logs if e.get("event") == "window_ledger.save_completed"]
        assert len(completed) == 1
        entry = completed[0]
        assert entry["log_level"] == "info"
        assert entry["rows"] == 2  # the merged row count actually written
        assert entry["duration_ms"] == 500.0  # (100.5 - 100.0) s, bit-exact via the clock
        # Under the 10 s default the healthy save never WARNs.
        assert [e for e in logs if e.get("event") == "window_ledger.save_slow"] == []

    def test_save_rows_byte_identical_with_telemetry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Parity pin (Session-B contract): the telemetry-carrying save stores EXACTLY the
        rows the ledger holds (the load→save→load row fixed point), and the logged ``rows``
        equals the restored count — logging is the only delta."""
        monkeypatch.delenv(LEDGER_SAVE_SLOW_WARN_ENV, raising=False)
        ledger = WindowLedger()
        ledger.record(_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        with capture_logs() as logs:
            save_window_ledger(ledger, tmp_path, clock=_two_step_clock(0.0, 0.1))
        restored = load_window_ledger(tmp_path)
        assert restored.to_rows() == ledger.to_rows()  # byte-identical stored row content
        completed = [e for e in logs if e.get("event") == "window_ledger.save_completed"]
        assert len(completed) == 1
        assert completed[0]["rows"] == restored.count == 1

    def test_default_wall_clock_branch_still_saves_and_logs(self, tmp_path: Path) -> None:
        """``clock=None`` → the real ``time.monotonic`` (the production default) — the save
        lands and emits one ``save_completed`` with a non-negative measured duration."""
        ledger = WindowLedger()
        ledger.record(_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        with capture_logs() as logs:
            save_window_ledger(ledger, tmp_path)
        assert load_window_ledger(tmp_path).count == 1
        completed = [e for e in logs if e.get("event") == "window_ledger.save_completed"]
        assert len(completed) == 1
        assert completed[0]["duration_ms"] >= 0.0

    def test_save_slow_warns_when_duration_exceeds_default_threshold(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(LEDGER_SAVE_SLOW_WARN_ENV, raising=False)
        ledger = WindowLedger()
        ledger.record(_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        with capture_logs() as logs:
            save_window_ledger(ledger, tmp_path, clock=_two_step_clock(100.0, 111.0))
        slow = [e for e in logs if e.get("event") == "window_ledger.save_slow"]
        assert len(slow) == 1
        entry = slow[0]
        assert entry["log_level"] == "warning"
        assert entry["rows"] == 1
        assert entry["duration_ms"] == 11_000.0
        assert entry["threshold_s"] == DEFAULT_LEDGER_SAVE_SLOW_WARN_S
        # save_completed STILL fires — save_slow is additive, never a replacement.
        assert len([e for e in logs if e.get("event") == "window_ledger.save_completed"]) == 1

    def test_save_at_exactly_the_threshold_is_not_slow(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The WARN fires only when the duration EXCEEDS the threshold (strict >) — an
        exactly-at-threshold save stays INFO-only (the boundary pin)."""
        monkeypatch.delenv(LEDGER_SAVE_SLOW_WARN_ENV, raising=False)
        ledger = WindowLedger()
        ledger.record(_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        with capture_logs() as logs:
            save_window_ledger(
                ledger,
                tmp_path,
                clock=_two_step_clock(100.0, 100.0 + DEFAULT_LEDGER_SAVE_SLOW_WARN_S),
            )
        assert [e for e in logs if e.get("event") == "window_ledger.save_slow"] == []
        assert len([e for e in logs if e.get("event") == "window_ledger.save_completed"]) == 1

    def test_save_slow_threshold_env_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(LEDGER_SAVE_SLOW_WARN_ENV, "0.2")
        ledger = WindowLedger()
        ledger.record(_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        with capture_logs() as logs:
            save_window_ledger(ledger, tmp_path, clock=_two_step_clock(100.0, 100.5))
        slow = [e for e in logs if e.get("event") == "window_ledger.save_slow"]
        assert len(slow) == 1
        assert slow[0]["threshold_s"] == 0.2

    def test_backwards_clock_clamps_duration_to_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A (theoretical) backwards clock never fabricates a negative duration — clamped 0."""
        monkeypatch.delenv(LEDGER_SAVE_SLOW_WARN_ENV, raising=False)
        ledger = WindowLedger()
        ledger.record(_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        with capture_logs() as logs:
            save_window_ledger(ledger, tmp_path, clock=_two_step_clock(100.0, 99.0))
        completed = [e for e in logs if e.get("event") == "window_ledger.save_completed"]
        assert completed[0]["duration_ms"] == 0.0

    def test_lock_timeout_skip_emits_no_save_completed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A lock-timeout SKIP keeps its existing loud warning and emits NEITHER
        ``save_completed`` NOR ``save_slow`` — nothing completed, so nothing is claimed
        (the honest-telemetry pin; also proves the skip control flow is unchanged)."""
        monkeypatch.setenv("BRUTEX_PARTITION_LOCK_TIMEOUT_S", "0.05")
        path = ledger_path(tmp_path)
        lock_path = partition_lock_path(tmp_path, path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        ledger = WindowLedger()
        ledger.record(_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        try:
            with capture_logs() as logs:
                save_window_ledger(ledger, tmp_path)  # returns — never raises
            assert any(e["event"] == "window_ledger.save_lock_timeout" for e in logs)
            assert [e for e in logs if e.get("event") == "window_ledger.save_completed"] == []
            assert [e for e in logs if e.get("event") == "window_ledger.save_slow"] == []
            assert not path.exists()  # the skipped save wrote nothing (unchanged)
        finally:
            os.close(fd)


class TestResolveLedgerSaveSlowWarnSeconds:
    """The fail-safe env parse (mirrors ``pull_stall.resolve_stall_timeout_seconds``)."""

    def test_unset_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(LEDGER_SAVE_SLOW_WARN_ENV, raising=False)
        assert resolve_ledger_save_slow_warn_seconds() == DEFAULT_LEDGER_SAVE_SLOW_WARN_S

    def test_blank_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(LEDGER_SAVE_SLOW_WARN_ENV, "   ")
        assert resolve_ledger_save_slow_warn_seconds() == DEFAULT_LEDGER_SAVE_SLOW_WARN_S

    def test_non_numeric_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(LEDGER_SAVE_SLOW_WARN_ENV, "fast")
        assert resolve_ledger_save_slow_warn_seconds() == DEFAULT_LEDGER_SAVE_SLOW_WARN_S

    def test_zero_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(LEDGER_SAVE_SLOW_WARN_ENV, "0")
        assert resolve_ledger_save_slow_warn_seconds() == DEFAULT_LEDGER_SAVE_SLOW_WARN_S

    def test_negative_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(LEDGER_SAVE_SLOW_WARN_ENV, "-3")
        assert resolve_ledger_save_slow_warn_seconds() == DEFAULT_LEDGER_SAVE_SLOW_WARN_S

    def test_valid_float_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(LEDGER_SAVE_SLOW_WARN_ENV, "2.5")
        assert resolve_ledger_save_slow_warn_seconds() == 2.5


class TestFlushPacingTelemetry:
    """DEC-TELEMETRY-RECONCILE-LEDGERSAVE-001 — the ``window_ledger.flush_paced`` DEBUG in
    ``make_live_ledger_flush``: the throttle's ALREADY-measured save duration is now logged;
    the throttle decisions + save call sequence are pinned byte-identical."""

    def test_write_logs_measured_duration_and_next_interval(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
        with capture_logs() as logs:
            flush()
        paced = [e for e in logs if e.get("event") == "window_ledger.flush_paced"]
        assert len(paced) == 1
        entry = paced[0]
        assert entry["log_level"] == "debug"
        assert entry["duration_ms"] == 4_000.0  # EXACTLY the throttle's measured duration
        assert entry["next_interval_s"] == 20.0  # 5 (duty factor) x 4 s > the 2 s floor

    def test_throttled_skip_logs_nothing_and_save_cadence_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Parity pin: the SAME clock script as the pre-change adaptive-throttle test yields
        the SAME save call sequence (1 → 1 → 2) — a throttled skip emits no pacing line, and
        logging is the only delta on the writes."""
        from brutex.data import window_ledger_store as store_module

        ledger = WindowLedger()
        ledger.record(_key(), raw_bars=1, error_type=None, bars_appended=1, now=_NOW)
        clock = [100.0]
        saves: list[float] = []

        def _slow_counting_save(_ledger: WindowLedger, _root: Path) -> None:
            saves.append(clock[0])
            clock[0] += 4.0

        monkeypatch.setattr(store_module, "save_window_ledger", _slow_counting_save)
        flush = make_live_ledger_flush(ledger, tmp_path, min_interval_s=2.0, clock=lambda: clock[0])
        with capture_logs() as logs:
            flush()  # writes (measures 4 s)
            assert len(saves) == 1
            clock[0] += 3.0  # past the fixed 2 s, inside the adaptive 20 s → skipped
            flush()
            assert len(saves) == 1  # byte-identical throttle decision
            clock[0] += 25.0  # past the adaptive interval → writes again
            flush()
            assert len(saves) == 2
        paced = [e for e in logs if e.get("event") == "window_ledger.flush_paced"]
        assert len(paced) == 2  # one per ACTUAL write — the skip logged nothing

    def test_fast_save_next_interval_floors_at_min_interval(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.data import window_ledger_store as store_module

        ledger = WindowLedger()
        ledger.record(_key(), raw_bars=1, error_type=None, bars_appended=1, now=_NOW)

        def _instant_save(_ledger: WindowLedger, _root: Path) -> None:
            return None  # zero simulated duration

        monkeypatch.setattr(store_module, "save_window_ledger", _instant_save)
        clock = [50.0]
        flush = make_live_ledger_flush(ledger, tmp_path, min_interval_s=2.0, clock=lambda: clock[0])
        with capture_logs() as logs:
            flush()
        paced = [e for e in logs if e.get("event") == "window_ledger.flush_paced"]
        assert paced[0]["duration_ms"] == 0.0
        assert paced[0]["next_interval_s"] == 2.0  # the fixed floor wins for a fast save


class TestBarsNetNewBackwardCompat:
    """DEC-INGEST-BOUNDARY-INTEGRITY-001: ``bars_net_new`` follows the exact
    optional-column template — appended LAST, absent column loads as an honest
    ``None`` (NEVER a fabricated 0), new files round-trip the int."""

    def test_old_format_parquet_loads_bars_net_new_none(self, tmp_path: Path) -> None:
        # A pre-column file (13 columns: core 11 + streak + fetches) MUST load;
        # the absent bars_net_new is an honest unknown None — never fabricated 0.
        _write_legacy_ledger_parquet(
            tmp_path,
            {
                "market": ["dhan"],
                "security_id": ["NIFTY"],
                "interval": ["1minute"],
                "window_start": ["2021-01-01"],
                "window_end": ["2021-04-01"],
                "status": [SUCCESS],
                "bars": [5],
                "attempts": [1],
                "last_error": [None],
                "first_seen_at": ["2026-06-08T12:00:00+00:00"],
                "last_attempt_at": ["2026-06-08T12:00:00+00:00"],
                "empty_streak": [0],
                "in_pull_fetches": [1],
            },
        )
        ledger = load_window_ledger(tmp_path)
        rec = ledger.records()[0]
        assert ledger.count == 1  # loaded, NOT degraded to empty
        assert rec.bars_net_new is None
        assert rec.bars == 5  # every other column intact
        assert ledger.is_skippable(_key()) is True  # the settled row still skips

    def test_new_round_trip_carries_the_int(self, tmp_path: Path) -> None:
        ledger = WindowLedger()
        ledger.record(
            _key(), raw_bars=5, error_type=None, bars_appended=5, bars_net_new=3, now=_NOW
        )
        save_window_ledger(ledger, tmp_path)
        restored = load_window_ledger(tmp_path)
        assert restored.to_rows() == ledger.to_rows()
        assert restored.records()[0].bars_net_new == 3

    def test_new_round_trip_preserves_a_null_cell(self, tmp_path: Path) -> None:
        # A NEW file can carry a NULL bars_net_new (e.g. a cold-start seed / a
        # record with no sink result) — it round-trips as None, not 0.
        ledger = WindowLedger()
        ledger.seed_lake_success(_key(), bars=42, now=_NOW)
        save_window_ledger(ledger, tmp_path)
        restored = load_window_ledger(tmp_path)
        assert restored.records()[0].bars_net_new is None
        assert restored.to_rows() == ledger.to_rows()

    def test_mixed_old_and_new_rows_merge_on_save(self, tmp_path: Path) -> None:
        # An OLD-format file already on disk (no bars_net_new column) merges with
        # a NEW in-memory ledger: the disk row keeps its honest None, the new row
        # its int — no crash, no fabrication.
        _write_legacy_ledger_parquet(
            tmp_path,
            {
                "market": ["dhan"],
                "security_id": ["SENSEX"],
                "interval": ["1minute"],
                "window_start": ["2021-01-01"],
                "window_end": ["2021-04-01"],
                "status": [SUCCESS],
                "bars": [7],
                "attempts": [1],
                "last_error": [None],
                "first_seen_at": ["2026-06-08T12:00:00+00:00"],
                "last_attempt_at": ["2026-06-08T12:00:00+00:00"],
            },
        )
        ledger = WindowLedger()
        ledger.record(
            _key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, bars_net_new=2, now=_NOW
        )
        save_window_ledger(ledger, tmp_path)  # read-merge-write folds the disk row in
        by_symbol = {r.security_id: r for r in load_window_ledger(tmp_path).records()}
        assert by_symbol["NIFTY"].bars_net_new == 2
        assert by_symbol["SENSEX"].bars_net_new is None

    def test_schema_pin_column_order_and_names(self, tmp_path: Path) -> None:
        # The on-disk schema is FIXED: bars_net_new is the LAST column, after
        # in_pull_fetches (the append-LAST back-compat contract).
        import pyarrow.parquet as pq

        ledger = WindowLedger()
        ledger.record(
            _key(), raw_bars=1, error_type=None, bars_appended=1, bars_net_new=1, now=_NOW
        )
        save_window_ledger(ledger, tmp_path)
        names = pq.read_schema(ledger_path(tmp_path)).names
        assert names == [
            "market",
            "security_id",
            "interval",
            "window_start",
            "window_end",
            "status",
            "bars",
            "attempts",
            "last_error",
            "first_seen_at",
            "last_attempt_at",
            "empty_streak",
            "in_pull_fetches",
            "bars_net_new",
        ]

    def test_merge_rows_prefer_by_leading_fields_regardless_of_net_new(self) -> None:
        # merge_ledger_rows is arity-safe with the 14th field: same-key conflict
        # resolution still keys off status/timestamp/attempts; the winning row's
        # bars_net_new rides along untouched.
        older = _row(last_attempt="2026-06-08T12:00:00+00:00", bars_net_new=None)
        newer = _row(last_attempt="2026-06-08T13:00:00+00:00", bars_net_new=9)
        assert merge_ledger_rows([older], [newer]) == [newer]
        assert merge_ledger_rows([newer], [older]) == [newer]
