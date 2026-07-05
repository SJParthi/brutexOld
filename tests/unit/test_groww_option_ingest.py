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
from datetime import UTC, date, datetime, timedelta
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
from brutex.adapters.groww.contract_bounds import default_fno_minute_window
from brutex.adapters.groww.errors import (
    GrowwTimeoutError,
    GrowwTokenIssuanceThrottled,
    GrowwTokenStillStaleError,
)
from brutex.adapters.groww.ingest import IngestReport
from brutex.adapters.groww.option_ingest import (
    SUSPECTED_PARTIAL_WINDOW_EVENT,
    UNDERLYING_ALL_EMPTY_EVENT,
    AllEmptyScan,
    GrowwOptionIngestReport,
    OptionPairFailure,
    UnderlyingEmptyReport,
    _default_backoff,
    contract_exchange,
    emit_underlying_all_empty,
    fetch_option_candles,
    ingest_option_contract,
    ingest_underlying_futures,
    ingest_underlying_options,
    make_breaker_canary_probe,
    plan_contract_windows,
    reconcile_fno_ledger_windows,
    run_groww_option_ingest,
    scan_underlying_all_empty,
)
from brutex.adapters.protocol import DerivativeContract
from brutex.data import partial_window_detect as _pwd
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


class _HungWriteSink:
    """A sink whose ``append`` BLOCKS past the durable-write deadline (a wedged fsync/replace).

    The hang is REAL — ``append`` waits on an :class:`threading.Event` the test never sets
    until teardown — so ``append_with_deadline``'s ``run_with_deadline`` raises a genuine
    ``TimeoutError`` after the (patched-small) deadline, which the spine reclassifies to a
    real :class:`~brutex.data.errors.WriteDeadlineExceeded`. This drives the F&O defer arm with
    an ACTUAL timeout (not a directly-raised exception), the coordinator-flagged "covered but
    never run" gap. The gate is released at teardown so the abandoned daemon worker exits.
    """

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.entered = threading.Event()
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
        self.entered.set()
        self.gate.wait(timeout=30.0)  # the wedged write — bounded only by the deadline guard
        return SinkAppendResult(bars_appended=0, partitions_written=0, write_results=())


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


class _AmpersandFormSDK:
    """A fake that answers ONLY the ``%26``-encoded form with data (plain form clean-empty)."""

    def __init__(self) -> None:
        self.symbols: list[str] = []

    def get_historical_candles(self, **kw: object) -> Mapping[str, object]:
        symbol = str(kw["groww_symbol"])
        self.symbols.append(symbol)
        return _candles() if "%26" in symbol else {"candles": []}

    def get_expiries(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        raise AssertionError("unused")

    def get_contracts(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        raise AssertionError("unused")


def _mm_ce() -> DerivativeContract:
    """An M&M option contract — the ampersand rides INSIDE the full groww_symbol."""
    return DerivativeContract(
        underlying_symbol="M&M",
        expiry=date(2025, 1, 2),
        symbol="NSE-M&M-02Jan25-2000-CE",
        contract_type="CE",
        strike=2000.0,
    )


def test_fetch_option_candles_ampersand_contract_falls_back_to_encoded_form() -> None:
    """An ``&`` CONTRACT whose plain-form fetch is clean-empty retries `%26`-encoded ONCE.

    DEC-GROWW-AMPERSAND-CANDLE-FALLBACK-001 — the CANDLE-fetch twin of the #898
    resolution fallback: M&M contracts RESOLVE fine but their candles silently read
    empty when the raw ``&`` is sent un-encoded. The shared ``fetch_candle_windows``
    seam recovers them; first non-empty form wins.
    """
    sdk = _AmpersandFormSDK()
    bars = fetch_option_candles(
        GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=1),
        contract=_mm_ce(),
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
    )
    assert len(bars) == 1  # the encoded form's bars came back through the option path
    assert sdk.symbols == ["NSE-M&M-02Jan25-2000-CE", "NSE-M%26M-02Jan25-2000-CE"]


def test_ampersand_fallback_stays_one_logical_fetch_for_ledger_accounting() -> None:
    """The `%26` fallback happens INSIDE one logical fetch — ledger accounting unchanged.

    DEC-GROWW-AMPERSAND-CANDLE-FALLBACK-001 accounting contract: an all-empty ``&``
    contract with ``empty_retry_attempts=2`` records ``in_pull_fetches == 2`` (the
    LOGICAL count the empty-retry / confirmed-empty math keys on) while the vendor
    saw 4 physical calls (plain + encoded per logical attempt) — the physical calls
    still ride ``client.pace`` + the call meter, never the ledger's fetch semantics.
    """

    class _AllEmptySDK(_AmpersandFormSDK):
        def get_historical_candles(self, **kw: object) -> Mapping[str, object]:
            self.symbols.append(str(kw["groww_symbol"]))
            return {"candles": []}  # BOTH forms empty — the honest-empty double-check

    all_empty = _AllEmptySDK()
    ledger = WindowLedger()
    report = ingest_option_contract(
        GrowwClient(sdk=all_empty, sleep=lambda _s: None, max_attempts=1),
        _Sink(),
        contract=_mm_ce(),
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        empty_retry_attempts=2,
        ledger=ledger,
        # A recent clock so the old-gap fast-settle does not fire (mirrors the
        # bounded-empty-retry test above).
        now_ist=lambda: datetime(2024, 1, 10, 8, 0, tzinfo=UTC),
    )
    assert report.windows == 1 and report.bars_appended == 0
    assert len(all_empty.symbols) == 4  # 2 logical fetches × (plain + encoded)
    assert ledger.records()[0].in_pull_fetches == 2  # LOGICAL count — semantics unchanged


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


def test_ingest_drops_window_pace_when_client_self_paces() -> None:
    """A self-pacing client → the per-window pace is DROPPED (single admission).

    DEC-PULL-FORENSIC-WAVE1-001: a live client already admits every physical fetch
    through the shared bucket, so pacing again per window would double-charge the
    500/min governor. With a self-pacing client the window ``pace`` never fires.
    """
    from dataclasses import replace

    sdk = _FakeSDK(payload=_candles())
    paces: list[int] = []
    report = ingest_option_contract(
        replace(_client(sdk), pace=lambda: None),  # a self-pacing client
        _Sink(),
        contract=_ce(),
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        pace=lambda: paces.append(1),
        now_ist=_now,
    )
    assert report.bars_appended == 1
    assert paces == []  # window-level pace dropped — the client is the sole admission


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


def test_ingest_drops_future_dated_phantom_bar() -> None:
    # DEC-CANDLE-DROP-FUTURE-BAR-001: an option bar dated AFTER the clamped window end
    # (a clock-skew / out-of-window phantom) is dropped before the sink, never writing a
    # future partition. 2024-01-05 09:30 IST is IN-SESSION but PAST the Jan-2 window end.
    sdk = _FakeSDK(payload=_candles("2024-01-05T09:30:00"))
    sink = _Sink()
    report = ingest_option_contract(
        _client(sdk),
        sink,
        contract=_ce(),
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),  # the Jan-5 bar is strictly after the clamped end
        now_ist=_now,
    )
    assert report.bars_appended == 0  # the future phantom is dropped before the sink
    assert sink.appends[0][4] == 0


def test_ingest_keeps_on_boundary_bar() -> None:
    # Regression for DEC-CANDLE-DROP-FUTURE-BAR-001: a bar ON the window-end date is KEPT
    # (the drop is STRICTLY-AFTER, so existing boundary behaviour is byte-unchanged).
    sdk = _FakeSDK(payload=_candles(_IN_SESSION))  # 2024-01-02 09:30 IST == the Jan-2 end date
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
    assert report.bars_appended == 1  # the on-boundary bar is KEPT
    assert sink.appends[0][4] == 1


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
    deferred = [r for r in logs if r["event"] == "option.ingest.window.write_deferred"]
    assert deferred  # the unified write-defer WARNING fired
    # A lock-acquire timeout is distinguished from a write-deadline by ``error_type``.
    assert deferred[0]["error_type"] == "PartitionLockTimeout"


def test_ingest_hung_write_real_timeout_defers_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A REAL hung durable write (deadline-exceeding) drives the F&O ``WriteDeadlineExceeded`` arm.

    DEC-FETCH-HUNG-WRITE-DEADLINE-001 — the coordinator-flagged "covered-but-never-run" gap: the
    F&O defer branch (`option_ingest.py` lines 601-611, the ``WriteDeadlineExceeded`` half of the
    ``except (PartitionLockTimeout, WriteDeadlineExceeded)``) was only ever driven by the spot
    path. Here the F&O spine's own durable write HANGS past a patched-small (0.2 s) deadline, so
    ``append_with_deadline``'s ``run_with_deadline`` raises a GENUINE ``TimeoutError`` →
    ``WriteDeadlineExceeded`` → the F&O arm DEFERS the contract window: recorded FAILED + counted
    transient (re-pulls next round), the worker freed, NOT a contract-wide quarantine and NOT an
    infinite block. The lane keeps going (this synchronous call RETURNS in bounded time).
    """
    monkeypatch.setenv("BRUTEX_GROWW_WRITE_DEADLINE_S", "0.2")
    sdk = _FakeSDK(payload=_candles())
    sink = _HungWriteSink()
    ledger = WindowLedger()
    try:
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
    finally:
        sink.gate.set()  # release the abandoned daemon write thread (no orphan)
    assert report.windows == 1
    assert report.bars_appended == 0
    assert report.transient_failures == 1 and report.still_failed_windows == 1
    assert sink.calls == 1  # the fetch ran; the write started then hung
    assert sink.entered.is_set()  # the hang was REAL (the write began)
    key = WindowKey(
        market=GROWW_OPTION_COVERAGE_MARKET,
        security_id="NSE-NIFTY-02Jan25-28500-CE",
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
    )
    assert not ledger.is_skippable(key)  # FAILED — deferred to the next pull


def test_ingest_hung_write_real_timeout_logs_write_deadline_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The F&O real-timeout defer logs ``write_deferred`` with ``error_type=WriteDeadlineExceeded``.

    Proves the defer WARNING distinguishes a hung WRITE (``WriteDeadlineExceeded``) from a lock
    ACQUIRE timeout (``PartitionLockTimeout``) — the ``defer_seconds`` branch on line 609-611 that
    reads ``exc.deadline_seconds`` (NOT ``exc.timeout_seconds``) is exercised by a REAL timeout.
    """
    monkeypatch.setenv("BRUTEX_GROWW_WRITE_DEADLINE_S", "0.2")
    sdk = _FakeSDK(payload=_candles())
    sink = _HungWriteSink()
    try:
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
    finally:
        sink.gate.set()
    assert report.transient_failures == 1
    deferred = [r for r in logs if r["event"] == "option.ingest.window.write_deferred"]
    assert deferred
    assert deferred[0]["error_type"] == "WriteDeadlineExceeded"
    # ``defer_seconds`` carries the exhausted DEADLINE (read via the else-arm exc.deadline_seconds).
    assert deferred[0]["defer_seconds"] == 0.2


