"""Unit tests for the COMMON two-side ratio primitive (DEC-MOVERS-OPTIONS-PCR-001).

Exhaustive over :mod:`brutex.data.pcr` — the fold law (each numbered clause of
``fold_side_ratio``'s docstring pinned), the fail-loud coverage record, the
``compute_pcr`` composition, the coverage-label/-note honesty vocabulary, and
Hypothesis property-style invariants: ``OK ⟺ ratio is not None``, non-negative
ratios, side-swap reciprocity, sums conservation, and orientation duality (the
DYNAMIC claim, executable). The ratio-value cross-check against an INDEPENDENT
:class:`fractions.Fraction` recompute (the DEC-SURFACE-TRIAL-001 idiom) is the
deterministic ``test_happy_path_matches_independent_fraction_recompute``
(hand-built 117/134 legs — the test that kills an inverted den/num fold); the
two ratio-valued properties additionally pin the PRODUCED Decimals — each
OK-arm ratio against the exact ``Fraction`` of its outcome sums and each
mirrored/dual PAIR's ratio product against 1 — so they are no longer algebraic
identities over the sums alone. Legs are plain tuples — the duck-typed input
contract proven by construction (no adapter class needed).
"""

from __future__ import annotations

import decimal
from decimal import Decimal
from fractions import Fraction

import pytest
from brutex.data.pcr import (
    CALL_SIDE,
    COVERAGE_EMPTY,
    COVERAGE_FULL,
    COVERAGE_PARTIAL,
    PUT_SIDE,
    PcrCorruptionError,
    PcrCoverage,
    PcrResult,
    RatioOutcome,
    RatioStatus,
    compute_pcr,
    fold_side_ratio,
)
from hypothesis import given
from hypothesis import strategies as st

# A leg is a plain (side, oi, volume) tuple — duck-typed input by construction.
Leg = tuple[str, int | None, int | None]


def _side(leg: Leg) -> str:
    return leg[0]


def _oi(leg: Leg) -> int | None:
    return leg[1]


def _volume(leg: Leg) -> int | None:
    return leg[2]


def _fold(legs: list[Leg]) -> RatioOutcome:
    """The PUT/CALL OI fold over tuple legs (the movers orientation)."""
    return fold_side_ratio(
        legs,
        side_of=_side,
        metric=_oi,
        numerator_side=PUT_SIDE,
        denominator_side=CALL_SIDE,
    )


# ── fold_side_ratio — the law, clause by clause ────────────────────────────


