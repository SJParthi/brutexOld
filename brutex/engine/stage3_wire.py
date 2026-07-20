"""Stage-3 launcher wiring — the audited post-pass over the persisted Stage-2 grid.

DEC-STAGE3-WIRE-001 (ADR-0007 / DEC-STAGE3-DESIGN-LOCK-001 §5 slice 5, built to
the 2026-06-10 slice-5 hostile review's conditions). The slice-4 refinement
runner (:mod:`brutex.engine.stage3_refine`) is engine-seam-injected and knows
nothing about bars, stores or grids; THIS module binds it into the
``brutex sweep-all`` launcher:

* **Condition 7 / Amendment A1 (review F1).** :func:`train_slice_bars` excludes
  the most recent :data:`~brutex.metrics.lockbox.LOCKBOX_DEFAULT_WINDOW_DAYS`
  trading days, mirroring the lockbox day-ordinal window EXACTLY (the shared
  constant, the same IST-date ``toordinal`` axis the cascade feeds filter #8 —
  never a re-derived boundary). Every refinement evaluates on that slice, and
  every refined row is stamped with that slice's content-derived
  :func:`data_snapshot_hash` — the launcher's own derivation, shared here as a
  non-CLI helper (CLAUDE.md §11 COMMON).
* **Review F4.** A persisted :class:`~brutex.engine.results_store.ResultRow`
  does not carry its (SL, TP, proximity); :func:`recover_survivor_cell`
  re-derives :func:`~brutex.engine.sweep_run.mask_run_id` over the grid's
  ``stop_losses × take_profits × proximities`` axes and matches ``row.run_id``
  — recovered deterministically, never guessed.
* **Review F3.** Refined rows land in their OWN per-quantized-cell Parquet
  files via the existing :func:`~brutex.engine.sweep_orchestrator.tuple_store_path`
  with a constructed :class:`~brutex.engine.sweep_orchestrator.SweepTuple`
  (``k = popcount(mask)``) under the TRAIN-hash grid context, so a refined cell
  can never collide with (or silently extend) a Stage-2 grid file.
  :func:`cascade_specs_for_refined_cells` then enumerates those cells for the
  Stage-4 cascade, and :func:`stage3_manifest_record` records enablement + the
  refined-cell list in the cascade manifest.
* **Review F8.** The refined-cell cascade specs carry EVERY refined-eval row
  UNTRIMMED (``top_k=None``) — each evaluation is a Bailey-LdP trial (ADR-0007
  condition 6), so none may be dropped before the cascade's N.
* **Review F9/F10.** Bars come from the launcher's in-memory slice memo
  (``bars_for``) — same-process only; ``bar_bits`` are rebuilt per survivor
  proximity via the golden-locked Stage-1A builder; the whole pass runs on a
  single thread inside the launcher's open audit chain, bracketed by
  ``STAGE_3_PASS_START`` / ``STAGE_3_PASS_COMPLETE`` lines around the per-mask
  ``STAGE_3_REFINE_*`` triples.

Idempotence (Golden Rule #7): survivor selection enumerates ONLY this grid's
Stage-2 cell files (never the whole results tree), so refined-cell files from a
previous run can never feed back into selection; a re-run re-derives the same
evaluations and re-appends identical rows (the store's idempotent no-op),
re-flushing byte-identical Parquet.

Honest envelope (Golden Rule #10): every refined evaluation is a FULL backtest
— O(bars) per eval, ≤ ``max_fevals × max_restarts`` (default ≤600) evals per
mask — and building each refined row RE-SIMULATES the trades for its real OHLC
price anchors (a second O(bars) walk per unique eval). Cell recovery is
O(survivors × |SL|·|TP|·|proximities|) BLAKE3 hashes. NOTHING here is one of
the Rule-#5 O(1) hot paths and no O(1) or wall-time claim is made; this is an
offline between-stage pass.
"""

from __future__ import annotations

import dataclasses
import itertools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import blake3
import structlog

from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.engine.cascade_wire import (
    CascadeTupleSpec,
    Stage3CellRecord,
    Stage3ManifestRecord,
    spec_from_store_rows,
)
from brutex.engine.combo_mask import popcount
from brutex.engine.results_store import ResultRow, ResultsStore, VixTrioConflictError
from brutex.engine.snapshot_hash import data_snapshot_hash
from brutex.engine.stage1a import apply_1day_mask_for, build_bar_bits
from brutex.engine.stage3_refine import (
    Evaluator,
    RefineBudget,
    optimizer_versions,
    refine_mask,
)
from brutex.engine.stage3_select import group_survivors
from brutex.engine.sweep_driver import metrics_for_mask
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    SweepTuple,
    Timeframe,
    _tuple_digest,
    iter_sweep_tuples,
    load_or_create_store,
    tuple_store_path,
)
from brutex.engine.sweep_run import (
    _direction_label,
    canonical_trade_params,
    mask_run_id,
    ranked_combo_to_result_row,
)
from brutex.engine.trial_stats import (
    trial_stats_sidecar_path,
    tuple_trial_stats_from_sharpes,
    write_tuple_trial_stats,
)
from brutex.metrics.lockbox import LOCKBOX_DEFAULT_WINDOW_DAYS
from brutex.models import BruteXError
from brutex.ranking import RankedCombo
from brutex.strategies.types import FibAnchorKind, PriceSourceCell, Side
from brutex.vocabulary import ProximityParams

if TYPE_CHECKING:
    # Annotation-only — keeps brutex.orchestrator.audit (and anything it pulls)
    # out of this module's runtime import graph, mirroring the sweep_run /
    # sweep_orchestrator discipline for the strict per-module coverage gates.
    from brutex.engine.reference_series import TradeReferenceStamper
    from brutex.metrics.compute import ResultMetrics
    from brutex.orchestrator.audit import AuditWriter

_LOG = structlog.get_logger("brutex.engine.stage3_wire")

