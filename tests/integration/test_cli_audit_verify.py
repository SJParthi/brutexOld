"""End-to-end CLI tests for ``brutex audit verify`` auto-detect
(DEC-AUDIT-VERIFY-UNIFY-001).

Proves the operability gap is closed: the BLAKE3 sweep-supervisor chain that
previously raised a FALSE ``AUDIT CHAIN CORRUPT`` now verifies cleanly, and the
exit-code contract (0 OK / 1 user error / 2 corruption) holds across both
formats and the ``--format`` override. ``brutex/cli.py`` is coverage-omitted
(per pyproject [tool.coverage.run].omit), so these live in integration to prove
the wiring rather than to hit a coverage gate.
"""

from __future__ import annotations

import json
from pathlib import Path

from brutex import cli
from brutex.engine.sweep_audit import SweepAuditWriter
from brutex.orchestrator.audit import AuditWriter


def _write_orch(path: Path, n: int = 3) -> None:
    with AuditWriter.open(path) as log:
        for i in range(n):
            log.append(action="STEP", subject=f"s{i}", payload={"i": i}, ts_utc=float(i))


def _write_sweep(path: Path, n: int = 3) -> None:
    with SweepAuditWriter.open(path) as log:
        for i in range(n):
            log.append(action="TUPLE_DONE", subject=f"c{i}", payload={"i": i}, ts_utc=float(i))


def test_cli_auto_verifies_sweep_supervisor_chain(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    """The bug: a valid BLAKE3 supervisor chain must NOT be reported corrupt."""
    p = tmp_path / "sweep-all-supervisor.jsonl"
    _write_sweep(p)
    rc = cli.main(["audit", "verify", str(p)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "audit chain OK [sweep]" in out
    assert "3 entries verified" in out


def test_cli_auto_verifies_orchestrator_chain(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "sweep-all.jsonl"
    _write_orch(p)
    rc = cli.main(["audit", "verify", str(p)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "audit chain OK [orchestrator]" in out


def test_cli_format_override_sweep(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "sup.jsonl"
    _write_sweep(p)
    rc = cli.main(["audit", "verify", str(p), "--format", "sweep"])
    assert rc == 0
    assert "[sweep]" in capsys.readouterr().out


def test_cli_missing_file_is_user_error(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    rc = cli.main(["audit", "verify", str(tmp_path / "nope.jsonl")])
    assert rc == cli.EXIT_USER_ERROR
    assert "not found" in capsys.readouterr().err


def test_cli_corrupt_chain_is_internal_error(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "sup.jsonl"
    _write_sweep(p)
    lines = p.read_text(encoding="utf-8").splitlines()
    data = json.loads(lines[2])
    data["payload"] = {"i": 999}
    lines[2] = json.dumps(data)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rc = cli.main(["audit", "verify", str(p)])
    assert rc == cli.EXIT_INTERNAL_ERROR
    assert "AUDIT CHAIN CORRUPT" in capsys.readouterr().err


def test_cli_malformed_line_is_user_error(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "sup.jsonl"
    p.write_text("{not json\n", encoding="utf-8")
    rc = cli.main(["audit", "verify", str(p)])
    assert rc == cli.EXIT_USER_ERROR
    assert "unreadable" in capsys.readouterr().err


def test_cli_empty_file_is_valid(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "empty.jsonl"
    p.write_text("", encoding="utf-8")
    rc = cli.main(["audit", "verify", str(p)])
    assert rc == 0
    assert "[empty]" in capsys.readouterr().out


def test_cli_wrong_format_override_reports_corrupt(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    """Forcing --format orchestrator on a BLAKE3 chain → corruption (exit 2)."""
    p = tmp_path / "sup.jsonl"
    _write_sweep(p)
    rc = cli.main(["audit", "verify", str(p), "--format", "orchestrator"])
    assert rc == cli.EXIT_INTERNAL_ERROR
    assert "AUDIT CHAIN CORRUPT" in capsys.readouterr().err
