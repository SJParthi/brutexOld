"""Tests for `brutex.adapters.dhan.option_universe_ingest` — the rolling fan-out.

100% line+branch via injected fakes (no network, no lake): the strike-window
generator (incl. the 0 and negative-offset edges), the full fan-out cardinality
(strikes × flags × codes × types), the **per-kind expiry set** (index
WEEK+MONTH vs stock MONTH-only, DEC-DHAN-STOCK-MONTHLY-ONLY-001) + the explicit
uniform override, per-series terminal-error quarantine, the report aggregates,
the empty-universe case, and custom fan-out axes.
"""

# Whitebox test of the module-private `_with_paced_client` (sanctioned per pyproject.toml).
# pyright: reportPrivateUsage=none

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

import pytest
from brutex.adapters.dhan.errors import DhanBadRequestError
from brutex.adapters.dhan.option_universe_ingest import (
    DEFAULT_EXPIRY_CODES,
    INDEX_STRIKE_OFFSETS,
    STOCK_EXPIRY_FLAGS,
    STOCK_STRIKE_OFFSETS,
    WEEKLY_EXPIRY_CODES,
    OptionsUniverseReport,
    OptionUnderlying,
    _with_paced_client,
    expiry_codes_for_flag,
    iter_series_jobs,
    plan_option_pull_windows,
    run_dhan_options_ingest,
    strike_window,
)
from brutex.data.models import OptionBar
from brutex.observability.correlation import pull_id_scope
from structlog.testing import capture_logs

#: Index fan-out depth per (strike, type): WEEK fans 1..8 + MONTH fans 1..3
#: (DEC-DHAN-WEEKLY-EXPIRY-DEPTH-001). An index carries both flags.
_INDEX_CODES_PER_AXIS = len(WEEKLY_EXPIRY_CODES) + len(DEFAULT_EXPIRY_CODES)

# 1609732800 = 2021-01-04 04:00:00 UTC = 09:30 IST → in session [09:15, 15:30).
_TS_IN = 1609732800


def _payload() -> dict[str, object]:
    """A one-in-session-bar CE side; PE null (skipped) — the rolling shape."""
    return {
        "data": {
            "ce": {
                "timestamp": [_TS_IN],
                "open": [100],
                "high": [110],
                "low": [90],
                "close": [105],
                "volume": [250],
                "oi": [1200],
                "strike": [17500],
                "spot": [17480.5],
                "iv": [0.15],
            },
            "pe": None,
        }
    }


class _OkClient:
    """A JSON poster that returns the same one-bar payload for every call."""

    def __init__(self) -> None:
        self.calls = 0

    def post_json(self, endpoint: str, body: Mapping[str, object]) -> dict[str, object]:
        del endpoint, body
        self.calls += 1
        return _payload()


class _FailClient:
    """A JSON poster that always raises a terminal (non-retryable) Dhan error."""

    def post_json(self, endpoint: str, body: Mapping[str, object]) -> dict[str, object]:
        del endpoint, body
        raise DhanBadRequestError("boom")


class _NoDataClient:
    """A JSON poster that always raises a Dhan DH-905 "no data" boundary."""

    def post_json(self, endpoint: str, body: Mapping[str, object]) -> dict[str, object]:
        del endpoint, body
        raise DhanBadRequestError("HTTP 400 bad request: DH-905 no data present")


class _Sink:
    """Counts appended bars per series (the OptionBarSink seam)."""

    def __init__(self) -> None:
        self.series = 0

    def append_option_bars(
        self,
        *,
        security_id: str,
        exch_id: str,
        underlying_symbol: str,
        exchange_segment: str,
        expiry_flag: str,
        expiry_code: int,
        strike: str,
        drv_option_type: str,
        bars: Sequence[OptionBar],
    ) -> int:
        del security_id, exch_id, underlying_symbol, exchange_segment
        del expiry_flag, expiry_code, strike, drv_option_type
        self.series += 1
        return len(bars)


def _index(security_id: str = "13", offsets: int = INDEX_STRIKE_OFFSETS) -> OptionUnderlying:
    # No expiry_flags passed → the OptionUnderlying default EXPIRY_FLAGS
    # (WEEK+MONTH) — the index set (DEC-DHAN-STOCK-MONTHLY-ONLY-001).
    return OptionUnderlying(
        security_id=security_id,
        exch_id="NSE",
        underlying_symbol="NIFTY",
        exchange_segment="NSE_FNO",
        instrument="OPTIDX",
        strike_offsets=offsets,
    )


