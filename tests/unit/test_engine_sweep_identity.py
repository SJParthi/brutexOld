"""Unit tests for the shared sweep-identity leaf (``brutex.engine.sweep_identity``).

Covers :func:`_gate_config_fragment` directly (default → None, overfit-only,
Stage-3-only inert-when-disabled, both active) so the leaf is 100% line+branch
without relying only on the transitive supervisor pins, and pins the re-export
shim so ``sweep_supervisor._gate_config_fragment`` stays the SAME function
object (DEC-SWEEP-TUPLE-DIGEST-GATE-IDENTITY-001, Golden Rule #11 COMMON).
"""

from __future__ import annotations

from brutex.engine import sweep_identity, sweep_orchestrator, sweep_supervisor
from brutex.engine.sweep_identity import _gate_config_fragment
from brutex.engine.sweep_orchestrator import SweepGridConfig, Timeframe
from brutex.ranking import CvarGateConfig, OverfitGateConfig
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

    def test_cvar_only(self) -> None:
        # DEC-RANK-CVAR-TAIL-GATE-001: an ACTIVE CVaR gate contributes its
        # [alpha_bps, threshold_paisa] leg; None (the default) is elided —
        # the default-is-None pin above already covers the OFF arm.
        gate = CvarGateConfig(threshold_paisa=50_000, alpha_bps=250)
        assert _gate_config_fragment(_grid(cvar_gate=gate)) == {"cvar": [250, 50_000]}

    def test_all_three_active(self) -> None:
        frag = _gate_config_fragment(
            _grid(
                overfit=OverfitGateConfig(dsr_min=0.90),
                stage3_enable=True,
                cvar_gate=CvarGateConfig(threshold_paisa=1),
            )
        )
        assert frag is not None
        assert set(frag) == {"overfit", "cvar", "stage3"}


class TestReExportShim:
    def test_supervisor_reexports_same_object(self) -> None:
        """The supervisor re-export is the SAME function object as the leaf's."""
        assert sweep_supervisor._gate_config_fragment is sweep_identity._gate_config_fragment

    def test_orchestrator_imports_same_object(self) -> None:
        assert sweep_orchestrator._gate_config_fragment is sweep_identity._gate_config_fragment
