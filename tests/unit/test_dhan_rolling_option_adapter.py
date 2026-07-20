"""Tests for the RollingOptionAdapter + the VERBATIM body-shape honesty pins.

DEC-DHAN-ROLLING-OPTIONS-001.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

import pytest
from brutex.adapters.dhan.rolling_option_adapter import (
    DHAN_ROLLING_CAPABILITIES,
    RollingOptionAdapter,
    build_rolling_option_body,
)
from brutex.adapters.dhan.rolling_option_plan import RollingOptionKey
from brutex.models import Exchange, OutOfScopeError

_KEY = RollingOptionKey(
    underlying="NIFTY",
    exchange="NSE",
    exchange_segment="NSE_FNO",
    security_id=13,
    expiry_flag="WEEK",
    expiry_code=0,
    strike_offset="ATM+1",
    option_type="CALL",
)

_PAYLOAD: dict[str, list[object]] = {
    "timestamp": [1_717_386_300.0],
    "open": [100],
    "high": [102],
    "low": [99],
    "close": [101],
}


class _FakeClient:
    def __init__(self, payload: Mapping[str, object]) -> None:
        self.payload = payload
        self.bodies: list[Mapping[str, object]] = []

    def rolling_option_data(self, *, body: Mapping[str, object]) -> Mapping[str, object]:
        self.bodies.append(body)
        return self.payload


# ── build_rolling_option_body — the VERBATIM shape ───────────────────────────


def test_body_is_verbatim_and_has_no_dhan_client_id() -> None:
    body = build_rolling_option_body(key=_KEY, from_date="2024-06-01", to_date="2024-06-30")
    assert set(body) == {
        "securityId",
        "exchangeSegment",
        "instrument",
        "expiryFlag",
        "expiryCode",
        "strike",
        "drvOptionType",
        "requiredData",
        "fromDate",
        "toDate",
        "interval",
    }
    # SETTLED: client-id travels only in the header, NEVER the body.
    assert "dhanClientId" not in body
    assert "client-id" not in body
    assert body["securityId"] == 13
    assert body["exchangeSegment"] == "NSE_FNO"
    assert body["instrument"] == "INDEX"  # never the OPTIDX alternate
    assert body["expiryFlag"] == "WEEK"
    assert body["expiryCode"] == 0
    assert body["strike"] == "ATM+1"  # the ATM-relative selector label
    assert body["drvOptionType"] == "CALL"
    assert body["requiredData"] == [
        "open",
        "high",
        "low",
        "close",
        "iv",
        "volume",
        "strike",
        "oi",
        "spot",
    ]
    assert body["fromDate"] == "2024-06-01"
    assert body["toDate"] == "2024-06-30"
    assert body["interval"] == 1


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("expiry_flag", "DAILY", "expiryFlag"),
        ("expiry_code", 9, "expiryCode"),
        ("option_type", "STRADDLE", "drvOptionType"),
    ],
)
def test_body_rejects_out_of_set_enums(field: str, value: object, match: str) -> None:
    import dataclasses

    bad_key = dataclasses.replace(_KEY, **{field: value})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=match):
        build_rolling_option_body(key=bad_key, from_date="2024-06-01", to_date="2024-06-30")


def test_body_rejects_empty_instrument() -> None:
    with pytest.raises(ValueError, match="instrument must be a non-empty"):
        build_rolling_option_body(
            key=_KEY, from_date="2024-06-01", to_date="2024-06-30", instrument=""
        )


# ── fetch_rolling_option ──────────────────────────────────────────────────────


def test_capabilities_are_rolling_atm() -> None:
    client = _FakeClient(_PAYLOAD)
    adapter = RollingOptionAdapter(client=client)
    caps = adapter.capabilities
    assert caps is DHAN_ROLLING_CAPABILITIES
    assert caps.market_id == "dhan-rolling-options"
    assert caps.has_derivatives is True
    assert caps.has_option_chains is False
    assert Exchange.NSE in caps.exchanges and Exchange.BSE in caps.exchanges


def test_fetch_single_window_parses_bars() -> None:
    client = _FakeClient(_PAYLOAD)
    adapter = RollingOptionAdapter(client=client)
    bars = adapter.fetch_rolling_option(key=_KEY, start=date(2024, 6, 1), end=date(2024, 6, 15))
    assert len(bars) == 1  # one payload row
    assert len(client.bodies) == 1  # a 14-day span fits ONE 30-day window
    assert client.bodies[0]["fromDate"] == "2024-06-01"
    assert client.bodies[0]["toDate"] == "2024-06-15"


def test_fetch_tiles_multiple_30_day_windows() -> None:
    client = _FakeClient(_PAYLOAD)
    adapter = RollingOptionAdapter(client=client)
    # 45-day span → 2 windows → 2 client calls.
    adapter.fetch_rolling_option(key=_KEY, start=date(2024, 6, 1), end=date(2024, 7, 16))
    assert len(client.bodies) == 2
    assert client.bodies[0]["fromDate"] == "2024-06-01"
    assert client.bodies[1]["fromDate"] == "2024-07-01"


def test_fetch_threads_derive_expiry_seam() -> None:
    client = _FakeClient(_PAYLOAD)

    def deriver(bar_date: date, flag: str, code: int) -> date:
        return date(2024, 6, 6)

    adapter = RollingOptionAdapter(client=client, derive_expiry=deriver)
    bars = adapter.fetch_rolling_option(key=_KEY, start=date(2024, 6, 1), end=date(2024, 6, 15))
    assert bars[0].resolved_expiry_date == date(2024, 6, 6)
    assert bars[0].resolved_expiry_source == "derived"


def test_fetch_rejects_unresolvable_security_id() -> None:
    import dataclasses

    client = _FakeClient(_PAYLOAD)
    adapter = RollingOptionAdapter(client=client)
    with pytest.raises(OutOfScopeError, match="no resolvable securityId"):
        adapter.fetch_rolling_option(
            key=dataclasses.replace(_KEY, security_id=0),
            start=date(2024, 6, 1),
            end=date(2024, 6, 15),
        )
    assert client.bodies == []  # never even attempted a fetch


def test_fetch_uses_configured_instrument_and_required_data() -> None:
    client = _FakeClient(_PAYLOAD)
    adapter = RollingOptionAdapter(client=client, instrument="INDEX", required_data=("close",))
    adapter.fetch_rolling_option(key=_KEY, start=date(2024, 6, 1), end=date(2024, 6, 2))
    assert client.bodies[0]["requiredData"] == ["close"]
    assert client.bodies[0]["instrument"] == "INDEX"
