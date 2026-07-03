"""Unit tests for ``brutex.adapters.groww.adapter`` (DEC-DHAN-GROWW-REMIGRATE-001).

Composes a fake SDK + a small real instrument index — no live API.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

import pytest
from brutex.adapters.groww.adapter import GROWW_CAPABILITIES, GrowwMarketAdapter
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.constants import INSTRUMENT_CSV_COLUMNS
from brutex.adapters.groww.instruments import load_index_from_csv
from brutex.adapters.protocol import MarketAdapter
from brutex.models import Exchange, OutOfScopeError

_HEADER = ",".join(INSTRUMENT_CSV_COLUMNS)


def _row(**over: str) -> str:
    cells = dict.fromkeys(INSTRUMENT_CSV_COLUMNS, "")
    cells.update(over)
    return ",".join(cells[c] for c in INSTRUMENT_CSV_COLUMNS)


_NIFTY = _row(
    exchange="NSE",
    exchange_token="NIFTY",
    trading_symbol="NIFTY",
    groww_symbol="NSE-NIFTY",
    name="NIFTY 50",
    instrument_type="IDX",
    segment="CASH",
)
_FUT = _row(
    exchange="NSE",
    exchange_token="53001",
    trading_symbol="NIFTY25Aug26FUT",
    groww_symbol="NSE-NIFTY-25Aug26-FUT",
    instrument_type="FUT",
    segment="FNO",
    underlying_symbol="NIFTY",
    underlying_exchange_token="26000",
    expiry_date="2026-08-25",
    lot_size="65",
    tick_size="0.05",
)
_INDEX = load_index_from_csv("\n".join([_HEADER, _NIFTY, _FUT]) + "\n")


class _FakeSDK:
    def __init__(self, *, expiries=None, contracts=None) -> None:
        self.calls = 0
        self.last_kwargs: dict[str, object] = {}
        self.expiry_years: list[object] = []
        self._expiries = expiries or (lambda _kw: {})
        self._contracts = contracts or (lambda _kw: {})

    def get_historical_candles(self, **kwargs: object) -> Mapping[str, object]:
        self.calls += 1
        self.last_kwargs = kwargs
        return {"candles": [["2024-01-01T09:15:00", 100.0, 110.0, 90.0, 105.0, 999, None]]}

    def get_expiries(self, **kw: object) -> Mapping[str, object]:
        self.expiry_years.append(kw.get("year"))
        return self._expiries(kw)

    def get_contracts(self, **kw: object) -> Mapping[str, object]:
        return self._contracts(kw)


def _adapter() -> tuple[GrowwMarketAdapter, _FakeSDK]:
    sdk = _FakeSDK()
    return GrowwMarketAdapter(client=GrowwClient(sdk=sdk, sleep=lambda _s: None), index=_INDEX), sdk


def test_is_a_market_adapter() -> None:
    adapter, _ = _adapter()
    assert isinstance(adapter, MarketAdapter)


def test_capabilities_spot_scope() -> None:
    caps = _adapter()[0].capabilities
    assert caps is GROWW_CAPABILITIES
    assert caps.has_derivatives is False
    assert caps.supports_intraday is True
    assert caps.exchanges == frozenset({Exchange.NSE, Exchange.BSE})
    assert "/" not in caps.market_id
    assert caps.rate_limit_rps > 0


def test_list_instruments_one_page_then_exhausted() -> None:
    adapter, _ = _adapter()
    page = adapter.list_instruments()
    assert len(page.instruments) == 2
    assert page.next_cursor is None
    # any non-None cursor is past the single page
    assert adapter.list_instruments(cursor="x").instruments == ()


def test_list_derivatives_empty_when_vendor_lists_none() -> None:
    # An empty-expiries SDK yields no contracts (the brute-force spot scope keeps
    # has_derivatives=False; this enumerator is the LAKE/reference-only surface).
    adapter, _ = _adapter()
    assert list(adapter.list_derivatives(underlying_symbol="NIFTY", exchange=Exchange.NSE)) == []


def test_capabilities_has_derivatives_false_engine_scope() -> None:
    # The flag describes the brute-force ENGINE scope (spot-only), NOT the Groww
    # market — list_derivatives can still enumerate the lake chain (CLAUDE.md §3).
    assert GROWW_CAPABILITIES.has_derivatives is False


def test_list_derivatives_enumerates_lake_chain() -> None:
    sdk = _FakeSDK(
        expiries=lambda kw: {"expiries": ["2024-01-25"]} if kw.get("year") == 2024 else {},
        contracts=lambda _kw: {"contracts": ["NSE-NIFTY-25Jan24-100-CE", "NSE-NIFTY-25Jan24-FUT"]},
    )
    adapter = GrowwMarketAdapter(client=GrowwClient(sdk=sdk, sleep=lambda _s: None), index=_INDEX)
    chain = list(adapter.list_derivatives(underlying_symbol="NIFTY", exchange=Exchange.NSE))
    assert {c.symbol for c in chain} == {"NSE-NIFTY-25Jan24-100-CE", "NSE-NIFTY-25Jan24-FUT"}
    assert {c.contract_type for c in chain} == {"CE", "FUT"}
    # one get_expiries call per historical year (EARLIEST_HISTORY .. current year)
    assert 2024 in sdk.expiry_years and len(sdk.expiry_years) >= 5


def test_fetch_ohlcv_index_sends_master_groww_symbol_and_drops_volume() -> None:
    adapter, sdk = _adapter()
    bars = adapter.fetch_ohlcv(
        symbol="NIFTY",
        exchange=Exchange.NSE,
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
    )
    assert len(bars) == 1
    assert bars[0].volume == 0  # index → volume dropped
    # The master groww_symbol resolved from the bare ticker (DEC-GROWW-HYPHEN-SYMBOL-
    # RESOLVE-001 — sent verbatim, NOT reconstructed; == "NSE-NIFTY" here).
    assert sdk.last_kwargs["groww_symbol"] == "NSE-NIFTY"
    assert sdk.last_kwargs["segment"] == "CASH"
    assert sdk.last_kwargs["candle_interval"] == "1minute"


def test_fetch_ohlcv_future_full_symbol_fno_segment_keeps_volume() -> None:
    adapter, sdk = _adapter()
    bars = adapter.fetch_ohlcv(
        symbol="NSE-NIFTY-25Aug26-FUT",
        exchange=Exchange.NSE,
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
    )
    assert bars[0].volume == 999  # non-index → volume kept
    assert sdk.last_kwargs["segment"] == "FNO"
    assert sdk.last_kwargs["groww_symbol"] == "NSE-NIFTY-25Aug26-FUT"  # used as-is


def test_fetch_ohlcv_tiles_multiple_windows() -> None:
    adapter, sdk = _adapter()
    bars = adapter.fetch_ohlcv(
        symbol="NIFTY",
        exchange=Exchange.NSE,
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 3, 16),  # 75 days → 3 × 30-day windows
    )
    assert sdk.calls == 3
    assert len(bars) == 3  # one bar per window payload


def test_fetch_ohlcv_unknown_symbol_raises() -> None:
    adapter, _ = _adapter()
    with pytest.raises(OutOfScopeError, match="not in Groww universe"):
        adapter.fetch_ohlcv(
            symbol="UNKNOWN",
            exchange=Exchange.NSE,
            interval="1minute",
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
        )


def test_fetch_ohlcv_bad_interval_raises() -> None:
    adapter, _ = _adapter()
    with pytest.raises(ValueError, match="unsupported Groww interval"):
        adapter.fetch_ohlcv(
            symbol="NIFTY",
            exchange=Exchange.NSE,
            interval="7minute",
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
        )


# ── DEC-GROWW-HYPHEN-SYMBOL-RESOLVE-001 — hyphenated single-stock resolution ──

_BAJAJ = _row(
    exchange="NSE",
    exchange_token="16669",
    trading_symbol="BAJAJ-AUTO",
    groww_symbol="NSE-BAJAJ-AUTO",
    name="Bajaj Auto Ltd",
    instrument_type="EQ",
    segment="CASH",
    lot_size="1",
    tick_size="0.05",
)
_MM = _row(
    exchange="NSE",
    exchange_token="2031",
    trading_symbol="M&M",
    groww_symbol="NSE-M&M",
    name="Mahindra & Mahindra Ltd",
    instrument_type="EQ",
    segment="CASH",
    lot_size="1",
    tick_size="0.05",
)
# A VERBOSE-symbol index: trading_symbol ("NIFTY MID SELECT") ≠ the MIDCPNIFTY
# component of its groww_symbol (NSE-MIDCPNIFTY). Reconstructing EXCH-trading_symbol
# would send the garbage "NSE-NIFTY MID SELECT"; the master groww_symbol is correct.
_MIDCP = _row(
    exchange="NSE",
    exchange_token="26074",
    trading_symbol="NIFTY MID SELECT",
    groww_symbol="NSE-MIDCPNIFTY",
    name="Nifty Midcap Select",
    instrument_type="IDX",
    segment="CASH",
)
_HYPHEN_INDEX = load_index_from_csv("\n".join([_HEADER, _NIFTY, _FUT, _BAJAJ, _MM, _MIDCP]) + "\n")


def _hyphen_adapter() -> tuple[GrowwMarketAdapter, _FakeSDK]:
    sdk = _FakeSDK()
    return (
        GrowwMarketAdapter(client=GrowwClient(sdk=sdk, sleep=lambda _s: None), index=_HYPHEN_INDEX),
        sdk,
    )


def test_fetch_ohlcv_hyphenated_stock_resolves_to_prefixed_groww_symbol() -> None:
    """``BAJAJ-AUTO`` (bare, hyphenated) resolves to ``NSE-BAJAJ-AUTO`` — the regression.

    The prior ``"-" in symbol`` heuristic looked the bare ticker up in
    ``by_groww_symbol``, missed, and raised; the master-authoritative
    ``(exchange, trading_symbol)`` lookup resolves it and sends the master's REAL
    groww_symbol.
    """
    adapter, sdk = _hyphen_adapter()
    bars = adapter.fetch_ohlcv(
        symbol="BAJAJ-AUTO",
        exchange=Exchange.NSE,
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
    )
    assert bars  # data came back (no OutOfScopeError)
    assert sdk.last_kwargs["groww_symbol"] == "NSE-BAJAJ-AUTO"
    assert sdk.last_kwargs["segment"] == "CASH"  # EQ → CASH, volume kept


def test_fetch_ohlcv_ampersand_stock_resolves() -> None:
    """``M&M`` (ampersand) resolves to ``NSE-M&M`` — guards the already-working & path."""
    adapter, sdk = _hyphen_adapter()
    adapter.fetch_ohlcv(
        symbol="M&M",
        exchange=Exchange.NSE,
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
    )
    assert sdk.last_kwargs["groww_symbol"] == "NSE-M&M"


def test_fetch_ohlcv_ampersand_spot_clean_empty_falls_back_to_encoded_form() -> None:
    """The SPOT path rides the shared `%26` candle fallback (DEC-GROWW-AMPERSAND-CANDLE-FALLBACK-001).

    ``M&M`` resolves fine, but the plain-``&`` candle fetch returns a clean-empty 200
    (the un-encoded query-string-separator miss); the shared
    :func:`fetch_candle_windows` seam retries the ``%26``-encoded form ONCE and the
    recovered bars flow back through ``fetch_ohlcv`` — no adapter-level change needed.
    """

    class _EmptyPlainSDK:
        def __init__(self) -> None:
            self.symbols: list[str] = []

        def get_historical_candles(self, **kw: object) -> Mapping[str, object]:
            symbol = str(kw["groww_symbol"])
            self.symbols.append(symbol)
            if "%26" in symbol:
                return {"candles": [["2024-01-01T09:15:00", 100.0, 110.0, 90.0, 105.0, 9, None]]}
            return {"candles": []}

        def get_expiries(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
            raise AssertionError("unused")

        def get_contracts(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
            raise AssertionError("unused")

    sdk = _EmptyPlainSDK()
    adapter = GrowwMarketAdapter(
        client=GrowwClient(sdk=sdk, sleep=lambda _s: None), index=_HYPHEN_INDEX
    )
    bars = adapter.fetch_ohlcv(
        symbol="M&M",
        exchange=Exchange.NSE,
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
    )
    assert len(bars) == 1  # the encoded form's bars surfaced through the spot path
    assert sdk.symbols == ["NSE-M&M", "NSE-M%26M"]  # plain first, one encoded retry


def test_fetch_ohlcv_prefixed_contract_still_used_as_is() -> None:
    """An already-prefixed contract groww_symbol resolves via by_groww_symbol, AS-IS.

    The future's trading_symbol is ``NIFTY25Aug26FUT`` (no hyphens), so the full
    ``NSE-NIFTY-25Aug26-FUT`` misses the (exchange, trading_symbol) index and falls
    through to by_groww_symbol — used verbatim (the FNO segment, volume kept).
    """
    adapter, sdk = _hyphen_adapter()
    adapter.fetch_ohlcv(
        symbol="NSE-NIFTY-25Aug26-FUT",
        exchange=Exchange.NSE,
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
    )
    assert sdk.last_kwargs["groww_symbol"] == "NSE-NIFTY-25Aug26-FUT"
    assert sdk.last_kwargs["segment"] == "FNO"


def test_fetch_ohlcv_genuinely_unknown_hyphenated_symbol_raises() -> None:
    """A hyphenated ticker NOT in the master raises OutOfScopeError naming the symbol."""
    adapter, _sdk = _hyphen_adapter()
    with pytest.raises(OutOfScopeError, match="not in Groww universe: NOPE-X"):
        adapter.fetch_ohlcv(
            symbol="NOPE-X",
            exchange=Exchange.NSE,
            interval="1day",
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
        )


def test_fetch_ohlcv_verbose_symbol_index_sends_master_groww_symbol() -> None:
    """A verbose-symbol index sends the master's REAL groww_symbol, NOT a reconstruction.

    ``MIDCPNIFTY``'s trading_symbol is ``"NIFTY MID SELECT"`` but its groww_symbol is
    ``NSE-MIDCPNIFTY``. Reconstructing ``f"{exchange}-{symbol}"`` would send the garbage
    ``"NSE-NIFTY MID SELECT"`` (a silent wrong fetch); the master ``groww_symbol_by_pair``
    sends ``NSE-MIDCPNIFTY``. This is the hostile-review BLOCKER regression.
    """
    adapter, sdk = _hyphen_adapter()
    adapter.fetch_ohlcv(
        symbol="NIFTY MID SELECT",
        exchange=Exchange.NSE,
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
    )
    assert sdk.last_kwargs["groww_symbol"] == "NSE-MIDCPNIFTY"
    assert sdk.last_kwargs["segment"] == "CASH"  # IDX → CASH
