"""INTERVAL_SECONDS registry-derivation pin (DEC-TF-REGISTRY-DYNAMIC-001).

The derived dict must serve the EXACT pre-registry literal values —
including the legacy epoch-bucketer's 1day = 86_400 WALL-CLOCK quirk.
"""

from __future__ import annotations

from brutex.feed.resampler import INTERVAL_SECONDS

_EXPECTED = {
    "1minute": 60,
    "2minute": 120,
    "3minute": 180,
    "4minute": 240,
    "5minute": 300,
    "6minute": 360,
    "7minute": 420,
    "8minute": 480,
    "9minute": 540,
    "10minute": 600,
    "11minute": 660,
    "12minute": 720,
    "13minute": 780,
    "14minute": 840,
    "15minute": 900,
    "30minute": 1800,
    "1hour": 3600,
    "2hour": 7200,
    "3hour": 10800,
    "4hour": 14400,
    "1day": 86400,
}


def test_interval_seconds_exact_literal_values() -> None:
    assert INTERVAL_SECONDS == _EXPECTED
