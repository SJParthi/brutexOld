"""Gate-scoped ``/results`` serving (DEC-SWEEP-SERVING-GATE-PROVENANCE-001).

The READ side of the gate-provenance fix, over REAL per-tuple Parquet stores
written by the REAL :class:`~brutex.engine.results_store.ResultsStore`:

* **arm-1** — the cross-store dedup keys on ``(gate_coordinate, run_id)``: the
  SAME gate-blind ``run_id`` re-run under a different gate config keeps BOTH
  rows (not a conflict), the gated coordinate serves the GATED row, and the
  ``run_id_conflicts`` counter narrows to WITHIN-coordinate conflicts.
* **arm-2** — a ZERO-survivor gated store (schema-present column, 0 rows) wins
  the coordinate set as the ``"gated"`` sentinel: the default view serves the
  honest EMPTY set, never the stale ungated rows (the KILLED shape).
* the payload contract cases 1/2/3, the |K|-based coexistence FOLDING rule
  (the MUST-PIN test), the explicit ``?gate=`` scoping + unknown-token
  refusal, and the 0-row ``.trials.json`` context surface.
"""

# The loader pins read module-private helpers deliberately.
# pyright: reportPrivateUsage=none
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars", reason="the reader loads per-tuple Parquet stores via ResultsStore")

from brutex.dashboard.sweep_results_view import (
    DEFAULT_RANK_BY,
    GATE_COORDINATE_SENTINEL,
    GATE_COORDINATE_UNGATED,
    build_sweep_leaderboard,
    gate_coordinate_of,
    load_sweep_scan,
    resolve_rank_by_param,
)
from brutex.engine.results_store import ResultRow, ResultsStore
from brutex.engine.sweep_identity import gate_provenance_token

_TOKEN_A = gate_provenance_token(gate_config={"overfit": [0.95, 0.5, 0.0, 0.0]}, selection=None)
_TOKEN_B = gate_provenance_token(gate_config={"overfit": [0.5, 0.5, 0.0, 0.0]}, selection=None)
assert _TOKEN_A is not None and _TOKEN_B is not None
_COORD_A = gate_coordinate_of(_TOKEN_A)
_COORD_B = gate_coordinate_of(_TOKEN_B)


def _row(
    seed: int,
    *,
    gate_provenance: str | None = None,
    sharpe: float = 1.0,
    vix_mean: float | None = None,
) -> ResultRow:
    """A minimal valid :class:`ResultRow`, optionally provenance-stamped."""
    return ResultRow(
        run_id=f"{seed:064x}",
        instrument="NIFTY",
        tf="5m",
        direction="bull",
        mode="A",
        mask_bits=3,
        net_pnl_paisa=100_00,
        gross_profit_paisa=200_00,
        gross_loss_paisa=100_00,
        max_drawdown_paisa=50_00,
        expectancy_paisa=10_00,
        total_trades=30,
        winning_trades=20,
        losing_trades=10,
        sharpe_ratio=sharpe,
        sortino_ratio=1.0,
        calmar_ratio=1.0,
        profit_factor=2.0,
        win_rate=0.5,
        max_drawdown_pct=0.1,
        avg_entry_price=Decimal("100.00"),
        avg_exit_price=Decimal("101.00"),
        gate_provenance=gate_provenance,
        vix_mean=vix_mean,
        vix_min=vix_mean,
        vix_max=vix_mean,
    )


def _write_store(
    lake_root: Path,
    name: str,
    rows: list[ResultRow],
    *,
    gate_provenance: str | None = None,
) -> Path:
    """Write one per-tuple store under the globbed ``<lake>/sweeps`` layout."""
    dest = lake_root / "sweeps" / "NIFTY" / "5m" / name
    store = ResultsStore(path=dest, gate_provenance=gate_provenance)
    for row in rows:
        store.append(row)
    dest.parent.mkdir(parents=True, exist_ok=True)
    store.flush()
    return dest


