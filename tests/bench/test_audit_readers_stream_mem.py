"""Bounded-memory bench for the streamed audit list-readers (DEC-AUDIT-READERS-STREAM-001).

The three whole-chain materialisations —
:func:`brutex.engine.sweep_verify.recorded_context_from_audit`,
:func:`brutex.engine.sweep_verify.recorded_stage3_from_audit`, and
:func:`brutex.orchestrator.replay.replay_audit_to_diff` — used to run
``list(read_entries(path))`` over the append-only, NEVER-rotated audit chain:
O(chain) peak memory per call. They now ride the streaming
:func:`brutex.orchestrator.audit.iter_entries` (one parsed entry resident at a
time), so the tracemalloc PEAK must be FLAT in chain length for the
sweep_verify folds, and O(distinct matching subjects) — never O(entries) —
for the replay projection (the frozenset IS its output contract).

MEASURED 2026-07-09 (methodology adapted from the breaker-B audit's
``round4_critic/firm_audit_reader.py``; 200,000 realistic entries, 77.1 MB
file, 385 B/line on disk, writer-equivalent chain):

* pre-fix ``list(read_entries(path))``: peak **223.8 MiB = 1,173 B/entry**
  (the handoff measured 1,165 B/entry logical on 30k REAL fsync'd entries —
  reproduced);
* post-fix ``recorded_context_from_audit``: peak **0.039 MiB** (~0.20
  B/entry — flat, one entry + fold state);
* post-fix ``replay_audit_to_diff`` (100 distinct subjects): peak
  **0.051 MiB** vs the pre-fix 169.8 MiB.

HONEST envelope (Golden Rule #10): this is a tracemalloc (Python-allocator)
peak bound, NOT an RSS claim; streaming changes MEMORY only — the chain FILE
still grows without bound (one line per material action, Golden Rule #8;
size-triggered rotation is the named follow-up F3-FU1, out of scope here).

The gate's TEETH are executable (the ``test_stage3_select_bounded_memory``
precedent): ``test_gate_has_teeth_materialised_reader_exceeds_ceiling`` runs
the pre-fix ``list(read_entries(...))`` shape through the SAME harness and
asserts it BLOWS both the ratio and the absolute ceiling — so the bound can
never silently go vacuous, and the fails-before proof is permanent.
"""

from __future__ import annotations

import gc
import tracemalloc
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Final

from brutex.engine.sweep_verify import (
    recorded_context_from_audit,
    recorded_stage3_from_audit,
)
from brutex.orchestrator.audit import GENESIS_PREV_HASH, AuditEntry, read_entries
from brutex.orchestrator.replay import replay_audit_to_diff

#: Chain sizes — LARGE = 10× SMALL so an O(chain) materialisation shows a
#: ~10× peak ratio while the streamed fold stays ~1×.
_SMALL: Final[int] = 5_000
_LARGE: Final[int] = 50_000

#: Flat-peak ratio ceiling for the streamed paths: a 10× chain growth may not
#: even double the traced peak (measured ratio ≈ 1.0 — one entry resident at
#: a time; the replay frozenset holds the SAME 100 subjects either way). The
#: pre-fix list(read_entries) shape measures ≈ 10× here — pinned executably
#: by the teeth test below. Generous headroom vs allocator/CI noise.
_PEAK_RATIO_CEILING: Final[float] = 2.0

#: Absolute per-call ceiling for the streamed paths at the LARGE base. The
#: measured post-fix peaks are ≤ ~0.06 MiB; the pre-fix materialisation at
#: 50k entries measures ~56 MiB (1,173 B/entry) — 4 MiB is ~14× headroom on
#: the fixed side while staying ~14× below the defect it guards against.
_PEAK_ABS_CEILING_BYTES: Final[int] = 4 * 2**20

#: Distinct replay subjects — the projection's inherent output size (≪ N).
_DISTINCT_SUBJECTS: Final[int] = 100


def _write_chain(path: Path, n: int, *, mode: str) -> None:
    """Write an *n*-entry hash-valid chain, writer-equivalent bytes (no fsync
    — the ``tests.fakes.audit_chain`` idiom) with realistic payload shapes.

    ``mode="verify"``: one ``SWEEP_START`` + one ``STAGE_3_PASS_START`` head,
    then ``SWEEP_TUPLE_DONE`` filler (the sweep_verify readers scan ALL of
    it). ``mode="replay"``: every entry ``action="add"`` cycling
    ``_DISTINCT_SUBJECTS`` distinct subjects.
    """
    prev = GENESIS_PREV_HASH
    with open(path, "w", encoding="utf-8") as f:
        for i in range(n):
            if mode == "verify":
                if i == 0:
                    action = "SWEEP_START"
                    payload: dict[str, object] = {
                        "data_snapshot_hash": "x" * 32,
                        "indicator_version": "v1",
                        "code_commit_sha": "c" * 40,
                    }
                elif i == 1:
                    action = "STAGE_3_PASS_START"
                    payload = {
                        "stage3_top_k": 3,
                        "max_fevals": 50,
                        "max_restarts": 1,
                        "train_snapshot_hash": "ab" * 32,
                    }
                else:
                    action = "SWEEP_TUPLE_DONE"
                    payload = {
                        "run_id": f"{i:032x}",
                        "combo": f"combo-{i % 977}",
                        "tuple_index": i,
                        "masks": 5460000 + i,
                        "status": "ok",
                    }
                subject = f"tuple-{i}"
            else:
                action = "add"
                payload = {"run_id": f"{i:032x}", "masks": 5460000 + i}
                subject = f"subj-{i % _DISTINCT_SUBJECTS}"
            bare = AuditEntry(
                seq=i,
                prev_hash=prev,
                ts_utc=float(i),
                actor="bench",
                action=action,
                subject=subject,
                payload=payload,
            )
            entry = replace(bare, entry_hash=bare.compute_hash())
            f.write(entry.to_json_line() + "\n")
            prev = entry.entry_hash


