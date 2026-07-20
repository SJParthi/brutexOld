"""Dual-layer (Simple + Pro) human-readable decoder for a winning combo.

A brute-force winner is a u128 condition mask (each set bit ``0..65`` is one
atomic condition from the shared vocabulary :mod:`brutex.vocabulary.bits`).
This module turns that opaque number into TWO plain-English readings of the
SAME rule so the operator can show a winner to anyone:

* a **Simple** sentence in everyday words (no jargon) — for a beginner;
* a **Pro** sentence in precise trader terms (``close>VWAP``, ``near R1``,
  ``Fib-50``, ``CHoCH↑``) — for an experienced trader.

Read-side ONLY. No bar evaluation, no I/O, no engine/vocab mutation. The
decoder keys every label off the canonical condition NAMES in
:data:`brutex.vocabulary.bits.NAME_TO_BIT`, so it stays in lock-step with the
manifest — a coverage test asserts every one of the 66 names has BOTH a Pro
and a Simple label, so a future condition added to the manifest without a
label fails the build (no silent gaps; CLAUDE.md §11 COMMON).

O(1) per condition: a bounded scan of the at-most-66 condition bits,
independent of bar count (CLAUDE.md §2 Rule 5).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from brutex.vocabulary.bits import BIT_TO_NAME, NAME_TO_BIT, TOTAL_CONDITIONS

# ─────────────────────────── Dual-label dictionary ───────────────────────────
#
# Every one of the 66 vocabulary condition names maps to (pro_label,
# simple_label). The Pro label is the precise trader phrase; the Simple label
# is the everyday-words phrase. A coverage test asserts this dict's keys are
# EXACTLY ``NAME_TO_BIT.keys()`` — so this table can never silently drift from
# the manifest.
_CONDITION_LABELS: Final[dict[str, tuple[str, str]]] = {
    # ── EMA Position ──
    "close_above_ema20": ("close>EMA20", "price above the short trend line"),
    "close_below_ema20": ("close<EMA20", "price below the short trend line"),
    "close_above_ema200": ("close>EMA200", "price above the long trend line"),
    "close_below_ema200": ("close<EMA200", "price below the long trend line"),
    "ema20_above_ema200": ("EMA20>EMA200", "trend is up"),
    "ema20_below_ema200": ("EMA20<EMA200", "trend is down"),
    # ── Pivot Proximity ──
    "near_pivot_p": ("near pivot P", "near the pivot line"),
    "near_pivot_r1": ("near R1", "at a ceiling (resistance)"),
    "near_pivot_r2": ("near R2", "at a higher ceiling (resistance)"),
    "near_pivot_r3": ("near R3", "at a far ceiling (resistance)"),
    "near_pivot_s1": ("near S1", "at a floor (support)"),
    "near_pivot_s2": ("near S2", "at a lower floor (support)"),
    "near_pivot_s3": ("near S3", "at a far floor (support)"),
    # ── PDH/PDL ──
    "close_above_pdh": ("close>PDH", "above yesterday's high"),
    "close_below_pdh": ("close<PDH", "below yesterday's high"),
    "close_above_pdl": ("close>PDL", "above yesterday's low"),
    "close_below_pdl": ("close<PDL", "below yesterday's low"),
    "near_pdh": ("near PDH", "near yesterday's high"),
    "near_pdl": ("near PDL", "near yesterday's low"),
    # ── Fibonacci ──
    "near_fib_0": ("Fib 0%", "at the swing start zone"),
    "near_fib_236": ("Fib 23.6%", "at the 23.6% bounce zone"),
    "near_fib_382": ("Fib 38.2%", "at the 38.2% bounce zone"),
    "near_fib_50": ("Fib 50%", "at the halfway bounce zone"),
    "near_fib_618": ("Fib 61.8%", "at the deep retrace zone"),
    "near_fib_786": ("Fib 78.6%", "at the deep retrace zone"),
    "near_fib_100": ("Fib 100%", "at the swing end zone"),
    "near_fib_1272": ("Fib 127.2%", "at the extension target"),
    "near_fib_1618": ("Fib 161.8%", "at the extension target"),
    "near_fib_200": ("Fib 200%", "at the extension target"),
    "near_fib_2618": ("Fib 261.8%", "at the extension target"),
    # ── Candle Shape ──
    "bar_bullish": ("bullish bar", "green candle"),
    "bar_bearish": ("bearish bar", "red candle"),
    "bar_doji": ("doji bar", "indecision candle"),
    "bar_large_body": ("large body", "strong push"),
    "bar_small_body": ("small body", "weak/small candle"),
    "long_upper_wick": ("long upper wick", "rejection from above (long wick)"),
    "long_lower_wick": ("long lower wick", "rejection from below (long wick)"),
    # ── Prior Bar Pattern ──
    "prior_n_bullish": ("prior bars bullish", "last few candles all up"),
    "prior_n_bearish": ("prior bars bearish", "last few candles all down"),
    "prior_alternating": ("prior bars alternating", "last few candles choppy"),
    # ── Day Structure ──
    "close_at_day_high": ("close at day high", "closed at the day's high"),
    "close_at_day_low": ("close at day low", "closed at the day's low"),
    "close_in_upper_third": (
        "close in upper third",
        "closed in the top third of the day",
    ),
    "close_in_lower_third": (
        "close in lower third",
        "closed in the bottom third of the day",
    ),
    # ── Time of Day ──
    "early_morning": ("early morning", "first hour"),
    "mid_morning": ("mid morning", "late morning"),
    "midday": ("midday", "midday"),
    "afternoon": ("afternoon", "afternoon"),
    # ── Day Type ──
    "gap_up_day": ("gap-up day", "gapped up at open"),
    "gap_down_day": ("gap-down day", "gapped down at open"),
    "inside_day": ("inside day", "quiet day (inside yesterday's range)"),
    "outside_day": ("outside day", "big day (engulfs yesterday)"),
    # ── VWAP Position (RETIRED tombstones — bits 52/53 never fire on
    # current runs; labels kept honest for OLD artifacts' masks) ──
    "close_above_vwap": (
        "close>VWAP (retired)",
        "price beats the day's average price (retired condition)",
    ),
    "close_below_vwap": (
        "close<VWAP (retired)",
        "price loses to the day's average price (retired condition)",
    ),
    # ── Pivot Proximity Extended ──
    "near_pivot_r5": ("near R5", "at a far ceiling (resistance)"),
    "near_pivot_s5": ("near S5", "at a far floor (support)"),
    # ── Market Structure ──
    "bos_bullish": ("BOS↑", "strong break (buyers took control)"),
    "bos_bearish": ("BOS↓", "strong break (sellers took control)"),
    "choch_bullish": ("CHoCH↑", "trend just flipped up"),
    "choch_bearish": ("CHoCH↓", "trend just flipped down"),
    # ── Central Pivot Range ──
    "above_cpr_tc": ("close>CPR-TC", "above the pivot-range ceiling"),
    "below_cpr_bc": ("close<CPR-BC", "below the pivot-range floor"),
    "inside_cpr": ("inside CPR", "inside the pivot range"),
    "narrow_cpr_day": ("narrow CPR day", "tight coil day (narrow pivot range)"),
    # ── Supertrend (declared layout-only — bits 64/65 never fire today;
    # labels kept so the exact-keys coverage law holds) ──
    "close_above_supertrend": (
        "close>SuperTrend (inactive)",
        "price above the SuperTrend line (not yet active)",
    ),
    "close_below_supertrend": (
        "close<SuperTrend (inactive)",
        "price below the SuperTrend line (not yet active)",
    ),
}

#: Readable grouping order for composing a sentence: trend → level →
#: candle/structure → time. Each entry is (group-label, set-of-condition-names).
#: A condition not in any of these groups would be dropped from the sentence,
#: so a coverage test asserts these four sets partition all 66 names exactly.
_TREND_NAMES: Final[frozenset[str]] = frozenset(
    {
        "close_above_ema20",
        "close_below_ema20",
        "close_above_ema200",
        "close_below_ema200",
        "ema20_above_ema200",
        "ema20_below_ema200",
        "close_above_vwap",
        "close_below_vwap",
        "close_above_supertrend",
        "close_below_supertrend",
    }
)
_LEVEL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "near_pivot_p",
        "near_pivot_r1",
        "near_pivot_r2",
        "near_pivot_r3",
        "near_pivot_r5",
        "near_pivot_s1",
        "near_pivot_s2",
        "near_pivot_s3",
        "near_pivot_s5",
        "close_above_pdh",
        "close_below_pdh",
        "close_above_pdl",
        "close_below_pdl",
        "near_pdh",
        "near_pdl",
        "near_fib_0",
        "near_fib_236",
        "near_fib_382",
        "near_fib_50",
        "near_fib_618",
        "near_fib_786",
        "near_fib_100",
        "near_fib_1272",
        "near_fib_1618",
        "near_fib_200",
        "near_fib_2618",
        "above_cpr_tc",
        "below_cpr_bc",
        "inside_cpr",
        "narrow_cpr_day",
    }
)
_STRUCTURE_NAMES: Final[frozenset[str]] = frozenset(
    {
        "bar_bullish",
        "bar_bearish",
        "bar_doji",
        "bar_large_body",
        "bar_small_body",
        "long_upper_wick",
        "long_lower_wick",
        "prior_n_bullish",
        "prior_n_bearish",
        "prior_alternating",
        "close_at_day_high",
        "close_at_day_low",
        "close_in_upper_third",
        "close_in_lower_third",
        "gap_up_day",
        "gap_down_day",
        "inside_day",
        "outside_day",
        "bos_bullish",
        "bos_bearish",
        "choch_bullish",
        "choch_bearish",
    }
)
_TIME_NAMES: Final[frozenset[str]] = frozenset(
    {
        "early_morning",
        "mid_morning",
        "midday",
        "afternoon",
    }
)

#: The composition order — trend → level → candle/structure → time.
_GROUP_ORDER: Final[tuple[frozenset[str], ...]] = (
    _TREND_NAMES,
    _LEVEL_NAMES,
    _STRUCTURE_NAMES,
    _TIME_NAMES,
)


@dataclass(frozen=True, slots=True)
class ConditionLabel:
    """One matched condition with its bit, technical name, and both labels.

    Attributes
    ----------
    bit
        The condition's bit position (``0..65``).
    technical_name
        The canonical snake_case condition name (the ``NAME_TO_BIT`` key).
    pro_label
        The precise trader phrase (e.g. ``"close>VWAP"``).
    simple_label
        The everyday-words phrase (e.g. ``"price beats the day's average
        price"``).
    """

    bit: int
    technical_name: str
    pro_label: str
    simple_label: str


@dataclass(frozen=True, slots=True)
class ComboExplanation:
    """A dual-layer human-readable explanation of a winning combo.

    Attributes
    ----------
    conditions
        One :class:`ConditionLabel` per set bit, in ascending bit order.
    simple_sentence
        The beginner plain-English reading of the whole rule.
    pro_sentence
        The trader-precise reading of the whole rule.
    regime_label
        The derived market regime (uptrend / downtrend / reversal / breakout /
        sideways / volatile / mixed), from the set bits.
    direction_label
        ``"Buy"`` or ``"Sell"``.
    """

    conditions: tuple[ConditionLabel, ...]
    simple_sentence: str
    pro_sentence: str
    regime_label: str
    direction_label: str


def _direction_label(direction: str) -> str:
    """Map a raw direction token to ``"Buy"`` / ``"Sell"``.

    ``"long"`` / ``"buy"`` (any case) → ``"Buy"``; everything else → ``"Sell"``
    (the brute-force engine emits exactly ``long`` / ``short``).
    """
    return "Buy" if direction.strip().lower() in {"long", "buy"} else "Sell"


def _derive_regime(names: frozenset[str]) -> str:
    """Derive the market regime from the set condition names (read-side rules).

    Combination rules (NO new bits) — precedence top-to-bottom:

    * **reversal** — a CHoCH bit set that is NOT confirming an existing EMA
      trend (a true trend flip). A CHoCH *concordant* with the EMA trend
      (``choch_bullish`` with ``ema20_above_ema200``, or ``choch_bearish``
      with ``ema20_below_ema200``) is a continuation, so it falls through to
      the uptrend/downtrend rule instead.
    * **breakout** — any BOS bit set (structure break).
    * **volatile** — a large body OR an outside day.
    * **sideways** — an inside day AND a small-body/doji/alternating bar.
    * **uptrend** — ``ema20_above_ema200`` set (incl. a confirming CHoCH↑).
    * **downtrend** — ``ema20_below_ema200`` set (incl. a confirming CHoCH↓).
    * **mixed** — none of the above (or conflicting EMA bits).
    """
    up = "ema20_above_ema200" in names
    down = "ema20_below_ema200" in names
    choch_up = "choch_bullish" in names
    choch_down = "choch_bearish" in names
    # A CHoCH that confirms the prevailing EMA trend is a continuation, not a
    # reversal. Treat the CHoCH as a reversal only when it is NOT concordant.
    concordant = (choch_up and up and not down) or (choch_down and down and not up)
    if (choch_up or choch_down) and not concordant:
        return "reversal"
    if {"bos_bullish", "bos_bearish"} & names:
        return "breakout"
    if {"bar_large_body", "outside_day"} & names:
        return "volatile"
    if "inside_day" in names and ({"bar_small_body", "bar_doji", "prior_alternating"} & names):
        return "sideways"
    if up and not down:
        return "uptrend"
    if down and not up:
        return "downtrend"
    return "mixed"


def _ordered_simple_phrases(names: list[str]) -> list[str]:
    """Return the simple labels in readable group order (trend→level→…→time)."""
    return [_CONDITION_LABELS[name][1] for group in _GROUP_ORDER for name in names if name in group]


def _ordered_pro_phrases(names: list[str]) -> list[str]:
    """Return the pro labels in readable group order (trend→level→…→time)."""
    return [_CONDITION_LABELS[name][0] for group in _GROUP_ORDER for name in names if name in group]


def _join_phrases(phrases: list[str]) -> str:
    """Join phrases with commas, an ``and`` before the last (Oxford-free)."""
    if len(phrases) == 1:
        return phrases[0]
    return ", ".join(phrases[:-1]) + " and " + phrases[-1]


def decode_combo(condition_mask: int, direction: str, timeframe: str) -> ComboExplanation:
    """Decode a winning combo into a dual-layer (Simple + Pro) explanation.

    Parameters
    ----------
    condition_mask
        The u128 strategy mask. Bits ``0..65`` are the declared conditions; bits
        ``>= TOTAL_CONDITIONS`` (forward-compat headroom) are ignored.
    direction
        The trade direction (``"long"`` / ``"short"`` / ``"buy"`` / ``"sell"``,
        any case).
    timeframe
        The timeframe label to surface in the sentence (e.g. ``"15m"``).

    Returns
    -------
    ComboExplanation
        The per-condition labels plus the composed Simple and Pro sentences,
        the derived regime, and the Buy/Sell direction label. For the empty
        mask (``0``) the sentences are an honest direction-only statement.
    """
    direction_label = _direction_label(direction)
    names = [BIT_TO_NAME[bit] for bit in range(TOTAL_CONDITIONS) if (condition_mask >> bit) & 1]
    conditions = tuple(
        ConditionLabel(
            bit=NAME_TO_BIT[name],
            technical_name=name,
            pro_label=_CONDITION_LABELS[name][0],
            simple_label=_CONDITION_LABELS[name][1],
        )
        for name in names
    )
    regime = _derive_regime(frozenset(names))

    if not names:
        empty = f"{direction_label} on {timeframe} (no conditions)."
        return ComboExplanation(
            conditions=conditions,
            simple_sentence=empty,
            pro_sentence=empty,
            regime_label=regime,
            direction_label=direction_label,
        )

    simple_body = _join_phrases(_ordered_simple_phrases(names))
    pro_body = _join_phrases(_ordered_pro_phrases(names))
    simple_sentence = f"{direction_label} on {timeframe} when {simple_body} ({regime})."
    pro_sentence = f"{direction_label} {timeframe} [{regime}]: {pro_body}."
    return ComboExplanation(
        conditions=conditions,
        simple_sentence=simple_sentence,
        pro_sentence=pro_sentence,
        regime_label=regime,
        direction_label=direction_label,
    )


__all__ = [
    "ComboExplanation",
    "ConditionLabel",
    "decode_combo",
]