class TestGateCoordinateOf:
    """The pure row-token → coordinate map."""

    def test_none_is_ungated(self) -> None:
        """A provenance-less row lands in the honest ungated bucket."""
        assert gate_coordinate_of(None) == GATE_COORDINATE_UNGATED

    def test_token_is_prefixed_short_hash(self) -> None:
        """A known token maps to a stable ``gate:<16-hex>`` tag."""
        coord = gate_coordinate_of(_TOKEN_A)
        assert coord.startswith("gate:")
        assert len(coord) == len("gate:") + 16
        assert coord == gate_coordinate_of(_TOKEN_A)  # deterministic

    def test_distinct_tokens_distinct_coordinates(self) -> None:
        """Two configs never share a coordinate."""
        assert _COORD_A != _COORD_B
        assert GATE_COORDINATE_SENTINEL not in (_COORD_A, _COORD_B)


class TestArm1GateAwareDedup:
    """The (gate_coordinate, run_id) dedup key."""

    def test_same_run_id_across_coordinates_keeps_both(self, tmp_path: Path) -> None:
        """arm-1: an ungated stale twin no longer shadows the gated row."""
        _write_store(tmp_path, "a_ungated.parquet", [_row(1, sharpe=0.1)])
        _write_store(tmp_path, "b_gated.parquet", [_row(1, gate_provenance=_TOKEN_A, sharpe=0.9)])
        scan = load_sweep_scan(tmp_path)
        assert scan.run_id_conflicts == 0  # different coordinates ≠ a conflict
        assert len(scan.rows) == 2  # BOTH kept, each under its own coordinate
        board = build_sweep_leaderboard(tmp_path, scan=scan)
        # Case 2: the gated coordinate is served — the GATED row, never the
        # sorted-first ungated one (pre-fix: first-wins dropped the gated row).
        assert [r["sharpe_ratio"] for r in board.rows] == [0.9]
        assert board.rows[0]["gate_provenance"] == _TOKEN_A

    def test_within_coordinate_conflict_still_counts(self, tmp_path: Path) -> None:
        """The pre-existing within-coordinate first-wins conflict is unchanged."""
        _write_store(tmp_path, "a.parquet", [_row(1, sharpe=1.0)])
        _write_store(tmp_path, "b.parquet", [_row(1, sharpe=9.9)])
        scan = load_sweep_scan(tmp_path)
        assert scan.run_id_conflicts == 1
        assert len(scan.rows) == 1
        assert scan.rows[0].sharpe_ratio == 1.0  # deterministic FIRST (sorted paths)

    def test_vix_only_divergence_within_coordinate_not_a_conflict(self, tmp_path: Path) -> None:
        """The VIX most-informed-wins law survives the key change, per coordinate."""
        _write_store(tmp_path, "a.parquet", [_row(1, gate_provenance=_TOKEN_A)])
        _write_store(tmp_path, "b.parquet", [_row(1, gate_provenance=_TOKEN_A, vix_mean=12.5)])
        scan = load_sweep_scan(tmp_path)
        assert scan.run_id_conflicts == 0
        assert len(scan.rows) == 1
        assert scan.rows[0].vix_mean == 12.5  # the more-informed row won


