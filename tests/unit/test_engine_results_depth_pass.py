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
    """A 12-day gap-up drift ALL FIVE masks rank on under the one worst-case law.

    RE-CRAFTED for DEC-FILL-WORST-CASE-ONLY-001 (a worst-case entry fills at
    the bar HIGH, so the old ±0.5-pad drift SL'd every entry on its own bar):
    7-bar cycles of NARROW gap-up bars (open = prior close + 3.00, range 0.50
    < the SL 0.80; the next gap-up's low clears the TP level → +50 wins), a
    shallow bearish bar at cycle position 3, and a DEEP bearish bar (range
    0.85 > the SL) every 4th cycle whose stop-out keeps profit_factor finite.
    The per-day price RESET to 100 under carried EMAs makes the below-EMA
    bits (1/3) fire early-day and the above-EMA bits (0/2/4) late-day, so
    every k=1 mask trades on both win and loss bars. Measured at sl=80/tp=50:
    masks 1/2/4/8/16 → PF 3.47/3.04/3.58/3.11/3.63, all gates green.
    """
    out: list[CandleBar] = []
    cycle = 0
    for d in range(12):
        price = 100.0
        for m in range(120):
            k = m % 7
            if k == 0:
                cycle += 1
            if k == 3:  # shallow bearish bar (below-EMA bits fire)
                o = price
                c, h, lo = o - 0.3, o + 0.1, o - 0.4
            elif k == 4 and cycle % 4 == 0:  # deep bearish loss carrier
                o = price
                c, h, lo = o - 0.65, o + 0.1, o - 0.75
            else:  # narrow gap-up win bar
                o = price + 3.0
                c, h, lo = o + 0.3, o + 0.4, o - 0.1
            ts = (
                datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=d, minutes=m)
            ).astimezone(UTC)
            out.append(
                CandleBar(
                    timestamp=ts,
                    open=Decimal(str(round(o, 2))),
                    high=Decimal(str(round(h, 2))),
                    low=Decimal(str(round(lo, 2))),
                    close=Decimal(str(round(c, 2))),
                    volume=100,
                    open_interest=None,
                )
            )
            price = c
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
            # tf==1minute fixed point (DEC-FILL-EXEC-GRID-1M-001): the SAME
            # list serves as decision AND 1m execution stream.
            run_persisted_sweep(
                bars,
                tuple_.to_run_config(grid),
                store=store,
                dedup=dedup,
                audit=audit,
                exec_bars_1m=bars,
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

    def test_no_minute_data_refuses_the_tuple_loudly(self, out_root: Path) -> None:
        """No 1m stream ⇒ the 1m-grid re-sim is IMPOSSIBLE — refuse, never fake.

        REWRITTEN for DEC-FILL-EXEC-GRID-1M-001: the old law (degrade the
        capture to ``no_1m_data`` and still build sidecars off a decision-TF
        re-sim) is RETIRED — every fill now resolves on the 1-minute execution
        stream, so an instrument with no 1m bars cannot be re-simulated at
        all. The pass records the tuple errored (the per-tuple fence catches
        the loud ``ResultsDepthError``) and writes NO sidecars — the
        correct-or-absent posture.
        """
        grid = _grid()
        with capture_logs() as logs:
            report = _run_pass(out_root, grid, minute=False)
        assert report.tuples_errored == 1
        assert report.tuples_processed == 0
        assert report.pool == 0
        assert report.top100_written is True  # the empty artifact still ships
        assert any(log["event"] == "results_depth.no_minute_data" for log in logs)
        errors = [log for log in logs if log["event"] == "results_depth.tuple_error"]
        assert len(errors) == 1
        assert errors[0]["error_type"] == "ResultsDepthError"
        # No sidecar trio was written for the refused tuple.
        paths = _tuple_paths(out_root, grid)
        assert load_stamp(paths) is None
        assert not paths.trades.exists()

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
        # DEC-DEPTH-STAMP-REFDATA-001 (#1926): carry the SAME reference_fingerprint
        # the pass wrote so the re-stamped trio stays CURRENT (both the store bytes
        # AND the reference fingerprint must match, else the tuple re-derives).
        write_tuple_sidecars(
            paths,
            [],
            [],
            store_content_hash=stamp.store_content_hash,
            reference_fingerprint=stamp.reference_fingerprint,
        )
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
            resilience=tmp_path / "x.resilience.parquet",
            resilience_stamp=tmp_path / "x.resilience.stamp.json",
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
            resilience=tmp_path / "x.resilience.parquet",
            resilience_stamp=tmp_path / "x.resilience.stamp.json",
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
            resilience=tmp_path / "absent.resilience.parquet",
            resilience_stamp=tmp_path / "absent.resilience.json",
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
            resilience=tmp_path / "x.resilience.parquet",
            resilience_stamp=tmp_path / "x.resilience.json",
        )
        monkeypatch.setitem(sys.modules, "polars", None)  # block `import polars`
        with pytest.raises(ResultsDepthError, match="requires 'polars'"):
            _load_agg_records(paths)
        with pytest.raises(ResultsDepthError, match="requires 'polars'"):
            _entry_ts_sets(paths, [run_id_of(1)])


