"""O(1)-per-trade bench for the futures translation executor (DEC-FUTURES-EXEC-TRANSLATION-001).

R29 / Golden Rule #5: after the resolver/source caches warm, the per-trade
cost of front-month resolution + entry/exit fill lookup must be independent of
(a) how many trades the run prices and (b) how many contracts the calendar
lists — a front-month pick over the cached FUT calendar (scaled here to 30
all-live AND to the real 60-expired+3-live registry shape to prove flatness)
+ two O(1) dict probes.

## HONEST ENVELOPE (Golden Rule #10 — stated, never hidden)

* The FIRST per-contract bar load is O(bars-in-contract) (≤ ~3 month files —
  the ``expiry−60d → expiry`` life) and the FIRST calendar load is
  O(contracts) — both paid ONCE per contract/underlying, outside this gate's
  steady-state measurement.
* The per-trade MAE/MFE walk is O(bars-in-[entry, exit]) (≤ ~375 intraday
  minutes) — inherent R22 work; this bench holds the window size CONSTANT
  across tiers so the per-trade ratio isolates the lookup cost.
* The front-month pick is O(M) over the underlying's FULL cached calendar —
  the registry deliberately RETAINS expired months (no live filter is
  possible at cache-build time: ``on_date`` varies per historical trade), so
  the real M is the whole accumulated calendar (~13/listed-year, ~78 for
  NIFTY 2020→2026, growing ~13/yr), NOT merely the ≤ ~3 currently-live
  serial months. M is bounded (dozens) + trade-count-independent, so the
  per-trade cost is effectively O(1) — proven FLAT here at 3 vs 30 all-live
  AND at 3-live vs 60-expired+3-live (never a literal O(1)-total claim).
* The AGGREGATE is O(N signals) — inherent work over the input.

Tiers: mean per-trade latency ratio gated at the shared-CI robust ceiling
(``tests/bench/_o1_tiers.py::ratio_ceiling`` — 5× shared / the decreed 1.1×
on the M4-Pro strict tier). Best-of-N timing de-flakes upward-only OS jitter.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from brutex.data.models import CandleBar
from brutex.futures.contract_resolver import FutContract, resolve_front_month
from brutex.futures.executor import (
    FuturesBarWindow,
    FuturesSignal,
    MaterializedFuturesTrade,
    materialize_futures_trade,
)
from brutex.options.strike_rules import TradeDirection

from tests.bench._o1_tiers import ratio_ceiling

_IST = ZoneInfo("Asia/Kolkata")
_ENTRY = datetime(2025, 1, 2, 9, 16, tzinfo=_IST)
_SYMBOL = "NSE-NIFTY-30Jan25-FUT"

_N_SMALL = 200
_N_BIG = 2_000
_REPEATS = 5
#: Fixed 10-minute trade window (constant MAE/MFE path length across tiers).
_WINDOW_MINUTES = 10


def _bar(ts: datetime, price: int) -> CandleBar:
    o = Decimal(price)
    return CandleBar(
        timestamp=ts.astimezone(UTC),
        open=o,
        high=o + 10,
        low=o - 10,
        close=o,
        volume=10,
        open_interest=None,
    )


@dataclass
class _DictSource:
    bars: dict[datetime, CandleBar]

    def load_window(
        self, *, contract_symbol: str, entry_ts: datetime, exit_ts: datetime
    ) -> FuturesBarWindow | None:
        entry = self.bars.get(entry_ts.astimezone(UTC))
        exit_ = self.bars.get(exit_ts.astimezone(UTC))
        if entry is None or exit_ is None:
            return None
        path: list[CandleBar] = []
        t = entry_ts.astimezone(UTC)
        end = exit_ts.astimezone(UTC)
        while t <= end:
            bar = self.bars.get(t)
            if bar is not None:
                path.append(bar)
            t += timedelta(minutes=1)
        return FuturesBarWindow(entry_bar=entry, exit_bar=exit_, path=tuple(path))


@dataclass
class _CalendarResolver:
    contracts: tuple[FutContract, ...]

    def resolve(self, *, instrument: str, on_date: date) -> str | None:
        return resolve_front_month(self.contracts, on_date=on_date)


def _signals(n: int) -> list[FuturesSignal]:
    # n same-session trades over a CONSTANT 10-minute window each (entry
    # minute rotates inside the session so the dict probes vary).
    out: list[FuturesSignal] = []
    for i in range(n):
        entry = _ENTRY + timedelta(minutes=i % 300)
        out.append(
            FuturesSignal(
                instrument="NIFTY",
                direction=TradeDirection.BULL,
                entry_ts=entry,
                exit_ts=entry + timedelta(minutes=_WINDOW_MINUTES),
            )
        )
    return out


def _session_source() -> _DictSource:
    bars = {
        (_ENTRY + timedelta(minutes=i)).astimezone(UTC): _bar(
            _ENTRY + timedelta(minutes=i), 22_000 + (i % 7)
        )
        for i in range(320)
    }
    return _DictSource(bars)


def _per_trade_seconds(n: int, resolver: _CalendarResolver, source: _DictSource) -> float:
    signals = _signals(n)
    # Warm every cache (first-load O(files)/O(contracts) is OUTSIDE the gate).
    materialize_futures_trade(signals[0], resolver=resolver, source=source)
    best = float("inf")
    for _ in range(_REPEATS):
        start = time.perf_counter()
        for signal in signals:
            out = materialize_futures_trade(signal, resolver=resolver, source=source)
            assert isinstance(out, MaterializedFuturesTrade)
        best = min(best, (time.perf_counter() - start) / n)
    return best


def _contracts(k: int, *, base: date = date(2025, 1, 30)) -> tuple[FutContract, ...]:
    out = []
    for i in range(k):
        month = (base.month - 1 + i) % 12 + 1
        year = base.year + (base.month - 1 + i) // 12
        expiry = date(year, month, 27)
        out.append(FutContract(f"NSE-NIFTY-27{expiry.strftime('%b%y')}-FUT", expiry))
    return tuple(out)


class TestFuturesExecO1:
    def test_r29_per_trade_flat_vs_signal_count(self) -> None:
        # [R29] per-trade latency at N=200 vs N=2,000 signals stays flat.
        resolver = _CalendarResolver(_contracts(3))
        source = _session_source()
        small = _per_trade_seconds(_N_SMALL, resolver, source)
        big = _per_trade_seconds(_N_BIG, resolver, source)
        ratio = big / small
        assert ratio <= ratio_ceiling(5.0), f"per-trade ratio {ratio:.2f} vs signals"

    def test_r29_per_trade_flat_vs_contract_count(self) -> None:
        # [R29] a 10× deeper ALL-LIVE contract calendar (3 → 30 live serial
        # months — far beyond the real ≤ ~3 live at a time) must not move the
        # per-trade cost. The real registry shape (expired months retained)
        # is pinned separately below.
        source = _session_source()
        shallow = _per_trade_seconds(_N_SMALL, _CalendarResolver(_contracts(3)), source)
        deep = _per_trade_seconds(_N_SMALL, _CalendarResolver(_contracts(30)), source)
        ratio = deep / shallow
        assert ratio <= ratio_ceiling(5.0), f"per-trade ratio {ratio:.2f} vs contracts"

    def test_r29_per_trade_flat_vs_expired_calendar_depth(self) -> None:
        # [R29 regression — O1-WORDING finding] the REAL registry shape: the
        # cached FUT calendar retains EXPIRED months (registry_store.py's
        # incremental-skip design; no live filter is possible at cache-build
        # time since on_date varies per historical trade), so the per-trade
        # pick is O(M) over the WHOLE accumulated calendar, not the ≤ ~3 live
        # months. Pin: 60 expired (2020→2024) + the same 3 live months (the
        # ~78-contract NIFTY 2020→2026 shape) vs 3-live-only must stay flat —
        # both tiers resolve the IDENTICAL front month (27Jan25), so the
        # ratio isolates the calendar-depth term.
        expired_plus_live = _contracts(63, base=date(2020, 1, 30))
        assert sum(c.expiry_date < _ENTRY.date() for c in expired_plus_live) == 60
        assert resolve_front_month(expired_plus_live, on_date=_ENTRY.date()) == (
            resolve_front_month(_contracts(3), on_date=_ENTRY.date())
        )
        source = _session_source()
        live_only = _per_trade_seconds(_N_SMALL, _CalendarResolver(_contracts(3)), source)
        deep = _per_trade_seconds(_N_SMALL, _CalendarResolver(expired_plus_live), source)
        ratio = deep / live_only
        assert ratio <= ratio_ceiling(5.0), f"per-trade ratio {ratio:.2f} vs expired depth"
