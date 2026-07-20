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
pool: the parallel driver's spawn workers write DISJOINT lake partitions by
construction (a partition path is exchange+symbol-scoped, and all targets of
one ``(exchange, underlying)`` whose symbols could collide share ONE work
unit, processed in sorted order inside one worker — one symbol string listed
under TWO exchanges rides two units, but those are two disjoint partitions),
so intra-pass workers never contend on a partition and the stored bytes are
IDENTICAL to the sequential pass (golden-pinned). The driver's own structlog
events are emitted PARENT-side in the deterministic sorted fold order; the
liveness beats fire once pre-dispatch then per COMPLETED unit
(completion order);
the DEEP seam logs (read-dedup warnings, ``spot_key_recovered``) fire inside
per-target processing and therefore emit CHILD-side in parallel mode —
honest: log parity is driver-events-only, never claimed for the deep seams.

Rust batch arm (`DEC-GREEKS-RUST-BATCH-001`, default OFF): the env flag
``BRUTEX_GREEKS_RUST_BATCH`` (tristate — truthy ``1/true/yes/on`` enables,
falsy ``0/false/no/off`` disables, unset/blank/unrecognised falls to the
default OFF, junk warned once per process) routes the screened-SOLVABLE rows
of :func:`enrich_option_bars` through ONE ``trading_core.greeks_batch``
FFI call per contract (chunk-capped at :data:`GREEKS_BATCH_MAX_ROWS`)
instead of N scalar calls. Every screen / gate / Decimal / repr / ``exp``
operation stays in the ONE shared Python copy (:func:`_screen_bar` /
:func:`_finish_bar`), so both arms are bit-identical by construction; a
16-element per-process burn-in bit-compares the batch outcomes against the
per-row oracle (:func:`_solve_one`) and any mismatch or batch-path exception
SHIPS THE ORACLE VALUES and latches the batch OFF for the process
(``greeks_enrich.rust_batch_autoreverted`` ERROR — never a silently wrong
bar). The flag is resolved per CALL from the process env, so spawn workers
read their own inherited environment; the OFF (default) arm is byte- and
log-identical to the pre-batch code. GIL envelope (honest): the ON arm's
FFI call deliberately holds the GIL for one whole ≤65,536-row chunk (no
release — sub-second at the cap), so an in-process signal/thread is
deferred until the chunk returns where the per-row arm yields between rows;
watchdog beats are unaffected either way — they fire per TARGET/unit, never
per row.
"""

from __future__ import annotations

import multiprocessing
import os
import pickle  # nosec B403 — dumps-only picklability probe; never loads untrusted data
import struct
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from datetime import UTC, date, datetime, timezone
from decimal import Decimal
from itertools import groupby
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

import structlog

from brutex.calendars.nse import IST_FIXED_OFFSET, IST_UTC_OFFSET
from brutex.data.errors import GreeksEnrichPoolBroken, LakeWriteHalt
from brutex.data.fno_candle_lake import FNO_CANDLE_LAKE_SEGMENT
from brutex.data.fno_symbol import parse_fno_symbol
from brutex.data.greeks_provenance import (
    GREEKS_FORMULA_VERSION,
    INPUTS_MASK,
    IV_SOURCE_SERVED,
    IV_SOURCE_SOLVED,
    Q_BIAS_ZERO,
    RATE_SOURCE_ID_CSV,
    RATE_SOURCE_ID_FIXED,
    RATE_SOURCE_ID_NONE,
    STATUS_AT_EXPIRY,
    STATUS_BAD_INPUT,
    STATUS_BELOW_INTRINSIC,
    STATUS_NAMES,
    STATUS_NO_RATE,
    STATUS_NO_SOLUTION,
    STATUS_NO_SPOT,
    STATUS_OK,
    STYLE_AMERICAN_APPROX,
    STYLE_EUROPEAN_EXACT,
    STYLE_UNKNOWN,
    TTE_ACT365_EXPIRY_1530_IST,
    GreeksProvenance,
    pack_greeks_provenance,
    unpack_greeks_provenance,
)
from brutex.data.guarded_read import read_parquet_regular
from brutex.data.lake_keys import spot_recovery_candidates
from brutex.data.lake_read import dedup_bars_frame
from brutex.data.models import GREEKS_OVERLAY_FIELDS, FnoGreeksBar
from brutex.data.session import ist_offset_duration
from brutex.data.spot_daily_resample import ist_span_partition_files
from brutex.data.spot_drift import load_stock_drift_map
from brutex.data.spot_join import SpotJoin, spot_close_by_ts_window, utc_instant
from brutex.engine.sweep_lookahead import env_flag_tristate
from brutex.money import round_money
from brutex.observability.progress_beat import emit_progress_beat
from brutex.options.greeks import (
    IV_CLAMP_MAX,
    IV_CLAMP_MIN,
    T_FLOOR_YEARS,
    GreeksBatchOutcome,
    GreeksInputError,
    GreeksSolveRequest,
    compute_greeks,
    compute_greeks_batch,
)
from brutex.options.risk_free import (
    CsvRateSource,
    FixedRateSource,
    RiskFreeRate,
    RiskFreeSource,
    load_risk_free_csv,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from brutex.data.fno_symbol import FnoSymbolParts
    from brutex.data.lake_keys import StockDriftMap

_LOG = structlog.get_logger("brutex.data.greeks_enrich")

__all__ = [
    "DEGENERATE_TIME_VALUE_REL",
    "EXPIRY_HOUR_IST",
    "EXPIRY_MINUTE_IST",
    "GREEKS_BATCH_MAX_ROWS",
    "GREEKS_FORMULA_VERSION",
    "GREEKS_RUST_BATCH_DEFAULT",
    "GREEKS_RUST_BATCH_ENV",
    "GREEKS_WORKERS_ENV",
    "IV_SOURCE_SERVED",
    "IV_SOURCE_SOLVED",
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
    "compute_bar_overlay",
    "current_inputs_pack",
    "enrich_greeks_for_targets",
    "enrich_option_bars",
    "expiry_instant_utc",
    "greeks_pool_workers",
    "greeks_rust_batch_enabled",
    "pack_greeks_provenance",
    "rate_source_id_for",
    "read_spot_join_with_recovery",
    "reset_rust_batch_for_tests",
    "resolve_greeks_workers",
    "resolve_risk_free_source",
    "served_iv_usable",
    "stamp_is_current_inputs",
    "t_years_act365",
    "unpack_greeks_provenance",
]

# The packed-provenance vocabulary (status/style/rate/q/tte constants, the bit
# layout, ``GreeksProvenance`` + pack/unpack) MOVED VERBATIM to the COMMON
# wheel-free :mod:`brutex.data.greeks_provenance` (DEC-DB-FNO-DISPLAY-TRUTH-001;
# the #1581 ``symbol_form_preference``/``contracts`` re-export precedent) so the
# ``/db`` viewer decodes stored stamps without this module's ``trading_core``
# wheel import chain. Every name is re-imported above — SAME objects,
# byte-compatible; ``__all__`` unchanged. The two module-private spellings the
# body (and history) use are aliased here:
_STATUS_NAMES: Final = STATUS_NAMES
_INPUTS_MASK: Final = INPUTS_MASK

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

#: Fixed +05:30 IST (no DST) — the same offset the vendor parser uses; the ONE
#: canonical :data:`brutex.calendars.nse.IST_FIXED_OFFSET` root
#: (DEC-TIMESTAMP-IST-SINGLESOURCE-001).
_IST: Final[timezone] = IST_FIXED_OFFSET

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

#: Env flag selecting the Rust columnar batch arm of
#: :func:`enrich_option_bars` (`DEC-GREEKS-RUST-BATCH-001`). Tristate via the
#: COMMON :func:`brutex.engine.sweep_lookahead.env_flag_tristate` vocabulary
#: (imported, never copied — CLAUDE.md §11): truthy → ON, falsy → OFF,
#: unset/blank → the default, non-blank junk → the default + ONE warn per
#: process (``greeks_enrich.rust_batch_flag_unrecognised``). Resolved per
#: CALL from the process env so spawn workers read their own inherited
#: environment (the ``rust_metrics`` stance; zero ``_GreeksWorkerContext``
#: change).
GREEKS_RUST_BATCH_ENV: Final[str] = "BRUTEX_GREEKS_RUST_BATCH"

#: The batch arm ships INERT — flipping the default needs its own DEC/ADR
#: with measured evidence (the ``SWEEP_RUST_METRICS_DEFAULT`` precedent).
GREEKS_RUST_BATCH_DEFAULT: Final[bool] = False

#: Python-side chunk cap on one ``trading_core.greeks_batch`` crossing —
#: bounds the FFI allocation envelope deterministically (~4.6 MiB of element
#: tuples at the cap; an alloc failure under ``panic="abort"`` would abort,
#: the same disclosed residual as every existing FFI path).
GREEKS_BATCH_MAX_ROWS: Final[int] = 65_536

#: Burn-in budget: the first N batch-served elements per PROCESS are ALSO
#: solved on the per-row oracle and bit-compared (the ``rust_metrics``
#: ``SELF_CHECK_SAMPLES`` twin).
_SELF_CHECK_SAMPLES: Final[int] = 16


@dataclass(slots=True)
class _RustBatchState:
    """Process-local batch-arm state — latch + burn-in budget + warn-once memos.

    A documented exception to the no-global-mutable-state rule (the
    ``brutex.engine.rust_metrics`` latch precedent): the autorevert latch
    MUST outlive one call so a poisoned wheel is consulted at most once per
    process, and the warn/engage memos exist precisely to bound log volume.
    Plain writes, NO lock — races are OUTPUT-SAFE (the rust_metrics clause):
    a racing thread can at worst run a few extra burn-in self-checks or
    double-fire a once-only log line, NEVER produce a wrong bar (every
    served element is batch≡oracle-verified or the oracle value itself).
    The state is process-LOCAL and the house parallelism is spawn
    PROCESSES (each worker owns its own copy); no production threadpool
    wraps the enrich pass.
    """

    disabled: bool = False
    self_check_left: int = _SELF_CHECK_SAMPLES
    engaged: bool = False
    flag_warned: bool = False


_RUST_BATCH_STATE: Final[_RustBatchState] = _RustBatchState()


def reset_rust_batch_for_tests() -> None:
    """Restore all four batch-arm state fields (test isolation seam)."""
    _RUST_BATCH_STATE.disabled = False
    _RUST_BATCH_STATE.self_check_left = _SELF_CHECK_SAMPLES
    _RUST_BATCH_STATE.engaged = False
    _RUST_BATCH_STATE.flag_warned = False


def greeks_rust_batch_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Resolve the ``BRUTEX_GREEKS_RUST_BATCH`` tristate (default OFF).

    Truthy literals enable, falsy literals disable, unset/blank falls to the
    default silently, and non-blank junk falls to the default with ONE
    ``greeks_enrich.rust_batch_flag_unrecognised`` WARNING per process (the
    raw value repr'd) — under default-OFF the junk fallback is already
    fail-safe.
    """
    mapping: Mapping[str, str] = os.environ if env is None else env
    raw = mapping.get(GREEKS_RUST_BATCH_ENV, "")
    verdict = env_flag_tristate(raw)
    if verdict is not None:
        return verdict
    if raw.strip() and not _RUST_BATCH_STATE.flag_warned:
        _RUST_BATCH_STATE.flag_warned = True
        _LOG.warning("greeks_enrich.rust_batch_flag_unrecognised", value=repr(raw))
    return GREEKS_RUST_BATCH_DEFAULT


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


