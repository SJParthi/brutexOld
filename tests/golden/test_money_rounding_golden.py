"""Frozen golden — the DEC-MONEY-2DP-001 rounding law as LITERAL strings.

Audit-session addendum to DEC-CONFIG-RUST-LEAVES-001 (MIGRATION.md #1943,
item 1a): ``round_money`` is applied to EVERY stored OHLC value at the
lake-write boundary (``parquet_writer`` imports it; the vectorized
``_snap_price_expr`` reproduces its byte contract), so the law is
run-identity / stored-bytes CRITICAL. This golden pins the oracle's
outputs over a hostile corpus as LITERAL expected strings — not
oracle-vs-crate parity (which would be vacuous if both drifted together).

THREE coordinated impls carry the ONE half-away-from-zero law and must
move together (addendum 1b):

1. the Python oracle (``brutex/money.py::_round_money_python``) — pinned
   HERE;
2. the ``rust/brutex-core`` crate (``src/money.rs``) — pinned by its cargo
   suite over the SAME literals (``round_money_rounding_boundaries`` +
   ``cross_crate_snap_corpus_consistency_pin``);
3. the polars-columnar ``_snap_price_expr`` contract
   (``brutex/data/parquet_writer.py``) — pinned by
   ``tests/golden/test_writer_vectorized_snap_parity.py``, whose
   ``_EDGE_CASES`` seed this file's :data:`_SNAP_CORPUS` mirrors through
   ``str(float)``.

A failure here means the LAW moved — that is a conscious DEC-level event,
never a refactor detail.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from brutex.money import format_inr, paisa_to_rupees, round_money, rupees_to_paisa

#: The hostile literal corpus — (input, frozen expected str(round_money)).
#: Probed against CPython decimal 2026-07-17 (the T35 milestone-1 probe).
_ROUND_GOLDEN: tuple[tuple[str, str], ...] = (
    # ±0.005 half cells — ties AWAY FROM ZERO (never banker's).
    ("1.005", "1.01"),
    ("-1.005", "-1.01"),
    ("0.005", "0.01"),
    ("-0.005", "-0.01"),
    ("100.005", "100.01"),
    ("100.015", "100.02"),
    # Below-half stays.
    ("1.004", "1.00"),
    ("0.004", "0.00"),
    # Carries, including across the integer boundary.
    ("0.995", "1.00"),
    ("9.995", "10.00"),
    ("-9.995", "-10.00"),
    ("99.995", "100.00"),
    ("999.999", "1000.00"),
    ("99999.995", "100000.00"),
    ("123456789.995", "123456790.00"),
    # Negatives + the sign-preserved negative zero.
    ("-0.001", "-0.00"),
    ("-0", "-0.00"),
    ("-0.00", "-0.00"),
    ("-100000.125", "-100000.13"),
    # Zeros / trailing zeros / already-exact.
    ("0", "0.00"),
    ("1.0100", "1.01"),
    ("22000.00", "22000.00"),
    ("12187.3", "12187.30"),
    ("1234.5", "1234.50"),
    # Scientific-notation Decimals normalise before rounding.
    ("1E+2", "100.00"),
    ("-1E+2", "-100.00"),
    ("1.5E+3", "1500.00"),
    ("1E-7", "0.00"),
    # Long-tail truncation-boundary class (the snap-parity BLOCKER class).
    ("0.00499999999", "0.00"),
    ("-0.00499999999", "-0.00"),
    ("0.12499999999", "0.12"),
    ("1.00499999999", "1.00"),
    ("2.67499999999", "2.67"),
    ("99999.99499999999", "99999.99"),
    ("0.00500000001", "0.01"),
    ("1.00500000001", "1.01"),
    ("0.004999999999999", "0.00"),
    ("0.005000000000001", "0.01"),
    # The classic float-repr trap + vendor 4dp noise.
    ("2.675", "2.68"),
    ("14020.7999", "14020.80"),
)

#: The `_snap_price_expr` corpus (test_writer_vectorized_snap_parity
#: ``_EDGE_CASES``) as ``str(float)`` seeds — the cross-impl consistency
#: leg (the SAME literals the brutex-core cargo pin carries).
_SNAP_CORPUS: tuple[tuple[float, str], ...] = (
    (14020.7999, "14020.80"),
    (100.005, "100.01"),
    (100.015, "100.02"),
    (1.005, "1.01"),
    (1.004, "1.00"),
    (2.675, "2.68"),
    (99999.995, "100000.00"),
    (100000.125, "100000.13"),
    (0.0, "0.00"),
    (-0.0, "-0.00"),
    (-1.005, "-1.01"),
    (-100000.125, "-100000.13"),
    (12187.3, "12187.30"),
    (22000.00, "22000.00"),
    (0.05, "0.05"),
    (0.04, "0.04"),
    (0.00499999999, "0.00"),
    (0.12499999999, "0.12"),
    (1.00499999999, "1.00"),
    (-0.00499999999, "-0.00"),
    (2.67499999999, "2.67"),
    # str(99999.99499999999) COLLAPSES to "99999.995" (the 17-digit
    # literal is not a distinct double) — the legacy float seed therefore
    # rounds UP; the raw decimal-STRING "99999.99499999999" (third frac
    # digit 4 → down) is pinned in _ROUND_GOLDEN instead.
    (99999.99499999999, "100000.00"),
    (0.00500000001, "0.01"),
    (1.00500000001, "1.01"),
    (0.004999999999999, "0.00"),
    (0.005000000000001, "0.01"),
)


class TestRoundMoneyGolden:
    @pytest.mark.parametrize(("raw", "expected"), _ROUND_GOLDEN)
    def test_frozen_literal(self, raw: str, expected: str) -> None:
        assert str(round_money(Decimal(raw))) == expected

    @pytest.mark.parametrize(("value", "expected"), _SNAP_CORPUS)
    def test_snap_corpus_cross_impl_leg(self, value: float, expected: str) -> None:
        # The legacy lake-write seed: round_money(Decimal(str(float))).
        assert str(round_money(Decimal(str(value)))) == expected

    def test_half_even_divergence_negative_pin(self) -> None:
        # Addendum 1c: the law is HALF_UP (away from zero) — NOT
        # trading-core's round_half_even. These ties DIVERGE between the
        # two laws and are LOCKED to the HALF_UP answer (half-even would
        # give 1.00 / 0.12 / -0.12 / 2.34 respectively).
        assert str(round_money(Decimal("1.005"))) == "1.01"
        assert str(round_money(Decimal("0.125"))) == "0.13"
        assert str(round_money(Decimal("-0.125"))) == "-0.13"
        assert str(round_money(Decimal("2.345"))) == "2.35"


class TestDerivedLawsGolden:
    def test_rupees_to_paisa_frozen(self) -> None:
        for raw, expected in (
            ("0.01", 1),
            ("1234.56", 123456),
            ("14020.7999", 1402080),
            ("1.005", 101),
            ("-1.005", -101),
            ("-0.001", 0),  # negative zero collapses on the int path
            ("9.995", 1000),
            ("123456789.995", 12345679000),
        ):
            assert rupees_to_paisa(Decimal(raw)) == expected

    def test_paisa_to_rupees_frozen(self) -> None:
        for paisa, expected in (
            (1, "0.01"),
            (99, "0.99"),
            (100, "1.00"),
            (123456, "1234.56"),
            (-50, "-0.50"),
            (0, "0.00"),
            (100_000_000, "1000000.00"),
        ):
            assert str(paisa_to_rupees(paisa)) == expected

    def test_format_inr_frozen(self) -> None:
        for paisa, expected in (
            (5, "₹0.05"),
            (0, "₹0.00"),
            (123456, "₹1,234.56"),
            (-123456, "₹-1,234.56"),
            (100_000_000, "₹1,000,000.00"),
            (-50, "₹-0.50"),
            (99999, "₹999.99"),
            (123456789012, "₹1,234,567,890.12"),
        ):
            assert format_inr(paisa) == expected
