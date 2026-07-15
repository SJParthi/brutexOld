"""Unit tests for the strategy-mask explainer (DEC-COMBO-EXPLAIN-001).

Covers ``describe_mask`` (multi-group / empty / reserved-bit), ``matched_condition_names``
(ordering / empty), and the group-partition invariant raise — for the strict 100%
line+branch gate.
"""

from __future__ import annotations

import pytest
from brutex.vocabulary.bits import TOTAL_CONDITIONS
from brutex.vocabulary.describe import (
    GROUP_RANGES,
    MaskGroup,
    _check_group_invariants,
    describe_mask,
    matched_condition_names,
)


def _mask(*bits: int) -> int:
    """Build a u64 mask with the given bit positions set."""
    m = 0
    for b in bits:
        m |= 1 << b
    return m


class TestDescribeMask:
    def test_multi_group_combo_decodes_to_its_rule(self) -> None:
        # bits 0,4 (EMA), 23 (Fib 61.8), 30 (bullish bar), 44 (early morning) →
        # the readable rule across four strategy families, in canonical order.
        groups = describe_mask(_mask(0, 4, 23, 30, 44))
        assert groups == [
            MaskGroup("EMA Position", ("close_above_ema20", "ema20_above_ema200")),
            MaskGroup("Fibonacci", ("near_fib_618",)),
            MaskGroup("Candle Shape", ("bar_bullish",)),
            MaskGroup("Time of Day", ("early_morning",)),
        ]

    def test_empty_mask_has_no_groups(self) -> None:
        # The no-condition mask exercises the "no match in any family" branch.
        assert describe_mask(0) == []

    def test_reserved_bits_are_ignored(self) -> None:
        # bit 66 is reserved u128 headroom (>= TOTAL_CONDITIONS) → ignored;
        # only bit 0 decodes. (bit 60 became a REAL condition — above_cpr_tc —
        # in the 60→66 reshape, so the headroom probe moved to 66.)
        assert describe_mask(_mask(0, 66)) == [MaskGroup("EMA Position", ("close_above_ema20",))]


class TestMatchedConditionNames:
    def test_names_in_ascending_bit_order(self) -> None:
        # Supplied out of order; returned ascending by bit (0, 23, 44).
        assert matched_condition_names(_mask(44, 0, 23)) == [
            "close_above_ema20",
            "near_fib_618",
            "early_morning",
        ]

    def test_empty_mask_yields_no_names(self) -> None:
        assert matched_condition_names(0) == []


class TestGroupInvariant:
    def test_canonical_groups_partition_the_vocabulary(self) -> None:
        covered = [bit for _, rng in GROUP_RANGES for bit in rng]
        assert sorted(covered) == list(range(TOTAL_CONDITIONS))

    def test_tampered_grouping_raises(self) -> None:
        # Drop the last family → no longer a full partition → AssertionError.
        with pytest.raises(AssertionError, match=rf"partition bits 0\.\.{TOTAL_CONDITIONS - 1}"):
            _check_group_invariants(GROUP_RANGES[:-1], TOTAL_CONDITIONS)