class TestOldSchemaAggsBackCompat:
    def test_49_column_aggs_loads_session_gap_zero_and_tuple_still_pools(
        self, out_root: Path
    ) -> None:
        """A pre-#1743 (49-column) aggs sidecar loads presence-tolerantly (review r1 NIT-3).

        Before the fix, ``ComboAggregate(**record)`` raised ``TypeError`` on the
        missing ``exits_session_gap`` kwarg — swallowed by the per-tuple fence as
        a spurious ``tuples_errored`` (double-counted beside
        ``tuples_skipped_current``) with the stamped-current tuple dropped from
        the pool. Now the column defaults to the HONEST 0 (the pre-guard walker
        never emitted the ``session_gap`` label, so 0 IS that old artifact's
        true label count) per the WindowLedgerRecord / Ruling-5 optional-column
        template, and the tuple pools normally. EXTENDED for
        DEC-FILL-EXEC-GRID-1M-001: the five 1m-grid counter columns
        (``exits_deferred`` … ``forced_fill_slid_minutes``) get the SAME
        presence-tolerant honest-0 default — dropping all six new-era columns
        yields the genuine 49-column pre-#1743 artifact shape (55 − 6 = 49).
        """
        import polars as pl

        grid = _grid()
        fresh = _run_pass(out_root, grid)
        assert fresh.tuples_processed == 1
        paths = _tuple_paths(out_root, grid)

        # Rewrite the aggs sidecar WITHOUT the new columns — the exact
        # 49-column shape a pre-#1743 writer produced — and re-stamp its
        # per-file hash the way the OLD code's own write_tuple_sidecars would
        # have (the stamp's store_content_hash is untouched, so the tuple
        # reads stamped-current — the #1743 honest clause (ii) shape).
        new_era_columns = [
            "exits_session_gap",
            "exits_deferred",
            "signals_dropped_collision",
            "fill_slid_minutes",
            "exit_fill_slid_minutes",
            "forced_fill_slid_minutes",
        ]
        df = pl.read_parquet(paths.aggs)
        assert all(col in df.columns for col in new_era_columns)
        assert len(df.columns) - len(new_era_columns) == 49
        # A REAL pre-#1743 artifact's FIVE tallies sum to n_trades (its
        # walker never emitted the session_gap label — those trades closed
        # under another label). The 1m-grid fixture legitimately produces
        # session_gap exits, so fold that count into exits_end_of_data before
        # dropping — emulating a self-consistent old record, never an
        # invariant-violating one (the honest-0 default must re-satisfy the
        # __post_init__ sum law on read-back).
        df = df.with_columns(
            (pl.col("exits_end_of_data") + pl.col("exits_session_gap")).alias("exits_end_of_data")
        )
        df.drop(new_era_columns).write_parquet(paths.aggs)
        import hashlib
        import json

        stamp_payload = json.loads(paths.stamp.read_text())
        old_bytes = paths.aggs.read_bytes()
        stamp_payload["content_hashes"]["aggs"] = hashlib.sha256(old_bytes).hexdigest()
        stamp_payload["content_sizes"]["aggs"] = len(old_bytes)
        paths.stamp.write_text(json.dumps(stamp_payload))

        # Unit: the reader loads the honest 0 default — no TypeError, no drop.
        from brutex.engine.results_depth_pass import _load_agg_records

        aggs = _load_agg_records(paths)
        assert len(aggs) == 5
        assert all(agg.exits_session_gap == 0 for agg in aggs)
        assert all(agg.exits_deferred == 0 for agg in aggs)
        assert all(agg.signals_dropped_collision == 0 for agg in aggs)
        assert all(agg.fill_slid_minutes == 0 for agg in aggs)
        assert all(agg.exit_fill_slid_minutes == 0 for agg in aggs)
        assert all(agg.forced_fill_slid_minutes == 0 for agg in aggs)

        # End-to-end: the stamped-current tuple is SKIPPED (not rebuilt) and
        # still POOLS — never the pre-fix errored/dropped double-count.
        report = _run_pass(out_root, grid)
        assert report.tuples_skipped_current == 1
        assert report.tuples_errored == 0
        assert report.pool == 5
        assert report.top100_written is True

    def test_present_column_value_is_preserved_not_clobbered(self, out_root: Path) -> None:
        """`setdefault` never overwrites a REAL stored count (mutant killer)."""
        import polars as pl

        grid = _grid()
        _run_pass(out_root, grid)
        paths = _tuple_paths(out_root, grid)
        records = pl.read_parquet(paths.aggs).to_dicts()
        # Hand-shape the first row to carry a DISTINCTIVE session_gap count,
        # keeping the exit-tally sum invariant (move ONE exit out of whichever
        # real bucket holds it into session_gap — the fixture's finalists all
        # trade, and under the 1m exec grid they already carry real
        # session_gap counts, so the pin is the INCREMENTED value surviving).
        first = records[0]
        donor = next(
            name
            for name in (
                "exits_stop_loss",
                "exits_take_profit",
                "exits_signal",
                "exits_forced_1520",
                "exits_end_of_data",
            )
            if int(first[name]) > 0
        )
        prior_first = int(first["exits_session_gap"])
        prior_total = sum(int(rec["exits_session_gap"]) for rec in records)
        first[donor] = int(first[donor]) - 1
        first["exits_session_gap"] = prior_first + 1
        pl.DataFrame(records).write_parquet(paths.aggs)
        from brutex.engine.results_depth_pass import _load_agg_records

        aggs = _load_agg_records(paths)
        by_id = {agg.run_id: agg for agg in aggs}
        # The real (incremented) count survived — a setdefault-clobber mutant
        # would read the honest-0 default here instead.
        assert by_id[str(first["run_id"])].exits_session_gap == prior_first + 1
        # Every other row kept its own stored count (total moved by exactly 1).
        assert sum(agg.exits_session_gap for agg in aggs) == prior_total + 1


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
        # The shared fake defaults the five 1m-grid counter fields to 0
        # (DEC-FILL-EXEC-GRID-1M-001) — no per-call overrides needed.
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


