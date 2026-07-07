"""Unit tests for the LEAF ``PriorStepsVerdict`` home (DEC-PULL-SIX-STEP-ORDER-001).

The verdict dataclass moved out of the pipeline sequencer
(:mod:`brutex.dashboard.pipeline_pull`) into the dependency-free leaf
:mod:`brutex.dashboard.pipeline_verdict` to break the CodeQL-flagged
module-level import cycle (the sequencer imports its consumers
``composite_pull`` → ``historical_pull`` at runtime, and BOTH consumers needed
the verdict type back from the sequencer — a cycle even ``TYPE_CHECKING``-only).
This dedicated suite pins the LEAF contract standalone: the value object itself
(construction + ``prior_clean`` + immutability), the sequencer's back-compat
re-export identity, and the LEAFNESS invariant the cycle fix rests on (the
module imports NOTHING from ``brutex.``). The sequencer-THREADING behavior of
the verdict stays pinned in ``tests/unit/test_pipeline_pull.py`` (it belongs to
``run_full_pipeline``, not to the value object).
"""

from __future__ import annotations

import ast
import dataclasses
import inspect

import pytest
from brutex.dashboard.pipeline_verdict import PriorStepsVerdict


class TestPriorCleanProperty:
    """``prior_clean`` — every step BEFORE the F&O phase settled clean."""

    @pytest.mark.parametrize(
        ("spot_clean", "metadata_clean", "expected"),
        [
            (True, True, True),
            (True, False, False),
            (False, True, False),
            (False, False, False),
        ],
    )
    def test_truth_table(self, spot_clean: bool, metadata_clean: bool, expected: bool) -> None:
        # ``strict`` is deliberately varied off the diagonal so the property is
        # proven independent of the strict flag (it folds ONLY spot + metadata).
        verdict = PriorStepsVerdict(
            spot_clean=spot_clean, metadata_clean=metadata_clean, strict=spot_clean
        )
        assert verdict.prior_clean is expected


class TestValueObjectContract:
    """Frozen + slotted — the verdict is threaded across phases, never mutated."""

    def test_frozen(self) -> None:
        verdict = PriorStepsVerdict(spot_clean=True, metadata_clean=True, strict=False)
        with pytest.raises(dataclasses.FrozenInstanceError):
            verdict.strict = True  # type: ignore[misc]

    def test_value_equality(self) -> None:
        left = PriorStepsVerdict(spot_clean=True, metadata_clean=False, strict=True)
        right = PriorStepsVerdict(spot_clean=True, metadata_clean=False, strict=True)
        assert left == right


class TestSequencerReExport:
    """The pipeline_pull back-compat alias is the SAME class object, no copy."""

    def test_reexport_identity(self) -> None:
        from brutex.dashboard.pipeline_pull import PriorStepsVerdict as ReExported

        assert ReExported is PriorStepsVerdict


class TestLeafness:
    """The no-cycle guarantee: this module must import NOTHING from brutex.

    CodeQL's module-level cyclic-import analysis counts even
    ``TYPE_CHECKING``-guarded imports, so the leaf stays a leaf by
    construction — any future ``brutex.*`` import here (top-level OR nested)
    re-opens the cycle risk and must fail this pin.
    """

    def test_no_brutex_imports(self) -> None:
        import brutex.dashboard.pipeline_verdict as leaf

        tree = ast.parse(inspect.getsource(leaf))
        offenders = [
            node
            for node in ast.walk(tree)
            if (isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "brutex")
            or (
                isinstance(node, ast.Import)
                and any(alias.name.split(".")[0] == "brutex" for alias in node.names)
            )
        ]
        assert offenders == []
