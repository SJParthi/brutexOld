"""Unit tests for the AWS-only Dhan credential reader (DEC-DHAN-AUTH-001).

The Dhan credential triple (client-id / pin / totp-secret) is AWS-SSM-only, MANDATORY +
FAIL-FAST exactly like the Groww reader. All values are FAKE (never a real secret); a
``FakeAwsClient`` drives every branch with NO boto3 install and NO network. Also pins that
``repr(DhanCredentials)`` never leaks the pin / totp secret.
"""

from __future__ import annotations

import pytest
from brutex.config.credentials_aws import load_dhan_aws_credentials
from brutex.config.credentials_types import CredentialsError, DhanCredentials

from tests.fakes.fake_aws_client import FakeAwsClient

_CID = "/tickvault/prod/dhan/client-id"
_PIN = "/tickvault/prod/dhan/pin"
_TOTP = "/tickvault/prod/dhan/totp-secret"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "BRUTEX_TOKEN_PARAM_DHAN_CLIENT_ID",
        "BRUTEX_TOKEN_PARAM_DHAN_PIN",
        "BRUTEX_TOKEN_PARAM_DHAN_TOTP_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)


def _seeded() -> FakeAwsClient:
    return FakeAwsClient(parameters={_CID: "FAKE-CID", _PIN: "FAKE-PIN", _TOTP: "FAKE-TOTP"})


def test_happy_path() -> None:
    creds = load_dhan_aws_credentials(client=_seeded())
    assert creds == DhanCredentials(client_id="FAKE-CID", pin="FAKE-PIN", totp_secret="FAKE-TOTP")


def test_env_override_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRUTEX_TOKEN_PARAM_DHAN_CLIENT_ID", "/c/cid")
    monkeypatch.setenv("BRUTEX_TOKEN_PARAM_DHAN_PIN", "/c/pin")
    monkeypatch.setenv("BRUTEX_TOKEN_PARAM_DHAN_TOTP_SECRET", "/c/totp")
    fake = FakeAwsClient(parameters={"/c/cid": "C", "/c/pin": "P", "/c/totp": "T"})
    creds = load_dhan_aws_credentials(client=fake)
    assert creds.client_id == "C" and creds.pin == "P" and creds.totp_secret == "T"


@pytest.mark.parametrize("missing", [_CID, _PIN, _TOTP])
def test_any_absent_raises_naming_every_missing(missing: str) -> None:
    fake = FakeAwsClient(param_not_found=frozenset({missing}), parameters=_seeded().parameters)
    with pytest.raises(CredentialsError) as ei:
        load_dhan_aws_credentials(client=fake)
    assert missing in str(ei.value)
    assert "Dhan" in str(ei.value)


def test_all_absent_names_all_three() -> None:
    fake = FakeAwsClient(param_not_found=frozenset({_CID, _PIN, _TOTP}))
    with pytest.raises(CredentialsError) as ei:
        load_dhan_aws_credentials(client=fake)
    for path in (_CID, _PIN, _TOTP):
        assert path in str(ei.value)


def test_blank_value_treated_absent() -> None:
    fake = FakeAwsClient(parameters={_CID: "C", _PIN: "   ", _TOTP: "T"})
    with pytest.raises(CredentialsError) as ei:
        load_dhan_aws_credentials(client=fake)
    assert _PIN in str(ei.value)


def test_hard_read_failure_fails_fast() -> None:
    fake = FakeAwsClient(fail_get_parameter=True)
    with pytest.raises(CredentialsError):
        load_dhan_aws_credentials(client=fake)


def test_repr_never_leaks_secrets() -> None:
    creds = load_dhan_aws_credentials(client=_seeded())
    blob = f"{creds!r} {creds!s}"
    assert "FAKE-PIN" not in blob
    assert "FAKE-TOTP" not in blob
    # The non-secret client id may show.
    assert "FAKE-CID" in repr(creds)
