"""Tests for the `/db` expired-option pager TOTAL cache (DEC-LAKE-READ-O1-AUDIT-001).

The options-lake twin of the spot count cache (DEC-DB-CANDLE-COUNT-CACHE-001 #867).
The expired-option page ``total`` is a ``COUNT(*)`` over ``read_parquet(series-glob)``
— DuckDB answers it from each Parquet file's FOOTER, so it is **O(files)** and was
recomputed on EVERY paging click of the ``/db`` options tab (the gap the audit found:
``query_candles`` got a count cache in #867 but ``query_option_candles`` never did).
``query_option_candles`` now accepts an optional generation-stamped ``count_cache``
(the COMMON :class:`~brutex.dashboard.movers_cache.MoversSnapshotCache` reused as a
generic LRU): the total is invariant across paging / sort / column subsetting of one
``(series, start, end)``, so a prev/next click within a series is an **O(1) hit**, and
a pull bumps the generation → recompute (never a stale total).

These tests pin: the key helper (paging-invariant, bound-aware, series-distinct,
``opt|`` prefixed so it shares the spot cache without collision), the cached total is
**byte-identical** to the uncached path, the COUNT query runs **once** across pages of
the same series+generation (proven with a COUNT-counting connection), generation
invalidation, distinct series cached independently, and the empty series early-return
never touches the cache. Offline + hermetic (real Parquet via the production option
writer + a real DuckDB connection; no FastAPI, no network).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("duckdb", reason="the option-count cache reads a real Parquet lake")

import duckdb
from brutex.dashboard.db_view import (
    CandleCountCache,
    option_candle_count_key,
    query_option_candles,
)
from brutex.dashboard.movers_cache import MoversSnapshotCache
from brutex.data.models import OptionBar
from brutex.data.option_parquet_writer import OptionParquetWriter, OptionSeriesCoordinate

#: The filesystem-safe series segment the writer derives from this PK (``|`` → ``__``).
_OPT_PK = "NSE_FNO|13|WEEK|0|ATM|CALL"
_OPT_SERIES = "NSE_FNO__13__WEEK__0__ATM__CALL"


def _coord_for(partition_key: str, *, underlying_symbol: str = "NIFTY") -> OptionSeriesCoordinate:
    """Build a series coordinate from a ``|``-joined partition key (test helper)."""
    seg, sec, flag, code, offset, _typ = partition_key.split("|")
    return OptionSeriesCoordinate(
        exch_id="NSE",
        exchange_segment=seg,
        security_id=int(sec),
        underlying_symbol=underlying_symbol,
        expiry_flag=flag,
        expiry_code=int(code),
        strike_offset=offset,
    )


def _obar(ts: datetime, side: str, close: str) -> OptionBar:
    """One expired-option bar (full-precision iv, 2dp money like the writer)."""
    return OptionBar(
        timestamp=ts,
        open=Decimal("100.005"),
        high=Decimal("110.10"),
        low=Decimal("90.00"),
        close=Decimal(close),
        volume=250,
        open_interest=1200,
        side=side,
        strike=Decimal("17500.005"),
        spot=Decimal("17480.567"),
        iv=Decimal("0.1523"),
    )


def _write_options(lake_root: Path, partition_key: str = _OPT_PK, *, n_bars: int = 4) -> None:
    """Write *n_bars* CE bars (one per minute) for a series via the real writer."""
    bars = [_obar(datetime(2024, 3, 1, 4, m, tzinfo=UTC), "CE", "105.00") for m in range(n_bars)]
    OptionParquetWriter(lake_root=lake_root).write(
        partition_key=partition_key,
        coordinate=_coord_for(partition_key),
        bars=bars,
    )


def _opt(
    conn: Any,
    lake: Path,
    *,
    series: str = _OPT_SERIES,
    limit: int = 1,
    offset: int = 0,
    descending: bool = False,
    count_cache: CandleCountCache | None = None,
    generation: int = 0,
) -> dict[str, Any]:
    """Call ``query_option_candles`` over the fixed series with typed kwargs.

    A typed wrapper (NOT a ``dict`` spread) so the int/bool/cache params keep their
    static types — pyright cannot prove a ``dict[str, str]`` spread won't fill them.
    """
    return query_option_candles(
        conn,
        lake,
        series=series,
        limit=limit,
        offset=offset,
        descending=descending,
        count_cache=count_cache,
        generation=generation,
    )


class _CountingConn:
    """A DuckDB connection wrapper that counts ``COUNT(*)`` executions.

    Delegates everything to a real connection; only the option-pager total query
    (``SELECT COUNT(*) …``) increments :attr:`count_execs`, so a test can prove the
    total is computed ONCE across paging clicks of the same series + generation.
    """

    def __init__(self, real: Any) -> None:
        self._real = real
        self.count_execs = 0

    def execute(self, sql: str, parameters: Any = None) -> Any:
        # The deduped total is a COUNT(DISTINCT ...) since DEC-LAKE-READ-UNIQUE-001.
        if "COUNT(" in sql:
            self.count_execs += 1
        return (
            self._real.execute(sql) if parameters is None else self._real.execute(sql, parameters)
        )


@pytest.fixture
def conn() -> Any:
    """A real in-process DuckDB connection (read_parquet over the tmp lake)."""
    return duckdb.connect()


# ── option_candle_count_key ───────────────────────────────────────────────────────


def test_option_count_key_format_and_none_bounds() -> None:
    """The key joins ``opt|`` + series + bounds with ``|``; ``None`` bounds render empty."""
    assert option_candle_count_key(_OPT_SERIES, None, None) == f"opt|{_OPT_SERIES}||"
    assert (
        option_candle_count_key(_OPT_SERIES, "2024-03-01", "2024-03-31")
        == f"opt|{_OPT_SERIES}|2024-03-01|2024-03-31"
    )


def test_option_count_key_is_paging_invariant() -> None:
    """The key depends only on series+bounds — NOT limit/offset/sort/columns.

    (Those are not parameters of the helper, so two pages of the same series
    necessarily build the identical key — the property the cache relies on.)
    """
    k1 = option_candle_count_key(_OPT_SERIES, "2024-03-01", "2024-03-02")
    k2 = option_candle_count_key(_OPT_SERIES, "2024-03-01", "2024-03-02")
    assert k1 == k2


def test_option_count_key_distinct_per_series_and_bounds() -> None:
    """A different series OR different date bounds yields a different key."""
    base = option_candle_count_key(_OPT_SERIES, None, None)
    assert base != option_candle_count_key("NSE_FNO__99__MONTH__1__ATM__PUT", None, None)
    assert base != option_candle_count_key(_OPT_SERIES, "2024-03-01", None)


def test_option_count_key_namespaced_away_from_spot_key() -> None:
    """The ``opt|`` prefix keeps an option total from colliding with a spot key.

    The spot :func:`candle_count_key` never emits a leading ``opt|`` (it starts with a
    bare exchange token), so the two helpers can safely share ONE injected cache.
    """
    from brutex.dashboard.db_view import candle_count_key

    spot = candle_count_key("NSE", "FNO", _OPT_SERIES, "1minute", None, None)
    opt = option_candle_count_key(_OPT_SERIES, None, None)
    assert spot != opt
    assert opt.startswith("opt|")


def test_no_spot_exchange_can_collide_with_the_opt_prefix() -> None:
    """The collision-freedom invariant: NO allowed spot exchange begins ``opt``.

    A spot key opens with the partition ``exchange`` token, restricted to
    ``VALID_EXCHANGES`` by ``_validate_candle_coords`` before any key is built — so a
    spot key can never begin ``opt|``. Pinning the invariant the namespace relies on
    (so a future allowlist edit that added an ``opt…`` exchange would fail HERE).
    """
    from brutex.dashboard.db_view import VALID_EXCHANGES, candle_count_key

    assert all(not exch.lower().startswith("opt") for exch in VALID_EXCHANGES)
    for exch in VALID_EXCHANGES:
        spot = candle_count_key(exch, "CASH", _OPT_SERIES, "1minute", None, None)
        assert not spot.startswith("opt|")


# ── byte-identical total ──────────────────────────────────────────────────────────


def test_cached_total_is_byte_identical_to_uncached(conn: Any, tmp_path: Path) -> None:
    """The cached total equals the inline (no-cache) total exactly."""
    _write_options(tmp_path, n_bars=3)
    uncached = _opt(conn, tmp_path, limit=1)
    cached = _opt(conn, tmp_path, limit=1, count_cache=MoversSnapshotCache(), generation=0)
    assert uncached["total"] == cached["total"] == 3


def test_cached_page_payload_is_byte_identical(conn: Any, tmp_path: Path) -> None:
    """The full page payload (rows + total + columns) is identical with/without cache."""
    _write_options(tmp_path, n_bars=4)
    uncached = _opt(conn, tmp_path, limit=2, offset=1)
    cached = _opt(
        conn, tmp_path, limit=2, offset=1, count_cache=MoversSnapshotCache(), generation=0
    )
    assert uncached == cached


# ── O(1) hit: the COUNT runs once across pages ────────────────────────────────────


def test_count_runs_once_across_pages_same_generation(conn: Any, tmp_path: Path) -> None:
    """Paging the same series at one generation computes the COUNT exactly once."""
    _write_options(tmp_path, n_bars=4)
    counting = _CountingConn(conn)
    cache = MoversSnapshotCache()
    # Page 1 (offset 0) — a miss → one COUNT.
    p1 = _opt(counting, tmp_path, limit=2, offset=0, count_cache=cache, generation=7)
    # Page 2 (offset 2), and a sort flip — all HITs → no new COUNT.
    p2 = _opt(counting, tmp_path, limit=2, offset=2, count_cache=cache, generation=7)
    p3 = _opt(
        counting, tmp_path, limit=2, offset=0, descending=True, count_cache=cache, generation=7
    )
    assert p1["total"] == p2["total"] == p3["total"] == 4
    assert counting.count_execs == 1, "the O(files) COUNT must run once, then serve cached"


def test_generation_bump_recomputes_the_total(conn: Any, tmp_path: Path) -> None:
    """A new generation (a pull landed) invalidates the cached total → recount."""
    _write_options(tmp_path, n_bars=2)
    counting = _CountingConn(conn)
    cache = MoversSnapshotCache()
    first = _opt(counting, tmp_path, limit=1, count_cache=cache, generation=1)
    assert first["total"] == 2
    assert counting.count_execs == 1
    # A pull writes more bars AND bumps the generation → the cache is stale → recount.
    _write_options(tmp_path, n_bars=3)
    second = _opt(counting, tmp_path, limit=1, count_cache=cache, generation=2)
    assert second["total"] == 3, "a fresh generation must reflect the new bars"
    assert counting.count_execs == 2, "the bumped generation must force a recount"


def test_distinct_series_cached_independently(conn: Any, tmp_path: Path) -> None:
    """Two series keep separate cached totals (the key includes the series segment)."""
    _write_options(tmp_path, partition_key=_OPT_PK, n_bars=2)
    other_pk = "NSE_FNO|99|MONTH|1|ATM|PUT"
    other_series = "NSE_FNO__99__MONTH__1__ATM__PUT"
    _write_options(tmp_path, partition_key=other_pk, n_bars=3)
    cache = MoversSnapshotCache()
    first = _opt(conn, tmp_path, series=_OPT_SERIES, limit=1, count_cache=cache, generation=0)
    second = _opt(conn, tmp_path, series=other_series, limit=1, count_cache=cache, generation=0)
    assert first["total"] == 2
    assert second["total"] == 3
    # TWO entries per series: the count ("opt|…") PLUS the default-range
    # stored-life span ("optspan|…") — the span memo added by DEC-DB-UX-PACK-001
    # (the hostile-review MAJOR: paging must never re-run the MIN/MAX scan).
    assert cache.size() == 4


def test_empty_series_never_touches_the_cache(conn: Any, tmp_path: Path) -> None:
    """An empty/not-yet-pulled series early-returns total 0 without caching."""
    cache = MoversSnapshotCache()
    out = _opt(conn, tmp_path, limit=1, count_cache=cache, generation=0)
    assert out["total"] == 0
    assert cache.size() == 0, "the empty-series early-return must not populate the cache"


def test_count_cache_is_optional_default_is_pure(conn: Any, tmp_path: Path) -> None:
    """With no cache injected (the default) the COUNT runs inline every call."""
    _write_options(tmp_path, n_bars=2)
    counting = _CountingConn(conn)
    _opt(counting, tmp_path, limit=1)
    _opt(counting, tmp_path, limit=1)
    assert counting.count_execs == 2, (
        "no cache → the COUNT runs on every call (unchanged behaviour)"
    )
