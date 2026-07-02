"""Unit tests for ``ParameterStoreTokenStore.load_with_reason`` (R7 seam).

DEC-DOCTOR-LIVE-PROOF-KIT-001. Stem-matched to
``brutex/adapters/groww/token_store_aws.py`` so the strict per-module coverage
discovery gate scopes these tests to the module they cover (moved out of the
combined ``test_doctor_live_seams.py``, whose stem matched no touched module).
"""

from __future__ import annotations

from datetime import UTC, datetime

from brutex.adapters.groww.aws_client import SSM_PARAMETER_NOT_FOUND
from brutex.adapters.groww.token_store_aws import (
    ParameterStoreTokenStore,
    build_readonly_token_store_with,
)

from tests.fakes.fake_aws_client import FakeAwsClient

_NOW = datetime(2026, 7, 2, 6, 0, tzinfo=UTC)


def _store(client: FakeAwsClient) -> ParameterStoreTokenStore:
    return ParameterStoreTokenStore(client=client, token_param="/t/token")


class TestLoadWithReason:
    def test_hit_returns_token_and_no_miss(self) -> None:
        token, miss = _store(FakeAwsClient(parameters={"/t/token": "tok"})).load_with_reason(
            now_utc=_NOW
        )
        assert miss is None
        assert token is not None
        assert token.access_token == "tok"

    def test_read_error_carries_error_kind(self) -> None:
        token, miss = _store(FakeAwsClient(fail_get_parameter=True)).load_with_reason(now_utc=_NOW)
        assert token is None
        assert miss is not None
        assert (miss.kind, miss.error_type) == ("error", "FakeAwsError")
        assert "access denied" in miss.detail

    def test_parameter_not_found_is_genuinely_absent(self) -> None:
        client = FakeAwsClient(param_not_found=frozenset({"/t/token"}))
        token, miss = _store(client).load_with_reason(now_utc=_NOW)
        assert token is None
        assert miss is not None
        assert miss.kind == "absent"
        assert miss.error_type == SSM_PARAMETER_NOT_FOUND

    def test_blank_value_is_absent(self) -> None:
        token, miss = _store(FakeAwsClient(parameters={"/t/token": "  "})).load_with_reason(
            now_utc=_NOW
        )
        assert token is None
        assert miss is not None
        assert (miss.kind, miss.error_type) == ("absent", None)

    def test_token_param_property(self) -> None:
        assert _store(FakeAwsClient()).token_param == "/t/token"

    def test_load_delegates_and_keeps_never_raises_contract(self) -> None:
        from brutex.adapters.groww.auth import GrowwCredentials

        creds = GrowwCredentials(api_key="k", totp_secret="s")
        assert _store(FakeAwsClient(fail_get_parameter=True)).load(creds, now_utc=_NOW) is None
        assert (
            _store(FakeAwsClient(param_not_found=frozenset({"/t/token"}))).load(creds, now_utc=_NOW)
            is None
        )
        hit = _store(FakeAwsClient(parameters={"/t/token": "tok"})).load(creds, now_utc=_NOW)
        assert hit is not None
        assert hit.access_token == "tok"

    def test_build_readonly_store_with_resolves_default_param(self) -> None:
        store = build_readonly_token_store_with(FakeAwsClient())
        assert store.token_param == "/tickvault/prod/groww/access-token"