class TestPayloadContract:
    """Cases 1/2/3 of the leaderboard payload contract."""

    def test_case1_single_ungated_coordinate(self, tmp_path: Path) -> None:
        """A legacy single-coordinate lake: identical rows + additive keys only."""
        _write_store(tmp_path, "a.parquet", [_row(1, sharpe=0.5), _row(2, sharpe=2.5)])
        payload = build_sweep_leaderboard(tmp_path).to_dict()
        assert [r["sharpe_ratio"] for r in payload["rows"]] == [2.5, 0.5]  # unscoped
        assert payload["gate_coordinate"] == GATE_COORDINATE_UNGATED
        assert payload["gate_coordinates"] == [GATE_COORDINATE_UNGATED]
        assert payload["mixed_gate_coordinates"] is False
        assert payload["gate_reasons"] == []
        assert all(r["gate_coordinate"] == GATE_COORDINATE_UNGATED for r in payload["rows"])
        assert all(r["gate_provenance"] is None for r in payload["rows"])
        assert all(r["gate_reasons"] == [] for r in payload["rows"])

    def test_case1_single_gated_coordinate(self, tmp_path: Path) -> None:
        """One populated gated coordinate alone: served whole, mixed False."""
        _write_store(tmp_path, "g.parquet", [_row(1, gate_provenance=_TOKEN_A)])
        payload = build_sweep_leaderboard(tmp_path).to_dict()
        assert payload["gate_coordinate"] == _COORD_A
        assert payload["gate_coordinates"] == [_COORD_A]
        assert len(payload["rows"]) == 1

    def test_case2_gated_beats_ungated_default(self, tmp_path: Path) -> None:
        """Exactly one gated + ungated: the GATED coordinate is the default view."""
        _write_store(tmp_path, "a_u.parquet", [_row(1, sharpe=0.1), _row(2, sharpe=0.2)])
        _write_store(tmp_path, "b_g.parquet", [_row(3, gate_provenance=_TOKEN_A, sharpe=0.9)])
        payload = build_sweep_leaderboard(tmp_path).to_dict()
        assert payload["gate_coordinate"] == _COORD_A
        assert sorted(payload["gate_coordinates"]) == sorted([_COORD_A, GATE_COORDINATE_UNGATED])
        assert payload["mixed_gate_coordinates"] is False
        assert [r["run_id"] for r in payload["rows"]] == [f"{3:064x}"]
        assert payload["total_rows"] == 1  # scoped count, honest "N of M"

    def test_case2_zero_row_gated_store_serves_empty(self, tmp_path: Path) -> None:
        """The arm-2 KILLED shape: 0-row gated store wins as the EMPTY set."""
        _write_store(tmp_path, "a_u.parquet", [_row(1), _row(2)])
        _write_store(tmp_path, "z_e.parquet", [], gate_provenance=_TOKEN_A)
        payload = build_sweep_leaderboard(tmp_path).to_dict()
        assert payload["rows"] == []  # the stale ungated rows are ABSENT
        assert payload["gate_coordinate"] == GATE_COORDINATE_SENTINEL
        assert payload["gate_coordinates"] == [
            GATE_COORDINATE_SENTINEL,
            GATE_COORDINATE_UNGATED,
        ]
        assert payload["mixed_gate_coordinates"] is False
        assert payload["available"] is True  # stores exist — honest empty, not blank

    def test_case3_mixed_serves_all_labeled(self, tmp_path: Path) -> None:
        """>1 known gated config: everything served + labeled, never a silent pick."""
        _write_store(tmp_path, "a_g.parquet", [_row(1, gate_provenance=_TOKEN_A)])
        _write_store(tmp_path, "b_g.parquet", [_row(2, gate_provenance=_TOKEN_B)])
        payload = build_sweep_leaderboard(tmp_path).to_dict()
        assert payload["gate_coordinate"] is None
        assert payload["mixed_gate_coordinates"] is True
        assert sorted(payload["gate_coordinates"]) == sorted([_COORD_A, _COORD_B])
        assert len(payload["rows"]) == 2
        assert {r["gate_coordinate"] for r in payload["rows"]} == {_COORD_A, _COORD_B}

    def test_explicit_gate_scopes(self, tmp_path: Path) -> None:
        """?gate=X serves exactly X's rows — including the ungated bucket."""
        _write_store(tmp_path, "a_u.parquet", [_row(1, sharpe=0.1)])
        _write_store(tmp_path, "b_g.parquet", [_row(2, gate_provenance=_TOKEN_A, sharpe=0.9)])
        gated = build_sweep_leaderboard(tmp_path, gate=_COORD_A).to_dict()
        assert [r["sharpe_ratio"] for r in gated["rows"]] == [0.9]
        assert gated["gate_coordinate"] == _COORD_A
        ungated = build_sweep_leaderboard(tmp_path, gate=GATE_COORDINATE_UNGATED).to_dict()
        assert [r["sharpe_ratio"] for r in ungated["rows"]] == [0.1]
        assert ungated["gate_coordinate"] == GATE_COORDINATE_UNGATED

    def test_explicit_unknown_gate_refuses(self, tmp_path: Path) -> None:
        """An unknown explicit token raises ValueError naming the available set."""
        _write_store(tmp_path, "a.parquet", [_row(1)])
        with pytest.raises(ValueError, match="unknown gate coordinate"):
            build_sweep_leaderboard(tmp_path, gate="gate:doesnotexist00")

    def test_explicit_gate_on_empty_tree_refuses(self, tmp_path: Path) -> None:
        """An explicit token can never match an empty coordinate set → refusal."""
        with pytest.raises(ValueError, match="available coordinates: \\(none\\)"):
            build_sweep_leaderboard(tmp_path, gate="ungated")

    def test_blank_gate_is_default(self, tmp_path: Path) -> None:
        """A blank/whitespace gate token rides the default resolution."""
        _write_store(tmp_path, "a.parquet", [_row(1)])
        payload = build_sweep_leaderboard(tmp_path, gate="  ").to_dict()
        assert payload["gate_coordinate"] == GATE_COORDINATE_UNGATED
        assert len(payload["rows"]) == 1


