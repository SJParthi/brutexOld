"""Unit tests for the default-OFF extreme-vol must-survive cascade filter #10
(DEC-REGIME-MUST-SURVIVE-STRATUM-001).

Covers: the flag-OFF byte-stability laws (the filter never runs, the config
dict elides the key, verdict dicts carry NO new keys), the flag-ON decision
laws (measured ``fail`` rejects at ``FilterStage.EXTREME_VOL_STRATUM``; both
ABSTAIN statuses pass through non-rejected but VISIBLE), the config
round-trip back-compat (an old manifest dict lacking the key), and the
``build_candidate_input`` threading (bars → daily closes → per-trade entry
flags + pnls, aligned in trade order).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import numpy.typing as npt
import pytest
from brutex.data.models import CandleBar
from brutex.engine import cascade_wire as cw
from brutex.metrics.cascade import (
    CandidateInput,
    CascadeConfig,
    FilterStage,
    run_stage4_cascade,
)
from brutex.metrics.extreme_vol_stratum import (
    MIN_STRATUM_TRADES,
    classify_extreme_sessions,
)

# pyright: reportPrivateUsage=none

_IST = timezone(timedelta(hours=5, minutes=30))


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


def _stratum_evidence(
    *, stratum_pnl: int, n_stratum: int = MIN_STRATUM_TRADES, calm_pnls: tuple[int, ...] = (50, 150)
) -> dict[str, object]:
    """CandidateInput overrides carrying *n_stratum* extreme trades + calm ones."""
    flags = (True,) * n_stratum + (False,) * len(calm_pnls)
    pnls = (stratum_pnl,) * n_stratum + calm_pnls
    return {"extreme_vol_entry_flags": flags, "trade_pnls_paisa": pnls}


# ────── flag OFF (the default) — byte-stability laws ──────


def test_flag_off_default_filter_never_runs() -> None:
    cfg = _pass_config()
    assert cfg.extreme_vol_stratum is False
    # Even a candidate carrying evidence that WOULD fail flag-ON survives
    # untouched, no stage key, no verdict fields.
    rep = run_stage4_cascade([_candidate("a", seed=1, **_stratum_evidence(stratum_pnl=-500))], cfg)
    assert rep.survivor_ids == ("a",)
    assert FilterStage.EXTREME_VOL_STRATUM.value not in rep.stage_survivors
    (v,) = rep.verdicts
    assert v.extreme_vol_status is None
    assert v.extreme_vol_stats is None


def test_flag_off_report_equals_pre_filter_shape() -> None:
    # The OFF-arm report is IDENTICAL whether the candidate carries the new
    # evidence fields or not (the filter block provably never executes).
    cfg = _pass_config()
    plain = run_stage4_cascade([_candidate("a", seed=1)], cfg)
    with_evidence = run_stage4_cascade(
        [_candidate("a", seed=1, **_stratum_evidence(stratum_pnl=-500))], cfg
    )
    assert plain == with_evidence


def test_config_round_trip_elides_the_default_off_key() -> None:
    cfg = cw.cascade_config_for_sweep("abcd")
    data = cw.cascade_config_to_dict(cfg)
    assert "extreme_vol_stratum" not in data  # default-elision (byte-stable manifest)
    assert cw.cascade_config_from_dict(data) == cfg


def test_config_round_trip_emits_the_flag_when_on() -> None:
    cfg = cw.cascade_config_for_sweep("abcd", overrides={"extreme_vol_stratum": True})
    data = cw.cascade_config_to_dict(cfg)
    assert data["extreme_vol_stratum"] is True
    assert cw.cascade_config_from_dict(data) == cfg


def test_config_from_old_manifest_dict_defaults_off() -> None:
    # Back-compat: a pre-filter manifest dict (no key) loads with the flag OFF.
    cfg = cw.cascade_config_for_sweep("abcd")
    data = cw.cascade_config_to_dict(cfg)
    assert "extreme_vol_stratum" not in data
    assert cw.cascade_config_from_dict(dict(data)).extreme_vol_stratum is False


def test_verdict_dict_carries_no_new_keys_when_off() -> None:
    rep = run_stage4_cascade([_candidate("a", seed=1)], _pass_config())
    (v,) = rep.verdicts
    payload = cw._verdict_to_dict(v)
    assert "extreme_vol_status" not in payload
    assert "extreme_vol_stats" not in payload


# ────── flag ON — decision laws ──────


def test_fail_rejects_at_the_stage_token() -> None:
    cfg = _pass_config(extreme_vol_stratum=True)
    rep = run_stage4_cascade(
        [_candidate("x", seed=1, **_stratum_evidence(stratum_pnl=-100))], cfg
    )
    (v,) = rep.verdicts
    assert v.passed is False
    assert v.rejected_at is FilterStage.EXTREME_VOL_STRATUM
    assert v.extreme_vol_status == "fail"
    assert "expectancy" in v.detail
    assert rep.stage_survivors[FilterStage.EXTREME_VOL_STRATUM.value] == 0
    assert rep.survivor_ids == ()


def test_ok_survives_with_visible_status_and_stats() -> None:
    cfg = _pass_config(extreme_vol_stratum=True)
    rep = run_stage4_cascade(
        [_candidate("x", seed=1, **_stratum_evidence(stratum_pnl=100))], cfg
    )
    (v,) = rep.verdicts
    assert v.passed is True
    assert v.extreme_vol_status == "ok"
    stats = v.extreme_vol_stats
    assert stats is not None
    assert stats["stratum_trades"] == MIN_STRATUM_TRADES
    assert stats["calm_trades"] == 2
    assert stats["stratum_expectancy_paisa"] == 100.0
    assert rep.stage_survivors[FilterStage.EXTREME_VOL_STRATUM.value] == 1


def test_unproven_abstain_passes_through_visible() -> None:
    # No evidence fields (None) → data unavailable → recorded abstain, NOT a
    # reject (coordinator-confirmed semantics).
    cfg = _pass_config(extreme_vol_stratum=True)
    rep = run_stage4_cascade([_candidate("x", seed=1)], cfg)
    (v,) = rep.verdicts
    assert v.passed is True
    assert v.extreme_vol_status == "unproven_current_regime"
    assert v.extreme_vol_stats is None  # nothing measurable without evidence
    assert rep.stage_survivors[FilterStage.EXTREME_VOL_STRATUM.value] == 1


def test_zero_stratum_trades_abstain_with_stats() -> None:
    # Evidence present but NO extreme-session trades → the measured
    # "unproven_current_regime" abstain WITH its stats recorded.
    cfg = _pass_config(extreme_vol_stratum=True)
    overrides = {
        "extreme_vol_entry_flags": (False,) * 4,
        "trade_pnls_paisa": (100, -50, 25, 10),
    }
    rep = run_stage4_cascade([_candidate("x", seed=1, **overrides)], cfg)
    (v,) = rep.verdicts
    assert v.passed is True
    assert v.extreme_vol_status == "unproven_current_regime"
    assert v.extreme_vol_stats is not None
    assert v.extreme_vol_stats["stratum_trades"] == 0
    assert v.extreme_vol_stats["calm_trades"] == 4


def test_insufficient_abstain_passes_through_visible() -> None:
    cfg = _pass_config(extreme_vol_stratum=True)
    rep = run_stage4_cascade(
        [_candidate("x", seed=1, **_stratum_evidence(stratum_pnl=-999, n_stratum=5))], cfg
    )
    (v,) = rep.verdicts
    assert v.passed is True  # 5 < MIN_STRATUM_TRADES → abstain even on losses
    assert v.extreme_vol_status == "insufficient_stratum_trades"
    assert v.extreme_vol_stats is not None
    assert v.extreme_vol_stats["stratum_trades"] == 5


def test_earlier_stage_reject_gains_no_stratum_status() -> None:
    # Scope is survivors-only: a hard-gate reject never reaches filter #10.
    cfg = _pass_config(extreme_vol_stratum=True)
    rep = run_stage4_cascade(
        [_candidate("x", seed=1, total_trades=1, **_stratum_evidence(stratum_pnl=-100))], cfg
    )
    (v,) = rep.verdicts
    assert v.rejected_at is FilterStage.HARD_GATES
    assert v.extreme_vol_status is None
    assert v.extreme_vol_stats is None


def test_flags_pnls_length_mismatch_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="extreme_vol_entry_flags length"):
        _candidate(
            "x",
            extreme_vol_entry_flags=(True, False),
            trade_pnls_paisa=(1, 2, 3),
        )


def test_verdict_dict_emits_status_and_stats_when_on() -> None:
    cfg = _pass_config(extreme_vol_stratum=True)
    rep = run_stage4_cascade(
        [_candidate("x", seed=1, **_stratum_evidence(stratum_pnl=100))], cfg
    )
    (v,) = rep.verdicts
    payload = cw._verdict_to_dict(v)
    assert payload["extreme_vol_status"] == "ok"
    stats = payload["extreme_vol_stats"]
    assert isinstance(stats, dict)
    assert stats["stratum_trades"] == MIN_STRATUM_TRADES
    # The abstain-without-evidence arm serialises status with null stats.
    rep2 = run_stage4_cascade([_candidate("y", seed=2)], cfg)
    (v2,) = rep2.verdicts
    payload2 = cw._verdict_to_dict(v2)
    assert payload2["extreme_vol_status"] == "unproven_current_regime"
    assert payload2["extreme_vol_stats"] is None


# ────── build_candidate_input threading (bars → flags + pnls) ──────


def _bar(day: int, minute: int, close: str, month: int = 1, year: int = 2024) -> CandleBar:
    """One IST-session CandleBar (stored UTC) at 2024-``month``-``day`` 09:``minute``."""
    ts = datetime(year, month, day, 9, 15 + minute, tzinfo=_IST).astimezone(UTC)
    price = Decimal(close)
    return CandleBar(
        timestamp=ts,
        open=price,
        high=price + Decimal("1"),
        low=price - Decimal("1"),
        close=price,
        volume=100,
        open_interest=None,
    )


def _multi_day_bars() -> list[CandleBar]:
    """Three 2024 sessions: day 3 closes +3% vs day 2 (EXTREME), day 4 is calm."""
    out: list[CandleBar] = []
    # 2024-01-02: closes at 100.00
    out.extend(_bar(2, m, "100.00") for m in range(6))
    # 2024-01-03: closes at 103.00 → +3.00% → extreme session
    out.extend(_bar(3, m, "103.00") for m in range(6))
    # 2024-01-04: closes at 103.40 → +0.39% → calm session
    out.extend(_bar(4, m, "103.40") for m in range(6))
    return out


def test_build_candidate_input_threads_flags_and_pnls() -> None:
    from datetime import date

    bars = _multi_day_bars()
    spec_kwargs: dict[str, object] = {
        "instrument": "NIFTY",
        "tf_label": "1minute",
        "tf_minutes": 1,
        "direction_label": "bull",
        "sl_paisa": None,
        "tp_paisa": None,
        "prior_n": 3,
        "proximity_bps": None,
        "start_equity_paisa": 10_000_000,
        # mask 0 matches every bar → entries throughout all three sessions.
        "winners": ((f"{1:064x}", 0),),
    }
    spec = cw.CascadeTupleSpec(**spec_kwargs)  # type: ignore[arg-type]
    (cand,) = cw.candidates_for_spec(spec, bars)

    flags = cand.extreme_vol_entry_flags
    pnls = cand.trade_pnls_paisa
    assert flags is not None
    assert pnls is not None
    assert len(flags) == len(pnls) == cand.total_trades > 0

    # Independent oracle: the daily-close series the wire derives is the last
    # close per IST session — classify it here and re-derive the expected
    # per-trade flag from each trade's entry session.
    expected_extreme = classify_extreme_sessions(
        {date(2024, 1, 2): 100.0, date(2024, 1, 3): 103.0, date(2024, 1, 4): 103.4}
    )
    assert expected_extreme == frozenset({date(2024, 1, 3)})
    # At least one trade enters on the extreme session and one on a calm one
    # (mask 0 signals every bar across all three sessions).
    assert any(flags)
    assert not all(flags)


def test_build_candidate_input_flags_match_trade_entry_sessions() -> None:
    # Re-run the SAME simulation the wire runs and pin flag[i] ==
    # (trade i's ENTRY session ∈ extreme sessions) index-for-index.
    from datetime import date

    from brutex.engine.combo_mask import mask_to_entry_signals
    from brutex.engine.stage1a import build_bar_bits
    from brutex.engine.trade_sim import simulate_trades
    from brutex.strategies.types import Side

    bars = _multi_day_bars()
    bar_bits = build_bar_bits(bars, proximity=None, prior_n=3)
    cand = cw.build_candidate_input(
        run_id=f"{1:064x}",
        mask=0,
        bars=bars,
        bar_bits=bar_bits,
        direction_label="bull",
        tf_minutes=1,
        sl_paisa=None,
        tp_paisa=None,
        start_equity_paisa=10_000_000,
    )
    trades = simulate_trades(
        bars,
        mask_to_entry_signals(bar_bits, 0),
        direction=Side.BUY,
        tf_minutes=1,
        stop_loss_paisa=None,
        take_profit_paisa=None,
        reference_stamper=None,
    )
    assert cand.extreme_vol_entry_flags is not None
    assert cand.trade_pnls_paisa is not None
    assert len(trades) == len(cand.extreme_vol_entry_flags)
    extreme = frozenset({date(2024, 1, 3)})
    for trade, flag, pnl in zip(
        trades, cand.extreme_vol_entry_flags, cand.trade_pnls_paisa, strict=True
    ):
        assert flag == (trade.entry_ts.astimezone(_IST).date() in extreme)
        assert pnl == trade.pnl_paisa


def test_determinism_with_flag_on() -> None:
    cfg = _pass_config(extreme_vol_stratum=True)
    cands = [_candidate("a", seed=1, **_stratum_evidence(stratum_pnl=100))]
    assert run_stage4_cascade(cands, cfg) == run_stage4_cascade(cands, cfg)


def test_off_config_replace_on_changes_report() -> None:
    # Sanity that the flag actually gates: same candidate, OFF vs ON differ
    # exactly in the recorded stage key + verdict fields.
    cand = _candidate("a", seed=1, **_stratum_evidence(stratum_pnl=100))
    off = run_stage4_cascade([cand], _pass_config())
    on = run_stage4_cascade([cand], replace(_pass_config(), extreme_vol_stratum=True))
    assert off != on
    assert off.survivor_ids == on.survivor_ids == ("a",)
