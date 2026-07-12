"""Tests for the Dhan auth constants (DEC-DHAN-AUTH-001).

Pins the verified endpoint values, the refresh-margin invariant, the renew kill switch, and
— critically — that no auth DECISION branch imports the ``_UNVERIFIED`` sentinels (AST pin).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from brutex.adapters.dhan import constants as c


def test_endpoints() -> None:
    assert c.DHAN_API_BASE == "https://api.dhan.co/v2/"
    assert c.DHAN_AUTH_MINT_URL == "https://auth.dhan.co/app/generateAccessToken"
    assert c.DHAN_RENEW_PATH == "RenewToken"
    assert c.DHAN_TOKEN_HEADER == "access-token"
    assert c.DHAN_CLIENT_ID_HEADER == "dhanClientId"


def test_lifetime_and_margin() -> None:
    assert c.DHAN_TOKEN_LIFETIME_S == 86_400
    # The refresh policy must fire BEFORE the lifetime ends (a real margin exists).
    assert c.DHAN_TOKEN_REFRESH_AGE_S < c.DHAN_TOKEN_LIFETIME_S
    assert c.DHAN_TOKEN_LIFETIME_S - c.DHAN_TOKEN_REFRESH_AGE_S >= 3_600


def test_retry_knobs() -> None:
    assert c.DHAN_TOKEN_ISSUANCE_RETRY_ATTEMPTS >= 1
    assert 0 < c.DHAN_TOKEN_ISSUANCE_RETRY_BASE_S <= c.DHAN_TOKEN_ISSUANCE_RETRY_CAP_S
    assert c.DHAN_MINT_LOCKDOWN_THRESHOLD >= 1
    assert c.DHAN_AUTH_HTTP_TIMEOUT_S > 0


def test_unverified_sentinels_present_and_honest() -> None:
    assert c.DHAN_MINT_PRECONDITIONS_UNVERIFIED is None
    assert c.DHAN_AUTH_RATE_LIMIT_UNVERIFIED is None
    assert c.DHAN_RENEW_REQUIRES_ACTIVE_TOKEN_UNVERIFIED is True
    assert c.DHAN_MINT_INVALIDATES_PRIOR_TOKEN_UNVERIFIED is None


class TestRenewKillSwitch:
    def test_default_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_DHAN_RENEW", raising=False)
        assert c.resolve_dhan_renew_enabled() is True

    def test_blank_on(self) -> None:
        assert c.resolve_dhan_renew_enabled("   ") is True

    @pytest.mark.parametrize("raw", ["1", "true", "YES", " On "])
    def test_truthy(self, raw: str) -> None:
        assert c.resolve_dhan_renew_enabled(raw) is True

    @pytest.mark.parametrize("raw", ["0", "false", "NO", " off "])
    def test_falsy(self, raw: str) -> None:
        assert c.resolve_dhan_renew_enabled(raw) is False

    def test_env_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_DHAN_RENEW", "off")
        assert c.resolve_dhan_renew_enabled() is False

    def test_junk_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="invalid BRUTEX_DHAN_RENEW"):
            c.resolve_dhan_renew_enabled("maybe")


def test_no_decision_branch_imports_unverified_sentinels() -> None:
    """AST pin: no auth-logic module imports a ``*_UNVERIFIED`` name (they are observe-only).

    The sentinels record what Phase-0 could not confirm; letting one gate a mint/renew branch
    would silently rely on an unverified assumption (Golden Rule #1).
    """
    unverified = {
        "DHAN_MINT_PRECONDITIONS_UNVERIFIED",
        "DHAN_AUTH_RATE_LIMIT_UNVERIFIED",
        "DHAN_RENEW_REQUIRES_ACTIVE_TOKEN_UNVERIFIED",
        "DHAN_MINT_INVALIDATES_PRIOR_TOKEN_UNVERIFIED",
    }
    pkg = Path(c.__file__).parent
    logic_modules = ["auth.py", "reauth.py", "token_store_aws.py", "token_lifecycle.py"]
    for name in logic_modules:
        tree = ast.parse((pkg / name).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imported.add(alias.name)
        leaked = imported & unverified
        assert not leaked, f"{name} imports UNVERIFIED sentinel(s) into decision logic: {leaked}"


def test_reauth_does_not_import_market_hours() -> None:
    """AST pin: the reauth spine must NOT IMPORT calendars/market_hours (auth is exempt).

    Checks actual import statements (the docstring legitimately NAMES the rule), so a real
    ``import`` of the market-hours guard fails while the prose mention does not.
    """
    pkg = Path(c.__file__).parent
    tree = ast.parse((pkg / "reauth.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert "market_hours" not in (node.module or ""), "reauth imports market_hours"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert "market_hours" not in alias.name, "reauth imports market_hours"
