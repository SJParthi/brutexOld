"""Latency-check pins: measured numbers always emitted; budgets gate warm only.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1797). Timing rides an
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
        self.text = f"HTTP {status_code}"


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
            "first_call_ms",
            "warm_ms_best",
            "warm_ms_all",
            "budget_ms",
            "ratio_first_over_warm",
            "units_checked",
        }
        assert len(probe.evidence["warm_ms_all"]) == 3
        assert client.calls == len(latency.PROBES) * 4  # 1 first + 3 warm each
        # Round 1 MINOR-2 honesty relabel: the first call is never claimed
        # cold — earlier checks share the client and may have warmed caches.
        assert any("not guaranteed cold" in limit for limit in probe.limits)

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

    def test_non_200_route_error_not_no_dataset(self, tmp_path: Path) -> None:
        # Round-7 F1: a generic non-200 (404/400/500) is an honest ROUTE_ERROR
        # with the status + detail — NEVER a fabricated NO_DATASET (Golden Rule
        # #10). This is the class the wrong `/api/v1/universe` path hid behind.
        lake = make_twin(tmp_path)
        ctx = make_ctx(lake, client_factory=lambda: _Client(status_code=404))
        ctx.perf_timer = _ticking_timer(1.0)
        check = latency.run(ctx)
        for sub in check.sub_checks:
            assert sub.verdict is Verdict.ABSTAIN
            assert sub.abstain_reason is AbstainReason.ROUTE_ERROR
            assert sub.evidence["status_code"] == 404
            assert "route error" in sub.evidence["reason"]
        assert check.verdict is Verdict.ABSTAIN

    def test_503_abstains_no_instrument_store(self, tmp_path: Path) -> None:
        # A 503 is the degraded no-master shape — an honest standing gap, NOT a
        # route error and NOT a fabricated no_dataset (round-7 F1).
        lake = make_twin(tmp_path)
        ctx = make_ctx(lake, client_factory=lambda: _Client(status_code=503))
        ctx.perf_timer = _ticking_timer(1.0)
        check = latency.run(ctx)
        for sub in check.sub_checks:
            assert sub.verdict is Verdict.ABSTAIN
            assert sub.abstain_reason is AbstainReason.NO_INSTRUMENT_STORE
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
        # Round-7 F1: pin the CORRECTED universe route path — the pre-fix
        # `/api/v1/universe` 404'd on every lake (the real one is …/summary).
        by_key = {p[0]: p[1] for p in latency.PROBES}
        assert by_key["universe_summary"] == "/api/v1/universe/summary"


class TestLatencyRealAppRoutePins:
    """Round-7 F1 regression: drive the REAL dashboard app (build_app +
    TestClient) — never a fake client that answers any path — so a wrong probe
    route (a 404 the fake client never surfaced, the #1631 fixture-only
    blindspot) is caught. On a master-present HEALTHY lake EVERY latency-probed
    route must 200 and the whole latency check must PASS with zero abstains —
    proving no permanent false amber can arise from a hardcoded wrong path.
    """

    def test_every_probe_route_200_on_master_present_healthy_lake(self, tmp_path: Path) -> None:
        from tests.fakes.acceptance_twin import make_master_present_twin

        lake = make_master_present_twin(tmp_path)
        ctx = make_ctx(lake, latency_rounds=2)  # REAL build_app + TestClient
        try:
            client = ctx.client()
            for _key, path, params, _budget, _env in latency.PROBES:
                resp = client.get(path, params=params)
                assert resp.status_code == 200, (path, resp.status_code, resp.text[:200])
        finally:
            ctx.close()

    def test_latency_check_passes_no_false_amber_on_healthy_lake(self, tmp_path: Path) -> None:
        from tests.fakes.acceptance_twin import make_master_present_twin

        lake = make_master_present_twin(tmp_path)
        ctx = make_ctx(lake, latency_rounds=2)  # REAL build_app + TestClient
        try:
            check = latency.run(ctx)
            # Every probe PASSes — no ABSTAIN, so no non-structural streak, so
            # no permanent exit-4 amber on a HEALTHY master-present lake.
            assert check.verdict is Verdict.PASS, [
                (s.id, str(s.verdict), s.abstain_reason) for s in check.sub_checks
            ]
            for sub in check.sub_checks:
                assert sub.verdict is Verdict.PASS
                assert sub.abstain_reason is None
            universe = _sub(check, "latency.universe_summary")
            assert universe.verdict is Verdict.PASS
        finally:
            ctx.close()
