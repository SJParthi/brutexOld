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

from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine import rust_enum
from brutex.engine.results_store import ResultRow, ResultsStore
from brutex.engine.rust_trade_sim import ResidentTradeSim
from brutex.engine.stage1a import build_bar_bits
from brutex.engine.sweep_driver import metrics_for_mask
from brutex.engine.sweep_kernel import ResidentSweepKernel
from brutex.engine.sweep_run import (
    ComboDedup,
    SweepComboError,
    SweepRunConfig,
    SweepRunSummary,
    canonical_trade_params,
    direction_label,
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
from structlog.testing import capture_logs

if TYPE_CHECKING:  # pragma: no cover - typing only (lazy numpy, see rust_trade_sim)
    from brutex.metrics.types import ClosedTrade

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


#: An ADR-0012 roll schedule covering the `_trending_bars` fixture (base day
#: 2026-01-05 IST): one mid-stream roll + a covering tail, as day-ordinals.
_FUT_SCHEDULE: tuple[int, ...] = (
    datetime(2026, 1, 8, tzinfo=_IST).date().toordinal(),
    datetime(2026, 3, 31, tzinfo=_IST).date().toordinal(),
)


# ────── DEC-SWEEP-RUST-TRADESIM-WIRE-001 test seams ──────
#
# The wired sweep batch-simulates via `sweep_run.ResidentTradeSim`; failure
# injection therefore targets (a) the batch call (a subclass whose
# `simulate_masks_with_exit_indices` raises — the chunk-failure seam), (b) the construction
# (`__init__` raises), and (c) the per-mask metrics tail
# (`sweep_run.metrics_from_trades`). The per-mask ORACLE seam
# (`sweep_run.metrics_for_mask`) is now exercised by the chunk-failure
# RE-ATTRIBUTION path.


def _forced_fallback_sim(
    bars: Sequence[CandleBar],
    bar_bits: Sequence[int],
    *,
    tf_minutes: int,
    reference_stamper: object = None,
) -> ResidentTradeSim:
    """A ResidentTradeSim with the compiled kernel DISABLED — the pure-Python arm.

    Patched over ``sweep_run.ResidentTradeSim`` to force the WHOLE sweep onto the
    bit-identical Python oracle (the wired-vs-unwired forcing seam — the same
    instance-level ``_rust = None`` mechanism the rust_trade_sim fallback tests
    use; a FACTORY rather than a subclass, so no ``__init__`` overwrites the
    attribute the superclass ctor just built).
    """
    sim = ResidentTradeSim(
        bars,
        bar_bits,
        tf_minutes=tf_minutes,
        reference_stamper=reference_stamper,  # type: ignore[arg-type]
    )
    sim._rust = None  # pyright: ignore[reportPrivateUsage] — the DEC-P0-017 friend seam
    return sim


def _chunk_raising_sim(exc_factory: Callable[[], BaseException]) -> type[ResidentTradeSim]:
    """A ResidentTradeSim subclass whose BATCH call raises — chunk-failure injection."""

    class _RaisingSim(ResidentTradeSim):
        def simulate_masks_with_exit_indices(self, *args: object, **kwargs: object) -> NoReturn:
            raise exc_factory()

    return _RaisingSim


class _ShortChunkSim(ResidentTradeSim):
    """A sim that RETURNS one fewer pair than masks — batch-contract violation.

    The kernel-misbehaviour shape that RETURNS wrong instead of RAISING (a PyO3
    marshalling bug, a kernel bug): unreachable from the real kernel (Rust
    returns exactly one Vec per mask by construction), injected here to pin
    that the sweep's length guard maps it onto the SAME re-attribution →
    ``SweepComboError`` tuple-skip ladder as a chunk that raises — never a bare
    ``ValueError`` escaping ``run_persisted_sweep`` as a grid-wide abort.
    """

    def simulate_masks_with_exit_indices(
        self, masks: Sequence[int], **kwargs: Any
    ) -> list[tuple[list[ClosedTrade], list[int] | None]]:
        return super().simulate_masks_with_exit_indices(masks, **kwargs)[:-1]


def _ctor_raising_sim(exc_factory: Callable[[], BaseException]) -> type[object]:
    """A ResidentTradeSim STAND-IN whose CONSTRUCTION raises — ctor-failure injection.

    Deliberately NOT a subclass: the fake must raise BEFORE any sim state exists
    (there is no half-built superclass to ``__init__``), and the sweep only ever
    CALLS the patched ``sweep_run.ResidentTradeSim`` name, so a
    signature-compatible stand-in exercises the exact production ctor wrap.
    """

    class _CtorRaisesSim:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise exc_factory()

    return _CtorRaisesSim


def _run_sweep(
    tmp_path: Path,
    tag: str,
    bars: Sequence[CandleBar],
    cfg: SweepRunConfig,
    *,
    kernel: ResidentSweepKernel | None = None,
    digest: str | None = None,
) -> SweepRunSummary:
    """One persisted sweep into ``<tag>.parquet`` / ``<tag>.jsonl`` under *tmp_path*."""
    store = ResultsStore(path=tmp_path / f"{tag}.parquet")
    with AuditWriter.open(tmp_path / f"{tag}.jsonl") as audit:
        return run_persisted_sweep(
            bars,
            cfg,
            store=store,
            dedup=_dedup(),
            audit=audit,
            kernel=kernel,
            trial_stats_digest=digest,
        )


def _crafted_kernel(bars: Sequence[CandleBar]) -> ResidentSweepKernel:
    """A real kernel whose ``bar_bits`` are HAND-CRAFTED so bits 0–4 fire on
    pairwise-DIFFERENT bar subsets (periods 2..6) — every mask's trade stream,
    and therefore its metrics, differ, so a chunk-boundary mask↔metrics swap
    CANNOT hide behind identical rows (the chunk-alignment tests' teeth).

    The resident Stage-1B count buffer still holds the ORIGINAL bits, so tests
    using this kernel MUST disable the pre-filter (``min_hits=0``).
    """
    kernel = ResidentSweepKernel(bars)
    kernel.bar_bits[:] = [
        sum(1 << b for b in range(5) if i % (b + 2) == 0) for i in range(len(bars))
    ]
    return kernel


# ────── direction_label (public re-export for the parallel driver) ──────


class TestDirectionLabel:
    """The PUBLIC re-export the parallel driver uses (DEC-MULTICORE-SWEEP-PHASE1-001).

    ``direction_label`` is the ONE-source-of-truth seam so
    :mod:`brutex.engine.sweep_parallel` builds byte-identical cache keys + labels
    without importing the private ``_direction_label``. These focused unit tests
    scope the re-export's coverage to sweep_run's OWN per-module gate (the parallel
    suite is not scoped for this module by the touched-module coverage gate).
    """

    def test_buy_is_bull(self) -> None:
        assert direction_label(Side.BUY) == "bull"

    def test_sell_is_bear(self) -> None:
        assert direction_label(Side.SELL) == "bear"

    def test_hold_rejected(self) -> None:
        with pytest.raises(ValueError, match="Side.BUY or Side.SELL"):
            direction_label(Side.HOLD)


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

    def test_strategy_id_encodes_mask_as_32_hex(self) -> None:
        # mask:%032x — the DEC-MASK-IDENTITY-001 / DEC-VOCAB-U128-CODEGEN-001 u128
        # encoding. Two masks differing only in a HIGH bit (>= 64) must still differ
        # (no truncation to 16 hex / u64 — the u128 headroom is preserved).
        a = mask_run_id(mask=0, params=_PARAMS, **_CTX)
        b = mask_run_id(mask=1 << 51, params=_PARAMS, **_CTX)
        assert a != b
        # A bit beyond u64 must NOT alias a u64-truncated mask.
        hi = mask_run_id(mask=1 << 100, params=_PARAMS, **_CTX)
        assert hi not in (a, b)

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

        with pytest.raises(ValueError, match=r"condition_bits \[199\] out of range \[0, 128\)"):
            _make_config(condition_bits=[0, 1, 199], k=1)
        # The boundary bit (== MAX_VOCAB_BITS) is also out of the half-open range.
        with pytest.raises(ValueError, match=r"out of range \[0, 128\)"):
            _make_config(condition_bits=[MAX_VOCAB_BITS], k=1)

    def test_negative_condition_bit_rejected_at_config_time(self) -> None:
        with pytest.raises(ValueError, match=r"condition_bits \[-1\] out of range \[0, 128\)"):
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

    # ── ADR-0012 instrument⟺schedule hard invariant (DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001) ──

    def test_futures_alias_requires_a_roll_schedule(self) -> None:
        # Direction 1: the alias with NO schedule is refused — a futures run
        # can never silently sweep the constant-key path.
        with pytest.raises(ValueError, match="roll_schedule is required for"):
            _make_config(instrument="NSE-NIFTY-FUT")

    @pytest.mark.parametrize("instrument", ["NIFTY", "BANKNIFTY", "SENSEX"])
    def test_spot_instrument_forbids_a_roll_schedule(self, instrument: str) -> None:
        # Direction 2: a spot config with a schedule is refused — presence is a
        # TOTAL function of the identity field, so no instrument can ever mint
        # both a with-key and a without-key run identity (the #1062 bug class).
        with pytest.raises(ValueError, match="roll_schedule is required for"):
            _make_config(instrument=instrument, roll_schedule=_FUT_SCHEDULE)

    def test_futures_alias_with_a_schedule_constructs(self) -> None:
        cfg = _make_config(instrument="NSE-NIFTY-FUT", roll_schedule=_FUT_SCHEDULE)
        assert cfg.roll_schedule == _FUT_SCHEDULE

    @pytest.mark.parametrize(
        "schedule",
        [(), (5, 5), (7, 3), (-1, 5)],
        ids=["empty", "duplicate", "decreasing", "negative"],
    )
    def test_invalid_roll_schedule_shape_rejected(self, schedule: tuple[int, ...]) -> None:
        with pytest.raises(ValueError, match="non-empty strictly increasing"):
            _make_config(instrument="NSE-NIFTY-FUT", roll_schedule=schedule)


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
        # The Fib anchor defaults to prev_day and is emitted UNCONDITIONALLY
        # (DEC-NIFTY-FIB-AXIS-001) so every family's run-identity carries it.
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
            "fib_anchor": "prev_day",
            "start_equity_paisa": 10_000_000,
        }

    def test_fib_anchor_enters_the_params_layout(self) -> None:
        # DEC-NIFTY-FIB-AXIS-001: a non-prev-day anchor changes the run-identity —
        # the anchor's string enum value is the ``fib_anchor`` key and two anchors
        # therefore hash to distinct combo identities (Golden Rule #4).
        from brutex.strategies.types import FibAnchorKind

        prev = canonical_trade_params(
            sl_paisa=80,
            tp_paisa=50,
            prior_n=3,
            proximity=None,
            start_equity_paisa=10_000_000,
            fib_anchor=FibAnchorKind.PREV_DAY,
        )
        rolling = canonical_trade_params(
            sl_paisa=80,
            tp_paisa=50,
            prior_n=3,
            proximity=None,
            start_equity_paisa=10_000_000,
            fib_anchor=FibAnchorKind.ROLLING_WEEKLY,
        )
        assert prev["fib_anchor"] == "prev_day"
        assert rolling["fib_anchor"] == "rolling_weekly"
        assert prev != rolling

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

    # ── ADR-0012 roll_schedule — conditional emission + the aliasing kill-shot ──

    def test_spot_params_carry_no_roll_schedule_key(self) -> None:
        """No schedule ⇒ NO key — spot run-ids stay byte-identical (P2).

        The exact dict (not just key absence) is pinned so a renamed or
        default-emitted key can never slip in.
        """
        params = canonical_trade_params(
            sl_paisa=80,
            tp_paisa=50,
            prior_n=3,
            proximity=None,
            start_equity_paisa=10_000_000,
            roll_schedule=None,
        )
        assert params == {
            "sl_paisa": 80,
            "tp_paisa": 50,
            "prior_n": 3,
            "proximity_bps": None,
            "fib_anchor": "prev_day",
            "start_equity_paisa": 10_000_000,
        }
        assert "roll_schedule" not in params

    def test_roll_schedule_emitted_as_json_canonical_ints(self) -> None:
        params = canonical_trade_params(
            sl_paisa=80,
            tp_paisa=50,
            prior_n=3,
            proximity=None,
            start_equity_paisa=10_000_000,
            roll_schedule=_FUT_SCHEDULE,
        )
        assert params["roll_schedule"] == list(_FUT_SCHEDULE)
        assert all(isinstance(v, int) for v in params["roll_schedule"])

    def test_aliasing_kill_shot_distinct_schedules_mint_distinct_run_ids(self) -> None:
        """TWO different schedules over IDENTICAL bar bytes ⇒ DISTINCT run_ids.

        The §1062-class containment teeth (DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001):
        the snapshot hash captures the BARS, so if two lakes stitch identical
        bytes under different boundary ordinals, ONLY the recorded
        ``roll_schedule`` separates the two computations — the run identity
        must move on the schedule alone.
        """
        ctx = dict(_CTX, instrument="NSE-NIFTY-FUT")

        def _rid(schedule: tuple[int, ...]) -> str:
            params = canonical_trade_params(
                sl_paisa=80,
                tp_paisa=50,
                prior_n=3,
                proximity=None,
                start_equity_paisa=10_000_000,
                roll_schedule=schedule,
            )
            # SAME mask, SAME context, SAME data_snapshot_hash — the bars are
            # byte-identical by construction; only the schedule differs.
            return mask_run_id(mask=0b101, params=params, **ctx)

        other_schedule = (_FUT_SCHEDULE[0] + 1, _FUT_SCHEDULE[1])
        assert _rid(_FUT_SCHEDULE) != _rid(other_schedule)

    def test_futures_config_identity_routes_the_schedule(self) -> None:
        # §11 COMMON: the futures config's identity params ARE this helper's
        # output, schedule included.
        from brutex.engine.sweep_run import (
            _canonical_params,  # pyright: ignore[reportPrivateUsage]
        )

        cfg = _make_config(instrument="NSE-NIFTY-FUT", roll_schedule=_FUT_SCHEDULE)
        assert _canonical_params(cfg)["roll_schedule"] == list(_FUT_SCHEDULE)


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

    def test_apriori_enabled_enumeration_source_is_byte_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DEC-SWEEP-APRIORI-TUPLE-001: with ``BRUTEX_SWEEP_APRIORI`` set the
        enumeration source becomes the Apriori pruned generator — exercising
        ``run_persisted_sweep``'s ``if apriori_enabled():`` arm (the lazy
        ``generate_apriori_candidates`` import + call) — and the persisted
        leaderboard stays byte-identical to the brute-force OFF path.

        The byte-parity golden proves the same equality, but its file stem is
        not attributed to ``sweep_run`` by the touched-module coverage gate, so
        this focused test scopes that arm's coverage to sweep_run's OWN
        per-module 100% gate.
        """
        bars = _trending_bars()
        cfg = _make_config(condition_bits=[0, 1, 2, 3, 4], k=2, min_hits=0)

        monkeypatch.delenv("BRUTEX_SWEEP_APRIORI", raising=False)
        off = _run_sweep(tmp_path, "off", bars, cfg)

        monkeypatch.setenv("BRUTEX_SWEEP_APRIORI", "1")
        on = _run_sweep(tmp_path, "on", bars, cfg)

        assert on.store_content_hash == off.store_content_hash
        assert (tmp_path / "on.parquet").read_bytes() == (tmp_path / "off.parquet").read_bytes()
        assert on.persisted == off.persisted

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
        # Pre-mark every enumerated identity → every mask is skipped. The params
        # layout carries the default ``fib_anchor`` (prev_day) so these hand-built
        # run-ids match the sweep's (DEC-NIFTY-FIB-AXIS-001).
        params = {
            "sl_paisa": 80,
            "tp_paisa": 50,
            "prior_n": 3,
            "proximity_bps": None,
            "fib_anchor": "prev_day",
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

    def test_gate_conflict_at_persist_becomes_tuple_scoped_combo_error(
        self, tmp_path: Path
    ) -> None:
        """C5 (DEC-SWEEP-TUPLE-DIGEST-GATE-IDENTITY-001): a gate-config conflict at the
        finalist ``store.append`` is converted AT THE SOURCE into a SweepComboError.

        The typed :class:`ResultsStoreGateConflictError` is a SIBLING of
        ``ResultsStoreError`` — converting it here (not letting it propagate as a
        ``ResultsStoreError``) makes it sail PAST ``run_one_tuple``'s
        ``except ResultsStoreError`` global-abort re-raise and land on the grid
        driver's ``except SweepComboError`` tuple-skip ladder, naming the config
        cause. This covers the new persist-loop except branch.
        """
        from brutex.engine.results_store import ResultsStoreGateConflictError

        bars = _trending_bars()

        class _GateConflictStore(ResultsStore):
            def append(self, row: ResultRow) -> bool:
                raise ResultsStoreGateConflictError(
                    "the ONLY differing field(s) (deflated_sharpe) are gate-derived DSR "
                    "metric(s) (DEC-SWEEP-TUPLE-DIGEST-GATE-IDENTITY-001)."
                )

        store = _GateConflictStore(path=tmp_path / "r.parquet")
        with AuditWriter.open(tmp_path / "a.jsonl") as audit:  # noqa: SIM117
            with pytest.raises(SweepComboError, match="gate-config conflict persisting") as exc:
                run_persisted_sweep(bars, _make_config(), store=store, dedup=_dedup(), audit=audit)
        assert "NIFTY" in str(exc.value)
        assert "DEC-SWEEP-TUPLE-DIGEST-GATE-IDENTITY-001" in str(exc.value)


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
        """Chunk failure → per-mask ORACLE re-run → the culprit is fingerprinted.

        The wired loop batch-simulates, so per-mask attribution happens in the
        re-attribution pass: the batch call raises, then the chunk re-runs one
        mask at a time through `metrics_for_mask` (the oracle) and the FIRST
        re-raising mask gets the standard fingerprint, chained from ITS error.
        """
        from brutex.engine.sweep_driver import metrics_for_mask as real_metrics

        bars = _trending_bars()
        cfg = _make_config()  # k=1, condition_bits=[0,1,2,3,4] → masks 1,2,4,8,16
        boom_mask = 0b100  # 1 << 2

        # The batch call fails (losing per-mask attribution) ...
        monkeypatch.setattr(
            "brutex.engine.sweep_run.ResidentTradeSim",
            _chunk_raising_sim(lambda: RuntimeError("batched kernel failure")),
        )

        def _raising_metrics(*args: object, **kwargs: object) -> object:
            mask = args[2]  # (bars, bar_bits, mask, ...)
            if mask == boom_mask:
                raise ValueError("corrupt bar window")
            return real_metrics(*args, **kwargs)  # type: ignore[arg-type]

        # ... and the oracle re-run identifies the culprit. Patch the names as
        # resolved inside run_persisted_sweep (the sweep_run module namespace).
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
        assert f"0x{boom_mask:032x}" in msg
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

        # The batch call fails AND every oracle re-run fails → the FIRST mask is
        # fingerprinted; nothing must have been flushed / marked / sentineled.
        monkeypatch.setattr(
            "brutex.engine.sweep_run.ResidentTradeSim",
            _chunk_raising_sim(lambda: RuntimeError("kernel exploded")),
        )

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
        (DEC-LAKE-WRITE-HALT-001), so a MemoryError from the BATCHED trade-sim
        call — the PyO3 Vec<Vec<TradeRecord>> marshal is a plausible real OOM
        site — must re-raise BARE, BEFORE any per-mask oracle re-attribution
        (which would repeat the allocation pressure under memory pressure) and
        never be downgraded into a SweepComboError tuple-skip. The store /
        sidecar / sentinel stay unwritten.
        """
        from brutex.engine.trial_stats import trial_stats_sidecar_path
        from brutex.engine.tuple_complete import completion_sentinel_path

        bars = _trending_bars()
        cfg = _make_config()

        monkeypatch.setattr(
            "brutex.engine.sweep_run.ResidentTradeSim",
            _chunk_raising_sim(lambda: MemoryError("out of memory")),
        )

        def _oracle_must_not_run(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("re-attribution must NOT run on a MemoryError")

        monkeypatch.setattr("brutex.engine.sweep_run.metrics_for_mask", _oracle_must_not_run)

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
        chunked loop's ``except Exception`` MUST NOT catch it — it propagates
        unchanged (no oracle re-attribution) so the whole (33B-trial) sweep
        aborts instead of being silently downgraded to a single-tuple skip +
        continue (DEC-ENGINE-DEDUP-NAN-CANON-001 / the audit H1 guard, the
        BaseException twin of the MemoryError test). The store / sidecar /
        sentinel stay unwritten + ``tuples_failed`` is irrelevant (the
        single-tuple runner does not even reach the grid driver's count).
        """
        from brutex.engine.trial_stats import trial_stats_sidecar_path
        from brutex.engine.tuple_complete import completion_sentinel_path

        bars = _trending_bars()
        cfg = _make_config()

        monkeypatch.setattr(
            "brutex.engine.sweep_run.ResidentTradeSim",
            _chunk_raising_sim(lambda: KeyboardInterrupt("operator ctrl-c")),
        )

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

        The chunked loop catches only ``Exception`` (and re-raises ``MemoryError``
        bare BEFORE it). Anything deriving from ``BaseException`` but NOT
        ``Exception`` — Ctrl-C, ``SystemExit``, or any custom ``BaseException``
        subclass — must propagate UNWRAPPED (no oracle re-attribution), pinning
        that a future refactor widening the catch to ``except BaseException``
        (which would silently downgrade a fatal signal into a tuple skip) is
        caught (audit H1).
        """
        from brutex.engine.trial_stats import trial_stats_sidecar_path
        from brutex.engine.tuple_complete import completion_sentinel_path

        class _FatalSignal(BaseException):
            """A BaseException that is NOT an Exception (mirrors Ctrl-C / SystemExit)."""

        bars = _trending_bars()
        cfg = _make_config()

        monkeypatch.setattr(
            "brutex.engine.sweep_run.ResidentTradeSim",
            _chunk_raising_sim(lambda: _FatalSignal("not an Exception")),
        )

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

    def test_store_append_error_aborts_not_a_combo_skip(self, tmp_path: Path) -> None:
        """A ResultsStoreError from a per-row store.append() propagates OUT (redo-not-skip).

        The finalists ``store.append(row)`` loop runs AFTER the combo
        ``metrics_for_mask`` loop (and BEFORE the flush already covered by
        ``test_store_flush_error_aborts_not_a_combo_skip``). A failure here — a
        run-identity conflict, a store-internal infra fault — must NOT be caught
        by the narrow ``except SweepComboError`` (which only wraps the per-combo
        ``metrics_for_mask`` raise): it propagates unchanged as a
        ``ResultsStoreError`` so the grid drivers ABORT instead of downgrading it
        to a single-tuple skip, and — because the raise is BEFORE the trial-stats
        sidecar / mark / completion-sentinel writes — the tuple is left
        UN-sentineled and the dedup UN-marked, so a resume deterministically
        REDOES the whole tuple (Golden Rule #7). ONE typed carve-out
        (DEC-VIX-STAMP-WIRE-001 round-2): a VIX-ONLY ``VixTrioConflictError``
        (VIX sits outside the run identity — the VIX lake moved, not the
        engine) IS demoted to a tuple-skip by the grid drivers; the PLAIN
        ``ResultsStoreError`` exercised here stays a grid abort.
        """
        from brutex.engine.results_store import ResultsStoreError
        from brutex.engine.trial_stats import trial_stats_sidecar_path
        from brutex.engine.tuple_complete import completion_sentinel_path

        bars = _trending_bars()
        cfg = _make_config()

        class _AppendFailsStore(ResultsStore):
            """A real store whose per-row append() raises an infra failure.

            The base ``flush`` is left intact — the append raise aborts the
            finalists loop BEFORE the flush is ever reached (asserted below by
            the absent store file), so no flush override is needed.
            """

            def append(self, row: object) -> bool:  # type: ignore[override]
                raise ResultsStoreError("simulated disk failure during append")

        store = _AppendFailsStore(path=tmp_path / "r.parquet")
        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(ResultsStoreError, match="simulated disk failure during append") as exc,
        ):
            run_persisted_sweep(
                bars,
                cfg,
                store=store,
                dedup=_dedup(),
                audit=audit,
                trial_stats_digest="ab12cd34ef56ab12",
            )
        # The raw store error, NOT a SweepComboError tuple-skip, anywhere in the chain.
        assert not isinstance(exc.value, SweepComboError)
        assert exc.value.__cause__ is None
        cause = exc.value.__context__
        while cause is not None:
            assert not isinstance(cause, SweepComboError)
            cause = cause.__context__
        # No store / sidecar / completion sentinel was written → the tuple redoes.
        assert not store.path.exists()
        assert not trial_stats_sidecar_path(store.path).exists()
        assert not completion_sentinel_path(store.path).exists()

    def test_completion_sentinel_write_error_propagates_un_sentineled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An OSError from the STRICTLY-LAST completion-sentinel write propagates (redo-not-skip).

        ``write_completion_sentinel`` is written last of all — after the store
        flush, the trial-stats sidecar, and ``mark_many`` — so its presence
        proves the whole sequence completed (DEC-MARK-MANY-SENTINEL-001). If the
        disk fails AT that final write, the error must PROPAGATE OUT (not be
        swallowed into a tuple-skip) AND leave the sentinel ABSENT, so a resume —
        which consults ONLY the sentinel — finds the tuple INCOMPLETE and
        deterministically REDOES it (Golden Rule #7). This pins the
        completion-sentinel boundary, the twin of the flush-error boundary.
        """
        from brutex.engine.tuple_complete import completion_sentinel_path

        bars = _trending_bars()
        cfg = _make_config()
        store = ResultsStore(path=tmp_path / "r.parquet")

        def _sentinel_write_fails(*_args: object, **_kwargs: object) -> object:
            raise OSError("simulated disk failure writing completion sentinel")

        # Patch the name as resolved inside run_persisted_sweep (the sweep_run
        # module namespace) — the string target avoids the re-export attr check.
        monkeypatch.setattr(
            "brutex.engine.sweep_run.write_completion_sentinel", _sentinel_write_fails
        )

        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(
                OSError, match="simulated disk failure writing completion sentinel"
            ) as exc,
        ):
            run_persisted_sweep(
                bars,
                cfg,
                store=store,
                dedup=_dedup(),
                audit=audit,
                trial_stats_digest="ab12cd34ef56ab12",
            )
        # The raw OSError is NOT downgraded into a SweepComboError tuple-skip.
        assert not isinstance(exc.value, SweepComboError)
        cause = exc.value.__context__
        while cause is not None:
            assert not isinstance(cause, SweepComboError)
            cause = cause.__context__
        # The sentinel — the redo authority — is ABSENT, so a resume redoes the tuple.
        assert not completion_sentinel_path(store.path).exists()

    def test_trial_stats_write_error_propagates_un_sentineled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An OSError from the trial-stats sidecar write propagates (redo-not-skip).

        ``write_tuple_trial_stats`` is written AFTER the store flush but BEFORE
        the strictly-last ``write_completion_sentinel`` (DEC-TRIAL-STATS-WRITE-001
        / DEC-MARK-MANY-SENTINEL-001). It sits OUTSIDE the narrow
        ``except SweepComboError`` that wraps only the per-combo
        ``metrics_for_mask`` loop, so a disk fault at this sidecar write must
        PROPAGATE OUT unchanged — never downgraded into a ``SweepComboError``
        tuple-skip (only a ``SweepComboError`` triggers the grid drivers'
        single-tuple skip + ``tuples_failed`` bump, so propagating a raw
        ``OSError`` keeps the failure a sweep-wide ABORT, not a combo-skip) — AND
        leave the completion sentinel ABSENT. A resume consults ONLY the
        sentinel, so an absent sentinel forces a deterministic FULL redo of the
        tuple (Golden Rule #7). This pins the trial-stats sidecar-write
        boundary — the twin of the flush-error, store-append-error, and
        completion-sentinel-write fault tests — completing the per-tuple
        finalize write-fault-injection matrix.
        """
        from brutex.engine.trial_stats import trial_stats_sidecar_path
        from brutex.engine.tuple_complete import completion_sentinel_path

        bars = _trending_bars()
        cfg = _make_config()
        store = ResultsStore(path=tmp_path / "r.parquet")

        def _trial_stats_write_fails(*_args: object, **_kwargs: object) -> object:
            raise OSError("simulated disk failure writing trial-stats sidecar")

        # Patch the name as resolved inside run_persisted_sweep (the sweep_run
        # module namespace) — the string target avoids the re-export attr check.
        monkeypatch.setattr(
            "brutex.engine.sweep_run.write_tuple_trial_stats", _trial_stats_write_fails
        )

        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(
                OSError, match="simulated disk failure writing trial-stats sidecar"
            ) as exc,
        ):
            run_persisted_sweep(
                bars,
                cfg,
                store=store,
                dedup=_dedup(),
                audit=audit,
                trial_stats_digest="ab12cd34ef56ab12",
            )
        # The raw OSError is NOT downgraded into a SweepComboError tuple-skip
        # (so the grid drivers ABORT instead of bumping tuples_failed + skipping).
        assert not isinstance(exc.value, SweepComboError)
        assert exc.value.__cause__ is None or not isinstance(exc.value.__cause__, SweepComboError)
        cause = exc.value.__context__
        while cause is not None:
            assert not isinstance(cause, SweepComboError)
            cause = cause.__context__
        # The sidecar write FAILED → no sidecar, and the strictly-last sentinel
        # was never reached → ABSENT, so a resume redoes the whole tuple.
        assert not trial_stats_sidecar_path(store.path).exists()
        assert not completion_sentinel_path(store.path).exists()


class TestResidentKernelBarMismatch:
    """A pre-built kernel whose bar count disagrees with the tuple's bars is rejected.

    ``run_persisted_sweep(..., kernel=)`` accepts a pre-built ``ResidentSweepKernel``
    to skip rebuilding ``bar_bits``; if the kernel was built from a DIFFERENT bar
    window than the tuple supplies, the sweep would silently score the wrong bars,
    so it raises ``ValueError`` at config time instead.
    """

    def test_kernel_built_from_other_bars_raises(self, tmp_path: Path) -> None:
        from brutex.engine.sweep_kernel import ResidentSweepKernel

        bars = _trending_bars()
        cfg = _make_config()
        # A kernel built from a SHORTER window than this tuple's bars.
        kernel = ResidentSweepKernel(bars[:-1], proximity=cfg.proximity, prior_n=cfg.prior_n)
        assert kernel.n_bars == len(bars) - 1
        store = ResultsStore(path=tmp_path / "r.parquet")
        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(ValueError, match="kernel has .* bars but .* bars were supplied"),
        ):
            run_persisted_sweep(
                bars,
                cfg,
                store=store,
                dedup=_dedup(),
                audit=audit,
                trial_stats_digest="ab12cd34ef56ab12",
                kernel=kernel,
            )

    def test_kernel_matching_bars_is_accepted(self, tmp_path: Path) -> None:
        """A kernel built from the SAME bars drives the sweep (the happy path)."""
        from brutex.engine.sweep_kernel import ResidentSweepKernel

        bars = _trending_bars()
        cfg = _make_config()
        kernel = ResidentSweepKernel(bars, proximity=cfg.proximity, prior_n=cfg.prior_n)
        assert kernel.n_bars == len(bars)
        store = ResultsStore(path=tmp_path / "r.parquet")
        with AuditWriter.open(tmp_path / "a.jsonl") as audit:
            summary = run_persisted_sweep(
                bars,
                cfg,
                store=store,
                dedup=_dedup(),
                audit=audit,
                trial_stats_digest="ab12cd34ef56ab12",
                kernel=kernel,
            )
        assert summary.enumerated >= 1

    def test_kernel_built_for_other_fib_anchor_raises(self, tmp_path: Path) -> None:
        """A kernel whose Fib anchor disagrees with the tuple's is rejected (R-1).

        DEC-NIFTY-FIB-AXIS-001: the pre-built ``bar_bits`` matrix is anchor-SPECIFIC,
        so a kernel built for a DIFFERENT Fib anchor would silently score the WRONG
        Group-4 levels; ``run_persisted_sweep`` fails-fast, exactly like the
        ``n_bars`` guard.
        """
        from datetime import date

        from brutex.calendars.nse import IST, is_trading_day
        from brutex.engine.sweep_kernel import ResidentSweepKernel
        from brutex.strategies.types import FibAnchorKind

        def _session_bar(ts_ist: datetime, o: float, c: float) -> CandleBar:
            hi = max(o, c) + 0.5
            lo = min(o, c) - 0.5
            return CandleBar(
                timestamp=ts_ist.astimezone(UTC),
                open=Decimal(str(round(o, 2))),
                high=Decimal(str(round(hi, 2))),
                low=Decimal(str(round(lo, 2))),
                close=Decimal(str(round(c, 2))),
                volume=100,
                open_interest=None,
            )

        # A rolling-weekly kernel resolves its anchor against REAL NSE trading days,
        # so build the bars over in-coverage 2021 sessions (one bar per session).
        trading_days: list[date] = []
        cursor = date(2021, 1, 4)
        while len(trading_days) < 12:
            if is_trading_day(cursor):
                trading_days.append(cursor)
            cursor += timedelta(days=1)
        bars: list[CandleBar] = []
        price = 10000.0
        for i, d in enumerate(trading_days):
            base = datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST)
            for m in range(3):
                o = price + m
                bars.append(_session_bar(base + timedelta(minutes=m), o, o + 1.0))
            price += 25.0 + (i % 3) * 5.0
        # The tuple sweeps the DEFAULT prev-day anchor …
        cfg = _make_config(condition_bits=[19, 20, 21, 22, 23], k=1)
        assert cfg.fib_anchor is FibAnchorKind.PREV_DAY
        # … but the kernel was built for a rolling-weekly anchor over the SAME bars
        # (so the n_bars guard passes and the anchor guard is the one that trips).
        kernel = ResidentSweepKernel(
            bars,
            proximity=cfg.proximity,
            prior_n=cfg.prior_n,
            fib_anchor=FibAnchorKind.ROLLING_WEEKLY,
        )
        assert kernel.n_bars == len(bars)
        assert kernel.fib_anchor is FibAnchorKind.ROLLING_WEEKLY
        store = ResultsStore(path=tmp_path / "r.parquet")
        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(ValueError, match="kernel was built for Fib anchor"),
        ):
            run_persisted_sweep(
                bars,
                cfg,
                store=store,
                dedup=_dedup(),
                audit=audit,
                trial_stats_digest="ab12cd34ef56ab12",
                kernel=kernel,
            )


