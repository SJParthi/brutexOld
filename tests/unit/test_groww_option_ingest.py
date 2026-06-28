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

import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
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
from brutex.adapters.groww.ingest import IngestReport
from brutex.adapters.groww.option_ingest import (
    UNDERLYING_ALL_EMPTY_EVENT,
    AllEmptyScan,
    GrowwOptionIngestReport,
    OptionPairFailure,
    UnderlyingEmptyReport,
    _default_backoff,
    contract_exchange,
    fetch_option_candles,
    ingest_option_contract,
    ingest_underlying_futures,
    ingest_underlying_options,
    plan_contract_windows,
    run_groww_option_ingest,
    scan_underlying_all_empty,
)
from brutex.adapters.protocol import DerivativeContract
from brutex.data.errors import LakeWriteHalt, PartitionLockTimeout
from brutex.data.fetch_coverage import WindowKey
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import BarConflict, WriteResult
from brutex.data.sink import SinkAppendResult
from brutex.data.window_ledger import WindowLedger
from brutex.models import BruteXError, OutOfScopeError
from brutex.orchestrator.parallel import PoolRunParallel, SerialRunParallel
from brutex.orchestrator.quota_tracker import DailyQuotaExceeded, QuotaInvariantError
from brutex.orchestrator.universe_phases import ItemResult
from structlog.testing import capture_logs


def _now() -> datetime:
    """A frozen IST clock so the current-day clamp is deterministic (all ranges are past)."""
    return datetime(2026, 6, 13, 8, 0, tzinfo=UTC)


# SDK-named exception fakes — `from_groww_exception` maps them by class name to the
# neutral GrowwError tree (mirrors tests/unit/test_groww_client.py).
class GrowwAPIRateLimitException(Exception): ...


class GrowwAPITimeoutException(Exception): ...


class GrowwAPIBadRequestException(Exception): ...


class GrowwAPINotFoundException(Exception): ...


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
        payloads: Sequence[Mapping[str, object]] | None = None,
        script: Sequence[Exception | Mapping[str, object] | None] | None = None,
        raise_for: Mapping[str, Exception] | None = None,
        expiries: Callable[..., Mapping[str, object]] | None = None,
        contracts: Callable[..., Mapping[str, object]] | None = None,
    ) -> None:
        self._payload = payload if payload is not None else _candles()
        # ``payloads`` scripts a per-call payload sequence (clamped to its last entry) so a
        # test can return e.g. an empty 200 then real data — the empty-retry recovery case.
        self._payloads = list(payloads) if payloads is not None else None
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
        if self._payloads is not None:
            return self._payloads[min(len(self.symbols) - 1, len(self._payloads) - 1)]
        if self._script is not None:
            item = self._script[min(len(self.symbols) - 1, len(self._script) - 1)]
            if isinstance(item, BaseException):
                raise item
            if item is not None:
                return item  # a scripted payload (e.g. an empty 200 mid-sequence)
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
        # Recorded from POOL worker threads under the parallel fan-out (DEC-GROWW-OPTION-POOL-001),
        # so guard the recording list — keeps a future order-assertion race-free.
        self._lock = threading.Lock()

    def append(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
        merge_mode: str = "keep-first",
    ) -> SinkAppendResult:
        with self._lock:
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
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
        merge_mode: str = "keep-first",
    ) -> SinkAppendResult:
        del exchange, segment, symbol, interval, bars
        raise RuntimeError("genuine bug in the sink")


class _LockTimeoutSink:
    """A sink whose append raises :class:`PartitionLockTimeout` (a wedged peer)."""

    def __init__(self, timeout_seconds: float = 0.5) -> None:
        self._timeout_seconds = timeout_seconds
        self.calls = 0

    def append(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
        merge_mode: str = "keep-first",
    ) -> SinkAppendResult:
        del exchange, segment, symbol, interval, bars, merge_mode
        self.calls += 1
        raise PartitionLockTimeout(self._timeout_seconds)


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
    # The transient FAILED row carries the in-pull fetch count (1 — 429'd on the first
    # fetch this pull; DEC-WINDOW-INPULL-FETCHES-001, the option spine's twin).
    assert ledger.records()[0].in_pull_fetches == 1


def test_ingest_partition_lock_timeout_defers_window() -> None:
    """A wedged-peer lock timeout on write DEFERS the window like a 429 (GAP #3).

    The sink's append raises ``PartitionLockTimeout``; the contract's window must be
    recorded FAILED + counted transient (deferred to the next pull), NOT a
    contract-wide quarantine and NOT an infinite block (DEC-PULL-CONTENTION-ANTISTALL-001).
    """
    sdk = _FakeSDK(payload=_candles())
    sink = _LockTimeoutSink()
    ledger = WindowLedger()
    report = ingest_option_contract(
        _client(sdk),
        sink,
        contract=_ce(),
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        ledger=ledger,
        max_attempts=0,
        now_ist=_now,
    )
    assert report.windows == 1
    assert report.bars_appended == 0
    assert report.transient_failures == 1 and report.still_failed_windows == 1
    assert sink.calls == 1  # the fetch ran; the write hit the wedged lock once
    key = WindowKey(
        market=GROWW_OPTION_COVERAGE_MARKET,
        security_id="NSE-NIFTY-02Jan25-28500-CE",
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
    )
    assert not ledger.is_skippable(key)  # FAILED — deferred to the next pull


def test_ingest_partition_lock_timeout_no_ledger_logs_deferred() -> None:
    """The defer path works with NO ledger (the ``ledger is None`` branch)."""
    sdk = _FakeSDK(payload=_candles())
    sink = _LockTimeoutSink()
    with capture_logs() as logs:
        report = ingest_option_contract(
            _client(sdk),
            sink,
            contract=_ce(),
            interval="1day",
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
            ledger=None,
            max_attempts=0,
            now_ist=_now,
        )
    assert report.transient_failures == 1
    events = [r["event"] for r in logs]
    assert "option.ingest.window.lock_deferred" in events


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
# Transient network / DNS failure → RETRYABLE (DEC-GROWW-NETWORK-ERROR-RETRY-001)
# A RAW transport exception (socket.gaierror / requests ConnectionError shape)
# the SDK raises is now classified `GrowwNetworkError` (retryable) by the client,
# so a DNS-blipping contract is RETRIED / deferred, NOT terminally quarantined —
# the 218-contract "Failed to resolve api.groww.in" data-loss class is closed.
# ─────────────────────────────────────────────────────────────────────────
class gaierror(Exception):
    """Fake ``socket.gaierror`` raised raw by the SDK on a host DNS outage."""


