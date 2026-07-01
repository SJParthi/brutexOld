"""Content-assertion tests for the remote Terraform state backend.

DEC-DEPLOY-REMOTE-STATE-001 (docs/MIGRATION.md #1076). The AWS Groww
token-minter deploy is made fresh-clone-repeatable + idempotent by moving
Terraform state from a laptop-local ``terraform.tfstate`` into AWS (an S3 bucket
+ a DynamoDB state-lock table). ``deploy/aws_token_minter/backend.tf`` declares
the ``s3`` backend (fixed ``key`` + ``region`` + ``encrypt``); the account-derived
``bucket`` + ``dynamodb_table`` are injected at ``terraform init`` time via
``-backend-config`` by ``deploy.sh``, which bootstraps the bucket + lock table
idempotently (check-then-create) BEFORE ``terraform init`` (the chicken-and-egg
ordering — the backend store must exist before Terraform can use it).

These tests pin those behaviours so a regression cannot silently drop the remote
backend, the idempotency guards, the state-bucket hardening, or the never-silent
``-migrate-state`` discipline. They are pure static assertions over the shell +
HCL source — no shell is executed, no network / AWS is touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY_DIR = _REPO_ROOT / "deploy" / "aws_token_minter"
_DEPLOY_SCRIPT = _DEPLOY_DIR / "deploy.sh"
_BACKEND_TF = _DEPLOY_DIR / "backend.tf"


@pytest.fixture(scope="module")
def deploy_script_text() -> str:
    """Return the full source of ``deploy.sh``."""
    assert _DEPLOY_SCRIPT.is_file(), f"missing deploy script: {_DEPLOY_SCRIPT}"
    return _DEPLOY_SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def backend_tf_text() -> str:
    """Return the full source of ``backend.tf``."""
    assert _BACKEND_TF.is_file(), f"missing backend.tf: {_BACKEND_TF}"
    return _BACKEND_TF.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# backend.tf declares the s3 backend with a fixed key/region + encryption, and
# does NOT hardcode the account-derived bucket / dynamodb_table.
# --------------------------------------------------------------------------- #
def test_backend_declares_s3_backend(backend_tf_text: str) -> None:
    """``backend.tf`` declares a ``terraform { backend "s3" { … } }`` block."""
    assert 'backend "s3"' in backend_tf_text
    assert 'key     = "tickvault/groww-token-minter/terraform.tfstate"' in backend_tf_text
    assert 'region  = "ap-south-1"' in backend_tf_text
    assert "encrypt = true" in backend_tf_text


def test_backend_does_not_hardcode_bucket_or_table(backend_tf_text: str) -> None:
    """The account-derived ``bucket`` / ``dynamodb_table`` are injected, not baked."""
    for raw in backend_tf_text.splitlines():
        code = raw.split("#", 1)[0]
        assert "bucket " not in code, f"bucket must be injected via -backend-config: {raw!r}"
        assert "dynamodb_table" not in code, (
            f"dynamodb_table must be injected via -backend-config: {raw!r}"
        )


# --------------------------------------------------------------------------- #
# deploy.sh bootstraps the backend idempotently BEFORE init, hardens the bucket,
# and never silently migrates state.
# --------------------------------------------------------------------------- #
def test_deploy_bootstraps_state_bucket_idempotently(deploy_script_text: str) -> None:
    """The S3 bucket is guarded by ``head-bucket`` (create only if absent)."""
    assert "aws s3api head-bucket" in deploy_script_text
    assert "aws s3api create-bucket" in deploy_script_text


def test_deploy_hardens_state_bucket(deploy_script_text: str) -> None:
    """The state bucket is versioned + AES256-SSE + fully public-access-blocked."""
    assert "put-bucket-versioning" in deploy_script_text
    assert "Status=Enabled" in deploy_script_text
    assert "put-bucket-encryption" in deploy_script_text
    assert "AES256" in deploy_script_text
    assert "put-public-access-block" in deploy_script_text
    assert "BlockPublicAcls=true" in deploy_script_text
    assert "RestrictPublicBuckets=true" in deploy_script_text


def test_deploy_bootstraps_lock_table_idempotently(deploy_script_text: str) -> None:
    """The DynamoDB lock table is guarded by ``describe-table`` (create if absent)."""
    assert "aws dynamodb describe-table" in deploy_script_text
    assert "aws dynamodb create-table" in deploy_script_text
    assert "aws dynamodb wait table-exists" in deploy_script_text
    assert "LockID" in deploy_script_text
    assert "PAY_PER_REQUEST" in deploy_script_text


def test_deploy_init_uses_remote_backend_config(deploy_script_text: str) -> None:
    """``terraform init`` injects the account-derived bucket/table/region."""
    assert "terraform init -input=false -reconfigure \\" in deploy_script_text
    assert '-backend-config="bucket=${STATE_BUCKET}"' in deploy_script_text
    assert '-backend-config="dynamodb_table=${STATE_LOCK_TABLE}"' in deploy_script_text
    assert '-backend-config="region=${STATE_REGION}"' in deploy_script_text


def _executable_lines(script_text: str) -> list[str]:
    """Return the executable (non-comment, non-blank) lines of a shell script."""
    out: list[str] = []
    for raw in script_text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append(raw)
    return out


def test_deploy_never_silently_migrates_state(deploy_script_text: str) -> None:
    """``deploy.sh`` runs a PLAIN ``terraform init`` — never ``-migrate-state``.

    Silently moving state would be a footgun; the one-time migration is a
    deliberate, confirm-``yes`` operator step documented in the runbook. Checks
    only executable code (the header + runbook comments legitimately describe
    the ``-migrate-state`` command for the operator).
    """
    for line in _executable_lines(deploy_script_text):
        assert "-migrate-state" not in line, (
            f"deploy.sh must never silently migrate state: {line!r}"
        )


def test_deploy_never_auto_approves(deploy_script_text: str) -> None:
    """``terraform apply`` NEVER passes ``-auto-approve`` — a human confirms.

    Checks only executable code (the header comments legitimately document that
    ``-auto-approve`` is deliberately absent).
    """
    for line in _executable_lines(deploy_script_text):
        assert "-auto-approve" not in line, f"deploy.sh must never auto-approve: {line!r}"


def test_deploy_bootstrap_precedes_init(deploy_script_text: str) -> None:
    """The bucket/table bootstrap runs BEFORE ``terraform init`` (chicken-and-egg)."""
    head_bucket_pos = deploy_script_text.index("aws s3api head-bucket")
    init_pos = deploy_script_text.index("terraform init -input=false -reconfigure")
    assert head_bucket_pos < init_pos, "state store must be created before terraform init"
