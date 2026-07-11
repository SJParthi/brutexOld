"""Check 7 — O(1)/latency spot-checks with MEASURED numbers (never a proof).

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756). In-process TestClient
+ ``perf_counter`` best-of-N-MIN (the house DEC-CANARY-MINTIME-001 idiom); one
recorded COLD call + N warm rounds per probe. Verdicts gate the WARM
best-of-N only; every number is emitted regardless. HONEST: on-box absolute
budgets — NOT the CI 1.1x strict tier and not an asymptotic proof; a breach
is a flag to investigate.
"""

from __future__ import annotations

from typing import Any, Final

from brutex.acceptance.context import AcceptanceContext
from brutex.acceptance.model import (
    AbstainReason,
    CheckResult,
    Verdict,
    make_check,
    make_coverage,
    make_subcheck,
)

CHECK_ID = "latency"

#: key → (route path, params, warm budget ms, envelope citation).
PROBES: Final[tuple[tuple[str, str, dict[str, Any], float, str], ...]] = (
    (
        "movers_hit",
        "/api/v1/movers",
        {"asset": "spot", "interval": "1day", "top_n": 5},
        500.0,
        "cached O(1) hit #860/#1034",
    ),
    (
        "coverage_windows",
        "/api/v1/coverage/windows",
        {},
        750.0,
        "O(page) #893/#1533",
    ),
    (
        "window_ledger_page",
        "/api/v1/db/window-ledger",
        {"limit": 100, "offset": 0},
        1000.0,
        "#1719 measured 564.8 ms cold",
    ),
    (
        "oneclick_runs",
        "/api/v1/oneclick/runs",
        {},
        250.0,
        "stat-token cache #1561/#1711",
    ),
    (
        "universe_summary",
        "/api/v1/universe",
        {},
        250.0,
        "content-token cache #866",
    ),
)


def _repro(ctx: AcceptanceContext) -> str:
    return f"brutex verify-acceptance --checks {CHECK_ID} --seed {ctx.seed}"


def run(ctx: AcceptanceContext) -> CheckResult:
    """Measure the probe table; FAIL only a warm best-of-N over its budget."""
    client = ctx.client()
    timer = ctx.perf_timer
    subs = []
    for key, path, params, default_budget, envelope in PROBES:
        budget = float(ctx.latency_budget_overrides.get(key, default_budget))
        t0 = timer()
        response = client.get(path, params=params)
        cold_ms = (timer() - t0) * 1000.0
        if response.status_code != 200:
            subs.append(
                make_subcheck(
                    f"{CHECK_ID}.{key}",
                    Verdict.ABSTAIN,
                    units_checked=0,
                    evidence={"status_code": response.status_code, "route": path},
                    abstain_reason=AbstainReason.NO_DATASET,
                    repro=_repro(ctx),
                )
            )
            continue
        warm: list[float] = []
        for _ in range(max(1, ctx.latency_rounds)):
            t0 = timer()
            client.get(path, params=params)
            warm.append((timer() - t0) * 1000.0)
        best = min(warm)
        limits = [
            f"on-box absolute budget ({envelope}) — not the CI 1.1x tier, "
            "not an asymptotic proof"
        ]
        if best >= cold_ms:
            limits.append("cache_not_faster: warm best did not beat the cold call")
        subs.append(
            make_subcheck(
                f"{CHECK_ID}.{key}",
                Verdict.FAIL if best > budget else Verdict.PASS,
                units_checked=len(warm) + 1,
                evidence={
                    "cold_ms": round(cold_ms, 3),
                    "warm_ms_best": round(best, 3),
                    "warm_ms_all": [round(w, 3) for w in warm],
                    "budget_ms": budget,
                    "ratio_cold_over_warm": round(cold_ms / best, 3) if best > 0 else None,
                },
                limits=limits,
                repro=_repro(ctx),
            )
        )
    coverage = make_coverage(
        mode="full",
        population=len(PROBES),
        examined=len(PROBES),
        seed=ctx.seed,
        selection=[p[0] for p in PROBES],
    )
    return make_check(CHECK_ID, subs, coverage=coverage, repro=_repro(ctx))
