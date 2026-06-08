"""Stage-2.5 SPRT early-stop — the PURE, deterministic decision core.

DEC-SPRT-EARLY-STOP-CORE-001 (the first increment of the ADR-0006 Layer-A
design, after DEC-SPRT-SPEC-RECONCILE-001 locked the intra-backtest algorithm).

This module is **inert**: nothing in the production sweep imports or calls it
yet. It is **opt-in, default-OFF** and is NOT wired into `run_persisted_sweep`
or `metrics_for_mask`. Wiring it (so a hopeless combo's bar-walk is aborted
early) remains BLOCKED on ADR-0006 items B2/B4/B5 (ranking-invariance proof,
`top_k`-tracked percentile, Rust production-path wiring + honest worst-case
SLA). Until then this is a tested building block that changes NOTHING.

The decision is the spec's literal rule (`TRACK1_BACKTEST_SPEC.md §"Stage 2.5"`,
now locked to the intra-backtest definition): after a combo has produced at
least ``min_trades`` closed trades, KILL it iff the **Gaussian** posterior
probability that its true Sharpe exceeds the ``p95_threshold`` benchmark is
below ``kill_posterior``. "Gaussian conjugate posterior" (spec) == the
Probabilistic Sharpe Ratio with normal skew/kurtosis, so we reuse
:func:`brutex.metrics.deflated_sharpe.probabilistic_sharpe_ratio` verbatim
(CLAUDE.md §11 COMMON — one posterior, no re-derivation).

Determinism (Golden Rule #4): :func:`sprt_decision` is a pure function of the
running statistic + the injected threshold — no I/O, no clock, no RNG, no
cross-combo state — so the same trade stream + same ``p95_threshold`` always
yields the same decision, independent of worker order.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Final

from brutex.metrics.deflated_sharpe import probabilistic_sharpe_ratio
from brutex.models import BruteXError

#: Minimum observations before a sample variance (hence a Sharpe) is defined.
_MIN_DEFINED_N: Final[int] = 2


class EarlyStopConfigError(BruteXError):
    """Raised when an :class:`SprtConfig` is constructed with invalid bounds."""


class Decision(Enum):
    """The per-trade SPRT verdict for an in-flight backtest."""

    CONTINUE = "continue"
    KILL = "kill"


@dataclass(frozen=True, slots=True)
class SprtConfig:
    """Wald-SPRT early-stop parameters (spec defaults α=0.05, β=0.10).

    Parameters
    ----------
    alpha, beta
        SPRT Type-I / Type-II error bounds, each in the open interval ``(0, 1)``.
    min_trades
        No KILL is even evaluated until the combo has produced at least this
        many closed trades (spec floor: 50). Must be ``>= 1``.
    kill_posterior
        KILL when ``Pr(true_sharpe > p95_threshold) < kill_posterior`` (spec:
        ``0.10``). Must be in ``(0, 1)``.
    """

    alpha: float = 0.05
    beta: float = 0.10
    min_trades: int = 50
    kill_posterior: float = 0.10

    def __post_init__(self) -> None:
        """Validate the bounds (fail-closed; an invalid config can never run)."""
        if not 0.0 < self.alpha < 1.0:
            raise EarlyStopConfigError(f"alpha must be in (0, 1), got {self.alpha}")
        if not 0.0 < self.beta < 1.0:
            raise EarlyStopConfigError(f"beta must be in (0, 1), got {self.beta}")
        if self.min_trades < 1:
            raise EarlyStopConfigError(f"min_trades must be >= 1, got {self.min_trades}")
        if not 0.0 < self.kill_posterior < 1.0:
            raise EarlyStopConfigError(
                f"kill_posterior must be in (0, 1), got {self.kill_posterior}"
            )


def wald_bounds(cfg: SprtConfig) -> tuple[float, float]:
    """Return the ``(lower, upper)`` Wald-SPRT log-likelihood-ratio bounds.

    From the classic Wald formulation (spec `:356` "document SPRT thresholds"):
    ``A = ln(β / (1 − α))`` (accept H0 / KILL) and ``B = ln((1 − β) / α)``
    (accept H1 / keep). For the spec defaults these are ≈ ``(−2.251, 2.890)``.
    The active kill criterion in :func:`sprt_decision` is the Gaussian PSR
    posterior; these bounds document the α/β envelope and back the future
    LLR-proper Wald variant (ADR-0006).
    """
    lower = math.log(cfg.beta / (1.0 - cfg.alpha))
    upper = math.log((1.0 - cfg.beta) / cfg.alpha)
    return lower, upper


@dataclass(slots=True)
class RunningSharpe:
    """Online (Welford) per-trade return moments → a running Sharpe ratio.

    O(1) per trade, reusing the exact mean/variance recurrence of
    :func:`brutex.metrics.compute` (CLAUDE.md §11 COMMON). ``sharpe`` is the
    per-trade, zero-risk-free, **population**-variance ratio ``mean / σ``;
    it is ``None`` while undefined (``n < 2`` or zero variance), matching the
    metrics convention so a degenerate stream never yields a spurious decision.
    """

    n: int = 0
    mean: float = 0.0
    m2: float = 0.0

    def update(self, trade_return: float) -> None:
        """Fold one closed trade's return into the running moments (O(1))."""
        self.n += 1
        delta = trade_return - self.mean
        self.mean += delta / self.n
        self.m2 += delta * (trade_return - self.mean)

    @property
    def sharpe(self) -> float | None:
        """The running per-trade Sharpe, or ``None`` while undefined."""
        if self.n < _MIN_DEFINED_N:
            return None
        variance = self.m2 / self.n
        if variance <= 0.0:
            return None
        return self.mean / math.sqrt(variance)


def sprt_decision(stat: RunningSharpe, *, p95_threshold: float, cfg: SprtConfig) -> Decision:
    """Decide CONTINUE/KILL for an in-flight backtest from its running Sharpe.

    The pure, deterministic core (no I/O / clock / RNG). Returns
    :attr:`Decision.CONTINUE` until the ``min_trades`` floor is reached or while
    the Sharpe is undefined; otherwise :attr:`Decision.KILL` iff the Gaussian
    posterior ``Pr(true_sharpe > p95_threshold)`` is below ``kill_posterior``.

    Parameters
    ----------
    stat
        The combo's running per-trade Sharpe statistic.
    p95_threshold
        The Sharpe a combo must plausibly clear to be a leaderboard contender
        (injected — never computed here; see ADR-0006 B4 on tying it to ``top_k``).
    cfg
        The SPRT parameters.
    """
    if stat.n < cfg.min_trades:
        return Decision.CONTINUE
    observed = stat.sharpe
    if observed is None:
        return Decision.CONTINUE
    # "Gaussian conjugate posterior" (spec) == PSR with normal skew/kurtosis.
    posterior = probabilistic_sharpe_ratio(observed, p95_threshold, stat.n, 0.0, 3.0)
    if posterior < cfg.kill_posterior:
        return Decision.KILL
    return Decision.CONTINUE


__all__ = [
    "Decision",
    "EarlyStopConfigError",
    "RunningSharpe",
    "SprtConfig",
    "sprt_decision",
    "wald_bounds",
]
