"""Stage-4 cascade orchestrator — chain all nine statistical filters into one
end-to-end pipeline that turns a candidate pool into a ranked survivor set.

Implements the Stage-4 pipeline of ``docs/TRACK1_BACKTEST_SPEC.md`` §"Stage 4"
(lines 371-631): the **hard rejection gates** (lines 589-600) run first, then
the **nine statistical filters in spec order** with short-circuit (a candidate
that fails any filter is rejected, recording the stage + statistic), then the
survivors are ranked by the **composite score** (lines 602-615). Ledgered
under **DEC-STAT-CASCADE-001**.

The nine filter *engines* are the pre-existing pure functions in
:mod:`brutex.metrics` (DSR #2, CPCV #3, Reality-Check/SPA #4, PBO #5,
Romano-Wolf #6, Walk-Forward #7, Lockbox #8, Monte-Carlo #9); this module is
the *orchestration* that wires them together. It is **pure**: every
per-candidate series the filters need (returns / trades / entry signals + exit
logic) is carried on the injected :class:`CandidateInput`, so the orchestrator
is deterministic and 100%-testable with hand-built candidates. In production a
thin adapter builds the :class:`CandidateInput`\\s by re-running the Stage-2
trade-sim (`brutex.engine.trade_sim.simulate_trades`) on the top leaderboard
rows and reading the lake — that adapter is **out of scope here** (it needs
real bars; honest envelope below).

Per-candidate vs pool-level filters
-----------------------------------
Five filters score one candidate at a time (DSR, CPCV, WFO, Lockbox,
Monte-Carlo). Three are **pool-level** — they consume the matrix of the
*still-surviving* candidates' returns:

* **SPA / Reality Check (#4)** asks "is the best candidate in the pool real,
  given best-of-N selection?". The cascade's documented choice
  (DEC-CASCADE-STAT-TIER-WIRE-001 — a monotone-tightening JOIN, RC first):
  if the pool's White Reality-Check p-value ``>= spa_alpha`` the whole
  surviving pool is data-snooped noise → **all remaining are rejected at
  SPA** (the byte-identical historical floor); when the RC passes, Hansen's
  (2005) SPA-consistent p-value — the studentized, null-recentred PRIMARY
  test (White's RC is conservative and distortable by poor/irrelevant
  alternatives) — must ALSO clear ``spa_alpha`` or the pool is rejected
  with a Hansen-SPA detail. Intersection composition keeps the false-pass
  probability ``<= spa_alpha``; the SPA arm can only ADD rejections.
* **PBO (#5)** is a pool overfitting probability; if ``pbo > pbo_max`` the
  pool is overfit → **all remaining are rejected at PBO**.
* **Romano-Wolf (#6)** returns the FWER rejection set (the individually-real
  strategies); the cascade **keeps only that set**, rejecting the rest.

These pool→per-candidate verdict mappings are a cascade design choice (the
spec pins each filter's own pass rule but not the cross-filter wiring); they
are documented here and configurable. Spec-pinned thresholds are the defaults
where given (WFO ≥0.75 of walks, SPA p<0.05, MC p<0.01, Lockbox per #8); the
DSR/CPCV/PBO cut-offs are conventional defaults flagged in :class:`CascadeConfig`
(no invented spec number — Golden Rule #1).

An OPTIONAL default-OFF **single-event sensitivity** stage
(DEC-SWEEP-SINGLE-EVENT-SENSITIVITY-GATE-001) runs between the hard gates and
DSR when :attr:`CascadeConfig.single_event_max_best_day_pct` is set: group the
candidate's per-bar returns by IST day (the ``worst5_days`` day law — the day
ordinals ``build_candidate_input`` already carries) and FAIL iff the total is
non-positive OR the total MINUS the best single day is strictly below X% of the
total ("would you still be profitable without your best day?"). This gate is
ADDITIONAL defense-in-depth for the windfall-fluke class; **DSR remains the
load-bearing anti-single-event filter** — the gate never replaces it. ``None``
(the default) means the stage does not run and is fully absent from behavior.

Robustness (no degenerate input crashes the sweep)
--------------------------------------------------
A brute-force sweep feeds wildly varying candidates, so the orchestrator never
lets one bad candidate abort the run (Golden Rule #1 "no NaN/inf", §9 hostile
review):

* :class:`CandidateInput` validates at construction — every scalar metric and
  every returns/lockbox value must be finite and the id non-empty, else a clear
  ``ValueError`` (turns an otherwise-opaque ``inf``/``nan`` crash deep in the
  composite z-score into a named error at the boundary).
* The **pool studentized tests** (SPA/RW) require each candidate's returns to
  have ``>= 2`` observations *and* positive variance. A constant or too-short
  excess series is *degenerate* — it is rejected at the SPA stage **before** the
  pool matrix is built, so Romano-Wolf never sees a zero-variance column (it
  would otherwise raise; reality_check tolerates it but RW does not).
* Each **per-candidate** filter call (DSR/CPCV/WFO/Lockbox/Monte-Carlo) is
  guarded: a domain ``ValueError`` from the engine (e.g. a returns series too
  short for the CPCV/WFO windows, an out-of-domain DSR radicand) becomes a
  *rejection of that one candidate at that stage* with the engine's message
  recorded in ``detail`` — it does not propagate. Only domain ``ValueError``\\s
  are caught; a genuine wiring bug (``TypeError``/``AttributeError``) still
  surfaces loudly.

Honest envelope (Golden Rule #10)
---------------------------------
Offline Stage-4 batch work, **not** an ``O(1)`` hot path: cost is
``O(K)`` per-candidate filter calls plus the pool bootstraps, run once on the
top ~1,000 candidates. Determinism (Golden Rule #4) is threaded through the
single :class:`BootstrapConfig` seed (SPA/RW) + the Monte-Carlo seed. This PR
ships the *orchestration logic*; the production series-regeneration adapter
(trade-sim + lake) and the live leaderboard feed are deferred.
"""

from __future__ import annotations

import itertools
import math
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Final

import numpy as np

from brutex.metrics.cpcv import DEFAULT_N_GROUPS, DEFAULT_N_TEST_GROUPS, cpcv_pass_rate
from brutex.metrics.deflated_sharpe import deflated_sharpe_ratio
from brutex.metrics.extreme_vol_stratum import evaluate_stratum
from brutex.metrics.lockbox import (
    LOCKBOX_DEFAULT_MIN_TRADES,
    LOCKBOX_DEFAULT_WINDOW_DAYS,
    LockboxResult,
    lockbox_evaluation,
)
from brutex.metrics.monte_carlo import monte_carlo_permutation_test
from brutex.metrics.pbo import pbo_probability
from brutex.metrics.regime_fragility import evaluate_fragility
from brutex.metrics.spa import BootstrapConfig, hansen_spa, reality_check, romano_wolf
from brutex.metrics.wfo import WFOScheme, wfo_pass_rate

if TYPE_CHECKING:
    import numpy.typing as npt

__all__ = [
    "CandidateInput",
    "CandidateVerdict",
    "CascadeConfig",
    "CascadeReport",
    "DsrTrialStats",
    "FilterStage",
    "run_stage4_cascade",
]

#: Composite z-score clamp (spec line 618: "clamped to [-3, +3]").
_Z_CLAMP: Final[float] = 3.0

#: Minimum sample count for a meaningful dispersion (variance / z-score) and
#: for the pool studentized tests (matches ``spa._MIN_OBSERVATIONS``).
_MIN_FOR_DISPERSION: Final[int] = 2

#: Minimum surviving pool size for PBO/CSCV (needs >= 2 strategies to rank).
_MIN_POOL_FOR_PBO: Final[int] = 2

#: The scalar metric fields that must be finite on a :class:`CandidateInput`
#: (the hard gates + the composite z-score read them; ``inf``/``nan`` would
#: otherwise crash the composite). The integer fields are finite by type.
_FINITE_METRIC_FIELDS: Final[tuple[str, ...]] = (
    "sharpe_ratio",
    "sortino_ratio",
    "calmar_ratio",
    "profit_factor",
    "win_rate",
    "win_loss_ratio",
    "recovery_factor",
    "max_drawdown_pct",
    "training_sharpe",
    "training_max_drawdown",
    "skew",
    "kurtosis",
)

#: Composite-score weights (spec lines 686-697), summing to 1.0.
_COMPOSITE_WEIGHTS: Final[dict[str, float]] = {
    "sharpe_ratio": 0.25,
    "sortino_ratio": 0.20,
    "calmar_ratio": 0.15,
    "profit_factor_excess": 0.15,  # profit_factor - 1.0
    "recovery_factor": 0.10,
    "win_quality": 0.10,  # win_rate * win_loss_ratio
    "lockbox_sharpe": 0.05,
}


#: DEC-SWEEP-SINGLE-EVENT-SENSITIVITY-GATE-001 — the single-event knob is a
#: PERCENT: valid strictly between 0 and this full scale.
_PCT_FULL_SCALE = 100.0


