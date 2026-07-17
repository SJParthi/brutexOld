"""Multi-symbol pre-build orchestrator tests (DEC-PREBUILD-SELECT-001 + DEC-PIPELINE-AUTOPREBUILD-001).

100% line+branch over ``brutex.dashboard.gated_prebuild``: the SPOT → FUTURES →
OPTIONS phase order, the NON-HALTING per-symbol quarantine
(DEC-PIPELINE-AUTOPREBUILD-001 — "one bad symbol never aborts a stage": a
per-symbol failure is recorded, the phase continues to the next symbol AND the
chain continues to the later phases; the old halt-on-first-phase-failure gate +
its ``halted_after`` field are REMOVED), the FUTURES + OPTIONS contract
discovery from the F&O candle-lake cascade, the real OPTIONS phase (and its
reported no-op when the options callables are not injected), the per-phase
attempted/succeeded/failed accounting, the underlying-token matching, the
global-stop propagation boundary, and the report ``to_dict`` / ``ok`` /
``total_bars`` projections.

Pure — injected ``materialize_spot`` / ``futures_contracts_for`` /
``materialize_future`` / ``options_contracts_for`` / ``materialize_option``
fakes (return a ``MaterializeResult`` or raise). No lake / DuckDB / FastAPI.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from brutex.dashboard.fno_cascade import FnoContract, build_fno_cascade
from brutex.dashboard.gated_prebuild import (
    OPTIONS_PREBUILD_SKIPPED_NOTE,
    PREBUILD_PHASE_ORDER,
    GatedPrebuildReport,
    PhasePrebuildReport,
    discover_futures_contracts,
    discover_option_contracts,
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


def _no_contracts(_inst: Instrument) -> list[str]:
    return []


def _raise_contract(_c: str) -> MaterializeResult:  # pragma: no cover — only used as an unused arg
    raise AssertionError("contract materialise should not be called")


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


# ── futures + options discovery from the cascade ───────────────────────────


def _fno_datasets() -> list[dict[str, object]]:
    """Two NIFTY futures (two expiries) + a CE and a PE, across 1m intervals."""
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
        {
            "symbol": "NSE-NIFTY-30Jan25-20000-PE",
            "exchange": "NSE",
            "underlying": "NIFTY",
            "expiry": "2025-01-30",
            "strike": "20000",
            "option_type": "PE",
            "instrument_type": "PE",
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


class TestDiscoverOptionContracts:
    """DEC-PIPELINE-AUTOPREBUILD-001: CE/PE discovery, the futures twin."""

    def test_finds_only_ce_pe_for_underlying(self) -> None:
        cascade = build_fno_cascade(_fno_datasets())
        found = discover_option_contracts(_spot("NSE-NIFTY"), cascade)
        symbols = {c.symbol for c in found}
        assert symbols == {"NSE-NIFTY-30Jan25-20000-CE", "NSE-NIFTY-30Jan25-20000-PE"}
        assert all(c.instrument_type in ("CE", "PE") for c in found)

    def test_never_returns_futures(self) -> None:
        cascade = build_fno_cascade(_fno_datasets())
        found = discover_option_contracts(_spot("NSE-NIFTY"), cascade)
        assert all(c.instrument_type != "FUT" for c in found)

    def test_unknown_underlying_returns_empty(self) -> None:
        cascade = build_fno_cascade(_fno_datasets())
        assert discover_option_contracts(_spot("NSE-BANKNIFTY"), cascade) == ()

    def test_empty_cascade_returns_empty(self) -> None:
        assert discover_option_contracts(_spot("NSE-NIFTY"), build_fno_cascade([])) == ()


# ── the run: all phases, non-halting quarantine ────────────────────────────


def test_all_phases_run_when_everything_succeeds() -> None:
    underlyings = [_spot("NSE-NIFTY")]
    spot_calls: list[str] = []
    fut_calls: list[str] = []
    opt_calls: list[str] = []

    def materialize_spot(inst: Instrument) -> MaterializeResult:
        spot_calls.append(inst.symbol_name)
        return _ok(10)

    def futures_for(_inst: Instrument) -> list[str]:
        return ["NSE-NIFTY-30Jan25-FUT", "NSE-NIFTY-27Feb25-FUT"]

    def materialize_future(contract: str) -> MaterializeResult:
        fut_calls.append(contract)
        return _ok(4)

    def options_for(_inst: Instrument) -> list[str]:
        return ["NSE-NIFTY-30Jan25-20000-CE"]

    def materialize_option(contract: str) -> MaterializeResult:
        opt_calls.append(contract)
        return _ok(2)

    report = run_gated_prebuild(
        underlyings=underlyings,
        materialize_spot=materialize_spot,
        futures_contracts_for=futures_for,
        materialize_future=materialize_future,
        options_contracts_for=options_for,
        materialize_option=materialize_option,
        source="pipeline",
    )

    assert report.ok
    assert report.source == "pipeline"
    assert [p.phase for p in report.phases] == ["spot", "futures", "options"]
    spot, futures, options = report.phases
    assert spot.succeeded == ("NSE-NIFTY",)
    assert spot_calls == ["NSE-NIFTY"]
    assert futures.attempted == ("NSE-NIFTY-30Jan25-FUT", "NSE-NIFTY-27Feb25-FUT")
    assert futures.succeeded == ("NSE-NIFTY-30Jan25-FUT", "NSE-NIFTY-27Feb25-FUT")
    assert fut_calls == ["NSE-NIFTY-30Jan25-FUT", "NSE-NIFTY-27Feb25-FUT"]
    assert options.attempted == ("NSE-NIFTY-30Jan25-20000-CE",)
    assert options.succeeded == ("NSE-NIFTY-30Jan25-20000-CE",)
    assert opt_calls == ["NSE-NIFTY-30Jan25-20000-CE"]
    assert options.total_bars == 2
    assert report.total_bars == 10 + 4 + 4 + 2


def test_spot_failure_does_not_halt_futures_or_options() -> None:
    """REGRESSION PIN (DEC-PIPELINE-AUTOPREBUILD-001): a failing SPOT phase no
    longer halts the chain — FUTURES + OPTIONS still run, the failure stays
    honestly reported in the spot phase's ``failed``.

    Under the superseded DEC-PREBUILD-SELECT-001 gate this exact scenario
    stopped after spot (``halted_after == "spot"``, later phases absent).
    """
    fut_called = {"n": 0}
    opt_called = {"n": 0}

    def materialize_spot(inst: Instrument) -> MaterializeResult:
        raise ValueError("no stored 1minute bars")

    def futures_for(_inst: Instrument) -> list[str]:
        fut_called["n"] += 1
        return []

    def options_for(_inst: Instrument) -> list[str]:
        opt_called["n"] += 1
        return []

    report = run_gated_prebuild(
        underlyings=[_spot("BAD")],
        materialize_spot=materialize_spot,
        futures_contracts_for=futures_for,
        materialize_future=_raise_contract,
        options_contracts_for=options_for,
        materialize_option=_raise_contract,
    )

    assert not report.ok
    assert [p.phase for p in report.phases] == ["spot", "futures", "options"]
    assert fut_called["n"] == 1  # the chain CONTINUED to futures
    assert opt_called["n"] == 1  # ... and to options
    assert report.phases[0].failed == (("BAD", "ValueError: no stored 1minute bars"),)


def test_spot_collects_all_failures_and_chain_continues() -> None:
    def materialize_spot(inst: Instrument) -> MaterializeResult:
        if inst.symbol_name in ("A", "C"):
            raise OSError(f"bad {inst.symbol_name}")
        return _ok(5)

    report = run_gated_prebuild(
        underlyings=[_spot("A"), _spot("B"), _spot("C")],
        materialize_spot=materialize_spot,
        futures_contracts_for=_no_contracts,
        materialize_future=_raise_contract,
    )

    spot = report.phases[0]
    assert spot.attempted == ("A", "B", "C")
    assert spot.succeeded == ("B",)
    assert [s for s, _ in spot.failed] == ["A", "C"]
    # All three phases still ran (non-halting).
    assert [p.phase for p in report.phases] == ["spot", "futures", "options"]


def test_futures_contract_failure_does_not_halt_options() -> None:
    opt_calls: list[str] = []

    def futures_for(_inst: Instrument) -> list[str]:
        return ["FUT-OK", "FUT-BAD"]

    def materialize_future(contract: str) -> MaterializeResult:
        if contract == "FUT-BAD":
            raise OSError("lake read fault")
        return _ok(7)

    def materialize_option(contract: str) -> MaterializeResult:
        opt_calls.append(contract)
        return _ok(1)

    report = run_gated_prebuild(
        underlyings=[_spot("NSE-NIFTY")],
        materialize_spot=lambda _i: _ok(3),
        futures_contracts_for=futures_for,
        materialize_future=materialize_future,
        options_contracts_for=lambda _i: ["OPT-1"],
        materialize_option=materialize_option,
    )

    assert not report.ok
    assert [p.phase for p in report.phases] == ["spot", "futures", "options"]
    futures = report.phases[1]
    assert futures.attempted == ("FUT-OK", "FUT-BAD")
    assert futures.succeeded == ("FUT-OK",)
    assert futures.failed == (("FUT-BAD", "OSError: lake read fault"),)
    assert opt_calls == ["OPT-1"]  # the OPTIONS phase still ran + materialised


def test_futures_discovery_failure_is_recorded() -> None:
    def futures_for(inst: Instrument) -> list[str]:
        raise ValueError("cascade build failed")

    report = run_gated_prebuild(
        underlyings=[_spot("NSE-NIFTY")],
        materialize_spot=lambda _i: _ok(3),
        futures_contracts_for=futures_for,
        materialize_future=_raise_contract,
    )

    futures = report.phases[1]
    assert futures.failed == (("NSE-NIFTY", "ValueError: cascade build failed"),)
    assert [p.phase for p in report.phases] == ["spot", "futures", "options"]


def test_no_futures_found_is_clean_skip_with_note() -> None:
    report = run_gated_prebuild(
        underlyings=[_spot("NSE-NIFTY")],
        materialize_spot=lambda _i: _ok(3),
        futures_contracts_for=_no_contracts,
        materialize_future=_raise_contract,
    )

    assert report.ok
    futures = report.phases[1]
    assert futures.attempted == ()
    assert futures.succeeded == ()
    assert futures.total_bars == 0
    assert "no futures contracts found" in futures.note
    assert report.phases[-1].phase == "options"


def test_spot_genuine_bug_propagates_not_swallowed() -> None:
    def materialize_spot(inst: Instrument) -> MaterializeResult:
        raise KeyError("genuine bug")

    with pytest.raises(KeyError, match="genuine bug"):
        run_gated_prebuild(
            underlyings=[_spot("X")],
            materialize_spot=materialize_spot,
            futures_contracts_for=_no_contracts,
            materialize_future=_raise_contract,
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


# ── the options phase: the by-design no-op is the PRODUCTION shape ─────────
# (DEC-OPTIONS-PREBUILD-RETIRE-001 — the operator ruling of 2026-07-15 as
# relayed via the coordinator, ADR-0021 RULING B, draft PR #1555: options are
# 1-minute-native; the injected arm below is RETAINED STRUCTURAL.)


class TestOptionsPhase:
    def test_options_noop_when_callables_not_injected(self) -> None:
        """No options callables (the PRODUCTION shape since the retirement) ⇒
        the explicit REPORTED by-design no-op: nothing attempted, zero work,
        the honest law-naming note — never a fake "0 contracts found"."""
        report = run_gated_prebuild(
            underlyings=[_spot("NSE-NIFTY")],
            materialize_spot=lambda _i: _ok(3),
            futures_contracts_for=_no_contracts,
            materialize_future=_raise_contract,
        )
        options = report.phases[-1]
        assert options.phase == "options"
        assert options.attempted == ()  # nothing is in scope BY DESIGN
        assert options.succeeded == ()
        assert options.failed == ()
        assert options.total_bars == 0
        assert options.note == OPTIONS_PREBUILD_SKIPPED_NOTE
        assert "1-minute-native" in options.note
        assert "DEC-OPTIONS-PREBUILD-RETIRE-001" in options.note

    def test_options_noop_when_only_discovery_injected(self) -> None:
        """EITHER callable missing ⇒ the no-op (never a half-wired crash)."""
        report = run_gated_prebuild(
            underlyings=[_spot("NSE-NIFTY")],
            materialize_spot=lambda _i: _ok(3),
            futures_contracts_for=_no_contracts,
            materialize_future=_raise_contract,
            options_contracts_for=lambda _i: ["OPT-1"],
            materialize_option=None,
        )
        assert report.phases[-1].note == OPTIONS_PREBUILD_SKIPPED_NOTE

    def test_options_contract_quarantine_continues_phase(self) -> None:
        """One bad option contract never aborts the options stage."""

        def materialize_option(contract: str) -> MaterializeResult:
            if contract == "OPT-BAD":
                raise ValueError("poisoned option partition")
            return _ok(2)

        report = run_gated_prebuild(
            underlyings=[_spot("NSE-NIFTY")],
            materialize_spot=lambda _i: _ok(3),
            futures_contracts_for=_no_contracts,
            materialize_future=_raise_contract,
            options_contracts_for=lambda _i: ["OPT-A", "OPT-BAD", "OPT-B"],
            materialize_option=materialize_option,
        )
        options = report.phases[-1]
        assert options.attempted == ("OPT-A", "OPT-BAD", "OPT-B")
        assert options.succeeded == ("OPT-A", "OPT-B")
        assert options.failed == (("OPT-BAD", "ValueError: poisoned option partition"),)
        assert options.total_bars == 4

    def test_options_discovery_failure_is_recorded(self) -> None:
        def options_for(_inst: Instrument) -> list[str]:
            raise OSError("options cascade read fault")

        report = run_gated_prebuild(
            underlyings=[_spot("NSE-NIFTY")],
            materialize_spot=lambda _i: _ok(3),
            futures_contracts_for=_no_contracts,
            materialize_future=_raise_contract,
            options_contracts_for=options_for,
            materialize_option=lambda _c: _ok(1),
        )
        options = report.phases[-1]
        assert options.failed == (("NSE-NIFTY", "OSError: options cascade read fault"),)

    def test_no_options_found_is_clean_skip_with_note(self) -> None:
        report = run_gated_prebuild(
            underlyings=[_spot("NSE-NIFTY")],
            materialize_spot=lambda _i: _ok(3),
            futures_contracts_for=_no_contracts,
            materialize_future=_raise_contract,
            options_contracts_for=_no_contracts,
            materialize_option=lambda _c: _ok(1),
        )
        options = report.phases[-1]
        assert options.attempted == ()
        assert "no option contracts found" in options.note

    def test_options_genuine_bug_propagates(self) -> None:
        def materialize_option(_c: str) -> MaterializeResult:
            raise TypeError("genuine option bug")

        with pytest.raises(TypeError, match="genuine option bug"):
            run_gated_prebuild(
                underlyings=[_spot("NSE-NIFTY")],
                materialize_spot=lambda _i: _ok(1),
                futures_contracts_for=_no_contracts,
                materialize_future=_raise_contract,
                options_contracts_for=lambda _i: ["OPT-X"],
                materialize_option=materialize_option,
            )


# ── poisoned-partition quarantine + global-stop boundary (DEC-LAKE-WRITE-UNIQUE-001) ──


def test_spot_resample_input_error_is_quarantined_not_stage_abort() -> None:
    """A poisoned symbol is quarantined + reported; every other symbol still runs."""
    from brutex.data.errors import ResampleInputError

    def materialize_spot(inst: Instrument) -> MaterializeResult:
        if inst.symbol_name == "POISONED":
            raise ResampleInputError("ts_utc must be STRICTLY increasing")
        return _ok(5)

    report = run_gated_prebuild(
        underlyings=[_spot("GOOD-A"), _spot("POISONED"), _spot("GOOD-B")],
        materialize_spot=materialize_spot,
        futures_contracts_for=_no_contracts,
        materialize_future=_raise_contract,
    )

    spot = report.phases[0]
    assert spot.attempted == ("GOOD-A", "POISONED", "GOOD-B")
    assert spot.succeeded == ("GOOD-A", "GOOD-B")  # the healthy symbols still built
    assert spot.failed == (("POISONED", "ResampleInputError: ts_utc must be STRICTLY increasing"),)
    # Non-halting (DEC-PIPELINE-AUTOPREBUILD-001): the later phases still ran.
    assert [p.phase for p in report.phases] == ["spot", "futures", "options"]


def test_per_symbol_data_errors_includes_arithmetic_error() -> None:
    """DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001: ``ArithmeticError`` (which
    covers ``decimal.InvalidOperation``) is a member of the per-symbol
    quarantine set, so a NaN-bearing partition's Decimal-conversion fault
    quarantines the ONE symbol instead of aborting the whole prebuild."""
    import decimal

    from brutex.dashboard.gated_prebuild import per_symbol_data_errors

    members = per_symbol_data_errors()
    assert ArithmeticError in members
    assert issubclass(decimal.InvalidOperation, ArithmeticError)


def test_spot_invalid_operation_is_quarantined_not_run_abort() -> None:
    """FAILS-BEFORE (DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001, defect #3): a
    NaN-bearing stored partition raises ``decimal.InvalidOperation`` out of
    the per-symbol Decimal price conversion (``frame_to_candle_bars`` / the
    ``CandleBar`` construction) — an ``ArithmeticError``, NOT a ``ValueError``,
    so pre-fix it ESCAPED the quarantine set and this exact call ABORTED the
    whole prebuild with a raw uncaught ``InvalidOperation``."""
    import decimal

    def materialize_spot(inst: Instrument) -> MaterializeResult:
        if inst.symbol_name == "NANSYM":
            raise decimal.InvalidOperation("NaN price in Decimal ordering compare")
        return _ok(5)

    report = run_gated_prebuild(
        underlyings=[_spot("GOOD-A"), _spot("NANSYM"), _spot("GOOD-B")],
        materialize_spot=materialize_spot,
        futures_contracts_for=_no_contracts,
        materialize_future=_raise_contract,
    )

    spot = report.phases[0]
    assert spot.succeeded == ("GOOD-A", "GOOD-B")  # the healthy symbols still built
    assert [s for s, _ in spot.failed] == ["NANSYM"]  # the poisoned symbol is NAMED
    assert "InvalidOperation" in spot.failed[0][1]
    # Non-halting: the later phases still ran.
    assert [p.phase for p in report.phases] == ["spot", "futures", "options"]


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
        materialize_future=_raise_contract,
    )

    futures = report.phases[1]
    assert futures.failed == (("NSE-NIFTY", "ResampleInputError: poisoned discovery read"),)


def test_lake_write_halt_propagates_through_spot_phase() -> None:
    """Regression pin: the per-symbol quarantine catches the NARROW
    ResampleInputError subclass, NOT DataLakeError broadly — LakeWriteHalt
    (disk-full / OOM, a pull-wide global stop) STILL propagates."""
    from brutex.data.errors import LakeWriteHalt

    def materialize_spot(_inst: Instrument) -> MaterializeResult:
        raise LakeWriteHalt("disk_full")

    with pytest.raises(LakeWriteHalt, match="disk_full"):
        run_gated_prebuild(
            underlyings=[_spot("X")],
            materialize_spot=materialize_spot,
            futures_contracts_for=_no_contracts,
            materialize_future=_raise_contract,
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


def test_lake_write_halt_propagates_through_options_phase() -> None:
    from brutex.data.errors import LakeWriteHalt

    def materialize_option(_c: str) -> MaterializeResult:
        raise LakeWriteHalt("disk_full")

    with pytest.raises(LakeWriteHalt, match="disk_full"):
        run_gated_prebuild(
            underlyings=[_spot("NSE-NIFTY")],
            materialize_spot=lambda _i: _ok(1),
            futures_contracts_for=_no_contracts,
            materialize_future=_raise_contract,
            options_contracts_for=lambda _i: ["OPT-X"],
            materialize_option=materialize_option,
        )


def test_spot_noop_phase_when_materialize_spot_is_none() -> None:
    """[round-6 two-button split] ``materialize_spot=None`` makes the SPOT
    phase an explicit REPORTED no-op carrying the COMMON by-design note
    (the options-no-op posture: never a silent skip) while the contract
    phases run for real over the same underlyings."""
    from brutex.dashboard.gated_prebuild import SPOT_PREBUILD_SKIPPED_NOTE

    contracts = {"NSE-NIFTY": ["NSE-NIFTY-30Jan25-FUT"]}
    report = run_gated_prebuild(
        underlyings=[_spot("NSE-NIFTY")],
        materialize_spot=None,
        futures_contracts_for=lambda inst: contracts.get(inst.symbol_name, []),
        materialize_future=lambda symbol: _ok(4),
        options_contracts_for=lambda inst: [],
        materialize_option=lambda symbol: _ok(4),
        source="pipeline",
    )
    spot_phase, futures_phase, options_phase = report.phases
    assert spot_phase.phase == "spot"
    assert spot_phase.attempted == ()
    assert spot_phase.succeeded == ()
    assert spot_phase.failed == ()
    assert spot_phase.total_bars == 0
    assert spot_phase.note == SPOT_PREBUILD_SKIPPED_NOTE
    assert "skipped by design (F&O-only scope)" in spot_phase.note
    assert futures_phase.succeeded == ("NSE-NIFTY-30Jan25-FUT",)  # F&O phases REAL
    assert options_phase.attempted == ()
    assert report.ok is True


def test_empty_selection_runs_three_empty_phases() -> None:
    report = run_gated_prebuild(
        underlyings=[],
        materialize_spot=lambda _i: _ok(1),
        futures_contracts_for=_no_contracts,
        materialize_future=_raise_contract,
    )
    assert report.ok
    assert [p.phase for p in report.phases] == ["spot", "futures", "options"]
    assert all(p.attempted == () for p in report.phases)
    assert report.total_bars == 0


# ── corrupt Parquet month-file quarantine (DEC-PREBUILD-CORRUPT-SKIP-001) ──


def test_spot_corrupt_parquet_month_file_is_quarantined_not_run_abort(tmp_path: Path) -> None:
    """REGRESSION PIN (DEC-PREBUILD-CORRUPT-SKIP-001): a genuinely corrupt
    stored Parquet month file for ONE symbol raises a REAL
    ``polars.exceptions.ComputeError`` — before the fix that unwound
    ``run_gated_prebuild`` entirely (crashed=ComputeError, symbols_built=0);
    now THAT symbol is quarantined, logged in ``failed``, and every healthy
    symbol + every later phase still runs."""
    pl = pytest.importorskip("polars")

    garbage = tmp_path / "2025" / "01.parquet"
    garbage.parent.mkdir(parents=True)
    garbage.write_bytes(b"NOT A PARQUET FILE \x00\xff garbage bytes")

    def materialize_spot(inst: Instrument) -> MaterializeResult:
        if inst.symbol_name == "CORRUPT":
            pl.read_parquet(garbage)  # raises the real ComputeError
            raise AssertionError("unreachable — the read above must raise")
        return _ok(5)

    report = run_gated_prebuild(
        underlyings=[_spot("GOOD-A"), _spot("CORRUPT"), _spot("GOOD-B")],
        materialize_spot=materialize_spot,
        futures_contracts_for=_no_contracts,
        materialize_future=_raise_contract,
    )

    spot = report.phases[0]
    assert spot.attempted == ("GOOD-A", "CORRUPT", "GOOD-B")
    assert spot.succeeded == ("GOOD-A", "GOOD-B")  # the healthy symbols still built
    assert [s for s, _ in spot.failed] == ["CORRUPT"]
    assert spot.failed[0][1].startswith("ComputeError:")
    # Non-halting: the run RETURNED and the later phases still ran.
    assert [p.phase for p in report.phases] == ["spot", "futures", "options"]


def test_futures_contract_polars_error_is_quarantined() -> None:
    pl = pytest.importorskip("polars")

    def materialize_future(contract: str) -> MaterializeResult:
        if contract == "FUT-CORRUPT":
            raise pl.exceptions.ComputeError("could not read parquet")
        return _ok(7)

    report = run_gated_prebuild(
        underlyings=[_spot("NSE-NIFTY")],
        materialize_spot=lambda _i: _ok(3),
        futures_contracts_for=lambda _i: ["FUT-OK", "FUT-CORRUPT"],
        materialize_future=materialize_future,
    )

    futures = report.phases[1]
    assert futures.succeeded == ("FUT-OK",)
    assert futures.failed == (("FUT-CORRUPT", "ComputeError: could not read parquet"),)
    assert [p.phase for p in report.phases] == ["spot", "futures", "options"]


def test_futures_discovery_polars_error_is_quarantined() -> None:
    pl = pytest.importorskip("polars")

    def futures_for(_inst: Instrument) -> list[str]:
        raise pl.exceptions.ComputeError("corrupt cascade partition")

    report = run_gated_prebuild(
        underlyings=[_spot("NSE-NIFTY")],
        materialize_spot=lambda _i: _ok(3),
        futures_contracts_for=futures_for,
        materialize_future=_raise_contract,
    )

    futures = report.phases[1]
    assert futures.failed == (("NSE-NIFTY", "ComputeError: corrupt cascade partition"),)


def test_per_symbol_set_wheel_free_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wheel-free contract: with polars ABSENT, ``_per_symbol_data_errors()``
    falls back to the documented ``(OSError,)``-based tuple (no PolarsError can
    exist there — nothing lost) and the quarantine still works at except time."""
    import sys

    from brutex.dashboard.gated_prebuild import (
        _per_symbol_data_errors,  # pyright: ignore[reportPrivateUsage]
    )

    monkeypatch.setitem(sys.modules, "polars", None)  # `import polars` → ModuleNotFoundError

    from brutex.data.errors import ResampleInputError

    # CONSCIOUSLY UPDATED (DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001):
    # ArithmeticError joined the set so a NaN partition's InvalidOperation
    # quarantines the symbol instead of aborting the run.
    assert _per_symbol_data_errors() == (ValueError, ArithmeticError, ResampleInputError, OSError)

    # The except sites still quarantine through the fallback tuple.
    def materialize_spot(inst: Instrument) -> MaterializeResult:
        if inst.symbol_name == "BAD":
            raise ValueError("no stored 1minute bars")
        return _ok(5)

    report = run_gated_prebuild(
        underlyings=[_spot("BAD"), _spot("GOOD")],
        materialize_spot=materialize_spot,
        futures_contracts_for=_no_contracts,
        materialize_future=_raise_contract,
    )
    spot = report.phases[0]
    assert spot.succeeded == ("GOOD",)
    assert spot.failed == (("BAD", "ValueError: no stored 1minute bars"),)


def test_per_symbol_set_pins_global_stop_and_bugs_outside() -> None:
    """Set-level pin: PolarsError IS quarantined; LakeWriteHalt (global stop)
    and genuine bugs (KeyError/TypeError) are NOT — complementing the
    end-to-end propagation tests above."""
    pl = pytest.importorskip("polars")

    from brutex.dashboard.gated_prebuild import (
        _per_symbol_data_errors,  # pyright: ignore[reportPrivateUsage]
    )
    from brutex.data.errors import LakeWriteHalt

    members = _per_symbol_data_errors()
    assert pl.exceptions.PolarsError in members
    assert OSError in members
    assert not issubclass(LakeWriteHalt, members)
    assert not issubclass(KeyError, members)
    assert not issubclass(TypeError, members)


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
            source="single",
        )
        d = report.to_dict()
        assert d["ok"] is True
        assert d["source"] == "single"
        assert d["total_bars"] == 4
        assert d["phases"][0]["phase"] == "spot"
        assert "halted_after" not in d  # the halt gate is removed (AUTOPREBUILD-001)

    def test_report_ok_false_when_any_phase_failed(self) -> None:
        report = GatedPrebuildReport(
            phases=(
                PhasePrebuildReport("spot", ("A",), ("A",), (), 1),
                PhasePrebuildReport("futures", ("F",), (), (("F", "ValueError: x"),), 0),
            ),
        )
        assert report.ok is False

    def test_default_report_is_empty_ok(self) -> None:
        report = GatedPrebuildReport()
        assert report.ok
        assert report.total_bars == 0
        assert report.phases == ()


