"""window_ledger_store CANONICAL-ENCODER pin (the ADR-0020 G10 resolution).

DEC-RUST-RUNID-STORES-001, resolving the G10 byte-parity escalation of
DEC-LAKE-RUST-LEDGERSTORE-SPIKE-001 (MIGRATION.md #1917) by its priced
**option (iii)** — GOLDEN RE-BASELINE: the #1917 spike PROVED byte-identity
with the pyarrow-25 production bytes unreachable by construction (the
divergence is encoder identity — ``created_by``, the footer format-version
marker, the ``ARROW:schema`` flatbuffer, page-header thrift — none an exposed
knob; the transcript + the priced DEC-LAKE-EXACT-PIN-001 escalation live at
``docs/lake/g10_window_ledger_byte_parity/``). The lane therefore DELETED the
pyarrow writer, made the ``brutex_identity`` kernel's parquet-rs encoder the
CANONICAL on-disk byte form (greenfield-sanctioned — no stored ledger
predates it), and kept the #1917 cross-encoder ROW FIXED POINT as the
correctness law. This test pins that resolution:

* the CANONICAL write (through the REAL production ``save_window_ledger``)
  is byte-DETERMINISTIC — same ledger, same bytes;
* the canonical file reads back to IDENTICAL row tuples through the
  production reader AND pyarrow AND DuckDB ``read_parquet`` — the
  cross-encoder ROW fixed point (readers are schema-name-based, never
  byte-based; the /db + /coverage DuckDB fast paths keep working);
* a LEGACY pyarrow-written file (the retired writer's schema + its snappy
  default) still reads through the production reader to the SAME row
  tuples — the pre-conversion history is never orphaned;
* the canonical bytes genuinely differ from the retired pyarrow encoder's in
  the DOCUMENTED encoder-identity class (``created_by``) — carrying the
  #1917 finding forward so a future "bytes became equal" flip re-opens the
  decision loudly instead of rotting silently.

Whitebox: drives the private ``_read_ledger_file_rows`` seam (the surface
under probe — the DEC-P0-017 convention).
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path

import brutex_identity
import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from brutex.data.fetch_coverage import WindowKey
from brutex.data.window_ledger import WindowLedger
from brutex.data.window_ledger_store import (
    _read_ledger_file_rows,
    ledger_path,
    render_ledger_parquet,
    save_window_ledger,
)

#: The canonical 16-column schema order — the kernel's ``LEDGER_COLUMNS`` as a
#: tuple (the ONE schema authority; the store's former ``_COLUMNS`` re-wrap is
#: gone — DEC-RUST-RUNID-STORES-001).
_COLUMNS: tuple[str, ...] = tuple(brutex_identity.LEDGER_COLUMNS)

_NOW = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)


def _key(security_id: str) -> WindowKey:
    return WindowKey(
        market="dhan",
        security_id=security_id,
        interval="1minute",
        start=date(2021, 1, 1),
        end=date(2021, 8, 1),
    )


def _corpus_ledger() -> WindowLedger:
    """The spike's case (c) — mixed record+plan, exercising the None arms."""
    ledger = WindowLedger()
    ledger.record(_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
    ledger.plan(_key("SENSEX"), now=_NOW)  # last_attempt_at None arm
    ledger.record(
        _key("BANKNIFTY"), raw_bars=0, error_type="DhanRateLimitError", bars_appended=0, now=_NOW
    )
    return ledger


def _production_parquet(root: Path) -> Path:
    """Write the corpus ledger through the REAL production writer; return the file."""
    save_window_ledger(_corpus_ledger(), root)
    return ledger_path(root)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _retired_pyarrow_parquet(prod: Path, out: Path) -> Path:
    """Re-render the canonical file's rows through the RETIRED pyarrow writer shape.

    The exact fixed-schema ``pa.table`` + all-defaults ``pq.write_table`` the
    pre-conversion ``save_window_ledger`` used — fabricated here (the
    production module no longer carries it) both as the LEGACY-file fixture
    and as the encoder-identity counterparty for the divergence pin.
    """
    rows = _read_ledger_file_rows(prod)
    schema = pa.schema(
        [(name, pa.string() if name in _STRING_COLUMNS else pa.int64()) for name in _COLUMNS]
    )
    table = pa.table(
        {name: [row[i] for row in rows] for i, name in enumerate(_COLUMNS)},
        schema=schema,
    )
    pq.write_table(table, out)
    return out


#: The retired writer's string columns (its fixed schema — every other column int64).
_STRING_COLUMNS = frozenset(
    {
        "market",
        "security_id",
        "interval",
        "window_start",
        "window_end",
        "status",
        "last_error",
        "first_seen_at",
        "last_attempt_at",
    }
)


def test_canonical_ledger_write_is_byte_deterministic(tmp_path: Path) -> None:
    """Same ledger saved twice → identical bytes (the canonical-encoder pin)."""
    sha_a = _sha(_production_parquet(tmp_path / "a"))
    sha_b = _sha(_production_parquet(tmp_path / "b"))
    print(f"canonical sha256: {sha_a[:16]} / {sha_b[:16]}")
    assert sha_a == sha_b
    # The pure render is deterministic too (the same bytes law one level down).
    rows = _read_ledger_file_rows(ledger_path(tmp_path / "a"))
    assert render_ledger_parquet(rows) == render_ledger_parquet(rows)


def test_cross_encoder_row_fixed_point(tmp_path: Path) -> None:
    """The option-(iii) correctness law: one byte form, every reader agrees.

    The canonical file reads back to IDENTICAL row tuples through the
    production reader, through pyarrow, and through DuckDB ``read_parquet``
    (the /db + /coverage fast paths) — loaders are schema-name-based, so the
    encoder re-baseline is invisible to every consumer.
    """
    prod = _production_parquet(tmp_path)
    prod_rows = _read_ledger_file_rows(prod)
    assert len(prod_rows) == 3  # anti-vacuous: the corpus genuinely has rows

    # pyarrow cross-read.
    table = pq.read_table(prod)
    pyarrow_rows = [
        tuple(table.column(name)[i].as_py() for name in _COLUMNS) for i in range(table.num_rows)
    ]
    assert pyarrow_rows == prod_rows

    # DuckDB cross-read (column order pinned by explicit projection).
    con = duckdb.connect()
    try:
        duck_rows = con.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM read_parquet(?)",  # noqa: S608 — trusted module-constant column names; the path is a bound parameter
            [str(prod)],
        ).fetchall()
    finally:
        con.close()
    assert [tuple(r) for r in duck_rows] == prod_rows


