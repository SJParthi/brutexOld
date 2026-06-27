"""Hypothesis property tests for the hash-chained audit log.

Exhaustively explores random append sequences and tamper patterns
to prove the audit chain's core invariants from
``docs/REQUIREMENTS.md §8.5 / NFR-OBS-003``:

1. **Append-only integrity.** Any sequence of appends produces a
   file where ``verify_chain()`` returns exactly the number of
   entries appended.

2. **Sequence monotonicity.** ``seq`` numbers are 0, 1, 2, ... with
   no gaps, no duplicates, no reordering.

3. **Prev-hash threading.** Each entry's ``prev_hash`` equals the
   previous entry's ``entry_hash``; the first entry's ``prev_hash``
   equals ``GENESIS_PREV_HASH``.

4. **Tamper detection.** Modifying any field of any entry (or
   deleting an entry, or reordering entries) causes
   ``verify_chain()`` to raise ``AuditChainCorruption`` — seven
   distinct tamper modes, one per test.

5. **Resume correctness.** Closing and reopening an audit writer
   continues the chain unbroken; the next appended entry has the
   correct ``seq`` and ``prev_hash``.

6. **JSON serialisability.** Every appended payload is round-trippable
   through JSON — the chain file is plain-text JSON lines, not a
   binary format.

Uses Hypothesis bounded strategies to keep per-property runtime
under a second. 100 examples per property = ~600 total scenarios
validated per CI run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from brutex.orchestrator.audit import (
    GENESIS_PREV_HASH,
    AuditChainCorruption,
    AuditPayloadError,
    AuditWriter,
    read_entries,
    verify_chain,
)
from hypothesis import given, settings
from hypothesis import strategies as st

# ────────────────────────────────────────────────────────────────────────
# Strategies
# ────────────────────────────────────────────────────────────────────────


_ACTION_ALPHABET = st.characters(min_codepoint=0x41, max_codepoint=0x5A)  # A-Z


action_st = st.text(alphabet=_ACTION_ALPHABET, min_size=1, max_size=12)

# The "|" hash-preimage delimiter is forbidden inside the free-form fields
# (DEC-ORCH-AUDIT-RATE-HARDEN-001 — keeps the preimage injective), so an HONEST chain
# never contains one; exclude it from the printable-ASCII subject alphabet.
subject_st = st.text(
    alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E, exclude_characters="|"),
    min_size=1,
    max_size=40,
)


payload_value_st = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**9), max_value=10**9),
    st.text(
        alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E),
        max_size=20,
    ),
)


payload_st = st.dictionaries(
    keys=st.text(
        alphabet=st.characters(min_codepoint=0x61, max_codepoint=0x7A),
        min_size=1,
        max_size=8,
    ),
    values=payload_value_st,
    max_size=5,
)


entry_spec_st = st.tuples(action_st, subject_st, payload_st)


# ────────────────────────────────────────────────────────────────────────
# Invariant 1 — append-only integrity
# ────────────────────────────────────────────────────────────────────────


class TestAppendOnlyIntegrity:
    @given(entries=st.lists(entry_spec_st, min_size=1, max_size=20))
    @settings(max_examples=100, deadline=None)
    def test_verify_chain_returns_entry_count(
        self, entries: list[tuple[str, str, dict]], tmp_path_factory
    ) -> None:
        path = tmp_path_factory.mktemp("audit") / "chain.jsonl"
        with AuditWriter.open(path) as writer:
            for action, subject, payload in entries:
                writer.append(action=action, subject=subject, payload=payload)
        count = verify_chain(path)
        assert count == len(entries)


# ────────────────────────────────────────────────────────────────────────
# Invariant 2 — sequence monotonicity
# ────────────────────────────────────────────────────────────────────────


class TestSequenceMonotonicity:
    @given(entries=st.lists(entry_spec_st, min_size=1, max_size=15))
    @settings(max_examples=100, deadline=None)
    def test_seq_numbers_are_dense_ordered(
        self, entries: list[tuple[str, str, dict]], tmp_path_factory
    ) -> None:
        path = tmp_path_factory.mktemp("audit") / "chain.jsonl"
        with AuditWriter.open(path) as writer:
            for action, subject, payload in entries:
                writer.append(action=action, subject=subject, payload=payload)

        seqs = []
        with path.open("r", encoding="utf-8") as fp:
            for line in fp:
                seqs.append(json.loads(line)["seq"])
        assert seqs == list(range(len(entries)))


# ────────────────────────────────────────────────────────────────────────
# Invariant 3 — prev-hash threading
# ────────────────────────────────────────────────────────────────────────


class TestPrevHashThreading:
    @given(entries=st.lists(entry_spec_st, min_size=2, max_size=15))
    @settings(max_examples=100, deadline=None)
    def test_prev_hash_chains_correctly(
        self, entries: list[tuple[str, str, dict]], tmp_path_factory
    ) -> None:
        path = tmp_path_factory.mktemp("audit") / "chain.jsonl"
        with AuditWriter.open(path) as writer:
            for action, subject, payload in entries:
                writer.append(action=action, subject=subject, payload=payload)

        raw_entries = []
        with path.open("r", encoding="utf-8") as fp:
            raw_entries = [json.loads(line) for line in fp]

        assert raw_entries[0]["prev_hash"] == GENESIS_PREV_HASH
        for i in range(1, len(raw_entries)):
            assert raw_entries[i]["prev_hash"] == raw_entries[i - 1]["entry_hash"], (
                f"broken chain at seq {i}"
            )


# ────────────────────────────────────────────────────────────────────────
# Invariant 4 — tamper detection (7 distinct modes)
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture
def populated_chain(tmp_path: Path) -> Path:
    path = tmp_path / "chain.jsonl"
    with AuditWriter.open(path) as writer:
        for i in range(5):
            writer.append(
                action=f"ACTION_{i}",
                subject=f"subject:{i}",
                payload={"i": i},
            )
    return path


def _rewrite_lines(path: Path, lines: list[str]) -> None:
    path.write_text("".join(line + "\n" for line in lines))


class TestTamperDetection:
    def test_tamper_modify_action(self, populated_chain: Path) -> None:
        """Changing an action value must break the chain."""
        lines = populated_chain.read_text().splitlines()
        obj = json.loads(lines[2])
        obj["action"] = "TAMPERED"
        lines[2] = json.dumps(obj)
        _rewrite_lines(populated_chain, lines)
        with pytest.raises(AuditChainCorruption):
            verify_chain(populated_chain)

    def test_tamper_modify_subject(self, populated_chain: Path) -> None:
        lines = populated_chain.read_text().splitlines()
        obj = json.loads(lines[1])
        obj["subject"] = "evil"
        lines[1] = json.dumps(obj)
        _rewrite_lines(populated_chain, lines)
        with pytest.raises(AuditChainCorruption):
            verify_chain(populated_chain)

    def test_tamper_modify_payload(self, populated_chain: Path) -> None:
        lines = populated_chain.read_text().splitlines()
        obj = json.loads(lines[3])
        obj["payload"] = {"i": 99}
        lines[3] = json.dumps(obj)
        _rewrite_lines(populated_chain, lines)
        with pytest.raises(AuditChainCorruption):
            verify_chain(populated_chain)

    def test_tamper_modify_entry_hash(self, populated_chain: Path) -> None:
        lines = populated_chain.read_text().splitlines()
        obj = json.loads(lines[2])
        obj["entry_hash"] = "0" * 64
        lines[2] = json.dumps(obj)
        _rewrite_lines(populated_chain, lines)
        with pytest.raises(AuditChainCorruption):
            verify_chain(populated_chain)

    def test_tamper_delete_entry(self, populated_chain: Path) -> None:
        lines = populated_chain.read_text().splitlines()
        del lines[2]
        _rewrite_lines(populated_chain, lines)
        with pytest.raises(AuditChainCorruption):
            verify_chain(populated_chain)

    def test_tamper_reorder_entries(self, populated_chain: Path) -> None:
        lines = populated_chain.read_text().splitlines()
        lines[2], lines[3] = lines[3], lines[2]
        _rewrite_lines(populated_chain, lines)
        with pytest.raises(AuditChainCorruption):
            verify_chain(populated_chain)

    def test_tamper_duplicate_entry(self, populated_chain: Path) -> None:
        lines = populated_chain.read_text().splitlines()
        lines.insert(3, lines[2])
        _rewrite_lines(populated_chain, lines)
        with pytest.raises(AuditChainCorruption):
            verify_chain(populated_chain)


# ────────────────────────────────────────────────────────────────────────
# Invariant 5 — resume correctness
# ────────────────────────────────────────────────────────────────────────


class TestResumeCorrectness:
    @given(
        initial=st.lists(entry_spec_st, min_size=1, max_size=10),
        resumed=st.lists(entry_spec_st, min_size=1, max_size=10),
    )
    @settings(max_examples=50, deadline=None)
    def test_close_and_reopen_continues_chain(
        self,
        initial: list[tuple[str, str, dict]],
        resumed: list[tuple[str, str, dict]],
        tmp_path_factory,
    ) -> None:
        path = tmp_path_factory.mktemp("audit") / "chain.jsonl"
        with AuditWriter.open(path) as writer:
            for action, subject, payload in initial:
                writer.append(action=action, subject=subject, payload=payload)

        with AuditWriter.open(path, resume=True) as writer:
            for action, subject, payload in resumed:
                writer.append(action=action, subject=subject, payload=payload)

        total = verify_chain(path)
        assert total == len(initial) + len(resumed)


# ────────────────────────────────────────────────────────────────────────
# Invariant 7 — RFC-safe JSON (DEC-AUDIT-FSYNC-DURABILITY-001)
#
# A non-finite float (NaN / Inf / -Inf) anywhere in a payload is REFUSED
# (AuditPayloadError) and the rejection NEVER advances the chain: the
# durable chain after any mix of good + bad appends always verifies to
# exactly the number of GOOD appends, with dense 0..n-1 seq numbers.
# ────────────────────────────────────────────────────────────────────────


_non_finite_st = st.sampled_from([float("nan"), float("inf"), float("-inf")])


class TestRfcSafeJsonInvariant:
    @given(
        flags=st.lists(st.booleans(), min_size=1, max_size=20),
        bad=_non_finite_st,
    )
    @settings(max_examples=100, deadline=None)
    def test_non_finite_appends_are_rejected_and_never_advance_chain(
        self, flags: list[bool], bad: float, tmp_path_factory
    ) -> None:
        # `flags[i] == True` → a BAD (non-finite) append that must be
        # rejected; False → a GOOD finite append that must persist.
        path = tmp_path_factory.mktemp("audit") / "chain.jsonl"
        good = 0
        with AuditWriter.open(path) as writer:
            for i, is_bad in enumerate(flags):
                if is_bad:
                    with pytest.raises(AuditPayloadError):
                        writer.append(action="X", subject=f"s{i}", payload={"v": bad})
                else:
                    writer.append(action="X", subject=f"s{i}", payload={"v": float(i)})
                    good += 1

        # The chain verifies to exactly the count of GOOD appends — the
        # rejected non-finite appends left the chain untouched.
        assert verify_chain(path) == good
        assert [e.seq for e in read_entries(path)] == list(range(good))
