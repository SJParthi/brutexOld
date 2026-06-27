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

from pathlib import Path

import pytest
from brutex.engine.sweep_cache import (
    SweepCache,
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
    # A fresh process re-opens: the in-RAM Bloom is empty, but the LMDB exact
    # store persisted — so the combo is still reported as already evaluated.
    with SweepCache.open(root, bloom_capacity=1000) as cache2:
        d = combo_digest(7, "NIFTY", "5m", "bull", {"sl": 100})
        # Seed the rebuilt Bloom so the exact-store confirm path is reached.
        cache2._bloom.add(d)
        assert cache2.seen_digest(d) is True
