"""Tests for :mod:`brutex.config.credentials_dhan_aws` + the ``DhanCredentials`` type."""

from __future__ import annotations

import pytest
from brutex.config.credentials_dhan_aws import (
    DHAN_CREDS_HOW_TO_FIX,
    load_dhan_aws_credentials,
)
from brutex.config.credentials_types import CredentialsError, DhanCredentials

from tests.fakes.fake_aws_client import FakeAwsClient

_CLIENT_ID_PATH = "/tickvault/prod/dhan/client-id"
_TOKEN_PATH = "/tickvault/prod/dhan/access-token"


def test_dhan_credentials_redacts_access_token_in_repr() -> None:
    creds = DhanCredentials(client_id="C123", access_token="SECRET.JWT.VALUE")
    text = repr(creds)
    assert "C123" in text
    assert "SECRET.JWT.VALUE" not in text  # repr=False guard


def test_load_both_present() -> None:
    client = FakeAwsClient(parameters={_CLIENT_ID_PATH: "C123", _TOKEN_PATH: "tok"})
    creds = load_dhan_aws_credentials(client=client)
    assert creds == DhanCredentials(client_id="C123", access_token="tok")


def test_load_client_id_absent_raises_naming_it() -> None:
    client = FakeAwsClient(
        parameters={_TOKEN_PATH: "tok"}, param_not_found=frozenset({_CLIENT_ID_PATH})
    )
    with pytest.raises(CredentialsError) as excinfo:
        load_dhan_aws_credentials(client=client)
    assert _CLIENT_ID_PATH in str(excinfo.value)
    assert DHAN_CREDS_HOW_TO_FIX in str(excinfo.value)


def test_load_token_absent_raises() -> None:
    client = FakeAwsClient(
        parameters={_CLIENT_ID_PATH: "C123"}, param_not_found=frozenset({_TOKEN_PATH})
    )
    with pytest.raises(CredentialsError) as excinfo:
        load_dhan_aws_credentials(client=client)
    assert _TOKEN_PATH in str(excinfo.value)


def test_load_blank_value_is_treated_as_absent() -> None:
    client = FakeAwsClient(parameters={_CLIENT_ID_PATH: "C123", _TOKEN_PATH: "   "})
    with pytest.raises(CredentialsError):
        load_dhan_aws_credentials(client=client)


def test_load_hard_read_error_is_fail_fast() -> None:
    client = FakeAwsClient(
        parameters={_CLIENT_ID_PATH: "C123", _TOKEN_PATH: "tok"}, fail_get_parameter=True
    )
    with pytest.raises(CredentialsError) as excinfo:
        load_dhan_aws_credentials(client=client)
    # A hard AWS error is fail-fast, never mistaken for "credential absent".
    assert "failed to read" in str(excinfo.value)
