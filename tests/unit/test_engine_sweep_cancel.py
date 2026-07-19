"""Unit tests for the durable cooperative-cancel sidecar (`brutex.engine.sweep_cancel`).

DEC-JOB-COOPERATIVE-CANCEL-001 — every public symbol + every branch for the
strict 100% line + branch gate (CLAUDE.md §4 / Golden Rule #12):

* absent sidecar → ``cancel_requested`` False; present → True (presence IS the
  signal — a MALFORMED body still cancels, the documented fail-safe);
* ``request_cancel`` writes an atomic, forensics-carrying JSON payload and
  creates the root;
* a non-ENOENT probe ``OSError`` reads True (fail-safe toward the harmless
  graceful stop) with one loud warning;
* ``clear_cancel`` returns whether a file existed; a non-ENOENT clear
  ``OSError`` is loud + False (never a crash);
* ``make_cancel_probe`` is a live closure over the sidecar state, and the hot
  probe costs exactly ONE ``os.stat`` (the O(1)-per-tuple discipline, spied).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from brutex.engine import sweep_cancel
from brutex.engine.sweep_cancel import (
    CANCEL_SIDECAR_NAME,
    CANCEL_SIDECAR_VERSION,
    cancel_requested,
    cancel_sidecar_path,
    clear_cancel,
    make_cancel_probe,
    request_cancel,
)


class TestSidecarPath:
    def test_path_is_root_sibling(self, tmp_path: Path) -> None:
        assert cancel_sidecar_path(tmp_path) == tmp_path / CANCEL_SIDECAR_NAME
        assert CANCEL_SIDECAR_NAME == ".sweep_cancel.json"


class TestRequestCancel:
    def test_writes_forensic_payload_and_creates_root(self, tmp_path: Path) -> None:
        root = tmp_path / "sweeps"  # does not exist yet — request creates it
        path = request_cancel(root, source="route")
        assert path == cancel_sidecar_path(root)
        assert path.is_file()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["version"] == CANCEL_SIDECAR_VERSION
        assert payload["source"] == "route"
        assert "requested_at_utc" in payload

    def test_idempotent_rewrite(self, tmp_path: Path) -> None:
        request_cancel(tmp_path, source="a")
        request_cancel(tmp_path, source="b")  # second request overwrites atomically
        assert json.loads(cancel_sidecar_path(tmp_path).read_text())["source"] == "b"


class TestCancelRequested:
    def test_absent_reads_false(self, tmp_path: Path) -> None:
        assert cancel_requested(tmp_path) is False

    def test_present_reads_true(self, tmp_path: Path) -> None:
        request_cancel(tmp_path, source="test")
        assert cancel_requested(tmp_path) is True

    def test_malformed_body_still_cancels(self, tmp_path: Path) -> None:
        """Presence IS the signal — the hot probe never parses the body."""
        cancel_sidecar_path(tmp_path).write_text("{not json", encoding="utf-8")
        assert cancel_requested(tmp_path) is True

    def test_probe_oserror_fails_safe_true(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-ENOENT probe error biases to the harmless graceful stop."""

        def _boom(_path: object) -> os.stat_result:
            raise PermissionError("hostile inode")

        monkeypatch.setattr(sweep_cancel.os, "stat", _boom)
        assert cancel_requested(tmp_path) is True

    def test_probe_is_one_os_stat(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """O(1)-per-tuple discipline: the hot probe is exactly ONE os.stat call."""
        calls: list[object] = []
        real_stat = os.stat

        def _spy(path: object, **kwargs: object) -> os.stat_result:
            calls.append(path)
            return real_stat(path, **kwargs)  # type: ignore[arg-type, call-overload]

        monkeypatch.setattr(sweep_cancel.os, "stat", _spy)
        assert cancel_requested(tmp_path) is False
        assert len(calls) == 1
        assert calls[0] == cancel_sidecar_path(tmp_path)


class TestClearCancel:
    def test_clear_absent_returns_false(self, tmp_path: Path) -> None:
        assert clear_cancel(tmp_path) is False

    def test_clear_present_returns_true_and_removes(self, tmp_path: Path) -> None:
        request_cancel(tmp_path, source="stale")
        assert clear_cancel(tmp_path) is True
        assert cancel_requested(tmp_path) is False

    def test_clear_oserror_is_loud_not_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        request_cancel(tmp_path, source="stuck")

        def _boom(_path: object) -> None:
            raise PermissionError("read-only volume")

        monkeypatch.setattr(sweep_cancel.os, "unlink", _boom)
        assert clear_cancel(tmp_path) is False  # loud (warned), never a crash


class TestMakeCancelProbe:
    def test_probe_tracks_live_sidecar_state(self, tmp_path: Path) -> None:
        probe = make_cancel_probe(tmp_path)
        assert probe() is False
        request_cancel(tmp_path, source="mid-run")
        assert probe() is True
        clear_cancel(tmp_path)
        assert probe() is False