class ConnectionError(Exception):  # noqa: A001 — mirrors requests.exceptions.ConnectionError
    """Fake ``requests.exceptions.ConnectionError`` (the ``"Failed to resolve …"`` shape)."""


def test_ingest_dns_error_retried_then_success() -> None:
    """A transient DNS failure then a clean response → the contract is RETRIED, bars land.

    Proves the 218-contract loss class is recoverable: the raw transport error the
    SDK raises is classified RETRYABLE, so the ingest-level retry re-issues it and
    the contract is NOT terminally quarantined.
    """
    sdk = _FakeSDK(
        payload=_candles(),
        script=[gaierror("[Errno -2] Name or service not known"), None],
    )
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
    assert report.still_failed_windows == 0  # recovered — NOT quarantined
    # A DNS blip is NOT a rate-limit, so it rides the GENTLE timeout ladder (on_timeout),
    # never the per-second rate stepdown (on_rate_limit).
    assert timeouts == [1] and slept == [0]


def test_ingest_dns_error_exhausted_is_deferred_not_terminal() -> None:
    """A persistent DNS failure is DEFERRED (``still_failed``), NOT terminally quarantined.

    When the in-pull retry budget is spent the window is left UNMARKED-as-success
    (re-pulled next pass), NOT recorded as a terminal drop — exactly the recovery
    path the 218 lost contracts never got.
    """
    sdk = _FakeSDK(script=[ConnectionError("Failed to resolve 'api.groww.in'")])
    sink = _Sink()
    ledger = WindowLedger()
    timeouts: list[int] = []
    report = ingest_option_contract(
        _client(sdk),
        sink,
        contract=_ce(),
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        on_timeout=lambda: timeouts.append(1),
        ledger=ledger,
        max_attempts=0,  # single attempt → immediate budget exhaustion
        now_ist=_now,
    )
    assert report.still_failed_windows == 1 and report.transient_failures == 1
    assert report.retried_windows == 0 and timeouts == [1]
    key = WindowKey(
        market=GROWW_OPTION_COVERAGE_MARKET,
        security_id="NSE-NIFTY-02Jan25-28500-CE",
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
    )
    assert not ledger.is_skippable(key)  # FAILED — deferred to the next pull, never dropped


