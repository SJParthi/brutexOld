"""Guard-retirement reconcile proof (DEC-DHAN-GUARD-RETIRE-001).

The operator re-admitted Dhan on 2026-07-12, so the ``no-dhan-guard`` CI gate (which forbade
the word ``dhan`` in the codebase) is RETIRED. This is the permanent structural regression
that the retirement is COMPLETE and left no dangling wiring — and that no OTHER gate now
blocks the word ``dhan``:

* the guard script ``scripts/ci/no-dhan-guard.sh`` is deleted,
* ``.github/workflows/ci.yml`` is valid YAML with no ``no-dhan-guard`` job, no such entry in
  ``ci-ok.needs``, and no dangling ``needs.no-dhan-guard.result`` reference,
* ``branch-protection-drift.yml`` no longer names the guard,
* the ``test_vendor_doc_consistency`` reconciliation holds — the chosen SSM-path override
  names ``BRUTEX_TOKEN_PARAM_DHAN_*`` do NOT collide with its banned direct-secret list,
* no active gate/script anywhere blocks the literal word ``dhan``.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import yaml

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_CI_YML: Path = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_BP_DRIFT: Path = _REPO_ROOT / ".github" / "workflows" / "branch-protection-drift.yml"


def _ci_config() -> dict[str, object]:
    return cast("dict[str, object]", yaml.safe_load(_CI_YML.read_text(encoding="utf-8")))


def test_guard_script_is_deleted() -> None:
    assert not (_REPO_ROOT / "scripts" / "ci" / "no-dhan-guard.sh").exists()


def test_ci_yml_is_valid_yaml() -> None:
    config = _ci_config()
    assert isinstance(config, dict)
    assert "jobs" in config


def test_ci_yml_has_no_no_dhan_guard_job() -> None:
    jobs = _ci_config()["jobs"]
    assert isinstance(jobs, dict)
    assert "no-dhan-guard" not in jobs


def test_ci_ok_needs_does_not_list_the_guard() -> None:
    jobs = _ci_config()["jobs"]
    assert isinstance(jobs, dict)
    ci_ok = jobs["ci-ok"]
    assert isinstance(ci_ok, dict)
    needs = ci_ok.get("needs", [])
    needs_list = [needs] if isinstance(needs, str) else list(needs)
    assert "no-dhan-guard" not in needs_list


def test_ci_yml_has_no_dangling_guard_result_reference() -> None:
    text = _CI_YML.read_text(encoding="utf-8")
    assert "no-dhan-guard" not in text
    assert "no_dhan_guard" not in text
    assert "needs.no-dhan-guard.result" not in text


def test_branch_protection_drift_no_longer_names_the_guard() -> None:
    text = _BP_DRIFT.read_text(encoding="utf-8")
    assert yaml.safe_load(text) is not None  # still valid YAML
    assert "no-dhan-guard" not in text


def test_no_active_gate_blocks_the_word_dhan() -> None:
    """Sweep scripts + workflows for a surviving Dhan blocker (the retired guard's shape)."""
    for base in (_REPO_ROOT / "scripts", _REPO_ROOT / ".github" / "workflows"):
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace").lower()
            # A file may mention "dhan" freely now; what must be gone is a gate that BLOCKS it.
            if "no-dhan-guard" in text:
                raise AssertionError(f"{path} still references the retired no-dhan-guard gate")


def test_vendor_doc_consistency_override_names_do_not_collide() -> None:
    """The chosen SSM-path overrides must not fall in the banned direct-secret list."""
    from tests.unit import test_vendor_doc_consistency as vdc

    override_names = (
        "BRUTEX_TOKEN_PARAM_DHAN_CLIENT_ID",
        "BRUTEX_TOKEN_PARAM_DHAN_PIN",
        "BRUTEX_TOKEN_PARAM_DHAN_TOTP_SECRET",
        "BRUTEX_TOKEN_PARAM_DHAN_ACCESS_TOKEN",
    )
    for name in override_names:
        assert name not in vdc._WRONG_AUTH_VARS
    # The banned list bans the DIRECT-secret shape only (different prefix).
    for banned in vdc._WRONG_AUTH_VARS:
        assert banned.startswith("BRUTEX_DHAN_")
        assert not banned.startswith("BRUTEX_TOKEN_PARAM_DHAN_")
