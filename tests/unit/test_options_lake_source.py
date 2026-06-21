"""Unit tests for the F&O candle-lake option-bar source (DEC-OPTIONS-LAKE-READER-001).

:class:`brutex.options.lake_source.LakeOptionBarSource` is the production
``OptionBarSource`` the executor injects: it resolves a leg's contract in the F&O
candle lake (written by the production :class:`~brutex.data.sink.ParquetBarSink`) and
returns the entry/exit :class:`~brutex.options.executor.OptionBarPair`, or ``None``
when absent — never a fabricated price (Golden Rule #1). Exercised against a REAL
tmp lake; no network.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.options.lake_source import (
    LakeOptionBarSource,
    _as_datetime,
    _as_decimal,
    _utc_instant,
)
from brutex.options.lot_size_history import OptionsUnderlying

_IST = ZoneInfo("Asia/Kolkata")
#: 09:15 IST entry + 15:20 IST forced exit on 2025-01-02 (Track-1 exit, CLAUDE.md §7).
_ENTRY = datetime(2025, 1, 2, 9, 15, tzinfo=_IST)
_EXIT = datetime(2025, 1, 2, 15, 20, tzinfo=_IST)


def _bar(ts_ist: datetime, close: str, *, oi: int | None = 1000) -> CandleBar:
    c = Decimal(close)
    return CandleBar(
        timestamp=ts_ist.astimezone(UTC),  # the lake stores UTC
        open=c,
        high=c + Decimal("1.00"),
        low=c - Decimal("1.00"),
        close=c,
        volume=50,
        open_interest=oi,
    )


def _write_contract(
    lake: Path, *, exchange: str, symbol: str, bars: list[CandleBar], interval: str = "1minute"
) -> None:
    ParquetBarSink(writer=ParquetWriter(lake_root=lake)).append(
        exchange=exchange, segment="FNO", symbol=symbol, interval=interval, bars=bars
    )


@pytest.fixture
def lake_with_nifty_ce(tmp_path: Path) -> Path:
    """A lake holding NSE-NIFTY-02Jan25-24000-CE with an entry (150) + exit (210) bar."""
    lake = tmp_path / "lake"
    _write_contract(
        lake,
        exchange="NSE",
        symbol="NSE-NIFTY-02Jan25-24000-CE",
        bars=[_bar(_ENTRY, "150.00", oi=1000), _bar(_EXIT, "210.00", oi=900)],
    )
    return lake


# ── happy path ────────────────────────────────────────────────────────────
def test_load_pair_returns_entry_exit_bars(lake_with_nifty_ce: Path) -> None:
    src = LakeOptionBarSource(lake_with_nifty_ce)
    pair = src.load_pair(
        instrument=OptionsUnderlying.NIFTY,
        expiry_date=date(2025, 1, 2),
        strike=24000,
        option_type="CE",
        entry_ts=_ENTRY,
        exit_ts=_EXIT,
    )
    assert pair is not None
    # Entry/exit OPEN premiums (the executor prices the round-trip off the OPEN).
    assert pair.entry_bar.open == Decimal("150.00")
    assert pair.exit_bar.open == Decimal("210.00")
    # The leg side + strike are stamped from the query; spot/iv are honest None.
    assert pair.entry_bar.side == "CE" and pair.exit_bar.side == "CE"
    assert pair.entry_bar.strike == Decimal("24000")
    assert pair.entry_bar.spot is None and pair.entry_bar.iv is None
    assert pair.entry_bar.open_interest == 1000 and pair.exit_bar.open_interest == 900


def test_load_pair_pe_mirror(tmp_path: Path) -> None:
    lake = tmp_path / "lake"
    _write_contract(
        lake,
        exchange="NSE",
        symbol="NSE-BANKNIFTY-02Jan25-48000-PE",
        bars=[_bar(_ENTRY, "300.00"), _bar(_EXIT, "120.00")],
    )
    src = LakeOptionBarSource(lake)
    pair = src.load_pair(
        instrument=OptionsUnderlying.BANKNIFTY,
        expiry_date=date(2025, 1, 2),
        strike=48000,
        option_type="PE",
        entry_ts=_ENTRY,
        exit_ts=_EXIT,
    )
    assert pair is not None
    assert pair.entry_bar.side == "PE" and pair.entry_bar.strike == Decimal("48000")


def test_sensex_resolves_under_bse(tmp_path: Path) -> None:
    # SENSEX F&O lives under BSE — the exchange mapping must pick it up.
    lake = tmp_path / "lake"
    _write_contract(
        lake,
        exchange="BSE",
        symbol="BSE-SENSEX-02Jan25-79000-CE",
        bars=[_bar(_ENTRY, "500.00"), _bar(_EXIT, "650.00")],
    )
    src = LakeOptionBarSource(lake)
    pair = src.load_pair(
        instrument=OptionsUnderlying.SENSEX,
        expiry_date=date(2025, 1, 2),
        strike=79000,
        option_type="CE",
        entry_ts=_ENTRY,
        exit_ts=_EXIT,
    )
    assert pair is not None and pair.entry_bar.open == Decimal("500.00")


# ── honest misses (None, never faked) ───────────────────────────────────────
def test_contract_not_in_lake_is_none(lake_with_nifty_ce: Path) -> None:
    src = LakeOptionBarSource(lake_with_nifty_ce)
    # A strike that was never ingested → no contract → None.
    assert (
        src.load_pair(
            instrument=OptionsUnderlying.NIFTY,
            expiry_date=date(2025, 1, 2),
            strike=25000,  # absent
            option_type="CE",
            entry_ts=_ENTRY,
            exit_ts=_EXIT,
        )
        is None
    )


def test_missing_entry_bar_is_none(lake_with_nifty_ce: Path) -> None:
    # The contract exists, but no bar at the requested entry minute → None.
    src = LakeOptionBarSource(lake_with_nifty_ce)
    other_minute = datetime(2025, 1, 2, 9, 16, tzinfo=_IST)  # no bar written here
    assert (
        src.load_pair(
            instrument=OptionsUnderlying.NIFTY,
            expiry_date=date(2025, 1, 2),
            strike=24000,
            option_type="CE",
            entry_ts=other_minute,
            exit_ts=_EXIT,
        )
        is None
    )


def test_missing_exit_bar_is_none(lake_with_nifty_ce: Path) -> None:
    src = LakeOptionBarSource(lake_with_nifty_ce)
    no_exit = datetime(2025, 1, 2, 15, 25, tzinfo=_IST)  # no bar here
    assert (
        src.load_pair(
            instrument=OptionsUnderlying.NIFTY,
            expiry_date=date(2025, 1, 2),
            strike=24000,
            option_type="CE",
            entry_ts=_ENTRY,
            exit_ts=no_exit,
        )
        is None
    )


def test_empty_lake_is_none(tmp_path: Path) -> None:
    # No bars/<exch>/FNO dir at all → the symbol index is empty → None.
    src = LakeOptionBarSource(tmp_path / "lake")
    assert (
        src.load_pair(
            instrument=OptionsUnderlying.NIFTY,
            expiry_date=date(2025, 1, 2),
            strike=24000,
            option_type="CE",
            entry_ts=_ENTRY,
            exit_ts=_EXIT,
        )
        is None
    )


def test_contract_dir_without_parquet_is_none(lake_with_nifty_ce: Path) -> None:
    # A contract dir present (so it INDEXES) but with no 1minute parquet → empty bar
    # map → None (the base.is_dir()/any-parquet guard's empty branch).
    (lake_with_nifty_ce / "bars" / "NSE" / "FNO" / "NSE-NIFTY-02Jan25-26000-CE").mkdir(parents=True)
    src = LakeOptionBarSource(lake_with_nifty_ce)
    assert (
        src.load_pair(
            instrument=OptionsUnderlying.NIFTY,
            expiry_date=date(2025, 1, 2),
            strike=26000,
            option_type="CE",
            entry_ts=_ENTRY,
            exit_ts=_EXIT,
        )
        is None
    )


# ── index hygiene: futures / undecodable / files / other underlyings skipped ─
def test_future_and_junk_dirs_excluded_from_index(tmp_path: Path) -> None:
    lake = tmp_path / "lake"
    # A real CE we WILL match…
    _write_contract(
        lake, exchange="NSE", symbol="NSE-NIFTY-02Jan25-24000-CE", bars=[_bar(_ENTRY, "150.00")]
    )
    # …a FUTURE (no option_type → skipped)…
    _write_contract(
        lake, exchange="NSE", symbol="NSE-NIFTY-30Jan25-FUT", bars=[_bar(_ENTRY, "24010.00")]
    )
    # …a DIFFERENT underlying's option (filtered out for NIFTY)…
    _write_contract(
        lake, exchange="NSE", symbol="NSE-RELIANCE-02Jan25-1300-CE", bars=[_bar(_ENTRY, "20.00")]
    )
    fno = lake / "bars" / "NSE" / "FNO"
    # …an undecodable contract dir (bad expiry token → parse None → skipped)…
    (fno / "NSE-NIFTY-BADX-CE").mkdir(parents=True)
    # …and a stray FILE in the FNO dir (not a dir → skipped).
    (fno / "stray.txt").write_text("x", encoding="utf-8")

    src = LakeOptionBarSource(lake)
    index = src._build_symbol_index("NSE", "NIFTY")
    # ONLY the NIFTY CE option is indexed; FUT / RELIANCE / junk / file excluded.
    assert index == {(date(2025, 1, 2), Decimal("24000"), "CE"): "NSE-NIFTY-02Jan25-24000-CE"}


# ── caching (O(1) on repeat) ────────────────────────────────────────────────
def test_symbol_index_and_bars_are_cached(lake_with_nifty_ce: Path) -> None:
    src = LakeOptionBarSource(lake_with_nifty_ce)
    kw = {
        "instrument": OptionsUnderlying.NIFTY,
        "expiry_date": date(2025, 1, 2),
        "strike": 24000,
        "option_type": "CE",
        "entry_ts": _ENTRY,
        "exit_ts": _EXIT,
    }
    first = src.load_pair(**kw)  # type: ignore[arg-type]
    # Caches are now populated (index per (exch, underlying); bars per contract).
    assert ("NSE", "NIFTY") in src._symbol_index
    assert "NSE-NIFTY-02Jan25-24000-CE" in src._bar_cache
    second = src.load_pair(**kw)  # type: ignore[arg-type]  # hits both cache-hit branches
    assert first is not None and second is not None
    assert first.entry_bar.open == second.entry_bar.open == Decimal("150.00")


# ── pure helpers ────────────────────────────────────────────────────────────
def test_utc_instant_naive_is_read_as_utc() -> None:
    naive = datetime(2025, 1, 2, 3, 45)  # no tzinfo
    assert _utc_instant(naive) == datetime(2025, 1, 2, 3, 45, tzinfo=UTC)
    aware = datetime(2025, 1, 2, 9, 15, tzinfo=_IST)
    assert _utc_instant(aware) == datetime(2025, 1, 2, 3, 45, tzinfo=UTC)


def test_as_datetime_naive_becomes_utc() -> None:
    assert _as_datetime(datetime(2025, 1, 2, 3, 45)) == datetime(2025, 1, 2, 3, 45, tzinfo=UTC)


def test_as_decimal_coerces_float_and_passes_decimal() -> None:
    assert _as_decimal(150.5) == Decimal("150.5")
    assert _as_decimal(Decimal("210.00")) == Decimal("210.00")
