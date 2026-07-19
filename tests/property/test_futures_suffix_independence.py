"""Property gates: futures-leg suffix independence (ROLL-9/R18) + rank independence (R13/R30).

DEC-FUTURES-EXEC-TRANSLATION-001. The futures leg for a trade over bars
``0..exit`` must be BIT-IDENTICAL whether the bars AFTER ``exit_ts`` are
absent, mutated, or extreme — the same look-ahead discipline as
DEC-SWEEP-LOOKAHEAD-GUARD-001's suffix-independence gates. And annotating a
result row can never perturb the pure row (annotation-never-reranks).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from brutex.data.models import CandleBar
from brutex.futures.executor import (
    FuturesBarWindow,
    FuturesRuleResult,
    FuturesSignal,
    MaterializedFuturesTrade,
    materialize_futures_trade,
)
from brutex.futures.leaderboard import ANNOTATION_KEY, project_futures_annotation
from brutex.options.strike_rules import TradeDirection
from hypothesis import given, settings
from hypothesis import strategies as st

_IST = ZoneInfo("Asia/Kolkata")
_ENTRY = datetime(2025, 1, 2, 9, 16, tzinfo=_IST)
_SYMBOL = "NSE-NIFTY-30Jan25-FUT"


def _bar(ts: datetime, price_paisa: int, spread_paisa: int) -> CandleBar:
    o = Decimal(price_paisa) / 100
    spread = Decimal(spread_paisa) / 100
    return CandleBar(
        timestamp=ts.astimezone(UTC),
        open=o,
        high=o + spread,
        low=o - spread,
        close=o,
        volume=10,
        open_interest=None,
    )


@dataclass
class _DictSource:
    """A minute-keyed in-memory FuturesBarSource over an arbitrary bar map."""

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
class _Resolver:
    def resolve(self, *, instrument: str, on_date: date) -> str | None:
        return _SYMBOL


_PRICES = st.lists(st.integers(min_value=2_000_000, max_value=2_400_000), min_size=6, max_size=30)
_SPREADS = st.integers(min_value=0, max_value=5_000)


class TestSuffixIndependence:
    @settings(max_examples=50, deadline=None)
    @given(prices=_PRICES, spread=_SPREADS, direction=st.sampled_from(list(TradeDirection)))
    def test_r18_futures_leg_suffix_independence(
        self, prices: list[int], spread: int, direction: TradeDirection
    ) -> None:
        # [ROLL-9/R18] the trade spans the FIRST HALF of the bar stream; the
        # suffix (bars after exit) is (a) present, (b) absent, (c) mutated to
        # extremes — fills + MAE/MFE + net must be bit-identical in all three.
        half = len(prices) // 2
        bars = {
            (_ENTRY + timedelta(minutes=i)).astimezone(UTC): _bar(
                _ENTRY + timedelta(minutes=i), p, spread
            )
            for i, p in enumerate(prices)
        }
        exit_ts = _ENTRY + timedelta(minutes=half - 1)
        signal = FuturesSignal(
            instrument="NIFTY", direction=direction, entry_ts=_ENTRY, exit_ts=exit_ts
        )
        with_suffix = materialize_futures_trade(
            signal, resolver=_Resolver(), source=_DictSource(bars)
        )
        truncated = {t: b for t, b in bars.items() if t <= exit_ts.astimezone(UTC)}
        without_suffix = materialize_futures_trade(
            signal, resolver=_Resolver(), source=_DictSource(truncated)
        )
        extreme = dict(truncated)
        for i in range(half, len(prices)):
            ts = (_ENTRY + timedelta(minutes=i)).astimezone(UTC)
            extreme[ts] = _bar(_ENTRY + timedelta(minutes=i), 9_900_000, 100_000)
        with_extreme_suffix = materialize_futures_trade(
            signal, resolver=_Resolver(), source=_DictSource(extreme)
        )
        assert isinstance(with_suffix, MaterializedFuturesTrade)
        assert with_suffix == without_suffix == with_extreme_suffix

    @settings(max_examples=50, deadline=None)
    @given(prices=_PRICES, spread=_SPREADS)
    def test_r18_entry_fills_at_the_signal_minute_never_a_later_bar(
        self, prices: list[int], spread: int
    ) -> None:
        # [R18] the entry fill derives ONLY from the entry-minute bar under
        # the ONE worst-case law (DEC-FILL-WORST-CASE-ONLY-001): its adverse
        # HIGH + 1 tick — recomputed independently from the input price list
        # (high = open + spread by the _bar helper's construction). A LATER
        # bar's price can never leak into the fill.
        bars = {
            (_ENTRY + timedelta(minutes=i)).astimezone(UTC): _bar(
                _ENTRY + timedelta(minutes=i), p, spread
            )
            for i, p in enumerate(prices)
        }
        exit_ts = _ENTRY + timedelta(minutes=len(prices) - 1)
        signal = FuturesSignal(
            instrument="NIFTY", direction=TradeDirection.BULL, entry_ts=_ENTRY, exit_ts=exit_ts
        )
        out = materialize_futures_trade(
            signal,
            resolver=_Resolver(),
            source=_DictSource(bars),
        )
        assert isinstance(out, MaterializedFuturesTrade)
        assert out.buy_fill_paisa == prices[0] + spread + 5


class TestRankIndependence:
    @settings(max_examples=50, deadline=None)
    @given(
        rank=st.integers(min_value=0, max_value=1_000_000),
        sharpe=st.floats(allow_nan=False, allow_infinity=False, width=32),
        net=st.integers(min_value=-(10**12), max_value=10**12),
    )
    def test_r13_r30_rank_independence_property(self, rank: int, sharpe: float, net: int) -> None:
        # [R13/R30] annotate → strip → the pure row is BIT-IDENTICAL; the
        # ranking-input fields are never rewritten by the annotation.
        row = {"rank": rank, "sharpe_ratio": sharpe, "net_pnl_paisa": net, "run_id": "r"}
        result = FuturesRuleResult(
            instrument="NIFTY",
            direction="bull",
            n_signals=1,
            n_accepted=1,
            n_rejected_lot_size_unknown=0,
            n_rejected_cost_regime_unverified=0,
            n_rejected_no_contract=0,
            n_rejected_no_data=0,
            n_rejected_stale_bar=0,
            gross_pnl_paisa=100,
            total_charges_paisa=0,
            net_pnl_paisa=100,
            n_winners=1,
            n_losers=0,
        )
        annotated = project_futures_annotation(row, result)
        stripped = {k: v for k, v in annotated.items() if k != ANNOTATION_KEY}
        assert stripped == row
        assert row == {"rank": rank, "sharpe_ratio": sharpe, "net_pnl_paisa": net, "run_id": "r"}
