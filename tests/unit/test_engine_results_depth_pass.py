"""Unit suite for :mod:`brutex.engine.results_depth_pass` (DEC-SWEEP-RESULTS-DEPTH-001, WP5).

Drives the REAL chain end-to-end on the fixed 12-day sweep fixture (the
``test_engine_sweep_run_golden`` bars): a real ``run_persisted_sweep`` per
grid tuple, then the pass — so the parity tooth, the stamp-skip resume, the
degradation paths and every fence are exercised against genuinely persisted
stores, never against mocks of our own modules.
"""

from __future__ import annotations

import shutil
import sys
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine import results_depth_pass
from brutex.engine.combo_aggregates import ComboAggregate
from brutex.engine.results_depth_pass import (
    RESULTS_DEPTH_ENV,
    VIX_MISS_WARN_CAP,
    ResultsDepthError,
    ResultsDepthReport,
    _Tallies,
    _warn_vix_miss,
    resolve_results_depth_enabled,
    run_results_depth_pass,
)
from brutex.engine.results_store import ResultsStore
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    Timeframe,
    iter_sweep_tuples,
    load_or_create_store,
    tuple_store_path,
)
from brutex.engine.sweep_run import ComboDedup, run_persisted_sweep
from brutex.engine.sweep_supervisor import grid_identity
from brutex.engine.top100_artifact import TOP100_FILENAME, load_top100
from brutex.engine.trade_store import (
    SidecarPaths,
    load_stamp,
    read_trade_page,
    sidecar_paths_for,
    sweep_run_dir,
    write_tuple_sidecars,
)
from brutex.orchestrator.audit import AuditWriter
from brutex.strategies.types import Side
from structlog.testing import capture_logs

from tests.fakes.wp5_depth_fixtures import make_result_row, run_id_of

if TYPE_CHECKING:
    from collections.abc import Sequence

_IST = timezone(timedelta(hours=5, minutes=30))


def _bars() -> list[CandleBar]:
    """The sweep golden's fixed 12-day upward-drift fixture, verbatim."""
    out: list[CandleBar] = []
    for d in range(12):
        price = 100.0
        for m in range(120):
            close = price + 0.6 if m % 3 else price - 0.3
            ts = (
                datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=d, minutes=m)
            ).astimezone(UTC)
            hi = max(price, close) + 0.5
            lo = min(price, close) - 0.5
            out.append(
                CandleBar(
                    timestamp=ts,
                    open=Decimal(str(round(price, 2))),
                    high=Decimal(str(round(hi, 2))),
                    low=Decimal(str(round(lo, 2))),
                    close=Decimal(str(round(close, 2))),
                    volume=100,
                    open_interest=None,
                )
            )
            price = close
    return out


_BARS = _bars()


def _daily_bars() -> list[CandleBar]:
    """One bar per fixture session (the derived-1day shape the lens consumes)."""
    out: list[CandleBar] = []
    for d in range(12):
        ts = (datetime(2026, 1, 5, 15, 20, tzinfo=_IST) + timedelta(days=d)).astimezone(UTC)
        out.append(
            CandleBar(
                timestamp=ts,
                open=Decimal("100.00"),
                high=Decimal("125.00"),
                low=Decimal("99.00"),
                close=Decimal(str(round(100.0 + d, 2))),
                volume=100,
                open_interest=None,
            )
        )
    return out


def _grid(**overrides: object) -> SweepGridConfig:
    base: dict[str, object] = {
        "instruments": ["NIFTY"],
        "timeframes": [Timeframe(label="1minute", minutes=1)],
        "directions": [Side.BUY],
        "k_values": [1],
        "condition_bits": [0, 1, 2, 3, 4],
        "stop_losses": [80],
        "take_profits": [50],
        "proximities": [None],
        "mode": "A",
        "data_snapshot_hash": "snap0001",
        "indicator_version": "ind-v1",
        "code_commit_sha": "deadbeef",
    }
    base.update(overrides)
    return SweepGridConfig(**base)  # type: ignore[arg-type]


