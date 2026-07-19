//! # Wave-D W-4 — the `lake_invariants` bounded scan core
//!
//! DEC-RUST-LAKE-WAVED-001, design §B4/§C-2 (`scratchpad/waved_design.md`):
//! the WHOLE `scan_lake_invariants` walk — the streaming `bars/` series walk,
//! the bars classification loop (parse → read → alien gates → dup/foreign/
//! price census → memoised canonical-sibling overlap), and the `options/`
//! tree loop (symlink probes → stem law → key-dtype/value gate →
//! dup/foreign → overlap) — moves here as ONE call per scan. The census
//! fold, `detect_legacy_drift`, the finding dataclasses, and every structlog
//! emission STAY Python: the crate returns typed finding records plus an
//! ORDERED replay-event stream (house law i — structlog stays Python; the
//! repr()-wrapping decisions of the #1594 rounds-7/8/9/16/17 log-injection
//! folds cross as per-field `*_repr` flags the shell applies).
//!
//! ## Parity laws honored (each mirrored token-for-token from
//! `brutex/data/lake_invariants.py`; the poison-corpus differential is the gate)
//!
//! * walk order: sorted directory names per level (`_child_dirs` — Python
//!   `sorted(Path)` component order == UTF-8 byte order), the fixed-depth
//!   `*/*.parquet` leaf glob (dot-names + dirs-named-`*.parquet` included),
//!   per-child stat-fault isolation, vanished-level `[]`.
//! * `parse_partition_path` twin: `resolve()`-based lake-root relativity,
//!   tail-7 shape, `int()`-parse of year/month (pure-ASCII digits natively;
//!   any OTHER shape Python's `int()` might still accept — signs,
//!   underscores, unicode digits/whitespace — DEGRADES the whole scan to the
//!   oracle rather than re-implementing CPython's grammar), vocabulary +
//!   forbidden-char + range validation (vocabularies cross the FFI as data —
//!   `VALID_INTERVALS` is registry-derived and must never be hard-coded).
//! * `glob=False` reads by construction (bytes of the exact path).
//! * the alien-schema classification (missing-column footer probe → the
//!   byte-exact `missing column(s): …` reason; the dtype gate's reasons
//!   embed the POLARS-PYTHON dtype repr via [`python_dtype_repr`] — an
//!   unmappable dtype degrades the scan, never emits a divergent reason).
//! * the out-of-range timestamp skip (`datetime_out_of_range_errors` class:
//!   physical min/max outside the per-unit Python-representable bounds).
//! * the options stem law (canonical zero-padded `YYYY-MM` only — proven
//!   equivalent to Python's isdigit→int→canonical-check chain), symlink
//!   probes at root/series/file + the canonical-sibling read, the key
//!   dtype/value gate, null-side/null-ts abstain laws.
//! * per-file pause ticks after EVERY read attempt (success, failure, and
//!   targeted canonical reads) — the L3 throttle law; `reads_performed`
//!   counts exactly the `_read_key_columns`-equivalent attempts (design
//!   §F2 — the counter the delete-time read-count pins rebind onto).
//!
//! ## Degrade-to-oracle (deviation D-2 doctrine)
//!
//! Any shape outside the byte-parity envelope errors OUT as `ValueError`
//! at the FFI and the facade re-runs the live Python oracle: non-UTF-8
//! names, `resolve()`-divergent paths (dangling/looping bars-tree
//! symlinks — `fs::canonicalize` fails where Python's `resolve(strict=False)`
//! lexically succeeds), ambiguous `int()` dir names, unmappable dtype
//! reprs, mixed-numeric-family price columns, and any filesystem fault
//! Python would CRASH on (the oracle then crashes identically — verdict
//! convergence by construction).

use std::collections::hash_map::Entry;
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};

use polars::prelude::*;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use super::civil_from_days;
use super::read::{DATETIME_MAX_US, DATETIME_MIN_US};

/// Microseconds per civil day — the UTC month-routing divisor.
const US_PER_DAY: i64 = 86_400_000_000;

/// The bars-tree scan columns (`lake_invariants.BAR_SCAN_COLUMNS`).
const BAR_SCAN_COLUMNS: [&str; 5] = ["timestamp", "open", "high", "low", "close"];

/// The options-tree key columns (`lake_invariants.UNIQUE_KEY_OPTIONS`).
const OPTION_KEY_COLUMNS: [&str; 2] = ["side", "timestamp"];

/// The two replayed log event names (the shell's `_LOG.warning` literals).
const EVENT_SCAN_SKIP: &str = "lake.invariant.scan_skip";
const EVENT_SYMLINK_SKIPPED: &str = "lake.invariant.symlink_skipped";

/// One scan failure: `Degrade` = the whole scan is outside the byte-parity
/// envelope (a `ValueError` at the FFI → the facade serves the oracle);
/// `PauseRaised` = the injected pause callback raised — the actual `PyErr`
/// is parked in the FFI shell's side slot so the core (and therefore the
/// `cargo test` binary) stays libpython-symbol-free, and the shell
/// re-raises it verbatim.
pub enum ScanFail {
    /// Whole-scan degrade with the human reason.
    Degrade(String),
    /// The Python pause callback's own exception (parked shell-side).
    PauseRaised,
}

/// The pause-tick seam — invoked after every read attempt (the L3 law).
type Pause<'a> = &'a mut dyn FnMut() -> Result<(), ScanFail>;

/// The scan configuration crossing the FFI (vocabularies are DATA — the
/// facade passes `partition.VALID_*` + `ENGINE_INDEX_COORDS`, never
/// re-minted here).
pub struct ScanConfig {
    /// The lake root as passed (paths in the payload join off it verbatim).
    pub lake_root: PathBuf,
    /// Optional bars-tree coordinate filters (`None` widens like `*`).
    pub exchange: Option<String>,
    /// See `exchange`.
    pub segment: Option<String>,
    /// See `exchange`.
    pub symbol: Option<String>,
    /// See `exchange`.
    pub interval: Option<String>,
    /// The optional `(year, month)` L2 scope bound.
    pub months: Option<HashSet<(i64, u32)>>,
    /// Scan the bars tree.
    pub include_bars: bool,
    /// Scan the options tree.
    pub include_options: bool,
    /// `partition.VALID_EXCHANGES`.
    pub valid_exchanges: HashSet<String>,
    /// `partition.VALID_SEGMENTS`.
    pub valid_segments: HashSet<String>,
    /// `partition.VALID_INTERVALS` (registry-derived — 23 labels today).
    pub valid_intervals: HashSet<String>,
    /// `partition._FORBIDDEN_SYMBOL_CHARS`.
    pub forbidden_symbol_chars: HashSet<char>,
    /// `lake_invariants.ENGINE_INDEX_COORDS`.
    pub engine_coords: HashSet<(String, String, String)>,
}

/// The six bars partition coordinates (the `PartitionCoords` wire twin).
#[derive(Clone)]
pub struct BarsCoords {
    exchange: String,
    segment: String,
    symbol: String,
    interval: String,
    year: i64,
    month: u32,
}

impl BarsCoords {
    fn engine(&self, cfg: &ScanConfig) -> bool {
        cfg.engine_coords.contains(&(
            self.exchange.clone(),
            self.segment.clone(),
            self.symbol.clone(),
        ))
    }
}

/// One finding record (the shell reconstructs the frozen dataclasses).
pub enum FindingRec {
    /// `IntraFileDup`.
    BarsDup {
        path: String,
        coords: BarsCoords,
        physical_rows: u64,
        unique_keys: u64,
    },
    /// `ForeignMonthRows`.
    BarsForeign {
        path: String,
        coords: BarsCoords,
        foreign_rows: u64,
        overlap_rows: Option<u64>,
    },
    /// `PriceSanityRows`.
    Price {
        path: String,
        coords: BarsCoords,
        bad_rows: u64,
    },
    /// `SchemaAlienFile` (bars arm — carries coords).
    AlienBars {
        path: String,
        coords: BarsCoords,
        reason: String,
    },
    /// `SchemaAlienFile` (options arm — carries the series name).
    AlienOption {
        path: String,
        series: String,
        reason: String,
    },
    /// `OptionIntraFileDup`.
    OptionDup {
        path: String,
        series: String,
        physical_rows: u64,
        unique_keys: u64,
    },
    /// `OptionForeignMonthRows`.
    OptionForeign {
        path: String,
        series: String,
        year: i64,
        month: u32,
        foreign_rows: u64,
        overlap_rows: Option<u64>,
    },
}

/// One ordered replay-stream record.
pub enum EventRec {
    /// A structlog WARNING the shell replays through today's literal event
    /// name; the `*_repr` flags carry the #907/#1594 repr()-wrapping law.
    Log {
        event: &'static str,
        path: String,
        path_repr: bool,
        error: Option<String>,
        error_repr: bool,
    },
    /// "Replay `log_lake_finding(findings[index])` here" — the exact
    /// Python log point of one finding (foreign findings log at overlap
    /// resolution, dup/price/alien at classification).
    Finding { index: usize },
    /// The `detect_legacy_drift` fold point (between the bars and options
    /// phases — the shell runs + logs the Python drift fold here).
    Drift,
}

/// The whole scan payload (converted to ONE Python dict at the FFI).
#[derive(Default)]
pub struct ScanPayload {
    /// Ordered findings (bars-tree first, then options-tree).
    pub findings: Vec<FindingRec>,
    /// Where the shell splices the drift findings (= the bars-tree count).
    pub drift_insert_at: usize,
    /// The ordered replay stream.
    pub events: Vec<EventRec>,
    /// `LakeScanReport` counters, verbatim semantics.
    pub files_scanned: u64,
    /// See `files_scanned`.
    pub files_skipped: u64,
    /// See `files_scanned`.
    pub series_scanned: u64,
    /// See `files_scanned`.
    pub option_files_scanned: u64,
    /// See `files_scanned`.
    pub option_files_skipped: u64,
    /// Per-`bars/`-segment scanned counts (sorted at conversion).
    pub files_by_segment: BTreeMap<String, u64>,
    /// See `files_scanned`.
    pub engine_files: u64,
    /// See `files_scanned`.
    pub engine_files_skipped: u64,
    /// First PATH-parse-skipped file (parse beats read at report build).
    pub first_skipped_parse: Option<String>,
    /// First read-skipped file.
    pub first_skipped_read: Option<String>,
    /// `(exchange, segment)` → symbol dir names (feeds the shell drift fold).
    pub symbol_dirs: BTreeMap<(String, String), BTreeSet<String>>,
    /// `_read_key_columns`-equivalent read attempts (design §F2 — the
    /// counter the delete-time read-count spy pins rebind onto).
    pub reads_performed: u64,
}

impl ScanPayload {
    fn note_first_parse(&mut self, path: &Path) {
        if self.first_skipped_parse.is_none() {
            self.first_skipped_parse = Some(path.display().to_string());
        }
    }

    fn note_first_read(&mut self, path: &Path) {
        if self.first_skipped_read.is_none() {
            self.first_skipped_read = Some(path.display().to_string());
        }
    }

