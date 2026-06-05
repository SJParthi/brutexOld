"""Tests for `brutex.adapters.dhan.instrument_refresh` — download → persist.

100% line+branch of `refresh_instruments` via a fake http_get + a fake
InstrumentSink (no network, no DuckDB).
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Sequence
from datetime import UTC, date, datetime

import pytest
from brutex import __version__
from brutex.adapters.dhan.instrument_refresh import (
    RefreshResult,
    _code_commit_sha,
    refresh_instruments,
)
from brutex.data.instrument_store import SCHEMA_VERSION, InstrumentRun
from brutex.models import Instrument

_HEADER = [
    "EXCH_ID",
    "SECURITY_ID",
    "INSTRUMENT",
    "INSTRUMENT_TYPE",
    "SYMBOL_NAME",
    "DISPLAY_NAME",
    "LOT_SIZE",
    "TICK_SIZE",
    "SM_FREEZE_QTY",
    "UNDERLYING_SYMBOL",
    "OPTION_TYPE",
    "STRIKE_PRICE",
    "SM_EXPIRY_DATE",
]


def _row(**over: str) -> dict[str, str]:
    base = dict.fromkeys(_HEADER, "")
    base.update(over)
    return base


def _csv(rows: list[dict[str, str]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_HEADER)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def _rows() -> list[dict[str, str]]:
    return [
        _row(
            EXCH_ID="NSE",
            SECURITY_ID="13",
            INSTRUMENT="INDEX",
            SYMBOL_NAME="NIFTY",
            DISPLAY_NAME="Nifty 50",
        ),
        _row(
            EXCH_ID="NSE",
            SECURITY_ID="2885",
            INSTRUMENT="EQUITY",
            SYMBOL_NAME="RELIANCE INDUSTRIES LTD",  # long name
            UNDERLYING_SYMBOL="RELIANCE",  # ticker → F&O join key + trading_symbol
            DISPLAY_NAME="Reliance",
            LOT_SIZE="1",
            TICK_SIZE="0.05",
        ),
        _row(
            EXCH_ID="NSE",
            SECURITY_ID="3001",
            INSTRUMENT="OPTSTK",
            SYMBOL_NAME="RELIANCE-OPT",
            DISPLAY_NAME="Rel CE",
            LOT_SIZE="250",
            TICK_SIZE="0.05",
            OPTION_TYPE="CE",
            UNDERLYING_SYMBOL="RELIANCE",
            STRIKE_PRICE="2600",
            SM_EXPIRY_DATE="2025-01-30",
        ),
    ]


class _Store:
    def __init__(self, pruned: int = 5) -> None:
        self.upserts: list[dict[str, object]] = []
        self.prune_calls = 0
        self._pruned = pruned
        self.runs: list[InstrumentRun] = []

    def upsert_many(
        self,
        instruments: Sequence[Instrument],
        *,
        snapshot_date: date,
        run_id: str,
        csv_source_sha256: str,
        replace_snapshot: bool,
    ) -> object:
        self.upserts.append(
            {
                "instruments": list(instruments),
                "snapshot_date": snapshot_date,
                "run_id": run_id,
                "csv_source_sha256": csv_source_sha256,
                "replace_snapshot": replace_snapshot,
            }
        )
        return object()

    def prune_to_latest_snapshot(self) -> int:
        self.prune_calls += 1
        return self._pruned

    def record_run(self, run: InstrumentRun) -> None:
        self.runs.append(run)


def test_refresh_persists_universe_full_snapshot() -> None:
    store = _Store(pruned=7)
    text = _csv(_rows())
    result = refresh_instruments(
        store,
        lambda _url: text,
        snapshot_date=date(2026, 5, 31),
        run_id="run-1",
    )
    assert len(store.upserts) == 1
    up = store.upserts[0]
    assert up["replace_snapshot"] is True
    assert up["snapshot_date"] == date(2026, 5, 31)
    assert up["run_id"] == "run-1"
    assert up["csv_source_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    persisted = up["instruments"]
    assert isinstance(persisted, list)
    assert sorted(i.symbol_name for i in persisted) == [
        "NIFTY",
        "RELIANCE",
    ]  # option leg dropped
    assert store.prune_calls == 1
    assert result == RefreshResult(
        parsed=3,  # NSE/BSE rows seen: NIFTY + RELIANCE + RELIANCE-OPT leg
        persisted=2,  # universe upserted: NIFTY + RELIANCE
        legs_dropped=1,  # the option leg, not persisted in the lean universe
        filtered=0,  # no out-of-scope (MCX) rows here
        snapshot_date=date(2026, 5, 31),
        csv_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        pruned=7,
    )


def test_refresh_uses_custom_url() -> None:
    store = _Store()
    seen: list[str] = []

    def _get(url: str) -> str:
        seen.append(url)
        return _csv(_rows())

    refresh_instruments(
        store,
        _get,
        snapshot_date=date(2026, 5, 31),
        run_id="r",
        url="https://images.dhan.co/x.csv",
    )
    assert seen == ["https://images.dhan.co/x.csv"]


def test_refresh_records_instrument_run() -> None:
    """The load is recorded in instrument_runs with the parse census (DEC-INSTRUMENT-RUN-AUDIT-001)."""
    store = _Store()
    text = _csv(_rows())
    refresh_instruments(store, lambda _url: text, snapshot_date=date(2026, 5, 31), run_id="run-1")
    assert len(store.runs) == 1
    run = store.runs[0]
    assert run.run_id == "run-1"
    assert run.snapshot_date == date(2026, 5, 31)
    assert run.csv_source_sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert run.total_raw_rows == 3
    assert run.integrity_failures == 0
    assert run.in_scope_count == 3
    assert run.out_of_scope_count == 0
    assert run.index_count == 1  # NIFTY
    assert run.futures_count == 0  # no future leg in this fixture
    assert run.options_count == 1  # RELIANCE-OPT
    assert run.nse_count == 3
    assert run.bse_count == 0
    assert run.schema_version == SCHEMA_VERSION
    assert run.fetched_at.tzinfo is not None  # default clock is UTC-aware


def test_refresh_run_uses_injected_clock() -> None:
    """A fixed clock makes ``fetched_at`` deterministic (Rule #4)."""
    store = _Store()
    fixed = datetime(2026, 5, 31, 9, 15, tzinfo=UTC)
    refresh_instruments(
        store,
        lambda _url: _csv(_rows()),
        snapshot_date=date(2026, 5, 31),
        run_id="run-1",
        clock=lambda: fixed,
    )
    assert store.runs[0].fetched_at == fixed


def test_code_commit_sha_env_then_version_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """``BRUTEX_COMMIT_SHA`` wins; empty/absent falls back to ``__version__``."""
    monkeypatch.setenv("BRUTEX_COMMIT_SHA", "deadbeefcafe")
    assert _code_commit_sha() == "deadbeefcafe"
    monkeypatch.delenv("BRUTEX_COMMIT_SHA", raising=False)
    assert _code_commit_sha() == __version__
    monkeypatch.setenv("BRUTEX_COMMIT_SHA", "")  # empty → falsy → fallback
    assert _code_commit_sha() == __version__
