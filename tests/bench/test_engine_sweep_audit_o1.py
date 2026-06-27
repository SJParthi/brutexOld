"""O(1) benchmark for the sweep audit-log append (`brutex.engine.sweep_audit`).

Enforces the O(1) decree (CLAUDE.md §2 r5 / §12, Golden Rule #8 — every material
action appends one hash-chained JSONL line) for the audit log's single hot path:

* :meth:`SweepAuditWriter.append` — the per-action append (compute the BLAKE3
  entry hash from the in-memory ``prev_hash``/``seq``, serialise one JSONL line,
  ``write`` + ``flush``) must be latency-independent of how many lines the chain
  already holds. The writer keeps the running ``prev_hash`` + ``seq`` in memory
  and never re-reads/re-hashes the chain on append, so the cost is a CONSTANT
  per call; a regression to an O(n) re-hash-the-whole-chain append (or an
  in-memory entries list scan) would blow the ratio and fail.

Mirrors ``tests/bench/test_engine_results_store_o1.py``: empirical per-call
latency at a LARGE prior-line base vs a SMALL base with CI-jitter headroom,
routed through the single-source-of-truth
:func:`tests.bench._o1_tiers.ratio_ceiling` (5× on shared CI, the strict 1.1×
tier on the M4-Pro runner). The payload is held constant across both bases so we
time the append, not the canonical-JSON of a growing payload.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Final

from brutex.engine.sweep_audit import SweepAuditWriter

from tests.bench._o1_tiers import ratio_ceiling

#: Prior-line bases — the chain already holds this many lines before we time
#: the next append. LARGE ≫ SMALL so an O(n) re-hash would be ~100× slower.
_SMALL: Final[int] = 1_000
_LARGE: Final[int] = 100_000

#: A fixed payload reused for every append so its canonical-JSON cost is a
#: constant — we are timing the chain append, not payload size growth.
_PAYLOAD: Final[dict[str, int]] = {"mask": 7, "trades": 42}


def _filled(path: Path, n: int) -> SweepAuditWriter:
    """Open a fresh audit log at *path* and append *n* prior lines."""
    writer = SweepAuditWriter.open(path)
    for _ in range(n):
        writer.append(action="COMBO_DONE", subject="combo:seed", payload=_PAYLOAD)
    return writer


class TestAppendIsO1:
    def test_append_throughput_smoke_small(self, benchmark, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        """Throughput smoke at a 1k-line base.

        NOTE: each ``benchmark`` round appends to the SAME growing file, so this
        measures end-to-end append THROUGHPUT, not a fixed-base O(1) latency. The
        O(1) latency claim is proved exclusively by :meth:`test_append_ratio_is_o1`.
        """
        with _filled(tmp_path / "small.jsonl", _SMALL) as writer:
            entry = benchmark(
                lambda: writer.append(action="COMBO_DONE", subject="combo:bench", payload=_PAYLOAD)
            )
            # Last round's entry must be a well-formed chain link: a 64-char hex
            # BLAKE3 hash, and the writer's seq must have advanced past the base.
            assert len(entry.entry_hash) == 64
            assert all(c in "0123456789abcdef" for c in entry.entry_hash)
            assert writer.seq > _SMALL

    def test_append_throughput_smoke_large(self, benchmark, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        """Throughput smoke at a 100k-line base.

        NOTE: each ``benchmark`` round appends to the SAME growing file, so this
        measures end-to-end append THROUGHPUT, not a fixed-base O(1) latency. The
        O(1) latency claim is proved exclusively by :meth:`test_append_ratio_is_o1`.
        """
        with _filled(tmp_path / "large.jsonl", _LARGE) as writer:
            entry = benchmark(
                lambda: writer.append(action="COMBO_DONE", subject="combo:bench", payload=_PAYLOAD)
            )
            # Last round's entry must be a well-formed chain link: a 64-char hex
            # BLAKE3 hash, and the writer's seq must have advanced past the base.
            assert len(entry.entry_hash) == 64
            assert all(c in "0123456789abcdef" for c in entry.entry_hash)
            assert writer.seq > _LARGE

    def test_append_ratio_is_o1(self, tmp_path: Path) -> None:
        """Appending one line costs the same at a 100k-line base as at a 1k base.

        An O(1) append keeps the running ``prev_hash`` + ``seq`` in memory and
        hashes only the new entry's content. An O(n) regression (re-reading or
        re-hashing the whole on-disk chain per append) would scale with the
        prior-line count and blow the ratio.
        """

        def _time(name: str, n: int, *, iterations: int = 2_000) -> float:
            with _filled(tmp_path / name, n) as writer:
                for _ in range(50):  # warm
                    writer.append(action="WARM", subject="combo:warm", payload=_PAYLOAD)
                t0 = time.perf_counter_ns()
                for _ in range(iterations):
                    writer.append(action="COMBO_DONE", subject="combo:t", payload=_PAYLOAD)
                t1 = time.perf_counter_ns()
            return (t1 - t0) / iterations

        t_small = _time("ratio_small.jsonl", _SMALL)
        t_large = _time("ratio_large.jsonl", _LARGE)
        ratio = t_large / max(t_small, 1.0)
        assert ratio < ratio_ceiling(5.0), (
            f"SweepAuditWriter.append ratio {ratio:.2f}× (100k vs 1k prior lines) "
            f"exceeds 5× — the append may have regressed from O(1) to an O(n) "
            f"re-hash of the whole chain. t_1k={t_small:.0f}ns, t_100k={t_large:.0f}ns."
        )
