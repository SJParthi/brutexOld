"""Generation-time Greeks enrichment — ``greeks_enrich_after_pull`` (Phase 2b).

`DEC-GREEKS-ENGINE-PR2B-001` — the pull-time compute-and-store of
`DEC-GREEKS-GENERATION-TIME-001` (``docs/GREEKS_ENGINE_PLAN.md`` §15 /
ADR-0015 §2b D10-D13). After a successful F&O 1-minute pull, every OPTION
contract's just-pulled bars are joined to the underlying spot at the SAME
minute (exact :func:`~brutex.data.spot_join.utc_instant` join, NO
nearest-fallback), the IV is solved and the six Greeks priced off the ONE
shared Rust kernel (:func:`brutex.options.greeks.compute_greeks`,
`DEC-GREEKS-ENGINE-PR2-001`), and the results are STORED alongside the bars as
the ten nullable overlay columns (:data:`~brutex.data.models.GREEKS_OVERLAY_FIELDS`)
via the SAME partition-flock ``latest-wins`` write spine the derived ``1day``
uses.

The locked model inputs frozen into every stored byte (plan §15.6 gate (b)):

* **Risk-free ``r``** — ONLY through PR-1's stamped
  :class:`~brutex.options.risk_free.RiskFreeSource` seam
  (`DEC-GREEKS-ENGINE-PR1-001`): the fixed 6.5% default, or the T-bill CSV
  step function via ``BRUTEX_RISK_FREE_CSV`` (:func:`resolve_risk_free_source`).
* **Dividend ``q = 0``** — the ADR-0015 §3 #3 default, bias direction stamped
  (:data:`Q_BIAS_ZERO`): the forward is ``F = S·e^(r·T)``.
* **Time-to-expiry ``T``** — calendar **Act/365 seconds to 15:30 IST on the
  expiry date** (:data:`TTE_ACT365_EXPIRY_1530_IST`) — the convention the
  kernel itself thinks in (its 600 s ``T``-floor and per-calendar-day theta
  are Act/365). The ADR §3 #4 trading-minutes refinement stays an
  operator-overridable follow-up; because the convention is packed into the
  provenance stamp, switching later is a clean stamp-driven bulk re-enrich,
  never a silent-staleness hazard (plan §15.6 top risk #2).
* **Exercise style** — derived from WHICH spot segment supplies the join
  (plan §15.5): ``INDEX`` → European exact, ``CASH`` → American single stock
  → the ADR §3 #1 default FLAGGED APPROXIMATION (:data:`STYLE_AMERICAN_APPROX`,
  computed but stamped, never silently presented as exact).

Every unsolvable / unknown input stores an HONEST NULL with a packed status
reason (plan §15.8) — never a fabricated value; a FUT leg is never enriched at
all ("never a bogus Greek for a future": its partition stays raw 7-column and
the overlay columns READ as NULL via the schema-tolerant read-back,
byte-identically to a stamped-NA row, at zero write cost).

Complexity (honest, plan §15.7): the pass is **O(bars in the pulled window)**,
run ONCE per pull AFTER the fetch lane has fully drained (it never blocks a
fetch), month-file-bounded via
:func:`~brutex.data.spot_daily_resample.ist_span_partition_files` (never
O(lake-depth)); the per-bar IV solve is the kernel's bounded O(1)-per-call
(NOT an O(1)-total claim). Reads afterwards are plain O(1)-per-bar column
projections. Re-runs are idempotent: a bar whose stored provenance stamp
matches the current inputs AND solved cleanly AND whose stored
``rate_used`` / ``spot_at_bar`` / style equal this pass's values is REUSED
without recompute (the value-hardened gate of
DEC-GREEKS-CORRECTNESS-AUDIT-001 — the stamp alone cannot see a rate/spot
VALUE change), an identical restate is detected pre-write (no partition
rewrite at all), and the ``latest-wins`` merge corrects any stale overlay
(Golden Rule #7).

Concurrency (honest envelope, DEC-GREEKS-CORRECTNESS-AUDIT-001 G-RACE-3 +
DEC-GREEKS-PARALLEL-001): the pass is WRITE-serialised via the per-partition
flock and safe against a concurrent KEEP-FIRST vendor pull (raw OHLCV identity
untouched either way), but two concurrent ENRICH passes are LAST-WRITER-WINS —
never torn, the stamp always agrees with its values, yet the slower pass's
inputs restate. The intended execution model is single-ENRICH-PASS-per-lake
(the pass runs once per pull, post-drain) — UNCHANGED by the in-pass process
pool: the parallel driver's spawn workers write DISJOINT contract symbols by
construction (all targets of one ``(exchange, underlying)`` whose symbols
could collide share ONE work unit, processed in sorted order inside one
worker), so intra-pass workers never contend on a partition and the stored
bytes are IDENTICAL to the sequential pass (golden-pinned). The driver's own
structlog events + liveness beats are emitted PARENT-side in sorted order;
the DEEP seam logs (read-dedup warnings, ``spot_key_recovered``) fire inside
per-target processing and therefore emit CHILD-side in parallel mode —
honest: log parity is driver-events-only, never claimed for the deep seams.
"""

from __future__ import annotations

import multiprocessing
import os
import pickle  # nosec B403 — dumps-only picklability probe; never loads untrusted data
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from itertools import groupby
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog

