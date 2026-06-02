"""Tests for `brutex.adapters.dhan.option_synthetic.SyntheticOptionClient`.

100% line+branch: the payload shape (both ce + pe sides, in-session bars),
the non-positive guard, and an end-to-end drive of the real rolling-option
ingest spine offline (through a NullOptionBarSink).
"""

from __future__ import annotations

from datetime import date

import pytest
from brutex.adapters.dhan.option_ingest import ingest_rolling_option_series
from brutex.adapters.dhan.option_synthetic import SyntheticOptionClient
from brutex.data.option_sink import NullOptionBarSink


def test_payload_has_both_sides_in_session() -> None:
    client = SyntheticOptionClient(bars_per_window=3)
    payload = client.post_json("charts/rollingoption", {})
    data = payload["data"]
    assert isinstance(data, dict)
    for side_key in ("ce", "pe"):
        side = data[side_key]
        assert isinstance(side, dict)
        assert len(side["timestamp"]) == 3  # type: ignore[arg-type]
        assert len(side["open"]) == 3  # type: ignore[arg-type]
        assert side["strike"] == [17500.0, 17500.0, 17500.0]


def test_default_bars_per_window() -> None:
    payload = SyntheticOptionClient().post_json("x", {})
    ce = payload["data"]["ce"]  # type: ignore[index]
    assert len(ce["timestamp"]) == 5  # default  # type: ignore[index]


def test_non_positive_raises() -> None:
    with pytest.raises(ValueError, match="bars_per_window must be positive"):
        SyntheticOptionClient(bars_per_window=0)


def test_drives_the_rolling_option_spine_offline() -> None:
    """The synthetic client + null sink run the real rolling-option ingest offline."""
    report = ingest_rolling_option_series(
        SyntheticOptionClient(bars_per_window=4),
        NullOptionBarSink(),
        security_id="13",
        exch_id="NSE",
        underlying_symbol="NIFTY",
        exchange_segment="NSE_FNO",
        instrument="OPTIDX",
        expiry_flag="WEEK",
        expiry_code=1,
        strike="ATM",
        drv_option_type="CALL",
        start=date(2021, 1, 4),
        end=date(2021, 1, 5),
    )
    assert report.windows == 1  # a 1-day range → one rolling window
    # Both sides (ce + pe) × 4 in-session bars each = 8 bars appended.
    assert report.bars_appended == 8
    assert report.transient_failures == 0