def _stock(security_id: str = "1333", offsets: int = STOCK_STRIKE_OFFSETS) -> OptionUnderlying:
    # A single-stock underlying — MONTH-only expiry set (NSE single stocks have
    # no weekly option series; DEC-DHAN-STOCK-MONTHLY-ONLY-001).
    return OptionUnderlying(
        security_id=security_id,
        exch_id="NSE",
        underlying_symbol="HDFCBANK",
        exchange_segment="NSE_FNO",
        instrument="OPTSTK",
        strike_offsets=offsets,
        expiry_flags=STOCK_EXPIRY_FLAGS,
    )


# ── strike_window ────────────────────────────────────────────────────────


def test_with_paced_client_injects_pace_into_live_dhan_client() -> None:
    """`_with_paced_client` wires `pace` into a real options DhanClient.

    Rate ≤5 GUARANTEE (DEC-DHAN-RATE-REALTIME-GUARANTEE-001) — options analog of
    the spot helper: covers pace None (unchanged), non-Dhan poster (unchanged),
    and the live DhanClient (rebuilt with the bucket `pace`).
    """
    from brutex.adapters.dhan.client import DhanClient

    def _transport(
        url: str,
        *,
        body: bytes,
        headers: Mapping[str, str],
        timeout: float,
        method: str = "POST",
    ) -> tuple[int, bytes]:
        return 200, b"{}"

    def _pace() -> None: ...

    client = DhanClient(access_token="t", client_id="c", transport=_transport)

    paced = _with_paced_client(client, _pace)
    assert paced is not client
    assert isinstance(paced, DhanClient) and paced.pace is _pace

    assert _with_paced_client(client, None) is client  # pace None → unchanged
    sentinel = object()
    # non-Dhan poster → returned unchanged
    assert _with_paced_client(sentinel, _pace) is sentinel  # type: ignore[arg-type]


def test_strike_window_zero_is_atm_only() -> None:
    assert strike_window(0) == ["ATM"]


def test_strike_window_three_is_seven_ordered() -> None:
    assert strike_window(3) == [
        "ATM",
        "ATM+1",
        "ATM-1",
        "ATM+2",
        "ATM-2",
        "ATM+3",
        "ATM-3",
    ]


def test_strike_window_ten_has_21() -> None:
    assert len(strike_window(INDEX_STRIKE_OFFSETS)) == 21
    assert len(strike_window(STOCK_STRIKE_OFFSETS)) == 7


def test_strike_window_negative_raises() -> None:
    with pytest.raises(ValueError, match="offsets must be >= 0"):
        strike_window(-1)


# ── run_dhan_options_ingest ────────────────────────────────────────────────


def test_full_fanout_cardinality_and_bars() -> None:
    client = _OkClient()
    sink = _Sink()
    report = run_dhan_options_ingest(
        client,
        sink,
        underlyings=[_index(offsets=1)],  # 3 strikes
        start=date(2021, 1, 4),
        end=date(2021, 1, 5),
    )
    # 3 strikes × (WEEK 1..8 + MONTH 1..3 = 11 codes) × 2 types = 66 series
    # (DEC-DHAN-WEEKLY-EXPIRY-DEPTH-001).
    expected = 3 * _INDEX_CODES_PER_AXIS * 2
    assert report.series_count == expected
    assert report.failed_series == 0
    assert client.calls == expected  # one window per series for a 1-day range
    assert report.bars_appended == expected  # one in-session bar per series
    assert report.windows == expected
    assert report.transient_failures == 0


def test_per_kind_expiry_flags_stock_month_only_index_week_month() -> None:
    """A stock fans MONTH-only; an index keeps WEEK+MONTH → the stock count HALVES.

    The per-kind reduction the live-pull-log bug needed: a stock previously
    fanned WEEK+MONTH, but Indian single stocks are monthly-only, so the WEEK
    leg only ever returned empty + burned Dhan quota
    (DEC-DHAN-STOCK-MONTHLY-ONLY-001).
    """
    report = run_dhan_options_ingest(
        _OkClient(),
        _Sink(),
        underlyings=[_index(offsets=1), _stock(offsets=1)],  # identical 3-strike window
        start=date(2021, 1, 4),
        end=date(2021, 1, 5),
    )
    index_series = {k for k in report.series if k[0] == "13"}
    stock_series = {k for k in report.series if k[0] == "1333"}
    # Index: 3 strikes × (WEEK 1..8 + MONTH 1..3) codes × 2 types.
    assert len(index_series) == 3 * _INDEX_CODES_PER_AXIS * 2
    # Stock: 3 strikes × 1 flag (MONTH 1..3) × 2 types — strictly fewer.
    assert len(stock_series) == 3 * 1 * len(DEFAULT_EXPIRY_CODES) * 2
    assert len(stock_series) < len(index_series)
    # No WEEK leg is fanned for the stock (the elided, always-empty series).
    assert {k[1] for k in stock_series} == {"MONTH"}
    assert {k[1] for k in index_series} == {"WEEK", "MONTH"}