class TestMissingStoreLoud:
    """DEC-DEPTH-MISSING-STORE-LOUD-001 (#1925, review round 1 M2): a missing
    finalist store is classified deterministically (no-bars ⇒ expected; bars
    present ⇒ deletion-suspect), counted + capped-INFO'd + — when the
    bars-present, not-quarantined count exceeds the run-scoped FAILED
    allowance (round 2 M1: the quarantined-prior class is identity-attributed) —
    WARNed loud with an actionable path sample. The out_root fixture swept the
    ONE-arm _grid(); running the pass over the TWO-arm grid leaves the tp=None
    tuple with no store on disk — exactly one (bars-present) missing store."""

    def _two_arm_grid(self) -> SweepGridConfig:
        return _grid(take_profits=[50, None])

    def test_missing_store_unexplained_warns(self, out_root: Path) -> None:
        with capture_logs() as logs:
            report = _run_pass(out_root, self._two_arm_grid(), expected_missing=0)
        assert report.tuples_missing_store == 1
        assert report.missing_expected_no_bars == 0  # bars present — deletion-suspect
        assert report.missing_store_unexplained == 1  # the pass's OWN computed field
        warns = [log for log in logs if log["event"] == "results_depth.missing_store_unexplained"]
        assert len(warns) == 1
        assert warns[0]["unexplained"] == 1
        assert warns[0]["expected"] == 0
        # Review round 1 item 3: the remediation names sweep-verify's REAL flag
        # (--results-root; --out-root does not exist) + review round 1 M2: the
        # run-scoped-allowance residual heuristic is disclosed in the note.
        assert "--results-root" in warns[0]["note"]
        assert "--out-root" not in warns[0]["note"]
        assert "run-scoped" in warns[0]["note"]
        # The per-store INFO fired (capped) + the sample carries the absent path.
        infos = [log for log in logs if log["event"] == "results_depth.missing_store"]
        assert len(infos) == 1
        assert report.missing_store_sample and report.missing_store_sample[0].endswith(".parquet")

    def test_explained_missing_is_info_only_no_warn(self, out_root: Path) -> None:
        with capture_logs() as logs:
            report = _run_pass(out_root, self._two_arm_grid(), expected_missing=1)
        assert report.tuples_missing_store == 1
        assert not [
            log for log in logs if log["event"] == "results_depth.missing_store_unexplained"
        ]
        # The per-store INFO still fires (observability), just not the WARN.
        assert [log for log in logs if log["event"] == "results_depth.missing_store"]

    def test_default_none_treats_every_missing_as_unexplained(self, out_root: Path) -> None:
        with capture_logs() as logs:
            report = _run_pass(out_root, self._two_arm_grid())  # expected_missing default None
        assert report.tuples_missing_store == 1
        warns = [log for log in logs if log["event"] == "results_depth.missing_store_unexplained"]
        assert len(warns) == 1
        assert warns[0]["expected"] is None

    def test_zero_missing_emits_no_missing_events(self, out_root: Path) -> None:
        with capture_logs() as logs:
            report = _run_pass(out_root, _grid())
        assert report.tuples_missing_store == 0
        assert report.missing_store_sample == ()
        assert not [
            log
            for log in logs
            if log["event"]
            in ("results_depth.missing_store", "results_depth.missing_store_unexplained")
        ]

    def test_missing_store_info_capped_and_sampled(self) -> None:
        from brutex.engine.results_depth_pass import (
            _MISSING_STORE_LOG_CAP,
            _MISSING_STORE_SAMPLE_CAP,
            _note_missing_store,
        )

        tallies = _Tallies()
        with capture_logs() as logs:
            for i in range(_MISSING_STORE_LOG_CAP + 5):
                _note_missing_store(
                    Path(f"/lake/{i}.parquet"), tallies, no_bars=False, quarantined=False
                )
        infos = [log for log in logs if log["event"] == "results_depth.missing_store"]
        assert len(infos) == _MISSING_STORE_LOG_CAP  # INFO capped
        assert tallies.tuples_missing_store == _MISSING_STORE_LOG_CAP + 5  # count uncapped
        assert len(tallies.missing_store_sample) == _MISSING_STORE_SAMPLE_CAP  # sample capped

    def test_no_bars_note_shares_the_info_cap_and_never_samples(self) -> None:
        """Review round 1 M2: the no-bars class logs its OWN capped INFO
        (results_depth.missing_store_no_bars), shares the ONE log-volume cap
        with the bars-present class, counts missing_expected_no_bars, and is
        NEVER sampled (the sample feeds the actionable deletion WARN only)."""
        from brutex.engine.results_depth_pass import (
            _MISSING_STORE_LOG_CAP,
            _note_missing_store,
        )

        tallies = _Tallies()
        with capture_logs() as logs:
            for i in range(_MISSING_STORE_LOG_CAP + 3):
                _note_missing_store(
                    Path(f"/lake/nb-{i}.parquet"), tallies, no_bars=True, quarantined=False
                )
        infos = [log for log in logs if log["event"] == "results_depth.missing_store_no_bars"]
        assert len(infos) == _MISSING_STORE_LOG_CAP  # shared INFO cap
        assert tallies.tuples_missing_store == _MISSING_STORE_LOG_CAP + 3
        assert tallies.missing_expected_no_bars == _MISSING_STORE_LOG_CAP + 3
        assert tallies.missing_store_sample == []  # never sampled
        assert not [log for log in logs if log["event"] == "results_depth.missing_store"]

    def test_no_bars_missing_store_is_expected_never_unexplained(self, out_root: Path) -> None:
        """Review round 1 M2 — THE RESUME regression: a tuple whose bar slice
        is EMPTY is deterministically storeless (the sweep never writes a
        store for it — first run AND checkpoint resume alike). On a RESUMED
        run its skip folds into skipped_resume, so the run-scoped allowance is
        0 (modelled by expected_missing=0 here) — the pre-fix run-N-counter
        formula false-WARNed "partial store deletion" on that healthy resume.
        The deterministic classification fires NO unexplained WARN."""
        grid = _grid(instruments=["NIFTY", "BANKNIFTY"])
        with capture_logs() as logs:
            report = run_results_depth_pass(
                out_root=out_root,
                grid=grid,
                # BANKNIFTY was never swept (its slice is empty) — the no-bars
                # prefix tuple; NIFTY's store exists from the fixture sweep.
                bars_for=lambda instrument, _tf: _BARS if instrument == "NIFTY" else [],
                minute_bars_for=lambda _i: _BARS,
                expected_missing=0,
            )
        assert report.tuples_missing_store == 1
        assert report.missing_expected_no_bars == 1
        assert report.missing_store_unexplained == 0
        assert not [
            log for log in logs if log["event"] == "results_depth.missing_store_unexplained"
        ]
        infos = [log for log in logs if log["event"] == "results_depth.missing_store_no_bars"]
        assert len(infos) == 1
        assert report.missing_store_sample == ()  # no-bars misses are never sampled

    def test_genuine_deletion_still_warns_with_run_scoped_allowance(self, out_root: Path) -> None:
        """The M2 teeth twin (genuine-deletion-on-resume): a swept BARS-PRESENT
        store deleted out from under the tree still WARNs against the resume's
        run-scoped allowance (expected_missing=0) — the classification never
        masks a real deletion."""
        grid = _grid()
        store = tuple_store_path(out_root, next(iter_sweep_tuples(grid)), grid)
        store.unlink()
        with capture_logs() as logs:
            report = _run_pass(out_root, grid, expected_missing=0)
        assert report.tuples_missing_store == 1
        assert report.missing_expected_no_bars == 0
        assert report.missing_store_unexplained == 1
        warns = [log for log in logs if log["event"] == "results_depth.missing_store_unexplained"]
        assert len(warns) == 1
        assert warns[0]["missing_no_bars"] == 0
        assert store.name in str(warns[0]["sample"])

    def test_over_allowance_clamps_to_zero_never_negative(self, out_root: Path) -> None:
        """max(0, …): an allowance larger than the observed misses reports an
        honest 0 (never a negative unexplained count) and stays silent."""
        with capture_logs() as logs:
            report = _run_pass(out_root, self._two_arm_grid(), expected_missing=5)
        assert report.tuples_missing_store == 1
        assert report.missing_store_unexplained == 0
        assert not [
            log for log in logs if log["event"] == "results_depth.missing_store_unexplained"
        ]


