//! The window-ledger Parquet CODEC — the canonical encoder + decoder.
//!
//! DEC-RUST-RUNID-STORES-001, resolving the #1917 G10 byte-parity escalation
//! (DEC-LAKE-RUST-LEDGERSTORE-SPIKE-001 / DEC-LAKE-EXACT-PIN-001) by its
//! priced **option (iii)** — GOLDEN RE-BASELINE: the retired pyarrow
//! `pq.write_table` writer is deleted in the same lane, THIS encoder's bytes
//! become the canonical on-disk form going forward, and the #1917
//! cross-encoder ROW FIXED POINT stays the correctness law (a file written
//! here reads back to identical row tuples through this decoder AND through
//! pyarrow AND through DuckDB `read_parquet` — pinned by
//! `tests/golden/test_window_ledger_bytes_probe.py`). Byte-identity with the
//! historical pyarrow bytes was PROVEN unreachable by the #1917 spike
//! (encoder-identity divergence: `created_by`, footer format version, the
//! `ARROW:schema` flatbuffer, page-header thrift) and is NOT pursued;
//! greenfield makes the re-baseline coherent — no stored ledger exists.
//!
//! Encoder discipline: the fixed 16-column `LEDGER_COLUMNS` schema (every
//! column OPTIONAL — the pyarrow-written shape the DuckDB `/db` +
//! `/coverage` fast paths already read), ONE row group, zstd level 3 (the
//! repo's lake compression standard), row order preserved from the caller
//! (the merge law pre-sorts). Deterministic: same rows → same bytes (no
//! clock, no randomness; `created_by` is the fixed parquet-rs identity).

use crate::ledger::{LEDGER_CORE_COLUMNS, LedgerRow, RawLedgerRow};
use parquet::basic::{Compression, LogicalType, Repetition, Type as PhysicalType, ZstdLevel};
use parquet::data_type::{ByteArray, ByteArrayType, Int64Type};
use parquet::file::properties::WriterProperties;
use parquet::file::reader::{FileReader, SerializedFileReader};
use parquet::file::writer::SerializedFileWriter;
use parquet::record::Field;
use parquet::schema::types::Type;
use std::sync::Arc;

/// The zstd compression level — matches the lake/results-store discipline
/// (`ZSTD_LEVEL = 3`, deterministic across zstd >= 1.5).
const ZSTD_LEVEL: i32 = 3;

/// Build the fixed 16-column ledger schema (all columns OPTIONAL).
fn ledger_schema() -> Result<Arc<Type>, String> {
    let mut fields: Vec<Arc<Type>> = Vec::with_capacity(16);
    let string_columns = [
        "market",
        "security_id",
        "interval",
        "window_start",
        "window_end",
        "status",
        "last_error",
        "first_seen_at",
        "last_attempt_at",
    ];
    let int_columns = [
        "bars",
        "attempts",
        "empty_streak",
        "in_pull_fetches",
        "bars_net_new",
        "empty_retries",
        "transient_retries",
    ];
    // Declaration ORDER must follow LEDGER_COLUMNS — build by name.
    for name in crate::ledger::LEDGER_COLUMNS {
        let field = if string_columns.contains(&name) {
            Type::primitive_type_builder(name, PhysicalType::BYTE_ARRAY)
                .with_logical_type(Some(LogicalType::String))
                .with_repetition(Repetition::OPTIONAL)
                .build()
        } else {
            debug_assert!(int_columns.contains(&name));
            Type::primitive_type_builder(name, PhysicalType::INT64)
                .with_repetition(Repetition::OPTIONAL)
                .build()
        }
        .map_err(|e| format!("ledger schema field {name}: {e}"))?;
        fields.push(Arc::new(field));
    }
    Type::group_type_builder("schema")
        .with_fields(fields)
        .build()
        .map(Arc::new)
        .map_err(|e| format!("ledger schema: {e}"))
}

/// Write one OPTIONAL BYTE_ARRAY (UTF8) column batch.
fn write_string_column(
    rg: &mut parquet::file::writer::SerializedRowGroupWriter<'_, &mut Vec<u8>>,
    cells: Vec<Option<&str>>,
) -> Result<(), String> {
    let mut col = rg
        .next_column()
        .map_err(|e| format!("ledger write: next_column: {e}"))?
        .ok_or_else(|| "ledger write: schema/column count mismatch".to_string())?;
    let def_levels: Vec<i16> = cells.iter().map(|c| i16::from(c.is_some())).collect();
    let values: Vec<ByteArray> = cells
        .iter()
        .filter_map(|c| c.map(|s| ByteArray::from(s.as_bytes().to_vec())))
        .collect();
    col.typed::<ByteArrayType>()
        .write_batch(&values, Some(&def_levels), None)
        .map_err(|e| format!("ledger write: string batch: {e}"))?;
    col.close()
        .map_err(|e| format!("ledger write: close column: {e}"))?;
    Ok(())
}

