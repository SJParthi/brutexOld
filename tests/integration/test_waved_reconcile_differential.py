"""Wave-D W-3 reconcile differential — flag-ON ≡ flag-OFF on real-writer lakes.

The real-wheel parity gate for the ``BRUTEX_LAKE_WAVED_RUST`` reconcile seam
(Wave-D design §E-4): lakes are built through the PRODUCTION writers
(:class:`~brutex.data.parquet_writer.ParquetWriter` — plus raw pyarrow poison
plants for the skip/finding arms), then the SAME
``reconcile_catalog_from_lake`` runs flag-OFF (the Python oracle) into one
catalog and flag-ON in the post-burn-in steady state
(``reset_lake_rust_waved_for_tests(remaining=0)`` — the crate scan served
raw, no oracle cushioning) into a TWIN catalog, and the results are compared
at ``ReconcileResult`` strength (findings included), catalog-ROW strength
(every ``CatalogEntry`` — the ``raw_hash`` sha256 exactness is the
run-identity tooth, Golden Rule #4: it feeds the FR-CAT-004
``data_snapshot_hash`` rollup, pinned here too), and LOG-EVENT strength (the
``catalog.reconcile.parquet_skip`` + ``lake.invariant.*`` streams — the
degrade-to-oracle law makes the skip payloads byte-identical).

Skips cleanly when the installed wheel predates the Wave-D W-3 API (the
lockstep law: crate and repo move together).
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
if not callable(getattr(_crate, "waved_reconcile_file", None)):
    pytest.skip("installed brutex_lake wheel lacks the Wave-D W-3 API", allow_module_level=True)

from brutex.data import lake_rust_waved as lrw  # noqa: E402
from brutex.data.catalog import Catalog  # noqa: E402
from brutex.data.catalog_reconcile import (  # noqa: E402
    ReconcileResult,
    reconcile_catalog_from_lake,
)
from brutex.data.models import CandleBar  # noqa: E402
from brutex.data.parquet_writer import ParquetWriter  # noqa: E402
from structlog.testing import capture_logs  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

#: The compared log-event families (everything else — e.g. the facade's own
#: ``lake_rust_waved.engaged`` INFO — is arm-specific observability).
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
def _flag_off_and_fresh(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Default every test to flag OFF + a fresh facade view."""
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


def _run_off(lake: Path, **kwargs: Any) -> tuple[ReconcileResult, Path, list[Any]]:
    """Flag-OFF reconcile into its own catalog; return (result, catalog path, events)."""
    cat_path = lake / "cat_off.duckdb"
    with Catalog.open(cat_path) as cat, capture_logs() as logs:
        result = reconcile_catalog_from_lake(lake_root=lake, catalog=cat, **kwargs)
    return result, cat_path, _compared(logs)


def _run_on(
    lake: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    remaining: int = 0,
    cat_name: str = "cat_on.duckdb",
    **kwargs: Any,
) -> tuple[ReconcileResult, Path, list[Any]]:
    """Flag-ON reconcile (default: post-burn-in steady state) into a twin catalog."""
    monkeypatch.setenv(lrw.LAKE_WAVED_RUST_ENV, "1")
    lrw.reset_lake_rust_waved_for_tests(remaining=remaining)
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