def test_ingest_async_pipeline_tail_lock_timeout_folds_transient() -> None:
    """Async-mode tail-side ``PartitionLockTimeout`` folds a tail-transient (lines 519-520).

    With a ``fetch_pipeline`` wired the SUCCESS-path tail runs on a BACKGROUND consumer.
    When that consumer's ``_process_option_window_tail`` hits a wedged-peer lock timeout it
    returns a report with ``still_failed_windows == 1``; the ``_run_tail`` closure must fold
    it via ``pipeline.fold_tail_transient`` (NOT silently drop it) so the across-pull retry
    round still re-fetches the contract (MAJOR-2 fix). The producer's report is the DEFERRED
    zero-bar window; the real transient lands on the pipeline's tally after drain.
    """
    from brutex.adapters.groww.fetch_pipeline import FetchPipeline

    sdk = _FakeSDK(payload=_candles())
    sink = _LockTimeoutSink()
    ledger = WindowLedger()
    pipeline = FetchPipeline(workers=1, queue_maxsize=8)
    try:
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
            fetch_pipeline=pipeline,
        )
    finally:
        pipeline.drain()
    # Producer DEFERRED: a clean fetch, zero bars reported synchronously (the tail is async).
    assert report.windows == 1
    assert report.bars_appended == 0
    assert report.transient_failures == 0 and report.still_failed_windows == 0
    # The BACKGROUND consumer hit the wedged lock and folded ONE tail-transient (519-520).
    assert sink.calls == 1
    assert pipeline.tail_transients == 1
    series = pipeline.per_series_totals()
    assert series[(_ce().symbol, "1day")].transient == 1
    # The window's ledger row is FAILED → a re-pull re-fetches it (deferred, not settled).
    key = WindowKey(
        market=GROWW_OPTION_COVERAGE_MARKET,
        security_id="NSE-NIFTY-02Jan25-28500-CE",
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
    )
    assert not ledger.is_skippable(key)


def test_ingest_async_pipeline_spine_wires_rehydrate_hook() -> None:
    """DEC-FETCH-NONBLOCKING-HANDOFF-001 — the F&O spine wires the spill ``rehydrate`` hook.

    The async F&O spine builds a :class:`~brutex.adapters.groww.fetch_pipeline.FetchTail` whose
    ``rehydrate`` swaps the contract window's bars in place in the captured ``raw`` (the pipeline
    calls it ``rehydrate(())`` on spill / ``rehydrate(loaded)`` on reload). Captured via a fake
    pipeline so the swap is asserted DETERMINISTICALLY (no slow-disk race): calling the wired
    ``rehydrate`` mutates the SAME ``raw_bars`` list the consumer's ``process`` reads, so after a
    reload ``_process_option_window_tail`` appends the RESTORED bars (covers ``option_ingest.py``
    line 533).
    """
    from brutex.adapters.groww.fetch_pipeline import RawFetchResult

    class _CapturePipeline:
        def __init__(self) -> None:
            self.captured: RawFetchResult | None = None
            self.folded: tuple[int, int] | None = None

        def submit(self, result: RawFetchResult) -> None:
            self.captured = result

        def fold_totals(self, *, bars_appended: int, conflicts: int, series_key: object) -> None:
            del series_key
            self.folded = (bars_appended, conflicts)

    sdk = _FakeSDK(payload=_candles())
    sink = _Sink()
    ledger = WindowLedger()
    pipeline = _CapturePipeline()
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
        fetch_pipeline=pipeline,  # type: ignore[arg-type]
    )
    # Producer DEFERRED: one window submitted, zero bars on the synchronous report.
    assert report.windows == 1
    assert report.bars_appended == 0
    captured = pipeline.captured
    assert captured is not None
    tail = captured.tail
    assert tail.rehydrate is not None

    def _option_bar() -> CandleBar:
        return CandleBar(
            timestamp=datetime(2024, 1, 2, 4, 0, tzinfo=UTC),  # 09:30 IST — in-session
            open=Decimal("100.0"),
            high=Decimal("110.0"),
            low=Decimal("90.0"),
            close=Decimal("105.0"),
            volume=50,
            open_interest=7,
        )

    # Drive the wired hook by hand: spill drops the bulk, reload restores it before processing.
    tail.rehydrate(())  # spill
    assert list(tail.raw_bars) == []
    restored = [_option_bar()]
    tail.rehydrate(restored)  # reload
    assert list(tail.raw_bars) == restored
    # The consumer's ``process`` reads the RESTORED bar + appends it (one in-session bar).
    tail.process(tail)
    assert len(sink.appends) == 1
    assert sink.appends[0][4] == 1
    assert pipeline.folded == (1, 0)


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


class _BreakerFeed:
    """Recording ``ErrorStormBreaker`` stand-in (the option twin of the spot spine's spy).

    Captures the exact ``observe_transport_failure`` arguments so the
    auth-ladder-exclusion tests pin the breaker feed BYTE-IDENTICAL to the
    pre-guard behaviour (DEC-AUTH-LADDER-EXCLUDE-001 — the guard withholds only
    the ``on_timeout`` step, never the dead-credential containment chain).
    """

    def __init__(self) -> None:
        self.fed: list[tuple[str, str, object, bool]] = []
        self.successes: list[str] = []

    def observe_transport_failure(
        self, symbol: str, *, error_type: str, window_key: object, still_stale: bool = False
    ) -> None:
        self.fed.append((symbol, error_type, window_key, still_stale))

    def observe_success(self, symbol: str) -> None:
        self.successes.append(symbol)


@pytest.mark.parametrize(
    "error",
    [
        GrowwTokenStillStaleError("re-read returned the same dead token"),
        GrowwTokenIssuanceThrottled("mint throttled"),
    ],
)
def test_ingest_retryable_auth_family_feeds_neither_hook(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """DEC-AUTH-LADDER-EXCLUDE-001 (option-spine twin): auth-family feeds NEITHER hook.

    A retryable auth-family failure (token-stale / mint-throttle) is credential
    state, not candle-endpoint evidence — it must not step the AIMD timeout
    ladder toward the 1 r/s deep floor. The FAILED ledger row, the deferred
    (``still_failed``) report shape, and the storm-breaker feed stay
    byte-identical (the regression: before the guard ``on_timeout()`` fired here).
    """

    def _auth_fetch(*_args: object, **_kw: object) -> list[CandleBar]:
        raise error

    monkeypatch.setattr(_oi, "fetch_option_candles", _auth_fetch)
    ledger = WindowLedger()
    breaker = _BreakerFeed()
    hits: list[int] = []
    timeouts: list[int] = []
    report = ingest_option_contract(
        _client(_FakeSDK(payload=_candles())),
        _Sink(),
        contract=_ce(),
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        on_rate_limit=lambda: hits.append(1),
        on_timeout=lambda: timeouts.append(1),
        ledger=ledger,
        storm_breaker=breaker,  # type: ignore[arg-type]
        max_attempts=0,
        now_ist=_now,
    )
    # NEITHER controller hook fires — no ladder step, no rate step.
    assert timeouts == []
    assert hits == []
    # Containment BYTE-IDENTICAL: deferred (re-pullable), never terminally quarantined…
    assert report.still_failed_windows == 1 and report.transient_failures == 1
    assert report.retried_windows == 0
    # …the ledger row is FAILED with the honest error type + in-pull fetch count…
    [row] = ledger.records()
    assert row.last_error == type(error).__name__
    assert row.in_pull_fetches == 1
    key = WindowKey(
        market=GROWW_OPTION_COVERAGE_MARKET,
        security_id="NSE-NIFTY-02Jan25-28500-CE",
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
    )
    assert not ledger.is_skippable(key)  # FAILED — deferred, never dropped
    # …and the breaker was fed EXACTLY once — the UNDERLYING symbol, the same
    # window key, and the still_stale flag routing the dead-credential streak.
    expected_stale = isinstance(error, GrowwTokenStillStaleError)
    assert breaker.fed == [("NIFTY", type(error).__name__, key, expected_stale)]
    assert breaker.successes == []


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
    ) -> Callable[[Mapping[str, object], str, str, str, str], None]:
        built.append((exchange, segment, symbol))

        def _hook(
            _payload: Mapping[str, object], _e: str, _s: str, _sym: str, _issued: str
        ) -> None:
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


def test_run_terminal_auth_failure_halts_pull_loudly() -> None:
    """A TERMINAL auth failure (dead credential) HALTS the option pull — never a quarantine.

    DEC-GROWW-AUTH-HALT-001 regression: previously a dead token quarantined EVERY
    contract one by one and the pull "completed" all-failed with no critical alert
    (the silent-for-days shape). It is now a ``_GLOBAL_STOP_ERRORS`` member and
    propagates so the halt-alert machinery fires upstream. The GOOD contract that
    was going to be pulled after the bad one is NOT silently consumed into a
    fake-complete report.
    """
    from brutex.adapters.groww.errors import GrowwAuthError

    bad, ok = _ce(), _ce2()
    sdk = _FakeSDK(raise_for={bad.symbol: GrowwAPIAuthenticationException("dead token")})
    with pytest.raises(GrowwAuthError):
        run_groww_option_ingest(
            _client(sdk),
            _Sink(),
            contracts=[bad, ok],
            intervals=["1day"],
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
            now_ist=_now,
        )


def test_run_retryable_auth_subclass_quarantines_not_halts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A RETRYABLE auth-family shape (stale read-only re-read) stays a per-contract quarantine.

    DEC-GROWW-AUTH-HALT-001 carve-out: ``GrowwTokenStillStaleError`` is a
    ``GrowwAuthError`` SUBCLASS but transient by design — it must NEVER halt the
    pull, even when it escapes a worker terminally (the client raises it once its
    bounded retry budget is spent). The drain check carves it back out of the
    ``GrowwAuthError`` global-stop membership.
    """
    from brutex.adapters.groww.errors import GrowwTokenStillStaleError

    bad, ok = _ce(), _ce2()

    def _fake_ingest(client, sink, *, contract, interval, start, end, **_kw):
        del client, sink, interval, start, end
        if contract.symbol == bad.symbol:
            raise GrowwTokenStillStaleError("re-read returned the same dead token")
        return IngestReport(windows=1, bars_appended=1, transient_failures=0)

    monkeypatch.setattr(_oi, "ingest_option_contract", _fake_ingest)
    report = run_groww_option_ingest(
        _client(_FakeSDK(payload=_candles())),
        _Sink(),
        contracts=[bad, ok],
        intervals=["1day"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        now_ist=_now,
    )
    assert report.failed_pairs == 1  # the ONE contract quarantined, the pull continued
    assert (ok.symbol, "1day") in report.pairs
    assert report.failures[0].auth_failure is True  # still honestly flagged as auth-family
    assert report.auth_aborted is True  # the derived flag keeps working for the retry engine


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


def _now_far() -> datetime:
    """A frozen clock AFTER the stable-start test's simulated envelopes (clamp = no-op)."""
    return datetime(2026, 7, 10, 8, 0, tzinfo=UTC)


