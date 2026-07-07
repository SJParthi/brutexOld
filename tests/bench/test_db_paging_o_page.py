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
* **FLAT IN LAKE SIZE (spec §10 bench (c), review round 2):** the per-consult
  validation cost depends on the ONE consulted dataset only — a click's
  python-level filesystem operations (``os.stat`` + ``os.scandir``: the stat
  sweep, the set-equality walk, the shard read) and its touched parquet files
  are counted EXACTLY EQUAL on a bare one-series lake vs the same series
  inside a lake padded with 48 unrelated datasets (a deterministic
  operation-count gate, chosen over a wall-clock ratio: equal op-counts make
  wall-clock flatness trivial, and the repo's bench history pins shared-CI
  GIL noise as the flake source — DEC-HARDENING-LEFTOVERS-001 (3)).

The expired-options pager is benched because it is the pure surface: no
%-change baseline reads (those are deliberately untouched this round) — every
``read_parquet`` in the request is the pager's own.
"""

from __future__ import annotations

import glob as globmod
import os
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
from brutex.data.models import CandleBar, OptionBar
from brutex.data.option_parquet_writer import OptionParquetWriter, OptionSeriesCoordinate
from brutex.data.parquet_writer import ParquetWriter

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


#: Unrelated spot datasets the padded lake carries (bench (c) fixture).
_PADDING_DATASETS = 48


def _build_spot_padding(root: Path, n_datasets: int) -> None:
    """Write *n_datasets* single-bar UNRELATED spot datasets — pure lake bulk."""
    writer = ParquetWriter(lake_root=root)
    price = Decimal("100.00")
    for i in range(n_datasets):
        writer.write(
            bars=[
                CandleBar(
                    timestamp=datetime(2024, 1, 5, 4, 0, tzinfo=UTC),
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=10,
                    open_interest=None,
                )
            ],
            exchange="NSE",
            segment="CASH",
            symbol=f"PAD{i:03d}",
            interval="1minute",
        )


class TestValidationFlatInLakeSize:
    """Spec §10 bench (c): per-consult validation cost is FLAT in LAKE size.

    Deterministic, noise-free teeth (see the module docstring): the SAME
    manifest-served click is measured on a bare one-series lake and on a lake
    padded with :data:`_PADDING_DATASETS` unrelated spot datasets — every
    python-level filesystem operation (``os.stat`` / ``os.scandir``) and every
    parquet file it touches must be EXACTLY equal, because
    ``scan_dataset_rels`` walks ONLY the consulted dataset's dir and every
    stat/read binds only that dataset's rels. Structural non-vacuity: the
    padded lake provably carries the extra datasets on disk.
    """

    def _measured_click(self, tmp: Path, *, padding: int) -> tuple[int, int, int]:
        """(os.stat count, os.scandir count, files touched) for one served click."""
        root = tmp / f"lake_pad{padding}"
        _build_series(root, _SMALL_MONTHS)
        _build_spot_padding(root, padding)
        cache = MoversSnapshotCache()
        conn = duckdb.connect()
        probe = _FileProbeConn(conn)
        _click(probe, root, use=True, cache=cache)  # fallback + write-behind seed
        _click(probe, root, use=True, cache=cache)  # manifest-served; memos warmed
        counts = {"stat": 0, "scandir": 0}
        real_stat = os.stat
        real_scandir = os.scandir

        def _stat(path: Any, *args: Any, **kwargs: Any) -> Any:
            counts["stat"] += 1
            return real_stat(path, *args, **kwargs)

        def _scandir(path: Any = ".", *args: Any, **kwargs: Any) -> Any:
            counts["scandir"] += 1
            return real_scandir(path, *args, **kwargs)

        probe.reset()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(os, "stat", _stat)
            patch.setattr(os, "scandir", _scandir)
            served = _click(probe, root, use=True, cache=cache)  # the measured click
        assert served["total"] == _SMALL_MONTHS * _BARS_PER_MONTH
        return (counts["stat"], counts["scandir"], len(probe.touched))

    def test_click_fs_ops_flat_in_lake_size(self, tmp_path: Path) -> None:
        bare = self._measured_click(tmp_path, padding=0)
        padded = self._measured_click(tmp_path, padding=_PADDING_DATASETS)
        # Structural non-vacuity: the padding really is on disk.
        bare_files = (
            len(list((tmp_path / "lake_pad0" / "bars").rglob("*.parquet")))
            if (tmp_path / "lake_pad0" / "bars").is_dir()
            else 0
        )
        padded_files = len(
            list((tmp_path / f"lake_pad{_PADDING_DATASETS}" / "bars").rglob("*.parquet"))
        )
        assert padded_files == bare_files + _PADDING_DATASETS
        assert padded == bare, (
            "a manifest-served click's validation cost must be flat in LAKE size "
            f"(bare lake (stat, scandir, files)={bare}, padded lake={padded})"
        )
        assert padded[2] == 1, "the click touches ONLY its page file at either lake size"
