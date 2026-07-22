"""Stage-3 restarted-CMA-ES refinement runner (ADR-0007 §5 slice 4 —
DEC-STAGE3-REFINE-001, optimizer re-hosted by DEC-RUST-STAGE3-REFINE-001).

The deterministic refinement pass over the Stage-2 survivor set: for each
survivor mask (per leaderboard, in leaderboard order — ADR-0007 condition 1),
an inner CMA-ES searches the continuous (SL, TP) plane normalized to [0,1]²,
denormalized and **snapped to the int-paisa Mode-A grid BEFORE every
evaluation** (condition 3 — floats never reach the 9-field hash), each
evaluation being one full standalone backtest through the unchanged engine
path (the *evaluate* seam — production binds a
:func:`~brutex.engine.sweep_driver.metrics_for_mask` closure; tests bind a
synthetic objective).

The OPTIMIZER MATH lives in the ``brutex_refine`` Rust wheel
(``rust/brutex-refine`` — DEC-RUST-STAGE3-REFINE-001, DEFAULT-ON, the pycma
path DELETED): this module keeps ONLY the bookkeeping — run identity, seed
derivation, the value cache, the eval records, the audit lines. The wheel's
:class:`~brutex.engine.refine_rust.RefineSessionLike` proposes QUANTIZED
on-grid pairs (the snap runs inside the driver), so the evaluator can only
ever see ints, structurally.

Design (the nine ADR-0007 conditions this module owns):

* **Condition 4 — refinement-run identity + value cache.** The resumable /
  dedup unit is the WHOLE per-mask refinement run, identified by
  :func:`refinement_run_id` = ``blake3(mask_run_id ‖ optimizer-config ‖ seed ‖
  budget ‖ optimizer version + build digest)``. Per-eval results live in a
  run-scoped VALUE cache keyed by the quantized ``(sl_paisa, tp_paisa)`` — the
  optimizer needs ``f(x)``, not membership, so a re-proposed grid point
  returns the cached metrics without re-simulating (and without inflating the
  trial count). The build token is the wheel's baked crate-source digest, so
  ANY optimizer source change mints NEW identities (Golden Rule #4).
* **Condition 5 — determinism.** The seed derives from the mask's BLAKE3 run
  identity (:func:`seed_from_mask_run_id`, the id's LOW 64 BITS — the
  ADR-0007 wording restored: the retired pycma path had narrowed it to 32
  bits under numpy's MT19937 seed cap, a constraint the in-crate PRNG does
  not have); the inner optimizer runs single-threaded; restart *k* seeds
  ``seed + k`` (u64 wrapping) so the restart streams are disjoint but fully
  derived. Reproducibility is claimed for same commit + same wheel + same
  platform ONLY (libm transcendentals are platform-rounded — the same honest
  envelope the pycma path carried).
* **Condition 6 — every evaluation is a Bailey-LdP trial.** EVERY cache-miss
  evaluation is returned in :attr:`RefineMaskReport.evals` so the caller
  appends each as a :class:`~brutex.engine.results_store.ResultRow` into the
  SAME pool as the grid survivors (slice-5 wiring) — never just the per-mask
  optimum. ``evals_total`` is this RUN's unique-eval count (an audit metric);
  the pass-level DSR-N contribution is the UNIQUE persisted refined rows —
  two survivors sharing a mask can each pay a cache-miss for the same
  quantized point that persists as ONE row, so ``Σ evals_total ≥ unique
  trials`` (corrected per ADR-0008 condition 4 / DEC-CASCADE-N-DESIGN-LOCK-001
  finding F8).
* **Condition 8 — honest bounded budget.** ``max_fevals`` asks per restart ×
  a HARD ``max_restarts`` cap (defaults 200 × 3 = ≤600 unique evals/mask; a
  flat-fitness early stop just moves on to the next restart, the cap bounds
  the churn). The Rust driver enforces the per-restart cap STRICTLY — the
  retired pycma loop could overshoot by a partial population (measured 121
  asks on a 60-ask budget); this port closes that to the documented bound
  exactly. No wall-time claim is made.
* **§4 observability.** ``STAGE_3_REFINE_START`` / ``…_FLUSH`` /
  ``…_COMPLETE`` audit lines per refinement run through the injected
  hash-chained writer, mirroring the sweep's three-line pattern.

**Honest envelope (Golden Rule #10):** each evaluation is a FULL backtest —
O(bars) per eval, ≤600 evals per mask, NOT an O(1) path and not CI-hot;
condition 7 (train-window-only bars) is the CALLER's obligation — this module
evaluates whatever the injected closure evaluates, and the slice-5 wiring
binds the lockbox-excluded training bars exactly as Stage 2 does.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import structlog

from brutex.engine.refine_rust import optimizer_versions, require_refine_rust
from brutex.engine.stage3_grid import MODE_A_SL_AXIS, MODE_A_TP_AXIS, normalize_sl_tp
from brutex.models import BruteXError

if TYPE_CHECKING:
    from brutex.metrics.compute import ResultMetrics

_LOG = structlog.get_logger("brutex.engine.stage3_refine")

#: The three hash-chained audit actions (ADR-0007 §4), mirroring the sweep's
#: SWEEP_START / SWEEP_FLUSH / SWEEP_COMPLETE three-line pattern.
STAGE3_ACTION_START = "STAGE_3_REFINE_START"
STAGE3_ACTION_FLUSH = "STAGE_3_REFINE_FLUSH"
STAGE3_ACTION_COMPLETE = "STAGE_3_REFINE_COMPLETE"

#: A BLAKE3 run identity is 64 lowercase-hex chars (the combo_hash contract).
_RUN_ID_HEX_LEN = 64


class Stage3RefineError(BruteXError):
    """Raised for invalid refinement configuration or evaluator misuse."""


class AuditSink(Protocol):
    """The ``append`` slice of the sweep's hash-chained audit writer."""

    def append(self, *, action: str, subject: str, payload: Mapping[str, object]) -> object:
        """Append one hash-chained audit line."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


#: One full standalone backtest at a QUANTIZED grid point — the engine seam.
#: Production binds ``lambda sl, tp: metrics_for_mask(bars, bar_bits, mask, …,
#: stop_loss_paisa=sl, take_profit_paisa=tp, …)``; tests bind a synthetic
#: objective. The runner guarantees both arguments are on-grid ints.
Evaluator = Callable[[int, int], "ResultMetrics"]


@dataclass(frozen=True, slots=True)
class RefineBudget:
    """The bounded per-mask search budget (ADR-0007 condition 8).

    ``max_fevals`` CMA asks per restart × a HARD ``max_restarts`` cap — the
    spec default 200 × 3 bounds a mask at ≤600 unique evaluations. ``sigma0``
    is the initial step in the normalized [0,1]² space.
    """

    max_fevals: int = 200
    max_restarts: int = 3
    sigma0: float = 0.3

    def __post_init__(self) -> None:
        """Reject non-positive budgets (a zero budget is a disabled run, not a loop)."""
        if self.max_fevals < 1:
            raise Stage3RefineError(f"max_fevals must be >= 1, got {self.max_fevals}")
        if self.max_restarts < 1:
            raise Stage3RefineError(f"max_restarts must be >= 1, got {self.max_restarts}")
        if not 0.0 < self.sigma0 <= 1.0:
            raise Stage3RefineError(f"sigma0 must be in (0, 1], got {self.sigma0}")

    def canonical(self) -> str:
        """Deterministic encoding for the refinement-run identity (condition 4)."""
        return f"fevals={self.max_fevals};restarts={self.max_restarts};sigma0={self.sigma0!r}"


def refinement_run_id(
    *,
    mask_run_id: str,
    budget: RefineBudget,
    seed: int,
    optimizer_version: str,
    optimizer_build: str,
) -> str:
    """The refinement run's BLAKE3 identity (ADR-0007 condition 4).

    ``blake3(mask_run_id ‖ optimizer-config ‖ seed ‖ budget ‖ optimizer
    version + build digest)`` — the resumable/dedup unit is the WHOLE
    per-mask run, so a bumped optimizer version, a REBUILT-from-changed-source
    wheel (the build digest moves), a changed budget, or a changed seed
    yields a NEW identity (cached per-eval values from another identity are
    never reused). The payload version token is ``stage3-refine-v2``
    (DEC-RUST-STAGE3-REFINE-001 — the v1 formula folded the retired
    ``(pycma, numpy)`` pair; greenfield ruling, no stored v1 identities
    exist).

    The three injectable string tokens (``mask_run_id``, ``optimizer_version``,
    ``optimizer_build``) are screened for the ``\\x1f`` join delimiter through
    the COMMON :func:`~brutex.strategies.combo.reject_unit_separator` guard
    (DEC-RUN-IDENTITY-GUARD-PARITY-001) — REJECT-ONLY, so every
    currently-legal input's digest is byte-unchanged (golden-pinned). The
    other two joined tokens are clean by construction (``budget.canonical()``
    emits only ``key=value;`` scalars; ``f"seed={seed}"`` formats an int) and
    are deliberately NOT guarded — a dead branch would fake coverage. The
    combo canonicaliser's recursion depth cap is structurally N/A here: the
    payload is flat scalars, no recursion exists to cap.

    Raises
    ------
    Stage3RefineError
        If ``mask_run_id``, ``optimizer_version``, or ``optimizer_build``
        contains the ``\\x1f`` unit-separator byte.
    """
    import fno_math

    from brutex.strategies.combo import reject_unit_separator

    reject_unit_separator(
        "refinement_run_id",
        (
            ("mask_run_id", mask_run_id),
            ("optimizer_version", optimizer_version),
            ("optimizer_build", optimizer_build),
        ),
        error=Stage3RefineError,
    )

    payload = "\x1f".join(
        (
            "stage3-refine-v2",
            mask_run_id,
            budget.canonical(),
            f"seed={seed}",
            f"optimizer={optimizer_version}",
            f"build={optimizer_build}",
        )
    )
    return str(fno_math.blake3_hash(payload.encode("utf-8")).hex())


def seed_from_mask_run_id(mask_run_id: str) -> int:
    """Derive the mask's deterministic optimizer seed (ADR-0007 condition 5).

    The LOW 64 bits of the mask's BLAKE3 run identity — fully derived, no RNG
    of its own, distinct across masks with overwhelming probability. This is
    the ADR-0007 wording RESTORED (DEC-RUST-STAGE3-REFINE-001): the retired
    pycma path narrowed the derivation to 32 bits because numpy's MT19937
    caps seeds at ``2**32 − 1``; the in-crate xoshiro256++ takes the full
    u64, so the narrowing is gone (greenfield ruling — no cached 32-bit-seed
    identities exist to preserve).
    """
    if len(mask_run_id) != _RUN_ID_HEX_LEN:
        raise Stage3RefineError(
            f"mask_run_id must be the 64-char BLAKE3 hex identity, got {len(mask_run_id)} chars"
        )
    return int(mask_run_id[-16:], 16)


@dataclass(frozen=True, slots=True)
class RefinedEval:
    """One cache-miss evaluation — a Bailey-LdP trial (ADR-0007 condition 6)."""

    sl_paisa: int
    tp_paisa: int
    metrics: ResultMetrics


@dataclass(frozen=True, slots=True)
class RefineMaskReport:
    """Outcome of one per-mask refinement run."""

    refinement_run_id: str
    seed: int
    restarts_run: int
    #: Unique (cache-miss) evaluations of THIS run — an audit metric. NOT the
    #: pass-level DSR-N contribution: identical evals across survivors sharing
    #: a mask collapse to ONE persisted row (ADR-0008 condition 4 / F8).
    evals_total: int
    #: CMA asks answered from the value cache (re-proposed grid points).
    cache_hits: int
    best_sl_paisa: int
    best_tp_paisa: int
    best_sharpe: float
    #: EVERY unique evaluation, in evaluation order (condition 6 — the caller
    #: appends each into the merged pool, never just the optimum).
    evals: tuple[RefinedEval, ...]


def _sharpe_of(metrics: ResultMetrics) -> float:
    """Project the pool-independent objective (condition 2); ``None`` → 0.0."""
    value = metrics.sharpe_ratio
    return value if value is not None else 0.0


def refine_mask(
    *,
    mask_run_id: str,
    start_sl_paisa: int,
    start_tp_paisa: int,
    evaluate: Evaluator,
    budget: RefineBudget | None = None,
    audit: AuditSink | None = None,
) -> RefineMaskReport:
    """Run ONE mask's bounded restarted-CMA-ES refinement over the (SL, TP) grid.

    The inner loop (ADR-0007 §1, DEC-RUST-STAGE3-REFINE-001): seed = the mask
    identity's low 64 bits; restart *k* runs a fresh single-threaded CMA-ES at
    ``seed + k`` in normalized [0,1]² with at most ``max_fevals`` asks (the
    hard per-restart cap, enforced inside the Rust driver); every proposed
    point is snapped to the Mode-A grid (ints) INSIDE the driver before it is
    exposed; a re-proposed grid point is answered from the run-scoped value
    cache. The objective is the pool-independent ``sharpe_ratio`` (maximized —
    condition 2; the driver minimizes, so this wrapper negates).

    Parameters
    ----------
    mask_run_id
        The survivor's 64-char BLAKE3 run identity (drives the seed + the
        refinement-run identity).
    start_sl_paisa, start_tp_paisa
        The survivor's CURRENT grid cell — the search's normalized x0 (must be
        on-grid; :func:`~brutex.engine.stage3_grid.normalize_sl_tp` rejects
        off-grid values, fail-fast).
    evaluate
        The engine seam — one FULL backtest at a quantized grid point.
    budget
        The bounded search budget (``None`` → the ADR defaults 200 × 3).
    audit
        Optional hash-chained writer for the three STAGE_3_REFINE_* lines.

    Returns
    -------
    RefineMaskReport
        The run identity, every unique evaluation (condition 6), and the best
        quantized candidate.

    Raises
    ------
    brutex.engine.refine_rust.RefineRustUnavailableError
        When the Rust runtime cannot be engaged (flag off / wheel absent) —
        loud, never a silent fallback (there is none).
    """
    runtime = require_refine_rust()

    budget = budget if budget is not None else RefineBudget()
    seed = seed_from_mask_run_id(mask_run_id)
    optimizer_version, optimizer_build = optimizer_versions()
    run_id = refinement_run_id(
        mask_run_id=mask_run_id,
        budget=budget,
        seed=seed,
        optimizer_version=optimizer_version,
        optimizer_build=optimizer_build,
    )
    subject = f"refine:{run_id[:16]}"
    if audit is not None:
        audit.append(
            action=STAGE3_ACTION_START,
            subject=subject,
            payload={
                "refinement_run_id": run_id,
                "mask_run_id": mask_run_id,
                "seed": seed,
                "budget": budget.canonical(),
                "optimizer_version": optimizer_version,
                "optimizer_build": optimizer_build,
                "x0": [start_sl_paisa, start_tp_paisa],
            },
        )

    x0 = normalize_sl_tp(start_sl_paisa, start_tp_paisa)
    session = runtime.RefineSession(
        x0,
        budget.sigma0,
        seed,
        budget.max_fevals,
        budget.max_restarts,
        (MODE_A_SL_AXIS.lo_paisa, MODE_A_SL_AXIS.hi_paisa, MODE_A_SL_AXIS.tick_paisa),
        (MODE_A_TP_AXIS.lo_paisa, MODE_A_TP_AXIS.hi_paisa, MODE_A_TP_AXIS.tick_paisa),
    )

    cache: dict[tuple[int, int], ResultMetrics] = {}
    evals: list[RefinedEval] = []
    cache_hits = 0
    while not session.done():
        values: list[float] = []
        for sl, tp in session.ask():
            hit = cache.get((sl, tp))
            if hit is None:
                hit = evaluate(sl, tp)
                cache[(sl, tp)] = hit
                evals.append(RefinedEval(sl_paisa=sl, tp_paisa=tp, metrics=hit))
            else:
                cache_hits += 1
            # NEGATED Sharpe — the driver minimizes (condition 2).
            values.append(-_sharpe_of(hit))
        session.tell(values)
    restarts_run = session.restarts_run()

    best_key = max(cache, key=lambda key: (_sharpe_of(cache[key]), (-key[0], -key[1])))
    best_sharpe = _sharpe_of(cache[best_key])
    if audit is not None:
        audit.append(
            action=STAGE3_ACTION_FLUSH,
            subject=subject,
            payload={
                "evals_total": len(evals),
                "cache_hits": cache_hits,
                "restarts_run": restarts_run,
            },
        )
        audit.append(
            action=STAGE3_ACTION_COMPLETE,
            subject=subject,
            payload={
                "refinement_run_id": run_id,
                "evals_total": len(evals),
                "best_sl_paisa": best_key[0],
                "best_tp_paisa": best_key[1],
                "best_sharpe": best_sharpe,
            },
        )
    _LOG.info(
        "stage3.refine.mask",
        refinement_run_id=run_id,
        mask_run_id=mask_run_id,
        evals_total=len(evals),
        cache_hits=cache_hits,
        restarts_run=restarts_run,
        best_sl_paisa=best_key[0],
        best_tp_paisa=best_key[1],
        best_sharpe=best_sharpe,
    )
    return RefineMaskReport(
        refinement_run_id=run_id,
        seed=seed,
        restarts_run=restarts_run,
        evals_total=len(evals),
        cache_hits=cache_hits,
        best_sl_paisa=best_key[0],
        best_tp_paisa=best_key[1],
        best_sharpe=best_sharpe,
        evals=tuple(evals),
    )


__all__ = [
    "STAGE3_ACTION_COMPLETE",
    "STAGE3_ACTION_FLUSH",
    "STAGE3_ACTION_START",
    "AuditSink",
    "Evaluator",
    "RefineBudget",
    "RefineMaskReport",
    "RefinedEval",
    "Stage3RefineError",
    "optimizer_versions",
    "refine_mask",
    "refinement_run_id",
    "seed_from_mask_run_id",
]
