"""Integration: the AWS-SSM + secret-scrub move-with-shim is Groww byte-identical.

DEC-AWS-SSM-SEAM-COMMON-001 (MIGRATION.md #1754). Commit 1 MOVED ``AwsClient`` + the region /
param resolvers + ``scrub_secrets`` to the COMMON ``brutex.adapters`` home, leaving the Groww
modules as thin re-export / delegating shims. This pins the move to be behaviour-preserving:

* the Groww re-exports ARE the moved objects (``is`` identity — not a re-implementation),
* the Groww ``_sanitize_reason`` produces byte-identical output to the COMMON scrubber,
* the read-only Groww ``AwsClient`` Protocol NEVER gains a write method (the standing security
  property — a Groww-side store is structurally unable to write the shared token).

The FULL Groww auth / credential / reauth test suites are the behavioural oracle; they pass
UNMODIFIED under the shims (run in the CI gate). These assertions are the permanent
regression that the shims stay identity-preserving.
"""

from __future__ import annotations

from brutex.adapters import aws_client as _common
from brutex.adapters import secret_scrub as _scrub
from brutex.adapters.groww import auth as _groww_auth
from brutex.adapters.groww import aws_client as _groww_aws
from brutex.adapters.groww import constants as _groww_constants


class TestReExportsAreTheMovedObjects:
    """The Groww shims re-export the EXACT COMMON objects (``is`` — never a copy)."""

    def test_groww_aws_client_reexports_are_identity(self) -> None:
        assert _groww_aws.AwsClient is _common.AwsClient
        assert _groww_aws.AwsBackendUnavailable is _common.AwsBackendUnavailable
        assert _groww_aws.build_real_aws_client is _common.build_real_aws_client
        assert _groww_aws.SSM_PARAMETER_NOT_FOUND is _common.SSM_PARAMETER_NOT_FOUND

    def test_groww_constants_resolver_reexports_are_identity(self) -> None:
        assert _groww_constants.resolve_token_broker_region is _common.resolve_token_broker_region
        assert _groww_constants.resolve_token_param_path is _common.resolve_token_param_path

    def test_groww_auth_uses_the_common_scrubber(self) -> None:
        # ``vars()`` reads the imported name directly (it is an implementation import, not an
        # exported symbol) so the ``is`` identity holds without a re-export.
        assert vars(_groww_auth)["scrub_secrets"] is _scrub.scrub_secrets


class TestScrubIsByteIdentical:
    """``groww.auth._sanitize_reason`` delegates to the COMMON scrubber, byte-for-byte."""

    def test_sanitize_reason_equals_common_scrub(self) -> None:
        secrets = ("api-key-abc", "totp-secret-xyz", "123456")
        text = "auth failed: api-key-abc used with totp-secret-xyz code 123456"
        via_groww = _groww_auth._sanitize_reason(text, *secrets)
        via_common = _scrub.scrub_secrets(text, secrets)
        assert via_groww == via_common
        for secret in secrets:
            assert secret not in via_groww

    def test_blank_secret_is_skipped_both_ways(self) -> None:
        # The empty-secret guard must survive the move (a bare "" would corrupt the whole string).
        assert _groww_auth._sanitize_reason("hello", "") == "hello"
        assert _scrub.scrub_secrets("hello", ("",)) == "hello"


class TestReadOnlyProtocolHasNoWrite:
    """The Groww read Protocol MUST NOT gain a write method (security property)."""

    def test_aws_client_has_no_put_parameter(self) -> None:
        assert not hasattr(_common.AwsClient, "put_parameter")
        assert "put_parameter" not in dir(_common.AwsClient)

    def test_groww_shim_does_not_expose_the_writer_protocol(self) -> None:
        # The write-capable Protocol is NOT re-exported through the read-only Groww shim.
        assert not hasattr(_groww_aws, "AwsClientWriter")
        assert not hasattr(_groww_aws, "build_real_aws_writer_client")

    def test_writer_protocol_extends_reader_with_exactly_put_parameter(self) -> None:
        # The write grant is expressed ONLY on the wider AwsClientWriter (Dhan self-mint).
        assert hasattr(_common.AwsClientWriter, "put_parameter")
        assert hasattr(_common.AwsClientWriter, "get_parameter")
        assert issubclass(_common.AwsClientWriter, _common.AwsClient)
