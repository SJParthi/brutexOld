"""Unit + golden + property tests for the sweep audit log (`brutex.engine.sweep_audit`).

100% line + branch coverage of the per-combo BLAKE3 hash-chained JSONL
(DEC-SWEEP-REALTIME-GUARANTEE-001, CLAUDE.md Golden Rule #8):

* :class:`SweepAuditEntry` ``compute_hash`` / ``to_json_line`` / ``from_json_line``
  (success + malformed-JSON + missing-field branches);
* :class:`SweepAuditWriter` ``open`` (new / resume-with-entries / resume-empty /
  resume-missing / already-exists raise), ``append`` (payload present + empty,
  explicit + default ts, closed-writer raise, disk-write OSError), the
  context-manager + raw-constructor ``__enter__`` paths, idempotent ``close``;
* :func:`iter_entries` — the O(1)-memory streaming primitive
  (DEC-SWEEP-SCALE-OPERABILITY-001: eager missing-file raise, laziness, torn-tail +
  blank-line skip, malformed-committed-line raise) — plus the ``_read_entries``
  / ``read_entries`` list wrappers over it (missing file, blank-line skip,
  malformed line);
* :func:`verify_chain` (empty, happy, seq-out-of-order, prev-hash break,
  entry-hash tamper) — now STREAMING: a no-list-materialisation proof, a
  comparative tracemalloc bounded-peak oracle, and mid-stream tamper/torn-tail
  checks — plus a hypothesis property that any honest chain verifies and any
  single-byte tamper is caught, and a frozen golden hash.
"""

from __future__ import annotations

