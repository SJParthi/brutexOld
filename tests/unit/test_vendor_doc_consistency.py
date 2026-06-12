"""Vendor single-source-of-truth guard (DEC-DHAN-DOC-RECONCILE-001).

Vendor-state guard, updated for the Dhan → Groww re-migration
(DEC-DHAN-GROWW-REMIGRATE-001, ADR-0009). The `groww` optional extra is now
RE-ADDED (PR-B) because the vendor SDK returns; but the OPERATOR-FACING auth
docs still describe Dhan until the cutover PR, and ``bootstrap.sh`` still must
NOT install the optional extra (installing it is what broke ``make setup``
historically). So these assertions now check:

* the ``groww`` pip extra IS present and declares ``growwapi`` (re-migration), AND
* ``bootstrap.sh`` does NOT pip-install it (it stays optional — unchanged), AND
* the operator-facing docs still carry the live Dhan auth env vars and do NOT
  yet carry ``BRUTEX_GROWW_*`` auth vars (those land at cutover, not now).

Scope is deliberately narrow (the *breaking* surfaces only): the pre-Dhan
``brutex.orchestrator.adaptive_rate_limiter`` still reads legacy
``BRUTEX_GROWW_*RPS`` rate vars, which ``ENV_VARS.md`` documents honestly
as tracked residue -- so this guard checks the *auth* vars, not every
historical mention.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Operator-facing docs whose AUTH instructions must be Dhan, never Groww.
_AUTH_DOCS: tuple[Path, ...] = (
    _REPO_ROOT / "README.md",
    _REPO_ROOT / "docs" / "runbooks" / "ENV_VARS.md",
    _REPO_ROOT / "docs" / "runbooks" / "data-fetch-runday.md",
)

#: The Groww auth env vars that must never reappear in operator docs.
_WRONG_AUTH_VARS: tuple[str, ...] = (
    "BRUTEX_GROWW_API_KEY",
    "BRUTEX_GROWW_TOTP_SECRET",
    "BRUTEX_GROWW_ACCESS_TOKEN",
)

#: The correct Dhan auth env vars (CLAUDE.md section 6).
_DHAN_AUTH_VARS: tuple[str, ...] = (
    "BRUTEX_DHAN_ACCESS_TOKEN",
    "BRUTEX_DHAN_CLIENT_ID",
)

#: A pip extra spec that contains a ``groww`` token, e.g. ``.[dev,data,ui,groww]``.
_GROWW_EXTRA_RE = re.compile(r"\.\[[^\]]*groww[^\]]*\]")


def test_pyproject_has_groww_extra() -> None:
    """The ``groww`` optional extra is RE-ADDED for the re-migration.

    DEC-DHAN-GROWW-REMIGRATE-001 (ADR-0009) reverses the DEC-DHAN-FINALIZE-001
    removal: the vendor SDK returns, so the extra must exist and declare
    ``growwapi``. It stays OPTIONAL (``bootstrap.sh`` must not install it —
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


def test_operator_docs_have_no_groww_auth_vars() -> None:
    """No operator-facing doc may instruct the wrong ``BRUTEX_GROWW_*`` auth vars."""
    for doc in _AUTH_DOCS:
        text = doc.read_text(encoding="utf-8")
        for var in _WRONG_AUTH_VARS:
            assert var not in text, f"{doc.name} still references stale auth var {var}"


def test_auth_docs_carry_dhan_credentials() -> None:
    """README + ENV_VARS must document the correct Dhan auth env vars (CLAUDE.md section 6)."""
    for doc in (_REPO_ROOT / "README.md", _REPO_ROOT / "docs" / "runbooks" / "ENV_VARS.md"):
        text = doc.read_text(encoding="utf-8")
        for var in _DHAN_AUTH_VARS:
            assert var in text, f"{doc.name} is missing the Dhan auth var {var}"
