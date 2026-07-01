"""Unit + golden + property tests for the sweep dedup cache (`brutex.engine.sweep_cache`).

100% line + branch coverage of the Bloom + LMDB O(1) membership dedup
(DEC-SWEEP-REALTIME-GUARANTEE-001, CLAUDE.md §2 Rule 11):

* :func:`combo_digest` (happy, ``params=None``, non-serialisable raise) + a
  frozen golden digest + an order-independence property;
* :class:`SweepCache` open / context-manager / idempotent close, closed-guard;
* :meth:`seen_digest` all three branches (Bloom negative fast-path, Bloom
  positive → LMDB hit, Bloom positive → LMDB miss), :meth:`record_digest`
  (new vs idempotent re-record), the combo-keyed convenience wrappers, and the
  assurance ``snapshot``;
* a hypothesis property: after ``record``, ``seen`` is always ``True`` (Bloom
  has no false negatives — the dedup correctness guarantee), and a re-open
  reloads the persisted exact-store membership.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from brutex.engine.sweep_cache import (
    SweepCache,
    SweepCacheCapacityError,
    SweepCacheError,
    combo_digest,
)
from hypothesis import given, settings
from hypothesis import strategies as st

#: Frozen golden digests — any change to the canonicalisation breaks these.
_GOLDEN = "305dd064f60094a485abdccd5fa608bfbc193bf3f3093bda803101186d04317d"
_GOLDEN_EMPTY = "50fb119cfc925983014cbac4e1f0a2dd7d73798009ccbd632743d66e39cda8b7"


# ── combo_digest ────────────────────────────────────────────────────────────


def test_golden_digest_with_params() -> None:
    assert combo_digest(7, "NIFTY", "5m", "bull", {"sl": 100}).hex() == _GOLDEN


def test_golden_digest_default_params_none() -> None:
    assert combo_digest(0, "BANKNIFTY", "1m", "bear").hex() == _GOLDEN_EMPTY


def test_digest_is_32_bytes() -> None:
    assert len(combo_digest(1, "NIFTY", "5m", "bull")) == 32


def test_digest_param_order_independent() -> None:
    a = combo_digest(1, "NIFTY", "5m", "bull", {"sl": 1, "tp": 2})
    b = combo_digest(1, "NIFTY", "5m", "bull", {"tp": 2, "sl": 1})
    assert a == b


def test_digest_non_serialisable_params_raises() -> None:
    with pytest.raises(SweepCacheError, match="not JSON-serialisable"):
        combo_digest(1, "NIFTY", "5m", "bull", {"bad": object()})


# ── combo_digest non-finite-float guard (DEC-ENGINE-DEDUP-NAN-CANON-001) ─────


def test_digest_nan_param_raises() -> None:
    """A ``NaN`` float param is rejected (one-NaN-policy, CLAUDE.md §2 Rule 11).

    The dedup-key canonicaliser uses ``allow_nan=False`` to match the
    run-identity hash (`combo.canonicalise_params`) and the completion sentinel
    (`tuple_complete.render_completion_sentinel`) — the membership key can never
    silently accept a non-finite float the identity key would refuse.
    """
    with pytest.raises(SweepCacheError, match="non-finite float"):
        combo_digest(1, "NIFTY", "5m", "bull", {"x": float("nan")})


def test_digest_inf_param_raises() -> None:
    with pytest.raises(SweepCacheError, match="non-finite float"):
        combo_digest(1, "NIFTY", "5m", "bull", {"x": float("inf")})


def test_digest_negative_inf_param_raises() -> None:
    with pytest.raises(SweepCacheError, match="non-finite float"):
        combo_digest(1, "NIFTY", "5m", "bull", {"x": float("-inf")})


def test_digest_finite_float_param_still_succeeds() -> None:
    """A FINITE float param is unaffected by the guard — a 64-hex digest."""
    digest = combo_digest(1, "NIFTY", "5m", "bull", {"x": 1.5, "y": -0.0})
    assert len(digest) == 32
    assert len(digest.hex()) == 64


# ── SweepCache lifecycle ────────────────────────────────────────────────────


def test_open_creates_and_dedups(tmp_path: Path) -> None:
    with SweepCache.open(tmp_path / "c", bloom_capacity=1000) as cache:
        assert cache.seen(7, "NIFTY", "5m", "bull", {"sl": 100}) is False
        assert cache.record(7, "NIFTY", "5m", "bull", {"sl": 100}) is True
        assert cache.seen(7, "NIFTY", "5m", "bull", {"sl": 100}) is True
        assert cache.record(7, "NIFTY", "5m", "bull", {"sl": 100}) is False  # idempotent


def test_close_is_idempotent(tmp_path: Path) -> None:
    cache = SweepCache.open(tmp_path / "c", bloom_capacity=1000)
    cache.close()
    cache.close()  # second close is a no-op


def test_use_after_close_raises(tmp_path: Path) -> None:
    cache = SweepCache.open(tmp_path / "c", bloom_capacity=1000)
    d = combo_digest(1, "NIFTY", "5m", "bull")
    cache.close()
    with pytest.raises(SweepCacheError, match="closed SweepCache"):
        cache.seen_digest(d)
    with pytest.raises(SweepCacheError, match="closed SweepCache"):
        cache.record_digest(d)


# ── seen_digest branches ────────────────────────────────────────────────────


def test_seen_digest_bloom_negative_fast_path(tmp_path: Path) -> None:
    with SweepCache.open(tmp_path / "c", bloom_capacity=1000) as cache:
        d = combo_digest(42, "NIFTY", "5m", "bull")
        assert cache.seen_digest(d) is False
        snap = cache.snapshot()
        assert snap["checks"] == 1
        assert snap["bloom_negatives"] == 1
        assert snap["hits"] == 0


def test_seen_digest_bloom_positive_lmdb_hit(tmp_path: Path) -> None:
    with SweepCache.open(tmp_path / "c", bloom_capacity=1000) as cache:
        d = combo_digest(7, "NIFTY", "5m", "bull")
        cache.record_digest(d)
        assert cache.seen_digest(d) is True
        assert cache.snapshot()["hits"] == 1


def test_seen_digest_bloom_positive_lmdb_miss(tmp_path: Path) -> None:
    # Force the rare "Bloom says maybe, LMDB says no" path by seeding the Bloom
    # directly without recording to the exact store.
    with SweepCache.open(tmp_path / "c", bloom_capacity=1000) as cache:
        d = combo_digest(99, "NIFTY", "5m", "bull")
        cache._bloom.add(d)
        assert cache.seen_digest(d) is False
        snap = cache.snapshot()
        assert snap["bloom_negatives"] == 0  # Bloom did NOT fast-fail
        assert snap["hits"] == 0  # LMDB ground-truth said not present


# ── record_digest + snapshot ────────────────────────────────────────────────


def test_record_digest_new_then_idempotent(tmp_path: Path) -> None:
    with SweepCache.open(tmp_path / "c", bloom_capacity=1000) as cache:
        d = combo_digest(3, "NIFTY", "5m", "bull")
        assert cache.record_digest(d) is True
        assert cache.record_digest(d) is False
        snap = cache.snapshot()
        assert snap["records"] == 1
        assert snap["bloom_insertions"] == 1  # idempotent re-record never re-inserts
        assert snap["bloom_bytes"] > 0
        assert snap["closed"] is False


# ── property + persistence ──────────────────────────────────────────────────


# deadline=None: each example opens a SweepCache (an LMDB environment ON
# DISK) — env create + mmap under a loaded runner routinely exceeds
# hypothesis's default 200 ms per-example deadline, making the test flake
# ~20% under contention (measured 4/20 locally, DEC-SWEEP-CACHE-DEADLINE-001).
# The house convention for I/O-touching property tests (tests/property/*)
# is deadline=None; correctness is untouched — examples still run.
@settings(deadline=None)
@given(
    combos=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=2**16),
            st.sampled_from(["NIFTY", "BANKNIFTY", "SENSEX"]),
            st.sampled_from(["1m", "5m", "15m"]),
            st.sampled_from(["bull", "bear"]),
        ),
        min_size=1,
        max_size=20,
        unique=True,
    )
)
def test_property_recorded_combos_always_seen(
    tmp_path_factory: pytest.TempPathFactory, combos: list[tuple[int, str, str, str]]
) -> None:
    d = tmp_path_factory.mktemp("kc")
    with SweepCache.open(d / "c", bloom_capacity=100_000) as cache:
        for mask, instrument, tf, direction in combos:
            cache.record(mask, instrument, tf, direction)
        # No false negatives — every recorded combo must be reported seen.
        for mask, instrument, tf, direction in combos:
            assert cache.seen(mask, instrument, tf, direction) is True


def test_reopen_reloads_persisted_membership(tmp_path: Path) -> None:
    root = tmp_path / "c"
    with SweepCache.open(root, bloom_capacity=1000) as cache:
        cache.record(7, "NIFTY", "5m", "bull", {"sl": 100})
    # A fresh process re-opens: the in-RAM Bloom is repopulated from the persisted
    # LMDB exact store on open(), so the combo is reported as already evaluated
    # WITHOUT any manual Bloom seed (the resume-dedup guarantee, Golden Rule #7).
    with SweepCache.open(root, bloom_capacity=1000) as cache2:
        d = combo_digest(7, "NIFTY", "5m", "bull", {"sl": 100})
        assert cache2.seen_digest(d) is True


# ── resume-dedup: the CRITICAL fix (DEC-SWEEP-CACHE-RESUME-DEDUP-001) ─────────


def test_resume_dedups_all_previously_recorded_without_manual_seed(
    tmp_path: Path,
) -> None:
    """Every digest recorded last run is reported ``seen`` after a RESUME.

    THE CRITICAL fix: on the first run N distinct combos are recorded (persisted
    to the L2 LMDB). A fresh :class:`SweepCache` re-opened on the SAME path
    repopulates its in-RAM L1 Bloom from that persisted store, so ``seen`` is
    ``True`` for all N with NO manual ``_bloom.add`` — the resume skips the prior
    sweep instead of re-running it (CLAUDE.md §2 Rules 4 & 7).

    Fail-before/pass-after oracle: without the ``open()`` Bloom-repopulate the
    fresh Bloom is empty, ``might_contain`` is ``False`` for every persisted
    digest, and ``seen`` returns ``False`` → each combo is RE-RUN.
    """
    root = tmp_path / "c"
    combos = [(mask, "NIFTY", "5m", "bull", {"sl": mask * 10}) for mask in range(1, 51)]
    with SweepCache.open(root, bloom_capacity=1000) as first:
        for mask, inst, tf, direction, params in combos:
            assert first.record(mask, inst, tf, direction, params) is True

    # Fresh instance, same path → resume.
    with SweepCache.open(root, bloom_capacity=1000) as resumed:
        for mask, inst, tf, direction, params in combos:
            assert resumed.seen(mask, inst, tf, direction, params) is True
        # None hit the Bloom-negative fast path — the Bloom was repopulated.
        snap = resumed.snapshot()
        assert snap["bloom_negatives"] == 0
        assert snap["hits"] == len(combos)
        # A genuinely-new combo is still correctly reported unseen (Bloom-negative).
        assert resumed.seen(999, "NIFTY", "5m", "bull") is False


def test_cold_open_repopulate_is_noop(tmp_path: Path) -> None:
    """A cold start (empty exact store) repopulates nothing — no spurious inserts."""
    with SweepCache.open(tmp_path / "c", bloom_capacity=1000) as cache:
        assert cache.snapshot()["bloom_insertions"] == 0


def test_resume_bloom_capacity_exceeded_fails_closed(tmp_path: Path) -> None:
    """A resume whose persisted count exceeds the Bloom capacity FAILS_CLOSED.

    Re-opening with a SMALLER ``bloom_capacity`` than the persisted digest count
    would overflow the Bloom's FAIL_CLOSED gate mid-rebuild — instead
    :meth:`open` raises :class:`SweepCacheCapacityError` up front so the operator
    re-opens with a larger capacity (Golden Rule #5, no silent FPR drift).
    """
    root = tmp_path / "c"
    with SweepCache.open(root, bloom_capacity=1000) as first:
        for mask in range(5):
            first.record(mask, "NIFTY", "5m", "bull")
    with pytest.raises(SweepCacheCapacityError, match="persisted exact-store"):
        SweepCache.open(root, bloom_capacity=3)


# ── concurrency: atomic check-then-act (DEC-SWEEP-CACHE-RESUME-DEDUP-001) ─────


def test_concurrent_record_admits_digest_exactly_once(tmp_path: Path) -> None:
    """N threads recording the SAME new digest admit it exactly once.

    Mirrors the OptionDedupIndex concurrency contract: the probe-then-insert is
    ONE critical section, so exactly ONE thread sees the digest as new
    (``record_digest`` returns ``True`` once), the Bloom is inserted into exactly
    once (``bloom_insertions == 1``, never N), and the internal ``records``
    counter is 1 — no double-count against the FAIL_CLOSED capacity, no
    double-evaluation.

    Fail-before/pass-after oracle: with the probe + insert OUTSIDE the lock, two
    threads both pass ``digest not in self._exact`` and both ``bloom.add`` →
    ``bloom_insertions``/``records`` inflate above 1.
    """
    d = combo_digest(7, "NIFTY", "5m", "bull", {"sl": 100})
    n_threads = 8
    with SweepCache.open(tmp_path / "c", bloom_capacity=1000) as cache:
        start = threading.Barrier(n_threads)
        results: list[bool] = []
        results_lock = threading.Lock()

        def worker() -> None:
            start.wait()
            was_new = cache.record_digest(d)
            with results_lock:
                results.append(was_new)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(results) == 1  # exactly one "was new"
        snap = cache.snapshot()
        assert snap["bloom_insertions"] == 1
        assert snap["records"] == 1
        assert cache.seen_digest(d) is True
