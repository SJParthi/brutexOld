"""Property tests for the Dhan pure laws — parser round-trip, chunk coverage, error totality.

DEC-DHAN-ADAPTER-001 (hypothesis; the 7-test-types property leg). Four invariants:

* **Parser round-trip** — a synthetic column payload of valid bars parses to exactly
  those bars (count + exact Decimal prices via ``str``), and timestamps are UTC-aware
  monotone when the input epochs are ascending.
* **Chunk-window coverage** — ``plan_candle_windows`` tiles any range contiguously
  (no gap, no overlap, clipped end) with every window ≤ the interval's cap.
* **Rate-limit classification totality** — ``from_dhan_response`` returns a DhanError
  for EVERY (status, DH-code) pair, retryable ⊆ the documented transient set, and the
  auth-family/retryable predicates stay mutually consistent.
* **Expiry-list round-trip** — parsed dates are sorted, deduplicated, exact.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

from brutex.adapters.dhan import errors as e
from brutex.adapters.dhan.chunks import plan_candle_windows, window_days_for_interval
from brutex.adapters.dhan.parser import parse_candle_columns, parse_expiry_list
from hypothesis import given, settings
from hypothesis import strategies as st

# Prices as 2dp-ish positive floats within the sane Indian price band.
_price = st.floats(min_value=0.05, max_value=100_000.0, allow_nan=False, allow_infinity=False)
_volume = st.integers(min_value=0, max_value=10**9)
# Epoch seconds within a sane band (2005..2035).
_epoch = st.integers(min_value=1_104_537_600, max_value=2_051_222_400)


@st.composite
def _candle_columns(draw: st.DrawFn) -> dict[str, object]:
    n = draw(st.integers(min_value=1, max_value=20))
    epochs = sorted(draw(st.lists(_epoch, min_size=n, max_size=n, unique=True)))
    opens = [draw(_price) for _ in range(n)]
    highs = [draw(_price) for _ in range(n)]
    lows = [draw(_price) for _ in range(n)]
    closes = [draw(_price) for _ in range(n)]
    volumes = [draw(_volume) for _ in range(n)]
    return {
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
        "timestamp": epochs,
    }


@given(payload=_candle_columns())
@settings(max_examples=50, deadline=None)
def test_parser_round_trip_count_and_monotone_utc(payload: dict[str, object]) -> None:
    bars = parse_candle_columns(payload)
    # every row has positive prices → nothing skipped
    assert len(bars) == len(payload["timestamp"])  # type: ignore[arg-type]
    stamps = [bar.timestamp for bar in bars]
    assert all(ts.tzinfo is UTC for ts in stamps)
    assert stamps == sorted(stamps)  # ascending epochs → ascending UTC stamps
    # the OHLC clamp preserves open/close EXACTLY (Decimal via str)
    for bar, raw_open in zip(bars, payload["open"], strict=True):  # type: ignore[arg-type]
        assert str(bar.open) == str(raw_open)


@given(
    start_days=st.integers(min_value=0, max_value=3000),
    span_days=st.integers(min_value=0, max_value=1200),
    interval=st.sampled_from(
        ["1minute", "5minute", "15minute", "25minute", "60minute", "1hour", "1day"]
    ),
)
@settings(max_examples=100, deadline=None)
def test_chunk_windows_cover_range_contiguously(
    start_days: int, span_days: int, interval: str
) -> None:
    start = datetime(2020, 1, 1) + timedelta(days=start_days)
    end = start + timedelta(days=span_days)
    windows = plan_candle_windows(start, end, interval=interval)
    cap = window_days_for_interval(interval)
    if span_days == 0:
        assert windows == []
        return
    # contiguous, non-overlapping, clipped
    assert windows[0].start == start.isoformat()
    assert windows[-1].end == end.isoformat()
    for left, right in itertools.pairwise(windows):
        assert left.end == right.start
    for w in windows:
        width = datetime.fromisoformat(w.end) - datetime.fromisoformat(w.start)
        assert timedelta(0) < width <= timedelta(days=cap)


_dh_codes = st.one_of(
    st.sampled_from(sorted(e.DH_CODE_TO_ERROR) + sorted(e.DH_CODE_UNVERIFIED)),
    st.text(min_size=0, max_size=8),
)


@given(status=st.integers(min_value=100, max_value=599), code=st.one_of(st.none(), _dh_codes))
@settings(max_examples=200, deadline=None)
def test_error_classification_totality(status: int, code: str | None) -> None:
    body: dict[str, object] = {} if code is None else {"errorCode": code}
    err = e.from_dhan_response(status, body)
    assert isinstance(err, e.DhanError)  # TOTAL — every pair classifies
    # retryable ⇒ one of the documented transient classes
    if e.is_retryable(err):
        assert isinstance(
            err,
            (
                e.DhanRateLimitError,
                e.DhanTimeoutError,
                e.DhanEmptyResponseError,
                e.DhanServerError,
                e.DhanNetworkError,
                e.DhanTokenStillStaleError,
            ),
        )
    # predicate consistency: terminal-auth ⇔ auth ∧ ¬retryable
    assert e.is_terminal_auth_failure(err) == (e.is_auth_failure(err) and not e.is_retryable(err))
    # the ladder predicate never fires for rate-limit / auth / terminal
    if e.steps_timeout_ladder(err):
        assert e.is_retryable(err)
        assert not e.is_rate_limit(err)
        assert not e.is_auth_failure(err)


@given(
    dates=st.lists(
        st.dates(min_value=datetime(2020, 1, 1).date(), max_value=datetime(2035, 12, 31).date()),
        min_size=0,
        max_size=20,
    )
)
@settings(max_examples=50, deadline=None)
def test_expiry_list_round_trip_sorted_dedup(dates: list) -> None:  # type: ignore[type-arg]
    payload = {"status": "success", "data": [d.isoformat() for d in dates]}
    got = parse_expiry_list(payload)
    assert got == sorted(set(dates))
