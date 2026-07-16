"""AST wiring pin for the Pin D1-1 caller binding (DEC-WRITE-DOMAIN-CALLER-BINDING-001).

The two LIVE production pull sink builders are ``# pragma: no cover``
(real TOTP credentials + network — absent in CI), so their new
``write_domain=primary_write_domain(lake_root)`` binding cannot be pinned by
an executing unit test. Per the house dormant-binding-trap convention
(the #935/#1014 lesson; the DEC-VIX-STAMP-WIRE-001 AST-pin precedent) this
pin walks each builder's AST and asserts, POSITIVELY, that its
``ParquetWriter(...)`` construction carries the
``write_domain=primary_write_domain(lake_root)`` kwarg — deleting the kwarg
(re-dormanting the containment) or rebinding it to anything else fails
loudly.

HONEST ENVELOPE (Golden Rule #10): a STATIC source pin — the binding's
BEHAVIOR (refusal + A/B byte-identity) is proven by
``tests/unit/test_parquet_writer_write_domain.py`` through the real writer
path; this pin owns only the never-silently-unbound invariant on the two
live builders.
"""

from __future__ import annotations

import ast
import inspect
from typing import Final

from brutex.dashboard import historical_pull, pull_controller

#: (module, builder function name) — the two live production pull sink
#: builders the binding commit arms (the #1833 deferral-register item (a)).
_BOUND_BUILDERS: Final = (
    (pull_controller, "_build_live_spot_adapter"),
    (historical_pull, "_build_live_client"),
)


def _function_node(module: object, name: str) -> ast.FunctionDef:
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found in {module!r}")


def _parquet_writer_calls(fn: ast.FunctionDef) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ParquetWriter"
        ):
            calls.append(node)
    return calls


class TestLiveBuildersBindThePrimaryWriteDomain:
    """The positive-presence law on both pragma-no-cover live builders."""

    def test_each_builder_constructs_exactly_one_domain_bound_writer(self) -> None:
        for module, name in _BOUND_BUILDERS:
            fn = _function_node(module, name)
            calls = _parquet_writer_calls(fn)
            assert len(calls) == 1, (
                f"{module.__name__}.{name} must construct exactly ONE ParquetWriter "
                f"(found {len(calls)}) — the single minted-dest chokepoint law"
            )
            (call,) = calls
            kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg is not None}
            assert "write_domain" in kwargs, (
                f"{module.__name__}.{name}: the ParquetWriter construction lost its "
                "write_domain kwarg — the Pin D1-1 caller binding has been silently "
                "re-dormanted (DEC-WRITE-DOMAIN-CALLER-BINDING-001)"
            )
            value = kwargs["write_domain"]
            assert (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "primary_write_domain"
            ), (
                f"{module.__name__}.{name}: write_domain must be bound to "
                f"primary_write_domain(...) — found {ast.dump(value)}"
            )
            assert [arg.id for arg in value.args if isinstance(arg, ast.Name)] == ["lake_root"], (
                f"{module.__name__}.{name}: primary_write_domain must anchor on the "
                "builder's own lake_root (the ONE physical lake root — ADR-0021 "
                "Decision 1)"
            )

    def test_each_builder_lazy_imports_the_domain_constructor(self) -> None:
        """The builders lazy-import per the house pattern — the import must
        live INSIDE the function so the dashboard module never becomes an
        import hub over the write-domain leaf at module level."""
        for module, name in _BOUND_BUILDERS:
            fn = _function_node(module, name)
            imported = [
                alias.name
                for node in ast.walk(fn)
                if isinstance(node, ast.ImportFrom) and node.module == "brutex.data.write_domain"
                for alias in node.names
            ]
            assert imported == ["primary_write_domain"], (
                f"{module.__name__}.{name}: expected the lazy "
                f"'from brutex.data.write_domain import primary_write_domain' — "
                f"found {imported}"
            )