# ── the vendor-empty no-source classification (DEC-SPOTONLY-PREBUILD-EMPTY-EXCLUDE-001) ──


class TestSpotNoSourceClassification:
    """The SPOT phase classifies a whole-partition zero-stored-bars read
    (:class:`NoStored1mBarsError`) as ``no_source`` — never a warm-withholding
    failure; every OTHER data error keeps today's quarantine semantics."""

    def test_no_stored_bars_classified_no_source_not_failed(self) -> None:
        """T4 [fails-before] — one of three spot targets reads no-source: it
        joins ``no_source`` (still ``attempted``), never ``failed``; ``ok``
        stays True; the others succeed; ONE per-symbol INFO event."""
        from brutex.data.timeframe_materialize import NoStored1mBarsError
        from structlog.testing import capture_logs

        def materialize_spot(inst: Instrument) -> MaterializeResult:
            if inst.symbol_name == "EMPTY":
                raise NoStored1mBarsError("no stored 1minute bars for NSE/CASH/EMPTY — …")
            return _ok(5)

        with capture_logs() as logs:
            report = run_gated_prebuild(
                underlyings=[_spot("A"), _spot("EMPTY"), _spot("B")],
                materialize_spot=materialize_spot,
                futures_contracts_for=_no_contracts,
                materialize_future=_raise_contract,
            )
        spot = report.phases[0]
        assert spot.no_source == ("EMPTY",)
        assert spot.failed == ()
        assert spot.ok is True
        assert report.ok is True  # the movers-warm gate upstream stays OPEN
        assert spot.attempted == ("A", "EMPTY", "B")  # honest — it WAS attempted
        assert spot.succeeded == ("A", "B")
        infos = [e for e in logs if e["event"] == "universe.prebuild.spot_no_source"]
        assert len(infos) == 1
        assert infos[0]["symbol"] == "EMPTY"
        assert infos[0]["log_level"] == "info"  # expected vendor behavior — never a WARN
        # And NEVER double-counted as a spot_failed WARN.
        assert not [e for e in logs if e["event"] == "universe.prebuild.spot_failed"]

    def test_plain_value_error_still_fails_pins_except_order(self) -> None:
        """T5 — a plain ``ValueError`` (a NON-no-source data fault) still
        quarantines into ``failed``; together with T4 this pins the except
        ORDER (``NoStored1mBarsError`` ⊂ ``ValueError`` — swapping the
        clauses reclassifies T4's symbol as failed) AND kills the
        widened-catch mutant (classifying every ValueError no_source)."""

        def materialize_spot(inst: Instrument) -> MaterializeResult:
            raise ValueError("poisoned partition")

        report = run_gated_prebuild(
            underlyings=[_spot("BAD")],
            materialize_spot=materialize_spot,
            futures_contracts_for=_no_contracts,
            materialize_future=_raise_contract,
        )
        spot = report.phases[0]
        assert spot.failed == (("BAD", "ValueError: poisoned partition"),)
        assert spot.no_source == ()
        assert report.ok is False

    def test_contract_phase_no_stored_bars_stays_failure(self) -> None:
        """T6 — the deliberate SPOT scope: a ``NoStored1mBarsError`` from a
        contract materialise is still a FAILURE (a discovered-with-files
        contract whose partition vanished by read time is a heal/TOCTOU
        anomaly worth a red prebuild, not vendor-empty-by-design)."""
        from brutex.data.timeframe_materialize import NoStored1mBarsError

        def materialize_future(contract: str) -> MaterializeResult:
            raise NoStored1mBarsError("no stored 1minute bars for NSE/FNO/FUT-1 — …")

        report = run_gated_prebuild(
            underlyings=[_spot("NSE-NIFTY")],
            materialize_spot=lambda _i: _ok(3),
            futures_contracts_for=lambda _i: ["FUT-1"],
            materialize_future=materialize_future,
        )
        futures = report.phases[1]
        assert [s for s, _ in futures.failed] == ["FUT-1"]
        assert futures.no_source == ()
        assert report.ok is False  # the warm stays withheld — unchanged law

    def test_phase_report_no_source_default_and_to_dict_both_arms(self) -> None:
        """T7 — the appended field defaults ``()`` and ``to_dict`` is
        EMIT-ON-NONEMPTY (the default payload above stays byte-identical —
        ``test_phase_to_dict``'s exact-dict pin is the absent arm)."""
        assert PhasePrebuildReport("spot", ("A",), ("A",), (), 1).no_source == ()
        with_no_source = PhasePrebuildReport(
            phase="spot",
            attempted=("A", "EMPTY"),
            succeeded=("A",),
            failed=(),
            total_bars=3,
            no_source=("EMPTY",),
        )
        assert with_no_source.ok is True  # no_source never flips ok
        payload = with_no_source.to_dict()
        assert payload["no_source"] == ["EMPTY"]
        assert payload["ok"] is True
        clean = PhasePrebuildReport("spot", ("A",), ("A",), (), 1).to_dict()
        assert "no_source" not in clean  # emit-on-nonempty — the absent arm
