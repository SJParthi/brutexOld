"""Unit tests for brutex.orchestrator.audit — hash-chained JSONL log.

Covers:

* AuditEntry serialization round-trip
* AuditEntry.compute_hash determinism and sensitivity to field changes
* AuditWriter append + chain advancement
* AuditWriter resume mode
* verify_chain happy path + every failure mode (seq, prev_hash, hash)
* Tamper detection: modifying payload, actor, action, subject, ts
* File I/O: create parent dir, refuse to overwrite unless resume
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import math
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO

import pytest
from brutex.orchestrator.audit import (
    GENESIS_PREV_HASH,
    AuditChainCorruption,
    AuditEntry,
    AuditError,
    AuditFieldError,
    AuditPayloadError,
    AuditSink,
    AuditWriteError,
    AuditWriter,
    BufferedAuditCollector,
    BufferedAuditRecord,
    read_entries,
    verify_chain,
)
from brutex.orchestrator.audit import (
    _find_last_newline_offset as find_last_newline_offset,
)
from brutex.orchestrator.audit import (
    _has_torn_tail as has_torn_tail,
)
from brutex.orchestrator.audit import (
    _iter_reverse_chunks as iter_reverse_chunks,
)
from brutex.orchestrator.audit import (
    _last_nonblank_committed_line as last_nonblank_committed_line,
)
from brutex.orchestrator.audit import (
    _read_tail_entry as read_tail_entry,
)
from brutex.orchestrator.audit import (
    _truncate_torn_tail as truncate_torn_tail,
)
from hypothesis import given
from hypothesis import strategies as st

# ────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "audit" / "run-test.jsonl"


# ────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────


class TestConstants:
    def test_genesis_prev_hash_is_64_zeros(self) -> None:
        # Byte-exact value + type pin. The `str` type assertion kills the
        # `"0" * 64` -> `"0" / 64` mutation (a `str.__truediv__` that would
        # not yield a 64-char string), and the all-chars-zero check kills a
        # repeat-count / fill-char mutation, on this module-level constant.
        assert isinstance(GENESIS_PREV_HASH, str)
        assert GENESIS_PREV_HASH == "0" * 64
        assert len(GENESIS_PREV_HASH) == 64
        assert set(GENESIS_PREV_HASH) == {"0"}


# ────────────────────────────────────────────────────────────────────────
# _iter_reverse_chunks — bounded termination + exact 8192-byte tiling
# (DEC-MUTATION-RUNTIME-003). Deliberately placed EARLY in this file: this
# generator is the shared reverse-seek walk under _read_tail_entry and
# _find_last_newline_offset, and a loop mutant in it (e.g. `pos > 0` →
# `pos >= 0`, or `pos -= read_size` → `pos = read_size`) SPINS FOREVER
# instead of failing. islice-bounding the generator makes THIS test fail
# fast under any such mutant, and running it before every consumer test
# means the mutation runner (`pytest -x`) dies here in milliseconds instead
# of wedging on a later hung test (the orphaned-pytest pipe wedge the
# 2026-07-07 rehearsal hit on this module's pass).
# ────────────────────────────────────────────────────────────────────────


class TestReverseChunkWalkBounded:
    """The O(1)-memory reverse walk: exact tiling, tail-first order, termination."""

    @pytest.mark.parametrize("size", [1, 5, 8191, 8192, 8193, 16384, 20000])
    def test_terminates_with_exact_8192_tiling_and_reconstruction(
        self, tmp_path: Path, size: int
    ) -> None:
        # The documented memory envelope: blocks of AT MOST 8192 bytes (the
        # hardcoded boundary — a drifted chunk constant is a real change to
        # the O(_TAIL_CHUNK_BYTES) resident-bytes contract), yielded from the
        # file END back to offset 0, terminating after exactly
        # ceil(size / 8192) blocks that tile the file bytes exactly.
        data = bytes(i % 251 for i in range(size))
        blob = tmp_path / "blob.bin"
        blob.write_bytes(data)
        expected_blocks = -(-size // 8192)  # ceil(size / 8192)
        with open(blob, "rb") as f:
            # islice bound: an infinite-loop mutant yields extra blocks and
            # FAILS the length assert fast instead of hanging the runner.
            blocks = list(itertools.islice(iter_reverse_chunks(f, size), expected_blocks + 2))
        assert len(blocks) == expected_blocks
        # Tail-first: the first block ends at EOF; the last begins at 0.
        assert blocks[0][0] == size - len(blocks[0][1])
        assert blocks[-1][0] == 0
        positions = [pos for pos, _ in blocks]
        assert positions == sorted(positions, reverse=True)
        for pos, block in blocks:
            assert 1 <= len(block) <= 8192
            assert data[pos : pos + len(block)] == block
        assert b"".join(block for _, block in reversed(blocks)) == data

    def test_empty_file_yields_nothing(self, tmp_path: Path) -> None:
        blob = tmp_path / "empty.bin"
        blob.write_bytes(b"")
        with open(blob, "rb") as f:
            assert list(itertools.islice(iter_reverse_chunks(f, 0), 2)) == []


class TestLastNonblankCommittedLinePins:
    """_last_nonblank_committed_line — the committed-line scanner, edge-pinned.

    Placed EARLY (before the first resume test) and with a deliberate case
    order: the scanner's blank-walk loop mutants (`end != -1` comparison
    flips, `end = start` → None) SPIN on the empty/blank chunks that every
    single-entry resume reaches via scan(b"") — but they return a fast
    WRONG value on the torn-suffix / blank+torn / no-newline cases below.
    Under the mutation runner's `-x`, those fast failures must fire before
    any resume test (or the blank-only cases further down) can hang.
    """

    def test_returns_last_committed_line_with_newline(self) -> None:
        assert last_nonblank_committed_line(b"a\nb\nc\n") == b"c\n"

    def test_ignores_the_unterminated_torn_suffix(self) -> None:
        assert last_nonblank_committed_line(b"a\nb\ntorn") == b"b\n"

    def test_blank_walk_with_torn_suffix_skips_both(self) -> None:
        # Blank committed filler AND a torn suffix together: the scanner
        # must walk back to the real committed line, never surface the
        # torn bytes.
        assert last_nonblank_committed_line(b"a\n\ntorn") == b"a\n"

    def test_walks_back_over_blank_committed_lines(self) -> None:
        assert last_nonblank_committed_line(b"a\n\n\n") == b"a\n"

    def test_line_after_a_leading_newline_is_cut_exactly(self) -> None:
        # The start-boundary pin: the candidate begins one byte AFTER the
        # preceding "\n", never at/before it.
        assert last_nonblank_committed_line(b"\nabc\n") == b"abc\n"

    def test_first_line_of_chunk_is_reachable(self) -> None:
        # start == -1 → the candidate is taken from offset 0.
        assert last_nonblank_committed_line(b"only\n") == b"only\n"

    def test_no_committed_line_returns_none(self) -> None:
        assert last_nonblank_committed_line(b"torn-only") is None

    def test_blank_only_returns_none(self) -> None:
        assert last_nonblank_committed_line(b"\n\n") is None

    def test_empty_chunk_returns_none(self) -> None:
        assert last_nonblank_committed_line(b"") is None


# ────────────────────────────────────────────────────────────────────────
# AuditEntry.compute_hash
# ────────────────────────────────────────────────────────────────────────


class TestAuditEntryHash:
    def test_hash_is_64_hex_chars(self) -> None:
        entry = AuditEntry(
            seq=0,
            prev_hash=GENESIS_PREV_HASH,
            ts_utc=1712987654.0,
            actor="brutex",
            action="RUN_START",
            subject="run:abc",
            payload={"k": "v"},
        )
        h = entry.compute_hash()
        assert len(h) == 64
        int(h, 16)  # will raise ValueError if not hex

    def test_hash_is_deterministic(self) -> None:
        e1 = AuditEntry(
            seq=0,
            prev_hash=GENESIS_PREV_HASH,
            ts_utc=1712987654.0,
            actor="brutex",
            action="RUN_START",
            subject="run:abc",
            payload={"k": "v"},
        )
        e2 = AuditEntry(
            seq=0,
            prev_hash=GENESIS_PREV_HASH,
            ts_utc=1712987654.0,
            actor="brutex",
            action="RUN_START",
            subject="run:abc",
            payload={"k": "v"},
        )
        assert e1.compute_hash() == e2.compute_hash()

    @pytest.mark.parametrize(
        "field",
        ["seq", "prev_hash", "ts_utc", "actor", "action", "subject"],
    )
    def test_hash_changes_when_field_changes(self, field: str) -> None:
        base_kwargs: dict[str, Any] = {
            "seq": 0,
            "prev_hash": GENESIS_PREV_HASH,
            "ts_utc": 1712987654.0,
            "actor": "brutex",
            "action": "RUN_START",
            "subject": "run:abc",
            "payload": {"k": "v"},
        }
        e1 = AuditEntry(**base_kwargs)
        modified_kwargs = dict(base_kwargs)
        modifications: dict[str, Any] = {
            "seq": 1,
            "prev_hash": "f" * 64,
            "ts_utc": 9999999.0,
            "actor": "other",
            "action": "RUN_END",
            "subject": "run:xyz",
        }
        modified_kwargs[field] = modifications[field]
        e2 = AuditEntry(**modified_kwargs)
        assert e1.compute_hash() != e2.compute_hash()

    def test_hash_changes_when_payload_changes(self) -> None:
        e1 = AuditEntry(
            seq=0,
            prev_hash=GENESIS_PREV_HASH,
            ts_utc=1712987654.0,
            actor="brutex",
            action="RUN_START",
            subject="run:abc",
            payload={"k": "v"},
        )
        e2 = AuditEntry(
            seq=0,
            prev_hash=GENESIS_PREV_HASH,
            ts_utc=1712987654.0,
            actor="brutex",
            action="RUN_START",
            subject="run:abc",
            payload={"k": "other"},
        )
        assert e1.compute_hash() != e2.compute_hash()

    def test_payload_key_order_does_not_affect_hash(self) -> None:
        """Canonical JSON sorts keys, so dict order shouldn't matter."""
        e1 = AuditEntry(
            seq=0,
            prev_hash=GENESIS_PREV_HASH,
            ts_utc=1712987654.0,
            actor="brutex",
            action="RUN_START",
            subject="run:abc",
            payload={"a": 1, "b": 2},
        )
        e2 = AuditEntry(
            seq=0,
            prev_hash=GENESIS_PREV_HASH,
            ts_utc=1712987654.0,
            actor="brutex",
            action="RUN_START",
            subject="run:abc",
            payload={"b": 2, "a": 1},
        )
        assert e1.compute_hash() == e2.compute_hash()


