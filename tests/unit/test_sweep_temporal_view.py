"""Unit tests for :mod:`brutex.dashboard.sweep_temporal_view` (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001).

The pure serve-time drill logic: run_id validation (hostile values rejected
disk-free), the stored quarter/month payloads + the derived half-year roll-up,
the ``sweep_runs`` invalidation token, the sidecar index over REAL
writer-produced stamps (``write_tuple_sidecars`` — never hand-faked artifacts),
and the day/week fold verified against an INDEPENDENT test-side oracle (the
#1154 surface-trial discipline).
"""

# The fold tests read module-private helpers deliberately.
# pyright: reportPrivateUsage=none
from __future__ import annotations

import os
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars", reason="the sidecar reads go through polars")

from brutex.calendars.nse import IST
from brutex.dashboard.sweep_temporal_view import (
    STAMP_SUFFIX,
    TEMPORAL_HORIZONS,
    _half_year_rollup,
    _paths_from_stamp,
    _period_key,
    build_sidecar_index,
    is_valid_run_id,
    sidecar_horizon_payload,
    stamp_stat_token,
    stored_horizon_payload,
    sweep_runs_token,
)
from brutex.engine.minute_path import PathStatus, SessionEquityPoint
from brutex.engine.period_stats import encode_judged_years, encode_period_stats
from brutex.engine.results_store import ResultRow
from brutex.engine.trade_capture import CapturedTrade, ComboCapture
from brutex.engine.trade_store import (
    SidecarPaths,
    sidecar_paths_for,
    write_tuple_sidecars,
)
from brutex.strategies.types import Side
from structlog.testing import capture_logs

_RUN_A = "aa" * 32
_RUN_B = "bb" * 32
_STORE_HASH = "12" * 32


def _ts(day: date, hh: int, mm: int) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST).astimezone(UTC)


def _trade(day: date, seq: int, *, pnl: int) -> CapturedTrade:
    return CapturedTrade(
        entry_ts=_ts(day, 9, 16 + 2 * seq),
        entry_px=Decimal("100.00"),
        exit_ts=_ts(day, 9, 17 + 2 * seq),
        exit_px=Decimal("100.50") if pnl >= 0 else Decimal("99.70"),
        direction=Side.BUY,
        lots=None,
        exit_reason="take_profit",
        pnl_paisa=pnl,
        costs_paisa=None,
        mae_paisa=10,
        mfe_paisa=60,
        mae_1m_paisa=None,
        mfe_1m_paisa=None,
        path_1m_status=PathStatus.NO_1M_DATA,
        vix_entry=None,
        vix_exit=None,
    )


#: Two IST sessions in DIFFERENT ISO weeks (Fri 2024-01-05 / Mon 2024-01-08).
_DAY_1 = date(2024, 1, 5)
_DAY_2 = date(2024, 1, 8)
_TRADES = (
    _trade(_DAY_1, 0, pnl=50),
    _trade(_DAY_1, 1, pnl=-30),
    _trade(_DAY_2, 2, pnl=20),
)


def _write_sidecars(lake_root: Path, *, grid_run: str, run_id: str = _RUN_A) -> SidecarPaths:
    """REAL writer-produced sidecar trio + stamp under ``<lake>/sweep_runs``."""
    store = lake_root / "sweeps" / "NIFTY" / "1minute" / f"bull_k1_{'0' * 16}.parquet"
    paths = sidecar_paths_for(store, lake_root / "sweeps", run_id=grid_run)
    capture = ComboCapture(
        run_id=run_id,
        trades=_TRADES,
        equity=(SessionEquityPoint(_DAY_1, 10_000_020, 9_999_970, 3),),
        equity_status=PathStatus.OK,
        vix_missing_trades=3,
        parity_ok=True,
        parity_detail="",
    )
    write_tuple_sidecars(paths, [capture], [], store_content_hash=_STORE_HASH)
    return paths


def _row(*, temporal: bool) -> ResultRow:
    """A valid row; ``temporal=True`` adds CONSISTENT stored period columns."""
    kwargs: dict[str, str] = {}
    if temporal:
        months = {"2021-01": (2, 500, 1, 100), "2021-07": (1, -200, 0, 200)}
        quarters = {"2021Q1": (2, 500, 1, 100), "2021Q3": (1, -200, 0, 200)}
        years = {"2021": (3, 300, 1, 300)}
        kwargs = {
            "stats_by_year_json": encode_period_stats(years),
            "stats_by_quarter_json": encode_period_stats(quarters),
            "stats_by_month_json": encode_period_stats(months),
            "temporal_judged_years_json": encode_judged_years([2021]),
        }
    return ResultRow(
        run_id=_RUN_A,
        instrument="NIFTY",
        tf="5m",
        direction="bull",
        mode="A",
        mask_bits=1,
        net_pnl_paisa=300,
        gross_profit_paisa=500,
        gross_loss_paisa=200,
        max_drawdown_paisa=300,
        expectancy_paisa=100,
        total_trades=3,
        winning_trades=1,
        losing_trades=2,
        sharpe_ratio=1.0,
        sortino_ratio=1.2,
        calmar_ratio=0.9,
        profit_factor=2.5,
        win_rate=0.33,
        max_drawdown_pct=0.05,
        avg_entry_price=Decimal("100.00"),
        avg_exit_price=Decimal("101.00"),
        **kwargs,  # type: ignore[arg-type]
    )


