"""Movers completeness — Top-N default 100 + all 21 timeframes backed (DEC-MOVERS-COMPLETENESS-001).

Pins the operator-directed defaults and proves the expanded `/movers` timeframe
dropdown is REAL, not an illusion: every one of the 21 canonical Track-1 timeframes
(`brutex.data.timeframes.LAKE_INTERVAL_LABELS`) is resolvable by the movers backend
(`interval_minutes`) AND derivable from 1-minute by the resampler — so picking any of
them shows data as long as 1-minute candles exist, with NO pre-materialised partition.
"""

from __future__ import annotations

from brutex.dashboard.movers_futures import DEFAULT_FUTURES_TOP_N
from brutex.dashboard.movers_options import DEFAULT_OPTIONS_TOP_N
from brutex.dashboard.movers_snapshot import DEFAULT_TOP_N, interval_minutes
from brutex.data.timeframes import ALL_TF_MINUTES, LAKE_INTERVAL_LABELS


def test_topn_default_is_100_across_all_three_tabs() -> None:
    # Operator decree "minimum 100 always" (DEC-MOVERS-COMPLETENESS-001) — the three
    # movers tabs share the deeper default (CLAUDE.md §11 COMMON keeps them aligned).
    assert DEFAULT_TOP_N == 100
    assert DEFAULT_FUTURES_TOP_N == 100
    assert DEFAULT_OPTIONS_TOP_N == 100


def test_every_canonical_timeframe_resolves_in_the_movers_backend() -> None:
    # The 21-option dropdown is only honest if EVERY label the frontend offers is one
    # the backend can resolve to a bar-minute count + resample from 1-minute. Prove it
    # for the WHOLE canonical set (not a hardcoded subset) so the two can never drift.
    assert len(LAKE_INTERVAL_LABELS) == 21
    for minutes, label in LAKE_INTERVAL_LABELS.items():
        resolved = interval_minutes(label)
        assert resolved == minutes, f"{label!r} must resolve to {minutes} min, got {resolved}"
        # Every TF is in the resampler's supported set (derivable from 1-minute).
        assert minutes in ALL_TF_MINUTES


def test_unknown_interval_is_none_not_a_crash() -> None:
    # A label outside the 21-set is honestly None (the route maps it to an empty,
    # never-500 payload) — no silent mis-resolution (Golden Rule #1).
    assert interval_minutes("7hour") is None
    assert interval_minutes("") is None
