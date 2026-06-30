"""Torn-tail recovery for the hash-chained audit logs (DEC-AUDIT-TORN-TAIL-001).

A crash / ``SIGKILL`` mid-append can leave a **torn tail** — an interrupted,
newline-LESS final line — on either audit log. Before this fix
``open(resume=True)`` (via ``_read_entries``) raised ``AuditError`` /
``SweepAuditError`` on that torn line, so an unattended restart died and the
§11 AUTOMATED auto-resume guarantee was defeated.

The discriminator is hash-chain-safe: a committed record is one terminated by
``"\\n"`` (the writer always appends ``line + "\\n"`` in one ``write()``), so a
crash can only damage the FINAL line and only by stripping its trailing
newline. ONLY a newline-less final line is forgiven; a malformed line that DOES
carry a trailing newline (so it is NOT the final line, or is a fully-flushed
garbage final record) is REAL corruption and still raises. A mid-file
hash-chain break among committed entries is unaffected and still raises from
``verify_chain``.

This module pins the full behaviour matrix for BOTH the ingest-side
:mod:`brutex.orchestrator.audit` (SHA256) and the sweep-side
:mod:`brutex.engine.sweep_audit` (BLAKE3) chains — they are deliberately
STANDALONE so the recovery logic is duplicated, and BOTH copies are exercised
here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from brutex.engine.sweep_audit import (
    SweepAuditChainCorruption,
    SweepAuditError,
    SweepAuditWriteError,
    SweepAuditWriter,
)
from brutex.engine.sweep_audit import (
    _read_entries as sweep_read_entries,
)
from brutex.engine.sweep_audit import (
    _read_entries_with_torn_tail as sweep_read_with_tail,
)
from brutex.engine.sweep_audit import (
    _truncate_torn_tail as sweep_truncate,
)
from brutex.engine.sweep_audit import (
    verify_chain as sweep_verify,
)
from brutex.orchestrator.audit import (
    AuditChainCorruption,
    AuditError,
    AuditWriteError,
    AuditWriter,
)
from brutex.orchestrator.audit import (
    _read_entries as audit_read_entries,
)
from brutex.orchestrator.audit import (
    _read_entries_with_torn_tail as audit_read_with_tail,
)
from brutex.orchestrator.audit import (
    _truncate_torn_tail as audit_truncate,
)
from brutex.orchestrator.audit import (
    verify_chain as audit_verify,
)

# ────────────────────────────────────────────────────────────────────────
# A tiny matrix that runs every case against BOTH chains. Each row binds the
# writer class + its read/verify/truncate helpers + its error types, so a
# single test body proves the ingest (SHA256) AND sweep (BLAKE3) copies.
# ────────────────────────────────────────────────────────────────────────


class _Backend:
    def __init__(
        self,
        name: str,
        writer_cls: type,
        read_with_tail,  # type: ignore[no-untyped-def]
        read_entries,  # type: ignore[no-untyped-def]
        truncate,  # type: ignore[no-untyped-def]
        verify,  # type: ignore[no-untyped-def]
        read_error: type[Exception],
        write_error: type[Exception],
        corruption_error: type[Exception],
    ) -> None:
        self.name = name
        self.writer_cls = writer_cls
        self.read_with_tail = read_with_tail
        self.read_entries = read_entries
        self.truncate = truncate
        self.verify = verify
        self.read_error = read_error
        self.write_error = write_error
        self.corruption_error = corruption_error


_AUDIT = _Backend(
    "audit",
    AuditWriter,
    audit_read_with_tail,
    audit_read_entries,
    audit_truncate,
    audit_verify,
    AuditError,
    AuditWriteError,
    AuditChainCorruption,
)
_SWEEP = _Backend(
    "sweep",
    SweepAuditWriter,
    sweep_read_with_tail,
    sweep_read_entries,
    sweep_truncate,
    sweep_verify,
    SweepAuditError,
    SweepAuditWriteError,
    SweepAuditChainCorruption,
)

_BACKENDS = [_AUDIT, _SWEEP]


@pytest.fixture(params=_BACKENDS, ids=lambda b: b.name)
def backend(request: pytest.FixtureRequest) -> _Backend:
    return request.param  # type: ignore[no-any-return]


def _write_clean_chain(backend: _Backend, path: Path, n: int) -> None:
    """Append *n* clean committed entries through the real writer."""
    with backend.writer_cls.open(path) as log:
        for i in range(n):
            log.append(action="COMBO_DONE", subject=f"s:{i}", payload={"i": i})


def _append_raw(path: Path, text: str) -> None:
    """Append raw bytes (no chain logic) — used to forge a torn / garbage tail."""
    with path.open("a", encoding="utf-8") as f:
        f.write(text)


# ────────────────────────────────────────────────────────────────────────
# Clean file — unchanged behaviour
# ────────────────────────────────────────────────────────────────────────


def test_clean_file_no_torn_tail(backend: _Backend, tmp_path: Path) -> None:
    """A normal, fully newline-terminated log reads with NO torn-tail flag."""
    path = tmp_path / "clean.jsonl"
    _write_clean_chain(backend, path, 3)

    entries, torn = backend.read_with_tail(path)
    assert torn is False
    assert [e.seq for e in entries] == [0, 1, 2]
    assert backend.verify(path) == 3


def test_clean_file_read_entries_unchanged(backend: _Backend, tmp_path: Path) -> None:
    """The public ``_read_entries`` wrapper returns the same entries as before."""
    path = tmp_path / "clean2.jsonl"
    _write_clean_chain(backend, path, 2)
    entries = backend.read_entries(path)
    assert [e.seq for e in entries] == [0, 1]


# ────────────────────────────────────────────────────────────────────────
# Torn tail — partial JSON last line (the crash-mid-append case)
# ────────────────────────────────────────────────────────────────────────


def test_torn_tail_partial_json_recovers(backend: _Backend, tmp_path: Path) -> None:
    """A partial, newline-less final line is skipped + flagged as a torn tail."""
    path = tmp_path / "partial.jsonl"
    _write_clean_chain(backend, path, 2)
    _append_raw(path, '{"seq": 2, "prev_hash": "abc", "ts_utc": 1.0, "act')  # cut off

    entries, torn = backend.read_with_tail(path)
    assert torn is True
    assert [e.seq for e in entries] == [0, 1]


def test_torn_tail_partial_json_resume_continues_chain(backend: _Backend, tmp_path: Path) -> None:
    """``open(resume=True)`` over a partial torn tail recovers + continues clean."""
    path = tmp_path / "partial_resume.jsonl"
    _write_clean_chain(backend, path, 2)
    _append_raw(path, '{"seq": 2, "prev_hash": "abc", "ts_utc": 1.0, "act')

    with backend.writer_cls.open(path, resume=True) as log:
        recovered = log.append(action="COMBO_DONE", subject="s:2", payload={"i": 2})

    assert recovered.seq == 2
    # The torn bytes are gone and the whole chain verifies across the boundary.
    assert backend.verify(path) == 3


# ────────────────────────────────────────────────────────────────────────
# Torn tail — COMPLETE JSON but no trailing newline
# ────────────────────────────────────────────────────────────────────────


def test_torn_tail_complete_json_no_newline_recovers(backend: _Backend, tmp_path: Path) -> None:
    """A COMPLETE final JSON object missing only its trailing ``\\n`` is a torn tail.

    The write got the whole line out but the ``"\\n"`` write was interrupted —
    still uncommitted, still skipped. Forged via a real writer append whose
    trailing newline byte is then stripped on disk.
    """
    path = tmp_path / "complete_no_nl.jsonl"
    _write_clean_chain(backend, path, 3)

    # Strip the file's final newline so the last (valid) line is unterminated.
    data = path.read_bytes()
    assert data.endswith(b"\n")
    path.write_bytes(data[:-1])

    entries, torn = backend.read_with_tail(path)
    assert torn is True
    # The last committed (newline-terminated) entry is seq 1; seq 2 lost its nl.
    assert [e.seq for e in entries] == [0, 1]


def test_torn_tail_complete_json_no_newline_resume(backend: _Backend, tmp_path: Path) -> None:
    """Resume after a complete-but-unterminated tail rewrites seq 2 cleanly."""
    path = tmp_path / "complete_no_nl_resume.jsonl"
    _write_clean_chain(backend, path, 3)
    data = path.read_bytes()
    path.write_bytes(data[:-1])  # drop the final newline

    with backend.writer_cls.open(path, resume=True) as log:
        recovered = log.append(action="COMBO_DONE", subject="s:2b", payload={"i": 22})

    assert recovered.seq == 2
    assert backend.verify(path) == 3


# ────────────────────────────────────────────────────────────────────────
# Torn tail that is the ONLY line → empty valid state
# ────────────────────────────────────────────────────────────────────────


def test_torn_tail_only_line_yields_empty_state(backend: _Backend, tmp_path: Path) -> None:
    """When the torn tail is the file's ONLY line, recovery yields an empty chain."""
    path = tmp_path / "only_torn.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    _append_raw(path, '{"seq": 0, "prev_hash": "00", "ts')  # the only, torn, line

    entries, torn = backend.read_with_tail(path)
    assert torn is True
    assert entries == []


