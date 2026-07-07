"""Stage-3 BIPOP-CMA-ES refinement runner (ADR-0007 §5 slice 4 — DEC-STAGE3-REFINE-001).

The deterministic refinement pass over the Stage-2 survivor set: for each
survivor mask (per leaderboard, in leaderboard order — ADR-0007 condition 1),
an inner CMA-ES searches the continuous (SL, TP) plane normalized to [0,1]²,
denormalized and **snapped to the int-paisa Mode-A grid BEFORE every
evaluation** (condition 3 — floats never reach the 9-field hash), each
evaluation being one full standalone backtest through the unchanged engine
path (the *evaluate* seam — production binds a
:func:`~brutex.engine.sweep_driver.metrics_for_mask` closure; tests bind a
synthetic objective).

Design (the nine ADR-0007 conditions this module owns):

* **Condition 4 — refinement-run identity + value cache.** The resumable /
  dedup unit is the WHOLE per-mask refinement run, identified by
  :func:`refinement_run_id` = ``blake3(mask_run_id ‖ optimizer-config ‖ seed ‖
  budget ‖ pycma/numpy versions)``. Per-eval results live in a run-scoped
  VALUE cache keyed by the quantized ``(sl_paisa, tp_paisa)`` — the optimizer
  needs ``f(x)``, not membership, so a re-proposed grid point returns the
  cached metrics without re-simulating (and without inflating the trial
  count).
* **Condition 5 — determinism.** The seed derives from the mask's BLAKE3 run
  identity (:func:`seed_from_mask_run_id`, the id's low 64 bits); the inner
  optimizer runs single-threaded; restart *k* seeds ``seed + k`` so the
  restart streams are disjoint but fully derived. Reproducibility is claimed
  for same commit + same lockfile + same platform ONLY (CMA-ES
  eigendecomposition is ULP-BLAS-sensitive — the A/B oracle in the tests
  pins same-process determinism, nothing more).
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
  the churn). No wall-time claim is made.
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

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import structlog

from brutex.engine.stage3_grid import denormalize_sl_tp, normalize_sl_tp
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


def optimizer_versions() -> tuple[str, str]:
    """Return ``(pycma_version, numpy_version)`` — the identity's library fields.

    Imported lazily so the engine package never hard-requires the ``optimize``
    extra (DEC-STAGE3-OPTIMIZE-EXTRA-001): a build without pycma can still run
    Stage 2; only an actual refinement run needs it.
    """
    import cma  # type: ignore[import-untyped, unused-ignore]  # no py.typed in pycma; dual-env
    import numpy

    return str(cma.__version__), str(numpy.__version__)


def refinement_run_id(
    *,
    mask_run_id: str,
    budget: RefineBudget,
    seed: int,
    pycma_version: str,
    numpy_version: str,
) -> str:
    """The refinement run's BLAKE3 identity (ADR-0007 condition 4).

    ``blake3(mask_run_id ‖ optimizer-config ‖ seed ‖ budget ‖ library
    versions)`` — the resumable/dedup unit is the WHOLE per-mask run, so a
    bumped optimizer version, budget, or seed yields a NEW identity (cached
    per-eval values from another identity are never reused).

    The three injectable string tokens (``mask_run_id``, ``pycma_version``,
    ``numpy_version``) are screened for the ``\\x1f`` join delimiter through
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
        If ``mask_run_id``, ``pycma_version``, or ``numpy_version`` contains
        the ``\\x1f`` unit-separator byte.
    """
    import fno_math

    from brutex.strategies.combo import reject_unit_separator

    reject_unit_separator(
        "refinement_run_id",
        (
            ("mask_run_id", mask_run_id),
            ("pycma_version", pycma_version),
            ("numpy_version", numpy_version),
        ),
        error=Stage3RefineError,
    )

    payload = "\x1f".join(
        (
            "stage3-refine-v1",
            mask_run_id,
            budget.canonical(),
            f"seed={seed}",
            f"cma={pycma_version}",
            f"numpy={numpy_version}",
        )
    )
    return str(fno_math.blake3_hash(payload.encode("utf-8")).hex())


def seed_from_mask_run_id(mask_run_id: str) -> int:
    """Derive the mask's deterministic optimizer seed (ADR-0007 condition 5).

    The LOW 32 bits of the mask's BLAKE3 run identity — fully derived, no RNG
    of its own, distinct across masks with overwhelming probability. ADR-0007
    wrote "low 64 bits"; numpy's MT19937 (pycma's RNG) hard-caps seeds at
    ``2**32 - 1``, so the derivation narrows to 32 bits — an
    implementation-forced narrowing recorded in DEC-STAGE3-REFINE-001, with
    the determinism property unchanged (same identity ⇒ same seed).
    """
    if len(mask_run_id) != _RUN_ID_HEX_LEN:
        raise Stage3RefineError(
            f"mask_run_id must be the 64-char BLAKE3 hex identity, got {len(mask_run_id)} chars"
        )
    return int(mask_run_id[-8:], 16)


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
    """Run ONE mask's bounded BIPOP-style CMA-ES refinement over the (SL, TP) grid.

    The inner loop (ADR-0007 §1): seed = the mask identity's low 64 bits;
    restart *k* runs a fresh single-threaded ``cma.CMAEvolutionStrategy`` at
    ``seed + k`` in normalized [0,1]² with ``max_fevals`` asks; every proposed
    point is snapped to the Mode-A grid (ints) BEFORE evaluation; a re-proposed
    grid point is answered from the run-scoped value cache. The objective is
    the pool-independent ``sharpe_ratio`` (maximized — condition 2).

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
    """
    import cma  # type: ignore[import-untyped, unused-ignore]  # no py.typed in pycma; dual-env

    budget = budget if budget is not None else RefineBudget()
    seed = seed_from_mask_run_id(mask_run_id)
    pycma_version, numpy_version = optimizer_versions()
    run_id = refinement_run_id(
        mask_run_id=mask_run_id,
        budget=budget,
        seed=seed,
        pycma_version=pycma_version,
        numpy_version=numpy_version,
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
                "pycma_version": pycma_version,
                "numpy_version": numpy_version,
                "x0": [start_sl_paisa, start_tp_paisa],
            },
        )

    x0 = normalize_sl_tp(start_sl_paisa, start_tp_paisa)
    cache: dict[tuple[int, int], ResultMetrics] = {}
    evals: list[RefinedEval] = []
    cache_hits = 0

    def _objective(point: Iterable[float]) -> float:
        """Quantize → cached full backtest → NEGATED Sharpe (CMA minimizes)."""
        nonlocal cache_hits
        u_sl, u_tp = point
        sl, tp = denormalize_sl_tp(float(u_sl), float(u_tp))
        hit = cache.get((sl, tp))
        if hit is None:
            hit = evaluate(sl, tp)
            cache[(sl, tp)] = hit
            evals.append(RefinedEval(sl_paisa=sl, tp_paisa=tp, metrics=hit))
        else:
            cache_hits += 1
        return -_sharpe_of(hit)

    restarts_run = 0
    for restart in range(budget.max_restarts):
        restarts_run += 1
        es = cma.CMAEvolutionStrategy(
            list(x0),
            budget.sigma0,
            {
                # Disjoint, fully-derived restart streams — wrapped to numpy's
                # uint32 seed domain (the same cap that narrows the base seed).
                "seed": (seed + restart) % 2**32,
                "verbose": -9,
                "maxfevals": budget.max_fevals,
                "bounds": [0.0, 1.0],
            },
        )
        while not es.stop():
            asks = es.ask()
            es.tell(asks, [_objective(a) for a in asks])

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
