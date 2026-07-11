"""Check 3 — incremental-pull idempotence, ZERO vendor calls (structural re-plan).

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756). Re-plans the ledger's
own covered spans on a COPY with a pinned clock via the SAME planners the pull
uses (:func:`~brutex.data.window_ledger_store.plan_windows` /
:func:`~brutex.adapters.groww.option_ingest.plan_contract_windows` — both
idempotent over settled rows, clamp-paritous keys). Review round 1 folds:
each series is re-planned over its MERGED COVERED spans (two disjoint
operator ranges — DEC-UNIVERSE-PULL-DATERANGE-001 — never false-FAIL the
by-design gap between them), and the F&O re-plan threads the SAME
expiry-capped ``contract_windows`` production derives
(:func:`~brutex.adapters.groww.contract_bounds.default_fno_minute_window`),
so the oracle's planning keys match production's. Explicitly NOT
``brutex fetch --dry-run`` (a synthetic adapter into an isolated temp lake —
proves nothing about THIS lake). Plus the keep-first counter laws and the
silent-loss detector (a SUCCESS row whose backing month files are ALL gone).
"""

from __future__ import annotations

import contextlib
import hashlib
from datetime import date, datetime, timedelta
from datetime import time as _dt_time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from brutex.acceptance.checks import run_fingerprinted_check
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

#: The anti-vacuous tail replan pins this post-close IST wall time.
_POST_CLOSE_IST = _dt_time(16, 0)


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


def merge_covered_spans(spans: list[tuple[date, date]]) -> list[tuple[date, date]]:
    """Merge overlapping/touching ``[start, end)`` windows into covered spans.

    Two DISJOINT operator ranges (DEC-UNIVERSE-PULL-DATERANGE-001) stay two
    spans — the never-requested gap between them is BY DESIGN unfetched and
    must never be re-planned into a false ``newly_fetchable`` FAIL (review
    round 1: the min→max hull planned the gap).
    """
    merged: list[tuple[date, date]] = []
    for ws, we in sorted(spans):
        if merged and ws <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], we))
        else:
            merged.append((ws, we))
    return merged


def _fno_window(
    security_id: str, *, start: date, end: date
) -> tuple[Any, tuple[date, date]] | None:
    """The production-parity ``(contract, bounded window)`` for one F&O series.

    ``None`` = production would plan NOTHING for this series (unparsable
    symbol, or the DEC-FNO-FAR-EXPIRY-WINDOW-001 not-yet-open skip). The
    window is the SAME :func:`default_fno_minute_window` map production
    threads into ``plan_contract_windows`` — the expiry cap the round-1
    review found the replan oracle dropped (``contract_windows=None``).
    """
    from brutex.adapters.groww.contract_bounds import default_fno_minute_window
    from brutex.adapters.protocol import DerivativeContract
    from brutex.data.fno_symbol import parse_fno_symbol

    parts = parse_fno_symbol(security_id)
    if parts is None:
        return None
    window = default_fno_minute_window(
        parts.expiry,
        full_start=start,
        full_end=end,
        is_option=parts.instrument_type in {"CE", "PE"},
    )
    if window is None:
        return None
    contract = DerivativeContract(
        underlying_symbol=parts.underlying,
        expiry=parts.expiry,
        symbol=security_id,
        contract_type=parts.instrument_type,
        strike=None if parts.strike is None else float(parts.strike),
    )
    return contract, window


def _replan_series(
    ledger_copy: Any,
    *,
    market: str,
    security_id: str,
    interval: str,
    start: date,
    end: date,
    now: datetime,
    now_ist: datetime,
) -> None:
    """Re-plan ONE (market, series, interval) span through the real planners."""
    from brutex.data.window_ledger_store import plan_windows

    if market.endswith(("-option", "-future")):
        from brutex.adapters.groww.option_ingest import plan_contract_windows

        resolved = _fno_window(security_id, start=start, end=end)
        if resolved is None:
            return
        contract, window = resolved
        plan_contract_windows(
            ledger_copy,
            contracts=[contract],
            intervals=[interval],
            start=start,
            end=end,
            market=market,
            contract_windows={security_id: window},
            now_ist=lambda: now_ist,
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
        now_ist=now_ist,
        market=market,
        window_days_for=window_days_for_interval,
    )


