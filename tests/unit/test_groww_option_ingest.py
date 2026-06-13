"""Unit tests for ``brutex.adapters.groww.option_ingest`` (DEC-GROWW-OPTION-INGEST-001).

100% line+branch: the index-free per-contract candle fetch (FNO, volume+OI kept),
the per-contract ingest spine (ledger skip + record, current-day clamp, session
filter, bounded transient retry, terminal propagation, conflict summary, every
``on_*``/``pace``/``ledger`` present-vs-absent branch), the sequential multi-contract
driver (quarantine + report sums + auth-abort + non-Groww propagation), and the
full underlying path (resolve → CE/PE → ingest, resolution-error propagation).

Driven by a FAKE SDK behind a REAL :class:`GrowwClient` (the repo pattern — the
SDK satisfies the ``GrowwSDK`` Protocol structurally), so error injection uses the
SDK-named exceptions :func:`from_groww_exception` maps; no ``growwapi``, no network.
The client is built with ``max_attempts=1`` so its own timeout fast-retry never
fires — the ingest-level retry is what the tests exercise.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.adapters.groww import option_ingest as _oi
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.constants import (
    GROWW_FUTURE_COVERAGE_MARKET,
    GROWW_OPTION_COVERAGE_MARKET,
    OPTION_SEGMENT,
)
from brutex.adapters.groww.option_ingest import (
    GrowwOptionIngestReport,
    OptionPairFailure,
    _default_backoff,
    contract_exchange,
    fetch_option_candles,
    ingest_option_contract,
    ingest_underlying_futures,
    ingest_underlying_options,
    run_groww_option_ingest,
)
from brutex.adapters.protocol import DerivativeContract
from brutex.data.fetch_coverage import WindowKey
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import BarConflict, WriteResult
from brutex.data.sink import SinkAppendResult
from brutex.data.window_ledger import WindowLedger
from structlog.testing import capture_logs


def _now() -> datetime:
    """A frozen IST clock so the current-day clamp is deterministic (all ranges are past)."""
    return datetime(2026, 6, 13, 8, 0, tzinfo=UTC)


# SDK-named exception fakes — `from_groww_exception` maps them by class name to the
# neutral GrowwError tree (mirrors tests/unit/test_groww_client.py).
class GrowwAPIRateLimitException(Exception): ...


class GrowwAPITimeoutException(Exception): ...


class GrowwAPIBadRequestException(Exception): ...


class GrowwAPIAuthenticationException(Exception): ...


# In-session: 09:30 IST → kept by the intraday session filter. Out-of-session:
# 17:30 IST → dropped intraday, kept daily (a daily bar has no intraday time).
_IN_SESSION = "2024-01-02T09:30:00"
_OUT_SESSION = "2024-01-02T17:30:00"


def _candles(ts: str = _IN_SESSION, *, volume: int = 50, oi: int = 7) -> Mapping[str, object]:
    return {"candles": [[ts, 100.0, 110.0, 90.0, 105.0, volume, oi]]}


class _FakeSDK:
    """A ``GrowwSDK``-shaped fake: candle payloads + optional error scripts.

    * ``script`` — a per-call list (``Exception`` raises, ``None`` returns the
      default payload), clamped to its last entry (for retry sequences).
    * ``raise_for`` — a ``{groww_symbol: Exception}`` map (terminal per-contract).
    * ``expiries`` / ``contracts`` — callables for the resolution path.
    """

    def __init__(
        self,
        *,
        payload: Mapping[str, object] | None = None,
        script: Sequence[Exception | None] | None = None,
        raise_for: Mapping[str, Exception] | None = None,
        expiries: Callable[..., Mapping[str, object]] | None = None,
        contracts: Callable[..., Mapping[str, object]] | None = None,
    ) -> None:
        self._payload = payload if payload is not None else _candles()
        self._script = list(script) if script is not None else None
        self._raise_for = dict(raise_for or {})
        self._expiries = expiries or (lambda **_kw: {"expiries": []})
        self._contracts = contracts or (lambda **_kw: {"contracts": []})
        self.symbols: list[str] = []

    @property
    def calls(self) -> int:
        return len(self.symbols)

    def get_historical_candles(self, **kw: object) -> Mapping[str, object]:
        self.symbols.append(str(kw["groww_symbol"]))
        if self._script is not None:
            item = self._script[min(len(self.symbols) - 1, len(self._script) - 1)]
            if item is not None:
                raise item
            return self._payload
        exc = self._raise_for.get(str(kw["groww_symbol"]))
        if exc is not None:
            raise exc
        return self._payload

    def get_expiries(self, **kw: object) -> Mapping[str, object]:
        return self._expiries(**kw)

    def get_contracts(self, **kw: object) -> Mapping[str, object]:
        return self._contracts(**kw)


def _client(sdk: _FakeSDK) -> GrowwClient:
    # max_attempts=1 → the client never fast-retries a timeout, so the ingest-level
    # retry is the only retry under test.
    return GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=1)


def _ce(symbol: str = "NSE-NIFTY-02Jan25-28500-CE") -> DerivativeContract:
    return DerivativeContract(
        underlying_symbol="NIFTY",
        expiry=date(2025, 1, 2),
        symbol=symbol,
        contract_type="CE",
        strike=28500.0,
    )


class _Sink:
    """Recording sink; optionally surfaces a keep-first conflict in its write result."""

    def __init__(self, *, with_conflict: bool = False) -> None:
        self.appends: list[tuple[str, str, str, str, int]] = []
        self._with_conflict = with_conflict

    def append(
        self, *, exchange: str, segment: str, symbol: str, interval: str, bars: Sequence[CandleBar]
    ) -> SinkAppendResult:
        self.appends.append((exchange, segment, symbol, interval, len(bars)))
        write_results: tuple[WriteResult, ...] = ()
        if self._with_conflict and bars:
            stored = bars[0]
            vendor = CandleBar(
                timestamp=stored.timestamp,
                open=stored.open,
                high=stored.high,
                low=stored.low,
                close=stored.close + Decimal("1.00"),
                volume=stored.volume,
                open_interest=stored.open_interest,
            )
            conflict = BarConflict(timestamp=stored.timestamp, stored=stored, vendor=vendor)
            write_results = (
                WriteResult(
                    path=Path("x.parquet"),
                    row_count=len(bars),
                    raw_hash="0" * 64,
                    first_ts=stored.timestamp,
                    last_ts=stored.timestamp,
                    bytes_written=1,
                    conflicts=(conflict,),
                ),
            )
        return SinkAppendResult(
            bars_appended=len(bars), partitions_written=1, write_results=write_results
        )


class _RaisingSink:
    """A sink whose append raises a NON-Groww error (a genuine bug path)."""

    def append(
        self, *, exchange: str, segment: str, symbol: str, interval: str, bars: Sequence[CandleBar]
    ) -> SinkAppendResult:
        del exchange, segment, symbol, interval, bars
        raise RuntimeError("genuine bug in the sink")


# ─────────────────────────────────────────────────────────────────────────
# contract_exchange + fetch_option_candles
# ─────────────────────────────────────────────────────────────────────────
def test_contract_exchange_reads_leading_token() -> None:
    assert contract_exchange(_ce("NSE-NIFTY-02Jan25-28500-CE")) == "NSE"
    assert contract_exchange(_ce("BSE-SENSEX-02Jan25-79000-PE")) == "BSE"


def test_contract_exchange_no_dash_raises() -> None:
    bad = DerivativeContract(
        underlying_symbol="X", expiry=date(2025, 1, 2), symbol="NSENIFTY", contract_type="FUT"
    )
    with pytest.raises(ValueError, match="no exchange token"):
        contract_exchange(bad)


def test_fetch_option_candles_is_fno_and_keeps_volume_oi() -> None:
    sdk = _FakeSDK(payload=_candles(volume=321, oi=99))
    bars = fetch_option_candles(
        _client(sdk), contract=_ce(), interval="1day", start=date(2024, 1, 1), end=date(2024, 1, 2)
    )
    assert len(bars) == 1
    assert bars[0].volume == 321  # options trade → volume kept (drop_volume=False)
    assert bars[0].open_interest == 99


# ─────────────────────────────────────────────────────────────────────────
# ingest_option_contract — happy / skip / clamp
# ─────────────────────────────────────────────────────────────────────────
def test_ingest_success_no_ledger_no_hooks() -> None:
    sdk = _FakeSDK(payload=_candles(_OUT_SESSION))  # daily → not filtered
    sink = _Sink()
    report = ingest_option_contract(
        _client(sdk),
        sink,
        contract=_ce(),
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        now_ist=_now,
    )
    assert report.windows == 1 and report.bars_appended == 1 and report.retried_windows == 0
    exch, seg, sym, interval, n = sink.appends[0]
    assert seg == OPTION_SEGMENT == "FNO" and exch == "NSE"
    assert sym == "NSE-NIFTY-02Jan25-28500-CE" and interval == "1day" and n == 1


def test_ingest_success_with_ledger_pace_and_on_success() -> None:
    sdk = _FakeSDK(payload=_candles())
    sink = _Sink()
    ledger = WindowLedger()
    paces: list[int] = []
    successes: list[int] = []
    report = ingest_option_contract(
        _client(sdk),
        sink,
        contract=_ce(),
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        pace=lambda: paces.append(1),
        on_success=lambda: successes.append(1),
        ledger=ledger,
        now_ist=_now,
    )
    assert report.bars_appended == 1
    assert paces == [1] and successes == [1]
    key = WindowKey(
        market=GROWW_OPTION_COVERAGE_MARKET,
        security_id="NSE-NIFTY-02Jan25-28500-CE",
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
    )
    assert ledger.is_skippable(key)  # recorded SUCCESS


def test_ingest_skips_settled_window() -> None:
    sdk = _FakeSDK(payload=_candles())
    sink = _Sink()
    ledger = WindowLedger()
    key = WindowKey(
        market=GROWW_OPTION_COVERAGE_MARKET,
        security_id="NSE-NIFTY-02Jan25-28500-CE",
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
    )
    ledger.record(key, raw_bars=5, error_type=None, bars_appended=5, now=datetime.now(UTC))
    report = ingest_option_contract(
        _client(sdk),
        sink,
        contract=_ce(),
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        ledger=ledger,
        now_ist=_now,
    )
    assert report.skipped_windows == 1 and report.bars_appended == 0
    assert sdk.symbols == [] and sink.appends == []  # no vendor call


def test_ingest_empty_range_after_clamp() -> None:
    sdk = _FakeSDK()
    sink = _Sink()
    report = ingest_option_contract(
        _client(sdk),
        sink,
        contract=_ce(),
        interval="1day",
        start=date(2024, 1, 2),
        end=date(2024, 1, 1),
        now_ist=_now,  # start >= end
    )
    assert report.windows == 0 and report.bars_appended == 0
    assert sdk.symbols == []


# ─────────────────────────────────────────────────────────────────────────
# ingest_option_contract — session filter
# ─────────────────────────────────────────────────────────────────────────
def test_ingest_intraday_filters_out_of_session() -> None:
    sdk = _FakeSDK(payload=_candles(_OUT_SESSION))
    sink = _Sink()
    report = ingest_option_contract(
        _client(sdk),
        sink,
        contract=_ce(),
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        now_ist=_now,
    )
    assert report.bars_appended == 0  # out-of-session bar dropped before the sink
    assert sink.appends[0][4] == 0


def test_ingest_intraday_keeps_in_session() -> None:
    sdk = _FakeSDK(payload=_candles(_IN_SESSION))
    sink = _Sink()
    report = ingest_option_contract(
        _client(sdk),
        sink,
        contract=_ce(),
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        now_ist=_now,
    )
    assert report.bars_appended == 1


def test_ingest_conflict_summary_logged() -> None:
    sdk = _FakeSDK(payload=_candles())
    sink = _Sink(with_conflict=True)
    with capture_logs() as logs:
        report = ingest_option_contract(
            _client(sdk),
            sink,
            contract=_ce(),
            interval="1day",
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
            now_ist=_now,
        )
    assert report.conflicts == 1
    assert any(e["event"] == "option.ingest.conflict.summary" for e in logs)


# ─────────────────────────────────────────────────────────────────────────
# ingest_option_contract — transient retry + terminal
# ─────────────────────────────────────────────────────────────────────────
def test_ingest_timeout_retry_then_success() -> None:
    sdk = _FakeSDK(payload=_candles(), script=[GrowwAPITimeoutException("stall"), None])
    sink = _Sink()
    ledger = WindowLedger()
    timeouts: list[int] = []
    slept: list[int] = []
    report = ingest_option_contract(
        _client(sdk),
        sink,
        contract=_ce(),
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        on_timeout=lambda: timeouts.append(1),
        backoff=slept.append,
        ledger=ledger,
        max_attempts=2,
        now_ist=_now,
    )
    assert report.bars_appended == 1 and report.retried_windows == 1
    assert timeouts == [1] and slept == [0]  # one timeout fed, one backoff before retry


def test_ingest_ratelimit_exhausted_records_failed() -> None:
    sdk = _FakeSDK(script=[GrowwAPIRateLimitException("429")])
    sink = _Sink()
    ledger = WindowLedger()
    hits: list[int] = []
    report = ingest_option_contract(
        _client(sdk),
        sink,
        contract=_ce(),
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        on_rate_limit=lambda: hits.append(1),
        ledger=ledger,
        max_attempts=0,
        now_ist=_now,  # 0 retries → single attempt
    )
    assert report.still_failed_windows == 1 and report.transient_failures == 1
    assert report.retried_windows == 0 and hits == [1]
    key = WindowKey(
        market=GROWW_OPTION_COVERAGE_MARKET,
        security_id="NSE-NIFTY-02Jan25-28500-CE",
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
    )
    assert not ledger.is_skippable(key)  # FAILED — deferred to the next pull


def test_ingest_transient_no_hooks_no_ledger() -> None:
    # timeout with on_timeout=None + ledger=None — exercises the no-hook branches.
    sdk = _FakeSDK(script=[GrowwAPITimeoutException("stall")])
    report = ingest_option_contract(
        _client(sdk),
        _Sink(),
        contract=_ce(),
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        max_attempts=0,
        now_ist=_now,
    )
    assert report.still_failed_windows == 1


def test_ingest_ratelimit_no_hook_no_ledger() -> None:
    sdk = _FakeSDK(script=[GrowwAPIRateLimitException("429")])
    report = ingest_option_contract(
        _client(sdk),
        _Sink(),
        contract=_ce(),
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        max_attempts=0,
        now_ist=_now,
    )
    assert report.still_failed_windows == 1


def test_ingest_terminal_error_propagates() -> None:
    from brutex.adapters.groww.errors import GrowwBadRequestError

    sdk = _FakeSDK(script=[GrowwAPIBadRequestException("bad")])
    with pytest.raises(GrowwBadRequestError):
        ingest_option_contract(
            _client(sdk),
            _Sink(),
            contract=_ce(),
            interval="1day",
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
            now_ist=_now,
        )


# ─────────────────────────────────────────────────────────────────────────
# run_groww_option_ingest
# ─────────────────────────────────────────────────────────────────────────
def _ce2() -> DerivativeContract:
    return _ce("NSE-NIFTY-02Jan25-28150-CE")


def test_run_multi_contract_success_and_quarantine() -> None:
    ok1, ok2, bad = _ce(), _ce2(), _ce("NSE-NIFTY-02Jan25-99999-PE")
    sdk = _FakeSDK(payload=_candles(), raise_for={bad.symbol: GrowwAPIBadRequestException("nope")})
    sink = _Sink()
    progress: list[tuple[str, int, bool]] = []
    report = run_groww_option_ingest(
        _client(sdk),
        sink,
        contracts=[ok1, ok2, bad],
        intervals=["1day"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        now_ist=_now,
        on_progress=lambda label, bars, failed: progress.append((label, bars, failed)),
    )
    assert set(report.pairs) == {(ok1.symbol, "1day"), (ok2.symbol, "1day")}
    assert report.failed_pairs == 1 and report.bars_appended == 2
    assert report.windows == 2 and not report.auth_aborted
    assert (f"{bad.symbol} 1day", 0, True) in progress
    assert (f"{ok1.symbol} 1day", 1, False) in progress


def test_run_auth_failure_sets_auth_aborted() -> None:
    bad = _ce()
    sdk = _FakeSDK(raise_for={bad.symbol: GrowwAPIAuthenticationException("dead token")})
    report = run_groww_option_ingest(
        _client(sdk),
        _Sink(),
        contracts=[bad],
        intervals=["1day"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        now_ist=_now,
    )
    assert report.failed_pairs == 1 and report.auth_aborted
    assert report.failures[0].auth_failure is True


def test_run_non_groww_error_propagates() -> None:
    # The sink raising a non-Groww error proves run_groww_option_ingest never
    # swallows a genuine bug (only GrowwError is quarantined).
    sdk = _FakeSDK(payload=_candles())
    with pytest.raises(RuntimeError, match="genuine bug"):
        run_groww_option_ingest(
            _client(sdk),
            _RaisingSink(),
            contracts=[_ce()],
            intervals=["1day"],
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
            now_ist=_now,
        )


def test_run_report_property_sums() -> None:
    sdk = _FakeSDK(payload=_candles())
    report = run_groww_option_ingest(
        _client(sdk),
        _Sink(),
        contracts=[_ce(), _ce2()],
        intervals=["1day"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        now_ist=_now,
    )
    assert report.transient_failures == 0 and report.skipped_windows == 0
    assert report.retried_windows == 0 and report.still_failed_windows == 0


def test_report_dataclasses_are_frozen() -> None:
    fail = OptionPairFailure(symbol="X", interval="1day", error="boom")
    rep = GrowwOptionIngestReport(pairs={}, failures=(fail,))
    assert rep.failed_pairs == 1 and rep.auth_aborted is False
    assert rep.windows == 0 and rep.bars_appended == 0


def test_default_backoff_is_bounded_exponential(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(_oi, "_time_sleep", slept.append)
    _default_backoff(0)  # base × 2**0 = 1.0s
    _default_backoff(10)  # clamped to the 8.0s ceiling
    assert slept == [1.0, 8.0]


# ─────────────────────────────────────────────────────────────────────────
# ingest_underlying_options — full path
# ─────────────────────────────────────────────────────────────────────────
def test_ingest_underlying_options_full_path() -> None:
    sdk = _FakeSDK(
        payload=_candles(),
        expiries=lambda **kw: {"expiries": ["2024-01-25"]} if kw.get("year") == 2024 else {},
        contracts=lambda **_kw: {
            "contracts": ["NSE-NIFTY-25Jan24-100-CE", "NSE-NIFTY-25Jan24-FUT"]  # FUT filtered out
        },
    )
    sink = _Sink()
    report = ingest_underlying_options(
        _client(sdk),
        sink,
        exchange="NSE",
        underlying_symbol="NIFTY",
        years=[2024],
        intervals=["1day"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 26),
        now_ist=_now,
    )
    # only the CE leg is ingested (futures are filtered by option_contracts)
    assert set(report.pairs) == {("NSE-NIFTY-25Jan24-100-CE", "1day")}
    assert report.bars_appended == 1


def test_ingest_underlying_futures_full_path() -> None:
    # DEC-GROWW-FUTURE-INGEST-001: the futures mirror — the FUT leg is the subject,
    # the CE leg is filtered out by future_contracts; reuses the shared driver.
    sdk = _FakeSDK(
        payload=_candles(),
        expiries=lambda **kw: {"expiries": ["2024-01-25"]} if kw.get("year") == 2024 else {},
        contracts=lambda **_kw: {
            "contracts": [
                "NSE-NIFTY-25Jan24-100-CE",
                "NSE-NIFTY-25Jan24-FUT",
            ]  # the CE leg is filtered out
        },
    )
    sink = _Sink()
    report = ingest_underlying_futures(
        _client(sdk),
        sink,
        exchange="NSE",
        underlying_symbol="NIFTY",
        years=[2024],
        intervals=["1day"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 26),
        now_ist=_now,
    )
    # only the FUT leg is ingested (options are filtered by future_contracts)
    assert set(report.pairs) == {("NSE-NIFTY-25Jan24-FUT", "1day")}
    assert report.bars_appended == 1


def test_ingest_option_contract_futures_namespace() -> None:
    # DEC-GROWW-FUTURE-INGEST-001: a FUT contract pulled with market=
    # GROWW_FUTURE_COVERAGE_MARKET records its window under the DISTINCT
    # "groww-future" namespace (never the "groww-option" one), so future + option
    # windows never collide on the shared lake ledger.
    sdk = _FakeSDK(payload=_candles())
    ledger = WindowLedger()
    contract = DerivativeContract(
        underlying_symbol="NIFTY",
        expiry=date(2024, 1, 25),
        symbol="NSE-NIFTY-25Jan24-FUT",
        contract_type="FUT",
        strike=None,
    )
    ingest_option_contract(
        _client(sdk),
        _Sink(),
        contract=contract,
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        ledger=ledger,
        now_ist=_now,
        market=GROWW_FUTURE_COVERAGE_MARKET,
    )
    fut_key = WindowKey(
        market=GROWW_FUTURE_COVERAGE_MARKET,
        security_id="NSE-NIFTY-25Jan24-FUT",
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
    )
    opt_key = WindowKey(
        market=GROWW_OPTION_COVERAGE_MARKET,
        security_id="NSE-NIFTY-25Jan24-FUT",
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
    )
    assert ledger.is_skippable(fut_key)  # recorded under "groww-future"
    assert not ledger.is_skippable(opt_key)  # the option namespace is DISTINCT


def test_ingest_underlying_options_resolution_error_propagates() -> None:
    def _boom(**_kw: object) -> Mapping[str, object]:
        raise GrowwAPIAuthenticationException("dead token during resolution")

    sdk = _FakeSDK(expiries=_boom)
    from brutex.adapters.groww.errors import GrowwAuthError

    with pytest.raises(GrowwAuthError):
        ingest_underlying_options(
            _client(sdk),
            _Sink(),
            exchange="NSE",
            underlying_symbol="NIFTY",
            years=[2024],
            intervals=["1day"],
            start=date(2024, 1, 1),
            end=date(2024, 1, 26),
            now_ist=_now,
        )


# ─────────────────────────────────────────────────────────────────────────
# contract_windows — smart 1-minute bounds (DEC-DERIV-MINUTE-BOUNDS-001)
# ─────────────────────────────────────────────────────────────────────────
_BOUND = (date(2024, 12, 2), date(2024, 12, 20))
_GLOBAL = (date(2021, 1, 1), date(2025, 2, 1))


def test_resolve_window_none_map_is_global() -> None:
    assert (
        _oi._resolve_window(None, symbol="X", interval="1minute", start=_GLOBAL[0], end=_GLOBAL[1])
        == _GLOBAL
    )


def test_resolve_window_daily_interval_never_bounded() -> None:
    # Even with the symbol in the map, the DAILY interval keeps the global range — it
    # is the coverage SOURCE, so bounding it would freeze a live contract's coverage.
    assert (
        _oi._resolve_window(
            {"X": _BOUND}, symbol="X", interval="1day", start=_GLOBAL[0], end=_GLOBAL[1]
        )
        == _GLOBAL
    )


def test_resolve_window_subdaily_symbol_present_is_bounded() -> None:
    assert (
        _oi._resolve_window(
            {"X": _BOUND}, symbol="X", interval="1minute", start=_GLOBAL[0], end=_GLOBAL[1]
        )
        == _BOUND
    )


def test_resolve_window_symbol_absent_is_global() -> None:
    assert (
        _oi._resolve_window(
            {"OTHER": _BOUND}, symbol="X", interval="1minute", start=_GLOBAL[0], end=_GLOBAL[1]
        )
        == _GLOBAL
    )


def _record_window(ledger: WindowLedger, *, interval: str) -> tuple[str, str]:
    rec = next(r for r in ledger.records() if r.interval == interval)
    return (rec.window_start, rec.window_end)


def test_run_contract_windows_bounds_subdaily_not_daily() -> None:
    # The minute pull is bounded to the contract's window; the daily pull keeps the
    # full range (the daily-guard) — proven via the persisted WindowLedger keys.
    contract = _ce()
    ledger = WindowLedger()
    run_groww_option_ingest(
        _client(_FakeSDK(payload=_candles())),
        _Sink(),
        contracts=[contract],
        intervals=["1minute", "1day"],
        start=_GLOBAL[0],
        end=_GLOBAL[1],
        ledger=ledger,
        now_ist=_now,
        contract_windows={contract.symbol: _BOUND},
    )
    assert _record_window(ledger, interval="1minute") == ("2024-12-02", "2024-12-20")
    assert _record_window(ledger, interval="1day") == ("2021-01-01", "2025-02-01")


def test_run_contract_windows_none_is_backward_compatible() -> None:
    contract = _ce()
    ledger = WindowLedger()
    run_groww_option_ingest(
        _client(_FakeSDK(payload=_candles())),
        _Sink(),
        contracts=[contract],
        intervals=["1minute"],
        start=_GLOBAL[0],
        end=_GLOBAL[1],
        ledger=ledger,
        now_ist=_now,
    )
    assert _record_window(ledger, interval="1minute") == ("2021-01-01", "2025-02-01")


def test_ingest_underlying_options_forwards_contract_windows() -> None:
    sym = "NSE-NIFTY-25Jan24-100-CE"
    ledger = WindowLedger()
    sdk = _FakeSDK(
        payload=_candles(),
        expiries=lambda **kw: {"expiries": ["2024-01-25"]} if kw.get("year") == 2024 else {},
        contracts=lambda **_kw: {"contracts": [sym]},
    )
    ingest_underlying_options(
        _client(sdk),
        _Sink(),
        exchange="NSE",
        underlying_symbol="NIFTY",
        years=[2024],
        intervals=["1minute"],
        start=_GLOBAL[0],
        end=_GLOBAL[1],
        ledger=ledger,
        now_ist=_now,
        contract_windows={sym: _BOUND},
    )
    assert _record_window(ledger, interval="1minute") == ("2024-12-02", "2024-12-20")


def test_ingest_underlying_futures_forwards_contract_windows() -> None:
    sym = "NSE-NIFTY-25Jan24-FUT"
    ledger = WindowLedger()
    sdk = _FakeSDK(
        payload=_candles(),
        expiries=lambda **kw: {"expiries": ["2024-01-25"]} if kw.get("year") == 2024 else {},
        contracts=lambda **_kw: {"contracts": [sym]},
    )
    ingest_underlying_futures(
        _client(sdk),
        _Sink(),
        exchange="NSE",
        underlying_symbol="NIFTY",
        years=[2024],
        intervals=["1minute"],
        start=_GLOBAL[0],
        end=_GLOBAL[1],
        ledger=ledger,
        now_ist=_now,
        contract_windows={sym: _BOUND},
    )
    assert _record_window(ledger, interval="1minute") == ("2024-12-02", "2024-12-20")
