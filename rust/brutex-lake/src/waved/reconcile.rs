//! # Wave-D W-3 — the `catalog_reconcile` per-file scan primitive
//!
//! DEC-RUST-LAKE-WAVED-001, design §B3/§C-3 (`scratchpad/waved_design.md`):
//! the per-file read/hash/classify body of
//! `brutex/data/catalog_reconcile.py::reconcile_catalog_from_lake` moves here
//! as ONE call per partition file; the LOOP — glob enumeration,
//! `parse_partition_path`, the stat-cache branch, `CatalogEntry` build +
//! upsert, the `detect_legacy_drift` fold, `_as_utc`, all logging — stays
//! Python (the catalog is DuckDB, ADR row 9).
//!
//! ## Split honored
//!
//! * The file is opened by EXACT path (the `glob=False` metachar law,
//!   DEC-SPOT-DERIVE-TS-RANGE-HARDENING-001 — holds BY CONSTRUCTION: the
//!   bytes are read verbatim and decoded from memory, never a glob pattern).
//! * `raw_hash` = sha256 over the file BYTES — **run-identity-adjacent**
//!   (Golden Rule #4: the hash feeds the FR-CAT-004 catalog rollup →
//!   `data_snapshot_hash`), so the hex MUST be byte-equal to Python's
//!   `hashlib.sha256(path.read_bytes()).hexdigest()`; the facade burn-in +
//!   the twin-catalog differential are the teeth.
//! * The L1 classify piggyback (`classify_bar_keys`) returns COUNTS —
//!   `(physical, unique)` when duplicated, plus the foreign-month row count;
//!   the Python facade reconstructs the `IntraFileDup` / `ForeignMonthRows`
//!   finding objects (dataclasses stay Python — house law).
//! * ERROR = degrade-to-oracle (deviation D-2 doctrine): any refusal —
//!   unreadable bytes, a non-`Datetime[us]` timestamp dtype, an all-null
//!   column, an out-of-`datetime`-range value — raises a plain `ValueError`
//!   at the FFI and the facade re-runs the live Python oracle, which raises
//!   the REAL exception (`PolarsError` / `OverflowError` / `ValueError`)
//!   with today's message bytes, so the shell's GAP-#9 skip verdict + its
//!   `catalog.reconcile.parquet_skip` warn payload stay byte-identical by
//!   construction.
//!
//! ## The byte-parity envelope (everything else degrades)
//!
//! * `timestamp` column present with dtype `Datetime[us]` (aware — any tz —
//!   or naive; the lake writer stores `us`/UTC only), at least one non-null
//!   value, every value inside Python's `datetime` range.
//! * `first_ts_us`/`last_ts_us` are PHYSICAL epoch microseconds +
//!   `tz_aware`; the facade rebuilds the exact Python `Series.min()/max()`
//!   values (an aware column's min compares instant-equal, a naive column's
//!   min is the naive wall time — both epoch + physical µs).
//! * `unique` counts a null timestamp as ONE distinct value (the polars
//!   `n_unique` law) and a null ABSTAINS from the foreign count (the
//!   `is_foreign_timestamp` isinstance guard) — nulls are hand-plant-only,
//!   but the twins must not fork on them.

use std::collections::HashSet;

use polars::prelude::*;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;

use super::civil_from_days;
use super::heal::sha256_hex;
use super::read::{DATETIME_MAX_US, DATETIME_MIN_US};

/// Microseconds per civil day — the UTC month-routing divisor.
const US_PER_DAY: i64 = 86_400_000_000;

/// One partition file's reconcile scan payload (the §C-3 wire, minus the
/// zero-row case which crosses as `None`).
#[derive(Debug)]
pub struct FileReconcile {
    /// Physical row count (> 0 — zero rows return `None` upstream).
    pub row_count: u64,
    /// Physical epoch-µs minimum of the non-null timestamps.
    pub first_ts_us: i64,
    /// Physical epoch-µs maximum of the non-null timestamps.
    pub last_ts_us: i64,
    /// Whether the stored column carries a timezone (any tz) — the facade
    /// rebuilds an aware-UTC (instant-equal) or naive datetime accordingly.
    pub tz_aware: bool,
    /// `sha256(file bytes)` lowercase hex — the run-identity-adjacent hash.
    pub sha256_hex: String,
    /// `(physical_rows, unique_keys)` when `physical > unique`, else `None`
    /// (the `IntraFileDup` counts; `None` when classification is off).
    pub dup: Option<(u64, u64)>,
    /// Rows whose UTC `(year, month)` differs from the classify home month
    /// (the `ForeignMonthRows` count; `0` when clean or classification off).
    pub foreign_rows: u64,
}

