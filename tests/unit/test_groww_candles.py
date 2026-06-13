"""Unit tests for ``brutex.adapters.groww.candles`` (DEC-GROWW-OPTION-INGEST-001).

100% line+branch: the shared tile → fetch → parse → concat primitive — interval
validation (fail-loud), per-tier window tiling, the ``drop_volume`` pass-through
(index vs tradable), and the empty-range short-circuit. Driven by a FAKE SDK
behind a REAL :class:`GrowwClient` — no ``growwapi``, no network.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

import pytest
from brutex.adapters.groww.candles import fetch_candle_windows
from brutex.adapters.groww.client import GrowwClient


class _FakeSDK:
    """Returns one canned candle per call; records the per-window kwargs."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def get_historical_candles(self, **kw: object) -> Mapping[str, object]:
        self.calls.append(dict(kw))
        return {"candles": [["2024-01-01T09:15:00", 100.0, 110.0, 90.0, 105.0, 777, 42]]}

    def get_expiries(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        raise AssertionError("unused")

    def get_contracts(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        raise AssertionError("unused")


def _client() -> tuple[GrowwClient, _FakeSDK]:
    sdk = _FakeSDK()
    return GrowwClient(sdk=sdk, sleep=lambda _s: None), sdk


def test_tiles_range_and_concatenates() -> None:
    client, sdk = _client()
    bars = fetch_candle_windows(
        client,
        exchange="NSE",
        segment="FNO",
        groww_symbol="NSE-NIFTY-02Jan25-28500-PE",
        interval="1minute",  # 30-day tier
        start=date(2024, 1, 1),
        end=date(2024, 3, 16),  # 75 days → 3 windows
        drop_volume=False,
    )
    assert len(bars) == 3 and sdk.calls == sdk.calls[:3]
    assert len(sdk.calls) == 3
    # the contract symbol + FNO segment are forwarded verbatim
    assert sdk.calls[0]["groww_symbol"] == "NSE-NIFTY-02Jan25-28500-PE"
    assert sdk.calls[0]["segment"] == "FNO"
    assert sdk.calls[0]["candle_interval"] == "1minute"


def test_keeps_volume_and_open_interest_for_tradable() -> None:
    client, _ = _client()
    bars = fetch_candle_windows(
        client,
        exchange="NSE",
        segment="FNO",
        groww_symbol="NSE-NIFTY-02Jan25-28500-CE",
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        drop_volume=False,
    )
    assert bars[0].volume == 777
    assert bars[0].open_interest == 42


def test_drop_volume_zeroes_volume_for_index() -> None:
    client, _ = _client()
    bars = fetch_candle_windows(
        client,
        exchange="NSE",
        segment="CASH",
        groww_symbol="NSE-NIFTY",
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        drop_volume=True,
    )
    assert bars[0].volume == 0


def test_bad_interval_raises() -> None:
    client, _ = _client()
    with pytest.raises(ValueError, match="unsupported Groww interval"):
        fetch_candle_windows(
            client,
            exchange="NSE",
            segment="FNO",
            groww_symbol="NSE-NIFTY-02Jan25-28500-PE",
            interval="7minute",
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
            drop_volume=False,
        )


def test_empty_range_fetches_nothing() -> None:
    client, sdk = _client()
    bars = fetch_candle_windows(
        client,
        exchange="NSE",
        segment="FNO",
        groww_symbol="NSE-NIFTY-02Jan25-28500-PE",
        interval="1minute",
        start=date(2024, 1, 2),
        end=date(2024, 1, 1),  # start >= end → no windows
        drop_volume=False,
    )
    assert bars == [] and sdk.calls == []
