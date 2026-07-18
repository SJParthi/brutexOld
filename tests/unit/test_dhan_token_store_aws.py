"""Unit tests for :mod:`brutex.adapters.dhan.token_store_aws` — 100% line+branch."""

from __future__ import annotations

import pytest
from brutex.adapters.dhan import token_store_aws as ts
from brutex.adapters.dhan.constants import DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT

from tests.fakes.fake_aws_client import FakeAwsClient

_TOKEN = DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT


def _store(client: FakeAwsClient) -> ts.DhanParameterStoreTokenStore:
    return ts.DhanParameterStoreTokenStore(client=client, token_param=_TOKEN)


def test_readonly_and_token_param() -> None:
    store = _store(FakeAwsClient())
    assert store.readonly is True
    assert store.token_param == _TOKEN


def test_read_token_hit() -> None:
    store = _store(FakeAwsClient(parameters={_TOKEN: "jwt-abc"}))
    assert store.read_token() == "jwt-abc"


def test_read_token_absent_returns_none() -> None:
    store = _store(FakeAwsClient(param_not_found=frozenset({_TOKEN})))
    assert store.read_token() is None


def test_read_token_blank_returns_none() -> None:
    store = _store(FakeAwsClient(parameters={_TOKEN: "   "}))
    assert store.read_token() is None


def test_read_token_hard_failure_returns_none_but_warns() -> None:
    store = _store(FakeAwsClient(fail_get_parameter=True))
    assert store.read_token() is None


def test_read_with_reason_hit() -> None:
    store = _store(FakeAwsClient(parameters={_TOKEN: "jwt-abc"}))
    token, miss = store.read_with_reason()
    assert token == "jwt-abc"
    assert miss is None


def test_read_with_reason_parameter_not_found() -> None:
    store = _store(FakeAwsClient(param_not_found=frozenset({_TOKEN})))
    token, miss = store.read_with_reason()
    assert token is None
    assert miss is not None
    assert miss.kind == "absent"
    assert miss.error_type == "ParameterNotFound"


def test_read_with_reason_hard_error() -> None:
    store = _store(FakeAwsClient(fail_get_parameter=True))
    token, miss = store.read_with_reason()
    assert token is None
    assert miss is not None
    assert miss.kind == "error"
    assert miss.error_type == "FakeAwsError"


def test_read_with_reason_blank_absent() -> None:
    store = _store(FakeAwsClient(parameters={_TOKEN: ""}))
    token, miss = store.read_with_reason()
    assert token is None
    assert miss is not None
    assert miss.kind == "absent"
    assert miss.error_type is None


def test_store_is_read_only_fail_loud() -> None:
    store = _store(FakeAwsClient())
    with pytest.raises(ts.DhanReadOnlyMintError, match="read-only consumer"):
        store.store("some-token")


def test_build_readonly_token_store_with_injected_client() -> None:
    store = ts.build_readonly_token_store(client=FakeAwsClient(parameters={_TOKEN: "jwt"}))
    assert isinstance(store, ts.DhanParameterStoreTokenStore)
    assert store.read_token() == "jwt"
