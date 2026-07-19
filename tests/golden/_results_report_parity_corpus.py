"""Deterministic input corpus for the results_report parity goldens.

DEC-RUST-CASCADE-RESULTS-001 phase A (slice 2c): the T14 lane will convert the
``brutex.engine.results_report`` RENDER-LAW family to the ``rust/brutex-cascade``
crate under the compressed greenfield cycle (capture-first — this corpus is
frozen against the UNTOUCHED Python module BEFORE any conversion). This module
is the ONE builder of the parity corpus INPUTS *and* the shared scenario
RUNNERS — shared verbatim by

* the capture generator (``gen_cascade_rust_parity.py`` — run ONCE against the
  pre-conversion pure-Python implementations; fixture frozen), and
* the replay golden (``test_results_report_rust_parity_golden.py`` — drives the
  post-conversion Rust-backed API against the frozen fixture),

so the two sides can never drift on what was fed in.

SCOPE (the lead's binding scout verdict): the module splits into (a) a
store-coupled I/O orchestration half — ``discover_result_files`` /
``load_results_tree`` / ``_merge_files*`` / ``_quarantine_snapshot_mismatch`` /
``_recorded_snapshot_hash`` / ``_stage3_rel_paths`` — which STAYS shim-side
Python in phase B (it orchestrates the ``results_store`` public API + the
filesystem), and (b) the RENDER-LAW family — the sort vocabulary, the two
``_format_*`` helpers, ``render_leaderboard``, ``_provisional_note``,
``_render_sidecar_group``, ``render_cascade_sidecar``'s render law, and
``results_show_report``'s text-assembly law — the phase-B conversion target.
Family (b) is frozen EXHAUSTIVELY (byte-exact text laws); family (a) is pinned
only via a few end-to-end ``results_show_report`` cases (its main protection is
the pre-existing UNMODIFIED ``tests/unit/test_engine_results_report.py`` suite).

Every case is fully deterministic: fixed labels, fixed numbers, no RNG, no
wall clock, and NO temp path ever reaches a captured value — the sidecar +
show-report runners render under a throwaway temp root and NORMALIZE the root
out of the captured text (``str(base) → "<ROOT>"``, applied identically by
generator AND replay — the recorded normalization). Inputs are built in CODE
(never parsed from the fixture) because several hostile cases carry values
JSON cannot round-trip faithfully as INPUTS (a lone-surrogate instrument,
bool-vs-int version puns).

The consumed laws stay Python and are treated as INPUT builders, not targets:
``results_store.ResultRow`` construction (its 2dp price snap, ``-0.0 → +0.0``
metric canonicalization and the ``condition_names`` vocabulary decode are ITS
laws — the corpus feeds only constructible instances), ``ResultsStore``
flush/load/top_k (the e2e tree), and ``cascade_wire``'s manifest writer (the
provenance-attribution seam input).
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from brutex.engine.results_report import (
    _format_conditions,
    _format_deflated,
    _provisional_note,
    _render_sidecar_group,
    _resolve_sort_field,
    render_cascade_sidecar,
    render_leaderboard,
    results_show_report,
)
from brutex.engine.results_store import ResultRow, ResultsStore, ResultsStoreError

__all__ = [
    "LeaderboardCase",
    "ShowReportCase",
    "format_conditions_cases",
    "format_deflated_cases",
    "leaderboard_cases",
    "provisional_note_cases",
    "run_format_conditions_case",
    "run_format_deflated_case",
    "run_leaderboard_case",
    "run_provisional_note_case",
    "run_show_report_case",
    "run_sidecar_group_case",
    "run_sidecar_render_case",
    "run_sort_field_case",
    "show_report_cases",
    "sidecar_group_cases",
    "sidecar_render_cases",
    "sort_field_cases",
]

#: The one root-path normalization token (see the module docstring) — every
#: captured text that could carry the throwaway temp root carries this instead.
ROOT_TOKEN = "<ROOT>"

#: The full 60-bit mask (mirrors ``results_store.MASK_BITS_MAX``).
_MAX_MASK = (1 << 60) - 1


def _rid(seed: int) -> str:
    """A deterministic 64-hex run_id from a small integer seed."""
    return f"{seed:064x}"


def _row(seed: int, **overrides: Any) -> ResultRow:
    """A deterministic, constructible :class:`ResultRow`, overridable by keyword."""
    base: dict[str, Any] = {
        "run_id": _rid(seed),
        "instrument": "NIFTY",
        "tf": "5m",
        "direction": "bull",
        "mode": "A",
        "mask_bits": (1 << 0) | (1 << 30),
        "net_pnl_paisa": 1_000,
        "gross_profit_paisa": 2_000,
        "gross_loss_paisa": 500,
        "max_drawdown_paisa": 250,
        "expectancy_paisa": 25,
        "total_trades": 40,
        "winning_trades": 25,
        "losing_trades": 15,
        "sharpe_ratio": 1.5,
        "sortino_ratio": 2.0,
        "calmar_ratio": 1.25,
        "profit_factor": 1.8,
        "win_rate": 0.625,
        "max_drawdown_pct": 12.5,
        "avg_entry_price": Decimal("100.00"),
        "avg_exit_price": Decimal("101.00"),
        "deflated_sharpe": 0.97,
    }
    base.update(overrides)
    return ResultRow(**base)


# ──────────────────────────────────────────────────────────────────────────
# _resolve_sort_field — the CLI sort vocabulary
# ──────────────────────────────────────────────────────────────────────────


def sort_field_cases() -> list[tuple[str, str]]:
    """Every alias, every CANONICAL column (a quirk: NOT accepted), and junk."""
    aliases = ["sharpe", "sortino", "calmar", "pnl", "win", "trades", "pf"]
    canonical = [
        "sharpe_ratio",
        "sortino_ratio",
        "calmar_ratio",
        "net_pnl_paisa",
        "win_rate",
        "total_trades",
        "profit_factor",
    ]
    cases = [(f"alias_{token}", token) for token in aliases]
    cases += [(f"canonical_rejected_{token}", token) for token in canonical]
    cases += [
        ("blank", ""),
        ("case_drift_Sharpe", "Sharpe"),
        ("case_drift_PNL", "PNL"),
        ("junk_token", "junk"),
    ]
    return cases


def run_sort_field_case(token: str) -> dict[str, Any]:
    """Resolve *token* — the mapped column, or the exact refusal message."""
    try:
        return {"field": _resolve_sort_field(token)}
    except ResultsStoreError as exc:
        return {"error_type": type(exc).__name__, "error_str": str(exc)}


# ──────────────────────────────────────────────────────────────────────────
# _format_deflated / _format_conditions — the two pure display helpers
# ──────────────────────────────────────────────────────────────────────────


def format_deflated_cases() -> list[tuple[str, float | None]]:
    """The ``+.4f`` display law over hostile floats (the sign of -0.0 included)."""
    return [
        ("none", None),
        ("zero", 0.0),
        ("negative_zero", -0.0),
        ("tenth", 0.1),
        ("tiny_underflows_display", 1e-7),
        ("typical", 0.97),
        ("negative_half_boundary", -1.2345),
        ("half_boundary", 2.00005),
        ("huge", 1e18),
        ("negative_huge", -1e18),
    ]


def run_format_deflated_case(value: float | None) -> str:
    return _format_deflated(value)


def format_conditions_cases() -> list[tuple[str, str]]:
    """The empty→note law; every non-empty string passes through VERBATIM."""
    return [
        ("empty", ""),
        ("single", "bar_bullish"),
        ("joined", "close_above_ema20 AND bar_bullish"),
        ("unicode_passthrough", "π≥3 AND ☂-condition"),
        ("whitespace_only_is_truthy", " "),
    ]


def run_format_conditions_case(value: str) -> str:
    return _format_conditions(value)


# ──────────────────────────────────────────────────────────────────────────
# render_leaderboard — the winners-table byte law
# ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LeaderboardCase:
    """One render_leaderboard invocation — rows in FIXED order + optional origins."""

    rows: tuple[ResultRow, ...]
    origins: dict[str, str] | None = None


def leaderboard_cases() -> list[tuple[str, LeaderboardCase]]:
    return [
        ("empty", LeaderboardCase(rows=())),
        ("single_no_origins", LeaderboardCase(rows=(_row(1),))),
        (
            "multi_with_origins",
            LeaderboardCase(
                rows=(_row(1), _row(2, sharpe_ratio=0.75), _row(3, sharpe_ratio=0.5)),
                origins={_rid(1): "grid", _rid(2): "stage3"},  # rid(3) absent → src=?
            ),
        ),
        (
            "empty_origins_is_not_none",
            LeaderboardCase(rows=(_row(1),), origins={}),  # every row src=?
        ),
        ("dsr_none_dash", LeaderboardCase(rows=(_row(1, deflated_sharpe=None),))),
        (
            "money_edges",
            LeaderboardCase(
                rows=(
                    _row(1, net_pnl_paisa=-1),
                    _row(2, net_pnl_paisa=0),
                    _row(3, net_pnl_paisa=5),
                    _row(4, net_pnl_paisa=123_456_789),
                    _row(5, net_pnl_paisa=-12_345_678_901),
                    # Beyond 2**53 — exact Decimal grouping, never f64.
                    _row(6, net_pnl_paisa=9_007_199_254_740_993),
                    # 2**62 paisa — deep int64 range stays exact.
                    _row(7, net_pnl_paisa=4_611_686_018_427_387_904),
                ),
            ),
        ),
        (
            "metric_format_edges",
            LeaderboardCase(
                rows=(
                    _row(1, win_rate=0.0, sharpe_ratio=0.0, deflated_sharpe=1e-7),
                    # ResultRow canonicalizes -0.0 → +0.0 BEFORE the render — the
                    # frozen line reads sharpe=+0.000 (the row-law seam pinned).
                    _row(2, win_rate=1.0, sharpe_ratio=-0.0, deflated_sharpe=-0.0),
                    _row(3, win_rate=0.335, sharpe_ratio=-1.2345),
                    _row(4, win_rate=0.99999, sharpe_ratio=2.00005, deflated_sharpe=None),
                ),
            ),
        ),
        (
            "mask_zero_no_conditions",
            LeaderboardCase(rows=(_row(1, mask_bits=0),)),
        ),
        (
            "mask_full_60bit_decode",
            LeaderboardCase(rows=(_row(1, mask_bits=_MAX_MASK),)),
        ),
        (
            "unicode_instrument_passthrough",
            LeaderboardCase(
                rows=(_row(1, instrument="निफ्टी☂", tf="५m", direction="तेजी"),),
            ),
        ),
        (
            "lone_surrogate_instrument",
            LeaderboardCase(rows=(_row(1, instrument="BAD-\ud800-SURROGATE"),)),
        ),
    ]


def run_leaderboard_case(case: LeaderboardCase) -> str:
    return render_leaderboard(case.rows, origins=case.origins)


# ──────────────────────────────────────────────────────────────────────────
# _provisional_note — the counted pool-N pass label
# ──────────────────────────────────────────────────────────────────────────


def _verdict(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"candidate_id": "c" * 64}
    base.update(fields)
    return base


def provisional_note_cases() -> list[tuple[str, dict[str, object]]]:
    return [
        ("verdicts_absent", {}),
        ("verdicts_not_a_list", {"verdicts": {"oops": 1}}),
        ("verdicts_string_not_a_list", {"verdicts": "provisional"}),
        ("verdicts_empty_list", {"verdicts": []}),
        ("no_verdict_stamped", {"verdicts": [_verdict(passed=True)]}),
        ("non_dict_entries_not_stamped", {"verdicts": [1, "x", None]}),
        (
            "one_provisional_pass",
            {"verdicts": [_verdict(passed=True, provisional=True), _verdict(passed=False)]},
        ),
        (
            "provisional_on_rejection_not_counted",
            {"verdicts": [_verdict(passed=False, provisional=True)]},
        ),
        (
            "provisional_int_1_is_not_True",
            # `v.get("provisional") is True` — identity, so int 1 does NOT count
            # (but the key IS stamped → the n==0 arm, not the unstamped arm).
            {"verdicts": [_verdict(passed=True, provisional=1)]},
        ),
        (
            "passed_int_1_is_not_True",
            {"verdicts": [_verdict(passed=1, provisional=True)]},
        ),
        (
            "two_counted",
            {
                "verdicts": [
                    _verdict(passed=True, provisional=True),
                    _verdict(passed=True, provisional=True),
                    _verdict(passed=True, provisional=False),
                ]
            },
        ),
    ]


def run_provisional_note_case(report: dict[str, object]) -> str:
    return _provisional_note(report)


# ──────────────────────────────────────────────────────────────────────────
# _render_sidecar_group — one group line, or the ValueError refusal
# ──────────────────────────────────────────────────────────────────────────


def _group(
    pool_pbo: object = 0.25,
    *,
    omit_pbo: bool = False,
    dsr: tuple[object, object] | None = ("leaderboard", 12),
    omit_dsr_trials: bool = False,
    verdicts: list[Any] | None = None,
    instrument: object = "NIFTY",
    tf: object = "1minute",
    omit_labels: bool = False,
    omit_counts: bool = False,
) -> dict[str, Any]:
    report: dict[str, Any] = {}
    if not omit_counts:
        report["n_input"] = 3
        report["n_survived"] = 2
    if not omit_pbo:
        report["pool_pbo"] = pool_pbo
    if dsr is not None:
        report["dsr_n_source"] = dsr[0]
        if not omit_dsr_trials:
            report["dsr_n_trials"] = dsr[1]
    if verdicts is not None:
        report["verdicts"] = verdicts
    entry: dict[str, Any] = {"report": report}
    if not omit_labels:
        entry["instrument"] = instrument
        entry["tf"] = tf
    return entry


def sidecar_group_cases() -> list[tuple[str, object]]:
    return [
        ("entry_not_object_int", 42),
        ("entry_not_object_list", [1, 2]),
        ("report_absent", {"instrument": "NIFTY", "tf": "1minute"}),
        ("report_not_object", {"instrument": "NIFTY", "tf": "1minute", "report": 7}),
        ("full_leaderboard_dsr", _group(0.6571428571428571)),
        ("labels_missing_question_marks", _group(0.25, omit_labels=True)),
        ("counts_missing_question_marks", _group(0.25, omit_counts=True)),
        ("non_string_labels_echoed", _group(0.25, instrument=5, tf=None)),
        ("dsr_stamp_absent_pre_adr0008", _group(0.25, dsr=None)),
        ("dsr_pool_fallback", _group(0.25, dsr=("pool", 3))),
        ("dsr_junk_source_echoed", _group(0.25, dsr=("weird", 9))),
        ("dsr_trials_missing_question_mark", _group(0.25, omit_dsr_trials=True)),
        ("pbo_absent_pre_sidecar", _group(omit_pbo=True)),
        ("pbo_null_skipped", _group(None)),
        ("pbo_int_coerced", _group(1)),
        ("pbo_bool_true_coerces_to_1", _group(True)),
        ("pbo_numeric_string_coerced", _group("0.25")),
        ("pbo_junk_string_raises", _group("junk")),
        ("pbo_full_precision_display_rounded", _group(0.6571428571428571, dsr=("pool", 3))),
        (
            "provisional_counted_in_line",
            _group(
                0.25,
                dsr=("pool", 3),
                verdicts=[_verdict(passed=True, provisional=True), _verdict(passed=False)],
            ),
        ),
        (
            "provisional_zero_flagged_no_label",
            _group(0.25, verdicts=[_verdict(passed=True, provisional=False)]),
        ),
    ]


def run_sidecar_group_case(entry: object) -> dict[str, Any]:
    try:
        return {"text": _render_sidecar_group(entry)}
    except ValueError as exc:
        return {"error_type": type(exc).__name__, "error_str": str(exc)}


# ──────────────────────────────────────────────────────────────────────────
# render_cascade_sidecar — the whole-section law over a real sidecar file
# ──────────────────────────────────────────────────────────────────────────


def _sidecar_json(groups: list[dict[str, Any]], *, version: object = 1) -> str:
    return json.dumps({"version": version, "groups": groups})


def sidecar_render_cases() -> list[tuple[str, str | None]]:
    """(name, file content) — ``None`` = no sidecar file at all."""
    return [
        ("absent_file", None),
        (
            "two_groups",
            _sidecar_json(
                [_group(0.6571428571428571), _group(None, dsr=("pool", 3), instrument="BANKNIFTY")]
            ),
        ),
        ("empty_groups_placeholder", _sidecar_json([])),
        ("corrupt_json", "{not json"),
        ("payload_not_object", "[1, 2]"),
        ("groups_not_a_list", '{"version": 1, "groups": {"oops": 1}}'),
        ("version_99", _sidecar_json([_group(0.5)], version=99)),
        # bool==int PUN: version=true LOADS (`True != 1` is False).
        ("version_true_bool_pun_loads", _sidecar_json([_group(0.5)], version=True)),
        # "1" != 1 — the repr in the refusal is the frozen law.
        ("version_string_1_refused_repr", _sidecar_json([_group(0.5)], version="1")),
        ("version_key_absent_none_repr", '{"groups": []}'),
        ("group_entry_not_object", '{"version": 1, "groups": [42]}'),
        ("group_float_coercion_valueerror", _sidecar_json([_group("junk")])),
    ]


def _normalized(text: str, base: Path) -> str:
    """The recorded path normalization — ``str(base)`` → :data:`ROOT_TOKEN`."""
    return text.replace(str(base), ROOT_TOKEN)


def run_sidecar_render_case(content: str | None) -> str:
    """Render under a throwaway root; the captured text is path-NORMALIZED."""
    with tempfile.TemporaryDirectory(prefix="bx-results-report-parity-") as tmp:
        root = Path(tmp)
        if content is not None:
            sidecar = root / "cascade" / "cascade-report.json"
            sidecar.parent.mkdir(parents=True)
            sidecar.write_text(content, encoding="utf-8")
        return _normalized(render_cascade_sidecar(root), root)


# ──────────────────────────────────────────────────────────────────────────
# results_show_report — the end-to-end text-assembly law over a REAL tree
# ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ShowReportCase:
    """One results_show_report invocation over a freshly-built temp tree."""

    tree: str = "standard"  # "standard" | "empty" | "missing"
    top: int = 10
    sort_by: str | None = None  # None → the DEFAULT_SORT_BY default argument
    sidecar: str | None = None
    manifest: str | None = None  # None | "stage3" | "disabled" | "pre_provenance"


#: The rel-path of the standard tree's first file — the stage3 manifest cell
#: attributes exactly this file's rows (the family (a)→(b) origins seam).
_STAGE3_REL = "NIFTY/5m/bull_k2_aaaa.parquet"


def _write_standard_tree(root: Path) -> None:
    """A 3-file / 5-row tree with sharpe AND trades ties (run_id tie-break)."""
    s1 = ResultsStore(path=root / "NIFTY" / "5m" / "bull_k2_aaaa.parquet")
    s1.append(_row(1, sharpe_ratio=3.25, net_pnl_paisa=9_000))
    s1.append(
        _row(
            2,
            sharpe_ratio=0.75,
            net_pnl_paisa=500,
            total_trades=12,
            winning_trades=7,
            losing_trades=5,
            deflated_sharpe=None,
        )
    )
    s1.flush()
    s2 = ResultsStore(path=root / "BANKNIFTY" / "15m" / "bull_k1_bbbb.parquet")
    s2.append(
        _row(
            3,
            instrument="BANKNIFTY",
            tf="15m",
            sharpe_ratio=1.5,
            net_pnl_paisa=4_000,
            total_trades=55,
            deflated_sharpe=0.25,
        )
    )
    s2.flush()
    s3 = ResultsStore(path=root / "SENSEX" / "1h" / "bear_k1_cccc.parquet")
    s3.append(
        _row(
            4,
            instrument="SENSEX",
            tf="1h",
            direction="bear",
            sharpe_ratio=1.5,
            net_pnl_paisa=-250,
            total_trades=55,
            deflated_sharpe=-0.5,
        )
    )
    s3.append(
        _row(
            5,
            instrument="SENSEX",
            tf="1h",
            direction="bear",
            mask_bits=0,
            sharpe_ratio=2.0,
            net_pnl_paisa=9_007_199_254_740_993,
            total_trades=7,
            winning_trades=7,
            losing_trades=0,
        )
    )
    s3.flush()


def _write_manifest_variant(root: Path, variant: str) -> None:
    """Mirror the unit suite's manifest builder (the provenance seam input)."""
    from brutex.engine.cascade_wire import (
        BarsContext,
        CascadeManifest,
        Stage3CellRecord,
        Stage3ManifestRecord,
        cascade_config_for_sweep,
        write_manifest,
    )

    def cell(rel_path: str | None) -> Stage3CellRecord:
        return Stage3CellRecord(
            instrument="NIFTY",
            tf_label="5m",
            direction_label="bull",
            k=1,
            sl_paisa=80,
            tp_paisa=50,
            proximity_bps=None,
            rel_path=rel_path,
        )

    if variant == "stage3":
        stage3 = Stage3ManifestRecord(
            enabled=True, train_snapshot_hash="ab" * 32, refined_cells=(cell(_STAGE3_REL),)
        )
    elif variant == "disabled":
        stage3 = Stage3ManifestRecord(enabled=False, train_snapshot_hash=None, refined_cells=())
    elif variant == "pre_provenance":
        stage3 = Stage3ManifestRecord(
            enabled=True, train_snapshot_hash="ab" * 32, refined_cells=(cell(None),)
        )
    else:  # pragma: no cover - a broken corpus spec
        raise AssertionError(f"unknown manifest variant {variant!r}")
    write_manifest(
        root,
        CascadeManifest(
            version=1,
            sweep_id="a" * 64,
            cascade_config=cascade_config_for_sweep("a" * 64),
            bars_context=BarsContext(
                exchange="NSE", segment="INDEX", start="2021-01-01", end="2021-02-01"
            ),
            specs=(),
            stage3=stage3,
        ),
    )


