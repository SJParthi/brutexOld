"""A dup-poisoned symbol on ``/movers`` is HEALED at read, never a 500.

History: DEC-LAKE-WRITE-UNIQUE-001 first bounded the blast radius by adding
the resampler tripwire's :class:`~brutex.data.errors.ResampleInputError`
(DEC-RESAMPLER-INPUT-GUARD-001) to
:func:`~brutex.dashboard.movers_resilient.movers_read_skip_errors`, so ONE
poisoned symbol was SKIPPED (counted) instead of 500ing the page. Dup-train
PR-2 (DEC-LAKE-READ-UNIQUE-001) then wired the keep-FIRST read gate into the
movers day readers, so a duplicate-only poison is now COLLAPSED before the
tripwire ever sees it — the symbol RANKS with its real bars + one aggregated
``lake.read.duplicates_dropped`` WARNING. The tripwire's skip-set membership
stays pinned here as the guard for any poisoned path that bypasses the day
readers, and the corrupt-Parquet skip of DEC-MOVERS-CORRUPT-SKIP-001 is
unchanged.

Also pins the set's honest boundary: :class:`~brutex.data.errors.LakeWriteHalt`
(a sibling ``DataLakeError`` — a pull-wide stop) is NOT in the skip set, so a
disk-full/OOM can never be miscounted as a "skipped symbol".

Skipped when ``fastapi`` / ``duckdb`` / ``polars`` / ``pyarrow`` are absent.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("polars")
pytest.importorskip("pyarrow")

from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.movers_resilient import movers_read_skip_errors
from brutex.dashboard.movers_snapshot import compute_spot_candidates
from brutex.data.errors import LakeWriteHalt, ResampleInputError
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

_DAY = date(2024, 6, 13)


def _min_bar(minute: int, *, close: str, volume: int) -> CandleBar:
    """A flat 1-minute bar at 09:35+*minute* IST (04:05+*minute* UTC) on ``_DAY``."""
    c = Decimal(close)
    return CandleBar(
        timestamp=datetime(_DAY.year, _DAY.month, _DAY.day, 4, 5 + minute, tzinfo=UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=volume,
        open_interest=None,
    )


def _poison_1minute_partition(
    lake_root: Path, *, exchange: str, symbol: str, rows: Sequence[tuple[int, float]]
) -> None:
    """Write a RAW pyarrow 1-minute partition carrying DUPLICATE timestamps.

    ``rows`` are ``(minute_offset, price)`` — repeated offsets are written
    verbatim, simulating a partition poisoned before DEC-LAKE-WRITE-UNIQUE-001
    (the fixed writer itself now refuses to store duplicates, so the poison
    must be planted with raw pyarrow, exactly like the operator's lake).
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest = lake_root / "bars" / exchange / "INDEX" / symbol / "1minute" / "2024" / "06.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    stamps = [
        datetime(_DAY.year, _DAY.month, _DAY.day, 4, 5 + offset, tzinfo=UTC) for offset, _ in rows
    ]
    prices = [price for _, price in rows]
    table = pa.table(
        {
            "timestamp": pa.array(stamps, type=pa.timestamp("us", tz="UTC")),
            "open": pa.array(prices, type=pa.float64()),
            "high": pa.array(prices, type=pa.float64()),
            "low": pa.array(prices, type=pa.float64()),
            "close": pa.array(prices, type=pa.float64()),
            "volume": pa.array([10] * len(rows), type=pa.int64()),
            "open_interest": pa.array([None] * len(rows), type=pa.int64()),
        }
    )
    pq.write_table(table, dest)


def _seed_good_and_poisoned(lake_root: Path) -> None:
    """A healthy NIFTY 1-minute series + a dup-poisoned SENSEX 1-minute series."""
    ParquetWriter(lake_root=lake_root).write(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        bars=[_min_bar(0, close="22000", volume=500), _min_bar(1, close="22010", volume=400)],
    )
    # Duplicate minute-0 rows → the resampler tripwire raises ResampleInputError.
    _poison_1minute_partition(
        lake_root,
        exchange="BSE",
        symbol="SENSEX",
        rows=[(0, 77000.0), (0, 77000.0), (1, 77010.0)],
    )


def _idx(symbol: str) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=abs(hash(symbol)) % 100_000,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[InstrumentStore]:
    s = InstrumentStore.open(tmp_path / "instruments.duckdb")
    s.upsert_many(
        [_idx("NIFTY")], snapshot_date=date(2024, 1, 2), run_id="r1", csv_source_sha256="0" * 64
    )
    yield s
    s.close()


