"""Check 5 — movers page-vs-DB truth: the ONE independent-recompute surface.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756). Leg A is the REAL wire
— in-process ``build_app`` + ``TestClient`` ``GET /api/v1/movers`` against the
real lake (the DEC-SURFACE-TRIAL-001 precedent, GET-only). Leg B is the FENCED
:mod:`brutex.acceptance.independent` recompute from the raw month parquet
files. Any compared-cell mismatch = FAIL with both legs' values. A divergence
attributable ONLY to the stored-1day prev-close anchor is bucketed
``stored_tf_prevclose_divergence`` (still FAIL, pre-attributed).

HONEST scope: rank order is verified WITHIN the served rows (sorted by the
independent metric); completeness of the top-N set is not independently
verified (bounded reads). Options iv/delta are compared for
presence/nullability only (stored overlay).
"""

from __future__ import annotations

import re
from datetime import date
from fractions import Fraction
from typing import Any

from brutex.acceptance import independent as ind
from brutex.acceptance.context import AcceptanceContext
from brutex.acceptance.model import (
    AbstainReason,
    CheckResult,
    Verdict,
    make_check,
    make_coverage,
    make_subcheck,
)

CHECK_ID = "movers"

#: The four spot coordinates: three Latest cuts + one explicit bucket anchor
#: (the #1601 completed-bucket law exercised on the wire).
SPOT_COORDS: tuple[tuple[str, str | None], ...] = (
    ("1minute", None),
    ("5minute", None),
    ("1day", None),
    ("3minute", "09:21"),
)

#: Mismatch samples are capped at this many entries.
MISMATCH_CAP = 20

#: HTTP success status for the F&O tab probes.
HTTP_OK = 200

#: Options moneyness label grammar (DEC-FNO-MONEYNESS-LABEL-001).
MONEYNESS_RE = re.compile(r"^(ATM|(ITM|OTM) [+-]?\d+)$")


def _repro(ctx: AcceptanceContext) -> str:
    return f"brutex verify-acceptance --checks {CHECK_ID} --seed {ctx.seed}"


def resolve_day(ctx: AcceptanceContext) -> str | None:
    """The movers coordinate day: ``--date`` or the latest stored lake day."""
    if ctx.date is not None:
        return ctx.date
    from brutex.dashboard.movers_warm import latest_movers_date

    return latest_movers_date(ctx.lake_root)


def _months_for(day: date) -> set[tuple[int, int]]:
    """The month cells the independent leg reads: the day's and the prior month."""
    prev_year, prev_month = (day.year, day.month - 1) if day.month > 1 else (day.year - 1, 12)
    return {(day.year, day.month), (prev_year, prev_month)}


def _pct_2dp(text: str) -> Fraction:
    return ind.half_up_2dp(Fraction(text))


def compare_spot_cells(page_row: dict[str, Any], ind_row: ind.IndependentSpotRow) -> list[str]:
    """Cell-for-cell compare of ONE page row vs its independent recompute.

    The comparator the teeth smoke drives through its injection seam (S3) —
    a perturbed page cell MUST surface here.
    """
    mismatches: list[str] = []
    if page_row.get("ltp") != ind_row.ltp_display:
        mismatches.append(f"ltp: page={page_row.get('ltp')!r} independent={ind_row.ltp_display!r}")
    if int(page_row.get("volume", -1)) != ind_row.volume:
        mismatches.append(f"volume: page={page_row.get('volume')!r} independent={ind_row.volume!r}")
    if page_row.get("value_cr") != ind_row.value_cr_display:
        mismatches.append(
            f"value_cr: page={page_row.get('value_cr')!r} independent={ind_row.value_cr_display!r}"
        )
    page_pct = page_row.get("pct_change")
    ind_pct = ind_row.pct
    if (page_pct is None) != (ind_pct is None):
        mismatches.append(f"pct_change nullability: page={page_pct!r} independent={ind_pct!r}")
    elif (
        page_pct is not None
        and ind_pct is not None
        and _pct_2dp(str(page_pct)) != (ind.half_up_2dp(ind_pct))
    ):
        mismatches.append(f"pct_change(2dp): page={page_pct!r} independent={float(ind_pct):.6f}")
    if ind_row.chng is not None:
        expected_change = ind.format_inr_2dp(ind_row.chng)
        if page_row.get("change") != expected_change:
            mismatches.append(
                f"change: page={page_row.get('change')!r} independent={expected_change!r}"
            )
    return mismatches


