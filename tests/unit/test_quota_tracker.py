"""Unit tests for ``brutex.orchestrator.quota_tracker`` (DEC-P0-029, PR #50b).

Covers the 4-agent pre-impl review's mandatory blockers:
- Atomic LMDB single-key persistence (Agent 3 §B)
- Thread-safety under contention (Agent 1 §G + Agent 2 §E + Agent 3 §D)
- Audit emission AFTER LMDB commit (Agent 1 §B + Agent 3 §E)
- Day-boundary edge cases (Agent 3 §A)
- Clock-skew detection (Agent 2 §B + Agent 3 §C)
- safe_message discipline on raised errors (Agent 2 §B + §C, Agent 3 §C + §I)
- Categorical audit payload (Agent 2 §C, Agent 3 §E)
- Constructor-injected clock — no env-var hook (Agent 2 §B, Agent 3 §F)
- Restart resume with fired-today rehydration (Agent 3 §H)
- Quota-invariant guards on corrupt state (Agent 2 §D)
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from brutex.orchestrator.quota_tracker import (
    DAILY_QUOTA_LIMIT,
    ClockSkewError,
    DailyQuotaExceeded,
    DailyQuotaTracker,
    QuotaInvariantError,
    _default_quota_path,
)

_IST = ZoneInfo("Asia/Kolkata")


def _make_clock(values: list[datetime]) -> Callable[[], datetime]:
    """Build a callable that returns values from a list, one per call.

    Raises IndexError if exhausted (catches tests that call more times than
    they expected).
    """
    iterator = iter(values)

    def _clock() -> datetime:
        return next(iterator)

    return _clock


def _make_static_clock(when: datetime) -> Callable[[], datetime]:
    """Build a callable that always returns the same datetime."""

    def _clock() -> datetime:
        return when

    return _clock


# ────────────────────────────────────────────────────────────────────────
# Construction + invariants
# ────────────────────────────────────────────────────────────────────────


class TestConstruction:
    def test_default_limit_is_100k(self, tmp_path: Path) -> None:
        tracker = DailyQuotaTracker(
            store_path=tmp_path / "quota.lmdb",
            clock=_make_static_clock(datetime(2026, 5, 18, 9, 15, tzinfo=_IST)),
        )
        try:
            assert tracker.daily_limit == 100_000
            assert DAILY_QUOTA_LIMIT == 100_000
        finally:
            tracker.close()

    def test_constructor_rejects_zero_limit(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="daily_limit must be positive"):
            DailyQuotaTracker(
                daily_limit=0,
                store_path=tmp_path / "quota.lmdb",
            )

    def test_constructor_rejects_negative_limit(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="daily_limit must be positive"):
            DailyQuotaTracker(
                daily_limit=-1,
                store_path=tmp_path / "quota.lmdb",
            )

    def test_constructor_rejects_zero_map_size(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="map_size_bytes must be positive"):
            DailyQuotaTracker(
                map_size_bytes=0,
                store_path=tmp_path / "quota.lmdb",
            )

    def test_constructor_creates_parent_dir(self, tmp_path: Path) -> None:
        nested = tmp_path / "deeply" / "nested" / "quota.lmdb"
        assert not nested.parent.exists()
        tracker = DailyQuotaTracker(
            store_path=nested,
            clock=_make_static_clock(datetime(2026, 5, 18, 9, 15, tzinfo=_IST)),
        )
        try:
            assert nested.parent.exists()
        finally:
            tracker.close()

    def test_default_quota_path_is_per_user_home(self) -> None:
        """The default LMDB path is the MIGRATION.md item-91 location.

        Pure path construction — no LMDB env is opened. Pins the
        per-user (NOT per-run) persistence convention
        (DEC-ORCH-STRICT-GATES-001 standalone-gap closure).
        """
        assert _default_quota_path() == Path.home() / ".brutex" / "quota" / "quota.lmdb"

    def test_close_before_env_assignment_is_safe(self) -> None:
        """``close()`` no-ops when ``_env`` was never assigned.

        Mirrors the failure mode close() defends against: __post_init__
        raising BEFORE the LMDB env exists (e.g. the daily_limit
        ValueError) followed by a defensive close() in caller cleanup
        (DEC-ORCH-STRICT-GATES-001 standalone-gap closure).
        """
        tracker = DailyQuotaTracker.__new__(DailyQuotaTracker)
        tracker.close()  # must not raise — getattr falls back to None


# ────────────────────────────────────────────────────────────────────────
# acquire_or_raise — basic semantics
# ────────────────────────────────────────────────────────────────────────


class TestAcquireBasic:
    def test_first_call_returns_one(self, tmp_path: Path) -> None:
        tracker = DailyQuotaTracker(
            daily_limit=10,
            store_path=tmp_path / "quota.lmdb",
            clock=_make_static_clock(datetime(2026, 5, 18, 9, 15, tzinfo=_IST)),
        )
        try:
            assert tracker.acquire_or_raise() == 1
        finally:
            tracker.close()

    def test_sequential_calls_increment_count(self, tmp_path: Path) -> None:
        tracker = DailyQuotaTracker(
            daily_limit=10,
            store_path=tmp_path / "quota.lmdb",
            clock=_make_static_clock(datetime(2026, 5, 18, 9, 15, tzinfo=_IST)),
        )
        try:
            results = [tracker.acquire_or_raise() for _ in range(5)]
            assert results == [1, 2, 3, 4, 5]
        finally:
            tracker.close()

    def test_acquire_persists_state(self, tmp_path: Path) -> None:
        path = tmp_path / "quota.lmdb"
        clock = _make_static_clock(datetime(2026, 5, 18, 9, 15, tzinfo=_IST))
        tracker = DailyQuotaTracker(daily_limit=10, store_path=path, clock=clock)
        try:
            for _ in range(3):
                tracker.acquire_or_raise()
        finally:
            tracker.close()

        # Reopen with a fresh tracker — same path, same day → should
        # resume from count=3.
        tracker2 = DailyQuotaTracker(daily_limit=10, store_path=path, clock=clock)
        try:
            assert tracker2.acquire_or_raise() == 4
        finally:
            tracker2.close()


# ────────────────────────────────────────────────────────────────────────
# Quota exhaustion
# ────────────────────────────────────────────────────────────────────────


class TestQuotaExhaustion:
    def test_acquire_at_limit_raises(self, tmp_path: Path) -> None:
        tracker = DailyQuotaTracker(
            daily_limit=3,
            store_path=tmp_path / "quota.lmdb",
            clock=_make_static_clock(datetime(2026, 5, 18, 9, 15, tzinfo=_IST)),
        )
        try:
            tracker.acquire_or_raise()
            tracker.acquire_or_raise()
            tracker.acquire_or_raise()
            with pytest.raises(DailyQuotaExceeded):
                tracker.acquire_or_raise()
        finally:
            tracker.close()

    def test_exceeded_error_has_safe_message(self, tmp_path: Path) -> None:
        tracker = DailyQuotaTracker(
            daily_limit=1,
            store_path=tmp_path / "quota.lmdb",
            clock=_make_static_clock(datetime(2026, 5, 18, 9, 15, tzinfo=_IST)),
        )
        try:
            tracker.acquire_or_raise()
            with pytest.raises(DailyQuotaExceeded) as exc_info:
                tracker.acquire_or_raise()
            # safe_message MUST be categorical — no count, no date.
            assert str(exc_info.value) == "Daily vendor API quota exceeded"
            assert exc_info.value.safe_message == "Daily vendor API quota exceeded"
        finally:
            tracker.close()

    def test_exceeded_count_never_passes_limit_persisted(self, tmp_path: Path) -> None:
        """After exhaustion + restart, persisted count is exactly the
        limit — never higher. Closes Agent 3 §H 100% threshold semantic."""
        path = tmp_path / "quota.lmdb"
        clock = _make_static_clock(datetime(2026, 5, 18, 9, 15, tzinfo=_IST))
        tracker = DailyQuotaTracker(daily_limit=2, store_path=path, clock=clock)
        try:
            tracker.acquire_or_raise()
            tracker.acquire_or_raise()
            with pytest.raises(DailyQuotaExceeded):
                tracker.acquire_or_raise()
            assert tracker.current_count() == 2
        finally:
            tracker.close()

        # Reopen — count is exactly 2, not 3.
        tracker2 = DailyQuotaTracker(daily_limit=2, store_path=path, clock=clock)
        try:
            assert tracker2.current_count() == 2
        finally:
            tracker2.close()


# ────────────────────────────────────────────────────────────────────────
# Day boundary (IST midnight)  — Agent 3 §A
# ────────────────────────────────────────────────────────────────────────


class TestDayBoundary:
    def test_call_at_2359_counts_today(self, tmp_path: Path) -> None:
        clock = _make_static_clock(datetime(2026, 5, 18, 23, 59, 59, 999_000, tzinfo=_IST))
        tracker = DailyQuotaTracker(daily_limit=10, store_path=tmp_path / "quota.lmdb", clock=clock)
        try:
            assert tracker.acquire_or_raise() == 1
            assert tracker.current_count() == 1
        finally:
            tracker.close()

    def test_first_call_after_midnight_resets_and_counts_tomorrow(self, tmp_path: Path) -> None:
        """Call at 23:59:59 IST counts today; call at 00:00:00.001 IST
        next-day resets AND counts (single call does BOTH operations).

        Closes Agent 3 §J(3) off-by-one rollover false-OK.
        """
        path = tmp_path / "quota.lmdb"
        clock = _make_clock(
            [
                datetime(2026, 5, 18, 23, 59, 59, 999_000, tzinfo=_IST),
                # Re-construct uses the SAME clock; need to provide enough
                # values for: __post_init__ read (1) + acquire (1) + assert (1).
                datetime(2026, 5, 19, 0, 0, 0, 1_000, tzinfo=_IST),
                datetime(2026, 5, 19, 0, 0, 0, 1_000, tzinfo=_IST),
                datetime(2026, 5, 19, 0, 0, 0, 1_000, tzinfo=_IST),
            ]
        )
        tracker = DailyQuotaTracker(daily_limit=10, store_path=path, clock=clock)
        try:
            assert tracker.acquire_or_raise() == 1  # today=2026-05-18
            assert tracker.acquire_or_raise() == 1  # tomorrow=2026-05-19, reset
            assert tracker.current_count() == 1
        finally:
            tracker.close()

    def test_rollover_clears_fired_today(self, tmp_path: Path) -> None:
        """After day rollover, threshold-fired-today set is empty so
        thresholds can fire again on the new day."""
        path = tmp_path / "quota.lmdb"
        # Use a small limit so 80% threshold = 1.
        days = [
            datetime(2026, 5, 18, 9, 15, tzinfo=_IST),
        ] * 12  # enough for many reads + acquires today
        days.extend([datetime(2026, 5, 19, 9, 15, tzinfo=_IST)] * 12)
        clock = _make_clock(days)
        tracker = DailyQuotaTracker(daily_limit=10, store_path=path, clock=clock)
        try:
            # Push count to 10 (which hits 80% = 8 and 95% = 9 and 100% = 10).
            for _ in range(10):
                tracker.acquire_or_raise()
            crossed_today = tracker.newly_crossed_thresholds()
            assert "80pct" in crossed_today
            assert "95pct" in crossed_today
            assert "exceeded" in crossed_today
            # Calling again returns nothing — already fired.
            assert tracker.newly_crossed_thresholds() == []
            # Now jump to next day — counter resets via the next acquire.
            tracker.acquire_or_raise()  # rolls over to 2026-05-19, count=1
            # Push count to 8 on new day to trigger 80% only.
            for _ in range(7):
                tracker.acquire_or_raise()
            crossed_tomorrow = tracker.newly_crossed_thresholds()
            # On new day, 80% should fire fresh.
            assert "80pct" in crossed_tomorrow
        finally:
            tracker.close()


# ────────────────────────────────────────────────────────────────────────
# Clock skew — Agent 2 §B + Agent 3 §C
# ────────────────────────────────────────────────────────────────────────


class TestClockSkew:
    def test_persisted_future_date_raises_clock_skew(self, tmp_path: Path) -> None:
        path = tmp_path / "quota.lmdb"
        # Seed with 2026-05-20.
        seed_clock = _make_static_clock(datetime(2026, 5, 20, 12, 0, tzinfo=_IST))
        tracker = DailyQuotaTracker(daily_limit=10, store_path=path, clock=seed_clock)
        try:
            tracker.acquire_or_raise()
        finally:
            tracker.close()

        # Reopen with a clock that says 2026-05-18 (jumped backward).
        rollback_clock = _make_static_clock(datetime(2026, 5, 18, 12, 0, tzinfo=_IST))
        tracker2 = DailyQuotaTracker(daily_limit=10, store_path=path, clock=rollback_clock)
        try:
            with pytest.raises(ClockSkewError) as exc_info:
                tracker2.acquire_or_raise()
            assert exc_info.value.safe_message is not None
            assert (
                exc_info.value.safe_message
                == "Persisted quota date is in the future relative to the "
                "observed IST date; refusing to proceed."
            )
        finally:
            tracker2.close()


# ────────────────────────────────────────────────────────────────────────
# Threshold emissions — Agent 2 §C + Agent 3 §E + §H
# ────────────────────────────────────────────────────────────────────────


class TestThresholds:
    def test_thresholds_fire_once_per_day(self, tmp_path: Path) -> None:
        tracker = DailyQuotaTracker(
            daily_limit=10,
            store_path=tmp_path / "quota.lmdb",
            clock=_make_static_clock(datetime(2026, 5, 18, 9, 15, tzinfo=_IST)),
        )
        try:
            for _ in range(8):
                tracker.acquire_or_raise()
            first = tracker.newly_crossed_thresholds()
            assert first == ["80pct"]
            # Calling again with no new crossings returns nothing.
            assert tracker.newly_crossed_thresholds() == []
        finally:
            tracker.close()

    def test_threshold_labels_are_categorical(self, tmp_path: Path) -> None:
        """Verify the returned labels are exact categorical strings —
        no count, no date in the labels (Agent 2 §C)."""
        tracker = DailyQuotaTracker(
            daily_limit=10,
            store_path=tmp_path / "quota.lmdb",
            clock=_make_static_clock(datetime(2026, 5, 18, 9, 15, tzinfo=_IST)),
        )
        try:
            for _ in range(10):
                tracker.acquire_or_raise()
            labels = tracker.newly_crossed_thresholds()
            assert set(labels) == {"80pct", "95pct", "exceeded"}
            for label in labels:
                # No digits except the percentage prefix.
                assert label in {"80pct", "95pct", "exceeded"}
        finally:
            tracker.close()

    def test_restart_does_not_re_emit_threshold(self, tmp_path: Path) -> None:
        """Process restart with count=85 (above 80% = 8 of daily_limit=10)
        on the same day MUST NOT re-emit the 80pct threshold.

        Closes Agent 3 §H restart-fired-set rehydration false-OK.
        """
        path = tmp_path / "quota.lmdb"
        clock = _make_static_clock(datetime(2026, 5, 18, 9, 15, tzinfo=_IST))
        tracker = DailyQuotaTracker(daily_limit=10, store_path=path, clock=clock)
        try:
            for _ in range(8):
                tracker.acquire_or_raise()
            first = tracker.newly_crossed_thresholds()
            assert first == ["80pct"]
        finally:
            tracker.close()

        # Reopen — same day, count is already 8.
        tracker2 = DailyQuotaTracker(daily_limit=10, store_path=path, clock=clock)
        try:
            # newly_crossed_thresholds() should report nothing because
            # 80pct was already fired today (rehydrated from persisted state).
            assert tracker2.newly_crossed_thresholds() == []
            # Advance to 9 — 95% fires fresh.
            tracker2.acquire_or_raise()
            crossed = tracker2.newly_crossed_thresholds()
            assert crossed == ["95pct"]
        finally:
            tracker2.close()

    def test_no_persisted_state_returns_empty(self, tmp_path: Path) -> None:
        """A fresh tracker (zero acquires) reports no crossings.

        Covers the persisted-is-None early return (quota_tracker.py:362,
        DEC-ORCH-STRICT-GATES-001 standalone-gap closure).
        """
        tracker = DailyQuotaTracker(
            daily_limit=10,
            store_path=tmp_path / "quota.lmdb",
            clock=_make_static_clock(datetime(2026, 5, 18, 9, 15, tzinfo=_IST)),
        )
        try:
            assert tracker.newly_crossed_thresholds() == []
        finally:
            tracker.close()

    def test_stale_persisted_day_returns_empty(self, tmp_path: Path) -> None:
        """Yesterday's persisted count reports no crossings today.

        The read-only query must not fire thresholds against a stale
        day — rollover happens in acquire_or_raise, never here
        (quota_tracker.py:366, DEC-ORCH-STRICT-GATES-001 gap closure).
        """
        clock = _make_clock(
            [
                datetime(2026, 5, 18, 23, 50, tzinfo=_IST),  # acquire (day 1)
                datetime(2026, 5, 19, 9, 15, tzinfo=_IST),  # query (day 2)
            ]
        )
        tracker = DailyQuotaTracker(
            daily_limit=10,
            store_path=tmp_path / "quota.lmdb",
            clock=clock,
        )
        try:
            tracker.acquire_or_raise()
            assert tracker.newly_crossed_thresholds() == []
        finally:
            tracker.close()


# ────────────────────────────────────────────────────────────────────────
# Corruption guards — Agent 2 §D
# ────────────────────────────────────────────────────────────────────────


class TestCorruptionGuards:
    def test_negative_count_raises_quota_invariant(self, tmp_path: Path) -> None:
        import lmdb

        path = tmp_path / "quota.lmdb"
        path.mkdir(parents=True, exist_ok=True)

        # Directly inject a corrupt payload with negative count.
        env = lmdb.open(str(path), map_size=1024 * 1024, subdir=True, create=True)
        try:
            with env.begin(write=True) as txn:
                txn.put(b"quota", b'{"date":"2026-05-18","count":-5}')
        finally:
            env.close()

        clock = _make_static_clock(datetime(2026, 5, 18, 9, 15, tzinfo=_IST))
        with pytest.raises(QuotaInvariantError):
            DailyQuotaTracker(daily_limit=10, store_path=path, clock=clock)

    def test_malformed_json_raises_quota_invariant(self, tmp_path: Path) -> None:
        import lmdb

        path = tmp_path / "quota.lmdb"
        path.mkdir(parents=True, exist_ok=True)

        env = lmdb.open(str(path), map_size=1024 * 1024, subdir=True, create=True)
        try:
            with env.begin(write=True) as txn:
                txn.put(b"quota", b"not-json-at-all")
        finally:
            env.close()

        clock = _make_static_clock(datetime(2026, 5, 18, 9, 15, tzinfo=_IST))
        with pytest.raises(QuotaInvariantError):
            DailyQuotaTracker(daily_limit=10, store_path=path, clock=clock)

    def test_missing_required_keys_raises(self, tmp_path: Path) -> None:
        import lmdb

        path = tmp_path / "quota.lmdb"
        path.mkdir(parents=True, exist_ok=True)
        env = lmdb.open(str(path), map_size=1024 * 1024, subdir=True, create=True)
        try:
            with env.begin(write=True) as txn:
                txn.put(b"quota", b'{"date":"2026-05-18"}')  # no count
        finally:
            env.close()

        clock = _make_static_clock(datetime(2026, 5, 18, 9, 15, tzinfo=_IST))
        with pytest.raises(QuotaInvariantError):
            DailyQuotaTracker(daily_limit=10, store_path=path, clock=clock)

    @pytest.mark.parametrize(
        ("payload", "detail"),
        [
            pytest.param(b"[1,2]", "not a dict", id="json-array-not-dict"),
            pytest.param(b'{"date":5,"count":1}', "date is not a string", id="date-not-str"),
            pytest.param(
                b'{"date":"2026-05-18","count":"5"}', "count is not an int", id="count-str"
            ),
            pytest.param(
                b'{"date":"2026-05-18","count":true}', "count is not an int", id="count-bool"
            ),
            pytest.param(b'{"date":"18/05/2026","count":1}', "not ISO-format", id="date-not-iso"),
        ],
    )
    def test_schema_violation_payloads_raise(
        self, tmp_path: Path, payload: bytes, detail: str
    ) -> None:
        """Every remaining documented schema deviation raises the guard.

        Completes the on-disk tamper matrix (Agent 2 §D): non-dict JSON,
        non-string date, non-int count (str AND the bool look-alike —
        ``isinstance(True, int)`` is True in Python), non-ISO date
        (quota_tracker.py:416/426/430/437-438,
        DEC-ORCH-STRICT-GATES-001 standalone-gap closure). ``str(exc)``
        stays the categorical safe message; the specific cause lives in
        the detailed message (``args[0]``) for local debugging only.
        """
        import lmdb

        path = tmp_path / "quota.lmdb"
        path.mkdir(parents=True, exist_ok=True)
        env = lmdb.open(str(path), map_size=1024 * 1024, subdir=True, create=True)
        try:
            with env.begin(write=True) as txn:
                txn.put(b"quota", payload)
        finally:
            env.close()

        clock = _make_static_clock(datetime(2026, 5, 18, 9, 15, tzinfo=_IST))
        with pytest.raises(QuotaInvariantError) as exc_info:
            DailyQuotaTracker(daily_limit=10, store_path=path, clock=clock)
        assert str(exc_info.value) == "Persisted quota state is corrupt or malformed"
        assert detail in str(exc_info.value.args[0])


# ────────────────────────────────────────────────────────────────────────
# FIX J — a corrupt-state __post_init__ raise must NOT orphan the shared-env
# refcount (DEC-ORCH-AUDIT-RATE-HARDEN-001). __post_init__ opens (refcount++)
# BEFORE _read_state, which raises on corrupt persisted state; since the
# constructor then raises, the instance never binds and close() is unreachable,
# so without the release-on-failure guard the (env, refcount) entry leaked and
# grew on every corrupt load.
# ────────────────────────────────────────────────────────────────────────


class TestEnvRefcountLeakOnCorruptState:
    @staticmethod
    def _write_corrupt(path: Path) -> None:
        import lmdb

        path.mkdir(parents=True, exist_ok=True)
        env = lmdb.open(str(path), map_size=1024 * 1024, subdir=True, create=True)
        try:
            with env.begin(write=True) as txn:
                txn.put(b"quota", b"not-json-at-all")
        finally:
            env.close()

    def test_corrupt_construct_releases_env_refcount(self, tmp_path: Path) -> None:
        from brutex.orchestrator import quota_tracker as qt

        path = tmp_path / "quota.lmdb"
        self._write_corrupt(path)
        key = str(path.resolve())

        with qt._ENV_REGISTRY_LOCK:
            assert key not in qt._ENV_REGISTRY
            registry_size_before = len(qt._ENV_REGISTRY)

        clock = _make_static_clock(datetime(2026, 5, 18, 9, 15, tzinfo=_IST))
        with pytest.raises(QuotaInvariantError):
            DailyQuotaTracker(daily_limit=10, store_path=path, clock=clock)

        # The failed construct must have released the reference it took: no
        # entry for this path, and the registry is back to its prior size.
        with qt._ENV_REGISTRY_LOCK:
            assert key not in qt._ENV_REGISTRY
            assert len(qt._ENV_REGISTRY) == registry_size_before

    def test_repeated_corrupt_constructs_do_not_grow_registry(self, tmp_path: Path) -> None:
        from brutex.orchestrator import quota_tracker as qt

        path = tmp_path / "quota.lmdb"
        self._write_corrupt(path)
        key = str(path.resolve())
        clock = _make_static_clock(datetime(2026, 5, 18, 9, 15, tzinfo=_IST))

        with qt._ENV_REGISTRY_LOCK:
            registry_size_before = len(qt._ENV_REGISTRY)

        # N repeated corrupt loads on the SAME path — the leak (if present)
        # would push the refcount 1→2→3… and keep the entry resident.
        for _ in range(5):
            with pytest.raises(QuotaInvariantError):
                DailyQuotaTracker(daily_limit=10, store_path=path, clock=clock)

        with qt._ENV_REGISTRY_LOCK:
            assert key not in qt._ENV_REGISTRY
            assert len(qt._ENV_REGISTRY) == registry_size_before

    def test_good_tracker_coexists_after_corrupt_release(self, tmp_path: Path) -> None:
        # After the failed-construct release, a SUBSEQUENT good tracker on the
        # same path opens cleanly at refcount 1 (the env was fully released,
        # not left half-open). Proves the release mirrors close() exactly.
        from brutex.orchestrator import quota_tracker as qt

        path = tmp_path / "quota.lmdb"
        self._write_corrupt(path)
        key = str(path.resolve())
        clock = _make_static_clock(datetime(2026, 5, 18, 9, 15, tzinfo=_IST))

        with pytest.raises(QuotaInvariantError):
            DailyQuotaTracker(daily_limit=10, store_path=path, clock=clock)

        # Overwrite the corrupt payload with a valid one, then construct.
        import lmdb

        env = lmdb.open(str(path), map_size=1024 * 1024, subdir=True, create=True)
        try:
            with env.begin(write=True) as txn:
                txn.put(b"quota", b'{"date":"2026-05-18","count":3}')
        finally:
            env.close()

        good = DailyQuotaTracker(daily_limit=10, store_path=path, clock=clock)
        try:
            with qt._ENV_REGISTRY_LOCK:
                assert qt._ENV_REGISTRY[key][1] == 1  # refcount exactly 1
            assert good.current_count() == 3
        finally:
            good.close()
        with qt._ENV_REGISTRY_LOCK:
            assert key not in qt._ENV_REGISTRY


# ────────────────────────────────────────────────────────────────────────
# Concurrency — Agent 1 §G + Agent 3 §D
# ────────────────────────────────────────────────────────────────────────


class TestConcurrency:
    def test_concurrent_acquire_exact_limit(self, tmp_path: Path) -> None:
        """N threads each calling acquire_or_raise() — exactly daily_limit
        succeed; the remainder all raise.

        Closes Agent 3 §D race-window correctness invariant.
        """
        tracker = DailyQuotaTracker(
            daily_limit=50,
            store_path=tmp_path / "quota.lmdb",
            clock=_make_static_clock(datetime(2026, 5, 18, 9, 15, tzinfo=_IST)),
        )

        successes: list[int] = []
        failures: list[Exception] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                value = tracker.acquire_or_raise()
                with lock:
                    successes.append(value)
            except DailyQuotaExceeded as exc:
                with lock:
                    failures.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(75)]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert len(successes) == 50  # exactly daily_limit succeeded
            assert len(failures) == 25  # the rest all raised
            assert sorted(successes) == list(range(1, 51))
        finally:
            tracker.close()


# ────────────────────────────────────────────────────────────────────────
# Read-only queries
# ────────────────────────────────────────────────────────────────────────


class TestReadOnlyQueries:
    def test_current_count_empty_state(self, tmp_path: Path) -> None:
        tracker = DailyQuotaTracker(
            daily_limit=10,
            store_path=tmp_path / "quota.lmdb",
            clock=_make_static_clock(datetime(2026, 5, 18, 9, 15, tzinfo=_IST)),
        )
        try:
            assert tracker.current_count() == 0
            assert tracker.remaining() == 10
        finally:
            tracker.close()

    def test_current_count_after_acquires(self, tmp_path: Path) -> None:
        tracker = DailyQuotaTracker(
            daily_limit=10,
            store_path=tmp_path / "quota.lmdb",
            clock=_make_static_clock(datetime(2026, 5, 18, 9, 15, tzinfo=_IST)),
        )
        try:
            for _ in range(7):
                tracker.acquire_or_raise()
            assert tracker.current_count() == 7
            assert tracker.remaining() == 3
        finally:
            tracker.close()

    def test_current_count_returns_zero_for_stale_persisted_day(self, tmp_path: Path) -> None:
        """If the persisted date is YESTERDAY (no acquire yet today),
        current_count() returns 0 — does not surface the stale count."""
        path = tmp_path / "quota.lmdb"
        seed_clock = _make_static_clock(datetime(2026, 5, 18, 9, 15, tzinfo=_IST))
        tracker = DailyQuotaTracker(daily_limit=10, store_path=path, clock=seed_clock)
        try:
            for _ in range(5):
                tracker.acquire_or_raise()
        finally:
            tracker.close()

        next_day_clock = _make_static_clock(datetime(2026, 5, 19, 9, 15, tzinfo=_IST))
        tracker2 = DailyQuotaTracker(daily_limit=10, store_path=path, clock=next_day_clock)
        try:
            assert tracker2.current_count() == 0
            assert tracker2.remaining() == 10
        finally:
            tracker2.close()


# ────────────────────────────────────────────────────────────────────────
# Shared-env registry — second open + ref-counted release
# (DEC-QUOTA-ENV-SINGLE-OPEN-001: LMDB forbids a second open of the same
# env in one process, so two live trackers on ONE store_path must share
# the cached env; closing one drops a reference, closing the last closes
# the env and evicts the registry entry.)
# ────────────────────────────────────────────────────────────────────────


class TestSharedEnvRefcount:
    def test_two_live_trackers_share_one_env_and_release_ref_by_ref(self, tmp_path: Path) -> None:
        from brutex.orchestrator import quota_tracker as qt

        path = tmp_path / "quota.lmdb"
        key = str(path.resolve())
        clock = _make_static_clock(datetime(2026, 5, 18, 9, 15, tzinfo=_IST))

        first = DailyQuotaTracker(daily_limit=10, store_path=path, clock=clock)
        try:
            # The SECOND concurrent tracker takes the registry fast path —
            # same env object, refcount 2 (a raw second lmdb.open would raise).
            second = DailyQuotaTracker(daily_limit=10, store_path=path, clock=clock)
            try:
                assert second._env is first._env
                with qt._ENV_REGISTRY_LOCK:
                    env, refs = qt._ENV_REGISTRY[key]
                    assert env is first._env
                    assert refs == 2
                assert second.acquire_or_raise() == 1
            finally:
                second.close()

            # Ref-counted release: the entry SURVIVES the first close (refs
            # 2 → 1) and the shared env stays open — the surviving tracker
            # still reads/writes through it.
            with qt._ENV_REGISTRY_LOCK:
                env, refs = qt._ENV_REGISTRY[key]
                assert env is first._env
                assert refs == 1
            assert first.acquire_or_raise() == 2
        finally:
            first.close()

        # Last release closes the env and evicts the entry.
        with qt._ENV_REGISTRY_LOCK:
            assert key not in qt._ENV_REGISTRY


# ────────────────────────────────────────────────────────────────────────
# Default clock — production sanity
# ────────────────────────────────────────────────────────────────────────


class TestDefaultClock:
    def test_default_clock_returns_ist_aware_datetime(self, tmp_path: Path) -> None:
        """Production default clock returns a tz-aware datetime with
        ZoneInfo("Asia/Kolkata"). Closes Agent 3 §F default-clock false-OK
        trap (a naive datetime.now() default would type-check but silently
        use local time).
        """
        from brutex.orchestrator.quota_tracker import _default_ist_clock

        observed = _default_ist_clock()
        assert observed.tzinfo is not None
        assert str(observed.tzinfo) == "Asia/Kolkata"