#: The two pass-level audit actions bracketing the per-mask STAGE_3_REFINE_*
#: triples (review F10) — one START before the first refinement, one COMPLETE
#: after the last cell flush, on the launcher's single hash-chained writer.
# S105/B105 fire because the names contain PASS (in the linters' password
# word-list) + string values — they are audit action labels, not credentials
# (the same precedent as LockboxStatus.PASS).
STAGE3_ACTION_PASS_START: Final[str] = "STAGE_3_PASS_START"  # noqa: S105 # nosec B105
STAGE3_ACTION_PASS_COMPLETE: Final[str] = "STAGE_3_PASS_COMPLETE"  # noqa: S105 # nosec B105


class Stage3WireError(BruteXError):
    """Raised for any Stage-3 wiring failure.

    Covers an unavailable Stage-3 Rust runtime at pass time, non-chronological bars in
    the train-slice helper, a survivor whose grid cell cannot be recovered by
    run-identity match, an unknown direction label / timeframe label on a
    persisted row, and a refined cell carrying a disabled (``None``) SL/TP leg.
    """


def ensure_stage3_runtime() -> tuple[str, str]:
    """Fail fast (and loudly) when the Stage-3 Rust runtime is unavailable.

    Called by the launcher BEFORE any Stage-3 work (and again at pass start) so
    an enabled run can never crash on a missing ``brutex_refine`` wheel AFTER
    the whole grid persisted — the same fail-fast rationale as the on-grid
    axis validation in :class:`~brutex.engine.sweep_orchestrator.SweepGridConfig`.

    Returns
    -------
    tuple[str, str]
        ``(optimizer_version, optimizer_build)`` — the identity fields of
        ADR-0007 condition 4 (DEC-RUST-STAGE3-REFINE-001: the crate version
        tag + the wheel's baked crate-source digest), proving the wheel
        engages.

    Raises
    ------
    Stage3WireError
        If the ``brutex_refine`` wheel cannot be engaged (absent/stale wheel,
        or a falsy/junk ``BRUTEX_REFINE_RUST`` — the Python optimizer math is
        deleted, there is no fallback).
    """
    from brutex.engine.refine_rust import RefineRustUnavailableError

    try:
        return optimizer_versions()
    except RefineRustUnavailableError as exc:
        raise Stage3WireError(
            f"--stage3-enable requires the Stage-3 Rust runtime (DEC-RUST-STAGE3-REFINE-001): {exc}"
        ) from exc


def _data_snapshot_hash_v1(
    *,
    exchange: str,
    segment: str,
    start: str,
    end: str,
    slices: Mapping[tuple[str, str], Sequence[CandleBar]],
) -> str:
    """The RETIRED v1 sampled derivation — recount DIAGNOSTIC only, never identity.

    The pre-DEC-SNAPSHOT-HASH-FULL-001 fingerprint (per slice: bar count +
    first/last timestamp+close), kept module-private so ``sweep-verify
    --recount`` can tell the operator "the recorded hash matches the LEGACY
    sampled derivation — re-sweep to mint a v2 identity" instead of a bare
    mismatch. MUST NOT be exported to any identity/digest/cache path: the v1
    identities were unsound (two different lakes could alias — the exact bug
    v2 kills). O(#slices), mismatch-diagnostic cost only.
    """
    parts: list[str] = [f"{exchange}|{segment}|{start}|{end}"]
    for (symbol, interval), bars in sorted(slices.items()):
        if bars:
            first, last = bars[0], bars[-1]
            parts.append(
                f"{symbol}|{interval}|{len(bars)}|{first.timestamp.isoformat()}|"
                f"{first.close}|{last.timestamp.isoformat()}|{last.close}"
            )
        else:
            parts.append(f"{symbol}|{interval}|0")
    # str(...) — the same blake3-stubs-absent mypy-env guard as above (#575).
    return str(blake3.blake3("\n".join(parts).encode("utf-8")).hexdigest())


def _ist_day_ordinal(bar: CandleBar) -> int:
    """Return *bar*'s IST trading-day ordinal — the lockbox/cascade day axis.

    EXACTLY the axis :func:`brutex.engine.cascade_wire.build_candidate_input`
    feeds the lockbox filter (#8): the bar timestamp converted to IST, its
    calendar date's ``toordinal()``.
    """
    return bar.timestamp.astimezone(IST).date().toordinal()


def train_slice_bars(bars: Sequence[CandleBar]) -> tuple[CandleBar, ...]:
    """Return the lockbox-excluded training slice of *bars* (ADR-0007 condition 7).

    Mirrors :func:`brutex.metrics.lockbox.lockbox_evaluation`'s DEFAULT
    day-ordinal window EXACTLY: with ``max_day`` the last bar's IST day ordinal,
    the lockbox window starts at ``max_day - LOCKBOX_DEFAULT_WINDOW_DAYS + 1``
    and the train slice keeps every bar strictly BEFORE that start. The window
    length is the shared :data:`~brutex.metrics.lockbox.LOCKBOX_DEFAULT_WINDOW_DAYS`
    constant — one source of truth, never a re-derived boundary (review F1).

    Honest envelope (Golden Rule #10): this mirrors the lockbox's DEFAULT
    window only. The lockbox's adaptive backward extension (when the default
    window holds < 500 trades) can consume days from before this boundary at
    cascade time — the uncontaminated arbiter stays the cascade's own lockbox
    gate; Stage-3 simply never trains on the default held-out window.

    Parameters
    ----------
    bars
        Chronologically ordered bars for one ``(instrument, interval)``.

    Returns
    -------
    tuple[CandleBar, ...]
        The train-slice bars (empty when *bars* is empty or spans no more than
        the lockbox window).

    Raises
    ------
    Stage3WireError
        If the bars' IST day ordinals are not non-decreasing (mirrors the
        lockbox's chronological-input validation).
    """
    if not bars:
        return ()
    ordinals = [_ist_day_ordinal(bar) for bar in bars]
    for prev, cur in itertools.pairwise(ordinals):
        if cur < prev:
            raise Stage3WireError(
                "train_slice_bars: bars must be chronological (non-decreasing IST day ordinals)"
            )
    lockbox_start = ordinals[-1] - LOCKBOX_DEFAULT_WINDOW_DAYS + 1
    return tuple(bar for bar, day in zip(bars, ordinals, strict=True) if day < lockbox_start)


