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

R2-SL-F4-F5 additions: the operator knobs (``bloom_capacity`` / ``map_size_mb``
resolve explicit > env > module default via
``resolve_sweep_cache_bloom_capacity`` / ``resolve_sweep_cache_map_mb``), the
actionable FAIL_CLOSED resume-refusal message + the proof the advertised
larger-knob remedy admits the resume, and the ``record_digest`` exact-first
durability ordering (a map-full during the L2 write leaves NO phantom L1 Bloom
entry, so a retry after growing the map neither double-counts nor
false-positive-skips).

Whitebox: the Bloom-seed / knob / ordering tests reach the private ``_bloom``
/ ``_exact`` tiers (DEC-P0-017), so the file silences ``reportPrivateUsage``.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, cast

import lmdb
import pytest
from brutex.data.lmdb_membership import (
    SWEEP_CACHE_MAP_MB_ENV,
    LmdbCorruptEnvError,
    LmdbMapFullError,
    LmdbMembership,
)
from brutex.engine.sweep_cache import (
    DEFAULT_BLOOM_CAPACITY,
    DEFAULT_MAP_SIZE_MB,
    SWEEP_CACHE_BLOOM_CAPACITY_ENV,
    SweepCache,
    SweepCacheCapacityError,
    SweepCacheError,
    _auto_grow_capacity,
    combo_digest,
    resolve_sweep_cache_bloom_capacity,
    resolve_sweep_cache_map_mb,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

#: A persisted count whose Bloom bit array at the default 1e-7 FPR sizes over
#: the 16 GiB ``MAX_BLOOM_BYTES`` wall (~4.19 bytes/item ⇒ ~21 GB) — used to
#: drive the wall arm as PURE MATH, never a real 16 GiB allocation.
_OVER_WALL_PERSISTED = 5_000_000_000

_MIB = 1024 * 1024

#: The suite's fixed reproducibility hashes — the three STRUCTURAL keyword-only
#: parameters of :func:`combo_digest` (DEC-COMBO-DIGEST-REPRO-HASHES-001,
#: CLAUDE.md §2 Golden Rule #4 run-identity formula).
_SNAP = "snap-aaaa"
_IND = "ind-v1"
_SHA = "c0ffee01"

#: Keyword bundle for the ``SweepCache.seen`` / ``.record`` convenience calls.
_REPRO_KW: dict[str, str] = {
    "data_snapshot_hash": _SNAP,
    "indicator_version": _IND,
    "code_commit_sha": _SHA,
}


def _digest(
    mask: int,
    instrument: str,
    tf: str,
    direction: str,
    params: dict[str, Any] | None = None,
) -> bytes:
    """``combo_digest`` bound to the suite's fixed reproducibility hashes."""
    return combo_digest(
        mask,
        instrument,
        tf,
        direction,
        params,
        data_snapshot_hash=_SNAP,
        indicator_version=_IND,
        code_commit_sha=_SHA,
    )


#: Frozen golden digests — any change to the canonicalisation breaks these.
#: CAPTURED FROM THE PRE-CHANGE (main) combo_digest under the LEGACY convention
#: (the three hashes injected INSIDE ``canonical_params`` by the caller), so
#: they double as the dedup-store byte-identity contract of
#: DEC-COMBO-DIGEST-REPRO-HASHES-001: the structural-kwargs API must reproduce
#: them EXACTLY (existing Bloom/LMDB stores are not invalidated).
_GOLDEN = "f940a93bdb390e459fc2b2877f8e874281ac738fcece153fe411240f143aa362"
_GOLDEN_EMPTY = "3f542e4994a63e5c99cf1b447a0f070fcffcb98e58c8441b79384f4b79be6737"

#: PRE-CHANGE capture of a full PRODUCTION-shaped ``_tuple_cache_params``-style
#: payload (every supervisor identity leg) under the legacy in-params
#: convention — the strongest byte-identity pin (the exact layout the live
#: Bloom/LMDB dedup stores were keyed with).
_GOLDEN_PRODUCTION_SHAPE = "d65aa979b0c2cfe6e3f0c7356f3be058953cc190bf5f4a12ee28dacdd5efdd20"


# ── combo_digest ────────────────────────────────────────────────────────────


def test_golden_digest_with_params() -> None:
    assert _digest(7, "NIFTY", "5m", "bull", {"sl": 100}).hex() == _GOLDEN


def test_golden_digest_default_params_none() -> None:
    assert _digest(0, "BANKNIFTY", "1m", "bear").hex() == _GOLDEN_EMPTY


def test_digest_is_32_bytes() -> None:
    assert len(_digest(1, "NIFTY", "5m", "bull")) == 32


def test_digest_param_order_independent() -> None:
    a = _digest(1, "NIFTY", "5m", "bull", {"sl": 1, "tp": 2})
    b = _digest(1, "NIFTY", "5m", "bull", {"tp": 2, "sl": 1})
    assert a == b


def test_digest_non_serialisable_params_raises() -> None:
    with pytest.raises(SweepCacheError, match="not JSON-serialisable"):
        _digest(1, "NIFTY", "5m", "bull", {"bad": object()})


# ── combo_digest structural reproducibility hashes ───────────────────────────
# DEC-COMBO-DIGEST-REPRO-HASHES-001: Golden Rule #4's three run-identity hashes
# are REQUIRED keyword-only parameters — a caller can no longer forget them and
# silently alias dedup entries across data snapshots (the stale-skip bug).


class TestReproHashesStructural:
    def test_byte_identical_to_legacy_in_params_convention(self) -> None:
        """The structural API reproduces the PRE-change digests EXACTLY.

        The three hex constants were captured by running the PRE-change (main)
        ``combo_digest`` with the hashes injected inside ``canonical_params``
        (the legacy supervisor convention). Byte-identity means existing
        Bloom/LMDB dedup stores and run identities are NOT invalidated.
        """
        assert _digest(7, "NIFTY", "5m", "bull", {"sl": 100}).hex() == _GOLDEN
        assert _digest(0, "BANKNIFTY", "1m", "bear").hex() == _GOLDEN_EMPTY
        production_params: dict[str, Any] = {
            "k": 2,
            "condition_bits": [0, 5, 9],
            "tf_minutes": 5,
            "sl_paisa": 8000,
            "tp_paisa": 5000,
            "proximity_bps": [10, 15, 20],
            "fib_anchor": "daily",
            "prior_n": 3,
            "start_equity_paisa": 10_000_000_00,
            "mode": "signal_only",
            "top_k": 50,
            "min_hits": 30,
            "max_combos": None,
        }
        digest = combo_digest(
            3,
            "NIFTY",
            "5minute",
            "bull",
            production_params,
            data_snapshot_hash="snap-prod-1",
            indicator_version="ind-prod-1",
            code_commit_sha="sha-prod-1",
        )
        assert digest.hex() == _GOLDEN_PRODUCTION_SHAPE

    def test_each_hash_moves_the_digest(self) -> None:
        """Changing ANY one reproducibility hash changes the digest.

        The structural fix's teeth: a combo evaluated on OLD data can never be
        skipped on NEW data (different ``data_snapshot_hash`` → different dedup
        key), and an indicator / code change likewise re-keys.
        """
        base = _digest(7, "NIFTY", "5m", "bull", {"sl": 100})
        changed_snap = combo_digest(
            7,
            "NIFTY",
            "5m",
            "bull",
            {"sl": 100},
            data_snapshot_hash="snap-CHANGED",
            indicator_version=_IND,
            code_commit_sha=_SHA,
        )
        changed_ind = combo_digest(
            7,
            "NIFTY",
            "5m",
            "bull",
            {"sl": 100},
            data_snapshot_hash=_SNAP,
            indicator_version="ind-CHANGED",
            code_commit_sha=_SHA,
        )
        changed_sha = combo_digest(
            7,
            "NIFTY",
            "5m",
            "bull",
            {"sl": 100},
            data_snapshot_hash=_SNAP,
            indicator_version=_IND,
            code_commit_sha="sha-CHANGED",
        )
        assert base not in {changed_snap, changed_ind, changed_sha}
        assert len({changed_snap, changed_ind, changed_sha}) == 3

    def test_omitting_the_hashes_is_a_typeerror(self) -> None:
        """A caller omitting the hashes fails LOUDLY — never a silent alias.

        ``cast(Any, …)`` bypasses the static signature so the RUNTIME contract
        is what is pinned here; the static contract (mypy/pyright refusing the
        call) is enforced by the required keyword-only parameters themselves.
        """
        with pytest.raises(TypeError, match="data_snapshot_hash"):
            cast("Any", combo_digest)(1, "NIFTY", "5m", "bull")

    def test_omitting_the_hashes_on_seen_and_record_is_a_typeerror(self, tmp_path: Path) -> None:
        with SweepCache.open(tmp_path / "c", bloom_capacity=1000) as cache:
            with pytest.raises(TypeError, match="data_snapshot_hash"):
                cast("Any", cache.seen)(7, "NIFTY", "5m", "bull")
            with pytest.raises(TypeError, match="data_snapshot_hash"):
                cast("Any", cache.record)(7, "NIFTY", "5m", "bull")

    @pytest.mark.parametrize(
        "reserved", ["data_snapshot_hash", "indicator_version", "code_commit_sha"]
    )
    def test_reserved_key_inside_params_raises(self, reserved: str) -> None:
        """The structural kwargs are the ONE channel — an in-``params`` copy is
        refused loudly (two channels could silently disagree)."""
        with pytest.raises(SweepCacheError, match="RESERVED reproducibility"):
            _digest(1, "NIFTY", "5m", "bull", {reserved: "sneaky", "sl": 1})


# ── combo_digest non-finite-float guard (DEC-ENGINE-DEDUP-NAN-CANON-001) ─────


def test_digest_nan_param_raises() -> None:
    """A ``NaN`` float param is rejected (one-NaN-policy, CLAUDE.md §2 Rule 11).

    The dedup-key canonicaliser uses ``allow_nan=False`` to match the
    run-identity hash (`combo.canonicalise_params`) and the completion sentinel
    (`tuple_complete.render_completion_sentinel`) — the membership key can never
    silently accept a non-finite float the identity key would refuse.
    """
    with pytest.raises(SweepCacheError, match="non-finite float"):
        _digest(1, "NIFTY", "5m", "bull", {"x": float("nan")})


def test_digest_inf_param_raises() -> None:
    with pytest.raises(SweepCacheError, match="non-finite float"):
        _digest(1, "NIFTY", "5m", "bull", {"x": float("inf")})


def test_digest_negative_inf_param_raises() -> None:
    with pytest.raises(SweepCacheError, match="non-finite float"):
        _digest(1, "NIFTY", "5m", "bull", {"x": float("-inf")})


def test_digest_finite_float_param_still_succeeds() -> None:
    """A FINITE float param is unaffected by the guard — a 64-hex digest."""
    digest = _digest(1, "NIFTY", "5m", "bull", {"x": 1.5, "y": -0.0})
    assert len(digest) == 32
    assert len(digest.hex()) == 64


# ── SweepCache lifecycle ────────────────────────────────────────────────────


def test_open_creates_and_dedups(tmp_path: Path) -> None:
    with SweepCache.open(tmp_path / "c", bloom_capacity=1000) as cache:
        assert cache.seen(7, "NIFTY", "5m", "bull", {"sl": 100}, **_REPRO_KW) is False
        assert cache.record(7, "NIFTY", "5m", "bull", {"sl": 100}, **_REPRO_KW) is True
        assert cache.seen(7, "NIFTY", "5m", "bull", {"sl": 100}, **_REPRO_KW) is True
        assert (
            cache.record(7, "NIFTY", "5m", "bull", {"sl": 100}, **_REPRO_KW) is False
        )  # idempotent


def test_close_is_idempotent(tmp_path: Path) -> None:
    cache = SweepCache.open(tmp_path / "c", bloom_capacity=1000)
    cache.close()
    cache.close()  # second close is a no-op


def test_use_after_close_raises(tmp_path: Path) -> None:
    cache = SweepCache.open(tmp_path / "c", bloom_capacity=1000)
    d = _digest(1, "NIFTY", "5m", "bull")
    cache.close()
    with pytest.raises(SweepCacheError, match="closed SweepCache"):
        cache.seen_digest(d)
    with pytest.raises(SweepCacheError, match="closed SweepCache"):
        cache.record_digest(d)


# ── seen_digest branches ────────────────────────────────────────────────────


def test_seen_digest_bloom_negative_fast_path(tmp_path: Path) -> None:
    with SweepCache.open(tmp_path / "c", bloom_capacity=1000) as cache:
        d = _digest(42, "NIFTY", "5m", "bull")
        assert cache.seen_digest(d) is False
        snap = cache.snapshot()
        assert snap["checks"] == 1
        assert snap["bloom_negatives"] == 1
        assert snap["hits"] == 0


def test_seen_digest_bloom_positive_lmdb_hit(tmp_path: Path) -> None:
    with SweepCache.open(tmp_path / "c", bloom_capacity=1000) as cache:
        d = _digest(7, "NIFTY", "5m", "bull")
        cache.record_digest(d)
        assert cache.seen_digest(d) is True
        assert cache.snapshot()["hits"] == 1


def test_seen_digest_bloom_positive_lmdb_miss(tmp_path: Path) -> None:
    # Force the rare "Bloom says maybe, LMDB says no" path by seeding the Bloom
    # directly without recording to the exact store.
    with SweepCache.open(tmp_path / "c", bloom_capacity=1000) as cache:
        d = _digest(99, "NIFTY", "5m", "bull")
        cache._bloom.add(d)
        assert cache.seen_digest(d) is False
        snap = cache.snapshot()
        assert snap["bloom_negatives"] == 0  # Bloom did NOT fast-fail
        assert snap["hits"] == 0  # LMDB ground-truth said not present


# ── record_digest + snapshot ────────────────────────────────────────────────


def test_record_digest_new_then_idempotent(tmp_path: Path) -> None:
    with SweepCache.open(tmp_path / "c", bloom_capacity=1000) as cache:
        d = _digest(3, "NIFTY", "5m", "bull")
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
            cache.record(mask, instrument, tf, direction, None, **_REPRO_KW)
        # No false negatives — every recorded combo must be reported seen.
        for mask, instrument, tf, direction in combos:
            assert cache.seen(mask, instrument, tf, direction, None, **_REPRO_KW) is True


def test_reopen_reloads_persisted_membership(tmp_path: Path) -> None:
    root = tmp_path / "c"
    with SweepCache.open(root, bloom_capacity=1000) as cache:
        cache.record(7, "NIFTY", "5m", "bull", {"sl": 100}, **_REPRO_KW)
    # A fresh process re-opens: the in-RAM Bloom is repopulated from the persisted
    # LMDB exact store on open(), so the combo is reported as already evaluated
    # WITHOUT any manual Bloom seed (the resume-dedup guarantee, Golden Rule #7).
    with SweepCache.open(root, bloom_capacity=1000) as cache2:
        d = _digest(7, "NIFTY", "5m", "bull", {"sl": 100})
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
            assert first.record(mask, inst, tf, direction, params, **_REPRO_KW) is True

    # Fresh instance, same path → resume.
    with SweepCache.open(root, bloom_capacity=1000) as resumed:
        for mask, inst, tf, direction, params in combos:
            assert resumed.seen(mask, inst, tf, direction, params, **_REPRO_KW) is True
        # None hit the Bloom-negative fast path — the Bloom was repopulated.
        snap = resumed.snapshot()
        assert snap["bloom_negatives"] == 0
        assert snap["hits"] == len(combos)
        # A genuinely-new combo is still correctly reported unseen (Bloom-negative).
        assert resumed.seen(999, "NIFTY", "5m", "bull", None, **_REPRO_KW) is False


def test_cold_open_repopulate_is_noop(tmp_path: Path) -> None:
    """A cold start (empty exact store) repopulates nothing — no spurious inserts."""
    with SweepCache.open(tmp_path / "c", bloom_capacity=1000) as cache:
        assert cache.snapshot()["bloom_insertions"] == 0


def test_resume_persisted_over_capacity_auto_grows_within_wall(tmp_path: Path) -> None:
    """STEP-1 auto-grow (DEC-ENGINE-MISC-PACK-001): a within-wall >capacity resume OPENS.

    Regression flip of the old unconditional FAIL_CLOSED: persisted=5 against a
    configured capacity of 3 now auto-grows to ``max(5, ceil(3 × 1.25)=4) = 5``,
    every persisted digest reads Bloom-positive (the resume-dedup guarantee),
    and EXACTLY ONE loud ``sweep_cache.bloom_auto_grow`` event names the
    deviation from the operator knob.
    """
    root = tmp_path / "c"
    with SweepCache.open(root, bloom_capacity=1000) as first:
        for mask in range(5):
            first.record(mask, "NIFTY", "5m", "bull", None, **_REPRO_KW)
    with capture_logs() as logs, SweepCache.open(root, bloom_capacity=3) as resumed:
        assert resumed._bloom.capacity == 5
        for mask in range(5):
            assert resumed.seen(mask, "NIFTY", "5m", "bull", None, **_REPRO_KW) is True
    grows = [e for e in logs if e["event"] == "sweep_cache.bloom_auto_grow"]
    assert len(grows) == 1
    assert grows[0]["persisted"] == 5
    assert grows[0]["configured_capacity"] == 3
    assert grows[0]["grown_capacity"] == 5
    assert grows[0]["m_bytes"] > 0


def test_resume_within_capacity_never_grows_and_never_logs(tmp_path: Path) -> None:
    """A ``persisted <= capacity`` resume is byte-identical: no grow, no log."""
    root = tmp_path / "c"
    with SweepCache.open(root, bloom_capacity=1000) as first:
        for mask in range(5):
            first.record(mask, "NIFTY", "5m", "bull", None, **_REPRO_KW)
    with capture_logs() as logs, SweepCache.open(root, bloom_capacity=1000) as resumed:
        assert resumed._bloom.capacity == 1000
    assert not [e for e in logs if e["event"] == "sweep_cache.bloom_auto_grow"]


def test_rebuild_guard_stays_as_defence_in_depth() -> None:
    """The `_rebuild_bloom_from_exact` >capacity guard is KEPT (invariant check).

    Unreachable from :meth:`SweepCache.open` now that the capacity is pre-sized
    (``_auto_grow_capacity`` returns ``>= persisted``), but a DIRECT caller that
    under-sizes still FAILS_CLOSED before any partial rebuild.
    """

    class _FakeExact:
        def __len__(self) -> int:
            return 5

        def iter_keys(self) -> list[bytes]:  # pragma: no cover — never reached
            return []

    with pytest.raises(SweepCacheCapacityError, match="persisted exact-store"):
        SweepCache._rebuild_bloom_from_exact(
            cast(Any, object()), cast(Any, _FakeExact()), bloom_capacity=3
        )


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
    d = _digest(7, "NIFTY", "5m", "bull", {"sl": 100})
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


# ── operator knobs: explicit > env > default (R2-SL-F4-F5) ───────────────────


class TestOperatorKnobResolvers:
    """The two pure resolvers — every arm, injected env (no os.environ leak)."""

    def test_map_mb_default_when_unset(self) -> None:
        assert resolve_sweep_cache_map_mb(env={}) == DEFAULT_MAP_SIZE_MB

    def test_map_mb_env_wins_over_default(self) -> None:
        assert resolve_sweep_cache_map_mb(env={SWEEP_CACHE_MAP_MB_ENV: "4096"}) == 4096

    def test_map_mb_explicit_wins_over_env(self) -> None:
        assert resolve_sweep_cache_map_mb(3, env={SWEEP_CACHE_MAP_MB_ENV: "4096"}) == 3

    def test_map_mb_bad_env_raises_naming_the_var(self) -> None:
        with pytest.raises(ValueError, match=SWEEP_CACHE_MAP_MB_ENV):
            resolve_sweep_cache_map_mb(env={SWEEP_CACHE_MAP_MB_ENV: "two-gigs"})

    def test_bloom_capacity_default_when_unset(self) -> None:
        assert resolve_sweep_cache_bloom_capacity(env={}) == DEFAULT_BLOOM_CAPACITY

    def test_bloom_capacity_env_wins_over_default(self) -> None:
        got = resolve_sweep_cache_bloom_capacity(env={SWEEP_CACHE_BLOOM_CAPACITY_ENV: "6000000"})
        assert got == 6_000_000

    def test_bloom_capacity_explicit_wins_over_env(self) -> None:
        got = resolve_sweep_cache_bloom_capacity(
            1000, env={SWEEP_CACHE_BLOOM_CAPACITY_ENV: "6000000"}
        )
        assert got == 1000

    def test_bloom_capacity_bad_env_raises_naming_the_var(self) -> None:
        with pytest.raises(ValueError, match=SWEEP_CACHE_BLOOM_CAPACITY_ENV):
            resolve_sweep_cache_bloom_capacity(env={SWEEP_CACHE_BLOOM_CAPACITY_ENV: "-1"})


class TestOpenKnobThreading:
    """`SweepCache.open` honours the knobs end-to-end (and defaults unchanged)."""

    def test_default_open_keeps_todays_sizing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No knobs set ⇒ byte-identical defaults (5M Bloom / 2 GiB map)."""
        monkeypatch.delenv(SWEEP_CACHE_BLOOM_CAPACITY_ENV, raising=False)
        monkeypatch.delenv(SWEEP_CACHE_MAP_MB_ENV, raising=False)
        with SweepCache.open(tmp_path / "c") as cache:
            assert cache._bloom.capacity == DEFAULT_BLOOM_CAPACITY
            assert cache._exact._env.info()["map_size"] == DEFAULT_MAP_SIZE_MB * _MIB

    def test_map_env_var_threads_into_exact_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SWEEP_CACHE_MAP_MB_ENV, "1")
        with SweepCache.open(tmp_path / "c", bloom_capacity=1000) as cache:
            assert cache._exact._env.info()["map_size"] == 1 * _MIB

    def test_explicit_map_wins_over_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SWEEP_CACHE_MAP_MB_ENV, "1")
        with SweepCache.open(tmp_path / "c", bloom_capacity=1000, map_size_mb=2) as cache:
            assert cache._exact._env.info()["map_size"] == 2 * _MIB

    def test_bloom_env_var_threads_into_bloom(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SWEEP_CACHE_BLOOM_CAPACITY_ENV, "1234")
        with SweepCache.open(tmp_path / "c") as cache:
            assert cache._bloom.capacity == 1234

    def test_bad_env_knob_fails_open_loudly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SWEEP_CACHE_BLOOM_CAPACITY_ENV, "0")
        with pytest.raises(ValueError, match=SWEEP_CACHE_BLOOM_CAPACITY_ENV):
            SweepCache.open(tmp_path / "c")


class TestAutoGrowCapacity:
    """The STEP-1 pure grow decision (`_auto_grow_capacity`, DEC-ENGINE-MISC-PACK-001)."""

    def test_within_capacity_is_identity(self) -> None:
        """``persisted <= capacity`` returns the configured knob unchanged."""
        with capture_logs() as logs:
            assert _auto_grow_capacity(1000, 0, 1e-7) == 1000
            assert _auto_grow_capacity(1000, 1000, 1e-7) == 1000
        assert not logs  # no grow, no log — byte-identical behaviour.

    def test_grow_takes_max_of_persisted_and_ratio(self) -> None:
        """``grown = max(persisted, ceil(capacity × 1.25))`` — both arms.

        The ``max`` is the correctness clamp: a bare ``ceil(cap × 1.25)`` at
        ``persisted=500, cap=100`` would return 125 < 500 and overflow the
        FAIL_CLOSED Bloom gate mid-rebuild.
        """
        assert _auto_grow_capacity(100, 101, 1e-7) == 125  # ratio arm wins.
        assert _auto_grow_capacity(100, 500, 1e-7) == 500  # persisted arm wins.

    def test_grow_emits_one_loud_event(self) -> None:
        with capture_logs() as logs:
            _auto_grow_capacity(100, 500, 1e-7)
        grows = [e for e in logs if e["event"] == "sweep_cache.bloom_auto_grow"]
        assert len(grows) == 1
        assert grows[0]["grown_capacity"] == 500

    def test_above_wall_fails_closed_naming_the_sharded_follow_up(self) -> None:
        """The 16 GiB wall arm stays FAIL_CLOSED — pure math, no allocation.

        The message must name the HONEST above-the-wall remedies (a
        ``bloom_capacity`` raise cannot help there): loosening ``bloom_fpr``
        and the sharded / mmap'd Bloom follow-up.
        """
        with pytest.raises(SweepCacheCapacityError) as excinfo:
            _auto_grow_capacity(1000, _OVER_WALL_PERSISTED, 1e-7)
        msg = str(excinfo.value)
        assert "MAX_BLOOM_BYTES" in msg
        assert "bloom_fpr" in msg
        assert "sharded" in msg
        assert "--sweep-cache-bloom-capacity" in msg  # named as NOT the remedy.
        assert SWEEP_CACHE_BLOOM_CAPACITY_ENV in msg

    def test_looser_fpr_shrinks_the_same_persisted_under_the_wall(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The advertised remedy WORKS: the wall-busting count fits at a looser FPR.

        The wall is PINNED to the 16 GiB ``MAX_BLOOM_BYTES_FLOOR`` for this
        arithmetic: since DEC-RAM-BUDGETS-001 the live ``MAX_BLOOM_BYTES`` is
        RAM-derived (total RAM × 1/3, ``brutex/orchestrator/bloom.py``), so on
        a 16 GB CI runner it resolves ~5.6 GB — BELOW the ~5.99 GB this
        5-billion-digest @ 1e-2 case needs. The remedy claim under test is the
        FPR-loosening math relative to a FIXED wall, not the machine's RAM, so
        the pin keeps the case machine-independent (``_auto_grow_capacity``
        reads the ceiling via its function-level ``from brutex.orchestrator
        .bloom import MAX_BLOOM_BYTES``, so patching the bloom module attribute
        is authoritative here).
        """
        from brutex.orchestrator import bloom as bloom_mod

        monkeypatch.setattr(
            bloom_mod, "MAX_BLOOM_BYTES", bloom_mod.MAX_BLOOM_BYTES_FLOOR, raising=True
        )
        grown = _auto_grow_capacity(1000, _OVER_WALL_PERSISTED, 1e-2)
        assert grown == _OVER_WALL_PERSISTED


class TestCapacityRefusalRemedy:
    """The wall refusal is ACTIONABLE, leak-free, and the knob still pre-sizes."""

    @staticmethod
    def _seed(root: Path, count: int) -> None:
        with SweepCache.open(root, bloom_capacity=1000) as cache:
            for mask in range(count):
                assert cache.record(mask, "NIFTY", "5m", "bull", None, **_REPRO_KW) is True

    def test_wall_refusal_closes_env_and_reopen_admits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An over-the-wall resume refuses LOUDLY and leaks no LMDB env.

        The wall arm is driven through the REAL ``open`` path by reporting a
        wall-busting persisted count from the exact store (a genuine 5-billion
        seed / 16 GiB Bloom is not constructible in a test — Golden Rule #10);
        the refused open must close its env so the immediate re-open of the
        SAME path (with the true count restored) is safe and dedups.
        """
        root = tmp_path / "c"
        self._seed(root, 5)
        with monkeypatch.context() as patched:
            patched.setattr(
                LmdbMembership, "__len__", lambda self: _OVER_WALL_PERSISTED, raising=True
            )
            with pytest.raises(SweepCacheCapacityError, match="sharded"):
                SweepCache.open(root, bloom_capacity=3)
        with SweepCache.open(root, bloom_capacity=1000) as resumed:
            for mask in range(5):
                assert resumed.seen(mask, "NIFTY", "5m", "bull", None, **_REPRO_KW) is True

    def test_bloom_config_error_after_exact_open_leaks_no_env(self, tmp_path: Path) -> None:
        """A BloomFilter CONFIG error (fpr out of range) also closes the env.

        Regression for the open() restructure: the Bloom is now built AFTER the
        exact store opens, so ANY construction fault must ride the same
        close-before-raise cleanup (the broadened except), proven by the
        immediate re-open of the SAME path succeeding.
        """
        root = tmp_path / "c"
        self._seed(root, 5)
        from brutex.orchestrator.bloom import BloomConfigError

        with pytest.raises(BloomConfigError):
            SweepCache.open(root, bloom_capacity=1000, bloom_fpr=0.9)
        with SweepCache.open(root, bloom_capacity=1000) as resumed:
            for mask in range(5):
                assert resumed.seen(mask, "NIFTY", "5m", "bull", None, **_REPRO_KW) is True

    def test_env_knob_small_capacity_self_heals_by_auto_grow(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The env spelling of the knob also rides the auto-grow (no refusal)."""
        root = tmp_path / "c"
        self._seed(root, 5)
        monkeypatch.setenv(SWEEP_CACHE_BLOOM_CAPACITY_ENV, "3")
        with capture_logs() as logs, SweepCache.open(root) as resumed:
            for mask in range(5):
                assert resumed.seen(mask, "NIFTY", "5m", "bull", None, **_REPRO_KW) is True
        assert [e for e in logs if e["event"] == "sweep_cache.bloom_auto_grow"]

    def test_explicit_knob_still_pre_sizes_without_grow(self, tmp_path: Path) -> None:
        """A big-enough explicit knob keeps the no-grow path (knob unchanged)."""
        root = tmp_path / "c"
        self._seed(root, 5)
        with capture_logs() as logs, SweepCache.open(root, bloom_capacity=10) as resumed:
            assert resumed._bloom.capacity == 10
            for mask in range(5):
                assert resumed.seen(mask, "NIFTY", "5m", "bull", None, **_REPRO_KW) is True
        assert not [e for e in logs if e["event"] == "sweep_cache.bloom_auto_grow"]


# ── record_digest exact-first durability ordering (R2-SL-F4-F5) ──────────────


class _MapFullOnceExact:
    """ExactMembership stub: the FIRST ``add`` raises map-full, later ones land.

    Simulates the L2 store refusing a write (the map-full infra fault) so the
    ordering contract is testable deterministically: with exact-first, the
    Bloom must be untouched by the failed attempt and the retry must count
    exactly once.
    """

    def __init__(self) -> None:
        self.keys: set[bytes] = set()
        self.fail_next = True

    def __contains__(self, key: bytes, /) -> bool:
        return key in self.keys

    def add(self, key: bytes, /) -> None:
        if self.fail_next:
            self.fail_next = False
            raise LmdbMapFullError("synthetic map full")
        self.keys.add(key)

    def close(self) -> None:
        """No-op — satisfies the SweepCache close contract."""


class TestRecordDigestExactFirst:
    def test_map_full_at_exact_add_leaves_no_phantom_bloom_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REGRESSION (ordering wart): exact-first means a failed L2 write
        leaves the L1 Bloom untouched, and the retry records exactly once.

        Fail-before/pass-after oracle: with the old bloom-first ordering the
        failed attempt leaves a PHANTOM Bloom entry — ``might_contain`` is
        already ``True``, ``bloom_insertions`` is already 1, and the retry
        double-counts the digest against the FAIL_CLOSED capacity (2 total).
        """
        with SweepCache.open(tmp_path / "c", bloom_capacity=1000) as cache:
            real_exact = cache._exact
            stub = _MapFullOnceExact()
            monkeypatch.setattr(cache, "_exact", stub)
            d = _digest(7, "NIFTY", "5m", "bull", {"sl": 100})
            with pytest.raises(LmdbMapFullError, match="synthetic map full"):
                cache.record_digest(d)
            # The fault propagated with the Bloom UNTOUCHED — no phantom entry.
            assert cache._bloom.might_contain(d) is False
            snap = cache.snapshot()
            assert snap["bloom_insertions"] == 0
            assert snap["records"] == 0
            # No false-positive skip: the digest still reads unseen.
            assert cache.seen_digest(d) is False
            # Retry after "growing the map": recorded + counted EXACTLY once.
            assert cache.record_digest(d) is True
            snap = cache.snapshot()
            assert snap["bloom_insertions"] == 1
            assert snap["records"] == 1
            assert cache.seen_digest(d) is True
            # Hygiene: the displaced REAL exact store is closed explicitly
            # (the with-exit close() sees only the stub).
            real_exact.close()

    def test_real_map_full_end_to_end_then_grow_and_retry(self, tmp_path: Path) -> None:
        """A GENUINE lmdb map-full through ``record_digest`` — the full story.

        Pre-fills the exact store to the brim of a 1 MiB map, opens the cache
        over it, records fresh digests until the real ``LmdbMapFullError``
        propagates (riding the supervisor-abort contract — no swallow, no
        retry loop), asserts no phantom Bloom entry, then re-opens with a
        larger ``map_size_mb`` and proves the refused digest records cleanly.
        """
        root = tmp_path / "c"
        root.mkdir(parents=True)
        with LmdbMembership.open(root / "exact", map_size_mb=1) as pre:
            filler = 0
            while True:
                batch = [n.to_bytes(32, "big") for n in range(filler, filler + 512)]
                try:
                    pre.add_many(batch)
                except LmdbMapFullError:
                    break
                filler += 512
                assert filler < 200_000, "1 MiB map never filled — fixture broken"
            persisted = len(pre)

        with SweepCache.open(root, bloom_capacity=persisted + 8192, map_size_mb=1) as cache:
            assert cache.snapshot()["bloom_insertions"] == persisted  # rebuild ran
            failing, successes, exc = self._record_until_map_full(cache)
            assert "--sweep-cache-map-mb" in str(exc)  # actionable remedy
            assert cache._bloom.might_contain(failing) is False  # no phantom
            assert cache.snapshot()["bloom_insertions"] == persisted + successes
            assert cache.seen_digest(failing) is False  # never a phantom skip

        # Grow the map (the remedy) — the refused digest lands exactly once.
        with SweepCache.open(root, bloom_capacity=persisted + 8192, map_size_mb=4) as grown:
            assert grown.seen_digest(failing) is False
            assert grown.record_digest(failing) is True
            assert grown.seen_digest(failing) is True

    @staticmethod
    def _record_until_map_full(cache: SweepCache) -> tuple[bytes, int, LmdbMapFullError]:
        """Record fresh digests until the map refuses one.

        Returns ``(failing_digest, successes_before_failure, the_error)``.
        """
        successes = 0
        for n in range(4096):
            digest = _digest(n, "NIFTY", "5m", "bull", {"fresh": True})
            try:
                cache.record_digest(digest)
            except LmdbMapFullError as exc:
                return digest, successes, exc
            successes += 1
        raise AssertionError("map never filled within the 4096-record bound")

    def test_map_full_rides_the_sweep_abort_not_the_tuple_skip(self) -> None:
        """A map-full from ``cache.record`` must ABORT the sweep, never be
        mis-classified as a skippable per-tuple failure.

        The supervisor's per-tuple quarantine catches ``SweepComboError``
        only; the fatal infra fault must stay OUTSIDE that hierarchy so it
        propagates to the whole-sweep abort path (no swallow, no retry loop).
        """
        # Imported lazily: sweep_run pulls the Rust-wheel-bound engine chain,
        # while this module's own tests stay wheel-free (module docstring).
        from brutex.engine.sweep_run import SweepComboError

        assert not issubclass(LmdbMapFullError, SweepComboError)


# ── R2-D2 corrupt-LMDB quarantine + rebuild ───────────────────────────────────
# DEC-SWEEP-LMDB-QUARANTINE-REBUILD-001, MIGRATION.md #1631.


def _corrupt_exact_data_mdb(cache_path: Path) -> None:
    """XOR the first min(4096, len) bytes of the exact store's ``data.mdb``."""
    data_mdb = cache_path / "exact" / "data.mdb"
    raw = bytearray(data_mdb.read_bytes())
    n = min(4096, len(raw))
    for i in range(n):
        raw[i] ^= 0xFF
    data_mdb.write_bytes(bytes(raw))


class TestCorruptEnvQuarantineRebuild:
    def test_corrupt_env_quarantined_and_rebuilt(self, tmp_path: Path) -> None:
        """A corrupt exact ``data.mdb`` is quarantined + rebuilt empty (recompute-safe).

        Fix-reversion (R2-D2): pre-fix run-2 ``SweepCache.open`` raises a raw
        ``lmdb.InvalidError``; post-fix it succeeds with an EMPTY rebuilt dedup set
        (the recorded combo re-runs — results byte-identical, Golden Rule #7), the
        original bytes preserved in an ``exact.corrupt-<ns>`` sibling, and one
        ``sweep_cache.corrupt_quarantined`` WARN.
        """
        cache_path = tmp_path / "cache"
        d = _digest(7, "NIFTY", "5m", "bull", {"sl": 100})
        with SweepCache.open(cache_path, bloom_capacity=1000) as cache:
            cache.record_digest(d)
            assert cache.seen_digest(d) is True
        _corrupt_exact_data_mdb(cache_path)

        with (
            capture_logs() as logs,
            SweepCache.open(cache_path, bloom_capacity=1000) as cache2,
        ):
            # Rebuilt EMPTY — the previously-recorded digest is gone.
            assert len(cache2._exact) == 0
            assert cache2.seen_digest(d) is False
            # And re-recording works on the fresh store.
            assert cache2.record_digest(d) is True
        # The original corrupt env bytes are preserved aside (never deleted).
        siblings = list(cache_path.glob("exact.corrupt-*"))
        assert len(siblings) == 1
        assert (siblings[0] / "data.mdb").exists()
        warns = [e for e in logs if e.get("event") == "sweep_cache.corrupt_quarantined"]
        assert len(warns) == 1
        assert warns[0]["quarantined_to"].startswith(str(cache_path / "exact.corrupt-"))

    def test_mid_rebuild_lmdb_fault_quarantined(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A raw ``lmdb.Error`` DURING the bloom rebuild is also quarantined + rebuilt."""
        cache_path = tmp_path / "cache"
        with SweepCache.open(cache_path, bloom_capacity=1000) as cache:
            cache.record_digest(_digest(1, "NIFTY", "5m", "bull"))

        real_rebuild = SweepCache._rebuild_bloom_from_exact
        calls = {"n": 0}

        def _rebuild_raises_once(bloom: object, exact: object, *, bloom_capacity: int) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise lmdb.Error("simulated mid-cursor read fault")
            real_rebuild(bloom, exact, bloom_capacity=bloom_capacity)  # type: ignore[arg-type]

        monkeypatch.setattr(SweepCache, "_rebuild_bloom_from_exact", _rebuild_raises_once)
        with (
            capture_logs() as logs,
            SweepCache.open(cache_path, bloom_capacity=1000) as cache2,
        ):
            assert len(cache2._exact) == 0
        assert calls["n"] == 2  # first raised, second (post-quarantine) succeeded
        assert any(e.get("event") == "sweep_cache.corrupt_quarantined" for e in logs)
        assert len(list(cache_path.glob("exact.corrupt-*"))) == 1

    def test_second_failure_propagates_typed_no_second_quarantine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A SECOND corruption (even the rebuilt env) propagates typed, ONE quarantine dir."""
        cache_path = tmp_path / "cache"
        cache_path.mkdir(parents=True, exist_ok=True)
        # Pre-create the exact/ dir so the quarantine rename has a real target.
        (cache_path / "exact").mkdir()

        def _always_corrupt(*_args: object, **_kwargs: object) -> object:
            raise LmdbCorruptEnvError("simulated persistent corruption")

        monkeypatch.setattr(SweepCache, "_open_exact_and_bloom", _always_corrupt)
        with pytest.raises(LmdbCorruptEnvError, match="persistent corruption"):
            SweepCache.open(cache_path, bloom_capacity=1000)
        # Exactly ONE quarantine happened (the retry is not itself wrapped).
        assert len(list(cache_path.glob("exact.corrupt-*"))) == 1

    def test_crash_window_preexisting_sibling_clean_fresh_open(self, tmp_path: Path) -> None:
        """A pre-existing ``exact.corrupt-<ns>`` sibling + no ``exact/`` → clean fresh open."""
        cache_path = tmp_path / "cache"
        cache_path.mkdir(parents=True, exist_ok=True)
        # Simulate a crash AFTER a prior quarantine but BEFORE the rebuild landed.
        stale = cache_path / "exact.corrupt-123456789"
        stale.mkdir()
        with (
            capture_logs() as logs,
            SweepCache.open(cache_path, bloom_capacity=1000) as cache,
        ):
            assert len(cache._exact) == 0  # fresh empty store
        # No NEW quarantine fired (nothing corrupt to detect); the stale sibling stays.
        assert not any(e.get("event") == "sweep_cache.corrupt_quarantined" for e in logs)
        assert stale.exists()
        assert (cache_path / "exact").exists()
