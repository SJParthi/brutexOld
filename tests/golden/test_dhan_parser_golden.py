"""Golden tests — byte-stable Dhan parsed output + the capability-matrix pin.

DEC-DHAN-ADAPTER-001 (the 7-test-types golden leg). Pins the EXACT parsed
representation of a fixed candle / rolling-option payload (Decimal string forms +
UTC stamps — bit-exact ``NFR-DET-001`` reproducibility) and the capability matrix's
verified/unverified split, so any silent drift in the pure parse law or the honesty
record fails loudly. Also the ``__init__`` public-surface pin (import smoke).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from brutex.adapters import dhan
from brutex.adapters.dhan.capability_matrix import (
    DHAN_CAPABILITY_MATRIX,
    DHAN_UNVERIFIED_PROBE_GATES,
)
from brutex.adapters.dhan.parser import parse_candle_columns, parse_rolling_option

#: A FIXED candle payload (epoch 2023-11-14T22:13:20Z + 60s) — never regenerate.
_CANDLE_PAYLOAD: dict[str, object] = {
    "open": [100.5, 101.25],
    "high": [102.0, 103.5],
    "low": [99.75, 100.0],
    "close": [101.0, 102.75],
    "volume": [1500, 2500],
    "timestamp": [1_700_000_000, 1_700_000_060],
    "open_interest": [7000, 7100],
}

#: The frozen golden of the parsed candle rows (str forms — Decimal exactness).
_CANDLE_GOLDEN: list[dict[str, str | int | None]] = [
    {
        "timestamp": "2023-11-14T22:13:20+00:00",
        "open": "100.5",
        "high": "102.0",
        "low": "99.75",
        "close": "101.0",
        "volume": 1500,
        "open_interest": 7000,
    },
    {
        "timestamp": "2023-11-14T22:14:20+00:00",
        "open": "101.25",
        "high": "103.5",
        "low": "100.0",
        "close": "102.75",
        "volume": 2500,
        "open_interest": 7100,
    },
]


def test_candle_parse_golden_byte_stable() -> None:
    bars = parse_candle_columns(_CANDLE_PAYLOAD)
    got = [
        {
            "timestamp": bar.timestamp.isoformat(),
            "open": str(bar.open),
            "high": str(bar.high),
            "low": str(bar.low),
            "close": str(bar.close),
            "volume": bar.volume,
            "open_interest": bar.open_interest,
        }
        for bar in bars
    ]
    assert got == _CANDLE_GOLDEN
    # canonical-JSON byte stability (sorted keys, tight separators)
    rendered = json.dumps(got, sort_keys=True, separators=(",", ":"))
    assert json.dumps(_CANDLE_GOLDEN, sort_keys=True, separators=(",", ":")) == rendered


def test_rolling_option_parse_golden() -> None:
    payload = {
        "ce": {
            "open": [10.5],
            "high": [11.0],
            "low": [10.0],
            "close": [10.75],
            "volume": [500],
            "timestamp": [1_700_000_000],
            "oi": [3000],
            "strike": [20000.0],
            "spot": [20050.5],
            "iv": [0.1425],
        }
    }
    bars = parse_rolling_option(payload)
    assert len(bars) == 1
    bar = bars[0]
    assert bar.side == "CE"
    assert bar.timestamp == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)
    assert str(bar.open) == "10.5"
    assert str(bar.strike) == "20000.0"
    assert str(bar.spot) == "20050.5"
    assert str(bar.iv) == "0.1425"
    assert bar.open_interest == 3000


def test_capability_matrix_golden_pin() -> None:
    """The honest capability record is FROZEN — a drift is a conscious re-pin."""
    pinned = {
        "historical_daily": ("daily max-range-per-call (no documented cap)",),
        "intraday": ("candle price unit (rupees-vs-paisa)",),
        "option_chain": ("exact per-strike Greek field names",),
        "expiry_list": (),
        "rolling_option": ("expired-futures history existence (undocumented — no path provided)",),
        "renew_token": (),
        "validate_token": (),
        "scrip_master": ("exact SEM_-prefixed column names + order",),
    }
    got = {cap.name: cap.unverified for cap in DHAN_CAPABILITY_MATRIX}
    assert got == pinned
    assert len(DHAN_UNVERIFIED_PROBE_GATES) == 7


def test_package_public_surface() -> None:
    """Import smoke + the public ``__all__`` pin (drives ``__init__`` to 100%)."""
    for name in dhan.__all__:
        assert getattr(dhan, name) is not None
    assert "DhanMarketAdapter" in dhan.__all__
    assert "DHAN_UNVERIFIED_PROBE_GATES" in dhan.__all__
