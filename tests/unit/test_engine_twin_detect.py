"""Unit tests for :mod:`brutex.engine.twin_detect` (DEC-SWEEP-RESULTS-DEPTH-001 WP4).

Covers: the tier-1 fingerprint law (identity fields move it, non-identity
fields provably do not, order is significant, the version prefix is live),
the entry-timestamp signature, candidate/threshold validation (junk fails
loudly), tier-1 + tier-2 grouping incl. the EXACT 0.90 Jaccard boundary,
union-find transitivity, the fewest-bits-then-run_id representative law,
cross-bucket twins by construction, and a bounded absolute smoke over a
100-candidate worst-case (all-pairs) pool.
"""

from __future__ import annotations

import math
import time
from datetime import timedelta
from decimal import Decimal

import blake3
import pytest
from brutex.engine.trade_capture import CapturedTrade
from brutex.engine.twin_detect import (
    FINGERPRINT_VERSION_PREFIX,
    TWIN_JACCARD_THRESHOLD,
    TwinCandidate,
    TwinDetectError,
    TwinFamilies,
    entry_ts_us_set,
    group_twins,
    trade_fingerprint,
)
from brutex.strategies.types import Side

from tests.fakes.wp4_twin_fixtures import (
    fingerprint_seed,
    ist_ts,
    make_captured_trade,
)


def _candidate(
    run_id: str,
    *,
    mask_bits: int = 0b1,
    seed: int = 0,
    entries: frozenset[int] = frozenset(),
) -> TwinCandidate:
    """A hand-built candidate with a deterministic valid fingerprint."""
    return TwinCandidate(
        run_id=run_id,
        mask_bits=mask_bits,
        fingerprint=fingerprint_seed(seed),
        entry_ts_us=entries,
    )


def _base_trades() -> list[CapturedTrade]:
    """A small mixed-direction identity fixture (freshly built per call)."""
    return [
        make_captured_trade(entry_minute=1, hold_minutes=25, direction=Side.BUY, pnl_paisa=1_250),
        make_captured_trade(entry_minute=49, hold_minutes=85, direction=Side.SELL, pnl_paisa=-475),
    ]


class TestTradeFingerprint:
    def test_identical_lists_same_fingerprint(self) -> None:
        fp_a = trade_fingerprint(_base_trades())
        fp_b = trade_fingerprint(_base_trades())
        assert fp_a == fp_b
        assert len(fp_a) == 64
        assert set(fp_a) <= set("0123456789abcdef")

    def test_empty_list_hashes_the_bare_version_prefix(self) -> None:
        expected = blake3.blake3(FINGERPRINT_VERSION_PREFIX.encode("utf-8")).hexdigest()
        assert trade_fingerprint([]) == expected

    def test_order_is_significant(self) -> None:
        trades = _base_trades()
        assert trade_fingerprint(trades) != trade_fingerprint(list(reversed(trades)))

    @pytest.mark.parametrize("field", ["entry_ts", "exit_ts", "direction", "pnl_paisa"])
    def test_each_identity_field_moves_the_fingerprint(self, field: str) -> None:
        base = _base_trades()
        first = base[0]
        one_us = timedelta(microseconds=1)
        # Perturb EXACTLY one identity field; every other field passes through.
        perturbed_first = make_captured_trade(
            entry_ts=first.entry_ts + (one_us if field == "entry_ts" else timedelta(0)),
            exit_ts=first.exit_ts + (one_us if field == "exit_ts" else timedelta(0)),
            direction=Side.SELL if field == "direction" else first.direction,
            pnl_paisa=first.pnl_paisa + (1 if field == "pnl_paisa" else 0),
        )
        assert trade_fingerprint([perturbed_first, *base[1:]]) != trade_fingerprint(base)

    def test_non_identity_fields_do_not_move_the_fingerprint(self) -> None:
        base = _base_trades()
        decorated = [
            make_captured_trade(
                entry_ts=t.entry_ts,
                exit_ts=t.exit_ts,
                direction=t.direction,
                pnl_paisa=t.pnl_paisa,
                entry_px=Decimal("222.22"),
                exit_px=Decimal("333.33"),
                exit_reason="stop_loss",
                mae_paisa=7_777,
                mfe_paisa=9_999,
                vix_entry=Decimal("15.25"),
                vix_exit=Decimal("18.5"),
            )
            for t in base
        ]
        assert trade_fingerprint(decorated) == trade_fingerprint(base)


