"""Unit + integration battery for the low-support rescue lane (100% line+branch).

DEC-STAGE1B-LOW-SUPPORT-RESCUE-001 (LANE W3). Covers: the fail-loud env
knob's full truth table, the effective-Apriori-floor law, config validation,
the deterministic BLAKE3 MC seed, the BORN-RUST binomial conduit against an
EXACT ``fractions.Fraction`` oracle, the barrier-implied null law
``p0 = SL/(SL+TP)`` (the round-1 hostile-review respecification —
Fraction-checked derivation exactness, threading into the gate, the
ties-discard ``n = winners + losers``, the R1 falsifying case measured
honestly, the achievability boundary), every validation-battery arm
(binom/lockbox/MC pass·fail·error·staged-skip), the real-machinery
``run_rescue_lane`` pass over a synthetic lake slice (band classification
through the REAL popcount kernel + trade sim + reused cascade candidate
builder), the popcount-kernel REUSE spy (no new counting path), the
JSON-safe provisional-specialists section, and the structural OFF pin (no
production module binds the lane — byte-identity when OFF is structural).
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from brutex.data.models import CandleBar
from brutex.engine import rescue_lane
from brutex.engine.rescue_lane import (
    RESCUE_MIN_HITS_DEFAULT,
    RESCUE_MIN_HITS_ENV,
    RescueConfig,
    RescuedCandidate,
    RescueLaneResult,
    RescueValidation,
    barrier_null_win_probability,
    binomial_tail_log_pvalue,
    effective_apriori_min_hits,
    provisional_specialists_section,
    rescue_mc_seed,
    rescue_section_or_none,
    resolve_rescue_min_hits_env,
    run_rescue_lane,
)
from brutex.metrics.cascade import CandidateInput
from brutex.ranking import MIN_TRADES
from brutex.strategies.types import Side

_IST = timezone(timedelta(hours=5, minutes=30))


# ── Env knob (fail-loud truth table) ─────────────────────────────────────────


class TestResolveEnvKnob:
    def test_unset_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(RESCUE_MIN_HITS_ENV, raising=False)
        assert resolve_rescue_min_hits_env() is None

    def test_blank_is_off(self) -> None:
        assert resolve_rescue_min_hits_env("") is None
        assert resolve_rescue_min_hits_env("   ") is None

    def test_zero_is_explicit_off(self) -> None:
        assert resolve_rescue_min_hits_env("0") is None

    def test_valid_floor_arms(self) -> None:
        assert resolve_rescue_min_hits_env("8") == 8
        assert resolve_rescue_min_hits_env(" 1 ") == 1
        assert resolve_rescue_min_hits_env(str(MIN_TRADES - 1)) == MIN_TRADES - 1

    def test_env_read_at_call_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(RESCUE_MIN_HITS_ENV, "12")
        assert resolve_rescue_min_hits_env() == 12

    @pytest.mark.parametrize("junk", ["eight", "8.5", "1e2", "--", "0x8"])
    def test_junk_fails_loud(self, junk: str) -> None:
        with pytest.raises(ValueError, match=RESCUE_MIN_HITS_ENV):
            resolve_rescue_min_hits_env(junk)

    @pytest.mark.parametrize("out", ["-1", str(MIN_TRADES), "999"])
    def test_out_of_range_fails_loud(self, out: str) -> None:
        with pytest.raises(ValueError, match="strictly BELOW|must be"):
            resolve_rescue_min_hits_env(out)


# ── Effective Apriori floor (the conscious supersession law) ─────────────────


class TestEffectiveAprioriMinHits:
    def test_off_is_byte_identical_floor(self) -> None:
        assert effective_apriori_min_hits(MIN_TRADES, None) == MIN_TRADES
        assert effective_apriori_min_hits(5, None) == 5

    def test_armed_tracks_the_rescue_floor(self) -> None:
        assert effective_apriori_min_hits(MIN_TRADES, RESCUE_MIN_HITS_DEFAULT) == 8
        assert effective_apriori_min_hits(MIN_TRADES, 1) == 1

    @pytest.mark.parametrize("bad", [0, -3, MIN_TRADES, MIN_TRADES + 1])
    def test_armed_floor_must_sit_below_main(self, bad: int) -> None:
        with pytest.raises(ValueError, match="rescue_min_hits must be in"):
            effective_apriori_min_hits(MIN_TRADES, bad)

    def test_boundary_one_below_main_is_legal(self) -> None:
        assert effective_apriori_min_hits(MIN_TRADES, MIN_TRADES - 1) == MIN_TRADES - 1


# ── RescueConfig validation ──────────────────────────────────────────────────


class TestRescueConfig:
    def test_defaults_are_the_ruled_values(self) -> None:
        cfg = RescueConfig()
        assert cfg.rescue_min_hits == 8
        assert cfg.binom_alpha == 1e-4
        assert cfg.mc_permutations == 1000
        assert cfg.mc_skip_if_failed is True

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("rescue_min_hits", 0),
            ("rescue_min_hits", MIN_TRADES),
            ("binom_alpha", 0.0),
            ("binom_alpha", 1.0),
            ("mc_permutations", 0),
            ("mc_alpha", 0.0),
            ("mc_alpha", 1.5),
            ("lockbox_min_trades", 0),
        ],
    )
    def test_out_of_band_knob_refused(self, field: str, value: float) -> None:
        with pytest.raises(ValueError, match=f"RescueConfig.{field}"):
            RescueConfig(**{field: value})  # type: ignore[arg-type]


# ── Deterministic MC seed ────────────────────────────────────────────────────


class TestRescueMcSeed:
    def test_deterministic_and_31_bit(self) -> None:
        a = rescue_mc_seed("rescue-00000000000000ff")
        assert a == rescue_mc_seed("rescue-00000000000000ff")
        assert 0 <= a < 2**31

    def test_distinct_ids_distinct_seeds(self) -> None:
        seeds = {rescue_mc_seed(f"rescue-{mask:016x}") for mask in range(32)}
        assert len(seeds) == 32  # a collision here would be astonishing


# ── The BORN-RUST binomial conduit vs the EXACT Fraction oracle ──────────────


def _exact_tail_log_half(n: int, k: int) -> float:
    """ln P(X >= k | Binomial(n, 1/2)) via EXACT integer arithmetic."""
    tail = Fraction(sum(math.comb(n, i) for i in range(k, n + 1)), 2**n)
    return math.log(tail)


def _exact_tail_log(n: int, k: int, p: Fraction) -> float:
    """EXACT ln P(X >= k | Binomial(n, p)) via ``Fraction`` arithmetic."""
    if k > n:
        return float("-inf")
    q = 1 - p
    tail = sum(math.comb(n, i) * p**i * q ** (n - i) for i in range(k, n + 1))
    return math.log(tail) if tail < 1 else 0.0


class TestBinomialTailConduit:
    @pytest.mark.parametrize(
        ("n", "k"), [(1, 1), (5, 3), (10, 10), (20, 20), (29, 22), (29, 29), (30, 25)]
    )
    def test_matches_exact_fraction_oracle_at_half(self, n: int, k: int) -> None:
        got = binomial_tail_log_pvalue(n, k, null_p=0.5)
        assert got == pytest.approx(_exact_tail_log_half(n, k), abs=1e-10)

    def test_certain_and_empty_tails(self) -> None:
        assert binomial_tail_log_pvalue(10, 0, null_p=0.5) == 0.0
        assert binomial_tail_log_pvalue(10, 11, null_p=0.5) == float("-inf")

    def test_kernel_validation_propagates(self) -> None:
        with pytest.raises(ValueError):
            binomial_tail_log_pvalue(-1, 0, null_p=0.5)
        with pytest.raises(ValueError):
            binomial_tail_log_pvalue(5, 2, null_p=float("nan"))

    def test_general_p_reference(self) -> None:
        # P(X >= 2 | n=3, p=1/4) = 10/64 exactly.
        assert binomial_tail_log_pvalue(3, 2, null_p=0.25) == pytest.approx(
            math.log(10 / 64), abs=1e-12
        )


# ── The barrier-implied null p0 = SL/(SL+TP) (round-1 respecification) ───────


class TestBarrierNullWinProbability:
    @pytest.mark.parametrize(
        ("sl", "tp", "expect"),
        [
            (20, 300, Fraction(20, 320)),  # tight SL / wide TP ⇒ small p0
            (80, 50, Fraction(80, 130)),  # the synthetic-lake fixture's cell
            (100, 100, Fraction(1, 2)),  # symmetric barriers ⇒ the fair coin
            (5, 500, Fraction(5, 505)),  # Mode-A extreme corner
        ],
    )
    def test_derivation_exactness_fraction_checked(
        self, sl: int, tp: int, expect: Fraction
    ) -> None:
        # int/int true division is correctly rounded — EXACTLY float(Fraction).
        assert barrier_null_win_probability(sl, tp) == float(expect)

    @pytest.mark.parametrize(
        ("sl", "tp"), [(None, 50), (80, None), (None, None), (0, 50), (80, 0), (-5, 50), (80, -1)]
    )
    def test_missing_or_nonpositive_barriers_fail_loud(
        self, sl: int | None, tp: int | None
    ) -> None:
        with pytest.raises(ValueError, match="barrier_null_win_probability"):
            barrier_null_win_probability(sl, tp)


# ── Validation battery arms (hand-built candidates) ──────────────────────────


def _candidate(
    *,
    sharpe: float = 2.0,
    training_sharpe: float | None = None,
    mc_value: float = 0.0,
    n_entries: int = 5,
    returns: tuple[float, ...] | None = None,
    days: tuple[int, ...] | None = None,
    trade_days: tuple[int, ...] | None = None,
) -> CandidateInput:
    rets = returns if returns is not None else (0.01, -0.002, 0.012, -0.001, 0.011, 0.002)
    day0 = datetime(2026, 1, 5, tzinfo=UTC).date().toordinal()
    ords = days if days is not None else tuple(day0 + i for i in range(len(rets)))
    tdays = trade_days if trade_days is not None else ords
    if rets:
        from brutex.metrics.cpcv import sharpe_of_returns

        lb_sharpe = sharpe_of_returns(rets)
        assert lb_sharpe is not None
    else:
        lb_sharpe = 1.0  # the empty-series ERROR arm never reads it
    return CandidateInput(
        candidate_id="rescue-0000000000000001",
        sharpe_ratio=sharpe,
        sortino_ratio=1.0,
        calmar_ratio=1.0,
        profit_factor=2.0,
        win_rate=0.8,
        win_loss_ratio=2.0,
        recovery_factor=1.0,
        max_drawdown_pct=1.0,
        max_consecutive_losses=1,
        total_trades=len(tdays),
        training_sharpe=lb_sharpe if training_sharpe is None else training_sharpe,
        training_max_drawdown=0.5,
        skew=0.0,
        kurtosis=3.0,
        returns=rets,
        lockbox_returns=rets,
        lockbox_days=ords,
        mc_n_bars=50,
        mc_n_entries=n_entries,
        mc_evaluate=lambda _positions: mc_value,
        lockbox_trade_days=tdays,
    )


def _cfg(**overrides: Any) -> RescueConfig:
    base: dict[str, Any] = {"mc_permutations": 199, "lockbox_min_trades": 5}
    base.update(overrides)
    return RescueConfig(**base)


class TestValidateCandidate:
    def test_all_gates_pass(self) -> None:
        # binom: 20/20 wins at p=.5 → p = 2^-20 ≈ 9.5e-7 < 1e-4.
        verdict = rescue_lane._validate_candidate(
            _candidate(), n_trades=20, n_wins=20, null_p=0.5, config=_cfg()
        )
        assert verdict.binom_passed is True
        assert verdict.lockbox_passed is True, verdict.fail_reasons
        assert verdict.mc_passed is True
        assert verdict.validated is True
        assert verdict.fail_reasons == ()
        # p = (1 + 0) / (199 + 1) = 0.005 < 0.01 — every permutation loses.
        assert verdict.mc_pvalue == pytest.approx(1 / 200)

    def test_binom_fail_stages_out_mc(self) -> None:
        verdict = rescue_lane._validate_candidate(
            _candidate(), n_trades=10, n_wins=5, null_p=0.5, config=_cfg()
        )
        assert verdict.binom_passed is False
        assert verdict.mc_pvalue is None
        assert verdict.mc_passed is None
        assert verdict.validated is False
        assert any("binomial tail" in reason for reason in verdict.fail_reasons)
        assert any("staged behind" in reason for reason in verdict.fail_reasons)

    def test_mc_runs_when_staging_disabled(self) -> None:
        verdict = rescue_lane._validate_candidate(
            _candidate(),
            n_trades=10,
            n_wins=5,
            null_p=0.5,
            config=_cfg(mc_skip_if_failed=False),
        )
        assert verdict.binom_passed is False
        assert verdict.mc_passed is True  # MC itself passes; conjunction still fails
        assert verdict.validated is False

    def test_mc_fail_arm(self) -> None:
        verdict = rescue_lane._validate_candidate(
            _candidate(mc_value=99.0),  # every permutation beats the observed
            n_trades=20,
            n_wins=20,
            null_p=0.5,
            config=_cfg(),
        )
        assert verdict.mc_passed is False
        assert verdict.validated is False
        assert any("Monte-Carlo p" in reason for reason in verdict.fail_reasons)

    def test_mc_error_arm(self) -> None:
        verdict = rescue_lane._validate_candidate(
            _candidate(n_entries=0),  # invalid → monte_carlo raises ValueError
            n_trades=20,
            n_wins=20,
            null_p=0.5,
            config=_cfg(),
        )
        assert verdict.mc_passed is False
        assert verdict.validated is False
        assert any("Monte-Carlo could not be evaluated" in r for r in verdict.fail_reasons)

    def test_lockbox_fail_arm(self) -> None:
        # A wildly better training reference ⇒ holdout degradation ⇒ FAIL.
        verdict = rescue_lane._validate_candidate(
            _candidate(training_sharpe=50.0),
            n_trades=20,
            n_wins=20,
            null_p=0.5,
            config=_cfg(),
        )
        assert verdict.lockbox_passed is False
        assert verdict.validated is False
        assert any(reason.startswith("lockbox ") for reason in verdict.fail_reasons)

    def test_lockbox_error_arm(self) -> None:
        # Empty lockbox series ⇒ the reused machinery raises ⇒ named fail.
        verdict = rescue_lane._validate_candidate(
            _candidate(returns=(), days=(), trade_days=()),
            n_trades=20,
            n_wins=20,
            null_p=0.5,
            config=_cfg(),
        )
        assert verdict.lockbox_status == "error"
        assert verdict.lockbox_passed is False
        assert any("lockbox could not be evaluated" in r for r in verdict.fail_reasons)

    def test_lockbox_insufficient_n_is_a_named_fail(self) -> None:
        verdict = rescue_lane._validate_candidate(
            _candidate(trade_days=()),  # zero holdout trades vs floor 5
            n_trades=20,
            n_wins=20,
            null_p=0.5,
            config=_cfg(),
        )
        assert verdict.lockbox_passed is False
        assert any("lockbox insufficient" in r for r in verdict.fail_reasons)

    def test_seed_is_the_candidate_law(self) -> None:
        verdict = rescue_lane._validate_candidate(
            _candidate(), n_trades=20, n_wins=20, null_p=0.5, config=_cfg()
        )
        assert verdict.mc_seed == rescue_mc_seed("rescue-0000000000000001")


# ── The barrier-implied null's gate behaviour (round-1 respecification) ──────


class TestBarrierNullGateBehaviour:
    """The R1 falsifying case + the achievability boundary — pinned honestly."""

    def test_r1_falsifying_case_measured_honestly(self) -> None:
        """The round-1 falsifier: 3 wins of +300 vs 7 losses of −20 (a
        genuinely profitable low-win-rate specialist). Under the REFUTED
        p=0.5 frequency null the tail read 0.9453 — an auto-reject blind to
        the edge. Under the barrier-implied null p0 = 20/(20+300) = 1/16 the
        SAME record measures tail ≈ 0.0210 (ln ≈ −3.863): still >= alpha at
        n=10 — HONESTLY "insufficient evidence at this n", never a relaxed
        alpha — but the measured p now reflects the real edge, and a
        stronger record of the same profile (7 wins / 3 losses, tail
        ≈ 3.78e-7) genuinely PASSES, which p=0.5 made impossible below n=14."""
        p0 = barrier_null_win_probability(20, 300)
        assert p0 == float(Fraction(1, 16))
        verdict = rescue_lane._validate_candidate(
            _candidate(), n_trades=10, n_wins=3, null_p=p0, config=_cfg()
        )
        assert verdict.binom_log_pvalue == pytest.approx(
            _exact_tail_log(10, 3, Fraction(1, 16)), abs=1e-9
        )
        assert math.exp(verdict.binom_log_pvalue) == pytest.approx(0.0210071, abs=1e-6)
        assert verdict.binom_passed is False  # n=10 with 3 wins: not enough evidence
        # The refuted null read 0.9453 for the SAME record — pinned so the
        # respec can never silently regress to the frequency null.
        assert math.exp(_exact_tail_log_half(10, 3)) == pytest.approx(0.9453125, abs=1e-12)
        # A strong small-n record of the same barrier profile PASSES.
        strong = rescue_lane._validate_candidate(
            _candidate(), n_trades=10, n_wins=7, null_p=p0, config=_cfg()
        )
        assert strong.binom_log_pvalue == pytest.approx(
            _exact_tail_log(10, 7, Fraction(1, 16)), abs=1e-9
        )
        assert strong.binom_passed is True

    def test_achievability_boundary_at_small_p0(self) -> None:
        """min attainable tail = p0^n (all wins) ⇒ passable iff
        n >= ceil(ln(alpha)/ln(p0)); at p0 = 1/16, alpha = 1e-4 that is
        n >= 4: an all-win n=4 record passes (1/16⁴ ≈ 1.53e-5), an all-win
        n=3 record cannot (1/4096 ≈ 2.44e-4) — by design (insufficient
        evidence), never a defect."""
        p0 = float(Fraction(1, 16))
        log_alpha = math.log(1e-4)
        assert binomial_tail_log_pvalue(4, 4, null_p=p0) < log_alpha
        assert binomial_tail_log_pvalue(3, 3, null_p=p0) >= log_alpha

    def test_high_p0_perfect_records_stay_unpassable_by_design(self) -> None:
        """The historical p=0.5 defect (no n<14 record could pass) is now a
        DESIGN property of high-p0 cells only: at symmetric barriers
        (p0=0.5) an all-win n=13 record still cannot pass (2^-13 ≈ 1.22e-4)
        while n=14 can — with a wide-SL/tight-TP cell (large p0) wins carry
        little evidence and that is the honest verdict."""
        log_alpha = math.log(1e-4)
        assert binomial_tail_log_pvalue(13, 13, null_p=0.5) >= log_alpha
        assert binomial_tail_log_pvalue(14, 14, null_p=0.5) < log_alpha


# ── run_rescue_lane over a real synthetic slice ──────────────────────────────


def _bars(sessions: int, per_session: int) -> list[CandleBar]:
    """Gap-up minute bars (the worst-case-rankable recipe, parity-fixture shape)."""
    out: list[CandleBar] = []
    for day in range(sessions):
        price = 100.0
        for minute in range(per_session):
            o = price + 3.0
            c, h, lo = o + 0.3, o + 0.4, o - 0.1
            ts = (
                datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=day, minutes=minute)
            ).astimezone(UTC)
            out.append(
                CandleBar(
                    timestamp=ts,
                    open=Decimal(str(round(o, 2))),
                    high=Decimal(str(round(h, 2))),
                    low=Decimal(str(round(lo, 2))),
                    close=Decimal(str(round(c, 2))),
                    volume=100,
                    open_interest=None,
                )
            )
            price = c
    return out


#: Bits 0/1 are a conflict pair in the 60-vocab (mutually exclusive per bar),
#: so their conjunction fires on ZERO bars — the deterministic below-floor mask.
_NEVER_MASK = (1 << 0) | (1 << 1)


class TestRunRescueLane:
    def test_empty_bars_refused(self) -> None:
        with pytest.raises(ValueError, match="bars must be non-empty"):
            run_rescue_lane([], [0], direction=Side.BUY, tf_minutes=1)

    def test_band_classification_and_rescue(self) -> None:
        bars = _bars(2, 10)  # 20 bars: mask 0 fires on every bar → hits=20 ∈ [8, 30)
        result = run_rescue_lane(
            bars,
            [0, _NEVER_MASK],
            direction=Side.BUY,
            tf_minutes=1,
            stop_loss_paisa=80,
            take_profit_paisa=50,
            config=_cfg(mc_permutations=25),
        )
        assert result.candidate_count == 2
        assert result.band_count == 1  # the never-mask (hits 0) is below floor
        assert result.simulated_count == 1
        assert len(result.rescued) == 1
        row = result.rescued[0]
        assert row.mask == 0
        assert row.hits == 20
        assert row.low_support is True
        assert row.total_trades >= 1
        assert 0 <= row.winning_trades <= row.total_trades
        # The validation ran end-to-end through the REAL reused machinery.
        assert isinstance(row.validation, RescueValidation)
        assert math.isfinite(row.validation.binom_log_pvalue) or (
            row.validation.binom_log_pvalue == float("-inf")
        )

    def test_above_band_masks_are_never_rescued(self) -> None:
        bars = _bars(4, 10)  # 40 bars: mask 0 hits 40 >= MIN_TRADES → main lane's
        result = run_rescue_lane(
            bars,
            [0],
            direction=Side.BUY,
            tf_minutes=1,
            stop_loss_paisa=80,
            take_profit_paisa=50,
            config=_cfg(mc_permutations=5),
        )
        assert result.band_count == 0
        assert result.rescued == ()

    def test_band_boundaries_are_exact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """hits == rescue floor is IN; hits == MIN_TRADES is OUT (main lane)."""
        bars = _bars(2, 10)
        recorded: list[list[int]] = []
        from brutex.engine import combo_mask

        real = combo_mask.count_supermasks

        def fake(bar_bits: Sequence[int], masks: Sequence[int]) -> list[int]:
            recorded.append(list(masks))
            # Three probes: at-floor (in), floor-1 (out), MIN_TRADES (out).
            return [RESCUE_MIN_HITS_DEFAULT, RESCUE_MIN_HITS_DEFAULT - 1, MIN_TRADES][: len(masks)]

        monkeypatch.setattr(combo_mask, "count_supermasks", fake)
        result = run_rescue_lane(
            bars,
            [0, 0, 0],
            direction=Side.BUY,
            tf_minutes=1,
            stop_loss_paisa=80,
            take_profit_paisa=50,
            config=_cfg(mc_permutations=5),
        )
        monkeypatch.setattr(combo_mask, "count_supermasks", real)
        assert recorded  # the kernel path was consulted
        assert result.band_count == 1
        assert len(result.rescued) == 1
        assert result.rescued[0].hits == RESCUE_MIN_HITS_DEFAULT

    def test_support_counting_reuses_the_stage1b_kernel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The REUSE spy: the lane counts through combo_mask.count_supermasks —
        the existing Stage-1B popcount kernel — never a private counting path."""
        bars = _bars(2, 10)
        calls: list[int] = []
        from brutex.engine import combo_mask

        real = combo_mask.count_supermasks

        def spy(bar_bits: Sequence[int], masks: Sequence[int]) -> list[int]:
            calls.append(len(masks))
            return real(bar_bits, masks)

        monkeypatch.setattr(combo_mask, "count_supermasks", spy)
        run_rescue_lane(
            bars,
            [0, _NEVER_MASK],
            direction=Side.BUY,
            tf_minutes=1,
            stop_loss_paisa=80,
            take_profit_paisa=50,
            config=_cfg(mc_permutations=5),
        )
        assert calls == [2]  # exactly ONE batched kernel call over all masks

    def test_zero_trade_band_mask_is_honestly_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bars = _bars(2, 10)
        from brutex.engine import sweep_driver

        real = sweep_driver.metrics_for_mask

        def zero_trades(*args: Any, **kwargs: Any) -> Any:
            metrics = real(*args, **kwargs)
            from dataclasses import replace

            return replace(metrics, total_trades=0)

        monkeypatch.setattr(sweep_driver, "metrics_for_mask", zero_trades)
        result = run_rescue_lane(
            bars,
            [0],
            direction=Side.BUY,
            tf_minutes=1,
            stop_loss_paisa=80,
            take_profit_paisa=50,
            config=_cfg(mc_permutations=5),
        )
        assert result.band_count == 1
        assert result.rescued == ()  # simulated, zero trades ⇒ no fabricated row

    def test_default_config_and_equity(self) -> None:
        bars = _bars(2, 10)
        result = run_rescue_lane(
            bars,
            [_NEVER_MASK],  # empty band ⇒ the cheap default-path walk
            direction=Side.BUY,
            tf_minutes=1,
            stop_loss_paisa=80,
            take_profit_paisa=50,
        )
        assert result.config == RescueConfig()
        assert result.band_count == 0

    @pytest.mark.parametrize(
        ("sl", "tp"), [(None, 50), (80, None), (None, None), (0, 50), (80, -1)]
    )
    def test_missing_or_nonpositive_barriers_refused(self, sl: int | None, tp: int | None) -> None:
        """The lane is undefined without both barriers (the binomial null is
        barrier-implied) — fail-loud BEFORE any counting/simulation."""
        with pytest.raises(ValueError, match="barrier_null_win_probability"):
            run_rescue_lane(
                _bars(1, 10),
                [0],
                direction=Side.BUY,
                tf_minutes=1,
                stop_loss_paisa=sl,
                take_profit_paisa=tp,
            )

    def test_null_p_is_barrier_implied_and_threaded(self) -> None:
        """The M-a mutant killer: the gate's null is p0 = SL/(SL+TP) of the
        run's OWN barriers (80/130 here), never a 0.5 constant — the rescued
        row's binom log-p equals the EXACT Fraction oracle at p0 and differs
        from the 0.5-null value."""
        bars = _bars(2, 10)
        result = run_rescue_lane(
            bars,
            [0, _NEVER_MASK],
            direction=Side.BUY,
            tf_minutes=1,
            stop_loss_paisa=80,
            take_profit_paisa=50,
            config=_cfg(mc_permutations=25),
        )
        p0 = Fraction(80, 130)
        assert result.binom_null_p == float(p0)
        row = result.rescued[0]
        n = row.winning_trades + row.metrics.losing_trades
        k = row.winning_trades
        assert n >= 1 and k >= 1  # anti-vacuous: a real decided record
        want = _exact_tail_log(n, k, p0)
        assert row.validation.binom_log_pvalue == pytest.approx(want, abs=1e-9)
        # The 0.5-constant mutant produces a materially different value.
        assert row.validation.binom_log_pvalue != pytest.approx(
            _exact_tail_log_half(n, k), abs=1e-6
        )

    def test_scratch_trades_discarded_from_sign_test(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The M-b mutant killer: n = winners + losers — a scratch
        (pnl == 0) trade changes NOTHING in the binomial gate. Plant metrics
        with total=5, wins=3, losses=1 (one scratch): the gate must compute
        at (n=4, k=3), never (n=5, k=3)."""
        bars = _bars(2, 10)
        from brutex.engine import sweep_driver

        real = sweep_driver.metrics_for_mask

        def with_scratch(*args: Any, **kwargs: Any) -> Any:
            from dataclasses import replace

            metrics = real(*args, **kwargs)
            return replace(metrics, total_trades=5, winning_trades=3, losing_trades=1)

        monkeypatch.setattr(sweep_driver, "metrics_for_mask", with_scratch)
        result = run_rescue_lane(
            bars,
            [0],
            direction=Side.BUY,
            tf_minutes=1,
            stop_loss_paisa=80,
            take_profit_paisa=50,
            config=_cfg(mc_permutations=5),
        )
        p0 = Fraction(80, 130)
        row = result.rescued[0]
        assert row.validation.binom_log_pvalue == pytest.approx(_exact_tail_log(4, 3, p0), abs=1e-9)
        # The ties-inflated mutant (n = total_trades = 5) reads differently.
        assert row.validation.binom_log_pvalue != pytest.approx(_exact_tail_log(5, 3, p0), abs=1e-6)


# ── Provisional-specialists section (the artifact payload) ───────────────────


def _result_for_section() -> RescueLaneResult:
    bars = _bars(2, 10)
    return run_rescue_lane(
        bars,
        [0, _NEVER_MASK],
        direction=Side.BUY,
        tf_minutes=1,
        stop_loss_paisa=80,
        take_profit_paisa=50,
        config=_cfg(mc_permutations=25),
    )


class TestProvisionalSpecialistsSection:
    def test_section_shape_and_tags(self) -> None:
        result = _result_for_section()
        section = provisional_specialists_section(result)
        assert section["schema_version"] == 1
        assert section["rescue_min_hits"] == 8
        assert section["main_min_hits"] == MIN_TRADES
        # The barrier-implied null provenance (round-1 respecification).
        assert section["binom_null_p"] == float(Fraction(80, 130))
        counts = section["counts"]
        assert counts["candidates_screened"] == 2
        assert counts["band"] == 1
        assert counts["rescued"] == 1
        entries = section["entries"]
        assert isinstance(entries, list) and len(entries) == 1
        entry = entries[0]
        assert entry["low_support"] is True
        assert entry["mask"] == "0x0"
        assert isinstance(entry["fail_reasons"], list)
        # Decided-trade transparency: wins AND losses surface, so the reader
        # can recompute the gate's n = wins + losses (scratch trades are the
        # difference to total_trades).
        assert entry["losing_trades"] >= 0
        assert entry["winning_trades"] + entry["losing_trades"] <= entry["total_trades"]

    def test_section_is_json_safe_allow_nan_false(self) -> None:
        section = provisional_specialists_section(_result_for_section())
        # The top100 writer's exact rendering law must accept it verbatim.
        rendered = json.dumps(
            section, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        )
        assert '"low_support":true' in rendered

    def test_neg_inf_binom_renders_null(self) -> None:
        result = _result_for_section()
        row = result.rescued[0]
        poisoned = RescueLaneResult(
            config=result.config,
            binom_null_p=result.binom_null_p,
            candidate_count=1,
            band_count=1,
            simulated_count=1,
            rescued=(
                RescuedCandidate(
                    mask=row.mask,
                    hits=row.hits,
                    total_trades=row.total_trades,
                    winning_trades=row.winning_trades,
                    metrics=row.metrics,
                    validation=RescueValidation(
                        binom_log_pvalue=float("-inf"),
                        binom_passed=True,
                        lockbox_status="pass",
                        lockbox_passed=True,
                        mc_seed=1,
                        mc_pvalue=None,
                        mc_passed=None,
                        validated=False,
                        fail_reasons=("x",),
                    ),
                ),
            ),
        )
        section = provisional_specialists_section(poisoned)
        entry = section["entries"][0]  # type: ignore[index]
        assert entry["binom_log_pvalue"] is None
        json.dumps(section, allow_nan=False)  # never a ValueError

    def test_conduit_none_passthrough(self) -> None:
        assert rescue_section_or_none(None) is None
        section = rescue_section_or_none(_result_for_section())
        assert section is not None and section["schema_version"] == 1


# ── Structural OFF pin — no production binding, byte-identity is structural ──


def test_no_production_module_imports_the_rescue_lane() -> None:
    """OFF ⇒ byte-identical is STRUCTURAL: no module under brutex/ imports
    rescue_lane (the production binding is the club-fold step, MIGRATION.md
    #1977) — so an unarmed sweep provably cannot execute one rescue opcode."""
    import_tokens = (
        "from brutex.engine.rescue_lane",
        "import brutex.engine.rescue_lane",
        "from brutex.engine import rescue_lane",
        "from .rescue_lane",
        "from . import rescue_lane",
    )
    package_root = Path(rescue_lane.__file__).resolve().parents[2] / "brutex"
    offenders = [
        str(path)
        for path in package_root.rglob("*.py")
        if path.name != "rescue_lane.py"
        and any(token in path.read_text(encoding="utf-8") for token in import_tokens)
    ]
    assert offenders == []  # docstring cross-references are fine; IMPORTS are not
