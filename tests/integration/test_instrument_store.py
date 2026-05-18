"""Integration tests for :class:`brutex.data.instrument_store.InstrumentStore`.

Exercises the full DuckDB round-trip: schema creation, upsert,
idempotent re-upsert, temporal queries, dedup verification,
snapshot loading. Gated on ``duckdb`` because the store class
lazy-imports it.

These tests are the **real-time proof** of the three guarantees
the user asked for:

1. **Uniqueness** \u2014 :meth:`InstrumentStore.verify_integrity` +
   ``IntegrityReport.pk_duplicates`` asserted to zero after every
   write path. DuckDB's PK constraint makes duplicates
   structurally impossible, but we still verify.
2. **Deduplication on re-run** \u2014 upsert the same 1000-instrument
   batch 10 times; the final ``count()`` is unchanged and every
   ``raw_hash`` matches across runs.
3. **O(1) lookup on the hot path** \u2014 covered by the companion
   bench file ``tests/bench/test_instrument_snapshot_o1.py``
   which asserts ``snapshot.get()`` p99 < 1\u00a0\u00b5s.
"""

from __future__ import annotations

import hashlib
import zlib
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.errors import CatalogError
from brutex.data.instrument_store import (
    SCHEMA_VERSION,
    ContractRecord,
    ExpiryRecord,
    InstrumentRun,
    InstrumentSnapshot,
    InstrumentStore,
    compute_raw_hash,
    open_instrument_store,
)
from brutex.models import (
    Exchange,
    Instrument,
    InstrumentType,
    Segment,
)

pytest.importorskip("duckdb")


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


def _csv_hash(tag: str = "fixture") -> str:
    return hashlib.sha256(tag.encode()).hexdigest()


# Per DEC-P0-018 (PR #40): hoisted to module scope from the per-call inline
# dict that was rebuilt on every `_idx()` invocation (Agent 1 LOW finding).
# Track 1 verified securityIds from CLAUDE.md §6 ONLY — the prior
# FINNIFTY/MIDCPNIFTY/NIFTYNXT50 entries were UNVERIFIED guesses (per
# docs/track2/PLAN_FINALIZATION_2026-05-16.md:339-345) and were removed
# (Agent 3 MEDIUM finding). Track 2 symbols fall through to crc32 fallback.
_TRACK1_TOKEN_MAP: dict[str, int] = {
    "NIFTY": 13,
    "BANKNIFTY": 25,
    "SENSEX": 51,
    "INDIAVIX": 21,
}


def _idx(symbol: str, *, exchange: Exchange = Exchange.NSE) -> Instrument:
    """Build a minimal valid IDX instrument.

    Per DEC-P0-011 (PR #33): exchange_token from a stable per-symbol map so each
    test-fixture instrument has a unique PK (previous fixed `26000` collided
    when multiple indices were inserted in the same batch). Track 1 verified
    securityIds (NIFTY=13, BANKNIFTY=25, SENSEX=51, INDIAVIX=21 per CLAUDE.md
    §6) are looked up in `_TRACK1_TOKEN_MAP`; unknown symbols fall back to
    crc32 with full 32-bit mask.

    Per DEC-P0-018 (PR #40): the mask was widened from 24-bit (`& 0xFFFFFF`)
    to 32-bit (`& 0xFFFFFFFF`) to reduce collision probability for arbitrary
    symbol inputs. This is a test-only fixture — do NOT reuse for fuzz
    harnesses (`hypothesis.strategies.text()`) without re-reviewing the
    collision bound. crc32 is NOT cryptographically secure.
    """
    token = _TRACK1_TOKEN_MAP.get(symbol, zlib.crc32(symbol.encode()) & 0xFFFFFFFF)
    return Instrument(
        exchange=exchange,
        exchange_token=token,
        trading_symbol=symbol,
        vendor_symbol=f"{exchange}-{symbol}",
        name=f"{symbol} 50",
        instrument_type=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_exchange_token=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        freeze_quantity=None,
        expiry_date=None,
        strike_price=None,
        is_reserved=False,
        buy_allowed=True,
        sell_allowed=True,
    )