def _tail_cap(market: str, security_id: str, *, start: date, run_end: date) -> date | None:
    """The furthest window end production could plan for this series at the tail.

    Spot: the pinned post-close ``run_date + 1``. F&O: capped at
    ``min(run_end, expiry + 1)`` via the production window law; ``None`` when
    production would plan nothing at all (unparsable / not-yet-open).
    """
    if not market.endswith(("-option", "-future")):
        return run_end
    resolved = _fno_window(security_id, start=start, end=run_end)
    if resolved is None:
        return None
    return resolved[1][1]


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


def _ledger_paths(ctx: AcceptanceContext) -> list[Path]:
    """The check's input files: the base ledger parquet + its pending deltas."""
    from brutex.data.window_ledger_store import ledger_path, list_ledger_deltas

    paths = [ledger_path(ctx.lake_root)]
    # a raced-away deltas dir is not a verdict
    with contextlib.suppress(OSError):
        paths.extend(list_ledger_deltas(ctx.lake_root))
    return paths


def run(ctx: AcceptanceContext) -> CheckResult:
    """Run the pull sub-checks over the ONE shared ledger load."""
    from brutex.observability.process_probe import read_rss_kib

    if ctx.pull_active() and not ctx.force_during_pull:
        return _abstain_check(ctx, AbstainReason.PULL_IN_PROGRESS)
    rss_before = read_rss_kib()
    if ctx.ledger() is None:
        return _abstain_check(ctx, AbstainReason.NO_LEDGER)
    rss_after = read_rss_kib()
    return run_fingerprinted_check(
        ctx,
        check_id=CHECK_ID,
        sub_id="pull.replan_zero",
        paths_fn=lambda: _ledger_paths(ctx),
        body=lambda: _run_body(ctx, rss=(rss_before, rss_after)),
    )


