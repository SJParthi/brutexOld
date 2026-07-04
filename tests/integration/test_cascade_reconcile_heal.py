"""L1 heal wiring at the cascade-wire reconcile chokepoint (DEC-LAKE-HEAL-001).

``brutex.engine.cascade_wire.load_bars_for_manifest`` reconciles the catalog
from on-disk Parquet before every slice read; the reconcile's FREE invariant
piggyback (DEC-LAKE-HEAL-001 L1) returns any poisoned-partition findings on
``ReconcileResult.findings``, and ONE ``heal_after_reconcile`` call at that
chokepoint heals them BEFORE the feed consumes the slice.

This suite drives the REAL end-to-end path over a poisoned tmp lake: raw
pyarrow-planted duplicate timestamps (the #1067 convention — the fixed writer
refuses dups) → ``load_bars_for_manifest`` → the returned bars are the
keep-FIRST winners, the on-disk file is healed (unique + strictly increasing),
the catalog row carries the ROTATED ``raw_hash`` of the healed bytes (Golden
Rule #4), a restorable quarantine copy of the ORIGINAL bytes exists under
``<lake>/.quarantine/``, and one hash-chained ``LAKE_HEAL_DEDUP`` audit line
landed in ``<lake>/audit/lake-heal.jsonl``. A second run over the healed lake
is a byte-identical no-op (Golden Rule #7), and ``BRUTEX_LAKE_HEAL=observe``
touches nothing.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from brutex.data.partition import build_partition_path

pytest.importorskip("polars")
pytest.importorskip("pyarrow")
pytest.importorskip("duckdb", reason="Catalog needs duckdb")

import brutex.engine.cascade_wire as cw
from brutex.data.catalog import Catalog

_SWEEP_ID = "a" * 64


def _ts(minute: int) -> datetime:
    """A UTC 1-minute instant inside the June-2024 partition (Mon 3 Jun, IST session)."""
    return datetime(2024, 6, 3, 4, minute, tzinfo=UTC)


def _poison_partition(dest: Path, rows: Sequence[tuple[datetime, float, int]]) -> None:
    """Write a RAW pyarrow partition with VERBATIM duplicate timestamps."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest.parent.mkdir(parents=True, exist_ok=True)
    prices = [price for _, price, _ in rows]
    table = pa.table(
        {
            "timestamp": pa.array([ts for ts, _, _ in rows], type=pa.timestamp("us", tz="UTC")),
            "open": pa.array(prices, type=pa.float64()),
            "high": pa.array(prices, type=pa.float64()),
            "low": pa.array(prices, type=pa.float64()),
            "close": pa.array(prices, type=pa.float64()),
            "volume": pa.array([vol for _, _, vol in rows], type=pa.int64()),
            "open_interest": pa.array([None] * len(rows), type=pa.int64()),
        }
    )
    pq.write_table(table, dest)


def _dest(lake: Path) -> Path:
    return build_partition_path(
        lake_root=lake,
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        year=2024,
        month=6,
    )


def _seed_poisoned_lake(lake: Path) -> Path:
    """Plant a June-2024 NIFTY 1minute partition with one duplicated timestamp."""
    dest = _dest(lake)
    _poison_partition(
        dest,
        [
            (_ts(16), 100.0, 10),  # keep-first winner for _ts(16)
            (_ts(16), 999.0, 99),  # the poisoned duplicate (later physical row loses)
            (_ts(17), 101.0, 11),
        ],
    )
    return dest


def _manifest() -> cw.CascadeManifest:
    spec = cw.CascadeTupleSpec(
        instrument="NIFTY",
        tf_label="1minute",
        tf_minutes=1,
        direction_label="bull",
        sl_paisa=None,
        tp_paisa=None,
        prior_n=3,
        proximity_bps=None,
        start_equity_paisa=10_000_000,
        winners=((_SWEEP_ID, 0),),
    )
    return cw.CascadeManifest(
        version=cw.MANIFEST_VERSION,
        sweep_id=_SWEEP_ID,
        cascade_config=cw.cascade_config_for_sweep(_SWEEP_ID),
        bars_context=cw.BarsContext(
            exchange="NSE", segment="INDEX", start="2024-06-01", end="2024-07-01"
        ),
        specs=(spec,),
    )


