"""DEC-SWEEP-ENUM-RUST-001 — MIGRATION.md #1738 commit-attribution correction pin.

Regression net for the review-round-4 refuter-confirmed NIT: the
merge-reconcile hook-env commit 57859da's message attributes its
``sweep_cache.py`` blake3 wrap to ``sweep_cache._params_digest`` — a function
that does not exist anywhere in the repo. The file's SOLE blake3 call site —
the one the commit actually (and correctly) wrapped in ``bytes()`` — lives
inside :func:`brutex.engine.sweep_cache.combo_digest`. A published commit
message can never be amended (CLAUDE.md §3 no-amend law), so the durable
correction is the REVIEW ROUND 4 clause of MIGRATION.md entry #1738; these
pins keep that correction present AND accurate:

1. entry #1738 attributes the wrap to ``combo_digest`` while naming the
   mislabel token ``_params_digest`` (so a search for the phantom name finds
   the correction, not dead air) — fails-before: the entry carried neither
   token until the round-4 fold;
2. ``_params_digest`` stays nonexistent in ``sweep_cache.py`` (if such a
   function ever appears, the correction's "does not exist" claim rots into
   a half-truth and must be rewritten consciously, not silently); and
3. the module's blake3 ``.digest()`` call still lives inside
   ``combo_digest`` — the source-truth anchor the correction points at.

Golden Rule #10 — gate/provenance evidence must be accurate; the ledger is
the single source of truth for the historical record.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = REPO_ROOT / "docs" / "MIGRATION.md"
SWEEP_CACHE_PATH = REPO_ROOT / "brutex" / "engine" / "sweep_cache.py"

#: The phantom function name commit 57859da's message cites.
MISLABEL_TOKEN = "_params_digest"

#: The function that actually owns the wrapped blake3 call site.
ACTUAL_FUNCTION = "combo_digest"


def _entry_1738() -> str:
    """Return the full single-line MIGRATION.md entry #1738."""
    text = MIGRATION_PATH.read_text(encoding="utf-8")
    match = re.search(r"^1738\. .*$", text, flags=re.MULTILINE)
    assert match is not None, "MIGRATION.md entry #1738 not found"
    return match.group(0)


class TestMigration1738CommitAttribution:
    """The round-4 correction is present in #1738 and stays accurate."""

    def test_correction_attributes_the_wrap_to_combo_digest(self) -> None:
        """#1738 names combo_digest as the wrapped site AND the mislabel.

        Fails-before: prior to the round-4 fold the entry contained neither
        token, so the only record of the sweep_cache wrap's true home was a
        commit message naming a nonexistent function.
        """
        entry = _entry_1738()
        assert ACTUAL_FUNCTION in entry, (
            "MIGRATION.md #1738 must attribute commit 57859da's sweep_cache "
            f"wrap to `{ACTUAL_FUNCTION}` (the round-4 correction clause)"
        )
        assert MISLABEL_TOKEN in entry, (
            "MIGRATION.md #1738 must NAME the mislabel token "
            f"`{MISLABEL_TOKEN}` so a search for the phantom function finds "
            "the correction"
        )

    def test_mislabel_target_stays_nonexistent_in_sweep_cache(self) -> None:
        """`_params_digest` must not exist in sweep_cache.py.

        The correction states the mislabeled function does not exist; if one
        is ever added under that name the correction becomes stale and must
        be rewritten consciously — this pin forces that.
        """
        source = SWEEP_CACHE_PATH.read_text(encoding="utf-8")
        assert MISLABEL_TOKEN not in source, (
            f"`{MISLABEL_TOKEN}` now exists in sweep_cache.py — the #1738 "
            "round-4 correction ('does not exist') is stale; rewrite the "
            "correction, do not let it rot into a half-truth"
        )

    def test_blake3_digest_call_lives_inside_combo_digest(self) -> None:
        """The source-truth anchor: the blake3 call sits in combo_digest.

        Guards the correction against source drift — if the call site moves
        to another function, #1738's attribution must be re-verified.
        """
        source = SWEEP_CACHE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        combo = next(
            (
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == ACTUAL_FUNCTION
            ),
            None,
        )
        assert combo is not None, f"{ACTUAL_FUNCTION} not found in sweep_cache.py"
        segment = ast.get_source_segment(source, combo)
        assert segment is not None
        assert "blake3.blake3(" in segment and ".digest()" in segment, (
            f"the blake3 .digest() call no longer lives inside "
            f"{ACTUAL_FUNCTION} — re-verify the #1738 round-4 attribution"
        )
