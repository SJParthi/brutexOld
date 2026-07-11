"""Check 2 — derived-timeframe correctness (19 intraday TFs composed + 1day NEW).

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756). Intraday freshness rides
the SAME seam the sweep preflight G1 gate + doctor consume
(:func:`~brutex.data.tf_autoderive.lake_derived_freshness_verdicts` — exact
OHLCV value equality after keep-first dedup, no tolerance). The 1day compare
is the NEW composition (inventory GAP 1): re-derive via
:func:`~brutex.data.spot_daily_resample.resample_1m_to_daily` under the real
cutoff law and value-compare vs the stored ``1day`` partition; a stored bar
for a session the cutoff law says cannot exist yet FAILs
(``forming_day_stored``). Derived-F&O ``open_interest`` is NOT value-compared
in v1 — ``tf.oi`` is a standing structural ABSTAIN until follow-up F2.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from brutex.acceptance.checks import run_fingerprinted_check
from brutex.acceptance.context import IST_OFFSET, AcceptanceContext
from brutex.acceptance.model import (
    AbstainReason,
    CheckResult,
    Verdict,
    make_check,
    make_coverage,
    make_subcheck,
)
from brutex.acceptance.sampling import (
    read_bars_frame_bounded,
    recent_months,
    sample_datasets,
    sample_fno_contracts,
)

CHECK_ID = "tf"

#: (symbol, tf) FAIL samples are capped at this many entries.
PAIR_CAP = 50

#: Sampled F&O contract dirs fed through the freshness seam per run.
FNO_SAMPLE_N = 6


def _repro(ctx: AcceptanceContext) -> str:
    return f"brutex verify-acceptance --checks {CHECK_ID} --seed {ctx.seed}"


def _abstain_check(ctx: AcceptanceContext, reason: AbstainReason) -> CheckResult:
    sub = make_subcheck(
        "tf.intraday",
        Verdict.ABSTAIN,
        units_checked=0,
        evidence={"reason": str(reason)},
        abstain_reason=reason,
        repro=_repro(ctx),
    )
    coverage = make_coverage(mode="none", population=0, examined=0, seed=ctx.seed)
    return make_check(
        CHECK_ID, [sub, _oi_standing_abstain(ctx)], coverage=coverage, repro=_repro(ctx)
    )


def _oi_standing_abstain(ctx: AcceptanceContext) -> Any:
    """The permanent ``tf.oi`` structural abstain — visible every run until F2."""
    return make_subcheck(
        "tf.oi",
        Verdict.ABSTAIN,
        units_checked=0,
        evidence={
            "law": "compare is OHLCV-6 (tf_autoderive.REQUIRED_COLUMNS) — derived-F&O "
            "open_interest is NOT value-compared in v1",
            "follow_up": "F2 (OI-aware derived-TF compare)",
        },
        abstain_reason=AbstainReason.NO_OI_COMPARE_SEAM,
        repro=_repro(ctx),
    )


def _has_interval_files(symbol_dir: Any, interval: str) -> bool:
    root = symbol_dir / interval
    return root.is_dir() and any(root.rglob("*.parquet"))


def _daily_month_cells(months: set[tuple[int, int]] | None) -> set[tuple[int, int]] | None:
    """The stored-1day file cells for a months bound: each cell + its predecessor.

    A midnight-IST daily bar for day 1 of month M is 18:30 UTC on the LAST day
    of month M−1, so its partition file lives in the PREVIOUS UTC month cell —
    the stored side pads one month back and the rows are re-filtered by IST
    date afterwards (never a false month-boundary divergence).
    """
    if months is None:
        return None
    padded = set(months)
    for year, month in months:
        padded.add((year, month - 1) if month > 1 else (year - 1, 12))
    return padded


def _daily_compare_for_symbol(
    ctx: AcceptanceContext,
    symbol_dir: Any,
    *,
    key: str,
    months: set[tuple[int, int]] | None = None,
) -> dict[str, Any]:
    """One symbol's 1day verdict cell: equal / divergent / forming / skip reason.

    Muhurat handling is PER-DAY (review round 1 MAJOR-2): a Muhurat session's
    daily bar is skipped on BOTH sides and counted as ONE unverified DAY
    (``muhurat_days_skipped`` = distinct IST dates, round 2 NIT: no more
    per-side double count) — the rest of the history still gets a real
    verdict. HONEST CONSEQUENCE (round 2 MINOR): a stored 1day bar on a
    Muhurat date is NOT verified — corruption there is invisible to this
    check; the skipped dates are surfaced as ``muhurat_dates`` per cell and
    rolled up as ``muhurat_days_unverified`` on the sub-check. Only a symbol
    whose EVERY comparable day is Muhurat-ambiguous abstains.
    """
    from brutex.calendars.nse import daily_derive_cutoff, has_muhurat_session
    from brutex.data.fno_daily_resample import resample_1m_to_fno_daily
    from brutex.data.spot_daily_resample import resample_1m_to_daily

    if not _has_interval_files(symbol_dir, "1minute"):
        return {"key": key, "state": "no_1m_base"}
    if not _has_interval_files(symbol_dir, "1day"):
        return {"key": key, "state": "no_stored_partition"}
    df_1m = read_bars_frame_bounded(symbol_dir, "1minute", months)
    stored = read_bars_frame_bounded(symbol_dir, "1day", _daily_month_cells(months))
    if df_1m is None or stored is None or df_1m.height == 0:
        return {"key": key, "state": "no_1m_base"}
    before = daily_derive_cutoff(ctx.now_ist())
    resample = (
        resample_1m_to_fno_daily if "/FNO/" in symbol_dir.as_posix() else (resample_1m_to_daily)
    )
    expected = resample(df_1m, before=before)
    muhurat_dates: set[Any] = set()
    expected_map: dict[Any, tuple[Any, ...]] = {}
    for bar in expected:
        bar_ist_date = (bar.timestamp + IST_OFFSET).date()
        if has_muhurat_session(bar_ist_date):
            muhurat_dates.add(bar_ist_date)
            continue
        expected_map[bar.timestamp] = (bar.open, bar.high, bar.low, bar.close, bar.volume)
    forming: list[str] = []
    divergent: list[str] = []
    compared = 0
    for row in stored.iter_rows(named=True):
        ts = row["ts_utc"]
        ist_date = (ts + IST_OFFSET).date()
        if months is not None and (ist_date.year, ist_date.month) not in months:
            continue  # the padded predecessor file's out-of-window rows
        if has_muhurat_session(ist_date):
            muhurat_dates.add(ist_date)
            continue
        if ist_date >= before:
            forming.append(ts.isoformat())
            continue
        got = expected_map.pop(ts.replace(tzinfo=None), None) or expected_map.pop(ts, None)
        if got is None:
            divergent.append(f"{ts.isoformat()}: stored bar not derivable from 1m")
            continue
        compared += 1
        stored_vals = tuple(Decimal(str(row[c])) for c in ("open", "high", "low", "close"))
        if stored_vals != tuple(got[:4]) or int(row["volume"]) != int(got[4]):
            divergent.append(f"{ts.isoformat()}: stored != re-derived")
    muhurat_iso = sorted(d.isoformat() for d in muhurat_dates)
    if forming:
        return {"key": key, "state": "forming_day_stored", "forming": forming[:5]}
    if divergent or expected_map:
        missing = [ts.isoformat() for ts in list(expected_map)[:5]]
        return {
            "key": key,
            "state": "divergent",
            "divergent": divergent[:5],
            "missing_from_stored": missing,
            "muhurat_days_skipped": len(muhurat_dates),
            "muhurat_dates": muhurat_iso,
        }
    if compared == 0 and muhurat_dates:
        # EVERY comparable day was a Muhurat session — the only whole-cell abstain.
        return {
            "key": key,
            "state": "daily_cutoff_ambiguous",
            "muhurat_days_skipped": len(muhurat_dates),
            "muhurat_dates": muhurat_iso,
        }
    return {
        "key": key,
        "state": "equal",
        "muhurat_days_skipped": len(muhurat_dates),
        "muhurat_dates": muhurat_iso,
    }


def run(ctx: AcceptanceContext) -> CheckResult:
    """Run intraday + daily derived-TF freshness over the sampled scope."""
    if ctx.pull_active() and not ctx.force_during_pull:
        return _abstain_check(ctx, AbstainReason.PULL_IN_PROGRESS)
    plan = sample_datasets(
        ctx.spot_datasets(),
        seed=ctx.seed,
        sample_n=ctx.sample_n,
        full=ctx.full,
    )
    contracts = ctx.fno_contracts()
    if not ctx.full:
        # Seed-ROTATED fill (round 2 fix 8) — mirrors unique's contract law.
        contracts = sample_fno_contracts(contracts, seed=ctx.seed, sample_n=FNO_SAMPLE_N)
    if not plan.selected and not contracts:
        return _abstain_check(ctx, AbstainReason.NO_DATASETS)

    def _paths() -> list[Path]:
        paths: list[Path] = []
        for exchange, segment, symbol in plan.selected:
            paths.extend((ctx.lake_root / "bars" / exchange / segment / symbol).rglob("*.parquet"))
        for contract in contracts:
            paths.extend((ctx.lake_root / "bars" / "NSE" / "FNO" / contract).rglob("*.parquet"))
        return sorted(set(paths))

    return run_fingerprinted_check(
        ctx,
        check_id=CHECK_ID,
        sub_id="tf.intraday",
        paths_fn=_paths,
        body=lambda: _run_body(ctx, plan=plan, contracts=contracts),
    )


def _run_body(ctx: AcceptanceContext, *, plan: Any, contracts: list[str]) -> CheckResult:
    from brutex.data.tf_autoderive import lake_derived_freshness_verdicts
    from brutex.data.timeframe_materialize import INTRADAY_TF_INTERVALS

    months = None if ctx.full else set(recent_months(ctx.run_date(), ctx.months))
    intervals = tuple(INTRADAY_TF_INTERVALS.values())
    verdicts: dict[tuple[str, str], str] = {}
    by_group: dict[tuple[str, str], list[str]] = {}
    for exchange, segment, symbol in plan.selected:
        by_group.setdefault((exchange, segment), []).append(symbol)
    if contracts:
        by_group.setdefault(("NSE", "FNO"), []).extend(contracts)
    for (exchange, segment), symbols in by_group.items():
        verdicts.update(
            {
                (f"{exchange}/{segment}/{sym}", tf): v
                for (sym, tf), v in lake_derived_freshness_verdicts(
                    ctx.lake_root,
                    exchange=exchange,
                    segment=segment,
                    symbols=symbols,
                    intervals=intervals,
                ).items()
            }
        )
    stale = sorted(k for k, v in verdicts.items() if v != "fresh")
    intraday_sub = make_subcheck(
        "tf.intraday",
        Verdict.PASS if not stale else Verdict.FAIL,
        units_checked=len(verdicts),
        evidence={
            "pairs_keyed": len(verdicts),
            "stale_pairs": [f"{sym}@{tf}" for sym, tf in stale[:PAIR_CAP]],
            "stale_pairs_total": len(stale),
            "stale_pairs_truncated": len(stale) > PAIR_CAP,
        },
        limits=[
            "exact OHLCV value equality after keep-first dedup — no tolerance",
            "the freshness seam reads full stored history (seam-owned; --months "
            "bounds the 1day compare only)",
        ],
        repro=_repro(ctx),
    )

    cells: list[dict[str, Any]] = []
    for exchange, segment, symbol in plan.selected:
        symbol_dir = ctx.lake_root / "bars" / exchange / segment / symbol
        cells.append(
            _daily_compare_for_symbol(
                ctx, symbol_dir, key=f"{exchange}/{segment}/{symbol}", months=months
            )
        )
    for contract in contracts:
        symbol_dir = ctx.lake_root / "bars" / "NSE" / "FNO" / contract
        cells.append(
            _daily_compare_for_symbol(ctx, symbol_dir, key=f"NSE/FNO/{contract}", months=months)
        )
    bad_states = {"divergent", "forming_day_stored"}
    failures = [c for c in cells if c["state"] in bad_states]
    ambiguous = [c for c in cells if c["state"] == "daily_cutoff_ambiguous"]
    compared = [c for c in cells if c["state"] == "equal"] + failures
    if failures:
        daily_verdict = Verdict.FAIL
        daily_reason = None
    elif not compared and ambiguous:
        daily_verdict = Verdict.ABSTAIN
        daily_reason = AbstainReason.DAILY_CUTOFF_AMBIGUOUS
    else:
        daily_verdict = Verdict.PASS
        daily_reason = None
    muhurat_unverified = sorted({d for c in cells for d in c.get("muhurat_dates", [])})
    daily_sub = make_subcheck(
        "tf.daily",
        daily_verdict,
        units_checked=len(compared),
        evidence={
            "cells": cells[:PAIR_CAP],
            "cells_total": len(cells),
            "cells_truncated": len(cells) > PAIR_CAP,
            "equal": sum(1 for c in cells if c["state"] == "equal"),
            "failures": len(failures),
            "ambiguous_abstained": len(ambiguous),
            "muhurat_days_skipped": sum(int(c.get("muhurat_days_skipped", 0)) for c in cells),
            "muhurat_days_unverified": muhurat_unverified[:PAIR_CAP],
            "muhurat_days_unverified_total": len(muhurat_unverified),
            "skipped": sum(1 for c in cells if c["state"] in {"no_1m_base", "no_stored_partition"}),
        },
        abstain_reason=daily_reason,
        limits=[
            "forming sessions excluded from BOTH sides (cutoff law)",
            "Muhurat sessions skipped PER-DAY on both sides (one count per "
            "distinct IST date — never a whole-history abstain)",
            "stored 1day bars on Muhurat session dates are NOT verified — a "
            "corrupt or fabricated Muhurat-date daily bar is invisible to this "
            "check (the skipped dates are named in muhurat_days_unverified)",
            f"1day compare bounded to the last {ctx.months} months (--full lifts)",
        ],
        repro=_repro(ctx),
    )

    coverage = make_coverage(
        mode=plan.mode,
        population=plan.population + len(ctx.fno_contracts()),
        examined=len(plan.selected) + len(contracts),
        seed=ctx.seed,
        selection=[*plan.selection_keys, *(f"NSE/FNO/{c}" for c in contracts)],
    )
    return make_check(
        CHECK_ID,
        [intraday_sub, daily_sub, _oi_standing_abstain(ctx)],
        coverage=coverage,
        repro=_repro(ctx),
    )
