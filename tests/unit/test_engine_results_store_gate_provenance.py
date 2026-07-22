"""Store-side gate-provenance carrier suite (DEC-SWEEP-SERVING-GATE-PROVENANCE-001).

MOVED here from ``test_engine_gate_provenance_carrier.py`` at the 2026-07-15
merge-reconcile (MIGRATION.md #1758): the per-module coverage-gate discovery
maps ``brutex/engine/results_store.py`` to the ``test_engine_results_store*.py``
stem family only, and these classes carry the store-level carrier branches —
the CONDITIONAL ``gate_provenance`` Parquet column (present ONLY in gated
stores; ungated bytes byte-identical to the pre-column format) and the
directional append backfill / two-known-token refusal
(``_merge_gate_provenance``) — so the module must be 100% with its OWN suite.
The write-side WIRING pins (token/grid/provider/projector) stay in the
carrier suite.
"""

from __future__ import annotations

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
from brutex.engine.sweep_identity import gate_provenance_token


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
