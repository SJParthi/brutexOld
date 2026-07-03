"""End-to-end fake-vendor-down / fake-dead-broker pulls (DEC-VENDOR-DOWN-BREAKER-001).

Drives REAL pulls through BOTH ingest spines (``run_groww_spot_ingest`` and
``run_groww_option_ingest`` — no monkeypatched spine internals) against fake
vendors, with the error-storm breaker armed at PRODUCTION default thresholds and
its alert hooks wired through the REAL ``SweepAlerter`` methods into a recording
fake notifier:

* FAKE VENDOR DOWN — every fetch raises a retryable transport error across ≥ 3
  distinct symbols with ZERO successes → exactly ONE graceful
  :class:`VendorDownError` halt + exactly ONE CRITICAL ``vendor_down`` Telegram
  alert; and a MIXED success/failure pull does NOT halt (zero alerts).
* FAKE DEAD BROKER — every fetch raises
  :class:`GrowwTokenStillStaleError` with zero adopted-token successes → the
  streak escalates (K consecutive over ≥ T minutes on an INJECTED clock) to
  exactly ONE :class:`GrowwCredentialDeadError` halt + ONE CRITICAL
  ``credential_dead`` alert; an intervening adopted-token success CLEARS the
  streak (no halt, no alert).

Fail-before/pass-after: with the spine ``observe_window_failure`` wiring stubbed
out (verified during development by neutering the two spine call sites), the two
halt tests FAIL with ``DID NOT RAISE`` — the wiring, not the breaker alone, is
what these tests pin.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from brutex.adapters.error_storm_breaker import ErrorStormBreaker, VendorDownError
from brutex.adapters.groww import ingest as ingest_mod
from brutex.adapters.groww.errors import (
    GrowwCredentialDeadError,
    GrowwTimeoutError,
    GrowwTokenStillStaleError,
)
from brutex.adapters.groww.option_ingest import run_groww_option_ingest
from brutex.adapters.groww.run_ingest import run_groww_spot_ingest
from brutex.adapters.protocol import DerivativeContract
from brutex.data.models import CandleBar
from brutex.data.sink import SinkAppendResult
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.notifications.sweep_alerts import SweepAlerter, escape_markdown_v2
from brutex.notifications.telegram import TelegramEvent, TelegramEventKind, TelegramSeverity
from brutex.orchestrator.parallel import SerialRunParallel

# ────────────────────────────────────────────────────────────────────────
# Fakes (mirror tests/unit/test_groww_ingest.py + test_groww_option_ingest.py)
# ────────────────────────────────────────────────────────────────────────

# 04:05 UTC = 09:35 IST → in the [09:15, 15:30) session.
_IN_SESSION_TS = datetime(2021, 1, 4, 4, 5, tzinfo=UTC)


def _bar() -> CandleBar:
    return CandleBar(
        timestamp=_IN_SESSION_TS,
        open=Decimal("100.00"),
        high=Decimal("110.00"),
        low=Decimal("90.00"),
        close=Decimal("105.00"),
        volume=1,
        open_interest=None,
    )


def _inst(symbol: str) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=13,
        symbol_name=symbol,
        display_name=symbol,
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


class _Sink:
    """Recording sink — appends the per-call bar count, returns a clean result."""

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
        del exchange, segment, symbol, interval, merge_mode
        self.appends.append(len(bars))
        return SinkAppendResult(bars_appended=len(bars), partitions_written=1, write_results=())


class _VendorDownAdapter:
    """Fake spot MarketAdapter — EVERY fetch fails with a retryable transport error."""

    def __init__(self) -> None:
        self.calls = 0

    def fetch_ohlcv(
        self, *, symbol: str, exchange: object, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        del symbol, exchange, interval, start, end
        self.calls += 1
        raise GrowwTimeoutError("read timeout — vendor down")


class _MixedAdapter:
    """Fake spot MarketAdapter — one healthy symbol, every other symbol failing."""

    def __init__(self, healthy: str) -> None:
        self._healthy = healthy

    def fetch_ohlcv(
        self, *, symbol: str, exchange: object, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        del exchange, interval, start, end
        if symbol == self._healthy:
            return [_bar()]
        raise GrowwTimeoutError("read timeout")


_OPTION_PAYLOAD: Mapping[str, object] = {
    "candles": [["2024-01-02T09:30:00", 100.0, 110.0, 90.0, 105.0, 50, 7]]
}


class _StillStaleClient:
    """Fake Groww client — every candle read raises the read-only broker stall.

    *adopt_for* names contract symbols whose fetch SUCCEEDS (the Lambda minted and
    the re-read adopted a fresh token mid-pull) — the streak-clearing case.
    """

    def __init__(self, *, adopt_for: frozenset[str] = frozenset()) -> None:
        self._adopt_for = adopt_for
        self.calls: list[str] = []

    def historical_candles(self, **kw: object) -> Mapping[str, object]:
        symbol = str(kw["groww_symbol"])
        self.calls.append(symbol)
        if symbol in self._adopt_for:
            return _OPTION_PAYLOAD
        raise GrowwTokenStillStaleError("re-read returned the SAME dead token")


def _ce(strike: int) -> DerivativeContract:
    return DerivativeContract(
        underlying_symbol="NIFTY",
        expiry=date(2025, 1, 2),
        symbol=f"NSE-NIFTY-02Jan25-{strike}-CE",
        contract_type="CE",
        strike=float(strike),
    )


def _now() -> datetime:
    """Frozen IST clock — every test range is safely in the past."""
    return datetime(2026, 6, 13, 8, 0, tzinfo=UTC)


class _Clock:
    """Injected monotonic clock — advances *step* seconds per read."""

    def __init__(self, step: float) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


class _FakeNotifier:
    """Captures every published TelegramEvent (the sweep_alerts test double)."""

    def __init__(self) -> None:
        self.published: list[TelegramEvent] = []

    def publish(self, event: TelegramEvent) -> None:
        self.published.append(event)


def _armed_breaker(notifier: _FakeNotifier, *, now_step: float | None = None) -> ErrorStormBreaker:
    """A breaker at PRODUCTION default thresholds, alerting via the REAL SweepAlerter.

    The alert hooks call the real :meth:`SweepAlerter.vendor_down` /
    :meth:`SweepAlerter.credential_dead` into the recording *notifier*, so the e2e
    asserts the REAL CRITICAL Telegram event — not a stub.
    """
    alerter = SweepAlerter(notifier=notifier)

    def _vendor(halt: VendorDownError) -> None:
        alerter.vendor_down(
            failed_windows=halt.failed_windows, distinct_symbols=halt.distinct_symbols
        )

    def _cred(halt: GrowwCredentialDeadError) -> None:
        alerter.credential_dead(reason=type(halt).__name__)

    kwargs: dict[str, object] = {}
    if now_step is not None:
        kwargs["now"] = _Clock(step=now_step)
    return ErrorStormBreaker(
        alert_vendor_down=_vendor,
        alert_credential_dead=_cred,
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.fixture
def _no_retry_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neuter the spot spine's real between-pass backoff sleeps (test speed only)."""
    monkeypatch.setattr(ingest_mod, "_default_retry_backoff", lambda _i: None)


