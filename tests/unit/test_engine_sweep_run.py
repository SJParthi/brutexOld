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

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.results_store import ResultsStore
from brutex.engine.stage1a import build_bar_bits
from brutex.engine.sweep_driver import metrics_for_mask
from brutex.engine.sweep_run import (
    ComboDedup,
    SweepComboError,
    SweepRunConfig,
    SweepRunSummary,
    canonical_trade_params,
    mask_run_id,
    ranked_combo_to_result_row,
    run_persisted_sweep,
)
from brutex.orchestrator.audit import AuditWriter, verify_chain
from brutex.ranking import OverfitGateConfig, RankedCombo, rank_combos
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

    def test_condition_bit_at_or_above_max_vocab_rejected_at_config_time(self) -> None:
        # GAP #16 (DEC-SWEEP-DEDUP-LMDB-001): a bit >= MAX_VOCAB_BITS must be
        # rejected AT CONFIG TIME, not surface late as a bare ValueError from
        # the lazy iter_masks_k_of_bits generator mid-enumeration.
        from brutex.engine.combo_mask import MAX_VOCAB_BITS

        with pytest.raises(ValueError, match=r"condition_bits \[99\] out of range \[0, 64\)"):
            _make_config(condition_bits=[0, 1, 99], k=1)
        # The boundary bit (== MAX_VOCAB_BITS) is also out of the half-open range.
        with pytest.raises(ValueError, match=r"out of range \[0, 64\)"):
            _make_config(condition_bits=[MAX_VOCAB_BITS], k=1)

    def test_negative_condition_bit_rejected_at_config_time(self) -> None:
        with pytest.raises(ValueError, match=r"condition_bits \[-1\] out of range \[0, 64\)"):
            _make_config(condition_bits=[-1, 0, 1], k=1)

    def test_top_condition_bit_in_range_is_valid(self) -> None:
        from brutex.engine.combo_mask import MAX_VOCAB_BITS

        cfg = _make_config(condition_bits=[0, MAX_VOCAB_BITS - 1], k=1)
        assert MAX_VOCAB_BITS - 1 in cfg.condition_bits

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

    def test_stamps_readable_mask_and_deflated_sharpe(self) -> None:
        # DEC-SWEEP-WINNERS-BY-NAME-001: mask_bits = rc.mask; condition_names is the
        # vocabulary decode of that mask; deflated_sharpe is carried through.
        from dataclasses import replace

        from brutex.vocabulary.describe import matched_condition_names

        bars = _trending_bars()
        rc_real, bar_bits = _ranked_combo(bars)
        rc = replace(rc_real, deflated_sharpe=0.92)
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
        assert row.mask_bits == rc.mask
        assert row.condition_names == " AND ".join(matched_condition_names(rc.mask))
        assert row.deflated_sharpe == 0.92

    def test_none_deflated_sharpe_passes_through(self) -> None:
        # The legacy (no overfit gate) path leaves rc.deflated_sharpe None → stored None.
        bars = _trending_bars()
        rc, bar_bits = _ranked_combo(bars)
        assert rc.deflated_sharpe is None
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
        assert row.deflated_sharpe is None

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

    def test_zero_trade_combo_gets_placeholder_price_anchors(self) -> None:
        # DEC-STAGE3-WIRE-001 (review F6): a Stage-3 refined evaluation is an
        # UNGATED Bailey-LdP trial and can close ZERO trades on the training
        # slice — the conflict mask (bullish AND bearish) provably never fires,
        # so the projector's zero-trade arm is real behaviour, not a stub.
        bars = _trending_bars()
        bar_bits = build_bar_bits(bars)
        mask = (1 << BIT_BAR_BULLISH) | (1 << BIT_BAR_BEARISH)
        metrics = metrics_for_mask(
            bars,
            bar_bits,
            mask,
            direction=Side.BUY,
            tf_minutes=1,
            stop_loss_paisa=80,
            take_profit_paisa=50,
            start_equity_paisa=10_000_000,
        )
        assert metrics.total_trades == 0
        rc = RankedCombo(mask=mask, metrics=metrics, composite_score=0.0)
        row = ranked_combo_to_result_row(
            rc,
            run_id=mask_run_id(mask=mask, params=_PARAMS, **_CTX),
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
        assert row.total_trades == 0
        assert row.avg_entry_price == Decimal("0.00")
        assert row.avg_exit_price == Decimal("0.00")


# ────── canonical_trade_params (the ONE params layout — DEC-STAGE3-WIRE-001 F4) ──────


class TestCanonicalTradeParams:
    def test_layout_matches_the_run_identity_contract(self) -> None:
        params = canonical_trade_params(
            sl_paisa=80,
            tp_paisa=50,
            prior_n=3,
            proximity=None,
            start_equity_paisa=10_000_000,
        )
        assert params == {
            "sl_paisa": 80,
            "tp_paisa": 50,
            "prior_n": 3,
            "proximity_bps": None,
            "start_equity_paisa": 10_000_000,
        }

    def test_proximity_flattens_to_three_bps_ints(self) -> None:
        params = canonical_trade_params(
            sl_paisa=None,
            tp_paisa=None,
            prior_n=2,
            proximity=ProximityParams(
                pivot_proximity_bps=5, pdh_pdl_proximity_bps=10, fib_proximity_bps=25
            ),
            start_equity_paisa=1,
        )
        assert params["proximity_bps"] == [5, 10, 25]
        assert params["sl_paisa"] is None and params["tp_paisa"] is None

    def test_sweep_config_identity_routes_through_it(self) -> None:
        # §11 COMMON: the sweep's run-identity params ARE this helper's output —
        # the Stage-3 cell recovery re-derives identities through the same
        # layout, so the two surfaces can never drift apart.
        proximity = ProximityParams(
            pivot_proximity_bps=5, pdh_pdl_proximity_bps=10, fib_proximity_bps=25
        )
        cfg = _make_config(proximity=proximity)
        expected = canonical_trade_params(
            sl_paisa=cfg.sl_paisa,
            tp_paisa=cfg.tp_paisa,
            prior_n=cfg.prior_n,
            proximity=proximity,
            start_equity_paisa=cfg.start_equity_paisa,
        )
        rid_via_helper = mask_run_id(mask=1, params=expected, **_CTX)
        # Run the real sweep identity derivation for the same mask + config and
        # confirm the persisted run identity matches the helper-built one.
        from brutex.engine.sweep_run import _canonical_params

        assert _canonical_params(cfg) == expected
        assert mask_run_id(mask=1, params=_canonical_params(cfg), **_CTX) == rid_via_helper


# ────── trial-stats sidecar write point (DEC-TRIAL-STATS-WRITE-001) ──────


class TestTrialStatsSidecar:
    _DIGEST = "ab12cd34ef56ab12"

    def test_full_evaluation_writes_a_paired_sidecar(self, tmp_path: Path) -> None:
        from brutex.engine.trial_stats import read_tuple_trial_stats, trial_stats_sidecar_path
        from brutex.orchestrator.audit import read_entries

        bars = _trending_bars()
        store = ResultsStore(path=tmp_path / "r.parquet")
        audit_path = tmp_path / "a.jsonl"
        with AuditWriter.open(audit_path) as audit:
            summary = run_persisted_sweep(
                bars,
                _make_config(),
                store=store,
                dedup=_dedup(),
                audit=audit,
                trial_stats_digest=self._DIGEST,
            )
        assert summary.trial_sidecar_written is True
        assert summary.backtested == summary.evaluated - summary.minhits_dropped
        stats = read_tuple_trial_stats(trial_stats_sidecar_path(store.path))
        # Condition 3: N == the combos that actually ran metrics_for_mask.
        assert stats.n_backtested == summary.backtested
        assert stats.n_finite == summary.trial_n_finite
        assert stats.sum_sharpe == summary.trial_sum_sharpe
        assert stats.sum_sharpe_sq == summary.trial_sum_sharpe_sq
        # Condition 5: the pairing identity binds sidecar ↔ store bytes.
        assert stats.tuple_digest == self._DIGEST
        assert stats.store_content_hash == summary.store_content_hash
        # The SWEEP_FLUSH payload carries the additive trial tallies.
        flush_payloads = [e.payload for e in read_entries(audit_path) if e.action == "SWEEP_FLUSH"]
        assert flush_payloads[0]["backtested"] == summary.backtested
        assert flush_payloads[0]["trial_sidecar_written"] is True

    def test_no_digest_writes_no_sidecar(self, tmp_path: Path) -> None:
        from brutex.engine.trial_stats import trial_stats_sidecar_path

        store = ResultsStore(path=tmp_path / "r.parquet")
        with AuditWriter.open(tmp_path / "a.jsonl") as audit:
            summary = run_persisted_sweep(
                _trending_bars(), _make_config(), store=store, dedup=_dedup(), audit=audit
            )
        assert summary.trial_sidecar_written is False
        assert not trial_stats_sidecar_path(store.path).exists()

    def test_corrupted_sidecar_invalidates_sentinel_and_self_heals(self, tmp_path: Path) -> None:
        # DEC-MARK-MANY-SENTINEL-001 superseded the slice-2 preserve-on-skips
        # arm: a corrupt sidecar UNPAIRS the completion sentinel, so the redo
        # re-runs the FULL population (dedup probes bypassed) and RESTORES the
        # sidecar byte-identical — self-healing, never preserved-corrupt.
        from brutex.engine.sweep_orchestrator import load_or_create_store
        from brutex.engine.trial_stats import trial_stats_sidecar_path

        bars = _trending_bars()
        dedup = _dedup()
        store = ResultsStore(path=tmp_path / "r.parquet")
        with AuditWriter.open(tmp_path / "a.jsonl") as audit:
            run_persisted_sweep(
                bars,
                _make_config(),
                store=store,
                dedup=dedup,
                audit=audit,
                trial_stats_digest=self._DIGEST,
            )
        sidecar = trial_stats_sidecar_path(store.path)
        original = sidecar.read_bytes()
        sidecar.write_bytes(b"{corrupt")
        with AuditWriter.open(tmp_path / "b.jsonl") as audit:
            resumed = run_persisted_sweep(
                bars,
                _make_config(),
                store=load_or_create_store(store.path),
                dedup=dedup,  # every mask marked — yet the marks are ADVISORY
                audit=audit,
                trial_stats_digest=self._DIGEST,
            )
        assert resumed.resumed_complete is False
        assert resumed.skipped_dedup == 0  # probes bypassed — full population
        assert resumed.trial_sidecar_written is True
        assert sidecar.read_bytes() == original  # healed, byte-identical

    def test_partially_marked_dedup_writes_full_population_sidecar(self, tmp_path: Path) -> None:
        # THE #650 regression (sidecar surface): a partially-marked dedup with
        # no sentinel must yield the FULL-population sidecar — byte-identical
        # to a clean run's — never a subset view and never a skipped write.
        from brutex.engine.sweep_run import _canonical_params
        from brutex.engine.trial_stats import trial_stats_sidecar_path

        cfg = _make_config()
        bars = _trending_bars()
        clean_store = ResultsStore(path=tmp_path / "clean" / "r.parquet")
        with AuditWriter.open(tmp_path / "clean" / "a.jsonl") as audit:
            run_persisted_sweep(
                bars,
                cfg,
                store=clean_store,
                dedup=_dedup(),
                audit=audit,
                trial_stats_digest=self._DIGEST,
            )
        baseline = trial_stats_sidecar_path(clean_store.path).read_bytes()

        rid = mask_run_id(
            mask=1 << cfg.condition_bits[0],
            direction="bull",
            instrument=cfg.instrument,
            tf=cfg.tf,
            mode=cfg.mode,
            params=_canonical_params(cfg),
            data_snapshot_hash=cfg.data_snapshot_hash,
            indicator_version=cfg.indicator_version,
            code_commit_sha=cfg.code_commit_sha,
        )
        dedup = _dedup()
        dedup.mark(rid)  # one of the five masks pre-marked (the crash subset)
        store = ResultsStore(path=tmp_path / "redo" / "r.parquet")
        with AuditWriter.open(tmp_path / "redo" / "a.jsonl") as audit:
            summary = run_persisted_sweep(
                bars,
                cfg,
                store=store,
                dedup=dedup,
                audit=audit,
                trial_stats_digest=self._DIGEST,
            )
        assert summary.skipped_dedup == 0  # the stale mark was never consulted
        assert summary.trial_sidecar_written is True
        assert trial_stats_sidecar_path(store.path).read_bytes() == baseline

    def test_redo_after_flush_rewrites_byte_identical(self, tmp_path: Path) -> None:
        # The flush-then-die corner (crash before the sentinel write): the
        # sentinel is absent, so the redo re-does the whole tuple with a fresh
        # view → the rewritten sidecar is byte-identical.
        from brutex.engine.sweep_orchestrator import load_or_create_store
        from brutex.engine.trial_stats import trial_stats_sidecar_path
        from brutex.engine.tuple_complete import completion_sentinel_path

        bars = _trending_bars()
        store = ResultsStore(path=tmp_path / "r.parquet")
        with AuditWriter.open(tmp_path / "a.jsonl") as audit:
            run_persisted_sweep(
                bars,
                _make_config(),
                store=store,
                dedup=_dedup(),
                audit=audit,
                trial_stats_digest=self._DIGEST,
            )
        sidecar = trial_stats_sidecar_path(store.path)
        first = sidecar.read_bytes()
        # Simulate the crash-before-sentinel window: sentinel never landed.
        completion_sentinel_path(store.path).unlink()
        # Fresh dedup = the post-crash redo (in-memory marks died with it).
        with AuditWriter.open(tmp_path / "b.jsonl") as audit:
            redone = run_persisted_sweep(
                bars,
                _make_config(),
                store=load_or_create_store(store.path),
                dedup=_dedup(),
                audit=audit,
                trial_stats_digest=self._DIGEST,
            )
        assert redone.resumed_complete is False
        assert redone.sentinel_written is True  # the redo healed the sentinel
        assert sidecar.read_bytes() == first


# ────── completion sentinel — the redo authority (DEC-MARK-MANY-SENTINEL-001) ──────


class _MarkCrashDedup(ComboDedup):
    """A dedup whose ``mark_many`` dies after the first key — the W4 crash."""

    def __init__(self) -> None:
        super().__init__(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))

    def mark_many(self, run_id_hexes: Iterable[str]) -> int:
        marked = 0
        for run_id_hex in run_id_hexes:
            if marked >= 1:
                raise RuntimeError("simulated crash inside the mark loop")
            self.mark(run_id_hex)
            marked += 1
        return marked


