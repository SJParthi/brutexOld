"""Tests for `brutex.dashboard.lake_revision` (DEC-HARDENING-LEFTOVERS-001).

100% line + branch on the O(1) cross-process lake freshness token:

* a lake with NO window ledger yields the stable :data:`ABSENT_TOKEN` (and the
  sentinel is stable across repeat probes — no oscillation);
* a present ledger yields the ``"<mtime_ns>:<size>"`` stat token, which CHANGES
  after a writer touches the ledger (the CLI-checkpoint signal);
* any non-missing-file :class:`OSError` (e.g. permissions) maps to ``None`` —
  "unknown, no signal" — never a raise, never a spurious invalidation.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from brutex.dashboard.lake_revision import ABSENT_TOKEN, lake_revision
from brutex.data.window_ledger_store import ledger_path


def _write_ledger_bytes(lake_root: Path, payload: bytes) -> Path:
    """Write raw *payload* at the COMMON ledger path (a stat-only stand-in)."""
    target = ledger_path(lake_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target


def test_absent_ledger_returns_stable_sentinel(tmp_path: Path) -> None:
    """No ledger file → the stable ``absent`` sentinel, on every probe."""
    assert lake_revision(tmp_path) == ABSENT_TOKEN
    # Stable: a second probe of the same absent lake returns the SAME token
    # (the sentinel never oscillates a generation).
    assert lake_revision(tmp_path) == ABSENT_TOKEN


def test_present_ledger_token_matches_stat(tmp_path: Path) -> None:
    """A present ledger yields the exact ``"<mtime_ns>:<size>"`` stat token."""
    target = _write_ledger_bytes(tmp_path, b"v1")
    st = os.stat(target)
    assert lake_revision(tmp_path) == f"{st.st_mtime_ns}:{st.st_size}"


def test_token_changes_after_a_ledger_write(tmp_path: Path) -> None:
    """A writer touching the ledger (the CLI checkpoint) moves the token."""
    _write_ledger_bytes(tmp_path, b"v1")
    first = lake_revision(tmp_path)
    # A second checkpoint with different content — size (and mtime) move.
    _write_ledger_bytes(tmp_path, b"v2-longer")
    second = lake_revision(tmp_path)
    assert first is not None and second is not None
    assert second != first
    # And the absent→present transition also moved the token.
    assert first != ABSENT_TOKEN


def test_unreadable_ledger_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-missing-file OSError (permissions) → ``None`` ("no signal")."""
    target = _write_ledger_bytes(tmp_path, b"v1")
    real_stat = os.stat

    def _denying_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if str(path) == str(target):
            raise PermissionError(13, "denied", str(target))
        return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]  # passthrough shim

    monkeypatch.setattr(os, "stat", _denying_stat)
    assert lake_revision(tmp_path) is None
