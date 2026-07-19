"""RECONCILIATION grid v2 — serial vs production-parallel arms made explicit.

fno_math's batch kernels dispatch to rayon when masks*n_bars >= 1<<20
(trade_sim.rs:127), so per-mask WALL cost depends on masks-per-call. v1 mixed
the arms across n_bars. Here:

  SERIAL tier   — 1 mask per kernel call (work = n_bars < 1<<20 only for
                  n_bars < 1,048,576: true for every grid point) => the
                  single-core kernel cost. Fit target for the two-term model.
  PROD tier     — production chunking (_trade_sim_chunk_masks), batch filled
                  to 2 chunks => the rayon-parallel wall cost the sweep sees.

Tiers x {count (kernel walk only), raw (kernel + record marshal),
wrapper (production op: + ClosedTrade/exit-index materialisation, Python)}.
"""

from __future__ import annotations

import json
import logging
import statistics
import sys
import time
from pathlib import Path

import structlog

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
sys.path.insert(0, "/home/user/bruteX")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from brutex.engine.rust_trade_sim import ResidentTradeSim  # noqa: E402
from brutex.engine.sweep_run import _trade_sim_chunk_masks  # noqa: E402
from brutex.strategies.types import Side  # noqa: E402
from measure_grid import (  # noqa: E402
    DENSITY_TARGETS,
    FIXL_BARS,
    build_bar_bits,
    fixture_hash,
    synth_bars,
)

HERE = Path(__file__).resolve().parent
SL_PAISA = 8_00
TP_PAISA = 12_00
REPS = 5


def bench_calls(fn_one_call, mask_batches: list[list[int]], reps: int = REPS) -> dict:
    n_masks = sum(len(b) for b in mask_batches)
    per: list[float] = []
    fn_one_call(mask_batches[0])  # warm
    for _ in range(reps):
        t0 = time.perf_counter()
        for b in mask_batches:
            fn_one_call(b)
        per.append((time.perf_counter() - t0) / n_masks)
    return {
        "us_best": 1e6 * min(per),
        "us_median": 1e6 * statistics.median(per),
        "rel_spread": (max(per) - min(per)) / min(per),
        "n_masks": n_masks,
        "n_calls": len(mask_batches),
    }


def cell(sim: ResidentTradeSim, phase_masks: list[int], chunk: int, sl: int, tp: int) -> dict:
    rust = sim._rust
    assert rust is not None

    def k_count(b):
        rust.count_trades(list(b), True, sl, tp, False, False, False)

    def k_raw(b):
        rust.simulate_masks(list(b), True, sl, tp, False, False, False)

    def k_wrap(b):
        sim.simulate_masks_with_exit_indices(
            b, direction=Side.BUY, stop_loss_paisa=sl, take_profit_paisa=tp
        )

    out: dict = {}
    # measured density (ground truth from the wrapper output)
    pairs = sim.simulate_masks_with_exit_indices(
        phase_masks, direction=Side.BUY, stop_loss_paisa=sl, take_profit_paisa=tp
    )
    out["trades_per_mask"] = sum(len(t) for t, _ in pairs) / len(phase_masks)

    # SERIAL: 1 mask per call (work = n_bars < 1<<20 for all grid n_bars)
    serial_batches = [[m] for m in phase_masks]
    # PROD: fill 2 production chunks by tiling the phase masks
    n_prod = 2 * chunk
    tiled = [phase_masks[i % len(phase_masks)] for i in range(n_prod)]
    prod_batches = [tiled[i : i + chunk] for i in range(0, n_prod, chunk)]

    for tier, fn in (("count", k_count), ("raw", k_raw), ("wrapper", k_wrap)):
        out[f"serial_{tier}"] = bench_calls(fn, serial_batches)
        out[f"prod_{tier}"] = bench_calls(fn, prod_batches)
    return out


def main() -> None:
    n_bars_grid = [48_620, 93_874, 299_948, 604_485]
    print("generating fixture...", flush=True)
    all_bars = synth_bars(FIXL_BARS)
    fhash = fixture_hash(all_bars)
    print(f"fixture sha256={fhash}", flush=True)

    results: dict = {
        "host": "sandbox x86 container, Intel Xeon @2.80GHz, 4 vCPU (NOT the M4 Pro)",
        "python": sys.version.split()[0],
        "rayon_threads": 4,
        "parallel_work_threshold": 1 << 20,
        "wheels": {
            "fno_math": "0.3.0 cp312-abi3-manylinux_2_34_x86_64 sha256=6397ca9d89935397daaf0941ae8094e34ec014a10a9015fb9caf3cc11832363d",
            "trading_core": "0.3.1 cp312-abi3-manylinux_2_34_x86_64 sha256=abbd38b648a9e53d96d8cc84cc7c9f5e10cdab829c4d203c8d56c548c86f10ed",
        },
        "fixture": {
            "n_bars_max": len(all_bars),
            "sha256": fhash,
            "kind": "synthetic 1m-shaped, 374 bars/session, weekdays from 2020-01-02, seed 20260709",
        },
        "sl_paisa": SL_PAISA,
        "tp_paisa": TP_PAISA,
        "density_targets": DENSITY_TARGETS,
        "cells": [],
    }

    for n_bars in n_bars_grid:
        bars = all_bars[:n_bars]
        bar_bits, masks_by_target = build_bar_bits(n_bars)
        sim = ResidentTradeSim(bars, bar_bits, tf_minutes=1)
        assert sim.rust_active
        chunk = _trade_sim_chunk_masks(n_bars)
        print(f"\nn_bars={n_bars} chunk={chunk}", flush=True)
        for target in DENSITY_TARGETS:
            c = cell(sim, masks_by_target[target], chunk, SL_PAISA, TP_PAISA)
            c.update(n_bars=n_bars, target=target, chunk=chunk)
            results["cells"].append(c)
            print(
                f"  t={target:5d} tr/mask={c['trades_per_mask']:7.1f} | serial "
                f"count={c['serial_count']['us_best']:8.1f} raw={c['serial_raw']['us_best']:8.1f} "
                f"wrap={c['serial_wrapper']['us_best']:8.1f} | prod "
                f"count={c['prod_count']['us_best']:8.1f} raw={c['prod_raw']['us_best']:8.1f} "
                f"wrap={c['prod_wrapper']['us_best']:8.1f}",
                flush=True,
            )
        if n_bars == 48_620:
            ctl = cell(sim, masks_by_target[45], chunk, 80_00, 120_00)
            ctl.update(n_bars=n_bars, target="control_sl80_tp120", chunk=chunk)
            results["cells"].append(ctl)
            print(
                f"  CONTROL sl80/tp120 tr/mask={ctl['trades_per_mask']:.1f} serial "
                f"count={ctl['serial_count']['us_best']:.1f} wrap={ctl['serial_wrapper']['us_best']:.1f} "
                f"prod count={ctl['prod_count']['us_best']:.1f} wrap={ctl['prod_wrapper']['us_best']:.1f}",
                flush=True,
            )
        del sim

    (HERE / "grid2_raw.json").write_text(json.dumps(results, indent=2))
    print("\nWROTE grid2_raw.json", flush=True)


if __name__ == "__main__":
    main()
