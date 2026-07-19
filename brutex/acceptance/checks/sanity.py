"""Check 6 — stored-data sanity over the sampled datasets (composed predicates).

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1958). Pure production
predicates re-used verbatim: price sanity (:func:`count_price_violations`),
the bounded bad-tick screen (:func:`is_bad_tick`, the #1616 disclosed escapes
named), the DEC-SPOT-PRICE-2DP-001 tick grid, the real per-date NSE session
window, the interior session-hole census (:func:`interior_gap_runs` — the NEW
lake-wide driver, inventory GAP 3), the VIX silent-empty detector, and the
completeness scoreboard folds.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from brutex.acceptance.checks import run_fingerprinted_check
from brutex.acceptance.context import AcceptanceContext, StoreUnavailable
from brutex.acceptance.model import (
    AbstainReason,
    CheckResult,
    Verdict,
    make_check,
    make_coverage,
    make_subcheck,
)
from brutex.acceptance.sampling import (
    VIX_COORD,
    month_bounded_files,
    read_bars_frame_bounded,
    recent_months,
    sample_datasets,
)

CHECK_ID = "sanity"

#: Per-dataset offender samples are capped at this many entries.
ROW_CAP = 20

#: Float slack for the 2dp tick-grid law (prices are 2dp-snapped at write).
_GRID_EPS = 1e-6


def _repro(ctx: AcceptanceContext) -> str:
    """Return the ``brutex verify-acceptance`` reproduce command for this check.

    Returns
    -------
    str
        The CLI invocation that re-runs this check with the run seed.
    """
    return f"brutex verify-acceptance --checks {CHECK_ID} --seed {ctx.seed}"


def _read_frame(
    symbol_dir: Path, interval: str, months: set[tuple[int, int]] | None = None
) -> pl.DataFrame | None:
    """Read a symbol's interval bars, optionally bounded to given months.

    Parameters
    ----------
    symbol_dir
        The symbol partition root.
    interval
        The bar interval to read.
    months
        Optional ``(year, month)`` set bounding which month files are read;
        ``None`` reads all present months.

    Returns
    -------
    pl.DataFrame | None
        The concatenated bars frame, or ``None`` when no files match.
    """
    frame = read_bars_frame_bounded(symbol_dir, interval, months)
    return None if frame is None or frame.height == 0 else frame


def _with_ist(frame: pl.DataFrame) -> pl.DataFrame:
    """Add a fixed +05:30 IST timestamp column to a UTC bars frame.

    Returns
    -------
    pl.DataFrame
        The input frame with an added IST-shifted timestamp column.
    """
    ist = frame.get_column("ts_utc").dt.replace_time_zone("UTC").dt.offset_by("5h30m")
    return frame.with_columns(
        ist.dt.date().alias("ist_date"),
        (ist.dt.hour().cast(pl.Int32) * 60 + ist.dt.minute().cast(pl.Int32)).alias("ist_minute"),
    )


def _tick_grid_violations(frame: pl.DataFrame) -> int:
    """Bars whose any price has >2 decimals (violates the 2dp write law)."""
    columns = ("open", "high", "low", "close")
    expr = ((pl.col(columns[0]) * 100).round(0) - pl.col(columns[0]) * 100).abs() > _GRID_EPS
    for col in columns[1:]:
        expr = expr | (((pl.col(col) * 100).round(0) - pl.col(col) * 100).abs() > _GRID_EPS)
    return int(frame.select(expr.sum()).item())


def _bad_tick_flags(frame_ist: pl.DataFrame, *, max_ratio: float) -> list[str]:
    """Per-IST-session bad-tick scan reusing the production predicate.

    Mirrors :class:`~brutex.data.bad_tick.BadTickScreen` semantics as a scan:
    first bar of each session exempt; a flagged bar never advances the
    baseline. The #1616 disclosed escapes (sub-4x, 2-bar ×10 shapes) stand.
    """
    from brutex.data.bad_tick import is_bad_tick

    flagged: list[str] = []
    ordered = frame_ist.sort("ts_utc")
    prev_close: float | None = None
    prev_date: date | None = None
    for row in ordered.iter_rows(named=True):
        d = row["ist_date"]
        close = float(row["close"])
        if d != prev_date:
            prev_date, prev_close = d, close
            continue
        if prev_close is not None and is_bad_tick(prev_close, close, max_ratio=max_ratio):
            flagged.append(f"{row['ts_utc'].isoformat()} close={close} prev={prev_close}")
            continue  # a skipped spike never advances the baseline
        prev_close = close
    return flagged


def _session_window_violations(frame_ist: pl.DataFrame) -> tuple[list[str], int]:
    """Bars outside their own IST date's REAL session + non-trading-date bars."""
    from brutex.calendars.nse import get_session, is_holiday_data_available

    offenders: list[str] = []
    out_of_coverage = 0
    for (key,), day_frame in frame_ist.group_by(["ist_date"], maintain_order=True):
        if not isinstance(key, date):  # pragma: no cover — polars date group keys
            continue
        d = key
        if not is_holiday_data_available(d):
            out_of_coverage += day_frame.height
            continue
        session = get_session(d)
        if session is None:
            offenders.extend(
                f"{ts.isoformat()} on non-trading {d}"
                for ts in day_frame.get_column("ts_utc").head(ROW_CAP).to_list()
            )
            continue
        open_min = session.open_time_ist.hour * 60 + session.open_time_ist.minute
        close_min = session.close_time_ist.hour * 60 + session.close_time_ist.minute
        bad = day_frame.filter(
            (pl.col("ist_minute") < open_min) | (pl.col("ist_minute") >= close_min)
        )
        offenders.extend(
            f"{ts.isoformat()} outside session of {d}"
            for ts in bad.get_column("ts_utc").head(ROW_CAP).to_list()
        )
    return offenders, out_of_coverage


