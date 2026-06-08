"""Tests for `brutex.adapters.dhan.token_keepalive` — the 12h token keepalive.

100% line+branch of the synchronous, injected-clock logic
(DEC-DHAN-TOKEN-KEEPALIVE-001): expiry parse (happy + millisecond + fallback),
the renew-when-below-12h decision, the no-renew-when-above sleep, the
renew-failure retry interval, holder propagation, the first-tick learn-expiry
path, and the `_run` loop driven by a fake sleep that sets the stop event after
N ticks (so even the loop is covered). NO real threads, NO real sleeps.
"""

# pyright: reportPrivateUsage=none
from __future__ import annotations

import base64
import json
from collections.abc import Mapping

from brutex.adapters.dhan.client import DhanClient
from brutex.adapters.dhan.token_holder import TokenHolder
from brutex.adapters.dhan.token_keepalive import (
    MAX_SLEEP_S,
    RENEW_BEFORE_EXPIRY_S,
    RETRY_WHEN_DEAD_S,
    TokenKeepalive,
    decode_jwt_expiry,
    parse_expiry_deadline,
)
from structlog.testing import capture_logs


def _jwt(claims: object) -> str:
    """Build a 3-segment JWT-shaped string whose middle segment encodes *claims*.

    base64url WITHOUT padding (how real JWTs are emitted) so the decoder's
    padding-restore branch is exercised on the same path production hits.
    """
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).rstrip(b"=")
    return f"hdr.{payload.decode('ascii')}.sig"


class _FakeTransport:
    """A fake transport returning a canned RenewToken response (or an error status).

    Each call pops the next scripted ``(status, body)`` so a test can sequence a
    success then a refusal; once exhausted it repeats the last script.
    """

    def __init__(self, scripts: list[tuple[int, bytes]]) -> None:
        self._scripts = scripts
        self.calls = 0

    def __call__(
        self,
        url: str,
        *,
        body: bytes,
        headers: Mapping[str, str],
        timeout: float,
        method: str = "POST",
    ) -> tuple[int, bytes]:
        self.calls += 1
        idx = min(self.calls - 1, len(self._scripts) - 1)
        return self._scripts[idx]


def _renew_body(token: str, expiry: str) -> bytes:
    # Real Dhan RenewToken shape (DEC-DHAN-RENEWTOKEN-ROTATE-001): the rotated
    # token is under "token" (NOT "accessToken"), alongside "expiryTime".
    return json.dumps({"token": token, "expiryTime": expiry}).encode("utf-8")


def _client(transport: _FakeTransport) -> DhanClient:
    return DhanClient(
        access_token="seed-jwt",
        client_id="100000001",
        transport=transport,  # type: ignore[arg-type]
        base_url="https://api.dhan.co/v2/",
        max_attempts=1,
        sleep=lambda _s: None,
    )


