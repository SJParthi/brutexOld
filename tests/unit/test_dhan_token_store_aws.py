"""Tests for :mod:`brutex.adapters.dhan.token_store_aws` — the READ-ONLY token backend."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from brutex.adapters.dhan.errors import DhanTokenReadOnlyError
from brutex.adapters.dhan.token_store_aws import (
    DhanCachedToken,
    ParameterStoreDhanTokenStore,
    build_readonly_dhan_token_store_with,
)

from tests.fakes.fake_aws_client import FakeAwsClient

_TOKEN_PATH = "/tickvault/prod/dhan/access-token"
_NOW = datetime(2026, 7, 12, 4, 0, tzinfo=UTC)


def _store(client: FakeAwsClient, *, param: str = _TOKEN_PATH) -> ParameterStoreDhanTokenStore:
    return ParameterStoreDhanTokenStore(client=client, token_param=param)


def test_readonly_and_token_param_properties() -> None:
    store = _store(FakeAwsClient())
    assert store.readonly is True
    assert store.token_param == _TOKEN_PATH


def test_load_hit() -> None:
    store = _store(FakeAwsClient(parameters={_TOKEN_PATH: "JWT"}))
    token = store.load(now_utc=_NOW)
    assert token == DhanCachedToken(access_token="JWT")


def test_cached_token_repr_redacts() -> None:
    assert "JWT" not in repr(DhanCachedToken(access_token="JWT"))


def test_load_absent_returns_none() -> None:
    store = _store(FakeAwsClient(param_not_found=frozenset({_TOKEN_PATH})))
    assert store.load(now_utc=_NOW) is None


def test_load_blank_returns_none() -> None:
    store = _store(FakeAwsClient(parameters={_TOKEN_PATH: "  "}))
    assert store.load(now_utc=_NOW) is None


def test_load_hard_error_returns_none_never_raises() -> None:
    store = _store(FakeAwsClient(fail_get_parameter=True))
    assert store.load(now_utc=_NOW) is None


def test_load_with_reason_absent_is_parameter_not_found() -> None:
    store = _store(FakeAwsClient(param_not_found=frozenset({_TOKEN_PATH})))
    token, miss = store.load_with_reason(now_utc=_NOW)
    assert token is None
    assert miss is not None
    assert miss.kind == "absent"
    assert miss.error_type == "ParameterNotFound"


def test_load_with_reason_blank_is_absent_no_error_type() -> None:
    store = _store(FakeAwsClient(parameters={_TOKEN_PATH: ""}))
    token, miss = store.load_with_reason(now_utc=_NOW)
    assert token is None
    assert miss is not None and miss.kind == "absent" and miss.error_type is None


def test_load_with_reason_hard_error() -> None:
    store = _store(FakeAwsClient(fail_get_parameter=True))
    token, miss = store.load_with_reason(now_utc=_NOW)
    assert token is None
    assert miss is not None and miss.kind == "error" and miss.error_type == "FakeAwsError"


def test_store_is_read_only_fail_loud() -> None:
    store = _store(FakeAwsClient())
    with pytest.raises(DhanTokenReadOnlyError):
        store.store(token="new", now_utc=_NOW)


def test_build_readonly_with_uses_default_param() -> None:
    store = build_readonly_dhan_token_store_with(FakeAwsClient(parameters={_TOKEN_PATH: "JWT"}))
    assert store.token_param == _TOKEN_PATH
    assert store.load(now_utc=_NOW) == DhanCachedToken(access_token="JWT")
