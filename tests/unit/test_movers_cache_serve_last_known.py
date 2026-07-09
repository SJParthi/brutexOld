"""Unit suite for the serve-last-known cache mode (DEC-CACHE-SERVE-LAST-KNOWN-001).

Proves :meth:`brutex.dashboard.movers_cache.MoversSnapshotCache.serve_last_known`
+ the ``mru_prefix=`` bookkeeping on ``get_or_compute``: during a revision-probe
OUTAGE the most-recent entry for a stable key PREFIX is served (labelled by the
``(value, served_last_known)`` return — the caller stamps the payload), a prefix
with no last-known entry computes ONCE and seeds the slot at the
:data:`~brutex.dashboard.movers_cache.OUTAGE_GENERATION` sentinel, the WARN is
latched once per outage per prefix (the ``rate_bridge_cache`` posture), the next
successful ``get_or_compute`` logs ONE recovery INFO and re-arms the latch, and
every new bookkeeping dict is BOUNDED (capacity-LRU — no growth). Fails-before
framing: the pre-change probe-``None`` doctrine ("compute fresh, cache NOTHING")
re-paid the full build on every poll for the outage's duration — the
zero-extra-compute assertions here fail on that code.
"""

from __future__ import annotations

import threading

import pytest
from brutex.dashboard.movers_cache import OUTAGE_GENERATION, MoversSnapshotCache
from structlog.testing import capture_logs