def validate_single_event_pct(value: float | None) -> float | None:
    """The ONE validation law for the single-event sensitivity knob.

    ``None`` (OFF) passes through; otherwise the value must be a non-bool
    finite number strictly between 0 and 100 (percent). Junk raises
    :class:`ValueError` naming BOTH the config field and its CLI flag
    (``--single-event-max-best-day-pct``), so the eager ``cmd_sweep_all``
    parse and :meth:`CascadeConfig.__post_init__` share exactly this one law
    (DEC-SWEEP-SINGLE-EVENT-SENSITIVITY-GATE-001, review round 1 — junk must
    refuse BEFORE any compute, never after a completed multi-day sweep).
    """
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        # The finite check is LOAD-BEARING in this exclusion form: a NaN
        # compares False against BOTH bounds below, so without it a NaN
        # would silently pass (mutation-killed, review round 1).
        or not math.isfinite(value)
        or float(value) <= 0.0
        or float(value) >= _PCT_FULL_SCALE
    ):
        raise ValueError(
            "single_event_max_best_day_pct (--single-event-max-best-day-pct) "
            "must be None (OFF) or a finite percent strictly between 0 and "
            f"100, got {value!r} (DEC-SWEEP-SINGLE-EVENT-SENSITIVITY-GATE-001)"
        )
    return value


class FilterStage(Enum):
    """The pipeline stage at which a candidate was rejected (or that it passed)."""

    HARD_GATES = "hard_gates"
    SINGLE_EVENT_SENSITIVITY = "single_event_sensitivity"
    DEFLATED_SHARPE = "deflated_sharpe"
    CPCV = "cpcv"
    REALITY_CHECK_SPA = "reality_check_spa"
    PBO = "pbo"
    ROMANO_WOLF = "romano_wolf"
    WALK_FORWARD = "walk_forward"
    LOCKBOX = "lockbox"
    MONTE_CARLO = "monte_carlo"
    # Default-OFF filter #10 (DEC-REGIME-MUST-SURVIVE-STRATUM-001): the
    # extreme-vol must-survive stratum — runs ONLY when
    # ``CascadeConfig.extreme_vol_stratum`` is True.
    EXTREME_VOL_STRATUM = "extreme_vol_stratum"
    # Default-OFF filter #11 (DEC-REGIME-FRAGILITY-GATE-001): the
    # regime-fragility gate — runs ONLY when
    # ``CascadeConfig.regime_fragility`` is True.
    REGIME_FRAGILITY = "regime_fragility"


@dataclass(frozen=True, slots=True)
class CandidateInput:
    """All inputs the nine filters need for one candidate (injected, pure).

    The scalar fields drive the hard rejection gates + composite ranking; the
    series fields drive the statistical filters. ``returns`` is the per-period
    excess-return series shared by CPCV / WFO / DSR and the SPA/RW/PBO pool
    matrix; all pooled candidates must share the same ``len(returns)``.

    Validated at construction (Golden Rule #1 — no NaN/inf): ``candidate_id``
    must be non-empty and every scalar metric + every returns/lockbox value
    must be finite, else :class:`ValueError`.
    """

    candidate_id: str
    # Scalar metrics (hard gates + composite ranking)
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    profit_factor: float
    win_rate: float
    win_loss_ratio: float
    recovery_factor: float
    max_drawdown_pct: float
    max_consecutive_losses: int
    total_trades: int
    training_sharpe: float
    training_max_drawdown: float
    # Distribution moments for the Deflated Sharpe PSR (non-excess kurtosis)
    skew: float
    kurtosis: float
    # Series for the statistical filters
    returns: tuple[float, ...]
    lockbox_returns: tuple[float, ...]
    lockbox_days: tuple[int, ...]
    mc_n_bars: int
    mc_n_entries: int
    mc_evaluate: Callable[[npt.NDArray[np.intp]], float]
    #: TRUE per-trade exit-day ordinals for the lockbox filter #8
    #: (DEC-STATS-GATE-INTEGRITY-001, R2-SR-F2): when supplied, the lockbox
    #: count gate + window extension are driven by real TRADES instead of the
    #: (per-bar) ``lockbox_returns`` observation count. ``None`` (the default)
    #: keeps the legacy bar-count behaviour byte-identical (a hand-built
    #: per-trade caller needs no evidence — its rows already ARE trades).
    #: May be EMPTY (a zero-trade candidate ⇒ honest ``INSUFFICIENT_N``).
    lockbox_trade_days: tuple[int, ...] | None = None
    #: The candidate's OWN search-family return COLUMNS for the per-candidate
    #: CSCV/PBO check at filter #5 (DEC-CASCADE-STAT-TIER-WIRE-001): one
    #: per-bar return series per member of the candidate's Stage-3 eval family
    #: — the (sl, tp) configurations the SAME mask was actually TRIED at —
    #: with the finalist's own series included as one column (the wire layer
    #: guarantees membership; see ``cascade_wire.build_candidate_input``).
    #: ``None`` (the default) = family unavailable → the per-candidate PBO is
    #: honestly SKIPPED (a pre-family manifest / a pure grid winner / a
    #: hand-built candidate). Every column must be all-finite and share
    #: ``len(returns)`` (the same bar axis).
    family_returns: tuple[tuple[float, ...], ...] | None = None
    #: Per-trade extreme-vol stratum membership flags for filter #10
    #: (DEC-REGIME-MUST-SURVIVE-STRATUM-001): ``flags[i]`` is True iff trade
    #: *i*'s ENTRY session classified extreme
    #: (:func:`brutex.metrics.extreme_vol_stratum.classify_extreme_sessions`).
    #: Aligned index-for-index with ``trade_pnls_paisa`` (same length when
    #: BOTH are present). ``None`` (the default) = data unavailable — the
    #: filter records the honest ``"unproven_current_regime"`` abstain,
    #: never a fabricated verdict and never a reject.
    extreme_vol_entry_flags: tuple[bool, ...] | None = None
    #: Per-trade net P&L (paisa, signed) aligned with
    #: ``extreme_vol_entry_flags`` — the series filter #10 splits by the
    #: flags. ``None`` (the default) keeps every existing construction
    #: byte-identical; the filter needs BOTH fields to evaluate.
    trade_pnls_paisa: tuple[int, ...] | None = None
    #: Per-trade session-lens trend token of the ENTRY session for filter
    #: #11 (DEC-REGIME-FRAGILITY-GATE-001) — the taxonomy leaf maps it
    #: (junk degrades to the UNKNOWN bucket, excluded from the laws).
    #: Aligned index-for-index with ``trade_pnls_paisa`` (same length when
    #: BOTH are present). ``None`` (the default) = the axis is skipped —
    #: an honest abstain, never a fabricated verdict and never a reject.
    trade_trend_tokens: tuple[str, ...] | None = None
    #: Per-trade structural-era label of the ENTRY date for filter #11
    #: (``regime_taxonomy.era_for_date(...).label``). Same alignment /
    #: default laws as ``trade_trend_tokens``.
    trade_era_labels: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        """Reject a non-finite metric / series value or an empty id up front."""
        if not self.candidate_id:
            raise ValueError("candidate_id must be a non-empty string")
        for name in _FINITE_METRIC_FIELDS:
            value: float = getattr(self, name)
            if not math.isfinite(value):
                raise ValueError(
                    f"CandidateInput.{name} must be finite, got {value!r} "
                    f"(candidate {self.candidate_id!r})"
                )
        for name, series in (("returns", self.returns), ("lockbox_returns", self.lockbox_returns)):
            if not bool(np.all(np.isfinite(np.asarray(series, dtype=np.float64)))):
                raise ValueError(
                    f"CandidateInput.{name} must be all-finite (no NaN/inf) "
                    f"(candidate {self.candidate_id!r})"
                )
        if self.family_returns is not None:
            for index, column in enumerate(self.family_returns):
                if len(column) != len(self.returns):
                    raise ValueError(
                        f"CandidateInput.family_returns[{index}] must share len(returns) "
                        f"({len(self.returns)}), got {len(column)} "
                        f"(candidate {self.candidate_id!r})"
                    )
                if not bool(np.all(np.isfinite(np.asarray(column, dtype=np.float64)))):
                    raise ValueError(
                        f"CandidateInput.family_returns[{index}] must be all-finite "
                        f"(no NaN/inf) (candidate {self.candidate_id!r})"
                    )
        if self.lockbox_trade_days is not None and any(
            later < earlier for earlier, later in itertools.pairwise(self.lockbox_trade_days)
        ):
            raise ValueError(
                f"CandidateInput.lockbox_trade_days must be non-decreasing "
                f"(trades chronological) (candidate {self.candidate_id!r})"
            )
        if (
            self.extreme_vol_entry_flags is not None
            and self.trade_pnls_paisa is not None
            and len(self.extreme_vol_entry_flags) != len(self.trade_pnls_paisa)
        ):
            raise ValueError(
                f"CandidateInput.extreme_vol_entry_flags length "
                f"{len(self.extreme_vol_entry_flags)} != trade_pnls_paisa length "
                f"{len(self.trade_pnls_paisa)} (candidate {self.candidate_id!r})"
            )
        for axis_name, axis_labels in (
            ("trade_trend_tokens", self.trade_trend_tokens),
            ("trade_era_labels", self.trade_era_labels),
        ):
            if (
                axis_labels is not None
                and self.trade_pnls_paisa is not None
                and len(axis_labels) != len(self.trade_pnls_paisa)
            ):
                raise ValueError(
                    f"CandidateInput.{axis_name} length {len(axis_labels)} != "
                    f"trade_pnls_paisa length {len(self.trade_pnls_paisa)} "
                    f"(candidate {self.candidate_id!r})"
                )


