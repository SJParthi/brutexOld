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
