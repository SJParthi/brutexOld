"""Tests for ``brutex.adapters.groww.universe_ingest`` — the universe pass.

These exercise the pure orchestration core (:func:`run_universe_ingest`) with
fakes (no DuckDB, no SDK, no network), plus the synthetic sources that power
``brutex fetch --universe --dry-run``. The module must import without the
``fno_math`` Rust kernel (it depends only on structural ``Protocol`` types, not
the heavy concrete result classes), so these tests collect even when the kernel
is not built.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from brutex.adapters.groww.universe_ingest import (
    SYNTHETIC_INSTRUMENT_CSV,
    UniverseIngestReport,
    run_universe_ingest,
)


@dataclass
class _FakeInstrumentResult:
    parsed: int
    filtered: int
    quarantined: int


@dataclass
class _FakeExpiryRecord:
    expiry_date: date


@dataclass
class _FakeExpiryResult:
    records: list[Any]


@dataclass
class _FakeContractResult:
    records: list[Any]


class _FakeInstrumentFetcher:
    """Records the refresh call + returns a canned instrument result."""

    def __init__(self) -> None:
        self.refreshed = 0

    def refresh(self) -> _FakeInstrumentResult:
        self.refreshed += 1
        return _FakeInstrumentResult(parsed=2, filtered=1, quarantined=0)


class _FakeExpiryFetcher:
    """Returns one expiry per (underlying, year) and records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def fetch(self, underlying: str, exchange: str, *, year: int) -> _FakeExpiryResult:
        self.calls.append((underlying, exchange, year))
        return _FakeExpiryResult(records=[_FakeExpiryRecord(expiry_date=date(year, 1, 30))])


class _FakeContractFetcher:
    """Returns two contracts per expiry and records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, date]] = []

    def fetch(self, underlying: str, exchange: str, expiry_date: date) -> _FakeContractResult:
        self.calls.append((underlying, exchange, expiry_date))
        return _FakeContractResult(records=["ce", "pe"])


def test_run_universe_ingest_aggregates_all_surfaces() -> None:
    """Refresh runs once; expiries per (underlying, year); contracts per expiry."""
    inst = _FakeInstrumentFetcher()
    exp = _FakeExpiryFetcher()
    con = _FakeContractFetcher()

    report = run_universe_ingest(
        inst,
        exp,
        con,
        [("NIFTY", "NSE"), ("BANKNIFTY", "NSE")],
        [2024, 2025],
    )

    assert isinstance(report, UniverseIngestReport)
    assert inst.refreshed == 1
    assert report.instruments.parsed == 2
    assert report.instruments.filtered == 1
    # 2 underlyings × 2 years.
    assert report.underlyings == 2
    assert exp.calls == [
        ("NIFTY", "NSE", 2024),
        ("NIFTY", "NSE", 2025),
        ("BANKNIFTY", "NSE", 2024),
        ("BANKNIFTY", "NSE", 2025),
    ]
    # 1 expiry per (underlying, year) → 4 expiries.
    assert report.expiries == 4
    # 2 contracts per expiry → 8 contracts.
    assert report.contracts == 8
    assert len(con.calls) == 4


def test_run_universe_ingest_empty_underlyings_still_refreshes_instruments() -> None:
    """With no underlyings, only the instrument master is refreshed."""
    inst = _FakeInstrumentFetcher()
    exp = _FakeExpiryFetcher()
    con = _FakeContractFetcher()

    report = run_universe_ingest(inst, exp, con, [], [2025])

    assert inst.refreshed == 1
    assert report.underlyings == 0
    assert report.expiries == 0
    assert report.contracts == 0
    assert exp.calls == []
    assert con.calls == []


def test_synthetic_instrument_csv_has_required_header() -> None:
    """The synthetic CSV header must carry every required instrument column."""
    from brutex.adapters.groww.instruments import (
        _REQUIRED_COLUMNS,
        InstrumentMasterFetcher,
    )

    header = SYNTHETIC_INSTRUMENT_CSV.splitlines()[0].split(",")
    assert frozenset(header) >= _REQUIRED_COLUMNS
    # And it actually parses through the real fetcher with a fake sink.
    captured: list[Any] = []

    class _FakeSink:
        def upsert_many(self, instruments: Any, **_: Any) -> None:
            captured.extend(instruments)

    fetcher = InstrumentMasterFetcher(
        http_get=lambda _url: SYNTHETIC_INSTRUMENT_CSV,
        store=_FakeSink(),
        run_id_factory=lambda: "synthetic-instruments",
    )
    result = fetcher.refresh(snapshot_date=date(2025, 1, 1))
    assert result.parsed == 2
    assert len(captured) == 2
