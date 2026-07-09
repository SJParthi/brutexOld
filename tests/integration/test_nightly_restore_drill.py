"""THE CI restore drill (DEC-SWEEP-NIGHTLY-AUTOMATION-001, spec §6.4).

A miniature REAL lake — the window ledger written by the real
``window_ledger_store`` writers (base + one delta), a real
``InstrumentStore`` DuckDB, a sweeps tree with a tuple parquet + completion
sentinel and a REAL hash-chained ``SweepAuditWriter`` audit — is snapshotted,
mutilated (flipped ledger bytes, deleted registry, dropped tuple, plus one
EXTRA post-snapshot delta), then restored. Asserts byte-equality of every
restored file against the manifest, the deltas-clear law, ledger-LAST copy
order, a still-green audit chain, and that ``bars/`` is never touched. The
negative arms pin the flipped-snapshot refusal (lake byte-untouched) and the
corrupt-manifest refusal.

(The ``cli.main(["nightly", "restore", …])`` driving arm lands with the CLI
wiring commit — see ``tests/unit/test_cli_nightly*.py``.)
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.fetch_coverage import WindowKey
from brutex.data.instrument_store import Instrument, InstrumentStore
from brutex.data.window_ledger import WindowLedger
from brutex.data.window_ledger_store import append_ledger_delta, save_window_ledger
from brutex.engine.sweep_audit import SweepAuditWriter, verify_chain
from brutex.models import Exchange, InstrumentType, Segment
from brutex.nightly.backup import MANIFEST_NAME, PARTIAL_SUFFIX, BackupManifest, create_snapshot
from brutex.nightly.restore import (
    RestoreRefused,
    RestoreVerifyError,
    plan_restore,
    restore_snapshot,
    verify_snapshot,
)

_NOW = datetime(2026, 7, 7, 10, 0, tzinfo=UTC)


def _window_key(security_id: str) -> WindowKey:
    return WindowKey(
        market="spot",
        security_id=security_id,
        interval="1minute",
        start=date(2026, 7, 1),
        end=date(2026, 7, 7),
    )


def _nifty_row() -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=26000,
        symbol_name="NIFTY",
        display_name="NIFTY 50",
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


@pytest.fixture
def real_lake(tmp_path: Path) -> tuple[Path, Path]:
    """A miniature REAL lake + results tree built by the production writers."""
    lake = tmp_path / "lake"
    results = lake / "sweeps"
    lake.mkdir()

    # Window ledger: base parquet via the real save, then ONE real delta.
    ledger = WindowLedger()
    ledger.record(_window_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
    save_window_ledger(ledger, lake)
    ledger.record(_window_key("BANKNIFTY"), raw_bars=3, error_type=None, bars_appended=3, now=_NOW)
    appended = append_ledger_delta(ledger, lake)
    assert appended == 1
    assert len(list((lake / "window_ledger" / "deltas").glob("*.parquet"))) == 1

    # Instrument master: a REAL DuckDB with one row.
    store = InstrumentStore.open(lake / "instruments.duckdb")
    store.upsert_many(
        [_nifty_row()],
        snapshot_date=date(2026, 7, 7),
        run_id="drill-seed",
        csv_source_sha256="0" * 64,
    )
    store.close()

    # Small real-byte registry/catalog stand-ins + lake audit JSONL.
    (lake / "registry.duckdb").write_bytes(b"REGISTRY-BYTES")
    (lake / "catalog.duckdb").write_bytes(b"CATALOG-BYTES")
    (lake / "audit").mkdir()
    (lake / "audit" / "retry-storm-p1.jsonl").write_text('{"seq": 0}\n', encoding="utf-8")

    # Sweeps tree: tuple parquet + sentinel + a REAL 2-entry hash-chained audit.
    (results / "NIFTY" / "5minute").mkdir(parents=True)
    (results / "NIFTY" / "5minute" / "BUY_k1_abcdef.parquet").write_bytes(b"TUPLE-PARQUET")
    (results / "NIFTY" / "5minute" / "BUY_k1_abcdef.parquet.complete.json").write_text(
        "{}", encoding="utf-8"
    )
    with SweepAuditWriter.open(results / "audit" / "sweep-all.jsonl", actor="sweep") as audit:
        audit.append(action="SWEEP_START", subject="sweep:drill", payload={"total": 1})
        audit.append(action="SWEEP_COMPLETE", subject="sweep:drill", payload={"failed": 0})
    assert verify_chain(results / "audit" / "sweep-all.jsonl") == 2

    # Bars — excluded by law; the drill proves restore never touches them.
    (lake / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2026").mkdir(parents=True)
    (lake / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2026" / "07.parquet").write_bytes(
        b"BARS-UNTOUCHED"
    )
    return lake, results


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_restore_drill_end_to_end(real_lake: tuple[Path, Path], tmp_path: Path) -> None:
    lake, results = real_lake
    backup_root = tmp_path / "backups"
    pre_snapshot_tree = _tree_bytes(lake)

    # ── Arm 1: snapshot seals, manifest parses, hashes recomputed test-side.
    result = create_snapshot(
        lake_root=lake,
        results_root=results,
        backup_root=backup_root,
        snapshot_id="drill-snap",
        nightly_id=None,
    )
    snapshot = result.snapshot_path
    assert snapshot.is_dir()
    assert not (backup_root / f"drill-snap{PARTIAL_SUFFIX}").exists()
    manifest = BackupManifest.from_dict(
        json.loads((snapshot / MANIFEST_NAME).read_text(encoding="utf-8"))
    )
    assert manifest.item_count == result.file_count > 0
    for entry in manifest.entries:
        raw = (snapshot / entry.namespace / entry.relpath).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == entry.sha256  # hand-rolled, independent
    assert not any("bars/" in e.relpath for e in manifest.entries)

    # ── Arm 2: mutilate + delete + write one EXTRA post-snapshot delta.
    ledger_file = lake / "window_ledger" / "ledger.parquet"
    flipped = bytearray(ledger_file.read_bytes())
    flipped[10] ^= 0xFF
    ledger_file.write_bytes(bytes(flipped))
    (lake / "registry.duckdb").unlink()
    (results / "NIFTY" / "5minute" / "BUY_k1_abcdef.parquet").unlink()
    extra_delta = lake / "window_ledger" / "deltas" / "99999999999999999999-99999.parquet"
    extra_delta.write_bytes(b"POST-SNAPSHOT-DELTA")

    # ── Arm 3: restore — byte parity, deltas cleared, ledger LAST, chain green.
    copies: list[str] = []

    def recording_copy(source: Path, dest: Path) -> None:
        copies.append(source.relative_to(snapshot).as_posix())
        dest.write_bytes(source.read_bytes())

    plan = plan_restore(snapshot, lake_root=lake, results_root=results)
    outcome = restore_snapshot(plan, copy_file=recording_copy)
    assert outcome.restored == manifest.item_count
    assert outcome.deltas_cleared >= 1
    assert not extra_delta.exists()
    assert outcome.ledger_restored_last is True
    # (c) copy order: every delta precedes the ledger; the ledger is LAST overall.
    delta_positions = [i for i, key in enumerate(copies) if "/deltas/" in key]
    ledger_position = copies.index("lake/window_ledger/ledger.parquet")
    assert delta_positions
    assert all(i < ledger_position for i in delta_positions)
    assert ledger_position == len(copies) - 1
    # (a) byte equality of every restored file vs the manifest.
    for entry in manifest.entries:
        root = lake if entry.namespace == "lake" else results
        restored_raw = (root / entry.relpath).read_bytes()
        assert hashlib.sha256(restored_raw).hexdigest() == entry.sha256
    # (d) the restored audit chain still verifies.
    assert verify_chain(results / "audit" / "sweep-all.jsonl") == 2
    # (e) bars/ untouched, and the whole lake is byte-identical to pre-snapshot.
    assert (
        lake / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2026" / "07.parquet"
    ).read_bytes() == b"BARS-UNTOUCHED"
    assert _tree_bytes(lake) == pre_snapshot_tree


def test_restore_drill_flipped_snapshot_byte_refuses_untouched(
    real_lake: tuple[Path, Path], tmp_path: Path
) -> None:
    lake, results = real_lake
    snapshot = create_snapshot(
        lake_root=lake,
        results_root=results,
        backup_root=tmp_path / "backups",
        snapshot_id="drill-neg",
        nightly_id=None,
    ).snapshot_path
    target = snapshot / "lake" / "registry.duckdb"
    raw = bytearray(target.read_bytes())
    raw[0] ^= 0x01
    target.write_bytes(bytes(raw))
    before = _tree_bytes(lake)
    plan = plan_restore(snapshot, lake_root=lake, results_root=results)
    with pytest.raises(RestoreVerifyError) as excinfo:
        restore_snapshot(plan)
    assert "lake/registry.duckdb (sha256)" in excinfo.value.mismatches
    assert _tree_bytes(lake) == before  # nothing touched


def test_restore_drill_corrupt_manifest_refused(
    real_lake: tuple[Path, Path], tmp_path: Path
) -> None:
    lake, results = real_lake
    snapshot = create_snapshot(
        lake_root=lake,
        results_root=results,
        backup_root=tmp_path / "backups",
        snapshot_id="drill-manifest",
        nightly_id=None,
    ).snapshot_path
    (snapshot / MANIFEST_NAME).write_text("{truncated!", encoding="utf-8")
    with pytest.raises(RestoreRefused, match="is not JSON"):
        verify_snapshot(snapshot)


def test_restore_drill_copy_failure_then_rerun_converges(
    real_lake: tuple[Path, Path], tmp_path: Path
) -> None:
    lake, results = real_lake
    before = _tree_bytes(lake)
    snapshot = create_snapshot(
        lake_root=lake,
        results_root=results,
        backup_root=tmp_path / "backups",
        snapshot_id="drill-crash",
        nightly_id=None,
    ).snapshot_path
    (lake / "registry.duckdb").write_bytes(b"POISON")
    plan = plan_restore(snapshot, lake_root=lake, results_root=results)
    calls = {"n": 0}

    def failing_copy(source: Path, dest: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 4:
            raise OSError(5, "io error at item 4")
        dest.write_bytes(source.read_bytes())

    with pytest.raises(OSError, match="item 4"):
        restore_snapshot(plan, copy_file=failing_copy)
    # Mixed old/new is the documented crash window; the RE-RUN converges.
    restore_snapshot(plan)
    assert _tree_bytes(lake) == before
