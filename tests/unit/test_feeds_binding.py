"""Unit tests for :mod:`brutex.feeds.binding` (DEC-FEED-INTERFACE-001, #1782).

100% line + branch. Covers the frozen dataclasses' ``__post_init__`` refusal
matrix and the ONE sanctioned sink constructor — proving a ``feedx`` write lands
under ``feeds/feedx/bars/...`` (wrapped in a ``FeedAuditSink``) and NOT under the
top ``bars/`` (the mis-construction killer), while the default feed returns the
plain sink at the top root.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from brutex.data.feed_audit import FeedAuditSink
from brutex.data.feed_root import feed_lake_root
from brutex.data.models import CandleBar
from brutex.data.sink import ParquetBarSink
from brutex.feeds.binding import (
    FeedBinding,
    FeedErrorPolicy,
    FeedPacing,
    build_feed_sink,
)
from brutex.feeds.groww_binding import build_groww_binding


def _retry(_exc: object) -> bool:
    return False


def _rate(_exc: object) -> bool:
    return False


def _self_paces(_client: object) -> bool:
    return False


def _valid_policy() -> FeedErrorPolicy:
    return FeedErrorPolicy(is_retryable=_retry, is_rate_limit=_rate, terminal_base=ValueError)


def _valid_pacing() -> FeedPacing:
    return FeedPacing(ceiling_rps=5.0, ceiling_per_min=300, daily_quota=None)


def _binding_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "feed_id": "feedx",
        "ledger_market": "feedx",
        "ledger_market_future": "feedx-future",
        "ledger_market_option": "feedx-option",
        "adapter_factory": object,
        "window_days_for": lambda _interval: 30,
        "error_policy": _valid_policy(),
        "pacing": _valid_pacing(),
        "client_self_paces": _self_paces,
        "client_self_reports_rate_limits": _self_paces,
        "client_self_reports_successes": _self_paces,
        "history_floor": date(2020, 1, 1),
        "lake_root_for": partial(feed_lake_root, feed="feedx"),
    }
    base.update(overrides)
    return base


def _bar(minute: int, *, close: str = "100.00") -> CandleBar:
    return CandleBar(
        timestamp=datetime(2024, 1, 4, 4, minute, tzinfo=UTC),
        open=Decimal("100.00"),
        high=Decimal("100.00"),
        low=Decimal("100.00"),
        close=Decimal(close),
        volume=1,
        open_interest=None,
    )


class TestFeedErrorPolicy:
    def test_valid(self) -> None:
        p = _valid_policy()
        assert p.terminal_base is ValueError

    def test_is_retryable_not_callable(self) -> None:
        with pytest.raises(ValueError, match="is_retryable must be callable"):
            FeedErrorPolicy(is_retryable="x", is_rate_limit=_rate, terminal_base=ValueError)  # type: ignore[arg-type]

    def test_is_rate_limit_not_callable(self) -> None:
        with pytest.raises(ValueError, match="is_rate_limit must be callable"):
            FeedErrorPolicy(is_retryable=_retry, is_rate_limit=3, terminal_base=ValueError)  # type: ignore[arg-type]

    def test_terminal_base_not_exception_type(self) -> None:
        with pytest.raises(ValueError, match="BaseException subclass"):
            FeedErrorPolicy(is_retryable=_retry, is_rate_limit=_rate, terminal_base=int)  # type: ignore[arg-type]

    def test_terminal_base_not_a_type(self) -> None:
        with pytest.raises(ValueError, match="BaseException subclass"):
            FeedErrorPolicy(is_retryable=_retry, is_rate_limit=_rate, terminal_base=ValueError())  # type: ignore[arg-type]


class TestFeedPacing:
    def test_valid_with_nones(self) -> None:
        p = FeedPacing(ceiling_rps=8.0, ceiling_per_min=None, daily_quota=None)
        assert p.ceiling_per_min is None and p.daily_quota is None

    def test_valid_with_quota(self) -> None:
        assert FeedPacing(ceiling_rps=1.0, ceiling_per_min=60, daily_quota=100).daily_quota == 100

    @pytest.mark.parametrize("rps", [0.0, -1.0, float("inf"), float("nan")])
    def test_bad_ceiling_rps(self, rps: float) -> None:
        with pytest.raises(ValueError, match="ceiling_rps"):
            FeedPacing(ceiling_rps=rps, ceiling_per_min=None, daily_quota=None)

    def test_bad_ceiling_per_min(self) -> None:
        with pytest.raises(ValueError, match="ceiling_per_min"):
            FeedPacing(ceiling_rps=5.0, ceiling_per_min=0, daily_quota=None)

    def test_bad_daily_quota(self) -> None:
        with pytest.raises(ValueError, match="daily_quota"):
            FeedPacing(ceiling_rps=5.0, ceiling_per_min=None, daily_quota=-5)


class TestFeedBinding:
    def test_valid(self) -> None:
        b = FeedBinding(**_binding_kwargs())
        assert b.feed_id == "feedx"

    def test_invalid_feed_id(self) -> None:
        from brutex.data.feed_root import FeedRootError

        with pytest.raises(FeedRootError):
            FeedBinding(**_binding_kwargs(feed_id="BAD"))

    def test_ledger_market_mismatch(self) -> None:
        with pytest.raises(ValueError, match="ledger_market must equal"):
            FeedBinding(**_binding_kwargs(ledger_market="other"))

    def test_ledger_market_future_mismatch(self) -> None:
        with pytest.raises(ValueError, match="ledger_market_future"):
            FeedBinding(**_binding_kwargs(ledger_market_future="feedx-fut"))

    def test_ledger_market_option_mismatch(self) -> None:
        with pytest.raises(ValueError, match="ledger_market_option"):
            FeedBinding(**_binding_kwargs(ledger_market_option="feedx-opt"))

    def test_error_policy_wrong_type(self) -> None:
        with pytest.raises(ValueError, match="error_policy must be a FeedErrorPolicy"):
            FeedBinding(**_binding_kwargs(error_policy=object()))

    def test_pacing_wrong_type(self) -> None:
        with pytest.raises(ValueError, match="pacing must be a FeedPacing"):
            FeedBinding(**_binding_kwargs(pacing=object()))

    @pytest.mark.parametrize(
        "field",
        [
            "adapter_factory",
            "window_days_for",
            "client_self_paces",
            "client_self_reports_rate_limits",
            "client_self_reports_successes",
            "lake_root_for",
        ],
    )
    def test_non_callable_field(self, field: str) -> None:
        with pytest.raises(ValueError, match=f"{field} must be callable"):
            FeedBinding(**_binding_kwargs(**{field: 123}))

    def test_history_floor_none_ok(self) -> None:
        assert FeedBinding(**_binding_kwargs(history_floor=None)).history_floor is None

    def test_history_floor_wrong_type(self) -> None:
        with pytest.raises(ValueError, match="history_floor must be a date or None"):
            FeedBinding(**_binding_kwargs(history_floor="2020-01-01"))


class TestBuildFeedSink:
    def test_default_feed_returns_plain_sink_at_top_root(self, tmp_path: Path) -> None:
        binding = build_groww_binding()
        sink = build_feed_sink(lake_root=tmp_path, binding=binding)
        assert isinstance(sink, ParquetBarSink)
        assert not isinstance(sink, FeedAuditSink)
        sink.append(
            exchange="NSE",
            segment="INDEX",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(15)],
        )
        assert (tmp_path / "bars" / "NSE" / "INDEX" / "NSE-NIFTY").is_dir()
        assert not (tmp_path / "feeds").exists()

    def test_non_default_feed_nests_and_audits(self, tmp_path: Path) -> None:
        binding = FeedBinding(**_binding_kwargs())
        sink = build_feed_sink(lake_root=tmp_path, binding=binding, code_commit="abc123")
        assert isinstance(sink, FeedAuditSink)
        result = sink.append(
            exchange="NSE",
            segment="INDEX",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(15)],
        )
        assert result.bars_net_new == 1
        feed_bars = tmp_path / "feeds" / "feedx" / "bars" / "NSE" / "INDEX" / "NSE-NIFTY"
        assert feed_bars.is_dir()
        # NOT at the top root — feed isolation.
        assert not (tmp_path / "bars").exists()
        # Audit chain written for the non-default feed (Golden Rule #8).
        assert (tmp_path / "feeds" / "feedx" / ".audit" / "ingest-chain.jsonl").exists()
