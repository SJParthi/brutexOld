//! # window_ledger_render — the window-ledger parquet RENDER (ADR-0020 G10 CONVERT)
//!
//! DEC-LAKE-RUST-LEDGERSTORE-CONVERT-001 (MIGRATION.md #1918; evidence
//! predecessor: the #1917 spike, `docs/lake/g10_window_ledger_byte_parity/`).
//! Renders the durable window ledger's 18-column row table
//! (`brutex/data/window_ledger_store.py::_rows_to_table`) from uncompressed
//! Arrow IPC bytes to parquet bytes through the crate's rev-pinned polars
//! write path — the SAME shared streaming-sink core the bars/options render
//! profiles ride ([`crate::render_parquet_with_options_impl`]).
//!
//! ## The standard is ROW-EQUIVALENCE, never bytes (the operator ruling of
//! 2026-07-16 as relayed via the coordinator)
//!
//! The #1917 spike measured 14 polars-native arms × 7 production-writer
//! corpus cases with ZERO sha256 matches against the pyarrow production
//! bytes — the divergence is ENCODER IDENTITY (`created_by`, the footer
//! format-version marker, the `ARROW:schema` flatbuffer bytes, the first
//! data-page header thrift, the empty-file 1-vs-0 row-group structure), none
//! an exposed polars knob. NO consumer hashes or byte-prefixes ledger bytes
//! (verified: the restore-drill byte-PREFIX law is audit-JSONL-only; backup
//! sha256s the destination copy self-consistently; lake_revision /
//! lake_manifest read only `(mtime_ns:size)` stat tokens), so this module's
//! contract is the module's OWN documented row-level fixed point
//! (`window_ledger_store.py:16`): the rendered parquet must yield IDENTICAL
//! ordered 18-column rows through every production reader (pyarrow, polars,
//! DuckDB — the reader-parity gates). Byte-identity with pyarrow is
//! MEASURED-INFEASIBLE and deliberately NOT claimed; golden re-baseline is
//! FORBIDDEN (the frozen goldens stand — the probe pin
//! `tests/golden/test_window_ledger_bytes_probe.py` keeps asserting the
//! divergence class).
//!
//! ## Options choice
//!
//! The render passes the crate's [`crate::defaults_writer_options`] — the
//! wheel's zero-kwarg `df.write_parquet(path)` shape (`Zstd(None)`,
//! statistics min/max/null_count, `row_group_size=None` → the streaming
//! sink's 122,880-row split). Under the row-equivalence standard the options
//! choose only the BYTE surface, never the rows; the zero-kwarg default is
//! the closest analogue of the Python oracle's zero-kwarg
//! `pq.write_table(table, tmp)` and is the crate's probe-proven
//! deterministic profile (the PR-2 corpus). A DISTINCT pyfunction
//! ([`render_ledger_parquet_from_ipc`]) still exists so the ledger facade's
//! SYMBOL handshake can tell a pre-G10 wheel apart (`crate_stale`) and so a
//! future ledger-specific options divergence needs no facade change.
//!
//! ## The dtype WATCH-ITEM (`last_attempt_at` stays a STRING)
//!
//! `first_seen_at` / `last_attempt_at` are DELIBERATELY ISO-8601 UTC
//! **strings** in the ledger schema (`window_ledger_store.py` — the
//! lexicographic-compare law): the DuckDB per-run counts arm
//! (`db_view._window_counts_since`) filters `last_attempt_at >= ?` as a
//! VARCHAR compare, and the merge conflict rule orders the raw strings. A
//! render that "helpfully" parsed them into a timestamp dtype would silently
//! change that comparison surface — so the render NEVER re-types a column:
//! the frame arrives with the facade-built dtypes (String × 10, Int64 × 8)
//! and the IPC round-trip preserves them exactly
//! (`ledger_schema_dtypes_exact` pins it; the facade's burn-in re-verifies
//! per process). Honest disclosure: polars String maps to Arrow
//! `large_string` in the embedded `ARROW:schema`, so a pyarrow READBACK of
//! the rendered file reports `large_string` where the pyarrow oracle reports
//! `string` — a representation-metadata artifact of the SAME logical string
//! column (parquet physical BYTE_ARRAY/UTF8 either way; DuckDB sees VARCHAR
//! either way; `to_pylist()` yields identical `str`s). The facade's burn-in
//! folds exactly that string FAMILY and nothing else — a timestamp-typed
//! `last_attempt_at` still refuses loudly.
//!
//! HONESTY (ADR-0020 banner): expected payoff ≈ 0 — conversion is for
//! uniformity, not speed. The engaged render pays one uncompressed-IPC
//! serialisation + one FFI crossing the pyarrow path does not; the seam runs
//! per throttled ledger flush (an I/O-dominated pull path) and ships
//! DEFAULT-OFF behind `BRUTEX_LAKE_RUST_LEDGER_RENDER`.

