"""Sweep slice PREFLIGHT — refuse silent no-bars tuples (DEC-SWEEP-PREFLIGHT-001).

PR 4 of the Phase 9.5 unblocking plan (the 4-agent review recorded in
MIGRATION.md #658): ``sweep-all`` pre-loads every ``(symbol, interval)``
slice, and the orchestrator SKIPS empty slices (``tuples_skipped_no_bars``)
— audited but quiet. For an operator who requested 21 timeframes that is a
trap: a forgotten materialization silently turns most of the grid into
no-ops while the sweep "completes". The pilot's coverage promise (all 3
instruments × all 21 TFs) demands a LOUD refusal instead.

This module is the pure core: given the pre-loaded slice sizes, name every
requested ``(symbol, interval)`` pair with ZERO bars and compose the exact
remediation. The CLI shim refuses to launch unless every requested pair has
bars — or the operator explicitly opts into partial coverage with
``--allow-missing-slices`` (deliberate, never accidental).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from brutex.data.fno_symbol import parse_fno_symbol
from brutex.data.session_density import (
    SESSION_DENSITY_ERROR_EVENT,
    SESSION_DENSITY_WARN_CAP,
    SessionDensityFlag,
    classify_session_density,
)
from brutex.universe import NIFTY_FUTURES_SERIES_SYMBOL

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from collections.abc import Set as AbstractSet
    from datetime import date

    from brutex.data.models import CandleBar

_LOG = structlog.get_logger(__name__)

#: The base interval the pull fetches directly from the vendor; anything else is a
#: derived timeframe whose remedy is materialization, not a re-pull. ``1minute``
#: ONLY — ``1day`` is DERIVED from the stored 1-minute since
#: DEC-1MINUTE-ONLY-DERIVE-1DAY-001 (#853), written by the pipeline's automatic
#: FINAL prebuild stage (DEC-PIPELINE-AUTOPREBUILD-001 #1145), so a missing
#: ``1day`` slice remediates like any other derived TF (the stale
#: ``{"1minute", "1day"}`` taxonomy corrected by
#: DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001).
_FETCHED_INTERVALS: frozenset[str] = frozenset({"1minute"})


def _is_fno_symbol(symbol: str) -> bool:
    """True iff *symbol* remediates via the F&O pull (a contract or the alias).

    The NIFTY-futures series alias (ADR-0012 /
    DEC-FUTURES-ADDRESSING-PIN-001) and any parseable F&O contract
    ``groww_symbol`` pull through the pipeline's F&O phase — telling an
    operator to "pull the symbol's spot history" for one would be a WRONG
    remedy (Golden Rule #1).
    """
    return symbol == NIFTY_FUTURES_SERIES_SYMBOL or parse_fno_symbol(symbol) is not None


@dataclass(frozen=True, slots=True)
class PreflightReport:
    """Outcome of the sweep slice preflight.

    Attributes
    ----------
    missing
        Every requested ``(symbol, interval)`` pair whose pre-loaded slice
        holds ZERO bars, in deterministic (symbol-major, request) order.
    requested
        Total requested pairs (symbols × intervals).
    """

    missing: tuple[tuple[str, str], ...]
    requested: int

    @property
    def ok(self) -> bool:
        """True iff every requested pair has at least one bar."""
        return not self.missing

    def remediation(self) -> str:
        """The exact operator remedy for each class of missing pair.

        Derived timeframes are missing because the pipeline's automatic
        FINAL prebuild stage never ran over the symbol
        (DEC-PIPELINE-AUTOPREBUILD-001 — derived data is written ONLY by
        that stage; the remedy is a full-pipeline pull); the fetched
        ``1minute`` interval is missing because the pull never covered it
        (the remedy is a pull — the F&O pull for an F&O contract / the
        NIFTY-futures series alias, the spot pull for a spot symbol;
        DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001).
        """
        derived = sorted({i for _, i in self.missing if i not in _FETCHED_INTERVALS})
        fetched_fno = sorted(
            {i for s, i in self.missing if i in _FETCHED_INTERVALS and _is_fno_symbol(s)}
        )
        fetched_spot = sorted(
            {i for s, i in self.missing if i in _FETCHED_INTERVALS and not _is_fno_symbol(s)}
        )
        lines: list[str] = []
        if derived:
            lines.append(
                f"derived TFs never materialized ({', '.join(derived)}): run the "
                "dashboard's ▶ Pull · full pipeline — its automatic final prebuild "
                "stage writes every derived timeframe (DEC-PIPELINE-AUTOPREBUILD-001)"
            )
        if fetched_fno:
            lines.append(
                f"fetched intervals with no bars ({', '.join(fetched_fno)}): pull the "
                "F&O history first (dashboard ▶ Pull · full pipeline — its F&O phase "
                "pulls every contract; the prebuild then derives its timeframes)"
            )
        if fetched_spot:
            lines.append(
                f"fetched intervals with no bars ({', '.join(fetched_spot)}): pull the "
                "symbol's spot history first (dashboard Pull / `brutex fetch`)"
            )
        return "; ".join(lines)


def preflight_slices(
    slice_sizes: Mapping[tuple[str, str], int],
    *,
    symbols: Sequence[str],
    intervals: Iterable[str],
) -> PreflightReport:
    """Name every requested ``(symbol, interval)`` pair with zero bars.

    ``slice_sizes`` maps each pre-loaded pair to its bar count (absent keys
    count as zero — the loader only records what it found). Deterministic
    output order: symbols in request order, intervals in request order
    within each symbol.
    """
    interval_list = list(intervals)
    missing = tuple(
        (symbol, interval)
        for symbol in symbols
        for interval in interval_list
        if slice_sizes.get((symbol, interval), 0) == 0
    )
    return PreflightReport(missing=missing, requested=len(symbols) * len(interval_list))


@dataclass(frozen=True, slots=True)
class FreshnessReport:
    """Outcome of the sweep derived-timeframe FRESHNESS preflight.

    DEC-SUSPICION-SWEEP-FIXES-001 (G1). The zero-bar :class:`PreflightReport`
    refuses a MISSING slice; this refuses a PRESENT-but-STALE one: a stored
    derived table — an intraday TF OR the derived ``1day``
    (DEC-MOVERS-STORED-FRESHNESS-001 fold-in, lap-8 vehicle) — that no longer
    equals a fresh re-derivation from its
    1-minute base (a 1m backfill after the last prebuild, a kill-9
    mid-incremental-re-derive, a
    pre-DEC-RESAMPLE-ANCHOR-002 grid) would feed the engine bars that disagree
    with the lake's own base data — and the run identity would hash the stale
    snapshot as if it were canonical (Golden Rule #4). Correctness > everything
    (the merge-readiness priority law), so the launch refuses loudly instead.

    Attributes
    ----------
    stale
        Every checked ``(symbol, interval)`` pair whose stored derived table
        drifted from a fresh re-derivation, in sorted deterministic order.
    checked
        Total pairs the freshness source could verify (pairs it abstained on —
        no stored derived partition / no 1-minute base / the fetched
        ``1minute`` / an FNO-segment ``1day`` — are NOT counted; see
        :func:`~brutex.data.tf_autoderive.lake_derived_freshness_verdicts`).
    """

    stale: tuple[tuple[str, str], ...]
    checked: int

    @property
    def ok(self) -> bool:
        """True iff every VERIFIABLE requested derived pair is fresh."""
        return not self.stale

    def remediation(self) -> str:
        """The exact operator remedy: re-run the pipeline's prebuild stage.

        Ghost-bucket escape clause (DEC-DERIVED-FRESHNESS-SCREEN-PARITY-001;
        the prune itself is the named follow-up
        DEC-DERIVED-GHOST-BUCKET-PRUNE-001): with the freshness re-derivation
        now bad-tick-screen-parity with the prebuild's read, the ONE stale
        shape a prebuild re-run can NEVER clear is a stored derived bucket
        whose SOLE 1-minute source is screened away — the latest-wins derived
        writers never delete, so the ghost survives even a forced full
        re-prebuild. This gate has NO escape flag by design, so the printed
        remedy must name the working manual escape or the refusal is a
        permanent wedge (executed-proven: deleting the symbol's derived TF
        partition dirs makes the next prebuild regenerate everything from the
        screened 1-minute, and the gate then reads fresh).

        The derived-``1day`` sibling clause (DEC-MOVERS-STORED-FRESHNESS-001
        fold-in review round 2): the ``1day`` write is the SAME latest-wins
        MERGE class, so a non-canonical legacy row that duplicates a covered
        session under a different timestamp survives every prebuild — the
        working remedy there is likewise removing the symbol's stored
        ``1day`` partition first (the prebuild fully re-derives it from the
        1-minute base). 1m-uncovered legacy days never refuse at all (they
        abstain per-bar in
        :func:`~brutex.data.tf_autoderive.compare_daily_freshness`).
        """
        return (
            "stale derived TFs (stored table drifted from a fresh 1-minute "
            "re-derivation): run the dashboard's ▶ Pull · full pipeline — its "
            "automatic final prebuild stage re-derives every derived timeframe "
            "(DEC-PIPELINE-AUTOPREBUILD-001); if a pair stays stale AFTER that "
            "prebuild, its stored table holds ghost bucket(s) with no surviving "
            "screened 1-minute source (the latest-wins derived writer never "
            "deletes — DEC-DERIVED-GHOST-BUCKET-PRUNE-001): delete the symbol's "
            "derived TF partition dirs, then re-run the prebuild (it regenerates "
            "every derived timeframe from the screened 1-minute); a surviving "
            "1day refusal is the same merge class (a non-canonical legacy 1day "
            "row the latest-wins merge cannot remove) — delete that symbol's "
            "stored 1day partition and re-run the prebuild"
        )


def preflight_derived_freshness(verdicts: Mapping[tuple[str, str], str]) -> FreshnessReport:
    """Fold per-pair freshness *verdicts* into the pure gate report.

    *verdicts* maps each VERIFIABLE requested ``(symbol, interval)`` pair to
    :func:`~brutex.data.tf_autoderive.compare_derived_freshness`'s verdict
    (intraday TFs) or
    :func:`~brutex.data.tf_autoderive.compare_daily_freshness`'s (the derived
    ``1day`` — DEC-MOVERS-STORED-FRESHNESS-001 fold-in), ``"fresh"`` /
    ``"stale"`` — anything not exactly ``"fresh"`` counts stale (fail-closed:
    an unexpected verdict string can never silently pass). Deterministic
    sorted output; pure + O(pairs).
    """
    stale = tuple(sorted(pair for pair, verdict in verdicts.items() if verdict != "fresh"))
    return FreshnessReport(stale=stale, checked=len(verdicts))


@dataclass(frozen=True, slots=True)
class SessionDensityNote:
    """Outcome of the WARN-only sweep-slice session-density preflight.

    DEC-SESSION-DENSITY-CENSUS-001 — the retro-lake arm of the bar-density
    census: post-pull windows are censused at the ingest tail, but a hole
    OLDER than the last pull only surfaces here, at the exact point the
    2-4-day sweep is about to consume the bars. A NOTE, never a gate (the
    ``one_day_trade_law_note`` posture): the sweep proceeds, there is NO
    refusal and NO escape flag — the remediation is a re-pull, and refusing
    a launch over an already-annotated deficit would be pure obstruction.

    Attributes
    ----------
    flagged
        Every ``(symbol, interval, flag)`` triple the census flagged across
        the loaded ``1minute`` slices, symbol-major deterministic order
        (known-irregular deficits are annotated facts, deliberately NOT in
        this WARN list).
    slices_checked
        Non-empty ``1minute`` slices actually censused.
    """

    flagged: tuple[tuple[str, str, SessionDensityFlag], ...]
    slices_checked: int

    @property
    def ok(self) -> bool:
        """True iff no loaded slice carries a flagged low-density session."""
        return not self.flagged

    def lines(self, cap: int = SESSION_DENSITY_WARN_CAP) -> tuple[str, ...]:
        """Printable per-flag lines, capped at *cap* + one honest overflow tail."""
        out = [
            (
                f"{symbol}/{interval} {flag.session.isoformat()}: "
                f"{flag.observed}/{flag.expected} bars ({flag.shape}, "
                f"largest gap {flag.largest_gap_minutes}m)"
            )
            for symbol, interval, flag in self.flagged[:cap]
        ]
        if len(self.flagged) > cap:
            out.append(f"... +{len(self.flagged) - cap} more low-density session(s)")
        return tuple(out)


def preflight_session_density(
    slices: Mapping[tuple[str, str], Sequence[CandleBar]],
    *,
    today_ist: date | None = None,
    observed: AbstractSet[date] = frozenset(),
) -> SessionDensityNote:
    """Census every loaded ``1minute`` slice's per-session bar density.

    DELEGATES to the ONE COMMON classifier
    (:func:`~brutex.data.session_density.classify_session_density` — CLAUDE.md
    §11: no second copy of the deficiency law) over the bars ALREADY loaded
    for the sweep, so this arm costs zero extra lake I/O — O(loaded slice
    bars + days in each slice's covered span) total (the classifier's own
    documented span-walk term; a sparse multi-year slice is walk-dominated —
    review round-4 wording honesty). Only the fetched ``1minute`` slices are
    judged (a derived
    TF's density follows its 1-minute base by construction; judging it would
    double-report every hole). Empty slices are skipped (the zero-bar
    :func:`preflight_slices` refusal owns those). WARN-only by decree — the
    caller prints, never refuses. Deliberately NOT kill-switch-gated HERE
    (no KILL-SWITCH env read inside the classifier/note builder — its two
    threshold tunables remain documented fail-soft internal reads when not
    injected): the ONE census kill switch
    (:data:`~brutex.data.session_density.SESSION_DENSITY_KILL_ENV`) is
    honored by the consumer — ``cmd_sweep_all`` gates this call on
    :func:`~brutex.data.session_density.session_density_census_enabled` — so
    the switch kills ALL census output while the note builder carries no
    kill-switch read of its own.

    *observed* threads the observed-holiday overlay
    (DEC-CALENDAR-OBSERVED-SESSIONS-001) into the classifier exactly like
    BOTH ingest wirings do (review round-3 — the asymmetry re-opened the
    ``missing_session`` false-flag the classifier's ``observed`` param was
    built to close): an overlaid interior date abstains ``non_trading``
    instead of false-flagging at every sweep launch. ``cmd_sweep_all`` loads
    it via the never-raising
    :func:`~brutex.calendars.observed_sessions.load_observed_holidays`.

    Pure modulo the BELT (review round-3, the ingest twin's
    swallow-and-continue discipline): a slice whose classify raises logs ONE
    :data:`~brutex.data.session_density.SESSION_DENSITY_ERROR_EVENT` WARNING
    and is skipped — a WARN-only note must NEVER crash the launch it
    decorates (the classifier is itself total; this is defense-in-depth).
    """
    flagged: list[tuple[str, str, SessionDensityFlag]] = []
    checked = 0
    for symbol, interval in sorted(slices):
        if interval not in _FETCHED_INTERVALS:
            continue
        bars = slices[(symbol, interval)]
        if not bars:
            continue
        try:
            verdict = classify_session_density(
                [bar.timestamp for bar in bars], today_ist=today_ist, observed=observed
            )
        except Exception as exc:  # belt: a WARN-only note never crashes the launch
            _LOG.warning(
                SESSION_DENSITY_ERROR_EVENT,
                symbol=repr(symbol),
                interval=interval,
                error=str(exc),
            )
            continue
        checked += 1
        flagged.extend((symbol, interval, flag) for flag in verdict.flags)
    return SessionDensityNote(flagged=tuple(flagged), slices_checked=checked)


def one_day_trade_law_note(intervals: Sequence[str]) -> str | None:
    """One honest launch note when a requested interval is the ``1day`` grid.

    HONEST limit (Golden Rule #10, review round-8 finding 1): under the
    intraday-only trade law the ``1day`` grid is structurally TRADE-less —
    every derived ``1day`` bar is midnight-IST-stamped, so (a) the next-bar
    entry fill always lands on a LATER IST session date and is dropped by the
    trade sim's overnight-gap rule, and (b) no ``1day`` bar ever reaches the
    15:20 forced-exit window — every mask yields zero trades by construction
    and ``MIN_TRADES`` gates every combo out. The grid still builds real
    Stage-1A ``bar_bits`` (the D12 ``apply_1day_mask`` consumers — see
    :func:`brutex.engine.stage1a.apply_1day_mask_for`), so a ``1day`` launch
    is not refused; it is NAMED so the operator never mistakes an empty
    ``1day`` leaderboard for a data defect. Real ``1day`` trade semantics
    are a named follow-up (DEC-FUTURES-LAUNCH-REVIEW-FIXES-008).

    Returns the note when ``"1day"`` is among *intervals*, else ``None``.
    Pure, O(#intervals).
    """
    if "1day" not in intervals:
        return None
    return (
        "NOTE: interval '1day' ranks NOTHING under the intraday-only trade law — "
        "every 1day bar is midnight-IST-stamped, so the next-bar fill lands on a "
        "later IST session date (dropped by the overnight-gap rule) and no 1day "
        "bar reaches the 15:20 forced exit: zero trades by construction, "
        "MIN_TRADES gates every combo out. The 1day tuples still build bar_bits "
        "(the D12 mask consumers); real 1day trade semantics are a named "
        "follow-up (DEC-FUTURES-LAUNCH-REVIEW-FIXES-008)."
    )
