"""Unit suite for the Wave-D Rust heal seam in ``brutex.data.lake_heal``.

DEC-RUST-LAKE-WAVED-001 §B2 (W-2): the seam pieces —
``_serialize_rust_findings`` (the D-10 plain-dict finding wire),
``_rust_heal_on_event`` (the ONE side-effect replay channel),
``_rust_heal_route`` (the flag gate + outcome replay onto ``_BatchState``),
the ``heal_findings`` loop bypass and the ``lake_content_token`` routing —
tested with FAKE facade functions (monkeypatched at
``brutex.data.lake_rust_waved``, where the seam's call-time imports
resolve), so the suite runs with NO wheel installed. The real-wheel parity
gate is ``tests/integration/test_waved_heal_differential.py``.

Flag OFF (the default) is pinned NEVER-ROUTES with bombed facade functions —
the byte-identity contract's structural half.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from brutex.data import lake_heal, lake_rust_waved
from brutex.data.errors import WriterError
from brutex.data.lake_heal import (
    HealAction,
    _BatchState,
    _lake_content_token_python,
    _replay_token_skip,
    _rust_heal_epoch_us,
    _rust_heal_on_event,
    _rust_heal_route,
    _serialize_rust_findings,
    heal_findings,
    lake_content_token,
)
from brutex.data.lake_invariants import (
    ForeignMonthRows,
    IntraFileDup,
    LegacyKeyDrift,
    OptionForeignMonthRows,
    OptionIntraFileDup,
    PartitionCoords,
    SchemaAlienFile,
)
from brutex.data.option_parquet_writer import OptionParquetWriter
from brutex.data.parquet_writer import ParquetWriter
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _fresh_facade(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(lake_rust_waved.LAKE_WAVED_RUST_ENV, raising=False)
    lake_rust_waved.reset_lake_rust_waved_for_tests()
    yield
    lake_rust_waved.reset_lake_rust_waved_for_tests()


_COORDS = PartitionCoords(
    exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", year=2024, month=6
)


def _dup(path: Path) -> IntraFileDup:
    return IntraFileDup(path=path, coords=_COORDS, physical_rows=3, unique_keys=2)


def _state(tmp_path: Path) -> _BatchState:
    lake = tmp_path / "lake"
    lake.mkdir(exist_ok=True)
    return _BatchState(
        lake_root=lake,
        quarantine_root=lake / ".quarantine" / "20260615T000000Z",
        audit=None,
        catalog=None,
        writer=ParquetWriter(lake_root=lake),
        option_writer=OptionParquetWriter(lake_root=lake),
    )


class _RecordingAudit:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str, dict[str, Any] | None]] = []

    def append(self, *, action: str, subject: str, payload: dict[str, Any] | None = None) -> object:
        self.lines.append((action, subject, payload))
        return None


class _RecordingCatalog:
    """A minimal catalog double recording upsert/delete calls."""

    def __init__(self) -> None:
        self.upserts: list[Any] = []
        self.deletes: list[dict[str, Any]] = []

    def upsert(self, entry: Any) -> None:
        self.upserts.append(entry)

    def delete_partition(self, **kwargs: Any) -> None:
        self.deletes.append(kwargs)


class TestSerializeRustFindings:
    def test_dup_serializes_with_coords(self, tmp_path: Path) -> None:
        out = _serialize_rust_findings([_dup(tmp_path / "06.parquet")])
        assert out == [
            {
                "kind": "IntraFileDup",
                "path": str(tmp_path / "06.parquet"),
                "exchange": "NSE",
                "segment": "INDEX",
                "symbol": "NIFTY",
                "interval": "1minute",
                "year": 2024,
                "month": 6,
                "has_coords": True,
            }
        ]

    def test_option_dup_serializes_with_series(self, tmp_path: Path) -> None:
        finding = OptionIntraFileDup(
            path=tmp_path / "2024-06.parquet", series="SERIES", physical_rows=2, unique_keys=1
        )
        out = _serialize_rust_findings([finding])
        assert out is not None
        assert out[0]["kind"] == "OptionIntraFileDup"
        assert out[0]["series"] == "SERIES"
        assert out[0]["has_coords"] is False

    def test_alien_serializes_both_arms(self, tmp_path: Path) -> None:
        bars_alien = SchemaAlienFile(path=tmp_path / "06.parquet", reason="r", coords=_COORDS)
        opt_alien = SchemaAlienFile(path=tmp_path / "2024-06.parquet", reason="r", series="S")
        out = _serialize_rust_findings([bars_alien, opt_alien])
        assert out is not None
        assert out[0]["has_coords"] is True
        assert out[0]["exchange"] == "NSE"
        assert out[1]["has_coords"] is False
        assert out[1]["series"] == "S"
        # A coords-less alien with a None series serializes the empty string
        # (the crate's get_str missing-key default — never a None crossing).
        none_series = SchemaAlienFile(path=tmp_path / "x.parquet", reason="r")
        solo = _serialize_rust_findings([none_series])
        assert solo is not None
        assert solo[0]["series"] == ""

    def test_multi_file_kinds_serialize_since_w2b(self, tmp_path: Path) -> None:
        """The W-2 short-circuit-to-``None`` contract is consciously FLIPPED
        (W-2b, deviation D-22): the three multi-file kinds now serialize onto
        the path-keyed crate wire — ``LegacyKeyDrift`` maps
        ``drifted_dir``→``path`` / ``drifted_symbol``→``symbol`` /
        ``canonical_symbol``→``canonical`` — and a mixed batch serializes
        WHOLE (the crate engages every kind).
        """
        foreign = ForeignMonthRows(
            path=Path("/x/07.parquet"), coords=_COORDS, foreign_rows=1, overlap_rows=0
        )
        drift = LegacyKeyDrift(
            exchange="NSE",
            segment="CASH",
            drifted_symbol="NSE-M&M",
            canonical_symbol="M&M",
            drifted_dir=Path("/x"),
            intervals=("1minute",),
            files=1,
        )
        option_foreign = OptionForeignMonthRows(
            path=Path("/x/2024-06.parquet"),
            series="S",
            year=2024,
            month=6,
            foreign_rows=1,
            overlap_rows=0,
        )
        out = _serialize_rust_findings(
            [_dup(tmp_path / "06.parquet"), foreign, drift, option_foreign]
        )
        assert len(out) == 4
        assert out[1] == {
            "kind": "ForeignMonthRows",
            "path": str(Path("/x/07.parquet")),
            "exchange": "NSE",
            "segment": "INDEX",
            "symbol": "NIFTY",
            "interval": "1minute",
            "year": 2024,
            "month": 6,
            "has_coords": True,
        }
        assert out[2] == {
            "kind": "LegacyKeyDrift",
            "path": str(Path("/x")),
            "exchange": "NSE",
            "segment": "CASH",
            "symbol": "NSE-M&M",
            "canonical": "M&M",
            "has_coords": False,
        }
        assert out[3] == {
            "kind": "OptionForeignMonthRows",
            "path": str(Path("/x/2024-06.parquet")),
            "series": "S",
            "year": 2024,
            "month": 6,
            "has_coords": False,
        }


class TestRustHealOnEvent:
    def test_beat_delegates_to_progress_beat(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        beats: list[int] = []
        monkeypatch.setattr(lake_heal, "emit_progress_beat", lambda: beats.append(1))
        handle = _rust_heal_on_event(_state(tmp_path))
        handle("beat", {})
        assert beats == [1]

    def test_log_event_applies_the_repr_laws(self, tmp_path: Path) -> None:
        handle = _rust_heal_on_event(_state(tmp_path))
        with capture_logs() as logs:
            handle(
                "log",
                {
                    "event": "lake_heal.partition_healed",
                    "partition": "options/S\n/x.parquet",
                    "partition_repr": True,
                    "kept": 1,
                    "dropped": 1,
                },
            )
            handle(
                "log",
                {
                    "event": "lake_heal.partition_error",
                    "partition": "bars/x.parquet",
                    "partition_repr": False,
                    "finding": "IntraFileDup",
                    "error": "boom\npayload",
                },
            )
        assert logs[0]["event"] == "lake_heal.partition_healed"
        assert logs[0]["partition"] == repr("options/S\n/x.parquet")
        assert logs[0]["log_level"] == "warning"
        assert "partition_repr" not in logs[0]
        assert logs[1]["partition"] == "bars/x.parquet"  # bars stay raw
        assert logs[1]["error"] == repr("boom\npayload")  # the round-16 law

    def test_audit_event_delegates_to_audit_append(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        audit = _RecordingAudit()
        state.audit = audit
        handle = _rust_heal_on_event(state)
        handle(
            "audit",
            {"action": "LAKE_HEAL_DEDUP", "subject": "bars/x.parquet", "payload": {"kept": 1}},
        )
        assert audit.lines == [("LAKE_HEAL_DEDUP", "bars/x.parquet", {"kept": 1})]

    def test_file_removed_move_invalidates_manifests(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        invalidated: list[Path] = []
        removed: list[tuple[Path, Path]] = []
        monkeypatch.setattr(lake_heal, "invalidate_manifest", invalidated.append)
        monkeypatch.setattr(
            lake_heal,
            "_remove_dataset_stats_for",
            lambda root, source: removed.append((root, source)),
        )
        state = _state(tmp_path)
        handle = _rust_heal_on_event(state)
        handle("file_removed_move", {"path": str(tmp_path / "lake" / "bars" / "x.parquet")})
        assert invalidated == [state.lake_root]
        assert removed == [(state.lake_root, tmp_path / "lake" / "bars" / "x.parquet")]

    def test_catalog_upsert_builds_the_writer_entry(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        catalog = _RecordingCatalog()
        state.catalog = catalog  # type: ignore[assignment]
        handle = _rust_heal_on_event(state)
        payload = {
            "exchange": "NSE",
            "segment": "INDEX",
            "symbol": "NIFTY",
            "interval": "1minute",
            "year": 2024,
            "month": 6,
            "raw_hash": "a" * 64,
            "row_count": 2,
            "first_ts_us": 1_717_387_200_000_000,
            "last_ts_us": 1_717_387_260_000_000,
        }
        handle("catalog_upsert", payload)
        assert len(catalog.upserts) == 1
        entry = catalog.upserts[0]
        assert entry.raw_hash == "a" * 64
        assert entry.row_count == 2
        # year/month derive from first_ts via make_catalog_entry (ONE law).
        assert (entry.year, entry.month) == (2024, 6)
        assert entry.first_ts == datetime(2024, 6, 3, 4, 0, tzinfo=UTC)

    def test_catalog_events_are_noops_without_a_catalog(self, tmp_path: Path) -> None:
        handle = _rust_heal_on_event(_state(tmp_path))
        handle(
            "catalog_upsert",
            {
                "exchange": "NSE",
                "segment": "INDEX",
                "symbol": "NIFTY",
                "interval": "1minute",
                "year": 2024,
                "month": 6,
                "raw_hash": "a" * 64,
                "row_count": 2,
                "first_ts_us": 0,
                "last_ts_us": 0,
            },
        )
        handle(
            "catalog_delete",
            {
                "exchange": "NSE",
                "segment": "INDEX",
                "symbol": "NIFTY",
                "interval": "1minute",
                "year": 2024,
                "month": 6,
            },
        )

    def test_catalog_delete_delegates(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        catalog = _RecordingCatalog()
        state.catalog = catalog  # type: ignore[assignment]
        handle = _rust_heal_on_event(state)
        handle(
            "catalog_delete",
            {
                "exchange": "NSE",
                "segment": "INDEX",
                "symbol": "NIFTY",
                "interval": "1minute",
                "year": 2024,
                "month": 6,
            },
        )
        assert catalog.deletes == [
            {
                "exchange": "NSE",
                "segment": "INDEX",
                "symbol": "NIFTY",
                "interval": "1minute",
                "year": 2024,
                "month": 6,
            }
        ]

    def test_bars_stats_record_delegates_to_the_writer_hook(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = _state(tmp_path)
        recorded: list[tuple[Path, Any]] = []
        monkeypatch.setattr(
            type(state.writer),
            "_record_stats_hook",
            lambda self, dest, result: recorded.append((dest, result)),
        )
        handle = _rust_heal_on_event(state)
        handle(
            "bars_stats_record",
            {
                "path": str(state.lake_root / "bars" / "x.parquet"),
                "rows": 2,
                "raw_hash": "b" * 64,
                "first_ts_us": 1_000_000,
                "last_ts_us": 2_000_000,
            },
        )
        assert len(recorded) == 1
        dest, result = recorded[0]
        assert dest == state.lake_root / "bars" / "x.parquet"
        assert result.row_count == 2
        assert result.raw_hash == "b" * 64
        assert result.file_rewritten is True
        assert result.bars_added == 0

    def test_option_stats_record_skips_when_manifest_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(lake_heal, "stats_manifest_enabled", lambda: False)
        handle = _rust_heal_on_event(_state(tmp_path))
        # Path does not exist — the read inside the enabled arm would raise,
        # so a clean return proves the disabled short-circuit.
        handle("option_stats_record", {"path": str(tmp_path / "absent.parquet")})

    def test_option_stats_record_rereads_and_delegates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import polars as pl
        from brutex.data import option_parquet_writer as opw

        state = _state(tmp_path)
        dest = state.lake_root / "options" / "S" / "2024-06.parquet"
        dest.parent.mkdir(parents=True)
        frame = pl.DataFrame(
            {
                "side": ["CE"],
                "timestamp": [datetime(2024, 6, 3, 4, 0, tzinfo=UTC)],
            }
        )
        frame.write_parquet(dest)
        monkeypatch.setattr(lake_heal, "stats_manifest_enabled", lambda: True)
        recorded: list[tuple[Path, Path, int]] = []
        monkeypatch.setattr(
            opw,
            "_record_option_stats",
            lambda lake_root, *, dest, frame: recorded.append((lake_root, dest, frame.height)),
        )
        handle = _rust_heal_on_event(state)
        handle("option_stats_record", {"path": str(dest)})
        assert recorded == [(state.lake_root, dest, 1)]

    def test_log_level_info_wire_and_repr_fields(self, tmp_path: Path) -> None:
        """The W-2b log wires: ``level="info"`` routes to ``_LOG.info`` (the
        writer's default-level ``lake.partition.write`` telemetry) and the
        ``repr_fields`` name-list generalizes the #907 repr law to arbitrary
        fields (``drift_merged`` drifted/canonical)."""
        handle = _rust_heal_on_event(_state(tmp_path))
        with capture_logs() as logs:
            handle(
                "log",
                {
                    "event": "lake.partition.write",
                    "level": "info",
                    "partition": "bars/x.parquet",
                    "rows": 2,
                },
            )
            handle(
                "log",
                {
                    "event": "lake_heal.drift_merged",
                    "repr_fields": ["drifted", "canonical"],
                    "drifted": "NSE-M&M",
                    "canonical": "M&M",
                    "files": 1,
                },
            )
        assert logs[0]["event"] == "lake.partition.write"
        assert logs[0]["log_level"] == "info"
        assert "level" not in logs[0]
        assert logs[0]["partition"] == "bars/x.parquet"
        assert logs[1]["log_level"] == "warning"
        assert "repr_fields" not in logs[1]
        assert logs[1]["drifted"] == repr("NSE-M&M")
        assert logs[1]["canonical"] == repr("M&M")
        assert logs[1]["files"] == 1

    def test_bars_stats_remove_invalidates_and_removes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bars wholly-removed twin hooks: manifest invalidation + the
        writer's OWN self-gated stats-remove hook (verbatim order)."""
        state = _state(tmp_path)
        invalidated: list[Path] = []
        removed: list[Path] = []
        monkeypatch.setattr(lake_heal, "invalidate_manifest", invalidated.append)
        monkeypatch.setattr(
            type(state.writer),
            "_remove_stats_hook",
            lambda self, dest: removed.append(dest),
        )
        handle = _rust_heal_on_event(state)
        dest = state.lake_root / "bars" / "x.parquet"
        handle("bars_stats_remove", {"path": str(dest)})
        assert invalidated == [state.lake_root]
        assert removed == [dest]

    def test_option_stats_remove_delegates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The options wholly-removed twin: stats-remove ONLY — the options
        tree is deliberately unmanifested (no invalidate_manifest)."""
        from brutex.data import option_parquet_writer as opw

        state = _state(tmp_path)
        invalidated: list[Path] = []
        removed: list[tuple[Path, Path]] = []
        monkeypatch.setattr(lake_heal, "invalidate_manifest", invalidated.append)
        monkeypatch.setattr(
            opw,
            "_remove_option_stats",
            lambda lake_root, *, dest: removed.append((lake_root, dest)),
        )
        handle = _rust_heal_on_event(state)
        dest = state.lake_root / "options" / "S" / "2024-06.parquet"
        handle("option_stats_remove", {"path": str(dest)})
        assert removed == [(state.lake_root, dest)]
        assert invalidated == []

    def test_record_dataset_delegates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dataset-CREATING reassignment/drift write replays the write()'s
        manifest upsert through the ONE lake_manifest law."""
        state = _state(tmp_path)
        recorded: list[tuple[Path, dict[str, str]]] = []
        monkeypatch.setattr(
            lake_heal,
            "record_dataset",
            lambda lake_root, **kwargs: recorded.append((lake_root, kwargs)),
        )
        handle = _rust_heal_on_event(state)
        handle(
            "record_dataset",
            {"exchange": "NSE", "segment": "CASH", "symbol": "M&M", "interval": "1minute"},
        )
        assert recorded == [
            (
                state.lake_root,
                {
                    "exchange": "NSE",
                    "segment": "CASH",
                    "symbol": "M&M",
                    "interval": "1minute",
                },
            )
        ]

    def test_unknown_kind_raises_loud(self, tmp_path: Path) -> None:
        handle = _rust_heal_on_event(_state(tmp_path))
        with pytest.raises(WriterError, match="unknown waved heal replay event kind"):
            handle("mystery", {})


class TestRustHealEpochUs:
    def test_exact_microsecond_reconstruction(self) -> None:
        assert _rust_heal_epoch_us(0) == datetime(1970, 1, 1, tzinfo=UTC)
        assert _rust_heal_epoch_us(1_717_387_200_000_001) == datetime(
            2024, 6, 3, 4, 0, 0, 1, tzinfo=UTC
        )
        assert _rust_heal_epoch_us(-1_000_000) == datetime(1969, 12, 31, 23, 59, 59, tzinfo=UTC)


class TestRustHealRoute:
    def test_flag_off_returns_none_without_touching_the_facade(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _bomb(**kwargs: Any) -> dict[str, Any]:
            raise AssertionError("the facade must not be called flag-OFF")

        monkeypatch.setattr(lake_rust_waved, "heal_batch_via_rust", _bomb)
        assert _rust_heal_route((_dup(tmp_path / "06.parquet"),), _state(tmp_path)) is None

    def test_multi_file_batch_reaches_the_ffi_since_w2b(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The W-2 shell-side short-circuit is DEAD (W-2b, deviation D-22):
        a drift-only batch now serializes and reaches the facade, which owns
        the engage-vs-degrade verdict via the crate's read-only preflight —
        the contract this test previously pinned is consciously FLIPPED."""
        monkeypatch.setenv(lake_rust_waved.LAKE_WAVED_RUST_ENV, "1")
        calls: list[dict[str, Any]] = []

        def _fake(**kwargs: Any) -> None:
            # Returning None = the crate-preflight degrade verdict (zero
            # mutation) — the shell must fall back to the Python core.
            calls.append(kwargs)

        monkeypatch.setattr(lake_rust_waved, "heal_batch_via_rust", _fake)
        drift = LegacyKeyDrift(
            exchange="NSE",
            segment="CASH",
            drifted_symbol="NSE-M&M",
            canonical_symbol="M&M",
            drifted_dir=tmp_path / "x",
            intervals=("1minute",),
            files=1,
        )
        assert _rust_heal_route((drift,), _state(tmp_path)) is None
        assert len(calls) == 1
        findings = calls[0]["findings"]
        assert findings[0]["kind"] == "LegacyKeyDrift"
        assert findings[0]["canonical"] == "M&M"

    def test_facade_none_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(lake_rust_waved.LAKE_WAVED_RUST_ENV, "1")
        monkeypatch.setattr(lake_rust_waved, "heal_batch_via_rust", lambda **kwargs: None)
        assert _rust_heal_route((_dup(tmp_path / "06.parquet"),), _state(tmp_path)) is None

    def test_outcome_replays_onto_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(lake_rust_waved.LAKE_WAVED_RUST_ENV, "1")
        action = {
            "action": "LAKE_HEAL_DEDUP",
            "partition": "bars/NSE/INDEX/NIFTY/1minute/2024/06.parquet",
            "kept": 2,
            "dropped": 1,
            "moved": 0,
            "conflicts": 0,
            "raw_hash_before": "a" * 64,
            "raw_hash_after": "b" * 64,
            "quarantine_path": "/q/x.parquet",
        }
        outcome = {
            "healed": 1,
            "skipped": 2,
            "errors": 3,
            "quarantined_files": 4,
            "actions": [action],
            "touched_minute": [["NSE", "INDEX", "NIFTY", [(2024, 6), (2024, 7)]]],
        }
        captured: dict[str, Any] = {}

        def _fake(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return outcome

        monkeypatch.setattr(lake_rust_waved, "heal_batch_via_rust", _fake)
        state = _state(tmp_path)
        assert _rust_heal_route((_dup(tmp_path / "06.parquet"),), state) == (1, 2, 3)
        assert state.quarantined_files == 4
        assert state.actions == [HealAction(**action)]  # type: ignore[arg-type]
        assert state.touched_minute == {("NSE", "INDEX", "NIFTY"): {(2024, 6), (2024, 7)}}
        assert captured["lake_root"] == str(state.lake_root)
        assert captured["quarantine_root"] == str(state.quarantine_root)
        assert captured["findings"] == _serialize_rust_findings((_dup(tmp_path / "06.parquet"),))

    def test_facade_exception_propagates_loud(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(lake_rust_waved.LAKE_WAVED_RUST_ENV, "1")

        def _boom(**kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("post-mutation fault")

        monkeypatch.setattr(lake_rust_waved, "heal_batch_via_rust", _boom)
        with pytest.raises(RuntimeError, match="post-mutation fault"):
            _rust_heal_route((_dup(tmp_path / "06.parquet"),), _state(tmp_path))


def _seed_dup_lake(tmp_path: Path) -> tuple[Path, IntraFileDup]:
    """A real dup-poisoned lake (raw plant — the writer refuses dups)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    lake = tmp_path / "lake"
    dest = lake / "bars/NSE/INDEX/NIFTY/1minute/2024/06.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    stamps = [
        datetime(2024, 6, 3, 4, 0, tzinfo=UTC),
        datetime(2024, 6, 3, 4, 0, tzinfo=UTC),
        datetime(2024, 6, 3, 4, 1, tzinfo=UTC),
    ]
    pq.write_table(
        pa.table(
            {
                "timestamp": pa.array(stamps, type=pa.timestamp("us", tz="UTC")),
                "open": [100.0, 200.0, 101.0],
                "high": [100.0, 200.0, 101.0],
                "low": [100.0, 200.0, 101.0],
                "close": [100.0, 200.0, 101.0],
                "volume": pa.array([10, 20, 30], type=pa.int64()),
                "open_interest": pa.array([None, None, None], type=pa.int64()),
            }
        ),
        dest,
    )
    return lake, IntraFileDup(path=dest, coords=_COORDS, physical_rows=3, unique_keys=2)


class TestHealFindingsWiring:
    def test_flag_off_heals_with_bombed_facade(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The never-routes spy: flag OFF never touches the facade surface."""

        def _bomb(**kwargs: Any) -> dict[str, Any]:
            raise AssertionError("flag OFF must never route")

        monkeypatch.setattr(lake_rust_waved, "heal_batch_via_rust", _bomb)
        monkeypatch.setattr(lake_rust_waved, "lake_content_token_via_rust", _bomb)
        lake, finding = _seed_dup_lake(tmp_path)
        report = heal_findings([finding], lake_root=lake, files_scanned=1, recall=False)
        assert report.healed == 1

    def test_flag_on_routed_outcome_bypasses_the_python_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(lake_rust_waved.LAKE_WAVED_RUST_ENV, "1")
        outcome = {
            "healed": 1,
            "skipped": 0,
            "errors": 0,
            "quarantined_files": 1,
            "actions": [],
            "touched_minute": [],
        }
        monkeypatch.setattr(lake_rust_waved, "heal_batch_via_rust", lambda **kwargs: outcome)

        def _loop_bomb(finding: Any, state: Any) -> bool:
            raise AssertionError("the Python loop must not run on a routed batch")

        monkeypatch.setattr(lake_heal, "_heal_one", _loop_bomb)
        lake, finding = _seed_dup_lake(tmp_path)
        report = heal_findings([finding], lake_root=lake, files_scanned=1, recall=False)
        assert (report.healed, report.skipped, report.errors) == (1, 0, 0)
        assert report.quarantine_root is not None  # quarantined_files=1 replayed

    def test_flag_on_facade_none_runs_the_python_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(lake_rust_waved.LAKE_WAVED_RUST_ENV, "1")
        monkeypatch.setattr(lake_rust_waved, "heal_batch_via_rust", lambda **kwargs: None)
        lake, finding = _seed_dup_lake(tmp_path)
        report = heal_findings([finding], lake_root=lake, files_scanned=1, recall=False)
        assert report.healed == 1  # the verbatim Python loop healed it


class TestContentTokenRouting:
    def test_flag_off_walks_python_with_bombed_facade(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _bomb(**kwargs: Any) -> str:
            raise AssertionError("flag OFF must never route")

        monkeypatch.setattr(lake_rust_waved, "lake_content_token_via_rust", _bomb)
        lake, _ = _seed_dup_lake(tmp_path)
        assert lake_content_token(lake) == _lake_content_token_python(lake)

    def test_flag_on_routes_through_the_facade(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(lake_rust_waved.LAKE_WAVED_RUST_ENV, "1")
        captured: dict[str, Any] = {}

        def _fake(*, lake_root: str, on_skip: Any, oracle: Any) -> str:
            captured.update(lake_root=lake_root, on_skip=on_skip, oracle=oracle)
            return "routed-token"

        monkeypatch.setattr(lake_rust_waved, "lake_content_token_via_rust", _fake)
        lake, _ = _seed_dup_lake(tmp_path)
        assert lake_content_token(lake) == "routed-token"
        assert captured["lake_root"] == str(lake)
        assert captured["on_skip"] is _replay_token_skip
        # The oracle closure is the verbatim Python walk over THIS lake.
        assert captured["oracle"]() == _lake_content_token_python(lake)

    def test_replay_token_skip_applies_the_repr_law(self) -> None:
        with capture_logs() as logs:
            _replay_token_skip("lake_heal.token_stat_skipped", "/p\n1", "e\n2")
        assert logs == [
            {
                "event": "lake_heal.token_stat_skipped",
                "path": repr("/p\n1"),
                "error": repr("e\n2"),
                "log_level": "warning",
            }
        ]
