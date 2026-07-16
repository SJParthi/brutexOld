"""Check registry — id → runner, run in a fixed operator-facing order.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756). Every runner takes the
shared :class:`~brutex.acceptance.context.AcceptanceContext` and returns a
:class:`~brutex.acceptance.model.CheckResult`. Fencing (a raising runner →
verdict ``ERROR``, the run continues) lives in :mod:`brutex.acceptance.run`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Final

from brutex.acceptance.context import AcceptanceContext
from brutex.acceptance.model import (
    AbstainReason,
    CheckResult,
    Verdict,
    make_check,
    make_coverage,
    make_subcheck,
)

#: Public surface — the check registry, its ordered id tuple (consumed by
#: :mod:`brutex.cli` + :mod:`brutex.acceptance.run` for ``--checks`` selection),
#: and the fingerprinted-run helper.
__all__ = [
    "ALL_CHECK_IDS",
    "CHECK_REGISTRY",
    "run_fingerprinted_check",
]


def run_fingerprinted_check(
    ctx: AcceptanceContext,
    *,
    check_id: str,
    sub_id: str,
    paths_fn: Callable[[], Sequence[Path]],
    body: Callable[[], CheckResult],
    on_retry: Callable[[], None] | None = None,
) -> CheckResult:
    """Run one check body under the §12.7 stat-fingerprint retry-once TOCTOU law.

    The check's ACTUAL sampled input files are stat-tokened before AND after
    the compute (O(sampled files) stats); a move retries the WHOLE body ONCE
    (*on_retry* fires first so the check can drop memoised state — the retry
    genuinely re-reads, review round 2 fix 4); a second move ⇒ the check
    ABSTAINS ``input_moved_during_check`` naming the movers — never a PASS
    over a torn read (review round 1 MAJOR-1: the law existed but had ZERO
    production callers).
    """
    result, fingerprint, ok = ctx.run_fingerprinted(paths_fn, body, on_retry=on_retry)
    if not ok:
        sub = make_subcheck(
            sub_id,
            Verdict.ABSTAIN,
            units_checked=0,
            evidence={
                "reason": "sampled input files moved on BOTH attempts",
                "movers": list(fingerprint.get("movers", [])),
            },
            abstain_reason=AbstainReason.INPUT_MOVED_DURING_CHECK,
            repro=f"brutex verify-acceptance --checks {check_id} --seed {ctx.seed}",
        )
        coverage = make_coverage(mode="none", population=0, examined=0, seed=ctx.seed)
        return make_check(check_id, [sub], coverage=coverage, input_fingerprint=fingerprint)
    checked: CheckResult = replace(result, input_fingerprint=dict(fingerprint))
    return checked


def _run_unique(ctx: AcceptanceContext) -> CheckResult:
    from brutex.acceptance.checks import unique

    return unique.run(ctx)


def _run_tf(ctx: AcceptanceContext) -> CheckResult:
    from brutex.acceptance.checks import tf

    return tf.run(ctx)


def _run_pull(ctx: AcceptanceContext) -> CheckResult:
    from brutex.acceptance.checks import pull

    return pull.run(ctx)


def _run_rate(ctx: AcceptanceContext) -> CheckResult:
    from brutex.acceptance.checks import rate

    return rate.run(ctx)


def _run_movers(ctx: AcceptanceContext) -> CheckResult:
    from brutex.acceptance.checks import movers

    return movers.run(ctx)


def _run_sanity(ctx: AcceptanceContext) -> CheckResult:
    from brutex.acceptance.checks import sanity

    return sanity.run(ctx)


def _run_latency(ctx: AcceptanceContext) -> CheckResult:
    from brutex.acceptance.checks import latency

    return latency.run(ctx)


#: Fixed execution + report order (lazy imports keep ``--checks unique``
#: wheel-light — the dashboard app import lives inside movers/latency only).
CHECK_REGISTRY: Final[dict[str, Callable[[AcceptanceContext], CheckResult]]] = {
    "unique": _run_unique,
    "tf": _run_tf,
    "pull": _run_pull,
    "rate": _run_rate,
    "movers": _run_movers,
    "sanity": _run_sanity,
    "latency": _run_latency,
}

ALL_CHECK_IDS: Final[tuple[str, ...]] = tuple(CHECK_REGISTRY)