class TestResidentKernelRollScheduleMismatch:
    """The ADR-0012 roll-schedule twin of the Fib-anchor kernel guard.

    A pre-built kernel's ``bar_bits`` are schedule-SPECIFIC (the per-contract
    hard resets move with the boundaries), so a kernel built under a DIFFERENT
    schedule than the tuple's would silently misplace every roll boundary
    (DEC-STAGE1A-ROLL-RESET-001) — fail-fast, exactly like the ``n_bars`` /
    anchor guards.
    """

    def test_kernel_built_for_other_schedule_raises(self, tmp_path: Path) -> None:
        bars = _trending_bars()
        cfg = _make_config(instrument="NSE-NIFTY-FUT", roll_schedule=_FUT_SCHEDULE)
        # SAME bars, SAME anchor — only the schedule disagrees (a one-entry
        # covering schedule vs the tuple's two-entry one).
        kernel = ResidentSweepKernel(
            bars,
            proximity=cfg.proximity,
            prior_n=cfg.prior_n,
            roll_schedule=(_FUT_SCHEDULE[-1],),
        )
        assert kernel.n_bars == len(bars)
        store = ResultsStore(path=tmp_path / "r.parquet")
        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(ValueError, match="kernel was built for roll_schedule"),
        ):
            run_persisted_sweep(
                bars,
                cfg,
                store=store,
                dedup=_dedup(),
                audit=audit,
                trial_stats_digest="ab12cd34ef56ab12",
                kernel=kernel,
            )

    def test_scheduleless_kernel_for_a_futures_tuple_raises(self, tmp_path: Path) -> None:
        # None vs tuple is ALSO a mismatch — a spot-built kernel can never
        # serve a futures tuple (its bits never hard-reset at the rolls).
        bars = _trending_bars()
        cfg = _make_config(instrument="NSE-NIFTY-FUT", roll_schedule=_FUT_SCHEDULE)
        kernel = ResidentSweepKernel(bars, proximity=cfg.proximity, prior_n=cfg.prior_n)
        store = ResultsStore(path=tmp_path / "r.parquet")
        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(ValueError, match="kernel was built for roll_schedule"),
        ):
            run_persisted_sweep(
                bars,
                cfg,
                store=store,
                dedup=_dedup(),
                audit=audit,
                trial_stats_digest="ab12cd34ef56ab12",
                kernel=kernel,
            )

    def test_matching_schedule_kernel_is_accepted(self, tmp_path: Path) -> None:
        pytest.importorskip("polars")
        bars = _trending_bars(n_days=4)
        cfg = _make_config(instrument="NSE-NIFTY-FUT", roll_schedule=_FUT_SCHEDULE)
        kernel = ResidentSweepKernel(
            bars,
            proximity=cfg.proximity,
            prior_n=cfg.prior_n,
            roll_schedule=_FUT_SCHEDULE,
        )
        summary = _run_sweep(tmp_path, "ok", bars, cfg, kernel=kernel)
        assert summary.evaluated > 0

    def test_kernel_built_with_other_1day_mask_flag_raises(self, tmp_path: Path) -> None:
        # The D12 twin (review round-1 finding 5): a hand-built kernel whose
        # ``apply_1day_mask`` flag disagrees with the tuple's derived flag
        # carries bar_bits with the intraday-only bits (ToD 44-47 / VWAP
        # 52-53) wrongly set/cleared — fail-fast, exactly like the schedule
        # guard (unreachable via the production cache, whose key contains
        # instrument+tf, the flag's only inputs).
        from brutex.engine.stage1a import apply_1day_mask_for

        bars = _trending_bars()
        cfg = _make_config()
        assert apply_1day_mask_for(cfg.instrument, cfg.tf) is False
        kernel = ResidentSweepKernel(
            bars,
            proximity=cfg.proximity,
            prior_n=cfg.prior_n,
            apply_1day_mask=True,
        )
        assert kernel.n_bars == len(bars)
        store = ResultsStore(path=tmp_path / "r.parquet")
        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(ValueError, match="kernel was built with apply_1day_mask"),
        ):
            run_persisted_sweep(
                bars,
                cfg,
                store=store,
                dedup=_dedup(),
                audit=audit,
                trial_stats_digest="ab12cd34ef56ab12",
                kernel=kernel,
            )


