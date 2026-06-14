"""Tests for the O(1) instrument↔connection mapping/sharding (:mod:`brutex.live.plan`)."""

from __future__ import annotations

import pytest
from brutex.live.plan import (
    DEFAULT_PER_CONNECTION_CAP,
    Instrument,
    build_grouped_plan,
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


# --- build_grouped_plan: spot-first / legs-after grouping (DEC-LIVE-SHARD-GROUPS-001) ---


def test_grouped_spot_only_one_connection() -> None:
    plan = build_grouped_plan([_i("S1", seg="IDX"), _i("S2", seg="IDX")], [], per_connection_cap=10)
    assert plan.num_connections == 1 and plan.total == 2
    assert plan.connection_of("NSE:IDX:S1") == 0
    assert [len(s) for s in plan.shards] == [2]


def test_grouped_spot_overflow_makes_two_spot_connections() -> None:
    spot = [_i(f"S{n}", seg="IDX") for n in range(3)]  # 3 spot, cap 2 → 2 spot shards
    plan = build_grouped_plan(spot, [], per_connection_cap=2)
    assert plan.num_connections == 2
    assert [len(s) for s in plan.shards] == [2, 1]
    # Both spot shards hold ONLY spot instruments.
    assert all(inst.segment == "IDX" for shard in plan.shards for inst in shard)


def test_grouped_boundary_never_mixes_spot_and_legs() -> None:
    # 3 spot + 3 legs, cap 2: spot fills shards [S0,S1],[S2]; legs start a NEW
    # shard [L0,L1],[L2] — the partial spot shard #1 is NEVER topped up with a leg.
    spot = [_i(f"S{n}", seg="IDX") for n in range(3)]
    legs = [_i(f"L{n}", "NIFTY") for n in range(3)]
    plan = build_grouped_plan(spot, legs, per_connection_cap=2)
    assert plan.num_connections == 4
    assert [len(s) for s in plan.shards] == [2, 1, 2, 1]
    # Shards 0-1 are pure spot, shards 2-3 are pure legs (boundary respected).
    assert all(inst.segment == "IDX" for inst in plan.shard(0) + plan.shard(1))
    assert all(inst.segment == "FNO" for inst in plan.shard(2) + plan.shard(3))
    assert plan.connection_of("NSE:IDX:S2") == 1  # trailing spot shard
    assert plan.connection_of("NSE:FNO:L0") == 2  # first leg shard, AFTER spot


def test_grouped_dedup_spot_wins_over_legs() -> None:
    # The same security identity appears as both spot and a leg → kept once, on
    # the SPOT connection (#0); the leg copy is dropped.
    shared = _i("DUP", "NIFTY", seg="IDX")
    plan = build_grouped_plan([shared], [shared, _i("L1", "NIFTY")], per_connection_cap=10)
    assert plan.total == 2  # DUP + L1, NOT 3
    assert plan.connection_of("NSE:IDX:DUP") == 0  # spot won — connection #0
    assert plan.connection_of("NSE:FNO:L1") == 1  # the genuine leg, AFTER spot
    # The dedup keeps the SPOT instance's underlying in the inverted index too.
    assert set(plan.legs_of("NIFTY")) == {"NSE:IDX:DUP", "NSE:FNO:L1"}


def test_grouped_empty_spot_legs_start_at_connection_zero() -> None:
    legs = [_i("L1", "NIFTY"), _i("L2", "NIFTY")]
    plan = build_grouped_plan([], legs, per_connection_cap=10)
    assert plan.num_connections == 1 and plan.total == 2
    assert plan.connection_of("NSE:FNO:L1") == 0  # no spot → legs ride #0
    assert plan.shards[0] == tuple(legs)


def test_grouped_empty_legs_is_spot_only() -> None:
    plan = build_grouped_plan([_i("S1", seg="IDX")], [], per_connection_cap=10)
    assert plan.num_connections == 1 and plan.total == 1
    assert plan.connection_of("NSE:IDX:S1") == 0


def test_grouped_both_empty_is_zero_connections() -> None:
    plan = build_grouped_plan([], [], per_connection_cap=10)
    assert plan.num_connections == 0 and plan.total == 0 and plan.shards == ()


def test_grouped_o1_connection_of_spot_key_vs_leg_key() -> None:
    plan = build_grouped_plan([_i("S1", seg="IDX")], [_i("L1", "NIFTY")], per_connection_cap=10)
    assert plan.connection_of("NSE:IDX:S1") == 0  # spot key → connection #0
    assert plan.connection_of("NSE:FNO:L1") == 1  # leg key → connection #1
    with pytest.raises(KeyError):
        plan.connection_of("NSE:FNO:NOPE")  # O(1) miss still raises


def test_grouped_bad_cap_raises() -> None:
    with pytest.raises(ValueError, match="per_connection_cap"):
        build_grouped_plan([], [], per_connection_cap=0)
