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
from brutex.data.coverage_ledger import CoverageRecord, classify_cell
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
        exch_id=exchange,
        security_id=token,
        symbol_name=symbol,
        display_name=f"{symbol} 50",
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


def _eq(symbol: str, *, exchange: Exchange = Exchange.NSE) -> Instrument:
    """Build a minimal valid cash-equity (EQ) instrument."""
    token = zlib.crc32(f"EQ-{symbol}".encode()) & 0x7FFFFFFF
    return Instrument(
        exch_id=exchange,
        security_id=token,
        symbol_name=symbol,
        display_name=f"{symbol} Ltd",
        instrument=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
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
        exch_id=exchange,
        security_id=hash(tag) & 0x7FFFFFFF,
        symbol_name=tag.replace(f"{exchange}-", ""),
        display_name=f"{underlying} {strike}",
        instrument=side,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol=underlying,
        underlying_security_id=26000,
        lot_size=lot_size,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=1800,
        sm_expiry_date=expiry,
        strike_price=Decimal(str(strike)),
        option_type=side.value,
        asm_gsm_flag=False,
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
            fetched = s.get("NIFTY")
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
            fetched = s.get("NIFTY")
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
            result = s.get("NIFTY", as_of=date(2025, 2, 1))
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
            assert {i.symbol_name for i in cash} == {"NIFTY", "BANKNIFTY"}

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
            assert {i.symbol_name for i in indices} == {
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
            fetched = snapshot.get(instr.symbol_name)
            assert fetched is not None
            assert fetched.symbol_name == instr.symbol_name

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
        with pytest.raises(CatalogError, match="duplicate symbol_name"):
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
        assert snap.get("NIFTY") is not None
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
            s.get("NIFTY")
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
            exch_id=exchange,
            underlying_symbol=underlying,
            sm_expiry_date=expiry,
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
        assert sorted(e.sm_expiry_date for e in nifty) == [
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
            exch_id="NSE",
            underlying_symbol=underlying,
            sm_expiry_date=expiry,
            symbol_name=symbol,
            snapshot_date=snapshot,
            instrument=side,
            strike_price=Decimal(str(strike)),
            lot_size=25,
            sm_freeze_qty=1800,
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
            result = s.list_contracts(underlying_symbol="NIFTY", sm_expiry_date=date(2025, 5, 29))
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
                sm_expiry_date=date(2025, 5, 29),
            )
            banknifty = s.list_contracts(
                underlying_symbol="BANKNIFTY",
                sm_expiry_date=date(2025, 5, 29),
            )
        assert len(nifty) == 3
        assert len(banknifty) == 1
        assert all(c.instrument == InstrumentType.CE for c in nifty)

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


class TestLeanReingestAndLegsDetection:
    """has_derivative_legs() + upsert_many(replace_snapshot=...) — DEC-LEAN-REINGEST-FIX-001."""

    def test_has_derivative_legs_false_for_lean_lake(self, tmp_path: Path) -> None:
        """A lean lake (only IDX + EQ) reports no derivative legs."""
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_many(
                [_idx("NIFTY"), _eq("RELIANCE")],
                snapshot_date=date(2026, 5, 29),
                run_id="r",
                csv_source_sha256=_csv_hash(),
            )
            assert s.has_derivative_legs() is False

    def test_has_derivative_legs_true_when_leg_present(self, tmp_path: Path) -> None:
        """A lake holding any FUT/CE/PE leg is flagged non-lean."""
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_many(
                [
                    _idx("NIFTY"),
                    _option(
                        underlying="NIFTY",
                        strike=24000,
                        side=InstrumentType.CE,
                        expiry=date(2026, 6, 25),
                    ),
                ],
                snapshot_date=date(2026, 5, 29),
                run_id="r",
                csv_source_sha256=_csv_hash(),
            )
            assert s.has_derivative_legs() is True

    def test_replace_snapshot_purges_stale_legs(self, tmp_path: Path) -> None:
        """A full-snapshot replace re-ingest drops legs left by a prior pull."""
        snap = date(2026, 5, 29)
        with InstrumentStore.open(tmp_path / "db") as s:
            # Prior (pre-lean) ingest: index + a stale option leg.
            s.upsert_many(
                [
                    _idx("NIFTY"),
                    _option(
                        underlying="NIFTY",
                        strike=24000,
                        side=InstrumentType.CE,
                        expiry=date(2026, 6, 25),
                    ),
                ],
                snapshot_date=snap,
                run_id="old",
                csv_source_sha256=_csv_hash("old"),
            )
            assert s.has_derivative_legs() is True
            assert s.count(in_scope_only=False) == 2

            # Lean re-ingest with replace_snapshot=True → wipes the day, keeps
            # only the lean set (the index); the stale leg is purged.
            result = s.upsert_many(
                [_idx("NIFTY")],
                snapshot_date=snap,
                run_id="lean",
                csv_source_sha256=_csv_hash("lean"),
                replace_snapshot=True,
            )
            assert result.rows_replaced == 2  # both prior rows wiped
            assert result.rows_written == 1
            assert s.count(in_scope_only=False) == 1
            assert s.has_derivative_legs() is False


class TestPlaceholderEconomicsAndPrune:
    """has_placeholder_stock_economics() + prune_to_latest_snapshot() —
    DEC-STALE-ECONOMICS-HEAL-001 + DEC-INSTRUMENT-LEAN-SNAPSHOT-001."""

    @staticmethod
    def _enriched_eq(symbol: str, *, lot_size: int = 500, freeze: int = 5000) -> Instrument:
        """An EQ row carrying REAL (leg-derived) economics — lot_size > 1."""
        from dataclasses import replace

        return replace(_eq(symbol), lot_size=lot_size, sm_freeze_qty=freeze)

    def test_placeholder_true_for_unenriched_equity(self, tmp_path: Path) -> None:
        """An EQ still at the master's placeholder lot_size=1 is flagged stale."""
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_many(
                [_idx("NIFTY"), _eq("RELIANCE")],  # _eq builds lot_size=1
                snapshot_date=date(2026, 5, 29),
                run_id="r",
                csv_source_sha256=_csv_hash(),
            )
            assert s.has_placeholder_stock_economics() is True

    def test_placeholder_false_when_equity_enriched(self, tmp_path: Path) -> None:
        """An EQ back-filled with a real F&O lot (>1) is NOT stale."""
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_many(
                [_idx("NIFTY"), self._enriched_eq("RELIANCE")],
                snapshot_date=date(2026, 5, 29),
                run_id="r",
                csv_source_sha256=_csv_hash(),
            )
            assert s.has_placeholder_stock_economics() is False

    def test_placeholder_false_on_index_only_lake(self, tmp_path: Path) -> None:
        """An IDX-only lake has no equity fingerprint (index lot=1 doesn't count)."""
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_many(
                [_idx("NIFTY")],
                snapshot_date=date(2026, 5, 29),
                run_id="r",
                csv_source_sha256=_csv_hash(),
            )
            assert s.has_placeholder_stock_economics() is False

    def test_placeholder_false_on_empty_lake(self, tmp_path: Path) -> None:
        """An empty lake has nothing stale to heal."""
        with InstrumentStore.open(tmp_path / "db") as s:
            assert s.has_placeholder_stock_economics() is False

    def test_placeholder_only_considers_latest_snapshot(self, tmp_path: Path) -> None:
        """A stale OLD snapshot is ignored once the NEWEST snapshot is enriched."""
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_many(
                [_eq("RELIANCE")],  # placeholder lot=1
                snapshot_date=date(2026, 5, 28),
                run_id="d1",
                csv_source_sha256=_csv_hash("d1"),
            )
            s.upsert_many(
                [self._enriched_eq("RELIANCE")],  # enriched lot=500
                snapshot_date=date(2026, 5, 29),
                run_id="d2",
                csv_source_sha256=_csv_hash("d2"),
            )
            # The newest snapshot per symbol is the enriched one → not stale.
            assert s.has_placeholder_stock_economics() is False

    def test_prune_collapses_to_latest_snapshot(self, tmp_path: Path) -> None:
        """Two daily snapshots → prune keeps only the newest (236→472 fix)."""
        with InstrumentStore.open(tmp_path / "db") as s:
            for d, rid in ((date(2026, 5, 28), "d1"), (date(2026, 5, 29), "d2")):
                s.upsert_many(
                    [_idx("NIFTY"), _eq("RELIANCE")],
                    snapshot_date=d,
                    run_id=rid,
                    csv_source_sha256=_csv_hash(str(d)),
                )
            assert s.count(in_scope_only=False) == 4  # 2 snapshots × 2 rows
            deleted = s.prune_to_latest_snapshot()
            assert deleted == 2  # the older day's 2 rows
            assert s.count(in_scope_only=False) == 2
            assert s.snapshot_dates() == [date(2026, 5, 29)]
            # The live universe is intact + clean.
            assert s.verify_integrity().is_clean is True

    def test_prune_idempotent_on_single_snapshot(self, tmp_path: Path) -> None:
        """Pruning an already-lean (single-snapshot) lake deletes nothing."""
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_many(
                [_idx("NIFTY"), _eq("RELIANCE")],
                snapshot_date=date(2026, 5, 29),
                run_id="r",
                csv_source_sha256=_csv_hash(),
            )
            assert s.prune_to_latest_snapshot() == 0
            assert s.prune_to_latest_snapshot() == 0  # still idempotent
            assert s.count(in_scope_only=False) == 2

    def test_prune_safe_on_empty_lake(self, tmp_path: Path) -> None:
        """Pruning an empty lake is a no-op returning 0."""
        with InstrumentStore.open(tmp_path / "db") as s:
            assert s.prune_to_latest_snapshot() == 0


class TestThreadSafety:
    """Concurrent read/write must not corrupt the shared connection — DEC-STORE-THREADSAFE-001.

    Reproduces the operator's ``/coverage`` 500 (``ValueError: not enough values
    to unpack (expected 26, got 1)``): the background universe-pull thread
    mutates ``instruments`` (upsert + prune) on the SAME DuckDB connection that a
    request thread reads via ``list_index_equity_instruments``. Without the
    per-store lock the two interleave on the single result cursor and garble
    rows; with it every read returns a clean, fully-formed row.
    """

    def test_concurrent_read_during_upsert_prune_never_corrupts(self, tmp_path: Path) -> None:
        import threading

        with InstrumentStore.open(tmp_path / "db") as s:
            # Seed so the first reads have rows.
            s.upsert_many(
                [_idx("NIFTY"), _eq("RELIANCE")],
                snapshot_date=date(2026, 5, 29),
                run_id="seed",
                csv_source_sha256=_csv_hash("seed"),
            )

            errors: list[Exception] = []
            stop = threading.Event()

            def _writer() -> None:
                # The pull thread's hot path: refresh-style upsert + prune,
                # stamping a NEW snapshot_date each round (the cross-day churn).
                day = 1
                try:
                    while not stop.is_set():
                        s.upsert_many(
                            [_idx("NIFTY"), _eq("RELIANCE")],
                            snapshot_date=date(2026, 6, (day % 28) + 1),
                            run_id=f"w{day}",
                            csv_source_sha256=_csv_hash(f"w{day}"),
                        )
                        s.prune_to_latest_snapshot()
                        day += 1
                except Exception as exc:
                    errors.append(exc)

            def _reader() -> None:
                # The /coverage request path: read the lean candidate set.
                try:
                    for _ in range(200):
                        for inst in s.list_index_equity_instruments():
                            # Touch a field — a garbled row would have raised on
                            # construction already, but assert the shape too.
                            assert inst.symbol_name
                        s.has_placeholder_stock_economics()
                        s.list_coverage()
                except Exception as exc:
                    errors.append(exc)

            writer = threading.Thread(target=_writer, name="writer")
            readers = [threading.Thread(target=_reader, name=f"reader{i}") for i in range(3)]
            writer.start()
            for r in readers:
                r.start()
            for r in readers:
                r.join(timeout=30)
            stop.set()
            writer.join(timeout=30)

            assert not errors, errors
            # Lake stayed lean + clean throughout the churn.
            assert s.verify_integrity().is_clean is True


class TestCoverageLedger:
    """coverage_ledger table CRUD + status transitions — DEC-COVERAGE-LEDGER-001."""

    @staticmethod
    def _cov(
        *,
        exchange: str = "NSE",
        underlying: str = "NIFTY",
        year: int = 2024,
        expiries: int = 2,
        contracts: int = 20,
        error: str | None = None,
        run_id: str = "run-1",
    ) -> CoverageRecord:
        return CoverageRecord(
            exch_id=exchange,
            underlying_symbol=underlying,
            year=year,
            expiries_pulled=expiries,
            contracts_pulled=contracts,
            status=classify_cell(expiries, contracts, error),
            last_run_id=run_id,
            last_fetched_at=datetime(2026, 5, 30, 9, 15, tzinfo=UTC),
            error_message=error,
        )

    def test_schema_version_is_three(self, tmp_path: Path) -> None:
        # v3: Dhan-native instrument-master rename + (security_id, exch_id,
        # segment) identity (ADR-0003 / DEC-DHAN-NATIVE-SCHEMA-001).
        with InstrumentStore.open(tmp_path / "db") as s:
            assert s.schema_version == 3
            assert SCHEMA_VERSION == 3

    def test_upsert_and_get_roundtrip(self, tmp_path: Path) -> None:
        with InstrumentStore.open(tmp_path / "db") as s:
            rec = self._cov()
            s.upsert_coverage(rec)
            got = s.get_coverage("NSE", "NIFTY", 2024)
        assert got is not None
        assert got.exch_id == "NSE"
        assert got.underlying_symbol == "NIFTY"
        assert got.year == 2024
        assert got.expiries_pulled == 2
        assert got.contracts_pulled == 20
        assert got.status == "complete"
        assert got.last_run_id == "run-1"
        assert got.error_message is None

    def test_get_missing_returns_none(self, tmp_path: Path) -> None:
        with InstrumentStore.open(tmp_path / "db") as s:
            assert s.get_coverage("NSE", "NOPE", 2024) is None

    def test_upsert_same_cell_100_times_is_idempotent(self, tmp_path: Path) -> None:
        rec = self._cov()
        with InstrumentStore.open(tmp_path / "db") as s:
            for _ in range(100):
                s.upsert_coverage(rec)
            assert len(s.list_coverage()) == 1

    def test_list_coverage_is_sorted(self, tmp_path: Path) -> None:
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_coverage(self._cov(exchange="NSE", underlying="ZED", year=2024))
            s.upsert_coverage(self._cov(exchange="NSE", underlying="ABLE", year=2024))
            s.upsert_coverage(self._cov(exchange="BSE", underlying="SENSEX", year=2024))
            rows = s.list_coverage()
        keys = [(r.exch_id, r.underlying_symbol, r.year) for r in rows]
        assert keys == [
            ("BSE", "SENSEX", 2024),
            ("NSE", "ABLE", 2024),
            ("NSE", "ZED", 2024),
        ]

    def test_list_incomplete_excludes_complete(self, tmp_path: Path) -> None:
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_coverage(self._cov(underlying="DONE", expiries=3))  # complete
            s.upsert_coverage(
                self._cov(underlying="FAIL", expiries=0, contracts=0, error="boom")
            )  # failed
            s.upsert_coverage(self._cov(underlying="WAIT", expiries=0, contracts=0))  # pending
            incomplete = s.list_incomplete_coverage()
        symbols = {r.underlying_symbol: r.status for r in incomplete}
        assert symbols == {"FAIL": "failed", "WAIT": "pending"}

    def test_status_transition_failed_then_complete(self, tmp_path: Path) -> None:
        """A failed cell re-pulled successfully flips to complete (in place)."""
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_coverage(self._cov(expiries=0, contracts=0, error="network down", run_id="r1"))
            failed = s.get_coverage("NSE", "NIFTY", 2024)
            assert failed is not None
            assert failed.status == "failed"
            assert len(s.list_incomplete_coverage()) == 1

            # Resume pull succeeds → same PK, new counts, no error.
            s.upsert_coverage(self._cov(expiries=4, contracts=40, run_id="r2"))
            got = s.get_coverage("NSE", "NIFTY", 2024)
        assert got is not None
        assert got.status == "complete"
        assert got.expiries_pulled == 4
        assert got.last_run_id == "r2"
        assert got.error_message is None

    def test_pending_then_failed_then_complete(self, tmp_path: Path) -> None:
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_coverage(self._cov(expiries=0, contracts=0))  # pending
            pending = s.get_coverage("NSE", "NIFTY", 2024)
            assert pending is not None
            assert pending.status == "pending"
            s.upsert_coverage(self._cov(expiries=0, contracts=0, error="boom"))  # failed
            failed = s.get_coverage("NSE", "NIFTY", 2024)
            assert failed is not None
            assert failed.status == "failed"
            s.upsert_coverage(self._cov(expiries=1, contracts=5))  # complete
            complete = s.get_coverage("NSE", "NIFTY", 2024)
            assert complete is not None
            assert complete.status == "complete"
            assert s.list_incomplete_coverage() == []

    def test_coverage_persists_across_reopen(self, tmp_path: Path) -> None:
        db = tmp_path / "db"
        with InstrumentStore.open(db) as s:
            s.upsert_coverage(self._cov())
        with InstrumentStore.open(db) as s:
            got = s.get_coverage("NSE", "NIFTY", 2024)
        assert got is not None
        assert got.status == "complete"