# ────── DEC-SWEEP-RUST-TRADESIM-WIRE-001 — the wired resident Rust trade-sim ──────


class TestTradeSimPathObservability:
    """The engine choice (Rust kernel vs Python oracle) is observable, never silent.

    A silently Python-degraded sweep would be a ~40× fake-performance illusion
    (Golden Rule #10) AND would make every wired-vs-unwired CI arm degenerate to
    Python==Python — so the wired path ASSERTS the kernel actually engaged.
    """

    def test_wired_sweep_engages_rust_and_logs_path(self, tmp_path: Path) -> None:
        bars = _trending_bars()
        cfg = _make_config()
        with capture_logs() as logs:
            summary = _run_sweep(tmp_path, "w", bars, cfg)
        events = [e for e in logs if e["event"] == "sweep_run.trade_sim_path"]
        assert len(events) == 1  # once per tuple, never per mask/chunk
        # The wheel is built in CI — the production-wired sweep MUST take the
        # Rust path (the golden gate is vacuous if this silently reads False).
        assert events[0]["rust_active"] is True
        assert events[0]["n_bars"] == len(bars)
        assert events[0]["instrument"] == "NIFTY"
        assert summary.persisted > 0

    def test_forced_fallback_logs_python_path_and_identical_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bars = _trending_bars()
        cfg = _make_config()
        wired = _run_sweep(tmp_path, "w", bars, cfg)
        monkeypatch.setattr("brutex.engine.sweep_run.ResidentTradeSim", _forced_fallback_sim)
        with capture_logs() as logs:
            fallback = _run_sweep(tmp_path, "f", bars, cfg)
        events = [e for e in logs if e["event"] == "sweep_run.trade_sim_path"]
        assert len(events) == 1
        assert events[0]["rust_active"] is False
        # Bit-identity: the Python arm produces the SAME store, byte for byte.
        assert fallback.store_content_hash == wired.store_content_hash
        assert (tmp_path / "f.parquet").read_bytes() == (tmp_path / "w.parquet").read_bytes()

    def test_no_survivors_builds_no_sim(self, tmp_path: Path) -> None:
        """An all-pruned tuple never constructs the trade-sim (no wasted ctor)."""
        bars = _trending_bars()
        cfg = _make_config(min_hits=10_000_000)  # Stage-1B drops every survivor
        with capture_logs() as logs:
            summary = _run_sweep(tmp_path, "e", bars, cfg)
        assert summary.persisted == 0
        assert not any(e["event"] == "sweep_run.trade_sim_path" for e in logs)


