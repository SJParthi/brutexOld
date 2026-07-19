"""Byte-parity golden: vectorized 2dp snap == legacy ``round_money(Decimal(str()))``.

DEC-LAKE-WRITE-VECTORIZE-001 — the merge READ's per-row
``round_money(Decimal(str(float)))`` reconstruction (the GIL-held biggest write
cost on a deep month partition) is replaced by the polars-native columnar
:func:`~brutex.data.parquet_writer._snap_price_expr`. This file is the
non-negotiable byte-parity oracle: the vectorized expression must reproduce the
EXACT ``Decimal.quantize(Decimal("0.01"), ROUND_HALF_UP)`` of ``Decimal(str(x))``
for every value — half-away-from-zero, never truncation (DEC-MONEY-2DP-001 /
DEC-SPOT-PRICE-2DP-001) — across vendor float-noise (``14020.7999``), exact
``.005`` half boundaries (the case where a banker's-round / binary-float path
would diverge), negatives, zeros, and large strikes.

The companion ``test_writer_columnar_parity.py`` already proves the END-TO-END
``_read_existing_bars`` output is byte-identical to the verbatim legacy per-row
path; this file pins the SNAP expression itself, value-by-value, so a future
edit to ``_snap_price_expr`` that silently breaks the half-up contract is caught
directly at the expression level (not only through the writer).

Whitebox: exercises the private ``_snap_price_expr``, so per the DEC-P0-017
convention the file silences ``reportPrivateUsage`` at the top.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import random
from decimal import ROUND_HALF_UP, Decimal

import pytest
from brutex.data.parquet_writer import _snap_price_expr
from brutex.money import round_money

pytest.importorskip("polars")

import polars as pl


def _legacy_snap_float(x: float) -> float:
    """The PRE-DEC-LAKE-WRITE-VECTORIZE-001 stored value, verbatim.

    The byte that ``_render_bytes`` would write for a stored ``x``: parse the
    float's ``str`` repr to an exact Decimal, snap half-up to 2dp via
    ``round_money``, and take ``float`` (what the re-render emits). This is the
    exact byte contract the vectorized path must reproduce.
    """
    return float(round_money(Decimal(str(x))))


def _legacy_snap_decimal(x: float) -> Decimal:
    """The legacy stored ``Decimal`` (what ``_bar_values_equal`` compares)."""
    return round_money(Decimal(str(x)))


def _vectorized_floats(values: list[float]) -> list[float]:
    """Run ``_snap_price_expr`` over *values* and return the ``float`` results."""
    frame = pl.DataFrame({"open": values}, schema={"open": pl.Float64})
    snapped = frame.select(_snap_price_expr("open"))
    return snapped.select(pl.col("open").cast(pl.Float64)).to_series().to_list()


def _vectorized_decimals(values: list[float]) -> list[Decimal]:
    """Run ``_snap_price_expr`` over *values* and return the ``Decimal`` results."""
    frame = pl.DataFrame({"open": values}, schema={"open": pl.Float64})
    return frame.select(_snap_price_expr("open")).to_series().to_list()


#: Hand-picked edge cases. ``.005`` boundaries are where ``Decimal(str())``
#: half-up DIVERGES from a binary-float round (polars ``.round`` / a direct
#: ``Float64 -> Decimal`` cast) — the whole reason the str round-trip is needed.
_EDGE_CASES: list[float] = [
    14020.7999,  # vendor 4dp noise → 14020.80
    100.005,  # exact half → 100.01 (binary float would give 100.00)
    100.015,  # exact half → 100.02
    1.005,  # → 1.01
    1.004,  # → 1.00 (below half)
    2.675,  # classic float-repr trap → 2.68
    99999.995,  # → 100000.00 (carry across the integer)
    100000.125,  # large strike, exact half → 100000.13
    0.0,
    -0.0,
    -1.005,  # negative exact half → -1.01 (away from zero)
    -100000.125,  # → -100000.13
    12187.3,  # already-2dp-ish, trailing-zero stability
    22000.00,
    0.05,
    0.04,
    # ── Long-tail truncation-boundary class (the second-review BLOCKER) ──
    # An 11-17-digit float just BELOW a .xx5 boundary. A lossy intermediate
    # Decimal(scale=10) would round these UP to the .xx5 grid FIRST, then half-up to
    # the WRONG 2dp; the full-scale parse keeps them on the legacy (DOWN) side.
    0.00499999999,  # → 0.00 (a too-small scale gives the wrong 0.01)
    0.12499999999,  # → 0.12 (wrong: 0.13)
    1.00499999999,  # → 1.00 (wrong: 1.01)
    -0.00499999999,  # → -0.00 (wrong: -0.01)
    2.67499999999,  # → 2.67 (wrong: 2.68)
    99999.99499999999,  # → 99999.99 (wrong: 100000.00)
    # And just ABOVE the boundary — must still round UP (both paths agree, a guard
    # that the fix did not over-correct the below-boundary class into a regression).
    0.00500000001,  # → 0.01
    1.00500000001,  # → 1.01
    0.004999999999999,  # 15-digit tail below half → 0.00
    0.005000000000001,  # 15-digit tail above half → 0.01
    # ── 18-20-FRACTIONAL-digit sub-0.1 class (the round-3 review MAJOR,
    # DEC-PREBUILD-FRAME-WRITE-001) ── a shortest float repr carries ≤ 17
    # SIGNIFICANT digits, but a sub-0.1 value spends non-significant leading
    # fractional ZEROS too, so the repr can reach 18-20 FRACTIONAL digits.
    # The old ``_SNAP_PARSE_SCALE=17`` parse ROUNDED these at digit 17 —
    # crossing the half-paisa boundary BEFORE the half-up step — so the fast
    # lane stored 0.01/0.02/0.03 where legacy ``round_money(Decimal(str(x)))``
    # stores 0.00/0.01/0.02. Each of these FAILED at scale 17; scale 20 parses
    # them losslessly.
    0.004999999999999996,  # 18 frac digits, just below 0.005 → 0.00 (17 gave 0.01)
    0.014999999999999996,  # 18 frac digits, just below 0.015 → 0.01 (17 gave 0.02)
    0.024999999999999998,  # nextafter(0.025, 0) → 0.02 (17 gave 0.03)
    0.004999999999999999,  # nextafter(0.005, 0), the last float below the half → 0.00
    0.014999999999999998,  # nextafter(0.015, 0) → 0.01
    0.005000000000000001,  # nextafter(0.005, 1) — just ABOVE → 0.01 (no over-correction)
    0.015000000000000001,  # nextafter(0.015, 1) → 0.02
    -0.004999999999999996,  # negative twin → -0.00 (numeric parity; signed-zero edge doc'd)
    # Sub-1e-4 values: polars prints FIXED notation ("0.0000999") where Python
    # str goes scientific ("9.99e-05"), so the repr can exceed 20 fractional
    # digits and the parse is VALUE-lossy (≤ 5e-21) — but provably SNAP-exact
    # (no half-paisa boundary within reach). Pinned so the envelope stays honest.
    9.99e-05,  # → 0.00 both paths
    9.989999999999999e-05,  # nextafter(9.99e-5, 0): 21 frac digits, lossy parse → 0.00
    1e-05,  # → 0.00 (the polars-vs-python repr-shape example)
]


class TestVectorizedSnapByteParity:
    """``_snap_price_expr`` reproduces the legacy half-up 2dp contract exactly."""

    @pytest.mark.parametrize("value", _EDGE_CASES)
    def test_edge_case_float_parity(self, value: float) -> None:
        """Each edge case: vectorized ``float`` == ``float(round_money(Decimal(str)))``."""
        (got,) = _vectorized_floats([value])
        assert got == _legacy_snap_float(value), (
            f"vectorized snap of {value!r} = {got!r}, legacy = {_legacy_snap_float(value)!r}"
        )

    @pytest.mark.parametrize("value", _EDGE_CASES)
    def test_edge_case_decimal_parity(self, value: float) -> None:
        """Each edge case: vectorized ``Decimal`` numerically == legacy ``round_money``."""
        (got,) = _vectorized_decimals([value])
        assert got == _legacy_snap_decimal(value), (
            f"vectorized Decimal of {value!r} = {got!s}, legacy = {_legacy_snap_decimal(value)!s}"
        )

    def test_exact_half_boundaries_round_away_from_zero(self) -> None:
        """Every ``N.xx5`` exact half rounds AWAY from zero (the contract crux)."""
        halves = [round(i + 0.005, 3) for i in range(0, 5000, 3)]
        halves += [round(-(i + 0.005), 3) for i in range(0, 5000, 3)]
        got = _vectorized_floats(halves)
        want = [_legacy_snap_float(x) for x in halves]
        assert got == want

    def test_batch_random_float_parity(self) -> None:
        """A large random batch (positive + negative, 1-5 dp) stays byte-identical."""
        rng = random.Random(20260630)
        values: list[float] = []
        for _ in range(40_000):
            sign = rng.choice((1.0, -1.0))
            values.append(round(sign * rng.uniform(0.0, 300_000.0), rng.choice([1, 2, 3, 4, 5])))
        got_floats = _vectorized_floats(values)
        got_decimals = _vectorized_decimals(values)
        for value, gf in zip(values, got_floats, strict=True):
            assert gf == _legacy_snap_float(value)
        for value, gd in zip(values, got_decimals, strict=True):
            assert gd == _legacy_snap_decimal(value)

    def test_already_snapped_is_a_fixpoint(self) -> None:
        """A value already on the 2dp grid snaps to itself (idempotent re-pull)."""
        snapped_once = _vectorized_floats(_EDGE_CASES)
        snapped_twice = _vectorized_floats(snapped_once)
        assert snapped_once == snapped_twice

    def test_quantize_contract_is_half_up_not_even(self) -> None:
        """Pin that the contract is ROUND_HALF_UP (away from zero), not banker's even.

        ``2.5``-style values are not 2dp halves, so the discriminating cases are the
        ``.xx5`` boundaries: ``0.125 -> 0.13`` (up) and ``0.135 -> 0.14`` (up). A
        banker's-even path would give ``0.12`` and ``0.14`` — this asserts the up.
        """
        got = _vectorized_floats([0.125, 0.135, 0.145])
        want = [
            float(Decimal("0.125").quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            float(Decimal("0.135").quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            float(Decimal("0.145").quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        ]
        assert got == want == [0.13, 0.14, 0.15]
