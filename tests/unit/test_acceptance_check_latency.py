"""Latency-check pins: measured numbers always emitted; budgets gate warm only.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756). Timing rides an
injected fake clock — no CI wall-clock flake.
"""

from __future__ import annotations

import itertools
from pathlib import Path

from brutex.acceptance.checks import latency
from brutex.acceptance.model import AbstainReason, Verdict

from tests.fakes.acceptance_twin import make_ctx, make_twin


class _Response:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code


class _Client:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.calls = 0

    def get(self, path: str, params: dict | None = None) -> _Response:
        self.calls += 1
        return _Response(self.status_code)


def _ticking_timer(step_ms: float):
    counter = itertools.count()
    return lambda: next(counter) * step_ms / 1000.0


def _sub(check, sub_id: str):
    return next(s for s in check.sub_checks if s.id == sub_id)


class TestLatencyCheck:
    def test_generous_budgets_pass_with_all_evidence_keys(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        client = _Client()
        ctx = make_ctx(lake, client_factory=lambda: client, latency_rounds=3)
        ctx.perf_timer = _ticking_timer(1.0)  # every call measures 1 ms
        check = latency.run(ctx)
        assert check.verdict is Verdict.PASS
        probe = _sub(check, "latency.movers_hit")
        assert set(probe.evidence) >= {
            "cold_ms",
            "warm_ms_best",
            "warm_ms_all",
            "budget_ms",
            "ratio_cold_over_warm",
            "units_checked",
        }
        assert len(probe.evidence["warm_ms_all"]) == 3
        assert client.calls == len(latency.PROBES) * 4  # 1 cold + 3 warm each

    def test_budget_zero_fail_tooth(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        ctx = make_ctx(
            lake,
            client_factory=_Client,
            latency_rounds=1,
            latency_budget_overrides={"movers_hit": 0.0},
        )
        ctx.perf_timer = _ticking_timer(5.0)
        check = latency.run(ctx)
        probe = _sub(check, "latency.movers_hit")
        assert probe.verdict is Verdict.FAIL
        others = _sub(check, "latency.coverage_windows")
        assert others.verdict is Verdict.PASS  # only the overridden probe gates

    def test_non_200_abstains_no_dataset(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        ctx = make_ctx(lake, client_factory=lambda: _Client(status_code=404))
        ctx.perf_timer = _ticking_timer(1.0)
        check = latency.run(ctx)
        for sub in check.sub_checks:
            assert sub.verdict is Verdict.ABSTAIN
            assert sub.abstain_reason is AbstainReason.NO_DATASET
        assert check.verdict is Verdict.ABSTAIN

    def test_cache_not_faster_warn_row(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)

        # cold reads 1 ms; warm rounds read 3 ms each → warm best >= cold.
        times = iter([0.0, 0.001, 0.010, 0.013, 0.020, 0.023])

        class _OneProbe:
            def get(self, path: str, params: dict | None = None) -> _Response:
                return _Response(200 if path == "/api/v1/movers" else 404)

        ctx = make_ctx(lake, client_factory=_OneProbe, latency_rounds=2)
        pool = itertools.chain(times, itertools.count(1.0))
        ctx.perf_timer = lambda: next(pool)
        check = latency.run(ctx)
        probe = _sub(check, "latency.movers_hit")
        assert any("cache_not_faster" in limit for limit in probe.limits)

    def test_rounds_clamped_to_one(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        client = _Client()
        ctx = make_ctx(lake, client_factory=lambda: client, latency_rounds=0)
        ctx.perf_timer = _ticking_timer(1.0)
        check = latency.run(ctx)
        probe = _sub(check, "latency.movers_hit")
        assert len(probe.evidence["warm_ms_all"]) == 1

    def test_probe_table_shape(self) -> None:
        keys = [p[0] for p in latency.PROBES]
        assert keys == [
            "movers_hit",
            "coverage_windows",
            "window_ledger_page",
            "oneclick_runs",
            "universe_summary",
        ]
        for _key, path, _params, budget, envelope in latency.PROBES:
            assert path.startswith("/api/v1/")
            assert budget > 0
            assert envelope  # every budget cites its documented envelope