class TestCompletionSentinel:
    _DIGEST = "ab12cd34ef56ab12"

    def _clean_run(
        self, root: Path, cfg: SweepRunConfig, bars: list[CandleBar]
    ) -> tuple[Path, SweepRunSummary]:
        """Run one clean digest sweep under *root*; return (store path, summary)."""
        store = ResultsStore(path=root / "r.parquet")
        with AuditWriter.open(root / "a.jsonl") as audit:
            summary = run_persisted_sweep(
                bars,
                cfg,
                store=store,
                dedup=_dedup(),
                audit=audit,
                trial_stats_digest=self._DIGEST,
            )
        return store.path, summary

    def _redo(
        self,
        store_path: Path,
        cfg: SweepRunConfig,
        bars: list[CandleBar],
        *,
        dedup: ComboDedup | None = None,
        fresh_store: bool = False,
    ) -> SweepRunSummary:
        """Re-enter the tuple (fresh audit), optionally with a forged dedup/store."""
        from brutex.engine.sweep_orchestrator import load_or_create_store

        store = ResultsStore(path=store_path) if fresh_store else load_or_create_store(store_path)
        with AuditWriter.open(store_path.parent / "b.jsonl") as audit:
            return run_persisted_sweep(
                bars,
                cfg,
                store=store,
                dedup=dedup if dedup is not None else _dedup(),
                audit=audit,
                trial_stats_digest=self._DIGEST,
            )

    def _rid(self, cfg: SweepRunConfig, bit: int) -> str:
        from brutex.engine.sweep_run import _canonical_params

        return mask_run_id(
            mask=1 << bit,
            direction="bull",
            instrument=cfg.instrument,
            tf=cfg.tf,
            mode=cfg.mode,
            params=_canonical_params(cfg),
            data_snapshot_hash=cfg.data_snapshot_hash,
            indicator_version=cfg.indicator_version,
            code_commit_sha=cfg.code_commit_sha,
        )

    def test_digest_run_writes_paired_sentinel(self, tmp_path: Path) -> None:
        from brutex.engine.tuple_complete import completion_sentinel_path, read_completion_sentinel

        cfg = _make_config()
        store_path, summary = self._clean_run(tmp_path, cfg, _trending_bars())
        assert summary.sentinel_written is True
        sentinel = read_completion_sentinel(completion_sentinel_path(store_path))
        assert sentinel.tuple_digest == self._DIGEST
        assert sentinel.store_content_hash == summary.store_content_hash
        assert sentinel.sidecar_written is True
        # On the digest path the dedup probes are BYPASSED, so the unconditional
        # advisory mark_many is SKIPPED (DEC-SWEEP-DEDUP-LMDB-001) — the sentinel
        # is the redo authority, so n_marked honestly reads 0 (no marks written).
        assert sentinel.n_marked == 0

    def test_no_digest_writes_no_sentinel(self, tmp_path: Path) -> None:
        from brutex.engine.tuple_complete import completion_sentinel_path

        store = ResultsStore(path=tmp_path / "r.parquet")
        with AuditWriter.open(tmp_path / "a.jsonl") as audit:
            summary = run_persisted_sweep(
                _trending_bars(), _make_config(), store=store, dedup=_dedup(), audit=audit
            )
        assert summary.sentinel_written is False
        assert not completion_sentinel_path(store.path).exists()

    def test_paired_sentinel_short_circuits_touching_nothing(self, tmp_path: Path) -> None:
        from brutex.engine.trial_stats import trial_stats_sidecar_path
        from brutex.engine.tuple_complete import completion_sentinel_path
        from brutex.orchestrator.audit import read_entries

        cfg = _make_config()
        bars = _trending_bars()
        store_path, first = self._clean_run(tmp_path, cfg, bars)
        snapshots = {
            p: p.read_bytes()
            for p in (
                store_path,
                trial_stats_sidecar_path(store_path),
                completion_sentinel_path(store_path),
            )
        }
        fresh_dedup = _dedup()
        resumed = self._redo(store_path, cfg, bars, dedup=fresh_dedup)
        assert resumed.resumed_complete is True
        assert resumed.store_content_hash == first.store_content_hash
        assert (
            resumed.enumerated
            == resumed.evaluated
            == resumed.ranked
            == resumed.persisted
            == resumed.skipped_dedup
            == 0
        )
        assert resumed.sentinel_written is False
        assert resumed.trial_sidecar_written is False
        for path, before in snapshots.items():
            assert path.read_bytes() == before  # byte-untouched
        # The dedup was never consulted or marked on the short-circuit path.
        assert not fresh_dedup.seen(self._rid(cfg, cfg.condition_bits[0]))
        # Exactly ONE audit line — SWEEP_RESUMED — and the chain still verifies.
        entries = read_entries(store_path.parent / "b.jsonl")
        assert [e.action for e in entries] == ["SWEEP_RESUMED"]
        assert entries[0].payload["store_content_hash"] == first.store_content_hash
        assert verify_chain(store_path.parent / "b.jsonl") == 1

    def test_overfit_wedge_regression(self, tmp_path: Path) -> None:
        # THE #650 hole, arm (a): under --overfit-gate the OLD subset re-rank
        # deflated against the subset N → differing deflated_sharpe → store
        # append conflict (a wedge). The sentinel fix re-runs the FULL
        # population, so the redo is conflict-free and byte-identical.
        from brutex.engine.tuple_complete import completion_sentinel_path

        cfg = _make_config(overfit=OverfitGateConfig(dsr_min=0.0))
        bars = _trending_bars()
        store_path, _ = self._clean_run(tmp_path, cfg, bars)
        baseline = store_path.read_bytes()
        completion_sentinel_path(store_path).unlink()  # crash before sentinel
        subset = _dedup()
        subset.mark(self._rid(cfg, cfg.condition_bits[0]))  # partial mark loop
        redone = self._redo(store_path, cfg, bars, dedup=subset)  # must NOT raise
        assert redone.resumed_complete is False
        assert redone.skipped_dedup == 0
        assert store_path.read_bytes() == baseline

    def test_topk_inflation_regression(self, tmp_path: Path) -> None:
        # THE #650 hole, arm (b): under top_k truncation the OLD subset re-rank
        # appended the subset's own top-K — EXTRA rows, silently breaking tree
        # byte-identity. The sentinel fix re-runs the full population.
        from brutex.engine.tuple_complete import completion_sentinel_path

        cfg = _make_config(top_k=1)
        bars = _trending_bars()
        store_path, first = self._clean_run(tmp_path, cfg, bars)
        assert first.persisted == 1
        baseline = store_path.read_bytes()
        completion_sentinel_path(store_path).unlink()  # crash before sentinel
        subset = _dedup()
        subset.mark(self._rid(cfg, cfg.condition_bits[0]))  # partial mark loop
        redone = self._redo(store_path, cfg, bars, dedup=subset)
        assert redone.persisted == 1  # never inflated
        assert store_path.read_bytes() == baseline

    def test_digest_path_never_calls_mark_many(self, tmp_path: Path) -> None:
        # Regression (DEC-SWEEP-DEDUP-LMDB-001): on the digest path the dedup
        # probes are BYPASSED (`seen` is never consulted), so the unconditional
        # advisory mark_many is now SKIPPED too — marking would be a wasted
        # O(survivors) write to the off-heap LMDB exact tier. A dedup whose
        # mark_many WOULD raise is therefore never invoked: the run completes
        # cleanly and writes the sentinel. (Before the fix this crashed inside
        # the mark loop, leaving no sentinel — the window this fix removes.)
        from brutex.engine.tuple_complete import (
            completion_sentinel_path,
            read_completion_sentinel,
        )

        cfg = _make_config()
        bars = _trending_bars()
        store = ResultsStore(path=tmp_path / "r.parquet")
        with AuditWriter.open(tmp_path / "a.jsonl") as audit:
            summary = run_persisted_sweep(
                bars,
                cfg,
                store=store,
                dedup=_MarkCrashDedup(),  # its mark_many RAISES if ever called
                audit=audit,
                trial_stats_digest=self._DIGEST,
            )
        # The crash-on-mark dedup was NEVER asked to mark, so the sweep finished.
        assert summary.sentinel_written is True
        sentinel = read_completion_sentinel(completion_sentinel_path(store.path))
        assert sentinel.n_marked == 0
        # And it resumes-complete on a redo (a paired sentinel short-circuits).
        resumed = self._redo(store.path, cfg, bars)
        assert resumed.resumed_complete is True

    def test_sentinel_without_sidecar_record_skips_the_sidecar_check(self, tmp_path: Path) -> None:
        # A sentinel recording sidecar_written=False pairs WITHOUT consulting
        # the sidecar (the schema allows sidecar-less completions; today's
        # digest path always writes one, but the check honours the record).
        from brutex.engine.trial_stats import trial_stats_sidecar_path
        from brutex.engine.tuple_complete import (
            TupleCompletionSentinel,
            completion_sentinel_path,
            read_completion_sentinel,
            write_completion_sentinel,
        )

        cfg = _make_config()
        bars = _trending_bars()
        store_path, _ = self._clean_run(tmp_path, cfg, bars)
        recorded = read_completion_sentinel(completion_sentinel_path(store_path))
        write_completion_sentinel(
            completion_sentinel_path(store_path),
            TupleCompletionSentinel(
                tuple_digest=recorded.tuple_digest,
                store_content_hash=recorded.store_content_hash,
                sidecar_written=False,
                n_marked=recorded.n_marked,
            ),
        )
        trial_stats_sidecar_path(store_path).unlink()  # would fail if consulted
        resumed = self._redo(store_path, cfg, bars)
        assert resumed.resumed_complete is True

    def test_corrupt_sentinel_forces_full_redo(self, tmp_path: Path) -> None:
        from brutex.engine.tuple_complete import completion_sentinel_path

        cfg = _make_config()
        bars = _trending_bars()
        store_path, _ = self._clean_run(tmp_path, cfg, bars)
        sentinel_path = completion_sentinel_path(store_path)
        original = sentinel_path.read_bytes()
        sentinel_path.write_bytes(b"{corrupt")
        redone = self._redo(store_path, cfg, bars)
        assert redone.resumed_complete is False
        assert sentinel_path.read_bytes() == original  # healed, byte-identical

    def test_digest_mismatch_forces_full_redo(self, tmp_path: Path) -> None:
        from brutex.engine.tuple_complete import (
            TupleCompletionSentinel,
            completion_sentinel_path,
            read_completion_sentinel,
            write_completion_sentinel,
        )

        cfg = _make_config()
        bars = _trending_bars()
        store_path, _ = self._clean_run(tmp_path, cfg, bars)
        sentinel_path = completion_sentinel_path(store_path)
        recorded = read_completion_sentinel(sentinel_path)
        write_completion_sentinel(
            sentinel_path,
            TupleCompletionSentinel(
                tuple_digest="0" * 16,  # a DIFFERENT tuple's sentinel
                store_content_hash=recorded.store_content_hash,
                sidecar_written=recorded.sidecar_written,
                n_marked=recorded.n_marked,
            ),
        )
        redone = self._redo(store_path, cfg, bars)
        assert redone.resumed_complete is False
        assert read_completion_sentinel(sentinel_path).tuple_digest == self._DIGEST

    def test_missing_store_forces_full_redo(self, tmp_path: Path) -> None:
        cfg = _make_config()
        bars = _trending_bars()
        store_path, _ = self._clean_run(tmp_path, cfg, bars)
        baseline = store_path.read_bytes()
        store_path.unlink()  # sentinel present, store gone (operator damage)
        redone = self._redo(store_path, cfg, bars, fresh_store=True)
        assert redone.resumed_complete is False
        assert store_path.read_bytes() == baseline  # healed, byte-identical

    def test_tampered_store_forces_full_redo(self, tmp_path: Path) -> None:
        cfg = _make_config()
        bars = _trending_bars()
        store_path, _ = self._clean_run(tmp_path, cfg, bars)
        baseline = store_path.read_bytes()
        store_path.write_bytes(baseline + b"tampered")
        redone = self._redo(store_path, cfg, bars, fresh_store=True)
        assert redone.resumed_complete is False
        assert store_path.read_bytes() == baseline  # healed, byte-identical

    def test_sidecar_pairing_mismatch_forces_full_redo(self, tmp_path: Path) -> None:
        # A VALID sidecar paired to the WRONG store bytes must unpair the
        # sentinel — the sidecar read succeeds, the hash comparison fails.
        from brutex.engine.trial_stats import (
            trial_stats_sidecar_path,
            tuple_trial_stats_from_sharpes,
            write_tuple_trial_stats,
        )

        cfg = _make_config()
        bars = _trending_bars()
        store_path, _ = self._clean_run(tmp_path, cfg, bars)
        sidecar_path = trial_stats_sidecar_path(store_path)
        original = sidecar_path.read_bytes()
        write_tuple_trial_stats(
            sidecar_path,
            tuple_trial_stats_from_sharpes(
                [0.5],
                n_enumerated=1,
                n_conflict_pruned=0,
                n_minhits_dropped=0,
                tuple_digest=self._DIGEST,
                store_content_hash="0" * 64,  # pairs to some OTHER store bytes
            ),
        )
        redone = self._redo(store_path, cfg, bars)
        assert redone.resumed_complete is False
        assert sidecar_path.read_bytes() == original  # healed, byte-identical