use polars::prelude::*;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

use crate::{defaults_writer_options, render_at_ffi_boundary, render_parquet_with_options_impl};

/// The ledger render core: uncompressed Arrow IPC bytes → parquet bytes via
/// the shared streaming-sink chain with the zero-kwarg defaults options
/// (module docs). A 0-ROW frame is a VALID input (an empty ledger save) and
/// renders a readable 18-column parquet with ZERO row groups (the documented
/// polars-vs-pyarrow empty-file structure divergence — footer-schema-based
/// readers are unaffected, the reader-parity arm (f) proves it); an EMPTY
/// IPC payload / garbage payload is a clean `PolarsError` (the shared core's
/// refusal), never a panic.
pub fn render_ledger_parquet_from_ipc_impl(ipc_bytes: &[u8]) -> PolarsResult<Vec<u8>> {
    render_parquet_with_options_impl(ipc_bytes, defaults_writer_options())
}

/// FFI face of [`render_ledger_parquet_from_ipc_impl`] — the shared
/// [`crate::render_at_ffi_boundary`] shell (GIL released; polars error →
/// `ValueError`, panic → `RuntimeError`; the result `PyBytes` construction
/// runs under its own panic guard). The DISTINCT symbol is the ledger
/// facade's stale-wheel handshake: a pre-G10 `brutex_lake` install imports
/// fine but lacks this name and latches `crate_stale`, never an
/// `AttributeError` into a ledger save.
#[pyfunction]
pub fn render_ledger_parquet_from_ipc(py: Python<'_>, ipc_bytes: &[u8]) -> PyResult<Py<PyBytes>> {
    render_at_ffi_boundary(py, ipc_bytes, render_ledger_parquet_from_ipc_impl)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The 18 ledger columns in the store's `_COLUMNS` order
    /// (`window_ledger_store.py` — 11 core + the 7 appended optionals).
    const LEDGER_COLUMNS: [&str; 18] = [
        "market",
        "security_id",
        "interval",
        "window_start",
        "window_end",
        "status",
        "bars",
        "attempts",
        "last_error",
        "first_seen_at",
        "last_attempt_at",
        "empty_streak",
        "in_pull_fetches",
        "bars_net_new",
        "empty_retries",
        "transient_retries",
        "streak_provenance",
        "failed_streak",
    ];

    /// Build a ledger-shaped 18-column frame (the facade's dtypes: String ×10,
    /// Int64 ×8) with per-column nulls in every NULLABLE column — plus its
    /// uncompressed IPC serialisation. Row `i` is keyed `SEC{i:04}` so
    /// row-order assertions are unambiguous.
    fn ledger_sample_ipc(rows: usize) -> (DataFrame, Vec<u8>) {
        let n = rows;
        let sec: Vec<String> = (0..n).map(|i| format!("SEC{i:04}")).collect();
        let status: Vec<&str> = (0..n)
            .map(|i| match i % 4 {
                0 => "SUCCESS",
                1 => "EMPTY",
                2 => "FAILED",
                _ => "PLANNED",
            })
            .collect();
        // Nullable string columns: last_error (None unless FAILED) and
        // last_attempt_at (None on the PLANNED arm — the never-attempted row).
        let last_error: Vec<Option<&str>> = (0..n)
            .map(|i| (i % 4 == 2).then_some("GrowwTimeoutError"))
            .collect();
        let last_attempt: Vec<Option<String>> = (0..n)
            .map(|i| (i % 4 != 3).then(|| format!("2026-06-08T12:{:02}:00.000001+00:00", i % 60)))
            .collect();
        // Nullable int columns (honest-unknown None legacy arms).
        let nullable_i64: Vec<Option<i64>> =
            (0..n).map(|i| (i % 3 != 1).then_some(i as i64)).collect();
        // Nullable string 17th column (club-5 #1299): None on the legacy arm.
        let streak_provenance: Vec<Option<&str>> =
            (0..n).map(|i| (i % 3 != 1).then_some("live")).collect();
        // failed_streak (18th, appended LAST) — a running counter, always a
        // REAL i64 (the store schema's non-null pa.int64()).
        let failed_streak: Vec<i64> = (0..n).map(|i| if i % 4 == 2 { 2 } else { 0 }).collect();
        let df = DataFrame::new(
            n,
            vec![
                Series::new("market".into(), vec!["groww"; n]).into(),
                Series::new("security_id".into(), sec).into(),
                Series::new("interval".into(), vec!["1minute"; n]).into(),
                Series::new("window_start".into(), vec!["2021-01-01"; n]).into(),
                Series::new("window_end".into(), vec!["2021-08-01"; n]).into(),
                Series::new("status".into(), status).into(),
                Series::new("bars".into(), (0..n as i64).collect::<Vec<_>>()).into(),
                Series::new("attempts".into(), vec![1i64; n]).into(),
                Series::new("last_error".into(), last_error).into(),
                Series::new(
                    "first_seen_at".into(),
                    vec!["2026-06-08T12:00:00.000001+00:00"; n],
                )
                .into(),
                Series::new("last_attempt_at".into(), last_attempt).into(),
                Series::new("empty_streak".into(), vec![0i64; n]).into(),
                Series::new("in_pull_fetches".into(), vec![1i64; n]).into(),
                Series::new("bars_net_new".into(), nullable_i64.clone()).into(),
                Series::new("empty_retries".into(), nullable_i64.clone()).into(),
                Series::new("transient_retries".into(), nullable_i64).into(),
                Series::new("streak_provenance".into(), streak_provenance).into(),
                Series::new("failed_streak".into(), failed_streak).into(),
            ],
        )
        .unwrap();
        let mut ipc = Vec::new();
        IpcWriter::new(&mut ipc)
            .with_compression(None)
            .finish(&mut df.clone())
            .unwrap();
        (df, ipc)
    }

    #[test]
    fn ledger_schema_dtypes_exact() {
        // The 18-col schema round-trips EXACTLY — names, ORDER and dtypes.
        // CRITICAL WATCH-ITEM: `last_attempt_at` (and `first_seen_at`) MUST
        // stay String — a timestamp dtype would silently change the DuckDB
        // `last_attempt_at >= ?` VARCHAR-compare surface
        // (db_view._window_counts_since) and the merge rule's lexicographic
        // ordering. The render never re-types a column.
        let (_, ipc) = ledger_sample_ipc(12);
        let parquet = render_ledger_parquet_from_ipc_impl(&ipc).unwrap();
        let back = ParquetReader::new(std::io::Cursor::new(parquet))
            .finish()
            .unwrap();
        let names: Vec<&str> = back.get_column_names().iter().map(|s| s.as_str()).collect();
        assert_eq!(names, LEDGER_COLUMNS);
        for name in [
            "market",
            "security_id",
            "interval",
            "window_start",
            "window_end",
            "status",
            "last_error",
            "first_seen_at",
            "last_attempt_at",
            "streak_provenance",
        ] {
            assert_eq!(
                back.column(name).unwrap().dtype(),
                &DataType::String,
                "{name} must stay a String column"
            );
        }
        for name in [
            "bars",
            "attempts",
            "empty_streak",
            "in_pull_fetches",
            "bars_net_new",
            "empty_retries",
            "transient_retries",
            "failed_streak",
        ] {
            assert_eq!(
                back.column(name).unwrap().dtype(),
                &DataType::Int64,
                "{name} must stay an Int64 column"
            );
        }
    }

    #[test]
    fn ledger_row_order_and_values_preserved() {
        // Row-order preservation: the rendered file decodes back to the SAME
        // frame in the SAME order (maintain_order=true rides the shared
        // UnifiedSinkArgs default), and a second render is byte-identical
        // (determinism — the burn-in's precondition).
        let (df, ipc) = ledger_sample_ipc(50);
        let parquet = render_ledger_parquet_from_ipc_impl(&ipc).unwrap();
        assert_eq!(&parquet[..4], b"PAR1");
        assert_eq!(&parquet[parquet.len() - 4..], b"PAR1");
        let back = ParquetReader::new(std::io::Cursor::new(parquet.clone()))
            .finish()
            .unwrap();
        assert!(back.equals_missing(&df));
        // Order, not just multiset: the key column decodes in write order.
        let want_sec = Series::new(
            "security_id".into(),
            (0..50).map(|i| format!("SEC{i:04}")).collect::<Vec<_>>(),
        );
        assert!(
            back.column("security_id")
                .unwrap()
                .as_materialized_series()
                .equals(&want_sec)
        );
        let again = render_ledger_parquet_from_ipc_impl(&ipc).unwrap();
        assert_eq!(parquet, again);
    }

    #[test]
    fn ledger_per_column_nulls_survive() {
        // Per-column null handling: every nullable arm (last_error,
        // last_attempt_at, bars_net_new, empty_retries, transient_retries,
        // streak_provenance) keeps its exact null positions through the render.
        let (df, ipc) = ledger_sample_ipc(24);
        let parquet = render_ledger_parquet_from_ipc_impl(&ipc).unwrap();
        let back = ParquetReader::new(std::io::Cursor::new(parquet))
            .finish()
            .unwrap();
        for name in [
            "last_error",
            "last_attempt_at",
            "bars_net_new",
            "empty_retries",
            "transient_retries",
            "streak_provenance",
        ] {
            let want = df.column(name).unwrap();
            let got = back.column(name).unwrap();
            assert!(want.null_count() > 0, "{name} fixture must carry nulls");
            assert_eq!(got.null_count(), want.null_count(), "{name} null count");
            assert!(
                got.is_null()
                    .into_series()
                    .equals(&want.is_null().into_series()),
                "{name} null positions"
            );
        }
    }

    #[test]
    fn ledger_empty_frame_renders_zero_row_groups() {
        // The EMPTY-ledger watch-item (spec arm f): a 0-ROW 18-col frame is a
        // VALID save shape; polars renders ZERO row groups (pyarrow renders
        // one empty group — the documented structure divergence) and the 18
        // columns live in the FOOTER schema, so footer-based readers
        // (pyarrow / polars / DuckDB describe) still see all 18.
        let (_, ipc) = ledger_sample_ipc(0);
        let parquet = render_ledger_parquet_from_ipc_impl(&ipc).unwrap();
        assert_eq!(&parquet[..4], b"PAR1");
        let mut reader = ParquetReader::new(std::io::Cursor::new(parquet.clone()));
        let metadata = reader.get_metadata().unwrap();
        assert_eq!(metadata.row_groups.len(), 0);
        let back = ParquetReader::new(std::io::Cursor::new(parquet))
            .finish()
            .unwrap();
        assert_eq!(back.height(), 0);
        assert_eq!(back.width(), 18);
        assert_eq!(
            back.get_column_names()
                .iter()
                .map(|s| s.as_str())
                .collect::<Vec<_>>(),
            LEDGER_COLUMNS
        );
    }

    #[test]
    fn ledger_empty_payload_is_refused() {
        // EMPTY IPC BYTES (distinct from a 0-row frame) refuse cleanly — the
        // shared core's guard; the facade degrades to the oracle on it.
        let err = render_ledger_parquet_from_ipc_impl(&[]).unwrap_err();
        assert!(err.to_string().contains("empty IPC payload"));
    }

    #[test]
    fn ledger_garbage_payload_is_a_clean_error() {
        let err = render_ledger_parquet_from_ipc_impl(b"not an arrow ipc stream").unwrap_err();
        assert!(!err.to_string().is_empty());
    }
}
