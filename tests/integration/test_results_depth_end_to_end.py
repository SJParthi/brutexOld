"""End-to-end integration: REAL sweep → results-depth pass → binding artifacts.

DEC-SWEEP-RESULTS-DEPTH-001 (WP5). Runs the REAL ``run_persisted_sweep`` over
a small two-arm grid (the sweep golden's 12-day fixture bars), then the REAL
``run_results_depth_pass`` with 1-minute + VIX feeds, and asserts every
artifact against the BINDING P3→P4 contract shapes:

* trade rows (paged via ``read_trade_page`` + streamed via
  ``iter_trades_for_export``) carry EXACTLY the contract keys + the
  documented additive keys, offsets partition, totals come from the stamp;
* ``<sweep_runs root>/<run_id>/top100.json`` exists at the Ruling-4 contract
  path (the ``sweep_runs/`` SIBLING root beside the sweeps tree) with the
  ordered ``{rank, mask_hex, conditions[], tf, direction, arm, metrics{},
  regime{}, vix_band{}, family_id}`` entry shape + the stage-stamp sidecar;
* the baseline artifact loads;
* a corrupted top100.json is stamp-detected (``load_top100 → None``) and the
  re-run pass (stamp-skip resume — zero re-captures) rebuilds it to the
  IDENTICAL bytes (R42's deterministic mid-chain recovery).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.baseline import load_baseline
from brutex.engine.results_depth_pass import ResultsDepthReport, run_results_depth_pass
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    Timeframe,
    iter_sweep_tuples,
    load_or_create_store,
    tuple_store_path,
)
from brutex.engine.sweep_run import ComboDedup, run_persisted_sweep
from brutex.engine.sweep_supervisor import grid_identity
from brutex.engine.top100_artifact import (
    TOP100_FILENAME,
    TOP100_STAMP_FILENAME,
    load_top100,
)
from brutex.engine.trade_store import (
    iter_trades_for_export,
    load_stamp,
    read_trade_page,
    sidecar_paths_for,
    sweep_run_dir,
)
from brutex.orchestrator.audit import AuditWriter
from brutex.strategies.types import Side

if TYPE_CHECKING:
    from brutex.engine.trade_store import SidecarPaths

_IST = timezone(timedelta(hours=5, minutes=30))

#: The BINDING P3→P4 trade-row contract keys, verbatim (integration contract
#: file: "Trade list (P3→P4) ... row shape {entry_ts, entry_px, exit_ts,
#: exit_px, direction, lots, exit_reason, pnl_paisa, costs_paisa, mae, mfe,
#: vix_entry, vix_exit}").
_CONTRACT_TRADE_KEYS = frozenset(
    {
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
    }
)

#: The documented ADDITIVE wire keys (design A.3 / coordinator contract note).
_ADDITIVE_TRADE_KEYS = frozenset({"trade_seq", "path_1m_status", "mae_granularity"})

#: The BINDING top100 entry contract keys, verbatim.
_CONTRACT_ENTRY_KEYS = frozenset(
    {
        "rank",
        "mask_hex",
        "conditions",
        "tf",
        "direction",
        "arm",
        "metrics",
        "regime",
        "vix_band",
        "family_id",
    }
)


def _bars() -> list[CandleBar]:
    """The worst-case-rankable 12-day gap-up drift (DEC-FILL-WORST-CASE-ONLY-001).

    Narrow gap-up bars (range 0.50 < the SL 0.80; the next gap-up's low
    clears the TP level so wins bank +50), a shallow bearish bar each 7-bar
    cycle, and a DEEP bearish bar (range 0.85) every 4th cycle whose stop-out
    keeps profit_factor finite — every k=1 mask over bits 0-4 clears the hard
    rank gates at sl=80/tp=50 under the one worst-case law.
    """
    out: list[CandleBar] = []
    cycle = 0
    for d in range(12):
        price = 100.0
        for m in range(120):
            k = m % 7
            if k == 0:
                cycle += 1
            if k == 3:  # shallow bearish bar
                o = price
                c, h, lo = o - 0.3, o + 0.1, o - 0.4
            elif k == 4 and cycle % 4 == 0:  # deep bearish loss carrier
                o = price
                c, h, lo = o - 0.65, o + 0.1, o - 0.75
            else:  # narrow gap-up win bar
                o = price + 3.0
                c, h, lo = o + 0.3, o + 0.4, o - 0.1
            ts = (
                datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=d, minutes=m)
            ).astimezone(UTC)
            out.append(
                CandleBar(
                    timestamp=ts,
                    open=Decimal(str(round(o, 2))),
                    high=Decimal(str(round(h, 2))),
                    low=Decimal(str(round(lo, 2))),
                    close=Decimal(str(round(c, 2))),
                    volume=100,
                    open_interest=None,
                )
            )
            price = c
    return out


_BARS = _bars()


def _vix_bars() -> list[CandleBar]:
    """A full-coverage synthetic India-VIX series on the SAME minute grid."""
    out: list[CandleBar] = []
    for i, bar in enumerate(_BARS):
        level = Decimal(str(round(14.0 + (i % 40) / 10.0, 2)))
        out.append(
            CandleBar(
                timestamp=bar.timestamp,
                open=level,
                high=level,
                low=level,
                close=level,
                volume=0,
                open_interest=None,
            )
        )
    return out


def _grid() -> SweepGridConfig:
    return SweepGridConfig(
        instruments=["NIFTY"],
        timeframes=[Timeframe(label="1minute", minutes=1)],
        directions=[Side.BUY],
        k_values=[1],
        condition_bits=[0, 1, 2, 3, 4],
        stop_losses=[80],
        take_profits=[50, None],  # TWO SL/TP arms → two tuples (R19 shape)
        proximities=[None],
        mode="A",
        data_snapshot_hash="snap0001",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
    )


@pytest.fixture(scope="module")
def chain(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, ResultsDepthReport]:
    """REAL sweep over both arms, then the REAL pass — built once."""
    out_root = tmp_path_factory.mktemp("e2e") / "sweeps"
    out_root.mkdir()
    grid = _grid()
    audit_path = out_root / "audit" / "sweep-all.jsonl"
    audit_path.parent.mkdir(parents=True)
    with AuditWriter(path=audit_path) as audit:
        for tuple_ in iter_sweep_tuples(grid):
            store = load_or_create_store(tuple_store_path(out_root, tuple_, grid))
            dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
            run_persisted_sweep(
                _BARS,
                tuple_.to_run_config(grid),
                store=store,
                dedup=dedup,
                audit=audit,
                exec_bars_1m=_BARS,
            )
    report = run_results_depth_pass(
        out_root=out_root,
        grid=grid,
        bars_for=lambda _instrument, _tf: _BARS,
        minute_bars_for=lambda _instrument: _BARS,
        vix_bars=_vix_bars(),
    )
    return out_root, report


def _first_tuple_paths(out_root: Path) -> SidecarPaths:
    grid = _grid()
    return sidecar_paths_for(
        tuple_store_path(out_root, next(iter_sweep_tuples(grid)), grid),
        out_root,
        run_id=grid_identity(grid),
    )


class TestReportAndSidecars:
    def test_report_counters_are_coherent(self, chain: tuple[Path, ResultsDepthReport]) -> None:
        _out_root, report = chain
        assert report.tuples_processed == 2
        assert report.tuples_errored == 0
        assert report.tuples_missing_store == 0
        assert report.combos_parity_failed == 0
        assert report.combos_captured == report.pool  # every capture parity-clean
        assert report.vix_missing_trades == 0  # full-coverage VIX feed
        assert report.baselines_written == 1
        assert report.top100_written is True
        assert report.top100_entries == report.gate_passers - report.families_collapsed

    def test_sidecar_trio_and_stamp_validate(self, chain: tuple[Path, ResultsDepthReport]) -> None:
        out_root, _report = chain
        grid = _grid()
        for tuple_ in iter_sweep_tuples(grid):
            paths = sidecar_paths_for(
                tuple_store_path(out_root, tuple_, grid), out_root, run_id=grid_identity(grid)
            )
            stamp = load_stamp(paths)  # full-hash tier
            assert stamp is not None
            assert stamp.trade_rows == sum(count for _, count in stamp.trade_spans.values())


class TestTradeListContract:
    def test_wire_rows_carry_exactly_the_contract_plus_additive_keys(
        self, chain: tuple[Path, ResultsDepthReport]
    ) -> None:
        out_root, _report = chain
        paths = _first_tuple_paths(out_root)
        stamp = load_stamp(paths)
        assert stamp is not None
        run_id = next(run for run, (_start, count) in stamp.trade_spans.items() if count > 0)
        page = read_trade_page(paths, run_id=run_id, offset=0, limit=50)
        assert page.total > 0
        for row in page.rows:
            assert set(row.keys()) == _CONTRACT_TRADE_KEYS | _ADDITIVE_TRADE_KEYS
            assert row["direction"] == "BUY"
            # NULL-by-design until P5 (Ruling 18) — never a fabricated 1/0.
            assert row["lots"] is None
            assert row["costs_paisa"] is None
            assert row["vix_entry"] is not None  # the full-coverage VIX feed
            assert row["mae_granularity"] == "1m"  # the 1-minute feed covered every trade
            assert row["path_1m_status"] == "ok"
            # ISO-8601 UTC timestamps parse back.
            assert datetime.fromisoformat(row["entry_ts"]).tzinfo is not None

    def test_page_offsets_partition_and_match_the_export_stream(
        self, chain: tuple[Path, ResultsDepthReport]
    ) -> None:
        out_root, _report = chain
        paths = _first_tuple_paths(out_root)
        stamp = load_stamp(paths)
        assert stamp is not None
        run_id, (_start, total) = max(stamp.trade_spans.items(), key=lambda item: item[1][1])
        assert total > 0
        limit = 7
        paged = []
        for offset in range(0, total, limit):
            page = read_trade_page(paths, run_id=run_id, offset=offset, limit=limit)
            assert page.total == total
            paged.extend(page.rows)
        streamed = list(iter_trades_for_export(paths, run_id))
        assert len(streamed) == total
        assert paged == streamed
        assert [row["trade_seq"] for row in streamed] == list(range(total))


class TestTop100Contract:
    def test_artifact_lives_at_the_contract_path_with_the_binding_shape(
        self, chain: tuple[Path, ResultsDepthReport]
    ) -> None:
        out_root, report = chain
        run_id = grid_identity(_grid())
        run_dir = sweep_run_dir(out_root, run_id)
        assert not run_dir.is_relative_to(out_root)  # Ruling 4: a SIBLING root
        artifact_path = run_dir / TOP100_FILENAME
        assert artifact_path.exists()
        assert (run_dir / TOP100_STAMP_FILENAME).exists()
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        entries = payload["entries"]
        assert isinstance(entries, list)
        assert len(entries) == report.top100_entries > 0
        for position, entry in enumerate(entries, start=1):
            assert _CONTRACT_ENTRY_KEYS.issubset(entry.keys())
            assert entry["rank"] == position  # ordered list order == rank order
            assert len(entry["mask_hex"]) == 32
            assert entry["conditions"]  # names, never empty for a k>=1 mask
            assert set(entry["arm"].keys()) == {"sl_paisa", "tp_paisa"}
            assert entry["vix_band"]["stamped"] > 0
        # The stamped-vs-validated reader agrees with the raw JSON.
        loaded = load_top100(run_dir.parent, run_id)
        assert loaded is not None
        assert loaded.to_payload() == payload

    def test_baseline_artifact_loads(self, chain: tuple[Path, ResultsDepthReport]) -> None:
        out_root, _report = chain
        run_id = grid_identity(_grid())
        baseline_path = sweep_run_dir(out_root, run_id) / "baseline-NIFTY.json"
        series = load_baseline(baseline_path)
        assert series is not None
        assert series.instrument == "NIFTY"
        assert len(series.sessions) == 12  # one point per fixture session


class TestCorruptionRecovery:
    def test_corrupt_top100_is_detected_and_rebuilt_byte_identically(
        self, chain: tuple[Path, ResultsDepthReport]
    ) -> None:
        out_root, _report = chain
        grid = _grid()
        run_id = grid_identity(grid)
        run_dir = sweep_run_dir(out_root, run_id)
        artifact_path = run_dir / TOP100_FILENAME
        pristine = artifact_path.read_bytes()
        artifact_path.write_bytes(b'{"schema_version": 1, "tampered": true}')
        assert load_top100(run_dir.parent, run_id) is None
        rerun = run_results_depth_pass(
            out_root=out_root,
            grid=grid,
            bars_for=lambda _instrument, _tf: _BARS,
            minute_bars_for=lambda _instrument: _BARS,
            vix_bars=_vix_bars(),
        )
        assert rerun.tuples_processed == 0  # stamp-skip resume — zero re-captures
        assert rerun.tuples_skipped_current == 2
        assert rerun.top100_written is True
        assert artifact_path.read_bytes() == pristine
        assert load_top100(run_dir.parent, run_id) is not None
