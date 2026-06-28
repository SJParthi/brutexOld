"""Gated multi-symbol pre-build orchestrator tests (DEC-PREBUILD-SELECT-001).

100% line+branch over ``brutex.dashboard.gated_prebuild``: the SPOT → FUTURES →
OPTIONS gated order, the halt-on-failure gate (a failing phase stops the run,
later phases never start), the explicit OPTIONS on-demand no-op, the per-phase
attempted/succeeded/failed accounting, and the report ``to_dict`` / ``ok`` /
``total_bars`` projections.

Pure — an injected ``materialize_one`` fake (returns a ``MaterializeResult`` or
raises). No lake / DuckDB / FastAPI.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from brutex.dashboard.gated_prebuild import (
    PREBUILD_PHASE_ORDER,
    GatedPrebuildReport,
    PhasePrebuildReport,
    run_gated_prebuild,
)
from brutex.data.timeframe_materialize import MaterializeResult, PrebuildKind
from brutex.models import Exchange, Instrument, InstrumentType, Segment


def _spot(name: str) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=hash(name) & 0xFFFF,
        symbol_name=name,
        display_name=name,
        instrument=InstrumentType.IDX,
        segment=Segment.INDEX,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
    )


def _future(name: str) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=hash(name) & 0xFFFF,
        symbol_name=name,
        display_name=name,
        instrument=InstrumentType.FUT,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol="NIFTY",
        underlying_security_id=1,
        lot_size=75,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=1800,
        sm_expiry_date=date(2025, 1, 30),
        strike_price=None,
    )


def _option(name: str) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=hash(name) & 0xFFFF,
        symbol_name=name,
        display_name=name,
        instrument=InstrumentType.CE,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol="NIFTY",
        underlying_security_id=1,
        lot_size=75,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=1800,
        sm_expiry_date=date(2025, 1, 30),
        strike_price=Decimal("20000"),
    )


def _ok(bars: int) -> MaterializeResult:
    return MaterializeResult(written={"5m": bars}, skipped=())


def test_phase_order_is_spot_futures_options() -> None:
    assert PREBUILD_PHASE_ORDER == (
        PrebuildKind.SPOT,
        PrebuildKind.FUTURES,
        PrebuildKind.OPTIONS,
    )


def test_all_phases_run_when_every_symbol_succeeds() -> None:
    targets = [_spot("NSE-NIFTY"), _future("NSE-NIFTY-FUT"), _option("NSE-NIFTY-CE")]
    calls: list[str] = []

    def materialize_one(inst: Instrument) -> MaterializeResult:
        calls.append(inst.symbol_name)
        return _ok(10)

    report = run_gated_prebuild(
        targets=targets, materialize_one=materialize_one, source="selection"
    )

    assert report.ok
    assert report.halted_after is None
    assert report.source == "selection"
    assert [p.phase for p in report.phases] == ["spot", "futures", "options"]
    # SPOT + FUTURES materialised; OPTIONS no-op (never called materialize_one).
    assert calls == ["NSE-NIFTY", "NSE-NIFTY-FUT"]
    spot, futures, options = report.phases
    assert spot.succeeded == ("NSE-NIFTY",)
    assert futures.succeeded == ("NSE-NIFTY-FUT",)
    assert options.attempted == ("NSE-NIFTY-CE",)
    assert options.succeeded == ()
    assert options.total_bars == 0
    assert "on-demand" in options.note
    assert report.total_bars == 20


def test_spot_failure_halts_before_futures_and_options() -> None:
    targets = [_spot("BAD"), _future("NSE-NIFTY-FUT"), _option("NSE-NIFTY-CE")]
    calls: list[str] = []

    def materialize_one(inst: Instrument) -> MaterializeResult:
        calls.append(inst.symbol_name)
        if inst.symbol_name == "BAD":
            raise ValueError("no stored 1minute bars")
        return _ok(10)

    report = run_gated_prebuild(targets=targets, materialize_one=materialize_one)

    assert not report.ok
    assert report.halted_after == "spot"
    # Only the spot phase ran; futures + options never started (the gate).
    assert [p.phase for p in report.phases] == ["spot"]
    assert calls == ["BAD"]  # futures never materialised
    spot = report.phases[0]
    assert spot.failed == (("BAD", "ValueError: no stored 1minute bars"),)
    assert spot.succeeded == ()


def test_futures_failure_halts_before_options() -> None:
    targets = [_spot("NSE-NIFTY"), _future("FUTBAD"), _option("NSE-NIFTY-CE")]

    def materialize_one(inst: Instrument) -> MaterializeResult:
        if inst.symbol_name == "FUTBAD":
            raise OSError("lake read fault")
        return _ok(7)

    report = run_gated_prebuild(targets=targets, materialize_one=materialize_one)

    assert not report.ok
    assert report.halted_after == "futures"
    assert [p.phase for p in report.phases] == ["spot", "futures"]
    assert report.phases[0].ok  # spot fully succeeded → advanced to futures
    assert report.phases[1].failed == (("FUTBAD", "OSError: lake read fault"),)


def test_phase_collects_all_failures_then_halts() -> None:
    """Within a phase, ALL failures are recorded (not just the first), then halt."""
    targets = [_spot("A"), _spot("B"), _spot("C")]

    def materialize_one(inst: Instrument) -> MaterializeResult:
        if inst.symbol_name in ("A", "C"):
            raise ValueError(f"bad {inst.symbol_name}")
        return _ok(5)

    report = run_gated_prebuild(targets=targets, materialize_one=materialize_one)

    spot = report.phases[0]
    assert spot.attempted == ("A", "B", "C")
    assert spot.succeeded == ("B",)
    assert [s for s, _ in spot.failed] == ["A", "C"]
    assert report.halted_after == "spot"


def test_genuine_bug_propagates_not_swallowed() -> None:
    """A non-data error (KeyError) is NOT caught — it surfaces (never a fake failure)."""
    targets = [_spot("X")]

    def materialize_one(inst: Instrument) -> MaterializeResult:
        raise KeyError("genuine bug")

    with pytest.raises(KeyError, match="genuine bug"):
        run_gated_prebuild(targets=targets, materialize_one=materialize_one)


def test_empty_selection_runs_three_empty_phases() -> None:
    report = run_gated_prebuild(targets=[], materialize_one=lambda _i: _ok(1))
    assert report.ok
    assert report.halted_after is None
    assert [p.phase for p in report.phases] == ["spot", "futures", "options"]
    assert all(p.attempted == () for p in report.phases)
    assert report.total_bars == 0


def test_options_only_selection_is_pure_noop() -> None:
    called = {"n": 0}

    def materialize_one(_inst: Instrument) -> MaterializeResult:
        called["n"] += 1
        return _ok(1)

    report = run_gated_prebuild(
        targets=[_option("NSE-NIFTY-CE"), _option("NSE-BANKNIFTY-PE")],
        materialize_one=materialize_one,
    )

    assert report.ok
    assert called["n"] == 0  # OPTIONS never materialises
    options = report.phases[-1]
    assert options.phase == "options"
    assert options.attempted == ("NSE-NIFTY-CE", "NSE-BANKNIFTY-PE")
    assert options.total_bars == 0


# ── report projections ────────────────────────────────────────────────────


class TestReportProjections:
    def test_phase_to_dict(self) -> None:
        phase = PhasePrebuildReport(
            phase="spot",
            attempted=("A", "B"),
            succeeded=("A",),
            failed=(("B", "ValueError: x"),),
            total_bars=3,
            note="n",
        )
        assert phase.to_dict() == {
            "phase": "spot",
            "ok": False,
            "attempted": ["A", "B"],
            "succeeded": ["A"],
            "failed": [{"symbol": "B", "error": "ValueError: x"}],
            "total_bars": 3,
            "note": "n",
        }

    def test_phase_ok_when_no_failures(self) -> None:
        assert PhasePrebuildReport("spot", ("A",), ("A",), (), 1).ok

    def test_report_to_dict_round_trips(self) -> None:
        report = GatedPrebuildReport(
            phases=(PhasePrebuildReport("spot", ("A",), ("A",), (), 4),),
            halted_after=None,
            source="single",
        )
        d = report.to_dict()
        assert d["ok"] is True
        assert d["halted_after"] is None
        assert d["source"] == "single"
        assert d["total_bars"] == 4
        assert d["phases"][0]["phase"] == "spot"

    def test_report_ok_false_when_halted_even_if_phases_ok(self) -> None:
        # halted_after set ⇒ not ok regardless of the ran-phase verdicts.
        report = GatedPrebuildReport(
            phases=(PhasePrebuildReport("spot", ("A",), ("A",), (), 1),),
            halted_after="futures",
        )
        assert report.ok is False

    def test_default_report_is_empty_ok(self) -> None:
        report = GatedPrebuildReport()
        assert report.ok
        assert report.total_bars == 0
        assert report.phases == ()
