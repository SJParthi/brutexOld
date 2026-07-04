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
``parse_fno_symbol`` per symbol. Deliberately NOT guarded: the forensic
surfaces (``sweep-verify`` all tiers incl. ``--recount``, ``sweep-heal``,
cascade/options replay) must be able to inspect a pre-guard tree — the one
recount grid-rebuild site opts out via the Layer-1
``unsafe_allow_out_of_scope_instruments`` field instead.

Honest envelope (Golden Rule #10): this is an ALLOWLIST over addressing
strings, not an oracle over lake contents — a mislabeled partition belongs to
the mapping/economics integrity audits (MIGRATION.md #874/#875). Layer 1 sees
only bare symbols; a right-symbol-wrong-coordinate launch (``SENSEX``
under ``NSE``) is exactly what THIS layer catches — that is why both exist.
"""

from __future__ import annotations

from dataclasses import dataclass

from brutex.universe import classify_engine_sweep_target

__all__ = [
    "ScopeReport",
    "SymbolVerdict",
    "check_sweep_scope",
]


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
        preserved for the warning + audit stamp). The dashboard always passes
        False — a route override does not exist by design.

    Returns
    -------
    ScopeReport
        ``ok`` + every refused verdict. O(#symbols); no I/O, no clock.
    """
    refused_list: list[SymbolVerdict] = []
    for symbol in symbols:
        verdict = classify_engine_sweep_target(exchange=exchange, segment=segment, symbol=symbol)
        if not verdict.allowed:
            refused_list.append(SymbolVerdict(symbol=symbol, reason=verdict.reason))
    refused = tuple(refused_list)
    overridden = bool(refused) and allow_out_of_scope
    return ScopeReport(
        ok=not refused or allow_out_of_scope,
        refused=refused,
        exchange=exchange,
        segment=segment,
        overridden=overridden,
    )