def recover_survivor_cell(
    row: ResultRow,
    grid: SweepGridConfig,
    *,
    roll_schedule: tuple[int, ...] | None = None,
) -> tuple[int, int, ProximityParams | None, FibAnchorKind, PriceSourceCell]:
    """Recover *row*'s grid cell ``(sl, tp, proximity, fib_anchor, price_source)`` by identity match.

    A persisted row folds its trade-sim params into the BLAKE3 ``run_id`` but
    does not store them as columns, so the survivor's refinement start cell is
    re-derived (review F4): for each point of the grid's ``stop_losses ×
    take_profits × proximities × fib_anchors × price_source_cells`` product, the
    9-field :func:`~brutex.engine.sweep_run.mask_run_id` is recomputed from the
    row's own labels + the grid's shared context and compared to ``row.run_id``.
    The first (cryptographically unique) match is the cell — never guessed. The
    Fib anchor AND the price-source cell are part of the product
    (DEC-NIFTY-FIB-AXIS-001 / DEC-NIFTY-PRICE-SOURCE-AXIS-001) because both enter
    the run-identity via ``canonical_trade_params``, so the row's ``run_id`` only
    reproduces at the ORIGINATING anchor + source — and Stage-3 must refine that
    survivor against ITS OWN anchor's Fib levels + ITS OWN VWAP/EMA source bits
    (never a stale prev-day / (HLC3, CLOSE) one).

    Parameters
    ----------
    row
        The persisted Stage-2 survivor row.
    grid
        The grid it was swept under (axes + shared identity context).
    roll_schedule
        ADR-0012 futures roll schedule for the row's instrument
        (DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001) — a futures row's ``run_id``
        folds ``roll_schedule`` into ``canonical_trade_params``, so the
        re-derivation must carry the SAME schedule or no cell reproduces.
        ``None`` (every spot row) is byte-identical to the pre-launch
        derivation.

    Returns
    -------
    tuple[int, int, ProximityParams | None, FibAnchorKind, PriceSourceCell]
        The survivor's ``(sl_paisa, tp_paisa, proximity, fib_anchor, price_source)``
        start cell.

    Raises
    ------
    Stage3WireError
        If no grid cell reproduces ``row.run_id`` (the row does not belong to
        this grid), or the matched cell has a disabled (``None``) SL/TP leg —
        Stage-3 refinement needs an on-grid numeric start point (the enabled
        config forbids ``None`` axes up front; this guards direct callers).
    """
    for sl, tp, proximity, fib_anchor, price_source in itertools.product(
        grid.stop_losses,
        grid.take_profits,
        grid.proximities,
        grid.fib_anchors,
        grid.price_source_cells,
    ):
        params = canonical_trade_params(
            sl_paisa=sl,
            tp_paisa=tp,
            prior_n=grid.prior_n,
            proximity=proximity,
            start_equity_paisa=grid.start_equity_paisa,
            fib_anchor=fib_anchor,
            vwap_source=price_source.vwap_source,
            ema_source=price_source.ema_source,
            roll_schedule=roll_schedule,
        )
        candidate = mask_run_id(
            mask=row.mask_bits,
            direction=row.direction,
            instrument=row.instrument,
            tf=row.tf,
            mode=row.mode,
            params=params,
            data_snapshot_hash=grid.data_snapshot_hash,
            indicator_version=grid.indicator_version,
            code_commit_sha=grid.code_commit_sha,
        )
        if candidate == row.run_id:
            if sl is None or tp is None:
                raise Stage3WireError(
                    f"recover_survivor_cell: run {row.run_id[:16]} matched a cell with a "
                    f"disabled leg (sl={sl}, tp={tp}) — Stage-3 needs numeric on-grid legs"
                )
            return sl, tp, proximity, fib_anchor, price_source
    raise Stage3WireError(
        f"recover_survivor_cell: no (sl × tp × proximity × fib_anchor × price_source) cell of "
        f"this grid reproduces run {row.run_id[:16]}… for mask {row.mask_bits:#x} — the row "
        f"does not belong to this grid's identity context"
    )


def _side_for_direction(direction: str) -> Side:
    """Map a persisted direction label back to its :class:`Side` (inverse of the sweep's)."""
    if direction == "bull":
        return Side.BUY
    if direction == "bear":
        return Side.SELL
    raise Stage3WireError(f"unknown persisted direction label {direction!r} (expect bull/bear)")


def _timeframe_for_label(label: str, grid: SweepGridConfig) -> Timeframe:
    """Resolve a persisted row's tf *label* to the grid's :class:`Timeframe`."""
    for tf in grid.timeframes:
        if tf.label == label:
            return tf
    raise Stage3WireError(
        f"persisted row tf label {label!r} is not one of this grid's timeframes "
        f"{[tf.label for tf in grid.timeframes]}"
    )


@dataclass(frozen=True, slots=True)
class _WriterAuditSink:
    """Adapt the launcher's :class:`AuditWriter` to the refine runner's ``AuditSink``.

    The writer's ``append`` takes a concrete ``dict`` payload while the
    :class:`~brutex.engine.stage3_refine.AuditSink` protocol passes a
    ``Mapping`` — this thin adapter materialises the dict, nothing more, so the
    per-mask STAGE_3_REFINE_* lines land on the SAME hash chain as the sweep's.
    """

    writer: AuditWriter

    def append(self, *, action: str, subject: str, payload: Mapping[str, object]) -> object:
        """Forward one hash-chained audit line to the wrapped writer."""
        return self.writer.append(action=action, subject=subject, payload=dict(payload))


def _make_evaluator(
    *,
    bars: Sequence[CandleBar],
    bar_bits: Sequence[int],
    mask: int,
    side: Side,
    tf_minutes: int,
    start_equity_paisa: int,
    reference_stamper: TradeReferenceStamper | None,
) -> Evaluator:
    """Bind one survivor's FULL-backtest evaluator over the TRAIN slice (condition 7).

    The production binding of the slice-4 engine seam: each call is one
    standalone :func:`~brutex.engine.sweep_driver.metrics_for_mask` backtest at
    a quantized on-grid ``(sl_paisa, tp_paisa)`` — O(bars) per call, never O(1).
    The run's India-VIX stamper rides each evaluation's trade sim
    (DEC-VIX-STAMP-WIRE-001; required, no default — the caller decides).
    """

    def _evaluate(sl_paisa: int, tp_paisa: int) -> ResultMetrics:
        return metrics_for_mask(
            bars,
            bar_bits,
            mask,
            direction=side,
            tf_minutes=tf_minutes,
            stop_loss_paisa=sl_paisa,
            take_profit_paisa=tp_paisa,
            start_equity_paisa=start_equity_paisa,
            reference_stamper=reference_stamper,
        )

    return _evaluate


