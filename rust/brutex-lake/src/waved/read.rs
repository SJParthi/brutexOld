//! # Wave-D W-1 — the `timeframe_materialize` 1-minute read core
//!
//! Ports the body of `brutex/data/timeframe_materialize.py::make_lake_read_1m._read`
//! (enumeration → collect → rename/select/sort → ts-datetime-range gate →
//! price-sanity gate → bad-tick screen) behind the default-OFF
//! `BRUTEX_LAKE_WAVED_RUST` facade (`brutex/data/lake_rust_waved.py`), plus
//! the `has_stored_1m_bars` / `month_partition_files` drift-pinned twins.
//!
//! ## Split honored (Wave-D design §B1)
//!
//! * The months / whole-tree / empty-partition SOURCE DECISION — and its
//!   pinned raises (`NoStored1mBarsError`, the months-arm plain
//!   `ValueError`) — stays PYTHON: the shell resolves the source first and
//!   passes EITHER an explicit file list (the months arm, computed by the
//!   Python `month_partition_files`) OR the partition root to walk (the
//!   whole-tree arm). The `glob=False` metachar law
//!   (DEC-SPOT-DERIVE-TS-RANGE-HARDENING-001) holds BY CONSTRUCTION: this
//!   module opens exact paths, never a glob pattern.
//! * Gate VERDICTS are value-level errors here; the facade wraps ANY crate
//!   error into the ONE typed quarantining `ResampleInputError` (the W-6a
//!   endgame law — the class already sits in the prebuild's per-symbol
//!   quarantine set, so a poisoned symbol isolates and the run continues;
//!   the Python read oracle is deleted).
//! * The bad-tick WARN (`feed.bad_tick_skipped`) stays PYTHON: this module
//!   returns per-session warn COORDINATES (`BadTickEvent` → one JSON
//!   string) the shell replays through `bad_tick.emit_bad_tick_warn` — the
//!   `rust/brutex-feed` `scan_bars` precedent; structlog never crosses the
//!   FFI (Wave-D house law i).
//!
//! ## Mirrored laws (load-bearing — do not "simplify")
//!
//! * ts-range bounds: the EXACT `datetime.min`/`datetime.max` epoch offsets
//!   of `price_sanity._TS_UNIT_BOUNDS` per time unit (`ms` floors, `ns`
//!   vacuous — every storable ns value is Python-representable).
//! * price sanity: the scalar `price_sanity.price_row_insane` law (null /
//!   non-finite / non-positive / broken OHLC ordering), which the repo
//!   property-pins equal to the vectorized `price_violation_expr`.
//! * bad-tick screen: `bad_tick.BadTickScreen` op-for-op — IST session key
//!   = calendar date of `ts + 05:30` (fixed integer arithmetic, the
//!   `resample.rs` law), first bar of a session always kept, a skipped
//!   spike never advances the baseline, multiplicative `>=`/`<=` boundary
//!   at ratio 4.0, one event per session with >= 1 skip (flush at each
//!   session boundary + at the end).

use std::io::ErrorKind;
use std::path::Path;

use polars::prelude::*;
use pyo3::exceptions::{PyOSError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;

use super::{BadTickEvent, InodeRefusal, bad_tick_events_json, civil_date_iso};

/// The read core's error split (D-31 — the typed inode contract,
/// DEC-LAKE-READ-INODE-GUARD-001/-002): the inode-gate refusal crosses the
/// FFI as a REAL `OSError` (the `locks.rs` errno-law sibling — the ONE
/// `OSError` the read pyfunction can raise), so the facade restores the
/// typed `NotRegularFileError` the pre-Wave-D reader raised; EVERY other
/// refusal stays the `ValueError`-bound polars class the facade wraps into
/// the typed quarantining `ResampleInputError`.
#[derive(Debug)]
pub enum ReadCoreError {
    /// A path in the read set is not a regular file (a planted FIFO /
    /// device / directory) — payload = the full
    /// `cannot open <path>: not a regular file (file type …)` rendering.
    NotRegularFile(String),
    /// Every other read/gate refusal (corrupt file, gate violation, …).
    Polars(PolarsError),
}

impl From<PolarsError> for ReadCoreError {
    fn from(err: PolarsError) -> Self {
        ReadCoreError::Polars(err)
    }
}

impl std::fmt::Display for ReadCoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ReadCoreError::NotRegularFile(msg) => f.write_str(msg),
            ReadCoreError::Polars(err) => write!(f, "{err}"),
        }
    }
}

/// The bounded bad-tick multiple — the twin of
/// `brutex/data/bad_tick.py::BAD_TICK_MAX_RATIO` (drift-pinned by the
/// facade suite via the event payload's `max_ratio` field).
pub const WAVED_BAD_TICK_MAX_RATIO: f64 = 4.0;

/// Exact epoch offset (microseconds) of Python `datetime.min`
/// (0001-01-01T00:00:00) — `price_sanity._DATETIME_MIN_US`.
pub const DATETIME_MIN_US: i64 = -62_135_596_800_000_000;

/// Exact epoch offset (microseconds) of Python `datetime.max`
/// (9999-12-31T23:59:59.999999) — `price_sanity._DATETIME_MAX_US`.
pub const DATETIME_MAX_US: i64 = 253_402_300_799_999_999;

/// Per-second physical ticks for a datetime time unit (`resample.rs` twin —
/// kept local so the waved tree stays additive to the hub).
fn ticks_per_second(unit: TimeUnit) -> i64 {
    match unit {
        TimeUnit::Milliseconds => 1_000,
        TimeUnit::Microseconds => 1_000_000,
        TimeUnit::Nanoseconds => 1_000_000_000,
    }
}

