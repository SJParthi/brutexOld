"""AST purity + source-freeze pins for the hand-rolled fold reference.

DEC-SWEEP-CORE-RUST-001 (MIGRATION.md #1739 reserved) — design §1(b): the
``_reference_fold`` oracle must import ONLY stdlib (never ``brutex.metrics``
leaf helpers, never ``fno_math`` — the recorded Pébay-m3 counterexample is a
differential whose oracle shared ``_accumulate_moments`` and passed a
delta-sign mutant), and its SOURCE TEXT is frozen by a sha256 pin so it can
never drift silently alongside the code under test.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Final

REFERENCE_PATH: Final[Path] = Path(__file__).resolve().parent / "_reference_fold.py"

#: The frozen source digest — re-pin ONLY on a conscious, reviewed oracle
#: change (the change and the re-pin must land in the same commit with a
#: MIGRATION.md note; a drifted hash here is the loud tamper signal).
REFERENCE_SHA256: Final[str] = "c5483386dea11d8a65d089bad4d4f6ac2319936830905e7b891ef9ae697b17a1"

#: The CLOSED allowed-import set — stdlib names the fold algorithms need.
#: ``__future__`` rides the annotations import; anything else fails loud.
_ALLOWED_IMPORTS: Final[frozenset[str]] = frozenset({"__future__", "math"})

#: Names whose mere appearance in the reference is a purity breach — the
#: live package the oracle must stay independent of, and the kernel itself.
_FORBIDDEN_NAMES: Final[frozenset[str]] = frozenset({"brutex", "fno_math"})


class TestReferenceFoldFrozen:
    def test_source_hash_is_pinned(self) -> None:
        """The oracle source text is byte-frozen (sha256 pin)."""
        digest = hashlib.sha256(REFERENCE_PATH.read_bytes()).hexdigest()
        assert digest == REFERENCE_SHA256, (
            "_reference_fold.py drifted from its frozen pin — the oracle may "
            "never change silently; re-pin ONLY with a reviewed oracle change "
            f"(got {digest})"
        )

    def test_imports_are_stdlib_closed_set(self) -> None:
        """Every import resolves inside the closed stdlib allow-set."""
        tree = ast.parse(REFERENCE_PATH.read_text(encoding="utf-8"))
        seen: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    seen.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                assert node.module is not None, "relative import in the oracle"
                seen.add(node.module.split(".")[0])
        assert seen <= _ALLOWED_IMPORTS, (
            f"oracle imports outside the closed stdlib set: {sorted(seen - _ALLOWED_IMPORTS)}"
        )

    def test_no_forbidden_name_appears_anywhere(self) -> None:
        """No ``brutex``/``fno_math`` reference in ANY name/attribute/string.

        Wider than the import walk: a lazy ``importlib.import_module("...")``
        or ``getattr`` reach-through would also surface here. DOCSTRINGS are
        exempt (they legitimately NAME the independence contract); every
        other string constant is scanned.
        """
        tree = ast.parse(REFERENCE_PATH.read_text(encoding="utf-8"))
        docstrings: set[int] = set()
        for scope in ast.walk(tree):
            if isinstance(
                scope, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
            ):
                body = scope.body
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id not in _FORBIDDEN_NAMES, f"forbidden name {node.id!r}"
            elif isinstance(node, ast.Attribute):
                assert node.attr not in _FORBIDDEN_NAMES, f"forbidden attr {node.attr!r}"
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
            ):
                for name in _FORBIDDEN_NAMES:
                    assert name not in node.value, (
                        f"forbidden literal {name!r} inside a string constant at line {node.lineno}"
                    )

    def test_reference_is_importable_and_total(self) -> None:
        """Smoke: the frozen module imports and folds a tiny stream."""
        from tests.rust_differential._reference_fold import reference_fold_row

        ints, floats, flags = reference_fold_row([(100, 150, 0, 2, 3)], 5, 10_000)
        assert flags == 0
        assert ints[0] == 1 and ints[1] == 100
        assert len(ints) == 15 and len(floats) == 9
