"""Property tests for the stats-manifest core (DEC-LAKE-STATS-MANIFEST-001).

Four invariants over randomly-shaped datasets (both kinds, random months, row
counts, in-month bounds, file sizes):

1. **Round-trip fidelity** — a read-side seed followed by the FULL read-time
   revalidation returns exactly the seeded statistics (``row_count == Σ rows``,
   files sorted ascending by ``rel``, aggregate bounds = the per-file fold).
2. **Single-mutation fail-closed** — ANY single divergence between the shard
   and the live directory (a touched file, an out-of-band new file, a deleted
   file, a corrupt/truncated shard, a foreign-month-poisoned stamp) makes the
   loader return ``None`` with a ``lake.stats_manifest.fallback`` event —
   never a silently-wrong number (the anti-illusion law).
3. **Randomized query equivalence** (review round 1 — the spec §10 item) —
   over a REAL random lake (months × bars, optionally poisoned) × random
   ``(bounds, offset, limit, direction)``: the manifest-opted
   ``query_candles`` payload equals the legacy payload cell-for-cell; a
   poisoned dataset ALWAYS surfaces an explicit fallback (never a silent
   divergence); and ``Σ pages == total`` when walking the whole result set at
   the drawn page size. Skipped when duckdb/polars are absent.
4. **Concurrent-writer thread fuzz** — N threads upserting DIFFERENT months
   through the real shard flock converge to one shard carrying every row with
   consistent aggregates (no lost update, no torn shard).
"""

# pyright: reportCallIssue=false
# Rationale (DEC-PYRIGHT-UNMASK-001 precedent): the Hypothesis `@st.composite`
# strategy below (`_dataset_spec`) is called WITHOUT the `draw` argument —
# Hypothesis injects it at runtime. pyright does not model the `@composite`
# signature transform and reports a false "Argument missing for parameter
# draw" at every `@given(...=_strategy())` site. Scoped to this file only.

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import UTC, date, datetime, timedelta
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import pytest
from brutex.data import lake_stats_manifest as lsm
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

pytestmark = pytest.mark.property

#: The query-equivalence property drives the REAL /db pager over a REAL
#: Parquet lake, so it needs the full lake stack; the leaf-module properties
#: above it stay runnable without it (find_spec — no import side effects).
_HAS_LAKE_STACK = all(find_spec(name) is not None for name in ("duckdb", "polars"))

BARS_KEY: dict[str, str] = {
    "exchange": "NSE",
    "segment": "INDEX",
    "symbol": "NIFTY",
    "interval": "1minute",
}
OPTIONS_KEY: dict[str, str] = {"series": "FNO__NSE__IDX__NIFTY"}

_MONTHS = st.lists(
    st.tuples(st.integers(min_value=2020, max_value=2027), st.integers(min_value=1, max_value=12)),
    min_size=1,
    max_size=5,
    unique=True,
)


@st.composite
def _dataset_spec(draw: st.DrawFn) -> tuple[str, list[tuple[int, int, int, int, int, int]]]:
    """Draw ``(kind, [(year, month, rows, lo_day, hi_day, size), ...])``."""
    kind = draw(st.sampled_from([lsm.KIND_BARS, lsm.KIND_OPTIONS]))
    months = draw(_MONTHS)
    specs: list[tuple[int, int, int, int, int, int]] = []
    for year, month in sorted(months):
        rows = draw(st.integers(min_value=1, max_value=500))
        day_a = draw(st.integers(min_value=1, max_value=28))
        day_b = draw(st.integers(min_value=1, max_value=28))
        lo, hi = sorted((day_a, day_b))
        size = draw(st.integers(min_value=1, max_value=64))
        specs.append((year, month, rows, lo, hi, size))
    return kind, specs


