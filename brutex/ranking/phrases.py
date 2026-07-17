"""The R33 dual-language trade-reason table — FIXED, hand-written, 74 entries.

DEC-SWEEP-RESULTS-DEPTH-001 (WP3). Every trade/combo shows WHY it entered in
BOTH modes — technical (``PDH_CROSS_UP ∧ CLOSE>R2``-style condition names)
and plain English — assembled MECHANICALLY from the mask's fired bits: a
lookup into :data:`PHRASE_TABLE`, never generated text, so hallucination is
structurally impossible (R33 / Golden Rule #1) and assembly is O(k) in the
mask's fired-bit count (bench-pinned).

## The table's provenance + drift guards

One frozen literal tuple of exactly ``TOTAL_CONDITIONS`` (74)
:class:`PhraseEntry` rows, ``bit == index``. Each ``plain`` phrase was
hand-written ONCE from the condition's authoritative semantics — the
``description`` field of ``rust/trading-core/conditions.toml`` (the SINGLE
source of truth for the shared vocabulary) reworded to operator English —
and is locked by a golden digest (``tests/golden/test_ranking_phrases_golden``);
any wording edit is a deliberate golden re-lock (the review-once law).
Import-time :func:`check_phrase_invariants` cross-pins every ``name``
against the generated :data:`brutex.vocabulary.bits.NAME_TO_BIT` (the
``describe._check_group_invariants`` precedent), so a vocabulary
renumber/rename can never silently desynchronise the phrases.

## ONE vocabulary, two surfaces

The technical leg must list exactly the names
:mod:`brutex.vocabulary.describe` decodes, in the same order — so
:func:`reason_for_mask` routes every fired bit's technical token through
the ONE :func:`brutex.vocabulary.describe.condition_display_name`
authority (a RETIRED tombstone bit reads ``"<name> (retired)"``, TOMB-01),
never a second copy of the suffix rule. The TABLE keeps the RAW
vocabulary names (the import-time ``NAME_TO_BIT`` cross-pin demands the
exact generated token); the suffix is applied at assembly time.

Bits at or above ``TOTAL_CONDITIONS`` (reserved u128 headroom) are IGNORED
by :func:`reason_for_mask` — the :mod:`brutex.vocabulary.describe`
forward-compatibility contract, verbatim. A mask with ZERO fired bits reads
as two honest empty strings, never fabricated prose.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from brutex.vocabulary.bits import NAME_TO_BIT, TOTAL_CONDITIONS
from brutex.vocabulary.describe import condition_display_name

__all__ = [
    "PHRASE_TABLE",
    "PhraseEntry",
    "ReasonText",
    "check_phrase_invariants",
    "reason_for_mask",
    "reason_payload",
]


@dataclass(frozen=True, slots=True)
class PhraseEntry:
    """One condition bit's dual-language row.

    ``name`` is the technical token (the generated vocabulary name,
    cross-pinned at import); ``plain`` is the hand-written operator-English
    phrase (lower-case fragment — the assembler joins fragments, it never
    rewrites them).
    """

    bit: int
    name: str
    plain: str


#: The FIXED 74-entry phrase table (R33) — ``bit == index``, import-validated.
PHRASE_TABLE: Final[tuple[PhraseEntry, ...]] = (
    # ── EMA Position (bits 0-5) ──
    PhraseEntry(0, "close_above_ema20", "the candle closed above its 20-period average price"),
    PhraseEntry(1, "close_below_ema20", "the candle closed below its 20-period average price"),
    PhraseEntry(2, "close_above_ema200", "the candle closed above its 200-period average price"),
    PhraseEntry(3, "close_below_ema200", "the candle closed below its 200-period average price"),
    PhraseEntry(
        4,
        "ema20_above_ema200",
        "the short-term average (EMA20) sat above the long-term average (EMA200) — a bullish stack",
    ),
    PhraseEntry(
        5,
        "ema20_below_ema200",
        "the short-term average (EMA20) sat below the long-term average (EMA200) — a bearish stack",
    ),
    # ── Pivot Proximity (bits 6-12) ──
    PhraseEntry(6, "near_pivot_p", "price was near the central pivot level (P)"),
    PhraseEntry(7, "near_pivot_r1", "price was near resistance level R1"),
    PhraseEntry(8, "near_pivot_r2", "price was near resistance level R2"),
    PhraseEntry(9, "near_pivot_r3", "price was near resistance level R3"),
    PhraseEntry(10, "near_pivot_s1", "price was near support level S1"),
    PhraseEntry(11, "near_pivot_s2", "price was near support level S2"),
    PhraseEntry(12, "near_pivot_s3", "price was near support level S3"),
    # ── PDH/PDL (bits 13-18) ──
    PhraseEntry(13, "close_above_pdh", "the candle closed above yesterday's high"),
    PhraseEntry(14, "close_below_pdh", "the candle closed below yesterday's high"),
    PhraseEntry(15, "close_above_pdl", "the candle closed above yesterday's low"),
    PhraseEntry(16, "close_below_pdl", "the candle closed below yesterday's low"),
    PhraseEntry(17, "near_pdh", "price was near yesterday's high"),
    PhraseEntry(18, "near_pdl", "price was near yesterday's low"),
    # ── Fibonacci (bits 19-29) ──
    PhraseEntry(19, "near_fib_0", "price was near the 0% Fibonacci level (the anchor high)"),
    PhraseEntry(20, "near_fib_236", "price was near the 23.6% Fibonacci retracement"),
    PhraseEntry(21, "near_fib_382", "price was near the 38.2% Fibonacci retracement"),
    PhraseEntry(22, "near_fib_50", "price was near the 50% Fibonacci retracement"),
    PhraseEntry(23, "near_fib_618", "price was near the 61.8% Fibonacci retracement"),
    PhraseEntry(24, "near_fib_786", "price was near the 78.6% Fibonacci retracement"),
    PhraseEntry(25, "near_fib_100", "price was near the 100% Fibonacci level (the anchor low)"),
    PhraseEntry(26, "near_fib_1272", "price was near the 127.2% Fibonacci extension"),
    PhraseEntry(27, "near_fib_1618", "price was near the 161.8% Fibonacci extension"),
    PhraseEntry(28, "near_fib_200", "price was near the 200% Fibonacci extension"),
    PhraseEntry(29, "near_fib_2618", "price was near the 261.8% Fibonacci extension"),
    # ── Candle Shape (bits 30-36) ──
    PhraseEntry(30, "bar_bullish", "the candle was bullish (closed above its open)"),
    PhraseEntry(31, "bar_bearish", "the candle was bearish (closed below its open)"),
    PhraseEntry(32, "bar_doji", "the candle was a doji (open and close nearly equal)"),
    PhraseEntry(33, "bar_large_body", "the candle had a large body (a strong one-way move)"),
    PhraseEntry(34, "bar_small_body", "the candle had a small body (an indecisive move)"),
    PhraseEntry(
        35, "long_upper_wick", "the candle had a long upper wick (sellers pushed price back down)"
    ),
    PhraseEntry(
        36, "long_lower_wick", "the candle had a long lower wick (buyers pushed price back up)"
    ),
    # ── Prior Bar Pattern (bits 37-39) ──
    PhraseEntry(37, "prior_n_bullish", "the previous few candles were all bullish"),
    PhraseEntry(38, "prior_n_bearish", "the previous few candles were all bearish"),
    PhraseEntry(
        39, "prior_alternating", "the previous few candles strictly alternated up and down"
    ),
    # ── Day Structure (bits 40-43) ──
    PhraseEntry(40, "close_at_day_high", "the candle closed at the day's high so far"),
    PhraseEntry(41, "close_at_day_low", "the candle closed at the day's low so far"),
    PhraseEntry(
        42, "close_in_upper_third", "the candle closed in the upper third of the day's range"
    ),
    PhraseEntry(
        43, "close_in_lower_third", "the candle closed in the lower third of the day's range"
    ),
    # ── Time of Day (bits 44-47) ──
    PhraseEntry(44, "early_morning", "it was early morning (9:16-10:00 IST)"),
    PhraseEntry(45, "mid_morning", "it was mid-morning (10:00-11:30 IST)"),
    PhraseEntry(46, "midday", "it was midday (11:30-13:00 IST)"),
    PhraseEntry(47, "afternoon", "it was afternoon (13:00-15:00 IST)"),
    # ── Day Type (bits 48-51) ──
    PhraseEntry(
        48, "gap_up_day", "the day opened with a gap up (over 0.5% above the previous close)"
    ),
    PhraseEntry(
        49, "gap_down_day", "the day opened with a gap down (over 0.5% below the previous close)"
    ),
    PhraseEntry(50, "inside_day", "the day's range stayed inside the previous day's range"),
    PhraseEntry(51, "outside_day", "the day's range engulfed the previous day's range"),
    # ── VWAP Position (bits 52-53) — RETIRED tombstones: the positions are
    # held forever but the evaluator never sets them (the VWAP/volume wipe;
    # the spot-only engine has no traded volume). The phrases stay honest
    # should an OLD artifact's mask carry them.
    PhraseEntry(
        52,
        "close_above_vwap",
        "price was above the session's volume-weighted average price (VWAP; retired "
        "condition — never fires on current runs)",
    ),
    PhraseEntry(
        53,
        "close_below_vwap",
        "price was below the session's volume-weighted average price (VWAP; retired "
        "condition — never fires on current runs)",
    ),
    # ── Pivot Proximity Extended (bits 54-55) ──
    PhraseEntry(54, "near_pivot_r5", "price was near resistance level R5"),
    PhraseEntry(55, "near_pivot_s5", "price was near support level S5"),
    # ── Market Structure (bits 56-59) ──
    PhraseEntry(
        56, "bos_bullish", "price broke above the last swing high (bullish break of structure)"
    ),
    PhraseEntry(
        57, "bos_bearish", "price broke below the last swing low (bearish break of structure)"
    ),
    PhraseEntry(
        58,
        "choch_bullish",
        "the trend flipped upward after a falling sequence (bullish change of character)",
    ),
    PhraseEntry(
        59,
        "choch_bearish",
        "the trend flipped downward after a rising sequence (bearish change of character)",
    ),
    # ── Central Pivot Range (bits 60-63) ──
    PhraseEntry(60, "above_cpr_tc", "the candle closed above the Central Pivot Range's top (TC)"),
    PhraseEntry(
        61, "below_cpr_bc", "the candle closed below the Central Pivot Range's bottom (BC)"
    ),
    PhraseEntry(
        62, "inside_cpr", "the candle closed inside the Central Pivot Range (between BC and TC)"
    ),
    PhraseEntry(
        63,
        "narrow_cpr_day",
        "the day's Central Pivot Range was narrow (a tight coil, often before a big move)",
    ),
    # ── Supertrend (bits 64-65) — declared layout-only (implemented=false):
    # the math lands in a later PR, so these bits never fire today. The
    # phrases exist because the table must cover every declared bit.
    PhraseEntry(
        64,
        "close_above_supertrend",
        "the candle closed above the SuperTrend line (declared condition — math "
        "not yet active, never fires on current runs)",
    ),
    PhraseEntry(
        65,
        "close_below_supertrend",
        "the candle closed below the SuperTrend line (declared condition — math "
        "not yet active, never fires on current runs)",
    ),
    # ── Gap Placeholders (bits 66-68) — DEC-FIB-TEMPLATE-EXPRESS-001 reserved
    # positions for the gap-midpoint conditions (#1901); dropped at the
    # re-stack onto the gap branch, NEVER implemented here. The phrases exist
    # because the table must cover every declared bit.
    PhraseEntry(
        66,
        "placeholder_gap_66",
        "reserved placeholder 66 for a gap-midpoint condition (never fires)",
    ),
    PhraseEntry(
        67,
        "placeholder_gap_67",
        "reserved placeholder 67 for a gap-midpoint condition (never fires)",
    ),
    PhraseEntry(
        68,
        "placeholder_gap_68",
        "reserved placeholder 68 for a gap-midpoint condition (never fires)",
    ),
    # ── Fibonacci Extended (bits 69-71) — DEC-FIB-TEMPLATE-EXPRESS-001 ──
    PhraseEntry(
        69,
        "near_fib_bull_236",
        "price was near the bullish 23.6% Fibonacci retracement (measured up "
        "from the previous day's low)",
    ),
    PhraseEntry(
        70,
        "near_fib_bull_786",
        "price was near the bullish 78.6% Fibonacci retracement (measured up "
        "from the previous day's low)",
    ),
    PhraseEntry(
        71,
        "near_fib_424",
        "price was near a 424% Fibonacci extension (the far breakout target, "
        "above or below the previous day's range)",
    ),
    # ── Swing Proximity (bits 72-73) — DEC-FIB-TEMPLATE-EXPRESS-001 ──
    PhraseEntry(
        72,
        "near_swing_high",
        "price was back at the last confirmed swing high (a retest of the structure level)",
    ),
    PhraseEntry(
        73,
        "near_swing_low",
        "price was back at the last confirmed swing low (a retest of the structure level)",
    ),
)


@dataclass(frozen=True, slots=True)
class ReasonText:
    """One mask's assembled dual-language reason (both honest-empty for mask 0)."""

    technical: str
    plain: str