def test_expiry_codes_for_flag_is_per_flag_depth() -> None:
    # WEEK fans the deep 1..8 ladder; MONTH (and any other flag) stays 1..3
    # (DEC-DHAN-WEEKLY-EXPIRY-DEPTH-001 — MONTH has no 4th depth, code 4 → 400).
    assert expiry_codes_for_flag("WEEK") == (1, 2, 3, 4, 5, 6, 7, 8)
    assert expiry_codes_for_flag("MONTH") == (1, 2, 3)


def test_default_fanout_uses_per_flag_codes_never_mixed() -> None:
    # With no explicit expiry_codes, an index fans WEEK codes 1..8 and MONTH
    # codes 1..3 — the per-flag depth, never the same set on both flags.
    report = run_dhan_options_ingest(
        _OkClient(),
        _Sink(),
        underlyings=[_index(offsets=0)],  # ATM only → 1 strike
        start=date(2021, 1, 4),
        end=date(2021, 1, 5),
    )
    week_codes = {k[2] for k in report.series if k[1] == "WEEK"}
    month_codes = {k[2] for k in report.series if k[1] == "MONTH"}
    assert week_codes == {1, 2, 3, 4, 5, 6, 7, 8}
    assert month_codes == {1, 2, 3}


def test_explicit_expiry_flags_override_beats_per_kind() -> None:
    """An explicit ``expiry_flags`` argument overrides each underlying's per-kind set."""
    report = run_dhan_options_ingest(
        _OkClient(),
        _Sink(),
        underlyings=[_stock(offsets=0)],  # per-kind set = MONTH-only
        start=date(2021, 1, 4),
        end=date(2021, 1, 5),
        expiry_flags=("WEEK", "MONTH"),  # force BOTH flags onto the stock
        expiry_codes=(1,),
        option_types=("CALL",),
    )
    # The uniform override wins: the stock fans both flags (1 × 2 × 1 × 1).
    assert report.series_count == 2
    assert {k[1] for k in report.series} == {"WEEK", "MONTH"}


def test_terminal_error_quarantines_every_series() -> None:
    report = run_dhan_options_ingest(
        _FailClient(),
        _Sink(),
        underlyings=[_index(offsets=1)],
        start=date(2021, 1, 4),
        end=date(2021, 1, 5),
    )
    expected = 3 * _INDEX_CODES_PER_AXIS * 2
    assert report.series_count == 0
    assert report.failed_series == expected
    assert report.bars_appended == 0
    # The failure carries the full series coordinates.
    first = report.failures[0]
    assert first.security_id == "13"
    assert first.expiry_flag in {"WEEK", "MONTH"}
    assert first.drv_option_type in {"CALL", "PUT"}
    assert "boom" in first.error


def test_empty_universe_is_empty_report() -> None:
    report = run_dhan_options_ingest(
        _OkClient(), _Sink(), underlyings=[], start=date(2021, 1, 4), end=date(2021, 1, 5)
    )
    assert report.series_count == 0
    assert report.failed_series == 0
    assert report.bars_appended == 0
    assert isinstance(report, OptionsUniverseReport)


def test_custom_axes_narrow_the_fanout() -> None:
    client = _OkClient()
    report = run_dhan_options_ingest(
        client,
        _Sink(),
        underlyings=[_index(offsets=0)],  # ATM only
        start=date(2021, 1, 4),
        end=date(2021, 1, 5),
        expiry_flags=("MONTH",),
        expiry_codes=(0,),
        option_types=("CALL",),
    )
    # 1 strike × 1 flag × 1 code × 1 type = 1 series.
    assert report.series_count == 1
    assert client.calls == 1
    key = ("13", "MONTH", 0, "ATM", "CALL")
    assert key in report.series


