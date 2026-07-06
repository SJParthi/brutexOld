"""Manifest-vs-walk discovery scaling bench (DEC-LAKE-MANIFEST-O1-001).

The guarantee we prove: a **manifest-served** dataset discovery has latency
independent of the lake's **partition/file count** — it is O(manifest-rows),
NOT O(files). The operator's measured incident: the 2.36M-file post-prebuild
lake paid a 29.7-minute O(files) walk on the first post-restart ``/movers``
request; the same row set serves from the manifest in one bounded JSONL read.

Two assertions on a synthetic lake (dirs + zero-byte ``*.parquet`` touch files
— the walk only probes existence, so the shape is exact while staying
CI-cheap):

1. **File-count independence** — the SAME dataset count with 30× the month
   files serves from the manifest within ``ratio_ceiling`` of the 1-file-per-
   dataset shape (the walk, by contrast, grows with the file count).
2. **Manifest beats the walk** — on the deep shape the manifest read is at
   least ``_MIN_SPEEDUP×`` faster than the legacy walk (a deliberately
   conservative floor on shared CI; the real-lake gap is minutes → seconds).

Honest envelope (Golden Rule #10): the manifest read is O(manifest-rows) ≈
O(datasets) — NOT a literal O(1)-total — and the FIRST discovery (or the first
after a vendor pull) still pays one real walk to (re)build it.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import time
from pathlib import Path

from brutex.dashboard import db_view
from brutex.data import lake_manifest as lm
from brutex.data.window_ledger_store import ledger_path

from tests.bench._o1_tiers import ratio_ceiling

#: Datasets per shape (constant across shapes — the quantity the manifest scales with).
_DATASETS = 150

#: Month files per dataset: shallow vs deep (the quantity the WALK scales with).
_SHALLOW_MONTHS = 1
_DEEP_MONTHS = 30

#: Conservative floor for "manifest beats the walk" on the deep shape.
_MIN_SPEEDUP = 3.0

_REPEATS = 5


def _build_lake(lake: Path, *, months: int) -> None:
    """A synthetic FNO-heavy lake: ``_DATASETS`` datasets × *months* files each."""
    ledger = ledger_path(lake)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_bytes(b"ledger-v1")
    for i in range(_DATASETS):
        tf_dir = lake / "bars" / "NSE" / "FNO" / f"NSE-SYM{i:04d}-FUT" / "1minute"
        for m in range(months):
            year, month = 2020 + m // 12, 1 + m % 12
            month_dir = tf_dir / f"{year:04d}"
            month_dir.mkdir(parents=True, exist_ok=True)
            (month_dir / f"{month:02d}.parquet").write_bytes(b"")


def _best_of(fn: object, repeats: int = _REPEATS) -> float:
    """Best-of-N wall time (the standard shared-CI noise floor treatment)."""
    best = float("inf")
    for _ in range(repeats):
        start = time.perf_counter()
        fn()  # type: ignore[operator]
        best = min(best, time.perf_counter() - start)
    return best


def test_manifest_serve_is_file_count_independent(tmp_path: Path) -> None:
    shallow = tmp_path / "shallow"
    deep = tmp_path / "deep"
    _build_lake(shallow, months=_SHALLOW_MONTHS)
    _build_lake(deep, months=_DEEP_MONTHS)
    # Build both manifests (each pays its one legacy walk here — not timed).
    assert db_view.list_candle_datasets(shallow)
    assert db_view.list_candle_datasets(deep)
    assert lm.manifest_meta_path(shallow).exists()
    assert lm.manifest_meta_path(deep).exists()

    t_shallow = _best_of(lambda: db_view.list_candle_datasets(shallow))
    t_deep = _best_of(lambda: db_view.list_candle_datasets(deep))
    ratio = t_deep / max(t_shallow, 1e-9)
    # Same dataset count, 30× the files: the manifest-served read must stay flat.
    assert ratio <= ratio_ceiling(5.0), (
        f"manifest-served discovery scaled with FILE count: deep/shallow ratio "
        f"{ratio:.2f} (shallow {t_shallow * 1e3:.2f} ms, deep {t_deep * 1e3:.2f} ms)"
    )


def test_manifest_beats_the_walk_on_a_deep_lake(tmp_path: Path) -> None:
    deep = tmp_path / "deep"
    _build_lake(deep, months=_DEEP_MONTHS)
    assert db_view.list_candle_datasets(deep)  # builds the manifest (one walk)

    t_walk = _best_of(
        lambda: db_view._walk_candle_datasets(deep, exclude_segments=frozenset(), intervals=None)
    )
    t_manifest = _best_of(lambda: db_view.list_candle_datasets(deep))
    speedup = t_walk / max(t_manifest, 1e-9)
    assert speedup >= _MIN_SPEEDUP, (
        f"manifest ({t_manifest * 1e3:.2f} ms) not ≥{_MIN_SPEEDUP}× faster than the "
        f"walk ({t_walk * 1e3:.2f} ms) on {_DATASETS} datasets × {_DEEP_MONTHS} months"
    )
    # Byte-identity while we are here: the accelerator changes NOTHING.
    assert db_view.list_candle_datasets(deep) == db_view._walk_candle_datasets(
        deep, exclude_segments=frozenset(), intervals=None
    )
