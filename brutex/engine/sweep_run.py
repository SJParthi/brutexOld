"""Persisted + deduped + audited single-(instrument, TF) brute-force sweep.

Phase 3 Slice-1 (DEC-SWEEP-RUN-001): the orchestration seam that turns *"sweep
the K-of-my-chosen-conditions mask space for ONE (instrument, TF, direction,
params)"* into a **reproducible, idempotent, audited** leaderboard on disk. It
**composes** the golden-locked engine bricks — it re-implements none of them
(CLAUDE.md §11 COMMON):

    bars --build_bar_bits--> bar_bits                       (Stage-1A, ONCE)
    condition_bits --iter_masks_k_of_bits--> masks          (combo enumeration)
         --has_conflict--> prune degenerate pairs           (vocabulary)
         --mask_run_id (combo_hash)--> run-identity          (Golden Rule #4)
         --ComboDedup.seen--> skip already-evaluated         (Bloom→exact, O(1))
         --count_supermasks--> Stage-1B min-hits prefilter   (combo_mask)
         --ResidentTradeSim.simulate_masks_with_exit_counts--> (trades, exit_idx, counts)
                                                             (rust_trade_sim, CHUNKED)
         --metrics_from_trades--> (mask, ResultMetrics)      (sweep_driver tail)
         --rank_combos--> RankedCombo leaderboard            (ranking)
         --ranked_combo_to_result_row--> ResultRow           (the projector)
         --ResultsStore.append/flush--> Parquet sink         (results_store)

The trade walk is the resident Rust kernel (DEC-SWEEP-RUST-TRADESIM-WIRE-001,
wiring the bit-identity-proven DEC-SWEEP-RUST-TRADESIM-001 kernel into
production): one :class:`~brutex.engine.rust_trade_sim.ResidentTradeSim` is
built per tuple (the OHLC→paisa conversion + session-aware forced-exit flags
paid once), the kept masks are batch-simulated in bounded CHUNKS, and each
mask's trade stream flows through the SAME
:func:`~brutex.engine.sweep_driver.metrics_from_trades` tail
:func:`~brutex.engine.sweep_driver.metrics_for_mask` uses — so the wired chain
is byte-identical to the pure-Python one (the wired-vs-unwired store-hash
golden enforces it), and an environment where the kernel cannot serve (stale
wheel, u128 masks, out-of-envelope prices) degrades to the SAME Python oracle
loudly, never silently. ``metrics_for_mask`` itself remains the per-mask
ORACLE: a failed chunk is re-run through it one mask at a time to attribute the
culprit (fail-loud-with-fingerprint preserved — see ``run_persisted_sweep``).

Three invariants make this a *sweep RUN*, not just a sweep:

* **Reproducibility (Golden Rule #4).** Every mask's identity is the 9-field
  BLAKE3 :func:`~brutex.strategies.combo.combo_hash` with
  ``strategy_id = "mask:%032x"`` of the u128 mask (DEC-MASK-IDENTITY-001 — the
  mask *is* the strategy). Same inputs → same run-ids → same
  :class:`~brutex.engine.results_store.FlushResult.content_hash`, bit-for-bit.
* **Idempotence (Golden Rule #7).** On the digest path (the production
  launcher), redo is decided by the per-tuple COMPLETION SENTINEL
  (:mod:`brutex.engine.tuple_complete`, DEC-MARK-MANY-SENTINEL-001): a paired
  sentinel short-circuits the whole tuple; an absent/unpaired sentinel forces
  a FULL re-evaluation with dedup probes BYPASSED — never a subset re-rank —
  then rewrites every artifact byte-identically. This closed the
  partial-``mark_many`` redo hole (MIGRATION.md #650): the per-key mark loop
  is not atomic, so its marks are ADVISORY, never the redo authority. Legacy
  no-digest callers keep the probe semantics: a re-run against the same dedup
  state skips every previously-evaluated mask (Bloom→exact-confirm, O(1) per
  mask, BEFORE the expensive trade sim) and re-produces a byte-identical
  store; dedup keys are marked seen **after** the store flush (redo-not-skip
  — never a silent hole).
* **Audit trail (Golden Rule #8).** Exactly THREE hash-chained audit lines per
  full sweep — ``SWEEP_START`` / ``SWEEP_FLUSH`` / ``SWEEP_COMPLETE`` — never
  one per combo (the audit cost is O(1) in the combo count, not O(N)). A
  sentinel short-circuit emits exactly ONE line (``SWEEP_RESUMED``).

Honest envelope (Golden Rule #10): :func:`run_persisted_sweep` refuses to
enumerate more than ``cfg.max_combos`` masks (raising before any ``bar_bits``
build), the same opt-in guardrail :func:`brutex.engine.sweep_driver.rank_sweep_over_bits`
uses — an oversized sweep is a conscious choice, not an accident.

**Bounded rank + memory budget (DEC-SWEEP-STREAMING-RANK-001 part 2).** The
Phase-C rank retention is budget-guarded: a pre-flight plan
(:func:`brutex.engine.sweep_mem_budget.plan_rank_memory`, mode env
``BRUTEX_SWEEP_BOUNDED_RANK`` in ``{off, auto, on}`` default ``auto``, budget
env ``BRUTEX_SWEEP_MEM_BUDGET_BYTES``) keeps today's batch ``pairs`` +
:func:`~brutex.ranking.rank_combos` path byte-identical below budget, and past
budget either ENGAGES the bounded columnar rank
(:mod:`brutex.engine.sweep_stream` — O(kept) columnar payload at ~80-105 B/kept
instead of ~1 KB/kept rich objects, ranked through the SAME shared
:mod:`brutex.ranking` internals so bounded == batch BIT-FOR-BIT, top-K selected
via an O(survivors x log K) heap on the strict ``(-composite, mask)`` key) or
REFUSES the tuple gracefully with a typed :class:`SweepMemoryBudgetError`
(a ``SweepComboError`` tuple-skip — never a raw ``MemoryError`` crash). The
bounded arm requires the digest path + a finite ``top_k``; a genuine
``MemoryError`` remains a bare global stop (DEC-LAKE-WRITE-HALT-001). Retained
memory in bounded mode is O(kept), NOT O(1).
"""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, NoReturn, Protocol, runtime_checkable

import structlog

from brutex.data.guarded_read import read_regular_bytes
from brutex.data.lake_read import bars_by_ts_first
from brutex.data.models import CandleBar
from brutex.engine.combo_mask import (
    MAX_VOCAB_BITS,
    count_combos,
    count_supermasks,
    iter_masks_k_of_bits,
    mask_to_entry_signals,
)
from brutex.engine.exec_grid import (
    EXECUTION_GRID_1M_NEXT_BAR,
    ExecGridMap,
    build_exec_grid_map,
)
from brutex.engine.period_stats import (
    encode_judged_years,
    encode_period_stats,
    fold_period_stats,
)
from brutex.engine.reference_wire import aggregate_trade_vix
from brutex.engine.results_store import ResultRow, ResultsStore, ResultsStoreGateConflictError
from brutex.engine.rust_enum import RustEnumResult, enum_prefilter_via_kernel
from brutex.engine.rust_rank_select import union_selection
from brutex.engine.rust_trade_sim import PriceColumns, ResidentTradeSim
from brutex.engine.session_lens import ist_trade_date
from brutex.engine.stage1a import apply_1day_mask_for, build_bar_bits
from brutex.engine.sweep_apriori import apriori_enabled, resolve_max_mask_bars
from brutex.engine.sweep_driver import (
    DEFAULT_MAX_COMBOS,
    DEFAULT_START_EQUITY_PAISA,
    metrics_for_mask,
    metrics_from_trades,
)
from brutex.engine.sweep_lookahead import maybe_guard_bars
from brutex.engine.sweep_mem_budget import (
    BoundedRankMode,
    plan_rank_memory,
    resolve_bounded_rank_mode,
    resolve_mem_budget_bytes,
    watermark_message,
)
from brutex.engine.sweep_stream import (
    BoundedRankResult,
    SurvivorColumns,
    rank_survivor_columns,
)
from brutex.engine.trade_sim import simulate_trades
from brutex.engine.trial_stats import (
    TrialStatsError,
    read_tuple_trial_stats,
    trial_stats_sidecar_path,
    tuple_trial_stats_from_sharpes,
    write_tuple_trial_stats,
)
from brutex.engine.tuple_complete import (
    TupleCompletionError,
    TupleCompletionSentinel,
    completion_sentinel_path,
    read_completion_sentinel,
    write_completion_sentinel,
)
from brutex.execution.fill_mode import FillMode
from brutex.metrics.compute import ResultMetrics
from brutex.models import BruteXError
from brutex.money import round_money
from brutex.ranking import (
    GATE_CVAR_TAIL,
    MIN_TRADES,
    CvarGateConfig,
    OverfitGateConfig,
    RankedCombo,
    cvar_rejects,
    rank_combos,
)
from brutex.ranking.multi_select import SelectionInputs
from brutex.ranking.temporal import (
    TemporalGateInput,
    TemporalGateStats,
    TemporalRobustnessConfig,
    TemporalTriple,
    judged_years,
)
from brutex.strategies.combo import combo_hash
from brutex.strategies.types import (
    FibAnchorKind,
    PriceSource,
    Side,
    price_source_params_fragment,
)
from brutex.universe import NIFTY_FUTURES_SERIES_SYMBOL
from brutex.vocabulary import ProximityParams, has_conflict, swing_params_fragment

if TYPE_CHECKING:
    # Annotation-only imports — kept out of the runtime import graph so the
    # `fastbloom_rs` PyO3 wheel (pulled in transitively by both modules below
    # via brutex.orchestrator.bloom) never enters brutex.engine's import chain.
    # That single-init-per-interpreter wheel breaks the strict per-module
    # coverage gates for the other brutex.engine.* modules otherwise (the same
    # constraint recorded in DEC-COV-BACKFILL-LIMIT-001). ``ComboDedup`` only
    # *receives* an already-built OptionDedupIndex; the runner only *receives*
    # an already-open ``AuditSink`` (the real ``AuditWriter`` on the sequential
    # path, a ``BufferedAuditCollector`` in a parallel-sweep worker,
    # DEC-MULTICORE-SWEEP-PHASE1-001) — neither is constructed here.
    from brutex.data.option_dedup import OptionDedupIndex
    from brutex.engine.reference_series import TradeReferenceStamper
    from brutex.engine.sweep_kernel import ResidentSweepKernel
    from brutex.orchestrator.audit import AuditSink

_LOG = structlog.get_logger("brutex.engine.sweep_run")

#: Audit action labels — the three lines a full sweep emits, plus the single
#: line a sentinel short-circuit emits (DEC-MARK-MANY-SENTINEL-001).
#: DEC-SWEEP-RUST-TRADESIM-WIRE-001 adds NO audit action and NO payload field —
#: the engine choice is engine-blind by design (run_id hashes mask/params/data/
#: code, never the engine; the hash-chained audit stays byte-identical) and the
#: path-selection signal lives in structlog ONLY.
_ACTION_START: Final[str] = "SWEEP_START"
_ACTION_FLUSH: Final[str] = "SWEEP_FLUSH"
_ACTION_COMPLETE: Final[str] = "SWEEP_COMPLETE"
_ACTION_RESUMED: Final[str] = "SWEEP_RESUMED"

#: Hard cap on masks per resident-trade-sim batch call (DEC-SWEEP-RUST-TRADESIM-
#: WIRE-001). Bounds the peak ``Vec<Vec<TradeRecord>>`` FFI marshal + the
#: per-chunk ``ClosedTrade`` materialisation regardless of how many masks
#: survive Stage-1B; chunking is a pure batching choice — order-preserving and
#: result-invariant (pinned by the chunk-invariance tests).
_TRADE_SIM_CHUNK_MASKS: Final[int] = 256

#: Cell budget (``masks × bars``) per batch call — the DELIBERATELY-CONSERVATIVE
#: memory bound for production bar counts (a multi-year 1-minute window is
#: ~350k-560k bars; a fixed 256-mask chunk there could materialise tens of
#: millions of trades at once). Per-mask trades ≤ n_bars/2, so one chunk's
#: output is ≤ cells/2 trade tuples — bounded. 4·2^20 cells keeps the 256-mask
#: chunk up to ~16k bars and still exceeds the kernel's 2^20 rayon threshold on
#: big windows (the parallel arm stays reachable in single-process runs).
_TRADE_SIM_CHUNK_CELLS: Final[int] = 4 << 20

#: Per-cell hit-count memo capacity (DEC-SWEEP-APRIORI-DEFAULT-001, MIGRATION.md
#: #1752). Counts are a PURE function of ``(bar_bits, mask)`` — per-mask
#: independent — so memoising them across a cell's k-ladder is identity-free by
#: construction (memo-on == memo-off bytes, parity-pinned). Beyond this cap the
#: memo becomes pure PASS-THROUGH (computed values are still returned, just not
#: retained), bounding the memo at ~100 MiB worst case (MEASURED, review fix
#: round 1 m3 — the earlier "~48 B/entry ≈ 50 MiB" figure ignored the boxed
#: PyLong keys/values: a full 2^20-entry ``dict[int, int]`` is ~40 MiB of dict
#: shell + ~60 MiB of PyLong objects). Worst case across the 8-worker parallel
#: envelope: ~800 MiB — inside the CLAUDE.md §8 48 GB envelope. AXIS-ORDER
#: caveat (review fix round 1 m6): ``iter_sweep_tuples`` nests proximity /
#: fib-anchor INSIDE k and both are cell-key determinants, so with
#: ``|proximities| × |fib_anchors| > 1`` the drivers' single-cell
#: reset-on-change memo delivers ZERO cross-k reuse (correctness unaffected —
#: the memo is pure pass-through; a multi-cell memo is a deliberately-deferred
#: perf follow-up). Every returned count stays identical either way.
HIT_MEMO_MAX_ENTRIES: Final[int] = 1_048_576


def _trade_sim_chunk_masks(n_bars: int) -> int:
    """Masks per :meth:`ResidentTradeSim.simulate_masks` call for an ``n_bars`` window.

    ``min(_TRADE_SIM_CHUNK_MASKS, _TRADE_SIM_CHUNK_CELLS // n_bars)`` floored at
    1 — the count cap for small windows, the cell budget for deep 1-minute
    windows (see the two constants). Chunk size NEVER changes results (only
    peak memory / FFI batching); the chunk-invariance tests pin it.
    """
    by_cells = _TRADE_SIM_CHUNK_CELLS // max(1, n_bars)
    return max(1, min(_TRADE_SIM_CHUNK_MASKS, by_cells))


class SweepComboError(BruteXError):
    """One combo's metric evaluation raised — names the offending mask + run-id + tuple.

    A raise here is treated CONSERVATIVELY as tuple-wide (the safe redo-everything
    choice): the only per-combo-varying input to `metrics_for_mask` is the mask, so a
    mask-specific edge cannot be ruled out — skipping the whole tuple is the conservative
    response. The grid drivers SKIP the whole tuple (deterministic redo next run), never
    the one mask.
    """


class SweepAprioriBudgetError(SweepComboError):
    """The Apriori pruned candidate set would exceed the per-cell mask·bar budget.

    Raised by :func:`brutex.engine.sweep_apriori.generate_apriori_candidates`
    (only when ``BRUTEX_SWEEP_APRIORI`` is enabled) when the PURE-INTEGER
    work estimate ``len(candidates) * n_bars`` STRICTLY EXCEEDS
    ``BRUTEX_SWEEP_APRIORI_MAX_MASK_BARS`` (a mask·bar count — no float division,
    no wall clock, so the trip is DETERMINISTIC and bit-identical Python↔Rust,
    Golden Rule #7). Subclassing :class:`SweepComboError` rides the existing
    tuple-skip contract UNCHANGED — the grid drivers count it in
    ``tuples_failed``, emit ``TUPLE_FAILED``, leave the tuple un-sentineled for a
    deterministic redo, and exit non-zero — a LOUD refusal of a degenerate cell
    rather than a silent multi-day run. The message NAMES the cell, ``k``, the
    candidate count, the ``mask·bar`` product / limit, and the remedies (raise
    ``BRUTEX_SWEEP_APRIORI_MAX_MASK_BARS``, tighten ``min_hits``, reduce ``k``, or
    unset the flag). The wall-clock runaway backstop is the separate #1608 stall
    watchdog (non-identity-bearing), which this deterministic breaker never
    touches.
    """


class SweepAprioriUnsupportedError(SweepComboError):
    """The Apriori frontier cannot represent a ``>= 64``-bit vocabulary — GRACEFUL refusal.

    Raised by :func:`brutex.engine.sweep_apriori.generate_apriori_candidates`
    (only when ``BRUTEX_SWEEP_APRIORI`` is enabled) when a chosen condition bit is
    ``>= 64``: the sorted-``uint64`` frequent frontier holds each mask in a single
    64-bit limb, so a u128 vocabulary is out of range. Subclassing
    :class:`SweepComboError` rides the existing tuple-skip contract UNCHANGED — the
    grid drivers count it in ``tuples_failed``, emit ``TUPLE_FAILED``, leave the
    tuple un-sentineled for a deterministic redo, and exit non-zero — a LOUD
    per-tuple refusal instead of a raw ``ValueError`` that would crash the WHOLE
    grid. The remedy is to unset ``BRUTEX_SWEEP_APRIORI``: the byte-identical
    brute-force enumeration path supports the full u128 vocabulary.
    """


class SweepMemoryBudgetError(SweepComboError):
    """The tuple's rank-region memory exceeds the operator budget — a GRACEFUL refusal.

    Raised (a) at PRE-FLIGHT when the batch-rank projection
    (:func:`brutex.engine.sweep_mem_budget.plan_rank_memory`) exceeds
    ``BRUTEX_SWEEP_MEM_BUDGET_BYTES`` and the bounded columnar rank cannot / may
    not engage, BEFORE any survivor allocation; or (b) MID-FLIGHT when the
    bounded accumulator's EXACT owned bytes cross the budget (the watermark).
    Subclassing :class:`SweepComboError` rides the existing tuple-skip contract
    UNCHANGED — the grid drivers count it in ``tuples_failed``, emit
    ``TUPLE_FAILED``, leave the tuple un-sentineled for a deterministic redo,
    and exit non-zero — instead of today's raw ``MemoryError`` whole-sweep
    crash (DEC-SWEEP-STREAMING-RANK-001). The message always NAMES the knobs
    (both env vars) + concrete remedies. A genuine ``MemoryError`` remains a
    bare global stop (DEC-LAKE-WRITE-HALT-001) — this error is the guard that
    fires before one plausibly can.
    """


