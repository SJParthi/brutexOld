"""Unit tests for :mod:`brutex.ranking.temporal` (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001).

The temporal-robustness year gate's PURE predicate core: exact-Fraction P1/P2
doors, the data-grounded judged-year set, the whole-gate abstention, the
gate|observe mode fold, and the serve-time diagnostic twin. Every boundary here
is an exact-arithmetic mutant-killer (a ``>=`` → ``>`` flip on either door dies
on the AT-threshold cases); the hypothesis-randomized laws live in
``tests/property/test_temporal_gate_properties.py``.
"""

from __future__ import annotations

import fractions
from typing import Any

import pytest
from brutex.ranking import temporal as temporal_mod
from brutex.ranking.temporal import (
    MODE_GATE,
    MODE_OBSERVE,
    YEAR_GATE_PROFITABLE_YEARS,
    YEAR_GATE_REASON_ORDER,
    YEAR_GATE_SINGLE_YEAR_SHARE,
    TemporalGateInput,
    TemporalGateStats,
    TemporalRobustnessConfig,
    TemporalTriple,
    apply_temporal_gate,
    judged_years,
    positive_mass_share,
    temporal_gate_passes,
    temporal_gate_positions,
    temporal_gate_reasons,
    triple_from_year_pnls,
    year_gate_diagnostic,
)

_CFG = TemporalRobustnessConfig()


def _triple(profitable: int, max_pos: int, sum_pos: int) -> TemporalTriple:
    return TemporalTriple(
        profitable_judged_years=profitable,
        max_pos_year_pnl_paisa=max_pos,
        sum_pos_year_pnl_paisa=sum_pos,
    )


class TestVocabulary:
    """The temporal reason vocabulary is OURS, stable, ordered, duplicate-free."""

    def test_reason_order_is_the_exact_wire_vocabulary(self) -> None:
        assert YEAR_GATE_REASON_ORDER == (
            "year_gate_profitable_years",
            "year_gate_single_year_share",
        )

    def test_tokens_match_their_constants(self) -> None:
        assert YEAR_GATE_PROFITABLE_YEARS == "year_gate_profitable_years"
        assert YEAR_GATE_SINGLE_YEAR_SHARE == "year_gate_single_year_share"

    def test_no_duplicate_tokens(self) -> None:
        assert len(set(YEAR_GATE_REASON_ORDER)) == len(YEAR_GATE_REASON_ORDER)

    def test_separate_from_the_frozen_hard_door_vocabulary(self) -> None:
        """GATE_REASON_ORDER is audit-1-owned and byte-untouched — no overlap."""
        from brutex.ranking import GATE_REASON_ORDER

        assert not set(YEAR_GATE_REASON_ORDER) & set(GATE_REASON_ORDER)

    def test_modes(self) -> None:
        assert MODE_GATE == "gate"
        assert MODE_OBSERVE == "observe"


class TestConfigValidation:
    def test_defaults_are_the_design_values(self) -> None:
        cfg = TemporalRobustnessConfig()
        assert cfg.min_profitable_years_fraction == 0.5
        assert cfg.max_single_year_pnl_share == 0.6
        assert cfg.min_edge_year_sessions == 120
        assert cfg.min_judged_years == 2
        assert cfg.mode == MODE_GATE

    @pytest.mark.parametrize("value", [0.0, -0.1, 1.0000001, float("nan"), float("inf")])
    def test_fraction_out_of_range_rejected(self, value: float) -> None:
        with pytest.raises(ValueError, match="min_profitable_years_fraction"):
            TemporalRobustnessConfig(min_profitable_years_fraction=value)

    @pytest.mark.parametrize("value", [0.0, -1.0, 2.0, float("nan"), float("-inf")])
    def test_share_out_of_range_rejected(self, value: float) -> None:
        with pytest.raises(ValueError, match="max_single_year_pnl_share"):
            TemporalRobustnessConfig(max_single_year_pnl_share=value)

    def test_boundary_one_is_valid(self) -> None:
        cfg = TemporalRobustnessConfig(
            min_profitable_years_fraction=1.0, max_single_year_pnl_share=1.0
        )
        assert cfg.p1_threshold() == 1
        assert cfg.p2_threshold() == 1

    def test_negative_edge_sessions_rejected(self) -> None:
        with pytest.raises(ValueError, match="min_edge_year_sessions"):
            TemporalRobustnessConfig(min_edge_year_sessions=-1)

    def test_zero_edge_sessions_valid(self) -> None:
        assert TemporalRobustnessConfig(min_edge_year_sessions=0).min_edge_year_sessions == 0

    def test_min_judged_years_below_one_rejected(self) -> None:
        with pytest.raises(ValueError, match="min_judged_years"):
            TemporalRobustnessConfig(min_judged_years=0)

    def test_unknown_mode_rejected(self) -> None:
        with pytest.raises(ValueError, match="mode"):
            TemporalRobustnessConfig(mode="audit")

    def test_observe_mode_valid(self) -> None:
        assert TemporalRobustnessConfig(mode=MODE_OBSERVE).mode == MODE_OBSERVE

    def test_thresholds_are_exact_fractions_of_the_decimal_string(self) -> None:
        """Fraction(str(0.5)) == 1/2 and Fraction(str(0.6)) == 3/5 EXACTLY —
        never the binary-float neighborhood (0.6 has no exact double)."""
        cfg = TemporalRobustnessConfig()
        assert cfg.p1_threshold() == fractions.Fraction(1, 2)
        assert cfg.p2_threshold() == fractions.Fraction(3, 5)