@dataclass(frozen=True, slots=True)
class Stage3RefinedCell:
    """One refined quantized cell written by the pass (review F3).

    Attributes
    ----------
    tuple_
        The constructed :class:`SweepTuple` addressing the cell
        (``k = popcount(mask)``; on-grid int SL/TP legs).
    path
        The cell's own Parquet file (under the TRAIN-hash grid context).
    rows
        Row count in the cell store after the flush.
    """

    tuple_: SweepTuple
    path: Path
    rows: int


@dataclass(frozen=True, slots=True)
class Stage3PassReport:
    """The tally :func:`run_stage3_pass` returns (and stamps into the audit).

    Attributes
    ----------
    enabled
        ``False`` when the grid had ``stage3_enable=False`` (a no-op pass).
    train_snapshot_hash
        The content-derived hash of the lockbox-excluded training slices
        (Amendment A1), or ``None`` when disabled.
    leaderboards
        Number of (instrument, TF, mode) leaderboards selected.
    survivors_total
        Total survivors selected across leaderboards (after the top-K trim).
    masks_refined
        Survivors actually refined (excludes skipped-empty-train leaderboards).
    evals_total
        Unique (cache-miss) evaluations summed across the refinement runs —
        an audit metric. NOT the DSR-N contribution: the honest N contribution
        is the UNIQUE persisted refined rows (cross-survivor duplicate evals
        collapse by identity; ADR-0008 condition 4 / F8).
    cache_hits_total
        CMA asks answered from the run-scoped value caches.
    rows_appended
        Refined rows NEWLY added to cell stores (an idempotent re-run of the
        same pass re-derives identical rows and appends zero).
    sidecars_written
        Cell trial-stats sidecars written this pass (DEC-TRIAL-STATS-WRITE-001;
        a cell whose store holds rows this pass did not derive is SKIPPED
        loudly — its count is ``len(cells) - sidecars_written``).
    skipped_empty_train
        Leaderboards skipped because their train slice was empty (a window no
        longer than the lockbox window leaves nothing to train on).
    cells
        Every refined cell written, in deterministic order (a VIX-conflicted
        cell is EXCLUDED — it was not written this pass).
    cells_conflicted
        Refined cells SKIPPED because their pre-existing store's REFERENCE-ONLY
        VIX trio conflicts with this pass's re-derivation
        (:class:`~brutex.engine.results_store.VixTrioConflictError` — the
        India-VIX lake moved between launches). Each skip leaves the on-disk
        cell store + sidecar untouched, WARNs ``stage3_wire.cell_vix_conflict``
        with the quarantine remedy, and makes the launch verdict non-zero —
        the stage-3 twin of the grid drivers' #904 tuple-skip contract.
    """

    enabled: bool
    train_snapshot_hash: str | None
    leaderboards: int
    survivors_total: int
    masks_refined: int
    evals_total: int
    cache_hits_total: int
    rows_appended: int
    skipped_empty_train: int
    cells: tuple[Stage3RefinedCell, ...]
    sidecars_written: int = 0
    cells_conflicted: int = 0


def _cell_sort_key(cell: SweepTuple) -> tuple[str, str, str, int, int, int, tuple[int, int, int]]:
    """Deterministic ordering key for refined-cell writes (Golden Rule #4)."""
    proximity = cell.proximity
    prox_key = (
        (-1, -1, -1)
        if proximity is None
        else (
            proximity.pivot_proximity_bps,
            proximity.pdh_pdl_proximity_bps,
            proximity.fib_proximity_bps,
        )
    )
    return (
        cell.instrument,
        cell.tf.label,
        cell.direction.value,
        cell.k,
        cell.sl_paisa if cell.sl_paisa is not None else 0,
        cell.tp_paisa if cell.tp_paisa is not None else 0,
        prox_key,
    )