def _direction_label(direction: Side) -> str:
    """Map a :class:`~brutex.strategies.types.Side` to its sweep direction label.

    Parameters
    ----------
    direction
        :data:`~brutex.strategies.types.Side.BUY` (long) or ``Side.SELL`` (short).

    Returns
    -------
    str
        ``"bull"`` for ``BUY``, ``"bear"`` for ``SELL`` — the labels the combo
        identity + result row carry (matching :func:`brutex.strategies.combo.combo_hash`).

    Raises
    ------
    ValueError
        If *direction* is :data:`~brutex.strategies.types.Side.HOLD` — a sweep
        backtests a directional edge; ``HOLD`` is not a trade direction.
    """
    if direction is Side.BUY:
        return "bull"
    if direction is Side.SELL:
        return "bear"
    raise ValueError(f"sweep direction must be Side.BUY or Side.SELL, not {direction!r}")


def direction_label(direction: Side) -> str:
    """PUBLIC re-export of :func:`_direction_label` for the parallel driver — O(1).

    ONE source of truth (CLAUDE.md §11 COMMON): the parallel driver
    (:mod:`brutex.engine.sweep_parallel`, DEC-MULTICORE-SWEEP-PHASE1-001) uses this
    to build the BYTE-IDENTICAL cache key + labels without importing the private
    ``_direction_label``.
    """
    return _direction_label(direction)


@runtime_checkable
class DedupSink(Protocol):
    """The dedup contract :func:`run_persisted_sweep` consults — ``seen`` + ``mark_many``.

    A structural seam (DEC-MULTICORE-SWEEP-PHASE1-001) so the runner accepts EITHER
    the real :class:`ComboDedup` (the legacy no-digest path, where the probes are
    load-bearing) OR a stand-in whose methods are never called on the DIGEST path
    (``bypass_dedup`` skips both). :class:`ComboDedup` satisfies this Protocol
    structurally, so widening the runner's annotation from ``ComboDedup`` to
    ``DedupSink`` is a pure widening that changes NO existing call.
    """

    def seen(self, run_id_hex: str) -> bool:
        """Return ``True`` iff *run_id_hex* was already marked seen."""
        ...

    def mark_many(self, run_id_hexes: Iterable[str]) -> int:
        """Mark every identity as seen; return how many were marked."""
        ...


class ComboDedup:
    """O(1) combo run-identity dedup — a thin adapter over an :class:`OptionDedupIndex`.

    The sweep dedups on the 32-byte BLAKE3 run-identity of each mask (the raw
    bytes of :func:`mask_run_id`'s hex). This is a **composition** of the
    golden-locked :class:`~brutex.data.option_dedup.OptionDedupIndex` (its
    Bloom→exact-confirm, thread-safe two-tier logic is reused verbatim, NOT
    re-implemented) — this class only re-keys from a hex string to the raw bytes
    the index consumes (CLAUDE.md §11 COMMON).

    Parameters
    ----------
    index
        The shared dedup index. Inject one built from a
        :class:`~brutex.orchestrator.bloom.BloomFilter` + an
        :class:`~brutex.data.lmdb_membership.LmdbMembership` (persistent) or a
        plain ``set`` (in-memory) per the run's recoverability needs.
    """

    def __init__(self, index: OptionDedupIndex) -> None:
        self._index = index

    def seen(self, run_id_hex: str) -> bool:
        """Return ``True`` iff *run_id_hex* was already marked — O(1), atomic.

        Parameters
        ----------
        run_id_hex
            A 64-char lowercase BLAKE3 hex run-identity (from :func:`mask_run_id`).

        Returns
        -------
        bool
            ``True`` if the identity is present (Bloom-positive **and**
            exact-confirmed), else ``False`` (Bloom-negative is definitively new).
        """
        return self._index.seen(bytes.fromhex(run_id_hex))

    def mark(self, run_id_hex: str) -> None:
        """Record *run_id_hex* as seen in both dedup tiers — O(1), atomic.

        Parameters
        ----------
        run_id_hex
            A 64-char lowercase BLAKE3 hex run-identity to mark.
        """
        self._index.add(bytes.fromhex(run_id_hex))

    def mark_many(self, run_id_hexes: Iterable[str]) -> int:
        """Mark every identity in *run_id_hexes* as seen; return how many were marked.

        Idempotent: re-marking an already-seen identity is a harmless no-op that
        still counts toward the returned total (it reflects identities processed,
        not net-new insertions — the underlying tiers absorb duplicates).

        Parameters
        ----------
        run_id_hexes
            Iterable of 64-char lowercase BLAKE3 hex run-identities.

        Returns
        -------
        int
            The number of identities marked.
        """
        count = 0
        for run_id_hex in run_id_hexes:
            self._index.add(bytes.fromhex(run_id_hex))
            count += 1
        return count


def mask_run_id(
    *,
    mask: int,
    direction: str,
    instrument: str,
    tf: str,
    mode: str,
    params: Mapping[str, Any],
    data_snapshot_hash: str,
    indicator_version: str,
    code_commit_sha: str,
) -> str:
    """Return the 9-field BLAKE3 run-identity of one strategy *mask* (Golden Rule #4).

    DEC-MASK-IDENTITY-001: the u128 mask **is** the strategy identity, encoded as
    the ``strategy_id`` ``"mask:%032x"`` (32 lowercase hex chars, zero-padded);
    the remaining eight fields are forwarded unchanged to
    :func:`brutex.strategies.combo.combo_hash`. Two masks share a run-identity
    iff all nine inputs collide, so the same mask under the same context + params
    + data + code always hashes to the same id (reproducibility) and a different
    mask never aliases it.

    Parameters
    ----------
    mask
        The u128 strategy mask. Encoded as ``"mask:%032x"`` for the ``strategy_id``.
    direction
        Direction label (``"bull"`` / ``"bear"``); non-empty.
    instrument, tf, mode
        Context labels (e.g. ``"NIFTY"``, ``"5m"``, ``"A"``); each non-empty.
    params
        Canonical parameter dict (sl/tp/prior_n/proximity/equity) —
        canonicalised deterministically by
        :func:`brutex.strategies.combo.canonicalise_params`.
    data_snapshot_hash, indicator_version, code_commit_sha
        The data-slice / indicator-stack / code-commit identity; each non-empty.

    Returns
    -------
    str
        The 64-char lowercase BLAKE3 hex run-identity.

    Raises
    ------
    brutex.strategies.errors.StrategyError
        If any required string field is empty (delegated to
        :func:`brutex.strategies.combo.combo_hash`).
    """
    return combo_hash(
        strategy_id=f"mask:{mask:032x}",
        direction=direction,
        instrument=instrument,
        tf=tf,
        mode=mode,
        params=params,
        data_snapshot_hash=data_snapshot_hash,
        indicator_version=indicator_version,
        code_commit_sha=code_commit_sha,
    )


@dataclass(frozen=True, slots=True)
class SweepRunConfig:
    """Immutable configuration for one persisted sweep run.

    Captures everything the sweep needs that is NOT the bars themselves: the
    mask space (``condition_bits`` + ``k``), the trade-sim knobs, the run-identity
    context (instrument / tf / direction / mode + the three reproducibility
    hashes), and the honest combo-count cap. Validated at construction so an
    impossible sweep fails fast.

    Attributes
    ----------
    instrument, tf, mode
        Context labels stamped on the run identity + result rows; each non-empty.
    direction
        Trade direction (:data:`~brutex.strategies.types.Side.BUY` or ``SELL``;
        ``HOLD`` is rejected by :func:`run_persisted_sweep`, not here, so the
        config stays a pure data holder).
    condition_bits
        The condition bit positions (of the declared condition vocabulary) to
        brute-force over; duplicates
        are ignored (set semantics) for both the enumeration and the ``k`` bound.
    k
        How many of the chosen conditions each combo requires
        (``0 <= k <= n_unique``).
    data_snapshot_hash, indicator_version, code_commit_sha
        The three reproducibility identity fields; each non-empty.
    tf_minutes
        Bar timeframe in whole minutes (``>= 1``).
    sl_paisa, tp_paisa
        Stop-loss / take-profit distances in paisa (``> 0``), or ``None`` to
        disable that leg.
    prior_n
        Prior-bar pattern look-back ∈ {1, 2, 3, 4} (passed to Stage-1A).
    proximity
        Proximity tolerances for the ``near_*`` conditions, or ``None`` for the
        Stage-1A default (10 bps).
    fib_anchor
        The swept Fibonacci-anchor family
        (:class:`~brutex.strategies.types.FibAnchorKind`) the Group-4 Fib levels are
        evaluated against (DEC-NIFTY-FIB-AXIS-001). Defaults to
        :attr:`FibAnchorKind.PREV_DAY` so a legacy caller is byte-identical. It is
        folded into the run-identity via :func:`canonical_trade_params`, so a
        different anchor is a distinct, reproducible combo (Golden Rule #4), and
        into the Stage-1A build so the sweep evaluates that anchor's Fib levels.
    start_equity_paisa
        Notional capital base (paisa) for the percentage-metric denominator.
    max_combos
        Refuse to enumerate more than this many masks (the honest opt-in guard).
    top_k
        Persist only the top ``top_k`` ranked combos, or ``None`` for all ranked.
    min_hits
        Stage-1B coarse pre-filter threshold (defaults to
        :data:`brutex.ranking.MIN_TRADES`); a mask firing on fewer bars can never
        be rankable, so it is pruned before the trade sim. ``0`` disables it.
    overfit
        Opt-in :class:`~brutex.ranking.overfit.OverfitGateConfig` activating the
        Stage-3 multiple-testing **overfit gate** (6th gate + Deflated-Sharpe
        ranking term, DEC-RANKING-OVERFIT-001) inside :func:`brutex.ranking.rank_combos`.
        ``None`` (the default) keeps the legacy five-gate / seven-term ranking. The
        leaderboard's trial count ``N`` is this tuple's backtested-combo count —
        the authoritative per-(instrument, TF) ``N`` (see the ``overfit`` module's
        honest envelope: no statistically-valid post-store re-gate exists). The
        gate identity is now encoded in the store ADDRESS itself: the
        active-only gate config folds into
        :func:`~brutex.engine.sweep_orchestrator._tuple_digest`
        (DEC-SWEEP-TUPLE-DIGEST-GATE-IDENTITY-001), so a gated vs an ungated run
        over ONE results root map to DIFFERENT Parquet + completion-sentinel
        addresses — an ungated store can never be served as a gated one via the
        sentinel short-circuit, and a same-address collision differing only in
        the nullable DSR metric is a tuple-scoped skip (C5), never a silent
        overwrite.
    temporal
        Opt-in :class:`~brutex.ranking.temporal.TemporalRobustnessConfig`
        activating the **temporal-robustness year gate + the per-period
        finalist-stats capture** (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001). ``None``
        (the default) is byte-identical legacy behaviour — no per-bar year
        table, no per-mask fold, no rank filter, no stored columns (the
        gate-OFF zero-work spy pin + the frozen goldens enforce it). Active
        (either mode — ``"observe"`` captures + counts would-be kills without
        filtering): an O(N_bars) per-tuple year/session precompute + an
        O(trades)/mask exit-year fold feed the pre-rank SURVIVORS filter
        (hard doors → temporal → overfit; the DSR candidate pool is
        UNTOUCHED), and every persisted finalist gains the four optional
        temporal columns. IDENTITY RULING: this config does NOT enter
        ``mask_run_id``/``canonical_trade_params`` (the verified DSR
        precedent — a pure rejection gate changes WHICH rows persist, never
        any row's bytes); provenance rides the hash-chained audit payload,
        emitted only when configured.

    Raises
    ------
    ValueError
        If a context / identity string is empty, ``tf_minutes < 1``, ``k`` is
        outside ``[0, n_unique]``, or a paisa knob is non-positive.
    """

    instrument: str
    tf: str
    direction: Side
    mode: str
    condition_bits: Sequence[int]
    k: int
    data_snapshot_hash: str
    indicator_version: str
    code_commit_sha: str
    tf_minutes: int
    sl_paisa: int | None = None
    tp_paisa: int | None = None
    prior_n: int = 3
    proximity: ProximityParams | None = None
    fib_anchor: FibAnchorKind = FibAnchorKind.PREV_DAY
    #: The swept VWAP / EMA price-source axis (DEC-NIFTY-PRICE-SOURCE-AXIS-001) —
    #: which per-bar scalar feeds the running VWAP (bits 52-53) / EMA (bits 0-5)
    #: accumulators. Default ``(HLC3, CLOSE)`` reproduces today's hardcoded
    #: reduction, so a legacy caller is byte-identical; the sources are folded
    #: into the run-identity via :func:`canonical_trade_params` (DEFAULT-OMITTED,
    #: Override 1) and into the Stage-1A build so a non-default source is a
    #: distinct, reproducible combo (Golden Rule #4).
    vwap_source: PriceSource = PriceSource.HLC3
    ema_source: PriceSource = PriceSource.CLOSE
    start_equity_paisa: int = DEFAULT_START_EQUITY_PAISA
    max_combos: int = DEFAULT_MAX_COMBOS
    top_k: int | None = None
    min_hits: int = MIN_TRADES
    overfit: OverfitGateConfig | None = None
    #: ADR-0012 NIFTY-futures roll schedule (DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001
    #: / DEC-STAGE1A-ROLL-RESET-001): strictly increasing owning-expiry
    #: day-ordinals from ``resolve_futures_series`` — the roll boundaries the
    #: Stage-1A build hard-resets on. PRESENCE IS A TOTAL FUNCTION OF THE
    #: ``instrument`` IDENTITY FIELD (the ``__post_init__`` hard invariant,
    #: both directions), so no instrument can ever mint both a with-key and a
    #: without-key run identity (the #1062 conditional-params bug class is
    #: contained). ``None`` — every spot config — emits NO params key, so spot
    #: run_ids stay byte-identical.
    roll_schedule: tuple[int, ...] | None = None
    temporal: TemporalRobustnessConfig | None = None
    #: Opt-in Door-6 SL-hit-rate ceiling (DEC-METRICS-LOSSRATE-FIRSTCLASS-001
    #: Stage B — the ``SweepGridConfig.max_sl_hit_rate`` knob, threaded here by
    #: ``to_run_config``): a gate-time SELECTION knob, so it is folded into the
    #: store-address identity by ``_gate_config_fragment``
    #: (active-only — ``None``, the default, leaves every digest AND every gate
    #: verdict byte-identical) and is NEVER part of ``canonical_trade_params``
    #: (simulation is unchanged). Appended LAST (defaulted-last law).
    max_sl_hit_rate: float | None = None
    #: Opt-in CVaR tail-risk rejection gate (DEC-RANK-CVAR-TAIL-GATE-001) —
    #: DEFAULT-OFF (``None`` = the pre-gate sweep, byte-identical). When set,
    #: each kept mask's per-trade P&L list is judged at the ONE eval spine
    #: (:func:`_iter_mask_metrics`) — the only point the trades exist — and a
    #: fat-tail mask is dropped BEFORE metrics/ranking (counted on
    #: :attr:`SweepRunSummary.cvar_rejected` + one ``sweep.cvar_gate`` INFO
    #: per tuple). DISCLOSED interaction: a rejected mask is thereby EXCLUDED
    #: from the overfit gate's trial pool ``N`` and the trial-stats sidecar
    #: population (both become post-CVaR when both gates are active) — an
    #: activation-time consideration recorded in the DEC. The active config
    #: re-keys the store address via the shared identity fragment
    #: (DEC-SWEEP-TUPLE-DIGEST-GATE-IDENTITY-001 law); ``None`` is elided.
    #: Appended LAST (defaulted-last law).
    cvar_gate: CvarGateConfig | None = None
    #: Gate-scoped-dedup provenance carrier (DEC-SWEEP-SERVING-GATE-
    #: PROVENANCE-001): the producing grid's canonical gate+selection token
    #: (:func:`brutex.engine.sweep_orchestrator.grid_gate_provenance` — set by
    #: ``SweepTuple.to_run_config``, the ONE production seam), stamped onto
    #: every persisted finalist row so the ``/results`` read side can scope
    #: coordinates. ``None`` (the default — every ungated grid and every
    #: legacy direct caller) is byte-identical to the pre-carrier config: no
    #: row token, no store column. NEVER an input to the run identity
    #: (:func:`_canonical_params` does not read it).
    gate_provenance: str | None = None

    def __post_init__(self) -> None:
        for name in ("instrument", "tf", "mode"):
            if not getattr(self, name):
                raise ValueError(f"SweepRunConfig.{name} must be non-empty")
        # Instrument ⟺ schedule hard invariant (DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001):
        # the futures alias REQUIRES a schedule, everything else FORBIDS one —
        # presence is a pure function of the identity field, never a caller mood.
        if (self.instrument == NIFTY_FUTURES_SERIES_SYMBOL) != (self.roll_schedule is not None):
            raise ValueError(
                "SweepRunConfig: roll_schedule is required for (and only for) the "
                f"NIFTY-futures series alias {NIFTY_FUTURES_SERIES_SYMBOL!r} — got "
                f"instrument={self.instrument!r} with roll_schedule="
                f"{self.roll_schedule!r}"
            )
        if self.roll_schedule is not None and (
            not self.roll_schedule
            or list(self.roll_schedule) != sorted(set(self.roll_schedule))
            or self.roll_schedule[0] < 0
        ):
            raise ValueError(
                "SweepRunConfig.roll_schedule must be a non-empty strictly increasing "
                f"tuple of non-negative day ordinals, got {self.roll_schedule!r}"
            )
        for name in ("data_snapshot_hash", "indicator_version", "code_commit_sha"):
            if not getattr(self, name):
                raise ValueError(f"SweepRunConfig.{name} must be non-empty")
        if self.tf_minutes < 1:
            raise ValueError(f"SweepRunConfig.tf_minutes must be >= 1, got {self.tf_minutes}")
        # Validate every condition bit is in the u128 mask range AT CONFIG
        # TIME (DEC-SWEEP-DEDUP-LMDB-001): an out-of-range bit otherwise slips
        # past the k-bound check and fails LATE as a bare ValueError raised from
        # the lazy ``iter_masks_k_of_bits`` generator mid-enumeration, aborting
        # the whole grid. Reject it here (the fail-fast contract this class
        # promises) so a bad combo is caught — and tuple-skipped — at planning.
        out_of_range = sorted({bit for bit in self.condition_bits if not 0 <= bit < MAX_VOCAB_BITS})
        if out_of_range:
            raise ValueError(
                f"SweepRunConfig.condition_bits {out_of_range} out of range [0, {MAX_VOCAB_BITS})"
            )
        n_unique = len(set(self.condition_bits))
        if not 0 <= self.k <= n_unique:
            raise ValueError(
                f"SweepRunConfig.k={self.k} must be in [0, {n_unique}] "
                f"({n_unique} unique condition bits)"
            )
        for name in ("sl_paisa", "tp_paisa"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"SweepRunConfig.{name} must be > 0 or None, got {value}")
        if self.start_equity_paisa <= 0:
            raise ValueError(
                f"SweepRunConfig.start_equity_paisa must be > 0, got {self.start_equity_paisa}"
            )
        if self.top_k is not None and self.top_k < 1:
            raise ValueError(f"SweepRunConfig.top_k must be >= 1 or None, got {self.top_k}")
        if self.min_hits < 0:
            raise ValueError(f"SweepRunConfig.min_hits must be >= 0, got {self.min_hits}")
        # The SAME range law SweepGridConfig enforces (CLAUDE.md §11 COMMON —
        # one validation contract on both config layers, so a direct
        # run_persisted_sweep caller cannot smuggle a junk ceiling past the
        # grid's check).
        if self.max_sl_hit_rate is not None and (
            not math.isfinite(self.max_sl_hit_rate) or not 0.0 < self.max_sl_hit_rate <= 1.0
        ):
            raise ValueError(
                f"SweepRunConfig.max_sl_hit_rate must be a finite fraction in "
                f"(0.0, 1.0] or None, got {self.max_sl_hit_rate}"
            )