def _single_event_verdict(c: CandidateInput, x_pct: float) -> str | None:
    """The single-event sensitivity LAW — reject detail or ``None`` (pass).

    Groups the candidate's per-bar ``returns`` by IST day using the SAME
    per-bar day ordinals ``build_candidate_input`` carries (``lockbox_days``
    is aligned 1:1 with ``returns`` — both are the whole-window per-bar
    series; the ``worst5_days``/``ist_trade_date`` day law). Let
    ``total = Σ returns`` and ``best`` = the max single-day sum. FAIL iff
    ``total <= 0`` OR ``total − best < (x_pct/100)·total`` (strict ``<`` —
    a remainder of exactly X% passes). Measured basis
    (DEC-SWEEP-SINGLE-EVENT-SENSITIVITY-GATE-001): the windfall fluke's
    remainder is 0.3% of total (16× below the 5.0 default) while the
    lumpiest legit comparator keeps 76.7% (15× above); the strict
    ``total − best <= 0`` form is REFUTED (the fluke's scratches net
    positive). Day ordinals that do not align with ``returns`` are an
    evaluation failure → an honest reject detail (fail-safe, the CPCV
    ValueError-arm posture), never a silent pass.
    """
    if len(c.lockbox_days) != len(c.returns):
        return (
            "single-event sensitivity could not be evaluated: day ordinals "
            f"({len(c.lockbox_days)}) do not align with returns ({len(c.returns)})"
        )
    total = math.fsum(c.returns)
    if total <= 0.0:
        return f"single-event sensitivity: total return {total:.6g} <= 0"
    day_sums: dict[int, list[float]] = {}
    for day, r in zip(c.lockbox_days, c.returns, strict=True):
        day_sums.setdefault(day, []).append(r)
    best = max(math.fsum(values) for values in day_sums.values())
    remaining = total - best
    if remaining < (x_pct / _PCT_FULL_SCALE) * total:
        remain_pct = remaining / total * _PCT_FULL_SCALE
        return (
            f"single-event sensitivity: total {total:.6g} minus best day {best:.6g} "
            f"leaves {remaining:.6g} ({remain_pct:.4g}% of total) < {x_pct}% floor"
        )
    return None


@dataclass(frozen=True, slots=True)
class CascadeConfig:
    """Thresholds + seeds for the cascade. ``seed`` is mandatory (Golden Rule #4)."""

    seed: int
    run_hard_gates: bool = True
    # Hard rejection gates (spec lines 595-600). The spec lists six; the sixth
    # (``lockbox_sharpe / training_sharpe < 0.5 ⇒ overfit``, spec line 600) is
    # enforced — more strictly — by the **Lockbox filter #8** (``sharpe_tolerance``
    # = 0.30 rejects below 0.70·training, stricter than the 0.50 ratio here), since
    # the out-of-sample lockbox Sharpe is not known at the pre-gate stage. So the
    # five pre-gates below are scalar-only; the sixth is not a dead pre-gate.
    min_total_trades: int = 30
    min_profit_factor: float = 1.2
    max_drawdown_pct: float = 30.0
    min_win_rate: float = 0.25
    min_win_loss_ratio: float = 2.0
    max_consecutive_losses: int = 10
    # #2 DSR — cut-off is a cascade default (not a spec number; Golden Rule #1)
    dsr_min: float = 0.95
    # #3 CPCV — default cut-off (cascade convention)
    cpcv_min_pass_rate: float = 0.5
    cpcv_n_groups: int = DEFAULT_N_GROUPS
    cpcv_n_test_groups: int = DEFAULT_N_TEST_GROUPS
    # #4 SPA / #6 RW (spec line 401: p < 0.05)
    spa_alpha: float = 0.05
    rw_alpha: float = 0.05
    n_resamples: int = 1000
    # #5 PBO — default cut-off (cascade convention)
    pbo_max: float = 0.5
    pbo_n_splits: int = 16
    # #7 WFO (spec line 414: ≥12/16 walks ⇒ rate ≥ 0.75; all 3 schemes)
    wfo_min_pass_rate: float = 0.75
    wfo_train_size: int = 252
    wfo_test_size: int = 126
    wfo_step: int = 63
    # #8 Lockbox (spec #8 defaults — the lockbox module's own public constants,
    # never re-typed literals; CLAUDE.md §11 COMMON / DEC-STATS-GATE-INTEGRITY-001)
    lockbox_min_trades: int = LOCKBOX_DEFAULT_MIN_TRADES
    lockbox_default_window_days: int = LOCKBOX_DEFAULT_WINDOW_DAYS
    # #9 Monte-Carlo (spec line 472: p < 0.01)
    mc_alpha: float = 0.01
    mc_n_permutations: int = 1000
    #: The Stage-4 walk-forward embargo OVERRIDE in decision-TF bars
    #: (DEC-CASCADE-STAT-TIER-WIRE-001). ``None`` (the default) = AUTO — the
    #: wire resolves one trading day in bars per (instrument, tf) group via
    #: :func:`brutex.engine.cascade_wire.resolve_cascade_embargo_bars` (the
    #: DEC-STAT-001 floor, MIGRATION.md #123: the cascade series are per-BAR
    #: equity returns, so the label horizon is 1 bar and the formula floors at
    #: ``ceil(375 / tf_minutes)``). An explicit ``int >= 0`` overrides
    #: uniformly across every group; a NEGATIVE value is refused by
    #: :func:`brutex.engine.cascade_wire.resolve_cascade_embargo_bars` (the
    #: production route — the ONLY reader of this field) and by
    #: :func:`run_stage4_cascade`'s own ``embargo_bars`` validation —
    #: ``run_stage4_cascade`` never reads this config field; direct callers
    #: pass ``embargo_bars`` explicitly (hostile-review round 1 wording; this
    #: frozen config class's ``__post_init__`` — added by
    #: DEC-SWEEP-SINGLE-EVENT-SENSITIVITY-GATE-001 for the single-event knob —
    #: deliberately does not validate this field).
    #: Appended + defaulted (the additive-config precedent), so an OLD
    #: manifest config dict without the key rebuilds with the default.
    wfo_embargo: int | None = None
    #: Cap on the per-candidate PBO family width (DEC-CASCADE-STAT-TIER-WIRE-001):
    #: the wire re-simulates one return column per family (sl, tp) pair, so an
    #: unbounded family would make the cascade build O(family x bars) without
    #: limit. A family wider than the cap is truncated to the FIRST ``cap``
    #: pairs in the deterministic (sorted) persisted order with ONE loud
    #: structlog WARNING — never silently; if the truncation drops the
    #: finalist's OWN pair the per-candidate PBO is honestly SKIPPED instead
    #: (CSCV over a family that excludes the candidate says nothing about it).
    candidate_pbo_family_cap: int = 256
    # #10 extreme-vol must-survive stratum (DEC-REGIME-MUST-SURVIVE-STRATUM-001)
    # — DEFAULT-OFF: when False (the default) the filter never runs and the
    # report is byte-identical to the pre-filter shape (no stage_survivors
    # key, no verdict fields). Flipping the default ON is its own DEC.
    extreme_vol_stratum: bool = False
    # #11 regime-fragility gate (DEC-REGIME-FRAGILITY-GATE-001) —
    # DEFAULT-OFF: when False (the default) the filter never runs and the
    # report is byte-identical to the pre-filter shape (no stage_survivors
    # key, no verdict fields). Flipping the default ON is its own DEC.
    regime_fragility: bool = False
    #: Default-OFF single-event sensitivity gate
    #: (DEC-SWEEP-SINGLE-EVENT-SENSITIVITY-GATE-001). ``None`` (the default)
    #: means the stage is NOT run — fully absent from behavior (reports
    #: byte-identical to pre-change). When set to X (percent, ``0 < X < 100``,
    #: finite), a candidate FAILS iff its total per-bar return is
    #: non-positive OR ``total − best_single_IST_day < (X/100)·total``
    #: (strict ``<`` — exactly X% remaining passes). Validated at
    #: construction: junk (non-finite, bool, ``<= 0`` or ``>= 100``) raises
    #: :class:`ValueError` loudly. ADDITIONAL defense-in-depth only — DSR
    #: remains the load-bearing anti-single-event filter.
    single_event_max_best_day_pct: float | None = None

    def __post_init__(self) -> None:
        """Validate the single-event knob up front (fail loud, Golden Rule #1).

        DELEGATES to :func:`validate_single_event_pct` — the ONE law shared
        with the eager ``cmd_sweep_all`` CLI parse (review round 1).
        """
        validate_single_event_pct(self.single_event_max_best_day_pct)

    def bootstrap(self) -> BootstrapConfig:
        """The shared deterministic :class:`BootstrapConfig` for SPA / RW."""
        return BootstrapConfig(seed=self.seed, n_resamples=self.n_resamples)


