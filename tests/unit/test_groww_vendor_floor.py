"""Lazy vendor-depth-floor learning (DEC-VENDOR-FLOOR-LAZY-PROBE-001).

100% line+branch over ``brutex.adapters.groww.vendor_floor``:

* the pure helpers (``vendor_floor_key`` / ``parse_vendor_floor`` /
  ``load_vendor_floors`` / ``floor_symbol_windows``);
* the ``probe_depth`` reuse bridge (``_AdapterCandleFetcher`` +
  ``make_adapter_depth_probe`` — the bisect runs over the pull's OWN adapter);
* the ledger mass-settle (``settle_pre_floor_windows`` — wholly-pre-floor rows
  only, straddling/settled/foreign rows untouched, malformed dates abstain);
* the learner (boundary-only trigger, the mass-empty-breaker gate BEFORE the
  probe AND re-checked before cementing, single-flight, inconclusive/at-boundary
  probes cement nothing, persist + settle + one ``vendor_floor.learned`` log);
* the None-safe ``observe_boundary_floor`` seam (never raises);
* the END-TO-END spot-spine regression: a confirmed clean-empty window AT the
  configured history floor triggers EXACTLY ONE probe through the REAL
  ``run_groww_spot_ingest`` spine, persists the learned floor and mass-settles a
  wholly-pre-floor ledger row ``SKIPPED("pre-floor")``. Fail-before (exact,
  Golden Rule #1): on origin/main this file COLLECTION-fails (ImportError —
  ``brutex.adapters.groww.vendor_floor`` does not exist there); the BEHAVIOURAL
  fail-before is proven ON-BRANCH by reverting the ``floor_learner`` spine
  threading, which kills the spine test;
* the probe-PACING regression (the review BLOCKER): the learner built over
  ``with_paced_client(adapter, pace)`` — the production build-site shape —
  admits EVERY physical probe fetch through the shared pace hook, while the raw
  (pool-leaf no-op-pace) adapter shape probes with ZERO admissions.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import contextlib
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast

import pytest
from brutex.adapters.empty_circuit_breaker import EmptySettleBreaker, MassEmptySettleHalt
from brutex.adapters.groww.adapter import GrowwMarketAdapter
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.constants import INSTRUMENT_CSV_COLUMNS
from brutex.adapters.groww.instruments import load_index_from_csv
from brutex.adapters.groww.probe import (
    DEPTH_PROBE_MAX_CALLS,
    VERDICT_ABSENT,
    VERDICT_OK,
    DepthProbeResult,
)
from brutex.adapters.groww.run_ingest import run_groww_spot_ingest, with_paced_client
from brutex.adapters.groww.vendor_floor import (
    PRE_FLOOR_REASON,
    VENDOR_FLOOR_META_PREFIX,
    VendorFloorLearner,
    _AdapterCandleFetcher,
    floor_symbol_windows,
    load_vendor_floors,
    make_adapter_depth_probe,
    make_vendor_floor_learner,
    observe_boundary_floor,
    parse_vendor_floor,
    settle_pre_floor_windows,
    vendor_floor_key,
)
from brutex.adapters.ingest_common import GROWW_COVERAGE_MARKET
from brutex.adapters.protocol import MarketAdapter
from brutex.data.fetch_coverage import WindowKey
from brutex.data.models import CandleBar
from brutex.data.sink import SinkAppendResult
from brutex.data.window_ledger import SKIPPED, WindowLedger, WindowLedgerRecord
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.orchestrator.parallel import SerialRunParallel
from structlog.testing import capture_logs

_FLOOR = date(2020, 1, 1)
_NOW = datetime(2026, 7, 4, 10, 0, tzinfo=UTC)
# 04:05 UTC = 09:35 IST → inside the [09:15, 15:30) session filter.
_IN_SESSION = (4, 5)


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


def _bar(day: date) -> CandleBar:
    return CandleBar(
        timestamp=datetime(day.year, day.month, day.day, *_IN_SESSION, tzinfo=UTC),
        open=Decimal("100.00"),
        high=Decimal("110.00"),
        low=Decimal("90.00"),
        close=Decimal("105.00"),
        volume=1,
        open_interest=None,
    )


def _result(
    *,
    verdict: str = VERDICT_OK,
    probed_floor: date | None = date(2021, 6, 1),
    calls_used: int = 6,
) -> DepthProbeResult:
    return DepthProbeResult(
        symbol="NIFTY",
        verdict=verdict,
        first_ts=None if probed_floor is None else f"{probed_floor.isoformat()}T09:15:00",
        probed_floor=probed_floor,
        boundary_has_data=False,
        calls_used=calls_used,
        detail="test stub",
    )


class _CountingProbe:
    """A probe stub recording every invocation, returning a fixed result."""

    def __init__(self, result: DepthProbeResult) -> None:
        self.result = result
        self.calls: list[str] = []

    def __call__(self, instrument: Instrument) -> DepthProbeResult:
        self.calls.append(instrument.symbol_name)
        return self.result


class _RecordingStore:
    """An in-memory ``schema_meta`` twin (the VendorFloorMetaStore Protocol)."""

    def __init__(self, seed: dict[str, str] | None = None) -> None:
        self.values: dict[str, str] = dict(seed or {})
        self.reads: list[str] = []

    def set_meta(self, key: str, value: str) -> None:
        self.values[key] = value

    def get_meta(self, key: str) -> str | None:
        self.reads.append(key)
        return self.values.get(key)


class _RaisingStore:
    """A store whose every access raises — the broken-sidecar degrade path."""

    def set_meta(self, key: str, value: str) -> None:
        raise RuntimeError("sidecar write broke")

    def get_meta(self, key: str) -> str | None:
        raise RuntimeError("sidecar read broke")


def _tripped_breaker() -> EmptySettleBreaker:
    """A REAL mass-empty breaker, tripped (``allow_settle() is False``)."""
    breaker = EmptySettleBreaker(
        consecutive_limit=1,
        min_series=1,
        min_observations=1,
        fraction_min_series=1,
        fraction_limit=0.5,
    )
    with contextlib.suppress(MassEmptySettleHalt):
        breaker.observe_empty("S:1minute", symbol="S", window_end=date(2020, 1, 2))
    assert breaker.allow_settle() is False
    return breaker


# ── pure helpers ─────────────────────────────────────────────────────────────────


class TestKeyAndParse:
    def test_key_prefix(self) -> None:
        assert vendor_floor_key("NIFTY") == f"{VENDOR_FLOOR_META_PREFIX}NIFTY"

    def test_parse_none(self) -> None:
        assert parse_vendor_floor(None) is None

    def test_parse_valid_with_whitespace(self) -> None:
        assert parse_vendor_floor(" 2021-06-01 ") == date(2021, 6, 1)

    def test_parse_malformed_degrades_to_none(self) -> None:
        assert parse_vendor_floor("not-a-date") is None


class TestLoadVendorFloors:
    def test_none_store_reads_nothing(self) -> None:
        assert load_vendor_floors(None, ["NIFTY"]) == {}

    def test_reads_only_parseable_floors(self) -> None:
        store = _RecordingStore(
            {
                vendor_floor_key("NIFTY"): "2021-06-01",
                vendor_floor_key("SENSEX"): "garbage",
            }
        )
        floors = load_vendor_floors(store, ["NIFTY", "SENSEX", "BANKNIFTY"])
        assert floors == {"NIFTY": date(2021, 6, 1)}  # malformed + absent omitted

    def test_broken_store_degrades_to_empty(self) -> None:
        with capture_logs() as logs:
            assert load_vendor_floors(_RaisingStore(), ["NIFTY"]) == {}
        assert any(e["event"] == "vendor_floor.load_failed" for e in logs)


class TestFloorSymbolWindows:
    def test_no_learned_floors_is_none(self) -> None:
        assert (
            floor_symbol_windows(["NIFTY"], start=_FLOOR, end=date(2026, 7, 3), floors={}) is None
        )

    def test_floor_at_or_before_start_clamps_nothing(self) -> None:
        out = floor_symbol_windows(
            ["NIFTY"], start=_FLOOR, end=date(2026, 7, 3), floors={"NIFTY": _FLOOR}
        )
        assert out is None

    def test_floor_inside_range_clamps_the_start(self) -> None:
        out = floor_symbol_windows(
            ["NIFTY", "SENSEX"],
            start=_FLOOR,
            end=date(2026, 7, 3),
            floors={"NIFTY": date(2021, 6, 1)},
        )
        assert out == {"NIFTY": (date(2021, 6, 1), date(2026, 7, 3))}

    def test_floor_at_or_past_end_degenerates_to_empty_pair(self) -> None:
        out = floor_symbol_windows(
            ["NIFTY"],
            start=_FLOOR,
            end=date(2021, 1, 1),
            floors={"NIFTY": date(2022, 1, 1)},
        )
        assert out == {
            "NIFTY": (date(2021, 1, 1), date(2021, 1, 1))
        }  # zero windows, never negative


# ── the probe_depth reuse bridge ─────────────────────────────────────────────────


class _ListingAdapter:
    """A fake adapter whose 1-minute history starts at *listing* (bars after it)."""

    def __init__(self, listing: date) -> None:
        self.listing = listing
        self.calls: list[tuple[date, date]] = []

    def fetch_ohlcv(
        self, *, symbol: str, exchange: object, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        del symbol, exchange, interval
        self.calls.append((start, end))
        if end < self.listing:
            return []
        return [_bar(max(start, self.listing))]


class TestAdapterProbeBridge:
    def test_fetcher_rewraps_bars_as_a_candles_payload(self) -> None:
        adapter = _ListingAdapter(listing=date(2021, 6, 1))
        fetcher = _AdapterCandleFetcher(
            adapter=cast(MarketAdapter, adapter), instrument=_inst("NIFTY")
        )
        payload = fetcher.historical_candles(
            exchange="IGNORED",
            segment="IGNORED",
            groww_symbol="IGNORED",
            interval="1minute",
            start="2021-06-01 09:15:00",
            end="2021-06-15 15:30:00",
        )
        candles = payload["candles"]
        assert isinstance(candles, list) and len(candles) == 1
        assert adapter.calls == [(date(2021, 6, 1), date(2021, 6, 15))]

    def test_fetcher_empty_window_is_an_empty_payload(self) -> None:
        adapter = _ListingAdapter(listing=date(2021, 6, 1))
        fetcher = _AdapterCandleFetcher(
            adapter=cast(MarketAdapter, adapter), instrument=_inst("NIFTY")
        )
        payload = fetcher.historical_candles(
            exchange="X",
            segment="X",
            groww_symbol="X",
            interval="1minute",
            start="2020-01-01 09:15:00",
            end="2020-01-15 15:30:00",
        )
        assert payload == {"candles": []}

    def test_bound_probe_bisects_to_the_listing_date(self) -> None:
        listing = date(2022, 3, 15)
        adapter = _ListingAdapter(listing=listing)
        probe = make_adapter_depth_probe(
            cast(MarketAdapter, adapter), boundary=_FLOOR, today=date(2026, 7, 3)
        )
        result = probe(_inst("NIFTY"))
        assert result.verdict == VERDICT_OK
        assert result.probed_floor == listing  # the first bar's own date is the floor
        assert result.calls_used <= DEPTH_PROBE_MAX_CALLS  # the bounded budget held
        assert len(adapter.calls) == result.calls_used


# ── the ledger mass-settle ───────────────────────────────────────────────────────


_LEARNED = date(2020, 6, 1)


def _key(symbol: str, start: date, end: date, *, market: str = GROWW_COVERAGE_MARKET) -> WindowKey:
    return WindowKey(market=market, security_id=symbol, interval="1minute", start=start, end=end)


def _seeded_ledger() -> WindowLedger:
    ledger = WindowLedger()
    # Wholly pre-floor, un-settled (PLANNED + FAILED) — the two rows to settle.
    ledger.plan(_key("NIFTY", date(2020, 1, 1), date(2020, 1, 31)), now=_NOW)
    ledger.record(
        _key("NIFTY", date(2020, 2, 1), date(2020, 2, 28)),
        raw_bars=0,
        error_type="GrowwTimeoutError",
        bars_appended=0,
        now=_NOW,
    )
    # Straddling the learned floor (end after it) — NEVER settled.
    ledger.plan(_key("NIFTY", date(2020, 5, 20), date(2020, 6, 10)), now=_NOW)
    # Already settled (SUCCESS + SKIPPED) — untouched.
    ledger.record(
        _key("NIFTY", date(2020, 3, 1), date(2020, 3, 31)),
        raw_bars=5,
        error_type=None,
        bars_appended=5,
        now=_NOW,
    )
    ledger.settle_known_gap(_key("NIFTY", date(2020, 4, 1), date(2020, 4, 30)), now=_NOW)
    # Another symbol + another market — untouched.
    ledger.plan(_key("SENSEX", date(2020, 1, 1), date(2020, 1, 31)), now=_NOW)
    ledger.plan(_key("NIFTY", date(2020, 1, 1), date(2020, 1, 31), market="other"), now=_NOW)
    return ledger


def _row(ledger: WindowLedger, symbol: str, start: str, *, market: str = GROWW_COVERAGE_MARKET):
    (row,) = [
        r
        for r in ledger.records()
        if r.security_id == symbol and r.window_start == start and r.market == market
    ]
    return row


class TestSettlePreFloorWindows:
    def test_settles_only_wholly_pre_floor_unsettled_rows(self) -> None:
        ledger = _seeded_ledger()
        settled = settle_pre_floor_windows(ledger, symbol="NIFTY", learned_floor=_LEARNED, now=_NOW)
        assert settled == 2
        for start in ("2020-01-01", "2020-02-01"):
            row = _row(ledger, "NIFTY", start)
            assert row.status == SKIPPED
            assert row.last_error == PRE_FLOOR_REASON  # reversible via reopen-skipped
        assert _row(ledger, "NIFTY", "2020-05-20").status != SKIPPED  # straddler kept
        assert _row(ledger, "NIFTY", "2020-03-01").status == "SUCCESS"
        assert _row(ledger, "SENSEX", "2020-01-01").status == "PLANNED"
        assert _row(ledger, "NIFTY", "2020-01-01", market="other").status == "PLANNED"

    def test_default_now_stamp(self) -> None:
        ledger = WindowLedger()
        ledger.plan(_key("NIFTY", date(2020, 1, 1), date(2020, 1, 31)), now=_NOW)
        assert settle_pre_floor_windows(ledger, symbol="NIFTY", learned_floor=_LEARNED) == 1

    def test_malformed_row_date_abstains(self) -> None:
        settled_keys: list[WindowKey] = []

        class _StubLedger:
            def records(self) -> list[WindowLedgerRecord]:
                return [
                    WindowLedgerRecord(
                        market=GROWW_COVERAGE_MARKET,
                        security_id="NIFTY",
                        interval="1minute",
                        window_start="not-a-date",
                        window_end="2020-01-31",
                        status="FAILED",
                        bars=0,
                        attempts=1,
                        last_error="x",
                        first_seen_at=_NOW.isoformat(),
                        last_attempt_at=None,
                    )
                ]

            def settle_known_gap(
                self, key: WindowKey, *, now: datetime, reason: str | None = None
            ) -> str:
                del now, reason
                settled_keys.append(key)
                return SKIPPED

        count = settle_pre_floor_windows(
            cast(WindowLedger, _StubLedger()), symbol="NIFTY", learned_floor=_LEARNED, now=_NOW
        )
        assert count == 0 and settled_keys == []  # never guessed at


# ── the learner ──────────────────────────────────────────────────────────────────


def _learner(
    probe: _CountingProbe,
    *,
    store: _RecordingStore | _RaisingStore | None = None,
) -> VendorFloorLearner:
    return VendorFloorLearner(probe=probe, history_floor=_FLOOR, store=store, now=lambda: _NOW)


class TestLearnerObserve:
    def test_non_boundary_window_never_probes(self) -> None:
        probe = _CountingProbe(_result())
        learner = _learner(probe)
        learner.observe_boundary_empty(
            instrument=_inst("NIFTY"),
            window_start=date(2021, 1, 1),  # NOT the configured floor
            ledger=None,
            empty_breaker=None,
        )
        assert probe.calls == []

    def test_gated_breaker_probes_nothing_and_does_not_claim_the_slot(self) -> None:
        probe = _CountingProbe(_result())
        store = _RecordingStore()
        learner = _learner(probe, store=store)
        with capture_logs() as logs:
            learner.observe_boundary_empty(
                instrument=_inst("NIFTY"),
                window_start=_FLOOR,
                ledger=None,
                empty_breaker=_tripped_breaker(),
            )
        assert probe.calls == [] and store.values == {}  # NO probe, NO settle
        assert any(e["event"] == "vendor_floor.probe_suppressed" for e in logs)
        # The slot was NOT claimed: a later trustworthy observation still probes.
        learner.observe_boundary_empty(
            instrument=_inst("NIFTY"), window_start=_FLOOR, ledger=None, empty_breaker=None
        )
        assert probe.calls == ["NIFTY"]

    def test_single_flight_one_probe_per_symbol_per_pull(self) -> None:
        probe = _CountingProbe(_result())
        learner = _learner(probe)
        for _ in range(3):
            learner.observe_boundary_empty(
                instrument=_inst("NIFTY"), window_start=_FLOOR, ledger=None, empty_breaker=None
            )
        assert probe.calls == ["NIFTY"]

    def test_inconclusive_verdict_cements_nothing(self) -> None:
        probe = _CountingProbe(_result(verdict=VERDICT_ABSENT, probed_floor=None))
        store = _RecordingStore()
        learner = _learner(probe, store=store)
        with capture_logs() as logs:
            learner.observe_boundary_empty(
                instrument=_inst("NIFTY"), window_start=_FLOOR, ledger=None, empty_breaker=None
            )
        assert store.values == {}
        assert any(e["event"] == "vendor_floor.probe_inconclusive" for e in logs)

    def test_ok_verdict_without_a_floor_is_inconclusive(self) -> None:
        probe = _CountingProbe(_result(verdict=VERDICT_OK, probed_floor=None))
        learner = _learner(probe)
        with capture_logs() as logs:
            learner.observe_boundary_empty(
                instrument=_inst("NIFTY"), window_start=_FLOOR, ledger=None, empty_breaker=None
            )
        assert any(e["event"] == "vendor_floor.probe_inconclusive" for e in logs)

    def test_probe_finding_boundary_data_cements_nothing(self) -> None:
        probe = _CountingProbe(_result(probed_floor=_FLOOR))  # a transient boundary empty
        store = _RecordingStore()
        learner = _learner(probe, store=store)
        with capture_logs() as logs:
            learner.observe_boundary_empty(
                instrument=_inst("NIFTY"), window_start=_FLOOR, ledger=None, empty_breaker=None
            )
        assert store.values == {}
        assert any(e["event"] == "vendor_floor.boundary_has_data" for e in logs)

    def test_gate_recheck_after_probe_suppresses_the_cement(self) -> None:
        """A soft-throttle beginning MID-probe poisons the probe's own empties."""
        breaker = EmptySettleBreaker(
            consecutive_limit=1,
            min_series=1,
            min_observations=1,
            fraction_min_series=1,
            fraction_limit=0.5,
        )
        store = _RecordingStore()
        ledger = _seeded_ledger()

        def _tripping_probe(instrument: Instrument) -> DepthProbeResult:
            del instrument
            with contextlib.suppress(MassEmptySettleHalt):
                # the throttle begins while the probe is in flight
                breaker.observe_empty("S:1minute", symbol="S", window_end=date(2020, 1, 2))
            return _result()

        learner = VendorFloorLearner(
            probe=_tripping_probe, history_floor=_FLOOR, store=store, now=lambda: _NOW
        )
        with capture_logs() as logs:
            learner.observe_boundary_empty(
                instrument=_inst("NIFTY"),
                window_start=_FLOOR,
                ledger=ledger,
                empty_breaker=breaker,
            )
        assert store.values == {}  # nothing persisted
        assert all(r.status != SKIPPED or r.window_start == "2020-04-01" for r in ledger.records())
        assert any(e["event"] == "vendor_floor.cement_suppressed" for e in logs)

    def test_clean_probe_persists_settles_and_logs_once(self) -> None:
        probe = _CountingProbe(_result(probed_floor=_LEARNED))
        store = _RecordingStore()
        ledger = _seeded_ledger()
        learner = _learner(probe, store=store)
        with capture_logs() as logs:
            learner.observe_boundary_empty(
                instrument=_inst("NIFTY"),
                window_start=_FLOOR,
                ledger=ledger,
                empty_breaker=EmptySettleBreaker(),  # healthy — the gate passes
            )
        assert store.values == {vendor_floor_key("NIFTY"): _LEARNED.isoformat()}
        learned = [e for e in logs if e["event"] == "vendor_floor.learned"]
        assert len(learned) == 1
        assert learned[0]["pre_floor_settled"] == 2
        assert learned[0]["persisted"] is True
        assert _row(ledger, "NIFTY", "2020-01-01").status == SKIPPED

    def test_none_ledger_persists_with_zero_settled(self) -> None:
        probe = _CountingProbe(_result(probed_floor=_LEARNED))
        store = _RecordingStore()
        learner = _learner(probe, store=store)
        with capture_logs() as logs:
            learner.observe_boundary_empty(
                instrument=_inst("NIFTY"), window_start=_FLOOR, ledger=None, empty_breaker=None
            )
        (learned,) = [e for e in logs if e["event"] == "vendor_floor.learned"]
        assert learned["pre_floor_settled"] == 0
        assert store.values == {vendor_floor_key("NIFTY"): _LEARNED.isoformat()}

    def test_none_store_learns_unpersisted(self) -> None:
        probe = _CountingProbe(_result(probed_floor=_LEARNED))
        learner = _learner(probe, store=None)
        with capture_logs() as logs:
            learner.observe_boundary_empty(
                instrument=_inst("NIFTY"), window_start=_FLOOR, ledger=None, empty_breaker=None
            )
        (learned,) = [e for e in logs if e["event"] == "vendor_floor.learned"]
        assert learned["persisted"] is False

    def test_broken_store_write_never_breaks_the_pull(self) -> None:
        probe = _CountingProbe(_result(probed_floor=_LEARNED))
        learner = _learner(probe, store=_RaisingStore())
        with capture_logs() as logs:
            learner.observe_boundary_empty(
                instrument=_inst("NIFTY"), window_start=_FLOOR, ledger=None, empty_breaker=None
            )
        assert any(e["event"] == "vendor_floor.persist_failed" for e in logs)
        assert any(e["event"] == "vendor_floor.learned" for e in logs)  # settle still ran

    def test_default_clock_stamps_utc_now(self) -> None:
        probe = _CountingProbe(_result(probed_floor=_LEARNED))
        ledger = WindowLedger()
        ledger.plan(_key("NIFTY", date(2020, 1, 1), date(2020, 1, 31)), now=_NOW)
        learner = VendorFloorLearner(probe=probe, history_floor=_FLOOR)  # default now()
        learner.observe_boundary_empty(
            instrument=_inst("NIFTY"), window_start=_FLOOR, ledger=ledger, empty_breaker=None
        )
        assert _row(ledger, "NIFTY", "2020-01-01").status == SKIPPED


