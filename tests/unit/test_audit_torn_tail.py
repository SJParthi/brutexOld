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
from typing import Any

import pytest
from brutex.engine import sweep_audit as sweep_audit_mod
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
    _read_tail_entry as sweep_read_tail,
)
from brutex.engine.sweep_audit import (
    _truncate_torn_tail as sweep_truncate,
)
from brutex.engine.sweep_audit import (
    verify_chain as sweep_verify,
)
from brutex.orchestrator import audit as audit_mod
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
    _read_tail_entry as audit_read_tail,
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
        read_tail,  # type: ignore[no-untyped-def]
        truncate,  # type: ignore[no-untyped-def]
        verify,  # type: ignore[no-untyped-def]
        module,  # type: ignore[no-untyped-def]
        read_error: type[Exception],
        write_error: type[Exception],
        corruption_error: type[Exception],
    ) -> None:
        self.name = name
        self.writer_cls = writer_cls
        self.read_with_tail = read_with_tail
        self.read_entries = read_entries
        self.read_tail = read_tail
        self.truncate = truncate
        self.verify = verify
        self.module = module
        self.read_error = read_error
        self.write_error = write_error
        self.corruption_error = corruption_error


_AUDIT = _Backend(
    "audit",
    AuditWriter,
    audit_read_with_tail,
    audit_read_entries,
    audit_read_tail,
    audit_truncate,
    audit_verify,
    audit_mod,
    AuditError,
    AuditWriteError,
    AuditChainCorruption,
)
_SWEEP = _Backend(
    "sweep",
    SweepAuditWriter,
    sweep_read_with_tail,
    sweep_read_entries,
    sweep_read_tail,
    sweep_truncate,
    sweep_verify,
    sweep_audit_mod,
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

    # Make the in-place os.truncate blow up to drive the OSError → write-error
    # mapping (DEC-AUDIT-TORN-TAIL-O1-001 truncates in place instead of the old
    # temp-file os.replace rewrite).
    import os as _os

    def _boom(_fd: object, _length: object) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(_os, "truncate", _boom)
    with pytest.raises(backend.write_error):
        backend.truncate(path)


# ────────────────────────────────────────────────────────────────────────
# DEC-AUDIT-RESUME-TAILREAD-001 — O(1) reverse tail-read on resume
#
# `open(resume=True)` no longer materialises the WHOLE log to recover the
# tail; it seeks to the end and parses only the final committed line via
# `_read_tail_entry`. These tests pin, for BOTH chains:
#   * the recovered (seq, prev_hash/entry_hash, torn_tail) EQUALS what the
#     old full-read (`_read_entries_with_torn_tail`) produced (parity),
#   * every torn-tail / empty / boundary edge behaves identically,
#   * resume continues a valid, verifiable chain, and
#   * the tail-read parses O(1) lines, NOT O(n) (the teeth test).
# ────────────────────────────────────────────────────────────────────────


def _full_read_tail(backend: _Backend, path: Path):  # type: ignore[no-untyped-def]
    """The OLD behaviour: full read, then ``(entries[-1] if entries else None, torn)``."""
    entries, torn = backend.read_with_tail(path)
    return (entries[-1] if entries else None), torn


def _assert_tail_parity(backend: _Backend, path: Path) -> None:
    """The reverse tail-read yields the SAME tail entry + torn flag as the full read."""
    want_entry, want_torn = _full_read_tail(backend, path)
    got_entry, got_torn = backend.read_tail(path)
    assert got_torn == want_torn
    if want_entry is None:
        assert got_entry is None
    else:
        assert got_entry is not None
        assert got_entry.seq == want_entry.seq
        assert got_entry.prev_hash == want_entry.prev_hash
        assert got_entry.entry_hash == want_entry.entry_hash
        # Full structural equality — the frozen dataclasses compare by value.
        assert got_entry == want_entry


def test_tailread_empty_file(backend: _Backend, tmp_path: Path) -> None:
    """An empty (zero-byte) file → ``(None, False)``, matching the full read."""
    path = tmp_path / "tail_empty.jsonl"
    path.write_text("", encoding="utf-8")
    assert backend.read_tail(path) == (None, False)
    _assert_tail_parity(backend, path)


def test_tailread_missing_file_raises(backend: _Backend, tmp_path: Path) -> None:
    """A missing file raises the chain's read error (line 699 / 556)."""
    path = tmp_path / "tail_missing.jsonl"
    with pytest.raises(backend.read_error):
        backend.read_tail(path)


def test_tailread_single_entry(backend: _Backend, tmp_path: Path) -> None:
    """A one-entry log recovers seq 0 with no torn flag — parity with full read."""
    path = tmp_path / "tail_single.jsonl"
    _write_clean_chain(backend, path, 1)
    entry, torn = backend.read_tail(path)
    assert torn is False
    assert entry is not None and entry.seq == 0
    _assert_tail_parity(backend, path)


def test_tailread_many_entries_recovers_last(backend: _Backend, tmp_path: Path) -> None:
    """A many-entry log recovers exactly the LAST entry (seq n-1), parity-checked."""
    path = tmp_path / "tail_many.jsonl"
    _write_clean_chain(backend, path, 50)
    entry, torn = backend.read_tail(path)
    assert torn is False
    assert entry is not None and entry.seq == 49
    _assert_tail_parity(backend, path)


def test_tailread_ends_on_newline_vs_midline(backend: _Backend, tmp_path: Path) -> None:
    """A log ending exactly on a newline (clean) vs mid-line (torn) — both parity."""
    path = tmp_path / "tail_boundary.jsonl"
    _write_clean_chain(backend, path, 4)

    # Ends exactly on a newline → clean, no torn tail.
    assert path.read_bytes().endswith(b"\n")
    entry, torn = backend.read_tail(path)
    assert torn is False
    assert entry is not None and entry.seq == 3
    _assert_tail_parity(backend, path)

    # Now strip the final newline → the last line is mid-line (torn tail).
    path.write_bytes(path.read_bytes()[:-1])
    entry2, torn2 = backend.read_tail(path)
    assert torn2 is True
    # The last COMMITTED line is now seq 2 (seq 3 lost its newline).
    assert entry2 is not None and entry2.seq == 2
    _assert_tail_parity(backend, path)


def test_tailread_partial_json_torn_recovers_last_complete(
    backend: _Backend, tmp_path: Path
) -> None:
    """A partial newline-less final line is skipped; the last complete entry wins."""
    path = tmp_path / "tail_partial.jsonl"
    _write_clean_chain(backend, path, 3)
    _append_raw(path, '{"seq": 3, "prev_hash": "abc", "ts_utc": 1.0, "act')  # cut off

    entry, torn = backend.read_tail(path)
    assert torn is True
    assert entry is not None and entry.seq == 2
    _assert_tail_parity(backend, path)


def test_tailread_only_torn_line_is_none(backend: _Backend, tmp_path: Path) -> None:
    """A file that is ONLY a torn line → ``(None, True)`` — parity with full read."""
    path = tmp_path / "tail_only_torn.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    _append_raw(path, '{"seq": 0, "prev_hash": "00", "ts')
    entry, torn = backend.read_tail(path)
    assert entry is None
    assert torn is True
    _assert_tail_parity(backend, path)


def test_tailread_blank_lines_skipped(backend: _Backend, tmp_path: Path) -> None:
    """Trailing blank committed lines are skipped to find the real last entry.

    Drives the multi-blank-line reverse scan (``end = start`` loop / line
    773 / 614) in ``_last_nonblank_committed_line``.
    """
    path = tmp_path / "tail_blanks.jsonl"
    _write_clean_chain(backend, path, 2)
    _append_raw(path, "\n\n\n")  # three blank committed lines after seq 1

    entry, torn = backend.read_tail(path)
    assert torn is False  # file ends in "\n"
    assert entry is not None and entry.seq == 1
    # Full read agrees (blank committed lines are benign filler on both paths).
    entries, full_torn = backend.read_with_tail(path)
    assert full_torn is False
    assert [e.seq for e in entries] == [0, 1]


def test_tailread_only_blank_lines_is_none(backend: _Backend, tmp_path: Path) -> None:
    """A file of only blank newline-terminated lines → ``(None, False)``."""
    path = tmp_path / "tail_only_blanks.jsonl"
    path.write_text("\n\n\n", encoding="utf-8")
    entry, torn = backend.read_tail(path)
    assert entry is None
    assert torn is False
    _assert_tail_parity(backend, path)


def test_tailread_last_committed_line_corrupt_raises(backend: _Backend, tmp_path: Path) -> None:
    """A committed (newline-terminated) garbage FINAL line is real corruption → raises.

    The tail-read parses the last committed line strictly, exactly as the full
    read did — a fully-flushed garbage record is a flipped byte, not a torn tail.
    """
    path = tmp_path / "tail_corrupt_last.jsonl"
    _write_clean_chain(backend, path, 1)
    _append_raw(path, "not json at all\n")  # committed (terminated) garbage
    with pytest.raises(backend.read_error):
        backend.read_tail(path)


def test_tailread_spans_multiple_chunks(
    backend: _Backend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tiny reverse-read chunk forces the backwards walk to loop — parity holds.

    Shrinking ``_TAIL_CHUNK_BYTES`` to a few bytes makes the reader step back
    through many chunks (the no-newline-yet ``continue`` arm and the loop
    that isolates the last committed line), proving the multi-chunk walk is
    correct, not just the single-read fast case.
    """
    monkeypatch.setattr(backend.module, "_TAIL_CHUNK_BYTES", 4)
    path = tmp_path / "tail_chunks.jsonl"
    _write_clean_chain(backend, path, 6)
    entry, torn = backend.read_tail(path)
    assert torn is False
    assert entry is not None and entry.seq == 5
    _assert_tail_parity(backend, path)


def test_tailread_single_giant_line_smaller_than_chunk(
    backend: _Backend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One entry whose line exceeds the chunk size still recovers via the walk.

    With a 4-byte chunk over a single long committed line, the reader loops to
    ``pos == 0`` (the ``for/else`` exhaustion arm) before it ever sees a
    newline-preceded region, then recovers the whole first line.
    """
    monkeypatch.setattr(backend.module, "_TAIL_CHUNK_BYTES", 4)
    path = tmp_path / "tail_giant.jsonl"
    _write_clean_chain(backend, path, 1)
    entry, torn = backend.read_tail(path)
    assert torn is False
    assert entry is not None and entry.seq == 0
    _assert_tail_parity(backend, path)


def test_tailread_resume_continues_valid_chain(backend: _Backend, tmp_path: Path) -> None:
    """Resume via the tail-read continues an unbroken, fully-verifiable chain."""
    path = tmp_path / "tail_resume.jsonl"
    _write_clean_chain(backend, path, 5)

    with backend.writer_cls.open(path, resume=True) as log:
        recovered = log.append(action="COMBO_DONE", subject="s:5", payload={"i": 5})

    assert recovered.seq == 5
    assert backend.verify(path) == 6


def test_tailread_resume_after_torn_recovers_and_truncates(
    backend: _Backend, tmp_path: Path
) -> None:
    """Resume over a torn tail truncates it then continues from the last entry."""
    path = tmp_path / "tail_resume_torn.jsonl"
    _write_clean_chain(backend, path, 3)
    _append_raw(path, '{"seq": 3, "prev_hash": "abc", "ts_utc": 1.0, "act')

    with backend.writer_cls.open(path, resume=True) as log:
        recovered = log.append(action="COMBO_DONE", subject="s:3", payload={"i": 3})

    assert recovered.seq == 3  # continues from the last COMPLETE entry (seq 2)
    assert backend.verify(path) == 4
    assert list(path.parent.glob("*.torn-tail-*.tmp")) == []


@pytest.mark.parametrize("n", [1, 2, 10, 137])
def test_tailread_parity_across_sizes(backend: _Backend, tmp_path: Path, n: int) -> None:
    """Across many log sizes, the tail-read's (seq, prev_hash, hash) match the full read."""
    path = tmp_path / f"tail_parity_{n}.jsonl"
    _write_clean_chain(backend, path, n)
    _assert_tail_parity(backend, path)


def test_tailread_parses_o1_lines_not_all(
    backend: _Backend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TEETH: the reverse tail-read parses O(1) lines, NOT O(n).

    Spy on ``AuditEntry.from_json_line`` / ``SweepAuditEntry.from_json_line`` and
    prove a 500-entry log's tail-read invokes it exactly ONCE (only the final
    committed line), whereas the OLD full read parses all 500. This is the
    load-bearing proof that the fix is genuinely O(1)-tail, not merely faster.
    """
    # The entry class lives on the module as AuditEntry / SweepAuditEntry.
    entry_type = (
        backend.module.AuditEntry if backend.name == "audit" else backend.module.SweepAuditEntry
    )
    real_parser = entry_type.from_json_line

    counter = {"n": 0}

    def _counting(line: str):  # type: ignore[no-untyped-def]
        counter["n"] += 1
        return real_parser(line)

    monkeypatch.setattr(entry_type, "from_json_line", staticmethod(_counting))

    path = tmp_path / "tail_teeth.jsonl"
    _write_clean_chain(backend, path, 500)

    counter["n"] = 0
    tail_entry, _torn = backend.read_tail(path)
    tail_parses = counter["n"]

    counter["n"] = 0
    full_entries, _torn2 = backend.read_with_tail(path)
    full_parses = counter["n"]

    # The tail-read parses exactly ONE line (the final committed entry)…
    assert tail_parses == 1
    # …while the old full read parses every one of the 500.
    assert full_parses == 500
    # …and both agree on the recovered tail entry (correctness alongside the teeth).
    assert tail_entry is not None
    assert tail_entry.seq == full_entries[-1].seq == 499
    assert tail_entry.entry_hash == full_entries[-1].entry_hash


# ────────────────────────────────────────────────────────────────────────
# DEC-AUDIT-TORN-TAIL-O1-001 — O(1)-memory torn-tail REPAIR
#
# `_truncate_torn_tail` used to `f.read()` the WHOLE log to find the last
# newline (`data.rfind(b"\n")`) — an OOM risk on a multi-GB log. It now locates
# the cut with the same bounded reverse-seek the resume tail-read uses
# (`_iter_reverse_chunks` → `_find_last_newline_offset`) and truncates in
# place. These tests pin, for BOTH chains:
#   * BYTE-IDENTITY: the repaired file equals the OLD implementation's output
#     (`data[: data.rfind(b"\n") + 1]`, empty when no newline) on every edge —
#     empty file, single clean line, single torn line, many clean lines, many
#     clean lines + torn tail, a torn line LONGER than one reverse-seek chunk
#     (the multi-block walk), last-byte-is/-isn't-newline, torn-at-start.
#   * O(1) MEMORY: the repair's largest single read is bounded by the chunk
#     size — it provably never reads the whole file (the teeth test).
# ────────────────────────────────────────────────────────────────────────


def _old_truncate_semantics(data: bytes) -> bytes:
    """The OLD `_truncate_torn_tail` result, verbatim: keep to the last newline.

    Mirrors the pre-DEC-AUDIT-TORN-TAIL-O1-001 body byte-for-byte:
    ``cut = data.rfind(b"\\n"); committed = data[: cut + 1] if cut != -1 else b""``.
    """
    cut = data.rfind(b"\n")
    return data[: cut + 1] if cut != -1 else b""


# Raw byte contents covering every torn-tail geometry the repair can meet.
# ids double as documentation of the case.
_TRUNCATE_CASES: list[tuple[str, bytes]] = [
    ("empty_file", b""),
    ("single_clean_line", b'{"a": 1}\n'),
    ("single_torn_line", b'{"seq": 0, "prev'),
    ("many_clean_lines", b'{"a": 1}\n{"b": 2}\n{"c": 3}\n'),
    ("many_clean_plus_torn_tail", b'{"a": 1}\n{"b": 2}\n{"seq": 2, "pre'),
    # A torn line LONGER than one reverse-seek chunk: with the chunk pinned to
    # 8 bytes below, this 100-byte torn tail spans 13 reverse blocks before the
    # walk reaches the last committed newline (the critical multi-block edge).
    ("torn_longer_than_chunk", b'{"a": 1}\n' + b"X" * 100),
    # A torn line longer than one chunk with NO newline anywhere (cut == -1
    # after walking every block to offset 0).
    ("all_torn_longer_than_chunk_no_newline", b"Y" * 100),
    ("last_byte_is_newline", b'{"a": 1}\n{"b": 2}\n'),
    ("last_byte_not_newline", b'{"a": 1}\n{"b": 2}'),
    ("torn_at_very_start_only_byte", b"{"),
    ("blank_lines_then_torn", b"\n\n\n{torn"),
    ("newline_only_file", b"\n"),
]


@pytest.mark.parametrize(("case_id", "data"), _TRUNCATE_CASES, ids=[c[0] for c in _TRUNCATE_CASES])
def test_truncate_byte_identical_to_old_impl(
    backend: _Backend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case_id: str,
    data: bytes,
) -> None:
    """The O(1) repair's output is byte-identical to the old whole-file rewrite.

    The chunk size is pinned to 8 bytes so every multi-block reverse-walk arm
    (including a torn tail spanning many chunks) is really exercised, not just
    the one-read fast path.
    """
    monkeypatch.setattr(backend.module, "_TAIL_CHUNK_BYTES", 8)
    path = tmp_path / f"bytes_{case_id}.jsonl"
    path.write_bytes(data)

    backend.truncate(path)

    assert path.read_bytes() == _old_truncate_semantics(data)


@pytest.mark.parametrize(("case_id", "data"), _TRUNCATE_CASES, ids=[c[0] for c in _TRUNCATE_CASES])
def test_truncate_byte_identical_default_chunk(
    backend: _Backend, tmp_path: Path, case_id: str, data: bytes
) -> None:
    """Same byte-identity matrix at the PRODUCTION chunk size (single-read path)."""
    path = tmp_path / f"bytes_dflt_{case_id}.jsonl"
    path.write_bytes(data)

    backend.truncate(path)

    assert path.read_bytes() == _old_truncate_semantics(data)


def test_truncate_is_idempotent(backend: _Backend, tmp_path: Path) -> None:
    """Repairing an already-clean file is a no-op (re-run safety, Golden Rule 7)."""
    path = tmp_path / "idem.jsonl"
    _write_clean_chain(backend, path, 3)
    clean = path.read_bytes()

    backend.truncate(path)
    once = path.read_bytes()
    backend.truncate(path)
    twice = path.read_bytes()

    assert once == twice == clean
    assert backend.verify(path) == 3


def test_truncate_real_writer_torn_tail_verifies(backend: _Backend, tmp_path: Path) -> None:
    """End-to-end: a real chain + torn remnant repairs to a fully verifying chain."""
    path = tmp_path / "e2e.jsonl"
    _write_clean_chain(backend, path, 5)
    committed = path.read_bytes()
    _append_raw(path, '{"seq": 5, "prev_hash": "abc", "ts_utc": 1.0, "act')

    backend.truncate(path)

    assert path.read_bytes() == committed
    assert backend.verify(path) == 5


class _ReadTrackingFile:
    """A binary-file proxy that records the size of every read() it serves.

    ``__enter__`` / ``__exit__`` are defined explicitly (NOT via
    ``__getattr__``) because the ``with`` statement looks special methods up on
    the type; ``__enter__`` returns the proxy itself so reads inside the
    ``with`` body stay tracked.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.read_sizes: list[int] = []

    def read(self, n: int = -1) -> bytes:
        data: bytes = self._inner.read(n)
        self.read_sizes.append(len(data))
        return data

    def __enter__(self) -> _ReadTrackingFile:
        self._inner.__enter__()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._inner.__exit__(*exc_info)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_truncate_memory_is_o_chunk_not_o_filesize(
    backend: _Backend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TEETH: the repair NEVER reads the whole file — every read is chunk-bounded.

    Builds a ~1 MiB log (128x the 8 KiB chunk) with a torn tail, wraps `open`
    so every `read()` size is recorded, and asserts (a) the largest single read
    is ≤ `_TAIL_CHUNK_BYTES` and (b) the TOTAL bytes read are a small bounded
    multiple of the chunk size (the reverse walk stops at the first newline),
    NOT the file size. The old implementation's single `f.read()` of the whole
    ~1 MiB file would fail both assertions.
    """
    chunk = backend.module._TAIL_CHUNK_BYTES
    path = tmp_path / "big.jsonl"
    # ~1 MiB of committed lines, then a torn tail longer than one chunk so the
    # reverse walk must cross a block boundary before finding the last newline.
    line = b'{"filler": "' + b"f" * 100 + b'"}\n'
    n_lines = (128 * chunk) // len(line) + 1
    with path.open("wb") as f:
        for _ in range(n_lines):
            f.write(line)
        f.write(b"T" * (chunk + chunk // 2))  # torn tail spanning 2 chunks
    committed_size = n_lines * len(line)
    file_size = path.stat().st_size
    assert file_size > 128 * chunk  # the log really is >> one chunk

    tracker: list[_ReadTrackingFile] = []
    real_open = open

    def _spy_open(file: Any, mode: str = "r", **kwargs: Any) -> Any:
        fh = real_open(file, mode, **kwargs)
        if file == path and "b" in mode:
            proxy = _ReadTrackingFile(fh)
            tracker.append(proxy)
            return proxy
        return fh

    monkeypatch.setattr(backend.module, "open", _spy_open, raising=False)
    backend.truncate(path)
    monkeypatch.undo()

    # Behaviour: torn tail gone, committed prefix intact byte-for-byte.
    assert path.stat().st_size == committed_size
    data = path.read_bytes()
    assert data.endswith(line)
    assert len(data) == committed_size

    # Memory teeth: every single read ≤ one chunk; total read ≤ 4 chunks
    # (the walk stops at the first block containing a newline) — and in every
    # case far below the file size the old implementation slurped.
    assert tracker, "the repair must have opened the log through the spy"
    all_reads = [n for t in tracker for n in t.read_sizes]
    assert all_reads, "the repair must actually read"
    assert max(all_reads) <= chunk
    assert sum(all_reads) <= 4 * chunk
    assert sum(all_reads) < file_size


def test_truncate_reverse_chunks_shared_with_tailread(backend: _Backend) -> None:
    """COMMON: the repair and the resume tail-read share ONE reverse-seek helper.

    Guards the dedup: `_read_tail_entry` and `_find_last_newline_offset` must
    both delegate to the module's single `_iter_reverse_chunks` generator (no
    copy-pasted reverse-walk loops).
    """
    import inspect

    module = backend.module
    assert hasattr(module, "_iter_reverse_chunks")
    assert "_iter_reverse_chunks(" in inspect.getsource(module._read_tail_entry)
    assert "_iter_reverse_chunks(" in inspect.getsource(module._find_last_newline_offset)
    # And the repair itself no longer whole-file-reads: it delegates the cut
    # search to _find_last_newline_offset.
    assert "_find_last_newline_offset(" in inspect.getsource(module._truncate_torn_tail)
