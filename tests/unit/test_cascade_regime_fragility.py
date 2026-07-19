"""Unit tests for the default-OFF regime-fragility cascade filter #11
(DEC-REGIME-FRAGILITY-GATE-001).

Covers: the flag-OFF byte-stability laws (the filter never runs, the config
dict elides the key, verdict dicts carry NO new keys), the flag-ON decision
laws (measured ``fail`` rejects at ``FilterStage.REGIME_FRAGILITY``; the
evaluated AND the evidence-None ``insufficient_strata`` abstains pass through
non-rejected but VISIBLE), the config round-trip back-compat (an old manifest
dict lacking the key), the survivors-only scope, and the construction-time
axis/pnl length-coherence guard. The pure-law battery lives in
:mod:`tests.unit.test_regime_fragility`.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
import pytest
from brutex.engine import cascade_wire as cw
from brutex.metrics.cascade import (
    CandidateInput,
    CascadeConfig,
    FilterStage,
    run_stage4_cascade,
)
from brutex.metrics.regime_fragility import MIN_STRATUM_TRADES

# pyright: reportPrivateUsage=none


def _null_exit(positions: npt.NDArray[np.intp]) -> float:
    """A null exit-logic: random entries never beat a positive observed Sharpe."""
    return 0.0


def _strong_returns(seed: int, n: int = 32, edge: float = 1.0) -> tuple[float, ...]:
    """A strongly-positive (real-edge) return series → passes SPA / RW."""
    return tuple((np.random.default_rng(seed).standard_normal(n) + edge).tolist())


def _good_lockbox(seed: int) -> tuple[tuple[float, ...], tuple[int, ...]]:
    """A lockbox trade series whose Sharpe clears the (low) training reference."""
    rng = np.random.default_rng(seed)
    returns = tuple((rng.standard_normal(40) * 0.05 + 0.2).tolist())  # high Sharpe
    days = tuple(range(40))
    return returns, days


def _candidate(cid: str, *, seed: int = 0, **overrides: object) -> CandidateInput:
    """A candidate that, with :func:`_pass_config`, survives every stage."""
    lb_returns, lb_days = _good_lockbox(seed + 100)
    base: dict[str, object] = {
        "candidate_id": cid,
        "sharpe_ratio": 2.0,
        "sortino_ratio": 2.4,
        "calmar_ratio": 1.6,
        "profit_factor": 1.8,
        "win_rate": 0.55,
        "win_loss_ratio": 2.5,
        "recovery_factor": 2.1,
        "max_drawdown_pct": 12.0,
        "max_consecutive_losses": 4,
        "total_trades": 600,
        "training_sharpe": 0.1,
        "training_max_drawdown": 5.0,
        "skew": 0.0,
        "kurtosis": 3.0,
        "returns": _strong_returns(seed),
        "lockbox_returns": lb_returns,
        "lockbox_days": lb_days,
        "mc_n_bars": 400,
        "mc_n_entries": 20,
        "mc_evaluate": _null_exit,
    }
    base.update(overrides)
    return CandidateInput(**base)  # type: ignore[arg-type]


def _pass_config(**overrides: object) -> CascadeConfig:
    """A config permissive enough that a strong-edge candidate survives every stage."""
    base: dict[str, object] = {
        "seed": 7,
        "n_resamples": 200,
        "dsr_min": 0.0,
        "cpcv_min_pass_rate": 0.0,
        "cpcv_n_groups": 4,
        "cpcv_n_test_groups": 2,
        "pbo_max": 1.0,
        "pbo_n_splits": 8,
        "wfo_min_pass_rate": 0.0,
        "wfo_train_size": 10,
        "wfo_test_size": 6,
        "wfo_step": 6,
        "lockbox_min_trades": 4,
        "lockbox_default_window_days": 5,
        "mc_n_permutations": 100,
    }
    base.update(overrides)
    return CascadeConfig(**base)  # type: ignore[arg-type]


def _stratum_pnls(total: int, n: int = MIN_STRATUM_TRADES) -> tuple[int, ...]:
    """*n* all-positive pnls summing EXACTLY to *total*."""
    return (total - (n - 1),) + (1,) * (n - 1)


def _fragility_evidence(up_total: int, down_total: int) -> dict[str, object]:
    """Two-supported-stratum trend evidence with exact positive-P&L totals."""
    up = _stratum_pnls(up_total)
    down = _stratum_pnls(down_total)
    return {
        "trade_pnls_paisa": up + down,
        "trade_trend_tokens": ("up",) * len(up) + ("down",) * len(down),
    }


# ────── flag OFF (the default) — byte-stability laws ──────


def test_flag_off_default_filter_never_runs() -> None:
    cfg = _pass_config()
    assert cfg.regime_fragility is False
    # Even a candidate carrying evidence that WOULD fail flag-ON survives
    # untouched, no stage key, no verdict fields.
    rep = run_stage4_cascade([_candidate("a", seed=1, **_fragility_evidence(801, 199))], cfg)
    assert rep.survivor_ids == ("a",)
    assert FilterStage.REGIME_FRAGILITY.value not in rep.stage_survivors
    (v,) = rep.verdicts
    assert v.regime_fragility_status is None
    assert v.regime_fragility_stats is None


def test_flag_off_report_equals_pre_filter_shape() -> None:
    # The OFF-arm report is IDENTICAL whether the candidate carries the new
    # evidence fields or not (the filter block provably never executes).
    cfg = _pass_config()
    plain = run_stage4_cascade([_candidate("a", seed=1)], cfg)
    with_evidence = run_stage4_cascade(
        [_candidate("a", seed=1, **_fragility_evidence(801, 199))], cfg
    )
    assert plain == with_evidence


def test_config_round_trip_elides_the_default_off_key() -> None:
    cfg = cw.cascade_config_for_sweep("abcd")
    data = cw.cascade_config_to_dict(cfg)
    assert "regime_fragility" not in data  # default-elision (byte-stable manifest)
    assert cw.cascade_config_from_dict(data) == cfg


def test_config_from_old_manifest_dict_defaults_off() -> None:
    # Back-compat: a pre-filter manifest dict (no key) loads with the flag OFF.
    cfg = cw.cascade_config_for_sweep("abcd")
    data = cw.cascade_config_to_dict(cfg)
    assert "regime_fragility" not in data
    assert cw.cascade_config_from_dict(dict(data)).regime_fragility is False


def test_verdict_dict_carries_no_new_keys_when_off() -> None:
    rep = run_stage4_cascade([_candidate("a", seed=1)], _pass_config())
    (v,) = rep.verdicts
    payload = cw._verdict_to_dict(v)
    assert "regime_fragility_status" not in payload
    assert "regime_fragility_stats" not in payload


# ────── flag ON — decision laws ──────


def test_fail_rejects_at_the_stage_token() -> None:
    cfg = _pass_config(regime_fragility=True)
    rep = run_stage4_cascade([_candidate("a", seed=1, **_fragility_evidence(801, 199))], cfg)
    assert rep.survivor_ids == ()
    (v,) = rep.verdicts
    assert v.passed is False
    assert v.rejected_at is FilterStage.REGIME_FRAGILITY
    assert "regime_concentration:trend:up" in v.detail
    assert v.regime_fragility_status == "fail"
    assert v.regime_fragility_stats is not None
    assert rep.stage_survivors[FilterStage.REGIME_FRAGILITY.value] == 0


def test_ok_survives_with_visible_status_and_stats() -> None:
    cfg = _pass_config(regime_fragility=True)
    # EXACTLY 80.0% concentration passes (strict >) — the boundary at the
    # cascade seam.
    rep = run_stage4_cascade([_candidate("a", seed=1, **_fragility_evidence(800, 200))], cfg)
    assert rep.survivor_ids == ("a",)
    (v,) = rep.verdicts
    assert v.passed is True
    assert v.regime_fragility_status == "ok"
    stats = v.regime_fragility_stats
    assert stats is not None
    axes = stats["axes"]
    assert isinstance(axes, dict)
    assert axes["era"] == {"skipped": True, "reason": "no_input"}
    assert rep.stage_survivors[FilterStage.REGIME_FRAGILITY.value] == 1


def test_evaluated_abstain_passes_through_visible() -> None:
    cfg = _pass_config(regime_fragility=True)
    # A single supported stratum: the law EVALUATES and abstains — status
    # recorded WITH stats, candidate kept.
    evidence: dict[str, object] = {
        "trade_pnls_paisa": _stratum_pnls(500),
        "trade_trend_tokens": ("up",) * MIN_STRATUM_TRADES,
    }
    rep = run_stage4_cascade([_candidate("a", seed=1, **evidence)], cfg)
    assert rep.survivor_ids == ("a",)
    (v,) = rep.verdicts
    assert v.regime_fragility_status == "insufficient_strata"
    assert v.regime_fragility_stats is not None


def test_evidence_none_abstains_with_null_stats() -> None:
    cfg = _pass_config(regime_fragility=True)
    # No evidence fields at all (an old manifest / hand-built caller):
    # honest abstain — status recorded, stats None, never a reject.
    rep = run_stage4_cascade([_candidate("a", seed=1)], cfg)
    assert rep.survivor_ids == ("a",)
    (v,) = rep.verdicts
    assert v.regime_fragility_status == "insufficient_strata"
    assert v.regime_fragility_stats is None


def test_pnls_without_any_axis_abstains() -> None:
    cfg = _pass_config(regime_fragility=True)
    # P&L present but BOTH axes absent → the evidence-None abstain arm.
    rep = run_stage4_cascade([_candidate("a", seed=1, trade_pnls_paisa=(1, 2, 3))], cfg)
    (v,) = rep.verdicts
    assert v.regime_fragility_status == "insufficient_strata"
    assert v.regime_fragility_stats is None


def test_era_axis_alone_evaluates() -> None:
    cfg = _pass_config(regime_fragility=True)
    era_a = _stratum_pnls(900)
    era_b = _stratum_pnls(100)
    evidence: dict[str, object] = {
        "trade_pnls_paisa": era_a + era_b,
        "trade_era_labels": ("era_a",) * len(era_a) + ("era_b",) * len(era_b),
    }
    rep = run_stage4_cascade([_candidate("a", seed=1, **evidence)], cfg)
    assert rep.survivor_ids == ()
    (v,) = rep.verdicts
    assert v.rejected_at is FilterStage.REGIME_FRAGILITY
    assert "regime_concentration:era:era_a" in v.detail


def test_earlier_stage_reject_gains_no_fragility_status() -> None:
    # Scope is survivors-only: a hard-gate reject never reaches filter #11.
    cfg = _pass_config(regime_fragility=True)
    rep = run_stage4_cascade(
        [_candidate("x", seed=1, total_trades=1, **_fragility_evidence(801, 199))], cfg
    )
    (v,) = rep.verdicts
    assert v.rejected_at is FilterStage.HARD_GATES
    assert v.regime_fragility_status is None
    assert v.regime_fragility_stats is None


def test_axis_pnls_length_mismatch_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="trade_trend_tokens length"):
        _candidate("x", trade_trend_tokens=("up", "down"), trade_pnls_paisa=(1, 2, 3))
    with pytest.raises(ValueError, match="trade_era_labels length"):
        _candidate("x", trade_era_labels=("a",), trade_pnls_paisa=(1, 2, 3))


def test_verdict_dict_emits_status_and_stats_when_on() -> None:
    cfg = _pass_config(regime_fragility=True)
    rep = run_stage4_cascade([_candidate("a", seed=1, **_fragility_evidence(800, 200))], cfg)
    (v,) = rep.verdicts
    payload = cw._verdict_to_dict(v)
    assert payload["regime_fragility_status"] == "ok"
    stats = payload["regime_fragility_stats"]
    assert isinstance(stats, dict)
    assert stats["taxonomy_version"] == 1
    # The evidence-None abstain arm serialises status with null stats.
    rep2 = run_stage4_cascade([_candidate("y", seed=2)], cfg)
    (v2,) = rep2.verdicts
    payload2 = cw._verdict_to_dict(v2)
    assert payload2["regime_fragility_status"] == "insufficient_strata"
    assert payload2["regime_fragility_stats"] is None


def test_determinism_with_flag_on() -> None:
    cfg = _pass_config(regime_fragility=True)
    cands = [_candidate("a", seed=1, **_fragility_evidence(800, 200))]
    assert run_stage4_cascade(cands, cfg) == run_stage4_cascade(cands, cfg)


def test_off_config_replace_on_changes_report() -> None:
    # Anti-vacuity: the SAME candidates under flag ON vs OFF produce
    # different reports (the filter genuinely executes when armed).
    cands = [_candidate("a", seed=1, **_fragility_evidence(801, 199))]
    off = run_stage4_cascade(cands, _pass_config())
    on = run_stage4_cascade(cands, _pass_config(regime_fragility=True))
    assert off.survivor_ids == ("a",)
    assert on.survivor_ids == ()
    assert off != on