class TestFoldSideRatio:
    def test_happy_path_matches_independent_fraction_recompute(self) -> None:
        # Non-terminating quotient (117/134) — the Fraction cross-check proves the
        # Decimal division to the default 28 significant digits.
        legs: list[Leg] = [
            (PUT_SIDE, 117_000, None),
            (CALL_SIDE, 100_000, None),
            (CALL_SIDE, 34_000, None),
        ]
        outcome = _fold(legs)
        assert outcome.status is RatioStatus.OK
        assert outcome.numerator_sum == 117_000
        assert outcome.denominator_sum == 134_000
        assert outcome.missing_legs == 0
        assert outcome.ratio is not None
        expected = Fraction(117_000, 134_000)
        # 28 significant digits (default Decimal context) vs the exact Fraction.
        assert abs(Fraction(str(outcome.ratio)) - expected) < Fraction(1, 10**26)
        assert str(outcome.ratio).startswith("0.8731343283582089552238805970")

    def test_exact_terminating_quotient(self) -> None:
        legs: list[Leg] = [(PUT_SIDE, 46, None), (CALL_SIDE, 40, None)]
        outcome = _fold(legs)
        assert outcome.ratio == Decimal("1.15")

    def test_denominator_of_exactly_one_is_ok_with_the_exact_ratio(self) -> None:
        # The smallest LIVE denominator — the clause 4/5 sentinel BOUNDARY.
        # Pins `denominator_sum > 0` exactly: a `> 1` mutant (a standard
        # mutmut number mutation) silently demotes this chain to
        # zero_denominator/None, which no other deterministic test and no
        # self-consistent property catches (round-7 hostile-review
        # mutation-gap fold; empirically proven surviving mutant).
        outcome = _fold([(PUT_SIDE, 5, None), (CALL_SIDE, 1, None)])
        assert outcome.status is RatioStatus.OK
        assert outcome.ratio == Decimal(5)
        assert (outcome.numerator_sum, outcome.denominator_sum) == (5, 1)

    def test_ratio_context_is_pinned_against_ambient_mutation(self) -> None:
        # Clause 4's determinism claim is STRUCTURAL (round-7 hostile-review
        # fold, the execution/costs.py NFR-DET-001 precedent): the division
        # runs under an explicitly pinned 28-digit ROUND_HALF_EVEN context,
        # so a hostile ambient thread-local context (tiny precision +
        # truncating rounding) cannot skew the wire string. Fails without
        # the localcontext pin ("0.87" under prec=2 ROUND_DOWN).
        legs: list[Leg] = [(PUT_SIDE, 117_000, None), (CALL_SIDE, 134_000, None)]
        with decimal.localcontext(decimal.Context(prec=2, rounding=decimal.ROUND_DOWN)):
            outcome = _fold(legs)
        assert str(outcome.ratio).startswith("0.8731343283582089552238805970")

    def test_zero_denominator_puts_alive_calls_dead(self) -> None:
        # Clause 5: puts > 0, calls == 0 → typed sentinel, NEVER Infinity;
        # the raw sums survive so "puts open, zero calls" stays visible.
        legs: list[Leg] = [(PUT_SIDE, 500, None), (CALL_SIDE, 0, None)]
        outcome = _fold(legs)
        assert outcome.status is RatioStatus.ZERO_DENOMINATOR
        assert outcome.ratio is None
        assert outcome.numerator_sum == 500
        assert outcome.denominator_sum == 0

    def test_zero_over_zero_is_the_same_sentinel(self) -> None:
        legs: list[Leg] = [(PUT_SIDE, 0, None), (CALL_SIDE, 0, None)]
        outcome = _fold(legs)
        assert outcome.status is RatioStatus.ZERO_DENOMINATOR
        assert outcome.ratio is None
        assert (outcome.numerator_sum, outcome.denominator_sum) == (0, 0)

    def test_empty_iterable_is_the_same_sentinel(self) -> None:
        outcome = _fold([])
        assert outcome.status is RatioStatus.ZERO_DENOMINATOR
        assert outcome.ratio is None
        assert (outcome.numerator_sum, outcome.denominator_sum, outcome.missing_legs) == (0, 0, 0)

    def test_none_metric_is_excluded_and_counted_never_zero_filled(self) -> None:
        # Clause 2: the None cell leaves BOTH sums untouched and bumps missing_legs.
        legs: list[Leg] = [
            (PUT_SIDE, 100, None),
            (PUT_SIDE, None, None),
            (CALL_SIDE, 50, None),
            (CALL_SIDE, None, None),
        ]
        outcome = _fold(legs)
        assert outcome.missing_legs == 2
        assert outcome.numerator_sum == 100
        assert outcome.denominator_sum == 50
        assert outcome.ratio == Decimal(2)

    def test_negative_metric_raises_loudly(self) -> None:
        # Clause 3: a negative cell is lake corruption — trip, never sum.
        with pytest.raises(ValueError, match="negative metric -7"):
            _fold([(PUT_SIDE, -7, None)])

    def test_negative_metric_is_the_typed_corruption_error(self) -> None:
        # Round-1 hostile-review fold: the negative-metric trip carries the
        # DISTINCT PcrCorruptionError type — a ValueError SUBCLASS (the §1.1
        # clause-3 fail-loud law byte-unchanged for every existing caller) that
        # a route can single out so lake corruption maps to an honest 500,
        # never the client-error 400 the plain ValueError mapping serves.
        assert issubclass(PcrCorruptionError, ValueError)
        with pytest.raises(PcrCorruptionError, match="negative metric -7"):
            _fold([(PUT_SIDE, -7, None)])
        # The wiring/side trips stay PLAIN ValueError — never mislabelled as
        # lake corruption (they are caller programming errors).
        with pytest.raises(ValueError) as unknown_side:
            _fold([("FUT", 1, None)])
        assert not isinstance(unknown_side.value, PcrCorruptionError)

    def test_unknown_side_raises_loudly(self) -> None:
        # Clause 1: a wrong side is a programming error, never a third bucket.
        with pytest.raises(ValueError, match="unknown side 'FUT'"):
            _fold([("FUT", 100, None)])

    def test_identical_sides_raise_loudly(self) -> None:
        # Fail-loud wiring: numerator == denominator is a caller bug.
        with pytest.raises(ValueError, match="identical"):
            fold_side_ratio(
                [],
                side_of=_side,
                metric=_oi,
                numerator_side=PUT_SIDE,
                denominator_side=PUT_SIDE,
            )


# ── PcrCoverage — fail-loud construction ───────────────────────────────────


class TestPcrCoverage:
    def test_negative_legs_read_raises(self) -> None:
        with pytest.raises(ValueError, match="negative coverage count"):
            PcrCoverage(legs_read=-1, legs_total=5)

    def test_negative_legs_total_raises(self) -> None:
        with pytest.raises(ValueError, match="negative coverage count"):
            PcrCoverage(legs_read=0, legs_total=-3)

    def test_read_exceeding_total_raises(self) -> None:
        # A denominator wiring bug must trip — never fake-full coverage.
        with pytest.raises(ValueError, match="exceeds legs_total"):
            PcrCoverage(legs_read=6, legs_total=5)

    def test_is_partial_true_when_short(self) -> None:
        assert PcrCoverage(legs_read=3, legs_total=5).is_partial is True

    def test_is_partial_false_when_complete(self) -> None:
        assert PcrCoverage(legs_read=5, legs_total=5).is_partial is False


