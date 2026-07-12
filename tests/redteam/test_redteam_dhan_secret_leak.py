"""Red-team battery: a Dhan auth secret MUST NEVER leak through ANY channel.

DEC-DHAN-AUTH-001 / PR-1. Secret safety is paramount: the headless PIN+TOTP mint carries the
secret PIN + TOTP code in the URL query string, and a urllib ``HTTPError.__str__`` embeds
that full secret-bearing URL. This module drives the REAL mint / renew / reauth code
end-to-end through a fake transport that raises exactly such secret-bearing errors and proves
the credential values NEVER reach:

* a raised exception's ``str`` / ``repr`` / ``args`` / ``safe_message`` / ``__cause__``,
* any structlog record (captured via :func:`structlog.testing.capture_logs`),
* the hash-chained audit ledger (a real on-disk chain),
* the token store's logged fields,
* the CRITICAL alerter's ``detail``,
* ANY file the auth stack writes to disk (a filesystem oracle over the audit + lock files).

It also covers the "unexpected response shape" leak class (the PR-2 analog): a token or an
echoed credential buried in an anomalous mint response must never reach a log / audit /
exception. Fixture secrets are obviously fake (``FAKE-`` prefixed, no ``eyJ`` JWT strings) and
distinctive enough that any leak is trivially detected by a substring scan.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
import structlog
from brutex.adapters.dhan import auth as auth_mod
from brutex.adapters.dhan import reauth as reauth_mod
from brutex.adapters.dhan.auth import _build_mint_url, mint_access_token, renew_access_token
from brutex.adapters.dhan.auth_audit import DhanAuthAudit
from brutex.adapters.dhan.errors import (
    DhanAuthError,
    DhanAuthInvalidCredentials,
    DhanAuthLockdown,
    DhanTokenMintThrottled,
    DhanTokenRenewError,
    DhanTokenStoreError,
)
from brutex.adapters.dhan.reauth import AuthAlerter, DhanTokenManager
from brutex.adapters.dhan.token_store_aws import DhanParameterStoreTokenStore, DhanTokenEnvelope
from brutex.adapters.secret_scrub import redact_url_query, scrub_secrets
from brutex.config.credentials_types import DhanCredentials
from brutex.orchestrator.audit import verify_chain
from structlog.testing import capture_logs

from tests.fakes.fake_aws_client import FakeAwsClient
from tests.fakes.fake_dhan_auth_http import FakeDhanAuthHttp, FakeHttpError

# ── Obviously-fake, distinctively-shaped secrets (no ``eyJ`` JWT strings) ─────────────────
_CLIENT_ID = "FAKE-CID-9a1b2c3d"
_PIN = "FAKE-PIN-e4f5a6b7"
_TOTP_SECRET = "FAKE-TOTPSECRET-c8d9e0f1"
_TOTP_CODE = "FAKE-CODE-13572468"
_TOKEN = "FAKE-TOKEN-aabbccddeeff00112233"
_TOKEN2 = "FAKE-TOKEN2-99887766554433221100"

#: Every value that must NEVER surface outside the vault (the SecureString) / the caller's hand.
_SECRETS: tuple[str, ...] = (_CLIENT_ID, _PIN, _TOTP_SECRET, _TOTP_CODE, _TOKEN, _TOKEN2)

_CREDS = DhanCredentials(client_id=_CLIENT_ID, pin=_PIN, totp_secret=_TOTP_SECRET)
_ANCHOR = datetime(2026, 7, 12, 6, 0, 0, tzinfo=UTC)
_PARAM = "/tickvault/prod/dhan/access-token"


def _fake_totp(_secret: str) -> str:
    return _TOTP_CODE


def _clock() -> datetime:
    return _ANCHOR


def _assert_clean(blob: object, label: str, secrets: Iterable[str] = _SECRETS) -> None:
    """Assert *blob* (stringified) contains NONE of *secrets* — names the leaked value + channel."""
    text = blob if isinstance(blob, str) else repr(blob)
    for secret in secrets:
        assert secret not in text, f"SECRET LEAK via {label}: {secret!r} found in {text!r}"


def _assert_exc_clean(exc: BaseException, label: str) -> None:
    """Sweep every channel a caught exception can leak through — incl. the FULL chain.

    Walks the entire ``__cause__`` / ``__context__`` chain: a raw secret-bearing transport
    object retained anywhere on the chain (even a naive ``repr(exc.__context__)``) is a leak.
    """
    seen: set[int] = set()
    node: BaseException | None = exc
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        _assert_clean(str(node), f"{label}:str")
        _assert_clean(repr(node), f"{label}:repr")
        _assert_clean(node.args, f"{label}:args")
        if isinstance(node, DhanAuthError):
            _assert_clean(node.safe_message, f"{label}:safe_message")
        _assert_clean(repr(node.__cause__), f"{label}:__cause__")
        _assert_clean(repr(node.__context__), f"{label}:__context__")
        node = node.__cause__ or node.__context__


def _assert_logs_clean(entries: Sequence[Mapping[str, object]], label: str) -> None:
    for entry in entries:
        _assert_clean(entry, f"{label}:log")


def _secret_bearing_url() -> str:
    """The real mint URL with the secret PIN + TOTP in the query — a urllib HTTPError body."""
    return _build_mint_url(_CREDS, _TOTP_CODE)


# ── 1. The scrub primitives cover the mint-URL sink (teeth + proof) ───────────────────────


class TestScrubPrimitivesCoverEverySink:
    def test_raw_mint_url_actually_contains_the_secrets(self) -> None:
        raw = _secret_bearing_url()
        # Teeth: the raw urllib-style message DOES carry the PIN + TOTP (so scrubbing is real).
        assert _PIN in raw
        assert _TOTP_CODE in raw
        assert _CLIENT_ID in raw

    def test_redact_url_query_strips_the_whole_query(self) -> None:
        raw = _secret_bearing_url()
        redacted = redact_url_query(raw)
        assert redacted.endswith("?[REDACTED]")
        assert "auth.dhan.co" in redacted  # host+path preserved (diagnostic)
        _assert_clean(redacted, "redact_url_query")

    def test_scrub_secrets_masks_a_plaintext_secret_outside_a_url(self) -> None:
        msg = f"connection reset while sending pin={_PIN} totp={_TOTP_CODE}"
        scrubbed = scrub_secrets(msg, (_PIN, _TOTP_CODE, _CLIENT_ID, _TOTP_SECRET))
        _assert_clean(scrubbed, "scrub_secrets")
        assert "***" in scrubbed

    def test_dhan_auth_error_constructor_scrubs_a_secret_bearing_url(self) -> None:
        # The belt: even constructing an error straight from the raw URL is leak-proof.
        exc = DhanAuthError(f"boom: {_secret_bearing_url()}", secrets=_SECRETS)
        _assert_exc_clean(exc, "DhanAuthError(raw-url)")


# ── 2. mint(): every failure branch re-raises SCRUBBED, logs clean ────────────────────────


class TestMintFailureBranchesNeverLeak:
    def _mint(self, transport: FakeDhanAuthHttp) -> None:
        mint_access_token(
            _CREDS,
            transport=cast("Any", transport),
            totp_minter=_fake_totp,
            clock=_clock,
            sleeper=lambda _s: None,
        )

    def test_terminal_invalid_cred_url_scrubbed(self) -> None:
        transport = FakeDhanAuthHttp([FakeHttpError(400, _secret_bearing_url())])
        with capture_logs() as caps, pytest.raises(DhanAuthInvalidCredentials) as ei:
            self._mint(transport)
        _assert_exc_clean(ei.value, "mint.invalid_cred")
        _assert_logs_clean(caps, "mint.invalid_cred")
        assert transport.call_count == 1  # zero retry on terminal (bad creds never re-sent)

    def test_throttle_exhausted_all_retries_scrubbed(self) -> None:
        transport = FakeDhanAuthHttp([FakeHttpError(429, _secret_bearing_url()) for _ in range(3)])
        with capture_logs() as caps, pytest.raises(DhanTokenMintThrottled) as ei:
            self._mint(transport)
        _assert_exc_clean(ei.value, "mint.throttled")
        _assert_logs_clean(caps, "mint.throttled")  # the per-retry warn logs are clean too

    def test_network_error_no_status_plaintext_secret_scrubbed(self) -> None:
        # A network error whose message embeds the secret OUTSIDE a URL (scrub_secrets path).
        transport = FakeDhanAuthHttp(
            [FakeHttpError(None, f"NameResolutionError sending pin={_PIN} code={_TOTP_CODE}")]
        )
        with capture_logs() as caps, pytest.raises(DhanAuthError) as ei:
            self._mint(transport)
        _assert_exc_clean(ei.value, "mint.network")
        _assert_logs_clean(caps, "mint.network")

    def test_2xx_without_token_is_scrubbed(self) -> None:
        transport = FakeDhanAuthHttp([{"status": "ok but no token", "echo": _PIN}])
        with capture_logs() as caps, pytest.raises(DhanAuthError) as ei:
            self._mint(transport)
        _assert_exc_clean(ei.value, "mint.no_token")
        _assert_logs_clean(caps, "mint.no_token")


# ── 3. The "unexpected response shape" leak class (PR-2 analog) ───────────────────────────


class TestUnexpectedResponseShapeNeverLeaks:
    def test_token_and_echoed_creds_in_anomalous_shape_never_logged(self) -> None:
        # Token buried in a nested shape ALONGSIDE echoed secrets — the success path must adopt
        # the token yet log ONLY ``source=mint`` (never the payload).
        payload: Mapping[str, Any] = {
            "data": {"token": _TOKEN, "echoedPin": _PIN, "debug": f"totp={_TOTP_CODE}"}
        }
        transport = FakeDhanAuthHttp([payload])
        with capture_logs() as caps:
            minted = mint_access_token(
                _CREDS,
                transport=cast("Any", transport),
                totp_minter=_fake_totp,
                clock=_clock,
                sleeper=lambda _s: None,
            )
        assert minted.token == _TOKEN  # extracted correctly
        # The token/pin/code the payload echoed must NOT be in any log record.
        _assert_logs_clean(caps, "mint.anomalous_shape")

    def test_unexpected_shape_with_no_token_raises_scrubbed(self) -> None:
        payload: Mapping[str, Any] = {"error": f"bad totp {_TOTP_CODE}", "pin_echo": _PIN}
        transport = FakeDhanAuthHttp([payload])
        with capture_logs() as caps, pytest.raises(DhanAuthError) as ei:
            mint_access_token(
                _CREDS,
                transport=cast("Any", transport),
                totp_minter=_fake_totp,
                clock=_clock,
                sleeper=lambda _s: None,
            )
        _assert_exc_clean(ei.value, "mint.unexpected_no_token")
        _assert_logs_clean(caps, "mint.unexpected_no_token")


# ── 4. renew(): headers-only, failure scrubbed ───────────────────────────────────────────


class TestRenewNeverLeaks:
    def test_transport_failure_wrapped_and_scrubbed(self) -> None:
        transport = FakeDhanAuthHttp(
            [FakeHttpError(500, f"upstream error token={_TOKEN} client={_CLIENT_ID}")]
        )
        with capture_logs() as caps, pytest.raises(DhanTokenRenewError) as ei:
            renew_access_token(_TOKEN, _CLIENT_ID, transport=cast("Any", transport), clock=_clock)
        _assert_exc_clean(ei.value, "renew.transport")
        _assert_logs_clean(caps, "renew.transport")

    def test_renew_success_logs_no_token(self) -> None:
        transport = FakeDhanAuthHttp([{"accessToken": _TOKEN2}])
        with capture_logs() as caps:
            minted = renew_access_token(
                _TOKEN, _CLIENT_ID, transport=cast("Any", transport), clock=_clock
            )
        assert minted.token == _TOKEN2
        _assert_logs_clean(caps, "renew.success")


# ── 5. token store: a write failure is scrubbed, logs carry only a fingerprint ────────────


class TestTokenStoreNeverLeaks:
    def _store(self, *, fail: bool) -> tuple[DhanParameterStoreTokenStore, FakeAwsClient]:
        client = FakeAwsClient(fail_put_parameter=fail)
        return DhanParameterStoreTokenStore(client=client, param_path=_PARAM), client

    def test_put_failure_is_scrubbed_and_logs_fingerprint_only(self) -> None:
        store, _ = self._store(fail=True)
        env = DhanTokenEnvelope(token=_TOKEN, minted_at_utc=_ANCHOR, source="mint")
        with capture_logs() as caps, pytest.raises(DhanTokenStoreError) as ei:
            store.store(env)
        _assert_exc_clean(ei.value, "store.put_failed")
        _assert_logs_clean(caps, "store.put_failed")

    def test_successful_store_logs_no_token(self) -> None:
        store, client = self._store(fail=False)
        env = DhanTokenEnvelope(token=_TOKEN, minted_at_utc=_ANCHOR, source="mint")
        with capture_logs() as caps:
            store.store(env)
        # The token lives in the SecureString (the vault) — that is intended, not a leak.
        assert client.parameters[_PARAM] and _TOKEN in client.parameters[_PARAM]
        # ...but no LOG record may carry it.
        _assert_logs_clean(caps, "store.success")


# ── 6. Full reauth lifecycle end-to-end + the filesystem oracle ──────────────────────────


class _FakeAlerter:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def token_auth_failed(self, *, vendor: str, reason: str, detail: str) -> bool:
        self.calls.append({"vendor": vendor, "reason": reason, "detail": detail})
        return True


@pytest.fixture
def _inject_totp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the reauth spine's mint to use the fake TOTP + a no-op sleeper (no pyotp / sleeps)."""
    real_mint = auth_mod.mint_access_token

    def _mint(creds: DhanCredentials, *, transport: object = None, clock: object = None) -> object:
        return real_mint(
            creds,
            transport=cast("Any", transport),
            totp_minter=_fake_totp,
            clock=cast("Any", clock),
            sleeper=lambda _s: None,
        )

    monkeypatch.setattr(reauth_mod, "mint_access_token", _mint)