@dataclass(frozen=True, slots=True)
class CandidateVerdict:
    """Per-candidate outcome: passed (with a composite score) or rejected at a stage."""

    candidate_id: str
    passed: bool
    rejected_at: FilterStage | None
    detail: str
    composite_score: float | None = None
    #: ``True`` iff this is a PASS whose DSR filter #2 deflated against the
    #: legacy entering-POOL fallback (``dsr_n_source == "pool"`` — an
    #: anti-conservative under-count of the true leaderboard N), so a
    #: pool-sourced pass can never silently read as proven
    #: (DEC-STATS-GATE-INTEGRITY-001, R2-SR-F4). PASS-scoped only: a rejected
    #: verdict stays ``False`` — a pool-N *rejection* is a-fortiori safe under
    #: the true larger N (DSR is monotonically decreasing in N). HONEST
    #: envelope: the flag covers ONLY the pool-N under-count; later filters
    #: keep their own skip states (e.g. ``pool_pbo=None``), so
    #: ``provisional=False`` is not a blanket "fully proven" claim.
    provisional: bool = False
    #: The candidate's MEASURED CPCV pass rate — the raw full-precision
    #: fraction of purged combinatorial train/test folds with positive OOS
    #: Sharpe, captured at filter #3 the moment :func:`cpcv_pass_rate`
    #: returns and BEFORE the threshold compare
    #: (DEC-RESULTS-CASCADE-VERDICT-JOIN-001 — the value was previously
    #: computed and DISCARDED, surviving only as 4-dp prose in ``detail`` on
    #: a threshold fail). Semantics: ``None`` for a candidate that never
    #: reached filter #3 (hard-gate / DSR rejection) and for the CPCV
    #: ``ValueError`` arm (the rate was never produced); the FAILING rate
    #: for a CPCV threshold rejection; the stage-3 rate for every
    #: later-stage rejection and every pass. RECORDING-ONLY: no ordering /
    #: RNG / threshold / statistic changes — the field is stamped by ONE
    #: final :func:`dataclasses.replace` pass over the already-decided
    #: verdicts. Appended + defaulted (the ``provisional`` precedent), so
    #: every existing construction is unaffected. NEVER rounded (CLAUDE.md
    #: §4 — display projections mint their own ``rate * 100.0`` alias).
    cpcv_pass_rate: float | None = None
    #: The cascade's OWN filter-#2 Deflated Sharpe for this candidate
    #: (DEC-RESULTS-TRUST-HONESTY-001, audit BLOCKER 5): the value was
    #: previously computed per candidate then DISCARDED on the pass path —
    #: only a reject's ``detail`` string carried it. Populated for EVERY
    #: candidate whose DSR evaluation succeeded (a pass, a filter-#2
    #: threshold reject, AND a later-stage reject alike); ``None`` when the
    #: candidate never reached filter #2 (hard-gate reject) or the
    #: evaluation itself raised. Deflated against the report's run-level
    #: ``dsr_n_trials`` under ``dsr_n_source`` (``"leaderboard"`` = the
    #: corrected search-N; ``"pool"`` = the legacy under-count that also
    #: sets :attr:`provisional`). Full precision, NEVER rounded
    #: (DEC-STAT-DSR-PBO-001). ADDITIVE with a ``None`` default so
    #: pre-change constructions/deserialisations load ``None``, never a
    #: fabricated 0.
    dsr_corrected: float | None = None
    #: The candidate's MEASURED per-candidate CSCV PBO over its OWN Stage-3
    #: search family (DEC-CASCADE-STAT-TIER-WIRE-001) — the raw full-precision
    #: probability from filter #5's per-candidate check, captured the moment
    #: :func:`~brutex.metrics.pbo.pbo_probability` returns and BEFORE the
    #: threshold compare, so a pass AND a threshold rejection both carry the
    #: real number. ``None`` = the check never produced a value: the candidate
    #: never REACHED the per-candidate check (an earlier rejection, or the
    #: whole pool died at the pool-PBO half of #5) OR it reached it and was
    #: SKIPPED (``family_returns`` absent / fewer than 2 columns / a series
    #: shorter than one split block). RECORDING follows the
    #: :attr:`cpcv_pass_rate` precedent exactly — stamped by the ONE final
    #: :func:`dataclasses.replace` pass over the already-decided verdicts;
    #: appended + defaulted; NEVER rounded (CLAUDE.md §4).
    candidate_pbo: float | None = None
    #: The extreme-vol stratum status token (filter #10,
    #: DEC-REGIME-MUST-SURVIVE-STRATUM-001):
    #: ``"ok"`` / ``"fail"`` / ``"unproven_current_regime"`` /
    #: ``"insufficient_stratum_trades"``, or ``None`` when the filter never
    #: evaluated this candidate (flag OFF, or the candidate died at an
    #: earlier stage — scope is deliberately survivors-only). Appended +
    #: defaulted (the ``cpcv_pass_rate`` precedent) so every existing
    #: construction is byte-identical. An ABSTAIN status is RECORDED but
    #: never a rejection (coordinator-confirmed semantics).
    extreme_vol_status: str | None = None
    #: The measured stratum numbers behind ``extreme_vol_status`` (trade
    #: counts / expectancies / variances / ratio — full precision, never
    #: rounded), or ``None`` whenever the status is ``None``.
    extreme_vol_stats: dict[str, float | int | None] | None = None
    #: The regime-fragility status token (filter #11,
    #: DEC-REGIME-FRAGILITY-GATE-001): ``"ok"`` / ``"fail"`` /
    #: ``"insufficient_strata"``, or ``None`` when the filter never
    #: evaluated this candidate (flag OFF, or the candidate died at an
    #: earlier stage — scope is deliberately survivors-only). Appended +
    #: defaulted (the ``extreme_vol_status`` precedent) so every existing
    #: construction is byte-identical. An ABSTAIN status is RECORDED but
    #: never a rejection.
    regime_fragility_status: str | None = None
    #: The measured per-axis per-stratum numbers behind
    #: ``regime_fragility_status`` (trade counts / expectancies /
    #: positive-P&L shares + ``taxonomy_version`` — full precision, never
    #: rounded), or ``None`` whenever the status is ``None`` OR the
    #: evidence was absent (the visible evidence-None abstain).
    regime_fragility_stats: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class DsrTrialStats:
    """Externally-supplied leaderboard deflation inputs for filter #2 (ADR-0008).

    The FROZEN per-(instrument, tf) trial population the cascade deflates
    against when the launcher supplies it (DEC-TRIAL-STATS-CONSUME-001):
    ``n_trials`` = every backtested combo + every unique Stage-3 refined row
    of the leaderboard (the spec's per-leaderboard N), and
    ``sharpe_variance`` = the population variance of the finite trial Sharpes
    (the same population — the Bailey-LdP requirement). Built by
    ``brutex.engine.cascade_wire`` from the manifest-frozen records; this
    module stays engine-agnostic.

    Raises
    ------
    ValueError
        If ``n_trials < 1``, ``n_finite`` is negative or exceeds ``n_trials``,
        or ``sharpe_variance`` is negative/non-finite.
    """

    n_trials: int
    sharpe_variance: float
    n_finite: int

    def __post_init__(self) -> None:
        """Validate the deflation inputs at construction time."""
        if self.n_trials < 1:
            raise ValueError(f"DsrTrialStats.n_trials must be >= 1, got {self.n_trials}")
        if not 0 <= self.n_finite <= self.n_trials:
            raise ValueError(
                f"DsrTrialStats.n_finite must be in [0, n_trials], got {self.n_finite}"
            )
        if not math.isfinite(self.sharpe_variance) or self.sharpe_variance < 0.0:
            raise ValueError(
                f"DsrTrialStats.sharpe_variance must be finite and >= 0, "
                f"got {self.sharpe_variance!r}"
            )