class TestTradeSimChunkSize:
    """`_trade_sim_chunk_masks` — the count cap × cell budget × ≥1 floor."""

    def test_small_window_uses_mask_cap(self) -> None:
        from brutex.engine.sweep_run import _TRADE_SIM_CHUNK_MASKS, _trade_sim_chunk_masks

        # 1800-bar unit fixture: cells budget allows far more than the cap.
        assert _trade_sim_chunk_masks(1_800) == _TRADE_SIM_CHUNK_MASKS

    def test_deep_window_bounded_by_cells(self) -> None:
        from brutex.engine.sweep_run import _TRADE_SIM_CHUNK_CELLS, _trade_sim_chunk_masks

        # A production multi-year 1-minute window (~500k bars) chunks by cells.
        assert _trade_sim_chunk_masks(500_000) == _TRADE_SIM_CHUNK_CELLS // 500_000
        assert _trade_sim_chunk_masks(500_000) < 256

    def test_floor_is_one_beyond_the_cell_budget(self) -> None:
        from brutex.engine.sweep_run import (
            _TRADE_SIM_CHUNK_CELLS,
            _TRADE_SIM_CHUNK_MASKS,
            _trade_sim_chunk_masks,
        )

        assert _trade_sim_chunk_masks(_TRADE_SIM_CHUNK_CELLS * 2) == 1
        # Degenerate div-by-zero guard (max(1, n_bars)) yields the plain cap;
        # unreachable in production — run_persisted_sweep validates bars non-empty.
        assert _trade_sim_chunk_masks(0) == _TRADE_SIM_CHUNK_MASKS

    def test_cap_boundary_is_exact(self) -> None:
        from brutex.engine.sweep_run import (
            _TRADE_SIM_CHUNK_CELLS,
            _TRADE_SIM_CHUNK_MASKS,
            _trade_sim_chunk_masks,
        )

        boundary = _TRADE_SIM_CHUNK_CELLS // _TRADE_SIM_CHUNK_MASKS
        assert _trade_sim_chunk_masks(boundary) == _TRADE_SIM_CHUNK_MASKS
        assert _trade_sim_chunk_masks(boundary + 1) == _TRADE_SIM_CHUNK_MASKS - 1


