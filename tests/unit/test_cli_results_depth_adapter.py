"""Unit suite for the cli results-depth run-end adapter (DEC-SWEEP-RESULTS-DEPTH-001, WP6).

Covers the extracted, non-pragma'd :func:`brutex.cli._run_results_depth_adapter`
in full: the WP3 provider wiring (phrases reasons + margin-true ROI), the
honest report line, the fence (loud print + log, sweep never fails), the
global-stop pass-through, and the default-runner arm driving the REAL
:func:`run_results_depth_pass` over an empty store tree (0-entry top100
written). The kill-switch PARSE law itself lives in
``test_engine_results_depth_pass.py`` (``resolve_results_depth_enabled``);
its production wiring is proven end-to-end in
``tests/integration/test_cli_results_depth_wiring.py``.
"""

# Whitebox suite: drives cli._run_results_depth_adapter /
# _print_resilience_lines directly (DEC-P0-017).
# pyright: reportPrivateUsage=none

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from brutex import cli
from brutex.engine.results_depth_pass import ResultsDepthReport
from brutex.ranking.phrases import reason_payload
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from pathlib import Path

pytest.importorskip("polars")

_ENTRY_DATE = date(2024, 1, 2)


def _report(**overrides: Any) -> ResultsDepthReport:
    base: dict[str, Any] = {
        "tuples_processed": 1,
        "tuples_skipped_current": 2,
        "tuples_missing_store": 0,
        "tuples_errored": 0,
        "resilience_backfilled": 0,
        "resilience_errored": 0,
        "combos_captured": 7,
        "combos_parity_failed": 0,
        "combos_zero_trades": 1,
        "vix_missing_trades": 3,
        "baselines_written": 1,
        "pool": 6,
        "gate_passers": 4,
        "excluded_no_aggs": 0,
        "zero_trade_excluded": 1,
        "parity_excluded": 0,
        "families_collapsed": 1,
        "top100_written": True,
        "top100_entries": 4,
    }
    base.update(overrides)
    return ResultsDepthReport(**base)


def _call(tmp_path: Path, runner: Any, **overrides: Any) -> bool:
    kwargs: dict[str, Any] = {
        "out_root": tmp_path / "sweeps",
        "grid": SimpleNamespace(),
        "bars_for": lambda _instrument, _tf: [],
        "minute_bars_for": lambda _instrument: None,
        "daily_bars_for": lambda _instrument: None,
        "vix_bars": None,
        "margin_entry_date": _ENTRY_DATE,
        "pass_runner": runner,
    }
    kwargs.update(overrides)
    return cli._run_results_depth_adapter(**kwargs)