def _option(
    *,
    underlying: str,
    strike: int,
    side: InstrumentType,
    expiry: date,
    exchange: Exchange = Exchange.NSE,
    lot_size: int = 25,
) -> Instrument:
    """Build a minimal valid option instrument."""
    tag = f"{exchange}-{underlying}-{expiry.strftime('%d%b%y')}-{strike}-{side.value}"
    return Instrument(
        exchange=exchange,
        exchange_token=hash(tag) & 0x7FFFFFFF,
        trading_symbol=tag.replace(f"{exchange}-", ""),
        vendor_symbol=tag,
        name=f"{underlying} {strike}",
        instrument_type=side,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol=underlying,
        underlying_exchange_token=26000,
        lot_size=lot_size,
        tick_size=Decimal("0.05"),
        freeze_quantity=1800,
        expiry_date=expiry,
        strike_price=Decimal(str(strike)),
        is_reserved=False,
        buy_allowed=True,
        sell_allowed=True,
    )


def _make_chain(
    underlying: str,
    expiry: date,
    strikes: range,
) -> list[Instrument]:
    """Build a CE+PE chain on *underlying* for *expiry*."""
    out: list[Instrument] = []
    for k in strikes:
        out.append(
            _option(
                underlying=underlying,
                strike=k,
                side=InstrumentType.CE,
                expiry=expiry,
            )
        )
        out.append(
            _option(
                underlying=underlying,
                strike=k,
                side=InstrumentType.PE,
                expiry=expiry,
            )
        )
    return out


# ────────────────────────────────────────────────────────────────────────
# Construction / schema
# ────────────────────────────────────────────────────────────────────────


class TestOpenAndSchema:
    def test_open_creates_file_and_tables(self, tmp_path: Path) -> None:
        db = tmp_path / "instruments.duckdb"
        with InstrumentStore.open(db) as store:
            assert store.path == db
            assert store.schema_version == SCHEMA_VERSION
        assert db.exists()

    def test_reopen_is_idempotent(self, tmp_path: Path) -> None:
        db = tmp_path / "instruments.duckdb"
        with InstrumentStore.open(db) as s:
            s.upsert_many(
                [_idx("NIFTY")],
                snapshot_date=date(2025, 4, 15),
                run_id="r1",
                csv_source_sha256=_csv_hash("day1"),
            )
        # Re-open should not wipe existing rows.
        with InstrumentStore.open(db) as s:
            assert s.count() == 1
            assert s.schema_version == SCHEMA_VERSION

    def test_context_manager_closes(self, tmp_path: Path) -> None:
        db = tmp_path / "c.duckdb"
        with open_instrument_store(db) as s:
            assert s.count() == 0
        # After close, operations raise.
        with pytest.raises(CatalogError, match="closed"):
            s.count()


# ------------------------------------------------------------------
# Uniqueness + deduplication - proof with real-time verification
# ------------------------------------------------------------------


