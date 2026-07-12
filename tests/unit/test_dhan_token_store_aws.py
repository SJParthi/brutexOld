"""Tests for the Dhan token store (DEC-DHAN-AUTH-001) — read + WRITE the SecureString."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from brutex.adapters.dhan.errors import DhanTokenStoreError
from brutex.adapters.dhan.token_store_aws import (
    DhanParameterStoreTokenStore,
    DhanTokenEnvelope,
    decode_envelope,
    encode_envelope,
    token_fingerprint,
)

from tests.fakes.fake_aws_client import FakeAwsClient

_PARAM = "/tickvault/prod/dhan/access-token"
_ANCHOR = datetime(2026, 7, 12, 6, 0, 0, tzinfo=UTC)
_ENV = DhanTokenEnvelope(token="FAKE-JWT", minted_at_utc=_ANCHOR, source="mint")


def _store(client: FakeAwsClient) -> DhanParameterStoreTokenStore:
    return DhanParameterStoreTokenStore(client=client, param_path=_PARAM)


def test_readonly_is_false() -> None:
    assert _store(FakeAwsClient()).readonly is False


def test_token_param_property() -> None:
    assert _store(FakeAwsClient()).token_param == _PARAM


def test_default_param_path_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BRUTEX_TOKEN_PARAM_DHAN_ACCESS_TOKEN", raising=False)
    store = DhanParameterStoreTokenStore(client=FakeAwsClient())
    assert store.token_param == _PARAM


def test_fingerprint_stable_and_short() -> None:
    fp = token_fingerprint("FAKE-JWT")
    assert len(fp) == 8
    assert fp == token_fingerprint("FAKE-JWT")
    assert fp != token_fingerprint("OTHER")


class TestEnvelopeCodec:
    def test_round_trip(self) -> None:
        assert decode_envelope(encode_envelope(_ENV)) == _ENV

    def test_round_trip_renew_source(self) -> None:
        env = DhanTokenEnvelope(token="FAKE", minted_at_utc=_ANCHOR, source="renew")
        assert decode_envelope(encode_envelope(env)) == env

    def test_naive_datetime_coerced_to_utc(self) -> None:
        # A stored ISO with no tz reads back as UTC.
        text = '{"v":1,"token":"FAKE","minted_at_utc":"2026-07-12T06:00:00","source":"mint"}'
        env = decode_envelope(text)
        assert env.minted_at_utc == _ANCHOR

    @pytest.mark.parametrize(
        "text",
        [
            "not json",
            "[]",
            '{"v":2,"token":"x","minted_at_utc":"2026-07-12T06:00:00Z","source":"mint"}',
            '{"v":1,"token":"  ","minted_at_utc":"2026-07-12T06:00:00Z","source":"mint"}',
            '{"v":1,"token":123,"minted_at_utc":"2026-07-12T06:00:00Z","source":"mint"}',
            '{"v":1,"token":"x","minted_at_utc":123,"source":"mint"}',
            '{"v":1,"token":"x","minted_at_utc":"nonsense","source":"mint"}',
            '{"v":1,"token":"x","minted_at_utc":"2026-07-12T06:00:00Z","source":"bogus"}',
        ],
    )
    def test_bad_shapes_raise(self, text: str) -> None:
        with pytest.raises(ValueError):
            decode_envelope(text)


class TestLoad:
    def test_hit(self) -> None:
        client = FakeAwsClient(parameters={_PARAM: encode_envelope(_ENV)})
        assert _store(client).load() == _ENV

    def test_absent_returns_none(self) -> None:
        client = FakeAwsClient(param_not_found=frozenset({_PARAM}))
        assert _store(client).load() is None

    def test_blank_returns_none(self) -> None:
        client = FakeAwsClient(parameters={_PARAM: "   "})
        assert _store(client).load() is None

    def test_read_error_returns_none(self) -> None:
        client = FakeAwsClient(fail_get_parameter=True)
        assert _store(client).load() is None

    def test_corrupt_returns_none(self) -> None:
        client = FakeAwsClient(parameters={_PARAM: "not-json"})
        assert _store(client).load() is None


class TestLoadWithReason:
    def test_hit(self) -> None:
        client = FakeAwsClient(parameters={_PARAM: encode_envelope(_ENV)})
        env, miss = _store(client).load_with_reason()
        assert env == _ENV and miss is None

    def test_absent_kind(self) -> None:
        client = FakeAwsClient(param_not_found=frozenset({_PARAM}))
        env, miss = _store(client).load_with_reason()
        assert env is None and miss is not None and miss.kind == "absent"
        assert miss.error_type == "ParameterNotFound"

    def test_blank_kind_absent(self) -> None:
        client = FakeAwsClient(parameters={_PARAM: ""})
        _env, miss = _store(client).load_with_reason()
        assert miss is not None and miss.kind == "absent" and miss.error_type is None

    def test_error_kind(self) -> None:
        client = FakeAwsClient(fail_get_parameter=True)
        _env, miss = _store(client).load_with_reason()
        assert miss is not None and miss.kind == "error" and miss.error_type == "FakeAwsError"

    def test_corrupt_kind(self) -> None:
        client = FakeAwsClient(parameters={_PARAM: "{bad"})
        _env, miss = _store(client).load_with_reason()
        assert miss is not None and miss.kind == "corrupt"


class TestStore:
    def test_writes_securestring_overwrite(self) -> None:
        client = FakeAwsClient()
        _store(client).store(_ENV)
        assert client.put_parameter_calls == 1
        name, value, secure, overwrite = client.put_parameter_args[0]
        assert name == _PARAM and secure is True and overwrite is True
        # Written value is the envelope; a re-load reads it back.
        assert decode_envelope(value) == _ENV

    def test_store_failure_raises_scrubbed(self) -> None:
        client = FakeAwsClient(fail_put_parameter=True)
        with pytest.raises(DhanTokenStoreError) as ei:
            _store(client).store(_ENV)
        # The token bytes never appear in the error.
        assert "FAKE-JWT" not in str(ei.value)
        assert ei.value.__cause__ is None
