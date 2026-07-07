"""Derive **1-day** bars from stored **1-minute** (DEC-1MINUTE-ONLY-DERIVE-1DAY-001).

Why this module exists
----------------------
DEC-1MINUTE-ONLY-DERIVE-1DAY-001 (operator decree) makes **1-minute the ONLY
granularity fetched from Groww** (spot, futures, options); ``1day`` and every other
timeframe are DERIVED from the stored 1-minute, never vendor-fetched. This module is
the 1-minute → 1-day derivation: it resamples the lake's 1-minute to midnight-IST
daily bars and writes the ``1day`` partition the engine reads (the daily anchor for
PDH/PDL). The derivation runs **always-on** after every spot / F&O 1-minute pull —
:func:`derive_daily_for_targets` over the FULL pulled date range per symbol — so the
``1day`` partition is an openly-derived bar, not a separate vendor pull.

This module supersedes the earlier resample-on-EMPTY feature
(DEC-SPOT-1DAY-RESAMPLE-ON-EMPTY-001 / DEC-REALDATA-ONLY-NO-1M-TO-1D-001 #852, which
removed it): the ledger-empty-window arm (:func:`reconcile_daily_from_minute` /
:func:`plan_daily_resample_windows`) is RETAINED for the targeted gap-fill case, but
the PRIMARY path is now the always-on full-range derivation — there is no vendor 1-day
to be empty in the first place, so the daily bar is derived unconditionally.

The midnight-IST stamping rule (the linchpin — DO NOT change without re-verifying)
----------------------------------------------------------------------------------
A vendor-fetched ``1day`` bar is stamped at **midnight IST** (``00:00`` IST =
``18:30`` UTC of the PREVIOUS calendar day), verified by
:func:`brutex.adapters.groww.contract_bounds.contract_daily_coverage` ("under
midnight-IST stamping a Jan-1-IST bar lives in the *December* UTC partition") and the
``test_daily_coverage_cross_utc_month_midnight_ist_golden`` /
``test_load_prev_day_ist_date_differs_from_utc_date`` golden tests. The shared
:func:`~brutex.data.tf_resampler.resample_1m_to_tf` labels its daily bucket at the
**09:15 IST** session open (``03:45`` UTC) instead — perfect for the brute-force
grid, WRONG for the lake's daily convention. So this module reuses that resampler
ONLY for the OHLCV aggregation (open=first / high=max / low=min / close=last /
volume=sum — CLAUDE.md §11 COMMON, never re-implemented) and then **re-stamps**
every derived bar to midnight IST using the SAME fixed +05:30 offset the Groww
parser uses (:data:`brutex.adapters.groww.parser`), so a resampled daily bar's UTC
instant is byte-identical IN STAMP/SCHEMA/OI to a vendor-parsed ``00:00:00 IST``
bar — the **timestamp** identity that keeps the daily series uniform. The OHLC
**values** are a 1-minute aggregation (first-minute open / summed volume /
last-minute close). Because 1-day is now ALWAYS derived from 1-minute (there is NO
vendor daily — DEC-1MINUTE-ONLY-DERIVE-1DAY-001), the always-on derive writes the
derived ``1day`` partition with ``merge_mode="latest-wins"`` (FIX-2): a
re-derivation of a date OVERWRITES the prior derived bar, so a later COMPLETE-session
pull CORRECTS a daily an earlier PARTIAL-session pull wrote (mid-session token death /
narrow ``to_date`` / a transient gap). A re-run over the SAME complete 1-minute is
still idempotent (identical OHLCV is a silent no-op). This latest-wins restate is
scoped ONLY to the derived ``1day`` partition; the authoritative 1-minute partition
keeps its keep-first idempotence unchanged.

Honest envelope (Golden Rule #1 / #10)
--------------------------------------
* **Never invents data.** When the lake holds NO 1-minute for the window, the fill
  is a no-op (``bars_written == 0``) — the daily window keeps whatever the ledger
  recorded (EMPTY → re-fetched next pull, or SKIPPED). The default verify-first
  ``1day``-only pull therefore self-heals nothing unless 1-minute was pulled too;
  that is surfaced LOUDLY by the caller, never papered over.
* **No partial/forming bar.** A session that is not yet complete (its IST date is
  on/after the ``before`` cutoff) is DROPPED, so a still-forming session is never
  written as a final daily bar (no look-ahead). The production ``before`` is the
  session-close-aware :func:`brutex.calendars.nse.daily_derive_cutoff` — today
  IST while the run day's session is forming, tomorrow once it provably closed
  or on a non-trading day (DEC-PREBUILD-SESSION-CLOSE-CUTOFF-001).
* **Spot OI is null.** Resampled daily bars carry ``open_interest=None`` — exactly
  what the Groww parser stores for a cash-equity/index daily bar (OI is FNO-only).

Pure core, injected I/O (CLAUDE.md §4 / §11)
--------------------------------------------
:func:`resample_1m_to_daily` and :func:`fill_daily_from_minute` are pure; the lake
read + write are injected callables, so the whole control flow is 100%
unit-testable with fakes. :func:`reconcile_daily_from_minute` drives the per-window
fill over the unified :class:`~brutex.data.window_ledger.WindowLedger`, recording a
filled window ``SUCCESS`` with ``in_pull_fetches=0`` (the honest "no network fetch"
provenance — a resample made zero vendor calls). The raw lake builders
(:func:`make_lake_read_1m_window` / :func:`make_lake_write_1day`) are
``# pragma: no cover`` — integration-tested, not line-gated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Final, Literal

import structlog

from brutex.adapters.groww.constants import TRACK1_DAILY_INTERVAL
from brutex.calendars.nse import SESSION_OPEN_IST, has_muhurat_session
from brutex.data.fetch_coverage import WindowKey
from brutex.data.partition import MONTHS_PER_YEAR
from brutex.data.timeframes import DAILY_TF_MINUTES
from brutex.data.window_ledger import EMPTY, SKIPPED, WindowLedger, WindowLedgerRecord

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence
    from pathlib import Path

    import polars as pl

    from brutex.data.models import CandleBar

#: Fixed +05:30 IST — the SAME offset :mod:`brutex.adapters.groww.parser` uses to
#: localize a vendor ``00:00:00 IST`` daily timestamp, so the re-stamped UTC instant
#: of a resampled daily bar is byte-identical to a vendor-fetched one (the dedup
#: identity). India has no DST, so a fixed offset is exact for the 2020+ scope.
_IST = timezone(timedelta(hours=5, minutes=30))

#: Structured logger — every degrade (no 1-minute to fill from / a quarantined
#: window) is logged, so a resample-fill pass is visible in /logs (Golden Rule #10).
_LOG = structlog.get_logger("brutex.data.spot_daily_resample")

__all__ = [
    "MAX_OPEN_SUBSTITUTION_WARNS",
    "DailyDeriveReport",
    "DailyDeriveTarget",
    "DailyFillResult",
    "DailyOpenSubstitutionScan",
    "DailyOpenVerdict",
    "DailyReconcileReport",
    "DailyResampleWindow",
    "classify_daily_open",
    "derive_daily_for_targets",
    "detect_daily_open_substitutions",
    "emit_daily_open_substitutions",
    "fill_daily_from_minute",
    "frame_to_candle_bars",
    "ist_span_partition_files",
    "ist_span_partition_months",
    "make_lake_read_1m_window",
    "make_lake_write_1day",
    "observe_daily_open_substitutions",
    "plan_daily_resample_windows",
    "reconcile_daily_from_minute",
    "resample_1m_to_daily",
]

#: Cap on per-(symbol, date) ``daily_open.substituted`` WARN lines per symbol
#: per scan (DEC-CALENDAR-EXPECTED-BARS-DAILY-OPEN-001; precedent: the #919
#: silent-vanish logger's cap of 20). The aggregate event ALWAYS carries the
#: TRUE count, so the cap never lies (Golden Rule #1).
MAX_OPEN_SUBSTITUTION_WARNS: Final[int] = 20

#: One session's verdict under the ALIGNED daily-open predicate
#: (DEC-DAILY-OPEN-PREDICATE-ALIGN-001): ``"substituted"`` — the derived 1-day
#: open is the earliest-present-minute substitute, not the canonical 09:15
#: price; ``"muhurat_abstained"`` — a Muhurat session, whose open is
#: off-09:15 BY DESIGN (never a substitution signal); ``"ok"`` — the first
#: present minute IS exactly 09:15, so the derived open is the canonical one.
DailyOpenVerdict = Literal["ok", "substituted", "muhurat_abstained"]


def classify_daily_open(ist_date: date, first_minute: time) -> DailyOpenVerdict:
    """The ONE COMMON per-session daily-open-substitution predicate — pure.

    DEC-DAILY-OPEN-PREDICATE-ALIGN-001: BOTH open-substitution surfaces — the
    prebuild/pipeline headline (:func:`detect_daily_open_substitutions`, which
    iterates this classifier) AND the /completeness · /universe · /coverage
    census (the DuckDB per-session CTE in
    :func:`brutex.dashboard.db_view.summarize_spot_datasets`, whose SQL
    predicate ``first_ist <> 09:15`` + Muhurat ``NOT IN`` exclusion is the
    mirror-pinned twin of this function — equivalence proven same-lake
    same-verdicts by ``test_sql_cte_equivalent_to_classifier_over_one_lake``)
    — key off this ONE policy (CLAUDE.md §11 COMMON):

    * a **Muhurat** session (:func:`brutex.calendars.nse.has_muhurat_session`)
      ABSTAINS — its open is off-09:15 BY DESIGN (evening 18:00/18:15 for
      2020-2024, afternoon 13:45 for 2025), so counting it would flag a
      perfectly healthy lake every year (the operator-mandated false-positive
      suppression, previously the detector-only rule);
    * any other session whose first present minute is NOT exactly
      :data:`~brutex.calendars.nse.SESSION_OPEN_IST` (09:15:00) is
      **substituted** — a stray PRE-09:15 first bar (should not survive the
      ingest session filter) COUNTS too (``!=``, never silently trusted; this
      closes the census's former strict-``>`` under-count);
    * exactly 09:15:00 is **ok** — the derived daily open IS the canonical
      open price.

    The classifier judges ONE (session, first-minute) pair; it deliberately
    carries NO population rule — the forming-session ``ist_date < before``
    cutoff (detector) vs all-stored-sessions (census) split is a documented
    POPULATION difference, not a predicate difference (see
    :func:`detect_daily_open_substitutions`).
    """
    if has_muhurat_session(ist_date):
        return "muhurat_abstained"
    if first_minute != SESSION_OPEN_IST:
        return "substituted"
    return "ok"


@dataclass(frozen=True, slots=True)
class DailyOpenSubstitutionScan:
    """Result of one :func:`detect_daily_open_substitutions` pass — pure data.

    Attributes
    ----------
    substituted
        ``((ist_date, first_minute_ist), ...)`` ascending by date — the
        COMPLETE sessions (``ist_date < before``) whose earliest stored
        1-minute bar is NOT exactly 09:15:00 IST
        (:data:`~brutex.calendars.nse.SESSION_OPEN_IST`). For each, the derived
        daily ``open`` silently substituted that first-present minute's open
        (the implicit ``open.first()`` in
        :func:`~brutex.data.tf_resampler.resample_1m_to_tf` — no coded branch).
        The predicate is the ONE COMMON :func:`classify_daily_open`
        (DEC-DAILY-OPEN-PREDICATE-ALIGN-001) — ``!= 09:15`` exactly: a stray
        PRE-09:15 bar (should not survive the ingest session filter) also
        flags, with the actual first minute in the payload — never silently
        trusted. Muhurat sessions are EXCLUDED (see ``muhurat_abstained``).
    sessions
        COMPLETE sessions examined (post-``before``-cutoff, INCLUDING the
        Muhurat-abstained ones).
    muhurat_abstained
        Sessions skipped because the date is in
        :data:`~brutex.calendars.nse.MUHURAT_SESSIONS` — a Muhurat session's
        open is BY DESIGN not 09:15 (evening 18:00/18:15 for 2020-2024,
        afternoon 13:45 for 2025), so flagging it would be a false positive.
        Coverage-bounded honesty (Golden Rule #1, review-corrected): the 2026
        Muhurat date's timings are NOT yet notified and deliberately absent
        from the calendar — but a MISSING entry can never reach this flag on
        a production pull: with no calendar session,
        :func:`brutex.calendars.nse.get_session` returns ``None`` and the
        calendar-aware ingest filter (DEC-INGEST-BOUNDARY-INTEGRITY-001,
        MIGRATION.md #1294) DROPS every bar of that date at the ingest
        boundary (an ALL_FILTERED re-pullable demotion), so the session never
        enters the lake and this detector never sees it; once the official
        NSE circular lands and the entry is added, the abstention covers it —
        the 2026 session flags in NEITHER state. A flag is possible ONLY for
        bars that bypassed the ingest filter (e.g. hand-placed parquet) —
        never fabricated, never asserted as a timing.
    """

    substituted: tuple[tuple[date, time], ...]
    sessions: int
    muhurat_abstained: int

    @property
    def count(self) -> int:
        """Number of substituted sessions (the TRUE total, never capped)."""
        return len(self.substituted)


def detect_daily_open_substitutions(
    df_1m: pl.DataFrame, *, before: date
) -> DailyOpenSubstitutionScan:
    """Scan a 1-minute frame for sessions whose derived daily open is substituted.

    Pure + read-only (DEC-CALENDAR-EXPECTED-BARS-DAILY-OPEN-001): one O(sessions)
    group-by over the frame ALREADY in memory — zero extra lake I/O, and the
    frame is never mutated (stored bars stay byte-unchanged; golden-pinned).
    Applies the SAME forming-session cutoff :func:`resample_1m_to_daily` uses
    (``ist_date < before``), so the scan flags ONLY sessions that were actually
    written as final daily bars. Order-independent: the per-session first
    minute is ``min(ts)``, never a positional first row. IST is the fixed
    +05:30 offset (:data:`_IST` — the same arithmetic the resampler uses,
    no tz-library drift between scan and write).

    ALIGNED with the read-side /completeness counter
    (DEC-DAILY-OPEN-PREDICATE-ALIGN-001 — the named follow-up decision the
    former three-way divergence of DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001
    #1267 deferred to): the per-session CTE in
    :func:`brutex.dashboard.db_view.summarize_spot_datasets` (surfaced as the
    /completeness "Open substituted N/M" column + the /universe strip) and
    this detector now share the ONE per-session predicate
    :func:`classify_daily_open` — Muhurat ABSTAINS on BOTH surfaces (a
    Muhurat open is off-09:15 BY DESIGN; counting it flagged a healthy lake
    every year), and the predicate is ``!= 09:15`` on BOTH (a stray pre-09:15
    first bar COUNTS, closing the census's former strict-``>`` under-count).
    This detector iterates the classifier directly; the CTE's SQL
    (``first_ist <> …`` + the :func:`~brutex.dashboard.db_view.
    _muhurat_exclusion_sql` date exclusion) is its mirror-pinned twin —
    equivalence proven same-lake same-verdicts by
    ``test_sql_cte_equivalent_to_classifier_over_one_lake`` and the alignment
    pinned by ``test_predicate_alignment_pin_vs_honesty_counter_1267``. The
    ONE remaining deliberate difference is POPULATION, not predicate: the
    census counts ALL stored sessions of the scored INDEX/CASH ``1minute``
    datasets (no cutoff), while this detector applies the forming-session
    ``ist_date < before`` cutoff over every SPOT prebuild target — so the two
    surfaces can differ ONLY on a not-yet-derived forming session (or a
    dataset outside the prebuild target set), NEVER on a session both
    examined (pinned by ``test_population_cutoff_divergence_pin``).

    Parameters
    ----------
    df_1m
        The symbol's stored 1-minute bars (the resampler's ``ts_utc`` column
        is the only one read; naive timestamps are treated as UTC — the lake
        convention). An empty frame yields an all-zero scan.
    before
        The forming-session cutoff (today IST) — sessions on/after it are
        neither flagged nor counted (they were not written as daily bars).
    """
    # Local import keeps polars out of this module's import graph for callers
    # that only need the dataclasses (mirrors resample_1m_to_daily).
    import polars as pl

    if df_1m.is_empty():
        return DailyOpenSubstitutionScan(substituted=(), sessions=0, muhurat_abstained=0)
    ist = pl.col("ts_utc") + pl.duration(hours=5, minutes=30)
    firsts = (
        df_1m.select(ist.alias("_ist"))
        .with_columns(pl.col("_ist").dt.date().alias("_ist_date"))
        .filter(pl.col("_ist_date") < before)
        .group_by("_ist_date")
        .agg(pl.col("_ist").min().alias("_first_ist"))
        .sort("_ist_date")
    )
    substituted: list[tuple[date, time]] = []
    muhurat_abstained = 0
    for row in firsts.iter_rows(named=True):
        ist_date: date = row["_ist_date"]
        first_minute: time = row["_first_ist"].time()
        # The ONE COMMON per-session predicate (DEC-DAILY-OPEN-PREDICATE-
        # ALIGN-001) — the same policy the /completeness SQL CTE mirrors.
        verdict = classify_daily_open(ist_date, first_minute)
        if verdict == "muhurat_abstained":
            # A Muhurat session opens off-09:15 BY DESIGN — abstain, counted.
            muhurat_abstained += 1
        elif verdict == "substituted":
            substituted.append((ist_date, first_minute))
    return DailyOpenSubstitutionScan(
        substituted=tuple(substituted),
        sessions=firsts.height,
        muhurat_abstained=muhurat_abstained,
    )


def emit_daily_open_substitutions(
    scan: DailyOpenSubstitutionScan, *, symbol: str, dense: bool
) -> None:
    """Emit the observe-only substitution log events for one symbol's scan.

    Log taxonomy (DEC-CALENDAR-EXPECTED-BARS-DAILY-OPEN-001):

    * ``daily_open.substituted`` — WARN per (symbol, date), ONLY for a DENSE
      (index) symbol, capped at :data:`MAX_OPEN_SUBSTITUTION_WARNS` per scan.
    * ``daily_open.substituted_aggregate`` — once per symbol when
      ``scan.count > 0``: WARN for dense, INFO (count-only) for illiquid —
      the operator-mandated aggregate path; ALWAYS carries the TRUE count.

    A zero-substitution scan emits NOTHING — the log surface of a healthy lake
    is byte-identical to before this feature existed. Pure emitter (may raise
    on a poisoned scan) — production wires it through the guarded
    :func:`observe_daily_open_substitutions`, mirroring the
    ``emit_underlying_all_empty`` precedent. The counts emitted here follow
    the ONE ALIGNED per-session predicate :func:`classify_daily_open`
    (DEC-DAILY-OPEN-PREDICATE-ALIGN-001) — the SAME rule the /completeness
    counter's SQL CTE mirrors; only the POPULATION (forming-session cutoff
    here, all stored sessions there) can differ — see
    :func:`detect_daily_open_substitutions`.
    """
    if scan.count == 0:
        return
    if dense:
        for session_date, first_minute in scan.substituted[:MAX_OPEN_SUBSTITUTION_WARNS]:
            _LOG.warning(
                "daily_open.substituted",
                symbol=symbol,
                session_date=session_date.isoformat(),
                first_minute_ist=first_minute.strftime("%H:%M"),
            )
    log = _LOG.warning if dense else _LOG.info
    log(
        "daily_open.substituted_aggregate",
        symbol=symbol,
        dense=dense,
        count=scan.count,
        sessions=scan.sessions,
        muhurat_abstained=scan.muhurat_abstained,
        warns_capped=dense and scan.count > MAX_OPEN_SUBSTITUTION_WARNS,
    )


def observe_daily_open_substitutions(
    df_1m: pl.DataFrame, *, before: date, symbol: str, dense: bool
) -> int:
    """Detect + emit in ONE guarded call — the production entry point.

    OBSERVE-ONLY by construction (DEC-CALENDAR-EXPECTED-BARS-DAILY-OPEN-001): the
    WHOLE detect+emit pass runs inside one ``try/except``, so an unexpected
    detector/emitter error (a dtype quirk, a poisoned frame) can NEVER fail
    the prebuild that hosts it — it logs ``daily_open.substitution_log_error``
    and returns ``0`` (an honest "not observed", never a fabricated count).
    Mirrors :func:`brutex.adapters.groww.option_ingest.emit_underlying_all_empty`'s
    scan-AND-log guard discipline. Returns the substituted-session count for
    :attr:`~brutex.data.timeframe_materialize.MaterializeResult.opens_substituted`.
    """
    try:
        scan = detect_daily_open_substitutions(df_1m, before=before)
        emit_daily_open_substitutions(scan, symbol=symbol, dense=dense)
    except Exception as exc:  # pylint: disable=broad-except - observe-only, must never break the prebuild
        _LOG.warning(
            "daily_open.substitution_log_error",
            symbol=symbol,
            error=repr(exc),
        )
        return 0
    return scan.count


@dataclass(frozen=True, slots=True)
class DailyFillResult:
    """Outcome of one :func:`fill_daily_from_minute` call.

    ``bars_written`` is whatever the injected ``write_1day`` returned (the sink's
    merge-on-write appended count — ``0`` when the 1-minute slice was empty OR every
    resampled bar already existed in the lake). ``first_ts`` / ``last_ts`` bound the
    bars that were resampled (``None`` when none), for the structured-log trail.
    """

    bars_written: int
    first_ts: datetime | None
    last_ts: datetime | None


@dataclass(frozen=True, slots=True)
class DailyResampleWindow:
    """One daily window the ledger marked empty that resample may be able to fill.

    Carries the exact :class:`~brutex.data.fetch_coverage.WindowKey` identity
    (``market`` / ``security_id`` / ``interval`` / ``[start, end)`` dates) so a
    successful fill re-records the SAME ledger row ``SUCCESS`` (never a duplicate).
    """

    market: str
    security_id: str
    interval: str
    start: date
    end: date


@dataclass(frozen=True, slots=True)
class DailyReconcileReport:
    """Aggregate outcome of a :func:`reconcile_daily_from_minute` pass.

    Attributes
    ----------
    filled
        Windows whose daily bars were derived from 1-minute and recorded ``SUCCESS``.
    no_minute
        Windows skipped because the lake held no 1-minute for them (honest no-op —
        the verify-first ``1day``-only default surfaces here when 1-minute was never
        pulled). The window keeps its prior EMPTY/SKIPPED status.
    errored
        Windows quarantined by a per-window error (a corrupt 1-minute slice / a write
        failure) — one bad window never blanks the pass (mirrors the ingest spine).
    bars
        Total daily bars written across every filled window.
    """

    filled: int
    no_minute: int
    errored: int
    bars: int

    @property
    def windows(self) -> int:
        """Total candidate windows considered (filled + no_minute + errored)."""
        return self.filled + self.no_minute + self.errored


@dataclass(frozen=True, slots=True)
class DailyDeriveTarget:
    """One symbol's ``[start, end)`` range to derive ``1day`` over (always-on path).

    Unlike :class:`DailyResampleWindow` (a ledger-EMPTY candidate keyed on a coverage
    market), this is the always-on full-range target (DEC-1MINUTE-ONLY-DERIVE-1DAY-001):
    the symbol's lake address (``exchange`` / ``segment`` / ``symbol``) plus the
    half-open ``[start, end)`` date range the pull just landed 1-minute over. The daily
    derivation reads that range's 1-minute and writes the resampled ``1day`` bars,
    unconditionally (there is no vendor 1-day to be empty).
    """

    exchange: str
    segment: str
    symbol: str
    start: date
    end: date


@dataclass(frozen=True, slots=True)
class DailyDeriveReport:
    """Aggregate outcome of a :func:`derive_daily_for_targets` always-on pass.

    Attributes
    ----------
    derived
        Targets whose ``1day`` bars were resampled + written (``bars_written > 0``).
    no_minute
        Targets whose range held no 1-minute in the lake (honest no-op — nothing to
        derive, never an invented bar).
    errored
        Targets quarantined by a per-target error (a corrupt 1-minute slice / a write
        failure) — one bad target never blanks the pass (mirrors the ingest spine).
    bars
        Total daily bars written across every derived target.
    """

    derived: int
    no_minute: int
    errored: int
    bars: int

    @property
    def targets(self) -> int:
        """Total targets considered (derived + no_minute + errored)."""
        return self.derived + self.no_minute + self.errored


def derive_daily_for_targets(
    *,
    targets: Iterable[DailyDeriveTarget],
    build_read_1m: Callable[[DailyDeriveTarget], Callable[[], pl.DataFrame]],
    build_write_1day: Callable[[DailyDeriveTarget], Callable[[Sequence[CandleBar]], int]],
    before: date,
    resample_fn: Callable[[pl.DataFrame, date], list[CandleBar]] | None = None,
) -> DailyDeriveReport:
    """Derive ``1day`` from 1-minute for every *target* — always-on, pure orchestration.

    The PRIMARY path of DEC-1MINUTE-ONLY-DERIVE-1DAY-001: for each target (a symbol +
    its just-pulled ``[start, end)`` range), bind the 1-minute reader + 1-day writer
    (the injected builders), resample-and-write (:func:`fill_daily_from_minute`), and
    tally the outcome. UNCONDITIONAL — unlike :func:`reconcile_daily_from_minute` it does
    not consult the ledger for EMPTY windows (there is no vendor 1-day to be empty); the
    daily bar is always derived from the 1-minute the pull just landed. Each target is
    quarantined: a per-target error (corrupt slice / write failure) increments
    ``errored`` and continues — one bad target never blanks the pass. All lake I/O is
    injected, so the whole flow is 100% unit-testable; the forming-session guard
    (*before*) drops today's incomplete session (no look-ahead).

    Parameters
    ----------
    targets
        The symbols + ranges to derive ``1day`` over (one per pulled instrument).
    build_read_1m
        ``(target) -> (() -> polars frame)`` — binds the target's 1-minute reader.
    build_write_1day
        ``(target) -> ((bars) -> bars_written)`` — binds the target's ``1day`` writer.
    before
        The forming-session cutoff (the caller-resolved IST date), forwarded to the resampler.
    resample_fn
        The 1-minute → daily resampler ``(frame, before) -> list[CandleBar]``.
        Defaults to the spot :func:`resample_1m_to_daily` (OI ``None``); the F&O
        derive injects :func:`brutex.data.fno_daily_resample.resample_1m_to_fno_daily`
        (carry-last OI) — the ONE knob that makes this driver serve both spot AND
        F&O without a second loop (DEC-FNO-TF-DERIVE-OI-001, CLAUDE.md §11 COMMON).

    Returns
    -------
    DailyDeriveReport
        The derived / no-minute / errored tallies + total bars written.
    """
    derived = no_minute = errored = bars = 0
    for target in targets:
        try:
            result = fill_daily_from_minute(
                read_1m=build_read_1m(target),
                write_1day=build_write_1day(target),
                before=before,
                resample_fn=resample_fn,
            )
        except Exception as exc:
            errored += 1
            _LOG.warning(
                "spot_daily_resample.target_errored",
                symbol=target.symbol,
                start=target.start.isoformat(),
                end=target.end.isoformat(),
                error=str(exc),
            )
            continue
        if result.bars_written > 0:
            derived += 1
            bars += result.bars_written
            _LOG.info(
                "spot_daily_resample.target_derived",
                symbol=target.symbol,
                start=target.start.isoformat(),
                end=target.end.isoformat(),
                bars=result.bars_written,
                first_ts=result.first_ts.isoformat() if result.first_ts is not None else None,
                last_ts=result.last_ts.isoformat() if result.last_ts is not None else None,
            )
        else:
            no_minute += 1
            _LOG.info(
                "spot_daily_resample.target_no_minute",
                symbol=target.symbol,
                start=target.start.isoformat(),
                end=target.end.isoformat(),
            )
    return DailyDeriveReport(derived=derived, no_minute=no_minute, errored=errored, bars=bars)


def frame_to_candle_bars(
    frame: pl.DataFrame,
    *,
    absent_open_interest: int | None = 0,
) -> list[CandleBar]:
    """Batched resampled-frame → :class:`~brutex.data.models.CandleBar` conversion.

    The COMMON (CLAUDE.md §11) replacement for the per-row
    ``frame.iter_rows(named=True)`` → ``CandleBar(Decimal(str(...)) × 4)`` loops
    the derived-TF writer and the spot daily resampler each carried
    (DEC-PREBUILD-GIL-VECTORIZE-001). Why: the pipeline prebuild runs INSIDE
    the dashboard server process, and that per-row conversion was the largest
    GIL-held pure-Python section of the prebuild (stack-sampled during the
    blank-``GET /`` starvation diagnosis at the two former loop sites —
    ``timeframe_materialize.py:516-527`` and ``spot_daily_resample.py:359-370``
    pre-fix), stealing event-loop time from uvicorn while polars saturated the
    cores. This batched form extracts each column ONCE (columnar, C-level)
    instead of building a Python dict per row, renders the four price columns
    to their shortest-round-trip decimal strings INSIDE polars
    (``cast(String)`` — GIL released; numerically ``Decimal``-equal to Python's
    ``str(float)`` per the DEC-LAKE-WRITE-VECTORIZE-001 byte-contract analysis
    in :mod:`brutex.data.parquet_writer`, which pinned the same seed for the
    write-side snap), and memoises the ``str → Decimal`` parse (2dp-snapped
    lake prices repeat heavily across resample buckets; ``Decimal`` is
    immutable, so sharing instances is value-safe).

    Lives HERE — the import-graph LEAF of the pair (:mod:`~brutex.data.
    timeframe_materialize` already imports this module for the daily-derive
    seam) — and is re-exported from :mod:`brutex.data.timeframe_materialize`
    for its original callers; it was moved from that module to break the
    ``spot_daily_resample ↔ timeframe_materialize`` import cycle the shared
    helper introduced (CodeQL ``py/cyclic-import``,
    DEC-PREBUILD-GIL-VECTORIZE-001 follow-through).

    BYTE-IDENTICAL lake output (Golden Rule #7): every constructed bar compares
    equal to the legacy per-row form — same instants, ``Decimal`` values
    numerically equal to ``Decimal(str(x))`` (polars may emit a different digit
    STRING for the same value, e.g. ``0.00001`` vs ``1e-05`` — numerically
    equal, and the sink's ``round_money`` 2dp snap keys off the numeric value
    only) — pinned by the golden byte-equality regression over seeded lakes.
    HONEST envelope (Golden Rule #10): this is a BATCHED construction, not a
    bar-object elimination — the irreducible per-bar ``CandleBar`` (validated,
    frozen) + Python ``datetime`` objects remain, so the measured win is the
    dict/str/Decimal overhead (~25-30 % of the conversion), NOT an
    order-of-magnitude cut; the sink write path is byte-unchanged. This
    decision ships ONLY the vectorization — the diagnosis's other lever, a
    ``POLARS_MAX_THREADS`` cap at the dashboard/pull entrypoints (set before
    the first polars import, never on the engine/sweep path, which pins its
    OWN per-worker cap in :mod:`brutex.engine.sweep_parallel`), is NOT
    implemented anywhere: ``brutex/cli.py`` is fenced by open PRs, so the cap
    is a REPORTED follow-up and polars core saturation during a prebuild
    remains (starvation REDUCED, not eliminated).

    Parameters
    ----------
    frame
        A resampled frame with columns ``ts_utc, open, high, low, close,
        volume`` and optionally ``open_interest``.
    absent_open_interest
        The ``open_interest`` used when the frame has NO OI column, and for a
        present-but-null OI value. ``0`` is the derived-intraday-TF/spot lake
        convention (the legacy ``_oi`` helper's contract); the spot daily
        resampler passes ``None`` (spot/index daily bars carry no OI).
    """
    # Local imports keep the module import graph free of polars + the bar model
    # for callers that only need the dataclasses (module discipline, §11).
    from decimal import Decimal

    import polars as pl

    from brutex.data.models import CandleBar

    if frame.height == 0:
        return []

    timestamps = frame.get_column("ts_utc").to_list()
    opens, highs, lows, closes = (
        frame.get_column(column).cast(pl.String).to_list()
        for column in ("open", "high", "low", "close")
    )
    volumes = frame.get_column("volume").to_list()
    open_interests: list[int | None]
    if "open_interest" in frame.columns:
        # Present-but-null OI maps to *absent_open_interest* (the legacy `_oi`
        # contract: the lake stored 0 for spot, so 0 is the consistent absent
        # value on the intraday path).
        open_interests = [
            absent_open_interest if value is None else int(value)
            for value in frame.get_column("open_interest").to_list()
        ]
    else:
        open_interests = [absent_open_interest] * frame.height

    decimal_memo: dict[str, Decimal] = {}

    def to_decimal(text: str) -> Decimal:
        value = decimal_memo.get(text)
        if value is None:
            value = decimal_memo[text] = Decimal(text)
        return value

    return [
        CandleBar(
            timestamp=ts,
            open=to_decimal(open_text),
            high=to_decimal(high_text),
            low=to_decimal(low_text),
            close=to_decimal(close_text),
            volume=int(volume),
            open_interest=open_interest,
        )
        for ts, open_text, high_text, low_text, close_text, volume, open_interest in zip(
            timestamps, opens, highs, lows, closes, volumes, open_interests, strict=True
        )
    ]


def resample_1m_to_daily(df_1m: pl.DataFrame, *, before: date) -> list[CandleBar]:
    """Resample a 1-minute :class:`polars.DataFrame` to **midnight-IST** daily bars.

    Reuses :func:`~brutex.data.tf_resampler.resample_1m_to_tf` (TF
    :data:`~brutex.data.timeframes.DAILY_TF_MINUTES`) for the OHLCV aggregation —
    the single source of truth for the session bucketing — then re-stamps each
    derived bar from the resampler's 09:15-IST label to **midnight IST** (the vendor
    daily convention; see the module docstring) and converts the polars row to a
    :class:`~brutex.data.models.CandleBar` with ``open_interest=None`` (spot/index
    daily bars carry no OI).

    Parameters
    ----------
    df_1m
        The symbol's stored 1-minute bars as a polars frame with the resampler's
        required columns (``ts_utc, open, high, low, close, volume``), sorted
        ascending by ``ts_utc``. An empty frame yields an empty list.
    before
        The forming-session cutoff (the caller-resolved IST date — in
        production :func:`brutex.calendars.nse.daily_derive_cutoff`,
        DEC-PREBUILD-SESSION-CLOSE-CUTOFF-001): any session whose IST date is
        ``>= before`` is DROPPED so a still-forming session is never emitted as a
        final daily bar (no look-ahead, no partial bar).

    Returns
    -------
    list[CandleBar]
        One midnight-IST-stamped daily bar per COMPLETE IST trading day present in
        ``df_1m``, ascending by timestamp. Prices stay full-precision ``Decimal``
        (the lake-write sink snaps to the 2-dp tick grid, DEC-SPOT-PRICE-2DP-001).
    """
    # Local imports keep polars out of this module's import graph for callers that
    # only need the dataclasses (CLAUDE.md §11 lazy dep, mirrors the materialiser).
    import polars as pl

    from brutex.data.tf_resampler import resample_1m_to_tf

    if df_1m.is_empty():
        return []

    daily = resample_1m_to_tf(df_1m, DAILY_TF_MINUTES)

    # VECTORIZED filter + re-stamp (DEC-PREBUILD-GIL-VECTORIZE-001 — the former
    # per-row `iter_rows` → `astimezone(_IST)` → `CandleBar(Decimal(str(...))×4)`
    # loop was one of the two GIL-held prebuild sections starving the dashboard
    # event loop; byte-identical output, see `frame_to_candle_bars`):
    #
    # 1. IST calendar date = the bar's instant shifted by the fixed +05:30 offset
    #    (`_IST` — India has no DST, so a duration shift IS the timezone
    #    conversion), then `.dt.date()` — identical to the former per-row
    #    `row["ts_utc"].astimezone(_IST).date()`.
    # 2. Still-forming / incomplete session (`ist_date >= before`) is DROPPED —
    #    never written as a final bar (same guard, now a columnar filter).
    # 3. Re-stamp to midnight IST (== 18:30 UTC of the prior calendar day): the
    #    IST date at 00:00, tagged UTC, shifted BACK by the same offset — the
    #    same UTC instant `datetime(y, m, d, tzinfo=_IST).astimezone(UTC)`
    #    produced, so a resampled daily bar stays byte-identical to a
    #    vendor-parsed "00:00:00 IST" one (the dedup identity; proven by the
    #    vectorize regression tests).
    ist_offset = _IST.utcoffset(None)  # timedelta(+05:30) — the ONE IST definition
    complete = (
        daily.with_columns((pl.col("ts_utc") + ist_offset).dt.date().alias("_ist_date"))
        .filter(pl.col("_ist_date") < before)
        .with_columns(
            (
                pl.col("_ist_date").cast(pl.Datetime("us")).dt.replace_time_zone("UTC") - ist_offset
            ).alias("ts_utc")
        )
        # Select exactly the converter's input columns — also drops `_ist_date`
        # and any OI column (spot/index daily bars carry `open_interest=None`
        # regardless, the legacy loop's unconditional contract).
        .select("ts_utc", "open", "high", "low", "close", "volume")
    )
    return frame_to_candle_bars(complete, absent_open_interest=None)


def resample_1m_to_daily_default(df_1m: pl.DataFrame, before: date) -> list[CandleBar]:
    """Positional ``(frame, before)`` adapter over :func:`resample_1m_to_daily`.

    :func:`resample_1m_to_daily` takes *before* keyword-only; the injectable
    ``resample_fn`` seam (:func:`fill_daily_from_minute`) uses a uniform positional
    ``(frame, before) -> list[CandleBar]`` signature so spot's default and the F&O
    :func:`brutex.data.fno_daily_resample.resample_1m_to_fno_daily` are
    interchangeable. This is the DEFAULT spot binding (OI ``None``).
    """
    return resample_1m_to_daily(df_1m, before=before)


def fill_daily_from_minute(
    *,
    read_1m: Callable[[], pl.DataFrame],
    write_1day: Callable[[Sequence[CandleBar]], int],
    before: date,
    resample_fn: Callable[[pl.DataFrame, date], list[CandleBar]] | None = None,
) -> DailyFillResult:
    """Read 1-minute, resample to midnight-IST daily, write — pure orchestration.

    Reads the window's 1-minute slice (via the injected *read_1m*), resamples it to
    daily (via *resample_fn*, default :func:`resample_1m_to_daily`), and — only when
    that yields at least one bar — writes the bars via the injected *write_1day*,
    returning the count. An empty 1-minute slice is an honest no-op
    (``bars_written == 0``, no write call), NEVER an invented bar. All lake/disk I/O
    is injected, so this is 100% unit-testable.

    Parameters
    ----------
    read_1m
        Zero-arg callable returning the window's stored 1-minute bars as a sorted
        polars frame (empty frame ⇒ no 1-minute in the lake ⇒ no-op).
    write_1day
        ``(bars) -> bars_written`` callable persisting the resampled daily bars to
        the ``1day`` partition (the sink snaps prices + merges idempotently).
    before
        The forming-session cutoff (the caller-resolved IST date), forwarded to the resampler.
    resample_fn
        The 1-minute → daily resampler ``(frame, before) -> list[CandleBar]``.
        Defaults to the spot :func:`resample_1m_to_daily` (OI ``None``); the F&O
        derive injects the carry-last-OI
        :func:`brutex.data.fno_daily_resample.resample_1m_to_fno_daily`
        (DEC-FNO-TF-DERIVE-OI-001).

    Returns
    -------
    DailyFillResult
        The written count + the resampled span (``first_ts`` / ``last_ts``).
    """
    resample = resample_1m_to_daily_default if resample_fn is None else resample_fn
    bars = resample(read_1m(), before)
    if not bars:
        return DailyFillResult(bars_written=0, first_ts=None, last_ts=None)
    written = write_1day(bars)
    return DailyFillResult(
        bars_written=written, first_ts=bars[0].timestamp, last_ts=bars[-1].timestamp
    )


def plan_daily_resample_windows(
    records: Iterable[WindowLedgerRecord],
    *,
    daily_interval: str = TRACK1_DAILY_INTERVAL,
) -> list[DailyResampleWindow]:
    """Select the daily windows a resample MIGHT fill — pure, deterministic.

    A candidate is a ``1day`` window the ledger settled EMPTY or SKIPPED **via the
    empty path** (``empty_streak > 0``): a below-threshold EMPTY (re-fetched next
    pull anyway) or an EMPTY-terminal / OLD-gap fast-settle SKIPPED. A SUCCESS
    window (has data) is excluded, and a SKIPPED window with ``empty_streak == 0``
    (a definitive vendor known-gap — Groww has none, but defended for safety) is
    excluded too: resample only targets windows that came back genuinely empty.

    The window's ``[start, end)`` ISO dates are parsed back to :class:`datetime.date`
    (tolerant of a date-only ``YYYY-MM-DD`` or a full-ISO datetime string) so the
    rebuilt :class:`~brutex.data.fetch_coverage.WindowKey` round-trips to the exact
    ledger row identity.
    """
    out: list[DailyResampleWindow] = []
    for record in records:
        if record.interval != daily_interval:
            continue
        if record.status not in (EMPTY, SKIPPED):
            continue
        if record.empty_streak <= 0:
            continue
        out.append(
            DailyResampleWindow(
                market=record.market,
                security_id=record.security_id,
                interval=record.interval,
                start=_parse_window_date(record.window_start),
                end=_parse_window_date(record.window_end),
            )
        )
    return out


def reconcile_daily_from_minute(
    *,
    records: Iterable[WindowLedgerRecord],
    ledger: WindowLedger,
    build_read_1m: Callable[[DailyResampleWindow], Callable[[], pl.DataFrame]],
    build_write_1day: Callable[[DailyResampleWindow], Callable[[Sequence[CandleBar]], int]],
    before: date,
    now: datetime,
    daily_interval: str = TRACK1_DAILY_INTERVAL,
) -> DailyReconcileReport:
    """Fill every empty daily window the lake's 1-minute can supply — pure orchestration.

    For each candidate window (:func:`plan_daily_resample_windows`): bind its
    1-minute reader + 1-day writer (the injected builders), resample-and-write
    (:func:`fill_daily_from_minute`), and on a non-empty fill re-record the SAME
    ledger row ``SUCCESS`` with ``raw_bars = bars_written`` and ``in_pull_fetches=0``
    (a resample makes ZERO vendor calls — the honest provenance signal). The
    ``SUCCESS`` record resets ``empty_streak`` to 0, so the now-has-data window is
    never re-promoted to SKIPPED. Each window is quarantined: a per-window error
    (corrupt slice / write failure) increments ``errored`` and continues — one bad
    window never blanks the pass (mirrors the ingest spine's per-window quarantine).
    All lake I/O is injected, so the whole flow is 100% unit-testable.
    """
    filled = no_minute = errored = bars = 0
    for window in plan_daily_resample_windows(records, daily_interval=daily_interval):
        try:
            result = fill_daily_from_minute(
                read_1m=build_read_1m(window),
                write_1day=build_write_1day(window),
                before=before,
            )
        except Exception as exc:
            errored += 1
            _LOG.warning(
                "spot_daily_resample.window_errored",
                symbol=window.security_id,
                window_start=window.start.isoformat(),
                window_end=window.end.isoformat(),
                error=str(exc),
            )
            continue
        if result.bars_written > 0:
            ledger.record(
                WindowKey(
                    market=window.market,
                    security_id=window.security_id,
                    interval=window.interval,
                    start=window.start,
                    end=window.end,
                ),
                raw_bars=result.bars_written,
                error_type=None,
                bars_appended=result.bars_written,
                now=now,
                in_pull_fetches=0,  # a resample made ZERO vendor fetches (honest provenance)
                # ... so its retry SPLIT is a KNOWN zero on both halves
                # (DEC-RETRY-COUNTER-SPLIT-001 — mirrors in_pull_fetches=0).
                empty_retries=0,
                transient_retries=0,
            )
            filled += 1
            bars += result.bars_written
            _LOG.info(
                "spot_daily_resample.window_filled",
                symbol=window.security_id,
                window_start=window.start.isoformat(),
                window_end=window.end.isoformat(),
                bars=result.bars_written,
                first_ts=result.first_ts.isoformat() if result.first_ts is not None else None,
                last_ts=result.last_ts.isoformat() if result.last_ts is not None else None,
            )
        else:
            no_minute += 1
            _LOG.info(
                "spot_daily_resample.window_no_minute",
                symbol=window.security_id,
                window_start=window.start.isoformat(),
                window_end=window.end.isoformat(),
            )
    return DailyReconcileReport(filled=filled, no_minute=no_minute, errored=errored, bars=bars)


def _parse_window_date(value: str) -> date:
    """Parse a ledger window bound (``YYYY-MM-DD`` or full ISO) to a :class:`date`.

    The ledger stores daily window bounds as date-only ISO (``WindowKey.start`` is a
    :class:`date`), but parse the full-ISO form too so a future datetime-keyed
    window never trips the reconcile. ``datetime.fromisoformat`` accepts both on
    Python 3.12.
    """
    return datetime.fromisoformat(value).date()


def ist_span_partition_months(start: date, end: date) -> list[tuple[int, int]]:
    """The UTC ``(year, month)`` lake partitions an IST ``[start, end)`` span can touch.

    The 1-minute lake partitions by **UTC** ``(year, month)``
    (:func:`~brutex.data.partition.partition_for_timestamp`), but
    :func:`make_lake_read_1m_window` filters by each bar's **IST trading date**
    ``(ts_utc + 05:30).date()``. An intraday spot bar lives at 09:15–15:30 IST =
    03:45–10:00 UTC, so its IST calendar day equals its UTC calendar day and hence its
    UTC partition month equals its IST month — so the partitions covering the IST span
    are exactly the UTC months the IST date range covers (DEC-RESAMPLE-BOUND-GLOB-001).

    To stay byte-correct even if a future / non-spot 1-minute bar ever sat within 05:30
    of a UTC month boundary (so its IST date and UTC partition month differ), the span
    is **padded by one calendar day on each side** before the month set is collected.
    That pad can at most add the immediately-adjacent month, so the result is still
    O(pulled-window) months (NOT O(lake-depth)); it merely guarantees no IST-edge bar
    is ever globbed out.

    Parameters
    ----------
    start, end
        The half-open IST date window ``[start, end)`` to derive ``1day`` over.

    Returns
    -------
    list[tuple[int, int]]
        The ascending ``(year, month)`` UTC partitions to read. An empty or inverted
        span (``end <= start``) yields ``[]`` — an honest no-op, exactly as the old
        whole-tree read produced an empty frame when the IST filter matched nothing.
    """
    if end <= start:
        return []
    # Pad one day each side so an IST-edge bar whose UTC partition month differs from
    # its IST month is never excluded (a proven no-op for intraday spot; a cheap safety
    # net for any boundary bar). ``[start, end)`` is half-open, so the last covered IST
    # date is ``end - 1 day``; pad that to ``end`` (inclusive) for the +1-day side.
    lo = start - timedelta(days=1)
    hi = end  # == (end - 1 day) + 1 day, the padded inclusive upper IST date
    months: list[tuple[int, int]] = []
    year, month = lo.year, lo.month
    while (year, month) <= (hi.year, hi.month):
        months.append((year, month))
        if month == MONTHS_PER_YEAR:
            year, month = year + 1, 1
        else:
            month += 1
    return months


def ist_span_partition_files(base: Path, start: date, end: date) -> list[str]:
    """The EXISTING ``<base>/<yyyy>/<mm>.parquet`` files for an IST ``[start, end)`` span.

    Bounds the post-pull 1-day derivation read to ONLY the month partitions the pulled
    window covers, instead of the whole ``<base>/**/*.parquet`` tree
    (DEC-RESAMPLE-BOUND-GLOB-001): the read is O(pulled-window) months, not
    O(lake-depth). The UTC partitions to consider come from
    :func:`ist_span_partition_months`; only those that EXIST on disk are returned (an
    absent month is skipped cleanly), as POSIX path strings ready for
    :func:`polars.read_parquet`. The in-memory IST-date filter in
    :func:`make_lake_read_1m_window` stays the precise final cut, so the resampled
    ``1day`` output is byte-identical to the old whole-tree read.

    Parameters
    ----------
    base
        The symbol's ``1minute`` partition root
        (``<lake_root>/bars/<exchange>/<segment>/<symbol>/1minute``).
    start, end
        The half-open IST date window ``[start, end)``.

    Returns
    -------
    list[str]
        Ascending POSIX paths of the existing month-partition Parquet files. Empty when
        the span is empty/inverted OR no covered month exists on disk.
    """
    files: list[str] = []
    for year, month in ist_span_partition_months(start, end):
        candidate = base / f"{year:04d}" / f"{month:02d}.parquet"
        if candidate.is_file():
            files.append(candidate.as_posix())
    return files


def make_lake_read_1m_window(  # pragma: no cover — raw lake read, integration-tested
    *,
    lake_root: Path,
    exchange: str,
    segment: str,
    symbol: str,
    start: date,
    end: date,
    include_oi: bool = False,
) -> Callable[[], pl.DataFrame]:
    """Build the injected 1-minute reader for one ``[start, end)`` daily window.

    Returns a zero-arg callable reading the symbol's stored ``1minute`` Parquet,
    filtered to the bars whose IST trading date falls in the half-open window
    ``[start, end)``, into a sorted polars frame with exactly the resampler's columns
    (``ts_utc, open, high, low, close, volume`` — plus ``open_interest`` when
    *include_oi* is True). The read is BOUNDED to ONLY the
    ``<yyyy>/<mm>.parquet`` month partitions the IST span covers
    (:func:`ist_span_partition_files`) instead of the whole ``/**/*.parquet`` tree, so
    the post-pull derivation is O(pulled-window) months, not O(lake-depth)
    (DEC-RESAMPLE-BOUND-GLOB-001); the in-memory IST-date filter below is the precise
    final cut, so the output is byte-identical to the old whole-tree read. UNLIKE
    :func:`~brutex.data.timeframe_materialize.make_lake_read_1m` (which RAISES when no
    1-minute exists), no covered partition on disk returns an EMPTY frame so "no
    1-minute to fill from" is a clean no-op, never an error (the verify-first default).

    *include_oi* (DEC-FNO-TF-DERIVE-OI-001): spot callers leave it False (the
    OHLCV-6 frame, unchanged). The F&O daily derive passes ``include_oi=True`` so the
    ``open_interest`` column is read — both the ``.select(...)`` and the empty-schema
    fallback frame gain it (``pl.Int64``) — and the OI-aware resampler carries
    last-of-day OI into the derived ``1day`` partition.
    Raw lake I/O — ``# pragma: no cover``; exercised by the round-trip integration test.
    """
    import polars as pl

    def _read() -> pl.DataFrame:
        base = lake_root / "bars" / exchange / segment / symbol / "1minute"
        files = ist_span_partition_files(base, start, end)
        columns = ["ts_utc", "open", "high", "low", "close", "volume"]
        if include_oi:
            columns.append("open_interest")
        if not files:
            # The F&O reader appends the nullable Int64 ``open_interest`` column so a
            # no-partition F&O target still returns a 7-col frame the OI-aware daily
            # resampler reads (DEC-FNO-TF-DERIVE-OI-001); spot stays the OHLCV-6. Two
            # literal schemas (not a merged dict) so polars' typed ``schema=`` is exact.
            if include_oi:
                return pl.DataFrame(
                    schema={
                        "ts_utc": pl.Datetime(time_unit="us", time_zone="UTC"),
                        "open": pl.Float64,
                        "high": pl.Float64,
                        "low": pl.Float64,
                        "close": pl.Float64,
                        "volume": pl.Int64,
                        "open_interest": pl.Int64,
                    }
                )
            return pl.DataFrame(
                schema={
                    "ts_utc": pl.Datetime(time_unit="us", time_zone="UTC"),
                    "open": pl.Float64,
                    "high": pl.Float64,
                    "low": pl.Float64,
                    "close": pl.Float64,
                    "volume": pl.Int64,
                }
            )
        # SCHEMA-TOLERANT multi-file read (DEC-GREEKS-ENGINE-PR2B-001): an OPTION
        # contract's month files can mix the raw 7-column and the Greeks-enriched
        # 17-column shape (plan §15.3 — e.g. a window-bounded enrich, or a crash
        # mid-pass), and polars refuses mixed schemas by default. The first file's
        # schema anchors; a missing column null-fills, an extra one is ignored —
        # the `.select(*columns)` below needs only the base OHLCV(+OI) columns,
        # which every shape carries, so the output is byte-identical for a
        # uniform tree.
        df = pl.scan_parquet(files, missing_columns="insert", extra_columns="ignore").collect()
        # IST trading date of each 1-minute bar = (ts_utc + 05:30) date. Intraday
        # bars (09:15–15:30 IST) never cross the UTC midnight, so this is exact.
        ist_date = (pl.col("ts_utc") + pl.duration(hours=5, minutes=30)).dt.date()
        return (
            df.rename({"timestamp": "ts_utc"})
            .with_columns(ist_date.alias("_ist_date"))
            .filter((pl.col("_ist_date") >= start) & (pl.col("_ist_date") < end))
            .select(*columns)
            .sort("ts_utc")
        )

    return _read


def make_lake_write_1day(  # pragma: no cover — raw lake write, integration-tested
    *,
    lake_root: Path,
    exchange: str,
    segment: str,
    symbol: str,
) -> Callable[[Sequence[CandleBar]], int]:
    """Build the injected ``1day`` writer for :func:`fill_daily_from_minute`.

    Returns a ``(bars) -> bars_written`` callable appending the resampled daily bars
    to the lake's ``1day`` partition via a
    :class:`~brutex.data.sink.ParquetBarSink` (the SAME sink the vendor spot spine
    writes through — CLAUDE.md §11 COMMON), so a resampled daily bar shares the
    vendor stamp/schema/OI (see the module docstring on value-vs-stamp identity). The
    sink snaps prices to the 2-dp tick grid (DEC-SPOT-PRICE-2DP-001) and merges by
    ``(year, month)`` using ``merge_mode="latest-wins"`` for this DERIVED ``1day``
    partition (DEC-1MINUTE-ONLY-DERIVE-1DAY-001 / FIX-2): a re-derivation of the SAME
    IST date OVERWRITES the prior derived bar, so a later COMPLETE-session pull
    CORRECTS a daily that an earlier PARTIAL-session pull wrote (mid-session token
    death / narrow ``to_date`` / a transient 1-minute gap). Since 1-day is now ALWAYS
    derived from 1-minute (there is NO vendor daily to protect with keep-first), this
    self-heals the partial-session frozen-bar hazard the old keep-first dedup left in
    place. A re-run over the SAME complete 1-minute is still idempotent (identical
    OHLCV is a silent no-op; only a CHANGED value rewrites). The returned
    ``bars_appended`` is the INPUT bar count (the sink's contract), NOT the net-new
    merged count. Raw lake I/O — ``# pragma: no cover``; integration-tested.
    """
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    # DERIVED-1day writer → per-partition telemetry at DEBUG
    # (DEC-PREBUILD-LOG-FLOOD-001, same rationale as make_lake_write_tf: the
    # prebuild's derived fan-out must cost O(symbols) INFO lines, never
    # O(partitions) — the per-symbol aggregate rides
    # `lake.derived.write_summary`); the live-pull vendor writers keep INFO.
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root, partition_log_level="debug"))

    def _write(bars: Sequence[CandleBar]) -> int:
        result = sink.append(
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            interval=TRACK1_DAILY_INTERVAL,
            bars=list(bars),
            merge_mode="latest-wins",  # derived 1day self-heals a partial-session bar
        )
        return result.bars_appended

    return _write