def current_inputs_pack(rate_source: RiskFreeSource) -> int:
    """The INPUT-defining stamp bits the CURRENT pass packs — ONE source of truth.

    ``formula_version + rate_source_id + rate_version + q_bias + tte_convention``
    (bits 0-19, the :data:`_INPUTS_MASK` subset) packed exactly as the
    :func:`enrich_option_bars` reuse gate packs them, so the scanner-side
    staleness predicate (:func:`stamp_is_current_inputs`) and the driver's
    recompute decision can NEVER drift (CLAUDE.md §11 COMMON;
    DEC-GREEKS-BULK-REENRICH-001). O(1) — two constant-time packs.

    Parameters
    ----------
    rate_source
        The resolved risk-free source whose packed id keys the stamp.

    Returns
    -------
    int
        The masked input bits a CURRENT-pass OK stamp carries.
    """
    source_id = rate_source_id_for(rate_source)
    return pack_greeks_provenance(rate_source_id=source_id, style_flag=0, status=0) & _INPUTS_MASK


def stamp_is_current_inputs(pid: int | None, *, rate_source: RiskFreeSource) -> bool:
    """``True`` iff *pid* carries the CURRENT model-input stamp bits.

    The bulk-backfill scanner's staleness predicate (DEC-GREEKS-BULK-REENRICH-001):
    ``None`` (an unstamped legacy bar) is NEVER current; otherwise the
    input-defining bits (:func:`current_inputs_pack` — formula version + rate
    source id + rate version + q bias + TTE convention) must equal the bits
    this pass would stamp. A ``False`` here is a SAFE over-approximation of
    the driver's per-bar reuse gate (:func:`enrich_option_bars` additionally
    value-checks ``rate_used`` / ``spot_at_bar`` / style / status), so a
    scanner-stale contract is always re-fed to the driver, which then reuses
    or recomputes per bar — never the other way round. HONEST blind spot
    (Golden Rule #10): a rate/spot VALUE drift under unchanged source kinds is
    stamp-invisible by construction — the backfill's ``--force`` mode routes
    every contract through the driver's value-checked gate, which catches
    exactly that class. O(1) — two int ops after one constant pack.

    Parameters
    ----------
    pid
        A stored ``greeks_provenance_id`` (or ``None`` when unstamped).
    rate_source
        The resolved risk-free source the current pass would stamp with.

    Returns
    -------
    bool
        Whether the stamp's input bits match the current pass's inputs.
    """
    if pid is None:
        return False
    return (pid & _INPUTS_MASK) == current_inputs_pack(rate_source)


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
    current_inputs = current_inputs_pack(rate_source)
    rate_by_date: dict[date, RiskFreeRate | None] = {}

    batch_on = greeks_rust_batch_enabled() and not _RUST_BATCH_STATE.disabled
    out: list[FnoGreeksBar | _PendingSolve] = []
    pending_meta: list[tuple[int, _PendingSolve, tuple[object, ...]]] = []
    solved = nulls = reused = changed = skipped = 0
    reasons: dict[int, int] = {}

    def _account(bar: FnoGreeksBar, stored_overlay: tuple[object, ...]) -> None:
        """Fold one recomputed bar into the tallies — order-invariant sums.

        Extracted so the per-row and batch arms share ONE accounting copy
        (`DEC-GREEKS-RUST-BATCH-001`): every counter is a plain sum and
        ``reasons`` folds name-sorted at return, so deferring a batch
        element's accounting to the fill-in pass is observationally
        invisible. Reused bars never flow through here (they cannot count
        ``changed`` — unchanged historical contract).
        """
        nonlocal solved, nulls, changed
        status = unpack_greeks_provenance(bar.greeks_provenance_id or 0).status
        if status == STATUS_OK:
            solved += 1
        else:
            nulls += 1
            reasons[status] = reasons.get(status, 0) + 1
        if _overlay_of_bar(bar) != stored_overlay:
            changed += 1

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
        bar_date = (ts + IST_UTC_OFFSET).date()
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
                # IS the recompute's solve input (`_screen_bar` snaps first,
                # SPOTJOIN-3) — value-identical to recomputing, and a legacy
                # non-2dp spot partition keeps the O(reuse) fast path instead
                # of re-solving every bar on every pass (review fold).
                out.append(
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
        screened = _screen_bar(
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
        if batch_on and isinstance(screened, _PendingSolve):
            # ON arm: defer the kernel solve — the placeholder keeps the
            # bar's index, the meta carries what the fill-in pass needs.
            out.append(screened)
            pending_meta.append((len(out) - 1, screened, stored_overlay))
            continue
        bar = (
            screened
            if isinstance(screened, FnoGreeksBar)
            else _finish_bar(screened, _solve_one(screened))
        )
        out.append(bar)
        _account(bar, stored_overlay)
    if pending_meta:
        outcomes = _batch_solve_outcomes([p for _, p, _ in pending_meta])
        for slot, (i, p, overlay) in enumerate(pending_meta):
            # An absent slot means the batch autoreverted — solve THAT row
            # on the per-row oracle (identical bytes by construction).
            outcome = outcomes.get(slot)
            filled = _finish_bar(p, outcome if outcome is not None else _solve_one(p))
            out[i] = filled
            _account(filled, overlay)
    bars = cast("list[FnoGreeksBar]", out)  # every slot filled by construction:
    # placeholders exist only in pending_meta, and the fill-in pass replaced
    # each one — no runtime re-check (an isinstance sweep would be an
    # uncoverable arm); TestFillInCompleteness type-checks every element.
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


@dataclass(frozen=True, slots=True)
class _PendingSolve:
    """One screened-SOLVABLE bar awaiting its kernel solve.

    Produced by :func:`_screen_bar` when every honest-NULL pre-screen passed
    (`DEC-GREEKS-RUST-BATCH-001` — the shared-single-copy seam split): the
    fields are exactly what :func:`_solve_one` needs to run today's
    ``compute_greeks`` call and what :func:`_finish_bar` needs to build the
    final bar. ``spot`` is the 2dp-SNAPPED join spot and ``rate`` the
    resolved :class:`~brutex.options.risk_free.RiskFreeRate` — both non-None
    by construction (their None screens returned a NULL bar instead);
    ``forward`` is the Python-computed ``float(spot)·e^{rate·T}`` (the
    ``math.exp`` stays Python-side on BOTH arms — no exp is ever computed in
    Rust that Python didn't already compute).
    """

    ts: datetime
    open_: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    oi: int | None
    spot: Decimal
    rate: RiskFreeRate
    t_years: float
    strike_f: float
    is_call: bool
    style_flag: int
    source_id: int
    forward: float


def served_iv_usable(served_iv: float | None) -> bool:
    """``True`` iff a vendor-served IV can price the Greeks DIRECTLY (no solve).

    The ONE predicate (CLAUDE.md §11 COMMON) both :func:`_compute_bar`'s served
    branch and the Dhan rolling reuse gate consult, so the stamped
    :data:`~brutex.data.greeks_provenance.IV_SOURCE_SERVED` regime and the
    idempotency compare can NEVER drift (DEC-DHAN-ROLLING-OPTIONS-001 §3). A
    served IV is usable iff it is present, finite, and inside the kernel's own
    ``[IV_CLAMP_MIN, IV_CLAMP_MAX]`` clamp — a garbage / out-of-band served IV
    falls through to the honest solve-from-premium path instead (never a
    fabricated Greek from a nonsense vol). Groww never supplies a served IV, so
    this is always ``False`` there and the solve path is byte-identical.
    """
    import math

    return (
        served_iv is not None
        and math.isfinite(served_iv)
        and IV_CLAMP_MIN <= served_iv <= IV_CLAMP_MAX
    )


def _screen_bar(
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
    apply_parity_screen: bool = True,
) -> FnoGreeksBar | _PendingSolve:
    """Run the honest-NULL pre-screens for one bar — the pre-solve half.

    The VERBATIM pre-screen body of the historical ``_compute_bar``
    (`DEC-GREEKS-RUST-BATCH-001` split — one shared copy serves the per-row
    AND the batch arm, so the status ladder can never drift between them):
    the join spot is snapped to the 2-decimal tick grid ONCE here
    (:func:`~brutex.money.round_money`, ``ROUND_HALF_UP`` —
    DEC-GREEKS-CORRECTNESS-AUDIT-001 SPOTJOIN-3), so the stored
    ``spot_at_bar`` is byte-identical to what the lake writer's boundary snap
    would store and the changed-comparison converges even against a legacy
    non-2dp spot partition (pre-fix: a never-ending ``enriched=1`` rewrite
    loop). The snap applies to ``spot_at_bar``/the solve input ONLY — never
    to any Greek, ``iv``, ``t_years_used`` or ``rate_used``
    (DEC-MONEY-2DP-001 forbids rounding statistics).

    ``apply_parity_screen`` (default ``True`` — every historical caller is
    byte-identical) exists for the served-IV recomposition seam
    (`DEC-DHAN-ROLLING-OPTIONS-001` §3 via :func:`_compute_bar`): the
    below-parity screen exists solely to protect the IV SOLVER from an
    indeterminate root, so the direct served-IV branch — which never
    consumes the traded premium — skips it (``False`` skips the floor
    computation + the BELOW_INTRINSIC refusal, nothing else).

    Returns a stamped honest-NULL :class:`FnoGreeksBar` when any screen
    refuses, else the :class:`_PendingSolve` carrying everything the solve
    + finish steps need.
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
    if apply_parity_screen:
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
    return _PendingSolve(
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
        forward=forward,
    )


def _solve_one(p: _PendingSolve) -> GreeksBatchOutcome:
    """Solve one pending bar through today's scalar kernel call — the oracle arm.

    The VERBATIM solve try/except of the historical ``_compute_bar``,
    returning the outcome as :class:`~brutex.options.greeks.GreeksBatchOutcome`
    DATA (so the per-row and batch arms share ONE finish step).
    """
    try:
        result = compute_greeks(
            price=float(p.close),
            forward=p.forward,
            strike=p.strike_f,
            t_years=p.t_years,
            rate=p.rate,
            is_call=p.is_call,
        )
    except GreeksInputError as exc:
        # A caller-contract reject (e.g. a zero-price stored bar) — stamped,
        # quarantined to this one bar, never a crash.
        return GreeksBatchOutcome(result=None, invalid_input=str(exc))
    return GreeksBatchOutcome(result=result, invalid_input=None)


def _finish_bar(
    p: _PendingSolve,
    outcome: GreeksBatchOutcome,
    *,
    iv_source: int = IV_SOURCE_SOLVED,
) -> FnoGreeksBar:
    """Build the final bar from a pending solve's outcome — the post-solve half.

    The VERBATIM tail of the historical ``_compute_bar``: an
    ``invalid_input`` reject stamps :data:`STATUS_BAD_INPUT`, an honest
    no-solution stamps :data:`STATUS_NO_SOLUTION`, and a solved result builds
    the OK bar with full-precision Greeks + the packed provenance. The
    kernel's admission verdict (``GreeksResult.admissible``) is deliberately
    NOT stored this round — it is exactly re-derivable downstream as
    ``abs(delta) >= 0.02 and vega * 100 >= 1e-6`` over the stored columns.
    ``iv_source`` (default :data:`~brutex.data.greeks_provenance.IV_SOURCE_SOLVED`
    — every historical stamp byte-identical) marks a served-IV-priced OK bar
    :data:`~brutex.data.greeks_provenance.IV_SOURCE_SERVED`
    (DEC-DHAN-ROLLING-OPTIONS-001 §3 via :func:`_compute_bar`); the honest-NULL
    arms never consume it (a null bar's stamp keeps the pack default).
    """
    if outcome.invalid_input is not None:
        return _null_bar(
            ts=p.ts,
            open_=p.open_,
            high=p.high,
            low=p.low,
            close=p.close,
            volume=p.volume,
            oi=p.oi,
            spot=p.spot,
            status=STATUS_BAD_INPUT,
            style_flag=p.style_flag,
            source_id=p.source_id,
        )
    result = outcome.result
    if result is None:
        return _null_bar(
            ts=p.ts,
            open_=p.open_,
            high=p.high,
            low=p.low,
            close=p.close,
            volume=p.volume,
            oi=p.oi,
            spot=p.spot,
            status=STATUS_NO_SOLUTION,
            style_flag=p.style_flag,
            source_id=p.source_id,
        )
    return FnoGreeksBar(
        timestamp=p.ts,
        open=p.open_,
        high=p.high,
        low=p.low,
        close=p.close,
        volume=p.volume,
        open_interest=p.oi,
        iv=result.iv,
        delta=result.delta,
        gamma=result.gamma,
        theta=result.theta,
        vega=result.vega,
        rho=result.rho,
        spot_at_bar=p.spot,
        t_years_used=p.t_years,
        rate_used=p.rate.rate,
        greeks_provenance_id=pack_greeks_provenance(
            rate_source_id=p.source_id,
            rate_version=result.rate_version,
            style_flag=p.style_flag,
            status=STATUS_OK,
            iv_source=iv_source,
        ),
    )


def _outcome_key(outcome: GreeksBatchOutcome) -> tuple[object, ...]:
    """Bit-exact burn-in compare key for one solve outcome.

    Shape verdicts first (which of the three legal outcome families), then —
    for a solved result — the seven floats as their IEEE-754 bit patterns
    (``struct.pack``, signed zeros included — the ``drawdown_compare_key``
    idiom), the admission verdict and the rate provenance stamp.
    """
    key: tuple[object, ...] = (outcome.invalid_input is not None, outcome.result is None)
    result = outcome.result
    if result is not None:
        key = (
            *key,
            struct.pack(
                "<7d",
                result.iv,
                result.delta,
                result.gamma,
                result.theta,
                result.vega,
                result.rho,
                result.price,
            ),
            result.admissible,
            result.rate_source,
            result.rate_version,
        )
    return key


def _batch_solve_outcomes(pending: list[_PendingSolve]) -> dict[int, GreeksBatchOutcome]:
    """Solve the pending rows through ONE columnar FFI crossing per chunk.

    The ON-arm dispatch of `DEC-GREEKS-RUST-BATCH-001`: builds the request
    columns, routes them through :func:`compute_greeks_batch` in
    :data:`GREEKS_BATCH_MAX_ROWS` chunks, burn-in bit-compares the first
    :data:`_SELF_CHECK_SAMPLES` elements per process against the per-row
    oracle (:func:`_solve_one`), and returns ``{slot: outcome}``.

    Fault posture: an EMPTY dict means "batch results discarded — solve every
    pending row on the oracle" (the caller's fill-in does exactly that).
    Any batch-path exception EXCEPT ``MemoryError`` (which re-raises raw —
    house law) latches the batch OFF for the process with one
    ``greeks_enrich.rust_batch_autoreverted`` ERROR; a burn-in mismatch does
    the same, so the oracle value ships for the WHOLE call and — because the
    screens already ran once in pass 1 — no per-row WARN is ever duplicated.
    """
    requests = [
        GreeksSolveRequest(
            price=float(p.close),
            forward=p.forward,
            strike=p.strike_f,
            t_years=p.t_years,
            rate=p.rate,
            is_call=p.is_call,
        )
        for p in pending
    ]
    try:
        outcomes: list[GreeksBatchOutcome] = []
        for start in range(0, len(requests), GREEKS_BATCH_MAX_ROWS):
            outcomes.extend(compute_greeks_batch(requests[start : start + GREEKS_BATCH_MAX_ROWS]))
    except MemoryError:
        raise  # house law: MemoryError re-raises raw (never absorbed)
    except Exception as exc:  # the autorevert fence: ANY batch-path
        # fault (a stale wheel's AttributeError, an FFI marshal fault, a
        # length-mismatch ValueError) must ship the oracle values, never
        # a crashed pass or a silently wrong bar.
        _RUST_BATCH_STATE.disabled = True
        _LOG.error(
            "greeks_enrich.rust_batch_autoreverted",
            reason="exception",
            # repr() the message per the house log-injection convention
            # (the lake_heal precedent) — no lake string reaches this fence
            # today; forward-looking belt-and-braces.
            error=repr(str(exc)),
            error_type=type(exc).__name__,
        )
        return {}
    check_n = min(_RUST_BATCH_STATE.self_check_left, len(outcomes))
    for slot in range(check_n):  # burn-in BEFORE adoption
        if _outcome_key(outcomes[slot]) != _outcome_key(_solve_one(pending[slot])):
            _RUST_BATCH_STATE.disabled = True
            _LOG.error(
                "greeks_enrich.rust_batch_autoreverted",
                reason="self_check_mismatch",
                slot=slot,
            )
            return {}
    _RUST_BATCH_STATE.self_check_left -= check_n
    if not _RUST_BATCH_STATE.engaged:
        _RUST_BATCH_STATE.engaged = True
        _LOG.info("greeks_enrich.rust_batch_engaged")
    return dict(enumerate(outcomes))


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
    served_iv: float | None = None,
) -> FnoGreeksBar:
    """Price one bar's overlay through the pre-screen + the shared kernel.

    The RECOMPOSED per-bar face over the ONE shared :func:`_screen_bar` /
    :func:`_solve_one` / :func:`_finish_bar` copies (the
    `DEC-GREEKS-RUST-BATCH-001` split) — this wrapper only COMPOSES them, it
    never re-implements a screen or a solve, so the status ladder can never
    drift between the Groww enrich loop and this seam.

    ``served_iv`` (DEC-DHAN-ROLLING-OPTIONS-001 §3) is the ONE cross-cutting
    served-vs-solved seam: when a USABLE vendor IV is supplied
    (:func:`served_iv_usable` — present, finite, in-clamp), the Greeks are
    priced DIRECTLY from ``(spot, strike, sigma, T, rate)`` through the SAME
    shared kernel (no root-solve, no copied math) and stamped
    :data:`~brutex.data.greeks_provenance.IV_SOURCE_SERVED`. Absent / unusable
    (the Groww default ``None``) → the byte-identical solve-from-premium path
    (``_finish_bar(p, _solve_one(p))``), stamped
    :data:`~brutex.data.greeks_provenance.IV_SOURCE_SOLVED`
    (``pack_greeks_provenance``'s default, so a Groww stamp is unchanged). The
    shared pre-screens (NO_RATE / AT_EXPIRY / NO_SPOT / BAD_INPUT-on-``close``)
    apply to BOTH branches; the below-parity screen is solve-branch-only (it
    exists solely to protect the IV solver — ``apply_parity_screen``).
    """
    served_ok = served_iv_usable(served_iv)
    screened = _screen_bar(
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
        apply_parity_screen=not served_ok,
    )
    if not isinstance(screened, _PendingSolve):
        return screened
    if served_ok:
        # ── served-IV branch (Dhan rolling-ATM, DEC-DHAN-ROLLING-OPTIONS-001
        # §3) ── A vendor-served IV prices the six Greeks DIRECTLY from
        # (spot, strike, sigma, T, rate) through the SAME shared Rust kernel —
        # `compute_greeks(sigma=served_iv, ...)` is the kernel's native
        # price-from-sigma path (NO root-solve, NO copied math). The traded
        # premium is not consumed here, so the below-parity screen (which
        # exists only to protect the SOLVER from an indeterminate root) was
        # deliberately skipped above: the Greeks are well-defined for any
        # in-clamp vol. Groww never supplies a served IV, so this branch is
        # unreachable there and the solve path below is byte-identical.
        try:
            served = compute_greeks(
                sigma=served_iv,
                forward=screened.forward,
                strike=screened.strike_f,
                t_years=screened.t_years,
                rate=screened.rate,
                is_call=screened.is_call,
            )
        except GreeksInputError as exc:
            # A caller-contract reject the served_iv clamp cannot screen — a
            # non-positive / non-finite strike or forward (a corrupt
            # resolved_strike). Stamped BAD_INPUT, quarantined to this bar.
            return _finish_bar(
                screened, GreeksBatchOutcome(result=None, invalid_input=str(exc))
            )
        # A ``None`` here is defensive: the kernel's price-from-sigma path
        # returns a value for every in-clamp sigma (no root to fail) — only a
        # stubbed kernel exercises it; ``_finish_bar`` stamps the honest
        # NO_SOLUTION so a future kernel that CAN degrade never fabricates a
        # Greek.
        return _finish_bar(
            screened,
            GreeksBatchOutcome(result=served, invalid_input=None),
            iv_source=IV_SOURCE_SERVED,
        )
    return _finish_bar(screened, _solve_one(screened))


#: Public seam alias for the per-bar overlay pricer (DEC-DHAN-ROLLING-OPTIONS-001
#: §3): the Dhan rolling-ATM Greeks pass reuses the SAME pre-screen + kernel core
#: — passing ``served_iv`` to take the direct served-IV branch — so there is ONE
#: source of truth for the Greeks math (CLAUDE.md §11 COMMON; the kept private
#: name serves the Groww call sites in this module unchanged).
compute_bar_overlay = _compute_bar


#: Back-compat private alias — the FIX-7 candidate ladder MOVED VERBATIM to its
#: COMMON pure home :func:`brutex.data.lake_keys.spot_recovery_candidates`
#: (DEC-DB-MONEYNESS-KEY-RECOVER-001) so the ``/db`` per-bar moneyness
#: spot-join shares ONE candidate builder with this enrichment (CLAUDE.md §11
#: COMMON — never copy-paste). The sourced alias map + the
#: ``BRUTEX_FNO_INDEX_ALIASES`` env override live there now
#: (``lake_keys.FNO_INDEX_ALIASES_ENV``); semantics are byte-identical.
_spot_recovery_candidates = spot_recovery_candidates


def read_spot_join_with_recovery(
    lake_root: Path,
    target: GreeksEnrichTarget,
    parts: FnoSymbolParts,
    *,
    stock_drift: StockDriftMap | None = None,
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

    *stock_drift* (DEC-GREEKS-SPOTJOIN-STOCK-001) is the per-pass
    evidence-grounded stock drift map (:mod:`brutex.data.spot_drift`); its
    candidates ride the SAME ladder walk as the APPENDED last rung, so
    ``None``/empty is byte-identical to the pre-stock reader. The one recovery
    log gains an additive ``rung`` field — ``"stock_drift"`` when the winning
    candidate came from the map, else ``"alias_ladder"`` — attributed via the
    pinned prefix invariant (the stock rung can only APPEND, so indices past
    the 2-arg ladder length are uniquely stock-sourced).
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
    candidates = _spot_recovery_candidates(
        parts.underlying, target.exchange, stock_drift=stock_drift
    )
    alias_count = len(candidates)
    if stock_drift:
        # The extra 2-arg call runs only on the already-cold exhausted-primary
        # path with a truthy map — pure string ops, O(1)-bounded.
        alias_count = len(_spot_recovery_candidates(parts.underlying, target.exchange))
    for idx, candidate in enumerate(candidates):
        recovered = _probe(candidate)
        if recovered.close_by_ts:
            _LOG.info(
                "greeks_enrich.spot_key_recovered",
                symbol=target.symbol,
                underlying=parts.underlying,
                recovered_key=candidate,
                segment=recovered.segment,
                rung="stock_drift" if idx >= alias_count else "alias_ladder",
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
        # Guarded literal read: *file* is ONE concrete month path
        # (DEC-SPOT-DERIVE-TS-RANGE-HARDENING-001, the #1594 round-15 metachar
        # law — held by construction inside read_parquet_regular — a
        # `[`-carrying contract dir must never glob-read its siblings' bars;
        # + DEC-LAKE-READ-INODE-GUARD-001 fd validation).
        df = read_parquet_regular(file)
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
    ist_date = (pl.col("timestamp") + ist_offset_duration()).dt.date()
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
    alias override is read per probe but stable within a pass, and the
    ``stock_drift`` map (DEC-GREEKS-SPOTJOIN-STOCK-001) is built ONCE in
    :func:`enrich_greeks_for_targets` before any target is processed and never
    mutated, so it too is a per-pass constant), so a memoised recovered join
    is the byte-identical join a fresh probe would return.

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

    Since DEC-HW-ENVELOPE-CONSOLIDATE-001 this DELEGATES to the COMMON
    :func:`brutex.config.hardware_envelope.cpu_pool_workers`
    (``reserve=_GREEKS_WORKERS_RESERVED_CPUS``, ``cap=_GREEKS_WORKERS_CAP``,
    sized off the logical count with ``prefer_performance=False`` and an
    empty env so no override applies) — the ONE sizing formula (CLAUDE.md
    §11), the EXACT ``auto_sweep_workers`` delegation shape — with a
    byte-unchanged result: ``min(max(1, n - 2), 8) == max(1, min(n - 2, 8))``
    for the cap ``8 >= 1`` (regression-pinned across cpu counts in
    ``tests/unit/test_greeks_enrich_parallel.py``).

    Parameters
    ----------
    cpu_count
        The detected CPU count, or ``None`` when undetectable.

    Returns
    -------
    int
        The auto worker count (``>= 1``).
    """
    from brutex.config.hardware_envelope import cpu_pool_workers, envelope_from

    logical = cpu_count if cpu_count is not None and cpu_count >= 1 else 1
    return cpu_pool_workers(
        {},
        reserve=_GREEKS_WORKERS_RESERVED_CPUS,
        cap=_GREEKS_WORKERS_CAP,
        prefer_performance=False,
        envelope=envelope_from({"logical_cores": logical}),
    )


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
      worker-side OOM must abort the WHOLE pass, never blend into a quiet
      per-target quarantine (the sweep-worker convention). Honest chain: a
      PICKLABLE ``MemoryError`` does NOT break the pool — the executor
      pickles it back and it re-raises RAW in the parent (remaining queued
      units still run: ``Executor.__exit__`` is a plain ``shutdown(wait=True)``
      with no ``cancel_futures``), landing in the pull wrapper's blanket
      fence as ``greeks_enrich_ok=False`` (the ``greeks_health`` failure
      counter observes it; NO CRITICAL alert). Only a hard OOM SIGKILL of
      the worker process yields ``BrokenProcessPool`` →
      :class:`~brutex.data.errors.GreeksEnrichPoolBroken` → the CRITICAL
      pull-halt alert.
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
    *,
    stock_drift: StockDriftMap | None = None,
) -> tuple[
    Callable[[GreeksEnrichTarget], list[dict[str, object]]],
    Callable[[GreeksEnrichTarget, FnoSymbolParts], SpotJoin],
    Callable[[GreeksEnrichTarget, Sequence[FnoGreeksBar]], int],
]:
    """The production (reader, spot-reader, writer) triple over *lake_root*.

    ONE binding site (CLAUDE.md §11 COMMON) used by the sequential driver's
    defaults AND rebuilt INSIDE each spawn worker from the picklable context
    — the closures themselves never cross the process boundary. The default
    spot reader binds the per-pass *stock_drift* map
    (DEC-GREEKS-SPOTJOIN-STOCK-001; ``None`` → byte-identical ladder).
    """
    return (
        lambda t: read_option_rows(lake_root, t),
        lambda t, parts: read_spot_join_with_recovery(lake_root, t, parts, stock_drift=stock_drift),
        lambda t, b: write_option_greeks(lake_root, t, b),
    )


@dataclass(frozen=True, slots=True)
class _GreeksWorkerContext:
    """Immutable picklable per-pool context handed to workers via the initializer.

    ``stock_drift`` (DEC-GREEKS-SPOTJOIN-STOCK-001) is the parent-built
    evidence-grounded drift map — a PLAIN ``dict`` of tuples (treat as frozen;
    never a ``MappingProxyType``, which is unpicklable and would silently
    degrade every production run to sequential via the ``_parallel_pickle_ok``
    rail). Declared LAST with a default so existing keyword constructions stay
    valid; workers consume it verbatim and NEVER scan (no per-worker DuckDB
    open), so sequential ≡ parallel bytes by construction.
    """

    lake_root: Path
    rate_source: RiskFreeSource
    stock_drift: dict[tuple[str, str], tuple[str, ...]] | None = None


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
    unit: _GreeksUnit,
    *,
    lake_root: Path,
    rate_source: RiskFreeSource,
    stock_drift: StockDriftMap | None = None,
) -> _GreeksUnitResult:
    """Process one unit over the DEFAULT seams — the worker body, in-process testable.

    Per-target failures quarantine to ``errored`` outcomes (never kill the
    pool); a :class:`~brutex.data.errors.LakeWriteHalt` stops the unit
    IMMEDIATELY and crosses as the ``halt_reason`` sentinel; a raw
    ``MemoryError`` propagates (whole-pass abort — the honest OOM signal;
    see :func:`_run_target` for the exact parent-side chain). The
    per-unit :class:`_SpotJoinMemo` counters ride the result and sum to the
    sequential totals (see :func:`_build_units`). *stock_drift* is the
    parent-built per-pass drift map (DEC-GREEKS-SPOTJOIN-STOCK-001) bound into
    the default spot reader — a worker never scans.
    """
    reader, spot_reader, writer = _default_seams(lake_root, stock_drift=stock_drift)
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
    if ctx is None:
        msg = "greeks worker context was not initialised"
        raise RuntimeError(msg)
    return _run_unit(
        unit,
        lake_root=ctx.lake_root,
        rate_source=ctx.rate_source,
        stock_drift=ctx.stock_drift,
    )


def _parallel_pickle_ok(
    lake_root: Path,
    rate_source: RiskFreeSource,
    stock_drift: StockDriftMap | None = None,
) -> bool:
    """True iff the worker context can cross the spawn boundary — probe, never crash.

    ``rate_source`` is only Protocol-typed: the production sources
    (:class:`~brutex.options.risk_free.FixedRateSource` /
    :class:`~brutex.options.risk_free.CsvRateSource`) are frozen picklable
    dataclasses, but a caller-injected fake (a closure, a mock) would blow up
    pool startup with an opaque pickling error — probed here instead, WARNed,
    and gracefully degraded to the sequential path. ``stock_drift`` rides the
    probe tuple too (the production loader emits a plain picklable ``dict``;
    the probe keeps the rail honest against any exotic ``Mapping``).
    """
    try:
        pickle.dumps((lake_root, rate_source, stock_drift))
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
    stock_drift: dict[tuple[str, str], tuple[str, ...]] | None = None,
) -> GreeksEnrichReport:
    """The pool driver: dispatch units, fold results in sequential order.

    Liveness (DEC-PIPELINE-STALL-HEARTBEAT-001): the parent beats once before
    dispatch and once per COMPLETED unit — real observed progress only, never
    a timer keepalive that would blind the watchdog to a genuinely hung
    worker. A unit is one option chain — ALL strikes of one
    ``(underlying, window)`` — typically bounded well under the 600 s
    watchdog, so beat silence is bounded by the largest single unit.

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
    job layer recognises as a pull-halt (CRITICAL Telegram alert). A
    PICKLABLE worker ``MemoryError`` is NOT a pool break: it re-raises RAW
    here (remaining units finish under the ``with``-exit
    ``shutdown(wait=True)``) and lands in the pull wrapper's blanket fence
    as ``greeks_enrich_ok=False`` — see :func:`_run_target`.
    """
    ctx = _GreeksWorkerContext(
        lake_root=lake_root, rate_source=rate_source, stock_drift=stock_drift
    )
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
    # The evidence-grounded stock drift map (DEC-GREEKS-SPOTJOIN-STOCK-001):
    # built ONCE per pass, parent only, and ONLY when the default spot reader
    # will actually run (an injected read_spot owns spot semantics — no scan,
    # no store stat, no log noise; empty targets — no scan). Per-pass-stable,
    # so the _SpotJoinMemo determinism invariant holds.
    stock_drift = load_stock_drift_map(lake_root) if (ordered and read_spot is None) else None
    if read_bars is None and read_spot is None and write_bars is None:
        resolved = resolve_greeks_workers() if workers is None else max(1, workers)
        if resolved > 1:
            units = _build_units(ordered)
            if len(units) > 1 and _parallel_pickle_ok(lake_root, rate_source, stock_drift):
                return _run_parallel(
                    units,
                    lake_root=lake_root,
                    rate_source=rate_source,
                    max_workers=resolved,
                    stock_drift=stock_drift,
                )
    default_reader, default_spot_reader, default_writer = _default_seams(
        lake_root, stock_drift=stock_drift
    )
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
