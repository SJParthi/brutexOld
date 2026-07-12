"""Write-side gate-provenance carrier (DEC-SWEEP-SERVING-GATE-PROVENANCE-001).

The row/store CARRIER of the gate-scoped ``/results`` serving fix:
``ResultRow.gate_provenance`` (optional, presence-tolerant — the Ruling-5
optional-column law), the CONDITIONAL ``gate_provenance`` Parquet column
(present ONLY in gated stores — ungated bytes stay byte-identical to the
pre-column format, keeping the frozen store-bytes goldens unmoved), the
store-level token that makes a ZERO-survivor gated store schema-discoverable
(the arm-2 carrier), the directional append backfill, and the write-side
wiring: ``gate_provenance_token`` / ``grid_gate_provenance`` →
``SweepTuple.to_run_config`` → ``ranked_combo_to_result_row`` →
``TupleStoreProvider``.
"""

# The wiring pins read module-private fragments deliberately.
# pyright: reportPrivateUsage=none
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars", reason="the store writes Parquet via polars")
import polars as pl
from brutex.engine.results_store import (
    GATE_PROVENANCE_FIELD,
    ResultRow,
    ResultsStore,
    ResultsStoreError,
)
from brutex.engine.sweep_identity import _gate_config_fragment, gate_provenance_token
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    SweepTuple,
    Timeframe,
    TupleStoreProvider,
    _selection_identity_fragment,
    grid_gate_provenance,
    load_or_create_store,
    make_store_for,
)
from brutex.ranking import OverfitGateConfig
from brutex.strategies.types import Side

from tests.fakes.metrics_fixtures import make_result_metrics


def _row(seed: int, *, gate_provenance: str | None = None, sharpe: float = 1.0) -> ResultRow:
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
    )


_TOKEN = gate_provenance_token(gate_config={"overfit": [0.95, 0.5, 0.0, 0.0]}, selection=None)
assert _TOKEN is not None
_OTHER_TOKEN = gate_provenance_token(gate_config=None, selection={"top_k": 5})
assert _OTHER_TOKEN is not None


def _grid(**overrides: object) -> SweepGridConfig:
    """A tiny valid grid; ``overrides`` flip the gate/selection knobs."""
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


def _tuple() -> SweepTuple:
    """The single grid point of :func:`_grid` (default PREV_DAY fib anchor)."""
    return SweepTuple(
        instrument="NIFTY",
        tf=Timeframe("5m", 5),
        direction=Side.BUY,
        k=1,
        sl_paisa=None,
        tp_paisa=None,
        proximity=None,
    )


class TestGateProvenanceToken:
    """The pure token combiner in :mod:`brutex.engine.sweep_identity`."""

    def test_both_none_is_none(self) -> None:
        """An ungated default-selection grid contributes NO token (golden lever)."""
        assert gate_provenance_token(gate_config=None, selection=None) is None

    def test_gate_only(self) -> None:
        """A gate-only token carries exactly the gate_config leg, canonical JSON."""
        token = gate_provenance_token(gate_config={"overfit": [0.95]}, selection=None)
        assert token == '{"gate_config":{"overfit":[0.95]}}'
        assert json.loads(token) == {"gate_config": {"overfit": [0.95]}}

    def test_selection_only(self) -> None:
        """A selection-only token carries exactly the selection leg."""
        token = gate_provenance_token(gate_config=None, selection={"top_k": 5})
        assert token == '{"selection":{"top_k":5}}'

    def test_both_legs_sorted_compact(self) -> None:
        """Both legs serialise sort_keys + compact — byte-deterministic."""
        token = gate_provenance_token(gate_config={"stage3": [10, 2, 1]}, selection={"min_hits": 0})
        assert token == '{"gate_config":{"stage3":[10,2,1]},"selection":{"min_hits":0}}'