def test_on_progress_fires_per_underlying_success() -> None:
    """on_progress fires once per underlying with (id, bars, any_failed=False)."""
    events: list[tuple[str, int, bool, bool]] = []
    run_dhan_options_ingest(
        _OkClient(),
        _Sink(),
        underlyings=[_index("13", offsets=0)],
        start=date(2021, 1, 4),
        end=date(2021, 1, 5),
        expiry_flags=("WEEK",),
        expiry_codes=(1,),
        option_types=("CALL",),
        on_progress=lambda label, bars, failed, gap: events.append((label, bars, failed, gap)),
    )
    # 1 series × 1 in-session CE bar → 1 bar appended, nothing failed.
    assert events == [("13", 1, False, False)]


def test_on_series_tick_fires_once_per_drained_series() -> None:
    """on_series_tick fires once per SERIES outcome — far finer than on_progress.

    DEC-OPTIONS-LIVE-STATUS-001: a single underlying fans 3 strikes × 2 codes ×
    2 types = 12 series, so the per-series observability tick fires 12 times while
    the per-underlying on_progress would fire only ONCE (at the end). This is what
    lets a single-underlying pull's lake-backed coverage surface tick live.
    """
    ticks: list[int] = []
    progress: list[tuple[str, int, bool, bool]] = []
    report = run_dhan_options_ingest(
        _OkClient(),
        _Sink(),
        underlyings=[_index("13", offsets=1)],  # 3 strikes
        start=date(2021, 1, 4),
        end=date(2021, 1, 5),
        expiry_flags=("WEEK", "MONTH"),
        expiry_codes=(1, 2),
        option_types=("CALL", "PUT"),
        on_progress=lambda label, bars, failed, gap: progress.append((label, bars, failed, gap)),
        on_series_tick=lambda: ticks.append(1),
    )
    # 3 strikes × 2 flags... no: expiry_codes uniform (1,2) on both flags →
    # 3 × 2 flags × 2 codes × 2 types = 24 series.
    assert report.series_count == 24
    assert len(ticks) == 24  # one tick per drained series outcome
    assert len(progress) == 1  # on_progress fires once, per underlying


def test_on_series_tick_fires_for_quarantined_series_too() -> None:
    """A terminal-error series still drains an outcome → the tick still fires.

    The tick means "a series finished" (success OR quarantine); invalidating the
    lake-backed surface on a failed series is harmless (the recompute reads the
    unchanged lake) and keeps the surface live regardless of outcome.
    """
    ticks: list[int] = []
    run_dhan_options_ingest(
        _FailClient(),
        _Sink(),
        underlyings=[_index("13", offsets=0)],  # 1 strike
        start=date(2021, 1, 4),
        end=date(2021, 1, 5),
        expiry_flags=("WEEK",),
        expiry_codes=(1,),
        option_types=("CALL",),
        on_series_tick=lambda: ticks.append(1),
    )
    assert ticks == [1]  # the single quarantined series still ticked


def test_skipped_windows_aggregate_after_repull() -> None:
    """A re-pull with the same unified ledger skips every window (aggregate)."""
    from brutex.data.window_ledger import WindowLedger

    led = WindowLedger()
    underlyings = [_index("13", offsets=0)]  # 1 strike → 1 series
    kwargs: dict[str, object] = {
        "underlyings": underlyings,
        "start": date(2021, 1, 4),
        "end": date(2021, 1, 5),  # one window per series
        "expiry_flags": ("WEEK",),
        "expiry_codes": (1,),
        "option_types": ("CALL",),
    }
    first = run_dhan_options_ingest(_OkClient(), _Sink(), ledger=led, **kwargs)  # type: ignore[arg-type]
    assert first.skipped_windows == 0
    # Re-pull → the one window is settled SUCCESS in the ledger → skipped (the
    # ledger is the SOLE skip authority for options too, DEC-WINDOW-LEDGER-001 c).
    second = run_dhan_options_ingest(_OkClient(), _Sink(), ledger=led, **kwargs)  # type: ignore[arg-type]
    assert second.skipped_windows == 1
    assert second.bars_appended == 0


