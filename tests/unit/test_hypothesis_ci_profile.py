"""Pins for the hypothesis ``ci`` profile (DEC-TEST-APPARATUS-DEBTS-001(b)).

``tests/conftest.py`` registers a fully-explicit ``ci`` hypothesis profile
(``derandomize=True, print_blob=True, deadline=None, database=None,
suppress_health_check=(too_slow,)``) and loads the profile named by the
``HYPOTHESIS_PROFILE`` env var (falling back to ``default``). CI workflows
(ci.yml / verify-m4pro.yml / mutation.yml) set ``HYPOTHESIS_PROFILE=ci`` so
PR-path example generation is a pure function of each test — a CI failure
reproduces on every re-run.

HONESTY (Golden Rule #1): hypothesis >=6.113 ships its OWN built-in ``ci``
profile with ``derandomize=True`` and auto-loads it whenever a CI env var
(``CI`` / ``GITHUB_ACTIONS`` / ...) is set — so GHA runs were implicitly
derandomized before the conftest block existed, and a naive
``get_profile("ci")`` field assertion is TAUTOLOGICAL (it passes against the
built-in with the conftest block deleted — the round-2 adversarial audit
demonstrated exactly that). The teeth here are therefore three-layered:

1. field pins on the FULLY-EXPLICIT registered profile (environment
   invariance: a local ``HYPOTHESIS_PROFILE=ci`` run is field-identical to
   CI — the register call never inherits ambient-default fields);
2. a SOURCE pin that ``tests/conftest.py`` itself performs the explicit
   registration (deleting the register block fails here even though the
   name ``ci`` would still resolve to hypothesis's built-in);
3. a SUBPROCESS pin with every CI-detection env var stripped, proving the
   ``HYPOTHESIS_PROFILE`` env selection in the conftest — NOT hypothesis's
   CI auto-detect — is what loads the profile (and that the no-env default
   stays random). This fails today if the conftest load line is deleted.

These pins use ONLY the public ``hypothesis.settings`` API (never
``hypothesis._settings``) so a hypothesis upgrade that silently changed the
profile mechanism fails here loudly.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFTEST = _REPO_ROOT / "tests" / "conftest.py"

# Superset of hypothesis's CI-detection env vars (hypothesis._settings
# _CI_VARS, mirrored here as data so the subprocess pins never import the
# private module). Stripping these guarantees hypothesis's built-in
# auto-load of its own "ci" profile CANNOT fire in the subprocess.
_CI_DETECTION_ENV_VARS = (
    "CI",
    "__TOX_ENVIRONMENT_VARIABLE_ORIGINAL_CI",
    "TF_BUILD",
    "bamboo.buildKey",
    "BUILDKITE",
    "CIRCLECI",
    "CIRRUS_CI",
    "CODEBUILD_BUILD_ID",
    "GITHUB_ACTIONS",
    "GITLAB_CI",
    "HEROKU_TEST_RUN_ID",
    "TEAMCITY_VERSION",
)


def test_ci_profile_is_registered_fully_explicit() -> None:
    """The registered ``ci`` profile carries EVERY load-bearing field explicitly.

    ``derandomize``/``print_blob`` are the determinism/reproduction pair;
    ``deadline=None``/``database=None``/``suppress_health_check=(too_slow,)``
    make the profile environment-invariant (registered with explicit values,
    never inherited from whatever ambient profile was active at conftest
    import) — so a local ``HYPOTHESIS_PROFILE=ci`` flush-out run executes the
    SAME profile CI does, never a stricter or looser one.
    """
    profile = settings.get_profile("ci")
    assert profile.derandomize is True
    assert profile.print_blob is True
    assert profile.deadline is None
    assert profile.database is None
    assert tuple(profile.suppress_health_check) == (HealthCheck.too_slow,)


def _literal_kwarg(kwargs: dict[str, ast.expr], name: str) -> object:
    """Return the literal value of keyword ``name``, asserting it IS a literal.

    Binding the node to a local before the ``isinstance`` check is what lets
    mypy/pyright narrow ``ast.expr`` → ``ast.Constant`` for the ``.value``
    access (narrowing ``kwargs.get(name)`` does not narrow a later
    ``kwargs[name]`` subscript — they are distinct expressions).
    """
    node = kwargs.get(name)
    assert isinstance(node, ast.Constant), (
        f"register_profile('ci', ...) kwarg {name!r} is missing or not an "
        f"explicit literal — the environment-invariance guarantee requires "
        f"every load-bearing field to be passed as a literal keyword"
    )
    return node.value


def test_conftest_source_performs_the_explicit_registration() -> None:
    """AST pin: the register call lives in tests/conftest.py, not upstream.

    Hypothesis's built-in ``ci`` profile makes a pure ``get_profile("ci")``
    assertion tautological — this pin is the non-tautological complement:
    deleting (or renaming) the conftest ``register_profile("ci", ...)`` block
    fails HERE even though ``get_profile("ci")`` would still resolve to the
    field-compatible built-in. It also pins that the registration is fully
    explicit (``derandomize=True`` + ``deadline=None`` + ``database=None``
    passed as literal keywords — the environment-invariance guarantee).
    """
    tree = ast.parse(_CONFTEST.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "register_profile"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "ci"
        ):
            kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
            assert _literal_kwarg(kwargs, "derandomize") is True
            assert _literal_kwarg(kwargs, "print_blob") is True
            assert _literal_kwarg(kwargs, "deadline") is None
            assert _literal_kwarg(kwargs, "database") is None
            assert "suppress_health_check" in kwargs
            return
    raise AssertionError(
        "tests/conftest.py no longer registers the 'ci' hypothesis profile "
        "explicitly — the repo's determinism claim would silently ride "
        "hypothesis's built-in ci profile (upstream-owned, version-dependent)"
    )


def _run_conftest_probe(extra_env: dict[str, str]) -> str:
    """Execute tests/conftest.py in a subprocess with ALL CI env vars stripped.

    Returns ``"<profile_name> <derandomize>"`` printed by the probe. With the
    CI-detection vars stripped, hypothesis's built-in auto-load cannot fire —
    whatever profile ends up current was selected by the CONFTEST mechanism.
    """
    probe = textwrap.dedent(
        f"""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "brutex_root_conftest", {str(_CONFTEST)!r}
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        from hypothesis import settings

        print(settings.get_current_profile_name(), settings.default.derandomize)
        """
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _CI_DETECTION_ENV_VARS and key != "HYPOTHESIS_PROFILE"
    }
    env.update(extra_env)
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=env,
        cwd=_REPO_ROOT,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        f"conftest probe failed (rc={result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return result.stdout.strip()


def test_hypothesis_profile_env_var_selects_ci_without_ci_autodetect() -> None:
    """``HYPOTHESIS_PROFILE=ci`` loads the ci profile with CI auto-detect impossible.

    Every CI-detection env var is stripped from the subprocess, so hypothesis's
    built-in ``if is_in_ci(): load_profile("ci")`` cannot have fired — the loaded
    profile is attributable ONLY to the conftest's env-var selection. Deleting
    the conftest ``load_profile`` line fails this pin (the subprocess would sit
    on the random ``default`` profile).
    """
    assert _run_conftest_probe({"HYPOTHESIS_PROFILE": "ci"}) == "ci True"


def test_no_env_var_stays_on_the_random_default_profile() -> None:
    """Without ``HYPOTHESIS_PROFILE``, the conftest loads the random default.

    This is the sanctioned-flip pin: a run that does not opt in via the env
    var gets exploration (``derandomize=False``), never a silently-inherited
    derandomized profile.
    """
    assert _run_conftest_probe({}) == "default False"


def _collect_draws_under_ci_profile() -> list[int]:
    """Run a tiny integer collector with the ``ci`` profile loaded; return draws.

    ``load_profile("ci")`` runs BEFORE the probe is defined — a ``@settings``
    decorator freezes its parent profile at CONSTRUCTION time, which is exactly
    how the real mechanism works in CI (the profile is loaded at conftest
    import, before any test module is imported), so this probe exercises the
    same inheritance path every real hypothesis test rides.
    """
    draws: list[int] = []
    settings.load_profile("ci")
    try:
        # deadline=None: the probe is trivially fast, but a loaded shared
        # runner must never deadline-flake a determinism PIN.
        @settings(max_examples=25, deadline=None)
        @given(st.integers())
        def _probe(value: int) -> None:
            draws.append(value)

        _probe()
    finally:
        # Restore EXACTLY what tests/conftest.py loaded for this process, so
        # this pin never leaks profile state into later-collected tests.
        settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))
    return draws


def test_ci_profile_derandomizes_example_generation() -> None:
    """Two runs of the same collector under the ``ci`` profile draw the SAME sequence.

    This is the mechanical determinism pin: with ``derandomize=True`` loaded,
    hypothesis derives the generation seed from the test itself, so the drawn
    example sequence is a pure function of the test — identical on every run.
    """
    first = _collect_draws_under_ci_profile()
    second = _collect_draws_under_ci_profile()
    assert first, "probe drew no examples — the pin is vacuous"
    assert first == second, (
        "ci profile did not derandomize: two identical runs drew different "
        f"example sequences (first 5: {first[:5]} vs {second[:5]})"
    )
