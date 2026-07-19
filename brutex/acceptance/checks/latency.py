"""Check 7 — O(1)/latency spot-checks with MEASURED numbers (never a proof).

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1958). In-process TestClient
+ ``perf_counter`` best-of-N-MIN (the house DEC-CANARY-MINTIME-001 idiom); one
recorded FIRST call + N warm rounds per probe. Verdicts gate the WARM
best-of-N only; every number is emitted regardless. HONEST: on-box absolute
budgets — NOT the CI 1.1x strict tier and not an asymptotic proof; a breach
is a flag to investigate. The first-call number is NOT claimed cold — earlier
checks share the client and may have warmed the caches (review round 1).
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

#: HTTP success status the probes require before timing warm rounds.
HTTP_OK = 200

#: A 503 from a store-backed probe means the acceptance client has no readable
#: master (it opens ``instruments.duckdb`` READ-ONLY and degrades to no-store
#: when it is locked/absent, review round 1 M1) — the route then 503s. ANY
#: OTHER non-200 (a 400/404/500) is a genuine route/data error and MUST NOT be
#: relabelled "no dataset" (Golden Rule #10, round-7 F1 — the same class as the
#: round-6 F2 movers-non-200 mislabel, fixed for movers but missed here).
MASTER_ABSENT_STATUS = 503

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
        "/api/v1/universe/summary",
        {},
        250.0,
        "content-token cache #866",
    ),
)


def _repro(ctx: AcceptanceContext) -> str:
    """Return the ``brutex verify-acceptance`` reproduce command for this check.

    Returns
    -------
    str
        The CLI invocation that re-runs this check with the run seed.
    """
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
        first_call_ms = (timer() - t0) * 1000.0
        if response.status_code != HTTP_OK:
            # HONEST split (Golden Rule #10, round-7 F1): a 503 is the
            # degraded no-master shape (an honest standing gap on a
            # master-absent lake); ANY OTHER non-200 (404/400/500) is a genuine
            # route/data error — NEVER a fabricated "no dataset". A
            # master-PRESENT healthy lake 200s every probe, so this branch never
            # fires there and can never drive a false permanent amber.
            status = response.status_code
            if status == MASTER_ABSENT_STATUS:
                reason = (
                    f"{CHECK_ID}.{key} route 503 — instrument master absent or "
                    "held read-write by another process"
                )
                abstain_reason = AbstainReason.NO_INSTRUMENT_STORE
            else:
                reason = f"{CHECK_ID}.{key} route error (HTTP {status})"
                abstain_reason = AbstainReason.ROUTE_ERROR
            subs.append(
                make_subcheck(
                    f"{CHECK_ID}.{key}",
                    Verdict.ABSTAIN,
                    units_checked=0,
                    evidence={
                        "status_code": status,
                        "route": path,
                        "reason": reason,
                        "detail": str(response.text)[:200],
                    },
                    abstain_reason=abstain_reason,
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
            f"on-box absolute budget ({envelope}) — not the CI 1.1x tier, not an asymptotic proof",
            # Honest relabel (review round 1 MINOR-2): earlier checks share this
            # client, so the first call here may already be cache-warm — it is a
            # FIRST-CALL number, never claimed cold; only the warm best gates.
            "first_call_ms is not guaranteed cold (earlier checks may have "
            "warmed shared caches) — warm best-of-N is the only gated number",
        ]
        if best >= first_call_ms:
            limits.append("cache_not_faster: warm best did not beat the first call")
        subs.append(
            make_subcheck(
                f"{CHECK_ID}.{key}",
                Verdict.FAIL if best > budget else Verdict.PASS,
                units_checked=len(warm) + 1,
                evidence={
                    "first_call_ms": round(first_call_ms, 3),
                    "warm_ms_best": round(best, 3),
                    "warm_ms_all": [round(w, 3) for w in warm],
                    "budget_ms": budget,
                    "ratio_first_over_warm": round(first_call_ms / best, 3) if best > 0 else None,
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
    return make_check(
        CHECK_ID,
        subs,
        coverage=coverage,
        repro=_repro(ctx),
        limits=[
            # Round-3 disclosure (B-NEW-2): the latency probes drive the same
            # in-process serving path the movers check does.
            "the in-process serving path may refresh <lake>/.manifest/ "
            "discovery caches exactly as the production dashboard does (a "
            "benign self-healing rebuild, results-safe — a disclosed "
            "residual beside the registry.duckdb open)",
        ],
    )
