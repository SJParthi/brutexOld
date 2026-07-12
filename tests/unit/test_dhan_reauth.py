"""Tests for the Dhan token manager (DEC-DHAN-AUTH-001) — the reauth spine.

Covers the full ladder (reread → renew → mint), store-first-then-adopt, single-flight
generation, freeze-guard timeout mapping, the lockdown latch (zero HTTP after, one alert),
and ensure_token — all with fakes and a tmp advisory-lock path (no real home writes).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from brutex.adapters.dhan import reauth as reauth_mod
from brutex.adapters.dhan.auth_audit import DhanAuthAudit
from brutex.adapters.dhan.constants import DHAN_MINT_LOCKDOWN_THRESHOLD, DHAN_TOKEN_REFRESH_AGE_S
from brutex.adapters.dhan.errors import (
    DhanAuthError,
    DhanAuthLockdown,
    DhanAuthTransportError,
    DhanTokenMintThrottled,
)
from brutex.adapters.dhan.reauth import AuthAlerter, DhanTokenManager
from brutex.adapters.dhan.token_store_aws import (
    DhanParameterStoreTokenStore,
    DhanTokenEnvelope,
    encode_envelope,
)
from brutex.config.credentials_types import DhanCredentials

from tests.fakes.fake_aws_client import FakeAwsClient
from tests.fakes.fake_dhan_auth_http import FakeDhanAuthHttp, FakeHttpError

_PARAM = "/tickvault/prod/dhan/access-token"
_ANCHOR = datetime(2026, 7, 12, 6, 0, 0, tzinfo=UTC)
_CREDS = DhanCredentials(client_id="FAKE-CID", pin="FAKE-PIN", totp_secret="FAKE-TOTP")


class _Clock:
    def __init__(self, t: datetime) -> None:
        self._t = t

    def set(self, t: datetime) -> None:
        self._t = t

    def __call__(self) -> datetime:
        return self._t


class _FakeAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, object]]] = []

    def record(self, action: str, *, subject: str, **payload: object) -> None:
        self.records.append((action, {"subject": subject, **payload}))

    def actions(self) -> list[str]:
        return [a for a, _ in self.records]


class _FakeAlerter:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def token_auth_failed(self, *, vendor: str, reason: str, detail: str) -> bool:
        self.calls.append({"vendor": vendor, "reason": reason, "detail": detail})
        return True


def _env(token: str, minted: datetime, source: str = "mint") -> DhanTokenEnvelope:
    return DhanTokenEnvelope(token=token, minted_at_utc=minted, source=source)  # type: ignore[arg-type]


def _totp(_secret: str) -> str:
    return "FAKE-CODE"


def _make(
    tmp_path: Path,
    *,
    transport: FakeDhanAuthHttp,
    seed: dict[str, str] | None = None,
    clock: _Clock | None = None,
    alerter: AuthAlerter | None = None,
    audit: _FakeAudit | None = None,
    max_reauths: int = 1,
) -> tuple[DhanTokenManager, FakeAwsClient]:
    client = FakeAwsClient(parameters=dict(seed or {}))
    store = DhanParameterStoreTokenStore(client=client, param_path=_PARAM)
    manager = DhanTokenManager(
        store,
        lambda: _CREDS,
        transport=transport,
        alerter=alerter,
        audit=cast("DhanAuthAudit | None", audit),
        clock=clock or _Clock(_ANCHOR),
        max_reauths=max_reauths,
        lock_path=tmp_path / "dhan-auth.lock",
        # mint_access_token needs a TOTP + a no-op sleeper; those default to the real ones,
        # but the transport never reaches pyotp because we script it. Patch via the module's
        # default only when a mint is scripted — the fake transport short-circuits it.
    )
    return manager, client


@pytest.fixture(autouse=True)
def _inject_totp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject a fake TOTP + no-op sleeper into the manager's mint (avoids pyotp / real sleeps).

    The manager calls ``mint_access_token`` with only ``transport``/``clock``, leaving the
    TOTP + sleeper at their real (pyotp / ``time.sleep``) defaults, which a default-arg bind
    makes unpatchable via the module attribute. Replace the reauth-module reference with a
    wrapper that forces the fakes.
    """
    from brutex.adapters.dhan import auth as auth_mod

    real_mint = auth_mod.mint_access_token

    def _mint(creds, *, transport=None, clock=None):  # type: ignore[no-untyped-def]
        return real_mint(
            creds, transport=transport, totp_minter=_totp, clock=clock, sleeper=lambda _s: None
        )

    monkeypatch.setattr(reauth_mod, "mint_access_token", _mint)