from brutex.data.errors import GreeksEnrichPoolBroken, LakeWriteHalt
from brutex.data.fno_candle_lake import FNO_CANDLE_LAKE_SEGMENT
from brutex.data.fno_symbol import parse_fno_symbol
from brutex.data.lake_keys import spot_recovery_candidates
from brutex.data.lake_read import dedup_bars_frame
from brutex.data.models import GREEKS_OVERLAY_FIELDS, FnoGreeksBar
from brutex.data.spot_daily_resample import ist_span_partition_files
from brutex.data.spot_join import SpotJoin, spot_close_by_ts_window, utc_instant
from brutex.money import round_money
from brutex.observability.progress_beat import emit_progress_beat
from brutex.options.greeks import T_FLOOR_YEARS, GreeksInputError, compute_greeks
from brutex.options.risk_free import (
    RISK_FREE_SOURCE_VERSION,
    CsvRateSource,
    FixedRateSource,
    RiskFreeRate,
    RiskFreeSource,
    load_risk_free_csv,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from brutex.data.fno_symbol import FnoSymbolParts

_LOG = structlog.get_logger("brutex.data.greeks_enrich")

__all__ = [
    "DEGENERATE_TIME_VALUE_REL",
    "EXPIRY_HOUR_IST",
    "EXPIRY_MINUTE_IST",
    "GREEKS_FORMULA_VERSION",
    "GREEKS_WORKERS_ENV",
    "Q_BIAS_ZERO",
    "RATE_SOURCE_ID_CSV",
    "RATE_SOURCE_ID_FIXED",
    "RATE_SOURCE_ID_NONE",
    "SECONDS_PER_YEAR_ACT365",
    "STATUS_AT_EXPIRY",
    "STATUS_BAD_INPUT",
    "STATUS_BELOW_INTRINSIC",
    "STATUS_NO_RATE",
    "STATUS_NO_SOLUTION",
    "STATUS_NO_SPOT",
    "STATUS_OK",
    "STYLE_AMERICAN_APPROX",
    "STYLE_EUROPEAN_EXACT",
    "STYLE_UNKNOWN",
    "TTE_ACT365_EXPIRY_1530_IST",
    "EnrichStats",
    "GreeksEnrichReport",
    "GreeksEnrichTarget",
    "GreeksProvenance",
    "enrich_greeks_for_targets",
    "enrich_option_bars",
    "expiry_instant_utc",
    "greeks_pool_workers",
    "pack_greeks_provenance",
    "rate_source_id_for",
    "read_spot_join_with_recovery",
    "resolve_greeks_workers",
    "resolve_risk_free_source",
    "t_years_act365",
    "unpack_greeks_provenance",
]

#: Formula version frozen into every stored tuple. Bump when the kernel maths /
#: overlay semantics change — the staleness key that turns a formula revision
#: into a positive re-enrich signal (plan §15.3 "why the stamp is MANDATORY").
#: v2 = the DEC-GREEKS-CORRECTNESS-AUDIT-001 round (degenerate time-value
#: pre-screen + value-hardened reuse + 2dp spot snap): every v1 stored bar —
#: incl. the IVX-1 at-floor artifacts — recomputes + restamps on the next
#: enrich pass over its window. HONEST envelope: that pass reaches only
#: contracts still inside a live pull window; expired contracts keep v1
#: stamps until a bulk re-enrich surface exists (named follow-up).
GREEKS_FORMULA_VERSION: Final[int] = 2

#: Rate-source ids packed into the provenance stamp (bits 8-11).
RATE_SOURCE_ID_NONE: Final[int] = 0
RATE_SOURCE_ID_FIXED: Final[int] = 1
RATE_SOURCE_ID_CSV: Final[int] = 2

#: Dividend-bias id (bits 16-17): ``q = 0`` with the documented bias direction
#: (over-prices calls / under-prices puts — ADR-0015 §4), the §3 #3 default.
Q_BIAS_ZERO: Final[int] = 1

#: Time-to-expiry convention id (bits 18-19): calendar Act/365 seconds to
#: 15:30 IST on the expiry date (see the module docstring for the lock note).
TTE_ACT365_EXPIRY_1530_IST: Final[int] = 1

#: Exercise-style flags (bits 20-22). ``STYLE_AMERICAN_APPROX`` marks the
#: ADR-0015 §3 #1 FLAGGED APPROXIMATION for the ~212 American single stocks —
#: computed, never silently presented as exact (Golden Rule #10).
STYLE_UNKNOWN: Final[int] = 0
STYLE_EUROPEAN_EXACT: Final[int] = 1
STYLE_AMERICAN_APPROX: Final[int] = 2

#: Per-bar solve statuses (bits 23-26) — the stamped NULL reasons of plan §15.8.
STATUS_OK: Final[int] = 0
STATUS_NO_SPOT: Final[int] = 1
STATUS_NO_SOLUTION: Final[int] = 2
STATUS_AT_EXPIRY: Final[int] = 3
STATUS_NO_RATE: Final[int] = 4
STATUS_BAD_INPUT: Final[int] = 5
STATUS_BELOW_INTRINSIC: Final[int] = 6

#: Human-readable name per packed status — the ``null_reasons`` telemetry
#: labels (DEC-GREEKS-CORRECTNESS-AUDIT-001 CTR-6).
_STATUS_NAMES: Final[dict[int, str]] = {
    STATUS_OK: "ok",
    STATUS_NO_SPOT: "no_spot",
    STATUS_NO_SOLUTION: "no_solution",
    STATUS_AT_EXPIRY: "at_expiry",
    STATUS_NO_RATE: "no_rate",
    STATUS_BAD_INPUT: "bad_input",
    STATUS_BELOW_INTRINSIC: "below_intrinsic",
}

#: NSE / BSE F&O contracts expire at the 15:30 IST market close of the expiry
#: date (CLAUDE.md §7 trading hours) — the expiry INSTANT the ``T`` convention
#: anchors on.
EXPIRY_HOUR_IST: Final[int] = 15
EXPIRY_MINUTE_IST: Final[int] = 30

#: Act/365 calendar seconds per year — the kernel's own day-count base
#: (its ``T``-floor is "600 s in Act/365 years"; theta is per Act/365 day).
SECONDS_PER_YEAR_ACT365: Final[float] = 365.0 * 86400.0

#: Relative degenerate-time-value band of the BELOW_INTRINSIC pre-screen
#: (DEC-GREEKS-CORRECTNESS-AUDIT-001 IVX-1). Mirrors the kernel's
#: ``VERIFY_REL_TOL`` (``rust/trading-core/src/greeks/mod.rs`` — the 1e-9
#: relative round-trip verification tolerance, whose discounted-space image is
#: exactly ``1e-9 · close``): a premium within this band of the discounted
#: no-arb floor carries ~zero time value, so the kernel would "verify" ANY
#: sigma across a huge indeterminacy band (measured up to [0.001, 0.29]) and
#: return one arbitrary member. Such bars are refused as honest
#: :data:`STATUS_BELOW_INTRINSIC` NULLs instead — never an arbitrary stored
#: IV. Preflight-validated on BOTH sides (CE and PE: 0 escapes, 0 over-screens
#: over 292,500 production-shaped combos; the smallest genuine one-tick time
#: value sits ~5000x above the band).
DEGENERATE_TIME_VALUE_REL: Final[float] = 1e-9

#: Fixed +05:30 IST (no DST) — the same offset the vendor parser uses.
_IST: Final[timezone] = timezone(timedelta(hours=5, minutes=30))

#: The interval the enrichment reads + writes — the ONLY vendor-fetched
#: granularity (DEC-1MINUTE-ONLY-DERIVE-1DAY-001). Derived-TF propagation is
#: close-snapshot per plan §15.10 and lands with the resample-consuming
#: Phase-3 surfaces, NOT here.
_MINUTE_INTERVAL: Final[str] = "1minute"

#: Capacity of the per-pass LRU memo over the resolved spot reader
#: (DEC-GREEKS-SPOT-MEMO-001). With targets sorted so identical spot
#: coordinates are adjacent, 2 covers the active (underlying, window) key plus
#: one boundary neighbour; each cached :class:`~brutex.data.spot_join.SpotJoin`
#: holds one window's close map, so the memory envelope is O(2 windows) —
#: deliberately NOT raised (unbounded would hold every underlying's map).
_SPOT_MEMO_CAPACITY: Final[int] = 2

# Bit layout of the packed Int32 provenance stamp. The INPUT-defining fields
# (formula/rate/q/T — bits 0-19) decide staleness; style + status (bits 20-26)
# describe the individual bar. Total 27 bits < 2^31 (Int32-safe, validated by
# FnoGreeksBar).
_FORMULA_SHIFT: Final[int] = 0
_RATE_SOURCE_SHIFT: Final[int] = 8
_RATE_VERSION_SHIFT: Final[int] = 12
_Q_BIAS_SHIFT: Final[int] = 16
_TTE_SHIFT: Final[int] = 18
_STYLE_SHIFT: Final[int] = 20
_STATUS_SHIFT: Final[int] = 23
_FORMULA_MASK: Final[int] = 0xFF
_RATE_SOURCE_MASK: Final[int] = 0xF
_RATE_VERSION_MASK: Final[int] = 0xF
_Q_BIAS_MASK: Final[int] = 0x3
_TTE_MASK: Final[int] = 0x3
_STYLE_MASK: Final[int] = 0x7
_STATUS_MASK: Final[int] = 0xF

#: The provenance bits that define the MODEL INPUTS (formula + rate source +
#: rate version + q bias + T convention) — the staleness-compare subset; style
#: and status are per-bar descriptions, not inputs.
_INPUTS_MASK: Final[int] = (1 << _STYLE_SHIFT) - 1


@dataclass(frozen=True, slots=True)
class GreeksProvenance:
    """The unpacked fields of one ``greeks_provenance_id`` stamp.

    Attributes
    ----------
    formula_version
        :data:`GREEKS_FORMULA_VERSION` at write time.
    rate_source_id, rate_version
        The risk-free source kind + its ``RISK_FREE_SOURCE_VERSION``.
    q_bias_id
        The dividend assumption (:data:`Q_BIAS_ZERO`).
    tte_convention_id
        The time-to-expiry convention (:data:`TTE_ACT365_EXPIRY_1530_IST`).
    style_flag
        The exercise-style flag (:data:`STYLE_EUROPEAN_EXACT` /
        :data:`STYLE_AMERICAN_APPROX` / :data:`STYLE_UNKNOWN`).
    status
        The per-bar solve status (:data:`STATUS_OK` or a stamped NULL reason).
    """

    formula_version: int
    rate_source_id: int
    rate_version: int
    q_bias_id: int
    tte_convention_id: int
    style_flag: int
    status: int


def pack_greeks_provenance(
    *,
    rate_source_id: int,
    rate_version: int = RISK_FREE_SOURCE_VERSION,
    style_flag: int,
    status: int,
    formula_version: int = GREEKS_FORMULA_VERSION,
    q_bias_id: int = Q_BIAS_ZERO,
    tte_convention_id: int = TTE_ACT365_EXPIRY_1530_IST,
) -> int:
    """Pack the provenance fields into the stored Int32 stamp.

    Every field is validated LOUDLY against its documented bit band BEFORE
    packing (DEC-GREEKS-CORRECTNESS-AUDIT-001 PROV-2): the old silent
    ``&``-mask made an out-of-band value (e.g. a future
    ``RISK_FREE_SOURCE_VERSION = 17``) collide bit-identically with an
    in-band stamp — the wrong failure mode for a STALENESS key, whose one job
    is to never alias two different input sets. An in-band call packs the
    byte-identical integer it always did (27 used bits < 2^31, so the
    :class:`~brutex.data.models.FnoGreeksBar` range validation can never fire
    for a stamp built here).

    Raises
    ------
    ValueError
        When any field lies outside its bit band (negative, or above the
        band's mask) — naming the field, the value, and the band width.
    """
    for name, value, mask in (
        ("formula_version", formula_version, _FORMULA_MASK),
        ("rate_source_id", rate_source_id, _RATE_SOURCE_MASK),
        ("rate_version", rate_version, _RATE_VERSION_MASK),
        ("q_bias_id", q_bias_id, _Q_BIAS_MASK),
        ("tte_convention_id", tte_convention_id, _TTE_MASK),
        ("style_flag", style_flag, _STYLE_MASK),
        ("status", status, _STATUS_MASK),
    ):
        if not 0 <= value <= mask:
            msg = (
                f"greeks provenance {name}={value} outside its "
                f"{mask.bit_length()}-bit band [0, {mask}]"
            )
            raise ValueError(msg)
    return (
        (formula_version << _FORMULA_SHIFT)
        | (rate_source_id << _RATE_SOURCE_SHIFT)
        | (rate_version << _RATE_VERSION_SHIFT)
        | (q_bias_id << _Q_BIAS_SHIFT)
        | (tte_convention_id << _TTE_SHIFT)
        | (style_flag << _STYLE_SHIFT)
        | (status << _STATUS_SHIFT)
    )


def unpack_greeks_provenance(packed: int) -> GreeksProvenance:
    """Unpack a stored ``greeks_provenance_id`` into its fields (pure inverse)."""
    return GreeksProvenance(
        formula_version=(packed >> _FORMULA_SHIFT) & _FORMULA_MASK,
        rate_source_id=(packed >> _RATE_SOURCE_SHIFT) & _RATE_SOURCE_MASK,
        rate_version=(packed >> _RATE_VERSION_SHIFT) & _RATE_VERSION_MASK,
        q_bias_id=(packed >> _Q_BIAS_SHIFT) & _Q_BIAS_MASK,
        tte_convention_id=(packed >> _TTE_SHIFT) & _TTE_MASK,
        style_flag=(packed >> _STYLE_SHIFT) & _STYLE_MASK,
        status=(packed >> _STATUS_SHIFT) & _STATUS_MASK,
    )


def rate_source_id_for(source: RiskFreeSource) -> int:
    """Map a :class:`~brutex.options.risk_free.RiskFreeSource` to its packed id.

    A source kind this module does not recognise packs
    :data:`RATE_SOURCE_ID_NONE` — an honest "unlabelled" rather than a wrong
    label (the rate VALUE consumed still rides the stamped
    :class:`~brutex.options.risk_free.RiskFreeRate` seam either way).
    """
    if isinstance(source, FixedRateSource):
        return RATE_SOURCE_ID_FIXED
    if isinstance(source, CsvRateSource):
        return RATE_SOURCE_ID_CSV
    return RATE_SOURCE_ID_NONE


def resolve_risk_free_source(env: Mapping[str, str] | None = None) -> RiskFreeSource:
    """Resolve the production risk-free source (the locked ``r`` of gate (b)).

    ``BRUTEX_RISK_FREE_CSV`` set → the T-bill daily CSV step function
    (:func:`~brutex.options.risk_free.load_risk_free_csv`); unset/blank → the
    plan-default :class:`~brutex.options.risk_free.FixedRateSource` (6.5%,
    ADR-0015 §3 #2). A configured-but-broken CSV raises
    :class:`~brutex.options.risk_free.RiskFreeError` LOUDLY — the guarded
    ``greeks_enrich_after_pull`` wrapper then SKIPS the pass with an error log
    rather than silently pricing with a rate the operator did not configure
    (Golden Rule #1).
    """
    mapping: Mapping[str, str] = os.environ if env is None else env
    path = mapping.get("BRUTEX_RISK_FREE_CSV", "").strip()
    if not path:
        return FixedRateSource()
    return load_risk_free_csv(Path(path))


def expiry_instant_utc(expiry: date) -> datetime:
    """The UTC instant an F&O contract expires: 15:30 IST on its expiry date."""
    return datetime(
        expiry.year,
        expiry.month,
        expiry.day,
        EXPIRY_HOUR_IST,
        EXPIRY_MINUTE_IST,
        tzinfo=_IST,
    ).astimezone(UTC)


def t_years_act365(bar_ts: datetime, expiry_at: datetime) -> float:
    """Time to expiry in Act/365 calendar years — the locked ``T`` convention.

    ``T`` anchors on the bar-LABEL instant (the minute OPEN) while the solved
    premium is the minute CLOSE — up to 60 s later
    (DEC-GREEKS-CORRECTNESS-AUDIT-001 B76-3, a standard convention stated
    honestly): the measured IV bias envelope of the 60 s skew is up to
    ~−4.7% relative on the LAST tradable expiry-day minutes, ~−0.8% at 1 h
    to expiry, and < 0.15% beyond ~6 h — negligible at ≥ 1 day. Changing the
    anchor would be a formula revision (a ``GREEKS_FORMULA_VERSION`` / TTE
    bump + bulk re-enrich), deliberately NOT done in the audit round.

    Negative when the bar sits at/after the expiry instant (the caller stamps
    :data:`STATUS_AT_EXPIRY`; such bars should not exist upstream —
    ``drop_bars_after`` already drops post-expiry-date bars — but the guard is
    honest, never a blown-up gamma).
    """
    return (expiry_at - utc_instant(bar_ts)).total_seconds() / SECONDS_PER_YEAR_ACT365


@dataclass(frozen=True, slots=True)
class GreeksEnrichTarget:
    """One option contract + its just-pulled ``[start, end)`` window to enrich.

    Mirrors :class:`~brutex.data.spot_daily_resample.DailyDeriveTarget` (the
    hook is the F&O Greeks twin of the daily derive): the lake address
    (``exchange`` + full contract ``symbol``; segment is always ``FNO``) plus
    the half-open IST date window the pull landed 1-minute bars over.
    """

    exchange: str
    symbol: str
    start: date
    end: date


@dataclass(frozen=True, slots=True)
class EnrichStats:
    """Per-target outcome of one :func:`enrich_option_bars` pass.

    Attributes
    ----------
    solved
        Bars whose Greeks were freshly SOLVED this pass (:data:`STATUS_OK`).
    nulls
        Bars stamped with an honest NULL reason this pass (any non-OK status).
    reused
        Bars whose stored stamp already matched the current inputs with
        :data:`STATUS_OK` — restated verbatim, no recompute (the O(1)
        stamp-compare skip of plan §15.4).
    changed
        Bars whose overlay DIFFERS from what is stored — ``0`` means the
        whole target is already OVERLAY-current and the pass issues no write.
        Honest scope note (CTR-4): "no write needed" is an overlay claim, not
        a disk-hygiene one — repair of a physically-duplicated (poisoned)
        partition is owned by the dedicated lake healer
        (:mod:`brutex.data.lake_heal`, DEC-LAKE-HEAL-001), never manufactured
        by the enricher skipping/forcing writes.
    skipped
        Rows dropped by the non-finite price screen (a NaN / inf OHLC field,
        DEC-GREEKS-CORRECTNESS-AUDIT-001 IVX-2) — NO bar is built for them, so
        the ``latest-wins`` merge leaves the stored raw row untouched (never a
        fabricated bar, never a whole-contract quarantine). Defaults to ``0``
        so a stats object built without the field reads as "no rows skipped".
    null_reasons
        The per-status breakdown of ``nulls`` this pass: sorted
        ``(status_name, count)`` pairs, non-zero counts only (CTR-6 — the
        telemetry that surfaces a whole-chain ``no_spot``). The driver logs
        it as a dict on ``greeks_enrich.target_enriched``. Defaults empty.
    """

    solved: int
    nulls: int
    reused: int
    changed: int
    skipped: int = 0
    null_reasons: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True, slots=True)
