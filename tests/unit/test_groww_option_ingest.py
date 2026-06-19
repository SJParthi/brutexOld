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
from brutex.models import BruteXError, OutOfScopeError
from brutex.orchestrator.quota_tracker import DailyQuotaExceeded, QuotaInvariantError
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
        now_ist=_now,
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

    def _fake_fetch(client, *, contract, interval, start, end):
        del client, interval, start, end
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
    ],
)
def test_run_global_stop_quota_error_propagates_not_quarantined(
    monkeypatch: pytest.MonkeyPatch, quota_exc: BruteXError
) -> None:
    # DEC-GROWW-QUARANTINE-BRUTEX-001 carve-out (option spine): a quota signal is a GLOBAL
    # stop, NOT one contract's problem — every remaining contract would fail the same way —
    # so it must PROPAGATE (the caller maps it to the quota_exhausted verdict,
    # DEC-SPOT-QUOTA-MIDROUND-001), never be quarantined per contract like an OutOfScopeError.
    def _fake_fetch(client, *, contract, interval, start, end):
        del client, contract, interval, start, end
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
