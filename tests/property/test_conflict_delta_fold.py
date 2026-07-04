"""Property tests for the max-close-delta fold (DEC-VENDOR-CONFLICT-SURFACE-001).

The vendor-conflict drift surface folds ``max_close_delta`` window → series →
phase → pipeline headline through ONE pure helper
(:func:`brutex.data.parquet_writer.fold_max_close_delta`). The fold's algebra is
load-bearing: the async consumer folds per-series in ARBITRARY completion order
and the phase re-derives from per-pair values, so the result must be
order-independent (commutative + associative) with ``None`` as the identity, and
EXACT (Decimal — prices are Decimal rupees per CLAUDE.md §6's price-unit row,
never float, so no fold step may introduce binary-float drift).

Fail-before proof: ``fold_max_close_delta`` / ``max_conflict_close_delta`` do
not exist on origin/main (``ImportError``), so this suite cannot pass there.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from functools import reduce

from brutex.data.models import CandleBar
from brutex.data.parquet_writer import (
    BarConflict,
    fold_max_close_delta,
    max_conflict_close_delta,
)
from hypothesis import given
from hypothesis import strategies as st

#: Exact-Decimal operands, 2dp-grid like real lake prices (DEC-SPOT-PRICE-2DP-001)
#: PLUS higher-precision values so exactness is proven beyond the tick grid.
_deltas = st.one_of(
    st.none(),
    st.decimals(
        min_value=Decimal("0"),
        max_value=Decimal("999999.9999"),
        allow_nan=False,
        allow_infinity=False,
        places=4,
    ),
)


@given(_deltas)
def test_none_is_left_and_right_identity(a: Decimal | None) -> None:
    """``None`` folds as the identity from either side (no-conflict = neutral)."""
    assert fold_max_close_delta(None, a) == a
    assert fold_max_close_delta(a, None) == a


def test_two_nones_fold_to_none_not_zero() -> None:
    """No conflict anywhere stays honestly ``None`` — never a fabricated 0 (Rule #1)."""
    assert fold_max_close_delta(None, None) is None


@given(_deltas, _deltas)
def test_commutative(a: Decimal | None, b: Decimal | None) -> None:
    """Fold order of two operands never changes the result (worker-completion order)."""
    assert fold_max_close_delta(a, b) == fold_max_close_delta(b, a)


@given(_deltas, _deltas, _deltas)
def test_associative(a: Decimal | None, b: Decimal | None, c: Decimal | None) -> None:
    """Grouping never changes the result — window→series→phase == any regrouping."""
    left = fold_max_close_delta(fold_max_close_delta(a, b), c)
    right = fold_max_close_delta(a, fold_max_close_delta(b, c))
    assert left == right


@given(_deltas)
def test_idempotent(a: Decimal | None) -> None:
    """Folding a value with itself is that value (a re-fold can never inflate)."""
    assert fold_max_close_delta(a, a) == a


@given(st.lists(_deltas, max_size=12))
def test_reduce_equals_bruteforce_max_and_stays_exact_decimal(
    values: list[Decimal | None],
) -> None:
    """A reduce over any operand list equals the brute-force max of the non-None set.

    Exactness: the result IS one of the input Decimals (identity-preserved —
    the fold only selects, never recomputes), so no step can introduce float
    drift or rounding (CLAUDE.md §4 money rule).
    """
    folded = reduce(fold_max_close_delta, values, None)
    real = [v for v in values if v is not None]
    if not real:
        assert folded is None
    else:
        assert folded == max(real)
        assert isinstance(folded, Decimal)
        assert any(folded is v or folded == v for v in real)


def _bar(close: str) -> CandleBar:
    price = Decimal(close)
    return CandleBar(
        timestamp=datetime(2024, 6, 3, 4, 1, tzinfo=UTC),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=1,
        open_interest=None,
    )


@given(st.lists(st.tuples(st.integers(0, 99999), st.integers(0, 99999)), max_size=8))
def test_max_conflict_close_delta_equals_bruteforce(pairs: list[tuple[int, int]]) -> None:
    """The conflict-batch derivation equals the brute-force max |vendor − stored| close.

    Closes are 2dp-grid Decimals (paisa-scaled integers ÷ 100 — the exact grid
    the lake stores per DEC-SPOT-PRICE-2DP-001), so the expected delta is an
    exact Decimal, asserted with ``==`` (no tolerance).
    """
    conflicts = [
        BarConflict(
            timestamp=datetime(2024, 6, 3, 4, 1, tzinfo=UTC),
            stored=_bar(str(Decimal(s) / 100)),
            vendor=_bar(str(Decimal(v) / 100)),
        )
        for s, v in pairs
    ]
    result = max_conflict_close_delta(conflicts)
    if not pairs:
        assert result is None
    else:
        expected = max(abs(Decimal(v) - Decimal(s)) for s, v in pairs) / 100
        assert result == expected
