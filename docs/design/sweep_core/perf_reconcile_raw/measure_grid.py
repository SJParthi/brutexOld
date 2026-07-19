"""RECONCILIATION grid: cost(mask) = c0 + walk_rate*n_bars + trade_cost*n_trades.

Times the EXACT production op — brutex.engine.rust_trade_sim.ResidentTradeSim
.simulate_masks_with_exit_indices (Rust ResidentSweepSim kernel + per-trade
ClosedTrade/exit-index materialisation) — over a controlled (n_bars x trade
density) grid, plus the raw-kernel and count-only tiers for attribution.

Fixture: synthetic 1-minute-shaped bars (374 bars/session, 09:16-15:29 IST)
over real consecutive weekdays from 2020-01-02, mean-reverting random walk,
2dp Decimal prices — the audit session's FIX-L shape (604,485 bars) is the
largest prefix. Audit artifacts (their scratchpad) are GONE from this
container, so this is an equivalent regenerated fixture, identity-hashed.

Trade-density control: bar_bits are SYNTHETIC (not Stage-1A): bit b fires
every K bars at a phase offset, so a single-bit mask yields ~n_bars/K trades
(tight SL/TP => holding ~a few bars << K). The kernel cannot tell how
bar_bits were produced; this isolates cost as a function of (n_bars, n_trades).

READ-ONLY on the repo. Sandbox x86 container — NOT the M4 Pro; absolute ns
are container-local, the RECONCILIATION (both disputed numbers were measured
in this same container class) is the signal.
"""

from __future__ import annotations

import hashlib
import json
import logging
import statistics
import sys
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import structlog

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
)

sys.path.insert(0, "/home/user/bruteX")

HERE = Path(__file__).resolve().parent
IST = timezone(timedelta(hours=5, minutes=30))

from brutex.data.models import CandleBar  # noqa: E402
from brutex.engine.rust_trade_sim import ResidentTradeSim  # noqa: E402
from brutex.engine.sweep_run import _trade_sim_chunk_masks  # noqa: E402
from brutex.strategies.types import Side  # noqa: E402

# --- fixture -----------------------------------------------------------------

BARS_PER_SESSION = 374  # 09:16..15:29 inclusive
FIXL_BARS = 604_485  # audit FIX-L row count
N_SESSIONS = FIXL_BARS // BARS_PER_SESSION + 1  # 1617 -> 604,758, truncated


def weekdays_from(start: date, n: int) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def synth_bars(n_bars: int, seed: int = 20260709) -> list[CandleBar]:
    """Mean-reverting random-walk 1-minute bars on consecutive weekdays."""
    import random

    rng = random.Random(seed)
    days = weekdays_from(date(2020, 1, 2), N_SESSIONS)
    bars: list[CandleBar] = []
    price = 22000.0
    for day in days:
        t = datetime(day.year, day.month, day.day, 9, 16, tzinfo=IST)
        end = datetime(day.year, day.month, day.day, 15, 29, tzinfo=IST)
        while t <= end:
            # mean-revert toward 22000 so the price never drifts negative /
            # beyond the kernel price envelope over 604k bars
            drift = rng.gauss(0, 4.0) + 0.0004 * (22000.0 - price)
            o = price
            c = price + drift
            hi = max(o, c) + abs(rng.gauss(0, 1.5))
            lo = min(o, c) - abs(rng.gauss(0, 1.5))
            bars.append(
                CandleBar(
                    timestamp=t.astimezone(timezone.utc),
                    open=Decimal(f"{o:.2f}"),
                    high=Decimal(f"{hi:.2f}"),
                    low=Decimal(f"{lo:.2f}"),
                    close=Decimal(f"{c:.2f}"),
                    volume=int(abs(rng.gauss(1000, 300))) + 1,
                    open_interest=None,
                )
            )
            price = c
            t += timedelta(minutes=1)
            if len(bars) >= n_bars:
                return bars
    return bars


def fixture_hash(bars: list[CandleBar]) -> str:
    h = hashlib.sha256()
    for b in bars:
        h.update(
            f"{b.timestamp.isoformat()}|{b.open}|{b.high}|{b.low}|{b.close}|{b.volume}\n".encode()
        )
    return h.hexdigest()


# --- density-controlled bar_bits ----------------------------------------------

DENSITY_TARGETS = [5, 45, 250, 700, 1400]  # target closed trades per mask
MASKS_PER_CELL = 10  # phase-offset single-bit masks per density


