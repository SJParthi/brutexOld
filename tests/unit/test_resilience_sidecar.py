"""Sidecar round-trip / torn-stamp / reader suite for the resilience sidecar.

Mirrors the futures ``TestTranslationSidecar`` gates: write→read round-trips,
byte-deterministic rebuild, torn/absent/reject branches (every reject logs one
``resilience.stamp_invalid``), malformed record + duplicate run_id, the two
verify-hash tiers, and the ``read_captured_trades`` backfill reader.
"""

# pyright: reportPrivateUsage=none
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import pytest
from brutex.engine.resilience_streaks import (
    RESILIENCE_SCHEMA_VERSION,
    ResilienceError,
    ResilienceStreaks,
    _record_from_row,
    fold_resilience,
    load_resilience_stamp,
    read_captured_trades,
    read_resilience_record,
    read_resilience_records,
    resilience_null,
    write_resilience_sidecar,
)
from brutex.engine.trade_store import SidecarPaths, sidecar_paths_for, sweep_run_dir
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from pathlib import Path

pytest.importorskip("polars")

_IST = timezone(timedelta(hours=5, minutes=30))
_HASH_A = "ab" * 32


@dataclass(frozen=True, slots=True)
class _FakeTrade:
    pnl_paisa: int
    entry_ts: datetime
    exit_ts: datetime


def _t(pnl: int, day: int) -> _FakeTrade:
    entry = datetime(2024, 1, day, 10, 0, tzinfo=_IST).astimezone(UTC)
    exit_ = datetime(2024, 1, day, 10, 1, tzinfo=_IST).astimezone(UTC)
    return _FakeTrade(pnl_paisa=pnl, entry_ts=entry, exit_ts=exit_)


def _paths(tmp_path: Path) -> SidecarPaths:
    base = tmp_path / "d"
    base.mkdir()
    return SidecarPaths(
        trades=base / "s.trades.parquet",
        aggs=base / "s.aggs.parquet",
        equity=base / "s.equity.parquet",
        stamp=base / "s.stamp.json",
        resilience=base / "s.resilience.parquet",
        resilience_stamp=base / "s.resilience.stamp.json",
    )


def _rec(run_id: str, trades: list[_FakeTrade]) -> ResilienceStreaks:
    return fold_resilience(
        run_id, trades, parity_ok=True, start_equity_paisa=1_000_000, fingerprint="fp"
    )