class TestColdStart:
    def test_adopts_cached_usable_token(self, tmp_path: Path) -> None:
        seed = {_PARAM: encode_envelope(_env("FAKE-A", _ANCHOR))}
        http = FakeDhanAuthHttp([])  # no mint should happen
        mgr, _ = _make(tmp_path, transport=http, seed=seed)
        assert mgr.current_token() == "FAKE-A"
        assert http.call_count == 0
        assert mgr.current_generation() == 1

    def test_mints_when_absent(self, tmp_path: Path) -> None:
        http = FakeDhanAuthHttp([{"accessToken": "FAKE-MINTED"}])
        mgr, client = _make(tmp_path, transport=http)
        assert mgr.current_token() == "FAKE-MINTED"
        assert http.call_count == 1
        # The freshly-minted token was stored back to SSM.
        assert client.put_parameter_calls == 1

    def test_invalid_cred_latches_lockdown(self, tmp_path: Path) -> None:
        http = FakeDhanAuthHttp([FakeHttpError(401)])
        alerter, audit = _FakeAlerter(), _FakeAudit()
        with pytest.raises(DhanAuthLockdown):
            _make(tmp_path, transport=http, alerter=alerter, audit=audit)
        assert len(alerter.calls) == 1
        assert alerter.calls[0]["vendor"] == "dhan"
        assert alerter.calls[0]["reason"] == "invalid_credentials"
        assert "DHAN_AUTH_LOCKDOWN" in audit.actions()

    def test_retryable_exhaust_raises(self, tmp_path: Path) -> None:
        http = FakeDhanAuthHttp([FakeHttpError(429), FakeHttpError(429), FakeHttpError(429)])
        audit = _FakeAudit()
        with pytest.raises(DhanTokenMintThrottled):
            _make(tmp_path, transport=http, audit=audit)
        assert "DHAN_AUTH_EXHAUSTED" in audit.actions()

    def test_store_failure_still_adopts(self, tmp_path: Path) -> None:
        http = FakeDhanAuthHttp([{"accessToken": "FAKE-MINTED"}])
        client = FakeAwsClient(fail_put_parameter=True)
        store = DhanParameterStoreTokenStore(client=client, param_path=_PARAM)
        audit = _FakeAudit()
        mgr = DhanTokenManager(
            store,
            lambda: _CREDS,
            transport=http,
            audit=cast("DhanAuthAudit | None", audit),
            lock_path=tmp_path / "l",
        )
        assert mgr.current_token() == "FAKE-MINTED"  # adopted despite store failure
        store_records = [p for a, p in audit.records if a == "DHAN_TOKEN_STORE"]
        assert store_records and store_records[0]["outcome"] == "failed"


