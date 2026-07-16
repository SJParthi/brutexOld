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
    STRIKE_COLLISION_EVENT,
    LakeOptionBarSource,
    _as_datetime,
    _as_decimal,
    _utc_instant,
)
from brutex.options.lot_size_history import OptionsUnderlying
from structlog.testing import capture_logs

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


# ── Decimal-equal strike collision (DEC-LAKE-SOURCE-STRIKE-COLLIDE-001) ─────
#: Two raw dir names decoding to Decimal-EQUAL strikes (``Decimal("24650") ==
#: Decimal("24650.0")`` and they hash alike → ONE index key). Sorted-walk
#: keep-policy: ``"-CE" < ".0-CE"`` so the ``24650.0`` spelling is the
#: lexicographically-LATER name and deterministically WINS.
_COLLIDE_SHADOWED = "NSE-NIFTY-02Jan25-24650-CE"
_COLLIDE_KEPT = "NSE-NIFTY-02Jan25-24650.0-CE"


@pytest.mark.parametrize(
    "write_order",
    [(_COLLIDE_SHADOWED, _COLLIDE_KEPT), (_COLLIDE_KEPT, _COLLIDE_SHADOWED)],
    ids=["shadowed-created-first", "kept-created-first"],
)
def test_strike_collision_warns_once_naming_both_and_still_resolves(
    tmp_path: Path, write_order: tuple[str, str]
) -> None:
    """Regression (fails-before): a Decimal-equal collision was a SILENT last-wins
    overwrite in OS-arbitrary ``iterdir()`` order. Now: exactly ONE
    ``lake_source.strike_collision`` WARNING names BOTH raw dirs via ``repr()``,
    and the index still resolves — deterministically to the lexicographically-later
    name — regardless of directory-creation order (never a fabricated price)."""
    lake = tmp_path / "lake"
    prices = {_COLLIDE_SHADOWED: "100.00", _COLLIDE_KEPT: "111.00"}
    for symbol in write_order:
        _write_contract(
            lake,
            exchange="NSE",
            symbol=symbol,
            bars=[_bar(_ENTRY, prices[symbol]), _bar(_EXIT, prices[symbol])],
        )
    src = LakeOptionBarSource(lake)
    with capture_logs() as logs:
        pair = src.load_pair(
            instrument=OptionsUnderlying.NIFTY,
            expiry_date=date(2025, 1, 2),
            strike=24650,
            option_type="CE",
            entry_ts=_ENTRY,
            exit_ts=_EXIT,
        )
    collisions = [entry for entry in logs if entry["event"] == STRIKE_COLLISION_EVENT]
    assert len(collisions) == 1  # one warning per collided PAIR (2-way ⇒ exactly one)
    warn = collisions[0]
    assert warn["log_level"] == "warning"
    # BOTH raw directory names are named via repr(), plus the decoded coordinates.
    assert warn["shadowed"] == repr(_COLLIDE_SHADOWED)
    assert warn["kept"] == repr(_COLLIDE_KEPT)
    assert warn["exchange"] == "NSE" and warn["underlying"] == "NIFTY"
    assert warn["expiry"] == "2025-01-02"
    assert warn["strike"] == "24650.0" and warn["option_type"] == "CE"
    # The index still resolves — to the KEPT contract's real stored bars.
    assert pair is not None
    assert pair.entry_bar.open == Decimal("111.00")
    index = src._symbol_index["NSE", "NIFTY"]
    assert index[date(2025, 1, 2), Decimal("24650"), "CE"] == _COLLIDE_KEPT


def test_three_way_collision_emits_one_warning_per_pair_naming_all_dirs(tmp_path: Path) -> None:
    """An N-way Decimal-equal collision emits N−1 warnings — every raw dir named.

    The honesty envelope of the pairwise WARN (DEC-LAKE-SOURCE-STRIKE-COLLIDE-001,
    reworded from the 2-way "exactly ONE" claim): three spellings of one strike
    (``24650`` / ``24650.0`` / ``24650.00``) emit TWO warnings, the intermediate
    winner appearing as ``kept`` in the first and ``shadowed`` in the second, so
    ALL THREE raw dirs are named across the warnings — no silent shadow.
    """
    three = [
        "NSE-NIFTY-02Jan25-24650-CE",
        "NSE-NIFTY-02Jan25-24650.0-CE",
        "NSE-NIFTY-02Jan25-24650.00-CE",
    ]
    assert sorted(three) == three  # the deterministic sorted-walk order this test pins
    lake = tmp_path / "lake"
    for symbol in three:
        _write_contract(
            lake,
            exchange="NSE",
            symbol=symbol,
            bars=[_bar(_ENTRY, "100.00"), _bar(_EXIT, "100.00")],
        )
    src = LakeOptionBarSource(lake)
    with capture_logs() as logs:
        index = src._build_symbol_index("NSE", "NIFTY")
    collisions = [entry for entry in logs if entry["event"] == STRIKE_COLLISION_EVENT]
    assert len(collisions) == 2  # N−1 pairwise warnings for an N-way collision
    assert collisions[0]["shadowed"] == repr(three[0])
    assert collisions[0]["kept"] == repr(three[1])
    assert collisions[1]["shadowed"] == repr(three[1])  # the intermediate winner, re-named
    assert collisions[1]["kept"] == repr(three[2])
    # Every colliding raw dir is named at least once across the warnings.
    named = {c["shadowed"] for c in collisions} | {c["kept"] for c in collisions}
    assert named == {repr(s) for s in three}
    # The final index resolves to the lexicographically-last spelling.
    assert index[date(2025, 1, 2), Decimal("24650"), "CE"] == three[-1]