# ────────────────────────────────────────────────────────────────────────
# AuditEntry JSON round-trip
# ────────────────────────────────────────────────────────────────────────


class TestAuditEntryJson:
    def test_to_json_then_from_json(self) -> None:
        entry = AuditEntry(
            seq=0,
            prev_hash=GENESIS_PREV_HASH,
            ts_utc=1712987654.0,
            actor="brutex",
            action="RUN_START",
            subject="run:abc",
            payload={"universe": 223, "target_intervals": ["1m", "5m"]},
            entry_hash="a" * 64,
        )
        line = entry.to_json_line()
        parsed = AuditEntry.from_json_line(line)
        assert parsed == entry

    def test_from_json_missing_field_raises(self) -> None:
        line = json.dumps({"seq": 0, "prev_hash": GENESIS_PREV_HASH})
        with pytest.raises(AuditError, match="missing required fields"):
            AuditEntry.from_json_line(line)

    def test_from_json_malformed_raises(self) -> None:
        with pytest.raises(AuditError, match="Malformed audit line"):
            AuditEntry.from_json_line("{not json")


# ────────────────────────────────────────────────────────────────────────
# AuditWriter — append path
# ────────────────────────────────────────────────────────────────────────


class TestAuditWriterAppend:
    def test_single_append(self, audit_path: Path) -> None:
        with AuditWriter.open(audit_path) as log:
            entry = log.append(
                action="RUN_START",
                subject="run:abc",
                payload={"universe": 223},
                ts_utc=1712987654.0,
            )
        assert audit_path.exists()
        assert entry.seq == 0
        assert entry.prev_hash == GENESIS_PREV_HASH
        assert entry.entry_hash == entry.compute_hash()
        assert len(entry.entry_hash) == 64

    def test_chain_advances_across_appends(self, audit_path: Path) -> None:
        with AuditWriter.open(audit_path) as log:
            e1 = log.append(action="RUN_START", subject="run:abc", ts_utc=1.0)
            e2 = log.append(action="PHASE_START", subject="A", ts_utc=2.0)
            e3 = log.append(action="PHASE_DONE", subject="A", ts_utc=3.0)

        assert e1.seq == 0
        assert e2.seq == 1
        assert e3.seq == 2
        assert e2.prev_hash == e1.entry_hash
        assert e3.prev_hash == e2.entry_hash

    def test_file_contains_jsonl(self, audit_path: Path) -> None:
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s1", ts_utc=1.0)
            log.append(action="B", subject="s2", ts_utc=2.0)
        lines = audit_path.read_text().splitlines()
        assert len(lines) == 2
        for line in lines:
            parsed = json.loads(line)
            assert "seq" in parsed
            assert "entry_hash" in parsed

    def test_empty_payload_defaults_to_empty_dict(self, audit_path: Path) -> None:
        with AuditWriter.open(audit_path) as log:
            entry = log.append(action="RUN_START", subject="run:abc", ts_utc=1.0)
        assert entry.payload == {}

    def test_append_after_close_raises(self, audit_path: Path) -> None:
        log = AuditWriter.open(audit_path)
        log.append(action="RUN_START", subject="run:abc", ts_utc=1.0)
        log.close()
        with pytest.raises(AuditWriteError, match="closed"):
            log.append(action="RUN_END", subject="run:abc")


# ────────────────────────────────────────────────────────────────────────
# AuditWriter.open — file semantics
# ────────────────────────────────────────────────────────────────────────


class TestAuditWriterOpen:
    def test_creates_parent_dir(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "nested" / "path" / "audit.jsonl"
        assert not nested.parent.exists()
        with AuditWriter.open(nested) as log:
            log.append(action="A", subject="s", ts_utc=1.0)
        assert nested.exists()

    def test_refuses_to_overwrite_without_resume(self, audit_path: Path) -> None:
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s", ts_utc=1.0)
        with pytest.raises(AuditWriteError, match="already exists"):
            AuditWriter.open(audit_path)

    def test_resume_continues_chain(self, audit_path: Path) -> None:
        # Write initial entries
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s1", ts_utc=1.0)
            log.append(action="B", subject="s2", ts_utc=2.0)
        # Resume
        with AuditWriter.open(audit_path, resume=True) as log:
            entry = log.append(action="C", subject="s3", ts_utc=3.0)
        # Continue correctly
        assert entry.seq == 2
        entries = read_entries(audit_path)
        assert len(entries) == 3
        # Verify whole chain
        assert verify_chain(audit_path) == 3

    def test_resume_on_empty_file(self, audit_path: Path) -> None:
        """Open once (create empty), close without writes, then resume."""
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.touch()
        with AuditWriter.open(audit_path, resume=True) as log:
            entry = log.append(action="A", subject="s", ts_utc=1.0)
        assert entry.seq == 0

    def test_resume_on_missing_file(self, audit_path: Path) -> None:
        """Resume mode on a non-existent file should just create it."""
        assert not audit_path.exists()
        with AuditWriter.open(audit_path, resume=True) as log:
            entry = log.append(action="A", subject="s", ts_utc=1.0)
        assert entry.seq == 0
        assert audit_path.exists()


# ────────────────────────────────────────────────────────────────────────
# verify_chain — happy path
# ────────────────────────────────────────────────────────────────────────


class TestVerifyChainHappy:
    def test_empty_file_verifies(self, audit_path: Path) -> None:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.touch()
        assert verify_chain(audit_path) == 0

    def test_single_entry_verifies(self, audit_path: Path) -> None:
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s", ts_utc=1.0)
        assert verify_chain(audit_path) == 1

    def test_many_entries_verify(self, audit_path: Path) -> None:
        with AuditWriter.open(audit_path) as log:
            for i in range(100):
                log.append(action="X", subject=f"s{i}", ts_utc=float(i))
        assert verify_chain(audit_path) == 100


# ────────────────────────────────────────────────────────────────────────
# verify_chain — failure modes (tamper detection)
# ────────────────────────────────────────────────────────────────────────


def _tamper_line(path: Path, line_idx: int, field: str, new_value: object) -> None:
    """Modify one field in one line of the audit file."""
    lines = path.read_text().splitlines()
    data = json.loads(lines[line_idx])
    data[field] = new_value
    lines[line_idx] = json.dumps(data)
    path.write_text("\n".join(lines) + "\n")


class TestVerifyChainTamper:
    def test_detects_modified_payload(self, audit_path: Path) -> None:
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s", payload={"k": "v"}, ts_utc=1.0)
            log.append(action="B", subject="s", payload={"k": "v2"}, ts_utc=2.0)
        # Tamper with the payload of entry 0 — entry_hash won't match
        _tamper_line(audit_path, 0, "payload", {"k": "HACKED"})
        with pytest.raises(AuditChainCorruption) as exc_info:
            verify_chain(audit_path)
        assert exc_info.value.seq == 0
        assert "tampered" in exc_info.value.reason.lower()

    def test_detects_modified_actor(self, audit_path: Path) -> None:
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s", ts_utc=1.0)
        _tamper_line(audit_path, 0, "actor", "attacker")
        with pytest.raises(AuditChainCorruption):
            verify_chain(audit_path)

    def test_detects_modified_action(self, audit_path: Path) -> None:
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s", ts_utc=1.0)
        _tamper_line(audit_path, 0, "action", "MALICIOUS")
        with pytest.raises(AuditChainCorruption):
            verify_chain(audit_path)

    def test_detects_modified_subject(self, audit_path: Path) -> None:
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s", ts_utc=1.0)
        _tamper_line(audit_path, 0, "subject", "different")
        with pytest.raises(AuditChainCorruption):
            verify_chain(audit_path)

    def test_detects_modified_ts(self, audit_path: Path) -> None:
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s", ts_utc=1.0)
        _tamper_line(audit_path, 0, "ts_utc", 99999.0)
        with pytest.raises(AuditChainCorruption):
            verify_chain(audit_path)

    def test_detects_broken_prev_hash_link(self, audit_path: Path) -> None:
        """Modify the second entry's prev_hash to break the chain link."""
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s", ts_utc=1.0)
            log.append(action="B", subject="s", ts_utc=2.0)
        _tamper_line(audit_path, 1, "prev_hash", "f" * 64)
        with pytest.raises(AuditChainCorruption) as exc_info:
            verify_chain(audit_path)
        assert exc_info.value.seq == 1

    def test_detects_out_of_order_seq(self, audit_path: Path) -> None:
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s", ts_utc=1.0)
            log.append(action="B", subject="s", ts_utc=2.0)
        # Swap seq numbers (entry 1 now claims seq=5)
        _tamper_line(audit_path, 1, "seq", 5)
        with pytest.raises(AuditChainCorruption) as exc_info:
            verify_chain(audit_path)
        assert exc_info.value.seq == 5  # reported from the offending entry

    def test_detects_direct_entry_hash_forgery(self, audit_path: Path) -> None:
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s", payload={"v": 1}, ts_utc=1.0)
        _tamper_line(audit_path, 0, "entry_hash", "e" * 64)
        with pytest.raises(AuditChainCorruption):
            verify_chain(audit_path)

    def test_missing_file_raises_audit_error(self, tmp_path: Path) -> None:
        with pytest.raises(AuditError, match="not found"):
            verify_chain(tmp_path / "does-not-exist.jsonl")

    def test_malformed_line_raises_audit_error(self, audit_path: Path) -> None:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text("{this is not json\n")
        with pytest.raises(AuditError):
            verify_chain(audit_path)


# ────────────────────────────────────────────────────────────────────────
# read_entries (public API)
# ────────────────────────────────────────────────────────────────────────


class TestReadEntries:
    def test_read_returns_ordered_entries(self, audit_path: Path) -> None:
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s1", ts_utc=1.0)
            log.append(action="B", subject="s2", ts_utc=2.0)
            log.append(action="C", subject="s3", ts_utc=3.0)
        entries = read_entries(audit_path)
        assert [e.action for e in entries] == ["A", "B", "C"]
        assert [e.seq for e in entries] == [0, 1, 2]

    def test_read_skips_blank_lines(self, audit_path: Path) -> None:
        """Some legacy tooling may insert blank lines; we tolerate them."""
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s", ts_utc=1.0)
        # Append a blank line
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write("\n")
        entries = read_entries(audit_path)
        assert len(entries) == 1


class TestWriterEdges:
    """The raw-constructor context-manager path + the write-failure branch."""

    def test_enter_opens_fp_for_raw_constructed_writer(self, audit_path: Path) -> None:
        # A writer built via the raw constructor (NOT .open()) has no fp until
        # __enter__ opens one in append mode (audit.py __enter__ branch).
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        writer = AuditWriter(path=audit_path)
        with writer as log:
            log.append(action="A", subject="s", ts_utc=1.0)
        assert verify_chain(audit_path) == 1

    def test_append_wraps_write_oserror(self, audit_path: Path) -> None:
        # An OSError from the underlying file (e.g. disk full) is wrapped as
        # AuditWriteError naming the seq + path (audit.py append OSError branch).
        class _BadFP:
            closed = False

            def write(self, _s: str) -> int:
                raise OSError("disk full")

            def flush(self) -> None:  # pragma: no cover — never reached
                pass

        audit_path.parent.mkdir(parents=True, exist_ok=True)
        writer = AuditWriter.open(audit_path)
        real_fp = writer._fp  # pyright: ignore[reportPrivateUsage]
        try:
            writer._fp = _BadFP()  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage]
            with pytest.raises(AuditWriteError, match="Failed to write audit entry seq=0"):
                writer.append(action="A", subject="s", ts_utc=1.0)
        finally:
            if real_fp is not None:
                real_fp.close()  # close the real fd we swapped out (no leak)

    def test_close_is_idempotent_when_already_closed(self, audit_path: Path) -> None:
        # A second close() (or close() on a never-opened writer) is a no-op —
        # the `self._fp is not None` guard's False branch (audit.py close()).
        writer = AuditWriter.open(audit_path)
        writer.close()
        writer.close()  # _fp is None now → guard skips → clean no-op


