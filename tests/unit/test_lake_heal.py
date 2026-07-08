"""The automatic keep-FIRST lake healer (DEC-LAKE-HEAL-001) — 100% of ``lake_heal.py``.

Every lake here is REAL (the #1067 convention): clean partitions via the real
:class:`ParquetWriter` / :class:`OptionParquetWriter`, poison planted with raw
pyarrow / a polars re-render (the fixed writers refuse duplicates), findings
from the REAL scanner :func:`scan_lake_invariants`. Pins, per the design spec:
observe-mode byte-identity, the mass-heal refusal rail, keep-first dedup +
foreign-row reassignment + legacy-dir drift merge + options ``(side,ts)`` dedup,
quarantine preserved + restorable, hash-chained audit lines on a REAL
``AuditWriter``, catalog ``raw_hash`` rotation, ``LakeWriteHalt`` passthrough,
per-finding error isolation, idempotent re-run byte-identity, the derived
recall (1day latest-wins re-derive + TF delete-and-rebuild), the L2/L1 seam
cores (``heal_after_pull`` / ``heal_after_reconcile``), and the L3
content-token startup sweep + ``StartupSweeper`` lifecycle.

Whitebox: drives private helpers directly where the design names them
(the DEC-P0-017 convention), so the file silences ``reportPrivateUsage``.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex.data import lake_heal
from brutex.data.catalog import Catalog
from brutex.data.errors import LakeWriteHalt, WriterError
from brutex.data.lake_heal import (
    AUDIT_HEAL_BATCH,
    AUDIT_HEAL_DEDUP,
    AUDIT_HEAL_DRIFT_MERGE,
    AUDIT_HEAL_QUARANTINE_FILE,
    AUDIT_HEAL_REASSIGN,
    DEFAULT_SWEEP_THROTTLE_S,
    LAKE_HEAL_ENV,
    LAKE_HEAL_THROTTLE_ENV,
    HealReport,
    StartupSweeper,
    heal_after_pull,
    heal_after_reconcile,
    heal_findings,
    lake_content_token,
    load_heal_stats,
    load_last_sweep,
    open_lake_heal_audit,
    resolve_lake_heal_mode,
    resolve_sweep_throttle_s,
    run_startup_sweep,
)
from brutex.data.lake_invariants import (
    ForeignMonthRows,
    IntraFileDup,
    LakeScanReport,
    OptionForeignMonthRows,
    OptionIntraFileDup,
    PartitionCoords,
    scan_lake_invariants,
)
from brutex.data.models import CandleBar, OptionBar
from brutex.data.option_parquet_writer import (
    OptionParquetWriter,
    OptionSeriesCoordinate,
    OptionWriterError,
)
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.partition import build_partition_path
from brutex.data.spot_daily_resample import ist_span_partition_months
from brutex.data.timeframe_materialize import PrebuildKind, materialize_lake_timeframes
from structlog.testing import capture_logs

pytest.importorskip("polars")
pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")


_NOW = datetime(2024, 7, 1, 0, 0, 0, tzinfo=UTC)
_QUARANTINE_TS = "20240701T000000Z"


def _now() -> datetime:
    """The injected batch clock (deterministic quarantine dir + stamps)."""
    return _NOW


def _ts(minute: int, *, day: int = 3, month: int = 6, year: int = 2024) -> datetime:
    """A UTC 1-minute instant (04:xx UTC = 09:3x IST — inside the session)."""
    return datetime(year, month, day, 4, minute, tzinfo=UTC)


def _bar(ts: datetime, *, close: str = "100.00", volume: int = 10) -> CandleBar:
    c = Decimal(close)
    return CandleBar(
        timestamp=ts, open=c, high=c, low=c, close=c, volume=volume, open_interest=None
    )


def _dest(
    lake: Path,
    *,
    exchange: str = "NSE",
    segment: str = "INDEX",
    symbol: str = "NIFTY",
    interval: str = "1minute",
    year: int = 2024,
    month: int = 6,
) -> Path:
    return build_partition_path(
        lake_root=lake,
        exchange=exchange,
        segment=segment,
        symbol=symbol,
        interval=interval,
        year=year,
        month=month,
    )


def _poison(dest: Path, rows: Sequence[tuple[datetime, float, int]]) -> None:
    """Raw-pyarrow plant: duplicate/foreign rows written VERBATIM (#1067)."""
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


def _tree_state(root: Path) -> dict[str, str]:
    """Recursive ``relpath → sha256`` of EVERY file under *root* (byte pin)."""
    out: dict[str, str] = {}
    if not root.exists():
        return out
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        out[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _stored_frame(dest: Path) -> Any:
    import polars as pl

    return pl.read_parquet(dest).sort("timestamp")


def _events(logs: Sequence[Mapping[str, object]], name: str) -> list[Mapping[str, object]]:
    return [e for e in logs if e["event"] == name]


class _FakeAudit:
    """A :class:`LakeAuditSink` capturing every line; can simulate a raise."""

    def __init__(self, *, raise_on_append: bool = False) -> None:
        self.lines: list[tuple[str, str, dict[str, Any] | None]] = []
        self.raise_on_append = raise_on_append

    def append(self, *, action: str, subject: str, payload: dict[str, Any] | None = None) -> object:
        if self.raise_on_append:
            raise RuntimeError("simulated audit failure")
        self.lines.append((action, subject, payload))
        return None

    def actions(self) -> list[str]:
        return [action for action, _subject, _payload in self.lines]


# ── mode + throttle env resolution ─────────────────────────────────────────


class TestModeResolution:
    def test_mode_default_auto(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(LAKE_HEAL_ENV, raising=False)
        assert resolve_lake_heal_mode() == "auto"

    def test_mode_observe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(LAKE_HEAL_ENV, "  ObSeRvE  ")
        assert resolve_lake_heal_mode() == "observe"

    def test_mode_garbage_falls_back_auto(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The tolerant parse fail-safes to AUTO (the deliberate inverse of opt-in)."""
        monkeypatch.setenv(LAKE_HEAL_ENV, "definitely-not-a-mode")
        assert resolve_lake_heal_mode() == "auto"

    def test_mode_explicit_mapping_override(self) -> None:
        assert resolve_lake_heal_mode({LAKE_HEAL_ENV: "observe"}) == "observe"
        assert resolve_lake_heal_mode({LAKE_HEAL_ENV: "auto"}) == "auto"
        assert resolve_lake_heal_mode({}) == "auto"


class TestThrottleResolution:
    def test_throttle_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(LAKE_HEAL_THROTTLE_ENV, raising=False)
        assert resolve_sweep_throttle_s() == DEFAULT_SWEEP_THROTTLE_S

    def test_throttle_garbage_falls_back_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(LAKE_HEAL_THROTTLE_ENV, "not-a-float")
        assert resolve_sweep_throttle_s() == DEFAULT_SWEEP_THROTTLE_S

    def test_throttle_negative_clamps_to_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(LAKE_HEAL_THROTTLE_ENV, "-3.5")
        assert resolve_sweep_throttle_s() == 0.0

    def test_throttle_valid_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(LAKE_HEAL_THROTTLE_ENV, "0.2")
        assert resolve_sweep_throttle_s() == 0.2


# ── observe mode ───────────────────────────────────────────────────────────


class TestObserveMode:
    def test_observe_mode_touches_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Spec-mandated byte pin: observe finds poison, reports it, and
        changes NOT ONE byte anywhere under the lake (no quarantine dir, no
        locks, no stats, no sentinel)."""
        monkeypatch.setenv(LAKE_HEAL_ENV, "observe")
        lake = tmp_path / "lake"
        dest = _dest(lake)
        _poison(dest, [(_ts(0), 100.0, 10), (_ts(0), 999.0, 11)])
        scan = scan_lake_invariants(lake)
        assert len(scan.findings) == 1
        before = _tree_state(lake)
        audit = _FakeAudit()
        reports: list[HealReport] = []
        healed_calls: list[bool] = []
        with capture_logs() as logs:
            report = heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.files_scanned,
                catalog=None,
                audit=audit,
                on_report=reports.append,
                on_healed=lambda: healed_calls.append(True),
                now=_now,
            )
        assert _tree_state(lake) == before  # byte-identical, recursively
        assert report.mode == "observe"
        assert report.findings == 1
        assert report.healed == 0
        assert report.quarantine_root is None
        assert report.recall is None
        assert reports == [report]  # on_report fired once
        assert healed_calls == []  # on_healed NOT fired
        warns = _events(logs, "lake_heal.observe_only")
        assert len(warns) == 1
        assert warns[0]["log_level"] == "warning"
        assert warns[0]["findings"] == 1
        assert audit.actions() == [AUDIT_HEAL_BATCH]
        assert audit.lines[0][2] == {"mode": "observe", "findings": 1, "healed": 0}

    def test_observe_mode_option_foreign_touches_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An option-foreign finding under observe: report + audit + alert,
        ZERO mutation anywhere (whole-tree byte pin) — no quarantine, no move
        (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001)."""
        monkeypatch.setenv(LAKE_HEAL_ENV, "observe")
        lake = tmp_path / "lake"
        _poison_option_foreign(lake)
        scan = scan_lake_invariants(lake)
        (finding,) = scan.findings
        assert isinstance(finding, OptionForeignMonthRows)
        before = _tree_state(lake)
        audit = _FakeAudit()
        reports: list[HealReport] = []
        report = heal_findings(
            scan.findings,
            lake_root=lake,
            files_scanned=scan.option_files_scanned,
            audit=audit,
            on_report=reports.append,
            now=_now,
        )
        assert _tree_state(lake) == before  # byte-identical, recursively
        assert report.mode == "observe"
        assert report.healed == 0
        assert report.quarantine_root is None
        assert audit.actions() == [AUDIT_HEAL_BATCH]
        assert reports == [report]  # the alert seam fired once

    def test_observe_mode_clean_scan_is_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Zero findings in observe: no WARNING, no audit line, on_report fires."""
        monkeypatch.setenv(LAKE_HEAL_ENV, "observe")
        audit = _FakeAudit()
        reports: list[HealReport] = []
        with capture_logs() as logs:
            report = heal_findings(
                (),
                lake_root=Path("/nonexistent"),
                files_scanned=0,
                audit=audit,
                on_report=reports.append,
            )
        assert report.findings == 0
        assert _events(logs, "lake_heal.observe_only") == []
        assert audit.lines == []
        assert reports == [report]


# ── mass-heal refusal rail ─────────────────────────────────────────────────


def _seed_mass_poisoned_option_series(lake: Path) -> Path:
    """21 raw option month files under ONE series, 11 each carrying ONE
    foreign 2019-12 row (> 50% of ≥ ``MASS_HEAL_MIN_FILES`` — the rail
    fixture). Planted raw via pyarrow (#1067 — the real writer refuses
    foreign months). Returns the series dir."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    series = lake / "options" / "SERIES"
    series.mkdir(parents=True)
    for idx in range(21):
        year = 2020 + idx // 12
        month = 1 + idx % 12
        home_ts = datetime(year, month, 3, 4, 0, tzinfo=UTC)
        rows: list[datetime] = [home_ts]
        if idx < 11:  # 11 of 21 carry ONE foreign row
            rows.append(datetime(2019, 12, 2, 4, 0, tzinfo=UTC))
        pq.write_table(
            pa.table(
                {
                    "side": pa.array(["CE"] * len(rows), type=pa.string()),
                    "timestamp": pa.array(rows, type=pa.timestamp("us", tz="UTC")),
                }
            ),
            series / f"{year:04d}-{month:02d}.parquet",
        )
    return series


class TestMassHealRefusal:
    def _synthetic_dup(self, index: int) -> IntraFileDup:
        coords = PartitionCoords(
            exchange="NSE",
            segment="CASH",
            symbol=f"SYM{index}",
            interval="1minute",
            year=2024,
            month=6,
        )
        return IntraFileDup(
            path=Path(f"/nonexistent/{index}.parquet"),
            coords=coords,
            physical_rows=3,
            unique_keys=2,
        )

    def test_mass_heal_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """21 files scanned, 11 poisoned (> 50%) → the WHOLE batch is refused
        untouched (the sweep_heal C7 twin), CRITICAL-flagged on the report."""
        monkeypatch.delenv(LAKE_HEAL_ENV, raising=False)
        findings = tuple(self._synthetic_dup(i) for i in range(11))
        audit = _FakeAudit()
        reports: list[HealReport] = []
        with capture_logs() as logs:
            report = heal_findings(
                findings,
                lake_root=Path("/nonexistent"),
                files_scanned=21,
                audit=audit,
                on_report=reports.append,
                now=_now,
            )
        assert report.refused_mass_heal is True
        assert report.healed == 0
        assert report.actions == ()
        assert report.quarantine_root is None
        errors = _events(logs, "lake_heal.mass_heal_refused")
        assert len(errors) == 1
        assert errors[0]["log_level"] == "error"
        assert errors[0]["affected_files"] == 11
        assert errors[0]["files_scanned"] == 21
        assert audit.actions() == [AUDIT_HEAL_BATCH]
        payload = audit.lines[0][2]
        assert payload is not None
        assert payload["refused_mass_heal"] is True
        assert reports == [report]

    def test_small_lake_never_trips_the_rail(self, tmp_path: Path) -> None:
        """Below MASS_HEAL_MIN_FILES the fraction is irrelevant — a 1-of-1
        poisoned tiny lake heals normally."""
        lake = tmp_path / "lake"
        dest = _dest(lake)
        _poison(dest, [(_ts(0), 100.0, 10), (_ts(0), 999.0, 11)])
        scan = scan_lake_invariants(lake)
        report = heal_findings(
            scan.findings,
            lake_root=lake,
            files_scanned=scan.files_scanned,
            mode="auto",
            recall=False,
            now=_now,
        )
        assert report.refused_mass_heal is False
        assert report.healed == 1

    def test_mass_heal_refused_over_option_foreign_files(self, tmp_path: Path) -> None:
        """The rail counts OPTION files end-to-end: 21 scanned option files,
        11 flagged foreign (> 50%) ⇒ the WHOLE batch refused byte-untouched
        (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001 — option files ride BOTH the
        numerator and the denominator)."""
        lake = tmp_path / "lake"
        _seed_mass_poisoned_option_series(lake)
        scan = scan_lake_invariants(lake)
        assert scan.option_files_scanned == 21
        assert len(scan.findings) == 11
        before = _tree_state(lake)
        report = heal_findings(
            scan.findings,
            lake_root=lake,
            files_scanned=scan.files_scanned + scan.option_files_scanned,
            mode="auto",
            recall=False,
            now=_now,
        )
        assert report.refused_mass_heal is True
        assert report.healed == 0
        assert _tree_state(lake) == before  # not one byte moved


# ── the dedup heal, end-to-end (audit + catalog + stats + recall) ──────────


class TestHealBatchDedupEndToEnd:
    def test_heal_batch_dedup_end_to_end(self, tmp_path: Path) -> None:
        """FAILS on unfixed main (API absent): a settled poisoned month is
        healed with NO new bars — keep-first winner, quarantine copy
        restorable, ONE hash-chained audit line, catalog raw_hash rotated,
        stats bumped, on_healed fired once, and the derived 1day recalled."""
        from brutex.orchestrator.audit import read_entries, verify_chain

        lake = tmp_path / "lake"
        dest = _dest(lake)
        _poison(dest, [(_ts(0), 100.0, 10), (_ts(0), 999.0, 11), (_ts(1), 101.0, 12)])
        original_bytes = dest.read_bytes()
        scan = scan_lake_invariants(lake)
        assert len(scan.findings) == 1
        audit = open_lake_heal_audit(lake)
        assert audit is not None
        healed_calls: list[bool] = []
        reports: list[HealReport] = []
        with Catalog.open(lake / "catalog.duckdb") as catalog:
            report = heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.files_scanned,
                catalog=catalog,
                audit=audit,
                on_report=reports.append,
                on_healed=lambda: healed_calls.append(True),
                now=_now,
            )
            row = catalog.get(
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                year=2024,
                month=6,
            )
        close = getattr(audit, "close", None)
        assert callable(close)
        close()
        # ── the heal itself: keep-FIRST winner, no new bars ──
        assert report.healed == 1
        assert report.skipped == 0
        assert report.errors == 0
        stored = _stored_frame(dest)
        assert stored["timestamp"].to_list() == [_ts(0), _ts(1)]
        assert stored["close"].to_list() == [100.0, 101.0]  # first physical row won
        # ── quarantine: original preserved + restorable ──
        quarantine = (
            lake / ".quarantine" / _QUARANTINE_TS / "bars/NSE/INDEX/NIFTY/1minute/2024/06.parquet"
        )
        assert quarantine.read_bytes() == original_bytes
        assert report.quarantine_root == str(lake / ".quarantine" / _QUARANTINE_TS)
        # ── report action shape ──
        (action,) = report.actions
        assert action.action == AUDIT_HEAL_DEDUP
        assert action.partition == "bars/NSE/INDEX/NIFTY/1minute/2024/06.parquet"
        assert action.kept == 2
        assert action.dropped == 1
        assert action.raw_hash_after == hashlib.sha256(dest.read_bytes()).hexdigest()
        # ── hash-chained audit: ONE heal line, chain verifies ──
        audit_path = lake / "audit" / "lake-heal.jsonl"
        assert verify_chain(audit_path) == 1
        (entry,) = read_entries(audit_path)
        assert entry.action == AUDIT_HEAL_DEDUP
        assert entry.subject == "bars/NSE/INDEX/NIFTY/1minute/2024/06.parquet"
        # ── catalog: rotated raw_hash reaches data_snapshot_hash inputs ──
        assert row is not None
        assert row.raw_hash == action.raw_hash_after
        assert row.row_count == 2
        # ── stats sidecar ──
        stats = load_heal_stats(lake)
        assert stats["healed_partitions_total"] == 1
        assert stats["quarantined_files_total"] == 1
        assert stats["last_heal_utc"] == _NOW.isoformat()
        # ── seams ──
        assert healed_calls == [True]
        assert reports == [report]
        # ── derived recall: the 1day was re-derived from the healed 1minute ──
        assert report.recall is not None
        assert report.recall.daily_targets == 1
        assert report.recall.daily_bars == 1
        daily = _stored_frame(_dest(lake, interval="1day"))
        assert daily["timestamp"].to_list() == [datetime(2024, 6, 2, 18, 30, tzinfo=UTC)]
        assert daily["open"].to_list() == [100.0]
        assert daily["high"].to_list() == [101.0]
        assert daily["close"].to_list() == [101.0]
        assert daily["volume"].to_list() == [22]

    def test_skipped_findings_vanished_and_already_clean(self, tmp_path: Path) -> None:
        """A vanished path and an already-clean file are SKIPPED (idempotent
        convergence), never errored."""
        lake = tmp_path / "lake"
        clean = _dest(lake)
        ParquetWriter(lake_root=lake).write(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=[_bar(_ts(0))],
        )
        coords = PartitionCoords(
            exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", year=2024, month=6
        )
        stale = IntraFileDup(path=clean, coords=coords, physical_rows=2, unique_keys=1)
        vanished = IntraFileDup(
            path=lake / "bars/NSE/INDEX/NIFTY/1minute/2024/07.parquet",
            coords=PartitionCoords(
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                year=2024,
                month=7,
            ),
            physical_rows=2,
            unique_keys=1,
        )
        report = heal_findings(
            (stale, vanished),
            lake_root=lake,
            files_scanned=2,
            mode="auto",
            recall=False,
            now=_now,
        )
        assert report.healed == 0
        assert report.skipped == 2
        assert report.errors == 0
        assert report.quarantine_root is None  # no quarantine dir for a no-op batch
        assert not (lake / ".quarantine").exists()

    def test_empty_findings_auto_batch_is_a_quiet_noop(self, tmp_path: Path) -> None:
        reports: list[HealReport] = []
        with capture_logs() as logs:
            report = heal_findings(
                (),
                lake_root=tmp_path,
                files_scanned=0,
                mode="auto",
                on_report=reports.append,
                now=_now,
            )
        assert report.findings == 0
        assert _events(logs, "lake_heal.batch_start") == []
        assert reports == [report]
        assert not (tmp_path / ".quarantine").exists()

    def test_audit_append_failure_never_fails_the_heal(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        dest = _dest(lake)
        _poison(dest, [(_ts(0), 100.0, 10), (_ts(0), 999.0, 11)])
        scan = scan_lake_invariants(lake)
        with capture_logs() as logs:
            report = heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.files_scanned,
                mode="auto",
                audit=_FakeAudit(raise_on_append=True),
                recall=False,
                now=_now,
            )
        assert report.healed == 1
        failures = _events(logs, "lake_heal.audit_append_failed")
        assert len(failures) == 1
        assert failures[0]["log_level"] == "warning"


# ── foreign-month reassignment ─────────────────────────────────────────────


class TestForeignRowReassignment:
    def _seed_reassign_lake(self, lake: Path) -> tuple[Path, Path]:
        """May canonical (ts1 stored=100.25) + a June file carrying two MAY
        rows: ts1 (conflicting value 999) and ts2 (missing from May)."""
        writer = ParquetWriter(lake_root=lake)
        writer.write(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=[_bar(_ts(1, month=5), close="100.25")],
        )
        june = _dest(lake, month=6)
        _poison(
            june,
            [
                (_ts(1, month=5), 999.0, 11),  # overlap + value conflict (canonical must WIN)
                (_ts(2, month=5), 55.0, 7),  # pure misplacement (moved home)
                (_ts(0, month=6), 101.0, 12),  # a legitimate June row (kept)
            ],
        )
        return _dest(lake, month=5), june

    def test_reassign_moves_foreign_rows_to_canonical(self, tmp_path: Path) -> None:
        """Cross-file overlap reassignment: the canonical file's stored row
        WINS (BarConflict counted), the misplaced row moves home, the source
        is rewritten without them, the original is quarantined."""
        lake = tmp_path / "lake"
        may, june = self._seed_reassign_lake(lake)
        june_original = june.read_bytes()
        scan = scan_lake_invariants(lake)
        (finding,) = scan.findings
        assert isinstance(finding, ForeignMonthRows)
        with capture_logs() as logs:
            report = heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.files_scanned,
                mode="auto",
                recall=False,
                now=_now,
            )
        assert report.healed == 1
        may_frame = _stored_frame(may)
        assert may_frame["timestamp"].to_list() == [_ts(1, month=5), _ts(2, month=5)]
        assert may_frame["close"].to_list() == [100.25, 55.0]  # canonical KEPT its value
        june_frame = _stored_frame(june)
        assert june_frame["timestamp"].to_list() == [_ts(0, month=6)]
        quarantine = (
            lake / ".quarantine" / _QUARANTINE_TS / "bars/NSE/INDEX/NIFTY/1minute/2024/06.parquet"
        )
        assert quarantine.read_bytes() == june_original
        (action,) = report.actions
        assert action.action == AUDIT_HEAL_REASSIGN
        assert action.moved == 2
        assert action.conflicts == 1  # the canonical-wins restatement
        assert action.dropped == 2  # both foreign rows removed from the source
        reassigned = _events(logs, "lake_heal.rows_reassigned")
        assert len(reassigned) == 1
        assert reassigned[0]["moved"] == 2
        assert reassigned[0]["months"] == ["2024-05"]
        assert finding.path == june

    def test_reassign_two_destination_months(self, tmp_path: Path) -> None:
        """The bars twin of the options two-destination-month pin (the
        pre-existing single-month test gap): one June file carrying strays to
        BOTH May and July — every month group is written home (a grouping-loop
        truncation mutant loses every month after the first, silently)."""
        lake = tmp_path / "lake"
        june = _dest(lake, month=6)
        _poison(
            june,
            [
                (_ts(1, month=5), 55.0, 7),  # May stray
                (_ts(2, month=7), 77.0, 9),  # July stray
                (_ts(0, month=6), 101.0, 12),  # a legitimate June row (kept)
            ],
        )
        scan = scan_lake_invariants(lake)
        (finding,) = scan.findings
        assert isinstance(finding, ForeignMonthRows)
        assert finding.foreign_rows == 2
        with capture_logs() as logs:
            report = heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.files_scanned,
                mode="auto",
                recall=False,
                now=_now,
            )
        assert report.healed == 1
        assert report.errors == 0
        may_frame = _stored_frame(_dest(lake, month=5))
        assert may_frame["timestamp"].to_list() == [_ts(1, month=5)]
        assert may_frame["close"].to_list() == [55.0]
        july_frame = _stored_frame(_dest(lake, month=7))
        assert july_frame["timestamp"].to_list() == [_ts(2, month=7)]
        assert july_frame["close"].to_list() == [77.0]
        assert _stored_frame(june)["timestamp"].to_list() == [_ts(0, month=6)]
        reassigned = _events(logs, "lake_heal.rows_reassigned")
        assert reassigned[0]["months"] == ["2024-05", "2024-07"]
        assert reassigned[0]["moved"] == 2
        assert scan_lake_invariants(lake).findings == ()  # nothing lost, nothing left

    def test_reassign_crash_between_copy_and_remove_converges(self, tmp_path: Path) -> None:
        """Idempotent convergence: a crash AFTER copy-forward but BEFORE the
        removal leaves the rows in BOTH files; the re-run cleans the source and
        leaves the canonical unchanged — bars/ byte-identical to the one-shot
        heal on a twin lake."""
        one_shot = tmp_path / "one-shot"
        crashed = tmp_path / "crashed"
        self._seed_reassign_lake(one_shot)
        self._seed_reassign_lake(crashed)
        # One-shot heal on the reference lake.
        scan_a = scan_lake_invariants(one_shot)
        heal_findings(
            scan_a.findings,
            lake_root=one_shot,
            files_scanned=scan_a.files_scanned,
            mode="auto",
            recall=False,
            now=_now,
        )
        # Simulate the crash on the twin: copy-forward ALREADY happened.
        ParquetWriter(lake_root=crashed).write(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=[_bar(_ts(2, month=5), close="55.00", volume=7)],
        )
        # The re-run (the next scan re-finds the foreign rows in the source).
        scan_b = scan_lake_invariants(crashed)
        assert len(scan_b.findings) == 1
        report = heal_findings(
            scan_b.findings,
            lake_root=crashed,
            files_scanned=scan_b.files_scanned,
            mode="auto",
            recall=False,
            now=_now,
        )
        assert report.healed == 1
        assert _tree_state(one_shot / "bars") == _tree_state(crashed / "bars")

    def test_wholly_foreign_file_is_quarantine_moved_and_catalog_row_deleted(
        self, tmp_path: Path
    ) -> None:
        """A file whose EVERY row belongs to another month is quarantine-MOVED
        away; its dangling catalog row is deleted (never a get_bars crash)."""
        from brutex.data.models import CatalogEntry

        lake = tmp_path / "lake"
        writer = ParquetWriter(lake_root=lake)
        writer.write(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=[_bar(_ts(1, month=5), close="100.25")],
        )
        june = _dest(lake, month=6)
        _poison(june, [(_ts(1, month=5), 100.25, 10), (_ts(2, month=5), 55.0, 7)])
        scan = scan_lake_invariants(lake)
        with Catalog.open(lake / "catalog.duckdb") as catalog:
            catalog.upsert(
                CatalogEntry(
                    exchange="NSE",
                    segment="INDEX",
                    symbol="NIFTY",
                    interval="1minute",
                    year=2024,
                    month=6,
                    raw_hash="0" * 64,
                    row_count=2,
                    first_ts=_ts(1, month=5),
                    last_ts=_ts(2, month=5),
                    fetched_at=_NOW,
                )
            )
            report = heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.files_scanned,
                mode="auto",
                catalog=catalog,
                recall=False,
                now=_now,
            )
            gone = catalog.get(
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                year=2024,
                month=6,
            )
            may_row = catalog.get(
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                year=2024,
                month=5,
            )
        assert report.healed == 1
        assert not june.exists()
        assert june.parent.exists()  # the <yyyy>/ dir survives — May still lives in it
        assert gone is None  # the dangling June row was deleted
        assert may_row is not None  # the canonical write upserted May
        may_frame = _stored_frame(_dest(lake, month=5))
        assert may_frame["timestamp"].to_list() == [_ts(1, month=5), _ts(2, month=5)]

    def test_stale_foreign_finding_is_skipped(self, tmp_path: Path) -> None:
        """A finding whose file no longer carries foreign rows is a skip."""
        lake = tmp_path / "lake"
        writer = ParquetWriter(lake_root=lake)
        write = writer.write(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=[_bar(_ts(0))],
        )
        coords = PartitionCoords(
            exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", year=2024, month=6
        )
        stale = ForeignMonthRows(path=write.path, coords=coords, foreign_rows=1, overlap_rows=0)
        vanished = ForeignMonthRows(
            path=lake / "bars/NSE/INDEX/NIFTY/1minute/2024/07.parquet",
            coords=PartitionCoords(
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                year=2024,
                month=7,
            ),
            foreign_rows=1,
            overlap_rows=0,
        )
        report = heal_findings(
            (stale, vanished),
            lake_root=lake,
            files_scanned=2,
            mode="auto",
            recall=False,
            now=_now,
        )
        assert report.healed == 0
        assert report.skipped == 2

    def test_reassign_probe_abstain_does_not_count_audit_or_touch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``changed=False`` heal_partition (probe abstain) on the removal
        step is a SKIP — no quarantine counter, no ``LAKE_HEAL_REASSIGN``
        audit line, no recall touch (mirrors ``_heal_dup``'s guard) — and the
        finding stays LIVE for the next scan (convergent re-try). Fails
        before the ``result.changed`` guard was added to ``_heal_foreign``.
        """
        from brutex.data.parquet_writer import PartitionHealResult

        lake = tmp_path / "lake"
        _may, june = self._seed_reassign_lake(lake)
        scan = scan_lake_invariants(lake)
        (finding,) = scan.findings
        assert isinstance(finding, ForeignMonthRows)

        def _abstain(self: ParquetWriter, dest: Path, **_kw: Any) -> PartitionHealResult:
            # The footer-probe-failure path: byte-untouched abstain (#1067 pin).
            return PartitionHealResult(
                path=dest,
                changed=False,
                kept=0,
                dropped_duplicates=0,
                dropped_removed=0,
                raw_hash_before="0" * 64,
                raw_hash_after="0" * 64,
                write=None,
            )

        monkeypatch.setattr(ParquetWriter, "heal_partition", _abstain)
        audit = _FakeAudit()
        with capture_logs() as logs:
            report = heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.files_scanned,
                mode="auto",
                audit=audit,
                recall=True,
                now=_now,
            )
        # Counted a SKIP, never a heal — and the counter/audit/touch stayed silent.
        assert report.healed == 0
        assert report.skipped == 1
        assert report.errors == 0
        assert report.actions == ()
        assert report.quarantine_root is None  # quarantined_files never bumped
        assert report.recall is None  # no 1minute touch → no recall
        assert AUDIT_HEAL_REASSIGN not in audit.actions()
        assert _events(logs, "lake_heal.rows_reassigned") == []
        assert load_heal_stats(lake) == {}  # healed == 0 → no stats bump
        # The source rows were NOT removed → the finding is re-detected live.
        rescan = scan_lake_invariants(lake)
        assert any(isinstance(f, ForeignMonthRows) and f.path == june for f in rescan.findings)


# ── legacy-key drift merge ─────────────────────────────────────────────────


def _write_spot(
    lake: Path,
    bars: list[CandleBar],
    *,
    exchange: str = "NSE",
    segment: str = "INDEX",
    symbol: str = "NIFTY",
    interval: str = "1minute",
) -> Path:
    return (
        ParquetWriter(lake_root=lake)
        .write(exchange=exchange, segment=segment, symbol=symbol, interval=interval, bars=bars)
        .path
    )


def _write_empty_parquet(dest: Path) -> None:
    """A VALID 0-row bars-schema parquet (the empty-legacy-file edge)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "timestamp": pa.array([], type=pa.timestamp("us", tz="UTC")),
            "open": pa.array([], type=pa.float64()),
            "high": pa.array([], type=pa.float64()),
            "low": pa.array([], type=pa.float64()),
            "close": pa.array([], type=pa.float64()),
            "volume": pa.array([], type=pa.int64()),
            "open_interest": pa.array([], type=pa.int64()),
        }
    )
    pq.write_table(table, dest)


class TestDriftMerge:
    def test_drift_merge_keep_first_into_canonical(self, tmp_path: Path) -> None:
        """The legacy ``NSE-M&M`` dir merges into ``M&M``: canonical stored
        rows WIN, the legacy 1day is merged, the derived-TF dir is quarantine-
        MOVED (never merged), the legacy dirs are pruned, the legacy catalog
        rows are deleted, ONE ``LAKE_HEAL_DRIFT_MERGE`` audit line."""
        from brutex.data.models import CatalogEntry

        lake = tmp_path / "lake"
        # Canonical M&M: ts0 close 100 (the value that must WIN).
        _write_spot(lake, [_bar(_ts(0), close="100.00")], segment="CASH", symbol="M&M")
        legacy = lake / "bars" / "NSE" / "CASH" / "NSE-M&M"
        # Legacy 1minute: a conflicting ts0 (999) + a new ts5 (77).
        _poison(
            legacy / "1minute" / "2024" / "06.parquet", [(_ts(0), 999.0, 11), (_ts(5), 77.0, 9)]
        )
        # Legacy DERIVED 5minute dir — must be MOVED, never merged.
        _poison(legacy / "5minute" / "2024" / "06.parquet", [(_ts(0), 999.0, 20)])
        # Legacy 1day — vendor-authoritative on CLI lakes: MERGED.
        daily_ts = datetime(2024, 6, 2, 18, 30, tzinfo=UTC)
        _poison(legacy / "1day" / "2024" / "06.parquet", [(daily_ts, 105.0, 21)])
        # A 0-row junk file (non-month stem): moved, no merge, no catalog key.
        _write_empty_parquet(legacy / "1minute" / "2024" / "junk.parquet")
        audit = _FakeAudit()
        scan = scan_lake_invariants(lake)
        drift = [f for f in scan.findings if type(f).__name__ == "LegacyKeyDrift"]
        assert len(drift) == 1
        with Catalog.open(lake / "catalog.duckdb") as catalog:
            for interval in ("1minute", "5minute", "1day"):
                catalog.upsert(
                    CatalogEntry(
                        exchange="NSE",
                        segment="CASH",
                        symbol="NSE-M&M",
                        interval=interval,
                        year=2024,
                        month=6,
                        raw_hash="0" * 64,
                        row_count=1,
                        first_ts=_ts(0),
                        last_ts=_ts(5),
                        fetched_at=_NOW,
                    )
                )
            with capture_logs() as logs:
                report = heal_findings(
                    scan.findings,
                    lake_root=lake,
                    files_scanned=scan.files_scanned,
                    mode="auto",
                    catalog=catalog,
                    audit=audit,
                    recall=False,
                    now=_now,
                )
            legacy_rows = [
                catalog.get(
                    exchange="NSE",
                    segment="CASH",
                    symbol="NSE-M&M",
                    interval=interval,
                    year=2024,
                    month=6,
                )
                for interval in ("1minute", "5minute", "1day")
            ]
            canonical_minute = catalog.get(
                exchange="NSE",
                segment="CASH",
                symbol="M&M",
                interval="1minute",
                year=2024,
                month=6,
            )
        assert report.healed == 1
        # Canonical 1minute: keep-first — ts0 kept 100.0, ts5 merged in.
        merged = _stored_frame(_dest(lake, segment="CASH", symbol="M&M"))
        assert merged["timestamp"].to_list() == [_ts(0), _ts(5)]
        assert merged["close"].to_list() == [100.0, 77.0]
        # Canonical 1day gained the legacy daily bar.
        daily = _stored_frame(_dest(lake, segment="CASH", symbol="M&M", interval="1day"))
        assert daily["timestamp"].to_list() == [daily_ts]
        # The legacy dir is GONE; every legacy file quarantined under its relpath.
        assert not legacy.exists()
        qroot = lake / ".quarantine" / _QUARANTINE_TS / "bars" / "NSE" / "CASH" / "NSE-M&M"
        assert (qroot / "1minute" / "2024" / "06.parquet").exists()
        assert (qroot / "1minute" / "2024" / "junk.parquet").exists()
        assert (qroot / "5minute" / "2024" / "06.parquet").exists()
        assert (qroot / "1day" / "2024" / "06.parquet").exists()
        # The derived 5minute was NOT merged into the canonical dir.
        assert not (lake / "bars" / "NSE" / "CASH" / "M&M" / "5minute").exists()
        # Legacy catalog rows deleted; the canonical write upserted.
        assert legacy_rows == [None, None, None]
        assert canonical_minute is not None
        # Audit: one QUARANTINE_FILE per moved file + ONE DRIFT_MERGE.
        assert audit.actions().count(AUDIT_HEAL_QUARANTINE_FILE) == 4
        assert audit.actions().count(AUDIT_HEAL_DRIFT_MERGE) == 1
        drift_line = next(line for line in audit.lines if line[0] == AUDIT_HEAL_DRIFT_MERGE)
        assert drift_line[1] == "NSE/CASH/NSE-M&M"
        payload = drift_line[2]
        assert payload is not None
        assert payload["canonical"] == "M&M"
        assert payload["files_by_interval"] == {"1minute": 2, "5minute": 1, "1day": 1}
        assert payload["rows_merged"] == 2  # ts5 + the daily bar (the conflict added 0)
        assert payload["conflicts"] == 1
        merged_logs = _events(logs, "lake_heal.drift_merged")
        assert len(merged_logs) == 1
        assert merged_logs[0]["drifted"] == "'NSE-M&M'"  # repr()'d, the #907 convention
        assert merged_logs[0]["canonical"] == "'M&M'"
        (action,) = report.actions
        assert action.action == AUDIT_HEAL_DRIFT_MERGE
        assert action.moved == 2
        assert action.conflicts == 1

    def test_drift_finding_for_vanished_dir_is_skipped(self, tmp_path: Path) -> None:
        from brutex.data.lake_invariants import LegacyKeyDrift

        lake = tmp_path / "lake"
        finding = LegacyKeyDrift(
            exchange="NSE",
            segment="CASH",
            drifted_symbol="NSE-M&M",
            canonical_symbol="M&M",
            drifted_dir=lake / "bars" / "NSE" / "CASH" / "NSE-M&M",
            intervals=("1minute",),
            files=1,
        )
        report = heal_findings(
            (finding,), lake_root=lake, files_scanned=1, mode="auto", recall=False, now=_now
        )
        assert report.skipped == 1
        assert report.healed == 0


# ── options-tree dup heal ──────────────────────────────────────────────────


_OPT_COORD = OptionSeriesCoordinate(
    exch_id="NSE",
    exchange_segment="NSE_FNO",
    security_id=13,
    underlying_symbol="NIFTY",
    expiry_flag="WEEK",
    expiry_code=0,
    strike_offset="ATM",
)


def _option_bar(*, side: str = "CE", minute: int = 0) -> OptionBar:
    return OptionBar(
        timestamp=datetime(2021, 1, 4, 4, minute, tzinfo=UTC),
        open=Decimal("100.00"),
        high=Decimal("110.10"),
        low=Decimal("90.00"),
        close=Decimal("105.00"),
        volume=250,
        open_interest=1200,
        side=side,
        strike=Decimal("17500.00"),
        spot=Decimal("17480.57"),
        iv=Decimal("0.1523"),
    )


def _poison_option_lake(lake: Path) -> Path:
    """A real options partition with a value-drifted LATER duplicate row."""
    import polars as pl

    OptionParquetWriter(lake_root=lake).write(
        partition_key="NSE_FNO|13|WEEK|0|ATM|CALL",
        coordinate=_OPT_COORD,
        bars=[_option_bar(side="CE"), _option_bar(side="PE")],
    )
    (dest,) = (lake / "options").rglob("*.parquet")
    df = pl.read_parquet(dest)
    drifted = df[0:1].with_columns((pl.col("close") + 999.0).alias("close"))
    pl.concat([df, drifted]).write_parquet(dest)
    return dest


class TestOptionDupHeal:
    def test_option_dup_heal(self, tmp_path: Path) -> None:
        """The options tree file is deduped ``(side, ts)`` keep-first with the
        quarantine copy kept; audit payload carries ``tree="options"``."""
        import polars as pl

        lake = tmp_path / "lake"
        dest = _poison_option_lake(lake)
        original = dest.read_bytes()
        audit = _FakeAudit()
        scan = scan_lake_invariants(lake)
        (finding,) = scan.findings
        assert isinstance(finding, OptionIntraFileDup)
        with capture_logs() as logs:
            report = heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.files_scanned + scan.option_files_scanned,
                mode="auto",
                audit=audit,
                recall=False,
                now=_now,
            )
        assert report.healed == 1
        (healed_log,) = _events(logs, "lake_heal.partition_healed")
        # repr() — the log token embeds the RAW series dir name (round-7 NIT, #907).
        assert healed_log["partition"] == repr(f"options/{dest.parent.name}/{dest.name}")
        healed = pl.read_parquet(dest)
        assert healed.height == 2
        assert healed.filter(pl.col("side") == "CE")["close"].to_list() == [105.0]  # first won
        quarantine = lake / ".quarantine" / _QUARANTINE_TS / dest.relative_to(lake)
        assert quarantine.read_bytes() == original
        (action,) = report.actions
        assert action.action == AUDIT_HEAL_DEDUP
        assert action.partition.startswith("options/")
        payload = audit.lines[0][2]
        assert payload is not None
        assert payload["tree"] == "options"
        assert report.recall is None  # nothing derives from options/

    def test_option_findings_vanished_or_clean_are_skipped(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        OptionParquetWriter(lake_root=lake).write(
            partition_key="NSE_FNO|13|WEEK|0|ATM|CALL",
            coordinate=_OPT_COORD,
            bars=[_option_bar(side="CE")],
        )
        (clean,) = (lake / "options").rglob("*.parquet")
        stale = OptionIntraFileDup(
            path=clean, series=clean.parent.name, physical_rows=2, unique_keys=1
        )
        vanished = OptionIntraFileDup(
            path=clean.parent / "2099-01.parquet",
            series=clean.parent.name,
            physical_rows=2,
            unique_keys=1,
        )
        report = heal_findings(
            (stale, vanished), lake_root=lake, files_scanned=2, mode="auto", recall=False, now=_now
        )
        assert report.skipped == 2
        assert report.healed == 0


# ── options-tree foreign-month reassignment (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001) ──


_OPT_SERIES = "NSE_FNO__13__WEEK__0__ATM__CALL"


def _opt_bar(*, side: str = "CE", ts: datetime, close: str = "105.00") -> OptionBar:
    c = Decimal(close)  # a flat bar keeps the OHLC-ordering validator happy
    return OptionBar(
        timestamp=ts,
        open=c,
        high=c,
        low=c,
        close=c,
        volume=250,
        open_interest=1200,
        side=side,
        strike=Decimal("17500.00"),
        spot=Decimal("17480.57"),
        iv=Decimal("0.1523"),
    )


def _opt_series_dir(lake: Path, *, partition_key: str = "NSE_FNO|13|WEEK|0|ATM|CALL") -> Path:
    return lake / "options" / partition_key.replace("|", "__")


def _poison_option_foreign(
    lake: Path, *, partition_key: str = "NSE_FNO|13|WEEK|0|ATM|CALL"
) -> tuple[Path, Path]:
    """Canonical July + June seeded through the REAL writer, June raw-poisoned.

    June then carries (a) an OVERLAP copy of the July CE key with a DRIFTED
    close (canonical must win by REMOVAL, value asserted unchanged) and (b) a
    pure-misplaced July PE row absent from canonical. Returns (june, july).
    """
    import polars as pl

    writer = OptionParquetWriter(lake_root=lake)
    writer.write(
        partition_key=partition_key,
        coordinate=_OPT_COORD,
        bars=[_opt_bar(side="CE", ts=_ts(1, month=7, day=1), close="200.00")],
    )
    writer.write(
        partition_key=partition_key,
        coordinate=_OPT_COORD,
        bars=[_opt_bar(side="CE", ts=_ts(0), close="100.00")],
    )
    series_dir = _opt_series_dir(lake, partition_key=partition_key)
    june = series_dir / "2024-06.parquet"
    july = series_dir / "2024-07.parquet"
    # glob=False — a round-15 fixture key may carry glob metachars (X[12]).
    june_df = pl.read_parquet(june, glob=False)
    july_df = pl.read_parquet(july, glob=False)
    drifted_overlap = july_df[0:1].with_columns((pl.col("close") + 999.0).alias("close"))
    misplaced = july_df[0:1].with_columns(pl.lit("PE").alias("side"), pl.lit(55.0).alias("close"))
    pl.concat([june_df, drifted_overlap, misplaced]).write_parquet(june)
    return june, july


class TestOptionForeignHeal:
    def test_option_reassign_moves_foreign_rows_home(self, tmp_path: Path) -> None:
        """The end-to-end pin: overlap copy REMOVED (canonical value wins,
        drifted close never restated), misplaced row REASSIGNED, source
        cleaned, quarantine copy restorable, audit + log + stats exact."""
        import polars as pl

        lake = tmp_path / "lake"
        june, july = _poison_option_foreign(lake)
        june_original = june.read_bytes()
        audit = _FakeAudit()
        scan = scan_lake_invariants(lake)
        (finding,) = scan.findings
        assert isinstance(finding, OptionForeignMonthRows)
        assert finding.foreign_rows == 2
        assert finding.overlap_rows == 1
        with capture_logs() as logs:
            report = heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.files_scanned + scan.option_files_scanned,
                mode="auto",
                audit=audit,
                recall=True,
                now=_now,
            )
        assert report.healed == 1
        assert report.errors == 0
        assert report.recall is None  # NOTHING derives from options/
        july_frame = pl.read_parquet(july).sort(["side", "timestamp"])
        # BOTH keys home with CANONICAL values — the drifted overlap copy was
        # REMOVED, never restated (keep-first at the destination).
        assert july_frame["side"].to_list() == ["CE", "PE"]
        assert july_frame["close"].to_list() == [200.00, 55.0]
        june_frame = pl.read_parquet(june)
        assert june_frame["side"].to_list() == ["CE"]
        assert june_frame["timestamp"].to_list() == [_ts(0)]
        quarantine = lake / ".quarantine" / _QUARANTINE_TS / june.relative_to(lake)
        assert quarantine.read_bytes() == june_original
        (action,) = report.actions
        assert action.action == AUDIT_HEAL_REASSIGN
        assert action.partition == f"options/{_OPT_SERIES}/2024-06.parquet"
        assert action.moved == 2
        assert action.conflicts == 0
        assert action.kept == 1
        assert action.dropped == 2
        assert action.raw_hash_before is None  # uncatalogued tree
        assert action.raw_hash_after is None
        (line,) = audit.lines
        assert line[0] == AUDIT_HEAL_REASSIGN
        assert line[1] == f"options/{_OPT_SERIES}/2024-06.parquet"
        payload = line[2]
        assert payload is not None
        assert payload["tree"] == "options"
        assert payload["moved"] == 2
        assert payload["added"] == 1  # the overlap copy added 0 (canonical won)
        assert payload["months"] == ["2024-07"]
        assert payload["kept"] == 1
        assert payload["dropped_duplicates"] == 0
        assert payload["dropped_removed"] == 2
        reassigned = _events(logs, "lake_heal.rows_reassigned")
        assert len(reassigned) == 1
        assert reassigned[0]["tree"] == "options"
        # repr() — the log token embeds the RAW series dir name (round-7 NIT, #907);
        # the HealAction/audit copies above stay raw (JSON-encoded machine surfaces).
        assert reassigned[0]["partition"] == repr(f"options/{_OPT_SERIES}/2024-06.parquet")
        assert reassigned[0]["series"] == repr(_OPT_SERIES)  # raw fs name — #907 repr()
        assert reassigned[0]["moved"] == 2
        assert reassigned[0]["added"] == 1
        assert reassigned[0]["months"] == ["2024-07"]
        assert reassigned[0]["conflicts"] == 0
        stats = load_heal_stats(lake)
        assert stats["healed_partitions_total"] == 1
        assert stats["quarantined_files_total"] == 1

    def test_option_reassign_creates_missing_canonical_sibling(self, tmp_path: Path) -> None:
        """No July file exists — the moved row CREATES it through the writer path."""
        import polars as pl

        lake = tmp_path / "lake"
        writer = OptionParquetWriter(lake_root=lake)
        writer.write(
            partition_key="NSE_FNO|13|WEEK|0|ATM|CALL",
            coordinate=_OPT_COORD,
            bars=[_opt_bar(side="CE", ts=_ts(0), close="100.00")],
        )
        series_dir = _opt_series_dir(lake)
        june = series_dir / "2024-06.parquet"
        june_df = pl.read_parquet(june)
        misplaced = june_df[0:1].with_columns(
            pl.lit(_ts(2, month=7, day=1)).dt.replace_time_zone("UTC").alias("timestamp"),
            pl.lit(55.0).alias("close"),
        )
        pl.concat([june_df, misplaced]).write_parquet(june)
        scan = scan_lake_invariants(lake)
        (finding,) = scan.findings
        assert isinstance(finding, OptionForeignMonthRows)
        assert finding.overlap_rows == 0  # missing sibling — pure misplacement
        report = heal_findings(
            scan.findings,
            lake_root=lake,
            files_scanned=scan.option_files_scanned,
            mode="auto",
            recall=False,
            now=_now,
        )
        assert report.healed == 1
        july = series_dir / "2024-07.parquet"
        assert july.exists()
        july_frame = pl.read_parquet(july)
        assert july_frame["timestamp"].to_list() == [_ts(2, month=7, day=1)]
        assert july_frame["close"].to_list() == [55.0]
        assert pl.read_parquet(june)["timestamp"].to_list() == [_ts(0)]

    def test_option_reassign_into_dup_poisoned_canonical_lands_key(self, tmp_path: Path) -> None:
        """BLOCKER regression (hostile review, EMPIRICALLY reproduced pre-fix):
        a dup-POISONED canonical destination must never cancel the moved key.
        Pre-fix, ``merge_partition_frame`` baselined ``added`` on RAW physical
        rows, so July's dup collapse cancelled the genuinely-new June key —
        ``added == 0`` skipped the rewrite, the source removal still ran, and
        the key existed in NEITHER lake file (silent, permanent, invisible to
        a second sweep). Post-fix: the key LANDS, July self-heals its dup on
        touch (``lake.partition.dedup_healed``), second sweep clean."""
        import polars as pl

        lake = tmp_path / "lake"
        writer = OptionParquetWriter(lake_root=lake)
        writer.write(
            partition_key="NSE_FNO|13|WEEK|0|ATM|CALL",
            coordinate=_OPT_COORD,
            bars=[_opt_bar(side="CE", ts=_ts(1, month=7, day=1), close="200.00")],
        )
        writer.write(
            partition_key="NSE_FNO|13|WEEK|0|ATM|CALL",
            coordinate=_OPT_COORD,
            bars=[_opt_bar(side="CE", ts=_ts(0), close="100.00")],
        )
        series_dir = _opt_series_dir(lake)
        june = series_dir / "2024-06.parquet"
        july = series_dir / "2024-07.parquet"
        # Poison the CANONICAL July with a physical dup pair of its own row —
        # exactly the OptionIntraFileDup state this healer exists to fix.
        july_df = pl.read_parquet(july)
        pl.concat([july_df, july_df[0:1]]).write_parquet(july)
        # June carries ONE pure-misplaced July key ABSENT from canonical July.
        june_df = pl.read_parquet(june)
        misplaced = june_df[0:1].with_columns(
            pl.lit(_ts(2, month=7, day=2)).dt.replace_time_zone("UTC").alias("timestamp"),
            pl.lit("PE").alias("side"),
            pl.lit(55.0).alias("close"),
        )
        pl.concat([june_df, misplaced]).write_parquet(june)
        scan = scan_lake_invariants(lake)
        assert {type(f).__name__ for f in scan.findings} == {
            "OptionForeignMonthRows",
            "OptionIntraFileDup",
        }
        with capture_logs() as logs:
            report = heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.option_files_scanned,
                mode="auto",
                recall=False,
                now=_now,
            )
        assert report.errors == 0
        assert report.healed == 1  # the foreign reassignment
        assert report.skipped == 1  # July's dup already collapsed by the merge touch
        july_after = pl.read_parquet(july).sort(["side", "timestamp"])
        # THE pin: the moved key is PRESENT at its canonical destination.
        assert july_after["side"].to_list() == ["CE", "PE"]
        assert july_after["timestamp"].to_list() == [_ts(1, month=7, day=1), _ts(2, month=7, day=2)]
        assert july_after["close"].to_list() == [200.0, 55.0]
        assert pl.read_parquet(june)["timestamp"].to_list() == [_ts(0)]  # source cleaned
        healed_logs = _events(logs, "lake.partition.dedup_healed")
        assert len(healed_logs) == 1  # the destination collapse is observable
        assert healed_logs[0]["kept"] == 1
        assert healed_logs[0]["dropped"] == 1
        assert scan_lake_invariants(lake).findings == ()  # second sweep clean — no loss

    def test_option_reassign_two_destination_months(self, tmp_path: Path) -> None:
        """MAJOR regression (mutation gap): ONE June source carrying strays to
        BOTH May and July — every destination-month group must be copied
        forward (a ``sorted(set(routes))[:1]`` truncation mutant silently
        loses every month after the first while the source removal still runs
        and a second sweep stays clean)."""
        import polars as pl

        lake = tmp_path / "lake"
        writer = OptionParquetWriter(lake_root=lake)
        writer.write(
            partition_key="NSE_FNO|13|WEEK|0|ATM|CALL",
            coordinate=_OPT_COORD,
            bars=[_opt_bar(side="CE", ts=_ts(0), close="100.00")],
        )
        series_dir = _opt_series_dir(lake)
        june = series_dir / "2024-06.parquet"
        june_df = pl.read_parquet(june)
        may_stray = june_df[0:1].with_columns(
            pl.lit(_ts(1, month=5, day=2)).dt.replace_time_zone("UTC").alias("timestamp"),
            pl.lit(66.0).alias("close"),
        )
        july_stray = june_df[0:1].with_columns(
            pl.lit(_ts(2, month=7, day=2)).dt.replace_time_zone("UTC").alias("timestamp"),
            pl.lit(77.0).alias("close"),
        )
        pl.concat([june_df, may_stray, july_stray]).write_parquet(june)
        audit = _FakeAudit()
        scan = scan_lake_invariants(lake)
        (finding,) = scan.findings
        assert isinstance(finding, OptionForeignMonthRows)
        assert finding.foreign_rows == 2
        with capture_logs() as logs:
            report = heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.option_files_scanned,
                mode="auto",
                audit=audit,
                recall=False,
                now=_now,
            )
        assert report.healed == 1
        assert report.errors == 0
        may_frame = pl.read_parquet(series_dir / "2024-05.parquet")
        assert may_frame["timestamp"].to_list() == [_ts(1, month=5, day=2)]
        assert may_frame["close"].to_list() == [66.0]
        july_frame = pl.read_parquet(series_dir / "2024-07.parquet")
        assert july_frame["timestamp"].to_list() == [_ts(2, month=7, day=2)]
        assert july_frame["close"].to_list() == [77.0]
        assert pl.read_parquet(june)["timestamp"].to_list() == [_ts(0)]  # home row only
        (line,) = audit.lines
        payload = line[2]
        assert payload is not None
        assert payload["months"] == ["2024-05", "2024-07"]  # BOTH months, sorted
        assert payload["moved"] == 2
        assert payload["added"] == 2
        reassigned = _events(logs, "lake_heal.rows_reassigned")
        assert reassigned[0]["months"] == ["2024-05", "2024-07"]
        assert reassigned[0]["moved"] == 2
        assert scan_lake_invariants(lake).findings == ()  # nothing lost, nothing left

    def test_option_reassign_months_sorted_not_insertion_order(self, tmp_path: Path) -> None:
        """Round-4 NIT (surviving-mutant kill): the two-destination-month test
        above plants May physically BEFORE July, so the ``rows_by_month`` dict
        insertion order already equals sorted order and a
        ``sorted(rows_by_month)`` → ``list(rows_by_month)`` mutant survived on
        fixture-order luck. Here the July stray comes physically FIRST — the
        audit + log ``months`` list must still be ASCENDING (deterministic,
        order-independent; CLAUDE.md §2 Rule 7)."""
        import polars as pl

        lake = tmp_path / "lake"
        writer = OptionParquetWriter(lake_root=lake)
        writer.write(
            partition_key="NSE_FNO|13|WEEK|0|ATM|CALL",
            coordinate=_OPT_COORD,
            bars=[_opt_bar(side="CE", ts=_ts(0), close="100.00")],
        )
        series_dir = _opt_series_dir(lake)
        june = series_dir / "2024-06.parquet"
        june_df = pl.read_parquet(june)
        july_stray = june_df[0:1].with_columns(
            pl.lit(_ts(2, month=7, day=2)).dt.replace_time_zone("UTC").alias("timestamp"),
            pl.lit(77.0).alias("close"),
        )
        may_stray = june_df[0:1].with_columns(
            pl.lit(_ts(1, month=5, day=2)).dt.replace_time_zone("UTC").alias("timestamp"),
            pl.lit(66.0).alias("close"),
        )
        pl.concat([june_df, july_stray, may_stray]).write_parquet(june)  # July FIRST
        audit = _FakeAudit()
        scan = scan_lake_invariants(lake)
        (finding,) = scan.findings
        assert isinstance(finding, OptionForeignMonthRows)
        with capture_logs() as logs:
            report = heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.option_files_scanned,
                mode="auto",
                audit=audit,
                recall=False,
                now=_now,
            )
        assert report.healed == 1
        assert report.errors == 0
        (line,) = audit.lines
        payload = line[2]
        assert payload is not None
        assert payload["months"] == ["2024-05", "2024-07"]  # SORTED, not insertion order
        reassigned = _events(logs, "lake_heal.rows_reassigned")
        assert reassigned[0]["months"] == ["2024-05", "2024-07"]
        assert pl.read_parquet(series_dir / "2024-05.parquet")["close"].to_list() == [66.0]
        assert pl.read_parquet(series_dir / "2024-07.parquet")["close"].to_list() == [77.0]
        assert scan_lake_invariants(lake).findings == ()

    def test_option_wholly_foreign_file_quarantine_moved(self, tmp_path: Path) -> None:
        """Every row foreign ⇒ source unlinked, rows home, kept=0; the series
        dir is NOT pruned — the destination lives in it (the writer-level
        prune arm is defensive, unit-tested at the writer)."""
        import polars as pl

        lake = tmp_path / "lake"
        writer = OptionParquetWriter(lake_root=lake)
        writer.write(
            partition_key="NSE_FNO|13|WEEK|0|ATM|CALL",
            coordinate=_OPT_COORD,
            bars=[
                _opt_bar(side="CE", ts=_ts(0), close="100.00"),
                _opt_bar(side="PE", ts=_ts(1), close="101.00"),
            ],
        )
        series_dir = _opt_series_dir(lake)
        june = series_dir / "2024-06.parquet"
        df = pl.read_parquet(june)
        shifted = df.with_columns((pl.col("timestamp") + pl.duration(days=30)).alias("timestamp"))
        shifted.write_parquet(june)
        scan = scan_lake_invariants(lake)
        (finding,) = scan.findings
        assert isinstance(finding, OptionForeignMonthRows)
        assert finding.foreign_rows == 2
        report = heal_findings(
            scan.findings,
            lake_root=lake,
            files_scanned=scan.option_files_scanned,
            mode="auto",
            recall=False,
            now=_now,
        )
        assert report.healed == 1
        assert not june.exists()  # quarantine-MOVED away
        assert series_dir.exists()  # the July destination lives in it
        july = series_dir / "2024-07.parquet"
        assert pl.read_parquet(july).height == 2
        (action,) = report.actions
        assert action.kept == 0
        quarantine = lake / ".quarantine" / _QUARANTINE_TS / june.relative_to(lake)
        assert quarantine.exists()

    def test_option_reassign_crash_between_copy_and_remove_converges(self, tmp_path: Path) -> None:
        """Twin-lake convergence: a crash AFTER copy-forward re-runs clean."""
        import polars as pl

        one_shot = tmp_path / "one-shot"
        crashed = tmp_path / "crashed"
        _poison_option_foreign(one_shot)
        june_crashed, july_crashed = _poison_option_foreign(crashed)
        # Simulate the crash on the twin: copy-forward ALREADY happened.
        june_frame = pl.read_parquet(june_crashed)
        foreign_rows = june_frame.filter(pl.col("timestamp") >= _ts(0, month=7, day=1))
        OptionParquetWriter(lake_root=crashed).merge_partition_frame(july_crashed, foreign_rows)
        for lake in (one_shot, crashed):
            scan = scan_lake_invariants(lake)
            assert len(scan.findings) == 1
            report = heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.option_files_scanned,
                mode="auto",
                recall=False,
                now=_now,
            )
            assert report.healed == 1
        assert _tree_state(one_shot / "options") == _tree_state(crashed / "options")

    def test_same_key_two_non_canonical_files(self, tmp_path: Path) -> None:
        """G6: the same foreign key in TWO non-canonical files — the
        path-ordered-FIRST copy wins at the destination, both sources
        cleaned, a second sweep finds NOTHING (deterministic convergence)."""
        import polars as pl

        lake = tmp_path / "lake"
        writer = OptionParquetWriter(lake_root=lake)
        writer.write(
            partition_key="NSE_FNO|13|WEEK|0|ATM|CALL",
            coordinate=_OPT_COORD,
            bars=[_opt_bar(side="CE", ts=_ts(0), close="100.00")],
        )
        writer.write(
            partition_key="NSE_FNO|13|WEEK|0|ATM|CALL",
            coordinate=_OPT_COORD,
            bars=[_opt_bar(side="CE", ts=_ts(0, month=8, day=1), close="102.00")],
        )
        series_dir = _opt_series_dir(lake)
        june = series_dir / "2024-06.parquet"
        august = series_dir / "2024-08.parquet"
        may_key_ts = _ts(3, month=5, day=1)
        for path, close in ((june, 111.0), (august, 222.0)):
            df = pl.read_parquet(path)
            stray = df[0:1].with_columns(
                pl.lit(may_key_ts).dt.replace_time_zone("UTC").alias("timestamp"),
                pl.lit(close).alias("close"),
            )
            pl.concat([df, stray]).write_parquet(path)
        scan = scan_lake_invariants(lake)
        assert len(scan.findings) == 2
        report = heal_findings(
            scan.findings,
            lake_root=lake,
            files_scanned=scan.option_files_scanned,
            mode="auto",
            recall=False,
            now=_now,
        )
        assert report.healed == 2
        may = series_dir / "2024-05.parquet"
        may_frame = pl.read_parquet(may)
        assert may_frame["timestamp"].to_list() == [may_key_ts]
        assert may_frame["close"].to_list() == [111.0]  # path-ordered FIRST (June) won
        assert pl.read_parquet(june)["timestamp"].to_list() == [_ts(0)]
        assert pl.read_parquet(august)["timestamp"].to_list() == [_ts(0, month=8, day=1)]
        rescan = scan_lake_invariants(lake)
        assert rescan.findings == ()  # a second sweep finds NOTHING

    def test_option_foreign_vanished_or_stale_skipped(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        writer = OptionParquetWriter(lake_root=lake)
        writer.write(
            partition_key="NSE_FNO|13|WEEK|0|ATM|CALL",
            coordinate=_OPT_COORD,
            bars=[_opt_bar(side="CE", ts=_ts(0), close="100.00")],
        )
        series_dir = _opt_series_dir(lake)
        clean = series_dir / "2024-06.parquet"
        stale = OptionForeignMonthRows(
            path=clean, series=_OPT_SERIES, year=2024, month=6, foreign_rows=1, overlap_rows=0
        )
        vanished = OptionForeignMonthRows(
            path=series_dir / "2099-01.parquet",
            series=_OPT_SERIES,
            year=2099,
            month=1,
            foreign_rows=1,
            overlap_rows=0,
        )
        report = heal_findings(
            (stale, vanished), lake_root=lake, files_scanned=2, mode="auto", recall=False, now=_now
        )
        assert report.healed == 0
        assert report.skipped == 2
        assert report.errors == 0

    def test_option_foreign_stale_zero_row_file_skipped(self, tmp_path: Path) -> None:
        """Round-3 MAJOR regression: a source truncated to ZERO rows (schema
        preserved) in the scan→heal window is a graceful SKIP — pre-fix the
        empty Python mask made ``pl.Series([])`` dtype ``Null`` and
        ``filter`` raised ``TypeError`` (NOT in ``_PER_FINDING_HEAL_ERRORS``),
        aborting the WHOLE batch."""
        import polars as pl

        lake = tmp_path / "lake"
        june, _july = _poison_option_foreign(lake)
        scan = scan_lake_invariants(lake)
        (finding,) = scan.findings
        assert isinstance(finding, OptionForeignMonthRows)
        # The threat-model window: out-of-band truncation AFTER the scan.
        pl.read_parquet(june).head(0).write_parquet(june)
        truncated_bytes = june.read_bytes()
        report = heal_findings(
            scan.findings,
            lake_root=lake,
            files_scanned=scan.option_files_scanned,
            mode="auto",
            recall=False,
            now=_now,
        )
        assert report.healed == 0
        assert report.skipped == 1  # the stale finding — never an abort
        assert report.errors == 0
        assert report.actions == ()
        assert report.quarantine_root is None
        assert june.read_bytes() == truncated_bytes  # read-only skip

    def test_option_foreign_then_dup_same_file_single_quarantine(self, tmp_path: Path) -> None:
        """G7: one file carries BOTH findings — foreign heals first (its
        rewrite fully dedups), the dup heal reads changed=False, and exactly
        ONE quarantine copy exists whose bytes == the ORIGINAL pre-heal."""
        import polars as pl

        lake = tmp_path / "lake"
        writer = OptionParquetWriter(lake_root=lake)
        writer.write(
            partition_key="NSE_FNO|13|WEEK|0|ATM|CALL",
            coordinate=_OPT_COORD,
            bars=[_opt_bar(side="CE", ts=_ts(0), close="100.00")],
        )
        june = _opt_series_dir(lake) / "2024-06.parquet"
        df = pl.read_parquet(june)
        dup_copy = df[0:1].with_columns((pl.col("close") + 999.0).alias("close"))
        misplaced = df[0:1].with_columns(
            pl.lit(_ts(2, month=7, day=1)).dt.replace_time_zone("UTC").alias("timestamp"),
            pl.lit("PE").alias("side"),
        )
        pl.concat([df, dup_copy, misplaced]).write_parquet(june)
        original = june.read_bytes()
        scan = scan_lake_invariants(lake)
        assert {type(f).__name__ for f in scan.findings} == {
            "OptionIntraFileDup",
            "OptionForeignMonthRows",
        }
        report = heal_findings(
            scan.findings,
            lake_root=lake,
            files_scanned=scan.option_files_scanned,
            mode="auto",
            recall=False,
            now=_now,
        )
        assert report.healed == 1
        assert report.skipped == 1  # the dup finding read changed=False
        assert [a.action for a in report.actions] == [AUDIT_HEAL_REASSIGN]
        quarantined = list((lake / ".quarantine").rglob("*.parquet"))
        assert len(quarantined) == 1  # exactly ONE copy — never a second write
        assert quarantined[0].read_bytes() == original
        healed = pl.read_parquet(june)
        assert healed.height == 1  # dup collapsed by the SAME rewrite
        assert healed["close"].to_list() == [100.0]  # first physical row won

    def test_option_foreign_duplicated_key_first_physical_copy_wins(self, tmp_path: Path) -> None:
        """Round-7 MINOR (surviving-mutant kill): a PHYSICALLY DUPLICATED
        foreign ``(side, ts)`` key with DIVERGENT values moves ONCE and the
        FIRST physical copy's values win — the ``_option_foreign_frame``
        ``keep="first"`` law. A ``keep="last"`` mutant reassigns the LAST
        copy's OHLCV instead (a silent nondeterministic-winner drift on
        hand-planted poison) and survived every prior test because no fixture
        planted the dup on a FOREIGN key: G7's dup rides the HOME key, whose
        collapse is ``heal_partition``'s own ``unique()``, not this frame."""
        import polars as pl

        lake = tmp_path / "lake"
        writer = OptionParquetWriter(lake_root=lake)
        writer.write(
            partition_key="NSE_FNO|13|WEEK|0|ATM|CALL",
            coordinate=_OPT_COORD,
            bars=[_opt_bar(side="CE", ts=_ts(0), close="100.00")],
        )
        june = _opt_series_dir(lake) / "2024-06.parquet"
        df = pl.read_parquet(june)
        july_ts = _ts(2, month=7, day=1)
        # The SAME July (CE, ts) key TWICE with divergent closes: 111.0 FIRST,
        # then 222.0 (raw plant — the real writer refuses dups, #1067).
        first_copy = df[0:1].with_columns(
            pl.lit(july_ts).dt.replace_time_zone("UTC").alias("timestamp"),
            pl.lit(111.0).alias("close"),
        )
        second_copy = first_copy.with_columns(pl.lit(222.0).alias("close"))
        pl.concat([df, first_copy, second_copy]).write_parquet(june)
        original = june.read_bytes()
        scan = scan_lake_invariants(lake)
        assert {type(f).__name__ for f in scan.findings} == {
            "OptionForeignMonthRows",
            "OptionIntraFileDup",
        }
        report = heal_findings(
            scan.findings,
            lake_root=lake,
            files_scanned=scan.option_files_scanned,
            mode="auto",
            recall=False,
            now=_now,
        )
        assert report.healed == 1  # the reassignment
        assert report.skipped == 1  # the same-file dup finding read changed=False
        assert report.errors == 0
        july = _opt_series_dir(lake) / "2024-07.parquet"
        july_frame = pl.read_parquet(july)
        assert july_frame["side"].to_list() == ["CE"]
        assert july_frame["timestamp"].to_list() == [july_ts]  # the key moved ONCE
        assert july_frame["close"].to_list() == [111.0]  # FIRST physical copy won
        assert pl.read_parquet(june)["timestamp"].to_list() == [_ts(0)]  # source cleaned
        (quarantined,) = (lake / ".quarantine").rglob("*.parquet")
        assert quarantined.read_bytes() == original  # BOTH physical copies preserved
        assert pl.read_parquet(quarantined)["close"].to_list() == [100.0, 111.0, 222.0]
        assert scan_lake_invariants(lake).findings == ()  # convergent — second sweep clean

    def test_option_foreign_heal_changed_false_guard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The defensive convergence guard: a changed=False removal is a SKIP —
        not counted, not audited, no quarantine tally."""
        from brutex.data.option_parquet_writer import OptionHealResult

        lake = tmp_path / "lake"
        _poison_option_foreign(lake)
        scan = scan_lake_invariants(lake)

        def _abstain(self: OptionParquetWriter, dest: Path, **_kw: Any) -> OptionHealResult:
            return OptionHealResult(
                path=dest,
                changed=False,
                kept=3,
                dropped_duplicates=0,
                quarantined_original=None,
                dropped_removed=0,
            )

        monkeypatch.setattr(OptionParquetWriter, "heal_partition", _abstain)
        audit = _FakeAudit()
        with capture_logs() as logs:
            report = heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.option_files_scanned,
                mode="auto",
                audit=audit,
                recall=False,
                now=_now,
            )
        assert report.healed == 0
        assert report.skipped == 1
        assert report.errors == 0
        assert report.actions == ()
        assert report.quarantine_root is None
        assert AUDIT_HEAL_REASSIGN not in audit.actions()
        assert _events(logs, "lake_heal.rows_reassigned") == []
        assert load_heal_stats(lake) == {}

    def test_option_foreign_error_isolated(self, tmp_path: Path) -> None:
        """A corrupt source errors in ISOLATION; a sibling finding still heals."""
        lake = tmp_path / "lake"
        _poison_option_lake(lake)  # a healable dup in the CALL series (single file)
        june, _july = _poison_option_foreign(lake, partition_key="NSE_FNO|13|WEEK|0|ATM|PUT")
        scan = scan_lake_invariants(lake)
        assert len(scan.findings) == 2
        june.write_bytes(b"not a parquet file at all")  # corrupt AFTER the scan
        with capture_logs() as logs:
            report = heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.option_files_scanned,
                mode="auto",
                recall=False,
                now=_now,
            )
        assert report.errors == 1
        assert report.healed == 1  # the option-dup sibling still healed
        errors = _events(logs, "lake_heal.partition_error")
        assert len(errors) == 1
        assert errors[0]["finding"] == "OptionForeignMonthRows"

    def test_option_partition_error_token_repr_guarded(self, tmp_path: Path) -> None:
        """Round-9 fold regression: the option-finding token rides ``repr()``.

        ``OptionParquetWriter._safe_segment`` rejects only ``/ \\ .. |`` and
        does NOT strip control characters, and the series key derives from
        vendor symbol data — so a series dir named with an embedded newline /
        ANSI escape must not forge a fake line in a rendered structlog stream.
        ``lake_heal.partition_error`` is precisely the attacker-triggerable
        site (containment refusal / corrupt partition / lock timeout); the
        observe-mode ``detail`` list rides the SAME ``_finding_token`` guard
        (the #907 convention, matching the round-8 option-tree fold).

        Round-16 NIT extension: the ``error=`` payload rides ``repr()`` too —
        ``OptionWriterError`` messages interpolate the SAME raw dest path
        (``corrupt existing partition <dest>: …``), so pre-fix the raw
        newline/ESC re-entered the site AROUND the round-9 token guard."""
        lake = tmp_path / "lake"
        # A bare ESC (\x1bM, ANSI Reverse Index) — not \x1b[ — kept
        # bracket-free so THIS test exercises ONLY the log-injection guard:
        # since round 15 every raw-path option read passes glob=False (a
        # bracket name now reads its literal file, no longer a glob), but a
        # second metachar dimension here would blur what this pin covers —
        # the glob-metachar reads have their own round-15 regressions.
        evil_key = "NSE_FNO|13|WEEK\nFORGED log line \x1bM|CALL"
        june, _july = _poison_option_foreign(lake, partition_key=evil_key)
        scan = scan_lake_invariants(lake)
        (finding,) = scan.findings
        assert isinstance(finding, OptionForeignMonthRows)
        with capture_logs() as observe_logs:
            heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.option_files_scanned,
                mode="observe",
                now=_now,
            )
        (observe,) = _events(observe_logs, "lake_heal.observe_only")
        detail = observe["detail"]
        assert isinstance(detail, list)
        assert detail == [repr(str(june))]  # repr()'d — quotes + escapes visible
        assert "\n" not in detail[0]
        assert "\x1b" not in detail[0]
        june.write_bytes(b"not a parquet file at all")  # corrupt AFTER the scan
        with capture_logs() as logs:
            report = heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.option_files_scanned,
                mode="auto",
                recall=False,
                now=_now,
            )
        assert report.errors == 1
        (error,) = _events(logs, "lake_heal.partition_error")
        assert error["finding"] == "OptionForeignMonthRows"
        partition = error["partition"]
        assert isinstance(partition, str)
        assert partition == repr(str(june))  # the raw path would embed \n + ESC
        assert "\n" not in partition
        assert "\x1b" not in partition
        # Round-16: the exception TEXT embeds the same vendor-derived path —
        # the error= field is repr()'d, so no raw control chars ride through.
        error_text = error["error"]
        assert isinstance(error_text, str)
        assert error_text.startswith(("'", '"'))  # repr shape, escapes visible
        assert "corrupt existing partition" in error_text
        assert "\n" not in error_text
        assert "\x1b" not in error_text

    def test_option_foreign_action_dropped_sums_dups_and_removals(self, tmp_path: Path) -> None:
        """Round-10 pin (surviving mutant): the reassignment ``HealAction.dropped``
        is EXACTLY ``dropped_duplicates + dropped_removed`` — the value
        ``emit_lake_heal_alert`` sums (``dropped=sum(action.dropped …)``), so a
        zeroed/one-component mutant under-reports dropped rows on the operator
        alert. ONE source carries a physically-DUPLICATED home key (dup
        collapse: 1) + a reassignable foreign row AND an overlap foreign row
        (keyed removals: 2) → ``dropped == 3``, no covering test asserted the
        dup component before this pin."""
        import polars as pl

        lake = tmp_path / "lake"
        june, _july = _poison_option_foreign(lake)
        june_df = pl.read_parquet(june)
        home_dup = june_df.filter(pl.col("timestamp") == _ts(0))[0:1].with_columns(
            (pl.col("close") + 1.0).alias("close")
        )
        pl.concat([june_df, home_dup]).write_parquet(june)
        scan = scan_lake_invariants(lake)
        kinds = {type(f).__name__ for f in scan.findings}
        assert kinds == {"OptionForeignMonthRows", "OptionIntraFileDup"}
        report = heal_findings(
            scan.findings,
            lake_root=lake,
            files_scanned=scan.option_files_scanned,
            mode="auto",
            recall=False,
            now=_now,
        )
        assert report.healed == 1  # the foreign reassignment (heals first)
        assert report.skipped == 1  # the same-file dup reads changed=False after
        assert report.errors == 0
        (action,) = report.actions
        assert action.action == AUDIT_HEAL_REASSIGN
        assert action.moved == 2
        assert action.kept == 1
        assert action.dropped == 3  # 1 home-dup collapse + 2 keyed removals
        # The exact aggregation the batch alert computes over the report
        # (notifications.sweep_alerts.emit_lake_heal_alert):
        assert sum(a.dropped for a in report.actions) == 3

    def test_option_nested_dtype_swap_isolated_batch_continues(self, tmp_path: Path) -> None:
        """Round-12 fold: a scan→heal swap to a nested-key-dtype file is an
        ISOLATED :class:`OptionWriterError` refusal, never a batch abort.

        Pre-fix, the flagged source swapped to a ``List(Utf8)``-side file made
        the removal-key build (``frozenset(zip(sides, stamps))``) raise
        ``TypeError: unhashable type`` — NOT in the per-finding isolation set,
        so the WHOLE heal batch aborted (later findings unhealed, no
        report/stats/alert — the round-3/round-5 isolation-escape class)."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        lake = tmp_path / "lake"
        _poison_option_lake(lake)  # a healable dup in the CALL series
        june, july = _poison_option_foreign(lake, partition_key="NSE_FNO|13|WEEK|0|ATM|PUT")
        scan = scan_lake_invariants(lake)
        assert len(scan.findings) == 2
        # Swap the flagged source to a nested-side-dtype file AFTER the scan.
        pq.write_table(
            pa.table(
                {
                    "side": pa.array([["CE"], ["CE"]], type=pa.list_(pa.string())),
                    "timestamp": pa.array(
                        [_ts(1), _ts(1, month=7, day=1)], type=pa.timestamp("us", tz="UTC")
                    ),
                }
            ),
            june,
        )
        swapped = june.read_bytes()
        july_before = july.read_bytes()
        reports: list[HealReport] = []
        with capture_logs() as logs:
            report = heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.option_files_scanned,
                mode="auto",
                recall=False,
                on_report=reports.append,
                now=_now,
            )
        assert report.errors == 1  # pre-fix: TypeError aborts the batch here
        assert report.healed == 1  # the CALL-series dup sibling still heals
        assert reports == [report]  # the alert seam still fires post-batch
        (error,) = _events(logs, "lake_heal.partition_error")
        assert error["finding"] == "OptionForeignMonthRows"
        assert june.read_bytes() == swapped  # refused byte-untouched
        assert july.read_bytes() == july_before  # nothing merged/imported home
        assert not (lake / ".quarantine" / _QUARANTINE_TS / june.relative_to(lake)).exists()

    def test_option_nested_dtype_swap_mid_heal_isolated_batch_continues(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Round-13 fold (refuter-confirmed MAJOR): a nested-dtype swap landing
        AFTER the healer's gated consistent read — between the copy-forward
        merge legs and ``heal_partition`` — is STILL the isolated refusal.

        The round-12 gate covered only the FIRST read;
        ``OptionParquetWriter.heal_partition`` RE-reads the source under its
        own locks and hashed the re-read stored keys ungated, so a
        ``List(Utf8)``-side swap in that second, WIDER window raised bare
        ``TypeError: unhashable type`` — NOT in the per-finding isolation set
        — aborting the WHOLE batch (no report/stats/alert, the later
        same-batch dup unhealed; the round-3/round-5/round-12
        isolation-escape class). The swap is injected via a
        ``merge_partition_frame`` spy that rewrites the SOURCE before
        delegating — exactly the mid-heal window."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        lake = tmp_path / "lake"
        _poison_option_lake(lake)  # a healable dup in the CALL series (heals LATER)
        june, _july = _poison_option_foreign(lake, partition_key="NSE_FNO|13|WEEK|0|ATM|PUT")
        scan = scan_lake_invariants(lake)
        assert len(scan.findings) == 2
        nested = pa.table(
            {
                "side": pa.array([["CE"], ["CE"]], type=pa.list_(pa.string())),
                "timestamp": pa.array(
                    [_ts(1), _ts(1, month=7, day=1)], type=pa.timestamp("us", tz="UTC")
                ),
            }
        )
        real_merge = OptionParquetWriter.merge_partition_frame
        swapped: list[bytes] = []

        def _swap_then_merge(self: OptionParquetWriter, dest: Path, frame: Any) -> int:
            if not swapped:  # swap the SOURCE after the gated consistent read
                pq.write_table(nested, june)
                swapped.append(june.read_bytes())
            return real_merge(self, dest, frame)

        monkeypatch.setattr(OptionParquetWriter, "merge_partition_frame", _swap_then_merge)
        reports: list[HealReport] = []
        with capture_logs() as logs:
            report = heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.option_files_scanned,
                mode="auto",
                recall=False,
                on_report=reports.append,
                now=_now,
            )
        assert swapped  # the spy really fired inside the foreign heal
        assert report.errors == 1  # pre-fix: bare TypeError aborted the batch HERE
        assert report.healed == 1  # the CALL-series dup — a LATER finding — still heals
        assert reports == [report]  # the alert seam still fires post-batch
        (error,) = _events(logs, "lake_heal.partition_error")
        assert error["finding"] == "OptionForeignMonthRows"
        assert june.read_bytes() == swapped[0]  # the swapped source refused byte-untouched
        assert not (lake / ".quarantine" / _QUARANTINE_TS / june.relative_to(lake)).exists()

    @pytest.mark.parametrize("missing", ["side", "timestamp"])
    def test_option_missing_key_column_swap_isolated_batch_continues(
        self, tmp_path: Path, missing: str
    ) -> None:
        """Round-14 fold (refuter-confirmed MAJOR): a scan→heal swap to a valid
        parquet MISSING a key column is the ISOLATED refusal, never a batch abort.

        The round-12 dtype gate was not total: ``option_key_dtype_error`` did
        ``frame.schema["side"]`` / ``["timestamp"]`` — a bare ``KeyError`` on
        an ABSENT column, in neither ``_PER_FINDING_HEAL_ERRORS`` nor
        ``LakeWriteHalt``, so the healer's consistent-read gate (the ONE site
        fed a FULL ``read_partition_frame`` read with no column selection)
        aborted the WHOLE batch (later findings unhealed, no
        report/stats/alert/AUDIT_HEAL_BATCH — the round-3/5/12/13
        isolation-escape class the gate itself was meant to close)."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        lake = tmp_path / "lake"
        _poison_option_lake(lake)  # a healable dup in the CALL series
        june, july = _poison_option_foreign(lake, partition_key="NSE_FNO|13|WEEK|0|ATM|PUT")
        scan = scan_lake_invariants(lake)
        assert len(scan.findings) == 2
        # Swap the flagged source to a valid parquet LACKING one key column
        # AFTER the scan (a bars-style timestamp-only file / a side-only file).
        if missing == "side":
            table = pa.table({"timestamp": pa.array([_ts(1)], type=pa.timestamp("us", tz="UTC"))})
        else:
            table = pa.table({"side": pa.array(["CE"], type=pa.string())})
        pq.write_table(table, june)
        swapped = june.read_bytes()
        july_before = july.read_bytes()
        reports: list[HealReport] = []
        with capture_logs() as logs:
            report = heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.option_files_scanned,
                mode="auto",
                recall=False,
                on_report=reports.append,
                now=_now,
            )
        assert report.errors == 1  # pre-fix: bare KeyError aborts the batch here
        assert report.healed == 1  # the CALL-series dup sibling still heals
        assert reports == [report]  # the alert seam still fires post-batch
        (error,) = _events(logs, "lake_heal.partition_error")
        assert error["finding"] == "OptionForeignMonthRows"
        assert missing in str(error["error"])  # the verdict names the column
        assert june.read_bytes() == swapped  # refused byte-untouched
        assert july.read_bytes() == july_before  # nothing merged/imported home
        assert not (lake / ".quarantine" / _QUARANTINE_TS / june.relative_to(lake)).exists()

    def test_lake_write_halt_propagates_from_option_merge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lake = tmp_path / "lake"
        _poison_option_foreign(lake)
        scan = scan_lake_invariants(lake)

        def _halt(self: OptionParquetWriter, dest: Path, frame: Any) -> int:
            raise LakeWriteHalt("disk_full")

        monkeypatch.setattr(OptionParquetWriter, "merge_partition_frame", _halt)
        with pytest.raises(LakeWriteHalt):
            heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.option_files_scanned,
                mode="auto",
                recall=False,
                now=_now,
            )

    def test_option_naive_timestamp_rows_route_as_utc_and_heal(self, tmp_path: Path) -> None:
        """G12: a hand-planted NAIVE column routes as-if-UTC and heals clean
        (fresh destination — the union core never concats)."""
        import polars as pl
        import pyarrow as pa
        import pyarrow.parquet as pq

        lake = tmp_path / "lake"
        june = lake / "options" / "SERIES" / "2024-06.parquet"
        june.parent.mkdir(parents=True, exist_ok=True)
        naive_home = datetime(2024, 6, 3, 4, 0)  # NO tzinfo
        naive_foreign = datetime(2024, 7, 1, 4, 2)
        pq.write_table(
            pa.table(
                {
                    "side": pa.array(["CE", "PE"], type=pa.string()),
                    "timestamp": pa.array(
                        [naive_home, naive_foreign],
                        type=pa.timestamp("us"),  # NO tz
                    ),
                }
            ),
            june,
        )
        scan = scan_lake_invariants(lake)
        (finding,) = scan.findings
        assert isinstance(finding, OptionForeignMonthRows)
        report = heal_findings(
            scan.findings,
            lake_root=lake,
            files_scanned=scan.option_files_scanned,
            mode="auto",
            recall=False,
            now=_now,
        )
        assert report.healed == 1
        assert report.errors == 0
        july = june.parent / "2024-07.parquet"
        assert pl.read_parquet(july)["timestamp"].to_list() == [naive_foreign]
        assert pl.read_parquet(june)["timestamp"].to_list() == [naive_home]

    def test_option_naive_vs_aware_destination_isolated_error(self, tmp_path: Path) -> None:
        """G12 companion: a naive source vs a tz-AWARE existing destination —
        the polars schema error is an ISOLATED counted error (never a silent
        cast), and the copy-forward-first order leaves the source untouched."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        lake = tmp_path / "lake"
        writer = OptionParquetWriter(lake_root=lake)
        writer.write(
            partition_key="SERIES",
            coordinate=_OPT_COORD,
            bars=[_opt_bar(side="CE", ts=_ts(1, month=7, day=1), close="200.00")],
        )
        june = lake / "options" / "SERIES" / "2024-06.parquet"
        naive_home = datetime(2024, 6, 3, 4, 0)
        naive_foreign = datetime(2024, 7, 1, 4, 2)
        pq.write_table(
            pa.table(
                {
                    "side": pa.array(["CE", "PE"], type=pa.string()),
                    "timestamp": pa.array([naive_home, naive_foreign], type=pa.timestamp("us")),
                }
            ),
            june,
        )
        june_bytes = june.read_bytes()
        scan = scan_lake_invariants(lake)
        foreigns = [f for f in scan.findings if isinstance(f, OptionForeignMonthRows)]
        assert len(foreigns) == 1
        with capture_logs() as logs:
            report = heal_findings(
                foreigns,
                lake_root=lake,
                files_scanned=scan.option_files_scanned,
                mode="auto",
                recall=False,
                now=_now,
            )
        assert report.errors == 1
        assert report.healed == 0
        assert june.read_bytes() == june_bytes  # copy-forward failed FIRST — untouched
        errors = _events(logs, "lake_heal.partition_error")
        assert len(errors) == 1

    def test_option_null_side_moved_null_ts_abstains(self, tmp_path: Path) -> None:
        """M15 (the routing-law pin): a null-SIDE row with a foreign ts IS
        moved (scanner count == healable set — no permanent finding); a
        null-TS row stays home untouched; the second sweep is clean."""
        import polars as pl
        import pyarrow as pa
        import pyarrow.parquet as pq

        lake = tmp_path / "lake"
        june = lake / "options" / "SERIES" / "2024-06.parquet"
        june.parent.mkdir(parents=True, exist_ok=True)
        july_ts = _ts(2, month=7, day=1)
        pq.write_table(
            pa.table(
                {
                    "side": pa.array(["CE", None, "PE"], type=pa.string()),
                    "timestamp": pa.array(
                        [_ts(0), july_ts, None], type=pa.timestamp("us", tz="UTC")
                    ),
                }
            ),
            june,
        )
        scan = scan_lake_invariants(lake)
        (finding,) = scan.findings
        assert isinstance(finding, OptionForeignMonthRows)
        assert finding.foreign_rows == 1
        report = heal_findings(
            scan.findings,
            lake_root=lake,
            files_scanned=scan.option_files_scanned,
            mode="auto",
            recall=False,
            now=_now,
        )
        assert report.healed == 1
        july = june.parent / "2024-07.parquet"
        july_frame = pl.read_parquet(july)
        assert july_frame["side"].to_list() == [None]  # the null side rode verbatim
        assert july_frame["timestamp"].to_list() == [july_ts]
        june_frame = pl.read_parquet(june).sort("side")
        assert june_frame["side"].to_list() == ["CE", "PE"]  # null-TS row stayed home
        rescan = scan_lake_invariants(lake)
        assert rescan.findings == ()  # the second sweep is clean

    def test_option_foreign_heal_glob_metachar_series_no_cross_series_import(
        self, tmp_path: Path
    ) -> None:
        """Round-15 MAJOR regression (refuter-confirmed): a balanced-bracket
        series name heals LITERALLY. Pre-fix the copy-forward leg's merge read
        (``pl.read_parquet(dest)`` default ``glob=True``) treated the
        ``…X[12]/2024-07.parquet`` destination as a GLOB matching the REAL
        sibling series ``…X1``/``…X2`` — the rewrite permanently DROPPED the
        literal canonical July row (no quarantine, the merge leg never
        quarantines), IMPORTED the siblings' rows cross-series into the
        flagged series, and falsely emitted ``dedup_healed`` against them —
        from the default AUTO-ON healer, with NO symlink/TOCTOU (static
        persistent state the containment rail cannot see), violating both the
        round-1 no-silent-row-loss law and the round-6 "cross-series
        contamination closed" claim. Post-fix: the heal converges on the
        literal bytes, siblings byte-untouched."""
        import polars as pl

        lake = tmp_path / "lake"
        bracket_key = "NSE_FNO|13|WEEK|0|ATM|X[12]"
        june, july = _poison_option_foreign(lake, partition_key=bracket_key)
        writer = OptionParquetWriter(lake_root=lake)
        for n, sibling_leaf in enumerate(("X1", "X2"), start=1):
            writer.write(
                partition_key=f"NSE_FNO|13|WEEK|0|ATM|{sibling_leaf}",
                coordinate=_OPT_COORD,
                bars=[_opt_bar(side="CE", ts=_ts(30 + n, month=7, day=1), close=f"{n * 111}.00")],
            )
        siblings = [
            _opt_series_dir(lake, partition_key=f"NSE_FNO|13|WEEK|0|ATM|{leaf}") / "2024-07.parquet"
            for leaf in ("X1", "X2")
        ]
        sibling_bytes = [p.read_bytes() for p in siblings]
        scan = scan_lake_invariants(lake)
        (finding,) = scan.findings
        assert isinstance(finding, OptionForeignMonthRows)
        assert finding.foreign_rows == 2  # the LITERAL June bytes, not a glob union
        assert finding.overlap_rows == 1
        with capture_logs() as logs:
            report = heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.option_files_scanned,
                mode="auto",
                recall=False,
                now=_now,
            )
        assert report.healed == 1
        assert report.errors == 0
        july_after = pl.read_parquet(july, glob=False).sort(["side", "timestamp"])
        # Canonical CE (200.0, PRESERVED — pre-fix it was silently dropped) +
        # the reassigned PE (55.0); the siblings' 111.0/222.0 never imported.
        assert sorted(july_after["close"].to_list()) == [55.0, 200.0]
        june_after = pl.read_parquet(june, glob=False)
        assert june_after["close"].to_list() == [100.0]  # source cleaned
        for path, before in zip(siblings, sibling_bytes, strict=True):
            assert path.read_bytes() == before  # siblings byte-untouched
        # No false dedup_healed against the siblings' rows.
        assert _events(logs, "lake.partition.dedup_healed") == []
        rescan = scan_lake_invariants(lake)
        assert rescan.findings == ()  # converged — the second sweep is clean