def show_report_cases() -> list[tuple[str, ShowReportCase]]:
    return [
        ("basic_top10_default_sort", ShowReportCase()),
        ("top2_pnl", ShowReportCase(top=2, sort_by="pnl")),
        ("trades_tie_run_id_asc", ShowReportCase(top=5, sort_by="trades")),
        ("empty_tree", ShowReportCase(tree="empty")),
        ("missing_root_refused", ShowReportCase(tree="missing")),
        ("top_zero_refused", ShowReportCase(top=0)),
        ("unknown_sort_refused", ShowReportCase(sort_by="Sharpe")),
        (
            "with_sidecar_section",
            ShowReportCase(top=3, sidecar=_sidecar_json([_group(0.25, dsr=("leaderboard", 841))])),
        ),
        (
            "with_unreadable_sidecar_still_renders",
            ShowReportCase(top=1, sidecar="{not json"),
        ),
        ("manifest_stage3_markers", ShowReportCase(manifest="stage3")),
        ("manifest_disabled_all_grid", ShowReportCase(manifest="disabled")),
        ("manifest_pre_provenance_disclaimer", ShowReportCase(manifest="pre_provenance")),
    ]


def run_show_report_case(case: ShowReportCase) -> dict[str, Any]:
    """Build the tree fresh, run the report, capture NORMALIZED text or refusal."""
    with tempfile.TemporaryDirectory(prefix="bx-results-report-parity-") as tmp:
        base = Path(tmp)
        if case.tree == "standard":
            root = base
            _write_standard_tree(root)
        elif case.tree == "empty":
            root = base
        elif case.tree == "missing":
            root = base / "nope"
        else:  # pragma: no cover - a broken corpus spec
            raise AssertionError(f"unknown tree {case.tree!r}")
        if case.sidecar is not None:
            sidecar = base / "cascade" / "cascade-report.json"
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            sidecar.write_text(case.sidecar, encoding="utf-8")
        if case.manifest is not None:
            _write_manifest_variant(base, case.manifest)
        kwargs: dict[str, Any] = {"top": case.top}
        if case.sort_by is not None:
            kwargs["sort_by"] = case.sort_by
        try:
            text = results_show_report(root, **kwargs)
        except ResultsStoreError as exc:
            return {
                "error_type": type(exc).__name__,
                "error_str": _normalized(str(exc), base),
            }
        return {"text": _normalized(text, base)}