def test_torn_tail_only_line_resume_starts_fresh(backend: _Backend, tmp_path: Path) -> None:
    """Resuming a file that is nothing but a torn tail starts a genesis chain."""
    path = tmp_path / "only_torn_resume.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    _append_raw(path, '{"seq": 0, "prev_hash": "00", "ts')

    with backend.writer_cls.open(path, resume=True) as log:
        first = log.append(action="SWEEP_START", subject="s:0", payload={})

    assert first.seq == 0
    assert backend.verify(path) == 1


def test_torn_tail_blank_unterminated_only(backend: _Backend, tmp_path: Path) -> None:
    """A purely-blank unterminated tail (crash at record start) → empty + flagged."""
    path = tmp_path / "blank_torn.jsonl"
    _write_clean_chain(backend, path, 1)
    _append_raw(path, "   ")  # whitespace, no newline — content-less torn remnant

    entries, torn = backend.read_with_tail(path)
    assert torn is True
    assert [e.seq for e in entries] == [0]


# ────────────────────────────────────────────────────────────────────────
# REAL corruption — a malformed NON-final (newline-terminated) line MUST raise
# ────────────────────────────────────────────────────────────────────────


def test_malformed_nonfinal_line_raises(backend: _Backend, tmp_path: Path) -> None:
    """A malformed line that is NOT the final line is real corruption → raises."""
    path = tmp_path / "midfile_bad.jsonl"
    _write_clean_chain(backend, path, 2)
    # A garbage line WITH a trailing newline, then a clean committed line after
    # it — so the garbage is provably not the final line.
    _append_raw(path, "this is not json\n")
    _append_valid_terminated_line(path)

    with pytest.raises(backend.read_error):
        backend.read_with_tail(path)


