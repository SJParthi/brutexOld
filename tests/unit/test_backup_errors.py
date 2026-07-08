"""Unit tests for :mod:`brutex.backup.errors` (DEC-BACKUP-OFFBOX-001 §1).

The exit-code vocabulary is the ONE source of truth the CLI, launchd logs and
every failure-mode test read — the exact numeric mapping is pinned here so a
renumbering can never slip through silently. The error hierarchy is pinned so
``except BackupError`` reliably catches every backup-domain defect.
"""

from __future__ import annotations

import pytest
from brutex.backup import errors
from brutex.backup.errors import (
    EXIT_BUSY,
    EXIT_CONFIG,
    EXIT_CORRUPTION,
    EXIT_DEADMAN_BREACH,
    EXIT_MAINTENANCE,
    EXIT_OK,
    EXIT_PREFLIGHT,
    EXIT_RESTIC,
    BackupBusyError,
    BackupError,
    BackupStateError,
)


class TestExitCodeVocabulary:
    """The deterministic exit-code triple contract (failure-mode matrix)."""

    def test_exact_numeric_mapping(self) -> None:
        """Each exit code carries its documented, load-bearing value."""
        assert EXIT_OK == 0
        assert EXIT_DEADMAN_BREACH == 1
        assert EXIT_CONFIG == 2
        assert EXIT_PREFLIGHT == 3
        assert EXIT_RESTIC == 4
        assert EXIT_BUSY == 5
        assert EXIT_CORRUPTION == 6
        assert EXIT_MAINTENANCE == 7

    def test_codes_are_distinct(self) -> None:
        """No two exit codes collide — each failure class is distinguishable."""
        codes = [
            EXIT_OK,
            EXIT_DEADMAN_BREACH,
            EXIT_CONFIG,
            EXIT_PREFLIGHT,
            EXIT_RESTIC,
            EXIT_BUSY,
            EXIT_CORRUPTION,
            EXIT_MAINTENANCE,
        ]
        assert len(set(codes)) == len(codes)

    def test_all_exports_exist(self) -> None:
        """``__all__`` names every public symbol and nothing dangles."""
        for name in errors.__all__:
            assert hasattr(errors, name)


class TestErrorHierarchy:
    """`BackupError` is the domain root; subclasses stay catchable via it."""

    def test_backup_error_is_exception(self) -> None:
        """The domain root derives from :class:`Exception` (never bare)."""
        assert issubclass(BackupError, Exception)

    @pytest.mark.parametrize("exc_type", [BackupStateError, BackupBusyError])
    def test_subclasses_derive_from_backup_error(self, exc_type: type[BackupError]) -> None:
        """State/busy errors are catchable as :class:`BackupError`."""
        assert issubclass(exc_type, BackupError)
        with pytest.raises(BackupError, match="boom"):
            raise exc_type("boom")