class GreeksEnrichReport:
    """Aggregate outcome of one :func:`enrich_greeks_for_targets` pass.

    Attributes
    ----------
    enriched
        Targets whose overlay was (re)written this pass.
    already_current
        Targets whose every bar already carried the current-input overlay —
        skipped with NO write (the idempotent re-run fast path).
    futures_skipped
        FUT targets skipped by design — never a bogus Greek for a future
        (plan §15.8; the raw 7-column partition reads NULL overlay columns).
    undecodable
        Targets whose symbol :func:`~brutex.data.fno_symbol.parse_fno_symbol`
        could not decode (logged, never a crash).
    no_bars
        Option targets with no stored 1-minute bars in the window (honest
        no-op — nothing to enrich, never an invented bar).
    errored
        Targets quarantined by a PER-CONTRACT error (corrupt slice / write
        failure) — one bad contract never sinks the pass (plan §15.4). This
        counts per-contract quarantines ONLY: a pull-wide
        :class:`~brutex.data.errors.LakeWriteHalt` (disk full / OOM)
        re-raises out of the pass instead of inflating this tally
        (DEC-GREEKS-CORRECTNESS-AUDIT-001 CTR-2). A quarantined target may
        still have durably landed SOME month partitions before its error
        (the sink writes months sequentially, each atomically) — those bars
        are excluded from every tally here and self-heal on the next pass.
    bars_written
        Total bars carried in the ``latest-wins`` writes of the targets
        counted under ``enriched`` — partitions durably landed by a target
        that then ERRORED are honestly NOT included (see ``errored``).
    bars_solved, bars_null, bars_reused
        Bar-level tallies summed across the written targets. The per-reason
        breakdown of ``bars_null`` (the ``no_spot`` tally the plan asks for)
        is the ``null_reasons`` dict each ``greeks_enrich.target_enriched``
        log line carries (:attr:`EnrichStats.null_reasons`) — the report
        itself keeps only the total.
    spot_reads, spot_hits
        Underlying spot window reads actually PERFORMED vs reads AVOIDED by
        the capacity-2 LRU memo over the resolved spot reader
        (DEC-GREEKS-SPOT-MEMO-001) — the measured redundant-read counters
        surfaced in ``fno.greeks_enrich.done`` (a measurement, never a
        wall-clock claim; Golden Rule #10).
    bars_skipped
        Rows dropped by the non-finite price screen, summed across EVERY read
        target (including ``already_current`` ones — a skip is tallied even
        when nothing else changed). Defaults to ``0`` for pre-audit callers.
    """

    enriched: int
    already_current: int
    futures_skipped: int
    undecodable: int
    no_bars: int
    errored: int
    bars_written: int
    bars_solved: int
    bars_null: int
    bars_reused: int
    spot_reads: int
    spot_hits: int
    bars_skipped: int = 0

    @property
    def targets(self) -> int:
        """Total targets considered across every tally bucket."""
        return (
            self.enriched
            + self.already_current
            + self.futures_skipped
            + self.undecodable
            + self.no_bars
            + self.errored
        )


def _overlay_of_row(row: Mapping[str, object]) -> tuple[object, ...]:
    """The stored overlay of a lake row, normalised for change-comparison.

    ``spot_at_bar`` arrives as a raw Parquet float; it is normalised through
    ``Decimal(str(...))`` so it compares equal to the freshly-built bar's
    2dp-snapped Decimal (both sides carry the same 2dp value).
    """
    values: list[object] = []
    for name in GREEKS_OVERLAY_FIELDS:
        value = row.get(name)
        if name == "spot_at_bar" and value is not None:
            value = Decimal(str(value))
        values.append(value)
    return tuple(values)


def _overlay_of_bar(bar: FnoGreeksBar) -> tuple[object, ...]:
    """The overlay of a freshly-built bar, in the canonical field order."""
    return tuple(getattr(bar, name) for name in GREEKS_OVERLAY_FIELDS)


def _row_price_fields(row: Mapping[str, object]) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Reconstruct a row's OHLC as exact Decimals (the lake stores 2dp floats)."""
    return (
        Decimal(str(row["open"])),
        Decimal(str(row["high"])),
        Decimal(str(row["low"])),
        Decimal(str(row["close"])),
    )


def _no_arb_floor(
    *, is_call: bool, spot: float, strike: float, rate: float, t_years: float
) -> float:
    """The Black-76 no-arbitrage LOWER bound of the option premium (q = 0).

    ``max(0, e^{−rT}(F − K)) = max(0, S − K·e^{−rT})`` for a call and
    ``max(0, e^{−rT}(K − F)) = max(0, K·e^{−rT} − S)`` for a put (with
    ``F = S·e^{rT}``, so ``F·e^{−rT} = S``). The DISCOUNTED parity — NOT the
    naive spot intrinsic: a deep-ITM PUT legitimately trades BELOW ``K − S``
    (its floor is ``K·e^{−rT} − S``), so screening on spot intrinsic would
    false-flag real premiums (caught by the stored-no-arb property test). A
    premium strictly below this floor has a NEGATIVE time value under the
    model and no IV root — stamped :data:`STATUS_BELOW_INTRINSIC`, never fed
    to the solver (plan §15.8 "below parity").
    """
    import math

    discounted_strike = strike * math.exp(-rate * t_years)
    floor = (spot - discounted_strike) if is_call else (discounted_strike - spot)
    return max(0.0, floor)


