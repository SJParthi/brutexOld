"""Golden byte-equality: manifest-served vs walked dataset list (DEC-LAKE-MANIFEST-O1-001).

A FIXED seeded lake (spot INDEX 1m+1d · FNO FUT · FNO CE · a #911 drift-key
dir · an invalid-exchange dir · an invalid-interval dir) is discovered twice —
once by the legacy O(files) walk, once manifest-served — and BOTH must equal
the pinned literal below, byte-for-byte on the JSON wire form. Any drift in
ordering, filtering, key naming, or the drift-key/invalid-dir handling fails
this golden.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from brutex.dashboard import db_view
from brutex.data import lake_manifest as lm
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.window_ledger_store import ledger_path

#: The pinned expected discovery result for the fixture lake below —
#: sorted by (exchange, segment, symbol, interval); the MCX + "9lives"
#: dirs are absent (invalid coordinates), the drift-key "NSE-M&M" present.
EXPECTED_GOLDEN: list[dict[str, str]] = [
    {
        "kind": "spot",
        "exchange": "NSE",
        "segment": "CASH",
        "symbol": "NSE-M&M",
        "interval": "1minute",
    },
    {
        "kind": "spot",
        "exchange": "NSE",
        "segment": "FNO",
        "symbol": "NSE-NIFTY-26Jun2024-24000-CE",
        "interval": "1minute",
    },
    {
        "kind": "spot",
        "exchange": "NSE",
        "segment": "FNO",
        "symbol": "NSE-NIFTY-26Jun2024-FUT",
        "interval": "1minute",
    },
    {"kind": "spot", "exchange": "NSE", "segment": "INDEX", "symbol": "NIFTY", "interval": "1day"},
    {
        "kind": "spot",
        "exchange": "NSE",
        "segment": "INDEX",
        "symbol": "NIFTY",
        "interval": "1minute",
    },
]


def _bar(minute: int) -> CandleBar:
    return CandleBar(
        timestamp=datetime(2024, 6, 3, 4, minute, tzinfo=UTC),
        open=Decimal("100.00"),
        high=Decimal("101.00"),
        low=Decimal("99.00"),
        close=Decimal("100.50"),
        volume=10,
        open_interest=None,
    )


def _seed_fixture_lake(lake: Path) -> None:
    ledger = ledger_path(lake)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_bytes(b"ledger-v1")
    writer = ParquetWriter(lake_root=lake)
    writer.write(
        exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", bars=[_bar(0)]
    )
    writer.write(exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1day", bars=[_bar(1)])
    writer.write(
        exchange="NSE",
        segment="FNO",
        symbol="NSE-NIFTY-26Jun2024-FUT",
        interval="1minute",
        bars=[_bar(2)],
    )
    writer.write(
        exchange="NSE",
        segment="FNO",
        symbol="NSE-NIFTY-26Jun2024-24000-CE",
        interval="1minute",
        bars=[_bar(3)],
    )
    drift = lake / "bars" / "NSE" / "CASH" / "NSE-M&M" / "1minute" / "2024"
    drift.mkdir(parents=True)
    (drift / "06.parquet").write_bytes(b"")
    bogus = lake / "bars" / "MCX" / "INDEX" / "GOLD" / "1minute" / "2024"
    bogus.mkdir(parents=True)
    (bogus / "06.parquet").write_bytes(b"")
    bogus_tf = lake / "bars" / "NSE" / "INDEX" / "NIFTY" / "9lives" / "2024"
    bogus_tf.mkdir(parents=True)
    (bogus_tf / "06.parquet").write_bytes(b"")


def test_walk_and_manifest_both_match_the_golden(tmp_path: Path) -> None:
    _seed_fixture_lake(tmp_path)
    walked = db_view._walk_candle_datasets(tmp_path, exclude_segments=frozenset(), intervals=None)
    assert json.dumps(walked) == json.dumps(EXPECTED_GOLDEN)  # byte pin, order included
    # First list_candle_datasets: walk + rebuild the manifest.
    first = db_view.list_candle_datasets(tmp_path)
    assert json.dumps(first) == json.dumps(EXPECTED_GOLDEN)
    assert lm.manifest_meta_path(tmp_path).exists()
    # Second: manifest-served — byte-identical to the golden AND the walk.
    served = db_view.list_candle_datasets(tmp_path)
    assert json.dumps(served) == json.dumps(EXPECTED_GOLDEN)
