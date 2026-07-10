"""S2 ship gate — DESIGN_FINAL_RUST_SWEEP_CORE.md §6 acceptance criterion (1).

On ONE frozen dense trade-bearing fixture at 604,485 bars, the post-port
INCLUSIVE steady-state mask-loop time (the ``_iter_mask_metrics_core``
consumption, summed over the fixture's masks) must be ≤ ~1.15× of the
SAME-fixture kernel-time SUM (raw ``fold_block`` + decode over the identical
chunks), both sides measured in the same process. SCOPE (binding): the gate
times the KERNEL+SEAM path ONLY — burn-in windows CLOSED
(``reset_self_check_for_tests``), the finalist belt + population spot-check
EXCLUDED (no candidates retained, no belt call) — it proves the PORT strands
no measurable Python residue on the mask loop; it is NOT a net-win gate.
Pinned fixture shape: 604,485 bars (1,680 sessions × 360 minutes — the heavy
1m-class shape), 8 dense masks (~1.3-1.4k trades/mask), ``min_hits`` not in
play (generator-level), ``top_k`` inert (no rank in the timed region).

The FORCED-DEMOTION TWIN: with the width latched, the same generator serves
the identical fixture via the spine — outputs bit-equal to the kernel pass
(the correctness twin of the timing gate; §6 mandated test (g)).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from brutex.data.models import CandleBar
from brutex.engine import sweep_core, sweep_run
from brutex.engine.rust_trade_sim import ResidentTradeSim
from brutex.engine.sweep_core import (
    DEFAULT_FOLD_WORK_CELLS,
    DEFAULT_ORACLE_NS_PER_BAR,
    CorePlan,
    decode_fold_block,
    metrics_equal,
    reset_self_check_for_tests,
)
from brutex.engine.sweep_run import SweepRunConfig
from brutex.strategies.types import Side

_IST = timezone(timedelta(hours=5, minutes=30))

#: The frozen S2 fixture shape (§6): 1,680 sessions × 360 bars, truncated
#: to the design's literal 604,485-bar count (calendar-coverage-bounded:
#: sessions start 2020-01-06 and stay inside the published 2020-2026 span).
_S2_BARS = 604_485
_S2_MASKS = 8
_S2_RATIO_CEILING = 1.15


def _s2_bars() -> list[CandleBar]:
    out: list[CandleBar] = []
    base = datetime(2020, 1, 6, 9, 16, tzinfo=_IST)
    price_up = Decimal("100.00")
    price_dn = Decimal("100.60")
    hi = Decimal("101.10")
    lo = Decimal("99.50")
    day = -1
    for i in range(_S2_BARS):
        minute = i % 360
        if minute == 0:
            day += 1
        ts = (base + timedelta(days=day, minutes=minute)).astimezone(UTC)
        up = minute % 3 != 0
        out.append(
            CandleBar(
                timestamp=ts,
                open=price_up if up else price_dn,
                high=hi,
                low=lo,
                close=price_dn if up else price_up,
                volume=100,
                open_interest=None,
            )
        )
    return out


def _s2_bar_bits(n: int) -> list[int]:
    # 8 dense trade-bearing bits: bit b fires every 431+b bars → ~1.3-1.4k
    # entry signals per mask over 604,485 bars (the §6 density anchor).
    return [sum(1 << b for b in range(_S2_MASKS) if i % (431 + b) == 0) for i in range(n)]


def _cfg() -> SweepRunConfig:
    return SweepRunConfig(
        instrument="NIFTY",
        tf="1m",
        direction=Side.BUY,
        mode="A",
        condition_bits=list(range(_S2_MASKS)),
        k=1,
        data_snapshot_hash="s2",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
        tf_minutes=1,
        sl_paisa=80,
        tp_paisa=50,
        min_hits=0,
        top_k=8,
    )


@pytest.fixture(scope="module")
def s2_fixture() -> tuple[list[CandleBar], list[int], list[int]]:
    bars = _s2_bars()
    return bars, _s2_bar_bits(len(bars)), [1 << b for b in range(_S2_MASKS)]


def _engaged_ctx(bars: list[CandleBar], bar_bits: list[int]) -> sweep_core.CoreFoldContext:
    reset_self_check_for_tests(total_remaining=0, plain_remaining=0)  # burn-in CLOSED
    ctx = sweep_core.core_fold_context(
        bars=bars,
        bar_bits=bar_bits,
        condition_bits=list(range(_S2_MASKS)),
        tf_minutes=1,
        n_masks=_S2_MASKS,
        digest_path=True,
        top_k=8,
        plan=CorePlan(
            requested=True,
            belt=True,
            fold_cells=DEFAULT_FOLD_WORK_CELLS,
            oracle_ns_per_bar=DEFAULT_ORACLE_NS_PER_BAR,
        ),
    )
    assert ctx is not None
    return ctx


def test_s2_gate_inclusive_mask_loop_within_ratio_of_kernel_sum(
    s2_fixture: tuple[list[CandleBar], list[int], list[int]],
) -> None:
    bars, bar_bits, masks = s2_fixture
    cfg = _cfg()
    ctx = _engaged_ctx(bars, bar_bits)
    sim = ResidentTradeSim(bars, bar_bits, tf_minutes=1)

    def _inclusive_once() -> float:
        reset_self_check_for_tests(total_remaining=0, plain_remaining=0)
        t0 = time.perf_counter()
        out = list(
            sweep_run._iter_mask_metrics_core(
                ctx,
                sim,
                masks,
                bars,
                bar_bits,
                cfg,
                "bull",
                lambda m: "rid",
                retained={},
                candidates=frozenset(),
            )
        )
        elapsed = time.perf_counter() - t0
        assert len(out) == _S2_MASKS
        return elapsed

    def _kernel_once() -> float:
        chunk_size = ctx.chunk_masks
        total = 0.0
        position = 0
        while position < len(masks):
            chunk = list(masks[position : position + chunk_size])
            t0 = time.perf_counter()
            block = ctx.core.fold_block(chunk, True, 80, 50, False, False, False, 10_000_000)
            decode_fold_block(block, len(chunk))
            total += time.perf_counter() - t0
            position += len(chunk)
        return total

    # Warm both sides once, then best-of-3 (the house best-of-N-MIN idiom).
    _inclusive_once()
    _kernel_once()
    inclusive = min(_inclusive_once() for _ in range(3))
    kernel_sum = min(_kernel_once() for _ in range(3))
    ratio = inclusive / kernel_sum
    assert ratio <= _S2_RATIO_CEILING, (
        f"S2 gate: inclusive mask-loop {inclusive * 1e3:.2f} ms vs kernel sum "
        f"{kernel_sum * 1e3:.2f} ms — ratio {ratio:.3f} > {_S2_RATIO_CEILING}"
    )


def test_s2_forced_demotion_twin_bit_equal(
    s2_fixture: tuple[list[CandleBar], list[int], list[int]],
) -> None:
    bars, bar_bits, masks = s2_fixture
    cfg = _cfg()
    ctx = _engaged_ctx(bars, bar_bits)
    sim = ResidentTradeSim(bars, bar_bits, tf_minutes=1)
    kernel_served = list(
        sweep_run._iter_mask_metrics_core(
            ctx,
            sim,
            masks,
            bars,
            bar_bits,
            cfg,
            "bull",
            lambda m: "rid",
            retained={},
            candidates=frozenset(),
        )
    )
    # Latch the width: the SAME generator demotes to the spine mid-entry.
    reset_self_check_for_tests(total_remaining=0, plain_remaining=0, latched="self_check_mismatch")
    demoted = list(
        sweep_run._iter_mask_metrics_core(
            ctx,
            sim,
            masks,
            bars,
            bar_bits,
            cfg,
            "bull",
            lambda m: "rid",
            retained={},
            candidates=frozenset(),
        )
    )
    reset_self_check_for_tests()
    assert [m for m, _ in demoted] == [m for m, _ in kernel_served] == masks
    for (_, a), (_, b) in zip(demoted, kernel_served, strict=True):
        assert metrics_equal(a, b)