def check_phrase_invariants(
    table: tuple[PhraseEntry, ...] = PHRASE_TABLE, total: int = TOTAL_CONDITIONS
) -> None:
    """Validate the table covers bits ``0..total-1`` with the REAL vocab names.

    Parameterised so the ``raise`` branches are reachable from tests with a
    tampered table (the ``describe._check_group_invariants`` precedent); the
    module calls it ONCE at import over the shipped table.

    Raises
    ------
    AssertionError
        If the table length drifts from *total*, an entry's ``bit`` is not
        its index, a ``name`` is not the vocabulary's name for that bit, or
        a ``plain`` phrase is blank.
    """
    if len(table) != total:
        raise AssertionError(
            f"PHRASE_TABLE must carry exactly {total} entries (one per condition bit), "
            f"got {len(table)} — the vocabulary grew/shrank without a phrase-table update"
        )
    for index, entry in enumerate(table):
        if entry.bit != index:
            raise AssertionError(
                f"PHRASE_TABLE[{index}] carries bit {entry.bit} — entries must be "
                "index-ordered (bit == index) so assembly is a direct lookup"
            )
        if NAME_TO_BIT.get(entry.name) != entry.bit:
            raise AssertionError(
                f"PHRASE_TABLE[{index}] name {entry.name!r} does not resolve to bit "
                f"{entry.bit} in the generated vocabulary (NAME_TO_BIT) — the phrase "
                "table drifted from conditions.toml"
            )
        if not entry.plain.strip():
            raise AssertionError(
                f"PHRASE_TABLE[{index}] ({entry.name}) has a blank plain phrase — "
                "every condition must read as honest operator English"
            )


