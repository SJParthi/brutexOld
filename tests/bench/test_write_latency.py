"""Micro-bench: the merge-write read/rebuild is faster AFTER the columnar cut.

DEC-WRITER-COLUMNAR-4A-001 — the merge READ (``_read_existing_bars``) rebuilt one
Python dict PER ROW via ``df.iter_rows(named=True)``; it now extracts each column
ONCE via ``Series.to_list`` and zips. This bench seeds a DEEP month partition, then
times a real merge-write (which re-reads + rewrites the whole partition) under the
BEFORE shape (legacy per-row reader rebound on the writer) vs the AFTER shape (the
production columnar reader), and asserts AFTER p50 AND p99 ≤ BEFORE (with a small
tolerance for timing jitter).

**Honest envelope (Golden Rule #10):** 4a is the SAFE, MODEST win — it removes the
per-row dict allocation from the read/rebuild only; the Decimal ``ROUND_HALF_UP``
reconstruction (the byte contract) is UNCHANGED, and the Parquet decode + the merge
+ the re-encode + the atomic write are all unchanged. So the expected improvement is
a fraction of the total write, NOT a 5×. The measured numbers are printed so the win
is reported honestly, not asserted as large. Whitebox: rebinds the private reader
(DEC-P0-017), so the file silences ``reportPrivateUsage``.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import statistics
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.atomic_io import lake_write_guard
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter, _tick_round

pytest.importorskip("polars")

_COORDS = {
    "exchange": "NSE",
    "segment": "INDEX",
    "symbol": "NIFTY",
    "interval": "1minute",
}

#: A deep partition — a multi-thousand-bar month is the steady-state re-pull shape.
_DEEP_ROWS = 8_000
#: Repeat the timed merge-write enough times for a stable p50/p99.
_TRIALS = 25


def _legacy_read_existing_bars(dest: Path, existing_bytes: bytes) -> dict[datetime, CandleBar]:
    """The pre-4a per-row reconstruction (the BEFORE shape), verbatim.

    Takes the already-read ``existing_bytes`` (the GAP #11 single-read signature).
    """
    import io

    import polars as pl

    with lake_write_guard():
        df = pl.read_parquet(io.BytesIO(existing_bytes))
        out: dict[datetime, CandleBar] = {}
        for row in df.iter_rows(named=True):
            oi = row["open_interest"]
            bar = _tick_round(
                CandleBar(
                    timestamp=row["timestamp"],
                    open=Decimal(str(row["open"])),
                    high=Decimal(str(row["high"])),
                    low=Decimal(str(row["low"])),
                    close=Decimal(str(row["close"])),
                    volume=int(row["volume"]),
                    open_interest=None if oi is None else int(oi),
                )
            )
            out[bar.timestamp] = bar
        return out


def _deep_bars(n: int) -> list[CandleBar]:
    """*n* distinct in-range bars spread across one (2021-01) month partition."""
    bars: list[CandleBar] = []
    for i in range(n):
        ts = datetime(2021, 1, 1, 0, 0, tzinfo=UTC).replace(
            day=1 + (i // (24 * 60)),
            hour=(i // 60) % 24,
            minute=i % 60,
        )
        base = Decimal(10_000) + Decimal(i) / Decimal(100)
        bars.append(
            CandleBar(
                timestamp=ts,
                open=base,
                high=base + Decimal("1.50"),
                low=base - Decimal("1.50"),
                close=base,
                volume=i,
                open_interest=(None if i % 3 == 0 else i * 7),
            )
        )
    return bars


def _time_merge_writes(root: Path, seed: list[CandleBar], *, legacy: bool) -> list[float]:
    """Seed a deep partition, then time *_TRIALS* idempotent merge re-pulls (seconds).

    A re-pull of the SAME bars exercises the read+rebuild (and the no-op rewrite
    decision) — the read/rebuild is exactly what 4a touches. Each trial uses a
    fresh lake so the seed write is amortised once per trial (the timed region is
    the merge write only).
    """
    durations: list[float] = []
    for trial in range(_TRIALS):
        lake = root / f"{'legacy' if legacy else 'new'}_{trial}"
        writer = ParquetWriter(lake_root=lake)
        if legacy:
            writer._read_existing_bars = staticmethod(  # type: ignore[method-assign]
                _legacy_read_existing_bars
            ).__func__
        writer.write(bars=seed, **_COORDS)  # untimed seed
        start = time.perf_counter()
        writer.write(bars=seed, **_COORDS)  # timed merge re-pull (re-read + decide)
        durations.append(time.perf_counter() - start)
    return durations


def test_columnar_merge_read_not_slower(tmp_path: Path) -> None:
    """AFTER (columnar) p50 and p99 ≤ BEFORE (per-row) on a deep partition."""
    seed = _deep_bars(_DEEP_ROWS)

    # Warm both paths once (import + decode caches) before timing.
    _time_merge_writes(tmp_path / "warm_old", seed[:100], legacy=True)
    _time_merge_writes(tmp_path / "warm_new", seed[:100], legacy=False)

    before = sorted(_time_merge_writes(tmp_path / "before", seed, legacy=True))
    after = sorted(_time_merge_writes(tmp_path / "after", seed, legacy=False))

    before_p50 = statistics.median(before)
    after_p50 = statistics.median(after)
    before_p99 = before[-1]  # max ≈ p99 for a small sample
    after_p99 = after[-1]

    print(
        f"\n[DEC-WRITER-COLUMNAR-4A-001 write-latency bench] deep partition "
        f"({_DEEP_ROWS} rows, {_TRIALS} trials):"
        f"\n  BEFORE (per-row)  p50={before_p50 * 1e3:.2f} ms  p99={before_p99 * 1e3:.2f} ms"
        f"\n  AFTER  (columnar) p50={after_p50 * 1e3:.2f} ms  p99={after_p99 * 1e3:.2f} ms"
        f"\n  speedup p50={before_p50 / after_p50:.3f}x  p99={before_p99 / after_p99:.3f}x"
    )

    # AFTER must not be slower. A small multiplicative tolerance absorbs timing
    # jitter on a shared CI runner — the claim is "no regression + a modest win",
    # NOT a fixed speedup factor (Golden Rule #10).
    tolerance = 1.15
    assert after_p50 <= before_p50 * tolerance, (
        f"columnar p50 {after_p50 * 1e3:.2f} ms regressed vs per-row {before_p50 * 1e3:.2f} ms"
    )
    assert after_p99 <= before_p99 * tolerance, (
        f"columnar p99 {after_p99 * 1e3:.2f} ms regressed vs per-row {before_p99 * 1e3:.2f} ms"
    )
