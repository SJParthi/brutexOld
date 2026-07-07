"""DEC-LEDGER-DELTA-O-PAGE-001 — MIGRATION.md #1522 Files-inventory pin.

Regression net for the fix-round ledger-completeness finding: the #1522
``**Files:**`` list omitted five touched files (``.github/workflows/ci.yml``
plus four MODIFIED pre-existing test suites) while naming their siblings,
and an earlier round cited a wrong filename (``tests/unit/test_lake_read.py``
for ``tests/unit/test_data_lake_read.py``). Two permanent pins:

1. every file in the change set's inventory is NAMED in the entry's
   ``**Files:**`` list (kills the omission class), and
2. every path the list names actually EXISTS in the repo (kills the
   wrong-filename class).

Golden Rule #2/#8 — the ledger is the single source of truth; its file
inventory must match the change set it documents.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = REPO_ROOT / "docs" / "MIGRATION.md"

#: The complete DEC-LEDGER-DELTA-O-PAGE-001 change-set inventory
#: (``git diff origin/main...HEAD --name-only`` at fix time, plus this pin).
CHANGE_SET_FILES: tuple[str, ...] = (
    ".github/workflows/ci.yml",
    "CLAUDE.md",
    "brutex/dashboard/app.py",
    "brutex/dashboard/db_view.py",
    "brutex/dashboard/lake_revision.py",
    "brutex/dashboard/movers_cache.py",
    "brutex/dashboard/static/bx-liveproof.js",
    "brutex/dashboard/static/bx-rate-monitor.js",
    "brutex/dashboard/summary_cache.py",
    "brutex/dashboard/sweep_results_view.py",
    "brutex/data/lake_read.py",
    "docs/MIGRATION.md",
    "tests/bench/test_ledger_delta_o_page_o1.py",
    "tests/unit/test_dashboard_db_view.py",
    "tests/unit/test_dashboard_db_view_ledger_delta_fold.py",
    "tests/unit/test_dashboard_db_view_ledger_read_cache.py",
    "tests/unit/test_dashboard_db_view_window_rows_cache.py",
    "tests/unit/test_dashboard_results_revision_key.py",
    "tests/unit/test_dashboard_results_route.py",
    "tests/unit/test_dashboard_widget_visibility_pause.py",
    "tests/unit/test_dashboard_window_payload_retry.py",
    "tests/unit/test_data_lake_read.py",
    "tests/unit/test_migration_ledger_1522_files.py",
    "tests/unit/test_summary_cache.py",
    "tests/unit/test_sweep_results_scan_split.py",
)


def _entry_1522_listed_files() -> list[str]:
    """Extract the backticked paths of entry #1522's ``**Files:**`` list."""
    text = MIGRATION_PATH.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("1522. "):
            match = re.search(r"\*\*Files:\*\*(.*?)\*\*Decision ID:\*\*", line)
            assert match is not None, (
                "entry #1522 lacks a '**Files:** ... **Decision ID:**' section"
            )
            listed = re.findall(r"`([^`]+)`", match.group(1))
            assert listed, "entry #1522 **Files:** list is empty"
            return listed
    raise AssertionError("MIGRATION.md entry #1522 not found")


def test_entry_1522_names_every_change_set_file() -> None:
    """Every file the change set touches appears in the #1522 Files list.

    Fails-before/passes-after: pre-fix the list omitted
    ``.github/workflows/ci.yml``, ``tests/unit/test_dashboard_db_view.py``,
    ``tests/unit/test_dashboard_results_route.py``,
    ``tests/unit/test_dashboard_window_payload_retry.py`` and
    ``tests/unit/test_summary_cache.py``.
    """
    listed = set(_entry_1522_listed_files())
    missing = [path for path in CHANGE_SET_FILES if path not in listed]
    assert not missing, f"MIGRATION.md #1522 **Files:** list omits: {missing}"


def test_entry_1522_lists_no_phantom_paths() -> None:
    """Every path the #1522 Files list names exists in the repo.

    The round-4 defect class: the entry cited a non-existent
    ``tests/unit/test_lake_read.py`` for ``tests/unit/test_data_lake_read.py``.
    """
    phantoms = [path for path in _entry_1522_listed_files() if not (REPO_ROOT / path).is_file()]
    assert not phantoms, f"MIGRATION.md #1522 **Files:** names phantom paths: {phantoms}"