def run(ctx: AcceptanceContext) -> CheckResult:
    """Run the seven sanity sub-checks over the sampled datasets."""
    if ctx.pull_active() and not ctx.force_during_pull:
        sub = make_subcheck(
            "sanity.price",
            Verdict.ABSTAIN,
            units_checked=0,
            evidence={"reason": "pull_in_progress"},
            abstain_reason=AbstainReason.PULL_IN_PROGRESS,
            repro=_repro(ctx),
        )
        coverage = make_coverage(mode="none", population=0, examined=0, seed=ctx.seed)
        return make_check(CHECK_ID, [sub], coverage=coverage, repro=_repro(ctx))

    plan = sample_datasets(
        ctx.spot_datasets(),
        seed=ctx.seed,
        sample_n=ctx.sample_n,
        full=ctx.full,
    )
    months = None if ctx.full else set(recent_months(ctx.run_date(), ctx.months))

    def _paths() -> list[Path]:
        paths: list[Path] = []
        for exchange, segment, symbol in plan.selected:
            symbol_dir = ctx.lake_root / "bars" / exchange / segment / symbol
            for interval in ("1minute", "1day"):
                paths.extend(month_bounded_files(symbol_dir / interval, months))
        vix_dir = ctx.lake_root / "bars" / VIX_COORD[0] / VIX_COORD[1] / VIX_COORD[2]
        paths.extend(month_bounded_files(vix_dir / "1minute", months))
        return sorted(set(paths))

    return run_fingerprinted_check(
        ctx,
        check_id=CHECK_ID,
        sub_id="sanity.price",
        paths_fn=_paths,
        body=lambda: _run_body(ctx, plan=plan, months=months),
    )