class TestUniquenessAndDedup:
    """The two guarantees you asked for, with real-time verification."""

    def test_upsert_same_row_100_times_converges_to_one(self, tmp_path: Path) -> None:
        """Pulling the same row 100 times \u2192 exactly one row in the table."""
        with InstrumentStore.open(tmp_path / "db") as s:
            snap_date = date(2025, 4, 15)
            nifty = _idx("NIFTY")
            for _ in range(100):
                s.upsert_many(
                    [nifty],
                    snapshot_date=snap_date,
                    run_id="r",
                    csv_source_sha256=_csv_hash(),
                )
            # Exactly one row survived.
            assert s.count(in_scope_only=False) == 1
            # raw_hash is unchanged across pulls \u2014 idempotent.
            fetched = s.get("NSE-NIFTY")
            assert fetched is not None
            assert compute_raw_hash(fetched) == compute_raw_hash(nifty)

    def test_verify_integrity_finds_no_duplicates_after_100_upserts(self, tmp_path: Path) -> None:
        """``verify_integrity()`` returns ``pk_duplicates == 0`` always."""
        instruments = [
            _idx("NIFTY"),
            _idx("BANKNIFTY"),
            _idx("SENSEX", exchange=Exchange.BSE),
            _idx("INDIAVIX"),
        ]
        with InstrumentStore.open(tmp_path / "db") as s:
            for _ in range(100):
                s.upsert_many(
                    instruments,
                    snapshot_date=date(2025, 4, 15),
                    run_id="r",
                    csv_source_sha256=_csv_hash(),
                )
            report = s.verify_integrity()

        assert report.total_rows == 4
        assert report.distinct_pk_count == 4
        assert report.pk_duplicates == 0
        assert report.raw_hash_mismatches == 0
        assert report.is_clean is True

    def test_assert_clean_passes_on_clean_store(self, tmp_path: Path) -> None:
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_many(
                [_idx("NIFTY")],
                snapshot_date=date(2025, 4, 15),
                run_id="r",
                csv_source_sha256=_csv_hash(),
            )
            s.assert_clean()  # must not raise

    def test_different_snapshot_dates_are_different_rows(self, tmp_path: Path) -> None:
        """Temporal history: same symbol, different dates \u2192 N rows."""
        with InstrumentStore.open(tmp_path / "db") as s:
            for d in (date(2025, 4, 10), date(2025, 4, 11), date(2025, 4, 12)):
                s.upsert_many(
                    [_idx("NIFTY")],
                    snapshot_date=d,
                    run_id=f"r-{d}",
                    csv_source_sha256=_csv_hash(str(d)),
                )
            assert s.count(in_scope_only=False) == 3
            assert sorted(s.snapshot_dates()) == [
                date(2025, 4, 10),
                date(2025, 4, 11),
                date(2025, 4, 12),
            ]
            # All three survive integrity check.
            assert s.verify_integrity().is_clean is True


# ────────────────────────────────────────────────────────────────────────
# Read API
# ────────────────────────────────────────────────────────────────────────