def test_run_multi_contract_dns_failure_does_not_terminally_quarantine() -> None:
    """A whole-pull DNS outage leaves contracts DEFERRED, with ZERO terminal quarantines.

    The driver-level proof: every contract hits a transient DNS error, so none is
    recorded as a terminal :class:`OptionPairFailure` — they are all
    ``still_failed`` (re-pulled next pass), in contrast to a 404 which DOES
    quarantine (the next test).
    """
    sdk = _FakeSDK(script=[gaierror("Name or service not known")])
    report = run_groww_option_ingest(
        _client(sdk),
        _Sink(),
        contracts=[_ce("NSE-NIFTY-02Jan25-28500-CE"), _ce("NSE-NIFTY-02Jan25-28600-CE")],
        intervals=["1day"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        ledger=WindowLedger(),
        max_attempts=0,
        now_ist=_now,
    )
    assert report.failed_pairs == 0  # ZERO terminal quarantines — all recoverable
    assert report.auth_aborted is False
    assert report.still_failed_windows == 2  # both deferred to the next pull


def test_run_multi_contract_real_404_still_terminally_quarantines() -> None:
    """A genuine 404 (a symbol truly absent) STILL terminally quarantines — the fix is narrow.

    The retryable-network change must NOT make a real terminal error recoverable: a
    ``GrowwAPINotFoundException`` is recorded as a terminal :class:`OptionPairFailure`
    exactly as before.
    """
    sdk = _FakeSDK(raise_for={"NSE-NIFTY-02Jan25-28500-CE": GrowwAPINotFoundException("404")})
    report = run_groww_option_ingest(
        _client(sdk),
        _Sink(),
        contracts=[_ce("NSE-NIFTY-02Jan25-28500-CE")],
        intervals=["1day"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        ledger=WindowLedger(),
        max_attempts=0,
        now_ist=_now,
    )
    assert report.failed_pairs == 1  # terminal — quarantined, NOT retried
    assert report.still_failed_windows == 0


# ─────────────────────────────────────────────────────────────────────────
# ingest_option_contract — in-pull EMPTY retry (DEC-OPTION-EMPTY-RETRY-001)
# The option-candle twin of the spot spine's empty-retry: a clean 0-bar 200 is
# re-fetched up to empty_retry_attempts times — closing the last "pull until a
# successful response" gap (spot candles + expiries + contracts already had it).
# ─────────────────────────────────────────────────────────────────────────
_EMPTY: Mapping[str, object] = {"candles": []}


def test_ingest_empty_recovers_on_second_attempt() -> None:
    """A glitched empty 200 then real data → the empty is recovered (bars land, 2 fetches)."""
    sdk = _FakeSDK(payloads=[_EMPTY, _candles()])  # empty glitch, then data
    sink = _Sink()
    report = ingest_option_contract(
        _client(sdk),
        sink,
        contract=_ce(),
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        empty_retry_attempts=3,
        empty_retry_backoff=lambda _a: None,
        now_ist=_now,
    )
    assert report.bars_appended == 1  # the 2nd fetch's data won
    assert sdk.calls == 2  # stopped as soon as data arrived — not all 3 attempts


def test_ingest_genuine_empty_bounded_to_attempts() -> None:
    """A persistently-empty window is re-fetched EXACTLY attempts times, then recorded empty."""
    sdk = _FakeSDK(payload=_EMPTY)  # always empty
    sink = _Sink()
    backoffs: list[int] = []
    ledger = WindowLedger()
    report = ingest_option_contract(
        _client(sdk),
        sink,
        contract=_ce(),
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        empty_retry_attempts=3,
        empty_retry_backoff=backoffs.append,
        ledger=ledger,
        # A RECENT clock (just after the window) so the old-gap fast-settle
        # (DEC-OLD-GAP-FAST-SETTLE-001) does NOT fire — this test pins the conservative
        # across-pull EMPTY-streak (an old expired-contract gap is fast-settled by its
        # own dedicated test below).
        now_ist=lambda: datetime(2024, 1, 10, 8, 0, tzinfo=UTC),
    )
    assert report.windows == 1 and report.bars_appended == 0  # confidently empty
    assert sdk.calls == 3  # bounded to exactly attempts — never an infinite loop
    # backoff fires before re-fetch on rounds 2+3 (not the last); 0-based arg mirrors the
    # spot spine (ingest._ingest_one_window passes empty_attempt, not empty_attempt + 1).
    assert backoffs == [0, 1]
    key = WindowKey(
        market=GROWW_OPTION_COVERAGE_MARKET,
        security_id="NSE-NIFTY-02Jan25-28500-CE",
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
    )
    # The in-pull empty-retry confirms "empty right now" within ONE pull, but the ledger's
    # CROSS-pull empty-streak threshold (3) is separate: a single empty (empty_streak=1) is
    # recorded yet NOT yet skippable — it is re-checked on future pulls until the streak
    # confirms a genuine, durable empty. Two complementary guards, not one.
    assert not ledger.is_skippable(key)
    # The option spine records the in-pull fetch count too: 3 fetches this pull, all
    # empty (the ↻ 3 the operator sees on /coverage/windows; DEC-WINDOW-INPULL-FETCHES-001).
    assert ledger.records()[0].in_pull_fetches == 3


def test_ingest_empty_attempts_one_disables_retry() -> None:
    """``empty_retry_attempts=1`` → a single fetch, no empty-retry (the opt-out)."""
    sdk = _FakeSDK(payload=_EMPTY)
    report = ingest_option_contract(
        _client(sdk),
        _Sink(),
        contract=_ce(),
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        empty_retry_attempts=1,
        now_ist=_now,
    )
    assert sdk.calls == 1 and report.bars_appended == 0  # one call, confidently empty


def test_ingest_empty_attempts_zero_clamps_to_single_fetch() -> None:
    """``empty_retry_attempts=0`` is degenerate — the ``max(1, …)`` guard still fetches ONCE.

    Hardens over the spot mirror's bare ``range(N)``: a 0 must never silently record an empty
    window WITHOUT calling the vendor (the out-of-box guard the hostile review flagged).
    """
    sdk = _FakeSDK(payload=_EMPTY)
    report = ingest_option_contract(
        _client(sdk),
        _Sink(),
        contract=_ce(),
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        empty_retry_attempts=0,
        now_ist=_now,
    )
    assert sdk.calls == 1 and report.windows == 1  # clamped to one real fetch, never zero


# ─────────────────────────────────────────────────────────────────────────
# ingest_option_contract — OLD-gap fast-settle (DEC-OLD-GAP-FAST-SETTLE-001)
# An expired/old contract window confirmed empty across the in-pull retry can
# never gain vendor data, so it settles SKIPPED in ONE pull instead of needing the
# conservative 3-pull EMPTY streak; a RECENT window keeps the conservative streak.
# ─────────────────────────────────────────────────────────────────────────
def test_old_empty_contract_window_fast_settles_in_one_pull() -> None:
    """An OLD expired-contract empty window settles SKIPPED in ONE pull (a permanent gap)."""
    from brutex.data.window_ledger import SKIPPED

    sdk = _FakeSDK(payload=_EMPTY)  # always empty
    ledger = WindowLedger()
    ingest_option_contract(
        _client(sdk),
        _Sink(),
        contract=_ce(),
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),  # > 60 days before the injected 2026 clock
        empty_retry_attempts=3,
        empty_retry_backoff=lambda _a: None,
        ledger=ledger,
        now_ist=_now,  # 2026-06-13 → the 2024 window is OLD
    )
    rec = ledger.records()[0]
    assert rec.status == SKIPPED  # terminal in this single pull
    assert rec.empty_streak == 3  # forced to the threshold (a normal settle's shape)
    assert rec.attempts == 1  # ONE pull
    assert rec.in_pull_fetches == 3  # re-hit the full in-pull budget, all empty


def test_recent_empty_contract_window_keeps_conservative_streak() -> None:
    """A RECENT empty contract window is NOT fast-settled — it stays EMPTY 1/3."""
    from brutex.data.window_ledger import EMPTY

    sdk = _FakeSDK(payload=_EMPTY)  # always empty
    ledger = WindowLedger()
    ingest_option_contract(
        _client(sdk),
        _Sink(),
        contract=_ce(),
        interval="1day",
        start=date(2026, 5, 1),
        end=date(2026, 6, 1),  # within 60 days of the injected now
        empty_retry_attempts=3,
        empty_retry_backoff=lambda _a: None,
        ledger=ledger,
        now_ist=lambda: datetime(2026, 6, 19, 8, 0, tzinfo=UTC),
    )
    rec = ledger.records()[0]
    assert rec.status == EMPTY  # recent gap may still fill → conservative streak
    assert rec.empty_streak == 1
    assert rec.in_pull_fetches == 3


def test_ingest_empty_then_transient_then_data() -> None:
    """empty → transient → data: the shared transient counter spans empty rounds correctly.

    Round 1 returns empty (counter still 0), round 2 hits a timeout (consumes 1 transient
    attempt) then re-fetches data on the SAME round — proving an empty round does not steal
    the transient budget and the glitch is fully recovered.
    """
    sdk = _FakeSDK(script=[_EMPTY, GrowwAPITimeoutException("stall"), _candles()])
    sink = _Sink()
    slept: list[int] = []
    report = ingest_option_contract(
        _client(sdk),
        sink,
        contract=_ce(),
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        max_attempts=2,
        backoff=slept.append,
        empty_retry_attempts=3,
        empty_retry_backoff=lambda _a: None,
        now_ist=_now,
    )
    assert report.bars_appended == 1 and report.retried_windows == 1  # data recovered
    assert sdk.calls == 3  # empty, timeout, data
    assert slept == [0]  # exactly one transient backoff consumed (not doubled by the empty)


def test_ingest_empty_then_ratelimit_exhausts_records_failed_with_fetch_count() -> None:
    """empty → 429 (budget spent) → FAILED carrying the MULTI-fetch in-pull count.

    Hostile-review NIT (DEC-WINDOW-INPULL-FETCHES-001): the transient-FAILED in_pull_fetches
    was only asserted for the count==1 case. Here the empty round (fetch 1) then a 429 that
    exhausts the transient budget (fetch 2) records FAILED with in_pull_fetches == 2 — the
    in-pull counter spans BOTH the empty re-fetch and the transient fetch.
    """
    sdk = _FakeSDK(script=[_EMPTY, GrowwAPIRateLimitException("429")])
    ledger = WindowLedger()
    report = ingest_option_contract(
        _client(sdk),
        _Sink(),
        contract=_ce(),
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        ledger=ledger,
        max_attempts=0,  # 429 → immediate budget exhaustion
        empty_retry_attempts=3,
        empty_retry_backoff=lambda _a: None,
        now_ist=_now,
    )
    assert report.still_failed_windows == 1
    assert sdk.calls == 2  # the empty fetch + the 429 fetch
    rec = ledger.records()[0]
    assert rec.status == "FAILED"
    assert rec.in_pull_fetches == 2  # spans the empty re-fetch AND the transient


def test_ingest_empty_retry_default_is_three_with_noop_backoff() -> None:
    """The default attempts is 3 and the default (no-op) backoff is used when none is injected."""
    sdk = _FakeSDK(payload=_EMPTY)
    ingest_option_contract(
        _client(sdk),
        _Sink(),
        contract=_ce(),
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        now_ist=_now,  # no empty_retry_attempts / backoff → defaults (3 + _noop_empty_backoff)
    )
    assert sdk.calls == _oi.DEFAULT_EMPTY_RETRY_ATTEMPTS == 3


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


def test_run_write_pool_routes_through_consumer() -> None:
    """DEC-FETCH-WRITE-OVERLAP-LEVER1-001: a write_pool routes the F&O write through the pool.

    The underlying ``_Sink`` (wrapped by the pool's WriteConsumerSink) STILL receives every
    contract's append — proving the write is durably handed off through the background shard
    and the settle-after-durable ordering preserves the recorded outcome.
    """
    from brutex.data.write_consumer import ShardedWriteConsumer

    ok1, ok2 = _ce(), _ce2()
    sdk = _FakeSDK(payload=_candles())
    sink = _Sink()
    pool = ShardedWriteConsumer(sink=sink, workers=2)
    try:
        report = run_groww_option_ingest(
            _client(sdk),
            sink,
            contracts=[ok1, ok2],
            intervals=["1day"],
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
            now_ist=_now,
            write_pool=pool,
        )
    finally:
        pool.drain()
    assert set(report.pairs) == {(ok1.symbol, "1day"), (ok2.symbol, "1day")}
    assert report.bars_appended == 2
    # Each contract's durable append reached the real sink THROUGH the pool.
    assert {a[2] for a in sink.appends} == {ok1.symbol, ok2.symbol}


def test_run_write_pool_none_uses_raw_sink() -> None:
    """write_pool=None (default) is byte-identical — the raw sink records the appends directly."""
    ok = _ce()
    sdk = _FakeSDK(payload=_candles())
    sink = _Sink()
    report = run_groww_option_ingest(
        _client(sdk),
        sink,
        contracts=[ok],
        intervals=["1day"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        now_ist=_now,
    )
    assert report.bars_appended == 1
    assert {a[2] for a in sink.appends} == {ok.symbol}


def test_run_capture_hook_factory_builds_per_contract_hook() -> None:
    """DEC-RAW-DATA-INTEGRITY-001: with no explicit ``on_raw_payload``, the production

    ``capture_hook_factory`` is invoked PER CONTRACT with that contract's resolved
    ``(exchange, OPTION_SEGMENT, symbol)`` coordinates (option_ingest.py:1026). A spy
    factory records each invocation, proving the per-contract hook is built here (one
    ingest run pulls many contracts, so the hook must be attributed per leg).
    """
    from brutex.adapters.groww.option_ingest import OPTION_SEGMENT, contract_exchange

    ok1, ok2 = _ce(), _ce2()
    sdk = _FakeSDK(payload=_candles())
    built: list[tuple[str, str, str]] = []

    def _factory(
        exchange: str, segment: str, symbol: str
    ) -> Callable[[Mapping[str, object], str, str, str], None]:
        built.append((exchange, segment, symbol))

        def _hook(_payload: Mapping[str, object], _e: str, _s: str, _sym: str) -> None:
            return None

        return _hook

    report = run_groww_option_ingest(
        _client(sdk),
        _Sink(),
        contracts=[ok1, ok2],
        intervals=["1day"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        now_ist=_now,
        capture_hook_factory=_factory,
    )
    assert report.bars_appended == 2
    # The factory was invoked once per contract with that leg's (exchange, FNO, symbol).
    assert set(built) == {
        (contract_exchange(ok1), OPTION_SEGMENT, ok1.symbol),
        (contract_exchange(ok2), OPTION_SEGMENT, ok2.symbol),
    }


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


def test_run_non_brutex_error_propagates() -> None:
    # The sink raising a non-BruteXError (a RuntimeError = a genuine CODE bug, not a
    # brutex DOMAIN error) proves run_groww_option_ingest never swallows a defect
    # (only a BruteXError is quarantined — DEC-GROWW-QUARANTINE-BRUTEX-001).
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


def test_run_out_of_scope_error_quarantines_contract_not_crashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # DEC-GROWW-QUARANTINE-BRUTEX-001: a non-GrowwError BruteXError (an OutOfScopeError —
    # an unresolvable contract symbol) on ONE contract quarantines just that contract and
    # the bulk option pull CONTINUES, exactly like the spot spine — never a crash.
    ok, bad = _ce(), _ce2()

    def _fake_fetch(client, *, contract, interval, start, end, on_raw_payload=None):
        del client, interval, start, end, on_raw_payload
        if contract.symbol == bad.symbol:
            raise OutOfScopeError(f"symbol not in Groww universe: {contract.symbol}")
        return []  # the good contract fetches cleanly (0 bars — a settled empty window)

    monkeypatch.setattr(_oi, "fetch_option_candles", _fake_fetch)
    report = run_groww_option_ingest(
        _client(_FakeSDK(payload=_candles())),
        _Sink(),
        contracts=[ok, bad],
        intervals=["1day"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        now_ist=_now,
    )
    assert report.failed_pairs == 1
    assert (ok.symbol, "1day") in report.pairs  # the good contract still landed
    assert report.failures[0].symbol == bad.symbol
    assert report.failures[0].auth_failure is False  # a scope error is never auth
    assert not report.auth_aborted


@pytest.mark.parametrize(
    "quota_exc",
    [
        DailyQuotaExceeded("daily quota of 100000 calls spent"),
        QuotaInvariantError("quota ledger invariant broke"),
        LakeWriteHalt("disk_full"),  # DEC-LAKE-WRITE-HALT-001 — disk full halts the F&O pull
        LakeWriteHalt("out_of_memory"),
    ],
)
def test_run_global_stop_quota_error_propagates_not_quarantined(
    monkeypatch: pytest.MonkeyPatch, quota_exc: BruteXError
) -> None:
    # DEC-GROWW-QUARANTINE-BRUTEX-001 carve-out (option spine): a quota signal is a GLOBAL
    # stop, NOT one contract's problem — every remaining contract would fail the same way —
    # so it must PROPAGATE (the caller maps it to the quota_exhausted verdict,
    # DEC-SPOT-QUOTA-MIDROUND-001), never be quarantined per contract like an OutOfScopeError.
    def _fake_fetch(client, *, contract, interval, start, end, on_raw_payload=None):
        del client, contract, interval, start, end, on_raw_payload
        raise quota_exc

    monkeypatch.setattr(_oi, "fetch_option_candles", _fake_fetch)
    with pytest.raises(type(quota_exc)):
        run_groww_option_ingest(
            _client(_FakeSDK(payload=_candles())),
            _Sink(),
            contracts=[_ce(), _ce2()],
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


# ─────────────────────────────────────────────────────────────────────────
# run_groww_option_ingest — parallel cross-contract fan-out (DEC-GROWW-OPTION-POOL-001)
# ─────────────────────────────────────────────────────────────────────────
class _RecordingRunner:
    """A deterministic ``RunParallel`` that RECORDS the worklist, then delegates serial.

    Proves the driver hands ``run_parallel`` the right ``(contract, interval)`` jobs and
    actually drains the INJECTED runner (the ``run_parallel is not None`` branch), while
    staying byte-for-byte deterministic (it runs through :class:`SerialRunParallel`).
    """

    def __init__(self) -> None:
        self.jobs: Sequence[object] = ()
        self._inner = SerialRunParallel()

    def __call__[ItemT, ResultT](
        self,
        fn: Callable[[ItemT], ResultT],
        items: Sequence[ItemT],
    ) -> Iterable[ItemResult[ItemT, ResultT]]:
        self.jobs = tuple(items)
        yield from self._inner(fn, items)


def test_run_uses_injected_run_parallel_with_contract_major_worklist() -> None:
    # The injected runner is USED (the run_parallel is-not-None branch) and fed the
    # deterministic contract-major × interval worklist (DEC-GROWW-OPTION-POOL-001).
    ok1, ok2 = _ce(), _ce2()
    runner: _RecordingRunner = _RecordingRunner()
    report = run_groww_option_ingest(
        _client(_FakeSDK(payload=_candles())),
        _Sink(),
        contracts=[ok1, ok2],
        intervals=["1day", "5minute"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        now_ist=_now,
        run_parallel=runner,
    )
    assert list(runner.jobs) == [
        (ok1, "1day"),
        (ok1, "5minute"),
        (ok2, "1day"),
        (ok2, "5minute"),
    ]
    assert set(report.pairs) == {
        (ok1.symbol, "1day"),
        (ok1.symbol, "5minute"),
        (ok2.symbol, "1day"),
        (ok2.symbol, "5minute"),
    }
    assert report.bars_appended == 4


def test_run_parallel_pool_quarantines_one_keeps_others() -> None:
    # Through a REAL thread pool: the per-contract quarantine + report aggregation run on
    # the single controller thread, so the report is order-independent + thread-safe — the
    # pool only changes COMPLETION order, never the result.
    ok1, ok2, bad = _ce(), _ce2(), _ce("NSE-NIFTY-02Jan25-99999-PE")
    sdk = _FakeSDK(payload=_candles(), raise_for={bad.symbol: GrowwAPIBadRequestException("nope")})
    progress: list[tuple[str, int, bool]] = []
    report = run_groww_option_ingest(
        _client(sdk),
        _Sink(),
        contracts=[ok1, ok2, bad],
        intervals=["1day"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        now_ist=_now,
        on_progress=lambda label, bars, failed: progress.append((label, bars, failed)),
        run_parallel=PoolRunParallel(),
    )
    assert set(report.pairs) == {(ok1.symbol, "1day"), (ok2.symbol, "1day")}
    assert report.failed_pairs == 1 and report.bars_appended == 2
    assert (f"{bad.symbol} 1day", 0, True) in progress


def test_run_parallel_pool_global_stop_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    # A GLOBAL stop raised inside a pool worker must still PROPAGATE through the drain
    # (re-raised, never quarantined) AND the pool must shut down cleanly — the test
    # COMPLETING proves the generator-close → ThreadPoolExecutor teardown never hangs.
    def _fake_fetch(client, *, contract, interval, start, end, on_raw_payload=None):
        del client, contract, interval, start, end, on_raw_payload
        raise DailyQuotaExceeded("daily quota spent")

    monkeypatch.setattr(_oi, "fetch_option_candles", _fake_fetch)
    with pytest.raises(DailyQuotaExceeded):
        run_groww_option_ingest(
            _client(_FakeSDK(payload=_candles())),
            _Sink(),
            contracts=[_ce(), _ce2()],
            intervals=["1day"],
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
            now_ist=_now,
            run_parallel=PoolRunParallel(),
        )


def test_ingest_underlying_options_forwards_run_parallel() -> None:
    # ``ingest_underlying_options`` forwards run_parallel verbatim: the resolved CE leg
    # reaches the INJECTED runner (if it were ignored, runner.jobs would stay empty —
    # the default SerialRunParallel would run instead).
    sym = "NSE-NIFTY-25Jan24-100-CE"
    sdk = _FakeSDK(
        payload=_candles(),
        expiries=lambda **kw: {"expiries": ["2024-01-25"]} if kw.get("year") == 2024 else {},
        contracts=lambda **_kw: {"contracts": [sym]},
    )
    runner: _RecordingRunner = _RecordingRunner()
    report = ingest_underlying_options(
        _client(sdk),
        _Sink(),
        exchange="NSE",
        underlying_symbol="NIFTY",
        years=[2024],
        intervals=["1day"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 26),
        now_ist=_now,
        run_parallel=runner,
    )
    assert len(runner.jobs) == 1 and report.bars_appended == 1


def test_ingest_underlying_futures_forwards_run_parallel() -> None:
    # The futures mirror — ``ingest_underlying_futures`` forwards run_parallel too.
    sym = "NSE-NIFTY-25Jan24-FUT"
    sdk = _FakeSDK(
        payload=_candles(),
        expiries=lambda **kw: {"expiries": ["2024-01-25"]} if kw.get("year") == 2024 else {},
        contracts=lambda **_kw: {"contracts": [sym]},
    )
    runner: _RecordingRunner = _RecordingRunner()
    report = ingest_underlying_futures(
        _client(sdk),
        _Sink(),
        exchange="NSE",
        underlying_symbol="NIFTY",
        years=[2024],
        intervals=["1day"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 26),
        now_ist=_now,
        run_parallel=runner,
    )
    assert len(runner.jobs) == 1 and report.bars_appended == 1


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


# ─────────────────────────────────────────────────────────────────────────
# plan_contract_windows — pre-seed PLANNED for /coverage/windows (DEC-FNO-WINDOW-PLAN-001)
# ─────────────────────────────────────────────────────────────────────────
def _fut(symbol: str = "NSE-NIFTY-02Jan25-FUT") -> DerivativeContract:
    return DerivativeContract(
        underlying_symbol="NIFTY", expiry=date(2025, 1, 2), symbol=symbol, contract_type="FUT"
    )


def test_plan_contract_windows_seeds_planned_rows() -> None:
    from brutex.data.window_ledger import PLANNED

    ledger = WindowLedger()
    planned = plan_contract_windows(
        ledger,
        contracts=[_ce("NSE-NIFTY-02Jan25-28500-CE"), _ce("NSE-NIFTY-02Jan25-28600-PE")],
        intervals=("1minute",),
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        market=GROWW_OPTION_COVERAGE_MARKET,
        now_ist=_now,
    )
    assert planned == 2
    recs = ledger.records()
    assert [r.status for r in recs] == [PLANNED, PLANNED]
    assert {r.security_id for r in recs} == {
        "NSE-NIFTY-02Jan25-28500-CE",
        "NSE-NIFTY-02Jan25-28600-PE",
    }
    assert all(r.market == GROWW_OPTION_COVERAGE_MARKET for r in recs)


def test_plan_contract_windows_default_clock_plans_past_range() -> None:
    # ``now_ist`` omitted → the real-IST default clock; a fully-PAST range still plans
    # (clamp leaves a past end unchanged), covering the `_now_ist` default branch.
    from brutex.data.window_ledger import PLANNED

    ledger = WindowLedger()
    planned = plan_contract_windows(
        ledger,
        contracts=[_ce()],
        intervals=("1minute",),
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        market=GROWW_OPTION_COVERAGE_MARKET,
    )
    assert planned == 1
    assert ledger.records()[0].status == PLANNED


def test_plan_contract_windows_empty_range_not_planned() -> None:
    ledger = WindowLedger()
    planned = plan_contract_windows(
        ledger,
        contracts=[_ce()],
        intervals=("1day",),
        start=date(2024, 1, 2),
        end=date(2024, 1, 1),  # start >= end → empty clamped range → no key
        market=GROWW_OPTION_COVERAGE_MARKET,
        now_ist=_now,
    )
    assert planned == 0
    assert ledger.records() == []


def test_plan_contract_windows_key_is_byte_identical_to_fetch() -> None:
    """The planned key EQUALS the fetched key — plan then fetch leaves ONE row (no ghost)."""
    ledger = WindowLedger()
    plan_contract_windows(
        ledger,
        contracts=[_ce()],
        intervals=("1minute",),
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        market=GROWW_OPTION_COVERAGE_MARKET,
        now_ist=_now,
    )
    assert ledger.count == 1  # one PLANNED row
    # Fetch the SAME contract/interval/range: a divergent key would add a SECOND row.
    ingest_option_contract(
        _client(_FakeSDK(payload=_candles())),
        _Sink(),
        contract=_ce(),
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        ledger=ledger,
        now_ist=_now,
    )
    recs = ledger.records()
    assert len(recs) == 1  # the PLANNED row was settled IN PLACE — no orphan ghost
    assert recs[0].status == "SUCCESS"


def test_plan_contract_windows_idempotent_keeps_settled() -> None:
    """Planning never resets a settled row from a prior pull (idempotent)."""
    ledger = WindowLedger()
    key = WindowKey(
        market=GROWW_OPTION_COVERAGE_MARKET,
        security_id="NSE-NIFTY-02Jan25-28500-CE",
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
    )
    ledger.record(key, raw_bars=5, error_type=None, bars_appended=5, now=datetime.now(UTC))
    plan_contract_windows(
        ledger,
        contracts=[_ce()],
        intervals=("1minute",),
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        market=GROWW_OPTION_COVERAGE_MARKET,
        now_ist=_now,
    )
    assert ledger.is_skippable(key)  # still SUCCESS — NOT reset to PLANNED
    assert ledger.records()[0].status == "SUCCESS"


def test_plan_contract_windows_prunes_superseded_shorter_end() -> None:
    """A prior shorter-end row of the planned series is pruned when the new end is longer."""
    from brutex.data.window_ledger import PLANNED

    ledger = WindowLedger()
    stale = WindowKey(
        market=GROWW_OPTION_COVERAGE_MARKET,
        security_id="NSE-NIFTY-02Jan25-28500-CE",
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),  # SHORTER end — superseded by the longer plan below
    )
    ledger.plan(stale, now=datetime.now(UTC))
    planned = plan_contract_windows(
        ledger,
        contracts=[_ce()],
        intervals=("1minute",),
        start=date(2024, 1, 1),
        end=date(2024, 1, 5),  # LONGER end supersedes the stale [.., 1-02) row
        market=GROWW_OPTION_COVERAGE_MARKET,
        now_ist=_now,
    )
    assert planned == 1
    recs = ledger.records()
    assert len(recs) == 1  # the shorter-end orphan was pruned (one O(rows) prune scan)
    assert recs[0].window_end == "2024-01-05" and recs[0].status == PLANNED


def test_plan_contract_windows_futures_namespace() -> None:
    from brutex.data.window_ledger import PLANNED

    ledger = WindowLedger()
    plan_contract_windows(
        ledger,
        contracts=[_fut()],
        intervals=("1minute",),
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        market=GROWW_FUTURE_COVERAGE_MARKET,
        now_ist=_now,
    )
    rec = ledger.records()[0]
    assert rec.market == GROWW_FUTURE_COVERAGE_MARKET and rec.status == PLANNED


def test_run_option_ingest_pre_plans_quarantined_contract_stays_planned() -> None:
    """The driver pre-plans every pair; a quarantined contract keeps its PLANNED row.

    A contract that hits a TERMINAL error is quarantined BEFORE it records an outcome, so
    its pre-seeded PLANNED row survives — exactly the "genuinely-outstanding, never silently
    missing" guarantee the spot planner gives, now visible on the Options tab.
    """
    from brutex.data.window_ledger import PLANNED

    good = _ce("NSE-NIFTY-02Jan25-28500-CE")
    bad = _ce("NSE-NIFTY-02Jan25-99999-PE")
    sdk = _FakeSDK(payload=_candles(), raise_for={bad.symbol: GrowwAPIBadRequestException("nope")})
    ledger = WindowLedger()
    report = run_groww_option_ingest(
        _client(sdk),
        _Sink(),
        contracts=[good, bad],
        intervals=("1minute",),
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        ledger=ledger,
        now_ist=_now,
    )
    assert report.failed_pairs == 1
    by_sym = {r.security_id: r.status for r in ledger.records()}
    assert by_sym["NSE-NIFTY-02Jan25-28500-CE"] == "SUCCESS"  # fetched + settled in place
    assert by_sym["NSE-NIFTY-02Jan25-99999-PE"] == PLANNED  # quarantined → still planned


def test_run_option_ingest_no_ledger_does_not_plan() -> None:
    # ``ledger=None`` (a dry-run) skips planning entirely — byte-identical to the prior path.
    sdk = _FakeSDK(payload=_candles())
    report = run_groww_option_ingest(
        _client(sdk),
        _Sink(),
        contracts=[_ce()],
        intervals=("1minute",),
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        now_ist=_now,
    )
    assert report.bars_appended == 1


# ─────────────────────────────────────────────────────────────────────────
# "Suspicious empty" diagnostic (DEC-SUSPICIOUS-EMPTY-FLAG-001) — observe-only
# ─────────────────────────────────────────────────────────────────────────
def _ireport(
    *, windows: int = 1, bars: int = 0, skipped: int = 0, still_failed: int = 0
) -> IngestReport:
    """An IngestReport with the fields the all-empty scan reads (rest default).

    ``transient_failures`` equals ``still_failed_windows`` (the same final-pass count,
    per the IngestReport contract), so the two stay consistent.
    """
    return IngestReport(
        windows=windows,
        bars_appended=bars,
        transient_failures=still_failed,
        skipped_windows=skipped,
        still_failed_windows=still_failed,
    )


def _udl_fut(symbol: str, underlying: str) -> DerivativeContract:
    """A FUT contract carrying *underlying* (no strike — keeps the factory simple).

    Named distinctly from the module's no-arg ``_fut()`` to avoid shadowing it.
    """
    return DerivativeContract(
        underlying_symbol=underlying,
        expiry=date(2025, 1, 2),
        symbol=symbol,
        contract_type="FUT",
        strike=None,
    )


def _empty_chain(underlying: str, n: int) -> tuple[list[DerivativeContract], dict]:
    """*n* contracts of *underlying* + a pairs map where each was a CLEAN empty fetch."""
    contracts = [_udl_fut(f"NSE-{underlying}-{i:02d}-FUT", underlying) for i in range(n)]
    pairs = {(c.symbol, "1day"): _ireport(windows=1, bars=0) for c in contracts}
    return contracts, pairs


class TestSuspiciousEmptyFlag:
    def test_all_empty_flags_underlying(self) -> None:
        contracts, pairs = _empty_chain("M&M", 3)
        scan = scan_underlying_all_empty(pairs, contracts)
        assert scan.has_suspicious()
        assert scan.suspicious == (
            UnderlyingEmptyReport(underlying="M&M", attempted_pairs=3, stored_bars=0),
        )

    def test_partial_not_flagged(self) -> None:
        # 3 clean fetches but one stored bars → stored_bars > 0 → not suspicious.
        contracts, pairs = _empty_chain("TCS", 3)
        pairs[contracts[0].symbol, "1day"] = _ireport(windows=1, bars=42)
        scan = scan_underlying_all_empty(pairs, contracts)
        assert not scan.has_suspicious()

    def test_all_good_not_flagged(self) -> None:
        contracts, pairs = _empty_chain("INFY", 3)
        pairs = {k: _ireport(windows=1, bars=10) for k in pairs}
        assert not scan_underlying_all_empty(pairs, contracts).has_suspicious()

    def test_below_threshold_not_flagged(self) -> None:
        # Only 2 empty contracts (< _ALL_EMPTY_MIN_CONTRACTS) → not suspicious.
        contracts, pairs = _empty_chain("WIPRO", 2)
        assert not scan_underlying_all_empty(pairs, contracts).has_suspicious()

    def test_skip_only_not_flagged(self) -> None:
        # All ledger-skips (skipped_windows=1) → 0 attempted → never-tried, not flagged.
        contracts, _ = _empty_chain("HDFC", 3)
        pairs = {(c.symbol, "1day"): _ireport(windows=1, skipped=1) for c in contracts}
        assert not scan_underlying_all_empty(pairs, contracts).has_suspicious()

    def test_transient_residual_not_flagged(self) -> None:
        # All still-failing transients → 0 attempted → not flagged (the transient
        # residual verdict's job, not a clean empty).
        contracts, _ = _empty_chain("SBIN", 3)
        pairs = {(c.symbol, "1day"): _ireport(windows=1, still_failed=1) for c in contracts}
        assert not scan_underlying_all_empty(pairs, contracts).has_suspicious()

    def test_empty_clamp_windows_zero_not_flagged(self) -> None:
        # windows == 0 (an empty-clamped no-op range) is not a clean fetch → 0 attempted.
        contracts, _ = _empty_chain("ITC", 3)
        pairs = {(c.symbol, "1day"): _ireport(windows=0) for c in contracts}
        assert not scan_underlying_all_empty(pairs, contracts).has_suspicious()

    def test_unknown_symbol_is_skipped(self) -> None:
        # A pair whose symbol is not in contract_list is defensively skipped (never raises).
        contracts, _ = _empty_chain("RELIANCE", 3)
        pairs = {("NSE-GHOST-99-FUT", "1day"): _ireport(windows=1, bars=0)}
        assert not scan_underlying_all_empty(pairs, contracts).has_suspicious()

    def test_multi_underlying_only_empty_flagged(self) -> None:
        empty_c, empty_p = _empty_chain("M&M", 3)
        good_c, good_p = _empty_chain("TCS", 3)
        good_p = {k: _ireport(windows=1, bars=5) for k in good_p}
        scan = scan_underlying_all_empty({**empty_p, **good_p}, [*empty_c, *good_c])
        assert [r.underlying for r in scan.suspicious] == ["M&M"]

    def test_emit_logs_one_warning_per_flagged(self) -> None:
        contracts, pairs = _empty_chain("M&M", 4)
        scan = scan_underlying_all_empty(pairs, contracts)
        with capture_logs() as logs:
            _oi._log_underlying_all_empty(scan, pull_id="pid-1")
        warns = [r for r in logs if r["event"] == UNDERLYING_ALL_EMPTY_EVENT]
        assert len(warns) == 1
        assert warns[0]["underlying"] == repr("M&M")
        assert warns[0]["attempted_pairs"] == 4 and warns[0]["stored_bars"] == 0

    def test_emit_silent_when_clean(self) -> None:
        with capture_logs() as logs:
            _oi._log_underlying_all_empty(AllEmptyScan(suspicious=()), pull_id="pid-1")
        assert [r for r in logs if r["event"] == UNDERLYING_ALL_EMPTY_EVENT] == []

    def test_emit_caps_sampled_underlyings(self) -> None:
        # More flagged underlyings than the sample cap → only the cap are named.
        flagged = tuple(
            UnderlyingEmptyReport(underlying=f"U{i:02d}", attempted_pairs=3, stored_bars=0)
            for i in range(_oi._ALL_EMPTY_SAMPLE_CAP + 5)
        )
        with capture_logs() as logs:
            _oi._log_underlying_all_empty(AllEmptyScan(suspicious=flagged), pull_id=None)
        warns = [r for r in logs if r["event"] == UNDERLYING_ALL_EMPTY_EVENT]
        assert len(warns) == _oi._ALL_EMPTY_SAMPLE_CAP

    def test_run_ingest_fires_flag_end_to_end(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 3 NIFTY contracts that all fetch cleanly-empty → the wiring fires the WARNING.
        cs = [
            _ce("NSE-NIFTY-02Jan25-28500-CE"),
            _ce("NSE-NIFTY-02Jan25-28200-CE"),
            _ce("NSE-NIFTY-02Jan25-28150-CE"),
        ]
        monkeypatch.setattr(_oi, "fetch_option_candles", lambda *a, **k: [])
        with capture_logs() as logs:
            report = run_groww_option_ingest(
                _client(_FakeSDK(payload=_candles())),
                _Sink(),
                contracts=cs,
                intervals=["1day"],
                start=date(2024, 1, 1),
                end=date(2024, 1, 2),
                now_ist=_now,
            )
        assert report.bars_appended == 0
        warns = [r for r in logs if r["event"] == UNDERLYING_ALL_EMPTY_EVENT]
        assert len(warns) == 1 and warns[0]["underlying"] == repr("NIFTY")

    def test_run_ingest_skips_scan_in_async_pipeline_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MAJOR-3 regression: in async-pipeline mode the all-empty scan is SKIPPED.

        With a ``fetch_pipeline`` wired, each pair's ``IngestReport`` is producer-resolved with
        ``bars_appended == 0`` (the real counts are folded onto the pipeline totals by the
        BACKGROUND consumer, not known synchronously). Scanning now would see EVERY pair as
        0-bars → a false ``pull.underlying_all_empty`` flood for the whole universe. The fix
        skips the scan entirely when a pipeline is active. The scan helper must NOT even be
        called (proven by a spy that raises if it runs).
        """
        from brutex.adapters.groww.fetch_pipeline import FetchPipeline

        cs = [
            _ce("NSE-NIFTY-02Jan25-28500-CE"),
            _ce("NSE-NIFTY-02Jan25-28200-CE"),
            _ce("NSE-NIFTY-02Jan25-28150-CE"),
        ]

        def _must_not_scan(*_a: object, **_k: object) -> AllEmptyScan:
            raise AssertionError("scan_underlying_all_empty must NOT run in pipeline mode")

        monkeypatch.setattr(_oi, "scan_underlying_all_empty", _must_not_scan)
        pipeline = FetchPipeline(workers=1, queue_maxsize=8)
        try:
            with capture_logs() as logs:
                run_groww_option_ingest(
                    _client(_FakeSDK(payload=_candles())),
                    _Sink(),
                    contracts=cs,
                    intervals=["1day"],
                    start=date(2024, 1, 1),
                    end=date(2024, 1, 2),
                    now_ist=_now,
                    fetch_pipeline=pipeline,
                )
        finally:
            pipeline.drain()
        # NO false all-empty WARNING (the scan never ran) and NO scan-error event either.
        assert not any(r["event"] == UNDERLYING_ALL_EMPTY_EVENT for r in logs)
        assert not any(r["event"] == _oi.UNDERLYING_ALL_EMPTY_ERROR_EVENT for r in logs)

    def test_run_ingest_scan_error_is_guarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The observe-only scan raising must NEVER break the pull — it logs an error event
        # and the report still returns.
        def _boom(*_a: object, **_k: object) -> AllEmptyScan:
            raise RuntimeError("scan blew up")

        monkeypatch.setattr(_oi, "scan_underlying_all_empty", _boom)
        with capture_logs() as logs:
            report = run_groww_option_ingest(
                _client(_FakeSDK(payload=_candles())),
                _Sink(),
                contracts=[_ce()],
                intervals=["1day"],
                start=date(2024, 1, 1),
                end=date(2024, 1, 2),
                now_ist=_now,
            )
        assert report.bars_appended == 1  # the pull itself is unaffected
        assert any(r["event"] == _oi.UNDERLYING_ALL_EMPTY_ERROR_EVENT for r in logs)
