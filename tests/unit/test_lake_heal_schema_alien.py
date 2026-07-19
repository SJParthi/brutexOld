"""The :class:`SchemaAlienFile` heal — whole-file quarantine MOVE.

DEC-ALIEN-SCHEMA-READ-HEAL-001 (the lake_heal half; the scanner half is
``test_lake_invariants_schema_alien.py``). FAILS-BEFORE: the scanner emitted
NO finding for a schema-poisoned file, so ``heal_findings`` never touched it —
the file stayed in place forever while the dashboard read side skipped it (or
pre-fix 500'd on it) on every poll. Now: the real end-to-end scan → heal moves
the alien file whole into ``.quarantine/`` (recoverable, never value-edited),
drops its catalog row, marks the derived recall, and stays honest in observe
mode / for a vanished file / for the PriceSanityRows observe-only filter.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from structlog.testing import capture_logs

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

import polars as pl
from brutex.data import lake_heal
from brutex.data.catalog import Catalog
from brutex.data.lake_heal import AUDIT_HEAL_QUARANTINE_FILE, heal_findings
from brutex.data.lake_invariants import SchemaAlienFile, scan_lake_invariants
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.partition import build_partition_path


@pytest.fixture(autouse=True)
def _endgame_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the ONE (Rust) heal/scan arm — W-6a deleted the Python cores."""
    monkeypatch.delenv("BRUTEX_LAKE_WAVED_RUST", raising=False)


def _now() -> datetime:
    return datetime(2026, 7, 9, 4, 0, tzinfo=UTC)


def _ts(minute: int) -> datetime:
    return datetime(2024, 6, 3, 4, minute, tzinfo=UTC)


def _bar(ts: datetime) -> CandleBar:
    c = Decimal("100.00")
    return CandleBar(timestamp=ts, open=c, high=c, low=c, close=c, volume=10, open_interest=None)


def _dest(lake: Path, *, symbol: str = "NIFTY", interval: str = "1minute") -> Path:
    return build_partition_path(
        lake_root=lake,
        exchange="NSE",
        segment="INDEX",
        symbol=symbol,
        interval=interval,
        year=2024,
        month=6,
    )


def _plant_alien(dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"ts": [1], "px": [1.0]}).write_parquet(dest)