def test_no_collision_emits_no_strike_collision_warning(lake_with_nifty_ce: Path) -> None:
    """A clean lake (distinct strikes only) never emits the collision event."""
    src = LakeOptionBarSource(lake_with_nifty_ce)
    with capture_logs() as logs:
        src._build_symbol_index("NSE", "NIFTY")
    assert not [entry for entry in logs if entry["event"] == STRIKE_COLLISION_EVENT]


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


# ── bounded LRU bar cache (DEC-MEMORY-FOOTPRINT-HARDENING-001) ───────────────
def _kw(strike: int) -> dict[str, object]:
    return {
        "instrument": OptionsUnderlying.NIFTY,
        "expiry_date": date(2025, 1, 2),
        "strike": strike,
        "option_type": "CE",
        "entry_ts": _ENTRY,
        "exit_ts": _EXIT,
    }


@pytest.fixture
def lake_with_three_ces(tmp_path: Path) -> Path:
    """Three NIFTY CE contracts (strikes 24000/24100/24200), distinct premiums."""
    lake = tmp_path / "lake"
    for strike, close in ((24000, "150.00"), (24100, "120.00"), (24200, "90.00")):
        _write_contract(
            lake,
            exchange="NSE",
            symbol=f"NSE-NIFTY-02Jan25-{strike}-CE",
            bars=[_bar(_ENTRY, close), _bar(_EXIT, close)],
        )
    return lake


def test_rejects_non_positive_bar_cache_capacity(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="bar_cache_capacity"):
        LakeOptionBarSource(tmp_path / "lake", bar_cache_capacity=0)


def test_bar_cache_capacity_is_respected(lake_with_three_ces: Path) -> None:
    """At most `bar_cache_capacity` contracts stay resident — never the pre-fix
    unbounded growth (one map per ever-touched contract)."""
    src = LakeOptionBarSource(lake_with_three_ces, bar_cache_capacity=2)
    for strike in (24000, 24100, 24200):
        assert src.load_pair(**_kw(strike)) is not None  # type: ignore[arg-type]
    assert len(src._bar_cache) == 2  # bounded — the pre-fix dict held all 3


def test_bar_cache_evicts_lru_not_the_recently_used(lake_with_three_ces: Path) -> None:
    """LRU order: a HIT refreshes recency, so the least-recently-USED map evicts."""
    src = LakeOptionBarSource(lake_with_three_ces, bar_cache_capacity=2)
    assert src.load_pair(**_kw(24000)) is not None  # type: ignore[arg-type]  # A
    assert src.load_pair(**_kw(24100)) is not None  # type: ignore[arg-type]  # B
    assert src.load_pair(**_kw(24000)) is not None  # type: ignore[arg-type]  # HIT A → B is LRU
    assert src.load_pair(**_kw(24200)) is not None  # type: ignore[arg-type]  # C evicts B, not A
    assert set(src._bar_cache) == {
        "NSE-NIFTY-02Jan25-24000-CE",
        "NSE-NIFTY-02Jan25-24200-CE",
    }


def test_hit_after_evict_recomputes_identical_bars(lake_with_three_ces: Path) -> None:
    """An evicted contract re-reads its immutable partition: byte-identical pair.

    Eviction = a recomputable read cache, never a changed result (the bounded-LRU
    correctness law of DEC-MEMORY-FOOTPRINT-HARDENING-001).
    """
    src = LakeOptionBarSource(lake_with_three_ces, bar_cache_capacity=1)
    first = src.load_pair(**_kw(24000))  # type: ignore[arg-type]
    assert first is not None
    # Touch another contract → the 24000 map is evicted (capacity 1).
    assert src.load_pair(**_kw(24100)) is not None  # type: ignore[arg-type]
    assert "NSE-NIFTY-02Jan25-24000-CE" not in src._bar_cache
    again = src.load_pair(**_kw(24000))  # type: ignore[arg-type]
    assert again is not None
    for field in (
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "open_interest",
        "side",
        "strike",
        "spot",
        "iv",
    ):
        assert getattr(again.entry_bar, field) == getattr(first.entry_bar, field)
        assert getattr(again.exit_bar, field) == getattr(first.exit_bar, field)


def test_default_capacity_is_generous(tmp_path: Path) -> None:
    """The default keeps 256 contracts resident (a strike chain's worth)."""
    src = LakeOptionBarSource(tmp_path / "lake")
    assert src._bar_cache_capacity == 256
