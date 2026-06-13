"""Golden (byte-frozen) test for the movers screener (DEC-MOVERS-VOLUME-001).

Pins one deterministic screen — a normal mover, a single-bar instrument whose
surge is undefined, and an empty (skipped) instrument — to an exact JSON render.
``str(Decimal)`` is used for the metric fields so the comparison is byte-exact
(the metrics are computed in a fixed 28-digit context — Golden Rule #4 / #7). Any
drift in the metric math or the ordering breaks this test.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from brutex.screeners.volume_movers import (
    MoverMetrics,
    MoversReport,
    ScreenTarget,
    rank_movers,
)

from tests.fakes.movers_fakes import make_candle

pytestmark = pytest.mark.golden

_START = datetime(2025, 1, 2, tzinfo=UTC)
_END = datetime(2025, 1, 3, tzinfo=UTC)

_NIFTY = ScreenTarget("NSE", "INDEX", "NSE-NIFTY", "1minute")
_BANKNIFTY = ScreenTarget("NSE", "INDEX", "NSE-BANKNIFTY", "1minute")
_SENSEX = ScreenTarget("BSE", "INDEX", "BSE-SENSEX", "1minute")


def _report() -> MoversReport:
    series = {
        _NIFTY: [
            make_candle(high="20100", low="19900", close="20000", volume=100, minute=0),
            make_candle(high="20200", low="19800", close="20000", volume=100, minute=1),
            make_candle(high="20600", low="19400", close="20000", volume=900, minute=2),
            make_candle(high="21000", low="19000", close="20000", volume=900, minute=3),
        ],
        _BANKNIFTY: [
            make_candle(high="45200", low="44800", close="45000", volume=50, minute=0),
        ],
        _SENSEX: [],
    }
    return rank_movers(series, window_start=_START, window_end=_END, recent_bars=2, top_n=3)


def _row(metric: MoverMetrics) -> dict[str, object]:
    def _opt(value: object) -> str | None:
        return None if value is None else str(value)

    return {
        "symbol": metric.target.symbol,
        "bar_count": metric.bar_count,
        "recent_avg_volume": _opt(metric.recent_avg_volume),
        "baseline_avg_volume": _opt(metric.baseline_avg_volume),
        "volume_surge_ratio": _opt(metric.volume_surge_ratio),
        "volatility_bar_count": metric.volatility_bar_count,
        "realised_range_volatility": _opt(metric.realised_range_volatility),
        "last_close_paisa": metric.last_close_paisa,
        "last_close_inr": metric.last_close_inr,
    }


def _render(report: MoversReport) -> str:
    payload = {
        "window_start": report.window_start.isoformat(),
        "window_end": report.window_end.isoformat(),
        "recent_bars": report.recent_bars,
        "top_n": report.top_n,
        "metrics": [_row(m) for m in report.metrics],
        "top_volume_surge": [m.target.symbol for m in report.top_volume_surge],
        "top_realised_volatility": [m.target.symbol for m in report.top_realised_volatility],
        "skipped": [t.symbol for t in report.skipped],
    }
    return json.dumps(payload, indent=2)


_GOLDEN = """\
{
  "window_start": "2025-01-02T00:00:00+00:00",
  "window_end": "2025-01-03T00:00:00+00:00",
  "recent_bars": 2,
  "top_n": 3,
  "metrics": [
    {
      "symbol": "NSE-BANKNIFTY",
      "bar_count": 1,
      "recent_avg_volume": null,
      "baseline_avg_volume": null,
      "volume_surge_ratio": null,
      "volatility_bar_count": 1,
      "realised_range_volatility": "0.008888888888888888888888888889",
      "last_close_paisa": 4500000,
      "last_close_inr": "\\u20b945,000.00"
    },
    {
      "symbol": "NSE-NIFTY",
      "bar_count": 4,
      "recent_avg_volume": "900",
      "baseline_avg_volume": "100",
      "volume_surge_ratio": "9",
      "volatility_bar_count": 4,
      "realised_range_volatility": "0.0475",
      "last_close_paisa": 2000000,
      "last_close_inr": "\\u20b920,000.00"
    }
  ],
  "top_volume_surge": [
    "NSE-NIFTY"
  ],
  "top_realised_volatility": [
    "NSE-NIFTY",
    "NSE-BANKNIFTY"
  ],
  "skipped": [
    "BSE-SENSEX"
  ]
}"""


def test_movers_report_is_byte_frozen() -> None:
    assert _render(_report()) == _GOLDEN