class TestCoexistenceFolding:
    """The MUST-PIN |K|-based folding rule."""

    def test_sentinel_folds_into_single_known_config(self, tmp_path: Path) -> None:
        """MUST-PIN: one populated config + a 0-row sentinel from the SAME run
        (+ an ungated store) → the sentinel FOLDS into the known config."""
        _write_store(tmp_path, "a_u.parquet", [_row(1, sharpe=0.1)])
        _write_store(tmp_path, "b_g.parquet", [_row(2, gate_provenance=_TOKEN_A, sharpe=0.9)])
        _write_store(tmp_path, "z_e.parquet", [], gate_provenance=_TOKEN_A)
        payload = build_sweep_leaderboard(tmp_path).to_dict()
        # The sentinel is FOLDED — exactly two coordinates, no separate "gated".
        assert len(payload["gate_coordinates"]) == 2
        assert payload["gate_coordinates"] == sorted([_COORD_A, GATE_COORDINATE_UNGATED])
        assert GATE_COORDINATE_SENTINEL not in payload["gate_coordinates"]
        assert payload["mixed_gate_coordinates"] is False
        # Served == the populated config's rows (the sentinel contributes nothing).
        assert [r["sharpe_ratio"] for r in payload["rows"]] == [0.9]
        assert payload["gate_coordinate"] == _COORD_A

    def test_sentinel_stands_apart_when_mixed(self, tmp_path: Path) -> None:
        """|K| >= 2: the sentinel cannot be attributed — it stands as its own entry."""
        _write_store(tmp_path, "a_g.parquet", [_row(1, gate_provenance=_TOKEN_A)])
        _write_store(tmp_path, "b_g.parquet", [_row(2, gate_provenance=_TOKEN_B)])
        _write_store(tmp_path, "z_e.parquet", [], gate_provenance=_TOKEN_A)
        payload = build_sweep_leaderboard(tmp_path).to_dict()
        assert payload["mixed_gate_coordinates"] is True
        assert GATE_COORDINATE_SENTINEL in payload["gate_coordinates"]
        assert len(payload["gate_coordinates"]) == 3

    def test_zero_row_foreign_config_misattribution_boundary(self, tmp_path: Path) -> None:
        """BOUNDARY PIN (the sole remaining honest residual — DEC-SWEEP-SERVING-
        GATE-PROVENANCE-001, MIGRATION.md #1758): TWO genuinely-DIFFERENT OVERFIT
        configs on one root, one producing a 0-row sentinel that mis-folds.

        Config B (a distinct overfit gate — ``_TOKEN_B``, NOT a ``--top``
        selection knob) ran and persisted ZERO survivors. Its token is
        unrecoverable from a 0-row store's bytes (the conditional column carries
        schema PRESENCE only — no row carries the token), so the store reads the
        ``"gated"`` sentinel; the |K| == 1 coexistence rule then FOLDS that
        sentinel into the single known populated overfit config A. So config B's
        honest empty set silently merges under config A's coordinate.

        This is now the RARE two-different-overfit-configs edge, NOT the former
        CLI-default (light ``--top``) shape — that far-more-common instance is
        CLOSED by scoping ``grid_gate_provenance`` to the gate config only
        (:class:`TestCliDefaultZeroRowServesEmpty`): a ``--top``-only baseline is
        now "ungated", so a zero-survivor gated re-run serves the honest empty
        set instead of the stale baseline rows. The residual here requires two
        SEPARATE institutional overfit gates on one root — a genuinely rare
        operator shape, deliberately NOT fixed here (a v2 carrier that persists
        the token at 0 rows is the named follow-up).
        """
        _write_store(tmp_path, "a_g.parquet", [_row(1, gate_provenance=_TOKEN_A, sharpe=0.9)])
        # Overfit config B ran and persisted ZERO survivors: token B never hits a row.
        _write_store(tmp_path, "b_zero.parquet", [], gate_provenance=_TOKEN_B)
        payload = build_sweep_leaderboard(tmp_path).to_dict()
        # The FOLD: B's sentinel vanished into A's coordinate — |K| == 1.
        assert payload["gate_coordinates"] == [_COORD_A]
        assert _COORD_B not in payload["gate_coordinates"]
        assert GATE_COORDINATE_SENTINEL not in payload["gate_coordinates"]
        assert payload["mixed_gate_coordinates"] is False
        # Case 1 view: config A's rows serve under A's coordinate; config B's
        # empty result is structurally invisible (the mis-attribution).
        assert payload["gate_coordinate"] == _COORD_A
        assert [r["sharpe_ratio"] for r in payload["rows"]] == [0.9]
        # ...and config B's coordinate cannot even be requested explicitly.
        with pytest.raises(ValueError, match="unknown gate coordinate"):
            build_sweep_leaderboard(tmp_path, gate=_COORD_B)

    def test_zero_row_ungated_store_contributes_ungated(self, tmp_path: Path) -> None:
        """A 0-row UNGATED store names the ungated coordinate, never a gate."""
        _write_store(tmp_path, "e_u.parquet", [])
        scan = load_sweep_scan(tmp_path)
        assert scan.gate_coordinates == (GATE_COORDINATE_UNGATED,)


