"""Rust-seam parity pins for :mod:`brutex.engine.baseline` (slice 2d).

DEC-RUST-CASCADE-RESULTS-001 phase B: the frozen replay corpus
(``test_baseline_rust_parity_golden.py``) is the primary contract; this file
pins the SPLIT-EXPOSED arms the corpus does not reach — the ``_price_str``
crate quantize twin against the LIVE ``round_money`` oracle (a hypothesis
differential over the full Decimal domain, the quiet-NaN passthrough, the
``sNaN`` / ``Infinity`` / beyond-28-digit ``InvalidOperation`` parity with
CPython's EXACT exception args, the negative-zero sign law) and the
quirk-24 write-side composition (an ``Infinity``-anchor series legally
CONSTRUCTS, then ``write_baseline`` raises ``InvalidOperation`` out of the
render's price snap — never a silent artifact).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pytest
from brutex.engine.baseline import BaselineSeries, _price_str, write_baseline
from brutex.money import round_money
from hypothesis import given
from hypothesis import strategies as st

_D1 = date(2024, 3, 4)
_D2 = date(2024, 3, 5)


def _oracle_price_str(value: Decimal) -> str:
    """The deleted law VERBATIM — ``str(round_money(value))``."""
    return str(round_money(value))


class TestPriceStrParity:
    @given(
        value=st.decimals(
            allow_nan=False,
            allow_infinity=False,
            min_value=Decimal("-1E+30"),
            max_value=Decimal("1E+30"),
            places=None,
        )
    )
    def test_matches_round_money_over_the_decimal_domain(self, value: Decimal) -> None:
        # The full differential incl. the 28-digit context wall: whenever
        # the LIVE round_money law raises InvalidOperation, the crate twin
        # raises the same type; else the strings are byte-equal.
        try:
            expected = _oracle_price_str(value)
        except InvalidOperation:
            with pytest.raises(InvalidOperation):
                _price_str(value)
            return
        assert _price_str(value) == expected

    @pytest.mark.parametrize("token", ["NaN", "-NaN"])
    def test_quiet_nan_passes_through_like_quantize(self, token: str) -> None:
        # CPython quantize propagates a quiet NaN silently (probe-verified);
        # the crate twin passes the token through verbatim — oracle-equal.
        assert _price_str(Decimal(token)) == _oracle_price_str(Decimal(token))

    @pytest.mark.parametrize("token", ["sNaN", "Infinity", "-Infinity", "1E+26"])
    def test_invalid_operation_parity_with_exact_cpython_args(self, token: str) -> None:
        value = Decimal(token)
        with pytest.raises(InvalidOperation) as oracle_info:
            _oracle_price_str(value)
        with pytest.raises(InvalidOperation) as crate_info:
            _price_str(value)
        # The C decimal module raises `InvalidOperation([InvalidOperation])`
        # — str(exc) is "[<class 'decimal.InvalidOperation'>]"; the crate
        # reproduces the EXACT instance shape.
        assert str(crate_info.value) == str(oracle_info.value)
        assert repr(crate_info.value.args) == repr(oracle_info.value.args)

    @pytest.mark.parametrize(
        ("token", "expected"),
        [("-0.004", "-0.00"), ("-0", "-0.00"), ("0E+2", "0.00")],
    )
    def test_negative_zero_keeps_its_sign(self, token: str, expected: str) -> None:
        assert _price_str(Decimal(token)) == expected
        assert _oracle_price_str(Decimal(token)) == expected


class TestInfinityAnchorWriteComposition:
    def test_infinity_anchor_series_constructs_then_write_raises(self, tmp_path: Path) -> None:
        # Quirk 24: `Infinity <= 0` is a legal False, so the series
        # CONSTRUCTS (the load path's `anchor_infinity_loads` case) — but
        # writing it hits the price snap, which raises the exact CPython
        # InvalidOperation out of write_baseline (never a silent artifact).
        series = BaselineSeries(
            instrument="NIFTY",
            start=_D1,
            end=_D2,
            anchor_open=Decimal("Infinity"),
            sessions=((_D1, Decimal("101.20")), (_D2, Decimal("99.95"))),
            total_return_pct=-0.05,
            source_note="note",
        )
        target = tmp_path / "baseline-NIFTY.json"
        with pytest.raises(InvalidOperation):
            write_baseline(target, series, data_snapshot_hash="5a" * 32)
        assert not target.exists()
