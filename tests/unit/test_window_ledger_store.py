"""Unit tests for :mod:`brutex.data.window_ledger_store` (DEC-WINDOW-LEDGER-001, PR-2).

100% line+branch on the durable Parquet backing: the save→load row fixed point,
the never-raise degradation (missing / corrupt / wrong-shape), the one-shot
migration that folds the two legacy sidecars (status-log wins on conflict), the
PLANNED upsert (`plan_windows`), the throttled live flush, and the
begin/end dry-run rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from brutex.data.fetch_coverage import FetchCoverage, WindowKey
from brutex.data.fetch_coverage_store import save_fetch_coverage
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
    """The one-shot fold of the two legacy JSON sidecars (status-log wins)."""

    def test_migrate_folds_both_sidecars(self, tmp_path: Path) -> None:
        # Status log: a SUCCESS (NIFTY) + a terminal EMPTY (SENSEX, attempts>=3).
        log = WindowStatusLog()
        log.record(_key("NIFTY"), raw_bars=9, error_type=None, bars_appended=9, now=_NOW)
        for _ in range(3):
            log.record(_key("SENSEX"), raw_bars=0, error_type=None, bars_appended=0, now=_NOW)
        save_window_status(log, tmp_path)
        # Coverage set: NIFTY (conflict — status wins) + a coverage-only BANKNIFTY.
        cov = FetchCoverage()
        cov.mark_complete(_key("NIFTY"))
        cov.mark_complete(_key("BANKNIFTY"))
        save_fetch_coverage(cov, tmp_path)

        ledger = load_or_migrate_window_ledger(tmp_path, now=_NOW)
        by_symbol = {r.security_id: r for r in ledger.records()}
        assert by_symbol["NIFTY"].status == SUCCESS
        assert by_symbol["NIFTY"].bars == 9  # status-log row WON (richer)
        assert by_symbol["SENSEX"].status == SKIPPED  # EMPTY-at-threshold → SKIPPED
        assert by_symbol["BANKNIFTY"].status == SUCCESS  # coverage-only seed
        assert by_symbol["BANKNIFTY"].bars == 0

    def test_migrate_empty_sidecars_is_empty_ledger(self, tmp_path: Path) -> None:
        assert load_or_migrate_window_ledger(tmp_path, now=_NOW).count == 0

    def test_existing_parquet_is_loaded_not_remigrated(self, tmp_path: Path) -> None:
        ledger = WindowLedger()
        ledger.record(_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
        save_window_ledger(ledger, tmp_path)
        # A stale coverage sidecar must be IGNORED once the Parquet exists.
        cov = FetchCoverage()
        cov.mark_complete(_key("BANKNIFTY"))
        save_fetch_coverage(cov, tmp_path)
        restored = load_or_migrate_window_ledger(tmp_path, now=_NOW)
        assert {r.security_id for r in restored.records()} == {"NIFTY"}

    def test_migrate_defaults_now(self, tmp_path: Path) -> None:
        cov = FetchCoverage()
        cov.mark_complete(_key("NIFTY"))
        save_fetch_coverage(cov, tmp_path)
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