/// Encode a DataFrame to uncompressed Arrow IPC bytes (`resample.rs` idiom).
pub(super) fn frame_to_ipc(df: &mut DataFrame) -> PolarsResult<Vec<u8>> {
    let mut buf: Vec<u8> = Vec::new();
    IpcWriter::new(&mut buf).finish(df)?;
    Ok(buf)
}

/// Collect every `*.parquet`-named ENTRY under *root* (recursive), ascending
/// lexicographic — the whole-tree arm's enumeration. A `*.parquet`-named
/// entry is COLLECTED whatever its inode type (file / directory / FIFO /
/// symlink): the per-file inode gate downstream owns the regular-file
/// verdict, so a `*.parquet`-named DIRECTORY (the out-of-band dataset-dir
/// plant) refuses loudly with the typed `NotRegularFile` class (D-32 —
/// restoring the pre-lane loud posture) instead of being descended into and
/// silently serving its member files. Ordinary (non-`*.parquet`-named)
/// directories — year dirs — descend unchanged. Walk errors surface (the
/// facade wraps them into the typed quarantining `ResampleInputError` —
/// the W-6a endgame law).
fn collect_tree_parquet_files(root: &Path) -> std::io::Result<Vec<String>> {
    let mut out: Vec<String> = Vec::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        for entry in std::fs::read_dir(&dir)? {
            let entry = entry?;
            let path = entry.path();
            if entry.file_name().to_string_lossy().ends_with(".parquet") {
                out.push(path.to_string_lossy().into_owned());
            } else if path.is_dir() {
                stack.push(path);
            }
        }
    }
    out.sort();
    Ok(out)
}

/// The `month_partition_files` twin (drift-pinned; pure): the EXISTING
/// `<base>/<yyyy>/<mm>.parquet` files for the requested months —
/// de-duplicated, ascending, absent months skipped cleanly.
pub fn month_partition_files_impl(base: &str, months: &[(i32, u32)]) -> Vec<String> {
    let unique: std::collections::BTreeSet<(i32, u32)> = months.iter().copied().collect();
    let mut out: Vec<String> = Vec::new();
    for (year, month) in unique {
        let candidate = Path::new(base)
            .join(format!("{year:04}"))
            .join(format!("{month:02}.parquet"));
        if candidate.is_file() {
            out.push(candidate.to_string_lossy().into_owned());
        }
    }
    out
}

/// Recursive walk arm of [`has_stored_1m_bars_impl`]: mirrors
/// `os.walk(onerror=raise)` — every level's entry NAMES are checked (file
/// OR directory, the `rglob`-parity law), symlinked dirs are listed but
/// never descended (`followlinks=False`), and ANY enumeration fault
/// propagates as the real `io::Error` (never read as "empty").
fn has_stored_walk(dir: &Path) -> std::io::Result<bool> {
    let mut subdirs: Vec<std::path::PathBuf> = Vec::new();
    for entry in std::fs::read_dir(dir)? {
        let entry = entry?;
        if entry.file_name().to_string_lossy().ends_with(".parquet") {
            return Ok(true);
        }
        if entry.file_type()?.is_dir() {
            subdirs.push(entry.path());
        }
    }
    for sub in subdirs {
        if has_stored_walk(&sub)? {
            return Ok(true);
        }
    }
    Ok(false)
}

/// The `has_stored_1m_bars` twin (drift-pinned): `true` iff the partition
/// holds >= 1 `*.parquet`-named entry. An absent base / a non-directory
/// at-or-inside the path reads `false` (the benign shape); every OTHER
/// `io::Error` (e.g. `EACCES`) propagates so the caller raises the real
/// `OSError` — the refuter-round-4 fail-loud law.
pub fn has_stored_1m_bars_impl(base: &str) -> std::io::Result<bool> {
    let meta = match std::fs::metadata(base) {
        Ok(meta) => meta,
        Err(err)
            if err.kind() == ErrorKind::NotFound || err.raw_os_error() == Some(libc::ENOTDIR) =>
        {
            return Ok(false);
        }
        Err(err) => return Err(err),
    };
    if !meta.is_dir() {
        return Ok(false);
    }
    has_stored_walk(Path::new(base))
}

/// The ts-datetime-range gate — `price_sanity.validate_frame_ts_datetime_range`
/// value-law twin: refuse a physical timestamp outside the
/// Python-representable datetime range for the column's unit. `ms` floors
/// `datetime.max` (exact for `datetime.min` — a whole day); `ns` is
/// vacuously in range; a non-`Datetime` dtype / all-null column returns
/// clean (the Python gate's own out-of-band skip arms).
fn ts_range_gate(df: &DataFrame) -> PolarsResult<()> {
    if df.height() == 0 {
        return Ok(());
    }
    let ts = df.column("ts_utc")?;
    let DataType::Datetime(unit, _) = ts.dtype() else {
        return Ok(());
    };
    let (low, high) = match unit {
        TimeUnit::Microseconds => (DATETIME_MIN_US, DATETIME_MAX_US),
        // Exact-floor law: MIN is divisible by 1_000 (a whole day), MAX
        // floors to the last whole millisecond — Python `// 1_000` twin
        // (Rust `/` truncates toward zero, equal here because MIN divides
        // exactly and MAX is positive).
        TimeUnit::Milliseconds => (DATETIME_MIN_US / 1_000, DATETIME_MAX_US / 1_000),
        // Every storable ns i64 is Python-representable — vacuous.
        TimeUnit::Nanoseconds => return Ok(()),
    };
    let phys = ts.to_physical_repr();
    let phys = phys.i64()?;
    let (Some(ts_min), Some(ts_max)) = (phys.min(), phys.max()) else {
        return Ok(()); // all-null — the resampler's own null guard owns that class
    };
    if low <= ts_min && ts_max <= high {
        return Ok(());
    }
    Err(polars_err!(
        ComputeError:
        "stored bars carry timestamp(s) outside the representable datetime range \
         (physical min {}, max {})",
        ts_min, ts_max
    ))
}

