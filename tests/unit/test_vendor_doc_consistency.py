"""Vendor single-source-of-truth guard (DEC-DHAN-DOC-RECONCILE-001).

Vendor-state guard, updated for the Dhan -> Groww re-migration
(DEC-DHAN-GROWW-REMIGRATE-001, ADR-0009). The vendor cutover is COMPLETE: the
operator-facing docs now describe the live Groww TOTP auth, and the retired
vendor's auth env vars have been scrubbed from every operator surface. The
`groww` optional extra IS present and declares `growwapi`; `bootstrap.sh` still
must NOT install the optional extra (installing it is what broke `make setup`
historically). So these assertions check:

* the ``groww`` pip extra IS present and declares ``growwapi`` (re-migration), AND
* ``bootstrap.sh`` does NOT pip-install it (it stays optional -- unchanged), AND
* the operator-facing docs carry the live ``BRUTEX_GROWW_*`` auth vars and do
  NOT carry any retired ``BRUTEX_DHAN_*`` auth var.

Scope is deliberately narrow (the *breaking* surfaces only): the legacy
``brutex.orchestrator.adaptive_rate_limiter`` reads vendor-neutral
``BRUTEX_RATE_*RPS`` rate vars, which ``ENV_VARS.md`` documents honestly as
tracked residue -- so this guard checks the *auth* vars, not every historical
mention.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Operator-facing docs whose AUTH instructions must be Groww, never Dhan.
_AUTH_DOCS: tuple[Path, ...] = (
    _REPO_ROOT / "README.md",
    _REPO_ROOT / "docs" / "runbooks" / "ENV_VARS.md",
    _REPO_ROOT / "docs" / "runbooks" / "data-fetch-runday.md",
)

#: The retired Dhan auth env vars that must never reappear in operator docs.
_WRONG_AUTH_VARS: tuple[str, ...] = (
    "BRUTEX_DHAN_ACCESS_TOKEN",
    "BRUTEX_DHAN_CLIENT_ID",
)

#: The correct live Groww auth env vars (CLAUDE.md section 6).
_GROWW_AUTH_VARS: tuple[str, ...] = (
    "BRUTEX_GROWW_API_KEY",
    "BRUTEX_GROWW_TOTP_SECRET",
)

#: A pip extra spec that contains a ``groww`` token, e.g. ``.[dev,data,ui,groww]``.
_GROWW_EXTRA_RE = re.compile(r"\.\[[^\]]*groww[^\]]*\]")


def test_pyproject_has_groww_extra() -> None:
    """The ``groww`` optional extra is RE-ADDED for the re-migration.

    DEC-DHAN-GROWW-REMIGRATE-001 (ADR-0009) reverses the DEC-DHAN-FINALIZE-001
    removal: the vendor SDK returns, so the extra must exist and declare
    ``growwapi``. It stays OPTIONAL (``bootstrap.sh`` must not install it --
    asserted by :func:`test_bootstrap_does_not_install_groww_extra`).
    """
    raw = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    data = tomllib.loads(raw)
    extras = data["project"]["optional-dependencies"]
    assert "groww" in extras, f"groww extra missing from pyproject: {sorted(extras)}"
    assert any("growwapi" in dep for dep in extras["groww"]), (
        f"groww extra must declare growwapi: {extras['groww']}"
    )


def test_bootstrap_does_not_install_groww_extra() -> None:
    """``bootstrap.sh`` must not pip-install a removed groww extra (it breaks ``make setup``)."""
    text = (_REPO_ROOT / "scripts" / "setup" / "bootstrap.sh").read_text(encoding="utf-8")
    hits = _GROWW_EXTRA_RE.findall(text)
    assert not hits, f"bootstrap.sh installs a non-existent groww extra: {hits}"


def test_operator_docs_have_no_retired_dhan_auth_vars() -> None:
    """No operator-facing doc may instruct a retired ``BRUTEX_DHAN_*`` auth var."""
    for doc in _AUTH_DOCS:
        text = doc.read_text(encoding="utf-8")
        for var in _WRONG_AUTH_VARS:
            assert var not in text, f"{doc.name} still references retired auth var {var}"


def test_auth_docs_carry_groww_credentials() -> None:
    """README + ENV_VARS must document the live Groww auth env vars (CLAUDE.md section 6)."""
    for doc in (_REPO_ROOT / "README.md", _REPO_ROOT / "docs" / "runbooks" / "ENV_VARS.md"):
        text = doc.read_text(encoding="utf-8")
        for var in _GROWW_AUTH_VARS:
            assert var in text, f"{doc.name} is missing the Groww auth var {var}"
