"""Vendor single-source-of-truth guard (DEC-DHAN-DOC-RECONCILE-001).

Vendor-state guard, updated for the Dhan -> Groww re-migration
(DEC-DHAN-GROWW-REMIGRATE-001, ADR-0009), then the AWS-SSM-only credential
cutover (DEC-AWS-ONLY-CREDS-001, MIGRATION.md #1065), and again for the Dhan
RE-ADMISSION as a SECOND data source (DEC-DHAN-AUTH-001; operator decision
2026-07-12, ADR-0020 superseding ADR-0009 §5). Groww stays the primary vendor;
Dhan returns as an AUTH adapter whose credentials — like Groww's — live ONLY in
AWS Parameter Store SecureStrings (``/tickvault/prod/dhan/*``), reached via the
``BRUTEX_TOKEN_PARAM_DHAN_*`` path-OVERRIDE env vars. So a DIRECT-secret Dhan
env var (``BRUTEX_DHAN_ACCESS_TOKEN`` / ``BRUTEX_DHAN_CLIENT_ID``) must still
NEVER appear in an operator doc — not because Dhan is retired, but because Dhan
creds are AWS-SSM-only exactly like Groww's (mirrors ``_RETIRED_GROWW_AUTH_VARS``
below). The ``BRUTEX_TOKEN_PARAM_DHAN_*`` path overrides do NOT collide with the
banned ``BRUTEX_DHAN_*`` names (different prefix), so they are allowed.

The `groww` optional extra IS present and declares `growwapi`; `bootstrap.sh`
still must NOT install the optional extra (installing it is what broke
`make setup` historically). So these assertions check:

* the ``groww`` pip extra IS present and declares ``growwapi`` (re-migration), AND
* ``bootstrap.sh`` does NOT pip-install it (it stays optional -- unchanged), AND
* the operator-facing docs describe the AWS-SSM-only credential flow
  (DEC-AWS-ONLY-CREDS-001) and carry NO instruction to set the retired
  ``BRUTEX_GROWW_*`` auth env vars (nor any ``BRUTEX_DHAN_*``).

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

#: Operator-facing docs whose AUTH instructions must be AWS-SSM-only, never a
#: retired secret env var (Dhan OR the pre-AWS Groww pair).
_AUTH_DOCS: tuple[Path, ...] = (
    _REPO_ROOT / "README.md",
    _REPO_ROOT / "docs" / "runbooks" / "ENV_VARS.md",
    _REPO_ROOT / "docs" / "runbooks" / "data-fetch-runday.md",
    _REPO_ROOT / "docs" / "runbooks" / "critical-alerts.md",
    _REPO_ROOT / "docs" / "runbooks" / "lake-backup-restore.md",
)

#: DIRECT-secret Dhan auth env vars that must never appear in operator docs —
#: Dhan creds are AWS-SSM-only (DEC-DHAN-AUTH-001), read via the SSM-path
#: overrides ``BRUTEX_TOKEN_PARAM_DHAN_*``, NEVER a direct token/client-id env
#: var. These banned ``BRUTEX_DHAN_*`` names do NOT collide with the allowed
#: ``BRUTEX_TOKEN_PARAM_DHAN_*`` path overrides (different prefix).
_WRONG_AUTH_VARS: tuple[str, ...] = (
    "BRUTEX_DHAN_ACCESS_TOKEN",
    "BRUTEX_DHAN_CLIENT_ID",
)

#: The RETIRED Groww auth env vars — creds are AWS-SSM-only (DEC-AWS-ONLY-CREDS-001).
_RETIRED_GROWW_AUTH_VARS: tuple[str, ...] = (
    "BRUTEX_GROWW_API_KEY",
    "BRUTEX_GROWW_TOTP_SECRET",
)

#: The live AWS credential-flow markers (brutex/adapters/groww/constants.py:355-370).
_AWS_CONTRACT_MARKERS: tuple[str, ...] = (
    "BRUTEX_TOKEN_PARAM_API_KEY",
    "BRUTEX_TOKEN_PARAM_TOTP_SECRET",
    "BRUTEX_TOKEN_PARAM_ACCESS_TOKEN",
    "/tickvault/prod/groww/api-key",
    "/tickvault/prod/groww/totp-secret",
    "/tickvault/prod/groww/access-token",
    "BRUTEX_TOKEN_AWS_REGION",
    "DEC-AWS-ONLY-CREDS-001",
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


def test_operator_docs_have_no_direct_secret_dhan_auth_vars() -> None:
    """No operator-facing doc may instruct a DIRECT-secret ``BRUTEX_DHAN_*`` auth var.

    Dhan is re-admitted (DEC-DHAN-AUTH-001) with creds AWS-SSM-only, so a direct
    token/client-id env var must never appear (the SSM-path ``BRUTEX_TOKEN_PARAM_DHAN_*``
    overrides are the only sanctioned Dhan env vars — different prefix, not banned).
    """
    for doc in _AUTH_DOCS:
        text = doc.read_text(encoding="utf-8")
        for var in _WRONG_AUTH_VARS:
            assert var not in text, f"{doc.name} still references retired auth var {var}"


def test_auth_docs_carry_aws_credential_flow() -> None:
    """README + ENV_VARS must document the AWS-SSM-only flow (DEC-AWS-ONLY-CREDS-001)."""
    for doc in (_REPO_ROOT / "README.md", _REPO_ROOT / "docs" / "runbooks" / "ENV_VARS.md"):
        text = doc.read_text(encoding="utf-8")
        for marker in _AWS_CONTRACT_MARKERS:
            # Boundary-pinned, not a bare substring (hostile-review fold): a
            # suffix/prefix rename (e.g. ``BRUTEX_TOKEN_PARAM_TOTP_SECRET_GONE``)
            # must NOT satisfy the marker — only the exact live name does.
            pattern = rf"(?<![\w-]){re.escape(marker)}(?![\w-])"
            assert re.search(pattern, text), (
                f"{doc.name} is missing the AWS credential marker {marker}"
            )


def test_operator_docs_have_no_retired_groww_auth_vars() -> None:
    """No operator-facing doc may instruct a retired ``BRUTEX_GROWW_*`` auth var.

    DEC-AWS-ONLY-CREDS-001 removed the env-var + credentials-file + webpage
    credential paths; AWS Parameter Store is the SOLE source. Mirrors the Dhan
    guard above so the retired names cannot creep back into operator docs.
    """
    for doc in _AUTH_DOCS:
        text = doc.read_text(encoding="utf-8")
        for var in _RETIRED_GROWW_AUTH_VARS:
            assert var not in text, f"{doc.name} still references retired auth var {var}"