class TestEntryTsUsSet:
    def test_exact_epoch_microseconds(self) -> None:
        # 2024-01-02 09:17 IST == 03:47:00 UTC == 1_704_167_220_000_000 µs.
        trade = make_captured_trade(entry_ts=ist_ts(9, 17), exit_ts=ist_ts(9, 42))
        assert entry_ts_us_set([trade]) == frozenset({1_704_167_220_000_000})

    def test_duplicate_entries_collapse(self) -> None:
        a = make_captured_trade(entry_minute=3, hold_minutes=2)
        b = make_captured_trade(entry_minute=3, hold_minutes=9, pnl_paisa=-50)
        assert len(entry_ts_us_set([a, b])) == 1

    def test_empty_list_is_the_empty_set(self) -> None:
        assert entry_ts_us_set([]) == frozenset()


class TestTwinCandidateValidation:
    def test_empty_run_id_refused(self) -> None:
        with pytest.raises(TwinDetectError, match="run_id must be non-empty"):
            _candidate("")

    def test_negative_mask_bits_refused(self) -> None:
        with pytest.raises(TwinDetectError, match="mask_bits must be >= 0"):
            _candidate("run-a", mask_bits=-1)

    @pytest.mark.parametrize(
        "junk",
        [
            "",
            "abc123",
            "A" * 64,  # uppercase hex refused
            "z" * 64,  # non-hex refused
            fingerprint_seed(0) + "0",  # 65 chars refused
        ],
    )
    def test_junk_fingerprint_refused(self, junk: str) -> None:
        with pytest.raises(TwinDetectError, match="64-char lowercase hex"):
            TwinCandidate(run_id="run-a", mask_bits=1, fingerprint=junk, entry_ts_us=frozenset())

    def test_zero_mask_bits_and_valid_fingerprint_accepted(self) -> None:
        cand = _candidate("run-a", mask_bits=0)
        assert cand.mask_bits.bit_count() == 0

    def test_from_trades_binds_fingerprint_and_signature_to_one_list(self) -> None:
        trades = _base_trades()
        cand = TwinCandidate.from_trades(run_id="run-a", mask_bits=0b101, trades=trades)
        assert cand.fingerprint == trade_fingerprint(trades)
        assert cand.entry_ts_us == entry_ts_us_set(trades)
        assert cand.run_id == "run-a"
        assert cand.mask_bits == 0b101


class TestGroupTwinsValidation:
    def test_duplicate_run_id_refused(self) -> None:
        pool = [_candidate("run-a", seed=1), _candidate("run-a", seed=2)]
        with pytest.raises(TwinDetectError, match="duplicate run_id 'run-a'"):
            group_twins(pool)

    @pytest.mark.parametrize("bad", [0.0, -0.25, 1.5, math.nan])
    def test_threshold_outside_envelope_refused(self, bad: float) -> None:
        with pytest.raises(TwinDetectError, match=r"jaccard_threshold must be in \(0.0, 1.0\]"):
            group_twins([_candidate("run-a")], jaccard_threshold=bad)

    def test_threshold_exactly_one_merges_only_identical_sets(self) -> None:
        same = frozenset(range(10))
        pool = [
            _candidate("run-a", seed=1, entries=same),
            _candidate("run-b", seed=2, entries=same),
            _candidate("run-c", seed=3, entries=frozenset(range(9))),  # 9/10 < 1.0
        ]
        families = group_twins(pool, jaccard_threshold=1.0)
        assert families.members_by_family == {
            "run-a": ("run-a", "run-b"),
            "run-c": ("run-c",),
        }


