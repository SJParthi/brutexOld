"""NIFTY-futures SERIES resolution — the ADR-0012 launch resolver.

DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001 / DEC-FUTURES-ADDRESSING-PIN-001: the
operator addresses the NIFTY-futures engine track by ONE frozen series alias,
:data:`brutex.universe.NIFTY_FUTURES_SERIES_SYMBOL` (``"NSE-NIFTY-FUT"``) —
deliberately NOT a lake directory name. This module resolves that alias, for
one sweep window, into the ordered per-contract lake partitions
``bars/NSE/FNO/NSE-NIFTY-<DDMmmYY>-FUT`` with **vendor-truth ownership
windows**: each IST calendar date is owned by exactly one contract, the one
whose vendor-parsed expiry is the first at-or-after that date (the
``resolve_near_month`` ``>=`` law — expiry day itself is owned by the
EXPIRING contract, ADR-0012 D11 / :mod:`brutex.data.futures_roll`).

## Vendor-dir-primary, calendar observe-only

Ownership derives from the LAKE DIRECTORY NAMES (each parsed by the one
canonical :func:`brutex.data.fno_symbol.parse_fno_symbol` — the #882
scan-and-parse pattern, never ``strftime`` reconstruction): the vendor's
holiday-shifted expiry token is the truth the pull actually stored. The
calendar (:func:`brutex.data.futures_roll.resolve_active_future_expiry`) is an
OBSERVE-ONLY cross-check — a computed-vs-parsed drift is WARNed with evidence
(:class:`CalendarCrossCheck`), a coverage horizon (2027+) abstains — so the
UNVERIFIED calendar==vendor equality and the calendar-table horizon are both
off the critical path (Golden Rule #1: the unverified equality is never
load-bearing).

## Failure-mode law (loud refusal vs graceful abstain)

* **Interior hole** (a missing monthly contract dir between two selected
  expiries, OR a window that STARTS inside such a hole — the pre-hole
  contract's expiry precedes the window start, so it is not selected, yet
  the start date belongs to the MISSING contract) → LOUD
  :class:`FuturesSeriesError` naming the missing expiry month(s) — the worst
  silent-corruption class (a hole would silently span two contracts across a
  basis gap). NOT maskable by ``--allow-missing-slices`` (that flag governs
  empty ``(symbol, interval)`` pairs, never interior continuity).
* **Head/tail unowned window spans** → GRACEFUL + one
  ``futures_series.head_unowned`` / ``futures_series.tail_unowned`` WARN
  (ANY positive span WARNs) + resolution evidence fields. The head span
  stays UNOWNED — ownership is never extended backwards, so the first
  contract's pre-front-month (far-month basis) bars are never served as
  series bars; the span simply loads nothing (true spot-mirroring: a spot
  sweep over a partially-backfilled lake silently loads what exists — and
  nothing exists for a missing day).
* **Registry absent** (``registry.duckdb``, a CLI-only lake) → NON-EVENT: the
  resolver never reads it — vendor dir names are the truth (the droppable-
  cache law, MIGRATION.md #1034).

## Complexity (honest, Golden Rule #10)

One ``os.scandir`` over ``bars/NSE/FNO`` — O(D) in the FNO dir count (D
includes thousands of option dirs; per-entry cost is one suffix compare, plus
a parse for ``-FUT`` names) + O(C log C) sort over C ≈ 78 futures contracts +
O(C) window math — ONCE per launch, never per bar. Honestly O(D), NOT O(1).

Pure scan + parse + window math: NO Catalog, NO ReplayFeed, NO registry, NO
network. The ``ReplayFeed``-consuming loader lives in
:mod:`brutex.feed.series_load` (feed→data is the existing import direction).
``futures_roll`` is imported lazily inside the cross-check/naming helpers only
(it pulls the trading_core wheel), so importing this module stays wheel-light.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Final

import structlog

from brutex.data.errors import DataLakeError
from brutex.data.fno_symbol import FUTURE_TYPE, parse_fno_symbol
from brutex.universe import NIFTY_FUTURES_SERIES_SYMBOL

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from pathlib import Path

__all__ = [
    "MAX_MONTHLY_EXPIRY_GAP_DAYS",
    "CalendarCrossCheck",
    "FuturesSeriesError",
    "FuturesSeriesResolution",
    "FuturesSeriesSegment",
    "is_futures_series_symbol",
    "resolve_futures_series",
]

_LOG = structlog.get_logger("brutex.data.futures_series")

#: The lake exchange + underlying + segment the ADR-0012 track resolves under
#: (NIFTY futures trade on NSE, stored under the FNO segment partition).
_SERIES_EXCHANGE: Final[str] = "NSE"
_SERIES_UNDERLYING: Final[str] = "NIFTY"
_SERIES_SEGMENT: Final[str] = "FNO"

#: Max calendar-day gap between consecutive monthly expiries before the series
#: is declared HOLED (missing contract). Monthly last-Thu/last-Tue expiries are
#: 26-36 days apart (holiday shift ±3); a skipped month is >= ~56. The bound is
#: property-pinned against the real 2020-2026 enumeration.
MAX_MONTHLY_EXPIRY_GAP_DAYS: Final[int] = 40

#: How close (calendar days) a parsed lake expiry must sit to a calendar-
#: computed one to be named as "nearest" evidence on an expiry-drift WARN.
_DRIFT_NEIGHBOUR_DAYS: Final[int] = 7


class FuturesSeriesError(DataLakeError):
    """Loud-refusal carrier — the message IS the operator remediation."""


@dataclass(frozen=True, slots=True)
class FuturesSeriesSegment:
    """One contract's slice of the stitched series (disjoint IST-date ownership).

    Attributes
    ----------
    contract_symbol
        The REAL lake dir name, e.g. ``"NSE-NIFTY-30Sep25-FUT"``.
    expiry
        Vendor-parsed from the dir token (the holiday-shifted truth).
    own_start
        First owned IST date (previous expiry + 1 day; the head segment keeps
        its NATURAL start even when the window begins earlier — see the head
        law in :func:`resolve_futures_series`).
    own_end
        Last owned IST date, INCLUSIVE (expiry-day ownership — the ``>=`` law:
        the expiry day is owned by the EXPIRING contract).
    """

    contract_symbol: str
    expiry: date
    own_start: date
    own_end: date


@dataclass(frozen=True, slots=True)
class CalendarCrossCheck:
    """Observe-only calendar-vs-vendor expiry evidence (FIELDS, not log-only).

    Attributes
    ----------
    expiry
        The vendor-parsed segment expiry.
    computed
        The calendar-computed active expiry at the segment's ``own_start``, or
        ``None`` on a coverage abstain.
    status
        ``"ok"`` | ``"drift"`` | ``"abstain"``.
    """

    expiry: date
    computed: date | None
    status: str


@dataclass(frozen=True, slots=True)
class FuturesSeriesResolution:
    """The full launch-time resolution of the series alias over one window.

    Attributes
    ----------
    segments
        Sorted by expiry; the ownership windows partition the OWNED part of
        the window's date axis (every owned in-window date owned by exactly
        one segment; a head-unowned span — ``head_clamped`` — is owned by NO
        segment and loads nothing).
    roll_schedule
        ``(segment.expiry.toordinal(), ...)`` — the ONE source of truth for
        the engine's roll boundaries (fed to
        :class:`brutex.engine.stage1a.Stage1AMatrixBuilder` and into
        ``canonical_trade_params``).
    contract_symbols
        The per-contract lake dirs, for the G1 freshness-gate expansion
        (probing the alias dir would silently abstain — a gate bypass).
    unparsed_dirs
        ``-FUT``-suffixed dirs :func:`~brutex.data.fno_symbol.parse_fno_symbol`
        could not decode (the 4-digit-year trap etc.) — skipped, WARNed,
        returned as evidence (never guessed, Golden Rule #1).
    cross_checks
        Per-segment calendar-vs-vendor evidence rows (WARNed AND returned).
    head_clamped
        The window start predates the first owned date (graceful: WARNed and
        left UNOWNED — never a backward ownership extension).
    tail_unowned_days
        Days the window end overhangs the last parsed expiry (0 when none;
        ANY positive overhang WARNs — the successor contract is absent).
    """

    segments: tuple[FuturesSeriesSegment, ...]
    roll_schedule: tuple[int, ...]
    contract_symbols: tuple[str, ...]
    unparsed_dirs: tuple[str, ...]
    cross_checks: tuple[CalendarCrossCheck, ...]
    head_clamped: bool
    tail_unowned_days: int


def is_futures_series_symbol(symbol: str) -> bool:
    """Return True iff *symbol* is the pinned NIFTY-futures series alias.

    Import-identity with :data:`brutex.universe.NIFTY_FUTURES_SERIES_SYMBOL`
    (no second literal exists to drift). Exact-case, O(1).
    """
    return symbol == NIFTY_FUTURES_SERIES_SYMBOL


def _calendar_active_expiry(on_date: date) -> date:
    """Calendar-computed active NIFTY futures expiry at *on_date* (lazy wheel import).

    Thin seam over :func:`brutex.data.futures_roll.resolve_active_future_expiry`
    so the wheel-heavy import happens only when a cross-check / naming helper
    actually runs (and so tests can monkeypatch ONE symbol).
    """
    from brutex.data.futures_roll import resolve_active_future_expiry
    from brutex.options.lot_size_history import OptionsUnderlying

    return resolve_active_future_expiry(OptionsUnderlying.NIFTY, on_date)


def _calendar_abstain_errors() -> tuple[type[Exception], ...]:
    """The exception set on which calendar-derived helpers ABSTAIN (never refuse).

    ``HolidayDataUnavailableError`` — the 2027+ coverage horizon;
    ``ValueError`` — the pre-2020 holiday-walk / regime edge. Both mean "the
    calendar cannot answer", never "the lake is wrong" (Golden Rule #1).
    """
    from brutex.calendars.nse import HolidayDataUnavailableError

    return (HolidayDataUnavailableError, ValueError)


def _name_missing_months(prev_expiry: date, next_expiry: date) -> list[str]:
    """Name the monthly expiries the calendar expects strictly inside a gap.

    Walks :func:`_calendar_active_expiry` from ``prev_expiry + 1 day``,
    collecting computed expiries strictly before *next_expiry* (each step is
    O(1): regime lookup + bounded holiday walk + one FFI). Returns
    ``["Sep 2025", ...]``; an empty list means the NAMING abstained (calendar
    coverage) — the gap TRIGGER itself never depends on calendar coverage.
    """
    months: list[str] = []
    cursor = prev_expiry + timedelta(days=1)
    try:
        while True:
            computed = _calendar_active_expiry(cursor)
            if computed >= next_expiry:
                break
            months.append(computed.strftime("%b %Y"))
            cursor = computed + timedelta(days=1)
    except _calendar_abstain_errors():
        return []
    return months


def _interior_hole_error(
    prev_symbol: str, prev_expiry: date, next_symbol: str, next_expiry: date
) -> FuturesSeriesError:
    """Build the loud interior-hole refusal (shared by BOTH continuity gates).

    Names the missing month(s) where the calendar can (naming-only — the gap
    TRIGGER never depends on calendar coverage), else the raw day-gap count.
    """
    gap_days = (next_expiry - prev_expiry).days
    months = _name_missing_months(prev_expiry, next_expiry)
    what = (
        f"the {', '.join(months)} monthly expiry(ies)"
        if months
        else f"a {gap_days}-day expiry gap (calendar naming unavailable)"
    )
    return FuturesSeriesError(
        f"INTERIOR hole in the NIFTY futures series: contract dir for "
        f"{what} is absent between {prev_symbol!r} "
        f"(expiry {prev_expiry.isoformat()}) and "
        f"{next_symbol!r} (expiry {next_expiry.isoformat()}) "
        "— pull it via the dashboard ▶ Pull · full pipeline (a CLI "
        "'brutex fetch' pull leaves derived TFs absent until the next "
        "pipeline run). NOT maskable by --allow-missing-slices (an interior "
        "hole would silently span two contracts)"
    )


def _head_own_start(first_expiry: date) -> date | None:
    """Calendar-fallback natural ``own_start`` for the very FIRST lake dir.

    The previous month's holiday-shifted expiry + 1 day, computed from the
    first day of the month BEFORE *first_expiry*'s month (monthly expiries
    always fall after the 1st, so the calendar resolves the previous month's
    contract). ``None`` on a coverage abstain — the pre-floor edge (a
    Dec-2019 computation under a Jan-2020 first expiry) AND the 2027+
    holiday-table horizon; the caller then bounds ownership at
    ``expiry - MAX_MONTHLY_EXPIRY_GAP_DAYS`` instead of ever extending it to
    the window start (review round-2 finding 8).
    """
    first_of_month = first_expiry.replace(day=1)
    first_of_prev_month = (first_of_month - timedelta(days=1)).replace(day=1)
    try:
        prev_expiry = _calendar_active_expiry(first_of_prev_month)
    except _calendar_abstain_errors():
        return None
    return prev_expiry + timedelta(days=1)


def _scan_future_dirs(fno_root: Path) -> tuple[list[tuple[str, date]], list[str]]:
    """Scan ``bars/NSE/FNO`` for NIFTY ``-FUT`` dirs → (parsed, unparsed_evidence).

    ONE ``os.scandir``; ``-FUT`` suffix prefilter, then the canonical
    :func:`~brutex.data.fno_symbol.parse_fno_symbol` filter (exchange NSE +
    underlying NIFTY + instrument_type FUT). Unparseable ``-FUT`` names are
    WARNed + returned as evidence, never guessed — including a hand-created
    literal ``NSE-NIFTY-FUT`` poison dir, which parses to ``None`` and is
    ignored (the alias never reaches a path-building API).
    """
    import os

    parsed: list[tuple[str, date]] = []
    unparsed: list[str] = []
    with os.scandir(fno_root) as entries:
        for entry in entries:
            name = entry.name
            if not name.endswith(f"-{FUTURE_TYPE}"):
                continue
            if not entry.is_dir(follow_symlinks=False):
                # A stray regular FILE — or a SYMLINK, even one pointing at a
                # real directory — named like a contract must never become an
                # owning segment (a link at a foreign contract's dir would
                # serve that target's bars as NIFTY series bars under this
                # expiry's ownership window — the exact poisoning the FILE
                # guard exists to prevent; review round-3 finding 3). Skipped
                # + WARNed, never guessed or followed.
                _LOG.warning("futures_series.non_dir_entry", entry=repr(name))
                continue
            parts = parse_fno_symbol(name)
            if parts is None:
                unparsed.append(name)
                # repr()-named: the dir name is hostile-controlled on-disk
                # input — newline/ANSI bytes must not forge log lines (the
                # #907/#919 repr-naming convention).
                _LOG.warning("futures_series.unparsed_dir", dir=repr(name))
                continue
            if (
                parts.exchange == _SERIES_EXCHANGE
                and parts.underlying == _SERIES_UNDERLYING
                and parts.instrument_type == FUTURE_TYPE
            ):
                parsed.append((name, parts.expiry))
    return parsed, sorted(unparsed)


def resolve_futures_series(
    lake_root: Path,
    *,
    start: date,
    end: date,
) -> FuturesSeriesResolution:
    """Resolve the NIFTY-futures series alias over ``[start, end]`` (IST dates).

    See the module docstring for the ownership + failure-mode law. O(FNO dirs)
    once per launch (Golden Rule #10 — honestly NOT O(1)).

    Parameters
    ----------
    lake_root
        The lake root (the dir holding ``bars/``).
    start, end
        The sweep window as IST calendar dates, both inclusive.

    Returns
    -------
    FuturesSeriesResolution
        Ordered segments + roll schedule + evidence fields.

    Raises
    ------
    FuturesSeriesError
        On an inverted window, a lake with zero NIFTY ``-FUT`` dirs, a window
        no contract's ownership intersects, two dirs sharing one parsed
        expiry (ambiguous lake), or an INTERIOR continuity hole (missing
        monthly contract — named by month where the calendar can name it),
        including a window that STARTS inside such a hole (the first selected
        segment's predecessor dir gaps past the monthly bound).
    """
    if end < start:
        raise FuturesSeriesError(
            f"futures series window is inverted: start {start.isoformat()} > end {end.isoformat()}"
        )
    fno_root = lake_root / "bars" / _SERIES_EXCHANGE / _SERIES_SEGMENT
    if not fno_root.is_dir():
        raise FuturesSeriesError(
            f"no {_SERIES_EXCHANGE}/{_SERIES_SEGMENT} lake tree at {fno_root} — no NIFTY "
            "futures contracts have been pulled; run the dashboard ▶ Pull · full "
            "pipeline (the F&O phase pulls every contract + the prebuild derives "
            "its timeframes)"
        )
    parsed, unparsed = _scan_future_dirs(fno_root)
    if not parsed:
        raise FuturesSeriesError(
            f"zero NIFTY futures contract dirs under {fno_root} — pull the F&O "
            "history via the dashboard ▶ Pull · full pipeline (a CLI 'brutex fetch' "
            "pull leaves derived TFs absent until the next pipeline run)"
        )

    # Duplicate-expiry refusal (ambiguous lake — two dirs claim one contract).
    by_expiry: dict[date, str] = {}
    for name, expiry in sorted(parsed, key=lambda item: (item[1], item[0])):
        other = by_expiry.get(expiry)
        if other is not None:
            raise FuturesSeriesError(
                f"ambiguous lake: contract dirs {other!r} and {name!r} both parse "
                f"to expiry {expiry.isoformat()} — quarantine the foreign dir before "
                "launching"
            )
        by_expiry[expiry] = name
    ordered = sorted(by_expiry.items())  # [(expiry, dir_name)] ascending

    # Ownership windows over the FULL scanned set (vendor truth first): the
    # predecessor is taken from the full dir set, not only in-window dirs.
    head_clamped = False
    selected: list[FuturesSeriesSegment] = []
    for idx, (expiry, name) in enumerate(ordered):
        if idx > 0:
            own_start = ordered[idx - 1][0] + timedelta(days=1)
        else:
            fallback = _head_own_start(expiry)
            # A None fallback is a calendar-coverage ABSTAIN — the pre-2020
            # floor edge AND the 2027+ horizon both land here. NEVER adopt the
            # window start (review round-2 finding 8): that silently extended
            # the first contract's ownership arbitrarily backwards over dates
            # truly owned by MISSING predecessor contracts, so its far-month
            # bars loaded as series bars across un-reset roll gaps (a silent
            # D11 violation with zero head_unowned WARN). Bound the abstain at
            # the maximum monthly span instead — the resolver cannot vouch for
            # anything earlier, so an earlier window span stays UNOWNED and
            # the head law below WARNs it. Byte-identical at the 2020 floor
            # (the bound predates the floor, so the intersection clamp lands
            # on the window start exactly as before — regression-pinned).
            own_start = (
                expiry - timedelta(days=MAX_MONTHLY_EXPIRY_GAP_DAYS)
                if fallback is None
                else fallback
            )
        if own_start > end or expiry < start:
            continue  # window does not intersect this contract's ownership
        selected.append(
            FuturesSeriesSegment(
                contract_symbol=name,
                expiry=expiry,
                own_start=own_start,
                own_end=expiry,
            )
        )

    if not selected:
        first_expiry, last_expiry = ordered[0][0], ordered[-1][0]
        raise FuturesSeriesError(
            f"no NIFTY futures contract owns any date in [{start.isoformat()} → "
            f"{end.isoformat()}] — the lake's parsed expiries span "
            f"{first_expiry.isoformat()} → {last_expiry.isoformat()}; adjust the "
            "window or pull the missing contracts via the dashboard ▶ Pull · full "
            "pipeline"
        )

    # Continuity gate — INTERIOR holes always refuse (the worst silent-
    # corruption class: a hole would silently span two contracts).
    for prev_seg, next_seg in itertools.pairwise(selected):
        gap_days = (next_seg.expiry - prev_seg.expiry).days
        if gap_days > MAX_MONTHLY_EXPIRY_GAP_DAYS:
            raise _interior_hole_error(
                prev_seg.contract_symbol,
                prev_seg.expiry,
                next_seg.contract_symbol,
                next_seg.expiry,
            )

    # Continuity gate, START side (review round-1 findings 1/10): the pairwise
    # gate above only sees SELECTED segments, so a window that STARTS inside a
    # hole — the pre-hole contract's expiry precedes the window start, which
    # excluded it from selection — would otherwise be served silently by the
    # successor's back-month bars. Check the FIRST selected segment against
    # its predecessor in the FULL ordered dir set: a predecessor was excluded
    # only because its expiry precedes the window start (a later dir WAS
    # selected), so an over-bound gap there means the window start sits on
    # dates owned by a MISSING contract → the SAME loud refusal.
    first = selected[0]
    first_pos = next(pos for pos, (expiry, _name) in enumerate(ordered) if expiry == first.expiry)
    if first_pos > 0:
        pred_expiry, pred_name = ordered[first_pos - 1]
        if (first.expiry - pred_expiry).days > MAX_MONTHLY_EXPIRY_GAP_DAYS:
            raise _interior_hole_error(pred_name, pred_expiry, first.contract_symbol, first.expiry)

    # Head law (review round-1 findings 14/18/19): an unowned span before the
    # first owned date stays UNOWNED — WARNed + recorded, never a refusal AND
    # never a backward ownership extension. Extending segment[0] to the window
    # start would serve the first contract's pre-front-month (far-month basis)
    # bars as series bars; leaving the span unowned makes it load NOTHING —
    # true spot-mirroring (a missing spot day loads nothing either). Keeping
    # own_start natural also keeps the observe-only calendar cross-check below
    # honest (it evaluates at the segment's REAL first owned date).
    if start < first.own_start:
        _LOG.warning(
            "futures_series.head_unowned",
            window_start=start.isoformat(),
            first_owned=first.own_start.isoformat(),
            contract=first.contract_symbol,
            unowned_days=(first.own_start - start).days,
        )
        head_clamped = True
    elif first.own_start < start:
        # Normal intersection clamp INTO the window.
        selected[0] = FuturesSeriesSegment(
            contract_symbol=first.contract_symbol,
            expiry=first.expiry,
            own_start=start,
            own_end=first.own_end,
        )

    # Tail law (review round-1 findings 4/12/20): ANY positive overhang past
    # the last parsed expiry means the successor contract owning those dates
    # is ABSENT from the lake (a current lake's live front month expires
    # on/after today), so the truncation is WARNed at ANY size — a missing
    # NEWEST contract is visible, never silent; no bars can exist beyond the
    # last dir's pull tail.
    last = selected[-1]
    tail_unowned_days = max(0, (end - last.expiry).days)
    if tail_unowned_days > 0:
        _LOG.warning(
            "futures_series.tail_unowned",
            window_end=end.isoformat(),
            last_expiry=last.expiry.isoformat(),
            unowned_days=tail_unowned_days,
            contract=last.contract_symbol,
            exceeds_monthly_gap=tail_unowned_days > MAX_MONTHLY_EXPIRY_GAP_DAYS,
        )
    if last.own_end > end:
        selected[-1] = FuturesSeriesSegment(
            contract_symbol=last.contract_symbol,
            expiry=last.expiry,
            own_start=selected[-1].own_start,
            own_end=end,
        )

    # Observe-only calendar cross-check (G4/G5 honesty): drift → WARN with BOTH
    # sides named; coverage → abstain. Vendor dates govern ownership either way.
    cross_checks: list[CalendarCrossCheck] = []
    for seg in selected:
        try:
            computed = _calendar_active_expiry(seg.own_start)
        except _calendar_abstain_errors():
            _LOG.warning(
                "futures_series.calendar_coverage_abstain",
                contract=seg.contract_symbol,
                own_start=seg.own_start.isoformat(),
            )
            cross_checks.append(
                CalendarCrossCheck(expiry=seg.expiry, computed=None, status="abstain")
            )
            continue
        if computed != seg.expiry:
            _LOG.warning(
                "futures_series.expiry_drift",
                contract=seg.contract_symbol,
                parsed_expiry=seg.expiry.isoformat(),
                computed_expiry=computed.isoformat(),
                nearest_parsed=[
                    e.isoformat()
                    for e, _ in ordered
                    if abs((e - computed).days) <= _DRIFT_NEIGHBOUR_DAYS
                ],
            )
            cross_checks.append(
                CalendarCrossCheck(expiry=seg.expiry, computed=computed, status="drift")
            )
        else:
            cross_checks.append(
                CalendarCrossCheck(expiry=seg.expiry, computed=computed, status="ok")
            )

    return FuturesSeriesResolution(
        segments=tuple(selected),
        roll_schedule=tuple(seg.expiry.toordinal() for seg in selected),
        contract_symbols=tuple(seg.contract_symbol for seg in selected),
        unparsed_dirs=tuple(unparsed),
        cross_checks=tuple(cross_checks),
        head_clamped=head_clamped,
        tail_unowned_days=tail_unowned_days,
    )
