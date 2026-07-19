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


# ────────── DEC-RUST-SWEEP-OPS-001 — slice-1 routing (wheel-free) ──────────


class TestSweepOpsRouting:
    """Flag-ON routing parity for the gate-config fragment law.

    Wheel-free: a behavior-correct FAKE crate is injected by patching the
    facade's ``importlib`` binding. Flag ON must be value-identical to the
    flag-OFF Python law (which every OTHER test in this file drives) — this
    law feeds RUN IDENTITY, so identity is the whole contract.
    """

    @staticmethod
    def _arm_fake_crate(monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from types import SimpleNamespace

        from brutex.engine import sweep_ops_rust

        def gate_config_fragment(
            overfit: tuple[float, float, float, float] | None,
            stage3_enable: bool,
            top_k: int,
            max_fevals: int,
            max_restarts: int,
        ) -> tuple[object, object]:
            stage3 = (top_k, max_fevals, max_restarts) if stage3_enable else None
            return (overfit, stage3)

        crate = SimpleNamespace(
            __version__="fake",
            join_path_segments=lambda base, segments: base,
            gate_config_fragment=gate_config_fragment,
            stall_scaled_timeout=lambda *args: (300.0, False),
        )
        monkeypatch.setattr(
            sweep_ops_rust,
            "importlib",
            SimpleNamespace(import_module=lambda name: crate),
        )
        monkeypatch.setenv(sweep_ops_rust.SWEEP_OPS_RUST_ENV, "1")
        sweep_ops_rust.reset_sweep_ops_rust_for_tests()

    @staticmethod
    def _reset_facade() -> None:
        from brutex.engine.sweep_ops_rust import reset_sweep_ops_rust_for_tests

        reset_sweep_ops_rust_for_tests()

    def test_flag_on_matches_python_law_all_shapes(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from brutex.engine.sweep_identity import _gate_config_fragment_python

        self._arm_fake_crate(monkeypatch)
        try:
            grids = [
                _grid(),  # both off → None
                _grid(overfit=OverfitGateConfig(dsr_min=0.95)),  # overfit only
                _grid(  # stage3 only
                    stage3_enable=True,
                    stage3_top_k=7,
                    stage3_max_fevals=11,
                    stage3_max_restarts=2,
                ),
                _grid(  # both legs
                    overfit=OverfitGateConfig(dsr_min=0.90), stage3_enable=True
                ),
            ]
            for grid in grids:
                assert _gate_config_fragment(grid) == _gate_config_fragment_python(grid)
        finally:
            self._reset_facade()
