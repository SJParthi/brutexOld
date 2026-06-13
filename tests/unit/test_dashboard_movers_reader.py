"""Unit tests for the ``/movers`` lake reader + payload builder (DEC-MOVERS-DASHBOARD-PAGE-001).

The pure surface — window parsing, JSON projection, the honest note, and the
``run_movers_screen`` empty-lake + catalog-unavailable branches — is exercised
here with an in-memory fake reader (no I/O); the real ``Catalog`` reconcile +
``get_bars`` path is proven end-to-end through the route in
``tests/integration/test_dashboard_movers.py``.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.dashboard.movers_reader import (
    DEFAULT_INTERVAL,
    DEFAULT_RECENT_BARS,
    DEFAULT_TOP_N,
    _decimal_str,
    build_movers_payload,
    empty_movers_payload,
    parse_window,
    run_movers_screen,
)
from brutex.data.errors import CatalogError
from brutex.data.models import CandleBar
from brutex.screeners import InstrumentRef, ScreenTarget

_START = datetime(2024, 1, 1, tzinfo=UTC)
_END = datetime(2024, 12, 31, tzinfo=UTC)


def _bar(*, day: int, open_: str, close: str, volume: int) -> CandleBar:
    """A valid daily OHLCV bar on ``2024-01-<day>`` (09:15 IST ≈ 03:45 UTC)."""
    o = Decimal(open_)
    c = Decimal(close)
    return CandleBar(
        timestamp=datetime(2024, 1, day, 3, 45, tzinfo=UTC),
        open=o,
        high=max(o, c),
        low=min(o, c),
        close=c,
        volume=volume,
        open_interest=None,
    )


@dataclass(slots=True)
class _FakeReader:
    """In-memory ``_MoversReader`` — maps a symbol to its window bars (no I/O).

    Counts reads per ``(symbol, "window"|"bars")`` so a test can prove the two
    screens reuse the SAME series (the production reader's request-scoped cache
    is what makes that a single disk read; the fake just records the calls).
    """

    bars_by_symbol: dict[str, list[CandleBar]]
    interval: str = "1day"
    reads: list[tuple[str, str]] = field(default_factory=list)

    def list_instruments(self) -> list[InstrumentRef]:
        return [
            InstrumentRef(symbol=s, exchange="NSE", segment="INDEX") for s in self.bars_by_symbol
        ]

    def list_screen_targets(self) -> list[ScreenTarget]:
        return [
            ScreenTarget(exchange="NSE", segment="INDEX", symbol=s, interval=self.interval)
            for s in self.bars_by_symbol
        ]

    def read_window(
        self, ref: InstrumentRef, *, interval: str, start: datetime, end: datetime
    ) -> list[CandleBar]:
        self.reads.append((ref.symbol, "window"))
        return self.bars_by_symbol.get(ref.symbol, [])

    def read_bars(self, target: ScreenTarget, *, start: datetime, end: datetime) -> list[CandleBar]:
        self.reads.append((target.symbol, "bars"))
        return self.bars_by_symbol.get(target.symbol, [])


def _rich_reader() -> _FakeReader:
    """A two-instrument reader: NIFTY gains +10%, BANKNIFTY loses -10% (with volume)."""
    return _FakeReader(
        bars_by_symbol={
            "NIFTY": [
                _bar(day=2, open_="20000", close="20100", volume=100),
                _bar(day=3, open_="20100", close="20100", volume=100),
                _bar(day=4, open_="20100", close="22000", volume=500),
            ],
            "BANKNIFTY": [
                _bar(day=2, open_="45000", close="44000", volume=80),
                _bar(day=3, open_="44000", close="44000", volume=80),
                _bar(day=4, open_="44000", close="40500", volume=90),
            ],
        }
    )


# ── parse_window ────────────────────────────────────────────────────────


def test_parse_window_defaults_are_utc_aware() -> None:
    start, end = parse_window(None, None)
    assert start == datetime(2021, 1, 1, tzinfo=UTC)
    assert end.tzinfo is UTC
    assert start <= end


def test_parse_window_explicit_bounds_are_inclusive_utc() -> None:
    start, end = parse_window("2024-03-01", "2024-03-31")
    assert start == datetime(2024, 3, 1, 0, 0, 0, tzinfo=UTC)
    # The 'to' bound snaps to end-of-day so the whole day is inclusive.
    assert end == datetime(2024, 3, 31, 23, 59, 59, tzinfo=UTC)


def test_parse_window_rejects_from_after_to() -> None:
    with pytest.raises(ValueError, match="must be on or before"):
        parse_window("2024-03-31", "2024-03-01")


def test_parse_window_rejects_malformed_date() -> None:
    with pytest.raises(ValueError, match="malformed date"):
        parse_window("not-a-date", None)


# ── _decimal_str ─────────────────────────────────────────────────────────


def test_decimal_str_keeps_full_precision_and_passes_none_through() -> None:
    # Statistical metrics are never money-rounded (CLAUDE.md §4) — full precision.
    assert _decimal_str(Decimal("1.234567890123")) == "1.234567890123"
    assert _decimal_str(None) is None


# ── build_movers_payload ─────────────────────────────────────────────────


def test_build_payload_ranks_gainers_losers_volume_volatility() -> None:
    reader = _rich_reader()
    payload = build_movers_payload(
        reader, interval="1day", start=_START, end=_END, top_n=5, recent_bars=1
    )
    assert payload["instrument_count"] == 2
    assert payload["skipped_count"] == 0
    # Gainer / loser split, money rendered 2dp via brutex.money.
    (gain,) = payload["gainers"]
    assert gain["symbol"] == "NIFTY"
    assert gain["base_price"] == "₹20,000.00"
    assert gain["last_price"] == "₹22,000.00"
    assert Decimal(gain["pct_change"]) == Decimal("10")
    assert gain["bar_count"] == 3
    (lose,) = payload["losers"]
    assert lose["symbol"] == "BANKNIFTY"
    assert Decimal(lose["pct_change"]) == Decimal("-10")
    # Both instruments have volume + range → present in both metric tables.
    assert {r["symbol"] for r in payload["volume_movers"]} == {"NIFTY", "BANKNIFTY"}
    assert {r["symbol"] for r in payload["volatility_movers"]} == {"NIFTY", "BANKNIFTY"}
    # The volume/volatility rows carry a full-precision metric + a ₹ last close.
    vrow = payload["volume_movers"][0]
    assert vrow["last_close"].startswith("₹")
    assert vrow["value"] is not None
    assert "engine" in payload["note"]


def test_build_payload_reuses_each_series_across_both_screens() -> None:
    # The two screens together read each instrument once per seam (window + bars):
    # the production reader's cache collapses those to a single disk read.
    reader = _rich_reader()
    build_movers_payload(reader, interval="1day", start=_START, end=_END, top_n=5, recent_bars=1)
    assert sorted(reader.reads) == [
        ("BANKNIFTY", "bars"),
        ("BANKNIFTY", "window"),
        ("NIFTY", "bars"),
        ("NIFTY", "window"),
    ]


def test_build_payload_empty_reader_says_pull_spot_first() -> None:
    reader = _FakeReader(bars_by_symbol={})
    payload = build_movers_payload(
        reader, interval="1day", start=_START, end=_END, top_n=5, recent_bars=1
    )
    assert payload["instrument_count"] == 0
    assert payload["gainers"] == []
    assert payload["losers"] == []
    assert payload["volume_movers"] == []
    assert payload["volatility_movers"] == []
    assert "pull spot first" in payload["note"]


def test_build_payload_data_present_but_window_empty_is_honest() -> None:
    # An instrument exists but the window has no bars → all four tables empty,
    # but the note distinguishes this from an empty lake (Golden Rule #10).
    reader = _FakeReader(bars_by_symbol={"NIFTY": []})
    payload = build_movers_payload(
        reader, interval="1day", start=_START, end=_END, top_n=5, recent_bars=1
    )
    assert payload["instrument_count"] == 1
    assert payload["skipped_count"] == 1
    assert payload["gainers"] == []
    assert payload["volume_movers"] == []
    assert "none had usable bars" in payload["note"]


def test_build_payload_zero_volume_index_is_volatility_only() -> None:
    # A zero-volume INDEX has an undefined volume surge (no baseline volume) but a
    # defined realised-range volatility → it lands in volatility-movers ONLY.
    reader = _FakeReader(
        bars_by_symbol={
            "NIFTY": [
                _bar(day=2, open_="20000", close="20100", volume=0),
                _bar(day=3, open_="20100", close="20300", volume=0),
            ]
        }
    )
    payload = build_movers_payload(
        reader, interval="1day", start=_START, end=_END, top_n=5, recent_bars=1
    )
    assert payload["volume_movers"] == []
    assert [r["symbol"] for r in payload["volatility_movers"]] == ["NIFTY"]


# ── empty_movers_payload ─────────────────────────────────────────────────


def test_empty_movers_payload_shape() -> None:
    payload = empty_movers_payload(
        interval="1day",
        start=_START,
        end=_END,
        top_n=DEFAULT_TOP_N,
        recent_bars=DEFAULT_RECENT_BARS,
    )
    assert payload["interval"] == "1day"
    assert payload["start"] == _START.isoformat()
    assert payload["end"] == _END.isoformat()
    assert payload["instrument_count"] == 0
    assert payload["skipped_count"] == 0
    assert "pull spot first" in payload["note"]


def test_module_defaults() -> None:
    assert DEFAULT_INTERVAL == "1day"
    assert DEFAULT_TOP_N == 10
    assert DEFAULT_RECENT_BARS == 5


# ── run_movers_screen — empty + catalog-unavailable branches ─────────────


def test_run_screen_empty_lake_short_circuits(tmp_path: Path) -> None:
    # No bars/ tree at all → no datasets → empty payload, no catalog opened.
    payload = run_movers_screen(
        lake_root=tmp_path,
        interval="1day",
        start=_START,
        end=_END,
        top_n=5,
        recent_bars=1,
    )
    assert payload["instrument_count"] == 0
    assert "pull spot first" in payload["note"]
    assert not (tmp_path / "catalog.duckdb").exists()


def test_run_screen_other_interval_only_is_empty(tmp_path: Path) -> None:
    # A lake holding ONLY a 1minute dataset is empty for a 1day screen.
    leaf = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2024" / "1.parquet"
    leaf.parent.mkdir(parents=True)
    leaf.touch()
    payload = run_movers_screen(
        lake_root=tmp_path, interval="1day", start=_START, end=_END, top_n=5, recent_bars=1
    )
    assert payload["instrument_count"] == 0


def test_run_screen_reports_busy_when_catalog_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Datasets exist (a placeholder parquet makes the dataset discoverable), but
    # the catalog can't be opened (busy / data deps missing) → honest note, no 500.
    leaf = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1day" / "2024" / "1.parquet"
    leaf.parent.mkdir(parents=True)
    leaf.touch()

    def _boom(_path: Path) -> object:
        raise CatalogError("database is locked")

    monkeypatch.setattr("brutex.data.catalog.Catalog.open", _boom)
    payload = run_movers_screen(
        lake_root=tmp_path, interval="1day", start=_START, end=_END, top_n=5, recent_bars=1
    )
    assert payload["instrument_count"] == 1
    assert "catalog unavailable" in payload["note"].lower()
    assert payload["gainers"] == []