def _run_sweep(out_root: Path, grid: SweepGridConfig, bars: Sequence[CandleBar]) -> None:
    """Run the REAL persisted sweep for every tuple of *grid* under *out_root*."""
    audit_path = out_root / "audit" / "sweep-all.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit = AuditWriter(path=audit_path)
    with audit:
        for tuple_ in iter_sweep_tuples(grid):
            store = load_or_create_store(tuple_store_path(out_root, tuple_, grid))
            dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
            run_persisted_sweep(
                bars, tuple_.to_run_config(grid), store=store, dedup=dedup, audit=audit
            )


@pytest.fixture(scope="module")
def swept_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One REAL swept tree, built once and copied per test (read-only source)."""
    root = tmp_path_factory.mktemp("swept") / "sweeps"
    root.mkdir()
    _run_sweep(root, _grid(), _BARS)
    return root


@pytest.fixture
def out_root(swept_root: Path, tmp_path: Path) -> Path:
    """A private, mutable copy of the swept tree for one test."""
    dest = tmp_path / "sweeps"
    shutil.copytree(swept_root, dest)
    return dest


@pytest.fixture(scope="module")
def swept_root_two_arms(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A two-tuple grid (TP-and-no-TP arms), swept for real, built once."""
    root = tmp_path_factory.mktemp("swept2") / "sweeps"
    root.mkdir()
    _run_sweep(root, _grid(take_profits=[50, None]), _BARS)
    return root


def _run_pass(
    out_root: Path,
    grid: SweepGridConfig | None = None,
    *,
    minute: bool = True,
    daily: bool = False,
    vix: bool = False,
    **kwargs: object,
) -> ResultsDepthReport:
    return run_results_depth_pass(
        out_root=out_root,
        grid=grid if grid is not None else _grid(),
        bars_for=lambda _instrument, _tf: _BARS,
        minute_bars_for=(lambda _i: _BARS) if minute else (lambda _i: None),
        daily_bars_for=(lambda _i: _daily_bars()) if daily else None,
        vix_bars=_BARS if vix else None,
        **kwargs,  # type: ignore[arg-type]
    )


def _tuple_paths(out_root: Path, grid: SweepGridConfig) -> SidecarPaths:
    tuple_ = next(iter_sweep_tuples(grid))
    return sidecar_paths_for(
        tuple_store_path(out_root, tuple_, grid), out_root, run_id=grid_identity(grid)
    )


class TestResolveResultsDepthEnabled:
    def test_unset_defaults_on(self) -> None:
        assert resolve_results_depth_enabled({}) is True

    def test_blank_defaults_on(self) -> None:
        assert resolve_results_depth_enabled({RESULTS_DEPTH_ENV: "  "}) is True

    @pytest.mark.parametrize("token", ["1", "on", "TRUE", " yes "])
    def test_truthy_tokens_enable(self, token: str) -> None:
        assert resolve_results_depth_enabled({RESULTS_DEPTH_ENV: token}) is True

    @pytest.mark.parametrize("token", ["0", "off", "False", " no "])
    def test_falsy_tokens_disable(self, token: str) -> None:
        assert resolve_results_depth_enabled({RESULTS_DEPTH_ENV: token}) is False

    def test_junk_fails_loud(self) -> None:
        with pytest.raises(ValueError, match=RESULTS_DEPTH_ENV):
            resolve_results_depth_enabled({RESULTS_DEPTH_ENV: "maybe"})

    def test_none_env_reads_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(RESULTS_DEPTH_ENV, "0")
        assert resolve_results_depth_enabled() is False


