"""The suite-wide AWS hermeticity belt (DEC-TEST-AWS-HERMETICITY-001).

Pins that the ``tests/conftest.py`` autouse belt is ACTIVE (a direct attempt to
build the real AWS client raises the loud belt error), that the error names the
remediation, that every from-import binding of the real-client factory is
covered (a test can never reach a live ``ssm:GetParameter`` through the Groww
credential / token chains), that the belt is not accidentally skippable
(source-pinned ``autouse=True``), and that deliberate fakes — the
constructor-injected ``AwsClient`` seam every existing AWS test uses — keep
working untouched. No secret values appear anywhere in this file; the belt is
about making the IN-PROCESS live path impossible, not about real credentials
(subprocess children + direct botocore use sit outside the in-process
monkeypatch — the disclosed residual, verified unreached by any current test).
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import brutex.adapters.groww.aws_client as aws_client_module
import brutex.adapters.groww.token_store_aws as token_store_aws_module
import brutex.config.credentials_aws as credentials_aws_module
import pytest
from brutex.config.credentials_types import CredentialsError

from tests.conftest import AWS_HERMETICITY_BELT_MESSAGE
from tests.fakes.fake_aws_client import FakeAwsClient

_CONFTEST = Path(__file__).resolve().parents[1] / "conftest.py"


class TestBeltActive:
    """(a) + (c): the belt is live for every test — the boundary raises loudly."""

    def test_defining_module_factory_is_blocked(self) -> None:
        """``aws_client.build_real_aws_client`` raises the belt error, never builds."""
        with pytest.raises(RuntimeError, match="AWS\\s+hermeticity belt"):
            aws_client_module.build_real_aws_client("ap-south-1")

    def test_credentials_aws_binding_is_blocked(self) -> None:
        """The ``credentials_aws`` from-import binding is covered too."""
        with pytest.raises(RuntimeError, match="hermeticity belt"):
            credentials_aws_module.build_real_aws_client("ap-south-1")

    def test_token_store_aws_binding_is_blocked(self) -> None:
        """The ``token_store_aws`` from-import binding is covered too."""
        with pytest.raises(RuntimeError, match="hermeticity belt"):
            token_store_aws_module.build_real_aws_client("ap-south-1")

    def test_credentials_chain_cannot_reach_live_aws(self) -> None:
        """The DEFAULT credential path degrades to the documented typed error.

        ``_build_client_or_raise`` maps ANY build failure to ``CredentialsError``
        (its fail-fast contract) — under the belt the failure is OUR loud belt
        error, so the chain provably never constructs a real SSM client. This is
        exactly the deterministic behaviour boto3-less CI always had; the belt
        makes it hold on EVERY box (boto3 + ambient IAM creds included).
        """
        with pytest.raises(CredentialsError) as excinfo:
            credentials_aws_module._build_client_or_raise()  # pyright: ignore[reportPrivateUsage]
        # The belt error is the chained cause — the live client was never built.
        assert isinstance(excinfo.value.__cause__, RuntimeError)
        assert "hermeticity belt" in str(excinfo.value.__cause__)

    def test_default_account_key_resolver_degrades_not_live(self) -> None:
        """``BackgroundPullJob``'s DEFAULT account-key resolver (the path the two
        ``test_dashboard_results_route.py`` sweep tests reached unmocked) now
        provably cannot perform a live ``ssm:GetParameter``: it takes the
        graceful ``CredentialsError -> None`` skip instead.
        """
        pytest.importorskip("polars", reason="pull_jobs imports the data lake surface")
        from brutex.dashboard.pull_jobs import (
            _default_account_key_resolver,  # pyright: ignore[reportPrivateUsage]
        )

        assert _default_account_key_resolver() is None

    def test_boto3_boundary_is_blocked_when_installed(self) -> None:
        """When boto3 IS present (an operator box), its client factory is patched."""
        if importlib.util.find_spec("boto3") is None:
            pytest.skip("boto3 not installed — the factory-level pins above are the belt here")
        import boto3  # type: ignore[import-not-found, unused-ignore]

        with pytest.raises(RuntimeError, match="hermeticity belt"):
            boto3.client("ssm", region_name="ap-south-1")


class TestBeltMessage:
    """(b): the error names the remediation, never a secret."""

    def test_message_names_the_belt_and_the_remediation(self) -> None:
        assert "blocked by the tests/conftest.py AWS" in AWS_HERMETICITY_BELT_MESSAGE
        assert "hermeticity belt" in AWS_HERMETICITY_BELT_MESSAGE
        assert "inject a fake" in AWS_HERMETICITY_BELT_MESSAGE
        assert "fake_aws_client" in AWS_HERMETICITY_BELT_MESSAGE

    def test_raised_error_carries_the_message_verbatim(self) -> None:
        with pytest.raises(RuntimeError) as excinfo:
            aws_client_module.build_real_aws_client("ap-south-1")
        assert str(excinfo.value) == AWS_HERMETICITY_BELT_MESSAGE


class TestBeltNotSkippable:
    """(c): the belt lives in tests/conftest.py as an AUTOUSE fixture (source pin)."""

    def test_conftest_defines_the_autouse_belt(self) -> None:
        """Deleting ``autouse=True`` (or the fixture) fails this pin loudly."""
        source = _CONFTEST.read_text(encoding="utf-8")
        match = re.search(
            r"@pytest\.fixture\(autouse=True\)\s*\ndef _aws_hermeticity_belt\(",
            source,
        )
        assert match is not None, (
            "tests/conftest.py must define `_aws_hermeticity_belt` as an "
            "@pytest.fixture(autouse=True) — the suite-wide AWS hermeticity belt "
            "(DEC-TEST-AWS-HERMETICITY-001) must never be opt-in"
        )

    def test_belt_patches_every_from_import_binding(self) -> None:
        """Each live binding IS the belt stand-in (hostile-review MINOR-2 teeth).

        The pre-fold identity check (`a is b is c`) was tautological — the
        from-import bindings share one function object even with the belt OFF.
        The tooth is the identity against the STAND-IN itself: with the belt
        off (or one binding missed), that binding would still be the real
        factory and this pin fails naming it.
        """
        from tests.conftest import (
            _aws_belt_blocked,  # pyright: ignore[reportPrivateUsage]  # house private-import test pattern
        )

        for label, binding in (
            ("aws_client", aws_client_module.build_real_aws_client),
            ("credentials_aws", credentials_aws_module.build_real_aws_client),
            ("token_store_aws", token_store_aws_module.build_real_aws_client),
        ):
            assert binding is _aws_belt_blocked, (
                f"the {label} binding of build_real_aws_client is NOT the belt "
                f"stand-in — the AWS hermeticity belt missed it"
            )


class TestDeliberateFakesStillWork:
    """Constructor-injected fakes bypass the belt BY DESIGN (no real client built)."""

    def test_injected_fake_client_flows_through_the_real_loader(self) -> None:
        """The established ``client=`` seam works under the belt — real code, fake AWS."""
        fake = FakeAwsClient(
            parameters={
                "/tickvault/prod/groww/api-key": "fake-api-key",
                "/tickvault/prod/groww/totp-secret": "fake-totp-secret",
            }
        )
        creds = credentials_aws_module.load_groww_aws_credentials(client=fake)
        assert creds.api_key == "fake-api-key"
        assert creds.totp_secret == "fake-totp-secret"

    def test_test_level_monkeypatch_overrides_the_belt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A test that deliberately re-patches the factory wins over the belt."""
        fake = FakeAwsClient(parameters={})
        monkeypatch.setattr(aws_client_module, "build_real_aws_client", lambda region: fake)
        assert aws_client_module.build_real_aws_client("ap-south-1") is fake