/// The per-file scan core: read the file bytes ONCE → sha256 → decode the
/// `timestamp` column from memory → row count / physical min-max /
/// ts-range gate → the optional L1 classify counts. `Ok(None)` = a
/// zero-row file (the caller's counted skip); ANY refusal is an `Err` the
/// facade degrades to the Python oracle (module docs).
pub fn reconcile_file_impl(
    path: &str,
    classify_home: Option<(i32, u32)>,
) -> PolarsResult<Option<FileReconcile>> {
    let bytes = std::fs::read(path)
        .map_err(|err| polars_err!(ComputeError: "cannot read {}: {}", path, err))?;
    let df = ParquetReader::new(std::io::Cursor::new(&bytes))
        .with_columns(Some(vec!["timestamp".to_string()]))
        .finish()?;
    let row_count = df.height();
    if row_count == 0 {
        return Ok(None);
    }
    let ts = df.column("timestamp")?;
    let DataType::Datetime(TimeUnit::Microseconds, tz) = ts.dtype() else {
        return Err(polars_err!(
            ComputeError:
            "timestamp column of {} is {} — outside the W-3 Datetime[us] envelope",
            path, ts.dtype()
        ));
    };
    let tz_aware = tz.is_some();
    let phys = ts.to_physical_repr();
    let phys = phys.i64()?;
    let (Some(first_ts_us), Some(last_ts_us)) = (phys.min(), phys.max()) else {
        return Err(polars_err!(
            ComputeError: "timestamp column of {} is all-null — outside the W-3 envelope", path
        ));
    };
    if first_ts_us < DATETIME_MIN_US || last_ts_us > DATETIME_MAX_US {
        // The Python arm's min()/max() raise OverflowError/ValueError here
        // (the round-18 class); erroring OUT routes the facade to the
        // oracle, which reproduces that exact skip verdict + message.
        return Err(polars_err!(
            ComputeError:
            "timestamp value outside the representable datetime range \
             (physical min {}, max {})",
            first_ts_us, last_ts_us
        ));
    }
    let (dup, foreign_rows) = match classify_home {
        None => (None, 0),
        Some((home_year, home_month)) => {
            let mut distinct: HashSet<Option<i64>> = HashSet::with_capacity(row_count);
            let mut foreign: u64 = 0;
            for value in phys.iter() {
                distinct.insert(value);
                if let Some(us) = value {
                    let (year, month, _) = civil_from_days(us.div_euclid(US_PER_DAY));
                    if year != i64::from(home_year) || month != home_month {
                        foreign += 1;
                    }
                }
            }
            let physical = row_count as u64;
            let unique = distinct.len() as u64;
            let dup = (physical > unique).then_some((physical, unique));
            (dup, foreign)
        }
    };
    Ok(Some(FileReconcile {
        row_count: row_count as u64,
        first_ts_us,
        last_ts_us,
        tz_aware,
        sha256_hex: sha256_hex(&bytes),
        dup,
        foreign_rows,
    }))
}

