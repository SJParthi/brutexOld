"""Unit tests for the options expiry-selector normaliser (DEC-MOVERS-O1-LATENCY-001).

:func:`brutex.dashboard.movers_options.normalize_options_selectors` is the single
source of truth that maps the raw ``?expiry=`` / ``?track=`` query strings to the
canonical ``(tier, track)`` tokens the cache key AND the computed candidate set
agree on — so every blank / unknown / mixed-case selector keys deterministically.
"""

from __future__ import annotations

import pytest
from brutex.dashboard.movers_options import normalize_options_selectors


@pytest.mark.parametrize(
    ("raw_tier", "raw_track", "expected"),
    [
        (None, None, ("near", "all")),
        ("", "", ("near", "all")),
        ("Near", "All", ("near", "all")),
        ("NEXT", "Monthly", ("next", "monthly")),
        ("far", "weekly", ("far", "weekly")),
        # An unknown track falls back to ``all`` (so it keys identically to default).
        ("near", "fortnightly", ("near", "all")),
        # An unknown tier is passed through lower-cased — the resolver maps any
        # unknown tier to the ``near`` index, so it ranks honestly, never crashes.
        ("bogus", "monthly", ("bogus", "monthly")),
    ],
)
def test_normalize(raw_tier: str | None, raw_track: str | None, expected: tuple[str, str]) -> None:
    assert normalize_options_selectors(raw_tier, raw_track) == expected
