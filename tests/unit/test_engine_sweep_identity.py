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

    def test_max_sl_hit_rate_folds_active_only(self) -> None:
        """The opt-in SL-hit-rate knob (DEC-METRICS-KELLY-GROWTH-001) is an
        active-gate axis: a SET ceiling re-keys, the default None contributes
        nothing. Exercised through the structural ``_GateConfigInput`` seam —
        the real ``SweepGridConfig`` refuses a set value until Stage B wires
        the enforcement (the fold must already be correct so the flip is a
        one-line deletion there)."""

        class _FakeGrid:
            overfit = None
            max_sl_hit_rate: float | None = 0.5
            cvar_gate: CvarGateConfig | None = None
            stage3_enable = False
            stage3_top_k = 10_000
            stage3_max_fevals = 200
            stage3_max_restarts = 3

        assert _gate_config_fragment(_FakeGrid()) == {"max_sl_hit_rate": 0.5}

        class _FakeGridOff(_FakeGrid):
            max_sl_hit_rate = None

        assert _gate_config_fragment(_FakeGridOff()) is None

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

        from tests.fakes.sweep_ops_crate import build_fake_sweep_ops_crate

        # The shared §11-COMMON builder carries EVERY `_CRATE_API` symbol —
        # a partial fake would latch `crate_stale` and turn this class vacuous.
        crate = build_fake_sweep_ops_crate(version="fake")
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

    def test_flag_on_set_max_sl_hit_rate_routes_to_oracle(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """The train-b tier-2 seam gate at the WIRED seam (#1699 knob ×
        #1665 facade): a SET ``max_sl_hit_rate`` is outside the crate
        fragment law's domain, so flag-ON the wired seam serves the Python
        oracle VALUE — the identity key intact, never silently dropped by
        the knob-blind crate fragment."""
        from brutex.engine.sweep_identity import _gate_config_fragment_python

        self._arm_fake_crate(monkeypatch)
        try:
            grid = _grid(max_sl_hit_rate=0.5, stage3_enable=True)
            frag = _gate_config_fragment(grid)
            assert frag == _gate_config_fragment_python(grid)
            assert frag is not None
            assert frag["max_sl_hit_rate"] == 0.5
            # A clean REFUSAL, never a burn-in autorevert: a knob-blind crate
            # route would mismatch the oracle here and latch the process OFF.
            from brutex.engine.sweep_ops_rust import sweep_ops_rust_disabled

            assert sweep_ops_rust_disabled() is False
        finally:
            self._reset_facade()

    def test_flag_on_set_cvar_gate_routes_to_oracle(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """DEC-RANK-CVAR-TAIL-GATE-001 × DEC-RUST-SWEEP-OPS-001: a SET
        ``cvar_gate`` is OUTSIDE the crate fragment law's domain, so flag-ON the
        WHOLE computation routes to the Python oracle VALUE — the ``cvar``
        identity key intact, never silently dropped by the cvar-blind crate
        fragment (the ``max_sl_hit_rate`` beyond-crate seam remedy, mirrored;
        this drives the sweep_identity.py:132 early-return arm)."""
        from brutex.engine.sweep_identity import _gate_config_fragment_python

        self._arm_fake_crate(monkeypatch)
        try:
            grid = _grid(cvar_gate=CvarGateConfig(threshold_paisa=50_000, alpha_bps=250))
            frag = _gate_config_fragment(grid)
            assert frag == _gate_config_fragment_python(grid)
            assert frag is not None
            assert "cvar" in frag
            # A clean REFUSAL, never a burn-in autorevert: a cvar-blind crate
            # route would drop the key here and latch the process OFF.
            from brutex.engine.sweep_ops_rust import sweep_ops_rust_disabled

            assert sweep_ops_rust_disabled() is False
        finally:
            self._reset_facade()


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