/// Scan one partition file for the catalog reconcile: `(row_count,
/// first_ts_us, last_ts_us, tz_aware, sha256_hex, dup, foreign_rows)`, or
/// `None` for a zero-row file. *classify_home* = the file's `(year, month)`
/// to run the L1 dup/foreign classification against, or `None` to skip it
/// (`check_invariants=False`).
///
/// Raises `ValueError` on any read/gate refusal (the facade degrades to the
/// Python oracle, which reproduces the exact typed error + message), and
/// `RuntimeError` if the scan panics (caught at this boundary — a panic
/// never crosses the FFI). Runs without the GIL (owned inputs).
#[pyfunction]
#[allow(clippy::type_complexity)]
pub(crate) fn waved_reconcile_file(
    py: Python<'_>,
    path: String,
    classify_home: Option<(i32, u32)>,
) -> PyResult<Option<(u64, i64, i64, bool, String, Option<(u64, u64)>, u64)>> {
    let result =
        py.detach(|| std::panic::catch_unwind(|| reconcile_file_impl(&path, classify_home)));
    match result {
        Ok(Ok(Some(scan))) => Ok(Some((
            scan.row_count,
            scan.first_ts_us,
            scan.last_ts_us,
            scan.tz_aware,
            scan.sha256_hex,
            scan.dup,
            scan.foreign_rows,
        ))),
        Ok(Ok(None)) => Ok(None),
        Ok(Err(err)) => Err(PyValueError::new_err(format!(
            "brutex-lake waved reconcile failed: {err}"
        ))),
        Err(_) => Err(PyRuntimeError::new_err(
            "brutex-lake waved reconcile panicked (caught at the FFI boundary)",
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A per-test unique scratch dir (std only — the `locks.rs` idiom).
    fn scratch_dir(tag: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "brutex-lake-waved-reconcile-{tag}-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// 2024-06-03 04:01 UTC (a June instant), epoch microseconds.
    const JUNE_US: i64 = 1_717_387_260_000_000;
    /// 2024-07-03 04:03 UTC (a July instant), epoch microseconds.
    const JULY_US: i64 = 1_719_979_380_000_000;
    const MINUTE_US: i64 = 60_000_000;

    fn ts_frame(ts_us: &[Option<i64>], tz: Option<TimeZone>) -> DataFrame {
        let dtype = DataType::Datetime(TimeUnit::Microseconds, tz);
        let ts = Series::new("timestamp".into(), ts_us.to_vec())
            .cast(&dtype)
            .unwrap();
        let closes: Vec<f64> = (0..ts_us.len()).map(|i| 100.0 + i as f64).collect();
        DataFrame::new_infer_height(vec![
            ts.into(),
            Column::new("open".into(), closes.clone()),
            Column::new("close".into(), closes),
        ])
        .unwrap()
    }

    fn write_parquet(dir: &std::path::Path, rel: &str, df: &DataFrame) -> String {
        let path = dir.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let file = std::fs::File::create(&path).unwrap();
        ParquetWriter::new(file).finish(&mut df.clone()).unwrap();
        path.to_string_lossy().into_owned()
    }

    #[test]
    fn clean_file_scans_counts_span_and_sha256() {
        let dir = scratch_dir("clean");
        let path = write_parquet(
            &dir,
            "06.parquet",
            &ts_frame(
                &[Some(JUNE_US), Some(JUNE_US + MINUTE_US)],
                Some(TimeZone::UTC),
            ),
        );
        let scan = reconcile_file_impl(&path, Some((2024, 6)))
            .unwrap()
            .unwrap();
        assert_eq!(scan.row_count, 2);
        assert_eq!(scan.first_ts_us, JUNE_US);
        assert_eq!(scan.last_ts_us, JUNE_US + MINUTE_US);
        assert!(scan.tz_aware);
        assert_eq!(scan.dup, None);
        assert_eq!(scan.foreign_rows, 0);
        // The run-identity tooth: the hex equals an independent sha256 over
        // the same bytes (cross-language equality is the Python differential).
        let expected = sha256_hex(&std::fs::read(&path).unwrap());
        assert_eq!(scan.sha256_hex, expected);
        assert_eq!(scan.sha256_hex.len(), 64);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn zero_row_file_returns_none() {
        let dir = scratch_dir("zerorow");
        let path = write_parquet(&dir, "06.parquet", &ts_frame(&[], Some(TimeZone::UTC)));
        assert!(
            reconcile_file_impl(&path, Some((2024, 6)))
                .unwrap()
                .is_none()
        );
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn dup_and_foreign_counts_match_the_classify_law() {
        let dir = scratch_dir("classify");
        // June home file carrying: one June row, one July row TWICE (a dup
        // that is ALSO foreign) — physical 3, unique 2, foreign 2.
        let path = write_parquet(
            &dir,
            "06.parquet",
            &ts_frame(
                &[Some(JUNE_US), Some(JULY_US), Some(JULY_US)],
                Some(TimeZone::UTC),
            ),
        );
        let scan = reconcile_file_impl(&path, Some((2024, 6)))
            .unwrap()
            .unwrap();
        assert_eq!(scan.dup, Some((3, 2)));
        assert_eq!(scan.foreign_rows, 2);
        // classify OFF: same span, no counts.
        let off = reconcile_file_impl(&path, None).unwrap().unwrap();
        assert_eq!(off.dup, None);
        assert_eq!(off.foreign_rows, 0);
        assert_eq!(off.row_count, 3);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn null_timestamp_counts_one_distinct_and_abstains_from_foreign() {
        let dir = scratch_dir("nulls");
        // physical 3, distinct {JUNE, null} = 2 (the polars n_unique law),
        // foreign 0 (null abstains).
        let path = write_parquet(
            &dir,
            "06.parquet",
            &ts_frame(&[Some(JUNE_US), Some(JUNE_US), None], Some(TimeZone::UTC)),
        );
        let scan = reconcile_file_impl(&path, Some((2024, 6)))
            .unwrap()
            .unwrap();
        assert_eq!(scan.row_count, 3);
        assert_eq!(scan.dup, Some((3, 2)));
        assert_eq!(scan.foreign_rows, 0);
        assert_eq!(scan.first_ts_us, JUNE_US);
        assert_eq!(scan.last_ts_us, JUNE_US);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn naive_column_reports_tz_unaware() {
        let dir = scratch_dir("naive");
        let path = write_parquet(&dir, "06.parquet", &ts_frame(&[Some(JUNE_US)], None));
        let scan = reconcile_file_impl(&path, Some((2024, 6)))
            .unwrap()
            .unwrap();
        assert!(!scan.tz_aware);
        assert_eq!(scan.foreign_rows, 0, "naive routes as-if-UTC");
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn month_boundary_routes_by_utc_civil_month() {
        let dir = scratch_dir("boundary");
        // 2024-06-30T23:59:59.999999Z is June; +1µs is July.
        let last_june_us = 1_719_791_999_999_999; // 2024-06-30 23:59:59.999999 UTC
        let first_july_us = last_june_us + 1;
        let path = write_parquet(
            &dir,
            "06.parquet",
            &ts_frame(
                &[Some(last_june_us), Some(first_july_us)],
                Some(TimeZone::UTC),
            ),
        );
        let scan = reconcile_file_impl(&path, Some((2024, 6)))
            .unwrap()
            .unwrap();
        assert_eq!(scan.foreign_rows, 1, "exactly the first-July µs is foreign");
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn out_of_range_timestamp_errors_out() {
        let dir = scratch_dir("oor");
        let path = write_parquet(
            &dir,
            "06.parquet",
            &ts_frame(&[Some(JUNE_US), Some(1 << 62)], Some(TimeZone::UTC)),
        );
        let err = reconcile_file_impl(&path, Some((2024, 6))).unwrap_err();
        assert!(
            err.to_string()
                .contains("outside the representable datetime range")
        );
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn boundary_exact_at_datetime_min_max_passes() {
        let dir = scratch_dir("bounds");
        let path = write_parquet(
            &dir,
            "06.parquet",
            &ts_frame(
                &[Some(DATETIME_MIN_US), Some(DATETIME_MAX_US)],
                Some(TimeZone::UTC),
            ),
        );
        let scan = reconcile_file_impl(&path, None).unwrap().unwrap();
        assert_eq!(scan.first_ts_us, DATETIME_MIN_US);
        assert_eq!(scan.last_ts_us, DATETIME_MAX_US);
        let over = write_parquet(
            &dir,
            "07.parquet",
            &ts_frame(&[Some(DATETIME_MAX_US + 1)], Some(TimeZone::UTC)),
        );
        assert!(reconcile_file_impl(&over, None).is_err());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn envelope_refusals_error_out() {
        let dir = scratch_dir("envelope");
        // Unreadable garbage bytes.
        let garbage = dir.join("garbage.parquet");
        std::fs::write(&garbage, b"not a parquet file").unwrap();
        assert!(reconcile_file_impl(&garbage.to_string_lossy(), None).is_err());
        // Absent file.
        assert!(reconcile_file_impl(&dir.join("missing.parquet").to_string_lossy(), None).is_err());
        // All-null timestamp column.
        let all_null = write_parquet(
            &dir,
            "null.parquet",
            &ts_frame(&[None, None], Some(TimeZone::UTC)),
        );
        assert!(reconcile_file_impl(&all_null, None).is_err());
        // A non-Datetime "timestamp" dtype (an i64 hand-plant): Python's
        // min() would yield an int the shell's _as_utc refuses — the crate
        // must degrade so the oracle owns that verdict.
        let mut int_ts = DataFrame::new_infer_height(vec![
            Column::new("timestamp".into(), vec![1i64, 2]),
            Column::new("close".into(), vec![100.0f64, 101.0]),
        ])
        .unwrap();
        let int_path = dir.join("int.parquet");
        let file = std::fs::File::create(&int_path).unwrap();
        ParquetWriter::new(file).finish(&mut int_ts).unwrap();
        assert!(reconcile_file_impl(&int_path.to_string_lossy(), None).is_err());
        // A missing timestamp column entirely.
        let mut no_ts =
            DataFrame::new_infer_height(vec![Column::new("close".into(), vec![100.0f64])]).unwrap();
        let no_ts_path = dir.join("nots.parquet");
        let file = std::fs::File::create(&no_ts_path).unwrap();
        ParquetWriter::new(file).finish(&mut no_ts).unwrap();
        assert!(reconcile_file_impl(&no_ts_path.to_string_lossy(), None).is_err());
        // A non-us unit (ms) — outside the envelope, degrades.
        let ms = Series::new("timestamp".into(), vec![Some(JUNE_US / 1_000)])
            .cast(&DataType::Datetime(
                TimeUnit::Milliseconds,
                Some(TimeZone::UTC),
            ))
            .unwrap();
        let mut ms_df = DataFrame::new_infer_height(vec![ms.into()]).unwrap();
        let ms_path = dir.join("ms.parquet");
        let file = std::fs::File::create(&ms_path).unwrap();
        ParquetWriter::new(file).finish(&mut ms_df).unwrap();
        assert!(reconcile_file_impl(&ms_path.to_string_lossy(), None).is_err());
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
