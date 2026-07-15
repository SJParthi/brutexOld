"""Bare-``run_in_threadpool`` drift pin over ``brutex/dashboard/`` (#1757).

DEC-DASHBOARD-LAKE-IO-LIMITER-001/-002 context: every LAKE-touching dashboard
offload rides the app's OWN 32-token ``LakeOffload`` limiter — ``_run_lake``
in ``app.py``, the ``run_lake=`` injection seam in ``launch_button.py`` — so a
WEDGED lake volume pins at most the 32 lake tokens and can never starve the
DEFAULT 40-token AnyIO pool the liveness / CPU / local-disk offloads live on
(MIGRATION.md #1642). The -002 slice closed the two stragglers that regressed
past -001 unnoticed: ``GET /api/v1/oneclick/equity`` (the #1665 merge-seam
route, authored on a parallel branch under the pre-#1642 comment doctrine) and
the THREE ``/api/v1/launch/*`` offloads (#1642's own HONEST-LIMITS named
follow-up). Both were invisible to the suite because nothing pinned WHICH pool
a dispatch rides — this file is that pin, the mechanical ratchet.

THE RULE: a bare ``run_in_threadpool`` CALL in ``brutex/dashboard/*.py`` is
sanctioned ONLY on the enumerated default-pool set below — the five are
enumerated CENTRALLY at app.py's ``_run_lake`` doctrine comment AND each
carries an at-site "Deliberately on the DEFAULT AnyIO pool" naming, with the
ledger record in #1642's deliberate-keep list + #1757. Counter
EQUALITY over ``(file, enclosing-def)`` — the #1492-G3 drift-pin pattern
crossed with the exact-count enumeration of the VIX stamper binding pin — so:

* a NEW bare call anywhere (a new route, or a SECOND call inside an
  already-sanctioned function) FAILS — route it through ``_run_lake`` /
  the ``run_lake=`` seam, or ledger a fresh DEC + sanction it here;
* a REMOVED sanctioned site ALSO fails (the ratchet — deleting/converting a
  deliberate default-pool site is a conscious allowlist edit, never silent).

Deliberately import-free of the ``[ui]`` extra (pure ``ast``/``pathlib`` over
the installed package sources — no importorskip, no fastapi), so it is
collected and fails on a reverted tree in ANY environment: the guaranteed
``bugfix-reversion-check`` kill for this fix.

Honest envelope (Golden Rule #10)
---------------------------------
An ALIASED call (``pool = run_in_threadpool; await pool(f)``) evades this
detector — a Name REFERENCE carries no ``run_in_threadpool`` Call node. That
is a DISCLOSED residual, owned by the behavioral pins (the wedged-lake
realserver regression's ``held_peak <= 32`` + the counting-offload dispatch
pins in ``test_dashboard_eventloop_offload.py`` /
``test_dashboard_launch_button.py``). The launch fallback
``_offload = run_lake if run_lake is not None else run_in_threadpool`` is
exactly that sanctioned Name-reference shape (the None-arm back-compat
default), which is why the importer-set belt below still admits
``launch_button.py``.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import brutex

_DASHBOARD = Path(brutex.__file__).resolve().parent / "dashboard"

#: DEC-DASHBOARD-LAKE-IO-LIMITER-001 deliberate default-pool set — enumerated
#: centrally at app.py's `_run_lake` doctrine comment AND named deliberate
#: in-code at each site (#1642's deliberate-keep list + #1757); a NEW entry
#: requires a ledgered DEC + an at-site "deliberately on the DEFAULT AnyIO
#: pool" comment (the #1642 doctrine).
_SANCTIONED: Counter[tuple[str, str]] = Counter(
    {
        ("app.py", "api_pull_rate"): 1,  # deadline-guarded liveness memo (#1522)
        ("app.py", "api_sweep_progress"): 1,  # deadline-guarded liveness memo (#1640)
        ("app.py", "api_preopen_xlsx"): 1,  # CPU render — rows already via _run_lake
        ("app.py", "api_preopen_zip"): 1,  # CPU render — rows already via _run_lake
        ("app.py", "api_logs"): 1,  # LOCAL rotating log (~/.brutex/logs), not the lake
    }
)

_FAILURE_HINT = (
    "a lake-touching dashboard closure must ride `_run_lake` (app.py) or the "
    "`run_lake=` seam (launch_button.py) — DEC-DASHBOARD-LAKE-IO-LIMITER-001/-002; "
    "if the new site is genuinely non-lake AND deadline-guarded/CPU-only, add it to "
    "_SANCTIONED with an in-code comment + a ledger entry"
)


def _bare_threadpool_calls(source: str) -> list[tuple[str, str]]:
    """``[(enclosing def name or '<module>', 'call')]`` per ``run_in_threadpool`` CALL.

    A Call whose func is ``Name('run_in_threadpool')`` or
    ``Attribute(attr='run_in_threadpool')`` (the ``concurrency.run_in_threadpool``
    spelling); the enclosing name is the innermost FunctionDef/AsyncFunctionDef
    on the visitor stack. Comments/docstrings never match (AST-level); a bare
    Name REFERENCE (aliasing) never matches — see the module docstring's
    honest envelope.
    """
    hits: list[tuple[str, str]] = []
    stack: list[str] = []

    class _Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            name: str | None = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name == "run_in_threadpool":
                hits.append((stack[-1] if stack else "<module>", "call"))
            self.generic_visit(node)

    _Visitor().visit(ast.parse(source))
    return hits


def _dashboard_sources() -> list[Path]:
    return sorted(_DASHBOARD.rglob("*.py"))


def _observed_counter() -> Counter[tuple[str, str]]:
    observed: Counter[tuple[str, str]] = Counter()
    for path in _dashboard_sources():
        # Keyed by dashboard-RELATIVE path (flat tree today, so == basename) —
        # a future dashboard/<subdir>/app.py can never alias the top-level key.
        rel = path.relative_to(_DASHBOARD).as_posix()
        for enclosing, _ in _bare_threadpool_calls(path.read_text(encoding="utf-8")):
            observed[(rel, enclosing)] += 1
    return observed


def test_no_unsanctioned_bare_threadpool_sites() -> None:
    """Counter EQUALITY: any new/removed bare default-pool call site fails."""
    observed = _observed_counter()
    assert observed == _SANCTIONED, (
        "bare run_in_threadpool CALL sites in brutex/dashboard/ drifted from the "
        f"sanctioned default-pool set.\n  observed:   {sorted(observed.items())}\n"
        f"  sanctioned: {sorted(_SANCTIONED.items())}\n  {_FAILURE_HINT}"
    )


def test_importers_are_exactly_the_two_sanctioned_modules() -> None:
    """Belt: only app.py + launch_button.py may import ``run_in_threadpool``.

    launch_button.py keeps the import for the ``run_lake=None`` fallback arm
    (a Name reference, not a Call); a NEW module adopting the bare pool
    wholesale trips this even if it aliases the callable past the Call scan.
    """
    importers: set[str] = set()
    for path in _dashboard_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "fastapi.concurrency"
                and any(alias.name == "run_in_threadpool" for alias in node.names)
            ):
                importers.add(path.name)
    assert importers == {"app.py", "launch_button.py"}, (
        f"run_in_threadpool importers drifted: {sorted(importers)} — {_FAILURE_HINT}"
    )


def test_scan_is_not_vacuous() -> None:
    """The G3 liveness twin: the scan must genuinely see the dashboard tree."""
    sources = _dashboard_sources()
    assert len(sources) >= 30, (
        f"only {len(sources)} dashboard modules scanned under {_DASHBOARD} — "
        "a tree move silently emptied the scan"
    )
    names = {path.name for path in sources}
    assert "app.py" in names and "launch_button.py" in names
    app_source = (_DASHBOARD / "app.py").read_text(encoding="utf-8")
    # 58 sites at authoring — a rename of `_run_lake` must consciously update
    # this pin (and the allowlist rule text) rather than silently hollowing
    # the "rides the lake limiter" claim.
    assert app_source.count("await _run_lake(") >= 50, (
        "app.py no longer awaits `_run_lake(` at least 50 times — the lake "
        "limiter was renamed or unwired; update this pin consciously"
    )
    observed = _observed_counter()
    for key in _SANCTIONED:
        assert observed[key] >= 1, (
            f"sanctioned site {key} not observed — the detector or the site moved"
        )


class TestDetectorTeeth:
    """In-memory fixtures prove the detector itself fires (#1180 harness)."""

    def test_bare_call_inside_async_def_detected(self) -> None:
        source = "async def h():\n    await run_in_threadpool(read_lake)\n"
        assert _bare_threadpool_calls(source) == [("h", "call")]

    def test_lake_limiter_dispatch_not_flagged(self) -> None:
        source = "async def h():\n    return await _run_lake(_resolve)\n"
        assert _bare_threadpool_calls(source) == []

    def test_module_level_call_detected(self) -> None:
        source = "run_in_threadpool(f)\n"
        assert _bare_threadpool_calls(source) == [("<module>", "call")]

    def test_attribute_form_detected(self) -> None:
        source = "async def h():\n    await concurrency.run_in_threadpool(f)\n"
        assert _bare_threadpool_calls(source) == [("h", "call")]

    def test_comment_and_docstring_mentions_not_flagged(self) -> None:
        source = (
            '"""offloaded via run_in_threadpool(...) — prose only."""\n'
            "# run_in_threadpool(f) in a comment never counts\n"
            "def h():\n"
            '    """calls run_in_threadpool(g) — still prose."""\n'
            "    return 1\n"
        )
        assert _bare_threadpool_calls(source) == []

    def test_aliasing_reference_without_call_not_flagged(self) -> None:
        """The disclosed residual shape — and the launch None-arm fallback."""
        source = "pool = run_in_threadpool\n_offload = x if x is not None else run_in_threadpool\n"
        assert _bare_threadpool_calls(source) == []

    def test_nested_def_attributes_to_innermost(self) -> None:
        source = (
            "async def outer():\n"
            "    def inner():\n"
            "        return run_in_threadpool(f)\n"
            "    await run_in_threadpool(g)\n"
        )
        assert _bare_threadpool_calls(source) == [("inner", "call"), ("outer", "call")]