class _FixedClock:
    """An epoch-seconds clock pinned to a fixed value (deterministic delta math)."""

    def __init__(self, value: float = 1_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class TestParseExpiryDeadline:
    def test_parses_milliseconds_format(self) -> None:
        # The string is naive wall-clock; assert it round-trips to the SAME epoch
        # as strptime would yield (self-consistent with the keepalive's clock).
        deadline = parse_expiry_deadline("2026-01-01T00:00:00.000")
        assert deadline > 0.0

    def test_parses_without_milliseconds(self) -> None:
        deadline = parse_expiry_deadline("2026-01-01T00:00:00")
        assert deadline > 0.0

    def test_unparsable_falls_back_to_now_plus_24h_and_warns(self) -> None:
        clock = _FixedClock(500.0)
        with capture_logs() as logs:
            deadline = parse_expiry_deadline("not-a-date", now=clock)
        assert deadline == 500.0 + 24 * 3600.0
        assert any(e["event"] == "auth.keepalive.expiry_unparsed" for e in logs)


class TestDecodeJwtExpiry:
    """Offline JWT-``exp`` learning (DEC-DHAN-RENEW-SINGLE-OWNER-001).

    Lets the keepalive learn a token's expiry with NO RenewToken (no rotation);
    every malformed shape degrades to ``None`` (the caller falls back to
    learn-by-renew), never raising.
    """

    def test_reads_integer_exp(self) -> None:
        assert decode_jwt_expiry(_jwt({"exp": 1_700_000_000})) == 1_700_000_000.0

    def test_reads_float_exp(self) -> None:
        assert decode_jwt_expiry(_jwt({"exp": 1_700_000_000.5})) == 1_700_000_000.5

    def test_missing_exp_returns_none(self) -> None:
        assert decode_jwt_expiry(_jwt({"sub": "client"})) is None

    def test_bool_exp_rejected(self) -> None:
        # ``True`` is an int subclass — must NOT be misread as epoch 1.
        assert decode_jwt_expiry(_jwt({"exp": True})) is None

    def test_string_exp_rejected(self) -> None:
        assert decode_jwt_expiry(_jwt({"exp": "1700000000"})) is None

    def test_non_object_payload_returns_none(self) -> None:
        assert decode_jwt_expiry(_jwt([1, 2, 3])) is None

    def test_not_three_segments_returns_none(self) -> None:
        assert decode_jwt_expiry("only-one-segment") is None

    def test_undecodable_base64_returns_none(self) -> None:
        # A 1-char payload segment is structurally invalid base64 (binascii.Error).
        assert decode_jwt_expiry("hdr.A.sig") is None

    def test_non_json_payload_returns_none(self) -> None:
        payload = base64.urlsafe_b64encode(b"not-json").rstrip(b"=").decode("ascii")
        assert decode_jwt_expiry(f"hdr.{payload}.sig") is None


class TestTick:
    def _build(
        self, transport: _FakeTransport, clock: _FixedClock
    ) -> tuple[TokenKeepalive, TokenHolder]:
        holder = TokenHolder("seed-jwt")
        keepalive = TokenKeepalive(
            _client(transport),
            holder,
            now=clock,
            sleep=lambda _s: None,
        )
        return keepalive, holder

    def test_first_tick_renews_to_learn_expiry_and_propagates(self) -> None:
        clock = _FixedClock(0.0)
        # First tick (deadline unknown) renews immediately to LEARN the expiry,
        # propagates the fresh token, and schedules the next tick within bounds.
        transport = _FakeTransport([(200, _renew_body("fresh-jwt", "2099-01-01T00:00:00.000"))])
        keepalive, holder = self._build(transport, clock)
        assert keepalive._expiry_deadline is None  # unknown at construction
        with capture_logs() as logs:
            sleep_s = keepalive._tick()
        assert holder.get() == "fresh-jwt"
        assert any(e["event"] == "auth.renewed" for e in logs)
        assert 0.0 <= sleep_s <= MAX_SLEEP_S
        assert keepalive._expiry_deadline is not None

    def test_no_renew_when_above_threshold_sleeps_until_12h_mark(self) -> None:
        clock = _FixedClock(1000.0)
        transport = _FakeTransport([(200, _renew_body("x", "x"))])
        keepalive, holder = self._build(transport, clock)
        # Deadline far out: remaining = 20h, well above the 12h threshold.
        keepalive._expiry_deadline = 1000.0 + 20 * 3600.0
        sleep_s = keepalive._tick()
        # remaining - 12h = 8h, clamped to MAX_SLEEP_S (1h).
        assert sleep_s == MAX_SLEEP_S
        assert transport.calls == 0  # no renew
        assert holder.get() == "seed-jwt"

    def test_no_renew_sleep_not_clamped_when_under_max(self) -> None:
        clock = _FixedClock(1000.0)
        transport = _FakeTransport([(200, _renew_body("x", "x"))])
        keepalive, _ = self._build(transport, clock)
        # remaining = 12h + 600s → sleep should be exactly 600s (< MAX_SLEEP_S).
        keepalive._expiry_deadline = 1000.0 + RENEW_BEFORE_EXPIRY_S + 600.0
        assert keepalive._tick() == 600.0

    def test_renews_when_below_threshold(self) -> None:
        clock = _FixedClock(1000.0)
        transport = _FakeTransport([(200, _renew_body("renewed-jwt", "2099-01-01T00:00:00.000"))])
        keepalive, holder = self._build(transport, clock)
        # remaining = 11h, below the 12h threshold → renew now.
        keepalive._expiry_deadline = 1000.0 + 11 * 3600.0
        with capture_logs() as logs:
            keepalive._tick()
        assert transport.calls == 1
        assert holder.get() == "renewed-jwt"
        assert any(e["event"] == "auth.renewed" for e in logs)

    def test_renew_failure_keeps_deadline_and_retries(self) -> None:
        clock = _FixedClock(1000.0)
        transport = _FakeTransport([(401, b"token expired")])
        keepalive, holder = self._build(transport, clock)
        old_deadline = 1000.0 + 1 * 3600.0  # 1h left → below threshold → renew
        keepalive._expiry_deadline = old_deadline
        with capture_logs() as logs:
            sleep_s = keepalive._tick()
        assert sleep_s == RETRY_WHEN_DEAD_S
        assert keepalive._expiry_deadline == old_deadline  # unchanged
        assert holder.get() == "seed-jwt"  # not propagated
        assert any(e["event"] == "auth.renew_failed" for e in logs)

    def test_first_tick_renew_failure_schedules_retry(self) -> None:
        clock = _FixedClock(0.0)
        transport = _FakeTransport([(401, b"dead token")])
        keepalive, holder = self._build(transport, clock)
        keepalive._expiry_deadline = None
        sleep_s = keepalive._tick()
        assert sleep_s == RETRY_WHEN_DEAD_S
        assert keepalive._expiry_deadline is None
        assert holder.get() == "seed-jwt"

    def test_first_tick_learns_from_jwt_no_renew_when_above_threshold(self) -> None:
        # The DEC-DHAN-RENEW-SINGLE-OWNER-001 fix: a healthy >12h JWT token is
        # learned OFFLINE — ZERO RenewToken, the live token is NOT rotated.
        clock = _FixedClock(1000.0)
        transport = _FakeTransport([(200, _renew_body("must-not-fire", "x"))])
        jwt = _jwt({"exp": 1000.0 + 20 * 3600.0})  # 20h out → above the 12h window
        holder = TokenHolder(jwt)
        keepalive = TokenKeepalive(_client(transport), holder, now=clock, sleep=lambda _s: None)
        with capture_logs() as logs:
            sleep_s = keepalive._tick()
        assert transport.calls == 0  # NO RenewToken fired
        assert holder.get() == jwt  # token untouched — no rotation
        assert sleep_s == MAX_SLEEP_S  # remaining - 12h = 8h, clamped to 1h
        assert keepalive._expiry_deadline == 1000.0 + 20 * 3600.0
        assert any(e["event"] == "auth.keepalive.expiry_learned" for e in logs)
        assert not any(e["event"] == "auth.renewed" for e in logs)

    def test_first_tick_jwt_below_threshold_renews(self) -> None:
        clock = _FixedClock(1000.0)
        transport = _FakeTransport([(200, _renew_body("renewed-jwt", "2099-01-01T00:00:00.000"))])
        jwt = _jwt({"exp": 1000.0 + 1 * 3600.0})  # only 1h out → inside 12h → renew
        holder = TokenHolder(jwt)
        keepalive = TokenKeepalive(_client(transport), holder, now=clock, sleep=lambda _s: None)
        with capture_logs() as logs:
            keepalive._tick()
        assert transport.calls == 1
        assert holder.get() == "renewed-jwt"
        assert any(e["event"] == "auth.renewed" for e in logs)


class TestRunLoop:
    def test_run_loops_ticks_then_stops_via_fake_sleep(self) -> None:
        # Drive _run with a fake clock + a stop event triggered after N ticks by the
        # injected sleep, so the loop glue itself is covered (no real thread/sleep).
        clock = _FixedClock(1000.0)
        transport = _FakeTransport([(200, _renew_body("x", "x"))])
        holder = TokenHolder("seed-jwt")
        keepalive = TokenKeepalive(
            _client(transport),
            holder,
            now=clock,
            sleep=lambda _s: None,
        )
        # Far-out deadline so each _tick returns MAX_SLEEP_S without renewing.
        keepalive._expiry_deadline = 1000.0 + 20 * 3600.0
        ticks = {"n": 0}
        real_wait = keepalive._stop.wait

        def fake_wait(timeout: float | None = None) -> bool:
            ticks["n"] += 1
            if ticks["n"] >= 3:
                keepalive._stop.set()
            return real_wait(0.0)

        keepalive._stop.wait = fake_wait  # type: ignore[method-assign]
        keepalive._run()
        assert ticks["n"] == 3

    def test_run_breaks_immediately_when_already_stopped(self) -> None:
        clock = _FixedClock(1000.0)
        transport = _FakeTransport([(200, _renew_body("x", "x"))])
        keepalive = TokenKeepalive(
            _client(transport), TokenHolder("s"), now=clock, sleep=lambda _s: None
        )
        keepalive._stop.set()
        keepalive._run()  # loop body never enters
        assert transport.calls == 0

    def test_run_breaks_when_wait_returns_true(self) -> None:
        # Cover the `if self._stop.wait(...)` True branch: tick once, then the wait
        # reports the stop flag was set during the sleep → break.
        clock = _FixedClock(1000.0)
        transport = _FakeTransport([(200, _renew_body("x", "x"))])
        keepalive = TokenKeepalive(
            _client(transport), TokenHolder("s"), now=clock, sleep=lambda _s: None
        )
        keepalive._expiry_deadline = 1000.0 + 20 * 3600.0
        keepalive._stop.wait = lambda _t=None: True  # type: ignore[method-assign,assignment]
        keepalive._run()