def _append_valid_terminated_line(path: Path) -> None:
    """Append one syntactically-valid, newline-terminated committed JSONL line.

    It need not chain — the garbage before it already triggers the raise; we
    only need a terminated line AFTER the garbage so the garbage is provably
    non-final.
    """
    valid = (
        '{"seq": 9, "prev_hash": "00", "ts_utc": 1.0, "actor": "x", '
        '"action": "A", "subject": "s", "payload": {}, "entry_hash": "h"}\n'
    )
    _append_raw(path, valid)


def test_malformed_final_line_with_newline_raises(backend: _Backend, tmp_path: Path) -> None:
    """A garbage FINAL line that DOES carry a trailing newline is NOT a torn tail.

    A trailing-newline final line was fully committed by the writer, so garbage
    there is real corruption (e.g. a flipped byte), not an interrupted append —
    it must raise, never be silently skipped.
    """
    path = tmp_path / "final_bad_nl.jsonl"
    _write_clean_chain(backend, path, 1)
    _append_raw(path, "not json at all\n")  # committed (terminated) garbage

    with pytest.raises(backend.read_error):
        backend.read_with_tail(path)


# ────────────────────────────────────────────────────────────────────────
# REAL corruption — a mid-file hash-chain break MUST raise from verify_chain
# ────────────────────────────────────────────────────────────────────────


def test_midfile_chain_break_still_raises(backend: _Backend, tmp_path: Path) -> None:
    """A tampered committed entry breaks the chain → verify_chain raises.

    The torn-tail tolerance must NOT weaken mid-file integrity: every line here
    is newline-terminated + well-formed JSON (so ``_read_entries`` accepts
    them), but one entry's payload is tampered, so the recomputed hash mismatches
    and ``verify_chain`` raises a corruption error naming the seq.
    """
    path = tmp_path / "chain_break.jsonl"
    _write_clean_chain(backend, path, 3)

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    # Tamper the middle committed line's payload without touching its stored
    # entry_hash → recomputed hash no longer matches.
    lines[1] = lines[1].replace('"i":1', '"i":999')
    assert lines[1].endswith("\n")
    path.write_text("".join(lines), encoding="utf-8")

    # _read_entries still parses (all lines are valid JSON, all terminated).
    entries, torn = backend.read_with_tail(path)
    assert torn is False
    assert len(entries) == 3
    # But the chain verification catches the tamper.
    with pytest.raises(backend.corruption_error) as exc_info:
        backend.verify(path)
    assert "seq=" in str(exc_info.value)