class TestReadAPI:
    def test_get_returns_latest_by_default(self, tmp_path: Path) -> None:
        with InstrumentStore.open(tmp_path / "db") as s:
            for d in (date(2025, 4, 10), date(2025, 4, 15)):
                s.upsert_many(
                    [_idx("NIFTY")],
                    snapshot_date=d,
                    run_id=f"r-{d}",
                    csv_source_sha256=_csv_hash(str(d)),
                )
            fetched = s.get("NSE-NIFTY")
            assert fetched is not None

    def test_get_as_of_returns_temporal_state(self, tmp_path: Path) -> None:
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_many(
                [_idx("NIFTY")],
                snapshot_date=date(2025, 1, 1),
                run_id="r1",
                csv_source_sha256=_csv_hash("2025-01-01"),
            )
            s.upsert_many(
                [_idx("NIFTY")],
                snapshot_date=date(2025, 4, 15),
                run_id="r2",
                csv_source_sha256=_csv_hash("2025-04-15"),
            )
            # as_of 2025-02-01 should return the Jan snapshot.
            result = s.get("NSE-NIFTY", as_of=date(2025, 2, 1))
            assert result is not None

    def test_get_missing_returns_none(self, tmp_path: Path) -> None:
        with InstrumentStore.open(tmp_path / "db") as s:
            assert s.get("NOPE") is None
            assert s.exists("NOPE") is False

    def test_list_in_scope_returns_idx_instruments(self, tmp_path: Path) -> None:
        """DEC-P0-011 (PR #33): rewrite from FNO chain → 4 IDX instruments.

        Track 1 universe filter (brutex/universe.py:is_in_scope()) admits only
        IDX in (NIFTY, BANKNIFTY, SENSEX, INDIA VIX). list_in_scope() returns
        the in-scope set. FNO/options filtering is Track 2 functionality.
        """
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_many(
                [_idx("NIFTY"), _idx("BANKNIFTY")],
                snapshot_date=date(2025, 4, 15),
                run_id="r",
                csv_source_sha256=_csv_hash(),
            )
            cash = s.list_in_scope(segment=Segment.CASH)
            assert len(cash) == 2
            assert {i.trading_symbol for i in cash} == {"NIFTY", "BANKNIFTY"}

    def test_list_indices_returns_only_idx(self, tmp_path: Path) -> None:
        """DEC-P0-011: rewrite from mixed IDX+FNO → Track 1's 4 in-scope indices."""
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_many(
                [
                    _idx("NIFTY"),
                    _idx("BANKNIFTY"),
                    _idx("SENSEX", exchange=Exchange.BSE),
                    _idx("INDIAVIX"),
                ],
                snapshot_date=date(2025, 4, 15),
                run_id="r",
                csv_source_sha256=_csv_hash(),
            )
            indices = s.list_indices()
            assert len(indices) == 4
            assert {i.trading_symbol for i in indices} == {
                "NIFTY",
                "BANKNIFTY",
                "SENSEX",
                "INDIAVIX",
            }

    @pytest.mark.skip(
        reason="Track 2 F&O functionality — list_options requires options data; revisit in Phase 2"
    )
    def test_list_options_by_underlying(self, tmp_path: Path) -> None:
        """Track 2 work — options listing requires F&O instrument data."""

    def test_count_by_underlying_with_indices(self, tmp_path: Path) -> None:
        """DEC-P0-011 + DEC-P0-014 (PR #36 Agent 3 audit-fix): IDX
        instruments have NULL ``underlying_symbol`` per Phase A schema.
        ``count_by_underlying()`` filters ``WHERE underlying_symbol IS NOT
        NULL``, so the call MUST return an empty dict for IDX-only data.

        The prior assertion ``isinstance(counts, dict)`` was a tautological
        false-OK (Agent 3 HIGH finding) — ``isinstance`` of any dict-return
        is trivially true. This assertion now pins the schema invariant.
        """
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_many(
                [_idx("NIFTY"), _idx("BANKNIFTY")],
                snapshot_date=date(2025, 4, 15),
                run_id="r",
                csv_source_sha256=_csv_hash(),
            )
            counts = s.count_by_underlying()

        # Real behavior pin: IDX rows have NULL underlying_symbol; SQL
        # filters them out; counts MUST be the empty dict (not just "a dict").
        assert counts == {}


# ────────────────────────────────────────────────────────────────────────
# InstrumentSnapshot \u2014 hot-path view
# ────────────────────────────────────────────────────────────────────────


