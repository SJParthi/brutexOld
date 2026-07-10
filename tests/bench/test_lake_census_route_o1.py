"""O(1) gate for the lake-uniqueness route SNAPSHOT path (DEC-LAKE-CENSUS-NONBLOCKING-001).

**Honest, narrow claim (Golden Rule #10):** this bench proves ONLY that a
``GET /api/v1/universe/lake-uniqueness`` POLL is **O(1) in the lake file
count** — the route returns the ``LakeCensusHolder``'s last-known-verdict
snapshot (a dict handoff + pure clock math) and NEVER walks the lake. The
census SCAN itself stays O(files) — it runs once per freshness window on the
dedicated daemon thread; that cost is scheduling, not the poll path, and is
deliberately NOT claimed away.

TEETH: the lakes on disk are REAL (N vs 10N genuine Parquet partition files)
and the scanner is the REAL ``scan_lake_invariants`` (the priming poll pays
the real O(files) scan once). A regression that re-introduces an O(files)
walk into the ROUTE path — inlining the scan per request, re-keying it onto a
per-poll cache miss, or re-adding the superseded blocking
``SummaryCache.get_or_refresh`` recompute — scales the timed poll with the
file count and blows past ``ratio_ceiling(5.0)`` (strict 1.1x on the M4-Pro
tier) AND the absolute sanity bound.

Second gate: ``LakeCensusHolder.snapshot()`` itself is O(1) in the census
SIZE — the payload is a BOUNDED dict (4 segment rows + scalars) whatever the
scanned file count, so a regression that grows the payload per-file (e.g. a
per-file findings list riding into the snapshot) blows the ratio.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="the route bench needs the [ui] extra")
pytest.importorskip("pyarrow")
pytest.importorskip("polars")

from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.lake_census_holder import LakeCensusHolder
from brutex.data.instrument_store import InstrumentStore
from brutex.data.lake_invariants import LakeScanReport, build_lake_census, census_to_dict
from fastapi.testclient import TestClient

from tests.bench._o1_tiers import ratio_ceiling

#: Small/large lake file counts. Big enough that a re-introduced O(files)
#: route walk is unmissable (a real per-file read is >=0.1 ms -> the 10x lake
#: adds >=150 ms per poll), small enough for CI setup (one-time copies).
_N_SMALL = 150
_N_BIG = 1_500

#: Absolute sanity bound per poll (seconds) — a snapshot poll is a dict
#: handoff behind TestClient overhead (measured single-digit ms); a poll that
#: re-runs even the SMALL lake's real scan (~150 file reads) breaches this.
_ABS_POLL_BOUND_S = 0.25


def _render_one_bar_parquet(dest: Path) -> None:
    """One tiny VALID bar Parquet (the scanner's key columns present)."""
    from datetime import UTC, datetime

    import pyarrow as pa
    import pyarrow.parquet as pq

    ts = datetime(2024, 1, 1, 4, 0, tzinfo=UTC)
    table = pa.table(
        {
            "timestamp": pa.array([ts], type=pa.timestamp("us", tz="UTC")),
            "open": pa.array([100.0], type=pa.float64()),
            "high": pa.array([101.0], type=pa.float64()),
            "low": pa.array([99.0], type=pa.float64()),
            "close": pa.array([100.5], type=pa.float64()),
            "volume": pa.array([10], type=pa.int64()),
            "open_interest": pa.array([None], type=pa.int64()),
        }
    )
    pq.write_table(table, dest)  # type: ignore[no-untyped-call, unused-ignore]


def _build_lake(tmp_path: Path, n_files: int, tag: str) -> Path:
    """A REAL lake of *n_files* one-bar Parquet partitions (one per symbol)."""
    import shutil

    lake_root = tmp_path / tag
    template = tmp_path / f"{tag}-template.parquet"
    template.parent.mkdir(parents=True, exist_ok=True)
    _render_one_bar_parquet(template)
    for i in range(n_files):
        dest = lake_root / "bars" / "NSE" / "CASH" / f"SYM{i:05d}" / "1minute" / "2024"
        dest.mkdir(parents=True)
        shutil.copyfile(template, dest / "01.parquet")
    return lake_root


def _primed_client(lake_root: Path) -> tuple[TestClient, InstrumentStore]:
    """A dashboard app on *lake_root* with the census verdict already LANDED.

    The holder's scan runs SYNCHRONOUSLY (the ``census_spawn`` test seam) so
    the FIRST poll pays the real O(files) scan once; every timed poll after is
    the pure snapshot handoff under test.
    """
    store = InstrumentStore.open(lake_root / "instruments.duckdb")
    app = build_app(
        DashboardState(),
        instrument_store=store,
        census_spawn=lambda body: body(),
    )
    client = TestClient(app)
    primed = client.get("/api/v1/universe/lake-uniqueness").json()
    assert primed["present"] is True  # the real scan landed a verdict
    assert primed["files_scanned"] >= 1
    return client, store


def _time_polls(client: TestClient, iters: int) -> float:
    start = time.perf_counter()
    for _ in range(iters):
        resp = client.get("/api/v1/universe/lake-uniqueness")
        assert resp.status_code == 200
    return time.perf_counter() - start


class TestLakeUniquenessPollIsO1InLakeSize:
    """A census-route POLL never walks the lake — flat vs the file count."""

    def test_poll_latency_flat_ratio_below_ceiling(self, tmp_path: Path) -> None:
        small_client, small_store = _primed_client(_build_lake(tmp_path, _N_SMALL, "small"))
        big_client, big_store = _primed_client(_build_lake(tmp_path, _N_BIG, "big"))
        try:
            _time_polls(small_client, 30)  # warm up both apps
            _time_polls(big_client, 30)
            iterations = 300
            small = _time_polls(small_client, iterations)
            big = _time_polls(big_client, iterations)
            ratio = big / small if small > 0 else 1.0
            assert ratio < ratio_ceiling(5.0), (
                f"lake-uniqueness poll ratio {ratio:.2f} scaled with the lake file "
                "count — an O(files) walk re-entered the route path (regression)."
            )
            # Absolute sanity: a poll is a snapshot handoff, not a scan.
            per_poll = big / iterations
            assert per_poll < _ABS_POLL_BOUND_S, (
                f"a single poll took {per_poll * 1000:.1f}ms on the {_N_BIG}-file "
                "lake — the route is doing per-request lake work (regression)."
            )
        finally:
            small_store.close()
            big_store.close()


def _holder_with_landed_census(files_scanned: int) -> LakeCensusHolder:
    """A holder whose landed payload came from the REAL census fold."""
    report = LakeScanReport(
        findings=(),
        files_scanned=files_scanned,
        files_skipped=0,
        series_scanned=files_scanned,
        option_files_scanned=0,
        option_files_skipped=0,
        duration_s=0.0,
        files_by_segment=(("CASH", files_scanned),),
        engine_files=0,
    )
    payload = census_to_dict(build_lake_census(report), mode="auto")

    holder = LakeCensusHolder(
        scan=lambda _tick: dict(payload),
        generation=lambda: 0,
        spawn=lambda body: body(),
    )
    assert holder.maybe_refresh() is True
    snap = holder.snapshot()
    assert snap["files_scanned"] == files_scanned
    return holder


class TestSnapshotIsO1InCensusSize:
    """`snapshot()` is a bounded-dict handoff whatever the census size."""

    def test_snapshot_latency_flat_ratio_below_ceiling(self) -> None:
        small_holder = _holder_with_landed_census(_N_SMALL)
        big_holder = _holder_with_landed_census(_N_BIG * 1_000)  # 1.5M "files"

        def _time_snapshots(holder: LakeCensusHolder, iters: int) -> float:
            start = time.perf_counter()
            for _ in range(iters):
                holder.snapshot()
            return time.perf_counter() - start

        _time_snapshots(small_holder, 2_000)  # warm up
        _time_snapshots(big_holder, 2_000)
        iterations = 20_000
        small = _time_snapshots(small_holder, iterations)
        big = _time_snapshots(big_holder, iterations)
        ratio = big / small if small > 0 else 1.0
        assert ratio < ratio_ceiling(5.0), (
            f"snapshot() ratio {ratio:.2f} scaled with the census size — the "
            "payload is no longer a bounded dict (regression)."
        )

    def test_snapshot_payload_stays_bounded(self) -> None:
        """The structural half of the O(1) claim: payload size is CONSTANT."""
        import json

        small = _holder_with_landed_census(_N_SMALL).snapshot()
        big = _holder_with_landed_census(_N_BIG * 1_000).snapshot()
        # Same key set; the serialized size differs only by scalar digit width.
        assert set(small) == set(big)
        assert abs(len(json.dumps(big)) - len(json.dumps(small))) < 64
