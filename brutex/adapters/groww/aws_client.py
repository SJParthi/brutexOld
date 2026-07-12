"""Groww AWS-client shim — re-exports the COMMON seam (DEC-AWS-SSM-SEAM-COMMON-001).

The ``AwsClient`` read Protocol + the ``ParameterNotFound`` classifier + the real boto3
factory were MOVED verbatim to :mod:`brutex.adapters.aws_client` (MIGRATION.md #1754) so a
SECOND vendor (Dhan) can share them without importing the Groww package. This module stays a
thin, identity-preserving re-export so every existing ``from brutex.adapters.groww.aws_client
import …`` call site is byte-unchanged (identity-pinned by the aws_client test — the Groww
suites are the oracle). BruteX is READ-ONLY on the Groww namespace, so ONLY the read surface
is re-exported here — the write-capable ``AwsClientWriter`` is deliberately NOT re-exported
(the Groww Protocol's write-incapability is a standing security property).
"""

from __future__ import annotations

from brutex.adapters.aws_client import (
    SSM_PARAMETER_NOT_FOUND,
    AwsBackendUnavailable,
    AwsClient,
    build_real_aws_client,
)

__all__ = [
    "SSM_PARAMETER_NOT_FOUND",
    "AwsBackendUnavailable",
    "AwsClient",
    "build_real_aws_client",
]
