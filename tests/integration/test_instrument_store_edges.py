"""Edge-case + defensive-path coverage for :mod:`brutex.data.instrument_store`.

The happy paths of :class:`~brutex.data.instrument_store.InstrumentStore`
(open / upsert / get / count / load_snapshot / coverage-ledger CRUD over a
real DuckDB store) are exercised by
:mod:`tests.integration.test_instrument_store`. This companion module drives
the remaining guard / error / defensive / Track-2-shaped branches that the
end-to-end suite never reaches, lifting ``instrument_store.py`` to **100%
line + branch** so it can be un-omitted from the coverage gate and locked by
a strict per-module gate (DEC-COVERAGE-INSTRUMENT-STORE-001, mirroring
DEC-COVERAGE-CATALOG-001 / DEC-COVERAGE-DRIVER-001).

Covered gaps (measured ``--cov`` miss list before this file, running the
dedicated integration suite alone):

* ``InstrumentRun.__post_init__`` validation — empty ``run_id`` (312),
  bad-length ``csv_source_sha256`` (314), naive ``fetched_at`` (319).
* ``InstrumentSnapshot`` read surface never touched by IDX-only fixtures:
  the ``underlying_symbol is not None`` ``by_underlying`` branch (552), the
  ``snapshot_date`` property (577), ``__contains__`` (585), ``__iter__``
  (593), ``meta()`` (613), plus ``underlyings()`` / ``by_instrument_type``.
* The whole ``_LockedReadConn`` whitebox read view reached via the
  ``connection`` property (718-784, 827): ``execute`` → ``description`` →
  ``fetchone``/``fetchall`` → release, the re-execute lock-drop, the
  execute-raises rollback, the description-only ``close()`` / ``with`` exit.
* ``InstrumentStore.open`` missing-``duckdb`` ``ImportError`` guard (840-841).
* ``upsert_many`` with a custom ``in_scope_of`` (the ``934->939`` false arm),
  the empty-``instruments`` no-row path (969->1024, 1025-1027 fast-forward),
  and the ``BEGIN → … → ROLLBACK → raise`` transactional-rollback path.
* The Phase-B/C writers + readers that are Track-2-shaped and thus
  ``@skip``-ped in the main suite: ``upsert_expiries`` (1084-1118),
  ``upsert_contracts`` (1129-1162), ``list_expiries`` (1255-1298),
  ``list_contracts`` (1324-1365), ``count_expiries`` (1385-1401),
  ``count_contracts`` (1405-1415) — exercised here against a real store with
  light synthetic option/future rows (vendor-neutral; no live vendor calls).
* ``list_in_scope`` ``instrument_type`` filter + ``as_of`` param (1479-1504),
  ``fno_eligible_underlyings`` both UNION arms + ``as_of`` (1548-1577),
  ``list_futures`` (1712), ``list_options`` CE+PE merge (1730-1740),
  ``universe_as_of`` (1744), ``latest_snapshot_date`` empty/non-empty
  (1758-1760), ``count`` ``as_of`` + ``segment`` clauses (1779-1783).
* ``verify_integrity`` raw-hash-mismatch counting (1885) + ``assert_clean``
  raise-on-dirty (1910), reached by tampering a row via direct SQL.
* The ``_build_insert_sql`` reference helper (1994-1996).

These tests need only ``duckdb`` (for a real on-disk store) plus light fakes
/ ``sys.modules`` monkeypatching; the defensive ``open()`` ImportError path
is simulated by hiding ``duckdb`` from ``sys.modules``.
"""

# Whitebox edge file (DEC-P0-017 convention, mirroring test_execution_simulator
# / test_config_runtime): it deliberately imports the module-private reference
# helpers ``_INSTR_COLUMNS`` + ``_build_insert_sql`` to pin them at 100%, so the
# project-wide ``reportPrivateUsage = "warning"`` is silenced file-wide here.
# pyright: reportPrivateUsage=none
from __future__ import annotations

