"""Shared sweep-identity fragments (COMMON, Golden Rule #11).

ONE home for the gate-config identity fragment consumed by BOTH the
supervisor (``grid_identity`` / ``tuple_cache_key``, #1330) AND the
orchestrator (``_tuple_digest`` store address,
DEC-SWEEP-TUPLE-DIGEST-GATE-IDENTITY-001). Extracted to a leaf because
:mod:`brutex.engine.sweep_supervisor` already imports FROM
:mod:`brutex.engine.sweep_orchestrator`, so the orchestrator cannot import
the fragment back from the supervisor without a cycle. :class:`SweepGridConfig`
is imported ONLY under ``TYPE_CHECKING`` (the body does pure attribute reads,
never constructs it), so at runtime this leaf imports nothing from the
orchestrator — the edge stays acyclic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from brutex.engine.sweep_orchestrator import SweepGridConfig


def _gate_config_fragment(grid: SweepGridConfig) -> dict[str, Any] | None:
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
    fragment: dict[str, Any] = {}
    overfit = grid.overfit
    if overfit is not None:
        fragment["overfit"] = [
            overfit.dsr_min,
            overfit.weight,
            overfit.default_skew,
            overfit.default_excess_kurtosis,
        ]
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
