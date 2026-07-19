"""Threading + scope-lock tests for the observed-sessions overlay wiring.

DEC-CALENDAR-OBSERVED-SESSIONS-001. The overlay reaches ONLY the sanctioned
consumers: the two completeness detectors' margin arithmetic (threaded
``observed_holidays`` kwarg through BOTH ingest spines, armed at the production
pull sites) and the anchor/report walks. It NEVER reaches pull window planning
and NEVER the engine's fib-definitions call sites (which keep the default empty
overlay so the blake3 run identity is unchanged — CLAUDE.md Golden Rule #4).
"""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import brutex.adapters.groww.ingest as ingest_mod
import brutex.adapters.groww.option_ingest as option_ingest_mod
import pytest
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.ingest import ingest_spot
from brutex.adapters.groww.option_ingest import ingest_option_contract
from brutex.adapters.protocol import DerivativeContract
from brutex.data.models import CandleBar
from brutex.data.sink import SinkAppendResult
from brutex.models import Exchange, Instrument, InstrumentType, Segment

_OBSERVED = frozenset({date(2026, 5, 12)})


def _inst(symbol: str = "NIFTY") -> Instrument:
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


def _bar(ts: datetime | None = None) -> CandleBar:
    stamp = ts if ts is not None else datetime(2021, 1, 4, 4, 5, tzinfo=UTC)
    return CandleBar(
        timestamp=stamp,
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal("105"),
        volume=10,
        open_interest=None,
    )


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


class _Adapter:
    def fetch_ohlcv(
        self, *, symbol: str, exchange: object, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        del symbol, exchange, interval, start, end
        return [_bar()]


class _ObserveSpies:
    """Recorders swapped in for the two COMMON detector observe helpers."""

    def __init__(self) -> None:
        self.partial_observed: list[object] = []
        self.interior_observed: list[object] = []

    def partial(self, *_args: object, observed: object = frozenset(), **_kw: object) -> None:
        self.partial_observed.append(observed)

    def interior(self, *_args: object, observed: object = frozenset(), **_kw: object) -> None:
        self.interior_observed.append(observed)


class TestSpotSpineThreading:
    def _run(
        self, monkeypatch: pytest.MonkeyPatch, **kwargs: Any
    ) -> tuple[list[object], list[object]]:
        spies = _ObserveSpies()
        monkeypatch.setattr(ingest_mod, "observe_partial_window", spies.partial)
        monkeypatch.setattr(ingest_mod, "observe_interior_gap", spies.interior)
        ingest_spot(
            _Adapter(),  # type: ignore[arg-type]
            _Sink(),
            instrument=_inst(),
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 2, 1),
            **kwargs,
        )
        return spies.partial_observed, spies.interior_observed

    def test_observed_holidays_threaded_to_detectors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        partial, interior = self._run(monkeypatch, observed_holidays=_OBSERVED)
        assert partial == [_OBSERVED]
        assert interior == [_OBSERVED]

    def test_default_is_empty_frozenset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        partial, interior = self._run(monkeypatch)
        assert partial == [frozenset()]
        assert interior == [frozenset()]


class TestOptionSpineThreading:
    def test_futures_observe_receives_observed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        observed_seen: list[object] = []

        def _spy(*_args: object, observed: object = frozenset(), **_kw: object) -> None:
            observed_seen.append(observed)

        monkeypatch.setattr(option_ingest_mod, "observe_partial_window", _spy)

        class _SDK:
            def get_historical_candles(self, **_kw: object) -> dict[str, object]:
                return {"candles": [["2025-01-02T10:00:00", 100.0, 110.0, 90.0, 105.0, 50, 7]]}

        contract = DerivativeContract(
            underlying_symbol="NIFTY",
            expiry=date(2025, 1, 2),
            symbol="NSE-NIFTY-Jan2025-FUT",
            contract_type="FUT",
            strike=None,
        )
        report = ingest_option_contract(
            GrowwClient(sdk=_SDK(), sleep=lambda _s: None, max_attempts=1),  # type: ignore[arg-type]
            _Sink(),
            contract=contract,
            interval="1minute",
            start=date(2025, 1, 1),
            end=date(2025, 1, 3),
            now_ist=lambda: datetime(2025, 2, 1, 12, 0, tzinfo=UTC),
            observed_holidays=_OBSERVED,
        )
        assert report.bars_appended == 1
        assert observed_seen == [_OBSERVED]


class TestScopeLock:
    """The overlay must NEVER reach pull window planning or the engine (regression).

    SANCTIONED pin evolution (DEC-SEED-OBSERVED-OVERLAY-001): the cold-start SEED
    (``seed_enclosed_windows`` — the continuity GATE, a refusal/permit decision
    over already-planned windows, not window planning) now DOES take ``observed=``
    so seed-time and fetch-time verdicts share one calendar. Window PLANNING
    (``plan_windows`` / ``default_fno_minute_window``) stays overlay-blind — the
    scope lock this class exists for."""

    def test_pull_planning_never_receives_overlay(self) -> None:
        from brutex.adapters.groww.contract_bounds import default_fno_minute_window
        from brutex.data.window_ledger_store import plan_windows

        for fn in (plan_windows, default_fno_minute_window):
            params = inspect.signature(fn).parameters
            assert "observed" not in params, fn.__name__
            assert "observed_holidays" not in params, fn.__name__

    def test_seed_gate_now_receives_overlay_sanctioned(self) -> None:
        # DEC-SEED-OBSERVED-OVERLAY-001 — the deliberate pin FLIP: the seed gate
        # takes the overlay (default frozenset() = byte-identical pre-threading).
        from brutex.data.window_ledger_store import seed_enclosed_windows

        params = inspect.signature(seed_enclosed_windows).parameters
        assert "observed" in params
        assert params["observed"].default == frozenset()

    def test_fib_definitions_engine_uses_default_overlay(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The engine-facing PREV_DAY resolution calls lookup_prev_day_ohlc with NO
        # observed kwarg — the default empty overlay — so ITEM 1 changes nothing
        # engine-visible (blake3 run identity unchanged, Golden Rule #4).
        import brutex.strategies.fib_definitions as fib_mod
        from brutex.indicators.prev_day_ohlc import DailyCandle
        from brutex.strategies.types import FibAnchorKind

        calls: list[dict[str, object]] = []
        # ``lookup_prev_day_ohlc`` is fib_definitions' imported collaborator (a
        # module attribute, not a re-export) — resolve via getattr for mypy.
        real = getattr(fib_mod, "lookup_prev_day_ohlc")  # noqa: B009

        def _spy(*args: object, **kwargs: object) -> object:
            calls.append(dict(kwargs))
            return real(*args, **kwargs)

        monkeypatch.setattr(fib_mod, "lookup_prev_day_ohlc", _spy)
        daily = {
            date(2026, 5, 11): DailyCandle(
                day=date(2026, 5, 11),
                open_paisa=100_00,
                high_paisa=110_00,
                low_paisa=90_00,
                close_paisa=105_00,
            )
        }
        levels = fib_mod.resolve_fib_anchor_levels(FibAnchorKind.PREV_DAY, date(2026, 5, 12), daily)
        assert levels is not None
        assert calls, "the PREV_DAY branch resolves through lookup_prev_day_ohlc"
        assert all("observed" not in kw for kw in calls)