def test_plan_contract_windows_open_far_contract_stable_start_converges() -> None:
    """DEC-FNO-FAR-EXPIRY-WINDOW-001: once OPEN, 5 daily default pulls converge to ONE row.

    The strict-window law fixes the start at ``expiry − lookback`` forever, so a
    daily pull moves ONLY the end — the EXISTING trailing-END rule
    (DEC-WINDOW-TRAILING-GC-001) collects yesterday's row with NO new GC mechanism
    (the hostile-review 5-pull repro, re-run under the stable-start semantics: no
    moving anchor exists, so no orphan class exists).
    """
    contract = DerivativeContract(
        underlying_symbol="NIFTY",
        expiry=date(2026, 7, 15),
        symbol="NSE-NIFTY-15Jul26-28500-CE",
        contract_type="CE",
        strike=28500.0,
    )
    ledger = WindowLedger()
    full_start = date(2020, 1, 1)
    for offset in range(5):
        full_end = date(2026, 7, 1) + timedelta(days=offset)  # the advancing prev-day
        window = default_fno_minute_window(
            contract.expiry, full_start=full_start, full_end=full_end, is_option=True
        )
        assert window is not None  # opens 2026-06-15 — inside every simulated envelope
        assert window[0] == date(2026, 6, 15)  # expiry−30d: the start NEVER moves
        plan_contract_windows(
            ledger,
            contracts=[contract],
            intervals=("1minute",),
            start=full_start,
            end=full_end,
            market=GROWW_OPTION_COVERAGE_MARKET,
            contract_windows={contract.symbol: window},
            now_ist=_now_far,
        )
        ledger.record(
            WindowKey(
                market=GROWW_OPTION_COVERAGE_MARKET,
                security_id=contract.symbol,
                interval="1minute",
                start=window[0],
                end=window[1],
            ),
            raw_bars=5,
            error_type=None,
            bars_appended=5,
            now=datetime.now(UTC),
        )
    assert ledger.count == 1  # the end-rule alone collects the dailies — no orphans
    record = ledger.records()[0]
    assert (record.window_start, record.window_end) == ("2026-06-15", "2026-07-05")
    assert record.status == "SUCCESS"


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

    def test_low_leg_two_clean_empty_flags(self) -> None:
        # REWRITTEN by DEC-SILENT-VANISH-LOWLEG-001 (was ``test_below_threshold_not_flagged``,
        # which pinned the 1-2-leg gap as intended): a PURE 2-leg all-clean-empty chain —
        # no skip / transient / clamp / quarantine sibling — NOW flags, marked low_leg.
        contracts, pairs = _empty_chain("WIPRO", 2)
        scan = scan_underlying_all_empty(pairs, contracts)
        assert scan.suspicious == (
            UnderlyingEmptyReport(
                underlying="WIPRO", attempted_pairs=2, stored_bars=0, low_leg=True
            ),
        )

    def test_low_leg_one_clean_empty_flags(self) -> None:
        # The 1-leg illiquid twin: a single clean-empty leg with NO disqualifying sibling flags.
        contracts, pairs = _empty_chain("JIOFIN", 1)
        scan = scan_underlying_all_empty(pairs, contracts)
        assert scan.suspicious == (
            UnderlyingEmptyReport(
                underlying="JIOFIN", attempted_pairs=1, stored_bars=0, low_leg=True
            ),
        )

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

    # ── DEC-SILENT-VANISH-LOWLEG-001: every excluded false-positive shape regression-pinned.
    # The low-leg Tier B rule must NOT reintroduce the shapes the #919 guards exclude —
    # each disqualifying sibling shape is pinned individually.

    def test_low_leg_skip_sibling_not_flagged(self) -> None:
        # RE-PULL shape: 1 clean-empty + 1 ledger-skipped sibling → NOT flagged (an
        # already-settled sibling means this is not a fresh whole-chain vendor answer).
        contracts, _ = _empty_chain("HDFC", 2)
        pairs = {
            (contracts[0].symbol, "1day"): _ireport(windows=1, bars=0),
            (contracts[1].symbol, "1day"): _ireport(windows=1, skipped=1),
        }
        assert not scan_underlying_all_empty(pairs, contracts).has_suspicious()

    def test_low_leg_transient_sibling_not_flagged(self) -> None:
        # TRANSIENT-storm shape: 1 clean-empty + 1 still-failing sibling → NOT flagged.
        contracts, _ = _empty_chain("SBIN", 2)
        pairs = {
            (contracts[0].symbol, "1day"): _ireport(windows=1, bars=0),
            (contracts[1].symbol, "1day"): _ireport(windows=1, still_failed=1),
        }
        assert not scan_underlying_all_empty(pairs, contracts).has_suspicious()

    def test_low_leg_empty_clamp_sibling_not_flagged(self) -> None:
        # EMPTY-CLAMP shape: 1 clean-empty + 1 windows==0 no-op sibling → NOT flagged.
        contracts, _ = _empty_chain("ITC", 2)
        pairs = {
            (contracts[0].symbol, "1day"): _ireport(windows=1, bars=0),
            (contracts[1].symbol, "1day"): _ireport(windows=0),
        }
        assert not scan_underlying_all_empty(pairs, contracts).has_suspicious()

    def test_low_leg_quarantined_sibling_not_flagged(self) -> None:
        # QUARANTINE shape: 1 clean-empty + 1 terminally-quarantined sibling (never enters
        # ``pairs``) → NOT flagged — "every attempted contract was a clean empty" is false.
        contracts, _ = _empty_chain("TATASTEEL", 2)
        pairs = {(contracts[0].symbol, "1day"): _ireport(windows=1, bars=0)}
        scan = scan_underlying_all_empty(pairs, contracts, [contracts[1].symbol])
        assert not scan.has_suspicious()

    def test_low_leg_stored_bars_sibling_not_flagged(self) -> None:
        # MIXED chain: 1 clean-empty + 1 clean-with-bars → stored > 0 → NOT flagged.
        contracts, _ = _empty_chain("INFY", 2)
        pairs = {
            (contracts[0].symbol, "1day"): _ireport(windows=1, bars=0),
            (contracts[1].symbol, "1day"): _ireport(windows=1, bars=11),
        }
        assert not scan_underlying_all_empty(pairs, contracts).has_suspicious()

    def test_low_leg_unknown_failed_symbol_is_ignored(self) -> None:
        # A quarantined symbol NOT in contract_list is defensively dropped (mirrors the
        # unknown-pair drop) — it cannot disqualify an unrelated 1-leg clean-empty chain.
        contracts, pairs = _empty_chain("RELIANCE", 1)
        scan = scan_underlying_all_empty(pairs, contracts, ["NSE-GHOST-99-FUT"])
        assert [r.underlying for r in scan.suspicious] == ["RELIANCE"]

    def test_low_leg_never_planned_sibling_still_flags(self) -> None:
        # HONEST-residual pin (docstring-documented, accepted): a NEVER-PLANNED sibling —
        # a far-future contract skipped at planning time (fno.window_not_open,
        # DEC-FNO-FAR-EXPIRY-WINDOW-001: zero vendor calls, zero ledger rows, no
        # IngestReport pair, no failure) — is structurally INVISIBLE to Tier B, so a
        # 1-clean-empty-near-leg + 1-not-yet-open-far-leg chain STILL flags low_leg=True.
        # Consistent with tried-vs-never-tried (Tier A ignores never-tried identically);
        # observe-only WARN noise, deliberately not mechanised away.
        contracts, _ = _empty_chain("BEL", 2)
        pairs = {(contracts[0].symbol, "1day"): _ireport(windows=1, bars=0)}
        # contracts[1] was never planned: absent from pairs AND from failed_symbols.
        scan = scan_underlying_all_empty(pairs, contracts, [])
        assert scan.suspicious == (
            UnderlyingEmptyReport(underlying="BEL", attempted_pairs=1, stored_bars=0, low_leg=True),
        )

    def test_tier_a_ignores_nonclean_and_failed_siblings(self) -> None:
        # The ≥3 Tier A rule must NOT become stricter: 3 clean empties + a skipped sibling
        # + a quarantined sibling STILL flag (non-clean/failed siblings ignored, as shipped),
        # and the report is NOT low_leg.
        contracts = [_udl_fut(f"NSE-VEDL-{i:02d}-FUT", "VEDL") for i in range(5)]
        pairs = {(c.symbol, "1day"): _ireport(windows=1, bars=0) for c in contracts[:3]}
        pairs[contracts[3].symbol, "1day"] = _ireport(windows=1, skipped=1)
        scan = scan_underlying_all_empty(pairs, contracts, [contracts[4].symbol])
        assert scan.suspicious == (
            UnderlyingEmptyReport(
                underlying="VEDL", attempted_pairs=3, stored_bars=0, low_leg=False
            ),
        )

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
        assert warns[0]["low_leg"] is False  # a Tier A whole-chain flag, not the low-leg tier

    def test_emit_logs_low_leg_marker(self) -> None:
        # DEC-SILENT-VANISH-LOWLEG-001: a Tier B flag carries low_leg=True in the payload
        # so triage can weigh a 1-leg warning lower than a whole-chain one.
        contracts, pairs = _empty_chain("WIPRO", 1)
        scan = scan_underlying_all_empty(pairs, contracts)
        with capture_logs() as logs:
            _oi._log_underlying_all_empty(scan, pull_id="pid-1")
        warns = [r for r in logs if r["event"] == UNDERLYING_ALL_EMPTY_EVENT]
        assert len(warns) == 1
        assert warns[0]["underlying"] == repr("WIPRO")
        assert warns[0]["attempted_pairs"] == 1
        assert warns[0]["low_leg"] is True

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

    def test_run_ingest_fires_low_leg_flag_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # DEC-SILENT-VANISH-LOWLEG-001: a 1-leg chain whose only contract fetches
        # cleanly-empty fires the WARNING through the sync driver, marked low_leg.
        monkeypatch.setattr(_oi, "fetch_option_candles", lambda *a, **k: [])
        with capture_logs() as logs:
            report = run_groww_option_ingest(
                _client(_FakeSDK(payload=_candles())),
                _Sink(),
                contracts=[_ce("NSE-NIFTY-02Jan25-28500-CE")],
                intervals=["1day"],
                start=date(2024, 1, 1),
                end=date(2024, 1, 2),
                now_ist=_now,
            )
        assert report.bars_appended == 0
        warns = [r for r in logs if r["event"] == UNDERLYING_ALL_EMPTY_EVENT]
        assert len(warns) == 1
        assert warns[0]["underlying"] == repr("NIFTY")
        assert warns[0]["low_leg"] is True

    def test_run_ingest_quarantined_sibling_suppresses_low_leg_flag(self) -> None:
        # DEC-SILENT-VANISH-LOWLEG-001: the sync driver threads its quarantined failures
        # into the scan — a 2-leg chain with 1 clean-empty + 1 terminally-quarantined leg
        # must NOT flag (the quarantine never enters ``pairs``, so without the threading
        # it would be indistinguishable from a genuine 1-leg all-clean-empty chain).
        good = _ce("NSE-NIFTY-02Jan25-28500-CE")
        bad = _ce("NSE-NIFTY-02Jan25-99999-PE")
        sdk = _FakeSDK(payload=_EMPTY, raise_for={bad.symbol: GrowwAPIBadRequestException("nope")})
        with capture_logs() as logs:
            report = run_groww_option_ingest(
                _client(sdk),
                _Sink(),
                contracts=[good, bad],
                intervals=["1day"],
                start=date(2024, 1, 1),
                end=date(2024, 1, 2),
                now_ist=_now,
            )
        assert report.failed_pairs == 1  # the quarantine happened
        assert [r for r in logs if r["event"] == UNDERLYING_ALL_EMPTY_EVENT] == []

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

    # ── emit_underlying_all_empty: the COMMON guarded scan→log helper ─────────
    # (DEC-EMPTY-DETECTOR-PRODUCTION-WIRING-001) reused by the in-driver path AND
    # the production post-drain call site in dashboard.historical_pull.

    def test_emit_helper_warns_on_silent_vanish(self) -> None:
        # A whole-underlying empty chain → ONE pull.underlying_all_empty WARNING.
        contracts, pairs = _empty_chain("M&M", 3)
        with capture_logs() as logs:
            emit_underlying_all_empty(pairs, contracts, pull_id="pid-9")
        warns = [r for r in logs if r["event"] == UNDERLYING_ALL_EMPTY_EVENT]
        assert len(warns) == 1
        assert warns[0]["underlying"] == repr("M&M")
        assert warns[0]["pull_id"] == "pid-9"

    def test_emit_helper_silent_when_clean(self) -> None:
        contracts, pairs = _empty_chain("TCS", 3)
        pairs = {k: _ireport(windows=1, bars=7) for k in pairs}
        with capture_logs() as logs:
            emit_underlying_all_empty(pairs, contracts, pull_id="pid-9")
        assert [r for r in logs if r["event"] == UNDERLYING_ALL_EMPTY_EVENT] == []
        assert [r for r in logs if r["event"] == _oi.UNDERLYING_ALL_EMPTY_ERROR_EVENT] == []

    def test_emit_helper_guards_scan_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A scan-internal error is swallowed + logged — NEVER propagates.
        def _boom(*_a: object, **_k: object) -> AllEmptyScan:
            raise RuntimeError("scan blew up")

        monkeypatch.setattr(_oi, "scan_underlying_all_empty", _boom)
        contracts, pairs = _empty_chain("INFY", 3)
        with capture_logs() as logs:
            emit_underlying_all_empty(pairs, contracts, pull_id="pid-9")
        errs = [r for r in logs if r["event"] == _oi.UNDERLYING_ALL_EMPTY_ERROR_EVENT]
        assert len(errs) == 1
        assert errs[0]["pull_id"] == "pid-9"

    def test_emit_helper_defaults_pull_id_from_scope(self) -> None:
        # No explicit pull_id → resolves the current pull-scope id (current_pull_id()).
        from brutex.observability.correlation import pull_id_scope

        contracts, pairs = _empty_chain("WIPRO", 3)
        with pull_id_scope("scoped-pid"), capture_logs() as logs:
            emit_underlying_all_empty(pairs, contracts)
        warns = [r for r in logs if r["event"] == UNDERLYING_ALL_EMPTY_EVENT]
        assert len(warns) == 1 and warns[0]["pull_id"] == "scoped-pid"


