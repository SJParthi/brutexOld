"""Low-support rescue lane — the quarantined second Stage-1B floor (default 8).

DEC-STAGE1B-LOW-SUPPORT-RESCUE-001 (LANE W3 of the 2026-07-18 operator
approve-all ruling — "Approve all dude okay", the 16-item winning-completeness
P0 list; item 6). A SECOND, QUARANTINED Stage-1B support floor
(:data:`RESCUE_MIN_HITS_DEFAULT` = 8): masks whose Stage-1B support (superset
hits) lands in ``[rescue_floor, MIN_TRADES)`` are RESCUED — simulated through
the NORMAL trade-sim path (:func:`brutex.engine.sweep_driver.metrics_for_mask`,
the shared per-mask core), tagged ``low_support=True``, and surfaced ONLY in
the separate "provisional specialists" artifact section
(:func:`provisional_specialists_section`). They are NEVER main-ranked: the
main leaderboard, its gates (:data:`brutex.ranking.MIN_TRADES` stands
untouched at 30) and every persisted main-result run-identity are
byte-identical whether the lane is armed or not (bomb-spy + frozen-golden
pinned).

Why DSR / CPCV are honestly BYPASSED here (Golden Rule #10)
-----------------------------------------------------------
The Deflated Sharpe Ratio's effective-N / estimator-variance correction and
CPCV's combinatorial fold counts are STRUCTURALLY INVALID below ~30 trades:
DSR's PSR radicand is a large-sample (asymptotic-normality) approximation
whose skew/kurtosis moment estimates are themselves noise at n < 30, and a
CPCV split of < 30 trades across S=16 groups leaves ~2 trades per group —
fold Sharpes are undefined or pure noise. Running them would manufacture
fake statistical authority. The rescue lane substitutes a battery that IS
valid at small n:

1. **Exact binomial sign-test tail** (``ln P(X >= wins)`` under fair-coin
   null, the BORN-RUST ``fno_math.stats_binom_tail_log_sf`` kernel — exact
   at any n, no asymptotics; gate ``p < 1e-4``).
2. **Monte-Carlo permutation test** (the EXISTING
   :func:`brutex.metrics.monte_carlo.monte_carlo_permutation_test`, B=1000,
   deterministically seeded from the candidate id via BLAKE3 — exact
   permutation inference, valid at any n).
3. **Lockbox holdout evaluation** (the EXISTING
   :func:`brutex.metrics.lockbox.lockbox_evaluation` machinery with a
   rescue-appropriate trade floor — the 500-trade production floor is a
   MAIN-lane law; the rescue floor default is
   :data:`RESCUE_LOCKBOX_MIN_TRADES_DEFAULT`, a W3-default constant standing
   in for an un-ruled operator value, disclosed in MIGRATION.md #1977).

Apriori interaction (conscious supersession)
--------------------------------------------
The apriori-freeze-contract's survival-law clause (F₁ = hits >=
``MIN_TRADES`` = 30) is CONSCIOUSLY SUPERSEDED-when-armed by the operator's
2026-07-18 approve-all ruling: when the rescue lane is armed the Apriori
support floor TRACKS the effective lower floor via
:func:`effective_apriori_min_hits` (the ``min_hits`` PARAMETER the frozen
``sweep_apriori`` module already takes — the frozen module itself is
BYTE-UNTOUCHED, its semantics unchanged; only the floor VALUE flows), so
rescued candidates are never pre-pruned. The pruned ≡ unpruned parity
invariant is consciously RE-DERIVED at floor 8 and golden-pinned
(``tests/golden/test_engine_sweep_apriori_floor8_parity.py``). When the lane
is OFF the floor is byte-identical at 30 (the caller's ``min_hits``).

Default-OFF law
---------------
The lane arms ONLY via an explicit config (:class:`RescueConfig`) or the
fail-loud env knob :data:`RESCUE_MIN_HITS_ENV` (``BRUTEX_RESCUE_MIN_HITS``;
unset/blank/``0`` = OFF, an int in ``[1, MIN_TRADES)`` = armed, junk = a
loud ``ValueError`` — never a silent fallback). OFF ⇒ no rescue code runs
(bomb-spy pinned) and the whole sweep is byte-identical.

Honest compute envelope (STAGED-COMPUTE safeguard)
--------------------------------------------------
Armed, the lane multiplies the simulated-mask count by the band fraction
``f = |{masks : hits ∈ [rescue_floor, 30)}| / |{masks : hits >= 30}|``
(grid-dependent; :class:`RescueLaneResult` carries the measured counts) plus
``B × O(n_bars)`` permutation sims per candidate that reaches the MC stage
(``mc_skip_if_failed=True`` stages that cost behind the two cheap gates).
Support counting REUSES the existing Stage-1B popcount kernel
(:func:`brutex.engine.combo_mask.count_supermasks` — O(masks·bars/64), NO
new counting code path; spy-pinned). OFF ⇒ default cost UNCHANGED.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from brutex.ranking import MIN_TRADES

if TYPE_CHECKING:
    from brutex.data.models import CandleBar
    from brutex.metrics.cascade import CandidateInput
    from brutex.metrics.compute import ResultMetrics
    from brutex.strategies.types import Side
    from brutex.vocabulary.evaluator import ProximityParams

__all__ = [
    "RESCUE_BINOM_ALPHA_DEFAULT",
    "RESCUE_LOCKBOX_MIN_TRADES_DEFAULT",
    "RESCUE_MIN_HITS_DEFAULT",
    "RESCUE_MIN_HITS_ENV",
    "RescueConfig",
    "RescueLaneResult",
    "RescueValidation",
    "RescuedCandidate",
    "binomial_tail_log_pvalue",
    "effective_apriori_min_hits",
    "provisional_specialists_section",
    "rescue_mc_seed",
    "resolve_rescue_min_hits_env",
    "run_rescue_lane",
]

#: The quarantined second Stage-1B floor (operator-approved item 6: "low-support
#: rescue lane (floor 8, quarantined)"). Masks with hits in
#: ``[RESCUE_MIN_HITS_DEFAULT, MIN_TRADES)`` are rescue-eligible.
RESCUE_MIN_HITS_DEFAULT: Final[int] = 8

#: The fail-loud arming knob — unset/blank/``0`` = OFF; ``1..MIN_TRADES-1`` =
#: armed at that floor; anything else = a loud ``ValueError`` (never silent).
RESCUE_MIN_HITS_ENV: Final[str] = "BRUTEX_RESCUE_MIN_HITS"

#: Exact-binomial gate level (the ruling's "exact binomial tail p < 1e-4").
RESCUE_BINOM_ALPHA_DEFAULT: Final[float] = 1e-4

#: Rescue-lane lockbox trade floor — a W3-DEFAULT constant standing in for an
#: un-ruled operator value (disclosed, MIGRATION.md #1977): the production
#: 500-trade lockbox floor is a MAIN-lane law that would auto-fail every
#: n < 30 candidate; 5 holdout trades is the smallest count with a
#: non-degenerate holdout Sharpe while still demanding real out-of-window
#: evidence.
RESCUE_LOCKBOX_MIN_TRADES_DEFAULT: Final[int] = 5

#: Deterministic-seed modulus — mirrors the cascade's sweep-id-derived seed
#: law (``cascade_wire._SEED_MODULUS``): a non-negative 31-bit int for
#: ``numpy.random.default_rng``.
_SEED_MODULUS: Final[int] = 2**31

#: The provisional-specialists section schema (additive artifact surface).
RESCUE_SECTION_SCHEMA_VERSION: Final[int] = 1


def resolve_rescue_min_hits_env(value: str | None = None) -> int | None:
    """Resolve the fail-loud arming knob → the rescue floor or ``None`` (OFF).

    Parameters
    ----------
    value
        The raw env value; ``None`` (default) reads ``os.environ`` at call
        time (the sweep resolves ONCE per launch — never re-read mid-run).

    Returns
    -------
    int | None
        ``None`` when unset/blank/``"0"`` (OFF); the validated floor in
        ``[1, MIN_TRADES)`` when armed.

    Raises
    ------
    ValueError
        On a non-integer token or an out-of-range floor — a junk knob must be
        a loud launch-time error, never a silent fallback (house env law).
    """
    raw = os.environ.get(RESCUE_MIN_HITS_ENV) if value is None else value
    if raw is None or not raw.strip():
        return None
    token = raw.strip()
    try:
        floor = int(token)
    except ValueError:
        raise ValueError(
            f"{RESCUE_MIN_HITS_ENV} must be an integer in [0, {MIN_TRADES}), "
            f"got {token!r} (0/unset disables the rescue lane)"
        ) from None
    if floor == 0:
        return None
    if not 1 <= floor < MIN_TRADES:
        raise ValueError(
            f"{RESCUE_MIN_HITS_ENV} must be in [1, {MIN_TRADES}) — the rescue floor "
            f"must sit strictly BELOW the MIN_TRADES gate — got {floor}"
        )
    return floor


def effective_apriori_min_hits(min_hits: int, rescue_min_hits: int | None) -> int:
    """The Apriori survival floor with the rescue lane folded in.

    OFF (``rescue_min_hits is None``) ⇒ *min_hits* byte-identical (the frozen
    contract's floor flows through untouched). ARMED ⇒ the floor becomes the
    RESCUE floor so rescued candidates are never pre-pruned — the conscious,
    operator-ruled supersession of the apriori-freeze-contract's survival-law
    clause (module docstring; the frozen ``sweep_apriori`` module itself is
    byte-untouched — only its ``min_hits`` ARGUMENT changes).

    Raises
    ------
    ValueError
        If an armed floor is not strictly below *min_hits* (a rescue floor at
        or above the main floor would be a silent no-op masquerading as a
        rescue — refused loudly).
    """
    if rescue_min_hits is None:
        return min_hits
    if not 1 <= rescue_min_hits < min_hits:
        raise ValueError(
            f"rescue_min_hits must be in [1, min_hits={min_hits}), got {rescue_min_hits}"
        )
    return rescue_min_hits


@dataclass(frozen=True, slots=True)
class RescueConfig:
    """The rescue lane's knobs — validated at construction.

    ``rescue_min_hits`` is the quarantined floor (must sit strictly below
    :data:`~brutex.ranking.MIN_TRADES`); the rest parameterise the
    small-sample validation battery (module docstring). ``mc_skip_if_failed``
    stages the expensive B×O(n_bars) permutation cost behind the two cheap
    gates (the STAGED-COMPUTE safeguard) — set ``False`` for full
    observability on every candidate.
    """

    rescue_min_hits: int = RESCUE_MIN_HITS_DEFAULT
    binom_alpha: float = RESCUE_BINOM_ALPHA_DEFAULT
    binom_null_p: float = 0.5
    mc_permutations: int = 1000
    mc_alpha: float = 0.01
    lockbox_min_trades: int = RESCUE_LOCKBOX_MIN_TRADES_DEFAULT
    mc_skip_if_failed: bool = True

    def __post_init__(self) -> None:
        """Reject an out-of-band knob loudly (never a silent clamp)."""
        if not 1 <= self.rescue_min_hits < MIN_TRADES:
            raise ValueError(
                f"RescueConfig.rescue_min_hits must be in [1, {MIN_TRADES}), "
                f"got {self.rescue_min_hits}"
            )
        if not 0.0 < self.binom_alpha < 1.0:
            raise ValueError(f"RescueConfig.binom_alpha must be in (0, 1), got {self.binom_alpha}")
        if not 0.0 < self.binom_null_p < 1.0:
            raise ValueError(
                f"RescueConfig.binom_null_p must be in (0, 1), got {self.binom_null_p}"
            )
        if self.mc_permutations < 1:
            raise ValueError(
                f"RescueConfig.mc_permutations must be >= 1, got {self.mc_permutations}"
            )
        if not 0.0 < self.mc_alpha < 1.0:
            raise ValueError(f"RescueConfig.mc_alpha must be in (0, 1), got {self.mc_alpha}")
        if self.lockbox_min_trades < 1:
            raise ValueError(
                f"RescueConfig.lockbox_min_trades must be >= 1, got {self.lockbox_min_trades}"
            )


def rescue_mc_seed(candidate_id: str) -> int:
    """Deterministic 31-bit Monte-Carlo seed from a candidate id via BLAKE3.

    Golden Rule #4: no wall-clock, no ``random`` module — the seed derives
    from run identity alone (``blake3("rescue-mc:" + candidate_id)``, first 8
    bytes, mod 2³¹ — the cascade's sweep-id seed-derivation law), so the same
    candidate always draws the same permutations, bit-for-bit.
    """
    # Typed direct-kernel import (the combo_mask idiom — mypy cannot see the
    # dynamic `brutex.math` facade re-exports; same object either way).
    from fno_math import blake3_hash

    digest: bytes = blake3_hash(b"rescue-mc:" + candidate_id.encode("utf-8"))
    return int.from_bytes(digest[:8], "big") % _SEED_MODULUS


def binomial_tail_log_pvalue(n_trades: int, n_wins: int, *, null_p: float = 0.5) -> float:
    """``ln P(X >= n_wins)`` under the fair-coin null — the BORN-RUST kernel.

    A thin conduit to the compiled ``fno_math.stats_binom_tail_log_sf``
    kernel (typed direct import — the combo_mask idiom; the ``brutex.math``
    facade re-exports the SAME object); Python carries NO production copy of
    the math (the transcription + exact-``Fraction`` oracles live
    tests-side). O(n_trades) per call, log-space (a deep tail never
    underflows). Raises ``ValueError`` on invalid ``n``/``k``/``p`` (the
    kernel's own validation, verbatim).
    """
    from fno_math import stats_binom_tail_log_sf

    return float(stats_binom_tail_log_sf(n_trades, n_wins, null_p))


@dataclass(frozen=True, slots=True)
class RescueValidation:
    """One rescued candidate's small-sample validation verdicts.

    ``validated`` is the conjunction of the three gates; ``fail_reasons``
    names every failed/skipped stage (``()`` iff validated). ``mc_pvalue`` /
    ``mc_passed`` are ``None`` when the MC stage was staged out
    (``mc_skip_if_failed`` after a cheap-gate failure) — honestly "not run",
    never a fabricated verdict.
    """

    binom_log_pvalue: float
    binom_passed: bool
    lockbox_status: str
    lockbox_passed: bool
    mc_seed: int
    mc_pvalue: float | None
    mc_passed: bool | None
    validated: bool
    fail_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RescuedCandidate:
    """One rescued (low-support) mask — tagged, validated, never main-ranked.

    ``low_support`` is structurally always ``True`` (the tag every artifact
    row carries); ``hits`` is the Stage-1B superset support that landed in
    the rescue band.
    """

    mask: int
    hits: int
    total_trades: int
    winning_trades: int
    metrics: ResultMetrics
    validation: RescueValidation
    low_support: bool = True


@dataclass(frozen=True, slots=True)
class RescueLaneResult:
    """The lane's output + the measured compute-growth counts.

    ``band_count`` (masks whose hits landed in ``[rescue_floor, MIN_TRADES)``)
    over ``candidate_count`` (all masks screened) is the measured band
    fraction of the compute disclosure; ``simulated_count`` == ``band_count``
    (every band mask is simulated exactly once).
    """

    config: RescueConfig
    candidate_count: int
    band_count: int
    simulated_count: int
    rescued: tuple[RescuedCandidate, ...]


def _validate_candidate(
    candidate: CandidateInput,
    *,
    n_trades: int,
    n_wins: int,
    config: RescueConfig,
) -> RescueValidation:
    """Run the small-sample battery over one reconstructed candidate.

    Order: exact binomial (cheap, O(n)) → lockbox (cheap, O(bars)) →
    Monte-Carlo (B × O(bars) — staged behind the cheap gates when
    ``mc_skip_if_failed``). A stage that cannot be evaluated (a ``ValueError``
    out of the reused machinery) is a FAIL with a named reason — never a
    crash, never a silent pass (the cascade's reject idiom).
    """
    from brutex.metrics.lockbox import lockbox_evaluation
    from brutex.metrics.monte_carlo import monte_carlo_permutation_test

    reasons: list[str] = []

    # 1 — exact binomial sign-test tail (the BORN-RUST kernel).
    log_alpha = math.log(config.binom_alpha)
    binom_log_p = binomial_tail_log_pvalue(n_trades, n_wins, null_p=config.binom_null_p)
    binom_passed = binom_log_p < log_alpha
    if not binom_passed:
        reasons.append(f"binomial tail ln(p) {binom_log_p:.4f} >= ln(alpha) {log_alpha:.4f}")

    # 2 — lockbox holdout (the reused machinery, rescue trade floor).
    try:
        lockbox = lockbox_evaluation(
            candidate.lockbox_returns,
            candidate.lockbox_days,
            training_sharpe=candidate.training_sharpe,
            training_max_drawdown=candidate.training_max_drawdown,
            min_trades=config.lockbox_min_trades,
            evidence_days=candidate.lockbox_trade_days,
        )
        lockbox_status = lockbox.status.value
        lockbox_passed = lockbox.passed
    except ValueError as exc:
        lockbox_status = "error"
        lockbox_passed = False
        reasons.append(f"lockbox could not be evaluated: {exc}")
    if lockbox_status != "error" and not lockbox_passed:
        reasons.append(f"lockbox {lockbox_status}")

    # 3 — Monte-Carlo permutation (deterministically seeded; staged).
    seed = rescue_mc_seed(candidate.candidate_id)
    mc_pvalue: float | None = None
    mc_passed: bool | None = None
    if config.mc_skip_if_failed and reasons:
        reasons.append("monte-carlo skipped (staged behind failed cheap gates)")
    else:
        try:
            mc = monte_carlo_permutation_test(
                candidate.sharpe_ratio,
                candidate.mc_n_bars,
                candidate.mc_n_entries,
                candidate.mc_evaluate,
                seed=seed,
                n_permutations=config.mc_permutations,
                alpha=config.mc_alpha,
            )
            mc_pvalue = mc.pvalue
            mc_passed = mc.passed
            if not mc.passed:
                reasons.append(f"Monte-Carlo p {mc.pvalue:.4f} >= {config.mc_alpha}")
        except ValueError as exc:
            mc_passed = False
            reasons.append(f"Monte-Carlo could not be evaluated: {exc}")

    return RescueValidation(
        binom_log_pvalue=binom_log_p,
        binom_passed=binom_passed,
        lockbox_status=lockbox_status,
        lockbox_passed=lockbox_passed,
        mc_seed=seed,
        mc_pvalue=mc_pvalue,
        mc_passed=mc_passed,
        validated=binom_passed and lockbox_passed and mc_passed is True,
        fail_reasons=tuple(reasons),
    )


def run_rescue_lane(
    bars: Sequence[CandleBar],
    masks: Sequence[int],
    *,
    direction: Side,
    tf_minutes: int,
    stop_loss_paisa: int | None = None,
    take_profit_paisa: int | None = None,
    start_equity_paisa: int | None = None,
    proximity: ProximityParams | None = None,
    prior_n: int = 3,
    config: RescueConfig | None = None,
) -> RescueLaneResult:
    """Run the quarantined rescue pass over *masks* — the lane's ONE entry.

    Builds the Stage-1A ``bar_bits`` matrix once, counts every mask's support
    via the EXISTING batched popcount kernel
    (:func:`brutex.engine.combo_mask.count_supermasks` — the reuse law, NO new
    counting path), keeps exactly the band ``rescue_floor <= hits <
    MIN_TRADES``, simulates each band mask through the NORMAL per-mask core
    (:func:`~brutex.engine.sweep_driver.metrics_for_mask` — the same trade-sim
    the main sweep runs), reconstructs the cascade-shaped candidate via the
    reused :func:`brutex.engine.cascade_wire.build_candidate_input`, and
    validates it with the small-sample battery.

    This is a STANDALONE quarantined pass: it writes nothing, ranks nothing,
    and never touches the main sweep's results/run-identity — the caller
    (the sweep CLI at club-fold binding time) feeds its result to
    :func:`provisional_specialists_section`.

    Raises
    ------
    ValueError
        If *bars* is empty (mirrors ``sweep_masks``).
    """
    from brutex.engine.cascade_wire import build_candidate_input
    from brutex.engine.combo_mask import count_supermasks
    from brutex.engine.stage1a import build_bar_bits
    from brutex.engine.sweep_driver import DEFAULT_START_EQUITY_PAISA, metrics_for_mask
    from brutex.strategies.types import Side as _Side

    if not bars:
        raise ValueError("run_rescue_lane: bars must be non-empty")
    cfg = RescueConfig() if config is None else config
    equity = DEFAULT_START_EQUITY_PAISA if start_equity_paisa is None else start_equity_paisa

    bar_bits = build_bar_bits(bars, proximity=proximity, prior_n=prior_n)
    hits = count_supermasks(bar_bits, masks)
    band = [
        (mask, hit)
        for mask, hit in zip(masks, hits, strict=True)
        if cfg.rescue_min_hits <= hit < MIN_TRADES
    ]

    rescued: list[RescuedCandidate] = []
    for mask, hit in band:
        metrics = metrics_for_mask(
            bars,
            bar_bits,
            mask,
            direction=direction,
            tf_minutes=tf_minutes,
            stop_loss_paisa=stop_loss_paisa,
            take_profit_paisa=take_profit_paisa,
            start_equity_paisa=equity,
            reference_stamper=None,
        )
        if metrics.total_trades < 1:
            # A band mask whose signals produced zero completed trades has
            # nothing to validate — honestly absent, never a fabricated row.
            continue
        candidate = build_candidate_input(
            run_id=f"rescue-{mask:016x}",
            mask=mask,
            bars=bars,
            bar_bits=bar_bits,
            direction_label="bull" if direction is _Side.BUY else "bear",
            tf_minutes=tf_minutes,
            sl_paisa=stop_loss_paisa,
            tp_paisa=take_profit_paisa,
            start_equity_paisa=equity,
            lockbox_min_trades=cfg.lockbox_min_trades,
        )
        validation = _validate_candidate(
            candidate,
            n_trades=metrics.total_trades,
            n_wins=metrics.winning_trades,
            config=cfg,
        )
        rescued.append(
            RescuedCandidate(
                mask=mask,
                hits=hit,
                total_trades=metrics.total_trades,
                winning_trades=metrics.winning_trades,
                metrics=metrics,
                validation=validation,
            )
        )

    return RescueLaneResult(
        config=cfg,
        candidate_count=len(masks),
        band_count=len(band),
        simulated_count=len(band),
        rescued=tuple(rescued),
    )


def _finite_or_none(value: float | None) -> float | None:
    """JSON-safe float: ``None`` for ``None``/NaN/±inf (``allow_nan=False`` law)."""
    if value is None or not math.isfinite(value):
        return None
    return float(value)


def provisional_specialists_section(result: RescueLaneResult) -> dict[str, Any]:
    """Render the lane's ADDITIVE "provisional specialists" artifact section.

    The payload the top100 artifact carries under its emit-on-present
    ``provisional_specialists`` root key (DEC-STAGE1B-LOW-SUPPORT-RESCUE-001):
    every entry is tagged ``low_support: true`` and carries its validation
    verdicts verbatim — a SEPARATE section, never merged into ``entries``
    (rescued rows are NEVER main-ranked). Deterministic: entries in input
    (mask-emission) order, every float JSON-safe (``allow_nan=False`` — a
    ``-inf`` binomial log-p renders ``null``).
    """
    entries: list[dict[str, Any]] = []
    for row in result.rescued:
        validation = row.validation
        entries.append(
            {
                "mask": f"{row.mask:#x}",
                "low_support": True,
                "hits": row.hits,
                "total_trades": row.total_trades,
                "winning_trades": row.winning_trades,
                "sharpe": _finite_or_none(row.metrics.sharpe_ratio),
                "net_pnl_paisa": row.metrics.net_pnl_paisa,
                "binom_log_pvalue": _finite_or_none(validation.binom_log_pvalue),
                "binom_passed": validation.binom_passed,
                "lockbox_status": validation.lockbox_status,
                "lockbox_passed": validation.lockbox_passed,
                "mc_seed": validation.mc_seed,
                "mc_pvalue": _finite_or_none(validation.mc_pvalue),
                "mc_passed": validation.mc_passed,
                "validated": validation.validated,
                "fail_reasons": list(validation.fail_reasons),
            }
        )
    section: dict[str, Any] = {
        "schema_version": RESCUE_SECTION_SCHEMA_VERSION,
        "rescue_min_hits": result.config.rescue_min_hits,
        "main_min_hits": MIN_TRADES,
        "binom_alpha": result.config.binom_alpha,
        "counts": {
            "candidates_screened": result.candidate_count,
            "band": result.band_count,
            "simulated": result.simulated_count,
            "rescued": len(result.rescued),
            "validated": sum(1 for row in result.rescued if row.validation.validated),
        },
        "entries": entries,
    }
    return section


def rescue_section_or_none(
    result: RescueLaneResult | None,
) -> Mapping[str, Any] | None:
    """The artifact conduit: ``None`` stays ``None`` (OFF ⇒ no section key)."""
    if result is None:
        return None
    return provisional_specialists_section(result)
