"""Unit tests for :mod:`brutex.adapters.dhan.client` — 100% line+branch.

Driven by the ``FakeDhanTransport`` through the REAL client (retry ladders, reauth,
pacing hooks, meter bracket) — no ``requests``, no network.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from brutex.adapters.dhan.client import DhanClient, DhanClientProtocol
from brutex.adapters.dhan.constants import (
    DHAN_ENDPOINT_EXPIRY_LIST,
    DHAN_ENDPOINT_HISTORICAL,
    DHAN_ENDPOINT_INTRADAY,
    DHAN_ENDPOINT_OPTION_CHAIN,
    DHAN_ENDPOINT_PROFILE,
    DHAN_ENDPOINT_RENEW_TOKEN,
    DHAN_ENDPOINT_ROLLING_OPTION,
    DHAN_HEADER_ACCESS_TOKEN,
    DHAN_HEADER_CLIENT_ID,
    DHAN_HEADER_DHAN_CLIENT_ID_UNVERIFIED,
)
from brutex.adapters.dhan.errors import (
    DhanAuthError,
    DhanBadRequestError,
    DhanEmptyResponseError,
    DhanError,
    DhanNetworkError,
    DhanRateLimitError,
    DhanTimeoutError,
    DhanTokenStillStaleError,
)
from brutex.adapters.dhan.reauth import DhanTokenRefresher

from tests.fakes.fake_dhan_transport import (
    FakeDhanTransport,
    Raise,
    Response,
    empty_body_error,
    network_error,
    timeout_error,
)


def _client(transport: FakeDhanTransport, **kwargs: Any) -> DhanClient:
    return DhanClient(transport=transport, access_token="tok0", client_id="1100", **kwargs)


def _ok(body: Mapping[str, object] | None = None) -> Response:
    return Response(200, body if body is not None else {"ok": True})


# ── happy paths per endpoint ───────────────────────────────────────────────


def test_historical_candles_happy() -> None:
    transport = FakeDhanTransport(script=[_ok({"open": []})])
    client = _client(transport)
    got = client.historical_candles(
        security_id="13",
        exchange_segment="IDX_I",
        instrument="INDEX",
        from_date="2024-01-01",
        to_date="2024-06-30",
    )
    assert got == {"open": []}
    assert transport.seen_paths == [DHAN_ENDPOINT_HISTORICAL]
    assert transport.seen_headers[0][DHAN_HEADER_ACCESS_TOKEN] == "tok0"
    assert DHAN_HEADER_CLIENT_ID not in transport.seen_headers[0]
    assert transport.seen_bodies[0]["securityId"] == "13"
    assert "expiryCode" not in transport.seen_bodies[0]


def test_historical_candles_with_expiry_code() -> None:
    transport = FakeDhanTransport(script=[_ok()])
    _client(transport).historical_candles(
        security_id="13",
        exchange_segment="NSE_FNO",
        instrument="FUTIDX",
        from_date="2024-01-01",
        to_date="2024-06-30",
        expiry_code=1,
    )
    assert transport.seen_bodies[0]["expiryCode"] == 1


def test_intraday_candles_happy() -> None:
    transport = FakeDhanTransport(script=[_ok()])
    _client(transport).intraday_candles(
        security_id="13",
        exchange_segment="IDX_I",
        instrument="INDEX",
        interval="1",
        from_date="2024-01-01 09:15:00",
        to_date="2024-03-01 15:30:00",
    )
    assert transport.seen_paths == [DHAN_ENDPOINT_INTRADAY]
    assert transport.seen_bodies[0]["interval"] == "1"


def test_option_chain_sends_client_id_and_special_pace() -> None:
    paces: list[str] = []
    transport = FakeDhanTransport(script=[_ok()])
    client = _client(
        transport,
        pace=lambda: paces.append("data"),
        option_chain_pace=lambda: paces.append("chain"),
    )
    client.option_chain(underlying_scrip=13, underlying_seg="IDX_I", expiry="2026-08-27")
    assert transport.seen_paths == [DHAN_ENDPOINT_OPTION_CHAIN]
    assert transport.seen_headers[0][DHAN_HEADER_CLIENT_ID] == "1100"
    # the shared Data-API token FIRST, then the SPECIAL 3s pacer (ADDITIONAL, not
    # instead-of — round-1 concurrency finding: chain-only pacing would issue
    # outside the shared 5/s aggregate bucket)
    assert paces == ["data", "chain"]


def test_expiry_list_happy() -> None:
    transport = FakeDhanTransport(script=[_ok()])
    _client(transport).expiry_list(underlying_scrip=13, underlying_seg="IDX_I")
    assert transport.seen_paths == [DHAN_ENDPOINT_EXPIRY_LIST]
    assert transport.seen_headers[0][DHAN_HEADER_CLIENT_ID] == "1100"


def test_rolling_option_happy() -> None:
    transport = FakeDhanTransport(script=[_ok()])
    _client(transport).rolling_option(
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
        required_data=["open", "high", "low", "close", "volume"],
    )
    assert transport.seen_paths == [DHAN_ENDPOINT_ROLLING_OPTION]
    assert transport.seen_bodies[0]["strike"] == "ATM"
    assert transport.seen_bodies[0]["requiredData"] == ["open", "high", "low", "close", "volume"]


def test_renew_token_sends_dhan_client_id() -> None:
    transport = FakeDhanTransport(script=[_ok()])
    _client(transport).renew_token()
    assert transport.seen_paths == [DHAN_ENDPOINT_RENEW_TOKEN]
    assert transport.seen_headers[0][DHAN_HEADER_DHAN_CLIENT_ID_UNVERIFIED] == "1100"


def test_validate_token_true_on_2xx() -> None:
    transport = FakeDhanTransport(script=[_ok()])
    assert _client(transport).validate_token() is True
    assert transport.seen_paths == [DHAN_ENDPOINT_PROFILE]


def test_validate_token_raises_on_auth_failure() -> None:
    transport = FakeDhanTransport(script=[Response(401, {"errorCode": "DH-901"})])
    with pytest.raises(DhanAuthError):
        _client(transport).validate_token()


# ── retry ladder ───────────────────────────────────────────────────────────


def test_rate_limit_retries_then_succeeds_and_feeds_hooks() -> None:
    hits: list[str] = []
    sleeps: list[float] = []
    transport = FakeDhanTransport(script=[Response(429, {}), _ok()])
    client = _client(
        transport,
        sleep=sleeps.append,
        on_rate_limit=lambda: hits.append("429"),
        on_success=lambda: hits.append("ok"),
    )
    got = client.expiry_list(underlying_scrip=1, underlying_seg="IDX_I")
    assert got == {"ok": True}
    assert hits == ["429", "ok"]  # decrement on the 429, recover on the clean call
    assert sleeps == [2.0]  # rate_limit_backoff_seconds × attempt 1


def test_retryable_exhausts_budget_and_raises() -> None:
    transport = FakeDhanTransport(script=[Response(503, {})] * 10)
    client = _client(transport, max_attempts=3)
    with pytest.raises(Exception) as excinfo:
        client.expiry_list(underlying_scrip=1, underlying_seg="IDX_I")
    assert "503" in str(excinfo.value)
    assert transport.calls == 3  # exactly max_attempts physical calls


def test_terminal_error_raises_immediately() -> None:
    transport = FakeDhanTransport(script=[Response(400, {"errorCode": "DH-905"})])
    client = _client(transport)
    with pytest.raises(DhanBadRequestError):
        client.expiry_list(underlying_scrip=1, underlying_seg="IDX_I")
    assert transport.calls == 1


def test_transport_empty_body_is_retried() -> None:
    transport = FakeDhanTransport(script=[Raise(empty_body_error()), _ok()])
    client = _client(transport)
    assert client.expiry_list(underlying_scrip=1, underlying_seg="IDX_I") == {"ok": True}
    assert transport.calls == 2


def test_transport_network_blip_is_retried() -> None:
    transport = FakeDhanTransport(script=[Raise(network_error()), _ok()])
    client = _client(transport)
    assert client.expiry_list(underlying_scrip=1, underlying_seg="IDX_I") == {"ok": True}


def test_transport_timeout_is_retried() -> None:
    transport = FakeDhanTransport(script=[Raise(timeout_error()), _ok()])
    client = _client(transport)
    assert client.expiry_list(underlying_scrip=1, underlying_seg="IDX_I") == {"ok": True}


def test_transport_timeout_exhaustion_raises_timeout() -> None:
    transport = FakeDhanTransport(script=[Raise(timeout_error())] * 5)
    client = _client(transport, max_attempts=2)
    with pytest.raises(DhanTimeoutError):
        client.expiry_list(underlying_scrip=1, underlying_seg="IDX_I")


def test_transport_network_exhaustion_raises_network() -> None:
    transport = FakeDhanTransport(script=[Raise(network_error())] * 5)
    client = _client(transport, max_attempts=2)
    with pytest.raises(DhanNetworkError):
        client.expiry_list(underlying_scrip=1, underlying_seg="IDX_I")


def test_empty_body_exhaustion_raises_empty_response() -> None:
    transport = FakeDhanTransport(script=[Raise(empty_body_error())] * 5)
    client = _client(transport, max_attempts=2)
    with pytest.raises(DhanEmptyResponseError):
        client.expiry_list(underlying_scrip=1, underlying_seg="IDX_I")


def test_rate_limit_exhaustion_raises_rate_limit() -> None:
    transport = FakeDhanTransport(script=[Response(429, {})] * 5)
    client = _client(transport, max_attempts=2)
    with pytest.raises(DhanRateLimitError):
        client.expiry_list(underlying_scrip=1, underlying_seg="IDX_I")


# ── HTTP-200 body-level failure envelope (hostile-review round 1, MAJOR) ──
# Dhan can deliver its DH-9xx / {status:"failure"} failure envelope on an
# HTTP 200 — the errors module's documented + unit-pinned SECOND failure
# mechanism. The client must classify the BODY on a 2xx too, so a
# 200-delivered dead token drives the reauth/terminal-auth path instead of
# masquerading as success and dying later as a misleading parse error.


def test_http200_dh901_without_refresher_is_terminal_auth() -> None:
    """A dead-token DH-901 delivered on HTTP 200 classifies — never a fake success."""
    transport = FakeDhanTransport(
        script=[Response(200, {"errorCode": "DH-901", "errorMessage": "invalid token"})]
    )
    fired: list[str] = []
    client = _client(transport, on_success=lambda: fired.append("ok"))
    with pytest.raises(DhanAuthError):
        client.expiry_list(underlying_scrip=1, underlying_seg="IDX_I")
    assert transport.calls == 1
    assert fired == []  # a 200-delivered failure NEVER fires on_success


def test_http200_dh901_with_refresher_rereads_and_retries() -> None:
    """A 200-delivered DH-901 drives the SAME reauth self-heal as a 401-delivered one."""
    refresher = DhanTokenRefresher(initial_token="tok0", read_token=lambda: "tok1")
    transport = FakeDhanTransport(script=[Response(200, {"errorCode": "DH-901"}), _ok()])
    client = _client(transport, refresher=refresher)
    got = client.expiry_list(underlying_scrip=1, underlying_seg="IDX_I")
    assert got == {"ok": True}
    # the SECOND call carried the freshly-adopted token — the reauth path fired
    assert transport.seen_headers[1][DHAN_HEADER_ACCESS_TOKEN] == "tok1"


def test_http200_dh905_is_typed_bad_request() -> None:
    """A 200-delivered DH-905 raises the TYPED bad-request, not a downstream parse error."""
    transport = FakeDhanTransport(script=[Response(200, {"errorCode": "DH-905"})])
    with pytest.raises(DhanBadRequestError):
        _client(transport).expiry_list(underlying_scrip=1, underlying_seg="IDX_I")


def test_http200_sdk_failure_envelope_classified() -> None:
    """The SDK {status:"failure", remarks:{...}} envelope on HTTP 200 classifies."""
    body: dict[str, object] = {
        "status": "failure",
        "remarks": {"error_code": "dh-901", "error_message": "bad token"},
    }
    transport = FakeDhanTransport(script=[Response(200, body)])
    with pytest.raises(DhanAuthError) as excinfo:
        _client(transport).expiry_list(underlying_scrip=1, underlying_seg="IDX_I")
    assert "bad token" in str(excinfo.value)


def test_http200_failure_status_without_code_is_terminal_base() -> None:
    """status:"failure" with NO usable DH code still classifies (terminal base)."""
    transport = FakeDhanTransport(script=[Response(200, {"status": "failure", "remarks": {}})])
    client = _client(transport)
    with pytest.raises(DhanError) as excinfo:
        client.expiry_list(underlying_scrip=1, underlying_seg="IDX_I")
    assert type(excinfo.value) is DhanError  # HTTP 200 is unmapped → the terminal base
    assert transport.calls == 1


def test_http200_clean_success_bodies_never_misclassified() -> None:
    """Clean success shapes (candle columns / expiry success envelope) stay success.

    The 2xx-body failure gate keys on POSITIVE failure markers only — never the
    absence of success keys — so a marker-free payload can never be misclassified.
    """
    candle: dict[str, object] = {
        "open": [1.0],
        "high": [1.0],
        "low": [1.0],
        "close": [1.0],
        "volume": [1],
        "timestamp": [1_700_000_000],
    }
    expiry: dict[str, object] = {"data": ["2026-08-27"], "status": "success"}
    fired: list[str] = []
    transport = FakeDhanTransport(script=[Response(200, candle), Response(200, expiry)])
    client = _client(transport, on_success=lambda: fired.append("ok"))
    got_candle = client.intraday_candles(
        security_id="13",
        exchange_segment="IDX_I",
        instrument="INDEX",
        interval="1",
        from_date="2024-01-01 09:15:00",
        to_date="2024-03-01 15:30:00",
    )
    assert got_candle == candle
    assert client.expiry_list(underlying_scrip=1, underlying_seg="IDX_I") == expiry
    assert fired == ["ok", "ok"]  # both clean 200s fired on_success


# ── option-chain shared + special pace composition (round 1, concurrency) ──


def test_option_chain_acquires_shared_and_special_pace_each_attempt() -> None:
    """option_chain consumes a SHARED Data-API token AND the special interval, per attempt.

    The option chain is a Data-API request: paced ONLY by the 3s special pacer it
    would issue OUTSIDE the shared 5/s bucket (5/s candles + 0.33/s chain = 5.33/s
    aggregate — a by-design vendor-cap breach at a future live wiring). The shared
    token is acquired FIRST; the special interval LAST (closest to the physical
    issue, so a blocking shared acquire can never erode the 3 s inter-issue law).
    """
    paces: list[str] = []
    transport = FakeDhanTransport(script=[Response(429, {}), _ok()])
    client = _client(
        transport,
        pace=lambda: paces.append("data"),
        option_chain_pace=lambda: paces.append("chain"),
    )
    client.option_chain(underlying_scrip=1, underlying_seg="IDX_I", expiry="2026-08-27")
    assert paces == ["data", "chain", "data", "chain"]  # BOTH hooks, EVERY physical attempt


# ── reauth self-heal ───────────────────────────────────────────────────────


def test_auth_error_with_refresher_rereads_and_retries() -> None:
    refresher = DhanTokenRefresher(initial_token="tok0", read_token=lambda: "tok1")
    transport = FakeDhanTransport(script=[Response(401, {"errorCode": "DH-901"}), _ok()])
    client = _client(transport, refresher=refresher)
    got = client.expiry_list(underlying_scrip=1, underlying_seg="IDX_I")
    assert got == {"ok": True}
    # the SECOND call carried the freshly-adopted token
    assert transport.seen_headers[1][DHAN_HEADER_ACCESS_TOKEN] == "tok1"


def test_auth_error_reread_still_stale_propagates_retryable() -> None:
    refresher = DhanTokenRefresher(initial_token="tok0", read_token=lambda: "tok0")
    transport = FakeDhanTransport(script=[Response(401, {"errorCode": "DH-901"})])
    client = _client(transport, refresher=refresher)
    with pytest.raises(DhanTokenStillStaleError):
        client.expiry_list(underlying_scrip=1, underlying_seg="IDX_I")
    assert transport.calls == 1  # the stale re-read propagates OUT — no extra call


def test_auth_error_without_refresher_is_terminal() -> None:
    transport = FakeDhanTransport(script=[Response(401, {"errorCode": "DH-901"})])
    client = _client(transport)
    with pytest.raises(DhanAuthError):
        client.expiry_list(underlying_scrip=1, underlying_seg="IDX_I")
    assert transport.calls == 1


def test_auth_error_reauth_budget_exhausted_is_terminal() -> None:
    tokens = iter(["tok1", "tok2", "tok3"])
    refresher = DhanTokenRefresher(initial_token="tok0", read_token=lambda: next(tokens))
    transport = FakeDhanTransport(
        script=[Response(401, {"errorCode": "DH-901"})] * 5, default=_ok()
    )
    client = _client(transport, refresher=refresher, max_reauths=1)
    with pytest.raises(DhanAuthError):
        client.expiry_list(underlying_scrip=1, underlying_seg="IDX_I")
    assert transport.calls == 2  # initial + one post-reauth retry, then terminal


# ── meter + properties + builders ──────────────────────────────────────────


def test_meter_counts_success_and_failure() -> None:
    transport = FakeDhanTransport(script=[Response(429, {}), _ok()])
    client = _client(transport)
    client.expiry_list(underlying_scrip=1, underlying_seg="IDX_I")
    snap = client.meter.snapshot()
    assert snap["issued"] == 2
    assert snap["completed"] == 1
    assert snap["failed"] == 1


def test_self_paces_and_self_reports_properties() -> None:
    client = _client(FakeDhanTransport())
    assert not client.self_paces
    assert not client.self_reports_rate_limits
    bound = client.with_pace(lambda: None)
    assert bound.self_paces
    hooked = client.with_rate_hooks(
        pace=lambda: None, on_rate_limit=lambda: None, on_success=lambda: None
    )
    assert hooked.self_paces
    assert hooked.self_reports_rate_limits
    # option_chain_pace preserved when not overridden
    assert hooked.option_chain_pace is client.option_chain_pace


def test_with_rate_hooks_overrides_option_chain_pace() -> None:
    chain_paces: list[int] = []
    client = _client(FakeDhanTransport(script=[_ok()]))
    hooked = client.with_rate_hooks(
        pace=lambda: None,
        on_rate_limit=lambda: None,
        on_success=lambda: None,
        option_chain_pace=lambda: chain_paces.append(1),
    )
    hooked.option_chain(underlying_scrip=1, underlying_seg="IDX_I", expiry="2026-08-27")
    assert chain_paces == [1]


def test_client_satisfies_protocol() -> None:
    client = _client(FakeDhanTransport())
    assert isinstance(client, DhanClientProtocol)


def test_client_repr_never_exposes_the_token() -> None:
    """``repr(DhanClient)`` must elide the access-token JWT (round-1 security NIT)."""
    client = DhanClient(
        transport=FakeDhanTransport(), access_token="jwt-secret-value", client_id="1100"
    )
    assert "jwt-secret-value" not in repr(client)
