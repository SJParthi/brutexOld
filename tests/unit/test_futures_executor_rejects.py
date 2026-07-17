"""Reject-order + reject-counting pins for the futures executor.

The PINNED deterministic order — lot_size → cost_regime → no_contract →
no_data → stale_bar — with the two pure DATE gates proven to fire BEFORE any
registry/lake I/O (never-called seams), one WARN per warned reject, and the
count-dict tally proven to never miscount (DEC-FUTURES-EXEC-TRANSLATION-001).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from brutex.data.models import CandleBar
from brutex.futures import executor as executor_module
from brutex.futures.executor import (
    FuturesBarWindow,
    FuturesRejectReason,
    FuturesSignal,
    MaterializedFuturesTrade,
    RejectedFuturesTrade,
    materialize_futures_trade,
    run_futures_over_signals,
)
from brutex.options.strike_rules import TradeDirection
from structlog.testing import capture_logs

_IST = ZoneInfo("Asia/Kolkata")
_ENTRY = datetime(2025, 1, 2, 9, 16, tzinfo=_IST)
_EXIT = datetime(2025, 1, 2, 15, 20, tzinfo=_IST)
_ENTRY_2020 = datetime(2020, 6, 1, 9, 16, tzinfo=_IST)
_EXIT_2020 = datetime(2020, 6, 1, 15, 20, tzinfo=_IST)
_SYMBOL = "NSE-NIFTY-30Jan25-FUT"


class _NeverCalledResolver:
    """Proves a date gate fired BEFORE any contract resolution I/O."""

    def resolve(self, *, instrument: str, on_date: date) -> str | None:
        raise AssertionError("resolver must not be called before the pure date gates pass")


class _NeverCalledSource:
    """Proves rejection fired BEFORE any lake I/O."""

    def load_window(
        self, *, contract_symbol: str, entry_ts: datetime, exit_ts: datetime
    ) -> FuturesBarWindow | None:
        raise AssertionError("source must not be called before earlier gates pass")


@dataclass
class _Resolver:
    symbol: str | None = _SYMBOL

    def resolve(self, *, instrument: str, on_date: date) -> str | None:
        return self.symbol


@dataclass
class _Source:
    window: FuturesBarWindow | None = None
    calls: list[tuple[str, datetime, datetime]] = field(default_factory=list)

    def load_window(
        self, *, contract_symbol: str, entry_ts: datetime, exit_ts: datetime
    ) -> FuturesBarWindow | None:
        self.calls.append((contract_symbol, entry_ts, exit_ts))
        return self.window


def _bar(ts: datetime, open_: str, *, volume: int = 100) -> CandleBar:
    o = Decimal(open_)
    return CandleBar(
        timestamp=ts.astimezone(UTC),
        open=o,
        high=o + Decimal("10.00"),
        low=o - Decimal("10.00"),
        close=o,
        volume=volume,
        open_interest=None,
    )


def _window(*, entry_volume: int = 100, exit_volume: int = 100) -> FuturesBarWindow:
    entry_bar = _bar(_ENTRY, "22000.00", volume=entry_volume)
    exit_bar = _bar(_EXIT, "22100.00", volume=exit_volume)
    return FuturesBarWindow(entry_bar=entry_bar, exit_bar=exit_bar, path=(entry_bar, exit_bar))


def _signal(**overrides: Any) -> FuturesSignal:
    kwargs: dict[str, Any] = {
        "instrument": "NIFTY",
        "direction": TradeDirection.BULL,
        "entry_ts": _ENTRY,
        "exit_ts": _EXIT,
    }
    kwargs.update(overrides)
    return FuturesSignal(**kwargs)


class TestRejectOrderPinned:
    def test_reject_order_pinned_two_date_gates_before_io(self) -> None:
        # [R14/R15/R28] gate 1 (LOT) fires with NEVER-CALLED seams: no
        # registry/lake I/O happens for a pre-history signal.
        out = materialize_futures_trade(
            _signal(entry_ts=_ENTRY_2020, exit_ts=_EXIT_2020),
            resolver=_NeverCalledResolver(),
            source=_NeverCalledSource(),
        )
        assert isinstance(out, RejectedFuturesTrade)
        assert out.reason is FuturesRejectReason.LOT_SIZE_UNKNOWN
        assert out.leg is None

    def test_cost_gate_fires_before_io(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Gate 2 (COST_REGIME) also fires with NEVER-CALLED seams — a
        # cost-bearing stock future with a KNOWN (synthetic) lot skips before
        # any wasted registry/lake read.
        monkeypatch.setattr(executor_module, "futures_lot_size", lambda *_a: 500)
        out = materialize_futures_trade(
            _signal(instrument="RELIANCE"),
            resolver=_NeverCalledResolver(),
            source=_NeverCalledSource(),
        )
        assert isinstance(out, RejectedFuturesTrade)
        assert out.reason is FuturesRejectReason.COST_REGIME_UNVERIFIED
        assert out.leg is None

    def test_lot_precedes_cost_regime(self) -> None:
        # A stock future with NO lot history trips LOT first (both gates
        # would fire; the pinned order picks lot).
        out = materialize_futures_trade(
            _signal(instrument="RELIANCE"),
            resolver=_NeverCalledResolver(),
            source=_NeverCalledSource(),
        )
        assert isinstance(out, RejectedFuturesTrade)
        assert out.reason is FuturesRejectReason.LOT_SIZE_UNKNOWN

    def test_no_contract_precedes_no_data(self) -> None:
        # Gate 3 (NO_CONTRACT) fires with a never-called SOURCE.
        out = materialize_futures_trade(
            _signal(), resolver=_Resolver(symbol=None), source=_NeverCalledSource()
        )
        assert isinstance(out, RejectedFuturesTrade)
        assert out.reason is FuturesRejectReason.NO_CONTRACT

    def test_no_data_precedes_stale_bar(self) -> None:
        # Gate 4 (NO_DATA) fires before the stale gate even with the flag ON.
        out = materialize_futures_trade(
            _signal(),
            resolver=_Resolver(),
            source=_Source(window=None),
        )
        assert isinstance(out, RejectedFuturesTrade)
        assert out.reason is FuturesRejectReason.NO_DATA

    def test_cost_free_index_skips_cost_gate_entirely(self) -> None:
        # The LIVE index path (cost-free) never consults the refusal tables —
        # a 2021+ NIFTY signal sails to resolution.
        out = materialize_futures_trade(
            _signal(), resolver=_Resolver(), source=_Source(window=_window())
        )
        assert isinstance(out, MaterializedFuturesTrade)


class TestR15MissingBarNoSubstitution:
    def test_r15_missing_entry_and_missing_exit_bar_each_no_data_no_substitution(self) -> None:
        # [R15/R14] the source honestly returns None for a missing endpoint —
        # the trade rejects NO_DATA; leg is carried (the reject names the
        # contract); no nearest-minute price is ever fabricated.
        out = materialize_futures_trade(
            _signal(), resolver=_Resolver(), source=_Source(window=None)
        )
        assert isinstance(out, RejectedFuturesTrade)
        assert out.reason is FuturesRejectReason.NO_DATA
        assert out.leg is not None
        assert out.leg.contract_symbol == _SYMBOL

    def test_r14_missing_bar_no_data_pure_row_untouched(self) -> None:
        # [R14/R28] the reject mutates NOTHING upstream: the signal object is
        # frozen and byte-identical after the reject (the pure spot row's
        # untouchability at this seam).
        sig = _signal()
        before = repr(sig)
        materialize_futures_trade(sig, resolver=_Resolver(), source=_Source(window=None))
        assert repr(sig) == before


class TestStaleBarUnconditional:
    """DEC-FILL-WORST-CASE-ONLY-001: the volume-0 stale-bar reject is
    UNCONDITIONAL — the ``reject_stale_bars`` knob is DELETED and futures
    FLIP from R15's fill-by-default to always-reject (the R15 "printed bar
    is REAL and TRADABLE" clause is consciously superseded; the reject is
    COUNTED so the survivorship tilt stays visible, never silent)."""

    def test_stale_gate_fires_unconditionally_on_entry(self) -> None:
        out = materialize_futures_trade(
            _signal(),
            resolver=_Resolver(),
            source=_Source(window=_window(entry_volume=0)),
        )
        assert isinstance(out, RejectedFuturesTrade)
        assert out.reason is FuturesRejectReason.STALE_BAR

    def test_stale_gate_fires_unconditionally_on_exit(self) -> None:
        out = materialize_futures_trade(
            _signal(),
            resolver=_Resolver(),
            source=_Source(window=_window(exit_volume=0)),
        )
        assert isinstance(out, RejectedFuturesTrade)
        assert out.reason is FuturesRejectReason.STALE_BAR

    def test_zero_volume_never_fills_the_r15_flip(self) -> None:
        # Fails-before pin: pre-DEC the flagless default FILLED this window.
        out = materialize_futures_trade(
            _signal(),
            resolver=_Resolver(),
            source=_Source(window=_window(entry_volume=0, exit_volume=0)),
        )
        assert isinstance(out, RejectedFuturesTrade)
        assert out.reason is FuturesRejectReason.STALE_BAR

    def test_no_reject_stale_bars_knob_survives(self) -> None:
        # Killed-token pin: the knob is gone from BOTH entry points.
        import inspect

        assert "reject_stale_bars" not in inspect.signature(materialize_futures_trade).parameters
        assert "reject_stale_bars" not in inspect.signature(run_futures_over_signals).parameters


class TestOneWarnPerReject:
    def test_one_warn_per_reject_and_aggregate_unverified_cost_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [R28] each warned reject emits EXACTLY one WARN.
        with capture_logs() as logs:
            materialize_futures_trade(
                _signal(entry_ts=_ENTRY_2020, exit_ts=_EXIT_2020),
                resolver=_NeverCalledResolver(),
                source=_NeverCalledSource(),
            )
        assert [x["event"] for x in logs] == ["futures.reject.lot_size_unknown"]

        monkeypatch.setattr(executor_module, "futures_lot_size", lambda *_a: 500)
        with capture_logs() as logs:
            materialize_futures_trade(
                _signal(instrument="RELIANCE"),
                resolver=_NeverCalledResolver(),
                source=_NeverCalledSource(),
            )
        events = [x["event"] for x in logs]
        assert events == ["futures.reject.cost_regime_unverified"]

        with capture_logs() as logs:
            materialize_futures_trade(
                _signal(),
                resolver=_Resolver(),
                source=_Source(window=_window(entry_volume=0)),
            )
        assert [x["event"] for x in logs] == ["futures.reject.stale_bar"]

    def test_no_data_and_no_contract_are_counted_not_warned(self) -> None:
        # Mirrors the options precedent: NO_DATA/NO_CONTRACT are counted
        # rejects (visible on the result + audit), not per-trade WARNs.
        with capture_logs() as logs:
            materialize_futures_trade(
                _signal(), resolver=_Resolver(symbol=None), source=_NeverCalledSource()
            )
            materialize_futures_trade(_signal(), resolver=_Resolver(), source=_Source(window=None))
        assert [x["event"] for x in logs] == []


class TestNarrowExcepts:
    def test_lot_gate_except_is_narrow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A NON-ValueError from the lot lookup propagates — the except wraps
        # only the honest lot-unknown case (mutation-killer).
        def _boom(*_a: object) -> int:
            raise RuntimeError("genuine defect")

        monkeypatch.setattr(executor_module, "futures_lot_size", _boom)
        with pytest.raises(RuntimeError, match="genuine defect"):
            materialize_futures_trade(
                _signal(), resolver=_NeverCalledResolver(), source=_NeverCalledSource()
            )

    def test_cost_gate_except_is_narrow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A generic ValueError from the charge probe propagates — only the
        # typed UnverifiedCostRegimeError converts to a reject.
        monkeypatch.setattr(executor_module, "futures_lot_size", lambda *_a: 500)

        def _boom(*_a: object) -> int:
            raise ValueError("generic defect, not a refusal")

        monkeypatch.setattr(executor_module, "futures_exchange_charge_bps_x100", _boom)
        with pytest.raises(ValueError, match="generic defect"):
            materialize_futures_trade(
                _signal(instrument="RELIANCE"),
                resolver=_NeverCalledResolver(),
                source=_NeverCalledSource(),
            )


class TestRejectCountsNoSilentMiscount:
    def test_reject_counts_no_silent_miscount(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # [R28] a batch hitting FOUR distinct reject reasons + one accept:
        # every bucket lands exactly, the invariant reconciles to the digit.
        lot_unknown = _signal(entry_ts=_ENTRY_2020, exit_ts=_EXIT_2020)
        accepted = _signal()
        no_data = _signal(entry_ts=_ENTRY + timedelta(minutes=1), exit_ts=_EXIT)
        stale_entry = _ENTRY + timedelta(minutes=2)
        stale = _signal(entry_ts=stale_entry, exit_ts=_EXIT)
        no_contract = _signal(entry_ts=_ENTRY + timedelta(minutes=3), exit_ts=_EXIT)

        windows: dict[datetime, FuturesBarWindow | None] = {}
        good = _window()
        windows[_ENTRY] = good
        stale_bar = _bar(stale_entry, "22000.00", volume=0)
        exit_bar = _bar(_EXIT, "22100.00")
        windows[stale_entry] = FuturesBarWindow(
            entry_bar=stale_bar, exit_bar=exit_bar, path=(stale_bar, exit_bar)
        )

        @dataclass
        class _MapSource:
            def load_window(
                self, *, contract_symbol: str, entry_ts: datetime, exit_ts: datetime
            ) -> FuturesBarWindow | None:
                return windows.get(entry_ts)

        # The no_contract signal needs a per-signal resolver miss: key on the
        # entry minute via a closure over the signal's timestamp.
        class _SelectiveResolver:
            def resolve(self, *, instrument: str, on_date: date) -> str | None:
                return _SYMBOL

        resolver = _SelectiveResolver()
        original = executor_module.select_futures_leg

        def _leg_router(signal: FuturesSignal, *, resolver: object) -> object:
            if signal.entry_ts == no_contract.entry_ts:
                return RejectedFuturesTrade(leg=None, reason=FuturesRejectReason.NO_CONTRACT)
            return original(signal, resolver=resolver)  # type: ignore[arg-type]

        monkeypatch.setattr(executor_module, "select_futures_leg", _leg_router)
        res = run_futures_over_signals(
            [lot_unknown, accepted, no_data, stale, no_contract],
            instrument="NIFTY",
            direction=TradeDirection.BULL,
            resolver=resolver,
            source=_MapSource(),
        )
        assert res.n_signals == 5
        assert res.n_accepted == 1
        assert res.n_rejected_lot_size_unknown == 1
        assert res.n_rejected_no_data == 1
        assert res.n_rejected_stale_bar == 1
        assert res.n_rejected_no_contract == 1
        assert res.n_rejected_cost_regime_unverified == 0
        assert res.n_rejected == 4
        assert res.n_accepted + res.n_rejected == res.n_signals