class TestMissingStoreQuarantineIdentity:
    """DEC-DEPTH-MISSING-STORE-LOUD-001 (#1925, review round 2 M1): a
    quarantined-prior missing store is attributed BY IDENTITY (the sidecar
    label set), NOT by a stale COUNT — so a stale entry for a tuple whose store
    now EXISTS can never launder an UNRELATED deletion into a silent green run.
    Plus round-2 MUST-FIX 2: a raising ``bars_for`` on a missing store
    quarantines ONLY that tuple (never aborts the pass)."""

    def _two_symbol_swept(self, dest: Path) -> SweepGridConfig:
        dest.mkdir()
        grid = _grid(instruments=["NIFTY", "BANKNIFTY"])
        _run_sweep(dest, grid, _BARS)
        return grid

    def test_stale_quarantine_identity_never_masks_a_deletion(self, tmp_path: Path) -> None:
        """THE round-2 M1 refuter repro: a STALE quarantine entry for tuple T
        (whose store still EXISTS) + a deleted UNRELATED tuple D (bars present)
        → the WARN FIRES naming D. The pre-fix count-based code
        (``expected_missing = tuples_failed + tuples_quarantined_prior``, a
        caller passing an allowance of 1) was SILENT here: missing(1) −
        allowance(1) = 0."""
        from brutex.engine.sweep_supervisor import tuple_label

        dest = tmp_path / "sweeps"
        grid = self._two_symbol_swept(dest)
        tuples = list(iter_sweep_tuples(grid))
        d_tuple, t_tuple = tuples[0], tuples[1]
        d_store = tuple_store_path(dest, d_tuple, grid)
        t_store = tuple_store_path(dest, t_tuple, grid)
        assert d_store.exists() and t_store.exists()  # both swept
        d_store.unlink()  # the genuine deletion
        with capture_logs() as logs:
            report = _run_pass(
                dest,
                grid,
                expected_missing=0,  # FAILED-only allowance; no failures this run
                quarantined_labels=frozenset({tuple_label(t_tuple)}),  # STALE: T exists
            )
        assert report.tuples_missing_store == 1
        # The stale identity matched NOTHING (T is not missing), so it granted
        # ZERO allowance — the deletion of D is loudly unexplained.
        assert report.missing_expected_quarantined == 0
        assert report.missing_store_unexplained == 1
        warns = [log for log in logs if log["event"] == "results_depth.missing_store_unexplained"]
        assert len(warns) == 1
        assert warns[0]["missing_quarantined"] == 0
        assert d_store.name in str(warns[0]["sample"])

    def test_quarantined_identity_explains_a_genuinely_missing_tuple(self, tmp_path: Path) -> None:
        """The complement: when the MISSING tuple's own identity is in the
        sidecar set, it is explained (``missing_expected_quarantined``) — a
        capped INFO, NEVER the unexplained WARN."""
        from brutex.engine.sweep_supervisor import tuple_label

        dest = tmp_path / "sweeps"
        grid = self._two_symbol_swept(dest)
        d_tuple = next(iter_sweep_tuples(grid))
        d_store = tuple_store_path(dest, d_tuple, grid)
        d_store.unlink()
        with capture_logs() as logs:
            report = _run_pass(
                dest,
                grid,
                expected_missing=0,
                quarantined_labels=frozenset({tuple_label(d_tuple)}),
            )
        assert report.tuples_missing_store == 1
        assert report.missing_expected_quarantined == 1
        assert report.missing_store_unexplained == 0
        assert not [
            log for log in logs if log["event"] == "results_depth.missing_store_unexplained"
        ]
        infos = [log for log in logs if log["event"] == "results_depth.missing_store_quarantined"]
        assert len(infos) == 1
        assert report.missing_store_sample == ()  # quarantined misses are never sampled

    def test_note_missing_store_quarantined_shares_the_info_cap(self) -> None:
        from brutex.engine.results_depth_pass import (
            _MISSING_STORE_LOG_CAP,
            _note_missing_store,
        )

        tallies = _Tallies()
        with capture_logs() as logs:
            for i in range(_MISSING_STORE_LOG_CAP + 4):
                _note_missing_store(
                    Path(f"/lake/q-{i}.parquet"), tallies, no_bars=False, quarantined=True
                )
        infos = [log for log in logs if log["event"] == "results_depth.missing_store_quarantined"]
        assert len(infos) == _MISSING_STORE_LOG_CAP  # shared INFO cap
        assert tallies.missing_expected_quarantined == _MISSING_STORE_LOG_CAP + 4
        assert tallies.missing_store_sample == []  # never sampled
        assert not [log for log in logs if log["event"] == "results_depth.missing_store"]

    def test_raising_bars_for_on_missing_store_quarantines_one_tuple(self, tmp_path: Path) -> None:
        """Round-2 MUST-FIX 2: the ``_bars`` no-bars classification runs INSIDE
        the per-tuple try, so a lake-reading ``bars_for`` that RAISES on the
        missing tuple quarantines ONLY that tuple (counted ``tuples_errored`` +
        logged) and the pass keeps going — the sibling with a present store is
        still processed. The pre-fix code had ``_bars`` OUTSIDE the try, so the
        raise aborted the WHOLE pass."""

        class _Boom(Exception):
            pass

        dest = tmp_path / "sweeps"
        grid = self._two_symbol_swept(dest)
        bnf_tuple = next(t for t in iter_sweep_tuples(grid) if t.instrument == "BANKNIFTY")
        tuple_store_path(dest, bnf_tuple, grid).unlink()  # BANKNIFTY store missing

        def _raising_bars_for(instrument: str, _tf: str) -> list[CandleBar]:
            if instrument == "BANKNIFTY":
                raise _Boom("lake read failed for the missing tuple")
            return _BARS

        with capture_logs() as logs:
            report = run_results_depth_pass(
                out_root=dest,
                grid=grid,
                bars_for=_raising_bars_for,
                minute_bars_for=lambda _i: _BARS,
                expected_missing=0,
            )
        # The raising bars_for did NOT abort the pass: BANKNIFTY quarantined
        # (counted errored), NIFTY still processed.
        assert report.tuples_errored == 1
        assert report.tuples_processed >= 1
        # It raised during classification, so the missing store was never
        # tallied as missing (it is an errored tuple instead).
        assert report.tuples_missing_store == 0
        errs = [log for log in logs if log["event"] == "results_depth.tuple_error"]
        assert len(errs) == 1
        assert errs[0]["error_type"] == "_Boom"