def enrich_option_bars(
    rows: Sequence[Mapping[str, object]],
    *,
    parts: FnoSymbolParts,
    spot_by_ts: Mapping[datetime, Decimal],
    rate_source: RiskFreeSource,
    style_flag: int,
) -> tuple[list[FnoGreeksBar], EnrichStats]:
    """Compute the Greeks overlay for one option contract's bars — pure core.

    Per bar (each *rows* mapping carries the 7 physical columns plus the
    nullable stored overlay): reuse the stored overlay ONLY when its stamp
    matches the current inputs with :data:`STATUS_OK` **and** its stored
    ``rate_used`` / ``spot_at_bar`` equal the values this pass would solve
    with — the spot compared after the 2dp ``round_money`` snap, i.e. the
    exact solve input — **and** its style flag matches (the value-hardened
    gate of DEC-GREEKS-CORRECTNESS-AUDIT-001 — a stamp cannot represent a
    rate/spot VALUE change under unchanged source kinds). Anything else recomputes:
    a coverage-shrunk rate CSV or a vanished spot minute honestly DEMOTES a
    previously-OK bar to :data:`STATUS_NO_RATE` / :data:`STATUS_NO_SPOT` on
    the recompute — never a frozen stale Greek. For a recompute, derive ``T``
    (Act/365 to 15:30 IST expiry), join the same-minute spot,
    look up the stamped rate, pre-screen the honest-NULL families
    (:data:`STATUS_AT_EXPIRY` / :data:`STATUS_NO_SPOT` / :data:`STATUS_NO_RATE`
    / :data:`STATUS_BELOW_INTRINSIC`), and only then solve
    ``compute_greeks(price=close, sigma=None, forward=S·e^{rT}, …)`` — an
    unsolvable tuple stamps :data:`STATUS_NO_SOLUTION`, a caller-contract
    reject (e.g. a zero-price bar) stamps :data:`STATUS_BAD_INPUT`; NEVER a
    fabricated value (plan §15.8).

    Returns the full bar list (reused + recomputed, ascending input order) and
    the :class:`EnrichStats` change tally — ``changed == 0`` means the target
    is byte-current and needs no write.
    """
    option_type = parts.option_type
    strike = parts.strike
    if option_type is None or strike is None:  # defensive: driver filters futures
        msg = f"enrich_option_bars called for a non-option symbol parts {parts!r}"
        raise ValueError(msg)
    is_call = option_type == "CE"
    strike_f = float(strike)
    expiry_at = expiry_instant_utc(parts.expiry)
    source_id = rate_source_id_for(rate_source)
    current_inputs = (
        pack_greeks_provenance(rate_source_id=source_id, style_flag=0, status=0) & _INPUTS_MASK
    )
    rate_by_date: dict[date, RiskFreeRate | None] = {}

    bars: list[FnoGreeksBar] = []
    solved = nulls = reused = changed = skipped = 0
    reasons: dict[int, int] = {}
    for row in rows:
        ts = utc_instant(row["timestamp"])  # type: ignore[arg-type]
        stored_overlay = _overlay_of_row(row)
        stored_pid = row.get("greeks_provenance_id")
        open_, high, low, close = _row_price_fields(row)
        if not all(p.is_finite() for p in (open_, high, low, close)):
            # A NaN / inf OHLC field cannot even build the honest-NULL bar
            # (FnoGreeksBar refuses non-finite prices), so the row is SKIPPED
            # — no bar, no write; the stored raw row stays untouched under the
            # latest-wins merge and the REST of the contract still enriches
            # (DEC-GREEKS-CORRECTNESS-AUDIT-001 IVX-2 — the old behaviour
            # quarantined the WHOLE contract on every pass).
            skipped += 1
            _LOG.warning(
                "greeks_enrich.bar_skipped_nonfinite",
                underlying=parts.underlying,
                expiry=parts.expiry.isoformat(),
                strike=str(strike),
                option_type=option_type,
                ts=ts.isoformat(),
            )
            continue
        volume = int(row["volume"])  # type: ignore[call-overload]
        raw_oi = row.get("open_interest")
        oi = None if raw_oi is None else int(raw_oi)  # type: ignore[call-overload]

        # Hoisted ABOVE the reuse gate (DEC-GREEKS-CORRECTNESS-AUDIT-001):
        # the gate value-checks the CURRENT rate + spot, so both lookups must
        # exist before the reuse decision. The explicit two-step memo replaces
        # the old ``setdefault`` form, whose default argument evaluated
        # ``rate_on`` eagerly on EVERY bar (RF-5 — the memo was dead code).
        bar_date = (ts + timedelta(hours=5, minutes=30)).date()
        if bar_date not in rate_by_date:
            rate_by_date[bar_date] = rate_source.rate_on(bar_date)
        rate = rate_by_date[bar_date]
        spot = spot_by_ts.get(ts)
        if spot is not None and (not spot.is_finite() or spot <= 0):
            # A non-finite / non-positive JOIN spot is unusable — degrade to
            # the honest NO_SPOT NULL (never a stored NaN spot, never a
            # whole-contract quarantine; DEC-GREEKS-CORRECTNESS-AUDIT-001
            # IVX-2's NaN-spot arm).
            _LOG.warning(
                "greeks_enrich.spot_invalid",
                underlying=parts.underlying,
                ts=ts.isoformat(),
                spot=str(spot),
            )
            spot = None

        if isinstance(stored_pid, int):
            stored = unpack_greeks_provenance(stored_pid)
            if (
                (stored_pid & _INPUTS_MASK) == current_inputs
                and stored.status == STATUS_OK
                and stored.style_flag == style_flag
                and rate is not None
                and _as_float(row.get("rate_used")) == rate.rate
                and spot is not None
                and _as_decimal(row.get("spot_at_bar")) == round_money(spot)
            ):
                # Already solved under the CURRENT inputs — restate verbatim.
                # The stamp bits alone are NOT sufficient: the spot 1-minute
                # lake is MUTABLE post DEC-LAKE-HEAL-001 (heal/quarantine/
                # re-derive rewrites), and a rate VALUE can move under an
                # unchanged source kind (a revised BRUTEX_RISK_FREE_CSV, a
                # re-configured fixed rate) with no stamp bit to show for it.
                # Reuse therefore additionally requires the stored
                # ``rate_used`` / ``spot_at_bar`` to EQUAL the values this
                # pass would solve with, and the style flag to match; any
                # mismatch falls through to the honest recompute below, which
                # re-solves or DEMOTES (NO_RATE / NO_SPOT) and lets the
                # latest-wins write heal the lake. The spot is compared
                # AFTER the 2dp ``round_money`` snap because the SNAPPED spot
                # IS the recompute's solve input (`_compute_bar` snaps first,
                # SPOTJOIN-3) — value-identical to recomputing, and a legacy
                # non-2dp spot partition keeps the O(reuse) fast path instead
                # of re-solving every bar on every pass (review fold).
                bars.append(
                    FnoGreeksBar(
                        timestamp=ts,
                        open=open_,
                        high=high,
                        low=low,
                        close=close,
                        volume=volume,
                        open_interest=oi,
                        iv=_as_float(row.get("iv")),
                        delta=_as_float(row.get("delta")),
                        gamma=_as_float(row.get("gamma")),
                        theta=_as_float(row.get("theta")),
                        vega=_as_float(row.get("vega")),
                        rho=_as_float(row.get("rho")),
                        spot_at_bar=_as_decimal(row.get("spot_at_bar")),
                        t_years_used=_as_float(row.get("t_years_used")),
                        rate_used=_as_float(row.get("rate_used")),
                        greeks_provenance_id=stored_pid,
                    )
                )
                reused += 1
                continue

        t_years = t_years_act365(ts, expiry_at)
        bar = _compute_bar(
            ts=ts,
            open_=open_,
            high=high,
            low=low,
            close=close,
            volume=volume,
            oi=oi,
            spot=spot,
            rate=rate,
            t_years=t_years,
            strike_f=strike_f,
            is_call=is_call,
            style_flag=style_flag,
            source_id=source_id,
        )
        bars.append(bar)
        status = unpack_greeks_provenance(bar.greeks_provenance_id or 0).status
        if status == STATUS_OK:
            solved += 1
        else:
            nulls += 1
            reasons[status] = reasons.get(status, 0) + 1
        if _overlay_of_bar(bar) != stored_overlay:
            changed += 1
    return bars, EnrichStats(
        solved=solved,
        nulls=nulls,
        reused=reused,
        changed=changed,
        skipped=skipped,
        null_reasons=tuple(sorted((_STATUS_NAMES[s], c) for s, c in reasons.items())),
    )


def _as_float(value: object) -> float | None:
    """A stored nullable Parquet numeric as ``float | None`` (read-back cast)."""
    return None if value is None else float(value)  # type: ignore[arg-type]


def _as_decimal(value: object) -> Decimal | None:
    """A stored nullable Parquet price as an exact ``Decimal | None``."""
    return None if value is None else Decimal(str(value))


def _null_bar(
    *,
    ts: datetime,
    open_: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    volume: int,
    oi: int | None,
    spot: Decimal | None,
    status: int,
    style_flag: int,
    source_id: int,
) -> FnoGreeksBar:
    """An honest stamped-NULL bar: no Greeks, no inputs, a packed reason.

    ``spot_at_bar`` is still materialised when the join DID supply a spot
    (e.g. a below-intrinsic bar has a real spot — only the solve is refused),
    so the stored row never claims less than what is known.
    """
    return FnoGreeksBar(
        timestamp=ts,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        open_interest=oi,
        spot_at_bar=spot,
        greeks_provenance_id=pack_greeks_provenance(
            rate_source_id=source_id, style_flag=style_flag, status=status
        ),
    )


def _compute_bar(
    *,
    ts: datetime,
    open_: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    volume: int,
    oi: int | None,
    spot: Decimal | None,
    rate: RiskFreeRate | None,
    t_years: float,
    strike_f: float,
    is_call: bool,
    style_flag: int,
    source_id: int,
) -> FnoGreeksBar:
    """Price one bar's overlay through the pre-screen + the shared kernel.

    The join spot is snapped to the 2-decimal tick grid ONCE here
    (:func:`~brutex.money.round_money`, ``ROUND_HALF_UP`` —
    DEC-GREEKS-CORRECTNESS-AUDIT-001 SPOTJOIN-3), so the stored
    ``spot_at_bar`` is byte-identical to what the lake writer's boundary snap
    would store and the changed-comparison converges even against a legacy
    non-2dp spot partition (pre-fix: a never-ending ``enriched=1`` rewrite
    loop). The snap applies to ``spot_at_bar``/the solve input ONLY — never
    to any Greek, ``iv``, ``t_years_used`` or ``rate_used``
    (DEC-MONEY-2DP-001 forbids rounding statistics). The kernel's admission
    verdict (``GreeksResult.admissible``) is deliberately NOT stored this
    round — it is exactly re-derivable downstream as ``abs(delta) >= 0.02
    and vega * 100 >= 1e-6`` over the stored columns.
    """
    import math

    if spot is not None:
        spot = round_money(spot)

    def _null(status: int, *, with_spot: Decimal | None) -> FnoGreeksBar:
        return _null_bar(
            ts=ts,
            open_=open_,
            high=high,
            low=low,
            close=close,
            volume=volume,
            oi=oi,
            spot=with_spot,
            status=status,
            style_flag=style_flag,
            source_id=source_id,
        )

    if rate is None:
        return _null(STATUS_NO_RATE, with_spot=spot)
    if t_years < T_FLOOR_YEARS:
        # At/after the expiry instant, or inside the kernel's 600 s trust
        # floor — the theta/gamma blow-up zone. Distinguishable from a solver
        # failure via the stamp (plan §15.8 "at expiry").
        return _null(STATUS_AT_EXPIRY, with_spot=spot)
    if spot is None:
        # No underlying spot minute — NEVER a carried-forward/next-bar spot.
        return _null(STATUS_NO_SPOT, with_spot=None)
    if close <= 0:
        # A finite zero/negative premium is a BAD INPUT regardless of
        # moneyness — checked BEFORE the parity floor so a deep-ITM zero
        # close no longer mislabels as BELOW_INTRINSIC
        # (DEC-GREEKS-CORRECTNESS-AUDIT-001 IVX-3; the NO_RATE / AT_EXPIRY /
        # NO_SPOT precedence above is unchanged).
        return _null(STATUS_BAD_INPUT, with_spot=spot)
    floor = _no_arb_floor(
        is_call=is_call, spot=float(spot), strike=strike_f, rate=rate.rate, t_years=t_years
    )
    if float(close) <= floor + DEGENERATE_TIME_VALUE_REL * float(close):
        # At/below the DISCOUNTED parity floor, or within the kernel's own
        # 1e-9-relative verification band of it: a negative time value has no
        # IV root, and a ~zero time value has NO DETERMINED root (the kernel
        # would "verify" an arbitrary sigma from a wide indeterminacy band —
        # the IVX-1 artifact). Never fed to the solver, never a spurious /
        # arbitrary root (plan §15.8; DEC-GREEKS-CORRECTNESS-AUDIT-001).
        return _null(STATUS_BELOW_INTRINSIC, with_spot=spot)
    forward = float(spot) * math.exp(rate.rate * t_years)
    try:
        result = compute_greeks(
            price=float(close),
            forward=forward,
            strike=strike_f,
            t_years=t_years,
            rate=rate,
            is_call=is_call,
        )
    except GreeksInputError:
        # A caller-contract reject (e.g. a zero-price stored bar) — stamped,
        # quarantined to this one bar, never a crash.
        return _null(STATUS_BAD_INPUT, with_spot=spot)
    if result is None:
        return _null(STATUS_NO_SOLUTION, with_spot=spot)
    return FnoGreeksBar(
        timestamp=ts,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        open_interest=oi,
        iv=result.iv,
        delta=result.delta,
        gamma=result.gamma,
        theta=result.theta,
        vega=result.vega,
        rho=result.rho,
        spot_at_bar=spot,
        t_years_used=t_years,
        rate_used=rate.rate,
        greeks_provenance_id=pack_greeks_provenance(
            rate_source_id=source_id,
            rate_version=result.rate_version,
            style_flag=style_flag,
            status=STATUS_OK,
        ),
    )


