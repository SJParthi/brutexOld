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

import json
from pathlib import Path
from typing import Any

import pytest
from brutex.orchestrator.audit import (
    GENESIS_PREV_HASH,
    AuditChainCorruption,
    AuditEntry,
    AuditError,
    AuditWriteError,
    AuditWriter,
    read_entries,
    verify_chain,
)

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
        assert GENESIS_PREV_HASH == "0" * 64
        assert len(GENESIS_PREV_HASH) == 64


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
