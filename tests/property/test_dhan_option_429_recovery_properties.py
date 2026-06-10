"""Property tests — the option series ingest is COMPLETE-or-EMPTY, never partial.

DEC-OPTION-429-RECOVERY-001 (audit Gap #2). The rolling-option series ingest is
transactional: a 429 (or any transient) cursor is re-fetched in-run up to the
retry budget, and the sink/coverage are mutated only in a final commit pass that
runs after EVERY cursor has been fetched cleanly. So whatever a series leaves in
the lake is always the WHOLE partition or NOTHING — never a page-holed partial,
no matter where the 429 lands or how many cursors the chain has.

The invariant under test (over randomized chain length, failing-cursor position,
heal-vs-permanent, and retry budget):

    committed_windows ∈ {0, N}   — never 1 … N-1

plus the heal/abort decision matches the budget arithmetic:

    series completes  ⇔  the failing cursor recovers within the retry budget.
"""

# The tiling oracle `window_dates` went PUBLIC in DEC-OPTION-WINDOW-PLAN-001
# sanctioned for tests, mirroring `test_dhan_option_universe_ingest.py`.
# pyright: reportPrivateUsage=none

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta

from brutex.adapters.dhan.errors import DhanRateLimitError
from brutex.adapters.dhan.option_ingest import ingest_rolling_option_series, window_dates
from brutex.data.models import OptionBar
from hypothesis import assume, given
from hypothesis import strategies as st

# 1609732800 = 2021-01-04 04:00:00 UTC = 09:30 IST → in session [09:15, 15:30).
_TS_IN = 1609732800


def _side() -> dict[str, object]:
    """A one-in-session-bar CE side; PE null — the rolling-option payload shape."""
    return {
        "timestamp": [_TS_IN],
        "open": [100],
        "high": [110],
        "low": [90],
        "close": [105],
        "volume": [250],
        "oi": [1200],
        "strike": [17500],
        "spot": [17480.5],
        "iv": [0.15],
    }


class _CursorFakeClient:
    """A FakeDhanClient that injects a 429 at a chosen cursor (window) index.

    Each distinct ``fromDate`` in the request body is a cursor; retries repeat the
    same ``fromDate`` so they map to the same cursor index. Cursor *fail_cursor*
    raises :class:`DhanRateLimitError` on its first *fail_times* attempts (``None``
    → permanently); every other call returns one in-session CE bar.
    """

    def __init__(self, *, fail_cursor: int, fail_times: int | None) -> None:
        self._fail_cursor = fail_cursor
        self._fail_times = fail_times
        self._order: dict[str, int] = {}
        self._attempts: dict[int, int] = {}
        self.calls = 0

    def post_json(self, endpoint: str, body: Mapping[str, object]) -> dict[str, object]:
        del endpoint
        self.calls += 1
        frm = str(body["fromDate"])
        idx = self._order.setdefault(frm, len(self._order))
        seen = self._attempts.get(idx, 0)
        self._attempts[idx] = seen + 1
        if idx == self._fail_cursor and (self._fail_times is None or seen < self._fail_times):
            raise DhanRateLimitError(f"HTTP 429 injected at cursor {idx}")
        return {"data": {"ce": _side(), "pe": None}}


class _RecordingSink:
    """Records committed bars per series — the lake stand-in for the partition."""

    def __init__(self) -> None:
        self.batches: list[list[OptionBar]] = []

    def append_option_bars(
        self,
        *,
        security_id: str,
        exch_id: str,
        underlying_symbol: str,
        exchange_segment: str,
        expiry_flag: str,
        expiry_code: int,
        strike: str,
        drv_option_type: str,
        bars: Sequence[OptionBar],
    ) -> int:
        del security_id, exch_id, underlying_symbol, exchange_segment
        del expiry_flag, expiry_code, strike, drv_option_type
        self.batches.append(list(bars))
        return len(bars)

    @property
    def committed(self) -> int:
        """Total bars committed across all windows of the series."""
        return sum(len(b) for b in self.batches)


def _ingest(
    client: _CursorFakeClient,
    sink: _RecordingSink,
    *,
    start: date,
    end: date,
    budget: int,
) -> object:
    return ingest_rolling_option_series(
        client,
        sink,
        security_id="13",
        exch_id="NSE",
        underlying_symbol="NIFTY",
        exchange_segment="NSE_FNO",
        instrument="OPTIDX",
        expiry_flag="WEEK",
        expiry_code=1,
        strike="ATM",
        drv_option_type="CALL",
        start=start,
        end=end,
        max_retry_passes=budget,
        retry_backoff=lambda _i: None,  # no real sleep
    )


@given(
    days=st.integers(min_value=1, max_value=180),
    cursor_frac=st.floats(min_value=0.0, max_value=0.999),
    budget=st.integers(min_value=0, max_value=3),
    fail_extra=st.integers(min_value=0, max_value=5),
    permanent=st.booleans(),
)
def test_partition_is_complete_or_empty_never_partial(
    days: int, cursor_frac: float, budget: int, fail_extra: int, permanent: bool
) -> None:
    start = date(2021, 1, 4)
    end = start + timedelta(days=days)
    windows = window_dates(start, end)
    n = len(windows)
    assume(n >= 1)
    fail_cursor = int(cursor_frac * n)  # ∈ [0, n-1]
    fail_times = None if permanent else fail_extra
    client = _CursorFakeClient(fail_cursor=fail_cursor, fail_times=fail_times)
    sink = _RecordingSink()

    report = _ingest(client, sink, start=start, end=end, budget=budget)

    # ── THE INVARIANT: the partition is whole or absent, never page-holed. ──
    assert sink.committed in (0, n), f"PARTIAL partition: committed {sink.committed} of {n} windows"
    if report.complete:  # type: ignore[attr-defined]
        assert sink.committed == n  # every cursor committed (one in-session bar each)
    else:
        assert sink.batches == []  # nothing committed → empty, not partial

    # ── The heal/abort decision matches the budget arithmetic. ──
    # The failing cursor gets ``budget + 1`` attempts; it recovers iff it stops
    # failing within them (fail_times ≤ budget), and a permanent failure never does.
    heals = (not permanent) and (fail_times is not None) and (fail_times <= budget)
    assert report.complete is heals  # type: ignore[attr-defined]