def _manager(
    tmp_path: Path,
    transport: FakeDhanAuthHttp,
    *,
    audit_path: Path,
    alerter: AuthAlerter,
) -> DhanTokenManager:
    client = FakeAwsClient()
    store = DhanParameterStoreTokenStore(client=client, param_path=_PARAM)
    return DhanTokenManager(
        store,
        lambda: _CREDS,
        transport=cast("Any", transport),
        alerter=alerter,
        audit=DhanAuthAudit(audit_path),
        clock=_clock,
        lock_path=tmp_path / "dhan-auth.lock",
    )


@pytest.mark.usefixtures("_inject_totp")
def test_full_lifecycle_mint_renew_lockdown_never_leaks_anywhere(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit" / "dhan-auth.jsonl"
    lock_path = tmp_path / "dhan-auth.lock"
    alerter = _FakeAlerter()
    # Cold-start MINT ok → reauth#1 RENEW ok → reauth#2 RENEW fails → MINT 400 → LOCKDOWN.
    transport = FakeDhanAuthHttp(
        [
            {"accessToken": _TOKEN},  # cold-start mint
            {"accessToken": _TOKEN2},  # reauth#1 renew
            FakeHttpError(500, f"renew blew up token={_TOKEN2}"),  # reauth#2 renew fail
            FakeHttpError(400, _secret_bearing_url()),  # reauth#2 mint → invalid cred
        ]
    )
    with capture_logs() as caps:
        manager = _manager(tmp_path, transport, audit_path=audit_path, alerter=alerter)
        assert manager.current_token() == _TOKEN
        manager.reauth(manager.current_generation())  # renew → TOKEN2
        assert manager.current_token() == _TOKEN2
        with pytest.raises(DhanAuthLockdown) as ei:
            manager.reauth(manager.current_generation())  # renew-fail → mint-400 → lockdown

    # (a) The lockdown exception + every log record are secret-free.
    _assert_exc_clean(ei.value, "lifecycle.lockdown")
    _assert_logs_clean(caps, "lifecycle")
    # (b) The CRITICAL alert detail is scrubbed.
    assert alerter.calls and alerter.calls[0]["vendor"] == "dhan"
    _assert_clean(alerter.calls[0]["detail"], "alerter.detail")
    # (c) The audit chain verifies AND carries no secret.
    verify_chain(audit_path)
    _assert_clean(audit_path.read_text(encoding="utf-8"), "audit.file")
    # (d) FILESYSTEM ORACLE: no auth-written file on disk may contain any secret.
    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert audit_path in written and lock_path in written  # both were created
    for path in written:
        _assert_clean(path.read_bytes().decode("utf-8", "replace"), f"fs:{path.name}")


@pytest.mark.usefixtures("_inject_totp")
def test_cold_start_lockdown_alert_and_audit_are_clean(tmp_path: Path) -> None:
    audit_path = tmp_path / "dhan-auth.jsonl"
    alerter = _FakeAlerter()
    transport = FakeDhanAuthHttp([FakeHttpError(401, _secret_bearing_url())])
    with capture_logs() as caps, pytest.raises(DhanAuthLockdown) as ei:
        _manager(tmp_path, transport, audit_path=audit_path, alerter=alerter)
    _assert_exc_clean(ei.value, "coldstart.lockdown")
    _assert_logs_clean(caps, "coldstart")
    assert alerter.calls
    _assert_clean(alerter.calls[0]["detail"], "coldstart.alerter")
    if audit_path.exists():
        verify_chain(audit_path)
        _assert_clean(audit_path.read_text(encoding="utf-8"), "coldstart.audit")


# ── 7. The audit envelope round-trip never spills the token into an audit key ─────────────


def test_audit_never_records_a_secret_value(tmp_path: Path) -> None:
    audit_path = tmp_path / "dhan-auth.jsonl"
    audit = DhanAuthAudit(audit_path)
    # A legitimate fingerprint-only record.
    fp = "abcd1234"
    audit.record("DHAN_TOKEN_ADOPT", subject=fp, outcome="mint", fingerprint=fp, source="mint")
    # A programming error that TRIES to log a secret key must RAISE, never write.
    with pytest.raises(ValueError, match="disallowed key"):
        audit.record("DHAN_TOKEN_MINT", subject=fp, token=_TOKEN)
    text = audit_path.read_text(encoding="utf-8")
    _assert_clean(text, "audit.record")
    # The one written line is a valid chain carrying only the fingerprint.
    verify_chain(audit_path)
    line = json.loads(text.strip().splitlines()[0])
    assert line["payload"]["fingerprint"] == fp
    assert "token" not in json.dumps(line["payload"])


# ── 8. A default-config structlog render (not just capture) also carries no secret ────────


def test_rendered_structlog_output_carries_no_secret(tmp_path: Path, capsys: Any) -> None:
    # Configure structlog to actually RENDER to stdout (KeyValue) — a belt beyond capture_logs,
    # proving the rendered stream (what an operator sees) is clean too. The prior global config
    # is saved + restored so no sibling test's structlog config is disturbed.
    prior_config = structlog.get_config()
    structlog.configure(
        processors=[structlog.processors.KeyValueRenderer()],
        logger_factory=structlog.PrintLoggerFactory(),
    )
    try:
        transport = FakeDhanAuthHttp([FakeHttpError(429, _secret_bearing_url()) for _ in range(3)])
        with pytest.raises(DhanTokenMintThrottled):
            mint_access_token(
                _CREDS,
                transport=cast("Any", transport),
                totp_minter=_fake_totp,
                clock=_clock,
                sleeper=lambda _s: None,
            )
    finally:
        structlog.configure(**prior_config)
    captured = capsys.readouterr()
    _assert_clean(captured.out, "rendered.stdout")
    _assert_clean(captured.err, "rendered.stderr")