class TestFreshPass:
    def test_full_fresh_pass_builds_everything(self, out_root: Path) -> None:
        grid = _grid()
        with capture_logs() as logs:
            report = _run_pass(out_root, grid)
        assert report.tuples_processed == 1
        assert report.tuples_skipped_current == 0
        assert report.tuples_missing_store == 0
        assert report.tuples_errored == 0
        assert report.combos_captured == 5  # the golden fixture persists 5 finalists
        assert report.combos_parity_failed == 0
        assert report.combos_zero_trades == 0
        assert report.vix_missing_trades > 0  # no VIX feed → every trade counted
        assert report.baselines_written == 1
        assert report.pool == 5
        assert report.excluded_no_aggs == 0
        assert report.zero_trade_excluded == 0
        assert report.parity_excluded == 0
        assert report.top100_written is True
        assert report.top100_entries == report.gate_passers - report.families_collapsed
        # Artifacts on disk, at the contract paths.
        paths = _tuple_paths(out_root, grid)
        for artifact in (paths.trades, paths.aggs, paths.equity, paths.stamp):
            assert artifact.exists()
        run_id = grid_identity(grid)
        run_dir = sweep_run_dir(out_root, run_id)
        assert (run_dir / TOP100_FILENAME).exists()
        assert (run_dir / "baseline-NIFTY.json").exists()
        assert load_top100(run_dir.parent, run_id) is not None
        # Ruling 4: NOTHING results-depth may sit inside the globbed sweeps
        # tree — the run dir is a sibling root and the tree itself is clean.
        assert not run_dir.is_relative_to(out_root)
        depth_parquet = [
            p
            for p in out_root.rglob("*.parquet")
            if p.name.endswith((".trades.parquet", ".aggs.parquet", ".equity.parquet"))
        ]
        assert depth_parquet == []
        events = [log["event"] for log in logs]
        assert "results_depth.vix_absent" in events
        assert "results_depth.vix_miss" in events  # capped per-combo WARN (Ruling 7)
        assert "results_depth.tuple_done" in events
        assert "results_depth.pass_done" in events

    def test_minute_path_feeds_the_wire_rows(self, out_root: Path) -> None:
        grid = _grid()
        _run_pass(out_root, grid, minute=True)
        paths = _tuple_paths(out_root, grid)
        stamp = load_stamp(paths)
        assert stamp is not None
        run_id = next(iter(stamp.trade_spans))
        page = read_trade_page(paths, run_id=run_id, offset=0, limit=5)
        assert page.total > 0
        assert all(row["mae_granularity"] == "1m" for row in page.rows)
        assert all(row["path_1m_status"] == "ok" for row in page.rows)

    def test_no_minute_data_degrades_honestly(self, out_root: Path) -> None:
        grid = _grid()
        with capture_logs() as logs:
            report = _run_pass(out_root, grid, minute=False)
        assert report.top100_written is True
        assert any(log["event"] == "results_depth.no_minute_data" for log in logs)
        paths = _tuple_paths(out_root, grid)
        stamp = load_stamp(paths)
        assert stamp is not None
        run_id = next(iter(stamp.trade_spans))
        page = read_trade_page(paths, run_id=run_id, offset=0, limit=5)
        assert all(row["path_1m_status"] == "no_1m_data" for row in page.rows)
        assert all(row["mae_granularity"] == "tf" for row in page.rows)

    def test_vix_feed_stamps_every_trade(self, out_root: Path) -> None:
        with capture_logs() as logs:
            report = _run_pass(out_root, vix=True)
        assert report.vix_missing_trades == 0
        # A fully-stamped pass emits ZERO vix_miss warns (nothing missed).
        assert all(log["event"] != "results_depth.vix_miss" for log in logs)

    def test_daily_lens_is_threaded(self, out_root: Path) -> None:
        report = _run_pass(out_root, daily=True)
        assert report.tuples_processed == 1
        assert report.top100_written is True

    def test_two_arm_grid_reuses_the_instrument_context(
        self, swept_root_two_arms: Path, tmp_path: Path
    ) -> None:
        dest = tmp_path / "sweeps"
        shutil.copytree(swept_root_two_arms, dest)
        report = _run_pass(dest, _grid(take_profits=[50, None]))
        assert report.tuples_processed == 2
        assert report.tuples_missing_store == 0
        assert report.baselines_written == 1  # ONE instrument → ONE baseline
        assert report.top100_written is True


