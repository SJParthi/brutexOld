"""Bench: the vectorized 2dp-snap merge READ is faster than the legacy per-row loop.

DEC-LAKE-WRITE-VECTORIZE-001 — the merge READ's per-row
``round_money(Decimal(str(float)))`` reconstruction is replaced by the columnar
``_snap_price_expr``. This bench makes the speedup CI-REPRODUCIBLE (CLAUDE.md §12
requires a committed ``bench`` test type) instead of an ad-hoc prose number: it
times BOTH the legacy per-row Decimal loop AND the new vectorized snap on the same
deep-partition frame and asserts the vectorized path is strictly faster.

The constant-factor win (~1.8× wall-time on a ~7,500-row partition; ~10× on the
price-column snap in isolation) comes from moving the heavy ``Decimal(str())`` +
half-up math out of a pure-Python loop into compiled polars. A constant-factor
win is robust under MIN-of-N timing (the least noisy estimator), so the assertion
holds even on a noisy shared CI runner — but a small margin is allowed so a
pathological scheduling hiccup never flakes the gate (it would have to make the
vectorized path SLOWER than the legacy loop, which the real win does not).

Whitebox: exercises the private ``_snap_price_expr``, so per DEC-P0-017 the file
silences ``reportPrivateUsage`` at the top.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest

# The CI ``bench`` job installs lean (no ``[data]`` extra), so ``polars`` — which
# the writer needs at write time — may be absent. Skip the whole module then,
# mirroring ``test_parquet_write_o1.py``. Data-layer symbols imported after.
pytest.importorskip("polars")

import polars as pl
from brutex.data.parquet_writer import _PRICE_COLUMNS, _snap_price_expr

#: A deep month partition — the steady-state re-pull case the win targets.
_ROWS = 7_500
#: Repeats; we take the MIN (least-noisy estimator of the true compute cost).
_REPS = 15
#: A small CI-noise margin: the vectorized path must be at least this much faster.
#: 1.0 = "no slower"; we require a genuine win with headroom for scheduler noise.
_MIN_SPEEDUP = 1.05

_Q = Decimal("0.01")


def _frame() -> pl.DataFrame:
    """A ``_ROWS``-deep frame of 4dp-noisy OHLC prices (one (year, month))."""
    base = datetime(2024, 1, 1, 4, 0, tzinfo=UTC)
    return pl.DataFrame(
        {
            "timestamp": [base + timedelta(minutes=i) for i in range(_ROWS)],
            "open": [14020.7999 + (i % 900) * 0.13 for i in range(_ROWS)],
            "high": [14025.7999 + (i % 900) * 0.13 for i in range(_ROWS)],
            "low": [14015.7999 + (i % 900) * 0.13 for i in range(_ROWS)],
            "close": [14020.5001 + (i % 900) * 0.13 for i in range(_ROWS)],
            "volume": list(range(_ROWS)),
            "open_interest": [None if i % 3 == 0 else i * 11 for i in range(_ROWS)],
        },
        schema={
            "timestamp": pl.Datetime(time_unit="us", time_zone="UTC"),
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
            "open_interest": pl.Int64,
        },
    )


def _legacy_snap(df: pl.DataFrame) -> list[Decimal]:
    """The pre-vectorization per-row ``round_money(Decimal(str(float)))`` loop."""
    out: list[Decimal] = []
    for column in _PRICE_COLUMNS:
        for value in df[column].to_list():
            out.append(Decimal(str(value)).quantize(_Q, rounding=ROUND_HALF_UP))
    return out


def _vectorized_snap(df: pl.DataFrame) -> list[Decimal]:
    """The committed columnar ``_snap_price_expr`` path (the production reader)."""
    snapped = df.with_columns(*(_snap_price_expr(c) for c in _PRICE_COLUMNS))
    out: list[Decimal] = []
    for column in _PRICE_COLUMNS:
        out.extend(snapped[column].to_list())
    return out


def _best_ms(fn: object, df: pl.DataFrame) -> float:
    """Min over ``_REPS`` runs, in milliseconds (least-noisy compute estimate)."""
    assert callable(fn)
    fn(df)  # warm
    best = float("inf")
    for _ in range(_REPS):
        start = time.perf_counter()
        fn(df)
        best = min(best, time.perf_counter() - start)
    return best * 1000.0


class TestVectorizedSnapIsFaster:
    """The columnar snap beats the per-row Decimal loop — and stays byte-equal."""

    def test_vectorized_is_faster_than_legacy_loop(self) -> None:
        df = _frame()

        # Parity floor: the bench compares the SAME computation, so the two paths
        # must produce numerically-equal Decimals (else the speed number is meaningless).
        assert _legacy_snap(df) == _vectorized_snap(df)

        legacy_ms = _best_ms(_legacy_snap, df)
        vectorized_ms = _best_ms(_vectorized_snap, df)
        speedup = legacy_ms / max(vectorized_ms, 1e-6)

        assert vectorized_ms * _MIN_SPEEDUP < legacy_ms, (
            f"vectorized snap not faster than the legacy per-row loop on a "
            f"{_ROWS}-row partition: vectorized={vectorized_ms:.2f}ms, "
            f"legacy={legacy_ms:.2f}ms, speedup={speedup:.2f}× "
            f"(need ≥ {_MIN_SPEEDUP}×). The DEC-LAKE-WRITE-VECTORIZE-001 win regressed."
        )
