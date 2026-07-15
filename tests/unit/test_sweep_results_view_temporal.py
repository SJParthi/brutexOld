"""Unit tests — /results temporal + cascade-verdict projection (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001).

The additive ``_project_row`` keys (temporal display + cascade join), the
``load_sweep_scan`` verdict/envelope fold, the frozen additive-key-NAME set
(the #1539 pattern — a rename breaks a pinned name, never silently drifts the
wire), the payload envelope keys, and the serve-diagnostic ≡ rank-verdict
equivalence on identical stored inputs. Stores are written through the REAL
``ResultsStore`` writer; the cascade report through the REAL ``write_report``.
"""

# The projection tests read module-private helpers deliberately.
# pyright: reportPrivateUsage=none
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars", reason="the reader loads per-tuple Parquet stores via ResultsStore")

import brutex.engine.cascade_wire as cw
from brutex.dashboard.sweep_results_view import (
    SweepScan,
    _verdict_marker_row_keys,
    _temporal_row_keys,
    build_sweep_leaderboard,
    load_sweep_scan,
    store_error_payload,
)
from brutex.engine.cascade_wire import VerdictMarker
from brutex.engine.period_stats import encode_judged_years, encode_period_stats
from brutex.engine.results_store import ResultRow, ResultsStore, ResultsStoreError
from brutex.metrics.cascade import CandidateVerdict, CascadeReport, FilterStage
from brutex.ranking.temporal import (
    TemporalRobustnessConfig,
    judged_years,
    temporal_gate_passes,
    triple_from_year_pnls,
)

#: The FROZEN additive per-row key-name set (the #1539 pattern): OUR keys only —
#: none of the audit-1 session's (`gate_reasons`/`gates_passed` are theirs).
TEMPORAL_ROW_KEYS = frozenset(
    {
        "stats_by_year",
        "temporal_judged_years",
        "max_year_share",
        "max_quarter_share",
        "year_gate_passed",
        "year_gate_abstained",
        "year_gate_reasons",
    }
)
CASCADE_ROW_KEYS = frozenset(
    {
        "cascade_verdict",
        "cascade_rejected_at",
        "cascade_provisional",
        # DEC-RESULTS-TRUST-HONESTY-001 (audit BLOCKER 5): the corrected
        # cascade DSR + its run-level deflation stamps.
        "cascade_dsr",
        "cascade_dsr_n_trials",
        "cascade_dsr_n_source",
    }
)


def _run_id(seed: int) -> str:
    return f"{seed:064x}"


def _row(
    *,
    seed: int,
    temporal: bool = False,
    losing_only: bool = False,
) -> ResultRow:
    """A valid :class:`ResultRow`; ``temporal=True`` adds CONSISTENT stored columns.

    The populated shape: 2021 profitable (2 trades, +500), 2022 losing
    (1 trade, −200) — judged years [2021, 2022]. ``losing_only=True`` flips
    every pnl negative (the S⁺=0 / no-positive-quarter diagnostic arm).
    """
    sign = -1 if losing_only else 1
    kwargs: dict[str, str | None] = {}
    if temporal:
        months = {
            "2021-01": (2, sign * 500, 0 if losing_only else 1, 100),
            "2022-07": (1, -200, 0, 200),
        }
        quarters = {
            "2021Q1": (2, sign * 500, 0 if losing_only else 1, 100),
            "2022Q3": (1, -200, 0, 200),
        }
        years = {
            "2021": (2, sign * 500, 0 if losing_only else 1, 100),
            "2022": (1, -200, 0, 200),
        }
        kwargs = {
            "stats_by_year_json": encode_period_stats(years),
            "stats_by_quarter_json": encode_period_stats(quarters),
            "stats_by_month_json": encode_period_stats(months),
            "temporal_judged_years_json": encode_judged_years([2021, 2022]),
        }
    net = sign * 500 - 200
    wins = 0 if losing_only else 1
    return ResultRow(
        run_id=_run_id(seed),
        instrument="NIFTY",
        tf="5m",
        direction="bull",
        mode="A",
        mask_bits=1,
        net_pnl_paisa=net if temporal else 100_00,
        gross_profit_paisa=0 if losing_only else 500,
        gross_loss_paisa=(700 if losing_only else 200) if temporal else 100_00,
        max_drawdown_paisa=50_00,
        expectancy_paisa=10_00,
        total_trades=3 if temporal else 10,
        winning_trades=wins if temporal else 6,
        losing_trades=(3 - wins) if temporal else 4,
        sharpe_ratio=1.0,
        sortino_ratio=1.2,
        calmar_ratio=0.9,
        profit_factor=2.0,
        win_rate=0.6,
        max_drawdown_pct=0.05,
        avg_entry_price=Decimal("100.00"),
        avg_exit_price=Decimal("101.00"),
        **kwargs,  # type: ignore[arg-type]
    )