def _partial_bar(d: date) -> CandleBar:
    """A valid in-session F&O bar whose IST trading date is *d* (for truncation tests)."""
    return CandleBar(
        timestamp=datetime(d.year, d.month, d.day, 4, 0, tzinfo=UTC),  # 09:30 IST = 04:00 UTC
        open=Decimal("100.0"),
        high=Decimal("110.0"),
        low=Decimal("90.0"),
        close=Decimal("105.0"),
        volume=50,
        open_interest=7,
    )


def _raw_window(
    *,
    last_bar_date: date | None,
    window_end: date,
    expiry: date | None,
    contract_type: str = "FUT",
) -> _oi._OptionRawWindow:
    """An ``_OptionRawWindow`` carrying one bar on *last_bar_date* (or empty), end *window_end*.

    Defaults to a FUT contract (the watched type). A ``CE``/``PE`` is built when
    *contract_type* asks (to prove the option-exclusion gate). Covered years (2022-2025)
    so the real NSE calendar is exercised (MAJOR-1 fix).
    """
    is_option = contract_type in {"CE", "PE"}
    symbol = "NSE-NIFTY-30Mar24-22000-CE" if is_option else "NSE-NIFTY-30Mar24-FUT"
    contract = DerivativeContract(
        underlying_symbol="NIFTY",
        expiry=expiry,
        symbol=symbol,
        contract_type=contract_type,
        strike=22000.0 if is_option else None,
    )
    key = WindowKey(
        market="groww-future" if not is_option else "groww-option",
        security_id=contract.symbol,
        interval="1day",
        start=date(2024, 1, 1),
        end=window_end,
    )
    bars = [] if last_bar_date is None else [_partial_bar(last_bar_date)]
    return _oi._OptionRawWindow(
        contract=contract,
        interval="1day",
        exchange="NSE",
        key=key,
        raw_bars=bars,
        fetch_count=1,
        settle_empty=False,
        old_window=False,
        attempt=0,
    )


def _observe(raw: _oi._OptionRawWindow) -> None:
    """Run the FUT-gated observe wiring over *raw*'s session-filtered bars."""
    _oi._observe_partial_window(
        raw,
        list(raw.raw_bars),
        pull_id="pid-1",
        fields={"symbol": raw.contract.symbol, "interval": "1day"},
    )


class TestPartialWindowFlag:
    """DEC-PARTIAL-WINDOW-DETECT-001 — observe-only truncated-SUCCESS-window WARNING (FUT only)."""

    def test_truncated_futures_window_fires_one_warning(self) -> None:
        # A FUTURES SUCCESS window whose last bar (2024-01-08) stops far before its end
        # (2024-03-01), contract still live to 2025 → the suspected-partial WARNING fires once.
        raw = _raw_window(
            last_bar_date=date(2024, 1, 8),
            window_end=date(2024, 3, 1),
            expiry=date(2025, 1, 30),
            contract_type="FUT",
        )
        with capture_logs() as logs:
            _observe(raw)
        warns = [r for r in logs if r["event"] == SUSPECTED_PARTIAL_WINDOW_EVENT]
        assert len(warns) == 1
        assert warns[0]["symbol"] == repr(raw.contract.symbol)
        assert warns[0]["last_bar_date"] == "2024-01-08"
        assert warns[0]["missing_sessions"] > 5

    def test_option_pre_expiry_is_excluded(self) -> None:
        # MINOR-2 / BLOCKER-1: an OPTION (CE) whose bars die 7 sessions before expiry — a
        # COMPLETE-but-illiquid contract — is NOT flagged because options are excluded entirely.
        raw = _raw_window(
            last_bar_date=date(2024, 3, 19),
            window_end=date(2024, 3, 29),  # expiry+1
            expiry=date(2024, 3, 28),
            contract_type="CE",
        )
        with capture_logs() as logs:
            _observe(raw)
        assert [r for r in logs if r["event"] == SUSPECTED_PARTIAL_WINDOW_EVENT] == []

    def test_put_option_also_excluded(self) -> None:
        # The PE leg is excluded too (both option legs trade sparsely near expiry).
        raw = _raw_window(
            last_bar_date=date(2024, 1, 8),
            window_end=date(2024, 3, 1),
            expiry=date(2025, 1, 30),
            contract_type="PE",
        )
        with capture_logs() as logs:
            _observe(raw)
        assert [r for r in logs if r["event"] == SUSPECTED_PARTIAL_WINDOW_EVENT] == []

    def test_sparse_complete_window_silent(self) -> None:
        # Last bar reaches the window end → COMPLETE → no WARNING (illiquid-but-complete guard).
        raw = _raw_window(
            last_bar_date=date(2024, 2, 29),
            window_end=date(2024, 3, 1),
            expiry=date(2025, 1, 30),
        )
        with capture_logs() as logs:
            _observe(raw)
        assert [r for r in logs if r["event"] == SUSPECTED_PARTIAL_WINDOW_EVENT] == []

    def test_empty_window_silent(self) -> None:
        # An EMPTY window (no bars) is never a truncation → silent.
        raw = _raw_window(last_bar_date=None, window_end=date(2024, 3, 1), expiry=date(2025, 1, 30))
        with capture_logs() as logs:
            _observe(raw)
        assert [r for r in logs if r["event"] == SUSPECTED_PARTIAL_WINDOW_EVENT] == []

    def test_diagnostic_error_is_guarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The classifier raising must NEVER break the pull — it logs the error event instead.
        def _boom(*_a: object, **_k: object) -> object:
            raise RuntimeError("classify blew up")

        monkeypatch.setattr(_pwd, "classify_partial_window", _boom)
        raw = _raw_window(
            last_bar_date=date(2024, 1, 8), window_end=date(2024, 3, 1), expiry=date(2025, 1, 30)
        )
        with capture_logs() as logs:
            _observe(raw)
        assert any(r["event"] == _oi.SUSPECTED_PARTIAL_WINDOW_ERROR_EVENT for r in logs)
        assert [r for r in logs if r["event"] == SUSPECTED_PARTIAL_WINDOW_EVENT] == []

    def test_wired_into_success_tail(self) -> None:
        # End-to-end through the SUCCESS-path consumer tail: a truncated FUT window fires the flag.
        raw = _raw_window(
            last_bar_date=date(2024, 1, 8), window_end=date(2024, 3, 1), expiry=date(2025, 1, 30)
        )
        with capture_logs() as logs:
            report = _oi._process_option_window_tail(
                _Sink(),
                raw,
                ledger=None,
                on_success=None,
                pull_id="pid-1",
                fields={"symbol": raw.contract.symbol, "interval": "1day"},
            )
        assert report.bars_appended == 1  # the SUCCESS write happened
        warns = [r for r in logs if r["event"] == SUSPECTED_PARTIAL_WINDOW_EVENT]
        assert len(warns) == 1


# ─────────────────────────────────────────────────────────────────────────
# DEC-FETCH-PER-WINDOW-GUARANTEE-001 — per-contract EMPTY-retry budget routing
# + the CRITICAL correctness contract: a REDUCED budget bounds ONLY clean-empty
# re-confirmations and is NEVER counted/triggered by a transient (429 / timeout /
# 5xx / empty-body-throttle / network), which ALWAYS retries via the separate
# transient ladder. Every permutation has its own test (the operator's data-loss
# concern). The budget map keys per-symbol so a far-OTM strike gets the reduced
# budget while a near-money strike keeps the full budget — proven end-to-end.
# ─────────────────────────────────────────────────────────────────────────


class _GenericServerError(Exception):
    """A generic SDK exception whose MESSAGE maps to a retryable GrowwServerError (5xx)."""


