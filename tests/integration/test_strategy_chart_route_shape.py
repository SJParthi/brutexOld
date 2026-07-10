"""The /strategy candle chart's route-shape golden ([W3-R1], DEC-ONECLICK-TOP100-OBJECT-ROOT-001 wave-3 fold).

THE FIXTURE-BLINDSPOT LESSON, AGAIN: the strategy page's inline candle mapper
indexed ``idx.ts`` while the REAL ``/api/v1/db/candles`` payload names the
column ``"timestamp"`` (``db_view.CANDLE_COLUMNS``) — so on every real payload
``raw[r][idx.ts]`` read ``undefined``, every row was skipped, and the chart
was DEAD ON ARRIVAL ("no candles in the window") while no test fed the mapper
a route-shaped payload. This suite drives the ACTUAL route (``build_app`` +
``TestClient`` over a lake written by the PRODUCTION ``ParquetWriter``) and
feeds the wire payload through the ACTUAL shipped mapper
(``bx-chart.js`` ``candleRowsFromDbPayload``) under node — with the node
process forced to ``TZ=America/New_York`` so the SECOND-layer fix (naive IST
strings previously parsed browser-LOCAL, shifting candles against the
offset-carrying trade timestamps → misplaced/dropped arrows in a non-IST
browser) is proven TZ-invariant end-to-end, arrows included.

Skipped when fastapi/duckdb/polars or node are absent (the existing route +
node-golden conventions).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 — fixed-argv node invocation, test-only
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("polars")

from brutex.dashboard.app import DashboardState, build_app
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

_STATIC = Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "static"
_CHART_JS = _STATIC / "bx-chart.js"
_NODE = shutil.which("node")

#: The exact coordinate the /strategy page builds (segment=INDEX, §6 universe).
_QUERY = {
    "exchange": "NSE",
    "segment": "INDEX",
    "symbol": "NSE-NIFTY",
    "interval": "1minute",
    "start": "2024-01-02",
    "end": "2024-01-02",
    "limit": 500,
}

#: Five session-open minutes: 03:45..03:49 UTC == 09:15..09:19 IST.
_BAR_INSTANTS = [datetime(2024, 1, 2, 3, 45 + m, tzinfo=UTC) for m in range(5)]


def _idx_instrument() -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=13,
        symbol_name="NIFTY",
        display_name="NIFTY 50",
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


def _bar(ts: datetime, base: int) -> CandleBar:
    return CandleBar(
        timestamp=ts,
        open=Decimal(f"{base}.00"),
        high=Decimal(f"{base + 2}.00"),
        low=Decimal(f"{base - 1}.00"),
        close=Decimal(f"{base + 1}.00"),
        volume=100 + base,
        open_interest=None,
    )


@pytest.fixture
def client(tmp_path: Path) -> Any:
    """A real app over a lake the PRODUCTION spot writer populated."""
    with InstrumentStore.open(tmp_path / "instruments.duckdb") as store:
        store.upsert_many(
            [_idx_instrument()],
            snapshot_date=date(2026, 6, 1),
            run_id="r1",
            csv_source_sha256="0" * 64,
        )
        ParquetWriter(lake_root=tmp_path).write(
            bars=[_bar(ts, 100 + n) for n, ts in enumerate(_BAR_INSTANTS)],
            exchange="NSE",
            segment="INDEX",
            symbol="NSE-NIFTY",
            interval="1minute",
        )
        yield TestClient(build_app(DashboardState(), instrument_store=store))


def _node_call_tz(tz: str, expr: str, *argv: str) -> Any:
    """Run the REAL shipped bx-chart.js under node with TZ forced to *tz*."""
    script = (
        f"const C = require({json.dumps(str(_CHART_JS))});"
        "const ARGS = process.argv.slice(1).map((a) => JSON.parse(a));"
        f"console.log(JSON.stringify({expr}));"
    )
    proc = subprocess.run(  # nosec B603 — fixed argv, test-only
        ["node", "-e", script, *argv],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "TZ": tz},
    )
    return json.loads(proc.stdout)


class TestStrategyChartRouteShape:
    """The REAL route payload through the REAL shipped mapper."""

    def test_route_column_is_timestamp_never_ts(self, client: Any) -> None:
        """The route truth the pre-fix mapper contradicted: the bar-time
        column is named "timestamp" — there is NO "ts" column to index."""
        payload = client.get("/api/v1/db/candles", params=_QUERY).json()
        assert "timestamp" in payload["columns"]
        assert "ts" not in payload["columns"]
        assert payload["total"] == len(_BAR_INSTANTS)

    @pytest.mark.skipif(_NODE is None, reason="node not installed")
    def test_real_payload_maps_to_candles_in_a_new_york_browser(self, client: Any) -> None:
        """Every stored bar plots (pre-fix: ZERO rows mapped — chart dead),
        with epochs equal to the true instants regardless of the browser TZ."""
        payload = client.get("/api/v1/db/candles", params=_QUERY).json()
        out = _node_call_tz(
            "America/New_York", "C.candleRowsFromDbPayload(ARGS[0])", json.dumps(payload)
        )
        assert out["missing"] is None
        rows = out["rows"]
        assert len(rows) == len(_BAR_INSTANTS)
        expected_ms = [int(ts.timestamp() * 1000) for ts in _BAR_INSTANTS]
        assert [r["ts"] for r in rows] == expected_ms  # ascending + TZ-invariant
        assert rows[0]["open"] == 100.0
        assert rows[-1]["close"] == 105.0
        # And the IST session-day anchor derived from a trade instant matches
        # the ?start/?end day the page requests — never a local-date shift.
        anchor = _node_call_tz(
            "America/New_York", "C.istDateOfEpoch(ARGS[0])", json.dumps(expected_ms[0])
        )
        assert anchor == "2024-01-02"

    @pytest.mark.skipif(_NODE is None, reason="node not installed")
    @pytest.mark.parametrize("tz", ["America/New_York", "Asia/Kolkata"])
    def test_trade_arrows_land_on_the_same_candles_in_every_tz(self, client: Any, tz: str) -> None:
        """Second layer ([W3-R1]): trade timestamps are the trade store's
        offset-carrying UTC form; candles are naive IST strings — the pre-fix
        browser-local parse shifted the candle epochs so the arrows misplaced
        or dropped outside IST. Placement is now identical in every zone."""
        payload = client.get("/api/v1/db/candles", params=_QUERY).json()
        entry_iso = _BAR_INSTANTS[2].isoformat()  # "...T03:47:00+00:00"
        exit_iso = _BAR_INSTANTS[4].isoformat()
        assert entry_iso.endswith("+00:00")  # the trade_store._us_to_iso form
        expr = (
            "(() => {"
            "  const rows = C.candleRowsFromDbPayload(ARGS[0]).rows;"
            "  return C.tradeArrow(C.parseTsIst(ARGS[1]), C.parseTsIst(ARGS[2]), rows);"
            "})()"
        )
        out = _node_call_tz(
            tz, expr, json.dumps(payload), json.dumps(entry_iso), json.dumps(exit_iso)
        )
        assert out == {"entry_idx": 2, "exit_idx": 4, "dropped": False, "note": ""}