/// Write one OPTIONAL INT64 column batch.
fn write_int_column(
    rg: &mut parquet::file::writer::SerializedRowGroupWriter<'_, &mut Vec<u8>>,
    cells: Vec<Option<i64>>,
) -> Result<(), String> {
    let mut col = rg
        .next_column()
        .map_err(|e| format!("ledger write: next_column: {e}"))?
        .ok_or_else(|| "ledger write: schema/column count mismatch".to_string())?;
    let def_levels: Vec<i16> = cells.iter().map(|c| i16::from(c.is_some())).collect();
    let values: Vec<i64> = cells.iter().filter_map(|c| *c).collect();
    col.typed::<Int64Type>()
        .write_batch(&values, Some(&def_levels), None)
        .map_err(|e| format!("ledger write: int batch: {e}"))?;
    col.close()
        .map_err(|e| format!("ledger write: close column: {e}"))?;
    Ok(())
}

/// Render ledger rows to canonical Parquet bytes (order-preserving).
pub fn write_ledger_parquet(rows: &[LedgerRow]) -> Result<Vec<u8>, String> {
    let schema = ledger_schema()?;
    let props = Arc::new(
        WriterProperties::builder()
            .set_compression(Compression::ZSTD(
                ZstdLevel::try_new(ZSTD_LEVEL).map_err(|e| format!("zstd level: {e}"))?,
            ))
            .build(),
    );
    let mut buf: Vec<u8> = Vec::new();
    {
        let mut writer = SerializedFileWriter::new(&mut buf, schema, props)
            .map_err(|e| format!("ledger write: open writer: {e}"))?;
        let mut rg = writer
            .next_row_group()
            .map_err(|e| format!("ledger write: open row group: {e}"))?;
        // Column order == LEDGER_COLUMNS order (the schema declaration order).
        write_string_column(
            &mut rg,
            rows.iter().map(|r| Some(r.market.as_str())).collect(),
        )?;
        write_string_column(
            &mut rg,
            rows.iter().map(|r| Some(r.security_id.as_str())).collect(),
        )?;
        write_string_column(
            &mut rg,
            rows.iter().map(|r| Some(r.interval.as_str())).collect(),
        )?;
        write_string_column(
            &mut rg,
            rows.iter().map(|r| Some(r.window_start.as_str())).collect(),
        )?;
        write_string_column(
            &mut rg,
            rows.iter().map(|r| Some(r.window_end.as_str())).collect(),
        )?;
        write_string_column(
            &mut rg,
            rows.iter().map(|r| Some(r.status.as_str())).collect(),
        )?;
        write_int_column(&mut rg, rows.iter().map(|r| Some(r.bars)).collect())?;
        write_int_column(&mut rg, rows.iter().map(|r| Some(r.attempts)).collect())?;
        write_string_column(
            &mut rg,
            rows.iter().map(|r| r.last_error.as_deref()).collect(),
        )?;
        write_string_column(
            &mut rg,
            rows.iter()
                .map(|r| Some(r.first_seen_at.as_str()))
                .collect(),
        )?;
        write_string_column(
            &mut rg,
            rows.iter().map(|r| r.last_attempt_at.as_deref()).collect(),
        )?;
        write_int_column(&mut rg, rows.iter().map(|r| Some(r.empty_streak)).collect())?;
        write_int_column(
            &mut rg,
            rows.iter().map(|r| Some(r.in_pull_fetches)).collect(),
        )?;
        write_int_column(&mut rg, rows.iter().map(|r| r.bars_net_new).collect())?;
        write_int_column(&mut rg, rows.iter().map(|r| r.empty_retries).collect())?;
        write_int_column(&mut rg, rows.iter().map(|r| r.transient_retries).collect())?;
        rg.close()
            .map_err(|e| format!("ledger write: close row group: {e}"))?;
        writer
            .close()
            .map_err(|e| format!("ledger write: close writer: {e}"))?;
    }
    Ok(buf)
}

/// One decoded cell in name-addressable form.
enum Cell {
    Null,
    Str(String),
    Int(i64),
}

