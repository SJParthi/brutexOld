"""JSON (de)serialisation for :class:`~brutex.data.models.CandleBar` maps.

The ``build`` step writes per-instrument 1-minute candles to disk; the ``parity``
step reads live + backtest candle files back. Prices round-trip as **strings**
(exact :class:`~decimal.Decimal`, never float) and timestamps as ISO-8601 UTC, so
a candle written then re-read is bit-identical (``NFR-DET-001``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from brutex.data.models import CandleBar


def candle_to_dict(bar: CandleBar) -> dict[str, Any]:
    """Serialise one :class:`CandleBar` to a JSON-native dict."""
    return {
        "timestamp": bar.timestamp.isoformat(),
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": bar.volume,
        "open_interest": bar.open_interest,
    }


def candle_from_dict(payload: Mapping[str, Any]) -> CandleBar:
    """Rebuild a :class:`CandleBar` from :func:`candle_to_dict` output."""
    oi = payload["open_interest"]
    return CandleBar(
        timestamp=datetime.fromisoformat(payload["timestamp"]),
        open=Decimal(str(payload["open"])),
        high=Decimal(str(payload["high"])),
        low=Decimal(str(payload["low"])),
        close=Decimal(str(payload["close"])),
        volume=int(payload["volume"]),
        open_interest=None if oi is None else int(oi),
    )


def candles_to_json(candles: Mapping[str, Sequence[CandleBar]]) -> str:
    """Serialise ``{instrument_key: [CandleBar, ...]}`` to a JSON string."""
    payload = {
        instrument: [candle_to_dict(bar) for bar in bars] for instrument, bars in candles.items()
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def candles_from_json(text: str) -> dict[str, list[CandleBar]]:
    """Parse a :func:`candles_to_json` string back to a candle map."""
    payload = json.loads(text)
    return {
        instrument: [candle_from_dict(bar) for bar in bars] for instrument, bars in payload.items()
    }


def write_candles(path: str | Path, candles: Mapping[str, Sequence[CandleBar]]) -> None:
    """Write a candle map to *path* as JSON (parent dirs created)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(candles_to_json(candles), encoding="utf-8")


def read_candles(path: str | Path) -> dict[str, list[CandleBar]]:
    """Read a candle map written by :func:`write_candles`."""
    return candles_from_json(Path(path).read_text(encoding="utf-8"))


__all__ = [
    "candle_from_dict",
    "candle_to_dict",
    "candles_from_json",
    "candles_to_json",
    "read_candles",
    "write_candles",
]