# ────────────────────────────────────────────────────────────────────────
# FAKE VENDOR DOWN — spot spine
# ────────────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("_no_retry_backoff")
class TestSpotVendorDownE2E:
    def test_sustained_storm_halts_once_with_one_critical_alert(self) -> None:
        """3 symbols × every window failing, zero successes → ONE graceful halt + ONE alert.

        PRODUCTION thresholds (25 windows / 3 distinct symbols): each symbol's
        1-day 2020→2023 range tiles to 7 windows × 3 passes = 21 ladder-exhausted
        failures, so the storm crosses BOTH floors on the third symbol — the halt
        must propagate out of ``run_groww_spot_ingest`` as the global stop (never
        a per-symbol quarantine), exactly once.
        """
        notifier = _FakeNotifier()
        breaker = _armed_breaker(notifier)
        adapter = _VendorDownAdapter()
        with pytest.raises(VendorDownError) as exc_info:
            run_groww_spot_ingest(
                adapter,  # type: ignore[arg-type]
                _Sink(),  # type: ignore[arg-type]
                instruments=[_inst("NIFTY"), _inst("BANKNIFTY"), _inst("SENSEX")],
                intervals=["1day"],
                start=date(2020, 1, 1),
                end=date(2023, 1, 1),
                run_parallel=SerialRunParallel(),
                storm_breaker=breaker,
            )
        assert exc_info.value.distinct_symbols == 3
        assert exc_info.value.failed_windows >= 25
        assert breaker.trip_rule == "vendor_down"
        # Exactly ONE CRITICAL vendor-down Telegram alert, with the WAIT wording.
        assert len(notifier.published) == 1
        ev = notifier.published[0]
        assert ev.kind == TelegramEventKind.PHASE_BLOCKED
        assert ev.severity == TelegramSeverity.CRITICAL
        assert ev.dedupe_key == "vendor-down"
        assert escape_markdown_v2("VENDOR DOWN") in ev.message
        assert escape_markdown_v2("WAIT") in ev.message

    def test_mixed_success_failure_pull_never_halts(self) -> None:
        """One healthy symbol between two failing ones → NO halt, NO alert.

        The healthy symbol's clean windows reset the storm between the failing
        symbols' runs, and each failing symbol alone can never satisfy the
        distinct-symbol floor — the pull completes with an honest transient
        residual (re-pulled next round), never a vendor-down verdict.
        """
        notifier = _FakeNotifier()
        breaker = _armed_breaker(notifier)
        sink = _Sink()
        report = run_groww_spot_ingest(
            _MixedAdapter(healthy="BANKNIFTY"),  # type: ignore[arg-type]
            sink,  # type: ignore[arg-type]
            instruments=[_inst("NIFTY"), _inst("BANKNIFTY"), _inst("SENSEX")],
            intervals=["1day"],
            start=date(2020, 1, 1),
            end=date(2023, 1, 1),
            run_parallel=SerialRunParallel(),
            storm_breaker=breaker,
        )
        assert breaker.tripped is False
        assert notifier.published == []  # zero alerts
        assert report.still_failed_windows > 0  # the honest transient residual
        assert report.failed_pairs == 0  # transients, never terminal quarantines
        assert sum(sink.appends) > 0  # the healthy symbol's bars landed