def _write_store(lake_root: Path, rows: list[ResultRow], *, name: str = "a" * 16) -> Path:
    dest = lake_root / "sweeps" / "NIFTY" / "5m" / f"bull_k1_{name}.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    store = ResultsStore(path=dest)
    store.extend(rows)
    store.flush()
    return dest


def _write_report(lake_root: Path, verdicts: tuple[CandidateVerdict, ...]) -> Path:
    survivors = tuple(v.candidate_id for v in verdicts if v.passed)
    group = CascadeReport(
        verdicts=verdicts,
        survivor_ids=survivors,
        n_input=len(verdicts),
        n_survived=len(survivors),
        stage_survivors={},
        pool_pbo=None,
    )
    report = cw.CascadeWireReport(sweep_id="a" * 64, groups=(("NIFTY", "5m", group),))
    return cw.write_report(lake_root / "sweeps", report)


class TestTemporalRowKeys:
    def test_frozen_key_name_set(self) -> None:
        """The #1539 pattern: the additive key NAMES are pinned — a rename here
        must consciously update this frozen set (and results.html).
        """
        assert frozenset(_temporal_row_keys(_row(seed=1))) == TEMPORAL_ROW_KEYS
        assert (
            frozenset(
                _verdict_marker_row_keys(
                    VerdictMarker(passed=True, rejected_at=None, provisional=False)
                )
            )
            == CASCADE_ROW_KEYS
        )
        # Emit-on-present: the None marker emits NOTHING (club S2 fold).
        assert _verdict_marker_row_keys(None) == {}

    def test_pre_extension_row_is_all_none(self) -> None:
        keys = _temporal_row_keys(_row(seed=1))
        assert all(v is None for v in keys.values())

    def test_populated_row_serves_stored_cells(self) -> None:
        keys = _temporal_row_keys(_row(seed=2, temporal=True))
        assert keys["stats_by_year"] == {
            "2021": [2, 500, 1, 100],
            "2022": [1, -200, 0, 200],
        }
        assert keys["temporal_judged_years"] == ["2021", "2022"]
        # One profitable year of two judged: share = 500/500 = 1.0 (only 2021 is positive).
        assert keys["max_year_share"] == 1.0
        assert keys["max_quarter_share"] == 1.0
        # 1 of 2 judged years profitable — P1 (≥ 0.5) passes at exactly the boundary;
        # P2 fails (share 1.0 > 0.6): verdict False with the share reason.
        assert keys["year_gate_passed"] is False
        assert keys["year_gate_abstained"] is False
        assert keys["year_gate_reasons"] == ["year_gate_single_year_share"]

    def test_all_losing_row_shares_are_none(self) -> None:
        """S⁺ = 0 (no positive year/quarter mass) → both shares honest ``None``."""
        keys = _temporal_row_keys(_row(seed=3, temporal=True, losing_only=True))
        assert keys["max_year_share"] is None
        assert keys["max_quarter_share"] is None
        assert keys["year_gate_passed"] is False
        assert keys["year_gate_reasons"] == ["year_gate_profitable_years"]

    def test_serve_diagnostic_equals_rank_verdict_on_identical_inputs(self) -> None:
        """The serve-time diagnostic and the rank-time predicate are the SAME
        core: identical (pnl_by_year, judged) inputs yield the identical verdict.
        """
        cfg = TemporalRobustnessConfig()
        pnl_by_year = {2021: 500, 2022: -200}
        judged = judged_years({2021: 200, 2022: 200}, cfg)
        triple = triple_from_year_pnls(pnl_by_year, judged)
        rank_verdict = temporal_gate_passes(triple, n_judged_years=len(judged), cfg=cfg)
        keys = _temporal_row_keys(_row(seed=4, temporal=True))
        assert keys["year_gate_passed"] == rank_verdict


