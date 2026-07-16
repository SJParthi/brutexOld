"""The STRUCTURAL stage-callable contract + the staged-stage vocabulary — a LEAF.

DEC-STAGED-SEQUENCER-SEAMS-001 (WS-B of the dual-vendor orchestration track),
implementing ADR-0021 §12 invariant 4 per the frozen design contract
`DEC-DUAL-VENDOR-PIPELINE-001` (assurance CLEAR-TO-FREEZE 2026-07-15; the
operator rulings were relayed via the coordinator — not a direct-user approval
in this session). The sequencer (:mod:`brutex.dashboard.pipeline_pull`) gains
ADDITIVE staged callables, each typed at the :class:`StageOutcome` STRUCTURAL
protocol below — never at the concrete Groww report types (the feed-track F3
ruling: the concrete report shapes are SATISFYING INSTANCES, never the
contract), so a second vendor's spine can bind its own outcome shapes against
the ONE sequencer without importing a Groww module.

This module is a dependency-free LEAF (the :mod:`~brutex.dashboard.pipeline_verdict`
house pattern): it imports NOTHING from :mod:`brutex.dashboard` — keep it that
way. Both the sequencer and any vendor binding import it without a cycle.

## The contract (ADR-0021 §12 invariant 4)

A stage callable returns an object satisfying :class:`StageOutcome`:

* **``ok`` verdict** — the ONE structurally-required member. ``True`` = the
  stage settled clean; ``False`` = the stage FAILED its gate (the sequencer
  stops fail-closed, ``stopped_after=<stage token>``); ``None`` = the stage
  RAN but had nothing to do (the
  :class:`~brutex.data.greeks_enrich_pass.GreeksEnrichOutcome` "did not run"
  semantics — e.g. a dry-run pre-gate skip). ``None`` is NOT a failure — the
  gate law is :func:`stage_outcome_ok` (``ok is not False``).
* **Failure counts** — the concrete outcome types carry their own
  failed/still-failed tallies (``still_failed_windows`` / ``failed_count`` /
  ``errored_targets`` …); the protocol deliberately does NOT unify their
  names — the sequencer's gate consumes only the ``ok`` verdict, and the
  counts stay on the vendor report for its own note/audit surfaces (forcing
  one field name across the five shipped Groww shapes would be a
  non-additive rename, not a protocol).
* **Halt propagation (behavioral invariant — ADR-0021 §12 invariant 3):** a
  stage callable NEVER swallows a vendor-halt / ``LakeWriteHalt`` into the
  outcome — halts are RAISED (typed exceptions), so the sequencer sees
  either a healed spine or a halt, never a silently-absorbed global stop.
  This is a review-time invariant on the callable, not a field.
* **NO stopped token (the N2 ruling, feed-track review fold):** the
  ``stopped_after`` token is SEQUENCER-owned — minted from stage POSITION at
  the gate; vendor reports never synthesize one, so the protocol carries no
  stopped-reason member.

The five shipped Groww report shapes —
:class:`~brutex.dashboard.pull_controller.BulkSpotReport`,
:class:`~brutex.dashboard.metadata_pull.MetadataPullReport`,
:class:`~brutex.dashboard.composite_pull.CompositePullReport`,
:class:`~brutex.dashboard.pipeline_prebuild.PrebuildOutcome`, and
:class:`~brutex.data.greeks_enrich_pass.GreeksEnrichOutcome` — ALL satisfy
the protocol (static assertion tests in ``tests/unit/test_stage_outcome.py``
pin it): the first three gained additive ``ok`` properties whose expressions
the sequencer's ``spot_phase_ok``-family gates now delegate to (CLAUDE.md §11
COMMON — one predicate, one home; the reports cannot import the sequencer, so
the expression lives on the report and the gate is the thin wrapper).

## The staged-stage vocabulary (ADR-0021 Decision 2 + §10)

:data:`STAGED_STAGE_ORDER` is the CANONICAL stage order every vendor's chain
is a SUBSET of. A vendor binds a stage by passing its callable; a ``None``
callable means the stage is ABSENT from that vendor's graph — never invoked,
scored VACUOUSLY-SETTLED with the DISTINCT :data:`STAGE_STATUS_ABSENT` token
(the ABSENT-vs-FAILED contract, ADR-0021 Decision 3 / CAPA-02 — an absent
stage is never conflated with a failed one, and the strict settle gate never
withholds the Greeks/post stages over absent stages). The tokens double as
the staged ``stopped_after`` vocabulary (sequencer-minted from stage
position; ``spot`` / ``metadata`` deliberately equal the legacy phase tokens).
"""

from __future__ import annotations