class TestResumeAndRebuild:
    def test_second_run_skips_current_and_converges(self, out_root: Path) -> None:
        grid = _grid()
        _run_pass(out_root, grid)
        run_id = grid_identity(grid)
        top100_path = sweep_run_dir(out_root, run_id) / TOP100_FILENAME
        first_bytes = top100_path.read_bytes()
        with capture_logs() as logs:
            second = _run_pass(out_root, grid)
        assert second.tuples_processed == 0
        assert second.tuples_skipped_current == 1
        assert second.pool == 5  # the pool re-reads the stamped-current sidecars
        assert second.top100_written is True
        assert top100_path.read_bytes() == first_bytes  # deterministic convergence
        assert any(log["event"] == "results_depth.tuple_skipped_current" for log in logs)

    def test_store_change_invalidates_the_stamp_and_rebuilds(self, out_root: Path) -> None:
        grid = _grid()
        _run_pass(out_root, grid)
        # Re-flush the store with one row fewer — new bytes, stale stamp.
        store_path = tuple_store_path(out_root, next(iter_sweep_tuples(grid)), grid)
        rows = ResultsStore.load(store_path).rows()
        store_path.unlink()
        shrunk = ResultsStore(path=store_path)
        for row in rows[:-1]:
            shrunk.append(row)
        shrunk.flush()
        report = _run_pass(out_root, grid)
        assert report.tuples_processed == 1
        assert report.tuples_skipped_current == 0
        assert report.pool == 4

    def test_missing_store_is_counted_not_fatal(self, out_root: Path) -> None:
        # A wider grid whose second tuple was never swept.
        grid = _grid(take_profits=[50, None])
        report = _run_pass(out_root, grid)
        assert report.tuples_missing_store == 1
        assert report.tuples_processed == 1
        assert report.top100_written is True


class TestExclusionsAndParity:
    def test_fabricated_rows_are_parity_and_zero_trade_excluded(self, tmp_path: Path) -> None:
        grid = _grid()
        out = tmp_path / "sweeps"
        tuple_ = next(iter_sweep_tuples(grid))
        store_path = tuple_store_path(out, tuple_, grid)
        store = ResultsStore(path=store_path)
        # A row whose stored counts can never match the oracle re-sim…
        store.append(
            make_result_row(
                run_id_of(1),
                total_trades=999,
                winning_trades=500,
                losing_trades=499,
            )
        )
        # …and a zero-trade row whose never-firing mask re-sims to an exact,
        # parity-CLEAN zero (the Stage-3-ungated shape).
        store.append(
            make_result_row(
                run_id_of(2),
                total_trades=0,
                winning_trades=0,
                losing_trades=0,
                net_pnl_paisa=0,
                gross_profit_paisa=0,
                gross_loss_paisa=0,
            )
        )
        store.flush()
        with capture_logs() as logs:
            report = _run_pass(out, grid)
        assert report.combos_parity_failed == 1
        assert report.combos_zero_trades == 1
        assert report.combos_captured == 1
        assert report.zero_trade_excluded == 1
        assert report.parity_excluded == 1
        assert report.pool == 0
        assert report.gate_passers == 0
        assert report.top100_written is True
        assert report.top100_entries == 0
        assert any(log["event"] == "results_depth.capture_mismatch" for log in logs)

    def test_rows_without_aggs_are_excluded_and_counted(self, out_root: Path) -> None:
        grid = _grid()
        _run_pass(out_root, grid)
        # Replace the sidecar trio with an EMPTY one stamped for the SAME
        # store bytes — a current stamp whose agg join misses every row.
        store_path = tuple_store_path(out_root, next(iter_sweep_tuples(grid)), grid)
        paths = sidecar_paths_for(store_path, out_root, run_id=grid_identity(grid))
        stamp = load_stamp(paths)
        assert stamp is not None
        write_tuple_sidecars(paths, [], [], store_content_hash=stamp.store_content_hash)
        report = _run_pass(out_root, grid)
        assert report.tuples_skipped_current == 1
        assert report.excluded_no_aggs == 5
        assert report.pool == 0