class TestGridGateProvenance:
    """The ONE write-side seam over the two #1336 digest fragments."""

    def test_default_grid_is_none(self) -> None:
        """An ungated default-selection grid carries NO provenance (byte-stable)."""
        assert grid_gate_provenance(_grid()) is None

    def test_gated_grid_matches_fragments(self) -> None:
        """A gated grid's token derives from the EXACT digest fragments (one source)."""
        grid = _grid(overfit=OverfitGateConfig(dsr_min=0.95))
        token = grid_gate_provenance(grid)
        assert token == gate_provenance_token(
            gate_config=_gate_config_fragment(grid),
            selection=_selection_identity_fragment(grid),
        )
        assert token is not None and "gate_config" in token

    def test_gated_grid_excludes_selection_while_address_keeps_it(self) -> None:
        """A gated grid with a NON-default ``top_k`` carries ONLY the gate_config
        leg in the SERVING token, while the store ADDRESS still folds ``top_k``.

        This pins the fix's CORE property directly (not trivially, unlike the
        default-selection case in :meth:`test_gated_grid_matches_fragments`):
        even when BOTH an overfit gate AND a non-default selection are present,
        the serving carrier drops the selection leg (the CLI-default
        0-row-misfold closer) — so ``'selection' not in`` the token FAILS on a
        revert of the ``selection=None`` seam — yet :func:`_tuple_digest` STILL
        separates on ``top_k`` so a different ``--top`` writes a distinct Parquet
        (#1336 store-address separation intact).
        """
        from brutex.engine.sweep_orchestrator import _tuple_digest

        grid = _grid(overfit=OverfitGateConfig(dsr_min=0.95), top_k=7)
        token = grid_gate_provenance(grid)
        # (a) SERVING carrier: gate_config only — the selection leg is dropped
        #     even with a non-default top_k (fails on revert to selection=<frag>).
        assert token is not None
        assert "gate_config" in token
        assert "selection" not in token
        # (b) STORE address: _tuple_digest STILL folds top_k, so the overfit grid
        #     at top_k=7 addresses a DIFFERENT store than the same overfit grid at
        #     the default (top_k=None) selection.
        overfit_default_top = _grid(overfit=OverfitGateConfig(dsr_min=0.95))
        tuple_ = _tuple()
        assert _tuple_digest(tuple_, grid) != _tuple_digest(tuple_, overfit_default_top)

    def test_selection_only_grid_carries_no_serving_token(self) -> None:
        """A non-default top_k alone does NOT gate the SERVING carrier (ungated).

        DEC-SWEEP-SERVING-GATE-PROVENANCE-001 scopes ``grid_gate_provenance`` to
        the GATE CONFIG only, so a ``--top``-only light run carries NO serving
        provenance and is "ungated" — the CLI-default 0-row-misfold closer. The
        store ADDRESS is SEPARABLE: :func:`_tuple_digest` STILL folds the
        selection fragment, so the on-disk Parquet path re-keys on top_k (#1336
        intact). Both pins live here so the decoupling is enforced together.
        """
        grid = _grid(top_k=7)
        # Serving carrier: selection-only ⇒ ungated (no token, no column).
        assert grid_gate_provenance(grid) is None
        # Store address: selection STILL re-keys the Parquet path (#1336).
        from brutex.engine.sweep_orchestrator import _tuple_digest, tuple_store_path

        default_grid = _grid()
        tuple_ = _tuple()
        assert _tuple_digest(tuple_, grid) != _tuple_digest(tuple_, default_grid)
        assert tuple_store_path(Path("/r"), tuple_, grid) != tuple_store_path(
            Path("/r"), tuple_, default_grid
        )

    def test_to_run_config_threads_token(self) -> None:
        """``SweepTuple.to_run_config`` stamps the grid token on the run config."""
        grid = _grid(overfit=OverfitGateConfig(dsr_min=0.95))
        tuple_ = _tuple()
        cfg = tuple_.to_run_config(grid)
        assert cfg.gate_provenance == grid_gate_provenance(grid)
        ungated = tuple_.to_run_config(_grid())
        assert ungated.gate_provenance is None