class _PerSymbolSDK:
    """A ``GrowwSDK``-shaped fake scripting a per-SYMBOL behaviour + COUNTING fetches per symbol.

    ``behaviour[symbol]`` is a callable ``(call_index) -> Mapping | Exception`` so a test can,
    per strike, return clean-empty / data / raise a transient on the Nth call. The default
    (a symbol not in the map) returns a clean-empty 200. ``fetches[symbol]`` is the per-symbol
    vendor-call count — the fetches-per-window evidence.
    """

    def __init__(self, behaviour: Mapping[str, Callable[[int], object]] | None = None) -> None:
        self._behaviour = dict(behaviour or {})
        self.fetches: dict[str, int] = {}

    def get_historical_candles(self, **kw: object) -> Mapping[str, object]:
        symbol = str(kw["groww_symbol"])
        idx = self.fetches.get(symbol, 0)
        self.fetches[symbol] = idx + 1
        fn = self._behaviour.get(symbol)
        if fn is None:
            return _EMPTY  # default: a CLEAN empty 200 (a dead strike)
        out = fn(idx)
        if isinstance(out, BaseException):
            raise out
        return out  # type: ignore[return-value]

    def get_expiries(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover - unused
        return {"expiries": []}

    def get_contracts(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover - unused
        return {"contracts": []}


def _opt(strike: int, opt_type: str = "CE") -> DerivativeContract:
    return DerivativeContract(
        underlying_symbol="NIFTY",
        expiry=date(2024, 1, 30),
        symbol=f"NSE-NIFTY-30Jan24-{strike}-{opt_type}",
        contract_type=opt_type,
        strike=float(strike),
    )


def _past_clock() -> datetime:
    """A clock far AFTER the test window so windows are firmly past, no current-day clamp."""
    return datetime(2024, 6, 1, 8, 0, tzinfo=UTC)


def _run_with_budget(
    sdk: _PerSymbolSDK,
    contracts: Sequence[DerivativeContract],
    *,
    empty_retry_by_symbol: Mapping[str, int] | None,
    max_attempts: int = 2,
    ledger: WindowLedger | None = None,
) -> GrowwOptionIngestReport:
    # Build the client directly (not via ``_client``, which is narrowly typed to ``_FakeSDK``):
    # ``_PerSymbolSDK`` satisfies the structural ``GrowwSDK`` Protocol. ``max_attempts=1`` so the
    # client's own fast-retry never fires — the ingest-level transient ladder is what is tested.
    client = GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=1)
    return run_groww_option_ingest(
        client,
        _Sink(),
        contracts=list(contracts),
        intervals=["1day"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 5),
        now_ist=_past_clock,
        max_attempts=max_attempts,
        backoff=lambda _a: None,
        ledger=ledger,
        empty_retry_by_symbol=empty_retry_by_symbol,
    )


def test_budget_clean_empty_far_otm_bounded_to_reduced_fetch() -> None:
    """PERMUTATION 1 — first-attempt CLEAN 200-empty far-OTM strike → bounded to ONE fetch.

    A deep strike with a reduced empty-retry budget (1) is fetched exactly ONCE to confirm its
    clean-empty (vs the full 3), driving fetches-per-window down with no data lost.
    """
    deep = _opt(18000)
    sdk = _PerSymbolSDK()  # everything returns a clean empty 200
    _run_with_budget(sdk, [deep], empty_retry_by_symbol={deep.symbol: 1})
    assert sdk.fetches[deep.symbol] == 1  # ONE fetch — the reduced budget bounded it


def test_budget_near_money_keeps_full_empty_retry() -> None:
    """PERMUTATION 6 — a near-money strike is NEVER reduced: it keeps the full 3 empty-fetches.

    The budget map is sparse: a symbol absent from it keeps the full ``empty_retry_attempts``,
    so a near-money empty is re-confirmed the full 3 times (never short-circuited).
    """
    near = _opt(23000)
    deep = _opt(18000)
    sdk = _PerSymbolSDK()
    # only the deep strike is mapped to the reduced budget; near is absent -> full default (3)
    _run_with_budget(sdk, [near, deep], empty_retry_by_symbol={deep.symbol: 1})
    assert sdk.fetches[near.symbol] == 3  # near-money kept the FULL budget
    assert sdk.fetches[deep.symbol] == 1  # deep reduced


def test_budget_429_always_retried_never_counted_as_empty() -> None:
    """PERMUTATION 2 — first-attempt 429 on a reduced-budget strike → retried, NOT bounded.

    A 429 raises a retryable GrowwRateLimitError handled by the SEPARATE transient ladder
    (max_attempts), never the empty-retry budget — so the reduced budget does NOT short-circuit
    a transient. Here the 429 clears on the retry and data lands.
    """
    deep = _opt(18000)
    sdk = _PerSymbolSDK(
        {deep.symbol: lambda i: GrowwAPIRateLimitException("429") if i == 0 else _candles()}
    )
    report = _run_with_budget(sdk, [deep], empty_retry_by_symbol={deep.symbol: 1}, max_attempts=2)
    # the 429 was retried (transient ladder), then data landed — NOT bounded to 1 by the budget
    assert sdk.fetches[deep.symbol] == 2
    assert report.bars_appended == 1  # data captured — no loss


def test_budget_timeout_always_retried_never_counted_as_empty() -> None:
    """PERMUTATION 3 — first-attempt timeout on a reduced-budget strike → retried, NOT bounded."""
    deep = _opt(18000)
    sdk = _PerSymbolSDK(
        {deep.symbol: lambda i: GrowwAPITimeoutException("stall") if i == 0 else _candles()}
    )
    report = _run_with_budget(sdk, [deep], empty_retry_by_symbol={deep.symbol: 1}, max_attempts=2)
    assert sdk.fetches[deep.symbol] == 2  # retried past the transient, NOT bounded
    assert report.bars_appended == 1


def test_budget_5xx_always_retried_never_counted_as_empty() -> None:
    """PERMUTATION 4 — first-attempt 5xx server error → retried (GrowwServerError), NOT bounded."""
    deep = _opt(18000)
    sdk = _PerSymbolSDK(
        {
            deep.symbol: lambda i: (
                _GenericServerError("internal server error") if i == 0 else _candles()
            )
        }
    )
    report = _run_with_budget(sdk, [deep], empty_retry_by_symbol={deep.symbol: 1}, max_attempts=2)
    assert sdk.fetches[deep.symbol] == 2  # the 5xx was retried, NOT counted as a clean-empty
    assert report.bars_appended == 1


def test_budget_empty_body_throttle_always_retried_never_counted_as_empty() -> None:
    """PERMUTATION 5 — an empty/non-JSON body (throttle) → RETRYABLE, re-pulled, NOT bounded.

    DEC-POSTPULL-DATAQUALITY-BATCH-001 #854 maps a ``json.JSONDecodeError`` body to a RETRYABLE
    GrowwEmptyResponseError precisely so a throttle-empty is re-pulled, NOT treated as no-data.
    A reduced budget must NOT short-circuit it — it rides the transient ladder.
    """
    import json

    deep = _opt(18000)

    def _throttle_then_data(i: int) -> object:
        if i == 0:
            return json.JSONDecodeError("Expecting value", "", 0)
        return _candles()

    sdk = _PerSymbolSDK({deep.symbol: _throttle_then_data})
    report = _run_with_budget(sdk, [deep], empty_retry_by_symbol={deep.symbol: 1}, max_attempts=2)
    assert sdk.fetches[deep.symbol] == 2  # throttle-empty was retried, NOT counted as clean-empty
    assert report.bars_appended == 1  # data recovered on the retry — no loss


def test_budget_transient_empty_then_data_strike_not_skipped() -> None:
    """PERMUTATION 7 — transient-empty followed by data on retry → strike NOT skipped, data kept.

    Even with the most aggressive reduced budget (1), a transient on the first call is retried
    and the data on the second call is captured — the strike is never wrongly skipped.
    """
    deep = _opt(18000)
    sdk = _PerSymbolSDK(
        {deep.symbol: lambda i: GrowwAPITimeoutException("stall") if i == 0 else _candles()}
    )
    report = _run_with_budget(sdk, [deep], empty_retry_by_symbol={deep.symbol: 1}, max_attempts=3)
    assert report.bars_appended == 1  # data captured despite the reduced empty budget


def test_old_empty_settle_recoverable_via_reopen_skipped() -> None:
    """PERMUTATION 8 REWRITTEN under DEC-OLD-EMPTY-SINGLE-CONFIRM-001.

    CONSCIOUSLY SUPERSEDES the #1172 "one reduced-budget clean empty never
    settles" pin (DEC-DEAD-STRIKE-LEDGER-SETTLE-001): the operator decreed a
    SINGLE clean empty on an expired window settles SKIPPED in the same pass
    (the round-2 re-confirmation sweep cost hours). The recovery net for a
    vendor-bug 200-empty is now the MANUAL ``brutex reopen-skipped``
    (``WindowLedger.reopen_skipped``) — pinned here: reopen → the next pull
    re-fetches → data recovered + stored (keep-first sink, no permanent loss).
    """
    deep = _opt(18000)
    ledger = WindowLedger()
    sdk = _PerSymbolSDK()
    _run_with_budget(sdk, [deep], empty_retry_by_symbol={deep.symbol: 1}, ledger=ledger)
    rec = ledger.records()[0]
    assert rec.in_pull_fetches == 1  # only one fetch this pull (reduced)
    key = WindowKey(
        market=GROWW_OPTION_COVERAGE_MARKET,
        security_id=deep.symbol,
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 5),
    )
    # The single-confirm law: settled NOW (expired window, genuine clean empty).
    assert ledger.is_skippable(key)
    # MANUAL recovery: the operator re-opens the settle...
    assert ledger.reopen_skipped() == 1
    assert not ledger.is_skippable(key)
    # ...and the next pull re-fetches it — data recovered + stored.
    sdk2 = _PerSymbolSDK({deep.symbol: lambda _i: _candles()})  # now it has data
    report = _run_with_budget(sdk2, [deep], empty_retry_by_symbol={deep.symbol: 1}, ledger=ledger)
    assert sdk2.fetches[deep.symbol] == 1  # re-fetched after the reopen
    assert report.bars_appended == 1


def test_old_empty_single_confirm_settles_in_pass_one_zero_round2_calls() -> None:
    """DEC-OLD-EMPTY-SINGLE-CONFIRM-001 — the until-clean ZERO-extra-calls proof.

    CONSCIOUSLY SUPERSEDES the #1172 cumulative-cross-pull pin
    (DEC-DEAD-STRIKE-LEDGER-SETTLE-001): the 2026-07-03 production run spent
    hours in a round-2 sweep re-confirming every deep-wing empty because an OLD
    window needed 2 confirmations. Now ONE clean-empty fetch on an expired
    (old) window settles it SKIPPED in the SAME pass — pass 2 makes ZERO
    vendor calls for it (no round-2 re-fetch).
    """
    from brutex.data.window_ledger import SKIPPED

    deep = _opt(18000)
    key = WindowKey(
        market=GROWW_OPTION_COVERAGE_MARKET,
        security_id=deep.symbol,
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 5),
    )
    ledger = WindowLedger()
    # Pass 1 — ONE clean empty (reduced budget = 1 fetch) settles SKIPPED NOW.
    sdk1 = _PerSymbolSDK()
    _run_with_budget(sdk1, [deep], empty_retry_by_symbol={deep.symbol: 1}, ledger=ledger)
    assert sdk1.fetches[deep.symbol] == 1
    rec = ledger.records()[0]
    assert rec.status == SKIPPED  # settled off the SINGLE clean empty, this pass
    assert rec.last_error == "vendor confirmed no trades (1 clean attempt, expired window)"
    assert ledger.is_skippable(key)
    # Pass 2 (the until-clean round-2 sweep) — ZERO vendor calls for it.
    sdk2 = _PerSymbolSDK()
    _run_with_budget(sdk2, [deep], empty_retry_by_symbol={deep.symbol: 1}, ledger=ledger)
    assert deep.symbol not in sdk2.fetches, "no round-2 re-confirmation fetch may fire"


def test_old_empty_transient_never_settles_and_data_wins() -> None:
    """DATA-SAFETY law under DEC-OLD-EMPTY-SINGLE-CONFIRM-001 — "if uncertain, fetch".

    A transient-only pull (429 storm, no clean answer) settles NOTHING even on
    an OLD window — the window stays re-fetchable, and when data finally
    arrives it is stored (a 429 is never no-data evidence; only a genuine,
    unsuppressed clean empty settles).
    """
    deep = _opt(18000)
    key = WindowKey(
        market=GROWW_OPTION_COVERAGE_MARKET,
        security_id=deep.symbol,
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 5),
    )
    ledger = WindowLedger()
    # Pull 1 — an all-429 transient storm: NOT clean-empty evidence, never settles.
    sdk1 = _PerSymbolSDK({deep.symbol: lambda _i: GrowwAPIRateLimitException("429")})
    _run_with_budget(
        sdk1, [deep], empty_retry_by_symbol={deep.symbol: 1}, max_attempts=1, ledger=ledger
    )
    assert not ledger.is_skippable(key)  # still re-fetchable — a 429 is not evidence
    # Pull 2 — DATA arrives: stored, never masked.
    sdk2 = _PerSymbolSDK({deep.symbol: lambda _i: _candles()})
    report = _run_with_budget(sdk2, [deep], empty_retry_by_symbol={deep.symbol: 1}, ledger=ledger)
    assert report.bars_appended == 1
    assert ledger.records()[0].status == "SUCCESS"
    assert ledger.records()[0].empty_streak == 0


def test_budget_none_map_byte_identical_full_budget() -> None:
    """A ``None`` budget map keeps EVERY strike at the full empty-retry — byte-identical default."""
    deep = _opt(18000)
    sdk = _PerSymbolSDK()
    _run_with_budget(sdk, [deep], empty_retry_by_symbol=None)
    assert sdk.fetches[deep.symbol] == 3  # full default budget — unwired == pre-feature


def test_budget_all_empty_detector_still_fires_with_reduced_budget() -> None:
    """RULE 6 — the #919 whole-underlying all-empty detector stays ACTIVE under reduced budgets.

    A whole chain that is reduced-budget AND all-empty must STILL flag
    ``pull.underlying_all_empty`` (never silently accepted) — the detector is unconditional
    and the reduced budget does not suppress it.
    """
    chain = [_opt(18000 + i * 50) for i in range(4)]  # >= _ALL_EMPTY_MIN_CONTRACTS, all empty
    budget_map = {c.symbol: 1 for c in chain}
    sdk = _PerSymbolSDK()  # everything clean-empty
    with capture_logs() as logs:
        _run_with_budget(sdk, chain, empty_retry_by_symbol=budget_map)
    assert any(r["event"] == UNDERLYING_ALL_EMPTY_EVENT for r in logs)
    # each was fetched exactly once (reduced), but the all-empty flag still fired.
    assert all(sdk.fetches[c.symbol] == 1 for c in chain)


# --- DEC-FNO-LEDGER-LAKE-RECONCILE-001: F&O cold-start ledger↔lake reconcile ----------

#: A fixed IST clock well past the 15:30 close so ``_clamped_window_key`` is deterministic
#: (independent of when the suite runs). Both expiries below are BEFORE this date, so an
#: intraday window's ``clamp_window_end`` never extends past the resolved ``end``.
_FNO_NOW = datetime(2026, 1, 1, 18, 0, tzinfo=UTC)
_FNO_START = date(2024, 12, 3)
_FNO_END = date(2025, 1, 2)


