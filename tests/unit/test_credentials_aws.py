"""Unit tests for the AWS-only Groww credential reader (DEC-AWS-ONLY-CREDS-001).

AWS Parameter Store is now the SOLE Groww credential source: the in-app webpage AND the
local env-var / ``credentials.json`` file paths were removed. :func:`load_groww_aws_credentials`
is MANDATORY + FAIL-FAST — it reads the two SecureStrings and raises :class:`CredentialsError`
on any absence / unreachability, never returning ``None`` and never falling back.

All values are FAKE — never a real secret. A ``FakeAwsClient`` drives every branch with NO
boto3 install and NO network.
"""

from __future__ import annotations

import pytest
from brutex.config.credentials import (
    CredentialsError,
    GrowwFileCredentials,
    groww_credentials_present,
    resolve_groww_aws_credentials,
)
from brutex.config.credentials_aws import load_groww_aws_credentials

from tests.fakes.fake_aws_client import FakeAwsClient

_API_KEY_PATH = "/tickvault/prod/groww/api-key"
_TOTP_PATH = "/tickvault/prod/groww/totp-secret"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure no env override leaks into the parameter-path / region resolution."""
    for name in (
        "BRUTEX_TOKEN_AWS_REGION",
        "AWS_REGION",
        "BRUTEX_TOKEN_PARAM_API_KEY",
        "BRUTEX_TOKEN_PARAM_TOTP_SECRET",
        "BRUTEX_GROWW_API_KEY",
        "BRUTEX_GROWW_TOTP_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)


def _fake_with_params() -> FakeAwsClient:
    return FakeAwsClient(parameters={_API_KEY_PATH: "AWS-KEY", _TOTP_PATH: "AWS-SECRET"})


class TestLoadGrowwAwsCredentials:
    """The reader is mandatory + fail-fast; both fields resolve or it raises."""

    def test_full_pair_resolves(self) -> None:
        creds = load_groww_aws_credentials(client=_fake_with_params())
        assert creds == GrowwFileCredentials(api_key="AWS-KEY", totp_secret="AWS-SECRET")

    def test_env_overridable_paths(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_TOKEN_PARAM_API_KEY", "/custom/key")
        monkeypatch.setenv("BRUTEX_TOKEN_PARAM_TOTP_SECRET", "/custom/totp")
        fake = FakeAwsClient(parameters={"/custom/key": "K2", "/custom/totp": "S2"})
        creds = load_groww_aws_credentials(client=fake)
        assert creds == GrowwFileCredentials(api_key="K2", totp_secret="S2")

    def test_missing_api_key_raises_naming_the_path(self) -> None:
        fake = FakeAwsClient(
            parameters={_TOTP_PATH: "S"}, param_not_found=frozenset({_API_KEY_PATH})
        )
        with pytest.raises(CredentialsError) as exc:
            load_groww_aws_credentials(client=fake)
        assert _API_KEY_PATH in str(exc.value)
        assert "deploy/aws_token_minter/deploy.sh" in str(exc.value)

    def test_missing_totp_raises_naming_the_path(self) -> None:
        fake = FakeAwsClient(
            parameters={_API_KEY_PATH: "K"}, param_not_found=frozenset({_TOTP_PATH})
        )
        with pytest.raises(CredentialsError) as exc:
            load_groww_aws_credentials(client=fake)
        assert _TOTP_PATH in str(exc.value)

    def test_both_absent_raises_naming_both(self) -> None:
        fake = FakeAwsClient(param_not_found=frozenset({_API_KEY_PATH, _TOTP_PATH}))
        with pytest.raises(CredentialsError) as exc:
            load_groww_aws_credentials(client=fake)
        assert _API_KEY_PATH in str(exc.value)
        assert _TOTP_PATH in str(exc.value)

    def test_blank_value_raises(self) -> None:
        """A blank SecureString is treated as absent → fail-fast."""
        fake = FakeAwsClient(parameters={_API_KEY_PATH: "   ", _TOTP_PATH: "S"})
        with pytest.raises(CredentialsError) as exc:
            load_groww_aws_credentials(client=fake)
        assert _API_KEY_PATH in str(exc.value)

    def test_read_error_is_fail_fast_not_absent(self) -> None:
        """An access-denied / network error raises immediately (not mistaken for absent)."""
        fake = FakeAwsClient(fail_get_parameter=True)
        with pytest.raises(CredentialsError) as exc:
            load_groww_aws_credentials(client=fake)
        assert "FakeAwsError" in str(exc.value)
        assert "deploy/aws_token_minter/deploy.sh" in str(exc.value)


class TestResolveGrowwAwsCredentials:
    """resolve_groww_aws_credentials — the public AWS-only resolver used by every pull site."""

    def test_resolves_and_normalises(self) -> None:
        creds = resolve_groww_aws_credentials(
            aws_loader=lambda: GrowwFileCredentials(api_key="  K \n", totp_secret=" S ")
        )
        assert creds == GrowwFileCredentials(api_key="K", totp_secret="S")

    def test_default_loader_used_when_none_passed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No aws_loader ⇒ the real module loader is imported + called (fail-fast on no AWS)."""
        sentinel = GrowwFileCredentials(api_key="REAL-K", totp_secret="REAL-S")
        monkeypatch.setattr(
            "brutex.config.credentials_aws.load_groww_aws_credentials",
            lambda: sentinel,
        )
        assert resolve_groww_aws_credentials() == sentinel

    def test_fail_fast_propagates(self) -> None:
        def _raise() -> GrowwFileCredentials:
            raise CredentialsError("no AWS token")

        with pytest.raises(CredentialsError):
            resolve_groww_aws_credentials(aws_loader=_raise)


class TestGrowwCredentialsPresent:
    """groww_credentials_present — the dry-run gate; degrades to False, never raises."""

    def test_true_when_resolved(self) -> None:
        assert (
            groww_credentials_present(
                aws_loader=lambda: GrowwFileCredentials(api_key="K", totp_secret="S")
            )
            is True
        )

    def test_false_on_fail_fast(self) -> None:
        def _raise() -> GrowwFileCredentials:
            raise CredentialsError("no AWS token")

        assert groww_credentials_present(aws_loader=_raise) is False