/// Extract a required string cell (a NULL here is a wrong-shape file — the
/// retired pyarrow reader silently rendered it as the string "None"; this
/// decoder REFUSES it, a disclosed narrowing of DEC-RUST-RUNID-STORES-001:
/// a "None"-keyed ledger row is a latent-bug shape, and the caller's degrade
/// contract turns the refusal into a safe re-pull).
fn required_str(
    cells: &std::collections::HashMap<String, Cell>,
    name: &str,
) -> Result<String, String> {
    match cells.get(name) {
        Some(Cell::Str(s)) => Ok(s.clone()),
        Some(Cell::Null) => Err(format!("ledger read: null in required column {name}")),
        Some(Cell::Int(_)) => Err(format!("ledger read: wrong dtype in column {name}")),
        None => Err(format!("ledger read: missing required column {name}")),
    }
}

/// Extract a required int cell (NULL → error, matching the retired reader's
/// `int(None)` TypeError degrade).
fn required_int(
    cells: &std::collections::HashMap<String, Cell>,
    name: &str,
) -> Result<i64, String> {
    match cells.get(name) {
        Some(Cell::Int(v)) => Ok(*v),
        Some(Cell::Null) => Err(format!("ledger read: null in required column {name}")),
        Some(Cell::Str(_)) => Err(format!("ledger read: wrong dtype in column {name}")),
        None => Err(format!("ledger read: missing required column {name}")),
    }
}

/// Extract an optional string cell (absent column ≡ NULL cell ≡ None).
fn optional_str(
    cells: &std::collections::HashMap<String, Cell>,
    name: &str,
) -> Result<Option<String>, String> {
    match cells.get(name) {
        Some(Cell::Str(s)) => Ok(Some(s.clone())),
        Some(Cell::Null) | None => Ok(None),
        Some(Cell::Int(_)) => Err(format!("ledger read: wrong dtype in column {name}")),
    }
}

/// Extract an optional int cell (absent column ≡ NULL cell ≡ None).
fn optional_int(
    cells: &std::collections::HashMap<String, Cell>,
    name: &str,
) -> Result<Option<i64>, String> {
    match cells.get(name) {
        Some(Cell::Int(v)) => Ok(Some(*v)),
        Some(Cell::Null) | None => Ok(None),
        Some(Cell::Str(_)) => Err(format!("ledger read: wrong dtype in column {name}")),
    }
}

/// Render a caught panic payload as text (`&str` / `String` payloads
/// verbatim — the `panic!`/`assert!`/`unwrap` classes; anything else the
/// honest placeholder).
fn panic_text(payload: &(dyn std::any::Any + Send)) -> &str {
    if let Some(text) = payload.downcast_ref::<&str>() {
        text
    } else if let Some(text) = payload.downcast_ref::<String>() {
        text.as_str()
    } else {
        "<non-string panic payload>"
    }
}

/// Decode one ledger-shaped Parquet byte buffer into RAW rows (the three
/// legacy-derivable optional columns still unresolved — the caller applies
/// `coerce_raw_row`). The 11 CORE columns are REQUIRED; a missing one, a
/// wrong-dtype cell, or corrupt bytes all error (the caller's
/// `_LEDGER_READ_ERRORS` degrade contract).
///
/// PANIC BOUNDARY (independent refuter round 2 of DEC-RUST-RUNID-STORES-001,
/// a BLOCKER): parquet-rs PANICS — it does not `Err` — on
/// structurally-plausible-but-corrupt bytes (reproduced at parquet-57.3.1:
/// "column start and length should not be negative" + `Option::unwrap()` on
/// `None` in the footer-thrift decode, each reachable by a SINGLE flipped
/// byte in a canonical ledger file's footer). This is the ONE kernel surface
/// fed UNTRUSTED on-disk bytes, so the decode runs under `catch_unwind` and
/// a caught panic maps onto the SAME `Err(String)` path as every other
/// malformed-buffer shape — the FFI edge turns it into a typed Python
/// `ValueError` (a `_LEDGER_READ_ERRORS` member), never a process abort and
/// never a `pyo3_runtime.PanicException` (a BaseException OUTSIDE that
/// degrade set). The crate profile drops `panic = "abort"` for exactly this
/// boundary (see Cargo.toml). Cosmetic residual: the default panic hook
/// still prints the caught panic to stderr once per corrupt file — the
/// operator surface is the caller's structured WARNING, not that line.
pub fn read_ledger_parquet(bytes: bytes::Bytes) -> Result<Vec<RawLedgerRow>, String> {
    // AssertUnwindSafe: the closure OWNS its only captured value (`bytes`,
    // dropped on unwind) and writes no state observable after a panic.
    match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        read_ledger_parquet_impl(bytes)
    })) {
        Ok(result) => result,
        Err(payload) => Err(format!(
            "ledger read: parquet decode panicked: {}",
            panic_text(payload.as_ref())
        )),
    }
}

