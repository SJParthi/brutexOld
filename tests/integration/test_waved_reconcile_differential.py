"""W-6a ENDGAME reconcile gate — Rust-arm determinism + frozen expectations.

THE FLIP LANDED (DEC-RUST-LAKE-WAVED-001 / Wave-D design §E-4): the Python
``_scan_file`` core is DELETED, so the pre-endgame flag-ON≡flag-OFF twin
run has no Python arm left. This suite is the reconcile seam's real-wheel
verification in the endgame shape: lakes are built through the PRODUCTION
writers (:class:`~brutex.data.parquet_writer.ParquetWriter` — plus raw
pyarrow poison plants for the skip/finding arms), reconciled through the
ONE Rust-backed ``reconcile_catalog_from_lake``, and pinned at

* determinism — TWIN runs into TWIN catalogs → ``ReconcileResult``-equal,
  catalog-ROW-equal (every ``CatalogEntry``), log-EVENT-equal;
* FROZEN expectations — the reconciled/skipped/finding counts, the naive-ts
  ``_as_utc`` spans and the legacy-drift event captured while the
  Python-parity differential was green (the pre-delete record);
* the ``raw_hash`` sha256 EXACTNESS tooth (Golden Rule #4: it feeds the
  FR-CAT-004 ``data_snapshot_hash`` rollup — pinned against an independent
  ``hashlib`` oracle);
* the GAP-#9 per-file isolation law (one poison never kills the pass) and
  the stat-cache warm-hit law (a warm pass never consults the crate).

Requires the built ``brutex_lake`` wheel — a production PREREQUISITE since
W-6a; skips honestly when absent (the unit suites own the wheel-free
typed-refusal story).
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

pl = pytest.importorskip("polars")
pytest.importorskip("duckdb")
pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
_crate = pytest.importorskip("brutex_lake")
if not callable(getattr(_crate, "waved_endgame_witness", None)):
    pytest.skip(
        "installed brutex_lake wheel predates the W-6a endgame API", allow_module_level=True
    )

from brutex.data import lake_rust_waved as lrw  # noqa: E402
from brutex.data.catalog import Catalog  # noqa: E402
from brutex.data.catalog_reconcile import (  # noqa: E402
    ReconcileResult,
    reconcile_catalog_from_lake,
)
from brutex.data.errors import LakeRustWavedFlagRemovedError  # noqa: E402
from brutex.data.models import CandleBar  # noqa: E402
from brutex.data.parquet_writer import ParquetWriter  # noqa: E402
from structlog.testing import capture_logs  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

#: The compared log-event families (everything else is run-local telemetry).
_COMPARED_EVENTS = frozenset(
    {
        "catalog.reconcile.parquet_skip",
        "catalog.reconcile.stat_skip",
        "lake.invariant.intra_file_dup",
        "lake.invariant.foreign_month_rows",
        "lake.invariant.legacy_key_drift",
    }
)


@pytest.fixture(autouse=True)
def _endgame_arm(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The ONE Rust path: retired flag unset, module state reset each test."""
    monkeypatch.delenv(lrw.LAKE_WAVED_RUST_ENV, raising=False)
    lrw.reset_lake_rust_waved_for_tests()
    yield
    lrw.reset_lake_rust_waved_for_tests()


def _write_partition(
    lake: Path,
    *,
    exchange: str = "NSE",
    segment: str = "INDEX",
    symbol: str = "NIFTY",
    year: int = 2021,
    month: int = 1,
    n: int = 5,
) -> Path:
    """Write *n* bars through the PRODUCTION writer; return the file path."""
    writer = ParquetWriter(lake_root=lake)
    bars = [
        CandleBar(
            timestamp=datetime(year, month, 1 + i, 9, 15, tzinfo=UTC),
            open=Decimal(str(100.0 + i)),
            high=Decimal(str(110.0 + i)),
            low=Decimal(str(90.0 + i)),
            close=Decimal(str(105.0 + i)),
            volume=100 + i,
            open_interest=500,
        )
        for i in range(n)
    ]
    writer.write(exchange=exchange, segment=segment, symbol=symbol, interval="1minute", bars=bars)
    return (
        lake
        / "bars"
        / exchange
        / segment
        / symbol
        / "1minute"
        / f"{year:04d}"
        / f"{month:02d}.parquet"
    )