class TestInstrumentSnapshot:
    """DEC-P0-011 (PR #33): rewritten from FNO chains \u2192 4 IDX instruments."""

    def test_load_snapshot_builds_frozen_view(self, tmp_path: Path) -> None:
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_many(
                [
                    _idx("NIFTY"),
                    _idx("BANKNIFTY"),
                    _idx("SENSEX", exchange=Exchange.BSE),
                    _idx("INDIAVIX"),
                ],
                snapshot_date=date(2025, 4, 15),
                run_id="r",
                csv_source_sha256=_csv_hash(),
            )
            snapshot = s.load_snapshot()
        assert isinstance(snapshot, InstrumentSnapshot)
        assert len(snapshot) == 4

    def test_snapshot_o1_by_symbol(self, tmp_path: Path) -> None:
        """``snapshot.get()`` is a plain dict lookup \u2014 constant time."""
        instruments = [
            _idx("NIFTY"),
            _idx("BANKNIFTY"),
            _idx("SENSEX", exchange=Exchange.BSE),
            _idx("INDIAVIX"),
        ]
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_many(
                instruments,
                snapshot_date=date(2025, 4, 15),
                run_id="r",
                csv_source_sha256=_csv_hash(),
            )
            snapshot = s.load_snapshot()
        for instr in instruments:
            fetched = snapshot.get(instr.vendor_symbol)
            assert fetched is not None
            assert fetched.vendor_symbol == instr.vendor_symbol

    def test_snapshot_by_segment_is_pre_indexed(self, tmp_path: Path) -> None:
        """All 4 Track 1 indices are CASH segment (no FNO in Track 1 scope)."""
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_many(
                [_idx("NIFTY"), _idx("BANKNIFTY")],
                snapshot_date=date(2025, 4, 15),
                run_id="r",
                csv_source_sha256=_csv_hash(),
            )
            snap = s.load_snapshot()
        cash = snap.by_segment(Segment.CASH)
        assert len(cash) == 2

    @pytest.mark.skip(
        reason="Track 2 F&O functionality \u2014 by_underlying needs options data; revisit in Phase 2"
    )
    def test_snapshot_by_underlying_is_pre_indexed(self, tmp_path: Path) -> None:
        """Track 2 work \u2014 derivatives grouping requires F&O instrument data."""

    def test_snapshot_meta_map_direct_handoff(self, tmp_path: Path) -> None:
        """``meta_map()`` produces the exact dict ExecutionSimulator wants."""
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_many(
                [_idx("NIFTY"), _idx("BANKNIFTY")],
                snapshot_date=date(2025, 4, 15),
                run_id="r",
                csv_source_sha256=_csv_hash(),
            )
            snap = s.load_snapshot()

        from brutex.execution.simulator import ExecutionSimulator

        sim = ExecutionSimulator(
            initial_cash=Decimal("1_000_000"),
            instruments=snap.meta_map(),
            default_qty=25,
            slippage_bps=Decimal("0"),
        )
        assert len(sim.positions) == 0
        assert sim.cash == Decimal("1_000_000")

    def test_snapshot_rejects_duplicates_at_construction(self) -> None:
        nifty = _idx("NIFTY")
        with pytest.raises(CatalogError, match="duplicate vendor_symbol"):
            InstrumentSnapshot([nifty, nifty])

    def test_snapshot_is_frozen(self, tmp_path: Path) -> None:
        """Snapshot has no mutator \u2014 thread-safety by construction."""
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_many(
                [_idx("NIFTY")],
                snapshot_date=date(2025, 4, 15),
                run_id="r",
                csv_source_sha256=_csv_hash(),
            )
            snap = s.load_snapshot()
        # No public mutator exists; __slots__ excludes attribute assignment.
        assert snap.get("NSE-NIFTY") is not None
        with pytest.raises(AttributeError):
            snap.foo = "bar"  # type: ignore[attr-defined]


# ────────────────────────────────────────────────────────────────────────
# InstrumentRun metadata
# ────────────────────────────────────────────────────────────────────────