@dataclass(frozen=True, slots=True)
class CascadeReport:
    """The full cascade outcome."""

    verdicts: tuple[CandidateVerdict, ...]
    survivor_ids: tuple[str, ...]  # passed candidates, ranked best-first by composite
    n_input: int
    n_survived: int
    stage_survivors: dict[str, int]  # FilterStage.value -> count still alive after it
    #: The MEASURED CSCV pool PBO from filter #5 (DEC-PBO-SIDECAR-001) — the raw
    #: probability, full precision, NEVER rounded (DEC-STAT-DSR-PBO-001). This is
    #: the COMBINED-DIRECTION pool statistic for the whole pooled slice (the wire
    #: pools bull AND bear specs of one ``(instrument, tf)`` together), NOT a
    #: per-direction or per-candidate number. ``None`` iff the filter never ran,
    #: which has exactly THREE causes: (a) the pool was already empty when #5 was
    #: reached (everything died at an earlier filter); (b) a lone survivor
    #: (CSCV needs >= 2 candidates); (c) the return series is shorter than one
    #: split block (``usable < pbo_n_splits``).
    pool_pbo: float | None = None
    #: The MEASURED pool Reality-Check p-value from filter #4
    #: (DEC-CASCADE-STAT-TIER-WIRE-001, the pool_pbo/DEC-PBO-SIDECAR-001
    #: precedent) — the raw p-value, full precision, NEVER rounded. Recorded
    #: whenever White's RC ran on a non-empty pool — pass AND reject alike.
    #: ``None`` iff the RC never ran (the pool was already empty when #4 was
    #: reached, or every candidate was degenerate-rejected before the pool
    #: matrix was built).
    pool_rc_pvalue: float | None = None
    #: The MEASURED pool Hansen-SPA CONSISTENT p-value from filter #4's SPA
    #: arm (DEC-CASCADE-STAT-TIER-WIRE-001). Recorded only when the SPA arm
    #: actually ran — i.e. the RC floor PASSED first (the join is
    #: monotone-tightening: RC-first with the byte-identical historical
    #: detail, then Hansen's studentized null-recentred consistent test as
    #: the primary). ``None`` when the RC already killed the pool or #4 was
    #: never reached with survivors — DISTINGUISHABLE from a measured value
    #: by construction: ``pool_spa_pvalue is None and pool_rc_pvalue is not
    #: None`` reads "RC rejected the pool", both ``None`` reads "the pool
    #: never reached the data-snooping test".
    pool_spa_pvalue: float | None = None
    #: Which trial population filter #2 deflated against
    #: (DEC-TRIAL-STATS-CONSUME-001): ``"leaderboard"`` — the frozen
    #: per-(instrument, tf) trial stats supplied by the wire (the honest,
    #: spec-mandated N); ``"pool"`` — the legacy entering-pool fallback (an
    #: anti-conservative under-count, stamped so it can never masquerade as
    #: the corrected figure). Reports serialized before this field existed
    #: simply lack the key (the three-state additive precedent).
    dsr_n_source: str = "pool"
    #: The N and V filter #2 actually used (echoed for transparency; full
    #: precision, never rounded — DEC-STAT-DSR-PBO-001).
    dsr_n_trials: int | None = None
    dsr_trial_variance: float | None = None

    @property
    def rejected_ids(self) -> tuple[str, ...]:
        """Candidate ids that did not survive, in input order."""
        return tuple(v.candidate_id for v in self.verdicts if not v.passed)

    @property
    def provisional_pass_ids(self) -> tuple[str, ...]:
        """Survivor ids whose pass is PROVISIONAL (pool-N DSR), in survivor order.

        The ``survivor_ids``-ordered subsequence of passes flagged
        :attr:`CandidateVerdict.provisional` (DEC-STATS-GATE-INTEGRITY-001,
        R2-SR-F4). Because ``dsr_n_source`` is a RUN-level stamp (computed
        once before the candidate loop), this is by construction
        ``survivor_ids`` itself when the source is ``"pool"`` and ``()``
        when it is ``"leaderboard"`` — the per-verdict stamp's value is that
        a verdict dict extracted from a serialized report stays
        self-describing.
        """
        by_id = {v.candidate_id: v for v in self.verdicts}
        return tuple(sid for sid in self.survivor_ids if by_id[sid].provisional)


def _hard_gate_failure(c: CandidateInput, cfg: CascadeConfig) -> str | None:
    """Return the first failing hard-rejection-gate reason, or ``None`` if all pass.

    Mirrors ``TRACK1_BACKTEST_SPEC.md`` lines 595-600 (the five scalar pre-gates;
    the sixth lockbox-ratio gate is subsumed by the stricter Lockbox filter #8 —
    see :class:`CascadeConfig`).
    """
    if c.total_trades < cfg.min_total_trades:
        return f"total_trades {c.total_trades} < {cfg.min_total_trades}"
    if c.profit_factor < cfg.min_profit_factor:
        return f"profit_factor {c.profit_factor} < {cfg.min_profit_factor}"
    if c.max_drawdown_pct > cfg.max_drawdown_pct:
        return f"max_drawdown_pct {c.max_drawdown_pct} > {cfg.max_drawdown_pct}"
    if c.win_rate < cfg.min_win_rate and c.win_loss_ratio < cfg.min_win_loss_ratio:
        return (
            f"win_rate {c.win_rate} < {cfg.min_win_rate} AND "
            f"win_loss_ratio {c.win_loss_ratio} < {cfg.min_win_loss_ratio}"
        )
    if c.max_consecutive_losses > cfg.max_consecutive_losses:
        return f"max_consecutive_losses {c.max_consecutive_losses} > {cfg.max_consecutive_losses}"
    return None


def _pool_usable(c: CandidateInput) -> bool:
    """Whether a candidate's returns can join the pool studentized tests.

    The Romano-Wolf statistic ``sqrt(n)·mean / omega`` is undefined for a
    constant column and the bootstrap needs ``>= 2`` observations, so a
    candidate is pool-usable iff its returns have ``>= _MIN_FOR_DISPERSION``
    observations *and* positive variance (mirrors
    :func:`brutex.metrics.spa._require_positive_variance`). Degenerate
    candidates are rejected at the SPA stage rather than crashing the pool.
    """
    arr = np.asarray(c.returns, dtype=np.float64)
    return arr.size >= _MIN_FOR_DISPERSION and bool(arr.var() > 0.0)


def _pool_matrix(survivors: list[CandidateInput]) -> npt.NDArray[np.float64]:
    """Stack the survivors' equal-length return series into an ``(n_obs, n)`` matrix.

    Raises
    ------
    ValueError
        If the survivors' ``returns`` are not all the same (>= 1) length — the
        pool filters require a common time axis.
    """
    lengths = {len(c.returns) for c in survivors}
    if len(lengths) != 1 or next(iter(lengths)) < 1:
        raise ValueError(
            f"pool-level filters require all candidates to share one returns length >= 1; got {sorted(lengths)}"
        )
    return np.column_stack([np.asarray(c.returns, dtype=np.float64) for c in survivors])


def _zscore_clamped(values: list[float]) -> list[float]:
    """Population z-scores clamped to ``[-_Z_CLAMP, +_Z_CLAMP]`` (spec line 618)."""
    if len(values) < _MIN_FOR_DISPERSION:
        return [0.0] * len(values)
    mean = statistics.fmean(values)
    std = statistics.pstdev(values, mu=mean)
    if std <= 0.0:
        return [0.0] * len(values)
    return [max(-_Z_CLAMP, min(_Z_CLAMP, (v - mean) / std)) for v in values]


def _measured_lockbox_sharpe(lb: LockboxResult) -> float:
    """The MEASURED lockbox Sharpe of a PASSING filter-#8 result, as a float.

    The composite's ``lockbox_sharpe`` term (spec lines 688-697) reads the
    measured out-of-sample value (DEC-STATS-GATE-INTEGRITY-001, R2-SR-F5).
    A ``lb.passed`` result always carries a non-``None`` Sharpe by
    construction (:func:`lockbox_evaluation`'s ``sharpe_gate`` requires it),
    but :class:`~brutex.metrics.lockbox.LockboxResult` is a plain frozen
    dataclass with no cross-field validation, so a hand-built inconsistent
    result is rejected LOUDLY here rather than crashing the z-score.
    """
    if lb.lockbox_sharpe is None:
        raise ValueError("a passing LockboxResult must carry a measured lockbox_sharpe")
    return lb.lockbox_sharpe


def _composite_scores(
    survivors: list[CandidateInput], lockbox_sharpe_by_id: Mapping[str, float]
) -> dict[str, float]:
    """Per-candidate composite score (spec lines 686-697): weighted clamped z-scores.

    Note: the spec's ``normalize(x)`` is a per-(instrument, TF) *leaderboard*
    z-score; here it is taken over the **surviving pool** (a deliberate
    consequence of the pure-pool design — the orchestrator only sees the
    injected survivors, not the full leaderboard), so scores rank survivors
    relative to each other. The ``lockbox_sharpe`` term reads the MEASURED
    per-candidate lockbox Sharpe retained from filter #8
    (*lockbox_sharpe_by_id*, a SUPERSET of the survivors — a #8-passer later
    rejected at #9 has an unused entry), never the training reference
    (DEC-STATS-GATE-INTEGRITY-001, R2-SR-F5 — spec conformance,
    ``TRACK1_BACKTEST_SPEC.md`` composite formula).
    """
    raw: dict[str, list[float]] = {
        "sharpe_ratio": [c.sharpe_ratio for c in survivors],
        "sortino_ratio": [c.sortino_ratio for c in survivors],
        "calmar_ratio": [c.calmar_ratio for c in survivors],
        "profit_factor_excess": [c.profit_factor - 1.0 for c in survivors],
        "recovery_factor": [c.recovery_factor for c in survivors],
        "win_quality": [c.win_rate * c.win_loss_ratio for c in survivors],
        "lockbox_sharpe": [lockbox_sharpe_by_id[c.candidate_id] for c in survivors],
    }
    z = {k: _zscore_clamped(v) for k, v in raw.items()}
    scores: dict[str, float] = {}
    for i, c in enumerate(survivors):
        scores[c.candidate_id] = sum(_COMPOSITE_WEIGHTS[k] * z[k][i] for k in _COMPOSITE_WEIGHTS)
    return scores


