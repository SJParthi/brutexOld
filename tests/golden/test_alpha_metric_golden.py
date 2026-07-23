"""Frozen alpha vectors with in-file hand-derivations (DEC-ALPHA-BUYHOLD-METRIC-001).

The golden oracle for the ONE formula (module docstring of
:mod:`brutex.engine.alpha_metric`)::

    strat_pct = float(net_pnl_paisa) / float(start_equity_paisa) * 100.0
    bh_pct    = float(last_close_paisa - anchor_open_paisa)
                / float(anchor_open_paisa) * 100.0
    alpha_pct = strat_pct - bh_pct

Every vector's expected value is HAND-DERIVED in its comment; the two
non-terminating cases are pinned by their exact IEEE-754 ``float.hex()``
pattern (any op-order / rounding drift moves the bits). Moving a value is a
deliberate, ledgered re-lock (the ``_GOLDEN_CONTENT_HASH`` discipline). Both
arms are pinned: the pure-Python oracle always, and the Rust kernel whenever
the wheel is installed (bit-equal to the same frozen vectors).
"""

from __future__ import annotations

import struct

import pytest
from brutex.engine.alpha_metric import alpha_vs_buyhold_pct_python

#: (net_pnl_paisa, start_equity_paisa, anchor_open_paisa, last_close_paisa,
#:  expected_hex) — expected values as IEEE-754 float.hex() patterns.
_GOLDEN_VECTORS: tuple[tuple[int, int, int, int, str], ...] = (
    # strategy +₹1,00,000 on ₹10,00,000 equity = +10%; B&H ₹25,000.00 →
    # ₹26,000.00 = +4%. alpha = 10 − 4 = 6.0 (every step exact in f64).
    (10_000_000, 100_000_000, 2_500_000, 2_600_000, (6.0).hex()),
    # strategy −₹50,000 on ₹10,00,000 = −5%; B&H ₹20,000.00 → ₹20,500.00 =
    # +2.5%. alpha = −5 − 2.5 = −7.5 (exact).
    (-5_000_000, 100_000_000, 2_000_000, 2_050_000, (-7.5).hex()),
    # flat strategy (0%) vs B&H ₹50,000.00 → ₹40,000.00 = −20%.
    # alpha = 0 − (−20) = +20.0 (exact).
    (0, 1_000_000, 5_000_000, 4_000_000, (20.0).hex()),
    # strategy +₹2,500 on ₹10,00,000 = +0.25%; flat B&H (close == anchor).
    # alpha = 0.25 − 0 = 0.25 (exact: 0.25 is a power-of-two fraction).
    (250_000, 100_000_000, 1_000_000, 1_000_000, (0.25).hex()),
    # the buy-&-hold strategy itself: net_pnl = close − anchor over
    # equity = anchor → the two legs are the SAME expression → exactly 0.0.
    (100_000, 2_500_000, 2_500_000, 2_600_000, (0.0).hex()),
    # non-terminating legs: 1/3·100 − 2/7·100. Derivation: 1/3 and 2/7 have
    # no finite binary expansion — each leg is the correctly-rounded
    # convert-divide-multiply chain; the subtraction's exact IEEE-754 result
    # (≈ 4.761904761904759) is pinned by its bit pattern.
    (1, 3, 7, 9, "0x1.30c30c30c30c0p+2"),
    # beyond 2**53 (the convert-FIRST law's teeth): pnl = 2**60 + 1 (rounds
    # to 2**60 at the f64 conversion) over equity 3; B&H = 1-paisa gain on
    # anchor 2**60 − 1. Exact IEEE-754 result ≈ 3.843071682022823e+19,
    # pinned by its bit pattern — an exact-rational division (no
    # convert-first) or an operand swap moves these bits.
    (2**60 + 1, 3, 2**60 - 1, 2**60, "0x1.0aaaaaaaaaaaap+65"),
)


def _bits(value: float) -> bytes:
    return struct.pack("<d", value)


@pytest.mark.parametrize(("pnl", "equity", "anchor", "close", "expected_hex"), _GOLDEN_VECTORS)
def test_oracle_matches_the_frozen_vectors(
    pnl: int, equity: int, anchor: int, close: int, expected_hex: str
) -> None:
    got = alpha_vs_buyhold_pct_python(pnl, equity, anchor, close)
    assert _bits(got) == _bits(float.fromhex(expected_hex)), (got.hex(), expected_hex)


@pytest.mark.parametrize(("pnl", "equity", "anchor", "close", "expected_hex"), _GOLDEN_VECTORS)
def test_kernel_matches_the_same_frozen_vectors(
    pnl: int, equity: int, anchor: int, close: int, expected_hex: str
) -> None:
    fno_math = pytest.importorskip("fno_math")
    got = fno_math.alpha_vs_buyhold_pct(pnl, equity, anchor, close)
    assert _bits(got) == _bits(float.fromhex(expected_hex)), (got.hex(), expected_hex)
