"""Tests for `brutex.data.fetch_coverage` — the O(1) fetch-window ledger.

100% line+branch: membership before/after mark, idempotent mark, the count,
and the deterministic serialise → restore round-trip.
"""

from __future__ import annotations

from datetime import date

from brutex.data.fetch_coverage import FetchCoverage, WindowKey


def _key(security_id: str = "13", interval: str = "1minute") -> WindowKey:
    return WindowKey(
        market="dhan",
        security_id=security_id,
        interval=interval,
        start=date(2021, 1, 1),
        end=date(2021, 4, 1),
    )


def test_empty_coverage_has_nothing_complete() -> None:
    cov = FetchCoverage()
    assert cov.completed_count == 0
    assert cov.is_complete(_key()) is False


def test_mark_then_complete_is_o1_membership() -> None:
    cov = FetchCoverage()
    key = _key()
    cov.mark_complete(key)
    assert cov.is_complete(key) is True
    assert cov.completed_count == 1
    # A different window is still incomplete.
    assert cov.is_complete(_key(security_id="25")) is False


def test_mark_is_idempotent() -> None:
    cov = FetchCoverage()
    key = _key()
    cov.mark_complete(key)
    cov.mark_complete(key)
    assert cov.completed_count == 1


def test_unmark_drops_a_completed_window() -> None:
    # DEC-FAILED-WINDOW-FORCE-REPULL-001: un-marking a complete window makes it
    # incomplete again so the ingest re-pulls it.
    cov = FetchCoverage()
    key = _key()
    cov.mark_complete(key)
    assert cov.is_complete(key) is True
    cov.unmark(key)
    assert cov.is_complete(key) is False
    assert cov.completed_count == 0


def test_unmark_absent_window_is_a_noop() -> None:
    cov = FetchCoverage()
    cov.unmark(_key())  # never marked → discard is a safe no-op
    assert cov.completed_count == 0


def test_serialise_restore_round_trip() -> None:
    cov = FetchCoverage()
    cov.mark_complete(_key("13", "1minute"))
    cov.mark_complete(_key("25", "1day"))
    records = cov.to_records()
    # Deterministic + sorted.
    assert records == sorted(records)
    assert ("dhan", "13", "1minute", "2021-01-01", "2021-04-01") in records

    restored = FetchCoverage.from_records(records)
    assert restored.completed_count == 2
    assert restored.is_complete(_key("13", "1minute")) is True
    assert restored.is_complete(_key("25", "1day")) is True
    assert restored.to_records() == records
