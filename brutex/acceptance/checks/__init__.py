"""Check registry — id → runner, run in a fixed operator-facing order.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756). Every runner takes the
shared :class:`~brutex.acceptance.context.AcceptanceContext` and returns a
:class:`~brutex.acceptance.model.CheckResult`. Fencing (a raising runner →
verdict ``ERROR``, the run continues) lives in :mod:`brutex.acceptance.run`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from brutex.acceptance.context import AcceptanceContext
from brutex.acceptance.model import CheckResult


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
