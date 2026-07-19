"""Mapping-integrity reconciliation — the real-time spot↔options guarantee.

DEC-MAPPING-INTEGRITY-RECONCILE-001. A PURE, offline reconciliation that answers the
operator's core trust question — *"is every options-eligible spot underlying actually
mapped to and pulling its option expiries, or is a name silently empty?"* — as a single
green/red verdict you can SEE, never a "trust me".

It cross-checks already-materialised, store-fed sets (NO lake/network, NO 89k option
legs — the lean store discards them, DEC-INSTRUMENT-UNIVERSE-FAST-001):

* the **options-eligible underlyings** the vendor master admits
  (``InstrumentStore.fno_eligible_underlyings`` — scoped to TRADABLE F&O
  underlyings, ``reference_only = FALSE``, so the ~539 NIFTY-Total-Market
  REFERENCE_ONLY constituents never inflate the gate;
  DEC-MAPPING-INTEGRITY-ATTEMPTED-001),
* the **coverage ledger** of which underlyings actually pulled expiries
  (``InstrumentStore.list_coverage`` → years with ``expiries_pulled > 0``), and
* the **attempted set** — every underlying that has ANY coverage-ledger row
  (regardless of ``expiries_pulled``), i.e. an expiry pull was actually RUN for it
  (DEC-MAPPING-INTEGRITY-ATTEMPTED-001).

A spot that was ATTEMPTED yet pulled ZERO expiries is the hard, GATING ``none`` failure
(e.g. M&M, the special-character options-fetch bug); a spot covering only PART of the
expected 2020→today span is ``partial``/amber (a late listing whose early years never
traded — note the coverage ledger keys such a hyphenated name under its PRIMARY F&O
form, ``BAJAJ-AUTO`` → ``BAJAJAUTO``, which is why the compare crosses keyspaces via
*key_forms* below — DEC-MAPPING-INTEGRITY-FORM-KEY-001). A spot that
was NEVER attempted (NO coverage row at all — the common case on a SELECTIVE pull that
targeted only a few underlyings) is the neutral ``not_attempted`` state: it is reported
SEPARATELY and NEVER gates, because "not pulled yet" is not a "pulled and got nothing"
failure (DEC-MAPPING-INTEGRITY-ATTEMPTED-001 — the false-RED-on-selective-pull fix). Only
the non-whitelisted ``none`` rows turn ``is_green`` red; ``partial`` and ``not_attempted``
stay green (distinguishing a genuine late listing from a bug needs the vendor-bounded
completeness verdict, NOT a fabricated year total — Golden Rule #10).

This is the never-attempted↔attempted-and-zero distinction the gate was missing: a
never-attempted spot has NO coverage row; an attempted-and-empty spot DOES (Phase-1
expiry discovery writes a ``pending`` ``expiries_pulled=0`` cell), so the two are
on-disk distinguishable. The coverage WRITER keys every cell on the PRIMARY F&O
underlying form (``metadata_pull._resolve_underlying_forms`` →
``primary_fno_underlying`` — ``BAJAJ-AUTO`` rows land under ``BAJAJAUTO``), while the
eligible set is SPOT-spelled; the dashboard builder therefore injects the *key_forms*
map (DEC-MAPPING-INTEGRITY-FORM-KEY-001) so the compare crosses the two keyspaces —
without it a hyphenated/aliased name read ``not_attempted`` forever and its
attempted-and-zero alarm could never fire. The fold carries the CROSS-OWNER guard
(REFUTER ROUND 5, Z5-1): a form that is itself a DIFFERENT eligible symbol's own key
(its exact spot spelling or its primary form) may still resolve coverage, but it can
never rescue a symbol whose OWN keys read attempted-and-zero — so two
normalize-colliding eligible names can never mask each other's silent-vanish. The
real M&M silent-vanish stays catchable (it is in the attempted set, zero years →
``none`` → red) while a clean selective pull reads green (the un-pulled names are
``not_attempted``, never red).

This module DETECTS; it does not FIX. The reconciliation is one O(eligible) pass over
O(1) dict/set lookups (no nested scan); inputs are injected so the whole module is 100%
offline-unit-testable.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from brutex.data.coverage_ledger import CoverageRecord

#: The empty owner set — the allocation-free default for the cross-owner probe
#: (REFUTER ROUND 5, Z5-1): a form no eligible symbol owns has no cross-owners.
_NO_OWNERS: Final[frozenset[str]] = frozenset()

#: An options-eligible spot pulled ZERO expiries — the hard, GATING failure (it has an
#: options chain on the vendor master but the coverage ledger shows nothing pulled, e.g.
#: M&M). Unless whitelisted, one of these turns the verdict red.
REASON_NONE: Final[str] = "none"

#: An options-eligible spot pulled SOME years but not the full expected 2020→today span
#: (e.g. BAJAJ-AUTO's 2024-only). Surfaced amber — NOT gating — because telling a genuine
#: late listing apart from a bug is the completeness page's vendor-bounded judgement, never
#: a fabricated total here (Golden Rule #10).
REASON_PARTIAL: Final[str] = "partial"

#: An options-eligible spot that was NEVER attempted — it has NO coverage-ledger row at
#: all, so an expiry pull was simply never RUN for it (the common case on a SELECTIVE pull
#: that targeted only a few underlyings). Reported separately and NEVER gating: "not pulled
#: yet" is not "pulled and got nothing" (DEC-MAPPING-INTEGRITY-ATTEMPTED-001 — the
#: false-RED-on-selective-pull fix).
REASON_NOT_ATTEMPTED: Final[str] = "not_attempted"

#: All three reasons, for drift tests / consumers.
REASONS: Final[tuple[str, str, str]] = (REASON_NONE, REASON_PARTIAL, REASON_NOT_ATTEMPTED)


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
        :data:`REASON_NONE` (attempted yet zero expiries pulled — gating),
        :data:`REASON_PARTIAL` (some years pulled, not the full expected span — amber), or
        :data:`REASON_NOT_ATTEMPTED` (no coverage row at all — never pulled, neutral).
    covered_years
        The calendar years this underlying actually pulled ≥1 expiry for (sorted).
    missing_years
        The expected years it did NOT cover (sorted) — empty for a pure ``none`` /
        ``not_attempted`` with no expected span, else the shortfall.
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
    not_attempted
        Count of eligible spots that were NEVER attempted (no coverage row) — the neutral
        "not pulled yet" tally, the common case on a SELECTIVE pull. NEVER gates
        (DEC-MAPPING-INTEGRITY-ATTEMPTED-001).
    under_pulled
        Every eligible spot that fell short — ``none`` / ``partial`` / ``not_attempted`` —
        sorted (exchange, symbol).
    expected_years
        The expected calendar-year span every eligible spot is checked against (sorted).
    whitelist
        The documented late-listing symbols excluded from the gating verdict (echoed
        verbatim for auditability — Golden Rule #1).
    """

    generated_at: str
    total_eligible: int
    fully_covered: int
    not_attempted: int
    under_pulled: tuple[UnderPulledSpot, ...]
    expected_years: tuple[int, ...]
    whitelist: tuple[str, ...]

    @property
    def gating_failures(self) -> tuple[UnderPulledSpot, ...]:
        """The non-whitelisted :data:`REASON_NONE` failures — the rows that turn it red.

        ONLY attempted-and-zero (``none``) rows gate; a ``not_attempted`` row (never
        pulled) and a ``partial`` row never do (DEC-MAPPING-INTEGRITY-ATTEMPTED-001).
        """
        return tuple(u for u in self.under_pulled if u.reason == REASON_NONE and not u.whitelisted)

    @property
    def is_green(self) -> bool:
        """``True`` iff NO non-whitelisted eligible spot was ATTEMPTED and pulled zero expiries.

        A never-attempted spot (``not_attempted``) and a ``partial`` spot never turn it
        red — so a clean SELECTIVE pull reads green (DEC-MAPPING-INTEGRITY-ATTEMPTED-001).
        """
        return not self.gating_failures


