"""Property invariants for the shared condition vocabulary bit/decode/structure layer.

Complements ``tests/property/test_conflict_pairs_exact.py`` (the Stage-1B
exactness proof over real bars) with the *structural* algebra of the bit table,
the decode partition, the conflict-pair symmetry, and a LOOK-AHEAD structural
property of :func:`brutex.vocabulary.evaluator.evaluate_atoms`:

* **Bit round-trip** — ``BIT_TO_NAME[NAME_TO_BIT[name]] == name`` and the inverse
  over all 52 conditions (the two tables are exact inverses, no collision).
* **Describe partition exactness** — for any mask, ``describe_mask`` flattens to
  exactly ``matched_condition_names`` (same names, same order, no dup/drop), and
  every grouped name belongs to its family's bit range.
* **Conflict symmetry / well-formedness** — every conflict pair is ``(a, b)`` with
  ``a < b`` over valid condition bits, and ``has_conflict`` is order-invariant
  (setting the two bits in either order is detected identically).
* **Look-ahead structural invariant (Property P-06 family)** — ``evaluate_atoms``
  is a pure function of the single bar-N ``IndicatorSnapshot``: the SAME snapshot
  yields the SAME mask regardless of any external mutable state evaluated in
  between (no bar N+1 access, no hidden cross-bar carry). This is the
  vocabulary-level statement of CLAUDE.md §4 "no look-ahead".

Honest envelope (Golden Rule #10): the look-ahead property here is a STRUCTURAL
purity check at the evaluator's input boundary (same snapshot ⇒ same output, no
external-state dependence) — it is NOT the index-based ``LookAheadBarrier`` accessor
(``brutex/feed/lookahead.py``), which guards the feed that BUILDS the snapshot.
Together they cover both halves: this proves the evaluator reads only bar N's
snapshot; the barrier proves the snapshot itself was built only from bars 0..N.
"""

from __future__ import annotations

import random
from datetime import datetime
from zoneinfo import ZoneInfo

from brutex.indicators.fib_levels import compute_fib_levels
from brutex.indicators.pivots import compute_pivot_levels
from brutex.vocabulary import (
    ALL_CONDITION_NAMES,
    BIT_TO_NAME,
    CONFLICT_PAIRS,
    GROUP_RANGES,
    NAME_TO_BIT,
    TOTAL_CONDITIONS,
    IndicatorSnapshot,
    PriorBarHistory,
    ProximityParams,
    describe_mask,
    evaluate_atoms,
    has_conflict,
    matched_condition_names,
)
from hypothesis import given, settings
from hypothesis import strategies as st

_IST = ZoneInfo("Asia/Kolkata")
_PROX = ProximityParams(pivot_proximity_bps=10, pdh_pdl_proximity_bps=10, fib_proximity_bps=10)

#: A mask spanning the 52 used bits AND the reserved 52..63 headroom, so the
#: decode/round-trip properties are exercised against forward-compatible masks too.
_mask_strategy = st.integers(min_value=0, max_value=(1 << 64) - 1)


class TestBitRoundTrip:
    """The two name↔bit tables are exact inverses over all 52 conditions."""

    def test_name_to_bit_round_trips_for_every_condition(self) -> None:
        for name, bit in NAME_TO_BIT.items():
            assert BIT_TO_NAME[bit] == name

    def test_bit_to_name_round_trips_for_every_bit(self) -> None:
        for bit, name in BIT_TO_NAME.items():
            assert NAME_TO_BIT[name] == bit

    def test_tables_are_total_and_collision_free(self) -> None:
        assert len(NAME_TO_BIT) == TOTAL_CONDITIONS
        assert set(NAME_TO_BIT.values()) == set(range(TOTAL_CONDITIONS))
        assert set(ALL_CONDITION_NAMES) == set(NAME_TO_BIT)
        # ALL_CONDITION_NAMES preserves NAME_TO_BIT insertion order (the stable
        # serialization contract downstream tooling relies on).
        assert tuple(NAME_TO_BIT) == ALL_CONDITION_NAMES


