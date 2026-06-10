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