def build_bar_bits(n_bars: int) -> tuple[list[int], dict[int, list[int]]]:
    """bar_bits with 10 phase-offset bits per density target.

    Returns (bar_bits, {target: [mask, ...]}).
    """
    bits = [0] * n_bars
    masks_by_target: dict[int, list[int]] = {}
    bit = 0
    for target in DENSITY_TARGETS:
        k = max(12, n_bars // target)
        cell_masks = []
        for j in range(MASKS_PER_CELL):
            phase = (j * 97 + 11) % k  # spread phases, deterministic
            for i in range(phase, n_bars, k):
                bits[i] |= 1 << bit
            cell_masks.append(1 << bit)
            bit += 1
        masks_by_target[target] = cell_masks
    return bits, masks_by_target


# --- timing -------------------------------------------------------------------

SL_PAISA = 8_00  # Rs 8 (~2 sigma/bar) -> holding a few bars, << signal spacing
TP_PAISA = 12_00
REPS = 7


def time_cell(sim: ResidentTradeSim, masks: list[int], chunk: int,
              sl: int = SL_PAISA, tp: int = TP_PAISA) -> dict:
    rust = sim._rust  # noqa: SLF001 — profiling the raw kernel tiers
    assert rust is not None

    def run_count() -> int:
        n = 0
        for i in range(0, len(masks), chunk):
            n += sum(
                rust.count_trades(
                    list(masks[i : i + chunk]), True, sl, tp, False, False, False
                )
            )
        return n

    def run_raw() -> int:
        n = 0
        for i in range(0, len(masks), chunk):
            raw = rust.simulate_masks(
                list(masks[i : i + chunk]), True, sl, tp, False, False, False
            )
            n += sum(len(r) for r in raw)
        return n

    def run_wrapper() -> int:
        n = 0
        for i in range(0, len(masks), chunk):
            pairs = sim.simulate_masks_with_exit_indices(
                masks[i : i + chunk],
                direction=Side.BUY,
                stop_loss_paisa=sl,
                take_profit_paisa=tp,
            )
            n += sum(len(t) for t, _ in pairs)
        return n

    out: dict = {}
    # verify density once (wrapper output) + warm caches
    n_trades_total = run_wrapper()
    out["trades_per_mask"] = n_trades_total / len(masks)
    for name, fn in (("count", run_count), ("raw", run_raw), ("wrapper", run_wrapper)):
        per: list[float] = []
        fn()  # warm
        for _ in range(REPS):
            t0 = time.perf_counter()
            fn()
            per.append((time.perf_counter() - t0) / len(masks))
        out[f"{name}_us_best"] = 1e6 * min(per)
        out[f"{name}_us_median"] = 1e6 * statistics.median(per)
        out[f"{name}_rel_spread"] = (max(per) - min(per)) / min(per)
    out["n_masks"] = len(masks)
    out["reps"] = REPS
    return out


def main() -> None:
    n_bars_grid = [48_620, 93_874, 299_948, 604_485]
    print("generating fixture (604,485 bars)...", flush=True)
    t0 = time.perf_counter()
    all_bars = synth_bars(FIXL_BARS)
    gen_s = time.perf_counter() - t0
    print(f"generated {len(all_bars)} bars in {gen_s:.1f}s", flush=True)
    t0 = time.perf_counter()
    fhash = fixture_hash(all_bars)
    print(f"fixture sha256={fhash} ({time.perf_counter() - t0:.1f}s)", flush=True)

    results: dict = {
        "host": "sandbox x86 container, Intel Xeon @2.80GHz, 4 vCPU (NOT the M4 Pro)",
        "python": sys.version.split()[0],
        "wheels": {
            "fno_math": "0.3.0 (cp312-abi3-manylinux_2_34_x86_64, sha256 6397ca9d...363d)",
            "trading_core": "0.3.1 (cp312-abi3, sha256 abbd38b6...10ed)",
        },
        "fixture": {
            "kind": "synthetic 1-minute-shaped, 374 bars/session, weekdays from 2020-01-02",
            "n_bars_max": len(all_bars),
            "sha256": fhash,
            "note": "audit FIX-L artifacts absent from container; equivalent regenerated shape",
        },
        "sl_paisa": SL_PAISA,
        "tp_paisa": TP_PAISA,
        "density_targets": DENSITY_TARGETS,
        "cells": [],
    }

    for n_bars in n_bars_grid:
        bars = all_bars[:n_bars]
        bar_bits, masks_by_target = build_bar_bits(n_bars)
        t0 = time.perf_counter()
        sim = ResidentTradeSim(bars, bar_bits, tf_minutes=1)
        construct_s = time.perf_counter() - t0
        assert sim.rust_active, "kernel must be active"
        chunk = _trade_sim_chunk_masks(n_bars)
        print(f"\nn_bars={n_bars} construct={construct_s:.1f}s chunk={chunk}", flush=True)
        for target in DENSITY_TARGETS:
            cell = time_cell(sim, masks_by_target[target], chunk)
            cell.update(n_bars=n_bars, target=target, chunk=chunk, construct_s=round(construct_s, 2))
            results["cells"].append(cell)
            print(
                f"  target={target:5d} trades/mask={cell['trades_per_mask']:8.1f} "
                f"count={cell['count_us_best']:9.1f}us raw={cell['raw_us_best']:9.1f}us "
                f"wrapper={cell['wrapper_us_best']:9.1f}us "
                f"(spread {cell['wrapper_rel_spread']:.2f})",
                flush=True,
            )
        del sim
        # control cell: production-like SL/TP (Rs80/Rs120 - long holdings) at 48.6k
        if n_bars == 48_620:
            sim2 = ResidentTradeSim(bars, bar_bits, tf_minutes=1)
            ctl = time_cell(sim2, masks_by_target[45], _trade_sim_chunk_masks(n_bars),
                            sl=80_00, tp=120_00)
            ctl.update(n_bars=n_bars, target="control_sl80_tp120", chunk=chunk)
            results["cells"].append(ctl)
            print(
                f"  CONTROL sl80/tp120 trades/mask={ctl['trades_per_mask']:.1f} "
                f"count={ctl['count_us_best']:.1f}us raw={ctl['raw_us_best']:.1f}us "
                f"wrapper={ctl['wrapper_us_best']:.1f}us",
                flush=True,
            )
            del sim2

    (HERE / "grid_raw.json").write_text(json.dumps(results, indent=2))
    print("\nWROTE grid_raw.json", flush=True)


if __name__ == "__main__":
    main()