class TestInstrumentRunMetadata:
    def test_record_run_and_read_back(self, tmp_path: Path) -> None:
        with InstrumentStore.open(tmp_path / "db") as s:
            run = InstrumentRun(
                run_id="r1",
                snapshot_date=date(2025, 4, 15),
                fetched_at=datetime(2025, 4, 15, 9, 15, tzinfo=UTC),
                csv_source_sha256=_csv_hash("2025-04-15"),
                total_raw_rows=75000,
                integrity_failures=0,
                in_scope_count=6247,
                out_of_scope_count=68753,
                index_count=8,
                futures_count=312,
                options_count=5927,
                bse_count=1245,
                nse_count=5002,
                code_commit_sha="abc123",
            )
            s.record_run(run)
            fetched = s.latest_run()
        assert fetched is not None
        assert fetched.run_id == "r1"
        assert fetched.in_scope_count == 6247
        assert fetched.csv_source_sha256 == _csv_hash("2025-04-15")

    def test_record_run_replaces_on_duplicate_id(self, tmp_path: Path) -> None:
        def _r(inscope: int) -> InstrumentRun:
            return InstrumentRun(
                run_id="r1",
                snapshot_date=date(2025, 4, 15),
                fetched_at=datetime(2025, 4, 15, 9, 15, tzinfo=UTC),
                csv_source_sha256=_csv_hash(),
                total_raw_rows=1,
                integrity_failures=0,
                in_scope_count=inscope,
                out_of_scope_count=0,
                index_count=0,
                futures_count=0,
                options_count=0,
                bse_count=0,
                nse_count=0,
                code_commit_sha="abc",
            )

        with InstrumentStore.open(tmp_path / "db") as s:
            s.record_run(_r(1))
            s.record_run(_r(99))
            assert s.latest_run() is not None
            assert s.latest_run().in_scope_count == 99  # type: ignore[union-attr]


# ────────────────────────────────────────────────────────────────────────
# compute_raw_hash \u2014 pure function
# ────────────────────────────────────────────────────────────────────────


class TestComputeRawHash:
    def test_identical_instruments_produce_identical_hashes(self) -> None:
        a = _idx("NIFTY")
        b = _idx("NIFTY")
        assert compute_raw_hash(a) == compute_raw_hash(b)

    def test_different_strike_produces_different_hashes(self) -> None:
        ce_a = _option(
            underlying="NIFTY",
            strike=24000,
            side=InstrumentType.CE,
            expiry=date(2025, 5, 29),
        )
        ce_b = _option(
            underlying="NIFTY",
            strike=24050,
            side=InstrumentType.CE,
            expiry=date(2025, 5, 29),
        )
        assert compute_raw_hash(ce_a) != compute_raw_hash(ce_b)

    def test_hex_64_chars(self) -> None:
        h = compute_raw_hash(_idx("NIFTY"))
        assert len(h) == 64
        int(h, 16)  # valid hex


# ────────────────────────────────────────────────────────────────────────
# Operations on closed store
# ────────────────────────────────────────────────────────────────────────


class TestClosedStore:
    def test_operations_raise_after_close(self, tmp_path: Path) -> None:
        s = InstrumentStore.open(tmp_path / "db")
        s.close()
        with pytest.raises(CatalogError, match="closed"):
            s.get("NSE-NIFTY")
        with pytest.raises(CatalogError, match="closed"):
            s.upsert_many(
                [_idx("NIFTY")],
                snapshot_date=date(2025, 4, 15),
                run_id="r",
                csv_source_sha256=_csv_hash(),
            )
        with pytest.raises(CatalogError, match="closed"):
            s.count()
        with pytest.raises(CatalogError, match="closed"):
            s.verify_integrity()

    def test_close_is_idempotent(self, tmp_path: Path) -> None:
        s = InstrumentStore.open(tmp_path / "db")
        s.close()
        s.close()  # must not raise


# ────────────────────────────────────────────────────────────────────────
# Idempotence stress test \u2014 pull the same batch many times
# ────────────────────────────────────────────────────────────────────────


