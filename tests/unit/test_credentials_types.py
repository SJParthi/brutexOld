"""Leaf credential types (DEC-AWS-ONLY-CREDS-001).

The dependency-free leaf :mod:`brutex.config.credentials_types` defines the two symbols
shared by :mod:`credentials` (the resolver) + :mod:`credentials_aws` (the AWS reader) —
:class:`CredentialsError` + :class:`GrowwFileCredentials` — so the two importers form a
one-directional DAG (no module-level cycle; the earlier bidirectional import was flagged by
CodeQL). This suite drives the leaf to 100% line+branch AND pins the no-cycle contract.
"""

from __future__ import annotations

import dataclasses
import importlib
import sys

import pytest
from brutex.config.credentials_types import CredentialsError, GrowwFileCredentials


class TestCredentialsError:
    def test_is_an_exception(self) -> None:
        assert issubclass(CredentialsError, Exception)

    def test_carries_its_message(self) -> None:
        err = CredentialsError("missing api-key")
        assert str(err) == "missing api-key"

    def test_is_raisable(self) -> None:
        with pytest.raises(CredentialsError, match="boom"):
            raise CredentialsError("boom")


class TestGrowwFileCredentials:
    def test_holds_both_fields(self) -> None:
        creds = GrowwFileCredentials(api_key="K", totp_secret="S")
        assert creds.api_key == "K"
        assert creds.totp_secret == "S"

    def test_is_frozen(self) -> None:
        creds = GrowwFileCredentials(api_key="K", totp_secret="S")
        with pytest.raises(dataclasses.FrozenInstanceError):
            creds.api_key = "X"  # type: ignore[misc]  # frozen dataclass — must raise

    def test_equality_by_value(self) -> None:
        assert GrowwFileCredentials(api_key="K", totp_secret="S") == GrowwFileCredentials(
            api_key="K", totp_secret="S"
        )


class TestNoImportCycle:
    def test_leaf_imports_neither_credentials_module(self) -> None:
        """Importing the leaf fresh must NOT pull in credentials / credentials_aws.

        HERMETIC: the popped ``sys.modules`` entries are SAVED + RESTORED in a ``finally`` so
        this test never leaves a re-imported / half-imported module behind for a later test
        (which would break a monkeypatch that targets the ORIGINAL module object).
        """
        names = (
            "brutex.config.credentials",
            "brutex.config.credentials_aws",
            "brutex.config.credentials_types",
        )
        saved = {name: sys.modules.get(name) for name in names}
        try:
            for name in names:
                sys.modules.pop(name, None)
            # importlib.import_module (a function call, not a duplicate `import` statement —
            # so the module is imported ONE way only, no CodeQL "import + import from") loads
            # the leaf FRESH; it must not drag in either credentials module.
            importlib.import_module("brutex.config.credentials_types")

            assert "brutex.config.credentials" not in sys.modules
            assert "brutex.config.credentials_aws" not in sys.modules
        finally:
            # Restore the ORIGINAL module objects so no later test sees a re-imported twin.
            for name, module in saved.items():
                if module is not None:
                    sys.modules[name] = module
                else:
                    sys.modules.pop(name, None)
