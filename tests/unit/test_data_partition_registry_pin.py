"""VALID_INTERVALS registry-derivation pin (DEC-TF-REGISTRY-DYNAMIC-001)."""

from __future__ import annotations

from brutex.data.partition import VALID_INTERVALS

_EXPECTED = frozenset(
    {
        "1minute",
        "2minute",
        "3minute",
        "4minute",
        "5minute",
        "6minute",
        "7minute",
        "8minute",
        "9minute",
        "10minute",
        "11minute",
        "12minute",
        "13minute",
        "14minute",
        "15minute",
        "30minute",
        "1hour",
        "2hour",
        "3hour",
        "4hour",
        "1day",
        "1week",
        "1month",
    }
)


def test_valid_intervals_exact_23_member_set() -> None:
    """The registry derivation serves the EXACT pre-registry 23-member set."""
    assert VALID_INTERVALS == _EXPECTED
