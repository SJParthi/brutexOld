"""Frozen-corpus parity goldens for the Rust cascade/results-depth laws.

DEC-RUST-CASCADE-RESULTS-001 (T14 lane). The fixtures under
``fixtures/cascade_rust_parity/`` were captured from the PRE-conversion
pure-Python implementations (commit 2a950c0, at merge-base 415e863) — the
capture-first discipline — and are BYTE-FROZEN (the generator refuses
regeneration without ``BRUTEX_REGEN_CASCADE_PARITY=1``). This suite replays
the SAME corpus inputs (built by ``_cascade_rust_parity_corpus`` — shared
with the generator, so inputs cannot drift) through the post-conversion
Rust-backed public API and asserts EXACT equality — ints exact, floats
bit-exact (the JSON round-trip preserves the shortest-repr float exactly),
JSON lens strings byte-exact, WARN counters + capped note strings verbatim.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from brutex.engine.cascade_join import CascadeJoinError, build_cascade_join
from brutex.engine.combo_aggregates import _float_stats, aggregate_combo, nearest_rank
from brutex.engine.minute_path import (
    MinuteIndex,
    SessionEquityPoint,
    ist_session_dates,
    minute_path_max_drawdown,
    session_equity_series,
    trade_excursions_1m,
)
from structlog.testing import capture_logs

from tests.golden._cascade_rust_parity_corpus import (
    aggregate_cases,
    cascade_join_payloads,
    drawdown_cases,
    equity_cases,
    excursion_cases,
    float_stats_cases,
    nearest_rank_cases,
    session_dates_cases,
)
from tests.golden.gen_cascade_rust_parity import _join_to_jsonable

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "cascade_rust_parity"


def _fixture(name: str) -> Any:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


class TestCascadeJoinParity:
    """Every frozen cascade_join case reproduces byte-exactly."""

    @pytest.mark.parametrize(
        "case_name", [name for name, _ in cascade_join_payloads()], ids=lambda n: n
    )
    def test_case(self, case_name: str) -> None:
        payloads = dict(cascade_join_payloads())
        frozen = {case["name"]: case["expect"] for case in _fixture("cascade_join.json")}
        expect = frozen[case_name]
        with capture_logs() as logs:
            if "error" in expect:
                with pytest.raises(CascadeJoinError) as exc_info:
                    build_cascade_join(payloads[case_name])
                assert str(exc_info.value) == expect["error"]
                return
            join = build_cascade_join(payloads[case_name])
        assert _join_to_jsonable(join) == expect["join"]
        warns = [e for e in logs if e.get("event") == "cascade_join.partial"]
        if expect["warn"] is None:
            assert warns == []
        else:
            assert len(warns) == 1
            warn = warns[0]
            for key in (
                "sweep_id",
                "skipped_groups",
                "skipped_verdicts",
                "nulled_values",
                "rank_inconsistencies",
                "duplicate_ids",
            ):
                assert warn[key] == expect["warn"][key], key
            assert list(warn["details"]) == expect["warn"]["details"]


class TestMinutePathParity:
    """Every frozen minute_path case reproduces exactly."""

    def test_excursions(self) -> None:
        frozen = {
            case["name"]: case["expect"] for case in _fixture("minute_path.json")["excursions"]
        }
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
            expect = frozen[case["name"]]
            assert result.mae_paisa == expect["mae_paisa"], case["name"]
            assert result.mfe_paisa == expect["mfe_paisa"], case["name"]
            assert result.status.value == expect["status"], case["name"]

    def test_equity_series(self) -> None:
        frozen = {case["name"]: case["expect"] for case in _fixture("minute_path.json")["equity"]}
        for case in equity_cases():
            bars = case["bars"]
            index = None if bars is None else MinuteIndex.from_bars(bars)
            points, status = session_equity_series(
                index,
                case["trades"],
                sessions=case["sessions"],
                start_equity_paisa=case["start_equity_paisa"],
            )
            expect = frozen[case["name"]]
            assert status.value == expect["status"], case["name"]
            got = [
                [
                    p.session_date.isoformat(),
                    p.equity_close_paisa,
                    p.equity_intraday_low_paisa,
                    p.trades_closed,
                ]
                for p in points
            ]
            assert got == expect["points"], case["name"]

    def test_drawdowns(self) -> None:
        frozen = {
            case["name"]: case["expect"] for case in _fixture("minute_path.json")["drawdowns"]
        }
        for case in drawdown_cases():
            points = tuple(
                SessionEquityPoint(
                    session_date=d,
                    equity_close_paisa=close,
                    equity_intraday_low_paisa=low,
                    trades_closed=n,
                )
                for d, close, low, n in case["points"]
            )
            dd, pct = minute_path_max_drawdown(
                points, start_equity_paisa=case["start_equity_paisa"]
            )
            expect = frozen[case["name"]]
            assert dd == expect["max_dd_paisa"], case["name"]
            # Bit-exact float equality — the JSON round trip preserves it.
            assert pct == expect["max_dd_pct"], case["name"]

    def test_session_dates(self) -> None:
        frozen = {case["name"]: case["expect"] for case in _fixture("minute_path.json")["dates"]}
        for case in session_dates_cases():
            got = [d.isoformat() for d in ist_session_dates(case["bars"])]
            assert got == frozen[case["name"]], case["name"]
            assert all(isinstance(d, date) for d in ist_session_dates(case["bars"]))


class TestComboAggregatesParity:
    """Every frozen combo_aggregates case reproduces exactly."""

    def test_nearest_rank(self) -> None:
        frozen = {
            case["name"]: case["expect"]
            for case in _fixture("combo_aggregates.json")["nearest_rank"]
        }
        for case in nearest_rank_cases():
            got = nearest_rank(sorted(case["values"]), case["pct"])
            assert got == frozen[case["name"]], case["name"]

    def test_float_stats(self) -> None:
        frozen = {
            case["name"]: case["expect"]
            for case in _fixture("combo_aggregates.json")["float_stats"]
        }
        for case in float_stats_cases():
            got = list(_float_stats(sorted(case["values"])))
            assert got == frozen[case["name"]], case["name"]

    def test_aggregates(self) -> None:
        frozen = {
            case["name"]: case["expect"] for case in _fixture("combo_aggregates.json")["aggregates"]
        }
        for case in aggregate_cases():
            agg = aggregate_combo(
                case["capture"],
                case["row"],
                lens=case["lens"],
                composite_score=case["composite_score"],
                fingerprint=case["fingerprint"],
                start_equity_paisa=case["start_equity_paisa"],
            )
            assert agg.to_record() == frozen[case["name"]], case["name"]