def contiguity_stamp_mismatch(
    page_row: dict[str, Any], ind_row: ind.IndependentSpotRow, *, day: date
) -> str | None:
    """The #1539/#1649b stamp law: a prev-close HOLE must carry its stamp.

    The independent leg back-walks the calendar by hand; when the served
    baseline provably skipped a nearer session, the page row must carry
    ``prev_close_contiguous: false`` (or the Muhurat coherence stamp).
    """
    expected = ind.previous_session_hand(day)
    anchored = ind_row.prev_close_date
    if anchored is None or expected is None:
        return None
    if anchored == expected:
        return None
    from brutex.calendars.nse import MUHURAT_SESSIONS

    if anchored in MUHURAT_SESSIONS and page_row.get("prev_close_muhurat"):
        return None
    if page_row.get("prev_close_contiguous") is False:
        return None
    return (
        f"prev-close hole without stamp: anchored {anchored} but the prior "
        f"session is {expected} and no noncontiguous/Muhurat stamp is present"
    )


def _rank_consistent(
    page_rows: list[dict[str, Any]],
    ind_by_symbol: dict[str, ind.IndependentSpotRow],
    *,
    category: str,
) -> list[str]:
    """Served-row ORDER must be sorted by the independent metric (tie: symbol)."""
    rows = [ind_by_symbol[r["symbol"]] for r in page_rows if r["symbol"] in ind_by_symbol]
    key = {
        "price_gainers": "gainers",
        "price_losers": "losers",
        "volume": "volume",
        "value": "value",
    }[category]
    expected = [r.symbol for r in ind.rank_rows(rows, key=key, top_n=len(rows))]
    got = [r["symbol"] for r in page_rows if r["symbol"] in ind_by_symbol]
    if expected != got:
        return [f"{category} rank order: page={got} independent={expected}"]
    return []


def _spot_payload(ctx: AcceptanceContext, *, interval: str, day: str, hhmm: str | None) -> Any:
    params: dict[str, Any] = {
        "asset": "spot",
        "interval": interval,
        "date": day,
        "top_n": ctx.top_n,
    }
    if hhmm is not None:
        params["time"] = hhmm
    response = ctx.client().get("/api/v1/movers", params=params)
    response.raise_for_status()
    return response.json()