class TestProviderWiring:
    def test_pass_receives_the_wp3_providers_and_the_report_prints(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        captured: dict[str, Any] = {}

        def _runner(**kwargs: Any) -> ResultsDepthReport:
            captured.update(kwargs)
            return _report()

        ok = _call(tmp_path, _runner)

        assert ok is True
        # The reasons provider is EXACTLY the phrases wire function — the ONE
        # R33 table, never a re-derived copy (CLAUDE.md §11 COMMON).
        assert captured["reason_provider"] is reason_payload
        # The margin provider closes over margin_roi_payload with lots=1 at
        # the sweep-window start date — the v1 verdict is honest "unverified".
        candidate = SimpleNamespace(row=SimpleNamespace(net_pnl_paisa=12_345, instrument="NIFTY"))
        payload = captured["margin_roi_provider"](candidate)
        assert payload["status"] == "unverified"
        assert payload["roi_pct"] is None
        assert "UNVERIFIED" in payload["detail"]
        # Every seam threads through unchanged.
        assert captured["out_root"] == tmp_path / "sweeps"
        assert captured["vix_bars"] is None
        out = capsys.readouterr().out
        assert (
            "[brutex] results-depth — tuples=1 skipped_current=2 missing_store=0 "
            "errored=0 resilience_errored=0 captured=7 parity_failed=0 pool=6 "
            "gate_passers=4 baselines=1 top100=written entries=4"
        ) in out

    def test_failed_top100_reads_failed_in_the_report_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ok = _call(tmp_path, lambda **_kwargs: _report(top100_written=False, top100_entries=0))
        assert ok is True  # the PASS completed; the artifact failure is named
        assert "top100=FAILED entries=0" in capsys.readouterr().out


class TestFence:
    def test_pass_failure_prints_loud_logs_and_never_raises(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def _boom(**_kwargs: Any) -> ResultsDepthReport:
            raise RuntimeError("sidecar write exploded")

        with capture_logs() as logs:
            ok = _call(tmp_path, _boom)

        assert ok is False
        err = capsys.readouterr().err
        assert "results-depth pass FAILED (RuntimeError: sidecar write exploded)" in err
        assert "sweep itself SUCCEEDED" in err
        assert "idempotent stamp-skip resume" in err
        events = [log for log in logs if log["event"] == "cli.results_depth.pass_failed"]
        assert len(events) == 1
        assert events[0]["error_type"] == "RuntimeError"

    @pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit, MemoryError])
    def test_global_stops_reraise_raw(self, tmp_path: Path, exc_type: type[BaseException]) -> None:
        def _stop(**_kwargs: Any) -> ResultsDepthReport:
            raise exc_type

        with pytest.raises(exc_type):
            _call(tmp_path, _stop)