# ────────────────────────────────────────────────────────────────────────
# Mutation-killing pins (DEC-MUTATION-RUNTIME-001)
#
# Every assertion below is byte-exact `==` on the full value — substring
# `match=` cannot kill mutmut's XX-wrap string mutants because the
# original text survives inside the wrap.
# ────────────────────────────────────────────────────────────────────────


class TestAuditChainCorruptionPins:
    """Byte-exact pins on AuditChainCorruption attributes + messages."""

    def test_attributes_and_message_pinned_byte_exact(self) -> None:
        # Kills the `self.expected = None` / `self.actual = None` attribute
        # mutants (the message uses the PARAMETERS, so it stays correct
        # under them) and the XX-wrapped message f-string mutant.
        exc = AuditChainCorruption(seq=3, expected="aaa", actual="bbb", reason="why")
        assert exc.seq == 3
        assert exc.expected == "aaa"
        assert exc.actual == "bbb"
        assert exc.reason == "why"
        assert str(exc) == "Audit chain corruption at seq=3: why. Expected 'aaa', got 'bbb'."

    def test_out_of_order_seq_pinned_byte_exact(self, audit_path: Path) -> None:
        # Kills the XX-wrapped "seq number out of order" reason mutant.
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s", ts_utc=1.0)
            log.append(action="B", subject="s", ts_utc=2.0)
        _tamper_line(audit_path, 1, "seq", 5)
        with pytest.raises(AuditChainCorruption) as exc_info:
            verify_chain(audit_path)
        err = exc_info.value
        assert err.reason == "seq number out of order"
        assert err.expected == "1"
        assert err.actual == "5"
        assert str(err) == (
            "Audit chain corruption at seq=5: seq number out of order. Expected '1', got '5'."
        )

    def test_broken_prev_hash_pinned_byte_exact(self, audit_path: Path) -> None:
        # Kills the XX-wrapped prev_hash-mismatch reason mutant.
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s", ts_utc=1.0)
            log.append(action="B", subject="s", ts_utc=2.0)
        first_hash = read_entries(audit_path)[0].entry_hash
        _tamper_line(audit_path, 1, "prev_hash", "f" * 64)
        with pytest.raises(AuditChainCorruption) as exc_info:
            verify_chain(audit_path)
        err = exc_info.value
        assert err.reason == "prev_hash does not match previous entry's entry_hash"
        assert err.expected == first_hash
        assert err.actual == "f" * 64

    def test_forged_entry_hash_pinned_byte_exact(self, audit_path: Path) -> None:
        # Kills the XX-wrapped content-tampered reason mutant.
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s", payload={"v": 1}, ts_utc=1.0)
        _tamper_line(audit_path, 0, "entry_hash", "e" * 64)
        recomputed = read_entries(audit_path)[0].compute_hash()
        with pytest.raises(AuditChainCorruption) as exc_info:
            verify_chain(audit_path)
        err = exc_info.value
        assert err.reason == "entry_hash does not match recomputed hash (content tampered)"
        assert err.expected == recomputed
        assert err.actual == "e" * 64


class TestCanonicalSerialisationPins:
    """Golden byte-exact pins on the canonical-JSON + hash recipe."""

    def test_compute_hash_golden_value(self) -> None:
        # Golden value computed via an INDEPENDENT reimplementation of the
        # documented recipe — sha256 over "|"-joined seq / prev_hash /
        # ts_utc / actor / action / subject / canonical_json(payload),
        # UTF-8, raw "₹" (no \\u escape). Kills all six `b"|"` → `b"XX|XX"`
        # separator mutants and the `ensure_ascii=False` → `True` flip,
        # which the determinism/sensitivity tests above cannot see.
        entry = AuditEntry(
            seq=0,
            prev_hash=GENESIS_PREV_HASH,
            ts_utc=1712987654.5,
            actor="brutex",
            action="RUN_START",
            subject="run:abc",
            payload={"note": "₹100"},
        )
        assert entry.compute_hash() == (
            "2fddb7a79ba93681936238f756fbddd7bef4d435e2c7fb0dabdcf456fc96bdff"
        )

    def test_to_json_line_pinned_byte_exact_with_non_ascii(self) -> None:
        # Pins key spelling, key order, compact separators, and raw-UTF-8
        # output byte-exact — kills the `ensure_ascii` flip ("₹" must
        # serialise raw, not as \\u20b9) and the XX-wrapped "action" key.
        entry = AuditEntry(
            seq=0,
            prev_hash=GENESIS_PREV_HASH,
            ts_utc=1712987654.5,
            actor="brutex",
            action="RUN_START",
            subject="run:abc",
            payload={"note": "₹100"},
            entry_hash="a" * 64,
        )
        assert entry.to_json_line() == (
            '{"action":"RUN_START","actor":"brutex","entry_hash":"'
            + "a" * 64
            + '","payload":{"note":"₹100"},"prev_hash":"'
            + GENESIS_PREV_HASH
            + '","seq":0,"subject":"run:abc","ts_utc":1712987654.5}'
        )


class TestAuditEntryDataclassPins:
    """AuditEntry dataclass discipline — frozen + slots + default pins."""

    def _entry(self) -> AuditEntry:
        return AuditEntry(
            seq=0,
            prev_hash=GENESIS_PREV_HASH,
            ts_utc=1.0,
            actor="brutex",
            action="A",
            subject="s",
            payload={},
        )

    def test_frozen(self) -> None:
        # frozen=True — an audit row is immutable once created (kills the
        # `frozen=False` flip).
        with pytest.raises(dataclasses.FrozenInstanceError):
            self._entry().seq = 99  # type: ignore[misc]

    def test_slots(self) -> None:
        # slots=True — no per-instance __dict__ (kills the `slots=False` flip).
        assert not hasattr(self._entry(), "__dict__")
        assert hasattr(AuditEntry, "__slots__")

    def test_entry_hash_defaults_to_empty_string(self) -> None:
        # Pins `field(default="")`: exactly "" (NOT "XXXX", NOT None) until
        # compute_hash populates it.
        assert self._entry().entry_hash == ""


