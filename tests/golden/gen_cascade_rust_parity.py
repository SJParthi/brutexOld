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
from collections.abc import Callable
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
        eq_index = None if bars is None else MinuteIndex.from_bars(bars)
        points, status = session_equity_series(
            eq_index,
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


def capture_cascade_wire() -> dict[str, Any]:
    """Freeze the cascade_wire serialisation / parse / math-trio laws (slice 2a)."""
    from brutex.engine.cascade_wire import (
        _finite,
        _max_drawdown_return_units,
        _per_bar_returns,
        manifest_from_json,
        manifest_to_json,
        serialize_wire_report,
        wire_report_to_dict,
    )

    from tests.golden._cascade_wire_parity_corpus import (
        finite_cases,
        manifest_cases,
        max_drawdown_cases,
        parse_payload_cases,
        per_bar_returns_cases,
        wire_report_cases,
    )

    manifests: list[dict[str, Any]] = []
    for name, manifest in manifest_cases():
        blob = manifest_to_json(manifest)
        manifests.append(
            {
                "name": name,
                "expect": {
                    "json": blob.decode("utf-8"),
                    "roundtrip_ok": manifest_from_json(blob) == manifest,
                },
            }
        )

    reports: list[dict[str, Any]] = []
    for name, report in wire_report_cases():
        blob = serialize_wire_report(report)
        parsed = json.loads(blob)
        reports.append(
            {
                "name": name,
                "expect": {
                    "json": blob.decode("utf-8"),
                    "dict": parsed,
                    # Floats survive the json round-trip exactly (repr law), so
                    # dict-vs-bytes equality is a lossless supplementary pin.
                    "dict_matches_json": wire_report_to_dict(report) == parsed,
                },
            }
        )

    parses: list[dict[str, Any]] = []
    for name, payload in parse_payload_cases():
        case: dict[str, Any] = {"name": name}
        try:
            manifest = manifest_from_json(payload)
        except Exception as exc:  # capture the VERBATIM law, whatever it
            # raises (several junk shapes escape as raw AttributeError /
            # IndexError, NOT CascadeWireError — that IS the law; the frozen
            # fixture enumerates ONLY Exception subclasses, so BaseException
            # would over-catch — KeyboardInterrupt/SystemExit and a pyo3
            # PanicException must propagate loudly, never be recorded).
            case["expect"] = {"error_type": type(exc).__name__, "error_str": str(exc)}
        else:
            case["expect"] = {"json": manifest_to_json(manifest).decode("utf-8")}
        parses.append(case)

    # Math trio: floats frozen as repr strings (nan/inf/-0.0 are not JSON-native).
    finite = [{"name": name, "expect_repr": repr(_finite(value))} for name, value in finite_cases()]
    returns = [
        {"name": name, "expect_reprs": [repr(r) for r in _per_bar_returns(equity, start)]}
        for name, equity, start in per_bar_returns_cases()
    ]
    drawdowns = [
        {"name": name, "expect_repr": repr(_max_drawdown_return_units(series))}
        for name, series in max_drawdown_cases()
    ]
    return {
        "manifests": manifests,
        "reports": reports,
        "parses": parses,
        "finite": finite,
        "per_bar_returns": returns,
        "max_drawdown": drawdowns,
    }


def capture_top100_artifact() -> dict[str, Any]:
    """Freeze the top100_artifact builder/writer/reader laws (slice 2b)."""
    from brutex.engine.top100_artifact import (
        DEFAULT_TOP100_PRESET,
        ENTRY_REQUIRED_KEYS,
        GATE_MAX_CONSECUTIVE_LOSSES,
        GATE_MAX_DRAWDOWN_PCT,
        GATE_MIN_PROFIT_FACTOR,
        GATE_MIN_TRADES,
        GATE_MIN_WIN_LOSS_RATIO,
        GATE_MIN_WIN_RATE,
        TOP100_FILENAME,
        TOP100_LIMIT,
        TOP100_PRESETS,
        TOP100_SCHEMA_VERSION,
        TOP100_STAGE,
        TOP100_STAMP_FILENAME,
        passes_hard_gates,
        rank_candidates,
        resolve_preset,
    )
    from structlog.testing import capture_logs

    from tests.golden._top100_artifact_parity_corpus import (
        build_cases,
        constructor_cases,
        gate_cases,
        load_scenarios,
        preset_resolution_cases,
        rank_cases,
        run_build_case,
        run_load_scenario,
        run_write_roundtrip,
        write_roundtrip_cases,
    )

    constants = {
        "schema_version": TOP100_SCHEMA_VERSION,
        "stage": TOP100_STAGE,
        "filename": TOP100_FILENAME,
        "stamp_filename": TOP100_STAMP_FILENAME,
        "limit": TOP100_LIMIT,
        "default_preset": DEFAULT_TOP100_PRESET,
        "presets": list(TOP100_PRESETS),
        "entry_required_keys": sorted(ENTRY_REQUIRED_KEYS),
        "gate_min_trades": GATE_MIN_TRADES,
        "gate_min_profit_factor": GATE_MIN_PROFIT_FACTOR,
        "gate_max_drawdown_pct": GATE_MAX_DRAWDOWN_PCT,
        "gate_min_win_rate": GATE_MIN_WIN_RATE,
        "gate_min_win_loss_ratio": GATE_MIN_WIN_LOSS_RATIO,
        "gate_max_consecutive_losses": GATE_MAX_CONSECUTIVE_LOSSES,
    }

    gates = [
        {"name": name, "expect": passes_hard_gates(candidate.row, candidate.agg)}
        for name, candidate in gate_cases()
    ]

    presets: list[dict[str, Any]] = []
    for name, token in preset_resolution_cases():
        with capture_logs() as logs:
            resolved = resolve_preset(token)
        warns = [e for e in logs if e.get("event") == "top100.unknown_preset"]
        presets.append(
            {
                "name": name,
                "expect": {
                    "resolved": resolved,
                    "warned": bool(warns),
                    "requested": warns[0]["requested"] if warns else None,
                },
            }
        )

    ranks = [
        {
            "name": name,
            "expect": [c.row.run_id for c in rank_candidates(pool, preset=preset)],
        }
        for name, preset, pool in rank_cases()
    ]

    builds = [{"name": name, "expect": run_build_case(case)} for name, case in build_cases()]

    constructors: list[dict[str, Any]] = []
    for name, thunk in constructor_cases():
        try:
            thunk()
        except Exception as exc:  # the frozen fixture enumerates ONLY
            # Top100Error — ordinary Exception subclasses; BaseException would
            # over-catch (KeyboardInterrupt/SystemExit and a pyo3
            # PanicException must propagate loudly, never be recorded).
            constructors.append(
                {
                    "name": name,
                    "expect": {"error_type": type(exc).__name__, "error_str": str(exc)},
                }
            )
        else:  # pragma: no cover - a passing thunk is a broken corpus
            raise AssertionError(f"constructor case {name} did not raise")

    writes = [
        {"name": name, "expect": run_write_roundtrip(build)}
        for name, build in write_roundtrip_cases()
    ]
    loads = [{"name": name, "expect": run_load_scenario(setup)} for name, setup in load_scenarios()]
    return {
        "constants": constants,
        "gates": gates,
        "preset_resolution": presets,
        "ranks": ranks,
        "builds": builds,
        "constructors": constructors,
        "write_roundtrip": writes,
        "load": loads,
    }


def capture_results_report() -> dict[str, Any]:
    """Freeze the results_report render-law family (slice 2c).

    Family (b) — the render laws — is frozen exhaustively; family (a) — the
    store-coupled I/O orchestration half, which stays shim-side Python in
    phase B — is pinned only through the few end-to-end ``show_report`` cases
    (its main protection is the UNMODIFIED pre-existing unit suite).
    """
    from brutex.engine.results_report import (
        _AUDIT_RELPATH,
        _QUARANTINE_DIRNAME,
        _QUARANTINE_LOG_PATH_CAP,
        DEFAULT_SORT_BY,
        SNAPSHOT_QUARANTINE_EVENT,
        SORT_ALIASES,
    )

    from tests.golden._results_report_parity_corpus import (
        format_conditions_cases,
        format_deflated_cases,
        leaderboard_cases,
        provisional_note_cases,
        run_format_conditions_case,
        run_format_deflated_case,
        run_leaderboard_case,
        run_provisional_note_case,
        run_show_report_case,
        run_sidecar_group_case,
        run_sidecar_render_case,
        run_sort_field_case,
        show_report_cases,
        sidecar_group_cases,
        sidecar_render_cases,
        sort_field_cases,
    )

    constants = {
        "sort_aliases": dict(sorted(SORT_ALIASES.items())),
        "default_sort_by": DEFAULT_SORT_BY,
        "quarantine_dirname": _QUARANTINE_DIRNAME,
        "audit_relpath": list(_AUDIT_RELPATH),
        "quarantine_log_path_cap": _QUARANTINE_LOG_PATH_CAP,
        "snapshot_quarantine_event": SNAPSHOT_QUARANTINE_EVENT,
    }
    return {
        "constants": constants,
        "sort_field": [
            {"name": name, "expect": run_sort_field_case(token)}
            for name, token in sort_field_cases()
        ],
        "format_deflated": [
            {"name": name, "expect": run_format_deflated_case(value)}
            for name, value in format_deflated_cases()
        ],
        "format_conditions": [
            {"name": name, "expect": run_format_conditions_case(value)}
            for name, value in format_conditions_cases()
        ],
        "leaderboard": [
            {"name": name, "expect": run_leaderboard_case(case)}
            for name, case in leaderboard_cases()
        ],
        "provisional_note": [
            {"name": name, "expect": run_provisional_note_case(report)}
            for name, report in provisional_note_cases()
        ],
        "sidecar_group": [
            {"name": name, "expect": run_sidecar_group_case(entry)}
            for name, entry in sidecar_group_cases()
        ],
        "sidecar_render": [
            {"name": name, "expect": run_sidecar_render_case(content)}
            for name, content in sidecar_render_cases()
        ],
        "show_report": [
            {"name": name, "expect": run_show_report_case(case)}
            for name, case in show_report_cases()
        ],
    }


def capture_twin_detect() -> dict[str, Any]:
    """Freeze the twin_detect law families (slice 2d)."""
    from brutex.engine.twin_detect import FINGERPRINT_VERSION_PREFIX, TWIN_JACCARD_THRESHOLD

    from tests.golden._twin_detect_parity_corpus import (
        candidate_cases,
        entry_set_cases,
        fingerprint_cases,
        group_cases,
        jaccard_cases,
        run_candidate_case,
        run_entry_set_case,
        run_fingerprint_case,
        run_group_case,
        run_jaccard_case,
    )

    constants = {
        "fingerprint_version_prefix": FINGERPRINT_VERSION_PREFIX,
        "twin_jaccard_threshold_repr": repr(TWIN_JACCARD_THRESHOLD),
    }
    return {
        "constants": constants,
        "fingerprint": [
            {"name": name, "expect": run_fingerprint_case(trades)}
            for name, trades in fingerprint_cases()
        ],
        "entry_set": [
            {"name": name, "expect": run_entry_set_case(trades)}
            for name, trades in entry_set_cases()
        ],
        "candidate": [
            {"name": name, "expect": run_candidate_case(thunk)} for name, thunk in candidate_cases()
        ],
        "jaccard": [
            {"name": name, "expect": run_jaccard_case(pair)} for name, pair in jaccard_cases()
        ],
        "group": [{"name": name, "expect": run_group_case(case)} for name, case in group_cases()],
    }


def capture_baseline() -> dict[str, Any]:
    """Freeze the baseline compute/artifact law families (slice 2d)."""
    from brutex.engine.baseline import _PAYLOAD_KEYS, _SESSION_FIELDS, BASELINE_SCHEMA_VERSION

    from tests.golden._baseline_parity_corpus import (
        compute_cases,
        filename_cases,
        load_cases,
        price_str_cases,
        run_compute_case,
        run_filename_case,
        run_load_case,
        run_price_str_case,
        run_series_ctor_case,
        run_write_case,
        series_ctor_cases,
        write_cases,
    )

    constants = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "payload_keys": sorted(_PAYLOAD_KEYS),
        "session_fields": _SESSION_FIELDS,
    }
    return {
        "constants": constants,
        "compute": [
            {"name": name, "expect": run_compute_case(case)} for name, case in compute_cases()
        ],
        "series_ctor": [
            {"name": name, "expect": run_series_ctor_case(thunk)}
            for name, thunk in series_ctor_cases()
        ],
        "filename": [
            {"name": name, "expect": run_filename_case(instrument)}
            for name, instrument in filename_cases()
        ],
        "price_str": [
            {"name": name, "expect": run_price_str_case(literal)}
            for name, literal in price_str_cases()
        ],
        "write": [{"name": name, "expect": run_write_case(thunk)} for name, thunk in write_cases()],
        "load": [{"name": name, "expect": run_load_case(raw)} for name, raw in load_cases()],
    }