from typing import Final, Protocol, runtime_checkable

#: The staged stage tokens (ADR-0021 Decision 2 — the canonical order's names).
#: ``spot`` + ``metadata`` are deliberately the SAME strings as the legacy
#: ``PHASE_SPOT`` / ``PHASE_METADATA`` tokens: one vocabulary, no synonyms.
STAGE_SPOT_DAILY: Final[str] = "spot_daily"
STAGE_SPOT: Final[str] = "spot"
STAGE_SPOT_PREBUILD: Final[str] = "spot_prebuild"
STAGE_METADATA: Final[str] = "metadata"
STAGE_FUTURES: Final[str] = "futures"
STAGE_FUTURES_PREBUILD: Final[str] = "futures_prebuild"
STAGE_OPTIONS: Final[str] = "options"
STAGE_GREEKS: Final[str] = "greeks"

#: The CANONICAL stage order (ADR-0021 Decision 2): every vendor's chain is a
#: SUBSET of this sequence — an absent capability produces NO node. The
#: sequencer walks exactly this order; there is no scheduler to misorder.
STAGED_STAGE_ORDER: Final[tuple[str, ...]] = (
    STAGE_SPOT_DAILY,
    STAGE_SPOT,
    STAGE_SPOT_PREBUILD,
    STAGE_METADATA,
    STAGE_FUTURES,
    STAGE_FUTURES_PREBUILD,
    STAGE_OPTIONS,
    STAGE_GREEKS,
)

#: Stage STATUS tokens (the per-stage report vocabulary):
#: ``absent`` — the stage has NO callable on this vendor's graph; NEVER
#: invoked; VACUOUSLY-SETTLED (``ok=True`` semantics — the ABSENT-vs-FAILED
#: contract, ADR-0021 Decision 3). DISTINCT from ``failed`` by construction.
STAGE_STATUS_ABSENT: Final[str] = "absent"
#: ``settled`` — the stage ran and its gate passed (:func:`stage_outcome_ok`).
STAGE_STATUS_SETTLED: Final[str] = "settled"
#: ``failed`` — the stage ran and its gate FAILED (``ok is False``); the
#: sequencer stops fail-closed with ``stopped_after=<this stage's token>``.
STAGE_STATUS_FAILED: Final[str] = "failed"
#: ``skipped`` — the stage IS bound on this vendor's graph but never ran
#: because an EARLIER stage failed / the market-hours belt deferred the
#: chain. Honestly distinct from ``absent`` (capability-unbound) — a skipped
#: stage re-runs next launch; an absent one never exists.
STAGE_STATUS_SKIPPED: Final[str] = "skipped"


@runtime_checkable
class StageOutcome(Protocol):
    """The STRUCTURAL contract every staged callable's return satisfies.

    ADR-0021 §12 invariant 4 (the feed-track F3 ruling): the sequencer's
    staged params are typed HERE, never at the concrete Groww report types.
    The one structurally-required member is the read-only ``ok`` verdict;
    the failure-count and halt-propagation semantics are documented on the
    module (they are per-shape fields / behavioral invariants, not
    uniformly-named members). ``@runtime_checkable`` supports the static
    assertion tests' ``isinstance`` pins; the REAL contract enforcement is
    ``mypy --strict``'s structural check at every binding site.
    """

    @property
    def ok(self) -> bool | None:
        """The stage verdict: ``True`` clean, ``False`` FAILED, ``None`` no-op."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


def stage_outcome_ok(outcome: StageOutcome) -> bool:
    """The ONE staged gate law: only an explicit ``ok is False`` fails a stage.

    ``True`` and ``None`` both pass — ``None`` is the honest "the stage ran
    but had nothing to do" verdict (the
    :class:`~brutex.data.greeks_enrich_pass.GreeksEnrichOutcome` semantics:
    a dry-run / pre-gate skip is not a failure; the gate detects FAILURE
    signals, not absence of work — the ``spot_phase_ok`` philosophy).
    """
    return outcome.ok is not False


__all__ = [
    "STAGED_STAGE_ORDER",
    "STAGE_FUTURES",
    "STAGE_FUTURES_PREBUILD",
    "STAGE_GREEKS",
    "STAGE_METADATA",
    "STAGE_OPTIONS",
    "STAGE_SPOT",
    "STAGE_SPOT_DAILY",
    "STAGE_SPOT_PREBUILD",
    "STAGE_STATUS_ABSENT",
    "STAGE_STATUS_FAILED",
    "STAGE_STATUS_SETTLED",
    "STAGE_STATUS_SKIPPED",
    "StageOutcome",
    "stage_outcome_ok",
]
