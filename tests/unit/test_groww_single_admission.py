"""Regression tests — ONE rate admission per physical fetch (DEC-RATE-SINGLE-ADMISSION-001).

The bug (refuter-CONFIRMED, runtime-proven): every live fetch paid TWO rate-controller
admissions — the window-level ``pace()`` in the ingest spines
(``ingest._fetch_window_raw`` / ``option_ingest.ingest_option_contract``) AND the
client-level ``pace()`` inside ``GrowwClient._call`` (rebound by
``run_ingest.with_paced_client`` on the spot spine; baked in by
``build_live_client``/``build_live_pool`` on the F&O spine) — halving the physical pull
rate and double-charging the 500/min governor.

The fix makes the CLIENT the one canonical admission point (it paces every physical
attempt, retries included); the window-level pace stands down when the client
self-paces and remains ONLY as the fallback for an un-paceable adapter/client.

These tests drive the REAL spines over a fake SDK and count admissions per physical
fetch: exactly one on both spines, one PER retry attempt, the 500/min governor seeing
every fetch, and the un-paceable fallback still admitted per window.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal

from brutex.adapters.groww.adapter import GrowwMarketAdapter
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.constants import INSTRUMENT_CSV_COLUMNS
from brutex.adapters.groww.instruments import load_index_from_csv
from brutex.adapters.groww.option_ingest import ingest_option_contract
from brutex.adapters.groww.run_ingest import run_groww_spot_ingest
from brutex.adapters.protocol import DerivativeContract, MarketAdapter
from brutex.adapters.rate_common import RateController
from brutex.data.models import CandleBar
from brutex.data.sink import SinkAppendResult
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.orchestrator.parallel import SerialRunParallel

# ── Fixtures (the repo pattern: fake SDK behind the REAL client/adapter) ──────────

_HEADER = ",".join(INSTRUMENT_CSV_COLUMNS)


def _row(**over: str) -> str:
    cells = dict.fromkeys(INSTRUMENT_CSV_COLUMNS, "")
    cells.update(over)
    return ",".join(cells[c] for c in INSTRUMENT_CSV_COLUMNS)


_NIFTY_ROW = _row(
    exchange="NSE",
    exchange_token="NIFTY",
    trading_symbol="NIFTY",
    groww_symbol="NSE-NIFTY",
    name="NIFTY 50",
    instrument_type="IDX",
    segment="CASH",
)
_INDEX = load_index_from_csv("\n".join([_HEADER, _NIFTY_ROW]) + "\n")


class GrowwAPITimeoutException(Exception):
    """SDK-named exception fake — ``from_groww_exception`` maps it by class name
    to the retryable ``GrowwTimeoutError`` (mirrors tests/unit/test_groww_client.py)."""


class _FakeSDK:
    """``GrowwSDK``-shaped fake: counts physical calls; optional per-call error script."""

    def __init__(self, *, script: Sequence[Exception | None] = ()) -> None:
        self.calls = 0
        self._script = list(script)

    def get_historical_candles(self, **_kwargs: object) -> Mapping[str, object]:
        self.calls += 1
        if self._script:
            step = self._script.pop(0)
            if step is not None:
                raise step
        return {"candles": [["2024-01-02T09:30:00", 100.0, 110.0, 90.0, 105.0, 999, None]]}

    def get_expiries(self, **_kwargs: object) -> Mapping[str, object]:
        return {}

    def get_contracts(self, **_kwargs: object) -> Mapping[str, object]:
        return {}


class _Sink:
    """Minimal recording sink (thread-safe; the spine appends from worker threads)."""

    def __init__(self) -> None:
        self.appended = 0
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
        del exchange, segment, symbol, interval, merge_mode
        with self._lock:
            self.appended += len(bars)
        return SinkAppendResult(
            bars_appended=len(bars),
            partitions_written=1 if bars else 0,
            write_results=(),
            bars_net_new=len(bars),
        )


class _Admissions:
    """A counting stand-in for ``RateController.pace`` (thread-safe)."""

    def __init__(self) -> None:
        self.count = 0
        self._lock = threading.Lock()

    def pace(self) -> None:
        with self._lock:
            self.count += 1


def _nifty() -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=13,
        symbol_name="NIFTY",
        display_name="NIFTY 50",
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


def _ce() -> DerivativeContract:
    return DerivativeContract(
        underlying_symbol="NIFTY",
        expiry=date(2025, 1, 2),
        symbol="NSE-NIFTY-02Jan25-28500-CE",
        contract_type="CE",
        strike=28500.0,
    )


def _run_spot(adapter: MarketAdapter, pace: _Admissions) -> None:
    """Drive ONE (NIFTY, 1day) pair over one past window through the REAL spot spine."""
    run_groww_spot_ingest(
        adapter,
        _Sink(),
        instruments=[_nifty()],
        intervals=["1day"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 3),
        pace=pace.pace,
        run_parallel=SerialRunParallel(),
    )


# The self_paces / client_self_paces PREDICATE tests live in the stem-matched
# ``test_groww_client_self_paces.py`` + ``test_groww_pool_self_paces.py`` (and the
# F&O window-pace-drop test in ``test_groww_option_ingest_self_paced.py``) so the
# strict per-module coverage discovery gate scopes them to the modules they cover.


# ── SPOT spine — one admission per physical fetch ─────────────────────────────────


class TestSpotSingleAdmission:
    def test_each_physical_fetch_admitted_exactly_once(self) -> None:
        """The BLOCKER regression: pre-fix each fetch paid TWO admissions (window+client)."""
        sdk = _FakeSDK()
        adapter = GrowwMarketAdapter(
            client=GrowwClient(sdk=sdk, sleep=lambda _s: None),
            index=_INDEX,
        )
        admissions = _Admissions()
        _run_spot(adapter, admissions)
        assert sdk.calls == 1  # one window, one physical fetch
        assert admissions.count == sdk.calls  # exactly ONE admission per fetch (was 2)

    def test_retry_attempt_gets_its_own_admission(self) -> None:
        """A client fast-retry is a NEW physical fetch → one NEW admission (never zero)."""
        sdk = _FakeSDK(script=[GrowwAPITimeoutException("stall"), None])
        adapter = GrowwMarketAdapter(
            client=GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=3),
            index=_INDEX,
        )
        admissions = _Admissions()
        _run_spot(adapter, admissions)
        assert sdk.calls == 2  # attempt 1 timed out, attempt 2 succeeded
        assert admissions.count == sdk.calls  # one admission PER physical attempt (was 3)

    def test_governor_still_sees_every_fetch(self) -> None:
        """The REAL controller's 500/min governor counts exactly one issue per fetch."""
        clock_t = [1000.0]

        def _clock() -> float:
            return clock_t[0]

        def _sleep(seconds: float) -> None:
            clock_t[0] += seconds

        controller = RateController(clock=_clock, sleep=_sleep, ceiling_per_min=500)
        sdk = _FakeSDK(script=[GrowwAPITimeoutException("stall"), None])
        adapter = GrowwMarketAdapter(
            client=GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=3),
            index=_INDEX,
        )
        run_groww_spot_ingest(
            adapter,
            _Sink(),
            instruments=[_nifty()],
            intervals=["1day"],
            start=date(2024, 1, 1),
            end=date(2024, 1, 3),
            pace=controller.pace,
            run_parallel=SerialRunParallel(),
        )
        snapshot = controller.snapshot()
        assert sdk.calls == 2
        # The rolling-60s governor ledger holds EXACTLY one issue per physical fetch —
        # never the pre-fix 2× (which burned the 500/min budget at twice the true rate).
        assert snapshot["recent_minute"] == sdk.calls

    def test_unpaceable_adapter_keeps_window_admission(self) -> None:
        """The fallback: a non-Groww adapter (no paceable client) still admits per window."""

        class _FakeAdapter:
            def __init__(self) -> None:
                self.calls = 0

            def fetch_ohlcv(
                self, *, symbol: str, exchange: object, interval: str, start: date, end: date
            ) -> list[CandleBar]:
                del symbol, exchange, interval, start, end
                self.calls += 1
                return []

        adapter = _FakeAdapter()
        admissions = _Admissions()
        run_groww_spot_ingest(
            adapter,  # type: ignore[arg-type]  # a fetch-only fake (the un-paceable case)
            _Sink(),
            instruments=[_nifty()],
            intervals=["1day"],
            start=date(2024, 1, 1),
            end=date(2024, 1, 3),
            pace=admissions.pace,
            run_parallel=SerialRunParallel(),
            # empty responses re-fetch (in-pull EMPTY retry) — every one must be admitted
        )
        assert adapter.calls >= 1
        assert admissions.count == adapter.calls  # window-level pace = the sole admission