    fn log(
        &mut self,
        event: &'static str,
        path: &Path,
        path_repr: bool,
        error: Option<String>,
        error_repr: bool,
    ) {
        self.events.push(EventRec::Log {
            event,
            path: path.display().to_string(),
            path_repr,
            error,
            error_repr,
        });
    }
}

fn degrade(reason: impl Into<String>) -> ScanFail {
    ScanFail::Degrade(reason.into())
}

/// The errno set Python's `pathlib` probe helpers (`is_dir`/`exists`/
/// `is_symlink`) swallow into `False` (`pathlib._ignore_error`): ENOENT,
/// ENOTDIR, EBADF, ELOOP — plus EINVAL (the `ValueError` arm). Any OTHER
/// stat fault RAISES in Python at the probe sites without a guard, so the
/// Rust twin DEGRADES there (the oracle then crashes identically).
fn ignorable_stat_error(err: &std::io::Error) -> bool {
    matches!(
        err.raw_os_error(),
        Some(code) if code == libc::ENOENT
            || code == libc::ENOTDIR
            || code == libc::EBADF
            || code == libc::ELOOP
            || code == libc::EINVAL
    )
}

/// `Path.is_symlink()` twin (lstat — never follows; ignorable faults read
/// `false`, others degrade — Python would raise there).
fn is_symlink(path: &Path) -> Result<bool, ScanFail> {
    match fs::symlink_metadata(path) {
        Ok(meta) => Ok(meta.file_type().is_symlink()),
        Err(err) if ignorable_stat_error(&err) => Ok(false),
        Err(err) => Err(degrade(format!("cannot lstat {}: {err}", path.display()))),
    }
}

/// `Path.is_dir()` twin at the UNGUARDED Python call sites (options-root /
/// series listing): ignorable faults read `false`, others degrade.
fn is_dir_strict(path: &Path) -> Result<bool, ScanFail> {
    match fs::metadata(path) {
        Ok(meta) => Ok(meta.is_dir()),
        Err(err) if ignorable_stat_error(&err) => Ok(false),
        Err(err) => Err(degrade(format!("cannot stat {}: {err}", path.display()))),
    }
}

fn entry_name(entry: &fs::DirEntry) -> Result<String, ScanFail> {
    entry
        .file_name()
        .into_string()
        .map_err(|_| degrade("non-UTF-8 directory entry name"))
}

/// `lake_invariants._child_dirs` twin: sorted child DIRECTORIES (metadata
/// follows symlinks), one-name filter, per-child fault isolation (ANY
/// stat fault skips that child — the Python per-child `except OSError`
/// guard), a vanished/mid-faulted level reads `[]`.
fn child_dirs(
    parent: &Path,
    name_filter: Option<&str>,
) -> Result<Vec<(String, PathBuf)>, ScanFail> {
    let Ok(entries) = fs::read_dir(parent) else {
        return Ok(Vec::new());
    };
    let mut out: Vec<(String, PathBuf)> = Vec::new();
    for entry in entries {
        let Ok(entry) = entry else {
            // Mid-iteration fault: Python's `list(parent.iterdir())` sits
            // inside ONE try/except OSError returning [] — mirror that.
            return Ok(Vec::new());
        };
        let name = entry_name(&entry)?;
        let path = entry.path();
        match fs::metadata(&path) {
            Ok(meta) if meta.is_dir() => {}
            _ => continue, // one unstat-able / non-dir child never drops its siblings
        }
        if let Some(filter) = name_filter
            && name != filter
        {
            continue;
        }
        out.push((name, path));
    }
    out.sort_by(|a, b| a.0.cmp(&b.0));
    Ok(out)
}

/// The `interval_dir.glob("*/*.parquet")` twin: one dir level (any name,
/// dot-names included, symlinked dirs followed) then `*.parquet`-NAMED
/// entries of ANY type (a dir / dangling symlink named `06.parquet` is a
/// candidate — its read/parse then classifies it). Unreadable levels are
/// silently absent (the glob's OSError suppression); sorted by
/// `(year_name, file_name)` — Python's `sorted(Path)` component order.
fn leaf_candidates(interval_dir: &Path) -> Result<Vec<PathBuf>, ScanFail> {
    let Ok(years) = fs::read_dir(interval_dir) else {
        return Ok(Vec::new());
    };
    let mut out: Vec<(String, String, PathBuf)> = Vec::new();
    for year_entry in years {
        let Ok(year_entry) = year_entry else {
            return Ok(Vec::new());
        };
        let year_name = entry_name(&year_entry)?;
        let year_path = year_entry.path();
        match fs::metadata(&year_path) {
            Ok(meta) if meta.is_dir() => {}
            _ => continue,
        }
        let Ok(files) = fs::read_dir(&year_path) else {
            continue;
        };
        for file_entry in files {
            let Ok(file_entry) = file_entry else {
                break;
            };
            let file_name = entry_name(&file_entry)?;
            if file_name.ends_with(".parquet") {
                out.push((year_name.clone(), file_name, file_entry.path()));
            }
        }
    }
    out.sort_by(|a, b| (&a.0, &a.1).cmp(&(&b.0, &b.1)));
    Ok(out.into_iter().map(|(_, _, path)| path).collect())
}

/// `int()`-twin verdict for a year/month path token.
pub(super) enum IntParse {
    /// Pure-ASCII-digit parse (the writer-minted shape).
    Value(i64),
    /// Python `int()` provably fails too → the `PartitionError` class.
    Fail,
    /// Python `int()` MIGHT accept (signs/underscores/whitespace/unicode
    /// digits) — outside the native envelope → whole-scan degrade.
    Ambiguous,
}

pub(super) fn int_twin(token: &str) -> IntParse {
    if token.is_empty() {
        return IntParse::Fail;
    }
    if token.bytes().all(|b| b.is_ascii_digit()) {
        return match token.parse::<i64>() {
            Ok(value) => IntParse::Value(value),
            // > i64 digits: Python parses the bigint then fails the
            // [2000, 2100] range check — the same PartitionError verdict.
            Err(_) => IntParse::Fail,
        };
    }
    for ch in token.chars() {
        if ch.is_ascii_digit()
            || ch == '+'
            || ch == '-'
            || ch == '_'
            || ch.is_whitespace()
            || ch.is_numeric()
        {
            continue;
        }
        return IntParse::Fail; // a letter/punct char no int() grammar accepts
    }
    IntParse::Ambiguous
}

/// `parse_partition_path(path, lake_root=…)` twin: `Ok(None)` = the
/// `PartitionError` class (a counted parse skip), `Ok(Some)` = coords.
/// The lake root canonicalizes LAZILY once; a path `fs::canonicalize`
/// cannot resolve (dangling symlink / loop — shapes Python's
/// `resolve(strict=False)` resolves lexically) degrades the whole scan.
fn parse_partition(
    cfg: &ScanConfig,
    canon_root: &mut Option<PathBuf>,
    path: &Path,
) -> Result<Option<BarsCoords>, ScanFail> {
    let root: &PathBuf = match canon_root {
        Some(root) => root,
        None => {
            let root = fs::canonicalize(&cfg.lake_root).map_err(|err| {
                degrade(format!(
                    "cannot resolve lake root {}: {err}",
                    cfg.lake_root.display()
                ))
            })?;
            canon_root.insert(root)
        }
    };
    let canon = fs::canonicalize(path)
        .map_err(|err| degrade(format!("cannot resolve {}: {err}", path.display())))?;
    let Ok(rel) = canon.strip_prefix(root) else {
        return Ok(None); // "not inside lake_root" — the PartitionError arm
    };
    let mut parts: Vec<&str> = Vec::new();
    for comp in rel.components() {
        match comp.as_os_str().to_str() {
            Some(text) => parts.push(text),
            None => return Err(degrade("non-UTF-8 path component")),
        }
    }
    if parts.len() < 7 {
        return Ok(None);
    }
    let tail = &parts[parts.len() - 7..];
    if tail[0] != "bars" {
        return Ok(None);
    }
    let Some(month_token) = tail[6].strip_suffix(".parquet") else {
        return Ok(None);
    };
    let year = match int_twin(tail[5]) {
        IntParse::Value(value) => value,
        IntParse::Fail => return Ok(None),
        IntParse::Ambiguous => {
            return Err(degrade(format!(
                "year dir name {:?} is int()-ambiguous — outside the native parse envelope",
                tail[5]
            )));
        }
    };
    let month = match int_twin(month_token) {
        IntParse::Value(value) => value,
        IntParse::Fail => return Ok(None),
        IntParse::Ambiguous => {
            return Err(degrade(format!(
                "month file name {:?} is int()-ambiguous — outside the native parse envelope",
                tail[6]
            )));
        }
    };
    if !cfg.valid_exchanges.contains(tail[1]) || !cfg.valid_segments.contains(tail[2]) {
        return Ok(None);
    }
    let symbol = tail[3];
    if symbol.is_empty()
        || symbol
            .chars()
            .any(|ch| cfg.forbidden_symbol_chars.contains(&ch))
    {
        return Ok(None);
    }
    if !cfg.valid_intervals.contains(tail[4]) {
        return Ok(None);
    }
    if !(2000..=2100).contains(&year) || !(1..=12).contains(&month) {
        return Ok(None);
    }
    Ok(Some(BarsCoords {
        exchange: tail[1].to_string(),
        segment: tail[2].to_string(),
        symbol: symbol.to_string(),
        interval: tail[4].to_string(),
        year,
        month: month as u32,
    }))
}

/// The ONE per-file column read (`_read_key_columns` twin — exact path,
/// never a glob; a projected-out missing column errors like polars-python's
/// `ColumnNotFoundError`).
fn read_columns(path: &Path, columns: &[&str]) -> PolarsResult<DataFrame> {
    let file = std::fs::File::open(path)
        .map_err(|err| polars_err!(ComputeError: "cannot open {}: {}", path.display(), err))?;
    let df = ParquetReader::new(file)
        .with_columns(Some(columns.iter().map(|c| (*c).to_string()).collect()))
        .finish()?;
    for column in columns {
        if df.column(column).is_err() {
            return Err(polars_err!(ColumnNotFound: "column {} not found", column));
        }
    }
    Ok(df)
}

/// `_missing_columns_reason` twin: one anomaly-only footer probe deciding
/// whether a failed read is the alien missing-column class — the reason
/// string is BYTE-EXACT (`missing column(s): a, b` in scan-column order).
fn missing_columns_reason(path: &Path, columns: &[&str]) -> Option<String> {
    let file = std::fs::File::open(path).ok()?;
    let schema = ParquetReader::new(file).schema().ok()?;
    let missing: Vec<&str> = columns
        .iter()
        .filter(|column| !schema.contains(column))
        .copied()
        .collect();
    if missing.is_empty() {
        None
    } else {
        Some(format!("missing column(s): {}", missing.join(", ")))
    }
}

fn time_unit_token(unit: TimeUnit) -> &'static str {
    match unit {
        TimeUnit::Microseconds => "us",
        TimeUnit::Milliseconds => "ms",
        TimeUnit::Nanoseconds => "ns",
    }
}

