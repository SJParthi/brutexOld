"""Tests for `brutex.dashboard.option_pull_controller` — the options pull core.

100% line+branch via the synthetic dry-run path + injected fake client/sink:
the instrument→underlying mapping (IDX/EQ/blank/derivative) incl. the per-kind
expiry set (index WEEK+MONTH vs stock MONTH-only,
DEC-DHAN-STOCK-MONTHLY-ONLY-001), the report aggregation (completed series +
quarantined failures grouped by underlying), the dry-run default path, the
injected-factory (non-dry-run) branch, the empty-list no-op, and the
``to_dict`` projection.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.adapters.dhan.errors import DhanError
from brutex.adapters.dhan.option_ingest import JsonPoster, OptionBarSink
from brutex.adapters.dhan.option_synthetic import SyntheticOptionClient
from brutex.dashboard.option_pull_controller import (
    OptionsPullReport,
    OptionSymbolResult,
    _build_option_audit,
    _close_sink,
    _live_option_runtime,
    _LiveOptionRuntime,
    _option_audit_path,
    instrument_to_option_underlying,
    pull_options,
)
from brutex.data.option_sink import NullOptionBarSink
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.orchestrator.audit import read_entries, verify_chain
from structlog.testing import capture_logs

_NOW = datetime(2021, 1, 6, tzinfo=UTC)  # tiny window so the synthetic fan-out is fast


def _index(symbol: str = "13", exchange: Exchange = Exchange.NSE) -> Instrument:
    """A spot index underlying (IDX → OPTIDX / ATM±10)."""
    return Instrument(
        exch_id=exchange,
        security_id=int(symbol),
        symbol_name="NIFTY",
        display_name="NIFTY",
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


def _stock(symbol: str = "1333") -> Instrument:
    """A spot equity underlying (EQ → OPTSTK / ATM±3)."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=int(symbol),
        symbol_name="HDFCBANK",
        display_name="HDFCBANK",
        instrument=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
        isin="INE040A01034",
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=550,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


