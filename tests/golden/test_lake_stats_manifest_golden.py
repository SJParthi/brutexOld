"""Golden schema-freeze pin for the stats-manifest shard JSON (DEC-LAKE-STATS-MANIFEST-001).

Pins :func:`brutex.data.lake_stats_manifest.render_shard`'s byte-for-byte
output for a fixed input. Any change to the shard schema — field names, key
order, timestamp rendering, version — breaks this literal and must bump
``STATS_MANIFEST_VERSION`` (old shards are then treated absent + reseeded, by
design). The DEC-LAKE-MANIFEST-O1-001 dataset-manifest goldens are untouched.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from brutex.data import lake_stats_manifest as lsm

pytestmark = pytest.mark.golden

_EXPECTED_BARS_SHARD = (
    '{"files":['
    '{"max_ts":"2026-06-30T10:00:00+00:00","min_ts":"2026-06-01T03:45:00+00:00",'
    '"mtime_ns":1751000000000000001,"raw_hash":"'
    + "a"
    * 64
    + '","rel":"2026/06.parquet","rows":5,"size":1024},'
    '{"max_ts":"2026-07-15T09:59:00+00:00","min_ts":"2026-07-01T03:45:00+00:00",'
    '"mtime_ns":1751500000000000002,"raw_hash":null,"rel":"2026/07.parquet","rows":3,'
    '"size":2048}],'
    '"identity_key":["timestamp"],'
    '"key":{"exchange":"NSE","interval":"1minute","segment":"INDEX","symbol":"NIFTY"},'
    '"kind":"bars",'
    '"max_ts":"2026-07-15T09:59:00+00:00",'
    '"min_ts":"2026-06-01T03:45:00+00:00",'
    '"row_count":8,'
    '"updated_utc":"2026-07-06T00:00:00+00:00",'
    '"version":1}'
)


class TestShardGolden:
    def test_bars_shard_literal_pinned(self) -> None:
        june = lsm.StatsFileRow(
            rel="2026/06.parquet",
            rows=5,
            min_ts=datetime(2026, 6, 1, 3, 45, tzinfo=UTC),
            max_ts=datetime(2026, 6, 30, 10, 0, tzinfo=UTC),
            mtime_ns=1751000000000000001,
            size=1024,
            raw_hash="a" * 64,
        )
        july = lsm.StatsFileRow(
            rel="2026/07.parquet",
            rows=3,
            min_ts=datetime(2026, 7, 1, 3, 45, tzinfo=UTC),
            max_ts=datetime(2026, 7, 15, 9, 59, tzinfo=UTC),
            mtime_ns=1751500000000000002,
            size=2048,
            raw_hash=None,
        )
        stats = lsm.build_dataset_stats(
            kind=lsm.KIND_BARS,
            key={"exchange": "NSE", "segment": "INDEX", "symbol": "NIFTY", "interval": "1minute"},
            files=[july, june],  # deliberately unsorted — the factory sorts
            updated_utc="2026-07-06T00:00:00+00:00",
        )
        assert lsm.render_shard(stats) == _EXPECTED_BARS_SHARD

    def test_version_constant_pinned(self) -> None:
        assert lsm.STATS_MANIFEST_VERSION == 1
