"""Hypothesis laws for the temporal-robustness year gate (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001).

Predicate-core invariants over :mod:`brutex.ranking.temporal`:

* the passes ⇔ reasons-empty twin parity (the door law can never fork);
* :func:`temporal_gate_positions` ≡ a brute-force per-triple reference predicate
  computed with exact :class:`fractions.Fraction` arithmetic written
  independently here (the #1154 test-side-oracle discipline);
* the kept MEMBERSHIP is invariant under survivor permutation (positions map
  through the permutation exactly);
* :func:`triple_from_year_pnls` always yields a self-consistent triple, is
  judged-set-scoped, and matches an independent dict fold.

The engine-integration laws (rank-time array fold ≡ ``ist_trade_date`` fold,
batch ≡ bounded with temporal ON) live with the engine wiring suites.
"""

from __future__ import annotations

from fractions import Fraction

from brutex.ranking.temporal import (
    MODE_GATE,
    MODE_OBSERVE,
    TemporalGateStats,
    TemporalRobustnessConfig,
    TemporalTriple,
    apply_temporal_gate,
    temporal_gate_passes,
    temporal_gate_positions,
    temporal_gate_reasons,
    triple_from_year_pnls,
)
from hypothesis import given
from hypothesis import strategies as st

_MAX_YEARS = 8
_PNL = st.integers(min_value=-(10**12), max_value=10**12)


@st.composite
def _triples(draw: st.DrawFn, n_judged: int = _MAX_YEARS) -> TemporalTriple:
    """A CONSISTENT triple: built from a real per-year pnl fold, never free ints."""
    pnls = draw(
        st.lists(_PNL, min_size=0, max_size=n_judged).map(
            lambda values: {2020 + i: v for i, v in enumerate(values)}
        )
    )
    return triple_from_year_pnls(pnls, range(2020, 2020 + n_judged))


_CONFIGS = st.builds(
    TemporalRobustnessConfig,
    min_profitable_years_fraction=st.sampled_from([0.25, 0.5, 0.6, 0.75, 1.0]),
    max_single_year_pnl_share=st.sampled_from([0.4, 0.5, 0.6, 0.8, 1.0]),
    min_judged_years=st.integers(min_value=1, max_value=4),
    mode=st.sampled_from([MODE_GATE, MODE_OBSERVE]),
)


def _reference_passes(triple: TemporalTriple, n_judged: int, cfg: TemporalRobustnessConfig) -> bool:
    """An INDEPENDENT re-statement of the P1 ∧ P2 law (exact Fractions)."""
    p1 = Fraction(triple.profitable_judged_years, n_judged) >= Fraction(
        str(cfg.min_profitable_years_fraction)
    )
    if triple.sum_pos_year_pnl_paisa == 0:
        p2 = True
    else:
        p2 = Fraction(triple.max_pos_year_pnl_paisa, triple.sum_pos_year_pnl_paisa) <= Fraction(
            str(cfg.max_single_year_pnl_share)
        )
    return p1 and p2


@given(triple=_triples(), cfg=_CONFIGS)
def test_passes_iff_reasons_empty(triple: TemporalTriple, cfg: TemporalRobustnessConfig) -> None:
    passes = temporal_gate_passes(triple, n_judged_years=_MAX_YEARS, cfg=cfg)
    reasons = temporal_gate_reasons(triple, n_judged_years=_MAX_YEARS, cfg=cfg)
    assert passes == (reasons == ())


@given(triple=_triples(), cfg=_CONFIGS)
def test_predicate_matches_the_reference(
    triple: TemporalTriple, cfg: TemporalRobustnessConfig
) -> None:
    assert temporal_gate_passes(triple, n_judged_years=_MAX_YEARS, cfg=cfg) == _reference_passes(
        triple, _MAX_YEARS, cfg
    )


@given(survivors=st.lists(_triples(), max_size=12), cfg=_CONFIGS)
def test_positions_equal_the_per_triple_reference(
    survivors: list[TemporalTriple], cfg: TemporalRobustnessConfig
) -> None:
    kept = temporal_gate_positions(survivors, n_judged_years=_MAX_YEARS, cfg=cfg)
    assert kept is not None  # _MAX_YEARS >= every drawn min_judged_years
    assert kept == [i for i, t in enumerate(survivors) if _reference_passes(t, _MAX_YEARS, cfg)]


@given(
    survivors=st.lists(_triples(), min_size=1, max_size=10),
    cfg=_CONFIGS,
    seed=st.randoms(use_true_random=False),
)
def test_kept_membership_is_permutation_invariant(
    survivors: list[TemporalTriple],
    cfg: TemporalRobustnessConfig,
    seed: object,
) -> None:
    """Permuting the survivor order permutes the kept POSITIONS exactly —
    membership (which triples survive) never depends on enumeration order."""
    import random

    rng = random.Random(0xB127E)
    order = list(range(len(survivors)))
    rng.shuffle(order)
    permuted = [survivors[i] for i in order]
    kept = temporal_gate_positions(survivors, n_judged_years=_MAX_YEARS, cfg=cfg)
    kept_permuted = temporal_gate_positions(permuted, n_judged_years=_MAX_YEARS, cfg=cfg)
    assert kept is not None and kept_permuted is not None
    assert sorted(order[p] for p in kept_permuted) == kept


@given(data=st.data(), cfg=_CONFIGS, n_judged=st.integers(0, 8))
def test_apply_gate_bookkeeping_law(
    data: st.DataObject, cfg: TemporalRobustnessConfig, n_judged: int
) -> None:
    """kept + (killed | observed_kills) == survivors on every arm; observe and
    abstention never filter; gate mode's kept set is the positions core's.

    Each survivor triple is folded over EXACTLY ``n_judged`` judged years so the
    (triple, n_judged) pair is always self-consistent (the predicate guard
    rejects an impossible pair loudly — pinned in the unit suite)."""
    survivors = data.draw(st.lists(_triples(n_judged=n_judged), max_size=10))
    stats = TemporalGateStats()
    kept = apply_temporal_gate(survivors, cfg=cfg, n_judged_years=n_judged, stats=stats)
    assert stats.evaluated is True
    if n_judged < cfg.min_judged_years:
        assert stats.abstained is True
        assert kept == list(range(len(survivors)))
        assert stats.killed == 0 and stats.observed_kills == 0
        return
    core = temporal_gate_positions(survivors, n_judged_years=n_judged, cfg=cfg)
    assert core is not None
    if cfg.mode == MODE_OBSERVE:
        assert kept == list(range(len(survivors)))
        assert stats.observed_kills == len(survivors) - len(core)
        assert stats.killed == 0
    else:
        assert kept == core
        assert stats.killed == len(survivors) - len(core)
        assert stats.observed_kills == 0


@given(
    pnls=st.dictionaries(st.integers(2018, 2030), _PNL, max_size=12),
    judged=st.lists(st.integers(2018, 2030), max_size=10),
)
def test_triple_fold_matches_an_independent_dict_fold(
    pnls: dict[int, int], judged: list[int]
) -> None:
    triple = triple_from_year_pnls(pnls, judged)
    judged_set = set(judged)
    positives = [pnls.get(y, 0) for y in judged_set if pnls.get(y, 0) > 0]
    assert triple.profitable_judged_years == len(positives)
    assert triple.sum_pos_year_pnl_paisa == sum(positives)
    assert triple.max_pos_year_pnl_paisa == (max(positives) if positives else 0)
