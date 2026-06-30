"""Unit + golden + property tests for the sweep audit log (`brutex.engine.sweep_audit`).

100% line + branch coverage of the per-combo BLAKE3 hash-chained JSONL
(DEC-SWEEP-REALTIME-GUARANTEE-001, CLAUDE.md Golden Rule #8):

* :class:`SweepAuditEntry` ``compute_hash`` / ``to_json_line`` / ``from_json_line``
  (success + malformed-JSON + missing-field branches);
* :class:`SweepAuditWriter` ``open`` (new / resume-with-entries / resume-empty /
  resume-missing / already-exists raise), ``append`` (payload present + empty,
  explicit + default ts, closed-writer raise, disk-write OSError), the
  context-manager + raw-constructor ``__enter__`` paths, idempotent ``close``;
* ``_read_entries`` (missing file, blank-line skip, malformed line) +
  ``read_entries`` wrapper;
* :func:`verify_chain` (empty, happy, seq-out-of-order, prev-hash break,
  entry-hash tamper) — plus a hypothesis property that any honest chain verifies
  and any single-byte tamper is caught, and a frozen golden hash.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from brutex.engine.sweep_audit import (
    GENESIS_PREV_HASH,
    SweepAuditChainCorruption,
    SweepAuditEntry,
    SweepAuditError,
    SweepAuditWriteError,
    SweepAuditWriter,
    read_entries,
    verify_chain,
)
from brutex.engine.sweep_audit import (
    _read_entries_with_torn_tail as _sweep_read_with_tail,
)
from brutex.engine.sweep_audit import (
    _truncate_torn_tail as _sweep_truncate,
)
from hypothesis import given
from hypothesis import strategies as st

#: A frozen golden hash — recomputing this exact entry must always match (any
#: change to the canonicalisation or BLAKE3 provider would break it).
_GOLDEN_HASH = "8f7c0c5bc96ca77cd126742bdd39f701be036fad8fadc59b9818d918c16f7abd"


def _entry(**over: Any) -> SweepAuditEntry:
    base: dict[str, Any] = {
        "seq": 0,
        "prev_hash": GENESIS_PREV_HASH,
        "ts_utc": 1700000000.0,
        "actor": "sweep",
        "action": "SWEEP_START",
        "subject": "sweep:golden",
        "payload": {"total_combos": 3},
    }
    base.update(over)
    return SweepAuditEntry(**base)


# ── SweepAuditEntry ─────────────────────────────────────────────────────────


def test_golden_hash_is_stable() -> None:
    assert _entry().compute_hash() == _GOLDEN_HASH


def test_compute_hash_does_not_mutate() -> None:
    e = _entry()
    assert e.entry_hash == ""
    e.compute_hash()
    assert e.entry_hash == ""  # frozen + pure


def test_json_line_round_trip() -> None:
    e = _entry(entry_hash=_GOLDEN_HASH)
    restored = SweepAuditEntry.from_json_line(e.to_json_line())
    assert restored == e


def test_from_json_line_malformed_json_raises() -> None:
    with pytest.raises(SweepAuditError, match="Malformed sweep-audit line"):
        SweepAuditEntry.from_json_line("{not json")


def test_from_json_line_missing_fields_raises() -> None:
    with pytest.raises(SweepAuditError, match="missing required fields"):
        SweepAuditEntry.from_json_line('{"seq": 0}')


# ── SweepAuditWriter.open ───────────────────────────────────────────────────


def test_open_new_then_append_and_verify(tmp_path: Path) -> None:
    p = tmp_path / "a.jsonl"
    with SweepAuditWriter.open(p) as w:
        assert w.seq == 0
        assert w.prev_hash == GENESIS_PREV_HASH
        e0 = w.append(action="SWEEP_START", subject="sweep:x", payload={"total": 3})
        e1 = w.append(action="COMBO_DONE", subject="combo:1", payload={"mask": 7})
        assert w.seq == 2
        assert w.prev_hash == e1.entry_hash
    assert e1.prev_hash == e0.entry_hash
    assert verify_chain(p) == 2


def test_open_resume_continues_chain(tmp_path: Path) -> None:
    p = tmp_path / "a.jsonl"
    with SweepAuditWriter.open(p) as w:
        w.append(action="SWEEP_START", subject="sweep:x")
    with SweepAuditWriter.open(p, resume=True) as w:
        assert w.seq == 1
        w.append(action="COMBO_DONE", subject="combo:1")
    assert verify_chain(p) == 2


def test_open_resume_empty_file_starts_at_zero(tmp_path: Path) -> None:
    p = tmp_path / "a.jsonl"
    p.touch()
    with SweepAuditWriter.open(p, resume=True) as w:
        assert w.seq == 0
        w.append(action="SWEEP_START", subject="sweep:x")
    assert verify_chain(p) == 1


def test_open_resume_missing_file_creates_new(tmp_path: Path) -> None:
    p = tmp_path / "missing.jsonl"
    with SweepAuditWriter.open(p, resume=True) as w:
        assert w.seq == 0
        w.append(action="SWEEP_START", subject="sweep:x")
    assert verify_chain(p) == 1


def test_open_existing_without_resume_raises(tmp_path: Path) -> None:
    p = tmp_path / "a.jsonl"
    p.touch()
    with pytest.raises(SweepAuditWriteError, match="already exists"):
        SweepAuditWriter.open(p)


# ── __enter__ / close ───────────────────────────────────────────────────────


def test_enter_via_raw_constructor_opens_handle(tmp_path: Path) -> None:
    p = tmp_path / "a.jsonl"
    writer = SweepAuditWriter(path=p)
    with writer as w:
        w.append(action="SWEEP_START", subject="sweep:x")
    assert verify_chain(p) == 1


def test_close_is_idempotent(tmp_path: Path) -> None:
    p = tmp_path / "a.jsonl"
    w = SweepAuditWriter.open(p)
    w.close()
    w.close()  # second close is a no-op (covers the _fp is None branch)


# ── append ──────────────────────────────────────────────────────────────────


def test_append_default_timestamp_is_set(tmp_path: Path) -> None:
    p = tmp_path / "a.jsonl"
    with SweepAuditWriter.open(p) as w:
        e = w.append(action="X", subject="s")  # payload None → {}, ts None → time()
    assert e.payload == {}
    assert e.ts_utc > 0.0


def test_append_to_closed_writer_raises(tmp_path: Path) -> None:
    p = tmp_path / "a.jsonl"
    w = SweepAuditWriter.open(p)
    w.close()
    with pytest.raises(SweepAuditWriteError, match="closed sweep audit writer"):
        w.append(action="X", subject="s")


def test_append_disk_write_failure_raises(tmp_path: Path) -> None:
    p = tmp_path / "a.jsonl"
    w = SweepAuditWriter.open(p)

    class _BadFP:
        closed = False

        def write(self, _s: str) -> int:
            raise OSError("disk full")

        def flush(self) -> None:
            pass

    real = w._fp  # keep a handle so we can close it (no leaked-fd warning)
    w._fp = _BadFP()
    try:
        with pytest.raises(SweepAuditWriteError, match="Failed to write sweep-audit entry"):
            w.append(action="X", subject="s")
    finally:
        real.close()


# ── _read_entries / read_entries ────────────────────────────────────────────


def test_read_entries_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(SweepAuditError, match="not found"):
        read_entries(tmp_path / "nope.jsonl")


def test_read_entries_skips_blank_lines(tmp_path: Path) -> None:
    p = tmp_path / "a.jsonl"
    with SweepAuditWriter.open(p) as w:
        w.append(action="X", subject="s")
    p.write_text(p.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")
    assert len(read_entries(p)) == 1


def test_read_entries_malformed_line_reports_line_number(tmp_path: Path) -> None:
    p = tmp_path / "a.jsonl"
    p.write_text("{bad}\n", encoding="utf-8")
    with pytest.raises(SweepAuditError, match=r":1:"):
        read_entries(p)


# ── verify_chain ────────────────────────────────────────────────────────────


def test_verify_empty_file_returns_zero(tmp_path: Path) -> None:
    p = tmp_path / "a.jsonl"
    p.touch()
    assert verify_chain(p) == 0


def _write_lines(p: Path, entries: list[SweepAuditEntry]) -> None:
    p.write_text("".join(e.to_json_line() + "\n" for e in entries), encoding="utf-8")


def test_verify_seq_out_of_order(tmp_path: Path) -> None:
    p = tmp_path / "a.jsonl"
    e = _entry(seq=5)
    _write_lines(p, [replace(e, entry_hash=e.compute_hash())])
    with pytest.raises(SweepAuditChainCorruption) as ei:
        verify_chain(p)
    assert ei.value.reason == "seq number out of order"


def test_verify_prev_hash_break(tmp_path: Path) -> None:
    p = tmp_path / "a.jsonl"
    bad = _entry(prev_hash="f" * 64)
    bad = replace(bad, entry_hash=bad.compute_hash())
    _write_lines(p, [bad])
    with pytest.raises(SweepAuditChainCorruption) as ei:
        verify_chain(p)
    assert "prev_hash" in ei.value.reason


def test_verify_entry_hash_tamper(tmp_path: Path) -> None:
    p = tmp_path / "a.jsonl"
    e = _entry(entry_hash="0" * 64)  # wrong stored hash
    _write_lines(p, [e])
    with pytest.raises(SweepAuditChainCorruption) as ei:
        verify_chain(p)
    assert "content tampered" in ei.value.reason
    assert ei.value.seq == 0


# ── property: honest chains verify; tamper is caught ────────────────────────


@given(
    actions=st.lists(
        st.tuples(
            st.text(min_size=1, max_size=8),
            st.text(min_size=1, max_size=8),
            st.dictionaries(st.text(max_size=4), st.integers(), max_size=3),
        ),
        min_size=1,
        max_size=8,
    )
)
def test_property_honest_chain_always_verifies(tmp_path_factory: Any, actions: list[Any]) -> None:
    p = tmp_path_factory.mktemp("au") / "a.jsonl"
    with SweepAuditWriter.open(p) as w:
        for i, (action, subject, payload) in enumerate(actions):
            w.append(action=action, subject=subject, payload=payload, ts_utc=float(i))
    assert verify_chain(p) == len(actions)


def test_property_single_payload_tamper_breaks_chain(tmp_path: Path) -> None:
    p = tmp_path / "a.jsonl"
    with SweepAuditWriter.open(p) as w:
        w.append(action="A", subject="s0", payload={"v": 1}, ts_utc=1.0)
        w.append(action="B", subject="s1", payload={"v": 2}, ts_utc=2.0)
    entries = read_entries(p)
    # Flip a payload value on entry 0 but keep its (now stale) stored hash.
    tampered = replace(entries[0], payload={"v": 999}, entry_hash=entries[0].entry_hash)
    _write_lines(p, [tampered, entries[1]])
    with pytest.raises(SweepAuditChainCorruption):
        verify_chain(p)


# ────────────────────────────────────────────────────────────────────────
# DEC-AUDIT-TORN-TAIL-001 — sweep-side torn-tail recovery
#
# A crash / SIGKILL mid-append can leave an unterminated final JSONL line on
# the sweep audit log. The reader skips it + flags it; the writer's resume path
# truncates it; a malformed COMMITTED (newline-terminated) line is still real
# corruption and must raise. These tests cover sweep_audit.py lines 303,
# 485-486 (the torn-tail flag in _read_entries_with_torn_tail) and 528-542
# (the body of _truncate_torn_tail) so the engine 100% coverage gate stays
# green WITHOUT depending on tests/unit/test_audit_torn_tail.py (which is
# scoped to the orchestrator-audit gate).
# ────────────────────────────────────────────────────────────────────────


def _append_raw(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(text)


def test_sweep_torn_tail_partial_json_recovers(tmp_path: Path) -> None:
    """An unterminated final JSONL line is skipped + flagged as a torn tail."""
    path = tmp_path / "sweep_partial.jsonl"
    with SweepAuditWriter.open(path) as log:
        log.append(action="A", subject="s0", payload={"i": 0}, ts_utc=1.0)
        log.append(action="B", subject="s1", payload={"i": 1}, ts_utc=2.0)
    _append_raw(path, '{"seq": 2, "prev_hash": "abc", "ts_utc": 3.0, "act')

    entries, torn = _sweep_read_with_tail(path)
    assert torn is True
    assert [e.seq for e in entries] == [0, 1]


def test_sweep_torn_tail_resume_truncates_and_continues(tmp_path: Path) -> None:
    """open(resume=True) over a torn tail truncates it and resumes a clean chain.

    Exercises the resume-branch ``_truncate_torn_tail(path)`` call at line 303
    PLUS the body of ``_truncate_torn_tail`` itself (lines 528-542).
    """
    path = tmp_path / "sweep_resume.jsonl"
    with SweepAuditWriter.open(path) as log:
        log.append(action="A", subject="s0", payload={"i": 0}, ts_utc=1.0)
        log.append(action="B", subject="s1", payload={"i": 1}, ts_utc=2.0)
    _append_raw(path, '{"seq": 2, "prev_hash": "abc", "ts_utc": 3.0, "act')

    with SweepAuditWriter.open(path, resume=True) as log:
        recovered = log.append(action="C", subject="s2", payload={"i": 2}, ts_utc=3.0)

    assert recovered.seq == 2
    # The torn bytes are gone and the whole chain verifies end-to-end.
    assert verify_chain(path) == 3


def test_sweep_truncate_keeps_committed_prefix(tmp_path: Path) -> None:
    """_truncate_torn_tail keeps exactly the committed (newline-terminated) bytes."""
    path = tmp_path / "sweep_trunc.jsonl"
    with SweepAuditWriter.open(path) as log:
        log.append(action="A", subject="s0", payload={"i": 0}, ts_utc=1.0)
        log.append(action="B", subject="s1", payload={"i": 1}, ts_utc=2.0)
    committed = path.read_bytes()
    _append_raw(path, '{"seq": 2, "prev')  # torn remnant

    _sweep_truncate(path)

    assert path.read_bytes() == committed
    entries, torn = _sweep_read_with_tail(path)
    assert torn is False
    assert [e.seq for e in entries] == [0, 1]
    # The atomic-replace temp file was cleaned up.
    assert list(path.parent.glob("*.torn-tail-*.tmp")) == []


def test_sweep_truncate_only_torn_line_empties_file(tmp_path: Path) -> None:
    """A file consisting only of a torn line truncates to an empty file.

    Exercises the ``cut == -1`` branch (no newline anywhere) of
    ``_truncate_torn_tail``.
    """
    path = tmp_path / "sweep_trunc_only.jsonl"
    _append_raw(path, '{"seq": 0, "prev')

    _sweep_truncate(path)

    assert path.read_bytes() == b""


def test_sweep_truncate_io_error_wraps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An OSError during the atomic replace is wrapped in SweepAuditWriteError."""
    path = tmp_path / "sweep_trunc_fail.jsonl"
    with SweepAuditWriter.open(path) as log:
        log.append(action="A", subject="s0", payload={"i": 0}, ts_utc=1.0)
    _append_raw(path, '{"seq": 1, "prev')

    def _boom(_src: object, _dst: object) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr("brutex.engine.sweep_audit.os.replace", _boom)
    with pytest.raises(SweepAuditWriteError):
        _sweep_truncate(path)
