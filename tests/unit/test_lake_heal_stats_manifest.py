"""``lake_heal._quarantine_move`` ↔ lake STATS manifest (DEC-LAKE-STATS-MANIFEST-001).

The heal batch's ONE file-removal chokepoint (`_quarantine_move` — the
whole-file quarantine AND the intraday-TF delete-and-rebuild) must unlink the
affected dataset's WHOLE stats shard beside the existing dataset-manifest
invalidation — a per-row edit would leave prefix sums lying about months a
rebuild is about to replace. Both trees are covered (bars + options), the
``(kind, key)`` derivation is VERBATIM from the on-disk path (an underivable
path abstains), and observe mode mutates ZERO shard bytes.

Whitebox per the DEC-P0-017 convention (drives the private ``_BatchState`` +
``_quarantine_move`` + ``_stats_dataset_coords`` directly — the public
``heal_findings`` routes are exercised end-to-end by ``test_lake_heal.py``),
so ``reportPrivateUsage`` is silenced.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from brutex.data import lake_stats_manifest as lsm
from brutex.data.lake_heal import (
    _BatchState,
    _quarantine_move,
    _remove_dataset_stats_for,
    _stats_dataset_coords,
    heal_findings,
)
from brutex.data.lake_invariants import scan_lake_invariants
from brutex.data.option_parquet_writer import OptionParquetWriter
from brutex.data.parquet_writer import ParquetWriter

BARS_KEY: dict[str, str] = {
    "exchange": "NSE",
    "segment": "INDEX",
    "symbol": "NIFTY",
    "interval": "1minute",
}

OPTIONS_KEY: dict[str, str] = {"series": "NSE_FNO__13__WEEK__0__ATM__CALL"}


@pytest.fixture(autouse=True)
def _python_oracle_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin this suite to the EXPLICIT flag-OFF (Python oracle) arm.

    Conscious W-5 reconciliation (recorded in the Wave-D lane scratchpad):
    since the ``BRUTEX_LAKE_WAVED_RUST`` default flipped ON, an unset env
    routes the heal through the crate core — but THIS suite is the verbatim
    PYTHON core's verification (the live degrade oracle every out-of-envelope
    shape still serves; its 100% ci-gate coverage is the oracle's gate), so
    it runs under the operator kill switch. The crate arm's gates are the
    heal differential (``tests/integration/test_waved_heal_differential.py``,
    default-ON) + the seam suite's explicit flag-ON tests.
    """
    monkeypatch.setenv("BRUTEX_LAKE_WAVED_RUST", "0")


def _state(lake: Path) -> _BatchState:
    return _BatchState(
        lake_root=lake,
        quarantine_root=lake / ".quarantine" / "20260706T000000Z",
        audit=None,
        catalog=None,
        writer=ParquetWriter(lake_root=lake),
        option_writer=OptionParquetWriter(lake_root=lake),
    )


def _plant_file(lake: Path, kind: str, key: dict[str, str], rel: str) -> Path:
    source = lsm.dataset_dir(lake, kind, key) / rel
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"partition-bytes")
    return source


def _seed_shard(lake: Path, kind: str, key: dict[str, str], source: Path) -> Path:
    stat = os.stat(source)
    rel = f"{source.parent.name}/{source.name}" if kind == lsm.KIND_BARS else source.name
    row = lsm.StatsFileRow(
        rel=rel,
        rows=5,
        min_ts=datetime(2024, 6, 3, 4, 0, tzinfo=UTC),
        max_ts=datetime(2024, 6, 3, 4, 30, tzinfo=UTC),
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
        raw_hash=None,
    )
    assert lsm.seed_dataset_stats(lake, kind=kind, key=key, files=[row])
    return lsm.stats_shard_path(lake, kind, key)