class TestFences:
    def test_tuple_error_is_quarantined_not_fatal(
        self, out_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_args: object, **_kwargs: object) -> list[int]:
            raise ValueError("stage1a exploded")

        monkeypatch.setattr(results_depth_pass, "build_bar_bits", _boom)
        with capture_logs() as logs:
            report = _run_pass(out_root)
        assert report.tuples_errored == 1
        assert report.tuples_processed == 0
        assert report.pool == 0
        assert report.top100_written is True  # the empty artifact still ships
        errors = [log for log in logs if log["event"] == "results_depth.tuple_error"]
        assert len(errors) == 1
        assert errors[0]["error_type"] == "ValueError"

    def test_memory_error_re_raises_raw(
        self, out_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _oom(*_args: object, **_kwargs: object) -> list[int]:
            raise MemoryError

        monkeypatch.setattr(results_depth_pass, "build_bar_bits", _oom)
        with pytest.raises(MemoryError):
            _run_pass(out_root)

    def test_top100_write_failure_is_loud_and_reported(
        self, out_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(*_args: object, **_kwargs: object) -> Path:
            raise OSError("disk gone")

        monkeypatch.setattr(results_depth_pass, "write_top100", _fail)
        with capture_logs() as logs:
            report = _run_pass(out_root)
        assert report.top100_written is False
        assert report.top100_entries == 0
        assert report.families_collapsed == 0
        assert any(log["event"] == "results_depth.top100_error" for log in logs)

    def test_baseline_memory_error_re_raises_raw(
        self, out_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _oom(*_args: object, **_kwargs: object) -> object:
            raise MemoryError

        monkeypatch.setattr(results_depth_pass, "compute_baseline", _oom)
        with pytest.raises(MemoryError):
            _run_pass(out_root)

    def test_top100_memory_error_re_raises_raw(
        self, out_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _oom(*_args: object, **_kwargs: object) -> object:
            raise MemoryError

        monkeypatch.setattr(results_depth_pass, "build_top100", _oom)
        with pytest.raises(MemoryError):
            _run_pass(out_root)

    def test_baseline_failure_is_loud_and_non_fatal(
        self, out_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(*_args: object, **_kwargs: object) -> object:
            raise OSError("volume yanked")

        monkeypatch.setattr(results_depth_pass, "compute_baseline", _fail)
        with capture_logs() as logs:
            report = _run_pass(out_root)
        assert report.baselines_written == 0
        assert report.top100_written is True
        assert any(log["event"] == "results_depth.baseline_error" for log in logs)
        run_id = grid_identity(_grid())
        loaded = load_top100(sweep_run_dir(out_root, run_id).parent, run_id)
        assert loaded is not None
        assert loaded.baselines == {}


class TestVixMissWarnCap:
    """The Ruling-7 capped per-combo vix_miss WARN — all three branches."""

    def test_warns_up_to_the_cap_then_one_capped_note_then_silence(self) -> None:
        tallies = _Tallies()
        with capture_logs() as logs:
            for i in range(VIX_MISS_WARN_CAP + 5):
                _warn_vix_miss(run_id_of(i + 1), 3, tallies)
        misses = [log for log in logs if log["event"] == "results_depth.vix_miss"]
        capped = [log for log in logs if log["event"] == "results_depth.vix_miss_capped"]
        assert len(misses) == VIX_MISS_WARN_CAP
        assert len(capped) == 1
        assert capped[0]["cap"] == VIX_MISS_WARN_CAP
        # Suppressed past the cap — but ALWAYS counted by the caller's tally.
        assert tallies.vix_miss_combos == VIX_MISS_WARN_CAP + 5

    def test_each_warn_names_the_combo_and_its_miss_count(self) -> None:
        tallies = _Tallies()
        with capture_logs() as logs:
            _warn_vix_miss(run_id_of(7), 4, tallies)
        (entry,) = [log for log in logs if log["event"] == "results_depth.vix_miss"]
        assert entry["run_id"] == run_id_of(7)
        assert entry["missing_trades"] == 4


class TestManifestProbe:
    def _runs_dir(self, out_root: Path) -> Path:
        target = sweep_run_dir(out_root, grid_identity(_grid()))
        target.mkdir(parents=True, exist_ok=True)
        return target

    def test_valid_manifest_is_acknowledged(self, out_root: Path) -> None:
        (self._runs_dir(out_root) / "manifest.json").write_text('{"run_id": "x"}')
        with capture_logs() as logs:
            _run_pass(out_root)
        assert any(log["event"] == "results_depth.manifest_seen" for log in logs)

    def test_torn_manifest_warns_and_proceeds(self, out_root: Path) -> None:
        (self._runs_dir(out_root) / "manifest.json").write_text("{torn")
        with capture_logs() as logs:
            report = _run_pass(out_root)
        assert report.top100_written is True
        assert any(
            log["event"] == "results_depth.manifest_invalid" and log["reason"] == "unreadable"
            for log in logs
        )

    def test_non_object_manifest_warns_and_proceeds(self, out_root: Path) -> None:
        (self._runs_dir(out_root) / "manifest.json").write_text("[1,2]")
        with capture_logs() as logs:
            report = _run_pass(out_root)
        assert report.top100_written is True
        assert any(
            log["event"] == "results_depth.manifest_invalid" and log["reason"] == "not_object"
            for log in logs
        )


class TestSidecarReaderBelts:
    def test_load_agg_records_wraps_read_damage(self, tmp_path: Path) -> None:
        from brutex.engine.results_depth_pass import _load_agg_records

        paths = SidecarPaths(
            trades=tmp_path / "x.trades.parquet",
            aggs=tmp_path / "x.aggs.parquet",
            equity=tmp_path / "x.equity.parquet",
            stamp=tmp_path / "x.stamp.json",
        )
        with pytest.raises(ResultsDepthError, match="unreadable aggs sidecar"):
            _load_agg_records(paths)

    def test_entry_ts_sets_wraps_read_damage(self, tmp_path: Path) -> None:
        from brutex.engine.results_depth_pass import _entry_ts_sets

        paths = SidecarPaths(
            trades=tmp_path / "x.trades.parquet",
            aggs=tmp_path / "x.aggs.parquet",
            equity=tmp_path / "x.equity.parquet",
            stamp=tmp_path / "x.stamp.json",
        )
        with pytest.raises(ResultsDepthError, match="unreadable trades sidecar"):
            _entry_ts_sets(paths, [run_id_of(1)])

    def test_entry_ts_sets_empty_run_ids_short_circuits(self, tmp_path: Path) -> None:
        from brutex.engine.results_depth_pass import _entry_ts_sets

        paths = SidecarPaths(
            trades=tmp_path / "absent.parquet",
            aggs=tmp_path / "absent.parquet",
            equity=tmp_path / "absent.parquet",
            stamp=tmp_path / "absent.json",
        )
        assert _entry_ts_sets(paths, []) == {}

    def test_missing_polars_raises_the_install_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.engine.results_depth_pass import _entry_ts_sets, _load_agg_records

        paths = SidecarPaths(
            trades=tmp_path / "x.parquet",
            aggs=tmp_path / "x.parquet",
            equity=tmp_path / "x.parquet",
            stamp=tmp_path / "x.json",
        )
        monkeypatch.setitem(sys.modules, "polars", None)  # block `import polars`
        with pytest.raises(ResultsDepthError, match="requires 'polars'"):
            _load_agg_records(paths)
        with pytest.raises(ResultsDepthError, match="requires 'polars'"):
            _entry_ts_sets(paths, [run_id_of(1)])


class TestCompositesFor:
    def test_composites_land_on_the_persisted_aggs(self, out_root: Path) -> None:
        grid = _grid()
        _run_pass(out_root, grid)
        paths = _tuple_paths(out_root, grid)
        from brutex.engine.results_depth_pass import _load_agg_records

        aggs = _load_agg_records(paths)
        assert len(aggs) == 5
        scores = [agg.composite_score for agg in aggs]
        assert all(isinstance(score, float) for score in scores)
        assert len(set(scores)) > 1  # a real z-spread, not a constant fill

    def test_empty_eligible_population_scores_nothing(self) -> None:
        from brutex.engine.results_depth_pass import _composites_for

        assert _composites_for({}, []) == {}

    def test_defensive_none_average_belts(self) -> None:
        # ``stats_live`` aggregates always carry the paisa averages; the belt
        # legs are exercised directly against a hand-relaxed record.
        from dataclasses import replace

        from brutex.engine.results_depth_pass import _composites_for

        from tests.fakes.wp4_twin_fixtures import fingerprint_seed
        from tests.fakes.wp5_depth_fixtures import make_agg

        row = make_result_row(run_id_of(1))
        agg = make_agg(run_id_of(1), fingerprint=fingerprint_seed(1))
        relaxed = replace(
            agg, mae_avg_paisa=None, mfe_avg_paisa=None, recovery_factor=None, win_loss_ratio=None
        )
        scores = _composites_for({row.run_id: row}, [relaxed])
        assert set(scores) == {row.run_id}

    def test_agg_records_round_trip_types(self, out_root: Path) -> None:
        grid = _grid()
        _run_pass(out_root, grid)
        from brutex.engine.results_depth_pass import _load_agg_records

        aggs = _load_agg_records(_tuple_paths(out_root, grid))
        assert all(isinstance(agg, ComboAggregate) for agg in aggs)
        assert all(agg.parity_ok for agg in aggs)


class TestCascadeJoinThreading:
    """DEC-RESULTS-CASCADE-VERDICT-JOIN-001: the kwarg threads VERBATIM."""

    def test_build_top100_receives_the_exact_join_object(
        self, out_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.engine import results_depth_pass
        from brutex.engine.cascade_join import CascadeJoin

        sentinel = CascadeJoin(
            sweep_id=grid_identity(_grid()),
            version=1,
            n_input=0,
            n_survived=0,
            by_candidate={},
            ambiguous_ids=frozenset(),
            groups=(),
            skipped_groups=0,
            skipped_verdicts=0,
            nulled_values=0,
            rank_inconsistencies=0,
        )
        seen: dict[str, object] = {}
        real_build = results_depth_pass.build_top100

        def _spy(*args: object, **kwargs: object) -> object:
            seen["cascade_join"] = kwargs.get("cascade_join")
            return real_build(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(results_depth_pass, "build_top100", _spy)
        _run_pass(out_root, cascade_join=sentinel)
        # The EXACT object — identity, not a copy (the writer's sweep_id gate
        # judges the very join the adapter built).
        assert seen["cascade_join"] is sentinel

    def test_default_none_writes_no_report_meta(self, out_root: Path) -> None:
        _run_pass(out_root)
        run_id = grid_identity(_grid())
        loaded = load_top100(sweep_run_dir(out_root, run_id).parent, run_id)
        assert loaded is not None
        assert loaded.cascade_join == {
            "state": "no_report",
            "sweep_id": None,
            "n_input": None,
            "n_survived": None,
        }

    def test_matching_join_stamps_the_written_artifact(self, out_root: Path) -> None:
        from brutex.engine.cascade_join import CascadeJoin

        run_id = grid_identity(_grid())
        join = CascadeJoin(
            sweep_id=run_id,
            version=1,
            n_input=3,
            n_survived=1,
            by_candidate={},
            ambiguous_ids=frozenset(),
            groups=(),
            skipped_groups=0,
            skipped_verdicts=0,
            nulled_values=0,
            rank_inconsistencies=0,
        )
        _run_pass(out_root, cascade_join=join)
        loaded = load_top100(sweep_run_dir(out_root, run_id).parent, run_id)
        assert loaded is not None
        assert loaded.cascade_join == {
            "state": "joined",
            "sweep_id": run_id,
            "n_input": 3,
            "n_survived": 1,
        }
        # No pooled run_id sits in the (empty) join — honest not_in_cascade_pool.
        assert all(
            entry["cascade"] == {"status": "not_validated", "reason": "not_in_cascade_pool"}
            for entry in loaded.entries
        )
