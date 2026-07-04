"""F&O-spine vendor-surface threading (DEC-VENDOR-CONFLICT-SURFACE-001).

The option spine's consumer tail folds the sink's
``vendor_dup_rows_dropped`` / ``max_close_delta`` onto the per-pair
:class:`IngestReport`, and :class:`GrowwOptionIngestReport` derives the
pair-summed / max-folded totals — the numbers the historical phase result and
the pipeline headline read. F&O lake stays REFERENCE_ONLY (CLAUDE.md §2 Rule 3)
— these are OBSERVABILITY counters, never an engine input.

Fail-before proof: on origin/main ``IngestReport`` lacks both fields and
``GrowwOptionIngestReport`` has no ``conflicts`` / ``vendor_dup_rows_dropped``
/ ``max_close_delta`` properties (``AttributeError``) — this suite cannot pass
there.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.ingest import IngestReport
from brutex.adapters.groww.option_ingest import (
    GrowwOptionIngestReport,
    ingest_option_contract,
)
from brutex.adapters.protocol import DerivativeContract
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

# 09:30 IST on 2024-01-02 — inside the intraday session.
_IN_SESSION = "2024-01-02T09:30:00"


def _now() -> datetime:
    """A frozen IST clock so the current-day clamp is deterministic (range is past)."""
    return datetime(2026, 6, 13, 8, 0, tzinfo=UTC)


class _FakeSDK:
    """Per-call scripted candle payloads (clamped to the last entry)."""

    def __init__(self, payloads: list[Mapping[str, object]]) -> None:
        self._payloads = payloads
        self.calls = 0

    def get_historical_candles(self, **kw: object) -> Mapping[str, object]:
        del kw
        payload = self._payloads[min(self.calls, len(self._payloads) - 1)]
        self.calls += 1
        return payload

    def get_expiries(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        raise AssertionError("unused")

    def get_contracts(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        raise AssertionError("unused")


def _payload(*rows: list[object]) -> Mapping[str, object]:
    return {"candles": list(rows)}


def _row(ts: str, close: float) -> list[object]:
    return [ts, 100.0, 110.0, 90.0, close, 50, 7]


def _ce() -> DerivativeContract:
    return DerivativeContract(
        underlying_symbol="NIFTY",
        expiry=date(2025, 1, 2),
        symbol="NSE-NIFTY-02Jan25-28500-CE",
        contract_type="CE",
        strike=28500.0,
    )


def _ingest(sink: ParquetBarSink, sdk: _FakeSDK) -> IngestReport:
    client = GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=1)
    return ingest_option_contract(
        client,
        sink,
        contract=_ce(),
        interval="1minute",
        start=date(2024, 1, 2),
        end=date(2024, 1, 3),
        now_ist=_now,
        empty_retry_attempts=1,
    )


def test_option_tail_carries_dup_and_delta(tmp_path: Path) -> None:
    """Pull 1 stores clean; pull 2's restate + duplicate reach the pair report."""
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=tmp_path))
    sdk = _FakeSDK(
        [
            _payload(_row(_IN_SESSION, 105.0)),
            _payload(
                _row(_IN_SESSION, 149.70),  # restates stored 105.00 → conflict, Δ 44.70
                _row("2024-01-02T09:31:00", 106.0),
                _row("2024-01-02T09:31:00", 106.0),  # within-batch duplicate → dropped
            ),
        ]
    )
    first = _ingest(sink, sdk)
    assert first.vendor_dup_rows_dropped == 0
    assert first.max_close_delta is None

    second = _ingest(sink, sdk)
    assert second.conflicts == 1
    assert second.vendor_dup_rows_dropped == 1
    assert second.max_close_delta == Decimal("44.70")


class TestAggregateReportProperties:
    def test_pair_sums_and_max_fold(self) -> None:
        report = GrowwOptionIngestReport(
            pairs={
                ("A", "1minute"): IngestReport(
                    windows=1,
                    bars_appended=10,
                    transient_failures=0,
                    conflicts=2,
                    vendor_dup_rows_dropped=3,
                    max_close_delta=Decimal("0.05"),
                ),
                ("B", "1minute"): IngestReport(
                    windows=1,
                    bars_appended=5,
                    transient_failures=0,
                    conflicts=1,
                    vendor_dup_rows_dropped=4,
                    max_close_delta=Decimal("44.70"),
                ),
            },
            failures=(),
        )
        assert report.conflicts == 3
        assert report.vendor_dup_rows_dropped == 7
        assert report.max_close_delta == Decimal("44.70")

    def test_empty_pairs_read_zero_and_none(self) -> None:
        report = GrowwOptionIngestReport(pairs={}, failures=())
        assert report.conflicts == 0
        assert report.vendor_dup_rows_dropped == 0
        assert report.max_close_delta is None
