"""R45 independent naive reference simulator pins (DEC-FUTURES-EXEC-TRANSLATION-001)."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from brutex.data.models import CandleBar
from brutex.futures import reference_sim
from brutex.futures.executor import (
    FuturesBarWindow,
    FuturesSignal,
    MaterializedFuturesTrade,
    materialize_futures_trade,
)
from brutex.futures.reference_sim import (
    DifferentialReport,
    differential_spot_check,
    naive_reference_futures_pnl,
)
from brutex.options.strike_rules import TradeDirection

_IST = ZoneInfo("Asia/Kolkata")
_ENTRY = datetime(2025, 1, 2, 9, 16, tzinfo=_IST)
_EXIT = datetime(2025, 1, 2, 15, 20, tzinfo=_IST)
_SYMBOL = "NSE-NIFTY-30Jan25-FUT"


def _bar(ts: datetime, open_: str) -> CandleBar:
    o = Decimal(open_)
    return CandleBar(
        timestamp=ts.astimezone(UTC),
        open=o,
        high=o + Decimal("10.00"),
        low=o - Decimal("10.00"),
        close=o,
        volume=10,
        open_interest=None,
    )


def _window(entry_open: str = "22000.00", exit_open: str = "22100.00") -> FuturesBarWindow:
    entry_bar = _bar(_ENTRY, entry_open)
    exit_bar = _bar(_EXIT, exit_open)
    return FuturesBarWindow(entry_bar=entry_bar, exit_bar=exit_bar, path=(entry_bar, exit_bar))


@dataclass
class _Resolver:
    symbol: str | None = _SYMBOL

    def resolve(self, *, instrument: str, on_date: date) -> str | None:
        return self.symbol


@dataclass
class _Source:
    window: FuturesBarWindow | None = None

    def load_window(
        self, *, contract_symbol: str, entry_ts: datetime, exit_ts: datetime
    ) -> FuturesBarWindow | None:
        return self.window


def _signal(**overrides: Any) -> FuturesSignal:
    kwargs: dict[str, Any] = {
        "instrument": "NIFTY",
        "direction": TradeDirection.BULL,
        "entry_ts": _ENTRY,
        "exit_ts": _EXIT,
    }
    kwargs.update(overrides)
    return FuturesSignal(**kwargs)


class TestNaiveReference:
    def test_r45_bull_agrees_bit_for_bit_with_production(self) -> None:
        # [R45] two independent number pipelines (Decimal rupees vs int paisa)
        # land on the SAME integer.
        resolver, source = _Resolver(), _Source(window=_window())
        naive = naive_reference_futures_pnl(_signal(), resolver=resolver, source=source)
        prod = materialize_futures_trade(_signal(), resolver=resolver, source=source)
        assert isinstance(prod, MaterializedFuturesTrade)
        assert naive == prod.net_pnl_paisa == 749_250

    def test_r45_bear_agrees_bit_for_bit_with_production(self) -> None:
        resolver, source = _Resolver(), _Source(window=_window())
        sig = _signal(direction=TradeDirection.BEAR)
        naive = naive_reference_futures_pnl(sig, resolver=resolver, source=source)
        prod = materialize_futures_trade(sig, resolver=resolver, source=source)
        assert isinstance(prod, MaterializedFuturesTrade)
        assert naive == prod.net_pnl_paisa == -750_750

    def test_n_lots_scaling_agrees(self) -> None:
        resolver, source = _Resolver(), _Source(window=_window())
        naive = naive_reference_futures_pnl(_signal(), resolver=resolver, source=source, n_lots=3)
        prod = materialize_futures_trade(_signal(), resolver=resolver, source=source, n_lots=3)
        assert isinstance(prod, MaterializedFuturesTrade)
        assert naive == prod.net_pnl_paisa

    def test_unpriceable_cases_return_none(self) -> None:
        # Non-index (production cost-refuses) / pre-history lot / no contract /
        # no window — all honest None, matching production rejects.
        assert (
            naive_reference_futures_pnl(
                _signal(instrument="RELIANCE"), resolver=_Resolver(), source=_Source()
            )
            is None
        )
        early = _signal(
            entry_ts=datetime(2020, 6, 1, 9, 16, tzinfo=_IST),
            exit_ts=datetime(2020, 6, 1, 15, 20, tzinfo=_IST),
        )
        assert naive_reference_futures_pnl(early, resolver=_Resolver(), source=_Source()) is None
        assert (
            naive_reference_futures_pnl(
                _signal(), resolver=_Resolver(symbol=None), source=_Source()
            )
            is None
        )
        assert (
            naive_reference_futures_pnl(
                _signal(), resolver=_Resolver(), source=_Source(window=None)
            )
            is None
        )

    def test_r45_independence_shares_only_primitive_readers(self) -> None:
        # [R45] the naive module never IMPORTS the executor's pricing logic —
        # the AST pin that stops the differential becoming a tautology (the
        # docstring may NAME the forbidden symbols; the import graph may not).
        import ast

        tree = ast.parse(inspect.getsource(reference_sim))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) or isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        for forbidden in (
            "apply_futures_fill",
            "compute_futures_trade_cost",
            "materialize_futures_trade",
            "FuturesTradeInput",
            "brutex.costs.futures_costs",
        ):
            assert forbidden not in imported, forbidden


class TestDifferentialSpotCheck:
    def test_r45_differential_naive_reference_clean(self) -> None:
        resolver, source = _Resolver(), _Source(window=_window())
        sig = _signal()
        prod = materialize_futures_trade(sig, resolver=resolver, source=source)
        assert isinstance(prod, MaterializedFuturesTrade)
        report = differential_spot_check(
            [sig], [prod.net_pnl_paisa], resolver=resolver, source=source
        )
        assert report == DifferentialReport(mismatches=(), n_checked=1, n_skipped_policy=0)
        assert not report.suspect

    def test_r45_divergent_production_trips_suspect(self) -> None:
        resolver, source = _Resolver(), _Source(window=_window())
        report = differential_spot_check([_signal()], [42], resolver=resolver, source=source)
        assert report.suspect
        assert "production=42" in report.mismatches[0]

    def test_rejected_trade_compares_none_to_none(self) -> None:
        resolver, source = _Resolver(symbol=None), _Source()
        report = differential_spot_check([_signal()], [None], resolver=resolver, source=source)
        assert not report.suspect
        assert report.n_checked == 1

    def test_policy_skipped_excluded(self) -> None:
        resolver, source = _Resolver(), _Source(window=_window())
        report = differential_spot_check(
            [_signal(), _signal(entry_ts=_ENTRY + timedelta(minutes=1))],
            [None, None],
            resolver=resolver,
            source=source,
            policy_skipped=[True, True],
        )
        assert report.n_skipped_policy == 2
        assert report.n_checked == 0
        assert not report.suspect

    def test_misaligned_inputs_fail_loud(self) -> None:
        with pytest.raises(ValueError, match="align 1:1"):
            differential_spot_check([_signal()], [], resolver=_Resolver(), source=_Source())
        with pytest.raises(ValueError, match="policy_skipped"):
            differential_spot_check(
                [_signal()], [None], resolver=_Resolver(), source=_Source(), policy_skipped=[]
            )
