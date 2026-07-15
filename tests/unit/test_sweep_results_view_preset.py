"""Preset ranking + store-error payload on the sweep leaderboard view.

DEC-SWEEP-RESULTS-DEPTH-001, package C (gap-register ruling 10): the OPTIONAL
``preset`` arm of :func:`brutex.dashboard.sweep_results_view.build_sweep_leaderboard`
— absent/blank is BYTE-IDENTICAL to the legacy ``rank_by`` ordering (payload
shape included, spy-pinned to never touch the preset machinery); a known token
delegates the WHOLE ordering to :mod:`brutex.ranking.presets` (full precision,
``run_id``-ASC deterministic ties); an unknown token refuses loudly. Plus the
C3 :func:`store_error_payload` honest degrade shape.
"""

# The spy tests read module attributes deliberately.
# pyright: reportPrivateUsage=none
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("polars", reason="the reader loads per-tuple Parquet stores via ResultsStore")

from brutex.dashboard.sweep_results_view import (
    DEFAULT_RANK_BY,
    PRESET_INERT_AXES,
    STORE_ERROR_PUBLIC_DETAIL,
    build_sweep_leaderboard,
    load_sweep_scan,
    resolve_preset_param,
    store_error_payload,
)
from brutex.engine.results_store import ResultsStoreError
from brutex.ranking.presets import PRESET_NAMES

from tests.fakes.leaderboard_preset_fixtures import (
    make_leaderboard_row,
    run_id_hex,
    write_tuple_store,
)

#: The exact pre-preset payload key set (the byte-identical absent-arm pin).
#: DEC-SWEEP-TEMPORAL-ROBUSTNESS-001 (graft 9) consciously extended it with the
#: two always-present additive envelope keys.
_LEGACY_PAYLOAD_KEYS = {
    "available",
    "total_rows",
    "store_count",
    "skipped",
    "rank_by",
    "instruments",
    "timeframes",
    "directions",
    "snapshot_hashes",
    "mixed_snapshot_eras",
    "run_id_conflicts",
    "rows",
    "temporal_coverage",
    "cascade_report",
}


class TestResolvePresetParam:
    def test_none_resolves_to_none(self) -> None:
        assert resolve_preset_param(None) is None

    def test_blank_and_whitespace_resolve_to_none(self) -> None:
        assert resolve_preset_param("") is None
        assert resolve_preset_param("   ") is None

    @pytest.mark.parametrize("token", PRESET_NAMES)
    def test_every_registry_token_resolves(self, token: str) -> None:
        assert resolve_preset_param(token) == token

    def test_outer_whitespace_is_stripped(self) -> None:
        assert resolve_preset_param(" min_dd_first ") == "min_dd_first"

    def test_unknown_token_raises_naming_every_valid_value(self) -> None:
        with pytest.raises(ValueError, match="unknown ranking preset") as excinfo:
            resolve_preset_param("bogus")
        message = str(excinfo.value)
        for token in PRESET_NAMES:
            assert token in message

    def test_case_sensitive_exact_match(self) -> None:
        """The registry keys are the wire vocabulary — no case folding."""
        with pytest.raises(ValueError, match="unknown ranking preset"):
            resolve_preset_param("MIN_DD_FIRST")