class TestCleanLakeParity:
    def test_twin_catalog_rows_and_snapshot_hash_equal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
        off, cat_off, off_events = _run_off(tmp_path)
        on, cat_on, on_events = _run_on(tmp_path, monkeypatch)
        assert on == off
        assert off == ReconcileResult(partitions_reconciled=4, bars_total=14, skipped=0)
        assert _rows(cat_on, series) == _rows(cat_off, series)
        assert on_events == off_events == []
        assert not lrw.rust_waved_disabled()
        # The FR-CAT-004 rollup — the run-identity consumer of raw_hash.
        window = {
            "start": datetime(2020, 1, 1, tzinfo=UTC),
            "end": datetime(2022, 1, 1, tzinfo=UTC),
        }
        with Catalog.open(cat_off) as a, Catalog.open(cat_on) as b:
            hash_off = a.data_snapshot_hash(
                exchange="NSE",
                segment="INDEX",
                symbols=["NIFTY", "BANKNIFTY"],
                interval="1minute",
                **window,
            )
            hash_on = b.data_snapshot_hash(
                exchange="NSE",
                segment="INDEX",
                symbols=["NIFTY", "BANKNIFTY"],
                interval="1minute",
                **window,
            )
        assert hash_on == hash_off

    def test_flag_on_raw_hash_is_sha256_of_file_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The independent sha256 oracle on the ROUTED arm (Golden Rule #4).
        path = _write_partition(tmp_path, n=3)
        _, cat_on, _ = _run_on(tmp_path, monkeypatch)
        with Catalog.open(cat_on) as cat:
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
        # Anti-vacuous: the steady-state ON arm must actually consult the
        # crate once per readable file (a silently-latched seam would make
        # every parity assertion vacuous).
        _write_partition(tmp_path, symbol="NIFTY")
        _write_partition(tmp_path, symbol="BANKNIFTY")
        calls: list[tuple[Any, Any]] = []
        crate = _live_crate()  # the module the facade will resolve (not the
        # collection-time binding — a sibling suite may have re-imported it)
        real = crate.waved_reconcile_file

        def _spy(path: str, home: Any) -> Any:
            calls.append((path, home))
            return real(path, home)

        monkeypatch.setattr(crate, "waved_reconcile_file", _spy)
        on, _, _ = _run_on(tmp_path, monkeypatch)
        assert on.partitions_reconciled == 2
        assert len(calls) == 2
        assert all(home is not None for _, home in calls)  # check_invariants=True default


class TestPoisonParity:
    def test_findings_skips_and_events_equal_on_a_poisoned_lake(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
        off, cat_off, off_events = _run_off(tmp_path)
        on, cat_on, on_events = _run_on(tmp_path, monkeypatch)
        assert on == off
        # good + dup/foreign plant reconcile; corrupt + OOR + zero-row +
        # bad-exchange + bracket dir are the five skips.
        assert off.partitions_reconciled == 2
        assert off.skipped == 5
        assert len(off.findings) == 2
        assert on_events == off_events
        skip_events = [e for e in on_events if e["event"] == "catalog.reconcile.parquet_skip"]
        assert {Path(str(e["path"])).parts[-4] for e in skip_events} == {"CORRUPT", "OORANGE"}
        oor_event = next(e for e in skip_events if "OORANGE" in str(e["path"]))
        assert "out of range" in str(oor_event["error"])
        series = [("NSE", "INDEX", "NIFTY"), ("NSE", "INDEX", "BANKNIFTY")]
        assert _rows(cat_on, series) == _rows(cat_off, series)
        assert not lrw.rust_waved_disabled()

    def test_per_file_isolation_one_poison_never_kills_the_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_partition(tmp_path, symbol="NIFTY", n=6)
        corrupt = _dest(tmp_path, "AAACORRUPT")  # sorts FIRST — poison before good
        corrupt.parent.mkdir(parents=True)
        corrupt.write_bytes(b"garbage")
        off, _, _ = _run_off(tmp_path)
        on, cat_on, _ = _run_on(tmp_path, monkeypatch)
        assert on == off
        assert on.partitions_reconciled == 1 and on.skipped == 1
        with Catalog.open(cat_on) as cat:
            assert cat.count() == 1

    def test_naive_and_non_utc_tz_columns_parity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A NAIVE hand-plant (defensive _as_utc arm) + an aware NON-UTC tz
        # plant: first/last spans must land identically in both catalogs.
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
        off, cat_off, _ = _run_off(tmp_path)
        on, cat_on, _ = _run_on(tmp_path, monkeypatch)
        assert on == off
        assert on.partitions_reconciled == 2
        series = [("NSE", "INDEX", "NAIVE"), ("NSE", "INDEX", "ISTTZ")]
        rows_on = _rows(cat_on, series)
        assert rows_on == _rows(cat_off, series)
        naive_row = next(r for r in rows_on if r.symbol == "NAIVE")
        # The naive wall time stamped UTC — the _as_utc law, both arms.
        assert naive_row.first_ts == datetime(2024, 6, 3, 9, 15, tzinfo=UTC)
        assert naive_row.last_ts == datetime(2024, 6, 4, 9, 15, tzinfo=UTC)
        assert not lrw.rust_waved_disabled()

    def test_legacy_drift_parity(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_partition(tmp_path, segment="CASH", symbol="M&M")
        _write_partition(tmp_path, segment="CASH", symbol="NSE-M&M")
        off, _, off_events = _run_off(tmp_path)
        on, _, on_events = _run_on(tmp_path, monkeypatch)
        assert on == off
        assert on_events == off_events
        assert [e["event"] for e in on_events] == ["lake.invariant.legacy_key_drift"]

    def test_check_invariants_false_parity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_partition(tmp_path, symbol="NIFTY")
        june = datetime(2024, 6, 3, 4, 2, tzinfo=UTC)
        _plant_raw(_dest(tmp_path, "BANKNIFTY"), [june, june])  # a dup, unclassified
        off, _, off_events = _run_off(tmp_path, check_invariants=False)
        on, _, on_events = _run_on(tmp_path, monkeypatch, check_invariants=False)
        assert on == off
        assert on.findings == ()
        assert on_events == off_events == []


class TestBurnInAndCache:
    def test_burn_in_arm_matches_and_never_autoreverts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A FRESH burn-in (full budget) over a poisoned lake: every engaged
        # file compares crate-vs-oracle — any real divergence would latch;
        # the seam must come out clean AND parity must hold.
        _write_partition(tmp_path, symbol="NIFTY", n=4)
        june = datetime(2024, 6, 3, 4, 2, tzinfo=UTC)
        july = datetime(2024, 7, 3, 4, 3, tzinfo=UTC)
        _plant_raw(_dest(tmp_path, "BANKNIFTY"), [june, july, july])
        off, _, _ = _run_off(tmp_path)
        on, _, _ = _run_on(tmp_path, monkeypatch, remaining=lrw.BURN_IN_SAMPLES)
        assert on == off
        assert not lrw.rust_waved_disabled()

    def test_stat_cache_warm_hits_parity_and_skip_the_crate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The stat-cache branch stays Python: a warm second pass skips the
        # per-file body entirely — in BOTH arms — so the ON arm's crate is
        # never consulted on a warm hit.
        _write_partition(tmp_path, symbol="NIFTY")
        _write_partition(tmp_path, symbol="BANKNIFTY")
        cat_off = tmp_path / "cat_off.duckdb"
        with Catalog.open(cat_off) as cat:
            reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat, stat_cache=True)
            off_warm = reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat, stat_cache=True)
        monkeypatch.setenv(lrw.LAKE_WAVED_RUST_ENV, "1")
        lrw.reset_lake_rust_waved_for_tests(remaining=0)
        cat_on = tmp_path / "cat_on.duckdb"
        with Catalog.open(cat_on) as cat:
            reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat, stat_cache=True)
            calls: list[str] = []
            crate = _live_crate()
            real = crate.waved_reconcile_file

            def _spy(path: str, home: Any) -> Any:
                calls.append(path)
                return real(path, home)

            monkeypatch.setattr(crate, "waved_reconcile_file", _spy)
            on_warm = reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat, stat_cache=True)
        assert on_warm == off_warm
        assert on_warm.stat_cache_hits == 2
        assert calls == []  # warm hits never reach the seam

    def test_flag_off_run_in_this_suite_never_touches_the_crate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The never-routes bomb spy at the CRATE boundary (the unit suite
        # pins the facade boundary; this pins the real-wheel one).
        _write_partition(tmp_path, n=3)

        def _bomb(path: str, home: Any) -> Any:
            raise AssertionError("the crate must never be consulted flag-OFF")

        monkeypatch.setattr(_live_crate(), "waved_reconcile_file", _bomb)
        off, _, _ = _run_off(tmp_path)
        assert off.partitions_reconciled == 1