def _run_body(
    ctx: AcceptanceContext, *, plan: Any, months: set[tuple[int, int]] | None
) -> CheckResult:
    """Run the stored-data sanity battery over the sampled coordinates.

    Parameters
    ----------
    ctx
        The acceptance context bound to the real lake.
    plan
        The sampling plan of coordinates to examine.
    months
        Optional ``(year, month)`` bound applied to the reads.

    Returns
    -------
    CheckResult
        The composed sanity check result.
    """
    from brutex.data.bad_tick import BAD_TICK_MAX_RATIO
    from brutex.data.interior_gap_detect import interior_gap_runs
    from brutex.data.price_sanity import count_price_violations

    price_offenders: list[str] = []
    bad_ticks: list[str] = []
    grid_offenders: list[str] = []
    session_offenders: list[str] = []
    out_of_coverage_bars = 0
    holes: list[str] = []
    hole_abstained = 0
    bars_scanned = 0
    datasets_scanned = 0
    latest_spot_date: date | None = None
    for exchange, segment, symbol in plan.selected:
        symbol_dir = ctx.lake_root / "bars" / exchange / segment / symbol
        key = f"{exchange}/{segment}/{symbol}"
        for interval in ("1minute", "1day"):
            frame = _read_frame(symbol_dir, interval, months)
            if frame is None:
                continue
            violations = count_price_violations(frame)
            if violations:
                price_offenders.append(f"{key}@{interval}: {violations} bad row(s)")
            grid = _tick_grid_violations(frame)
            if grid:
                grid_offenders.append(f"{key}@{interval}: {grid} off-grid row(s)")
            bars_scanned += frame.height
        frame_1m = _read_frame(symbol_dir, "1minute", months)
        if frame_1m is None or frame_1m.height == 0:
            continue
        datasets_scanned += 1
        frame_ist = _with_ist(frame_1m)
        for hit in _bad_tick_flags(frame_ist, max_ratio=BAD_TICK_MAX_RATIO)[:ROW_CAP]:
            bad_ticks.append(f"{key}: {hit}")
        offenders, out_cov = _session_window_violations(frame_ist)
        session_offenders.extend(f"{key}: {o}" for o in offenders[:ROW_CAP])
        out_of_coverage_bars += out_cov
        covered = sorted(frame_ist.get_column("ist_date").unique().to_list())
        if (exchange, segment, symbol) != VIX_COORD and covered:
            latest_spot_date = max(latest_spot_date or covered[-1], covered[-1])
        runs = interior_gap_runs(covered_dates=covered)
        if runs is None:
            hole_abstained += 1
        else:
            holes.extend(f"{key}: {first} → {last}" for first, last in runs)

    def _sub(id_: str, offenders: list[str], units: int, **extra: Any) -> Any:
        return make_subcheck(
            id_,
            Verdict.FAIL if offenders else Verdict.PASS,
            units_checked=units,
            evidence={
                "offenders": offenders[:ROW_CAP],
                "offenders_total": len(offenders),
                "offenders_truncated": len(offenders) > ROW_CAP,
                **extra,
            },
            repro=_repro(ctx),
        )

    price_sub = _sub("sanity.price", price_offenders, bars_scanned)
    bad_tick_sub = make_subcheck(
        "sanity.bad_tick",
        Verdict.FAIL if bad_ticks else Verdict.PASS,
        units_checked=bars_scanned,
        evidence={
            "offenders": bad_ticks[:ROW_CAP],
            "offenders_total": len(bad_ticks),
            "max_ratio": BAD_TICK_MAX_RATIO,
        },
        limits=[
            "heuristic 4x screen — the #1616 disclosed escapes (sub-4x, 2-bar x10 shapes) stand"
        ],
        repro=_repro(ctx),
    )
    grid_sub = _sub("sanity.tick_grid", grid_offenders, bars_scanned)
    session_sub = _sub(
        "sanity.session_window",
        session_offenders,
        bars_scanned,
        out_of_coverage_bars_abstained=out_of_coverage_bars,
    )
    contiguity_sub = _sub(
        "sanity.contiguity",
        holes,
        datasets_scanned,
        datasets_abstained=hole_abstained,
    )

    # ── sanity.vix_presence — the acceptance-night incident, detected ──
    vix_dir = ctx.lake_root / "bars" / VIX_COORD[0] / VIX_COORD[1] / VIX_COORD[2]
    vix_frame = _read_frame(vix_dir, "1minute", months)
    if latest_spot_date is None:
        vix_sub = make_subcheck(
            "sanity.vix_presence",
            Verdict.ABSTAIN,
            units_checked=0,
            evidence={"reason": "no spot session to anchor the VIX presence probe"},
            abstain_reason=AbstainReason.NOTHING_TO_CHECK,
            repro=_repro(ctx),
        )
    else:
        vix_bars_on_day = 0
        if vix_frame is not None and vix_frame.height:
            vix_ist = _with_ist(vix_frame)
            vix_bars_on_day = vix_ist.filter(pl.col("ist_date") == latest_spot_date).height
        vix_sub = make_subcheck(
            "sanity.vix_presence",
            Verdict.PASS if vix_bars_on_day > 0 else Verdict.FAIL,
            units_checked=1,
            evidence={
                "anchor_session": latest_spot_date.isoformat(),
                "vix_bars_on_anchor_session": vix_bars_on_day,
                "failure_class": None if vix_bars_on_day else "vix_silent_empty",
            },
            repro=_repro(ctx),
        )

    # ── sanity.completeness — the scoreboard fold (engine rows only gate) ──
    completeness_sub = _completeness_sub(ctx)

    coverage = make_coverage(
        mode=plan.mode,
        population=plan.population,
        examined=len(plan.selected),
        seed=ctx.seed,
        selection=plan.selection_keys,
    )
    return make_check(
        CHECK_ID,
        [
            price_sub,
            bad_tick_sub,
            grid_sub,
            session_sub,
            contiguity_sub,
            vix_sub,
            completeness_sub,
        ],
        coverage=coverage,
        repro=_repro(ctx),
        limits=(
            []
            if months is None
            else [
                f"reads bounded to the last {ctx.months} months (--full lifts) — "
                "contiguity holes older than the window are out of scope this run"
            ]
        ),
    )


