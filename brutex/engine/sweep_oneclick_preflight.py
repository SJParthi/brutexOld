"""One-click institutional sweep PREFLIGHT verdict (R4/R20/R23 + gap-hunt rulings).

The composed backend for the one-click institutional sweep button's HARD
DATA PREFLIGHT (R4) + LIVE pre-run ESTIMATOR (R20): per symbol × timeframe ×
date-range, verify 1-minute coverage against the REAL NSE trading calendar
(holidays/weekends/Muhurat are NOT "missing"), reuse the existing G1
derived-timeframe-freshness refusal gate, DISK-verify (never trust the
ledger alone), check for a concurrent pull, check free disk space, verify
futures-contract coverage for the execution-translation leg (P5's module),
verify India-VIX coverage (gap-hunt ruling #7), and compute the live trial/
seconds/bytes estimate (R20/R23) — all folded into ONE
:class:`OneClickPreflightVerdict` in the exact
``{ok, refusals, clamps, estimator}`` integration-contract shape.

This module NEVER pulls (R3: the button is SWEEP-ONLY; ``/universe`` owns
pull→prebuild→heal) and NEVER edits another phase's files — it calls
:func:`brutex.futures.preflight.preflight_futures_months` (P5's shipped
module) and reuses :func:`brutex.engine.sweep_preflight.preflight_derived_freshness`
(the existing G1 gate) rather than reimplementing either.

## Edge clamps (R4)

* **Pre-2020 floor clamp**: a *start* before the calendar's published
  coverage floor is clamped up, with a note.
* **Future-to-date clamp**: an *end* beyond today (or beyond the calendar's
  published coverage ceiling) is clamped down to the last verifiable
  session.
* **All-holiday zero-session refusal**: a clamped window with ZERO tradable
  NSE sessions is an honest whole-request refusal, never a silent empty
  sweep.
* **Partial availability → refuse-whole** (ruling O-a): ANY named gap
  refuses the ENTIRE request — the institutional default
  (``allow_missing_slices=False``) never silently shrinks the grid.

## VWAP dead-bits (gap-hunt ruling #8) — DEFAULT flag-and-run

EXCLUDE-vs-FLAG is an explicit OPERATOR DECISION still open (queued, ruling
#8) — this module DEFAULTS to flag-and-run: the swept condition vocabulary
is NEVER reduced here; :func:`brutex.engine.sweep_estimator.effective_condition_count`
is surfaced as an honest annotation (a :class:`Clamp` entry) alongside the
nominal count, never as a silent trial-count reduction.

## Multi-symbol / SENSEX (gap-hunt ruling #9) — DEFAULT flag-and-run

Run semantics for a fan-out spanning multiple exchanges (SENSEX is
BSE-listed, NIFTY/BANKNIFTY are NSE-listed) are likewise an explicit
OPERATOR DECISION still open. This module's contract is per-EXCHANGE-GROUP
(one verdict per ``(exchange, segment)`` coordinate); the ONE-logical-run_id
fan-out that wraps N per-exchange verdicts is composed by
:mod:`brutex.dashboard.sweep_runner` (the entry-point wiring layer) — DEFAULT
flag-and-run: every exchange group is preflighted and estimated
independently and a refusal in ANY group refuses the WHOLE logical run.

## Honest envelope (Golden Rule #10)

Every arm here is cheap EXCEPT two, both honestly documented at their call
site rather than falsely claimed O(1): the derived-freshness reuse
(:func:`brutex.data.tf_autoderive.lake_derived_freshness_verdicts`) is
``O(bars)`` — it re-derives from 1-minute to compare, the G1 gate's own
pre-existing cost; and :func:`brutex.data.lake_footprint.evaluate_disk_guard`
internally measures the footprint of the WHOLE lake
(``O(files in the lake)``, an unscoped ``rglob``), NOT just the requested
symbols/months. A multi-exchange-group caller MUST compute the disk decision
ONCE per logical plan and pass it via :func:`run_oneclick_preflight`'s
*disk_decision* parameter — never once per group — or one click pays a
full-lake scan N times over (:func:`brutex.dashboard.sweep_runner.oneclick_preflight_for_plan`
does this correctly). Every other arm is ``O(symbols × months)`` disk-exists
probes or ``O(1)`` arithmetic/lock-probes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final

from brutex.calendars.nse import (
    HOLIDAY_COVERAGE_FIRST_YEAR,
    HOLIDAY_COVERAGE_LAST_YEAR,
    trading_days_between_checked,
)
from brutex.dashboard.pull_intent import probe_pull_singleflight
from brutex.data.lake_footprint import DiskGuardDecision, evaluate_disk_guard
from brutex.data.lake_invariants import scan_lake_invariants
from brutex.data.partition import build_partition_path
from brutex.data.tf_autoderive import lake_derived_freshness_verdicts
from brutex.engine.estimator_calibration import load_estimator_calibration
from brutex.engine.sweep_estimator import EstimatorEstimate, estimate_sweep_cost
from brutex.engine.sweep_preflight import preflight_derived_freshness
from brutex.engine.sweep_sla_guard import SlaDecision, check_sweep_sla
from brutex.futures.contract_resolver import RegistryFrontMonthResolver
from brutex.futures.preflight import preflight_futures_months

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

__all__ = [
    "INDIA_VIX_SYMBOL",
    "SPOT_1M_INTERVAL",
    "Clamp",
    "OneClickPreflightVerdict",
    "Refusal",
    "WindowClamp",
    "clamp_sweep_window",
    "missing_1m_partitions",
    "run_oneclick_preflight",
]

#: The lake symbol for India VIX (gap-hunt ruling #7's coverage arm) —
#: reference-only, never a swept underlying (CLAUDE.md §2 Rule 3).
INDIA_VIX_SYMBOL: Final[str] = "INDIAVIX"

#: The ONLY exchange India VIX is ever published under (it is an NSE index —
#: there is no BSE India-VIX partition, ever, by design). A round-3
#: adversarial-refuter review found the VIX arm probed the CALLING group's
#: own ``exchange`` unconditionally, so a SENSEX/BSE exchange-group call
#: always reported a spurious "VIX coverage missing" clamp — a false,
#: operator-facing claim about data the arm never actually checked (it
#: checked a partition path that can never exist). The arm now runs ONLY
#: for an NSE group.
_INDIA_VIX_EXCHANGE: Final[str] = "NSE"

#: The base 1-minute lake interval every derived timeframe re-derives from.
SPOT_1M_INTERVAL: Final[str] = "1minute"

#: Segment whose spot index winners get FUTURES-translated in-chain (R14) —
#: triggers the futures-coverage preflight arm (ruling #6/P1-half).
_INDEX_SEGMENT: Final[str] = "INDEX"

#: Wildcard symbol/timeframe used for a whole-request (not per-symbol)
#: refusal (zero-session window, pull-in-progress, free-disk, SLA).
_WILDCARD: Final[str] = "*"


@dataclass(frozen=True, slots=True)
class Refusal:
    """One named preflight refusal — the ``refusals[]`` contract row."""

    symbol: str
    tf: str
    missing_spans: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict[str, object]:
        """Render the exact ``refusals[]`` contract row shape (JSON-safe)."""
        return {
            "symbol": self.symbol,
            "tf": self.tf,
            "missing_spans": list(self.missing_spans),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class Clamp:
    """One applied edge-case clamp or honest annotation — the ``clamps[]`` contract row."""

    field: str
    requested: str
    applied: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        """Render the exact ``clamps[]`` contract row shape (JSON-safe)."""
        return {
            "field": self.field,
            "requested": self.requested,
            "applied": self.applied,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class WindowClamp:
    """The clamped ``[start, end]`` window + every clamp applied to reach it."""

    start: date
    end: date
    clamps: tuple[Clamp, ...]


@dataclass(frozen=True, slots=True)
class OneClickPreflightVerdict:
    """The full R4/R20 preflight verdict — the exact integration-contract shape.

    Attributes
    ----------
    ok
        ``True`` iff every refusal-producing arm passed — the run may launch.
    refusals
        Every named refusal, in the DETERMINISTIC arm-execution order:
        window/zero-session, sweep-vs-pull lock, then per-symbol arms
        (1-minute coverage, lake invariants, derived-TF freshness, futures
        coverage) in request order, then free-disk-space, then the SLA
        guard LAST (a round-3 adversarial-refuter review found this
        docstring previously — wrongly — claimed disk ran first; corrected
        to match the actual arm order in :func:`run_oneclick_preflight`).
    clamps
        Every applied edge-case clamp or honest flag-and-run annotation
        (never itself a refusal source).
    estimator
        The R20 live trial/seconds/bytes estimate.
    """

    ok: bool
    refusals: tuple[Refusal, ...]
    clamps: tuple[Clamp, ...]
    estimator: EstimatorEstimate

    def to_dict(self) -> dict[str, object]:
        """Render the exact ``{ok, refusals, clamps, estimator}`` contract shape."""
        return {
            "ok": self.ok,
            "refusals": [r.to_dict() for r in self.refusals],
            "clamps": [c.to_dict() for c in self.clamps],
            "estimator": self.estimator.to_dict(),
        }


def clamp_sweep_window(start: date, end: date, *, today: date) -> WindowClamp:
    """Clamp ``[start, end]`` into the published holiday-coverage span ``[floor, ceiling]``.

    Clamps are applied BEFORE any calendar lookup so every date handed to
    :func:`~brutex.calendars.nse.trading_days_between_checked` sits inside
    the published holiday-coverage span — the walk never raises
    :class:`~brutex.calendars.nse.HolidayDataUnavailableError`.

    This is an interval-INTERSECTION, not two independent one-sided clamps
    (a hostile review caught the earlier one-sided version: clamping only
    ``start`` up to the floor and only ``end`` down to the ceiling left a
    request that lies ENTIRELY before the floor — e.g. ``start=2015-01-01,
    end=2016-01-01`` — with its unclamped, pre-floor ``end`` surviving
    untouched, which then crashed the calendar walk below with
    :class:`~brutex.calendars.nse.HolidayDataUnavailableError` instead of
    the honest clamp this function promises). The fix: compute the valid
    span ``[floor, ceiling]`` first; if the request doesn't overlap it at
    all, collapse the WHOLE window to the single nearest boundary (never a
    stray unclamped endpoint); otherwise clamp each side independently
    (the two clamps can never invert each other once the overlap check has
    passed).

    Parameters
    ----------
    start, end
        The requested window bounds. ``start <= end`` is normally already
        enforced upstream by :func:`brutex.dashboard.sweep_runner.resolve_sweep_request`'s
        ``end < start`` refusal at the request-validation boundary — but
        :func:`run_oneclick_preflight` (and this function) are reachable
        directly, bypassing that boundary, so the invariant is ALSO enforced
        HERE (an adversarial-refuter review found that without this check, a
        reversed ``(start, end)`` pair both sitting inside ``[floor,
        ceiling]`` silently returned unmodified with `start > end` and ZERO
        clamps recorded — a caller bug masquerading as a clean, unclamped
        result). Given the invariant holds, whichever side of ``[floor,
        ceiling]`` the whole window falls outside of, BOTH endpoints
        necessarily differ from the single collapse point (see below) —
        there is no partial-collapse case to guard against.
    today
        The injected "now" date (never read from the clock here — Golden
        Rule #4 reproducibility).

    Raises
    ------
    ValueError
        If ``end < start`` — a caller-contract violation, never a data
        problem this function can honestly clamp around.
    """
    if end < start:
        raise ValueError(f"end {end.isoformat()!r} must not precede start {start.isoformat()!r}")
    clamps: list[Clamp] = []
    floor = date(HOLIDAY_COVERAGE_FIRST_YEAR, 1, 1)
    # `today` is Golden-Rule-#4 injected and in real (non-test) use is always
    # the genuine current date — always >= floor, since floor predates the
    # earliest possible "now". A test injecting an unrealistic `today` before
    # `floor` must not be allowed to invert the valid span (`ceiling < floor`)
    # — clamp the ceiling to never fall below the floor.
    ceiling = max(floor, min(date(HOLIDAY_COVERAGE_LAST_YEAR, 12, 31), today))
    floor_reason = (
        f"pre-{HOLIDAY_COVERAGE_FIRST_YEAR} floor clamp — no published "
        f"NSE holiday-calendar coverage before {HOLIDAY_COVERAGE_FIRST_YEAR}"
    )
    ceiling_reason = (
        "future-to-date clamp — the window may not extend past today"
        if today < date(HOLIDAY_COVERAGE_LAST_YEAR, 12, 31)
        else (
            f"post-{HOLIDAY_COVERAGE_LAST_YEAR} coverage-ceiling clamp — no "
            f"published NSE holiday-calendar coverage after {HOLIDAY_COVERAGE_LAST_YEAR}"
        )
    )

    if end < floor or start > ceiling:
        # No overlap at all between [start, end] and [floor, ceiling] — the
        # WHOLE window collapses to the single nearest boundary point, never
        # a stray unclamped endpoint on the other side.
        collapse_to = floor if end < floor else ceiling
        collapse_reason = (
            f"requested window lies entirely before the coverage floor ({floor_reason})"
            if end < floor
            else f"requested window lies entirely after the clamped ceiling ({ceiling_reason})"
        )
        # Under the `start <= end` precondition (see docstring), the whole
        # window lying outside [floor, ceiling] means BOTH endpoints differ
        # from `collapse_to` — never just one — so both clamps always apply.
        clamps.append(
            Clamp(
                field="start",
                requested=start.isoformat(),
                applied=collapse_to.isoformat(),
                reason=f"{collapse_reason} — collapsed to a single-day window",
            )
        )
        clamps.append(
            Clamp(
                field="end",
                requested=end.isoformat(),
                applied=collapse_to.isoformat(),
                reason=f"{collapse_reason} — collapsed to a single-day window",
            )
        )
        return WindowClamp(start=collapse_to, end=collapse_to, clamps=tuple(clamps))

    applied_start = max(start, floor)
    if applied_start != start:
        clamps.append(
            Clamp(
                field="start",
                requested=start.isoformat(),
                applied=applied_start.isoformat(),
                reason=floor_reason,
            )
        )
    applied_end = min(end, ceiling)
    if applied_end != end:
        clamps.append(
            Clamp(
                field="end",
                requested=end.isoformat(),
                applied=applied_end.isoformat(),
                reason=ceiling_reason,
            )
        )

    return WindowClamp(start=applied_start, end=applied_end, clamps=tuple(clamps))


def missing_1m_partitions(
    lake_root: Path,
    *,
    exchange: str,
    segment: str,
    symbols: Sequence[str],
    required_months: Sequence[tuple[int, int]],
) -> dict[str, tuple[str, ...]]:
    """Return every symbol's missing 1-minute month-partitions, DISK-verified.

    For each *symbol*, checks ``Path.exists()`` (never the DuckDB Catalog
    ledger alone — R4's "DISK-verified not just ledger") on the canonical
    :func:`~brutex.data.partition.build_partition_path` for every
    ``(year, month)`` in *required_months*. O(symbols × months) — cheap
    filesystem stats, never a full bar read (that cost is paid once, later,
    by the sweep's own pre-load — this arm exists to refuse EAGERLY, before
    that expensive load even starts).

    Parameters
    ----------
    lake_root
        The lake root.
    exchange, segment
        The lake partition coordinates.
    symbols
        The requested symbols.
    required_months
        The distinct ``(year, month)`` pairs the trading calendar says the
        window needs (deduplicated, any order).

    Returns
    -------
    dict[str, tuple[str, ...]]
        ``{symbol: ("YYYY-MM", ...)}`` for every symbol with at least one
        missing month — a symbol with full coverage is absent from the dict.
    """
    missing: dict[str, tuple[str, ...]] = {}
    for symbol in symbols:
        gaps = tuple(
            f"{year:04d}-{month:02d}"
            for year, month in sorted(required_months)
            if not build_partition_path(
                lake_root=lake_root,
                exchange=exchange,
                segment=segment,
                symbol=symbol,
                interval=SPOT_1M_INTERVAL,
                year=year,
                month=month,
            ).exists()
        )
        if gaps:
            missing[symbol] = gaps
    return missing


def run_oneclick_preflight(
    *,
    lake_root: Path,
    results_root: Path,
    exchange: str,
    segment: str,
    symbols: Sequence[str],
    intervals: Sequence[str],
    start: date,
    end: date,
    today: date,
    k_values: Sequence[int],
    other_axes_product: int,
    tuple_count: int,
    top: int,
    allow_missing_slices: bool,
    acknowledge_sla_overrun: bool,
    check_vix_coverage: bool,
    free_bytes: int | None = None,
    disk_decision: DiskGuardDecision | None = None,
) -> OneClickPreflightVerdict:
    """Compose the full one-click institutional preflight verdict.

    Never pulls, never mutates the lake — every arm is a read-only disk
    check, a non-blocking lock probe, or pure arithmetic.

    Parameters
    ----------
    lake_root
        The lake root (parent of ``bars/``).
    results_root
        The sweep results root (``<lake>/sweeps`` —
        :func:`brutex.dashboard.sweep_runner.default_results_root`); the
        estimator calibration file lives at
        ``<results_root>/estimator_calibration.json``.
    exchange, segment
        The lake partition coordinates for *symbols* (already scope-checked
        upstream by :func:`brutex.dashboard.sweep_runner.resolve_sweep_request`
        — this composer does not re-run :func:`~brutex.engine.sweep_scope.check_sweep_scope`).
    symbols, intervals
        The requested axes.
    start, end
        The requested window (pre-clamp).
    today
        Injected "now" (Golden Rule #4).
    k_values, other_axes_product, tuple_count, top
        Feed :func:`~brutex.engine.sweep_estimator.estimate_sweep_cost`.
    allow_missing_slices
        The institutional default is ``False`` (R5) — when ``False``, ANY
        named 1m-coverage or freshness gap refuses the WHOLE request
        (ruling O-a, refuse-whole); when ``True`` (a deliberate research
        override upstream), the SAME gaps are folded into ``clamps`` as
        honest partial-coverage notes instead of refusals.
    acknowledge_sla_overrun
        Threaded into :func:`~brutex.engine.sweep_sla_guard.check_sweep_sla`
        — an acknowledged overrun is a :class:`Clamp` note, never a refusal
        (ruling #2: ACKNOWLEDGE-AND-PROCEED, never a silent hard block).
    check_vix_coverage
        Whether to run the India-VIX coverage arm (ruling #7) — the caller
        decides based on whether VIX stamping is in scope for this launch.
        The arm additionally only ever runs for an NSE *exchange* group
        (India VIX is an NSE-only index; a BSE/SENSEX group has no such
        partition to check and would otherwise get a false "missing" clamp).
    free_bytes
        Injectable override for the free-disk-space check (tests); ``None``
        reads the real ``shutil.disk_usage`` via
        :func:`~brutex.data.lake_footprint.evaluate_disk_guard`. Ignored when
        *disk_decision* is given.
    disk_decision
        A pre-computed :class:`~brutex.data.lake_footprint.DiskGuardDecision`
        — when given, skips calling :func:`~brutex.data.lake_footprint.evaluate_disk_guard`
        internally. **Cost honesty (Golden Rule #10):** that function's
        :func:`~brutex.data.lake_footprint.measure_lake_footprint` call is
        O(files in the WHOLE lake) — an unscoped ``rglob("*.parquet")`` under
        ``bars/``, NOT O(1) and NOT scoped to *symbols*/*intervals* — so a
        multi-exchange-group caller (:func:`brutex.dashboard.sweep_runner.oneclick_preflight_for_plan`)
        MUST compute this ONCE per logical plan and pass it here, never
        per-group, or a single one-click launch pays the full-lake scan N
        times over. ``None`` (the single-call/test default) computes it
        fresh, once, for this call only.

    Raises
    ------
    ValueError
        If ``end < start`` — propagated from :func:`clamp_sweep_window`,
        a caller-contract violation, never a data problem this preflight
        can honestly clamp/refuse around.
    """
    refusals: list[Refusal] = []
    clamps: list[Clamp] = []
    # De-duplicate, order-preserving (a hostile review found a repeated
    # symbol, e.g. "NIFTY,NIFTY", made the lake-invariants arm below append
    # the SAME finding twice and re-paid its O(files) scan twice — the
    # estimator already dedupes its own axis counts via
    # brutex.dashboard.sweep_runner._dedup_axis; every per-symbol arm here
    # must see the same de-duplicated set).
    symbols = tuple(dict.fromkeys(symbols))

    # ── 1. Window clamps + all-holiday zero-session refusal ──────────────
    window = clamp_sweep_window(start, end, today=today)
    clamps.extend(window.clamps)
    sessions = trading_days_between_checked(window.start, window.end, exchange=exchange)
    if not sessions:
        refusals.append(
            Refusal(
                symbol=_WILDCARD,
                tf=_WILDCARD,
                missing_spans=(f"{window.start.isoformat()}..{window.end.isoformat()}",),
                reason="zero tradable NSE sessions in the window after clamping (all-holiday/weekend span)",
            )
        )

    required_months = sorted({(d.year, d.month) for d in sessions})

    # ── 2. Sweep-vs-pull exclusion lock (read-only probe; P1 takes no lock) ──
    if not probe_pull_singleflight(lake_root):
        refusals.append(
            Refusal(
                symbol=_WILDCARD,
                tf=_WILDCARD,
                missing_spans=(),
                reason="a pull is already running for this lake — sweep and pull never run concurrently",
            )
        )

    # ── 3. 1-minute coverage vs the REAL NSE calendar, DISK-verified ──────
    if required_months:
        gaps = missing_1m_partitions(
            lake_root,
            exchange=exchange,
            segment=segment,
            symbols=symbols,
            required_months=required_months,
        )
        for symbol, months in gaps.items():
            entry = Refusal(
                symbol=symbol,
                tf=SPOT_1M_INTERVAL,
                missing_spans=months,
                reason="1-minute coverage missing for these months (DISK-verified) — pull on /universe first",
            )
            if allow_missing_slices:
                clamps.append(
                    Clamp(
                        field=f"1m_coverage[{symbol}]",
                        requested=",".join(months),
                        applied="skipped (allow_missing_slices)",
                        reason=entry.reason,
                    )
                )
            else:
                refusals.append(entry)

        # ── 4. Lake invariants — no quarantined/corrupt partitions in range ──
        derived_intervals = [iv for iv in intervals if iv != SPOT_1M_INTERVAL]
        for symbol in symbols:
            scan = scan_lake_invariants(
                lake_root,
                exchange=exchange,
                segment=segment,
                symbol=symbol,
                months=required_months,
                include_options=False,
            )
            for finding in scan.findings:
                refusals.append(
                    Refusal(
                        symbol=symbol,
                        tf=_WILDCARD,
                        missing_spans=(),
                        reason=(
                            f"lake invariant finding ({type(finding).__name__}) — "
                            "DISK-verified corrupt/duplicate/drifted partition, never ledger-trusted"
                        ),
                    )
                )

        # ── 5. Derived-TF freshness vs 1m (reuse the G1 refusal gate) ─────
        if derived_intervals:
            freshness = preflight_derived_freshness(
                lake_derived_freshness_verdicts(
                    lake_root,
                    exchange=exchange,
                    segment=segment,
                    symbols=symbols,
                    intervals=derived_intervals,
                )
            )
            for symbol, interval in freshness.stale:
                entry = Refusal(
                    symbol=symbol,
                    tf=interval,
                    missing_spans=(),
                    reason="stale derived timeframe — drifted from a fresh 1-minute re-derivation (G1)",
                )
                if allow_missing_slices:
                    clamps.append(
                        Clamp(
                            field=f"freshness[{symbol}/{interval}]",
                            requested="fresh",
                            applied="stale (allow_missing_slices)",
                            reason=entry.reason,
                        )
                    )
                else:
                    refusals.append(entry)

        # ── 6. Futures-contract coverage for the execution-translation leg ──
        if segment.upper() == _INDEX_SEGMENT:
            resolver = RegistryFrontMonthResolver(lake_root)
            futures_report = preflight_futures_months(
                lake_root,
                resolver=resolver,
                trade_months=[
                    (symbol, year, month) for symbol in symbols for year, month in required_months
                ],
            )
            if not futures_report.ok:
                refusals.append(
                    Refusal(
                        symbol=_WILDCARD,
                        tf=_WILDCARD,
                        missing_spans=tuple(
                            f"{u} {y:04d}-{m:02d}" for u, y, m in futures_report.missing_months
                        ),
                        reason=futures_report.remediation(),
                    )
                )

        # ── 7. India-VIX coverage arm (gap-hunt ruling #7) ────────────────
        # NSE-only (see _INDIA_VIX_EXCHANGE) — a BSE/SENSEX group has no VIX
        # partition to ever find, so the arm is skipped there, not falsely
        # reported as missing.
        if check_vix_coverage and exchange == _INDIA_VIX_EXCHANGE:
            vix_gaps = missing_1m_partitions(
                lake_root,
                exchange=exchange,
                segment=segment,
                symbols=(INDIA_VIX_SYMBOL,),
                required_months=required_months,
            )
            if INDIA_VIX_SYMBOL in vix_gaps:
                clamps.append(
                    Clamp(
                        field="vix_coverage",
                        requested="full",
                        applied="partial — vix_at_entry/exit will be honest NULL for affected months",
                        reason=(
                            f"India VIX 1-minute coverage missing for months: "
                            f"{', '.join(vix_gaps[INDIA_VIX_SYMBOL])} (REFERENCE_ONLY, never blocks the run)"
                        ),
                    )
                )

    # ── 8. Free-disk-space preflight (gap-hunt ruling #22) ────────────────
    disk = (
        disk_decision
        if disk_decision is not None
        else evaluate_disk_guard(lake_root, observed_multiplier=None, free_bytes=free_bytes)
    )
    if disk.blocked:
        refusals.append(
            Refusal(symbol=_WILDCARD, tf=_WILDCARD, missing_spans=(), reason=disk.detail)
        )

    # ── 9. Estimator (R20) + SLA acknowledge-and-proceed (ruling #2) ──────
    calibration = load_estimator_calibration(results_root)
    estimator = estimate_sweep_cost(
        k_values=k_values,
        other_axes_product=other_axes_product,
        tuple_count=tuple_count,
        top=top,
        segment=segment,
        calibration=calibration,
    )
    if estimator.effective_condition_count != estimator.nominal_condition_count:
        clamps.append(
            Clamp(
                field="condition_count",
                requested=str(estimator.nominal_condition_count),
                applied=f"{estimator.effective_condition_count} effective (flag-and-run)",
                reason=(
                    "VWAP-position bits are structurally dead on zero-volume INDEX spot "
                    "data — swept as-is (exclude-vs-flag is an open operator decision, "
                    "gap-hunt ruling #8); the run proceeds over the full nominal vocabulary"
                ),
            )
        )

    sla = check_sweep_sla(tuple_count, acknowledge=acknowledge_sla_overrun)
    if sla.decision is SlaDecision.REFUSED:
        refusals.append(
            Refusal(symbol=_WILDCARD, tf=_WILDCARD, missing_spans=(), reason=sla.refusal_message())
        )
    elif sla.decision is SlaDecision.ACKNOWLEDGED:
        clamps.append(
            Clamp(
                field="sla_overrun",
                requested="within envelope",
                applied="acknowledged overrun — proceeding",
                reason=sla.acknowledged_warning(),
            )
        )

    return OneClickPreflightVerdict(
        ok=not refusals,
        refusals=tuple(refusals),
        clamps=tuple(clamps),
        estimator=estimator,
    )
