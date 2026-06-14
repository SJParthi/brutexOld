"""Tests for the O(1) instrument↔connection mapping/sharding (:mod:`brutex.live.plan`)."""

from __future__ import annotations

import pytest
from brutex.live.plan import (
    DEFAULT_PER_CONNECTION_CAP,
    Instrument,
    build_plan,
    connection_count,
)


def _i(
    token: str, underlying: str | None = None, *, exch: str = "NSE", seg: str = "FNO"
) -> Instrument:
    return Instrument(exchange=exch, segment=seg, exchange_token=token, underlying=underlying)


def test_default_cap_is_1000() -> None:
    assert DEFAULT_PER_CONNECTION_CAP == 1000


def test_instrument_key_and_feed_dict() -> None:
    inst = Instrument("NSE", "IDX", "26000")
    assert inst.key == "NSE:IDX:26000"
    assert inst.feed_dict() == {"exchange": "NSE", "segment": "IDX", "exchange_token": "26000"}
    assert inst.underlying is None


def test_single_connection_and_o1_lookup() -> None:
    plan = build_plan([_i("1"), _i("2")], per_connection_cap=10)
    assert plan.num_connections == 1 and plan.total == 2
    assert plan.connection_of("NSE:FNO:1") == 0
    assert len(plan.shard(0)) == 2


def test_shards_pack_over_cap() -> None:
    plan = build_plan([_i(str(n)) for n in range(5)], per_connection_cap=2)
    assert plan.num_connections == 3
    assert [len(s) for s in plan.shards] == [2, 2, 1]
    assert plan.connection_of("NSE:FNO:4") == 2  # the trailing shard


def test_dedup_drops_duplicate_keys() -> None:
    plan = build_plan([_i("1"), _i("1"), _i("2")], per_connection_cap=10)
    assert plan.total == 2


def test_underlying_inverted_index() -> None:
    plan = build_plan(
        [_i("F1", "NIFTY"), _i("O1", "NIFTY"), Instrument("NSE", "IDX", "26000")],
        per_connection_cap=10,
    )
    assert set(plan.legs_of("NIFTY")) == {"NSE:FNO:F1", "NSE:FNO:O1"}
    assert plan.legs_of("UNKNOWN") == ()  # O(1) miss
    assert "NIFTY" in plan.by_underlying


def test_connection_of_unknown_raises() -> None:
    plan = build_plan([_i("1")], per_connection_cap=10)
    with pytest.raises(KeyError):
        plan.connection_of("NSE:FNO:999")


def test_empty_universe_is_zero_connections() -> None:
    plan = build_plan([], per_connection_cap=10)
    assert plan.num_connections == 0 and plan.total == 0 and plan.shards == ()


def test_bad_cap_raises() -> None:
    with pytest.raises(ValueError, match="per_connection_cap"):
        build_plan([], per_connection_cap=0)


def test_connection_count() -> None:
    assert connection_count(0) == 0
    assert connection_count(1000) == 1
    assert connection_count(1001) == 2
    assert connection_count(241) == 1  # the full universe fits one connection
    with pytest.raises(ValueError, match="per_connection_cap"):
        connection_count(5, per_connection_cap=0)