def test_torn_tail_after_chain_break_break_wins(backend: _Backend, tmp_path: Path) -> None:
    """A mid-file break + a torn tail: the break still raises (integrity first).

    The torn tail is forgiven on the READ, but the committed prefix still
    contains a real chain break, so ``verify_chain`` raises — a torn tail can
    never mask genuine corruption of committed entries.
    """
    path = tmp_path / "break_plus_torn.jsonl"
    _write_clean_chain(backend, path, 3)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    lines[0] = lines[0].replace('"i":0', '"i":777')  # tamper committed seq 0
    path.write_text("".join(lines), encoding="utf-8")
    _append_raw(path, '{"seq": 3, "prev')  # plus a torn tail

    entries, torn = backend.read_with_tail(path)
    assert torn is True
    assert len(entries) == 3  # read tolerates the tail
    with pytest.raises(backend.corruption_error):
        backend.verify(path)


# ────────────────────────────────────────────────────────────────────────
# Empty file vs missing file (not torn — distinct edges)
# ────────────────────────────────────────────────────────────────────────


def test_empty_file_is_not_torn(backend: _Backend, tmp_path: Path) -> None:
    """A zero-byte file is empty + valid, NOT a torn tail."""
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    entries, torn = backend.read_with_tail(path)
    assert entries == []
    assert torn is False
    assert backend.verify(path) == 0


def test_file_with_only_blank_terminated_line(backend: _Backend, tmp_path: Path) -> None:
    """A file of only a blank, newline-terminated line is empty + NOT torn."""
    path = tmp_path / "blank_line.jsonl"
    path.write_text("\n", encoding="utf-8")
    entries, torn = backend.read_with_tail(path)
    assert entries == []
    assert torn is False


def test_missing_file_raises(backend: _Backend, tmp_path: Path) -> None:
    """A missing file is an error, NOT a torn tail."""
    path = tmp_path / "nope.jsonl"
    with pytest.raises(backend.read_error):
        backend.read_with_tail(path)


# ────────────────────────────────────────────────────────────────────────
# _truncate_torn_tail — direct unit coverage
# ────────────────────────────────────────────────────────────────────────


def test_truncate_keeps_committed_prefix(backend: _Backend, tmp_path: Path) -> None:
    """Truncation keeps exactly the committed (newline-terminated) bytes."""
    path = tmp_path / "trunc.jsonl"
    _write_clean_chain(backend, path, 2)
    committed = path.read_bytes()
    _append_raw(path, '{"seq": 2, "prev')  # torn remnant

    backend.truncate(path)

    assert path.read_bytes() == committed
    # And the truncated file is clean — no torn flag, full verify.
    entries, torn = backend.read_with_tail(path)
    assert torn is False
    assert [e.seq for e in entries] == [0, 1]
    # The temp file is gone (atomic replace cleaned it up).
    assert list(path.parent.glob("*.torn-tail-*.tmp")) == []


def test_truncate_only_torn_line_yields_empty_file(backend: _Backend, tmp_path: Path) -> None:
    """When the file is nothing but a torn line, truncation empties it.

    Exercises the ``cut == -1`` branch (no committed newline anywhere).
    """
    path = tmp_path / "trunc_only.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    _append_raw(path, '{"seq": 0, "prev')

    backend.truncate(path)

    assert path.read_bytes() == b""


def test_truncate_io_error_wraps(
    backend: _Backend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An I/O failure during truncation is wrapped in the chain's write error."""
    path = tmp_path / "trunc_fail.jsonl"
    _write_clean_chain(backend, path, 1)
    _append_raw(path, '{"seq": 1, "prev')

    # Make os.replace blow up to drive the OSError → write-error mapping.
    import os as _os

    def _boom(_src: object, _dst: object) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(_os, "replace", _boom)
    with pytest.raises(backend.write_error):
        backend.truncate(path)