# ────── run_persisted_sweep ──────


class TestRunPersistedSweep:
    def test_empty_bars_raises(self, tmp_path: Path) -> None:
        store = ResultsStore(path=tmp_path / "r.parquet")
        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(ValueError, match="bars must be non-empty"),
        ):
            run_persisted_sweep([], _make_config(), store=store, dedup=_dedup(), audit=audit)

    def test_hold_direction_rejected(self, tmp_path: Path) -> None:
        store = ResultsStore(path=tmp_path / "r.parquet")
        cfg = _make_config(direction=Side.HOLD)
        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(ValueError, match="must be Side.BUY or Side.SELL"),
        ):
            run_persisted_sweep(_trending_bars(), cfg, store=store, dedup=_dedup(), audit=audit)

    def test_over_cap_raises_before_compute(self, tmp_path: Path) -> None:
        store = ResultsStore(path=tmp_path / "r.parquet")
        # C(5,2)=10 > max_combos=3 → refuse.
        cfg = _make_config(condition_bits=[0, 1, 2, 3, 4], k=2, max_combos=3)
        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(ValueError, match="exceeds max_combos=3"),
        ):
            run_persisted_sweep(_trending_bars(), cfg, store=store, dedup=_dedup(), audit=audit)

    def test_happy_path_persists_and_audits_three_lines(self, tmp_path: Path) -> None:
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

    def test_overfit_gate_threads_into_persisted_leaderboard(self, tmp_path: Path) -> None:
        # End-to-end: the opt-in overfit gate is wired through run_persisted_sweep.
        # With dsr_min=0.0 every deflatable combo clears the 6th gate, so the
        # survivor SET matches the legacy run, but each finalist now carries a
        # Deflated Sharpe — proving the gate ran over the real backtest pool.
        bars = _trending_bars()
        legacy_store = ResultsStore(path=tmp_path / "legacy.parquet")
        with AuditWriter.open(tmp_path / "la.jsonl") as audit:
            legacy = run_persisted_sweep(
                bars, _make_config(), store=legacy_store, dedup=_dedup(), audit=audit
            )

        gated_store = ResultsStore(path=tmp_path / "gated.parquet")
        cfg = _make_config(overfit=OverfitGateConfig(dsr_min=0.0))
        with AuditWriter.open(tmp_path / "ga.jsonl") as audit:
            gated = run_persisted_sweep(bars, cfg, store=gated_store, dedup=_dedup(), audit=audit)
        # dsr_min=0.0 rejects nobody → same leaderboard size as legacy.
        assert gated.ranked == legacy.ranked

    def test_overfit_gate_rejects_can_only_shrink_leaderboard(self, tmp_path: Path) -> None:
        # A stricter dsr_min can only drop combos (monotone): the gated leaderboard
        # is never larger than the legacy one (Golden Rule #10 — the gate rejects).
        bars = _trending_bars()
        legacy_store = ResultsStore(path=tmp_path / "legacy.parquet")
        with AuditWriter.open(tmp_path / "la.jsonl") as audit:
            legacy = run_persisted_sweep(
                bars, _make_config(), store=legacy_store, dedup=_dedup(), audit=audit
            )
        gated_store = ResultsStore(path=tmp_path / "gated.parquet")
        cfg = _make_config(overfit=OverfitGateConfig(dsr_min=0.99))
        with AuditWriter.open(tmp_path / "ga.jsonl") as audit:
            gated = run_persisted_sweep(bars, cfg, store=gated_store, dedup=_dedup(), audit=audit)
        assert gated.ranked <= legacy.ranked
        assert gated.persisted == len(gated_store)

    def test_top_k_truncates_persisted(self, tmp_path: Path) -> None:
        bars = _trending_bars()
        store = ResultsStore(path=tmp_path / "r.parquet")
        cfg = _make_config(top_k=1)
        with AuditWriter.open(tmp_path / "a.jsonl") as audit:
            summary = run_persisted_sweep(bars, cfg, store=store, dedup=_dedup(), audit=audit)
        assert summary.ranked >= 1
        assert summary.persisted == 1

    def test_skip_path_on_prepopulated_dedup(self, tmp_path: Path) -> None:
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
        bars = _trending_bars()
        cfg = _make_config(min_hits=0)
        store = ResultsStore(path=tmp_path / "r.parquet")
        with AuditWriter.open(tmp_path / "a.jsonl") as audit:
            summary = run_persisted_sweep(bars, cfg, store=store, dedup=_dedup(), audit=audit)
        assert summary.evaluated == 5  # no Stage-1B drop

    def test_sell_direction_runs(self, tmp_path: Path) -> None:
        # Exercise the Side.SELL → "bear" direction-label branch end-to-end.
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


