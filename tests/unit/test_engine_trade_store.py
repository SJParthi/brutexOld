"""Unit tests for :mod:`brutex.engine.trade_store` (DEC-SWEEP-RESULTS-DEPTH-001 WP1).

Covers: the path-mechanical sidecar mapping, write→read round trips, the wire
row's EXACT binding-contract shape (key order + granularity switch), the full
stamp damage matrix (every rejection → ``None`` → rebuild), the db_view clamp
laws, zero-trade combos as first-class, byte-determinism (write twice / permuted
inputs → identical Parquet bytes), the span-belt desync guard, and the
``polars``-missing guards.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex.calendars.nse import IST
from brutex.engine.minute_path import PathStatus, SessionEquityPoint
from brutex.engine.trade_capture import CapturedTrade, ComboCapture
from brutex.engine.trade_store import (
    AGG_COLUMNS,
    PAGE_LIMIT_MAX,
    RESULTS_DEPTH_DIRNAME,
    SWEEP_RUNS_DIRNAME,
    TRADES_SCHEMA_VERSION,
    SidecarPaths,
    TradeStoreError,
    _is_count,
    _is_sha256,
    iter_trades_for_export,
    load_stamp,
    read_trade_page,
    sidecar_paths_for,
    sweep_run_dir,
    write_tuple_sidecars,
)
from brutex.strategies.types import Side

_DAY = date(2024, 1, 2)
_RUN_A = "aa" * 32
_RUN_Z = "ff" * 32
_STORE_HASH = "12" * 32
#: The grid-level run id every sidecar path is keyed under (Ruling 4 layout).
_GRID_RUN = "grid-0123456789abcdef"


def _ts(hh: int, mm: int) -> datetime:
    return datetime(_DAY.year, _DAY.month, _DAY.day, hh, mm, tzinfo=IST).astimezone(UTC)


def _trade(
    seq: int,
    *,
    status: PathStatus = PathStatus.OK,
    pnl: int = 50,
    vix: str | None = "15.25",
) -> CapturedTrade:
    one_m = None if status is PathStatus.NO_1M_DATA else 100 + seq
    return CapturedTrade(
        entry_ts=_ts(9, 16 + 2 * seq),
        entry_px=Decimal("100.00"),
        exit_ts=_ts(9, 17 + 2 * seq),
        exit_px=Decimal("100.50") if pnl >= 0 else Decimal("99.70"),
        direction=Side.BUY,
        lots=None,  # NULL-by-design until P5 (Ruling 18)
        exit_reason="take_profit",
        pnl_paisa=pnl,
        costs_paisa=None,  # NULL-by-design until P5 (Ruling 18)
        mae_paisa=10 + seq,
        mfe_paisa=60 + seq,
        mae_1m_paisa=one_m,
        mfe_1m_paisa=one_m,
        path_1m_status=status,
        vix_entry=None if vix is None else Decimal(vix),
        vix_exit=None if vix is None else Decimal(vix),
    )


_TRADES_A = (
    _trade(0, status=PathStatus.OK),
    _trade(1, status=PathStatus.PARTIAL, pnl=-30, vix=None),
    _trade(2, status=PathStatus.NO_1M_DATA),
)


def _captures() -> list[ComboCapture]:
    combo_a = ComboCapture(
        run_id=_RUN_A,
        trades=_TRADES_A,
        equity=(SessionEquityPoint(_DAY, 10_000_070, 9_999_900, 3),),
        equity_status=PathStatus.OK,
        vix_missing_trades=1,
        parity_ok=True,
        parity_detail="",
    )
    combo_z = ComboCapture(  # zero-trade combo — first-class
        run_id=_RUN_Z,
        trades=(),
        equity=(SessionEquityPoint(_DAY, 10_000_000, None, 0),),
        equity_status=PathStatus.NO_1M_DATA,
        vix_missing_trades=0,
        parity_ok=True,
        parity_detail="",
    )
    return [combo_a, combo_z]


def _agg_record(run_id: str, **overrides: object) -> dict[str, object]:
    defaults: dict[str, object] = {
        "run_id": run_id,
        "n_trades": 3,
        "zero_trades": False,
        "parity_ok": True,
        "mae_avg_paisa": 20,
        "mae_worst_paisa": 40,
        "mae_p95_paisa": 40,
        "mfe_avg_paisa": 33,
        "mfe_best_paisa": 62,
        "mfe_p95_paisa": 62,
        "mae_1m_avg_paisa": None,
        "mae_1m_worst_paisa": None,
        "mae_1m_p95_paisa": None,
        "mfe_1m_avg_paisa": None,
        "mfe_1m_best_paisa": None,
        "mfe_1m_p95_paisa": None,
        "trades_with_1m": 1,
        "max_dd_minute_path_paisa": 170,
        "max_dd_minute_path_pct": 0.0000169999,
        "equity_status": "ok",
        "worst5_days_json": '[["2024-01-02",-30]]',
        "vix_win_min": 15.25,
        "vix_win_avg": 15.25,
        "vix_win_max": 15.25,
        "vix_loss_min": None,
        "vix_loss_avg": None,
        "vix_loss_max": None,
        "vix_band_lo": 15.25,
        "vix_band_hi": 15.25,
        "vix_stamped_trades": 2,
        "vix_missing_trades": 1,
        "pnl_by_year_json": '{"2024":70}',
        "pnl_by_trend_json": '{"sideways":70}',
        "one_regime_wonder": False,
        "regime_wonder_axis": None,
        "entry_hour_hist_json": '{"9":3}',
        "event_day_counts_json": "{}",
        "event_day_pnl_json": "{}",
        "exits_stop_loss": 1,
        "exits_take_profit": 2,
        "exits_signal": 0,
        "exits_forced_1520": 0,
        "exits_end_of_data": 0,
        "exits_session_gap": 0,
        "max_consecutive_losses": 1,
        "max_consecutive_wins": 1,
        "composite_score": 1.234567890123,
        "win_loss_ratio": 2.0,
        "recovery_factor": None,
        "fingerprint": "f0" * 32,
    }
    defaults.update(overrides)
    return defaults


@pytest.fixture
def paths(tmp_path: Path) -> SidecarPaths:
    store = tmp_path / "sweeps" / "NIFTY" / "1minute" / "bull_k1_0123456789abcdef.parquet"
    return sidecar_paths_for(store, tmp_path / "sweeps", run_id=_GRID_RUN)


@pytest.fixture
def written(paths: SidecarPaths) -> SidecarPaths:
    write_tuple_sidecars(
        paths,
        _captures(),
        [_agg_record(_RUN_A), _agg_record(_RUN_Z, n_trades=0, zero_trades=True)],
        store_content_hash=_STORE_HASH,
    )
    return paths


class TestSweepRunDir:
    def test_sibling_root_beside_the_sweeps_tree(self, tmp_path: Path) -> None:
        """Ruling 4: the run dir is `<sweeps parent>/sweep_runs/<run_id>` —
        a SIBLING of the sweeps tree (the register's `<lake>/sweep_runs/`
        example), never inside the `**/*.parquet`-globbed tree."""
        sweeps = tmp_path / "sweeps"
        run_dir = sweep_run_dir(sweeps, _GRID_RUN)
        assert run_dir == tmp_path / SWEEP_RUNS_DIRNAME / _GRID_RUN
        # Structurally OUTSIDE the sweeps tree.
        assert not run_dir.is_relative_to(sweeps)

    def test_rejects_empty_run_id(self, tmp_path: Path) -> None:
        with pytest.raises(TradeStoreError, match="run_id must be non-empty"):
            sweep_run_dir(tmp_path / "sweeps", "")


class TestSidecarPathsFor:
    def test_mechanical_mapping(self, tmp_path: Path) -> None:
        root = tmp_path / "sweeps"
        store = root / "NIFTY" / "5minute" / "bull_k3_deadbeef00000000.parquet"
        mapped = sidecar_paths_for(store, root, run_id=_GRID_RUN)
        base = sweep_run_dir(root, _GRID_RUN) / RESULTS_DEPTH_DIRNAME / "NIFTY" / "5minute"
        assert mapped == SidecarPaths(
            trades=base / "bull_k3_deadbeef00000000.trades.parquet",
            aggs=base / "bull_k3_deadbeef00000000.aggs.parquet",
            equity=base / "bull_k3_deadbeef00000000.equity.parquet",
            stamp=base / "bull_k3_deadbeef00000000.stamp.json",
        )

    def test_nothing_lands_inside_the_globbed_sweeps_tree(self, tmp_path: Path) -> None:
        """Ruling 4 teeth: no sidecar path may sit under the sweeps root —
        results_report + sweep_verify recursively glob `**/*.parquet` there."""
        root = tmp_path / "sweeps"
        store = root / "NIFTY" / "5minute" / "bull_k3_deadbeef00000000.parquet"
        mapped = sidecar_paths_for(store, root, run_id=_GRID_RUN)
        for path in (mapped.trades, mapped.aggs, mapped.equity, mapped.stamp):
            assert not path.is_relative_to(root), path

    def test_rejects_store_outside_root(self, tmp_path: Path) -> None:
        with pytest.raises(TradeStoreError, match="not under the sweeps root"):
            sidecar_paths_for(
                tmp_path / "elsewhere" / "x.parquet", tmp_path / "sweeps", run_id=_GRID_RUN
            )

    def test_rejects_wrong_depth(self, tmp_path: Path) -> None:
        root = tmp_path / "sweeps"
        with pytest.raises(TradeStoreError, match="3-level"):
            sidecar_paths_for(root / "NIFTY" / "x.parquet", root, run_id=_GRID_RUN)

    def test_rejects_non_parquet(self, tmp_path: Path) -> None:
        root = tmp_path / "sweeps"
        with pytest.raises(TradeStoreError, match="3-level"):
            sidecar_paths_for(root / "NIFTY" / "1minute" / "x.trials.json", root, run_id=_GRID_RUN)

    def test_rejects_empty_run_id(self, tmp_path: Path) -> None:
        root = tmp_path / "sweeps"
        with pytest.raises(TradeStoreError, match="run_id must be non-empty"):
            sidecar_paths_for(root / "NIFTY" / "1minute" / "x.parquet", root, run_id="")


class TestWriteAndStamp:
    def test_stamp_fields(self, written: SidecarPaths) -> None:
        stamp = load_stamp(written)
        assert stamp is not None
        assert stamp.schema_version == TRADES_SCHEMA_VERSION
        assert stamp.store_content_hash == _STORE_HASH
        assert stamp.run_ids == 2
        assert stamp.trade_rows == 3
        assert stamp.agg_rows == 2
        assert stamp.equity_rows == 2
        assert stamp.trade_spans == {_RUN_A: (0, 3), _RUN_Z: (3, 0)}
        for key, path in (
            ("trades", written.trades),
            ("aggs", written.aggs),
            ("equity", written.equity),
        ):
            assert stamp.content_hashes[key] == hashlib.sha256(path.read_bytes()).hexdigest()

    def test_write_twice_byte_identical(self, paths: SidecarPaths) -> None:
        aggs = [_agg_record(_RUN_A), _agg_record(_RUN_Z, n_trades=0, zero_trades=True)]
        write_tuple_sidecars(paths, _captures(), aggs, store_content_hash=_STORE_HASH)
        first = (
            paths.trades.read_bytes(),
            paths.aggs.read_bytes(),
            paths.equity.read_bytes(),
        )
        write_tuple_sidecars(paths, _captures(), aggs, store_content_hash=_STORE_HASH)
        assert (
            paths.trades.read_bytes(),
            paths.aggs.read_bytes(),
            paths.equity.read_bytes(),
        ) == first

    def test_input_order_never_matters(self, paths: SidecarPaths) -> None:
        aggs = [_agg_record(_RUN_A), _agg_record(_RUN_Z, n_trades=0, zero_trades=True)]
        write_tuple_sidecars(paths, _captures(), aggs, store_content_hash=_STORE_HASH)
        first = paths.trades.read_bytes(), paths.aggs.read_bytes(), paths.equity.read_bytes()
        write_tuple_sidecars(
            paths,
            list(reversed(_captures())),
            list(reversed(aggs)),
            store_content_hash=_STORE_HASH,
        )
        assert (
            paths.trades.read_bytes(),
            paths.aggs.read_bytes(),
            paths.equity.read_bytes(),
        ) == first

    def test_rejects_bad_store_hash(self, paths: SidecarPaths) -> None:
        with pytest.raises(TradeStoreError, match="store_content_hash"):
            write_tuple_sidecars(paths, [], [], store_content_hash="nope")

    def test_rejects_duplicate_capture_run_id(self, paths: SidecarPaths) -> None:
        combo = _captures()[0]
        with pytest.raises(TradeStoreError, match="duplicate capture run_id"):
            write_tuple_sidecars(paths, [combo, combo], [], store_content_hash=_STORE_HASH)

    def test_rejects_agg_key_mismatch(self, paths: SidecarPaths) -> None:
        record = _agg_record(_RUN_A)
        record.pop("fingerprint")
        record["surprise"] = 1
        with pytest.raises(
            TradeStoreError, match=r"missing=\['fingerprint'\].*extra=\['surprise'\]"
        ):
            write_tuple_sidecars(paths, [], [record], store_content_hash=_STORE_HASH)

    def test_rejects_agg_extra_key_alone(self, paths: SidecarPaths) -> None:
        """The strict agg key-set EQUALITY check refuses a pure-extra-key record.

        Pins a PRE-EXISTING strictness law adjacent to the AGG_COLUMNS
        extension (refuter round 5 MS4 — in-scope hardening beside the
        `exits_session_gap` row, not a drive-by): a record carrying ALL 50
        pinned columns PLUS one unknown extra key must refuse with
        ``missing=[]`` under the STRICT ``keys != _AGG_COLUMN_NAMES`` compare.
        A superset-weakening mutant (``not keys >= _AGG_COLUMN_NAMES``)
        silently ACCEPTS this shape — the extra key dropped at columnarise —
        while the rename-shaped sibling above (missing + extra
        simultaneously) still raises under it, so only this shape kills it.
        """
        record = _agg_record(_RUN_A)
        record["surprise"] = 1
        with pytest.raises(TradeStoreError, match=r"missing=\[\].*extra=\['surprise'\]"):
            write_tuple_sidecars(paths, [], [record], store_content_hash=_STORE_HASH)

    def test_rejects_agg_bad_run_id(self, paths: SidecarPaths) -> None:
        bad_type = _agg_record(_RUN_A)
        bad_type["run_id"] = 7
        with pytest.raises(TradeStoreError, match="run_id must be a non-empty str"):
            write_tuple_sidecars(paths, [], [bad_type], store_content_hash=_STORE_HASH)
        bad_empty = _agg_record(_RUN_A)
        bad_empty["run_id"] = ""
        with pytest.raises(TradeStoreError, match="run_id must be a non-empty str"):
            write_tuple_sidecars(paths, [], [bad_empty], store_content_hash=_STORE_HASH)

    def test_rejects_duplicate_agg_run_id(self, paths: SidecarPaths) -> None:
        with pytest.raises(TradeStoreError, match="duplicate agg record run_id"):
            write_tuple_sidecars(
                paths,
                [],
                [_agg_record(_RUN_A), _agg_record(_RUN_A)],
                store_content_hash=_STORE_HASH,
            )

    def test_rejects_agg_value_off_schema(self, paths: SidecarPaths) -> None:
        with pytest.raises(TradeStoreError, match="pinned schema"):
            write_tuple_sidecars(
                paths,
                [],
                [_agg_record(_RUN_A, n_trades="three")],
                store_content_hash=_STORE_HASH,
            )

    def test_write_without_polars_raises_hint(
        self, paths: SidecarPaths, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "polars", None)  # block `import polars`
        with pytest.raises(TradeStoreError, match="requires 'polars'"):
            write_tuple_sidecars(paths, [], [], store_content_hash=_STORE_HASH)


class TestReadTradePage:
    def test_wire_shape_exact_contract_order(self, written: SidecarPaths) -> None:
        page = read_trade_page(written, run_id=_RUN_A, offset=0, limit=10)
        assert page.total == 3
        assert [list(row.keys()) for row in page.rows] == [
            [
                "entry_ts",
                "entry_px",
                "exit_ts",
                "exit_px",
                "direction",
                "lots",
                "exit_reason",
                "pnl_paisa",
                "costs_paisa",
                "mae",
                "mfe",
                "vix_entry",
                "vix_exit",
                "trade_seq",
                "path_1m_status",
                "mae_granularity",
            ]
        ] * 3

    def test_wire_values_and_granularity_switch(self, written: SidecarPaths) -> None:
        rows = read_trade_page(written, run_id=_RUN_A, offset=0, limit=10).rows
        ok, partial, missing = rows
        # OK path → 1m values; PARTIAL / NO_1M_DATA → TF values, "tf" label.
        assert (ok["mae"], ok["mfe"], ok["mae_granularity"]) == (100, 100, "1m")
        assert (partial["mae"], partial["mfe"], partial["mae_granularity"]) == (11, 61, "tf")
        assert (missing["mae"], missing["mfe"], missing["mae_granularity"]) == (12, 62, "tf")
        assert ok["entry_ts"] == _ts(9, 16).isoformat()
        assert ok["exit_ts"] == _ts(9, 17).isoformat()
        assert (ok["entry_px"], ok["exit_px"]) == (100.0, 100.5)
        assert (ok["direction"], ok["lots"], ok["exit_reason"]) == ("BUY", None, "take_profit")
        assert (ok["pnl_paisa"], ok["costs_paisa"]) == (50, None)  # costs NULL until P5
        assert (ok["vix_entry"], ok["vix_exit"]) == (15.25, 15.25)
        assert (partial["vix_entry"], partial["vix_exit"]) == (None, None)
        assert [row["trade_seq"] for row in rows] == [0, 1, 2]

    def test_page_partition_matches_export(self, written: SidecarPaths) -> None:
        page1 = read_trade_page(written, run_id=_RUN_A, offset=0, limit=2)
        page2 = read_trade_page(written, run_id=_RUN_A, offset=2, limit=2)
        assert page1.total == page2.total == 3
        exported = list(iter_trades_for_export(written, _RUN_A))
        assert list(page1.rows) + list(page2.rows) == exported

    def test_clamps(self, written: SidecarPaths) -> None:
        page = read_trade_page(written, run_id=_RUN_A, offset=-5, limit=0)
        assert (page.offset, page.limit) == (0, 1)
        assert len(page.rows) == 1
        page = read_trade_page(written, run_id=_RUN_A, offset=0, limit=10**9)
        assert page.limit == PAGE_LIMIT_MAX
        page = read_trade_page(written, run_id=_RUN_A, offset=10**30, limit=5)
        assert page.rows == ()
        assert page.total == 3

    def test_offset_beyond_total_empty(self, written: SidecarPaths) -> None:
        page = read_trade_page(written, run_id=_RUN_A, offset=3, limit=5)
        assert page.rows == ()
        assert page.total == 3

    def test_unknown_run_id_empty_total_zero(self, written: SidecarPaths) -> None:
        page = read_trade_page(written, run_id="00" * 32, offset=0, limit=5)
        assert (page.rows, page.total) == ((), 0)

    def test_zero_trade_combo_first_class(self, written: SidecarPaths) -> None:
        page = read_trade_page(written, run_id=_RUN_Z, offset=0, limit=5)
        assert (page.rows, page.total) == ((), 0)

    def test_invalid_stamp_raises(self, written: SidecarPaths) -> None:
        written.stamp.unlink()
        with pytest.raises(TradeStoreError, match="stamp missing or invalid"):
            read_trade_page(written, run_id=_RUN_A, offset=0, limit=5)

    def test_span_desync_fails_loud(self, written: SidecarPaths) -> None:
        payload = json.loads(written.stamp.read_text())
        payload["trade_spans"] = {_RUN_A: [3, 0], _RUN_Z: [0, 3]}  # swapped
        written.stamp.write_text(json.dumps(payload))
        with pytest.raises(TradeStoreError, match="diverges from the stamped"):
            read_trade_page(written, run_id=_RUN_Z, offset=0, limit=5)

    def test_read_without_polars_raises_hint(
        self, written: SidecarPaths, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "polars", None)
        with pytest.raises(TradeStoreError, match="requires 'polars'"):
            read_trade_page(written, run_id=_RUN_A, offset=0, limit=5)

    def test_hash_matched_garbage_trades_file_fails_typed(self, written: SidecarPaths) -> None:
        # A stamp whose trades hash/size match a NON-Parquet file: load_stamp
        # passes, the scan fails → the typed unreadable error (never a raw
        # polars crash into a route).
        garbage = b"garbage, not parquet"
        written.trades.write_bytes(garbage)
        payload = json.loads(written.stamp.read_text())
        payload["content_hashes"]["trades"] = hashlib.sha256(garbage).hexdigest()
        payload["content_sizes"]["trades"] = len(garbage)
        written.stamp.write_text(json.dumps(payload))
        with pytest.raises(TradeStoreError, match="unreadable trades sidecar"):
            read_trade_page(written, run_id=_RUN_A, offset=0, limit=5)


class TestIterTradesForExport:
    def test_unknown_run_id_yields_nothing(self, written: SidecarPaths) -> None:
        assert list(iter_trades_for_export(written, "00" * 32)) == []

    def test_invalid_stamp_raises(self, written: SidecarPaths) -> None:
        written.stamp.write_text("{}")
        with pytest.raises(TradeStoreError, match="stamp missing or invalid"):
            list(iter_trades_for_export(written, _RUN_A))


def _mutate_stamp(paths: SidecarPaths, mutate: Any) -> None:
    payload = json.loads(paths.stamp.read_text())
    mutate(payload)
    paths.stamp.write_text(json.dumps(payload))


class TestLoadStampDamageMatrix:
    def test_valid_stamp_loads(self, written: SidecarPaths) -> None:
        assert load_stamp(written) is not None

    def test_missing_stamp(self, written: SidecarPaths) -> None:
        written.stamp.unlink()
        assert load_stamp(written) is None

    def test_torn_json(self, written: SidecarPaths) -> None:
        written.stamp.write_text('{"schema_version": 1, "store_conte')
        assert load_stamp(written) is None

    def test_not_an_object(self, written: SidecarPaths) -> None:
        written.stamp.write_text("[1, 2, 3]")
        assert load_stamp(written) is None

    def test_wrong_schema_version(self, written: SidecarPaths) -> None:
        _mutate_stamp(written, lambda p: p.__setitem__("schema_version", 2))
        assert load_stamp(written) is None

    @pytest.mark.parametrize("field", ["run_ids", "trade_rows", "agg_rows", "equity_rows"])
    def test_bad_counts(self, written: SidecarPaths, field: str) -> None:
        _mutate_stamp(written, lambda p: p.__setitem__(field, -1))
        assert load_stamp(written) is None

    def test_bool_count_rejected(self, written: SidecarPaths) -> None:
        _mutate_stamp(written, lambda p: p.__setitem__("run_ids", True))
        assert load_stamp(written) is None

    def test_missing_count_field(self, written: SidecarPaths) -> None:
        _mutate_stamp(written, lambda p: p.__delitem__("agg_rows"))
        assert load_stamp(written) is None

    def test_bad_store_hash(self, written: SidecarPaths) -> None:
        _mutate_stamp(written, lambda p: p.__setitem__("store_content_hash", "AB" * 32))
        assert load_stamp(written) is None

    def test_content_hashes_not_dict(self, written: SidecarPaths) -> None:
        _mutate_stamp(written, lambda p: p.__setitem__("content_hashes", []))
        assert load_stamp(written) is None

    def test_content_hashes_missing_key(self, written: SidecarPaths) -> None:
        _mutate_stamp(written, lambda p: p["content_hashes"].pop("aggs"))
        assert load_stamp(written) is None

    def test_content_hashes_non_hex(self, written: SidecarPaths) -> None:
        _mutate_stamp(written, lambda p: p["content_hashes"].__setitem__("trades", "z" * 64))
        assert load_stamp(written) is None

    def test_trade_spans_not_dict(self, written: SidecarPaths) -> None:
        _mutate_stamp(written, lambda p: p.__setitem__("trade_spans", [1, 2]))
        assert load_stamp(written) is None

    @pytest.mark.parametrize("span", ["ab", [1], [1, 2, 3], [-1, 0], [0, True]])
    def test_malformed_span(self, written: SidecarPaths, span: object) -> None:
        _mutate_stamp(written, lambda p: p["trade_spans"].__setitem__(_RUN_A, span))
        assert load_stamp(written) is None

    def test_span_sum_mismatch(self, written: SidecarPaths) -> None:
        _mutate_stamp(written, lambda p: p["trade_spans"].__setitem__(_RUN_Z, [3, 7]))
        assert load_stamp(written) is None

    def test_bad_produced_at(self, written: SidecarPaths) -> None:
        _mutate_stamp(written, lambda p: p.__setitem__("produced_at_utc", 5))
        assert load_stamp(written) is None

    def test_missing_sidecar_file(self, written: SidecarPaths) -> None:
        written.equity.unlink()
        assert load_stamp(written) is None

    def test_bad_content_sizes(self, written: SidecarPaths) -> None:
        _mutate_stamp(written, lambda p: p.__setitem__("content_sizes", {"trades": 1}))
        assert load_stamp(written) is None

    def test_torn_file_caught_by_size_probe(self, written: SidecarPaths) -> None:
        # A grown/truncated file fails the O(1) stat probe on BOTH tiers.
        written.trades.write_bytes(written.trades.read_bytes() + b"x")
        assert load_stamp(written) is None
        assert load_stamp(written, verify_hashes=False) is None

    def test_same_size_corruption_needs_the_hash_tier(self, written: SidecarPaths) -> None:
        # Flip bytes IN PLACE keeping the size: the full-hash tier (the pass's
        # skip/rebuild decision) catches it; the page-read stat tier honestly
        # does not (the documented envelope — no sanctioned writer does this).
        content = bytearray(written.trades.read_bytes())
        content[-1] ^= 0xFF
        written.trades.write_bytes(bytes(content))
        assert load_stamp(written) is None  # hash tier → rebuild
        assert load_stamp(written, verify_hashes=False) is not None

    def test_stat_ok_but_read_fails_on_hash_tier(
        self, written: SidecarPaths, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # stat succeeds but the content read errors (I/O fault mid-validation).
        # DEC-LAKE-READ-INODE-GUARD-001: the hash-tier read rides the guarded
        # deadline reader now — fault THAT seam (Path.read_bytes is unused).
        from brutex.engine import trade_store as ts_mod

        original = ts_mod.read_regular_bytes_deadline

        def _flaky(path: Path, **kwargs: object) -> bytes:
            if path == written.equity:
                raise OSError("injected read fault")
            return original(path, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(ts_mod, "read_regular_bytes_deadline", _flaky)
        assert load_stamp(written) is None


class TestValidatorEdges:
    def test_is_sha256(self) -> None:
        assert _is_sha256("ab" * 32) is True
        assert _is_sha256(42) is False
        assert _is_sha256("ab" * 31) is False
        assert _is_sha256("AB" * 32) is False
        assert _is_sha256("zz" * 32) is False

    def test_is_count(self) -> None:
        assert _is_count(0) is True
        assert _is_count(7) is True
        assert _is_count(-1) is False
        assert _is_count(True) is False
        assert _is_count("3") is False


def test_agg_columns_cover_the_b2_schema() -> None:
    """The B2 schema authority: 50 pinned columns, run_id first.

    49 → 50: ``exits_session_gap`` joined the per-reason counter block
    (DEC-TRADESIM-OPEN-SESSION-GUARD-001 — the exit-reason vocabulary is
    append-only, and the agg schema tracks it in lockstep).
    """
    names = [name for name, _ in AGG_COLUMNS]
    assert names[0] == "run_id"
    assert len(names) == len(set(names)) == 50
    assert "exits_session_gap" in names
    assert "fingerprint" in names
    assert "composite_score" in names