def _temporal_row() -> ResultRow:
    return _row(temporal=True)


def _bare_row() -> ResultRow:
    return _row(temporal=False)


class TestRunIdValidation:
    def test_valid_64_hex(self) -> None:
        assert is_valid_run_id(_RUN_A)

    @pytest.mark.parametrize(
        "hostile",
        ["", "AA" * 32, "g" * 64, "a" * 63, "a" * 65, "../" + "a" * 61, "a" * 64 + "\n"],
    )
    def test_hostile_values_rejected(self, hostile: str) -> None:
        assert not is_valid_run_id(hostile)

    def test_horizon_vocabulary(self) -> None:
        assert TEMPORAL_HORIZONS == ("quarter", "month", "day", "week")


class TestStoredHorizonPayload:
    def test_unknown_run_id_is_absent(self) -> None:
        payload = stored_horizon_payload(None, run_id=_RUN_A, horizon="quarter")
        assert payload["status"] == "absent"
        assert "unknown run_id" in payload["detail"]

    def test_pre_extension_row_is_absent_with_remediation(self) -> None:
        payload = stored_horizon_payload(_bare_row(), run_id=_RUN_A, horizon="month")
        assert payload["status"] == "absent"
        assert "re-sweep to populate" in payload["detail"]

    def test_quarter_serves_cells_and_derived_half_year(self) -> None:
        payload = stored_horizon_payload(_temporal_row(), run_id=_RUN_A, horizon="quarter")
        assert payload["status"] == "ok"
        assert payload["source"] == "stored"
        assert payload["cells"] == {"2021Q1": [2, 500, 1, 100], "2021Q3": [1, -200, 0, 200]}
        # Half-year: Q1 → H1, Q3 → H2 — n/pnl/wins exact sums, NO dd (not derivable).
        assert payload["half_year"] == {"2021H1": [2, 500, 1], "2021H2": [1, -200, 0]}

    def test_month_serves_cells_without_half_year(self) -> None:
        payload = stored_horizon_payload(_temporal_row(), run_id=_RUN_A, horizon="month")
        assert payload["status"] == "ok"
        assert payload["cells"] == {"2021-01": [2, 500, 1, 100], "2021-07": [1, -200, 0, 200]}
        assert "half_year" not in payload

    def test_half_year_rollup_sums_q1_q2(self) -> None:
        halves = _half_year_rollup({"2022Q1": (1, 10, 1, 5), "2022Q2": (2, -4, 0, 9)})
        assert halves == {"2022H1": [3, 6, 1]}


class TestSweepRunsToken:
    def test_absent_tree_reads_stable_absent(self, tmp_path: Path) -> None:
        assert sweep_runs_token(tmp_path) == "absent"
        assert sweep_runs_token(tmp_path) == "absent"

    def test_stray_file_reads_absent(self, tmp_path: Path) -> None:
        (tmp_path / "sweep_runs").write_text("not a dir", encoding="utf-8")
        assert sweep_runs_token(tmp_path) == "absent"

    def test_new_run_dir_moves_the_token(self, tmp_path: Path) -> None:
        (tmp_path / "sweep_runs").mkdir()
        before = sweep_runs_token(tmp_path)
        assert before not in (None, "absent")
        (tmp_path / "sweep_runs" / "grid-a").mkdir()
        after = sweep_runs_token(tmp_path)
        assert after not in (None, "absent")
        assert after != before

    def test_probe_outage_reads_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "sweep_runs").mkdir()
        real_scandir = os.scandir

        def _deny(target: object, *args: object, **kwargs: object) -> object:
            if str(target).endswith("sweep_runs"):
                raise PermissionError("synthetic scandir outage")
            return real_scandir(target, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "scandir", _deny)
        with capture_logs() as logs:
            assert sweep_runs_token(tmp_path) is None
        assert [e for e in logs if e["event"] == "sweep_temporal_view.sweep_runs_token_failed"]