def test_on_progress_flags_failed_underlying() -> None:
    """A wholly-failing underlying fires on_progress with (id, 0, True)."""
    events: list[tuple[str, int, bool, bool]] = []
    run_dhan_options_ingest(
        _FailClient(),
        _Sink(),
        underlyings=[_index("13", offsets=0)],
        start=date(2021, 1, 4),
        end=date(2021, 1, 5),
        expiry_flags=("WEEK",),
        expiry_codes=(1,),
        option_types=("CALL",),
        on_progress=lambda label, bars, failed, gap: events.append((label, bars, failed, gap)),
    )
    assert events == [("13", 0, True, False)]


def test_on_progress_flags_no_data_gap_underlying() -> None:
    """An underlying whose only empty series are DH-905 boundaries fires on_progress
    with gap=True, failed=False — a benign no-data gap, NOT a failure
    (DEC-DHAN-NO-DATA-GAP-COUNT-001, options arm)."""
    events: list[tuple[str, int, bool, bool]] = []
    report = run_dhan_options_ingest(
        _NoDataClient(),
        _Sink(),
        underlyings=[_index("13", offsets=0)],
        start=date(2021, 1, 4),
        end=date(2021, 1, 5),
        expiry_flags=("WEEK",),
        expiry_codes=(1,),
        option_types=("CALL",),
        on_progress=lambda label, bars, failed, gap: events.append((label, bars, failed, gap)),
    )
    assert events == [("13", 0, False, True)]  # gap, not a failure
    # The DH-905 series is still RECORDED (re-tried next pull, never masked).
    assert len(report.failures) == 1


def _events(logs: Sequence[Mapping[str, object]], name: str) -> list[Mapping[str, object]]:
    """All captured events with ``event == name`` (in emission order)."""
    return [e for e in logs if e["event"] == name]


class TestOptionUniverseLogging:
    """The fan-out must roll up per-underlying (INFO) + name per-series quarantines.

    DEC-DHAN-INGEST-LOGGING-001: ``ingest.underlying`` (INFO, one per underlying)
    + ``ingest.quarantine`` (WARNING, one per terminal series failure), both
    carrying the pull's ``pull_id``. Observability only — report is unchanged.
    """

    def test_underlying_rollup_per_underlying(self) -> None:
        with capture_logs() as logs, pull_id_scope("optuniv00001"):
            run_dhan_options_ingest(
                _OkClient(),
                _Sink(),
                underlyings=[_index("13", offsets=0)],  # ATM only → 1 series
                start=date(2021, 1, 4),
                end=date(2021, 1, 5),
                expiry_flags=("WEEK",),
                expiry_codes=(1,),
                option_types=("CALL",),
            )
        (rollup,) = _events(logs, "ingest.underlying")
        assert rollup["log_level"] == "info"
        assert rollup["pull_id"] == "optuniv00001"
        assert rollup["security_id"] == "13"
        assert rollup["underlying_symbol"] == "NIFTY"
        assert rollup["instrument"] == "OPTIDX"
        assert rollup["series_completed"] == 1
        assert rollup["series_failed"] == 0
        assert rollup["bars_appended"] == 1
        assert rollup["windows"] == 1
        assert _events(logs, "ingest.quarantine") == []

    def test_quarantine_event_names_each_failed_series(self) -> None:
        with capture_logs() as logs, pull_id_scope("optuniv00002"):
            run_dhan_options_ingest(
                _FailClient(),
                _Sink(),
                underlyings=[_index("13", offsets=0)],  # 1 series, fails terminally
                start=date(2021, 1, 4),
                end=date(2021, 1, 5),
                expiry_flags=("WEEK",),
                expiry_codes=(1,),
                option_types=("CALL",),
            )
        (quarantine,) = _events(logs, "ingest.quarantine")
        assert quarantine["log_level"] == "warning"
        assert quarantine["pull_id"] == "optuniv00002"
        assert quarantine["security_id"] == "13"
        assert quarantine["underlying_symbol"] == "NIFTY"
        assert quarantine["expiry_flag"] == "WEEK"
        assert quarantine["expiry_code"] == 1
        assert quarantine["strike"] == "ATM"
        assert quarantine["drv_option_type"] == "CALL"
        assert "boom" in str(quarantine["error"])
        # The underlying rollup records the quarantine.
        (rollup,) = _events(logs, "ingest.underlying")
        assert rollup["series_failed"] == 1
        assert rollup["series_completed"] == 0

    def test_events_carry_empty_pull_id_outside_a_scope(self) -> None:
        # Called with no enclosing scope → the rollup carries a benign pull_id="".
        with capture_logs() as logs:
            run_dhan_options_ingest(
                _OkClient(),
                _Sink(),
                underlyings=[_index("13", offsets=0)],
                start=date(2021, 1, 4),
                end=date(2021, 1, 5),
                expiry_flags=("WEEK",),
                expiry_codes=(1,),
                option_types=("CALL",),
            )
        assert _events(logs, "ingest.underlying")[0]["pull_id"] == ""


