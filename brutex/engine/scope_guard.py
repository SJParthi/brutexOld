"""Engine-entry symbol scope guard — the corporate-action future-blocker gate.

The ONE source of truth for which symbols the brute-force ENGINE may sweep
(DEC-CORP-ACTION-GATE-001, ADR-0016 — the 2026-07-03 audit finding #5).
BruteX has **ZERO corporate-action adjustment code** (repo-verified: no
split/bonus/adjustment-factor layer exists anywhere in ``brutex/``). That is
CORRECT for the locked 3-index engine scope — an index divisor absorbs its
constituents' corporate actions (index-methodology claim, **UNVERIFIED**
against a primary NSE Indices / BSE methodology document — Golden Rule #1;
see ADR-0016 §1: the gate does not depend on it, because raw single-stock
candles being unadjusted across ex-dates is the operative hazard either
way) — but it would be CATASTROPHIC for a
single-stock sweep: a split/bonus boundary inside the swept window would feed
the engine a fabricated overnight price cliff and produce silently corrupt
results. Until the corporate-action adjustment layer of ADR-0016 exists, this
guard REFUSES every out-of-scope symbol at every production sweep entry
(CLAUDE.md §2 Golden Rule #3 scope lock; the lake may hold ~750 REFERENCE_ONLY
underlyings per DEC-P0-063 / DEC-NTM-GROWW-REPOINT-001 — the ENGINE may not
sweep them).

Allowed set
-----------
Exactly the three spot indexes ``NIFTY`` / ``BANKNIFTY`` / ``SENSEX`` — the
bare LAKE-partition trading symbols (what ``brutex sweep`` / ``--symbols``
take), the engine twin of the lake-side groww-key truth
``brutex.adapters.groww.constants.BRUTE_FORCE_INDEX_SYMBOLS``
(``{"NSE-NIFTY", "NSE-BANKNIFTY", "BSE-SENSEX"}``, constants.py:99). The set
is defined LOCALLY (the engine imports nothing from ``brutex.adapters`` —
the existing layering, kept) with a unit test asserting the two sets agree
exchange-prefix-for-prefix so they can never drift
(``tests/unit/test_engine_scope_guard.py``).

ADR-0012 NIFTY-futures extension point
--------------------------------------
DEC-NIFTY-DYNAMIC-STRATEGY-001 / ADR-0012 AUTHORIZES the engine to also run
on NIFTY futures for the dynamic-strategy track — but NO futures sweep wiring
exists today (grounded: ``brutex/strategies/spec.py`` deliberately does NOT
pre-assign the NIFTY-futures ``symbol_id`` — "it lands with the futures-sweep
PR once its real instrument label exists"; no sweep path resolves an FNO
contract symbol). So this guard admits ONLY the 3 spot symbols, and nothing
real is blocked by that: when the futures-sweep PR lands, it extends THIS
module (add the real NIFTY-futures instrument label to the allowed set in the
same reviewed PR that assigns its ``symbol_id``) — never a bypass flag.

Honest envelope (Golden Rule #10)
---------------------------------
Guarded at every production ENGINE-EXECUTING entry: ``brutex sweep`` (the
single-tuple CLI), the ONE ``--symbols`` → ``SweepGridConfig.instruments``
chokepoint in ``cmd_sweep_all`` (which ``sweep-all``, ``sweep-daemon`` and the
dashboard 🚀 Run Sweep button all flow through — the dashboard additionally
gates EAGERLY in ``resolve_sweep_request`` so a refusal is a 400, not a
background-job stderr line), AND the ``sweep-verify --recount`` Tier-3 shadow
re-sweep (which EXECUTES the real ``run_sweep_grid`` into ``--scratch-root``;
``--allow-context-mismatch`` overrides the recount CONTEXT gate only, never
scope). Direct LIBRARY construction of ``SweepGridConfig`` is NOT guarded — a
documented limitation, not an operator surface (the test suite builds
synthetic-label grids by design). ``sweep-verify``'s report-only Tier 1/2
(structural + identity) are deliberately unguarded — they verify an existing
tree and launch no engine, so a legacy out-of-scope tree stays
report-verifiable (its Tier-3 recount is refused — the accepted trade-off).

O(1) per symbol; pure except for one structlog WARNING on refusal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import structlog

if TYPE_CHECKING:
    from collections.abc import Iterable

_LOG = structlog.get_logger("brutex.engine.scope_guard")

#: Display order for the allowed set (the CLAUDE.md §1 wording, not sorted()).
_ALLOWED_DISPLAY: Final[tuple[str, ...]] = ("NIFTY", "BANKNIFTY", "SENSEX")

#: The engine-entry allowed symbols — bare LAKE trading symbols, the engine
#: twin of the lake-side ``BRUTE_FORCE_INDEX_SYMBOLS`` groww keys
#: (``adapters/groww/constants.py:99``); set-agreement is test-pinned.
ENGINE_SCOPE_SYMBOLS: Final[frozenset[str]] = frozenset(_ALLOWED_DISPLAY)


class EngineScopeError(ValueError):
    """A production sweep entry was asked to run OUTSIDE the engine scope.

    A :class:`ValueError` subclass — the house pattern for config-time
    validation in :mod:`brutex.engine.sweep_orchestrator` — so any existing
    ``except ValueError`` config handling keeps working; the CLI chokepoints
    catch it precisely and map it to a clean ``EXIT_USER_ERROR``.
    """


def _refusal_message(refused: tuple[str, ...]) -> str:
    """Compose the refusal naming EVERY refused symbol + the exact reason."""
    names = ", ".join(repr(s) for s in refused) if refused else "<no symbols>"
    allowed = " / ".join(_ALLOWED_DISPLAY)
    return (
        f"engine scope refused for {names}: BruteX has NO corporate-action "
        "adjustment layer (corporate-action adjustment layer missing — "
        "DEC-CORP-ACTION-GATE-001, ADR-0016); single-stock/reference-only "
        "symbols would produce silently corrupt results across split/bonus "
        f"boundaries. Engine scope is {allowed} (+ NIFTY futures per ADR-0012 "
        "when that wiring lands)."
    )


def ensure_engine_scope(symbols: Iterable[str]) -> tuple[str, ...]:
    """Admit *symbols* into the engine, or refuse LOUDLY naming every offender.

    Normalizes each symbol (strip + upper-case, so ``" nifty "`` → ``"NIFTY"``)
    and returns the normalized tuple in input order (duplicates preserved —
    downstream axes de-dup themselves). Any symbol whose normalized form is
    not in :data:`ENGINE_SCOPE_SYMBOLS` — including a blank/empty string —
    is collected and ALL offenders are named in one refusal (never just the
    first), after ONE ``engine.scope_refused`` structlog WARNING. An empty
    iterable is refused too (an engine entry must sweep something in scope).

    Parameters
    ----------
    symbols
        The operator-supplied engine-entry symbols (bare lake trading
        symbols, e.g. from ``brutex sweep SYMBOL`` or ``--symbols A,B``).

    Returns
    -------
    tuple[str, ...]
        The normalized (stripped, upper-cased) symbols, input order.

    Raises
    ------
    EngineScopeError
        If any symbol is outside the engine scope, or *symbols* is empty.
        The message contains the literal phrase "corporate-action adjustment
        layer missing" + DEC-CORP-ACTION-GATE-001 + ADR-0016 and names every
        refused symbol.
    """
    raw = tuple(symbols)
    normalized: list[str] = []
    refused: list[str] = []
    for symbol in raw:
        norm = symbol.strip().upper()
        if norm in ENGINE_SCOPE_SYMBOLS:
            normalized.append(norm)
        else:
            refused.append(symbol)
    if refused or not raw:
        _LOG.warning(
            "engine.scope_refused",
            refused=[repr(s) for s in refused],
            allowed=list(_ALLOWED_DISPLAY),
            decision="DEC-CORP-ACTION-GATE-001",
        )
        raise EngineScopeError(_refusal_message(tuple(refused)))
    return tuple(normalized)