def _outage_warns(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [e for e in logs if e["event"] == "movers_cache.revision_probe_outage"]


def _recovered_infos(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [e for e in logs if e["event"] == "movers_cache.revision_probe_recovered"]


class TestServeLastKnown:
    """The outage-serve arm: MRU serve, fresh seed, labelling, latch."""

    def test_serves_the_mru_entry_recorded_via_mru_prefix(self) -> None:
        """An entry stored under a revision-folded key serves via its prefix."""
        cache = MoversSnapshotCache()
        cache.get_or_compute("p|rev=1", generation=0, compute=lambda: "v1", mru_prefix="p")
        calls = {"n": 0}

        def _compute() -> str:
            calls["n"] += 1
            return "fresh"

        value, served = cache.serve_last_known("p", compute=_compute)
        assert (value, served) == ("v1", True)
        assert calls["n"] == 0  # no compute — the last-known entry served

    def test_pointer_tracks_the_newest_revision(self) -> None:
        """A newer revision's store moves the pointer — the LAST entry serves."""
        cache = MoversSnapshotCache()
        cache.get_or_compute("p|rev=1", generation=0, compute=lambda: "v1", mru_prefix="p")
        cache.get_or_compute("p|rev=2", generation=0, compute=lambda: "v2", mru_prefix="p")
        value, served = cache.serve_last_known("p", compute=lambda: "fresh")
        assert (value, served) == ("v2", True)

    def test_direct_key_equals_prefix_fallback(self) -> None:
        """A stable-key caller (key == prefix) is served WITHOUT mru_prefix wiring."""
        cache = MoversSnapshotCache()
        cache.get_or_compute("stable", generation=7, compute=lambda: "v")
        value, served = cache.serve_last_known("stable", compute=lambda: "fresh")
        assert (value, served) == ("v", True)

    def test_no_entry_computes_once_and_seeds(self) -> None:
        """No last-known entry → compute ONCE, seed the slot; later outage
        polls serve the seed (fails-before: the old doctrine recomputed per poll)."""
        cache = MoversSnapshotCache()
        calls = {"n": 0}

        def _compute() -> str:
            calls["n"] += 1
            return f"v{calls['n']}"

        first, first_served = cache.serve_last_known("p", compute=_compute)
        assert (first, first_served) == ("v1", False)
        assert cache.size() == 1  # the seed was stored
        second, second_served = cache.serve_last_known("p", compute=_compute)
        assert (second, second_served) == ("v1", True)
        assert calls["n"] == 1  # ONE build per outage — never per-poll

    def test_seed_generation_is_the_unreachable_sentinel(self) -> None:
        """The seed can never read as a clean hit for any real generation."""
        cache = MoversSnapshotCache()
        cache.serve_last_known("p", compute=lambda: "seed")
        assert OUTAGE_GENERATION < 0  # revision_generation() is non-negative
        calls = {"n": 0}

        def _compute() -> str:
            calls["n"] += 1
            return "real"

        assert cache.get_or_compute("p", generation=0, compute=_compute) == "real"
        assert calls["n"] == 1  # the sentinel mismatched → fresh recompute

    def test_raising_compute_stores_nothing(self) -> None:
        cache = MoversSnapshotCache()
        with pytest.raises(RuntimeError, match="boom"):
            cache.serve_last_known("p", compute=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert cache.size() == 0

    def test_pointer_to_an_evicted_entry_falls_back(self) -> None:
        """An evicted MRU target degrades to the seed arm — never a KeyError."""
        cache = MoversSnapshotCache(capacity=1)
        cache.get_or_compute("p|rev=1", generation=0, compute=lambda: "v1", mru_prefix="p")
        cache.get_or_compute("other", generation=0, compute=lambda: "x")  # evicts p|rev=1
        value, served = cache.serve_last_known("p", compute=lambda: "fresh")
        assert (value, served) == ("fresh", False)

    def test_seed_respects_the_lru_capacity(self) -> None:
        cache = MoversSnapshotCache(capacity=1)
        cache.get_or_compute("other", generation=0, compute=lambda: "x")
        cache.serve_last_known("p", compute=lambda: "seed")
        assert cache.size() == 1  # the seed evicted the LRU entry — bounded


class TestBoundedBookkeeping:
    """The new dicts never grow past capacity (no-growth law)."""

    def test_mru_pointer_dict_is_bounded(self) -> None:
        cache = MoversSnapshotCache(capacity=4)
        for i in range(20):
            cache.get_or_compute(
                f"p{i}|rev=1", generation=0, compute=lambda: "v", mru_prefix=f"p{i}"
            )
        assert len(cache._mru_by_prefix) <= 4

    def test_outage_latch_is_bounded(self) -> None:
        cache = MoversSnapshotCache(capacity=4)
        for i in range(20):
            cache.serve_last_known(f"p{i}", compute=lambda: "v")
        assert len(cache._outage_latched) <= 4


class TestOutageWarnLatch:
    """One WARN per outage per prefix; one INFO per recovery; re-armed after."""

    def test_warn_once_per_outage_then_recovered_once(self) -> None:
        cache = MoversSnapshotCache()
        cache.get_or_compute("p|rev=1", generation=0, compute=lambda: "v1", mru_prefix="p")
        with capture_logs() as logs:
            for _ in range(3):
                cache.serve_last_known("p", compute=lambda: "fresh")
        warns = _outage_warns(logs)
        assert len(warns) == 1  # latched — never per-poll
        assert warns[0]["served"] == "last_known"
        # Recovery: the next successful get_or_compute clears the latch (one INFO)…
        with capture_logs() as logs:
            cache.get_or_compute("p|rev=2", generation=0, compute=lambda: "v2", mru_prefix="p")
            cache.get_or_compute("p|rev=2", generation=0, compute=lambda: "v2", mru_prefix="p")
        assert len(_recovered_infos(logs)) == 1
        # …and the latch is re-armed: the NEXT outage WARNs again.
        with capture_logs() as logs:
            cache.serve_last_known("p", compute=lambda: "fresh")
        assert len(_outage_warns(logs)) == 1

    def test_seed_arm_warns_with_the_fresh_seeded_label(self) -> None:
        cache = MoversSnapshotCache()
        with capture_logs() as logs:
            cache.serve_last_known("p", compute=lambda: "seed")
        warns = _outage_warns(logs)
        assert len(warns) == 1
        assert warns[0]["served"] == "fresh_seeded"

    def test_seed_after_a_latched_serve_stays_silent(self) -> None:
        """A mid-outage eviction's re-seed does NOT re-WARN — one WARN per outage."""
        cache = MoversSnapshotCache(capacity=1)
        cache.get_or_compute("p|rev=1", generation=0, compute=lambda: "v1", mru_prefix="p")
        with capture_logs() as logs:
            cache.serve_last_known("p", compute=lambda: "fresh")  # WARN #1 (last_known)
            cache.get_or_compute("other", generation=0, compute=lambda: "x")  # evicts
            value, served = cache.serve_last_known("p", compute=lambda: "reseed")
        assert (value, served) == ("reseed", False)
        assert len(_outage_warns(logs)) == 1  # the seed arm saw the armed latch

    def test_recovery_via_clean_hit_clears_the_latch(self) -> None:
        """A latched prefix whose SAME-generation entry hits clean also recovers."""
        cache = MoversSnapshotCache()
        cache.get_or_compute("p|rev=1", generation=3, compute=lambda: "v1", mru_prefix="p")
        cache.serve_last_known("p", compute=lambda: "fresh")  # latch armed
        with capture_logs() as logs:
            hit = cache.get_or_compute(
                "p|rev=1", generation=3, compute=lambda: "never", mru_prefix="p"
            )
        assert hit == "v1"
        assert len(_recovered_infos(logs)) == 1

    def test_bare_key_caller_recovers_its_own_latch(self) -> None:
        """A stable-key caller (no mru_prefix) clears the latch keyed on its key."""
        cache = MoversSnapshotCache()
        cache.serve_last_known("stable", compute=lambda: "seed")  # latch armed on "stable"
        with capture_logs() as logs:
            cache.get_or_compute("stable", generation=1, compute=lambda: "real")
        assert len(_recovered_infos(logs)) == 1

    def test_floor_serve_also_recovers_the_latch(self) -> None:
        """A pull-time floor stale-serve is a probe SUCCESS — it clears the latch."""
        clock = {"now": 100.0}
        cache = MoversSnapshotCache(
            pull_active_probe=lambda: True, adaptive_floor=True, clock=lambda: clock["now"]
        )
        cache.get_or_compute("p|rev=1", generation=1, compute=lambda: "v1", mru_prefix="p")
        cache.serve_last_known("p", compute=lambda: "fresh")  # latch armed
        clock["now"] += 0.5  # within the 2 s base floor
        with capture_logs() as logs:
            served = cache.get_or_compute(
                "p|rev=1", generation=2, compute=lambda: "never", mru_prefix="p"
            )
        assert served == "v1"
        assert [
            e["event"] for e in logs if e["event"] != "movers_cache.revision_probe_recovered"
        ] == ["movers_cache.stale_served"]
        assert len(_recovered_infos(logs)) == 1

    def test_waiter_loop_hit_recovers_the_latch(self) -> None:
        """A single-flight WAITER whose wake finds the entry also clears the latch."""
        cache = MoversSnapshotCache()
        cache.serve_last_known("p", compute=lambda: "seed")  # latch armed on "p"
        computing = threading.Event()
        release = threading.Event()

        def _slow() -> str:
            computing.set()
            release.wait(timeout=5.0)
            return "slow-value"

        results: dict[str, str] = {}

        def _owner() -> None:
            # No mru_prefix: the owner's store must NOT clear "p"'s latch.
            results["owner"] = cache.get_or_compute("k", generation=1, compute=_slow)

        def _waiter() -> None:
            results["waiter"] = cache.get_or_compute(
                "k", generation=1, compute=lambda: "never", mru_prefix="p"
            )

        owner = threading.Thread(target=_owner)
        owner.start()
        assert computing.wait(timeout=5.0)
        waiter = threading.Thread(target=_waiter)
        waiter.start()
        with capture_logs() as logs:
            release.set()
            owner.join(timeout=5.0)
            waiter.join(timeout=5.0)
        assert results == {"owner": "slow-value", "waiter": "slow-value"}
        assert len(_recovered_infos(logs)) == 1  # the waiter's loop-hit recovered "p"
