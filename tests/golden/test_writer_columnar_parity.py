"""Byte-parity golden: columnar ``_read_existing_bars`` == the legacy per-row read.

DEC-WRITER-COLUMNAR-4A-001 — the merge READ's per-row ``df.iter_rows(named=True)``
dict rebuild is replaced by COLUMN-wise ``Series.to_list`` extraction, a
byte-PRESERVING speed cut (the Decimal ``ROUND_HALF_UP`` 2dp reconstruction is
unchanged — switching to float math is explicitly out of scope, that is 4b).

This file is the non-negotiable parity oracle: for every merge scenario the
NEW (production) writer and a writer monkeypatched to the LEGACY per-row read
must produce a BYTE-IDENTICAL partition file, the SAME ``raw_hash``, and the
SAME reconstructed bar frame. The legacy implementation is reproduced verbatim
below so the comparison is a true before/after, not a self-comparison.

Whitebox: exercises the private ``_read_existing_bars`` reconstruction, so per
the DEC-P0-017 convention the file silences ``reportPrivateUsage`` at the top.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from brutex.data.atomic_io import lake_write_guard
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter, WriteResult, _tick_round

if TYPE_CHECKING:  # pragma: no cover
    pass

pytest.importorskip("polars")


def _legacy_read_existing_bars(dest: Path) -> dict[datetime, CandleBar]:
    """The PRE-DEC-WRITER-COLUMNAR-4A-001 per-row reconstruction, verbatim.

    Reproduces the exact ``df.iter_rows(named=True)`` + ``Decimal(str(...))`` +
    ``_tick_round`` path the production reader replaced, so the parity test is a
    genuine old-vs-new byte comparison rather than a self-comparison.
    """
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


def _bar(
    minute: int,
    *,
    open_: str,
    high: str,
    low: str,
    close: str,
    volume: int = 0,
    open_interest: int | None = None,
) -> CandleBar:
    # Spread *minute* across hours so the helper stays within one (year, month)
    # partition (2021-01) even for a deep 800-bar large-partition scenario.
    ts = datetime(2021, 1, 4, 0, 0, tzinfo=UTC).replace(hour=minute // 60, minute=minute % 60)
    return CandleBar(
        timestamp=ts,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
        open_interest=open_interest,
    )


_COORDS = {
    "exchange": "NSE",
    "segment": "INDEX",
    "symbol": "NIFTY",
    "interval": "1minute",
}


def _seed(writer: ParquetWriter, bars: list[CandleBar]) -> Path:
    """Write *bars* fresh and return the partition path."""
    res = writer.write(bars=bars, **_COORDS)
    return res.path


def _new_writer(tmp_path: Path) -> ParquetWriter:
    return ParquetWriter(lake_root=tmp_path)


def _legacy_writer(tmp_path: Path) -> ParquetWriter:
    """A writer whose merge READ uses the legacy per-row reconstruction."""
    writer = ParquetWriter(lake_root=tmp_path)
    # Bind the legacy reader as a static-method replacement on this instance only.
    writer._read_existing_bars = staticmethod(_legacy_read_existing_bars).__func__  # type: ignore[method-assign]
    return writer


def _assert_byte_parity(
    new_res: WriteResult,
    legacy_res: WriteResult,
    new_path: Path,
    legacy_path: Path,
) -> None:
    """Assert the two writes are byte-identical across every observable field."""
    new_bytes = new_path.read_bytes()
    legacy_bytes = legacy_path.read_bytes()
    assert new_bytes == legacy_bytes, "stored Parquet bytes diverged"
    assert new_res.raw_hash == legacy_res.raw_hash, "raw_hash diverged"
    assert new_res.row_count == legacy_res.row_count
    assert new_res.first_ts == legacy_res.first_ts
    assert new_res.last_ts == legacy_res.last_ts
    assert new_res.bytes_written == legacy_res.bytes_written
    assert new_res.conflicts == legacy_res.conflicts
    # Frame parity: the reconstructed bar maps must be identical.
    import polars as pl

    new_df = pl.read_parquet(new_path)
    legacy_df = pl.read_parquet(legacy_path)
    assert new_df.equals(legacy_df), "reconstructed frame diverged"


def _run_scenario(
    new_root: Path,
    legacy_root: Path,
    seed_bars: list[CandleBar],
    merge_bars: list[CandleBar],
    *,
    merge_mode: str = "keep-first",
) -> None:
    """Seed both lakes identically, merge *merge_bars*, assert byte parity."""
    new_writer = _new_writer(new_root)
    legacy_writer = _legacy_writer(legacy_root)

    new_seed_path = _seed(new_writer, seed_bars)
    legacy_seed_path = _seed(legacy_writer, seed_bars)
    # The fresh seed must already be byte-identical (it never touches the read path).
    assert new_seed_path.read_bytes() == legacy_seed_path.read_bytes()

    new_res = new_writer.write(bars=merge_bars, merge_mode=merge_mode, **_COORDS)
    legacy_res = legacy_writer.write(bars=merge_bars, merge_mode=merge_mode, **_COORDS)
    _assert_byte_parity(new_res, legacy_res, new_res.path, legacy_res.path)


class TestColumnarReadByteParity:
    """Every merge scenario: columnar read == legacy per-row read, byte-for-byte."""

    def test_empty_existing_fresh_write(self, tmp_path: Path) -> None:
        """A fresh write never reads — both paths produce identical first bytes."""
        bars = [_bar(0, open_="100.00", high="101.00", low="99.00", close="100.50", volume=5)]
        new_path = _seed(_new_writer(tmp_path / "new"), bars)
        legacy_path = _seed(_legacy_writer(tmp_path / "leg"), bars)
        assert new_path.read_bytes() == legacy_path.read_bytes()

    def test_partial_append(self, tmp_path: Path) -> None:
        seed = [_bar(0, open_="100.00", high="101.00", low="99.00", close="100.50", volume=5)]
        merge = [_bar(1, open_="100.50", high="102.00", low="100.00", close="101.75", volume=9)]
        _run_scenario(tmp_path / "n", tmp_path / "l", seed, merge)

    def test_full_re_touch_idempotent_no_rewrite(self, tmp_path: Path) -> None:
        """An identical re-pull is a no-op — both paths hash the existing file."""
        seed = [
            _bar(0, open_="100.00", high="101.00", low="99.00", close="100.50", volume=5),
            _bar(1, open_="100.50", high="102.00", low="100.00", close="101.75", volume=9),
        ]
        _run_scenario(tmp_path / "n", tmp_path / "l", seed, list(seed))

    def test_out_of_order_merge(self, tmp_path: Path) -> None:
        seed = [_bar(5, open_="100.00", high="101.00", low="99.00", close="100.50", volume=5)]
        merge = [
            _bar(2, open_="98.00", high="99.00", low="97.00", close="98.50", volume=3),
            _bar(8, open_="103.00", high="104.00", low="102.00", close="103.50", volume=7),
            _bar(0, open_="95.00", high="96.00", low="94.00", close="95.50", volume=1),
        ]
        _run_scenario(tmp_path / "n", tmp_path / "l", seed, merge)

    def test_exact_dup_timestamps_noop(self, tmp_path: Path) -> None:
        """Identical timestamp + identical values → silent no-op, no rewrite."""
        seed = [
            _bar(0, open_="100.00", high="101.00", low="99.00", close="100.50", volume=5),
            _bar(1, open_="100.50", high="102.00", low="100.00", close="101.75", volume=9),
        ]
        merge = [_bar(1, open_="100.50", high="102.00", low="100.00", close="101.75", volume=9)]
        _run_scenario(tmp_path / "n", tmp_path / "l", seed, merge)

    def test_same_ts_diff_value_keep_first_conflict(self, tmp_path: Path) -> None:
        """Same timestamp, different value → keep-first conflict recorded identically."""
        seed = [_bar(0, open_="100.00", high="101.00", low="99.00", close="100.50", volume=5)]
        merge = [_bar(0, open_="200.00", high="201.00", low="199.00", close="200.50", volume=8)]
        _run_scenario(tmp_path / "n", tmp_path / "l", seed, merge)

    def test_latest_wins_mode(self, tmp_path: Path) -> None:
        """latest-wins overwrites the stored bar — both read paths overwrite identically."""
        seed = [_bar(0, open_="100.00", high="101.00", low="99.00", close="100.50", volume=5)]
        merge = [_bar(0, open_="200.00", high="201.00", low="199.00", close="200.50", volume=8)]
        _run_scenario(tmp_path / "n", tmp_path / "l", seed, merge, merge_mode="latest-wins")

    def test_4dp_vendor_noise_converges_to_2dp(self, tmp_path: Path) -> None:
        """A stored 4dp remnant (14020.7999) reads back as 14020.80 on BOTH paths."""
        # Seed a 4dp-noisy bar by going AROUND _tick_round (write a raw frame),
        # so the read path is what converges it — exercising the reconstruction.
        import polars as pl

        new_root = tmp_path / "n"
        legacy_root = tmp_path / "l"
        new_writer = _new_writer(new_root)
        # First create the partition via a normal clean write so the path exists.
        clean = [
            _bar(0, open_="14020.7999", high="14020.7999", low="14020.7999", close="14020.7999")
        ]
        new_path = _seed(new_writer, clean)
        legacy_path = _seed(_legacy_writer(legacy_root), clean)
        # The clean write already snapped to 2dp; assert the stored value is 2dp,
        # then overwrite the on-disk file with a deliberate 4dp-noise frame to
        # exercise the read-path convergence on BOTH writers.
        noisy = pl.DataFrame(
            {
                "timestamp": [datetime(2021, 1, 4, 4, 0, tzinfo=UTC)],
                "open": [14020.7999],
                "high": [14020.7999],
                "low": [14020.7999],
                "close": [14020.7999],
                "volume": [0],
                "open_interest": [None],
            },
            schema={
                "timestamp": pl.Datetime(time_unit="us", time_zone="UTC"),
                "open": pl.Float64,
                "high": pl.Float64,
                "low": pl.Float64,
                "close": pl.Float64,
                "volume": pl.Int64,
                "open_interest": pl.Int64,
            },
        )
        noisy.write_parquet(new_path)
        noisy.write_parquet(legacy_path)
        # Now merge a clean 2dp bar at a NEW timestamp, forcing a re-read+rewrite
        # of the noisy partition through each writer's reconstruction.
        merge = [_bar(1, open_="100.00", high="100.00", low="100.00", close="100.00")]
        new_res = new_writer.write(bars=merge, **_COORDS)
        legacy_res = _legacy_writer(legacy_root).write(bars=merge, **_COORDS)
        _assert_byte_parity(new_res, legacy_res, new_res.path, legacy_res.path)
        # And the noisy bar converged to 2dp on disk (14020.7999 → 14020.80) —
        # exact float equality (approx would treat 14020.80 ≈ 14020.7999).
        stored_opens = pl.read_parquet(new_res.path)["open"].to_list()
        assert 14020.80 in stored_opens
        assert 14020.7999 not in stored_opens

    def test_open_interest_none_vs_int(self, tmp_path: Path) -> None:
        seed = [
            _bar(0, open_="100.00", high="101.00", low="99.00", close="100.50", open_interest=None),
            _bar(
                1, open_="100.50", high="102.00", low="100.00", close="101.75", open_interest=1234
            ),
        ]
        merge = [
            _bar(2, open_="101.00", high="103.00", low="100.50", close="102.50", open_interest=0),
            _bar(
                3, open_="102.00", high="104.00", low="101.50", close="103.50", open_interest=None
            ),
        ]
        _run_scenario(tmp_path / "n", tmp_path / "l", seed, merge)

    def test_large_partition(self, tmp_path: Path) -> None:
        """A deep partition (the steady-state case) stays byte-identical."""
        seed = [
            _bar(
                i,
                open_=f"{100 + i}.12",
                high=f"{101 + i}.34",
                low=f"{99 + i}.56",
                close=f"{100 + i}.78",
                volume=i * 7,
                open_interest=(None if i % 3 == 0 else i * 11),
            )
            for i in range(400)
        ]
        merge = [
            _bar(
                i,
                open_=f"{200 + i}.12",
                high=f"{201 + i}.34",
                low=f"{199 + i}.56",
                close=f"{200 + i}.78",
                volume=i * 13,
                open_interest=(None if i % 2 == 0 else i * 17),
            )
            for i in range(400, 800)
        ]
        _run_scenario(tmp_path / "n", tmp_path / "l", seed, merge)