def main() -> int:
    # PER-FILE refusal (slice 2a): each family whose fixture already exists is
    # skipped LOUDLY unless the regen env is set — so a later slice can add its
    # own capture file while the earlier slices' fixtures stay byte-frozen.
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    builders: dict[str, Callable[[], Any]] = {
        "cascade_join.json": capture_cascade_join,
        "minute_path.json": capture_minute_path,
        "combo_aggregates.json": capture_combo_aggregates,
        "cascade_wire.json": capture_cascade_wire,
        "top100_artifact.json": capture_top100_artifact,
        "results_report.json": capture_results_report,
        "twin_detect.json": capture_twin_detect,
        "baseline.json": capture_baseline,
    }
    regen = os.environ.get(_REGEN_ENV) == "1"
    wrote = 0
    for filename, build in builders.items():
        path = FIXTURE_DIR / filename
        if path.exists() and not regen:
            sys.stderr.write(
                f"REFUSED: fixture {path} already exists — the pre-conversion capture\n"
                f"is the oracle. Set {_REGEN_ENV}=1 only for a DELIBERATE, ledgered\n"
                "re-baseline (DEC-RUST-CASCADE-RESULTS-001).\n"
            )
            continue
        payload = build()
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        sys.stdout.write(f"wrote {path}\n")
        wrote += 1
    return 0 if wrote else 2


if __name__ == "__main__":
    raise SystemExit(main())
