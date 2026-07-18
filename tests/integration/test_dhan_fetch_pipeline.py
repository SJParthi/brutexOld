"""Integration tests — fetch→parse end-to-end through the REAL Dhan client + parsers.

DEC-DHAN-ADAPTER-001. Drives the REAL ``DhanMarketAdapter`` / orchestration modules /
``DhanClient`` / parsers via the injected ``FakeDhanTransport`` — no network, no
``requests``. Covers: the 3s option-chain special pacer wiring, retry + reauth
ladders through a multi-window fetch, and the AWS credential path via the injected
fake (belt-guarded).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from brutex.adapters.dhan.adapter import DhanMarketAdapter
from brutex.adapters.dhan.client import DhanClient
from brutex.adapters.dhan.constants import (
    DHAN_OPTION_CHAIN_MIN_INTERVAL_S,
    DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT,
    DHAN_TOKEN_PARAM_CLIENT_ID_DEFAULT,
)
from brutex.adapters.dhan.credentials import DhanCredentials, load_dhan_aws_credentials
from brutex.adapters.dhan.errors import DhanAuthError
from brutex.adapters.dhan.expired_options import fetch_rolling_option
from brutex.adapters.dhan.instruments import DhanInstrumentIndex, DhanScripRow
from brutex.adapters.dhan.option_chain import fetch_expiry_list, fetch_option_chain
from brutex.adapters.dhan.reauth import DhanTokenRefresher
from brutex.adapters.dhan.token_store_aws import build_readonly_token_store
from brutex.models import Exchange

from tests.fakes.fake_aws_client import FakeAwsClient
from tests.fakes.fake_dhan_transport import (
    FakeDhanTransport,
    Raise,
    Response,
    empty_body_error,
    timeout_error,
)


def _scrip(sec: int, sym: str, seg: str, itype: str) -> DhanScripRow:
    return DhanScripRow(
        security_id=sec,
        trading_symbol=sym,
        exchange_segment=seg,
        instrument_type=itype,
        series=None,
        expiry_date=None,
        option_type=None,
        strike_price=None,
        lot_size=None,
        tick_size=None,
    )


def _client(transport: FakeDhanTransport, **kwargs: Any) -> DhanClient:
    return DhanClient(transport=transport, access_token="tok0", client_id="1100", **kwargs)


def test_end_to_end_intraday_fetch_with_transient_storm() -> None:
    """A multi-window intraday fetch survives a 429 + timeout + empty-body burst."""
    payload = FakeDhanTransport.candle_payload(n=3)
    transport = FakeDhanTransport(
        script=[
            Response(429, {}),  # window 1: throttle → retry
            Response(200, payload),
            Raise(timeout_error()),  # window 2: socket timeout → retry
            Response(200, payload),
            Raise(empty_body_error()),  # window 3: empty body → retry
            Response(200, payload),
        ]
    )
    index = DhanInstrumentIndex.build([_scrip(13, "NIFTY", "IDX_I", "INDEX")])
    adapter = DhanMarketAdapter(client=_client(transport), index=index)
    bars = adapter.fetch_ohlcv(
        symbol="NIFTY",
        exchange=Exchange.NSE,
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 7, 1),  # 3 windows at the 90-day cap
    )
    assert len(bars) == 9  # 3 windows × 3 bars, every transient healed
    assert transport.calls == 6
    assert all(bar.volume == 0 for bar in bars)  # index volume dropped


def test_end_to_end_reauth_mid_fetch() -> None:
    """A 401 mid-fetch self-heals via the refresher re-read, and the fresh token is used."""
    payload = FakeDhanTransport.candle_payload(n=1)
    empty_columns: dict[str, object] = {
        "open": [],
        "high": [],
        "low": [],
        "close": [],
        "volume": [],
        "timestamp": [],
    }
    transport = FakeDhanTransport(
        script=[
            Response(200, payload),
            Response(401, {"errorCode": "DH-901"}),  # token dies mid-run
            Response(200, payload),
        ],
        default=Response(200, empty_columns),  # the 3rd tiled window: a clean empty
    )
    refresher = DhanTokenRefresher(initial_token="tok0", read_token=lambda: "tok1")
    index = DhanInstrumentIndex.build([_scrip(2885, "RELIANCE", "NSE_EQ", "EQUITY")])
    adapter = DhanMarketAdapter(client=_client(transport, refresher=refresher), index=index)
    bars = adapter.fetch_ohlcv(
        symbol="RELIANCE",
        exchange=Exchange.NSE,
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 7, 1),
    )
    assert len(bars) == 2  # window 1 + healed window 2 (3rd window rides default 200 {})
    assert transport.seen_headers[0]["access-token"] == "tok0"
    assert transport.seen_headers[2]["access-token"] == "tok1"  # fresh token adopted


def test_option_chain_pipeline_with_3s_special_pacer() -> None:
    """expiry-list → option-chain end-to-end; the chain call rides the SPECIAL pacer."""
    chain_pace_calls: list[float] = []

    def chain_pace() -> None:
        # a live site wires a 3-second min-interval pacer here — record its cadence hook
        chain_pace_calls.append(DHAN_OPTION_CHAIN_MIN_INTERVAL_S)

    transport = FakeDhanTransport(
        script=[
            Response(200, FakeDhanTransport.expiry_list_payload()),
            Response(200, FakeDhanTransport.option_chain_payload()),
        ]
    )
    client = _client(transport, option_chain_pace=chain_pace)
    expiries = fetch_expiry_list(client, underlying_scrip=13, underlying_seg="IDX_I")
    snap = fetch_option_chain(
        client, underlying_scrip=13, underlying_seg="IDX_I", expiry=expiries[0]
    )
    assert expiries == [date(2026, 8, 27), date(2026, 9, 24)]
    assert snap.last_price == Decimal("20050.0")
    assert chain_pace_calls == [3.0]  # ONLY the chain call rides the special pacer
    assert transport.seen_headers[1]["client-id"] == "1100"


def test_rolling_option_pipeline() -> None:
    """The expired-options (rolling) fetch parses ce+pe sides end-to-end."""
    transport = FakeDhanTransport(
        script=[Response(200, FakeDhanTransport.rolling_option_payload(n=2))]
    )
    bars = fetch_rolling_option(
        _client(transport),
        exchange_segment="NSE_FNO",
        security_id="13",
        instrument="OPTIDX",
        expiry_flag="MONTH",
        expiry_code=0,
        strike="ATM",
        drv_option_type="CALL",
        interval="1",
        from_date="2024-01-01 09:15:00",
        to_date="2024-01-30 15:30:00",
    )
    assert [b.side for b in bars] == ["CE", "CE", "PE", "PE"]
    assert bars[0].iv == Decimal("0.15")


def test_aws_credentials_and_token_store_via_injected_fake() -> None:
    """Credentials + read-only token store + refresher compose end-to-end (fake AWS)."""
    fake_aws = FakeAwsClient(
        parameters={
            DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT: "jwt-live",
            DHAN_TOKEN_PARAM_CLIENT_ID_DEFAULT: "1100",
        }
    )
    creds = load_dhan_aws_credentials(client=fake_aws)
    assert creds == DhanCredentials(access_token="jwt-live", client_id="1100")

    store = build_readonly_token_store(client=fake_aws)
    refresher = DhanTokenRefresher(initial_token=creds.access_token, read_token=store.read_token)
    # rotate the shared token in "SSM"; a reauth adopts it
    fake_aws.parameters[DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT] = "jwt-fresh"
    refresher.reauth()
    assert refresher.access_token == "jwt-fresh"

    # the read-only store REFUSES a mint loudly
    with pytest.raises(DhanAuthError):
        store.store("never-minted")
