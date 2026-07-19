"""Pins for ``scripts/ci/no-dhan-guard.sh`` — the zero-vendor-word CI guard.

DEC-UNIVERSE-INDEX-ONLY-001 (MIGRATION.md #1934, the r6 confirmation-review
fold): the per-feed scope map ``DEFAULT_FEED_SCOPES`` carries the
directive-(B) key ``dhan`` (``brutex/config/feed_scope.py``), so the guard
gained exemption 1b — the key's sanctioned CONFIG-TOKEN spellings (quoted /
backticked / the ``dhan=index4|full`` / ``dhan: index4|full`` binding forms)
are stripped before the scan. These tests pin BOTH directions:

* the live repo surface is CLEAN under the amended guard (exit 0 — the
  branch's ci-ok member is genuinely green, not exemption-vacuous), and
* the guard's stale-vendor-wording TEETH survive the exemption — bare prose,
  identifiers, uppercase, and non-scope-token bindings still FAIL (exit 1)
  in a hermetic tmp tree, while the sanctioned forms and the pre-existing
  DEC-token exemption still PASS.

The hermetic battery copies the guard into ``<tmp>/scripts/ci/`` (the script
resolves its repo root from its own location) and plants one file under
``<tmp>/brutex/`` per case — no monkeypatching of the script itself.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GUARD_REL = Path("scripts/ci/no-dhan-guard.sh")
_GUARD = _REPO_ROOT / _GUARD_REL
_TIMEOUT_S = 120.0


def _run_guard(repo_root: Path) -> subprocess.CompletedProcess[str]:
    """Run the guard script rooted at ``repo_root`` and return the result."""
    return subprocess.run(
        ["bash", str(repo_root / _GUARD_REL)],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
        check=False,
    )


def _tmp_tree(tmp_path: Path, planted: str) -> Path:
    """Build a minimal scan tree: the real guard + one planted brutex file."""
    (tmp_path / "scripts" / "ci").mkdir(parents=True)
    shutil.copy2(_GUARD, tmp_path / _GUARD_REL)
    (tmp_path / "brutex").mkdir()
    (tmp_path / "brutex" / "planted.py").write_text(planted, encoding="utf-8")
    return tmp_path


class TestRepoSurfaceClean:
    def test_live_surface_exits_zero(self) -> None:
        """The amended guard passes on the REAL repo surface (the ci-ok member)."""
        result = _run_guard(_REPO_ROOT)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "zero forbidden" in result.stdout


class TestSanctionedFormsPass:
    def test_scope_map_config_token_spellings_exit_zero(self, tmp_path: Path) -> None:
        """Exemption 1b: every sanctioned spelling of the #1934 scope-map key."""
        planted = "\n".join(
            [
                '_MAP = {"groww": "index4", "dhan": "index4"}',
                "#: the ``dhan`` key has NO adapter in-tree",
                "# env form: groww=index4,dhan=index4",
                "# map notation: {groww: index4, dhan: index4}",
                "# wide binding: dhan=full",
                "# wide map: dhan: full",
                "",
            ]
        )
        result = _run_guard(_tmp_tree(tmp_path, planted))
        assert result.returncode == 0, result.stdout + result.stderr

    def test_dec_id_token_exemption_still_holds(self, tmp_path: Path) -> None:
        """The pre-existing exemption 1 (immutable DEC-ID tokens) is unchanged."""
        planted = "# per DEC-DHAN-GROWW-REMIGRATE-001 and DEC-GROWW-DHAN-REVERT-001\n"
        result = _run_guard(_tmp_tree(tmp_path, planted))
        assert result.returncode == 0, result.stdout + result.stderr


class TestTeethRetained:
    @pytest.mark.parametrize(
        "planted",
        [
            pytest.param("# pull the candles from dhan instead\n", id="bare-prose"),
            pytest.param("dhan_client = build_client()\n", id="identifier"),
            pytest.param('_MAP = {"DHAN": "index4"}\n', id="uppercase-quoted-key"),
            pytest.param("# dhan: the old vendor\n", id="non-scope-token-binding"),
            pytest.param("# fetched via dhan=legacy mode\n", id="non-scope-token-env"),
        ],
    )
    def test_forbidden_shapes_still_fail(self, tmp_path: Path, planted: str) -> None:
        """The 1b strip is narrow: stale vendor wording still exits 1."""
        result = _run_guard(_tmp_tree(tmp_path, planted))
        assert result.returncode == 1, result.stdout + result.stderr
        assert "brutex/planted.py" in result.stdout
        assert "forbidden 'dhan'" in result.stdout
