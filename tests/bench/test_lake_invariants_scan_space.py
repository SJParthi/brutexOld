"""Space bench — the lake scan + content token never hold O(files) memory.

DEC-LAKE-INVARIANTS-PREINDEX-001. **Honest, narrow claims (Golden Rule #10):**

1. ``scan_lake_invariants`` streams the ``bars/`` tree ONE SERIES AT A TIME —
   resident is O(largest single series + findings + symbol dirs), NEVER an
   O(files) pre-index. Pinned as a gc-settled tracemalloc-PEAK ratio: 10× the
   files must cost < 3× the peak (measured 2026-07-11 on the shared dev
   container: post-fix 192 files → 150 KiB, 1,920 files → 172 KiB, ratio
   1.15×; the pre-streaming scanner FAILED this exact pin at 8.66× — the
   revert teeth, verified by stashing the fix).
2. ``lake_content_token`` feeds an INCREMENTAL hasher over a sorted walk —
   transient peak is O(largest directory listing), never the old O(files)
   lines list + sorted copy + join (measured 2026-07-11: post-fix 33.5 KiB →
   48.9 KiB for 10× files, ratio 1.46×; pre-fix FAILED at 8.75× — verified
   by stashing the fix).

Neither claim is O(1) WALL: both passes remain O(files) in TIME (stat / read
per file — stated in both functions' docstrings; this bench pins SPACE only).

Fixture files are 0-byte markers (synthetic; enough for the pre-index + token
paths, which only walk/stat/parse — every per-file read skips loudly and the
scanner's logger is stubbed to keep the bench I/O-quiet).
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import gc
import os
import tracemalloc
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

pytestmark = pytest.mark.bench


@pytest.fixture(autouse=True)
def _python_oracle_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin this suite to the EXPLICIT flag-OFF (Python oracle) arm.

    Conscious W-5 reconciliation: these read-count / residency spies pin the
    PYTHON scanner's implementation shape (pl.read_parquet call counts,
    per-series residency), which the engaged crate arm deliberately does not
    share — its read-count law is the scan differential's
    ``last_rust_scan_reads()`` engagement census.
    """
    monkeypatch.setenv("BRUTEX_LAKE_WAVED_RUST", "0")


_SMALL_SYMBOLS = 2
_BIG_SYMBOLS = 20
_INTERVALS = ("1minute", "1day")
_MONTHS = 12
_YEARS = (2020, 2021, 2022, 2023)  # 2 × 2 × 12 × 4 = 192 / 1,920 files
_PEAK_RATIO_CEILING = 3.0  # 10× files must cost < 3× peak (memory, low-noise)


class _NullLog:
    """A no-op structlog stand-in (0-byte fixtures skip-warn per file)."""

    def warning(self, *args: Any, **kwargs: Any) -> None:
        """Swallow the per-file scan_skip storm."""

    def info(self, *args: Any, **kwargs: Any) -> None:
        """Swallow the scan_complete line."""


def _build_empty_lake(root: Path, n_symbols: int) -> int:
    """0-byte marker files in the real layout; returns the file count."""
    n = 0
    for i in range(n_symbols):
        for interval in _INTERVALS:
            for year in _YEARS:
                d = root / "bars" / "NSE" / "CASH" / f"SYM{i:04d}" / interval / str(year)
                d.mkdir(parents=True, exist_ok=True)
                for month in range(1, _MONTHS + 1):
                    fd = os.open(d / f"{month:02d}.parquet", os.O_CREAT | os.O_WRONLY, 0o600)
                    os.close(fd)
                    n += 1
    return n


def _traced_peak(fn: Any) -> int:
    """The tracemalloc peak of one *fn()* call (gc-settled)."""
    gc.collect()
    tracemalloc.start()
    fn()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return peak


def test_scan_resident_is_per_series_not_per_lake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """10× the lake files must NOT cost 10× the scan's peak memory.

    The pause seam gc-settles every 50 files: each 0-byte fixture's skip
    raises an exception whose traceback forms a CYCLE (frame ↔ exception),
    and that unreachable garbage otherwise accumulates on the automatic gc's
    threshold timing (~400 B/file measured) — the pin targets REACHABLE
    retention (the pre-index class), not collector scheduling. The teeth are
    unaffected: the old scanner's pre-index was reachable, so it failed this
    pin at 8.66× (measured 2026-07-11) with the same gc-settled pause.
    """
    from brutex.data import lake_invariants as li

    monkeypatch.setattr(li, "_LOG", _NullLog())
    small = tmp_path / "small"
    big = tmp_path / "big"
    n_small = _build_empty_lake(small, _SMALL_SYMBOLS)
    n_big = _build_empty_lake(big, _BIG_SYMBOLS)
    assert n_big == 10 * n_small
    calls = [0]

    def _gc_settle_pause() -> None:
        calls[0] += 1
        if calls[0] % 50 == 0:
            gc.collect()

    # Warm the read path once so one-time allocations never land in a peak.
    li.scan_lake_invariants(small)
    peak_small = _traced_peak(
        lambda: li.scan_lake_invariants(small, per_file_pause=_gc_settle_pause)
    )
    peak_big = _traced_peak(lambda: li.scan_lake_invariants(big, per_file_pause=_gc_settle_pause))
    ratio = peak_big / peak_small
    assert ratio < _PEAK_RATIO_CEILING, (
        f"scan peak grew {ratio:.2f}x for 10x files "
        f"({peak_small} -> {peak_big} B) — an O(files) pre-index is back"
    )


def test_token_transient_is_per_directory_not_per_lake(tmp_path: Path) -> None:
    """10× the lake files must NOT cost 10× the token walk's peak memory."""
    from brutex.data.lake_heal import lake_content_token

    small = tmp_path / "small"
    big = tmp_path / "big"
    n_small = _build_empty_lake(small, _SMALL_SYMBOLS)
    n_big = _build_empty_lake(big, _BIG_SYMBOLS)
    assert n_big == 10 * n_small
    lake_content_token(small)  # warm
    peak_small = _traced_peak(lambda: lake_content_token(small))
    peak_big = _traced_peak(lambda: lake_content_token(big))
    ratio = peak_big / peak_small
    assert ratio < _PEAK_RATIO_CEILING, (
        f"token peak grew {ratio:.2f}x for 10x files "
        f"({peak_small} -> {peak_big} B) — the O(files) lines list is back"
    )
