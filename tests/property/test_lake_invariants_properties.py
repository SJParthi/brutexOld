"""Property tests for the lake invariant scanner core (DEC-LAKE-HEAL-001).

For ANY generated timestamp multiset (arbitrary duplicate patterns + arbitrary
foreign-month relocations around the home month), the PURE classifier
:func:`brutex.data.lake_invariants.classify_bar_keys` must agree with an
independent reference fold:

* ``physical_rows`` / ``unique_keys`` match the multiset's size / distinct count;
* ``foreign_rows`` equals the reference count of timestamps whose UTC
  ``(year, month)`` differs from the file's — the ONLY valid month invariant
  (never IST months);
* a column wholly inside its home UTC month NEVER produces a finding (no false
  positives — the 1day midnight-IST straddle / GIFTNIFTY guarantee in miniature).

And for ANY symbol/exchange text, the #911 ladder
(:func:`brutex.data.lake_keys.resolve_spot_key` /
:func:`~brutex.data.lake_keys.spot_key_candidates`) resolves ONLY to the exact
key or one of its ordered drift candidates, never fabricating a key (Golden
Rule #1).
"""

# pyright: reportCallIssue=false
# Rationale (DEC-PYRIGHT-UNMASK-001 precedent): the Hypothesis `@st.composite`
# strategy below (`_timestamp_multisets`) is called WITHOUT the `draw` argument —
# Hypothesis injects it at runtime. pyright does not model the `@composite`
# signature transform and reports a false "Argument missing for parameter draw"
# at the `@given(_timestamp_multisets())` site. Scoped to this file only.
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

pytest.importorskip("polars")

import polars as pl
from brutex.data.lake_invariants import (
    PartitionCoords,
    classify_bar_keys,
    classify_option_keys,
)
from brutex.data.lake_keys import resolve_spot_key, spot_key_candidates
from brutex.data.partition import partition_for_timestamp

_COORDS = PartitionCoords(
    exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", year=2024, month=6
)

#: Midnight UTC of the home month's first day.
_HOME_START = datetime(2024, 6, 1, tzinfo=UTC)

#: Minute offsets spanning ~3 UTC months around the home month (May..July),
#: so relocations land both before and after the home month.
_MINUTES_IN_MONTH = 30 * 24 * 60


@st.composite
def _timestamp_multisets(draw: st.DrawFn) -> list[datetime]:
    """A 1..60-element multiset of minute-grid timestamps around 2024-06."""
    offsets = draw(
        st.lists(
            st.integers(min_value=-_MINUTES_IN_MONTH, max_value=2 * _MINUTES_IN_MONTH - 1),
            min_size=1,
            max_size=40,
        )
    )
    # Inject explicit duplicate copies of already-drawn offsets (0..half).
    dup_picks = draw(
        st.lists(
            st.integers(min_value=0, max_value=len(offsets) - 1),
            min_size=0,
            max_size=len(offsets) // 2,
        )
    )
    offsets.extend(offsets[i] for i in dup_picks)
    return [_HOME_START + timedelta(minutes=m) for m in offsets]


@given(_timestamp_multisets())
@settings(max_examples=60, deadline=None)
def test_classify_matches_reference_fold(timestamps: list[datetime]) -> None:
    series = pl.Series("timestamp", timestamps, dtype=pl.Datetime("us", "UTC"))
    dup, foreign = classify_bar_keys(_COORDS, series)

    physical = len(timestamps)
    unique = len(set(timestamps))
    ref_foreign = sum(
        1 for ts in timestamps if partition_for_timestamp(ts) != (_COORDS.year, _COORDS.month)
    )

    if physical > unique:
        assert dup is not None
        assert dup.physical_rows == physical
        assert dup.unique_keys == unique
        assert dup.dup_rows == physical - unique
    else:
        assert dup is None

    if ref_foreign:
        assert foreign is not None
        assert foreign.foreign_rows == ref_foreign
        assert foreign.overlap_rows is None  # the pure core never reads siblings
    else:
        assert foreign is None


@given(
    st.lists(
        st.integers(min_value=0, max_value=_MINUTES_IN_MONTH - 1),
        min_size=1,
        max_size=40,
        unique=True,
    )
)
@settings(max_examples=60, deadline=None)
def test_home_month_column_never_flagged(offsets: list[int]) -> None:
    """A unique, wholly-home-month column produces ZERO findings (no false flags)."""
    timestamps = [_HOME_START + timedelta(minutes=m) for m in offsets]
    series = pl.Series("timestamp", timestamps, dtype=pl.Datetime("us", "UTC"))
    assert classify_bar_keys(_COORDS, series) == (None, None)


#: The concrete path the option classifier stamps (pure — never read).
_OPT_PATH = Path("options/SERIES/2024-06.parquet")