class TestOptionSymlinkContainment:
    """Round-2 MAJOR (refuter-confirmed): the scan→heal TOCTOU rail end-to-end.

    The scanner's ``is_symlink()`` probe is scan-TIME only — pre-fix, a real
    series dir swapped to a symlink AFTER the scan made the default AUTO-ON
    heal write THROUGH the link (empirically reproduced: the heal created
    ``2024-07.parquet`` OUTSIDE the lake and unlinked the outside source).
    The writer's mutation-boundary containment (checked at heal time on the
    RESOLVED destination) now refuses BOTH mutation legs as isolated counted
    errors; nothing outside the lake is ever created, rewritten, or unlinked.
    """

    def test_toctou_series_dir_swap_refused_and_isolated(self, tmp_path: Path) -> None:
        import polars as pl

        lake = tmp_path / "lake"
        june, _july = _poison_option_foreign(lake)
        # Add a physical dup of the home row too, so BOTH heal-side writer
        # entries (merge_partition_frame + heal_partition) are driven.
        june_df = pl.read_parquet(june)
        pl.concat([june_df, june_df[0:1]]).write_parquet(june)
        scan = scan_lake_invariants(lake)
        assert {type(f).__name__ for f in scan.findings} == {
            "OptionForeignMonthRows",
            "OptionIntraFileDup",
        }
        # The TOCTOU swap: AFTER the scan, the real series dir moves outside
        # and a symlink takes its place (the review's S2 probe, verbatim).
        series_dir = june.parent
        outside = tmp_path / "outside" / series_dir.name
        outside.parent.mkdir(parents=True)
        series_dir.rename(outside)
        series_dir.symlink_to(outside, target_is_directory=True)
        outside_before = _tree_state(outside)
        with capture_logs() as logs:
            report = heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.files_scanned + scan.option_files_scanned,
                mode="auto",
                recall=False,
                now=_now,
            )
        assert report.errors == 2  # foreign-reassign leg + dup leg, both refused
        assert report.healed == 0
        assert report.quarantine_root is None  # refused BEFORE any quarantine copy
        assert not (lake / ".quarantine").exists()
        assert _tree_state(outside) == outside_before  # outside tree byte-untouched
        errors = _events(logs, "lake_heal.partition_error")
        assert len(errors) == 2
        assert all("outside the options tree" in str(e["error"]) for e in errors)

    def test_symlinked_source_file_read_refused_never_imports_outside_rows(
        self, tmp_path: Path
    ) -> None:
        """Round-4 MINOR (refuter-confirmed, empirically probed): the FILE-level
        TOCTOU twin of the series-dir swap above (at scan time the scanner's
        per-file ``is_symlink()`` probe now skips a linked file outright).
        Pre-fix ``read_partition_frame`` had NO containment rail: the healer
        read the OUTSIDE file through the link, routed ITS foreign rows, and
        the copy-forward leg CREATED an in-lake canonical file carrying the
        outside rows — that dest PASSED the write-leg rail because its parent
        is the REAL in-lake series dir — while the source-removal leg was
        refused: outside ``close=666.0`` landed INSIDE the lake and the
        finding never converged (findings=1 healed=0 errors=1 on every sweep).
        Now the READ itself is refused — one isolated counted error, nothing
        imported, outside bytes untouched, no quarantine dir."""
        import polars as pl

        lake = tmp_path / "lake"
        june, _july = _poison_option_foreign(lake)
        scan = scan_lake_invariants(lake)
        (finding,) = scan.findings
        assert isinstance(finding, OptionForeignMonthRows)
        # The TOCTOU swap: AFTER the scan the June FILE becomes a symlink to an
        # outside file whose row routes to August (a month with no in-lake file).
        outside = tmp_path / "outside-lake"
        aug_ts = _ts(3, month=8, day=1)
        OptionParquetWriter(lake_root=outside).merge_partition_frame(
            outside / "options" / "SERIES" / "2024-06.parquet",
            pl.DataFrame(
                {
                    "side": pl.Series(["CE"], dtype=pl.Utf8),
                    "timestamp": pl.Series([aug_ts], dtype=pl.Datetime("us", "UTC")),
                    "close": pl.Series([666.0], dtype=pl.Float64),
                }
            ),
        )
        outside_file = outside / "options" / "SERIES" / "2024-06.parquet"
        outside_bytes = outside_file.read_bytes()
        june.unlink()
        june.symlink_to(outside_file)
        with capture_logs() as logs:
            report = heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.option_files_scanned,
                mode="auto",
                recall=False,
                now=_now,
            )
        assert report.errors == 1  # refused at the READ — isolated, counted
        assert report.healed == 0
        assert not (june.parent / "2024-08.parquet").exists()  # nothing imported
        assert outside_file.read_bytes() == outside_bytes  # outside byte-untouched
        assert not (lake / ".quarantine").exists()
        errors = _events(logs, "lake_heal.partition_error")
        assert len(errors) == 1
        assert "outside the options tree" in str(errors[0]["error"])

    def test_in_lake_symlink_swap_refused_no_cross_series_contamination(
        self, tmp_path: Path
    ) -> None:
        """Round-6 MINOR (refuter-confirmed, empirically probed): the IN-LAKE
        twin of the round-2/4 escape vectors. The scan flags
        SERIES_A/2024-01.parquet carrying a foreign March row; between scan
        and heal the flagged REAL file is swapped to a symlink pointing at
        ``options/SERIES_B/2024-03.parquet`` — a DIFFERENT series still
        INSIDE the lake, so the resolved-containment rail alone PASSED it.
        Pre-fix the heal read SERIES_B's rows through the link, routed them
        all to March, and the copy-forward leg CREATED
        SERIES_A/2024-03.parquet permanently holding SERIES_B's rows
        (cross-series contamination, healed=1 errors=0) before
        quarantine-moving the symlink. Now the source READ refuses ANY
        symlinked target — one isolated counted error, no new SERIES_A file,
        SERIES_B byte-untouched, the symlink left in place."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        lake = tmp_path / "lake"
        series_a = lake / "options" / "SERIES_A"
        series_a.mkdir(parents=True)
        pq.write_table(
            pa.table(
                {
                    "side": pa.array(["CE", "CE"], type=pa.string()),
                    "timestamp": pa.array(
                        [_ts(0, month=1, day=2), _ts(1, month=3, day=4)],
                        type=pa.timestamp("us", tz="UTC"),
                    ),
                }
            ),
            series_a / "2024-01.parquet",
        )
        # SERIES_B is a REAL, clean series seeded through the writer.
        writer = OptionParquetWriter(lake_root=lake)
        writer.write(
            partition_key="NSE_FNO|14|WEEK|0|ATM|PUT",
            coordinate=_OPT_COORD,
            bars=[_opt_bar(side="CE", ts=_ts(2, month=3, day=4), close="42.00")],
        )
        b_file = lake / "options" / "NSE_FNO__14__WEEK__0__ATM__PUT" / "2024-03.parquet"
        b_bytes = b_file.read_bytes()
        scan = scan_lake_invariants(lake)
        (finding,) = scan.findings
        assert isinstance(finding, OptionForeignMonthRows)
        assert finding.path == series_a / "2024-01.parquet"
        # The TOCTOU swap: AFTER the scan the flagged file becomes a symlink
        # to the OTHER series' canonical March file (in-lake — the round-2
        # escape rail cannot see it).
        (series_a / "2024-01.parquet").unlink()
        (series_a / "2024-01.parquet").symlink_to(b_file)
        with capture_logs() as logs:
            report = heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.option_files_scanned,
                mode="auto",
                recall=False,
                now=_now,
            )
        assert report.errors == 1  # refused at the READ — isolated, counted
        assert report.healed == 0
        assert not (series_a / "2024-03.parquet").exists()  # nothing imported cross-series
        assert b_file.read_bytes() == b_bytes  # SERIES_B byte-untouched
        assert (series_a / "2024-01.parquet").is_symlink()  # never unlinked/quarantine-moved
        assert not (lake / ".quarantine").exists()
        errors = _events(logs, "lake_heal.partition_error")
        assert len(errors) == 1
        assert "symlink inside the options tree" in str(errors[0]["error"])

    def test_symlink_loop_reassignment_target_isolated_not_batch_abort(
        self, tmp_path: Path
    ) -> None:
        """Round-5 MAJOR (refuter-confirmed): a symlink LOOP planted as the
        reassignment TARGET (the foreign rows' canonical home-month file)
        made ``_assert_options_containment``'s ``dest.resolve()`` raise a
        bare ``RuntimeError('Symlink loop from …')`` on Python ≤ 3.12 — NOT
        in ``_PER_FINDING_HEAL_ERRORS`` and not ``LakeWriteHalt``, so it
        escaped ``heal_findings``' per-finding isolation loop and aborted
        the WHOLE batch: no HealReport, no stats bump, no ``on_report``
        alert, and every later finding went unhealed. NO TOCTOU timing is
        needed — the scanner symlink-skips only the flagged SOURCE file,
        never the reassignment target, so a pre-planted loop suffices. Now
        the refusal is the documented isolated counted
        ``OptionWriterError``; the batch continues and a later finding in
        the SAME batch still heals."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        lake = tmp_path / "lake"
        # Series A: a real June file carrying a Jan-foreign row; the canonical
        # Jan home file is a self-referential symlink loop.
        june = _opt_series_dir(lake) / "2024-06.parquet"
        june.parent.mkdir(parents=True)
        pq.write_table(
            pa.table(
                {
                    "side": pa.array(["CE", "CE"], type=pa.string()),
                    "timestamp": pa.array(
                        [_ts(0), _ts(1, month=1, day=2)], type=pa.timestamp("us", tz="UTC")
                    ),
                }
            ),
            june,
        )
        loop = june.parent / "2024-01.parquet"
        loop.symlink_to(loop)
        # Series B: an unrelated dup that MUST still heal after the refusal.
        dup_file = lake / "options" / "ZZ_DUP" / "2024-06.parquet"
        dup_file.parent.mkdir(parents=True)
        pq.write_table(
            pa.table(
                {
                    "side": pa.array(["CE", "CE"], type=pa.string()),
                    "timestamp": pa.array([_ts(5), _ts(5)], type=pa.timestamp("us", tz="UTC")),
                }
            ),
            dup_file,
        )
        scan = scan_lake_invariants(lake)
        assert {type(f).__name__ for f in scan.findings} == {
            "OptionForeignMonthRows",
            "OptionIntraFileDup",
        }
        june_bytes = june.read_bytes()
        reports: list[HealReport] = []
        with capture_logs() as logs:
            report = heal_findings(  # pre-fix: raises RuntimeError right here
                scan.findings,
                lake_root=lake,
                files_scanned=scan.files_scanned + scan.option_files_scanned,
                mode="auto",
                on_report=reports.append,
                recall=False,
                now=_now,
            )
        assert report.errors == 1  # the loop refusal — isolated, counted
        assert report.healed == 1  # the LATER dup finding still healed
        assert report.skipped == 0
        assert reports == [report]  # the alert seam fired despite the refusal
        assert june.read_bytes() == june_bytes  # refusal preceded any mutation
        assert loop.is_symlink()  # the loop itself untouched
        errors = _events(logs, "lake_heal.partition_error")
        assert len(errors) == 1
        assert "cannot resolve" in str(errors[0]["error"])
        # The dup heal quarantined its original as usual.
        assert report.quarantine_root == str(lake / ".quarantine" / _QUARANTINE_TS)


class TestQuarantineRootCollision:
    def test_same_second_second_batch_keeps_first_before_image(self, tmp_path: Path) -> None:
        """Round-5 NIT (pre-existing DEC-LAKE-HEAL-001 machinery): the
        quarantine stamp is second-granular, so a SECOND batch under the
        same clock reused ``<lake>/.quarantine/<ts>/`` and its atomic copy
        OVERWROTE the first batch's before-image at the same lake-relative
        path — losing the forensic original. The ``-NN`` serial now picks a
        fresh dir; both before-images survive."""
        lake = tmp_path / "lake"
        dest = _dest(lake)
        _poison(dest, [(_ts(1), 100.0, 10), (_ts(1), 100.0, 10)])
        scan1 = scan_lake_invariants(lake)
        report1 = heal_findings(
            scan1.findings,
            lake_root=lake,
            files_scanned=scan1.files_scanned,
            mode="auto",
            recall=False,
            now=_now,
        )
        assert report1.healed == 1
        assert report1.quarantine_root == str(lake / ".quarantine" / _QUARANTINE_TS)
        first_image = lake / ".quarantine" / _QUARANTINE_TS / dest.relative_to(lake)
        first_bytes = first_image.read_bytes()
        # Re-poison the SAME partition with DIFFERENT rows; same-second clock.
        _poison(dest, [(_ts(2), 200.0, 5), (_ts(2), 200.0, 5)])
        scan2 = scan_lake_invariants(lake)
        report2 = heal_findings(
            scan2.findings,
            lake_root=lake,
            files_scanned=scan2.files_scanned,
            mode="auto",
            recall=False,
            now=_now,
        )
        assert report2.healed == 1
        assert report2.quarantine_root == str(lake / ".quarantine" / f"{_QUARANTINE_TS}-01")
        # Pre-fix: the second copy landed at first_image and clobbered it.
        assert first_image.read_bytes() == first_bytes
        second_image = lake / ".quarantine" / f"{_QUARANTINE_TS}-01" / dest.relative_to(lake)
        assert second_image.exists()
        assert second_image.read_bytes() != first_bytes


class TestQuarantineSymlinkContainment:
    """Round-17 fold (refuter-confirmed MINOR): the quarantine BEFORE-image
    write was the ONE heal-side mutation outside the rounds-2/4/6 symlink-
    containment program — ``<lake>/.quarantine`` pre-planted as a symlink
    (the round-2 V1 capability class) sent every batch's forensic
    before-image OUTSIDE the lake root before the in-lake source was
    rewritten/unlinked, voiding the "the quarantine copy IS the forensic
    before-image" guarantee. ``heal_findings`` now refuses the WHOLE batch
    untouched at batch start, and the ``-NN`` serial loop also serials past
    a DANGLING symlink at the stamp path (``exists()`` alone missed it)."""

    def test_symlinked_quarantine_root_refuses_whole_batch_untouched(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        dest = _dest(lake)
        _poison(dest, [(_ts(1), 100.0, 10), (_ts(1), 100.0, 10)])
        opt_dest = _poison_option_lake(lake)
        outside = tmp_path / "outside"
        outside.mkdir()
        (lake / ".quarantine").symlink_to(outside, target_is_directory=True)
        bars_bytes = dest.read_bytes()
        opt_bytes = opt_dest.read_bytes()
        audit = _FakeAudit()
        reports: list[HealReport] = []
        scan = scan_lake_invariants(lake)
        assert len(scan.findings) == 2  # one bars dup + one option dup
        with capture_logs() as logs:
            report = heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.files_scanned + scan.option_files_scanned,
                mode="auto",
                audit=audit,
                on_report=reports.append,
                recall=False,
                now=_now,
            )
        # The WHOLE batch refused: every finding an error, zero mutation.
        assert report.healed == 0
        assert report.skipped == 0
        assert report.errors == 2
        assert report.findings == 2
        assert report.actions == ()
        assert report.quarantine_root is None
        assert dest.read_bytes() == bars_bytes  # pre-fix: healed + copied out
        assert opt_dest.read_bytes() == opt_bytes
        assert list(outside.rglob("*")) == []  # nothing landed outside the lake
        assert (lake / ".quarantine").is_symlink()  # the link left in place
        (refusal,) = _events(logs, "lake_heal.quarantine_symlink_refused")
        assert refusal["quarantine"] == str(lake / ".quarantine")
        assert refusal["findings"] == 2
        assert reports == [report]  # the alert seam still fired
        (line,) = audit.lines
        assert line[0] == AUDIT_HEAL_BATCH
        payload = line[2]
        assert payload is not None
        assert payload["refused_quarantine_symlink"] is True
        assert payload["errors"] == 2
        # errors > 0 ⇒ the L3 sentinel is never stamped — next boot re-attempts.
        assert lake_heal._sentinel_skip_reason(report) == "heal_errors"
        # A re-run refuses identically with NO seams wired (idempotent, and
        # the refusal never requires an on_report/audit consumer).
        again = heal_findings(
            scan.findings,
            lake_root=lake,
            files_scanned=scan.files_scanned + scan.option_files_scanned,
            mode="auto",
            recall=False,
            now=_now,
        )
        assert again.errors == 2
        assert again.healed == 0
        assert dest.read_bytes() == bars_bytes
        assert list(outside.rglob("*")) == []

    def test_zero_finding_batch_with_symlinked_quarantine_not_refused(self, tmp_path: Path) -> None:
        """The ``ordered`` gate pinned: a CLEAN batch never writes a
        before-image, so a pre-existing ``.quarantine`` symlink does not turn
        a clean sweep into a refusal (the sentinel may still stamp)."""
        lake = tmp_path / "lake"
        _write_spot(lake, [_bar(_ts(0))])
        (lake / ".quarantine").symlink_to(tmp_path / "elsewhere")
        scan = scan_lake_invariants(lake)
        assert scan.findings == ()
        with capture_logs() as logs:
            report = heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.files_scanned,
                mode="auto",
                recall=False,
                now=_now,
            )
        assert report.errors == 0
        assert report.findings == 0
        assert _events(logs, "lake_heal.quarantine_symlink_refused") == []
        assert lake_heal._sentinel_skip_reason(report) is None

    def test_dangling_symlink_at_stamp_dir_serialed_past(self, tmp_path: Path) -> None:
        """A DANGLING symlink pre-planted at ``.quarantine/<stamp>`` reads
        ``exists() == False``, so the pre-fix serial loop picked it and the
        batch's before-images were written THROUGH the link; the
        ``is_symlink()`` arm now serials past it to ``<stamp>-01``."""
        lake = tmp_path / "lake"
        dest = _dest(lake)
        _poison(dest, [(_ts(1), 100.0, 10), (_ts(1), 100.0, 10)])
        (lake / ".quarantine").mkdir(parents=True)
        planted = lake / ".quarantine" / _QUARANTINE_TS
        target = tmp_path / "link-target-never-created"
        planted.symlink_to(target)
        assert not planted.exists()  # dangling — the exists() arm alone misses it
        scan = scan_lake_invariants(lake)
        report = heal_findings(
            scan.findings,
            lake_root=lake,
            files_scanned=scan.files_scanned,
            mode="auto",
            recall=False,
            now=_now,
        )
        assert report.healed == 1
        assert report.quarantine_root == str(lake / ".quarantine" / f"{_QUARANTINE_TS}-01")
        image = lake / ".quarantine" / f"{_QUARANTINE_TS}-01" / dest.relative_to(lake)
        assert image.exists()
        assert planted.is_symlink()  # untouched
        assert not target.exists()  # never written through


