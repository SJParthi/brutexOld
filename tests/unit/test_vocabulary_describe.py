"""Unit tests for the strategy-mask explainer (DEC-COMBO-EXPLAIN-001).

Covers ``describe_mask`` (multi-group / empty / reserved-bit), ``matched_condition_names``
(ordering / empty), and the group-partition invariant raise — for the strict 100%
line+branch gate.
"""

from __future__ import annotations

import pytest
from brutex.vocabulary.bits import (
    BIT_CLOSE_ABOVE_SUPERTREND,
    BIT_CLOSE_ABOVE_VWAP,
    BIT_CLOSE_BELOW_VWAP,
    BIT_NARROW_CPR_DAY,
    IMPLEMENTED_MASK,
    RETIRED_MASK,
    TOTAL_CONDITIONS,
)
from brutex.vocabulary.describe import (
    GROUP_RANGES,
    RETIRED_SUFFIX,
    MaskGroup,
    _check_group_invariants,
    condition_display_name,
    describe_mask,
    display_condition_names,
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
        # bit 74 is reserved u128 headroom (>= TOTAL_CONDITIONS) → ignored;
        # only bit 0 decodes. (bits 66-68 are REAL gap-midpoint conditions on
        # the re-stacked train and bits 69-73 became REAL conditions in the
        # fib-template express — DEC-FIB-TEMPLATE-EXPRESS-001 — so the
        # headroom probe moved to 74.)
        assert describe_mask(_mask(0, 74)) == [MaskGroup("EMA Position", ("close_above_ema20",))]


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


class TestTombstoneRendering:
    """TOMB-01 — the ONE common tombstone-aware rendering authority.

    VACUOUS on this tree: all 74 declared bits are IMPLEMENTED and
    ``RETIRED_MASK == 0`` (DEC-VOCAB-VWAP-RETAIN-001, MIGRATION.md #2206 —
    the bits-52/53 wipe reversed), so NO bit renders the "(retired)" suffix.
    The mask-driven authority is deliberately KEPT (grow-only machinery for
    any future tombstone) — these pins hold its RETIRED_MASK-derivation law.
    """

    def test_retained_vwap_bits_render_plain(self) -> None:
        # The wipe-era "(retired)" suffix expectation is dead — the RETAINED
        # pair reads its plain live names again (#2206).
        assert condition_display_name(BIT_CLOSE_ABOVE_VWAP) == "close_above_vwap"
        assert condition_display_name(BIT_CLOSE_BELOW_VWAP) == "close_below_vwap"

    def test_implemented_bit_renders_plain(self) -> None:
        assert condition_display_name(0) == "close_above_ema20"
        assert condition_display_name(BIT_NARROW_CPR_DAY) == "narrow_cpr_day"

    def test_layout_only_bit_renders_plain_not_retired(self) -> None:
        # IMPLEMENTED since the vocab-66 SuperTrend flip — plain either way
        # (the suffix appears ONLY for RETIRED_MASK bits, of which there are
        # none on the 74/74 tree).
        assert condition_display_name(BIT_CLOSE_ABOVE_SUPERTREND) == "close_above_supertrend"

    def test_undeclared_bit_raises(self) -> None:
        with pytest.raises(KeyError):
            condition_display_name(TOTAL_CONDITIONS)

    def test_suffix_set_is_exactly_the_retired_mask(self) -> None:
        # Exhaustive: the suffix appears for EXACTLY the RETIRED_MASK bits —
        # the EMPTY set on the 74/74 retention tree (#2206).
        suffixed = {
            bit
            for bit in range(TOTAL_CONDITIONS)
            if condition_display_name(bit).endswith(RETIRED_SUFFIX)
        }
        assert suffixed == {bit for bit in range(TOTAL_CONDITIONS) if (RETIRED_MASK >> bit) & 1}
        assert suffixed == set()
        assert RETIRED_MASK == 0

    def test_matched_condition_names_renders_a_vwap_mask_plain(self) -> None:
        # A persisted mask carrying bit 52 beside a live bit — both PLAIN
        # (the retention reversed the wipe; 52 is a live condition again).
        assert matched_condition_names(_mask(0, BIT_CLOSE_ABOVE_VWAP)) == [
            "close_above_ema20",
            "close_above_vwap",
        ]

    def test_describe_mask_renders_the_vwap_group_plain(self) -> None:
        groups = describe_mask(_mask(BIT_CLOSE_ABOVE_VWAP, BIT_CLOSE_BELOW_VWAP))
        assert groups == [
            MaskGroup(
                group="VWAP Position",
                conditions=("close_above_vwap", "close_below_vwap"),
            )
        ]

    def test_implemented_only_mask_is_byte_identical_to_plain_names(self) -> None:
        # Current-era output unchanged: an all-implemented mask carries no suffix.
        names = matched_condition_names(IMPLEMENTED_MASK)
        assert all(RETIRED_SUFFIX not in name for name in names)

    def test_display_condition_names_maps_old_payload_names(self) -> None:
        assert display_condition_names(
            ["close_above_vwap", "close_above_ema20", "junk_name", "close_below_vwap (retired)"]
        ) == [
            "close_above_vwap",  # a RETAINED live name maps to itself (#2206)
            "close_above_ema20",
            "junk_name",  # unknown strings pass through verbatim — never a crash
            # A WIPE-ERA artifact's already-suffixed string is not a known
            # name — it passes through verbatim (never a crash, never dropped).
            "close_below_vwap (retired)",
        ]

    def test_display_condition_names_is_idempotent(self) -> None:
        once = display_condition_names(["close_above_vwap", "bar_bullish"])
        assert display_condition_names(once) == once


class TestGroupInvariant:
    def test_canonical_groups_partition_the_vocabulary(self) -> None:
        covered = [bit for _, rng in GROUP_RANGES for bit in rng]
        assert sorted(covered) == list(range(TOTAL_CONDITIONS))

    def test_tampered_grouping_raises(self) -> None:
        # Drop the last family → no longer a full partition → AssertionError.
        with pytest.raises(AssertionError, match=rf"partition bits 0\.\.{TOTAL_CONDITIONS - 1}"):
            _check_group_invariants(GROUP_RANGES[:-1], TOTAL_CONDITIONS)