class TestSettlementTrustworthy:
    def test_none_breaker_imposes_no_gate(self) -> None:
        assert VendorFloorLearner._settlement_trustworthy(None) is True

    def test_healthy_breaker_allows(self) -> None:
        assert VendorFloorLearner._settlement_trustworthy(EmptySettleBreaker()) is True

    def test_tripped_breaker_gates(self) -> None:
        assert VendorFloorLearner._settlement_trustworthy(_tripped_breaker()) is False


class TestMakeLearner:
    def test_builds_a_fresh_bound_learner(self) -> None:
        adapter = _ListingAdapter(listing=date(2022, 3, 15))
        store = _RecordingStore()
        learner = make_vendor_floor_learner(
            cast(MarketAdapter, adapter),
            history_floor=_FLOOR,
            today=date(2026, 7, 3),
            store=store,
        )
        other = make_vendor_floor_learner(
            cast(MarketAdapter, adapter), history_floor=_FLOOR, today=date(2026, 7, 3)
        )
        assert learner._probed is not other._probed  # per-pull state never shared
        learner.observe_boundary_empty(
            instrument=_inst("NIFTY"), window_start=_FLOOR, ledger=None, empty_breaker=None
        )
        assert store.values == {vendor_floor_key("NIFTY"): date(2022, 3, 15).isoformat()}