class TestCliDefaultZeroRowServesEmpty:
    """The CLI-default 0-row-misfold, CLOSED (DEC-SWEEP-SERVING-GATE-PROVENANCE-001)."""

    def test_top_only_baseline_then_zero_survivor_gated_serves_empty(self, tmp_path: Path) -> None:
        """A ``--top``-only baseline + a zero-survivor gated re-run → honest empty set.

        The exact CLI-default shape: the operator runs a light ``sweep-all --top 20``
        (overfit OFF), then re-runs it gated (overfit ON, high ``dsr_min``) which
        survives ZERO combos. Because :func:`grid_gate_provenance` is scoped to the
        GATE CONFIG only, the ``--top``-only baseline carries NO serving token → it
        is "ungated", so the known-gated set is EMPTY (|K| == 0); the gated 0-row
        sentinel therefore serves the honest EMPTY set (case-2), NOT the stale
        baseline rows.

        DISCRIMINATION: this REPRODUCES the misfold if the seam is reverted. With
        ``selection=_selection_identity_fragment(grid)`` restored, the baseline's
        token becomes ``{"selection":{"top_k":20}}`` — a KNOWN populated gate:
        coordinate (|K| == 1) — and the |K| == 1 fold serves the stale baseline
        rows, failing the ``rows == []`` / sentinel assertions below.
        """
        from brutex.engine.sweep_orchestrator import (
            SweepGridConfig,
            Timeframe,
            grid_gate_provenance,
        )
        from brutex.ranking import OverfitGateConfig
        from brutex.strategies.types import Side

        def _grid(**overrides: object) -> SweepGridConfig:
            base: dict[str, object] = {
                "instruments": ("NIFTY",),
                "timeframes": (Timeframe("5m", 5),),
                "directions": (Side.BUY,),
                "condition_bits": (0, 1),
                "k_values": (1,),
                "stop_losses": (None,),
                "take_profits": (None,),
                "proximities": (None,),
                "mode": "A",
                "data_snapshot_hash": "c" * 64,
                "indicator_version": "iv-test",
                "code_commit_sha": "d" * 40,
            }
            base.update(overrides)
            return SweepGridConfig(**base)  # type: ignore[arg-type]

        # The light CLI baseline: --top 20, overfit OFF. Each finalist row is
        # stamped with grid_gate_provenance(grid) exactly as a REAL sweep does
        # (ranked_combo_to_result_row → the per-row token). Scoped to the gate
        # config, that token is None ⇒ the rows read as UNGATED.
        baseline_grid = _grid(top_k=20)
        baseline_token = grid_gate_provenance(baseline_grid)
        _write_store(
            tmp_path,
            "a_baseline.parquet",
            [
                _row(1, gate_provenance=baseline_token, sharpe=0.1),
                _row(2, gate_provenance=baseline_token, sharpe=0.2),
            ],
            gate_provenance=baseline_token,
        )

        # The gated re-run: same --top 20, overfit ON, high dsr_min ⇒ ZERO survivors.
        gated_grid = _grid(top_k=20, overfit=OverfitGateConfig(dsr_min=0.99))
        gated_token = grid_gate_provenance(gated_grid)
        assert gated_token is not None and "gate_config" in gated_token
        _write_store(tmp_path, "z_gated_zero.parquet", [], gate_provenance=gated_token)

        payload = build_sweep_leaderboard(tmp_path).to_dict()
        # Case 2 (empty): the stale baseline rows are ABSENT — the honest empty set.
        assert payload["rows"] == []
        assert payload["gate_coordinate"] == GATE_COORDINATE_SENTINEL
        assert payload["gate_coordinates"] == [
            GATE_COORDINATE_SENTINEL,
            GATE_COORDINATE_UNGATED,
        ]
        assert payload["mixed_gate_coordinates"] is False
        assert payload["available"] is True  # stores exist — honest empty, not blank