class TestCascadeRowKeys:
    def test_not_judged_emits_nothing(self) -> None:
        # Club S2 fold: emit-on-present conformance to the landed
        # DEC-RESULTS-CASCADE-VERDICT-JOIN-001 no-report byte-identity law —
        # an unjudged/no-report row carries NO marker keys (absence renders
        # "—" client-side, never "passed").
        assert _verdict_marker_row_keys(None) == {}

    def test_passed_and_provisional(self) -> None:
        passed = _verdict_marker_row_keys(VerdictMarker(passed=True, rejected_at=None, provisional=False))
        assert passed["cascade_verdict"] == "passed"
        assert passed["cascade_provisional"] is False
        provisional = _verdict_marker_row_keys(
            VerdictMarker(passed=True, rejected_at=None, provisional=True)
        )
        assert provisional["cascade_verdict"] == "passed"
        assert provisional["cascade_provisional"] is True

    def test_rejected_carries_the_stage_token(self) -> None:
        keys = _verdict_marker_row_keys(
            VerdictMarker(
                passed=False, rejected_at=FilterStage.WALK_FORWARD.value, provisional=False
            )
        )
        assert keys["cascade_verdict"] == "rejected"
        assert keys["cascade_rejected_at"] == "walk_forward"

    def test_ambiguous_is_a_loud_question_never_a_coin_flip(self) -> None:
        keys = _verdict_marker_row_keys(cw.AMBIGUOUS_MARKER)
        assert keys == {
            "cascade_verdict": "ambiguous",
            "cascade_rejected_at": None,
            "cascade_provisional": None,
            # A conflicting figure is never coin-flip-served (BLOCKER 5).
            "cascade_dsr": None,
            "cascade_dsr_n_trials": None,
            "cascade_dsr_n_source": None,
        }

    def test_dsr_keys_ride_the_marker(self) -> None:
        """DEC-RESULTS-TRUST-HONESTY-001: the corrected DSR + its run-level
        N/source stamps pass through verbatim (full precision, never rounded);
        an unstamped (pre-change) marker reads honest None, never 0.
        """
        keys = _verdict_marker_row_keys(
            VerdictMarker(
                passed=True,
                rejected_at=None,
                provisional=False,
                dsr_corrected=0.0031218374618234,
                dsr_n_trials=8_400_000,
                dsr_n_source="leaderboard",
            )
        )
        assert keys["cascade_dsr"] == 0.0031218374618234
        assert keys["cascade_dsr_n_trials"] == 8_400_000
        assert keys["cascade_dsr_n_source"] == "leaderboard"
        bare = _verdict_marker_row_keys(VerdictMarker(passed=True, rejected_at=None, provisional=False))
        assert bare["cascade_dsr"] is None
        assert bare["cascade_dsr_n_trials"] is None
        assert bare["cascade_dsr_n_source"] is None


