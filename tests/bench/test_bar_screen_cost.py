"""HONEST cost pin for the dashboard split-brain screen (DEC-DASHBOARD-READ-SCREEN-001).

The reviewer-3 measured finding: :func:`brutex.data.bar_screen.screen_frame` is
NOT free on clean data — it materialises 5 columns + runs an O(rows) per-row
classify pass BEFORE deciding same-object (measured linear: 375 rows 0.303 ms /
3,750 rows 2.546 ms / 37,500 rows 24.711 ms). This bench pins the TRUTH:

* **LINEARITY** — the clean-frame screen cost SCALES with rows (a per-row
  Python pass; asserted `>= 2x` for a 10x-larger frame, so the docstring's
  "O(rows) honest cost" claim can never silently rot into a false flat claim —
  and if a future polars-vectorized / Rust rewrite genuinely flattens it, this
  pin fails LOUDLY and forces the conscious flat re-claim);
* **same-object identity** — clean data returns the SAME frame object (the
  byte-identity ship gate), so the cost is pure screening work, never a copy.

No flat/O(1) claim is made anywhere for this pass (Golden Rule #10); the
`_o1_tiers` ceilings deliberately do NOT apply — this is a linearity pin, the
inverse teeth.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("polars")

import polars as pl
from brutex.data.bar_screen import screen_frame

#: 09:15 IST == 03:45 UTC.
_OPEN_UTC = datetime(2024, 1, 2, 3, 45, tzinfo=UTC)


def _clean_frame(rows: int) -> pl.DataFrame:
    """A clean (positive + ordered + spike-free) ascending 1m-shaped frame."""
    closes = [1000.0 + (i % 7) * 0.05 for i in range(rows)]
    ts = [_OPEN_UTC + timedelta(minutes=i) for i in range(rows)]
    return pl.DataFrame(
        {
            "ts_utc": pl.Series(ts).dt.replace_time_zone("UTC"),
            "open": [c - 0.05 for c in closes],
            "high": [c + 0.10 for c in closes],
            "low": [c - 0.10 for c in closes],
            "close": closes,
            "volume": [10] * rows,
        }
    )


def _best_of(n: int, frame: pl.DataFrame) -> float:
    """Best-of-*n* wall seconds for one clean screen_frame pass (house MIN idiom)."""
    best = float("inf")
    for _ in range(n):
        t0 = time.perf_counter()
        out = screen_frame(frame, ts_col="ts_utc", source="bench")
        best = min(best, time.perf_counter() - t0)
        assert out is frame  # same-object identity — clean data, zero drops
    return best


def test_clean_frame_screen_cost_is_honestly_linear() -> None:
    """The clean-frame screen pass SCALES with rows — the honest O(rows) pin.

    A 10x-larger frame must cost at least 2x (generous CI-noise floor; the
    measured sandbox ratio is ~8-10x). If a future vectorized rewrite makes
    this genuinely flat, this pin fails and the flat claim is re-earned
    consciously (docstrings + ledger updated), never silently assumed.
    """
    small = _clean_frame(375)  # one session day of 1m bars
    big = _clean_frame(3750)  # 10x
    t_small = _best_of(5, small)
    t_big = _best_of(5, big)
    assert t_big >= t_small * 2.0, (
        f"clean-frame screen cost went FLAT ({t_small * 1e3:.3f} ms -> "
        f"{t_big * 1e3:.3f} ms for 10x rows) — if a vectorized rewrite landed, "
        f"re-earn the flat claim consciously (update the bar_screen docstrings + "
        f"the DEC-DASHBOARD-READ-SCREEN-001 ledger honest-cost note, then relax "
        f"this pin)"
    )


def test_clean_frame_screen_returns_same_object() -> None:
    """Byte-identity on clean data: the SAME frame object, never a copy."""
    frame = _clean_frame(375)
    assert screen_frame(frame, ts_col="ts_utc") is frame
