"""Unit tests for :mod:`brutex.adapters.dhan.pool` — 100% line+branch."""

from __future__ import annotations

import pytest
from brutex.adapters.dhan.client import DhanClient, DhanClientProtocol
from brutex.adapters.dhan.pool import DEFAULT_POOL_SIZE, DhanClientPool

from tests.fakes.fake_dhan_transport import FakeDhanTransport, Response


def _client(transport: FakeDhanTransport) -> DhanClient:
    return DhanClient(transport=transport, access_token="tok", client_id="1")


def _ok() -> Response:
    return Response(200, {"ok": True})


def test_default_pool_size_is_small() -> None:
    assert DEFAULT_POOL_SIZE == 2


def test_empty_pool_raises() -> None:
    with pytest.raises(ValueError, match="at least one client"):
        DhanClientPool([])


def test_round_robin_dispatch() -> None:
    t1, t2 = FakeDhanTransport(default=_ok()), FakeDhanTransport(default=_ok())
    pool = DhanClientPool([_client(t1), _client(t2)])
    for _ in range(4):
        pool.expiry_list(underlying_scrip=1, underlying_seg="IDX_I")
    assert t1.calls == 2
    assert t2.calls == 2
    assert pool.size == 2


def test_shared_meter_aggregates() -> None:
    t1, t2 = FakeDhanTransport(default=_ok()), FakeDhanTransport(default=_ok())
    pool = DhanClientPool([_client(t1), _client(t2)])
    pool.historical_candles(
        security_id="1",
        exchange_segment="IDX_I",
        instrument="INDEX",
        from_date="2024-01-01",
        to_date="2024-01-31",
    )
    pool.intraday_candles(
        security_id="1",
        exchange_segment="IDX_I",
        instrument="INDEX",
        interval="1",
        from_date="2024-01-01 09:15:00",
        to_date="2024-01-31 15:30:00",
    )
    snap = pool.meter.snapshot()
    assert snap["issued"] == 2  # BOTH leaves recorded into the ONE shared meter
    assert snap["completed"] == 2


def test_all_endpoint_dispatchers() -> None:
    t = FakeDhanTransport(default=_ok())
    pool = DhanClientPool([_client(t)])
    pool.option_chain(underlying_scrip=1, underlying_seg="IDX_I", expiry="2026-08-27")
    pool.rolling_option(
        exchange_segment="NSE_FNO",
        security_id="1",
        instrument="OPTIDX",
        expiry_flag="MONTH",
        expiry_code=0,
        strike="ATM",
        drv_option_type="CALL",
        interval="1",
        from_date="2024-01-01 09:15:00",
        to_date="2024-01-30 15:30:00",
        required_data=["open"],
    )
    assert t.calls == 2


def test_with_pace_rebinds_every_leaf() -> None:
    paces: list[int] = []
    t = FakeDhanTransport(default=_ok())
    pool = DhanClientPool([_client(t), _client(FakeDhanTransport(default=_ok()))])
    bound = pool.with_pace(lambda: paces.append(1))
    bound.expiry_list(underlying_scrip=1, underlying_seg="IDX_I")
    bound.expiry_list(underlying_scrip=1, underlying_seg="IDX_I")
    assert paces == [1, 1]  # both leaves paced


def test_with_rate_hooks_rebinds_every_leaf() -> None:
    hits: list[str] = []
    t = FakeDhanTransport(script=[Response(429, {}), _ok()])
    pool = DhanClientPool([_client(t)])
    hooked = pool.with_rate_hooks(
        pace=lambda: None,
        on_rate_limit=lambda: hits.append("429"),
        on_success=lambda: hits.append("ok"),
    )
    hooked.expiry_list(underlying_scrip=1, underlying_seg="IDX_I")
    assert hits == ["429", "ok"]


def test_pool_satisfies_protocol() -> None:
    pool = DhanClientPool([_client(FakeDhanTransport())])
    assert isinstance(pool, DhanClientProtocol)
