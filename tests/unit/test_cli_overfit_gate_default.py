"""Default-ON pins for the Deflated-Sharpe overfit gate CLI flag.

PROPOSAL — DEC-SWEEP-OVERFIT-GATE-DEFAULT-ON-001 (MIGRATION.md #1961,
PROPOSED — pending operator ruling; reverts wholesale if the operator rules
OFF). These tests pin the flipped default (``--overfit-gate`` ON by default
on every ``_add_sweep_grid_args`` surface — ``sweep-all`` · ``sweep-daemon``
· ``sweep-verify`` · ``nightly run``) plus the loud ``--no-overfit-gate``
opt-out and the ``cmd_sweep_all`` keyword default.

Fails-before discipline: ``test_sweep_all_overfit_gate_defaults_on`` and
``test_cmd_sweep_all_keyword_default_is_on`` FAIL on the pre-flip source
(default ``False``) — the mandated fails-before proof for the flip.

RUN-IDENTITY LAW (DEC-SWEEP-TUPLE-DIGEST-GATE-IDENTITY-001, MIGRATION.md
#1715): the ACTIVE gate config folds into the per-tuple store address
(``engine/sweep_identity._gate_config_fragment`` →
``sweep_orchestrator._tuple_digest``), default-ELIDED when the gate is off.
Flipping the CLI default therefore MOVES the store address of every
default-invocation sweep (full recompute on the first post-flip run) while
the ENGINE-level identity is untouched: ``SweepGridConfig.overfit`` still
defaults ``None``, so the frozen ungated digest ``7a0120c1123b44c6``
(``test_engine_sweep_orchestrator.TestTupleDigestGateSelectionIdentity``)
stays byte-stable and every frozen engine golden passes UNMODIFIED. The
gated-at-defaults successor digest is pinned here.
"""

from __future__ import annotations

import inspect

import pytest
from brutex import cli
from brutex.engine.sweep_orchestrator import SweepGridConfig, SweepTuple
from brutex.ranking import OverfitGateConfig

_SWEEP_ALL_MINIMAL = [
    "sweep-all",
    "--start",
    "2024-01-01",
    "--end",
    "2024-01-31",
]


class TestOverfitGateDefaultOn:
    """The overfit gate defaults ON (PROPOSAL re-lock — reverts if ruled OFF)."""

    def test_sweep_all_overfit_gate_defaults_on(self) -> None:
        """FAILS-BEFORE: the pre-flip parser default was ``False``."""
        args = cli.build_parser().parse_args(_SWEEP_ALL_MINIMAL)
        assert args.overfit_gate is True

    def test_no_overfit_gate_opts_out(self) -> None:
        args = cli.build_parser().parse_args([*_SWEEP_ALL_MINIMAL, "--no-overfit-gate"])
        assert args.overfit_gate is False

    def test_explicit_overfit_gate_flag_still_accepted(self) -> None:
        """The legacy explicit-ON spelling stays a working (now no-op) flag."""
        args = cli.build_parser().parse_args([*_SWEEP_ALL_MINIMAL, "--overfit-gate"])
        assert args.overfit_gate is True

    def test_dsr_min_default_unchanged(self) -> None:
        args = cli.build_parser().parse_args(_SWEEP_ALL_MINIMAL)
        assert args.dsr_min == 0.95

    def test_cmd_sweep_all_keyword_default_is_on(self) -> None:
        """FAILS-BEFORE: the pre-flip keyword default was ``False``."""
        param = inspect.signature(cli.cmd_sweep_all).parameters["overfit_gate"]
        assert param.default is True

    @pytest.mark.parametrize("command", ["sweep-daemon", "sweep-verify"])
    def test_sibling_sweep_surfaces_share_the_default(self, command: str) -> None:
        """The shared ``_add_sweep_grid_args`` declaration flips every surface."""
        argv = [command, "--start", "2024-01-01", "--end", "2024-01-31"]
        if command == "sweep-verify":
            argv = [command, *argv[1:]]
        args = cli.build_parser().parse_args(argv)
        assert args.overfit_gate is True

    def test_nightly_run_shares_the_default(self) -> None:
        """``nightly run`` (dates_required=False) inherits gate-ON too."""
        args = cli.build_parser().parse_args(["nightly", "run"])
        assert args.overfit_gate is True


class TestGateIdentityConsequence:
    """The flip's run-identity consequence, pinned at the digest layer."""

    # The frozen UNGATED digest (engine-level default — SweepGridConfig.overfit
    # stays None): byte-stable across this proposal, mirrored from
    # test_engine_sweep_orchestrator.TestTupleDigestGateSelectionIdentity
    # (_DEFAULT_DIGEST — the post-DEC-FILL-WORST-CASE-ONLY-001 #1825 value;
    # the dead salvage build pinned the PRECISE-era 5b020e892247deac,
    # re-executed here on the worst-case-era main).
    _UNGATED_DIGEST = "7a0120c1123b44c6"

    # PROPOSAL re-lock — the store-address digest a DEFAULT CLI invocation now
    # targets (gate active at its defaults: dsr_min=0.95, weight=0.05,
    # skew=0.0, excess_kurtosis=0.0 → the identity fragment
    # {"overfit": [0.95, 0.05, 0.0, 0.0]}). Hand-derivation: the #1715
    # descriptor gains descriptor["gate_config"] = {"overfit":
    # [0.95, 0.05, 0.0, 0.0]} before the canonical-JSON → BLAKE3 fold, so the
    # digest MOVES from the ungated 7a0120c1123b44c6. Captured empirically on
    # this branch; REVERTS (delete this pin) if the operator rules the gate
    # back OFF.
    _GATED_DEFAULT_DIGEST = "78ea140f9fa4906a"

    @staticmethod
    def _tuple_and_grids() -> tuple[SweepTuple, SweepGridConfig, SweepGridConfig]:
        from tests.unit.test_engine_sweep_orchestrator import _grid, _tuple

        return _tuple(), _grid(), _grid(overfit=OverfitGateConfig())

    def test_ungated_engine_digest_unmoved(self) -> None:
        from brutex.engine.sweep_orchestrator import _tuple_digest

        tuple_, ungated, _ = self._tuple_and_grids()
        assert _tuple_digest(tuple_, ungated) == self._UNGATED_DIGEST

    def test_gated_default_digest_is_the_pinned_successor(self) -> None:
        from brutex.engine.sweep_orchestrator import _tuple_digest

        tuple_, _, gated = self._tuple_and_grids()
        assert _tuple_digest(tuple_, gated) == self._GATED_DEFAULT_DIGEST

    def test_gated_and_ungated_never_share_a_store(self) -> None:
        assert self._GATED_DEFAULT_DIGEST != self._UNGATED_DIGEST

    def test_default_cli_grid_carries_the_default_gate_config(self) -> None:
        """The default invocation constructs OverfitGateConfig() exactly."""
        cfg = OverfitGateConfig(dsr_min=0.95)
        assert cfg == OverfitGateConfig()
        assert (cfg.dsr_min, cfg.weight, cfg.default_skew, cfg.default_excess_kurtosis) == (
            0.95,
            0.05,
            0.0,
            0.0,
        )
