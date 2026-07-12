"""Tests for the Dhan auth audit (DEC-DHAN-AUTH-001) — hash chain, allowlist, fail-soft."""

from __future__ import annotations

from pathlib import Path

import pytest
from brutex.adapters.dhan.auth_audit import (
    ACTION_ADOPT,
    ACTION_MINT,
    DHAN_AUTH_ACTOR,
    DhanAuthAudit,
)
from brutex.orchestrator.audit import AuditWriter, verify_chain


def _audit(tmp_path: Path) -> tuple[DhanAuthAudit, Path]:
    path = tmp_path / "dhan-auth.jsonl"
    return DhanAuthAudit(path), path


def test_one_chained_line_per_action_and_verifies(tmp_path: Path) -> None:
    audit, path = _audit(tmp_path)
    audit.record(ACTION_MINT, subject="abcd1234", outcome="ok", fingerprint="abcd1234")
    audit.record(ACTION_ADOPT, subject="abcd1234", outcome="mint", fingerprint="abcd1234")
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    # The chain verifies + carries the dhan-auth actor.
    verify_chain(path)
    assert '"actor":"dhan-auth"' in lines[0]
    assert DHAN_AUTH_ACTOR == "dhan-auth"


def test_allowlist_rejects_secret_key(tmp_path: Path) -> None:
    audit, path = _audit(tmp_path)
    for bad in ("token", "pin", "totp", "whatever"):
        with pytest.raises(ValueError, match="disallowed key"):
            audit.record(ACTION_MINT, subject="x", **{bad: "SECRET"})
    # Nothing was written (the raise precedes the append).
    assert not path.exists()


def test_fail_soft_latches_off(tmp_path: Path) -> None:
    calls: list[int] = []

    def _boom_factory(_path: Path) -> AuditWriter:
        calls.append(1)
        if not _path.name:  # mypy can't prove this False → keeps a non-NoReturn return path
            return AuditWriter.open(_path)  # pragma: no cover — never taken at runtime
        raise OSError("disk full")

    audit = DhanAuthAudit(tmp_path / "x.jsonl", writer_factory=_boom_factory)
    audit.record(ACTION_MINT, subject="x", outcome="ok")  # first failure latches
    audit.record(ACTION_ADOPT, subject="x", outcome="ok")  # no second attempt (latched off)
    assert audit.degraded  # latched
    assert calls == [1]  # only ONE factory call — latched off after the first


def test_default_path_home() -> None:
    audit = DhanAuthAudit()
    # No public getter; assert the default resolves under the user's ~/.brutex/audit.
    from brutex.adapters.dhan.auth_audit import _default_audit_path

    assert _default_audit_path().name == "dhan-auth.jsonl"
    assert ".brutex" in str(_default_audit_path())
    assert audit.degraded is False