# ── compute_pcr — the two variants via the one fold ────────────────────────


class TestComputePcr:
    def test_outcome_independence_and_accessor_delegation(self) -> None:
        # Zero call-VOLUME with real call-OI: vol_pcr is None while oi_pcr computes
        # (edge 3 — the two outcomes are independent). Accessors delegate exactly.
        legs: list[Leg] = [(PUT_SIDE, 100, 40), (CALL_SIDE, 50, 0)]
        result = compute_pcr(
            legs, side_of=_side, oi_of=_oi, volume_of=_volume, legs_total=len(legs)
        )
        assert result.oi_pcr == Decimal(2)
        assert result.oi.status is RatioStatus.OK
        assert result.vol_pcr is None
        assert result.volume.status is RatioStatus.ZERO_DENOMINATOR
        assert result.put_oi == result.oi.numerator_sum == 100
        assert result.call_oi == result.oi.denominator_sum == 50
        assert result.put_volume == result.volume.numerator_sum == 40
        assert result.call_volume == result.volume.denominator_sum == 0

    def test_legs_total_undercount_raises_via_coverage(self) -> None:
        legs: list[Leg] = [(PUT_SIDE, 1, 1), (CALL_SIDE, 1, 1)]
        with pytest.raises(ValueError, match="exceeds legs_total"):
            compute_pcr(legs, side_of=_side, oi_of=_oi, volume_of=_volume, legs_total=1)


# ── coverage_label + coverage_note — the honesty vocabulary ────────────────


def _result(legs: list[Leg], *, legs_total: int) -> PcrResult:
    return compute_pcr(legs, side_of=_side, oi_of=_oi, volume_of=_volume, legs_total=legs_total)


class TestCoverageLabel:
    def test_full_when_every_leg_read_and_every_cell_present(self) -> None:
        result = _result([(PUT_SIDE, 1, 1), (CALL_SIDE, 1, 1)], legs_total=2)
        assert result.coverage_label == COVERAGE_FULL

    def test_partial_on_a_short_read(self) -> None:
        result = _result([(PUT_SIDE, 1, 1)], legs_total=2)
        assert result.coverage_label == COVERAGE_PARTIAL

    def test_partial_on_a_missing_oi_cell(self) -> None:
        result = _result([(PUT_SIDE, None, 1), (CALL_SIDE, 1, 1)], legs_total=2)
        assert result.coverage_label == COVERAGE_PARTIAL

    def test_partial_on_a_missing_volume_cell(self) -> None:
        result = _result([(PUT_SIDE, 1, None), (CALL_SIDE, 1, 1)], legs_total=2)
        assert result.coverage_label == COVERAGE_PARTIAL

    def test_empty_when_zero_read_of_a_real_chain(self) -> None:
        result = _result([], legs_total=4)
        assert result.coverage_label == COVERAGE_EMPTY

    def test_empty_for_the_vacuous_chain(self) -> None:
        result = _result([], legs_total=0)
        assert result.coverage_label == COVERAGE_EMPTY


class TestCoverageNote:
    def test_none_when_full(self) -> None:
        assert _result([(PUT_SIDE, 1, 1), (CALL_SIDE, 1, 1)], legs_total=2).coverage_note() is None

    def test_short_read_alone(self) -> None:
        assert _result([(PUT_SIDE, 1, 1)], legs_total=3).coverage_note() == "1/3 legs read"

    def test_missing_oi_alone(self) -> None:
        result = _result([(PUT_SIDE, None, 1), (CALL_SIDE, 1, 1)], legs_total=2)
        assert result.coverage_note() == "1 leg(s) missing OI"

    def test_missing_volume_alone(self) -> None:
        result = _result([(PUT_SIDE, 1, None), (CALL_SIDE, 1, 1)], legs_total=2)
        assert result.coverage_note() == "1 leg(s) missing volume"

    def test_combined_in_fixed_order(self) -> None:
        result = _result([(PUT_SIDE, None, None), (CALL_SIDE, 1, 1)], legs_total=3)
        assert (
            result.coverage_note()
            == "2/3 legs read · 1 leg(s) missing OI · 1 leg(s) missing volume"
        )

    def test_vacuous_chain_reads_no_legs(self) -> None:
        assert _result([], legs_total=0).coverage_note() == "no legs"


# ── Hypothesis property invariants ─────────────────────────────────────────