def _fno_span(
    contract: DerivativeContract,
    *,
    interval: str = "1minute",
    bars: int = 100,
    first: str | None = "2024-12-01",
    last: str | None = "2025-01-05",
) -> dict[str, object]:
    """A ``summarize_spot_datasets``-shaped lake span for a contract's F&O partition."""
    return {
        "exchange": contract_exchange(contract),
        "segment": "FNO",
        "symbol": contract.symbol,
        "interval": interval,
        "bars": bars,
        "first_date": first,
        "last_date": last,
    }


def _reconcile_ce(
    ledger: WindowLedger,
    spans: Sequence[dict[str, object]],
    *,
    contract: DerivativeContract | None = None,
    market: str = GROWW_OPTION_COVERAGE_MARKET,
    contract_windows: Mapping[str, tuple[date, date]] | None = None,
) -> int:
    """Run the F&O reconcile over one contract with the deterministic clock/window."""
    c = contract if contract is not None else _ce()
    return reconcile_fno_ledger_windows(
        ledger,
        list(spans),
        contracts=[c],
        intervals=["1minute"],
        start=_FNO_START,
        end=_FNO_END,
        market=market,
        contract_windows=contract_windows,
        now=_FNO_NOW,
        now_ist=lambda: _FNO_NOW,
    )


def _fetched_key(
    contract: DerivativeContract, *, market: str = GROWW_OPTION_COVERAGE_MARKET
) -> WindowKey:
    """The exact key the fetch/plan records — the reconcile seed MUST byte-match this."""
    key = _oi._clamped_window_key(
        contract.symbol,
        interval="1minute",
        start=_FNO_START,
        end=_FNO_END,
        market=market,
        now_for_clamp=_FNO_NOW,
    )
    assert key is not None
    return key


class TestReconcileFnoLedgerWindows:
    """DEC-FNO-LEDGER-LAKE-RECONCILE-001: seed SUCCESS for lake-enclosed absent F&O windows."""

    def test_seeds_an_enclosed_window_making_it_skippable(self) -> None:
        # A lake with F&O bars + an EMPTY ledger → the reconcile seeds the window SUCCESS,
        # so the upcoming is_skippable flip skips it (fails-before: without this, the window
        # is only PLANNED, which is NOT skippable → re-fetched).
        ledger = WindowLedger()
        contract = _ce()
        seeded = _reconcile_ce(ledger, [_fno_span(contract)])
        assert seeded == 1
        rec = ledger.records()[0]
        assert rec.status == "SUCCESS"
        assert rec.bars == 100
        # The seeded key is BYTE-IDENTICAL to the fetch/plan key → is_skippable is True.
        assert ledger.is_skippable(_fetched_key(contract))

    def test_seeded_key_equals_planned_key(self) -> None:
        # The reconcile seed and plan_contract_windows produce the SAME key (no ghost).
        ledger = WindowLedger()
        contract = _ce()
        _reconcile_ce(ledger, [_fno_span(contract)])
        # A subsequent plan_contract_windows must find the row already settled (untouched).
        plan_contract_windows(
            ledger,
            contracts=[contract],
            intervals=["1minute"],
            start=_FNO_START,
            end=_FNO_END,
            market=GROWW_OPTION_COVERAGE_MARKET,
            now_ist=lambda: _FNO_NOW,
        )
        # Still exactly ONE row, still SUCCESS (the plan did NOT overwrite it to PLANNED).
        assert ledger.count == 1
        assert ledger.records()[0].status == "SUCCESS"

    def test_genuinely_missing_window_is_not_seeded_and_stays_pullable(self) -> None:
        # No span for this contract → nothing seeded → the window has NO row → not skippable.
        ledger = WindowLedger()
        contract = _ce()
        seeded = _reconcile_ce(ledger, [])  # empty lake
        assert seeded == 0
        assert ledger.count == 0
        assert not ledger.is_skippable(_fetched_key(contract))

    def test_partially_covered_window_stays_re_pullable(self) -> None:
        # The span does NOT fully enclose the window (last_date before window end) → NOT
        # seeded → the window stays re-pullable (never falsely marked done).
        ledger = WindowLedger()
        contract = _ce()
        seeded = _reconcile_ce(ledger, [_fno_span(contract, last="2024-12-20")])
        assert seeded == 0
        assert ledger.count == 0
        assert not ledger.is_skippable(_fetched_key(contract))

    def test_partial_left_edge_stays_re_pullable(self) -> None:
        # The span starts AFTER the window start (first_date > window start) → not enclosed.
        ledger = WindowLedger()
        contract = _ce()
        seeded = _reconcile_ce(ledger, [_fno_span(contract, first="2024-12-15")])
        assert seeded == 0
        assert ledger.count == 0

    def test_zero_bar_and_null_and_missing_spans_seed_nothing(self) -> None:
        contract = _ce()
        assert _reconcile_ce(WindowLedger(), [_fno_span(contract, bars=0)]) == 0
        assert _reconcile_ce(WindowLedger(), [_fno_span(contract, first=None, last=None)]) == 0
        assert _reconcile_ce(WindowLedger(), []) == 0

    def test_never_overwrites_an_existing_failed_row(self) -> None:
        ledger = WindowLedger()
        contract = _ce()
        key = _fetched_key(contract)
        ledger.record(
            key, raw_bars=0, error_type="GrowwTimeoutError", bars_appended=0, now=_FNO_NOW
        )
        seeded = _reconcile_ce(ledger, [_fno_span(contract)])
        assert seeded == 0  # the FAILED row is left for a re-pull, never masked
        assert ledger.records()[0].status == "FAILED"

    def test_never_reseeds_an_already_success_row(self) -> None:
        ledger = WindowLedger()
        contract = _ce()
        # First reconcile seeds it SUCCESS.
        assert _reconcile_ce(ledger, [_fno_span(contract)]) == 1
        # A second reconcile is a no-op (only-if-absent) — never double-counts.
        assert _reconcile_ce(ledger, [_fno_span(contract)]) == 0
        assert ledger.count == 1

    def test_futures_market_namespace_is_honoured(self) -> None:
        # A FUT contract seeds under the FUTURES ledger namespace (never the option one).
        ledger = WindowLedger()
        fut = _fut()
        seeded = _reconcile_ce(
            ledger, [_fno_span(fut)], contract=fut, market=GROWW_FUTURE_COVERAGE_MARKET
        )
        assert seeded == 1
        assert ledger.is_skippable(_fetched_key(fut, market=GROWW_FUTURE_COVERAGE_MARKET))
        # It is NOT skippable under the option namespace (distinct buckets).
        assert not ledger.is_skippable(_fetched_key(fut, market=GROWW_OPTION_COVERAGE_MARKET))

    def test_empty_clamped_range_yields_no_candidate(self) -> None:
        # A start >= end range clamps to None → no candidate → nothing seeded (no crash).
        ledger = WindowLedger()
        contract = _ce()
        seeded = reconcile_fno_ledger_windows(
            ledger,
            [_fno_span(contract)],
            contracts=[contract],
            intervals=["1minute"],
            start=_FNO_END,  # start == end → empty clamped range
            end=_FNO_END,
            market=GROWW_OPTION_COVERAGE_MARKET,
            now=_FNO_NOW,
            now_ist=lambda: _FNO_NOW,
        )
        assert seeded == 0
        assert ledger.count == 0

    def test_contract_windows_bounds_the_seed_key(self) -> None:
        # A per-contract bounded window (contract_windows) → the seed key uses the bounded
        # range (byte-matching the fetch), so a span enclosing the BOUNDED window seeds it.
        ledger = WindowLedger()
        contract = _ce()
        bounded = (date(2024, 12, 10), date(2024, 12, 20))
        seeded = _reconcile_ce(
            ledger,
            [_fno_span(contract, first="2024-12-05", last="2024-12-25")],
            contract_windows={contract.symbol: bounded},
        )
        assert seeded == 1
        # The seeded key is the BOUNDED window, not the global one.
        bounded_key = WindowKey(
            market=GROWW_OPTION_COVERAGE_MARKET,
            security_id=contract.symbol,
            interval="1minute",
            start=bounded[0],
            end=bounded[1],
        )
        assert ledger.is_skippable(bounded_key)

    def test_defaults_now_and_clock_when_absent(self) -> None:
        # now=None / now_ist=None exercise the default-clock branches (a real seed still
        # lands SUCCESS; we only assert the count, not the exact wall-clock stamp).
        ledger = WindowLedger()
        contract = _ce()
        seeded = reconcile_fno_ledger_windows(
            ledger,
            [_fno_span(contract)],
            contracts=[contract],
            intervals=["1minute"],
            start=_FNO_START,
            end=_FNO_END,
            market=GROWW_OPTION_COVERAGE_MARKET,
        )
        assert seeded == 1
        assert ledger.records()[0].status == "SUCCESS"


class TestReconcileFnoLedgerWithLake:
    """The wired entry point: load_lake_spans + the pure reconcile."""

    def test_with_lake_delegates_and_seeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.adapters.groww.option_ingest import reconcile_fno_ledger_with_lake

        contract = _ce()
        monkeypatch.setattr(
            "brutex.dashboard.coverage_reconcile.load_lake_spans",
            lambda _root: [_fno_span(contract)],
        )
        ledger = WindowLedger()
        seeded = reconcile_fno_ledger_with_lake(
            ledger,
            Path("/unused"),
            contracts=[contract],
            intervals=["1minute"],
            start=_FNO_START,
            end=_FNO_END,
            market=GROWW_OPTION_COVERAGE_MARKET,
            now=_FNO_NOW,
            now_ist=lambda: _FNO_NOW,
        )
        assert seeded == 1
        assert ledger.is_skippable(_fetched_key(contract))


# --- DEC-WINDOW-LEDGER-RESCUE-001: default-window rescue + live-edge + visible skips --


class TestFnoDefaultWindowRescue:
    """The enclosure off-by-one fix — the F&O rescue actually rescues.

    A DEFAULT-window contract's ledger key ends at ``expiry + 1 day``
    (fetch-EXCLUSIVE — ``contract_bounds.default_fno_minute_window``) while the lake
    span's ``last_date`` is bar-INCLUSIVE (the last stored bar is ON expiry), so the
    old ``end <= last_date`` enclosure was ``expiry + 1 <= expiry`` — ALWAYS false:
    the rescue seeded 0 windows deterministically even over a maximally dense lake,
    and a lost ledger re-downloaded the whole chain (~1,100 wasted calls).
    """

    def test_dense_lake_seeds_all_expired_default_window_contracts(self) -> None:
        # REGRESSION (a) — FAILS-BEFORE: was deterministically 0 seeded.
        contracts = [
            _ce("NSE-NIFTY-02Jan25-28500-CE"),
            _ce("NSE-NIFTY-02Jan25-28600-PE"),
            _fut(),
        ]
        full_start, full_end = date(2020, 1, 1), date(2025, 6, 1)
        windows: dict[str, tuple[date, date]] = {}
        # A MAXIMALLY dense lake: bars from each window's first day THROUGH expiry —
        # the last stored bar is ON expiry (bar-inclusive last_date == expiry).
        spans: list[dict[str, object]] = []
        for contract in contracts:
            expiry = contract.expiry
            assert expiry is not None
            window = default_fno_minute_window(
                expiry,
                full_start=full_start,
                full_end=full_end,
                is_option=contract.contract_type in {"CE", "PE"},
            )
            assert window is not None
            assert window[1] == expiry + timedelta(days=1)  # fetch-exclusive end
            windows[contract.symbol] = window
            spans.append(_fno_span(contract, first=window[0].isoformat(), last=expiry.isoformat()))
        ledger = WindowLedger()
        seeded = reconcile_fno_ledger_windows(
            ledger,
            spans,
            contracts=contracts,
            intervals=["1minute"],
            start=full_start,
            end=full_end,
            market=GROWW_OPTION_COVERAGE_MARKET,
            contract_windows=windows,
            now=_FNO_NOW,
            now_ist=lambda: _FNO_NOW,
        )
        assert seeded == len(contracts) == 3
        for contract in contracts:
            key = WindowKey(
                market=GROWW_OPTION_COVERAGE_MARKET,
                security_id=contract.symbol,
                interval="1minute",
                start=windows[contract.symbol][0],
                end=windows[contract.symbol][1],
            )
            assert ledger.is_skippable(key)  # the rescue makes the re-pull skip

    def test_live_edge_window_with_partial_today_bar_not_seeded(self) -> None:
        # ADVERSARIAL over-skip pin: a window whose LAST COVERED day is the clamp
        # clock's TODAY (end − 1 == today, the post-close today+1 clamp) is NEVER
        # seeded — a mid-session partial store can put a bar on today without the
        # session being complete, and a span-level check cannot tell partial from
        # complete, so the live edge is conservatively left to the fetch. The span
        # here even CLAIMS to cover the whole window — the guard still refuses.
        contract = _ce()
        clock = datetime(2025, 1, 2, 18, 0, tzinfo=UTC)  # clock: today=2025-01-02, post-close
        window = (date(2024, 12, 3), date(2025, 1, 3))  # end − 1 == today
        ledger = WindowLedger()
        seeded = reconcile_fno_ledger_windows(
            ledger,
            [_fno_span(contract, first="2024-12-03", last="2025-01-03")],
            contracts=[contract],
            intervals=["1minute"],
            start=window[0],
            end=window[1],
            market=GROWW_OPTION_COVERAGE_MARKET,
            now=clock,
            now_ist=lambda: clock,
        )
        assert seeded == 0  # the live-edge day may be partial — left to the fetch
        assert ledger.count == 0