class TestConditionalColumnEmission:
    """The CONDITIONAL Parquet column — present ONLY in gated stores."""

    def test_ungated_store_has_no_column(self, tmp_path: Path) -> None:
        """An ungated store's schema carries NO gate_provenance column."""
        store = ResultsStore(path=tmp_path / "u.parquet")
        store.append(_row(1))
        store.flush()
        assert GATE_PROVENANCE_FIELD not in pl.read_parquet_schema(tmp_path / "u.parquet")
        assert store.gate_provenance_present is False

    def test_gated_rows_emit_column_and_roundtrip(self, tmp_path: Path) -> None:
        """Token-carrying rows emit the column; load reads the token back."""
        store = ResultsStore(path=tmp_path / "g.parquet")
        store.append(_row(1, gate_provenance=_TOKEN))
        store.flush()
        assert GATE_PROVENANCE_FIELD in pl.read_parquet_schema(tmp_path / "g.parquet")
        loaded = ResultsStore.load(tmp_path / "g.parquet")
        assert loaded.gate_provenance_present is True
        assert loaded.rows()[0].gate_provenance == _TOKEN

    def test_zero_row_gated_store_is_schema_discoverable(self, tmp_path: Path) -> None:
        """A store-level token emits the column at ZERO rows (the arm-2 carrier)."""
        store = ResultsStore(path=tmp_path / "e.parquet", gate_provenance=_TOKEN)
        store.flush()
        assert GATE_PROVENANCE_FIELD in pl.read_parquet_schema(tmp_path / "e.parquet")
        loaded = ResultsStore.load(tmp_path / "e.parquet")
        assert len(loaded) == 0
        assert loaded.gate_provenance_present is True

    def test_zero_row_ungated_store_stays_ungated(self, tmp_path: Path) -> None:
        """A tokenless empty store emits NO column — never a fabricated gate."""
        store = ResultsStore(path=tmp_path / "eu.parquet")
        store.flush()
        assert GATE_PROVENANCE_FIELD not in pl.read_parquet_schema(tmp_path / "eu.parquet")
        assert ResultsStore.load(tmp_path / "eu.parquet").gate_provenance_present is False

    def test_load_flush_roundtrip_preserves_gated_schema(self, tmp_path: Path) -> None:
        """A loaded 0-row gated store re-flushes BYTE-IDENTICAL (column kept)."""
        ResultsStore(path=tmp_path / "e.parquet", gate_provenance=_TOKEN).flush()
        before = (tmp_path / "e.parquet").read_bytes()
        loaded = ResultsStore.load(tmp_path / "e.parquet")
        loaded.flush()
        assert (tmp_path / "e.parquet").read_bytes() == before

    def test_pre_extension_row_loads_none(self, tmp_path: Path) -> None:
        """A store written without the column loads gate_provenance=None (Ruling 5)."""
        # Simulate the pre-extension generation: write an UNGATED store (no
        # column emitted — byte-identical to the pre-extension writer), then
        # load it back; the presence-tolerant read yields the honest None.
        store = ResultsStore(path=tmp_path / "old.parquet")
        store.append(_row(1))
        store.flush()
        loaded = ResultsStore.load(tmp_path / "old.parquet")
        assert loaded.rows()[0].gate_provenance is None