def _materialize(
    lake: Path, kind: str, specs: list[tuple[int, int, int, int, int, int]]
) -> list[lsm.StatsFileRow]:
    """Create real partition files + stat-accurate rows for *specs*."""
    key = BARS_KEY if kind == lsm.KIND_BARS else OPTIONS_KEY
    ddir = lsm.dataset_dir(lake, kind, key)
    rows: list[lsm.StatsFileRow] = []
    for year, month, count, lo, hi, size in specs:
        rel = (
            f"{year:04d}/{month:02d}.parquet"
            if kind == lsm.KIND_BARS
            else f"{year:04d}-{month:02d}.parquet"
        )
        dest = ddir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x" * size)
        stat = os.stat(dest)
        rows.append(
            lsm.StatsFileRow(
                rel=rel,
                rows=count,
                min_ts=datetime(year, month, lo, 9, 15, tzinfo=UTC),
                max_ts=datetime(year, month, hi, 10, 0, tzinfo=UTC),
                mtime_ns=stat.st_mtime_ns,
                size=stat.st_size,
                raw_hash=None,
            )
        )
    return rows


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(spec=_dataset_spec())
def test_seed_then_load_roundtrip(
    spec: tuple[str, list[tuple[int, int, int, int, int, int]]],
) -> None:
    """Seed → full revalidation returns exactly the seeded statistics."""
    kind, specs = spec
    key = BARS_KEY if kind == lsm.KIND_BARS else OPTIONS_KEY
    with tempfile.TemporaryDirectory() as tmp:
        lake = Path(tmp)
        rows = _materialize(lake, kind, specs)
        assert lsm.seed_dataset_stats(lake, kind=kind, key=key, files=rows)
        stats = lsm.load_valid_dataset_stats(lake, kind=kind, key=key)
        assert stats is not None
        assert stats.row_count == sum(row.rows for row in rows)
        rels = [row.rel for row in stats.files]
        assert rels == sorted(rels)
        assert set(rels) == {row.rel for row in rows}
        assert stats.min_ts == min(row.min_ts for row in rows)
        assert stats.max_ts == max(row.max_ts for row in rows)
        assert stats.identity_key == lsm.STATS_IDENTITY_KEYS[kind]