/// The unguarded decode body (see [`read_ledger_parquet`] for the panic
/// boundary that wraps it).
fn read_ledger_parquet_impl(bytes: bytes::Bytes) -> Result<Vec<RawLedgerRow>, String> {
    let reader = SerializedFileReader::new(bytes).map_err(|e| format!("ledger read: open: {e}"))?;
    // Fail fast on a missing CORE column (before any row decode).
    let file_fields: Vec<String> = reader
        .metadata()
        .file_metadata()
        .schema()
        .get_fields()
        .iter()
        .map(|f| f.name().to_string())
        .collect();
    for core in LEDGER_CORE_COLUMNS {
        if !file_fields.iter().any(|name| name == core) {
            return Err(format!("ledger read: missing required column {core}"));
        }
    }
    let mut out: Vec<RawLedgerRow> = Vec::new();
    let rows = reader
        .get_row_iter(None)
        .map_err(|e| format!("ledger read: row iter: {e}"))?;
    for row in rows {
        let row = row.map_err(|e| format!("ledger read: row decode: {e}"))?;
        let mut cells: std::collections::HashMap<String, Cell> = std::collections::HashMap::new();
        for (name, field) in row.get_column_iter() {
            let cell = match field {
                Field::Null => Cell::Null,
                Field::Str(s) => Cell::Str(s.clone()),
                Field::Long(v) => Cell::Int(*v),
                other => {
                    return Err(format!(
                        "ledger read: unsupported field type in column {name}: {other:?}"
                    ));
                }
            };
            cells.insert(name.clone(), cell);
        }
        out.push(RawLedgerRow {
            market: required_str(&cells, "market")?,
            security_id: required_str(&cells, "security_id")?,
            interval: required_str(&cells, "interval")?,
            window_start: required_str(&cells, "window_start")?,
            window_end: required_str(&cells, "window_end")?,
            status: required_str(&cells, "status")?,
            bars: required_int(&cells, "bars")?,
            attempts: required_int(&cells, "attempts")?,
            last_error: optional_str(&cells, "last_error")?,
            first_seen_at: required_str(&cells, "first_seen_at")?,
            last_attempt_at: optional_str(&cells, "last_attempt_at")?,
            empty_streak: optional_int(&cells, "empty_streak")?,
            in_pull_fetches: optional_int(&cells, "in_pull_fetches")?,
            bars_net_new: optional_int(&cells, "bars_net_new")?,
            empty_retries: optional_int(&cells, "empty_retries")?,
            transient_retries: optional_int(&cells, "transient_retries")?,
        });
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ledger::coerce_raw_row;

    fn sample_rows() -> Vec<LedgerRow> {
        vec![
            LedgerRow {
                market: "groww".into(),
                security_id: "NSE-NIFTY".into(),
                interval: "1minute".into(),
                window_start: "2024-01-01".into(),
                window_end: "2024-01-31".into(),
                status: "SUCCESS".into(),
                bars: 375,
                attempts: 1,
                last_error: None,
                first_seen_at: "2024-01-31T16:00:00+00:00".into(),
                last_attempt_at: Some("2024-01-31T16:00:00+00:00".into()),
                empty_streak: 0,
                in_pull_fetches: 1,
                bars_net_new: Some(375),
                empty_retries: Some(0),
                transient_retries: Some(0),
            },
            LedgerRow {
                market: "groww".into(),
                security_id: "NSE-M&M".into(),
                interval: "1day".into(),
                window_start: "2024-02-01".into(),
                window_end: "2024-02-29".into(),
                status: "FAILED".into(),
                bars: 0,
                attempts: 3,
                last_error: Some("429 rate limited\nline2\ttab".into()),
                first_seen_at: "2024-02-29T16:00:00+00:00".into(),
                last_attempt_at: None,
                empty_streak: 2,
                in_pull_fetches: 4,
                bars_net_new: None,
                empty_retries: None,
                transient_retries: None,
            },
        ]
    }

    /// The G10 option-(iii) correctness law: write → read is a ROW fixed
    /// point (the pyarrow + DuckDB cross-reads are pinned Python-side).
    #[test]
    fn row_fixed_point() {
        let rows = sample_rows();
        let bytes = write_ledger_parquet(&rows).expect("write");
        let raw = read_ledger_parquet(bytes::Bytes::from(bytes)).expect("read");
        let resolved: Vec<LedgerRow> = raw.into_iter().map(|r| coerce_raw_row(r, 3)).collect();
        assert_eq!(resolved, rows);
    }

    /// Same rows → same bytes (the canonical-encoder determinism pin; no
    /// clock, no randomness in the write path).
    #[test]
    fn write_is_deterministic() {
        let rows = sample_rows();
        let a = write_ledger_parquet(&rows).expect("write a");
        let b = write_ledger_parquet(&rows).expect("write b");
        assert_eq!(a, b);
    }

    /// The empty ledger round-trips (a fresh lake's first save).
    #[test]
    fn empty_round_trip() {
        let bytes = write_ledger_parquet(&[]).expect("write");
        let raw = read_ledger_parquet(bytes::Bytes::from(bytes)).expect("read");
        assert!(raw.is_empty());
    }

    /// Corrupt bytes error loudly (the caller degrades, never crashes).
    #[test]
    fn corrupt_bytes_error() {
        let err = read_ledger_parquet(bytes::Bytes::from_static(b"not parquet at all"))
            .expect_err("corrupt");
        assert!(err.contains("ledger read"), "{err}");
    }

    /// The refuter-round-2 BLOCKER regression (DEC-RUST-RUNID-STORES-001):
    /// a deterministic single-byte corruption sweep over a canonical ledger
    /// file's footer region (both an XOR 0x01 bit-flip and an XOR 0xFF byte
    /// inversion per offset, plus a stride over the body) must NEVER escape
    /// [`read_ledger_parquet`] as a panic — parquet-rs panics on several of
    /// these shapes ("column start and length should not be negative",
    /// `Option::unwrap()` on `None` in the footer thrift) and the
    /// catch_unwind boundary must map every one onto the `Err(String)`
    /// degrade path. Fails-before: without the boundary this test dies as a
    /// panic at the first such case (and the shipped `panic = "abort"` wheel
    /// SIGABRTed the whole Python process — reproduced, exit 134).
    /// Anti-vacuous teeth: the sweep must actually EXERCISE the panic class,
    /// including the named negative-column-start/length metadata panic.
    #[test]
    fn corrupt_footer_sweep_panics_are_caught_not_escaped() {
        let base = write_ledger_parquet(&sample_rows()).expect("write");
        let n = base.len();
        // Silence the default panic hook for the sweep (message printing
        // only — behaviour is unchanged); restore it after.
        let previous_hook = std::panic::take_hook();
        std::panic::set_hook(Box::new(|_| {}));
        let mut panicked_cases = 0usize;
        let mut negative_metadata_cases = 0usize;
        let mut offsets: Vec<usize> = (n.saturating_sub(700)..n).collect();
        offsets.extend((0..n.saturating_sub(700)).step_by(97));
        for offset in offsets {
            for mask in [0x01u8, 0xFF] {
                let mut corrupted = base.clone();
                corrupted[offset] ^= mask;
                // A panic HERE (not caught by the boundary) fails the test.
                match read_ledger_parquet(bytes::Bytes::from(corrupted)) {
                    Ok(_) => {} // an immaterial byte (padding / a value cell)
                    Err(err) => {
                        if err.contains("parquet decode panicked") {
                            panicked_cases += 1;
                            if err.contains("negative") {
                                negative_metadata_cases += 1;
                            }
                        }
                        assert!(err.contains("ledger read"), "{err}");
                    }
                }
            }
        }
        std::panic::set_hook(previous_hook);
        assert!(panicked_cases >= 1, "sweep never exercised the panic class");
        assert!(
            negative_metadata_cases >= 1,
            "sweep never exercised the negative column start/length metadata panic"
        );
    }

    /// A caught panic's message carries the payload text (the operator sees
    /// WHAT parquet-rs refused, not just "panicked").
    #[test]
    fn panic_text_renders_str_and_string_payloads() {
        let s: Box<dyn std::any::Any + Send> = Box::new("static payload");
        assert_eq!(panic_text(s.as_ref()), "static payload");
        let owned: Box<dyn std::any::Any + Send> = Box::new(String::from("owned payload"));
        assert_eq!(panic_text(owned.as_ref()), "owned payload");
        let other: Box<dyn std::any::Any + Send> = Box::new(42usize);
        assert_eq!(panic_text(other.as_ref()), "<non-string panic payload>");
    }
}