class TestSkipSetMembership:
    def test_resample_input_error_is_in_the_movers_skip_set(self) -> None:
        """FAILS on unfixed main: ResampleInputError was not in the set."""
        assert ResampleInputError in movers_read_skip_errors()

    def test_lake_write_halt_is_not_swallowed_by_the_skip_set(self) -> None:
        """The set stays narrow: a pull-wide LakeWriteHalt is never a 'skipped symbol'."""
        skip_set = movers_read_skip_errors()
        assert LakeWriteHalt not in skip_set
        assert not issubclass(LakeWriteHalt, skip_set)


class TestPoisonedSymbolSkippedNot500:
    def test_compute_heals_the_poisoned_symbol_keep_first(self, tmp_path: Path) -> None:
        """The dup-poisoned symbol now RANKS with its deduped bars + a WARNING.

        DEC-LAKE-READ-UNIQUE-001 supersedes the whole-symbol skip for a
        duplicate-only poison: the read gate collapses the duplicate keep-FIRST
        BEFORE the resampler tripwire, so the symbol serves its REAL bars
        (warned, never silent) instead of vanishing from the screen. The
        tripwire + skip set remain the guard for a genuinely CORRUPT file.
        (Before the read gate this same fixture pinned skipped == 1.)
        """
        from structlog.testing import capture_logs

        _seed_good_and_poisoned(tmp_path)
        with capture_logs() as logs:
            result = compute_spot_candidates(
                lake_root=tmp_path, interval="5minute", date="2024-06-13", time=None, exchange=None
            )
        # BOTH symbols rank — the duplicate was healed at read, nothing skipped.
        assert {s.instrument.symbol for s in result.snapshots} == {"NIFTY", "SENSEX"}
        assert result.skipped == 0
        # ONE aggregated warning PER READ (the screen makes several bounded reads
        # of the same poisoned month — session + prev-day probes), every one
        # naming the poisoned SENSEX partition, never a per-row storm.
        dedups = [e for e in logs if e["event"] == "lake.read.duplicates_dropped"]
        assert dedups, "the collapse must be warned, never silent"
        assert all("SENSEX" in str(e["source"]) for e in dedups)
        assert all(e["dropped"] == 1 for e in dedups)

    def test_movers_route_returns_200_with_poisoned_symbol(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        """FAILS on unfixed main: the route 500'd on the poisoned symbol."""
        _seed_good_and_poisoned(tmp_path)
        client = TestClient(
            build_app(DashboardState(), instrument_store=store),
            raise_server_exceptions=False,
        )
        resp = client.get(
            "/api/v1/movers",
            params={
                "asset": "spot",
                "interval": "5minute",
                "date": "2024-06-13",
                "top_n": 5,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        # The poisoned SENSEX is healed at read (keep-FIRST) and ranks alongside
        # NIFTY — no skip, no 500 (DEC-LAKE-READ-UNIQUE-001).
        assert {r["symbol"] for r in body["volume"]} == {"NIFTY", "SENSEX"}
        assert body["skipped_count"] == 0


# ────────────────────────────────────────────────────────────────────────
# Price-insane stored rows — skip + count, never a 500, never a launder
# (DEC-READ-GUARD-PARITY-001, GAP-2)
# ────────────────────────────────────────────────────────────────────────

#: The frozen pre-fix spot row key set (the /movers byte-identity vocabulary —
#: the `_SPOT_ROW_KEYS` idiom of tests/integration/test_dashboard_movers.py).
_SPOT_ROW_KEYS = frozenset(
    {
        "symbol",
        "name",
        "exchange",
        "segment",
        "ltp",
        "change",
        "pct_change",
        "value_cr",
        "volume",
        "bar_count",
    }
)

#: The spot payload's four ranked-category keys (every ranking a symbol can appear in).
_SPOT_CATEGORIES = ("price_gainers", "price_losers", "volume", "value")


def _plant_inverted_1m(
    lake_root: Path,
    *,
    exchange: str,
    symbol: str,
    clean_offsets: Sequence[int],
    inverted_offset: int,
) -> None:
    """RAW-plant a 1-minute partition: flat clean rows + ONE inverted row.

    The inverted row ``(open=95.0, high=95.0, low=100.5, close=95.0)`` violates
    the OHLC ordering, so ``CandleBar`` REFUSES it (models._validate_price_bar)
    and the plant must be raw pyarrow — the #1067 legacy-poisoned-lake
    convention (exactly like ``_poison_1minute_partition`` above). Offsets are
    minutes past 09:35 IST (04:05 UTC) on ``_DAY``.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    offsets = sorted([*clean_offsets, inverted_offset])
    dest = lake_root / "bars" / exchange / "INDEX" / symbol / "1minute" / "2024" / "06.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    stamps = [
        datetime(_DAY.year, _DAY.month, _DAY.day, 4, 5 + offset, tzinfo=UTC) for offset in offsets
    ]
    lows = [100.5 if offset == inverted_offset else 95.0 for offset in offsets]
    table = pa.table(
        {
            "timestamp": pa.array(stamps, type=pa.timestamp("us", tz="UTC")),
            "open": pa.array([95.0] * len(offsets), type=pa.float64()),
            "high": pa.array([95.0] * len(offsets), type=pa.float64()),
            "low": pa.array(lows, type=pa.float64()),
            "close": pa.array([95.0] * len(offsets), type=pa.float64()),
            "volume": pa.array([10] * len(offsets), type=pa.int64()),
            "open_interest": pa.array([None] * len(offsets), type=pa.int64()),
        }
    )
    pq.write_table(table, dest)


def _spot_client(store: InstrumentStore) -> TestClient:
    return TestClient(
        build_app(DashboardState(), instrument_store=store),
        raise_server_exceptions=False,
    )


def _ranked_symbols(body: dict[str, object]) -> set[str]:
    """Every symbol appearing in ANY of the four spot rankings."""
    symbols: set[str] = set()
    for category in _SPOT_CATEGORIES:
        rows = body[category]
        assert isinstance(rows, list)
        symbols.update(row["symbol"] for row in rows)
    return symbols


class TestPriceInsaneSymbolSkippedNot500:
    """GAP-2 (DEC-READ-GUARD-PARITY-001) under the landed engine-parity screen.

    RECONCILED at the 2026-07-15 club-T fold (MIGRATION.md #1811): main's
    DEC-DASHBOARD-READ-SCREEN-001 (#1759, the QUAD club) landed
    ``bar_screen.screen_frame`` at the SAME ``_read_cut_1m_frame`` seam in
    flight — the insane row is now DROPPED row-level (counted; the ONE
    sanctioned engine divergence: the good remainder still renders) BEFORE
    this pack's ``validate_frame_price_sanity`` gate, which STANDS behind it
    as the inert fail-loud belt (it raises only if the screen ever
    regresses). The GAP-2 INTENT is unchanged and pinned here on the composed
    tree: an inverted stored 1m row is NEVER a silently laundered resampled
    bucket and NEVER a CandleBar bare-``ValueError`` 500 — its values are
    simply never served (the whole-symbol skip+count expectations of the
    pre-fold pack were re-derived onto the landed drop semantics, the
    in-flight-twin precedent of MIGRATION.md #1719).
    """

    def test_inverted_ohlc_row_dropped_multi_row_bucket_never_laundered(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        """FAILS-BEFORE (silent-launder mode): the inverted row shared a
        5-minute bucket with clean rows, so the resampled bucket aggregated
        self-consistent-looking and the corrupt VALUES were SERVED. Now (the
        landed screen): the inverted row is DROPPED row-level — the bucket
        aggregates ONLY the clean rows (ltp/volume prove it), the symbol
        still ranks, and no whole-symbol skip fires (``skipped_count == 0``,
        no ``movers.parquet_skip``)."""
        from structlog.testing import capture_logs

        ParquetWriter(lake_root=tmp_path).write(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=[_min_bar(0, close="22000", volume=500), _min_bar(1, close="22010", volume=400)],
        )
        _plant_inverted_1m(
            tmp_path, exchange="BSE", symbol="SENSEX", clean_offsets=(0, 1), inverted_offset=2
        )
        client = _spot_client(store)
        with capture_logs() as logs:
            resp = client.get(
                "/api/v1/movers",
                params={
                    "asset": "spot",
                    "interval": "5minute",
                    "date": "2024-06-13",
                    "top_n": 5,
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        # The good remainder renders (the sanctioned screen disposition) …
        assert {"NIFTY", "SENSEX"} <= _ranked_symbols(body)
        assert body["skipped_count"] == 0
        skips = [e for e in logs if e.get("event") == "movers.parquet_skip"]
        assert skips == []  # row-level drop, never a whole-symbol skip
        # … and the served figures aggregate ONLY the clean rows: the
        # inverted row's fabricated low (100.5) / its 10-share turnover are
        # never laundered into the 09:35 bucket.
        sensex_rows = [r for r in body["volume"] if r["symbol"] == "SENSEX"]
        assert len(sensex_rows) == 1
        assert sensex_rows[0]["ltp"] == "₹95.00"
        assert sensex_rows[0]["volume"] == 20  # 2 clean rows x 10; NOT 30

    def test_inverted_ohlc_single_row_bucket_dropped_not_500(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        """FAILS-BEFORE (ValueError-500 mode): the inverted row ALONE in its
        5-minute bucket resampled to ITSELF, so ``_row_to_bar`` → ``CandleBar``
        raised a bare ``ValueError`` — deliberately NOT in the skip set (the
        code-bug class) — and the whole /movers screen 500'd. Now (the landed
        screen): the row is DROPPED before the resample, so the corrupt
        bucket VANISHES and the symbol serves from its clean buckets — 200,
        the 500 mode is structurally dead.

        (Deviation from the plan's ``interval=1minute`` shape, recorded in
        MIGRATION.md #1811: a whole-day TF-1minute request is served by the
        STORED-partition read — ``_stored_serves_cutoff(1, ·)`` is always
        true — which bypasses the screened resample seam; the seam's
        ValueError-500 mode is the single-corrupt-row BUCKET on a derived TF.)
        """
        ParquetWriter(lake_root=tmp_path).write(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=[_min_bar(0, close="22000", volume=500)],
        )
        # Offset 5 = 09:40 IST — its own 5-minute bucket, no clean sibling row.
        _plant_inverted_1m(
            tmp_path, exchange="BSE", symbol="SENSEX", clean_offsets=(0, 1), inverted_offset=5
        )
        client = _spot_client(store)
        resp = client.get(
            "/api/v1/movers",
            params={
                "asset": "spot",
                "interval": "5minute",
                "date": "2024-06-13",
                "top_n": 5,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "SENSEX" in _ranked_symbols(body)  # serves from the clean bucket
        assert body["skipped_count"] == 0
        sensex_rows = [r for r in body["volume"] if r["symbol"] == "SENSEX"]
        assert len(sensex_rows) == 1
        # The 09:40 corrupt bucket vanished: only the 09:35 clean bucket serves.
        assert sensex_rows[0]["bar_count"] == 1
        assert sensex_rows[0]["ltp"] == "₹95.00"

    def test_top_value_turnover_excludes_the_dropped_insane_row(self, tmp_path: Path) -> None:
        """The ``spot_session_value_paisa`` co-seam (it shares the ONE screened
        ``_read_cut_1m_frame``): the dropped insane row's turnover never
        reaches the VALUE ranking — the symbol's value counts ONLY its clean
        rows, and the clean sibling's turnover is the untouched HLC3 law."""
        ParquetWriter(lake_root=tmp_path).write(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=[_min_bar(0, close="22000", volume=500), _min_bar(1, close="22010", volume=400)],
        )
        _plant_inverted_1m(
            tmp_path, exchange="BSE", symbol="SENSEX", clean_offsets=(0, 1), inverted_offset=2
        )
        result = compute_spot_candidates(
            lake_root=tmp_path, interval="5minute", date="2024-06-13", time=None, exchange=None
        )
        assert {s.instrument.symbol for s in result.snapshots} == {"NIFTY", "SENSEX"}
        assert result.skipped == 0
        by_symbol = {s.instrument.symbol: s for s in result.snapshots}
        # Flat bars ⇒ HLC3 == close: 22000x500 + 22010x400 rupees, in paisa.
        assert by_symbol["NIFTY"].value_paisa == 1_980_400_000
        # SENSEX: ONLY the 2 clean flat-95 rows x 10 shares = 1900 rupees —
        # the dropped inverted row's 10-share turnover is NOT counted (a
        # laundered HLC3 over (95+100.5+95)/3 would move this figure).
        assert by_symbol["SENSEX"].value_paisa == 190_000

    def test_corrupt_row_after_cutoff_early_asof_read_still_serves(self, tmp_path: Path) -> None:
        """The blast-radius pin (the #1719 strictly-narrower-degrade law): the
        gate validates the POST-cut frame — a corrupt row strictly AFTER the
        as-of cutoff never blanks an earlier-cut view."""
        _plant_inverted_1m(
            tmp_path, exchange="BSE", symbol="SENSEX", clean_offsets=(0, 1), inverted_offset=15
        )
        # 09:38 IST (a non-anchor pick ⇒ as-of-instant cut) keeps offsets 0-3
        # only; the inverted row at offset 15 (09:50 IST) is never validated.
        result = compute_spot_candidates(
            lake_root=tmp_path, interval="5minute", date="2024-06-13", time="09:38", exchange=None
        )
        assert {s.instrument.symbol for s in result.snapshots} == {"SENSEX"}
        assert result.skipped == 0

    def test_clean_lake_spot_payload_unchanged_after_gate(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        """Clean-lake belt over the frozen pins: the gated read serves the
        exact pre-fix row vocabulary + values — clean lakes are untouched."""
        ParquetWriter(lake_root=tmp_path).write(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=[_min_bar(0, close="22000", volume=500), _min_bar(1, close="22010", volume=400)],
        )
        client = _spot_client(store)
        resp = client.get(
            "/api/v1/movers",
            params={
                "asset": "spot",
                "interval": "5minute",
                "date": "2024-06-13",
                "top_n": 5,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["skipped_count"] == 0
        (row,) = body["volume"]
        assert set(row) == _SPOT_ROW_KEYS  # the frozen pre-fix key-name set
        assert row["symbol"] == "NIFTY"
        assert row["ltp"] == "₹22,010.00"
        assert row["volume"] == 900
        assert row["value_cr"] == "1.98"  # 1_980_400_000 paisa / 10^9, 2dp
