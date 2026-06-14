"""Unit tests for the N-connection live subscription orchestration.

Every collaborator (feed open, subscribe) is injected with a fake — no
``growwapi``, no network. Covers :mod:`brutex.live.multiconn`.
"""

from __future__ import annotations

from typing import Any

import pytest
from brutex.live import multiconn as mc


def _idx(token: str) -> dict[str, Any]:
    return {"exchange": "NSE", "segment": "IDX", "exchange_token": token, "instrument_type": "IDX"}


def _eq(token: str) -> dict[str, Any]:
    return {"exchange": "NSE", "segment": "CASH", "exchange_token": token, "instrument_type": "EQ"}


def _leg(token: str, *, underlying: str) -> dict[str, Any]:
    return {
        "exchange": "NSE",
        "segment": "FNO",
        "exchange_token": token,
        "instrument_type": "CE",
        "underlying_symbol": underlying,
    }


# ───────────────────────── _instrument_of ─────────────────────────


def test_instrument_of_reads_underlying_variants() -> None:
    assert mc._instrument_of(_leg("1", underlying="NIFTY")).underlying == "NIFTY"
    # the bare ``underlying`` key is honoured when ``underlying_symbol`` is absent
    assert mc._instrument_of({**_eq("2"), "underlying": "RELIANCE"}).underlying == "RELIANCE"
    # neither key → no underlying
    assert mc._instrument_of(_eq("3")).underlying is None


# ───────────────────────── plan_connections ───────────────────────


def test_plan_connections_spot_first_then_legs() -> None:
    spot = [_idx("26000"), _eq("100")]
    legs = [_leg("900", underlying="NIFTY"), _leg("901", underlying="NIFTY")]
    bindings, skipped, plan = mc.plan_connections(spot, legs, per_connection_cap=1000)
    assert skipped == 0
    # spot fills connection #0 ("both"), legs connection #1 ("ltp"), never mixed
    assert [b.connection_index for b in bindings] == [0, 1]
    assert bindings[0].feed_kind == "both" and bindings[0].is_spot
    assert {r["exchange_token"] for r in bindings[0].instruments} == {"26000", "100"}
    assert bindings[1].feed_kind == "ltp" and not bindings[1].is_spot
    assert {r["exchange_token"] for r in bindings[1].instruments} == {"900", "901"}
    assert plan.num_connections == 2 and bindings[0].size == 2


def test_plan_connections_shards_each_group_at_the_cap() -> None:
    spot = [_idx("26000"), _eq("100")]  # 2 → 2 spot shards at cap=1
    legs = [_leg("900", underlying="NIFTY"), _leg("901", underlying="BANKNIFTY")]
    bindings, skipped, _plan = mc.plan_connections(spot, legs, per_connection_cap=1)
    assert skipped == 0
    assert [b.feed_kind for b in bindings] == ["both", "both", "ltp", "ltp"]
    assert all(b.size == 1 for b in bindings)


def test_plan_connections_honours_the_ceiling_and_reports_skipped() -> None:
    spot = [_idx("26000"), _eq("100")]
    legs = [_leg("900", underlying="NIFTY"), _leg("901", underlying="NIFTY")]
    # cap=1 → 4 shards; ceiling=2 binds #0,#1 and skips the 2 leg shards
    bindings, skipped, plan = mc.plan_connections(spot, legs, per_connection_cap=1, ceiling=2)
    assert plan.num_connections == 4
    assert [b.connection_index for b in bindings] == [0, 1]
    assert skipped == 2


def test_plan_connections_dedups_cross_group_spot_wins() -> None:
    shared = _idx("26000")
    bindings, _skipped, _plan = mc.plan_connections([shared], [shared], per_connection_cap=1000)
    # the instrument present in BOTH groups is kept ONCE, on its spot connection
    assert len(bindings) == 1
    assert bindings[0].is_spot and bindings[0].size == 1


def test_plan_connections_empty_is_zero_connections() -> None:
    bindings, skipped, plan = mc.plan_connections([], [], per_connection_cap=1000)
    assert bindings == [] and skipped == 0 and plan.num_connections == 0


def test_plan_connections_zero_ceiling_skips_everything() -> None:
    bindings, skipped, plan = mc.plan_connections([_idx("26000")], [], ceiling=0)
    assert bindings == [] and skipped == plan.num_connections == 1


