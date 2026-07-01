"""Unit tests for the AWS Parameter-Store credential reader (DEC-TOKEN-BROKER-AWS-001).

Drives :func:`load_groww_aws_credentials` + the ``env → AWS → file`` chain insertion in
:func:`resolve_groww_file_credentials` through ``FakeAwsClient`` (no boto3, no network).

The reader is gated on ``BRUTEX_TOKEN_BROKER_READONLY`` and targets the operator's fixed
Parameter-Store paths ``/tickvault/prod/groww/{api-key,totp-secret}`` (region ``ap-south-1``).
"""

from __future__ import annotations

import pytest
from brutex.config.credentials import (
    CredentialsError,
    GrowwFileCredentials,
    resolve_groww_file_credentials,
)
from brutex.config.credentials_aws import load_groww_aws_credentials

from tests.fakes.fake_aws_client import FakeAwsClient

_READONLY_ENV = "BRUTEX_TOKEN_BROKER_READONLY"
_API_KEY_PATH = "/tickvault/prod/groww/api-key"
_TOTP_PATH = "/tickvault/prod/groww/totp-secret"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        _READONLY_ENV,
        "BRUTEX_TOKEN_AWS_REGION",
        "AWS_REGION",
        "BRUTEX_TOKEN_PARAM_API_KEY",
        "BRUTEX_TOKEN_PARAM_TOTP_SECRET",
        "BRUTEX_GROWW_API_KEY",
        "BRUTEX_GROWW_TOTP_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)


def _fake_with_params() -> FakeAwsClient:
    return FakeAwsClient(
        parameters={
            _API_KEY_PATH: "AWS-KEY",
            _TOTP_PATH: "AWS-SECRET",
        }
    )