def _ledger_keys(ledger: object) -> set[tuple[str, str, str, str, str]]:
    """Project a ledger's rows to comparable (market, id, interval, start, end) keys."""
    return {
        (r.market, r.security_id, r.interval, r.window_start, r.window_end)
        for r in ledger.records()  # type: ignore[attr-defined]
    }


class TestPlanFetchKeyParity:
    """DEC-OPTION-WINDOW-PLAN-001 C4 — the planner and the fetch can never drift.

    The golden parity: ``plan_option_pull_windows`` and the REAL ingest spine
    must touch IDENTICAL coverage keys (same enumeration, same tiler, same key
    builder). A drift in either direction would mean phantom PLANNED rows that
    never settle, or fetched windows the plan never showed.
    """

    def test_planner_keys_equal_fetch_keys(self) -> None:
        from datetime import date as _date

        from brutex.data.window_ledger import WindowLedger

        underlyings = [_index(offsets=1)]  # 3 strikes
        start, end = _date(2021, 1, 4), _date(2021, 2, 10)  # two 30-day windows

        planned = WindowLedger()
        count = plan_option_pull_windows(
            planned,
            underlyings,
            start=start,
            end=end,
            expiry_flags=("WEEK",),
            expiry_codes=(1, 2),
            option_types=("CALL",),
        )
        assert count == 3 * 2 * 2  # strikes × codes × windows

        fetched = WindowLedger()
        run_dhan_options_ingest(
            _OkClient(),
            _Sink(),
            underlyings=underlyings,
            start=start,
            end=end,
            ledger=fetched,
            expiry_flags=("WEEK",),
            expiry_codes=(1, 2),
            option_types=("CALL",),
        )
        assert _ledger_keys(fetched) == _ledger_keys(planned)
        # Every planned key SETTLED on the fully-successful run.
        assert {r.status for r in fetched.records()} == {"SUCCESS"}

    def test_plan_never_resets_an_existing_row(self) -> None:
        # Hostile finding 8: plan() is a no-op on ANY existing row — a FAILED
        # cursor must survive a re-plan un-masked (never back to PLANNED).
        from datetime import UTC as _UTC
        from datetime import date as _date
        from datetime import datetime as _datetime

        from brutex.adapters.dhan.option_ingest import coverage_key
        from brutex.data.window_ledger import WindowLedger

        underlyings = [_index(offsets=0)]
        start, end = _date(2021, 1, 4), _date(2021, 1, 5)
        ledger = WindowLedger()
        plan_option_pull_windows(
            ledger,
            underlyings,
            start=start,
            end=end,
            expiry_flags=("WEEK",),
            expiry_codes=(1,),
            option_types=("CALL",),
        )
        key = coverage_key(
            exchange_segment="NSE_FNO",
            security_id="13",
            expiry_flag="WEEK",
            expiry_code=1,
            strike="ATM",
            drv_option_type="CALL",
            window_start=start,
            window_end=_date(2021, 1, 5),
        )
        ledger.record(
            key,
            raw_bars=0,
            error_type="DhanRateLimitError",
            bars_appended=0,
            now=_datetime(2026, 6, 10, tzinfo=_UTC),
        )
        plan_option_pull_windows(
            ledger,
            underlyings,
            start=start,
            end=end,
            expiry_flags=("WEEK",),
            expiry_codes=(1,),
            option_types=("CALL",),
        )
        (record,) = ledger.records()
        assert record.status == "FAILED"

    def test_runner_consumes_the_same_generator(self) -> None:
        # The runner's job list IS the generator's output (no second enumeration).
        underlyings = [_index(offsets=1), _stock()]
        jobs = list(iter_series_jobs(underlyings, option_types=("CALL",)))
        # index: (8 WEEK + 3 MONTH codes) × 3 strikes; stock: 3 MONTH codes × 7 strikes
        assert len(jobs) == (8 + 3) * 3 + 3 * 7
        assert all(job[1] in underlyings for job in jobs)