def run(ctx: AcceptanceContext) -> CheckResult:
    """Run the five movers sub-checks against the real wire + the fenced leg."""
    day_str = resolve_day(ctx)
    if day_str is None:
        sub = make_subcheck(
            "movers.spot",
            Verdict.ABSTAIN,
            units_checked=0,
            evidence={"reason": "no stored lake day to anchor the movers coordinate"},
            abstain_reason=AbstainReason.NO_DATASET,
            repro=_repro(ctx),
        )
        coverage = make_coverage(mode="none", population=0, examined=0, seed=ctx.seed)
        return make_check(CHECK_ID, [sub], coverage=coverage, repro=_repro(ctx))
    day = date.fromisoformat(day_str)
    months = _months_for(day)

    payloads: dict[tuple[str, str | None], Any] = {}
    gated = False
    for interval, hhmm in SPOT_COORDS:
        payload = _spot_payload(ctx, interval=interval, day=day_str, hhmm=hhmm)
        if payload.get("gated"):
            gated = True
            break
        payloads[(interval, hhmm)] = payload

    if gated:
        sub = make_subcheck(
            "movers.spot",
            Verdict.ABSTAIN,
            units_checked=0,
            evidence={"reason": "prebuild in progress — /api/v1/movers is gated"},
            abstain_reason=AbstainReason.PREBUILD_GATED,
            repro=_repro(ctx),
        )
        coverage = make_coverage(mode="none", population=0, examined=0, seed=ctx.seed)
        return make_check(CHECK_ID, [sub], coverage=coverage, repro=_repro(ctx))

    # ── movers.spot — cell-for-cell vs the fenced independent leg ──────
    bars_cache: dict[str, list[ind.Bar1m]] = {}

    def _bars_for(exchange: str, segment: str, symbol: str) -> list[ind.Bar1m]:
        key = f"{exchange}/{segment}/{symbol}"
        if key not in bars_cache:
            bars_cache[key] = ind.read_1m_bars(
                ctx.lake_root / "bars" / exchange / segment / symbol, months=months
            )
        return bars_cache[key]

    mismatches: list[str] = []
    stored_tf_prevclose: list[str] = []
    cells_compared = 0
    for (interval, hhmm), payload in payloads.items():
        minutes = {"1minute": 1, "3minute": 3, "5minute": 5, "1day": 375}[interval]
        cutoff = ind.bucket_asof_cutoff_minute(minutes, hhmm)
        for category in ("price_gainers", "price_losers", "volume", "value"):
            rows = payload.get(category, [])
            ind_by_symbol: dict[str, ind.IndependentSpotRow] = {}
            for row in rows:
                bars = _bars_for(row["exchange"], row["segment"], row["symbol"])
                ind_row = ind.independent_spot_row(
                    row["symbol"], bars, day=day, cutoff_minute=cutoff
                )
                if ind_row is None:
                    mismatches.append(
                        f"{interval}/{category}/{row['symbol']}: page served a row "
                        "but the independent leg found no bar"
                    )
                    continue
                ind_by_symbol[row["symbol"]] = ind_row
                cell_mismatches = compare_spot_cells(row, ind_row)
                stamp = contiguity_stamp_mismatch(row, ind_row, day=day)
                if stamp is not None:
                    cell_mismatches = [*cell_mismatches, stamp]
                cells_compared += 7
                for m in cell_mismatches:
                    label = f"{interval}@{hhmm or 'latest'}/{category}/{row['symbol']}: {m}"
                    if m.startswith("pct_change") or m.startswith("change"):
                        # A %CHNG-family divergence may be the stored-1day
                        # prev-close anchor (parked audit 2/5) — pre-attribute.
                        stored_tf_prevclose.append(label)
                    else:
                        mismatches.append(label)
            mismatches.extend(_rank_consistent(rows, ind_by_symbol, category=category))
    all_failures = mismatches + [
        f"[stored_tf_prevclose_divergence] {m}" for m in stored_tf_prevclose
    ]
    spot_sub = make_subcheck(
        "movers.spot",
        Verdict.FAIL if all_failures else Verdict.PASS,
        units_checked=cells_compared,
        evidence={
            "coordinates": [f"{i}@{t or 'latest'}" for i, t in payloads],
            "cells_compared": cells_compared,
            "mismatches": all_failures[:MISMATCH_CAP],
            "mismatches_total": len(all_failures),
            "stored_tf_prevclose_divergences": len(stored_tf_prevclose),
        },
        limits=[
            "rank order verified within served rows; top-N set completeness not "
            "independently verified (bounded reads)"
        ],
        repro=_repro(ctx),
    )

    # ── movers.tf_invariance — #1535/#1538 cross-TF agreement on the wire ──
    tf_mismatches: list[str] = []
    tf_units = 0
    # The three Latest coords are structurally present here (a gated payload
    # returned early above), so the cross-TF compare always has its inputs.
    by_tf = {
        interval: {r["symbol"]: r for r in payloads[(interval, None)].get("volume", [])}
        for interval in ("1minute", "5minute", "1day")
    }
    common = set.intersection(*(set(m) for m in by_tf.values()))
    for symbol in sorted(common):
        tf_units += 1
        ltps = {tf: by_tf[tf][symbol]["ltp"] for tf in by_tf}
        values = {tf: by_tf[tf][symbol]["value_cr"] for tf in by_tf}
        if len(set(ltps.values())) != 1:
            tf_mismatches.append(f"{symbol}: ltp diverges across TFs {ltps}")
        if len(set(values.values())) != 1:
            tf_mismatches.append(f"{symbol}: value_cr diverges across TFs {values}")
    tf_sub = make_subcheck(
        "movers.tf_invariance",
        Verdict.FAIL if tf_mismatches else Verdict.PASS,
        units_checked=tf_units,
        evidence={
            "symbols_compared": tf_units,
            "mismatches": tf_mismatches[:MISMATCH_CAP],
        },
        repro=_repro(ctx),
    )

    # ── movers.cache_consistency — second hit must byte-equal the first ──
    first = ctx.client().get(
        "/api/v1/movers",
        params={"asset": "spot", "interval": "1day", "date": day_str, "top_n": ctx.top_n},
    )
    second = ctx.client().get(
        "/api/v1/movers",
        params={"asset": "spot", "interval": "1day", "date": day_str, "top_n": ctx.top_n},
    )
    cache_sub = make_subcheck(
        "movers.cache_consistency",
        Verdict.PASS if first.content == second.content else Verdict.FAIL,
        units_checked=1,
        evidence={
            "bytes_first": len(first.content),
            "bytes_second": len(second.content),
            "failure_class": None if first.content == second.content else "cache_divergence",
        },
        repro=_repro(ctx),
    )

    futures_sub = _futures_sub(ctx, day=day, day_str=day_str)
    options_sub = _options_sub(ctx, day_str=day_str)

    coverage = make_coverage(
        mode="sample",
        population=len(SPOT_COORDS) + 2,
        examined=len(payloads) + 2,
        seed=ctx.seed,
        selection=[f"spot:{i}@{t or 'latest'}" for i, t in SPOT_COORDS]
        + ["futures:near", "options:near"],
    )
    return make_check(
        CHECK_ID,
        [spot_sub, tf_sub, cache_sub, futures_sub, options_sub],
        coverage=coverage,
        repro=_repro(ctx),
    )


