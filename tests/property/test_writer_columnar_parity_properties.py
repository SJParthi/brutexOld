"""Property: random bar sets reconstruct byte-identically old-path vs new-path.

DEC-WRITER-COLUMNAR-4A-001 — the merge READ replaced ``df.iter_rows(named=True)``
(a per-row dict) with COLUMN-wise ``Series.to_list`` extraction, a byte-PRESERVING
speed cut. This hypothesis property fuzzes random seed + merge bar sets through
BOTH the production columnar reader AND the legacy per-row reader and asserts the
two writers leave a BYTE-IDENTICAL partition file + identical ``raw_hash`` (the
idempotence contract, CLAUDE.md Golden Rule #7).

Whitebox: rebinds the private ``_read_existing_bars`` on one writer instance to the
legacy implementation, so the file silences ``reportPrivateUsage`` (DEC-P0-017).
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.atomic_io import lake_write_guard
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter, _tick_round
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

pytest.importorskip("polars")

_COORDS = {
    "exchange": "NSE",
    "segment": "INDEX",
    "symbol": "NIFTY",
    "interval": "1minute",
}


def _legacy_read_existing_bars(dest: Path) -> dict[datetime, CandleBar]:
    """The pre-4a per-row reconstruction, verbatim (the parity oracle)."""
    import polars as pl

    with lake_write_guard():
        df = pl.read_parquet(dest)
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


# A bar = (minute, base-price tenths, span tenths, volume, oi-or-None).
_bar_strategy = st.tuples(
    st.integers(min_value=0, max_value=780),  # minute → hour minute // 60 (≤ 13)
    st.integers(min_value=1, max_value=5_000_000),  # base price in hundredths
    st.integers(min_value=0, max_value=2_000),  # half-span in hundredths
    st.integers(min_value=0, max_value=10_000_000),  # volume
    st.one_of(st.none(), st.integers(min_value=0, max_value=10_000_000)),  # open_interest
)


def _make_bar(spec: tuple[int, int, int, int, int | None]) -> CandleBar:
    minute, base_h, span_h, volume, oi = spec
    base = Decimal(base_h) / Decimal(100)
    span = Decimal(span_h) / Decimal(100)
    ts = datetime(2021, 1, 4, 0, 0, tzinfo=UTC).replace(hour=minute // 60, minute=minute % 60)
    # low ≤ open == close ≤ high so the CandleBar invariant always holds.
    return CandleBar(
        timestamp=ts,
        open=base,
        high=base + span,
        low=base - span if base - span >= Decimal(0) else Decimal(0),
        close=base,
        volume=volume,
        open_interest=oi,
    )


def _dedupe_by_minute(
    specs: list[tuple[int, int, int, int, int | None]],
) -> list[CandleBar]:
    """One bar per minute (the writer rejects a multi-partition / honours keys)."""
    by_minute: dict[int, CandleBar] = {}
    for spec in specs:
        by_minute[spec[0]] = _make_bar(spec)
    return list(by_minute.values())


@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    seed_specs=st.lists(_bar_strategy, min_size=1, max_size=40),
    merge_specs=st.lists(_bar_strategy, min_size=1, max_size=40),
)
def test_random_bar_sets_byte_identical_old_vs_new(
    tmp_path_factory: pytest.TempPathFactory,
    seed_specs: list[tuple[int, int, int, int, int | None]],
    merge_specs: list[tuple[int, int, int, int, int | None]],
) -> None:
    """A random seed → merge through both readers leaves byte-identical files."""
    seed_bars = _dedupe_by_minute(seed_specs)
    merge_bars = _dedupe_by_minute(merge_specs)

    root = tmp_path_factory.mktemp("parity")
    new_root = root / "new"
    legacy_root = root / "legacy"

    new_writer = ParquetWriter(lake_root=new_root)
    legacy_writer = ParquetWriter(lake_root=legacy_root)
    legacy_writer._read_existing_bars = staticmethod(  # type: ignore[method-assign]
        _legacy_read_existing_bars
    ).__func__

    new_seed = new_writer.write(bars=seed_bars, **_COORDS)
    legacy_seed = legacy_writer.write(bars=seed_bars, **_COORDS)
    # The fresh seed never reads — it is a sanity floor.
    assert new_seed.path.read_bytes() == legacy_seed.path.read_bytes()

    new_res = new_writer.write(bars=merge_bars, **_COORDS)
    legacy_res = legacy_writer.write(bars=merge_bars, **_COORDS)

    assert new_res.path.read_bytes() == legacy_res.path.read_bytes()
    assert new_res.raw_hash == legacy_res.raw_hash
    assert new_res.row_count == legacy_res.row_count
    assert new_res.conflicts == legacy_res.conflicts
