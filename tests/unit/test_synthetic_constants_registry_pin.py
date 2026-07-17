"""Synthetic INTERVAL_TO_MINUTES registry-derivation pin (DEC-TF-REGISTRY-DYNAMIC-001)."""

from __future__ import annotations

from brutex.adapters.synthetic.constants import INTERVAL_TO_MINUTES, SUPPORTED_INTERVALS

_EXPECTED = {
    "1minute": 1,
    "5minute": 5,
    "15minute": 15,
    "1hour": 60,
    "1day": 375,
}


def test_interval_to_minutes_exact_literal_values() -> None:
    assert dict(INTERVAL_TO_MINUTES) == _EXPECTED


def test_supported_intervals_unchanged() -> None:
    assert frozenset(_EXPECTED) == SUPPORTED_INTERVALS