def _traced_peak(fn: Callable[[], object]) -> int:
    """tracemalloc peak (bytes) of one *fn()* call."""
    gc.collect()
    tracemalloc.start()
    fn()
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return peak


class TestStreamedReadersPeakIsFlat:
    def test_recorded_context_peak_flat_vs_chain_length(self, tmp_path: Path) -> None:
        """A 10× longer chain may not double the fold's traced peak."""
        small, large = tmp_path / "s.jsonl", tmp_path / "l.jsonl"
        _write_chain(small, _SMALL, mode="verify")
        _write_chain(large, _LARGE, mode="verify")
        peak_small = _traced_peak(lambda: recorded_context_from_audit(small))
        peak_large = _traced_peak(lambda: recorded_context_from_audit(large))
        ratio = peak_large / max(peak_small, 1)
        assert ratio <= _PEAK_RATIO_CEILING, (
            f"recorded_context_from_audit peak grew {ratio:.2f}× for a 10× chain "
            f"({peak_small}→{peak_large} B) — the streaming O(1) bound regressed"
        )
        assert peak_large <= _PEAK_ABS_CEILING_BYTES, (
            f"recorded_context_from_audit peak {peak_large} B at {_LARGE} entries "
            f"exceeds the {_PEAK_ABS_CEILING_BYTES} B ceiling (chain materialised?)"
        )

    def test_recorded_stage3_peak_flat_vs_chain_length(self, tmp_path: Path) -> None:
        """The Stage-3 knob fold rides the same streaming bound."""
        small, large = tmp_path / "s.jsonl", tmp_path / "l.jsonl"
        _write_chain(small, _SMALL, mode="verify")
        _write_chain(large, _LARGE, mode="verify")
        peak_small = _traced_peak(lambda: recorded_stage3_from_audit(small))
        peak_large = _traced_peak(lambda: recorded_stage3_from_audit(large))
        ratio = peak_large / max(peak_small, 1)
        assert ratio <= _PEAK_RATIO_CEILING, (
            f"recorded_stage3_from_audit peak grew {ratio:.2f}× for a 10× chain "
            f"({peak_small}→{peak_large} B) — the streaming O(1) bound regressed"
        )
        assert peak_large <= _PEAK_ABS_CEILING_BYTES

    def test_replay_projection_peak_bounded_by_subjects(self, tmp_path: Path) -> None:
        """Replay peak is O(distinct subjects) — flat when the subject set is
        fixed, however long the chain (the frozenset IS the output)."""
        small, large = tmp_path / "s.jsonl", tmp_path / "l.jsonl"
        _write_chain(small, _SMALL, mode="replay")
        _write_chain(large, _LARGE, mode="replay")
        live = frozenset({f"subj-{i}" for i in range(_DISTINCT_SUBJECTS)})
        peak_small = _traced_peak(
            lambda: replay_audit_to_diff(audit_path=small, live_subjects=live)
        )
        peak_large = _traced_peak(
            lambda: replay_audit_to_diff(audit_path=large, live_subjects=live)
        )
        ratio = peak_large / max(peak_small, 1)
        assert ratio <= _PEAK_RATIO_CEILING, (
            f"replay_audit_to_diff peak grew {ratio:.2f}× for a 10× chain "
            f"({peak_small}→{peak_large} B) — the streaming bound regressed"
        )
        assert peak_large <= _PEAK_ABS_CEILING_BYTES
        # Semantic sanity: the streamed diff is still the real projection.
        diff = replay_audit_to_diff(audit_path=large, live_subjects=live)
        assert diff.is_consistent
        assert diff.replayed_count == _DISTINCT_SUBJECTS

    def test_gate_has_teeth_materialised_reader_exceeds_ceiling(self, tmp_path: Path) -> None:
        """FAILS-BEFORE, executable: the pre-fix ``list(read_entries(...))``
        shape through the SAME harness blows BOTH pins — the ratio (measured
        ≈ 10× for a 10× chain) and the absolute ceiling (measured ~1,173
        B/entry ⇒ ~56 MiB at the LARGE base vs the 4 MiB ceiling). If the
        streamed paths ever regress to materialisation, the tests above fail
        exactly as this shape does here."""
        small, large = tmp_path / "s.jsonl", tmp_path / "l.jsonl"
        _write_chain(small, _SMALL, mode="verify")
        _write_chain(large, _LARGE, mode="verify")
        peak_small = _traced_peak(lambda: list(read_entries(small)))
        peak_large = _traced_peak(lambda: list(read_entries(large)))
        assert peak_large / max(peak_small, 1) > _PEAK_RATIO_CEILING
        assert peak_large > _PEAK_ABS_CEILING_BYTES