class TestAppendBackfill:
    """The directional gate_provenance backfill on a same-run_id re-append."""

    def test_none_to_token_backfills(self, tmp_path: Path) -> None:
        """A provenance-stamped redo upgrades a legacy None row (returns True)."""
        store = ResultsStore(path=tmp_path / "s.parquet")
        store.append(_row(1))
        backfilled = store.append(_row(1, gate_provenance=_TOKEN))
        assert backfilled is True
        kept = store.get(f"{1:064x}")
        assert kept is not None and kept.gate_provenance == _TOKEN

    def test_token_to_none_is_noop(self, tmp_path: Path) -> None:
        """A less-informed redo never erases a stamp (returns False)."""
        store = ResultsStore(path=tmp_path / "s.parquet")
        store.append(_row(1, gate_provenance=_TOKEN))
        noop = store.append(_row(1))
        assert noop is False
        kept = store.get(f"{1:064x}")
        assert kept is not None and kept.gate_provenance == _TOKEN

    def test_two_known_tokens_conflict(self, tmp_path: Path) -> None:
        """Two DIFFERENT known tokens at one address raise loudly (Golden Rule #4)."""
        store = ResultsStore(path=tmp_path / "s.parquet")
        store.append(_row(1, gate_provenance=_TOKEN))
        with pytest.raises(ResultsStoreError, match="gate-provenance conflict"):
            store.append(_row(1, gate_provenance=_OTHER_TOKEN))

    def test_non_backfill_field_still_conflicts(self, tmp_path: Path) -> None:
        """A metric divergence stays the strict run-identity conflict."""
        store = ResultsStore(path=tmp_path / "s.parquet")
        store.append(_row(1, sharpe=1.0))
        with pytest.raises(ResultsStoreError, match="run-identity conflict"):
            store.append(_row(1, gate_provenance=_TOKEN, sharpe=9.9))


class TestStoreProviderWiring:
    """The orchestrator store factory stamps the grid token on FRESH stores."""

    def test_gated_grid_fresh_store_carries_token(self, tmp_path: Path) -> None:
        """A gated grid's zero-survivor tuple still flushes a GATED Parquet."""
        grid = _grid(overfit=OverfitGateConfig(dsr_min=0.95))
        provider = make_store_for(tmp_path / "sweeps", grid)
        assert isinstance(provider, TupleStoreProvider)
        store = provider(_tuple())
        store.flush()  # ZERO rows persisted — the arm-2 shape
        assert GATE_PROVENANCE_FIELD in pl.read_parquet_schema(store.path)

    def test_ungated_grid_fresh_store_stays_ungated(self, tmp_path: Path) -> None:
        """An ungated grid's fresh store flushes the byte-identical legacy schema."""
        provider = make_store_for(tmp_path / "sweeps", _grid())
        assert isinstance(provider, TupleStoreProvider)
        store = provider(_tuple())
        store.flush()
        assert GATE_PROVENANCE_FIELD not in pl.read_parquet_schema(store.path)

    def test_load_or_create_loads_existing_over_token(self, tmp_path: Path) -> None:
        """An EXISTING store loads as-is; the on-disk schema stays authoritative."""
        dest = tmp_path / "existing.parquet"
        ResultsStore(path=dest).flush()  # an ungated 0-row store on disk
        store = load_or_create_store(dest, gate_provenance=_TOKEN)
        # Loaded: the fresh-store token is NOT adopted — the file's own
        # (ungated) schema presence governs, so a resume never mutates a
        # legacy store's gatedness.
        assert store.gate_provenance_present is False


class TestProjectorThreading:
    """``ranked_combo_to_result_row`` threads the token onto the row."""

    def test_projector_stamps_token(self) -> None:
        """The kwarg lands verbatim on the row; default None is byte-identical."""
        from brutex.engine.sweep_run import ranked_combo_to_result_row
        from brutex.ranking import RankedCombo

        rc = RankedCombo(
            mask=1,
            metrics=make_result_metrics(total_trades=0, winning_trades=0, losing_trades=0),
            composite_score=0.0,
            deflated_sharpe=None,
        )
        common: dict[str, object] = {
            "run_id": "e" * 64,
            "instrument": "NIFTY",
            "tf": "5m",
            "direction": "bull",
            "mode": "A",
            "bars": [],
            "bar_bits": [],
            "tf_minutes": 5,
            "sl_paisa": None,
            "tp_paisa": None,
        }
        stamped = ranked_combo_to_result_row(rc, gate_provenance=_TOKEN, **common)  # type: ignore[arg-type]
        assert stamped.gate_provenance == _TOKEN
        default = ranked_combo_to_result_row(rc, **common)  # type: ignore[arg-type]
        assert default.gate_provenance is None