import hashlib
import sys
import zlib
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.coverage_ledger import CoverageRecord
from brutex.data.errors import CatalogError
from brutex.data.instrument_store import (
    _INSTR_COLUMNS,
    SCHEMA_VERSION,
    ContractRecord,
    ExpiryRecord,
    InstrumentRun,
    InstrumentSnapshot,
    InstrumentStore,
    _build_insert_sql,
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
# Helpers (vendor-neutral synthetic fixtures — no live vendor calls)
# ────────────────────────────────────────────────────────────────────────


def _csv_hash(tag: str = "fixture") -> str:
    return hashlib.sha256(tag.encode()).hexdigest()


def _idx(symbol: str, *, exchange: Exchange = Exchange.NSE) -> Instrument:
    """Build a minimal valid IDX instrument (NULL underlying)."""
    return Instrument(
        exch_id=exchange,
        security_id=zlib.crc32(symbol.encode()) & 0x7FFFFFFF,
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


def _eq(symbol: str, *, exchange: Exchange = Exchange.NSE, lot_size: int = 500) -> Instrument:
    """Build a minimal valid cash-equity (EQ) instrument."""
    return Instrument(
        exch_id=exchange,
        security_id=zlib.crc32(f"EQ-{symbol}".encode()) & 0x7FFFFFFF,
        symbol_name=symbol,
        display_name=f"{symbol} Ltd",
        instrument=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=lot_size,
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
    """Build a minimal valid option leg (CE/PE) on *underlying*."""
    tag = f"{exchange}-{underlying}-{expiry.strftime('%d%b%y')}-{strike}-{side.value}"
    return Instrument(
        exch_id=exchange,
        security_id=zlib.crc32(tag.encode()) & 0x7FFFFFFF,
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
        asm_gsm_flag=False,
    )


def _future(
    *,
    underlying: str,
    expiry: date,
    exchange: Exchange = Exchange.NSE,
    lot_size: int = 25,
) -> Instrument:
    """Build a minimal valid futures leg (FUT) on *underlying*."""
    tag = f"{exchange}-{underlying}-{expiry.strftime('%d%b%y')}-FUT"
    return Instrument(
        exch_id=exchange,
        security_id=zlib.crc32(tag.encode()) & 0x7FFFFFFF,
        symbol_name=tag.replace(f"{exchange}-", ""),
        display_name=f"{underlying} FUT",
        instrument=InstrumentType.FUT,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol=underlying,
        underlying_security_id=26000,
        lot_size=lot_size,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=1800,
        sm_expiry_date=expiry,
        strike_price=None,
        asm_gsm_flag=False,
    )


# ────────────────────────────────────────────────────────────────────────
# InstrumentRun.__post_init__ validation (312, 314, 319)
# ────────────────────────────────────────────────────────────────────────


def _valid_run(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "run_id": "r1",
        "snapshot_date": date(2025, 4, 15),
        "fetched_at": datetime(2025, 4, 15, 9, 15, tzinfo=UTC),
        "csv_source_sha256": _csv_hash("seed"),
        "total_raw_rows": 1,
        "integrity_failures": 0,
        "in_scope_count": 1,
        "out_of_scope_count": 0,
        "index_count": 1,
        "futures_count": 0,
        "options_count": 0,
        "bse_count": 0,
        "nse_count": 1,
        "code_commit_sha": "abc123",
    }
    base.update(overrides)
    return base


class TestInstrumentRunValidation:
    def test_empty_run_id_rejected(self) -> None:
        """A blank ``run_id`` cannot anchor an audit row (312)."""
        with pytest.raises(ValueError, match="run_id must be non-empty"):
            InstrumentRun(**_valid_run(run_id=""))  # type: ignore[arg-type]

    def test_bad_length_csv_sha_rejected(self) -> None:
        """``csv_source_sha256`` must be a 64-char hex digest (314)."""
        with pytest.raises(ValueError, match="64-char sha256 hex"):
            InstrumentRun(**_valid_run(csv_source_sha256="deadbeef"))  # type: ignore[arg-type]

    def test_naive_fetched_at_rejected(self) -> None:
        """A naive ``fetched_at`` trips the tz-aware guard (319)."""
        with pytest.raises(ValueError, match="timezone-aware"):
            InstrumentRun(
                **_valid_run(fetched_at=datetime(2025, 4, 15, 9, 15)),  # type: ignore[arg-type]
            )

    def test_valid_run_constructs(self) -> None:
        """The happy construction is the control for the three guards above."""
        run = InstrumentRun(**_valid_run())  # type: ignore[arg-type]
        assert run.run_id == "r1"
        assert run.schema_version >= 2


# ────────────────────────────────────────────────────────────────────────
# InstrumentSnapshot read surface untouched by IDX-only fixtures
# (552, 577, 585, 593, 613, underlyings, by_instrument_type)
# ────────────────────────────────────────────────────────────────────────


class TestSnapshotReadSurface:
    def test_by_underlying_indexes_derivative_legs(self) -> None:
        """A snapshot built with option legs pre-indexes ``by_underlying`` (552)."""
        ce = _option(
            underlying="NIFTY", strike=24000, side=InstrumentType.CE, expiry=date(2025, 5, 29)
        )
        pe = _option(
            underlying="NIFTY", strike=24000, side=InstrumentType.PE, expiry=date(2025, 5, 29)
        )
        idx = _idx("NIFTY")  # NULL underlying — exercises the False arm of 551->552
        snap = InstrumentSnapshot([ce, pe, idx], snapshot_date=date(2025, 5, 29))

        legs = snap.by_underlying("NIFTY")
        assert {i.symbol_name for i in legs} == {ce.symbol_name, pe.symbol_name}
        assert snap.underlyings() == ("NIFTY",)
        # IDX (no underlying) is absent from by_underlying but present overall.
        assert idx.symbol_name in {i.symbol_name for i in snap}

    def test_snapshot_date_property_echoes_constructor_arg(self) -> None:
        """``snapshot_date`` returns the as-of the snapshot was built for (577)."""
        snap = InstrumentSnapshot([_idx("NIFTY")], snapshot_date=date(2025, 5, 29))
        assert snap.snapshot_date == date(2025, 5, 29)
        # And ``None`` flows through unchanged for "latest of everything".
        assert InstrumentSnapshot([_idx("NIFTY")]).snapshot_date is None

    def test_contains_iter_len_and_symbols(self) -> None:
        """Membership / iteration / sizing surface (585, 593) over a real set."""
        a, b = _idx("NIFTY"), _idx("BANKNIFTY")
        snap = InstrumentSnapshot([a, b])
        assert a.symbol_name in snap  # __contains__ (585)
        assert "NOPE" not in snap
        assert len(snap) == 2
        assert {i.symbol_name for i in snap} == {
            a.symbol_name,
            b.symbol_name,
        }  # __iter__ (593)
        assert set(snap.symbols()) == {a.symbol_name, b.symbol_name}

    def test_meta_lookup_present_and_missing(self) -> None:
        """``meta()`` returns the InstrumentMeta for a known symbol, ``None`` else (613)."""
        idx = _idx("NIFTY")
        snap = InstrumentSnapshot([idx])
        meta = snap.meta(idx.symbol_name)
        assert meta is not None
        assert meta.symbol == idx.symbol_name
        assert meta.lot_size == 1
        assert snap.meta("NOPE") is None

    def test_by_instrument_type_and_segment(self) -> None:
        """Pre-indexed type/segment views return the matching rows or empty tuple."""
        idx = _idx("NIFTY")
        ce = _option(
            underlying="NIFTY", strike=24000, side=InstrumentType.CE, expiry=date(2025, 5, 29)
        )
        snap = InstrumentSnapshot([idx, ce])
        assert snap.by_instrument_type(InstrumentType.IDX) == (idx,)
        assert snap.by_instrument_type(InstrumentType.CE) == (ce,)
        assert snap.by_instrument_type(InstrumentType.PE) == ()
        assert snap.by_segment(Segment.FNO) == (ce,)


# ────────────────────────────────────────────────────────────────────────
# _build_insert_sql reference helper (1994-1996)
# ────────────────────────────────────────────────────────────────────────


class TestBuildInsertSql:
    def test_insert_sql_matches_column_tuple(self) -> None:
        """The reference INSERT names every ``_INSTR_COLUMNS`` column with one ``?`` each."""
        sql = _build_insert_sql()
        assert sql.startswith("INSERT INTO instruments (")
        for col in _INSTR_COLUMNS:
            assert col in sql
        # One placeholder per column.
        assert sql.count("?") == len(_INSTR_COLUMNS)


# ────────────────────────────────────────────────────────────────────────
# _LockedReadConn — the whitebox `connection` read view (718-784, 827)
# ────────────────────────────────────────────────────────────────────────


class TestLockedReadConnection:
    """The lock-serialized read view the ``/db`` viewer consumes.

    Drives every method of :class:`_LockedReadConn` plus the
    re-execute lock-drop, the execute-raises rollback, and the
    description-only ``close()`` / ``with``-exit lock release.
    """

    def test_execute_fetchone_roundtrip(self, tmp_path: Path) -> None:
        """``connection.execute(...).fetchone()`` returns a row and frees the lock (827, 745, 752-757)."""
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_many(
                [_idx("NIFTY")],
                snapshot_date=date(2025, 4, 15),
                run_id="r",
                csv_source_sha256=_csv_hash(),
            )
            conn = s.connection
            row = conn.execute("SELECT COUNT(*) FROM instruments").fetchone()
            assert row[0] == 1
            # Lock was released by fetchone — a typed call still works.
            assert s.count(in_scope_only=False) == 1

    def test_execute_fetchall_and_description(self, tmp_path: Path) -> None:
        """``description`` is readable while the lock is held, then ``fetchall`` releases it (750, 759-764)."""
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_many(
                [_idx("NIFTY"), _idx("BANKNIFTY")],
                snapshot_date=date(2025, 4, 15),
                run_id="r",
                csv_source_sha256=_csv_hash(),
            )
            conn = s.connection
            cursor = conn.execute("SELECT symbol_name FROM instruments ORDER BY symbol_name")
            cols = [c[0] for c in cursor.description]
            assert cols == ["symbol_name"]
            rows = cursor.fetchall()
            assert [r[0] for r in rows] == ["BANKNIFTY", "NIFTY"]

    def test_execute_with_parameters(self, tmp_path: Path) -> None:
        """The ``parameters is not None`` arm binds placeholders (737-741)."""
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_many(
                [_idx("NIFTY")],
                snapshot_date=date(2025, 4, 15),
                run_id="r",
                csv_source_sha256=_csv_hash(),
            )
            conn = s.connection
            row = conn.execute(
                "SELECT COUNT(*) FROM instruments WHERE symbol_name = ?",
                ["NIFTY"],
            ).fetchone()
            assert row[0] == 1

    def test_reexecute_without_fetch_drops_prior_lock(self, tmp_path: Path) -> None:
        """A second ``execute`` on the same view releases the first hold first (733, 768-770)."""
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_many(
                [_idx("NIFTY")],
                snapshot_date=date(2025, 4, 15),
                run_id="r",
                csv_source_sha256=_csv_hash(),
            )
            conn = s.connection
            # First execute acquires the lock but we never fetch.
            conn.execute("SELECT 1")
            # Re-execute must drop the stale hold, not deadlock or over-acquire.
            row = conn.execute("SELECT 2").fetchone()
            assert row[0] == 2
            # The store lock is now free — a typed call proceeds.
            assert s.count(in_scope_only=False) == 1

    def test_execute_failure_releases_lock(self, tmp_path: Path) -> None:
        """A bad statement releases the lock and propagates the error (742-744)."""
        import duckdb

        with InstrumentStore.open(tmp_path / "db") as s:
            conn = s.connection
            # ``_LockedReadConn.execute`` re-raises inside its ``except
            # BaseException: self._release(); raise`` guard — DuckDB surfaces a
            # CatalogException (subclass of duckdb.Error) for the unknown table.
            with pytest.raises(duckdb.Error):
                conn.execute("SELECT * FROM no_such_table_xyz")
            # Lock was released on the raise — the store is still usable.
            assert s.count(in_scope_only=False) == 0

    def test_close_releases_description_only_read(self, tmp_path: Path) -> None:
        """A description-only reader frees the lock via ``close()`` with no fetch (778, 772-778)."""
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_many(
                [_idx("NIFTY")],
                snapshot_date=date(2025, 4, 15),
                run_id="r",
                csv_source_sha256=_csv_hash(),
            )
            conn = s.connection
            cursor = conn.execute("SELECT * FROM instruments LIMIT 0")
            _ = cursor.description  # read schema only, never fetch
            cursor.close()  # must release the held lock
            # close() is idempotent — a second call does not double-release.
            cursor.close()
            assert s.count(in_scope_only=False) == 1

    def test_context_manager_releases_on_exit(self, tmp_path: Path) -> None:
        """Using the view as a context manager releases the lock on exit (780-784)."""
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_many(
                [_idx("NIFTY")],
                snapshot_date=date(2025, 4, 15),
                run_id="r",
                csv_source_sha256=_csv_hash(),
            )
            with s.connection as conn:
                cursor = conn.execute("SELECT * FROM instruments LIMIT 0")
                assert cursor is conn  # execute returns self (__enter__ returns self too)
                _ = cursor.description
            # __exit__ released the lock — the store proceeds normally.
            assert s.count(in_scope_only=False) == 1


# ────────────────────────────────────────────────────────────────────────
# InstrumentStore.open — missing duckdb (840-841)
# ────────────────────────────────────────────────────────────────────────


class TestOpenWithoutDuckDB:
    def test_open_raises_catalog_error_when_duckdb_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``InstrumentStore.open`` surfaces a friendly ``CatalogError`` if duckdb is missing."""
        # A ``None`` entry in sys.modules makes ``import duckdb`` raise
        # ImportError — simulating the [data] extra not being installed.
        monkeypatch.setitem(sys.modules, "duckdb", None)
        with pytest.raises(CatalogError, match="duckdb"):
            InstrumentStore.open(tmp_path / "instruments.duckdb")


# ────────────────────────────────────────────────────────────────────────
# upsert_many — custom in_scope_of (934->939), empty batch (969->1024,
# 1025-1027 fast-forward), rollback path
# ────────────────────────────────────────────────────────────────────────


class _RollbackConn:
    """Fake DuckDB connection whose ``INSERT INTO instruments`` fails after BEGIN.

    Drives :meth:`InstrumentStore.upsert_many`'s
    ``except → ROLLBACK → raise`` path without corrupting a real database.
    The Arrow ``register`` / ``unregister`` + COUNT/DELETE all succeed; only
    the bulk ``INSERT INTO instruments`` raises, so the ``finally: unregister``
    and the outer ROLLBACK both run.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, sql: str, parameters: object = None) -> _RollbackConn:
        verb = sql.strip().split(None, 1)[0].upper() if sql.strip() else sql
        self.calls.append(verb)
        if "INSERT INTO INSTRUMENTS" in " ".join(sql.upper().split()):
            raise RuntimeError("simulated bulk INSERT failure")

        class _R:
            def fetchone(self) -> tuple[int]:
                return (0,)

        return _R()  # type: ignore[return-value]

    def register(self, name: str, table: object) -> None:
        self.calls.append("REGISTER")

    def unregister(self, name: str) -> None:
        self.calls.append("UNREGISTER")


class TestUpsertManyEdges:
    def test_custom_in_scope_of_callable_is_used(self, tmp_path: Path) -> None:
        """Passing ``in_scope_of`` skips the default-universe import (934->939)."""
        with InstrumentStore.open(tmp_path / "db") as s:
            # A predicate that marks EVERYTHING out of scope — proves the
            # supplied callable (not brutex.universe.is_in_scope) drove the column.
            result = s.upsert_many(
                [_idx("NIFTY"), _idx("BANKNIFTY")],
                snapshot_date=date(2025, 4, 15),
                run_id="r",
                csv_source_sha256=_csv_hash(),
                in_scope_of=lambda _instr: False,
            )
            assert result.rows_written == 2
            # Custom predicate forced in_scope=False on every row.
            assert s.count(in_scope_only=True) == 0
            assert s.count(in_scope_only=False) == 2

    def test_empty_batch_is_a_noop(self, tmp_path: Path) -> None:
        """An empty instrument list commits cleanly with zero rows (969->1024)."""
        with InstrumentStore.open(tmp_path / "db") as s:
            result = s.upsert_many(
                [],
                snapshot_date=date(2025, 4, 15),
                run_id="r",
                csv_source_sha256=_csv_hash(),
            )
            assert result.rows_written == 0
            assert result.rows_replaced == 0
            assert s.count(in_scope_only=False) == 0
            # The store is still healthy after the no-row transaction.
            assert s.verify_integrity().is_clean is True

    def test_failed_bulk_insert_rolls_back_and_reraises(self, tmp_path: Path) -> None:
        """A mid-transaction bulk-INSERT failure rolls back and propagates (1025-1027)."""
        conn = _RollbackConn()
        store = InstrumentStore(conn=conn, path=tmp_path / "db")
        with pytest.raises(RuntimeError, match="simulated bulk INSERT failure"):
            store.upsert_many(
                [_idx("NIFTY")],
                snapshot_date=date(2025, 4, 15),
                run_id="r",
                csv_source_sha256=_csv_hash(),
                in_scope_of=lambda _instr: True,
            )
        # BEGIN opened, Arrow table registered + unregistered, ROLLBACK ran,
        # COMMIT never did.
        assert "BEGIN" in conn.calls
        assert "REGISTER" in conn.calls
        assert "UNREGISTER" in conn.calls
        assert "ROLLBACK" in conn.calls
        assert "COMMIT" not in conn.calls


# ────────────────────────────────────────────────────────────────────────
# Phase B writers/readers — expiries (1084-1118, 1255-1298, 1385-1401)
# ────────────────────────────────────────────────────────────────────────


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


class _RowInsertRollbackConn:
    """Fake connection whose per-row ``INSERT INTO <table>`` raises after BEGIN.

    Drives the ``except → ROLLBACK → raise`` arm of the per-row DELETE+INSERT
    writers (:meth:`InstrumentStore.upsert_expiries` /
    :meth:`upsert_contracts`). ``BEGIN`` + the ``DELETE`` succeed; the first
    ``INSERT`` raises, so the outer ROLLBACK runs and COMMIT never does.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, sql: str, parameters: object = None) -> _RowInsertRollbackConn:
        verb = sql.strip().split(None, 1)[0].upper() if sql.strip() else sql
        self.calls.append(verb)
        if sql.strip().upper().startswith("INSERT"):
            raise RuntimeError("simulated row INSERT failure")
        return self


class TestExpiriesWriterReader:
    """Phase-B expiries persistence — Track-2-shaped, ``@skip``-ped upstream."""

    def test_upsert_idempotent_and_list_and_count(self, tmp_path: Path) -> None:
        """Upsert the same expiry 5× → one row; list + count reflect it (1084-1118, 1385-1401)."""
        exp = _exp(expiry=date(2025, 5, 29), snapshot=date(2025, 4, 15))
        with InstrumentStore.open(tmp_path / "db") as s:
            for _ in range(5):
                written = s.upsert_expiries([exp])
                assert written == 1
            assert s.count_expiries() == 1
            rows = s.list_expiries(underlying_symbol="NIFTY")
            assert len(rows) == 1
            assert rows[0].sm_expiry_date == date(2025, 5, 29)
            assert rows[0].pulled_year == 2025

    def test_list_expiries_unfiltered_and_as_of(self, tmp_path: Path) -> None:
        """No-filter list returns all; ``as_of`` selects the latest snapshot ≤ cutoff (1255-1298)."""
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_expiries(
                [
                    _exp(
                        underlying="NIFTY",
                        expiry=date(2025, 5, 29),
                        snapshot=date(2025, 4, 10),
                        run_id="r1",
                    ),
                    _exp(
                        underlying="BANKNIFTY",
                        expiry=date(2025, 5, 29),
                        snapshot=date(2025, 4, 10),
                        run_id="r1",
                    ),
                ]
            )
            # Re-pull NIFTY on a later snapshot — temporal history.
            s.upsert_expiries(
                [
                    _exp(
                        underlying="NIFTY",
                        expiry=date(2025, 5, 29),
                        snapshot=date(2025, 4, 15),
                        run_id="r2",
                    )
                ]
            )
            # Unfiltered: both underlyings, NIFTY at its latest snapshot.
            all_rows = s.list_expiries()
            assert {r.underlying_symbol for r in all_rows} == {"NIFTY", "BANKNIFTY"}
            # as_of before the NIFTY re-pull → the older NIFTY snapshot wins.
            older = s.list_expiries(underlying_symbol="NIFTY", as_of=date(2025, 4, 11))
            assert len(older) == 1
            assert older[0].snapshot_date == date(2025, 4, 10)

    def test_count_expiries_as_of_filters_by_snapshot(self, tmp_path: Path) -> None:
        """``count_expiries(as_of=...)`` excludes expiries first seen after the cutoff (1393-1400)."""
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_expiries([_exp(expiry=date(2025, 5, 29), snapshot=date(2025, 4, 10))])
            s.upsert_expiries(
                [_exp(underlying="BANKNIFTY", expiry=date(2025, 6, 26), snapshot=date(2025, 5, 1))]
            )
            assert s.count_expiries() == 2
            # Only the first triplet existed on/before 2025-04-15.
            assert s.count_expiries(as_of=date(2025, 4, 15)) == 1

    def test_upsert_expiries_empty_is_zero(self, tmp_path: Path) -> None:
        """An empty expiry batch is a clean no-op returning 0 (loop never runs)."""
        with InstrumentStore.open(tmp_path / "db") as s:
            assert s.upsert_expiries([]) == 0
            assert s.count_expiries() == 0

    def test_upsert_expiries_rolls_back_on_insert_failure(self, tmp_path: Path) -> None:
        """A mid-loop INSERT failure rolls back and re-raises (1115-1117)."""
        conn = _RowInsertRollbackConn()
        store = InstrumentStore(conn=conn, path=tmp_path / "db")
        with pytest.raises(RuntimeError, match="simulated row INSERT failure"):
            store.upsert_expiries([_exp(expiry=date(2025, 5, 29), snapshot=date(2025, 4, 15))])
        assert conn.calls == ["BEGIN", "DELETE", "INSERT", "ROLLBACK"]
        assert "COMMIT" not in conn.calls


# ────────────────────────────────────────────────────────────────────────
# Phase C writers/readers — contracts (1129-1162, 1324-1365, 1405-1415)
# ────────────────────────────────────────────────────────────────────────


def _contract(
    *,
    underlying: str = "NIFTY",
    expiry: date,
    strike: int | None,
    side: InstrumentType,
    snapshot: date,
    run_id: str = "r1",
) -> ContractRecord:
    label = "FUT" if strike is None else f"{strike}-{side.value}"
    symbol = f"NSE-{underlying}-{expiry.strftime('%d%b%y')}-{label}"
    return ContractRecord(
        exch_id="NSE",
        underlying_symbol=underlying,
        sm_expiry_date=expiry,
        symbol_name=symbol,
        snapshot_date=snapshot,
        instrument=side,
        strike_price=Decimal(str(strike)) if strike is not None else None,
        lot_size=25,
        sm_freeze_qty=1800 if strike is not None else None,
        tick_size=Decimal("0.05"),
        fetched_at=datetime(snapshot.year, snapshot.month, snapshot.day, tzinfo=UTC),
        run_id=run_id,
    )


class TestContractsWriterReader:
    """Phase-C contracts persistence — Track-2-shaped, ``@skip``-ped upstream."""

    def test_upsert_idempotent_and_list_and_count(self, tmp_path: Path) -> None:
        """Upsert the same contract 5× → one row; list/count reflect it (1129-1162, 1405-1415)."""
        c = _contract(
            expiry=date(2025, 5, 29),
            strike=24000,
            side=InstrumentType.CE,
            snapshot=date(2025, 4, 15),
        )
        with InstrumentStore.open(tmp_path / "db") as s:
            for _ in range(5):
                assert s.upsert_contracts([c]) == 1
            assert s.count_contracts() == 1
            rows = s.list_contracts(underlying_symbol="NIFTY", sm_expiry_date=date(2025, 5, 29))
            assert len(rows) == 1
            assert rows[0].strike_price == Decimal("24000.0000")
            assert rows[0].instrument == InstrumentType.CE

    def test_fut_leg_roundtrips_with_null_strike(self, tmp_path: Path) -> None:
        """A FUT leg stores/reads ``strike_price=None`` + ``freeze=None`` (1372-1378 None arms)."""
        fut = _contract(
            expiry=date(2025, 5, 29),
            strike=None,
            side=InstrumentType.FUT,
            snapshot=date(2025, 4, 15),
        )
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_contracts([fut])
            rows = s.list_contracts()
            assert len(rows) == 1
            assert rows[0].strike_price is None
            assert rows[0].sm_freeze_qty is None
            assert rows[0].instrument == InstrumentType.FUT

    def test_list_contracts_unfiltered_and_as_of(self, tmp_path: Path) -> None:
        """No-filter list returns all legs; ``as_of`` picks the latest snapshot (1324-1365)."""
        with InstrumentStore.open(tmp_path / "db") as s:
            for strike in (23900, 24000, 24100):
                s.upsert_contracts(
                    [
                        _contract(
                            expiry=date(2025, 5, 29),
                            strike=strike,
                            side=InstrumentType.CE,
                            snapshot=date(2025, 4, 10),
                            run_id="r1",
                        )
                    ]
                )
            # Re-pull one leg on a newer snapshot.
            s.upsert_contracts(
                [
                    _contract(
                        expiry=date(2025, 5, 29),
                        strike=24000,
                        side=InstrumentType.CE,
                        snapshot=date(2025, 4, 15),
                        run_id="r2",
                    )
                ]
            )
            assert len(s.list_contracts()) == 3
            # as_of before the re-pull still resolves every leg (3 legs).
            assert len(s.list_contracts(as_of=date(2025, 4, 11))) == 3

    def test_count_contracts_as_of(self, tmp_path: Path) -> None:
        """``count_contracts(as_of=...)`` filters by snapshot (1411-1414)."""
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_contracts(
                [
                    _contract(
                        expiry=date(2025, 5, 29),
                        strike=24000,
                        side=InstrumentType.CE,
                        snapshot=date(2025, 4, 10),
                    )
                ]
            )
            s.upsert_contracts(
                [
                    _contract(
                        expiry=date(2025, 6, 26),
                        strike=24500,
                        side=InstrumentType.PE,
                        snapshot=date(2025, 5, 1),
                    )
                ]
            )
            assert s.count_contracts() == 2
            assert s.count_contracts(as_of=date(2025, 4, 15)) == 1

    def test_upsert_contracts_empty_is_zero(self, tmp_path: Path) -> None:
        """An empty contract batch is a clean no-op returning 0."""
        with InstrumentStore.open(tmp_path / "db") as s:
            assert s.upsert_contracts([]) == 0
            assert s.count_contracts() == 0

    def test_upsert_contracts_rolls_back_on_insert_failure(self, tmp_path: Path) -> None:
        """A mid-loop INSERT failure rolls back and re-raises (1159-1161)."""
        conn = _RowInsertRollbackConn()
        store = InstrumentStore(conn=conn, path=tmp_path / "db")
        with pytest.raises(RuntimeError, match="simulated row INSERT failure"):
            store.upsert_contracts(
                [
                    _contract(
                        expiry=date(2025, 5, 29),
                        strike=24000,
                        side=InstrumentType.CE,
                        snapshot=date(2025, 4, 15),
                    )
                ]
            )
        assert conn.calls == ["BEGIN", "DELETE", "INSERT", "ROLLBACK"]
        assert "COMMIT" not in conn.calls


# ────────────────────────────────────────────────────────────────────────
# Filtered scans + universe queries
# (1479-1504, 1548-1577, 1712, 1730-1740, 1744, 1758-1760, 1779-1783)
# ────────────────────────────────────────────────────────────────────────


class TestFilteredScans:
    def _seed_fno(self, s: InstrumentStore, *, snapshot: date = date(2025, 4, 15)) -> None:
        """Seed an in-scope index + an EQ + a CE/PE/FUT chain on one underlying.

        Forces ``in_scope=True`` on every row via a custom predicate so the
        ``WHERE in_scope = TRUE`` scans return the synthetic legs (Track-1's
        real universe filter would drop options — this is a store-level test).
        """
        expiry = date(2025, 5, 29)
        s.upsert_many(
            [
                _idx("NIFTY"),
                _eq("RELIANCE"),
                _option(underlying="NIFTY", strike=24000, side=InstrumentType.CE, expiry=expiry),
                _option(underlying="NIFTY", strike=24000, side=InstrumentType.PE, expiry=expiry),
                _future(underlying="NIFTY", expiry=expiry),
            ],
            snapshot_date=snapshot,
            run_id="r",
            csv_source_sha256=_csv_hash(),
            in_scope_of=lambda _instr: True,
        )

    def test_list_in_scope_filters_by_type_and_underlying(self, tmp_path: Path) -> None:
        """Type + underlying filters reach the ``extras`` clause builder (1475-1481)."""
        with InstrumentStore.open(tmp_path / "db") as s:
            self._seed_fno(s)
            ce = s.list_in_scope(instrument_type=InstrumentType.CE, underlying_symbol="NIFTY")
            assert [i.instrument for i in ce] == [InstrumentType.CE]

    def test_list_in_scope_as_of_param(self, tmp_path: Path) -> None:
        """An ``as_of`` cutoff pushes the snapshot param into the query (1503-1504)."""
        with InstrumentStore.open(tmp_path / "db") as s:
            self._seed_fno(s, snapshot=date(2025, 4, 10))
            rows = s.list_in_scope(as_of=date(2025, 4, 15))
            assert len(rows) == 5
            # A cutoff BEFORE the only snapshot returns nothing.
            assert s.list_in_scope(as_of=date(2025, 4, 1)) == []

    def test_list_futures_and_options(self, tmp_path: Path) -> None:
        """``list_futures`` (1712) + ``list_options`` CE+PE merge (1730-1740)."""
        with InstrumentStore.open(tmp_path / "db") as s:
            self._seed_fno(s)
            futs = s.list_futures(underlying="NIFTY")
            assert [i.instrument for i in futs] == [InstrumentType.FUT]
            opts = s.list_options(underlying="NIFTY")
            # CE+PE merged, sorted by vendor_symbol.
            assert {i.instrument for i in opts} == {InstrumentType.CE, InstrumentType.PE}
            assert [i.symbol_name for i in opts] == sorted(i.symbol_name for i in opts)

    def test_universe_as_of_delegates_to_list_in_scope(self, tmp_path: Path) -> None:
        """``universe_as_of`` returns the in-scope set as of a date (1744)."""
        with InstrumentStore.open(tmp_path / "db") as s:
            self._seed_fno(s, snapshot=date(2025, 4, 10))
            rows = s.universe_as_of(date(2025, 4, 30))
            assert len(rows) == 5

    def test_fno_eligible_underlyings_union_of_legs_and_eq(self, tmp_path: Path) -> None:
        """Both UNION arms — leg-named + EQ-row — contribute (1548-1577)."""
        with InstrumentStore.open(tmp_path / "db") as s:
            self._seed_fno(s)
            eligible = s.fno_eligible_underlyings()
            # Arm (a): NIFTY named by its CE/PE/FUT legs.
            assert (Exchange.NSE, "NIFTY") in eligible
            # Arm (b): the persisted NSE EQ row (RELIANCE) by construction.
            assert (Exchange.NSE, "RELIANCE") in eligible

    def test_fno_eligible_underlyings_as_of(self, tmp_path: Path) -> None:
        """The ``as_of`` cutoff binds the snapshot param into BOTH UNION arms (1573-1575)."""
        with InstrumentStore.open(tmp_path / "db") as s:
            self._seed_fno(s, snapshot=date(2025, 4, 10))
            eligible = s.fno_eligible_underlyings(as_of=date(2025, 4, 30))
            assert (Exchange.NSE, "NIFTY") in eligible
            # Before the only snapshot → empty set.
            assert s.fno_eligible_underlyings(as_of=date(2025, 4, 1)) == frozenset()

    def test_count_with_as_of_and_segment(self, tmp_path: Path) -> None:
        """``count`` reaches both the ``as_of`` and ``segment`` clause arms (1778-1783)."""
        with InstrumentStore.open(tmp_path / "db") as s:
            self._seed_fno(s)
            # FNO segment (the 3 legs) within the snapshot window.
            n = s.count(in_scope_only=True, as_of=date(2025, 4, 20), segment=Segment.FNO)
            assert n == 3
            # CASH segment (IDX + EQ).
            assert s.count(in_scope_only=True, segment=Segment.CASH) == 2

    def test_latest_snapshot_date_empty_then_populated(self, tmp_path: Path) -> None:
        """``latest_snapshot_date`` is ``None`` on an empty store, then the max (1758-1760)."""
        with InstrumentStore.open(tmp_path / "db") as s:
            assert s.latest_snapshot_date() is None
            self._seed_fno(s, snapshot=date(2025, 4, 10))
            self._seed_fno(s, snapshot=date(2025, 4, 15))
            assert s.latest_snapshot_date() == date(2025, 4, 15)


# ────────────────────────────────────────────────────────────────────────
# verify_integrity raw-hash mismatch (1885) + assert_clean raise (1910)
# ────────────────────────────────────────────────────────────────────────


class TestIntegrityTampering:
    def test_raw_hash_mismatch_detected_and_assert_clean_raises(self, tmp_path: Path) -> None:
        """A hand-edited row breaks the stored ``raw_hash`` → mismatch + raise (1885, 1910)."""
        with InstrumentStore.open(tmp_path / "db") as s:
            s.upsert_many(
                [_idx("NIFTY")],
                snapshot_date=date(2025, 4, 15),
                run_id="r",
                csv_source_sha256=_csv_hash(),
            )
            # Direct-SQL tamper: change a hashed field WITHOUT recomputing
            # raw_hash, exactly the corruption verify_integrity() guards.
            s.connection.execute(
                "UPDATE instruments SET display_name = 'TAMPERED' WHERE symbol_name = ?",
                ["NIFTY"],
            ).fetchall()
            report = s.verify_integrity()
            assert report.raw_hash_mismatches == 1
            assert report.is_clean is False
            with pytest.raises(CatalogError, match="integrity check FAILED"):
                s.assert_clean()


# ────────────────────────────────────────────────────────────────────────
# Coverage-ledger guards reached via open_instrument_store sugar
# ────────────────────────────────────────────────────────────────────────


class TestCoverageLedgerSugar:
    def test_open_sugar_and_incomplete_listing(self, tmp_path: Path) -> None:
        """``open_instrument_store`` yields a live store; incomplete listing works."""
        rec = CoverageRecord(
            exch_id="NSE",
            underlying_symbol="NIFTY",
            year=2024,
            expiries_pulled=0,
            contracts_pulled=0,
            status="pending",
            last_run_id="r1",
            last_fetched_at=datetime(2026, 5, 30, 9, 15, tzinfo=UTC),
            error_message=None,
        )
        with open_instrument_store(tmp_path / "db") as s:
            s.upsert_coverage(rec)
            incomplete = s.list_incomplete_coverage()
            captured = s
        assert [r.underlying_symbol for r in incomplete] == ["NIFTY"]
        # The sugar closed the store on block exit.
        with pytest.raises(CatalogError, match="closed"):
            captured.list_coverage()


def test_open_migrates_older_schema_version_drops_renamed_tables(tmp_path: Path) -> None:
    """An older on-disk ``SCHEMA_VERSION`` triggers the drop+recreate migration.

    DEC-DHAN-NATIVE-SCHEMA-001: when the persisted ``schema_version`` is below
    the current one, ``InstrumentStore.open`` drops the renamed tables (the
    ``_DDL_DROP_RENAMED`` loop) before recreating them — they are 100%
    re-derivable from the Dhan CSV. A row written under the old shape therefore
    does NOT survive the upgrade. Pins the migration branch deterministically
    (it was only incidentally covered via test-ordering before).
    """
    import duckdb

    db = tmp_path / "instruments.duckdb"
    # 1. Seed a store with one instrument at the current schema version.
    with InstrumentStore.open(db) as store:
        store.upsert_many(
            [_idx("NIFTY")],
            snapshot_date=date(2024, 1, 2),
            run_id="r1",
            csv_source_sha256=_csv_hash(),
        )
        assert "NIFTY" in store.load_snapshot(in_scope_only=False)
    # 2. Force the on-disk schema_version OLDER than the current one.
    conn = duckdb.connect(str(db))
    conn.execute(
        "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
        [str(SCHEMA_VERSION - 1)],
    )
    conn.close()
    # 3. Re-open → the migration drops the renamed tables → the old row is gone.
    with InstrumentStore.open(db) as store:
        assert "NIFTY" not in store.load_snapshot(in_scope_only=False)
