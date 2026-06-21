"""Mapping-integrity reconciliation — the real-time spot↔options guarantee.

DEC-MAPPING-INTEGRITY-RECONCILE-001. A PURE, offline reconciliation that answers the
operator's core trust question — *"is every options-eligible spot underlying actually
mapped to and pulling its option expiries, or is a name silently empty?"* — as a single
green/red verdict you can SEE, never a "trust me".

It cross-checks two already-materialised, store-fed sets (NO lake/network, NO 89k option
legs — the lean store discards them, DEC-INSTRUMENT-UNIVERSE-FAST-001):

* the **options-eligible underlyings** the vendor master admits
  (``InstrumentStore.fno_eligible_underlyings``), and
* the **coverage ledger** of which underlyings actually pulled expiries
  (``InstrumentStore.list_coverage`` → years with ``expiries_pulled > 0``),

and flags every eligible spot that pulled NOTHING (``none`` — e.g. M&M, the
special-character options-fetch bug F2 fixes) or only PART of the expected 2020→today span
(``partial`` — e.g. BAJAJ-AUTO's 2024-only). The ``none`` failures are the GATING red
signal (``is_green``); ``partial`` is surfaced amber (an honestly-stated "covered some
years, not all" — distinguishing a genuine late listing from a bug needs the vendor-bounded
completeness verdict, NOT a fabricated year total, so it does not gate — Golden Rule #10).

This module DETECTS; it does not FIX. Until F2 normalises the options-fetch symbol, M&M
reads red here — that red is the honest, intended pre-fix proof, not a regression.

The reconciliation is one O(eligible) pass over O(1) dict/set lookups (no nested scan);
inputs are injected so the whole module is 100% offline-unit-testable.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol

#: An options-eligible spot pulled ZERO expiries — the hard, GATING failure (it has an
#: options chain on the vendor master but the coverage ledger shows nothing pulled, e.g.
#: M&M). Unless whitelisted, one of these turns the verdict red.
REASON_NONE: Final[str] = "none"

#: An options-eligible spot pulled SOME years but not the full expected 2020→today span
#: (e.g. BAJAJ-AUTO's 2024-only). Surfaced amber — NOT gating — because telling a genuine
#: late listing apart from a bug is the completeness page's vendor-bounded judgement, never
#: a fabricated total here (Golden Rule #10).
REASON_PARTIAL: Final[str] = "partial"

#: Both reasons, for drift tests / consumers.
REASONS: Final[tuple[str, str]] = (REASON_NONE, REASON_PARTIAL)


@dataclass(frozen=True, slots=True)
class EligibleSpot:
    """One options-eligible spot underlying — ``exchange`` token + ``symbol`` (trading symbol)."""

    exchange: str
    symbol: str


@dataclass(frozen=True, slots=True)
class UnderPulledSpot:
    """One options-eligible spot that did NOT pull its full expected option history.

    Attributes
    ----------
    exchange, symbol
        The eligible underlying's exchange token + trading symbol.
    reason
        :data:`REASON_NONE` (zero expiries pulled — gating) or :data:`REASON_PARTIAL`
        (some years pulled, not the full expected span — amber).
    covered_years
        The calendar years this underlying actually pulled ≥1 expiry for (sorted).
    missing_years
        The expected years it did NOT cover (sorted) — empty for a pure ``none`` with no
        expected span, else the shortfall.
    whitelisted
        ``True`` iff the symbol is in the operator's documented whitelist (a known-genuine
        late listing), which excludes it from the gating verdict.
    """

    exchange: str
    symbol: str
    reason: str
    covered_years: tuple[int, ...]
    missing_years: tuple[int, ...]
    whitelisted: bool


@dataclass(frozen=True, slots=True)
class MappingIntegrityReport:
    """The reconciliation result — the green/red mapping-integrity verdict + its drill-down.

    Attributes
    ----------
    generated_at
        ISO timestamp the report was built (for the dashboard "as of").
    total_eligible
        Count of distinct options-eligible spot underlyings reconciled.
    fully_covered
        Count that pulled the full expected span (the healthy majority).
    under_pulled
        Every eligible spot that fell short, sorted (exchange, symbol).
    expected_years
        The expected calendar-year span every eligible spot is checked against (sorted).
    whitelist
        The documented late-listing symbols excluded from the gating verdict (echoed
        verbatim for auditability — Golden Rule #1).
    """

    generated_at: str
    total_eligible: int
    fully_covered: int
    under_pulled: tuple[UnderPulledSpot, ...]
    expected_years: tuple[int, ...]
    whitelist: tuple[str, ...]

    @property
    def gating_failures(self) -> tuple[UnderPulledSpot, ...]:
        """The non-whitelisted :data:`REASON_NONE` failures — the rows that turn it red."""
        return tuple(u for u in self.under_pulled if u.reason == REASON_NONE and not u.whitelisted)

    @property
    def is_green(self) -> bool:
        """``True`` iff NO non-whitelisted eligible spot pulled zero expiries (the hard gate)."""
        return not self.gating_failures


class _CoverageRow(Protocol):
    """The duck-typed coverage-ledger row this module reads (a subset of ``CoverageRecord``)."""

    exch_id: str
    underlying_symbol: str
    year: int
    expiries_pulled: int


def coverage_years_from_records(
    records: Iterable[_CoverageRow],
) -> dict[tuple[str, str], set[int]]:
    """Group coverage-ledger rows → ``{(exch_id, underlying_symbol): {years with expiries}}``.

    Only years with ``expiries_pulled > 0`` count (a zero-expiry cell is NOT coverage — it is
    exactly the M&M / pre-listing case the reconciler must flag). The thin dashboard route
    feeds ``InstrumentStore.list_coverage()`` here so its grouping logic stays unit-tested.
    """
    covered: dict[tuple[str, str], set[int]] = {}
    for rec in records:
        if rec.expiries_pulled > 0:
            covered.setdefault((rec.exch_id, rec.underlying_symbol), set()).add(rec.year)
    return covered


def _as_pair(spot: EligibleSpot | tuple[str, str]) -> tuple[str, str]:
    """Normalise an eligible-spot input (dataclass OR ``(exchange, symbol)`` tuple) to a pair."""
    if isinstance(spot, EligibleSpot):
        return (spot.exchange, spot.symbol)
    exchange, symbol = spot
    return (str(exchange), str(symbol))


def reconcile_mapping_integrity(
    *,
    eligible_spots: Iterable[EligibleSpot | tuple[str, str]],
    covered_years: Mapping[tuple[str, str], Iterable[int]],
    expected_years: Iterable[int],
    whitelist: Iterable[str] = (),
    now: datetime,
) -> MappingIntegrityReport:
    """Reconcile options-eligible spots against pulled coverage → a green/red report.

    For each distinct eligible ``(exchange, symbol)``: look up its covered years (O(1) dict
    get). Zero covered → :data:`REASON_NONE`; covered but missing any *expected_years* →
    :data:`REASON_PARTIAL`; else fully covered. Whitelisted symbols are still reported but
    excluded from the gating verdict. One O(eligible) pass, O(1) per spot — no nested scan.

    Parameters
    ----------
    eligible_spots
        The options-eligible underlyings (``EligibleSpot`` or ``(exchange, symbol)`` tuples);
        de-duplicated here so a repeated row is counted once.
    covered_years
        Map ``(exchange, symbol) -> years`` that pulled ≥1 expiry (from the coverage ledger).
    expected_years
        The calendar-year span each eligible spot is expected to span (e.g. 2020→today). An
        empty span disables the ``partial`` check (only ``none`` can then fail).
    whitelist
        Documented genuine late-listing symbols, excluded from the gating verdict.
    now
        The report timestamp (injected for determinism).

    Returns
    -------
    MappingIntegrityReport
        The verdict + the sorted under-pulled drill-down.
    """
    expected = tuple(sorted(set(expected_years)))
    expected_set = frozenset(expected)
    wl = frozenset(whitelist)
    pairs = sorted({_as_pair(spot) for spot in eligible_spots})

    under: list[UnderPulledSpot] = []
    fully_covered = 0
    for exchange, symbol in pairs:
        years = frozenset(covered_years.get((exchange, symbol), ()))
        if not years:
            under.append(
                UnderPulledSpot(
                    exchange=exchange,
                    symbol=symbol,
                    reason=REASON_NONE,
                    covered_years=(),
                    missing_years=expected,
                    whitelisted=symbol in wl,
                )
            )
        elif expected_set and not expected_set <= years:
            under.append(
                UnderPulledSpot(
                    exchange=exchange,
                    symbol=symbol,
                    reason=REASON_PARTIAL,
                    covered_years=tuple(sorted(years)),
                    missing_years=tuple(sorted(expected_set - years)),
                    whitelisted=symbol in wl,
                )
            )
        else:
            fully_covered += 1

    return MappingIntegrityReport(
        generated_at=now.isoformat(),
        total_eligible=len(pairs),
        fully_covered=fully_covered,
        under_pulled=tuple(under),
        expected_years=expected,
        whitelist=tuple(sorted(wl)),
    )


def report_to_dict(report: MappingIntegrityReport) -> dict[str, object]:
    """Project a :class:`MappingIntegrityReport` to the JSON the dashboard route returns."""
    return {
        "generated_at": report.generated_at,
        "is_green": report.is_green,
        "total_eligible": report.total_eligible,
        "fully_covered": report.fully_covered,
        "under_pulled_count": len(report.under_pulled),
        "gating_failures": len(report.gating_failures),
        "expected_years": list(report.expected_years),
        "whitelist": list(report.whitelist),
        "under_pulled": [
            {
                "exchange": u.exchange,
                "symbol": u.symbol,
                "reason": u.reason,
                "covered_years": list(u.covered_years),
                "missing_years": list(u.missing_years),
                "whitelisted": u.whitelisted,
            }
            for u in report.under_pulled
        ],
    }


__all__ = [
    "REASONS",
    "REASON_NONE",
    "REASON_PARTIAL",
    "EligibleSpot",
    "MappingIntegrityReport",
    "UnderPulledSpot",
    "coverage_years_from_records",
    "reconcile_mapping_integrity",
    "report_to_dict",
]
