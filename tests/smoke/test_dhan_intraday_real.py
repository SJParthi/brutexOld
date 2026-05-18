"""Opt-in smoke test: real ``DhanClient.fetch_intraday`` against api.dhan.co.

Gated by **both** ``BRUTEX_DHAN_SMOKE=1`` env var AND a real
``BRUTEX_DHAN_ACCESS_TOKEN`` (or token file). The default CI run skips it.

What this test asserts:

* The full chain — auth → rate-limiter → httpx → classify_response →
  parser — yields ``Outcome.OK_WITH_DATA`` for one trading day of NIFTY.
* At least 300 1-minute bars come back (a typical NSE day is ~375).
* Every bar's UTC timestamp falls within the requested window converted
  to UTC bounds.
* Bars are sorted by timestamp ascending and unique (parser's
  ``_sort_and_dedup`` invariant verified end-to-end).

Security posture matches PR #51's `test_dhan_securityid_verification.py`:
``httpx.Timeout(30s)`` + ``trust_env=False`` + ``verify=True`` +
``follow_redirects=False`` + no response body in logs.

To run::

    BRUTEX_DHAN_SMOKE=1 BRUTEX_DHAN_ACCESS_TOKEN=... \
        pytest tests/smoke/test_dhan_intraday_real.py -v

References
----------
* CLAUDE.md §6: ``securityId — NIFTY 50 = 13``
* CLAUDE.md §7: trading hours 09:15-15:30 IST
* DEC-P0-036 (PR #2c): SSRF guard + real-Dhan smoke
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta

import pytest

pytest.importorskip("httpx")


pytestmark = [
    pytest.mark.smoke,
    pytest.mark.skipif(
        not os.environ.get("BRUTEX_DHAN_SMOKE"),
        reason="opt-in smoke test — set BRUTEX_DHAN_SMOKE=1 to run",
    ),
]


from brutex.adapters.dhan.auth import load_dhan_token  # noqa: E402
from brutex.adapters.dhan.body import IntradayChunkRequest  # noqa: E402
from brutex.adapters.dhan.client import DhanClient  # noqa: E402
from brutex.adapters.dhan.constants import NIFTY  # noqa: E402
from brutex.adapters.dhan.timeconv import IST, UTC  # noqa: E402
from brutex.orchestrator.quota_tracker import DailyQuotaTracker  # noqa: E402
from brutex.orchestrator.rate_limiter import AdaptiveTokenBucket  # noqa: E402
from brutex.orchestrator.state import Outcome  # noqa: E402

#: Minimum bars expected for one full NSE trading day. The market is
#: open 09:15-15:30 IST = 375 minutes; allowing for the open auction
#: skew + occasional gaps, 300 bars is a conservative floor.
_MIN_BARS_PER_DAY: int = 300


def _recent_trading_day() -> date:
    """Return a recent (likely-open) NSE trading day.

    NSE is closed on weekends. Walk backwards from yesterday until we
    hit a weekday. Public holidays will still fail the bar-count check;
    operators re-run on the next trading day in that case.
    """
    today = datetime.now(tz=IST).date()
    d = today - timedelta(days=1)
    while d.weekday() >= 5:  # Saturday=5, Sunday=6
        d -= timedelta(days=1)
    return d


def test_fetch_one_day_of_nifty_intraday() -> None:
    """One full NSE trading day of NIFTY 1-minute bars."""
    quota = DailyQuotaTracker()
    bucket = AdaptiveTokenBucket(quota_tracker=quota)
    token = load_dhan_token()
    trading_day = _recent_trading_day()

    req = IntradayChunkRequest(
        instrument=NIFTY,
        interval="1",
        from_date=trading_day,
        to_date=trading_day,
    )

    client = DhanClient(token=token, bucket=bucket)
    try:
        result = client.fetch_intraday(req)
    finally:
        client.close()

    assert result.outcome == Outcome.OK_WITH_DATA, (
        f"Expected OK_WITH_DATA, got {result.outcome.name} "
        f"(error class: {type(result.error).__name__ if result.error else 'None'})"
    )
    assert result.error is None
    assert len(result.bars) >= _MIN_BARS_PER_DAY, (
        f"Expected at least {_MIN_BARS_PER_DAY} bars for one full trading day; "
        f"got {len(result.bars)}. The requested day {trading_day.isoformat()} "
        f"may have been a public holiday — re-run on the next trading day."
    )

    # Window invariant: every bar's UTC timestamp falls inside the
    # IST trading day converted to UTC bounds.
    day_start_utc = datetime.combine(trading_day, datetime.min.time(), tzinfo=IST).astimezone(UTC)
    day_end_utc = day_start_utc + timedelta(days=1)
    for bar in result.bars:
        assert day_start_utc <= bar.timestamp_utc < day_end_utc, (
            f"Bar at {bar.timestamp_utc.isoformat()} falls outside requested "
            f"IST day {trading_day.isoformat()}"
        )

    # Parser invariants: sorted ascending + unique.
    timestamps = [bar.timestamp_utc for bar in result.bars]
    assert timestamps == sorted(timestamps)
    assert len(set(timestamps)) == len(timestamps)
