"""O(page) proof for the stats-manifest ``/db`` pager (DEC-LAKE-STATS-MANIFEST-001).

The #867/#1025 claim under test: with a VALIDATED per-file stats manifest, a
paging click reads ONLY the files the page needs — O(page) in partition depth
— while the legacy path binds the whole ``**/*.parquet`` glob (every file's
footer, O(files)).

Two teeth, one deterministic and one timed:

* **FILE-ACCESS PROBE (deterministic, noise-free):** a recording connection
  captures every ``read_parquet`` binding; a manifest-served click on a
  72-month expired-options series touches EXACTLY the one month file its page
  lives in, while the SAME click on the legacy path touches all 72 — and the
  legacy touch count SCALES with depth (6 → 72) while the manifest count stays
  constant. The flat gate can therefore never pass vacuously (the Design-1
  graft): the fixture provably distinguishes the two complexities.
* **FLAT LATENCY:** the manifest-served click's latency at 72 months vs 6
  months stays under the shared-CI ratio ceiling (strict 1.1× on the M4-Pro
  tier via :func:`ratio_ceiling`). Honest envelope (Golden Rule #10): the
  manifest click still pays an O(D) stat-token revalidation (D = the ONE
  dataset's month files — metadata syscalls), so this is O(page) Parquet work
  + O(D) statx, NOT a literal O(1).

The expired-options pager is benched because it is the pure surface: no
%-change baseline reads (those are deliberately untouched this round) — every
``read_parquet`` in the request is the pager's own.
"""

from __future__ import annotations

import glob as globmod
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.bench._o1_tiers import ratio_ceiling

pytest.importorskip("polars")
pytest.importorskip("duckdb")

import duckdb
from brutex.dashboard.db_view import query_option_candles
from brutex.dashboard.movers_cache import MoversSnapshotCache
from brutex.data.models import OptionBar
from brutex.data.option_parquet_writer import OptionParquetWriter, OptionSeriesCoordinate

_OPT_PK = "NSE_FNO|13|WEEK|0|ATM|CALL"
_OPT_SERIES = "NSE_FNO__13__WEEK__0__ATM__CALL"

#: Month depths the click must be independent of (12× difference).
_SMALL_MONTHS = 6
_LARGE_MONTHS = 72
#: CE bars per month file (one page fits inside one month).
_BARS_PER_MONTH = 8
#: The measured page click (DESC first page — the /db default).
_LIMIT = 5
#: Timed queries per depth (averaged).
_QUERIES = 20


def _obar(ts: datetime) -> OptionBar:
    return OptionBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("110.00"),
        low=Decimal("90.00"),
        close=Decimal("105.00"),
        volume=10,
        open_interest=100,
        side="CE",
        strike=Decimal("17500.00"),
        spot=Decimal("17480.00"),
        iv=None,
    )


def _build_series(lake_root: Path, months: int) -> None:
    """Write *months* month partitions via the production options writer."""
    seg, sec, flag, code, offset, _typ = _OPT_PK.split("|")
    coord = OptionSeriesCoordinate(
        exch_id="NSE",
        exchange_segment=seg,
        security_id=int(sec),
        underlying_symbol="NIFTY",
        expiry_flag=flag,
        expiry_code=int(code),
        strike_offset=offset,
    )
    writer = OptionParquetWriter(lake_root=lake_root)
    year, month = 2018, 1
    for _ in range(months):
        writer.write(
            partition_key=_OPT_PK,
            coordinate=coord,
            bars=[
                _obar(datetime(year, month, 3, 4, minute, tzinfo=UTC))
                for minute in range(_BARS_PER_MONTH)
            ],
        )
        month += 1
        if month > 12:
            year, month = year + 1, 1


class _FileProbeConn:
    """Records the CONCRETE parquet files every ``read_parquet`` binding touches.

    A bound glob string is expanded (DuckDB reads every matched file's footer
    to bind a ``union_by_name`` schema — the honest O(files) accounting); a
    bound list is its own file set.
    """

    def __init__(self, real: Any) -> None:
        self._real = real
        self.touched: set[str] = set()

    def reset(self) -> None:
        self.touched = set()

    def execute(self, sql: str, parameters: Any = None) -> Any:
        if parameters is not None and "read_parquet" in sql:
            src = parameters[0]
            if isinstance(src, list):
                self.touched.update(str(p) for p in src)
            else:
                self.touched.update(globmod.glob(str(src), recursive=True))
        if parameters is None:
            return self._real.execute(sql)
        return self._real.execute(sql, parameters)


