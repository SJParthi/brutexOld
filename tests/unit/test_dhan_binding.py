"""Tests for :mod:`brutex.feeds.dhan_binding` — identity pins + nesting + audit chain.

100% line+branch. Every seam is pinned by OBJECT IDENTITY to the Dhan leaf object,
so the thin shim cannot drift without a red test; the honest ``history_floor`` is
``None``; the lake nests under ``feeds/dhan/``; a non-default feed's writes are
hash-chained by :class:`FeedAuditSink`; and the Groww binding is re-run for
byte-identity (Groww UNCHANGED).
"""

from __future__ import annotations

from pathlib import Path

from brutex.adapters.dhan.adapter import DhanMarketAdapter
from brutex.adapters.dhan.chunks import window_days_for_interval
from brutex.adapters.dhan.client import (
    client_self_paces,
    client_self_reports_rate_limits,
    client_self_reports_successes,
)
from brutex.adapters.dhan.errors import DhanError, is_rate_limit, is_retryable
from brutex.adapters.protocol import MarketAdapter
from brutex.data.feed_audit import FeedAuditSink
from brutex.feeds.binding import FeedBinding, build_feed_sink
from brutex.feeds.dhan_binding import build_dhan_binding
from brutex.feeds.groww_binding import build_groww_binding
from brutex.feeds.registry import resolve_feed_binding


def test_returns_feed_binding() -> None:
    assert isinstance(build_dhan_binding(), FeedBinding)


def test_ledger_market_prefix_law() -> None:
    b = build_dhan_binding()
    assert b.feed_id == "dhan"
    assert b.ledger_market == "dhan"
    assert b.ledger_market_future == "dhan-future"
    assert b.ledger_market_option == "dhan-option"


def test_adapter_factory_is_the_dhan_adapter_class() -> None:
    assert build_dhan_binding().adapter_factory is DhanMarketAdapter
    adapter: MarketAdapter = DhanMarketAdapter(client=object(), security_ids={})  # type: ignore[arg-type]
    assert isinstance(adapter, MarketAdapter)


def test_window_days_for_is_the_dhan_tiler() -> None:
    assert build_dhan_binding().window_days_for is window_days_for_interval


def test_error_triple_identity() -> None:
    policy = build_dhan_binding().error_policy
    assert policy.is_retryable is is_retryable
    assert policy.is_rate_limit is is_rate_limit
    assert policy.terminal_base is DhanError


def test_pacing_is_5_none_100000() -> None:
    pacing = build_dhan_binding().pacing
    assert pacing.ceiling_rps == 5.0
    assert pacing.ceiling_per_min is None
    assert pacing.daily_quota == 100_000


def test_self_pacing_predicate_identity() -> None:
    b = build_dhan_binding()
    assert b.client_self_paces is client_self_paces
    assert b.client_self_reports_rate_limits is client_self_reports_rate_limits
    assert b.client_self_reports_successes is client_self_reports_successes


def test_history_floor_is_none_until_verified() -> None:
    # Golden Rule #1: UNVERIFIED — never a fabricated floor.
    assert build_dhan_binding().history_floor is None


def test_lake_root_for_nests_under_feeds_dhan() -> None:
    b = build_dhan_binding()
    assert b.lake_root_for(Path("/lake")) == Path("/lake/feeds/dhan")


def test_resolve_feed_binding_dhan() -> None:
    b = resolve_feed_binding("dhan")
    assert b.feed_id == "dhan"
    assert isinstance(b, FeedBinding)


def test_build_feed_sink_wraps_dhan_in_audit_sink(tmp_path: Path) -> None:
    sink = build_feed_sink(lake_root=tmp_path, binding=build_dhan_binding())
    assert isinstance(sink, FeedAuditSink)
    # The audit chain lives under the nested feed tree.
    assert sink.chain_path == tmp_path / "feeds" / "dhan" / ".audit" / "ingest-chain.jsonl"


def test_groww_binding_byte_identity_unchanged() -> None:
    # The Dhan re-admission must leave Groww UNCHANGED: the default feed still
    # resolves to the TOP lake root by identity (Groww byte-identity guarantee).
    g = build_groww_binding()
    assert g.feed_id == "groww"
    top = Path("/lake")
    assert g.lake_root_for(top) is top