@dataclass(frozen=True, slots=True)
class SweepRunSummary:
    """The tally a :func:`run_persisted_sweep` returns (and stamps into the audit).

    Attributes
    ----------
    enumerated
        Total masks yielded by the K-of-bits enumeration.
    conflict_pruned
        Of those, how many carried a degenerate conflict pair (pruned, never
        evaluated).
    skipped_dedup
        Of the non-conflict masks, how many were already-seen (skipped before
        evaluation — the idempotence path).
    evaluated
        Non-conflict, non-dedup masks whose fate this run decided (their identity
        is marked seen so a re-run skips them) — includes those the Stage-1B
        pre-filter dropped.
    ranked
        Surviving combos that cleared the hard rejection gates — and, when
        ``cfg.overfit`` is set, the Stage-3 multiple-testing overfit gate too (the
        leaderboard size before ``top_k`` truncation).
    persisted
        Finalists appended to the store — the LANE-W1 persist UNION
        (DEC-RANK-PARETO-PERSIST-001 / DEC-RANK-QUOTA-PERSIST-1000-001): the
        composite top-``cfg.top_k`` prefix PLUS the union-only extras the
        per-axis quotas + the ε-Pareto frontier admit (so
        ``persisted >= min(ranked, top_k)``; at ``top_k=None`` the union adds
        tags, never rows — ``persisted == ranked``).
    store_content_hash
        The :class:`~brutex.engine.results_store.FlushResult` content hash — the
        sweep's reproducible content address.
    """

    enumerated: int
    conflict_pruned: int
    skipped_dedup: int
    evaluated: int
    ranked: int
    persisted: int
    store_content_hash: str
    #: Combos that actually ran ``metrics_for_mask`` (the Bailey-LdP trial
    #: population — ADR-0008 condition 3); ``evaluated - minhits_dropped``.
    backtested: int = 0
    #: Stage-1B drops among the evaluated survivors (audit context).
    minhits_dropped: int = 0
    #: Finite-Sharpe trials + their fsum moments (the sidecar's V inputs;
    #: zero when no sidecar was written this run).
    trial_n_finite: int = 0
    trial_sum_sharpe: float = 0.0
    trial_sum_sharpe_sq: float = 0.0
    #: Masks the opt-in CVaR tail gate rejected at the eval spine
    #: (DEC-RANK-CVAR-TAIL-GATE-001); always 0 when ``cfg.cvar_gate`` is
    #: ``None`` (the default). NOT folded into the supervisor/parallel grid
    #: tallies (the per-tuple ``sweep.cvar_gate`` INFO carries it) — a named
    #: activation-time follow-up.
    cvar_rejected: int = 0
    #: ``True`` iff this run wrote/overwrote the tuple's trial-stats sidecar.
    #: With a digest supplied this is every non-short-circuited run: the
    #: sentinel-authoritative redo BYPASSES dedup probes, so the population is
    #: always FULL — ADR-0008 condition 2's preserve-on-partial-resume rule is
    #: now enforced structurally (a partial population cannot occur on the
    #: digest path; DEC-MARK-MANY-SENTINEL-001).
    trial_sidecar_written: bool = False
    #: ``True`` iff the tuple was short-circuited by a PAIRED completion
    #: sentinel (DEC-MARK-MANY-SENTINEL-001): nothing was evaluated, flushed,
    #: written, or marked — every other tally is zero and
    #: ``store_content_hash`` echoes the sentinel's recorded hash.
    resumed_complete: bool = False
    #: ``True`` iff this run wrote the tuple's completion sentinel (strictly
    #: last, after ``mark_many``).
    sentinel_written: bool = False
    # ── Temporal year-gate tallies (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001) —
    #    additive defaulted fields, all-zero/False unless ``cfg.temporal`` ran ──
    #: Hard-door survivors REMOVED by the temporal gate (``mode="gate"``).
    temporal_gated: int = 0
    #: Survivors that WOULD have been removed (``mode="observe"`` — counted,
    #: logged, audited; nothing filtered).
    temporal_observed_kills: int = 0
    #: The WHOLE gate abstained — judged years below the config floor (no
    #: combo rejected; one ``sweep_run.temporal_gate`` WARN fired).
    temporal_abstained: bool = False
    #: How many calendar years the tuple's window JUDGED (the P1 denominator).
    temporal_judged_years: int = 0
    #: LANE-W1 (DEC-RANK-QUOTA-PERSIST-1000-001): gate-passing RANKED combos
    #: NOT selected by ANY persist basis (``ranked - persisted``) — the
    #: "gated-pass-but-truncated" honesty counter (0 at ``top_k=None``).
    gated_pass_truncated: int = 0
    #: LANE-W1 (DEC-RANK-PARETO-PERSIST-001): ε-Pareto frontier members
    #: dropped by the ``PARETO_FRONTIER_CAP`` bound (every drop also warned).
    frontier_dropped: int = 0


def canonical_trade_params(
    *,
    sl_paisa: int | None,
    tp_paisa: int | None,
    prior_n: int,
    proximity: ProximityParams | None,
    start_equity_paisa: int,
    fib_anchor: FibAnchorKind = FibAnchorKind.PREV_DAY,
    vwap_source: PriceSource = PriceSource.HLC3,
    ema_source: PriceSource = PriceSource.CLOSE,
    roll_schedule: tuple[int, ...] | None = None,
) -> dict[str, Any]:
    """Return the deterministic trade-sim params dict for the run-identity hash.

    Bundles the trade-sim knobs into one JSON-canonicalisable dict (no floats,
    no objects — only ints / strings / ``None``) so
    :func:`brutex.strategies.combo.canonicalise_params` can hash it stably. The
    proximity tolerances are flattened to their three integer bps values (or
    ``None`` when defaulted) so two configs with the same effective proximity
    hash identically.

    Two params-emit classes ride this dict (both eval-affecting, both
    params-carried, neither an ``indicator_version`` bump):

    * **Unconditionally emitted** — ``{fib_anchor}``. The anchor genuinely
      changes the Group-4 Fib levels a combo evaluates against, so a run under a
      different anchor is a DIFFERENT computation and MUST get a distinct
      run-identity (Golden Rule #4 / #7). It is emitted for EVERY family
      (including prev-day) so the honest run-identity always carries it.
    * **Optionally emitted when non-default** — ``{vwap_source, ema_source}``
      (DEC-NIFTY-PRICE-SOURCE-AXIS-001, Override 1). The keys are OMITTED when
      they equal the default ``(HLC3, CLOSE)`` and emitted only when a non-default
      source is swept. RATIONALE: unconditional emit would re-key EVERY existing
      run's identity (a hard back-compat break for the sibling Rust-core owner's
      + product-audit's twin-baseline capture, which depend on existing run_ids
      staying byte-identical). Default-omit has no canonicalisation ambiguity —
      absent ALWAYS means the default, a clean bijection — and follows the repo's
      optional-param convention. A default cell is byte-identical to the pre-axis
      run-identity; a non-default cell gets a distinct one (no store collision).

    The ADR-0012 futures ``roll_schedule`` enters CONDITIONALLY
    (DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001): ``bar_bits`` are a function of
    ``(bars, schedule)`` and the schedule derives from LAKE state (dir names) —
    ``code_commit_hash`` cannot capture it — so Golden Rule #4 demands it in
    the recorded identity, and the recorded key makes each futures run
    SELF-DESCRIBING for the recount's evidence check. The conditional emission
    can never re-open the #1062 bug class because
    :class:`SweepRunConfig.__post_init__` pins presence ⟺ the futures alias
    (both directions) — a spot config emits NO key and stays byte-identical.
    ``canonical_spec_params`` (the spec-axis params builder) deliberately does
    NOT carry ``roll_schedule`` — the spec axis has no futures sweep tree and
    its params vocabulary stays dormant (test-pinned on both builders).

    This is the ONE source of the params layout inside every persisted mask
    run-identity (CLAUDE.md §11 COMMON): the sweep hashes through it via
    :func:`_canonical_params`, and the Stage-3 wiring re-derives survivor
    identities through it (over the ``sl × tp × proximity × fib_anchor`` grid) to
    recover each survivor's grid cell by ``run_id`` match (DEC-STAGE3-WIRE-001,
    hostile-review condition F4 — never guessed, always re-derived).
    """
    if proximity is None:
        proximity_bps: list[int] | None = None
    else:
        proximity_bps = [
            proximity.pivot_proximity_bps,
            proximity.pdh_pdl_proximity_bps,
            proximity.fib_proximity_bps,
        ]
    params: dict[str, Any] = {
        "sl_paisa": sl_paisa,
        "tp_paisa": tp_paisa,
        "prior_n": prior_n,
        "proximity_bps": proximity_bps,
        "fib_anchor": fib_anchor.value,
        "start_equity_paisa": start_equity_paisa,
    }
    # DEFAULT-OMIT the price-source axis (Override 1) via the ONE COMMON fragment
    # (CLAUDE.md §11): absent ⟺ the (HLC3, CLOSE) default, byte-identical identity.
    params.update(price_source_params_fragment(vwap_source, ema_source))
    # DEFAULT-OMIT the swept swing band (DEC-FIB-TEMPLATE-EXPRESS-001) via the
    # ONE COMMON fragment: the ``proximity_bps`` 3-list layout above is FROZEN
    # (never grows to 4 — that would re-key every existing non-None-proximity
    # run); a non-default ``swing_proximity_bps`` adds exactly ONE top-level
    # key, absent ⟺ the default 10 (clean bijection).
    params.update(swing_params_fragment(proximity))
    if roll_schedule is not None:  # presence ⟺ the futures alias (see above)
        params["roll_schedule"] = list(roll_schedule)  # ints — JSON-canonical
    # DEC-FILL-WORST-CASE-ONLY-001: worst-case is the ONLY fill model, and the
    # key is emitted UNCONDITIONALLY. Removing the key instead would ALIAS
    # every worst-case run_id with the frozen precise-era identities (the
    # retired emit-on-non-PRECISE elide law meant "no key = PRECISE legacy"),
    # silently re-serving precise-priced stores as worst-case results. The
    # permanent key makes every post-DEC identity distinct from every
    # precise-era identity by construction (Golden Rule #4 / #7); the
    # verify/recount law is now "fill_mode key MUST be present and MUST read
    # 'worst_case' — anything else is a refused legacy artifact".
    params["fill_mode"] = FillMode.WORST_CASE.value
    # DEC-FILL-EXEC-GRID-1M-001 (design §4.2): the 1-minute execution grid is
    # the ONLY fill grid, and its key is emitted UNCONDITIONALLY beside the
    # fill_mode key with the same anti-aliasing rationale — without it a
    # tf=1minute run under the PR-B law would ALIAS its PR-A-era identity
    # (same bars, same params, same snapshot) while the trades DIFFER (the
    # §2.4 same-bar deferral changes results even in the degenerate case).
    # The verify/recount law is the fill_mode law's twin: "execution_grid MUST
    # be present and MUST read '1m_next_bar' — anything else is a refused
    # decision-TF-era artifact".
    params["execution_grid"] = EXECUTION_GRID_1M_NEXT_BAR
    return params


def _canonical_params(cfg: SweepRunConfig) -> dict[str, Any]:
    """Project *cfg*'s trade-sim knobs through :func:`canonical_trade_params`."""
    return canonical_trade_params(
        sl_paisa=cfg.sl_paisa,
        tp_paisa=cfg.tp_paisa,
        prior_n=cfg.prior_n,
        proximity=cfg.proximity,
        start_equity_paisa=cfg.start_equity_paisa,
        fib_anchor=cfg.fib_anchor,
        vwap_source=cfg.vwap_source,
        ema_source=cfg.ema_source,
        roll_schedule=cfg.roll_schedule,
    )


class _TemporalRuntime:
    """One tuple's temporal-gate runtime (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001).

    Built ONCE per tuple (only when ``cfg.temporal`` is active — the gate-OFF
    arm never constructs one, spy-pinned) by :func:`_build_temporal_runtime`:
    the O(N_bars) IST year-per-bar table, the data-grounded distinct-session
    census, the judged-year set, and the per-mask fold sink
    :func:`_iter_mask_metrics` drives. The sink reduces each kept mask's
    trades to ONE 3-int :class:`~brutex.ranking.temporal.TemporalTriple`
    (≈17 B) and parks it in a one-slot holder the caller drains — the batch
    arm appends it beside ``pairs``; the bounded arm hands it straight to
    ``SurvivorColumns.append`` so no O(kept) triple list is retained there.
    """

    __slots__ = (
        "_acc",
        "_last_triple",
        "base_year",
        "cfg",
        "judged",
        "judged_offsets",
        "n_judged",
        "stats",
        "triples",
        "year_of_bar",
    )

    def __init__(
        self,
        cfg: TemporalRobustnessConfig,
        year_of_bar: Sequence[int],
        base_year: int,
        n_years: int,
        judged: tuple[int, ...],
    ) -> None:
        self.cfg = cfg
        self.year_of_bar = year_of_bar
        self.base_year = base_year
        self.judged = judged
        self.n_judged = len(judged)
        self.judged_offsets = tuple(year - base_year for year in judged)
        self.stats = TemporalGateStats()
        #: The reusable per-mask year accumulator (≤ a handful of slots —
        #: allocated ONCE, zeroed after each reduction; zero retained
        #: per-mask state beyond the 3-int triple).
        self._acc = [0] * max(n_years, 1)
        #: BATCH arm only — the per-candidate triples aligned with ``pairs``.
        self.triples: list[TemporalTriple] = []
        self._last_triple: TemporalTriple | None = None

    def sink(
        self,
        mask: int,
        trades: Sequence[Any],
        exit_indices: Sequence[int] | None,
    ) -> None:
        """Fold ONE kept mask's trades into its per-judged-year triple.

        The FAST arm indexes the pre-built ``year_of_bar`` table by each
        trade's certified exit-bar index — two array indexings + one int add
        per trade — guarded by the exit-index CONSISTENCY BELT (length +
        bounds; the DEC-SWEEP-EXIT-INDEX-FASTPATH-001 demote law): an
        inconsistent stream demotes to the per-trade
        ``ist_trade_date(exit_ts).year`` fold, which is identical by
        construction (``exit_ts`` IS the exit bar's timestamp —
        property-pinned equal), never to a wrong year.
        """
        n_bars = len(self.year_of_bar)
        acc = self._acc
        use_fast = exit_indices is not None and len(exit_indices) == len(trades)
        if use_fast and exit_indices is not None:
            for index in exit_indices:
                if not 0 <= index < n_bars:
                    use_fast = False
                    break
        if use_fast and exit_indices is not None:
            for trade, index in zip(trades, exit_indices, strict=True):
                acc[self.year_of_bar[index] - self.base_year] += trade.pnl_paisa
        else:
            for trade in trades:
                acc[ist_trade_date(trade.exit_ts).year - self.base_year] += trade.pnl_paisa
        profitable = 0
        max_pos = 0
        sum_pos = 0
        for offset in self.judged_offsets:
            pnl = acc[offset]
            if pnl > 0:
                profitable += 1
                sum_pos += pnl
                max_pos = max(max_pos, pnl)
        # Zero the reusable accumulator (O(#window years) ≤ a handful).
        for slot in range(len(acc)):
            acc[slot] = 0
        self._last_triple = TemporalTriple(
            profitable_judged_years=profitable,
            max_pos_year_pnl_paisa=max_pos,
            sum_pos_year_pnl_paisa=sum_pos,
        )

    def take_last(self, mask: int) -> TemporalTriple:
        """Drain the one-slot holder — fail LOUD if the sink never ran (wiring)."""
        triple = self._last_triple
        if triple is None:
            raise SweepComboError(
                f"temporal fold missing for mask 0x{mask:032x} — the sink was not "
                f"invoked for this kept mask (wiring bug), never a silent pass-all"
            )
        self._last_triple = None
        return triple

    def gate_input(self) -> TemporalGateInput:
        """The BATCH rank seam bundle (triples aligned with ``pairs``)."""
        return TemporalGateInput(
            cfg=self.cfg, n_judged_years=self.n_judged, triples=self.triples, stats=self.stats
        )

    def columns_gate_input(self) -> TemporalGateInput:
        """The BOUNDED rank seam bundle (triples live in the SurvivorColumns)."""
        return TemporalGateInput(
            cfg=self.cfg, n_judged_years=self.n_judged, triples=None, stats=self.stats
        )


