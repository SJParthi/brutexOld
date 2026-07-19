"""Shared LANE-W5 test fixture builders (decay / champions / nightly e2e).

CLAUDE.md §11 COMMON: one loader-valid ``top100.json`` writer + one
raw-planted index-lake builder shared by ``test_nightly_decay.py``,
``test_nightly_champions.py`` and the nightly integration suite — never
copy-pasted per file. The artifact writer produces bytes
:func:`brutex.engine.top100_artifact.load_top100` VALIDATES (correct
stamp sha256 + every ``ENTRY_REQUIRED_KEYS`` member + rank order); the
lake builder plants RAW pyarrow partitions (the
``test_engine_lake_slice`` idiom) that the REAL
``load_lake_slice`` → Stage-1A → trade-sim chain consumes.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

#: A fixed, clock-free "tonight" (UTC) — every planted session is within
#: the trailing calendar window of this instant.
FIXTURE_NOW = datetime(2024, 6, 20, 18, 0, tzinfo=UTC)

#: Twelve June-2024 NSE weekdays (all inside the 2024/06 partition).
FIXTURE_SESSION_DAYS = (3, 4, 5, 6, 7, 10, 11, 12, 13, 14, 17, 18)


def make_entry(
    rank: int,
    *,
    direction: str = "bull",
    instrument: str = "NIFTY",
    tf: str = "1minute",
    sharpe: float | None = 2.0,
    max_dd_paisa: int | None = 100_000,
    mask_hex: str = "0" * 32,
    cascade_status: str = "pass",
    sl_paisa: int | None = None,
    tp_paisa: int | None = None,
) -> dict[str, object]:
    """One loader-valid top100 entry (every ``ENTRY_REQUIRED_KEYS`` member)."""
    return {
        "rank": rank,
        "run_id": f"{rank:064x}",
        "mask_hex": mask_hex,
        "conditions": [],
        "instrument": instrument,
        "tf": tf,
        "direction": direction,
        "arm": {"sl_paisa": sl_paisa, "tp_paisa": tp_paisa},
        "metrics": {"sharpe_ratio": sharpe, "max_drawdown_paisa": max_dd_paisa},
        "regime": {},
        "vix_band": {},
        "family_id": f"fam-{rank}",
        "family_members": [],
        "reasons": None,
        "margin_roi": None,
        "equity_ref": {"tuple": f"t{rank}", "sidecar": "equity"},
        "cascade": {"status": cascade_status},
    }


def write_top100_fixture(
    results_root: Path,
    run_id: str,
    entries: Sequence[Mapping[str, object]],
    *,
    mtime: float | None = None,
) -> Path:
    """Write a loader-valid ``top100.json`` + stamp into ``sweep_runs/<id>/``.

    *mtime* (optional) is applied to the IMMUTABLE ``top100.json`` FILE —
    the discovery sort key (round-1 MINOR #2) — for tests that pin
    explicit orderings; natural write-order mtimes suffice otherwise.
    """
    from brutex.engine.top100_artifact import (
        TOP100_FILENAME,
        TOP100_SCHEMA_VERSION,
        TOP100_STAGE,
        TOP100_STAMP_FILENAME,
    )

    run_dir = results_root / "sweep_runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": TOP100_SCHEMA_VERSION,
        "run_id": run_id,
        "preset": "min_dd_first",
        "source": {"test": True},
        "baselines": {},
        "entries": list(entries),
    }
    raw = json.dumps(payload).encode("utf-8")
    (run_dir / TOP100_FILENAME).write_bytes(raw)
    stamp = {
        "stage": TOP100_STAGE,
        "run_id": run_id,
        "content_hash": hashlib.sha256(raw).hexdigest(),
        "produced_at_utc": "2024-06-20T00:00:00+00:00",
    }
    (run_dir / TOP100_STAMP_FILENAME).write_text(json.dumps(stamp), encoding="utf-8")
    if mtime is not None:
        os.utime(run_dir / TOP100_FILENAME, (mtime, mtime))
    return run_dir


def plant_index_lake(lake: Path, *, symbol: str = "NIFTY") -> None:
    """Plant 12 sessions of 5 descending 1-minute index bars (raw pyarrow).

    Per session index ``i``: five bars at 04:00..04:04 UTC (09:30..09:34
    IST) with prices stepping DOWN by ``0.05 * (i + 1)`` rupees per bar —
    a bull mask-0 finalist re-simulated over these LOSES per session with
    varying magnitude (a real negative Sharpe); a bear one WINS.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq
    from brutex.data.partition import build_partition_path

    rows: list[tuple[datetime, float]] = []
    for i, day in enumerate(FIXTURE_SESSION_DAYS):
        step = 0.05 * (i + 1)
        for minute in range(5):
            price = round(100.0 - step * minute, 2)
            rows.append((datetime(2024, 6, day, 4, minute, tzinfo=UTC), price))
    dest = build_partition_path(
        lake_root=lake,
        exchange="NSE",
        segment="INDEX",
        symbol=symbol,
        interval="1minute",
        year=2024,
        month=6,
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    prices = [price for _, price in rows]
    table = pa.table(
        {
            "timestamp": pa.array([ts for ts, _ in rows], type=pa.timestamp("us", tz="UTC")),
            "open": pa.array(prices, type=pa.float64()),
            "high": pa.array(prices, type=pa.float64()),
            "low": pa.array(prices, type=pa.float64()),
            "close": pa.array(prices, type=pa.float64()),
            "volume": pa.array([10] * len(rows), type=pa.int64()),
            "open_interest": pa.array([None] * len(rows), type=pa.int64()),
        }
    )
    pq.write_table(table, dest)