class TestSchemaAlienHeal:
    def test_scan_to_heal_quarantines_alien_file(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        alien = _dest(lake)
        _plant_alien(alien)
        scan = scan_lake_invariants(lake)
        assert any(isinstance(f, SchemaAlienFile) for f in scan.findings)
        with capture_logs() as logs:
            report = heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=max(scan.files_scanned, 1),
                mode="auto",
                recall=False,
                now=_now,
            )
        assert report.healed == 1
        assert not alien.exists()
        assert report.quarantine_root is not None
        quarantined = Path(report.quarantine_root) / alien.relative_to(lake)
        assert quarantined.is_file()
        # Recoverable bytes, never value-edited: the quarantined copy still
        # parses as the SAME alien frame.
        assert pl.read_parquet(quarantined).columns == ["ts", "px"]
        assert [a.action for a in report.actions] == [AUDIT_HEAL_QUARANTINE_FILE]
        action = report.actions[0]
        assert action.partition == "bars/NSE/INDEX/NIFTY/1minute/2024/06.parquet"
        assert (action.kept, action.dropped, action.moved) == (0, 0, 0)
        events = [e for e in logs if e["event"] == "lake_heal.alien_file_quarantined"]
        assert len(events) == 1
        assert "missing column(s)" in events[0]["reason"]

    def test_bars_alien_drops_catalog_row_and_marks_recall(self, tmp_path: Path) -> None:
        # W-6a: driven through the public heal_findings chokepoint — the
        # per-finding Python heal was deleted; the observables are the healed
        # count, the quarantine-move, the catalog delete and the recall fire.
        lake = tmp_path / "lake"
        writer = ParquetWriter(lake_root=lake)
        result = writer.write(
            exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", bars=[_bar(_ts(1))]
        )
        alien = _dest(lake)
        _plant_alien(alien)
        scan = scan_lake_invariants(lake)
        finding = next(f for f in scan.findings if isinstance(f, SchemaAlienFile))
        with Catalog.open(lake / "catalog.duckdb") as catalog:
            catalog.upsert(
                writer.make_catalog_entry(
                    exchange="NSE",
                    segment="INDEX",
                    symbol="NIFTY",
                    interval="1minute",
                    result=result,
                )
            )
            report = lake_heal.heal_findings(
                (finding,),
                lake_root=lake,
                files_scanned=scan.files_scanned,
                mode="auto",
                catalog=catalog,
                recall=True,
                now=_now,
            )
            assert report.healed == 1 and report.errors == 0
            assert not alien.exists()
            # Catalog row dropped (Golden Rule #4) + the 1-minute month marked
            # for the derived recall (the recall genuinely ran).
            row = catalog.get(
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                year=2024,
                month=6,
            )
        assert row is None
        assert report.recall is not None

    def test_non_minute_alien_never_marks_recall(self, tmp_path: Path) -> None:
        # W-6a: through the public chokepoint — a non-1minute alien heals
        # (quarantine-move) but never marks the derived recall.
        lake = tmp_path / "lake"
        alien = _dest(lake, interval="1day")
        _plant_alien(alien)
        scan = scan_lake_invariants(lake)
        finding = next(f for f in scan.findings if isinstance(f, SchemaAlienFile))
        report = lake_heal.heal_findings(
            (finding,),
            lake_root=lake,
            files_scanned=scan.files_scanned,
            mode="auto",
            recall=True,
            now=_now,
        )
        assert report.healed == 1 and report.errors == 0
        assert not alien.exists()
        assert report.recall is None  # no 1minute touch → no recall

    def test_options_tree_alien_quarantined(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        alien = lake / "options" / "SERIES-A" / "2024-06.parquet"
        alien.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({"nothing": [1]}).write_parquet(alien)
        scan = scan_lake_invariants(lake)
        report = heal_findings(
            scan.findings,
            lake_root=lake,
            files_scanned=1,
            mode="auto",
            recall=False,
            now=_now,
        )
        assert report.healed == 1
        assert not alien.exists()
        assert report.actions[0].partition == "options/SERIES-A/2024-06.parquet"

    def test_vanished_alien_file_skipped(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        alien = _dest(lake)
        _plant_alien(alien)
        scan = scan_lake_invariants(lake)
        alien.unlink()  # consumed between scan and heal
        report = heal_findings(
            scan.findings,
            lake_root=lake,
            files_scanned=1,
            mode="auto",
            recall=False,
            now=_now,
        )
        assert report.healed == 0
        assert report.skipped == 1

    def test_observe_mode_never_moves_the_file(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        alien = _dest(lake)
        _plant_alien(alien)
        scan = scan_lake_invariants(lake)
        report = heal_findings(
            scan.findings,
            lake_root=lake,
            files_scanned=1,
            mode="observe",
            recall=False,
            now=_now,
        )
        assert report.healed == 0
        assert alien.exists()

    def test_order_alien_heals_last(self) -> None:
        # Deterministic order: the alien whole-file quarantine sorts AFTER every
        # row-level heal class (drift 0 → foreign 1 → dup 2 → option-dup 3 → alien 4).
        alien = SchemaAlienFile(path=Path("/x/a.parquet"), reason="r")
        from brutex.data.lake_invariants import IntraFileDup, PartitionCoords

        coords = PartitionCoords(
            exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", year=2024, month=6
        )
        dup = IntraFileDup(path=Path("/x/z.parquet"), coords=coords, physical_rows=2, unique_keys=1)
        ordered = lake_heal._order_findings([alien, dup])
        assert [type(f).__name__ for f in ordered] == ["IntraFileDup", "SchemaAlienFile"]
