"""Streaming verify_chain tests for :mod:`brutex.orchestrator.audit`
(DEC-AUDIT-VERIFY-STREAM-001).

``verify_chain`` previously materialised the FULL entry list (O(N) RAM); it now
ports the :mod:`brutex.engine.sweep_audit` generator pattern — iterate one entry
at a time carrying only ``prev_hash`` + a running count. These tests pin:

* the new :func:`iter_entries` streaming primitive (missing file, order,
  torn-tail skip, blank-line skip, malformed committed line with ``path:line``);
* that ``verify_chain`` NEVER routes through the list-materialising readers
  (deterministic monkeypatch-poison proof);
* FLAT peak RAM — a comparative tracemalloc oracle across 10k vs 100k entries
  (the peak must NOT scale with N), plus the stream-vs-list peak ratio;
* IDENTICAL corruption discrimination — every tamper class raises the SAME
  exception type / seq / reason as the pre-streaming implementation, and a torn
  tail still verifies the committed prefix only.

The pre-existing byte-exact corruption pins in
``tests/unit/test_orchestrator_audit.py`` (``TestAuditChainCorruptionPins`` +
``TestVerifyChainTamper``) run against the SAME rewritten function and are the
primary regression net; the tamper tests here re-confirm the discrimination on
a DEEP mid-file entry, which only a streaming walk reaches incrementally.
"""

from __future__ import annotations

import gc
import json
import tracemalloc
from dataclasses import replace
from pathlib import Path

import pytest
from brutex.orchestrator import audit as audit_mod
from brutex.orchestrator.audit import (
    AuditChainCorruption,
    AuditEntry,
    AuditError,
    iter_entries,
    read_entries,
    verify_chain,
)

from tests.fakes.audit_chain import write_clean_chain as _clean_chain

# ────────────────────────────────────────────────────────────────────────
# Helpers — fast hand-built chains (no per-append fsync, so 100k is cheap)
# now live in the COMMON tests/fakes/audit_chain.py (shared with the append
# O(1) bench, DEC-O1-CLAIMS-HONESTY-001); the alias keeps call sites intact.
# ────────────────────────────────────────────────────────────────────────


def _write_lines(path: Path, entries: list[AuditEntry]) -> None:
    """Overwrite *path* with the given entries, one JSONL line each."""
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(entry.to_json_line() + "\n")


def _append_raw(path: Path, raw: str) -> None:
    """Append *raw* bytes verbatim (torn tails / garbage lines)."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(raw)


# ────────────────────────────────────────────────────────────────────────
# iter_entries — the streaming primitive
# ────────────────────────────────────────────────────────────────────────


class TestIterEntries:
    def test_missing_file_raises_at_call_time(self, tmp_path: Path) -> None:
        with pytest.raises(AuditError, match="not found"):
            iter_entries(tmp_path / "absent.jsonl")

    def test_streams_committed_entries_in_order(self, tmp_path: Path) -> None:
        p = tmp_path / "a.jsonl"
        _clean_chain(p, 5)
        it = iter_entries(p)
        assert not isinstance(it, list)  # a lazy iterator, never a list
        assert [e.seq for e in it] == [0, 1, 2, 3, 4]

    def test_skips_torn_tail_and_blank_filler(self, tmp_path: Path) -> None:
        p = tmp_path / "a.jsonl"
        _clean_chain(p, 1)
        _append_raw(p, "\n")  # blank committed filler
        _append_raw(p, '{"seq": 1, "prev_hash": "ab')  # torn tail (no newline)
        assert [e.seq for e in iter_entries(p)] == [0]

    def test_malformed_committed_line_raises_with_line_no(self, tmp_path: Path) -> None:
        p = tmp_path / "a.jsonl"
        _clean_chain(p, 1)
        _append_raw(p, "{not json}\n")
        with pytest.raises(AuditError, match=r":2:"):
            list(iter_entries(p))


# ────────────────────────────────────────────────────────────────────────
# verify_chain — streaming + flat RAM
# ────────────────────────────────────────────────────────────────────────


class TestVerifyChainStreaming:
    def test_never_routes_through_list_readers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Poison every list-materialising reader; the streaming verify still
        succeeds — deterministic proof the whole-file list load is gone."""
        p = tmp_path / "a.jsonl"
        _clean_chain(p, 50)

        def _boom(_path: Path) -> object:
            raise AssertionError("verify_chain materialised the whole log")

        monkeypatch.setattr(audit_mod, "read_entries", _boom)
        monkeypatch.setattr(audit_mod, "_read_entries", _boom)
        monkeypatch.setattr(audit_mod, "_read_entries_with_torn_tail", _boom)
        assert verify_chain(p) == 50

    def test_peak_memory_is_flat_10k_vs_100k(self, tmp_path: Path) -> None:
        """The streamed verify's peak heap must NOT scale with entry count.

        Comparative tracemalloc oracle (platform-independent — both sides are
        measured in the same run with the same allocator): verifying a 10×
        longer chain must hold the peak within a TIGHT bound of the small
        chain's peak (one raw line + one parsed entry at a time), never ~10×.
        """
        small = tmp_path / "small.jsonl"
        big = tmp_path / "big.jsonl"
        _clean_chain(small, 10_000)
        _clean_chain(big, 100_000)

        gc.collect()
        tracemalloc.start()
        assert verify_chain(small) == 10_000
        _, small_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        gc.collect()
        tracemalloc.start()
        assert verify_chain(big) == 100_000
        _, big_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # Flat: 10× the entries, peak within 1.5× + a tiny absolute slack
        # (allocator jitter), nowhere near the ~10× an O(N) list would show.
        assert big_peak <= small_peak * 1.5 + 64 * 1024

    def test_stream_peak_far_below_list_peak(self, tmp_path: Path) -> None:
        """verify_chain's peak sits FAR below read_entries' O(N) list peak."""
        p = tmp_path / "padded.jsonl"
        _clean_chain(p, 2_000, pad="x" * 120)

        gc.collect()
        tracemalloc.start()
        entries = read_entries(p)  # the deliberate O(N) list contract (unchanged)
        _, list_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert len(entries) == 2_000
        del entries
        gc.collect()

        tracemalloc.start()
        assert verify_chain(p) == 2_000  # the streaming path
        _, stream_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        assert stream_peak < list_peak / 5

    def test_counts_committed_only_with_torn_tail(self, tmp_path: Path) -> None:
        """A torn final line is excluded from the streamed verify count."""
        p = tmp_path / "a.jsonl"
        _clean_chain(p, 2)
        _append_raw(p, '{"seq": 2, "prev_hash": "abc", "ts')
        assert verify_chain(p) == 2


