"""Content-assertion tests for the ``deploy.sh`` auto-adopt state preflight.

DEC-DEPLOY-AUTO-FIRST-MINT-001 (docs/MIGRATION.md). A stack first deployed under
the OLD local Terraform state (or created out-of-band) has its name-fixed AWS
resources LIVE while the shared REMOTE state (DEC-DEPLOY-REMOTE-STATE-001,
docs/MIGRATION.md #1078) is EMPTY — so a fresh-clone ``terraform apply`` dies
with ``ParameterAlreadyExists`` / ``EntityAlreadyExists`` (409) on every one of
them (the operator hit exactly this live). ``deploy.sh`` now runs an AUTO-ADOPT
preflight between ``terraform init`` and ``terraform apply``: for each of the 7
adoptable addresses, if the address is absent from state AND the AWS resource
exists, it is ``terraform import``-ed automatically; already-in-state and
not-present-in-AWS are honest no-ops; a FAILED import HARD-STOPS the deploy
(continuing into apply would just re-fail with the same conflict).

These tests pin: the preflight exists, covers all 7 addresses with the correct
import ids, runs AFTER init and BEFORE apply, hard-stops on an import failure,
keeps the idempotent no-op paths, and probes AWS with the right CLI calls. They
are pure static assertions over the shell source — no shell is executed, no
network / AWS / terraform is touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY_SCRIPT = _REPO_ROOT / "deploy" / "aws_token_minter" / "deploy.sh"

#: The 7 adoptable (address, kind, import-id expression) triples — the ONLY
#: resources in the stack whose operator-visible names are FIXED and can 409 an
#: apply when they pre-exist outside the state (everything else derives from
#: them at apply time or is created with idempotent put semantics).
_ADOPTABLE: tuple[tuple[str, str, str], ...] = (
    ("aws_ssm_parameter.access_token", "ssm-param", "${PARAM_PREFIX}/${TOKEN_PARAM_NAME}"),
    ("aws_iam_role.minter", "iam-role", "${LAMBDA_FUNCTION_NAME}-minter"),
    ("aws_iam_role.reader_tickvault", "iam-role", "${LAMBDA_FUNCTION_NAME}-reader-tickvault"),
    ("aws_iam_role.reader_brutex", "iam-role", "${LAMBDA_FUNCTION_NAME}-reader-brutex"),
    ("aws_cloudwatch_log_group.minter", "log-group", "/aws/lambda/${LAMBDA_FUNCTION_NAME}"),
    ("aws_lambda_function.minter", "lambda-function", "${LAMBDA_FUNCTION_NAME}"),
    (
        "aws_lambda_permission.allow_eventbridge",
        "lambda-permission",
        "${LAMBDA_FUNCTION_NAME}/AllowEventBridgeInvoke",
    ),
)


@pytest.fixture(scope="module")
def deploy_script_text() -> str:
    """Return the full source of ``deploy.sh``."""
    assert _DEPLOY_SCRIPT.is_file(), f"missing deploy script: {_DEPLOY_SCRIPT}"
    return _DEPLOY_SCRIPT.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# The preflight exists and is wired between init and apply.
# --------------------------------------------------------------------------- #
def test_adopt_preflight_exists(deploy_script_text: str) -> None:
    """``deploy.sh`` carries the state-adopt preflight (state-show + import)."""
    assert "terraform state show" in deploy_script_text
    assert "terraform import" in deploy_script_text
    assert "adopt_resource" in deploy_script_text


def test_adopt_runs_after_init_and_before_apply(deploy_script_text: str) -> None:
    """The preflight runs AFTER ``terraform init``, BEFORE ``terraform apply``.

    Importing needs the initialised remote backend (else there is no state to
    inspect), and adopting must complete before apply (else apply 409s first).
    """
    init_pos = deploy_script_text.index("terraform init -input=false -reconfigure")
    apply_pos = deploy_script_text.index('terraform apply "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"')
    first_call = deploy_script_text.index('adopt_resource "aws_ssm_parameter.access_token"')
    last_call = deploy_script_text.index('adopt_resource "aws_lambda_permission.allow_eventbridge"')
    assert init_pos < first_call, "adopt preflight must run after terraform init"
    assert last_call < apply_pos, "adopt preflight must complete before terraform apply"


# --------------------------------------------------------------------------- #
# All 7 adoptable addresses are covered, with the correct import ids.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("address", "kind", "import_id"), _ADOPTABLE)
def test_adopt_covers_address(
    deploy_script_text: str, address: str, kind: str, import_id: str
) -> None:
    """Each adoptable resource is adopted with its exact kind + import id."""
    call = f'adopt_resource "{address}" {kind} "{import_id}"'
    assert call in deploy_script_text, f"missing adopt call: {call}"


def test_adopt_covers_exactly_seven_addresses(deploy_script_text: str) -> None:
    """Exactly the 7 known-colliding addresses are adopted (no drift)."""
    count = deploy_script_text.count("\nadopt_resource ")
    assert count == len(_ADOPTABLE) == 7, (
        f"expected exactly 7 adopt_resource call sites, found {count}"
    )


# --------------------------------------------------------------------------- #
# Failure + idempotency semantics.
# --------------------------------------------------------------------------- #
def test_adopt_hard_stops_on_import_failure(deploy_script_text: str) -> None:
    """A failed ``terraform import`` exits NON-ZERO — never continues to apply.

    The guard block between ``if ! terraform import`` and its ``fi`` must exit 1
    so a doomed apply (which would just re-fail with the 409) is never reached.
    """
    start = deploy_script_text.index("if ! terraform import")
    block = deploy_script_text[start : deploy_script_text.index("fi", start)]
    assert "exit 1" in block, "import failure must hard-stop the deploy"
    assert "FATAL" in block, "import failure must be loudly labelled"


def test_adopt_import_does_not_swallow_terraform_stderr(deploy_script_text: str) -> None:
    """The ``terraform import`` line keeps stderr visible (verbatim error)."""
    start = deploy_script_text.index("if ! terraform import")
    line = deploy_script_text[start:].splitlines()[0]
    assert "2>/dev/null" not in line
    assert "2>&1" not in line


def test_adopt_has_honest_no_op_paths(deploy_script_text: str) -> None:
    """Already-in-state and not-present-in-AWS each print an honest no-op line."""
    assert "already in state (no-op)" in deploy_script_text
    assert "not present in AWS (apply will create it)" in deploy_script_text
    assert "adopted into the remote state" in deploy_script_text


def test_adopt_import_passes_operator_var_overrides(deploy_script_text: str) -> None:
    """``terraform import`` rides the SAME guarded EXTRA_ARGS as apply.

    An operator ``-var 'region=…'`` / ``-var 'lambda_function_name=…'`` must
    shape the import exactly as it shapes the apply, and the expansion must use
    the bash-3.2-safe empty-array idiom (DEC-DEPLOY-EMPTY-ARGS-FIX-001,
    docs/MIGRATION.md #1075).
    """
    guarded_import = (
        'terraform import -input=false "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}" '
        '"$ADOPT_ADDR" "$ADOPT_ID"'
    )
    assert guarded_import in deploy_script_text


# --------------------------------------------------------------------------- #
# The AWS existence probes are the right CLI calls per resource kind.
# --------------------------------------------------------------------------- #
def test_adopt_probes_use_the_right_aws_calls(deploy_script_text: str) -> None:
    """Each resource kind is probed with its exact-match AWS CLI existence check."""
    assert "aws ssm get-parameter --region" in deploy_script_text
    assert "aws iam get-role --role-name" in deploy_script_text
    assert "aws logs describe-log-groups --region" in deploy_script_text
    assert "aws lambda get-function --region" in deploy_script_text
    assert "aws lambda get-policy --region" in deploy_script_text


def test_adopt_log_group_probe_requires_exact_match(deploy_script_text: str) -> None:
    """The log-group probe exact-matches (``--log-group-name-prefix`` is a prefix)."""
    assert "logGroups[?logGroupName==" in deploy_script_text
    assert '[ "$FOUND_LOG_GROUP" = "$2" ]' in deploy_script_text


def test_adopt_permission_probe_is_pipeline_free(deploy_script_text: str) -> None:
    """The lambda-permission probe avoids ``| grep -q`` under ``set -o pipefail``.

    A ``grep -q`` early-exit SIGPIPE would misread an EXISTING permission as
    "not present" and let apply 409; the probe captures the policy text then
    substring-matches with ``case``.
    """
    assert "POLICY_TEXT=" in deploy_script_text
    assert "get-policy --region" in deploy_script_text
    start = deploy_script_text.index("lambda-permission)")
    block = deploy_script_text[start : deploy_script_text.index("esac", start)]
    for raw in block.splitlines():
        code = raw.split("#", 1)[0]  # the explanatory comment legitimately names grep
        assert "| grep" not in code, f"pipeline-to-grep in the permission probe: {raw!r}"


# --------------------------------------------------------------------------- #
# The name overrides feeding the preflight honor operator -var flags.
# --------------------------------------------------------------------------- #
def test_deploy_scans_name_var_overrides(deploy_script_text: str) -> None:
    """The arg scan honors the -var overrides that shape the adoptable names."""
    assert 'lambda_function_name=*) LAMBDA_FUNCTION_NAME="${arg#lambda_function_name=}"' in (
        deploy_script_text
    )
    assert 'param_prefix=*) PARAM_PREFIX="${arg#param_prefix=}"' in deploy_script_text
    assert 'token_param_name=*) TOKEN_PARAM_NAME="${arg#token_param_name=}"' in deploy_script_text


def test_deploy_name_defaults_mirror_variables_tf(deploy_script_text: str) -> None:
    """The scanned defaults are byte-identical to the variables.tf defaults."""
    assert 'LAMBDA_FUNCTION_NAME="groww-token-minter"' in deploy_script_text
    assert 'PARAM_PREFIX="$PARAM_PREFIX_DEFAULT"' in deploy_script_text
    assert 'TOKEN_PARAM_NAME="access-token"' in deploy_script_text