def run_stage3_pass(
    grid: SweepGridConfig,
    *,
    results_root: Path,
    bars_for: Callable[[str, str], Sequence[CandleBar]],
    audit: AuditWriter,
    exchange: str,
    segment: str,
    start: str,
    end: str,
    roll_schedule_by_instrument: Mapping[str, tuple[int, ...]] | None = None,
    stamper_for: Callable[[], TradeReferenceStamper | None] | None = None,
    rollup_paths_hook: Callable[[Sequence[Path]], None] | None = None,
) -> Stage3PassReport:
    """Run the Stage-3 refinement post-pass over a persisted grid — single-threaded.

    The launcher calls this INSIDE its open audit ``with``-block, right after
    the supervised grid returns (review F10). Sequence:

    1. No-op fast path when ``grid.stage3_enable`` is ``False``.
    2. :func:`ensure_stage3_runtime` (fail fast on an unavailable Rust runtime).
    3. Select survivors from ONLY this grid's Stage-2 cell files
       (:func:`~brutex.engine.sweep_orchestrator.iter_sweep_tuples` +
       :func:`~brutex.engine.sweep_orchestrator.tuple_store_path`), grouped to
       per-(instrument, TF, mode) top-K leaderboards in deterministic order
       (:func:`~brutex.engine.stage3_select.group_survivors`).
    4. Slice every loaded ``(instrument, tf)`` to its lockbox-excluded train
       window (:func:`train_slice_bars`) and hash the train slices through the
       shared :func:`data_snapshot_hash` (Amendment A1).
    5. Emit ``STAGE_3_PASS_START``; per survivor (leaderboard order): recover
       the start cell (review F4), rebuild ``bar_bits`` for its proximity over
       the TRAIN bars (review F9, memoized per (instrument, tf, proximity)),
       run the slice-4 :func:`~brutex.engine.stage3_refine.refine_mask`, and
       project EVERY unique evaluation into a refined
       :class:`~brutex.engine.results_store.ResultRow` stamped with the train
       hash.
    6. Write each refined cell's rows to its OWN Parquet file (review F3) in
       deterministic cell order via the load-or-create store (idempotent). A
       cell whose PRE-EXISTING store's VIX trio conflicts with this pass's
       re-derivation (the India-VIX lake moved between launches —
       :class:`~brutex.engine.results_store.VixTrioConflictError`) is SKIPPED
       + counted (``cells_conflicted``) + WARNed with the quarantine remedy,
       its on-disk store/sidecar untouched — the stage-3 arm of the #904
       skip-and-continue contract, never a whole-launch abort.
    7. Emit ``STAGE_3_PASS_COMPLETE`` with the full tally.

    Parameters
    ----------
    grid
        The validated grid that just swept (its ``stage3_*`` knobs drive the
        pass).
    results_root
        The sweep results root the grid persisted under.
    bars_for
        The launcher's in-memory ``(instrument, tf_label) -> bars`` slice memo
        — the SAME bars the sweep ran on (same-process only, review F9).
    audit
        The launcher's open hash-chained writer (single-threaded use).
    exchange, segment, start, end
        The data-scope labels the launcher hashed its full window with —
        reused verbatim for the train-slice hash derivation.
    roll_schedule_by_instrument
        ADR-0012 futures roll schedules keyed by instrument symbol
        (DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001) — threaded into the survivor-cell
        run_id re-derivation, the refined-row identities AND the train-slice
        ``bar_bits`` memo (the recovered survivor cells must re-derive against
        the SAME schedule the sweep built with). ``None`` (the default) is the
        byte-identical spot path.
    stamper_for
        Optional ZERO-ARG provider of the run's India-VIX stamper
        (DEC-VIX-STAMP-WIRE-001). The production launchers (``cmd_sweep_all``
        + the sweep-verify recount) pass ``lambda: reference_stamper`` over
        their ONE eagerly-built launch stamper — never a second lake read;
        the lazy :func:`~brutex.engine.reference_wire.vix_stamper_provider`
        closure also fits this seam but has no production consumer today
        (its docstring states the dormancy honestly). Invoked LAZILY, once,
        AFTER the ``stage3_enable`` fast path (a disabled pass never resolves
        the provider); the resolved stamper (or
        an honest ``None``) rides every refinement evaluation AND the
        persisted refined-row projection. ``None`` (default) = unstamped.
    rollup_paths_hook
        Optional ``/results`` rollup batch-merge seam
        (DEC-RESULTS-TOPK-ROLLUP-001 Hook A, stage-3 arm —
        ``RollupMergeHook.merge_paths``), fired ONCE per pass with the
        flushed cell-store paths after the cell loop completes (never per
        cell). FENCED INTERNALLY at BLOCKER-1 strength — never raises into
        the pass. ``None`` (the default) is byte-identical to the
        pre-rollup pass.

    Returns
    -------
    Stage3PassReport
        The tally + the refined-cell list (the cascade-extension input).

    Raises
    ------
    Stage3WireError
        Unavailable Stage-3 Rust runtime, an unrecoverable survivor cell, an unknown
        direction/timeframe label, or non-chronological bars.
    """
    if not grid.stage3_enable:
        return Stage3PassReport(
            enabled=False,
            train_snapshot_hash=None,
            leaderboards=0,
            survivors_total=0,
            masks_refined=0,
            evals_total=0,
            cache_hits_total=0,
            rows_appended=0,
            skipped_empty_train=0,
            cells=(),
        )
    ensure_stage3_runtime()
    # LAZY stamper resolution (DEC-VIX-STAMP-WIRE-001): resolved exactly once,
    # strictly AFTER the disabled fast path above — a stage3-disabled grid
    # never reads the VIX lake through this seam.
    reference_stamper: TradeReferenceStamper | None = None if stamper_for is None else stamper_for()

    grid_rows: list[ResultRow] = []
    for tuple_ in iter_sweep_tuples(grid):
        path = tuple_store_path(results_root, tuple_, grid)
        if path.exists():
            grid_rows.extend(ResultsStore.load(path).rows())
    leaderboards = group_survivors(grid_rows, top_k=grid.stage3_top_k)
    survivors_total = sum(len(rows) for rows in leaderboards.values())

    train_slices: dict[tuple[str, str], tuple[CandleBar, ...]] = {}
    for instrument in grid.instruments:
        for tf in grid.timeframes:
            train_slices[(instrument, tf.label)] = train_slice_bars(bars_for(instrument, tf.label))
    train_hash = data_snapshot_hash(
        exchange=exchange, segment=segment, start=start, end=end, slices=train_slices
    )
    # The refined rows' store context: SAME grid, TRAIN-slice snapshot identity
    # (Amendment A1) — so a refined cell file can never collide with a Stage-2
    # grid cell file even at identical (instrument, tf, direction, k, sl, tp).
    train_grid = dataclasses.replace(grid, data_snapshot_hash=train_hash)

    budget = RefineBudget(max_fevals=grid.stage3_max_fevals, max_restarts=grid.stage3_max_restarts)
    subject = f"stage3:{grid.data_snapshot_hash[:16]}"
    audit.append(
        action=STAGE3_ACTION_PASS_START,
        subject=subject,
        payload={
            "stage3_top_k": grid.stage3_top_k,
            "max_fevals": grid.stage3_max_fevals,
            "max_restarts": grid.stage3_max_restarts,
            "train_snapshot_hash": train_hash,
            "leaderboards": len(leaderboards),
            "survivors_total": survivors_total,
        },
    )
    sink = _WriterAuditSink(audit)

    masks_refined = 0
    evals_total = 0
    cache_hits_total = 0
    skipped_empty_train = 0
    cell_rows: dict[SweepTuple, list[ResultRow]] = {}
    cell_sharpes: dict[SweepTuple, dict[str, float | None]] = {}
    bits_memo: dict[
        tuple[
            str,
            str,
            ProximityParams | None,
            FibAnchorKind,
            PriceSourceCell,
            tuple[int, ...] | None,
        ],
        Sequence[int],
    ] = {}
    for key, survivors in leaderboards.items():
        train_bars = train_slices.get((key.instrument, key.tf), ())
        if not train_bars:
            skipped_empty_train += 1
            continue
        tf = _timeframe_for_label(key.tf, grid)
        roll_schedule = (
            roll_schedule_by_instrument.get(key.instrument)
            if roll_schedule_by_instrument is not None
            else None
        )
        for row in survivors:
            side = _side_for_direction(row.direction)
            start_sl, start_tp, proximity, fib_anchor, price_source = recover_survivor_cell(
                row, grid, roll_schedule=roll_schedule
            )
            # The ``bar_bits`` matrix is Fib-anchor-specific (DEC-NIFTY-FIB-AXIS-001),
            # price-source-specific (DEC-NIFTY-PRICE-SOURCE-AXIS-001: the swept
            # VWAP/EMA source moves bits 52-53 / 0-5) AND roll-schedule-specific
            # (DEC-STAGE1A-ROLL-RESET-001), so the memo key + the build carry ALL
            # THREE — a survivor is refined against ITS OWN Fib levels + ITS OWN
            # source bits + ITS OWN roll boundaries, never stale ones.
            memo_key = (key.instrument, key.tf, proximity, fib_anchor, price_source, roll_schedule)
            bar_bits = bits_memo.get(memo_key)
            if bar_bits is None:
                bar_bits = build_bar_bits(
                    train_bars,
                    proximity=proximity,
                    prior_n=grid.prior_n,
                    fib_anchor=fib_anchor,
                    vwap_source=price_source.vwap_source,
                    ema_source=price_source.ema_source,
                    roll_schedule=roll_schedule,
                    apply_1day_mask=apply_1day_mask_for(key.instrument, key.tf),
                )
                bits_memo[memo_key] = bar_bits
            report = refine_mask(
                mask_run_id=row.run_id,
                start_sl_paisa=start_sl,
                start_tp_paisa=start_tp,
                evaluate=_make_evaluator(
                    bars=train_bars,
                    bar_bits=bar_bits,
                    mask=row.mask_bits,
                    side=side,
                    tf_minutes=tf.minutes,
                    start_equity_paisa=grid.start_equity_paisa,
                    reference_stamper=reference_stamper,
                ),
                budget=budget,
                audit=sink,
            )
            masks_refined += 1
            evals_total += report.evals_total
            cache_hits_total += report.cache_hits
            for refined_eval in report.evals:
                refined_id = mask_run_id(
                    mask=row.mask_bits,
                    direction=row.direction,
                    instrument=row.instrument,
                    tf=row.tf,
                    mode=row.mode,
                    params=canonical_trade_params(
                        sl_paisa=refined_eval.sl_paisa,
                        tp_paisa=refined_eval.tp_paisa,
                        prior_n=grid.prior_n,
                        proximity=proximity,
                        start_equity_paisa=grid.start_equity_paisa,
                        fib_anchor=fib_anchor,
                        vwap_source=price_source.vwap_source,
                        ema_source=price_source.ema_source,
                        roll_schedule=roll_schedule,
                    ),
                    data_snapshot_hash=train_hash,
                    indicator_version=grid.indicator_version,
                    code_commit_sha=grid.code_commit_sha,
                )
                refined_row = ranked_combo_to_result_row(
                    # An UNGATED Bailey-LdP trial: composite_score is the
                    # pool-relative ranking term and is NOT defined per-eval
                    # (ADR-0007 condition 2) — 0.0 placeholder, never ranked on.
                    RankedCombo(
                        mask=row.mask_bits,
                        metrics=refined_eval.metrics,
                        composite_score=0.0,
                        deflated_sharpe=None,
                    ),
                    run_id=refined_id,
                    instrument=row.instrument,
                    tf=row.tf,
                    direction=row.direction,
                    mode=row.mode,
                    bars=train_bars,
                    bar_bits=bar_bits,
                    tf_minutes=tf.minutes,
                    sl_paisa=refined_eval.sl_paisa,
                    tp_paisa=refined_eval.tp_paisa,
                    # The OBSERVABLE bind (DEC-VIX-STAMP-WIRE-001): the refined
                    # row's re-simulation feeds the PERSISTED cell store.
                    reference_stamper=reference_stamper,
                )
                cell = SweepTuple(
                    instrument=row.instrument,
                    tf=tf,
                    direction=side,
                    k=popcount(row.mask_bits),
                    sl_paisa=refined_eval.sl_paisa,
                    tp_paisa=refined_eval.tp_paisa,
                    proximity=proximity,
                    fib_anchor=fib_anchor,
                    price_source=price_source,
                )
                cell_rows.setdefault(cell, []).append(refined_row)
                # Raw float|None Sharpe keyed by refined_id — the V-moment
                # source, deduped by identity (ADR-0008 condition 4: never the
                # row's None→0.0 projection, never evals_total).
                cell_sharpes.setdefault(cell, {})[refined_id] = refined_eval.metrics.sharpe_ratio

    rows_appended = 0
    sidecars_written = 0
    cells_conflicted = 0
    cells: list[Stage3RefinedCell] = []
    for cell in sorted(cell_rows, key=_cell_sort_key):
        cell_path = tuple_store_path(results_root, cell, train_grid)
        cell_appended = 0
        try:
            # The LOAD sits inside the same try as the appends (round-4 panel
            # consistency fold): ``ResultsStore.load`` re-appends each stored
            # record, so a cell parquet holding duplicate-run_id rows that
            # differ ONLY in non-None VIX values (tamper-grade — the dict-keyed
            # flush can never write one) raises ``VixTrioConflictError`` at
            # LOAD time; the grid drivers tuple-skip the same file, so the
            # pass must cell-skip it too, never abort the launch.
            store = load_or_create_store(cell_path)
            for refined_row in cell_rows[cell]:
                # Identical re-derived rows (re-runs; the same refined cell reached
                # from two survivors of the same mask) are idempotent no-ops.
                if store.append(refined_row):
                    cell_appended += 1
            cell_flush = store.flush()
        except VixTrioConflictError as exc:
            # The India-VIX lake moved between two launches and this cell's
            # PRE-EXISTING store carries the old trio (DEC-VIX-STAMP-WIRE-001
            # round-3 panel MAJOR: the pass re-appends into existing cell
            # stores on EVERY resume — the dominant redo path for this
            # reference-only conflict, which the grid drivers' #904 tuple-skip
            # never sees because sentinel-resumed tuples skip their appends).
            # Mirror the grid contract: skip + count THIS ONE cell — the flush
            # is skipped so its on-disk store + sidecar stay untouched, the
            # cell is excluded from the cascade-spec list, and the launch
            # verdict goes non-zero via ``cells_conflicted`` — never a
            # whole-launch abort over a reference-only annotation.
            cells_conflicted += 1
            _LOG.warning(
                "stage3_wire.cell_vix_conflict",
                cell_path=str(cell_path),
                error=str(exc),
                remediation=(
                    "quarantine/delete this cell's results store so a redo "
                    "re-persists it against the current VIX series, or re-run "
                    "after the VIX lake settles"
                ),
            )
            continue
        rows_appended += cell_appended
        # Per-cell trial-stats sidecar (DEC-TRIAL-STATS-WRITE-001): the pass
        # re-derives every cell deterministically, so recompute-and-overwrite
        # is the idempotent policy HERE (the grid tuples' preserve-on-skips
        # rule guards partial resumes; a Stage-3 pass has none). Written ONLY
        # when this pass derived EVERY row in the store — a count mismatch
        # means the store holds rows from a differently-configured earlier run
        # and a sidecar from this pass alone would UNDER-COUNT N: skip loudly;
        # the stale/absent sidecar then fails the condition-5 pairing check at
        # cascade-build time instead of fabricating a partial N.
        derived = cell_sharpes[cell]
        if len(store) == len(derived):
            write_tuple_trial_stats(
                trial_stats_sidecar_path(cell_path),
                tuple_trial_stats_from_sharpes(
                    [derived[refined_id] for refined_id in sorted(derived)],
                    n_enumerated=len(derived),
                    n_conflict_pruned=0,
                    n_minhits_dropped=0,
                    tuple_digest=_tuple_digest(cell, train_grid),
                    store_content_hash=cell_flush.content_hash,
                ),
            )
            sidecars_written += 1
        else:
            _LOG.warning(
                "stage3_wire.cell_sidecar_skipped",
                cell_path=str(cell_path),
                store_rows=len(store),
                derived_rows=len(derived),
            )
        cells.append(Stage3RefinedCell(tuple_=cell, path=cell_path, rows=len(store)))

    if rollup_paths_hook is not None and cells:
        # Hook A, stage-3 arm (DEC-RESULTS-TOPK-ROLLUP-001): ONE rollup merge
        # per PASS over the flushed cell stores (never per cell — the doc's
        # one-merge-per-pass law). A VIX-conflicted cell above was NOT flushed
        # and is deliberately absent from `cells`. The hook is fenced
        # internally (BLOCKER-1) — it never raises into the pass.
        rollup_paths_hook([cell.path for cell in cells])

    payload = {
        "masks_refined": masks_refined,
        "evals_total": evals_total,
        "cache_hits_total": cache_hits_total,
        "cells_written": len(cells),
        "rows_appended": rows_appended,
        "sidecars_written": sidecars_written,
        "cells_conflicted": cells_conflicted,
        "skipped_empty_train": skipped_empty_train,
        "train_snapshot_hash": train_hash,
    }
    audit.append(action=STAGE3_ACTION_PASS_COMPLETE, subject=subject, payload=payload)
    _LOG.info("stage3_wire.pass_complete", subject=subject, **payload)
    return Stage3PassReport(
        enabled=True,
        train_snapshot_hash=train_hash,
        leaderboards=len(leaderboards),
        survivors_total=survivors_total,
        masks_refined=masks_refined,
        evals_total=evals_total,
        cache_hits_total=cache_hits_total,
        rows_appended=rows_appended,
        skipped_empty_train=skipped_empty_train,
        cells=tuple(cells),
        sidecars_written=sidecars_written,
        cells_conflicted=cells_conflicted,
    )


