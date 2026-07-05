"""Cross-module ordering regression for DEC-WRITE-DEADLINE-LOCK-ORDER-001.

The durable-write freeze guard (:func:`brutex.adapters.groww.write_deadline.append_with_deadline`,
DEC-FETCH-HUNG-WRITE-DEADLINE-001) wraps the WHOLE ``sink.append`` — INCLUDING the
cross-process partition-lock acquire (DEC-LAKE-PARTITION-FLOCK-001). The audited 2026-07-05
inversion: the lock-acquire default was 120 s while the write deadline was 90 s, so the lock
bound was UNREACHABLE — the deadline abandoned the write thread while it still blocked in the
``flock`` poll loop (thread + lock fd leaked per contended window) instead of the clean
:class:`~brutex.data.errors.PartitionLockTimeout` defer path.

This suite pins THREE things:

* the ORDERING INVARIANT on the defaults — ``DEFAULT_PARTITION_LOCK_TIMEOUT_SECONDS`` strictly
  below ``GROWW_WRITE_DEADLINE_S`` with real margin. The two constants deliberately live in
  their own modules (``brutex.data`` must never import ``brutex.adapters``), so this
  cross-module import IS the single mechanical gate against a future re-inversion;
* the use-time WARN seam — the env overrides (``BRUTEX_PARTITION_LOCK_TIMEOUT_S`` /
  ``BRUTEX_GROWW_WRITE_DEADLINE_S``) CAN re-invert the ordering at runtime; an inverted
  EFFECTIVE configuration is WARNed exactly once per process
  (``groww.write_deadline.lock_timeout_inversion``) on the default-deadline path, never
  silently accepted and never spammed per window;
* the explicit per-call ``deadline_seconds`` path (a test exercising a tiny timeout) never
  trips the configuration check.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from brutex.adapters.groww.constants import GROWW_WRITE_DEADLINE_S
from brutex.adapters.groww.write_deadline import (
    WRITE_DEADLINE_ENV,
    append_with_deadline,
    reset_lock_timeout_inversion_warning,
)
from brutex.data.models import CandleBar
from brutex.data.partition_lock import (
    DEFAULT_PARTITION_LOCK_TIMEOUT_SECONDS,
    PARTITION_LOCK_TIMEOUT_ENV,
)
from brutex.data.sink import SinkAppendResult
from structlog.testing import capture_logs

_INVERSION_EVENT = "groww.write_deadline.lock_timeout_inversion"


class _OkSink:
    """A sink whose ``append`` returns immediately (the normal path)."""

    def append(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
        merge_mode: str = "keep-first",
    ) -> SinkAppendResult:
        del exchange, segment, symbol, interval, merge_mode
        return SinkAppendResult(bars_appended=len(bars), partitions_written=0, write_results=())


def _call(deadline_seconds: float | None) -> SinkAppendResult:
    return append_with_deadline(
        _OkSink(),
        exchange="NSE",
        segment="CASH",
        symbol="NIFTY",
        interval="1minute",
        bars=[],
        deadline_seconds=deadline_seconds,
    )


def _inversion_events(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in logs if entry["event"] == _INVERSION_EVENT]


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts from the audited defaults with the warn-once guard re-armed."""
    monkeypatch.delenv(PARTITION_LOCK_TIMEOUT_ENV, raising=False)
    monkeypatch.delenv(WRITE_DEADLINE_ENV, raising=False)
    reset_lock_timeout_inversion_warning()


class TestOrderingInvariant:
    def test_default_lock_timeout_strictly_below_write_deadline(self) -> None:
        """THE cross-module ordering regression gate (DEC-WRITE-DEADLINE-LOCK-ORDER-001).

        Imports BOTH constants (they deliberately live in separate modules —
        ``brutex.data`` never imports ``brutex.adapters``) and asserts the lock-acquire
        default sits strictly below the durable-write deadline. A future edit re-inverting
        the ordering (the audited 120 s vs 90 s inversion) fails HERE, in CI, by name.
        """
        assert DEFAULT_PARTITION_LOCK_TIMEOUT_SECONDS < GROWW_WRITE_DEADLINE_S

    def test_ordering_margin_is_real_not_epsilon(self) -> None:
        """>= 10 s of margin so a slow-but-clean acquire still leaves the merge/render/
        fsync/replace time under the outer deadline (75 s vs 90 s = 15 s today)."""
        assert GROWW_WRITE_DEADLINE_S - DEFAULT_PARTITION_LOCK_TIMEOUT_SECONDS >= 10.0

    def test_audited_default_values(self) -> None:
        """Pin the audited pair so EITHER side moving forces a conscious re-derivation."""
        assert DEFAULT_PARTITION_LOCK_TIMEOUT_SECONDS == 75.0
        assert GROWW_WRITE_DEADLINE_S == 90.0


class TestInversionWarnSeam:
    def test_default_configuration_never_warns(self) -> None:
        """75 s < 90 s — the shipped defaults are NOT inverted; no warning fires."""
        with capture_logs() as logs:
            _call(deadline_seconds=None)
        assert _inversion_events(logs) == []

    def test_env_inverted_configuration_warns_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An env override re-inverting the ordering (the documented escape-hatch risk)
        WARNs exactly once per process — not once per window — with both effective values.
        """
        monkeypatch.setenv(PARTITION_LOCK_TIMEOUT_ENV, "200")
        with capture_logs() as logs:
            _call(deadline_seconds=None)
            _call(deadline_seconds=None)  # second window: deduped, no spam
        events = _inversion_events(logs)
        assert len(events) == 1
        assert events[0]["log_level"] == "warning"
        assert events[0]["partition_lock_timeout_s"] == 200.0
        assert events[0]["write_deadline_s"] == GROWW_WRITE_DEADLINE_S

    def test_equal_values_count_as_inverted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The invariant is STRICTLY-below: lock timeout == write deadline still leaves the
        lock bound effectively unreachable (no margin for the write after the acquire)."""
        monkeypatch.setenv(PARTITION_LOCK_TIMEOUT_ENV, str(GROWW_WRITE_DEADLINE_S))
        with capture_logs() as logs:
            _call(deadline_seconds=None)
        assert len(_inversion_events(logs)) == 1

    def test_reset_seam_rearms_the_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(PARTITION_LOCK_TIMEOUT_ENV, "200")
        with capture_logs() as logs:
            _call(deadline_seconds=None)
        assert len(_inversion_events(logs)) == 1
        reset_lock_timeout_inversion_warning()
        with capture_logs() as logs:
            _call(deadline_seconds=None)
        assert len(_inversion_events(logs)) == 1  # re-armed → warns again

    def test_explicit_per_call_deadline_skips_the_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit ``deadline_seconds`` (how the timeout tests drive tiny deadlines) is a
        caller choice, NOT the process configuration — the check must not false-fire there
        even when the tiny per-call deadline sits below the lock timeout."""
        monkeypatch.setenv(PARTITION_LOCK_TIMEOUT_ENV, "200")
        with capture_logs() as logs:
            _call(deadline_seconds=0.5)
        assert _inversion_events(logs) == []

    def test_inverted_write_deadline_env_also_warns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The inversion can come from EITHER side: lowering the write deadline below the
        default 75 s lock timeout also trips the warn."""
        monkeypatch.setenv(WRITE_DEADLINE_ENV, "30")
        with capture_logs() as logs:
            _call(deadline_seconds=None)
        events = _inversion_events(logs)
        assert len(events) == 1
        assert events[0]["partition_lock_timeout_s"] == DEFAULT_PARTITION_LOCK_TIMEOUT_SECONDS
        assert events[0]["write_deadline_s"] == 30.0
