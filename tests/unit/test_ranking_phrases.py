"""Unit suite for :mod:`brutex.ranking.phrases` (DEC-SWEEP-RESULTS-DEPTH-001, WP3).

Pins the 60-entry table's structural invariants (bit==index, names cross-pinned
against the generated vocabulary), every :func:`check_phrase_invariants` raise
branch via tampered tables, the mechanical assembly for k=1/3/5 masks, the
honest-empty mask-0 answer, the reserved-headroom ignore contract and the
``describe.matched_condition_names`` consistency law.
"""

from __future__ import annotations

import pytest
from brutex.ranking.phrases import (
    PHRASE_TABLE,
    PhraseEntry,
    ReasonText,
    check_phrase_invariants,
    reason_for_mask,
    reason_payload,
)
from brutex.vocabulary.bits import NAME_TO_BIT, TOTAL_CONDITIONS
from brutex.vocabulary.describe import matched_condition_names


class TestTableInvariants:
    def test_exactly_one_entry_per_condition_bit(self) -> None:
        assert len(PHRASE_TABLE) == TOTAL_CONDITIONS == 66

    def test_bit_equals_index(self) -> None:
        for index, entry in enumerate(PHRASE_TABLE):
            assert entry.bit == index

    def test_names_are_the_exact_vocabulary_inverse(self) -> None:
        # Bidirectional: every table name resolves to its own bit AND every
        # vocabulary name appears exactly once — no gap, no alias, no drift.
        assert {entry.name: entry.bit for entry in PHRASE_TABLE} == dict(NAME_TO_BIT)

    def test_every_plain_phrase_is_nonblank_and_distinct(self) -> None:
        plains = [entry.plain for entry in PHRASE_TABLE]
        assert all(plain.strip() for plain in plains)
        # 66 conditions ⇒ 66 distinct meanings — a copy-paste dup is a bug.
        assert len(set(plains)) == len(plains)

    def test_shipped_table_passes_the_checker(self) -> None:
        check_phrase_invariants()  # must not raise


class TestCheckerRaiseBranches:
    def test_wrong_length_rejected(self) -> None:
        with pytest.raises(AssertionError, match="exactly 66 entries"):
            check_phrase_invariants(PHRASE_TABLE[:-1])

    def test_bit_not_index_rejected(self) -> None:
        tampered = (*PHRASE_TABLE[:-1], PhraseEntry(7, "choch_bearish", "words"))
        with pytest.raises(AssertionError, match="index-ordered"):
            check_phrase_invariants(tampered)

    def test_wrong_name_rejected(self) -> None:
        tampered = (*PHRASE_TABLE[:-1], PhraseEntry(65, "not_a_condition", "words"))
        with pytest.raises(AssertionError, match="does not resolve to bit"):
            check_phrase_invariants(tampered)

    def test_swapped_name_rejected(self) -> None:
        # A REAL vocabulary name on the WRONG bit must also fail the cross-pin.
        tampered = (*PHRASE_TABLE[:-1], PhraseEntry(65, "close_above_ema20", "words"))
        with pytest.raises(AssertionError, match="does not resolve to bit"):
            check_phrase_invariants(tampered)

    def test_blank_plain_rejected(self) -> None:
        tampered = (*PHRASE_TABLE[:-1], PhraseEntry(65, "close_below_supertrend", "   "))
        with pytest.raises(AssertionError, match="blank plain phrase"):
            check_phrase_invariants(tampered)


class TestReasonAssembly:
    def test_single_bit_mask_has_no_joiners(self) -> None:
        reason = reason_for_mask(1 << 13)
        assert reason == ReasonText(
            technical="close_above_pdh", plain="the candle closed above yesterday's high"
        )

    def test_three_bit_mask_joins_in_ascending_bit_order(self) -> None:
        mask = (1 << 13) | (1 << 8) | (1 << 52)
        reason = reason_for_mask(mask)
        # The tombstoned bit 52 reads the describe.condition_display_name
        # display form ("<name> (retired)") on the technical leg too — the
        # ONE suffix authority, identical on both surfaces (TOMB-01).
        assert reason.technical == "near_pivot_r2 ∧ close_above_pdh ∧ close_above_vwap (retired)"
        # G-RETROT-01: a mask carrying the tombstoned bit 52 (an OLD
        # artifact) renders the RETIRED prose — never skipped silently,
        # never live-sounding text.
        assert reason.plain == (
            "price was near resistance level R2"
            "; and the candle closed above yesterday's high"
            "; and price was above the session's volume-weighted average price (VWAP; "
            "retired condition — never fires on current runs)"
        )

    def test_five_bit_mask_counts(self) -> None:
        mask = 0b11111  # bits 0-4
        reason = reason_for_mask(mask)
        assert reason.technical.split(" ∧ ") == [
            "close_above_ema20",
            "close_below_ema20",
            "close_above_ema200",
            "close_below_ema200",
            "ema20_above_ema200",
        ]
        assert reason.plain.count("; and ") == 4

    def test_mask_zero_is_honest_empty(self) -> None:
        assert reason_for_mask(0) == ReasonText(technical="", plain="")

    def test_reserved_headroom_bits_ignored(self) -> None:
        # The describe.py forward-compat contract: bits >= TOTAL_CONDITIONS
        # never raise and never contribute prose.
        reserved_only = 1 << TOTAL_CONDITIONS | 1 << 127
        assert reason_for_mask(reserved_only) == ReasonText(technical="", plain="")
        mixed = reserved_only | (1 << 30)
        assert reason_for_mask(mixed) == reason_for_mask(1 << 30)

    def test_negative_mask_rejected(self) -> None:
        with pytest.raises(ValueError, match="mask must be >= 0"):
            reason_for_mask(-1)

    @pytest.mark.parametrize(
        "mask",
        [
            1,
            0b101,
            (1 << 59) | (1 << 19) | (1 << 44),
            (1 << 60) - 1,
            0x5A5A5A5A5A5A5A5A & ((1 << 60) - 1),
        ],
    )
    def test_technical_names_match_the_common_decoder(self, mask: int) -> None:
        # ONE vocabulary, two surfaces: the technical leg must list exactly
        # the names brutex.vocabulary.describe decodes, in the same order.
        reason = reason_for_mask(mask)
        expected = list(matched_condition_names(mask))
        assert reason.technical.split(" ∧ ") == expected


class TestReasonPayload:
    def test_wire_shape(self) -> None:
        payload = reason_payload((1 << 13) | (1 << 8))
        assert payload == {
            "technical": "near_pivot_r2 ∧ close_above_pdh",
            "plain": (
                "price was near resistance level R2; and the candle closed above yesterday's high"
            ),
        }

    def test_zero_mask_payload_is_empty_strings(self) -> None:
        assert reason_payload(0) == {"technical": "", "plain": ""}