def cascade_specs_for_refined_cells(
    cells: Sequence[Stage3RefinedCell], *, grid: SweepGridConfig
) -> list[CascadeTupleSpec]:
    """Build the Stage-4 cascade specs for every refined cell (reviews F3 + F8).

    Each refined cell becomes its own
    :class:`~brutex.engine.cascade_wire.CascadeTupleSpec` carrying EVERY
    refined-eval row UNTRIMMED (``top_k=None``): each evaluation is a
    Bailey-LdP trial (ADR-0007 condition 6) and none may be dropped before the
    cascade counts its N. The cascade then re-simulates each candidate at its
    own quantized coordinates over the cascade's bars, exactly as Amendment A1
    describes.

    Stage-3 search FAMILY (DEC-CASCADE-STAT-TIER-WIRE-001): per condition 6
    EVERY stage-3 eval is persisted as a ResultRow — the same mask lands in
    one refined-cell store per DISTINCT quantized ``(sl_paisa, tp_paisa)`` it
    was tried at, so a mask's search family is recoverable HERE (the one
    place the cells + their rows are all in hand) as the set of cell
    coordinates whose store holds a row with that mask, per
    ``(instrument, tf, direction)`` group. Each cell's spec carries
    ``family_sl_tp`` = its winner masks' COMMON family (sorted; the cell's
    own pair is a member by construction — the cell contributed itself).
    HONEST LIMIT (spec-level field, recorded deviation): a cell whose rows
    span MULTIPLE masks with DIFFERING family sets cannot be expressed by
    ONE spec-level family — it is skipped LOUDLY
    (``stage3_wire.family_ambiguous_cell``; the per-candidate PBO abstains
    for that cell, never a fabricated family).

    Parameters
    ----------
    cells
        The pass report's refined cells (already in deterministic order).
    grid
        The owning grid (its shared ``prior_n`` / ``start_equity_paisa``).

    Returns
    -------
    list[CascadeTupleSpec]
        One spec per non-empty refined cell.
    """
    # ONE load per cell store (reused for the family map AND the spec rows).
    loaded: list[tuple[Stage3RefinedCell, tuple[ResultRow, ...]]] = [
        (cell, tuple(ResultsStore.load(cell.path).rows())) for cell in cells
    ]
    # (instrument, tf_label, direction_label) -> mask -> {(sl, tp), ...}
    family_by_mask: dict[tuple[str, str, str], dict[int, set[tuple[int, int]]]] = {}
    for cell, rows in loaded:
        tuple_ = cell.tuple_
        if tuple_.sl_paisa is None or tuple_.tp_paisa is None:
            # Structurally unreachable for refined cells ("refined cells never
            # carry a disabled leg" — Stage3CellRecord's > 0 law); defensive
            # totality so a hand-built cell can never poison the map.
            continue
        key = (tuple_.instrument, tuple_.tf.label, _direction_label(tuple_.direction))
        per_mask = family_by_mask.setdefault(key, {})
        for row in rows:
            per_mask.setdefault(row.mask_bits, set()).add((tuple_.sl_paisa, tuple_.tp_paisa))

    specs: list[CascadeTupleSpec] = []
    for cell, rows in loaded:
        tuple_ = cell.tuple_
        proximity = tuple_.proximity
        proximity_bps = (
            None
            if proximity is None
            else (
                proximity.pivot_proximity_bps,
                proximity.pdh_pdl_proximity_bps,
                proximity.fib_proximity_bps,
            )
        )
        family_sl_tp: tuple[tuple[int, int], ...] | None = None
        if rows and tuple_.sl_paisa is not None and tuple_.tp_paisa is not None:
            key = (tuple_.instrument, tuple_.tf.label, _direction_label(tuple_.direction))
            per_mask = family_by_mask[key]
            families = {frozenset(per_mask[row.mask_bits]) for row in rows}
            if len(families) == 1:
                family_sl_tp = tuple(sorted(next(iter(families))))
            else:
                _LOG.warning(
                    "stage3_wire.family_ambiguous_cell",
                    cell_path=str(cell.path),
                    n_masks=len({row.mask_bits for row in rows}),
                    n_distinct_families=len(families),
                    detail=(
                        "the cell's winner masks carry differing search families — "
                        "the spec-level family_sl_tp cannot express them; the "
                        "per-candidate PBO is skipped for this cell"
                    ),
                )
        spec = spec_from_store_rows(
            instrument=tuple_.instrument,
            tf_label=tuple_.tf.label,
            tf_minutes=tuple_.tf.minutes,
            direction_label=_direction_label(tuple_.direction),
            sl_paisa=tuple_.sl_paisa,
            tp_paisa=tuple_.tp_paisa,
            prior_n=grid.prior_n,
            proximity_bps=proximity_bps,
            start_equity_paisa=grid.start_equity_paisa,
            rows=rows,
            top_k=None,
            fib_anchor=tuple_.fib_anchor,
            vwap_source=tuple_.price_source.vwap_source,
            ema_source=tuple_.price_source.ema_source,
            family_sl_tp=family_sl_tp,
        )
        if spec is not None:
            specs.append(spec)
    return specs


