"""Tests for :mod:`brutex.adapters.dhan.reauth` — READ-ONLY-reread self-heal, NEVER mints."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest
from brutex.adapters.dhan.errors import (
    DhanAuthError,
    DhanTimeoutError,
    DhanTokenStillStaleError,
)
from brutex.adapters.dhan.reauth import DhanSdkRefresher
from brutex.adapters.dhan.token_store_aws import DhanCachedToken

_NOW = datetime(2026, 7, 12, 4, 0, tzinfo=UTC)


class _FakeSdk:
    """A fake read-path SDK carrying the token it was built from."""

    def __init__(self, token: str) -> None:
        self.token = token

    def intraday_minute_data(self, **_kwargs: object) -> dict[str, object]:
        return {"status": "success", "data": {}}


def _sdk_factory(_client_id: str, access_token: str) -> _FakeSdk:
    return _FakeSdk(access_token)


class _Reread:
    """A scripted token re-read hook (best-effort, never raises)."""

    def __init__(self, *tokens: str | None) -> None:
        self._tokens = list(tokens)
        self.calls = 0

    def __call__(self, *, now_utc: datetime) -> DhanCachedToken | None:
        _ = now_utc
        self.calls += 1
        token = self._tokens.pop(0) if self._tokens else None
        return DhanCachedToken(access_token=token) if token is not None else None


def _build(reread: _Reread, **kwargs: object) -> DhanSdkRefresher:
    return DhanSdkRefresher(
        client_id="C1",
        token_reread=reread,
        sdk_factory=_sdk_factory,
        clock=lambda: _NOW,
        sleep=lambda _s: None,
        **kwargs,  # type: ignore[arg-type]
    )


def test_cold_start_builds_from_first_read() -> None:
    ref = _build(_Reread("A"))
    sdk = ref.current_sdk()
    assert isinstance(sdk, _FakeSdk) and sdk.token == "A"
    assert ref.current_generation() == 0


def test_default_clock_is_used_when_not_injected() -> None:
    # Exercise the real ``_now_utc`` default clock (no ``clock=`` injection).
    ref = DhanSdkRefresher(
        client_id="C1",
        token_reread=_Reread("A"),
        sdk_factory=_sdk_factory,
        sleep=lambda _s: None,
    )
    assert ref.current_sdk().token == "A"  # type: ignore[attr-defined]


def test_cold_start_retries_then_succeeds() -> None:
    reread = _Reread(None, "A")  # miss, then hit
    ref = _build(reread)
    assert ref.current_sdk().token == "A"  # type: ignore[attr-defined]
    assert reread.calls == 2


def test_cold_start_exhaustion_fails_loud_never_mints() -> None:
    reread = _Reread(None, None, None)
    with pytest.raises(DhanAuthError) as excinfo:
        _build(reread, cold_start_attempts=3)
    # The loud, actionable message names the resolved SSM parameter + the fix.
    assert "READ-ONLY" in str(excinfo.value)
    assert "/tickvault/prod/dhan/access-token" in str(excinfo.value)
    assert "never mints" in str(excinfo.value).lower()


def test_reauth_adopts_new_token_and_bumps_generation() -> None:
    ref = _build(_Reread("A", "B"))  # cold-start A, reauth reads B
    gen = ref.reauth(seen_generation=0)
    assert gen == 1
    assert ref.current_sdk().token == "B"  # type: ignore[attr-defined]


def test_reauth_same_dead_token_raises_still_stale() -> None:
    ref = _build(_Reread("A", "A"))  # re-read returns the SAME dead token
    with pytest.raises(DhanTokenStillStaleError):
        ref.reauth(seen_generation=0)
    assert ref.current_generation() == 0  # no bump


def test_reauth_absent_token_raises_still_stale() -> None:
    ref = _build(_Reread("A"))  # cold-start A, reauth read misses (None)
    with pytest.raises(DhanTokenStillStaleError):
        ref.reauth(seen_generation=0)


def test_reauth_single_flight_no_op_when_generation_moved() -> None:
    reread = _Reread("A")
    ref = _build(reread)
    calls_after_cold_start = reread.calls
    # A stale seen_generation means another thread already re-read — no second read.
    assert ref.reauth(seen_generation=99) == 0
    assert reread.calls == calls_after_cold_start


def test_reauth_read_hang_is_retryable_timeout() -> None:
    blocked = threading.Event()

    class _HangSecondRead:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, *, now_utc: datetime) -> DhanCachedToken | None:
            _ = now_utc
            self.calls += 1
            if self.calls == 1:
                return DhanCachedToken(access_token="A")  # cold start succeeds
            blocked.wait(timeout=5.0)  # a hung SSM read on reauth
            return None

    ref = _build(_HangSecondRead(), call_timeout_seconds=0.05)  # type: ignore[arg-type]
    try:
        with pytest.raises(DhanTimeoutError):
            ref.reauth(seen_generation=0)
    finally:
        blocked.set()


def test_cold_start_read_hang_is_retryable_timeout() -> None:
    blocked = threading.Event()

    def _hang(*, now_utc: datetime) -> DhanCachedToken | None:
        _ = now_utc
        blocked.wait(timeout=5.0)
        return None

    try:
        with pytest.raises(DhanTimeoutError):
            DhanSdkRefresher(
                client_id="C1",
                token_reread=_hang,
                sdk_factory=_sdk_factory,
                clock=lambda: _NOW,
                sleep=lambda _s: None,
                call_timeout_seconds=0.05,
            )
    finally:
        blocked.set()