/// The price-sanity gate — the scalar `price_sanity.price_row_insane` law
/// per row (the repo property-pins the scalar twin equal to the vectorized
/// `price_violation_expr` over the full float plane): any null /
/// non-finite / non-positive price, or broken OHLC ordering, refuses the
/// whole frame. Expects Float64 price columns (the lake's only stored
/// price dtype; anything else errors → the facade's typed quarantining
/// `ResampleInputError`).
fn price_gate(df: &DataFrame) -> PolarsResult<()> {
    if df.height() == 0 {
        return Ok(());
    }
    let open = df.column("open")?.f64()?;
    let high = df.column("high")?.f64()?;
    let low = df.column("low")?.f64()?;
    let close = df.column("close")?.f64()?;
    let mut violations: usize = 0;
    for i in 0..df.height() {
        let insane = match (open.get(i), high.get(i), low.get(i), close.get(i)) {
            (Some(o), Some(h), Some(l), Some(c)) => {
                !o.is_finite()
                    || !h.is_finite()
                    || !l.is_finite()
                    || !c.is_finite()
                    || o <= 0.0
                    || h <= 0.0
                    || l <= 0.0
                    || c <= 0.0
                    || h < l
                    || o > h
                    || o < l
                    || c > h
                    || c < l
            }
            _ => true,
        };
        if insane {
            violations += 1;
        }
    }
    if violations == 0 {
        return Ok(());
    }
    Err(polars_err!(
        ComputeError:
        "stored 1-minute bars carry {} price-insane row(s) \
         (non-finite or non-positive price, or broken OHLC ordering)",
        violations
    ))
}

/// The bad-tick screen — `bad_tick.BadTickScreen` op-for-op over the sorted
/// frame (module docs): returns the kept frame (the INPUT frame when
/// nothing dropped) + the per-session warn events. Runs AFTER the sanity
/// gates (closes are non-null, finite, positive by then; the defensive
/// null/dtype arms error → the facade's typed quarantining
/// `ResampleInputError`).
fn screen_bad_ticks(df: DataFrame) -> PolarsResult<(DataFrame, Vec<BadTickEvent>)> {
    if df.height() == 0 {
        return Ok((df, Vec::new()));
    }
    let ts = df.column("ts_utc")?;
    let DataType::Datetime(unit, _) = ts.dtype() else {
        return Err(
            polars_err!(ComputeError: "ts_utc must be a Datetime column, got {}", ts.dtype()),
        );
    };
    let per_second = ticks_per_second(*unit);
    let per_day = 86_400 * per_second;
    let ist_offset = 19_800 * per_second; // +05:30 — the fixed-arithmetic IST law
    let phys = ts.to_physical_repr();
    let phys = phys.i64()?;
    let closes = df.column("close")?.f64()?;

    let mut keep: Vec<bool> = Vec::with_capacity(df.height());
    let mut events: Vec<BadTickEvent> = Vec::new();
    let mut session: Option<i64> = None;
    let mut prev_close: Option<f64> = None;
    let mut skipped: u64 = 0;
    let mut first_offender: Option<(f64, f64)> = None;

    fn flush(
        events: &mut Vec<BadTickEvent>,
        session: Option<i64>,
        skipped: u64,
        first_offender: Option<(f64, f64)>,
    ) {
        if skipped > 0
            && let Some((first_prev_close, first_close)) = first_offender
            && let Some(day) = session
        {
            events.push(BadTickEvent {
                session_iso: civil_date_iso(day),
                skipped,
                max_ratio: WAVED_BAD_TICK_MAX_RATIO,
                first_prev_close,
                first_close,
            });
        }
    }

    for i in 0..df.height() {
        let (Some(value), Some(close)) = (phys.get(i), closes.get(i)) else {
            return Err(polars_err!(
                ComputeError: "null ts_utc/close at row {} — cannot screen bad ticks", i
            ));
        };
        // `checked_add`: on the ns unit EVERY i64 is datetime-representable
        // (the #1919 "ns vacuously representable" clause), so a stamp near
        // i64::MAX reaches this shift — an overflow is an isolated
        // ComputeError-style refusal, never a wrap/panic (R1 fix-round).
        let Some(shifted) = value.checked_add(ist_offset) else {
            return Err(polars_err!(
                ComputeError: "ts_utc physical value {} at row {} overflows the IST shift", value, i
            ));
        };
        let day = shifted.div_euclid(per_day);
        if Some(day) != session {
            // New session: flush the warn owed for the session just left,
            // then reset — the first bar of a session is always kept.
            flush(&mut events, session, skipped, first_offender);
            session = Some(day);
            prev_close = None;
            skipped = 0;
            first_offender = None;
        }
        if let Some(prev) = prev_close
            && (close >= prev * WAVED_BAD_TICK_MAX_RATIO
                || close * WAVED_BAD_TICK_MAX_RATIO <= prev)
        {
            skipped += 1;
            if first_offender.is_none() {
                first_offender = Some((prev, close));
            }
            // Deliberately do NOT advance prev_close: the next bar is
            // judged against the last KEPT good close (baseline-hold).
            keep.push(false);
            continue;
        }
        prev_close = Some(close);
        keep.push(true);
    }
    flush(&mut events, session, skipped, first_offender);

    if keep.iter().all(|kept| *kept) {
        return Ok((df, events));
    }
    let mask = BooleanChunked::from_slice("keep".into(), &keep);
    Ok((df.filter(&mask)?, events))
}