def test_run_driver_emits_windows_skipped_settled_info() -> None:
    """DEC-WINDOW-LEDGER-RESCUE-001 — ONE aggregate INFO line per F&O driver pass."""
    sdk = _FakeSDK(payload=_candles(_OUT_SESSION))
    sink = _Sink()
    ledger = WindowLedger()
    settled = _ce()
    fetched = _ce("NSE-NIFTY-02Jan25-28600-CE")
    key = WindowKey(
        market=GROWW_OPTION_COVERAGE_MARKET,
        security_id=settled.symbol,
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
    )
    ledger.record(key, raw_bars=5, error_type=None, bars_appended=5, now=datetime.now(UTC))
    with capture_logs() as logs:
        report = run_groww_option_ingest(
            _client(sdk),
            sink,
            contracts=[settled, fetched],
            intervals=["1day"],
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
            ledger=ledger,
            now_ist=_now,
        )
    assert report.skipped_windows == 1
    events = [e for e in logs if e["event"] == "option.ingest.windows_skipped_settled"]
    assert len(events) == 1
    assert events[0]["windows_skipped_settled"] == 1
    assert events[0]["pairs"] == 2
    assert events[0]["log_level"] == "info"


class TestEmptyBreakerOptionSpine:
    """DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001 — the mass-empty breaker on the F&O spine."""

    def test_trip_halts_contract_and_leaves_window_retryable(self) -> None:
        from brutex.adapters.empty_circuit_breaker import EmptySettleBreaker, MassEmptySettleHalt

        ledger = WindowLedger()
        # Trip on the FIRST observed empty via the FRACTION rule (an all-empty fresh
        # pull is the throttle-from-start shape; the consecutive rule requires a
        # post-data empty, so it can never fire here by design).
        breaker = EmptySettleBreaker(
            min_series=1,
            consecutive_limit=0,
            fraction_limit=1.0,
            min_observations=1,
            fraction_min_series=1,
        )
        with pytest.raises(MassEmptySettleHalt):
            ingest_option_contract(
                _client(_FakeSDK(payload=_EMPTY)),
                _Sink(),
                contract=_ce(),
                interval="1day",
                start=date(2024, 1, 1),
                end=date(2024, 1, 2),
                ledger=ledger,
                empty_retry_attempts=2,
                now_ist=_now,
                empty_breaker=breaker,
            )
        assert breaker.tripped
        # The tripping window was never recorded — it stays fully retryable.
        assert ledger.count == 0
        key = WindowKey(
            market=GROWW_OPTION_COVERAGE_MARKET,
            security_id="NSE-NIFTY-02Jan25-28500-CE",
            interval="1day",
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
        )
        assert not ledger.is_skippable(key)

    def test_data_window_observes_without_trip(self) -> None:
        from brutex.adapters.empty_circuit_breaker import EmptySettleBreaker

        breaker = EmptySettleBreaker(
            min_series=1, consecutive_limit=1, fraction_limit=1.0, min_observations=999
        )
        report = ingest_option_contract(
            _client(_FakeSDK()),  # returns candles
            _Sink(),
            contract=_ce(),
            interval="1day",
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
            now_ist=_now,
            empty_breaker=breaker,
        )
        assert report.bars_appended == 1
        assert not breaker.tripped
        assert breaker.counts() == (1, 0, 0)  # observed once, zero empties

    def test_run_ingest_propagates_halt_as_global_stop(self) -> None:
        # The runner must NOT quarantine a MassEmptySettleHalt as one contract's
        # failure — it is a _GLOBAL_STOP_ERRORS member: contract 1's empty counts,
        # contract 2's empty trips, and the halt propagates out of the whole run.
        from brutex.adapters.empty_circuit_breaker import EmptySettleBreaker, MassEmptySettleHalt

        breaker = EmptySettleBreaker(
            min_series=1,
            consecutive_limit=0,
            fraction_limit=1.0,
            min_observations=2,
            fraction_min_series=1,
        )
        with pytest.raises(MassEmptySettleHalt) as excinfo:
            run_groww_option_ingest(
                _client(_FakeSDK(payload=_EMPTY)),
                _Sink(),
                contracts=[_ce("NSE-NIFTY-02Jan25-28500-CE"), _ce("NSE-NIFTY-02Jan25-28600-CE")],
                intervals=["1day"],
                start=date(2024, 1, 1),
                end=date(2024, 1, 2),
                ledger=WindowLedger(),
                now_ist=_now,
                empty_breaker=breaker,
            )
        assert excinfo.value.consecutive_empties == 2
        assert "reopen-skipped" in str(excinfo.value)

    def test_tail_suppresses_settle_when_tripped(self) -> None:
        # A queued/in-flight tail recording AFTER the trip must not settle: the
        # option tail re-checks allow_settle() and records an UNTRUSTED empty.
        from brutex.adapters.empty_circuit_breaker import EmptySettleBreaker, MassEmptySettleHalt
        from brutex.adapters.groww.option_ingest import (
            _OptionRawWindow,
            _process_option_window_tail,
        )
        from brutex.data.window_ledger import EMPTY as _EMPTY_STATUS

        breaker = EmptySettleBreaker(
            min_series=1,
            consecutive_limit=0,
            fraction_limit=1.0,
            min_observations=1,
            fraction_min_series=1,
        )
        with pytest.raises(MassEmptySettleHalt):
            breaker.observe_empty("X:1day", symbol="X", window_end=date(2024, 1, 2))
        ledger = WindowLedger()
        key = WindowKey(
            market=GROWW_OPTION_COVERAGE_MARKET,
            security_id="NSE-NIFTY-02Jan25-28500-CE",
            interval="1day",
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
        )
        raw = _OptionRawWindow(
            contract=_ce(),
            interval="1day",
            exchange="NSE",
            key=key,
            raw_bars=[],
            fetch_count=2,
            settle_empty=True,  # computed pre-trip on the producer
            old_window=True,  # even the budget-aware cumulative arm must stay suppressed
            attempt=0,
        )
        report = _process_option_window_tail(
            _Sink(),
            raw,
            ledger=ledger,
            on_success=None,
            pull_id="p",
            fields={"symbol": key.security_id, "interval": "1day"},
            empty_breaker=breaker,
        )
        assert report.bars_appended == 0
        rec = ledger.records()[0]
        assert rec.status == _EMPTY_STATUS  # NOT SKIPPED — settle suppressed
        assert rec.empty_streak == 0  # untrusted empty: streak preserved
        assert not ledger.is_skippable(key)


# ─────────────────────────────────────────────────────────────────────────
# DEC-INGEST-BOUNDARY-INTEGRITY-001 — calendar-aware boundary + all-filtered
# demotion + persisted bars_net_new (the F&O spine)
# ─────────────────────────────────────────────────────────────────────────

# 2024-01-06 (Saturday) 10:00 IST — in-window wall clock, NO session (the
# non-trading-date leak the date-blind filter wrongly stored).
_SATURDAY = "2024-01-06T10:00:00"
# 2024-01-02 09:00 IST — pre-open on a regular trading day.
_PRE_OPEN = "2024-01-02T09:00:00"
# 2024-11-01 18:30 IST — inside the Diwali muhurat session 18:00–19:00.
_MUHURAT_EVENING = "2024-11-01T18:30:00"


class _NetNewSink:
    """Recording sink whose result carries an EXPLICIT deduped ``bars_net_new``
    (``bars_appended - 1``, floored at 0) so the two counts DIFFER — proving the
    ledger stores the sink's deduped count, not a mirror of the input count."""

    def __init__(self) -> None:
        self.appends: list[int] = []

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
        del exchange, segment, symbol, interval
        self.appends.append(len(bars))
        return SinkAppendResult(
            bars_appended=len(bars),
            partitions_written=1,
            write_results=(),
            bars_net_new=max(len(bars) - 1, 0),
        )


def _option_key(interval: str = "1minute", *, end: date = date(2024, 1, 8)) -> WindowKey:
    return WindowKey(
        market=GROWW_OPTION_COVERAGE_MARKET,
        security_id="NSE-NIFTY-02Jan25-28500-CE",
        interval=interval,
        start=date(2024, 1, 1),
        end=end,
    )


def test_option_session_filter_warns_exactly_once_per_window_with_counts() -> None:
    # One window, three bars: kept / pre-open (off-session) / Saturday
    # (non-trading date). EXACTLY ONE aggregated WARNING with identity + counts.
    payload = {
        "candles": [
            [_IN_SESSION, 100.0, 110.0, 90.0, 105.0, 50, 7],
            [_PRE_OPEN, 100.0, 110.0, 90.0, 105.0, 50, 7],
            [_SATURDAY, 100.0, 110.0, 90.0, 105.0, 50, 7],
        ]
    }
    sdk = _FakeSDK(payload=payload)
    sink = _Sink()
    with capture_logs() as logs:
        report = ingest_option_contract(
            _client(sdk),
            sink,
            contract=_ce(),
            interval="1minute",
            start=date(2024, 1, 1),
            end=date(2024, 1, 8),
            now_ist=_now,
        )
    assert report.bars_appended == 1  # only the in-session Tuesday bar survives
    warns = [e for e in logs if e["event"] == "option.ingest.session_filter.dropped"]
    assert len(warns) == 1
    assert warns[0]["dropped"] == 2
    assert warns[0]["off_session"] == 1
    assert warns[0]["non_trading_date"] == 1
    assert warns[0]["window_start"] == "2024-01-01"
    assert warns[0]["window_end"] == "2024-01-08"
    assert warns[0]["symbol"] == "NSE-NIFTY-02Jan25-28500-CE"


def test_option_muhurat_evening_bar_is_now_stored() -> None:
    # SANCTIONED BEHAVIOR CHANGE (DEC-INGEST-BOUNDARY-INTEGRITY-001): an evening
    # Diwali-muhurat option bar was wrongly dropped by the date-blind filter.
    sdk = _FakeSDK(payload=_candles(_MUHURAT_EVENING))
    sink = _Sink()
    with capture_logs() as logs:
        report = ingest_option_contract(
            _client(sdk),
            sink,
            contract=_ce(),
            interval="1minute",
            start=date(2024, 11, 1),
            end=date(2024, 11, 2),
            now_ist=_now,
        )
    assert report.bars_appended == 1  # KEPT (was 0 under the old filter)
    assert not any(e["event"] == "option.ingest.session_filter.dropped" for e in logs)


