"""Pinned (RAYON_NUM_THREADS=1 — the PRODUCTION worker config, sweep_parallel.py:558)
re-run of the grid, with the real FIX-L k=2 median density (~1,532 trades/mask)
added at every n_bars. Must be launched with RAYON_NUM_THREADS=1 in the env
(before the first kernel call initialises rayon's pool)."""

# Capture base: origin/main 665181f3 — sweep_parallel.py:558 at capture; the pin
# lives at :842 as of 95955a71; semantics unchanged. (Verifier A round-2.)
# Comment only — grid output bytes unchanged.

from __future__ import annotations

import os
import sys
from pathlib import Path

assert os.environ.get("RAYON_NUM_THREADS") == "1", "launch with RAYON_NUM_THREADS=1"

sys.path.insert(0, str(Path(__file__).resolve().parent))

import measure_grid  # noqa: E402

measure_grid.DENSITY_TARGETS = [5, 45, 250, 700, 1400, 1532]

import measure_grid2  # noqa: E402

measure_grid2.DENSITY_TARGETS = measure_grid.DENSITY_TARGETS

if __name__ == "__main__":
    measure_grid2.main()