_legs_st = st.lists(
    st.tuples(
        st.sampled_from([PUT_SIDE, CALL_SIDE]),
        st.one_of(st.none(), st.integers(min_value=0, max_value=10**9)),
        st.one_of(st.none(), st.integers(min_value=0, max_value=10**9)),
    ),
    max_size=40,
)

# The fold divides under its EXPLICITLY PINNED 28-significant-digit
# ROUND_HALF_EVEN context (the default context's values, pinned via
# localcontext — round-7 fold), so the produced Decimal sits within half an ulp of the
# exact quotient — a RELATIVE error ≤ 5×10⁻²⁸. 10⁻²⁶ relative is a 20×
# safety margin that still fails on any real defect (e.g. an inverted
# den/num fold, off-by-one sums, a truncating division).
_RATIO_REL_TOL = Fraction(1, 10**26)
# A product of two such Decimals compounds to ≤ ~2×10⁻²⁶ relative; 10⁻²⁵
# absolute (the exact product is 1) keeps the same margin discipline.
_PRODUCT_TOL = Fraction(1, 10**25)


def _assert_ratio_tracks_sums(outcome: RatioOutcome) -> None:
    """Pin the PRODUCED ``outcome.ratio`` Decimal against its exact quotient.

    The sum-equality asserts in the ratio-valued properties are algebraic
    identities over the outcome's OWN sums — they can never fail once the
    sums match, and never inspect ``outcome.ratio``. This helper closes that
    tautology: a fold mutated to return the INVERTED ratio (denominator /
    numerator) fails here for every numerator ≠ denominator draw.
    """
    assert outcome.ratio is not None
    exact = Fraction(outcome.numerator_sum, outcome.denominator_sum)
    assert abs(Fraction(str(outcome.ratio)) - exact) <= exact * _RATIO_REL_TOL


class TestFoldProperties:
    @given(legs=_legs_st)
    def test_status_ok_iff_ratio_not_none_and_ratio_non_negative(self, legs: list[Leg]) -> None:
        outcome = _fold(legs)
        assert (outcome.status is RatioStatus.OK) == (outcome.ratio is not None)
        if outcome.ratio is not None:
            assert outcome.ratio >= 0

    @given(legs=_legs_st)
    def test_sums_conservation(self, legs: list[Leg]) -> None:
        outcome = _fold(legs)
        present = [oi for _side_tok, oi, _vol in legs if oi is not None]
        assert outcome.numerator_sum + outcome.denominator_sum == sum(present)
        assert outcome.missing_legs == len(legs) - len(present)

    @given(legs=_legs_st)
    def test_side_swap_reciprocity(self, legs: list[Leg]) -> None:
        # Relabelling every PE↔CE maps ratio → 1/ratio when both denominators > 0.
        swapped: list[Leg] = [
            (CALL_SIDE if side == PUT_SIDE else PUT_SIDE, oi, vol) for side, oi, vol in legs
        ]
        original = _fold(legs)
        mirrored = _fold(swapped)
        if original.ratio is not None and mirrored.ratio is not None:
            product = Fraction(original.numerator_sum, original.denominator_sum) * Fraction(
                mirrored.numerator_sum, mirrored.denominator_sum
            )
            assert product == 1
            # The promise on the PRODUCED Decimals, not just the sums: each
            # ratio tracks its exact quotient, and ratio × mirrored-ratio ≈ 1.
            _assert_ratio_tracks_sums(original)
            _assert_ratio_tracks_sums(mirrored)
            produced = Fraction(str(original.ratio)) * Fraction(str(mirrored.ratio))
            assert abs(produced - 1) <= _PRODUCT_TOL

    @given(legs=_legs_st)
    def test_orientation_duality(self, legs: list[Leg]) -> None:
        # The DYNAMIC claim, executable: the CALL/PUT orientation is the exact
        # reciprocal of the PUT/CALL fold under the same non-zero condition.
        put_call = _fold(legs)
        call_put = fold_side_ratio(
            legs,
            side_of=_side,
            metric=_oi,
            numerator_side=CALL_SIDE,
            denominator_side=PUT_SIDE,
        )
        assert call_put.numerator_sum == put_call.denominator_sum
        assert call_put.denominator_sum == put_call.numerator_sum
        assert call_put.missing_legs == put_call.missing_legs
        if put_call.ratio is not None and call_put.ratio is not None:
            assert Fraction(put_call.numerator_sum, put_call.denominator_sum) == 1 / Fraction(
                call_put.numerator_sum, call_put.denominator_sum
            )
            # Duality on the PRODUCED Decimals: each orientation's ratio
            # tracks its exact quotient, and their product ≈ 1.
            _assert_ratio_tracks_sums(put_call)
            _assert_ratio_tracks_sums(call_put)
            produced = Fraction(str(put_call.ratio)) * Fraction(str(call_put.ratio))
            assert abs(produced - 1) <= _PRODUCT_TOL
