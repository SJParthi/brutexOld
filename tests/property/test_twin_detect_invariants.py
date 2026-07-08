"""Property tests for :mod:`brutex.engine.twin_detect` (DEC-SWEEP-RESULTS-DEPTH-001 WP4).

Five invariants over hypothesis-generated inputs:

1. **Fingerprint invariance** — the fingerprint is a function of ONLY the four
   behavior-identity fields: decorating every non-identity field (prices,
   excursions, exit reason, VIX) never moves it.
2. **Fingerprint sensitivity / collision-freedom on samples** — two trade
   lists whose identity projections differ always fingerprint differently
   (the serialization is injective; blake3 collisions are not observable at
   sample scale).
3. **Union-find determinism** — ``group_twins`` is input-order-independent:
   any permutation of the candidate pool yields the IDENTICAL
   ``TwinFamilies`` (the sorted-processing law, R34).
4. **Partition + representative law** — the output is a true partition of the
   pool; every family's ``family_id`` is exactly its
   ``min((mask_bits.bit_count(), run_id))`` member; re-running is stable.
5. **Representative stability under unrelated growth** — adding a candidate
   that shares nothing (unique fingerprint, disjoint non-empty entry set)
   never reshuffles the existing families (connected components are local).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from brutex.engine.trade_capture import CapturedTrade
from brutex.engine.twin_detect import (
    TwinCandidate,
    entry_ts_us_set,
    group_twins,
    trade_fingerprint,
)
from brutex.strategies.types import Side
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from tests.fakes.wp4_twin_fixtures import fingerprint_seed, make_captured_trade

_directions = st.sampled_from([Side.BUY, Side.SELL])


@dataclass(frozen=True, slots=True)
class _TradeSpec:
    """One trade's identity projection (entry minute, hold, direction, pnl)."""

    entry_gap_min: int
    hold_min: int
    direction: Side
    pnl_paisa: int


_trade_specs = st.builds(
    _TradeSpec,
    entry_gap_min=st.integers(min_value=1, max_value=20),
    hold_min=st.integers(min_value=1, max_value=20),
    direction=_directions,
    pnl_paisa=st.integers(min_value=-500, max_value=500),
)


def _trades_from_specs(specs: list[_TradeSpec]) -> list[CapturedTrade]:
    trades: list[CapturedTrade] = []
    minute = 0
    for spec in specs:
        minute += spec.entry_gap_min
        trades.append(
            make_captured_trade(
                entry_minute=minute,
                hold_minutes=spec.hold_min,
                direction=spec.direction,
                pnl_paisa=spec.pnl_paisa,
            )
        )
        minute += spec.hold_min
    return trades


def _identity_projection(specs: list[_TradeSpec]) -> tuple[tuple[int, int, str, int], ...]:
    """The canonical identity tuple a spec list denotes (entry/exit/dir/pnl)."""
    projection = []
    minute = 0
    for spec in specs:
        minute += spec.entry_gap_min
        projection.append((minute, minute + spec.hold_min, spec.direction.value, spec.pnl_paisa))
        minute += spec.hold_min
    return tuple(projection)


@dataclass(frozen=True, slots=True)
class _CandidateSpec:
    """One hand-built pool member (small alphabets force tier-1/2 collisions)."""

    mask_bits: int
    fp_seed: int
    entries: frozenset[int]


_candidate_specs = st.builds(
    _CandidateSpec,
    mask_bits=st.integers(min_value=0, max_value=255),
    fp_seed=st.integers(min_value=0, max_value=3),
    entries=st.frozensets(st.integers(min_value=0, max_value=15), max_size=8),
)


def _pool_from_specs(specs: list[_CandidateSpec]) -> list[TwinCandidate]:
    return [
        TwinCandidate(
            run_id=f"run-{i:02d}",
            mask_bits=spec.mask_bits,
            fingerprint=fingerprint_seed(spec.fp_seed),
            entry_ts_us=spec.entries,
        )
        for i, spec in enumerate(specs)
    ]


_pools = st.lists(_candidate_specs, min_size=0, max_size=8).map(_pool_from_specs)