#: Back-compat private alias — the FIX-7 candidate ladder MOVED VERBATIM to its
#: COMMON pure home :func:`brutex.data.lake_keys.spot_recovery_candidates`
#: (DEC-DB-MONEYNESS-KEY-RECOVER-001) so the ``/db`` per-bar moneyness
#: spot-join shares ONE candidate builder with this enrichment (CLAUDE.md §11
#: COMMON — never copy-paste). The sourced alias map + the
#: ``BRUTEX_FNO_INDEX_ALIASES`` env override live there now
#: (``lake_keys.FNO_INDEX_ALIASES_ENV``); semantics are byte-identical.
_spot_recovery_candidates = spot_recovery_candidates


def read_spot_join_with_recovery(
    lake_root: Path, target: GreeksEnrichTarget, parts: FnoSymbolParts
) -> SpotJoin:
    """The DEFAULT spot reader: exact key first, then the drift-recovery ladder.

    Probes ``parts.underlying`` exactly (zero extra I/O and NO log on the hot
    path); ONLY when that join comes back EMPTY does it try the
    :func:`_spot_recovery_candidates` rungs in order, returning the FIRST
    candidate with any joined spot minute and emitting ONE
    ``greeks_enrich.spot_key_recovered`` info naming both keys
    (DEC-GREEKS-CORRECTNESS-AUDIT-001 SPOTJOIN-2 — pre-fix, a
    ``MIDCPNIFTY`` chain over a ``NIFTYMIDSELECT``-keyed spot dir stamped the
    WHOLE chain NO_SPOT + STYLE_UNKNOWN forever). The exercise style is
    stamped from the WINNING probe's segment (the returned
    :class:`~brutex.data.spot_join.SpotJoin` carries it). Ladder exhaustion
    returns the original empty join — the honest NO_SPOT, never a fabricated
    spot.
    """

    def _probe(underlying: str) -> SpotJoin:
        return spot_close_by_ts_window(
            lake_root,
            exchange=target.exchange,
            underlying=underlying,
            interval=_MINUTE_INTERVAL,
            start=target.start,
            end=target.end,
        )

    join = _probe(parts.underlying)
    if join.close_by_ts:
        return join
    for candidate in _spot_recovery_candidates(parts.underlying, target.exchange):
        recovered = _probe(candidate)
        if recovered.close_by_ts:
            _LOG.info(
                "greeks_enrich.spot_key_recovered",
                symbol=target.symbol,
                underlying=parts.underlying,
                recovered_key=candidate,
                segment=recovered.segment,
            )
            return recovered
    return join


def _style_for_segment(segment: str | None) -> int:
    """Map the spot-join probe segment to the packed exercise-style flag."""
    if segment == "INDEX":
        return STYLE_EUROPEAN_EXACT
    if segment == "CASH":
        return STYLE_AMERICAN_APPROX
    return STYLE_UNKNOWN


def read_option_rows(lake_root: Path, target: GreeksEnrichTarget) -> list[dict[str, object]]:
    """Read one option contract's stored 1-minute rows over the target window.

    Month-file-bounded (:func:`~brutex.data.spot_daily_resample.ist_span_partition_files`
    — O(pulled-window), never O(lake-depth)) and SCHEMA-TOLERANT per file: a
    legacy 7-column partition gains ``None``-filled overlay columns, an
    enriched 17-column partition passes its stored overlay through — so a
    mixed tree (e.g. a crash mid-enrich) can never break the pass. Each month
    file is additionally bounded to its OWN UTC ``(year, month)`` — the lake
    partitions by UTC month (``partition_for_timestamp``; the
    DEC-LAKE-HEAL-001 "foreign row" invariant), so a #911-class poisoned
    duplicate sitting in a FOREIGN month file can never win the keep-first
    dedup and be re-written into a canonical partition by the latest-wins
    enrich write (DEC-GREEKS-CORRECTNESS-AUDIT-001 G-RACE-1). Rows are
    filtered to the half-open IST date window ``[start, end)`` and sorted
    ascending.
    """
    import polars as pl

    base = lake_root / "bars" / target.exchange / FNO_CANDLE_LAKE_SEGMENT / target.symbol
    files = ist_span_partition_files(base / _MINUTE_INTERVAL, target.start, target.end)
    if not files:
        return []
    base_columns = ("timestamp", "open", "high", "low", "close", "volume", "open_interest")
    frames: list[pl.DataFrame] = []
    for file in files:
        df = pl.read_parquet(file)
        missing = [name for name in GREEKS_OVERLAY_FIELDS if name not in df.columns]
        if missing:
            df = df.with_columns(
                pl.lit(
                    None, dtype=pl.Int32 if name == "greeks_provenance_id" else pl.Float64
                ).alias(name)
                for name in missing
            )
        # Bound the file to its OWN UTC month — the path is constructed by
        # ist_span_partition_files as <base>/<yyyy>/<mm>.parquet, so the
        # file's canonical (year, month) is recoverable from it exactly.
        file_path = Path(file)
        year = int(file_path.parent.name)
        month = int(file_path.stem)
        df = df.filter(
            (pl.col("timestamp").dt.year() == year) & (pl.col("timestamp").dt.month() == month)
        )
        frames.append(df.select(*base_columns, *GREEKS_OVERLAY_FIELDS))
    merged = pl.concat(frames)
    # Keep-FIRST read dedup BEFORE the sort (DEC-LAKE-READ-UNIQUE-001): a
    # poisoned-era duplicated 1-minute row would otherwise be enriched twice and
    # re-written; the first stored row wins, warned, never silent.
    merged, _dropped = dedup_bars_frame(merged, source=f"greeks_enrich:{target.symbol}")
    ist_date = (pl.col("timestamp") + pl.duration(hours=5, minutes=30)).dt.date()
    bounded = (
        merged.with_columns(ist_date.alias("_ist_date"))
        .filter((pl.col("_ist_date") >= target.start) & (pl.col("_ist_date") < target.end))
        .drop("_ist_date")
        .sort("timestamp")
    )
    return list(bounded.iter_rows(named=True))


def write_option_greeks(
    lake_root: Path, target: GreeksEnrichTarget, bars: Sequence[FnoGreeksBar]
) -> int:
    """Write one contract's enriched bars via the shared ``latest-wins`` spine.

    The SAME :class:`~brutex.data.sink.ParquetBarSink` →
    :class:`~brutex.data.parquet_writer.ParquetWriter` partition-flock
    read-modify-write every lake writer uses (DEC-LAKE-PARTITION-FLOCK-001);
    ``merge_mode="latest-wins"`` is the enrich-overlay contract (plan §15.4):
    a corrected/re-derived overlay OVERWRITES the stale one, an identical
    restate is a byte-identical no-op, and the RAW OHLCV keep-first identity
    of the vendor spine is untouched (the writer's ``_bar_values_equal`` stays
    OHLCV+OI-only).

    Concurrency (DEC-GREEKS-CORRECTNESS-AUDIT-001 G-RACE-3 +
    DEC-GREEKS-PARALLEL-001): the flock serialises each write's
    read-modify-write, so this is safe against a concurrent keep-first VENDOR
    writer; two concurrent ENRICH passes are last-writer-wins (the
    single-ENRICH-PASS-per-lake execution model — the pass runs once per pull
    — stands). WITHIN one pass, the parallel driver's workers write DISJOINT
    contract symbols by construction (a symbol's every target rides one work
    unit), so intra-pass writes never contend on a partition and each
    partition's merge sequence is identical to the sequential pass.
    """
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    result = sink.append(
        exchange=target.exchange,
        segment=FNO_CANDLE_LAKE_SEGMENT,
        symbol=target.symbol,
        interval=_MINUTE_INTERVAL,
        bars=list(bars),
        merge_mode="latest-wins",
    )
    return result.bars_appended


def _chain_key(target: GreeksEnrichTarget) -> tuple[str, str, date, date]:
    """The target's spot-CHAIN coordinate — exactly the :class:`_SpotJoinMemo` key.

    ``(exchange, underlying, start, end)``: every CE/PE strike of one
    (underlying, window) chain shares this key (ONE spot read per chain). The
    underlying comes from :func:`~brutex.data.fno_symbol.parse_fno_symbol`;
    an undecodable symbol falls back to the raw symbol so the keying is total
    + deterministic (DEC-GREEKS-PARALLEL-001 unit assignment reuses it).
    """
    parts = parse_fno_symbol(target.symbol)
    underlying = parts.underlying if parts is not None else target.symbol
    return (target.exchange, underlying, target.start, target.end)