class TestObserveBoundaryFloorSeam:
    def test_none_learner_observes_nothing(self) -> None:
        observe_boundary_floor(
            None,
            instrument=_inst("NIFTY"),
            window_start=_FLOOR,
            ledger=None,
            empty_breaker=None,
        )  # must simply not raise

    def test_delegates_to_the_learner(self) -> None:
        probe = _CountingProbe(_result())
        observe_boundary_floor(
            _learner(probe),
            instrument=_inst("NIFTY"),
            window_start=_FLOOR,
            ledger=None,
            empty_breaker=None,
        )
        assert probe.calls == ["NIFTY"]

    def test_a_learner_bug_is_swallowed_and_logged(self) -> None:
        def _broken_probe(instrument: Instrument) -> DepthProbeResult:
            raise RuntimeError("probe bug")

        learner = VendorFloorLearner(probe=_broken_probe, history_floor=_FLOOR)
        with capture_logs() as logs:
            observe_boundary_floor(
                learner,
                instrument=_inst("NIFTY"),
                window_start=_FLOOR,
                ledger=None,
                empty_breaker=None,
            )
        assert any(e["event"] == "vendor_floor.observe_failed" for e in logs)


# ── the END-TO-END spot-spine regression ─────────────────────────────────────────


class _EmptyAdapter:
    """A fake spot adapter whose every window is a clean 200-zero-candles."""

    def __init__(self) -> None:
        self.calls = 0

    def fetch_ohlcv(
        self, *, symbol: str, exchange: object, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        del symbol, exchange, interval, start, end
        self.calls += 1
        return []


class _Sink:
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
        return SinkAppendResult(bars_appended=len(bars), partitions_written=1, write_results=())


class TestSpineIntegration:
    def test_boundary_empty_triggers_exactly_one_probe_persist_and_mass_settle(self) -> None:
        """THE item-8 regression: a confirmed clean-empty window AT the history floor
        runs ONE single-flight, breaker-gated depth probe through the REAL spot
        spine, persists ``groww.vendor_floor.<symbol>`` and mass-settles the
        wholly-pre-floor ledger row ``SKIPPED("pre-floor")``.

        Fail-before (exact): on origin/main this FILE collection-fails
        (ImportError — the vendor_floor module does not exist there); the
        behavioural fail-before is proven ON-BRANCH by reverting the
        ``floor_learner`` spine threading, which kills this test.
        """
        start, end = date(2020, 11, 1), date(2021, 4, 1)
        learned = date(2020, 12, 1)
        probe = _CountingProbe(
            DepthProbeResult(
                symbol="NIFTY",
                verdict=VERDICT_OK,
                first_ts=f"{learned.isoformat()}T09:15:00",
                probed_floor=learned,
                boundary_has_data=False,
                calls_used=6,
                detail="stub",
            )
        )
        store = _RecordingStore()
        ledger = WindowLedger()
        # A wholly-pre-floor leftover from a "prior pull" (FAILED, re-pullable today).
        stale = _key("NIFTY", date(2020, 9, 1), date(2020, 9, 5))
        ledger.record(stale, raw_bars=0, error_type="GrowwTimeoutError", bars_appended=0, now=_NOW)
        learner = VendorFloorLearner(
            probe=probe, history_floor=start, store=store, now=lambda: _NOW
        )
        with capture_logs() as logs:
            report = run_groww_spot_ingest(
                cast(MarketAdapter, _EmptyAdapter()),
                cast("object", _Sink()),  # type: ignore[arg-type]
                instruments=[_inst("NIFTY")],
                intervals=["1day"],
                start=start,
                end=end,
                ledger=ledger,
                run_parallel=SerialRunParallel(),
                floor_learner=learner,
            )
        assert probe.calls == ["NIFTY"]  # exactly ONE probe for the boundary window
        assert store.values == {vendor_floor_key("NIFTY"): learned.isoformat()}
        row = _row(ledger, "NIFTY", "2020-09-01")
        assert row.status == SKIPPED and row.last_error == PRE_FLOOR_REASON
        assert sum(1 for e in logs if e["event"] == "vendor_floor.learned") == 1
        assert report.failed_pairs == 0

    def test_gated_breaker_spine_pull_never_probes(self) -> None:
        """The design-hazard pin: an untrustworthy emptiness (tripped mass-empty
        breaker) probes NOTHING and settles NOTHING — through the real spine.

        A tripped breaker also keeps raising its pull-wide
        :class:`MassEmptySettleHalt` on every further empty observation (the
        breaker's own contract) — the pin here is that the learner ran BEFORE
        that halt and STILL probed/cemented nothing.
        """
        start, end = date(2020, 11, 1), date(2021, 4, 1)
        probe = _CountingProbe(_result())
        store = _RecordingStore()
        learner = VendorFloorLearner(
            probe=probe, history_floor=start, store=store, now=lambda: _NOW
        )
        with pytest.raises(MassEmptySettleHalt):
            run_groww_spot_ingest(
                cast(MarketAdapter, _EmptyAdapter()),
                cast("object", _Sink()),  # type: ignore[arg-type]
                instruments=[_inst("NIFTY")],
                intervals=["1day"],
                start=start,
                end=end,
                run_parallel=SerialRunParallel(),
                empty_breaker=_tripped_breaker(),
                floor_learner=learner,
            )
        assert probe.calls == [] and store.values == {}


# ── probe PACING — every probe fetch admits through the shared pace ─────────────
# (the review BLOCKER: the live pool leaves carry a no-op pace, and the session
# RateController is bound only via ``run_ingest.with_paced_client`` — a learner
# built over the RAW adapter probed UNPACED, bypassing the 8 r/s ceiling + the
# 500/min governor. The build sites now construct the learner over
# ``with_paced_client(adapter, pace)``; these tests pin the mechanism end-to-end
# through the REAL GrowwMarketAdapter/GrowwClient, mirroring the
# DEC-RATE-SINGLE-ADMISSION-001 canary "one governed call — never a cap bypass".)


_CSV_HEADER = ",".join(INSTRUMENT_CSV_COLUMNS)


def _csv_row(**over: str) -> str:
    cells = dict.fromkeys(INSTRUMENT_CSV_COLUMNS, "")
    cells.update(over)
    return ",".join(cells[c] for c in INSTRUMENT_CSV_COLUMNS)


_NIFTY_CSV_ROW = _csv_row(
    exchange="NSE",
    exchange_token="NIFTY",
    trading_symbol="NIFTY",
    groww_symbol="NSE-NIFTY",
    name="NIFTY 50",
    instrument_type="IDX",
    segment="CASH",
)
_NIFTY_INDEX = load_index_from_csv("\n".join([_CSV_HEADER, _NIFTY_CSV_ROW]) + "\n")


class _EmptySDK:
    """``GrowwSDK``-shaped fake: counts physical calls, every window clean-empty."""

    def __init__(self) -> None:
        self.calls = 0

    def get_historical_candles(self, **_kwargs: object) -> Mapping[str, object]:
        self.calls += 1
        return {"candles": []}

    def get_expiries(self, **_kwargs: object) -> Mapping[str, object]:
        return {}

    def get_contracts(self, **_kwargs: object) -> Mapping[str, object]:
        return {}


class _Admissions:
    """A counting stand-in for ``RateController.pace``."""

    def __init__(self) -> None:
        self.count = 0

    def pace(self) -> None:
        self.count += 1


def _groww_adapter(sdk: _EmptySDK) -> GrowwMarketAdapter:
    return GrowwMarketAdapter(
        client=GrowwClient(sdk=sdk, sleep=lambda _s: None),
        index=_NIFTY_INDEX,
    )


class TestProbePacing:
    def test_every_probe_fetch_admits_through_the_shared_pace(self) -> None:
        """THE BLOCKER regression: the learner built over ``with_paced_client(adapter,
        pace)`` (the production build-site shape) admits EVERY physical probe fetch
        through the shared pace hook — the client's per-attempt ``pace()`` is the one
        canonical admission point (DEC-RATE-SINGLE-ADMISSION-001), so the probe can
        never bypass the 8 r/s ceiling / 500/min governor."""
        sdk = _EmptySDK()
        admissions = _Admissions()
        learner = make_vendor_floor_learner(
            with_paced_client(_groww_adapter(sdk), admissions.pace),
            history_floor=_FLOOR,
            today=date(2020, 3, 1),
        )
        learner.observe_boundary_empty(
            instrument=_inst("NIFTY"),
            window_start=_FLOOR,
            ledger=None,
            empty_breaker=None,
        )
        assert sdk.calls >= 2  # the boundary fetch + at least one bisect step ran
        assert sdk.calls <= DEPTH_PROBE_MAX_CALLS
        assert admissions.count == sdk.calls  # ONE admission per physical probe fetch

    def test_raw_adapter_probe_is_unpaced_the_blocker_shape(self) -> None:
        """The bypass this pins against: a learner built over the RAW adapter (the
        pre-fix build-site shape — the live pool leaf's pace is a no-op) probes with
        ZERO admissions. Pacing is the CALLER's obligation via ``with_paced_client``;
        if this test ever fails because vendor_floor started pacing internally,
        re-point the build-site pins first (never silently double-admit)."""
        sdk = _EmptySDK()
        admissions = _Admissions()
        learner = make_vendor_floor_learner(
            _groww_adapter(sdk),  # RAW — no with_paced_client
            history_floor=_FLOOR,
            today=date(2020, 3, 1),
        )
        learner.observe_boundary_empty(
            instrument=_inst("NIFTY"),
            window_start=_FLOOR,
            ledger=None,
            empty_breaker=None,
        )
        assert sdk.calls >= 2  # the probe genuinely fetched
        assert admissions.count == 0  # ...and NOTHING admitted through the bucket