def _click(
    conn: Any, root: Path, *, use: bool, cache: MoversSnapshotCache | None
) -> dict[str, Any]:
    return query_option_candles(
        conn,
        root,
        series=_OPT_SERIES,
        descending=True,
        limit=_LIMIT,
        offset=0,
        count_cache=cache,
        generation=1,
        use_stats_manifest=use,
    )


def _touched_at_depth(tmp: Path, months: int) -> tuple[int, int, int]:
    """(total files, manifest-click touches, legacy-click touches) at *months*."""
    root = tmp / f"lake{months}"
    _build_series(root, months)
    cache = MoversSnapshotCache()
    conn = duckdb.connect()
    probe = _FileProbeConn(conn)
    _click(probe, root, use=True, cache=cache)  # fallback + write-behind seed
    _click(probe, root, use=True, cache=cache)  # manifest-served; star memo warmed
    probe.reset()
    served = _click(probe, root, use=True, cache=cache)  # the measured click
    manifest_touched = set(probe.touched)
    probe.reset()
    legacy = _click(probe, root, use=False, cache=None)
    legacy_touched = set(probe.touched)
    assert served == legacy, "the two paths must serve identical pages"
    total_files = len(list((root / "options" / _OPT_SERIES).glob("*.parquet")))
    return total_files, len(manifest_touched), len(legacy_touched)


class TestManifestPagingIsOPage:
    def test_manifest_click_touches_only_the_page_file(self, tmp_path: Path) -> None:
        """DETERMINISTIC teeth: page files only vs the whole glob, 12× apart."""
        total_s, manifest_s, legacy_s = _touched_at_depth(tmp_path, _SMALL_MONTHS)
        total_l, manifest_l, legacy_l = _touched_at_depth(tmp_path, _LARGE_MONTHS)
        assert (total_s, total_l) == (_SMALL_MONTHS, _LARGE_MONTHS)
        # The DESC first page lives wholly in the newest month file.
        assert manifest_s == manifest_l == 1, (
            f"the manifest click must touch ONLY the page's file (got {manifest_s} / {manifest_l})"
        )
        # NON-VACUOUS: the legacy path really is O(files) on this fixture —
        # its touch count IS the depth, so a regression that quietly re-bound
        # the glob would fail the constant-touch assert above, and a fixture
        # that stopped distinguishing the paths would fail THIS one.
        assert legacy_s == _SMALL_MONTHS
        assert legacy_l == _LARGE_MONTHS

    def test_manifest_click_latency_flat_as_series_deepens(self, tmp_path: Path) -> None:
        """Manifest-click latency at 72 vs 6 months stays under the tier ceiling."""

        def _time(months: int) -> float:
            root = tmp_path / f"lat{months}"
            _build_series(root, months)
            cache = MoversSnapshotCache()
            conn = duckdb.connect()
            _click(conn, root, use=True, cache=cache)  # seed
            for _ in range(3):  # warm DuckDB + the OS page cache + the memos
                _click(conn, root, use=True, cache=cache)
            t0 = time.perf_counter_ns()
            for _ in range(_QUERIES):
                _click(conn, root, use=True, cache=cache)
            return (time.perf_counter_ns() - t0) / _QUERIES

        t_small = _time(_SMALL_MONTHS)
        t_large = _time(_LARGE_MONTHS)
        ratio = t_large / max(t_small, 1.0)
        assert ratio < ratio_ceiling(8.0), (
            f"manifest paging latency ratio {ratio:.2f}× ({_LARGE_MONTHS} vs "
            f"{_SMALL_MONTHS} months) exceeds the ceiling — the pruned page may "
            f"have regressed to an O(files) read. t_small={t_small:.0f}ns, "
            f"t_large={t_large:.0f}ns."
        )
