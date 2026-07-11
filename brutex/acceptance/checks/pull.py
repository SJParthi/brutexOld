"""Check 3 — incremental-pull idempotence, ZERO vendor calls (structural re-plan).

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756). Re-plans the ledger's
own covered spans on a COPY with a pinned clock via the SAME planners the pull
uses (:func:`~brutex.data.window_ledger_store.plan_windows` /
:func:`~brutex.adapters.groww.option_ingest.plan_contract_windows` — both
idempotent over settled rows, clamp-paritous keys). Explicitly NOT
``brutex fetch --dry-run`` (a synthetic adapter into an isolated temp lake —
proves nothing about THIS lake). Plus the keep-first counter laws and the
silent-loss detector (a SUCCESS row whose backing month files are ALL gone).
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from brutex.acceptance.context import AcceptanceContext
from brutex.acceptance.model import (
    AbstainReason,
    CheckResult,
    Verdict,
    make_check,
    make_coverage,
    make_subcheck,
)

CHECK_ID = "pull"

#: SUCCESS rows sampled for the partition-presence stat probe per run.
PRESENCE_SAMPLE_N = 400

#: Evidence key samples are capped at this many entries.
KEY_CAP = 20


def _repro(ctx: AcceptanceContext) -> str:
    return f"brutex verify-acceptance --checks {CHECK_ID} --seed {ctx.seed}"


def _abstain_check(ctx: AcceptanceContext, reason: AbstainReason) -> CheckResult:
    sub = make_subcheck(
        "pull.replan_zero",
        Verdict.ABSTAIN,
        units_checked=0,
        evidence={"reason": str(reason)},
        abstain_reason=reason,
        repro=_repro(ctx),
    )
    coverage = make_coverage(mode="none", population=0, examined=0, seed=ctx.seed)
    return make_check(CHECK_ID, [sub], coverage=coverage, repro=_repro(ctx))


def _row_key(rec: Any) -> tuple[str, str, str, str, str]:
    return (rec.market, rec.security_id, rec.interval, rec.window_start, rec.window_end)


def _replan_series(
    ledger_copy: Any,
    *,
    market: str,
    security_id: str,
    interval: str,
    start: date,
    end: date,
    now: datetime,
) -> None:
    """Re-plan ONE (market, series, interval) span through the real planners."""
    from brutex.data.fno_symbol import parse_fno_symbol
    from brutex.data.window_ledger_store import plan_windows

    if market.endswith(("-option", "-future")):
        from brutex.adapters.groww.option_ingest import plan_contract_windows
        from brutex.adapters.protocol import DerivativeContract

        parts = parse_fno_symbol(security_id)
        if parts is None:
            return
        contract = DerivativeContract(
            underlying_symbol=parts.underlying,
            expiry=parts.expiry,
            symbol=security_id,
            contract_type=parts.instrument_type,
            strike=None if parts.strike is None else float(parts.strike),
        )
        plan_contract_windows(
            ledger_copy,
            contracts=[contract],
            intervals=[interval],
            start=start,
            end=end,
            market=market,
            now_ist=lambda: now,
        )
        return
    from brutex.adapters.groww.chunks import window_days_for_interval

    plan_windows(
        ledger_copy,
        instruments=[SimpleNamespace(symbol_name=security_id)],
        intervals=[interval],
        start=start,
        end=end,
        now=now,
        now_ist=now,
        market=market,
        window_days_for=window_days_for_interval,
    )


def _spot_symbol_dir(lake_root: Path, symbol: str) -> Path | None:
    """Locate a spot symbol's dataset dir (the ledger key carries no exch/seg)."""
    bars = lake_root / "bars"
    if not bars.is_dir():
        return None
    for exch in sorted(p for p in bars.iterdir() if p.is_dir()):
        for seg in sorted(p for p in exch.iterdir() if p.is_dir()):
            if seg.name == "FNO":
                continue
            candidate = seg / symbol
            if candidate.is_dir():
                return candidate
    return None