def _stored_timestamps(dest: Path) -> list[datetime]:
    import polars as pl

    return list(pl.read_parquet(dest, columns=["timestamp"])["timestamp"])


class TestCascadeReconcileHeal:
    def test_poisoned_lake_healed_and_bars_read_clean(self, tmp_path: Path) -> None:
        """The flagship L1 pin: reconcile detects → heal fixes → replay reads clean."""
        lake = tmp_path / "lake"
        dest = _seed_poisoned_lake(lake)
        original_bytes = dest.read_bytes()

        bars_for = cw.load_bars_for_manifest(_manifest(), lake_root=lake)

        # The replay slice is the deduped keep-FIRST view: 2 unique bars, and
        # the duplicated instant carries the FIRST physical row's values.
        bars = bars_for("NIFTY", "1minute")
        assert [bar.timestamp for bar in bars] == [_ts(16), _ts(17)]
        assert float(bars[0].close) == 100.0
        assert bars[0].volume == 10

        # On disk: healed to unique, strictly-increasing timestamps.
        assert _stored_timestamps(dest) == [_ts(16), _ts(17)]

        # Catalog row: rotated raw_hash == sha256 of the HEALED bytes (never
        # the poisoned original's) — a healed partition can't masquerade.
        with Catalog.open(lake / "catalog.duckdb") as cat:
            entry = cat.get(
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                year=2024,
                month=6,
            )
        assert entry is not None
        assert entry.row_count == 2
        assert entry.raw_hash == hashlib.sha256(dest.read_bytes()).hexdigest()
        assert entry.raw_hash != hashlib.sha256(original_bytes).hexdigest()

        # Quarantine copy of the ORIGINAL poisoned bytes is restorable.
        quarantined = list((lake / ".quarantine").rglob("06.parquet"))
        assert len(quarantined) == 1
        assert quarantined[0].read_bytes() == original_bytes

        # One hash-chained LAKE_HEAL_DEDUP audit line landed.
        audit_path = lake / "audit" / "lake-heal.jsonl"
        assert audit_path.is_file()
        actions = [
            json.loads(line)["action"] for line in audit_path.read_text().strip().splitlines()
        ]
        assert actions.count("LAKE_HEAL_DEDUP") == 1

    def test_second_run_over_healed_lake_is_byte_identical(self, tmp_path: Path) -> None:
        """Golden Rule #7: re-running the wiring over a healed lake changes nothing."""
        lake = tmp_path / "lake"
        dest = _seed_poisoned_lake(lake)
        cw.load_bars_for_manifest(_manifest(), lake_root=lake)
        healed_bytes = dest.read_bytes()
        quarantine_dirs = sorted(p.name for p in (lake / ".quarantine").iterdir())

        bars_for = cw.load_bars_for_manifest(_manifest(), lake_root=lake)

        assert dest.read_bytes() == healed_bytes
        # No new quarantine batch dir was created by the clean second pass.
        assert sorted(p.name for p in (lake / ".quarantine").iterdir()) == quarantine_dirs
        assert [bar.timestamp for bar in bars_for("NIFTY", "1minute")] == [_ts(16), _ts(17)]

    def test_observe_mode_touches_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BRUTEX_LAKE_HEAL=observe: findings are detected but no byte changes."""
        monkeypatch.setenv("BRUTEX_LAKE_HEAL", "observe")
        lake = tmp_path / "lake"
        dest = _seed_poisoned_lake(lake)
        original_bytes = dest.read_bytes()

        cw.load_bars_for_manifest(_manifest(), lake_root=lake)

        assert dest.read_bytes() == original_bytes
        assert not (lake / ".quarantine").exists()