def _target_sort_key(target: GreeksEnrichTarget) -> tuple[str, str, date, date, str]:
    """Deterministic total order grouping targets that share one spot read.

    ``(exchange, underlying, start, end, symbol)`` — the leading four fields
    are EXACTLY the :class:`_SpotJoinMemo` key (:func:`_chain_key`), so after
    sorting every target sharing one spot window sits adjacent and the
    capacity-2 LRU reaches O(distinct keys) reads (registry order gives NO
    adjacency guarantee — the fetch phase's FUT-first market sort never
    reaches the enrich targets). An undecodable symbol falls back to the raw
    symbol (sorting by the raw symbol alone would interleave strike/expiry
    tokens lexically across one chain). The trailing ``symbol`` makes the
    order total + deterministic.
    """
    return (*_chain_key(target), target.symbol)


class _SpotJoinMemo:
    """Capacity-bounded LRU memo over the RESOLVED spot reader (DEC-GREEKS-SPOT-MEMO-001).

    Keyed by ``(target.exchange, parts.underlying, target.start, target.end)``
    — precisely the coordinates
    :func:`~brutex.data.spot_join.spot_close_by_ts_window` reads by — so every
    CE/PE strike of one (underlying, expiry) chain (all sharing the identical
    resolved window) costs ONE spot read instead of ~2×strikes byte-identical
    month-file scans. Wraps the RESOLVED reader (injected OR the default
    :func:`read_spot_join_with_recovery`) so the memo is seam-testable —
    memoising INCLUDES the DEC-GREEKS-CORRECTNESS-AUDIT-001 drift-recovery
    ladder, which is safe because the ladder is a deterministic function of
    exactly the memo key (``(exchange, underlying, start, end)`` — the env
    alias override is read per probe but stable within a pass), so a memoised
    recovered join is the byte-identical join a fresh probe would return.

    Correctness-safe by construction: the enrich pass writes ONLY FNO option
    partitions — nothing writes SPOT partitions mid-pass — and a
    :class:`~brutex.data.spot_join.SpotJoin` is consumed read-only, so a
    memoised instance is safely aliased across targets. A future IN-PASS spot
    writer must invalidate this memo (pinning that assumption here).

    Attributes
    ----------
    reads
        Spot window reads actually PERFORMED (memo misses; a RAISING read
        still counts — the attempt was made and nothing is cached, so the
        per-target quarantine stays honest).
    hits
        Reads AVOIDED by the memo.
    """

    __slots__ = ("_cache", "_capacity", "_reader", "hits", "reads")

    def __init__(
        self,
        reader: Callable[[GreeksEnrichTarget, FnoSymbolParts], SpotJoin],
        *,
        capacity: int = _SPOT_MEMO_CAPACITY,
    ) -> None:
        self._reader = reader
        self._capacity = capacity
        self._cache: OrderedDict[tuple[str, str, date, date], SpotJoin] = OrderedDict()
        self.reads = 0
        self.hits = 0

    def get(self, target: GreeksEnrichTarget, parts: FnoSymbolParts) -> SpotJoin:
        """The window's :class:`SpotJoin` — cached on a hit, read-then-cached on a miss."""
        key = (target.exchange, parts.underlying, target.start, target.end)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            self.hits += 1
            return cached
        self.reads += 1
        join = self._reader(target, parts)
        self._cache[key] = join
        if len(self._cache) > self._capacity:
            self._cache.popitem(last=False)
        return join


# ────────────────────────────────────────────────────────────────────────
# Parallel enrichment (DEC-GREEKS-PARALLEL-001)
# ────────────────────────────────────────────────────────────────────────

#: Environment variable overriding the AUTO Greeks worker count. ``"1"`` is
#: the operator KILL-SWITCH forcing the unchanged sequential path; unset /
#: blank resolves the AUTO count; an invalid value (non-integer / ``< 1``)
#: falls back to AUTO and is logged LOUDLY at WARNING (the
#: :func:`~brutex.engine.sweep_parallel.resolve_sweep_workers` invalid-env
#: convention, mirrored).
GREEKS_WORKERS_ENV: Final[str] = "BRUTEX_GREEKS_WORKERS"

#: AUTO worker-count cap + reserved CPUs — ``max(1, min(cpu_count - 2, 8))``,
#: the sweep pool's ADR-0016 envelope arithmetic mirrored (reserve 2 cores for
#: the rest of the machine per CLAUDE.md §8, cap at 8). HONEST (Golden Rule
#: #10): a LOCAL mirror, not a measured optimum for the IV-solve workload; no
#: shared hardware-envelope module exists on main today — when one lands, both
#: resolvers should repoint at it (named follow-up, CLAUDE.md §11 COMMON).
_GREEKS_WORKERS_CAP: Final[int] = 8
_GREEKS_WORKERS_RESERVED_CPUS: Final[int] = 2

#: Per-target outcome kinds — one per :class:`GreeksEnrichReport` tally bucket.
_KIND_ENRICHED: Final[str] = "enriched"
_KIND_ALREADY_CURRENT: Final[str] = "already_current"
_KIND_FUTURES_SKIPPED: Final[str] = "futures_skipped"
_KIND_UNDECODABLE: Final[str] = "undecodable"
_KIND_NO_BARS: Final[str] = "no_bars"
_KIND_ERRORED: Final[str] = "errored"


def greeks_pool_workers(cpu_count: int | None) -> int:
    """The AUTO Greeks worker count for *cpu_count* CPUs — pure + injectable.

    ``max(1, min(cpu_count - 2, 8))``, mirroring
    :func:`~brutex.engine.sweep_parallel.auto_sweep_workers` (the GIL-held
    Rust IV solve means a process pool is the only real parallelism — the
    ``trading_core`` kernel never detaches the GIL). A ``None`` *cpu_count*
    (``os.cpu_count()`` may return ``None`` on exotic platforms) resolves to
    1 — never guess parallelism on an undetectable machine. O(1).

    Parameters
    ----------
    cpu_count
        The detected CPU count, or ``None`` when undetectable.

    Returns
    -------
    int
        The auto worker count (``>= 1``).
    """
    if cpu_count is None:
        cpu_count = 1
    return max(1, min(cpu_count - _GREEKS_WORKERS_RESERVED_CPUS, _GREEKS_WORKERS_CAP))


def resolve_greeks_workers(
    env: Mapping[str, str] | None = None,
    *,
    cpu_count_fn: Callable[[], int | None] | None = None,
) -> int:
    """Resolve the Greeks-enrichment worker count from :data:`GREEKS_WORKERS_ENV`.

    * **unset / blank** → the AUTO count :func:`greeks_pool_workers`.
    * **explicit valid env ``>= 1``** → that value verbatim (``"1"`` is the
      operator kill-switch — the driver runs the unchanged sequential path).
    * **INVALID env (non-integer / ``< 1``)** → the AUTO count, logged LOUDLY
      at WARNING on ``greeks_enrich.workers_invalid`` naming the value via
      ``repr`` (the ``resolve_sweep_workers`` invalid-env convention). O(1).

    Parameters
    ----------
    env
        Environment mapping (defaults to :data:`os.environ`) — injectable.
    cpu_count_fn
        CPU-count probe (defaults to :func:`os.cpu_count`) — injectable.

    Returns
    -------
    int
        The resolved worker count (``>= 1``).
    """
    source = os.environ if env is None else env
    probe = os.cpu_count if cpu_count_fn is None else cpu_count_fn
    auto = greeks_pool_workers(probe())
    raw = source.get(GREEKS_WORKERS_ENV)
    if raw is None or not raw.strip():
        return auto
    try:
        parsed = int(raw)
    except ValueError:
        parsed = 0  # sentinel: falls through to the invalid branch below.
    if parsed < 1:
        _LOG.warning("greeks_enrich.workers_invalid", requested_env=repr(raw), fallback=auto)
        return auto
    return parsed


@dataclass(frozen=True, slots=True)
class _TargetOutcome:
    """One target's picklable verdict — the unit of parent-side fold + emit.

    Carries EVERYTHING the driver-level structlog events + the report
    counters need, as plain picklable data (``null_reasons`` stays the
    :class:`EnrichStats` tuple-of-pairs form; the emitter re-derives the
    event's dict). ``skipped`` is tallied for EVERY kind that reached
    :func:`enrich_option_bars` — including a target that then ERRORED in its
    write (the pre-refactor driver incremented ``bars_skipped`` before the
    write, preserved bit-for-bit).
    """

    kind: str
    symbol: str
    start: str
    end: str
    written: int = 0
    solved: int = 0
    nulls: int = 0
    reused: int = 0
    skipped: int = 0
    null_reasons: tuple[tuple[str, int], ...] = ()
    style: int = STYLE_UNKNOWN
    error: str | None = None