class TestTripleValidation:
    def test_valid_triple(self) -> None:
        t = _triple(2, 700, 1_000)
        assert t.profitable_judged_years == 2

    def test_all_zero_triple_valid(self) -> None:
        assert _triple(0, 0, 0).sum_pos_year_pnl_paisa == 0

    def test_negative_profitable_rejected(self) -> None:
        with pytest.raises(ValueError, match="profitable_judged_years"):
            _triple(-1, 0, 0)

    def test_negative_max_pos_rejected(self) -> None:
        with pytest.raises(ValueError, match="positive-mass"):
            _triple(1, -5, 10)

    def test_negative_sum_pos_rejected(self) -> None:
        with pytest.raises(ValueError, match="positive-mass"):
            _triple(1, 5, -10)

    def test_sum_below_max_rejected(self) -> None:
        with pytest.raises(ValueError, match="impossible positive-mass fold"):
            _triple(1, 100, 99)

    def test_profitable_without_mass_rejected(self) -> None:
        with pytest.raises(ValueError, match="disagrees"):
            _triple(1, 0, 0)

    def test_mass_without_profitable_rejected(self) -> None:
        with pytest.raises(ValueError, match="disagrees"):
            _triple(0, 5, 5)


class TestJudgedYears:
    def test_empty_census_yields_no_judged_years(self) -> None:
        assert judged_years({}, _CFG) == ()

    def test_negative_session_count_rejected(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            judged_years({2021: -1}, _CFG)

    def test_interior_years_always_judged_even_with_zero_sessions(self) -> None:
        """A whole-year window hole is an interior year — judged (P_y = 0
        honestly counts against, exactly like a flat year)."""
        census = {2020: 248, 2023: 250}  # 2021/2022 absent entirely
        assert judged_years(census, _CFG) == (2020, 2021, 2022, 2023)

    def test_edge_year_at_exactly_the_session_floor_is_judged(self) -> None:
        """The >= boundary — a > mutant dies here."""
        census = {2020: 120, 2021: 250, 2022: 120}
        assert judged_years(census, _CFG) == (2020, 2021, 2022)

    def test_edge_year_one_below_the_floor_is_not_judged(self) -> None:
        census = {2020: 119, 2021: 250, 2022: 119}
        assert judged_years(census, _CFG) == (2021,)

    def test_full_floor_2020_to_mid_2026_shape(self) -> None:
        """The design's quantified default: 2020 (~248 sessions) judged; a
        2026-07-09 sweep's ~130-session 2026 judged; a Feb sweep's is not."""
        census = dict.fromkeys(range(2020, 2026), 248) | {2026: 130}
        assert judged_years(census, _CFG) == tuple(range(2020, 2027))
        feb = dict.fromkeys(range(2020, 2026), 248) | {2026: 25}
        assert judged_years(feb, _CFG) == tuple(range(2020, 2026))

    def test_single_year_window_is_edge_on_both_sides(self) -> None:
        assert judged_years({2024: 200}, _CFG) == (2024,)
        assert judged_years({2024: 50}, _CFG) == ()

    def test_zero_floor_judges_every_edge(self) -> None:
        cfg = TemporalRobustnessConfig(min_edge_year_sessions=0)
        assert judged_years({2020: 0, 2021: 1}, cfg) == (2020, 2021)


class TestTripleFromYearPnls:
    def test_zero_trade_judged_year_counts_against(self) -> None:
        """2022 is judged but absent from the map ⇒ P_y = 0 ⇒ NOT profitable."""
        triple = triple_from_year_pnls({2021: 100, 2023: 200}, [2021, 2022, 2023])
        assert triple.profitable_judged_years == 2
        assert triple.sum_pos_year_pnl_paisa == 300
        assert triple.max_pos_year_pnl_paisa == 200

    def test_flat_year_is_not_profitable(self) -> None:
        """P_y == 0 present in the map is strictly NOT > 0."""
        triple = triple_from_year_pnls({2021: 0, 2022: 5}, [2021, 2022])
        assert triple.profitable_judged_years == 1

    def test_unjudged_year_contributes_nothing(self) -> None:
        """Symmetric exclusion: a positive pnl in an UNJUDGED sliver year joins
        neither the numerator nor the positive mass."""
        triple = triple_from_year_pnls({2019: 10_000, 2021: 100}, [2021])
        assert triple == _triple(1, 100, 100)

    def test_negative_years_join_no_positive_mass(self) -> None:
        triple = triple_from_year_pnls({2021: -500, 2022: 300}, [2021, 2022])
        assert triple == _triple(1, 300, 300)

    def test_max_tracks_the_largest_positive_year(self) -> None:
        """Both arms of the running-max branch (ascending AND descending order)."""
        a = triple_from_year_pnls({2021: 100, 2022: 900}, [2021, 2022])
        b = triple_from_year_pnls({2021: 900, 2022: 100}, [2021, 2022])
        assert a == b == _triple(2, 900, 1_000)

    def test_duplicate_judged_years_are_ignored(self) -> None:
        triple = triple_from_year_pnls({2021: 100}, [2021, 2021])
        assert triple == _triple(1, 100, 100)

    def test_empty_judged_set_yields_the_zero_triple(self) -> None:
        assert triple_from_year_pnls({2021: 100}, []) == _triple(0, 0, 0)


class TestPredicateGuards:
    def test_n_judged_below_one_rejected(self) -> None:
        with pytest.raises(ValueError, match="n_judged_years >= 1"):
            temporal_gate_reasons(_triple(0, 0, 0), n_judged_years=0, cfg=_CFG)

    def test_profitable_beyond_judged_rejected(self) -> None:
        with pytest.raises(ValueError, match="impossible fold"):
            temporal_gate_reasons(_triple(3, 10, 10), n_judged_years=2, cfg=_CFG)


class TestP1Coverage:
    def test_four_of_seven_passes_the_default_half(self) -> None:
        """Fraction(4,7) >= 1/2 — exact; a >= → > mutant survives 4/7 but dies
        on the exact-half case below."""
        triple = _triple(4, 100, 250)
        assert YEAR_GATE_PROFITABLE_YEARS not in temporal_gate_reasons(
            triple, n_judged_years=7, cfg=_CFG
        )

    def test_three_of_seven_fails_the_default_half(self) -> None:
        triple = _triple(3, 100, 250)
        assert YEAR_GATE_PROFITABLE_YEARS in temporal_gate_reasons(
            triple, n_judged_years=7, cfg=_CFG
        )

    def test_exactly_at_the_threshold_passes(self) -> None:
        """2/4 == 1/2 exactly — the >= boundary mutant-killer (the share is
        90/150 == 0.6 exactly, so P2 sits at ITS boundary too)."""
        triple = _triple(2, 90, 150)
        assert temporal_gate_passes(triple, n_judged_years=4, cfg=_CFG)

    def test_one_lucky_year_of_seven_fails(self) -> None:
        """The operator demand as executable truth: 1-of-7 must not rank."""
        triple = _triple(1, 5_000_000, 5_000_000)
        reasons = temporal_gate_reasons(triple, n_judged_years=7, cfg=_CFG)
        assert YEAR_GATE_PROFITABLE_YEARS in reasons
        assert YEAR_GATE_SINGLE_YEAR_SHARE in reasons  # 100% share fails P2 too
        assert reasons == YEAR_GATE_REASON_ORDER  # evaluation order pinned


class TestP2Concentration:
    def test_share_exactly_at_the_threshold_passes(self) -> None:
        """M⁺/S⁺ == 3/5 == Fraction("0.6") exactly — the <= boundary
        mutant-killer (a < mutant dies here)."""
        triple = _triple(4, 600, 1_000)
        assert YEAR_GATE_SINGLE_YEAR_SHARE not in temporal_gate_reasons(
            triple, n_judged_years=7, cfg=_CFG
        )

    def test_share_one_paisa_over_fails(self) -> None:
        triple = _triple(4, 601, 1_000)
        assert YEAR_GATE_SINGLE_YEAR_SHARE in temporal_gate_reasons(
            triple, n_judged_years=7, cfg=_CFG
        )

    def test_seven_profitable_years_with_eighty_percent_share_fails(self) -> None:
        """P1 and P2 are complements: profitable EVERYWHERE but the profit lives
        in one year — dies at P2 alone (the defaults-encoded operator demand)."""
        triple = _triple(7, 800, 1_000)
        reasons = temporal_gate_reasons(triple, n_judged_years=7, cfg=_CFG)
        assert reasons == (YEAR_GATE_SINGLE_YEAR_SHARE,)

    def test_mixed_sign_share_is_positive_mass_based(self) -> None:
        """(+100k, +100k, −150k) → share 100k/200k = 0.5 ⇒ P2 passes; the combo
        dies at P1 (2/7) — the net-total share would have read 100k/50k = 2.0."""
        triple = triple_from_year_pnls(
            {2020: 100_000, 2021: 100_000, 2022: -150_000},
            range(2020, 2027),
        )
        reasons = temporal_gate_reasons(triple, n_judged_years=7, cfg=_CFG)
        assert reasons == (YEAR_GATE_PROFITABLE_YEARS,)

    def test_zero_positive_mass_abstains_without_division(self) -> None:
        """S⁺ == 0 ⇒ P2 abstains; the Fraction share is NEVER constructed
        (spy: a recording Fraction wrapper sees no zero-denominator call —
        and no two-arg call at all on this arm)."""
        calls: list[tuple[Any, ...]] = []
        real_fraction = temporal_mod.Fraction

        def recording_fraction(*args: Any) -> Any:
            calls.append(args)
            return real_fraction(*args)

        triple = _triple(0, 0, 0)
        try:
            temporal_mod.Fraction = recording_fraction  # type: ignore[misc]
            reasons = temporal_gate_reasons(triple, n_judged_years=7, cfg=_CFG)
        finally:
            temporal_mod.Fraction = real_fraction  # type: ignore[misc]
        assert reasons == (YEAR_GATE_PROFITABLE_YEARS,)  # P1 still judges: 0/7
        two_arg_int_calls = [c for c in calls if len(c) == 2]
        # The ONLY two-arg Fraction on this arm is P1's (0, 7) — the share
        # Fraction(M⁺, S⁺) with S⁺ == 0 was never constructed (no division).
        assert two_arg_int_calls == [(0, 7)]


class TestGatePositions:
    def test_abstains_below_min_judged_years(self) -> None:
        assert temporal_gate_positions([_triple(0, 0, 0)], n_judged_years=1, cfg=_CFG) is None
        assert temporal_gate_positions([_triple(0, 0, 0)], n_judged_years=0, cfg=_CFG) is None

    def test_active_at_exactly_min_judged_years(self) -> None:
        """The >= boundary: 2 judged years (the default floor) does NOT abstain."""
        kept = temporal_gate_positions(
            [_triple(2, 500, 1_000), _triple(0, 0, 0)], n_judged_years=2, cfg=_CFG
        )
        assert kept == [0]

    def test_positions_are_input_order_ascending(self) -> None:
        passing = _triple(4, 500, 1_000)
        failing = _triple(1, 500, 500)
        kept = temporal_gate_positions([passing, failing, passing], n_judged_years=7, cfg=_CFG)
        assert kept == [0, 2]

    def test_empty_survivors_keep_empty(self) -> None:
        assert temporal_gate_positions([], n_judged_years=7, cfg=_CFG) == []


class TestApplyTemporalGate:
    def test_gate_mode_filters_and_counts_kills(self) -> None:
        stats = TemporalGateStats()
        kept = apply_temporal_gate(
            [_triple(4, 500, 1_000), _triple(1, 500, 500)],
            cfg=_CFG,
            n_judged_years=7,
            stats=stats,
        )
        assert kept == [0]
        assert stats.evaluated is True
        assert stats.abstained is False
        assert stats.killed == 1
        assert stats.observed_kills == 0

    def test_observe_mode_filters_nothing_but_counts(self) -> None:
        stats = TemporalGateStats()
        cfg = TemporalRobustnessConfig(mode=MODE_OBSERVE)
        kept = apply_temporal_gate(
            [_triple(4, 500, 1_000), _triple(1, 500, 500)],
            cfg=cfg,
            n_judged_years=7,
            stats=stats,
        )
        assert kept == [0, 1]
        assert stats.killed == 0
        assert stats.observed_kills == 1

    def test_abstention_keeps_everything_and_records(self) -> None:
        stats = TemporalGateStats()
        kept = apply_temporal_gate(
            [_triple(0, 0, 0), _triple(1, 5, 5)], cfg=_CFG, n_judged_years=1, stats=stats
        )
        assert kept == [0, 1]
        assert stats.abstained is True
        assert stats.killed == 0
        assert stats.observed_kills == 0

    def test_stats_none_is_accepted_on_every_arm(self) -> None:
        triples = [_triple(4, 500, 1_000), _triple(1, 500, 500)]
        assert apply_temporal_gate(triples, cfg=_CFG, n_judged_years=7) == [0]
        observe = TemporalRobustnessConfig(mode=MODE_OBSERVE)
        assert apply_temporal_gate(triples, cfg=observe, n_judged_years=7) == [0, 1]
        assert apply_temporal_gate(triples, cfg=_CFG, n_judged_years=1) == [0, 1]

    def test_gate_input_bundle_shape(self) -> None:
        """TemporalGateInput carries the rank-seam bundle (triples None on the
        bounded path — the columns carry them)."""
        stats = TemporalGateStats()
        bundle = TemporalGateInput(cfg=_CFG, n_judged_years=7, stats=stats)
        assert bundle.triples is None
        assert bundle.stats is stats


class TestPositiveMassShare:
    def test_none_on_zero_mass(self) -> None:
        assert positive_mass_share(_triple(0, 0, 0)) is None

    def test_display_float(self) -> None:
        assert positive_mass_share(_triple(2, 600, 1_000)) == 0.6


class TestYearGateDiagnostic:
    def test_defaults_config_pass(self) -> None:
        d = year_gate_diagnostic(dict.fromkeys(range(2020, 2027), 100), range(2020, 2027))
        assert d.passed is True
        assert d.abstained is False
        assert d.reasons == ()
        assert d.profitable_years == 7
        assert d.n_judged_years == 7
        assert d.max_year_share == pytest.approx(1 / 7)

    def test_lucky_year_fails_with_reasons(self) -> None:
        d = year_gate_diagnostic({2020: 1_000_000}, range(2020, 2027))
        assert d.passed is False
        assert d.reasons == YEAR_GATE_REASON_ORDER
        assert d.max_year_share == 1.0

    def test_abstains_below_the_floor(self) -> None:
        d = year_gate_diagnostic({2026: 500}, [2026])
        assert d.passed is None
        assert d.abstained is True
        assert d.reasons == ()
        assert d.n_judged_years == 1
        assert d.max_year_share == 1.0

    def test_zero_mass_share_is_none(self) -> None:
        d = year_gate_diagnostic({}, [2020, 2021])
        assert d.passed is False  # 0/2 profitable fails P1
        assert d.max_year_share is None

    def test_explicit_config_is_honored(self) -> None:
        strict = TemporalRobustnessConfig(min_profitable_years_fraction=6 / 7)
        d = year_gate_diagnostic(dict.fromkeys(range(2020, 2025), 100), range(2020, 2027))
        assert d.passed is True  # 5/7 >= 0.5 at defaults
        strict_d = year_gate_diagnostic(
            dict.fromkeys(range(2020, 2025), 100), range(2020, 2027), cfg=strict
        )
        assert strict_d.passed is False  # 5/7 < 6/7

    def test_duplicate_judged_years_deduplicate(self) -> None:
        d = year_gate_diagnostic({2020: 100, 2021: 100}, [2020, 2020, 2021])
        assert d.n_judged_years == 2
        assert d.passed is True
