"""Unit tests for the Stage-3 launcher wiring (`brutex.engine.stage3_wire`).

100% line + branch for the strict per-module gate (Golden Rule #12), built to
the DEC-STAGE3-WIRE-001 hostile-review conditions:

* **F1 / condition 7 + Amendment A1** — :func:`train_slice_bars` mirrors the
  lockbox day-ordinal window EXACTLY (cross-checked against
  :func:`brutex.metrics.lockbox.lockbox_evaluation` itself), and every refined
  row's run-identity re-derives with the TRAIN slice's content hash.
* **F4** — survivor cells are recovered by run-identity match over the grid's
  (sl × tp × proximity) axes, never guessed; an unmatchable row raises.
* **F3** — refined rows land in their OWN per-quantized-cell Parquet files,
  disjoint from every Stage-2 grid file; ``k = popcount(mask)``.
* **F8** — refined-cell cascade specs carry every refined-eval row UNTRIMMED.
* **F10** — the pass brackets its per-mask STAGE_3_REFINE_* triples with
  STAGE_3_PASS_START / STAGE_3_PASS_COMPLETE on one hash-verified chain.
* **F11** — the A/B determinism oracle: two full enabled runs (grid sweep +
  Stage-3 pass) over the synthetic bar fixture produce byte-identical Parquet
  trees (tree only; audit timestamps excluded), and a re-run on the same tree
  is an idempotent no-op (Golden Rule #7).

The full-pipeline fixture is computed ONCE per module (the sweep + pass are
O(bars)-per-eval offline work, Golden Rule #10 — no O(1) claim anywhere here).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.cascade_wire import Stage3ManifestRecord
from brutex.engine.results_store import ResultRow, ResultsStore
from brutex.engine.stage1a import build_bar_bits
from brutex.engine.stage3_wire import (
    STAGE3_ACTION_PASS_COMPLETE,
    STAGE3_ACTION_PASS_START,
    Stage3PassReport,
    Stage3RefinedCell,
    Stage3WireError,
    _cell_sort_key,
    _data_snapshot_hash_v1,
    _side_for_direction,
    _timeframe_for_label,
    cascade_specs_for_refined_cells,
    data_snapshot_hash,
    ensure_stage3_runtime,
    recover_survivor_cell,
    run_stage3_pass,
    stage3_manifest_record,
    train_slice_bars,
)
from brutex.engine.sweep_driver import metrics_for_mask
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    SweepTuple,
    Timeframe,
    iter_sweep_tuples,
    make_store_for,
    run_sweep_grid,
    tuple_store_path,
)
from brutex.engine.sweep_run import ComboDedup, canonical_trade_params, mask_run_id
from brutex.metrics.lockbox import LOCKBOX_DEFAULT_WINDOW_DAYS, lockbox_evaluation
from brutex.orchestrator.audit import AuditWriter, read_entries, verify_chain
from brutex.strategies.types import (
    DEFAULT_PRICE_SOURCE_CELL,
    FibAnchorKind,
    PriceSource,
    PriceSourceCell,
    Side,
)
from brutex.vocabulary import ProximityParams

if TYPE_CHECKING:
    from brutex.metrics.compute import ResultMetrics

pytest.importorskip("cma")  # the optimize extra — installed in the CI unit job

_IST = timezone(timedelta(hours=5, minutes=30))

#: The grid identity context shared by every fixture grid.
_CTX: dict[str, str] = {
    "mode": "A",
    "data_snapshot_hash": "snap0001",
    "indicator_version": "ind-v1",
    "code_commit_sha": "deadbeef",
}
#: The bars-context scope the pass hashes its train slices with.
_SCOPE: dict[str, str] = {
    "exchange": "NSE",
    "segment": "INDEX",
    "start": "2026-01-01",
    "end": "2026-12-31",
}


def _bar(day: int, minute: int, o: float, c: float) -> CandleBar:
    """Build one IST-session minute bar (stored UTC) with Decimal OHLC."""
    ts = (
        datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=day, minutes=minute)
    ).astimezone(UTC)
    hi = max(o, c) + 0.5
    lo = min(o, c) - 0.5
    return CandleBar(
        timestamp=ts,
        open=Decimal(str(round(o, 2))),
        high=Decimal(str(round(hi, 2))),
        low=Decimal(str(round(lo, 2))),
        close=Decimal(str(round(c, 2))),
        volume=100,
        open_interest=None,
    )


def _trending_bars(n_days: int, per_day: int = 30) -> list[CandleBar]:
    """A multi-day upward-drifting session — BUY combos win often via the TP leg."""
    bars: list[CandleBar] = []
    for d in range(n_days):
        price = 100.0
        for m in range(per_day):
            close = price + 0.6 if m % 3 else price - 0.3
            bars.append(_bar(d, m, price, close))
            price = close
    return bars


def _grid(**overrides: object) -> SweepGridConfig:
    """A small Stage-3-enabled grid: 2 tuples (k=0 enter-every-bar + k=1).

    Bit 1 (bar-bearish entry) is the swept condition because BOTH masks (the
    k=0 enter-every-bar mask and the k=1 bearish-dip mask) clear the hard
    rank gates on the trending fixture — two leaderboard survivors exercise
    the per-proximity ``bar_bits`` memo's hit arm.
    """
    base: dict[str, object] = {
        "instruments": ["NIFTY"],
        "timeframes": [Timeframe("1minute", 1)],
        "directions": [Side.BUY],
        "k_values": [0, 1],
        "condition_bits": [1],
        "stop_losses": [80],
        "take_profits": [50],
        "proximities": [None],
        "top_k": 2,
        "stage3_enable": True,
        "stage3_top_k": 2,
        "stage3_max_fevals": 6,
        "stage3_max_restarts": 1,
        **_CTX,
    }
    base.update(overrides)
    return SweepGridConfig(**base)  # type: ignore[arg-type]


def _dedup() -> ComboDedup:
    return ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))


def _run_full(out_root: Path, bars: list[CandleBar], grid: SweepGridConfig) -> Stage3PassReport:
    """One full enabled run: grid sweep → Stage-3 pass, one audit chain."""

    def bars_for(_instrument: str, _tf_label: str) -> list[CandleBar]:
        return bars

    audit_path = out_root / "audit.jsonl"
    with AuditWriter.open(audit_path, resume=audit_path.exists()) as audit:
        run_sweep_grid(
            grid,
            bars_for=bars_for,
            store_for=make_store_for(out_root, grid),
            dedup=_dedup(),
            audit=audit,
        )
        return run_stage3_pass(
            grid,
            results_root=out_root,
            bars_for=bars_for,
            audit=audit,
            exchange=_SCOPE["exchange"],
            segment=_SCOPE["segment"],
            start=_SCOPE["start"],
            end=_SCOPE["end"],
        )


def _tree_hashes(root: Path) -> dict[str, str]:
    """Map every results-tree artifact under *root* (relative path) to its SHA-256.

    Covers the per-tuple Parquet stores, their trial-stats sidecars
    (DEC-TRIAL-STATS-WRITE-001), AND their completion sentinels
    (DEC-MARK-MANY-SENTINEL-001) — the F11 byte-identity contract extends to
    all three; audit/checkpoint/progress files stay excluded (timestamps).
    """
    artifacts = (
        sorted(root.glob("**/*.parquet"))
        + sorted(root.glob("**/*.trials.json"))
        + sorted(root.glob("**/*.complete.json"))
    )
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(artifacts)
    }


#: Window long enough that the train slice keeps ~8 leading days.
_N_DAYS = LOCKBOX_DEFAULT_WINDOW_DAYS + 8


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Stage3PassReport]:
    """One full enabled run, shared by the read-only assertions below."""
    out_root = tmp_path_factory.mktemp("stage3-pipeline")
    report = _run_full(out_root, _trending_bars(_N_DAYS), _grid())
    return out_root, report


# ────── train_slice_bars (review F1 / ADR-0007 condition 7) ──────


class TestTrainSliceBars:
    def test_empty_bars_yield_empty_slice(self) -> None:
        assert train_slice_bars([]) == ()

    def test_window_no_longer_than_lockbox_yields_empty_slice(self) -> None:
        bars = [_bar(d, 0, 100.0, 101.0) for d in range(LOCKBOX_DEFAULT_WINDOW_DAYS)]
        assert train_slice_bars(bars) == ()

    def test_boundary_is_exactly_the_lockbox_default_start(self) -> None:
        # 184 daily bars (days 0..183): lockbox start = 183 - 182 + 1 = day 2,
        # so the train slice keeps days 0 and 1 ONLY.
        bars = [_bar(d, 0, 100.0, 101.0) for d in range(184)]
        train = train_slice_bars(bars)
        assert [b.timestamp for b in train] == [b.timestamp for b in bars[:2]]

    def test_mirrors_lockbox_evaluation_window_exactly(self) -> None:
        # The shared-constant proof: lockbox_evaluation's own window_start_day
        # (defaults, floor satisfied) equals the first day ordinal the train
        # slice EXCLUDES — same boundary, same constant, no re-derivation.
        bars = [_bar(d, 0, 100.0, 101.0) for d in range(200)]
        days = [b.timestamp.astimezone(_IST).date().toordinal() for b in bars]
        result = lockbox_evaluation(
            [0.01] * len(days),
            days,
            training_sharpe=0.0,
            training_max_drawdown=0.0,
            min_trades=1,
        )
        train = train_slice_bars(bars)
        train_days = [b.timestamp.astimezone(_IST).date().toordinal() for b in train]
        assert train_days == [d for d in days if d < result.window_start_day]

    def test_non_chronological_bars_rejected(self) -> None:
        bars = [_bar(5, 0, 100.0, 101.0), _bar(1, 0, 100.0, 101.0)]
        with pytest.raises(Stage3WireError, match="chronological"):
            train_slice_bars(bars)


# ────── data_snapshot_hash (shared non-CLI helper, Amendment A1) ──────


class TestDataSnapshotHash:
    def test_deterministic_and_64char_hex(self) -> None:
        slices = {("NIFTY", "1minute"): _trending_bars(2)}
        a = data_snapshot_hash(**_SCOPE, slices=slices)
        b = data_snapshot_hash(**_SCOPE, slices=slices)
        assert a == b
        assert len(a) == 64 and a == a.lower()

    def test_content_change_changes_the_hash(self) -> None:
        bars = _trending_bars(2)
        a = data_snapshot_hash(**_SCOPE, slices={("NIFTY", "1minute"): bars})
        b = data_snapshot_hash(**_SCOPE, slices={("NIFTY", "1minute"): bars[:-1]})
        assert a != b

    def test_empty_slice_participates_as_zero_entry(self) -> None:
        a = data_snapshot_hash(**_SCOPE, slices={("NIFTY", "1minute"): []})
        b = data_snapshot_hash(**_SCOPE, slices={("BANKNIFTY", "1minute"): []})
        assert a != b

    def test_cli_launcher_delegates_to_this_helper(self) -> None:
        # §11 COMMON pin: the launcher's _data_snapshot_hash IS this derivation.
        from brutex.cli import _data_snapshot_hash

        slices = {("NIFTY", "1minute"): _trending_bars(2)}
        assert _data_snapshot_hash(**_SCOPE, slices=slices) == data_snapshot_hash(
            **_SCOPE, slices=slices
        )


# ────── _data_snapshot_hash_v1 (RETIRED sampled diagnostic) ──────


class TestDataSnapshotHashV1Legacy:
    """The RETIRED v1 sampled derivation — recount DIAGNOSTIC only, never identity.

    Kept module-private for ``sweep-verify --recount``'s "legacy v1 identity"
    verdict; its documented unsoundness is WHY the full-content v2 exists
    (DEC-SNAPSHOT-HASH-FULL-001).
    """

    def test_deterministic_hex_over_multi_slice_grid(self) -> None:
        # Non-empty + single-bar + EMPTY slices — both loop arms exercised.
        grid = {
            ("NIFTY", "1minute"): _trending_bars(2),
            ("BANKNIFTY", "1minute"): [_bar(0, 0, 100.0, 101.0)],
            ("SENSEX", "1minute"): [],
        }
        a = _data_snapshot_hash_v1(**_SCOPE, slices=grid)
        b = _data_snapshot_hash_v1(**_SCOPE, slices=grid)
        assert a == b
        assert len(a) == 64 and a == a.lower()

    def test_v1_weakness_interior_edit_aliases(self) -> None:
        # The documented v1 unsoundness pin (DEC-SNAPSHOT-HASH-FULL-001): an
        # interior-bar edit that preserves count + first/last timestamp+close
        # produces the SAME v1 hash — the exact aliasing bug the full-content
        # v2 kills (v2 asserted-distinct below).
        base = [_bar(0, m, 100.0, 101.0) for m in range(5)]
        edited = list(base)
        edited[2] = _bar(0, 2, 100.0, 999.0)  # middle bar; endpoints untouched
        assert _data_snapshot_hash_v1(
            **_SCOPE, slices={("NIFTY", "1minute"): base}
        ) == _data_snapshot_hash_v1(**_SCOPE, slices={("NIFTY", "1minute"): edited})
        assert data_snapshot_hash(
            **_SCOPE, slices={("NIFTY", "1minute"): base}
        ) != data_snapshot_hash(**_SCOPE, slices={("NIFTY", "1minute"): edited})

    def test_count_and_endpoint_changes_change_the_hash(self) -> None:
        base = [_bar(0, m, 100.0, 101.0) for m in range(5)]
        a = _data_snapshot_hash_v1(**_SCOPE, slices={("NIFTY", "1minute"): base})
        # Count change (also moves the sampled last endpoint).
        assert a != _data_snapshot_hash_v1(**_SCOPE, slices={("NIFTY", "1minute"): base[:-1]})
        # Last-endpoint close edit at the SAME count + timestamps.
        endpoint = [*base[:-1], _bar(0, 4, 100.0, 250.0)]
        assert a != _data_snapshot_hash_v1(**_SCOPE, slices={("NIFTY", "1minute"): endpoint})


# ────── recover_survivor_cell (review F4) ──────


def _row_for(run_id: str, mask: int = 0, direction: str = "bull") -> ResultRow:
    return ResultRow(
        run_id=run_id,
        instrument="NIFTY",
        tf="1minute",
        direction=direction,
        mode="A",
        mask_bits=mask,
        net_pnl_paisa=0,
        gross_profit_paisa=0,
        gross_loss_paisa=0,
        max_drawdown_paisa=0,
        expectancy_paisa=0,
        total_trades=0,
        winning_trades=0,
        losing_trades=0,
        sharpe_ratio=0.0,
        sortino_ratio=0.0,
        calmar_ratio=0.0,
        profit_factor=0.0,
        win_rate=0.0,
        max_drawdown_pct=0.0,
        avg_entry_price=Decimal("0"),
        avg_exit_price=Decimal("0"),
    )


def _run_id_for(
    grid: SweepGridConfig,
    *,
    sl: int | None,
    tp: int | None,
    mask: int = 0,
    fib_anchor: FibAnchorKind = FibAnchorKind.PREV_DAY,
    price_source: PriceSourceCell = DEFAULT_PRICE_SOURCE_CELL,
) -> str:
    return mask_run_id(
        mask=mask,
        direction="bull",
        instrument="NIFTY",
        tf="1minute",
        mode="A",
        params=canonical_trade_params(
            sl_paisa=sl,
            tp_paisa=tp,
            prior_n=grid.prior_n,
            proximity=None,
            start_equity_paisa=grid.start_equity_paisa,
            fib_anchor=fib_anchor,
            vwap_source=price_source.vwap_source,
            ema_source=price_source.ema_source,
        ),
        data_snapshot_hash=grid.data_snapshot_hash,
        indicator_version=grid.indicator_version,
        code_commit_sha=grid.code_commit_sha,
    )


class TestRecoverSurvivorCell:
    def test_recovers_a_non_first_cell_by_identity_match(self) -> None:
        grid = _grid(stop_losses=[80, 100], take_profits=[50, 120])
        run_id = _run_id_for(grid, sl=100, tp=120)
        # The recovered cell now includes the Fib anchor (DEC-NIFTY-FIB-AXIS-001)
        # AND the price-source cell (DEC-NIFTY-PRICE-SOURCE-AXIS-001); the default
        # prev-day / (HLC3, CLOSE) grid recovers PREV_DAY + the baseline cell.
        assert recover_survivor_cell(_row_for(run_id), grid) == (
            100,
            120,
            None,
            FibAnchorKind.PREV_DAY,
            DEFAULT_PRICE_SOURCE_CELL,
        )

    def test_recovers_a_non_prev_day_anchor_cell(self) -> None:
        # A survivor swept under a NON-prev-day anchor recovers THAT anchor —
        # proving the anchor is part of the identity re-derivation (Golden Rule #4).
        grid = _grid(fib_anchors=[FibAnchorKind.PREV_DAY, FibAnchorKind.ROLLING_WEEKLY])
        run_id = _run_id_for(grid, sl=80, tp=50, fib_anchor=FibAnchorKind.ROLLING_WEEKLY)
        assert recover_survivor_cell(_row_for(run_id), grid) == (
            80,
            50,
            None,
            FibAnchorKind.ROLLING_WEEKLY,
            DEFAULT_PRICE_SOURCE_CELL,
        )

    def test_recovers_a_non_default_price_source_cell(self) -> None:
        # A survivor swept under a NON-default price-source cell recovers THAT cell —
        # proving the source is part of the identity re-derivation (Golden Rule #4);
        # the row's run_id ONLY reproduces at its ORIGINATING (vwap, ema) source.
        non_default = PriceSourceCell(vwap_source=PriceSource.OPEN, ema_source=PriceSource.CLOSE)
        grid = _grid(price_source_cells=[DEFAULT_PRICE_SOURCE_CELL, non_default])
        run_id = _run_id_for(grid, sl=80, tp=50, price_source=non_default)
        assert recover_survivor_cell(_row_for(run_id), grid) == (
            80,
            50,
            None,
            FibAnchorKind.PREV_DAY,
            non_default,
        )

    def test_unmatchable_row_raises(self) -> None:
        with pytest.raises(
            Stage3WireError,
            match="no \\(sl × tp × proximity × fib_anchor × price_source\\) cell",
        ):
            recover_survivor_cell(_row_for("ab" * 32), _grid())

    def test_matched_cell_with_disabled_sl_leg_raises(self) -> None:
        grid = _grid(stage3_enable=False, stop_losses=[None])
        run_id = _run_id_for(grid, sl=None, tp=50)
        with pytest.raises(Stage3WireError, match="disabled leg"):
            recover_survivor_cell(_row_for(run_id), grid)

    def test_matched_cell_with_disabled_tp_leg_raises(self) -> None:
        grid = _grid(stage3_enable=False, take_profits=[None])
        run_id = _run_id_for(grid, sl=80, tp=None)
        with pytest.raises(Stage3WireError, match="disabled leg"):
            recover_survivor_cell(_row_for(run_id), grid)


# ────── label/timeframe helpers + the deterministic cell order ──────


class TestHelpers:
    def test_side_for_direction_maps_both_labels(self) -> None:
        assert _side_for_direction("bull") is Side.BUY
        assert _side_for_direction("bear") is Side.SELL

    def test_side_for_direction_rejects_unknown_label(self) -> None:
        with pytest.raises(Stage3WireError, match="unknown persisted direction"):
            _side_for_direction("sideways")

    def test_timeframe_for_label_finds_a_later_axis_entry(self) -> None:
        grid = _grid(timeframes=[Timeframe("1minute", 1), Timeframe("5minute", 5)])
        assert _timeframe_for_label("5minute", grid) == Timeframe("5minute", 5)

    def test_timeframe_for_label_rejects_unknown_label(self) -> None:
        with pytest.raises(Stage3WireError, match="not one of this grid's timeframes"):
            _timeframe_for_label("3minute", _grid())

    def test_cell_sort_key_orders_none_proximity_before_params(self) -> None:
        tf = Timeframe("1minute", 1)
        plain = SweepTuple(
            instrument="NIFTY",
            tf=tf,
            direction=Side.BUY,
            k=1,
            sl_paisa=80,
            tp_paisa=50,
            proximity=None,
        )
        proximate = SweepTuple(
            instrument="NIFTY",
            tf=tf,
            direction=Side.BUY,
            k=1,
            sl_paisa=80,
            tp_paisa=50,
            proximity=ProximityParams(),
        )
        assert _cell_sort_key(plain) < _cell_sort_key(proximate)


# ────── ensure_stage3_runtime ──────


class TestEnsureRuntime:
    def test_returns_versions_when_optimize_extra_present(self) -> None:
        pycma_version, numpy_version = ensure_stage3_runtime()
        assert pycma_version and numpy_version

    def test_missing_optimizer_raises_with_install_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> tuple[str, str]:
            raise ImportError("No module named 'cma'")

        monkeypatch.setattr("brutex.engine.stage3_wire.optimizer_versions", _boom)
        with pytest.raises(Stage3WireError, match="optimize"):
            ensure_stage3_runtime()


# ────── run_stage3_pass ──────


class TestRunStage3PassDisabled:
    def test_disabled_grid_is_a_noop_with_no_audit_lines(self, tmp_path: Path) -> None:
        audit_path = tmp_path / "audit.jsonl"
        with AuditWriter.open(audit_path) as audit:
            report = run_stage3_pass(
                _grid(stage3_enable=False, stop_losses=[None]),
                results_root=tmp_path,
                bars_for=lambda _i, _t: [],
                audit=audit,
                **_SCOPE,
            )
        assert report == Stage3PassReport(
            enabled=False,
            train_snapshot_hash=None,
            leaderboards=0,
            survivors_total=0,
            masks_refined=0,
            evals_total=0,
            cache_hits_total=0,
            rows_appended=0,
            skipped_empty_train=0,
            cells=(),
        )
        assert read_entries(audit_path) == []


class TestRunStage3PassEdges:
    def test_no_persisted_grid_files_yields_zero_leaderboards(self, tmp_path: Path) -> None:
        audit_path = tmp_path / "audit.jsonl"
        with AuditWriter.open(audit_path) as audit:
            report = run_stage3_pass(
                _grid(),
                results_root=tmp_path,
                bars_for=lambda _i, _t: _trending_bars(2),
                audit=audit,
                **_SCOPE,
            )
        assert report.enabled is True
        assert report.leaderboards == 0
        assert report.masks_refined == 0
        assert report.cells == ()
        actions = [e.action for e in read_entries(audit_path)]
        assert actions == [STAGE3_ACTION_PASS_START, STAGE3_ACTION_PASS_COMPLETE]

    def test_window_shorter_than_lockbox_skips_leaderboard(self, tmp_path: Path) -> None:
        # 20 days < the 182-day lockbox window ⇒ the train slice is empty and
        # the whole leaderboard is skipped — recorded, never silently dropped.
        grid = _grid()
        report = _run_full(tmp_path, _trending_bars(20), grid)
        assert report.enabled is True
        assert report.leaderboards == 1
        assert report.skipped_empty_train == 1
        assert report.masks_refined == 0
        assert report.cells == ()


class TestRunStage3PassFull:
    def test_refines_every_leaderboard_survivor(
        self, pipeline: tuple[Path, Stage3PassReport]
    ) -> None:
        _, report = pipeline
        assert report.enabled is True
        assert report.leaderboards == 1
        # Both grid tuples (k=0 enter-every-bar + k=1) persist a survivor and
        # both are refined — the second proves the per-proximity bar_bits memo.
        assert report.survivors_total == 2
        assert report.masks_refined == 2
        assert report.evals_total > 0
        assert report.skipped_empty_train == 0
        assert report.cells

    def test_refined_rows_land_untrimmed_in_their_own_cells(
        self, pipeline: tuple[Path, Stage3PassReport]
    ) -> None:
        _, report = pipeline
        # Every unique evaluation became exactly one persisted row (F8 — the
        # DSR-N trials are all there; nothing was trimmed or gated away).
        assert sum(cell.rows for cell in report.cells) == report.evals_total
        assert report.rows_appended == report.evals_total
        for cell in report.cells:
            store_rows = ResultsStore.load(cell.path).rows()
            assert len(store_rows) == cell.rows
            assert all(row.mask_bits.bit_count() == cell.tuple_.k for row in store_rows)

    def test_refined_cell_files_are_disjoint_from_grid_files(
        self, pipeline: tuple[Path, Stage3PassReport]
    ) -> None:
        out_root, report = pipeline
        grid = _grid()
        grid_paths = {tuple_store_path(out_root, t, grid) for t in iter_sweep_tuples(grid)}
        for cell in report.cells:
            assert cell.path.exists()
            assert cell.path not in grid_paths  # F3: their OWN files

    def test_refined_row_identity_re_derives_with_the_train_hash(
        self, pipeline: tuple[Path, Stage3PassReport]
    ) -> None:
        _, report = pipeline
        grid = _grid()
        cell = report.cells[0]
        row = ResultsStore.load(cell.path).rows()[0]
        assert report.train_snapshot_hash is not None
        expected = mask_run_id(
            mask=row.mask_bits,
            direction=row.direction,
            instrument=row.instrument,
            tf=row.tf,
            mode=row.mode,
            params=canonical_trade_params(
                sl_paisa=cell.tuple_.sl_paisa,
                tp_paisa=cell.tuple_.tp_paisa,
                prior_n=grid.prior_n,
                proximity=cell.tuple_.proximity,
                start_equity_paisa=grid.start_equity_paisa,
            ),
            data_snapshot_hash=report.train_snapshot_hash,
            indicator_version=grid.indicator_version,
            code_commit_sha=grid.code_commit_sha,
        )
        assert row.run_id == expected  # Amendment A1: the data is in the identity

    def test_refined_metrics_come_from_the_train_slice_only(
        self, pipeline: tuple[Path, Stage3PassReport]
    ) -> None:
        # Condition 7 proof: re-evaluating a refined cell ON THE TRAIN SLICE
        # reproduces the persisted Sharpe exactly; the full window would not.
        _, report = pipeline
        grid = _grid()
        cell = report.cells[0]
        row = ResultsStore.load(cell.path).rows()[0]
        train_bars = train_slice_bars(_trending_bars(_N_DAYS))
        bar_bits = build_bar_bits(train_bars, proximity=None, prior_n=grid.prior_n)
        assert cell.tuple_.sl_paisa is not None and cell.tuple_.tp_paisa is not None
        metrics: ResultMetrics = metrics_for_mask(
            train_bars,
            bar_bits,
            row.mask_bits,
            direction=Side.BUY,
            tf_minutes=1,
            stop_loss_paisa=cell.tuple_.sl_paisa,
            take_profit_paisa=cell.tuple_.tp_paisa,
            start_equity_paisa=grid.start_equity_paisa,
        )
        expected_sharpe = metrics.sharpe_ratio if metrics.sharpe_ratio is not None else 0.0
        assert row.sharpe_ratio == expected_sharpe
        assert row.total_trades == metrics.total_trades

    def test_audit_brackets_the_refine_triples_on_one_chain(
        self, pipeline: tuple[Path, Stage3PassReport]
    ) -> None:
        out_root, report = pipeline
        audit_path = out_root / "audit.jsonl"
        assert verify_chain(audit_path) > 0  # the hash chain holds end-to-end
        actions = [e.action for e in read_entries(audit_path)]
        stage3 = [a for a in actions if a.startswith("STAGE_3")]
        assert stage3[0] == STAGE3_ACTION_PASS_START
        assert stage3[-1] == STAGE3_ACTION_PASS_COMPLETE
        assert stage3.count("STAGE_3_REFINE_START") == report.masks_refined
        # Bracketing (F10): nothing STAGE_3 outside START..COMPLETE, and the
        # pass lines appear AFTER the sweep's grid lines (same single thread).
        assert actions.index(STAGE3_ACTION_PASS_START) > actions.index("GRID_SWEEP_COMPLETE")


# ────── non-default price-source cell threading (DEC-NIFTY-PRICE-SOURCE-AXIS-001) ──────


class TestStage3NonDefaultPriceSource:
    """MAJOR-1 regression: a non-default price-source cell + Stage-3 COMPLETES.

    Before the fix, ``recover_survivor_cell`` iterated ``sl × tp × proximity ×
    fib_anchor`` only and always hashed the DEFAULT ``(HLC3, CLOSE)`` source, so a
    survivor swept under e.g. ``open:close`` never matched its own ``run_id`` and
    the whole staged use case aborted with ``Stage3WireError``. These tests pin
    that the pass now completes AND refines against the ORIGINATING source bits.
    """

    _CELL = PriceSourceCell(vwap_source=PriceSource.OPEN, ema_source=PriceSource.CLOSE)

    def test_full_pass_completes_and_refines_against_the_swept_source(self, tmp_path: Path) -> None:
        grid = _grid(price_source_cells=[self._CELL])
        # Would raise Stage3WireError pre-fix; now the pass runs end-to-end.
        report = _run_full(tmp_path, _trending_bars(_N_DAYS), grid)
        assert report.enabled is True
        assert report.masks_refined >= 1
        assert report.cells
        # Every refined cell carries the ORIGINATING non-default source (never a
        # silent (HLC3, CLOSE) fallback).
        for cell in report.cells:
            assert cell.tuple_.price_source == self._CELL

    def test_refined_row_identity_re_derives_with_the_non_default_source(
        self, tmp_path: Path
    ) -> None:
        grid = _grid(price_source_cells=[self._CELL])
        report = _run_full(tmp_path, _trending_bars(_N_DAYS), grid)
        assert report.train_snapshot_hash is not None
        cell = report.cells[0]
        row = ResultsStore.load(cell.path).rows()[0]
        # The stored run_id ONLY reproduces when canonical_trade_params is fed the
        # cell's OWN (vwap, ema) source — the correctness teeth for the thread.
        expected = mask_run_id(
            mask=row.mask_bits,
            direction=row.direction,
            instrument=row.instrument,
            tf=row.tf,
            mode=row.mode,
            params=canonical_trade_params(
                sl_paisa=cell.tuple_.sl_paisa,
                tp_paisa=cell.tuple_.tp_paisa,
                prior_n=grid.prior_n,
                proximity=cell.tuple_.proximity,
                start_equity_paisa=grid.start_equity_paisa,
                vwap_source=self._CELL.vwap_source,
                ema_source=self._CELL.ema_source,
            ),
            data_snapshot_hash=report.train_snapshot_hash,
            indicator_version=grid.indicator_version,
            code_commit_sha=grid.code_commit_sha,
        )
        assert row.run_id == expected
        # And it does NOT reproduce at the default source (the false-green a
        # missing thread would have produced).
        wrong = mask_run_id(
            mask=row.mask_bits,
            direction=row.direction,
            instrument=row.instrument,
            tf=row.tf,
            mode=row.mode,
            params=canonical_trade_params(
                sl_paisa=cell.tuple_.sl_paisa,
                tp_paisa=cell.tuple_.tp_paisa,
                prior_n=grid.prior_n,
                proximity=cell.tuple_.proximity,
                start_equity_paisa=grid.start_equity_paisa,
            ),
            data_snapshot_hash=report.train_snapshot_hash,
            indicator_version=grid.indicator_version,
            code_commit_sha=grid.code_commit_sha,
        )
        assert row.run_id != wrong

    def test_manifest_record_and_cascade_specs_carry_the_source(self, tmp_path: Path) -> None:
        grid = _grid(price_source_cells=[self._CELL])
        report = _run_full(tmp_path, _trending_bars(_N_DAYS), grid)
        record = stage3_manifest_record(report)
        assert record.refined_cells
        for rec in record.refined_cells:
            assert rec.vwap_source is self._CELL.vwap_source
            assert rec.ema_source is self._CELL.ema_source
        specs = cascade_specs_for_refined_cells(report.cells, grid=grid)
        assert specs
        for spec in specs:
            assert spec.vwap_source is self._CELL.vwap_source
            assert spec.ema_source is self._CELL.ema_source


# ────── the A/B determinism oracle (review F11) + idempotent re-run ──────


class TestDeterminismOracle:
    def test_two_full_runs_produce_byte_identical_trees_and_rerun_is_noop(
        self, pipeline: tuple[Path, Stage3PassReport], tmp_path: Path
    ) -> None:
        out_root_a, report_a = pipeline
        bars = _trending_bars(_N_DAYS)
        out_root_b = tmp_path / "run-b"
        report_b = _run_full(out_root_b, bars, _grid())

        # A/B: byte-identical Parquet trees (tree ONLY — the audit chains carry
        # wall-clock timestamps and are deliberately excluded).
        tree_a = _tree_hashes(out_root_a)
        tree_b = _tree_hashes(out_root_b)
        assert tree_a == tree_b
        assert report_b.evals_total == report_a.evals_total
        assert report_b.train_snapshot_hash == report_a.train_snapshot_hash

        # Idempotent re-run (Golden Rule #7): the same run over the same tree
        # re-derives identical rows, appends none, and rewrites identical bytes.
        report_b2 = _run_full(out_root_b, bars, _grid())
        assert report_b2.rows_appended == 0
        assert report_b2.evals_total == report_b.evals_total
        assert _tree_hashes(out_root_b) == tree_b


# ────── cascade_specs_for_refined_cells (reviews F3 + F8) ──────


class TestCascadeSpecsForRefinedCells:
    def test_specs_carry_every_refined_row_untrimmed(
        self, pipeline: tuple[Path, Stage3PassReport]
    ) -> None:
        _, report = pipeline
        grid = _grid()
        specs = cascade_specs_for_refined_cells(report.cells, grid=grid)
        assert len(specs) == len(report.cells)
        for spec, cell in zip(specs, report.cells, strict=True):
            assert spec.instrument == cell.tuple_.instrument
            assert spec.tf_label == cell.tuple_.tf.label
            assert spec.direction_label == "bull"
            assert spec.sl_paisa == cell.tuple_.sl_paisa
            assert spec.tp_paisa == cell.tuple_.tp_paisa
            assert spec.proximity_bps is None
            assert len(spec.winners) == cell.rows  # UNTRIMMED (F8)

    def test_empty_cell_store_yields_no_spec(self, tmp_path: Path) -> None:
        empty_path = tmp_path / "empty.parquet"
        ResultsStore(path=empty_path).flush()
        cell = Stage3RefinedCell(
            tuple_=SweepTuple(
                instrument="NIFTY",
                tf=Timeframe("1minute", 1),
                direction=Side.BUY,
                k=1,
                sl_paisa=80,
                tp_paisa=50,
                proximity=None,
            ),
            path=empty_path,
            rows=0,
        )
        assert cascade_specs_for_refined_cells([cell], grid=_grid()) == []

    def test_proximity_cell_maps_to_bps_triple(
        self, pipeline: tuple[Path, Stage3PassReport], tmp_path: Path
    ) -> None:
        _, report = pipeline
        source_row = ResultsStore.load(report.cells[0].path).rows()[0]
        path = tmp_path / "prox.parquet"
        store = ResultsStore(path=path)
        store.append(source_row)
        store.flush()
        cell = Stage3RefinedCell(
            tuple_=SweepTuple(
                instrument="NIFTY",
                tf=Timeframe("1minute", 1),
                direction=Side.BUY,
                k=1,
                sl_paisa=80,
                tp_paisa=50,
                proximity=ProximityParams(
                    pivot_proximity_bps=5, pdh_pdl_proximity_bps=10, fib_proximity_bps=25
                ),
            ),
            path=path,
            rows=1,
        )
        (spec,) = cascade_specs_for_refined_cells([cell], grid=_grid())
        assert spec.proximity_bps == (5, 10, 25)

    def test_spec_carries_the_swept_fib_anchor(
        self, pipeline: tuple[Path, Stage3PassReport], tmp_path: Path
    ) -> None:
        # DEC-NIFTY-FIB-AXIS-001 (the Stage-4 cascade MAJOR): a non-prev-day
        # refined cell must produce a spec carrying its OWN swept anchor, so the
        # cascade re-simulation scores against the right Fib levels. FAILS BEFORE
        # the fix (the anchor was dropped, spec defaulted to prev-day).
        _, report = pipeline
        source_row = ResultsStore.load(report.cells[0].path).rows()[0]
        path = tmp_path / "rolling.parquet"
        store = ResultsStore(path=path)
        store.append(source_row)
        store.flush()
        cell = Stage3RefinedCell(
            tuple_=SweepTuple(
                instrument="NIFTY",
                tf=Timeframe("1minute", 1),
                direction=Side.BUY,
                k=1,
                sl_paisa=80,
                tp_paisa=50,
                proximity=None,
                fib_anchor=FibAnchorKind.ROLLING_WEEKLY,
            ),
            path=path,
            rows=1,
        )
        (spec,) = cascade_specs_for_refined_cells([cell], grid=_grid())
        assert spec.fib_anchor is FibAnchorKind.ROLLING_WEEKLY

    def test_spec_defaults_to_prev_day_anchor(
        self, pipeline: tuple[Path, Stage3PassReport]
    ) -> None:
        # A default (prev-day) refined cell yields a prev-day spec — byte-identical.
        _, report = pipeline
        specs = cascade_specs_for_refined_cells(report.cells, grid=_grid())
        assert all(spec.fib_anchor is FibAnchorKind.PREV_DAY for spec in specs)


# ────── per-cell trial-stats sidecars (DEC-TRIAL-STATS-WRITE-001) ──────


class TestCellTrialStatsSidecars:
    def test_every_cell_gets_a_paired_sidecar(
        self, pipeline: tuple[Path, Stage3PassReport]
    ) -> None:
        import dataclasses
        import hashlib as _hashlib

        from brutex.engine.sweep_orchestrator import _tuple_digest
        from brutex.engine.trial_stats import (
            read_tuple_trial_stats,
            trial_stats_sidecar_path,
        )

        _, report = pipeline
        assert report.cells
        grid = _grid()
        assert report.train_snapshot_hash is not None
        train_grid = dataclasses.replace(grid, data_snapshot_hash=report.train_snapshot_hash)
        for cell in report.cells:
            stats = read_tuple_trial_stats(trial_stats_sidecar_path(cell.path))
            # The N population == the persisted unique rows (condition 4: the
            # dedup-by-refined_id accumulation, never evals_total).
            assert stats.n_backtested == cell.rows
            assert stats.n_finite <= stats.n_backtested
            # Pairing identity (condition 5): digest + store content hash.
            assert stats.tuple_digest == _tuple_digest(cell.tuple_, train_grid)
            assert stats.store_content_hash == _hashlib.sha256(cell.path.read_bytes()).hexdigest()

    def test_foreign_row_in_cell_store_skips_the_sidecar_loudly(self, tmp_path: Path) -> None:
        # A cell store holding a row THIS pass did not derive (a differently-
        # configured earlier run) must NOT get a fresh sidecar — a partial N
        # would under-count; the stale pairing hash stays detectable instead.
        from brutex.engine.trial_stats import read_tuple_trial_stats, trial_stats_sidecar_path

        bars = _trending_bars(_N_DAYS)
        grid = _grid()
        report1 = _run_full(tmp_path, bars, grid)
        target = report1.cells[0]
        stale_stats = read_tuple_trial_stats(trial_stats_sidecar_path(target.path))
        store = ResultsStore.load(target.path)
        foreign = ResultsStore.load(target.path).rows()[0]
        # Re-key the row under a fresh valid identity the pass never derives.
        from dataclasses import replace as _replace

        store.append(_replace(foreign, run_id="ab" * 32))
        store.flush()

        report2 = _run_full(tmp_path, bars, grid)
        refreshed = read_tuple_trial_stats(trial_stats_sidecar_path(target.path))
        # The sidecar was SKIPPED (preserved), so it still pairs with the OLD
        # store bytes, not the foreign-row store — drift stays detectable.
        assert refreshed == stale_stats
        assert report2.sidecars_written == len(report2.cells) - 1


# ────── stage3_manifest_record (review F3 — manifest provenance) ──────


class TestStage3ManifestRecord:
    def test_disabled_report_maps_to_disabled_record(self) -> None:
        report = Stage3PassReport(
            enabled=False,
            train_snapshot_hash=None,
            leaderboards=0,
            survivors_total=0,
            masks_refined=0,
            evals_total=0,
            cache_hits_total=0,
            rows_appended=0,
            skipped_empty_train=0,
            cells=(),
        )
        assert stage3_manifest_record(report) == Stage3ManifestRecord(
            enabled=False, train_snapshot_hash=None, refined_cells=()
        )

    def test_enabled_report_records_every_refined_cell(
        self, pipeline: tuple[Path, Stage3PassReport]
    ) -> None:
        _, report = pipeline
        record = stage3_manifest_record(report)
        assert record.enabled is True
        assert record.train_snapshot_hash == report.train_snapshot_hash
        assert len(record.refined_cells) == len(report.cells)
        for rec, cell in zip(record.refined_cells, report.cells, strict=True):
            assert rec.instrument == cell.tuple_.instrument
            assert rec.k == cell.tuple_.k
            assert rec.sl_paisa == cell.tuple_.sl_paisa
            assert rec.tp_paisa == cell.tuple_.tp_paisa
            assert rec.proximity_bps is None

    def test_proximity_cell_records_its_bps_triple(self) -> None:
        cell = Stage3RefinedCell(
            tuple_=SweepTuple(
                instrument="NIFTY",
                tf=Timeframe("1minute", 1),
                direction=Side.BUY,
                k=1,
                sl_paisa=80,
                tp_paisa=50,
                proximity=ProximityParams(
                    pivot_proximity_bps=5, pdh_pdl_proximity_bps=10, fib_proximity_bps=25
                ),
            ),
            path=Path("unused.parquet"),
            rows=1,
        )
        report = Stage3PassReport(
            enabled=True,
            train_snapshot_hash="ab" * 32,
            leaderboards=1,
            survivors_total=1,
            masks_refined=1,
            evals_total=1,
            cache_hits_total=0,
            rows_appended=1,
            skipped_empty_train=0,
            cells=(cell,),
        )
        record = stage3_manifest_record(report)
        assert record.refined_cells[0].proximity_bps == (5, 10, 25)

    @pytest.mark.parametrize(("sl", "tp"), [(None, 50), (80, None)])
    def test_disabled_leg_cell_rejected(self, sl: int | None, tp: int | None) -> None:
        cell = Stage3RefinedCell(
            tuple_=SweepTuple(
                instrument="NIFTY",
                tf=Timeframe("1minute", 1),
                direction=Side.BUY,
                k=1,
                sl_paisa=sl,
                tp_paisa=tp,
                proximity=None,
            ),
            path=Path("unused.parquet"),
            rows=1,
        )
        report = Stage3PassReport(
            enabled=True,
            train_snapshot_hash="ab" * 32,
            leaderboards=1,
            survivors_total=1,
            masks_refined=1,
            evals_total=1,
            cache_hits_total=0,
            rows_appended=1,
            skipped_empty_train=0,
            cells=(cell,),
        )
        with pytest.raises(Stage3WireError, match="disabled leg"):
            stage3_manifest_record(report)


class TestManifestRecordRelPath:
    """DEC-ROW-PROVENANCE-001: results_root threads the cell's relative path."""

    def _report_with_cell(self, path: Path) -> Stage3PassReport:
        cell = Stage3RefinedCell(
            tuple_=SweepTuple(
                instrument="NIFTY",
                tf=Timeframe("1minute", 1),
                direction=Side.BUY,
                k=1,
                sl_paisa=80,
                tp_paisa=50,
                proximity=None,
            ),
            path=path,
            rows=1,
        )
        return Stage3PassReport(
            enabled=True,
            train_snapshot_hash="ab" * 32,
            leaderboards=1,
            survivors_total=1,
            masks_refined=1,
            evals_total=1,
            cache_hits_total=0,
            rows_appended=1,
            skipped_empty_train=0,
            cells=(cell,),
        )

    def test_results_root_threads_the_relative_path(self, tmp_path: Path) -> None:
        cell_path = tmp_path / "NIFTY" / "1minute" / ("BUY_k1_" + "ab" * 8 + ".parquet")
        record = stage3_manifest_record(self._report_with_cell(cell_path), results_root=tmp_path)
        assert record.refined_cells[0].rel_path == ("NIFTY/1minute/BUY_k1_" + "ab" * 8 + ".parquet")

    def test_no_results_root_omits_the_path(self, tmp_path: Path) -> None:
        cell_path = tmp_path / "NIFTY" / "1minute" / ("BUY_k1_" + "ab" * 8 + ".parquet")
        record = stage3_manifest_record(self._report_with_cell(cell_path))
        assert record.refined_cells[0].rel_path is None

    def test_cell_outside_root_raises(self, tmp_path: Path) -> None:
        outside = tmp_path / "elsewhere" / "x.parquet"
        (tmp_path / "root").mkdir()
        with pytest.raises(Stage3WireError, match="not under"):
            stage3_manifest_record(self._report_with_cell(outside), results_root=tmp_path / "root")