# ── heal order + idempotent re-run + failure envelopes ─────────────────────


class TestBatchOrderAndIdempotence:
    def _seed_kitchen_sink(self, lake: Path) -> None:
        """All five finding classes in ONE lake."""
        # Drift: legacy NSE-M&M vs canonical M&M.
        _write_spot(lake, [_bar(_ts(0), close="100.00")], segment="CASH", symbol="M&M")
        _poison(
            lake / "bars" / "NSE" / "CASH" / "NSE-M&M" / "1minute" / "2024" / "06.parquet",
            [(_ts(5), 77.0, 9)],
        )
        # Foreign: NIFTY June file carrying a May row; canonical May present.
        _write_spot(lake, [_bar(_ts(1, month=5), close="100.25")])
        _poison(
            _dest(lake, month=6),
            [(_ts(2, month=5), 55.0, 7), (_ts(0, month=6), 101.0, 12)],
        )
        # Dup: BANKNIFTY June.
        _poison(
            _dest(lake, symbol="BANKNIFTY", month=6),
            [(_ts(0), 200.0, 10), (_ts(0), 999.0, 11), (_ts(1), 201.0, 12)],
        )
        # Option dup (the CALL series).
        _poison_option_lake(lake)
        # Option foreign (a DISTINCT PUT series — DEC-LAKE-HEAL-OPTIONS-FOREIGN-001).
        _poison_option_foreign(lake, partition_key="NSE_FNO|13|WEEK|0|ATM|PUT")

    def test_heal_order_drift_foreign_dup_option(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        self._seed_kitchen_sink(lake)
        scan = scan_lake_invariants(lake)
        assert len(scan.findings) == 5
        report = heal_findings(
            scan.findings,
            lake_root=lake,
            files_scanned=scan.files_scanned + scan.option_files_scanned,
            mode="auto",
            recall=False,
            now=_now,
        )
        assert report.healed == 5
        assert [a.action for a in report.actions] == [
            AUDIT_HEAL_DRIFT_MERGE,
            AUDIT_HEAL_REASSIGN,
            AUDIT_HEAL_DEDUP,
            AUDIT_HEAL_REASSIGN,
            AUDIT_HEAL_DEDUP,
        ]
        assert report.actions[1].partition.startswith("bars/")
        assert report.actions[2].partition == "bars/NSE/INDEX/BANKNIFTY/1minute/2024/06.parquet"
        assert report.actions[3].partition == (
            "options/NSE_FNO__13__WEEK__0__ATM__PUT/2024-06.parquet"
        )
        assert report.actions[4].partition.startswith("options/NSE_FNO__13__WEEK__0__ATM__CALL/")

    def test_order_findings_five_class_law(self, tmp_path: Path) -> None:
        """Direct unit: option-foreign sorts between dup and option-dup, by path."""
        coords = PartitionCoords(
            exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", year=2024, month=6
        )
        option_dup = OptionIntraFileDup(
            path=tmp_path / "options" / "A" / "2024-06.parquet",
            series="A",
            physical_rows=2,
            unique_keys=1,
        )
        option_foreign_b = OptionForeignMonthRows(
            path=tmp_path / "options" / "B" / "2024-06.parquet",
            series="B",
            year=2024,
            month=6,
            foreign_rows=1,
            overlap_rows=0,
        )
        option_foreign_a = OptionForeignMonthRows(
            path=tmp_path / "options" / "A" / "2024-07.parquet",
            series="A",
            year=2024,
            month=7,
            foreign_rows=1,
            overlap_rows=0,
        )
        bar_dup = IntraFileDup(path=Path("x"), coords=coords, physical_rows=2, unique_keys=1)
        ordered = lake_heal._order_findings(
            (option_dup, option_foreign_b, option_foreign_a, bar_dup)
        )
        assert list(ordered) == [bar_dup, option_foreign_a, option_foreign_b, option_dup]

    def test_idempotent_second_run_no_actions(self, tmp_path: Path) -> None:
        """Spec-mandated: the re-scan of a healed lake finds ZERO findings and
        a second heal changes NOT ONE byte anywhere (whole-tree sha compare,
        quarantine + sidecars included)."""
        lake = tmp_path / "lake"
        self._seed_kitchen_sink(lake)
        scan = scan_lake_invariants(lake)
        heal_findings(
            scan.findings,
            lake_root=lake,
            files_scanned=scan.files_scanned + scan.option_files_scanned,
            mode="auto",
            recall=True,
            now=_now,
        )
        rescan = scan_lake_invariants(lake)
        assert rescan.findings == ()
        before = _tree_state(lake)
        second = heal_findings(
            rescan.findings,
            lake_root=lake,
            files_scanned=rescan.files_scanned + rescan.option_files_scanned,
            mode="auto",
            recall=True,
            now=_now,
        )
        assert second.healed == 0
        assert second.actions == ()
        assert _tree_state(lake) == before  # byte-identical, recursively

    def test_lake_write_halt_propagates_out_of_batch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Disk-full mid-batch: the halt propagates untouched; the EARLIER
        heal is durable; the LATER finding's file is untouched."""
        lake = tmp_path / "lake"
        bank = _dest(lake, symbol="BANKNIFTY")
        nifty = _dest(lake, symbol="NIFTY")
        _poison(bank, [(_ts(0), 200.0, 10), (_ts(0), 999.0, 11)])
        _poison(nifty, [(_ts(0), 100.0, 10), (_ts(0), 999.0, 11)])
        nifty_before = nifty.read_bytes()
        scan = scan_lake_invariants(lake)
        real_heal = ParquetWriter.heal_partition

        def _halting_heal(self: ParquetWriter, dest: Path, **kwargs: Any) -> Any:
            if dest == nifty:
                raise LakeWriteHalt("disk_full")
            return real_heal(self, dest, **kwargs)

        monkeypatch.setattr(ParquetWriter, "heal_partition", _halting_heal)
        with pytest.raises(LakeWriteHalt):
            heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.files_scanned,
                mode="auto",
                recall=False,
                now=_now,
            )
        # BANKNIFTY (healed FIRST — path order) is durable; NIFTY untouched.
        assert _stored_frame(bank)["timestamp"].to_list() == [_ts(0)]
        assert nifty.read_bytes() == nifty_before

    def test_per_finding_error_isolated(self, tmp_path: Path) -> None:
        """One unreadable partition errors; the remaining finding still heals."""
        lake = tmp_path / "lake"
        bank = _dest(lake, symbol="BANKNIFTY")
        bank.parent.mkdir(parents=True, exist_ok=True)
        bank.write_bytes(b"not a parquet file at all")
        nifty = _dest(lake, symbol="NIFTY")
        _poison(nifty, [(_ts(0), 100.0, 10), (_ts(0), 999.0, 11)])
        corrupt = IntraFileDup(
            path=bank,
            coords=PartitionCoords(
                exchange="NSE",
                segment="INDEX",
                symbol="BANKNIFTY",
                interval="1minute",
                year=2024,
                month=6,
            ),
            physical_rows=2,
            unique_keys=1,
        )
        scan = scan_lake_invariants(lake, symbol="NIFTY")
        with capture_logs() as logs:
            report = heal_findings(
                (corrupt, *scan.findings),
                lake_root=lake,
                files_scanned=2,
                mode="auto",
                recall=False,
                now=_now,
            )
        assert report.errors == 1
        assert report.healed == 1
        errors = _events(logs, "lake_heal.partition_error")
        assert len(errors) == 1
        assert errors[0]["log_level"] == "warning"
        assert errors[0]["finding"] == "IntraFileDup"
        assert _stored_frame(nifty)["timestamp"].to_list() == [_ts(0)]