def _run_target(
    target: GreeksEnrichTarget,
    *,
    reader: Callable[[GreeksEnrichTarget], list[dict[str, object]]],
    writer: Callable[[GreeksEnrichTarget, Sequence[FnoGreeksBar]], int],
    memo: _SpotJoinMemo,
    rate_source: RiskFreeSource,
    reraise: tuple[type[BaseException], ...] = (LakeWriteHalt,),
) -> _TargetOutcome:
    """ONE target's read→join→solve→write body — shared by BOTH drivers.

    The single source of truth (CLAUDE.md §11 COMMON) for the per-target
    pipeline: the sequential driver and the pool worker call THIS, so the two
    paths cannot drift. Exception policy is the caller's via *reraise*:

    * the sequential driver keeps the default ``(LakeWriteHalt,)`` — the
      CTR-2 whole-pass abort (DEC-GREEKS-CORRECTNESS-AUDIT-001), every other
      ``Exception`` (including ``MemoryError``, the pre-refactor behaviour)
      quarantines to an ``errored`` outcome;
    * the pool worker passes ``(LakeWriteHalt, MemoryError)`` — a raw
      worker-side OOM must tear the pool down, never blend into a quiet
      per-target quarantine (the sweep-worker convention).
    """
    start = target.start.isoformat()
    end = target.end.isoformat()
    skipped_seen = 0
    try:
        parts = parse_fno_symbol(target.symbol)
        if parts is None:
            return _TargetOutcome(
                kind=_KIND_UNDECODABLE, symbol=target.symbol, start=start, end=end
            )
        if parts.option_type is None or parts.strike is None:
            return _TargetOutcome(
                kind=_KIND_FUTURES_SKIPPED, symbol=target.symbol, start=start, end=end
            )
        rows = reader(target)
        if not rows:
            return _TargetOutcome(kind=_KIND_NO_BARS, symbol=target.symbol, start=start, end=end)
        join = memo.get(target, parts)
        style = _style_for_segment(join.segment)
        bars, stats = enrich_option_bars(
            rows,
            parts=parts,
            spot_by_ts=join.close_by_ts,
            rate_source=rate_source,
            style_flag=style,
        )
        skipped_seen = stats.skipped
        if stats.changed == 0:
            return _TargetOutcome(
                kind=_KIND_ALREADY_CURRENT,
                symbol=target.symbol,
                start=start,
                end=end,
                skipped=skipped_seen,
            )
        written = writer(target, bars)
        return _TargetOutcome(
            kind=_KIND_ENRICHED,
            symbol=target.symbol,
            start=start,
            end=end,
            written=written,
            solved=stats.solved,
            nulls=stats.nulls,
            reused=stats.reused,
            skipped=skipped_seen,
            null_reasons=stats.null_reasons,
            style=style,
        )
    except reraise:
        # A pull-wide global stop (disk full / OOM) fails EVERY remaining
        # write identically — re-raised BEFORE the per-target quarantine so
        # the pass never keeps solving + write-hammering N-1 doomed contracts
        # (DEC-GREEKS-CORRECTNESS-AUDIT-001 CTR-2). Since
        # DEC-GREEKS-PARALLEL-001 the `_enrich_greeks_from_minute` wrapper
        # RE-RAISES `LakeWriteHalt` + `GreeksEnrichPoolBroken` (only those —
        # every other enrich error stays fenced), so a genuine host halt
        # ABORTS the pull and fires the CRITICAL Telegram pull-halt alert.
        raise
    except Exception as exc:
        return _TargetOutcome(
            kind=_KIND_ERRORED,
            symbol=target.symbol,
            start=start,
            end=end,
            skipped=skipped_seen,
            error=str(exc),
        )


def _emit_target_event(outcome: _TargetOutcome) -> None:
    """Emit the driver-level structlog event for one folded outcome.

    The SAME three events (name + payload, byte-for-byte) the pre-refactor
    sequential loop emitted inline; the parallel driver relays them
    PARENT-side in the deterministic fold order. A ``futures_skipped`` /
    ``no_bars`` / ``already_current`` outcome emits nothing (as before).
    """
    if outcome.kind == _KIND_ENRICHED:
        _LOG.info(
            "greeks_enrich.target_enriched",
            symbol=outcome.symbol,
            start=outcome.start,
            end=outcome.end,
            bars=outcome.written,
            solved=outcome.solved,
            nulls=outcome.nulls,
            null_reasons=dict(outcome.null_reasons),
            reused=outcome.reused,
            style=outcome.style,
        )
    elif outcome.kind == _KIND_UNDECODABLE:
        _LOG.warning("greeks_enrich.target_undecodable", symbol=outcome.symbol)
    elif outcome.kind == _KIND_ERRORED:
        _LOG.warning(
            "greeks_enrich.target_errored",
            symbol=outcome.symbol,
            start=outcome.start,
            end=outcome.end,
            error=outcome.error,
        )


@dataclass(slots=True)
class _PassCounters:
    """Mutable fold accumulator — one per pass, parent-side only."""

    enriched: int = 0
    already_current: int = 0
    futures_skipped: int = 0
    undecodable: int = 0
    no_bars: int = 0
    errored: int = 0
    bars_written: int = 0
    bars_solved: int = 0
    bars_null: int = 0
    bars_reused: int = 0
    bars_skipped: int = 0

    def fold(self, outcome: _TargetOutcome) -> None:
        """Fold one outcome — deterministic, order = the sorted target order."""
        self.bars_skipped += outcome.skipped
        if outcome.kind == _KIND_ENRICHED:
            self.enriched += 1
            self.bars_written += outcome.written
            self.bars_solved += outcome.solved
            self.bars_null += outcome.nulls
            self.bars_reused += outcome.reused
        elif outcome.kind == _KIND_ALREADY_CURRENT:
            self.already_current += 1
        elif outcome.kind == _KIND_FUTURES_SKIPPED:
            self.futures_skipped += 1
        elif outcome.kind == _KIND_UNDECODABLE:
            self.undecodable += 1
        elif outcome.kind == _KIND_NO_BARS:
            self.no_bars += 1
        else:
            self.errored += 1

    def report(self, *, spot_reads: int, spot_hits: int) -> GreeksEnrichReport:
        """The immutable pass report from the folded tallies."""
        return GreeksEnrichReport(
            enriched=self.enriched,
            already_current=self.already_current,
            futures_skipped=self.futures_skipped,
            undecodable=self.undecodable,
            no_bars=self.no_bars,
            errored=self.errored,
            bars_written=self.bars_written,
            bars_solved=self.bars_solved,
            bars_null=self.bars_null,
            bars_reused=self.bars_reused,
            spot_reads=spot_reads,
            spot_hits=spot_hits,
            bars_skipped=self.bars_skipped,
        )


@dataclass(frozen=True, slots=True)
class _GreeksUnit:
    """One picklable work unit — a spot CHAIN (or a merged same-underlying block).

    ``index`` is the unit's position in the SEQUENTIAL sorted order — the
    parent folds results in ascending index so the driver events + counters
    are IDENTICAL to the sequential pass, not merely equivalent.
    """

    index: int
    targets: tuple[GreeksEnrichTarget, ...]


@dataclass(frozen=True, slots=True)
class _GreeksUnitResult:
    """One unit's picklable result — plain data only, no exception crosses.

    ``halt_reason`` is the :class:`~brutex.data.errors.LakeWriteHalt` sentinel
    (the unit stopped IMMEDIATELY; the parent re-raises) — a domain exception
    riding the executor result channel would risk the unpickling-failure →
    whole-pool-poisoning class of bug, so failures cross as data by design.
    """

    index: int
    outcomes: tuple[_TargetOutcome, ...]
    halt_reason: str | None
    spot_reads: int
    spot_hits: int


