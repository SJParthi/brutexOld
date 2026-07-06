"""``ParquetWriter`` ↔ lake-manifest wiring (DEC-LAKE-MANIFEST-O1-001).

The write-side chokepoint: a FRESH write that CREATES a dataset (its interval
dir held zero ``*.parquet``) upserts one manifest row; a new month of an
existing dataset and a merge append cost ZERO manifest work; the
``heal_partition`` quarantine-MOVE (file removal) invalidates the manifest.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from brutex.data import lake_manifest as lm
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.window_ledger_store import ledger_path


def _bar(day: int, minute: int) -> CandleBar:
    return CandleBar(
        timestamp=datetime(2024, 6, day, 4, minute, tzinfo=UTC),
        open=Decimal("100.00"),
        high=Decimal("101.00"),
        low=Decimal("99.00"),
        close=Decimal("100.50"),
        volume=10,
        open_interest=None,
    )


def _seed_base(lake: Path) -> None:
    """Seed the ledger + an EMPTY manifest base (the walk-built bootstrap)."""
    target = ledger_path(lake)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"ledger-v1")
    token = lm.ledger_stat_token(lake)
    assert token is not None
    assert lm.rebuild_manifest(lake, [], ledger_token=token) is True


def _meta_rows(lake: Path) -> int:
    return int(json.loads(lm.manifest_meta_path(lake).read_text(encoding="utf-8"))["rows"])


class TestWriterManifestUpsert:
    def test_fresh_dataset_records_row(self, tmp_path: Path) -> None:
        _seed_base(tmp_path)
        writer = ParquetWriter(lake_root=tmp_path)
        writer.write(
            exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", bars=[_bar(3, 0)]
        )
        assert lm.load_manifest_datasets(tmp_path) == [
            {"exchange": "NSE", "segment": "INDEX", "symbol": "NIFTY", "interval": "1minute"}
        ]

    def test_no_manifest_base_is_untouched(self, tmp_path: Path) -> None:
        """Without a walk-built base the writer must NOT accrete a partial manifest."""
        writer = ParquetWriter(lake_root=tmp_path)
        writer.write(
            exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", bars=[_bar(3, 0)]
        )
        assert not lm.manifest_meta_path(tmp_path).exists()
        assert not lm.manifest_data_path(tmp_path).exists()

    def test_new_month_of_existing_dataset_adds_no_row(self, tmp_path: Path) -> None:
        """A fresh MONTH file of a known dataset short-circuits on the rglob probe."""
        _seed_base(tmp_path)
        writer = ParquetWriter(lake_root=tmp_path)
        writer.write(
            exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", bars=[_bar(3, 0)]
        )
        assert _meta_rows(tmp_path) == 1
        july = CandleBar(
            timestamp=datetime(2024, 7, 1, 4, 0, tzinfo=UTC),
            open=Decimal("100.00"),
            high=Decimal("101.00"),
            low=Decimal("99.00"),
            close=Decimal("100.50"),
            volume=10,
            open_interest=None,
        )
        writer.write(
            exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", bars=[july]
        )
        assert _meta_rows(tmp_path) == 1  # no duplicate row for a new month

    def test_merge_append_adds_no_row(self, tmp_path: Path) -> None:
        """The hot existing-partition merge path never touches the manifest."""
        _seed_base(tmp_path)
        writer = ParquetWriter(lake_root=tmp_path)
        writer.write(
            exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", bars=[_bar(3, 0)]
        )
        meta_before = lm.manifest_meta_path(tmp_path).read_bytes()
        writer.write(
            exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", bars=[_bar(3, 1)]
        )
        assert lm.manifest_meta_path(tmp_path).read_bytes() == meta_before

    def test_heal_quarantine_move_invalidates(self, tmp_path: Path) -> None:
        """Removing a partition's every row (quarantine-MOVE) invalidates the manifest."""
        _seed_base(tmp_path)
        writer = ParquetWriter(lake_root=tmp_path)
        bar = _bar(3, 0)
        writer.write(
            exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", bars=[bar]
        )
        assert lm.manifest_meta_path(tmp_path).exists()
        dest = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2024" / "06.parquet"
        result = writer.heal_partition(
            dest,
            remove_timestamps=frozenset({bar.timestamp}),
            quarantine_to=tmp_path / ".quarantine" / "ts" / "06.parquet",
        )
        assert result.kept == 0 and result.raw_hash_after is None
        assert not lm.manifest_meta_path(tmp_path).exists()
        assert not lm.manifest_data_path(tmp_path).exists()

    def test_heal_rewrite_keeps_manifest(self, tmp_path: Path) -> None:
        """A row-KEEPING heal (dedup rewrite) does not invalidate the manifest."""
        _seed_base(tmp_path)
        writer = ParquetWriter(lake_root=tmp_path)
        writer.write(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=[_bar(3, 0), _bar(3, 1)],
        )
        dest = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2024" / "06.parquet"
        result = writer.heal_partition(
            dest,
            remove_timestamps=frozenset({_bar(3, 1).timestamp}),
            quarantine_to=tmp_path / ".quarantine" / "ts" / "06.parquet",
        )
        assert result.kept == 1  # rows remain — the dataset still exists
        assert lm.manifest_meta_path(tmp_path).exists()