def test_plan_connections_rejects_negative_ceiling() -> None:
    with pytest.raises(ValueError, match="ceiling must be >= 0"):
        mc.plan_connections([_idx("26000")], [], ceiling=-1)


# ─────────────────────── open_and_subscribe ───────────────────────


class _FakeFeed:
    def __init__(self, ordinal: int) -> None:
        self.ordinal = ordinal


def test_open_and_subscribe_opens_one_feed_per_binding() -> None:
    spot = [_idx("26000")]
    legs = [_leg("900", underlying="NIFTY")]
    bindings, _skipped, _plan = mc.plan_connections(spot, legs, per_connection_cap=1000)

    opened_ordinals: list[int] = []
    calls: list[tuple[int, str, int]] = []
    cb = lambda: None  # noqa: E731 - sentinel callback

    def open_feed(ordinal: int) -> _FakeFeed:
        opened_ordinals.append(ordinal)
        return _FakeFeed(ordinal)

    def make_subscribe(feed: Any, instruments: Any, feed_kind: str):
        def subscribe(callback: Any) -> None:
            assert callback is cb  # the SAME callback feeds every connection
            calls.append((feed.ordinal, feed_kind, len(list(instruments))))

        return subscribe

    opened = mc.open_and_subscribe(
        bindings, open_feed=open_feed, make_subscribe=make_subscribe, callback=cb
    )
    assert opened_ordinals == [0, 1]
    assert calls == [(0, "both", 1), (1, "ltp", 1)]
    assert [o.binding.connection_index for o in opened] == [0, 1]
    assert opened[0].feed.ordinal == 0


# ──────────────────────────── report ──────────────────────────────


def test_build_report_summarises_per_connection() -> None:
    spot = [_idx("26000"), _eq("100")]
    legs = [_leg("900", underlying="NIFTY")]
    bindings, skipped, _plan = mc.plan_connections(spot, legs, per_connection_cap=1000)
    report = mc.build_report(bindings, skipped)
    assert report.num_connections == 2
    assert report.total_instruments == 3
    assert report.connections[0].feed_kind == "both" and report.connections[0].subscribed == 2
    assert report.summary() == "2 connections (1 spot + 1 leg), 3 instruments"
    md = report.render_markdown()
    assert "| #0 | both | 2 |" in md and "| #1 | ltp | 1 |" in md


def test_build_report_surfaces_skipped_shards() -> None:
    spot = [_idx("26000"), _eq("100")]
    legs = [_leg("900", underlying="NIFTY"), _leg("901", underlying="NIFTY")]
    bindings, skipped, _plan = mc.plan_connections(spot, legs, per_connection_cap=1, ceiling=2)
    report = mc.build_report(bindings, skipped)
    assert report.skipped_shards == 2
    assert "2 shard(s) over the ceiling" in report.summary()


# ─────────────────── make_multiconn_capture seam ──────────────────


def test_make_multiconn_capture_subscribes_all_and_merges_snapshots() -> None:
    spot = [_idx("26000")]
    legs = [_leg("900", underlying="NIFTY")]
    bindings, _skipped, _plan = mc.plan_connections(spot, legs, per_connection_cap=1000)

    def open_feed(ordinal: int) -> _FakeFeed:
        return _FakeFeed(ordinal)

    def make_subscribe(feed: Any, _instruments: Any, _feed_kind: str):
        return lambda _cb: None  # the real SDK subscribe is exercised elsewhere

    def snapshot_of(feed: _FakeFeed, feed_kind: str) -> dict[str, Any]:
        # each connection contributes a distinct instrument to the merged record
        return {f"conn{feed.ordinal}:{feed_kind}": float(feed.ordinal)}

    subscribe, read_snapshot, opened = mc.make_multiconn_capture(
        bindings, open_feed=open_feed, make_subscribe=make_subscribe, snapshot_of=snapshot_of
    )
    # nothing opened until subscribe() is called (the capture's start() does it)
    assert opened == []
    assert read_snapshot() == {}
    subscribe(lambda: None)
    assert [o.binding.connection_index for o in opened] == [0, 1]
    # the merged snapshot unions every connection's snapshot
    assert read_snapshot() == {"conn0:both": 0.0, "conn1:ltp": 1.0}