def _build_units(ordered: Sequence[GreeksEnrichTarget]) -> list[_GreeksUnit]:
    """Group the SORTED targets into parallel work units — chain-grained.

    The unit of dispatch is the spot-memo CHAIN :func:`_chain_key`
    ``(exchange, underlying, start, end)`` — every unit owns whole chains, so
    a fresh per-unit :class:`_SpotJoinMemo` performs exactly one read per
    distinct chain and the summed ``spot_reads`` / ``spot_hits`` equal the
    sequential pass (in sorted order a chain key never recurs after its
    contiguous block, so the sequential capacity-2 LRU never re-reads an
    evicted key either — both sides count distinct chains).

    SYMBOL AFFINITY guard: when one symbol appears under MORE than one chain
    (possible only via the public API — the production caller maps each
    symbol to at most one window), ALL chains of that ``(exchange,
    underlying)`` merge into ONE unit processed in sorted order, so a
    symbol's writes are always time-ordered inside a single worker and the
    stored bytes stay byte-identical to sequential. Units keep the sorted
    order via ``index``.
    """
    chains: OrderedDict[tuple[str, str, date, date], list[GreeksEnrichTarget]] = OrderedDict()
    first_chain_of_symbol: dict[str, tuple[str, str, date, date]] = {}
    merged_groups: set[tuple[str, str]] = set()
    for target in ordered:
        key = _chain_key(target)
        chains.setdefault(key, []).append(target)
        prior = first_chain_of_symbol.setdefault(target.symbol, key)
        if prior != key:
            merged_groups.add((key[0], key[1]))
    units: list[_GreeksUnit] = []
    for group, group_chains in groupby(chains.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        if group in merged_groups:
            merged: list[GreeksEnrichTarget] = []
            for _key, chain in group_chains:
                merged.extend(chain)
            units.append(_GreeksUnit(index=len(units), targets=tuple(merged)))
        else:
            for _key, chain in group_chains:
                units.append(_GreeksUnit(index=len(units), targets=tuple(chain)))
    return units


def _default_seams(
    lake_root: Path,
) -> tuple[
    Callable[[GreeksEnrichTarget], list[dict[str, object]]],
    Callable[[GreeksEnrichTarget, FnoSymbolParts], SpotJoin],
    Callable[[GreeksEnrichTarget, Sequence[FnoGreeksBar]], int],
]:
    """The production (reader, spot-reader, writer) triple over *lake_root*.

    ONE binding site (CLAUDE.md §11 COMMON) used by the sequential driver's
    defaults AND rebuilt INSIDE each spawn worker from the picklable context
    — the closures themselves never cross the process boundary.
    """
    return (
        lambda t: read_option_rows(lake_root, t),
        lambda t, parts: read_spot_join_with_recovery(lake_root, t, parts),
        lambda t, b: write_option_greeks(lake_root, t, b),
    )


@dataclass(frozen=True, slots=True)
class _GreeksWorkerContext:
    """Immutable picklable per-pool context handed to workers via the initializer."""

    lake_root: Path
    rate_source: RiskFreeSource


#: The per-worker-PROCESS context, set ONCE by :func:`_greeks_worker_init`. A
#: module global by necessity — a ``ProcessPoolExecutor`` initializer cannot
#: return a value (the sweep-pool convention, ``sweep_parallel._WORKER_CTX``).
_GREEKS_WORKER_CTX: _GreeksWorkerContext | None = None


def _greeks_worker_init(ctx: _GreeksWorkerContext) -> None:
    """Pool ``initializer``: pin single-threaded numerics + stash the context.

    OMP / RAYON / POLARS pinned to 1 so N workers never oversubscribe into
    N × num_cpus nested threads (the ``sweep_parallel._worker_init``
    determinism pin, mirrored). ``polars`` is lazily imported by the readers
    AFTER this runs, so ``POLARS_MAX_THREADS`` lands in time;
    ``trading_core`` is imported at module-import (before this), but its
    rayon pools initialise lazily and the crate has no detach/parallel
    sections — the RAYON pin is kept for convention-consistency, not claimed
    to precede the import.
    """
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["RAYON_NUM_THREADS"] = "1"
    os.environ["POLARS_MAX_THREADS"] = "1"
    global _GREEKS_WORKER_CTX  # noqa: PLW0603 — set-once per worker process
    _GREEKS_WORKER_CTX = ctx


def _run_unit(
    unit: _GreeksUnit, *, lake_root: Path, rate_source: RiskFreeSource
) -> _GreeksUnitResult:
    """Process one unit over the DEFAULT seams — the worker body, in-process testable.

    Per-target failures quarantine to ``errored`` outcomes (never kill the
    pool); a :class:`~brutex.data.errors.LakeWriteHalt` stops the unit
    IMMEDIATELY and crosses as the ``halt_reason`` sentinel; a raw
    ``MemoryError`` propagates (pool teardown — the honest OOM signal). The
    per-unit :class:`_SpotJoinMemo` counters ride the result and sum to the
    sequential totals (see :func:`_build_units`).
    """
    reader, spot_reader, writer = _default_seams(lake_root)
    memo = _SpotJoinMemo(spot_reader)
    outcomes: list[_TargetOutcome] = []
    halt_reason: str | None = None
    for target in unit.targets:
        try:
            outcome = _run_target(
                target,
                reader=reader,
                writer=writer,
                memo=memo,
                rate_source=rate_source,
                reraise=(LakeWriteHalt, MemoryError),
            )
        except LakeWriteHalt as halt:
            halt_reason = halt.reason
            break
        outcomes.append(outcome)
    return _GreeksUnitResult(
        index=unit.index,
        outcomes=tuple(outcomes),
        halt_reason=halt_reason,
        spot_reads=memo.reads,
        spot_hits=memo.hits,
    )


def _greeks_worker(unit: _GreeksUnit) -> _GreeksUnitResult:
    """The module-level pool task: bind the initializer's context, run the unit."""
    ctx = _GREEKS_WORKER_CTX
    if ctx is None:  # pragma: no cover — the initializer always runs before a task
        msg = "greeks worker context was not initialised"
        raise RuntimeError(msg)
    return _run_unit(unit, lake_root=ctx.lake_root, rate_source=ctx.rate_source)


def _parallel_pickle_ok(lake_root: Path, rate_source: RiskFreeSource) -> bool:
    """True iff the worker context can cross the spawn boundary — probe, never crash.

    ``rate_source`` is only Protocol-typed: the production sources
    (:class:`~brutex.options.risk_free.FixedRateSource` /
    :class:`~brutex.options.risk_free.CsvRateSource`) are frozen picklable
    dataclasses, but a caller-injected fake (a closure, a mock) would blow up
    pool startup with an opaque pickling error — probed here instead, WARNed,
    and gracefully degraded to the sequential path.
    """
    try:
        pickle.dumps((lake_root, rate_source))
    except (pickle.PicklingError, TypeError, AttributeError):
        _LOG.warning(
            "greeks_enrich.parallel_disabled",
            reason="context_unpicklable",
            rate_source_type=type(rate_source).__name__,
        )
        return False
    return True


def _run_parallel(
    units: Sequence[_GreeksUnit],
    *,
    lake_root: Path,
    rate_source: RiskFreeSource,
    max_workers: int,
) -> GreeksEnrichReport:
    """The pool driver: dispatch units, fold results in sequential order.

    Liveness (DEC-PIPELINE-STALL-HEARTBEAT-001): the parent beats once before
    dispatch and once per COMPLETED unit — real observed progress only, never
    a timer keepalive that would blind the watchdog to a genuinely hung
    worker. A unit is one option chain (bounded — minutes, well under the
    600 s watchdog), so beat silence is bounded by the largest single unit.

    Failure model: a unit's ``halt_reason`` (LakeWriteHalt in a worker)
    cancels every QUEUED unit and re-raises ``LakeWriteHalt(reason)`` —
    HONEST envelope: units already RUNNING in sibling workers finish first
    (``shutdown(wait=True)``; a spawned process cannot be safely killed
    mid-write), so the parallel halt is not instantaneous like sequential
    CTR-2 — on a full disk each in-flight worker hits its own halt quickly.
    A :class:`BrokenProcessPool` (a worker died ABRUPTLY — OOM SIGKILL,
    external kill, hard interpreter crash: NOT distinguishable through the
    public executor API) raises
    :class:`~brutex.data.errors.GreeksEnrichPoolBroken`, which the dashboard
    job layer recognises as a pull-halt (CRITICAL Telegram alert).
    """
    ctx = _GreeksWorkerContext(lake_root=lake_root, rate_source=rate_source)
    pool_size = min(max_workers, len(units))
    _LOG.info("greeks_enrich.parallel_start", units=len(units), workers=pool_size)
    emit_progress_beat()
    results: dict[int, _GreeksUnitResult] = {}
    try:
        with ProcessPoolExecutor(
            max_workers=pool_size,
            # SPAWN, never FORK (the Linux default): the parent runs threads
            # (watchdog daemons) + holds flock/logging locks, and a
            # fork-after-threads inherits those locks in an arbitrary state —
            # a classic child deadlock (the sweep_parallel precedent).
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_greeks_worker_init,
            initargs=(ctx,),
        ) as pool:
            futures = [pool.submit(_greeks_worker, unit) for unit in units]
            for future in as_completed(futures):
                emit_progress_beat()
                result = future.result()
                results[result.index] = result
                if result.halt_reason is not None:
                    pool.shutdown(wait=True, cancel_futures=True)
                    raise LakeWriteHalt(result.halt_reason)
    except BrokenProcessPool as exc:
        detail = (
            "a worker process died before completing its unit — an OOM "
            "SIGKILL, an external kill or a hard interpreter crash are NOT "
            "distinguishable from BrokenProcessPool"
        )
        _LOG.error(
            "greeks_enrich.pool_broken",
            workers=pool_size,
            units_total=len(units),
            units_done=len(results),
            error=str(exc),
        )
        raise GreeksEnrichPoolBroken(detail) from exc
    counters = _PassCounters()
    spot_reads = spot_hits = 0
    for unit in units:
        result = results[unit.index]
        spot_reads += result.spot_reads
        spot_hits += result.spot_hits
        for outcome in result.outcomes:
            _emit_target_event(outcome)
            counters.fold(outcome)
    return counters.report(spot_reads=spot_reads, spot_hits=spot_hits)


def enrich_greeks_for_targets(
    *,
    targets: Iterable[GreeksEnrichTarget],
    lake_root: Path,
    rate_source: RiskFreeSource,
    read_bars: Callable[[GreeksEnrichTarget], list[dict[str, object]]] | None = None,
    read_spot: Callable[[GreeksEnrichTarget, FnoSymbolParts], SpotJoin] | None = None,
    write_bars: Callable[[GreeksEnrichTarget, Sequence[FnoGreeksBar]], int] | None = None,
    workers: int | None = None,
) -> GreeksEnrichReport:
    """Enrich every option target's stored bars with Greeks — the pass driver.

    The F&O Greeks twin of
    :func:`~brutex.data.spot_daily_resample.derive_daily_for_targets`: each
    target is QUARANTINED (a per-contract read/solve/write error increments
    ``errored``, logs ``greeks_enrich.target_errored`` and continues — one bad
    contract never sinks the pass, plan §15.4); a FUT / undecodable target is
    tallied and skipped (never a bogus Greek for a future); an
    ``already_current`` target (every bar stamp-matched, nothing changed) is
    skipped with NO write, so an immediate re-run is a byte-identical no-op
    (Golden Rule #7). All lake I/O is injectable for tests; the defaults are
    the real window-bounded readers/writer.

    Processing order (DEC-GREEKS-SPOT-MEMO-001): targets are enriched in the
    deterministic sorted order ``(exchange, underlying, start, end, symbol)``
    — NOT caller order — so targets sharing one spot window are adjacent and
    the capacity-2 :class:`_SpotJoinMemo` over the RESOLVED ``read_spot``
    (injected OR default) collapses a chain's ~2×strikes identical spot scans
    to one read per distinct ``(exchange, underlying, window)`` key. HONEST
    envelope: spot I/O is O(distinct (exchange, underlying, window)) reads per
    pass, NOT O(1) total — per-expiry windows are distinct keys by design; the
    performed/avoided counts are reported as ``spot_reads`` / ``spot_hits``.
    ``read_spot`` must therefore be a pure function of ``(target.exchange,
    parts.underlying, target.start, target.end)`` — the memo key — never of
    the individual contract symbol.

    Parallel execution (DEC-GREEKS-PARALLEL-001): with DEFAULT seams, a
    resolved worker count > 1 (*workers* param, else
    :data:`GREEKS_WORKERS_ENV`, else auto) and ≥ 2 work units, the pass fans
    the chain-grained units over a SPAWN ``ProcessPoolExecutor`` (the Rust IV
    solve holds the GIL, so processes are the only real parallelism). The
    STORED bytes, the report (every counter incl. ``spot_reads`` /
    ``spot_hits``) and the driver-level event sequence are IDENTICAL to the
    sequential pass (golden-pinned); the DEEP seam logs fire child-side (see
    the module docstring). FORCED sequential — honestly, never silently
    broken: ``workers=1`` / ``BRUTEX_GREEKS_WORKERS=1`` (the kill-switch), ANY
    injected seam (test closures are unpicklable; the seam contract is
    unchanged), fewer than 2 units, or an unpicklable *rate_source* (WARNed).

    Durability / crash-resumability: each partition write is the atomic
    flock-guarded tmp→fsync→``os.replace`` spine, so a crash mid-pass leaves
    every partition either fully old or fully new (never corrupt); the next
    pull's pass re-enriches the un-stamped remainder incrementally.
    """
    ordered = sorted(targets, key=_target_sort_key)
    if read_bars is None and read_spot is None and write_bars is None:
        resolved = resolve_greeks_workers() if workers is None else max(1, workers)
        if resolved > 1:
            units = _build_units(ordered)
            if len(units) > 1 and _parallel_pickle_ok(lake_root, rate_source):
                return _run_parallel(
                    units,
                    lake_root=lake_root,
                    rate_source=rate_source,
                    max_workers=resolved,
                )
    default_reader, default_spot_reader, default_writer = _default_seams(lake_root)
    reader = read_bars if read_bars is not None else default_reader
    spot_reader = read_spot if read_spot is not None else default_spot_reader
    writer = write_bars if write_bars is not None else default_writer

    memo = _SpotJoinMemo(spot_reader)
    counters = _PassCounters()
    for target in ordered:
        # Liveness beat per contract (DEC-PIPELINE-STALL-HEARTBEAT-001): the Greeks
        # enrichment pass runs minutes-to-hours AFTER the last fetch heartbeat, so
        # without this the pull stall watchdog false-declared a HEALTHY pipeline HUNG
        # (forensically proven). No-op outside a dashboard pull; best-effort; O(1).
        emit_progress_beat()
        outcome = _run_target(
            target, reader=reader, writer=writer, memo=memo, rate_source=rate_source
        )
        _emit_target_event(outcome)
        counters.fold(outcome)
    return counters.report(spot_reads=memo.reads, spot_hits=memo.hits)
