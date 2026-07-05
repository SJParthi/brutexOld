"""Unit tests for ``brutex.dashboard.greeks_health`` (DEC-GREEKS-ENRICH-OBSERVE-001).

The consecutive-failure health counter over the post-pull Greeks-enrichment
whole-pass verdicts: increment on ``False``, reset+re-arm on ``True``, ignore
``None`` (dry-run — the pass never ran), and exactly ONE structlog WARNING
``pull.greeks_enrich_failing`` at each OK→BREACH threshold crossing. 100%
line + branch from birth; a thread-safety smoke proves the lock discipline
(the counter is observed from background pull-job threads).
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence

from brutex.dashboard.greeks_health import (
    DEFAULT_GREEKS_FAILURE_THRESHOLD,
    GreeksEnrichHealth,
)
from structlog.testing import capture_logs


def _breaches(logs: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    """The captured ``pull.greeks_enrich_failing`` breach events."""
    return [r for r in logs if r["event"] == "pull.greeks_enrich_failing"]


def test_default_threshold_is_three() -> None:
    assert DEFAULT_GREEKS_FAILURE_THRESHOLD == 3


def test_false_increments_and_returns_count() -> None:
    health = GreeksEnrichHealth()
    with capture_logs() as logs:
        assert health.observe(False, error="RuntimeError: a") == 1
        assert health.observe(False, error="RuntimeError: b") == 2
    assert _breaches(logs) == []  # below the default threshold — no warning yet


def test_true_resets_the_streak() -> None:
    health = GreeksEnrichHealth()
    with capture_logs() as logs:
        assert health.observe(False, error="RuntimeError: a") == 1
        assert health.observe(True) == 0
        assert health.observe(False, error="RuntimeError: b") == 1
    assert _breaches(logs) == []  # the reset kept the streak below the threshold


def test_none_is_ignored_and_returns_current_count() -> None:
    # A dry-run's "pass did not run" must move the streak in NEITHER direction.
    health = GreeksEnrichHealth()
    with capture_logs() as logs:
        assert health.observe(None) == 0  # ignored on a fresh counter too
        assert health.observe(False, error="RuntimeError: a") == 1
        assert health.observe(None) == 1  # neither an increment nor a reset
        assert health.observe(False, error="RuntimeError: b") == 2
    assert _breaches(logs) == []


def test_threshold_crossing_fires_exactly_once() -> None:
    health = GreeksEnrichHealth(threshold=3)
    with capture_logs() as logs:
        health.observe(False, error="RuntimeError: a")
        health.observe(False, error="RuntimeError: b")
        assert _breaches(logs) == []  # 2 < 3: still armed, silent
        health.observe(False, error="RuntimeError: c")
        health.observe(False, error="RuntimeError: d")  # past the crossing: no re-fire
    breaches = _breaches(logs)
    assert len(breaches) == 1  # ONE warning at the OK→BREACH crossing only
    assert breaches[0]["consecutive"] == 3
    assert breaches[0]["threshold"] == 3
    assert breaches[0]["last_error"] == "RuntimeError: c"


def test_recovery_rearms_and_a_relapse_fires_again() -> None:
    health = GreeksEnrichHealth(threshold=2)
    with capture_logs() as logs:
        health.observe(False, error="RuntimeError: a")
        health.observe(False, error="RuntimeError: b")  # first breach
        health.observe(True)  # recovery re-arms the latch
        health.observe(False, error="RuntimeError: c")
        health.observe(False, error="RuntimeError: d")  # second breach
    breaches = _breaches(logs)
    assert len(breaches) == 2
    assert breaches[0]["last_error"] == "RuntimeError: b"
    assert breaches[1]["last_error"] == "RuntimeError: d"


def test_recovery_clears_the_last_error() -> None:
    # A breach AFTER a recovery must never carry a stale pre-recovery error.
    health = GreeksEnrichHealth(threshold=1)
    with capture_logs() as logs:
        health.observe(False, error="RuntimeError: old")
        health.observe(True)
        health.observe(False)  # relapse without an error detail
    breaches = _breaches(logs)
    assert len(breaches) == 2
    assert breaches[1]["last_error"] is None  # honest: not the stale "old" error


def test_threshold_clamped_to_at_least_one() -> None:
    # A non-positive threshold clamps to 1 — the first failure IS the crossing.
    health = GreeksEnrichHealth(threshold=0)
    with capture_logs() as logs:
        assert health.observe(False, error="RuntimeError: a") == 1
    breaches = _breaches(logs)
    assert len(breaches) == 1
    assert breaches[0]["threshold"] == 1


def test_thread_safety_smoke() -> None:
    """Concurrent observers never lose an increment and breach exactly once."""
    health = GreeksEnrichHealth(threshold=5)
    workers = 16
    start = threading.Barrier(workers)

    def _observe() -> None:
        start.wait()
        health.observe(False, error="RuntimeError: concurrent")

    with capture_logs() as logs:
        threads = [threading.Thread(target=_observe) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert health.observe(None) == workers  # no lost increment under the lock
    assert len(_breaches(logs)) == 1  # the latch fired exactly once