# ────────────────────────────────────────────────────────────────────────
# FAKE VENDOR DOWN — F&O spine
# ────────────────────────────────────────────────────────────────────────


class _TimeoutClient:
    """Fake Groww client — every candle read raises a retryable timeout."""

    def __init__(self) -> None:
        self.calls = 0

    def historical_candles(self, **kw: object) -> Mapping[str, object]:
        del kw
        self.calls += 1
        raise GrowwTimeoutError("read timeout — vendor down")


class TestOptionVendorDownE2E:
    def test_sustained_storm_across_contracts_halts_with_one_alert(self) -> None:
        """25+ contracts all transport-failing → ONE VendorDownError + ONE alert."""
        notifier = _FakeNotifier()
        breaker = _armed_breaker(notifier)
        contracts = [_ce(20000 + 100 * i) for i in range(30)]
        with pytest.raises(VendorDownError):
            run_groww_option_ingest(
                _TimeoutClient(),  # type: ignore[arg-type]
                _Sink(),  # type: ignore[arg-type]
                contracts=contracts,
                intervals=["1day"],
                start=date(2024, 1, 1),
                end=date(2024, 1, 10),
                max_attempts=0,
                now_ist=_now,
                storm_breaker=breaker,
            )
        assert breaker.trip_rule == "vendor_down"
        assert len(notifier.published) == 1
        assert notifier.published[0].dedupe_key == "vendor-down"


# ────────────────────────────────────────────────────────────────────────
# FAKE DEAD BROKER — F&O spine (StillStale escalation, injected clock)
# ────────────────────────────────────────────────────────────────────────


class TestDeadBrokerE2E:
    def test_still_stale_streak_escalates_to_credential_dead(self) -> None:
        """K consecutive StillStale over ≥ T minutes → ONE credential-dead halt + alert.

        PRODUCTION thresholds (K=8, T=10 min) on an INJECTED clock advancing
        120 s per read: the streak stamps its start on the 1st stall and the
        post-K time gate crosses 600 s on the 12th consecutive stall — the halt
        propagates out of ``run_groww_option_ingest`` as the global stop.
        """
        notifier = _FakeNotifier()
        breaker = _armed_breaker(notifier, now_step=120.0)
        contracts = [_ce(20000 + 100 * i) for i in range(20)]
        client = _StillStaleClient()
        with pytest.raises(GrowwCredentialDeadError) as exc_info:
            run_groww_option_ingest(
                client,  # type: ignore[arg-type]
                _Sink(),  # type: ignore[arg-type]
                contracts=contracts,
                intervals=["1day"],
                start=date(2024, 1, 1),
                end=date(2024, 1, 10),
                max_attempts=0,
                now_ist=_now,
                storm_breaker=breaker,
            )
        assert breaker.trip_rule == "credential_dead"
        assert "OPERATOR ACTION NEEDED" in str(exc_info.value)
        assert len(client.calls) == 12  # halted at the K+T boundary, not later
        # Exactly ONE CRITICAL credential-dead alert with the operator wording,
        # textually DISTINCT from the vendor-down alert.
        assert len(notifier.published) == 1
        ev = notifier.published[0]
        assert ev.kind == TelegramEventKind.PHASE_BLOCKED
        assert ev.severity == TelegramSeverity.CRITICAL
        assert ev.dedupe_key == "credential-dead:GrowwCredentialDeadError"
        assert escape_markdown_v2("OUR CREDENTIAL / LAMBDA BROKER DEAD") in ev.message
        assert escape_markdown_v2("OPERATOR ACTION NEEDED") in ev.message
        assert escape_markdown_v2("VENDOR DOWN") not in ev.message

    def test_adopted_token_success_clears_the_streak_no_halt(self) -> None:
        """An adopted-token success mid-stream resets the streak — the pull completes."""
        notifier = _FakeNotifier()
        breaker = _armed_breaker(notifier, now_step=120.0)
        contracts = [_ce(20000 + 100 * i) for i in range(15)]
        # The 8th contract's fetch SUCCEEDS (fresh token adopted): streak runs of
        # 7 then 7 — both below K=8 — so the escalation never fires.
        adopted = contracts[7].symbol
        client = _StillStaleClient(adopt_for=frozenset({adopted}))
        sink = _Sink()
        report = run_groww_option_ingest(
            client,  # type: ignore[arg-type]
            sink,  # type: ignore[arg-type]
            contracts=contracts,
            intervals=["1day"],
            start=date(2024, 1, 1),
            end=date(2024, 1, 10),
            max_attempts=0,
            now_ist=_now,
            storm_breaker=breaker,
        )
        assert breaker.tripped is False
        assert notifier.published == []  # zero alerts
        assert report.still_failed_windows == 14  # every stalled window stays re-pullable
        assert report.failed_pairs == 0  # StillStale stays retryable, never terminal
        assert sum(sink.appends) == 1  # the adopted-token contract's bars landed
