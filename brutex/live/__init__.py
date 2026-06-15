"""Groww LIVE tick-capture → 1-minute candle → backtest-parity tooling.

Operator-authorized **measurement** surface (DEC-GROWW-LIVE-PARITY-001): capture
the Groww live websocket feed for the brute-force spot indexes, fold the captured
ticks into 1-minute candles over the **09:15:00–15:29:00 IST** session window, and
diff those live candles against the backtest ``get_historical_candles`` 1-minute
candles — the answer to "is our live feed's 1-minute bar identical to the
historical 1-minute bar we backtest on?".

Scope guard (Golden Rule #3): this is REFERENCE/RESEARCH only — **read-only**
market data, **no order placement anywhere**, and it is NOT wired into the
brute-force engine or the 52-condition vocabulary. The live feed remains out of
the Track-1 *engine* scope; this package is a side measurement tool the operator
explicitly directed (2026-06-14).

Design (all pure + injected → unit-tested with fakes, no ``growwapi`` / network):

* :mod:`~brutex.live.store` — append-only JSONL tick log (resume-safe, idempotent).
* :mod:`~brutex.live.tick` — :class:`~brutex.live.tick.LiveTick` + snapshot flatten.
* :mod:`~brutex.live.candle_builder` — ticks → 1-minute :class:`~brutex.data.models.CandleBar`.
* :mod:`~brutex.live.parity` — live vs backtest 1-minute diff report.
* :mod:`~brutex.live.capture` — subscribe + on-tick loop (feed injected).
"""

from __future__ import annotations

from brutex.live.candle_builder import build_minute_candles
from brutex.live.capture import TickCapture
from brutex.live.parity import ParityReport, compare_candles
from brutex.live.ring import RingFlusher, RingItem, TickRing
from brutex.live.store import TickRecord, TickStore
from brutex.live.tick import LiveTick, flatten_index_snapshot, iter_ticks

__all__ = [
    "LiveTick",
    "ParityReport",
    "RingFlusher",
    "RingItem",
    "TickCapture",
    "TickRecord",
    "TickRing",
    "TickStore",
    "build_minute_candles",
    "compare_candles",
    "flatten_index_snapshot",
    "iter_ticks",
]