class TestFromJsonLineMessagePins:
    """Full-string pins on from_json_line error messages."""

    def test_malformed_line_message_pinned_byte_exact(self) -> None:
        # Prefix + CPython's own JSONDecodeError text, captured live so the
        # pin survives CPython wording changes but still kills the XX-wrap.
        with pytest.raises(json.JSONDecodeError) as raw_exc:
            json.loads("{not json")
        with pytest.raises(AuditError) as exc_info:
            AuditEntry.from_json_line("{not json")
        assert str(exc_info.value) == f"Malformed audit line: {raw_exc.value}"

    def test_missing_fields_message_pinned_byte_exact(self) -> None:
        line = json.dumps({"seq": 0, "prev_hash": GENESIS_PREV_HASH})
        with pytest.raises(AuditError) as exc_info:
            AuditEntry.from_json_line(line)
        assert str(exc_info.value) == (
            "Audit line missing required fields: "
            "['action', 'actor', 'entry_hash', 'payload', 'subject', 'ts_utc']"
        )


class TestWriterDefaultActorPins:
    """The actor default must be exactly "brutex" on BOTH entry points.

    No earlier test asserts ``entry.actor``, so a mutated default produces
    a self-consistent (still verifiable) chain — only a direct pin kills it.
    """

    def test_raw_constructor_default_actor_is_brutex(self, audit_path: Path) -> None:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with AuditWriter(path=audit_path) as log:
            entry = log.append(action="A", subject="s", ts_utc=1.0)
        assert entry.actor == "brutex"

    def test_open_default_actor_is_brutex(self, audit_path: Path) -> None:
        with AuditWriter.open(audit_path) as log:
            entry = log.append(action="A", subject="s", ts_utc=1.0)
        assert entry.actor == "brutex"


class TestWriterResumePins:
    """Resume must continue from the LAST entry via a live file handle."""

    def test_resume_continues_from_last_of_three_entries(self, audit_path: Path) -> None:
        # THREE entries on purpose: with only two, `entries[-1]` and the
        # `entries[+1]` index mutant coincide (both pick the second row).
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s1", ts_utc=1.0)
            log.append(action="B", subject="s2", ts_utc=2.0)
            log.append(action="C", subject="s3", ts_utc=3.0)
        third_hash = read_entries(audit_path)[2].entry_hash
        with AuditWriter.open(audit_path, resume=True) as log:
            entry = log.append(action="D", subject="s4", ts_utc=4.0)
        assert entry.seq == 3
        assert entry.prev_hash == third_hash
        assert verify_chain(audit_path) == 4

    def test_resume_writer_is_live_without_context_manager(self, audit_path: Path) -> None:
        # `.open(resume=True)` must hand back an ALREADY-writable writer.
        # Appending WITHOUT `with` bypasses __enter__'s reopen fallback,
        # which masks a `writer._fp = None` mutant in the resume branch.
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s", ts_utc=1.0)
        writer = AuditWriter.open(audit_path, resume=True)
        try:
            entry = writer.append(action="B", subject="s", ts_utc=2.0)
        finally:
            writer.close()
        assert entry.seq == 1
        assert verify_chain(audit_path) == 2


class TestAppendGuardPins:
    """The closed-writer guard, timestamp stamping, and failure messages."""

    def test_refuse_overwrite_message_pinned_byte_exact(self, audit_path: Path) -> None:
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s", ts_utc=1.0)
        with pytest.raises(AuditWriteError) as exc_info:
            AuditWriter.open(audit_path)
        assert str(exc_info.value) == (
            f"Audit log {audit_path} already exists and resume=False. "
            "Use resume=True to continue an existing chain or delete "
            "the file if you intend to start over."
        )

    def test_fp_without_closed_attr_is_treated_as_closed(self, audit_path: Path) -> None:
        # The `getattr(..., "closed", True)` DEFAULT arm: an fp-like object
        # with no `closed` attribute must be treated as closed, not writable
        # (the `default=False` mutant happily writes to it and returns).
        class _NoClosedFP:
            def write(self, s: str) -> int:  # pragma: no cover — mutant-only path
                return len(s)

            def flush(self) -> None:  # pragma: no cover — mutant-only path
                pass

        writer = AuditWriter(path=audit_path)
        writer._fp = _NoClosedFP()  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(AuditWriteError):
            writer.append(action="A", subject="s", ts_utc=1.0)

    def test_closed_but_attached_fp_raises_with_exact_message(self, audit_path: Path) -> None:
        # `or` → `and` mutant: a writer whose fp is attached but CLOSED must
        # still raise AuditWriteError — the mutant short-circuits past the
        # guard into a raw ValueError from writing to a closed file. The
        # byte-exact message also kills its XX-wrap mutant.
        writer = AuditWriter.open(audit_path)
        writer._fp.close()  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(AuditWriteError) as exc_info:
            writer.append(action="A", subject="s", ts_utc=1.0)
        assert str(exc_info.value) == f"Cannot append to closed audit writer at {audit_path}"

    def test_explicit_ts_utc_is_stamped_verbatim(self, audit_path: Path) -> None:
        # `is not None` → `is None` mutant silently replaces the injected
        # timestamp with time() — the chain still verifies under it, so only
        # a direct equality pin can see it.
        with AuditWriter.open(audit_path) as log:
            entry = log.append(action="A", subject="s", ts_utc=1712987654.5)
        assert entry.ts_utc == 1712987654.5

    def test_write_failure_message_pinned_byte_exact(self, audit_path: Path) -> None:
        # Same _BadFP swap as test_append_wraps_write_oserror above, but
        # asserting full-string equality on the wrapped message.
        class _BadFP:
            closed = False

            def write(self, _s: str) -> int:
                raise OSError("disk full")

            def flush(self) -> None:  # pragma: no cover — never reached
                pass

        writer = AuditWriter.open(audit_path)
        real_fp = writer._fp  # pyright: ignore[reportPrivateUsage]
        try:
            writer._fp = _BadFP()  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage]
            with pytest.raises(AuditWriteError) as exc_info:
                writer.append(action="A", subject="s", ts_utc=1.0)
        finally:
            if real_fp is not None:
                real_fp.close()  # close the real fd we swapped out (no leak)
        assert str(exc_info.value) == (
            f"Failed to write audit entry seq=0 to {audit_path}: disk full"
        )