class TestLoadGrowwAwsCredentials:
    """The reader returns None unless the read-only broker is enabled; both fields or nothing."""

    def test_unconfigured_returns_none(self) -> None:
        assert load_groww_aws_credentials() is None

    def test_broker_disabled_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A configured client but the broker OFF ⇒ None (byte-identical to today)."""
        monkeypatch.setenv(_READONLY_ENV, "0")
        assert load_groww_aws_credentials(client=_fake_with_params()) is None

    def test_full_pair_resolves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_READONLY_ENV, "true")
        creds = load_groww_aws_credentials(client=_fake_with_params())
        assert creds == GrowwFileCredentials(api_key="AWS-KEY", totp_secret="AWS-SECRET")

    def test_env_overridable_paths(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_READONLY_ENV, "true")
        monkeypatch.setenv("BRUTEX_TOKEN_PARAM_API_KEY", "/custom/key")
        monkeypatch.setenv("BRUTEX_TOKEN_PARAM_TOTP_SECRET", "/custom/totp")
        fake = FakeAwsClient(parameters={"/custom/key": "K2", "/custom/totp": "S2"})
        creds = load_groww_aws_credentials(client=fake)
        assert creds == GrowwFileCredentials(api_key="K2", totp_secret="S2")

    def test_missing_api_key_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_READONLY_ENV, "true")
        fake = FakeAwsClient(
            parameters={_TOTP_PATH: "S"},
            param_not_found=frozenset({_API_KEY_PATH}),
        )
        assert load_groww_aws_credentials(client=fake) is None

    def test_missing_totp_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_READONLY_ENV, "true")
        fake = FakeAwsClient(
            parameters={_API_KEY_PATH: "K"},
            param_not_found=frozenset({_TOTP_PATH}),
        )
        assert load_groww_aws_credentials(client=fake) is None

    def test_blank_value_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_READONLY_ENV, "true")
        fake = FakeAwsClient(parameters={_API_KEY_PATH: "   ", _TOTP_PATH: "S"})
        assert load_groww_aws_credentials(client=fake) is None

    def test_read_error_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_READONLY_ENV, "true")
        fake = FakeAwsClient(fail_get_parameter=True)
        assert load_groww_aws_credentials(client=fake) is None

    def test_absent_param_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A ParameterNotFound (both absent) ⇒ None, no raise."""
        monkeypatch.setenv(_READONLY_ENV, "true")
        fake = FakeAwsClient(param_not_found=frozenset({_API_KEY_PATH, _TOTP_PATH}))
        assert load_groww_aws_credentials(client=fake) is None

    def test_no_client_and_build_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Broker enabled but the real client build yields None (boto3 missing) ⇒ None."""
        monkeypatch.setenv(_READONLY_ENV, "true")
        monkeypatch.setattr("brutex.config.credentials_aws._build_client_or_none", lambda: None)
        assert load_groww_aws_credentials() is None


class TestResolveChain:
    """resolve_groww_file_credentials — env → AWS → file precedence."""

    def test_env_wins_over_aws(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """env api_key wins; the AWS tier is skipped since env also has totp (both set)."""
        monkeypatch.setenv("BRUTEX_GROWW_API_KEY", "ENV-KEY")
        monkeypatch.setenv("BRUTEX_GROWW_TOTP_SECRET", "ENV-SEC")
        creds = resolve_groww_file_credentials(
            aws_loader=lambda: GrowwFileCredentials(api_key="AWS-KEY", totp_secret="AWS-SEC")
        )
        assert creds == GrowwFileCredentials(api_key="ENV-KEY", totp_secret="ENV-SEC")

    def test_aws_skipped_when_env_has_both(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Short-circuit (hostile-review MINOR): env has both ⇒ the AWS loader NEVER runs."""
        monkeypatch.setenv("BRUTEX_GROWW_API_KEY", "ENV-KEY")
        monkeypatch.setenv("BRUTEX_GROWW_TOTP_SECRET", "ENV-SEC")

        def must_not_load() -> GrowwFileCredentials:
            raise AssertionError("AWS loader must not run when env satisfies both fields")

        creds = resolve_groww_file_credentials(aws_loader=must_not_load)
        assert creds == GrowwFileCredentials(api_key="ENV-KEY", totp_secret="ENV-SEC")

    def test_aws_wins_over_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from brutex.config.credentials import save_groww_credentials

        path = tmp_path / "creds.json"
        save_groww_credentials(
            GrowwFileCredentials(api_key="FILE-KEY", totp_secret="FILE-SEC"), path=path
        )
        creds = resolve_groww_file_credentials(
            path=path,
            aws_loader=lambda: GrowwFileCredentials(api_key="AWS-KEY", totp_secret="AWS-SEC"),
        )
        assert creds == GrowwFileCredentials(api_key="AWS-KEY", totp_secret="AWS-SEC")

    def test_file_used_when_aws_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from brutex.config.credentials import save_groww_credentials

        path = tmp_path / "creds.json"
        save_groww_credentials(
            GrowwFileCredentials(api_key="FILE-KEY", totp_secret="FILE-SEC"), path=path
        )
        creds = resolve_groww_file_credentials(path=path, aws_loader=lambda: None)
        assert creds == GrowwFileCredentials(api_key="FILE-KEY", totp_secret="FILE-SEC")

    def test_default_loader_used_when_none_passed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        """No aws_loader ⇒ the real (broker-disabled) loader ⇒ byte-identical env→file."""
        from brutex.config.credentials import save_groww_credentials

        path = tmp_path / "creds.json"
        save_groww_credentials(
            GrowwFileCredentials(api_key="FILE-KEY", totp_secret="FILE-SEC"), path=path
        )
        # No BRUTEX_TOKEN_BROKER_READONLY ⇒ real loader returns None ⇒ file used.
        creds = resolve_groww_file_credentials(path=path)
        assert creds.api_key == "FILE-KEY"

    def test_aws_backfills_missing_env_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """env api_key + AWS totp_secret ⇒ a mixed resolution per-field."""
        monkeypatch.setenv("BRUTEX_GROWW_API_KEY", "ENV-KEY")
        creds = resolve_groww_file_credentials(
            aws_loader=lambda: GrowwFileCredentials(api_key="AWS-KEY", totp_secret="AWS-SEC")
        )
        assert creds == GrowwFileCredentials(api_key="ENV-KEY", totp_secret="AWS-SEC")

    def test_nothing_available_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(CredentialsError):
            resolve_groww_file_credentials(
                path=tmp_path / "absent.json", env={}, aws_loader=lambda: None
            )