def test_legacy_pyarrow_file_reads_to_identical_rows(tmp_path: Path) -> None:
    """A retired-writer (pyarrow, snappy) file is NOT orphaned by the re-baseline.

    The production reader yields row tuples IDENTICAL to the canonical file's —
    the #1917 cross-encoder row fixed point, now serving as the legacy
    back-compat law (pre-conversion ledgers keep loading bit-for-bit).
    """
    prod = _production_parquet(tmp_path)
    legacy = _retired_pyarrow_parquet(prod, tmp_path / "legacy_pyarrow.parquet")
    assert _read_ledger_file_rows(legacy) == _read_ledger_file_rows(prod)


def test_canonical_bytes_diverge_from_retired_encoder_in_documented_class(
    tmp_path: Path,
) -> None:
    """The #1917 finding carried forward: encoder identity differs, rows do not.

    If a future parquet-rs/pyarrow bump ever makes the bytes EQUAL, this pin
    fails loudly — that would materially re-open the G10 decision (re-run the
    spike probe, update the escalation record). Never a flake: both writes are
    deterministic.
    """
    prod = _production_parquet(tmp_path)
    legacy = _retired_pyarrow_parquet(prod, tmp_path / "legacy_pyarrow.parquet")
    assert _sha(prod) != _sha(legacy)
    prod_md = pq.read_metadata(prod)
    legacy_md = pq.read_metadata(legacy)
    # created_by IS the encoder identity: parquet-rs vs parquet-cpp-arrow.
    assert prod_md.created_by != legacy_md.created_by
    assert legacy_md.created_by.startswith("parquet-cpp-arrow")
    assert "parquet-rs" in prod_md.created_by
    # Both are still real parquet files (PAR1 magic head + tail).
    for path in (prod, legacy):
        blob = path.read_bytes()
        assert blob[:4] == b"PAR1" and blob[-4:] == b"PAR1"