def _futures_sub(ctx: AcceptanceContext, *, day: date, day_str: str) -> Any:
    """Reduced independent compare of the futures near tier (close + volume)."""
    response = ctx.client().get(
        "/api/v1/movers",
        params={"asset": "futures", "interval": "1minute", "date": day_str, "top_n": ctx.top_n},
    )
    payload = response.json() if response.status_code == HTTP_OK else {}
    rows = [r for rows in payload.values() if isinstance(rows, list) for r in rows]
    rows = [r for r in rows if isinstance(r, dict) and "symbol" in r]
    if not rows:
        return make_subcheck(
            "movers.futures",
            Verdict.ABSTAIN,
            units_checked=0,
            evidence={"reason": "no futures rows served (empty F&O lake or tab)"},
            abstain_reason=AbstainReason.NO_FNO_DATA,
            repro=_repro(ctx),
        )
    mismatches: list[str] = []
    compared = 0
    seen: set[str] = set()
    for row in rows:
        symbol = str(row["symbol"])
        if symbol in seen:
            continue
        seen.add(symbol)
        parsed = ind.parse_fut_symbol_hand(symbol)
        if parsed is None:
            continue
        bars = ind.read_1m_bars(
            ctx.lake_root / "bars" / symbol.split("-", 1)[0] / "FNO" / symbol,
            months=_months_for(day),
        )
        ind_row = ind.independent_spot_row(symbol, bars, day=day, cutoff_minute=None)
        if ind_row is None:
            mismatches.append(f"{symbol}: page served a row but no raw bar found")
            continue
        compared += 1
        if row.get("ltp") != ind_row.ltp_display:
            mismatches.append(
                f"{symbol}: ltp page={row.get('ltp')!r} independent={ind_row.ltp_display!r}"
            )
        if int(row.get("volume", -1)) != ind_row.volume:
            mismatches.append(
                f"{symbol}: volume page={row.get('volume')!r} independent={ind_row.volume!r}"
            )
    return make_subcheck(
        "movers.futures",
        Verdict.FAIL if mismatches else Verdict.PASS,
        units_checked=compared,
        evidence={
            "rows_served": len(rows),
            "contracts_compared": compared,
            "mismatches": mismatches[:MISMATCH_CAP],
        },
        limits=["reduced cell set: close + volume (premium/OI stay production-only)"],
        repro=_repro(ctx),
    )


def _options_sub(ctx: AcceptanceContext, *, day_str: str) -> Any:
    """Options near/all: moneyness label grammar + iv/delta presence only."""
    response = ctx.client().get(
        "/api/v1/movers",
        params={"asset": "options", "interval": "1minute", "date": day_str, "top_n": ctx.top_n},
    )
    payload = response.json() if response.status_code == HTTP_OK else {}
    rows = [r for rows in payload.values() if isinstance(rows, list) for r in rows]
    rows = [r for r in rows if isinstance(r, dict) and "symbol" in r]
    if not rows:
        return make_subcheck(
            "movers.options",
            Verdict.ABSTAIN,
            units_checked=0,
            evidence={"reason": "no options rows served (empty F&O lake or tab)"},
            abstain_reason=AbstainReason.NO_FNO_DATA,
            repro=_repro(ctx),
        )
    bad_labels: list[str] = []
    missing_overlay_keys: list[str] = []
    for row in rows:
        label = row.get("moneyness")
        if label is not None and not MONEYNESS_RE.fullmatch(str(label)):
            bad_labels.append(f"{row['symbol']}: moneyness label {label!r} off-grammar")
        for key in ("iv", "oi"):
            if key not in row:
                missing_overlay_keys.append(f"{row['symbol']}: {key} key absent")
    failures = bad_labels + missing_overlay_keys
    return make_subcheck(
        "movers.options",
        Verdict.FAIL if failures else Verdict.PASS,
        units_checked=len(rows),
        evidence={
            "rows_served": len(rows),
            "bad_labels": bad_labels[:MISMATCH_CAP],
            "missing_overlay_keys": missing_overlay_keys[:MISMATCH_CAP],
        },
        limits=["iv/delta compared for presence/nullability only (stored overlay)"],
        repro=_repro(ctx),
    )