/// The read core: exact paths (months arm) OR a tree walk (whole-tree arm)
/// → per-file eager reads with schema-tolerant projection
/// (`missing_columns="insert"` / `extra_columns="ignore"` twin: absent
/// needed columns null-filled at the first-seen dtype, extras dropped) →
/// vstack → `timestamp`→`ts_utc` rename → select order → sort → gates →
/// optional screen → `(ipc_bytes, events_json)`. The inode-gate refusal is
/// the DISTINCT [`ReadCoreError::NotRegularFile`] class (D-31); everything
/// else is the [`ReadCoreError::Polars`] class.
pub fn read_1m_impl(
    files: &[String],
    whole_tree_root: Option<&str>,
    with_open_interest: bool,
    screen_enabled: bool,
) -> Result<(Vec<u8>, String), ReadCoreError> {
    let paths: Vec<String> = match whole_tree_root {
        Some(root) => {
            if !files.is_empty() {
                return Err(polars_err!(
                    ComputeError: "pass explicit files OR whole_tree_root, never both"
                )
                .into());
            }
            collect_tree_parquet_files(Path::new(root)).map_err(
                |err| polars_err!(ComputeError: "cannot walk 1-minute partition {}: {}", root, err),
            )?
        }
        None => files.to_vec(),
    };
    if paths.is_empty() {
        return Err(polars_err!(ComputeError: "no stored 1-minute parquet files to read").into());
    }

    let mut stored_columns: Vec<&str> = vec!["timestamp", "open", "high", "low", "close", "volume"];
    if with_open_interest {
        stored_columns.push("open_interest");
    }

    let mut frames: Vec<DataFrame> = Vec::with_capacity(paths.len());
    for path in &paths {
        // Inode gate BEFORE the open (DEC-LAKE-READ-INODE-GUARD-001 on the
        // Wave-D arm): a planted FIFO otherwise blocks forever in `open(2)`.
        // The inode class routes to the DISTINCT `NotRegularFile` variant
        // (D-31 — it crosses the FFI as a real `OSError` so the facade can
        // restore the typed `NotRegularFileError` contract); a stat FAILURE
        // (vanished / EACCES) stays the generic quarantining class.
        super::ensure_regular_file(std::path::Path::new(path)).map_err(
            |refusal| match refusal {
                InodeRefusal::NotRegularFile(_) => {
                    ReadCoreError::NotRegularFile(format!("cannot open {path}: {refusal}"))
                }
                InodeRefusal::Stat(_) => {
                    polars_err!(ComputeError: "cannot open {}: {}", path, refusal).into()
                }
            },
        )?;
        let file = std::fs::File::open(path)
            .map_err(|err| polars_err!(ComputeError: "cannot open {}: {}", path, err))?;
        frames.push(ParquetReader::new(file).finish()?);
    }

    // Schema-tolerant projection dtypes: the first file carrying each
    // needed column donates its dtype (the unified-schema twin); a column
    // absent from EVERY file refuses (the Python select would too).
    let mut seen_dtypes: Vec<Option<DataType>> = vec![None; stored_columns.len()];
    for frame in &frames {
        for (i, name) in stored_columns.iter().enumerate() {
            if seen_dtypes[i].is_none()
                && let Ok(column) = frame.column(name)
            {
                seen_dtypes[i] = Some(column.dtype().clone());
            }
        }
    }
    let mut dtypes: Vec<DataType> = Vec::with_capacity(seen_dtypes.len());
    for (i, dtype) in seen_dtypes.into_iter().enumerate() {
        match dtype {
            Some(dtype) => dtypes.push(dtype),
            None => {
                return Err(polars_err!(
                    ColumnNotFound: "column {} missing from every stored 1-minute file",
                    stored_columns[i]
                )
                .into());
            }
        }
    }

    let mut acc: Option<DataFrame> = None;
    for frame in frames {
        let mut columns: Vec<Column> = Vec::with_capacity(stored_columns.len());
        for (i, name) in stored_columns.iter().enumerate() {
            match frame.column(name) {
                Ok(column) => columns.push(column.clone()),
                Err(_) => {
                    columns.push(Column::full_null(
                        (*name).into(),
                        frame.height(),
                        &dtypes[i],
                    ));
                }
            }
        }
        let projected = DataFrame::new_infer_height(columns)?;
        match &mut acc {
            None => acc = Some(projected),
            Some(combined) => {
                combined.vstack_mut(&projected)?;
            }
        }
    }
    let mut df = match acc {
        Some(df) => df,
        None => return Err(polars_err!(ComputeError: "no frames collected").into()),
    };
    df.rename("timestamp", "ts_utc".into())?;
    let df = df.sort(["ts_utc"], SortMultipleOptions::default())?;

    ts_range_gate(&df)?;
    price_gate(&df)?;
    let (mut df, events) = if screen_enabled {
        screen_bad_ticks(df)?
    } else {
        (df, Vec::new())
    };
    let bytes = frame_to_ipc(&mut df)?;
    Ok((bytes, bad_tick_events_json(&events)))
}

