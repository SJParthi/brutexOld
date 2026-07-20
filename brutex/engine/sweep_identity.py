"""Shared sweep-identity fragments (COMMON, Golden Rule #11).

ONE home for the gate-config identity fragment consumed by BOTH the
supervisor (``grid_identity`` / ``tuple_cache_key``, #1330) AND the
orchestrator (``_tuple_digest`` store address,
DEC-SWEEP-TUPLE-DIGEST-GATE-IDENTITY-001). Extracted to a leaf because
:mod:`brutex.engine.sweep_supervisor` already imports FROM
:mod:`brutex.engine.sweep_orchestrator`, so the orchestrator cannot import
the fragment back from the supervisor without a cycle.

The fragment's input is typed against the minimal :class:`_GateConfigInput`
Protocol below — the gate/Stage-3 attributes it actually reads — so this leaf
imports NOTHING from :mod:`brutex.engine.sweep_orchestrator` (not even under
``TYPE_CHECKING``). The module-import graph is therefore provably acyclic to
BOTH the runtime and static analysers (an earlier ``TYPE_CHECKING`` back-import
of :class:`SweepGridConfig` tripped CodeQL's ``py/unsafe-cyclic-import`` even
though ``from __future__ import annotations`` made it a no-op at runtime).
:class:`SweepGridConfig` satisfies the Protocol structurally, so the two call
sites pass their grid unchanged and the frozen ``grid_identity`` /
``tuple_cache_key`` / ``_tuple_digest`` goldens are byte-unmoved.
"""

from __future__ import annotations

from typing import Any, Protocol

# DEC-RUST-SWEEP-OPS-001 (slice 1): the default-OFF Rust routing facade. The
# wired fragment gates on `sweep_ops_rust_enabled()` FIRST so the flag-OFF
# default path is byte-identical (including attribute-access order); the
# verbatim Python law below is the live oracle. This law feeds RUN IDENTITY
# (`grid_identity` / `tuple_cache_key` / `_tuple_digest`) — the facade's
# burn-in + exact-type gates are the anti-drift teeth. A SET
# `max_sl_hit_rate` (DEC-METRICS-KELLY-GROWTH-001 — the identity axis
# post-dates the crate's fragment law) is refused to the oracle INSIDE the
# facade (`gate_fragment_beyond_crate`; the train-b tier-2 seam remedy).
from brutex.engine.sweep_ops_rust import routed_gate_fragment, sweep_ops_rust_enabled
from brutex.ranking import OverfitGateConfig


class _GateConfigInput(Protocol):
    """Structural view of the knobs :func:`_gate_config_fragment` folds.

    Captures ONLY the gate + Stage-3 attributes the fragment reads, so the leaf
    needs no import of the orchestrator-owned :class:`SweepGridConfig` (which
    imports FROM here). Members are read-only ``property`` declarations so a
    FROZEN :class:`SweepGridConfig` (its attributes are read-only) satisfies the
    Protocol structurally — a writable attribute member would be invariant and
    reject the frozen dataclass.

    Member bodies ``raise NotImplementedError`` (the
    :mod:`brutex.orchestrator.supervisor` Protocol-stub form): a docstring-only
    body implicitly returns ``None`` and trips pyright's ``reportReturnType``
    on the non-``None`` members, while a trailing ``...`` after the docstring
    is CodeQL ``py/ineffectual-statement``. The raise is coverage-excluded
    (``exclude_lines``) and never executes — Protocol members are structural.
    """

    @property
    def overfit(self) -> OverfitGateConfig | None:
        """The overfit gate config, or ``None`` when the gate is off."""
        raise NotImplementedError

    @property
    def max_sl_hit_rate(self) -> float | None:
        """The opt-in SL-hit-rate gate ceiling, or ``None`` when the gate is off.

        DEC-METRICS-KELLY-GROWTH-001: an ACTIVE (set) knob re-keys the
        per-tuple store address via the identity fragment; enforcement is
        ranking Door 6 (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B —
        :func:`brutex.ranking.passes_gates` / :func:`brutex.ranking.gate_reasons`).
        The default ``None`` contributes nothing, so ungated digests are
        byte-identical.
        """
        raise NotImplementedError

    @property
    def stage3_enable(self) -> bool:
        """Whether the Stage-3 refinement pass is enabled."""
        raise NotImplementedError

    @property
    def stage3_top_k(self) -> int:
        """Number of top finalists Stage-3 refines."""
        raise NotImplementedError

    @property
    def stage3_max_fevals(self) -> int:
        """Per-finalist function-evaluation budget for Stage-3."""
        raise NotImplementedError

    @property
    def stage3_max_restarts(self) -> int:
        """Per-finalist restart budget for Stage-3."""
        raise NotImplementedError


