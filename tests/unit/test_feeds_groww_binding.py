"""Unit tests for :mod:`brutex.feeds.groww_binding` (DEC-FEED-INTERFACE-001, #1752).

100% line + branch. Every seam is pinned by OBJECT IDENTITY to the live spine
object, so the thin shim structurally cannot drift without a red test; ceilings
are pinned by literal value; the history floor by value; the quota is honestly
``None``; and the adapter class conforms to :class:`MarketAdapter`.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from brutex.adapters.groww.adapter import GrowwMarketAdapter
from brutex.adapters.groww.chunks import window_days_for_interval
from brutex.adapters.groww.client import (
    client_self_paces,
    client_self_reports_rate_limits,
    client_self_reports_successes,
)
from brutex.adapters.groww.constants import (
    EARLIEST_HISTORY,
    GROWW_FUTURE_COVERAGE_MARKET,
    GROWW_OPTION_COVERAGE_MARKET,
    GROWW_RATE_LIMIT_PER_MIN,
    GROWW_RATE_LIMIT_RPS_CEILING,
)
from brutex.adapters.groww.errors import GrowwError, is_rate_limit, is_retryable
from brutex.adapters.groww.instruments import InstrumentIndex
from brutex.adapters.ingest_common import GROWW_COVERAGE_MARKET
from brutex.adapters.protocol import MarketAdapter
from brutex.feeds.binding import FeedBinding
from brutex.feeds.groww_binding import build_groww_binding


def test_returns_feed_binding() -> None:
    assert isinstance(build_groww_binding(), FeedBinding)


def test_ledger_market_namespaces() -> None:
    b = build_groww_binding()
    assert b.feed_id == "groww"
    assert b.ledger_market == GROWW_COVERAGE_MARKET == "groww"
    assert b.ledger_market_future == GROWW_FUTURE_COVERAGE_MARKET == "groww-future"
    assert b.ledger_market_option == GROWW_OPTION_COVERAGE_MARKET == "groww-option"


def test_window_days_for_is_live_object() -> None:
    assert build_groww_binding().window_days_for is window_days_for_interval


def test_error_triple_identity() -> None:
    b = build_groww_binding()
    assert b.error_policy.is_retryable is is_retryable
    assert b.error_policy.is_rate_limit is is_rate_limit
    assert b.error_policy.terminal_base is GrowwError


def test_self_pacing_predicate_identity() -> None:
    b = build_groww_binding()
    assert b.client_self_paces is client_self_paces
    assert b.client_self_reports_rate_limits is client_self_reports_rate_limits
    assert b.client_self_reports_successes is client_self_reports_successes


def test_pacing_literal_values() -> None:
    b = build_groww_binding()
    assert b.pacing.ceiling_rps == GROWW_RATE_LIMIT_RPS_CEILING == 8.0
    assert b.pacing.ceiling_per_min == GROWW_RATE_LIMIT_PER_MIN == 500
    # honestly none — the backtesting daily quota is UNVERIFIED (§6, Golden Rule #1).
    assert b.pacing.daily_quota is None


def test_history_floor_is_verified_2020() -> None:
    b = build_groww_binding()
    assert b.history_floor == EARLIEST_HISTORY == date(2020, 1, 1)


def test_adapter_factory_identity_and_conformance() -> None:
    b = build_groww_binding()
    assert b.adapter_factory is GrowwMarketAdapter
    empty_index = InstrumentIndex(by_groww_symbol={}, by_exchange_symbol={}, universe=())
    adapter = b.adapter_factory(client=object(), index=empty_index)
    assert isinstance(adapter, MarketAdapter)


def test_lake_root_for_is_identity_default() -> None:
    b = build_groww_binding()
    root = Path("/lake")
    assert b.lake_root_for(root) is root