class TestRoundTrip:
    def test_write_read_round_trips(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        rec = _rec("a" * 8, [_t(-100, 1), _t(-100, 2), _t(200, 3)])
        stamp = write_resilience_sidecar(paths, [rec], store_content_hash=_HASH_A)
        assert stamp.rows == 1
        assert stamp.store_content_hash == _HASH_A
        loaded = load_resilience_stamp(paths)
        assert loaded is not None
        assert loaded.content_hash == stamp.content_hash
        back = read_resilience_record(paths, run_id="a" * 8)
        assert back == rec

    def test_idempotent_rerun_is_byte_identical(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        rec = _rec("a" * 8, [_t(-100, 1), _t(50, 2)])
        write_resilience_sidecar(paths, [rec], store_content_hash=_HASH_A)
        first = paths.resilience.read_bytes()
        write_resilience_sidecar(paths, [rec], store_content_hash=_HASH_A)
        assert paths.resilience.read_bytes() == first  # parquet is a pure function

    def test_records_sorted_and_batch_read(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        recs = [_rec("b" * 8, [_t(-1, 1)]), _rec("a" * 8, [_t(5, 1)])]
        write_resilience_sidecar(paths, recs, store_content_hash=_HASH_A)
        got = read_resilience_records(paths)
        assert set(got) == {"a" * 8, "b" * 8}

    def test_null_record_round_trips(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        rec = resilience_null("z" * 8, 4, "fp")
        write_resilience_sidecar(paths, [rec], store_content_hash=_HASH_A)
        assert read_resilience_record(paths, run_id="z" * 8) == rec

    def test_unknown_run_id_returns_none(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        write_resilience_sidecar(paths, [_rec("a" * 8, [_t(-1, 1)])], store_content_hash=_HASH_A)
        assert read_resilience_record(paths, run_id="nope") is None


class TestDamageAndRejects:
    def test_absent_stamp_returns_none(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        with capture_logs() as logs:
            assert load_resilience_stamp(paths) is None
        assert any(e["event"] == "resilience.stamp_invalid" for e in logs)
        assert read_resilience_record(paths, run_id="x") is None
        assert read_resilience_records(paths) == {}

    def test_torn_parquet_fails_size(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        write_resilience_sidecar(paths, [_rec("a" * 8, [_t(-1, 1)])], store_content_hash=_HASH_A)
        paths.resilience.write_bytes(paths.resilience.read_bytes()[:10])
        assert load_resilience_stamp(paths) is None

    def test_same_size_corruption_fails_full_hash_tier(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        write_resilience_sidecar(paths, [_rec("a" * 8, [_t(-1, 1)])], store_content_hash=_HASH_A)
        data = bytearray(paths.resilience.read_bytes())
        data[-1] ^= 0xFF  # same length, different bytes
        paths.resilience.write_bytes(bytes(data))
        assert load_resilience_stamp(paths, verify_hashes=True) is None

    def test_missing_parquet_stat_fails(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        write_resilience_sidecar(paths, [_rec("a" * 8, [_t(-1, 1)])], store_content_hash=_HASH_A)
        paths.resilience.unlink()
        assert load_resilience_stamp(paths) is None

    @pytest.mark.parametrize(
        ("mutate", "reason"),
        [
            (lambda p: p.write_text("not json", encoding="utf-8"), "stamp_not_json"),
            (lambda p: p.write_text("[1,2]", encoding="utf-8"), "stamp_not_object"),
        ],
    )
    def test_stamp_parse_rejects(self, tmp_path: Path, mutate: Any, reason: str) -> None:
        paths = _paths(tmp_path)
        write_resilience_sidecar(paths, [_rec("a" * 8, [_t(-1, 1)])], store_content_hash=_HASH_A)
        mutate(paths.resilience_stamp)
        with capture_logs() as logs:
            assert load_resilience_stamp(paths) is None
        assert any(e.get("reason") == reason for e in logs)

    @pytest.mark.parametrize(
        ("key", "value", "reason"),
        [
            ("schema_version", 999, "schema_version_mismatch"),
            ("rows", -1, "bad_rows"),
            ("content_size", "x", "bad_content_size"),
            ("store_content_hash", "zz", "bad_store_content_hash"),
            ("content_hash", "zz", "bad_content_hash"),
            ("produced_at_utc", 5, "bad_produced_at_utc"),
        ],
    )
    def test_stamp_field_rejects(self, tmp_path: Path, key: str, value: Any, reason: str) -> None:
        paths = _paths(tmp_path)
        write_resilience_sidecar(paths, [_rec("a" * 8, [_t(-1, 1)])], store_content_hash=_HASH_A)
        payload = json.loads(paths.resilience_stamp.read_text(encoding="utf-8"))
        payload[key] = value
        paths.resilience_stamp.write_text(json.dumps(payload), encoding="utf-8")
        with capture_logs() as logs:
            assert load_resilience_stamp(paths) is None
        assert any(e.get("reason") == reason for e in logs)

    def test_bad_store_hash_rejected_on_write(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        with pytest.raises(ResilienceError, match="sha256"):
            write_resilience_sidecar(paths, [_rec("a" * 8, [_t(-1, 1)])], store_content_hash="nope")

    def test_64_char_non_hex_store_hash_rejected(self, tmp_path: Path) -> None:
        # 64 chars, lowercase, but not valid hex → the bytes.fromhex ValueError arm.
        paths = _paths(tmp_path)
        with pytest.raises(ResilienceError, match="sha256"):
            write_resilience_sidecar(
                paths, [_rec("a" * 8, [_t(-1, 1)])], store_content_hash="z" * 64
            )

    def test_read_error_in_verify_tier(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # stat succeeds (size matches) but the content read raises → parquet_unreadable.
        import pathlib

        paths = _paths(tmp_path)
        write_resilience_sidecar(paths, [_rec("a" * 8, [_t(-1, 1)])], store_content_hash=_HASH_A)
        real_read = pathlib.Path.read_bytes

        def _boom(self: pathlib.Path) -> bytes:
            if self == paths.resilience:
                raise OSError("read blocked")
            return real_read(self)

        monkeypatch.setattr(pathlib.Path, "read_bytes", _boom)
        with capture_logs() as logs:
            assert load_resilience_stamp(paths, verify_hashes=True) is None
        assert any(e.get("reason") == "parquet_unreadable" for e in logs)

    def test_row_overflowing_int64_fails_the_schema(self, tmp_path: Path) -> None:
        # n_trades >= 0 passes __post_init__ but 2**70 overflows the pinned Int64.
        paths = _paths(tmp_path)
        rec = resilience_null("a" * 8, 2**70, "fp")
        with pytest.raises(ResilienceError, match="do not fit the pinned schema"):
            write_resilience_sidecar(paths, [rec], store_content_hash=_HASH_A)

    def test_duplicate_run_id_rejected(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        rec = _rec("a" * 8, [_t(-1, 1)])
        with pytest.raises(ResilienceError, match="duplicate resilience run_id"):
            write_resilience_sidecar(paths, [rec, rec], store_content_hash=_HASH_A)


class TestRecordFromRow:
    def _row(self, **over: object) -> dict[str, object]:
        rec = _rec("a" * 8, [_t(-100, 1), _t(200, 2)])
        row = dict(rec.to_record())
        row.update(over)
        return row

    def test_round_trips_a_real_row(self) -> None:
        rec = _rec("a" * 8, [_t(-100, 1), _t(200, 2)])
        assert _record_from_row(dict(rec.to_record())) == rec

    def test_mis_keyed_row_raises(self) -> None:
        row = self._row()
        del row["fingerprint"]
        with pytest.raises(ResilienceError, match="do not match RESILIENCE_COLUMNS"):
            _record_from_row(row)

    def test_bad_int_type_raises(self) -> None:
        with pytest.raises(ResilienceError, match="expected a non-null int"):
            _record_from_row(self._row(n_trades="x"))

    def test_bad_optional_int_raises(self) -> None:
        with pytest.raises(ResilienceError, match="expected an int or None"):
            _record_from_row(self._row(losing_streak_p50="x"))

    def test_bad_optional_float_raises(self) -> None:
        with pytest.raises(ResilienceError, match="expected a float or None"):
            _record_from_row(self._row(worst_streak_pnl_pct="x"))

    def test_bad_optional_bool_raises(self) -> None:
        with pytest.raises(ResilienceError, match="expected a bool or None"):
            _record_from_row(self._row(still_underwater_at_end=1))

    def test_bad_optional_str_raises(self) -> None:
        with pytest.raises(ResilienceError, match="expected a str or None"):
            _record_from_row(self._row(worst_loss_month=5))

    def test_float_from_int_is_accepted(self) -> None:
        # A stored spread may read back as an int-typed 0 → coerced to float.
        rec = _record_from_row(self._row(worst_streak_pnl_pct=0))
        assert rec.worst_streak_pnl_pct == 0.0


class TestReadCapturedTrades:
    def test_reconstructs_ordered_trades(self, tmp_path: Path) -> None:
        # Persist a real trades sidecar via the production writer, then read back.
        from decimal import Decimal

        from brutex.engine.minute_path import PathStatus
        from brutex.engine.trade_capture import CapturedTrade, ComboCapture
        from brutex.engine.trade_store import write_tuple_sidecars
        from brutex.strategies.types import Side

        paths = _paths(tmp_path)

        def _ct(pnl: int, day: int) -> CapturedTrade:
            return CapturedTrade(
                entry_ts=datetime(2024, 1, day, 4, 30, tzinfo=UTC),
                entry_px=Decimal("100.00"),
                exit_ts=datetime(2024, 1, day, 4, 31, tzinfo=UTC),
                exit_px=Decimal("100.10"),
                direction=Side.BUY,
                lots=None,
                exit_reason="signal",
                pnl_paisa=pnl,
                costs_paisa=None,
                mae_paisa=1,
                mfe_paisa=1,
                mae_1m_paisa=None,
                mfe_1m_paisa=None,
                path_1m_status=PathStatus.NO_1M_DATA,
                vix_entry=None,
                vix_exit=None,
            )

        cap = ComboCapture(
            run_id="a" * 8,
            trades=(_ct(-100, 2), _ct(50, 3)),
            equity=(),
            equity_status=PathStatus.NO_1M_DATA,
            vix_missing_trades=0,
            parity_ok=True,
            parity_detail="",
        )
        write_tuple_sidecars(paths, [cap], [], store_content_hash=_HASH_A)
        got = read_captured_trades(paths.trades)
        assert list(got) == ["a" * 8]
        assert [t.pnl_paisa for t in got["a" * 8]] == [-100, 50]

    def test_missing_polars_hint(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys

        paths = _paths(tmp_path)
        monkeypatch.setitem(sys.modules, "polars", None)
        with pytest.raises(ResilienceError, match="requires 'polars'"):
            read_captured_trades(paths.trades)

    def test_unreadable_parquet_wrapped(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        paths.trades.write_bytes(b"not a parquet")
        with pytest.raises(ResilienceError, match="unreadable parquet"):
            read_captured_trades(paths.trades)


def test_render_missing_polars_on_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    paths = _paths(tmp_path)
    monkeypatch.setitem(sys.modules, "polars", None)
    with pytest.raises(ResilienceError, match="requires 'polars'"):
        write_resilience_sidecar(paths, [_rec("a" * 8, [_t(-1, 1)])], store_content_hash=_HASH_A)


def test_sidecar_paths_for_populates_resilience(tmp_path: Path) -> None:
    root = tmp_path / "sweeps"
    store = root / "NIFTY" / "1minute" / "bull_k1_deadbeef00000000.parquet"
    paths = sidecar_paths_for(store, root, run_id="r" * 8)
    base = sweep_run_dir(root, "r" * 8) / "results-depth" / "NIFTY" / "1minute"
    assert paths.resilience == base / "bull_k1_deadbeef00000000.resilience.parquet"
    assert paths.resilience_stamp == base / "bull_k1_deadbeef00000000.resilience.stamp.json"
    assert RESILIENCE_SCHEMA_VERSION == 1