class TestSidecarIndex:
    def test_real_writer_stamps_index_their_run_ids(self, tmp_path: Path) -> None:
        written = _write_sidecars(tmp_path, grid_run="grid-a")
        index = build_sidecar_index(tmp_path)
        assert index[_RUN_A] == written
        assert stamp_stat_token(written) != "absent"

    def test_absent_tree_is_empty(self, tmp_path: Path) -> None:
        assert build_sidecar_index(tmp_path) == {}

    def test_duplicate_run_id_keeps_first_sorted(self, tmp_path: Path) -> None:
        first = _write_sidecars(tmp_path, grid_run="grid-a")
        _write_sidecars(tmp_path, grid_run="grid-b")
        index = build_sidecar_index(tmp_path)
        assert index[_RUN_A] == first  # "grid-a" sorts before "grid-b"

    def test_invalid_stamp_skipped_with_warn(self, tmp_path: Path) -> None:
        paths = _write_sidecars(tmp_path, grid_run="grid-a")
        paths.stamp.write_text("{corrupt", encoding="utf-8")
        with capture_logs() as logs:
            assert build_sidecar_index(tmp_path) == {}
        assert [e for e in logs if e["event"] == "sweep_temporal_view.stamp_invalid"]

    def test_paths_from_stamp_reconstructs_the_writer_layout(self, tmp_path: Path) -> None:
        written = _write_sidecars(tmp_path, grid_run="grid-a")
        assert _paths_from_stamp(written.stamp) == written
        assert written.stamp.name.endswith(STAMP_SUFFIX)

    def test_stamp_stat_token_absent(self, tmp_path: Path) -> None:
        missing = SidecarPaths(
            trades=tmp_path / "x.trades.parquet",
            aggs=tmp_path / "x.aggs.parquet",
            equity=tmp_path / "x.equity.parquet",
            stamp=tmp_path / "x.stamp.json",
        )
        assert stamp_stat_token(missing) == "absent"


class TestSidecarHorizonPayload:
    def test_missing_artifacts_read_awaiting(self) -> None:
        payload = sidecar_horizon_payload(None, run_id=_RUN_A, horizon="day")
        assert payload["status"] == "awaiting"
        assert "BRUTEX_RESULTS_DEPTH" in payload["detail"]

    def test_day_fold_matches_independent_oracle(self, tmp_path: Path) -> None:
        """#1154 discipline: the served buckets equal a hand-rolled test-side
        fold over the SAME trade definitions (never the code under test).
        """
        paths = _write_sidecars(tmp_path, grid_run="grid-a")
        payload = sidecar_horizon_payload(paths, run_id=_RUN_A, horizon="day")
        assert payload["status"] == "ok"
        assert payload["source"] == "results_depth_sidecar"
        oracle: dict[str, list[int]] = {}
        for trade in _TRADES:
            key = trade.exit_ts.astimezone(IST).date().isoformat()
            cell = oracle.setdefault(key, [0, 0, 0])
            cell[0] += 1
            cell[1] += trade.pnl_paisa
            cell[2] += 1 if trade.pnl_paisa > 0 else 0
        assert payload["cells"] == oracle
        assert payload["cells"] == {"2024-01-05": [2, 20, 1], "2024-01-08": [1, 20, 1]}

    def test_week_fold_buckets_by_iso_week(self, tmp_path: Path) -> None:
        paths = _write_sidecars(tmp_path, grid_run="grid-a")
        payload = sidecar_horizon_payload(paths, run_id=_RUN_A, horizon="week")
        # Fri 2024-01-05 is ISO 2024-W01; Mon 2024-01-08 is ISO 2024-W02.
        assert payload["cells"] == {"2024-W01": [2, 20, 1], "2024-W02": [1, 20, 1]}

    def test_unknown_run_id_pages_empty_ok(self, tmp_path: Path) -> None:
        """A stamp that exists but has no span for this run_id serves an honest
        EMPTY ok payload (the read_trade_page total-0 law), never a raise.
        """
        paths = _write_sidecars(tmp_path, grid_run="grid-a")
        payload = sidecar_horizon_payload(paths, run_id=_RUN_B, horizon="day")
        assert payload["status"] == "ok"
        assert payload["cells"] == {}

    def test_mid_serve_store_fault_reads_awaiting(self, tmp_path: Path) -> None:
        paths = _write_sidecars(tmp_path, grid_run="grid-a")
        paths.stamp.unlink()  # vanished between index build and serve
        with capture_logs() as logs:
            payload = sidecar_horizon_payload(paths, run_id=_RUN_A, horizon="day")
        assert payload["status"] == "awaiting"
        assert "rebuild" in payload["detail"]
        assert [e for e in logs if e["event"] == "sweep_temporal_view.sidecar_unreadable"]

    def test_period_key_iso_year_boundary(self) -> None:
        """2021-01-01 IST belongs to ISO week 2020-W53 — the GGGG law, pinned."""
        ts = datetime(2021, 1, 1, 10, 0, tzinfo=IST).astimezone(UTC).isoformat()
        assert _period_key(ts, horizon="week") == "2020-W53"
        assert _period_key(ts, horizon="day") == "2021-01-01"