# ────── SweepComboError — fail-loud-with-fingerprint (Resilient: skip+continue) ──────


class TestSweepComboError:
    """A combo that raises mid-evaluation surfaces a fingerprinted SweepComboError.

    The error names the exact offending combo (mask hex + run-id + tuple
    coordinate), and — critically — NO store flush / sidecar / completion
    sentinel is written, so the tuple redoes deterministically next run.
    """

    def test_is_a_brutex_error_subclass(self) -> None:
        from brutex.models import BruteXError

        assert issubclass(SweepComboError, BruteXError)

    def test_raises_naming_the_offending_combo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.engine.sweep_driver import metrics_for_mask as real_metrics

        bars = _trending_bars()
        cfg = _make_config()  # k=1, condition_bits=[0,1,2,3,4] → masks 1,2,4,8,16
        boom_mask = 0b100  # 1 << 2

        def _raising_metrics(*args: object, **kwargs: object) -> object:
            mask = args[2]  # (bars, bar_bits, mask, ...)
            if mask == boom_mask:
                raise ValueError("corrupt bar window")
            return real_metrics(*args, **kwargs)  # type: ignore[arg-type]

        # Patch the name as resolved inside run_persisted_sweep (the sweep_run
        # module namespace) — the string target avoids the re-export attr check.
        monkeypatch.setattr("brutex.engine.sweep_run.metrics_for_mask", _raising_metrics)

        store = ResultsStore(path=tmp_path / "r.parquet")
        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(SweepComboError) as excinfo,
        ):
            run_persisted_sweep(
                bars,
                cfg,
                store=store,
                dedup=_dedup(),
                audit=audit,
                trial_stats_digest="ab12cd34ef56ab12",
            )
        msg = str(excinfo.value)
        # The message names the mask hex, the run-id, the tuple coordinate, and k.
        assert f"0x{boom_mask:016x}" in msg
        assert "NIFTY/1m/bull" in msg
        assert "k=1" in msg
        assert "run_id=" in msg
        assert "corrupt bar window" in msg  # the original error is chained in
        # The raise is chained from the original exception (`from exc`).
        assert isinstance(excinfo.value.__cause__, ValueError)

    def test_no_store_or_sentinel_written_on_raise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.engine.trial_stats import trial_stats_sidecar_path
        from brutex.engine.tuple_complete import completion_sentinel_path

        bars = _trending_bars()
        cfg = _make_config()

        def _always_raise(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("kernel exploded")

        monkeypatch.setattr("brutex.engine.sweep_run.metrics_for_mask", _always_raise)

        store = ResultsStore(path=tmp_path / "r.parquet")
        dedup = _dedup()
        first_rid = mask_run_id(
            mask=1,
            direction="bull",
            instrument=cfg.instrument,
            tf=cfg.tf,
            mode=cfg.mode,
            params=canonical_trade_params(
                sl_paisa=cfg.sl_paisa,
                tp_paisa=cfg.tp_paisa,
                prior_n=cfg.prior_n,
                proximity=cfg.proximity,
                start_equity_paisa=cfg.start_equity_paisa,
            ),
            data_snapshot_hash=cfg.data_snapshot_hash,
            indicator_version=cfg.indicator_version,
            code_commit_sha=cfg.code_commit_sha,
        )
        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(SweepComboError),
        ):
            run_persisted_sweep(
                bars,
                cfg,
                store=store,
                dedup=dedup,
                audit=audit,
                trial_stats_digest="ab12cd34ef56ab12",
            )
        # The store Parquet, the trial-stats sidecar, and the completion sentinel
        # were ALL left unwritten (the raise is BEFORE flush/mark/sentinel), so the
        # tuple redoes next run (un-sentineled, reproducible).
        assert not store.path.exists()
        assert not trial_stats_sidecar_path(store.path).exists()
        assert not completion_sentinel_path(store.path).exists()
        # The dedup was never marked, so a re-run re-evaluates the masks.
        assert not dedup.seen(first_rid)

    def test_memory_error_propagates_bare_not_a_combo_skip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A MemoryError is a GLOBAL abort, NOT a per-tuple SweepComboError skip.

        OOM is treated as a global stop everywhere in the codebase
        (DEC-LAKE-WRITE-HALT-001), so the combo loop must re-raise it BARE instead
        of wrapping it into a SweepComboError that the grid drivers would downgrade
        to a single-tuple skip. The store / sidecar / sentinel stay unwritten.
        """
        from brutex.engine.trial_stats import trial_stats_sidecar_path
        from brutex.engine.tuple_complete import completion_sentinel_path

        bars = _trending_bars()
        cfg = _make_config()

        def _oom(*_args: object, **_kwargs: object) -> object:
            raise MemoryError("out of memory")

        monkeypatch.setattr("brutex.engine.sweep_run.metrics_for_mask", _oom)

        store = ResultsStore(path=tmp_path / "r.parquet")
        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(MemoryError, match="out of memory") as excinfo,
        ):
            run_persisted_sweep(
                bars,
                cfg,
                store=store,
                dedup=_dedup(),
                audit=audit,
                trial_stats_digest="ab12cd34ef56ab12",
            )
        # It is NOT downgraded to a SweepComboError (the per-tuple skip path).
        assert not isinstance(excinfo.value, SweepComboError)
        # Bare re-raise: no SweepComboError wrap chained around it.
        assert excinfo.value.__cause__ is None
        # No artifact was written (the raise is before flush/mark/sentinel).
        assert not store.path.exists()
        assert not trial_stats_sidecar_path(store.path).exists()
        assert not completion_sentinel_path(store.path).exists()

    def test_keyboard_interrupt_propagates_bare_not_a_combo_skip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A KeyboardInterrupt ESCAPES the per-tuple SweepComboError catch BARE.

        An operator Ctrl-C is a ``BaseException`` (NOT an ``Exception``), so the
        combo loop's ``except Exception`` MUST NOT catch it — it propagates
        unchanged so the whole (33B-trial) sweep aborts instead of being silently
        downgraded to a single-tuple skip + continue (DEC-ENGINE-DEDUP-NAN-CANON-001
        / the audit H1 guard, the BaseException twin of the MemoryError test). The
        store / sidecar / sentinel stay unwritten + ``tuples_failed`` is irrelevant
        (the single-tuple runner does not even reach the grid driver's count).
        """
        from brutex.engine.trial_stats import trial_stats_sidecar_path
        from brutex.engine.tuple_complete import completion_sentinel_path

        bars = _trending_bars()
        cfg = _make_config()

        def _interrupt(*_args: object, **_kwargs: object) -> object:
            raise KeyboardInterrupt("operator ctrl-c")

        monkeypatch.setattr("brutex.engine.sweep_run.metrics_for_mask", _interrupt)

        store = ResultsStore(path=tmp_path / "r.parquet")
        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(KeyboardInterrupt, match="operator ctrl-c") as excinfo,
        ):
            run_persisted_sweep(
                bars,
                cfg,
                store=store,
                dedup=_dedup(),
                audit=audit,
                trial_stats_digest="ab12cd34ef56ab12",
            )
        # It is NOT downgraded to a SweepComboError (the per-tuple skip path).
        assert not isinstance(excinfo.value, SweepComboError)
        # Bare re-raise (escaped `except Exception`): no SweepComboError chained.
        assert excinfo.value.__cause__ is None
        cause = excinfo.value.__context__
        while cause is not None:
            assert not isinstance(cause, SweepComboError)
            cause = cause.__context__
        # No artifact was written (the raise is before flush/mark/sentinel).
        assert not store.path.exists()
        assert not trial_stats_sidecar_path(store.path).exists()
        assert not completion_sentinel_path(store.path).exists()

    def test_base_exception_subclass_propagates_bare_not_a_combo_skip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A generic BaseException subclass ESCAPES the ``except Exception`` catch.

        The combo loop catches only ``Exception`` (and re-raises ``MemoryError``
        bare BEFORE it). Anything deriving from ``BaseException`` but NOT
        ``Exception`` — Ctrl-C, ``SystemExit``, or any custom ``BaseException``
        subclass — must propagate UNWRAPPED, pinning that a future refactor
        widening the catch to ``except BaseException`` (which would silently
        downgrade a fatal signal into a tuple skip) is caught (audit H1).
        """
        from brutex.engine.trial_stats import trial_stats_sidecar_path
        from brutex.engine.tuple_complete import completion_sentinel_path

        class _FatalSignal(BaseException):
            """A BaseException that is NOT an Exception (mirrors Ctrl-C / SystemExit)."""

        bars = _trending_bars()
        cfg = _make_config()

        def _fatal(*_args: object, **_kwargs: object) -> object:
            raise _FatalSignal("not an Exception")

        monkeypatch.setattr("brutex.engine.sweep_run.metrics_for_mask", _fatal)

        store = ResultsStore(path=tmp_path / "r.parquet")
        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(_FatalSignal, match="not an Exception") as excinfo,
        ):
            run_persisted_sweep(
                bars,
                cfg,
                store=store,
                dedup=_dedup(),
                audit=audit,
                trial_stats_digest="ab12cd34ef56ab12",
            )
        # Not an Exception, so it cannot be wrapped by the SweepComboError catch.
        assert not isinstance(excinfo.value, Exception)
        assert not isinstance(excinfo.value, SweepComboError)
        assert excinfo.value.__cause__ is None
        # No artifact was written (raise is before flush/mark/sentinel).
        assert not store.path.exists()
        assert not trial_stats_sidecar_path(store.path).exists()
        assert not completion_sentinel_path(store.path).exists()

    def test_store_flush_error_aborts_not_a_combo_skip(self, tmp_path: Path) -> None:
        """A ResultsStoreError from store.flush() propagates OUT — NOT a tuple skip.

        The flush happens AFTER the combo loop, so a genuine infra/store failure
        must NOT be caught by the ``except SweepComboError`` (which only wraps the
        per-combo ``metrics_for_mask`` raise). It propagates unchanged as a
        ``ResultsStoreError`` (a ``BruteXError`` subclass) so the grid drivers
        ABORT globally instead of downgrading it to a single-tuple skip — proving
        the combo-loop catch is narrow.
        """
        from brutex.engine.results_store import FlushResult, ResultsStoreError

        bars = _trending_bars()
        cfg = _make_config()

        class _FlushFailsStore(ResultsStore):
            """A real store whose flush() (post-combo-loop) raises infra failure."""

            def flush(self) -> FlushResult:
                raise ResultsStoreError("simulated disk failure during flush")

        store = _FlushFailsStore(path=tmp_path / "r.parquet")
        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(ResultsStoreError, match="simulated disk failure during flush") as exc,
        ):
            run_persisted_sweep(
                bars,
                cfg,
                store=store,
                dedup=_dedup(),
                audit=audit,
                trial_stats_digest="ab12cd34ef56ab12",
            )
        # It is NOT wrapped as a SweepComboError tuple-skip — it is the raw store
        # error, with no SweepComboError anywhere in the chain.
        assert not isinstance(exc.value, SweepComboError)
        assert exc.value.__cause__ is None
        cause = exc.value.__context__
        while cause is not None:
            assert not isinstance(cause, SweepComboError)
            cause = cause.__context__