class TestGateTrialsContext:
    """The best-effort 0-row-gated ``.trials.json`` context surface."""

    def test_trials_context_surfaces_on_sentinel_view(self, tmp_path: Path) -> None:
        """A readable sidecar phrases the honest zero-survivor count."""
        from brutex.engine.trial_stats import (
            trial_stats_sidecar_path,
            tuple_trial_stats_from_sharpes,
            write_tuple_trial_stats,
        )

        _write_store(tmp_path, "a_u.parquet", [_row(1)])
        dest = _write_store(tmp_path, "z_e.parquet", [], gate_provenance=_TOKEN_A)
        stats = tuple_trial_stats_from_sharpes(
            [0.1, 0.2, 0.3, 0.4],
            n_enumerated=4,
            n_conflict_pruned=0,
            n_minhits_dropped=0,
            tuple_digest="a" * 16,
            store_content_hash="b" * 64,
        )
        write_tuple_trial_stats(trial_stats_sidecar_path(dest), stats)
        payload = build_sweep_leaderboard(tmp_path).to_dict()
        assert payload["gate_coordinate"] == GATE_COORDINATE_SENTINEL
        assert payload["gate_reasons"] == ["4 evaluated, 0 survived the gate"]

    def test_missing_sidecar_is_empty_context(self, tmp_path: Path) -> None:
        """No sidecar → no fabricated count; the empty view still serves."""
        _write_store(tmp_path, "a_u.parquet", [_row(1)])
        _write_store(tmp_path, "z_e.parquet", [], gate_provenance=_TOKEN_A)
        payload = build_sweep_leaderboard(tmp_path).to_dict()
        assert payload["gate_coordinate"] == GATE_COORDINATE_SENTINEL
        assert payload["gate_reasons"] == []

    def test_reasons_absent_off_the_sentinel_view(self, tmp_path: Path) -> None:
        """The trials context never leaks onto a populated coordinate's view."""
        from brutex.engine.trial_stats import (
            trial_stats_sidecar_path,
            tuple_trial_stats_from_sharpes,
            write_tuple_trial_stats,
        )

        _write_store(tmp_path, "b_g.parquet", [_row(2, gate_provenance=_TOKEN_A)])
        dest = _write_store(tmp_path, "z_e.parquet", [], gate_provenance=_TOKEN_A)
        stats = tuple_trial_stats_from_sharpes(
            [0.5],
            n_enumerated=1,
            n_conflict_pruned=0,
            n_minhits_dropped=0,
            tuple_digest="a" * 16,
            store_content_hash="b" * 64,
        )
        write_tuple_trial_stats(trial_stats_sidecar_path(dest), stats)
        payload = build_sweep_leaderboard(tmp_path).to_dict()
        # The sentinel FOLDED into the known config — the served coordinate is
        # the populated one, so the empty-set context stays off this view.
        assert payload["gate_coordinate"] == _COORD_A
        assert payload["gate_reasons"] == []


class TestResolveRankByParam:
    """The public thin delegator over the ONE ``_resolve_rank_by`` fall-back.

    DEC-SWEEP-SERVING-GATE-PROVENANCE-001 (d1 cache-key fix): the route keys
    its L2 cache on the RESOLVED column — this direct unit lives in the
    module's own discovery-mapped suite so the delegator never rides on the
    route suite alone.
    """

    def test_known_column_passes_through(self) -> None:
        assert resolve_rank_by_param("net_pnl_paisa") == "net_pnl_paisa"

    def test_none_blank_and_unknown_fall_back_to_default(self) -> None:
        assert resolve_rank_by_param(None) == DEFAULT_RANK_BY
        assert resolve_rank_by_param("") == DEFAULT_RANK_BY
        assert resolve_rank_by_param("sharpe_ratio|p=|g=ungated") == DEFAULT_RANK_BY