class TestScanVerdictJoin:
    def test_scan_loads_verdicts_and_report_facts(self, tmp_path: Path) -> None:
        _write_store(tmp_path, [_row(seed=1), _row(seed=2)])
        _write_report(
            tmp_path,
            (
                CandidateVerdict(
                    candidate_id=_run_id(1),
                    passed=True,
                    rejected_at=None,
                    detail="ok",
                    composite_score=0.5,
                ),
            ),
        )
        scan = load_sweep_scan(tmp_path)
        assert scan.verdicts[_run_id(1)].passed is True
        assert _run_id(2) not in scan.verdicts
        assert scan.cascade_report_present is True
        assert scan.cascade_report_groups == 1
        assert isinstance(scan.cascade_report_mtime_ns, int)

    def test_scan_without_report_is_honest_absent(self, tmp_path: Path) -> None:
        _write_store(tmp_path, [_row(seed=1)])
        scan = load_sweep_scan(tmp_path)
        assert scan.verdicts == {}
        assert scan.cascade_report_present is False
        assert scan.cascade_report_groups is None
        assert scan.cascade_report_mtime_ns is None

    def test_corrupt_report_is_present_with_unknown_groups(self, tmp_path: Path) -> None:
        """Present-but-corrupt: chips read ``—`` (empty map), the envelope says
        present with an honest ``groups: None`` — never a fabricated count.
        """
        _write_store(tmp_path, [_row(seed=1)])
        report = tmp_path / "sweeps" / "cascade" / "cascade-report.json"
        report.parent.mkdir(parents=True)
        report.write_bytes(b"{corrupt")
        scan = load_sweep_scan(tmp_path)
        assert scan.verdicts == {}
        assert scan.cascade_report_present is True
        assert scan.cascade_report_groups is None


class TestLeaderboardFold:
    def test_rows_carry_both_key_families_in_both_arms(self, tmp_path: Path) -> None:
        _write_store(tmp_path, [_row(seed=1, temporal=True), _row(seed=2)])
        _write_report(
            tmp_path,
            (
                CandidateVerdict(
                    candidate_id=_run_id(1),
                    passed=False,
                    rejected_at=FilterStage.LOCKBOX,
                    detail="lockbox fail",
                    composite_score=None,
                ),
            ),
        )
        board = build_sweep_leaderboard(tmp_path)
        by_id = {r["run_id"]: r for r in board.rows}
        populated, bare = by_id[_run_id(1)], by_id[_run_id(2)]
        assert frozenset(populated) >= TEMPORAL_ROW_KEYS | CASCADE_ROW_KEYS
        # Club S2 fold (emit-on-present): the report-UNJUDGED row carries the
        # temporal family but NO cascade marker keys — absence renders "—".
        assert frozenset(bare) >= TEMPORAL_ROW_KEYS
        assert not (frozenset(bare) & CASCADE_ROW_KEYS)
        assert populated["temporal_judged_years"] == ["2021", "2022"]
        assert populated["cascade_verdict"] == "rejected"
        assert populated["cascade_rejected_at"] == "lockbox"
        assert bare["stats_by_year"] is None
        assert "cascade_verdict" not in bare  # not judged — never "passed" (emit-on-present)

    def test_envelope_coverage_and_report_keys(self, tmp_path: Path) -> None:
        _write_store(tmp_path, [_row(seed=1, temporal=True), _row(seed=2)])
        payload = build_sweep_leaderboard(tmp_path).to_dict()
        assert payload["temporal_coverage"] == {"rows_with_data": 1, "rows_total": 2}
        assert payload["cascade_report"] == {"present": False, "groups": None, "mtime_ns": None}

    def test_coverage_counts_whole_scan_not_filtered_slice(self, tmp_path: Path) -> None:
        """The header chip answers for the whole tree — a filter that excludes
        the temporal row leaves the coverage figures unchanged.
        """
        _write_store(tmp_path, [_row(seed=1, temporal=True), _row(seed=2)])
        board = build_sweep_leaderboard(tmp_path, instrument="SENSEX")
        assert board.total_rows == 0
        assert board.temporal_rows_with_data == 1
        assert board.temporal_rows_total == 2

    def test_store_error_payload_carries_the_envelope_keys(self) -> None:
        payload = store_error_payload(ResultsStoreError("synthetic"))
        assert payload["temporal_coverage"] == {"rows_with_data": 0, "rows_total": 0}
        assert payload["cascade_report"] == {"present": False, "groups": None, "mtime_ns": None}

    def test_scan_defaults_are_backward_compatible(self) -> None:
        """A SweepScan built without the new fields (older call sites / tests)
        defaults to the honest empty join."""
        scan = SweepScan(
            rows=(),
            store_count=0,
            skipped=0,
            run_id_conflicts=0,
            eras={},
            instruments=(),
            timeframes=(),
            directions=(),
            snapshot_hashes=(),
        )
        assert scan.verdicts == {}
        assert scan.cascade_report_present is False
