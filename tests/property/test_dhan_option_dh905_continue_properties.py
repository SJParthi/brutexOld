"""Property tests — DH-905 settle-and-continue partition arithmetic.

DEC-OPTION-DH905-CONTINUE-001. A DH-905 "no data" cursor is the vendor's
DEFINITIVE 0-bar answer for ONE window: it settles ``SKIPPED`` in the ledger
and the series CONTINUES — never a failure, never residual work. The hostile
review (finding 4) demanded the window partition be pinned by property over
randomized gap / transient / success / pre-settled interleavings, because the
existing 429-recovery property randomizes transients only:

    complete  ⇒  windows == skipped + no_data + committed-window count
    abort     ⇒  windows == skipped + no_data + still_failed   (counts as
                 accumulated up to the aborting cursor — the running-count
                 contract the abort path has always had for ``skipped``)

plus the invariants that survive every interleaving:

* a gap window NEVER reaches the sink (ledger state only, no bars);
* an abort commits NOTHING (the all-or-nothing transient invariant is intact);
* the ledger holds exactly the pre-settled + freshly-settled ``SKIPPED`` rows,
  and ``SUCCESS`` rows only when the series committed;
* a pre-settled window never costs a vendor call.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta

from brutex.adapters.dhan.errors import DhanBadRequestError, DhanRateLimitError
from brutex.adapters.dhan.option_ingest import (
    coverage_key,
    ingest_rolling_option_series,
    window_dates,
)
from brutex.data.models import OptionBar
from brutex.data.window_ledger import SKIPPED, SUCCESS, WindowLedger
from hypothesis import given
from hypothesis import strategies as st

# 1609732800 = 2021-01-04 04:00:00 UTC = 09:30 IST → in session [09:15, 15:30).
_TS_IN = 1609732800

#: Per-window outcomes the strategy interleaves. ``storm`` is a permanent 429
#: (the cursor outruns a 0 retry budget → the series aborts there).
_OUTCOMES = ("ok", "gap", "storm")


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


class _ScriptedClient:
    """Answers each cursor per its scripted outcome, recording every fromDate."""

    def __init__(self, outcome_by_from: dict[str, str]) -> None:
        self._outcome_by_from = outcome_by_from
        self.from_dates: list[str] = []

    def post_json(self, endpoint: str, body: Mapping[str, object]) -> dict[str, object]:
        del endpoint
        frm = str(body["fromDate"])
        self.from_dates.append(frm)
        outcome = self._outcome_by_from[frm]
        if outcome == "gap":
            raise DhanBadRequestError("HTTP 400 bad request: DH-905 no data present")
        if outcome == "storm":
            raise DhanRateLimitError("HTTP 429 too many requests")
        return {"data": {"ce": _side(), "pe": None}}


class _RecordingSink:
    """Records committed bars per window — the lake stand-in for the partition."""

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


@given(
    days=st.integers(min_value=1, max_value=150),
    outcome_seed=st.lists(st.sampled_from(_OUTCOMES), min_size=1, max_size=8),
    presettle_seed=st.lists(st.booleans(), min_size=1, max_size=8),
)
def test_window_partition_arithmetic_over_gap_interleavings(
    days: int, outcome_seed: list[str], presettle_seed: list[bool]
) -> None:
    start = date(2021, 1, 4)
    end = start + timedelta(days=days)
    windows = window_dates(start, end)
    n = len(windows)
    outcomes = [outcome_seed[i % len(outcome_seed)] for i in range(n)]
    presettled = [presettle_seed[i % len(presettle_seed)] for i in range(n)]

    ledger = WindowLedger()
    for (w_start, w_end), is_pre in zip(windows, presettled, strict=True):
        if is_pre:
            key = coverage_key(
                exchange_segment="NSE_FNO",
                security_id="13",
                expiry_flag="WEEK",
                expiry_code=1,
                strike="ATM",
                drv_option_type="CALL",
                window_start=w_start,
                window_end=w_end,
            )
            ledger.settle_known_gap(key, now=datetime.now(UTC), reason="pre-settled")

    client = _ScriptedClient(
        {
            w_start.isoformat(): outcome
            for (w_start, _), outcome in zip(windows, outcomes, strict=True)
        }
    )
    sink = _RecordingSink()
    report = ingest_rolling_option_series(
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
        ledger=ledger,
        max_retry_passes=0,  # a storm cursor aborts on its first answer
        retry_backoff=lambda _i: None,  # no real sleep
    )

    # ── Oracle: walk the windows exactly as the spine does. ──
    skipped = no_data = committed = 0
    aborted = False
    for outcome, is_pre in zip(outcomes, presettled, strict=True):
        if is_pre:
            skipped += 1
            continue
        if outcome == "gap":
            no_data += 1
            continue
        if outcome == "storm":
            aborted = True
            break
        committed += 1

    statuses = [r.status for r in ledger.records()]
    if aborted:
        # The all-or-nothing transient invariant is INTACT: nothing committed.
        assert report.complete is False
        assert report.bars_appended == 0
        assert sink.batches == []
        assert SUCCESS not in statuses
        # Settled gaps are EXCLUDED from the residual (running counts up to the
        # aborting cursor — the abort path's long-standing ``skipped`` contract).
        assert report.windows == skipped + no_data + report.still_failed_windows
        assert report.transient_failures == report.still_failed_windows
    else:
        assert report.complete is True
        # The partition arithmetic: every window is exactly one of
        # skipped / known-gap / committed.
        assert report.windows == skipped + no_data + committed
        assert report.bars_appended == committed  # one in-session bar per ok window
        assert len(sink.batches) == committed  # a gap window NEVER reaches the sink
        assert statuses.count(SUCCESS) == committed
    assert report.skipped_windows == skipped
    assert report.no_data_windows == no_data
    # Pre-settled rows persist + every fetched gap settled SKIPPED.
    assert statuses.count(SKIPPED) == sum(presettled) + no_data
    # A pre-settled window never costs a vendor call (the O(1) skip).
    pre_starts = {
        w_start.isoformat()
        for (w_start, _), is_pre in zip(windows, presettled, strict=True)
        if is_pre
    }
    assert pre_starts.isdisjoint(client.from_dates)