def stage3_manifest_record(
    report: Stage3PassReport, *, results_root: Path | None = None
) -> Stage3ManifestRecord:
    """Project a pass report into the cascade manifest's Stage-3 record (review F3).

    Records enablement, the train-slice snapshot hash, and the refined-cell
    list — so a manifest reader can tell exactly which specs are ungated
    Stage-3 refined cells versus gated Stage-2 grid cells. With *results_root*
    supplied, each cell record additionally carries its store path RELATIVE to
    that root (DEC-ROW-PROVENANCE-001) — the machine-readable attribution the
    read-back uses to label refined rows by source file; ``None`` (legacy
    callers) omits it and readers fall back to the blanket disclaimer.

    Parameters
    ----------
    report
        The :func:`run_stage3_pass` report (enabled or the disabled no-op).
    results_root
        The sweep results root the cells were written under (the launcher
        passes it; every cell path must live beneath it).

    Returns
    -------
    Stage3ManifestRecord
        The manifest record (``enabled=False`` ⇒ no hash, no cells).

    Raises
    ------
    Stage3WireError
        If an enabled report carries a refined cell with a disabled (``None``)
        SL/TP leg — refined cells are always quantized on-grid ints — or a
        cell path outside *results_root*.
    """
    if not report.enabled:
        return Stage3ManifestRecord(enabled=False, train_snapshot_hash=None, refined_cells=())
    records: list[Stage3CellRecord] = []
    for cell in report.cells:
        tuple_ = cell.tuple_
        if tuple_.sl_paisa is None or tuple_.tp_paisa is None:
            raise Stage3WireError(
                f"stage3_manifest_record: refined cell {tuple_.instrument}/{tuple_.tf.label} "
                f"has a disabled leg (sl={tuple_.sl_paisa}, tp={tuple_.tp_paisa})"
            )
        rel_path: str | None = None
        if results_root is not None:
            try:
                rel_path = cell.path.relative_to(results_root).as_posix()
            except ValueError as exc:
                raise Stage3WireError(
                    f"stage3_manifest_record: cell store {cell.path} is not under "
                    f"the results root {results_root}"
                ) from exc
        proximity = tuple_.proximity
        records.append(
            Stage3CellRecord(
                instrument=tuple_.instrument,
                tf_label=tuple_.tf.label,
                direction_label=_direction_label(tuple_.direction),
                k=tuple_.k,
                sl_paisa=tuple_.sl_paisa,
                tp_paisa=tuple_.tp_paisa,
                proximity_bps=(
                    None
                    if proximity is None
                    else (
                        proximity.pivot_proximity_bps,
                        proximity.pdh_pdl_proximity_bps,
                        proximity.fib_proximity_bps,
                    )
                ),
                rel_path=rel_path,
                fib_anchor=tuple_.fib_anchor,
                vwap_source=tuple_.price_source.vwap_source,
                ema_source=tuple_.price_source.ema_source,
            )
        )
    return Stage3ManifestRecord(
        enabled=True,
        train_snapshot_hash=report.train_snapshot_hash,
        refined_cells=tuple(records),
    )


__all__ = [
    "STAGE3_ACTION_PASS_COMPLETE",
    "STAGE3_ACTION_PASS_START",
    "Stage3PassReport",
    "Stage3RefinedCell",
    "Stage3WireError",
    "cascade_specs_for_refined_cells",
    "data_snapshot_hash",
    "ensure_stage3_runtime",
    "recover_survivor_cell",
    "run_stage3_pass",
    "stage3_manifest_record",
    "train_slice_bars",
]