def _run_body(ctx: AcceptanceContext, *, rss: tuple[int | None, int | None]) -> CheckResult:
    from brutex.calendars.nse import trading_days_between
    from brutex.data.spot_daily_resample import ist_span_partition_files
    from brutex.data.window_ledger import SETTLED_STATUSES, WindowLedger

    rss_before, rss_after = rss
    ledger = ctx.ledger()
    records = ledger.records()
    now = ctx.now_utc()

    # ── pull.replan_zero — the idempotence oracle ──────────────────────
    copy = WindowLedger.from_rows(ledger.to_rows())
    before_keys = {_row_key(r) for r in records}
    windows_by_series: dict[tuple[str, str, str], list[tuple[date, date]]] = {}
    for rec in records:
        series = (rec.market, rec.security_id, rec.interval)
        windows_by_series.setdefault(series, []).append(
            (date.fromisoformat(rec.window_start), date.fromisoformat(rec.window_end))
        )
    spans_by_series = {
        series: merge_covered_spans(spans) for series, spans in windows_by_series.items()
    }
    spans_replanned = 0
    for (market, security_id, interval), spans in sorted(spans_by_series.items()):
        for start, end in spans:
            spans_replanned += 1
            _replan_series(
                copy,
                market=market,
                security_id=security_id,
                interval=interval,
                start=start,
                end=end,
                now=now,
                now_ist=ctx.now_ist(),
            )
    after_main = {_row_key(r) for r in copy.records()}
    new_keys = sorted(after_main - before_keys)
    max_end = max((date.fromisoformat(r.window_end) for r in records), default=None)
    newly_fetchable = [k for k in new_keys if date.fromisoformat(k[4]) <= (max_end or date.min)]
    open_by_design = sum(1 for r in records if r.status not in SETTLED_STATUSES) + (
        len(new_keys) - len(newly_fetchable)
    )
    # Anti-vacuous tooth: a trading session newer than the max settled end MUST
    # be plannable by SOME still-plannable series — a planner lobotomized to
    # return [] FAILs here. Review round 1 folds: the tail reuses the SAME
    # replanned copy (no second full ledger copy), probes eligible series one
    # at a time (one usually suffices), and an EXPIRED contract — whose
    # production window cap ends at expiry — is honestly ineligible, so an
    # all-expired lake reads no_tail_eligible_series, never planner_vacuous.
    tail_check = "not_applicable"
    tail_probed = 0
    eligible_total = 0
    if max_end is not None and trading_days_between(max_end, ctx.run_date()):
        run_end = ctx.run_date() + timedelta(days=1)
        post_close_ist = datetime.combine(ctx.run_date(), _POST_CLOSE_IST)
        eligible: list[tuple[tuple[str, str, str], date]] = []
        for (market, security_id, interval), spans in sorted(
            spans_by_series.items(),
            key=lambda item: (item[0][0].endswith(("-option", "-future")), item[0]),
        ):
            covered_end = max(end for _start, end in spans)
            cap = _tail_cap(market, security_id, start=spans[0][0], run_end=run_end)
            if cap is not None and cap > covered_end:
                eligible.append(((market, security_id, interval), spans[0][0]))
        eligible_total = len(eligible)
        if not eligible:
            tail_check = "no_tail_eligible_series"
        else:
            tail_check = "planner_vacuous"
            for (market, security_id, interval), start in eligible:
                tail_probed += 1
                _replan_series(
                    copy,
                    market=market,
                    security_id=security_id,
                    interval=interval,
                    start=start,
                    end=run_end,
                    now=now,
                    now_ist=post_close_ist,
                )
                if {_row_key(r) for r in copy.records()} - after_main:
                    tail_check = "planned"
                    break
    replan_verdict = (
        Verdict.FAIL if newly_fetchable or tail_check == "planner_vacuous" else Verdict.PASS
    )
    progress = ledger.progress_counts()
    replan_sub = make_subcheck(
        "pull.replan_zero",
        replan_verdict,
        units_checked=len(records),
        evidence={
            "newly_fetchable": len(newly_fetchable),
            "newly_fetchable_keys": ["|".join(k) for k in newly_fetchable[:KEY_CAP]],
            "open_by_design": open_by_design,
            "tail_check": tail_check,
            "tail_eligible_series": eligible_total,
            "tail_series_probed": tail_probed,
            "series_replanned": len(spans_by_series),
            "spans_replanned": spans_replanned,
            # Ledger progress counters: trending EVIDENCE only (the former
            # decorative always-PASS progress_snapshot sub-check is deleted).
            "ledger_progress": {
                "total": progress.total,
                "settled": progress.settled,
                "resolved": progress.resolved,
                "failed": progress.failed,
                "planned": progress.planned,
                "bars": progress.bars,
            },
            "ledger_rss_kib_before": rss_before,
            "ledger_rss_kib_after": rss_after,
        },
        limits=[
            "plan-level convergence only — not vendor-side determinism",
            "re-plan runs per MERGED covered span: a deleted ledger row that "
            "splits a span is invisible here (partition presence + census own "
            "file-level loss)",
        ],
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

    coverage = make_coverage(
        mode="full",
        population=len(records),
        examined=len(records),
        seed=ctx.seed,
        selection=[f"ledger rows={len(records)}"],
    )
    return make_check(
        CHECK_ID,
        [replan_sub, net_new_sub, presence_sub],
        coverage=coverage,
        repro=_repro(ctx),
        limits=[
            f"partition presence sampled {PRESENCE_SAMPLE_N} of {len(success_rows)} SUCCESS rows"
        ],
    )