class TestReadEntriesPins:
    """_read_entries line handling + error messages, pinned byte-exact."""

    def test_missing_file_message_pinned_byte_exact(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist.jsonl"
        with pytest.raises(AuditError) as exc_info:
            read_entries(missing)
        assert str(exc_info.value) == f"Audit log not found: {missing}"

    def test_junk_line_error_pins_line_number_and_message(self, audit_path: Path) -> None:
        # A bare "X" line is the discriminating probe: (a) enumerate must
        # start at 1 (":1:" kills the `start=2` mutant); (b) rstrip must
        # strip ONLY "\n" — the rstrip("XX\nXX") mutant ALSO strips the X,
        # leaving a blank line that is silently skipped instead of raising;
        # (c) the wrapper message is pinned byte-exact against its XX-wrap.
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text("X\n", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError) as raw_exc:
            json.loads("X")
        with pytest.raises(AuditError) as exc_info:
            read_entries(audit_path)
        assert str(exc_info.value) == f"{audit_path}:1: Malformed audit line: {raw_exc.value}"

    def test_blank_line_in_middle_does_not_stop_reading(self, audit_path: Path) -> None:
        # The blank-line `continue` must SKIP, not `break`: entries after an
        # interior blank line still count. (The existing blank-line test puts
        # the blank at EOF, where continue and break coincide.)
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s1", ts_utc=1.0)
            log.append(action="B", subject="s2", ts_utc=2.0)
        lines = audit_path.read_text(encoding="utf-8").splitlines()
        audit_path.write_text(lines[0] + "\n\n" + lines[1] + "\n", encoding="utf-8")
        entries = read_entries(audit_path)
        assert [e.action for e in entries] == ["A", "B"]
        assert verify_chain(audit_path) == 2


# ────────────────────────────────────────────────────────────────────────
# RFC-safe JSON — NaN / Inf rejection (DEC-AUDIT-FSYNC-DURABILITY-001 #2)
#
# `json.dumps` defaults to `allow_nan=True`, emitting the bare tokens
# `NaN` / `Infinity` / `-Infinity` — NOT valid RFC 8259 JSON. The audit
# writer now serialises with `allow_nan=False` and maps the resulting
# ValueError to AuditPayloadError, so a non-finite metric value can never
# be written into the tamper-evident chain.
# ────────────────────────────────────────────────────────────────────────


class TestRfcSafeJson:
    @pytest.mark.parametrize(
        "bad_value",
        [float("nan"), float("inf"), float("-inf")],
        ids=["nan", "inf", "-inf"],
    )
    def test_append_rejects_non_finite_payload(self, audit_path: Path, bad_value: float) -> None:
        # A NaN / Inf metric value is REFUSED at append time with a clear
        # domain error — never silently written as non-RFC JSON.
        with (
            AuditWriter.open(audit_path) as log,
            pytest.raises(AuditPayloadError, match="not RFC 8259-serialisable"),
        ):
            log.append(action="RUN_STATS", subject="s", payload={"sharpe": bad_value})

    def test_rejected_append_does_not_corrupt_the_chain(self, audit_path: Path) -> None:
        # The bad append must leave the on-disk chain untouched: a good
        # entry before it, a good entry after it, and the chain still
        # verifies with EXACTLY the two good entries (the bad one absent).
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s1", payload={"ok": 1.0}, ts_utc=1.0)
            with pytest.raises(AuditPayloadError):
                log.append(action="BAD", subject="s2", payload={"x": math.inf}, ts_utc=2.0)
            after = log.append(action="C", subject="s3", payload={"ok": 2.0}, ts_utc=3.0)
        # The rejected append did NOT advance the chain — `after` is seq 1.
        assert after.seq == 1
        entries = read_entries(audit_path)
        assert [e.action for e in entries] == ["A", "C"]
        assert [e.seq for e in entries] == [0, 1]
        assert verify_chain(audit_path) == 2

    def test_nested_non_finite_is_rejected(self, audit_path: Path) -> None:
        # The check is recursive (json.dumps walks the whole structure) —
        # a NaN buried in a nested list/dict is caught too.
        with (
            AuditWriter.open(audit_path) as log,
            pytest.raises(AuditPayloadError),
        ):
            log.append(
                action="A",
                subject="s",
                payload={"nested": {"metrics": [1.0, float("nan"), 3.0]}},
            )

    def test_canonical_json_rejection_message_pinned_byte_exact(self, audit_path: Path) -> None:
        # Pin the full message (prefix + CPython's own ValueError text,
        # captured live so it survives wording changes but kills the XX-wrap).
        with pytest.raises(ValueError) as raw_exc:
            json.dumps(float("nan"), allow_nan=False)
        with (
            AuditWriter.open(audit_path) as log,
            pytest.raises(AuditPayloadError) as exc_info,
        ):
            log.append(action="A", subject="s", payload={"x": float("nan")})
        assert str(exc_info.value) == (
            "Audit payload is not RFC 8259-serialisable "
            f"(NaN/Infinity not permitted): {raw_exc.value}"
        )

    def test_finite_floats_still_serialise(self, audit_path: Path) -> None:
        # The guard must NOT reject legitimate finite floats — the common
        # case (a real Sharpe / p-value) must still write + verify.
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s", payload={"sharpe": 1.5, "p": 0.0})
        assert verify_chain(audit_path) == 1


# ────────────────────────────────────────────────────────────────────────
# Kill-recovery / fsync durability (DEC-AUDIT-FSYNC-DURABILITY-001 #1 + #3)
#
# A crash mid-append can leave a torn final line (the bytes were written
# but the process died before the line + newline + fsync completed). The
# chain reader must DETECT the broken/partial tail and the chain must stay
# verifiable up to the last DURABLE record.
# ────────────────────────────────────────────────────────────────────────


class TestKillRecovery:
    def test_torn_last_line_is_recovered_not_chained_in(self, audit_path: Path) -> None:
        # Updated contract (DEC-AUDIT-TORN-TAIL-001): a crash that wrote a
        # PARTIAL final line (no closing brace / newline) after two durable
        # entries leaves a TORN TAIL — an interrupted, newline-less append.
        # verify_chain must TOLERATE it: the torn line is uncommitted (it was
        # never newline-terminated), so it is SKIPPED, never chained in as a
        # valid entry, and the durable two-entry prefix verifies cleanly. This
        # is the auto-resume guarantee — an unattended restart must not die on
        # a torn tail. (A torn line that DOES carry a trailing newline — i.e.
        # was fully committed — is real corruption and STILL raises; see
        # test_committed_garbage_final_line_still_raises.)
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s1", ts_utc=1.0)
            log.append(action="B", subject="s2", ts_utc=2.0)
        # Append a torn (truncated, no newline) partial line, as a crash
        # between write() and the next flush/fsync would leave.
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write('{"seq": 2, "prev_hash": "abc", "ts_utc": 3.0, "act')  # torn
        # The torn tail is skipped; the durable prefix verifies to its length.
        assert verify_chain(audit_path) == 2

    def test_committed_garbage_final_line_still_raises(self, audit_path: Path) -> None:
        # The torn-tail tolerance is NARROW: a malformed FINAL line that DOES
        # carry a trailing newline was fully committed by a write(), so garbage
        # there is real corruption (a flipped byte), NOT an interrupted append.
        # It must STILL raise — never be silently skipped (DEC-AUDIT-TORN-TAIL-001).
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s1", ts_utc=1.0)
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write("this is not json\n")  # committed (newline-terminated) garbage
        with pytest.raises(AuditError, match="Malformed audit line"):
            verify_chain(audit_path)

    def test_durable_prefix_recovers_via_resume_after_torn_tail(self, audit_path: Path) -> None:
        # The KEY regression: after a torn tail, the operator recovers by
        # truncating the partial line to the last newline (the standard
        # JSONL recovery), and the durable prefix verifies + resumes
        # unbroken. This proves the chain remains usable up to the last
        # durable record after a crash.
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s1", ts_utc=1.0)
            log.append(action="B", subject="s2", ts_utc=2.0)
        durable = audit_path.read_text(encoding="utf-8")
        # Crash writes a torn partial third line.
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write('{"seq": 2, "prev_hash": "broken')  # torn, no newline

        # Recovery: drop the partial trailing line (everything after the
        # last newline) — the durable, newline-terminated prefix is intact.
        recovered = audit_path.read_text(encoding="utf-8")
        last_nl = recovered.rfind("\n")
        audit_path.write_text(recovered[: last_nl + 1], encoding="utf-8")
        assert audit_path.read_text(encoding="utf-8") == durable

        # The durable prefix verifies to its exact length …
        assert verify_chain(audit_path) == 2
        # … and resume continues the chain unbroken from the last record.
        with AuditWriter.open(audit_path, resume=True) as log:
            entry = log.append(action="C", subject="s3", ts_utc=3.0)
        assert entry.seq == 2
        assert verify_chain(audit_path) == 3

    def test_append_calls_fsync_per_record(
        self, audit_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Durability proof: each append fsyncs the record (O(1) per record,
        # not per byte) — exactly one fsync of the writer's fd per append.
        calls: list[int] = []
        real_fsync = os.fsync

        def _spy_fsync(fd: int) -> None:
            calls.append(fd)
            real_fsync(fd)

        monkeypatch.setattr("brutex.orchestrator.audit.os.fsync", _spy_fsync)
        with AuditWriter.open(audit_path) as log:
            target_fd = log._fp.fileno()  # pyright: ignore[reportPrivateUsage]
            log.append(action="A", subject="s1", ts_utc=1.0)
            log.append(action="B", subject="s2", ts_utc=2.0)
            # Two appends → at least two fsyncs of the log's own fd (the
            # context-manager close adds one more on top).
            assert calls.count(target_fd) >= 2
        assert verify_chain(audit_path) == 2

    def test_fsync_oserror_on_append_is_suppressed_flush_still_durable(
        self, audit_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # On a filesystem / sandbox that rejects fsync on an append fd, the
        # OSError from os.fsync is SUPPRESSED (best-effort, same as close())
        # — the flush above already reached the kernel, so the append
        # succeeds and the chain verifies. A genuine WRITE OSError is still
        # wrapped (covered by test_append_wraps_write_oserror).
        def _boom_fsync(_fd: int) -> None:
            raise OSError("fsync not supported on this fs")

        monkeypatch.setattr("brutex.orchestrator.audit.os.fsync", _boom_fsync)
        with AuditWriter.open(audit_path) as log:
            entry = log.append(action="A", subject="s", ts_utc=1.0)
        assert entry.seq == 0
        assert verify_chain(audit_path) == 1


# ────────────────────────────────────────────────────────────────────────
# FIX B — the hash-preimage delimiter "|" is forbidden in the free-form
# fields (DEC-ORCH-AUDIT-RATE-HARDEN-001). compute_hash joins actor/action/subject
# into the preimage with a literal "|", so a "|" inside any of them would make
# the encoding non-injective: action="A|B", subject="C" and action="A",
# subject="B|C" both flatten to "…|A|B|C|…" → one hash. AuditEntry.__post_init__
# rejects "|" on BOTH the write path (construction) and the read path
# (from_json_line), with NO change to the hash bytes for any conforming entry.
# ────────────────────────────────────────────────────────────────────────


class TestDelimiterCollisionGuard:
    def _entry(self, *, actor: str = "brutex", action: str = "A", subject: str = "s") -> AuditEntry:
        return AuditEntry(
            seq=0,
            prev_hash=GENESIS_PREV_HASH,
            ts_utc=1.0,
            actor=actor,
            action=action,
            subject=subject,
            payload={},
        )

    def test_historical_collision_pair_both_rejected(self) -> None:
        # The exact straddling-collision pair the chain could not previously
        # distinguish: action="A|B", subject="C"  vs  action="A", subject="B|C".
        # Both flattened to the SAME "…|A|B|C|…" preimage → identical hash.
        # Now BOTH are unconstructable, so the collision is impossible.
        with pytest.raises(AuditFieldError):
            self._entry(action="A|B", subject="C")
        with pytest.raises(AuditFieldError):
            self._entry(action="A", subject="B|C")

    def test_pipe_in_actor_rejected(self) -> None:
        with pytest.raises(AuditFieldError, match="'actor'"):
            self._entry(actor="bru|tex")

    def test_pipe_in_action_rejected(self) -> None:
        with pytest.raises(AuditFieldError, match="'action'"):
            self._entry(action="RUN|START")

    def test_pipe_in_subject_rejected(self) -> None:
        with pytest.raises(AuditFieldError, match="'subject'"):
            self._entry(subject="run|abc")

    def test_error_message_names_field_and_value(self) -> None:
        with pytest.raises(AuditFieldError) as exc_info:
            self._entry(subject="run|abc")
        msg = str(exc_info.value)
        assert "'subject'" in msg
        assert "'|'" in msg
        assert "'run|abc'" in msg

    def test_audit_field_error_is_an_audit_error(self) -> None:
        # The new subclass must remain catchable by existing `except AuditError`
        # handlers (doctor / cli / verify_chain / _read_entries) — no caller
        # needs to learn the new type.
        assert issubclass(AuditFieldError, AuditError)

    def test_conforming_fields_construct_fine(self) -> None:
        # The canonical delimiter-free conventions (`-`, `:`) are untouched.
        entry = self._entry(subject="NSE-NIFTY-22Dec25-25000-CE", action="INGEST_CHUNK_DONE")
        assert entry.subject == "NSE-NIFTY-22Dec25-25000-CE"

    def test_append_rejects_pipe_in_subject_without_touching_chain(self, audit_path: Path) -> None:
        # The guard fires at append-time construction BEFORE any write — a
        # good entry, a rejected `|`-bearing append, then a good entry: the
        # chain holds exactly the two good entries.
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s1", ts_utc=1.0)
            with pytest.raises(AuditFieldError):
                log.append(action="B", subject="bad|subject", ts_utc=2.0)
            after = log.append(action="C", subject="s3", ts_utc=3.0)
        assert after.seq == 1
        assert [e.action for e in read_entries(audit_path)] == ["A", "C"]
        assert verify_chain(audit_path) == 2

    def test_from_json_line_rejects_tampered_pipe_field(self) -> None:
        # A tampered on-disk line that injected a "|" into a field is rejected
        # by from_json_line (which rebuilds the entry through __post_init__).
        line = json.dumps(
            {
                "seq": 0,
                "prev_hash": GENESIS_PREV_HASH,
                "ts_utc": 1.0,
                "actor": "brutex",
                "action": "A|B",
                "subject": "C",
                "payload": {},
                "entry_hash": "a" * 64,
            }
        )
        with pytest.raises(AuditFieldError):
            AuditEntry.from_json_line(line)

    def test_verify_chain_rejects_on_disk_pipe_resplit(self, audit_path: Path) -> None:
        # The end-to-end read-path contract: a tampered line re-splitting a
        # "|"-bearing (action, subject) pair is surfaced by verify_chain as an
        # AuditError (the _read_entries wrapper prefixes path:line_no).
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s1", ts_utc=1.0)
            log.append(action="B", subject="s2", ts_utc=2.0)
        lines = audit_path.read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        first["action"] = "A|B"  # inject the delimiter
        lines[0] = json.dumps(first)
        audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with pytest.raises(AuditError, match=f"{audit_path}:1:"):
            verify_chain(audit_path)

    def test_golden_hash_unchanged_by_the_guard(self) -> None:
        # Regression: the "|" guard must NOT move the hash recipe — the frozen
        # golden from TestCanonicalSerialisationPins still holds byte-for-byte.
        entry = AuditEntry(
            seq=0,
            prev_hash=GENESIS_PREV_HASH,
            ts_utc=1712987654.5,
            actor="brutex",
            action="RUN_START",
            subject="run:abc",
            payload={"note": "₹100"},
        )
        assert entry.compute_hash() == (
            "2fddb7a79ba93681936238f756fbddd7bef4d435e2c7fb0dabdcf456fc96bdff"
        )


# ────────────────────────────────────────────────────────────────────────
# FIX B (property) — once "|" is excluded from the field domain the preimage
# is injective: two DISTINCT (actor, action, subject) triples never collide.
# ────────────────────────────────────────────────────────────────────────

# Pipe excluded (forbidden by __post_init__); codec="utf-8" drops lone
# surrogates compute_hash cannot UTF-8 encode.
_PIPE_FREE_TEXT = st.text(alphabet=st.characters(codec="utf-8", exclude_characters="|"), max_size=8)
_TRIPLE = st.tuples(_PIPE_FREE_TEXT, _PIPE_FREE_TEXT, _PIPE_FREE_TEXT)


@given(a=_TRIPLE, b=_TRIPLE)
def test_property_distinct_pipe_free_triples_never_collide(
    a: tuple[str, str, str], b: tuple[str, str, str]
) -> None:
    # With "|" removed from the field domain, the only way two triples share a
    # compute_hash() is if they are equal. (All other preimage components are
    # held fixed.) This is the injectivity the in-band delimiter previously
    # broke for "|"-bearing fields.
    def _hash(triple: tuple[str, str, str]) -> str:
        actor, action, subject = triple
        return AuditEntry(
            seq=0,
            prev_hash=GENESIS_PREV_HASH,
            ts_utc=1.0,
            actor=actor,
            action=action,
            subject=subject,
            payload={},
        ).compute_hash()

    if a == b:
        assert _hash(a) == _hash(b)
    else:
        assert _hash(a) != _hash(b)


# ────────────────────────────────────────────────────────────────────────
# FIX D — a tampered on-disk line whose payload holds a non-finite float
# (json.loads accepts bare NaN / Infinity tokens) is surfaced by verify_chain
# as AuditChainCorruption (NOT the sibling AuditPayloadError), so the
# AuditChainCorruption-catching callers (doctor / cli) behave correctly.
# ────────────────────────────────────────────────────────────────────────


class TestNonFinitePayloadTamper:
    @pytest.mark.parametrize(
        ("token", "label"),
        [("NaN", "nan"), ("Infinity", "inf"), ("-Infinity", "-inf")],
        ids=["nan", "inf", "-inf"],
    )
    def test_tampered_non_finite_payload_is_chain_corruption(
        self, audit_path: Path, token: str, label: str
    ) -> None:
        # Build a valid 2-entry chain, then byte-rewrite the FIRST (non-final)
        # line's payload value to a bare non-finite token. seq + prev_hash of
        # that line are untouched, so verify_chain reaches compute_hash, whose
        # AuditPayloadError must surface as AuditChainCorruption(seq=0).
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s1", payload={"v": 1.0}, ts_utc=1.0)
            log.append(action="B", subject="s2", payload={"v": 2.0}, ts_utc=2.0)
        lines = audit_path.read_text(encoding="utf-8").splitlines()
        assert '"payload":{"v":1.0}' in lines[0]
        lines[0] = lines[0].replace('"payload":{"v":1.0}', f'"payload":{{"v":{token}}}')
        audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        with pytest.raises(AuditChainCorruption) as exc_info:
            verify_chain(audit_path)
        err = exc_info.value
        assert err.seq == 0
        assert err.reason == "payload contains a non-finite float (tampered line)"
        assert err.expected == "a finite, RFC 8259-serialisable payload"
        assert err.actual == "non-finite payload (NaN / Infinity / -Infinity)"

    def test_chain_corruption_is_not_payload_error(self) -> None:
        # The whole point of FIX D: AuditChainCorruption is NOT a subclass of
        # AuditPayloadError (they are siblings), so a caller that catches only
        # AuditChainCorruption (doctor / cli) now catches the NaN tamper.
        assert not issubclass(AuditChainCorruption, AuditPayloadError)
        assert not issubclass(AuditPayloadError, AuditChainCorruption)

    def test_non_finite_in_final_line_also_chain_corruption(self, audit_path: Path) -> None:
        # The final line is equally covered: the last entry's NaN payload is
        # still raised as AuditChainCorruption (seq=1), not AuditPayloadError.
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s1", payload={"v": 1.0}, ts_utc=1.0)
            log.append(action="B", subject="s2", payload={"v": 2.0}, ts_utc=2.0)
        lines = audit_path.read_text(encoding="utf-8").splitlines()
        lines[1] = lines[1].replace('"payload":{"v":2.0}', '"payload":{"v":NaN}')
        audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with pytest.raises(AuditChainCorruption) as exc_info:
            verify_chain(audit_path)
        assert exc_info.value.seq == 1


# ────────────────────────────────────────────────────────────────────────
# BufferedAuditCollector / BufferedAuditRecord / AuditSink seam
# (DEC-MULTICORE-SWEEP-PHASE1-001) — a parallel-sweep worker BUFFERS its
# `SWEEP_*` audit lines as plain data instead of hashing them (it has no view
# of the global chain position); the parent's single writer-actor replays the
# buffered records into the REAL hash chain in canonical enumeration order.
# These tests live HERE (not only in test_engine_sweep_parallel.py) so the
# strict per-module `--cov=brutex.orchestrator.audit` gate measures the
# collector's __init__ / append / records at 100% (its lines are in audit.py).
# ────────────────────────────────────────────────────────────────────────


class TestBufferedAuditCollector:
    """The worker-side buffering :class:`AuditSink` used by the parallel sweep."""

    def test_new_collector_has_no_records(self) -> None:
        """A fresh collector starts empty (covers ``__init__``)."""
        collector = BufferedAuditCollector()
        assert collector.records == ()

    def test_append_buffers_records_in_order(self) -> None:
        """``append`` buffers each line as a :class:`BufferedAuditRecord`, in order."""
        collector = BufferedAuditCollector()
        collector.append(action="SWEEP_START", subject="s1", payload={"x": 1})
        collector.append(action="SWEEP_COMPLETE", subject="s2")
        records = collector.records
        assert [r.action for r in records] == ["SWEEP_START", "SWEEP_COMPLETE"]
        assert [r.subject for r in records] == ["s1", "s2"]
        assert records[0].payload == {"x": 1}
        # A None payload is normalised to an empty dict — never None on the record.
        assert records[1].payload == {}

    def test_append_copies_the_payload(self) -> None:
        """The buffered payload is a COPY — a later caller mutation cannot leak in."""
        collector = BufferedAuditCollector()
        payload = {"k": "v"}
        collector.append(action="SWEEP_FLUSH", subject="s", payload=payload)
        payload["k"] = "MUTATED"
        assert collector.records[0].payload == {"k": "v"}

    def test_append_returns_the_buffered_record(self) -> None:
        """``append`` returns the buffered record (the runner ignores it)."""
        collector = BufferedAuditCollector()
        record = collector.append(action="SWEEP_START", subject="s", payload={"a": 1}, ts_utc=42.0)
        assert isinstance(record, BufferedAuditRecord)
        assert record.action == "SWEEP_START"
        assert record.subject == "s"
        assert record.payload == {"a": 1}
        # ts_utc is accepted for AuditSink signature parity but never stored — the
        # parent's real AuditWriter stamps a chain-correct timestamp at replay time.
        assert not hasattr(record, "ts_utc")

    def test_records_is_an_immutable_snapshot(self) -> None:
        """``records`` returns a tuple SNAPSHOT — a later append never grows it."""
        collector = BufferedAuditCollector()
        collector.append(action="A", subject="s")
        snapshot = collector.records
        assert isinstance(snapshot, tuple)
        collector.append(action="B", subject="s")
        assert len(snapshot) == 1  # the earlier snapshot did not mutate
        assert len(collector.records) == 2

    def test_collector_satisfies_audit_sink_protocol(self) -> None:
        """A collector structurally satisfies the :class:`AuditSink` runtime Protocol."""
        assert isinstance(BufferedAuditCollector(), AuditSink)

    def test_real_writer_also_satisfies_audit_sink(self, audit_path: Path) -> None:
        """The real :class:`AuditWriter` also satisfies :class:`AuditSink` (the seam
        is a pure widening — either impl is accepted by the shared runner)."""
        with AuditWriter.open(audit_path) as writer:
            assert isinstance(writer, AuditSink)


# ---------------------------------------------------------------------------
# open_ingest_audit — the ONE COMMON ingest-audit factory
# (DEC-INGEST-AUDIT-WIRING-001, Golden Rule #8)
# ---------------------------------------------------------------------------


class TestOpenIngestAudit:
    """The COMMON ``<lake>/audit/<name>.jsonl`` factory every ingest entrypoint binds."""

    def test_enabled_opens_fresh_writer_at_lake_convention_path(self, tmp_path: Path) -> None:
        from brutex.orchestrator.audit import open_ingest_audit

        with open_ingest_audit(tmp_path) as writer:
            assert isinstance(writer, AuditWriter)
            writer.append(action="INGEST_RUN_START", subject="pull:t", payload={"jobs": 1})
        path = tmp_path / "audit" / "spot-ingest.jsonl"
        assert path.exists()
        assert verify_chain(path) == 1

    def test_enabled_resumes_the_existing_chain(self, tmp_path: Path) -> None:
        """A second open CONTINUES the chain (resume) — never restarts / overwrites."""
        from brutex.orchestrator.audit import open_ingest_audit

        with open_ingest_audit(tmp_path) as writer:
            assert writer is not None
            writer.append(action="INGEST_RUN_START", subject="pull:a", payload={})
        with open_ingest_audit(tmp_path) as writer:
            assert writer is not None
            entry = writer.append(action="INGEST_RUN_COMPLETE", subject="pull:b", payload={})
        assert entry.seq == 1  # continued, not restarted
        path = tmp_path / "audit" / "spot-ingest.jsonl"
        assert verify_chain(path) == 2

    def test_name_selects_the_fno_trail(self, tmp_path: Path) -> None:
        from brutex.orchestrator.audit import open_ingest_audit

        with open_ingest_audit(tmp_path, name="fno-ingest") as writer:
            assert writer is not None
            writer.append(action="INGEST_RUN_START", subject="pull:f", payload={})
        assert verify_chain(tmp_path / "audit" / "fno-ingest.jsonl") == 1
        assert not (tmp_path / "audit" / "spot-ingest.jsonl").exists()

    def test_disabled_yields_none_and_touches_nothing(self, tmp_path: Path) -> None:
        """A dry-run / inspect pull gets the null context — no file, no directory."""
        from brutex.orchestrator.audit import open_ingest_audit

        with open_ingest_audit(tmp_path, enabled=False) as writer:
            assert writer is None
        assert not (tmp_path / "audit").exists()


# ────────────────────────────────────────────────────────────────────────
# Mutation-survivor kill pack (DEC-MUTATION-RUNTIME-003): the 2026-07-07
# rehearsal (run 28853009068, PR #1239) found 16 surviving mutants in this
# module — clustered in the torn-tail / BufferedAuditRecord code that
# postdates the last audit mutation enumeration — plus an untested tail
# (mutants 174-274) the 120-min job kill never reached; a local scoped
# re-enumeration measured 70 survivors + 5 suspicious against the prior
# suite. The classes below pin the exact behaviours those mutants flip:
# torn-tail recovery semantics, the auto-repair truncation boundary, the
# O(1) tail-read consumption contract, the final-byte discriminator, and
# the buffered-record dataclass discipline.
#
# ORDER MATTERS (deliberate): the scanner-direct classes run BEFORE any
# class that feeds blank-heavy input into _read_tail_entry, because the
# blank-walk loop mutants (`end = start` → None, `end != -1` comparison
# flips) SPIN on blank input but fail FAST on the scanner-direct cases —
# under the mutation runner's `-x` the fast failure must come first so a
# spin mutant records a quick KILLED, never a hung/suspicious run.
# ────────────────────────────────────────────────────────────────────────


class TestAuditFieldErrorMessagePin:
    def test_delimiter_rejection_message_pinned_byte_exact(self) -> None:
        # Byte-exact pin: the diagnostic must name the FIELD, the DELIMITER
        # and the offending VALUE verbatim (kills the f-string mutants on
        # both message halves).
        with pytest.raises(AuditFieldError) as exc_info:
            AuditEntry(
                seq=0,
                prev_hash=GENESIS_PREV_HASH,
                ts_utc=1.0,
                actor="bru|tex",
                action="A",
                subject="s",
                payload={},
            )
        assert str(exc_info.value) == (
            "Audit field 'actor' must not contain the '|' hash-preimage delimiter: 'bru|tex'"
        )


class TestBufferedAuditRecordDataclassPins:
    """BufferedAuditRecord dataclass discipline — frozen + slots flag pins."""

    def _record(self) -> BufferedAuditRecord:
        return BufferedAuditRecord(action="SWEEP_START", subject="s", payload={"i": 1})

    def test_frozen(self) -> None:
        # frozen=True — the record crosses a worker→parent process boundary
        # and must be immutable data (kills the `frozen=False` flip).
        with pytest.raises(dataclasses.FrozenInstanceError):
            self._record().action = "TAMPERED"  # type: ignore[misc]

    def test_slots(self) -> None:
        # slots=True — no per-instance __dict__ (kills the `slots=False` flip).
        assert not hasattr(self._record(), "__dict__")
        assert hasattr(BufferedAuditRecord, "__slots__")


class TestHasTornTailPins:
    """_has_torn_tail — the O(1) final-byte torn discriminator, every boundary pinned."""

    def _probe(self, tmp_path: Path, data: bytes) -> bool:
        p = tmp_path / "log.jsonl"
        p.write_bytes(data)
        return has_torn_tail(p)

    def test_empty_file_is_not_torn(self, tmp_path: Path) -> None:
        # An empty file has no lines, so it can never be torn.
        assert self._probe(tmp_path, b"") is False

    def test_newline_terminated_file_is_not_torn(self, tmp_path: Path) -> None:
        assert self._probe(tmp_path, b'{"seq": 0}\n') is False

    def test_unterminated_final_line_is_torn(self, tmp_path: Path) -> None:
        assert self._probe(tmp_path, b'{"seq": 0}\n{"seq') is True

    def test_single_torn_byte_is_torn(self, tmp_path: Path) -> None:
        # The 1-byte boundary: size == 1 must NOT be mistaken for empty.
        assert self._probe(tmp_path, b"x") is True

    def test_single_newline_file_is_not_torn(self, tmp_path: Path) -> None:
        assert self._probe(tmp_path, b"\n") is False


class TestFindLastNewlineOffsetPins:
    """_find_last_newline_offset — exact absolute offsets across chunk seams."""

    def _offset(self, tmp_path: Path, data: bytes) -> int:
        p = tmp_path / "blob.bin"
        p.write_bytes(data)
        with open(p, "rb") as f:
            return find_last_newline_offset(f, len(data))

    def test_last_newline_in_the_tail_chunk(self, tmp_path: Path) -> None:
        assert self._offset(tmp_path, b"a\nbc\ndef") == 4

    def test_trailing_newline_is_found_at_size_minus_one(self, tmp_path: Path) -> None:
        assert self._offset(tmp_path, b"abc\n") == 3

    def test_no_newline_returns_minus_one(self, tmp_path: Path) -> None:
        assert self._offset(tmp_path, b"abc") == -1

    def test_empty_file_returns_minus_one(self, tmp_path: Path) -> None:
        assert self._offset(tmp_path, b"") == -1

    def test_newline_only_in_an_earlier_chunk(self, tmp_path: Path) -> None:
        # The last "\n" sits more than one full 8192-byte chunk back from
        # EOF, so the answer comes from a NON-final block: the absolute
        # offset must be pos + idx of THAT block (kills the offset
        # arithmetic mutants).
        data = b"x" * 8193 + b"\n" + b"y" * 9000
        assert self._offset(tmp_path, data) == 8193


class TestResumeTornTailAutoRepairPins:
    """resume=True auto-repairs a torn tail through the REAL open() path."""

    def test_resume_truncates_the_torn_tail_to_the_committed_prefix(self, audit_path: Path) -> None:
        # Kills the `_truncate_torn_tail(None)` argument mutant: the repair
        # must run against the REAL log path, byte-exactly restoring the
        # durable prefix before the resumed chain appends.
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s1", ts_utc=1.0)
            log.append(action="B", subject="s2", ts_utc=2.0)
        durable = audit_path.read_bytes()
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write('{"seq": 2, "prev_hash": "torn')  # crash mid-append, no newline
        with AuditWriter.open(audit_path, resume=True) as log:
            # The torn remnant is gone the moment open() returns…
            assert audit_path.read_bytes() == durable
            # …and the chain continues from the last committed entry.
            entry = log.append(action="C", subject="s3", ts_utc=3.0)
        assert entry.seq == 2
        assert verify_chain(audit_path) == 3


class TestReadTailEntryPins:
    """_read_tail_entry — the O(1) resume tail-read: boundaries + multi-chunk walks."""

    def _chain(self, path: Path, n: int) -> list[AuditEntry]:
        with AuditWriter.open(path) as log:
            return [log.append(action="A", subject=f"s:{i}", ts_utc=float(i)) for i in range(n)]

    def _raw(self, audit_path: Path, data: bytes) -> Path:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_bytes(data)
        return audit_path

    def test_clean_file_returns_last_entry_and_no_torn_flag(self, audit_path: Path) -> None:
        entries = self._chain(audit_path, 3)
        assert read_tail_entry(audit_path) == (entries[-1], False)

    def test_torn_tail_returns_last_committed_entry_and_flag(self, audit_path: Path) -> None:
        entries = self._chain(audit_path, 2)
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write('{"seq": 2, "prev')
        assert read_tail_entry(audit_path) == (entries[-1], True)

    def test_empty_file_returns_none_and_no_torn_flag(self, audit_path: Path) -> None:
        assert read_tail_entry(self._raw(audit_path, b"")) == (None, False)

    def test_torn_only_file_returns_none_with_flag(self, audit_path: Path) -> None:
        assert read_tail_entry(self._raw(audit_path, b'{"seq": 0, "never')) == (None, True)

    def test_single_torn_byte_returns_none_with_flag(self, audit_path: Path) -> None:
        # The size == 1 boundary must not short-circuit as empty.
        assert read_tail_entry(self._raw(audit_path, b"x")) == (None, True)

    def test_blank_only_file_returns_none_without_flag(self, audit_path: Path) -> None:
        # Newline-terminated blank filler is committed-but-benign: no entry,
        # and NOT torn (the file ends in "\n").
        assert read_tail_entry(self._raw(audit_path, b"\n\n\n")) == (None, False)

    def test_multi_chunk_torn_tail_recovers_the_entry(self, audit_path: Path) -> None:
        # The torn remnant spans >2 reverse-seek chunks (20000 > 2 × 8192):
        # the walk must keep accumulating until the committed entry appears.
        entries = self._chain(audit_path, 1)
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write('{"pad": "' + "x" * 20000)
        assert read_tail_entry(audit_path) == (entries[-1], True)

    def test_blank_padding_before_tail_walks_back_to_the_entry(self, audit_path: Path) -> None:
        # >1 chunk of committed blank filler after the only entry exercises
        # the candidate-None continue path (blank lines are walked over).
        entries = self._chain(audit_path, 1)
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write("\n" * 9000)
        assert read_tail_entry(audit_path) == (entries[-1], False)

    def test_giant_last_entry_spanning_chunks_is_recovered(self, audit_path: Path) -> None:
        # A committed FINAL entry larger than one 8192-byte chunk: the first
        # chunk holds no newline, so the walk accumulates backwards first.
        with AuditWriter.open(audit_path) as log:
            log.append(action="A", subject="s:0", ts_utc=1.0)
            big = log.append(action="B", subject="s:1", payload={"pad": "y" * 12000}, ts_utc=2.0)
        assert read_tail_entry(audit_path) == (big, False)

    def test_tail_read_consumes_only_the_final_chunk(
        self, audit_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The O(1)-tail contract itself (DEC-AUDIT-RESUME-TAILREAD-001):
        # when the last committed entry sits inside the FINAL 8192-byte
        # chunk of a multi-chunk log, the reverse walk stops after ONE
        # chunk — a mutant that degrades the early-exit into an
        # accumulate-the-whole-log walk changes NO answer, only the read
        # complexity, so the consumption count is the observable.
        entries = self._chain(audit_path, 300)
        assert audit_path.stat().st_size > 3 * 8192  # the fixture really spans >3 chunks
        consumed: list[int] = []

        def _spy(f: BinaryIO, size: int) -> Iterator[tuple[int, bytes]]:
            for item in iter_reverse_chunks(f, size):
                consumed.append(item[0])
                yield item

        monkeypatch.setattr("brutex.orchestrator.audit._iter_reverse_chunks", _spy)
        assert read_tail_entry(audit_path) == (entries[-1], False)
        assert len(consumed) == 1

    def test_committed_trailing_garbage_is_not_trimmed(self, audit_path: Path) -> None:
        # A committed (newline-terminated) line carrying trailing junk after
        # the JSON is REAL corruption: it must raise, never be silently
        # repaired by over-stripping the line.
        self._chain(audit_path, 1)
        raw = audit_path.read_bytes()
        self._raw(audit_path, raw[:-1] + b"X\n")  # "…}X\n" — committed, malformed
        with pytest.raises(AuditError):
            read_tail_entry(audit_path)

    def test_missing_file_message_pinned_byte_exact(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.jsonl"
        with pytest.raises(AuditError) as exc_info:
            read_tail_entry(missing)
        assert str(exc_info.value) == f"Audit log not found: {missing}"


class TestTruncateTornTailPins:
    """_truncate_torn_tail — byte-exact repair boundary + error-wrap pins."""

    def test_keeps_exactly_the_committed_prefix(self, tmp_path: Path) -> None:
        p = tmp_path / "log.jsonl"
        committed = b'{"seq": 0}\n{"seq": 1}\n'
        p.write_bytes(committed + b'{"seq": 2, "torn')
        truncate_torn_tail(p)
        assert p.read_bytes() == committed

    def test_two_byte_committed_prefix_is_kept_exactly(self, tmp_path: Path) -> None:
        # The smallest interesting cut: the last "\n" at offset 1 (the
        # keep-boundary arithmetic must hold at tiny offsets too).
        p = tmp_path / "log.jsonl"
        p.write_bytes(b"a\ntorn")
        truncate_torn_tail(p)
        assert p.read_bytes() == b"a\n"

    def test_multi_chunk_torn_tail_repairs_to_the_last_newline(self, tmp_path: Path) -> None:
        # A torn remnant spanning >2 chunks must still cut at EXACTLY the
        # last "\n" — one byte more would keep torn garbage, one byte less
        # would eat the committed terminator.
        p = tmp_path / "log.jsonl"
        committed = b'{"seq": 0}\n'
        p.write_bytes(committed + b"z" * 20000)
        truncate_torn_tail(p)
        assert p.read_bytes() == committed

    def test_no_committed_newline_truncates_to_empty(self, tmp_path: Path) -> None:
        p = tmp_path / "log.jsonl"
        p.write_bytes(b"never terminated")
        truncate_torn_tail(p)
        assert p.read_bytes() == b""

    def test_clean_file_is_unchanged(self, tmp_path: Path) -> None:
        p = tmp_path / "log.jsonl"
        committed = b'{"seq": 0}\n'
        p.write_bytes(committed)
        truncate_torn_tail(p)
        assert p.read_bytes() == committed

    def test_repair_is_idempotent(self, tmp_path: Path) -> None:
        p = tmp_path / "log.jsonl"
        committed = b'{"seq": 0}\n'
        p.write_bytes(committed + b"torn")
        truncate_torn_tail(p)
        truncate_torn_tail(p)
        assert p.read_bytes() == committed

    def test_io_error_wraps_with_pinned_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        p = tmp_path / "log.jsonl"
        p.write_bytes(b"torn")

        def _boom(_fd: int, _length: int) -> None:
            raise OSError("boom")

        monkeypatch.setattr("brutex.orchestrator.audit.os.truncate", _boom)
        with pytest.raises(AuditWriteError) as exc_info:
            truncate_torn_tail(p)
        assert str(exc_info.value) == f"Failed to truncate torn tail of audit log {p}: boom"
