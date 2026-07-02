"""Gated multi-symbol pre-build orchestrator tests (DEC-PREBUILD-SELECT-001).

100% line+branch over ``brutex.dashboard.gated_prebuild``: the SPOT → FUTURES →
OPTIONS gated order, the halt-on-failure gate (a failing phase stops the run,
later phases never start), the FUTURES-contract discovery from the F&O candle
lake cascade, the explicit OPTIONS on-demand no-op, the per-phase
attempted/succeeded/failed accounting, the underlying-token matching, and the
report ``to_dict`` / ``ok`` / ``total_bars`` projections.

Pure — injected ``materialize_spot`` / ``futures_contracts_for`` /
``materialize_future`` fakes (return a ``MaterializeResult`` or raise). No lake /
DuckDB / FastAPI.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from brutex.dashboard.fno_cascade import FnoContract, build_fno_cascade
from brutex.dashboard.gated_prebuild import (
    PREBUILD_PHASE_ORDER,
    GatedPrebuildReport,
    PhasePrebuildReport,
    discover_futures_contracts,
    run_gated_prebuild,
    underlying_match_tokens,
)
from brutex.data.timeframe_materialize import MaterializeResult, PrebuildKind
from brutex.models import Exchange, Instrument, InstrumentType, Segment


def _spot(name: str, *, underlying: str | None = None) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=abs(hash(name)) & 0xFFFF,
        symbol_name=name,
        display_name=name,
        instrument=InstrumentType.IDX,
        segment=Segment.INDEX,
        series=None,
        isin=None,
        underlying_symbol=underlying,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
    )


def _ok(bars: int) -> MaterializeResult:
    return MaterializeResult(written={"5m": bars}, skipped=())


def _no_futures(_inst: Instrument) -> list[str]:
    return []


def _raise_future(_c: str) -> MaterializeResult:  # pragma: no cover — only used as an unused arg
    raise AssertionError("materialize_future should not be called")


# ── phase order ────────────────────────────────────────────────────────────


def test_phase_order_is_spot_futures_options() -> None:
    assert PREBUILD_PHASE_ORDER == (
        PrebuildKind.SPOT,
        PrebuildKind.FUTURES,
        PrebuildKind.OPTIONS,
    )


# ── underlying token matching ──────────────────────────────────────────────


class TestUnderlyingMatchTokens:
    def test_strips_exchange_prefix(self) -> None:
        tokens = underlying_match_tokens(_spot("NSE-NIFTY"))
        assert "NSE-NIFTY" in tokens
        assert "NIFTY" in tokens

    def test_bse_prefix(self) -> None:
        assert "SENSEX" in underlying_match_tokens(_spot("BSE-SENSEX"))

    def test_bare_symbol_unchanged(self) -> None:
        tokens = underlying_match_tokens(_spot("RELIANCE"))
        assert tokens == frozenset({"RELIANCE"})

    def test_includes_underlying_symbol(self) -> None:
        tokens = underlying_match_tokens(_spot("NSE-NIFTY", underlying="NIFTY"))
        assert "NIFTY" in tokens


# ── futures discovery from the cascade ─────────────────────────────────────


def _fno_datasets() -> list[dict[str, object]]:
    """Two NIFTY futures (two expiries) + one option, across 1m + 1d intervals."""
    return [
        {
            "symbol": "NSE-NIFTY-30Jan25-FUT",
            "exchange": "NSE",
            "underlying": "NIFTY",
            "expiry": "2025-01-30",
            "strike": None,
            "option_type": None,
            "instrument_type": "FUT",
            "interval": "1minute",
        },
        {
            "symbol": "NSE-NIFTY-27Feb25-FUT",
            "exchange": "NSE",
            "underlying": "NIFTY",
            "expiry": "2025-02-27",
            "strike": None,
            "option_type": None,
            "instrument_type": "FUT",
            "interval": "1minute",
        },
        {
            "symbol": "NSE-NIFTY-30Jan25-20000-CE",
            "exchange": "NSE",
            "underlying": "NIFTY",
            "expiry": "2025-01-30",
            "strike": "20000",
            "option_type": "CE",
            "instrument_type": "CE",
            "interval": "1minute",
        },
    ]


class TestDiscoverFuturesContracts:
    def test_finds_only_futures_for_underlying(self) -> None:
        cascade = build_fno_cascade(_fno_datasets())
        found = discover_futures_contracts(_spot("NSE-NIFTY"), cascade)
        symbols = {c.symbol for c in found}
        assert symbols == {"NSE-NIFTY-30Jan25-FUT", "NSE-NIFTY-27Feb25-FUT"}
        assert all(c.instrument_type == "FUT" for c in found)
        assert all(isinstance(c, FnoContract) for c in found)

    def test_unknown_underlying_returns_empty(self) -> None:
        cascade = build_fno_cascade(_fno_datasets())
        assert discover_futures_contracts(_spot("NSE-BANKNIFTY"), cascade) == ()

    def test_empty_cascade_returns_empty(self) -> None:
        cascade = build_fno_cascade([])
        assert discover_futures_contracts(_spot("NSE-NIFTY"), cascade) == ()


# ── the gated run ──────────────────────────────────────────────────────────


def test_all_phases_run_when_everything_succeeds() -> None:
    underlyings = [_spot("NSE-NIFTY")]
    spot_calls: list[str] = []
    fut_calls: list[str] = []

    def materialize_spot(inst: Instrument) -> MaterializeResult:
        spot_calls.append(inst.symbol_name)
        return _ok(10)

    def futures_for(_inst: Instrument) -> list[str]:
        return ["NSE-NIFTY-30Jan25-FUT", "NSE-NIFTY-27Feb25-FUT"]

    def materialize_future(contract: str) -> MaterializeResult:
        fut_calls.append(contract)
        return _ok(4)

    report = run_gated_prebuild(
        underlyings=underlyings,
        materialize_spot=materialize_spot,
        futures_contracts_for=futures_for,
        materialize_future=materialize_future,
        source="selection",
    )

    assert report.ok
    assert report.halted_after is None
    assert report.source == "selection"
    assert [p.phase for p in report.phases] == ["spot", "futures", "options"]
    spot, futures, options = report.phases
    assert spot.succeeded == ("NSE-NIFTY",)
    assert spot_calls == ["NSE-NIFTY"]
    assert futures.attempted == ("NSE-NIFTY-30Jan25-FUT", "NSE-NIFTY-27Feb25-FUT")
    assert futures.succeeded == ("NSE-NIFTY-30Jan25-FUT", "NSE-NIFTY-27Feb25-FUT")
    assert fut_calls == ["NSE-NIFTY-30Jan25-FUT", "NSE-NIFTY-27Feb25-FUT"]
    assert options.attempted == ("NSE-NIFTY",)
    assert options.succeeded == ()
    assert options.total_bars == 0
    assert "on-demand" in options.note
    assert report.total_bars == 10 + 4 + 4


def test_spot_failure_halts_before_futures_and_options() -> None:
    fut_called = {"n": 0}

    def materialize_spot(inst: Instrument) -> MaterializeResult:
        raise ValueError("no stored 1minute bars")

    def futures_for(_inst: Instrument) -> list[str]:  # pragma: no cover — never reached
        fut_called["n"] += 1
        return ["X"]

    report = run_gated_prebuild(
        underlyings=[_spot("BAD")],
        materialize_spot=materialize_spot,
        futures_contracts_for=futures_for,
        materialize_future=_raise_future,
    )

    assert not report.ok
    assert report.halted_after == "spot"
    assert [p.phase for p in report.phases] == ["spot"]
    assert fut_called["n"] == 0  # futures phase never started (the gate)
    assert report.phases[0].failed == (("BAD", "ValueError: no stored 1minute bars"),)


def test_spot_collects_all_failures_then_halts() -> None:
    def materialize_spot(inst: Instrument) -> MaterializeResult:
        if inst.symbol_name in ("A", "C"):
            raise OSError(f"bad {inst.symbol_name}")
        return _ok(5)

    report = run_gated_prebuild(
        underlyings=[_spot("A"), _spot("B"), _spot("C")],
        materialize_spot=materialize_spot,
        futures_contracts_for=_no_futures,
        materialize_future=_raise_future,
    )

    spot = report.phases[0]
    assert spot.attempted == ("A", "B", "C")
    assert spot.succeeded == ("B",)
    assert [s for s, _ in spot.failed] == ["A", "C"]
    assert report.halted_after == "spot"


def test_futures_contract_failure_halts_before_options() -> None:
    def futures_for(_inst: Instrument) -> list[str]:
        return ["FUT-OK", "FUT-BAD"]

    def materialize_future(contract: str) -> MaterializeResult:
        if contract == "FUT-BAD":
            raise OSError("lake read fault")
        return _ok(7)

    report = run_gated_prebuild(
        underlyings=[_spot("NSE-NIFTY")],
        materialize_spot=lambda _i: _ok(3),
        futures_contracts_for=futures_for,
        materialize_future=materialize_future,
    )

    assert not report.ok
    assert report.halted_after == "futures"
    assert [p.phase for p in report.phases] == ["spot", "futures"]
    futures = report.phases[1]
    assert futures.attempted == ("FUT-OK", "FUT-BAD")
    assert futures.succeeded == ("FUT-OK",)
    assert futures.failed == (("FUT-BAD", "OSError: lake read fault"),)


def test_futures_discovery_failure_is_recorded() -> None:
    def futures_for(inst: Instrument) -> list[str]:
        raise ValueError("cascade build failed")

    report = run_gated_prebuild(
        underlyings=[_spot("NSE-NIFTY")],
        materialize_spot=lambda _i: _ok(3),
        futures_contracts_for=futures_for,
        materialize_future=_raise_future,
    )

    assert report.halted_after == "futures"
    futures = report.phases[1]
    assert futures.failed == (("NSE-NIFTY", "ValueError: cascade build failed"),)


def test_no_futures_found_is_clean_skip_with_note() -> None:
    report = run_gated_prebuild(
        underlyings=[_spot("NSE-NIFTY")],
        materialize_spot=lambda _i: _ok(3),
        futures_contracts_for=_no_futures,
        materialize_future=_raise_future,
    )

    assert report.ok
    futures = report.phases[1]
    assert futures.attempted == ()
    assert futures.succeeded == ()
    assert futures.total_bars == 0
    assert "no futures contracts found" in futures.note
    # OPTIONS still runs (gate not tripped).
    assert report.phases[-1].phase == "options"


def test_spot_genuine_bug_propagates_not_swallowed() -> None:
    def materialize_spot(inst: Instrument) -> MaterializeResult:
        raise KeyError("genuine bug")

    with pytest.raises(KeyError, match="genuine bug"):
        run_gated_prebuild(
            underlyings=[_spot("X")],
            materialize_spot=materialize_spot,
            futures_contracts_for=_no_futures,
            materialize_future=_raise_future,
        )


def test_futures_genuine_bug_propagates() -> None:
    def materialize_future(_c: str) -> MaterializeResult:
        raise KeyError("genuine future bug")

    with pytest.raises(KeyError, match="genuine future bug"):
        run_gated_prebuild(
            underlyings=[_spot("NSE-NIFTY")],
            materialize_spot=lambda _i: _ok(1),
            futures_contracts_for=lambda _i: ["FUT-X"],
            materialize_future=materialize_future,
        )


# ── poisoned-partition quarantine + global-stop boundary (DEC-LAKE-WRITE-UNIQUE-001) ──


def test_spot_resample_input_error_is_quarantined_not_stage_abort() -> None:
    """FAILS on unfixed main: ResampleInputError escaped the (ValueError, OSError)
    catch and ABORTED the whole pre-build stage — now the poisoned symbol is
    quarantined + reported and every other symbol still runs."""
    from brutex.data.errors import ResampleInputError

    def materialize_spot(inst: Instrument) -> MaterializeResult:
        if inst.symbol_name == "POISONED":
            raise ResampleInputError("ts_utc must be STRICTLY increasing")
        return _ok(5)

    report = run_gated_prebuild(
        underlyings=[_spot("GOOD-A"), _spot("POISONED"), _spot("GOOD-B")],
        materialize_spot=materialize_spot,
        futures_contracts_for=_no_futures,
        materialize_future=_raise_future,
    )

    spot = report.phases[0]
    assert spot.attempted == ("GOOD-A", "POISONED", "GOOD-B")
    assert spot.succeeded == ("GOOD-A", "GOOD-B")  # the healthy symbols still built
    assert spot.failed == (("POISONED", "ResampleInputError: ts_utc must be STRICTLY increasing"),)
    assert report.halted_after == "spot"  # the gate still holds on a failing phase


def test_futures_resample_input_error_is_quarantined() -> None:
    from brutex.data.errors import ResampleInputError

    def materialize_future(contract: str) -> MaterializeResult:
        if contract == "FUT-POISONED":
            raise ResampleInputError("duplicate ts_utc")
        return _ok(7)

    report = run_gated_prebuild(
        underlyings=[_spot("NSE-NIFTY")],
        materialize_spot=lambda _i: _ok(3),
        futures_contracts_for=lambda _i: ["FUT-OK", "FUT-POISONED"],
        materialize_future=materialize_future,
    )

    futures = report.phases[1]
    assert futures.succeeded == ("FUT-OK",)
    assert futures.failed == (("FUT-POISONED", "ResampleInputError: duplicate ts_utc"),)


def test_futures_discovery_resample_input_error_is_quarantined() -> None:
    from brutex.data.errors import ResampleInputError

    def futures_for(_inst: Instrument) -> list[str]:
        raise ResampleInputError("poisoned discovery read")

    report = run_gated_prebuild(
        underlyings=[_spot("NSE-NIFTY")],
        materialize_spot=lambda _i: _ok(3),
        futures_contracts_for=futures_for,
        materialize_future=_raise_future,
    )

    futures = report.phases[1]
    assert futures.failed == (("NSE-NIFTY", "ResampleInputError: poisoned discovery read"),)


def test_lake_write_halt_propagates_through_spot_phase() -> None:
    """Regression pin: the widened per-symbol quarantine catches the NARROW
    ResampleInputError subclass, NOT DataLakeError broadly — LakeWriteHalt
    (disk-full / OOM, a pull-wide global stop) STILL propagates."""
    from brutex.data.errors import LakeWriteHalt

    def materialize_spot(_inst: Instrument) -> MaterializeResult:
        raise LakeWriteHalt("disk_full")

    with pytest.raises(LakeWriteHalt, match="disk_full"):
        run_gated_prebuild(
            underlyings=[_spot("X")],
            materialize_spot=materialize_spot,
            futures_contracts_for=_no_futures,
            materialize_future=_raise_future,
        )


def test_lake_write_halt_propagates_through_futures_phase() -> None:
    from brutex.data.errors import LakeWriteHalt

    def materialize_future(_c: str) -> MaterializeResult:
        raise LakeWriteHalt("out_of_memory")

    with pytest.raises(LakeWriteHalt, match="out_of_memory"):
        run_gated_prebuild(
            underlyings=[_spot("NSE-NIFTY")],
            materialize_spot=lambda _i: _ok(1),
            futures_contracts_for=lambda _i: ["FUT-X"],
            materialize_future=materialize_future,
        )


def test_empty_selection_runs_three_empty_phases() -> None:
    report = run_gated_prebuild(
        underlyings=[],
        materialize_spot=lambda _i: _ok(1),
        futures_contracts_for=_no_futures,
        materialize_future=_raise_future,
    )
    assert report.ok
    assert report.halted_after is None
    assert [p.phase for p in report.phases] == ["spot", "futures", "options"]
    assert all(p.attempted == () for p in report.phases)
    assert report.total_bars == 0


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
