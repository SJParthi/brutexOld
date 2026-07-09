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

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

#: The base intervals the pull fetches directly from the vendor; anything else is a
#: derived timeframe whose remedy is materialization, not a re-pull.
_FETCHED_INTERVALS: frozenset[str] = frozenset({"1minute", "1day"})


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
        that stage; the remedy is a full-pipeline pull); fetched intervals
        (``1minute``/``1day``) are missing because the pull never covered
        them (the remedy is a pull).
        """
        derived = sorted({i for _, i in self.missing if i not in _FETCHED_INTERVALS})
        fetched = sorted({i for _, i in self.missing if i in _FETCHED_INTERVALS})
        lines: list[str] = []
        if derived:
            lines.append(
                f"derived TFs never materialized ({', '.join(derived)}): run the "
                "dashboard's ▶ Pull · full pipeline — its automatic final prebuild "
                "stage writes every derived timeframe (DEC-PIPELINE-AUTOPREBUILD-001)"
            )
        if fetched:
            lines.append(
                f"fetched intervals with no bars ({', '.join(fetched)}): pull the "
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
    (DEC-MOVERS-STORED-FRESHNESS-001 fold-in) — that no longer equals a fresh
    re-derivation from its 1-minute base (a 1m backfill after the last
    prebuild, a kill-9 mid-incremental-re-derive, a
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

        The escalation clause names the ONE residual class the prebuild alone
        provably cannot clear (review round 2): the derived-``1day`` write is
        a latest-wins MERGE that never DELETES rows, so a non-canonical
        legacy row that duplicates a covered session under a different
        timestamp survives every prebuild — the working remedy there is
        removing the symbol's stored ``1day`` partition first (the prebuild
        fully re-derives it from the 1-minute base). 1m-uncovered legacy days
        no longer refuse at all (they abstain per-bar in
        :func:`~brutex.data.tf_autoderive.compare_daily_freshness`).
        """
        return (
            "stale derived TFs (stored table drifted from a fresh 1-minute "
            "re-derivation): run the dashboard's ▶ Pull · full pipeline — its "
            "automatic final prebuild stage re-derives every derived timeframe "
            "(DEC-PIPELINE-AUTOPREBUILD-001); if a 1day refusal survives a "
            "prebuild (a non-canonical legacy 1day row the latest-wins merge "
            "cannot remove), delete that symbol's stored 1day partition and "
            "re-run the prebuild — it fully re-derives from the 1-minute base"
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