def test_option_all_filtered_window_is_demoted_to_repullable() -> None:
    # GAP (c), F&O scope extension: raw bars > 0 but EVERY one session-filtered →
    # demoted FAILED + ALL_FILTERED (re-examinable). UNgated on this spine — the
    # F&O dry-run client returns EMPTY payloads (raw_bars == 0), so the flag can
    # never fire offline; #1031's observe-only stance for the liquidity-heuristic
    # detectors (interior gap / partial window) is PRESERVED.
    from brutex.data.session import ALL_FILTERED

    sdk = _FakeSDK(payload=_candles(_SATURDAY))
    ledger = WindowLedger()
    report = ingest_option_contract(
        _client(sdk),
        _Sink(),
        contract=_ce(),
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 8),
        ledger=ledger,
        now_ist=_now,
    )
    assert report.bars_appended == 0
    rec = ledger.records()[0]
    assert rec.status == "FAILED"
    assert rec.last_error == ALL_FILTERED
    assert rec.empty_streak == 1  # bounded-settle machinery engaged
    assert not ledger.is_skippable(_option_key())  # re-pulled next pass


def test_option_all_filtered_settles_skipped_after_threshold() -> None:
    # BOUNDED: after EMPTY_TERMINAL_THRESHOLD consecutive all-filtered pulls the
    # contract window settles SKIPPED so the universe pull converges (data-safe —
    # the sink is keep-first; nothing is deleted).
    from brutex.data.window_ledger import EMPTY_TERMINAL_THRESHOLD, SKIPPED

    ledger = WindowLedger()
    for _ in range(EMPTY_TERMINAL_THRESHOLD):
        sdk = _FakeSDK(payload=_candles(_SATURDAY))
        ingest_option_contract(
            _client(sdk),
            _Sink(),
            contract=_ce(),
            interval="1minute",
            start=date(2024, 1, 1),
            end=date(2024, 1, 8),
            ledger=ledger,
            now_ist=_now,
        )
    assert ledger.records()[0].status == SKIPPED


def test_option_dedup_to_zero_net_new_stays_success() -> None:
    # ORTHOGONALITY: a re-pull whose bars all dedup to ZERO net-new still passes
    # bars THROUGH the filter → SUCCESS (the discriminator reads filter
    # survivors, never the sink dedup). _Sink models full dedup (bars_net_new=0).
    from brutex.data.window_ledger import SUCCESS

    sdk = _FakeSDK(payload=_candles(_IN_SESSION))
    ledger = WindowLedger()
    ingest_option_contract(
        _client(sdk),
        _Sink(),  # bars_appended=1, bars_net_new=0 (all deduped)
        contract=_ce(),
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 8),
        ledger=ledger,
        now_ist=_now,
    )
    rec = ledger.records()[0]
    assert rec.status == SUCCESS
    assert rec.bars == 1
    assert rec.bars_net_new == 0  # GAP (d): the deduped count is persisted


def test_option_bars_net_new_threaded_from_sink_to_ledger() -> None:
    # GAP (d): the ledger stores the SINK's deduped net-new — deliberately
    # different from bars_appended so a mirror would fail.
    payload = {
        "candles": [
            ["2024-01-02T09:30:00", 100.0, 110.0, 90.0, 105.0, 50, 7],
            ["2024-01-02T09:31:00", 101.0, 111.0, 91.0, 106.0, 51, 8],
        ]
    }
    sdk = _FakeSDK(payload=payload)
    ledger = WindowLedger()
    ingest_option_contract(
        _client(sdk),
        _NetNewSink(),  # bars_appended=2, bars_net_new=1
        contract=_ce(),
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 8),
        ledger=ledger,
        now_ist=_now,
    )
    rec = ledger.records()[0]
    assert rec.bars == 2
    assert rec.bars_net_new == 1


def test_option_write_deferred_record_carries_known_zero_net_new() -> None:
    # A deferred write (lock timeout) stored nothing — a KNOWN 0, not None.
    sdk = _FakeSDK(payload=_candles(_IN_SESSION))
    ledger = WindowLedger()
    ingest_option_contract(
        _client(sdk),
        _LockTimeoutSink(),
        contract=_ce(),
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 8),
        ledger=ledger,
        now_ist=_now,
    )
    rec = ledger.records()[0]
    assert rec.last_error == "PartitionLockTimeout"
    assert rec.bars_net_new == 0


def test_option_daily_interval_is_not_session_filtered_and_never_all_filtered() -> None:
    # The daily path keeps its no-filter behavior (a daily bar has no intraday
    # time) — an out-of-session-looking DAILY bar is stored and never demoted.
    from brutex.data.window_ledger import SUCCESS

    sdk = _FakeSDK(payload=_candles(_OUT_SESSION))
    ledger = WindowLedger()
    report = ingest_option_contract(
        _client(sdk),
        _Sink(),
        contract=_ce(),
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 8),
        ledger=ledger,
        now_ist=_now,
    )
    assert report.bars_appended == 1
    rec = ledger.records()[0]
    assert rec.status == SUCCESS
    assert rec.last_error is None


# 2024-01-09 (Tuesday, a regular NSE trading day) 10:00 IST — in-session for its
# OWN date but STRICTLY AFTER the [2024-01-01, 2024-01-08] test window's clamped
# end, so the session filter KEEPS it and drop_bars_after removes it.
_FUTURE_TRADING = "2024-01-09T10:00:00"


def test_option_all_future_dropped_window_is_demoted() -> None:
    # FAILS-BEFORE / PASSES-AFTER (DEC-DATA-HONESTY-ALLFILTERED-SEED-001, the F&O
    # twin of the spot boundary-widen): a window whose every bar survives the
    # session filter but is dropped by drop_bars_after previously recorded a
    # silent-green SUCCESS with zero stored bars.
    from brutex.data.session import ALL_FILTERED

    sdk = _FakeSDK(payload=_candles(_FUTURE_TRADING))
    ledger = WindowLedger()
    report = ingest_option_contract(
        _client(sdk),
        _Sink(),
        contract=_ce(),
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 8),
        ledger=ledger,
        now_ist=_now,
    )
    assert report.bars_appended == 0
    rec = ledger.records()[0]
    assert rec.status == "FAILED"
    assert rec.last_error == ALL_FILTERED
    assert rec.empty_streak == 1


def test_option_all_filtered_warn_carries_window_bounds() -> None:
    # The dedicated grep-able WARNING fires on the F&O spine too, carrying the
    # window bounds (spot-parity triage fields — refuter F-8 fold) + the
    # contract coordinates; the per-cause session_filter warn ALSO fires for a
    # session-filtered shape (two distinguishable events, one signal each).
    sdk = _FakeSDK(payload=_candles(_SATURDAY))
    with capture_logs() as logs:
        ingest_option_contract(
            _client(sdk),
            _Sink(),
            contract=_ce(),
            interval="1minute",
            start=date(2024, 1, 1),
            end=date(2024, 1, 8),
            now_ist=_now,
        )
    warns = [e for e in logs if e["event"] == "ingest.window.all_bars_filtered"]
    assert len(warns) == 1
    assert warns[0]["log_level"] == "warning"
    assert warns[0]["raw_bars"] == 1
    assert warns[0]["kept_bars"] == 0
    assert warns[0]["window_start"] == "2024-01-01"
    assert warns[0]["window_end"] == "2024-01-08"
    assert warns[0]["symbol"] == "NSE-NIFTY-02Jan25-28500-CE"
    assert warns[0]["interval"] == "1minute"


def test_option_kept_bars_emit_no_all_filtered_warn() -> None:
    # A surviving bar keeps the dedicated event silent.
    sdk = _FakeSDK(payload=_candles(_IN_SESSION))
    with capture_logs() as logs:
        ingest_option_contract(
            _client(sdk),
            _Sink(),
            contract=_ce(),
            interval="1minute",
            start=date(2024, 1, 1),
            end=date(2024, 1, 8),
            now_ist=_now,
        )
    assert not any(e["event"] == "ingest.window.all_bars_filtered" for e in logs)


# ── make_breaker_canary_probe (DEC-EMPTY-BREAKER-CANARY-001) ─────────────────


class _CanarySDK:
    """A recording SDK fake for the canary probe (captures full call kwargs)."""

    def __init__(self, payload: Mapping[str, object] | Exception) -> None:
        self._payload = payload
        self.calls: list[dict[str, object]] = []

    def get_historical_candles(self, **kw: object) -> Mapping[str, object]:
        self.calls.append(dict(kw))
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload

    def get_expiries(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        raise AssertionError("unused")

    def get_contracts(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        raise AssertionError("unused")


class TestBreakerCanaryProbe:
    """The one-call NIFTY-spot vendor-health canary the F&O phase wires."""

    def test_data_bearing_canary_is_healthy_and_paced(self) -> None:
        """Bars returned ⇒ True; the shared pace gate ran BEFORE the fetch
        (one governed vendor call — never a rate-cap bypass)."""
        sdk = _CanarySDK(_candles())
        paced: list[int] = []
        probe = make_breaker_canary_probe(
            GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=1),
            pace=lambda: paced.append(1),
        )
        with capture_logs() as logs:
            assert probe() is True
        assert paced == [1]
        # ONE canary fetch, addressed at the NIFTY spot index (CASH segment).
        assert len(sdk.calls) == 1
        assert sdk.calls[0]["groww_symbol"] == "NSE-NIFTY"
        assert sdk.calls[0]["segment"] == "CASH"
        (event,) = [e for e in logs if e["event"] == "empty_breaker.canary_probe"]
        assert event["healthy"] is True
        assert event["bars"] == 1

    def test_clean_empty_canary_is_unhealthy(self) -> None:
        """A clean-but-EMPTY canary answer is the vendor-unhealthy verdict."""
        sdk = _CanarySDK({"candles": []})
        probe = make_breaker_canary_probe(
            GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=1)
        )
        with capture_logs() as logs:
            assert probe() is False
        (event,) = [e for e in logs if e["event"] == "empty_breaker.canary_probe"]
        assert event["healthy"] is False

    def test_raising_canary_is_unhealthy_never_raises(self) -> None:
        """A transport error on the canary is False + logged — never a raise."""
        sdk = _CanarySDK(GrowwTimeoutError("canary timeout"))
        probe = make_breaker_canary_probe(
            GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=1)
        )
        with capture_logs() as logs:
            assert probe() is False
        (event,) = [e for e in logs if e["event"] == "empty_breaker.canary_probe"]
        assert event["healthy"] is False
        assert event["error_type"] == "GrowwTimeoutError"

    def test_canary_window_is_bounded_and_recent(self) -> None:
        """The canary spans exactly the 7-day lookback ending 'today' — ONE
        vendor chunk at 1-minute (30-day tier), never a wide range."""
        sdk = _CanarySDK(_candles())
        now = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)
        probe = make_breaker_canary_probe(
            GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=1),
            now_utc=lambda: now,
        )
        assert probe() is True
        assert len(sdk.calls) == 1  # one 7-day range = one 30-day-tier chunk
        call = sdk.calls[0]
        assert str(call["start_time"]).startswith("2026-06-26")
        assert str(call["end_time"]).startswith("2026-07-0")

    def test_run_ingest_threads_probe_into_resolved_breaker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``run_groww_option_ingest(breaker_health_probe=…)`` reaches
        ``resolve_empty_breaker(health_probe=…)`` (the arming pin — a dropped
        kwarg would ship the canary DORMANT)."""
        captured: list[object] = []
        real_resolve = _oi.resolve_empty_breaker

        def _spy_resolve(**kwargs: object) -> object:
            captured.append(kwargs.get("health_probe"))
            return real_resolve(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(_oi, "resolve_empty_breaker", _spy_resolve)

        def _probe() -> bool:  # pragma: no cover — identity only
            return True

        sdk = _FakeSDK(payload=_candles())
        run_groww_option_ingest(
            _client(sdk),
            _Sink(),
            contracts=[_ce()],
            intervals=("1day",),
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
            breaker_health_probe=_probe,
        )
        assert captured == [_probe]