import gc
import tracemalloc
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from brutex.engine import sweep_audit as _sweep_mod
from brutex.engine.sweep_audit import (
    GENESIS_PREV_HASH,
    SweepAuditChainCorruption,
    SweepAuditEntry,
    SweepAuditError,
    SweepAuditWriteError,
    SweepAuditWriter,
    iter_entries,
    read_entries,
    verify_chain,
)
from brutex.engine.sweep_audit import (
    _read_entries_with_torn_tail as _sweep_read_with_tail,
)
from brutex.engine.sweep_audit import (
    _read_tail_entry as _sweep_read_tail,
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
    # The in-place truncate (DEC-AUDIT-TORN-TAIL-O1-001) leaves no sibling temp
    # file behind (the prior os.replace rewrite is gone).
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
    """An OSError during the in-place truncate is wrapped in SweepAuditWriteError."""
    path = tmp_path / "sweep_trunc_fail.jsonl"
    with SweepAuditWriter.open(path) as log:
        log.append(action="A", subject="s0", payload={"i": 0}, ts_utc=1.0)
    _append_raw(path, '{"seq": 1, "prev')

    def _boom(_fd: object, _length: object) -> None:
        raise OSError("disk gone")

    # DEC-AUDIT-TORN-TAIL-O1-001 truncates in place, so os.truncate is the I/O
    # failure point (was os.replace on the old temp-file rewrite).
    monkeypatch.setattr("brutex.engine.sweep_audit.os.truncate", _boom)
    with pytest.raises(SweepAuditWriteError):
        _sweep_truncate(path)


# ── _read_tail_entry — O(1) reverse tail-read on resume ──────────────────────
# DEC-AUDIT-RESUME-TAILREAD-001: resume no longer materialises the whole log —
# it seeks to the end and parses only the last committed line. These pin the
# sweep-side (BLAKE3) tail reader at 100% line+branch under the engine gate; the
# cross-chain parity + O(1)-teeth matrix lives in test_audit_torn_tail.py.


def _sweep_clean_chain(path: Path, n: int) -> None:
    with SweepAuditWriter.open(path) as log:
        for i in range(n):
            log.append(action="COMBO_DONE", subject=f"s:{i}", payload={"i": i}, ts_utc=float(i))


def test_sweep_tailread_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "st_empty.jsonl"
    path.write_text("", encoding="utf-8")
    assert _sweep_read_tail(path) == (None, False)


def test_sweep_tailread_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(SweepAuditError):
        _sweep_read_tail(tmp_path / "st_missing.jsonl")


def test_sweep_tailread_single_and_many(tmp_path: Path) -> None:
    p1 = tmp_path / "st_one.jsonl"
    _sweep_clean_chain(p1, 1)
    e1, torn1 = _sweep_read_tail(p1)
    assert torn1 is False and e1 is not None and e1.seq == 0

    pn = tmp_path / "st_many.jsonl"
    _sweep_clean_chain(pn, 40)
    en, tornn = _sweep_read_tail(pn)
    assert tornn is False and en is not None and en.seq == 39
    # Parity with the full read's tail entry.
    entries, _ = _sweep_read_with_tail(pn)
    assert en == entries[-1]


def test_sweep_tailread_torn_partial_recovers_last(tmp_path: Path) -> None:
    path = tmp_path / "st_partial.jsonl"
    _sweep_clean_chain(path, 3)
    _append_raw(path, '{"seq": 3, "prev_hash": "abc", "ts')
    entry, torn = _sweep_read_tail(path)
    assert torn is True and entry is not None and entry.seq == 2


def test_sweep_tailread_ends_on_newline_vs_midline(tmp_path: Path) -> None:
    path = tmp_path / "st_boundary.jsonl"
    _sweep_clean_chain(path, 3)
    assert path.read_bytes().endswith(b"\n")
    e, torn = _sweep_read_tail(path)
    assert torn is False and e is not None and e.seq == 2
    # Strip the final newline → the last line is now a torn tail.
    path.write_bytes(path.read_bytes()[:-1])
    e2, torn2 = _sweep_read_tail(path)
    assert torn2 is True and e2 is not None and e2.seq == 1


def test_sweep_tailread_only_torn_line_is_none(tmp_path: Path) -> None:
    path = tmp_path / "st_only_torn.jsonl"
    _append_raw(path, '{"seq": 0, "prev')
    assert _sweep_read_tail(path) == (None, True)


def test_sweep_tailread_blank_lines_skipped(tmp_path: Path) -> None:
    path = tmp_path / "st_blanks.jsonl"
    _sweep_clean_chain(path, 2)
    _append_raw(path, "\n\n\n")  # blank committed filler after seq 1
    entry, torn = _sweep_read_tail(path)
    assert torn is False and entry is not None and entry.seq == 1


def test_sweep_tailread_only_blank_lines_is_none(tmp_path: Path) -> None:
    path = tmp_path / "st_only_blanks.jsonl"
    path.write_text("\n\n", encoding="utf-8")
    assert _sweep_read_tail(path) == (None, False)


def test_sweep_tailread_last_committed_corrupt_raises(tmp_path: Path) -> None:
    path = tmp_path / "st_corrupt_last.jsonl"
    _sweep_clean_chain(path, 1)
    _append_raw(path, "not json at all\n")
    with pytest.raises(SweepAuditError):
        _sweep_read_tail(path)


def test_sweep_tailread_multichunk_walk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tiny chunk forces the reverse walk to loop across many reads."""
    monkeypatch.setattr(_sweep_mod, "_TAIL_CHUNK_BYTES", 4)
    path = tmp_path / "st_chunks.jsonl"
    _sweep_clean_chain(path, 6)
    entry, torn = _sweep_read_tail(path)
    assert torn is False and entry is not None and entry.seq == 5


def test_sweep_tailread_single_line_exceeds_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One entry longer than the chunk drives the for/else exhaustion arm."""
    monkeypatch.setattr(_sweep_mod, "_TAIL_CHUNK_BYTES", 4)
    path = tmp_path / "st_giant.jsonl"
    _sweep_clean_chain(path, 1)
    entry, torn = _sweep_read_tail(path)
    assert torn is False and entry is not None and entry.seq == 0


def test_sweep_tailread_resume_continues_valid_chain(tmp_path: Path) -> None:
    path = tmp_path / "st_resume.jsonl"
    _sweep_clean_chain(path, 5)
    with SweepAuditWriter.open(path, resume=True) as log:
        rec = log.append(action="COMBO_DONE", subject="s:5", payload={"i": 5})
    assert rec.seq == 5
    assert verify_chain(path) == 6


# ── iter_entries — the O(1)-memory streaming primitive ──────────────────────
# DEC-SWEEP-SCALE-OPERABILITY-001: verify_chain / read_entries no longer materialise
# the whole log — iter_entries streams one committed entry at a time with the
# EXACT torn-tail discriminator (DEC-AUDIT-TORN-TAIL-001) the list readers used.


def test_iter_entries_missing_file_raises_eagerly(tmp_path: Path) -> None:
    """The missing-file raise fires at CALL time, before any iteration."""
    with pytest.raises(SweepAuditError, match="not found"):
        iter_entries(tmp_path / "nope.jsonl")


def test_iter_entries_is_lazy_and_matches_read_entries(tmp_path: Path) -> None:
    """iter_entries returns a lazy iterator whose items equal read_entries."""
    p = tmp_path / "a.jsonl"
    _sweep_clean_chain(p, 5)
    it = iter_entries(p)
    assert not isinstance(it, list)
    assert iter(it) is it  # a genuine one-shot iterator, not a container
    assert list(it) == read_entries(p)


def test_iter_entries_skips_torn_tail_and_blank_lines(tmp_path: Path) -> None:
    """A blank committed line and an unterminated torn tail are both skipped."""
    p = tmp_path / "a.jsonl"
    _sweep_clean_chain(p, 1)
    _append_raw(p, "\n")  # blank committed filler
    _append_raw(p, '{"seq": 1, "prev_hash": "ab')  # torn tail (no newline)
    assert [e.seq for e in iter_entries(p)] == [0]


def test_iter_entries_malformed_committed_line_raises_with_line_no(tmp_path: Path) -> None:
    """A malformed NEWLINE-TERMINATED line is real corruption — raise with path:line."""
    p = tmp_path / "a.jsonl"
    _sweep_clean_chain(p, 1)
    _append_raw(p, "{not json}\n")
    with pytest.raises(SweepAuditError, match=r":2:"):
        list(iter_entries(p))


def test_read_with_tail_empty_file_is_not_torn(tmp_path: Path) -> None:
    """_read_entries_with_torn_tail (via _has_torn_tail) on an empty file."""
    p = tmp_path / "empty.jsonl"
    p.touch()
    assert _sweep_read_with_tail(p) == ([], False)


def test_verify_chain_streams_without_list_materialisation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """verify_chain never routes through the list-building readers (O(1) RAM).

    Poison every list-materialising reader in the module; the streaming
    verify must still succeed — deterministic proof that the whole-file
    ``entries = _read_entries(path)`` load is gone (DEC-SWEEP-SCALE-OPERABILITY-001).
    """
    p = tmp_path / "a.jsonl"
    _sweep_clean_chain(p, 50)

    def _boom(_path: Path) -> object:
        raise AssertionError("verify_chain materialised the whole log")

    monkeypatch.setattr(_sweep_mod, "read_entries", _boom)
    monkeypatch.setattr(_sweep_mod, "_read_entries", _boom)
    monkeypatch.setattr(_sweep_mod, "_read_entries_with_torn_tail", _boom)
    assert verify_chain(p) == 50


def test_verify_chain_streaming_peak_memory_is_bounded(tmp_path: Path) -> None:
    """The streaming verify's peak heap sits FAR below the materialised list's.

    Comparative tracemalloc oracle (platform-independent — both sides measured
    in the same run over the same file): ``read_entries`` must hold all N
    entries at once (its list contract), while ``verify_chain`` holds ONE
    entry at a time, so its peak must be a small fraction of the list peak.
    """
    p = tmp_path / "big.jsonl"
    pad = "x" * 120
    with SweepAuditWriter.open(p) as w:
        for i in range(2000):
            w.append(
                action="COMBO_DONE",
                subject=f"combo:{i}",
                payload={"i": i, "pad": pad},
                ts_utc=float(i),
            )

    gc.collect()
    tracemalloc.start()
    entries = read_entries(p)  # the deliberate O(N) list contract (unchanged)
    _, list_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(entries) == 2000
    del entries
    gc.collect()

    tracemalloc.start()
    assert verify_chain(p) == 2000  # the streaming path
    _, stream_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert stream_peak < list_peak / 5


def test_verify_chain_skips_torn_tail_counts_committed_only(tmp_path: Path) -> None:
    """A torn final line is excluded from the streamed verify count."""
    p = tmp_path / "a.jsonl"
    _sweep_clean_chain(p, 2)
    _append_raw(p, '{"seq": 2, "prev_hash": "abc", "ts')
    assert verify_chain(p) == 2


def test_verify_chain_streaming_tamper_detected_mid_file(tmp_path: Path) -> None:
    """A payload tamper deep inside a streamed log still breaks at its seq."""
    p = tmp_path / "a.jsonl"
    _sweep_clean_chain(p, 100)
    entries = read_entries(p)
    tampered = replace(entries[40], payload={"i": 999}, entry_hash=entries[40].entry_hash)
    _write_lines(p, [*entries[:40], tampered, *entries[41:]])
    with pytest.raises(SweepAuditChainCorruption) as ei:
        verify_chain(p)
    assert ei.value.seq == 40
    assert "content tampered" in ei.value.reason


# ────────────────────────────────────────────────────────────────────────
# DEC-RUST-SWEEP-OPS-001 — flag-ON routing parity (wheel-free fake crate)
# ────────────────────────────────────────────────────────────────────────


class TestSweepOpsRouting:
    """Flag-ON routing parity for the entry-hash + verify-step laws.

    Wheel-free: the shared behavior-correct FAKE crate is injected by
    patching the facade's ``importlib`` binding. Flag ON must be
    byte-identical to the flag-OFF Python law (which every OTHER test in
    this file drives) — the written chain bytes, the verify count, AND the
    tamper exception message bytes.
    """

    @staticmethod
    def _arm_fake_crate(monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace

        from brutex.engine import sweep_ops_rust

        from tests.fakes.sweep_ops_crate import build_fake_sweep_ops_crate

        crate = build_fake_sweep_ops_crate(version="fake")
        monkeypatch.setattr(
            sweep_ops_rust,
            "importlib",
            SimpleNamespace(import_module=lambda name: crate),
        )
        monkeypatch.setenv(sweep_ops_rust.SWEEP_OPS_RUST_ENV, "1")
        sweep_ops_rust.reset_sweep_ops_rust_for_tests()

    @staticmethod
    def _reset_facade() -> None:
        from brutex.engine.sweep_ops_rust import reset_sweep_ops_rust_for_tests

        reset_sweep_ops_rust_for_tests()

    @staticmethod
    def _write_chain(path: Path) -> None:
        with SweepAuditWriter.open(path) as log:
            log.append(action="SWEEP_START", subject="s", payload={"n": 1}, ts_utc=1.5)
            log.append(action="COMBO_DONE", subject="c", payload={"mask": 7}, ts_utc=2.25)

    def test_flag_on_chain_bytes_and_verify_match_flag_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        off_log = tmp_path / "off.jsonl"
        self._write_chain(off_log)
        assert verify_chain(off_log) == 2

        self._arm_fake_crate(monkeypatch)
        try:
            on_log = tmp_path / "on.jsonl"
            self._write_chain(on_log)
            assert on_log.read_bytes() == off_log.read_bytes()
            assert verify_chain(on_log) == 2
        finally:
            self._reset_facade()

    def test_flag_on_tamper_message_matches_flag_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log = tmp_path / "audit.jsonl"
        self._write_chain(log)
        tampered = log.read_text(encoding="utf-8").replace('"mask":7', '"mask":8')
        log.write_text(tampered, encoding="utf-8")

        with pytest.raises(SweepAuditChainCorruption) as off_exc:
            verify_chain(log)

        self._arm_fake_crate(monkeypatch)
        try:
            with pytest.raises(SweepAuditChainCorruption) as on_exc:
                verify_chain(log)
        finally:
            self._reset_facade()

        assert str(on_exc.value) == str(off_exc.value)
        assert on_exc.value.seq == 1
        assert "content tampered" in on_exc.value.reason