def _apply_mutation(lake: Path, kind: str, key: dict[str, str], mutation: str) -> None:
    """Introduce exactly one shard↔directory divergence."""
    ddir = lsm.dataset_dir(lake, kind, key)
    shard = lsm.stats_shard_path(lake, kind, key)
    obj: dict[str, Any] = json.loads(shard.read_text(encoding="utf-8"))
    first_rel: str = obj["files"][0]["rel"]
    if mutation == "touch":
        target = ddir / first_rel
        stat = os.stat(target)
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    elif mutation == "extra_file":
        extra = ddir / ("1999/01.parquet" if kind == lsm.KIND_BARS else "1999-01.parquet")
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_bytes(b"out-of-band")
    elif mutation == "remove_file":
        (ddir / first_rel).unlink()
    elif mutation == "corrupt_shard":
        shard.write_text("{corrupt", encoding="utf-8")
    elif mutation == "truncate_shard":
        body = shard.read_text(encoding="utf-8")
        shard.write_text(body[: len(body) // 2], encoding="utf-8")
    else:  # poison_month: shift one stamped bound outside its rel month
        row = obj["files"][0]
        start = datetime.fromisoformat(str(row["min_ts"]))
        # Strictly before the month start ⇒ a foreign-month stamp. files[0] is
        # the chronologically earliest rel (zero-padded sort), so the dataset
        # aggregate min is exactly the poisoned bound — the shard stays
        # structurally consistent and ONLY the month-bounds clause can catch it.
        poisoned = start.replace(day=1, hour=0, minute=0, second=0) - timedelta(minutes=1)
        row["min_ts"] = poisoned.isoformat()
        obj["min_ts"] = poisoned.isoformat()
        shard.write_text(json.dumps(obj, sort_keys=True), encoding="utf-8")


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    spec=_dataset_spec(),
    mutation=st.sampled_from(
        ["touch", "extra_file", "remove_file", "corrupt_shard", "truncate_shard", "poison_month"]
    ),
)
def test_any_single_mutation_falls_back(
    spec: tuple[str, list[tuple[int, int, int, int, int, int]]], mutation: str
) -> None:
    """Any single divergence ⇒ ``None`` + a fallback event — never a wrong number."""
    kind, specs = spec
    key = BARS_KEY if kind == lsm.KIND_BARS else OPTIONS_KEY
    with tempfile.TemporaryDirectory() as tmp:
        lake = Path(tmp)
        rows = _materialize(lake, kind, specs)
        assert lsm.seed_dataset_stats(lake, kind=kind, key=key, files=rows)
        _apply_mutation(lake, kind, key, mutation)
        with capture_logs() as logs:
            assert lsm.load_valid_dataset_stats(lake, kind=kind, key=key) is None
        assert any(entry["event"] == "lake.stats_manifest.fallback" for entry in logs)


# ── randomized query equivalence over the REAL pager (spec §10 PROPERTY) ────────────

_QUERY_COORDS: dict[str, str] = {
    "exchange": "NSE",
    "segment": "CASH",
    "symbol": "NSE-NIFTY",
    "interval": "1minute",
}

#: Poison classes applied AFTER a clean seed: ``foreign_copy`` re-rels a real
#: month file (the G-RACE-1 class — file-set fails AND the reseed month-bounds
#: rejects), ``touch`` moves one stat token, ``extra_file`` plants a stray.
_POISONS = st.sampled_from(["none", "foreign_copy", "touch", "extra_file"])


@st.composite
def _paging_case(
    draw: st.DrawFn,
) -> tuple[list[int], int, str, str | None, str | None, int, int, bool]:
    """Draw ``(months, bars_per_month, poison, start, end, offset, limit, desc)``."""
    months = draw(
        st.lists(st.integers(min_value=1, max_value=3), min_size=1, max_size=3, unique=True)
    )
    n_bars = draw(st.integers(min_value=4, max_value=10))
    poison = draw(_POISONS)

    def _iso(d: date | None) -> str | None:
        return d.isoformat() if d is not None else None

    start = draw(st.none() | st.dates(min_value=date(2024, 1, 1), max_value=date(2024, 3, 31)))
    end = draw(st.none() | st.dates(min_value=date(2024, 1, 1), max_value=date(2024, 3, 31)))
    if start is not None and end is not None and start > end:
        start, end = end, start
    offset = draw(st.integers(min_value=0, max_value=35))
    limit = draw(st.integers(min_value=1, max_value=9))
    descending = draw(st.booleans())
    return (sorted(months), n_bars, poison, _iso(start), _iso(end), offset, limit, descending)


def _build_query_lake(lake: Path, months: list[int], n_bars: int) -> None:
    """Write a REAL spot lake via the production writer (distinct minutes,
    days 5–8 so mid-month bounds genuinely straddle a file)."""
    from decimal import Decimal

    from brutex.data.models import CandleBar
    from brutex.data.parquet_writer import ParquetWriter

    writer = ParquetWriter(lake_root=lake)
    for month in months:
        bars = [
            CandleBar(
                timestamp=datetime(2024, month, 5 + minute % 4, 4, minute, tzinfo=UTC),
                open=Decimal(f"{100 + minute}.00"),
                high=Decimal(f"{100 + minute}.00"),
                low=Decimal(f"{100 + minute}.00"),
                close=Decimal(f"{100 + minute}.00"),
                volume=5,
                open_interest=None,
            )
            for minute in range(n_bars)
        ]
        writer.write(bars=bars, **_QUERY_COORDS)


def _apply_query_poison(lake: Path, poison: str) -> None:
    ddir = lsm.dataset_dir(lake, lsm.KIND_BARS, _QUERY_COORDS)
    first = sorted(ddir.rglob("*.parquet"))[0]
    if poison == "foreign_copy":
        (first.parent / "11.parquet").write_bytes(first.read_bytes())
    elif poison == "touch":
        stat = os.stat(first)
        os.utime(first, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    elif poison == "extra_file":
        (ddir / "stray.parquet").write_bytes(first.read_bytes())


@pytest.mark.skipif(not _HAS_LAKE_STACK, reason="duckdb/polars required for the real pager")
@settings(max_examples=12, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(case=_paging_case())
def test_random_paging_equivalence_and_page_sum(
    case: tuple[list[int], int, str, str | None, str | None, int, int, bool],
) -> None:
    """Manifest-opted == legacy cell-for-cell; poison ⇒ explicit fallback,
    never silent divergence; Σ pages == total at the drawn page size."""
    import duckdb
    from brutex.dashboard.db_view import query_candles

    months, n_bars, poison, start, end, offset, limit, descending = case
    with tempfile.TemporaryDirectory() as tmp:
        lake = Path(tmp)
        _build_query_lake(lake, months, n_bars)
        conn = duckdb.connect()

        def _page(*, use: bool, page_offset: int) -> dict[str, Any]:
            return query_candles(
                conn,
                lake,
                exchange=_QUERY_COORDS["exchange"],
                segment=_QUERY_COORDS["segment"],
                symbol=_QUERY_COORDS["symbol"],
                interval=_QUERY_COORDS["interval"],
                start=start,
                end=end,
                descending=descending,
                limit=limit,
                offset=page_offset,
                use_stats_manifest=use,
            )

        _page(use=True, page_offset=0)  # first view: fallback(absent) + write-behind seed
        _apply_query_poison(lake, poison)
        with capture_logs() as logs:
            served = _page(use=True, page_offset=offset)
        legacy = _page(use=False, page_offset=offset)
        assert served == legacy, f"divergence at {case}"
        if poison != "none":
            assert any(e["event"] == "lake.stats_manifest.fallback" for e in logs), (
                "a poisoned dataset must surface an EXPLICIT fallback, never silence"
            )
        # Σ pages == total, walked at the drawn page size on the opted path.
        total = served["total"]
        walked = 0
        page_offset = 0
        while page_offset <= total:
            page = _page(use=True, page_offset=page_offset)
            assert page["total"] == total
            walked += len(page["rows"])
            if not page["rows"]:
                break
            page_offset += limit
        assert walked == total, f"Σ pages != total at {case}"


@settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    kind=st.sampled_from([lsm.KIND_BARS, lsm.KIND_OPTIONS]),
    months=st.lists(st.integers(min_value=1, max_value=12), min_size=3, max_size=5, unique=True),
    rows_per_file=st.integers(min_value=1, max_value=50),
)
def test_concurrent_writer_upserts_converge(
    kind: str, months: list[int], rows_per_file: int
) -> None:
    """N threads upserting DIFFERENT months through the REAL shard flock
    converge: one valid shard carrying EVERY month's row with consistent
    aggregates — no lost update, no torn shard (spec §10 thread fuzz)."""
    key = BARS_KEY if kind == lsm.KIND_BARS else OPTIONS_KEY
    seed_month, *upsert_months = sorted(months)
    with tempfile.TemporaryDirectory() as tmp:
        lake = Path(tmp)
        seed_rows = _materialize(lake, kind, [(2025, seed_month, rows_per_file, 5, 6, 8)])
        assert lsm.seed_dataset_stats(lake, kind=kind, key=key, files=seed_rows)
        ddir = lsm.dataset_dir(lake, kind, key)
        dests: dict[int, Path] = {}
        for month in upsert_months:
            rel = (
                f"2025/{month:02d}.parquet"
                if kind == lsm.KIND_BARS
                else f"2025-{month:02d}.parquet"
            )
            dest = ddir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"y" * (month + 1))
            dests[month] = dest
        barrier = threading.Barrier(len(upsert_months))
        results: dict[int, bool] = {}

        def _upsert(month: int) -> None:
            barrier.wait()
            results[month] = lsm.record_partition_stats(
                lake,
                kind=kind,
                key=key,
                dest=dests[month],
                rows=rows_per_file,
                min_ts=datetime(2025, month, 5, 9, 15, tzinfo=UTC),
                max_ts=datetime(2025, month, 6, 10, 0, tzinfo=UTC),
                raw_hash=None,
                stat=os.stat(dests[month]),
            )

        threads = [threading.Thread(target=_upsert, args=(m,)) for m in upsert_months]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert all(results.values()), f"an upsert was lost: {results}"
        stats = lsm.load_valid_dataset_stats(lake, kind=kind, key=key)
        assert stats is not None, "the converged shard must validate"
        assert len(stats.files) == len(months)
        assert stats.row_count == rows_per_file * len(months)
        rels = [row.rel for row in stats.files]
        assert rels == sorted(rels)