# ────────────────────────────────────────────────────────────────────────
# Corruption discrimination — byte-identical verdicts after the port
# ────────────────────────────────────────────────────────────────────────


class TestStreamingCorruptionDiscrimination:
    def test_mid_file_payload_tamper_breaks_at_its_seq(self, tmp_path: Path) -> None:
        p = tmp_path / "a.jsonl"
        entries = _clean_chain(p, 100)
        tampered = replace(entries[40], payload={"i": 999}, entry_hash=entries[40].entry_hash)
        _write_lines(p, [*entries[:40], tampered, *entries[41:]])
        with pytest.raises(AuditChainCorruption) as ei:
            verify_chain(p)
        assert ei.value.seq == 40
        assert ei.value.reason == "entry_hash does not match recomputed hash (content tampered)"

    def test_mid_file_prev_hash_break_names_linkage(self, tmp_path: Path) -> None:
        p = tmp_path / "a.jsonl"
        entries = _clean_chain(p, 10)
        broken = replace(entries[3], prev_hash="f" * 64, entry_hash=entries[3].entry_hash)
        _write_lines(p, [*entries[:3], broken, *entries[4:]])
        with pytest.raises(AuditChainCorruption) as ei:
            verify_chain(p)
        assert ei.value.seq == 3
        assert ei.value.reason == "prev_hash does not match previous entry's entry_hash"

    def test_mid_file_seq_gap_names_ordering(self, tmp_path: Path) -> None:
        p = tmp_path / "a.jsonl"
        entries = _clean_chain(p, 10)
        skewed = replace(entries[5], seq=99, entry_hash=entries[5].entry_hash)
        _write_lines(p, [*entries[:5], skewed, *entries[6:]])
        with pytest.raises(AuditChainCorruption) as ei:
            verify_chain(p)
        assert ei.value.seq == 99
        assert ei.value.reason == "seq number out of order"

    def test_non_finite_payload_tamper_maps_to_chain_corruption(self, tmp_path: Path) -> None:
        """A tampered line carrying a bare NaN token still maps to the SAME
        AuditChainCorruption verdict (the AuditPayloadError branch survived
        the streaming port)."""
        p = tmp_path / "a.jsonl"
        _clean_chain(p, 3)
        lines = p.read_text(encoding="utf-8").splitlines()
        assert '"payload":{"i":1}' in lines[1]
        lines[1] = lines[1].replace('"payload":{"i":1}', '"payload":{"i":NaN}')
        assert json.loads(lines[1])  # the tampered line still json.loads-parses
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with pytest.raises(AuditChainCorruption) as ei:
            verify_chain(p)
        assert ei.value.seq == 1
        assert ei.value.reason == "payload contains a non-finite float (tampered line)"

    def test_empty_and_torn_only_files(self, tmp_path: Path) -> None:
        """An empty file verifies to 0; a torn-only file also verifies to 0
        (nothing committed) — read_entries agrees on both."""
        empty = tmp_path / "empty.jsonl"
        empty.touch()
        assert verify_chain(empty) == 0
        assert read_entries(empty) == []

        torn_only = tmp_path / "torn.jsonl"
        torn_only.write_text('{"seq": 0, "prev', encoding="utf-8")
        assert verify_chain(torn_only) == 0
