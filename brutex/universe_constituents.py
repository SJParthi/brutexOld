"""Precise constituent → Dhan ``SECURITY_ID`` mapping (PURE, no I/O).

DEC-NIFTY-TOTAL-MARKET-MAP-001 — Stage 2a of DEC-NIFTY-TOTAL-MARKET-001
(MIGRATION.md). Given an index's parsed constituents (from
:func:`brutex.dashboard.nse_constituents.parse_constituents`, e.g. the ~750
NIFTY Total Market names) and the Dhan instrument master, resolve **each
constituent to its NSE cash-equity ``SECURITY_ID``** — the numeric key the Dhan
charts API actually needs.

## Why ISIN is the load-bearing join key (Golden Rule #1, no hallucination)

The constituent CSV gives a **ticker** (``Symbol``) + a globally-unique
**``ISIN Code``**. The Dhan master gives ``security_id``, ``isin``, and
``symbol_name``. Matching on the ticker alone is unreliable: for a **non-F&O**
Dhan equity (the majority of the ~750 Total-Market names) the engine
``symbol_name`` is the **long company name**, not the ticker — only F&O-eligible
EQ rows carry the ticker in ``symbol_name`` (DEC-DHAN-FNO-JOIN-KEY-001). So:

* **Primary key — ISIN.** Globally unique, immune to ticker drift / dual
  listings. ``constituent.isin`` → ``instrument.isin``.
* **Fallback — ticker.** ``constituent.symbol`` → ``instrument.symbol_name``;
  only resolves the F&O-eligible names (where ``symbol_name`` IS the ticker),
  used solely when a constituent has no ISIN or its ISIN is absent from Dhan.

Both keys are matched **only against NSE cash-equity rows** (``instrument == EQ``
∧ ``exch_id == NSE`` ∧ ``segment == CASH``) so derivative legs and BSE dual
listings never mis-match.

## Performance (O(1) per lookup — CLAUDE.md §2 Rule 5)

The Dhan side is indexed **once** into two hash maps (``isin →`` instrument,
``ticker →`` instrument); each of the N constituents is then a constant-time
lookup. Total ``O(N_master + N_constituents)``, each per-name resolve O(1).

## Determinism (worst case: one ISIN → multiple rows)

If two NSE cash-equity rows share an ISIN (or a ticker), the row with the
**lowest ``security_id``** wins — a deterministic tiebreak independent of input
order, so the same inputs always produce the same mapping (Golden Rule #7).

## Honesty (Golden Rule #10)

Nothing is fabricated: a constituent whose ISIN is absent from Dhan and whose
ticker doesn't match a cash-equity ``symbol_name`` is returned in ``unmatched``
— never assigned a guessed ``SECURITY_ID``. :meth:`MatchReport.summary` makes
the coverage visible (``matched X/Y — A by ISIN, B by ticker; unmatched Z``).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from brutex.dashboard.nse_constituents import ConstituentRow
from brutex.models import Exchange, Instrument, InstrumentType, Segment


@dataclass(frozen=True)
class ConstituentMatch:
    """A resolved constituent → its NSE cash-equity Dhan ``SECURITY_ID``."""

    symbol: str
    isin: str | None
    security_id: int
    #: How the match was made: ``"isin"`` (primary) or ``"ticker"`` (fallback).
    matched_by: str


@dataclass(frozen=True)
class MatchReport:
    """The outcome of resolving a list of constituents against the Dhan master."""

    matched: tuple[ConstituentMatch, ...]
    unmatched: tuple[ConstituentRow, ...]

    @property
    def total(self) -> int:
        """Total constituents considered (matched + unmatched)."""
        return len(self.matched) + len(self.unmatched)

    @property
    def matched_by_isin(self) -> int:
        """How many resolved via the primary ISIN key."""
        return sum(1 for m in self.matched if m.matched_by == "isin")

    @property
    def matched_by_ticker(self) -> int:
        """How many resolved via the ticker fallback."""
        return sum(1 for m in self.matched if m.matched_by == "ticker")

    @property
    def security_ids(self) -> tuple[int, ...]:
        """The resolved ``SECURITY_ID``s, order-preserved by the matched list."""
        return tuple(m.security_id for m in self.matched)

    def summary(self) -> str:
        """A one-line, human-readable coverage summary (honest envelope)."""
        unmatched_syms = ", ".join(c.symbol for c in self.unmatched)
        tail = f"; unmatched {len(self.unmatched)}: [{unmatched_syms}]" if self.unmatched else ""
        return (
            f"matched {len(self.matched)}/{self.total} "
            f"({self.matched_by_isin} by ISIN, {self.matched_by_ticker} by ticker){tail}"
        )


def _nse_cash_equities(instruments: Iterable[Instrument]) -> Iterator[Instrument]:
    """Yield only the NSE cash-equity spot rows (EQ ∧ NSE ∧ CASH)."""
    for ins in instruments:
        if (
            ins.instrument is InstrumentType.EQ
            and ins.exch_id is Exchange.NSE
            and ins.segment is Segment.CASH
        ):
            yield ins


def _index_by_keys(
    instruments: Iterable[Instrument],
) -> tuple[dict[str, Instrument], dict[str, Instrument]]:
    """Build ``{isin → instrument}`` + ``{ticker → instrument}`` over NSE cash equities.

    Built once for O(1) per-constituent resolution. On a key collision the row
    with the **lowest ``security_id``** wins (deterministic, order-independent).
    """
    by_isin: dict[str, Instrument] = {}
    by_ticker: dict[str, Instrument] = {}
    for ins in _nse_cash_equities(instruments):
        if ins.isin:
            key = ins.isin.strip().upper()
            current = by_isin.get(key)
            if current is None or ins.security_id < current.security_id:
                by_isin[key] = ins
        ticker = ins.symbol_name.strip().upper()
        if ticker:
            current_t = by_ticker.get(ticker)
            if current_t is None or ins.security_id < current_t.security_id:
                by_ticker[ticker] = ins
    return by_isin, by_ticker


def resolve_constituents(
    constituents: Iterable[ConstituentRow],
    instruments: Iterable[Instrument],
) -> MatchReport:
    """Resolve constituents → NSE cash-equity ``SECURITY_ID`` (ISIN-primary).

    PURE — no I/O. See the module docstring for the join-key rationale and the
    O(1) / determinism / honesty guarantees.

    Parameters
    ----------
    constituents
        The index members (e.g. from :func:`parse_constituents`).
    instruments
        The Dhan instrument master (any iterable of :class:`Instrument`); only
        the NSE cash-equity rows participate.

    Returns
    -------
    MatchReport
        ``matched`` (with the join method per row) + ``unmatched`` (never
        assigned a guessed id), order-preserved by the *constituents* order.
    """
    by_isin, by_ticker = _index_by_keys(instruments)
    matched: list[ConstituentMatch] = []
    unmatched: list[ConstituentRow] = []
    for constituent in constituents:
        hit: Instrument | None = None
        how = ""
        if constituent.isin is not None:
            hit = by_isin.get(constituent.isin.strip().upper())
            if hit is not None:
                how = "isin"
        if hit is None:
            hit = by_ticker.get(constituent.symbol.strip().upper())
            if hit is not None:
                how = "ticker"
        if hit is None:
            unmatched.append(constituent)
        else:
            matched.append(
                ConstituentMatch(
                    symbol=constituent.symbol,
                    isin=constituent.isin,
                    security_id=hit.security_id,
                    matched_by=how,
                )
            )
    return MatchReport(matched=tuple(matched), unmatched=tuple(unmatched))


class ConstituentCoverageError(Exception):
    """Raised when a constituent basket does not resolve to 100% coverage.

    DEC-NIFTY-TOTAL-MARKET-WIRE-001 (Stage 2b PR-4b): the hostile review's P1
    fail-loud rule — an unmatched constituent is silently absent from the lake,
    so "all 750" quietly becomes "750 − N". Rather than let the count drop in
    silence, :func:`resolve_reference_security_ids` raises this with the
    :meth:`MatchReport.summary` so the operator SEES the gap and acts.
    """


def resolve_reference_security_ids(
    constituents: Iterable[ConstituentRow],
    instruments: Iterable[Instrument],
    *,
    require_full_coverage: bool = True,
) -> frozenset[int]:
    """Resolve constituents → the set of NSE cash-equity ``SECURITY_ID``s to admit.

    DEC-NIFTY-TOTAL-MARKET-WIRE-001. The fail-loud wrapper over
    :func:`resolve_constituents` that the loader/refresh feed into
    ``select_universe``'s / ``upsert_many``'s ``reference_*`` parameters.

    Parameters
    ----------
    constituents
        The index members (e.g. from :func:`parse_constituents`).
    instruments
        The Dhan instrument master (only NSE cash equities participate).
    require_full_coverage
        When ``True`` (default), raise :class:`ConstituentCoverageError` if ANY
        constituent is unmatched — the hostile review's fail-loud rule, so a
        silent coverage drop is impossible. When ``False``, return only the
        matched ids (the caller has explicitly accepted partial coverage).

    Returns
    -------
    frozenset[int]
        The matched NSE cash-equity ``SECURITY_ID``s, ready to pass as
        ``reference_security_ids``.

    Raises
    ------
    ConstituentCoverageError
        If ``require_full_coverage`` and one or more constituents are unmatched.
    """
    report = resolve_constituents(constituents, instruments)
    if require_full_coverage and report.unmatched:
        raise ConstituentCoverageError(report.summary())
    return frozenset(report.security_ids)


__all__ = [
    "ConstituentCoverageError",
    "ConstituentMatch",
    "MatchReport",
    "resolve_constituents",
    "resolve_reference_security_ids",
]