class TestBarsSha256:
    """DEC-DEPTH-STAMP-REFDATA-001 (#1926): the full-content reference-bar digest."""

    def test_identical_streams_match(self) -> None:
        from brutex.engine.results_depth_pass import _bars_sha256

        assert _bars_sha256(_BARS) == _bars_sha256(list(_BARS))

    def test_absent_series_folds_a_stable_token(self) -> None:
        from brutex.engine.results_depth_pass import _bars_sha256

        assert _bars_sha256(()) == _bars_sha256([])
        assert _bars_sha256(()) != _bars_sha256(_BARS)

    def test_interior_change_moves_the_digest(self) -> None:
        """A single interior bar mutation (SAME count/endpoints) moves the digest —
        the property that kills a sampled count+endpoints fingerprint mutant."""
        from dataclasses import replace

        from brutex.engine.results_depth_pass import _bars_sha256

        mid = len(_BARS) // 2
        mutated = list(_BARS)
        mutated[mid] = replace(mutated[mid], close=mutated[mid].close + Decimal("0.01"))
        assert _bars_sha256(mutated) != _bars_sha256(_BARS)
        # endpoints unchanged, count unchanged — only an interior bar moved.
        assert mutated[0] == _BARS[0]
        assert mutated[-1] == _BARS[-1]
        assert len(mutated) == len(_BARS)

    def test_open_interest_is_covered(self) -> None:
        from dataclasses import replace

        from brutex.engine.results_depth_pass import _bars_sha256

        with_oi = [replace(_BARS[0], open_interest=42)]
        without_oi = [replace(_BARS[0], open_interest=None)]
        assert _bars_sha256(with_oi) != _bars_sha256(without_oi)

    def test_naive_timestamp_fails_loud(self) -> None:
        """Review round 1 item 7: a tz-NAIVE timestamp must raise — a naive
        ``astimezone(UTC)`` would resolve via the MACHINE's local zone, making
        the digest (and the resume decision) silently host-dependent."""
        from brutex.engine.results_depth_pass import _bar_ts_us

        with pytest.raises(ValueError, match="naive bar timestamp"):
            _bar_ts_us(datetime(2026, 1, 5, 9, 16))
        # The tz-aware path stays exact-integer µs (no float rounding).
        assert _bar_ts_us(datetime(1970, 1, 1, 0, 0, 1, 500, tzinfo=UTC)) == 1_000_500