def _series_dir(lake_root: Path, market: str, security_id: str) -> Path | None:
    if market.endswith(("-option", "-future")):
        exchange = security_id.split("-", 1)[0]
        candidate = lake_root / "bars" / exchange / "FNO" / security_id
        return candidate if candidate.is_dir() else None
    return _spot_symbol_dir(lake_root, security_id)


def run(ctx: AcceptanceContext) -> CheckResult:  # noqa: PLR0915 — one linear evidence pipeline
    """Run the four pull sub-checks over the ONE shared ledger load."""
    from brutex.calendars.nse import trading_days_between
    from brutex.data.spot_daily_resample import ist_span_partition_files
    from brutex.data.window_ledger import SETTLED_STATUSES, WindowLedger
    from brutex.observability.process_probe import read_rss_kib

    if ctx.pull_active() and not ctx.force_during_pull:
        return _abstain_check(ctx, AbstainReason.PULL_IN_PROGRESS)
    rss_before = read_rss_kib()
    ledger = ctx.ledger()
    if ledger is None:
        return _abstain_check(ctx, AbstainReason.NO_LEDGER)
    rss_after = read_rss_kib()
    records = ledger.records()
    now = ctx.now_utc()

    # ── pull.replan_zero — the idempotence oracle ──────────────────────
    copy = WindowLedger.from_rows(ledger.to_rows())
    before_keys = {_row_key(r) for r in records}
    settled_by_series: dict[tuple[str, str, str], tuple[date, date]] = {}
    for rec in records:
        series = (rec.market, rec.security_id, rec.interval)
        ws = date.fromisoformat(rec.window_start)
        we = date.fromisoformat(rec.window_end)
        span = settled_by_series.get(series)
        settled_by_series[series] = (
            (ws, we) if span is None else (min(span[0], ws), max(span[1], we))
        )
    for (market, security_id, interval), (start, end) in sorted(settled_by_series.items()):
        _replan_series(
            copy,
            market=market,
            security_id=security_id,
            interval=interval,
            start=start,
            end=end,
            now=now,
        )
    after = {_row_key(r): r for r in copy.records()}
    new_keys = sorted(set(after) - before_keys)
    max_end = max((date.fromisoformat(r.window_end) for r in records), default=None)
    newly_fetchable = [k for k in new_keys if date.fromisoformat(k[4]) <= (max_end or date.min)]
    open_by_design = sum(
        1 for r in records if r.status not in SETTLED_STATUSES
    ) + (len(new_keys) - len(newly_fetchable))
    # Anti-vacuous tooth: a trading session newer than the max settled end MUST
    # be plannable — a planner lobotomized to return [] FAILs here.
    tail_check = "not_applicable"
    if max_end is not None:
        newer_sessions = trading_days_between(max_end + timedelta(days=1), ctx.run_date())
        if newer_sessions:
            tail_copy = WindowLedger.from_rows(ledger.to_rows())
            tail_planned_before = {_row_key(r) for r in tail_copy.records()}
            for (market, security_id, interval), (start, _end) in sorted(
                settled_by_series.items()
            ):
                _replan_series(
                    tail_copy,
                    market=market,
                    security_id=security_id,
                    interval=interval,
                    start=start,
                    end=ctx.run_date(),
                    now=now,
                )
            tail_new = {_row_key(r) for r in tail_copy.records()} - tail_planned_before
            tail_check = "planned" if tail_new else "planner_vacuous"
    replan_verdict = (
        Verdict.FAIL if newly_fetchable or tail_check == "planner_vacuous" else Verdict.PASS
    )
    replan_sub = make_subcheck(
        "pull.replan_zero",
        replan_verdict,
        units_checked=len(records),
        evidence={
            "newly_fetchable": len(newly_fetchable),
            "newly_fetchable_keys": ["|".join(k) for k in newly_fetchable[:KEY_CAP]],
            "open_by_design": open_by_design,
            "tail_check": tail_check,
            "series_replanned": len(settled_by_series),
            "ledger_rss_kib_before": rss_before,
            "ledger_rss_kib_after": rss_after,
        },
        limits=["plan-level convergence only — not vendor-side determinism"],
        repro=_repro(ctx),
    )

    # ── pull.net_new_law — keep-first + counter-split invariants ───────
    violations: list[str] = []
    counted = 0
    unknown = 0
    for rec in records:
        if rec.bars_net_new is None and rec.empty_retries is None:
            unknown += 1
            continue
        counted += 1
        if rec.bars_net_new is not None and rec.bars_net_new > rec.bars:
            violations.append(f"{'|'.join(_row_key(rec))}: bars_net_new > bars")
        if rec.empty_retries is not None and rec.transient_retries is not None:
            expected = rec.empty_retries + rec.transient_retries + (1 if rec.bars else 0)
            if expected != rec.in_pull_fetches:
                violations.append(f"{'|'.join(_row_key(rec))}: counter invariant broken")
    net_new_sub = make_subcheck(
        "pull.net_new_law",
        Verdict.FAIL if violations else Verdict.PASS,
        units_checked=counted,
        evidence={
            "rows_with_counters": counted,
            "rows_honest_unknown": unknown,
            "violations": violations[:KEY_CAP],
            "violations_total": len(violations),
        },
        limits=["None counters are honest-unknown — counted, never failed"],
        repro=_repro(ctx),
    )

    # ── pull.partition_presence — the silent-loss detector ─────────────
    success_rows = [r for r in records if r.status == "SUCCESS" and r.bars > 0]
    ranked = sorted(
        success_rows,
        key=lambda r: hashlib.sha256((ctx.seed + "|".join(_row_key(r))).encode()).hexdigest(),
    )[:PRESENCE_SAMPLE_N]
    missing: list[str] = []
    probed = 0
    for rec in ranked:
        series_dir = _series_dir(ctx.lake_root, rec.market, rec.security_id)
        if series_dir is None:
            missing.append(f"{'|'.join(_row_key(rec))}: whole series dir missing")
            probed += 1
            continue
        base = series_dir / rec.interval
        ws = date.fromisoformat(rec.window_start)
        we = date.fromisoformat(rec.window_end)
        candidates = ist_span_partition_files(base, ws, max(ws, we - timedelta(days=1)))
        probed += 1
        if not any(Path(c).is_file() for c in candidates):
            missing.append(f"{'|'.join(_row_key(rec))}: expected {candidates[:2]}")
    presence_sub = make_subcheck(
        "pull.partition_presence",
        Verdict.FAIL if missing else Verdict.PASS,
        units_checked=probed,
        evidence={
            "sampled": probed,
            "success_rows_total": len(success_rows),
            "missing": len(missing),
            "missing_keys": missing[:KEY_CAP],
        },
        limits=["a FAIL is definitive; a PASS is sample-scoped"],
        repro=_repro(ctx),
    )

    # ── pull.progress_snapshot — trending evidence ─────────────────────
    progress = ledger.progress_counts()
    progress_sub = make_subcheck(
        "pull.progress_snapshot",
        Verdict.PASS,
        units_checked=progress.total,
        evidence={
            "total": progress.total,
            "settled": progress.settled,
            "resolved": progress.resolved,
            "failed": progress.failed,
            "planned": progress.planned,
            "bars": progress.bars,
        },
        repro=_repro(ctx),
    )

    coverage = make_coverage(
        mode="full",
        population=len(records),
        examined=len(records),
        seed=ctx.seed,
        selection=[f"ledger rows={len(records)}"],
    )
    return make_check(
        CHECK_ID,
        [replan_sub, net_new_sub, presence_sub, progress_sub],
        coverage=coverage,
        repro=_repro(ctx),
        limits=[f"partition presence sampled {PRESENCE_SAMPLE_N} of {len(success_rows)} SUCCESS rows"],
    )