class TestAbsentPresetByteIdentical:
    """The absent/blank arm keeps the CURRENT ordering + payload byte-identical."""

    def test_no_preset_kw_equals_none_equals_blank(self, tmp_path: Path) -> None:
        write_tuple_store(
            tmp_path,
            [
                make_leaderboard_row(1, sharpe_ratio=0.5),
                make_leaderboard_row(2, sharpe_ratio=2.5),
            ],
        )
        legacy = build_sweep_leaderboard(tmp_path).to_dict()
        assert build_sweep_leaderboard(tmp_path, preset=None).to_dict() == legacy
        assert build_sweep_leaderboard(tmp_path, preset="").to_dict() == legacy

    def test_absent_payload_has_no_preset_keys(self, tmp_path: Path) -> None:
        """The pre-preset payload SHAPE is untouched — no additive keys leak."""
        write_tuple_store(tmp_path, [make_leaderboard_row(1)])
        payload = build_sweep_leaderboard(tmp_path).to_dict()
        assert set(payload) == _LEGACY_PAYLOAD_KEYS
        assert "preset" not in payload
        assert "preset_inert_axes" not in payload

    def test_absent_preset_never_touches_the_preset_machinery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Spy pin: with no preset the ordering path never calls sort_by_preset,
        so the legacy ordering is byte-identical by CONSTRUCTION, not luck."""
        from brutex.dashboard import sweep_results_view as view_mod

        write_tuple_store(
            tmp_path,
            [
                make_leaderboard_row(1, sharpe_ratio=0.5),
                make_leaderboard_row(2, sharpe_ratio=2.5),
            ],
        )

        def _forbidden(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("sort_by_preset must not run on the absent-preset path")

        monkeypatch.setattr(view_mod, "sort_by_preset", _forbidden)
        board = build_sweep_leaderboard(tmp_path, preset=None)
        assert [r["sharpe_ratio"] for r in board.rows] == [2.5, 0.5]


class TestPresetOrdering:
    """A given preset orders via brutex.ranking.presets — the ONE ordering law."""

    def test_min_dd_first_orders_ascending_drawdown(self, tmp_path: Path) -> None:
        write_tuple_store(
            tmp_path,
            [
                make_leaderboard_row(1, max_drawdown_pct=20.0),
                make_leaderboard_row(2, max_drawdown_pct=5.0),
                make_leaderboard_row(3, max_drawdown_pct=12.5),
            ],
        )
        board = build_sweep_leaderboard(tmp_path, preset="min_dd_first")
        assert [r["max_drawdown_pct"] for r in board.rows] == [5.0, 12.5, 20.0]
        assert [r["rank"] for r in board.rows] == [1, 2, 3]
        assert board.preset == "min_dd_first"

    def test_min_dd_first_ties_break_run_id_ascending(self, tmp_path: Path) -> None:
        """The store's tie law: equal drawdown (and inert tiebreak axes) falls
        through to run_id ASC — deterministic for any discovery order."""
        write_tuple_store(
            tmp_path,
            [
                make_leaderboard_row(9, max_drawdown_pct=10.0),
                make_leaderboard_row(3, max_drawdown_pct=10.0),
                make_leaderboard_row(6, max_drawdown_pct=10.0),
            ],
        )
        board = build_sweep_leaderboard(tmp_path, preset="min_dd_first")
        assert [r["run_id"] for r in board.rows] == [run_id_hex(3), run_id_hex(6), run_id_hex(9)]

    def test_profit_factor_preset_orders_descending(self, tmp_path: Path) -> None:
        write_tuple_store(
            tmp_path,
            [
                make_leaderboard_row(1, profit_factor=1.5),
                make_leaderboard_row(2, profit_factor=3.25),
            ],
        )
        board = build_sweep_leaderboard(tmp_path, preset="profit_factor")
        assert [r["profit_factor"] for r in board.rows] == [3.25, 1.5]

    def test_win_rate_preset_orders_descending(self, tmp_path: Path) -> None:
        write_tuple_store(
            tmp_path,
            [
                make_leaderboard_row(1, win_rate=0.4),
                make_leaderboard_row(2, win_rate=0.75),
            ],
        )
        board = build_sweep_leaderboard(tmp_path, preset="win_rate")
        assert [r["win_rate"] for r in board.rows] == [0.75, 0.4]

    def test_calmar_preset_orders_descending_full_precision(self, tmp_path: Path) -> None:
        """Raw full-precision values order the board — never rounded."""
        write_tuple_store(
            tmp_path,
            [
                make_leaderboard_row(1, calmar_ratio=1.0000001),
                make_leaderboard_row(2, calmar_ratio=1.0000002),
            ],
        )
        board = build_sweep_leaderboard(tmp_path, preset="calmar")
        assert [r["run_id"] for r in board.rows] == [run_id_hex(2), run_id_hex(1)]

    def test_composite_preset_degenerates_to_run_id_honestly(self, tmp_path: Path) -> None:
        """composite_score is NOT persisted on ResultRow: the axis is constant-
        filled (inert) so ordering falls to run_id ASC — surfaced honestly via
        ``preset_inert_axes``, never a silently fabricated per-row composite."""
        write_tuple_store(
            tmp_path,
            [
                make_leaderboard_row(5, sharpe_ratio=9.9),
                make_leaderboard_row(2, sharpe_ratio=0.1),
            ],
        )
        payload = build_sweep_leaderboard(tmp_path, preset="composite").to_dict()
        assert [r["run_id"] for r in payload["rows"]] == [run_id_hex(2), run_id_hex(5)]
        assert payload["preset"] == "composite"
        assert payload["preset_inert_axes"] == list(PRESET_INERT_AXES)

    def test_unknown_preset_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="unknown ranking preset"):
            build_sweep_leaderboard(tmp_path, preset="not_a_preset")

    def test_preset_payload_gains_exactly_two_additive_keys(self, tmp_path: Path) -> None:
        write_tuple_store(tmp_path, [make_leaderboard_row(1)])
        payload = build_sweep_leaderboard(tmp_path, preset="min_dd_first").to_dict()
        assert set(payload) == _LEGACY_PAYLOAD_KEYS | {"preset", "preset_inert_axes"}
        # rank_by is echoed unchanged for the dropdown state — it did NOT order.
        assert payload["rank_by"] == DEFAULT_RANK_BY

    def test_preset_composes_with_filters_and_top_n(self, tmp_path: Path) -> None:
        write_tuple_store(
            tmp_path,
            [
                make_leaderboard_row(1, max_drawdown_pct=20.0),
                make_leaderboard_row(2, max_drawdown_pct=5.0),
                make_leaderboard_row(3, max_drawdown_pct=1.0),
            ],
        )
        write_tuple_store(
            tmp_path,
            [
                make_leaderboard_row(
                    4, instrument="BANKNIFTY", tf="15m", direction="bear", max_drawdown_pct=0.5
                )
            ],
            instrument="BANKNIFTY",
            tf="15m",
            direction="bear",
        )
        board = build_sweep_leaderboard(
            tmp_path, instrument="NIFTY", top_n=2, preset="min_dd_first"
        )
        assert board.total_rows == 3  # filter counts, before the top slice
        assert [r["max_drawdown_pct"] for r in board.rows] == [1.0, 5.0]
        assert {r["instrument"] for r in board.rows} == {"NIFTY"}

    def test_preset_with_prescanned_scan_matches_inline_scan(self, tmp_path: Path) -> None:
        """The route's cached-scan path ranks identically under a preset."""
        write_tuple_store(
            tmp_path,
            [
                make_leaderboard_row(1, max_drawdown_pct=9.0),
                make_leaderboard_row(2, max_drawdown_pct=3.0),
            ],
        )
        scan = load_sweep_scan(tmp_path)
        via_scan = build_sweep_leaderboard(tmp_path, scan=scan, preset="min_dd_first")
        inline = build_sweep_leaderboard(tmp_path, preset="min_dd_first")
        assert via_scan == inline

    def test_preset_ranking_is_discovery_order_independent(self, tmp_path: Path) -> None:
        """Two stores, interleaved values → ONE deterministic total order."""
        write_tuple_store(tmp_path, [make_leaderboard_row(1, max_drawdown_pct=7.0)])
        write_tuple_store(
            tmp_path,
            [make_leaderboard_row(2, tf="15m", max_drawdown_pct=2.0)],
            tf="15m",
        )
        board = build_sweep_leaderboard(tmp_path, preset="min_dd_first")
        assert [r["max_drawdown_pct"] for r in board.rows] == [2.0, 7.0]


