"""Capture generator for the cascade/results-depth Rust parity goldens.

DEC-RUST-CASCADE-RESULTS-001 (T14 lane). Run ONCE against the PRE-conversion
pure-Python implementations (the capture-first discipline) to freeze the
fixtures under ``tests/golden/fixtures/cascade_rust_parity/``; the replay
golden (``test_cascade_rust_parity_golden.py``) then drives the post-conversion
(Rust-backed) API against these frozen bytes.

Refuses to overwrite existing fixtures unless ``BRUTEX_REGEN_CASCADE_PARITY=1``
(the preopen-golden generator precedent) — the pre-conversion capture IS the
oracle; regenerating it against the converted tree would be a vacuous
self-comparison.

Usage::

    .venv/bin/python tests/golden/gen_cascade_rust_parity.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:  # pragma: no cover - direct-script convenience
    sys.path.insert(0, str(_REPO_ROOT))

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "cascade_rust_parity"

_REGEN_ENV = "BRUTEX_REGEN_CASCADE_PARITY"


def _join_to_jsonable(join: Any) -> dict[str, Any]:
    """Serialize a CascadeJoin deterministically (sorted candidate keys)."""
    return {
        "sweep_id": join.sweep_id,
        "version": join.version,
        "n_input": join.n_input,
        "n_survived": join.n_survived,
        "by_candidate": {
            cid: {
                "candidate_id": view.candidate_id,
                "instrument": view.instrument,
                "tf": view.tf,
                "passed": view.passed,
                "rejected_at": view.rejected_at,
                "detail": view.detail,
                "cascade_composite": view.cascade_composite,
                "provisional": view.provisional,
                "cpcv_pass_rate": view.cpcv_pass_rate,
                "survivor_rank": view.survivor_rank,
            }
            for cid, view in sorted(join.by_candidate.items())
        },
        "ambiguous_ids": sorted(join.ambiguous_ids),
        "groups": [
            {
                "instrument": g.instrument,
                "tf": g.tf,
                "n_input": g.n_input,
                "n_survived": g.n_survived,
                "pool_pbo": g.pool_pbo,
                "dsr_n_source": g.dsr_n_source,
                "dsr_n_trials": g.dsr_n_trials,
                "dsr_trial_variance": g.dsr_trial_variance,
            }
            for g in join.groups
        ],
        "skipped_groups": join.skipped_groups,
        "skipped_verdicts": join.skipped_verdicts,
        "nulled_values": join.nulled_values,
        "rank_inconsistencies": join.rank_inconsistencies,
    }


def capture_cascade_join() -> list[dict[str, Any]]:
    from brutex.engine.cascade_join import CascadeJoinError, build_cascade_join
    from structlog.testing import capture_logs

    from tests.golden._cascade_rust_parity_corpus import cascade_join_payloads

    out: list[dict[str, Any]] = []
    for name, payload in cascade_join_payloads():
        case: dict[str, Any] = {"name": name}
        with capture_logs() as logs:
            try:
                join = build_cascade_join(payload)
            except CascadeJoinError as exc:
                case["expect"] = {"error": str(exc)}
            else:
                warns = [e for e in logs if e.get("event") == "cascade_join.partial"]
                case["expect"] = {
                    "join": _join_to_jsonable(join),
                    "warn": None
                    if not warns
                    else {
                        "sweep_id": warns[0]["sweep_id"],
                        "skipped_groups": warns[0]["skipped_groups"],
                        "skipped_verdicts": warns[0]["skipped_verdicts"],
                        "nulled_values": warns[0]["nulled_values"],
                        "rank_inconsistencies": warns[0]["rank_inconsistencies"],
                        "duplicate_ids": warns[0]["duplicate_ids"],
                        "details": list(warns[0]["details"]),
                    },
                }
        out.append(case)
    return out


def capture_minute_path() -> dict[str, Any]:
    from brutex.engine.minute_path import (
        MinuteIndex,
        ist_session_dates,
        minute_path_max_drawdown,
        session_equity_series,
        trade_excursions_1m,
    )
    from brutex.engine.minute_path import (
        SessionEquityPoint as Point,
    )

    from tests.golden._cascade_rust_parity_corpus import (
        drawdown_cases,
        equity_cases,
        excursion_cases,
        session_dates_cases,
    )

    excursions: list[dict[str, Any]] = []
    for case in excursion_cases():
        index = MinuteIndex.from_bars(case["bars"])
        result = trade_excursions_1m(
            index,
            entry_ts=case["entry_ts"],
            exit_ts=case["exit_ts"],
            entry_fill_paisa=case["entry_fill_paisa"],
            direction=case["direction"],
            exit_bar_full_range=case["exit_bar_full_range"],
        )
        excursions.append(
            {
                "name": case["name"],
                "expect": {
                    "mae_paisa": result.mae_paisa,
                    "mfe_paisa": result.mfe_paisa,
                    "status": result.status.value,
                },
            }
        )

    equity: list[dict[str, Any]] = []
    for case in equity_cases():
        bars = case["bars"]
        index = None if bars is None else MinuteIndex.from_bars(bars)
        points, status = session_equity_series(
            index,
            case["trades"],
            sessions=case["sessions"],
            start_equity_paisa=case["start_equity_paisa"],
        )
        equity.append(
            {
                "name": case["name"],
                "expect": {
                    "status": status.value,
                    "points": [
                        [
                            p.session_date.isoformat(),
                            p.equity_close_paisa,
                            p.equity_intraday_low_paisa,
                            p.trades_closed,
                        ]
                        for p in points
                    ],
                },
            }
        )

    drawdowns: list[dict[str, Any]] = []
    for case in drawdown_cases():
        points = tuple(
            Point(
                session_date=d,
                equity_close_paisa=close,
                equity_intraday_low_paisa=low,
                trades_closed=n,
            )
            for d, close, low, n in case["points"]
        )
        dd, pct = minute_path_max_drawdown(points, start_equity_paisa=case["start_equity_paisa"])
        drawdowns.append({"name": case["name"], "expect": {"max_dd_paisa": dd, "max_dd_pct": pct}})

    dates: list[dict[str, Any]] = []
    for case in session_dates_cases():
        dates.append(
            {
                "name": case["name"],
                "expect": [d.isoformat() for d in ist_session_dates(case["bars"])],
            }
        )
    return {"excursions": excursions, "equity": equity, "drawdowns": drawdowns, "dates": dates}


def capture_combo_aggregates() -> dict[str, Any]:
    from brutex.engine.combo_aggregates import _float_stats, aggregate_combo, nearest_rank

    from tests.golden._cascade_rust_parity_corpus import (
        aggregate_cases,
        float_stats_cases,
        nearest_rank_cases,
    )

    ranks = [
        {"name": c["name"], "expect": nearest_rank(sorted(c["values"]), c["pct"])}
        for c in nearest_rank_cases()
    ]
    stats = [
        {"name": c["name"], "expect": list(_float_stats(sorted(c["values"])))}
        for c in float_stats_cases()
    ]
    aggregates = []
    for case in aggregate_cases():
        agg = aggregate_combo(
            case["capture"],
            case["row"],
            lens=case["lens"],
            composite_score=case["composite_score"],
            fingerprint=case["fingerprint"],
            start_equity_paisa=case["start_equity_paisa"],
        )
        aggregates.append({"name": case["name"], "expect": agg.to_record()})
    return {"nearest_rank": ranks, "float_stats": stats, "aggregates": aggregates}


def main() -> int:
    if FIXTURE_DIR.exists() and any(FIXTURE_DIR.iterdir()) and os.environ.get(_REGEN_ENV) != "1":
        sys.stderr.write(
            f"REFUSED: fixtures already exist under {FIXTURE_DIR} — the pre-conversion\n"
            f"capture is the oracle. Set {_REGEN_ENV}=1 only for a DELIBERATE,\n"
            "ledgered re-baseline (DEC-RUST-CASCADE-RESULTS-001).\n"
        )
        return 2
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    payloads = {
        "cascade_join.json": capture_cascade_join(),
        "minute_path.json": capture_minute_path(),
        "combo_aggregates.json": capture_combo_aggregates(),
    }
    for filename, payload in payloads.items():
        path = FIXTURE_DIR / filename
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        sys.stdout.write(f"wrote {path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