def run_stage4_cascade(
    candidates: Sequence[CandidateInput],
    config: CascadeConfig,
    *,
    trial_stats: DsrTrialStats | None = None,
    embargo_bars: int | None = None,
) -> CascadeReport:
    """Run the full Stage-4 cascade over a candidate pool.

    Applies the hard rejection gates, then the nine statistical filters in
    spec order with short-circuit, then ranks survivors by composite score.
    Degenerate per-candidate inputs are isolated as rejections (see the module
    "Robustness" section), so one bad candidate never aborts the sweep.

    Parameters
    ----------
    candidates
        The candidate pool (each carrying its filter inputs). Must have unique
        ``candidate_id``\\s.
    config
        The :class:`CascadeConfig` (thresholds + mandatory seed).
    trial_stats
        Optional FROZEN leaderboard deflation inputs (ADR-0008 /
        DEC-TRIAL-STATS-CONSUME-001): when supplied, filter #2 deflates
        against ``trial_stats.n_trials`` / ``trial_stats.sharpe_variance``
        instead of the entering pool's own count + dispersion; the report's
        ``dsr_n_source`` records which source was used either way.
    embargo_bars
        The resolved Stage-4 embargo in decision-TF bars
        (DEC-CASCADE-STAT-TIER-WIRE-001, the DEC-STAT-001 law —
        ``docs/MIGRATION.md`` #123): the production wire resolves it per
        (instrument, tf) group via
        :func:`brutex.engine.cascade_wire.resolve_cascade_embargo_bars`.
        ``None`` (the default) → ``0`` — full back-compat for direct
        callers. Threaded into BOTH embargo-aware engines: filter #3's
        :func:`~brutex.metrics.cpcv.cpcv_pass_rate` (where it is validated
        but PROVABLY rate-inert — the model-free metric evaluates test
        folds only; see that docstring) and filter #7's
        :func:`~brutex.metrics.wfo.wfo_pass_rate` (where it genuinely
        shifts every walk's test window ``embargo`` bars past the training
        stop — the REAL serial-correlation de-leakage). A series too short
        to fit one embargoed walk takes the EXISTING WFO short-series
        degrade arm (the engine's "yields no walks" ``ValueError`` becomes
        a rejection at the walk-forward stage, never a raise).

    Returns
    -------
    CascadeReport
        Per-candidate verdicts, the ranked survivor ids, the per-stage
        survivor counts (audit trail), and the DSR deflation-source stamp.

    Raises
    ------
    ValueError
        If two candidates share a ``candidate_id``, two *pool-surviving*
        candidates have unequal-length return series (the pool filters need a
        common time axis), or ``embargo_bars`` is negative. Per-candidate
        engine domain errors do **not** raise — they become rejections at the
        relevant stage.
    """
    resolved_embargo = 0 if embargo_bars is None else embargo_bars
    if resolved_embargo < 0:
        raise ValueError(f"embargo_bars must be >= 0 or None, got {embargo_bars}")
    pool = list(candidates)
    ids = [c.candidate_id for c in pool]
    if len(set(ids)) != len(ids):
        raise ValueError("candidate_id values must be unique")

    verdicts: dict[str, CandidateVerdict] = {}
    stage_survivors: dict[str, int] = {}
    # DEC-RESULTS-TRUST-HONESTY-001 (audit BLOCKER 5): every successfully
    # evaluated filter-#2 DSR is RETAINED per candidate so the verdict carries
    # it on the pass path too (it was previously discarded there — only the
    # reject detail string kept the figure). Absent id ⇒ honest None (hard-gate
    # reject / the DSR evaluation itself raised), never a fabricated 0.
    dsr_by_id: dict[str, float] = {}

    def reject(c: CandidateInput, stage: FilterStage, detail: str) -> None:
        verdicts[c.candidate_id] = CandidateVerdict(
            candidate_id=c.candidate_id,
            passed=False,
            rejected_at=stage,
            detail=detail,
            dsr_corrected=dsr_by_id.get(c.candidate_id),
        )

    survivors = pool
    boot = config.bootstrap()

    # ── HARD GATES (scalar, spec 595-600) ──
    if config.run_hard_gates:
        kept: list[CandidateInput] = []
        for c in survivors:
            reason = _hard_gate_failure(c, config)
            if reason is None:
                kept.append(c)
            else:
                reject(c, FilterStage.HARD_GATES, reason)
        survivors = kept
    stage_survivors[FilterStage.HARD_GATES.value] = len(survivors)

    # ── #1b SINGLE-EVENT SENSITIVITY (per-candidate, DEFAULT-OFF) ──
    # DEC-SWEEP-SINGLE-EVENT-SENSITIVITY-GATE-001: runs ONLY when the knob is
    # set — the None default leaves this block fully absent from behavior
    # (stage_survivors carries no token, reports byte-identical). Additional
    # defense-in-depth; DSR (#2) stays the load-bearing anti-single-event
    # filter.
    if config.single_event_max_best_day_pct is not None:
        x_pct = config.single_event_max_best_day_pct
        kept = []
        for c in survivors:
            detail = _single_event_verdict(c, x_pct)
            if detail is None:
                kept.append(c)
            else:
                reject(c, FilterStage.SINGLE_EVENT_SENSITIVITY, detail)
        survivors = kept
        stage_survivors[FilterStage.SINGLE_EVENT_SENSITIVITY.value] = len(survivors)

    # ── #2 DEFLATED SHARPE (per-candidate). The deflation inputs come from ONE
    # of two same-population sources (ADR-0008 condition 3 / DEC-TRIAL-STATS-
    # CONSUME-001): the FROZEN leaderboard trial stats when supplied — N = every
    # backtested combo + every unique Stage-3 refined row, V = the variance of
    # that population's finite Sharpes (the spec's per-leaderboard N,
    # `TRACK1_BACKTEST_SPEC.md` §Stage 4) — or, as the LOUDLY-STAMPED legacy
    # fallback, the *entering* pool's own count + Sharpe dispersion (an
    # anti-conservative under-count, kept only for old trees/manifests). ──
    if trial_stats is not None:
        n_trials = trial_stats.n_trials
        trial_var = trial_stats.sharpe_variance
        dsr_n_source = "leaderboard"
    else:
        pool_sharpes = [c.sharpe_ratio for c in pool]
        n_trials = len(pool)
        trial_var = (
            statistics.pvariance(pool_sharpes) if len(pool_sharpes) >= _MIN_FOR_DISPERSION else 0.0
        )
        dsr_n_source = "pool"
    kept = []
    for c in survivors:
        try:
            dsr = deflated_sharpe_ratio(
                c.sharpe_ratio,
                n_trials=n_trials,
                trial_sharpe_variance=trial_var,
                sample_length=max(c.total_trades, 2),
                skew=c.skew,
                kurtosis=c.kurtosis,
            )
        except ValueError as exc:
            reject(c, FilterStage.DEFLATED_SHARPE, f"DSR could not be evaluated: {exc}")
            continue
        # Retained BEFORE the threshold branch so pass AND reject verdicts
        # alike carry the evaluated figure (DEC-RESULTS-TRUST-HONESTY-001).
        dsr_by_id[c.candidate_id] = dsr.deflated_sharpe_ratio
        if dsr.deflated_sharpe_ratio < config.dsr_min:
            reject(
                c,
                FilterStage.DEFLATED_SHARPE,
                f"DSR {dsr.deflated_sharpe_ratio:.4f} < {config.dsr_min}",
            )
        else:
            kept.append(c)
    survivors = kept
    stage_survivors[FilterStage.DEFLATED_SHARPE.value] = len(survivors)

    # ── #3 CPCV (per-candidate) ──
    # DEC-RESULTS-CASCADE-VERDICT-JOIN-001: record every successfully-computed
    # rate BEFORE the threshold compare, so a pass AND a threshold fail both
    # carry the real number (the ValueError arm never produced one → absent).
    # Pure recording — the decision logic below is byte-untouched.
    cpcv_rate_by_id: dict[str, float] = {}
    kept = []
    for c in survivors:
        try:
            # DEC-CASCADE-STAT-TIER-WIRE-001: the resolved embargo is threaded
            # (label_span stays 0 — per-BAR equity returns have a 1-bar label
            # horizon, no forward window). HONEST: cpcv_pass_rate is model-free
            # (test folds only), so the rate is UNCHANGED by construction for
            # any valid embargo — the threading is declaration + validation,
            # never a tightening illusion (pinned in tests/unit/test_cpcv.py).
            rate = cpcv_pass_rate(
                c.returns,
                n_groups=config.cpcv_n_groups,
                n_test_groups=config.cpcv_n_test_groups,
                label_span=0,
                embargo=resolved_embargo,
            )
        except ValueError as exc:
            reject(c, FilterStage.CPCV, f"CPCV could not be evaluated: {exc}")
            continue
        cpcv_rate_by_id[c.candidate_id] = rate
        if rate < config.cpcv_min_pass_rate:
            reject(c, FilterStage.CPCV, f"CPCV pass-rate {rate:.4f} < {config.cpcv_min_pass_rate}")
        else:
            kept.append(c)
    survivors = kept
    stage_survivors[FilterStage.CPCV.value] = len(survivors)

    # ── #4 SPA / Reality Check (POOL: best-of-N must be real or the pool is noise) ──
    # First isolate degenerate candidates (constant / < 2-obs returns): they
    # cannot enter the studentized pool tests (Romano-Wolf at #6 would raise),
    # so reject them here. This guarantees a valid pool matrix for SPA and RW.
    usable_pool: list[CandidateInput] = []
    for c in survivors:
        if _pool_usable(c):
            usable_pool.append(c)
        else:
            reject(
                c,
                FilterStage.REALITY_CHECK_SPA,
                "degenerate returns (zero variance or < 2 observations) — "
                "cannot run the pool data-snooping test",
            )
    survivors = usable_pool
    # DEC-CASCADE-STAT-TIER-WIRE-001: filter #4 is a monotone-tightening JOIN,
    # RC-first. Hansen's (2005) SPA-consistent is the PRIMARY test — the
    # studentized, null-recentred refinement (White's RC is conservative and
    # distortable by poor/irrelevant alternatives in the pool) — while White's
    # RC is RETAINED as the historical floor: a pool that failed the shipped
    # cascade still fails HERE with the byte-identical detail, and the SPA arm
    # can only ADD rejections (intersection composition keeps the false-pass
    # probability <= spa_alpha). The `_pool_usable` degenerate pre-rejection
    # above guarantees every column has >= 2 observations AND positive
    # variance — exactly hansen_spa's `_require_positive_variance` demand —
    # so the SPA arm can never raise on the pool matrix. Both tests consume
    # the SAME deterministic BootstrapConfig (one seed). The OTHER hansen_spa
    # raise — the bootstrap-variance-collapse ValueError inside
    # spa._studentization — is likewise unreachable here:
    # ``CascadeConfig.bootstrap()`` always leaves ``block_length=None``, so
    # the resampler derives ``expected_block_length(n_obs)`` — never a
    # pathological FIXED block long enough to reproduce the full sample
    # (hostile-review round 1).
    pool_rc_pvalue: float | None = None
    pool_spa_pvalue: float | None = None
    if survivors:
        matrix = _pool_matrix(survivors)
        rc = reality_check(matrix, boot)
        pool_rc_pvalue = rc.pvalue
        if rc.pvalue >= config.spa_alpha:
            for c in survivors:
                reject(
                    c,
                    FilterStage.REALITY_CHECK_SPA,
                    f"pool Reality-Check p {rc.pvalue:.4f} >= {config.spa_alpha}",
                )
            survivors = []
        else:
            spa = hansen_spa(matrix, boot)
            pool_spa_pvalue = spa.pvalue_consistent
            if spa.pvalue_consistent >= config.spa_alpha:
                for c in survivors:
                    reject(
                        c,
                        FilterStage.REALITY_CHECK_SPA,
                        f"pool Hansen-SPA consistent p {spa.pvalue_consistent:.4f} "
                        f">= {config.spa_alpha}",
                    )
                survivors = []
    stage_survivors[FilterStage.REALITY_CHECK_SPA.value] = len(survivors)

    # ── #5 PBO (POOL: overfit pool is rejected wholesale) ──
    # CSCV ranks in-sample vs out-of-sample ACROSS the pool, so it is only
    # defined for >= 2 surviving candidates; a lone survivor is skipped (it
    # cannot be "overfit relative to a pool of one"). CSCV also needs the row
    # count to be a whole multiple of n_splits, so we use the most-recent
    # ``(n_obs // n_splits) * n_splits`` rows (trim the oldest); a series
    # shorter than one split-block is skipped.
    n_obs_pbo = len(survivors[0].returns) if survivors else 0
    usable = (n_obs_pbo // config.pbo_n_splits) * config.pbo_n_splits
    # DEC-PBO-SIDECAR-001: the MEASURED pool PBO is recorded on the report
    # whether the filter passes or rejects — it was previously dropped on the
    # pass path, leaving the cascade's headline overfitting statistic invisible.
    pool_pbo: float | None = None
    if len(survivors) >= _MIN_POOL_FOR_PBO and usable >= config.pbo_n_splits:
        result = pbo_probability(
            [
                [float(c.returns[t]) for c in survivors]
                for t in range(n_obs_pbo - usable, n_obs_pbo)
            ],
            n_splits=config.pbo_n_splits,
        )
        pool_pbo = result.pbo
        if result.pbo > config.pbo_max:
            for c in survivors:
                reject(c, FilterStage.PBO, f"pool PBO {result.pbo:.4f} > {config.pbo_max}")
            survivors = []
    # DEC-CASCADE-STAT-TIER-WIRE-001: AFTER the (unchanged) pool law, the
    # PER-CANDIDATE CSCV — Bailey's PBO applied to each survivor's OWN Stage-3
    # search family (the (sl, tp) configurations its mask was actually TRIED
    # at; the statistically intended CSCV use). A candidate with no family
    # (``family_returns`` None), fewer than 2 columns, or a series shorter
    # than one split block is SKIPPED (no reject, value ``None`` — honest
    # abstention, never a fabricated verdict). The SAME ``pbo_max`` knob
    # governs both halves (one overfitting ceiling — a deliberate,
    # documented choice). The measured value is recorded pass AND reject
    # (the cpcv_pass_rate capture precedent) and stamped by the ONE final
    # ``replace`` pass below.
    candidate_pbo_by_id: dict[str, float] = {}
    kept = []
    for c in survivors:
        family = c.family_returns
        if family is None or len(family) < _MIN_POOL_FOR_PBO:
            kept.append(c)
            continue
        n_obs_family = len(c.returns)
        usable_family = (n_obs_family // config.pbo_n_splits) * config.pbo_n_splits
        if usable_family < config.pbo_n_splits:
            kept.append(c)
            continue
        family_result = pbo_probability(
            [
                [float(column[t]) for column in family]
                for t in range(n_obs_family - usable_family, n_obs_family)
            ],
            n_splits=config.pbo_n_splits,
        )
        candidate_pbo_by_id[c.candidate_id] = family_result.pbo
        if family_result.pbo > config.pbo_max:
            reject(c, FilterStage.PBO, f"candidate PBO {family_result.pbo:.4f} > {config.pbo_max}")
        else:
            kept.append(c)
    survivors = kept
    stage_survivors[FilterStage.PBO.value] = len(survivors)

    # ── #6 Romano-Wolf (POOL: keep only the FWER rejection set) ──
    # Every survivor here is pool-usable (positive variance, >= 2 obs) — the SPA
    # degenerate filter above guarantees it and the pool only shrinks — so the
    # studentized RW statistic is always well-defined (no zero-variance column).
    if survivors:
        rw = romano_wolf(_pool_matrix(survivors), boot, alpha=config.rw_alpha)
        real = set(rw.rejected_indices)
        kept = []
        for i, c in enumerate(survivors):
            if i in real:
                kept.append(c)
            else:
                reject(
                    c,
                    FilterStage.ROMANO_WOLF,
                    f"not in RW rejection set (adj_p {rw.adjusted_pvalues[i]:.4f})",
                )
        survivors = kept
    stage_survivors[FilterStage.ROMANO_WOLF.value] = len(survivors)

    # ── #7 Walk-Forward (per-candidate; ALL THREE schemes must pass, spec 429) ──
    kept = []
    for c in survivors:
        failed_scheme: str | None = None
        try:
            for scheme in WFOScheme:
                # DEC-CASCADE-STAT-TIER-WIRE-001: the resolved embargo GAP
                # between each walk's training stop and its test start — the
                # genuine serial-correlation de-leakage (unlike CPCV's inert
                # threading above). A series the embargo leaves walk-less
                # raises the engine's "yields no walks" ValueError → the
                # EXISTING short-series arm below rejects at this stage.
                rate = wfo_pass_rate(
                    c.returns,
                    scheme,
                    train_size=config.wfo_train_size,
                    test_size=config.wfo_test_size,
                    step=config.wfo_step,
                    embargo=resolved_embargo,
                )
                if rate < config.wfo_min_pass_rate:
                    failed_scheme = (
                        f"{scheme.value} pass-rate {rate:.4f} < {config.wfo_min_pass_rate}"
                    )
                    break
        except ValueError as exc:
            reject(c, FilterStage.WALK_FORWARD, f"WFO could not be evaluated: {exc}")
            continue
        if failed_scheme is None:
            kept.append(c)
        else:
            reject(c, FilterStage.WALK_FORWARD, failed_scheme)
    survivors = kept
    stage_survivors[FilterStage.WALK_FORWARD.value] = len(survivors)

    # ── #8 Lockbox (per-candidate). ``evidence_days`` carries the TRUE
    # per-trade exit-day ordinals when the caller supplied them
    # (DEC-STATS-GATE-INTEGRITY-001, R2-SR-F2) so the 500-trade floor counts
    # trades, not bars; the MEASURED lockbox Sharpe of each passer is retained
    # for the composite's lockbox term (R2-SR-F5). ──
    lockbox_sharpe_by_id: dict[str, float] = {}
    kept = []
    for c in survivors:
        try:
            lb = lockbox_evaluation(
                c.lockbox_returns,
                c.lockbox_days,
                training_sharpe=c.training_sharpe,
                training_max_drawdown=c.training_max_drawdown,
                min_trades=config.lockbox_min_trades,
                default_window_days=config.lockbox_default_window_days,
                evidence_days=c.lockbox_trade_days,
            )
        except ValueError as exc:
            reject(c, FilterStage.LOCKBOX, f"lockbox could not be evaluated: {exc}")
            continue
        if lb.passed:
            kept.append(c)
            lockbox_sharpe_by_id[c.candidate_id] = _measured_lockbox_sharpe(lb)
        else:
            reject(c, FilterStage.LOCKBOX, f"lockbox {lb.status.value}")
    survivors = kept
    stage_survivors[FilterStage.LOCKBOX.value] = len(survivors)

    # ── #9 Monte-Carlo (per-candidate) ──
    kept = []
    for c in survivors:
        try:
            mc = monte_carlo_permutation_test(
                c.sharpe_ratio,
                c.mc_n_bars,
                c.mc_n_entries,
                c.mc_evaluate,
                seed=config.seed,
                n_permutations=config.mc_n_permutations,
                alpha=config.mc_alpha,
            )
        except ValueError as exc:
            reject(c, FilterStage.MONTE_CARLO, f"Monte-Carlo could not be evaluated: {exc}")
            continue
        if mc.passed:
            kept.append(c)
        else:
            reject(
                c, FilterStage.MONTE_CARLO, f"Monte-Carlo p {mc.pvalue:.4f} >= {config.mc_alpha}"
            )
    survivors = kept
    stage_survivors[FilterStage.MONTE_CARLO.value] = len(survivors)

    # ── #10 EXTREME-VOL MUST-SURVIVE STRATUM (per-candidate, DEFAULT-OFF —
    # DEC-REGIME-MUST-SURVIVE-STRATUM-001). Gated on the config flag: when
    # OFF (the default) this block does not execute AT ALL — no
    # stage_survivors key, no verdict fields, report byte-identical. Scope is
    # deliberately MINIMAL: only candidates surviving to #10 are evaluated
    # (an earlier-stage reject never gains a stratum status — it was already
    # decided). ABSTAIN statuses ("unproven_current_regime" /
    # "insufficient_stratum_trades") are RECORDED but NEVER reject — data
    # unavailability is visible honesty, not a failure. ──
    extreme_vol_status_by_id: dict[str, str] = {}
    extreme_vol_stats_by_id: dict[str, dict[str, float | int | None]] = {}
    if config.extreme_vol_stratum:
        kept = []
        for c in survivors:
            if c.extreme_vol_entry_flags is None or c.trade_pnls_paisa is None:
                # Data unavailable (an old manifest / a hand-built caller):
                # honest ABSTAIN — visible, never a reject (Golden Rule #1).
                extreme_vol_status_by_id[c.candidate_id] = "unproven_current_regime"
                kept.append(c)
                continue
            stratum_pnls: list[int] = []
            calm_pnls: list[int] = []
            for pnl, is_extreme in zip(c.trade_pnls_paisa, c.extreme_vol_entry_flags, strict=True):
                (stratum_pnls if is_extreme else calm_pnls).append(pnl)
            sv = evaluate_stratum(stratum_pnls, calm_pnls)
            extreme_vol_status_by_id[c.candidate_id] = sv.status
            extreme_vol_stats_by_id[c.candidate_id] = {
                "stratum_trades": sv.stratum_trades,
                "calm_trades": sv.calm_trades,
                "stratum_expectancy_paisa": sv.stratum_expectancy_paisa,
                "calm_expectancy_paisa": sv.calm_expectancy_paisa,
                "stratum_variance": sv.stratum_variance,
                "calm_variance": sv.calm_variance,
                "variance_ratio": sv.variance_ratio,
            }
            if sv.status == "fail":
                reject(c, FilterStage.EXTREME_VOL_STRATUM, sv.detail)
            else:
                kept.append(c)
        survivors = kept
        stage_survivors[FilterStage.EXTREME_VOL_STRATUM.value] = len(survivors)

    # ── #11 REGIME-FRAGILITY GATE (per-candidate, DEFAULT-OFF —
    # DEC-REGIME-FRAGILITY-GATE-001). Gated on the config flag: when OFF
    # (the default) this block does not execute AT ALL — no stage_survivors
    # key, no verdict fields, report byte-identical. Scope is deliberately
    # MINIMAL: only candidates surviving to #11 are evaluated (an
    # earlier-stage reject never gains a fragility status — it was already
    # decided). The ABSTAIN status ("insufficient_strata") is RECORDED but
    # NEVER rejects — data unavailability is visible honesty, not a
    # failure. ──
    regime_fragility_status_by_id: dict[str, str] = {}
    regime_fragility_stats_by_id: dict[str, dict[str, object]] = {}
    if config.regime_fragility:
        kept = []
        for c in survivors:
            if c.trade_pnls_paisa is None or (
                c.trade_trend_tokens is None and c.trade_era_labels is None
            ):
                # Evidence unavailable (an old manifest / a hand-built
                # caller): honest ABSTAIN — visible, never a reject
                # (Golden Rule #1).
                regime_fragility_status_by_id[c.candidate_id] = "insufficient_strata"
                kept.append(c)
                continue
            fv = evaluate_fragility(
                c.trade_pnls_paisa,
                trend_tokens=c.trade_trend_tokens,
                era_labels=c.trade_era_labels,
            )
            regime_fragility_status_by_id[c.candidate_id] = fv.status
            regime_fragility_stats_by_id[c.candidate_id] = fv.stats
            if fv.status == "fail":
                reject(c, FilterStage.REGIME_FRAGILITY, fv.detail)
            else:
                kept.append(c)
        survivors = kept
        stage_survivors[FilterStage.REGIME_FRAGILITY.value] = len(survivors)

    # ── Composite ranking of survivors (spec 686-697) ──
    scores = _composite_scores(survivors, lockbox_sharpe_by_id)
    # Deterministic TOTAL order (DEC-STATS-GATE-INTEGRITY-001 R2-SR-F6,
    # mirroring DEC-RANKING-TIEBREAK-001 in ``brutex.ranking``): primary key is
    # the composite score DESCENDING with the unique ``candidate_id`` ASCENDING
    # as the final tie-break, so tied scores can never leave the survivor order
    # input-dependent (``sorted`` is stable — a bare score key permuted with the
    # candidate enumeration on any tie). The key is always well-defined: every
    # composite is finite by construction (finite-validated inputs + the
    # ``[-3, +3]`` z-clamp + finite weights) and id uniqueness is enforced at
    # entry, so ``(-score, candidate_id)`` is a strict total order.
    ranked = sorted(survivors, key=lambda c: (-scores[c.candidate_id], c.candidate_id))
    for c in survivors:
        verdicts[c.candidate_id] = CandidateVerdict(
            candidate_id=c.candidate_id,
            passed=True,
            rejected_at=None,
            detail="survived all Stage-4 filters",
            composite_score=scores[c.candidate_id],
            # A pool-N-deflated pass is PROVISIONAL (R2-SR-F4): the legacy
            # entering-pool fallback under-counts the true leaderboard N.
            provisional=dsr_n_source == "pool",
            # Every survivor passed filter #2, so its evaluated DSR is always
            # present here (DEC-RESULTS-TRUST-HONESTY-001 — the previously
            # discarded pass-path value; .get keeps the honest-None contract).
            dsr_corrected=dsr_by_id.get(c.candidate_id),
        )

    # ONE final recording-only stamp (DEC-RESULTS-CASCADE-VERDICT-JOIN-001 +
    # DEC-CASCADE-STAT-TIER-WIRE-001): every verdict — pass, later-stage
    # reject, CPCV/PBO reject — gains its measured stage-3 rate + its measured
    # per-candidate PBO; a candidate that never produced either stamps the
    # honest ``None``. ``replace`` preserves every other field bit-for-bit;
    # ordering/decisions were finalised above.
    # The extreme-vol filter #10 fields ride the SAME recording-only stamp
    # (DEC-REGIME-MUST-SURVIVE-STRATUM-001): both default to ``None``, so a
    # flag-OFF run (empty maps) stamps every verdict with its own defaults —
    # provably byte-identical to the pre-filter report.
    # The regime-fragility filter #11 fields ride the SAME recording-only
    # stamp (DEC-REGIME-FRAGILITY-GATE-001): both default to ``None``, so a
    # flag-OFF run (empty maps) stamps every verdict with its own defaults —
    # provably byte-identical to the pre-filter report.
    ordered_verdicts = tuple(
        replace(
            verdicts[cid],
            cpcv_pass_rate=cpcv_rate_by_id.get(cid),
            candidate_pbo=candidate_pbo_by_id.get(cid),
            extreme_vol_status=extreme_vol_status_by_id.get(cid),
            extreme_vol_stats=extreme_vol_stats_by_id.get(cid),
            regime_fragility_status=regime_fragility_status_by_id.get(cid),
            regime_fragility_stats=regime_fragility_stats_by_id.get(cid),
        )
        for cid in ids
    )
    return CascadeReport(
        verdicts=ordered_verdicts,
        survivor_ids=tuple(c.candidate_id for c in ranked),
        n_input=len(pool),
        n_survived=len(ranked),
        stage_survivors=stage_survivors,
        pool_pbo=pool_pbo,
        pool_rc_pvalue=pool_rc_pvalue,
        pool_spa_pvalue=pool_spa_pvalue,
        dsr_n_source=dsr_n_source,
        dsr_n_trials=n_trials,
        dsr_trial_variance=trial_var,
    )
