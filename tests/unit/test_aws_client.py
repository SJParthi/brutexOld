"""Unit tests for the AWS-client Protocol seam (DEC-TOKEN-BROKER-AWS-001).

Covers the dependency-free surface: the :class:`AwsClient` Protocol stub, the
:class:`AwsBackendUnavailable` error, the classifier-name constant, and that
``FakeAwsClient`` structurally satisfies the Protocol (drives everything else). The real
boto3 factory + adapter are ``# pragma: no cover`` (need boto3 + a live account).
"""

from __future__ import annotations

import pytest
from brutex.adapters.groww.aws_client import (
    SSM_PARAMETER_NOT_FOUND,
    AwsBackendUnavailable,
    AwsClient,
)

from tests.fakes.fake_aws_client import FakeAwsClient


def test_backend_unavailable_is_import_error() -> None:
    assert issubclass(AwsBackendUnavailable, ImportError)


def test_classifier_name_constant() -> None:
    assert SSM_PARAMETER_NOT_FOUND == "ParameterNotFound"


def test_fake_satisfies_protocol() -> None:
    assert isinstance(FakeAwsClient(), AwsClient)


class TestProtocolStubs:
    """The structural Protocol stub is never executed; call it to pin the contract.

    ``@runtime_checkable`` Protocol method BODIES are real code lines — exercise the
    ``raise NotImplementedError`` so the module reaches 100% (mirrors how the growwapi
    Protocol stubs are covered).
    """

    def test_stub_raises_not_implemented(self) -> None:
        stub = AwsClient
        with pytest.raises(NotImplementedError):
            AwsClient.get_parameter(stub, name="n", with_decryption=True)  # type: ignore[arg-type]


# ── COMMON seam (DEC-AWS-SSM-SEAM-COMMON-001) ────────────────────────────────
from brutex.adapters import aws_client as _common  # noqa: E402  # grouped after the oracle block
from brutex.adapters.groww import aws_client as _groww_shim  # noqa: E402
from brutex.adapters.groww import constants as _groww_constants  # noqa: E402


class TestCommonSeam:
    """The COMMON seam + the Groww re-export shim identity pins."""

    def test_region_constants(self) -> None:
        assert _common.TOKEN_BROKER_REGION_ENV == "BRUTEX_TOKEN_AWS_REGION"
        assert _common.AWS_REGION_ENV == "AWS_REGION"
        assert _common.TOKEN_BROKER_REGION_DEFAULT == "ap-south-1"

    def test_groww_shim_identity(self) -> None:
        assert _groww_shim.AwsClient is _common.AwsClient
        assert _groww_shim.build_real_aws_client is _common.build_real_aws_client
        assert _groww_shim.SSM_PARAMETER_NOT_FOUND is _common.SSM_PARAMETER_NOT_FOUND
        # The read-only Groww shim must NOT expose the write Protocol.
        assert not hasattr(_groww_shim, "AwsClientWriter")

    def test_resolver_reexport_identity(self) -> None:
        assert _groww_constants.resolve_token_broker_region is _common.resolve_token_broker_region
        assert _groww_constants.resolve_token_param_path is _common.resolve_token_param_path

    def test_resolve_region_prefers_brutex_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_TOKEN_AWS_REGION", " us-east-1 ")
        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        assert _common.resolve_token_broker_region() == "us-east-1"

    def test_resolve_region_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_TOKEN_AWS_REGION", "  ")
        monkeypatch.setenv("AWS_REGION", " eu-west-1 ")
        assert _common.resolve_token_broker_region() == "eu-west-1"

    def test_resolve_region_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_TOKEN_AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_REGION", raising=False)
        assert _common.resolve_token_broker_region() == "ap-south-1"

    def test_resolve_param_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOME_ENV", " /custom ")
        assert _common.resolve_token_param_path("SOME_ENV", "/default") == "/custom"

    def test_resolve_param_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SOME_ENV", raising=False)
        assert _common.resolve_token_param_path("SOME_ENV", "/default") == "/default"

    def test_resolve_param_blank_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOME_ENV", "   ")
        assert _common.resolve_token_param_path("SOME_ENV", "/default") == "/default"

    def test_writer_protocol_structural(self) -> None:
        class _Reader:
            def get_parameter(self, *, name: str, with_decryption: bool) -> str | None:
                return None

        class _Writer(_Reader):
            def put_parameter(
                self, *, name: str, value: str, secure: bool = True, overwrite: bool = True
            ) -> None:
                return None

        assert isinstance(_Writer(), _common.AwsClientWriter)
        assert isinstance(_Writer(), _common.AwsClient)
        assert not isinstance(_Reader(), _common.AwsClientWriter)

    def test_writer_stub_raises(self) -> None:
        stub: object = object()
        put = _common.AwsClientWriter.put_parameter
        with pytest.raises(NotImplementedError):
            put(stub, name="x", value="y", secure=True, overwrite=True)  # type: ignore[arg-type]