def _option_frame(rows: list[tuple[str, datetime]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "side": pl.Series("side", [side for side, _ in rows], dtype=pl.Utf8),
            "timestamp": pl.Series(
                "timestamp", [ts for _, ts in rows], dtype=pl.Datetime("us", "UTC")
            ),
        }
    )


@st.composite
def _option_key_multisets(draw: st.DrawFn) -> list[tuple[str, datetime]]:
    """A 1..60-element (side, ts) multiset around 2024-06, dups injected."""
    sides = st.sampled_from(["CE", "PE"])
    pairs = draw(
        st.lists(
            st.tuples(
                sides,
                st.integers(min_value=-_MINUTES_IN_MONTH, max_value=2 * _MINUTES_IN_MONTH - 1),
            ),
            min_size=1,
            max_size=40,
        )
    )
    dup_picks = draw(
        st.lists(
            st.integers(min_value=0, max_value=len(pairs) - 1),
            min_size=0,
            max_size=len(pairs) // 2,
        )
    )
    pairs.extend(pairs[i] for i in dup_picks)
    return [(side, _HOME_START + timedelta(minutes=m)) for side, m in pairs]


@given(_option_key_multisets())
@settings(max_examples=60, deadline=None)
def test_classify_option_matches_reference_fold(rows: list[tuple[str, datetime]]) -> None:
    """The options classifier agrees with an independent (side, ts) reference fold.

    DEC-LAKE-HEAL-OPTIONS-FOREIGN-001: ``physical_rows``/``unique_keys`` match
    the pair-multiset size / distinct count (cross-side same-ts alone is never
    a dup); ``foreign_rows`` equals an independent per-ROW ts-month fold
    (timestamp-only routing — side never consulted); ``overlap_rows`` is
    ``None`` from the pure core (it never reads siblings).
    """
    dup, foreign = classify_option_keys(_OPT_PATH, "SERIES", (2024, 6), _option_frame(rows))

    physical = len(rows)
    unique = len(set(rows))
    ref_foreign = sum(1 for _side, ts in rows if partition_for_timestamp(ts) != (2024, 6))

    if physical > unique:
        assert dup is not None
        assert dup.physical_rows == physical
        assert dup.unique_keys == unique
    else:
        assert dup is None

    if ref_foreign:
        assert foreign is not None
        assert foreign.foreign_rows == ref_foreign
        assert foreign.overlap_rows is None  # the pure core never reads siblings
    else:
        assert foreign is None


@given(
    st.lists(
        st.tuples(
            st.sampled_from(["CE", "PE"]),
            st.integers(min_value=0, max_value=_MINUTES_IN_MONTH - 1),
        ),
        min_size=1,
        max_size=40,
        unique=True,
    )
)
@settings(max_examples=60, deadline=None)
def test_option_home_month_frame_never_flagged(pairs: list[tuple[str, int]]) -> None:
    """A unique, wholly-home-month (side, ts) frame produces ZERO findings."""
    rows = [(side, _HOME_START + timedelta(minutes=m)) for side, m in pairs]
    assert classify_option_keys(_OPT_PATH, "SERIES", (2024, 6), _option_frame(rows)) == (None, None)


_SYMBOL_TEXT = st.text(
    alphabet=st.sampled_from(list("ABCXYZ&- ")),
    min_size=0,
    max_size=12,
)


@given(symbol=_SYMBOL_TEXT, exchange=st.sampled_from(["NSE", "BSE", "", " "]))
@settings(max_examples=100, deadline=None)
def test_resolve_only_ever_returns_symbol_or_a_candidate(symbol: str, exchange: str) -> None:
    candidates = spot_key_candidates(symbol, exchange)
    # The identity is never a candidate; empty keys are never candidates.
    assert symbol not in candidates
    assert "" not in candidates
    # Resolution against a canonical universe made of the candidates themselves:
    # the FIRST rung that matches wins, and the value always round-trips.
    canonical = {key: f"v:{key}" for key in candidates}
    canonical[symbol] = "v:self"
    resolved = resolve_spot_key(symbol, exchange, canonical)
    assert resolved == (symbol, "v:self")  # exact match always wins first
    del canonical[symbol]
    resolved = resolve_spot_key(symbol, exchange, canonical)
    if candidates:
        assert resolved == (candidates[0], f"v:{candidates[0]}")
    else:
        assert resolved is None


@given(symbol=_SYMBOL_TEXT, exchange=st.sampled_from(["NSE", "BSE", ""]))
@settings(max_examples=100, deadline=None)
def test_unknown_stray_never_fabricated(symbol: str, exchange: str) -> None:
    """Against an empty canonical universe NOTHING ever resolves (Golden Rule #1)."""
    assert resolve_spot_key(symbol, exchange, {}) is None