class TestReferenceFingerprintRedo:
    """DEC-DEPTH-STAMP-REFDATA-001 (#1926): a reference-data change forces a redo;
    unchanged refs + store still skip; an old field-less stamp re-derives once."""

    def test_reference_change_forces_redo(self, out_root: Path) -> None:
        # VECTOR 8b: pass 1 with NO VIX stamps the tuple; pass 2 with REAL VIX,
        # store bytes UNCHANGED, must re-derive (not skip) and stamp VIX.
        first = _run_pass(out_root, _grid(), vix=False)
        assert first.tuples_processed == 1
        with capture_logs() as logs:
            second = _run_pass(out_root, _grid(), vix=True)
        assert second.tuples_skipped_current == 0
        assert second.tuples_processed == 1
        redo = [log for log in logs if log["event"] == "results_depth.tuple_redo_reference_changed"]
        assert len(redo) == 1
        # The re-derived trades now carry real VIX stamps (not the pass-1 NULLs).
        import polars as pl

        trades = pl.read_parquet(_tuple_paths(out_root, _grid()).trades)
        assert trades.get_column("vix_entry").drop_nulls().len() > 0

    def test_unchanged_refs_and_store_still_skip(self, out_root: Path) -> None:
        _run_pass(out_root, _grid(), vix=True)
        paths = _tuple_paths(out_root, _grid())
        before = {p: p.read_bytes() for p in (paths.trades, paths.aggs, paths.equity)}
        report = _run_pass(out_root, _grid(), vix=True)
        assert report.tuples_skipped_current == 1
        assert report.tuples_processed == 0
        # The sidecar trio is byte-identical — a skip rewrites nothing (O(1) resume).
        for p, data in before.items():
            assert p.read_bytes() == data

    def test_old_field_absent_stamp_re_derives_once(self, out_root: Path) -> None:
        import json

        _run_pass(out_root, _grid(), vix=True)
        stamp_path = _tuple_paths(out_root, _grid()).stamp
        payload = json.loads(stamp_path.read_text(encoding="utf-8"))
        assert "reference_fingerprint" in payload  # a fresh stamp carries the field
        # Simulate a PRE-feature stamp: strip the key entirely.
        del payload["reference_fingerprint"]
        stamp_path.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
        )
        stamp = load_stamp(_tuple_paths(out_root, _grid()))
        assert stamp is not None
        assert stamp.reference_fingerprint is None  # tolerant parse
        report = _run_pass(out_root, _grid(), vix=True)
        assert report.tuples_processed == 1  # re-derived, never a fabricated match
        # The rewritten stamp now carries the field.
        assert load_stamp(_tuple_paths(out_root, _grid())).reference_fingerprint is not None  # type: ignore[union-attr]

    def test_provider_flag_flip_forces_redo(self, out_root: Path) -> None:
        _run_pass(out_root, _grid(), vix=True)
        # Flip the reasons provider None → wired: sidecar CONTENT changes, so the
        # fingerprint moves and the tuple re-derives.
        report = run_results_depth_pass(
            out_root=out_root,
            grid=_grid(),
            bars_for=lambda _i, _tf: _BARS,
            minute_bars_for=lambda _i: _BARS,
            daily_bars_for=None,
            vix_bars=_BARS,
            reason_provider=lambda _c: {"reason": "x"},
        )
        assert report.tuples_processed == 1

    def test_minute_bars_change_re_derives_only_that_instrument(self, tmp_path: Path) -> None:
        """Granularity pin: a per-instrument fingerprint means a one-instrument
        minute-bars change re-derives ONLY that instrument's tuples."""
        grid = _grid(instruments=["NIFTY", "BANKNIFTY"])
        out_root = tmp_path / "sweeps"
        out_root.mkdir()
        _run_sweep(out_root, grid, _BARS)

        def _run(minute_for: object) -> ResultsDepthReport:
            return run_results_depth_pass(
                out_root=out_root,
                grid=grid,
                bars_for=lambda _i, _tf: _BARS,
                minute_bars_for=minute_for,  # type: ignore[arg-type]
                daily_bars_for=None,
                vix_bars=None,
            )

        first = _run(lambda _i: _BARS)
        assert first.tuples_processed == 2  # both instruments stamped
        # Change ONLY NIFTY's minute bars (drop the last bar) → only NIFTY moves.
        nifty_bars = list(_BARS[:-1])

        def _minute(instrument: str) -> Sequence[CandleBar]:
            return nifty_bars if instrument == "NIFTY" else _BARS

        with capture_logs() as logs:
            second = _run(_minute)
        assert second.tuples_processed == 1  # exactly one instrument re-derived
        assert second.tuples_skipped_current == 1
        redo = [log for log in logs if log["event"] == "results_depth.tuple_redo_reference_changed"]
        assert len(redo) == 1
        assert "NIFTY" in redo[0]["store"]
        assert "BANKNIFTY" not in redo[0]["store"]

    def test_two_identical_passes_are_deterministic(self, out_root: Path) -> None:
        """The determinism golden: two identical passes → byte-identical trio +
        equal fingerprint (produced_at_utc stays the ONE non-deterministic field)."""
        _run_pass(out_root, _grid(), vix=True)
        paths = _tuple_paths(out_root, _grid())
        fp1 = load_stamp(paths).reference_fingerprint  # type: ignore[union-attr]
        trio1 = {p: p.read_bytes() for p in (paths.trades, paths.aggs, paths.equity)}
        # A second pass skips (current), so a fresh REBUILD is forced to compare
        # bytes: strip the stamp so the trio is re-derived from scratch.
        paths.stamp.unlink()
        _run_pass(out_root, _grid(), vix=True)
        fp2 = load_stamp(paths).reference_fingerprint  # type: ignore[union-attr]
        assert fp1 == fp2
        for p, data in trio1.items():
            assert p.read_bytes() == data


# ── WORST_CASE capture chain (DEC-FILL-WORST-CASE-ONLY-001 fix round 3) ────────────