class TestTradeSimChunkInvariance:
    """Chunking is a pure batching choice — results are chunk-size-INVARIANT.

    The chunking of `kept` is the only new algorithmic code the wiring adds; an
    off-by-one at a chunk boundary (overlap / skip / reorder) would mis-align
    mask → trades → metrics → run_id and poison the store. These tests use the
    CRAFTED fixture whose five masks have pairwise-distinct metrics, so any
    misalignment flips the store bytes.
    """

    def _distinct_expected(
        self, bars: list[CandleBar], kernel: ResidentSweepKernel, cfg: SweepRunConfig
    ) -> dict[int, object]:
        """Per-mask oracle metrics; asserts the fixture's pairwise-distinct teeth."""
        expected = {
            1 << b: metrics_for_mask(
                bars,
                kernel.bar_bits,
                1 << b,
                direction=cfg.direction,
                tf_minutes=cfg.tf_minutes,
                stop_loss_paisa=cfg.sl_paisa,
                take_profit_paisa=cfg.tp_paisa,
                start_equity_paisa=cfg.start_equity_paisa,
            )
            for b in range(5)
        }
        fingerprints = {
            (m.net_pnl_paisa, m.total_trades, m.sharpe_ratio) for m in expected.values()
        }
        assert len(fingerprints) == 5, "fixture lost its teeth: masks must differ pairwise"
        return dict(expected)

    @pytest.mark.parametrize("chunk", [1, 2, 3, 5, 7])
    def test_chunk_sizes_produce_byte_identical_stores(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chunk: int
    ) -> None:
        """{1, prime, ==len, >len} chunks — incl. len%chunk != 0 — same bytes."""
        bars = _trending_bars()
        kernel = _crafted_kernel(bars)
        cfg = _make_config(min_hits=0)
        baseline = _run_sweep(tmp_path, "base", bars, cfg, kernel=kernel)
        assert baseline.persisted > 0  # the invariance claim is non-vacuous
        monkeypatch.setattr("brutex.engine.sweep_run._trade_sim_chunk_masks", lambda n_bars: chunk)
        run = _run_sweep(tmp_path, f"c{chunk}", bars, cfg, kernel=kernel)
        assert run.store_content_hash == baseline.store_content_hash
        assert (tmp_path / f"c{chunk}.parquet").read_bytes() == (
            tmp_path / "base.parquet"
        ).read_bytes()

    def test_chunked_masks_align_with_per_mask_oracle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each persisted row's metrics == the ORACLE metrics for ITS mask.

        Chunk size 2 over 5 masks (a ragged final chunk) — every stored row is
        joined back to its mask via the persisted u128 limbs and compared
        field-by-field against a direct `metrics_for_mask` call, so a
        boundary swap cannot hide.
        """
        import polars as pl

        bars = _trending_bars()
        kernel = _crafted_kernel(bars)
        cfg = _make_config(min_hits=0)
        expected = self._distinct_expected(bars, kernel, cfg)
        monkeypatch.setattr("brutex.engine.sweep_run._trade_sim_chunk_masks", lambda n_bars: 2)
        summary = _run_sweep(tmp_path, "a", bars, cfg, kernel=kernel)
        assert summary.persisted > 0
        rows = pl.read_parquet(tmp_path / "a.parquet")
        assert rows.height == summary.persisted
        for row in rows.iter_rows(named=True):
            # The u128 mask persists as two OFFSET-encoded signed-Int64 limbs
            # (`limb - 2**63`, results_store._encode_limb) — invert the offset.
            mask = ((row["mask_bits_hi"] + (1 << 63)) << 64) | (row["mask_bits_lo"] + (1 << 63))
            metrics = expected[mask]
            assert row["net_pnl_paisa"] == metrics.net_pnl_paisa  # type: ignore[attr-defined]
            assert row["total_trades"] == metrics.total_trades  # type: ignore[attr-defined]
            assert row["sharpe_ratio"] == metrics.sharpe_ratio  # type: ignore[attr-defined]


class TestTradeSimConstructionFailure:
    """A trade-sim ctor failure lands as the #904 tuple-skip, never a grid abort.

    The construction seam sits OUTSIDE the per-mask try/except; the grid drivers
    catch ONLY SweepComboError — so the ctor wrap must map an Exception to the
    tuple-skip contract (and re-raise MemoryError bare: OOM is a GLOBAL stop).
    """

    def test_ctor_exception_maps_to_sweep_combo_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.engine.trial_stats import trial_stats_sidecar_path
        from brutex.engine.tuple_complete import completion_sentinel_path

        bars = _trending_bars()
        cfg = _make_config()
        monkeypatch.setattr(
            "brutex.engine.sweep_run.ResidentTradeSim",
            _ctor_raising_sim(lambda: ValueError("corrupt price column")),
        )
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
        assert "trade-sim construction" in msg
        assert f"{len(bars)} bars" in msg
        assert "NIFTY/1m/bull" in msg
        assert "k=1" in msg
        assert "corrupt price column" in msg
        assert isinstance(excinfo.value.__cause__, ValueError)
        # Tuple-skip semantics: nothing written → deterministic redo next run.
        assert not store.path.exists()
        assert not trial_stats_sidecar_path(store.path).exists()
        assert not completion_sentinel_path(store.path).exists()

    def test_ctor_memory_error_propagates_bare(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bars = _trending_bars()
        cfg = _make_config()
        monkeypatch.setattr(
            "brutex.engine.sweep_run.ResidentTradeSim",
            _ctor_raising_sim(lambda: MemoryError("ctor OOM")),
        )
        store = ResultsStore(path=tmp_path / "r.parquet")
        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(MemoryError, match="ctor OOM") as excinfo,
        ):
            run_persisted_sweep(
                bars,
                cfg,
                store=store,
                dedup=_dedup(),
                audit=audit,
                trial_stats_digest="ab12cd34ef56ab12",
            )
        assert not isinstance(excinfo.value, SweepComboError)
        assert excinfo.value.__cause__ is None
        assert not store.path.exists()


class TestChunkFailureReattribution:
    """The chunk-failure → per-mask-oracle re-attribution contract."""

    @pytest.mark.parametrize("culprit_bit", [0, 2, 4])  # first / middle / last of the chunk
    def test_culprit_position_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, culprit_bit: int
    ) -> None:
        from brutex.engine.sweep_driver import metrics_for_mask as real_metrics

        bars = _trending_bars()
        cfg = _make_config()  # kept = masks 1,2,4,8,16 in ONE default chunk
        boom_mask = 1 << culprit_bit
        monkeypatch.setattr(
            "brutex.engine.sweep_run.ResidentTradeSim",
            _chunk_raising_sim(lambda: RuntimeError("batch boom")),
        )

        def _raising(*args: object, **kwargs: object) -> object:
            if args[2] == boom_mask:
                raise ValueError("culprit mask")
            return real_metrics(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("brutex.engine.sweep_run.metrics_for_mask", _raising)
        store = ResultsStore(path=tmp_path / "r.parquet")
        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(SweepComboError) as excinfo,
        ):
            run_persisted_sweep(bars, cfg, store=store, dedup=_dedup(), audit=audit)
        assert f"0x{boom_mask:032x}" in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, ValueError)

    def test_multiple_culprits_names_the_first_in_kept_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.engine.sweep_driver import metrics_for_mask as real_metrics

        bars = _trending_bars()
        cfg = _make_config()
        culprits = {1 << 1, 1 << 3}
        monkeypatch.setattr(
            "brutex.engine.sweep_run.ResidentTradeSim",
            _chunk_raising_sim(lambda: RuntimeError("batch boom")),
        )

        def _raising(*args: object, **kwargs: object) -> object:
            if args[2] in culprits:
                raise ValueError("culprit mask")
            return real_metrics(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("brutex.engine.sweep_run.metrics_for_mask", _raising)
        store = ResultsStore(path=tmp_path / "r.parquet")
        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(SweepComboError) as excinfo,
        ):
            run_persisted_sweep(bars, cfg, store=store, dedup=_dedup(), audit=audit)
        # Deterministic: the FIRST culprit in kept order (mask 2), never mask 8.
        assert f"0x{1 << 1:032x}" in str(excinfo.value)
        assert f"0x{1 << 3:032x}" not in str(excinfo.value)

    def test_no_culprit_fails_loud_never_adopts_oracle_results(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Rust-only batch failure (every mask passes the oracle) fails LOUD.

        Silently adopting the oracle's per-mask results would mask a
        kernel-divergence signal and make engine provenance non-deterministic
        across runs — the error must name the CHUNK, chain the ORIGINAL batch
        exception, and leave every artifact unwritten.
        """
        from brutex.engine.trial_stats import trial_stats_sidecar_path
        from brutex.engine.tuple_complete import completion_sentinel_path

        bars = _trending_bars()
        cfg = _make_config()
        monkeypatch.setattr(
            "brutex.engine.sweep_run.ResidentTradeSim",
            _chunk_raising_sim(lambda: RuntimeError("pyo3 marshal failure")),
        )
        # metrics_for_mask is NOT patched — the real oracle passes every mask.
        store = ResultsStore(path=tmp_path / "r.parquet")
        with (
            capture_logs() as logs,
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
        assert "trade-sim chunk of 5 masks" in msg
        assert f"0x{1:032x}..0x{16:032x}" in msg
        assert "pyo3 marshal failure" in msg
        assert "NOT" in msg and "adopted" in msg
        assert "NIFTY/1m/bull" in msg
        assert isinstance(excinfo.value.__cause__, RuntimeError)
        # The chunk failure was WARNed before re-attribution began.
        warn = [e for e in logs if e["event"] == "sweep_run.trade_sim_chunk_failed"]
        assert len(warn) == 1
        assert warn[0]["error_type"] == "RuntimeError"
        assert warn[0]["n_masks"] == 5
        # Results NOT adopted: no store / sidecar / sentinel — the tuple redoes.
        assert not store.path.exists()
        assert not trial_stats_sidecar_path(store.path).exists()
        assert not completion_sentinel_path(store.path).exists()

    def test_memory_error_during_reattribution_propagates_bare(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OOM inside the oracle re-run is STILL a global stop, never a tuple skip."""
        bars = _trending_bars()
        cfg = _make_config()
        monkeypatch.setattr(
            "brutex.engine.sweep_run.ResidentTradeSim",
            _chunk_raising_sim(lambda: RuntimeError("batch boom")),
        )

        def _oom(*_args: object, **_kwargs: object) -> object:
            raise MemoryError("oracle OOM")

        monkeypatch.setattr("brutex.engine.sweep_run.metrics_for_mask", _oom)
        store = ResultsStore(path=tmp_path / "r.parquet")
        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(MemoryError, match="oracle OOM") as excinfo,
        ):
            run_persisted_sweep(bars, cfg, store=store, dedup=_dedup(), audit=audit)
        assert not isinstance(excinfo.value, SweepComboError)
        assert excinfo.value.__cause__ is None  # bare re-raise, no wrap
        assert not store.path.exists()


class TestChunkLengthContract:
    """A wrong-LENGTH batch return rides the tuple-skip ladder, never a grid abort.

    Before the guard, `zip(chunk, chunk_trades, strict=True)` sat OUTSIDE the
    chunk try — a count-mismatched return escaped as a bare `ValueError` past
    the grid drivers' `except SweepComboError` (#904), aborting the WHOLE grid
    with no TUPLE_FAILED line. The guard raises INSIDE the chunk try, so the
    mismatch is re-attributed (every mask passes the oracle — batch-level) and
    fails LOUD as a chunk-naming `SweepComboError` chained from the length
    `ValueError` — the documented tuple-skip contract, uniformly.
    """

    def test_short_return_maps_to_chunk_sweep_combo_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bars = _trending_bars()
        cfg = _make_config()  # kept masks: 1, 2, 4, 8, 16 — one chunk of 5
        monkeypatch.setattr("brutex.engine.sweep_run.ResidentTradeSim", _ShortChunkSim)
        store = ResultsStore(path=tmp_path / "r.parquet")
        with (
            capture_logs() as logs,
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(SweepComboError) as excinfo,
        ):
            run_persisted_sweep(bars, cfg, store=store, dedup=_dedup(), audit=audit)
        msg = str(excinfo.value)
        # The SweepComboError names the CHUNK + carries the length ValueError.
        assert "trade-sim chunk of 5 masks" in msg
        assert "returned 4 trade/exit-index pairs for a chunk of 5 masks" in msg
        assert "batch-contract violation" in msg
        assert "every mask passed the per-mask Python-oracle re-run" in msg
        assert isinstance(excinfo.value.__cause__, ValueError)
        # The chunk failure was logged before re-attribution, and nothing was
        # persisted (the tuple is left un-flushed for a deterministic redo).
        events = [e for e in logs if e["event"] == "sweep_run.trade_sim_chunk_failed"]
        assert len(events) == 1
        assert events[0]["error_type"] == "ValueError"
        assert not store.path.exists()


class TestMetricsTailFailure:
    """The per-mask metrics tail keeps the fail-loud fingerprint + OOM carve-out."""

    def test_tail_error_names_the_offending_mask(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.engine.sweep_driver import metrics_from_trades as real_tail

        bars = _trending_bars()
        cfg = _make_config()  # kept order: masks 1, 2, 4, 8, 16
        calls = {"n": 0}

        def _tail(*args: object, **kwargs: object) -> object:
            calls["n"] += 1
            if calls["n"] == 3:  # the 3rd kept mask = 0b100
                raise ValueError("tail boom")
            return real_tail(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("brutex.engine.sweep_run.metrics_from_trades", _tail)
        store = ResultsStore(path=tmp_path / "r.parquet")
        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(SweepComboError) as excinfo,
        ):
            run_persisted_sweep(bars, cfg, store=store, dedup=_dedup(), audit=audit)
        msg = str(excinfo.value)
        assert f"0x{0b100:032x}" in msg
        assert "run_id=" in msg
        assert "tail boom" in msg
        assert isinstance(excinfo.value.__cause__, ValueError)

    def test_tail_memory_error_propagates_bare(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bars = _trending_bars()
        cfg = _make_config()

        def _oom(*_args: object, **_kwargs: object) -> object:
            raise MemoryError("tail OOM")

        monkeypatch.setattr("brutex.engine.sweep_run.metrics_from_trades", _oom)
        store = ResultsStore(path=tmp_path / "r.parquet")
        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(MemoryError, match="tail OOM") as excinfo,
        ):
            run_persisted_sweep(bars, cfg, store=store, dedup=_dedup(), audit=audit)
        assert not isinstance(excinfo.value, SweepComboError)
        assert excinfo.value.__cause__ is None
        assert not store.path.exists()


class TestU128EnvelopeThroughSweep:
    """Wide (>64-bit, ≤ u128) words are RUST-served through the whole sweep.

    DEC-TRADE-SIM-U128-KERNEL-001: `iter_masks_k_of_bits` accepts bits up to
    127 (MAX_VOCAB_BITS = 128) and the seam now width-dispatches onto the u128
    sibling kernel — the old whole-chunk oracle degrade for a wide mask is
    GONE, byte-equality with the forced-Python run preserved. Only a word
    beyond even the u128 envelope still routes to the oracle (never a PyO3
    OverflowError).
    """

    def test_u128_mask_sweep_rust_served_equals_forced_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bit 0 FIRES on `_trending_bars` (the default-config masks produce real
        # trades), bits 64/65 never do — the wide masks now PROMOTE the batch
        # to the u128 kernel (no call fallback), and the byte-equality below
        # compares stores with REAL trade rows, never two degenerate
        # zero-trade stores (the vacuous-equality trap).
        bars = _trending_bars()
        cfg = _make_config(condition_bits=[0, 64, 65], k=1, min_hits=0)
        with capture_logs() as logs:
            wired = _run_sweep(tmp_path, "w", bars, cfg)
        # NOT a fallback any more: the wide sibling kernel was promoted once.
        fallbacks = [e for e in logs if e["event"] == "rust_trade_sim.call_fallback"]
        assert fallbacks == []
        promotions = [e for e in logs if e["event"] == "rust_trade_sim.wide_kernel_promoted"]
        assert len(promotions) == 1
        # The sweep-level path signal reads RUST for the wide-mask tuple.
        paths = [e for e in logs if e["event"] == "sweep_run.trade_sim_path"]
        assert paths[0]["rust_active"] is True
        # Non-vacuous: the ranking gate drops the two zero-trade u128-bit
        # masks, so EXACTLY the firing mask 0b1 persists — the byte-equality
        # below compares stores carrying a real trade-bearing row, never two
        # empty/degenerate stores. Its trades are pinned via the independent
        # metrics oracle.
        assert wired.evaluated == 3
        assert wired.persisted == 1
        direct = metrics_for_mask(
            bars,
            build_bar_bits(bars),
            1,
            direction=cfg.direction,
            tf_minutes=cfg.tf_minutes,
            stop_loss_paisa=cfg.sl_paisa,
            take_profit_paisa=cfg.tp_paisa,
            start_equity_paisa=cfg.start_equity_paisa,
        )
        assert direct.total_trades > 0
        monkeypatch.setattr("brutex.engine.sweep_run.ResidentTradeSim", _forced_fallback_sim)
        fallback = _run_sweep(tmp_path, "f", bars, cfg)
        assert wired.persisted == fallback.persisted == 1
        assert wired.store_content_hash == fallback.store_content_hash
        assert (tmp_path / "w.parquet").read_bytes() == (tmp_path / "f.parquet").read_bytes()

    def test_u128_bar_bits_rust_served_at_construction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A wide bar_bits word now builds the u128 kernel at CONSTRUCTION —
        # rust_active True for the wide window (the item-4 plan's sweep-level
        # pin) — and the results stay byte-equal to the forced-Python run.
        bars = _trending_bars()
        kernel = ResidentSweepKernel(bars)
        kernel.bar_bits[0] |= 1 << 100  # the documented u128 headroom, in-place
        cfg = _make_config(min_hits=0)
        with capture_logs() as logs:
            wired = _run_sweep(tmp_path, "w", bars, cfg, kernel=kernel)
        # No construction fallback …
        fallbacks = [e for e in logs if e["event"] == "rust_trade_sim.fallback"]
        assert fallbacks == []
        # … and the sweep-level path signal reads RUST for the wide window.
        paths = [e for e in logs if e["event"] == "sweep_run.trade_sim_path"]
        assert paths[0]["rust_active"] is True
        monkeypatch.setattr("brutex.engine.sweep_run.ResidentTradeSim", _forced_fallback_sim)
        fallback = _run_sweep(tmp_path, "f", bars, cfg, kernel=kernel)
        assert wired.store_content_hash == fallback.store_content_hash
        assert (tmp_path / "w.parquet").read_bytes() == (tmp_path / "f.parquet").read_bytes()

    def test_beyond_u128_bar_bits_fall_back_at_construction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Beyond even the u128 envelope the fallback law is unchanged: whole-
        # object oracle, loud, byte-equal — never a PyO3 OverflowError.
        bars = _trending_bars()
        kernel = ResidentSweepKernel(bars)
        kernel.bar_bits[0] |= 1 << 130  # beyond MAX_VOCAB_BITS = 128
        cfg = _make_config(min_hits=0)
        with capture_logs() as logs:
            wired = _run_sweep(tmp_path, "w", bars, cfg, kernel=kernel)
        fallbacks = [e for e in logs if e["event"] == "rust_trade_sim.fallback"]
        assert len(fallbacks) == 1
        assert fallbacks[0]["reason"] == "bar_bits_beyond_u128"
        paths = [e for e in logs if e["event"] == "sweep_run.trade_sim_path"]
        assert paths[0]["rust_active"] is False
        monkeypatch.setattr("brutex.engine.sweep_run.ResidentTradeSim", _forced_fallback_sim)
        fallback = _run_sweep(tmp_path, "f", bars, cfg, kernel=kernel)
        assert wired.store_content_hash == fallback.store_content_hash
        assert (tmp_path / "w.parquet").read_bytes() == (tmp_path / "f.parquet").read_bytes()


class TestMixedEraCheckpointResume:
    """A tuple completed under the PYTHON engine resumes cleanly under the WIRED build.

    The exact production upgrade scenario this PR creates: the sentinel/digest
    machinery hashes results, never the engine — safe ONLY because of
    bit-identity, so both directions are pinned here.
    """

    def test_python_era_sentinel_resumes_and_redoes_byte_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.engine.tuple_complete import completion_sentinel_path

        bars = _trending_bars()
        cfg = _make_config()
        digest = "ab12cd34ef56ab12"
        store_path = tmp_path / "t.parquet"

        def _resume_run(tag: str) -> SweepRunSummary:
            # Same STORE path (the tuple's artifact), fresh audit chain per era.
            store = ResultsStore(path=store_path)
            with AuditWriter.open(tmp_path / f"{tag}.jsonl") as audit:
                return run_persisted_sweep(
                    bars, cfg, store=store, dedup=_dedup(), audit=audit, trial_stats_digest=digest
                )

        # Era 1: the tuple completes on the PYTHON engine (pre-wiring build).
        with monkeypatch.context() as m:
            m.setattr("brutex.engine.sweep_run.ResidentTradeSim", _forced_fallback_sim)
            first = _run_sweep(tmp_path, "t", bars, cfg, digest=digest)
        assert first.sentinel_written
        python_era_bytes = store_path.read_bytes()
        # Era 2a: the WIRED build resumes — the sentinel short-circuits, nothing
        # is touched, and the recorded hash still pairs with the Python-era store.
        resumed = _resume_run("t2a")
        assert resumed.resumed_complete
        assert resumed.store_content_hash == first.store_content_hash
        assert store_path.read_bytes() == python_era_bytes
        # Era 2b: force a FULL wired redo (sentinel gone) — the Rust engine
        # rewrites the Python-era artifacts byte-identically (Golden Rule #7).
        completion_sentinel_path(store_path).unlink()
        redone = _resume_run("t2b")
        assert not redone.resumed_complete
        assert redone.sentinel_written
        assert redone.store_content_hash == first.store_content_hash
        assert store_path.read_bytes() == python_era_bytes


class TestSweepLookaheadGuardWiring:
    """The opt-in sweep look-ahead barrier is wired at run_persisted_sweep's setup.

    DEC-SWEEP-LOOKAHEAD-GUARD-001: the env flag is resolved ONCE per sweep run
    (never per bar); OFF keeps the hot path byte-identical (the identical bars
    object flows through), ON wraps the window in a ``GuardedBarWindow`` — and
    the guarded run's persisted output is bit-identical to the unguarded run's
    (the barrier observes, it never alters the computation).
    """

    def _run(
        self, tmp_path: Path, tag: str, bars: list[CandleBar]
    ) -> tuple[SweepRunSummary, bytes]:
        store_path = tmp_path / f"{tag}.parquet"
        store = ResultsStore(path=store_path)
        with AuditWriter.open(tmp_path / f"{tag}.jsonl") as audit:
            summary = run_persisted_sweep(
                bars, _make_config(), store=store, dedup=_dedup(), audit=audit
            )
        return summary, store_path.read_bytes()

    def test_guard_on_is_bit_identical_to_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.engine.sweep_lookahead import SWEEP_LOOKAHEAD_GUARD_ENV

        bars = _trending_bars()
        monkeypatch.delenv(SWEEP_LOOKAHEAD_GUARD_ENV, raising=False)
        off_summary, off_bytes = self._run(tmp_path, "off", bars)
        monkeypatch.setenv(SWEEP_LOOKAHEAD_GUARD_ENV, "1")
        on_summary, on_bytes = self._run(tmp_path, "on", bars)
        assert on_summary == off_summary
        assert on_bytes == off_bytes

    def test_guard_on_is_bit_identical_to_off_on_the_kernel_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Hostile-review F1: the production grid path supplies a pre-built
        # ResidentSweepKernel, so build_bar_bits is SKIPPED and the guarded
        # window's ARMED first sequential pass is the trade-sim walk — a
        # different code path from the kernel-less tests above. Pin ON≡OFF
        # bit-identity there too (summary + persisted store bytes).
        from brutex.engine.sweep_kernel import ResidentSweepKernel
        from brutex.engine.sweep_lookahead import SWEEP_LOOKAHEAD_GUARD_ENV

        bars = _trending_bars()
        cfg = _make_config()
        kernel = ResidentSweepKernel(bars, proximity=cfg.proximity, prior_n=cfg.prior_n)

        def _run_with_kernel(tag: str) -> tuple[SweepRunSummary, bytes]:
            store_path = tmp_path / f"{tag}.parquet"
            store = ResultsStore(path=store_path)
            with AuditWriter.open(tmp_path / f"{tag}.jsonl") as audit:
                summary = run_persisted_sweep(
                    bars, cfg, store=store, dedup=_dedup(), audit=audit, kernel=kernel
                )
            return summary, store_path.read_bytes()

        monkeypatch.delenv(SWEEP_LOOKAHEAD_GUARD_ENV, raising=False)
        off_summary, off_bytes = _run_with_kernel("kernel-off")
        monkeypatch.setenv(SWEEP_LOOKAHEAD_GUARD_ENV, "1")
        on_summary, on_bytes = _run_with_kernel("kernel-on")
        assert on_summary == off_summary
        assert on_bytes == off_bytes

    def test_guard_env_on_actually_wraps_the_bar_window(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Prove the seam is LIVE: with the env flag ON, the sequence every
        # downstream phase consumes is the GuardedBarWindow, not the raw list.
        from brutex.engine.sweep_lookahead import (
            SWEEP_LOOKAHEAD_GUARD_ENV,
            GuardedBarWindow,
            maybe_guard_bars,
        )

        monkeypatch.setenv(SWEEP_LOOKAHEAD_GUARD_ENV, "1")
        seen: list[object] = []

        def _spy(bars: object) -> object:
            result = maybe_guard_bars(bars)  # type: ignore[arg-type]
            seen.append(result)
            return result

        # String-target setattr: patches the name inside brutex.engine.sweep_run
        # without a second import style for the module (CodeQL py/import-and-import-from).
        monkeypatch.setattr("brutex.engine.sweep_run.maybe_guard_bars", _spy)
        store = ResultsStore(path=tmp_path / "r.parquet")
        with AuditWriter.open(tmp_path / "a.jsonl") as audit:
            run_persisted_sweep(
                _trending_bars(), _make_config(), store=store, dedup=_dedup(), audit=audit
            )
        assert len(seen) == 1
        assert isinstance(seen[0], GuardedBarWindow)

    def test_guard_off_passes_the_identical_object_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The zero-cost OFF contract at the wiring site: the seam hands the
        # SAME bars object downstream (no wrapper, no per-bar cost).
        from brutex.engine.sweep_lookahead import SWEEP_LOOKAHEAD_GUARD_ENV, maybe_guard_bars

        monkeypatch.delenv(SWEEP_LOOKAHEAD_GUARD_ENV, raising=False)
        seen: list[tuple[object, object]] = []

        def _spy(bars: object) -> object:
            result = maybe_guard_bars(bars)  # type: ignore[arg-type]
            seen.append((bars, result))
            return result

        # String-target setattr (same rationale as the ON-mode wiring test).
        monkeypatch.setattr("brutex.engine.sweep_run.maybe_guard_bars", _spy)
        bars = _trending_bars()
        store = ResultsStore(path=tmp_path / "r.parquet")
        with AuditWriter.open(tmp_path / "a.jsonl") as audit:
            run_persisted_sweep(bars, _make_config(), store=store, dedup=_dedup(), audit=audit)
        assert len(seen) == 1
        passed, returned = seen[0]
        assert passed is bars
        assert returned is bars


# ────── DEC-SWEEP-STREAMING-RANK-001 (part 1) — lazy run_id on the digest path ──────


class TestLazyRunIdDigestPath:
    """The digest path computes run-identities LAZILY — only where consumed.

    PR-2 of the engine-scale series (DEC-SWEEP-STREAMING-RANK-001): on the
    digest path the dedup probes are bypassed (sentinel-authoritative redo) and
    ``mark_many`` is skipped, so a survivor's run_id has exactly TWO consumers
    — the :class:`SweepComboError` fingerprint and the finalist rows — both
    recomputed on demand from the pure :func:`mask_run_id`. These tests pin:

    * the CALL COUNT — exactly ``len(finalists)`` BLAKE3 hashes on the digest
      happy path (never ``len(enumerated)``), while the legacy no-digest arm
      keeps its eager one-hash-per-enumerated-mask semantics (the dedup probe
      genuinely needs the identity before the trade sim);
    * BYTE IDENTITY — the digest-path store is byte-identical to the legacy
      (eager, untouched-semantics) arm's store over the same inputs;
    * the ERROR FINGERPRINT — full-string equality of the lazily-recomputed
      ``SweepComboError`` message against the eager-era format;
    * the structural tallies — ``skipped_dedup == 0`` and sentinel
      ``n_marked == 0`` even against a fully pre-marked dedup.
    """

    _DIGEST = "ab12cd34ef56ab12"

    @staticmethod
    def _count_run_id_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
        """Patch ``sweep_run.mask_run_id`` with a delegating call counter."""
        calls = {"n": 0}

        def _spy(**kwargs: Any) -> str:
            calls["n"] += 1
            return mask_run_id(**kwargs)

        monkeypatch.setattr("brutex.engine.sweep_run.mask_run_id", _spy)
        return calls

    @staticmethod
    def _expected_run_ids(cfg: SweepRunConfig) -> dict[int, str]:
        """Independently recompute the run-identity of every k=1 mask of the cfg."""
        params = canonical_trade_params(
            sl_paisa=cfg.sl_paisa,
            tp_paisa=cfg.tp_paisa,
            prior_n=cfg.prior_n,
            proximity=cfg.proximity,
            start_equity_paisa=cfg.start_equity_paisa,
            fib_anchor=cfg.fib_anchor,
        )
        return {1 << b: mask_run_id(mask=1 << b, params=params, **_CTX) for b in cfg.condition_bits}

    def test_digest_path_hashes_exactly_the_finalists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The counter gate: len(finalists) BLAKE3 calls, NOT len(enumerated).
        calls = self._count_run_id_calls(monkeypatch)
        summary = _run_sweep(
            tmp_path, "d", _trending_bars(), _make_config(top_k=2), digest=self._DIGEST
        )
        assert summary.enumerated == 5
        assert summary.persisted == 2  # top_k truncation → 2 finalists
        assert calls["n"] == summary.persisted
        assert calls["n"] < summary.enumerated - summary.conflict_pruned

    def test_legacy_path_keeps_eager_hashing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The legacy no-digest arm still hashes ONCE per non-conflict enumerated
        # mask (the dedup probe keys on the identity BEFORE the trade sim) and
        # the finalists reuse those probe-time values — no recompute, so the
        # count stays exactly 5 even though 2 finalists persist.
        calls = self._count_run_id_calls(monkeypatch)
        summary = _run_sweep(tmp_path, "l", _trending_bars(), _make_config(top_k=2))
        assert summary.persisted == 2
        assert calls["n"] == summary.enumerated - summary.conflict_pruned == 5

    def test_digest_store_bytes_equal_the_eager_legacy_arm(self, tmp_path: Path) -> None:
        # Before/after equality via the untouched arm: the legacy path kept the
        # eager per-mask semantics byte-verbatim, so lazy(digest) == eager(legacy)
        # over the same inputs IS the before/after store-bytes proof.
        bars = _trending_bars()
        lazy = _run_sweep(tmp_path, "lazy", bars, _make_config(), digest=self._DIGEST)
        eager = _run_sweep(tmp_path, "eager", bars, _make_config())
        assert lazy.store_content_hash == eager.store_content_hash
        assert (tmp_path / "lazy.parquet").read_bytes() == (tmp_path / "eager.parquet").read_bytes()

    def test_finalist_run_ids_match_independent_recompute(self, tmp_path: Path) -> None:
        pl = pytest.importorskip("polars")
        cfg = _make_config()
        summary = _run_sweep(tmp_path, "d", _trending_bars(), cfg, digest=self._DIGEST)
        stored = pl.read_parquet(tmp_path / "d.parquet")["run_id"].to_list()
        expected = self._expected_run_ids(cfg)
        assert len(stored) == summary.persisted > 0
        assert set(stored) <= set(expected.values())
        assert len(set(stored)) == len(stored)  # one distinct identity per finalist

    def test_digest_error_message_equals_the_eager_era_format(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # FULL-string equality of the fail-loud fingerprint: the lazily
        # recomputed run_id lands in the exact pre-change message bytes.
        from brutex.engine.sweep_driver import metrics_from_trades as real_tail

        cfg = _make_config()
        calls = {"n": 0}

        def _tail(*args: object, **kwargs: object) -> object:
            calls["n"] += 1
            if calls["n"] == 3:  # the 3rd kept mask = 0b100
                raise ValueError("tail boom")
            return real_tail(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("brutex.engine.sweep_run.metrics_from_trades", _tail)
        store = ResultsStore(path=tmp_path / "r.parquet")
        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(SweepComboError) as excinfo,
        ):
            run_persisted_sweep(
                _trending_bars(),
                cfg,
                store=store,
                dedup=_dedup(),
                audit=audit,
                trial_stats_digest=self._DIGEST,
            )
        rid = self._expected_run_ids(cfg)[0b100]
        assert str(excinfo.value) == (
            f"combo 0x{0b100:032x} (run_id={rid}) in NIFTY/1m/bull k=1: tail boom"
        )

    def test_digest_tallies_pinned_against_a_premarked_dedup(self, tmp_path: Path) -> None:
        # Structural pins (V3): probes bypassed → skipped_dedup == 0 even when
        # EVERY identity is pre-marked; mark_many skipped → sentinel n_marked == 0.
        from brutex.engine.tuple_complete import completion_sentinel_path, read_completion_sentinel

        cfg = _make_config()
        dedup = _dedup()
        dedup.mark_many(self._expected_run_ids(cfg).values())
        store = ResultsStore(path=tmp_path / "r.parquet")
        with AuditWriter.open(tmp_path / "a.jsonl") as audit:
            summary = run_persisted_sweep(
                _trending_bars(),
                cfg,
                store=store,
                dedup=dedup,
                audit=audit,
                trial_stats_digest=self._DIGEST,
            )
        assert summary.skipped_dedup == 0
        assert summary.persisted > 0
        assert read_completion_sentinel(completion_sentinel_path(store.path)).n_marked == 0


class TestLazyRunIdHardening:
    """DEC-SWEEP-STREAMING-RANK-001 (part 1) — hardening pins ON TOP of #1184.

    Incremental teeth the shipped :class:`TestLazyRunIdDigestPath` suite
    (PR #1116) does not carry:

    * the DIGEST arm's conflict-prune tallies (the shipped prune pin runs the
      legacy arm only);
    * FULL-STRING ``SweepComboError`` equality for the CHUNK-reattribution
      fingerprint on BOTH arms (the shipped string pin covers only the
      per-mask metrics tail, digest arm only) — the digest arm recomputes the
      culprit's run-id lazily inside ``_reattribute_chunk_failure``;
    * the metrics-tail message pinned on the LEGACY arm too (cross-arm
      byte-equality of the fail-loud fingerprint);
    * a tracemalloc PEAK bound proving the digest arm's survivor collection
      allocates strictly below the legacy arm's per-survivor run-id retention.
    """

    _DIGEST = "ab12cd34ef56ab12"

    def test_digest_conflict_prune_path(self, tmp_path: Path) -> None:
        # Digest twin of test_conflict_prune_path: the prune branch runs BEFORE
        # the bypass_dedup split, so pin its tallies on the digest arm too.
        bars = _trending_bars()
        cfg = _make_config(condition_bits=[BIT_BAR_BULLISH, BIT_BAR_BEARISH, 0], k=2)
        summary = _run_sweep(tmp_path, "d", bars, cfg, digest=self._DIGEST)
        assert summary.enumerated == 3  # C(3,2)
        assert summary.conflict_pruned == 1
        assert summary.skipped_dedup == 0  # probes bypassed by construction

    def test_tail_error_message_string_equal_on_both_arms(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The metrics-tail fingerprint: legacy-arm literal pin + cross-arm equality.

        The SAME metrics-tail failure forced on both arms must produce the
        byte-EQUAL message — the digest arm's lazily recomputed run-id lands in
        the exact bytes of the legacy arm's eager probe-time one — each pinned
        against the independently recomputed expected literal.
        """
        from brutex.engine.sweep_driver import metrics_from_trades as real_tail

        bars = _trending_bars()
        cfg = _make_config()  # kept order: masks 1, 2, 4, 8, 16
        boom_mask = 0b100  # the 3rd kept mask

        def _make_tail() -> Callable[..., object]:
            calls = {"n": 0}

            def _tail(*args: object, **kwargs: object) -> object:
                calls["n"] += 1
                if calls["n"] == 3:
                    raise ValueError("tail boom")
                return real_tail(*args, **kwargs)  # type: ignore[arg-type]

            return _tail

        messages: dict[str, str] = {}
        for arm, digest in (("legacy", None), ("digest", self._DIGEST)):
            monkeypatch.setattr("brutex.engine.sweep_run.metrics_from_trades", _make_tail())
            store = ResultsStore(path=tmp_path / f"{arm}.parquet")
            with (
                AuditWriter.open(tmp_path / f"{arm}.jsonl") as audit,
                pytest.raises(SweepComboError) as excinfo,
            ):
                run_persisted_sweep(
                    bars,
                    cfg,
                    store=store,
                    dedup=_dedup(),
                    audit=audit,
                    trial_stats_digest=digest,
                )
            assert isinstance(excinfo.value.__cause__, ValueError)
            assert not store.path.exists()  # nothing flushed — the tuple redoes
            messages[arm] = str(excinfo.value)
        expected_rid = TestLazyRunIdDigestPath._expected_run_ids(cfg)[boom_mask]  # pyright: ignore[reportPrivateUsage] — same-module sibling suite (COMMON, no copy)
        expected = (
            f"combo 0x{boom_mask:032x} (run_id={expected_rid}) in NIFTY/1m/bull k=1: tail boom"
        )
        assert messages["legacy"] == expected
        assert messages["digest"] == expected

    def test_chunk_culprit_error_message_string_equal_on_both_arms(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pinned STRING equality gate for the chunk-reattribution fingerprint.

        A batch failure re-attributed to the same culprit mask on both arms
        must name it with the byte-EQUAL ``_combo_error`` string (the digest
        arm resolves the run-id via the lazy ``run_id_of`` closure inside
        ``_reattribute_chunk_failure``; the shipped suite never string-pinned
        this path).
        """
        from brutex.engine.sweep_driver import metrics_for_mask as real_metrics

        bars = _trending_bars()
        cfg = _make_config()
        boom_mask = 1 << 2
        monkeypatch.setattr(
            "brutex.engine.sweep_run.ResidentTradeSim",
            _chunk_raising_sim(lambda: RuntimeError("batch boom")),
        )

        def _raising(*args: object, **kwargs: object) -> object:
            if args[2] == boom_mask:
                raise ValueError("culprit mask")
            return real_metrics(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("brutex.engine.sweep_run.metrics_for_mask", _raising)
        messages: dict[str, str] = {}
        for arm, digest in (("legacy", None), ("digest", self._DIGEST)):
            store = ResultsStore(path=tmp_path / f"{arm}.parquet")
            with (
                AuditWriter.open(tmp_path / f"{arm}.jsonl") as audit,
                pytest.raises(SweepComboError) as excinfo,
            ):
                run_persisted_sweep(
                    bars,
                    cfg,
                    store=store,
                    dedup=_dedup(),
                    audit=audit,
                    trial_stats_digest=digest,
                )
            assert isinstance(excinfo.value.__cause__, ValueError)
            messages[arm] = str(excinfo.value)
        expected_rid = TestLazyRunIdDigestPath._expected_run_ids(cfg)[boom_mask]  # pyright: ignore[reportPrivateUsage] — same-module sibling suite (COMMON, no copy)
        expected = (
            f"combo 0x{boom_mask:032x} (run_id={expected_rid}) in NIFTY/1m/bull k=1: culprit mask"
        )
        assert messages["legacy"] == expected
        assert messages["digest"] == expected

    def test_digest_arm_peak_allocations_below_legacy(self, tmp_path: Path) -> None:
        """Memory regression bound (honestly framed): tracemalloc peaks, digest < legacy.

        2,024 enumerated masks (C(24,3)) with Stage-1B dropping everything
        (``min_hits`` huge) isolate the survivor-collection cost: no trade sim,
        no ranking, no finalists. The legacy arm materialises one 64-hex
        run-id string per survivor (the parallel ``survivor_run_ids`` list +
        the eager ``run_id_of`` dict, plus its by-design probe/mark work); the
        digest arm holds bare ints only, so its traced allocation peak must
        sit at least 48 bytes/survivor BELOW the legacy arm's (a deliberately
        conservative floor — the id string alone is ~113 bytes). HONEST
        envelope: this is a tracemalloc (Python-allocator) peak bound over the
        same fixture, NOT an RSS/absolute-memory claim; the structural
        laziness pin (``TestLazyRunIdDigestPath.
        test_digest_path_hashes_exactly_the_finalists`` — exactly
        ``len(finalists)`` BLAKE3 calls) is the primary net. A warm-up pass
        per arm absorbs one-time cache/interning allocations.
        """
        import gc
        import tracemalloc

        bars = _trending_bars(n_days=1, per_day=8)
        cfg = _make_config(condition_bits=list(range(24)), k=3, min_hits=10_000_000)

        def _measure(tag: str, digest: str | None) -> tuple[SweepRunSummary, int]:
            store = ResultsStore(path=tmp_path / f"{tag}.parquet")
            dedup = _dedup()
            with AuditWriter.open(tmp_path / f"{tag}.jsonl") as audit:
                gc.collect()
                tracemalloc.start()
                try:
                    summary = run_persisted_sweep(
                        bars,
                        cfg,
                        store=store,
                        dedup=dedup,
                        audit=audit,
                        trial_stats_digest=digest,
                    )
                    peak = tracemalloc.get_traced_memory()[1]
                finally:
                    tracemalloc.stop()
            return summary, peak

        _measure("warm_legacy", None)  # warm-ups: one-time caches / interning
        _measure("warm_digest", self._DIGEST)
        legacy, legacy_peak = _measure("legacy", None)
        digest, digest_peak = _measure("digest", self._DIGEST)
        assert legacy.evaluated == digest.evaluated > 1_000  # identical survivor mass
        assert digest.persisted == legacy.persisted == 0  # Stage-1B dropped everything
        assert digest_peak < legacy_peak
        assert legacy_peak - digest_peak > digest.evaluated * 48


# ────── DEC-SWEEP-ENUM-RUST-001 — the ranged Rust enum+prune+Stage-1B seam ──────
#
# The flag-gated `enum_prefilter_via_kernel` probe inside `run_persisted_sweep`
# engages on the DIGEST path only (a legacy no-digest tuple is refused with the
# `legacy_dedup_path` warn — the design-of-record engagement-condition order);
# the Apriori arm never probes (apriori WINS — the frozen contract), and every
# fallback (`None`) runs the verbatim pre-existing loop. The kernel FUSES the
# Stage-1B min-hits count, so the seam's counter BUCKETING (enumerated /
# conflict_pruned / evaluated / minhits_dropped — the residual-(ix) sidecar
# law) is pinned here across both min-hits arms. Byte-identity end-to-end is
# the golden's job (tests/golden/test_engine_rust_enum_parity.py).


class TestRustEnumSeam:
    """Dispatch + parity pins for the DEC-SWEEP-ENUM-RUST-001 seam."""

    _DIGEST = "abad1deaabad1dea"

    @pytest.fixture(autouse=True)
    def _fresh_rust_enum_state(self, monkeypatch: pytest.MonkeyPatch) -> Iterable[None]:
        """Fresh warn-once memo / burn-in budget / flag per test."""
        rust_enum.reset_observability_memo_for_tests()
        rust_enum.reset_enum_self_check_for_tests()
        monkeypatch.delenv(rust_enum.SWEEP_RUST_ENUM_ENV, raising=False)
        monkeypatch.delenv("BRUTEX_SWEEP_APRIORI", raising=False)
        yield
        rust_enum.reset_observability_memo_for_tests()
        rust_enum.reset_enum_self_check_for_tests()

    def test_on_digest_tuple_equals_off_byte_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ON+digest == OFF+digest: every summary counter, the store hash, the
        parquet bytes AND the trial-stats sidecar bytes — with the anti-vacuous
        proof that the kernel genuinely served the ON run."""
        bars = _trending_bars()
        # Bits {0,1,2,3,4} at k=2 carry the REAL (0,1) + (2,3) conflict pairs,
        # so the prune genuinely fires (anti-vacuous conflict_pruned > 0).
        cfg = _make_config(condition_bits=[0, 1, 2, 3, 4], k=2, min_hits=0)
        off = _run_sweep(tmp_path, "off", bars, cfg, digest=self._DIGEST)
        monkeypatch.setenv(rust_enum.SWEEP_RUST_ENUM_ENV, "1")
        with capture_logs() as logs:
            on = _run_sweep(tmp_path, "on", bars, cfg, digest=self._DIGEST)
        assert [e for e in logs if e["event"] == "sweep_rust_enum.engaged"], (
            "the ON run never engaged the kernel — a vacuous parity"
        )
        assert on.enumerated == off.enumerated == 10  # C(5, 2)
        assert on.conflict_pruned == off.conflict_pruned == 2
        assert on.skipped_dedup == off.skipped_dedup == 0
        assert on.evaluated == off.evaluated == 8
        assert on.persisted == off.persisted
        assert on.ranked == off.ranked
        assert on.backtested == off.backtested
        assert on.store_content_hash == off.store_content_hash
        assert (tmp_path / "on.parquet").read_bytes() == (tmp_path / "off.parquet").read_bytes()
        from brutex.engine.trial_stats import trial_stats_sidecar_path

        on_sidecar = trial_stats_sidecar_path(tmp_path / "on.parquet")
        off_sidecar = trial_stats_sidecar_path(tmp_path / "off.parquet")
        assert on_sidecar.read_bytes() == off_sidecar.read_bytes()

    def test_legacy_no_digest_refuses_with_the_named_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The legacy dedup arm stays pure Python even flag-ON: the wrapper is
        probed but REFUSES with one `legacy_dedup_path` warn and zero engaged
        events (the design's engagement-condition teeth), bytes unchanged."""
        bars = _trending_bars()
        cfg = _make_config()
        off = _run_sweep(tmp_path, "off", bars, cfg, digest=None)
        monkeypatch.setenv(rust_enum.SWEEP_RUST_ENUM_ENV, "1")
        with capture_logs() as logs:
            on = _run_sweep(tmp_path, "on", bars, cfg, digest=None)
        warns = [e for e in logs if e["event"] == "sweep_rust_enum.fallback"]
        assert [w["reason"] for w in warns] == ["legacy_dedup_path"]
        assert not [e for e in logs if e["event"] == "sweep_rust_enum.engaged"]
        assert on.enumerated == off.enumerated
        assert on.conflict_pruned == off.conflict_pruned
        assert on.store_content_hash == off.store_content_hash
        assert (tmp_path / "on.parquet").read_bytes() == (tmp_path / "off.parquet").read_bytes()

    def test_apriori_wins_over_the_enum_kernel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FM-10: with BOTH flags on the Apriori generator is the mask source
        and the enum-kernel probe is never reached (its probe lives only in
        the else arm)."""
        bars = _trending_bars()
        cfg = _make_config(condition_bits=[0, 1, 2, 3, 4], k=2)
        monkeypatch.setenv("BRUTEX_SWEEP_APRIORI", "1")
        monkeypatch.setenv(rust_enum.SWEEP_RUST_ENUM_ENV, "1")

        def _must_not_probe(*args: object, **kwargs: object) -> NoReturn:
            raise AssertionError("the Apriori arm must never probe the enum kernel")

        monkeypatch.setattr("brutex.engine.sweep_run.enum_prefilter_via_kernel", _must_not_probe)
        summary = _run_sweep(tmp_path, "ap", bars, cfg, digest=self._DIGEST)
        # The Apriori arm ran (conflict-free candidates by construction).
        assert summary.conflict_pruned == 0

    def test_kernel_fallback_none_runs_the_python_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A kernel `None` (any ladder fallback) leaves the digest tuple on
        the verbatim Python loop — probed exactly once, bytes unchanged."""
        bars = _trending_bars()
        cfg = _make_config(condition_bits=[0, 1, 2, 3, 4], k=2, min_hits=0)
        off = _run_sweep(tmp_path, "off", bars, cfg, digest=self._DIGEST)
        monkeypatch.setenv(rust_enum.SWEEP_RUST_ENUM_ENV, "1")
        calls: list[tuple[tuple[int, ...], int, int, bool]] = []

        def _always_none(
            bits: Sequence[int],
            k: int,
            *,
            min_hits: int,
            bar_bits: Sequence[int],
            count_hits: object,
            digest_path: bool,
        ) -> None:
            calls.append((tuple(bits), k, min_hits, digest_path))

        monkeypatch.setattr("brutex.engine.sweep_run.enum_prefilter_via_kernel", _always_none)
        on = _run_sweep(tmp_path, "fb", bars, cfg, digest=self._DIGEST)
        assert calls == [((0, 1, 2, 3, 4), 2, 0, True)]
        assert on.enumerated == off.enumerated
        assert on.conflict_pruned == off.conflict_pruned
        assert on.store_content_hash == off.store_content_hash
        assert (tmp_path / "fb.parquet").read_bytes() == (tmp_path / "off.parquet").read_bytes()

    def test_u128_bits_through_the_kernel_seam(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bits ≥ 64 flow through the kernel-served seam byte-identically —
        the u128 twin of TestU128EnvelopeThroughSweep for the enum segment."""
        bars = _trending_bars()
        cfg = _make_config(condition_bits=[0, 64, 65], k=1, min_hits=0)
        off = _run_sweep(tmp_path, "off", bars, cfg, digest=self._DIGEST)
        monkeypatch.setenv(rust_enum.SWEEP_RUST_ENUM_ENV, "1")
        with capture_logs() as logs:
            on = _run_sweep(tmp_path, "on", bars, cfg, digest=self._DIGEST)
        assert [e for e in logs if e["event"] == "sweep_rust_enum.engaged"]
        assert on.enumerated == off.enumerated == 3
        assert on.evaluated == off.evaluated == 3
        assert on.store_content_hash == off.store_content_hash
        assert (tmp_path / "on.parquet").read_bytes() == (tmp_path / "off.parquet").read_bytes()

    @pytest.mark.parametrize("min_hits", [5, 10**6])
    def test_min_hits_fused_counters_bucket_identically(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, min_hits: int
    ) -> None:
        """min_hits > 0, ON+digest == OFF: the FUSED kernel count buckets into
        evaluated / minhits_dropped / the trial-stats sidecar EXACTLY as the
        Python batch filter (the design residual-(ix) tally-bucketing law) —
        and past burn-in the Python Stage-1B batch count NEVER runs on the
        kernel arm (the fusing is real, not a recompute)."""
        bars = _trending_bars()
        cfg = _make_config(condition_bits=[0, 1, 2, 3, 4], k=2, min_hits=min_hits)
        off = _run_sweep(tmp_path, "off", bars, cfg, digest=self._DIGEST)
        monkeypatch.setenv(rust_enum.SWEEP_RUST_ENUM_ENV, "1")
        rust_enum.reset_enum_self_check_for_tests(masks_remaining=0)  # past burn-in
        batch_calls: list[int] = []
        from brutex.engine.combo_mask import count_supermasks as _real_count

        def _spy(bar_bits: Sequence[int], masks: Sequence[int]) -> list[int]:
            batch_calls.append(len(masks))
            return _real_count(bar_bits, masks)

        monkeypatch.setattr("brutex.engine.sweep_run.count_supermasks", _spy)
        with capture_logs() as logs:
            on = _run_sweep(tmp_path, "on", bars, cfg, digest=self._DIGEST)
        assert [e for e in logs if e["event"] == "sweep_rust_enum.engaged"]
        assert batch_calls == []  # fused in-kernel — zero Python batch counts
        assert on.enumerated == off.enumerated == 10
        assert on.conflict_pruned == off.conflict_pruned == 2
        assert on.evaluated == off.evaluated == 8
        assert on.minhits_dropped == off.minhits_dropped
        if min_hits == 10**6:
            # Anti-vacuous: an impossible floor drops EVERY survivor.
            assert on.minhits_dropped == 8
        assert on.backtested == off.backtested
        assert on.store_content_hash == off.store_content_hash
        assert (tmp_path / "on.parquet").read_bytes() == (tmp_path / "off.parquet").read_bytes()
        from brutex.engine.trial_stats import trial_stats_sidecar_path

        assert (
            trial_stats_sidecar_path(tmp_path / "on.parquet").read_bytes()
            == trial_stats_sidecar_path(tmp_path / "off.parquet").read_bytes()
        )
