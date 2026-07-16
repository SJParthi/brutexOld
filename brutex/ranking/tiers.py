"""The S+ / S / A strategy tier classifier (DEC-RANKING-SPLUS-TIER-001).

An ANNOTATION-ONLY tier label ("how exceptional is this strategy?") layered on
EXISTING metrics. It COMPOSES with — never bypasses — the 5 hard rejection
gates (:mod:`brutex.ranking`) and the 9-filter Stage-4 cascade
(:mod:`brutex.metrics.cascade`): every classified candidate already passed the
hard gates (the top-100 writer classifies only its gate-passing pool), and the
cascade verdict is itself one of the tier axes. The verdict NEVER reranks,
hides or excludes an entry (the DEC-RESULTS-CASCADE-VERDICT-JOIN-001 R13 law)
and is NEVER part of the blake3 run identity (CLAUDE.md §2 Rule 4).

## The approved tier law (operator-approved 2026-07-16 — implement exactly)

Every axis must pass for a tier; :func:`classify_tier` evaluates S+ first,
then S, then A; below A → no tier (``tier=None``).

===========================  ==========  ==========  ==========
Axis                         S+          S           A
===========================  ==========  ==========  ==========
1 total_trades ≥             500         200         30
2 win_rate ≥                 0.65        0.55        0.40
3 payoff (win/loss ratio) ≥  1.0         0.8         0.6
4 profit_factor ≥            2.0         1.6         1.2
5 expectancy_paisa           ≥ 2×H       ≥ H         > 0
6 stop width (bps of entry)  ≤ 10.0      ≤ 20.0      (no stop required)
6 MAE₁ₘ worst ≤ (×sl)        1.5         2.0         (not required)
7 max_drawdown_pct ≤         5.0         10.0        30.0
7 minute-path DD pct ≤       7.5         15.0        (not required)
8 max_consecutive_losses ≤   6           8           10
9 cascade status             == pass     pass|prov.  != fail
===========================  ==========  ==========  ==========

Per-axis rationale (each documented on the threshold constants too):

* **500 trades (S+)** is the lockbox floor
  (:data:`brutex.metrics.cascade.LOCKBOX_DEFAULT_MIN_TRADES` — the sample size
  the Stage-4 lockbox filter demands before an out-of-sample verdict counts);
  **30 (A)** is the hard-gate :data:`brutex.ranking.MIN_TRADES` floor.
* **Streak 6 (S+)**: at the S-tier win-rate floor (win 0.55 → loss prob 0.45
  ≈ 0.35–0.45 band) the EXPECTED maximum losing run over 500 trades is
  ``ln(500)/ln(1/0.35) ≈ 5.9`` — a streak beyond ~6 at S+ quality is evidence
  the losses are not independent bad luck.
* **PF / win-rate / payoff** are a JOINT floor: any two of the three can be
  gamed by the third (a 90% win rate with payoff 0.1 has PF < 1), so all
  three are required per tier.
* **H (the expectancy haircut)** is the per-round-trip adverse-fill surrogate
  — see :class:`TierConfig`.
* **bps-relative stop width** (``sl / entry × 10_000``) keeps the "extreme
  tight stop" axis comparable ACROSS instruments (a ₹24 stop is tight on
  NIFTY at ₹24,000 and enormous on a ₹100 stock).

## Honesty laws (Golden Rule #1 / #10)

* **Fail-closed on non-finite input**: a ``NaN``/±``inf`` in ANY consulted
  float yields ``tier=None`` + the single reason
  :data:`TIER_NON_FINITE_INPUT` — mirroring
  :func:`brutex.ranking.passes_gates`'s guard (a ``NaN`` silently passes a
  ``<``/``>`` comparison, a wrong-ACCEPT on malformed data).
* **Honest missing-input degrade**: an input a tier NEEDS but the data cannot
  supply (no 1-minute path, no usable entry price) makes THAT tier ineligible
  via a ``missing_input:<field>`` token — never a fabricated pass, never a
  crash. Tier A deliberately needs NO minute-path/stop inputs, so a
  no-minute-data lake still tiers honestly at A.
* **``None``-ratio semantics**: a ``None`` win/loss ratio caused by ZERO
  losing trades PASSES the payoff axis (there are no losses to be paid off
  against); a ``None`` profit factor caused by ZERO gross loss PASSES the PF
  axis (division by the zero loss is undefined-favourably). Any other
  ``None`` fails its axis honestly. NOTE: :class:`~brutex.engine.results_store.ResultRow`
  persists an undefined profit factor as the ``0.0`` sentinel
  (``sweep_run._metric_or_zero``), so the zero-loss arm keys on
  ``gross_loss_paisa == 0`` — never on the sentinel value.
* **Gross-signal-P&L scope**: every consulted metric is signal-only —
  NO metric in this repo is computed under a literal worst-case fill today
  (the trade sim fills at stored real prices, ``brutex/engine/trade_sim.py``;
  the futures executor's fill law is optimistic-adverse-tick,
  ``brutex/futures/executor.py``). ``H`` is the v1 surrogate; the true
  bar-extreme adverse-fill re-sim is the NAMED follow-up
  DEC-TIER-PESSIMISTIC-FILL-002.

O(1) per call (a bounded handful of comparisons + small tuple allocations).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

__all__ = [
    "A_THRESHOLDS",
    "DEFAULT_TIER_CONFIG",
    "MISSING_INPUT_PREFIX",
    "SPLUS_THRESHOLDS",
    "S_THRESHOLDS",
    "TIER_A",
    "TIER_CASCADE",
    "TIER_CONSECUTIVE_LOSSES",
    "TIER_EXPECTANCY_HEADROOM",
    "TIER_LABELS",
    "TIER_MAX_DRAWDOWN",
    "TIER_MINUTE_PATH_DRAWDOWN",
    "TIER_MIN_TRADES",
    "TIER_NON_FINITE_INPUT",
    "TIER_PAYOFF",
    "TIER_PROFIT_FACTOR",
    "TIER_REASON_ORDER",
    "TIER_S",
    "TIER_SPLUS",
    "TIER_STOP_MISSING",
    "TIER_STOP_NOT_CONTAINED",
    "TIER_STOP_TOO_WIDE",
    "TIER_WIN_RATE",
    "TierConfig",
    "TierInputs",
    "TierThresholds",
    "TierVerdict",
    "classify_tier",
    "tier_config_payload",
]

# ── Tier labels ──────────────────────────────────────────────────────────────

#: The exceptional tier — every S+ axis passed (blocked_next is empty).
TIER_SPLUS: Final[str] = "S+"
#: The strong tier — every S axis passed; blocked_next names the S+ blockers.
TIER_S: Final[str] = "S"
#: The solid tier — every A axis passed; blocked_next names the S blockers.
TIER_A: Final[str] = "A"

#: The complete label vocabulary, best-first (display + bridge validation).
TIER_LABELS: Final[tuple[str, ...]] = (TIER_SPLUS, TIER_S, TIER_A)

# ── Reason-token vocabulary (STABLE wire tokens — the GATE_REASON_ORDER law) ─

#: Fail-closed guard tripped: a consulted float input was present but
#: non-finite (``NaN``/±``inf``) — the metrics are malformed, so NO axis is
#: judged (judging garbage would fabricate axis verdicts, Golden Rule #1).
TIER_NON_FINITE_INPUT: Final[str] = "non_finite_input"
#: Axis 1 — ``total_trades`` below the tier's floor.
TIER_MIN_TRADES: Final[str] = "min_trades"
#: Axis 2 — ``win_rate`` below the tier's floor.
TIER_WIN_RATE: Final[str] = "win_rate"
#: Axis 3 — win/loss ratio below the tier's floor (``None`` with zero losing
#: trades PASSES; ``None`` otherwise fails here).
TIER_PAYOFF: Final[str] = "payoff"
#: Axis 4 — profit factor below the tier's floor (zero gross loss PASSES).
TIER_PROFIT_FACTOR: Final[str] = "profit_factor"
#: Axis 5 — expectancy below the tier's haircut-multiple floor.
TIER_EXPECTANCY_HEADROOM: Final[str] = "expectancy_headroom"
#: Axis 6 — the tier requires a stop-loss arm and ``sl_paisa`` is ``None``.
TIER_STOP_MISSING: Final[str] = "stop_missing"
#: Axis 6 — the stop's width in bps of the average entry price exceeds the
#: tier's ceiling.
TIER_STOP_TOO_WIDE: Final[str] = "stop_too_wide"
#: Axis 6 — the worst 1-minute-path MAE exceeds the tier's multiple of the
#: stop (the risk-containment law: the stop must actually bound the heat).
TIER_STOP_NOT_CONTAINED: Final[str] = "stop_not_contained"
#: Axis 7 — ``max_drawdown_pct`` above the tier's ceiling.
TIER_MAX_DRAWDOWN: Final[str] = "max_drawdown"
#: Axis 7 — the 1-minute-path drawdown pct above the tier's ceiling.
TIER_MINUTE_PATH_DRAWDOWN: Final[str] = "minute_path_drawdown"
#: Axis 8 — ``max_consecutive_losses`` above the tier's ceiling.
TIER_CONSECUTIVE_LOSSES: Final[str] = "consecutive_losses"
#: Axis 9 — the joined Stage-4 cascade status outside the tier's allowed set.
TIER_CASCADE: Final[str] = "cascade"

#: The honest missing-input token prefix: ``missing_input:<field>`` names an
#: input a tier NEEDS but the data cannot supply — e.g.
#: ``missing_input:mae_1m`` (no 1-minute path) or
#: ``missing_input:avg_entry_price`` (no usable entry price for the bps stop
#: width). These tokens slot at their owning axis's position in the emitted
#: order; they are the ONLY non-fixed members of the vocabulary.
MISSING_INPUT_PREFIX: Final[str] = "missing_input:"

#: The FIXED reason vocabulary in EVALUATION order — the exact order
#: :func:`classify_tier` judges the axes, so ``blocked_next[0]`` is always
#: the first blocking axis. ``missing_input:<field>`` tokens (see
#: :data:`MISSING_INPUT_PREFIX`) slot at their owning axis's position and are
#: deliberately not enumerable here. Consumers may rely on this order being
#: stable (the :data:`brutex.ranking.GATE_REASON_ORDER` law).
TIER_REASON_ORDER: Final[tuple[str, ...]] = (
    TIER_NON_FINITE_INPUT,
    TIER_MIN_TRADES,
    TIER_WIN_RATE,
    TIER_PAYOFF,
    TIER_PROFIT_FACTOR,
    TIER_EXPECTANCY_HEADROOM,
    TIER_STOP_MISSING,
    TIER_STOP_TOO_WIDE,
    TIER_STOP_NOT_CONTAINED,
    TIER_MAX_DRAWDOWN,
    TIER_MINUTE_PATH_DRAWDOWN,
    TIER_CONSECUTIVE_LOSSES,
    TIER_CASCADE,
)

#: The equity-status token meaning "no 1-minute path exists for this combo"
#: (:class:`brutex.engine.combo_aggregates.ComboAggregate.equity_status` —
#: ``ok`` / ``partial`` / ``no_1m_data``). Pinned by test against the
#: aggregate module's docstring vocabulary.
_EQUITY_STATUS_NO_1M: Final[str] = "no_1m_data"

#: The cascade status token that BLOCKS tier A (every other status —
#: including absent — is acceptable at A).
_CASCADE_FAIL: Final[str] = "fail"

#: Basis points per unit ratio (the stop-width projection).
_BPS_PER_UNIT: Final[float] = 10_000.0


@dataclass(frozen=True, slots=True)
class TierThresholds:
    """One tier's complete axis-threshold set (operator-approved values).

    Attributes
    ----------
    label
        The tier label this row defines (one of :data:`TIER_LABELS`).
    min_trades
        Axis 1 floor (inclusive). S+ 500 = the lockbox sample floor
        (``LOCKBOX_DEFAULT_MIN_TRADES``); A 30 = the hard-gate
        :data:`brutex.ranking.MIN_TRADES`.
    min_win_rate
        Axis 2 floor (inclusive, fraction in ``[0, 1]``).
    min_payoff
        Axis 3 floor (inclusive) on the win/loss ratio.
    min_profit_factor
        Axis 4 floor (inclusive).
    expectancy_haircut_multiple
        Axis 5: expectancy must clear ``multiple × H`` paisa (H =
        :attr:`TierConfig.haircut_paisa`). S+ 2.0 / S 1.0 / A 0.0.
    expectancy_exclusive
        ``True`` → the axis-5 comparison is STRICT (``>``, the A tier's
        "> 0" arm); ``False`` → inclusive (``>=``).
    requires_stop
        Axis 6: whether the tier demands a stop-loss arm at all (S+/S yes,
        A no — A deliberately does not require a stop).
    max_stop_bps
        Axis 6 ceiling (inclusive) on ``sl / entry × 10_000`` — the
        cross-instrument-comparable stop width. ``None`` = not required.
    max_mae_sl_multiple
        Axis 6 risk-containment ceiling (inclusive): worst 1-minute MAE must
        be ``<= multiple × sl_paisa``. ``None`` = not required.
    max_drawdown_pct
        Axis 7 ceiling (inclusive) on the trade-close drawdown pct.
    max_minute_dd_pct
        Axis 7 ceiling (inclusive) on the 1-minute-path drawdown pct.
        ``None`` = not required (the A tier).
    max_consecutive_losses
        Axis 8 ceiling (inclusive). S+ 6: at 500 trades the expected max
        losing run is ``ln(500)/ln(1/0.35) ≈ 5.9`` — beyond it the losses
        are unlikely to be independent bad luck.
    cascade_allowed
        Axis 9: the exact set of acceptable joined cascade status tokens, or
        ``None`` meaning "any status EXCEPT ``fail``" (the A tier —
        absent/``not_validated`` is acceptable there).
    """

    label: str
    min_trades: int
    min_win_rate: float
    min_payoff: float
    min_profit_factor: float
    expectancy_haircut_multiple: float
    expectancy_exclusive: bool
    requires_stop: bool
    max_stop_bps: float | None
    max_mae_sl_multiple: float | None
    max_drawdown_pct: float
    max_minute_dd_pct: float | None
    max_consecutive_losses: int
    cascade_allowed: frozenset[str] | None

    def __post_init__(self) -> None:
        if self.label not in TIER_LABELS:
            raise ValueError(
                f"TierThresholds.label must be one of {TIER_LABELS}, got {self.label!r}"
            )
        if self.min_trades < 0:
            raise ValueError(f"TierThresholds.min_trades must be >= 0, got {self.min_trades}")
        if not 0.0 <= self.min_win_rate <= 1.0:
            raise ValueError(
                f"TierThresholds.min_win_rate must be in [0, 1], got {self.min_win_rate}"
            )
        for name in (
            "min_payoff",
            "min_profit_factor",
            "expectancy_haircut_multiple",
            "max_drawdown_pct",
        ):
            value: float = getattr(self, name)
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"TierThresholds.{name} must be finite and >= 0, got {value!r}")
        for name in ("max_stop_bps", "max_mae_sl_multiple", "max_minute_dd_pct"):
            optional: float | None = getattr(self, name)
            if optional is not None and (not math.isfinite(optional) or optional <= 0.0):
                raise ValueError(
                    f"TierThresholds.{name} must be finite and > 0 when set, got {optional!r}"
                )
        if self.max_consecutive_losses < 0:
            raise ValueError(
                "TierThresholds.max_consecutive_losses must be >= 0, "
                f"got {self.max_consecutive_losses}"
            )
        if self.cascade_allowed is not None and not self.cascade_allowed:
            raise ValueError(
                "TierThresholds.cascade_allowed must be a non-empty set or None "
                "(None = any status except 'fail')"
            )


#: The S+ tier thresholds (operator-approved 2026-07-16). 500 trades = the
#: Stage-4 lockbox floor; win 0.65 / payoff 1.0 / PF 2.0 = the joint quality
#: floor no single metric can game; expectancy ≥ 2×H = twice the adverse-fill
#: haircut survives; stop ≤ 10 bps of entry + MAE ≤ 1.5×sl = the "extreme
#: tight, actually-binding stop"; DD ≤ 5% close / 7.5% minute-path; streak
#: ≤ 6 (≈ the expected-max-run bound, module docstring); cascade == pass
#: EXACTLY (a provisional pass is not S+ evidence).
SPLUS_THRESHOLDS: Final[TierThresholds] = TierThresholds(
    label=TIER_SPLUS,
    min_trades=500,
    min_win_rate=0.65,
    min_payoff=1.0,
    min_profit_factor=2.0,
    expectancy_haircut_multiple=2.0,
    expectancy_exclusive=False,
    requires_stop=True,
    max_stop_bps=10.0,
    max_mae_sl_multiple=1.5,
    max_drawdown_pct=5.0,
    max_minute_dd_pct=7.5,
    max_consecutive_losses=6,
    cascade_allowed=frozenset({"pass"}),
)

#: The S tier thresholds (operator-approved 2026-07-16): 200 trades; win
#: 0.55 / payoff 0.8 / PF 1.6; expectancy ≥ H (survives ONE haircut); stop
#: ≤ 20 bps + MAE ≤ 2.0×sl; DD ≤ 10% / 15% minute-path; streak ≤ 8;
#: cascade pass OR provisional_pass (the pool-N DSR fallback is acceptable
#: S evidence, never S+).
S_THRESHOLDS: Final[TierThresholds] = TierThresholds(
    label=TIER_S,
    min_trades=200,
    min_win_rate=0.55,
    min_payoff=0.8,
    min_profit_factor=1.6,
    expectancy_haircut_multiple=1.0,
    expectancy_exclusive=False,
    requires_stop=True,
    max_stop_bps=20.0,
    max_mae_sl_multiple=2.0,
    max_drawdown_pct=10.0,
    max_minute_dd_pct=15.0,
    max_consecutive_losses=8,
    cascade_allowed=frozenset({"pass", "provisional_pass"}),
)

#: The A tier thresholds (operator-approved 2026-07-16): exactly the hard
#: rejection gates' floors where they exist (trades 30 = ``MIN_TRADES``,
#: PF 1.2, DD 30%, streak 10) plus win 0.40 / payoff 0.6 / expectancy
#: STRICTLY > 0; NO stop and NO minute-path demands (an A tier must be
#: reachable on a lake without minute data); cascade merely NOT ``fail``
#: (absent / not_validated is acceptable). NOTE (hostile-review round-1
#: NIT-2, deliberate): the gate-mirroring axes (trades/PF/DD/streak) are
#: STRUCTURAL no-ops at the top-100 writer — every classified entry already
#: passed the hard gates — they exist so ``classify_tier`` stays a total,
#: self-contained law for ANY caller, not just the artifact writer.
A_THRESHOLDS: Final[TierThresholds] = TierThresholds(
    label=TIER_A,
    min_trades=30,
    min_win_rate=0.40,
    min_payoff=0.6,
    min_profit_factor=1.2,
    expectancy_haircut_multiple=0.0,
    expectancy_exclusive=True,
    requires_stop=False,
    max_stop_bps=None,
    max_mae_sl_multiple=None,
    max_drawdown_pct=30.0,
    max_minute_dd_pct=None,
    max_consecutive_losses=10,
    cascade_allowed=None,
)


@dataclass(frozen=True, slots=True)
class TierConfig:
    """The three tier threshold rows + the expectancy haircut ``H``.

    ``haircut_paisa`` (H) is the per-round-trip adverse-fill HAIRCUT the
    expectancy axis must clear — default 1000 paisa = ₹10 per unit
    (operator-approved 2026-07-16). EXPLICITLY a v1 SURROGATE for the
    deferred pessimistic-fill re-sim (DEC-TIER-PESSIMISTIC-FILL-002 — the
    true bar-extreme adverse-fill leg in ``results_depth_pass``,
    O(finalists)): no metric in this repo is computed under a literal
    worst-case fill today, and the ₹10 figure is an UNVERIFIED calibration
    (Golden Rule #10 — stated, never claimed measured).
    """

    splus: TierThresholds = SPLUS_THRESHOLDS
    s: TierThresholds = S_THRESHOLDS
    a: TierThresholds = A_THRESHOLDS
    haircut_paisa: int = 1000

    def __post_init__(self) -> None:
        if self.haircut_paisa <= 0:
            raise ValueError(
                f"TierConfig.haircut_paisa must be > 0, got {self.haircut_paisa} "
                "(a non-positive H would collapse the S/S+ expectancy floors onto A's)"
            )
        for name, expected in (("splus", TIER_SPLUS), ("s", TIER_S), ("a", TIER_A)):
            row: TierThresholds = getattr(self, name)
            if row.label != expected:
                raise ValueError(
                    f"TierConfig.{name} must carry label {expected!r}, got {row.label!r}"
                )

    def ladder(self) -> tuple[TierThresholds, ...]:
        """The tiers best-first — the exact evaluation order."""
        return (self.splus, self.s, self.a)


#: The ONE default config (code constants, never env vars — the repo's gate
#: precedent: ``brutex.ranking``'s thresholds are module constants too).
DEFAULT_TIER_CONFIG: Final[TierConfig] = TierConfig()


@dataclass(frozen=True, slots=True)
class TierInputs:
    """Every input the classifier consults — all EXISTING persisted values.

    The top-100 writer builds this from its ``Top100Candidate``
    (``ResultRow`` ⋈ ``ComboAggregate`` + the owning tuple's arm + the
    joined cascade status); nothing here is newly computed or persisted.

    Attributes
    ----------
    total_trades, win_rate, max_drawdown_pct, expectancy_paisa
        The persisted ``ResultRow`` values (win_rate a fraction; expectancy
        signed int64 paisa).
    win_loss_ratio, losing_trades
        The agg's recomputed payoff ratio (``None`` when undefined) + the
        row's losing-trade count (the zero-losses PASS arm's evidence).
    profit_factor, gross_loss_paisa
        The row's PF + gross loss magnitude. ``gross_loss_paisa == 0`` PASSES
        the PF axis regardless of the PF value (the row persists an undefined
        PF as the ``0.0`` sentinel — module docstring).
    sl_paisa
        The owning tuple's stop-loss arm (``None`` = no stop armed).
    entry_price_paisa
        The row's average entry price in int paisa (``avg_entry_price``
        Decimal RUPEES × 100 — exact on the 2dp tick grid), or ``None`` when
        unusable (non-positive) — the bps stop-width denominator.
    mae_1m_worst_paisa, trades_with_1m, equity_status
        The agg's 1-minute-path evidence; the minute path is AVAILABLE iff
        ``mae_1m_worst_paisa is not None and trades_with_1m > 0 and
        equity_status != "no_1m_data"``.
    max_dd_minute_path_pct
        The agg's minute-path drawdown pct (``None`` = not measured).
    max_consecutive_losses
        The agg's longest losing run (``None`` = unknown → fails any tier).
    cascade_status
        The JOINED per-entry cascade status token (``pass`` /
        ``provisional_pass`` / ``fail`` / ``not_validated``) or ``None``
        when no verdict exists at all.
    """

    total_trades: int
    win_rate: float
    win_loss_ratio: float | None
    losing_trades: int
    profit_factor: float | None
    gross_loss_paisa: int
    expectancy_paisa: int
    sl_paisa: int | None
    entry_price_paisa: int | None
    mae_1m_worst_paisa: int | None
    trades_with_1m: int
    equity_status: str
    max_drawdown_pct: float
    max_dd_minute_path_pct: float | None
    max_consecutive_losses: int | None
    cascade_status: str | None

    @property
    def minute_path_available(self) -> bool:
        """Whether the 1-minute-path evidence exists for this candidate."""
        return (
            self.mae_1m_worst_paisa is not None
            and self.trades_with_1m > 0
            and self.equity_status != _EQUITY_STATUS_NO_1M
        )


@dataclass(frozen=True, slots=True)
class TierVerdict:
    """The classification outcome.

    ``tier`` is one of :data:`TIER_LABELS` or ``None`` (below A / fail-closed);
    ``blocked_next`` names the axes blocking the NEXT tier up in STABLE
    evaluation order (:data:`TIER_REASON_ORDER` + ``missing_input:*`` slotted
    at their axis) — empty for S+; for ``tier=None`` it names the A blockers.
    """

    tier: str | None
    blocked_next: tuple[str, ...]


def _non_finite(value: float | None) -> bool:
    """Present-but-non-finite check (the :mod:`brutex.ranking` guard law)."""
    return value is not None and not math.isfinite(value)


def _missing(field: str) -> str:
    """The ``missing_input:<field>`` token for *field*."""
    return f"{MISSING_INPUT_PREFIX}{field}"


def _stop_failures(inputs: TierInputs, row: TierThresholds) -> list[str]:
    """Axis-6 failure tokens (stop presence · bps width · MAE containment)."""
    failures: list[str] = []
    if not row.requires_stop:
        return failures
    if inputs.sl_paisa is None:
        # Without a stop the width + containment sub-axes are unjudgeable —
        # ONE token, not three (the stop itself is the missing prerequisite).
        return [TIER_STOP_MISSING]
    if row.max_stop_bps is not None:
        if inputs.entry_price_paisa is None:
            failures.append(_missing("avg_entry_price"))
        elif (inputs.sl_paisa / inputs.entry_price_paisa) * _BPS_PER_UNIT > row.max_stop_bps:
            failures.append(TIER_STOP_TOO_WIDE)
    if row.max_mae_sl_multiple is not None:
        mae = inputs.mae_1m_worst_paisa
        if mae is None or not inputs.minute_path_available:
            failures.append(_missing("mae_1m"))
        elif mae > row.max_mae_sl_multiple * inputs.sl_paisa:
            failures.append(TIER_STOP_NOT_CONTAINED)
    return failures


def _axis_failures(
    inputs: TierInputs, row: TierThresholds, *, haircut_paisa: int
) -> tuple[str, ...]:
    """Every axis of *row* that *inputs* fails, in stable evaluation order.

    Empty tuple ⇔ the tier is earned. Tokens are deduplicated preserving the
    first occurrence (``missing_input:mae_1m`` can arise from axis 6 AND
    axis 7 — it is emitted once).
    """
    failures: list[str] = []
    # Axis 1 — sample size.
    if inputs.total_trades < row.min_trades:
        failures.append(TIER_MIN_TRADES)
    # Axis 2 — win rate.
    if inputs.win_rate < row.min_win_rate:
        failures.append(TIER_WIN_RATE)
    # Axis 3 — payoff. None + zero losing trades = PASS (no losses to pay
    # off against); None otherwise fails honestly.
    if inputs.win_loss_ratio is None:
        if inputs.losing_trades > 0:
            failures.append(TIER_PAYOFF)
    elif inputs.win_loss_ratio < row.min_payoff:
        failures.append(TIER_PAYOFF)
    # Axis 4 — profit factor. Zero gross loss = PASS regardless of the
    # stored value (the ResultRow 0.0-sentinel law, module docstring).
    if inputs.gross_loss_paisa != 0 and (
        inputs.profit_factor is None or inputs.profit_factor < row.min_profit_factor
    ):
        failures.append(TIER_PROFIT_FACTOR)
    # Axis 5 — expectancy headroom over the haircut multiple (the A tier's
    # ``> 0`` arm is STRICT — expectancy_exclusive).
    floor = row.expectancy_haircut_multiple * haircut_paisa
    expectancy_fails = (
        inputs.expectancy_paisa <= floor
        if row.expectancy_exclusive
        else inputs.expectancy_paisa < floor
    )
    if expectancy_fails:
        failures.append(TIER_EXPECTANCY_HEADROOM)
    # Axis 6 — the stop axes (S+/S only).
    failures.extend(_stop_failures(inputs, row))
    # Axis 7 — drawdowns (close-basis always; minute-path when required).
    if inputs.max_drawdown_pct > row.max_drawdown_pct:
        failures.append(TIER_MAX_DRAWDOWN)
    if row.max_minute_dd_pct is not None:
        if inputs.max_dd_minute_path_pct is None or not inputs.minute_path_available:
            failures.append(_missing("mae_1m"))
        elif inputs.max_dd_minute_path_pct > row.max_minute_dd_pct:
            failures.append(TIER_MINUTE_PATH_DRAWDOWN)
    # Axis 8 — losing streak (an AGG column, present for every top-100
    # candidate; None = unknown → fails honestly, never a fabricated pass).
    if inputs.max_consecutive_losses is None:
        failures.append(_missing("max_consecutive_losses"))
    elif inputs.max_consecutive_losses > row.max_consecutive_losses:
        failures.append(TIER_CONSECUTIVE_LOSSES)
    # Axis 9 — the joined Stage-4 cascade verdict.
    if row.cascade_allowed is not None:
        if inputs.cascade_status not in row.cascade_allowed:
            failures.append(TIER_CASCADE)
    elif inputs.cascade_status == _CASCADE_FAIL:
        failures.append(TIER_CASCADE)
    # Dedupe preserving first occurrence (dict is insertion-ordered).
    return tuple(dict.fromkeys(failures))


def classify_tier(inputs: TierInputs, *, config: TierConfig = DEFAULT_TIER_CONFIG) -> TierVerdict:
    """Classify one candidate under the approved tier law (module docstring).

    Evaluates S+ first, then S, then A — every axis must pass for a tier;
    below A returns ``tier=None``. ``blocked_next`` names the axes blocking
    the NEXT tier up (empty for S+; the A blockers for ``tier=None``), in
    the stable :data:`TIER_REASON_ORDER` evaluation order.

    **Fail-closed** (Golden Rule #1): a present ``NaN``/±``inf`` in ANY
    consulted float (``win_rate`` · ``win_loss_ratio`` · ``profit_factor``
    · ``max_drawdown_pct`` · ``max_dd_minute_path_pct``) returns
    ``TierVerdict(None, ("non_finite_input",))`` ALONE — no axis is judged
    on malformed data (the :func:`brutex.ranking.passes_gates` guard law).
    """
    if any(
        _non_finite(value)
        for value in (
            inputs.win_rate,
            inputs.win_loss_ratio,
            inputs.profit_factor,
            inputs.max_drawdown_pct,
            inputs.max_dd_minute_path_pct,
        )
    ):
        return TierVerdict(tier=None, blocked_next=(TIER_NON_FINITE_INPUT,))
    haircut = config.haircut_paisa
    splus_failures = _axis_failures(inputs, config.splus, haircut_paisa=haircut)
    if not splus_failures:
        return TierVerdict(tier=TIER_SPLUS, blocked_next=())
    s_failures = _axis_failures(inputs, config.s, haircut_paisa=haircut)
    if not s_failures:
        return TierVerdict(tier=TIER_S, blocked_next=splus_failures)
    a_failures = _axis_failures(inputs, config.a, haircut_paisa=haircut)
    if not a_failures:
        return TierVerdict(tier=TIER_A, blocked_next=s_failures)
    return TierVerdict(tier=None, blocked_next=a_failures)


def _thresholds_payload(row: TierThresholds) -> dict[str, object]:
    """One tier row as a deterministic JSON-safe mapping (self-description)."""
    return {
        "min_trades": row.min_trades,
        "min_win_rate": row.min_win_rate,
        "min_payoff": row.min_payoff,
        "min_profit_factor": row.min_profit_factor,
        "expectancy_haircut_multiple": row.expectancy_haircut_multiple,
        "expectancy_exclusive": row.expectancy_exclusive,
        "requires_stop": row.requires_stop,
        "max_stop_bps": row.max_stop_bps,
        "max_mae_sl_multiple": row.max_mae_sl_multiple,
        "max_drawdown_pct": row.max_drawdown_pct,
        "max_minute_dd_pct": row.max_minute_dd_pct,
        "max_consecutive_losses": row.max_consecutive_losses,
        "cascade_allowed": (None if row.cascade_allowed is None else sorted(row.cascade_allowed)),
    }


def tier_config_payload(config: TierConfig = DEFAULT_TIER_CONFIG) -> dict[str, object]:
    """The artifact's self-describing ``tier_config`` root meta payload.

    Echoes every threshold VALUE the classifier used, so a stored artifact's
    tier labels are interpretable without this module's source at that
    commit (the top-100 writer stamps it on every new artifact).
    """
    return {
        "haircut_paisa": config.haircut_paisa,
        "tiers": {row.label: _thresholds_payload(row) for row in config.ladder()},
    }
