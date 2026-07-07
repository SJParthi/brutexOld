"""Live sweep trials/GB/hours ESTIMATOR + fail-CLOSED results-disk decision
(DEC-SWEEP-PREFLIGHT-ESTIMATOR-001, BUILD-SPEC §2.2/§5/§7).

Pure arithmetic over the RAW sweep selection axes — ZERO I/O: the calibration
snapshot and the results-volume free bytes are INJECTED by the caller (the
composed gate / the dashboard route / the CLI shim). The math is the exact
twin of the shipped oracles:

* ``estimate_tuples`` == :func:`brutex.engine.sweep_orchestrator.count_sweep_tuples`
  (the 8-axis product) and ``estimate_trials`` ==
  :func:`brutex.engine.sweep_orchestrator.bloom_capacity_hint`
  (``Σ_k C(n, k) × the other-7-axis product``) — parity-PINNED bit-for-bit by
  the property suite, licensing ZERO ``sweep_orchestrator.py`` changes.
* The time fallback is the SLA guard's exact per-tuple arithmetic
  (:func:`brutex.engine.sweep_sla_guard.resolve_per_tuple_seconds` — env-aware,
  fail-loud parse), so the confirm gate and the SLA refusal can never quote two
  different hour figures (BUILD-SPEC D9).

## Honest envelope (Golden Rules #1/#5/#10)

* **O(axes)** pure arithmetic — O(1) w.r.t. lake depth, stored winners and bar
  counts (bench-pinned in ``tests/bench/test_sweep_estimate_o1.py``); NOT
  O(1) in the number of distinct axis VALUES (each k costs one ``math.comb``).
* Trials ``X`` is an UPPER bound on ENUMERATED masks — conflict pruning,
  Stage-1B min-hits and dedup skips reduce the live count; a re-run of the same
  selection can drop to ~0 recomputed (the ``bloom_capacity_hint`` honesty
  clause, reproduced verbatim on :func:`estimate_trials`).
* The byte constants (:data:`FALLBACK_RESULT_BYTES_PER_ROW` /
  :data:`TRIAL_SIDECAR_BYTES` / :data:`AUDIT_BYTES_PER_TUPLE`) are **UNMEASURED,
  conservative projections** in the ``BATCH_RANK_BYTES_PER_KEPT`` house style
  (``sweep_mem_budget.py``): named, documented, superseded by the calibration
  store's observed rates the moment one qualifying run lands. Every payload
  names its model (``time_model`` / ``bytes_model``).
* :func:`check_results_disk` is deliberately **fail-CLOSED** — a documented
  divergence from the prebuild guard's fail-open (``make_disk_guard``): the
  prebuild has the ``LakeWriteHalt`` backstop, while the sweep's results store
  rides the guard-FREE ``atomic_write_simple`` write path, so this gate is the
  ONLY ENOSPC wall. **NO env kill switch, NO escape flag exists** (BUILD-SPEC
  excision #2).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from math import isfinite, prod
from typing import TYPE_CHECKING, Final

import blake3
import structlog

from brutex.engine.combo_mask import count_combos
from brutex.engine.sweep_sla_guard import ENV_PER_TUPLE_SECONDS, resolve_per_tuple_seconds
from brutex.vocabulary.bits import IMPLEMENTED_MASK, TOTAL_CONDITIONS

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import date
    from pathlib import Path

    from brutex.engine.sweep_calibration import CalibrationSnapshot

logger = structlog.get_logger("brutex.engine.sweep_estimate")

#: The preflight payload/token schema version — the P2-P6 machine contract.
ESTIMATE_SCHEMA_VERSION: Final[int] = 1

#: UNMEASURED, conservative bytes-per-persisted-row projection (the
#: ``BATCH_RANK_BYTES_PER_KEPT`` house style, ``sweep_mem_budget.py``): one
#: results-store row spans 25 zstd-3 on-disk columns (``results_store.py``
#: ``_render_bytes``) but NO measured bytes/row constant exists in the repo —
#: this figure is a NAMED projection, superseded by the calibration store's
#: observed median the moment one qualifying run lands (``bytes_model`` names
#: the arm on every payload).
FALLBACK_RESULT_BYTES_PER_ROW: Final[int] = 1_024

#: UNMEASURED per-tuple ``<store>.trials.json`` sidecar projection — the
#: sidecar is "a few hundred bytes per tuple" (``trial_stats.py``); 512 B is
#: the conservative round-up, labelled a projection like its siblings.
TRIAL_SIDECAR_BYTES: Final[int] = 512

#: UNMEASURED per-tuple hash-chained audit JSONL projection (~3 chain lines
#: per tuple across the sweep-all + supervisor logs) — a NAMED projection.
AUDIT_BYTES_PER_TUPLE: Final[int] = 1_536

#: O-f: the default sweep depth ceiling — any ``k`` PAST this WARNS before
#: compute (R20) but NEVER blocks; the SLA verdict is the time wall (the
#: consent axes stay separate, BUILD-SPEC excision #7).
ABSURD_K_MAX: Final[int] = 3

#: The 10% results-volume safety margin (the ``lake_footprint``
#: ``DEFAULT_SAFETY_MARGIN`` precedent) folded into ``need_bytes``.
SWEEP_DISK_SAFETY_MARGIN: Final[Decimal] = Decimal("0.10")


def default_condition_bits() -> tuple[int, ...]:
    """Return the DEFAULT swept condition-bit set — every IMPLEMENTED bit.

    The ONE source of truth for "which conditions does a sweep test when
    ``--conditions`` is not given" (CLAUDE.md §11 COMMON), MOVED here from
    ``brutex/cli.py`` (the cli keeps a delegating wrapper) so the engine, the
    dashboard and the CLI all reach the same derivation. It is derived from
    the vocabulary's :data:`~brutex.vocabulary.bits.IMPLEMENTED_MASK` — the
    bitmask of conditions whose evaluator computes a real boolean — so the
    default AUTOMATICALLY tracks the vocabulary and can never drift behind it
    again (the pre-DEC-SWEEP-DEFAULT-CONDITIONS-60-001 bug where the default
    was hardcoded to ``range(52)`` while the vocabulary had grown to 60).

    Deriving from ``IMPLEMENTED_MASK`` (rather than ``range(TOTAL_CONDITIONS)``)
    is deliberately HONEST: a future LAYOUT-ONLY bit (``implemented=false`` in
    ``conditions.toml``) is EXCLUDED, so the default never sweeps a dead
    condition that could never fire. Today every one of the
    ``TOTAL_CONDITIONS`` bits is implemented, so this returns ``(0, ..., 59)``.

    Returns
    -------
    tuple[int, ...]
        The implemented condition-bit indices in ascending order.
    """
    return tuple(bit for bit in range(TOTAL_CONDITIONS) if IMPLEMENTED_MASK & (1 << bit))


def _dedup_axis[T](values: Sequence[T], *, axis: str) -> tuple[T, ...]:
    """Order-preserving dedup of one selection axis; empty ⇒ :class:`ValueError`.

    Mirrors ``SweepGridConfig``'s construction-time ``_dedup_seq``
    (``dict.fromkeys``) so the raw-axes numbers equal the grid oracles
    bit-for-bit; an empty axis is a clean user error, never a traceback.
    """
    deduped = tuple(dict.fromkeys(values))
    if not deduped:
        raise ValueError(f"sweep selection axis {axis!r} must be non-empty")
    return deduped


@dataclass(frozen=True, slots=True)
class SweepSelection:
    """Raw pre-grid axes, deduped first-seen-order (mirrors ``SweepGridConfig._dedup_seq``).

    Attributes
    ----------
    symbols, intervals, directions, k_values, stop_losses, take_profits,
    proximities, fib_anchors
        The EIGHT swept axes — ``proximities`` is ``(None,)`` from both
        production surfaces today but is NEVER dropped (the 8th axis; the
        WORK-MAP trap).
    n_conditions
        ``len(set(condition_bits))`` — 60 via :func:`default_condition_bits`.
    top_k
        Persisted-rows-per-tuple cap (the ``--top`` knob; ``>= 1``).
    start, end
        The (clamped) sweep date range — carried for labels/token ONLY, never
        part of the trial math (the grid carries no dates either).
    """

    symbols: tuple[str, ...]
    intervals: tuple[str, ...]
    directions: tuple[str, ...]
    k_values: tuple[int, ...]
    stop_losses: tuple[int | None, ...]
    take_profits: tuple[int | None, ...]
    proximities: tuple[float | None, ...]
    fib_anchors: tuple[str, ...]
    n_conditions: int
    top_k: int
    start: date
    end: date


def selection_from_raw(
    *,
    symbols: Sequence[str],
    intervals: Sequence[str],
    directions: Sequence[str],
    k_values: Sequence[int],
    stop_losses: Sequence[int | None],
    take_profits: Sequence[int | None],
    fib_anchors: Sequence[str],
    proximities: Sequence[float | None] = (None,),
    condition_bits: Sequence[int] | None = None,
    top_k: int,
    start: date,
    end: date,
) -> SweepSelection:
    """Build a :class:`SweepSelection` from the RAW pre-I/O axes.

    Every axis is deduped first-seen-order via ``dict.fromkeys`` — the exact
    mirror of ``SweepGridConfig``'s construction-time dedup, so
    :func:`estimate_trials` / :func:`estimate_tuples` equal
    ``bloom_capacity_hint`` / ``count_sweep_tuples`` bit-for-bit on the same
    inputs (parity-pinned).

    Parameters
    ----------
    symbols, intervals, directions, k_values, stop_losses, take_profits,
    fib_anchors, proximities
        The eight swept axes (raw, possibly duplicated).
    condition_bits
        The swept condition-bit set; ``None`` ⇒ :func:`default_condition_bits`.
    top_k
        Persisted-rows-per-tuple cap; must be ``>= 1``.
    start, end
        The (clamped) date range — labels/token only.

    Raises
    ------
    ValueError
        On an empty axis, an empty *condition_bits*, ``top_k < 1``, or a ``k``
        outside ``[0, n_conditions]`` — a bad selection is a clean 400/user
        error, never a traceback (mirrors the grid validation).
    """
    bits = default_condition_bits() if condition_bits is None else tuple(condition_bits)
    n_conditions = len(set(bits))
    if n_conditions == 0:
        raise ValueError("sweep selection condition_bits must be non-empty")
    if top_k < 1:
        raise ValueError(f"sweep selection top_k must be >= 1; got {top_k!r}")
    deduped_k = _dedup_axis(k_values, axis="k_values")
    for k in deduped_k:
        if k < 0 or k > n_conditions:
            raise ValueError(
                f"sweep selection k={k} out of range [0, {n_conditions}] "
                f"(n_conditions = {n_conditions})"
            )
    return SweepSelection(
        symbols=_dedup_axis(symbols, axis="symbols"),
        intervals=_dedup_axis(intervals, axis="intervals"),
        directions=_dedup_axis(directions, axis="directions"),
        k_values=deduped_k,
        stop_losses=_dedup_axis(stop_losses, axis="stop_losses"),
        take_profits=_dedup_axis(take_profits, axis="take_profits"),
        proximities=_dedup_axis(proximities, axis="proximities"),
        fib_anchors=_dedup_axis(fib_anchors, axis="fib_anchors"),
        n_conditions=n_conditions,
        top_k=top_k,
        start=start,
        end=end,
    )


def estimate_tuples(sel: SweepSelection) -> int:
    """Return the selection's TUPLE count — the ``count_sweep_tuples`` twin.

    The product of the EIGHT axis cardinalities (parity-PINNED bit-for-bit
    against :func:`brutex.engine.sweep_orchestrator.count_sweep_tuples` over
    real grids — BUILD-SPEC §10.3). A TUPLE is the SLA/resume/checkpoint unit,
    NOT a trial (``sweep_sla_guard`` doctrine); both are always emitted.
    """
    return prod(
        (
            len(sel.symbols),
            len(sel.intervals),
            len(sel.directions),
            len(sel.k_values),
            len(sel.stop_losses),
            len(sel.take_profits),
            len(sel.proximities),
            len(sel.fib_anchors),
        )
    )


def estimate_trials(sel: SweepSelection) -> int:
    """Return the selection's TRIAL count — the ``bloom_capacity_hint`` twin.

    ``(Σ_{k ∈ k_values} C(n_conditions, k)) × the other-7-axis product``,
    with :func:`brutex.engine.combo_mask.count_combos` (integer-exact
    ``math.comb`` + the u128 vocabulary ceiling guard) doing the mask math —
    parity-PINNED bit-for-bit against
    :func:`brutex.engine.sweep_orchestrator.bloom_capacity_hint`.

    Honest envelope (verbatim from the oracle, Golden Rule #10): this is an
    UPPER bound on *enumerated* masks, not a promise the sweep evaluates them
    all — conflict pruning, Stage-1B min-hits and dedup skips reduce the live
    count, and a re-run of the same selection can drop to ~0 recomputed.
    """
    masks = sum(count_combos(k, sel.n_conditions) for k in sel.k_values)
    other_axes = prod(
        (
            len(sel.symbols),
            len(sel.intervals),
            len(sel.directions),
            len(sel.stop_losses),
            len(sel.take_profits),
            len(sel.proximities),
            len(sel.fib_anchors),
        )
    )
    return masks * other_axes


@dataclass(frozen=True, slots=True)
class EstimateWarning:
    """One computed estimator warning (WARN-only; never a block).

    Attributes
    ----------
    code
        Machine-stable warning code — ``"absurd_depth"`` is the only P1 code
        (StrEnum-ready for P2).
    text
        The computed operator sentence (golden-pinned shape).
    """

    code: str
    text: str


@dataclass(frozen=True, slots=True)
class SweepEstimate:
    """One live selection estimate — trials / tuples / bytes / hours, model-named.

    Attributes
    ----------
    trials
        ``Σ_k C(n, k) ×`` the other-7-axis product (UPPER bound on enumerated
        masks — see :func:`estimate_trials`).
    tuples
        The 8-axis product (the SLA/resume unit; TUPLE ≠ TRIAL).
    masks
        ``Σ_k C(n_conditions, k)`` — masks per axis point.
    n_conditions
        The deduped condition-bit count the mask math used.
    rows_upper_bound
        ``Σ_k tuples_per_k × min(top_k, C(n, k))`` — the top-k-truncated
        persisted-row ceiling (results bytes are NOT proportional to trials).
    est_result_bytes
        ``store + sidecars + audit + dedup_map_bytes`` — the disk-gate NEED
        basis (before the safety margin).
    store_bytes, sidecar_bytes, audit_bytes
        The three projection terms, named separately.
    dedup_map_bytes
        Worst-case LMDB dedup-map reserve — FOLDED into the need (the map CAN
        grow to its cap) and NAMED separately on every display.
    est_seconds
        From the DISPLAYED time model (see *time_model*).
    est_seconds_fallback
        The per-tuple SLA arithmetic — ALWAYS emitted (BUILD-SPEC D9).
    time_model
        ``"calibrated_trials"`` | ``"per_tuple_sla_fallback"``.
    bytes_model
        ``"calibrated_bytes_per_row"`` | ``"projection_fallback"``.
    provisional
        ``True`` unless a QUALIFYING hardware-matched calibration served the
        time model (R23).
    calibration_samples, calibration_last_ts
        Calibration-ring provenance for the label (0 / ``None`` uncalibrated).
    absurd_k
        Any swept ``k > ABSURD_K_MAX`` (O-f) — WARN before compute, never a
        block (R20).
    warnings
        The computed :class:`EstimateWarning` list.
    notes
        Honesty notes (model provenance, top-k truncation, dedup reserve).
    """

    trials: int
    tuples: int
    masks: int
    n_conditions: int
    rows_upper_bound: int
    est_result_bytes: int
    store_bytes: int
    sidecar_bytes: int
    audit_bytes: int
    dedup_map_bytes: int
    est_seconds: float
    est_seconds_fallback: float
    time_model: str
    bytes_model: str
    provisional: bool
    calibration_samples: int
    calibration_last_ts: str | None
    absurd_k: bool
    warnings: tuple[EstimateWarning, ...]
    notes: tuple[str, ...]

    def headline(self) -> str:
        """The one-line confirm question (golden-pinned shape).

        ``"this selection = 72,676,800 trials, est. 50.4 h (PROVISIONAL —
        uncalibrated), ~0.14 GB results (+2.0 GiB dedup map reserve) —
        proceed?"`` — the results GB figure is the store+sidecar+audit
        projection; the dedup-map reserve is named separately (it is a sparse
        worst-case cap, not expected occupancy).
        """
        if self.provisional:
            label = "PROVISIONAL — uncalibrated"
        else:
            last = "unknown" if self.calibration_last_ts is None else self.calibration_last_ts[:10]
            label = f"calibrated from {self.calibration_samples} runs, last {last}"
        display_bytes = self.store_bytes + self.sidecar_bytes + self.audit_bytes
        dedup_part = ""
        if self.dedup_map_bytes > 0:
            dedup_part = f" (+{self.dedup_map_bytes / 2**30:.1f} GiB dedup map reserve)"
        return (
            f"this selection = {self.trials:,} trials, "
            f"est. {self.est_seconds / 3600.0:.1f} h ({label}), "
            f"~{display_bytes / 1e9:.2f} GB results{dedup_part} — proceed?"
        )

    def payload(self) -> dict[str, object]:
        """The JSON-safe estimate payload, :data:`ESTIMATE_SCHEMA_VERSION` stamped.

        Both time figures are always emitted (``est_hours`` +
        ``est_hours_fallback``, BUILD-SPEC D9) and both models are named.
        """
        return {
            "schema_version": ESTIMATE_SCHEMA_VERSION,
            "trials": self.trials,
            "tuples": self.tuples,
            "masks": self.masks,
            "n_conditions": self.n_conditions,
            "rows_upper_bound": self.rows_upper_bound,
            "est_result_bytes": self.est_result_bytes,
            "store_bytes": self.store_bytes,
            "sidecar_bytes": self.sidecar_bytes,
            "audit_bytes": self.audit_bytes,
            "dedup_map_bytes": self.dedup_map_bytes,
            "est_seconds": self.est_seconds,
            "est_hours": self.est_seconds / 3600.0,
            "est_seconds_fallback": self.est_seconds_fallback,
            "est_hours_fallback": self.est_seconds_fallback / 3600.0,
            "time_model": self.time_model,
            "bytes_model": self.bytes_model,
            "provisional": self.provisional,
            "calibration_samples": self.calibration_samples,
            "calibration_last_ts": self.calibration_last_ts,
            "absurd_k": self.absurd_k,
            "warnings": [{"code": w.code, "text": w.text} for w in self.warnings],
            "notes": list(self.notes),
            "headline": self.headline(),
        }


def estimate_selection(
    sel: SweepSelection,
    *,
    calibration: CalibrationSnapshot | None,
    per_tuple_seconds: float | None = None,
    dedup_map_bytes: int = 0,
    env: Mapping[str, str] | None = None,
) -> SweepEstimate:
    """Compute the live :class:`SweepEstimate` for *sel* — pure O(axes) arithmetic.

    The dual time model (BUILD-SPEC D9): ``time_model="calibrated_trials"``
    (``trials / trials_per_second``, ``provisional=False``) when a QUALIFYING
    hardware-matched calibration exists, else ``"per_tuple_sla_fallback"``
    (``tuples × per-tuple seconds`` — the SLA guard's exact arithmetic + env
    override, ``provisional=True``). Both figures are always emitted. The
    bytes model mirrors it (calibrated median bytes/row vs the UNMEASURED
    :data:`FALLBACK_RESULT_BYTES_PER_ROW` projection).

    Emits ONE ``sweep.estimate`` structlog INFO per call (observability; the
    composed gate additionally logs its own verdict).

    Parameters
    ----------
    sel
        The deduped selection (:func:`selection_from_raw`).
    calibration
        The injected calibration snapshot (``None`` = no store — PROVISIONAL).
    per_tuple_seconds
        Explicit fallback-model rate; ``None`` ⇒
        :func:`~brutex.engine.sweep_sla_guard.resolve_per_tuple_seconds`
        (env-aware, fail-LOUD parse).
    dedup_map_bytes
        The resolved LMDB dedup-map size (worst-case sparse reserve), injected
        by the caller (``cache_knobs.map_size_mb × 2**20``); ``>= 0``.
    env
        Environment mapping forwarded to the per-tuple resolver (tests).

    Raises
    ------
    ValueError
        On ``dedup_map_bytes < 0``, a non-finite/non-positive explicit
        *per_tuple_seconds*, or a malformed ``BRUTEX_SWEEP_PER_TUPLE_SECONDS``
        override (fail-loud — a broken knob is a clean user error).
    """
    if dedup_map_bytes < 0:
        raise ValueError(f"dedup_map_bytes must be >= 0; got {dedup_map_bytes!r}")
    if per_tuple_seconds is None:
        resolved_per_tuple = resolve_per_tuple_seconds(env)
    elif not isfinite(per_tuple_seconds) or per_tuple_seconds <= 0:
        raise ValueError(
            f"per_tuple_seconds must be a positive, finite number of seconds; "
            f"got {per_tuple_seconds!r}"
        )
    else:
        resolved_per_tuple = per_tuple_seconds

    tuples = estimate_tuples(sel)
    trials = estimate_trials(sel)
    masks = sum(count_combos(k, sel.n_conditions) for k in sel.k_values)
    # Integer by construction: tuples is the product of all 8 axis widths,
    # so it divides evenly by |k_values|.
    tuples_per_k = tuples // len(sel.k_values)
    rows_upper_bound = sum(
        tuples_per_k * min(sel.top_k, count_combos(k, sel.n_conditions)) for k in sel.k_values
    )

    cal_samples = 0 if calibration is None else calibration.samples
    cal_last_ts = None if calibration is None else calibration.last_run_ts

    if (
        calibration is not None
        and calibration.calibrated
        and calibration.bytes_per_row is not None
        and calibration.bytes_per_row > 0
    ):
        store_bytes = int(rows_upper_bound * calibration.bytes_per_row)
        bytes_model = "calibrated_bytes_per_row"
    else:
        store_bytes = rows_upper_bound * FALLBACK_RESULT_BYTES_PER_ROW
        bytes_model = "projection_fallback"
    sidecar_bytes = tuples * TRIAL_SIDECAR_BYTES
    audit_bytes = tuples * AUDIT_BYTES_PER_TUPLE
    est_result_bytes = store_bytes + sidecar_bytes + audit_bytes + dedup_map_bytes

    est_seconds_fallback = tuples * resolved_per_tuple
    if (
        calibration is not None
        and calibration.calibrated
        and calibration.trials_per_second is not None
        and calibration.trials_per_second > 0
    ):
        est_seconds = trials / calibration.trials_per_second
        time_model = "calibrated_trials"
        provisional = False
    else:
        est_seconds = est_seconds_fallback
        time_model = "per_tuple_sla_fallback"
        provisional = True

    absurd_ks = tuple(k for k in sel.k_values if k > ABSURD_K_MAX)
    absurd_k = bool(absurd_ks)
    warnings: tuple[EstimateWarning, ...] = ()
    if absurd_k:
        named = ", ".join(str(k) for k in absurd_ks)
        warnings = (
            EstimateWarning(
                code="absurd_depth",
                text=(
                    f"absurd depth: k value(s) {named} exceed the k≤{ABSURD_K_MAX} default "
                    f"(O-f) — Σ_k C({sel.n_conditions}, k) = {masks:,} masks per axis point; "
                    f"allowed with this WARNING, never a block (the SLA verdict is the time "
                    f"wall)"
                ),
            ),
        )

    notes: list[str] = []
    if provisional:
        notes.append(
            f"time estimate PROVISIONAL — uncalibrated (no qualifying measured run on this "
            f"hardware yet; serial {resolved_per_tuple:g} s/tuple placeholder, UNBENCHMARKED "
            f"— override via {ENV_PER_TUPLE_SECONDS})"
        )
    else:
        notes.append(
            f"time estimate calibrated from {cal_samples} runs, last {cal_last_ts} "
            f"(median trials/s over qualifying calibration-ring entries)"
        )
    if bytes_model == "projection_fallback":
        notes.append(
            f"results-bytes model: projection fallback ({FALLBACK_RESULT_BYTES_PER_ROW} B/row "
            f"+ {TRIAL_SIDECAR_BYTES} B/tuple sidecar + {AUDIT_BYTES_PER_TUPLE} B/tuple audit "
            f"— UNMEASURED projections until a qualifying run calibrates)"
        )
    else:
        notes.append(
            "results-bytes model: calibrated bytes/row (median over qualifying "
            "calibration-ring entries)"
        )
    notes.append(
        "results bytes are NOT proportional to trials (top-k truncation caps persisted "
        "rows per tuple)"
    )
    notes.append(
        "trials X is an UPPER bound on enumerated masks (conflict pruning, min-hits and "
        "dedup skips reduce the live count; a re-run of the same selection can drop to "
        "~0 recomputed)"
    )
    if dedup_map_bytes > 0:
        notes.append(
            f"incl. up to {dedup_map_bytes / 2**30:.1f} GiB dedup map reserve (worst-case "
            f"LMDB map size, folded into the disk need)"
        )

    estimate = SweepEstimate(
        trials=trials,
        tuples=tuples,
        masks=masks,
        n_conditions=sel.n_conditions,
        rows_upper_bound=rows_upper_bound,
        est_result_bytes=est_result_bytes,
        store_bytes=store_bytes,
        sidecar_bytes=sidecar_bytes,
        audit_bytes=audit_bytes,
        dedup_map_bytes=dedup_map_bytes,
        est_seconds=est_seconds,
        est_seconds_fallback=est_seconds_fallback,
        time_model=time_model,
        bytes_model=bytes_model,
        provisional=provisional,
        calibration_samples=cal_samples,
        calibration_last_ts=cal_last_ts,
        absurd_k=absurd_k,
        warnings=warnings,
        notes=tuple(notes),
    )
    logger.info(
        "sweep.estimate",
        trials=trials,
        tuples=tuples,
        masks=masks,
        est_seconds=est_seconds,
        est_result_bytes=est_result_bytes,
        time_model=time_model,
        bytes_model=bytes_model,
        provisional=provisional,
        absurd_k=absurd_k,
    )
    return estimate


@dataclass(frozen=True, slots=True)
class DiskPreflightDecision:
    """The fail-CLOSED results-volume disk verdict.

    Attributes
    ----------
    blocked
        ``True`` when the sweep must refuse (insufficient free bytes OR the
        probe failed — fail-CLOSED).
    free_bytes
        The injected free-byte figure; ``None`` = probe failed ⇒ blocked.
    need_bytes
        ``est_result_bytes × (1 + margin)`` (ceiling-rounded).
    margin
        The safety margin applied (:data:`SWEEP_DISK_SAFETY_MARGIN` default).
    probe_error
        Honest probe-failure clause when *free_bytes* is ``None``; else
        ``None``.
    volume
        ``str(out_root)`` — the results volume the verdict names.
    """

    blocked: bool
    free_bytes: int | None
    need_bytes: int
    margin: Decimal
    probe_error: str | None
    volume: str

    @property
    def ok(self) -> bool:
        """``True`` iff the sweep may proceed past the disk gate."""
        return not self.blocked

    def refusal(self) -> str:
        """The operator-facing refusal text — GiB numbers + the results volume path.

        Only meaningful on a blocked decision (the renderer calls it on
        refusal); both arms end with the remedy naming ``--results-root``.
        """
        need_gib = self.need_bytes / 2**30
        if self.free_bytes is None:
            return (
                f"DISK refusal (fail-CLOSED): could not probe free space on the results "
                f"volume {self.volume} ({self.probe_error}); need ~{need_gib:.2f} GiB "
                f"(estimate + {self.margin:.0%} margin). The sweep's results writes have "
                f"NO ENOSPC backstop, so an unknown volume is refused — no escape flag "
                f"exists. Free space on the results volume or point --results-root at a "
                f"larger volume."
            )
        free_gib = self.free_bytes / 2**30
        return (
            f"DISK refusal: {free_gib:.2f} GiB free < {need_gib:.2f} GiB needed "
            f"(estimate + {self.margin:.0%} margin) on the results volume {self.volume} — "
            f"free space on the results volume or point --results-root at a larger volume."
        )


def check_results_disk(
    estimate: SweepEstimate,
    *,
    out_root: Path,
    free_bytes: int | None,
    margin: Decimal = SWEEP_DISK_SAFETY_MARGIN,
) -> DiskPreflightDecision:
    """Decide the fail-CLOSED results-volume disk gate for *estimate*.

    FAIL-CLOSED (a deliberate, documented divergence from ``make_disk_guard``'s
    fail-open — the prebuild has the ``LakeWriteHalt`` backstop, while the
    sweep's ``results_store`` rides the guard-FREE ``atomic_write_simple``
    write path, so this gate is the ONLY ENOSPC wall): ``free_bytes is None``
    (the caller maps any probe ``OSError`` to ``None``) ⇒ ``blocked=True`` +
    *probe_error*; else ``blocked ⇔ free < need`` where ``need`` already
    carries the safety margin (ceiling-rounded, never truncated down). **NO
    env kill switch, NO escape flag** (BUILD-SPEC excision #2). Emits ONE
    ``sweep.estimate.disk`` structlog event (INFO ok / WARNING blocked).

    Parameters
    ----------
    estimate
        The live estimate whose ``est_result_bytes`` is the need basis.
    out_root
        The results volume root (``results_root or <lake>/sweeps``).
    free_bytes
        The injected ``volume_free_bytes(out_root)`` figure, or ``None`` when
        the probe failed (fail-CLOSED).
    margin
        The safety margin (default :data:`SWEEP_DISK_SAFETY_MARGIN`); must be
        a finite non-negative :class:`~decimal.Decimal`.

    Raises
    ------
    ValueError
        On a non-finite or negative *margin* (fail-loud config error).
    """
    if not margin.is_finite() or margin < 0:
        raise ValueError(f"margin must be a finite non-negative Decimal; got {margin!r}")
    need_bytes = int(
        (Decimal(estimate.est_result_bytes) * (Decimal(1) + margin)).to_integral_value(
            rounding=ROUND_CEILING
        )
    )
    if free_bytes is None:
        decision = DiskPreflightDecision(
            blocked=True,
            free_bytes=None,
            need_bytes=need_bytes,
            margin=margin,
            probe_error=(
                "free-space probe failed — the caller mapped the probe error to None; "
                "refusing fail-CLOSED"
            ),
            volume=str(out_root),
        )
    else:
        decision = DiskPreflightDecision(
            blocked=free_bytes < need_bytes,
            free_bytes=free_bytes,
            need_bytes=need_bytes,
            margin=margin,
            probe_error=None,
            volume=str(out_root),
        )
    log = logger.warning if decision.blocked else logger.info
    log(
        "sweep.estimate.disk",
        blocked=decision.blocked,
        free_bytes=decision.free_bytes,
        need_bytes=decision.need_bytes,
        margin=str(decision.margin),
        probe_error=decision.probe_error,
        volume=decision.volume,
    )
    return decision


def selection_confirm_token(
    sel: SweepSelection,
    *,
    launches: Sequence[tuple[str, str]],
    lake_token: str | None,
) -> str:
    """Mint the blake3 16-hex confirm token binding a proceed to the exact selection.

    The digest covers the canonical selection JSON (sorted keys), the clamped
    dates, the launch ``(exchange, segment)`` coordinates,
    :data:`ESTIMATE_SCHEMA_VERSION` and the injected *lake_token* — the
    dashboard passes ``lake_revision(lake_root)`` (the engine never imports
    ``brutex.dashboard``), so a pull finishing between estimate and click
    moves the token and forces a re-render.

    Honest gap (documented): out-of-band raw ``bars/`` writes that bypass the
    window ledger are invisible to ``lake_revision`` — the run-side belt (the
    gates re-run inside ``cmd_sweep_all`` + the data snapshot hash) is the
    backstop.

    Parameters
    ----------
    sel
        The deduped selection (with its CLAMPED dates).
    launches
        The resolved launch coordinates (D8 fan-out), in launch order.
    lake_token
        The caller-injected lake content token, or ``None`` (CLI — the
        sequential print-then-proceed consent path needs no token binding).
    """
    canonical: dict[str, object] = {
        "schema_version": ESTIMATE_SCHEMA_VERSION,
        "symbols": list(sel.symbols),
        "intervals": list(sel.intervals),
        "directions": list(sel.directions),
        "k_values": list(sel.k_values),
        "stop_losses": list(sel.stop_losses),
        "take_profits": list(sel.take_profits),
        "proximities": list(sel.proximities),
        "fib_anchors": list(sel.fib_anchors),
        "n_conditions": sel.n_conditions,
        "top_k": sel.top_k,
        "start": sel.start.isoformat(),
        "end": sel.end.isoformat(),
        "launches": [[exchange, segment] for exchange, segment in launches],
        "lake_token": lake_token,
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return blake3.blake3(blob.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "ABSURD_K_MAX",
    "AUDIT_BYTES_PER_TUPLE",
    "ESTIMATE_SCHEMA_VERSION",
    "FALLBACK_RESULT_BYTES_PER_ROW",
    "SWEEP_DISK_SAFETY_MARGIN",
    "TRIAL_SIDECAR_BYTES",
    "DiskPreflightDecision",
    "EstimateWarning",
    "SweepEstimate",
    "SweepSelection",
    "check_results_disk",
    "default_condition_bits",
    "estimate_selection",
    "estimate_trials",
    "estimate_tuples",
    "selection_confirm_token",
    "selection_from_raw",
]
