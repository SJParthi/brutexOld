"""Shared Groww candle fetch — tile a range, fetch each window, parse, concat.

DEC-GROWW-OPTION-INGEST-001. The single low-level tiling-fetch primitive BOTH
candle paths call (CLAUDE.md §11 COMMON — one source of truth, no copy-paste):

* the **spot** adapter (:meth:`~brutex.adapters.groww.adapter.GrowwMarketAdapter.fetch_ohlcv`)
  — index-RESOLVED: it looks the symbol up in the
  :class:`~brutex.adapters.groww.instruments.InstrumentIndex` first, then hands
  the resolved ``(segment, groww_symbol, drop_volume)`` here;
* the **option** path
  (:func:`~brutex.adapters.groww.option_ingest.fetch_option_candles`)
  — index-FREE: the ~89k FUT/CE/PE legs are DISCARDED from the lean universe
  (DEC-INSTRUMENT-UNIVERSE-FAST-001), so an expired contract is never in the
  index; the option path resolves contracts via ``get_expiries`` /
  ``get_contracts`` and fetches each one HERE by its full ``groww_symbol`` with
  ``segment="FNO"`` (:data:`~brutex.adapters.groww.constants.OPTION_SEGMENT`).

Pure orchestration over injected leaves (the :class:`~brutex.adapters.groww.client.GrowwClient`
+ the pure :func:`~brutex.adapters.groww.parser.parse_candle_rows`), so every
branch is unit-testable with a fake client — no ``growwapi``, no network. O(1)
per emitted window (the loop body is one client call + one parse); the TOTAL is
O(windows) which is network-bound, NOT an O(1)-total claim (Golden Rule #10).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime, time

from brutex.adapters.groww.chunks import plan_candle_windows
from brutex.adapters.groww.client import GrowwClientProtocol
from brutex.adapters.groww.constants import INTERVAL_TO_GROWW
from brutex.adapters.groww.parser import parse_candle_rows
from brutex.data.models import CandleBar

#: Raw-payload capture hook (DEC-RAW-DATA-INTEGRITY-001). Fired ONCE per fetched
#: window with the RAW vendor payload (the row-of-arrays mapping, BEFORE any
#: parse/transform) plus the exact ``(groww_interval, window_start, window_end)``
#: strings the call was issued with — the coordinates a verify re-fetch needs to
#: re-issue the byte-identical call. The hash + sidecar write happen INSIDE the
#: caller's hook (best-effort, guarded), so the integrity layer is purely
#: additive: ``None`` (the default — CLI / tests / non-instrumented paths) makes
#: this a no-op and the fetch byte-identical to the pre-feature behaviour.
RawPayloadHook = Callable[[Mapping[str, object], str, str, str], None]


def fetch_candle_windows(
    client: GrowwClientProtocol,
    *,
    exchange: str,
    segment: str,
    groww_symbol: str,
    interval: str,
    start: date,
    end: date,
    drop_volume: bool,
    on_raw_payload: RawPayloadHook | None = None,
) -> list[CandleBar]:
    """Fetch + parse every Groww candle window for ``[start, end)`` at *interval*.

    Tiles the range into Groww-window-sized chunks for *interval*'s tier
    (:func:`~brutex.adapters.groww.chunks.plan_candle_windows` — 30/90/180-day),
    fetches each via :meth:`GrowwClient.historical_candles`, parses each payload
    with :func:`~brutex.adapters.groww.parser.parse_candle_rows`, and
    concatenates in window order.

    Parameters
    ----------
    client
        The injected :class:`~brutex.adapters.groww.client.GrowwClient` (the only
        network surface; its ``pace`` hook is the rate gate — DEC-DHAN-RATE-SINGLE-BUCKET-001).
    exchange
        The Groww exchange string (``"NSE"`` / ``"BSE"``).
    segment
        The Groww segment string (``"CASH"`` for spot equity, ``"INDEX"`` is mapped
        to the neutral spot row, ``"FNO"`` for derivatives — the caller decides).
    groww_symbol
        The full Groww symbol fed verbatim to the SDK (e.g. ``"NSE-NIFTY"`` for an
        index, ``"NSE-NIFTY-02Jan25-28500-PE"`` for an option contract).
    interval
        A BruteX canonical interval (must be a key of
        :data:`~brutex.adapters.groww.constants.INTERVAL_TO_GROWW`).
    start, end
        Half-open date range. ``start >= end`` yields no windows (empty list).
    drop_volume
        Passed through to :func:`parse_candle_rows`: ``True`` zeroes per-bar
        volume (an INDEX carries no traded volume — DEC-VENDOR-VOLUME-NORMALIZE-001);
        ``False`` keeps it (equities / FUT / CE / PE all trade — 03-backtesting.md:141-143).
    on_raw_payload
        Optional :data:`RawPayloadHook` fired ONCE per fetched window with the
        RAW vendor payload (BEFORE :func:`parse_candle_rows`) + the exact
        ``(groww_interval, window_start, window_end)`` the call was issued with —
        the raw-data integrity capture seam (DEC-RAW-DATA-INTEGRITY-001). ``None``
        (the default) makes the fetch byte-identical to the pre-feature path; the
        hash + sidecar persist live inside the caller's (best-effort) hook, so a
        hook failure is the CALLER's to absorb — the candle fetch itself is never
        affected by the capture.

    Returns
    -------
    list[CandleBar]
        Parsed bars across all windows, in input order. No-trade bars are dropped
        by the parser, so the result may be shorter than the raw candle count.

    Raises
    ------
    ValueError
        If *interval* is not a supported Groww interval (fail-loud, Golden Rule #1).
    """
    if interval not in INTERVAL_TO_GROWW:
        raise ValueError(f"unsupported Groww interval: {interval!r}")
    groww_interval = INTERVAL_TO_GROWW[interval]
    windows = plan_candle_windows(
        datetime.combine(start, time()), datetime.combine(end, time()), interval=interval
    )
    bars: list[CandleBar] = []
    for window in windows:
        payload = client.historical_candles(
            exchange=exchange,
            segment=segment,
            groww_symbol=groww_symbol,
            interval=groww_interval,
            start=window.start,
            end=window.end,
        )
        # Raw-data integrity capture (DEC-RAW-DATA-INTEGRITY-001): hand the RAW
        # payload + the exact issued coordinates to the hook BEFORE any parse, so
        # the recorded hash attests EXACTLY what the vendor sent. Purely additive
        # — a no-op when no hook is wired.
        if on_raw_payload is not None:
            on_raw_payload(payload, groww_interval, window.start, window.end)
        bars.extend(parse_candle_rows(payload, drop_volume=drop_volume))
    return bars


__all__ = ["RawPayloadHook", "fetch_candle_windows"]
