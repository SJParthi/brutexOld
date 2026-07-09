"""Full multi-parameter sweep ORCHESTRATOR (DEC-SWEEP-ORCHESTRATOR-001).

Phase 3 Wave-2a: the seam that turns the single-slice
:func:`brutex.engine.sweep_run.run_persisted_sweep` (which sweeps the mask space
for ONE ``(instrument, tf, direction, params)`` tuple) into a **full grid sweep**
over the outer product::

    instrument × timeframe × direction × K × stop-loss × take-profit × proximity

For every tuple in that product the orchestrator builds a
:class:`~brutex.engine.sweep_run.SweepRunConfig`, loads that tuple's bars, calls
:func:`~brutex.engine.sweep_run.run_persisted_sweep`, and persists every ranked
finalist to that tuple's own :class:`~brutex.engine.results_store.ResultsStore`
(one Parquet file per tuple). It **composes** the golden-locked single-slice
runner and re-implements none of it (CLAUDE.md §11 COMMON).

Design (driven by the CLAUDE.md §9 4-agent hostile review)
----------------------------------------------------------
* **One store (Parquet file) per tuple.** :meth:`ResultsStore.flush
  <brutex.engine.results_store.ResultsStore.flush>` re-serialises the *whole*
  in-memory row dict on every call, so a single shared store across ``T`` tuples
  would make each flush O(all-finalists-so-far) → quadratic. A per-tuple store
  keeps each flush O(finalists-for-that-tuple).
* **Deterministic, collision-proof per-tuple paths.** Each tuple's file lives at
  ``<root>/<instrument>/<tf>/<direction>_k<k>_<digest>.parquet`` where ``digest``
  is a BLAKE3 of the tuple's *full* canonical identity (incl. sl/tp/proximity/
  prior_n/equity + the provenance context). The human prefix is legibility only;
  the digest is the collision guard — ``sl=None`` and ``sl=50000`` map to
  different files, so one tuple's results can NEVER silently overwrite another's.
* **Idempotent re-run is byte-identical AND lossless** (Golden Rule #4 / #7).
  :func:`make_store_for` *loads* a tuple's existing Parquet before the runner
  touches it (:func:`load_or_create_store`). So a re-run — whether the shared
  dedup skips every mask (true resume) or re-evaluates them (fresh dedup) —
  re-flushes the SAME finalist rows and produces a byte-identical file. Without
  this preload a resume against a populated dedup would flush an *empty* store
  over good results (the hostile review's P0 data-loss finding).
* **Shared dedup + single audit chain across the grid.** The dedup is keyed on
  the 9-field run-identity (instrument/tf/direction/mode/params all participate),
  so two tuples never alias each other; sharing one
  :class:`~brutex.engine.sweep_run.ComboDedup` only adds resume-skip, never a
  false skip. The single :class:`~brutex.orchestrator.audit.AuditWriter` is
  bracketed by ``GRID_SWEEP_START`` / ``GRID_SWEEP_COMPLETE`` lines around the
  per-tuple ``SWEEP_*`` lines; because the writer is not thread-safe the grid
  loop is **strictly sequential** (parallel tuples are a tracked W2-c follow-up).

Honest envelope (Golden Rule #10) — NO O(1)-total claim
-------------------------------------------------------
Each *operation* is O(1): :func:`count_sweep_tuples` (a pure product),
:func:`iter_sweep_tuples`'s ``next`` (an :func:`itertools.product` step),
:meth:`ComboDedup.seen <brutex.engine.sweep_run.ComboDedup.seen>`, and one
tuple's config build. The **grid as a whole is inherently O(T) tuples ×
O(N) masks each** = O(Σ Nᵢ) work, plus O(Σ finalists) stored rows — claiming
O(1) total time/space for a whole sweep would be a physical impossibility and is
FORBIDDEN. A resume even re-pays mask enumeration + the BLAKE3 run-id + the
dedup probe per mask (only the expensive trade-sim is skipped). :func:`run_sweep_grid`
holds only scalar tallies, so its own memory is O(1) in ``T`` (the per-tuple
detail lives in the audit chain + the Parquet files, streamed via ``on_tuple``).
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from math import prod
from pathlib import Path
from typing import TYPE_CHECKING, Final

import blake3
import structlog

from brutex.engine.combo_mask import MAX_VOCAB_BITS, count_combos
from brutex.engine.results_store import ResultsStore, ResultsStoreSchemaError
from brutex.engine.sweep_driver import DEFAULT_MAX_COMBOS, DEFAULT_START_EQUITY_PAISA
from brutex.engine.sweep_kernel import ResidentKernelCache
from brutex.engine.sweep_run import (
    SweepComboError,
    SweepRunConfig,
    SweepRunSummary,
    run_persisted_sweep,
)
from brutex.ranking import MIN_TRADES, OverfitGateConfig
from brutex.strategies.types import FibAnchorKind, Side
from brutex.universe import is_engine_sweep_symbol
from brutex.vocabulary import ProximityParams

if TYPE_CHECKING:
    # Annotation-only — keep the `fastbloom_rs` PyO3 wheel (pulled transitively
    # by brutex.orchestrator.audit via brutex.orchestrator.bloom) and the
    # brutex.data.option_dedup wheel out of this module's runtime import graph,
    # so the strict per-module `--cov=brutex.engine` gate stays clean (the same
    # constraint sweep_run.py observes). Both are *received*, never constructed.
    from brutex.data.models import CandleBar
    from brutex.engine.sweep_run import DedupSink
    from brutex.orchestrator.audit import AuditSink

_LOG = structlog.get_logger("brutex.engine.sweep_orchestrator")

#: Audit action labels — the two lines the grid emits around the per-tuple lines.
_ACTION_GRID_START: Final[str] = "GRID_SWEEP_START"
_ACTION_GRID_COMPLETE: Final[str] = "GRID_SWEEP_COMPLETE"

#: Audit action for a tuple that raised mid-evaluation and was SKIPPED
#: ("Resilient: skip + continue") — names the exact offending combo + error.
_ACTION_TUPLE_FAILED: Final[str] = "TUPLE_FAILED"

#: Remediation string for a per-tuple CORRUPT / old-schema finalist store
#: (DEC-SWEEP-STORE-CORRUPT-CONTAIN-001, MIGRATION.md #1632). A store that raises
#: :class:`~brutex.engine.results_store.ResultsStoreSchemaError` at LOAD is
#: CONTAINED to a ``TUPLE_FAILED`` skip (exit 6), never a whole-grid abort — but
#: it stays in place (un-quarantined, un-sentineled), so every resume re-counts it
#: failed until the operator heals it. Deliberately NOT auto-quarantined: a fresh
#: empty store flushed over a still-populated grid-wide MASK dedup would silently
#: drop the tuple's finalists (the #650 / sweep_heal C5 empty-flush wedge).
#:
#: The command chain is COPY-PASTE RUNNABLE against the real CLI (review round 1
#: MAJOR-1 — the prior text named a nonexistent ``--out-root`` flag and skipped
#: the required verify→emit-plan→heal steps): the ``--`` flags below are pinned
#: against the LIVE argparse surface by
#: ``tests/unit/test_engine_sweep_orchestrator.py::TestRemediationFlagsRunnable``
#: so the string can never rot again.
TUPLE_STORE_CORRUPT_REMEDIATION: Final[str] = (
    "finalist store unreadable — run 'brutex sweep-verify --results-root "
    "<results-root> --recount --emit-heal-plan' with the SAME grid flags the "
    "sweep ran with (writes the heal plan under <results-root>/heal/), then "
    "'brutex sweep-heal <results-root>/heal/<plan>.json --results-root "
    "<results-root> --execute' (quarantines the store AND wipes checkpoint/ and "
    "dedup-cache/ so the tuple genuinely recomputes) — or move the corrupt store "
    "file aside and wipe both skip-tier dirs manually; NOTE: when the plan "
    "condemns >= 50% of the grid's expected stores (e.g. a single-tuple grid), "
    "'sweep-heal' refuses until --allow-mass-quarantine is also passed (the C7 "
    "mass-quarantine gate)"
)

#: Default ceiling on the number of tuples a grid may enumerate (the honest
#: opt-in guard — a grid this large is a conscious choice, raise it explicitly).
DEFAULT_MAX_TUPLES: Final[int] = 100_000

#: Length of the BLAKE3 hex prefix used as the per-tuple filename digest.
_PATH_DIGEST_LEN: Final[int] = 16


@dataclass(frozen=True, slots=True)
class Timeframe:
    """A timeframe axis value: the lake interval *label* plus its whole *minutes*.

    Attributes
    ----------
    label
        The lake interval label (e.g. ``"1minute"``, ``"5minute"``) — stamped on
        the result rows + used to load the tuple's bars; non-empty.
    minutes
        The interval in whole minutes (``>= 1``) — the trade-sim timeframe knob.

    Raises
    ------
    ValueError
        If *label* is empty or *minutes* ``< 1``.
    """

    label: str
    minutes: int

    def __post_init__(self) -> None:
        if not self.label:
            raise ValueError("Timeframe.label must be non-empty")
        if self.minutes < 1:
            raise ValueError(f"Timeframe.minutes must be >= 1, got {self.minutes}")


@dataclass(frozen=True, slots=True)
class SweepGridConfig:
    """Immutable configuration for a full multi-parameter grid sweep.

    Defines the seven sweep axes (instrument × timeframe × direction × K ×
    stop-loss × take-profit × proximity) plus the shared run context (mode + the
    three reproducibility hashes) and the per-tuple trade-sim knobs that are NOT
    swept (prior_n, start equity). Every axis is order-preservingly de-duplicated
    at construction (so ``k_values=[2, 2]`` is one tuple, not two) and the whole
    config is validated so an impossible grid fails fast — incl. the two honest
    opt-in guards (``max_tuples`` on the grid size, ``max_combos`` on each
    tuple's mask count).

    Attributes
    ----------
    instruments
        Instrument symbols to sweep (e.g. ``["NIFTY", "BANKNIFTY"]``); non-empty,
        each label non-empty.
    timeframes
        :class:`Timeframe` axis values; non-empty.
    directions
        Trade directions (:data:`~brutex.strategies.types.Side.BUY` /
        ``Side.SELL``); non-empty. ``Side.HOLD`` is rejected (a sweep backtests a
        directional edge) so it can never crash the runner mid-grid.
    k_values
        How many of the chosen conditions each combo requires; non-empty, each in
        ``[0, n_unique]`` where ``n_unique = len(set(condition_bits))``.
    condition_bits
        The condition bit positions (of the 60-condition vocabulary) to
        brute-force over; non-empty, de-duplicated for the ``k`` bound + the
        mask enumeration.
    stop_losses, take_profits
        Stop-loss / take-profit distances in paisa (``> 0``), or ``None`` to
        disable that leg; each axis non-empty.
    proximities
        Proximity tolerances for the ``near_*`` conditions, or ``None`` for the
        Stage-1A default; non-empty.
    fib_anchors
        The swept Fibonacci-anchor families
        (:class:`~brutex.strategies.types.FibAnchorKind`) — prev-day /
        rolling-weekly / weekly-static (DEC-NIFTY-FIB-AXIS-001); non-empty.
        Defaults to ``(FibAnchorKind.PREV_DAY,)`` so an unset grid is
        byte-identical to the pre-axis prev-day-only sweep (DEFAULT-PRESERVATION).
        The anchor is a swept STRATEGY parameter (ADR-0012 D4), not a vocabulary
        bit — it moves ONLY the Group-4 Fib levels the sweep evaluates against, and
        enters both the store address (:func:`_tuple_digest`) AND every mask's
        run-identity (``canonical_trade_params``) so two anchors never collide.
    mode, data_snapshot_hash, indicator_version, code_commit_sha
        The shared run context + the three reproducibility identity fields; each
        non-empty.
    prior_n
        Prior-bar pattern look-back (``>= 1``), shared across the grid.
    start_equity_paisa
        Notional capital base (paisa, ``> 0``) for the percentage-metric
        denominator, shared across the grid.
    max_combos
        Refuse any tuple whose ``C(n_unique, k)`` mask count exceeds this (the
        per-tuple honest guard; forwarded to each
        :class:`~brutex.engine.sweep_run.SweepRunConfig`).
    max_tuples
        Refuse a grid whose total tuple count exceeds this (the grid-size honest
        guard).
    top_k
        Persist only the top ``top_k`` ranked combos per tuple, or ``None`` for
        all ranked.
    min_hits
        Stage-1B coarse pre-filter threshold (defaults to
        :data:`brutex.ranking.MIN_TRADES`); ``0`` disables it.
    overfit
        Opt-in :class:`~brutex.ranking.overfit.OverfitGateConfig`, forwarded to
        every tuple's :class:`~brutex.engine.sweep_run.SweepRunConfig` so the
        Stage-3 multiple-testing overfit gate (DEC-RANKING-OVERFIT-001) runs per
        leaderboard. ``None`` (the default) keeps the legacy ranking. Each tuple's
        own backtested-combo count is its authoritative trial count ``N``.
    unsafe_allow_out_of_scope_instruments
        Layer-1 engine-scope escape hatch (DEC-ENGINE-SCOPE-GUARD-001; default
        ``False``): when False, every instrument must satisfy
        :func:`brutex.universe.is_engine_sweep_symbol` or construction raises.
        IDENTITY-SAFE — never enters ``_tuple_digest`` / ``_grid_descriptor`` /
        ``_tuple_cache_params``.

    Raises
    ------
    ValueError
        If any axis is empty, a context/identity string is empty, a ``k`` is out
        of range, a paisa knob is non-positive, ``prior_n < 1``,
        ``start_equity_paisa <= 0``, ``top_k < 1``, ``min_hits < 0``, a ``k``'s
        ``C(n_unique, k)`` exceeds ``max_combos``, or the total tuple count
        exceeds ``max_tuples``.
    """

    instruments: Sequence[str]
    timeframes: Sequence[Timeframe]
    directions: Sequence[Side]
    k_values: Sequence[int]
    condition_bits: Sequence[int]
    stop_losses: Sequence[int | None]
    take_profits: Sequence[int | None]
    proximities: Sequence[ProximityParams | None]
    mode: str
    data_snapshot_hash: str
    indicator_version: str
    code_commit_sha: str
    #: The swept Fib-anchor axis (DEC-NIFTY-FIB-AXIS-001). Defaults to prev-day
    #: only so an unset grid is byte-identical to the pre-axis sweep.
    fib_anchors: Sequence[FibAnchorKind] = (FibAnchorKind.PREV_DAY,)
    prior_n: int = 3
    start_equity_paisa: int = DEFAULT_START_EQUITY_PAISA
    max_combos: int = DEFAULT_MAX_COMBOS
    max_tuples: int = DEFAULT_MAX_TUPLES
    top_k: int | None = None
    min_hits: int = MIN_TRADES
    overfit: OverfitGateConfig | None = None
    #: Stage-3 refinement pass (DEC-STAGE3-WIRE-001, ADR-0007 §5 slice 5) —
    #: DEFAULT-OFF. When enabled, after every grid tuple persists, the launcher
    #: runs the BIPOP-CMA-ES refinement over each (instrument, tf, mode)
    #: leaderboard's top-`stage3_top_k` survivors on the lockbox-excluded
    #: training slice (ADR-0007 Amendment A1) and merges every evaluation into
    #: the same results tree as its own per-quantized-cell file.
    stage3_enable: bool = False
    stage3_top_k: int = 10_000
    stage3_max_fevals: int = 200
    stage3_max_restarts: int = 3
    #: Layer-1 engine-scope hard invariant (DEC-ENGINE-SCOPE-GUARD-001): by
    #: DEFAULT every instrument must satisfy
    #: :func:`brutex.universe.is_engine_sweep_symbol` (the 3 brute-force spot
    #: indexes, CLAUDE.md §2 Rule 3, plus the ADR-0012 NIFTY-futures shapes) so
    #: even PROGRAMMATIC construction cannot silently sweep India VIX / an F&O
    #: stock. The ONLY production True site is the ``sweep-verify --recount``
    #: grid rebuild (verifying recorded work, not launching); the CLI research
    #: escape is ``--allow-out-of-scope`` (loud + audit-stamped). IDENTITY-SAFE
    #: by construction: this field enters NONE of ``_tuple_digest`` /
    #: ``_grid_descriptor`` / ``_tuple_cache_params`` (all explicit field
    #: enumerations) — digests, checkpoints, cache keys and store paths stay
    #: byte-identical for in-scope grids (regression-pinned).
    unsafe_allow_out_of_scope_instruments: bool = False

    def __post_init__(self) -> None:
        # Order-preserving de-dup of every axis (set semantics, deterministic
        # order) so a repeated axis value never yields a duplicate tuple.
        object.__setattr__(self, "instruments", _dedup_seq(self.instruments))
        object.__setattr__(self, "timeframes", _dedup_seq(self.timeframes))
        object.__setattr__(self, "directions", _dedup_seq(self.directions))
        object.__setattr__(self, "k_values", _dedup_seq(self.k_values))
        object.__setattr__(self, "condition_bits", _dedup_seq(self.condition_bits))
        object.__setattr__(self, "stop_losses", _dedup_seq(self.stop_losses))
        object.__setattr__(self, "take_profits", _dedup_seq(self.take_profits))
        object.__setattr__(self, "proximities", _dedup_seq(self.proximities))
        object.__setattr__(self, "fib_anchors", _dedup_seq(self.fib_anchors))

        for name in (
            "instruments",
            "timeframes",
            "directions",
            "k_values",
            "condition_bits",
            "stop_losses",
            "take_profits",
            "proximities",
            "fib_anchors",
        ):
            if not getattr(self, name):
                raise ValueError(f"SweepGridConfig.{name} must be a non-empty axis")
        for symbol in self.instruments:
            if not symbol:
                raise ValueError("SweepGridConfig.instruments entries must be non-empty")
        # Layer-1 engine-scope hard invariant (DEC-ENGINE-SCOPE-GUARD-001):
        # deny-by-default at CONSTRUCTION so no programmatic caller can sweep
        # an out-of-engine-scope instrument silently. Coordinate correctness
        # (exchange/segment) is Layer 2's job (engine/sweep_scope.py) — this
        # layer sees only bare symbols.
        if not self.unsafe_allow_out_of_scope_instruments:
            for symbol in self.instruments:
                if not is_engine_sweep_symbol(symbol):
                    raise ValueError(
                        f"SweepGridConfig.instruments contains {symbol!r}, which is "
                        "outside the brute-force engine scope — allowed: the 3 spot "
                        "indexes NIFTY/BANKNIFTY/SENSEX (CLAUDE.md §2 Rule 3) plus "
                        "NIFTY futures (ADR-0012; series-key 'NIFTY-FUT-YYYY-MM-DD' "
                        "or a lake 'NSE-NIFTY-<DDMmmYY>-FUT' contract symbol). "
                        "Research escape hatch: construct with "
                        "unsafe_allow_out_of_scope_instruments=True "
                        "(the CLI flag is --allow-out-of-scope)"
                    )
        if Side.HOLD in self.directions:
            raise ValueError(
                "SweepGridConfig.directions must not contain Side.HOLD "
                "(a sweep backtests a directional edge)"
            )
        for name in ("mode", "data_snapshot_hash", "indicator_version", "code_commit_sha"):
            if not getattr(self, name):
                raise ValueError(f"SweepGridConfig.{name} must be non-empty")

        # Validate every condition bit is in the u64 vocabulary range AT CONFIG
        # TIME (DEC-SWEEP-DEDUP-LMDB-001): the shared axis otherwise fails LATE
        # as a bare ValueError from the lazy enumeration inside one tuple's
        # ``SweepRunConfig`` — which the grid loop does NOT catch (only
        # SweepComboError is a tuple-skip), aborting the WHOLE grid mid-run.
        # Caught here, the CLI surfaces it as a clean user-config error.
        out_of_range = sorted({bit for bit in self.condition_bits if not 0 <= bit < MAX_VOCAB_BITS})
        if out_of_range:
            raise ValueError(
                f"SweepGridConfig.condition_bits {out_of_range} out of range [0, {MAX_VOCAB_BITS})"
            )
        n_unique = len(set(self.condition_bits))
        for k in self.k_values:
            if not 0 <= k <= n_unique:
                raise ValueError(
                    f"SweepGridConfig.k_values contains {k}, must be in [0, {n_unique}] "
                    f"({n_unique} unique condition bits)"
                )
        for axis in ("stop_losses", "take_profits"):
            for value in getattr(self, axis):
                if value is not None and value <= 0:
                    raise ValueError(
                        f"SweepGridConfig.{axis} entries must be > 0 or None, got {value}"
                    )
        if self.prior_n < 1:
            raise ValueError(f"SweepGridConfig.prior_n must be >= 1, got {self.prior_n}")
        if self.start_equity_paisa <= 0:
            raise ValueError(
                f"SweepGridConfig.start_equity_paisa must be > 0, got {self.start_equity_paisa}"
            )
        if self.top_k is not None and self.top_k < 1:
            raise ValueError(f"SweepGridConfig.top_k must be >= 1 or None, got {self.top_k}")
        if self.min_hits < 0:
            raise ValueError(f"SweepGridConfig.min_hits must be >= 0, got {self.min_hits}")

        # Per-tuple guard: reject up front any k whose mask count exceeds the cap,
        # so a tuple can never raise mid-grid AFTER earlier tuples persisted.
        for k in self.k_values:
            n_masks = count_combos(k, n_unique)
            if n_masks > self.max_combos:
                raise ValueError(
                    f"SweepGridConfig: k={k} enumerates {n_masks} masks "
                    f"(C({n_unique},{k})) exceeding max_combos={self.max_combos}; "
                    f"raise max_combos explicitly to sweep this wide"
                )
        # Grid-size guard: reject a grid with too many tuples (the product is O(1)
        # to compute, so this is a cheap fail-fast before any bar load).
        total = count_sweep_tuples(self)
        if total > self.max_tuples:
            raise ValueError(
                f"SweepGridConfig: {total} tuples exceed max_tuples={self.max_tuples}; "
                f"raise max_tuples explicitly to run a grid this large"
            )
        # Stage-3 guards (DEC-STAGE3-WIRE-001, hostile F5/F13): the refinement
        # x0 must normalize on the Mode-A grid, so an enabled pass REQUIRES
        # every SL/TP axis value to be non-None and on-grid — checked HERE,
        # before any bar load, so the post-pass can never crash after the whole
        # grid persisted. Silent snapping is forbidden (the slice-3 rationale).
        if self.stage3_top_k < 1:
            raise ValueError(f"SweepGridConfig.stage3_top_k must be >= 1, got {self.stage3_top_k}")
        if self.stage3_max_fevals < 1 or self.stage3_max_restarts < 1:
            raise ValueError(
                "SweepGridConfig.stage3_max_fevals and stage3_max_restarts must be >= 1, "
                f"got {self.stage3_max_fevals}/{self.stage3_max_restarts}"
            )
        if self.stage3_enable:
            from brutex.engine.stage3_grid import (
                MODE_A_SL_AXIS,
                MODE_A_TP_AXIS,
                GridAxis,
                Stage3GridError,
            )

            def _require_on_grid(label: str, axis: GridAxis, values: Sequence[int | None]) -> None:
                for value in values:
                    if value is None:
                        raise ValueError(
                            f"SweepGridConfig.stage3_enable requires every {label} value "
                            f"to be a Mode-A grid paisa int — got None (the refinement x0 "
                            f"must normalize on the grid; ADR-0007 condition 3)"
                        )
                    try:
                        axis.normalize(value)
                    except Stage3GridError as exc:
                        raise ValueError(
                            f"SweepGridConfig.stage3_enable requires every {label} value "
                            f"on the Mode-A grid: {exc}"
                        ) from exc

            _require_on_grid("stop_losses", MODE_A_SL_AXIS, self.stop_losses)
            _require_on_grid("take_profits", MODE_A_TP_AXIS, self.take_profits)


@dataclass(frozen=True, slots=True)
class SweepTuple:
    """One point of the sweep grid — a single ``(instrument, tf, direction, K, sl, tp, proximity, fib_anchor)``.

    Attributes
    ----------
    instrument
        The instrument symbol.
    tf
        The :class:`Timeframe` (label + minutes).
    direction
        The trade direction (:data:`~brutex.strategies.types.Side.BUY` / ``SELL``).
    k
        How many of the grid's conditions this combo requires.
    sl_paisa, tp_paisa
        Stop-loss / take-profit distances in paisa, or ``None``.
    proximity
        Proximity tolerances, or ``None`` for the Stage-1A default.
    fib_anchor
        The swept Fib-anchor family
        (:class:`~brutex.strategies.types.FibAnchorKind`) this tuple evaluates the
        Group-4 Fib levels against (DEC-NIFTY-FIB-AXIS-001). Defaults to
        ``FibAnchorKind.PREV_DAY`` so a tuple built without the axis keeps the
        pre-axis behaviour + store address.
    """

    instrument: str
    tf: Timeframe
    direction: Side
    k: int
    sl_paisa: int | None
    tp_paisa: int | None
    proximity: ProximityParams | None
    fib_anchor: FibAnchorKind = FibAnchorKind.PREV_DAY

    def to_run_config(self, grid: SweepGridConfig) -> SweepRunConfig:
        """Build the single-slice :class:`SweepRunConfig` for this tuple under *grid*.

        Threads the grid's shared context (mode + the three reproducibility
        hashes), shared knobs (``condition_bits``, ``prior_n``,
        ``start_equity_paisa``), the honest guards (``max_combos``, ``top_k``,
        ``min_hits``) and the opt-in ``overfit`` gate config together with this
        tuple's swept axes.

        Parameters
        ----------
        grid
            The owning :class:`SweepGridConfig`.

        Returns
        -------
        SweepRunConfig
            The validated config to hand to
            :func:`~brutex.engine.sweep_run.run_persisted_sweep`.
        """
        return SweepRunConfig(
            instrument=self.instrument,
            tf=self.tf.label,
            direction=self.direction,
            mode=grid.mode,
            condition_bits=grid.condition_bits,
            k=self.k,
            data_snapshot_hash=grid.data_snapshot_hash,
            indicator_version=grid.indicator_version,
            code_commit_sha=grid.code_commit_sha,
            tf_minutes=self.tf.minutes,
            sl_paisa=self.sl_paisa,
            tp_paisa=self.tp_paisa,
            prior_n=grid.prior_n,
            proximity=self.proximity,
            fib_anchor=self.fib_anchor,
            start_equity_paisa=grid.start_equity_paisa,
            max_combos=grid.max_combos,
            top_k=grid.top_k,
            min_hits=grid.min_hits,
            overfit=grid.overfit,
        )


@dataclass(frozen=True, slots=True)
class SweepGridSummary:
    """The aggregate tally :func:`run_sweep_grid` returns (and stamps into the audit).

    Holds only scalar totals, so a grid's memory footprint is O(1) in the tuple
    count ``T`` — the per-tuple detail lives in the audit chain + the Parquet
    files (and is streamed live through the ``on_tuple`` hook).

    Attributes
    ----------
    tuples_total
        The full grid size (``count_sweep_tuples(grid)``).
    tuples_run
        Tuples that had bars and were evaluated.
    tuples_skipped_no_bars
        Tuples skipped because ``bars_for`` returned no bars (only when
        ``skip_empty=True``).
    enumerated, conflict_pruned, skipped_dedup, evaluated, ranked, persisted
        The per-tuple :class:`~brutex.engine.sweep_run.SweepRunSummary` fields,
        summed across every tuple that ran.
    tuples_failed
        Tuples whose combo evaluation raised a
        :class:`~brutex.engine.sweep_run.SweepComboError` mid-sweep and were
        SKIPPED ("Resilient: skip + continue"): the remaining tuples still ran,
        but a non-zero count makes the grid verdict NON-ZERO and leaves each
        failed tuple un-sentineled / un-cached for a deterministic redo next run.
        NOT counted as run or skipped-no-bars.
    """

    tuples_total: int
    tuples_run: int
    tuples_skipped_no_bars: int
    enumerated: int
    conflict_pruned: int
    skipped_dedup: int
    evaluated: int
    ranked: int
    persisted: int
    tuples_failed: int


def _dedup_seq[T](seq: Sequence[T]) -> tuple[T, ...]:
    """Return *seq* as a tuple with duplicates removed, preserving first-seen order."""
    return tuple(dict.fromkeys(seq))


def count_sweep_tuples(grid: SweepGridConfig) -> int:
    """Return the total number of tuples in *grid*'s outer product — O(1).

    The product of the seven axis cardinalities (after the construction-time
    de-dup). Pure arithmetic; never enumerates the product.

    Parameters
    ----------
    grid
        The grid config.

    Returns
    -------
    int
        ``|instruments| · |timeframes| · |directions| · |k_values| ·
        |stop_losses| · |take_profits| · |proximities| · |fib_anchors|``.
    """
    return prod(
        (
            len(grid.instruments),
            len(grid.timeframes),
            len(grid.directions),
            len(grid.k_values),
            len(grid.stop_losses),
            len(grid.take_profits),
            len(grid.proximities),
            len(grid.fib_anchors),
        )
    )


def iter_sweep_tuples(grid: SweepGridConfig) -> Iterator[SweepTuple]:
    """Yield every :class:`SweepTuple` of *grid*'s outer product, deterministically.

    Streams the product in a fixed nested order (instrument → timeframe →
    direction → k → stop-loss → take-profit → proximity → fib-anchor) so the
    enumeration is reproducible. Each ``next`` is O(1) (an :func:`itertools.product`
    step) with O(1) memory; the generator never materialises the full product.

    Parameters
    ----------
    grid
        The grid config.

    Yields
    ------
    SweepTuple
        One grid point per step.
    """
    for instrument, tf, direction, k, sl, tp, proximity, fib_anchor in itertools.product(
        grid.instruments,
        grid.timeframes,
        grid.directions,
        grid.k_values,
        grid.stop_losses,
        grid.take_profits,
        grid.proximities,
        grid.fib_anchors,
    ):
        yield SweepTuple(
            instrument=instrument,
            tf=tf,
            direction=direction,
            k=k,
            sl_paisa=sl,
            tp_paisa=tp,
            proximity=proximity,
            fib_anchor=fib_anchor,
        )


def bloom_capacity_hint(grid: SweepGridConfig) -> int:
    """Return a Bloom-sizing hint = the total mask identities *grid* may evaluate.

    ``(Σ_k C(n_unique, k)) · (tuples-per-k-fixed)`` — i.e. the number of distinct
    run-identities the shared dedup could insert across the whole grid. The CLI
    launcher passes this as the shared
    :class:`~brutex.data.option_dedup.OptionDedupIndex` capacity so a large grid's
    Bloom is sized for the grid total, not one tuple (an undersized Bloom
    fails closed). Honest envelope (Golden Rule #10): this is an upper bound on
    *enumerated* masks, not a promise the sweep evaluates them all (conflict +
    Stage-1B pruning + dedup skips reduce the live count).

    Parameters
    ----------
    grid
        The grid config.

    Returns
    -------
    int
        ``>= 1`` — the per-grid mask-identity upper bound.
    """
    n_unique = len(set(grid.condition_bits))
    masks_per_tuple = sum(count_combos(k, n_unique) for k in grid.k_values)
    other_axes = prod(
        (
            len(grid.instruments),
            len(grid.timeframes),
            len(grid.directions),
            len(grid.stop_losses),
            len(grid.take_profits),
            len(grid.proximities),
            len(grid.fib_anchors),
        )
    )
    return max(1, masks_per_tuple * other_axes)


def _tuple_digest(tuple_: SweepTuple, grid: SweepGridConfig) -> str:
    """Return the BLAKE3 hex digest of *tuple_*'s full canonical identity under *grid*.

    Uniquely + stably addresses the tuple's Parquet file. Includes every field
    that distinguishes one store from another — the swept axes AND the shared
    provenance context — so two distinct tuples cannot collide and the same tuple
    always maps to the same file (Golden Rule #4). ``None`` legs encode distinctly
    from any numeric value (JSON ``null`` vs a number).
    """
    proximity_bps = (
        None
        if tuple_.proximity is None
        else [
            tuple_.proximity.pivot_proximity_bps,
            tuple_.proximity.pdh_pdl_proximity_bps,
            tuple_.proximity.fib_proximity_bps,
        ]
    )
    descriptor = {
        "instrument": tuple_.instrument,
        "tf": tuple_.tf.label,
        "tf_minutes": tuple_.tf.minutes,
        "direction": tuple_.direction.value,
        "k": tuple_.k,
        "condition_bits": sorted(set(grid.condition_bits)),
        "sl_paisa": tuple_.sl_paisa,
        "tp_paisa": tuple_.tp_paisa,
        "prior_n": grid.prior_n,
        "proximity_bps": proximity_bps,
        # The swept Fib anchor distinguishes the store address (DEC-NIFTY-FIB-AXIS-001):
        # two tuples differing ONLY by anchor evaluate DIFFERENT Fib levels, so they
        # MUST map to different Parquet files (Golden Rule #4 — else one silently
        # overwrites the other).
        "fib_anchor": tuple_.fib_anchor.value,
        "start_equity_paisa": grid.start_equity_paisa,
        "mode": grid.mode,
        "data_snapshot_hash": grid.data_snapshot_hash,
        "indicator_version": grid.indicator_version,
        "code_commit_sha": grid.code_commit_sha,
    }
    canonical = json.dumps(descriptor, sort_keys=True, separators=(",", ":"))
    # str(...) keeps this typed str where the blake3 stubs are absent (the
    # isolated pre-commit mypy env — the #575 hook-portability precedent).
    return str(blake3.blake3(canonical.encode("utf-8")).hexdigest())[:_PATH_DIGEST_LEN]


def tuple_store_path(root: Path, tuple_: SweepTuple, grid: SweepGridConfig) -> Path:
    """Return the deterministic per-tuple Parquet path under *root*.

    ``<root>/<instrument>/<tf>/<direction>_k<k>_<digest>.parquet``. The human
    prefix is legibility only; the BLAKE3 ``digest`` (:func:`_tuple_digest`) is
    the collision guard — distinct tuples never share a path, and the same tuple
    always resolves to the same path (so a re-run overwrites byte-identically).

    Parameters
    ----------
    root
        The grid's results root directory.
    tuple_
        The grid point.
    grid
        The owning grid config (its context participates in the digest).

    Returns
    -------
    pathlib.Path
        The Parquet file path for this tuple's results.
    """
    digest = _tuple_digest(tuple_, grid)
    filename = f"{tuple_.direction.value}_k{tuple_.k}_{digest}.parquet"
    return root / tuple_.instrument / tuple_.tf.label / filename


def load_or_create_store(path: Path) -> ResultsStore:
    """Return a :class:`ResultsStore` for *path*, pre-loaded if the file exists.

    The idempotence keystone (Golden Rule #4 / #7): by loading an existing
    Parquet into the store BEFORE the runner flushes it, a re-run that skips every
    mask (true resume against a populated dedup) re-flushes the SAME rows — a
    byte-identical file — instead of overwriting good results with an empty store.

    Parameters
    ----------
    path
        The destination Parquet file.

    Returns
    -------
    ResultsStore
        A store loaded from *path* if it exists, else a fresh empty store
        targeting *path*.
    """
    if path.exists():
        return ResultsStore.load(path)
    return ResultsStore(path=path)


def make_store_for(root: Path, grid: SweepGridConfig) -> Callable[[SweepTuple], ResultsStore]:
    """Return a ``store_for`` callback that maps a tuple to its load-or-create store.

    The default store provider for :func:`run_sweep_grid`: it resolves the
    deterministic :func:`tuple_store_path` under *root* and returns a
    :func:`load_or_create_store` for it — giving the grid its
    lossless, byte-identical re-run guarantee out of the box.

    Parameters
    ----------
    root
        The grid's results root directory.
    grid
        The owning grid config.

    Returns
    -------
    Callable[[SweepTuple], ResultsStore]
        The per-tuple store factory.
    """
    return lambda tuple_: load_or_create_store(tuple_store_path(root, tuple_, grid))


def run_one_tuple(
    grid: SweepGridConfig,
    tuple_: SweepTuple,
    *,
    bars_for: Callable[[str, str], Sequence[CandleBar]],
    store_for: Callable[[SweepTuple], ResultsStore],
    dedup: DedupSink,
    audit: AuditSink,
    skip_empty: bool = True,
    kernel_cache: ResidentKernelCache | None = None,
) -> SweepRunSummary | None:
    """Execute ONE grid tuple — the single per-tuple body shared across drivers.

    The COMMON seam (CLAUDE.md §11) between the plain :func:`run_sweep_grid` and
    the guarantee-&-assurance :func:`brutex.engine.sweep_supervisor.run_supervised_grid`:
    load the tuple's bars via *bars_for*; if it has bars, build its
    :class:`~brutex.engine.sweep_run.SweepRunConfig`, obtain its load-or-create
    store via *store_for* (called **only** on the run path, never eagerly — so a
    skip never flushes an empty store over good results), and delegate to
    :func:`~brutex.engine.sweep_run.run_persisted_sweep` (which owns the
    dedup-mark / flush ordering — this seam never marks or flushes itself).

    A tuple that *has bars* but ranks zero finalists returns a real
    :class:`~brutex.engine.sweep_run.SweepRunSummary` with ``persisted == 0`` —
    ``None`` is reserved **strictly** for the no-bars case so a caller can tell
    "no data" from "data, nothing survived".

    Parameters
    ----------
    grid
        The validated grid config.
    tuple_
        The grid point to run.
    bars_for
        ``(instrument, tf_label) -> bars`` loader. An empty sequence means the
        tuple has no data.
    store_for
        ``tuple -> ResultsStore`` factory (called only when the tuple has bars).
    dedup
        The shared :class:`~brutex.engine.sweep_run.ComboDedup`.
    audit
        An open, single-writer :class:`~brutex.orchestrator.audit.AuditWriter`.
    skip_empty
        When ``True`` (default), a tuple with no bars returns ``None``; when
        ``False``, it raises :class:`ValueError`.
    kernel_cache
        Optional bounded :class:`~brutex.engine.sweep_kernel.ResidentKernelCache`
        (DEC-SWEEP-RESIDENT-KERNEL-001). When provided, the Stage-1A bar kernel
        for this tuple's ``(instrument, tf, proximity, prior_n)`` is fetched/reused
        from it and handed to :func:`~brutex.engine.sweep_run.run_persisted_sweep`
        (``kernel=``) — a byte-identical result, only the redundant rebuild is
        skipped (Golden Rule #4). ``None`` rebuilds per tuple (legacy path).

    Returns
    -------
    SweepRunSummary | None
        The per-tuple tally, or ``None`` when the tuple has no bars and
        ``skip_empty`` is ``True``.

    Raises
    ------
    ValueError
        If ``skip_empty=False`` and the tuple has no bars.
    """
    bars = bars_for(tuple_.instrument, tuple_.tf.label)
    if not bars:
        if not skip_empty:
            raise ValueError(
                f"run_sweep_grid: no bars for {tuple_.instrument}/{tuple_.tf.label} "
                f"(skip_empty=False)"
            )
        return None
    kernel = (
        kernel_cache.get(
            bars,
            instrument=tuple_.instrument,
            tf_label=tuple_.tf.label,
            proximity=tuple_.proximity,
            prior_n=grid.prior_n,
            fib_anchor=tuple_.fib_anchor,
        )
        if kernel_cache is not None
        else None
    )
    return run_persisted_sweep(
        bars,
        tuple_.to_run_config(grid),
        store=store_for(tuple_),
        dedup=dedup,
        audit=audit,
        kernel=kernel,
        # Enables the per-tuple trial-stats sidecar (DEC-TRIAL-STATS-WRITE-001):
        # the digest is the same collision guard the store filename carries.
        trial_stats_digest=_tuple_digest(tuple_, grid),
    )


def run_sweep_grid(
    grid: SweepGridConfig,
    *,
    bars_for: Callable[[str, str], Sequence[CandleBar]],
    store_for: Callable[[SweepTuple], ResultsStore],
    dedup: DedupSink,
    audit: AuditSink,
    skip_empty: bool = True,
    on_tuple: Callable[[SweepTuple, SweepRunSummary], None] | None = None,
    reuse_bar_bits: bool = True,
) -> SweepGridSummary:
    """Run the full grid sweep: enumerate the outer product + persist every tuple.

    For each tuple in :func:`iter_sweep_tuples` (strictly sequential — the shared
    audit writer is not thread-safe): load its bars via *bars_for*; build its
    :class:`SweepRunConfig`; obtain its (load-or-create) store via *store_for*;
    call :func:`~brutex.engine.sweep_run.run_persisted_sweep` (which owns the
    dedup-mark / flush ordering — the orchestrator never marks or flushes
    itself); accumulate the tally; and fire *on_tuple*. The grid is bracketed by
    one ``GRID_SWEEP_START`` + one ``GRID_SWEEP_COMPLETE`` audit line.

    A tuple with no bars is either skipped (``skip_empty=True``, counted in
    ``tuples_skipped_no_bars``) or raises a clear, tuple-named error
    (``skip_empty=False``) — never an opaque mid-grid traceback.

    "Resilient: skip + continue": a tuple whose combo evaluation raises a
    :class:`~brutex.engine.sweep_run.SweepComboError` mid-sweep is SKIPPED (its
    store is left un-sentineled so it redoes next run), counted in
    ``tuples_failed``, and recorded as a ``TUPLE_FAILED`` audit line naming the
    exact offending combo; the grid finishes the remaining tuples and a non-zero
    ``tuples_failed`` makes the run's verdict NON-ZERO. A
    :class:`~brutex.engine.results_store.ResultsStoreSchemaError` — a CORRUPT /
    old-schema finalist store raised at its LOAD boundary on resume — is CONTAINED
    identically (DB-02, DEC-SWEEP-STORE-CORRUPT-CONTAIN-001) so one damaged store
    can never abort the whole grid; it also fires a ``sweep_grid.tuple_store_corrupt``
    WARN carrying the store path + heal remediation. Every OTHER exception still
    propagates uncaught (global abort) — only ``SweepComboError`` + the G14
    ``ResultsStoreSchemaError`` are caught (NOT its parent ``ResultsStoreError``).

    Honest envelope (Golden Rule #10): the loop is O(T) tuples, each O(N) masks;
    no part is O(1) total. This function's own memory is O(1) in ``T`` (scalar
    tallies only).

    Parameters
    ----------
    grid
        The validated grid config.
    bars_for
        ``(instrument, tf_label) -> bars`` loader. Returning an empty sequence
        marks the tuple as having no data.
    store_for
        ``tuple -> ResultsStore`` factory (use :func:`make_store_for` for the
        default load-or-create behaviour).
    dedup
        The shared :class:`~brutex.engine.sweep_run.ComboDedup` (resume-skip +
        single dedup state across the grid).
    audit
        An open, single-writer :class:`~brutex.orchestrator.audit.AuditWriter`.
    skip_empty
        When ``True`` (default), a tuple with no bars is skipped + counted; when
        ``False``, it raises :class:`ValueError`.
    on_tuple
        Optional hook fired with ``(tuple, summary)`` after each tuple that ran
        (never for a skipped tuple) — the streaming seam for live progress /
        dashboards without retaining O(T) state.
    reuse_bar_bits
        When ``True`` (default), the grid threads a bounded
        :class:`~brutex.engine.sweep_kernel.ResidentKernelCache` through its tuples
        (DEC-SWEEP-RESIDENT-KERNEL-001): the Stage-1A ``bar_bits`` build + the
        bar-window Python→Rust marshal are paid ONCE per distinct ``(instrument,
        tf, proximity, prior_n)`` rather than once per ``(direction, K, sl, tp)``
        tuple — a byte-identical result (Golden Rule #4), only faster. Set
        ``False`` to rebuild per tuple (the legacy path). Honest envelope (Golden
        Rule #10): the cache holds at most ``|proximities|`` bar windows resident →
        bounded ``O(|proximities| · N_bars)`` memory, never O(1) total.

    Returns
    -------
    SweepGridSummary
        The aggregate tally across the grid.

    Raises
    ------
    ValueError
        If ``skip_empty=False`` and a tuple has no bars.
    """
    total = count_sweep_tuples(grid)
    subject = (
        f"grid:{len(grid.instruments)}i×{len(grid.timeframes)}tf×"
        f"{len(grid.directions)}d:tuples={total}"
    )
    audit.append(
        action=_ACTION_GRID_START,
        subject=subject,
        payload={
            "tuples_total": total,
            "instruments": list(grid.instruments),
            "timeframes": [tf.label for tf in grid.timeframes],
            "directions": [d.value for d in grid.directions],
            "k_values": list(grid.k_values),
            "fib_anchors": [a.value for a in grid.fib_anchors],
            "mode": grid.mode,
            "data_snapshot_hash": grid.data_snapshot_hash,
            "indicator_version": grid.indicator_version,
            "code_commit_sha": grid.code_commit_sha,
        },
    )

    tuples_run = 0
    tuples_skipped_no_bars = 0
    tuples_failed = 0
    enumerated = 0
    conflict_pruned = 0
    skipped_dedup = 0
    evaluated = 0
    ranked = 0
    persisted = 0

    # Bounded resident-kernel cache (DEC-SWEEP-RESIDENT-KERNEL-001): one bar window
    # per distinct (instrument, tf, proximity, prior_n, fib_anchor). Sized to the
    # proximity × fib-anchor fan-out (DEC-NIFTY-FIB-AXIS-001 — the kernel's bar_bits
    # is anchor-SPECIFIC, so each anchor needs its own resident kernel) so a fixed
    # (instrument, tf) reuses every kernel across its direction/K/sl/tp tuples.
    # `None` when reuse is disabled (legacy rebuild path).
    kernel_cache = (
        ResidentKernelCache(capacity=len(grid.proximities) * len(grid.fib_anchors))
        if reuse_bar_bits
        else None
    )

    for tuple_ in iter_sweep_tuples(grid):
        # "Resilient: skip + continue" — a combo that raises mid-evaluation
        # surfaces as a SweepComboError naming the exact offending combo. Skip
        # the WHOLE tuple (it is provably a tuple-wide bug / corrupt bar window,
        # not a transient single combo), count it, audit the fingerprint, and
        # finish the remaining tuples; the failed tuple is left un-sentineled
        # (run_persisted_sweep raised before its flush/mark/sentinel writes) so
        # it redoes deterministically next run. SweepComboError (a combo bug /
        # corrupt bar window) AND ResultsStoreSchemaError (a CORRUPT / old-schema
        # finalist store surfaced at its LOAD boundary) are the two per-tuple
        # containment classes — DELIBERATELY widened (DB-02,
        # DEC-SWEEP-STORE-CORRUPT-CONTAIN-001, MIGRATION.md #1632): one damaged
        # store must never abort the WHOLE grid on resume. Only the G14 schema
        # class is caught, NOT its parent ResultsStoreError (a run-identity
        # CONFLICT / missing-polars env fault keeps the documented global abort).
        # Every OTHER exception still propagates (global abort).
        try:
            summary = run_one_tuple(
                grid,
                tuple_,
                bars_for=bars_for,
                store_for=store_for,
                dedup=dedup,
                audit=audit,
                skip_empty=skip_empty,
                kernel_cache=kernel_cache,
            )
        except (SweepComboError, ResultsStoreSchemaError) as exc:
            tuples_failed += 1
            label = (
                f"{tuple_.instrument}/{tuple_.tf.label}/{tuple_.direction.value}"
                f"/k={tuple_.k}/sl={tuple_.sl_paisa}/tp={tuple_.tp_paisa}"
                f"/fib={tuple_.fib_anchor.value}"
            )
            error = f"{type(exc).__name__}: {exc}"
            audit.append(
                action=_ACTION_TUPLE_FAILED,
                subject=subject,
                payload={"tuple": label, "error": error},
            )
            _LOG.warning("sweep_grid.tuple_failed", subject=subject, tuple=label, error=error)
            if isinstance(exc, ResultsStoreSchemaError):
                # The error string already NAMES the store path (the G14 load
                # contract) — carry it + the heal remediation on the dedicated
                # corrupt-store WARN so the completeness hole is loud + fixable.
                _LOG.warning(
                    "sweep_grid.tuple_store_corrupt",
                    subject=subject,
                    tuple=label,
                    error=error,
                    remediation=TUPLE_STORE_CORRUPT_REMEDIATION,
                )
            continue
        if summary is None:
            tuples_skipped_no_bars += 1
            continue
        tuples_run += 1
        enumerated += summary.enumerated
        conflict_pruned += summary.conflict_pruned
        skipped_dedup += summary.skipped_dedup
        evaluated += summary.evaluated
        ranked += summary.ranked
        persisted += summary.persisted
        if on_tuple is not None:
            on_tuple(tuple_, summary)

    grid_summary = SweepGridSummary(
        tuples_total=total,
        tuples_run=tuples_run,
        tuples_skipped_no_bars=tuples_skipped_no_bars,
        enumerated=enumerated,
        conflict_pruned=conflict_pruned,
        skipped_dedup=skipped_dedup,
        evaluated=evaluated,
        ranked=ranked,
        persisted=persisted,
        tuples_failed=tuples_failed,
    )
    payload = {
        "tuples_total": total,
        "tuples_run": tuples_run,
        "tuples_skipped_no_bars": tuples_skipped_no_bars,
        "tuples_failed": tuples_failed,
        "enumerated": enumerated,
        "conflict_pruned": conflict_pruned,
        "skipped_dedup": skipped_dedup,
        "evaluated": evaluated,
        "ranked": ranked,
        "persisted": persisted,
    }
    audit.append(action=_ACTION_GRID_COMPLETE, subject=subject, payload=payload)
    _LOG.info("sweep_grid.complete", subject=subject, **payload)
    return grid_summary


__all__ = [
    "DEFAULT_MAX_TUPLES",
    "SweepGridConfig",
    "SweepGridSummary",
    "SweepTuple",
    "Timeframe",
    "bloom_capacity_hint",
    "count_sweep_tuples",
    "iter_sweep_tuples",
    "load_or_create_store",
    "make_store_for",
    "run_one_tuple",
    "run_sweep_grid",
    "tuple_store_path",
]
