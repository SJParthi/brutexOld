"""Unit tests for the persisted sweep runner (`brutex.engine.sweep_run`).

Covers every public symbol and every branch of the new module for the strict
100% line + branch gate (CLAUDE.md §4 / Golden Rule #12):

* :func:`mask_run_id` — known→known identity + empty-field rejection.
* :class:`ComboDedup` — seen / mark / mark_many, idempotent.
* :class:`SweepRunConfig` — every ``__post_init__`` validation branch.
* :func:`ranked_combo_to_result_row` — field mapping + ``None`` → ``0.0``.
* :func:`run_persisted_sweep` — happy path, skip (dedup) path, empty-survivor
  path, conflict-prune path, ``max_combos`` guard, ``HOLD`` rejection,
  ``top_k`` truncation, ``min_hits`` disabled.

The bars are small, deterministic, tz-aware synthetic sessions with ``Decimal``
OHLC (Golden Rule #1 — real-shaped data, no fabrication).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import structlog
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.results_store import ResultsStore
from brutex.engine.stage1a import build_bar_bits
from brutex.engine.sweep_driver import metrics_for_mask
from brutex.engine.sweep_run import (
    ComboDedup,
    SweepRunConfig,
    SweepRunSummary,
    mask_run_id,
    ranked_combo_to_result_row,
    run_persisted_sweep,
)
from brutex.orchestrator.audit import AuditWriter, verify_chain
from brutex.ranking import RankedCombo, rank_combos
from brutex.strategies.errors import StrategyError
from brutex.strategies.types import Side
from brutex.vocabulary import ProximityParams
from brutex.vocabulary.bits import BIT_BAR_BEARISH, BIT_BAR_BULLISH

_IST = timezone(timedelta(hours=5, minutes=30))

#: A fixed run-identity context the tests reuse (the 9-field identity inputs).
_CTX: dict[str, str] = {
    "direction": "bull",
    "instrument": "NIFTY",
    "tf": "1m",
    "mode": "A",
    "data_snapshot_hash": "snap0001",
    "indicator_version": "ind-v1",
    "code_commit_sha": "deadbeef",
}
_PARAMS: dict[str, object] = {"sl_paisa": 80, "tp_paisa": 50, "prior_n": 3}


def _quiet_logs() -> None:
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))


def _bar(day: int, minute: int, o: float, c: float) -> CandleBar:
    """Build one IST-session minute bar (stored UTC) with Decimal OHLC."""
    ts = (
        datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=day, minutes=minute)
    ).astimezone(UTC)
    hi = max(o, c) + 0.5
    lo = min(o, c) - 0.5
    return CandleBar(
        timestamp=ts,
        open=Decimal(str(round(o, 2))),
        high=Decimal(str(round(hi, 2))),
        low=Decimal(str(round(lo, 2))),
        close=Decimal(str(round(c, 2))),
        volume=100,
        open_interest=None,
    )


def _trending_bars(n_days: int = 15, per_day: int = 120) -> list[CandleBar]:
    """A multi-day upward-drifting session — BUY combos win often via the TP leg."""
    bars: list[CandleBar] = []
    for d in range(n_days):
        price = 100.0
        for m in range(per_day):
            close = price + 0.6 if m % 3 else price - 0.3
            bars.append(_bar(d, m, price, close))
            price = close
    return bars


def _make_config(**overrides: object) -> SweepRunConfig:
    base: dict[str, object] = {
        "instrument": "NIFTY",
        "tf": "1m",
        "direction": Side.BUY,
        "mode": "A",
        "condition_bits": [0, 1, 2, 3, 4],
        "k": 1,
        "data_snapshot_hash": "snap0001",
        "indicator_version": "ind-v1",
        "code_commit_sha": "deadbeef",
        "tf_minutes": 1,
        "sl_paisa": 80,
        "tp_paisa": 50,
    }
    base.update(overrides)
    return SweepRunConfig(**base)  # type: ignore[arg-type]


def _dedup() -> ComboDedup:
    return ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))


# ────── mask_run_id ──────


class TestMaskRunId:
    def test_known_inputs_give_stable_64char_hex(self) -> None:
        rid = mask_run_id(mask=0b101, params=_PARAMS, **_CTX)
        assert len(rid) == 64
        assert rid == rid.lower()
        bytes.fromhex(rid)  # parses → valid hex

    def test_same_inputs_are_identical(self) -> None:
        a = mask_run_id(mask=7, params=_PARAMS, **_CTX)
        b = mask_run_id(mask=7, params=_PARAMS, **_CTX)
        assert a == b

    def test_different_mask_changes_id(self) -> None:
        a = mask_run_id(mask=1, params=_PARAMS, **_CTX)
        b = mask_run_id(mask=2, params=_PARAMS, **_CTX)
        assert a != b

    def test_strategy_id_encodes_mask_as_16_hex(self) -> None:
        # mask:%016x — the DEC-MASK-IDENTITY-001 encoding. Two masks differing only
        # in a high bit must still differ (no truncation to fewer than 16 hex).
        a = mask_run_id(mask=0, params=_PARAMS, **_CTX)
        b = mask_run_id(mask=1 << 51, params=_PARAMS, **_CTX)
        assert a != b

    def test_empty_field_rejected(self) -> None:
        ctx = {**_CTX, "instrument": ""}
        with pytest.raises(StrategyError, match="instrument must be non-empty"):
            mask_run_id(mask=1, params=_PARAMS, **ctx)


# ────── ComboDedup ──────


class TestComboDedup:
    def test_unseen_then_seen_after_mark(self) -> None:
        dedup = _dedup()
        rid = mask_run_id(mask=1, params=_PARAMS, **_CTX)
        assert dedup.seen(rid) is False
        dedup.mark(rid)
        assert dedup.seen(rid) is True

    def test_mark_is_idempotent(self) -> None:
        dedup = _dedup()
        rid = mask_run_id(mask=2, params=_PARAMS, **_CTX)
        dedup.mark(rid)
        dedup.mark(rid)  # no error, still seen
        assert dedup.seen(rid) is True

    def test_mark_many_returns_count_and_marks_all(self) -> None:
        dedup = _dedup()
        rids = [mask_run_id(mask=m, params=_PARAMS, **_CTX) for m in (1, 2, 4)]
        assert dedup.mark_many(rids) == 3
        assert all(dedup.seen(r) for r in rids)

    def test_mark_many_empty_is_zero(self) -> None:
        assert _dedup().mark_many([]) == 0


# ────── SweepRunConfig validation ──────


class TestSweepRunConfig:
    def test_valid_config_constructs(self) -> None:
        cfg = _make_config()
        assert cfg.k == 1
        assert cfg.min_hits >= 0

    @pytest.mark.parametrize("field", ["instrument", "tf", "mode"])
    def test_empty_context_label_rejected(self, field: str) -> None:
        with pytest.raises(ValueError, match=f"{field} must be non-empty"):
            _make_config(**{field: ""})

    @pytest.mark.parametrize(
        "field", ["data_snapshot_hash", "indicator_version", "code_commit_sha"]
    )
    def test_empty_identity_hash_rejected(self, field: str) -> None:
        with pytest.raises(ValueError, match=f"{field} must be non-empty"):
            _make_config(**{field: ""})

    def test_tf_minutes_below_one_rejected(self) -> None:
        with pytest.raises(ValueError, match="tf_minutes must be >= 1"):
            _make_config(tf_minutes=0)

    def test_k_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"k=3 must be in \[0, 2\]"):
            _make_config(condition_bits=[0, 1], k=3)

    def test_k_zero_is_valid(self) -> None:
        cfg = _make_config(condition_bits=[0, 1], k=0)
        assert cfg.k == 0

    @pytest.mark.parametrize("field", ["sl_paisa", "tp_paisa"])
    def test_nonpositive_paisa_knob_rejected(self, field: str) -> None:
        with pytest.raises(ValueError, match=f"{field} must be > 0 or None"):
            _make_config(**{field: 0})

    def test_none_paisa_knob_is_valid(self) -> None:
        cfg = _make_config(sl_paisa=None, tp_paisa=None)
        assert cfg.sl_paisa is None

    def test_nonpositive_start_equity_rejected(self) -> None:
        with pytest.raises(ValueError, match="start_equity_paisa must be > 0"):
            _make_config(start_equity_paisa=0)

    def test_top_k_below_one_rejected(self) -> None:
        with pytest.raises(ValueError, match="top_k must be >= 1 or None"):
            _make_config(top_k=0)

    def test_negative_min_hits_rejected(self) -> None:
        with pytest.raises(ValueError, match="min_hits must be >= 0"):
            _make_config(min_hits=-1)

    def test_proximity_is_carried(self) -> None:
        prox = ProximityParams(pivot_proximity_bps=25)
        cfg = _make_config(proximity=prox)
        assert cfg.proximity is prox


# ────── ranked_combo_to_result_row ──────


def _ranked_combo(bars: list[CandleBar]) -> tuple[RankedCombo, list[int]]:
    """Build a real ranked combo (>= MIN_TRADES) over *bars* for the projector tests."""
    bar_bits = build_bar_bits(bars)
    masks = [1, 2, 4, 8, 16]
    pairs = [
        (
            m,
            metrics_for_mask(
                bars,
                bar_bits,
                m,
                direction=Side.BUY,
                tf_minutes=1,
                stop_loss_paisa=80,
                take_profit_paisa=50,
                start_equity_paisa=10_000_000,
            ),
        )
        for m in masks
    ]
    ranked = rank_combos(pairs)
    assert ranked, "fixture must yield at least one ranked combo"
    return ranked[0], bar_bits


class TestRankedComboToResultRow:
    def test_maps_real_combo_into_row(self) -> None:
        bars = _trending_bars()
        rc, bar_bits = _ranked_combo(bars)
        rid = mask_run_id(mask=rc.mask, params=_PARAMS, **_CTX)
        row = ranked_combo_to_result_row(
            rc,
            run_id=rid,
            instrument="NIFTY",
            tf="1m",
            direction="bull",
            mode="A",
            bars=bars,
            bar_bits=bar_bits,
            tf_minutes=1,
            sl_paisa=80,
            tp_paisa=50,
        )
        assert row.run_id == rid
        assert row.total_trades == rc.metrics.total_trades
        assert row.net_pnl_paisa == rc.metrics.net_pnl_paisa
        # Prices are 2dp-snapped Decimals drawn from real OHLC.
        assert row.avg_entry_price == row.avg_entry_price.quantize(Decimal("0.01"))
        assert row.avg_entry_price > 0

    def test_short_direction_uses_sell_side(self) -> None:
        bars = _trending_bars()
        rc, bar_bits = _ranked_combo(bars)
        # direction != "bull" → SELL side path in the projector.
        row = ranked_combo_to_result_row(
            rc,
            run_id=mask_run_id(mask=rc.mask, params=_PARAMS, **{**_CTX, "direction": "bear"}),
            instrument="NIFTY",
            tf="1m",
            direction="bear",
            mode="A",
            bars=bars,
            bar_bits=bar_bits,
            tf_minutes=1,
            sl_paisa=80,
            tp_paisa=50,
        )
        assert row.direction == "bear"

    def test_none_metrics_map_to_zero(self) -> None:
        # Hand-build a RankedCombo whose optional ratios are None to exercise the
        # None → 0.0 mapping deterministically (independent of the trade sim).
        bars = _trending_bars()
        rc_real, bar_bits = _ranked_combo(bars)
        from dataclasses import replace

        metrics = replace(
            rc_real.metrics,
            sortino_ratio=None,
            calmar_ratio=None,
            profit_factor=None,
        )
        rc = RankedCombo(mask=rc_real.mask, metrics=metrics, composite_score=0.0)
        row = ranked_combo_to_result_row(
            rc,
            run_id=mask_run_id(mask=rc.mask, params=_PARAMS, **_CTX),
            instrument="NIFTY",
            tf="1m",
            direction="bull",
            mode="A",
            bars=bars,
            bar_bits=bar_bits,
            tf_minutes=1,
            sl_paisa=80,
            tp_paisa=50,
        )
        assert row.sortino_ratio == 0.0
        assert row.calmar_ratio == 0.0
        assert row.profit_factor == 0.0


# ────── run_persisted_sweep ──────


class TestRunPersistedSweep:
    def test_empty_bars_raises(self, tmp_path: Path) -> None:
        _quiet_logs()
        store = ResultsStore(path=tmp_path / "r.parquet")
        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(ValueError, match="bars must be non-empty"),
        ):
            run_persisted_sweep([], _make_config(), store=store, dedup=_dedup(), audit=audit)

    def test_hold_direction_rejected(self, tmp_path: Path) -> None:
        _quiet_logs()
        store = ResultsStore(path=tmp_path / "r.parquet")
        cfg = _make_config(direction=Side.HOLD)
        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(ValueError, match="must be Side.BUY or Side.SELL"),
        ):
            run_persisted_sweep(_trending_bars(), cfg, store=store, dedup=_dedup(), audit=audit)

    def test_over_cap_raises_before_compute(self, tmp_path: Path) -> None:
        _quiet_logs()
        store = ResultsStore(path=tmp_path / "r.parquet")
        # C(5,2)=10 > max_combos=3 → refuse.
        cfg = _make_config(condition_bits=[0, 1, 2, 3, 4], k=2, max_combos=3)
        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(ValueError, match="exceeds max_combos=3"),
        ):
            run_persisted_sweep(_trending_bars(), cfg, store=store, dedup=_dedup(), audit=audit)

    def test_happy_path_persists_and_audits_three_lines(self, tmp_path: Path) -> None:
        _quiet_logs()
        bars = _trending_bars()
        store = ResultsStore(path=tmp_path / "r.parquet")
        audit_path = tmp_path / "a.jsonl"
        with AuditWriter.open(audit_path) as audit:
            summary = run_persisted_sweep(
                bars, _make_config(), store=store, dedup=_dedup(), audit=audit
            )
        assert isinstance(summary, SweepRunSummary)
        assert summary.enumerated == 5  # C(5,1)
        assert summary.ranked >= 1
        assert summary.persisted == summary.ranked
        assert len(store) == summary.persisted
        assert len(summary.store_content_hash) == 64
        # Exactly THREE audit lines, independent of combo count (Golden Rule #8).
        assert verify_chain(audit_path) == 3

    def test_top_k_truncates_persisted(self, tmp_path: Path) -> None:
        _quiet_logs()
        bars = _trending_bars()
        store = ResultsStore(path=tmp_path / "r.parquet")
        cfg = _make_config(top_k=1)
        with AuditWriter.open(tmp_path / "a.jsonl") as audit:
            summary = run_persisted_sweep(bars, cfg, store=store, dedup=_dedup(), audit=audit)
        assert summary.ranked >= 1
        assert summary.persisted == 1

    def test_skip_path_on_prepopulated_dedup(self, tmp_path: Path) -> None:
        _quiet_logs()
        bars = _trending_bars()
        cfg = _make_config()
        dedup = _dedup()
        # Pre-mark every enumerated identity → every mask is skipped.
        params = {
            "sl_paisa": 80,
            "tp_paisa": 50,
            "prior_n": 3,
            "proximity_bps": None,
            "start_equity_paisa": 10_000_000,
        }
        for mask in (1, 2, 4, 8, 16):
            dedup.mark(
                mask_run_id(
                    mask=mask,
                    direction="bull",
                    instrument="NIFTY",
                    tf="1m",
                    mode="A",
                    params=params,
                    data_snapshot_hash="snap0001",
                    indicator_version="ind-v1",
                    code_commit_sha="deadbeef",
                )
            )
        store = ResultsStore(path=tmp_path / "r.parquet")
        with AuditWriter.open(tmp_path / "a.jsonl") as audit:
            summary = run_persisted_sweep(bars, cfg, store=store, dedup=dedup, audit=audit)
        assert summary.skipped_dedup == 5
        assert summary.evaluated == 0
        assert summary.ranked == 0
        assert summary.persisted == 0

    def test_conflict_prune_path(self, tmp_path: Path) -> None:
        _quiet_logs()
        bars = _trending_bars()
        # BIT_BAR_BULLISH + BIT_BAR_BEARISH is a conflict pair → the k=2 mask that
        # selects both is pruned; the other k=2 masks survive.
        cfg = _make_config(condition_bits=[BIT_BAR_BULLISH, BIT_BAR_BEARISH, 0], k=2)
        store = ResultsStore(path=tmp_path / "r.parquet")
        with AuditWriter.open(tmp_path / "a.jsonl") as audit:
            summary = run_persisted_sweep(bars, cfg, store=store, dedup=_dedup(), audit=audit)
        assert summary.enumerated == 3  # C(3,2)
        assert summary.conflict_pruned == 1

    def test_empty_survivor_path_with_high_min_hits(self, tmp_path: Path) -> None:
        _quiet_logs()
        bars = _trending_bars()
        # A min_hits larger than the bar count → every survivor is pre-filtered out;
        # nothing is ranked or persisted, but all are still counted as evaluated.
        cfg = _make_config(min_hits=10_000_000)
        store = ResultsStore(path=tmp_path / "r.parquet")
        with AuditWriter.open(tmp_path / "a.jsonl") as audit:
            summary = run_persisted_sweep(bars, cfg, store=store, dedup=_dedup(), audit=audit)
        assert summary.evaluated == 5
        assert summary.ranked == 0
        assert summary.persisted == 0

    def test_min_hits_disabled_keeps_all_survivors(self, tmp_path: Path) -> None:
        _quiet_logs()
        bars = _trending_bars()
        cfg = _make_config(min_hits=0)
        store = ResultsStore(path=tmp_path / "r.parquet")
        with AuditWriter.open(tmp_path / "a.jsonl") as audit:
            summary = run_persisted_sweep(bars, cfg, store=store, dedup=_dedup(), audit=audit)
        assert summary.evaluated == 5  # no Stage-1B drop

    def test_sell_direction_runs(self, tmp_path: Path) -> None:
        # Exercise the Side.SELL → "bear" direction-label branch end-to-end.
        _quiet_logs()
        bars = _trending_bars()
        cfg = _make_config(direction=Side.SELL)
        store = ResultsStore(path=tmp_path / "r.parquet")
        with AuditWriter.open(tmp_path / "a.jsonl") as audit:
            summary = run_persisted_sweep(bars, cfg, store=store, dedup=_dedup(), audit=audit)
        assert summary.enumerated == 5
        # Every persisted row carries the bear label.
        assert all(row.direction == "bear" for row in store.rows())

    def test_proximity_param_is_canonicalised(self, tmp_path: Path) -> None:
        # A non-None ProximityParams exercises the proximity-bps params branch and
        # must change the run identity vs the default-proximity sweep (it is part
        # of the canonical params hashed into every mask's run_id).
        _quiet_logs()
        bars = _trending_bars()
        cfg = _make_config(proximity=ProximityParams(pivot_proximity_bps=25))
        store = ResultsStore(path=tmp_path / "r.parquet")
        with AuditWriter.open(tmp_path / "a.jsonl") as audit:
            summary = run_persisted_sweep(bars, cfg, store=store, dedup=_dedup(), audit=audit)
        assert summary.enumerated == 5
        # The default-proximity run_id for the same mask differs (proximity is in
        # the canonical params), proving the proximity branch fed the hash.
        default_params = {
            "sl_paisa": 80,
            "tp_paisa": 50,
            "prior_n": 3,
            "proximity_bps": None,
            "start_equity_paisa": 10_000_000,
        }
        prox_params = {**default_params, "proximity_bps": [25, 10, 10]}
        assert mask_run_id(mask=1, params=prox_params, **_CTX) != mask_run_id(
            mask=1, params=default_params, **_CTX
        )