def _worst_case_ranking_bars(n_days: int = 15, per_day: int = 120) -> list[CandleBar]:
    """The `test_engine_sweep_run` worst-case RANKING fixture, verbatim.

    Rises 1.0 with a 0.5 dip every 4th bar under a tight ±0.05 pad — strong
    enough that combos rank + persist under the WORST_CASE adverse-max floor,
    with SL/TP trigger timing diverging HARD between the modes (the
    observable the parity tooth keys on).
    """
    bars: list[CandleBar] = []
    for d in range(n_days):
        price = 100.0
        for m in range(per_day):
            close = price + 1.0 if m % 4 else price - 0.5
            ts = (
                datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=d, minutes=m)
            ).astimezone(UTC)
            hi = max(price, close) + 0.05
            lo = min(price, close) - 0.05
            bars.append(
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
    return bars


class TestWorstCaseCaptureChain:
    """DEC-FILL-WORST-CASE-ONLY-001: the capture chain runs the ONE hardcoded
    worst-case law end-to-end — `results_depth_pass._process_tuple` →
    `capture_combo` → the walker re-sim reproduce a worst-case store with
    ZERO parity failures, and the mode seams are GONE (killed-token pins).
    The anti-vacuity check proves the captured entry genuinely fills at the
    entry bar's adverse extreme (BUY HIGH), never its open.
    """

    def test_one_law_pass_captures_with_parity(self, tmp_path: Path) -> None:
        bars = _worst_case_ranking_bars()
        grid = _grid(stop_losses=[200], take_profits=[100])
        root = tmp_path / "sweeps"
        root.mkdir()
        _run_sweep(root, grid, bars)
        report = run_results_depth_pass(
            out_root=root,
            grid=grid,
            bars_for=lambda _instrument, _tf: bars,
            minute_bars_for=lambda _i: bars,
        )
        # The chain is EXERCISED, not vacuously green: finalists persisted,
        # every one captured, ZERO parity failures.
        assert report.combos_captured >= 1
        assert report.combos_parity_failed == 0

        # Killed-token pins: no fill_mode/policy seam survives on the chain.
        import inspect

        from brutex.engine.combo_mask import mask_to_entry_signals
        from brutex.engine.stage1a import build_bar_bits
        from brutex.engine.trade_capture import capture_combo
        from brutex.engine.trade_sim import simulate_trades

        assert "fill_mode" not in inspect.signature(capture_combo).parameters
        assert "policy" not in inspect.signature(simulate_trades).parameters

        # Anti-vacuity: the captured first trade's entry fill IS the entry
        # bar's HIGH (the adverse extreme for a BUY) — under any open-fill
        # regression this equality breaks.
        (tuple_,) = iter_sweep_tuples(grid)
        rows = ResultsStore.load(tuple_store_path(root, tuple_, grid)).rows()
        assert rows
        row = rows[0]
        bar_bits = build_bar_bits(bars)
        capture = capture_combo(
            row,
            bars=bars,
            bar_bits=bar_bits,
            tf_minutes=1,
            sl_paisa=200,
            tp_paisa=100,
            exec_bars_1m=bars,  # tf==1m fixed point — same list, both streams
            minute_index=None,
            stamper=None,
        )
        assert capture.parity_ok is True
        assert capture.trades
        detail_sink: list[object] = []
        simulate_trades(
            bars,
            mask_to_entry_signals(bar_bits, row.mask_bits),
            direction=Side.BUY,
            tf_minutes=1,
            exec_bars_1m=bars,  # tf==1m fixed point — same list, both streams
            stop_loss_paisa=200,
            take_profit_paisa=100,
            detail_sink=detail_sink,  # type: ignore[arg-type]
        )
        assert detail_sink
        first = detail_sink[0]
        assert capture.trades[0].entry_px == Decimal(first.entry_fill_paisa) / 100  # type: ignore[attr-defined]
        # The fill is the entry bar's HIGH — locate the entry bar by ts.
        entry_ts = capture.trades[0].entry_ts
        (entry_bar,) = [b for b in bars if b.timestamp == entry_ts]
        assert capture.trades[0].entry_px == entry_bar.high


# ── Exec-provider + fill-sensitivity threading (DEC-OPTIONS-EXEC-FEEDBACK-001 /
#    DEC-FILL-EXEC-GRID-1M-001, R13) ─────────────────────────────────────────────


class TestExecProviderAndFillSensitivityThreading:
    """The provider kwargs thread VERBATIM; ``fill_sensitivity=True`` builds the
    resolver over the pass's own caches — the default arm stays byte-identical
    (no additive keys, ``None`` providers into :func:`build_top100`)."""

    @staticmethod
    def _spy_build(monkeypatch: pytest.MonkeyPatch, seen: dict[str, object]) -> None:
        real_build = results_depth_pass.build_top100

        def _spy(*args: object, **kwargs: object) -> object:
            for key in (
                "options_exec_provider",
                "futures_exec_provider",
                "fill_sensitivity_provider",
            ):
                seen[key] = kwargs.get(key)
            return real_build(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(results_depth_pass, "build_top100", _spy)

    def test_default_threads_none_and_keeps_the_prechange_artifact_shape(
        self, out_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, object] = {}
        self._spy_build(monkeypatch, seen)
        _run_pass(out_root)
        assert seen["options_exec_provider"] is None
        assert seen["futures_exec_provider"] is None
        assert seen["fill_sensitivity_provider"] is None
        run_id = grid_identity(_grid())
        loaded = load_top100(sweep_run_dir(out_root, run_id).parent, run_id)
        assert loaded is not None
        assert loaded.entries
        # The pre-change artifact SHAPE: the emit-on-wired keys are ABSENT on
        # every entry AND on the source header (absence == not wired — the
        # additive-key law that keeps provider-None artifact bytes identical).
        for entry in loaded.entries:
            assert "options_exec" not in entry
            assert "futures_exec" not in entry
            assert "fill_sensitivity" not in entry
        for flag in ("options_exec_wired", "futures_exec_wired", "fill_sensitivity_wired"):
            assert flag not in loaded.source

    def test_wired_providers_thread_verbatim_and_annotate_every_ranked_entry(
        self, out_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.engine.results_depth_pass import _FillSensitivityResolver
        from brutex.engine.top100_artifact import Top100Candidate

        def _options(_candidate: Top100Candidate) -> dict[str, object]:
            return {"status": "not_priced", "reason": "sentinel_options"}

        def _futures(_candidate: Top100Candidate) -> dict[str, object]:
            return {"status": "not_priced", "reason": "sentinel_futures"}

        seen: dict[str, object] = {}
        self._spy_build(monkeypatch, seen)
        _run_pass(
            out_root,
            options_exec_provider=_options,
            futures_exec_provider=_futures,
            fill_sensitivity=True,
        )
        # Threaded VERBATIM — the exact callables, identity not copies.
        assert seen["options_exec_provider"] is _options
        assert seen["futures_exec_provider"] is _futures
        resolver = seen["fill_sensitivity_provider"]
        assert isinstance(resolver, _FillSensitivityResolver)
        run_id = grid_identity(_grid())
        loaded = load_top100(sweep_run_dir(out_root, run_id).parent, run_id)
        assert loaded is not None
        assert loaded.entries
        for entry in loaded.entries:
            assert entry["options_exec"] == {"status": "not_priced", "reason": "sentinel_options"}
            assert entry["futures_exec"] == {"status": "not_priced", "reason": "sentinel_futures"}
            # The REAL resolver re-simulated the fixture finalist (parity-OK on
            # this fixture) and PRICED the fold — the haircut arithmetic law.
            verdict = entry["fill_sensitivity"]
            assert isinstance(verdict, dict)
            assert verdict["status"] == "priced"
            assert verdict["reference_basis"] == "open_anchored"
            assert verdict["n_trades"] > 0
            assert (
                verdict["haircut_paisa"]
                == verdict["reference_net_pnl_paisa"] - verdict["worst_case_net_pnl_paisa"]
            )
            # Dominance on REAL walker output (round-1 review fold): the
            # worst-case law never overstates the open-anchored reference,
            # so the shipped haircut is >= 0 here — the fold never guards.
            haircut = verdict["haircut_paisa"]
            assert isinstance(haircut, int)
            assert haircut >= 0
        assert loaded.source["fill_sensitivity_wired"] is True
        # The captured resolver degrades HONESTLY on an out-of-band candidate
        # (a run_id no pooled tuple recorded) — the named reason, never a crash.
        from tests.fakes.wp5_depth_fixtures import make_candidate

        stray = make_candidate(999, fingerprint="f" * 16)
        assert resolver(stray) == {"status": "not_priced", "reason": "no_replay_context"}


class TestFillSensitivityResolver:
    """`_FillSensitivityResolver` direct unit arms (DEC-FILL-EXEC-GRID-1M-001):
    every NAMED degrade, the global-stop re-raise law, and the per-tuple memos."""

    @staticmethod
    def _context(minute_bars: Sequence[CandleBar]) -> object:
        from brutex.engine.results_depth_pass import _InstrumentContext
        from brutex.engine.session_lens import build_session_lens

        return _InstrumentContext(
            minute_index=None,
            minute_bars=tuple(minute_bars),
            lens=build_session_lens((), instrument="NIFTY"),
        )

    def _resolver(
        self,
        *,
        mapped_run_id: str | None,
        minute_bars: Sequence[CandleBar],
        bars_of: object,
    ) -> object:
        from brutex.engine.results_depth_pass import _FillSensitivityResolver

        grid = _grid()
        (tuple_,) = iter_sweep_tuples(grid)
        tuple_of = {} if mapped_run_id is None else {mapped_run_id: tuple_}
        context = self._context(minute_bars)
        return _FillSensitivityResolver(
            bars_of=bars_of,  # type: ignore[arg-type]
            context_of=lambda _instrument: context,  # type: ignore[arg-type, return-value]
            tuple_of=tuple_of,
            grid=grid,
        )

    @staticmethod
    def _candidate(n: int) -> object:
        from tests.fakes.wp5_depth_fixtures import make_candidate

        return make_candidate(n, fingerprint="ab" * 8)

    def test_unmapped_run_id_degrades_no_replay_context(self) -> None:
        def _never(*_args: object) -> object:
            raise AssertionError("must not touch bars for an unmapped candidate")

        resolver = self._resolver(mapped_run_id=None, minute_bars=_BARS, bars_of=_never)
        assert resolver(self._candidate(7)) == {  # type: ignore[operator]
            "status": "not_priced",
            "reason": "no_replay_context",
        }

    def test_empty_minute_stream_degrades_no_1m_data(self) -> None:
        from tests.fakes.wp5_depth_fixtures import run_id_of as _rid

        def _never(*_args: object) -> object:
            raise AssertionError("must not load bars without a 1m stream")

        resolver = self._resolver(mapped_run_id=_rid(7), minute_bars=(), bars_of=_never)
        assert resolver(self._candidate(7)) == {  # type: ignore[operator]
            "status": "not_priced",
            "reason": "no_1m_data",
        }

    def test_memory_error_re_raises_raw(self) -> None:
        from tests.fakes.wp5_depth_fixtures import run_id_of as _rid

        def _oom(*_args: object) -> object:
            raise MemoryError

        resolver = self._resolver(mapped_run_id=_rid(7), minute_bars=_BARS, bars_of=_oom)
        with pytest.raises(MemoryError):
            resolver(self._candidate(7))  # type: ignore[operator]

    def test_resim_failure_degrades_named_and_logged(self) -> None:
        from tests.fakes.wp5_depth_fixtures import run_id_of as _rid

        def _boom(*_args: object) -> object:
            raise ValueError("planted re-sim fault")

        resolver = self._resolver(mapped_run_id=_rid(7), minute_bars=_BARS, bars_of=_boom)
        with capture_logs() as logs:
            verdict = resolver(self._candidate(7))  # type: ignore[operator]
        assert verdict == {"status": "not_priced", "reason": "resim_failed"}
        assert any(log["event"] == "results_depth.fill_sensitivity_resim_failed" for log in logs)

    def test_parity_mismatch_degrades_and_memos_are_per_tuple(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        from tests.fakes.wp5_depth_fixtures import run_id_of as _rid

        calls = {"capture": 0, "bars": 0}

        def _bars_of(_instrument: str, _tf_label: str) -> Sequence[CandleBar]:
            calls["bars"] += 1
            return _BARS

        def _fake_capture(*_args: object, **_kwargs: object) -> object:
            calls["capture"] += 1
            return SimpleNamespace(parity_ok=False)

        monkeypatch.setattr(results_depth_pass, "capture_combo", _fake_capture)
        resolver = self._resolver(mapped_run_id=_rid(7), minute_bars=_BARS, bars_of=_bars_of)
        expected = {"status": "not_priced", "reason": "resim_parity_mismatch"}
        assert resolver(self._candidate(7)) == expected  # type: ignore[operator]
        # A second resolve for the SAME tuple hits BOTH memos (bar_bits + the
        # exec map are mask-independent) — no rebuild, identical verdict.
        assert resolver(self._candidate(7)) == expected  # type: ignore[operator]
        assert calls["capture"] == 2
        assert calls["bars"] == 2