class TestFingerprintInvariance:
    @given(specs=st.lists(_trade_specs, min_size=1, max_size=6))
    @settings(max_examples=50, deadline=None)
    def test_non_identity_fields_never_move_the_fingerprint(self, specs: list[_TradeSpec]) -> None:
        base = _trades_from_specs(specs)
        decorated = [
            make_captured_trade(
                entry_ts=t.entry_ts,
                exit_ts=t.exit_ts,
                direction=t.direction,
                pnl_paisa=t.pnl_paisa,
                entry_px=Decimal("55.55"),
                exit_px=Decimal("66.66"),
                exit_reason="take_profit",
                mae_paisa=1_234,
                mfe_paisa=4_321,
                vix_entry=Decimal("14.05"),
                vix_exit=Decimal("21.9"),
            )
            for t in base
        ]
        assert trade_fingerprint(decorated) == trade_fingerprint(base)

    @given(
        specs_a=st.lists(_trade_specs, min_size=0, max_size=6),
        specs_b=st.lists(_trade_specs, min_size=0, max_size=6),
    )
    @settings(max_examples=50, deadline=None)
    def test_distinct_identity_projections_fingerprint_differently(
        self, specs_a: list[_TradeSpec], specs_b: list[_TradeSpec]
    ) -> None:
        assume(_identity_projection(specs_a) != _identity_projection(specs_b))
        assert trade_fingerprint(_trades_from_specs(specs_a)) != trade_fingerprint(
            _trades_from_specs(specs_b)
        )

    @given(specs=st.lists(_trade_specs, min_size=0, max_size=6))
    @settings(max_examples=50, deadline=None)
    def test_fingerprint_and_signature_are_pure(self, specs: list[_TradeSpec]) -> None:
        trades = _trades_from_specs(specs)
        assert trade_fingerprint(trades) == trade_fingerprint(_trades_from_specs(specs))
        assert entry_ts_us_set(trades) == entry_ts_us_set(_trades_from_specs(specs))


class TestGroupTwinsDeterminism:
    @given(data=st.data(), pool=_pools)
    @settings(max_examples=50, deadline=None)
    def test_permutation_invariance(self, data: st.DataObject, pool: list[TwinCandidate]) -> None:
        shuffled = data.draw(st.permutations(pool))
        assert group_twins(shuffled) == group_twins(pool)

    @given(pool=_pools)
    @settings(max_examples=50, deadline=None)
    def test_partition_and_min_bit_representative_law(self, pool: list[TwinCandidate]) -> None:
        by_run_id = {c.run_id: c for c in pool}
        families = group_twins(pool)
        # Re-running is byte-stable (purity — the R34 "stable across re-runs").
        assert group_twins(pool) == families
        # Every candidate is mapped; the members tuples partition the pool.
        assert set(families.family_by_run_id) == set(by_run_id)
        flattened = [rid for members in families.members_by_family.values() for rid in members]
        assert sorted(flattened) == sorted(by_run_id)
        for family_id, members in families.members_by_family.items():
            assert members == tuple(sorted(members))
            expected_rep = min(members, key=lambda rid: (by_run_id[rid].mask_bits.bit_count(), rid))
            assert family_id == expected_rep
            for rid in members:
                assert families.family_by_run_id[rid] == family_id

    @given(pool=_pools)
    @settings(max_examples=50, deadline=None)
    def test_unrelated_candidate_never_reshuffles_existing_families(
        self, pool: list[TwinCandidate]
    ) -> None:
        # Unique fingerprint (seed far outside the pool alphabet) + a
        # NON-EMPTY entry set disjoint from the pool universe ⇒ zero edges.
        loner = TwinCandidate(
            run_id="zz-loner",
            mask_bits=0,
            fingerprint=fingerprint_seed(999),
            entry_ts_us=frozenset(range(10_000, 10_005)),
        )
        before = group_twins(pool)
        after = group_twins([*pool, loner])
        assert after.family_by_run_id["zz-loner"] == "zz-loner"
        assert after.members_by_family["zz-loner"] == ("zz-loner",)
        for run_id, family_id in before.family_by_run_id.items():
            assert after.family_by_run_id[run_id] == family_id
        for family_id, members in before.members_by_family.items():
            assert after.members_by_family[family_id] == members
