"""Integration: Groww option ingest → REAL Parquet lake (DEC-GROWW-OPTION-INGEST-001).

Drives :func:`~brutex.adapters.groww.option_ingest.run_groww_option_ingest` through
the REAL :class:`~brutex.data.sink.ParquetBarSink` + a real
:class:`~brutex.data.window_ledger.WindowLedger` into a tmp lake, proving the full
path lands an absolute-strike contract's OHLCV+OI under the standard
``bars/<exch>/FNO/<contract_groww_symbol>/<interval>/<yyyy>/<mm>.parquet`` layout
(``partition.py``) and that a re-pull is idempotent (ledger skip — no second write).
Only the SDK is faked (no network); everything below the adapter is the real stack.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.constants import (
    GROWW_FUTURE_COVERAGE_MARKET,
    GROWW_OPTION_COVERAGE_MARKET,
)
from brutex.adapters.groww.option_ingest import plan_contract_windows, run_groww_option_ingest
from brutex.adapters.protocol import DerivativeContract
from brutex.dashboard.db_view import build_window_payload
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.partition import build_partition_path
from brutex.data.sink import ParquetBarSink
from brutex.data.window_ledger import WindowLedger
from brutex.data.window_ledger_store import save_window_ledger

_SYMBOL = "NSE-NIFTY-02Jan25-28500-CE"


def _now() -> datetime:
    return datetime(2026, 6, 13, 8, 0, tzinfo=UTC)


class _FakeSDK:
    """Returns two in-session option bars (09:30 + 09:31 IST), with volume + OI."""

    def __init__(self) -> None:
        self.calls = 0

    def get_historical_candles(self, **_kw: object) -> Mapping[str, object]:
        self.calls += 1
        return {
            "candles": [
                ["2024-01-02T09:30:00", 245.95, 246.15, 245.05, 245.60, 735060, 12345],
                ["2024-01-02T09:31:00", 245.60, 245.66, 244.80, 244.94, 682373, 12350],
            ]
        }

    def get_expiries(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        return {"expiries": []}

    def get_contracts(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        return {"contracts": []}


def _contract() -> DerivativeContract:
    return DerivativeContract(
        underlying_symbol="NIFTY",
        expiry=date(2025, 1, 2),
        symbol=_SYMBOL,
        contract_type="CE",
        strike=28500.0,
    )


def test_option_bars_land_in_fno_partition_and_repull_is_idempotent(tmp_path: Path) -> None:
    lake = tmp_path / "lake"
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake), catalog=None)
    ledger = WindowLedger()
    sdk = _FakeSDK()
    client = GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=1)

    first = run_groww_option_ingest(
        client,
        sink,
        contracts=[_contract()],
        intervals=["1minute"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 3),
        ledger=ledger,
        now_ist=_now,
    )
    assert first.bars_appended == 2 and first.failed_pairs == 0

    # The contract bars landed under bars/NSE/FNO/<contract>/1minute/2024/01.parquet.
    part = build_partition_path(
        lake_root=lake,
        exchange="NSE",
        segment="FNO",
        symbol=_SYMBOL,
        interval="1minute",
        year=2024,
        month=1,
    )
    assert part.exists()
    df = pl.read_parquet(part)
    assert df.height == 2
    # OHLCV + OI all persisted (options trade → volume + OI kept).
    assert set(df["open_interest"].to_list()) == {12345, 12350}
    assert 735060 in df["volume"].to_list()

    # Re-pull over the SAME ledger: the settled window is skipped — no new vendor
    # call, the partition is unchanged (idempotent — Golden Rule #7).
    calls_after_first = sdk.calls
    second = run_groww_option_ingest(
        client,
        sink,
        contracts=[_contract()],
        intervals=["1minute"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 3),
        ledger=ledger,
        now_ist=_now,
    )
    assert second.skipped_windows == 1 and second.bars_appended == 0
    assert sdk.calls == calls_after_first
    assert pl.read_parquet(part).height == 2  # no duplication on re-pull


def test_coverage_windows_shows_fno_planned_total_and_units(tmp_path: Path) -> None:
    """End-to-end: the pre-fetch plan makes /coverage/windows show PLANNED counts for F&O.

    DEC-FNO-WINDOW-PLAN-001 — the operator's "why no planned items + units for futures and
    options?" complaint. Pre-planning an options + a futures series, persisting the ledger,
    and projecting it through the SAME ``build_window_payload`` the dashboard route uses
    proves both buckets now carry a real ``PLANNED`` total (the units), exactly like spot.
    """
    lake = tmp_path / "lake"
    ledger = WindowLedger()
    option = _contract()
    future = DerivativeContract(
        underlying_symbol="NIFTY",
        expiry=date(2025, 1, 2),
        symbol="NSE-NIFTY-02Jan25-FUT",
        contract_type="FUT",
    )
    plan_contract_windows(
        ledger,
        contracts=[option],
        intervals=("1minute",),
        start=date(2024, 1, 1),
        end=date(2024, 1, 3),
        market=GROWW_OPTION_COVERAGE_MARKET,
        now_ist=_now,
    )
    plan_contract_windows(
        ledger,
        contracts=[future],
        intervals=("1minute",),
        start=date(2024, 1, 1),
        end=date(2024, 1, 3),
        market=GROWW_FUTURE_COVERAGE_MARKET,
        now_ist=_now,
    )
    save_window_ledger(ledger, lake)

    payload = build_window_payload(lake)
    # Both F&O buckets now show a PLANNED total (the units) — the spot-parity fix.
    assert payload["counts"]["options"]["PLANNED"] == 1
    assert payload["counts"]["futures"]["PLANNED"] == 1
    assert payload["count"] == 2

    # PLANNED rows are counted everywhere but only LISTED on an explicit status filter
    # (they are the bulk of a fresh plan and carry no per-row info beyond the count).
    assert all(r["status"] != "PLANNED" for r in payload["windows"])
    planned_only = build_window_payload(lake, status="PLANNED")
    assert planned_only["matched"] == 2
    assert {r["market"] for r in planned_only["windows"]} == {
        GROWW_OPTION_COVERAGE_MARKET,
        GROWW_FUTURE_COVERAGE_MARKET,
    }