@pytest.mark.skip(  # pyright: ignore[reportUntypedClassDecorator]
    reason=(
        "DEC-P0-011 (PR #33): Track 2 F&O functionality. The expiries table "
        "stores option expiry dates per (exchange, underlying, year) \u2014 meaningless "
        "for Track 1 spot indexes which don't have expiries. Revisit in Phase 2 "
        "when Track 2 options pipeline lands."
    ),
)
class TestExpiriesTable:
    """Phase B persistence \u2014 expiries per (exchange, underlying, year)."""

    @staticmethod
    def _exp(
        *,
        underlying: str = "NIFTY",
        expiry: date,
        snapshot: date,
        run_id: str = "r1",
        exchange: str = "NSE",
    ) -> ExpiryRecord:
        return ExpiryRecord(
            exchange=exchange,
            underlying_symbol=underlying,
            expiry_date=expiry,
            snapshot_date=snapshot,
            pulled_year=expiry.year,
            fetched_at=datetime(snapshot.year, snapshot.month, snapshot.day, tzinfo=UTC),
            run_id=run_id,
        )

    def test_upsert_same_expiry_100_times_is_idempotent(self, tmp_path: Path) -> None:
        exp = self._exp(expiry=date(2025, 5, 29), snapshot=date(2025, 4, 15))
        with InstrumentStore.open(tmp_path / "db") as s:
            for _ in range(100):
                s.upsert_expiries([exp])
            assert s.count_expiries() == 1
            results = s.list_expiries(underlying_symbol="NIFTY")
            assert len(results) == 1

    def test_list_expiries_filters_underlying(self, tmp_path: Path) -> None:
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_expiries(
                [
                    self._exp(
                        underlying="NIFTY",
                        expiry=date(2025, 5, 29),
                        snapshot=date(2025, 4, 15),
                    ),
                    self._exp(
                        underlying="BANKNIFTY",
                        expiry=date(2025, 5, 29),
                        snapshot=date(2025, 4, 15),
                    ),
                    self._exp(
                        underlying="NIFTY",
                        expiry=date(2025, 6, 26),
                        snapshot=date(2025, 4, 15),
                    ),
                ]
            )
            nifty = s.list_expiries(underlying_symbol="NIFTY")
            banknifty = s.list_expiries(underlying_symbol="BANKNIFTY")
        assert len(nifty) == 2
        assert len(banknifty) == 1
        assert sorted(e.expiry_date for e in nifty) == [
            date(2025, 5, 29),
            date(2025, 6, 26),
        ]

    def test_temporal_history_preserved(self, tmp_path: Path) -> None:
        """Same expiry on two different snapshot_dates \u2192 two rows."""
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_expiries(
                [
                    self._exp(
                        expiry=date(2025, 5, 29),
                        snapshot=date(2025, 4, 10),
                        run_id="r1",
                    ),
                ]
            )
            s.upsert_expiries(
                [
                    self._exp(
                        expiry=date(2025, 5, 29),
                        snapshot=date(2025, 4, 15),
                        run_id="r2",
                    ),
                ]
            )
            # count_expiries() counts DISTINCT (exch, underlying, expiry) triplets,
            # so the two snapshots on the same triplet collapse to 1.
            assert s.count_expiries() == 1
            # as_of(2025-04-11) returns the 2025-04-10 snapshot.
            rows = s.list_expiries(underlying_symbol="NIFTY", as_of=date(2025, 4, 11))
            assert len(rows) == 1
            assert rows[0].snapshot_date == date(2025, 4, 10)