/// The polars-PYTHON `str(dtype)` spelling of *dtype* — feeds the alien
/// reasons (compared byte-for-byte through the finding wire). `None` = an
/// unmapped dtype (nested/exotic) → the caller degrades the whole scan
/// (the oracle then renders the real repr; deviation D-19).
fn python_dtype_repr(dtype: &DataType) -> Option<String> {
    Some(match dtype {
        DataType::Null => "Null".to_string(),
        DataType::Boolean => "Boolean".to_string(),
        DataType::Int8 => "Int8".to_string(),
        DataType::Int16 => "Int16".to_string(),
        DataType::Int32 => "Int32".to_string(),
        DataType::Int64 => "Int64".to_string(),
        DataType::Int128 => "Int128".to_string(),
        DataType::UInt8 => "UInt8".to_string(),
        DataType::UInt16 => "UInt16".to_string(),
        DataType::UInt32 => "UInt32".to_string(),
        DataType::UInt64 => "UInt64".to_string(),
        DataType::Float32 => "Float32".to_string(),
        DataType::Float64 => "Float64".to_string(),
        DataType::String => "String".to_string(),
        DataType::Binary => "Binary".to_string(),
        DataType::Date => "Date".to_string(),
        DataType::Time => "Time".to_string(),
        DataType::Datetime(unit, tz) => {
            let tz_repr = match tz {
                None => "None".to_string(),
                Some(tz) => format!("'{tz}'"),
            };
            format!(
                "Datetime(time_unit='{}', time_zone={})",
                time_unit_token(*unit),
                tz_repr
            )
        }
        DataType::Duration(unit) => {
            format!("Duration(time_unit='{}')", time_unit_token(*unit))
        }
        // Decimal is feature-gated OFF in this crate build (`dtype-decimal`
        // absent from the manifest) — a Decimal-typed stored column cannot
        // materialize in a crate-read frame, so no mapping is needed; any
        // OTHER unmapped dtype falls through to the honest None (the caller
        // degrades to the oracle, never guesses a repr).
        _ => return None,
    })
}

/// `_bar_dtype_alien_reason` twin over the projected frame's schema.
fn bar_dtype_alien_reason(df: &DataFrame) -> Result<Option<String>, ScanFail> {
    let ts_dtype = df
        .column("timestamp")
        .map_err(|err| degrade(format!("timestamp column vanished: {err}")))?
        .dtype()
        .clone();
    if !matches!(ts_dtype, DataType::Datetime(..)) {
        let repr = python_dtype_repr(&ts_dtype)
            .ok_or_else(|| degrade(format!("unmappable timestamp dtype {ts_dtype}")))?;
        return Ok(Some(format!("timestamp dtype {repr} is not Datetime")));
    }
    for column in &BAR_SCAN_COLUMNS[1..] {
        let dtype = df
            .column(column)
            .map_err(|err| degrade(format!("{column} column vanished: {err}")))?
            .dtype()
            .clone();
        // Python's `dtype.is_numeric()` additionally admits Decimal; the
        // crate build has Decimal feature-gated OFF, so a Decimal column can
        // never reach this frame (the parquet read itself fails → degrade).
        let numeric = dtype.is_primitive_numeric();
        if !numeric {
            let repr = python_dtype_repr(&dtype)
                .ok_or_else(|| degrade(format!("unmappable {column} dtype {dtype}")))?;
            return Ok(Some(format!("column {column} dtype {repr} is not numeric")));
        }
    }
    Ok(None)
}

/// One price column's exact values (int family widened to i128, float
/// family to f64 — both EXACT widenings; a mixed / exotic family degrades).
enum PriceCol {
    F(Vec<Option<f64>>),
    I(Vec<Option<i128>>),
}

fn price_column(df: &DataFrame, name: &str) -> Result<PriceCol, ScanFail> {
    let column = df
        .column(name)
        .map_err(|err| degrade(format!("{name} column vanished: {err}")))?;
    let series = column.as_materialized_series();
    let fail = |err: PolarsError| degrade(format!("{name} accessor failed: {err}"));
    Ok(match series.dtype() {
        DataType::Float64 => PriceCol::F(series.f64().map_err(fail)?.iter().collect()),
        DataType::Float32 => PriceCol::F(
            series
                .f32()
                .map_err(fail)?
                .iter()
                .map(|v| v.map(f64::from))
                .collect(),
        ),
        DataType::Int8 => PriceCol::I(
            series
                .i8()
                .map_err(fail)?
                .iter()
                .map(|v| v.map(i128::from))
                .collect(),
        ),
        DataType::Int16 => PriceCol::I(
            series
                .i16()
                .map_err(fail)?
                .iter()
                .map(|v| v.map(i128::from))
                .collect(),
        ),
        DataType::Int32 => PriceCol::I(
            series
                .i32()
                .map_err(fail)?
                .iter()
                .map(|v| v.map(i128::from))
                .collect(),
        ),
        DataType::Int64 => PriceCol::I(
            series
                .i64()
                .map_err(fail)?
                .iter()
                .map(|v| v.map(i128::from))
                .collect(),
        ),
        DataType::UInt8 => PriceCol::I(
            series
                .u8()
                .map_err(fail)?
                .iter()
                .map(|v| v.map(i128::from))
                .collect(),
        ),
        DataType::UInt16 => PriceCol::I(
            series
                .u16()
                .map_err(fail)?
                .iter()
                .map(|v| v.map(i128::from))
                .collect(),
        ),
        DataType::UInt32 => PriceCol::I(
            series
                .u32()
                .map_err(fail)?
                .iter()
                .map(|v| v.map(i128::from))
                .collect(),
        ),
        DataType::UInt64 => PriceCol::I(
            series
                .u64()
                .map_err(fail)?
                .iter()
                .map(|v| v.map(i128::from))
                .collect(),
        ),
        other => {
            return Err(degrade(format!(
                "price column {name} dtype {other} outside the native scan envelope"
            )));
        }
    })
}