/// Read a symbol's stored 1-minute bars (exact month files OR a partition
/// tree walk) through the ts-range + price-sanity gates and the optional
/// bad-tick screen. Returns `(ipc_bytes, events_json)` — the sorted frame
/// as uncompressed Arrow IPC plus the per-session bad-tick warn events the
/// shell replays.
///
/// Raises `OSError` for the inode-gate refusal ONLY (a non-regular inode —
/// planted FIFO / device / directory — in the read set; the `locks.rs`
/// real-`OSError` channel, D-31: the facade maps it back to the typed
/// `NotRegularFileError` of DEC-LAKE-READ-INODE-GUARD-001/-002),
/// `ValueError` on every OTHER read/gate refusal (the facade wraps it into
/// the typed quarantining `ResampleInputError` — the W-6a endgame law), and
/// `RuntimeError` if the compute — or the result construction — panics
/// (both caught at this boundary; a panic never crosses the FFI). Runs
/// without the GIL (`Python::detach` — the inputs are owned values).
#[pyfunction]
pub(crate) fn waved_read_1m_ipc(
    py: Python<'_>,
    files: Vec<String>,
    whole_tree_root: Option<String>,
    with_open_interest: bool,
    screen_enabled: bool,
) -> PyResult<(Py<PyBytes>, String)> {
    let result = py.detach(|| {
        std::panic::catch_unwind(|| {
            read_1m_impl(
                &files,
                whole_tree_root.as_deref(),
                with_open_interest,
                screen_enabled,
            )
        })
    });
    let (bytes, events_json) = match result {
        Ok(Ok(payload)) => payload,
        Ok(Err(ReadCoreError::NotRegularFile(msg))) => {
            // D-31: the inode class crosses as a REAL `OSError` — the ONE
            // OSError this call raises — message verbatim, no prefix (the
            // facade re-raises it as the typed `NotRegularFileError`).
            return Err(PyOSError::new_err(msg));
        }
        Ok(Err(err)) => {
            return Err(PyValueError::new_err(format!(
                "brutex-lake waved read failed: {err}"
            )));
        }
        Err(_) => {
            return Err(PyRuntimeError::new_err(
                "brutex-lake waved read panicked (caught at the FFI boundary)",
            ));
        }
    };
    let py_bytes = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        PyBytes::new(py, &bytes).unbind()
    }))
    .map_err(|_| {
        PyRuntimeError::new_err(
            "brutex-lake waved read result construction panicked (caught at the FFI boundary)",
        )
    })?;
    Ok((py_bytes, events_json))
}

/// The `has_stored_1m_bars` drift-pinned twin: `true` iff the partition
/// holds >= 1 `*.parquet`-named entry (file OR directory). An absent /
/// non-directory base reads `false`; any other enumeration fault raises
/// the REAL `OSError` with its errno intact (the `locks.rs` errno law) —
/// never a fabricated "empty".
#[pyfunction]
pub(crate) fn waved_has_stored_1m_bars(py: Python<'_>, base: String) -> PyResult<bool> {
    let result = py.detach(|| std::panic::catch_unwind(|| has_stored_1m_bars_impl(&base)));
    match result {
        Ok(Ok(found)) => Ok(found),
        Ok(Err(err)) => Err(err.into()),
        Err(_) => Err(PyRuntimeError::new_err(
            "brutex-lake waved walk panicked (caught at the FFI boundary)",
        )),
    }
}

