"""Unit tests for the read-only Parameter-Store token store (DEC-TOKEN-BROKER-AWS-001).

Drives :class:`ParameterStoreTokenStore` + :func:`build_readonly_token_store_with`
through the REAL broker code via ``FakeAwsClient`` (no boto3, no network) to 100%
line+branch. The full failure permutation matrix lives in
``test_token_broker_permutations.py``; this file pins the store primitives (SSM read,
session anchoring, absent/blank/unreachable miss, and the read-only fail-fast on store).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from brutex.adapters.groww.auth import GrowwCredentials
from brutex.adapters.groww.constants import (
    GROWW_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT,
    resolve_token_broker_region,
    resolve_token_param_path,
)
from brutex.adapters.groww.token_cache import current_session
from brutex.adapters.groww.token_store_aws import (
    ParameterStoreTokenStore,
    TokenBrokerReadOnlyError,
    build_readonly_token_store_with,
)

from tests.fakes.fake_aws_client import FakeAwsClient

_CREDS = GrowwCredentials(api_key="key-1", totp_secret="sec-1")
_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)  # 17:30 IST — same-session as 2026-07-01
_TOKEN_PATH = GROWW_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean broker env for every test (no override paths / regions leaking in)."""
    for name in (
        "BRUTEX_TOKEN_AWS_REGION",
        "AWS_REGION",
        "BRUTEX_TOKEN_PARAM_ACCESS_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)


def _store(client: FakeAwsClient) -> ParameterStoreTokenStore:
    return ParameterStoreTokenStore(client=client, token_param=_TOKEN_PATH)


class TestParameterStoreTokenStoreLoad:
    """load reads the shared SecureString; absent/blank/unreachable ⇒ None (never raises)."""

    def test_hit_returns_cached_token(self) -> None:
        fake = FakeAwsClient(parameters={_TOKEN_PATH: "SHARED-TOKEN"})
        cached = _store(fake).load(_CREDS, now_utc=_NOW)
        assert cached is not None
        assert cached.access_token == "SHARED-TOKEN"
        assert cached.issued_for_session == current_session(_NOW)

    def test_reads_with_decryption(self) -> None:
        """The read must request WithDecryption (SecureString → plaintext)."""
        seen: dict[str, object] = {}

        class _SpyClient(FakeAwsClient):
            def get_parameter(self, *, name: str, with_decryption: bool) -> str | None:
                seen["name"] = name
                seen["with_decryption"] = with_decryption
                return "TOK"

        cached = _store(_SpyClient()).load(_CREDS, now_utc=_NOW)
        assert cached is not None and cached.access_token == "TOK"
        assert seen == {"name": _TOKEN_PATH, "with_decryption": True}

    def test_absent_is_miss(self) -> None:
        fake = FakeAwsClient()  # parameter not seeded
        assert _store(fake).load(_CREDS, now_utc=_NOW) is None

    def test_blank_value_is_miss(self) -> None:
        fake = FakeAwsClient(parameters={_TOKEN_PATH: "   "})
        assert _store(fake).load(_CREDS, now_utc=_NOW) is None

    def test_param_not_found_is_miss(self) -> None:
        fake = FakeAwsClient(param_not_found=frozenset({_TOKEN_PATH}))
        assert _store(fake).load(_CREDS, now_utc=_NOW) is None

    def test_read_error_is_miss_never_raises(self) -> None:
        fake = FakeAwsClient(fail_get_parameter=True)
        assert _store(fake).load(_CREDS, now_utc=_NOW) is None

    def test_creds_unused_shared_parameter(self) -> None:
        """The shared parameter is account-scoped: any creds resolve the same token."""
        fake = FakeAwsClient(parameters={_TOKEN_PATH: "ONE-TOKEN"})
        other = GrowwCredentials(api_key="key-2", totp_secret="s")
        a = _store(fake).load(_CREDS, now_utc=_NOW)
        b = _store(fake).load(other, now_utc=_NOW)
        assert a is not None and b is not None
        assert a.access_token == b.access_token == "ONE-TOKEN"


class TestParameterStoreTokenStoreReadOnly:
    """store fails LOUD — BruteX must never mint / write the shared token."""

    def test_store_raises_readonly_error(self) -> None:
        fake = FakeAwsClient()
        with pytest.raises(TokenBrokerReadOnlyError):
            _store(fake).store(_CREDS, "TOK", now_utc=_NOW)
        # Nothing was written anywhere.
        assert fake.get_parameter_calls == 0

    def test_readonly_error_message_redacts_token(self) -> None:
        with pytest.raises(TokenBrokerReadOnlyError) as exc:
            _store(FakeAwsClient()).store(_CREDS, "SECRET-TOKEN", now_utc=_NOW)
        assert "SECRET-TOKEN" not in str(exc.value)

    def test_readonly_error_is_auth_error(self) -> None:
        from brutex.adapters.groww.errors import GrowwAuthError

        assert issubclass(TokenBrokerReadOnlyError, GrowwAuthError)


class TestBuildReadonlyTokenStore:
    """build_readonly_token_store_with — path resolution + assembly."""

    def test_build_with_returns_store(self) -> None:
        store = build_readonly_token_store_with(FakeAwsClient())
        assert isinstance(store, ParameterStoreTokenStore)

    def test_build_with_uses_default_path(self) -> None:
        fake = FakeAwsClient(parameters={_TOKEN_PATH: "DEFAULTED"})
        store = build_readonly_token_store_with(fake)
        cached = store.load(_CREDS, now_utc=_NOW)
        assert cached is not None and cached.access_token == "DEFAULTED"

    def test_build_with_honours_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_TOKEN_PARAM_ACCESS_TOKEN", "/custom/token")
        fake = FakeAwsClient(parameters={"/custom/token": "OVERRIDDEN"})
        store = build_readonly_token_store_with(fake)
        cached = store.load(_CREDS, now_utc=_NOW)
        assert cached is not None and cached.access_token == "OVERRIDDEN"


class TestResolveTokenBrokerRegion:
    """resolve_token_broker_region — BRUTEX_TOKEN_AWS_REGION → AWS_REGION → ap-south-1."""

    def test_unset_is_ap_south_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_TOKEN_AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_REGION", raising=False)
        assert resolve_token_broker_region() == "ap-south-1"

    def test_brutex_env_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_TOKEN_AWS_REGION", "  us-east-1  ")
        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        assert resolve_token_broker_region() == "us-east-1"

    def test_aws_region_secondary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_TOKEN_AWS_REGION", raising=False)
        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        assert resolve_token_broker_region() == "eu-west-1"

    def test_blank_brutex_falls_to_aws_region(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_TOKEN_AWS_REGION", "   ")
        monkeypatch.setenv("AWS_REGION", "ap-southeast-1")
        assert resolve_token_broker_region() == "ap-southeast-1"


class TestResolveTokenParamPath:
    """resolve_token_param_path — env override, else default."""

    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SOME_PATH_ENV", raising=False)
        assert resolve_token_param_path("SOME_PATH_ENV", "/def/path") == "/def/path"

    def test_blank_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOME_PATH_ENV", "   ")
        assert resolve_token_param_path("SOME_PATH_ENV", "/def/path") == "/def/path"

    def test_env_override_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOME_PATH_ENV", "  /custom/x  ")
        assert resolve_token_param_path("SOME_PATH_ENV", "/def/path") == "/custom/x"
