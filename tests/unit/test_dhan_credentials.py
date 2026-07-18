"""Unit tests for :mod:`brutex.adapters.dhan.credentials` — 100% line+branch.

Driven by the shared ``FakeAwsClient`` through the REAL reader (no boto3, no network).
The conftest AWS-hermeticity belt additionally blocks any live boto3 call.
"""

from __future__ import annotations

import pytest
from brutex.adapters.dhan import credentials as cred
from brutex.adapters.dhan.constants import (
    DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT,
    DHAN_TOKEN_PARAM_CLIENT_ID_DEFAULT,
)
from brutex.config.credentials_types import CredentialsError

from tests.fakes.fake_aws_client import FakeAwsClient

_ACCESS = DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT
_CLIENT = DHAN_TOKEN_PARAM_CLIENT_ID_DEFAULT


def test_load_happy() -> None:
    client = FakeAwsClient(parameters={_ACCESS: "jwt-token", _CLIENT: "1100"})
    got = cred.load_dhan_aws_credentials(client=client)
    assert got == cred.DhanCredentials(access_token="jwt-token", client_id="1100")


def test_load_access_token_absent() -> None:
    client = FakeAwsClient(parameters={_CLIENT: "1100"}, param_not_found=frozenset({_ACCESS}))
    with pytest.raises(CredentialsError, match="missing Dhan credentials"):
        cred.load_dhan_aws_credentials(client=client)


def test_load_client_id_absent() -> None:
    client = FakeAwsClient(parameters={_ACCESS: "jwt"}, param_not_found=frozenset({_CLIENT}))
    with pytest.raises(CredentialsError, match=r"client-id"):
        cred.load_dhan_aws_credentials(client=client)


def test_load_both_absent() -> None:
    client = FakeAwsClient(param_not_found=frozenset({_ACCESS, _CLIENT}))
    with pytest.raises(CredentialsError, match="missing Dhan credentials"):
        cred.load_dhan_aws_credentials(client=client)


def test_load_blank_value_is_missing() -> None:
    client = FakeAwsClient(parameters={_ACCESS: "  ", _CLIENT: "1100"})
    with pytest.raises(CredentialsError, match="access-token"):
        cred.load_dhan_aws_credentials(client=client)


def test_load_hard_read_failure_fails_fast() -> None:
    client = FakeAwsClient(parameters={_ACCESS: "jwt", _CLIENT: "1"}, fail_get_parameter=True)
    with pytest.raises(CredentialsError, match="failed to read the Dhan credential"):
        cred.load_dhan_aws_credentials(client=client)


def test_how_to_fix_names_subscription_gate() -> None:
    assert "Data API" in cred.DHAN_CREDS_HOW_TO_FIX
    assert "₹499" in cred.DHAN_CREDS_HOW_TO_FIX


def test_credentials_repr_never_exposes_the_token() -> None:
    """``repr(DhanCredentials)`` must elide the JWT (round-1 security NIT).

    The access token is a genuine secret; the default dataclass repr would
    render it verbatim into any log/traceback that interpolates the object.
    The non-secret ``client_id`` stays visible for diagnostics.
    """
    creds = cred.DhanCredentials(access_token="jwt-secret-value", client_id="1100")
    assert "jwt-secret-value" not in repr(creds)
    assert "1100" in repr(creds)