class TestStoreErrorPayload:
    """C3: the honest degrade shape for a typed store fault (no blank illusion)."""

    def test_shape_carries_error_markers_plus_full_empty_board(self) -> None:
        payload = store_error_payload(ResultsStoreError("synthetic store fault"))
        assert payload["available"] is False
        assert payload["error"] == STORE_ERROR_PUBLIC_DETAIL
        assert payload["error_type"] == "results_store"
        assert payload["rows"] == []
        assert payload["total_rows"] == 0
        assert payload["rank_by"] == DEFAULT_RANK_BY
        # The renderer-compatible empty shape is complete (never a KeyError on
        # the page), PLUS the two explicit error markers.
        assert set(payload) == _LEGACY_PAYLOAD_KEYS | {"error", "error_type"}

    def test_distinguishable_from_the_no_sweep_blank(self, tmp_path: Path) -> None:
        """An error payload can never masquerade as "no sweep run yet"."""
        blank = build_sweep_leaderboard(tmp_path).to_dict()
        errored = store_error_payload(ResultsStoreError("boom"))
        assert "error" not in blank
        assert errored["error"] == STORE_ERROR_PUBLIC_DETAIL

    def test_exception_internals_never_reach_the_wire(self) -> None:
        """CodeQL py/stack-trace-exposure pin: the served payload is SANITIZED.

        The raised message routinely carries internals (a store path, a decode
        traceback's text) — none of it may appear ANYWHERE in the externally
        served payload; the wire carries only the stable generic marker + the
        stable ``results_store`` reason token.
        """
        import json

        secret = "/secret/lake/sweeps/NIFTY/5m/long_k3_deadbeef.parquet: boom-internals"
        payload = store_error_payload(ResultsStoreError(f"decode failed: {secret}"))
        wire = json.dumps(payload)
        assert "boom-internals" not in wire
        assert secret not in wire
        assert payload["error"] == STORE_ERROR_PUBLIC_DETAIL
        assert payload["error_type"] == "results_store"

    def test_logs_the_degrade_with_full_detail(self) -> None:
        """The FULL exception detail lands server-side (log), never wire-side."""
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            store_error_payload(ResultsStoreError("visible fault"))
        events = [log for log in logs if log["event"] == "sweep_results.store_error"]
        assert len(events) == 1
        assert events[0]["error"] == "visible fault"
        assert events[0]["error_type"] == "ResultsStoreError"