class TestGroupTwinsFamilies:
    def test_empty_pool(self) -> None:
        assert group_twins([]) == TwinFamilies(family_by_run_id={}, members_by_family={})

    def test_singleton_is_its_own_family(self) -> None:
        families = group_twins([_candidate("run-a", entries=frozenset({1, 2}))])
        assert families.family_by_run_id == {"run-a": "run-a"}
        assert families.members_by_family == {"run-a": ("run-a",)}

    def test_tier1_identical_fingerprints_merge_rep_has_fewest_bits(self) -> None:
        pool = [
            _candidate("run-a", mask_bits=0b111, seed=7, entries=frozenset({1})),
            _candidate("run-b", mask_bits=0b11, seed=7, entries=frozenset({500})),
        ]
        families = group_twins(pool)
        # Same fingerprint groups them even with DISJOINT entry sets; the
        # simpler mask (2 bits) represents, not the smaller run_id.
        assert families.family_by_run_id == {"run-a": "run-b", "run-b": "run-b"}
        assert families.members_by_family == {"run-b": ("run-a", "run-b")}

    def test_representative_tie_breaks_run_id_asc(self) -> None:
        pool = [
            _candidate("run-b", mask_bits=0b101, seed=7),
            _candidate("run-a", mask_bits=0b011, seed=7),  # same bit count (2)
        ]
        families = group_twins(pool)
        assert families.members_by_family == {"run-a": ("run-a", "run-b")}

    def test_tier2_jaccard_boundary_090_is_inclusive(self) -> None:
        # |inter| = 18, |union| = 20 → exactly 0.90 → MERGES (>=, test-pinned).
        pool = [
            _candidate("run-a", mask_bits=0b1, seed=1, entries=frozenset(range(20))),
            _candidate("run-b", mask_bits=0b11, seed=2, entries=frozenset(range(18))),
        ]
        families = group_twins(pool)
        assert families.members_by_family == {"run-a": ("run-a", "run-b")}
        assert TWIN_JACCARD_THRESHOLD == 0.90

    def test_tier2_just_below_boundary_stays_separate(self) -> None:
        # |inter| = 17, |union| = 20 → 0.85 < 0.90 → two families.
        pool = [
            _candidate("run-a", seed=1, entries=frozenset(range(20))),
            _candidate("run-b", seed=2, entries=frozenset(range(17))),
        ]
        families = group_twins(pool)
        assert families.members_by_family == {
            "run-a": ("run-a",),
            "run-b": ("run-b",),
        }

    def test_same_entries_different_pnl_are_tier2_twins(self) -> None:
        # The realistic near-twin: identical entry instants, one pnl differs
        # by a tick → fingerprints differ (tier 1 misses) but Jaccard is 1.0.
        base = _base_trades()
        shifted = [
            make_captured_trade(
                entry_ts=t.entry_ts,
                exit_ts=t.exit_ts,
                direction=t.direction,
                pnl_paisa=t.pnl_paisa + 1,
            )
            for t in base
        ]
        pool = [
            TwinCandidate.from_trades(run_id="run-a", mask_bits=0b111, trades=base),
            TwinCandidate.from_trades(run_id="run-b", mask_bits=0b1, trades=shifted),
        ]
        assert pool[0].fingerprint != pool[1].fingerprint
        families = group_twins(pool)
        assert families.members_by_family == {"run-b": ("run-a", "run-b")}

    def test_union_find_transitivity_chains_families(self) -> None:
        # A~B and B~C at >= 0.90 while A↔C alone is 0.818 → ONE family.
        set_a = frozenset(range(0, 40))
        set_b = frozenset(range(2, 42))
        set_c = frozenset(range(4, 44))
        assert len(set_a & set_c) / len(set_a | set_c) < TWIN_JACCARD_THRESHOLD
        pool = [
            _candidate("run-a", mask_bits=0b111, seed=1, entries=set_a),
            _candidate("run-b", mask_bits=0b11, seed=2, entries=set_b),
            _candidate("run-c", mask_bits=0b1111, seed=3, entries=set_c),
        ]
        families = group_twins(pool)
        assert families.members_by_family == {"run-b": ("run-a", "run-b", "run-c")}

    def test_tier1_then_tier2_deep_chain_one_family(self) -> None:
        # r3~r4 by fingerprint (tier 1), then r1~r3 by Jaccard (tier 2):
        # exercises a depth-2 union-find chain + the already-connected skip.
        pool = [
            _candidate("run-1", mask_bits=0b11, seed=1, entries=frozenset(range(20))),
            _candidate(
                "run-3", mask_bits=0b111, seed=2, entries=frozenset(range(19)) | {100}
            ),  # 19/21 ≈ 0.905 vs run-1
            _candidate("run-4", mask_bits=0b1, seed=2, entries=frozenset(range(200, 210))),
        ]
        families = group_twins(pool)
        assert families.family_by_run_id == {
            "run-1": "run-4",
            "run-3": "run-4",
            "run-4": "run-4",
        }
        assert families.members_by_family == {"run-4": ("run-1", "run-3", "run-4")}

    def test_tier2_union_adopts_the_smaller_existing_root(self) -> None:
        # q1~q3 by fingerprint first, then q2~q3 by Jaccard: the union of
        # (q2, q3) must fold q2 under q1's root — all three one family.
        pool = [
            _candidate("run-1", mask_bits=0b1, seed=9, entries=frozenset({1_000})),
            _candidate("run-2", mask_bits=0b11, seed=8, entries=frozenset(range(19)) | {500}),
            _candidate("run-3", mask_bits=0b111, seed=9, entries=frozenset(range(20))),
        ]
        families = group_twins(pool)
        assert families.members_by_family == {"run-1": ("run-1", "run-2", "run-3")}

    def test_both_empty_entry_sets_are_identical_behavior(self) -> None:
        # Hand-built zero-trade candidates whose fingerprints differ: the
        # tier-2 Jaccard of two EMPTY sets is defined 1.0 → one family.
        pool = [
            _candidate("run-a", mask_bits=0b11, seed=1),
            _candidate("run-b", mask_bits=0b1, seed=2),
        ]
        families = group_twins(pool)
        assert families.members_by_family == {"run-b": ("run-a", "run-b")}

    def test_custom_threshold_is_plumbed_through(self) -> None:
        pool = [
            _candidate("run-a", seed=1, entries=frozenset(range(10))),
            _candidate("run-b", seed=2, entries=frozenset(range(6))),  # 6/10 = 0.6
        ]
        assert len(group_twins(pool).members_by_family) == 2
        loose = group_twins(pool, jaccard_threshold=0.5)
        assert loose.members_by_family == {"run-a": ("run-a", "run-b")}

    def test_output_is_a_deterministically_ordered_partition(self) -> None:
        pool = [
            _candidate("run-c", mask_bits=0b1, seed=5, entries=frozenset({50})),
            _candidate("run-a", mask_bits=0b11, seed=7, entries=frozenset({1})),
            _candidate("run-b", mask_bits=0b111, seed=7, entries=frozenset({999})),
        ]
        families = group_twins(pool)
        assert list(families.family_by_run_id) == ["run-a", "run-b", "run-c"]
        assert list(families.members_by_family) == ["run-a", "run-c"]
        seen = [rid for members in families.members_by_family.values() for rid in members]
        assert sorted(seen) == ["run-a", "run-b", "run-c"]
        for family_id, members in families.members_by_family.items():
            assert members == tuple(sorted(members))
            assert all(families.family_by_run_id[rid] == family_id for rid in members)