# ── derived-data recall ────────────────────────────────────────────────────


def _replant_with_dup(dest: Path) -> None:
    """Re-render a CLEAN stored partition with an identical duplicate of row 0."""
    import polars as pl

    df = pl.read_parquet(dest)
    pl.concat([df[0:1], df]).write_parquet(dest)


class TestDerivedRecall:
    def test_recall_daily_rederives_affected_months(self, tmp_path: Path) -> None:
        """Latest-wins re-derive CORRECTS a poisoned stored 1day bar after the
        1minute heals (the recon-C latest-wins overwrite proof, heal-driven)."""
        lake = tmp_path / "lake"
        _write_spot(lake, [_bar(_ts(0), close="100.00"), _bar(_ts(1), close="101.00", volume=12)])
        # A POISONED stored daily bar at the derived midnight-IST timestamp.
        daily_ts = datetime(2024, 6, 2, 18, 30, tzinfo=UTC)
        _poison(_dest(lake, interval="1day"), [(daily_ts, 999.0, 999)])
        _replant_with_dup(_dest(lake))
        scan = scan_lake_invariants(lake)
        dups = [f for f in scan.findings if isinstance(f, IntraFileDup)]
        assert len(dups) == 1
        report = heal_findings(
            scan.findings,
            lake_root=lake,
            files_scanned=scan.files_scanned,
            mode="auto",
            recall=True,
            now=_now,
        )
        assert report.recall is not None
        assert report.recall.daily_targets == 1
        assert report.recall.daily_bars == 1
        assert report.recall.tf_rebuilt == 0  # no TF dirs were ever materialised
        daily = _stored_frame(_dest(lake, interval="1day"))
        assert daily["timestamp"].to_list() == [daily_ts]
        assert daily["open"].to_list() == [100.0]  # the corrected derivation WON
        assert daily["close"].to_list() == [101.0]
        assert daily["volume"].to_list() == [22]

    def test_recall_removes_the_derive_stamp(self, tmp_path: Path) -> None:
        """DEC-PREBUILD-INCREMENTAL-001: a healed symbol's derive stamp is
        removed outright by the recall (belt-and-braces on top of the source
        token the rewrite already moved), so the next pipeline prebuild can
        never skip a healed symbol."""
        from datetime import UTC as _UTC
        from datetime import datetime as _dt

        from brutex.data.derive_stamp import (
            DeriveStamp,
            derive_stamp_path,
            save_derive_stamp,
        )

        lake = tmp_path / "lake"
        _write_spot(lake, [_bar(_ts(0), close="100.00"), _bar(_ts(1), close="101.00", volume=12)])
        stamp_file = derive_stamp_path(lake, "NSE", "INDEX", "NIFTY")
        save_derive_stamp(
            stamp_file,
            DeriveStamp(
                token="stale",
                tfs=("5m",),
                include_daily=True,
                daily_before="2024-06-03",
                last_1m_ist_date="2024-06-02",
                written_at=_dt.now(_UTC).isoformat(timespec="seconds"),
            ),
        )
        assert stamp_file.exists()
        _replant_with_dup(_dest(lake))
        scan = scan_lake_invariants(lake)
        report = heal_findings(
            scan.findings,
            lake_root=lake,
            files_scanned=scan.files_scanned,
            mode="auto",
            recall=True,
            now=_now,
        )
        assert report.recall is not None and report.recall.daily_targets == 1
        assert not stamp_file.exists()  # the stale stamp is GONE

    def test_recall_fno_daily_carries_oi_through_fno_resampler(self, tmp_path: Path) -> None:
        """The F&O branch reads OI + resamples through the OI-carry positional
        resampler — the recalled 1day row carries last-of-day open_interest."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        lake = tmp_path / "lake"
        symbol = "NSE-NIFTY-27Jun24-24000-CE"
        dest = _dest(lake, segment="FNO", symbol=symbol)
        dest.parent.mkdir(parents=True, exist_ok=True)
        rows = [(_ts(0), 100.0, 10, 500), (_ts(0), 100.0, 10, 500), (_ts(1), 101.0, 12, 600)]
        prices = [p for _, p, _, _ in rows]
        pq.write_table(
            pa.table(
                {
                    "timestamp": pa.array(
                        [ts for ts, _, _, _ in rows], type=pa.timestamp("us", tz="UTC")
                    ),
                    "open": pa.array(prices, type=pa.float64()),
                    "high": pa.array(prices, type=pa.float64()),
                    "low": pa.array(prices, type=pa.float64()),
                    "close": pa.array(prices, type=pa.float64()),
                    "volume": pa.array([v for _, _, v, _ in rows], type=pa.int64()),
                    "open_interest": pa.array([oi for _, _, _, oi in rows], type=pa.int64()),
                }
            ),
            dest,
        )
        scan = scan_lake_invariants(lake)
        report = heal_findings(
            scan.findings,
            lake_root=lake,
            files_scanned=scan.files_scanned,
            mode="auto",
            recall=True,
            now=_now,
        )
        assert report.healed == 1
        assert report.recall is not None
        assert report.recall.daily_bars == 1
        daily = _stored_frame(_dest(lake, segment="FNO", symbol=symbol, interval="1day"))
        assert daily["open_interest"].to_list() == [600]  # carry-LAST OI (F&O resampler)

    def test_recall_tf_delete_and_rebuild(self, tmp_path: Path) -> None:
        """The mandatory TF pin: the keep-first TF month is DELETE-and-rebuilt
        — an in-place re-derive would have kept the poisoned stored bucket."""
        from brutex.data.models import CatalogEntry

        lake = tmp_path / "lake"
        _write_spot(lake, [_bar(_ts(0), close="100.00"), _bar(_ts(1), close="101.00", volume=12)])
        _write_spot(lake, [_bar(_ts(1, month=5), close="99.00")])
        materialize_lake_timeframes(
            lake_root=lake,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            kind=PrebuildKind.SPOT,
            tfs=["5m"],
        )
        tf_dest = _dest(lake, interval="5minute")
        assert tf_dest.exists()
        # Delete the MAY TF month — a touched month with NO materialised file
        # is skipped by the delete loop (and re-created by the rebuild).
        _dest(lake, interval="5minute", month=5).unlink()
        # VALUE-poison the derived TF bucket (keep-first would freeze this).
        import polars as pl

        poisoned_tf = pl.read_parquet(tf_dest).with_columns(
            (pl.col("volume") * 1000).alias("volume")
        )
        poisoned_tf.write_parquet(tf_dest)
        poisoned_tf_bytes = tf_dest.read_bytes()
        # Dup-poison BOTH 1minute months so the heal touches May AND June
        # (identical dups — the healed 1minute equals the clean baseline).
        _replant_with_dup(_dest(lake))
        _replant_with_dup(_dest(lake, month=5))
        audit = _FakeAudit()
        scan = scan_lake_invariants(lake)
        with Catalog.open(lake / "catalog.duckdb") as catalog:
            catalog.upsert(
                CatalogEntry(
                    exchange="NSE",
                    segment="INDEX",
                    symbol="NIFTY",
                    interval="5minute",
                    year=2024,
                    month=6,
                    raw_hash="0" * 64,
                    row_count=1,
                    first_ts=_ts(0),
                    last_ts=_ts(1),
                    fetched_at=_NOW,
                )
            )
            with capture_logs() as logs:
                report = heal_findings(
                    scan.findings,
                    lake_root=lake,
                    files_scanned=scan.files_scanned,
                    mode="auto",
                    catalog=catalog,
                    audit=audit,
                    recall=True,
                    now=_now,
                )
            tf_row = catalog.get(
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="5minute",
                year=2024,
                month=6,
            )
        assert report.recall is not None
        assert report.recall.tf_rebuilt == 1
        assert report.recall.tf_failed == 0
        # The TF month was rebuilt from the healed 1minute — poison GONE.
        rebuilt = _stored_frame(tf_dest)
        assert rebuilt["volume"].to_list() == [22]  # 10 + 12, not the ×1000 poison
        # The poisoned TF copy is preserved in quarantine.
        quarantine = lake / ".quarantine" / _QUARANTINE_TS / tf_dest.relative_to(lake)
        assert quarantine.read_bytes() == poisoned_tf_bytes
        assert AUDIT_HEAL_QUARANTINE_FILE in audit.actions()
        # The stale TF catalog row was deleted (the rebuild writers are
        # catalog-less by design — reconcile re-registers it).
        assert tf_row is None
        assert len(_events(logs, "lake_heal.recall_tf_rebuilt")) == 1

    def test_recall_skipped_when_only_tf_healed(self, tmp_path: Path) -> None:
        """Healing ONLY a derived-TF file triggers NO recall (the trigger is a
        touched ``1minute`` interval, nothing else)."""
        lake = tmp_path / "lake"
        _poison(
            _dest(lake, interval="5minute"),
            [(_ts(0), 100.0, 10), (_ts(0), 999.0, 11)],
        )
        scan = scan_lake_invariants(lake)
        report = heal_findings(
            scan.findings,
            lake_root=lake,
            files_scanned=scan.files_scanned,
            mode="auto",
            recall=True,
            now=_now,
        )
        assert report.healed == 1
        assert report.recall is None

    def test_recall_tf_rebuild_failure_counted_months_stay_deleted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lake = tmp_path / "lake"
        _write_spot(lake, [_bar(_ts(0)), _bar(_ts(1))])
        materialize_lake_timeframes(
            lake_root=lake,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            kind=PrebuildKind.SPOT,
            tfs=["5m"],
        )
        tf_dest = _dest(lake, interval="5minute")
        _replant_with_dup(_dest(lake))
        scan = scan_lake_invariants(lake)

        def _boom(**_kwargs: Any) -> Any:
            raise ValueError("simulated rebuild failure")

        monkeypatch.setattr(lake_heal, "materialize_lake_timeframes", _boom)
        with capture_logs() as logs:
            report = heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.files_scanned,
                mode="auto",
                recall=True,
                now=_now,
            )
        assert report.recall is not None
        assert report.recall.tf_rebuilt == 0
        assert report.recall.tf_failed == 1
        assert not tf_dest.exists()  # honestly deleted, loudly logged
        failures = _events(logs, "lake_heal.tf_rebuild_failed")
        assert len(failures) == 1
        assert failures[0]["log_level"] == "warning"

    def test_recall_lake_write_halt_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A disk-full during the TF rebuild is a pull-wide stop, never a
        swallowed per-symbol failure."""
        lake = tmp_path / "lake"
        _write_spot(lake, [_bar(_ts(0)), _bar(_ts(1))])
        materialize_lake_timeframes(
            lake_root=lake,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            kind=PrebuildKind.SPOT,
            tfs=["5m"],
        )
        _replant_with_dup(_dest(lake))
        scan = scan_lake_invariants(lake)

        def _halt(**_kwargs: Any) -> Any:
            raise LakeWriteHalt("disk_full")

        monkeypatch.setattr(lake_heal, "materialize_lake_timeframes", _halt)
        with pytest.raises(LakeWriteHalt):
            heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.files_scanned,
                mode="auto",
                recall=True,
                now=_now,
            )

    def test_recall_tf_rebuild_is_month_bounded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DEC-TF-REBUILD-MONTH-BOUND-001: the recall threads the touched-months
        set into ``materialize_lake_timeframes(months=…)`` so the rebuild READ
        is O(affected-months), never the whole 1-minute tree — and the bounded
        rebuild still heals the poisoned bucket."""
        lake = tmp_path / "lake"
        _write_spot(lake, [_bar(_ts(0)), _bar(_ts(1))])
        materialize_lake_timeframes(
            lake_root=lake,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            kind=PrebuildKind.SPOT,
            tfs=["5m"],
        )
        _replant_with_dup(_dest(lake))
        scan = scan_lake_invariants(lake)

        captured: list[Any] = []
        real = lake_heal.materialize_lake_timeframes

        def _spy(**kwargs: Any) -> Any:
            captured.append(kwargs.get("months"))
            return real(**kwargs)

        monkeypatch.setattr(lake_heal, "materialize_lake_timeframes", _spy)
        report = heal_findings(
            scan.findings,
            lake_root=lake,
            files_scanned=scan.files_scanned,
            mode="auto",
            recall=True,
            now=_now,
        )
        assert report.recall is not None
        assert report.recall.tf_rebuilt == 1
        assert report.recall.tf_failed == 0
        assert captured == [{(2024, 6)}]  # the touched-months set, nothing wider

    def test_prebuild_kind_from_coords_matrix(self) -> None:
        assert lake_heal._prebuild_kind_from_coords("INDEX", "NIFTY") is PrebuildKind.SPOT
        assert lake_heal._prebuild_kind_from_coords("CASH", "M&M") is PrebuildKind.SPOT
        assert (
            lake_heal._prebuild_kind_from_coords("FNO", "NSE-NIFTY-27Jun24-FUT")
            is PrebuildKind.FUTURES
        )
        assert lake_heal._prebuild_kind_from_coords("FNO", "NSE-NIFTY-27Jun24-24000-CE") is None
        assert lake_heal._prebuild_kind_from_coords("FNO", "JUNK") is None

    def test_rebuild_tf_months_skips_options_kind(self, tmp_path: Path) -> None:
        """A CE symbol with a (foreign) TF dir is SKIPPED — options intraday is
        never materialised, so it must never be rebuilt (whitebox)."""
        lake = tmp_path / "lake"
        symbol = "NSE-NIFTY-27Jun24-24000-CE"
        (lake / "bars" / "NSE" / "FNO" / symbol / "5minute").mkdir(parents=True)
        state = lake_heal._BatchState(
            lake_root=lake,
            quarantine_root=lake / ".quarantine" / _QUARANTINE_TS,
            audit=None,
            catalog=None,
            writer=ParquetWriter(lake_root=lake),
            option_writer=OptionParquetWriter(lake_root=lake),
        )
        assert lake_heal._rebuild_tf_months(
            state, exchange="NSE", segment="FNO", symbol=symbol, months={(2024, 6)}
        ) == (0, 0)


# ── stats sidecar + loaders ────────────────────────────────────────────────


class TestStatsAndLoaders:
    def test_stats_accumulate_across_batches(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        for symbol in ("BANKNIFTY", "NIFTY"):
            _poison(
                _dest(lake, symbol=symbol),
                [(_ts(0), 100.0, 10), (_ts(0), 999.0, 11)],
            )
            scan = scan_lake_invariants(lake, symbol=symbol)
            heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.files_scanned,
                mode="auto",
                recall=False,
                now=_now,
            )
        stats = load_heal_stats(lake)
        assert stats["healed_partitions_total"] == 2
        assert stats["quarantined_files_total"] == 2

    def test_stats_write_failure_is_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("simulated stats write failure")

        monkeypatch.setattr(lake_heal, "atomic_write_simple", _boom)
        with capture_logs() as logs:
            lake_heal._bump_stats(tmp_path, healed=1, quarantined=1, now_fn=_now)
        failures = _events(logs, "lake_heal.stats_write_failed")
        assert len(failures) == 1
        assert failures[0]["log_level"] == "warning"
        assert load_heal_stats(tmp_path) == {}

    def test_load_heal_stats_tolerant(self, tmp_path: Path) -> None:
        assert load_heal_stats(tmp_path) == {}  # missing
        stats_path = tmp_path / ".lake_heal" / "stats.json"
        stats_path.parent.mkdir(parents=True)
        stats_path.write_text("{corrupt", encoding="utf-8")
        assert load_heal_stats(tmp_path) == {}  # corrupt
        stats_path.write_text("[1, 2]", encoding="utf-8")
        assert load_heal_stats(tmp_path) == {}  # non-object
        stats_path.write_text('{"healed_partitions_total": 7}', encoding="utf-8")
        assert load_heal_stats(tmp_path) == {"healed_partitions_total": 7}

    def test_load_last_sweep_tolerant(self, tmp_path: Path) -> None:
        assert load_last_sweep(tmp_path) is None  # missing
        sentinel = tmp_path / ".lake_heal" / "last_sweep.json"
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text("{corrupt", encoding="utf-8")
        assert load_last_sweep(tmp_path) is None  # corrupt → fail-open
        sentinel.write_text('"a string"', encoding="utf-8")
        assert load_last_sweep(tmp_path) is None  # non-object → fail-open
        sentinel.write_text('{"lake_token": "abc"}', encoding="utf-8")
        assert load_last_sweep(tmp_path) == {"lake_token": "abc"}


# ── L2 + L1 seam cores ─────────────────────────────────────────────────────


class TestHealAfterPull:
    def test_heal_after_pull_scopes_months_and_symbols(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The scan is bounded to the pulled coordinates × span months, one
        scan per UNIQUE target, options tree excluded (spy pin)."""
        calls: list[dict[str, Any]] = []

        def _spy_scan(lake_root: Path, **kwargs: Any) -> LakeScanReport:
            calls.append({"lake_root": lake_root, **kwargs})
            return LakeScanReport(
                findings=(),
                files_scanned=3,
                files_skipped=0,
                series_scanned=1,
                option_files_scanned=0,
                option_files_skipped=0,
                duration_s=0.0,
            )

        monkeypatch.setattr(lake_heal, "scan_lake_invariants", _spy_scan)
        start, end = date(2024, 6, 1), date(2024, 7, 1)
        report = heal_after_pull(
            lake_root=tmp_path,
            targets=[
                ("NSE", "INDEX", "NIFTY"),
                ("NSE", "INDEX", "NIFTY"),  # duplicate — deduped
                ("BSE", "INDEX", "SENSEX"),
            ],
            start=start,
            end=end,
            scope="pull:t1",
        )
        assert report.findings == 0
        assert len(calls) == 2
        expected_months = set(ist_span_partition_months(start, end))
        for call in calls:
            assert call["months"] == expected_months
            assert call["include_options"] is False
        assert calls[0]["symbol"] == "NIFTY"
        assert calls[1]["symbol"] == "SENSEX"

    def test_heal_after_pull_heals_in_span_only(self, tmp_path: Path) -> None:
        """E2E: an in-span poisoned month heals; an out-of-span one does NOT
        (the L2 bound is real, not decorative)."""
        lake = tmp_path / "lake"
        in_span = _dest(lake, month=6)
        out_of_span = _dest(lake, year=2023, month=1)
        _poison(in_span, [(_ts(0), 100.0, 10), (_ts(0), 999.0, 11)])
        _poison(
            out_of_span,
            [(_ts(0, year=2023, month=1), 100.0, 10), (_ts(0, year=2023, month=1), 999.0, 11)],
        )
        out_bytes = out_of_span.read_bytes()
        report = heal_after_pull(
            lake_root=lake,
            targets=[("NSE", "INDEX", "NIFTY")],
            start=date(2024, 6, 1),
            end=date(2024, 7, 1),
            scope="pull:t2",
        )
        assert report.healed == 1
        assert _stored_frame(in_span)["timestamp"].to_list() == [_ts(0)]
        assert out_of_span.read_bytes() == out_bytes  # out of scope — untouched

    def test_heal_after_pull_default_leaves_options_poison_untouched(self, tmp_path: Path) -> None:
        """The default (``include_options=False``) byte-preserves the options
        tree — every existing caller unchanged."""
        lake = tmp_path / "lake"
        june, _july = _poison_option_foreign(lake)
        june_bytes = june.read_bytes()
        report = heal_after_pull(
            lake_root=lake,
            targets=[("NSE", "INDEX", "NIFTY")],
            start=date(2024, 6, 1),
            end=date(2024, 7, 1),
            scope="pull:t3",
        )
        assert report.findings == 0
        assert june.read_bytes() == june_bytes

    def test_heal_after_pull_include_options_heals_in_span(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``include_options=True``: a months-in-scope poisoned options file is
        healed; ``option_files_scanned`` folds into the mass-rail DENOMINATOR;
        the ``post_pull_scan`` event carries ``option_files``
        (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001). An in-span BARS poison for a
        NON-target symbol stays invisible — the options census passes
        ``include_bars=False``, so it must be neither found, healed, nor
        counted into the denominator (round-3 mutant pin: flipping the census
        to ``include_bars=True`` rescans the whole bars tree unbounded and
        fails every count below)."""
        import polars as pl

        lake = tmp_path / "lake"
        june, july = _poison_option_foreign(lake)
        # An IN-SPAN bars dup for a symbol that is NOT a pull target: only an
        # unbounded (mutant) census could reach it.
        non_target_bars = _dest(lake)  # NSE/INDEX/NIFTY 2024-06 — in span
        _poison(non_target_bars, [(_ts(0), 100.0, 10), (_ts(0), 999.0, 11)])
        non_target_bytes = non_target_bars.read_bytes()
        captured: list[dict[str, Any]] = []
        real_heal_findings = lake_heal.heal_findings

        def _spy(findings: Any, **kwargs: Any) -> Any:
            captured.append(dict(kwargs))
            return real_heal_findings(findings, **kwargs)

        monkeypatch.setattr(lake_heal, "heal_findings", _spy)
        with capture_logs() as logs:
            report = heal_after_pull(
                lake_root=lake,
                targets=[("NSE", "FNO", "NSE-NIFTY-27Jun24-24000-CE")],
                start=date(2024, 6, 1),
                end=date(2024, 7, 1),
                scope="pull:t4",
                include_options=True,
            )
        assert report.findings == 1  # ONLY the options finding — no bars leak
        assert report.healed == 1
        july_frame = pl.read_parquet(july).sort(["side", "timestamp"])
        assert july_frame["side"].to_list() == ["CE", "PE"]  # reassigned home
        assert pl.read_parquet(june).height == 1  # source cleaned
        assert non_target_bars.read_bytes() == non_target_bytes  # never healed
        # The denominator folds the 2 scanned option files (0 bars files —
        # the non-target bars poison is EXCLUDED from the census count).
        assert captured[0]["files_scanned"] == 2
        (scan_event,) = _events(logs, "lake_heal.post_pull_scan")
        assert scan_event["option_files"] == 2
        assert scan_event["files"] == 2
        assert scan_event["findings"] == 1

    def test_heal_after_pull_options_census_call_shape(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Spy pin on the census call itself: exactly ONE extra scan, options
        tree ONLY (``include_bars is False`` — the round-3 surviving-mutant
        pin), no coordinate filters (whole-tree, months-bounded)."""
        calls: list[dict[str, Any]] = []

        def _spy_scan(lake_root: Path, **kwargs: Any) -> LakeScanReport:
            calls.append({"lake_root": lake_root, **kwargs})
            return LakeScanReport(
                findings=(),
                files_scanned=0,
                files_skipped=0,
                series_scanned=0,
                option_files_scanned=0,
                option_files_skipped=0,
                duration_s=0.0,
            )

        monkeypatch.setattr(lake_heal, "scan_lake_invariants", _spy_scan)
        start, end = date(2024, 6, 1), date(2024, 7, 1)
        heal_after_pull(
            lake_root=tmp_path,
            targets=[("NSE", "FNO", "NSE-NIFTY-27Jun24-24000-CE")],
            start=start,
            end=end,
            scope="pull:t4b",
            include_options=True,
        )
        assert len(calls) == 2  # one per-target scan + ONE options census
        census = calls[1]
        assert census["include_options"] is True
        assert census["include_bars"] is False  # never rescans the bars tree
        assert census["months"] == set(ist_span_partition_months(start, end))
        assert "exchange" not in census
        assert "segment" not in census
        assert "symbol" not in census

    def test_heal_after_pull_include_options_is_month_bounded(self, tmp_path: Path) -> None:
        """An options poison OUTSIDE the pulled span is not even READ
        (bytes + mtime pinned) — the bounded law, filename-parsed."""
        lake = tmp_path / "lake"
        june, _july = _poison_option_foreign(lake)
        before_bytes = june.read_bytes()
        before_mtime = june.stat().st_mtime_ns
        report = heal_after_pull(
            lake_root=lake,
            targets=[("NSE", "FNO", "NSE-NIFTY-27Jun24-24000-CE")],
            start=date(2023, 1, 5),
            end=date(2023, 1, 20),
            scope="pull:t5",
            include_options=True,
        )
        assert report.findings == 0
        assert june.read_bytes() == before_bytes
        assert june.stat().st_mtime_ns == before_mtime


class TestHealAfterReconcile:
    def test_clean_reconcile_returns_none(self, tmp_path: Path) -> None:
        from brutex.data.catalog_reconcile import reconcile_catalog_from_lake

        lake = tmp_path / "lake"
        _write_spot(lake, [_bar(_ts(0))])
        with Catalog.open(lake / "catalog.duckdb") as catalog:
            result = reconcile_catalog_from_lake(lake_root=lake, catalog=catalog)
            assert result.findings == ()
            assert heal_after_reconcile(result, lake_root=lake, catalog=catalog) is None

    def test_heal_after_reconcile_heals_and_reupserts(self, tmp_path: Path) -> None:
        """The healed partition's catalog row carries the ROTATED raw_hash —
        never the stale pre-heal hash the reconcile recorded."""
        from brutex.data.catalog_reconcile import reconcile_catalog_from_lake

        lake = tmp_path / "lake"
        dest = _dest(lake)
        _poison(dest, [(_ts(0), 100.0, 10), (_ts(0), 999.0, 11)])
        pre_heal_hash = hashlib.sha256(dest.read_bytes()).hexdigest()
        with Catalog.open(lake / "catalog.duckdb") as catalog:
            result = reconcile_catalog_from_lake(lake_root=lake, catalog=catalog)
            assert len(result.findings) == 1
            report = heal_after_reconcile(result, lake_root=lake, catalog=catalog)
            row = catalog.get(
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                year=2024,
                month=6,
            )
        assert report is not None
        assert report.healed == 1
        assert row is not None
        assert row.raw_hash == hashlib.sha256(dest.read_bytes()).hexdigest()
        assert row.raw_hash != pre_heal_hash


# ── L3: content token + startup sweep + sweeper lifecycle ──────────────────


class TestLakeContentToken:
    def test_token_moves_on_any_file_change(self, tmp_path: Path) -> None:
        """Add / rewrite / delete each move the token (bars AND options)."""
        import os

        lake = tmp_path / "lake"
        _write_spot(lake, [_bar(_ts(0))])
        t0 = lake_content_token(lake)
        assert t0 == lake_content_token(lake)  # stable over an unchanged tree
        # ADD (an options-tree file counts too).
        OptionParquetWriter(lake_root=lake).write(
            partition_key="NSE_FNO|13|WEEK|0|ATM|CALL",
            coordinate=_OPT_COORD,
            bars=[_option_bar()],
        )
        t1 = lake_content_token(lake)
        assert t1 != t0
        # REWRITE (same path, same size — the mtime_ns component moves it).
        dest = _dest(lake)
        os.utime(dest, ns=(1, 1))
        t2 = lake_content_token(lake)
        assert t2 != t1
        # DELETE.
        dest.unlink()
        t3 = lake_content_token(lake)
        assert t3 != t2

    def test_token_is_stat_only_no_file_reads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The token walk never OPENS a lake file — stat only (spy pin)."""
        lake = tmp_path / "lake"
        _write_spot(lake, [_bar(_ts(0))])

        def _no_reads(*_args: Any, **_kwargs: Any) -> bytes:
            raise AssertionError("lake_content_token must never read file bytes")

        monkeypatch.setattr(Path, "read_bytes", _no_reads)
        monkeypatch.setattr(Path, "open", _no_reads)
        token = lake_content_token(lake)
        assert len(token) == 64

    def test_token_of_an_empty_root_is_stable(self, tmp_path: Path) -> None:
        assert lake_content_token(tmp_path) == lake_content_token(tmp_path)

    def test_dangling_symlink_never_crashes_token(self, tmp_path: Path) -> None:
        """Round-11 fold: a dangling ``*.parquet`` symlink (bars AND options)
        no longer crashes the walk — pre-fix ``path.stat()`` followed the link
        and raised ``FileNotFoundError``, leaving the guarded L3 startup sweep
        permanently inert (``lake_heal.sweep_failed`` every boot). ``lstat``
        never follows: the dangling link contributes its OWN link metadata
        deterministically."""
        lake = tmp_path / "lake"
        _write_spot(lake, [_bar(_ts(0))])
        OptionParquetWriter(lake_root=lake).write(
            partition_key="NSE_FNO|13|WEEK|0|ATM|CALL",
            coordinate=_OPT_COORD,
            bars=[_option_bar()],
        )
        (_dest(lake).parent / "07.parquet").symlink_to(lake / "vanished-bars.parquet")
        (_opt_series_dir(lake) / "2099-01.parquet").symlink_to(lake / "vanished-opt.parquet")
        token = lake_content_token(lake)  # pre-fix: FileNotFoundError right here
        assert len(token) == 64
        assert token == lake_content_token(lake)  # deterministic over identical content

    def test_racing_deletion_mid_walk_skipped_deterministically(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Round-11 fold: a file deleted between the glob and its stat (the
        race twin of the dangling symlink) is skipped LOUDLY, and the token
        equals the one an actually-absent file yields (deterministic)."""
        import os

        lake = tmp_path / "lake"
        _write_spot(lake, [_bar(_ts(0))])
        _write_spot(lake, [_bar(_ts(0, month=7, day=1))])
        june = _dest(lake)
        real_lstat = Path.lstat

        def _racing_lstat(self: Path) -> os.stat_result:
            if self == june:
                raise FileNotFoundError(2, "raced away", str(self))
            return real_lstat(self)

        monkeypatch.setattr(Path, "lstat", _racing_lstat)
        with capture_logs() as logs:
            skipped_token = lake_content_token(lake)  # pre-fix: raises
        (skip,) = _events(logs, "lake_heal.token_stat_skipped")
        assert skip["path"] == repr(str(june))
        # Round-17: error= joins the escaped-payload convention (repr()'d) —
        # belt-and-braces here, since CPython's OSError.__str__ already
        # repr()s its filename component.
        assert skip["error"] == repr(str(FileNotFoundError(2, "raced away", str(june))))
        monkeypatch.undo()
        june.unlink()
        assert lake_content_token(lake) == skipped_token


class TestRunStartupSweep:
    def test_startup_sweep_runs_once_then_skips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(LAKE_HEAL_THROTTLE_ENV, "0")
        lake = tmp_path / "lake"
        _write_spot(lake, [_bar(_ts(0))])
        first = run_startup_sweep(lake, now=_now)  # default pause (throttle 0)
        assert first.ran is True
        assert first.scan is not None
        assert first.report is not None
        sentinel = load_last_sweep(lake)
        assert sentinel is not None
        assert sentinel["lake_token"] == first.token
        assert sentinel["mode"] == "auto"
        with capture_logs() as logs:
            second = run_startup_sweep(lake, per_file_pause=lambda: None, now=_now)
        assert second.ran is False
        assert second.token == first.token
        assert second.scan is None
        assert second.report is None
        assert len(_events(logs, "lake_heal.sweep_skip_token_match")) == 1

    def test_startup_sweep_restamps_post_heal_token(self, tmp_path: Path) -> None:
        """A healing sweep stamps the POST-heal token — the very next sweep is
        a no-op over the healed tree (never a perpetual re-sweep loop)."""
        lake = tmp_path / "lake"
        _poison(_dest(lake), [(_ts(0), 100.0, 10), (_ts(0), 999.0, 11)])
        pause_calls: list[bool] = []
        result = run_startup_sweep(lake, per_file_pause=lambda: pause_calls.append(True), now=_now)
        assert result.ran is True
        assert result.report is not None
        assert result.report.healed == 1
        assert pause_calls  # the throttle seam ran per file
        assert result.token == lake_content_token(lake)  # POST-heal recompute
        again = run_startup_sweep(lake, per_file_pause=lambda: None, now=_now)
        assert again.ran is False

    def test_startup_sweep_force_reruns(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        _write_spot(lake, [_bar(_ts(0))])
        assert run_startup_sweep(lake, per_file_pause=lambda: None, now=_now).ran is True
        forced = run_startup_sweep(lake, force=True, per_file_pause=lambda: None, now=_now)
        assert forced.ran is True

    def test_startup_sweep_corrupt_sentinel_fails_open_to_sweeping(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        _write_spot(lake, [_bar(_ts(0))])
        run_startup_sweep(lake, per_file_pause=lambda: None, now=_now)
        sentinel = lake / ".lake_heal" / "last_sweep.json"
        sentinel.write_text("{corrupt", encoding="utf-8")
        assert run_startup_sweep(lake, per_file_pause=lambda: None, now=_now).ran is True

    def test_startup_sweep_heals_option_foreign_and_stamps(self, tmp_path: Path) -> None:
        """G9(a): an options-foreign poison rides the DEFAULT L3 sweep
        automatically (``include_options=True`` default) — healed + sentinel
        stamped on the clean completion (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001)."""
        import polars as pl

        lake = tmp_path / "lake"
        june, july = _poison_option_foreign(lake)
        result = run_startup_sweep(lake, per_file_pause=lambda: None, now=_now)
        assert result.ran is True
        assert result.report is not None
        assert result.report.healed == 1
        assert pl.read_parquet(july).height == 2  # both keys home
        assert pl.read_parquet(june).height == 1  # source cleaned
        assert load_last_sweep(lake) is not None  # stamped — genuinely complete
        again = run_startup_sweep(lake, per_file_pause=lambda: None, now=_now)
        assert again.ran is False  # token match over the healed tree

    def test_startup_sweep_mass_rail_counts_option_files_in_denominator(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Round-16 fold (surviving-mutant pin): run_startup_sweep's OWN
        denominator fold ``scan.files_scanned + scan.option_files_scanned``
        is what arms the rail on an OPTIONS-ONLY lake. Dropping the option
        term (the mutant that survived the whole prior suite — the earlier
        rail pin computed the denominator inside the TEST body and called
        heal_findings() directly, so it never saw the L3 fold) reads
        ``files_scanned=0``, the ``>= MASS_HEAL_MIN_FILES`` small-lake guard
        disarms, and the boot sweep HEALS the mass-poisoned tree the rail
        exists to refuse. The real 20-file threshold is deliberately NOT
        monkeypatched — that is the mutant's escape hatch."""
        monkeypatch.delenv(LAKE_HEAL_ENV, raising=False)
        lake = tmp_path / "lake"
        _seed_mass_poisoned_option_series(lake)
        before = _tree_state(lake)
        with capture_logs() as logs:
            result = run_startup_sweep(lake, per_file_pause=lambda: None, now=_now)
        assert result.ran is True
        assert result.scan is not None
        assert result.scan.files_scanned == 0  # options-ONLY lake: no bars term
        assert result.scan.option_files_scanned == 21
        assert len(result.scan.findings) == 11
        assert result.report is not None
        assert result.report.refused_mass_heal is True  # mutant: heals instead
        assert result.report.healed == 0
        assert _tree_state(lake) == before  # not one byte moved
        assert load_last_sweep(lake) is None  # a refused sweep never stamps
        (refusal,) = _events(logs, "lake_heal.mass_heal_refused")
        assert refusal["files_scanned"] == 21  # bars 0 + options 21

    def test_sweep_proceeds_past_dangling_symlinks_and_heals(self, tmp_path: Path) -> None:
        """Round-11 fold: pre-fix ONE dangling ``*.parquet`` symlink crashed
        ``lake_content_token`` before the scan even started — the guarded
        ``StartupSweeper.run_sync`` logged ``lake_heal.sweep_failed`` on EVERY
        boot and never healed anything (permanently inert, loud but dead).
        Post-fix the sweep proceeds and heals the sibling poison."""
        lake = tmp_path / "lake"
        dest = _poison_option_lake(lake)  # a real healable (side, ts) dup
        (dest.parent / "2099-01.parquet").symlink_to(lake / "vanished-opt.parquet")
        (lake / "bars").mkdir(parents=True, exist_ok=True)
        (lake / "bars" / "dangling.parquet").symlink_to(lake / "vanished-bars.parquet")
        result = run_startup_sweep(lake, per_file_pause=lambda: None, now=_now)
        assert result.ran is True
        assert result.report is not None
        assert result.report.healed == 1  # the dup healed — no longer inert
        assert result.report.errors == 0
        # And the production guarded wrapper no longer degrades to None.
        sweeper_result = StartupSweeper(lake).run_sync()
        assert sweeper_result is not None


class TestSentinelStampGate:
    """The sentinel stamps ONLY a genuinely-completed auto sweep (fix-pass).

    A refused / observe / errored / partially-healed sweep must SKIP the stamp
    (``lake_heal.sweep_sentinel_skipped``) so the next boot re-attempts —
    stamping would token-match past live poison and silently stop L3
    self-healing + its one-shot alert. The stamp cases (zero-finding clean
    sweep, healed-all sweep) are pinned by ``TestRunStartupSweep``.
    """

    def _skip_events(self, logs: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
        return _events(logs, "lake_heal.sweep_sentinel_skipped")

    def test_observe_sweep_skips_sentinel_and_reattempts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(LAKE_HEAL_ENV, "observe")
        lake = tmp_path / "lake"
        _poison(_dest(lake), [(_ts(0), 100.0, 10), (_ts(0), 999.0, 11)])
        with capture_logs() as logs:
            result = run_startup_sweep(lake, per_file_pause=lambda: None, now=_now)
        assert result.ran is True
        assert load_last_sweep(lake) is None  # NOT stamped
        (skip,) = self._skip_events(logs)
        assert skip["reason"] == "observe_mode"
        assert skip["log_level"] == "warning"
        # The next boot re-attempts (no sentinel → no token match).
        assert run_startup_sweep(lake, per_file_pause=lambda: None, now=_now).ran is True

    def test_refused_mass_heal_skips_sentinel_and_reattempts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(LAKE_HEAL_ENV, raising=False)
        monkeypatch.setattr(lake_heal, "MASS_HEAL_MIN_FILES", 1)
        lake = tmp_path / "lake"
        _poison(_dest(lake), [(_ts(0), 100.0, 10), (_ts(0), 999.0, 11)])
        with capture_logs() as logs:
            result = run_startup_sweep(lake, per_file_pause=lambda: None, now=_now)
        assert result.ran is True
        assert result.report is not None
        assert result.report.refused_mass_heal is True
        assert load_last_sweep(lake) is None
        (skip,) = self._skip_events(logs)
        assert skip["reason"] == "mass_heal_refused"
        assert run_startup_sweep(lake, per_file_pause=lambda: None, now=_now).ran is True

    def test_errored_sweep_skips_sentinel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(LAKE_HEAL_ENV, raising=False)
        lake = tmp_path / "lake"
        _poison(_dest(lake), [(_ts(0), 100.0, 10), (_ts(0), 999.0, 11)])

        def _boom(self: ParquetWriter, dest: Path, **_kw: Any) -> Any:
            raise WriterError("simulated per-finding heal failure")

        monkeypatch.setattr(ParquetWriter, "heal_partition", _boom)
        with capture_logs() as logs:
            result = run_startup_sweep(lake, per_file_pause=lambda: None, now=_now)
        assert result.ran is True
        assert result.report is not None
        assert result.report.errors == 1
        assert load_last_sweep(lake) is None
        (skip,) = self._skip_events(logs)
        assert skip["reason"] == "heal_errors"

    def test_partially_healed_sweep_skips_sentinel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A probe-abstain leaves ``healed < findings`` → no stamp (re-attempt)."""
        from brutex.data.parquet_writer import PartitionHealResult

        monkeypatch.delenv(LAKE_HEAL_ENV, raising=False)
        lake = tmp_path / "lake"
        _poison(_dest(lake), [(_ts(0), 100.0, 10), (_ts(0), 999.0, 11)])

        def _abstain(self: ParquetWriter, dest: Path, **_kw: Any) -> PartitionHealResult:
            return PartitionHealResult(
                path=dest,
                changed=False,
                kept=0,
                dropped_duplicates=0,
                dropped_removed=0,
                raw_hash_before="0" * 64,
                raw_hash_after="0" * 64,
                write=None,
            )

        monkeypatch.setattr(ParquetWriter, "heal_partition", _abstain)
        with capture_logs() as logs:
            result = run_startup_sweep(lake, per_file_pause=lambda: None, now=_now)
        assert result.ran is True
        assert result.report is not None
        assert result.report.healed == 0
        assert result.report.skipped == 1
        assert load_last_sweep(lake) is None
        (skip,) = self._skip_events(logs)
        assert skip["reason"] == "unhealed_findings"

    def test_errored_option_foreign_sweep_skips_sentinel_and_reattempts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G9(b): an ERRORED options batch must NOT stamp the sentinel — the
        next boot re-attempts instead of token-matching past live poison
        (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001)."""
        monkeypatch.delenv(LAKE_HEAL_ENV, raising=False)
        lake = tmp_path / "lake"
        _poison_option_foreign(lake)

        def _boom(self: OptionParquetWriter, dest: Path) -> Any:
            raise OptionWriterError("simulated corrupt option source")

        monkeypatch.setattr(OptionParquetWriter, "read_partition_frame", _boom)
        with capture_logs() as logs:
            result = run_startup_sweep(lake, per_file_pause=lambda: None, now=_now)
        assert result.ran is True
        assert result.report is not None
        assert result.report.errors == 1
        assert load_last_sweep(lake) is None  # NOT stamped
        (skip,) = self._skip_events(logs)
        assert skip["reason"] == "heal_errors"
        # The next boot re-attempts (no sentinel → no token match).
        second = run_startup_sweep(lake, per_file_pause=lambda: None, now=_now)
        assert second.ran is True


class TestStartupSweeper:
    def test_run_sync_success_with_catalog_and_audit(self, tmp_path: Path) -> None:
        """The full production body: real audit chain, real catalog opened at
        catalog_path (and closed), the L3 alert binding fired best-effort, the
        generation seam bumped."""
        lake = tmp_path / "lake"
        _poison(_dest(lake), [(_ts(0), 100.0, 10), (_ts(0), 999.0, 11)])
        healed_calls: list[bool] = []
        sweeper = StartupSweeper(
            lake,
            on_healed=lambda: healed_calls.append(True),
            catalog_path=lake / "catalog.duckdb",
        )
        result = sweeper.run_sync()
        assert result is not None
        assert result.ran is True
        assert result.report is not None
        assert result.report.healed == 1
        assert healed_calls == [True]
        assert (lake / "audit" / "lake-heal.jsonl").exists()
        with Catalog.open(lake / "catalog.duckdb") as catalog:
            row = catalog.get(
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                year=2024,
                month=6,
            )
        assert row is not None  # the heal upserted through the opened catalog

    def test_run_sync_without_catalog_path(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        _write_spot(lake, [_bar(_ts(0))])
        result = StartupSweeper(lake).run_sync()
        assert result is not None
        assert result.ran is True
        assert not (lake / "catalog.duckdb").exists()

    def test_run_sync_never_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The blanket boot guard: an injected bomb → ``sweep_failed`` + None."""

        def _boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("simulated sweep bomb")

        monkeypatch.setattr(lake_heal, "run_startup_sweep", _boom)
        with capture_logs() as logs:
            result = StartupSweeper(tmp_path).run_sync()
        assert result is None
        failures = _events(logs, "lake_heal.sweep_failed")
        assert len(failures) == 1
        assert failures[0]["log_level"] == "warning"

    def test_startup_sweeper_spawn_seam(self, tmp_path: Path) -> None:
        """The injectable spawn runs the body synchronously — no real thread
        under coverage."""
        lake = tmp_path / "lake"
        _write_spot(lake, [_bar(_ts(0))])
        bodies: list[Any] = []
        sweeper = StartupSweeper(lake, spawn=bodies.append)
        sweeper.start()
        (body,) = bodies
        body()  # run synchronously
        assert load_last_sweep(lake) is not None  # the sweep really ran

    def test_default_spawn_runs_body_on_a_daemon_thread(self) -> None:
        ran = threading.Event()
        lake_heal._default_spawn(ran.set)
        assert ran.wait(timeout=5.0)


class TestOpenLakeHealAudit:
    def test_open_creates_then_resumes_the_chain(self, tmp_path: Path) -> None:
        from brutex.orchestrator.audit import verify_chain

        first = open_lake_heal_audit(tmp_path)
        assert first is not None
        first.append(action=AUDIT_HEAL_DEDUP, subject="a", payload={})
        close = getattr(first, "close", None)
        assert callable(close)
        close()
        second = open_lake_heal_audit(tmp_path)
        assert second is not None
        second.append(action=AUDIT_HEAL_DEDUP, subject="b", payload={})
        close2 = getattr(second, "close", None)
        assert callable(close2)
        close2()
        assert verify_chain(tmp_path / "audit" / "lake-heal.jsonl") == 2

    def test_open_failure_is_best_effort_none(self, tmp_path: Path) -> None:
        """An unopenable audit path → None + ``audit_open_failed`` (the heal
        proceeds un-audited rather than not at all)."""
        (tmp_path / "audit").write_text("a file where the dir should be", encoding="utf-8")
        with capture_logs() as logs:
            sink = open_lake_heal_audit(tmp_path)
        assert sink is None
        failures = _events(logs, "lake_heal.audit_open_failed")
        assert len(failures) == 1
        assert failures[0]["log_level"] == "warning"


class TestLakeHealAuditContext:
    """The COMMON close idiom every heal seam shares (L1/L2/L3)."""

    def test_yields_real_sink_and_closes_on_exit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        closed: list[bool] = []

        class _Sink:
            def append(self, **_kw: object) -> object:
                return None

            def close(self) -> None:
                closed.append(True)

        sink = _Sink()
        monkeypatch.setattr(lake_heal, "open_lake_heal_audit", lambda _root: sink)
        with lake_heal.lake_heal_audit(tmp_path) as yielded:
            assert yielded is sink
            assert closed == []
        assert closed == [True]

    def test_none_sink_is_close_free(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(lake_heal, "open_lake_heal_audit", lambda _root: None)
        with lake_heal.lake_heal_audit(tmp_path) as yielded:
            assert yielded is None

    def test_closes_even_when_the_body_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        closed: list[bool] = []

        class _Sink:
            def append(self, **_kw: object) -> object:
                return None

            def close(self) -> None:
                closed.append(True)

        def _boom() -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(lake_heal, "open_lake_heal_audit", lambda _root: _Sink())
        with pytest.raises(RuntimeError, match="boom"), lake_heal.lake_heal_audit(tmp_path):
            _boom()  # raising via a helper keeps the assert below reachable to CodeQL
        assert closed == [True]


class TestOpenLakeCatalog:
    """The L2 seams' best-effort catalog opener (DEC-LAKE-HEAL-001 fix-pass)."""

    def test_missing_file_returns_none_and_creates_nothing(self, tmp_path: Path) -> None:
        assert lake_heal.open_lake_catalog(tmp_path) is None
        assert not (tmp_path / "catalog.duckdb").exists()  # never conjured

    def test_existing_file_opens_a_usable_catalog(self, tmp_path: Path) -> None:
        path = tmp_path / "catalog.duckdb"
        Catalog.open(path).close()  # a real pre-existing catalog file
        catalog = lake_heal.open_lake_catalog(tmp_path)
        assert isinstance(catalog, Catalog)
        assert (
            catalog.get(
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                year=2024,
                month=6,
            )
            is None
        )  # usable (open, queryable) — the caller owns the close
        catalog.close()

    def test_open_error_degrades_to_none_and_logs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "catalog.duckdb").write_text("present", encoding="utf-8")

        class _BoomCatalog:
            @staticmethod
            def open(_path: Path) -> Any:
                raise RuntimeError("simulated catalog open failure")

        monkeypatch.setattr(lake_heal, "Catalog", _BoomCatalog)
        with capture_logs() as logs:
            assert lake_heal.open_lake_catalog(tmp_path) is None
        (warn,) = _events(logs, "lake_heal.catalog_open_failed")
        assert warn["log_level"] == "warning"
        assert "simulated catalog open failure" in str(warn["error"])


class TestSentinelShape:
    def test_sentinel_payload_shape(self, tmp_path: Path) -> None:
        """The sentinel carries the documented keys (the census reads them)."""
        lake = tmp_path / "lake"
        _write_spot(lake, [_bar(_ts(0))])
        run_startup_sweep(lake, per_file_pause=lambda: None, now=_now)
        raw = json.loads((lake / ".lake_heal" / "last_sweep.json").read_text(encoding="utf-8"))
        assert raw["version"] == 1
        assert raw["completed_utc"] == _NOW.isoformat()
        assert raw["files_scanned"] == 1
        assert raw["findings"] == 0
        assert raw["healed"] == 0
        assert raw["mode"] == "auto"


# ── residual branch pins (whitebox where the design names privates) ────────


class TestResidualBranches:
    def test_observe_names_a_drift_finding_without_on_report(self, tmp_path: Path) -> None:
        """Observe detail names a DRIFT finding (its human token) and the
        on_report seam is optional."""
        from brutex.data.lake_invariants import LegacyKeyDrift

        finding = LegacyKeyDrift(
            exchange="NSE",
            segment="CASH",
            drifted_symbol="NSE-M&M",
            canonical_symbol="M&M",
            drifted_dir=tmp_path / "bars" / "NSE" / "CASH" / "NSE-M&M",
            intervals=("1minute",),
            files=1,
        )
        with capture_logs() as logs:
            report = heal_findings(
                (finding,), lake_root=tmp_path, files_scanned=1, mode="observe", now=_now
            )
        assert report.mode == "observe"
        (warn,) = _events(logs, "lake_heal.observe_only")
        assert warn["detail"] == ["NSE/CASH/NSE-M&M"]

    def test_mass_heal_refusal_without_on_report(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(LAKE_HEAL_ENV, raising=False)
        coords = PartitionCoords(
            exchange="NSE", segment="CASH", symbol="X", interval="1minute", year=2024, month=6
        )
        findings = tuple(
            IntraFileDup(
                path=Path(f"/nonexistent/{i}.parquet"),
                coords=coords,
                physical_rows=2,
                unique_keys=1,
            )
            for i in range(11)
        )
        report = heal_findings(findings, lake_root=Path("/nonexistent"), files_scanned=21, now=_now)
        assert report.refused_mass_heal is True

    def test_reassign_on_a_non_minute_interval_triggers_no_recall(self, tmp_path: Path) -> None:
        """Foreign rows in a derived-TF file are still reassigned home, but a
        non-1minute heal never drives the recall."""
        lake = tmp_path / "lake"
        _poison(
            _dest(lake, interval="5minute", month=6),
            [(_ts(2, month=5), 55.0, 7), (_ts(0, month=6), 101.0, 12)],
        )
        scan = scan_lake_invariants(lake)
        report = heal_findings(
            scan.findings,
            lake_root=lake,
            files_scanned=scan.files_scanned,
            mode="auto",
            recall=True,
            now=_now,
        )
        assert report.healed == 1
        assert report.recall is None
        may_tf = _stored_frame(_dest(lake, interval="5minute", month=5))
        assert may_tf["timestamp"].to_list() == [_ts(2, month=5)]

    def test_drift_merge_without_minute_files_triggers_no_recall(self, tmp_path: Path) -> None:
        """A drifted dir holding ONLY a derived-TF dir merges nothing —
        quarantine-moved files, no 1minute touch, no recall."""
        lake = tmp_path / "lake"
        _write_spot(lake, [_bar(_ts(0))], segment="CASH", symbol="M&M")
        _poison(
            lake / "bars" / "NSE" / "CASH" / "NSE-M&M" / "5minute" / "2024" / "06.parquet",
            [(_ts(0), 999.0, 20)],
        )
        scan = scan_lake_invariants(lake)
        drift = [f for f in scan.findings if type(f).__name__ == "LegacyKeyDrift"]
        assert len(drift) == 1
        report = heal_findings(
            tuple(drift),
            lake_root=lake,
            files_scanned=scan.files_scanned,
            mode="auto",
            recall=True,
            now=_now,
        )
        assert report.healed == 1
        assert report.recall is None
        assert not (lake / "bars" / "NSE" / "CASH" / "NSE-M&M").exists()

    def test_sync_catalog_defensive_unchanged_result_is_a_noop(self, tmp_path: Path) -> None:
        """Whitebox: a changed=False result reaches neither the delete nor the
        upsert arm (defensive — the healers return before calling this)."""
        from brutex.data.parquet_writer import PartitionHealResult

        lake = tmp_path / "lake"
        with Catalog.open(lake / "catalog.duckdb") as catalog:
            state = lake_heal._BatchState(
                lake_root=lake,
                quarantine_root=lake / ".quarantine" / _QUARANTINE_TS,
                audit=None,
                catalog=catalog,
                writer=ParquetWriter(lake_root=lake),
                option_writer=OptionParquetWriter(lake_root=lake),
            )
            lake_heal._sync_catalog_after_heal(
                state,
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                year=2024,
                month=6,
                result=PartitionHealResult(
                    path=lake / "x.parquet",
                    changed=False,
                    kept=1,
                    dropped_duplicates=0,
                    dropped_removed=0,
                    raw_hash_before="a" * 64,
                    raw_hash_after="a" * 64,
                    write=None,
                ),
            )
            assert catalog.count() == 0

    def test_prune_empty_dirs_missing_root_is_a_noop(self, tmp_path: Path) -> None:
        lake_heal._prune_empty_dirs(tmp_path / "does-not-exist")  # no raise

    def test_first_day_after_december_rolls_the_year(self) -> None:
        assert lake_heal._first_day_after(2024, 12) == date(2025, 1, 1)
        assert lake_heal._first_day_after(2024, 6) == date(2024, 7, 1)