def _plant_raw(
    dest: Path,
    timestamps: Sequence[datetime],
    *,
    ts_type: Any = None,
) -> None:
    """Raw pyarrow plant (the #1067 convention — the fixed writer refuses dups)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = len(timestamps)
    prices = [100.0] * n
    pq.write_table(
        pa.table(
            {
                "timestamp": pa.array(
                    list(timestamps),
                    type=pa.timestamp("us", tz="UTC") if ts_type is None else ts_type,
                ),
                "open": pa.array(prices, type=pa.float64()),
                "high": pa.array(prices, type=pa.float64()),
                "low": pa.array(prices, type=pa.float64()),
                "close": pa.array(prices, type=pa.float64()),
                "volume": pa.array([10] * n, type=pa.int64()),
                "open_interest": pa.array([None] * n, type=pa.int64()),
            }
        ),
        dest,
    )


def _dest(lake: Path, symbol: str, *, year: int = 2024, month: int = 7) -> Path:
    return (
        lake
        / "bars"
        / "NSE"
        / "INDEX"
        / symbol
        / "1minute"
        / f"{year:04d}"
        / f"{month:02d}.parquet"
    )


def _run(lake: Path, *, cat_name: str, **kwargs: Any) -> tuple[ReconcileResult, Path, list[Any]]:
    """One reconcile through the ONE Rust path into its own catalog."""
    cat_path = lake / cat_name
    with Catalog.open(cat_path) as cat, capture_logs() as logs:
        result = reconcile_catalog_from_lake(lake_root=lake, catalog=cat, **kwargs)
    return result, cat_path, _compared(logs)


def _compared(logs: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    return [e for e in logs if e["event"] in _COMPARED_EVENTS]


def _live_crate() -> Any:
    """The ``brutex_lake`` module the facade will actually resolve.

    Resolved at USE time via ``importlib`` (never the module-scope
    ``_crate`` binding): a sibling suite's module-purity test pops
    ``brutex_lake`` from ``sys.modules``, so a whole-battery run re-imports
    a FRESH module object — patching the stale binding would spy on a
    module the facade never touches.
    """
    import importlib

    return importlib.import_module("brutex_lake")


def _rows(cat_path: Path, series: Sequence[tuple[str, str, str]]) -> list[Any]:
    """Dump every CatalogEntry row for the given (exchange, segment, symbol) series."""
    rows: list[Any] = []
    with Catalog.open(cat_path) as cat:
        for exchange, segment, symbol in series:
            rows.extend(
                cat.list_partitions(
                    exchange=exchange, segment=segment, symbol=symbol, interval="1minute"
                )
            )
    return rows


class TestCleanLakeEndgame:
    def test_twin_runs_deterministic_and_frozen(self, tmp_path: Path) -> None:
        # Multi-symbol, multi-month, cross-exchange — the real-writer shape.
        _write_partition(tmp_path, symbol="NIFTY", month=1)
        _write_partition(tmp_path, symbol="NIFTY", month=2, n=3)
        _write_partition(tmp_path, symbol="BANKNIFTY", n=4)
        _write_partition(tmp_path, exchange="BSE", symbol="SENSEX", n=2)
        series = [
            ("NSE", "INDEX", "NIFTY"),
            ("NSE", "INDEX", "BANKNIFTY"),
            ("BSE", "INDEX", "SENSEX"),
        ]
        run_a, cat_a, events_a = _run(tmp_path, cat_name="cat_a.duckdb")
        run_b, cat_b, events_b = _run(tmp_path, cat_name="cat_b.duckdb")
        assert run_a == run_b
        # FROZEN: the pre-delete parity record's exact result.
        assert run_a == ReconcileResult(partitions_reconciled=4, bars_total=14, skipped=0)
        assert _rows(cat_a, series) == _rows(cat_b, series)
        assert events_a == events_b == []
        # The FR-CAT-004 rollup — the run-identity consumer of raw_hash.
        window = {
            "start": datetime(2020, 1, 1, tzinfo=UTC),
            "end": datetime(2022, 1, 1, tzinfo=UTC),
        }
        with Catalog.open(cat_a) as a, Catalog.open(cat_b) as b:
            hash_a = a.data_snapshot_hash(
                exchange="NSE",
                segment="INDEX",
                symbols=["NIFTY", "BANKNIFTY"],
                interval="1minute",
                **window,
            )
            hash_b = b.data_snapshot_hash(
                exchange="NSE",
                segment="INDEX",
                symbols=["NIFTY", "BANKNIFTY"],
                interval="1minute",
                **window,
            )
        assert hash_a == hash_b

    def test_raw_hash_is_sha256_of_file_bytes(self, tmp_path: Path) -> None:
        # The independent sha256 oracle (Golden Rule #4).
        path = _write_partition(tmp_path, n=3)
        _, cat_path, _ = _run(tmp_path, cat_name="cat.duckdb")
        with Catalog.open(cat_path) as cat:
            entry = cat.get(
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                year=2021,
                month=1,
            )
        assert entry is not None
        assert entry.raw_hash == hashlib.sha256(path.read_bytes()).hexdigest()

    def test_crate_is_genuinely_engaged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Anti-vacuous: the pass must actually consult the crate once per
        # readable file (a silently-inert seam would make every frozen
        # assertion vacuous).
        _write_partition(tmp_path, symbol="NIFTY")
        _write_partition(tmp_path, symbol="BANKNIFTY")
        calls: list[tuple[Any, Any]] = []
        crate = _live_crate()
        real = crate.waved_reconcile_file

        def _spy(path: str, home: Any) -> Any:
            calls.append((path, home))
            return real(path, home)

        monkeypatch.setattr(crate, "waved_reconcile_file", _spy)
        result, _, _ = _run(tmp_path, cat_name="cat.duckdb")
        assert result.partitions_reconciled == 2
        assert len(calls) == 2
        assert all(home is not None for _, home in calls)  # check_invariants=True default

    @pytest.mark.parametrize("raw", ["0", "1", "junk"])
    def test_set_retired_flag_refuses_before_any_reconcile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        """The retired-flag law at the reconcile seam: ANY set value refuses."""
        _write_partition(tmp_path, n=2)
        monkeypatch.setenv(lrw.LAKE_WAVED_RUST_ENV, raw)
        with Catalog.open(tmp_path / "cat.duckdb") as cat:  # noqa: SIM117
            with pytest.raises(LakeRustWavedFlagRemovedError, match="no longer flag-gated"):
                reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat)


class TestPoisonEndgame:
    def test_poisoned_lake_frozen_verdict_and_determinism(self, tmp_path: Path) -> None:
        # One GOOD partition + the poison shapes: a dup+foreign plant, a
        # corrupt garbage file, an out-of-datetime-range stamp, a zero-row
        # file, an unparseable exchange dir, a metachar (bracket) symbol dir.
        _write_partition(tmp_path, symbol="NIFTY", n=4)
        june = datetime(2024, 6, 3, 4, 2, tzinfo=UTC)
        july = datetime(2024, 7, 3, 4, 3, tzinfo=UTC)
        _plant_raw(_dest(tmp_path, "BANKNIFTY"), [june, july, july])  # dup + foreign
        corrupt = _dest(tmp_path, "CORRUPT")
        corrupt.parent.mkdir(parents=True)
        corrupt.write_bytes(b"not a parquet file at all")
        oor = _dest(tmp_path, "OORANGE")
        oor.parent.mkdir(parents=True)
        pq.write_table(
            pa.table(
                {"timestamp": pa.array([2**62], type=pa.int64()).cast(pa.timestamp("us", tz="UTC"))}
            ),
            oor,
        )
        _plant_raw(_dest(tmp_path, "EMPTYROW"), [])
        bad_exchange = tmp_path / "bars" / "ZZ" / "INDEX" / "X" / "1minute" / "2021" / "01.parquet"
        _plant_raw(bad_exchange, [june])
        bracket = (
            tmp_path / "bars" / "NSE" / "INDEX" / "NIF[12]" / "1minute" / "2021" / "01.parquet"
        )
        _plant_raw(bracket, [june])
        run_a, cat_a, events_a = _run(tmp_path, cat_name="cat_a.duckdb")
        run_b, cat_b, events_b = _run(tmp_path, cat_name="cat_b.duckdb")
        assert run_a == run_b
        # FROZEN: good + dup/foreign plant reconcile; corrupt + OOR +
        # zero-row + bad-exchange + bracket dir are the five GAP-#9 skips.
        assert run_a.partitions_reconciled == 2
        assert run_a.skipped == 5
        assert len(run_a.findings) == 2
        assert events_a == events_b
        skip_events = [e for e in events_a if e["event"] == "catalog.reconcile.parquet_skip"]
        assert {Path(str(e["path"])).parts[-4] for e in skip_events} == {"CORRUPT", "OORANGE"}
        oor_event = next(e for e in skip_events if "OORANGE" in str(e["path"]))
        assert "outside the representable datetime range" in str(oor_event["error"])
        series = [("NSE", "INDEX", "NIFTY"), ("NSE", "INDEX", "BANKNIFTY")]
        assert _rows(cat_a, series) == _rows(cat_b, series)

    def test_per_file_isolation_one_poison_never_kills_the_pass(self, tmp_path: Path) -> None:
        _write_partition(tmp_path, symbol="NIFTY", n=6)
        corrupt = _dest(tmp_path, "AAACORRUPT")  # sorts FIRST — poison before good
        corrupt.parent.mkdir(parents=True)
        corrupt.write_bytes(b"garbage")
        result, cat_path, _ = _run(tmp_path, cat_name="cat.duckdb")
        assert result.partitions_reconciled == 1 and result.skipped == 1
        with Catalog.open(cat_path) as cat:
            assert cat.count() == 1

    def test_naive_and_non_utc_tz_columns_frozen_spans(self, tmp_path: Path) -> None:
        # A NAIVE hand-plant (defensive _as_utc arm) + an aware NON-UTC tz
        # plant: the first/last spans are the frozen _as_utc law.
        naive = _dest(tmp_path, "NAIVE", year=2024, month=6)
        _plant_raw(
            naive,
            [datetime(2024, 6, 3, 9, 15), datetime(2024, 6, 4, 9, 15)],
            ts_type=pa.timestamp("us"),
        )
        ist = _dest(tmp_path, "ISTTZ", year=2024, month=6)
        _plant_raw(
            ist,
            [datetime(2024, 6, 3, 4, 2, tzinfo=UTC)],
            ts_type=pa.timestamp("us", tz="Asia/Kolkata"),
        )
        run_a, cat_a, _ = _run(tmp_path, cat_name="cat_a.duckdb")
        run_b, cat_b, _ = _run(tmp_path, cat_name="cat_b.duckdb")
        assert run_a == run_b
        assert run_a.partitions_reconciled == 2
        series = [("NSE", "INDEX", "NAIVE"), ("NSE", "INDEX", "ISTTZ")]
        rows_a = _rows(cat_a, series)
        assert rows_a == _rows(cat_b, series)
        naive_row = next(r for r in rows_a if r.symbol == "NAIVE")
        # FROZEN: the naive wall time stamped UTC — the _as_utc law.
        assert naive_row.first_ts == datetime(2024, 6, 3, 9, 15, tzinfo=UTC)
        assert naive_row.last_ts == datetime(2024, 6, 4, 9, 15, tzinfo=UTC)
        ist_row = next(r for r in rows_a if r.symbol == "ISTTZ")
        # FROZEN: an aware non-UTC stamp folds to the same INSTANT in UTC.
        assert ist_row.first_ts == datetime(2024, 6, 3, 4, 2, tzinfo=UTC)

    def test_legacy_drift_frozen_event(self, tmp_path: Path) -> None:
        _write_partition(tmp_path, segment="CASH", symbol="M&M")
        _write_partition(tmp_path, segment="CASH", symbol="NSE-M&M")
        run_a, _, events_a = _run(tmp_path, cat_name="cat_a.duckdb")
        run_b, _, events_b = _run(tmp_path, cat_name="cat_b.duckdb")
        assert run_a == run_b
        assert events_a == events_b
        assert [e["event"] for e in events_a] == ["lake.invariant.legacy_key_drift"]

    def test_check_invariants_false_classifies_nothing(self, tmp_path: Path) -> None:
        _write_partition(tmp_path, symbol="NIFTY")
        june = datetime(2024, 6, 3, 4, 2, tzinfo=UTC)
        _plant_raw(_dest(tmp_path, "BANKNIFTY"), [june, june])  # a dup, unclassified
        result, _, events = _run(tmp_path, cat_name="cat.duckdb", check_invariants=False)
        assert result.findings == ()
        assert events == []
        assert result.partitions_reconciled == 2


class TestStatCache:
    def test_stat_cache_warm_hits_skip_the_crate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The stat-cache branch stays Python: a warm second pass skips the
        # per-file body entirely — the crate is never consulted on a warm hit.
        _write_partition(tmp_path, symbol="NIFTY")
        _write_partition(tmp_path, symbol="BANKNIFTY")
        cat_path = tmp_path / "cat.duckdb"
        with Catalog.open(cat_path) as cat:
            reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat, stat_cache=True)
            calls: list[str] = []
            crate = _live_crate()
            real = crate.waved_reconcile_file

            def _spy(path: str, home: Any) -> Any:
                calls.append(path)
                return real(path, home)

            monkeypatch.setattr(crate, "waved_reconcile_file", _spy)
            warm = reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat, stat_cache=True)
        assert warm.stat_cache_hits == 2
        assert calls == []  # warm hits never reach the seam