check_phrase_invariants()

#: Mask of the real condition bits — fired bits above it are reserved headroom.
_CONDITION_MASK: Final[int] = (1 << TOTAL_CONDITIONS) - 1


def reason_for_mask(mask: int) -> ReasonText:
    """Assemble the dual-language reason for *mask* — pure lookup, O(k) fired bits.

    ``technical`` joins the vocabulary DISPLAY names with ``" ∧ "`` in
    ascending bit order — each name via the ONE
    :func:`brutex.vocabulary.describe.condition_display_name` authority, so
    a RETIRED tombstone bit (e.g. the VWAP bits 52/53 an OLD artifact's mask
    may carry) reads ``"<name> (retired)"`` exactly as
    ``describe.matched_condition_names`` decodes it (one vocabulary, two
    surfaces); ``plain`` joins the hand-written phrases with ``"; and "``
    (the design B7 assembly law). Bits ``>= TOTAL_CONDITIONS`` are ignored
    (reserved headroom — the ``describe`` forward-compat contract); a mask
    with zero fired condition bits returns two empty strings.

    Raises
    ------
    ValueError
        On a negative mask (not a valid u128 bit pattern).
    """
    if mask < 0:
        raise ValueError(f"reason_for_mask: mask must be >= 0, got {mask}")
    names: list[str] = []
    plains: list[str] = []
    fired = mask & _CONDITION_MASK
    while fired:
        bit = (fired & -fired).bit_length() - 1
        entry = PHRASE_TABLE[bit]
        names.append(condition_display_name(bit))
        plains.append(entry.plain)
        fired &= fired - 1
    return ReasonText(technical=" ∧ ".join(names), plain="; and ".join(plains))


def reason_payload(mask: int) -> dict[str, str]:
    """The :func:`reason_for_mask` result as the top-100 ``reasons`` wire shape.

    The exact ``{"technical": …, "plain": …}`` mapping the
    ``top100_artifact.ReasonProvider`` seam expects — the WP6 CLI adapter
    passes THIS function as the production ``reason_provider``.
    """
    reason = reason_for_mask(mask)
    return {"technical": reason.technical, "plain": reason.plain}