class TestDefaultRunnerArm:
    def test_none_runner_drives_the_real_pass(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``pass_runner=None`` runs the REAL run_results_depth_pass: a grid
        whose single store never swept counts missing + still writes the
        honest 0-entry top-100 artifact (provider-wired, ``*_wired=True``)."""
        import json

        from brutex.engine.sweep_orchestrator import SweepGridConfig, Timeframe
        from brutex.engine.sweep_supervisor import grid_identity
        from brutex.strategies.types import Side

        grid = SweepGridConfig(
            instruments=["NIFTY"],
            timeframes=[Timeframe(label="1minute", minutes=1)],
            directions=[Side.BUY],
            k_values=[1],
            condition_bits=[0, 1],
            stop_losses=[80],
            take_profits=[50],
            proximities=[None],
            mode="A",
            data_snapshot_hash="snap0001",
            indicator_version="ind-v1",
            code_commit_sha="deadbeef",
        )
        out_root = tmp_path / "sweeps"
        out_root.mkdir()

        ok = _call(tmp_path, None, grid=grid)

        assert ok is True
        out = capsys.readouterr().out
        assert "results-depth — tuples=0" in out
        assert "missing_store=1" in out
        assert "top100=written entries=0" in out
        # Ruling 4: the artifact lands in the sweep_runs SIBLING root.
        artifact_path = tmp_path / "sweep_runs" / grid_identity(grid) / "top100.json"
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        assert payload["entries"] == []
        assert payload["source"]["reasons_wired"] is True
        assert payload["source"]["margin_roi_wired"] is True


@dataclass(frozen=True, slots=True)
class _ResTrade:
    """A ``fold_resilience`` ``_TradeLike`` (frozen fields — pyright-clean)."""

    pnl_paisa: int
    entry_ts: datetime
    exit_ts: datetime


def _grid() -> Any:
    from brutex.engine.sweep_orchestrator import SweepGridConfig, Timeframe
    from brutex.strategies.types import Side

    return SweepGridConfig(
        instruments=["NIFTY"],
        timeframes=[Timeframe(label="5minute", minutes=5)],
        directions=[Side.BUY],
        k_values=[1],
        condition_bits=[0, 1],
        stop_losses=[80],
        take_profits=[50],
        proximities=[None],
        mode="A",
        data_snapshot_hash="snap0001",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
    )


def _write_top100_with_resilience(out_root: Path, grid: Any) -> None:
    """Write a real top100.json (stamp + hash) whose entries carry a LIVE and
    a NULL resilience block — the on-disk artifact ``_print_resilience_lines``
    reads back."""
    from brutex.engine.resilience_streaks import fold_resilience, resilience_payload
    from brutex.engine.sweep_supervisor import grid_identity
    from brutex.engine.top100_artifact import TOP100_SCHEMA_VERSION, Top100Artifact, write_top100
    from brutex.engine.trade_store import sweep_run_dir

    def _trade(pnl: int, hour: int) -> _ResTrade:
        ts = datetime(2024, 1, 2, hour, 0, tzinfo=UTC)
        return _ResTrade(pnl_paisa=pnl, entry_ts=ts, exit_ts=ts)

    run_id = grid_identity(grid)
    live = fold_resilience(
        run_id,
        [_trade(-100, 10), _trade(-100, 11), _trade(50, 12)],
        parity_ok=True,
        start_equity_paisa=1_000_000,
        fingerprint="fp0",
    )

    def _entry(rank: int, max_losses: int, record: Any) -> dict[str, Any]:
        return {
            "rank": rank,
            "run_id": run_id,
            "mask_hex": "0" * 32,
            "conditions": [],
            "instrument": "NSE-NIFTY",
            "tf": "5m",
            "direction": "long",
            "arm": {"sl_paisa": None, "tp_paisa": None},
            "metrics": {"max_consecutive_losses": max_losses},
            "regime": {},
            "vix_band": None,
            "family_id": "fam",
            "family_members": ["fam"],
            "reasons": None,
            "margin_roi": None,
            "equity_ref": {"tuple": "t", "sidecar": "equity"},
            "resilience": resilience_payload(record),
        }

    artifact = Top100Artifact(
        schema_version=TOP100_SCHEMA_VERSION,
        run_id=run_id,
        preset="min_dd_first",
        source={},
        baselines={},
        entries=(_entry(1, 2, live), _entry(2, 0, None)),
    )
    write_top100(sweep_run_dir(out_root, run_id).parent, artifact)


class TestResilienceLines:
    """``_print_resilience_lines`` — the additive CLI resilience surface."""

    def test_not_written_prints_nothing(self, tmp_path: Path) -> None:
        lines: list[str] = []
        cli._print_resilience_lines(
            out_root=tmp_path / "sweeps",
            grid=_grid(),
            report=_report(top100_written=False),
            printer=lines.append,
        )
        assert lines == []

    def test_grid_identity_failure_is_fenced_and_logged(self, tmp_path: Path) -> None:
        lines: list[str] = []
        with capture_logs() as logs:
            cli._print_resilience_lines(
                out_root=tmp_path / "sweeps",
                grid=SimpleNamespace(),  # grid_identity chokes → fenced
                report=_report(top100_written=True),
                printer=lines.append,
            )
        assert lines == []
        events = [log for log in logs if log["event"] == "cli.resilience_line.skipped"]
        assert len(events) == 1

    @pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit, MemoryError])
    def test_global_stops_reraise(self, tmp_path: Path, exc_type: type[BaseException]) -> None:
        import brutex.engine.sweep_supervisor as supervisor

        def _boom(_grid: Any) -> str:
            raise exc_type

        original = supervisor.grid_identity
        supervisor.grid_identity = _boom  # type: ignore[assignment]
        try:
            with pytest.raises(exc_type):
                cli._print_resilience_lines(
                    out_root=tmp_path / "sweeps",
                    grid=_grid(),
                    report=_report(top100_written=True),
                    printer=lambda _line: None,
                )
        finally:
            supervisor.grid_identity = original  # type: ignore[assignment]

    def test_missing_artifact_prints_the_unreadable_line(self, tmp_path: Path) -> None:
        lines: list[str] = []
        cli._print_resilience_lines(
            out_root=tmp_path / "sweeps",
            grid=_grid(),
            report=_report(top100_written=True),  # nothing on disk → load None
            printer=lines.append,
        )
        assert len(lines) == 1
        assert "top100 unreadable for the summary lines" in lines[0]

    def test_empty_entries_prints_nothing(self, tmp_path: Path) -> None:
        from brutex.engine.sweep_supervisor import grid_identity
        from brutex.engine.top100_artifact import (
            TOP100_SCHEMA_VERSION,
            Top100Artifact,
            write_top100,
        )
        from brutex.engine.trade_store import sweep_run_dir

        grid = _grid()
        out_root = tmp_path / "sweeps"
        run_id = grid_identity(grid)
        write_top100(
            sweep_run_dir(out_root, run_id).parent,
            Top100Artifact(
                schema_version=TOP100_SCHEMA_VERSION,
                run_id=run_id,
                preset="min_dd_first",
                source={},
                baselines={},
                entries=(),
            ),
        )
        lines: list[str] = []
        cli._print_resilience_lines(
            out_root=out_root, grid=grid, report=_report(top100_written=True), printer=lines.append
        )
        assert lines == []

    def test_live_and_null_entries_print_lines_and_the_null_footer(self, tmp_path: Path) -> None:
        grid = _grid()
        out_root = tmp_path / "sweeps"
        _write_top100_with_resilience(out_root, grid)
        lines: list[str] = []
        cli._print_resilience_lines(
            out_root=out_root, grid=grid, report=_report(top100_written=True), printer=lines.append
        )
        # header + 2 entry lines + the honest NULL footer.
        assert len(lines) == 4
        assert "losing-streak endurance" in lines[0]
        # rank-1 LIVE line names the worst streak (2 in a row) + honest tail.
        assert "worst-streak 2" in lines[1]
        assert "signal-only gross per-unit" in lines[1]
        # rank-2 NULL line reads the honest em-dash.
        assert "— (parity-failed or no trades)" in lines[2]
        assert "⚠ 1 of 2 shown had NO resilience stats" in lines[3]

    def test_all_live_entries_print_no_null_footer(self, tmp_path: Path) -> None:
        from brutex.engine.resilience_streaks import fold_resilience, resilience_payload
        from brutex.engine.sweep_supervisor import grid_identity
        from brutex.engine.top100_artifact import (
            TOP100_SCHEMA_VERSION,
            Top100Artifact,
            write_top100,
        )
        from brutex.engine.trade_store import sweep_run_dir

        grid = _grid()
        out_root = tmp_path / "sweeps"
        run_id = grid_identity(grid)
        ts = datetime(2024, 1, 2, 10, 0, tzinfo=UTC)
        live = fold_resilience(
            run_id,
            [_ResTrade(pnl_paisa=-100, entry_ts=ts, exit_ts=ts)],
            parity_ok=True,
            start_equity_paisa=1_000_000,
            fingerprint="fp0",
        )
        entry = {
            "rank": 1,
            "run_id": run_id,
            "mask_hex": "0" * 32,
            "conditions": [],
            "instrument": "NSE-NIFTY",
            "tf": "5m",
            "direction": "long",
            "arm": {"sl_paisa": None, "tp_paisa": None},
            "metrics": {"max_consecutive_losses": 1},
            "regime": {},
            "vix_band": None,
            "family_id": "fam",
            "family_members": ["fam"],
            "reasons": None,
            "margin_roi": None,
            "equity_ref": {"tuple": "t", "sidecar": "equity"},
            "resilience": resilience_payload(live),
        }
        write_top100(
            sweep_run_dir(out_root, run_id).parent,
            Top100Artifact(
                schema_version=TOP100_SCHEMA_VERSION,
                run_id=run_id,
                preset="min_dd_first",
                source={},
                baselines={},
                entries=(entry,),
            ),
        )
        lines: list[str] = []
        cli._print_resilience_lines(
            out_root=out_root, grid=grid, report=_report(top100_written=True), printer=lines.append
        )
        assert len(lines) == 2  # header + one LIVE line, NO null footer
        assert not any("had NO resilience stats" in line for line in lines)
