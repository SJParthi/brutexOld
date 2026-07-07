"""Unit tests for :mod:`brutex.backup.schedule_render` (DEC-BACKUP-OFFBOX-001 §9).

Golden byte-equality pins between each renderer and its generated docs copy
(``docs/runbooks/backup-templates/*.plist`` can never drift — graft B), the
one-shot NO-``KeepAlive`` guard, the no-secrets/{{PLACEHOLDER}} law, the
dead-man plist's deliberate NO-repo-env pin, and the cron-fallback golden.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from brutex.backup.schedule_render import (
    render_backup_plist,
    render_cron_lines,
    render_deadman_plist,
    render_drill_plist,
)

_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "docs" / "runbooks" / "backup-templates"


def _docs_copy(name: str) -> str:
    return (_TEMPLATES_DIR / name).read_text(encoding="utf-8")


class TestGoldenDocsCopies:
    def test_backup_plist_golden_equals_docs_copy(self) -> None:
        assert render_backup_plist() == _docs_copy("com.brutex.backup.plist")

    def test_deadman_plist_golden_equals_docs_copy(self) -> None:
        assert render_deadman_plist() == _docs_copy("com.brutex.backup-deadman.plist")

    def test_drill_plist_golden_equals_docs_copy(self) -> None:
        assert render_drill_plist() == _docs_copy("com.brutex.backup-drill.plist")


class TestTemplateLaw:
    @pytest.mark.parametrize(
        "render", [render_backup_plist, render_deadman_plist, render_drill_plist]
    )
    def test_no_keepalive_in_any_plist(self, render: object) -> None:
        """All three jobs are ONE-SHOT — KeepAlive spin-loops a one-shot."""
        assert "KeepAlive" not in render()  # type: ignore[operator]

    def test_backup_plist_shape(self) -> None:
        """Nightly 03:35, run subcommand, placeholders only — never a secret value."""
        plist = render_backup_plist()
        assert "<string>backup</string><string>run</string>" in plist
        assert "<key>Hour</key><integer>3</integer><key>Minute</key><integer>35</integer>" in plist
        assert "{{BRUTEX_BACKUP_PASSWORD_FILE}}" in plist  # a PATH placeholder
        assert "RESTIC_PASSWORD" not in plist.replace("BRUTEX_BACKUP_PASSWORD_FILE", "")
        assert "<key>RunAtLoad</key><false/>" in plist

    def test_deadman_plist_no_repo_env(self) -> None:
        """The shallow check must work when the backup env is broken — NO repo env."""
        plist = render_deadman_plist()
        assert "<string>backup</string><string>check</string>" in plist
        assert "<key>StartInterval</key><integer>3600</integer>" in plist
        assert "<key>RunAtLoad</key><true/>" in plist
        assert "<key>BRUTEX_BACKUP_REPO</key>" not in plist
        # The env-var NAME appears only in the no-secrets comment, never as an env key.
        assert "<key>BRUTEX_BACKUP_PASSWORD_FILE</key>" not in plist
        assert "{{BRUTEX_BACKUP_HEALTHCHECK_URL}}" in plist

    def test_drill_plist_monthly(self) -> None:
        plist = render_drill_plist()
        assert "<string>backup</string><string>restore-drill</string>" in plist
        assert "<key>Day</key><integer>1</integer>" in plist
        assert "<key>Hour</key><integer>4</integer><key>Minute</key><integer>30</integer>" in plist
        assert "{{BRUTEX_BACKUP_REPO}}" in plist  # the drill needs the full backup env

    def test_cron_lines_golden(self) -> None:
        """The §9.3 cron fallback block, byte-exact."""
        assert render_cron_lines() == (
            "35 3 * * *  BRUTEX_LAKE_ROOT=/path/lake /path/venv/bin/brutex backup run"
            "           >> ~/.brutex/logs/backup.log 2>&1\n"
            "0  * * * *  /path/venv/bin/brutex backup check"
            "                                     >> ~/.brutex/logs/backup.log 2>&1\n"
            "30 4 1 * *  BRUTEX_LAKE_ROOT=/path/lake /path/venv/bin/brutex backup restore-drill"
            " >> ~/.brutex/logs/backup.log 2>&1\n"
        )
