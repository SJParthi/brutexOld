"""Tests for `brutex.adapters.groww.adapter` — GrowwMarketAdapter protocol.

100% line+branch: capabilities, list_instruments, list_derivatives (yields
nothing), fetch_ohlcv happy path (chunking, index drop_volume, multi-chunk
concatenation), OutOfScopeError for unknown symbol / unsupported interval,
and the daily vs intraday open/close time selection.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex.adapters.groww.adapter import GrowwMarketAdapter
from brutex.adapters.groww.auth import GrowwAuth
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.constants import (
    CANDLE_INTERVAL_1D,
    CANDLE_INTERVAL_1M,
    HISTORY_START,
    MARKET_OPEN_IST,
    RATE_LIMIT_INITIAL_RPS,
)
from brutex.adapters.groww.instruments import build_brute_force_registry, build_registry
from brutex.models import Exchange, OutOfScopeError

# ── helpers ───────────────────────────────────────────────────────────────

_CSV_HEADER = (
    "exchange,exchange_token,trading_symbol,groww_symbol,name,"
    "instrument_type,segment,series,isin,underlying_symbol,"
    "underlying_exchange_token,expiry_date,strike_price,lot_size,"
    "tick_size,freeze_quantity,is_reserved,buy_allowed,sell_allowed\n"
)

_RELIANCE_CSV_ROW = (
    "NSE,2885,RELIANCE,NSE-RELIANCE,Reliance Industries,"
    "EQ,CASH,EQ,INE002A01018,nan,nan,nan,nan,1,0.05,5000,0,True,True\n"
)


def _make_auth(tmp_path: Path) -> GrowwAuth:
    token_path = tmp_path / "token.json"
    token_path.write_text(json.dumps({"access_token": "tok"}))
    return GrowwAuth(
        api_key="k",
        totp_generator=lambda: "111111",
        token_path=token_path,
        transport=lambda url, h, b: (200, json.dumps({"token": "tok"}).encode()),
    )


def _candle_row(ts: str = "2024-06-03T09:15:00") -> list[Any]:
    return [ts, 22000.0, 22100.0, 21900.0, 22050.0, 1500]


def _ok_transport(candles: list[Any] | None = None) -> Any:
    """Return a transport yielding a single-page candle response."""
    payload = {"candles": candles or [_candle_row()]}

    def transport(url: str, headers: dict[str, str], timeout: int) -> tuple[int, bytes]:
        return 200, json.dumps({"payload": payload}).encode()

    return transport


def _make_adapter(
    tmp_path: Path,
    candles: list[Any] | None = None,
    csv_text: str | None = None,
) -> GrowwMarketAdapter:
    auth = _make_auth(tmp_path)
    client = GrowwClient(auth=auth, transport=_ok_transport(candles))
    registry = build_brute_force_registry() if csv_text is None else build_registry(csv_text)
    return GrowwMarketAdapter(client=client, registry=registry)


# ── capabilities ──────────────────────────────────────────────────────────


class TestCapabilities:
    def test_market_id(self, tmp_path: Path) -> None:
        adapter = _make_adapter(tmp_path)
        assert adapter.capabilities.market_id == "groww"

    def test_exchanges_contains_nse_and_bse(self, tmp_path: Path) -> None:
        adapter = _make_adapter(tmp_path)
        caps = adapter.capabilities
        assert Exchange.NSE in caps.exchanges
        assert Exchange.BSE in caps.exchanges

    def test_no_derivatives_in_track1(self, tmp_path: Path) -> None:
        adapter = _make_adapter(tmp_path)
        caps = adapter.capabilities
        assert caps.has_derivatives is False
        assert caps.has_expiries is False
        assert caps.has_option_chains is False

    def test_earliest_history_is_2020(self, tmp_path: Path) -> None:
        adapter = _make_adapter(tmp_path)
        assert adapter.capabilities.earliest_history == HISTORY_START

    def test_rate_limit_is_initial(self, tmp_path: Path) -> None:
        adapter = _make_adapter(tmp_path)
        assert adapter.capabilities.rate_limit_rps == RATE_LIMIT_INITIAL_RPS

    def test_native_timezone_ist(self, tmp_path: Path) -> None:
        adapter = _make_adapter(tmp_path)
        assert adapter.capabilities.native_timezone == "Asia/Kolkata"


# ── list_instruments ──────────────────────────────────────────────────────


class TestListInstruments:
    def test_first_page_returns_all(self, tmp_path: Path) -> None:
        adapter = _make_adapter(tmp_path)
        page = adapter.list_instruments()
        assert len(page.instruments) == 4  # brute-force registry

    def test_cursor_exhausted_returns_empty(self, tmp_path: Path) -> None:
        adapter = _make_adapter(tmp_path)
        page = adapter.list_instruments(cursor="next")
        assert page.instruments == ()
        assert page.next_cursor is None


# ── list_derivatives ──────────────────────────────────────────────────────


class TestListDerivatives:
    def test_yields_nothing(self, tmp_path: Path) -> None:
        adapter = _make_adapter(tmp_path)
        result = list(adapter.list_derivatives(underlying_symbol="NIFTY", exchange=Exchange.NSE))
        assert result == []


# ── fetch_ohlcv ───────────────────────────────────────────────────────────


class TestFetchOhlcv:
    def test_returns_candle_bars(self, tmp_path: Path) -> None:
        adapter = _make_adapter(tmp_path)
        bars = adapter.fetch_ohlcv(
            symbol="NIFTY",
            exchange=Exchange.NSE,
            interval=CANDLE_INTERVAL_1M,
            start=date(2024, 6, 3),
            end=date(2024, 6, 3),
        )
        assert len(bars) == 1
        assert bars[0].open == Decimal("22000.0")

    def test_timestamps_are_utc_aware(self, tmp_path: Path) -> None:
        adapter = _make_adapter(tmp_path)
        bars = adapter.fetch_ohlcv(
            symbol="NIFTY",
            exchange=Exchange.NSE,
            interval=CANDLE_INTERVAL_1M,
            start=date(2024, 6, 3),
            end=date(2024, 6, 3),
        )
        assert bars[0].timestamp.tzinfo is not None

    def test_unknown_symbol_raises_out_of_scope(self, tmp_path: Path) -> None:
        adapter = _make_adapter(tmp_path)
        with pytest.raises(OutOfScopeError, match="not in the Groww universe"):
            adapter.fetch_ohlcv(
                symbol="UNKNOWN_XYZ",
                exchange=Exchange.NSE,
                interval=CANDLE_INTERVAL_1M,
                start=date(2024, 6, 3),
                end=date(2024, 6, 3),
            )

    def test_unsupported_interval_raises_out_of_scope(self, tmp_path: Path) -> None:
        adapter = _make_adapter(tmp_path)
        with pytest.raises(OutOfScopeError, match="not a recognised Groww interval"):
            adapter.fetch_ohlcv(
                symbol="NIFTY",
                exchange=Exchange.NSE,
                interval="badinterval",
                start=date(2024, 6, 3),
                end=date(2024, 6, 3),
            )

    def test_index_volume_is_zero(self, tmp_path: Path) -> None:
        # NIFTY is IDX — drop_volume should zero out any vendor volume
        adapter = _make_adapter(
            tmp_path,
            candles=[
                ["2024-06-03T09:15:00", 22000.0, 22100.0, 21900.0, 22050.0, 99999],
            ],
        )
        bars = adapter.fetch_ohlcv(
            symbol="NIFTY",
            exchange=Exchange.NSE,
            interval=CANDLE_INTERVAL_1M,
            start=date(2024, 6, 3),
            end=date(2024, 6, 3),
        )
        assert bars[0].volume == 0

    def test_equity_volume_preserved(self, tmp_path: Path) -> None:
        csv = _CSV_HEADER + _RELIANCE_CSV_ROW
        adapter = _make_adapter(
            tmp_path,
            candles=[
                ["2024-06-03T09:15:00", 2500.0, 2510.0, 2490.0, 2505.0, 12345],
            ],
            csv_text=csv,
        )
        bars = adapter.fetch_ohlcv(
            symbol="RELIANCE",
            exchange=Exchange.NSE,
            interval=CANDLE_INTERVAL_1M,
            start=date(2024, 6, 3),
            end=date(2024, 6, 3),
        )
        assert bars[0].volume == 12345

    def test_multi_chunk_bars_concatenated(self, tmp_path: Path) -> None:
        # Two separate 1-bar responses for a 31-day range (2 chunks at 30d window)
        call_count = 0

        def counting_transport(
            url: str, headers: dict[str, str], timeout: int
        ) -> tuple[int, bytes]:
            nonlocal call_count
            call_count += 1
            ts = "2024-06-03T09:15:00" if call_count == 1 else "2024-07-04T09:15:00"
            payload = {"candles": [[ts, 22000.0, 22100.0, 21900.0, 22050.0, 1000]]}
            return 200, json.dumps({"payload": payload}).encode()

        auth = _make_auth(tmp_path)
        client = GrowwClient(auth=auth, transport=counting_transport)
        adapter = GrowwMarketAdapter(client=client, registry=build_brute_force_registry())
        # 31 days → 2 chunks at the 30-day window
        bars = adapter.fetch_ohlcv(
            symbol="NIFTY",
            exchange=Exchange.NSE,
            interval=CANDLE_INTERVAL_1M,
            start=date(2024, 6, 3),
            end=date(2024, 7, 4),
        )
        assert call_count == 2
        assert len(bars) == 2

    def test_exchange_arg_ignored_uses_registry_exchange(self, tmp_path: Path) -> None:
        # NIFTY is NSE; passing Exchange.BSE should not matter
        adapter = _make_adapter(tmp_path)
        bars = adapter.fetch_ohlcv(
            symbol="NIFTY",
            exchange=Exchange.BSE,  # wrong exchange — should still work
            interval=CANDLE_INTERVAL_1M,
            start=date(2024, 6, 3),
            end=date(2024, 6, 3),
        )
        assert len(bars) == 1

    def test_daily_interval_uses_full_day_times(self, tmp_path: Path) -> None:
        url_calls: list[str] = []

        def recording_transport(
            url: str, headers: dict[str, str], timeout: int
        ) -> tuple[int, bytes]:
            url_calls.append(url)
            payload = {"candles": [["2024-06-03T00:00:00", 22000.0, 22100.0, 21900.0, 22050.0, 0]]}
            return 200, json.dumps({"payload": payload}).encode()

        auth = _make_auth(tmp_path)
        client = GrowwClient(auth=auth, transport=recording_transport)
        adapter = GrowwMarketAdapter(client=client, registry=build_brute_force_registry())
        adapter.fetch_ohlcv(
            symbol="NIFTY",
            exchange=Exchange.NSE,
            interval=CANDLE_INTERVAL_1D,
            start=date(2024, 6, 3),
            end=date(2024, 6, 3),
        )
        assert any("00%3A00%3A00" in u or "00:00:00" in u for u in url_calls)

    def test_intraday_interval_uses_market_open_time(self, tmp_path: Path) -> None:
        url_calls: list[str] = []

        def recording_transport(
            url: str, headers: dict[str, str], timeout: int
        ) -> tuple[int, bytes]:
            url_calls.append(url)
            payload = {"candles": [_candle_row()]}
            return 200, json.dumps({"payload": payload}).encode()

        auth = _make_auth(tmp_path)
        client = GrowwClient(auth=auth, transport=recording_transport)
        adapter = GrowwMarketAdapter(client=client, registry=build_brute_force_registry())
        adapter.fetch_ohlcv(
            symbol="NIFTY",
            exchange=Exchange.NSE,
            interval=CANDLE_INTERVAL_1M,
            start=date(2024, 6, 3),
            end=date(2024, 6, 3),
        )
        # Market open IST in URL (URL-encoded or plain)
        open_encoded = MARKET_OPEN_IST.replace(":", "%3A")
        assert any(MARKET_OPEN_IST in u or open_encoded in u for u in url_calls)


# ── post_init index building ──────────────────────────────────────────────


class TestPostInit:
    def test_sym_index_built_from_registry(self, tmp_path: Path) -> None:
        adapter = _make_adapter(tmp_path)
        # All brute-force symbols should be in the internal index
        for sym in ("NIFTY", "BANKNIFTY", "SENSEX", "INDIAVIX"):
            gi = adapter._sym_index.get(sym)  # type: ignore[attr-defined]
            assert gi is not None

    def test_instruments_tuple_matches_registry(self, tmp_path: Path) -> None:
        adapter = _make_adapter(tmp_path)
        assert len(adapter._instruments) == len(adapter.registry.all_instruments)  # type: ignore[attr-defined]