@pytest.mark.skip(  # pyright: ignore[reportUntypedClassDecorator]
    reason=(
        "DEC-P0-011 (PR #33): Track 2 F&O functionality. The contracts table "
        "stores option contract legs per (underlying, expiry) \u2014 meaningless for "
        "Track 1 spot indexes. Revisit in Phase 2."
    ),
)
class TestContractsTable:
    """Phase C persistence \u2014 contract legs per (underlying, expiry)."""

    @staticmethod
    def _contract(
        *,
        underlying: str = "NIFTY",
        expiry: date,
        strike: int,
        side: InstrumentType,
        snapshot: date,
        run_id: str = "r1",
    ) -> ContractRecord:
        symbol = f"NSE-{underlying}-{expiry.strftime('%d%b%y')}-{strike}-{side.value}"
        return ContractRecord(
            exchange="NSE",
            underlying_symbol=underlying,
            expiry_date=expiry,
            contract_symbol=symbol,
            snapshot_date=snapshot,
            instrument_type=side,
            strike_price=Decimal(str(strike)),
            lot_size=25,
            freeze_quantity=1800,
            tick_size=Decimal("0.05"),
            fetched_at=datetime(snapshot.year, snapshot.month, snapshot.day, tzinfo=UTC),
            run_id=run_id,
        )

    def test_upsert_same_contract_100_times_is_idempotent(self, tmp_path: Path) -> None:
        c = self._contract(
            expiry=date(2025, 5, 29),
            strike=24000,
            side=InstrumentType.CE,
            snapshot=date(2025, 4, 15),
        )
        with InstrumentStore.open(tmp_path / "db") as s:
            for _ in range(100):
                s.upsert_contracts([c])
            assert s.count_contracts() == 1
            result = s.list_contracts(underlying_symbol="NIFTY", expiry_date=date(2025, 5, 29))
            assert len(result) == 1
            assert result[0].strike_price == Decimal("24000.0000")

    def test_list_contracts_by_underlying_and_expiry(self, tmp_path: Path) -> None:
        contracts = [
            self._contract(
                underlying="NIFTY",
                expiry=date(2025, 5, 29),
                strike=s,
                side=InstrumentType.CE,
                snapshot=date(2025, 4, 15),
            )
            for s in (23900, 24000, 24100)
        ] + [
            self._contract(
                underlying="BANKNIFTY",
                expiry=date(2025, 5, 29),
                strike=52000,
                side=InstrumentType.PE,
                snapshot=date(2025, 4, 15),
            ),
        ]
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_contracts(contracts)
            nifty = s.list_contracts(
                underlying_symbol="NIFTY",
                expiry_date=date(2025, 5, 29),
            )
            banknifty = s.list_contracts(
                underlying_symbol="BANKNIFTY",
                expiry_date=date(2025, 5, 29),
            )
        assert len(nifty) == 3
        assert len(banknifty) == 1
        assert all(c.instrument_type == InstrumentType.CE for c in nifty)

    def test_list_contracts_all_returns_everything(self, tmp_path: Path) -> None:
        with InstrumentStore.open(tmp_path / "db") as s:
            contracts = [
                self._contract(
                    expiry=date(2025, 5, 29),
                    strike=s,
                    side=InstrumentType.CE,
                    snapshot=date(2025, 4, 15),
                )
                for s in (24000, 24050, 24100)
            ]
            s.upsert_contracts(contracts)
            assert len(s.list_contracts()) == 3


class TestIdempotenceStress:
    """DEC-P0-011 (PR #33): rewritten from FNO chains → 4 Track 1 indices."""

    def test_10x_upsert_same_batch_is_stable(self, tmp_path: Path) -> None:
        """Pull the same batch 10 times \u2192 final state is a pure function.

        Audit-fix (DEC-P0-014, PR #36 Agent 3): the prior PR #33 rewrite
        still used FINNIFTY + ``_make_chain()`` FNO chains while claiming
        "4 Track 1 indices" -- a HIGH false-OK. Body now matches docstring.
        """
        instruments = [
            _idx("NIFTY"),
            _idx("BANKNIFTY"),
            _idx("SENSEX", exchange=Exchange.BSE),
            _idx("INDIAVIX"),
        ]
        with InstrumentStore.open(tmp_path / "db") as s:
            for _ in range(10):
                s.upsert_many(
                    instruments,
                    snapshot_date=date(2025, 4, 15),
                    run_id="r",
                    csv_source_sha256=_csv_hash(),
                )
            report = s.verify_integrity()
            in_scope_count = s.count(in_scope_only=True)
            total_count = s.count(in_scope_only=False)

        # All 4 are in scope (Track 1). No out-of-scope rows should exist.
        assert in_scope_count == 4
        assert total_count == 4
        assert report.is_clean is True
        assert report.raw_hash_mismatches == 0
        assert report.pk_duplicates == 0