class TestReauthLadder:
    def test_single_flight_noop(self, tmp_path: Path) -> None:
        http = FakeDhanAuthHttp([{"accessToken": "FAKE-A"}])
        mgr, _ = _make(tmp_path, transport=http)
        # generation is 1; a stale observed generation is a no-op handoff.
        mgr.reauth(0)
        assert http.call_count == 1  # no second mint
        assert mgr.current_generation() == 1

    def test_reread_brake_adopts_sibling_token(self, tmp_path: Path) -> None:
        http = FakeDhanAuthHttp([{"accessToken": "FAKE-A"}])
        mgr, client = _make(tmp_path, transport=http)
        # A sibling process minted FAKE-B into SSM: reauth adopts it with ZERO vendor calls.
        client.parameters[_PARAM] = encode_envelope(_env("FAKE-B", _ANCHOR))
        mgr.reauth(mgr.current_generation())
        assert mgr.current_token() == "FAKE-B"
        assert http.call_count == 1  # no renew/mint
        assert mgr.current_generation() == 2

    def test_renew_success(self, tmp_path: Path) -> None:
        http = FakeDhanAuthHttp([{"accessToken": "FAKE-A"}, {"accessToken": "FAKE-RENEWED"}])
        mgr, _ = _make(tmp_path, transport=http)
        # SSM still holds FAKE-A (same as current) → reread skips → renew runs.
        mgr.reauth(mgr.current_generation())
        assert mgr.current_token() == "FAKE-RENEWED"
        assert http.requests[-1].method == "GET"  # renew is a GET

    def test_renew_fail_falls_to_mint(self, tmp_path: Path) -> None:
        http = FakeDhanAuthHttp(
            [{"accessToken": "FAKE-A"}, FakeHttpError(500), {"accessToken": "FAKE-MINTED2"}]
        )
        mgr, _ = _make(tmp_path, transport=http)
        mgr.reauth(mgr.current_generation())
        assert mgr.current_token() == "FAKE-MINTED2"
        assert http.call_count == 3

    def test_renew_disabled_mints(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_DHAN_RENEW", "off")
        http = FakeDhanAuthHttp([{"accessToken": "FAKE-A"}, {"accessToken": "FAKE-MINTED3"}])
        mgr, _ = _make(tmp_path, transport=http)
        mgr.reauth(mgr.current_generation())
        assert mgr.current_token() == "FAKE-MINTED3"
        assert http.requests[-1].method == "POST"  # a mint, not a renew

    def test_renew_timeout_falls_to_mint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        http = FakeDhanAuthHttp([{"accessToken": "FAKE-A"}, {"accessToken": "FAKE-MINTED4"}])
        mgr, _ = _make(tmp_path, transport=http)
        from brutex.config.deadline import run_with_deadline as real

        def _fake(fn, secs, *, what="", thread_name=""):  # type: ignore[no-untyped-def]
            if what == "Dhan renew":
                raise TimeoutError("hung renew")
            return real(fn, secs, what=what, thread_name=thread_name)

        monkeypatch.setattr("brutex.adapters.dhan.reauth.run_with_deadline", _fake)
        mgr.reauth(mgr.current_generation())
        # renew timed out → fell through to a real mint (the second scripted response).
        assert mgr.current_token() == "FAKE-MINTED4"

    def test_mint_timeout_maps_to_transport_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BRUTEX_DHAN_RENEW", "off")
        http = FakeDhanAuthHttp([{"accessToken": "FAKE-A"}])
        mgr, _ = _make(tmp_path, transport=http)
        from brutex.config.deadline import run_with_deadline as real

        def _fake(fn, secs, *, what="", thread_name=""):  # type: ignore[no-untyped-def]
            if what == "Dhan mint":
                raise TimeoutError("hung mint")
            return real(fn, secs, what=what, thread_name=thread_name)

        monkeypatch.setattr("brutex.adapters.dhan.reauth.run_with_deadline", _fake)
        with pytest.raises(DhanAuthTransportError):
            mgr.reauth(mgr.current_generation())


class TestFreezeGuardSsm:
    """The SSM read + write run under the freeze guard (a hung boto3 call never holds the lock)."""

    def test_load_timeout_falls_through_to_mint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # SSM holds a usable sibling token, but the READ hangs → freeze guard → fall to mint.
        seed = {_PARAM: encode_envelope(_env("FAKE-SIBLING", _ANCHOR))}
        http = FakeDhanAuthHttp([{"accessToken": "FAKE-MINTED"}])
        from brutex.config.deadline import run_with_deadline as real

        def _fake(fn, secs, *, what="", thread_name=""):  # type: ignore[no-untyped-def]
            if what == "Dhan SSM read":
                raise TimeoutError("hung SSM read")
            return real(fn, secs, what=what, thread_name=thread_name)

        monkeypatch.setattr("brutex.adapters.dhan.reauth.run_with_deadline", _fake)
        mgr, _ = _make(tmp_path, transport=http, seed=seed)
        # The hung read degraded to None (never adopted the sibling) → a fresh mint ran.
        assert mgr.current_token() == "FAKE-MINTED"
        assert http.call_count == 1

    def test_store_timeout_still_adopts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        http = FakeDhanAuthHttp([{"accessToken": "FAKE-MINTED"}])
        audit = _FakeAudit()
        from brutex.config.deadline import run_with_deadline as real

        def _fake(fn, secs, *, what="", thread_name=""):  # type: ignore[no-untyped-def]
            if what == "Dhan SSM write":
                raise TimeoutError("hung SSM write")
            return real(fn, secs, what=what, thread_name=thread_name)

        monkeypatch.setattr("brutex.adapters.dhan.reauth.run_with_deadline", _fake)
        mgr, _ = _make(tmp_path, transport=http, audit=audit)
        # A hung store never un-adopts: the minted token is live + audited store-failed.
        assert mgr.current_token() == "FAKE-MINTED"
        store_records = [p for a, p in audit.records if a == "DHAN_TOKEN_STORE"]
        assert store_records and store_records[0]["outcome"] == "failed"
        assert store_records[0]["reason_class"] == "TimeoutError"


class TestLockdown:
    def _lock_down_via_repeated_terminal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[DhanTokenManager, FakeDhanAuthHttp, _FakeAlerter, _FakeAudit]:
        monkeypatch.setenv("BRUTEX_DHAN_RENEW", "off")
        # cold-mint success, then N no-token 2xx responses (each a terminal DhanAuthError).
        responses: list[object] = [{"accessToken": "FAKE-A"}]
        responses += [{"nope": 1}] * DHAN_MINT_LOCKDOWN_THRESHOLD
        http = FakeDhanAuthHttp(responses)  # type: ignore[arg-type]
        alerter, audit = _FakeAlerter(), _FakeAudit()
        mgr, _ = _make(tmp_path, transport=http, alerter=alerter, audit=audit)
        # The first THRESHOLD-1 reauths raise a plain terminal DhanAuthError; the last latches.
        for _ in range(DHAN_MINT_LOCKDOWN_THRESHOLD - 1):
            with pytest.raises(DhanAuthError):
                mgr.reauth(mgr.current_generation())
        with pytest.raises(DhanAuthLockdown):
            mgr.reauth(mgr.current_generation())
        return mgr, http, alerter, audit

    def test_repeated_terminal_latches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mgr, http, alerter, _ = self._lock_down_via_repeated_terminal(tmp_path, monkeypatch)
        assert len(alerter.calls) == 1
        assert alerter.calls[0]["reason"] == "repeated_terminal"
        # A further reauth fails fast with ZERO new HTTP.
        calls_before = http.call_count
        with pytest.raises(DhanAuthLockdown):
            mgr.reauth(mgr.current_generation())
        assert http.call_count == calls_before

    def test_current_token_and_ensure_raise_after_lockdown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mgr, _http, _al, _au = self._lock_down_via_repeated_terminal(tmp_path, monkeypatch)
        with pytest.raises(DhanAuthLockdown):
            mgr.current_token()
        with pytest.raises(DhanAuthLockdown):
            mgr.ensure_token()


class TestEnsureToken:
    def test_fresh_short_circuits(self, tmp_path: Path) -> None:
        http = FakeDhanAuthHttp([{"accessToken": "FAKE-A"}])
        mgr, _ = _make(tmp_path, transport=http)
        assert mgr.ensure_token() == "FAKE-A"
        assert http.call_count == 1  # no reauth

    def test_stale_triggers_reauth(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_DHAN_RENEW", "off")
        clock = _Clock(_ANCHOR)
        http = FakeDhanAuthHttp([{"accessToken": "FAKE-A"}, {"accessToken": "FAKE-FRESH"}])
        mgr, _ = _make(tmp_path, transport=http, clock=clock)
        clock.set(_ANCHOR + timedelta(seconds=DHAN_TOKEN_REFRESH_AGE_S + 100))
        assert mgr.ensure_token() == "FAKE-FRESH"

    def test_exhausted_returns_current_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        http = FakeDhanAuthHttp([{"accessToken": "FAKE-A"}])
        mgr, _ = _make(tmp_path, transport=http)
        # Force a stale current + a no-op reauth so the budget exhausts with a token present.
        mgr._current = _env("FAKE-STALE", _ANCHOR - timedelta(days=2))
        monkeypatch.setattr(DhanTokenManager, "reauth", lambda _self, _gen: None)
        assert mgr.ensure_token() == "FAKE-STALE"

    def test_exhausted_raises_when_no_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        http = FakeDhanAuthHttp([{"accessToken": "FAKE-A"}])
        mgr, _ = _make(tmp_path, transport=http)
        mgr._current = None
        monkeypatch.setattr(DhanTokenManager, "reauth", lambda _self, _gen: None)
        with pytest.raises(DhanAuthError):
            mgr.ensure_token()


class TestMisc:
    def test_current_token_none_raises(self, tmp_path: Path) -> None:
        http = FakeDhanAuthHttp([{"accessToken": "FAKE-A"}])
        mgr, _ = _make(tmp_path, transport=http)
        mgr._current = None
        with pytest.raises(DhanAuthError):
            mgr.current_token()

    def test_latch_without_alerter(self, tmp_path: Path) -> None:
        # No alerter wired — the latch must still work (no crash on the None-alerter branch).
        http = FakeDhanAuthHttp([FakeHttpError(403)])
        with pytest.raises(DhanAuthLockdown):
            _make(tmp_path, transport=http, alerter=None, audit=None)

    def test_alerter_protocol_stub_raises(self) -> None:
        with pytest.raises(NotImplementedError):
            AuthAlerter.token_auth_failed(object(), vendor="x", reason="y", detail="z")  # type: ignore[arg-type]

    def test_now_utc_default_tz_aware(self) -> None:
        assert reauth_mod._now_utc().tzinfo is UTC

    def test_default_lock_path(self) -> None:
        assert reauth_mod._default_lock_path().name == "dhan-auth.lock"
