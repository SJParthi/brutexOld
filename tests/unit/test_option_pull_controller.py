"""Tests for `brutex.dashboard.option_pull_controller` — the options pull core.

100% line+branch via the synthetic dry-run path + injected fake client/sink:
the instrument→underlying mapping (IDX/EQ/blank/derivative) incl. the per-kind
expiry set (index WEEK+MONTH vs stock MONTH-only,
DEC-DHAN-STOCK-MONTHLY-ONLY-001), the report aggregation (completed series +
quarantined failures grouped by underlying), the dry-run default path, the
injected-factory (non-dry-run) branch, the empty-list no-op, the dead-token
pre-flight refusal gate (DEC-OPTIONS-TOKEN-PREFLIGHT-001 — zero fan-out calls
on a dead token), and the ``to_dict`` projection.
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
    _live_option_runtime,
    _LiveOptionRuntime,
    count_progress_units,
    instrument_to_option_underlying,
    pull_options,
)
from brutex.data.option_sink import NullOptionBarSink
from brutex.models import Exchange, Instrument, InstrumentType, Segment
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
        # A live pull persists the unified window ledger so a re-pull skips the
        # windows already in the lake (DEC-WINDOW-LEDGER-001).
        assert (tmp_path / "window_ledger" / "ledger.parquet").is_file()

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
        events: list[tuple[str, int, bool, bool]] = []
        pull_options(
            [_index(symbol="13"), _stock(symbol="1333")],
            lake_root=Path("/unused"),
            dry_run=True,
            now=_NOW,
            expiry_flags=("WEEK",),
            expiry_codes=(1,),
            option_types=("CALL",),
            on_progress=lambda label, bars, failed, gap: events.append((label, bars, failed, gap)),
        )
        # One event per underlying, in order, each with bars>0 and not failed.
        assert [e[0] for e in events] == ["13", "1333"]
        assert all(bars > 0 and failed is False for _, bars, failed, _gap in events)

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
            on_progress=lambda label, bars, failed, gap: progress.append(label),
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

    def test_preflight_dead_refuses_pull(self, tmp_path: Path) -> None:
        """A DEAD pre-flight REFUSES the options pull — ZERO fan-out, loud ``auth_expired``.

        DEC-OPTIONS-TOKEN-PREFLIGHT-001 (the spot DEC-TOKEN-PREFLIGHT-001 twin):
        before the gate a dead token cost one doomed paced call + the AuthGuard
        drain across the remaining series (DEC-OPTIONS-UNTIL-CLEAN-001); with the
        gate it costs ZERO calls — the client factory itself must never run, and
        the until-clean loop must never start (``rounds_run == 0``).
        """
        from brutex.adapters.dhan.token_preflight import classify_token_lifetime

        built: list[int] = []

        def factory(_lake_root: Path) -> tuple[JsonPoster, OptionBarSink]:
            built.append(1)  # must NEVER run for a dead token
            return SyntheticOptionClient(), NullOptionBarSink()

        with capture_logs() as logs:
            report = pull_options(
                [_index(symbol="13"), _stock(symbol="1333")],
                lake_root=tmp_path,
                dry_run=False,
                now=_NOW,
                client_factory=factory,
                until_clean=True,  # the auto-resume loop must not start either
                preflight=classify_token_lifetime(-60.0),  # dead
            )

        assert built == []  # the client was never built — zero paced fan-out calls
        assert report.auth_aborted is True
        assert report.symbols == []
        assert report.rounds_run == 0  # not even one fan-out round ran
        assert report.verdict.kind == "auth_expired"
        assert report.verdict.needs_operator is True
        assert "DEAD" in (report.auth_cause or "")
        assert report.to_dict()["auth_aborted"] is True
        # No ledger side effects either — the refusal precedes begin_pull_ledger.
        assert not (tmp_path / "window_ledger").exists()
        (refused,) = _events(logs, "universe.pull_options.preflight_refused")
        assert refused["underlyings"] == 2
        assert _events(logs, "universe.pull_options.start") == []

    def test_preflight_alive_proceeds(self) -> None:
        """A healthy pre-flight does NOT block the pull — it completes normally."""
        from brutex.adapters.dhan.token_preflight import classify_token_lifetime

        report = pull_options(
            [_index()],
            lake_root=Path("/unused"),
            dry_run=True,
            now=_NOW,
            expiry_flags=("WEEK",),
            expiry_codes=(1,),
            option_types=("CALL",),
            preflight=classify_token_lifetime(20 * 3600.0),  # valid
        )
        assert report.auth_aborted is False
        assert report.total_series_completed == 21
        assert report.verdict.kind == "clean"
        assert report.rounds_run == 1


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


class TestCountProgressUnits:
    """DEC-OPTIONS-PROGRESS-SERIES-001 — the /universe bar total is the REAL fan-out.

    The operator's live report: a 3-index options pull showed "0/3 chunks" (one
    tick per UNDERLYING) and sat at 0% for hours while spot showed its true
    678/1,566. The total is now series + underlyings, counted via the SAME
    iter_series_jobs enumeration as the fetch and the window planner.
    """

    def test_three_indices_count_series_plus_underlyings(self) -> None:
        # Index fan-out: (8 WEEK + 3 MONTH codes) × 21 strikes × CALL/PUT = 462.
        instruments = [_index("13"), _index("25"), _index("51")]
        assert count_progress_units(instruments) == 3 * 462 + 3  # 1,389 — not 3

    def test_stock_counts_month_only_fan(self) -> None:
        # Stock fan-out: 3 MONTH codes × 7 strikes × CALL/PUT = 42 (+1 rollup).
        assert count_progress_units([_stock()]) == 42 + 1

    def test_overrides_shrink_the_fan(self) -> None:
        units = count_progress_units(
            [_index("13")],
            expiry_flags=("MONTH",),
            expiry_codes=(1,),
            option_types=("CALL",),
        )
        assert units == 21 + 1  # 21 strikes × 1 code × 1 type, + the rollup

    def test_bad_instrument_fails_fast_before_the_job_starts(self) -> None:
        with pytest.raises(ValueError, match="only\\s+IDX / EQ"):
            count_progress_units([_option_leg()])