def _completeness_sub(ctx: AcceptanceContext) -> Any:
    """Engine indexes must not read failed/none on the completeness scoreboard.

    The verdict is composed through the EXACT production ``/completeness``
    inputs (``app.py`` ``_completeness_inputs``): the FNO-pruned
    ``summarize_spot_verdict_datasets`` scan folded with the window-ledger
    ``spot_window_rollup`` chip via ``build_spot_coverage_payload``, then scored
    by the same ``per_symbol_completeness``. On a HEALTHY master-present lake
    every engine index therefore reads its TRUE verdict (settled → full, etc.),
    never a fabricated ``none`` (round-6 BLOCKER — the prior plain
    ``summarize_spot_datasets`` dropped the ``completeness`` chip so every engine
    index FAILed regardless of data health). A genuinely incomplete engine index
    still reads failed/none → FAIL (the true-positive tooth).

    The master is opened READ-ONLY (never ``InstrumentStore.open`` — its
    migration DDL DROPs an older-schema store; review round 1 M1). A locked /
    unreadable / schema-foreign store ⇒ named ABSTAIN — never a migration,
    never a write, never a crash.
    """
    import duckdb

    from brutex.acceptance.context import open_instrument_store_readonly
    from brutex.data.errors import CatalogError

    store_path = ctx.lake_root / "instruments.duckdb"
    if not store_path.is_file():
        return make_subcheck(
            "sanity.completeness",
            Verdict.ABSTAIN,
            units_checked=0,
            evidence={"reason": "no instruments.duckdb beside the lake"},
            abstain_reason=AbstainReason.NO_INSTRUMENT_STORE,
            repro=_repro(ctx),
        )
    from brutex.dashboard.completeness import (
        categorize_instruments,
        per_symbol_completeness,
    )
    from brutex.dashboard.coverage_view import build_spot_coverage_payload
    from brutex.dashboard.db_view import (
        spot_window_rollup,
        summarize_spot_verdict_datasets,
    )

    try:
        store = open_instrument_store_readonly(store_path)
    except StoreUnavailable as exc:
        return make_subcheck(
            "sanity.completeness",
            Verdict.ABSTAIN,
            units_checked=0,
            evidence={"reason": f"instrument master not readable read-only: {exc}"},
            abstain_reason=AbstainReason.NO_INSTRUMENT_STORE,
            repro=_repro(ctx),
        )
    try:
        with store:
            instruments = store.list_index_equity_instruments()
            # Compose the SAME enrichment the production /completeness route applies
            # (app.py `_completeness_inputs`): the FNO-pruned verdict-interval scan
            # folded with the window-ledger rollup chip, so `row["completeness"]`
            # carries the settled/pending/failed authority. The prior plain
            # `summarize_spot_datasets` returned rows with NO `completeness` key, so
            # `_spot_dimension_verdict(None)` read `none` for EVERY engine index on
            # a HEALTHY master-present lake — a permanent spurious FAIL contradicting
            # the operator's own /completeness page (round-6 BLOCKER). Verify what
            # production SHOWS; never a weaker reimplementation (the whole-slice law).
            spot_rows = build_spot_coverage_payload(
                summarize_spot_verdict_datasets(store.connection, ctx.lake_root),
                spot_window_rollup(ctx.lake_root),
            )["rows"]
            records = list(store.list_coverage())
    except (duckdb.Error, CatalogError) as exc:
        # An older/foreign-schema store: observe-only means ABSTAIN, never DDL.
        return make_subcheck(
            "sanity.completeness",
            Verdict.ABSTAIN,
            units_checked=0,
            evidence={"reason": f"instrument master query failed read-only: {exc}"},
            abstain_reason=AbstainReason.NO_INSTRUMENT_STORE,
            repro=_repro(ctx),
        )
    categories = categorize_instruments(
        instruments,
        is_index=lambda i: bool(getattr(i, "is_index", False)),
        is_reference=lambda i: bool(getattr(i, "reference_only", False)),
    )
    per_symbol: dict[str, Any] = per_symbol_completeness(spot_rows, categories, records, categories)
    engine_bad: list[str] = []
    from brutex.data.lake_invariants import ENGINE_INDEX_COORDS

    for _exchange, _segment, symbol in ENGINE_INDEX_COORDS:
        cell = per_symbol.get(symbol)
        # Gate the SPOT dimension ONLY. The engine brute-forces the 3 indices on
        # SPOT (1minute → the derived TFs); their OPTIONS are REFERENCE_ONLY, so
        # an index with no stored option data legitimately reads options
        # ``none`` — that is not an engine-data-health defect. Walking the whole
        # cell (incl. that options ``none``) would false-FAIL every master-present
        # index whose options aren't materialised (round-6 BLOCKER, second
        # vector). ``settled_partial_range``/``partial``/``full`` all PASS —
        # reference-name partial-range is evidence, not a defect.
        spot = cell.get("spot") if isinstance(cell, dict) else None
        verdicts = _cell_verdict_tokens(spot)
        if any(v in {"failed", "none"} for v in verdicts):
            engine_bad.append(f"{symbol}: spot={sorted(verdicts)}")
    return make_subcheck(
        "sanity.completeness",
        Verdict.FAIL if engine_bad else Verdict.PASS,
        units_checked=len(per_symbol),
        evidence={
            "engine_bad": engine_bad,
            "symbols_on_scoreboard": len(per_symbol),
        },
        limits=[
            "gates the SPOT dimension only — an engine index's REFERENCE_ONLY "
            "options reading 'none' is evidence, not a defect; "
            "settled_partial_range/partial are evidence, not defect"
        ],
        repro=_repro(ctx),
    )


def _cell_verdict_tokens(cell: Any) -> set[str]:
    """Every verdict-shaped string inside one per-symbol scoreboard cell."""
    tokens: set[str] = set()
    if isinstance(cell, dict):
        for value in cell.values():
            if isinstance(value, str):
                tokens.add(value)
            elif isinstance(value, dict):
                tokens |= _cell_verdict_tokens(value)
    return tokens
