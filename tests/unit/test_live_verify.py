"""Tests for the live-feed subscription self-test (:mod:`brutex.live.verify`)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from brutex.live.store import TickStore
from brutex.live.verify import (
    InstrumentCheck,
    SubscriptionReport,
    render_subscription_markdown,
    verify_subscription,
)

_OPEN = datetime(2026, 6, 15, 3, 45, tzinfo=UTC)


class _Feed:
    """Fake feed: fires the callback; each callback reads the next canned snapshot."""

    def __init__(self, snapshots: list[dict[str, Any]]) -> None:
        self._snaps = snapshots
        self._i = 0
        self._cb = None

    def subscribe(self, callback) -> None:
        self._cb = callback

    def read(self) -> dict[str, Any]:
        snap = self._snaps[min(self._i, len(self._snaps) - 1)]
        self._i += 1
        return snap

    def fire(self) -> None:
        assert self._cb is not None
        self._cb()


def _run(feed: _Feed, *, requested: int, store: TickStore) -> SubscriptionReport:
    return verify_subscription(
        store=store,
        subscribe=feed.subscribe,
        read_snapshot=feed.read,
        requested=requested,
        seconds=1.0,
        sleep=lambda _s: feed.fire(),
        monotonic=iter([0.0, 0.4, 0.8, 1.2]).__next__,
        clock=lambda: _OPEN,
        ns_clock=lambda: 1,
    )


def test_verify_all_requested_ticking(tmp_path) -> None:
    feed = _Feed([{"NIFTY": 100, "BANKNIFTY": 200}, {"NIFTY": 101, "BANKNIFTY": 201}])
    with TickStore.open(tmp_path / "t.jsonl") as store:
        report = _run(feed, requested=2, store=store)
    assert report.observed == 2 and report.total_ticks == 4  # 2 fires × 2 instruments
    assert report.all_requested_ticking and report.silent == 0
    assert "ALL TICKING" in report.summary()
    assert {c.instrument_key for c in report.checks} == {"BANKNIFTY", "NIFTY"}
    assert all(c.live for c in report.checks)
    # the self-test ALSO persisted the ticks.
    assert len(TickStore.read(tmp_path / "t.jsonl")) == 2


def test_verify_reports_silent_instruments(tmp_path) -> None:
    feed = _Feed([{"NIFTY": 100}, {"NIFTY": 101}])
    with TickStore.open(tmp_path / "t.jsonl") as store:
        report = _run(feed, requested=3, store=store)  # asked 3, only 1 ticks
    assert report.observed == 1 and report.silent == 2
    assert not report.all_requested_ticking
    assert "SOME SILENT" in report.summary()


def test_verify_zero_requested_is_not_all_ticking(tmp_path) -> None:
    feed = _Feed([{}])
    with TickStore.open(tmp_path / "t.jsonl") as store:
        report = _run(feed, requested=0, store=store)
    assert not report.all_requested_ticking and report.silent == 0


def test_instrument_check_live() -> None:
    assert InstrumentCheck("X", 3).live
    assert not InstrumentCheck("Y", 0).live


def test_render_markdown_ticking_silent_and_truncation() -> None:
    report = SubscriptionReport(
        requested=2,
        observed=1,
        total_ticks=5,
        seconds=2.0,
        checks=(InstrumentCheck("NIFTY", 5), InstrumentCheck("DEADSYM", 0)),
    )
    md = render_subscription_markdown(report)
    assert "subscription self-test" in md and "SOME SILENT" in md
    assert "| NIFTY | 5 | ✅ |" in md
    assert "| DEADSYM | 0 | — |" in md

    big = SubscriptionReport(
        requested=5,
        observed=5,
        total_ticks=5,
        seconds=1.0,
        checks=tuple(InstrumentCheck(f"I{i}", 1) for i in range(5)),
    )
    out = render_subscription_markdown(big, max_rows=2)
    assert "ALL TICKING" in out and "more instruments" in out
