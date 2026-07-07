"""Enforce the ``tests/stress`` marker contract — the SOAK-R7-1 drift, pinned.

Round 7 of DEC-SOAK-REAL-TIER-001 (MIGRATION.md #1534 (x)) found 3 stress files
(8 tests) carrying NEITHER ``@pytest.mark.stress`` NOR ``@pytest.mark.soak``:
because BOTH CI tiers select BY MARKER (nightly ``-m stress``; self-hosted soak
``-m 'stress or soak'``), an unmarked stress test silently drops out of both —
only the per-PR ``-m 'not soak'`` step still runs it. That fix's "collection
pin" was verified MANUALLY, so nothing stopped the drift from recurring on the
next new stress file (AL9-4). This test IS the enforced collection pin, running
in the per-PR unit tier:

* every test in ``tests/stress`` carries EXACTLY ONE of ``stress`` / ``soak``
  (via module-level ``pytestmark``, class-level ``pytestmark``, or a function
  decorator) — so ``-m stress`` selects everything except the long soak and
  ``-m 'stress or soak'`` selects the whole directory (a test carrying BOTH
  would leak the long soak into the nightly tier: the contract is INSTEAD,
  not "also" — see ``tests/stress/__init__.py``);
* the ``soak`` marker is carried by EXACTLY the one long driver,
  ``test_soak_sustained.py::test_soak_sustained`` — any other ``soak`` test
  would silently vanish from the nightly + per-PR tiers, and a de-marked long
  driver would run for hours inside them.

The scan mirrors pytest's own collection rules for this directory (module
``test_*.py`` → function ``test_*`` / class ``Test*`` → method ``test_*``) and
reads the SAME ``pytestmark`` attributes pytest reads, so a marker change that
would alter collection fails HERE, on every PR.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from typing import Any

_STRESS_DIR = Path(__file__).resolve().parents[1] / "stress"

_TIER_MARKS = frozenset({"stress", "soak"})

# The ONE test allowed (and required) to carry ``soak`` — the multi-hour driver
# soak-selfhosted.yml exists for. Anything else here is a contract change that
# must be made consciously, alongside the workflow tiers.
_EXPECTED_SOAK_NODES = frozenset({"test_soak_sustained.py::test_soak_sustained"})


def _tier_mark_names(obj: object) -> set[str]:
    """The {stress, soak} marker names attached to *obj* via ``pytestmark``.

    Handles both shapes pytest accepts: a module-level ``pytestmark`` may be a
    single mark or a list; decorator-applied marks accumulate as a list on the
    function/class ``pytestmark`` attribute. Non-tier marks (``parametrize``,
    ...) are ignored.
    """
    marks: Any = getattr(obj, "pytestmark", [])
    if not isinstance(marks, list):
        marks = [marks]
    return {getattr(mark, "name", "") for mark in marks} & _TIER_MARKS


def _collect_stress_nodes() -> dict[str, set[str]]:
    """Map every tests/stress test node id to its effective tier-mark set."""
    nodes: dict[str, set[str]] = {}
    module_paths = sorted(_STRESS_DIR.glob("test_*.py"))
    assert module_paths, f"no stress test modules found under {_STRESS_DIR}"
    for path in module_paths:
        module = importlib.import_module(f"tests.stress.{path.stem}")
        module_marks = _tier_mark_names(module)
        for name, obj in vars(module).items():
            if name.startswith("test_") and inspect.isfunction(obj):
                nodes[f"{path.name}::{name}"] = module_marks | _tier_mark_names(obj)
            elif name.startswith("Test") and inspect.isclass(obj):
                class_marks = module_marks | _tier_mark_names(obj)
                for meth_name, meth in vars(obj).items():
                    if meth_name.startswith("test_") and inspect.isfunction(meth):
                        node = f"{path.name}::{name}::{meth_name}"
                        nodes[node] = class_marks | _tier_mark_names(meth)
    return nodes


def test_every_stress_test_carries_exactly_one_tier_marker() -> None:
    """The SOAK-R7-1 pin: no unmarked (or double-marked) stress test can recur.

    Unmarked → deselected by BOTH marker-selecting CI tiers (silent coverage
    loss); marked with both → the long soak would leak into ``-m stress``.
    """
    nodes = _collect_stress_nodes()
    # Sanity floor: the directory holds well over 20 tests today — a glob or
    # import-path rot that silently collected nothing must fail loudly, never
    # green an empty scan.
    assert len(nodes) > 20, f"suspiciously few stress tests scanned: {sorted(nodes)}"
    violations = {node: marks for node, marks in nodes.items() if len(marks) != 1}
    assert not violations, (
        "tests/stress marker contract violated — every test must carry EXACTLY "
        "ONE of @pytest.mark.stress / @pytest.mark.soak (unmarked tests are "
        "silently deselected by BOTH the nightly '-m stress' and the soak "
        f"'-m \"stress or soak\"' tiers; SOAK-R7-1/AL9-4): {violations}"
    )


def test_soak_marker_is_exactly_the_one_long_driver() -> None:
    """Only ``test_soak_sustained`` is the multi-hour ``@soak`` test — pinned."""
    nodes = _collect_stress_nodes()
    soak_nodes = {node for node, marks in nodes.items() if "soak" in marks}
    assert soak_nodes == set(_EXPECTED_SOAK_NODES), (
        "the @soak marker set drifted — a new soak-marked test silently skips "
        "the nightly + per-PR tiers, and de-marking the long driver would run "
        f"it for hours inside them: {sorted(soak_nodes)}"
    )
