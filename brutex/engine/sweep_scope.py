"""Sweep-entry scope preflight — refuse out-of-engine-scope launch coordinates.

Layer 2 of DEC-ENGINE-SCOPE-GUARD-001 (the coordinate law; Layer 1 is the
``SweepGridConfig.__post_init__`` hard invariant): every PRODUCTION sweep
launch mouth — ``brutex sweep-all`` (and the daemon that delegates to it),
``brutex sweep``, and the dashboard sweep route — runs
:func:`check_sweep_scope` over its requested ``(exchange, segment, symbols)``
BEFORE any catalog reconcile or bar load, so an out-of-scope launch (India
VIX, an F&O stock, a BANKNIFTY future, any option) is refused with a law
citation at ZERO lake I/O cost. Classification delegates to the ONE canonical
predicate :func:`brutex.universe.classify_engine_sweep_target` (CLAUDE.md §11
COMMON — never a copy).

Pure preflight pattern (the :mod:`brutex.engine.sweep_preflight` sibling):
no I/O, no clock, O(#symbols) — a frozenset probe + at most one regex + one
``parse_fno_symbol`` per symbol; the ONE side effect is a single structlog
hazard WARNING when the research override actually lets refused symbols
through (DEC-CORP-ACTION-GATE-001 below). ``sweep-verify --recount`` is ALSO
gated (the Tier-3 shadow re-sweep EXECUTES the real ``run_sweep_grid`` into
``--scratch-root`` — a production sweep entry, not a report; the corp-action
extension's hostile-review MAJOR); the remaining forensic surfaces
(``sweep-verify`` Tier 1/2, ``sweep-heal``, cascade/options replay) stay
unguarded — they inspect a pre-guard tree and run NO engine — and the
recount grid-rebuild site opts out of the Layer-1
``unsafe_allow_out_of_scope_instruments`` field so a legacy out-of-scope
tree keeps Tier-1/2 verifiability.

Corporate-action extension (DEC-CORP-ACTION-GATE-001 / ADR-0016, extending
DEC-ENGINE-SCOPE-GUARD-001): BruteX has ZERO corporate-action adjustment code
(no split/bonus/adjustment-factor/ex-date layer anywhere in ``brutex/``) —
CORRECT for the divisor-maintained 3-index scope, CATASTROPHIC for a
single-name sweep (a 1:1 bonus reads as a fabricated −50% overnight cliff
feeding every PDH/PDL/pivot/gap bit). So the hard refusal names that
rationale, and the ``allow_out_of_scope`` research escape can never proceed
SILENTLY: every overridden launch emits one loud
``sweep.scope_override_hazard`` structlog WARNING carrying the
"corporate-action adjustment layer missing" hazard.

Honest envelope (Golden Rule #10): this is an ALLOWLIST over addressing
strings, not an oracle over lake contents — a mislabeled partition belongs to
the mapping/economics integrity audits (MIGRATION.md #874/#875). Layer 1 sees
only bare symbols; a right-symbol-wrong-coordinate launch (``SENSEX``
under ``NSE``) is exactly what THIS layer catches — that is why both exist.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from brutex.universe import classify_engine_sweep_target

__all__ = [
    "ScopeReport",
    "SymbolVerdict",
    "check_sweep_scope",
]

_LOG = structlog.get_logger("brutex.engine.sweep_scope")

#: The corporate-action hazard rationale (DEC-CORP-ACTION-GATE-001 / ADR-0016
#: — the extension of DEC-ENGINE-SCOPE-GUARD-001): stated verbatim on BOTH the
#: hard refusal and the override hazard WARNING so no launch outside the
#: engine scope — refused OR overridden — can ever read as routine.
_CORP_ACTION_HAZARD: str = (
    "BruteX has NO corporate-action adjustment layer (corporate-action "
    "adjustment layer missing — DEC-CORP-ACTION-GATE-001 / ADR-0016, the "
    "corporate-action extension of DEC-ENGINE-SCOPE-GUARD-001): an "
    "out-of-scope single-name sweep feeds the engine unadjusted candles "
    "across split/bonus boundaries and produces silently corrupt results."
)


@dataclass(frozen=True, slots=True)
class SymbolVerdict:
    """One refused symbol + its law classification.

    Attributes
    ----------
    symbol
        The requested instrument symbol (verbatim, as parsed from the launch).
    reason
        The specific refusal ground from
        :func:`brutex.universe.classify_engine_sweep_target` (names the law —
        CLAUDE.md §2 Rule 3 / ADR-0012 / REFERENCE_ONLY — never a generic no).
    """

    symbol: str
    reason: str


@dataclass(frozen=True, slots=True)
class ScopeReport:
    """The scope verdict for one sweep launch request.

    Attributes
    ----------
    ok
        True iff the launch may proceed (every symbol allowed, or the
        operator explicitly overrode with ``allow_out_of_scope``).
    refused
        Every out-of-scope ``(symbol, reason)`` verdict — PRESERVED even when
        an override flips ``ok`` True, so the override warning + the audit
        stamp can name exactly what was let through (Golden Rule #8).
    exchange, segment
        The launch coordinates the symbols were classified under (echoed into
        the refusal message so the operator sees the exact request).
    overridden
        True iff ``allow_out_of_scope`` flipped a refusing report to ``ok``.
    """

    ok: bool
    refused: tuple[SymbolVerdict, ...]
    exchange: str
    segment: str
    overridden: bool

    def refusal_message(self) -> str:
        """Render the operator-facing refusal (every symbol + law + remedy)."""
        lines = [
            f"sweep REFUSED: {len(self.refused)} of the requested symbol(s) under "
            f"{self.exchange}/{self.segment} are outside the brute-force engine scope:"
        ]
        lines.extend(f"  - {verdict.symbol}: {verdict.reason}" for verdict in self.refused)
        lines.append(
            "The engine sweeps ONLY NIFTY/BANKNIFTY/SENSEX spot (CLAUDE.md §2 Rule 3) "
            "plus NIFTY futures (ADR-0012 / DEC-NIFTY-DYNAMIC-STRATEGY-001). "
            f"Rationale: {_CORP_ACTION_HAZARD} "
            "Remedy: drop the out-of-scope symbol(s), or — CLI research runs ONLY — "
            "re-run with --allow-out-of-scope (loud + audit-stamped; no dashboard "
            "override exists)."
        )
        return "\n".join(lines)

    def override_warning(self) -> str:
        """Render the loud WARNING printed when an override lets symbols through."""
        names = ", ".join(verdict.symbol for verdict in self.refused)
        return (
            f"WARNING: --allow-out-of-scope OVERRIDE — sweeping {len(self.refused)} "
            f"out-of-engine-scope symbol(s) under {self.exchange}/{self.segment}: "
            f"{names}. This is a research escape hatch (DEC-ENGINE-SCOPE-GUARD-001); "
            "results are OUTSIDE the CLAUDE.md §2 Rule 3 engine scope and the launch "
            "is audit-stamped scope_override=True."
        )


def check_sweep_scope(
    symbols: list[str] | tuple[str, ...],
    *,
    exchange: str,
    segment: str,
    allow_out_of_scope: bool,
) -> ScopeReport:
    """Classify every launch symbol under the engine scope law — pure, zero I/O.

    Parameters
    ----------
    symbols
        The requested instrument symbols (already parsed from the launch CSV).
    exchange, segment
        The lake partition coordinates the launch would read under.
    allow_out_of_scope
        The CLI-only research escape hatch: when True a refusing report still
        returns ``ok=True`` (with ``overridden=True`` and ``refused``
        preserved for the warning + audit stamp), and ONE
        ``sweep.scope_override_hazard`` structlog WARNING is emitted naming
        the let-through symbols + the corporate-action hazard
        (DEC-CORP-ACTION-GATE-001 / ADR-0016 — no silent override). The
        dashboard always passes False — a route override does not exist by
        design.

    Returns
    -------
    ScopeReport
        ``ok`` + every refused verdict. O(#symbols); no I/O, no clock; the
        only side effect is the override hazard WARNING above.
    """
    refused_list: list[SymbolVerdict] = []
    for symbol in symbols:
        verdict = classify_engine_sweep_target(exchange=exchange, segment=segment, symbol=symbol)
        if not verdict.allowed:
            refused_list.append(SymbolVerdict(symbol=symbol, reason=verdict.reason))
    refused = tuple(refused_list)
    overridden = bool(refused) and allow_out_of_scope
    if overridden:
        # The corporate-action hazard WARNING (DEC-CORP-ACTION-GATE-001 /
        # ADR-0016): the ONE chokepoint every override passes through, so no
        # research escape can ever proceed silently — emitted exactly once
        # per check_sweep_scope invocation, only when refused symbols are
        # actually let through (an in-scope launch with the flag set never
        # warns, matching the ``overridden`` stamp).
        _LOG.warning(
            "sweep.scope_override_hazard",
            symbols=[verdict.symbol for verdict in refused],
            exchange=exchange,
            segment=segment,
            hazard=_CORP_ACTION_HAZARD,
        )
    return ScopeReport(
        ok=not refused or allow_out_of_scope,
        refused=refused,
        exchange=exchange,
        segment=segment,
        overridden=overridden,
    )
