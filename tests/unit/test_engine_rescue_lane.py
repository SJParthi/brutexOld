"""Unit + integration battery for the low-support rescue lane (100% line+branch).

DEC-STAGE1B-LOW-SUPPORT-RESCUE-001 (LANE W3). Covers: the fail-loud env
knob's full truth table, the effective-Apriori-floor law, config validation,
the deterministic BLAKE3 MC seed, the BORN-RUST binomial conduit against an
EXACT ``fractions.Fraction`` oracle, every validation-battery arm
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
            ("binom_null_p", 0.0),
            ("binom_null_p", 1.0),
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


class TestBinomialTailConduit:
    @pytest.mark.parametrize(
        ("n", "k"), [(1, 1), (5, 3), (10, 10), (20, 20), (29, 22), (29, 29), (30, 25)]
    )
    def test_matches_exact_fraction_oracle_at_half(self, n: int, k: int) -> None:
        got = binomial_tail_log_pvalue(n, k)
        assert got == pytest.approx(_exact_tail_log_half(n, k), abs=1e-10)

    def test_certain_and_empty_tails(self) -> None:
        assert binomial_tail_log_pvalue(10, 0) == 0.0
        assert binomial_tail_log_pvalue(10, 11) == float("-inf")

    def test_kernel_validation_propagates(self) -> None:
        with pytest.raises(ValueError):
            binomial_tail_log_pvalue(-1, 0)
        with pytest.raises(ValueError):
            binomial_tail_log_pvalue(5, 2, null_p=float("nan"))

    def test_general_p_reference(self) -> None:
        # P(X >= 2 | n=3, p=1/4) = 10/64 exactly.
        assert binomial_tail_log_pvalue(3, 2, null_p=0.25) == pytest.approx(
            math.log(10 / 64), abs=1e-12
        )


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
            _candidate(), n_trades=20, n_wins=20, config=_cfg()
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
            _candidate(), n_trades=10, n_wins=5, config=_cfg()
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
            config=_cfg(),
        )
        assert verdict.lockbox_passed is False
        assert any("lockbox insufficient" in r for r in verdict.fail_reasons)

    def test_seed_is_the_candidate_law(self) -> None:
        verdict = rescue_lane._validate_candidate(
            _candidate(), n_trades=20, n_wins=20, config=_cfg()
        )
        assert verdict.mc_seed == rescue_mc_seed("rescue-0000000000000001")


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
        )
        assert result.config == RescueConfig()
        assert result.band_count == 0


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
