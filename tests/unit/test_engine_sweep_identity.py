"""Unit tests for the shared sweep-identity leaf (``brutex.engine.sweep_identity``).

Covers :func:`_gate_config_fragment` directly (default → None, overfit-only,
Stage-3-only inert-when-disabled, both active) so the leaf is 100% line+branch
without relying only on the transitive supervisor pins, and pins the re-export
shim so ``sweep_supervisor._gate_config_fragment`` stays the SAME function
object (DEC-SWEEP-TUPLE-DIGEST-GATE-IDENTITY-001, Golden Rule #11 COMMON).

``TestGateProvenanceToken`` (DEC-SWEEP-SERVING-GATE-PROVENANCE-001,
MIGRATION.md #1758) MOVED here from the gate-provenance carrier suite at the
2026-07-15 merge-reconcile: :func:`gate_provenance_token` is THIS module's
function, and the per-module coverage-gate discovery maps only the
``test_engine_sweep_identity*.py`` stem family — the leaf must be 100% with
its OWN suite.
"""

from __future__ import annotations

import json

from brutex.engine import sweep_identity, sweep_orchestrator, sweep_supervisor
from brutex.engine.sweep_identity import _gate_config_fragment, gate_provenance_token
from brutex.engine.sweep_orchestrator import SweepGridConfig, Timeframe
from brutex.ranking import OverfitGateConfig
from brutex.strategies.types import Side


def _grid(**overrides: object) -> SweepGridConfig:
    base: dict[str, object] = {
        "instruments": ["NIFTY"],
        "timeframes": [Timeframe("1minute", 1)],
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


class TestGateConfigFragment:
    def test_default_is_none(self) -> None:
        assert _gate_config_fragment(_grid()) is None

    def test_overfit_only(self) -> None:
        cfg = OverfitGateConfig(dsr_min=0.95)
        frag = _gate_config_fragment(_grid(overfit=cfg))
        assert frag == {
            "overfit": [
                cfg.dsr_min,
                cfg.weight,
                cfg.default_skew,
                cfg.default_excess_kurtosis,
            ]
        }

    def test_stage3_disabled_knobs_are_inert(self) -> None:
        # stage3_top_k set but stage3_enable False → the pass never runs → no leg.
        assert _gate_config_fragment(_grid(stage3_top_k=999)) is None

    def test_stage3_only_when_enabled(self) -> None:
        frag = _gate_config_fragment(
            _grid(stage3_enable=True, stage3_top_k=7, stage3_max_fevals=11, stage3_max_restarts=2)
        )
        assert frag == {"stage3": [7, 11, 2]}

    def test_both_active(self) -> None:
        cfg = OverfitGateConfig(dsr_min=0.90)
        frag = _gate_config_fragment(_grid(overfit=cfg, stage3_enable=True))
        assert frag is not None
        assert set(frag) == {"overfit", "stage3"}


class TestReExportShim:
    def test_supervisor_reexports_same_object(self) -> None:
        """The supervisor re-export is the SAME function object as the leaf's."""
        assert sweep_supervisor._gate_config_fragment is sweep_identity._gate_config_fragment

    def test_orchestrator_imports_same_object(self) -> None:
        assert sweep_orchestrator._gate_config_fragment is sweep_identity._gate_config_fragment


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