def coverage_years_from_records(
    records: Iterable[CoverageRecord],
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


def attempted_pairs_from_records(
    records: Iterable[CoverageRecord],
) -> set[tuple[str, str]]:
    """Collect the ``(exch_id, underlying_symbol)`` of every underlying with ANY coverage row.

    DEC-MAPPING-INTEGRITY-ATTEMPTED-001. A coverage-ledger row exists IFF an expiry pull was
    actually RUN for that underlying — including an attempted-and-EMPTY run, which Phase-1
    expiry discovery records as a ``pending`` ``expiries_pulled=0`` cell
    (``brutex.dashboard.metadata_pull._on_expiry_cell``). Unlike
    :func:`coverage_years_from_records` (which keeps only ``expiries_pulled > 0`` cells), this
    keeps a pair for ANY row, so an attempted-and-empty underlying (the M&M silent-vanish) is
    in the attempted set while a never-pulled underlying (no row) is NOT — the distinction the
    gate needs to separate a real "pulled ZERO expiries" failure from a benign "not pulled yet".

    The thin dashboard route feeds ``InstrumentStore.list_coverage()`` here so its grouping
    logic stays unit-tested. One O(rows) pass; the returned set has O(1) membership.
    """
    return {(rec.exch_id, rec.underlying_symbol) for rec in records}


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
    attempted: Iterable[tuple[str, str]] | None = None,
    whitelist: Iterable[str] = (),
    now: datetime,
    key_forms: Mapping[str, tuple[str, ...]] | None = None,  # DEC-MAPPING-INTEGRITY-FORM-KEY-001
) -> MappingIntegrityReport:
    """Reconcile options-eligible spots against pulled coverage → a green/red report.

    For each distinct eligible ``(exchange, symbol)``: look up its covered years (O(1) dict
    get). If it has covered years missing any *expected_years* → :data:`REASON_PARTIAL`; if
    it covers the full span → fully covered. If it pulled ZERO expiries the verdict depends
    on whether it was ATTEMPTED (DEC-MAPPING-INTEGRITY-ATTEMPTED-001): a spot WITH a coverage
    row but zero years → :data:`REASON_NONE` (gating — the real M&M silent-vanish); a spot
    with NO coverage row → :data:`REASON_NOT_ATTEMPTED` (neutral — never pulled, never gates,
    so a clean SELECTIVE pull reads green). Whitelisted symbols are still reported but excluded
    from the gating verdict. One O(eligible) pass, O(1) per spot — no nested scan.

    **The *key_forms* law (DEC-MAPPING-INTEGRITY-FORM-KEY-001):** the coverage WRITER
    keys its cells on the PRIMARY F&O underlying form
    (``metadata_pull._resolve_underlying_forms`` — ``BAJAJ-AUTO`` rows land under
    ``BAJAJAUTO``), while the eligible set is SPOT-spelled. When *key_forms* maps an
    eligible SYMBOL to its ordered forms superset, the covered years are the UNION
    over every ``(exchange, form)`` key (never primary-only — legacy raw-key coverage
    eras stay visible, DEC-PULL-FORENSIC-WAVE2-001) and "attempted" means ANY form has
    a coverage row. Display + whitelist stay on the SPOT spelling. A ``None`` map, a
    symbol absent from it, and an EMPTY forms tuple (the blank-symbol degenerate,
    DEC-FNO-UNDERLYING-EMPTY-GUARD-001) each degrade to today's exact raw-key compare
    — never a guess, and the default is byte-identical.

    **The cross-owner guard (REFUTER ROUND 5, Z5-1):** a form that is itself a
    DIFFERENT eligible symbol's own key — its exact spot spelling OR its primary
    write form (``forms[0]``) — is CROSS-OWNED: coverage under that key may belong
    to the sibling. Cross-owned forms still participate in the resolution fold (the
    with-legs dual-spelling law stands), but they can never RESCUE a symbol from its
    own-key silent-vanish: when any NON-cross-owned form of the symbol has a coverage
    row (positive own-key attempted evidence) while the non-cross-owned fold holds
    ZERO covered years, the symbol gates :data:`REASON_NONE` regardless of what the
    cross-owned forms would donate — two normalize-colliding eligible names
    (``{AB, A-B}``) can never mask each other's attempted-and-zero. The symbol's own
    exact spot key always stays in its fold, so the guard's worst case is today's
    exact-key verdict (main parity), never a masked GREEN. The back-compat
    ``attempted=None`` blanket is not own-key evidence and never arms the guard.
    Residual (disclosed, not mechanised): a key owned only via a sibling's
    NON-primary, NON-raw form (a pre-alias-era normalized key of an ALIASED sibling)
    is not treated as cross-owned — reaching it needs an alias-flip era on top of a
    normalize collision, deeper still inside the no-real-master-instance class.

    Parameters
    ----------
    eligible_spots
        The options-eligible underlyings (``EligibleSpot`` or ``(exchange, symbol)`` tuples);
        de-duplicated here so a repeated row is counted once.
    covered_years
        Map ``(exchange, symbol) -> years`` that pulled ≥1 expiry (from the coverage ledger).
        Values may be ANY ``Iterable[int]`` — including a single-shot iterator: each
        distinct probe key's value is read + materialized EXACTLY ONCE per call (REFUTER
        ROUND 7, H1-2), so the forms fold's two reads of the same key (``own_years`` +
        ``years``) can never consume an exhausted iterator into a false-RED verdict.
    expected_years
        The calendar-year span each eligible spot is expected to span (e.g. 2020→today). An
        empty span disables the ``partial`` check (only ``none`` / ``not_attempted`` can fail).
    attempted
        The ``(exchange, symbol)`` pairs that HAVE a coverage-ledger row (an expiry pull was
        actually RUN — see :func:`attempted_pairs_from_records`). A zero-expiry eligible spot
        IN this set is :data:`REASON_NONE` (gating); one NOT in it is
        :data:`REASON_NOT_ATTEMPTED` (neutral). ``None`` (the back-compat default) treats every
        eligible spot AS attempted — the pre-DEC-MAPPING-INTEGRITY-ATTEMPTED-001 behaviour.
    whitelist
        Documented genuine late-listing symbols, excluded from the gating verdict.
    now
        The report timestamp (injected for determinism).
    key_forms
        Optional map of a spot SYMBOL (the eligible spelling, ``"BAJAJ-AUTO"``) to its
        ordered F&O underlying forms superset (``("BAJAJAUTO", "BAJAJ-AUTO")`` — the
        COMMON :func:`~brutex.dashboard.fno_underlying.fno_underlying_forms` output,
        injected as PLAIN DATA so this data-layer leaf never imports the dashboard —
        the DEC-GREEKS-SPOTJOIN-STOCK-001 injection template). Keyed by bare symbol
        (the resolver is exchange-agnostic; the loop holds ``exchange`` to build each
        probe pair). ``None`` (the default) = exact-key compare, byte-identical.

    Returns
    -------
    MappingIntegrityReport
        The verdict + the sorted under-pulled drill-down.
    """
    expected = tuple(sorted(set(expected_years)))
    expected_set = frozenset(expected)
    wl = frozenset(whitelist)
    pairs = sorted({_as_pair(spot) for spot in eligible_spots})
    # ``None`` ⇒ treat every eligible spot as attempted (back-compat); otherwise only the
    # pairs WITH a coverage row are attempted. ``covered_years`` keys are always a subset of
    # the attempted set (a covered year implies a row), so a covered spot is never miscounted
    # as not-attempted even if a caller passes a narrower ``attempted``.
    attempted_set = frozenset(covered_years) if attempted is None else frozenset(attempted)

    # REFUTER ROUND 5 (Z5-1) — the cross-owner ledger for the forms fold: every
    # ``(exchange, key)`` that is some eligible symbol's OWN key — its exact spot
    # spelling or its primary write form (``forms[0]``) — mapped to the symbol(s)
    # owning it. Derived purely from the injected inputs (the eligible pairs +
    # *key_forms*), so this data-layer leaf stays dashboard-import-free:
    # ``primary_fno_underlying(X)`` IS ``key_forms[X][0]`` by the
    # ``fno_underlying_forms`` construction.
    owners: dict[tuple[str, str], set[str]] = {}
    for exchange, symbol in pairs:
        forms = (key_forms or {}).get(symbol) or (symbol,)
        for owned in {symbol, forms[0]}:
            owners.setdefault((exchange, owned), set()).add(symbol)

    # REFUTER ROUND 7 (H1-2) — materialize each ``(exchange, form)`` probe's years
    # EXACTLY ONCE. ``covered_years`` values are typed ``Iterable[int]``, which legally
    # admits a single-shot iterator; the round-2/5 fold reads the SAME mapping value in
    # the ``own_years`` fold AND AGAIN in the ``years`` fold (the raw key is always in
    # both), so a second raw ``covered_years.get`` iteration would consume an EXHAUSTED
    # iterator and flip a fully-covered symbol to a false-RED ``none``. One ``.get`` +
    # one materialization per DISTINCT probe key, memoised for this call — re-iterable
    # inputs (every in-repo producer feeds ``dict[..., set[int]]``) are byte-identical.
    years_memo: dict[tuple[str, str], frozenset[int]] = {}

    def _probe_years(exchange: str, form: str) -> frozenset[int]:
        key = (exchange, form)
        got = years_memo.get(key)
        if got is None:
            got = frozenset(covered_years.get(key, ()))
            years_memo[key] = got
        return got

    under: list[UnderPulledSpot] = []
    fully_covered = 0
    not_attempted = 0
    for exchange, symbol in pairs:
        # DEC-MAPPING-INTEGRITY-FORM-KEY-001: probe every underlying form the coverage
        # writer may have keyed this symbol under. The single `or (symbol,)` covers ALL
        # THREE degrades in one expression — `key_forms is None`, symbol absent from
        # the map, and an EMPTY forms tuple (the blank-symbol degenerate,
        # DEC-FNO-UNDERLYING-EMPTY-GUARD-001) — each falling back to today's exact raw
        # key, never a guess.
        forms = (key_forms or {}).get(symbol) or (symbol,)
        # REFUTER ROUND 5 (Z5-1) — the cross-owner guard: a form that is itself a
        # DIFFERENT eligible symbol's own key (its exact spot spelling OR its primary
        # form) is CROSS-OWNED — coverage under that key may belong to the sibling.
        # Cross-owned forms still RESOLVE coverage (the with-legs arm-(a) law stands),
        # but this symbol's silent-vanish verdict rides its OWN forms; the exact spot
        # key always stays in (the main-parity floor), so the guard's worst case is
        # today's exact-key verdict — never a masked GREEN. The trailing
        # `or (symbol,)` covers the degenerate injected map whose every form is
        # cross-owned (raw spelling absent) — the own-evidence probe then degrades
        # to the exact raw key.
        own_forms = tuple(
            form
            for form in forms
            if form == symbol or owners.get((exchange, form), _NO_OWNERS) <= {symbol}
        ) or (symbol,)
        own_years = frozenset(y for form in own_forms for y in _probe_years(exchange, form))
        own_attempted = attempted is not None and any(
            (exchange, form) in attempted_set for form in own_forms
        )
        years = frozenset(y for form in forms for y in _probe_years(exchange, form))
        if own_attempted and not own_years:
            # Z5-1: this symbol's OWN keys read attempted-and-ZERO — the gating
            # silent-vanish — and a normalize-colliding sibling's coverage (a
            # cross-owned form) may NEVER mask it GREEN. The verdict matches main's
            # exact-key RED on the colliding shape, never a stolen fully_covered.
            # ``attempted is None`` (the back-compat blanket) is NOT own-key
            # evidence, so it never arms the guard — the full fold below keeps the
            # with-legs resolution byte-identical there.
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
        elif not years:
            was_attempted = attempted is None or any(
                (exchange, form) in attempted_set for form in forms
            )
            reason = REASON_NONE if was_attempted else REASON_NOT_ATTEMPTED
            if not was_attempted:
                not_attempted += 1
            under.append(
                UnderPulledSpot(
                    exchange=exchange,
                    symbol=symbol,
                    reason=reason,
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
        not_attempted=not_attempted,
        under_pulled=tuple(under),
        expected_years=expected,
        whitelist=tuple(sorted(wl)),
    )


def report_to_dict(report: MappingIntegrityReport) -> dict[str, Any]:
    """Project a :class:`MappingIntegrityReport` to the JSON the dashboard route returns."""
    return {
        "generated_at": report.generated_at,
        "is_green": report.is_green,
        "total_eligible": report.total_eligible,
        "fully_covered": report.fully_covered,
        "not_attempted": report.not_attempted,
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
    "REASON_NOT_ATTEMPTED",
    "REASON_PARTIAL",
    "EligibleSpot",
    "MappingIntegrityReport",
    "UnderPulledSpot",
    "attempted_pairs_from_records",
    "coverage_years_from_records",
    "reconcile_mapping_integrity",
    "report_to_dict",
]