class TestDescribePartition:
    """``describe_mask`` partitions exactly into ``matched_condition_names``."""

    @given(mask=_mask_strategy)
    @settings(max_examples=500)
    def test_describe_flattens_to_matched_names(self, mask: int) -> None:
        flat = [name for grp in describe_mask(mask) for name in grp.conditions]
        # Same names, same order, no duplication or drop — an exact partition.
        assert flat == matched_condition_names(mask)

    @given(mask=_mask_strategy)
    @settings(max_examples=500)
    def test_every_grouped_name_is_in_its_family_range(self, mask: int) -> None:
        # describe_mask renders DISPLAY names (a tombstoned bit reads
        # "(retired)" — TOMB-01), so the family table is derived through the
        # SAME rendering authority.
        from brutex.vocabulary.describe import condition_display_name

        family_of = {
            name: fam
            for fam, rng in GROUP_RANGES
            for name in (condition_display_name(b) for b in rng)
        }
        for grp in describe_mask(mask):
            for name in grp.conditions:
                assert family_of[name] == grp.group

    @given(mask=_mask_strategy)
    @settings(max_examples=300)
    def test_reserved_bits_never_appear(self, mask: int) -> None:
        # Bits 52..63 are headroom — a forward-compatible mask must decode to the
        # SAME names as the same mask with those bits cleared.
        used_only = mask & ((1 << TOTAL_CONDITIONS) - 1)
        assert matched_condition_names(mask) == matched_condition_names(used_only)
        assert describe_mask(mask) == describe_mask(used_only)


class TestConflictSymmetry:
    """Conflict pairs are well-formed and ``has_conflict`` is order-invariant."""

    def test_every_pair_is_ordered_and_valid(self) -> None:
        for a, b in CONFLICT_PAIRS:
            assert 0 <= a < b < TOTAL_CONDITIONS

    @given(pair=st.sampled_from(sorted(CONFLICT_PAIRS)))
    def test_has_conflict_is_order_invariant(self, pair: tuple[int, int]) -> None:
        a, b = pair
        mask_ab = (1 << a) | (1 << b)
        mask_ba = (1 << b) | (1 << a)  # identical value, built in the other order
        assert has_conflict(mask_ab) is True
        assert has_conflict(mask_ba) is True
        assert has_conflict(1 << a) is False  # one half alone never conflicts
        assert has_conflict(1 << b) is False


def _snapshot(rnd: random.Random) -> tuple[IndicatorSnapshot, PriorBarHistory]:
    """A randomized valid bar-N snapshot from a seeded RNG."""
    center = rnd.randint(15_000_00, 50_000_00)
    half = rnd.randint(1, 4_000)
    lo, hi = center - half, center + half
    o, c = rnd.randint(lo, hi), rnd.randint(lo, hi)
    pd = rnd.randint(1, 5_000)
    pdl, pdh = center - pd, center + pd
    pdc = rnd.randint(pdl, pdh)
    ts = datetime(2026, 5, 13, 11, 0, tzinfo=_IST)
    day = ts.date()
    snap = IndicatorSnapshot(
        timestamp_ist=ts,
        open_paisa=o,
        high_paisa=hi,
        low_paisa=lo,
        close_paisa=c,
        ema20_paisa=rnd.randint(lo, hi),
        ema200_paisa=rnd.randint(lo, hi),
        pivots=compute_pivot_levels(day, pdh, pdl, pdc),
        fib=compute_fib_levels(day, pdh, pdl),
        day_high_paisa=hi,
        day_low_paisa=lo,
        open_of_day_paisa=center,
    )
    return snap, PriorBarHistory(prior_n=3, flags=[True, False, True])


class TestLookAheadStructural:
    """``evaluate_atoms`` depends ONLY on the bar-N snapshot (no external state)."""

    @given(seed=st.integers(min_value=0, max_value=2**31 - 1))
    @settings(max_examples=300)
    def test_same_snapshot_same_mask_regardless_of_intervening_evaluations(self, seed: int) -> None:
        rnd = random.Random(seed)
        snap, prior = _snapshot(rnd)
        first = evaluate_atoms(snap, _PROX, prior)
        # Evaluate a stream of UNRELATED bars in between — if any cross-bar state
        # leaked, the re-evaluation of the SAME snapshot would diverge.
        for _ in range(8):
            other_snap, other_prior = _snapshot(rnd)
            evaluate_atoms(other_snap, _PROX, other_prior)
        again = evaluate_atoms(snap, _PROX, prior)
        assert again == first, "evaluate_atoms is not a pure function of bar N's snapshot"

    @given(seed=st.integers(min_value=0, max_value=2**31 - 1))
    @settings(max_examples=200)
    def test_mask_is_a_function_of_inputs_only(self, seed: int) -> None:
        rnd = random.Random(seed)
        snap, prior = _snapshot(rnd)
        # A fresh PriorBarHistory with identical contents must give an identical
        # mask (the prior window is read by value, not by identity).
        clone = PriorBarHistory(prior_n=prior.prior_n, flags=list(prior.flags))
        assert evaluate_atoms(snap, _PROX, prior) == evaluate_atoms(snap, _PROX, clone)
