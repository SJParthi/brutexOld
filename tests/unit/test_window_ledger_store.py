"""Unit tests for :mod:`brutex.data.window_ledger_store` (DEC-WINDOW-LEDGER-001, PR-2).

100% line+branch on the durable Parquet backing: the save→load row fixed point,
the never-raise degradation (missing / corrupt / wrong-shape), the one-shot
migration that folds the legacy status-log sidecar (the coverage sidecar fold was
retired in Stage e), the PLANNED upsert (`plan_windows`), the throttled live
flush, and the begin/end dry-run rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from brutex.data.fetch_coverage import WindowKey
from brutex.data.window_ledger import SKIPPED, SUCCESS, WindowLedger
from brutex.data.window_ledger_store import (
    begin_pull_ledger,
    end_pull_ledger,
    ledger_dir,
    ledger_path,
    load_or_migrate_window_ledger,
    load_window_ledger,
    make_live_ledger_flush,
    plan_windows,
    reconcile_ledger_windows,
    reconcile_ledger_with_lake,
    save_window_ledger,
)
from brutex.data.window_status import WindowStatusLog
from brutex.data.window_status_store import save_window_status

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
        # Status log: a SUCCESS (NIFTY) + a terminal EMPTY (SENSEX, attempts>=3).
        log = WindowStatusLog()
        log.record(_key("NIFTY"), raw_bars=9, error_type=None, bars_appended=9, now=_NOW)
        for _ in range(3):
            log.record(_key("SENSEX"), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        save_window_status(log, tmp_path)

        ledger = load_or_migrate_window_ledger(tmp_path, now=_NOW)
        by_symbol = {r.security_id: r for r in ledger.records()}
        assert by_symbol["NIFTY"].status == SUCCESS
        assert by_symbol["NIFTY"].bars == 9  # status-log row folded
        assert by_symbol["SENSEX"].status == SKIPPED  # EMPTY-at-threshold → SKIPPED

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
        from brutex.adapters.dhan.ingest import _windows

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

    def test_empty_streak_round_trips(self, tmp_path: Path) -> None:
        ledger = WindowLedger()
        # Build a streak of 2 (below threshold 3) so it persists as EMPTY, streak 2.
        ledger.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        ledger.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        save_window_ledger(ledger, tmp_path)
        restored = load_window_ledger(tmp_path)
        assert restored.to_rows() == ledger.to_rows()
        assert restored.records()[0].empty_streak == 2


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
            WindowKey(market="dhan", security_id="NIFTY", interval="1day", start=_START, end=_END)
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
        key = WindowKey(market="dhan", security_id="NIFTY", interval="1day", start=_START, end=_END)
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
