"""Frozen golden: polars ``Decimal(38,2) → Float64`` cast ≡ Python ``float(Decimal)``.

DEC-PREBUILD-FRAME-WRITE-001 §2 item 7(a), demanded by the V3 constraints audit
§0(c): the existing snap-parity golden pins the :func:`_snap_price_expr`
EXPRESSION (its ``Decimal(scale=2)`` outputs equal the legacy
``round_money(Decimal(str(x)))``), but nothing pinned the CAST — the
``Decimal(38,2) → Float64`` step a frame-direct render performs where the
legacy path calls Python ``float(Decimal)`` per row. This file freezes that
cast law BIT-for-bit (IEEE-754 pattern compare via ``struct.pack``) over:

* exact ``.005`` half-up boundaries (post-snap grid points);
* long-tail float reprs near a boundary — BOTH the 11-17-fractional-digit
  class (``0.00499999999``) and the 18-20-fractional-digit sub-0.1 class the
  round-3 review MAJOR caught (``0.004999999999999996`` — lossy at the old
  ``_SNAP_PARSE_SCALE=17``, lossless at 20);
* legacy off-grid vendor noise (``14020.7999``);
* max-realistic magnitudes up to the fast lane's G4 admission bound
  (``< 1e12``) — far below the documented ``≳ 1e16`` snap-expression
  overflow;
* the exhaustive sub-₹20 cent grid + a seeded random grid sample ≤ ₹10⁷.

Two pins, both against the live-Python oracle (frozen semantics — CPython's
``float(Decimal)`` is correctly rounded and version-stable):

1. the DIRECT cast: a ``Decimal(precision=38, scale=2)`` polars column cast to
   ``Float64`` must equal ``float(d)`` bit-for-bit for every 2dp grid Decimal;
2. the full pipeline: ``_snap_price_expr`` → ``cast(Float64)`` must equal
   ``float(round_money(Decimal(str(x))))`` bit-for-bit — the byte the legacy
   render writes.

Whitebox: exercises the private ``_snap_price_expr``, so per the DEC-P0-017
convention the file silences ``reportPrivateUsage`` at the top.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import random
import struct
from decimal import Decimal

import pytest
from brutex.data.parquet_writer import _snap_price_expr
from brutex.money import round_money

pytest.importorskip("polars")

import polars as pl

#: The fast lane's magnitude admission bound (G4) — the cast law is pinned up
#: to (just under) this bound, so every admitted price is inside the proven
#: envelope. Frozen literal (never imported), like the render golden's knobs.
_MAGNITUDE_BOUND = 1e12


def _bits(x: float) -> bytes:
    """The IEEE-754 bit pattern of *x* — signed zeros / payloads distinguished."""
    return struct.pack("<d", x)


_EDGE_FLOATS: list[float] = [
    0.0,
    0.005,
    0.00499999999,
    # The 18-20-fractional-digit sub-0.1 class (round-3 review MAJOR): lossy
    # at the old scale-17 parse (fast lane stored 0.01 vs legacy 0.00),
    # lossless at scale 20.
    0.004999999999999996,
    0.014999999999999996,
    0.024999999999999998,
    0.005000000000000001,
    0.015,
    1.005,
    1.004,
    2.675,
    100.005,
    100.015,
    14020.7999,
    99999.995,
    100000.125,
    123456789.005,
    999999999999.99,  # the largest 2dp grid point under the G4 bound
    999999999999.985,  # half boundary just under the bound
]


def _grid_decimals() -> list[Decimal]:
    """A deterministic set of 2dp grid Decimals across the admitted magnitude range."""
    values: list[Decimal] = []
    # Exhaustive sub-₹20 cent grid (2,000 points).
    values.extend(Decimal(n) / Decimal(100) for n in range(0, 2000))
    # Seeded random grid points ≤ ₹10⁷ (the realistic price/strike range).
    rng = random.Random(20260714)
    values.extend(Decimal(rng.randrange(0, 10**9)) / Decimal(100) for _ in range(5000))
    # Magnitude ladder up to the G4 bound.
    values.extend(
        Decimal(sig) * (Decimal(10) ** exp) + Decimal("0.05")
        for exp in range(0, 12)
        for sig in (1, 7)
        if Decimal(sig) * (Decimal(10) ** exp) < Decimal(int(_MAGNITUDE_BOUND))
    )
    return values


def test_decimal38_2_to_float64_cast_matches_python_float() -> None:
    """DIRECT cast pin: polars ``Decimal(38,2) → Float64`` ≡ ``float(Decimal)`` bitwise."""
    decimals = _grid_decimals()
    series = pl.Series("px", decimals, dtype=pl.Decimal(precision=38, scale=2))
    cast_floats = series.cast(pl.Float64).to_list()
    assert len(cast_floats) == len(decimals)
    for d, f in zip(decimals, cast_floats, strict=True):
        assert _bits(f) == _bits(float(d)), f"cast diverged for {d}: {f!r} != {float(d)!r}"


def test_snap_then_cast_matches_legacy_render_float() -> None:
    """PIPELINE pin: ``_snap_price_expr`` → ``Float64`` ≡ ``float(round_money(Decimal(str(x))))``."""
    rng = random.Random(20260714 + 1)
    values = list(_EDGE_FLOATS)
    # Random off-grid 4-6dp vendor-noise floats across the magnitude range.
    values.extend(round(rng.uniform(0.0, 25_000.0), 4) for _ in range(3000))
    values.extend(round(rng.uniform(0.0, 1e9), 6) for _ in range(1000))

    frame = pl.DataFrame({"open": values}, schema={"open": pl.Float64})
    pipeline = (
        frame.select(_snap_price_expr("open"))
        .select(pl.col("open").cast(pl.Float64))
        .to_series()
        .to_list()
    )
    for x, produced in zip(values, pipeline, strict=True):
        legacy = float(round_money(Decimal(str(x))))
        assert _bits(produced) == _bits(legacy), (
            f"snap+cast diverged for {x!r}: {produced!r} != legacy {legacy!r}"
        )


def test_cast_pin_covers_half_boundaries_post_snap() -> None:
    """Anti-vacuity: the grid includes genuine ``.x5`` half-boundary POST-snap points."""
    decimals = _grid_decimals()
    assert any(d % Decimal("0.10") == Decimal("0.05") for d in decimals)
    assert max(decimals) > Decimal(10) ** 11  # the magnitude ladder reached the bound
