"""Wiring tests for the ``_merge_write`` Rust merge-decision seam (ADR-0020 PR-3).

DEC-LAKE-RUST-MERGE-DEDUP-001 (MIGRATION.md #1911 — allocator-final).
Crate-FREE: a fake ``brutex_lake`` module implemented ON TOP of the installed
Python polars (it genuinely parses the seam's IPC decision frames and replays
the fold law over them) drives the flag-ON routing through the REAL
:meth:`ParquetWriter.write` merge branch; the flag-OFF arm is pinned
byte-identical with a never-routes spy. Real-crate byte parity lives in
``tests/golden/test_lake_rust_merge_parity.py``.

Whitebox (DEC-P0-017): exercises ``_merge_write``/``_decision_frame`` directly.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import errno
import io
import sys
import types
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data import lake_rust_merge as lrm
from brutex.data.errors import LakeWriteHalt
from brutex.data.models import GREEKS_OVERLAY_FIELDS, CandleBar, FnoGreeksBar
from brutex.data.parquet_writer import ParquetWriter, WriteResult, _decision_frame, _ipc_bytes
from structlog.testing import capture_logs

pytest.importorskip("polars")

COORDS = {"exchange": "NSE", "segment": "CASH", "symbol": "NSE-NIFTY", "interval": "1minute"}


@pytest.fixture(autouse=True)
def _fresh_process_view() -> Iterator[None]:
    lrm.reset_lake_rust_merge_for_tests()
    yield
    lrm.reset_lake_rust_merge_for_tests()


def _bar(minute: int, *, volume: int = 1_000, oi: int | None = None) -> CandleBar:
    return CandleBar(
        timestamp=datetime(2024, 1, 1, 4, minute, tzinfo=UTC),
        open=Decimal("100.25"),
        high=Decimal("101.00"),
        low=Decimal("99.75"),
        close=Decimal("100.50"),
        volume=volume,
        open_interest=oi,
    )


def _greeks_bar(minute: int, *, iv: float | None = 0.21) -> FnoGreeksBar:
    return FnoGreeksBar(
        timestamp=datetime(2024, 1, 1, 4, minute, tzinfo=UTC),
        open=Decimal("100.25"),
        high=Decimal("101.00"),
        low=Decimal("99.75"),
        close=Decimal("100.50"),
        volume=1_000,
        open_interest=25,
        iv=iv,
        delta=0.5,
        spot_at_bar=Decimal("100.00"),
        greeks_provenance_id=7,
    )


def _polars_backed_fake_crate() -> types.ModuleType:
    """A fake crate whose decision REPLAYS the fold law over the REAL IPC frames.

    Parses the seam's actual ``_decision_frame`` → ``_ipc_bytes`` handoff
    back through the installed polars and computes actions with an
    independent dict-of-rows fold — so the routed path (frame build, IPC
    round-trip, action materialization) is proven end-to-end without the
    compiled crate.
    """
    import polars as pl

    module = types.ModuleType("brutex_lake")
    value_keys = ("open", "high", "low", "close", "volume", "open_interest")

    def merge_decision_from_ipc(stored_ipc: bytes, batch_ipc: bytes, keep_first: bool) -> list[int]:
        stored = pl.read_ipc(io.BytesIO(stored_ipc)).to_dicts()
        batch = pl.read_ipc(io.BytesIO(batch_ipc)).to_dicts()
        winners: dict[object, dict[str, object]] = {}
        for row in stored:
            winners.setdefault(row["timestamp"], row)
        actions: list[int] = []
        for row in batch:
            prev = winners.get(row["timestamp"])
            if prev is None:
                winners[row["timestamp"]] = row
                actions.append(lrm.ACTION_INSERT)
            elif any(prev[k] != row[k] for k in value_keys):
                if keep_first:
                    actions.append(lrm.ACTION_CONFLICT)
                else:
                    winners[row["timestamp"]] = row
                    actions.append(lrm.ACTION_OVERWRITE)
            elif any(prev[k] != row[k] for k in GREEKS_OVERLAY_FIELDS):
                if keep_first:
                    actions.append(lrm.ACTION_SKIP)
                else:
                    winners[row["timestamp"]] = row
                    actions.append(lrm.ACTION_OVERWRITE)
            else:
                actions.append(lrm.ACTION_SKIP)
        return actions

    module.merge_decision_from_ipc = merge_decision_from_ipc  # type: ignore[attr-defined]
    module.pinned_polars_version = lambda: pl.__version__  # type: ignore[attr-defined]
    module.linked_zstd_version = lambda: "fake"  # type: ignore[attr-defined]
    return module


def _result_fields(result: WriteResult) -> tuple[object, ...]:
    return (
        result.row_count,
        result.raw_hash,
        result.first_ts,
        result.last_ts,
        result.bytes_written,
        result.bars_added,
        tuple((c.timestamp, c.stored, c.vendor) for c in result.conflicts),
        result.vendor_dup_rows_dropped,
        result.file_rewritten,
    )


def _run_sequence(lake_root: Path, *, greeks: bool = False) -> list[WriteResult]:
    """One representative write sequence: fresh → merge (new + dup +
    conflict) → latest-wins restate → fully-duplicate no-op merge."""
    writer = ParquetWriter(lake_root=lake_root)
    coords = dict(COORDS)
    if greeks:
        coords["segment"] = "FNO"
    results = [writer.write(bars=[_greeks_bar(0) if greeks else _bar(0)], **coords)]
    batch = [
        _greeks_bar(0) if greeks else _bar(0),  # identical dup
        _bar(0, volume=9_999),  # keep-first value conflict
        _bar(1),  # net-new
    ]
    results.append(writer.write(bars=batch, **coords))
    restate = [_bar(0, volume=5_555)]
    results.append(writer.write(bars=restate, merge_mode="latest-wins", **coords))
    results.append(writer.write(bars=[_bar(1)], **coords))  # no-op merge
    return results


class TestFlagOff:
    def test_kill_switch_never_touches_the_seam(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # OFF is addressed via the explicit "0" kill switch since the
        # default flipped ON (DEC-RUST-DEFAULT-ON-WAVE-001) — the never-routes teeth survive.
        monkeypatch.setenv(lrm.LAKE_RUST_MERGE_ENV, "0")

        def _must_not_route(*_a: object, **_k: object) -> object:
            raise AssertionError("flag-OFF must never call lake_rust_merge.merge_fold")

        monkeypatch.setattr(lrm, "merge_fold", _must_not_route)
        results = _run_sequence(tmp_path / "lake")
        assert results[1].bars_added == 1 and len(results[1].conflicts) == 1
        assert results[2].file_rewritten is True  # the latest-wins restate
        assert results[3].file_rewritten is False  # the no-op merge

    def test_junk_flag_is_loud_off(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(lrm.LAKE_RUST_MERGE_ENV, "ture")

        def _must_not_route(*_a: object, **_k: object) -> object:
            raise AssertionError("a junk flag must resolve OFF")

        monkeypatch.setattr(lrm, "merge_fold", _must_not_route)
        with capture_logs() as logs:
            results = _run_sequence(tmp_path / "lake")
        assert results[1].bars_added == 1
        fallbacks = [e for e in logs if e["event"] == "lake_rust_merge.fallback"]
        assert fallbacks and fallbacks[0]["reason"] == "flag_unrecognised"


class TestFlagOn:
    @pytest.mark.parametrize("greeks", [False, True])
    def test_on_with_faithful_crate_is_byte_identical_to_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, greeks: bool
    ) -> None:
        """The full write() sequence — fresh, conflict merge, latest-wins
        restate, no-op merge — produces byte-identical partitions and
        field-identical WriteResults ON vs OFF (spot AND Greeks shapes)."""
        monkeypatch.delenv(lrm.LAKE_RUST_MERGE_ENV, raising=False)
        off_results = _run_sequence(tmp_path / "off", greeks=greeks)

        monkeypatch.setitem(sys.modules, "brutex_lake", _polars_backed_fake_crate())
        monkeypatch.setenv(lrm.LAKE_RUST_MERGE_ENV, "1")
        lrm.reset_lake_rust_merge_for_tests()
        with capture_logs() as logs:
            on_results = _run_sequence(tmp_path / "on", greeks=greeks)
        assert not lrm.rust_merge_disabled()  # every burn-in compare passed
        assert any(e["event"] == "lake_rust_merge.engaged" for e in logs)
        for off, on in zip(off_results, on_results, strict=True):
            assert _result_fields(off) == _result_fields(on)
        off_file = off_results[-1].path
        on_file = on_results[-1].path
        assert off_file.read_bytes() == on_file.read_bytes()

    def test_crate_memory_error_maps_to_lake_write_halt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OOM inside the engaged decision rides the merge fold's
        ``lake_write_guard`` → ``LakeWriteHalt("out_of_memory")`` — identical
        to the pure-Python fold's OOM contract (DEC-LAKE-MERGE-OOM-HALT-001)."""
        import polars as pl

        fake = types.ModuleType("brutex_lake")
        fake.pinned_polars_version = lambda: pl.__version__  # type: ignore[attr-defined]
        fake.linked_zstd_version = lambda: "fake"  # type: ignore[attr-defined]

        def _oom(*_a: object) -> list[int]:
            raise MemoryError

        fake.merge_decision_from_ipc = _oom  # type: ignore[attr-defined]
        writer = ParquetWriter(lake_root=tmp_path / "lake")
        writer.write(bars=[_bar(0)], **COORDS)
        monkeypatch.setitem(sys.modules, "brutex_lake", fake)
        monkeypatch.setenv(lrm.LAKE_RUST_MERGE_ENV, "1")
        lrm.reset_lake_rust_merge_for_tests()
        with pytest.raises(LakeWriteHalt) as excinfo:
            writer.write(bars=[_bar(1)], **COORDS)
        assert excinfo.value.reason == "out_of_memory"

    def test_enospc_during_engaged_merge_rewrite_maps_to_disk_full(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ENOSPC at the merge rewrite's atomic replace, flag ON →
        ``LakeWriteHalt("disk_full")`` — the DEC-LAKE-WRITE-HALT-001 taxonomy
        is untouched by the seam."""
        writer = ParquetWriter(lake_root=tmp_path / "lake")
        writer.write(bars=[_bar(0)], **COORDS)
        monkeypatch.setitem(sys.modules, "brutex_lake", _polars_backed_fake_crate())
        monkeypatch.setenv(lrm.LAKE_RUST_MERGE_ENV, "1")
        lrm.reset_lake_rust_merge_for_tests()

        def _enospc(src: object, dst: object) -> None:
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr("brutex.data.atomic_io.os.replace", _enospc)
        with pytest.raises(LakeWriteHalt) as excinfo:
            writer.write(bars=[_bar(1)], **COORDS)
        assert excinfo.value.reason == "disk_full"

    def test_crate_panic_degrades_to_identical_python_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An escaped ``PanicException`` latches ``crate_panic`` and the write
        COMPLETES with the Python fold's exact result — never a raw
        BaseException into the pull spine, never a LakeWriteHalt."""
        import polars as pl
        import polars.exceptions as pl_exc

        monkeypatch.delenv(lrm.LAKE_RUST_MERGE_ENV, raising=False)
        off_results = _run_sequence(tmp_path / "off")

        fake = types.ModuleType("brutex_lake")
        fake.pinned_polars_version = lambda: pl.__version__  # type: ignore[attr-defined]
        fake.linked_zstd_version = lambda: "fake"  # type: ignore[attr-defined]

        def _panic(*_a: object) -> list[int]:
            raise pl_exc.PanicException("simulated escaped panic")

        fake.merge_decision_from_ipc = _panic  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "brutex_lake", fake)
        monkeypatch.setenv(lrm.LAKE_RUST_MERGE_ENV, "1")
        lrm.reset_lake_rust_merge_for_tests()
        with capture_logs() as logs:
            on_results = _run_sequence(tmp_path / "on")
        for off, on in zip(off_results, on_results, strict=True):
            assert _result_fields(off) == _result_fields(on)
        assert on_results[-1].path.read_bytes() == off_results[-1].path.read_bytes()
        disabled = [e for e in logs if e["event"] == "lake_rust_merge.rust_path_disabled"]
        assert disabled and disabled[0]["reason"] == "crate_panic"

    def test_poisoned_decision_refused_serves_identical_python_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crate returning law-contradicting actions is REFUSED wholesale;
        the write completes with the Python fold's exact result."""
        import polars as pl

        monkeypatch.delenv(lrm.LAKE_RUST_MERGE_ENV, raising=False)
        off_results = _run_sequence(tmp_path / "off")

        fake = types.ModuleType("brutex_lake")
        fake.pinned_polars_version = lambda: pl.__version__  # type: ignore[attr-defined]
        fake.linked_zstd_version = lambda: "fake"  # type: ignore[attr-defined]

        def _poison(stored_ipc: bytes, batch_ipc: bytes, keep_first: bool) -> list[int]:
            return [lrm.ACTION_INSERT] * len(
                pl.read_ipc(io.BytesIO(batch_ipc)).to_dicts()
            )  # INSERT at stored timestamps — contradicts the fold law

        fake.merge_decision_from_ipc = _poison  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "brutex_lake", fake)
        monkeypatch.setenv(lrm.LAKE_RUST_MERGE_ENV, "1")
        lrm.reset_lake_rust_merge_for_tests()
        with capture_logs() as logs:
            on_results = _run_sequence(tmp_path / "on")
        for off, on in zip(off_results, on_results, strict=True):
            assert _result_fields(off) == _result_fields(on)
        fallbacks = [e for e in logs if e["event"] == "lake_rust_merge.fallback"]
        assert any(e["reason"] == "decision_refused" for e in fallbacks)

    def test_dup_poisoned_destination_still_force_rewrites(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The dedup self-heal law (bars_added==0 force-rewrite,
        ``lake.partition.dedup_healed``) is UNTOUCHED by the seam — the heal
        verdict is computed OUTSIDE the routed fold (stored physical rows vs
        unique count), so a hand-poisoned closed month heals identically ON."""
        import polars as pl

        lake = tmp_path / "lake"
        writer = ParquetWriter(lake_root=lake)
        first = writer.write(bars=[_bar(0), _bar(1)], **COORDS)
        # Hand-poison the stored file with a duplicated timestamp row
        # (bypassing the writer — the pre-DEC-LAKE-WRITE-UNIQUE-001 shape).
        df = pl.read_parquet(first.path)
        poisoned = pl.concat([df, df.slice(0, 1)]).sort("timestamp")
        poisoned.write_parquet(first.path)

        monkeypatch.setitem(sys.modules, "brutex_lake", _polars_backed_fake_crate())
        monkeypatch.setenv(lrm.LAKE_RUST_MERGE_ENV, "1")
        lrm.reset_lake_rust_merge_for_tests()
        with capture_logs() as logs:
            result = writer.write(bars=[_bar(0)], **COORDS)  # adds nothing
        assert result.bars_added == 0
        assert result.file_rewritten is True  # the forced dedup-heal rewrite
        assert result.row_count == 2
        assert any(e["event"] == "lake.partition.dedup_healed" for e in logs)
        assert not lrm.rust_merge_disabled()


class TestDecisionFrame:
    def test_decision_frame_is_always_17_columns(self) -> None:
        plain = _decision_frame([_bar(0)])
        enriched = _decision_frame([_greeks_bar(0)])
        expected = [
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "open_interest",
            *GREEKS_OVERLAY_FIELDS,
        ]
        assert plain.columns == expected == enriched.columns
        assert plain["iv"].to_list() == [None]  # plain bars read a null overlay
        assert enriched["iv"].to_list() == [0.21]
        assert enriched["spot_at_bar"].to_list() == [100.0]
        assert enriched["greeks_provenance_id"].to_list() == [7]

    def test_decision_frame_ipc_round_trips(self) -> None:
        import polars as pl

        frame = _decision_frame([_greeks_bar(0), _bar(1)])
        back = pl.read_ipc(io.BytesIO(_ipc_bytes(frame)))
        assert back.equals(frame)

    def test_empty_bars_build_a_zero_row_frame(self) -> None:
        frame = _decision_frame([])
        assert frame.height == 0
        assert len(frame.columns) == 17
