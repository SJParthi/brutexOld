"""Unit tests for the live-module kit helpers (G2 / no-mint guard seams).

DEC-DOCTOR-LIVE-PROOF-KIT-001. Stem-matched to
``brutex/adapters/groww/live.py`` so the strict per-module coverage discovery
gate scopes these tests to the module they cover (moved out of the combined
``test_doctor_live_seams.py``, whose stem matched no touched module). The
``http_timeout`` shape-probe alias pin rides along (it guards the same G2
SDK-shape stage).
"""

# Whitebox pin of the private alias source — sanctioned.
# pyright: reportPrivateUsage=none

from __future__ import annotations

import pytest
from brutex.adapters.groww.http_timeout import _REQUEST_METHOD_NAMES, REQUEST_METHOD_NAMES
from brutex.adapters.groww.live import (
    KitMintBlockedError,
    installed_growwapi_version,
    mint_blocked_token_factory,
    mint_blocked_totp_minter,
)


class TestLiveKitHelpers:
    def test_mint_blocked_token_factory_refuses(self) -> None:
        with pytest.raises(KitMintBlockedError, match="never mints a Groww token"):
            mint_blocked_token_factory("key", "123456")

    def test_mint_blocked_totp_minter_refuses(self) -> None:
        with pytest.raises(KitMintBlockedError, match="never mints a TOTP code"):
            mint_blocked_totp_minter("secret")

    def test_installed_growwapi_version_absent_is_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # DETERMINISTIC absence: the helper must map PackageNotFoundError → None.
        # (`growwapi` is a MAIN dependency — pyproject `growwapi>=1.0,<2.0` — so a
        # standard install HAS it; the prior env-dependent bare assert failed on
        # any such env. Simulate the absent-distribution case explicitly instead.)
        import importlib.metadata

        def _absent(_name: str) -> str:
            raise importlib.metadata.PackageNotFoundError("growwapi")

        monkeypatch.setattr(importlib.metadata, "version", _absent)
        assert installed_growwapi_version() is None

    def test_installed_growwapi_version_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib.metadata

        monkeypatch.setattr(importlib.metadata, "version", lambda _name: "1.5.2")
        assert installed_growwapi_version() == "1.5.2"

    def test_request_method_alias_matches_private_source(self) -> None:
        assert REQUEST_METHOD_NAMES == _REQUEST_METHOD_NAMES
        assert REQUEST_METHOD_NAMES == ("_request_get", "_request_post", "_request_put")