# ── F&O spine — one admission per physical fetch ─────────────────────────────────


class TestFnoSingleAdmission:
    # The paced-client window-pace-drop test lives in the stem-matched
    # ``test_groww_option_ingest_self_paced.py`` (per-module coverage gate).

    def test_retry_attempt_gets_its_own_admission(self) -> None:
        admissions = _Admissions()
        sdk = _FakeSDK(script=[GrowwAPITimeoutException("stall"), None])
        client = GrowwClient(
            sdk=sdk,
            sleep=lambda _s: None,
            max_attempts=3,
            pace=admissions.pace,
        )
        report = ingest_option_contract(
            client,
            _Sink(),
            contract=_ce(),
            interval="1day",
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
            pace=admissions.pace,
        )
        assert report.bars_appended == 1
        assert sdk.calls == 2  # the retry is a NEW physical fetch
        assert admissions.count == sdk.calls  # …with exactly ONE new admission (was 3)

    def test_unpaced_client_keeps_window_admission(self) -> None:
        """The fallback: a pace-less client (tests / window-only callers) still admits per fetch."""
        admissions = _Admissions()
        sdk = _FakeSDK()
        client = GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=1)
        report = ingest_option_contract(
            client,
            _Sink(),
            contract=_ce(),
            interval="1day",
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
            pace=admissions.pace,
        )
        assert report.bars_appended == 1
        assert sdk.calls == 1
        assert admissions.count == sdk.calls  # window-level pace = the sole admission