def _build_temporal_runtime(
    bars: Sequence[CandleBar], cfg: TemporalRobustnessConfig
) -> _TemporalRuntime:
    """ONE O(N_bars) pass: the year-per-bar table + the judged-year census.

    ``year_of_bar`` maps each bar index to its IST calendar year (the
    :func:`~brutex.engine.session_lens.ist_trade_date` law — the ONE UTC→IST
    conversion boundary); ``sessions_per_year`` counts DISTINCT IST session
    dates per year from the tuple's OWN bars (data-grounded — immune to window
    holes; bars are chronological, so distinct dates are date transitions),
    feeding the :func:`~brutex.ranking.temporal.judged_years` edge-year rule.
    """
    from array import array

    year_of_bar = array("h")
    sessions_per_year: dict[int, int] = {}
    last_date = None
    for bar in bars:
        bar_date = ist_trade_date(bar.timestamp)
        year_of_bar.append(bar_date.year)
        if bar_date != last_date:
            sessions_per_year[bar_date.year] = sessions_per_year.get(bar_date.year, 0) + 1
            last_date = bar_date
    judged = judged_years(sessions_per_year, cfg)
    base_year = min(sessions_per_year) if sessions_per_year else 0
    n_years = (max(sessions_per_year) - base_year + 1) if sessions_per_year else 1
    return _TemporalRuntime(cfg, year_of_bar, base_year, n_years, judged)


def ranked_combo_to_result_row(
    rc: RankedCombo,
    *,
    run_id: str,
    instrument: str,
    tf: str,
    direction: str,
    mode: str,
    bars: Sequence[CandleBar],
    bar_bits: Sequence[int],
    tf_minutes: int,
    sl_paisa: int | None,
    tp_paisa: int | None,
    exec_bars_1m: Sequence[CandleBar],
    exec_map: ExecGridMap | None = None,
    reference_stamper: TradeReferenceStamper | None = None,
    temporal: TemporalRobustnessConfig | None = None,
    temporal_judged_years: Sequence[int] | None = None,
    start_equity_paisa: int = DEFAULT_START_EQUITY_PAISA,
    selection_basis: str | None = None,
    pareto_member: bool | None = None,
    gate_provenance: str | None = None,
) -> ResultRow:
    """Project a :class:`~brutex.ranking.RankedCombo` into a storable :class:`ResultRow`.

    The seam between the ranked leaderboard (rich metrics, no storage discipline)
    and the deterministic store (money/metric/price families + the readable-mask
    surface). The winning combo is stamped two ways (DEC-SWEEP-WINNERS-BY-NAME-001):
    ``mask_bits=rc.mask`` (the machine-readable u64) and — derived from it inside
    :class:`ResultRow` via the single-source-of-truth vocabulary name table — the
    human-readable ``condition_names``; ``rc.deflated_sharpe`` is carried through for
    leaderboard transparency (``None`` when the pool was not deflatable). HONEST
    SCOPE (DEC-STATS-GATE-INTEGRITY-001, R2-SR-F3): the carried figure is
    **TUPLE-scoped** (``rc.dsr_n_scope == "tuple"`` whenever it is non-``None`` —
    deflated against ONE grid cell's ``N = len(candidates)``, an inflating
    under-count vs the true leaderboard N; measured: DSR 0.230 at ``n_trials=1e3``
    vs 0.003 at ``1e7``); the persisted row's value is superseded by the Stage-4
    cascade's leaderboard-N DSR gate and is labelled ``dsr@tupleN`` wherever
    rendered (ADR-0008 condition 7). The rest of
    the mapping is mechanical except two deliberate choices:

    * **``None`` metrics → ``0.0``.** ``ResultMetrics`` carries ``sortino_ratio``
      / ``calmar_ratio`` / ``profit_factor`` as ``float | None`` (mathematically
      undefined for the sample), but ``ResultRow``'s metric columns are
      non-optional ``float``. A ranked combo has already cleared the gates (so
      ``profit_factor`` is real), but the others can still be ``None``; they map
      to ``0.0`` (documented, not silent). The full-precision real values are
      preserved unchanged.
    * **Representative price anchors from REAL OHLC (Golden Rule #1).**
      ``ResultMetrics`` carries no per-trade fill prices, so the finalist's
      trades are re-simulated here and ``avg_entry_price`` / ``avg_exit_price``
      are the mean of the real ``open`` (at each trade's entry 1m bar) /
      ``close`` (at each trade's exit 1m bar) — read over the EXECUTION
      stream (DEC-FILL-EXEC-GRID-1M-001 §Q1: the fills resolve on 1m bars,
      so the anchors read the bars that actually filled; open/close anchor
      semantics KEPT), snapped to the 2dp tick grid via
      :func:`brutex.money.round_money`. These are HONEST anchors drawn from real
      bars, never fabricated. Threading the exact fill price through
      :class:`~brutex.metrics.types.ClosedTrade` is a tracked follow-up. A
      sweep-ranked combo always has ``>= MIN_TRADES`` trades; a Stage-3 refined
      evaluation (DEC-STAGE3-WIRE-001) is UNGATED and may have zero trades, in
      which case both anchors are the 0.00 placeholder
      (``round_money(Decimal("0"))``) — documented, not fabricated.

      This re-simulation deliberately stays on the pure-Python
      :func:`~brutex.engine.trade_sim.simulate_trades` ORACLE — an explicit
      DEC-SWEEP-RUST-TRADESIM-WIRE-001 non-goal, NOT an oversight: it runs once
      per top-K FINALIST (low volume; the ~33B-trial hot path is the per-mask
      sweep loop, not this projector). Since that loop is now the resident Rust
      kernel, the "reproduce the ranked combo's trades exactly" contract above
      spans TWO engines — it holds precisely because the kernel is
      bit-identity-proven against this same oracle (45,863/45,863 trades, all 9
      fields; the wired-vs-unwired store-hash golden trips on any drift, since
      these price anchors are part of the hashed store bytes).

    Parameters
    ----------
    rc
        The ranked combo to project (its ``mask`` + full ``metrics``).
    run_id
        The 64-char BLAKE3 run-identity for ``rc.mask`` (the store dedup key).
    instrument, tf, direction, mode
        Context labels carried onto the row.
    bars, bar_bits
        The same bars + pre-built Stage-1A matrix the sweep used (for the price
        re-simulation; ``bar_bits`` is reused, not rebuilt).
    tf_minutes, sl_paisa, tp_paisa
        Trade-sim knobs — must match the sweep's so the re-simulated trades
        reproduce the ranked combo's trades exactly. The fill law itself is
        NOT a knob: worst-case is the only fill model
        (DEC-FILL-WORST-CASE-ONLY-001), so the re-simulation prices under the
        same hardcoded adverse-max law the sweep did by construction.
    reference_stamper
        Optional India-VIX
        :class:`~brutex.engine.reference_series.TradeReferenceStamper`
        (DEC-VIX-STAMP-WIRE-001): the FINALIST projection is the OBSERVABLE
        re-simulation feeding the persisted row, so the run's stamper is bound
        HERE — the finalist's trades carry ``vix_at_entry`` / ``vix_at_exit``,
        which the ONE COMMON
        :func:`~brutex.engine.reference_wire.aggregate_trade_vix` folds into
        the row's nullable ``vix_mean`` / ``vix_min`` / ``vix_max`` trio.
        REFERENCE ONLY (CLAUDE.md §2 Rule 3): the trio is never rankable and
        never in the run identity — the row's ``run_id`` is byte-identical
        with or without stamping (regression-pinned); ONLY the three nullable
        VIX columns differ. ``None`` (default) = unstamped → the honest
        ``(None, None, None)`` trio, never a fabricated level.
    temporal
        The tuple's :class:`~brutex.ranking.temporal.TemporalRobustnessConfig`
        (both modes — ``observe`` captures too), or ``None`` for no capture
        (byte-identical legacy row — DEC-SWEEP-TEMPORAL-ROBUSTNESS-001). When
        active AND the finalist has trades, the re-simulated stream is folded
        through :func:`brutex.engine.period_stats.fold_period_stats` at the
        accepted O(finalists) re-sim seam (zero hot-path cost) and the four
        optional temporal columns are stamped. A ZERO-trade finalist (a
        Stage-3 refined row) stores ``None`` — honest, matching its
        placeholder price anchors.
    temporal_judged_years
        The tuple's judged-year set (from the per-tuple census) — stored as
        the serve-time denominator. Ignored when *temporal* is ``None``.
    start_equity_paisa
        The equity base anchoring the per-period dd walk (the
        ``build_equity_curve`` law; the dd values are invariant to the
        constant offset — threaded for basis-law fidelity).
    selection_basis, pareto_member
        The LANE-W1 selection-provenance stamps (DEC-RANK-PARETO-PERSIST-001 /
        DEC-RANK-QUOTA-PERSIST-1000-001) computed ONCE per ranked pool by the
        union composer at the persist seam; ``None`` (the defaults — every
        pre-W1 caller) keeps the row untagged. REFERENCE ONLY: never rankable,
        never in the run identity (:data:`brutex.engine.results_store.SELECTION_FIELDS`
        is excluded from ``_RANKABLE_FIELDS`` and validated at construction).
    gate_provenance
        Optional gate-scoped-dedup provenance token
        (DEC-SWEEP-SERVING-GATE-PROVENANCE-001) stamped verbatim onto the
        row's nullable ``gate_provenance`` carrier — the producing grid's
        canonical gate+selection identity string
        (:func:`brutex.engine.sweep_orchestrator.grid_gate_provenance`,
        threaded here via ``SweepRunConfig.gate_provenance``). ``None`` (the
        default — every ungated grid and every legacy caller) is
        byte-identical to before: no row token, no store column. NEVER an
        input to the run identity and NOT the statutory 5-door gate re-check.

    Returns
    -------
    ResultRow
        The validated, price-snapped row ready for
        :meth:`brutex.engine.results_store.ResultsStore.append`.
    """
    m = rc.metrics
    side = Side.BUY if direction == "bull" else Side.SELL
    trades = simulate_trades(
        bars,
        mask_to_entry_signals(bar_bits, rc.mask),
        direction=side,
        tf_minutes=tf_minutes,
        exec_bars_1m=exec_bars_1m,
        exec_map=exec_map,
        stop_loss_paisa=sl_paisa,
        take_profit_paisa=tp_paisa,
        reference_stamper=reference_stamper,
    )
    if trades:
        # Keep-FIRST timestamp map (DEC-LAKE-READ-UNIQUE-001): a plain dict
        # comprehension is silently LAST-wins on a duplicated bar timestamp, so
        # a poisoned-era duplicate would swap the REAL entry/exit price anchor
        # for the re-stated one. The COMMON gate keeps the first bar + WARNs.
        ts_to_bar = bars_by_ts_first(
            exec_bars_1m, source=f"sweep_run:{instrument}/{tf}/{direction}"
        )
        entries = [ts_to_bar[t.entry_ts].open for t in trades]
        exits = [ts_to_bar[t.exit_ts].close for t in trades]
        avg_entry = round_money(sum(entries, Decimal("0")) / len(entries))
        avg_exit = round_money(sum(exits, Decimal("0")) / len(exits))
    else:
        # REACHABLE since DEC-STAGE3-WIRE-001: a Stage-3 refined evaluation is
        # an UNGATED Bailey-LdP trial (ADR-0007 condition 6) and may close zero
        # trades on the training slice; its price anchors are then a 0.00
        # placeholder pair (a sweep-ranked combo still always has >= MIN_TRADES).
        avg_entry = round_money(Decimal("0"))
        avg_exit = round_money(Decimal("0"))

    # The per-combo VIX summary (DEC-VIX-STAMP-WIRE-001): the ONE COMMON fold
    # over the finalist's stamped trades. Unstamped / all-miss / zero trades →
    # the honest (None, None, None) — never fabricated (Golden Rule #1).
    vix_mean, vix_min, vix_max = aggregate_trade_vix(trades)

    # Tier-1 per-period capture (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001, both
    # modes): folded from the SAME re-simulated trades above — cross-sum
    # consistency with the row's own totals is guaranteed by construction
    # (the store validator re-proves it at append). Zero-trade finalists
    # store None — honest, never fabricated empties.
    year_json: str | None = None
    quarter_json: str | None = None
    month_json: str | None = None
    judged_json: str | None = None
    if temporal is not None and trades:
        period = fold_period_stats(trades, start_equity_paisa=start_equity_paisa)
        year_json = encode_period_stats(period.by_year)
        quarter_json = encode_period_stats(period.by_quarter)
        month_json = encode_period_stats(period.by_month)
        judged_json = encode_judged_years(
            temporal_judged_years if temporal_judged_years is not None else ()
        )

    return ResultRow(
        run_id=run_id,
        instrument=instrument,
        tf=tf,
        direction=direction,
        mode=mode,
        mask_bits=rc.mask,
        deflated_sharpe=rc.deflated_sharpe,
        net_pnl_paisa=m.net_pnl_paisa,
        gross_profit_paisa=m.gross_profit_paisa,
        gross_loss_paisa=abs(m.gross_loss_paisa),
        max_drawdown_paisa=abs(m.max_drawdown_paisa),
        expectancy_paisa=m.expectancy_paisa,
        total_trades=m.total_trades,
        winning_trades=m.winning_trades,
        losing_trades=m.losing_trades,
        sharpe_ratio=_metric_or_zero(m.sharpe_ratio),
        sortino_ratio=_metric_or_zero(m.sortino_ratio),
        calmar_ratio=_metric_or_zero(m.calmar_ratio),
        profit_factor=_metric_or_zero(m.profit_factor),
        win_rate=m.win_rate,
        max_drawdown_pct=m.max_drawdown_pct,
        avg_entry_price=avg_entry,
        avg_exit_price=avg_exit,
        vix_mean=vix_mean,
        vix_min=vix_min,
        vix_max=vix_max,
        stats_by_year_json=year_json,
        stats_by_quarter_json=quarter_json,
        stats_by_month_json=month_json,
        temporal_judged_years_json=judged_json,
        # The loss/growth trio (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 /
        # DEC-METRICS-KELLY-GROWTH-001): loss_rate is total on ResultMetrics;
        # the Kelly pair rides the SAME _metric_or_zero None → 0.0 coercion
        # the rankable profit_factor column has always ridden (the mirrored
        # precedent — design-pack ruling), so a row written by current code
        # always carries concrete floats and a stored None can only mean a
        # pre-extension file (the Ruling-5 read boundary).
        loss_rate=m.loss_rate,
        kelly_fraction=_metric_or_zero(m.kelly_fraction),
        log_growth_at_half_kelly=_metric_or_zero(m.log_growth_at_half_kelly),
        # sl_hit_rate (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B) is a
        # PLAIN pass-through — deliberately NO _metric_or_zero coercion: a
        # None here means the metrics were computed WITHOUT the exit-reason
        # counters ("reasons unavailable"), and coercing it to 0.0 would
        # fabricate a perfect-looking SL record (Golden Rule #1). The sweep
        # spine always threads the counters, so every sweep-persisted row's
        # column is concrete (test-pinned); None reaches disk only from a
        # counter-less legacy caller — the same honest-unknown a
        # pre-extension store reads back.
        sl_hit_rate=m.sl_hit_rate,
        # The recency family (DEC-METRICS-TRAILING-WINDOW-001 Stage C) is a
        # PLAIN pass-through — NO _metric_or_zero coercion: None means the
        # GLOBAL trailing anchor was absent (a legacy caller), or — for the
        # trailing trio — the 24-month window honestly held zero trades;
        # coercing either to 0.0 would fabricate a measured recency figure
        # (Golden Rule #1). The sweep spine always threads the anchor, so a
        # sweep-persisted row's decay composite is concrete (test-pinned)
        # while its trailing trio is concrete exactly when the window held
        # trades. The exact int trailing net rides the row's nullable
        # Float64 lane (exact below 2**53 paisa).
        trailing_sharpe_ratio=m.trailing_sharpe_ratio,
        trailing_profit_factor=m.trailing_profit_factor,
        trailing_net_pnl_paisa=(
            None if m.trailing_net_pnl_paisa is None else float(m.trailing_net_pnl_paisa)
        ),
        decay_weighted_net_paisa=m.decay_weighted_net_paisa,
        pareto_member=pareto_member,
        selection_basis=selection_basis,
        gate_provenance=gate_provenance,
    )


def _metric_or_zero(value: float | None) -> float:
    """Map a ``float | None`` metric to a non-optional float (``None`` → ``0.0``).

    ``ResultMetrics`` ratios are ``None`` when mathematically undefined for the
    sample; the ``ResultRow`` schema's metric columns are non-optional, so a
    ``None`` becomes ``0.0`` (documented in :func:`ranked_combo_to_result_row`).
    Real values pass through with full precision.
    """
    return value if value is not None else 0.0


def _sentinel_resume(store_path: Path, digest: str) -> TupleCompletionSentinel | None:
    """Return the tuple's PAIRED completion sentinel, or ``None`` to force a full redo.

    The redo decision authority (DEC-MARK-MANY-SENTINEL-001): COMPLETE iff the
    sentinel exists, reads valid, carries this tuple's *digest*, its recorded
    ``store_content_hash`` matches a re-hash of the actual store bytes, and —
    when it records a written trial-stats sidecar — that sidecar still reads
    and pairs. ANY failure is logged loudly (``sweep_run.sentinel_unpaired``)
    and returns ``None``: the caller re-runs the FULL population and rewrites
    every artifact, so a stale/tampered/corrupt state self-heals rather than
    being falsely skipped or crashing the grid.
    """
    path = completion_sentinel_path(store_path)
    if not path.exists():
        return None

    def _unpaired(reason: str) -> None:
        _LOG.warning("sweep_run.sentinel_unpaired", path=str(path), reason=reason)

    try:
        sentinel = read_completion_sentinel(path)
    except TupleCompletionError as exc:
        _unpaired(f"unreadable sentinel: {exc}")
        return None
    if sentinel.tuple_digest != digest:
        _unpaired("tuple digest mismatch")
        return None
    try:
        content = read_regular_bytes(store_path)
    except OSError as exc:
        _unpaired(f"store unreadable: {exc}")
        return None
    if hashlib.sha256(content).hexdigest() != sentinel.store_content_hash:
        _unpaired("store content hash mismatch")
        return None
    if sentinel.sidecar_written:
        try:
            stats = read_tuple_trial_stats(trial_stats_sidecar_path(store_path))
        except TrialStatsError as exc:
            _unpaired(f"sidecar unreadable: {exc}")
            return None
        if stats.tuple_digest != digest or stats.store_content_hash != sentinel.store_content_hash:
            _unpaired("sidecar pairing mismatch")
            return None
    return sentinel


