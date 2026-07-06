"""O(1) benchmark for the orchestrator audit-log append (`brutex.orchestrator.audit`).

Enforces the O(1) decree (CLAUDE.md §2 r5 / §12, Golden Rule #8) for
:meth:`AuditWriter.append` — compute the SHA-256 entry hash from the in-memory
``prev_hash``/``seq``, serialise one JSONL line, ``write`` + ``flush`` + ONE
best-effort ``os.fsync`` (DEC-AUDIT-FSYNC-DURABILITY-001) — which must be
latency-independent of how many lines the chain already holds. The writer keeps
the running ``prev_hash``/``seq`` in memory and never re-reads/re-hashes the
chain on append; the fsync is a per-append CONSTANT, so it cannot mask an O(n)
regression, only add jitter (absorbed by the per-base sample mean).

Mirrors ``tests/bench/test_engine_sweep_audit_o1.py`` (the BLAKE3 sweep twin)
with ONE honest prefill delta: this writer fsyncs EVERY append, so a 100k-line
prefill through the real ``append`` would be ~100k fsyncs (minutes of pure I/O
on a shared runner). The bases are therefore prefilled via
:func:`tests.fakes.audit_chain.write_clean_chain` (writer-equivalent bytes, no
fsync) and reopened with ``AuditWriter.open(path, resume=True)`` — an O(1)
tail read (DEC-AUDIT-RESUME-TAILREAD-001) — so ONLY the timed region runs the
real production append, fsync included (nothing patched out). Prefill
equivalence is itself PINNED by ``test_prefill_is_writer_equivalent`` (a
``verify_chain`` + seq/prev_hash-continuity check), so an ``AuditEntry``
serialization drift can never make the bench silently time appends onto a
broken chain. Ratio routed through the single-source-of-truth
:func:`tests.bench._o1_tiers.ratio_ceiling` (5× on shared CI; strict tier on
the M4-Pro runner).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Final

from brutex.orchestrator.audit import AuditWriter, verify_chain

from tests.bench._o1_tiers import ratio_ceiling
from tests.fakes.audit_chain import write_clean_chain

#: Prior-line bases — the chain already holds this many lines before we time
#: the next append. LARGE ≫ SMALL so an O(n) re-hash would be ~100× slower.
_SMALL: Final[int] = 1_000
_LARGE: Final[int] = 100_000

#: Fixed payload — constant canonical-JSON cost; we time the chain append.
_PAYLOAD: Final[dict[str, int]] = {"mask": 7, "trades": 42}


def _resumed(path: Path, n: int) -> AuditWriter:
    """Prefill *n* writer-equivalent lines (no fsync), resume the real writer."""
    write_clean_chain(path, n)
    return AuditWriter.open(path, resume=True)


class TestAppendIsO1:
    def test_prefill_is_writer_equivalent(self, tmp_path: Path) -> None:
        """The hand-built prefill is genuinely chain-valid (defensive pin).

        A serialization drift in ``AuditEntry``/``write_clean_chain`` must fail
        HERE, never silently let the ratio test time appends onto a broken
        chain: real appends resumed onto a 25-line prefill continue the seq +
        prev_hash chain and the WHOLE file passes ``verify_chain``.
        """
        path = tmp_path / "equiv.jsonl"
        prefilled = write_clean_chain(path, 25)
        first_real = None
        with AuditWriter.open(path, resume=True) as writer:
            for i in range(5):
                entry = writer.append(
                    action="INGEST_CHUNK_DONE", subject=f"chunk:{i}", payload=_PAYLOAD
                )
                if first_real is None:
                    first_real = entry
        assert verify_chain(path) == 30
        assert first_real is not None
        assert first_real.seq == 25
        assert first_real.prev_hash == prefilled[-1].entry_hash

    def test_append_throughput_smoke_small(self, benchmark, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        """Throughput smoke at a 1k-line base.

        NOTE: each ``benchmark`` round appends to the SAME growing file, so this
        measures end-to-end (fsync-bound) append THROUGHPUT, not a fixed-base
        O(1) latency. The O(1) latency claim is proved exclusively by
        :meth:`test_append_ratio_is_o1`.
        """
        with _resumed(tmp_path / "small.jsonl", _SMALL) as writer:
            entry = benchmark(
                lambda: writer.append(
                    action="INGEST_CHUNK_DONE", subject="chunk:bench", payload=_PAYLOAD
                )
            )
            # Last round's entry must be a well-formed chain link: a 64-char hex
            # SHA-256 hash, and the seq must have advanced past the base.
            # (`AuditWriter` has no public `.seq` — assert on the returned entry.)
            assert len(entry.entry_hash) == 64
            assert all(c in "0123456789abcdef" for c in entry.entry_hash)
            assert entry.seq > _SMALL

    def test_append_throughput_smoke_large(self, benchmark, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        """Throughput smoke at a 100k-line base.

        NOTE: each ``benchmark`` round appends to the SAME growing file, so this
        measures end-to-end (fsync-bound) append THROUGHPUT, not a fixed-base
        O(1) latency. The O(1) latency claim is proved exclusively by
        :meth:`test_append_ratio_is_o1`.
        """
        with _resumed(tmp_path / "large.jsonl", _LARGE) as writer:
            entry = benchmark(
                lambda: writer.append(
                    action="INGEST_CHUNK_DONE", subject="chunk:bench", payload=_PAYLOAD
                )
            )
            # Last round's entry must be a well-formed chain link: a 64-char hex
            # SHA-256 hash, and the seq must have advanced past the base.
            # (`AuditWriter` has no public `.seq` — assert on the returned entry.)
            assert len(entry.entry_hash) == 64
            assert all(c in "0123456789abcdef" for c in entry.entry_hash)
            assert entry.seq > _LARGE

    def test_append_ratio_is_o1(self, tmp_path: Path) -> None:
        """Appending one line costs the same at a 100k-line base as at a 1k base.

        An O(1) append keeps the running ``prev_hash`` + ``seq`` in memory and
        hashes only the new entry's content. An O(n) regression (re-reading or
        re-hashing the whole on-disk chain per append) would scale with the
        prior-line count and blow the ratio.
        """

        def _time(name: str, n: int, *, iterations: int = 400) -> float:
            with _resumed(tmp_path / name, n) as writer:
                for _ in range(25):  # warm
                    writer.append(action="WARM", subject="chunk:warm", payload=_PAYLOAD)
                t0 = time.perf_counter_ns()
                for _ in range(iterations):
                    writer.append(action="INGEST_CHUNK_DONE", subject="chunk:t", payload=_PAYLOAD)
                t1 = time.perf_counter_ns()
            return (t1 - t0) / iterations

        t_small = _time("ratio_small.jsonl", _SMALL)
        t_large = _time("ratio_large.jsonl", _LARGE)
        ratio = t_large / max(t_small, 1.0)
        assert ratio < ratio_ceiling(5.0), (
            f"AuditWriter.append ratio {ratio:.2f}× (100k vs 1k prior lines) exceeds "
            f"the ceiling — the append may have regressed from O(1) to an O(n) "
            f"re-hash/re-read of the chain. t_1k={t_small:.0f}ns, t_100k={t_large:.0f}ns."
        )
