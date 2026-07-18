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

from __future__ import annotations

from datetime import date
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
            "errored=0 captured=7 parity_failed=0 pool=6 gate_passers=4 "
            "baselines=1 top100=written entries=4"
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


class TestCascadeReportThreading:
    """DEC-RESULTS-CASCADE-VERDICT-JOIN-001: the in-memory report → join → pass."""

    def test_report_flattened_and_threaded_to_the_pass(self, tmp_path: Path) -> None:
        from brutex.engine.cascade_wire import CascadeWireReport
        from brutex.metrics.cascade import CandidateVerdict, CascadeReport

        inner = CascadeReport(
            verdicts=(
                CandidateVerdict(
                    candidate_id="a" * 64,
                    passed=True,
                    rejected_at=None,
                    detail="survived all Stage-4 filters",
                    composite_score=1.0,
                    cpcv_pass_rate=0.75,
                ),
            ),
            survivor_ids=("a" * 64,),
            n_input=1,
            n_survived=1,
            stage_survivors={},
        )
        report = CascadeWireReport(sweep_id="s" * 64, groups=(("NIFTY", "1minute", inner),))
        captured: dict[str, Any] = {}

        def _runner(**kwargs: Any) -> ResultsDepthReport:
            captured.update(kwargs)
            return _report()

        ok = _call(tmp_path, _runner, cascade_report=report)
        assert ok is True
        join = captured["cascade_join"]
        assert join is not None
        assert join.sweep_id == "s" * 64
        assert join.by_candidate["a" * 64].cpcv_pass_rate == 0.75
        assert join.by_candidate["a" * 64].survivor_rank == 1

    def test_none_report_threads_none(self, tmp_path: Path) -> None:
        captured: dict[str, Any] = {}

        def _runner(**kwargs: Any) -> ResultsDepthReport:
            captured.update(kwargs)
            return _report()

        assert _call(tmp_path, _runner) is True
        assert captured["cascade_join"] is None

    def test_join_error_belt_logs_and_never_fails_the_sweep(self, tmp_path: Path) -> None:
        # A structurally-unusable report (empty sweep_id) trips the
        # CascadeJoinError belt: WARN + None threaded, the pass still runs.
        from brutex.engine.cascade_wire import CascadeWireReport

        bad = CascadeWireReport(sweep_id="", groups=())
        captured: dict[str, Any] = {}

        def _runner(**kwargs: Any) -> ResultsDepthReport:
            captured.update(kwargs)
            return _report()

        with capture_logs() as logs:
            ok = _call(tmp_path, _runner, cascade_report=bad)
        assert ok is True
        assert captured["cascade_join"] is None
        (warn,) = [e for e in logs if e["event"] == "cli.results_depth.cascade_join_failed"]
        assert warn["error_type"] == "CascadeJoinError"


class TestExecSimProviderThreading:
    """`DEC-OPTIONS-EXEC-FEEDBACK-001`: the winners-hook in-memory per-run_id
    verdict maps become TOTAL provider closures — a known run_id serves its
    EXACT verdict object, an unknown one the NAMED not_priced/not_in_replay_set
    degrade; a ``None`` map threads NO provider (the entry key stays ABSENT,
    artifact bytes byte-identical)."""

    def _captured(self, tmp_path: Path, **overrides: Any) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        def _runner(**kwargs: Any) -> ResultsDepthReport:
            captured.update(kwargs)
            return _report()

        assert _call(tmp_path, _runner, **overrides) is True
        return captured

    def test_options_map_builds_a_total_provider_closure(self, tmp_path: Path) -> None:
        from brutex.options.exec_verdict import options_exec_not_priced

        verdict: dict[str, object] = {"status": "priced", "net_after_cost_paisa": 64_206}
        captured = self._captured(tmp_path, options_exec={"r-known": verdict})
        provider = captured["options_exec_provider"]
        assert provider is not None
        # The known run_id serves the EXACT verdict object (never a copy —
        # dict(options_exec) shallow-copies the map, values ride through).
        known = provider(SimpleNamespace(row=SimpleNamespace(run_id="r-known")))
        assert known is verdict
        # An unknown run_id reads the NAMED degrade, never a wrong join.
        unknown = provider(SimpleNamespace(row=SimpleNamespace(run_id="r-unknown")))
        assert unknown == options_exec_not_priced("not_in_replay_set")
        assert unknown == {"status": "not_priced", "reason": "not_in_replay_set"}
        # Only the WIRED map grows a provider.
        assert captured["futures_exec_provider"] is None

    def test_futures_map_builds_a_total_provider_closure(self, tmp_path: Path) -> None:
        from brutex.futures.leaderboard import futures_exec_not_priced

        verdict: dict[str, object] = {"status": "executed", "net_pnl_paisa": 749_625}
        captured = self._captured(tmp_path, futures_exec={"r-known": verdict})
        provider = captured["futures_exec_provider"]
        assert provider is not None
        known = provider(SimpleNamespace(row=SimpleNamespace(run_id="r-known")))
        assert known is verdict
        unknown = provider(SimpleNamespace(row=SimpleNamespace(run_id="r-gone")))
        assert unknown == futures_exec_not_priced("not_in_replay_set")
        assert unknown == {"status": "not_priced", "reason": "not_in_replay_set"}
        assert captured["options_exec_provider"] is None

    def test_none_maps_thread_no_providers(self, tmp_path: Path) -> None:
        """The byte-identical passthrough: a sim that never ran threads NO
        provider — build_top100 leaves the entry key ABSENT."""
        captured = self._captured(tmp_path)
        assert captured["options_exec_provider"] is None
        assert captured["futures_exec_provider"] is None

    def test_top100_preset_threads_the_resolved_token(self, tmp_path: Path) -> None:
        from brutex.engine.top100_artifact import DEFAULT_TOP100_PRESET

        default = self._captured(tmp_path)
        assert default["preset_name"] == DEFAULT_TOP100_PRESET
        explicit = self._captured(tmp_path, top100_preset="net_after_cost")
        assert explicit["preset_name"] == "net_after_cost"

    def test_fill_sensitivity_defaults_true_and_threads_false(self, tmp_path: Path) -> None:
        """DEC-FILL-EXEC-GRID-1M-001: the annotation half is production-ON by
        default; an explicit False threads through."""
        default = self._captured(tmp_path)
        assert default["fill_sensitivity"] is True
        off = self._captured(tmp_path, fill_sensitivity=False)
        assert off["fill_sensitivity"] is False


class TestResolveTop100Preset:
    """`DEC-OPTIONS-EXEC-FEEDBACK-001`: the eager fail-loud --top100-preset law."""

    @pytest.mark.parametrize("token", [None, "", "   "])
    def test_none_and_blank_read_the_default(self, token: str | None) -> None:
        from brutex.engine.top100_artifact import DEFAULT_TOP100_PRESET

        assert cli._resolve_top100_preset(token) == DEFAULT_TOP100_PRESET

    def test_known_token_passes_through_stripped(self) -> None:
        assert cli._resolve_top100_preset(" composite ") == "composite"

    def test_unknown_token_refuses_naming_every_valid_value(self) -> None:
        from brutex.engine.top100_artifact import TOP100_PRESETS

        with pytest.raises(cli.CliUserError) as excinfo:
            cli._resolve_top100_preset("typo")
        message = str(excinfo.value)
        assert "unknown --top100-preset 'typo'" in message
        for preset in TOP100_PRESETS:
            assert preset in message
