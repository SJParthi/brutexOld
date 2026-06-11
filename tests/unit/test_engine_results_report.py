"""Unit tests for the sweep results read-back (`brutex.engine.results_report`).

100% line + branch coverage of the operator leaderboard read-back
(DEC-SWEEP-WINNERS-BY-NAME-001):

* :func:`discover_result_files` — recursive sorted glob, the not-a-directory raise,
  and the no-files-yet empty result;
* :func:`load_results_tree` — empty tree → empty store, and a multi-file merge;
* :func:`render_leaderboard` — the empty placeholder, the deflated ``None`` → ``—``
  and float branches, and the no-condition vs named branches;
* :func:`results_show_report` — the header, top-N truncation, alias sorting, the
  empty-store branch, and the ``top < 1`` / unknown-sort guards.

A fixture builds a real per-tuple Parquet tree with
:class:`~brutex.engine.results_store.ResultsStore` so the read path is exercised
end-to-end (write → discover → load → render).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from brutex.engine.results_report import (
    SORT_ALIASES,
    discover_result_files,
    load_results_tree,
    render_cascade_sidecar,
    render_leaderboard,
    results_show_report,
)
from brutex.engine.results_store import ResultRow, ResultsStore, ResultsStoreError


def _rid(seed: int) -> str:
    return f"{seed:064x}"


def _row(
    seed: int,
    *,
    instrument: str = "NIFTY",
    tf: str = "5m",
    mask_bits: int = (1 << 0) | (1 << 30),
    sharpe: float = 1.5,
    net_pnl_paisa: int = 1_000,
    deflated_sharpe: float | None = 0.97,
) -> ResultRow:
    return ResultRow(
        run_id=_rid(seed),
        instrument=instrument,
        tf=tf,
        direction="bull",
        mode="A",
        mask_bits=mask_bits,
        net_pnl_paisa=net_pnl_paisa,
        gross_profit_paisa=2_000,
        gross_loss_paisa=500,
        max_drawdown_paisa=250,
        expectancy_paisa=25,
        total_trades=40,
        winning_trades=25,
        losing_trades=15,
        sharpe_ratio=sharpe,
        sortino_ratio=2.0,
        calmar_ratio=1.25,
        profit_factor=1.8,
        win_rate=0.625,
        max_drawdown_pct=12.5,
        avg_entry_price=Decimal("100.00"),
        avg_exit_price=Decimal("101.00"),
        deflated_sharpe=deflated_sharpe,
    )


def _write_tree(root: Path) -> None:
    """Write a 2-file results tree mirroring <root>/<instrument>/<tf>/<file>.parquet."""
    s1 = ResultsStore(path=root / "NIFTY" / "5m" / "bull_k2_aaaa.parquet")
    s1.append(_row(1, sharpe=3.25, net_pnl_paisa=9_000))
    s1.append(_row(2, sharpe=0.75, net_pnl_paisa=500, deflated_sharpe=None))
    s1.flush()
    s2 = ResultsStore(path=root / "BANKNIFTY" / "15m" / "bull_k1_bbbb.parquet")
    s2.append(_row(3, instrument="BANKNIFTY", tf="15m", sharpe=1.5, net_pnl_paisa=4_000))
    s2.flush()


# ──────────────────────────────────────────────────────────────────────────
# discover_result_files
# ──────────────────────────────────────────────────────────────────────────


class TestDiscover:
    def test_missing_root_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ResultsStoreError, match="results root not found"):
            discover_result_files(tmp_path / "nope")

    def test_root_is_a_file_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "afile"
        f.write_text("x")
        with pytest.raises(ResultsStoreError, match="not a directory"):
            discover_result_files(f)

    def test_empty_root_returns_empty_list(self, tmp_path: Path) -> None:
        assert discover_result_files(tmp_path) == []

    def test_discovers_nested_files_sorted(self, tmp_path: Path) -> None:
        _write_tree(tmp_path)
        files = discover_result_files(tmp_path)
        assert files == sorted(files)
        assert len(files) == 2


# ──────────────────────────────────────────────────────────────────────────
# load_results_tree
# ──────────────────────────────────────────────────────────────────────────


class TestLoadTree:
    def test_empty_tree_is_empty_store(self, tmp_path: Path) -> None:
        store = load_results_tree(tmp_path)
        assert isinstance(store, ResultsStore)
        assert len(store) == 0

    def test_merges_all_files(self, tmp_path: Path) -> None:
        _write_tree(tmp_path)
        store = load_results_tree(tmp_path)
        assert len(store) == 3
        assert store.get(_rid(1)) is not None
        assert store.get(_rid(3)) is not None


# ──────────────────────────────────────────────────────────────────────────
# render_leaderboard
# ──────────────────────────────────────────────────────────────────────────


class TestRender:
    def test_empty_rows_placeholder(self) -> None:
        assert render_leaderboard([]) == "(no persisted results)"

    def test_named_conditions_and_deflated_value(self) -> None:
        out = render_leaderboard([_row(1, mask_bits=(1 << 0) | (1 << 23))])
        assert "#1  NIFTY 5m bull" in out
        assert "close_above_ema20 AND near_fib_618" in out
        # ADR-0008 condition 7: the row's deflated number is LABELLED with the
        # N it was deflated against (the per-tuple rank-time count) — the bare
        # ambiguous `dsr=` label is gone.
        assert "dsr@tupleN=+0.9700" in out
        assert " dsr=" not in out

    def test_no_per_row_gate_verdict_is_rendered(self) -> None:
        # DEC-STAGE3-WIRE-001 (review F6): a row cannot reveal whether it is a
        # gated Stage-2 finalist or an UNGATED Stage-3 refined trial, so the
        # old unconditional per-row `gate=PASS` claim was scoped out.
        out = render_leaderboard([_row(1, mask_bits=1)])
        assert "gate=" not in out

    def test_no_condition_mask_and_deflated_none(self) -> None:
        out = render_leaderboard([_row(1, mask_bits=0, deflated_sharpe=None)])
        assert "(no conditions — enters every bar)" in out
        assert "dsr@tupleN=—" in out


# ──────────────────────────────────────────────────────────────────────────
# results_show_report
# ──────────────────────────────────────────────────────────────────────────


class TestShowReport:
    def test_top_must_be_at_least_one(self, tmp_path: Path) -> None:
        with pytest.raises(ResultsStoreError, match="top must be >= 1"):
            results_show_report(tmp_path, top=0)

    def test_unknown_sort_key_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ResultsStoreError, match="unknown sort key"):
            results_show_report(tmp_path, top=5, sort_by="bogus")

    def test_empty_tree_reports_zero(self, tmp_path: Path) -> None:
        out = results_show_report(tmp_path, top=5)
        assert "files=0  finalists=0" in out
        assert "(no persisted results)" in out

    def test_ranked_by_sharpe_default(self, tmp_path: Path) -> None:
        _write_tree(tmp_path)
        out = results_show_report(tmp_path, top=10)
        assert "finalists=3" in out
        # sharpe order: 3.25 (rid1) > 1.5 (rid3) > 0.75 (rid2) → rid1 first.
        first_line = out.split("#1")[1].splitlines()[0]
        assert "NIFTY 5m bull" in first_line
        assert "showing top 3" in out

    def test_top_truncates(self, tmp_path: Path) -> None:
        _write_tree(tmp_path)
        out = results_show_report(tmp_path, top=1)
        assert "showing top 1" in out
        assert "#2" not in out

    def test_sort_by_pnl_alias(self, tmp_path: Path) -> None:
        _write_tree(tmp_path)
        out = results_show_report(tmp_path, top=10, sort_by="pnl")
        # pnl order: 9_000 (rid1) > 4_000 (rid3) > 500 (rid2).
        assert out.index("pnl=") > 0
        assert "sort=pnl" in out


def test_sort_aliases_resolve_to_real_columns(tmp_path: Path) -> None:
    # Every advertised alias must be a usable sort key end-to-end.
    _write_tree(tmp_path)
    for alias in SORT_ALIASES:
        out = results_show_report(tmp_path, top=10, sort_by=alias)
        assert f"sort={alias}" in out


# ──────────────────────────────────────────────────────────────────────────
# render_cascade_sidecar — the pool-PBO sidecar section (DEC-PBO-SIDECAR-001)
# ──────────────────────────────────────────────────────────────────────────


def _write_sidecar(root: Path, body: str) -> Path:
    path = root / "cascade" / "cascade-report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _sidecar_json(groups: list[dict[str, object]], *, version: int = 1) -> str:
    import json

    return json.dumps({"version": version, "sweep_id": "a" * 64, "groups": groups})


def _group(
    pbo: float | None,
    *,
    omit_key: bool = False,
    dsr: tuple[str, int] | None = None,
) -> dict[str, object]:
    report: dict[str, object] = {"n_input": 3, "n_survived": 2}
    if not omit_key:
        report["pool_pbo"] = pbo
    if dsr is not None:
        report["dsr_n_source"] = dsr[0]
        report["dsr_n_trials"] = dsr[1]
    return {"instrument": "NIFTY", "tf": "1minute", "report": report}


class TestCascadeSidecar:
    def test_absent_file_renders_empty(self, tmp_path: Path) -> None:
        assert render_cascade_sidecar(tmp_path) == ""

    def test_absent_file_show_report_is_byte_identical(self, tmp_path: Path) -> None:
        # The hostile C8 pin: with no sidecar, results_show_report's bytes are
        # EXACTLY the pre-sidecar render (header + leaderboard, nothing more).
        _write_tree(tmp_path)
        out = results_show_report(tmp_path, top=10)
        assert render_cascade_sidecar(tmp_path) == ""
        assert not out.endswith("\n")
        assert "Stage-4 cascade sidecar" not in out

    def test_measured_pool_pbo_renders_fixed_width(self, tmp_path: Path) -> None:
        _write_sidecar(tmp_path, _sidecar_json([_group(0.6571428571428571)]))
        out = render_cascade_sidecar(tmp_path)
        assert "Stage-4 cascade sidecar" in out
        assert "NIFTY 1minute  pool PBO=0.6571  in=3 survived=2" in out

    def test_dsr_leaderboard_source_renders_n(self, tmp_path: Path) -> None:
        # ADR-0008 condition 7: the cascade group line labels which trial
        # population filter #2 deflated against, echoing the frozen N.
        _write_sidecar(tmp_path, _sidecar_json([_group(0.25, dsr=("leaderboard", 841))]))
        out = render_cascade_sidecar(tmp_path)
        assert "dsr N=841 source=leaderboard" in out

    def test_dsr_pool_fallback_renders_loudly(self, tmp_path: Path) -> None:
        # The "pool" stamp can never masquerade as the corrected figure.
        _write_sidecar(tmp_path, _sidecar_json([_group(0.25, dsr=("pool", 3))]))
        out = render_cascade_sidecar(tmp_path)
        assert "dsr N=3 source=pool" in out

    def test_pre_adr0008_report_dsr_stamp_absent_is_distinguished(self, tmp_path: Path) -> None:
        # Three-state honesty: a report serialized before the stamp existed is
        # a DIFFERENT state from either source value — never flattened.
        _write_sidecar(tmp_path, _sidecar_json([_group(0.25)]))
        out = render_cascade_sidecar(tmp_path)
        assert "dsr N not stamped (pre-ADR-0008 report)" in out
        assert "source=" not in out

    def test_null_pool_pbo_renders_skipped(self, tmp_path: Path) -> None:
        _write_sidecar(tmp_path, _sidecar_json([_group(None)]))
        out = render_cascade_sidecar(tmp_path)
        assert "pool PBO skipped (pool < 2 candidates / short series / pool empty at #5)" in out

    def test_pre_sidecar_report_key_absent_is_distinguished(self, tmp_path: Path) -> None:
        # Hostile C7a: key-ABSENT (pre-sidecar report) is a different honest
        # state from null (measured skip) — never flattened into one dash.
        _write_sidecar(tmp_path, _sidecar_json([_group(None, omit_key=True)]))
        out = render_cascade_sidecar(tmp_path)
        assert "pool PBO not recorded (pre-sidecar report)" in out
        assert "skipped" not in out

    def test_corrupt_json_is_loud(self, tmp_path: Path) -> None:
        path = _write_sidecar(tmp_path, "{not json")
        out = render_cascade_sidecar(tmp_path)
        assert str(path) in out
        assert "UNREADABLE:" in out

    def test_non_dict_payload_is_loud(self, tmp_path: Path) -> None:
        _write_sidecar(tmp_path, "[1, 2]")
        assert "UNREADABLE: not a cascade report object" in render_cascade_sidecar(tmp_path)

    def test_groups_not_a_list_is_loud(self, tmp_path: Path) -> None:
        _write_sidecar(tmp_path, '{"version": 1, "groups": {"oops": 1}}')
        assert "UNREADABLE: not a cascade report object" in render_cascade_sidecar(tmp_path)

    def test_unsupported_version_is_loud(self, tmp_path: Path) -> None:
        _write_sidecar(tmp_path, _sidecar_json([_group(0.5)], version=99))
        assert "UNREADABLE: unsupported report version 99" in render_cascade_sidecar(tmp_path)

    def test_group_entry_not_object_is_loud(self, tmp_path: Path) -> None:
        _write_sidecar(tmp_path, '{"version": 1, "groups": [42]}')
        assert "UNREADABLE: group entry is not an object" in render_cascade_sidecar(tmp_path)

    def test_group_report_not_object_is_loud(self, tmp_path: Path) -> None:
        _write_sidecar(tmp_path, '{"version": 1, "groups": [{"instrument": "NIFTY", "report": 7}]}')
        assert "UNREADABLE: group report is not an object" in render_cascade_sidecar(tmp_path)

    def test_empty_groups_render_placeholder(self, tmp_path: Path) -> None:
        _write_sidecar(tmp_path, _sidecar_json([]))
        assert "(no pooled slices)" in render_cascade_sidecar(tmp_path)

    def test_show_report_appends_sidecar_section(self, tmp_path: Path) -> None:
        _write_tree(tmp_path)
        _write_sidecar(tmp_path, _sidecar_json([_group(0.25, dsr=("leaderboard", 12))]))
        out = results_show_report(tmp_path, top=10)
        assert "finalists=3" in out
        assert out.rstrip().endswith(
            "pool PBO=0.2500  in=3 survived=2  dsr N=12 source=leaderboard"
        )
