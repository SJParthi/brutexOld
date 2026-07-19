"""cascade_wire (de)serialisation + threading of the regime-fragility
filter-#11 surface (DEC-REGIME-FRAGILITY-GATE-001).

Stem-matched companion of :mod:`tests.unit.test_engine_cascade_wire` for the
discovery coverage gate: pins the flag-ON arms of the DEFAULT-ELIDED
``regime_fragility`` config key, the EMIT-ON-PRESENT
``regime_fragility_status``/``regime_fragility_stats`` verdict keys, and the
``build_candidate_input`` evidence threading (per-trade ERA labels via the
taxonomy bisect; per-trade TREND tokens via the ONE production
``build_session_lens`` over per-session daily bars folded from the same bars
in hand). The full behaviour battery lives in
:mod:`tests.unit.test_cascade_regime_fragility` and the pure-law battery in
:mod:`tests.unit.test_regime_fragility`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import brutex.engine.cascade_wire as cw
from brutex.data.models import CandleBar
from brutex.engine.session_lens import build_session_lens
from brutex.metrics.cascade import CandidateVerdict
from brutex.metrics.regime_taxonomy import era_for_date

# pyright: reportPrivateUsage=none

_IST = timezone(timedelta(hours=5, minutes=30))


# ────── config / verdict serialisation ──────


def test_config_to_dict_keeps_flag_when_on_and_round_trips() -> None:
    cfg = cw.cascade_config_for_sweep("abcd", overrides={"regime_fragility": True})
    data = cw.cascade_config_to_dict(cfg)
    assert data["regime_fragility"] is True  # NOT elided when non-default
    assert cw.cascade_config_from_dict(data) == cfg


def test_verdict_to_dict_emits_status_with_stats_dict() -> None:
    verdict = CandidateVerdict(
        candidate_id="c1",
        passed=False,
        rejected_at=None,
        detail="regime fragility fail",
        regime_fragility_status="fail",
        regime_fragility_stats={"taxonomy_version": 1, "axes": {}},
    )
    payload = cw._verdict_to_dict(verdict)
    assert payload["regime_fragility_status"] == "fail"
    stats = payload["regime_fragility_stats"]
    assert stats == {"taxonomy_version": 1, "axes": {}}
    assert stats is not verdict.regime_fragility_stats  # defensive copy, never aliased


def test_verdict_to_dict_emits_status_with_none_stats() -> None:
    verdict = CandidateVerdict(
        candidate_id="c2",
        passed=True,
        rejected_at=None,
        detail="pass",
        regime_fragility_status="insufficient_strata",
        regime_fragility_stats=None,
    )
    payload = cw._verdict_to_dict(verdict)
    assert payload["regime_fragility_status"] == "insufficient_strata"
    assert payload["regime_fragility_stats"] is None


# ────── build_candidate_input evidence threading ──────


def _bar(day: int, minute: int, close: str) -> CandleBar:
    """One IST-session CandleBar (stored UTC) at 2024-01-``day`` 09:``minute``."""
    ts = datetime(2024, 1, day, 9, 15 + minute, tzinfo=_IST).astimezone(UTC)
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
    """Three 2024 sessions of six 1-minute bars each."""
    out: list[CandleBar] = []
    out.extend(_bar(2, m, "100.00") for m in range(6))
    out.extend(_bar(3, m, "103.00") for m in range(6))
    out.extend(_bar(4, m, "103.40") for m in range(6))
    return out


def _folded_daily(close: str, day: int) -> CandleBar:
    """The hand-folded daily bar the wire derives for one fixture session."""
    price = Decimal(close)
    return CandleBar(
        timestamp=datetime(2024, 1, day, 9, 15, tzinfo=_IST).astimezone(UTC),
        open=price,
        high=price + Decimal("1"),
        low=price - Decimal("1"),
        close=price,
        volume=600,
        open_interest=None,
    )


def _wire_candidate() -> tuple[object, list[CandleBar]]:
    from brutex.engine.stage1a import build_bar_bits

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
    return cand, bars


def _resimulated_trades(bars: list[CandleBar]) -> list[object]:
    from brutex.engine.combo_mask import mask_to_entry_signals
    from brutex.engine.stage1a import build_bar_bits
    from brutex.engine.trade_sim import simulate_trades
    from brutex.strategies.types import Side

    bar_bits = build_bar_bits(bars, proximity=None, prior_n=3)
    return list(
        simulate_trades(
            bars,
            mask_to_entry_signals(bar_bits, 0),
            direction=Side.BUY,
            tf_minutes=1,
            stop_loss_paisa=None,
            take_profit_paisa=None,
            reference_stamper=None,
        )
    )


def test_build_candidate_input_threads_era_labels_per_entry_date() -> None:
    cand, bars = _wire_candidate()
    labels = cand.trade_era_labels  # type: ignore[attr-defined]
    pnls = cand.trade_pnls_paisa  # type: ignore[attr-defined]
    assert labels is not None
    assert pnls is not None
    assert len(labels) == len(pnls) > 0
    # Every fixture entry date sits in [2021-09-01, 2024-11-14) — the
    # taxonomy's post_peak_margin_full era.
    assert era_for_date(date(2024, 1, 2)).label == "post_peak_margin_full"
    assert set(labels) == {"post_peak_margin_full"}
    # Index-for-index oracle against the SAME simulation the wire runs.
    trades = _resimulated_trades(bars)
    assert len(trades) == len(labels)
    for trade, label in zip(trades, labels, strict=True):
        entry_day = trade.entry_ts.astimezone(_IST).date()  # type: ignore[attr-defined]
        assert label == era_for_date(entry_day).label


def test_build_candidate_input_threads_trend_tokens_from_the_lens() -> None:
    cand, bars = _wire_candidate()
    tokens = cand.trade_trend_tokens  # type: ignore[attr-defined]
    assert tokens is not None
    # Independent oracle: the lens over the HAND-folded daily series the
    # wire derives (first open / max high / min low / last close / summed
    # volume, first-timestamp keyed). The lens `instrument` drives only
    # the expiry EVENT flag, never the trend fold, so any token matches.
    daily = (
        _folded_daily("100.00", 2),
        _folded_daily("103.00", 3),
        _folded_daily("103.40", 4),
    )
    lens = build_session_lens(daily, instrument="NIFTY")
    trades = _resimulated_trades(bars)
    assert len(trades) == len(tokens)
    for trade, token in zip(trades, tokens, strict=True):
        entry_day = trade.entry_ts.astimezone(_IST).date()  # type: ignore[attr-defined]
        assert token == lens.trend_by_date[entry_day].value
    # Three warm-up sessions never confirm a fractal swing (k = 2): the
    # detector honestly reads SIDEWAYS throughout — a deterministic pin.
    assert set(tokens) == {"sideways"}