def _combo_error(
    mask: int, run_id: str, cfg: SweepRunConfig, direction_label: str, exc: BaseException
) -> SweepComboError:
    """Build the fail-loud-with-fingerprint :class:`SweepComboError` for ONE combo.

    ONE source of the fingerprint format (CLAUDE.md §11 COMMON) — mask hex +
    run-id + tuple coordinate + k + the original error — shared by the wired
    per-mask metrics tail, the chunk-failure re-attribution, and (verbatim) the
    pre-wiring per-mask loop's message, so grid TUPLE_FAILED audit lines stay
    format-identical across the Rust wiring.
    """
    return SweepComboError(
        f"combo 0x{mask:032x} (run_id={run_id}) in "
        f"{cfg.instrument}/{cfg.tf}/{direction_label} k={cfg.k}: {exc}"
    )


def _build_trade_sim(
    bars: Sequence[CandleBar],
    bar_bits: Sequence[int],
    cfg: SweepRunConfig,
    direction_label: str,
    *,
    exec_bars_1m: Sequence[CandleBar],
    exec_map: ExecGridMap | None,
    reference_stamper: TradeReferenceStamper | None,
    kernel_columns: PriceColumns | None = None,
) -> ResidentTradeSim:
    """Build the tuple's resident trade-sim; map a failure to the #904 tuple-skip contract.

    Built ONCE per tuple, AFTER the kernel n_bars/fib_anchor guards (so
    ``bar_bits`` is final) and only when survivors exist. Construction converts
    all four OHLC columns eagerly (the old per-mask walk converted lazily, only
    bars a trade touched), so a corrupt price ANYWHERE in the window now fails
    the tuple LOUDLY at construction instead of completing silently when no
    trade happened to touch it (Golden Rule #1) — and the failure lands as a
    :class:`SweepComboError` (grid drivers: tuple skip + ``TUPLE_FAILED`` +
    non-zero verdict, DEC-ENGINE-TUPLE-SKIP-RESILIENT-001), NEVER a grid-wide
    abort. ``MemoryError`` re-raises BARE first (OOM = GLOBAL stop,
    DEC-LAKE-WRITE-HALT-001); ``KeyboardInterrupt``/``BaseException`` escape the
    ``except Exception`` untouched. The identity guards the co-located-cache
    design would have needed (tf_minutes/fib_anchor/n_bars consistency) are
    structurally moot here: the sim is built from THIS tuple's own
    ``bars``/``bar_bits``/``cfg.tf_minutes``, never cached across tuples.

    The path choice is logged (``sweep_run.trade_sim_path``, INFO) so a
    Python-degraded sweep is observable, not a silent 40× regression (Golden
    Rule #10); the wrapper itself already WARNs on every fallback cause. The
    log also carries ``vix_stamped`` (DEC-VIX-STAMP-WIRE-001) so an unstamped
    inner stream is a stated fact, never a silent omission.

    ``reference_stamper`` is REQUIRED (no default) so every caller makes an
    EXPLICIT stamping decision for the trades this sim will emit — the
    one-kwarg revert lever the coordinator ruling demands.

    ``kernel_columns`` (the W2 rider of DEC-STAGE1A-RUST-BATCH-001, rebased
    onto the 1m EXECUTION stream by DEC-FILL-EXEC-GRID-1M-001) threads a
    :class:`~brutex.engine.sweep_kernel.ResidentSweepKernel`'s once-per-window
    EXECUTION-stream :class:`~brutex.engine.rust_trade_sim.PriceColumns` into
    the sim so a kernel-reusing tuple skips the per-tuple 4×-Decimal-snap
    derivation over the (much longer) 1m stream; ``None`` (the default —
    every kernel-less caller) derives locally, byte-identical.
    """
    try:
        sim = ResidentTradeSim(
            bars,
            bar_bits,
            tf_minutes=cfg.tf_minutes,
            exec_bars_1m=exec_bars_1m,
            exec_map=exec_map,
            reference_stamper=reference_stamper,
            precomputed_columns=kernel_columns,
        )
    except MemoryError:
        # OOM is a GLOBAL stop (DEC-LAKE-WRITE-HALT-001), never a tuple skip.
        raise
    except Exception as exc:
        raise SweepComboError(
            f"trade-sim construction over {len(bars)} bars in "
            f"{cfg.instrument}/{cfg.tf}/{direction_label} k={cfg.k}: {exc}"
        ) from exc
    _LOG.info(
        "sweep_run.trade_sim_path",
        rust_active=sim.rust_active,
        vix_stamped=sim.reference_stamper is not None,
        n_bars=sim.n_bars,
        chunk_masks=_trade_sim_chunk_masks(sim.n_bars),
        instrument=cfg.instrument,
        tf=cfg.tf,
        direction=direction_label,
        k=cfg.k,
    )
    return sim


def _reattribute_chunk_failure(
    bars: Sequence[CandleBar],
    bar_bits: Sequence[int],
    chunk: Sequence[int],
    cfg: SweepRunConfig,
    direction_label: str,
    batch_exc: Exception,
    run_id_of: Callable[[int], str],
    *,
    exec_bars_1m: Sequence[CandleBar],
    exec_map: ExecGridMap | None,
    reference_stamper: TradeReferenceStamper | None,
) -> NoReturn:
    """A batched trade-sim chunk raised — attribute the culprit mask, then fail LOUD.

    The batch call loses per-mask attribution, so the failed chunk is re-run one
    mask at a time through the ORACLE (:func:`metrics_for_mask` — the exact
    pre-wiring per-mask chain, engine-agnostic: it attributes a Python-fallback
    chunk failure identically). The FIRST mask that re-raises gets the standard
    fingerprint (:func:`_combo_error`, chained from ITS error), its run-identity
    resolved via *run_id_of* (lazy recompute on the digest path, the eager
    probe-time value on the legacy path — byte-identical either way because
    :func:`mask_run_id` is pure; DEC-SWEEP-STREAMING-RANK-001). ``MemoryError``
    during re-attribution re-raises BARE (the global-stop law holds under
    memory pressure too — never a downgrade to a tuple skip).
    ``reference_stamper`` is the SIM'S OWN stamper, handed in by
    :func:`_iter_mask_metrics` (DEC-VIX-STAMP-WIRE-001) so the oracle replay
    simulates with the EXACT stamping input the failed batch used — the
    STRUCTURAL oracle≡batch guarantee (required, no default: a second wiring
    could silently diverge).

    If EVERY mask passes the oracle, the failure was batch-level (a PyO3
    marshalling error, a kernel-only validator, a kernel bug): the oracle's
    results are **NEVER adopted** — silently swapping engines mid-tuple would
    mask a kernel-divergence signal and make engine provenance
    non-deterministic across runs (Golden Rules #4/#10). Instead a
    :class:`SweepComboError` naming the CHUNK (size + first/last mask hex +
    tuple + the original error) raises, chained from the ORIGINAL batch
    exception — the grid skips the tuple for a deterministic redo.
    """
    _LOG.warning(
        "sweep_run.trade_sim_chunk_failed",
        n_masks=len(chunk),
        error=str(batch_exc),
        error_type=type(batch_exc).__name__,
        instrument=cfg.instrument,
        tf=cfg.tf,
        direction=direction_label,
        k=cfg.k,
    )
    for mask in chunk:
        try:
            metrics_for_mask(
                bars,
                bar_bits,
                mask,
                direction=cfg.direction,
                tf_minutes=cfg.tf_minutes,
                exec_bars_1m=exec_bars_1m,
                exec_map=exec_map,
                stop_loss_paisa=cfg.sl_paisa,
                take_profit_paisa=cfg.tp_paisa,
                start_equity_paisa=cfg.start_equity_paisa,
                reference_stamper=reference_stamper,
            )
        except MemoryError:
            raise
        except Exception as exc:
            raise _combo_error(mask, run_id_of(mask), cfg, direction_label, exc) from exc
    raise SweepComboError(
        f"trade-sim chunk of {len(chunk)} masks "
        f"(0x{chunk[0]:032x}..0x{chunk[-1]:032x}) in "
        f"{cfg.instrument}/{cfg.tf}/{direction_label} k={cfg.k}: batched simulation raised "
        f"{type(batch_exc).__name__}: {batch_exc}, but every mask passed the per-mask "
        f"Python-oracle re-run — a batch/kernel-level failure; the oracle results are NOT "
        f"adopted (engine provenance stays deterministic) and the tuple is skipped for redo"
    ) from batch_exc


def _iter_mask_metrics(
    sim: ResidentTradeSim,
    masks: Sequence[int],
    bars: Sequence[CandleBar],
    bar_bits: Sequence[int],
    cfg: SweepRunConfig,
    direction_label: str,
    run_id_of: Callable[[int], str],
    *,
    temporal_sink: Callable[[int, Sequence[Any], Sequence[int] | None], None] | None = None,
    cvar_gate: CvarGateConfig | None = None,
    on_cvar_reject: Callable[[int], None] | None = None,
) -> Iterator[tuple[int, ResultMetrics]]:
    """Yield ``(mask, metrics)`` for *masks* in order — THE one chunked eval spine.

    The factored Phase-C body (DEC-SWEEP-STREAMING-RANK-001, CLAUDE.md §11
    COMMON): chunked
    :meth:`~brutex.engine.rust_trade_sim.ResidentTradeSim.simulate_masks_with_exit_counts`
    batches — each mask's ``(trades, exit_indices, exit_counts)`` triple, the
    certified exit indices threaded into the per-mask
    :func:`~brutex.engine.sweep_driver.metrics_from_trades` tail so its
    kernel arm skips the per-mask ``exit_ts``→index dict build
    (DEC-SWEEP-EXIT-INDEX-FASTPATH-001; a ``None`` stream demotes to the
    dict, results byte-identical) and the exit-reason counters threaded so
    the metrics carry a concrete ``sl_hit_rate``
    (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B — the sweep spine ALWAYS
    supplies them, so every persisted row's column is concrete) — shared
    VERBATIM by the batch rank
    accumulation, the bounded columnar accumulation, AND the bounded
    finalists' O(top_k) metrics recompute — one spine, so the three
    consumers can never drift (the recomputed finalist metrics are
    bit-identical to the streamed pass because the batch simulation is
    per-mask independent and deterministic).

    Error contract UNCHANGED from the pre-factor inline loop: a batch failure
    goes through :func:`_reattribute_chunk_failure` (per-mask oracle replay →
    the exact :class:`SweepComboError` fingerprint, or the loud chunk-level
    error); a metrics-tail failure raises :func:`_combo_error`; ``MemoryError``
    re-raises BARE at every level (OOM = GLOBAL stop, DEC-LAKE-WRITE-HALT-001).

    ``temporal_sink`` (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001, kw-only, default
    ``None`` = byte-identical control flow — zero allocation, zero branch
    taken beyond one ``is None`` test; the gate-OFF spy pin): when set, it is
    invoked ONCE per yielded mask with ``(mask, trades, exit_indices)`` —
    inside the metrics try-block, so a sink failure rides the SAME
    :func:`_combo_error` tuple-skip fingerprint as a metrics-tail failure.
    """
    chunk_size = _trade_sim_chunk_masks(len(bars))
    # The GLOBAL recency anchor (DEC-METRICS-TRAILING-WINDOW-001) — hoisted +
    # empty-guarded (refuter round 1): an EMPTY bars sequence must keep the
    # PRE-recency failure shape (the metrics tail's own empty-bars
    # ValueError, ridden into _combo_error), never a new ``bars[-1]``
    # IndexError from the anchor read.
    trailing_anchor = bars[-1].timestamp if bars else None
    for chunk_start in range(0, len(masks), chunk_size):
        chunk = masks[chunk_start : chunk_start + chunk_size]
        try:
            chunk_triples = sim.simulate_masks_with_exit_counts(
                chunk,
                direction=cfg.direction,
                stop_loss_paisa=cfg.sl_paisa,
                take_profit_paisa=cfg.tp_paisa,
            )
            # Batch-contract guard: the batch call must RETURN exactly one
            # (trades, exit_indices, exit_counts) triple per mask (both arms
            # do, by construction). A wrong-length return (a PyO3 marshalling
            # bug, a kernel bug) raises HERE — inside the chunk try — so it
            # rides the SAME re-attribution → SweepComboError tuple-skip
            # ladder as a chunk that RAISES (the oracle passes every mask, so
            # it fails LOUD naming the chunk), instead of escaping
            # run_persisted_sweep as a bare error the grid drivers (#904)
            # would turn into a grid-wide abort. The `strict=True` zip below
            # is then a structurally-unreachable second belt. The kernel arm's
            # own sum(exit_counts)==len(trades) validation raises inside this
            # SAME try, riding the identical ladder.
            if len(chunk_triples) != len(chunk):
                raise ValueError(
                    f"simulate_masks_with_exit_counts returned {len(chunk_triples)} "
                    f"trade/exit-index/exit-count triples for a chunk of {len(chunk)} "
                    f"masks — batch-contract violation"
                )
        except MemoryError:
            raise
        except Exception as exc:
            # The sim's OWN stamper rides into the oracle replay — the
            # STRUCTURAL oracle≡batch stamping guarantee (DEC-VIX-STAMP-WIRE-001).
            _reattribute_chunk_failure(
                bars,
                bar_bits,
                chunk,
                cfg,
                direction_label,
                exc,
                run_id_of,
                # The sim's OWN exec stream + map ride into the oracle replay
                # (DEC-FILL-EXEC-GRID-1M-001): the replay walks the EXACT
                # dual feed the failed batch walked.
                exec_bars_1m=sim.exec_bars,
                exec_map=sim.exec_map,
                reference_stamper=sim.reference_stamper,
            )
        for mask, (trades, exit_indices, exit_counts) in zip(chunk, chunk_triples, strict=True):
            if cvar_gate is not None:
                # DEC-RANK-CVAR-TAIL-GATE-001: the CVaR tail gate judges the
                # mask's per-trade P&L list HERE — the one point the trades
                # exist — and a fat-tail mask is dropped before the metrics
                # tail (never yielded, so it reaches neither ranking nor the
                # overfit trial pool — the disclosed post-CVaR-N interaction).
                # An empty trade list ABSTAINS inside cvar_rejects. A raise is
                # a per-combo fault (the metrics-tail error contract);
                # MemoryError re-raises BARE (OOM = GLOBAL stop).
                try:
                    rejected = cvar_rejects([t.pnl_paisa for t in trades], cvar_gate)
                except MemoryError:
                    raise
                except Exception as exc:
                    raise _combo_error(mask, run_id_of(mask), cfg, direction_label, exc) from exc
                if rejected:
                    if on_cvar_reject is not None:
                        on_cvar_reject(mask)
                    continue
            try:
                metrics = metrics_from_trades(
                    bars,
                    trades,
                    start_equity_paisa=cfg.start_equity_paisa,
                    tf_minutes=cfg.tf_minutes,
                    exec_bars_1m=sim.exec_bars,
                    exec_map=sim.exec_map,
                    exit_indices=exit_indices,
                    exit_reason_counts=exit_counts,
                    # The loaded bar range's END instant — one value per launch
                    # (every mask shares the tuple's full bars), NEVER a
                    # per-combo max exit_ts. Hoisted above the loop.
                    trailing_anchor=trailing_anchor,
                )
                if temporal_sink is not None:
                    temporal_sink(mask, trades, exit_indices)
            except MemoryError:
                raise
            except Exception as exc:
                raise _combo_error(mask, run_id_of(mask), cfg, direction_label, exc) from exc
            yield mask, metrics


def _selection_inputs_from_ranked(ranked: Sequence[RankedCombo]) -> SelectionInputs:
    """Project a composite-ordered leaderboard into the union composer's columns.

    The BATCH arm's :class:`~brutex.ranking.multi_select.SelectionInputs`
    builder (LANE W1): ``rank_combos`` already returns the pool in the
    ``(-composite, mask)`` order the composer's prefix/over-cap laws demand,
    so the projection is a mechanical column pull — O(ranked).
    """
    return SelectionInputs(
        masks=tuple(rc.mask for rc in ranked),
        composites=tuple(rc.composite_score for rc in ranked),
        net_pnl_paisa=tuple(rc.metrics.net_pnl_paisa for rc in ranked),
        win_rate=tuple(rc.metrics.win_rate for rc in ranked),
        max_dd_pct=tuple(rc.metrics.max_drawdown_pct for rc in ranked),
        losing_trades=tuple(rc.metrics.losing_trades for rc in ranked),
        expectancy_paisa=tuple(rc.metrics.expectancy_paisa for rc in ranked),
    )


def _rebuild_bounded_finalists(
    selected: BoundedRankResult,
    sim: ResidentTradeSim | None,
    bars: Sequence[CandleBar],
    bar_bits: Sequence[int],
    cfg: SweepRunConfig,
    direction_label: str,
    run_id_of: Callable[[int], str],
) -> list[RankedCombo]:
    """Recompute the bounded finalists' full ``ResultMetrics`` — O(top_k), same spine.

    The bounded rank streams composites without retaining any
    :class:`~brutex.metrics.compute.ResultMetrics`; the ≤ ``top_k`` finalists'
    metrics are recomputed here through the SAME :func:`_iter_mask_metrics`
    spine the stream used (deterministic + per-mask independent, so the values
    are bit-identical to the streamed pass — the established finalist
    re-simulation precedent, spec V16), then re-married to the streamed
    composite + DSR into ordinary :class:`~brutex.ranking.RankedCombo`s so the
    UNCHANGED ``ranked_combo_to_result_row → append → flush`` chain runs.
    """
    if not selected.finalists:
        return []
    if sim is None:
        # Structurally unreachable via run_persisted_sweep (finalists ⊆ kept,
        # and the sim is built whenever kept is non-empty) — a defensive,
        # tested guard so a future wiring regression fails LOUD as a tuple
        # skip, never an AttributeError mid-persist.
        raise SweepComboError(
            f"bounded finalist recompute in {cfg.instrument}/{cfg.tf}/{direction_label} "
            f"k={cfg.k}: no trade-sim was built for this tuple (finalists without kept "
            f"masks — a wiring bug)"
        )
    finalist_masks = [finalist.mask for finalist in selected.finalists]
    rebuilt: list[RankedCombo] = []
    for finalist, (mask, metrics) in zip(
        selected.finalists,
        _iter_mask_metrics(sim, finalist_masks, bars, bar_bits, cfg, direction_label, run_id_of),
        strict=True,
    ):
        # _iter_mask_metrics preserves input order, so the pairing is positional;
        # the equality is a belt against a future spine reorder.
        if mask != finalist.mask:
            raise SweepComboError(
                f"bounded finalist recompute in {cfg.instrument}/{cfg.tf}/{direction_label} "
                f"k={cfg.k}: metrics stream returned 0x{mask:032x} where "
                f"0x{finalist.mask:032x} was requested — order contract violated"
            )
        rebuilt.append(
            RankedCombo(
                mask=finalist.mask,
                metrics=metrics,
                composite_score=finalist.composite_score,
                deflated_sharpe=finalist.deflated_sharpe,
            )
        )
    return rebuilt


