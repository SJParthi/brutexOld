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