def _gate_config_fragment(grid: _GateConfigInput) -> dict[str, Any] | None:
    """Return the canonical GATE-config identity fragment, or ``None`` at defaults.

    Coordinator Ruling 3 (DEC-SWEEP-UNKILLABLE-CORE-001): the overfit gate +
    the Stage-3 refinement knobs change what a persisted tuple's rows MEAN
    (gated-institutional vs light-research), so they belong in BOTH the run
    identity (:func:`grid_identity`) and the per-tuple cache key
    (:func:`tuple_cache_key`) — otherwise a gated re-run over a light-run
    results tree resumes the same checkpoint and cache-skips the ungated
    tuples, serving ungated rows as institutional winners.

    A SANCTIONED re-key that stays BYTE-STABLE at defaults (Golden Rule #4):
    the fragment is emitted ONLY when a gate is actually active — overfit set
    (``overfit is not None``) and/or the Stage-3 pass enabled
    (``stage3_enable``). A disabled gate's knobs are INERT (no pass runs, no
    row changes), so an ungated grid contributes NOTHING and hashes exactly as
    it did before this axis existed (the frozen ``grid_identity`` /
    ``tuple_cache_key`` goldens are unmoved — every golden grid is at gate
    defaults). All legs flattened to JSON-native scalars.
    """
    if not sweep_ops_rust_enabled():
        return _gate_config_fragment_python(grid)
    overfit = grid.overfit
    overfit_values = (
        None
        if overfit is None
        else (
            overfit.dsr_min,
            overfit.weight,
            overfit.default_skew,
            overfit.default_excess_kurtosis,
        )
    )
    return routed_gate_fragment(
        overfit_values,
        bool(grid.stage3_enable),
        (grid.stage3_top_k, grid.stage3_max_fevals, grid.stage3_max_restarts),
        max_sl_hit_rate=grid.max_sl_hit_rate,
        python_law=lambda: _gate_config_fragment_python(grid),
    )


def _gate_config_fragment_python(grid: _GateConfigInput) -> dict[str, Any] | None:
    """The verbatim Python fragment law — the live parity oracle
    (DEC-RUST-SWEEP-OPS-001; retained until the final slice's default flip)."""
    fragment: dict[str, Any] = {}
    overfit = grid.overfit
    if overfit is not None:
        # Explicit float() normalization: the recount-recovery reader
        # (sweep_verify.recorded_overfit_from_audit) reconstructs this leg
        # from the audit JSON as floats, so a future int-carrying
        # OverfitGateConfig (e.g. weight=0) must not mint an int here and
        # split the digest from the recovered replay
        # (DEC-SWEEP-OVERFIT-RECOUNT-RECOVERY-001 review-fix NIT).
        fragment["overfit"] = [
            float(overfit.dsr_min),
            float(overfit.weight),
            float(overfit.default_skew),
            float(overfit.default_excess_kurtosis),
        ]
    if grid.max_sl_hit_rate is not None:
        # The opt-in SL-hit-rate gate (DEC-METRICS-KELLY-GROWTH-001) is an
        # active-gate axis exactly like overfit: a SET ceiling changes what a
        # tuple's persisted rows MEAN, so it re-keys; the default None
        # contributes NOTHING and every existing digest is byte-identical
        # (the frozen 5b020e892247deac default-digest pin). Enforcement is
        # ranking Door 6 (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B); the
        # fragment arm itself stays covered via a Protocol fake.
        fragment["max_sl_hit_rate"] = grid.max_sl_hit_rate
    if grid.stage3_enable:
        # The Stage-3 knobs matter to the result ONLY when the pass runs; a
        # disabled pass leaves them inert, so they never re-key a light grid.
        fragment["stage3"] = [
            grid.stage3_top_k,
            grid.stage3_max_fevals,
            grid.stage3_max_restarts,
        ]
    return fragment or None


__all__ = ["_gate_config_fragment"]