/// The `month_partition_files` drift-pinned twin: the EXISTING
/// `<base>/<yyyy>/<mm>.parquet` files for the requested `(year, month)`
/// pairs — de-duplicated, ascending POSIX paths, absent months skipped.
#[pyfunction]
pub(crate) fn waved_month_partition_files(
    py: Python<'_>,
    base: String,
    months: Vec<(i32, u32)>,
) -> PyResult<Vec<String>> {
    let result =
        py.detach(|| std::panic::catch_unwind(|| month_partition_files_impl(&base, &months)));
    result.map_err(|_| {
        PyRuntimeError::new_err(
            "brutex-lake waved month enumeration panicked (caught at the FFI boundary)",
        )
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A per-test unique scratch dir (no tempfile dev-dep — std only, the
    /// `locks.rs` idiom).
    fn scratch_dir(tag: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "brutex-lake-waved-{tag}-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// 2024-01-01 09:15 IST == 03:45 UTC, epoch microseconds (the
    /// `resample.rs` anchor).
    const OPEN_0915_UTC_US: i64 = 1_704_080_700_000_000;
    const MINUTE_US: i64 = 60_000_000;
    const DAY_US: i64 = 86_400_000_000;

    /// Build a stored-shape frame (`timestamp` column name — pre-rename) at
    /// explicit UTC-microsecond instants; every bar's OHLC collapses to its
    /// close (self-consistent ordering by construction).
    fn stored_frame(ts_us: &[i64], closes: &[f64], with_oi: bool) -> DataFrame {
        let ts = Series::new("timestamp".into(), ts_us.to_vec())
            .cast(&DataType::Datetime(
                TimeUnit::Microseconds,
                Some(TimeZone::UTC),
            ))
            .unwrap();
        let volumes: Vec<i64> = (0..ts_us.len()).map(|i| 10 + i as i64).collect();
        let mut cols = vec![
            ts.into(),
            Column::new("open".into(), closes.to_vec()),
            Column::new("high".into(), closes.to_vec()),
            Column::new("low".into(), closes.to_vec()),
            Column::new("close".into(), closes.to_vec()),
            Column::new("volume".into(), volumes),
        ];
        if with_oi {
            let oi: Vec<i64> = (0..ts_us.len()).map(|i| 1_000 + i as i64).collect();
            cols.push(Column::new("open_interest".into(), oi));
        }
        DataFrame::new_infer_height(cols).unwrap()
    }

    fn write_parquet(dir: &Path, rel: &str, df: &DataFrame) -> String {
        let path = dir.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let file = std::fs::File::create(&path).unwrap();
        ParquetWriter::new(file).finish(&mut df.clone()).unwrap();
        path.to_string_lossy().into_owned()
    }

    fn frame_from_ipc(bytes: &[u8]) -> DataFrame {
        IpcReader::new(std::io::Cursor::new(bytes))
            .finish()
            .unwrap()
    }

    #[test]
    fn month_twin_returns_existing_sorted_deduped() {
        let dir = scratch_dir("months");
        write_parquet(
            &dir,
            "2020/03.parquet",
            &stored_frame(&[OPEN_0915_UTC_US], &[100.0], false),
        );
        write_parquet(
            &dir,
            "2020/01.parquet",
            &stored_frame(&[OPEN_0915_UTC_US], &[100.0], false),
        );
        let base = dir.to_string_lossy().into_owned();
        let out = month_partition_files_impl(&base, &[(2020, 3), (2020, 1), (2020, 1), (2021, 5)]);
        assert_eq!(
            out,
            vec![
                format!("{base}/2020/01.parquet"),
                format!("{base}/2020/03.parquet"),
            ]
        );
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn has_stored_twin_truth_table() {
        let dir = scratch_dir("hasstored");
        let base = dir.join("1minute");
        // Absent base -> false.
        assert!(!has_stored_1m_bars_impl(&base.to_string_lossy()).unwrap());
        // Empty dir -> false.
        std::fs::create_dir_all(&base).unwrap();
        assert!(!has_stored_1m_bars_impl(&base.to_string_lossy()).unwrap());
        // Nested parquet FILE -> true.
        std::fs::create_dir_all(base.join("2020")).unwrap();
        std::fs::write(base.join("2020/01.parquet"), b"x").unwrap();
        assert!(has_stored_1m_bars_impl(&base.to_string_lossy()).unwrap());
        // Parquet-named DIRECTORY counts too (rglob-parity).
        let dir_plant = dir.join("plant/1minute");
        std::fs::create_dir_all(dir_plant.join("2020/01.parquet")).unwrap();
        assert!(has_stored_1m_bars_impl(&dir_plant.to_string_lossy()).unwrap());
        // A FILE at the base path -> false (nothing can be stored under it).
        let file_base = dir.join("filebase");
        std::fs::write(&file_base, b"x").unwrap();
        assert!(!has_stored_1m_bars_impl(&file_base.to_string_lossy()).unwrap());
        // A path THROUGH a file -> ENOTDIR -> false (the benign shape).
        assert!(!has_stored_1m_bars_impl(&file_base.join("nested").to_string_lossy()).unwrap());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn read_concats_renames_sorts_across_files() {
        let dir = scratch_dir("read");
        // Two month files written OUT of chronological order.
        let later = write_parquet(
            &dir,
            "2024/02.parquet",
            &stored_frame(&[OPEN_0915_UTC_US + 31 * DAY_US], &[200.0], false),
        );
        let earlier = write_parquet(
            &dir,
            "2024/01.parquet",
            &stored_frame(
                &[OPEN_0915_UTC_US, OPEN_0915_UTC_US + MINUTE_US],
                &[100.0, 101.0],
                false,
            ),
        );
        let (bytes, events) = read_1m_impl(&[later, earlier], None, false, true).unwrap();
        assert_eq!(events, "[]");
        let df = frame_from_ipc(&bytes);
        assert_eq!(
            df.get_column_names(),
            ["ts_utc", "open", "high", "low", "close", "volume"]
        );
        let closes: Vec<f64> = df
            .column("close")
            .unwrap()
            .f64()
            .unwrap()
            .into_no_null_iter()
            .collect();
        assert_eq!(
            closes,
            vec![100.0, 101.0, 200.0],
            "sorted by ts across files"
        );
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn whole_tree_walk_reads_nested_files() {
        let dir = scratch_dir("tree");
        write_parquet(
            &dir,
            "2024/01.parquet",
            &stored_frame(&[OPEN_0915_UTC_US], &[100.0], false),
        );
        write_parquet(
            &dir,
            "2024/02.parquet",
            &stored_frame(&[OPEN_0915_UTC_US + 31 * DAY_US], &[105.0], false),
        );
        let (bytes, _) = read_1m_impl(&[], Some(&dir.to_string_lossy()), false, true).unwrap();
        assert_eq!(frame_from_ipc(&bytes).height(), 2);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn missing_oi_column_null_filled_when_requested() {
        let dir = scratch_dir("mixedoi");
        let with_oi = write_parquet(
            &dir,
            "2024/01.parquet",
            &stored_frame(&[OPEN_0915_UTC_US], &[100.0], true),
        );
        let without_oi = write_parquet(
            &dir,
            "2024/02.parquet",
            &stored_frame(&[OPEN_0915_UTC_US + 31 * DAY_US], &[105.0], false),
        );
        let (bytes, _) = read_1m_impl(&[with_oi, without_oi], None, true, true).unwrap();
        let df = frame_from_ipc(&bytes);
        assert_eq!(
            df.get_column_names(),
            [
                "ts_utc",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "open_interest"
            ]
        );
        let oi = df.column("open_interest").unwrap();
        assert_eq!(
            oi.null_count(),
            1,
            "absent column null-filled, never fabricated"
        );
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn extra_columns_dropped_from_enriched_files() {
        let dir = scratch_dir("extra");
        let mut enriched = stored_frame(&[OPEN_0915_UTC_US], &[100.0], true);
        enriched
            .with_column(Column::new("iv".into(), vec![0.2f64]))
            .unwrap();
        let path = write_parquet(&dir, "2024/01.parquet", &enriched);
        let (bytes, _) = read_1m_impl(&[path], None, false, true).unwrap();
        assert_eq!(
            frame_from_ipc(&bytes).get_column_names(),
            ["ts_utc", "open", "high", "low", "close", "volume"]
        );
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn ts_range_gate_refuses_out_of_range_stamp() {
        let dir = scratch_dir("tsrange");
        let path = write_parquet(
            &dir,
            "2024/01.parquet",
            &stored_frame(&[OPEN_0915_UTC_US, 1 << 62], &[100.0, 101.0], false),
        );
        let err = read_1m_impl(&[path], None, false, true).unwrap_err();
        assert!(
            err.to_string()
                .contains("outside the representable datetime range")
        );
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn ts_range_gate_boundary_exact_at_datetime_min_max() {
        let dir = scratch_dir("tsbound");
        let path = write_parquet(
            &dir,
            "2024/01.parquet",
            &stored_frame(&[DATETIME_MIN_US, DATETIME_MAX_US], &[100.0, 101.0], false),
        );
        assert!(read_1m_impl(std::slice::from_ref(&path), None, false, false).is_ok());
        let over = write_parquet(
            &dir,
            "2024/02.parquet",
            &stored_frame(&[DATETIME_MAX_US + 1], &[100.0], false),
        );
        assert!(read_1m_impl(&[over], None, false, false).is_err());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn price_gate_refuses_insane_rows() {
        let dir = scratch_dir("price");
        // high < low: hand-build a broken-ordering frame.
        let mut df = stored_frame(&[OPEN_0915_UTC_US], &[100.0], false);
        df.replace("high", Series::new("high".into(), vec![90.0f64]).into())
            .unwrap();
        df.replace("low", Series::new("low".into(), vec![95.0f64]).into())
            .unwrap();
        let path = write_parquet(&dir, "2024/01.parquet", &df);
        let err = read_1m_impl(&[path], None, false, true).unwrap_err();
        assert!(err.to_string().contains("1 price-insane row(s)"));
        // Non-finite close.
        let mut nan_df = stored_frame(&[OPEN_0915_UTC_US], &[100.0], false);
        nan_df
            .replace("close", Series::new("close".into(), vec![f64::NAN]).into())
            .unwrap();
        let nan_path = write_parquet(&dir, "2024/02.parquet", &nan_df);
        assert!(read_1m_impl(&[nan_path], None, false, true).is_err());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn bad_tick_screen_drops_spike_and_reports_session_event() {
        let dir = scratch_dir("screen");
        // 100 -> 1000 (x10 spike, dropped) -> 101 (kept vs baseline 100).
        let path = write_parquet(
            &dir,
            "2024/01.parquet",
            &stored_frame(
                &[
                    OPEN_0915_UTC_US,
                    OPEN_0915_UTC_US + MINUTE_US,
                    OPEN_0915_UTC_US + 2 * MINUTE_US,
                ],
                &[100.0, 1_000.0, 101.0],
                false,
            ),
        );
        let (bytes, events) = read_1m_impl(std::slice::from_ref(&path), None, false, true).unwrap();
        let df = frame_from_ipc(&bytes);
        assert_eq!(df.height(), 2, "spike dropped, healthy successor kept");
        assert_eq!(
            events,
            "[{\"session\":\"2024-01-01\",\"skipped\":1,\"max_ratio\":4.0,\
             \"first_prev_close\":100.0,\"first_close\":1000.0}]"
        );
        // Screen OFF keeps the spike and reports no events.
        let (bytes_off, events_off) = read_1m_impl(&[path], None, false, false).unwrap();
        assert_eq!(frame_from_ipc(&bytes_off).height(), 3);
        assert_eq!(events_off, "[]");
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn bad_tick_first_bar_of_new_session_exempt() {
        let dir = scratch_dir("session");
        // Day 1 close 100; day 2 opens at 1000 (a 10x gap ACROSS sessions —
        // never screened; prev_close resets per session).
        let path = write_parquet(
            &dir,
            "2024/01.parquet",
            &stored_frame(
                &[OPEN_0915_UTC_US, OPEN_0915_UTC_US + DAY_US],
                &[100.0, 1_000.0],
                false,
            ),
        );
        let (bytes, events) = read_1m_impl(&[path], None, false, true).unwrap();
        assert_eq!(frame_from_ipc(&bytes).height(), 2);
        assert_eq!(events, "[]");
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn bad_tick_ns_ist_shift_overflow_is_an_isolated_compute_error() {
        // R1 fix-round: on the ns unit EVERY i64 is datetime-representable
        // (the #1919 "ns vacuously representable" clause), so a stamp near
        // i64::MAX legitimately reaches the +05:30 shift — the checked_add
        // refuses with a ComputeError instead of a debug panic/release wrap.
        let ts = Series::new("ts_utc".into(), vec![i64::MAX - 1])
            .cast(&DataType::Datetime(
                TimeUnit::Nanoseconds,
                Some(TimeZone::UTC),
            ))
            .unwrap();
        let df = DataFrame::new_infer_height(vec![
            ts.into(),
            Column::new("close".into(), vec![100.0f64]),
        ])
        .unwrap();
        // let-else (not `unwrap_err`): the Ok payload carries the
        // Debug-less `BadTickEvent`.
        let Err(err) = screen_bad_ticks(df) else {
            panic!("expected the IST-shift overflow refusal");
        };
        assert!(err.to_string().contains("overflows the IST shift"), "{err}");
        // The µs arm is untouched: an ordinary in-range µs stamp screens
        // through the same (now-checked) shift with zero events.
        let ts_us = Series::new("ts_utc".into(), vec![OPEN_0915_UTC_US])
            .cast(&DataType::Datetime(
                TimeUnit::Microseconds,
                Some(TimeZone::UTC),
            ))
            .unwrap();
        let ok = DataFrame::new_infer_height(vec![
            ts_us.into(),
            Column::new("close".into(), vec![100.0f64]),
        ])
        .unwrap();
        let (kept, events) = screen_bad_ticks(ok).unwrap();
        assert_eq!(kept.height(), 1);
        assert!(events.is_empty());
    }

    #[test]
    #[cfg(unix)]
    fn inode_gate_refusal_is_the_distinct_not_regular_class() {
        // D-31 (DEC-LAKE-READ-INODE-GUARD-001/-002): a planted FIFO in the
        // read set is the DISTINCT `NotRegularFile` class on BOTH source
        // arms (it crosses the FFI as a real `OSError`); a VANISHED listed
        // file stays the generic quarantining `Polars` class.
        let dir = scratch_dir("inodegate");
        let healthy = write_parquet(
            &dir,
            "2024/01.parquet",
            &stored_frame(&[OPEN_0915_UTC_US], &[100.0], false),
        );
        let fifo = dir.join("2024/02.parquet");
        let c_path = std::ffi::CString::new(fifo.to_string_lossy().as_bytes()).unwrap();
        #[allow(unsafe_code)] // SAFETY: `libc::mkfifo` on an owned NUL-terminated
        // path in the test scratch dir — no aliasing, rc checked below.
        let rc = unsafe { libc::mkfifo(c_path.as_ptr(), 0o600) };
        assert_eq!(rc, 0);
        // Months arm (explicit list).
        let err = read_1m_impl(
            &[healthy, fifo.to_string_lossy().into_owned()],
            None,
            false,
            true,
        )
        .unwrap_err();
        match &err {
            ReadCoreError::NotRegularFile(msg) => {
                assert!(msg.contains("not a regular file"), "{msg}");
                assert!(msg.contains("02.parquet"), "{msg}");
            }
            other => panic!("expected the NotRegularFile class, got {other:?}"),
        }
        // Whole-tree arm: the walk enumerates the FIFO → same class.
        let tree_err = read_1m_impl(&[], Some(&dir.to_string_lossy()), false, true).unwrap_err();
        assert!(
            matches!(tree_err, ReadCoreError::NotRegularFile(_)),
            "whole-tree arm must take the inode class too: {tree_err:?}"
        );
        // A VANISHED listed file is a stat FAILURE — the generic class.
        let gone = dir.join("2024/03.parquet");
        let gone_err =
            read_1m_impl(&[gone.to_string_lossy().into_owned()], None, false, true).unwrap_err();
        assert!(
            matches!(gone_err, ReadCoreError::Polars(_)),
            "a vanished file must stay the quarantining class: {gone_err:?}"
        );
        assert!(gone_err.to_string().contains("cannot stat"), "{gone_err}");
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn parquet_named_directory_refuses_on_the_whole_tree_arm() {
        // D-32: a `*.parquet`-NAMED DIRECTORY in the tree is collected as a
        // read candidate — never descended — so the inode gate refuses it
        // with the DISTINCT `NotRegularFile` class (the pre-lane loud
        // posture); its member files are NEVER silently served.
        let dir = scratch_dir("dirplant");
        write_parquet(
            &dir,
            "2024/01.parquet",
            &stored_frame(&[OPEN_0915_UTC_US], &[100.0], false),
        );
        // The dir-with-contents shape: a healthy member INSIDE the plant
        // must not be served through the walk.
        write_parquet(
            &dir,
            "2024/02.parquet/member.parquet",
            &stored_frame(&[OPEN_0915_UTC_US + 31 * DAY_US], &[105.0], false),
        );
        let err = read_1m_impl(&[], Some(&dir.to_string_lossy()), false, true).unwrap_err();
        match &err {
            ReadCoreError::NotRegularFile(msg) => {
                assert!(msg.contains("not a regular file"), "{msg}");
                assert!(msg.contains("02.parquet"), "{msg}");
            }
            other => panic!("expected the NotRegularFile class, got {other:?}"),
        }
        // An EMPTY `*.parquet`-named dir refuses identically (pre-fix it
        // was silently descended and contributed nothing).
        let empty_plant = scratch_dir("dirplant-empty");
        write_parquet(
            &empty_plant,
            "2024/01.parquet",
            &stored_frame(&[OPEN_0915_UTC_US], &[100.0], false),
        );
        std::fs::create_dir_all(empty_plant.join("2024/03.parquet")).unwrap();
        let empty_err =
            read_1m_impl(&[], Some(&empty_plant.to_string_lossy()), false, true).unwrap_err();
        assert!(
            matches!(&empty_err, ReadCoreError::NotRegularFile(msg) if msg.contains("03.parquet")),
            "empty dir plant must refuse too: {empty_err:?}"
        );
        std::fs::remove_dir_all(&dir).unwrap();
        std::fs::remove_dir_all(&empty_plant).unwrap();
    }

    #[test]
    fn empty_inputs_and_conflicting_args_are_errors() {
        assert!(read_1m_impl(&[], None, false, true).is_err());
        let err = read_1m_impl(
            &["x.parquet".to_string()],
            Some("/nonexistent-root"),
            false,
            true,
        )
        .unwrap_err();
        assert!(err.to_string().contains("never both"));
        // A missing tree root surfaces the walk error.
        assert!(read_1m_impl(&[], Some("/nonexistent-root-waved"), false, true).is_err());
    }

    #[test]
    fn zero_row_file_reads_to_zero_row_frame() {
        let dir = scratch_dir("zerorow");
        let path = write_parquet(&dir, "2024/01.parquet", &stored_frame(&[], &[], false));
        let (bytes, events) = read_1m_impl(&[path], None, false, true).unwrap();
        assert_eq!(frame_from_ipc(&bytes).height(), 0);
        assert_eq!(events, "[]");
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
