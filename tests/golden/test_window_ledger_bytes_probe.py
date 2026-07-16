"""window_ledger_store byte-divergence PROBE pin (ADR-0020 G10 spike — branch B).

DEC-LAKE-RUST-LEDGERSTORE-SPIKE-001 (MIGRATION.md #1917 — allocator-final).
The G10 spike swept 14 polars-native (Rust encoder) writer arms across a
7-case corpus built through the PRODUCTION ledger writer and found ZERO
sha256 matches against the pyarrow-25 production bytes — confirming ADR-0020
F3's "infeasible by construction" by measurement (the transcript + the priced
DEC-LAKE-EXACT-PIN-001 escalation live at
``docs/lake/g10_window_ledger_byte_parity/``). This test PINS the DOCUMENTED
divergence class so CI carries the finding without flaking, and proves the
ROW-LEVEL fixed point (the module's own contract,
``brutex/data/window_ledger_store.py:16``) HOLDS across encoders:

* the production pyarrow write is byte-DETERMINISTIC (same ledger rendered
  twice yields identical bytes — the measured divergence is encoder
  identity, never write nondeterminism);
* the best polars-native arm (``pl.from_arrow`` + ``compression="snappy"``,
  ``use_pyarrow=False``) is NOT byte-identical, and the divergence is the
  measured ENCODER-IDENTITY class: the ``created_by`` footer string differs,
  both encoders embed an ``ARROW:schema`` key-value block whose serialized
  flatbuffer BYTES differ, and the footer format-version marker
  differs — NONE of these is an exposed polars knob
  (``pl.DataFrame.write_parquet`` signature captured in the transcript);
* the polars-written file read back through the PRODUCTION
  ``_read_ledger_file_rows`` yields row tuples IDENTICAL to the pyarrow
  file's — the cross-encoder ROW fixed point the escalation's options
  (ii)/(iii) rely on (the reader is schema-name-based, never byte-based).

If a future pyarrow/polars bump ever makes any of these assertions flip
(e.g. the bytes become EQUAL), this test fails LOUDLY and deterministically —
that materially re-opens the G10 decision: re-run the spike probe and update
the escalation. That is the pin's purpose, not a flake.

Whitebox: drives the private ``_rows_to_table`` / ``_read_ledger_file_rows``
seams (the surfaces under probe — the DEC-P0-017 convention).
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pyarrow.parquet as pq

from brutex.data.fetch_coverage import WindowKey
from brutex.data.window_ledger import WindowLedger
from brutex.data.window_ledger_store import (
    _read_ledger_file_rows,
    _rows_to_table,
    ledger_path,
    save_window_ledger,
)

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


def _polars_native_parquet(prod: Path, out: Path) -> Path:
    """The spike's best arm: from_arrow + snappy through the polars Rust encoder."""
    rows = _read_ledger_file_rows(prod)
    frame = pl.from_arrow(_rows_to_table(rows))
    assert isinstance(frame, pl.DataFrame)
    frame.write_parquet(out, use_pyarrow=False, compression="snappy")
    return out


def test_production_ledger_write_is_byte_deterministic(tmp_path: Path) -> None:
    """Same ledger saved twice → identical bytes (divergence below is never flake)."""
    sha_a = _sha(_production_parquet(tmp_path / "a"))
    sha_b = _sha(_production_parquet(tmp_path / "b"))
    print(f"production sha256: {sha_a[:16]} / {sha_b[:16]}")
    assert sha_a == sha_b


def test_polars_native_bytes_diverge_in_the_documented_class(tmp_path: Path) -> None:
    """The measured G10 divergence class holds: not byte-identical, for pinned reasons."""
    prod = _production_parquet(tmp_path)
    pol = _polars_native_parquet(prod, tmp_path / "polars_native.parquet")
    prod_sha, pol_sha = _sha(prod), _sha(pol)
    print(f"pyarrow sha256: {prod_sha[:16]}  polars-native sha256: {pol_sha[:16]}")
    # The finding: NOT byte-identical (equality here re-opens the G10 decision).
    assert prod_sha != pol_sha
    assert prod.read_bytes() != pol.read_bytes()
    prod_md = pq.read_metadata(prod)  # type: ignore[no-untyped-call, unused-ignore]
    pol_md = pq.read_metadata(pol)  # type: ignore[no-untyped-call, unused-ignore]
    # Class 1: created_by is encoder identity — no polars knob exists for it.
    assert prod_md.created_by != pol_md.created_by
    assert prod_md.created_by.startswith("parquet-cpp-arrow")
    # Class 2: BOTH encoders embed an ARROW:schema key-value block (pyarrow via
    # its store_schema=True default; polars writes its own), but the serialized
    # flatbuffer BYTES differ — and neither side exposes a byte-level knob.
    prod_kv = prod_md.metadata
    pol_kv = pol_md.metadata
    assert prod_kv is not None and b"ARROW:schema" in prod_kv
    assert pol_kv is not None and b"ARROW:schema" in pol_kv
    assert prod_kv[b"ARROW:schema"] != pol_kv[b"ARROW:schema"]
    # Class 3: the footer format-version marker differs (pyarrow '2.6' family
    # vs polars '1.0' at the measured pyarrow 25.0.0 / polars 1.42.1).
    assert prod_md.format_version != pol_md.format_version
    # Both are still real parquet files (PAR1 magic head + tail).
    for path in (prod, pol):
        blob = path.read_bytes()
        assert blob[:4] == b"PAR1" and blob[-4:] == b"PAR1"


def test_cross_encoder_row_fixed_point(tmp_path: Path) -> None:
    """The module's REAL contract survives the encoder swap: rows are identical.

    ``_read_ledger_file_rows`` (the ONE production reader, schema-name-based)
    reads the polars-native file into row tuples IDENTICAL to the pyarrow
    file's — the load-bearing fact for the escalation's options (ii)/(iii):
    loaders are unaffected by the byte divergence.
    """
    prod = _production_parquet(tmp_path)
    pol = _polars_native_parquet(prod, tmp_path / "polars_native.parquet")
    prod_rows = _read_ledger_file_rows(prod)
    pol_rows = _read_ledger_file_rows(pol)
    assert prod_rows == pol_rows
    assert len(prod_rows) == 3  # anti-vacuous: the corpus genuinely has rows