/// `count_price_violations` twin: null / non-finite / non-positive prices +
/// broken OHLC ordering, per row — the `price_violation_expr` boolean over
/// same-family columns (mixed float+int OHLC degrades — polars would
/// supertype-cast there; the oracle owns that verdict).
fn price_violations(df: &DataFrame) -> Result<u64, ScanFail> {
    if df.height() == 0 {
        return Ok(0);
    }
    let open = price_column(df, "open")?;
    let high = price_column(df, "high")?;
    let low = price_column(df, "low")?;
    let close = price_column(df, "close")?;
    let mut violations: u64 = 0;
    match (&open, &high, &low, &close) {
        (PriceCol::F(o), PriceCol::F(h), PriceCol::F(l), PriceCol::F(c)) => {
            for i in 0..df.height() {
                let insane = match (o[i], h[i], l[i], c[i]) {
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
        }
        (PriceCol::I(o), PriceCol::I(h), PriceCol::I(l), PriceCol::I(c)) => {
            for i in 0..df.height() {
                let insane = match (o[i], h[i], l[i], c[i]) {
                    (Some(o), Some(h), Some(l), Some(c)) => {
                        o <= 0
                            || h <= 0
                            || l <= 0
                            || c <= 0
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
        }
        _ => {
            return Err(degrade(
                "mixed int/float OHLC dtypes — outside the native scan envelope",
            ));
        }
    }
    Ok(violations)
}

/// A Datetime column's raw physicals + unit + tz-awareness (`None` for a
/// non-Datetime dtype — the caller's honest-abstain arms own that).
type TsRaw = Option<(Vec<Option<i64>>, TimeUnit, bool)>;

fn ts_raw(column: &Column) -> Result<TsRaw, ScanFail> {
    let series = column.as_materialized_series();
    let DataType::Datetime(unit, tz) = series.dtype() else {
        return Ok(None);
    };
    let unit = *unit;
    let aware = tz.is_some();
    let phys = series.to_physical_repr();
    let values = phys
        .i64()
        .map_err(|err| degrade(format!("timestamp physical view failed: {err}")))?
        .iter()
        .collect();
    Ok(Some((values, unit, aware)))
}

/// The per-unit Python-representable physical bounds (`None` = ns —
/// vacuously in range, every storable i64 converts).
fn unit_bounds(unit: TimeUnit) -> Option<(i64, i64)> {
    match unit {
        TimeUnit::Microseconds => Some((DATETIME_MIN_US, DATETIME_MAX_US)),
        TimeUnit::Milliseconds => Some((DATETIME_MIN_US / 1_000, DATETIME_MAX_US / 1_000)),
        TimeUnit::Nanoseconds => None,
    }
}

/// `True` iff any non-null physical lies outside the unit's representable
/// range — the `datetime_out_of_range_errors` conversion-fault class.
fn out_of_datetime_range(values: &[Option<i64>], unit: TimeUnit) -> bool {
    let Some((low, high)) = unit_bounds(unit) else {
        return false;
    };
    values
        .iter()
        .flatten()
        .any(|value| *value < low || *value > high)
}

/// Normalize IN-RANGE physicals to microseconds (`ms` multiply is safe
/// post-range-check; `ns` floors — sub-µs precision cannot move a month).
fn normalize_us(values: &[Option<i64>], unit: TimeUnit) -> Vec<Option<i64>> {
    values
        .iter()
        .map(|value| {
            value.map(|v| match unit {
                TimeUnit::Microseconds => v,
                TimeUnit::Milliseconds => v * 1_000,
                TimeUnit::Nanoseconds => v.div_euclid(1_000),
            })
        })
        .collect()
}

/// The UTC `(year, month)` of one epoch-µs instant
/// (`partition_for_timestamp(utc_aware(ts))` twin).
fn utc_month(us: i64) -> (i64, u32) {
    let (year, month, _) = civil_from_days(us.div_euclid(US_PER_DAY));
    (year, month)
}

// ────────────────────────────────────────────────────────────────────────
// bars tree
// ────────────────────────────────────────────────────────────────────────

/// The bars canonical-sibling timestamp set (`_canonical_timestamps` twin):
/// `Some(empty)` for a missing/impossible sibling, `None` for an unreadable
/// or out-of-range one (honest abstain, warned + pause-ticked).
#[allow(clippy::too_many_arguments)]
fn bars_canonical_set(
    cfg: &ScanConfig,
    payload: &mut ScanPayload,
    pause: Pause<'_>,
    coords: &BarsCoords,
    year: i64,
    month: u32,
) -> Result<Option<HashSet<i64>>, ScanFail> {
    if !(2000..=2100).contains(&year) {
        // `build_partition_path` raises PartitionError there — no canonical
        // partition can exist (pure misplacement).
        return Ok(Some(HashSet::new()));
    }
    let canonical = cfg
        .lake_root
        .join("bars")
        .join(&coords.exchange)
        .join(&coords.segment)
        .join(&coords.symbol)
        .join(&coords.interval)
        .join(format!("{year:04}"))
        .join(format!("{month:02}.parquet"));
    match fs::metadata(&canonical) {
        Err(err) if ignorable_stat_error(&err) => return Ok(Some(HashSet::new())),
        Err(err) => {
            return Err(degrade(format!(
                "cannot stat canonical sibling {}: {err}",
                canonical.display()
            )));
        }
        Ok(_) => {}
    }
    payload.reads_performed += 1;
    let df = match read_columns(&canonical, &["timestamp"]) {
        Err(err) => {
            // Bars-tree scan_skip: RAW path + RAW error (catalogued
            // coordinates — the established convention).
            payload.log(
                EVENT_SCAN_SKIP,
                &canonical,
                false,
                Some(err.to_string()),
                false,
            );
            pause()?;
            return Ok(None);
        }
        Ok(df) => df,
    };
    pause()?;
    let ts = df
        .column("timestamp")
        .map_err(|err| degrade(format!("canonical timestamp column vanished: {err}")))?;
    match ts_raw(ts)? {
        // Non-Datetime values are never `isinstance(ts, datetime)` — the
        // Python set comprehension yields nothing for them.
        None => Ok(Some(HashSet::new())),
        Some((values, unit, _aware)) => {
            if out_of_datetime_range(&values, unit) {
                payload.log(
                    EVENT_SCAN_SKIP,
                    &canonical,
                    false,
                    Some("timestamp value out of datetime range".to_string()),
                    false,
                );
                return Ok(None);
            }
            Ok(Some(
                normalize_us(&values, unit).into_iter().flatten().collect(),
            ))
        }
    }
}

/// `_classify_overlap` twin: group foreign instants by canonical month,
/// one memoised targeted sibling read per affected month, `None` on any
/// unresolved (unreadable) sibling.
#[allow(clippy::too_many_arguments)]
fn classify_overlap(
    cfg: &ScanConfig,
    payload: &mut ScanPayload,
    pause: Pause<'_>,
    coords: &BarsCoords,
    foreign_us: &BTreeSet<i64>,
    cache: &mut HashMap<(i64, u32), Option<HashSet<i64>>>,
) -> Result<Option<u64>, ScanFail> {
    let mut grouped: BTreeMap<(i64, u32), Vec<i64>> = BTreeMap::new();
    for us in foreign_us {
        grouped.entry(utc_month(*us)).or_default().push(*us);
    }
    let mut overlap: u64 = 0;
    let mut unresolved = false;
    for ((year, month), values) in &grouped {
        let slot = match cache.entry((*year, *month)) {
            Entry::Occupied(slot) => slot.into_mut(),
            Entry::Vacant(slot) => slot.insert(bars_canonical_set(
                cfg, payload, pause, coords, *year, *month,
            )?),
        };
        match slot {
            None => unresolved = true,
            Some(stored) => {
                overlap += values.iter().filter(|us| stored.contains(us)).count() as u64;
            }
        }
    }
    Ok(if unresolved { None } else { Some(overlap) })
}

/// One bars series' classification loop (parse already done by the caller).
fn scan_bars_series(
    cfg: &ScanConfig,
    payload: &mut ScanPayload,
    pause: Pause<'_>,
    entries: &[(BarsCoords, PathBuf)],
) -> Result<(), ScanFail> {
    let mut scanned_in_series: u64 = 0;
    let mut pending: Vec<(usize, BarsCoords, BTreeSet<i64>)> = Vec::new();
    let mut canonical_cache: HashMap<(i64, u32), Option<HashSet<i64>>> = HashMap::new();
    for (coords, path) in entries {
        payload.reads_performed += 1;
        let df = match read_columns(path, &BAR_SCAN_COLUMNS) {
            Err(err) => {
                match missing_columns_reason(path, &BAR_SCAN_COLUMNS) {
                    Some(reason) => {
                        let index = payload.findings.len();
                        payload.findings.push(FindingRec::AlienBars {
                            path: path.display().to_string(),
                            coords: coords.clone(),
                            reason,
                        });
                        payload.events.push(EventRec::Finding { index });
                    }
                    None => {
                        payload.log(EVENT_SCAN_SKIP, path, false, Some(err.to_string()), false);
                    }
                }
                payload.files_skipped += 1;
                payload.note_first_read(path);
                if coords.engine(cfg) {
                    payload.engine_files_skipped += 1;
                }
                pause()?;
                continue;
            }
            Ok(df) => df,
        };
        pause()?;
        if let Some(reason) = bar_dtype_alien_reason(&df)? {
            // Dtype-alien: skipped WITHOUT a first-skip note or engine
            // attribution (the Python flow's exact asymmetry — the finding
            // itself carries the engine relevance).
            let index = payload.findings.len();
            payload.findings.push(FindingRec::AlienBars {
                path: path.display().to_string(),
                coords: coords.clone(),
                reason,
            });
            payload.events.push(EventRec::Finding { index });
            payload.files_skipped += 1;
            continue;
        }
        let ts = df
            .column("timestamp")
            .map_err(|err| degrade(format!("timestamp column vanished: {err}")))?;
        let Some((raw, unit, _aware)) = ts_raw(ts)? else {
            return Err(degrade("timestamp dtype changed under the alien gate"));
        };
        if out_of_datetime_range(&raw, unit) {
            payload.log(
                EVENT_SCAN_SKIP,
                path,
                false,
                Some("timestamp value out of datetime range".to_string()),
                false,
            );
            payload.files_skipped += 1;
            payload.note_first_read(path);
            if coords.engine(cfg) {
                payload.engine_files_skipped += 1;
            }
            continue;
        }
        let physical = raw.len() as u64;
        let bad_rows = price_violations(&df)?;
        payload.files_scanned += 1;
        scanned_in_series += 1;
        *payload
            .files_by_segment
            .entry(coords.segment.clone())
            .or_insert(0) += 1;
        if coords.engine(cfg) {
            payload.engine_files += 1;
        }
        if physical > 0 {
            let unique = raw.iter().collect::<HashSet<_>>().len() as u64;
            if physical > unique {
                let index = payload.findings.len();
                payload.findings.push(FindingRec::BarsDup {
                    path: path.display().to_string(),
                    coords: coords.clone(),
                    physical_rows: physical,
                    unique_keys: unique,
                });
                payload.events.push(EventRec::Finding { index });
            }
            let home = (coords.year, i64::from(coords.month) as u32);
            let normalized = normalize_us(&raw, unit);
            let mut foreign_rows: u64 = 0;
            let mut foreign_us: BTreeSet<i64> = BTreeSet::new();
            for value in normalized.iter().flatten() {
                if utc_month(*value) != (home.0, home.1) {
                    foreign_rows += 1;
                    foreign_us.insert(*value);
                }
            }
            if foreign_rows > 0 {
                let index = payload.findings.len();
                payload.findings.push(FindingRec::BarsForeign {
                    path: path.display().to_string(),
                    coords: coords.clone(),
                    foreign_rows,
                    overlap_rows: None,
                });
                pending.push((index, coords.clone(), foreign_us));
            }
        }
        if bad_rows > 0 {
            let index = payload.findings.len();
            payload.findings.push(FindingRec::Price {
                path: path.display().to_string(),
                coords: coords.clone(),
                bad_rows,
            });
            payload.events.push(EventRec::Finding { index });
        }
    }
    if scanned_in_series > 0 {
        payload.series_scanned += 1;
    }
    for (index, coords, foreign_us) in pending {
        let overlap = classify_overlap(
            cfg,
            payload,
            pause,
            &coords,
            &foreign_us,
            &mut canonical_cache,
        )?;
        if let FindingRec::BarsForeign { overlap_rows, .. } = &mut payload.findings[index] {
            *overlap_rows = overlap;
        }
        payload.events.push(EventRec::Finding { index });
    }
    Ok(())
}

fn scan_bars(
    cfg: &ScanConfig,
    payload: &mut ScanPayload,
    canon_root: &mut Option<PathBuf>,
    pause: Pause<'_>,
) -> Result<(), ScanFail> {
    let base = cfg.lake_root.join("bars");
    for (_exch_name, exch_dir) in child_dirs(&base, cfg.exchange.as_deref())? {
        for (_seg_name, seg_dir) in child_dirs(&exch_dir, cfg.segment.as_deref())? {
            for (_sym_name, sym_dir) in child_dirs(&seg_dir, cfg.symbol.as_deref())? {
                for (_interval_name, interval_dir) in child_dirs(&sym_dir, cfg.interval.as_deref())?
                {
                    let candidates = leaf_candidates(&interval_dir)?;
                    if candidates.is_empty() {
                        continue;
                    }
                    let mut entries: Vec<(BarsCoords, PathBuf)> = Vec::new();
                    for path in &candidates {
                        match parse_partition(cfg, canon_root, path)? {
                            None => {
                                payload.files_skipped += 1;
                                payload.note_first_parse(path);
                                // Positional engine attribution from the
                                // WALK tail (`_engine_coord_from_path`).
                                if let Ok(rel) = path.strip_prefix(&base) {
                                    let walk: Vec<&str> = rel
                                        .components()
                                        .take(3)
                                        .filter_map(|c| c.as_os_str().to_str())
                                        .collect();
                                    if walk.len() == 3
                                        && cfg.engine_coords.contains(&(
                                            walk[0].to_string(),
                                            walk[1].to_string(),
                                            walk[2].to_string(),
                                        ))
                                    {
                                        payload.engine_files_skipped += 1;
                                    }
                                }
                                continue;
                            }
                            Some(coords) => {
                                payload
                                    .symbol_dirs
                                    .entry((coords.exchange.clone(), coords.segment.clone()))
                                    .or_default()
                                    .insert(coords.symbol.clone());
                                if let Some(scope) = &cfg.months
                                    && !scope.contains(&(coords.year, coords.month))
                                {
                                    continue; // the L2 scope bound — never read
                                }
                                entries.push((coords, path.clone()));
                            }
                        }
                    }
                    if entries.is_empty() {
                        continue;
                    }
                    // Numeric within-series order (stable — parse-order ties keep
                    // walk order, the Python `list.sort` law).
                    entries.sort_by_key(|(coords, _)| (coords.year, coords.month));
                    scan_bars_series(cfg, payload, pause, &entries)?;
                }
            }
        }
    }
    Ok(())
}

// ────────────────────────────────────────────────────────────────────────
// options tree
// ────────────────────────────────────────────────────────────────────────

/// `_option_file_month` twin — proven equivalent to Python's
/// isdigit→int→bounds→canonical-render chain: ONLY the writer's canonical
/// zero-padded ASCII `YYYY-MM` stem parses (any unicode-digit / signed /
/// non-canonical shape fails the final canonical-render equality in Python
/// too, so the ASCII-only pre-check nets the identical verdict).
fn option_file_month(file_name: &str) -> Option<(i64, u32)> {
    let stem = file_name.strip_suffix(".parquet")?;
    let (year_token, month_token) = stem.rsplit_once('-')?;
    if year_token.is_empty()
        || month_token.is_empty()
        || !year_token.bytes().all(|b| b.is_ascii_digit())
        || !month_token.bytes().all(|b| b.is_ascii_digit())
    {
        return None;
    }
    let year: i64 = year_token.parse().ok()?;
    let month: u32 = month_token.parse().ok()?;
    if !(1..=12).contains(&month) || !(1..=9999).contains(&year) {
        return None;
    }
    if stem != format!("{year:04}-{month:02}") {
        return None;
    }
    Some((year, month))
}

/// One option key's timestamp payload: µs-normalized physical + awareness
/// (aware vs naive datetimes are NEVER equal in Python — the raw-key law).
type OptionTs = Option<(i64, bool)>;

/// One raw option key (`(side, timestamp)` — side is payload, null rides).
type OptionKey = (Option<String>, OptionTs);

/// `option_key_dtype_error` twin: the verdict string (log-only for the
/// scan — never in a finding) or `None` when the key columns are sound.
fn option_key_gate(df: &DataFrame) -> Result<Option<String>, ScanFail> {
    let Ok(side) = df.column("side") else {
        return Ok(Some(
            "option side column missing — unclassifiable".to_string(),
        ));
    };
    let side_dtype = side.dtype().clone();
    if !matches!(side_dtype, DataType::String | DataType::Null) {
        let repr = python_dtype_repr(&side_dtype).unwrap_or_else(|| side_dtype.to_string());
        return Ok(Some(format!(
            "option side column dtype {repr} is not String/Null — unclassifiable"
        )));
    }
    let Ok(ts) = df.column("timestamp") else {
        return Ok(Some(
            "option timestamp column missing — unclassifiable".to_string(),
        ));
    };
    let ts_dtype = ts.dtype().clone();
    if !matches!(ts_dtype, DataType::Datetime(..) | DataType::Null) {
        let repr = python_dtype_repr(&ts_dtype).unwrap_or_else(|| ts_dtype.to_string());
        return Ok(Some(format!(
            "option timestamp column dtype {repr} is not Datetime/Null — unclassifiable"
        )));
    }
    if let Some((values, unit, _aware)) = ts_raw(ts)?
        && out_of_datetime_range(&values, unit)
    {
        return Ok(Some(
            "option timestamp value out of datetime range — unclassifiable".to_string(),
        ));
    }
    Ok(None)
}

/// The side column's values (`String` or all-`Null` — the gate ran first).
fn side_values(df: &DataFrame) -> Result<Vec<Option<String>>, ScanFail> {
    let column = df
        .column("side")
        .map_err(|err| degrade(format!("side column vanished: {err}")))?;
    let series = column.as_materialized_series();
    match series.dtype() {
        DataType::String => Ok(series
            .str()
            .map_err(|err| degrade(format!("side accessor failed: {err}")))?
            .iter()
            .map(|value| value.map(str::to_string))
            .collect()),
        DataType::Null => Ok(vec![None; series.len()]),
        other => Err(degrade(format!("side dtype {other} escaped the gate"))),
    }
}

/// The timestamp column's option-key payloads (µs-normalized + awareness);
/// a `Null`-dtype column reads all-`None` (abstains from routing).
fn option_ts_values(df: &DataFrame) -> Result<Vec<OptionTs>, ScanFail> {
    let column = df
        .column("timestamp")
        .map_err(|err| degrade(format!("timestamp column vanished: {err}")))?;
    match ts_raw(column)? {
        None => Ok(vec![None; df.height()]), // Null dtype (the gate excluded the rest)
        Some((values, unit, aware)) => Ok(normalize_us(&values, unit)
            .into_iter()
            .map(|value| value.map(|us| (us, aware)))
            .collect()),
    }
}

/// `_canonical_option_keys` twin: the sibling's raw key set; `Some(empty)`
/// for a missing sibling, `None` for a symlinked / unreadable /
/// gate-refused one (honest abstain).
fn option_canonical_keys(
    payload: &mut ScanPayload,
    pause: Pause<'_>,
    series_dir: &Path,
    year: i64,
    month: u32,
) -> Result<Option<HashSet<OptionKey>>, ScanFail> {
    let canonical = series_dir.join(format!("{year:04}-{month:02}.parquet"));
    if is_symlink(&canonical)? {
        // repr() — the path embeds the vendor-influenceable series dir name
        // (the round-8 log-injection fold); no pause — no read attempted.
        payload.log(EVENT_SYMLINK_SKIPPED, &canonical, true, None, false);
        return Ok(None);
    }
    match fs::metadata(&canonical) {
        Err(err) if ignorable_stat_error(&err) => return Ok(Some(HashSet::new())),
        Err(err) => {
            return Err(degrade(format!(
                "cannot stat canonical option sibling {}: {err}",
                canonical.display()
            )));
        }
        Ok(_) => {}
    }
    payload.reads_performed += 1;
    let df = match read_columns(&canonical, &OPTION_KEY_COLUMNS) {
        Err(err) => {
            // repr() on the path AND the error (the rounds-8/17 folds: a
            // polars message embeds the raw vendor path).
            payload.log(
                EVENT_SCAN_SKIP,
                &canonical,
                true,
                Some(err.to_string()),
                true,
            );
            pause()?;
            return Ok(None);
        }
        Ok(df) => df,
    };
    pause()?;
    if let Some(verdict) = option_key_gate(&df)? {
        // Verdict strings are path-free — the error field rides RAW.
        payload.log(EVENT_SCAN_SKIP, &canonical, true, Some(verdict), false);
        return Ok(None);
    }
    let sides = side_values(&df)?;
    let stamps = option_ts_values(&df)?;
    Ok(Some(sides.into_iter().zip(stamps).collect()))
}

/// `_classify_option_overlap` twin over one series' pending foreign keys.
fn classify_option_overlap(
    payload: &mut ScanPayload,
    pause: Pause<'_>,
    series_dir: &Path,
    foreign_keys: &BTreeSet<OptionKey>,
    cache: &mut HashMap<(i64, u32), Option<HashSet<OptionKey>>>,
) -> Result<Option<u64>, ScanFail> {
    let mut grouped: BTreeMap<(i64, u32), Vec<&OptionKey>> = BTreeMap::new();
    for key in foreign_keys {
        let Some((us, _aware)) = key.1 else {
            return Err(degrade("foreign key without timestamp".to_string()));
        };
        grouped.entry(utc_month(us)).or_default().push(key);
    }
    let mut overlap: u64 = 0;
    let mut unresolved = false;
    for ((year, month), keys) in &grouped {
        let slot = match cache.entry((*year, *month)) {
            Entry::Occupied(slot) => slot.into_mut(),
            Entry::Vacant(slot) => slot.insert(option_canonical_keys(
                payload, pause, series_dir, *year, *month,
            )?),
        };
        match slot {
            None => unresolved = true,
            Some(stored) => {
                overlap += keys.iter().filter(|key| stored.contains(**key)).count() as u64;
            }
        }
    }
    Ok(if unresolved { None } else { Some(overlap) })
}

fn scan_options(
    cfg: &ScanConfig,
    payload: &mut ScanPayload,
    pause: Pause<'_>,
) -> Result<(), ScanFail> {
    let options_root = cfg.lake_root.join("options");
    if is_symlink(&options_root)? {
        // The options-ROOT skip stays RAW: operator-configured root + a
        // fixed literal, no vendor-derived component.
        payload.log(EVENT_SYMLINK_SKIPPED, &options_root, false, None, false);
        return Ok(());
    }
    if !is_dir_strict(&options_root)? {
        return Ok(());
    }
    // The series listing is UNGUARDED in Python (`options_root.iterdir()`
    // raising crashes the scan) — a listing fault degrades, the oracle
    // then crashes identically.
    let entries = fs::read_dir(&options_root)
        .map_err(|err| degrade(format!("cannot list options root: {err}")))?;
    let mut series_dirs: Vec<(String, PathBuf)> = Vec::new();
    for entry in entries {
        let entry = entry.map_err(|err| degrade(format!("options root listing fault: {err}")))?;
        let name = entry_name(&entry)?;
        let path = entry.path();
        if is_dir_strict(&path)? {
            series_dirs.push((name, path));
        }
    }
    series_dirs.sort_by(|a, b| a.0.cmp(&b.0));
    for (_series_name, series_dir) in series_dirs {
        if is_symlink(&series_dir)? {
            payload.log(EVENT_SYMLINK_SKIPPED, &series_dir, true, None, false);
            continue;
        }
        let mut canonical_cache: HashMap<(i64, u32), Option<HashSet<OptionKey>>> = HashMap::new();
        let mut pending: Vec<(usize, BTreeSet<OptionKey>)> = Vec::new();
        let mut file_names: Vec<String> = Vec::new();
        if let Ok(files) = fs::read_dir(&series_dir) {
            for file_entry in files {
                let Ok(file_entry) = file_entry else {
                    break;
                };
                let name = entry_name(&file_entry)?;
                if name.ends_with(".parquet") {
                    file_names.push(name);
                }
            }
        }
        file_names.sort();
        let series = series_dir
            .file_name()
            .and_then(|name| name.to_str())
            .ok_or_else(|| degrade("non-UTF-8 series dir name"))?
            .to_string();
        for file_name in &file_names {
            let path = series_dir.join(file_name);
            let home = option_file_month(file_name);
            if let Some(scope) = &cfg.months {
                // The L2 scope bound — a pure filename parse BEFORE the
                // symlink probe (the round-5 fold).
                match home {
                    None => continue,
                    Some(month) if !scope.contains(&month) => continue,
                    Some(_) => {}
                }
            }
            if is_symlink(&path)? {
                payload.log(EVENT_SYMLINK_SKIPPED, &path, true, None, false);
                payload.option_files_skipped += 1;
                payload.note_first_read(&path);
                continue;
            }
            payload.reads_performed += 1;
            let df = match read_columns(&path, &OPTION_KEY_COLUMNS) {
                Err(err) => {
                    match missing_columns_reason(&path, &OPTION_KEY_COLUMNS) {
                        Some(reason) => {
                            let index = payload.findings.len();
                            payload.findings.push(FindingRec::AlienOption {
                                path: path.display().to_string(),
                                series: series.clone(),
                                reason,
                            });
                            payload.events.push(EventRec::Finding { index });
                        }
                        None => {
                            payload.log(EVENT_SCAN_SKIP, &path, true, Some(err.to_string()), true);
                        }
                    }
                    payload.option_files_skipped += 1;
                    payload.note_first_read(&path);
                    pause()?;
                    continue;
                }
                Ok(df) => df,
            };
            pause()?;
            if let Some(verdict) = option_key_gate(&df)? {
                payload.log(EVENT_SCAN_SKIP, &path, true, Some(verdict), false);
                payload.option_files_skipped += 1;
                payload.note_first_read(&path);
                continue;
            }
            let physical = df.height() as u64;
            payload.option_files_scanned += 1;
            if physical == 0 {
                continue;
            }
            let sides = side_values(&df)?;
            let stamps = option_ts_values(&df)?;
            let unique = sides
                .iter()
                .zip(stamps.iter())
                .collect::<HashSet<_>>()
                .len() as u64;
            if physical > unique {
                let index = payload.findings.len();
                payload.findings.push(FindingRec::OptionDup {
                    path: path.display().to_string(),
                    series: series.clone(),
                    physical_rows: physical,
                    unique_keys: unique,
                });
                payload.events.push(EventRec::Finding { index });
            }
            if let Some((year, month)) = home {
                let mut foreign_rows: u64 = 0;
                let mut foreign_keys: BTreeSet<OptionKey> = BTreeSet::new();
                for (side, ts) in sides.iter().zip(stamps.iter()) {
                    if let Some((us, _aware)) = ts
                        && utc_month(*us) != (year, month)
                    {
                        foreign_rows += 1;
                        foreign_keys.insert((side.clone(), *ts));
                    }
                }
                if foreign_rows > 0 {
                    let index = payload.findings.len();
                    payload.findings.push(FindingRec::OptionForeign {
                        path: path.display().to_string(),
                        series: series.clone(),
                        year,
                        month,
                        foreign_rows,
                        overlap_rows: None,
                    });
                    pending.push((index, foreign_keys));
                }
            }
        }
        for (index, foreign_keys) in pending {
            let overlap = classify_option_overlap(
                payload,
                pause,
                &series_dir,
                &foreign_keys,
                &mut canonical_cache,
            )?;
            if let FindingRec::OptionForeign { overlap_rows, .. } = &mut payload.findings[index] {
                *overlap_rows = overlap;
            }
            payload.events.push(EventRec::Finding { index });
        }
    }
    Ok(())
}

/// The whole scan (`scan_lake_invariants` core twin — census + drift fold
/// + report build stay Python).
pub fn scan_lake_impl(cfg: &ScanConfig, pause: Pause<'_>) -> Result<ScanPayload, ScanFail> {
    let mut payload = ScanPayload::default();
    let mut canon_root: Option<PathBuf> = None;
    if cfg.include_bars {
        scan_bars(cfg, &mut payload, &mut canon_root, pause)?;
        payload.drift_insert_at = payload.findings.len();
        payload.events.push(EventRec::Drift);
    }
    if cfg.include_options {
        scan_options(cfg, &mut payload, pause)?;
    }
    Ok(payload)
}

// ────────────────────────────────────────────────────────────────────────
// FFI
// ────────────────────────────────────────────────────────────────────────

fn coords_into_dict(dict: &Bound<'_, PyDict>, coords: &BarsCoords) -> PyResult<()> {
    dict.set_item("exchange", &coords.exchange)?;
    dict.set_item("segment", &coords.segment)?;
    dict.set_item("symbol", &coords.symbol)?;
    dict.set_item("interval", &coords.interval)?;
    dict.set_item("year", coords.year)?;
    dict.set_item("month", coords.month)?;
    Ok(())
}

fn finding_to_py<'py>(py: Python<'py>, finding: &FindingRec) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    match finding {
        FindingRec::BarsDup {
            path,
            coords,
            physical_rows,
            unique_keys,
        } => {
            dict.set_item("kind", "bars_dup")?;
            dict.set_item("path", path)?;
            coords_into_dict(&dict, coords)?;
            dict.set_item("physical_rows", physical_rows)?;
            dict.set_item("unique_keys", unique_keys)?;
        }
        FindingRec::BarsForeign {
            path,
            coords,
            foreign_rows,
            overlap_rows,
        } => {
            dict.set_item("kind", "bars_foreign")?;
            dict.set_item("path", path)?;
            coords_into_dict(&dict, coords)?;
            dict.set_item("foreign_rows", foreign_rows)?;
            dict.set_item("overlap_rows", *overlap_rows)?;
        }
        FindingRec::Price {
            path,
            coords,
            bad_rows,
        } => {
            dict.set_item("kind", "price")?;
            dict.set_item("path", path)?;
            coords_into_dict(&dict, coords)?;
            dict.set_item("bad_rows", bad_rows)?;
        }
        FindingRec::AlienBars {
            path,
            coords,
            reason,
        } => {
            dict.set_item("kind", "alien_bars")?;
            dict.set_item("path", path)?;
            coords_into_dict(&dict, coords)?;
            dict.set_item("reason", reason)?;
        }
        FindingRec::AlienOption {
            path,
            series,
            reason,
        } => {
            dict.set_item("kind", "alien_option")?;
            dict.set_item("path", path)?;
            dict.set_item("series", series)?;
            dict.set_item("reason", reason)?;
        }
        FindingRec::OptionDup {
            path,
            series,
            physical_rows,
            unique_keys,
        } => {
            dict.set_item("kind", "option_dup")?;
            dict.set_item("path", path)?;
            dict.set_item("series", series)?;
            dict.set_item("physical_rows", physical_rows)?;
            dict.set_item("unique_keys", unique_keys)?;
        }
        FindingRec::OptionForeign {
            path,
            series,
            year,
            month,
            foreign_rows,
            overlap_rows,
        } => {
            dict.set_item("kind", "option_foreign")?;
            dict.set_item("path", path)?;
            dict.set_item("series", series)?;
            dict.set_item("year", year)?;
            dict.set_item("month", month)?;
            dict.set_item("foreign_rows", foreign_rows)?;
            dict.set_item("overlap_rows", *overlap_rows)?;
        }
    }
    Ok(dict)
}

fn payload_to_py(py: Python<'_>, payload: &ScanPayload) -> PyResult<Py<PyDict>> {
    let dict = PyDict::new(py);
    let findings = PyList::empty(py);
    for finding in &payload.findings {
        findings.append(finding_to_py(py, finding)?)?;
    }
    dict.set_item("findings", findings)?;
    dict.set_item("drift_insert_at", payload.drift_insert_at)?;
    let events = PyList::empty(py);
    for event in &payload.events {
        let record = PyDict::new(py);
        match event {
            EventRec::Log {
                event,
                path,
                path_repr,
                error,
                error_repr,
            } => {
                record.set_item("kind", "log")?;
                record.set_item("event", *event)?;
                record.set_item("path", path)?;
                record.set_item("path_repr", *path_repr)?;
                record.set_item("error", error.as_deref())?;
                record.set_item("error_repr", *error_repr)?;
            }
            EventRec::Finding { index } => {
                record.set_item("kind", "finding")?;
                record.set_item("index", index)?;
            }
            EventRec::Drift => {
                record.set_item("kind", "drift")?;
            }
        }
        events.append(record)?;
    }
    dict.set_item("events", events)?;
    dict.set_item("files_scanned", payload.files_scanned)?;
    dict.set_item("files_skipped", payload.files_skipped)?;
    dict.set_item("series_scanned", payload.series_scanned)?;
    dict.set_item("option_files_scanned", payload.option_files_scanned)?;
    dict.set_item("option_files_skipped", payload.option_files_skipped)?;
    let by_segment: Vec<(String, u64)> = payload
        .files_by_segment
        .iter()
        .map(|(segment, count)| (segment.clone(), *count))
        .collect();
    dict.set_item("files_by_segment", by_segment)?;
    dict.set_item("engine_files", payload.engine_files)?;
    dict.set_item("engine_files_skipped", payload.engine_files_skipped)?;
    dict.set_item(
        "first_skipped_parse",
        payload.first_skipped_parse.as_deref(),
    )?;
    dict.set_item("first_skipped_read", payload.first_skipped_read.as_deref())?;
    let symbol_dirs: Vec<(String, String, Vec<String>)> = payload
        .symbol_dirs
        .iter()
        .map(|((exchange, segment), symbols)| {
            (
                exchange.clone(),
                segment.clone(),
                symbols.iter().cloned().collect(),
            )
        })
        .collect();
    dict.set_item("symbol_dirs", symbol_dirs)?;
    dict.set_item("reads_performed", payload.reads_performed)?;
    Ok(dict.unbind())
}

/// Scan the lake's `bars/` (+ optionally `options/`) trees for invariant
/// findings — the `scan_lake_invariants` walk core. Returns ONE dict:
/// ordered finding records + the ordered replay-event stream + every
/// `LakeScanReport` counter + `symbol_dirs` (the shell drift-fold input) +
/// `reads_performed` (the §F2 read-count wire).
///
/// Raises `ValueError` on any out-of-envelope shape (the facade degrades
/// to the Python oracle — verdict convergence), re-raises a raising
/// *pause_cb* verbatim, and `RuntimeError` if the scan panics (caught at
/// this boundary — a panic never crosses the FFI). Runs without the GIL
/// when *pause_cb* is `None`; with a pause callback the scan stays
/// attached (the callback re-enters Python per read — the L3 throttle's
/// own `time.sleep` releases the GIL while sleeping).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub(crate) fn waved_scan_lake(
    py: Python<'_>,
    lake_root: String,
    exchange: Option<String>,
    segment: Option<String>,
    symbol: Option<String>,
    interval: Option<String>,
    months: Option<Vec<(i64, u32)>>,
    include_bars: bool,
    include_options: bool,
    valid_exchanges: Vec<String>,
    valid_segments: Vec<String>,
    valid_intervals: Vec<String>,
    forbidden_symbol_chars: String,
    engine_coords: Vec<(String, String, String)>,
    pause_cb: Option<Py<PyAny>>,
) -> PyResult<Py<PyDict>> {
    let cfg = ScanConfig {
        lake_root: PathBuf::from(lake_root),
        exchange,
        segment,
        symbol,
        interval,
        months: months.map(|pairs| pairs.into_iter().collect()),
        include_bars,
        include_options,
        valid_exchanges: valid_exchanges.into_iter().collect(),
        valid_segments: valid_segments.into_iter().collect(),
        valid_intervals: valid_intervals.into_iter().collect(),
        forbidden_symbol_chars: forbidden_symbol_chars.chars().collect(),
        engine_coords: engine_coords.into_iter().collect(),
    };
    let raised: std::cell::RefCell<Option<PyErr>> = std::cell::RefCell::new(None);
    let result = match &pause_cb {
        None => py.detach(|| std::panic::catch_unwind(|| scan_lake_impl(&cfg, &mut || Ok(())))),
        Some(cb) => {
            let mut pause = || -> Result<(), ScanFail> {
                cb.call0(py).map(|_| ()).map_err(|err| {
                    *raised.borrow_mut() = Some(err);
                    ScanFail::PauseRaised
                })
            };
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                scan_lake_impl(&cfg, &mut pause)
            }))
        }
    };
    let payload = match result {
        Ok(Ok(payload)) => payload,
        Ok(Err(ScanFail::Degrade(reason))) => {
            return Err(PyValueError::new_err(format!(
                "brutex-lake waved scan degraded: {reason}"
            )));
        }
        Ok(Err(ScanFail::PauseRaised)) => {
            return Err(match raised.into_inner() {
                Some(err) => err,
                None => {
                    PyRuntimeError::new_err("brutex-lake waved scan pause raise lost (defensive)")
                }
            });
        }
        Err(_) => {
            return Err(PyRuntimeError::new_err(
                "brutex-lake waved scan panicked (caught at the FFI boundary)",
            ));
        }
    };
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| payload_to_py(py, &payload))).map_err(
        |_| {
            PyRuntimeError::new_err(
                "brutex-lake waved scan result construction panicked (caught at the FFI boundary)",
            )
        },
    )?
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A per-test unique scratch dir (std only — the `locks.rs` idiom).
    fn scratch_dir(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "brutex-lake-waved-scan-{tag}-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// 2024-06-03 04:01 UTC, epoch µs.
    const JUNE_US: i64 = 1_717_387_260_000_000;
    /// 2024-07-03 04:03 UTC, epoch µs.
    const JULY_US: i64 = 1_719_979_380_000_000;
    /// 2024-05-02 04:01 UTC, epoch µs.
    const MAY_US: i64 = 1_714_622_460_000_000;

    fn cfg_for(lake: &Path) -> ScanConfig {
        ScanConfig {
            lake_root: lake.to_path_buf(),
            exchange: None,
            segment: None,
            symbol: None,
            interval: None,
            months: None,
            include_bars: true,
            include_options: true,
            valid_exchanges: ["NSE", "BSE"].iter().map(|s| s.to_string()).collect(),
            valid_segments: ["INDEX", "CASH", "FNO"]
                .iter()
                .map(|s| s.to_string())
                .collect(),
            valid_intervals: ["1minute", "1day"].iter().map(|s| s.to_string()).collect(),
            forbidden_symbol_chars: "/\\:*?\"<>|\0[]".chars().collect(),
            engine_coords: [("NSE", "INDEX", "NIFTY")]
                .iter()
                .map(|(a, b, c)| (a.to_string(), b.to_string(), c.to_string()))
                .collect(),
        }
    }

    fn run(cfg: &ScanConfig) -> ScanPayload {
        scan_lake_impl(cfg, &mut || Ok(()))
            .map_err(|fail| match fail {
                ScanFail::Degrade(reason) => reason,
                ScanFail::PauseRaised => "pause raised".to_string(),
            })
            .unwrap()
    }

    fn bars_frame(ts_us: &[Option<i64>], price: f64) -> DataFrame {
        let dtype = DataType::Datetime(TimeUnit::Microseconds, Some(TimeZone::UTC));
        let ts = Series::new("timestamp".into(), ts_us.to_vec())
            .cast(&dtype)
            .unwrap();
        let prices: Vec<f64> = vec![price; ts_us.len()];
        DataFrame::new_infer_height(vec![
            ts.into(),
            Column::new("open".into(), prices.clone()),
            Column::new("high".into(), prices.clone()),
            Column::new("low".into(), prices.clone()),
            Column::new("close".into(), prices),
        ])
        .unwrap()
    }

    fn write_frame(path: &Path, df: &DataFrame) {
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let file = std::fs::File::create(path).unwrap();
        ParquetWriter::new(file).finish(&mut df.clone()).unwrap();
    }

    fn bars_dest(lake: &Path, year: i64, month: u32) -> PathBuf {
        lake.join(format!(
            "bars/NSE/INDEX/NIFTY/1minute/{year:04}/{month:02}.parquet"
        ))
    }

    fn option_frame(rows: &[(Option<&str>, Option<i64>)]) -> DataFrame {
        let sides: Vec<Option<&str>> = rows.iter().map(|(side, _)| *side).collect();
        let ts_us: Vec<Option<i64>> = rows.iter().map(|(_, ts)| *ts).collect();
        let dtype = DataType::Datetime(TimeUnit::Microseconds, Some(TimeZone::UTC));
        let ts = Series::new("timestamp".into(), ts_us).cast(&dtype).unwrap();
        DataFrame::new_infer_height(vec![Column::new("side".into(), sides), ts.into()]).unwrap()
    }

    #[test]
    fn clean_lake_scans_with_zero_findings() {
        let dir = scratch_dir("clean");
        write_frame(
            &bars_dest(&dir, 2024, 6),
            &bars_frame(&[Some(JUNE_US), Some(JUNE_US + 60_000_000)], 100.0),
        );
        let payload = run(&cfg_for(&dir));
        assert!(payload.findings.is_empty());
        assert_eq!(payload.files_scanned, 1);
        assert_eq!(payload.files_skipped, 0);
        assert_eq!(payload.series_scanned, 1);
        assert_eq!(payload.engine_files, 1);
        assert_eq!(payload.reads_performed, 1);
        assert_eq!(payload.files_by_segment.get("INDEX"), Some(&1));
        assert_eq!(
            payload.symbol_dirs[&("NSE".to_string(), "INDEX".to_string())]
                .iter()
                .collect::<Vec<_>>(),
            ["NIFTY"]
        );
        // One Drift marker only (clean lake — no log/finding events).
        assert_eq!(payload.events.len(), 1);
        assert!(matches!(payload.events[0], EventRec::Drift));
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn bars_dup_foreign_overlap_and_price_flow() {
        let dir = scratch_dir("barsflow");
        // June canonical file holds the overlapping key.
        write_frame(
            &bars_dest(&dir, 2024, 6),
            &bars_frame(&[Some(JUNE_US), Some(JUNE_US + 60_000_000)], 100.0),
        );
        // July stray: one dup'd July key + one June foreign key that ALSO
        // lives in the June canonical (overlap 1).
        write_frame(
            &bars_dest(&dir, 2024, 7),
            &bars_frame(&[Some(JULY_US), Some(JULY_US), Some(JUNE_US)], 100.0),
        );
        let payload = run(&cfg_for(&dir));
        assert_eq!(payload.files_scanned, 2);
        // 2 main reads + 1 targeted canonical read.
        assert_eq!(payload.reads_performed, 3);
        assert_eq!(payload.findings.len(), 2);
        match &payload.findings[0] {
            FindingRec::BarsDup {
                physical_rows,
                unique_keys,
                ..
            } => {
                assert_eq!((*physical_rows, *unique_keys), (3, 2));
            }
            _ => panic!("expected dup first"),
        }
        match &payload.findings[1] {
            FindingRec::BarsForeign {
                foreign_rows,
                overlap_rows,
                ..
            } => {
                assert_eq!(*foreign_rows, 1);
                assert_eq!(*overlap_rows, Some(1));
            }
            _ => panic!("expected foreign second"),
        }
        // Events: dup at classification, foreign at overlap resolution,
        // Drift after the bars phase.
        let kinds: Vec<&str> = payload
            .events
            .iter()
            .map(|event| match event {
                EventRec::Log { .. } => "log",
                EventRec::Finding { .. } => "finding",
                EventRec::Drift => "drift",
            })
            .collect();
        assert_eq!(kinds, ["finding", "finding", "drift"]);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn price_census_counts_violations() {
        let dir = scratch_dir("price");
        let dtype = DataType::Datetime(TimeUnit::Microseconds, Some(TimeZone::UTC));
        let ts = Series::new(
            "timestamp".into(),
            vec![
                Some(JUNE_US),
                Some(JUNE_US + 60_000_000),
                Some(JUNE_US + 120_000_000),
            ],
        )
        .cast(&dtype)
        .unwrap();
        let df = DataFrame::new_infer_height(vec![
            ts.into(),
            Column::new("open".into(), [100.0, -5.0, 100.0]),
            Column::new("high".into(), [101.0, -1.0, 1.0]),
            Column::new("low".into(), [99.0, -10.0, 50.0]),
            Column::new("close".into(), [100.5, -2.0, 60.0]),
        ])
        .unwrap();
        write_frame(&bars_dest(&dir, 2024, 6), &df);
        let payload = run(&cfg_for(&dir));
        assert_eq!(payload.findings.len(), 1);
        match &payload.findings[0] {
            FindingRec::Price { bad_rows, .. } => assert_eq!(*bad_rows, 2),
            _ => panic!("expected a price finding"),
        }
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn parse_fail_skips_with_engine_attribution() {
        let dir = scratch_dir("parsefail");
        // month-13 file under the ENGINE series: PartitionError class.
        write_frame(
            &dir.join("bars/NSE/INDEX/NIFTY/1minute/2024/13.parquet"),
            &bars_frame(&[Some(JUNE_US)], 100.0),
        );
        let payload = run(&cfg_for(&dir));
        assert_eq!(payload.files_scanned, 0);
        assert_eq!(payload.files_skipped, 1);
        assert_eq!(payload.engine_files_skipped, 1);
        assert!(
            payload
                .first_skipped_parse
                .as_deref()
                .unwrap()
                .ends_with("13.parquet")
        );
        assert_eq!(payload.reads_performed, 0);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn ambiguous_year_dir_degrades() {
        let dir = scratch_dir("ambiguous");
        write_frame(
            &dir.join("bars/NSE/INDEX/NIFTY/1minute/ 2024/06.parquet"),
            &bars_frame(&[Some(JUNE_US)], 100.0),
        );
        let err = scan_lake_impl(&cfg_for(&dir), &mut || Ok(()))
            .map(|_| ())
            .unwrap_err();
        match err {
            ScanFail::Degrade(reason) => assert!(reason.contains("int()-ambiguous")),
            ScanFail::PauseRaised => panic!("expected degrade"),
        }
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn missing_columns_classify_alien_with_exact_reason() {
        let dir = scratch_dir("alien");
        let dtype = DataType::Datetime(TimeUnit::Microseconds, Some(TimeZone::UTC));
        let ts = Series::new("timestamp".into(), vec![Some(JUNE_US)])
            .cast(&dtype)
            .unwrap();
        let df = DataFrame::new_infer_height(vec![ts.into()]).unwrap();
        write_frame(&bars_dest(&dir, 2024, 6), &df);
        let payload = run(&cfg_for(&dir));
        assert_eq!(payload.files_skipped, 1);
        // Read-fail alien notes the first skip + engine attribution.
        assert_eq!(payload.engine_files_skipped, 1);
        assert!(payload.first_skipped_read.is_some());
        match &payload.findings[0] {
            FindingRec::AlienBars { reason, .. } => {
                assert_eq!(reason, "missing column(s): open, high, low, close");
            }
            _ => panic!("expected an alien finding"),
        }
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn dtype_alien_reason_uses_python_repr_without_first_skip_note() {
        let dir = scratch_dir("dtypealien");
        // Utf8 OHLC columns: the alien reason must carry the POLARS-PYTHON
        // dtype spelling ("String"), and the dtype-alien flow neither notes
        // a first skip nor attributes the engine skip (the Python flow's
        // exact asymmetry).
        let dtype = DataType::Datetime(TimeUnit::Microseconds, Some(TimeZone::UTC));
        let ts = Series::new("timestamp".into(), vec![Some(JUNE_US)])
            .cast(&dtype)
            .unwrap();
        let text = Column::new("open".into(), ["junk"]);
        let df = DataFrame::new_infer_height(vec![
            ts.into(),
            text,
            Column::new("high".into(), ["junk"]),
            Column::new("low".into(), ["junk"]),
            Column::new("close".into(), ["junk"]),
        ])
        .unwrap();
        write_frame(&bars_dest(&dir, 2024, 6), &df);
        let payload = run(&cfg_for(&dir));
        assert_eq!(payload.files_skipped, 1);
        assert_eq!(payload.engine_files_skipped, 0);
        assert!(payload.first_skipped_read.is_none());
        assert!(payload.first_skipped_parse.is_none());
        match &payload.findings[0] {
            FindingRec::AlienBars { reason, .. } => {
                assert_eq!(reason, "column open dtype String is not numeric");
            }
            _ => panic!("expected an alien finding"),
        }
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn out_of_range_timestamp_skips_loudly() {
        let dir = scratch_dir("oor");
        write_frame(
            &bars_dest(&dir, 2024, 7),
            &bars_frame(&[Some(1 << 62)], 100.0),
        );
        let payload = run(&cfg_for(&dir));
        assert_eq!(payload.files_scanned, 0);
        assert_eq!(payload.files_skipped, 1);
        assert_eq!(payload.engine_files_skipped, 1);
        assert!(payload.findings.is_empty());
        assert!(matches!(
            payload.events[0],
            EventRec::Log {
                event: EVENT_SCAN_SKIP,
                path_repr: false,
                ..
            }
        ));
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn months_scope_bounds_reads_and_feeds_symbol_dirs() {
        let dir = scratch_dir("scope");
        write_frame(
            &bars_dest(&dir, 2024, 6),
            &bars_frame(&[Some(JUNE_US)], 100.0),
        );
        write_frame(
            &bars_dest(&dir, 2024, 7),
            &bars_frame(&[Some(JULY_US)], 100.0),
        );
        let mut cfg = cfg_for(&dir);
        cfg.months = Some([(2024i64, 7u32)].into_iter().collect());
        let payload = run(&cfg);
        assert_eq!(payload.files_scanned, 1);
        assert_eq!(payload.reads_performed, 1);
        // The scoped-out June file still fed symbol_dirs (the drift-fold law).
        assert!(
            payload
                .symbol_dirs
                .contains_key(&("NSE".to_string(), "INDEX".to_string()))
        );
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn option_dup_foreign_multi_month_overlap_and_null_side() {
        let dir = scratch_dir("optflow");
        let series = dir.join("options/SERIES");
        // May + July canonical siblings each hold one overlapping key.
        write_frame(
            &series.join("2024-05.parquet"),
            &option_frame(&[(Some("CE"), Some(MAY_US))]),
        );
        write_frame(
            &series.join("2024-07.parquet"),
            &option_frame(&[(Some("CE"), Some(JULY_US))]),
        );
        // June: a dup'd key + a NULL-side foreign key (no canonical twin)
        // + two overlapping foreign keys routed to TWO months.
        write_frame(
            &series.join("2024-06.parquet"),
            &option_frame(&[
                (Some("CE"), Some(JUNE_US)),
                (Some("CE"), Some(JUNE_US)),
                (None, Some(JULY_US + 60_000_000)),
                (Some("CE"), Some(MAY_US)),
                (Some("CE"), Some(JULY_US)),
            ]),
        );
        let payload = run(&cfg_for(&dir));
        assert_eq!(payload.option_files_scanned, 3);
        // 3 main reads + 2 targeted canonical reads (May + July, memoised).
        assert_eq!(payload.reads_performed, 5);
        let mut dup = None;
        let mut foreign = None;
        for finding in &payload.findings {
            match finding {
                FindingRec::OptionDup {
                    physical_rows,
                    unique_keys,
                    ..
                } => dup = Some((*physical_rows, *unique_keys)),
                FindingRec::OptionForeign {
                    foreign_rows,
                    overlap_rows,
                    ..
                } => foreign = Some((*foreign_rows, *overlap_rows)),
                _ => {}
            }
        }
        assert_eq!(dup, Some((5, 4)));
        assert_eq!(foreign, Some((3, Some(2))));
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn option_stem_law_matrix() {
        assert_eq!(option_file_month("2024-06.parquet"), Some((2024, 6)));
        assert_eq!(option_file_month("0001-01.parquet"), Some((1, 1)));
        assert_eq!(option_file_month("9999-12.parquet"), Some((9999, 12)));
        assert_eq!(option_file_month("2024-6.parquet"), None); // non-canonical
        assert_eq!(option_file_month("2024-13.parquet"), None); // month range
        assert_eq!(option_file_month("12024-06.parquet"), None); // year range
        assert_eq!(option_file_month("0000-06.parquet"), None); // year 0
        assert_eq!(option_file_month("²²-06.parquet"), None); // unicode digits
        assert_eq!(option_file_month("2024x-06.parquet"), None);
        assert_eq!(option_file_month("-06.parquet"), None);
        assert_eq!(option_file_month(".parquet"), None);
        assert_eq!(option_file_month("2024-06.txt"), None);
    }

    #[test]
    fn option_gate_verdicts() {
        // Null side + Null ts are SOUND; an out-of-range Datetime verdicts.
        let null_side = DataFrame::new_infer_height(vec![
            Column::full_null("side".into(), 1, &DataType::Null),
            Column::full_null("timestamp".into(), 1, &DataType::Null),
        ])
        .unwrap();
        assert_eq!(option_key_gate(&null_side).map_err(|_| ()).unwrap(), None);
        let oor = option_frame(&[(Some("CE"), Some(1 << 62))]);
        assert_eq!(
            option_key_gate(&oor).map_err(|_| ()).unwrap().unwrap(),
            "option timestamp value out of datetime range — unclassifiable"
        );
        let int_side = DataFrame::new_infer_height(vec![
            Column::new("side".into(), [1i64]),
            Column::full_null("timestamp".into(), 1, &DataType::Null),
        ])
        .unwrap();
        assert_eq!(
            option_key_gate(&int_side).map_err(|_| ()).unwrap().unwrap(),
            "option side column dtype Int64 is not String/Null — unclassifiable"
        );
    }

    #[cfg(unix)]
    #[test]
    fn symlink_probes_skip_loudly() {
        use std::os::unix::fs::symlink;

        let dir = scratch_dir("symlinks");
        let outside = dir.join("outside");
        std::fs::create_dir_all(&outside).unwrap();
        write_frame(
            &outside.join("2024-06.parquet"),
            &option_frame(&[(Some("CE"), Some(JUNE_US)), (Some("CE"), Some(JUNE_US))]),
        );
        let series = dir.join("lake/options/SERIES");
        std::fs::create_dir_all(&series).unwrap();
        // A dangling file symlink + a real dup sibling in the SAME series.
        symlink(dir.join("absent.parquet"), series.join("2024-05.parquet")).unwrap();
        write_frame(
            &series.join("2024-06.parquet"),
            &option_frame(&[(Some("CE"), Some(JUNE_US)), (Some("CE"), Some(JUNE_US))]),
        );
        // A symlinked series dir beside it.
        symlink(&outside, dir.join("lake/options/LINKED")).unwrap();
        let cfg = cfg_for(&dir.join("lake"));
        let payload = run(&cfg);
        assert_eq!(payload.option_files_scanned, 1);
        assert_eq!(payload.option_files_skipped, 1);
        assert!(matches!(
            payload.findings[0],
            FindingRec::OptionDup {
                physical_rows: 2,
                unique_keys: 1,
                ..
            }
        ));
        let symlink_events = payload
            .events
            .iter()
            .filter(|event| {
                matches!(
                    event,
                    EventRec::Log {
                        event: EVENT_SYMLINK_SKIPPED,
                        path_repr: true,
                        ..
                    }
                )
            })
            .count();
        assert_eq!(symlink_events, 2); // the LINKED series dir + the dangling file
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn symlinked_options_root_skips_raw() {
        use std::os::unix::fs::symlink;

        let dir = scratch_dir("rootlink");
        let outside = dir.join("outside-options/SERIES");
        std::fs::create_dir_all(&outside).unwrap();
        write_frame(
            &outside.join("2024-06.parquet"),
            &option_frame(&[(Some("CE"), Some(JUNE_US)), (Some("CE"), Some(JUNE_US))]),
        );
        let lake = dir.join("lake");
        std::fs::create_dir_all(&lake).unwrap();
        symlink(dir.join("outside-options"), lake.join("options")).unwrap();
        let payload = run(&cfg_for(&lake));
        assert_eq!(payload.option_files_scanned, 0);
        assert!(payload.findings.is_empty());
        assert!(matches!(
            payload.events.as_slice(),
            [
                EventRec::Drift,
                EventRec::Log {
                    event: EVENT_SYMLINK_SKIPPED,
                    path_repr: false,
                    ..
                }
            ]
        ));
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn pause_ticks_after_every_read_attempt() {
        let dir = scratch_dir("pause");
        // A bars foreign (1 targeted canonical read) + a garbage option
        // sibling read attempt: ticks = 2 bars reads + 1 bars canonical +
        // 2 option reads + 1 failed option canonical = 6.
        write_frame(
            &bars_dest(&dir, 2024, 6),
            &bars_frame(&[Some(JUNE_US)], 100.0),
        );
        write_frame(
            &bars_dest(&dir, 2024, 7),
            &bars_frame(&[Some(JULY_US), Some(JUNE_US)], 100.0),
        );
        let series = dir.join("options/SERIES");
        std::fs::create_dir_all(&series).unwrap();
        std::fs::write(series.join("2024-05.parquet"), b"garbage").unwrap();
        write_frame(
            &series.join("2024-06.parquet"),
            &option_frame(&[(Some("CE"), Some(JUNE_US)), (Some("PE"), Some(MAY_US))]),
        );
        let mut ticks = 0u64;
        let payload = scan_lake_impl(&cfg_for(&dir), &mut || {
            ticks += 1;
            Ok(())
        })
        .map_err(|_| ())
        .unwrap();
        assert_eq!(payload.reads_performed, 6);
        assert_eq!(ticks, 6);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn int_twin_matrix() {
        assert!(matches!(int_twin("2024"), IntParse::Value(2024)));
        assert!(matches!(int_twin("02024"), IntParse::Value(2024)));
        assert!(matches!(int_twin(""), IntParse::Fail));
        assert!(matches!(int_twin("20x4"), IntParse::Fail));
        assert!(matches!(int_twin("999999999999999999999"), IntParse::Fail));
        assert!(matches!(int_twin(" 2024"), IntParse::Ambiguous));
        assert!(matches!(int_twin("+2024"), IntParse::Ambiguous));
        assert!(matches!(int_twin("2_024"), IntParse::Ambiguous));
        assert!(matches!(int_twin("٢٠٢٤"), IntParse::Ambiguous));
        assert!(matches!(int_twin("²²"), IntParse::Ambiguous));
    }

    #[test]
    fn python_dtype_repr_matrix() {
        // Pinned against the polars-python `str(dtype)` spellings (probed
        // 2026-07-19 on the installed 1.42.1 wheel).
        assert_eq!(python_dtype_repr(&DataType::Int64).unwrap(), "Int64");
        assert_eq!(python_dtype_repr(&DataType::String).unwrap(), "String");
        assert_eq!(python_dtype_repr(&DataType::Boolean).unwrap(), "Boolean");
        assert_eq!(
            python_dtype_repr(&DataType::Datetime(
                TimeUnit::Microseconds,
                Some(TimeZone::UTC)
            ))
            .unwrap(),
            "Datetime(time_unit='us', time_zone='UTC')"
        );
        assert_eq!(
            python_dtype_repr(&DataType::Datetime(TimeUnit::Milliseconds, None)).unwrap(),
            "Datetime(time_unit='ms', time_zone=None)"
        );
        assert_eq!(
            python_dtype_repr(&DataType::Duration(TimeUnit::Nanoseconds)).unwrap(),
            "Duration(time_unit='ns')"
        );
        // Decimal is feature-gated OFF in this crate build — no mapping
        // exists (unmapped dtypes degrade to the oracle at the call sites).
        assert!(python_dtype_repr(&DataType::List(Box::new(DataType::String))).is_none());
    }
}