def run_persisted_sweep(
    bars: Sequence[CandleBar],
    cfg: SweepRunConfig,
    *,
    store: ResultsStore,
    dedup: DedupSink,
    audit: AuditSink,
    exec_bars_1m: Sequence[CandleBar],
    exec_map: ExecGridMap | None = None,
    kernel: ResidentSweepKernel | None = None,
    trial_stats_digest: str | None = None,
    reference_stamper: TradeReferenceStamper | None = None,
    hit_count_memo: MutableMapping[int, int] | None = None,
    on_mask_population: Callable[[Sequence[int]], None] | None = None,
) -> SweepRunSummary:
    """Run a reproducible, deduped, audited K-of-bits sweep over ONE (instrument, TF).

    The orchestrator that composes the engine bricks (see the module docstring
    for the data flow). Sequence of operations:

    1. Validate (non-empty bars; ``HOLD`` rejected; combo count within
       ``cfg.max_combos`` — the honest opt-in guard, raising BEFORE the Stage-1A
       build).
    2. **Completion-sentinel check** (digest path only,
       DEC-MARK-MANY-SENTINEL-001): a PAIRED sentinel proves the tuple's full
       write sequence completed — emit one ``SWEEP_RESUMED`` audit line and
       short-circuit, touching nothing. Absent/unpaired → fall through to a
       FULL re-run with dedup probes BYPASSED (the marks are advisory; a
       partially-marked dedup must never produce a subset re-rank —
       MIGRATION.md #650).
    3. Emit the ``SWEEP_START`` audit line.
    4. Build the Stage-1A ``bar_bits`` **once**.
    5. Enumerate the K-of-bits masks; for each: prune conflicts; on the legacy
       no-digest path, compute the run-identity eagerly (the dedup probe keys
       on it) and skip already-seen identities (O(1) Bloom→exact, BEFORE any
       trade sim); on the digest path collect the bare mask — the run-identity
       is NOT computed per enumerated mask there
       (DEC-SWEEP-STREAMING-RANK-001): probes are bypassed and marks skipped,
       so its only consumers (the :class:`SweepComboError` fingerprint + the
       finalist rows) recompute it lazily from the pure :func:`mask_run_id`.
    6. Stage-1B coarse pre-filter (``cfg.min_hits``): drop survivors that fire on
       too few bars to ever rank (still counted as ``evaluated`` + marked seen).
    7. Backtest the kept survivors — the wired resident Rust trade-sim
       (DEC-SWEEP-RUST-TRADESIM-WIRE-001): one
       :class:`~brutex.engine.rust_trade_sim.ResidentTradeSim` per tuple,
       chunked :meth:`~brutex.engine.rust_trade_sim.ResidentTradeSim.simulate_masks`
       batches, then the per-mask
       :func:`~brutex.engine.sweep_driver.metrics_from_trades` tail —
       byte-identical to the pre-wiring per-mask
       :func:`~brutex.engine.sweep_driver.metrics_for_mask` loop (which remains
       the per-mask oracle for chunk-failure re-attribution). Rank them
       (:func:`brutex.ranking.rank_combos`), then compute the LANE-W1 persist
       UNION (DEC-RANK-PARETO-PERSIST-001 / DEC-RANK-QUOTA-PERSIST-1000-001)
       via the ONE burn-in-checked composer binding
       (:func:`brutex.engine.rust_rank_select.union_selection`): the composite
       top-``cfg.top_k`` prefix PLUS the per-axis reserved-quota + ε-Pareto
       frontier extras, every finalist tagged ``selection_basis`` +
       ``pareto_member``.
       Under the rank-memory budget plan (DEC-SWEEP-STREAMING-RANK-001 part 2
       — resolved BEFORE step 3, after the sentinel check) the BOUNDED arm
       instead folds each kept mask into
       :class:`~brutex.engine.sweep_stream.SurvivorColumns` (the metrics
       object dropped per mask, an exact owned-bytes watermark enforcing the
       budget) and ranks via
       :func:`~brutex.engine.sweep_stream.rank_survivor_columns` — pinned
       BIT-IDENTICAL to the batch arm — recomputing only the ≤ ``top_k``
       finalists' full metrics through the same sim spine.
    8. Project each finalist to a :class:`ResultRow` and append to the store.
    9. Flush the store; write the trial-stats sidecar (digest path); mark
       **all** evaluated identities seen; then write the completion sentinel
       STRICTLY LAST (digest path) — its presence proves the whole sequence.
    10. Emit ``SWEEP_FLUSH`` + ``SWEEP_COMPLETE`` audit lines.

    Exactly three audit lines per full sweep regardless of combo count (Golden
    Rule #8 — O(1) audit cost); one line for a sentinel short-circuit. The
    store receives only finalists.

    Parameters
    ----------
    bars
        Chronologically ordered bars for one ``(instrument, interval)``
        (non-empty).
    cfg
        The validated :class:`SweepRunConfig`.
    store
        The destination :class:`~brutex.engine.results_store.ResultsStore`
        (finalist sink).
    dedup
        The :class:`ComboDedup` carrying the run's cross-run dedup state.
    audit
        An open :class:`~brutex.orchestrator.audit.AuditSink` — the real
        :class:`~brutex.orchestrator.audit.AuditWriter` on the sequential path
        (byte-unchanged), or a
        :class:`~brutex.orchestrator.audit.BufferedAuditCollector` in a
        parallel-sweep worker (DEC-MULTICORE-SWEEP-PHASE1-001).
    kernel
        Optional pre-built :class:`~brutex.engine.sweep_kernel.ResidentSweepKernel`
        for this tuple's ``(instrument, tf, proximity, prior_n)`` bar window
        (DEC-SWEEP-RESIDENT-KERNEL-001). When supplied, the Stage-1A ``bar_bits``
        build is skipped (its pre-built matrix is reused) and the Stage-1B count is
        served from the resident Rust buffer (no bar re-marshal) — the
        :func:`~brutex.engine.sweep_orchestrator.run_sweep_grid` reuse path. The
        result is **byte-identical** to the ``kernel=None`` path (both delegate to
        the same SUPERSET kernel; Golden Rule #4); only redundant rebuild/re-marshal
        work is removed. ``None`` (default) builds + counts inline as before. The
        kernel MUST be built from the SAME ``bars`` (a length mismatch raises).
    trial_stats_digest
        The tuple's 16-hex path digest, enabling the SENTINEL-AUTHORITATIVE
        redo protocol (DEC-MARK-MANY-SENTINEL-001) and the trial-stats sidecar
        write (DEC-TRIAL-STATS-WRITE-001): when provided, a PAIRED completion
        sentinel short-circuits the tuple; otherwise the run BYPASSES dedup
        probes (full population, ``skipped_dedup == 0`` by construction),
        atomically writes the per-tuple (N, V-moments) sidecar between flush
        and dedup-mark, and writes the completion sentinel strictly last.
        ADR-0008 condition 2's preserve-on-partial-resume rule is thereby
        enforced structurally — a partial population cannot occur on this
        path. ``None`` (default) keeps the legacy probe semantics and writes
        no sidecar/sentinel (library callers; the launcher path supplies the
        digest via :func:`~brutex.engine.sweep_orchestrator.run_one_tuple`).
    reference_stamper
        Optional India-VIX
        :class:`~brutex.engine.reference_series.TradeReferenceStamper`
        (DEC-VIX-STAMP-WIRE-001). Bound ONLY at the OBSERVABLE surface — the
        finalist projection (:func:`ranked_combo_to_result_row`), whose
        re-simulated trades feed the persisted rows. The K-of-N inner mask
        stream stays deliberately UNSTAMPED (see the explicit literal at the
        ``_build_trade_sim`` call). Each persisted finalist row carries the
        stamps' :func:`~brutex.engine.reference_wire.aggregate_trade_vix`
        summary in its nullable ``vix_mean`` / ``vix_min`` / ``vix_max``
        columns (unstamped → the honest ``None`` trio). REFERENCE ONLY: VIX
        is never a signal, never ranked on, never in the run identity — every
        persisted ``run_id`` and every non-VIX column is byte-identical with
        or without the stamper (regression-pinned); ONLY the three nullable
        VIX columns differ.
    hit_count_memo
        Optional per-CELL superset-hit-count memo (DEC-SWEEP-APRIORI-DEFAULT-001,
        MIGRATION.md #1752): a ``{mask: hits}`` mapping the caller owns across a
        cell's whole k-ladder, wrapped around the ONE shared ``_count_hits``
        (consumed by BOTH the Apriori frequent-frontier build and the Stage-1B
        pre-filter, so target-level counts seed the next tuple's frontier for
        free). Counts are a pure function of ``(bar_bits, mask)`` — per-mask
        independent — so memo-on is BIT-IDENTICAL to memo-off (parity-pinned);
        beyond :data:`HIT_MEMO_MAX_ENTRIES` the memo is pure pass-through.
        ``None`` (default — every legacy call site) is byte-identical to the
        pre-memo path. The grid drivers pass a memo ONLY when the Apriori
        exploration mode is enabled.
    on_mask_population
        Optional observability capture (DEC-SWEEP-APRIORI-DEFAULT-001,
        review fix round 3): fired EXACTLY once on the APRIORI arm — after
        the candidate generation, before the enumeration loop — with the
        tuple's already-materialised candidate list, so the R45 diffcheck
        can sample the EXECUTED mask population instead of re-walking the
        raw C(n, k) enumeration (which the extinction-mode auto-raise
        unbounds). NEVER fired on the legacy arm (its ``mask_source`` is a
        lazy generator; materialising it for a capture would re-create the
        very cost this closes — the diffcheck's own raw walk stays the
        legacy population). Fail-soft: a raising capture is WARNed
        (``sweep_run.mask_population_capture_failed``) and the sweep
        continues — an observability seam never kills a tuple (the R45
        I-3 law). ``None`` (default — every legacy call site) is
        byte-identical.

    Returns
    -------
    SweepRunSummary
        The full tally + the store content hash.

    Raises
    ------
    ValueError
        If ``bars`` is empty, ``cfg.direction`` is ``HOLD``, the enumerated combo
        count exceeds ``cfg.max_combos``, ``kernel`` was built from a different
        number of bars than ``bars``, an env knob is malformed
        (``BRUTEX_SWEEP_BOUNDED_RANK`` / ``BRUTEX_SWEEP_MEM_BUDGET_BYTES``), or
        ``BRUTEX_SWEEP_BOUNDED_RANK=on`` is forced without the digest path or a
        finite ``top_k`` (a config error — global, not a tuple condition).
    SweepMemoryBudgetError
        The graceful tuple-skip refusal (DEC-SWEEP-STREAMING-RANK-001): the
        batch-rank projection exceeds ``BRUTEX_SWEEP_MEM_BUDGET_BYTES`` and the
        bounded rank may not / cannot engage (mode ``off``, ``top_k=None``, or
        the legacy no-digest path), or the bounded accumulator's EXACT owned
        bytes crossed the budget mid-flight — never a raw ``MemoryError``.
    """
    if not bars:
        raise ValueError("run_persisted_sweep: bars must be non-empty")
    if not exec_bars_1m:
        # DEC-FILL-EXEC-GRID-1M-001 (design §3.3): a decision slice without
        # its 1-minute execution stream is not "partial coverage" — it is an
        # unrunnable coordinate. Refuse LOUDLY, never a silent single-feed
        # degrade (the launch preflight refuses earlier with the remedy;
        # this is the spine-level belt).
        raise ValueError(
            f"run_persisted_sweep: exec_bars_1m must be non-empty — the 1-minute "
            f"execution stream for {cfg.instrument!r} is REQUIRED "
            f"(DEC-FILL-EXEC-GRID-1M-001); pull/prebuild the 1minute slice"
        )
    if exec_map is None:
        # ONE map per tuple (mask-independent; the launcher threads its
        # preload-scoped per-coordinate memo instead — design §1.5). Derived
        # BEFORE the look-ahead guard wraps `bars` (the map build is a
        # sequential O(N) two-pointer pass, not a strategy read).
        exec_map = build_exec_grid_map(bars, exec_bars_1m, tf_minutes=cfg.tf_minutes)
    # Opt-in runtime look-ahead barrier (DEC-SWEEP-LOOKAHEAD-GUARD-001): the
    # env flag is resolved ONCE here at setup — never inside any per-bar loop.
    # OFF (the default) returns the SAME `bars` object (identity), so the hot
    # path below is byte-identical and cost-unchanged; ON wraps the window in
    # an index-guarded view that raises LookAheadError on any read ahead of
    # the forward consumption front (see brutex.engine.sweep_lookahead for
    # the honest envelope of what it catches vs. the property gates).
    bars = maybe_guard_bars(bars)
    direction_label = _direction_label(cfg.direction)

    n_unique = len(set(cfg.condition_bits))
    n_masks = count_combos(cfg.k, n_unique)  # C(n, k); validates 0 <= k <= n
    if n_masks > cfg.max_combos:
        raise ValueError(
            f"run_persisted_sweep: {n_masks} combos (C({n_unique},{cfg.k})) exceeds "
            f"max_combos={cfg.max_combos}; raise max_combos explicitly to run a sweep this large"
        )

    params = _canonical_params(cfg)
    subject = f"sweep:{cfg.instrument}:{cfg.tf}:{direction_label}:k={cfg.k}"

    # ── Completion-sentinel resume check (DEC-MARK-MANY-SENTINEL-001) ──
    # The sentinel — never the dedup marks — decides whether this tuple is
    # already complete. A paired sentinel short-circuits everything below.
    if trial_stats_digest is not None:
        sentinel = _sentinel_resume(store.path, trial_stats_digest)
        if sentinel is not None:
            resume_payload = {
                "tuple_digest": trial_stats_digest,
                "store_content_hash": sentinel.store_content_hash,
                "sidecar_written": sentinel.sidecar_written,
                "n_marked": sentinel.n_marked,
            }
            audit.append(action=_ACTION_RESUMED, subject=subject, payload=resume_payload)
            _LOG.info("sweep_run.resumed_complete", subject=subject, **resume_payload)
            return SweepRunSummary(
                enumerated=0,
                conflict_pruned=0,
                skipped_dedup=0,
                evaluated=0,
                ranked=0,
                persisted=0,
                store_content_hash=sentinel.store_content_hash,
                resumed_complete=True,
            )

    # ── Rank-memory budget plan (DEC-SWEEP-STREAMING-RANK-001 part 2) ──
    # ONE deterministic pre-flight decision from the config alone, taken AFTER
    # the sentinel short-circuit (a complete tuple is never refused) and BEFORE
    # any O(masks) allocation: below budget the batch path stays byte-identical
    # (the default-preservation goldens pin it); past budget mode `auto`
    # engages the bounded columnar rank while an ineligible/forbidden tuple is
    # REFUSED as a graceful SweepMemoryBudgetError tuple-skip — never today's
    # raw MemoryError crash. Mode `on`'s structural refusals (legacy path /
    # top_k=None) raise ValueError from plan_rank_memory — an explicit operator
    # forcing with an impossible shape is a config error, not a tuple condition.
    # Worker-aware budget (DEC-SWEEP-MEM-BUDGET-WORKERS-WIRE-001): the division
    # by the live eval-worker count happens INSIDE resolve_mem_budget_bytes —
    # the pool initializer exports BRUTEX_SWEEP_BUDGET_WORKERS, so each worker's
    # RAM-derived default is total × 1/6 ÷ pool size (N parallel eval workers
    # share ONE budget fraction). The BRUTEX_SWEEP_MEM_BUDGET_BYTES override is
    # PER-WORKER, never divided (the #1352 contract); T2's call-site division
    # (DEC-SWEEP-SCALE-GUARDS-001) was DEDUPED to this landed mechanism — a
    # second division here would double-divide the budget.
    plan = plan_rank_memory(
        n_masks=n_masks,
        top_k=cfg.top_k,
        digest_path=trial_stats_digest is not None,
        mode=resolve_bounded_rank_mode(),
        budget_bytes=resolve_mem_budget_bytes(),
    )
    if plan.refusal is not None:
        raise SweepMemoryBudgetError(f"{subject}: {plan.refusal}")
    if plan.bounded:
        engaged_payload = {
            "subject": subject,
            "mode": plan.mode.value,
            "n_masks": n_masks,
            "projected_bytes": plan.projected_bytes,
            "budget_bytes": plan.budget_bytes,
            "top_k": plan.bounded_top_k,
        }
        if plan.mode is BoundedRankMode.AUTO:
            # LOUD by design: an auto-engage means the tuple would have blown
            # past the budget on the batch path — the operator should see it.
            _LOG.warning("sweep_run.bounded_rank_engaged", **engaged_payload)
        else:
            _LOG.info("sweep_run.bounded_rank_engaged", **engaged_payload)

    start_payload: dict[str, Any] = {
        "instrument": cfg.instrument,
        "tf": cfg.tf,
        "direction": direction_label,
        "mode": cfg.mode,
        "k": cfg.k,
        "n_bits": n_unique,
        "n_masks_enumerated": n_masks,
        "data_snapshot_hash": cfg.data_snapshot_hash,
        "indicator_version": cfg.indicator_version,
        "code_commit_sha": cfg.code_commit_sha,
        # DEC-FILL-WORST-CASE-ONLY-001: `params` already carries the
        # UNCONDITIONAL "fill_mode": "worst_case" key (the elide law is
        # retired with the PRECISE mode), so the audit line records the
        # identity dict verbatim — hash inputs and audit wire agree.
        "params": params,
    }
    if cfg.temporal is not None:
        # Provenance WITHOUT re-key (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001): the
        # gate config rides the hash-chained SWEEP_START payload, emitted ONLY
        # when configured — the default audit bytes are unchanged (pinned).
        start_payload["temporal_gate"] = {
            "mode": cfg.temporal.mode,
            "min_profitable_years_fraction": cfg.temporal.min_profitable_years_fraction,
            "max_single_year_pnl_share": cfg.temporal.max_single_year_pnl_share,
            "min_edge_year_sessions": cfg.temporal.min_edge_year_sessions,
            "min_judged_years": cfg.temporal.min_judged_years,
        }
    audit.append(action=_ACTION_START, subject=subject, payload=start_payload)

    if kernel is None:
        bar_bits = build_bar_bits(
            bars,
            proximity=cfg.proximity,
            prior_n=cfg.prior_n,
            fib_anchor=cfg.fib_anchor,
            vwap_source=cfg.vwap_source,
            ema_source=cfg.ema_source,
            roll_schedule=cfg.roll_schedule,
            apply_1day_mask=apply_1day_mask_for(cfg.instrument, cfg.tf),
        )
    else:
        if kernel.n_bars != len(bars):
            raise ValueError(
                f"run_persisted_sweep: kernel has {kernel.n_bars} bars but {len(bars)} "
                f"bars were supplied — the kernel must be built from this tuple's bars"
            )
        # DEC-FILL-EXEC-GRID-1M-001 twin of the n_bars guard: a kernel built
        # with a DIFFERENT execution stream would hand misaligned 1m columns.
        if kernel.exec_bars_1m is not None and len(kernel.exec_bars_1m) != len(exec_bars_1m):
            raise ValueError(
                f"run_persisted_sweep: kernel holds {len(kernel.exec_bars_1m)} execution "
                f"bars but {len(exec_bars_1m)} were supplied — the kernel must be built "
                f"from this tuple's 1-minute execution stream"
            )
        # R-1 (DEC-NIFTY-FIB-AXIS-001): the pre-built ``bar_bits`` matrix is
        # anchor-SPECIFIC, so a kernel built for a different Fib anchor would make
        # the sweep silently evaluate the WRONG Group-4 levels. Fail-fast on a
        # mismatch, exactly like the ``n_bars`` guard above.
        if kernel.fib_anchor is not cfg.fib_anchor:
            raise ValueError(
                f"run_persisted_sweep: kernel was built for Fib anchor "
                f"{kernel.fib_anchor.value!r} but the tuple sweeps "
                f"{cfg.fib_anchor.value!r} — the kernel must match this tuple's anchor"
            )
        # DEC-NIFTY-PRICE-SOURCE-AXIS-001 (MANDATORY): ``bar_bits`` bits 52-53
        # (VWAP position) and 0-5 (EMA-vs-close) are price-source-SPECIFIC, so a
        # kernel built for a different source would silently serve the WRONG bits.
        # Fail-fast on a mismatch, exactly like the Fib-anchor guard above.
        if kernel.vwap_source is not cfg.vwap_source:
            raise ValueError(
                f"run_persisted_sweep: kernel was built for VWAP source "
                f"{kernel.vwap_source.value!r} but the tuple sweeps "
                f"{cfg.vwap_source.value!r} — the kernel must match this tuple's source"
            )
        if kernel.ema_source is not cfg.ema_source:
            raise ValueError(
                f"run_persisted_sweep: kernel was built for EMA source "
                f"{kernel.ema_source.value!r} but the tuple sweeps "
                f"{cfg.ema_source.value!r} — the kernel must match this tuple's source"
            )
        # ADR-0012 roll-schedule twin of the anchor guard: the ``bar_bits``
        # matrix is schedule-SPECIFIC (the per-contract hard resets), so a
        # kernel built under a different schedule would silently misplace
        # every roll boundary (DEC-STAGE1A-ROLL-RESET-001).
        if kernel.roll_schedule != cfg.roll_schedule:
            raise ValueError(
                f"run_persisted_sweep: kernel was built for roll_schedule "
                f"{kernel.roll_schedule!r} but the tuple sweeps "
                f"{cfg.roll_schedule!r} — the kernel must match this tuple's schedule"
            )
        # D12 twin (review round-1 finding 5): a kernel built under the wrong
        # ``apply_1day_mask`` flag carries intraday-only bits (ToD 44-47 + the
        # retained session-VWAP 52-53 — LIVE again per
        # DEC-VOCAB-VWAP-RETAIN-001, both families 1day-cleared by
        # ``APPLIES_TO_1DAY_MASK``) on a futures ``1day`` grid — unreachable through the
        # production cache (the flag is a pure function of instrument+tf,
        # both in its key) but guarded for a hand-built kernel, exactly like
        # the schedule twin above.
        if kernel.apply_1day_mask != apply_1day_mask_for(cfg.instrument, cfg.tf):
            raise ValueError(
                f"run_persisted_sweep: kernel was built with apply_1day_mask="
                f"{kernel.apply_1day_mask!r} but the tuple requires "
                f"{apply_1day_mask_for(cfg.instrument, cfg.tf)!r} — the kernel "
                f"must match this tuple's D12 flag"
            )
        bar_bits = kernel.bar_bits

    # ONE shared superset-hit counter (CLAUDE.md §11 COMMON) — the resident
    # kernel's buffer when reusing a pre-built kernel (no bar re-marshal), else
    # the free ``count_supermasks``; both delegate to the SAME Rust kernel /
    # numpy fallback, so they are bit-identical (Golden Rule #4). Consumed by BOTH
    # the Stage-1B pre-filter below AND (when ``BRUTEX_SWEEP_APRIORI`` is on) the
    # Apriori frequent-frontier build, so the two provably agree on frequency.
    def _count_hits_uncached(masks: Sequence[int]) -> list[int]:
        if kernel is not None:
            return kernel.count_hits(masks)
        return count_supermasks(bar_bits, masks)

    # Per-cell memo wrap (DEC-SWEEP-APRIORI-DEFAULT-001): counts are a pure
    # function of (bar_bits, mask) and per-mask independent (both counters are
    # per-mask maps), so serving memoised counts + computing only the missing
    # sub-batch is BIT-IDENTICAL to computing the whole batch — identical counts
    # in identical order (parity-pinned memo-on vs memo-off). Beyond
    # HIT_MEMO_MAX_ENTRIES the memo is pure pass-through: computed values ride a
    # call-local overlay, never retained. ``hit_count_memo is None`` (every
    # legacy call site) short-circuits to the uncached counter — zero behavior.
    def _count_hits(masks: Sequence[int]) -> list[int]:
        if hit_count_memo is None:
            return _count_hits_uncached(masks)  # byte-identical legacy arm
        overlay: dict[int, int] = {}
        missing = [m for m in masks if m not in hit_count_memo]
        if missing:
            for m, c in zip(missing, _count_hits_uncached(missing), strict=True):
                if len(hit_count_memo) < HIT_MEMO_MAX_ENTRIES:
                    hit_count_memo[m] = c
                else:
                    overlay[m] = c
        return [hit_count_memo[m] if m in hit_count_memo else overlay[m] for m in masks]

    # Sentinel-authoritative redo (DEC-MARK-MANY-SENTINEL-001): with a digest
    # and NO paired sentinel, the marks cannot be trusted (a crash inside the
    # per-key mark loop leaves a subset — MIGRATION.md #650), so probes are
    # BYPASSED: the FULL population re-runs and every artifact rewrites
    # byte-identically. Only legacy no-digest callers consult the probes.
    bypass_dedup = trial_stats_digest is not None
    enumerated = 0
    conflict_pruned = 0
    skipped_dedup = 0

    def _lazy_run_id(mask: int) -> str:
        """Recompute one mask's run-identity on demand (:func:`mask_run_id` is pure)."""
        return mask_run_id(
            mask=mask,
            direction=direction_label,
            instrument=cfg.instrument,
            tf=cfg.tf,
            mode=cfg.mode,
            params=params,
            data_snapshot_hash=cfg.data_snapshot_hash,
            indicator_version=cfg.indicator_version,
            code_commit_sha=cfg.code_commit_sha,
        )

    survivors: list[int] = []
    #: Legacy no-digest arm ONLY — the probe-time run_ids, parallel to
    #: ``survivors`` (consumed by ``mark_many`` + reused for finalists/errors).
    survivor_run_ids: list[str] = []
    # Enumeration source (DEC-SWEEP-APRIORI-TUPLE-001): default is the
    # brute-force ``iter_masks_k_of_bits`` (every C(n, k) combo). With
    # ``BRUTEX_SWEEP_APRIORI`` enabled it is the Apriori level-wise pruned
    # candidate list — a SUBSEQUENCE of the brute-force enumeration whose
    # Stage-1B-kept set is provably identical (byte-identical leaderboard); only
    # the honest ``enumerated``/``conflict_pruned`` tallies shrink. The generator
    # is imported LAZILY here (sweep_apriori raises this module's
    # SweepAprioriBudgetError at its breaker via a lazy import too, so neither
    # module tops the other — no import cycle).
    mask_source: Iterable[int]
    # Ranged Rust enum+prune+Stage-1B kernel probe (DEC-SWEEP-ENUM-RUST-001,
    # default-ON behind BRUTEX_SWEEP_RUST_ENUM since #2215, `=0` kills;
    # design-of-record §1.4 seam
    # shape): the wrapper is probed on the brute-force arm ONLY (the Apriori
    # arm WINS over the kernel — its probe lives only in the else arm, per
    # the frozen DEC-SWEEP-APRIORI-TUPLE-001 contract) and engages on the
    # DIGEST path only (a legacy no-digest tuple with the flag ON warns
    # ``legacy_dedup_path`` once — the design's engagement-condition order).
    # The kernel FUSES the Stage-1B min-hits count (the design §2 row: fusing
    # avoids materialising the 5.5M–40M pre-min-hits survivor Python ints),
    # so a kernel-served tuple gets ``kept`` directly plus the exact integer
    # tallies the loop + batch filter below would have produced —
    # ``evaluated`` derives as ``enumerated − conflict_pruned`` (the
    # pre-min-hits survivor count) and ``minhits_dropped`` stays
    # ``evaluated − len(kept)``, so the sidecar/audit buckets are IDENTICAL
    # to the Python arm's (the design residual-(ix) bucketing law).
    # ``skipped_dedup`` stays 0 and ``survivor_run_ids`` stays empty on this
    # path by construction. Every fallback (``None``) runs the pre-existing
    # loop over the pre-existing lazy source, byte-identical by construction.
    kernel_enum: RustEnumResult | None = None
    apriori_on = apriori_enabled()
    #: The per-level census + wall-timings the FLUSH/COMPLETE audit payload
    #: carries on the apriori arm (DEC-SWEEP-APRIORI-DEFAULT-001) — empty and
    #: never emitted on the legacy arm (EMIT-ON-apriori, payload byte-identical).
    apriori_levels: list[dict[str, object]] = []
    apriori_generator_ms = 0.0
    apriori_max_mask_bars = 0
    if apriori_on:
        from brutex.engine.sweep_apriori import generate_apriori_candidates

        apriori_max_mask_bars = resolve_max_mask_bars()

        # Census recorder (DEC-SWEEP-APRIORI-DEFAULT-001): the generator calls
        # its injected counter EXACTLY once per level 1..k-1, in level order
        # (the frozen call-order invariant — call j IS level j, pinned by the
        # census-vs-log bit-equality test), so wrapping the ONE shared counter
        # reproduces the module's own ``sweep.apriori.census`` rows bit-for-bit:
        # ``candidates = len(masks)`` is the raw |C_j| and
        # ``sum(c >= min_hits) `` is the frequent |F_j|. Timings are wall-clock
        # OBSERVABILITY only — they ride the audit payload + structlog, never
        # the sidecar / store / sentinel (nothing non-deterministic enters a
        # byte-compared artifact, Golden Rule #7).
        def _census_count_hits(masks: Sequence[int]) -> list[int]:
            t0 = time.monotonic()
            counts = _count_hits(masks)
            apriori_levels.append(
                {
                    "level": len(apriori_levels) + 1,
                    "candidates": len(masks),
                    "frequent": sum(c >= cfg.min_hits for c in counts),
                    "count_ms": round((time.monotonic() - t0) * 1000.0, 3),
                }
            )
            return counts

        t_gen = time.monotonic()
        apriori_candidates = generate_apriori_candidates(
            bar_bits,
            condition_bits=cfg.condition_bits,
            k=cfg.k,
            min_hits=cfg.min_hits,
            count_hits=_census_count_hits,
            n_bars=len(bar_bits),
            # The budget resolved ONCE above (review fix round 1 n4) — passing
            # it forward halves the per-tuple env re-resolve, so a junk
            # BRUTEX_SWEEP_APRIORI_MAX_MASK_BARS warns once per tuple here,
            # not twice; the value is identical by construction.
            max_mask_bars=apriori_max_mask_bars,
            cell_label=subject,
        )
        apriori_generator_ms = round((time.monotonic() - t_gen) * 1000.0, 3)
        # The TARGET row mirrors ``_emit_census``'s target level: raw |C_k|
        # with ``frequent`` omitted (null — Stage-1B is the authority; its
        # truth is the ``backtested`` tally on the same payload) and no
        # counter call of its own (``count_ms`` null). ``k == 1`` therefore
        # yields exactly ONE row (zero counter calls), matching the module.
        # ``k == 0`` mirrors the module 1:1 too (review fix round 1 m9): the
        # frozen generator emits NO census row for the single empty mask, so
        # this wrapper must not invent one — the levels list stays EMPTY.
        if cfg.k > 0:
            apriori_levels.append(
                {
                    "level": cfg.k,
                    "candidates": len(apriori_candidates),
                    "frequent": None,
                    "count_ms": None,
                }
            )
        if on_mask_population is not None:
            # R45 executed-population handoff (review fix round 3): the
            # candidate list is ALREADY materialised in memory — hand the
            # reference to the caller (the diffcheck's sample population);
            # nothing is recomputed. Fail-soft per the I-3 law.
            try:
                on_mask_population(apriori_candidates)
            except Exception as exc:  # an observability capture never kills the tuple
                _LOG.warning(
                    "sweep_run.mask_population_capture_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
        mask_source = apriori_candidates
    else:
        kernel_enum = enum_prefilter_via_kernel(
            cfg.condition_bits,
            cfg.k,
            min_hits=cfg.min_hits,
            bar_bits=bar_bits,
            count_hits=_count_hits,
            digest_path=bypass_dedup,
        )
        mask_source = iter_masks_k_of_bits(cfg.condition_bits, cfg.k)
    kept: list[int]
    if kernel_enum is not None:
        # Kernel-served: the identical lex-ordered post-min-hits kept list +
        # the identical integer tallies the loop AND Stage-1B filter below
        # would have produced on the digest path (bit-identity pinned by the
        # parity golden/property suites; the unconsumed generator above is
        # lazy — free when unused). The Python ``_count_hits`` batch call
        # deliberately does NOT run here — the kernel counted in-fuse (its
        # in-band ``hits_counted`` law is seam-validated in the wrapper).
        kept = kernel_enum.kept
        enumerated += kernel_enum.enumerated
        conflict_pruned += kernel_enum.conflict_pruned
        evaluated = kernel_enum.evaluated
    else:
        for mask in mask_source:
            enumerated += 1
            if has_conflict(mask):
                conflict_pruned += 1
                continue
            if bypass_dedup:
                # Lazy run-identity (DEC-SWEEP-STREAMING-RANK-001): on the digest
                # path the dedup probes are BYPASSED (sentinel-authoritative redo,
                # above) and ``mark_many`` is SKIPPED (below), so a survivor's
                # run_id has exactly TWO consumers — the SweepComboError
                # fingerprint and the finalist rows — both recomputed on demand
                # via ``run_id_of`` (``mask_run_id`` is a pure BLAKE3 of its 9
                # canonical inputs, Golden Rule #4: same bytes, computed later).
                # Skipping the eager per-enumerated-mask hash deletes O(survivors)
                # BLAKE3 work + the (mask, run_id) tuple/str/dict retention.
                survivors.append(mask)
                continue
            run_id = _lazy_run_id(mask)
            if dedup.seen(run_id):
                skipped_dedup += 1
                continue
            survivors.append(mask)
            survivor_run_ids.append(run_id)

        # Stage-1B coarse pre-filter — drop survivors that can never reach the
        # trade-count gate (still "evaluated": we decided their fate + mark them
        # seen).
        kept = survivors
        if cfg.min_hits > 0 and survivors:
            # Resident buffer (kernel) avoids re-marshalling the bar window;
            # both arms are bit-identical (same SUPERSET kernel) so the kept
            # set never differs. Routed through the ONE shared ``_count_hits``
            # the Apriori generator uses.
            hits = _count_hits(survivors)
            kept = [mask for mask, hit in zip(survivors, hits, strict=True) if hit >= cfg.min_hits]
        evaluated = len(survivors)

    # ONE run-identity resolver for every downstream consumer (finalist rows +
    # the SweepComboError fingerprints): the digest path recomputes lazily
    # (O(finalists + errors) BLAKE3 instead of O(survivors)); the legacy path
    # reuses the exact probe-time values already paid for above (eager
    # semantics untouched — the dedup probe genuinely needs them).
    run_id_of: Callable[[int], str]
    if bypass_dedup:
        run_id_of = _lazy_run_id
    else:
        run_id_of = dict(zip(survivors, survivor_run_ids, strict=True)).__getitem__
    # Combo evaluation — the wired resident Rust trade-sim
    # (DEC-SWEEP-RUST-TRADESIM-WIRE-001, wiring the bit-identity-proven
    # DEC-SWEEP-RUST-TRADESIM-001 kernel): one ResidentTradeSim per tuple (built
    # under `_build_trade_sim`'s tuple-skip contract), the kept masks
    # batch-simulated in bounded CHUNKS, each mask's trades fed through the SAME
    # `metrics_from_trades` tail `metrics_for_mask` uses — byte-identical pairs
    # in the SAME order as the pre-wiring per-mask loop (the wired-vs-unwired
    # store-hash golden + chunk-invariance tests enforce it).
    #
    # Fail-loud-with-fingerprint is PRESERVED: a per-mask metrics-tail raise is
    # re-thrown as a `SweepComboError` naming the exact offending combo (mask
    # hex + run-id + tuple coordinate, `_combo_error`); a CHUNK failure loses
    # per-mask attribution, so `_reattribute_chunk_failure` re-runs that chunk
    # one mask at a time through the `metrics_for_mask` ORACLE to name the
    # culprit — and if none re-raises (a batch/kernel-level failure) it fails
    # LOUD naming the chunk, NEVER silently adopting the oracle's results. Per
    # "Resilient: skip + continue", the grid drivers catch SweepComboError and
    # skip the WHOLE tuple — treated CONSERVATIVELY as tuple-wide (the safe
    # redo-everything choice); the tuple is left un-sentineled / un-cached so it
    # redoes next run. A MemoryError is re-raised bare at EVERY level — the
    # chunk call, the metrics tail, the re-attribution — BEFORE any wrap or
    # oracle re-run (OOM is a GLOBAL stop, DEC-LAKE-WRITE-HALT-001, never a
    # per-tuple skip and never a retry under memory pressure).
    # The chunked simulate→metrics spine itself is the SHARED `_iter_mask_metrics`
    # generator (one spine for the batch accumulation, the bounded columnar
    # accumulation, and the bounded finalist recompute — COMMON). The BATCH arm
    # collects `pairs` exactly as before (byte-identical below budget); the
    # BOUNDED arm (DEC-SWEEP-STREAMING-RANK-001 part 2) appends each kept mask's
    # ranking columns to `SurvivorColumns` and DROPS the ResultMetrics — O(kept)
    # columnar payload (~80-105 B/kept) instead of O(kept) rich objects — under
    # an EXACT owned-bytes watermark that degrades gracefully to a
    # SweepMemoryBudgetError tuple-skip if even the columns outgrow the budget.
    # DEC-RANK-CVAR-TAIL-GATE-001: per-tuple tally of eval-spine CVaR
    # rejections (always 0 when cfg.cvar_gate is None — the callback is then
    # never invoked, byte-identical to the pre-gate loop).
    cvar_rejected = 0

    def _count_cvar_reject(_mask: int) -> None:
        nonlocal cvar_rejected
        cvar_rejected += 1

    pairs: list[tuple[int, ResultMetrics]] = []
    columns = (
        SurvivorColumns(
            with_overfit=cfg.overfit is not None,
            with_temporal=cfg.temporal is not None,
            max_sl_hit_rate=cfg.max_sl_hit_rate,
        )
        if plan.bounded
        else None
    )
    # Temporal runtime (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001): built ONLY when the
    # gate is configured — the temporal=None arm never constructs the year
    # table, the sink, or the predicates (the gate-OFF zero-work spy pin; the
    # frozen goldens pin the bytes).
    temporal_runtime = (
        _build_temporal_runtime(bars, cfg.temporal) if cfg.temporal is not None else None
    )
    sim: ResidentTradeSim | None = None
    if kept:
        # HONEST ENVELOPE (DEC-VIX-STAMP-WIRE-001, coordinator ruling): the
        # K-of-N inner mask stream's trades are DISCARDED after the metrics
        # tail — no row, report or operator surface ever sees them — so
        # stamping every one of the O(kept x trades) inner trades would be
        # pure wasted work with zero observable output. The stream is
        # therefore EXPLICITLY unstamped via this literal (one of the
        # ENUMERATED sanctioned literal-None seam bindings — five after the
        # round-6 review also unstamped the cascade candidate re-simulation
        # — AST-pinned with exact counts by
        # tests/unit/test_vix_stamper_binding_pin.py — never a silent
        # omission). REVERT LEVER: pass ``reference_stamper=reference_stamper``
        # here to stamp the inner stream too (one kwarg; its stamps are
        # discarded WITH the trades so the persisted bytes are identical
        # either way — the finalist row's VIX trio derives from the
        # projector's OWN re-simulation, never this stream — and
        # trade_sim_path logs vix_stamped=False so the unstamped stream is a
        # stated fact).
        sim = _build_trade_sim(
            bars,
            bar_bits,
            cfg,
            direction_label,
            exec_bars_1m=exec_bars_1m,
            exec_map=exec_map,
            reference_stamper=None,
            # The W2 rider (DEC-STAGE1A-RUST-BATCH-001): a kernel-reusing
            # tuple hands its once-per-window columns; the kernel=None arm
            # derives locally (byte-identical).
            # The W2 rider rebased onto the EXECUTION stream
            # (DEC-FILL-EXEC-GRID-1M-001): a kernel built WITH the exec
            # stream hands its once-per-window 1m columns; a kernel without
            # one (or kernel=None) derives locally, byte-identical.
            kernel_columns=kernel.exec_columns if kernel is not None else None,
        )
        for mask, metrics in _iter_mask_metrics(
            sim,
            kept,
            bars,
            bar_bits,
            cfg,
            direction_label,
            run_id_of,
            temporal_sink=None if temporal_runtime is None else temporal_runtime.sink,
            cvar_gate=cfg.cvar_gate,
            on_cvar_reject=_count_cvar_reject,
        ):
            if columns is None:
                pairs.append((mask, metrics))
                if temporal_runtime is not None:
                    # BATCH arm: retain the triple aligned with `pairs` (the
                    # rank seam's per-candidate stream).
                    temporal_runtime.triples.append(temporal_runtime.take_last(mask))
                continue
            columns.append(
                mask,
                metrics,
                temporal=None if temporal_runtime is None else temporal_runtime.take_last(mask),
            )
            if columns.owned_nbytes() > plan.budget_bytes:
                raise SweepMemoryBudgetError(
                    f"{subject}: "
                    + watermark_message(
                        owned_bytes=columns.owned_nbytes(),
                        budget_bytes=plan.budget_bytes,
                        n_kept=columns.n_kept,
                    )
                )
    # ── LANE-W1 persist UNION (DEC-RANK-PARETO-PERSIST-001 /
    # DEC-RANK-QUOTA-PERSIST-1000-001): both arms resolve the SAME
    # (finalists, per-finalist tags, truncated, frontier_dropped) via the ONE
    # shared composer — batch through `_selection_inputs_from_ranked` +
    # `union_selection`, bounded through `rank_survivor_columns`'
    # `union_selection_fn` seam (which delegates to the SAME binding), so the
    # two arms' persisted row sets + tags are identical BY CONSTRUCTION.
    finalist_tags: list[tuple[str | None, bool | None]]
    if columns is None:
        ranked = rank_combos(
            pairs,
            overfit=cfg.overfit,
            temporal=None if temporal_runtime is None else temporal_runtime.gate_input(),
            max_sl_hit_rate=cfg.max_sl_hit_rate,
        )
        selection = union_selection(_selection_inputs_from_ranked(ranked), top_k=cfg.top_k)
        finalists = [ranked[index] for index in selection.order]
        finalist_tags = [
            (selection.basis_by_index[index], index in selection.frontier)
            for index in selection.order
        ]
        gated_pass_truncated = selection.truncated
        frontier_dropped = selection.frontier_dropped
        n_ranked = len(ranked)
        backtested = len(pairs)
        sidecar_sharpes = [metrics.sharpe_ratio for _, metrics in pairs]
    else:
        # Bounded rank: the SAME shared ranking internals over the columns
        # (bit-identical to `rank_combos(pairs)` — property-pinned), the union
        # via the SAME injected composer binding, then the finalists' full
        # metrics recomputed O(selected) via the SAME eval spine (the V16
        # re-simulation precedent; NO temporal_sink there — the finalists'
        # triples already gated them).
        selected = rank_survivor_columns(
            columns,
            overfit=cfg.overfit,
            top_k=plan.require_bounded_top_k(),
            temporal=None if temporal_runtime is None else temporal_runtime.columns_gate_input(),
            union_selection_fn=union_selection,
        )
        finalists = _rebuild_bounded_finalists(
            selected, sim, bars, bar_bits, cfg, direction_label, run_id_of
        )
        finalist_tags = [
            (finalist.selection_basis, finalist.pareto_member) for finalist in selected.finalists
        ]
        gated_pass_truncated = selected.truncated
        frontier_dropped = selected.frontier_dropped
        n_ranked = selected.n_ranked
        backtested = columns.n_kept
        sidecar_sharpes = columns.sharpe_values()

    if temporal_runtime is not None and temporal_runtime.stats.abstained:
        # Whole-gate abstention — LOUD by design (Golden Rule #10): a
        # sub-floor window cannot evidence cross-year robustness, so NO combo
        # was rejected; the operator should know the gate stood down.
        _LOG.warning(
            "sweep_run.temporal_gate",
            abstained=True,
            judged_years=temporal_runtime.n_judged,
            min_judged_years=temporal_runtime.cfg.min_judged_years,
            subject=subject,
        )

    persisted = 0
    for rc, (selection_basis, pareto_member) in zip(finalists, finalist_tags, strict=True):
        row = ranked_combo_to_result_row(
            rc,
            run_id=run_id_of(rc.mask),
            instrument=cfg.instrument,
            tf=cfg.tf,
            direction=direction_label,
            mode=cfg.mode,
            bars=bars,
            bar_bits=bar_bits,
            tf_minutes=cfg.tf_minutes,
            sl_paisa=cfg.sl_paisa,
            tp_paisa=cfg.tp_paisa,
            exec_bars_1m=exec_bars_1m,
            exec_map=exec_map,
            # The OBSERVABLE bind (DEC-VIX-STAMP-WIRE-001): the finalist
            # re-simulation feeds the persisted row, so the run's stamper
            # lands here.
            reference_stamper=reference_stamper,
            temporal=cfg.temporal,
            temporal_judged_years=None if temporal_runtime is None else temporal_runtime.judged,
            start_equity_paisa=cfg.start_equity_paisa,
            # The LANE-W1 provenance stamps — computed ONCE per pool above,
            # BEFORE the single append per run_id (the results_store
            # run-identity conflict law).
            selection_basis=selection_basis,
            pareto_member=pareto_member,
            # The gate-provenance carrier (DEC-SWEEP-SERVING-GATE-
            # PROVENANCE-001): threaded from the grid via to_run_config.
            gate_provenance=cfg.gate_provenance,
        )
        try:
            store.append(row)
        except ResultsStoreGateConflictError as exc:
            # C5 (DEC-SWEEP-TUPLE-DIGEST-GATE-IDENTITY-001): a gate-config re-run
            # into the same store address surfaces here as an all-nullable-metric
            # conflict. Convert AT THE SOURCE to a SweepComboError — a SIBLING of
            # ResultsStoreError — so it sails PAST run_one_tuple's
            # ``except ResultsStoreError`` (which, with store_quarantine=None,
            # re-raises as a GLOBAL grid abort) and lands on the driver's
            # ``except SweepComboError`` tuple-skip + TUPLE_FAILED-count ladder.
            raise SweepComboError(
                f"gate-config conflict persisting {cfg.instrument}/{cfg.tf}/"
                f"{direction_label} k={cfg.k}: {exc}"
            ) from exc
        persisted += 1

    flush = store.flush()
    # Trial-stats sidecar (DEC-TRIAL-STATS-WRITE-001): written BETWEEN flush
    # and mark — a crash in this window re-does the whole tuple and rewrites a
    # byte-identical sidecar. ADR-0008 condition 2 required the FULL mask
    # population; the dedup bypass above guarantees it structurally on this
    # path (``skipped_dedup == 0`` by construction), so the old
    # preserve-on-partial-resume arm is unreachable and removed
    # (DEC-MARK-MANY-SENTINEL-001) — a subset population can no longer exist
    # to be preserved against.
    sidecar_written = False
    trial_n_finite = 0
    trial_sum_sharpe = 0.0
    trial_sum_sharpe_sq = 0.0
    if trial_stats_digest is not None:
        stats = tuple_trial_stats_from_sharpes(
            sidecar_sharpes,
            n_enumerated=enumerated,
            n_conflict_pruned=conflict_pruned,
            n_minhits_dropped=evaluated - len(kept),
            tuple_digest=trial_stats_digest,
            store_content_hash=flush.content_hash,
        )
        write_tuple_trial_stats(trial_stats_sidecar_path(store.path), stats)
        sidecar_written = True
        trial_n_finite = stats.n_finite
        trial_sum_sharpe = stats.sum_sharpe
        trial_sum_sharpe_sq = stats.sum_sharpe_sq
    # Mark seen AFTER the flush so a crash between evaluation and flush re-does
    # the work rather than skipping it (redo-not-skip; Golden Rule #7). The
    # marks are ADVISORY on the digest path (the sentinel below is the redo
    # authority); they remain load-bearing only for legacy no-digest callers.
    # On ``bypass_dedup`` (the digest path) the probes were NEVER consulted —
    # ``seen`` was skipped above — so marking is a pure, wasted O(survivors)
    # write to the (now off-heap LMDB) exact tier; skip it (DEC-SWEEP-DEDUP-LMDB-001).
    # The sentinel below is the redo authority on this path, so ``n_marked``
    # honestly reads 0 (no marks were written).
    marked = 0 if bypass_dedup else dedup.mark_many(survivor_run_ids)
    # Completion sentinel STRICTLY LAST (DEC-MARK-MANY-SENTINEL-001): its
    # presence proves flush + sidecar + mark all completed; a crash anywhere
    # before this line leaves it absent → the next run redoes the FULL tuple.
    sentinel_written = False
    if trial_stats_digest is not None:
        write_completion_sentinel(
            completion_sentinel_path(store.path),
            TupleCompletionSentinel(
                tuple_digest=trial_stats_digest,
                store_content_hash=flush.content_hash,
                sidecar_written=sidecar_written,
                n_marked=marked,
            ),
        )
        sentinel_written = True

    tallies: dict[str, Any] = {
        "enumerated": enumerated,
        "conflict_pruned": conflict_pruned,
        "skipped_dedup": skipped_dedup,
        "evaluated": evaluated,
        "ranked": n_ranked,
        "persisted": persisted,
        # Additive trial-stats tallies (DEC-TRIAL-STATS-WRITE-001) — the
        # hash-chain is per-line, so new payload keys are chain-safe.
        "backtested": backtested,
        "minhits_dropped": evaluated - len(kept),
        "trial_n_finite": trial_n_finite,
        "trial_sidecar_written": sidecar_written,
        # Additive sentinel tally (DEC-MARK-MANY-SENTINEL-001) — chain-safe.
        "sentinel_written": sentinel_written,
        # Additive LANE-W1 selection tallies (DEC-RANK-PARETO-PERSIST-001 /
        # DEC-RANK-QUOTA-PERSIST-1000-001) — chain-safe.
        "gated_pass_truncated": gated_pass_truncated,
        "frontier_dropped": frontier_dropped,
    }
    if temporal_runtime is not None:
        # Temporal counters (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001) — emitted ONLY
        # when configured, so the default COMPLETE payload bytes are unchanged.
        tallies["temporal_gated"] = temporal_runtime.stats.killed
        tallies["temporal_observed_kills"] = temporal_runtime.stats.observed_kills
        tallies["temporal_abstained"] = temporal_runtime.stats.abstained
        tallies["temporal_judged_years"] = temporal_runtime.n_judged
    if apriori_on:
        # EMIT-ON-apriori census artifact (DEC-SWEEP-APRIORI-DEFAULT-001,
        # MIGRATION.md #1752): the per-level |C_j|/|F_j| curve + wall timings
        # land on the FLUSH + COMPLETE audit payloads (hash-chained JSONL —
        # additive keys are chain-safe, the DEC-TRIAL-STATS-WRITE-001
        # precedent) + the ``sweep_run.complete`` log. A LEGACY tuple carries
        # NO key, so its payload is byte-identical (frozen-key-set pinned).
        # Timings live ONLY here — never in the sidecar / store / sentinel.
        tallies["apriori"] = {
            "max_mask_bars": apriori_max_mask_bars,
            "generator_ms": apriori_generator_ms,
            "levels": apriori_levels,
        }
    audit.append(
        action=_ACTION_FLUSH,
        subject=subject,
        payload={**tallies, "store_content_hash": flush.content_hash},
    )
    audit.append(action=_ACTION_COMPLETE, subject=subject, payload=tallies)
    _LOG.info("sweep_run.complete", subject=subject, **tallies)
    if cfg.cvar_gate is not None:
        # EMIT-ON-ACTIVE (DEC-RANK-CVAR-TAIL-GATE-001): one INFO per tuple
        # naming the gate's config + rejection tally under the stable
        # ``cvar_tail`` wire token. A default (gate-off) sweep emits NOTHING —
        # zero log delta on the byte-identical default arm. Deliberately NOT
        # folded into the audit ``tallies`` payload (the frozen per-tuple
        # audit contract stays byte-untouched).
        _LOG.info(
            "sweep.cvar_gate",
            subject=subject,
            reason=GATE_CVAR_TAIL,
            rejected=cvar_rejected,
            alpha_bps=cfg.cvar_gate.alpha_bps,
            threshold_paisa=cfg.cvar_gate.threshold_paisa,
        )

    return SweepRunSummary(
        enumerated=enumerated,
        conflict_pruned=conflict_pruned,
        skipped_dedup=skipped_dedup,
        evaluated=evaluated,
        ranked=n_ranked,
        persisted=persisted,
        store_content_hash=flush.content_hash,
        backtested=backtested,
        minhits_dropped=evaluated - len(kept),
        trial_n_finite=trial_n_finite,
        trial_sum_sharpe=trial_sum_sharpe,
        trial_sum_sharpe_sq=trial_sum_sharpe_sq,
        trial_sidecar_written=sidecar_written,
        sentinel_written=sentinel_written,
        temporal_gated=0 if temporal_runtime is None else temporal_runtime.stats.killed,
        temporal_observed_kills=(
            0 if temporal_runtime is None else temporal_runtime.stats.observed_kills
        ),
        temporal_abstained=(
            False if temporal_runtime is None else temporal_runtime.stats.abstained
        ),
        temporal_judged_years=0 if temporal_runtime is None else temporal_runtime.n_judged,
        cvar_rejected=cvar_rejected,
        gated_pass_truncated=gated_pass_truncated,
        frontier_dropped=frontier_dropped,
    )


__all__ = [
    "HIT_MEMO_MAX_ENTRIES",
    "ComboDedup",
    "SweepAprioriBudgetError",
    "SweepAprioriUnsupportedError",
    "SweepComboError",
    "SweepMemoryBudgetError",
    "SweepRunConfig",
    "SweepRunSummary",
    "canonical_trade_params",
    "mask_run_id",
    "ranked_combo_to_result_row",
    "run_persisted_sweep",
]