def _poison_dup(dest: Path, rows: Sequence[tuple[datetime, float]]) -> None:
    """Raw-pyarrow plant of VERBATIM rows (duplicates included) — the #1067 way."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest.parent.mkdir(parents=True, exist_ok=True)
    prices = [price for _, price in rows]
    pq.write_table(
        pa.table(
            {
                "timestamp": pa.array([ts for ts, _ in rows], type=pa.timestamp("us", tz="UTC")),
                "open": pa.array(prices, type=pa.float64()),
                "high": pa.array(prices, type=pa.float64()),
                "low": pa.array(prices, type=pa.float64()),
                "close": pa.array(prices, type=pa.float64()),
                "volume": pa.array([10] * len(rows), type=pa.int64()),
                "open_interest": pa.array([None] * len(rows), type=pa.int64()),
            }
        ),
        dest,
    )


class TestQuarantineMoveUnlinksShard:
    def test_bars_tree_shard_unlinked(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        source = _plant_file(lake, lsm.KIND_BARS, BARS_KEY, "2024/06.parquet")
        shard = _seed_shard(lake, lsm.KIND_BARS, BARS_KEY, source)

        dest = _quarantine_move(_state(lake), source)

        assert not source.exists()  # moved away
        assert dest.read_bytes() == b"partition-bytes"  # quarantine preserved
        assert not shard.exists()  # the WHOLE shard is gone
        assert lsm.load_shard_stats(lake, kind=lsm.KIND_BARS, key=BARS_KEY) is None

    def test_options_tree_shard_unlinked(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        source = _plant_file(lake, lsm.KIND_OPTIONS, OPTIONS_KEY, "2024-06.parquet")
        shard = _seed_shard(lake, lsm.KIND_OPTIONS, OPTIONS_KEY, source)

        _quarantine_move(_state(lake), source)

        assert not source.exists()
        assert not shard.exists()

    def test_kill_switch_off_leaves_shard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The kill switch makes the stats hook a no-op — the move still happens."""
        lake = tmp_path / "lake"
        source = _plant_file(lake, lsm.KIND_BARS, BARS_KEY, "2024/06.parquet")
        shard = _seed_shard(lake, lsm.KIND_BARS, BARS_KEY, source)
        monkeypatch.setenv(lsm.STATS_MANIFEST_ENV, "off")

        _quarantine_move(_state(lake), source)

        assert not source.exists()  # the quarantine itself is untouched
        assert shard.exists()  # ... but the shard hook no-opped (legacy bytes)


class TestStatsDatasetCoords:
    """The VERBATIM path → ``(kind, key)`` derivation (abstain on any other shape)."""

    def test_bars_partition(self, tmp_path: Path) -> None:
        source = tmp_path / "bars" / "NSE" / "CASH" / "NSE-M&M" / "1day" / "2024" / "06.parquet"
        assert _stats_dataset_coords(tmp_path, source) == (
            lsm.KIND_BARS,
            {
                "exchange": "NSE",
                "segment": "CASH",
                "symbol": "NSE-M&M",  # a #911-drift dir stays its own dataset
                "interval": "1day",
            },
        )

    def test_options_partition(self, tmp_path: Path) -> None:
        source = tmp_path / "options" / "SERIES" / "2024-06.parquet"
        assert _stats_dataset_coords(tmp_path, source) == (
            lsm.KIND_OPTIONS,
            {"series": "SERIES"},
        )

    def test_outside_lake_is_none(self, tmp_path: Path) -> None:
        assert _stats_dataset_coords(tmp_path, Path("/absolutely/elsewhere.parquet")) is None

    def test_bars_depth_without_bars_root_is_none(self, tmp_path: Path) -> None:
        source = tmp_path / "spare" / "NSE" / "CASH" / "SYM" / "1day" / "2024" / "06.parquet"
        assert _stats_dataset_coords(tmp_path, source) is None

    def test_options_depth_without_options_root_is_none(self, tmp_path: Path) -> None:
        assert _stats_dataset_coords(tmp_path, tmp_path / "bars" / "S" / "x.parquet") is None

    def test_wrong_depth_is_none(self, tmp_path: Path) -> None:
        assert _stats_dataset_coords(tmp_path, tmp_path / "stray.parquet") is None

    def test_remove_for_underivable_path_abstains(self, tmp_path: Path) -> None:
        """An underivable source is a silent no-op — other shards untouched."""
        lake = tmp_path / "lake"
        source = _plant_file(lake, lsm.KIND_BARS, BARS_KEY, "2024/06.parquet")
        shard = _seed_shard(lake, lsm.KIND_BARS, BARS_KEY, source)

        _remove_dataset_stats_for(lake, lake / "stray.parquet")

        assert shard.exists()  # nothing was unlinked


class TestObserveModeZeroShardMutation:
    def test_observe_heal_leaves_shard_bytes_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Observe mode heals nothing ⇒ the byte-pinned zero-mutation contract
        holds trivially for the stats shards too."""
        pytest.importorskip("polars")
        monkeypatch.setenv("BRUTEX_LAKE_HEAL", "observe")
        lake = tmp_path / "lake"
        dest = lake / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2024" / "06.parquet"
        ts = datetime(2024, 6, 3, 4, 0, tzinfo=UTC)
        _poison_dup(dest, [(ts, 100.0), (ts, 999.0)])
        shard = _seed_shard(lake, lsm.KIND_BARS, BARS_KEY, dest)
        before = shard.read_bytes()

        scan = scan_lake_invariants(lake)
        assert len(scan.findings) == 1
        report = heal_findings(scan.findings, lake_root=lake, files_scanned=scan.files_scanned)

        assert report.mode == "observe"
        assert report.healed == 0
        assert shard.read_bytes() == before  # zero shard mutation