class TestCrossBucketTwins:
    def test_twins_across_tf_direction_buckets_group_by_construction(self) -> None:
        """Candidates carry no bucket coordinates — pooling IS the mechanism.

        Two combos from DIFFERENT tuple buckets (say bull/5minute and
        bull/15minute) that produced the same trade list land in one family,
        and the simplest mask represents, exactly like an intra-bucket pair.
        """
        trades = _base_trades()
        bucket_5m = TwinCandidate.from_trades(
            run_id="5m-" + "a" * 61, mask_bits=(1 << 52) | (1 << 7) | 1, trades=trades
        )
        bucket_15m = TwinCandidate.from_trades(
            run_id="15m-" + "b" * 60, mask_bits=(1 << 9) | 1, trades=trades
        )
        loner = TwinCandidate.from_trades(
            run_id="zz-loner",
            mask_bits=0b1,
            trades=[make_captured_trade(entry_minute=400, hold_minutes=3, pnl_paisa=9)],
        )
        families = group_twins([loner, bucket_5m, bucket_15m])
        assert families.family_by_run_id == {
            bucket_5m.run_id: bucket_15m.run_id,
            bucket_15m.run_id: bucket_15m.run_id,
            "zz-loner": "zz-loner",
        }


class TestBoundedSmoke:
    def test_group_twins_on_100_disjoint_candidates_is_bounded(self) -> None:
        """Worst-case pool (nothing merges → all 4,950 pairs pay a Jaccard).

        An ABSOLUTE smoke bound, not an O(1) claim — grouping is O(C²) over
        the bounded top-100 candidate pool by design (module docstring).
        """
        pool = [
            _candidate(
                f"run-{i:03d}",
                mask_bits=i + 1,
                seed=i,
                entries=frozenset(range(100 * i, 100 * i + 60)),
            )
            for i in range(100)
        ]
        started = time.perf_counter()
        families = group_twins(pool)
        elapsed = time.perf_counter() - started
        assert len(families.members_by_family) == 100
        assert len(families.family_by_run_id) == 100
        assert elapsed < 10.0, f"group_twins over 100 candidates took {elapsed:.2f}s"
