"""G3 drift pin — the epoch-anchored legacy resampler stays OUT of production paths.

DEC-SUSPICION-SWEEP-FIXES-001 / DEC-RESAMPLE-ANCHOR-002 context: BruteX carries
TWO resamplers — the canonical SESSION-anchored ``brutex.data.tf_resampler``
(the lake/derive/engine law: buckets anchor at 09:15 IST per session) and the
legacy EPOCH-anchored ``brutex.feed.resampler.resample_bars`` (midnight-epoch
buckets, kept for the feed package's exports + its historical tests). If a
production module ever quietly imported ``resample_bars``, its buckets would
silently disagree with every stored derived timeframe — the dual-resampler
split drifting into a production path with no error, only wrong bars.

This test mechanically pins the split: an AST walk over EVERY ``brutex/**/*.py``
asserts ``resample_bars`` is imported / referenced by NO production module
outside the sanctioned set (its defining module + the ``brutex.feed`` package
re-export). Docstring/comment mentions are naturally ignored (AST sees code
only). Adding a new import site fails this test and forces a conscious
decision, never a silent drift.
"""

from __future__ import annotations

import ast
from pathlib import Path

import brutex

#: The ONLY production files allowed to bind the name ``resample_bars``:
#: its defining module and the package re-export. Everything else — including
#: every other ``brutex.feed`` module — must go through the session-anchored
#: ``brutex.data.tf_resampler.resample_1m_to_tf``.
_ALLOWED = {
    Path("feed") / "resampler.py",
    Path("feed") / "__init__.py",
}

_PACKAGE_ROOT = Path(brutex.__file__).resolve().parent


def _references_resample_bars(tree: ast.AST) -> bool:
    """True iff the module imports or references the ``resample_bars`` symbol."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if any(alias.name == "resample_bars" for alias in node.names):
                return True
        elif (
            (isinstance(node, ast.Name) and node.id == "resample_bars")
            or (isinstance(node, ast.Attribute) and node.attr == "resample_bars")
            # the defining site itself (sanctioned-site liveness check)
            or (isinstance(node, ast.FunctionDef) and node.name == "resample_bars")
        ):
            return True
    return False


class TestNoProductionEpochResamplerImport:
    def test_resample_bars_imported_by_no_production_module(self) -> None:
        offenders: list[str] = []
        for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
            rel = path.relative_to(_PACKAGE_ROOT)
            if rel in _ALLOWED:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            if _references_resample_bars(tree):
                offenders.append(str(rel))
        assert offenders == [], (
            f"production modules import/reference the EPOCH-anchored legacy "
            f"resample_bars: {offenders} — production resampling must use the "
            "session-anchored brutex.data.tf_resampler (DEC-RESAMPLE-ANCHOR-002); "
            "an epoch-bucketed path silently disagrees with every stored derived TF."
        )

    def test_the_sanctioned_sites_still_exist(self) -> None:
        """The allowlist stays honest: the definition + the re-export are real
        (a rename/move must update this pin consciously, not bypass it)."""
        for rel in _ALLOWED:
            path = _PACKAGE_ROOT / rel
            assert path.is_file(), f"sanctioned resample_bars site vanished: {rel}"
            assert _references_resample_bars(ast.parse(path.read_text(encoding="utf-8"))), (
                f"sanctioned site {rel} no longer references resample_bars — "
                "update the G3 allowlist to match the new layout"
            )