def _option_leg() -> Instrument:
    """A CE leg — never a valid options fan-out *underlying*."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=35044,
        symbol_name="NIFTY25MAR29050CE",
        display_name=None,
        instrument=InstrumentType.CE,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol="NIFTY",
        underlying_security_id=26000,
        lot_size=25,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=7200,
        sm_expiry_date=date(2025, 3, 27),
        strike_price=Decimal("29050"),
        asm_gsm_flag=False,
    )


# ── instrument_to_option_underlying ──────────────────────────────────────


class TestInstrumentToOptionUnderlying:
    def test_index_maps_to_optidx_wide_window(self) -> None:
        inst = _index(symbol="13", exchange=Exchange.NSE)
        u = instrument_to_option_underlying(inst)
        # security_id is the NUMERIC Dhan SECURITY_ID, NOT the engine
        # symbol_name string — the 252-failed root cause
        # (DEC-OPTION-SECURITY-ID-FIX-001 / ADR-0003: vendor_symbol dropped).
        assert u.security_id == "13"
        assert u.security_id == str(inst.security_id)
        assert u.security_id != inst.symbol_name
        assert u.instrument == "OPTIDX"
        assert u.exchange_segment == "NSE_FNO"
        assert u.strike_offsets == 10
        # An index keeps the full WEEK+MONTH expiry set (only some indices
        # carry weeklies; DEC-DHAN-STOCK-MONTHLY-ONLY-001).
        assert u.expiry_flags == ("WEEK", "MONTH")
        # The addressing fields promoted to the expired-options table
        # (DEC-EXPIRED-OPTIONS-TABLE-001) carry through from the instrument.
        assert u.exch_id == "NSE"
        assert u.underlying_symbol == inst.symbol_name

    def test_bse_index_maps_to_bse_fno(self) -> None:
        u = instrument_to_option_underlying(_index(symbol="51", exchange=Exchange.BSE))
        assert u.exchange_segment == "BSE_FNO"

    def test_stock_maps_to_optstk_narrow_window(self) -> None:
        u = instrument_to_option_underlying(_stock(symbol="1333"))
        assert u.instrument == "OPTSTK"
        assert u.exchange_segment == "NSE_FNO"
        assert u.strike_offsets == 3
        # A single stock is MONTHLY-only — NSE single stocks carry no weekly
        # series, so the WEEK leg is elided (DEC-DHAN-STOCK-MONTHLY-ONLY-001).
        assert u.expiry_flags == ("MONTH",)

    def test_nonpositive_security_id_raises(self) -> None:
        with pytest.raises(ValueError, match="valid numeric SECURITY_ID"):
            instrument_to_option_underlying(_index(symbol="0"))

    def test_derivative_leg_is_not_an_underlying(self) -> None:
        with pytest.raises(ValueError, match="only IDX / EQ"):
            instrument_to_option_underlying(_option_leg())


# ── pull_options ─────────────────────────────────────────────────────────


class TestPullOptions:
    def test_dry_run_single_index_counts_synthetic_bars(self) -> None:
        report = pull_options([_index()], lake_root=Path("/unused"), dry_run=True, now=_NOW)
        assert report.dry_run is True
        assert report.start_date == "2021-01-01"
        # No explicit to_date → default end is the previous day (now=01-06 →
        # 01-05); the in-progress current session is excluded by default (FIX 5).
        assert report.end_date == "2021-01-05"
        assert report.total_symbols == 1
        # 21 strikes × (WEEK 1..8 + MONTH 1..3 = 11 codes) × 2 types = 462 series,
        # all complete (DEC-DHAN-WEEKLY-EXPIRY-DEPTH-001).
        assert report.symbols[0].series_completed == 21 * 11 * 2
        assert report.symbols[0].series_failed == 0
        assert report.symbols[0].instrument == "OPTIDX"
        # Every series appends synthetic in-session bars → strictly positive.
        assert report.total_bars > 0
        assert report.total_windows == report.symbols[0].series_completed  # 1 window each

    def test_dry_run_bulk_mix_of_index_and_stock(self) -> None:
        report = pull_options(
            [_index(symbol="13"), _stock(symbol="1333")],
            lake_root=Path("/unused"),
            dry_run=True,
            now=_NOW,
        )
        assert report.total_symbols == 2
        by_symbol = {s.symbol: s for s in report.symbols}
        # Index keeps WEEK+MONTH (2 flags); a STOCK is MONTHLY-only — NSE single
        # stocks carry no weekly series (only some indices do), so the stock
        # fans MONTH only (1 flag): its series count DROPS vs the old
        # WEEK+MONTH fan-out — fewer wasted Dhan calls/quota
        # (DEC-DHAN-STOCK-MONTHLY-ONLY-001).
        # ATM±10, WEEK 1..8 + MONTH 1..3 (DEC-DHAN-WEEKLY-EXPIRY-DEPTH-001).
        assert by_symbol["13"].series_completed == 21 * 11 * 2
        assert by_symbol["1333"].series_completed == 7 * 1 * 3 * 2  # ATM±3, MONTH-only
        # The per-kind elision strictly REDUCES the stock fan-out (was 7*2*3*2).
        assert by_symbol["1333"].series_completed < 7 * 2 * 3 * 2

    def test_empty_list_is_clean_noop(self) -> None:
        report = pull_options([], lake_root=Path("/unused"), dry_run=True, now=_NOW)
        assert report.total_symbols == 0
        assert report.total_bars == 0
        assert report.total_series_completed == 0

    def test_injected_factory_non_dry_run_branch(self, tmp_path: Path) -> None:
        """A fake (live-shaped) factory exercises the not-dry-run branch with no network."""
        client = SyntheticOptionClient(bars_per_window=2)
        sink = NullOptionBarSink()

        captured: list[Path] = []

        def factory(lake_root: Path) -> tuple[JsonPoster, OptionBarSink]:
            captured.append(lake_root)
            return client, sink

        report = pull_options(
            [_index()],
            lake_root=tmp_path,
            dry_run=False,  # not dry-run, but injected factory ⇒ no live rate/network
            now=_NOW,
            client_factory=factory,
        )
        assert captured == [tmp_path]
        assert report.dry_run is False
        assert report.total_series_completed == 21 * 11 * 2  # WEEK 1..8 + MONTH 1..3
        # A live pull persists the skip-ledger so a re-pull skips the windows
        # already in the lake (DEC-FETCH-COVERAGE-WIRING-001).
        assert (tmp_path / "fetch_coverage.json").is_file()

    def test_terminal_failure_is_quarantined_per_underlying(self, tmp_path: Path) -> None:
        """A fake client that always errors terminally → every series quarantined."""

        class _AlwaysTerminal:
            def post_json(self, endpoint: str, body: Mapping[str, object]) -> dict[str, object]:
                del endpoint, body
                raise DhanError("DH-902 terminal")

        def factory(_lake_root: Path) -> tuple[JsonPoster, OptionBarSink]:
            return _AlwaysTerminal(), NullOptionBarSink()

        report = pull_options(
            [_stock(symbol="1333")],
            lake_root=tmp_path,
            dry_run=False,
            now=_NOW,
            client_factory=factory,
            expiry_flags=("WEEK",),
            expiry_codes=(1,),
            option_types=("CALL",),
        )
        # 7 strikes × 1 flag × 1 code × 1 type = 7 series, all terminal → quarantined.
        assert report.symbols[0].series_failed == 7
        assert report.symbols[0].series_completed == 0
        assert report.total_bars == 0

    def test_axis_overrides_shrink_the_fan_out(self) -> None:
        report = pull_options(
            [_index()],
            lake_root=Path("/unused"),
            dry_run=True,
            now=_NOW,
            expiry_flags=("WEEK",),
            expiry_codes=(1,),
            option_types=("CALL",),
        )
        assert report.symbols[0].series_completed == 21  # 21 strikes × 1 × 1 × 1

    def test_on_progress_forwarded_to_driver(self) -> None:
        """pull_options threads on_progress → fires once per underlying (dry-run)."""
        events: list[tuple[str, int, bool]] = []
        pull_options(
            [_index(symbol="13"), _stock(symbol="1333")],
            lake_root=Path("/unused"),
            dry_run=True,
            now=_NOW,
            expiry_flags=("WEEK",),
            expiry_codes=(1,),
            option_types=("CALL",),
            on_progress=lambda label, bars, failed: events.append((label, bars, failed)),
        )
        # One event per underlying, in order, each with bars>0 and not failed.
        assert [e[0] for e in events] == ["13", "1333"]
        assert all(bars > 0 and failed is False for _, bars, failed in events)

    def test_on_series_tick_forwarded_to_driver(self) -> None:
        """pull_options threads on_series_tick → fires once per SERIES (dry-run).

        DEC-OPTIONS-LIVE-STATUS-001: the per-series observability tick reaches the
        ingest spine and fires once per fanned series — far finer than the
        per-underlying on_progress (which fires once here). The dashboard wires a
        throttled summary-cache mark_dirty here so /coverage/options ticks live.
        """
        ticks: list[int] = []
        progress: list[str] = []
        pull_options(
            [_index(symbol="13")],
            lake_root=Path("/unused"),
            dry_run=True,
            now=_NOW,
            expiry_flags=("WEEK",),
            expiry_codes=(1,),
            option_types=("CALL",),
            on_progress=lambda label, bars, failed: progress.append(label),
            on_series_tick=lambda: ticks.append(1),
        )
        # ATM±10 = 21 strikes × 1 flag × 1 code × 1 type = 21 series → 21 ticks,
        # but only 1 per-underlying on_progress.
        assert len(ticks) == 21
        assert progress == ["13"]

    def test_now_defaults_to_wall_clock(self) -> None:
        """Omitting *now* uses the real clock — the window still starts at the floor."""
        report = pull_options(
            [_index()],
            lake_root=Path("/unused"),
            dry_run=True,
            expiry_flags=("WEEK",),
            expiry_codes=(1,),
            option_types=("CALL",),
        )
        assert report.start_date == "2021-01-01"
        assert report.total_symbols == 1


# ── report dataclasses ───────────────────────────────────────────────────


class TestOptionsPullReport:
    def test_aggregate_properties_and_to_dict(self) -> None:
        report = OptionsPullReport(
            dry_run=True,
            start_date="2021-01-01",
            end_date="2021-01-05",
            symbols=[
                OptionSymbolResult(
                    symbol="13",
                    instrument="OPTIDX",
                    series_completed=4,
                    series_failed=1,
                    bars_appended=40,
                    windows=4,
                ),
                OptionSymbolResult(
                    symbol="1333",
                    instrument="OPTSTK",
                    series_completed=2,
                    series_failed=0,
                    bars_appended=10,
                    windows=2,
                ),
            ],
        )
        assert report.total_symbols == 2
        assert report.total_series_completed == 6
        assert report.total_series_failed == 1
        assert report.total_bars == 50
        assert report.total_windows == 6

        payload = report.to_dict()
        assert payload["total_symbols"] == 2
        assert payload["total_bars"] == 50
        assert payload["symbols"][0] == {
            "symbol": "13",
            "instrument": "OPTIDX",
            "series_completed": 4,
            "series_failed": 1,
            "bars_appended": 40,
            "windows": 4,
        }

    def test_empty_report_totals_are_zero(self) -> None:
        report = OptionsPullReport(dry_run=False, start_date="2021-01-01", end_date="2021-01-05")
        assert report.total_symbols == 0
        assert report.total_bars == 0
        assert report.to_dict()["symbols"] == []


def _events(logs: Sequence[Mapping[str, object]], name: str) -> list[Mapping[str, object]]:
    """All captured events with ``event == name`` (in emission order)."""
    return [e for e in logs if e["event"] == name]


class TestPullOptionsCorrelation:
    """One ``pull_id`` ties the whole options fan-out together.

    DEC-DHAN-INGEST-LOGGING-001: the controller's ``pull_id_scope`` binds the id,
    so the start/done summary AND the real dry-run spine's ``ingest.*`` events all
    carry it. Narrow axes keep the synthetic fan-out fast (21 strikes × 1 × 1 × 1).
    """

    def test_correlates_start_done_and_spine(self) -> None:
        with capture_logs() as logs:
            pull_options(
                [_index()],
                lake_root=Path("/unused"),
                dry_run=True,
                now=_NOW,
                expiry_flags=("WEEK",),
                expiry_codes=(1,),
                option_types=("CALL",),
                pull_id="optctl000001",
            )
        (start,) = _events(logs, "universe.pull_options.start")
        (done,) = _events(logs, "universe.pull_options.done")
        assert start["pull_id"] == "optctl000001"
        assert done["pull_id"] == "optctl000001"
        # The spine inherited the SAME id via the bound scope.
        (underlying,) = _events(logs, "ingest.underlying")
        assert underlying["pull_id"] == "optctl000001"
        windows = _events(logs, "ingest.window")
        assert windows and all(e["pull_id"] == "optctl000001" for e in windows)

    def test_mints_a_pull_id_when_omitted(self) -> None:
        with capture_logs() as logs:
            pull_options(
                [_index()],
                lake_root=Path("/unused"),
                dry_run=True,
                now=_NOW,
                expiry_flags=("WEEK",),
                expiry_codes=(1,),
                option_types=("CALL",),
            )
        (start,) = _events(logs, "universe.pull_options.start")
        (done,) = _events(logs, "universe.pull_options.done")
        pid = start["pull_id"]
        assert isinstance(pid, str) and len(pid) == 12
        assert done["pull_id"] == pid


class _ClosingNullSink(NullOptionBarSink):
    """A counts-but-discards sink that ALSO records ``close`` calls.

    Stands in for the live concrete sink (which owns a closeable audit handle) so
    the not-dry-run path can prove ``pull_options`` releases the sink at end of run
    with no real Dhan token / network.
    """

    def __init__(self) -> None:
        self.closes = 0

    def close(self) -> None:
        self.closes += 1


class TestOptionAuditWiring:
    """Hash-chained option-ingest audit chain wiring (DEC-OPTION-AUDIT-WIRING-001).

    Finding D-2: the live option sink was constructed WITHOUT ``audit=`` so the
    ``OPTION_BARS_WRITTEN`` chain was never written. These cover the (now unit-
    testable) audit construction + lifecycle the live builder delegates to; the
    live builder itself stays ``# pragma: no cover`` (real token + network).
    """

    def test_audit_path_under_lake_audit_dir(self, tmp_path: Path) -> None:
        assert _option_audit_path(tmp_path) == tmp_path / "audit" / "option_ingest.jsonl"

    def test_build_option_audit_opens_a_verifiable_chain(self, tmp_path: Path) -> None:
        audit = _build_option_audit(tmp_path)
        try:
            audit.append(
                action="OPTION_BARS_WRITTEN",
                subject="NSE_FNO|13|WEEK|0|ATM|CALL",
                payload={"written": 2},
            )
        finally:
            audit.close()
        path = _option_audit_path(tmp_path)
        assert path.is_file()
        assert verify_chain(path) == 1  # genesis-linked, recomputes clean
        (entry,) = read_entries(path)
        assert entry.action == "OPTION_BARS_WRITTEN"

    def test_build_option_audit_resumes_existing_chain(self, tmp_path: Path) -> None:
        # resume=True: a re-pull APPENDS to the existing chain (idempotent re-pulls,
        # Golden Rule #7) rather than failing on an existing file.
        first = _build_option_audit(tmp_path)
        try:
            first.append(action="OPTION_BARS_WRITTEN", subject="s0", payload={})
        finally:
            first.close()
        second = _build_option_audit(tmp_path)  # must NOT raise on the existing file
        try:
            second.append(action="OPTION_BARS_WRITTEN", subject="s1", payload={})
        finally:
            second.close()
        path = _option_audit_path(tmp_path)
        assert verify_chain(path) == 2  # both rows link into ONE chain
        assert [e.seq for e in read_entries(path)] == [0, 1]


class TestCloseSink:
    """``_close_sink`` releases a closeable sink, no-ops otherwise."""

    def test_closes_a_closeable_sink(self) -> None:
        sink = _ClosingNullSink()
        _close_sink(sink)
        assert sink.closes == 1

    def test_noop_for_non_closeable_sink(self) -> None:
        # NullOptionBarSink has no close() → getattr returns None → no-op (no error).
        _close_sink(NullOptionBarSink())

    def test_pull_options_closes_the_sink_at_end_of_run(self, tmp_path: Path) -> None:
        # The not-dry-run path with an injected closeable sink: pull_options must
        # close it on the way out (the ExitStack callback) so the live audit handle
        # is released — proven without a real token / network.
        sink = _ClosingNullSink()

        def factory(_lake_root: Path) -> tuple[JsonPoster, OptionBarSink]:
            return SyntheticOptionClient(bars_per_window=1), sink

        pull_options(
            [_index()],
            lake_root=tmp_path,
            dry_run=False,
            now=_NOW,
            client_factory=factory,
            expiry_flags=("WEEK",),
            expiry_codes=(1,),
            option_types=("CALL",),
        )
        assert sink.closes == 1


class TestLiveOptionRuntime:
    """``_live_option_runtime`` (DEC-DHAN-TIMEOUT-BACKOFF-001 wiring).

    A dry-run / injected-factory pull needs no throttle → the all-``None``
    runtime, so the options spine uses its default pool and stays feedback-free.
    The LIVE branch (controller construction + bound-method wiring) is
    ``# pragma: no cover`` (real token + network), exercised by the dry-run path
    here and the spot path's own rate-feedback tests.
    """

    def test_dry_run_is_the_all_none_runtime(self) -> None:
        runtime = _live_option_runtime(use_live_rate=False)
        assert runtime == _LiveOptionRuntime()
        assert runtime.pace is None
        assert runtime.on_rate_limit is None
        assert runtime.on_timeout is None
        assert runtime.on_success is None
        assert runtime.run_parallel is None

    def test_dry_run_ignores_session_rate(self) -> None:
        # DEC-DHAN-OPTION-PERSIST-RATE-001: the new session_rate is accepted and
        # threaded, but a dry-run pull stays feedback-free (the shared controller
        # is only resolved on the live branch). So passing a session holder must
        # NOT change the all-None dry-run runtime.
        from brutex.dashboard.pull_controller import SessionRateController

        runtime = _live_option_runtime(use_live_rate=False, session_rate=SessionRateController())
        assert runtime == _LiveOptionRuntime()
