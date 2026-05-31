"""Tests for `brutex.adapters.dhan.instrument_refresh` — download → persist.

100% line+branch of `refresh_instruments` via a fake http_get + a fake
InstrumentSink (no network, no DuckDB).
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Sequence
from datetime import date

from brutex.adapters.dhan.instrument_refresh import RefreshResult, refresh_instruments
from brutex.models import Instrument

_HEADER = [
    "EXCH_ID",
    "SECURITY_ID",
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
            INSTRUMENT_TYPE="INDEX",
            SYMBOL_NAME="NIFTY",
            DISPLAY_NAME="Nifty 50",
        ),
        _row(
            EXCH_ID="NSE",
            SECURITY_ID="2885",
            INSTRUMENT_TYPE="EQUITY",
            SYMBOL_NAME="RELIANCE",
            DISPLAY_NAME="Reliance",
            LOT_SIZE="1",
            TICK_SIZE="0.05",
        ),
        _row(
            EXCH_ID="NSE",
            SECURITY_ID="3001",
            INSTRUMENT_TYPE="OPTSTK",
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
    assert sorted(i.trading_symbol for i in persisted) == [
        "NIFTY",
        "RELIANCE",
    ]  # option leg dropped
    assert store.prune_calls == 1
    assert result == RefreshResult(
        parsed=2,
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
