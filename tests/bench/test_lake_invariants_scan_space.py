"""Space bench — the lake scan + content token's PYTHON-visible residency.

DEC-LAKE-INVARIANTS-PREINDEX-001 → DEC-RUST-LAKE-WAVED-001 W-6a. **Honest,
narrow claims (Golden Rule #10):** the Python scan/token cores are DELETED —
the crate owns the walk, and its Rust-side residency is NOT observable from
``tracemalloc`` (a crate-internal concern, covered by the crate's own
design: the scan streams one series at a time, the token hasher is
incremental). What THIS bench can honestly pin is the PYTHON-visible
residency of the ONE remaining Python layer — the wire reconstruction:

1. ``scan_lake_invariants`` over a CLEAN lake reconstructs O(findings +
   events) Python objects — for a clean lake that is O(1), so 10× the
   files must NOT cost 10× the Python peak. (The pre-endgame Python
   pre-index class would land HERE if it ever came back as a Python-side
   wire explosion.)
2. ``lake_content_token`` returns one 64-hex token + an empty skip list on
   a healthy tree — Python peak flat in lake size.

Neither claim is O(1) WALL: both passes remain O(files) in TIME (stat/read
per file, crate-side — stated in both functions' docstrings; this bench
pins the PYTHON-side SPACE only). An ALL-SKIP hostile lake's skip-event
wire is O(skipped) BY DESIGN (each skip is a loud counted event — that
linearity is the honesty law, not a regression, and is deliberately not
ratio-pinned here).
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import gc
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("polars")
pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

pytestmark = pytest.mark.bench


@pytest.fixture(autouse=True)
def _endgame_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ONE Rust path (W-6a): retired flag unset."""
    monkeypatch.delenv("BRUTEX_LAKE_WAVED_RUST", raising=False)


_SMALL_SYMBOLS = 2
_BIG_SYMBOLS = 20
_MONTHS = 12
_YEARS = (2022, 2023)  # 2 × 12 × 2 = 48 / 480 files
_PEAK_RATIO_CEILING = 3.0  # 10× files must cost < 3× peak (memory, low-noise)


def _build_clean_lake(root: Path, n_symbols: int) -> int:
    """Tiny REAL one-row parquet partitions in the real layout; file count."""
    n = 0
    for i in range(n_symbols):
        for year in _YEARS:
            d = root / "bars" / "NSE" / "CASH" / f"SYM{i:04d}" / "1minute" / str(year)
            d.mkdir(parents=True, exist_ok=True)
            for month in range(1, _MONTHS + 1):
                table = pa.table(
                    {
                        "timestamp": pa.array(
                            [datetime(year, month, 2, 4, 0, tzinfo=UTC)],
                            type=pa.timestamp("us", tz="UTC"),
                        ),
                        "open": [100.0],
                        "high": [100.0],
                        "low": [100.0],
                        "close": [100.0],
                        "volume": pa.array([10], type=pa.int64()),
                        "open_interest": pa.array([None], type=pa.int64()),
                    }
                )
                pq.write_table(table, d / f"{month:02d}.parquet")
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


def test_scan_python_visible_peak_flat_on_a_clean_lake(tmp_path: Path) -> None:
    """10× the lake files must NOT cost 10× the scan's PYTHON peak memory.

    The crate walks/reads in Rust; Python holds only the wire
    reconstruction — O(findings + events), O(1) on a clean lake.
    """
    from brutex.data import lake_invariants as li

    small = tmp_path / "small"
    big = tmp_path / "big"
    n_small = _build_clean_lake(small, _SMALL_SYMBOLS)
    n_big = _build_clean_lake(big, _BIG_SYMBOLS)
    assert n_big == 10 * n_small
    # Warm the read path once so one-time allocations never land in a peak.
    report = li.scan_lake_invariants(small)
    assert report.findings == () and report.files_scanned == n_small
    peak_small = _traced_peak(lambda: li.scan_lake_invariants(small))
    peak_big = _traced_peak(lambda: li.scan_lake_invariants(big))
    ratio = peak_big / peak_small
    assert ratio < _PEAK_RATIO_CEILING, (
        f"scan PYTHON peak grew {ratio:.2f}x for 10x files "
        f"({peak_small} -> {peak_big} B) — an O(files) Python-side wire/"
        f"pre-index is back"
    )


def test_token_python_visible_peak_flat_on_a_clean_lake(tmp_path: Path) -> None:
    """10× the lake files must NOT cost 10× the token walk's PYTHON peak."""
    from brutex.data.lake_heal import lake_content_token

    small = tmp_path / "small"
    big = tmp_path / "big"
    n_small = _build_clean_lake(small, _SMALL_SYMBOLS)
    n_big = _build_clean_lake(big, _BIG_SYMBOLS)
    assert n_big == 10 * n_small
    lake_content_token(small)  # warm
    peak_small = _traced_peak(lambda: lake_content_token(small))
    peak_big = _traced_peak(lambda: lake_content_token(big))
    ratio = peak_big / peak_small
    assert ratio < _PEAK_RATIO_CEILING, (
        f"token PYTHON peak grew {ratio:.2f}x for 10x files "
        f"({peak_small} -> {peak_big} B) — an O(files) Python-side lines "
        f"list is back"
    )
