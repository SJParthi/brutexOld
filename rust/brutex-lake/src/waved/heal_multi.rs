//! # Wave-D W-2b — the `lake_heal` MULTI-FILE heal algorithms
//!
//! DEC-RUST-LAKE-WAVED-001 (design §B2): the three heal algorithms W-2 left
//! Python-only — the D-7 flip blocker — ported natively:
//!
//! * [`heal_bars_foreign`] — the `_heal_foreign` twin: copy-forward FIRST
//!   (keep-first grouped-by-UTC-month writes through the byte-locked bars
//!   render core — `_write_bars_grouped_by_month`/`ParquetWriter.write`
//!   semantics), remove SECOND (`heal_partition` with `remove_timestamps`) —
//!   the crash-convergent ordering law.
//! * [`heal_option_foreign`] — the `_heal_option_foreign` twin: keep-first
//!   `(side, timestamp)` dedup of the consistent read, timestamp-only foreign
//!   routing, ascending-month canonical-sibling merges
//!   (`merge_partition_frame` semantics — the `added` baseline on UNIQUE
//!   stored keys + the FORCE-rewrite of a dup-poisoned destination even at
//!   `added == 0`, with the pinned `lake.partition.dedup_healed` event), then
//!   the RAW-keyed removal (`heal_partition` with `remove_keys` — keyed,
//!   never positional; a wholly-removed file is quarantine-MOVED).
//! * [`heal_drift`] — the `_heal_drift` twin: `1minute`+`1day` files MERGED
//!   keep-first into the canonical symbol (grouped by month), every other
//!   interval quarantine-MOVED without merging, legacy dirs pruned, legacy
//!   catalog rows deleted.
//!
//! Every rewrite renders through the EXISTING byte-locked render cores
//! (`render_parquet_from_ipc_impl` bars / `render_parquet_defaults_from_ipc_impl`
//! options-defaults) — never a new parquet-render path — and every side
//! effect (structlog, audit, catalog, stats/manifest hooks, `record_dataset`)
//! streams through the ONE `on_event` channel the shell replays (house laws
//! i-iii).
//!
//! ## The D-22 preflight posture (recorded deviation, `waved_deviations.md`)
//!
//! The multi-file heals can touch files the SCANNER never validated (merge
//! destinations, drifted-dir members), and their Python error paths embed
//! exception-MESSAGE text in the pinned `lake_heal.partition_error` log —
//! text Rust cannot reproduce byte-exactly. So [`preflight_bars_foreign`] /
//! [`preflight_option_foreign`] / [`preflight_drift`] refuse the FINDING
//! PRE-mutation (read-only probes; since the W-6a endgame the batch loop
//! converts the verdict into a per-finding ISOLATED error and CONTINUES —
//! the Python core these shapes used to degrade to is deleted) for every
//! such shape: an
//! unreadable/symlinked source or destination, an out-of-envelope bars file
//! (schema/dtype/ts-range/price-band/CandleBar-law), a schema-incompatible
//! option merge sibling, an `int()`-ambiguous drifted month name, a
//! non-UTF-8 path component, a `*.parquet`-NAMED directory. Only clean
//! shapes engage; a shape that mutates AFTER preflight (a mid-batch swap) is
//! a per-finding isolated error whose TEXT may diverge — the disclosed
//! out-of-differential envelope (the W-2 unreadable-dup convention).

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::io::ErrorKind;
use std::path::{Path, PathBuf};
use std::time::Instant;

use polars::prelude::*;
use pyo3::prelude::*;

use super::civil_from_days;
use super::heal::{
    BarRow, BatchCtx, Envelope, EventSink, Finding, HealErr, PRESERVE_MULTI_REASON,
    acquire_partition_flock, assert_options_containment, atomic_write_bytes, classify_io,
    collapse_keep_first, obj, push_action, quarantine_move, read_bars_file, render_bars,
    sha256_hex, to_us,
};
use super::read::frame_to_ipc;
use super::scan::{IntParse, int_twin};

/// Microseconds per civil day (UTC month routing).
const US_PER_DAY: i64 = 86_400_000_000;

/// The two MERGED drift intervals — `lake_heal._MERGE_INTERVALS`.
const MERGE_INTERVALS: [&str; 2] = ["1minute", "1day"];

/// The `(side, timestamp)` unique-key subset (the option writers' merge key).
fn option_subset() -> Vec<String> {
    vec!["side".into(), "timestamp".into()]
}

/// The `partition_for_timestamp(aware_utc)` twin: UTC `(year, month)` of an
/// epoch-µs instant (naive stored values route as-if-UTC — the `utc_aware`
/// law; the physical value IS the UTC instant for tz-aware columns).
pub(super) fn month_of_us(us: i64) -> (i32, u32) {
    let (year, month, _day) = civil_from_days(us.div_euclid(US_PER_DAY));
    (year as i32, month)
}

/// `option_partition_stem(year, month)` — the §11 COMMON `YYYY-MM` formatter.
pub(super) fn option_stem(year: i32, month: u32) -> String {
    format!("{year:04}-{month:02}")
}

/// `build_partition_path` twin for a bars month partition.
fn bars_partition_path(
    lake_root: &Path,
    exchange: &str,
    segment: &str,
    symbol: &str,
    interval: &str,
    year: i32,
    month: u32,
) -> PathBuf {
    lake_root
        .join("bars")
        .join(exchange)
        .join(segment)
        .join(symbol)
        .join(interval)
        .join(format!("{year:04}"))
        .join(format!("{month:02}.parquet"))
}

/// `Path.is_symlink()` on the LITERAL path (lstat — ignorable faults False).
fn is_symlink_quiet(path: &Path) -> bool {
    std::fs::symlink_metadata(path)
        .map(|meta| meta.file_type().is_symlink())
        .unwrap_or(false)
}

/// The `_interval_merge_order` sort rank: `1minute` → 0, `1day` → 1, rest 2.
fn interval_merge_rank(name: &str) -> usize {
    MERGE_INTERVALS
        .iter()
        .position(|known| *known == name)
        .unwrap_or(MERGE_INTERVALS.len())
}

/// Round a wall-clock ms figure to 3 decimals (the write-telemetry shape;
/// timings are excluded from every differential comparison).
fn round3(value: f64) -> f64 {
    (value * 1000.0).round() / 1000.0
}

// ── preflight probes (read-only; Some(reason) = a per-finding isolated refusal since W-6a) ──

/// Probe one bars merge DESTINATION: absent = fine (fresh write), a symlink /
/// unreadable / out-of-envelope file degrades.
fn preflight_bars_dest(dest: &Path) -> Option<String> {
    match std::fs::symlink_metadata(dest) {
        Err(err) if err.kind() == ErrorKind::NotFound => return None,
        Err(_) => return Some("bars_foreign: destination unprobeable".to_string()),
        Ok(meta) if meta.file_type().is_symlink() => {
            return Some("bars_foreign: destination is a symlink".to_string());
        }
        Ok(_) => {}
    }
    match read_bars_file(dest) {
        Ok((_, _, _, Envelope::Ok)) => None,
        // W-6b: a preserve-class DESTINATION (overlay / big-price) refuses —
        // the merge legs rebuild values through `BarRow` and would drop the
        // overlay columns (the recorded residual; single-file dedup only).
        Ok((_, _, _, Envelope::Preserve)) => {
            Some(format!("bars_envelope: {}", PRESERVE_MULTI_REASON))
        }
        Ok((_, _, _, Envelope::Degrade(reason))) => Some(format!("bars_envelope: {reason}")),
        // A naive-ts DESTINATION is NOT crate-owned (W-5 phase A): the
        // Python arm errors MID-copy-forward — after earlier months may
        // already have been written — a partial-mutation shape this slice
        // deliberately leaves to the verbatim Python core.
        Err(HealErr::Refuse(_)) => {
            Some("bars_foreign: naive-timestamp destination outside the crate envelope".to_string())
        }
        Err(_) => Some("bars_foreign: destination unreadable".to_string()),
    }
}

/// Preflight one `ForeignMonthRows` finding (design §B2 / deviation D-22).
pub(super) fn preflight_bars_foreign(lake_root: &Path, finding: &Finding) -> Option<String> {
    if !finding.path.exists() {
        return None; // both arms skip a vanished source identically
    }
    if is_symlink_quiet(&finding.path) {
        return Some("bars_foreign: source is a symlink".to_string());
    }
    let rows = match read_bars_file(&finding.path) {
        Ok((_, _, rows, Envelope::Ok)) => rows,
        // W-6b: a preserve-class SOURCE refuses (see `preflight_bars_dest`).
        Ok((_, _, _, Envelope::Preserve)) => {
            return Some(format!("bars_envelope: {}", PRESERVE_MULTI_REASON));
        }
        Ok((_, _, _, Envelope::Degrade(reason))) => {
            return Some(format!("bars_envelope: {reason}"));
        }
        // A naive-ts SOURCE (W-5 phase A, `bars_naive_timestamp_routing`):
        // the heal refuses PRE-mutation with the byte-exact Python message
        // (`HealErr::Refuse` at the per-finding loop) — the crate owns the
        // verdict, so the batch ENGAGES; no destination is ever probed
        // because the heal errors before touching one.
        Err(HealErr::Refuse(_)) => return None,
        Err(_) => return Some("bars_foreign: source unreadable".to_string()),
    };
    let home = (finding.year, finding.month);
    let mut months: BTreeSet<(i32, u32)> = BTreeSet::new();
    for row in &rows {
        let month = month_of_us(row.ts_us);
        if month != home {
            months.insert(month);
        }
    }
    for (year, month) in months {
        let dest = bars_partition_path(
            lake_root,
            &finding.exchange,
            &finding.segment,
            &finding.symbol,
            &finding.interval,
            year,
            month,
        );
        if let Some(reason) = preflight_bars_dest(&dest) {
            return Some(reason);
        }
    }
    None
}

/// The frame-level options envelope: `side` dtype `String`, `timestamp`
/// dtype `Datetime` (any unit/tz), every non-null value µs-representable
/// AND inside Python's `datetime` range.
fn option_frame_envelope(frame: &DataFrame) -> Option<String> {
    let Ok(side) = frame.column("side") else {
        return Some("option side column missing".to_string());
    };
    if !matches!(side.dtype(), DataType::String) {
        return Some(format!(
            "option side dtype {:?} outside the envelope",
            side.dtype()
        ));
    }
    let Ok(ts) = frame.column("timestamp") else {
        return Some("option timestamp column missing".to_string());
    };
    let DataType::Datetime(unit, _tz) = ts.dtype() else {
        return Some(format!(
            "option timestamp dtype {:?} outside the envelope",
            ts.dtype()
        ));
    };
    let Ok(phys) = std::convert::identity(ts.to_physical_repr().i64().cloned()) else {
        return Some("option timestamp physical repr not i64".to_string());
    };
    for value in phys.iter().flatten() {
        let Some(us) = to_us(value, *unit) else {
            return Some("option timestamp overflows µs".to_string());
        };
        if !(super::read::DATETIME_MIN_US..=super::read::DATETIME_MAX_US).contains(&us) {
            return Some("option timestamp outside the datetime range".to_string());
        }
    }
    None
}

/// Preflight one `OptionForeignMonthRows` finding (deviation D-22).
pub(super) fn preflight_option_foreign(finding: &Finding) -> Option<String> {
    let src = &finding.path;
    match std::fs::symlink_metadata(src) {
        Err(err) if err.kind() == ErrorKind::NotFound => return None,
        Err(_) => return Some("option_foreign: source unprobeable".to_string()),
        Ok(meta) if meta.file_type().is_symlink() => {
            return Some("option_foreign: source is a symlink".to_string());
        }
        Ok(_) => {}
    }
    if src.parent().is_some_and(is_symlink_quiet) {
        return Some("option_foreign: series dir is a symlink".to_string());
    }
    let Ok(bytes) = std::fs::read(src) else {
        return Some("option_foreign: source unreadable".to_string());
    };
    let Ok(frame) = ParquetReader::new(std::io::Cursor::new(&bytes)).finish() else {
        return Some("option_foreign: source unreadable".to_string());
    };
    if let Some(reason) = option_frame_envelope(&frame) {
        return Some(format!("option_envelope: {reason}"));
    }
    let home = (finding.year, finding.month);
    let Ok(keys) = option_row_keys(&frame) else {
        return Some("option_envelope: option timestamp overflows µs".to_string());
    };
    let mut months: BTreeSet<(i32, u32)> = BTreeSet::new();
    for key in &keys {
        if let Some(us) = key.2 {
            let month = month_of_us(us);
            if month != home {
                months.insert(month);
            }
        }
    }
    let Some(series_dir) = src.parent() else {
        return Some("option_foreign: source has no parent dir".to_string());
    };
    for (year, month) in months {
        let dest = series_dir.join(format!("{}.parquet", option_stem(year, month)));
        match std::fs::symlink_metadata(&dest) {
            Err(err) if err.kind() == ErrorKind::NotFound => continue, // fresh
            Err(_) => return Some("option_foreign: destination unprobeable".to_string()),
            Ok(meta) if meta.file_type().is_symlink() => {
                // A symlink-LOOP dest is crate-owned (W-5 phase A,
                // `symlink_loop_partition_file`): the containment rail
                // reproduces Python's `Symlink loop from '…'` refusal
                // byte-exactly, so the batch ENGAGES — stop probing (the
                // heal refuses at THIS month, months processed in the same
                // ascending order on both arms, so later months are never
                // reached by either). Any other symlink dest stays the
                // degrade envelope (its refusal wording depends on the
                // link's resolution — not reproduced here).
                match std::fs::canonicalize(&dest) {
                    Err(err) if err.raw_os_error() == Some(libc::ELOOP) => break,
                    _ => return Some("option_foreign: destination is a symlink".to_string()),
                }
            }
            Ok(_) => {}
        }
        let Ok(dest_bytes) = std::fs::read(&dest) else {
            return Some("option_foreign: destination unreadable".to_string());
        };
        let Ok(dest_frame) = ParquetReader::new(std::io::Cursor::new(&dest_bytes)).finish() else {
            return Some("option_foreign: destination unreadable".to_string());
        };
        if dest_frame.schema() != frame.schema() {
            // A schema-mismatched merge sibling makes the Python concat raise
            // a PolarsError-message-bearing isolated error (the documented
            // naive-vs-aware never-converging hand-plant included) — degrade.
            return Some("option_foreign: destination schema mismatch".to_string());
        }
        if let Some(reason) = option_frame_envelope(&dest_frame) {
            return Some(format!("option_envelope: {reason}"));
        }
    }
    None
}

/// Preflight one `LegacyKeyDrift` finding (deviation D-22).
pub(super) fn preflight_drift(lake_root: &Path, finding: &Finding) -> Option<String> {
    let dir = &finding.path;
    match std::fs::symlink_metadata(dir) {
        Err(err) if err.kind() == ErrorKind::NotFound => return None,
        Err(_) => return Some("drift: drifted dir unprobeable".to_string()),
        Ok(meta) if meta.file_type().is_symlink() => {
            return Some("drift: drifted dir is a symlink".to_string());
        }
        Ok(_) => {}
    }
    if !std::fs::metadata(dir).map(|m| m.is_dir()).unwrap_or(false) {
        return None; // both arms skip a non-dir identically
    }
    let entries = match std::fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(_) => return Some("drift: drifted dir unreadable".to_string()),
    };
    // (interval, month) destination pairs the merge would touch.
    let mut dest_months: BTreeSet<(String, i32, u32)> = BTreeSet::new();
    for entry in entries {
        let Ok(entry) = entry else {
            return Some("drift: drifted dir unreadable".to_string());
        };
        let path = entry.path();
        if is_symlink_quiet(&path) {
            return Some("drift: symlink inside the drifted dir".to_string());
        }
        if !std::fs::metadata(&path)
            .map(|m| m.is_dir())
            .unwrap_or(false)
        {
            continue; // Python iterates interval DIRS only — stray files stay
        }
        let Some(interval) = path.file_name().and_then(|n| n.to_str()) else {
            return Some("drift: non-UTF-8 interval dir name".to_string());
        };
        let interval = interval.to_string();
        let files = match collect_parquet_entries_sorted(&path) {
            Ok(files) => files,
            Err(reason) => return Some(reason),
        };
        for file in files {
            if !std::fs::metadata(&file)
                .map(|m| m.is_file())
                .unwrap_or(false)
            {
                return Some("drift: *.parquet-named non-file entry".to_string());
            }
            let (Some(parent_name), Some(stem)) = (
                file.parent()
                    .and_then(|p| p.file_name())
                    .and_then(|n| n.to_str()),
                file.file_stem().and_then(|n| n.to_str()),
            ) else {
                return Some("drift: non-UTF-8 file path component".to_string());
            };
            for token in [parent_name, stem] {
                if matches!(int_twin(token), IntParse::Ambiguous) {
                    return Some("drift: int()-ambiguous month path token".to_string());
                }
            }
            if MERGE_INTERVALS.contains(&interval.as_str()) {
                let rows = match read_bars_file(&file) {
                    Ok((_, _, rows, Envelope::Ok)) => rows,
                    // W-6b: a preserve-class LEGACY file refuses (see
                    // `preflight_bars_dest` — the drift merge is `BarRow`-
                    // rebuilt and cannot preserve overlay columns).
                    Ok((_, _, _, Envelope::Preserve)) => {
                        return Some(format!("bars_envelope: {}", PRESERVE_MULTI_REASON));
                    }
                    Ok((_, _, _, Envelope::Degrade(reason))) => {
                        return Some(format!("bars_envelope: {reason}"));
                    }
                    // A naive-ts LEGACY file is NOT crate-owned here (W-5
                    // phase A): the Python drift merge errors MID-way with
                    // per-file partial mutation possible — degrade.
                    Err(HealErr::Refuse(_)) => {
                        return Some(
                            "drift: naive-timestamp legacy file outside the crate envelope"
                                .to_string(),
                        );
                    }
                    Err(_) => return Some("drift: legacy file unreadable".to_string()),
                };
                for row in &rows {
                    let (year, month) = month_of_us(row.ts_us);
                    dest_months.insert((interval.clone(), year, month));
                }
            } else if std::fs::File::open(&file).is_err() {
                return Some("drift: legacy file unreadable".to_string());
            }
        }
    }
    for (interval, year, month) in dest_months {
        let dest = bars_partition_path(
            lake_root,
            &finding.exchange,
            &finding.segment,
            &finding.canonical,
            &interval,
            year,
            month,
        );
        if let Some(reason) = preflight_bars_dest(&dest) {
            return Some(reason);
        }
    }
    None
}

/// Collect every `*.parquet`-named entry under *dir* (recursive), ascending
/// by full-path bytes — the `sorted(interval_dir.rglob("*.parquet"))` twin
/// (UTF-8 byte order == code-point order; component paths share the same
/// `/` separators, so the full-string sort matches Python's `sorted(Path)`).
/// Degrade-shaped errors return `Err(reason)` (the preflight consumes them;
/// heal-time callers map to an isolated error).
fn collect_parquet_entries_sorted(dir: &Path) -> Result<Vec<PathBuf>, String> {
    let mut out: Vec<PathBuf> = Vec::new();
    let mut stack: Vec<PathBuf> = vec![dir.to_path_buf()];
    while let Some(current) = stack.pop() {
        let entries =
            std::fs::read_dir(&current).map_err(|_| "drift: drifted dir unreadable".to_string())?;
        for entry in entries {
            let entry = entry.map_err(|_| "drift: drifted dir unreadable".to_string())?;
            let path = entry.path();
            if path.as_os_str().to_str().is_none() {
                return Err("drift: non-UTF-8 file path component".to_string());
            }
            let is_dir = std::fs::metadata(&path)
                .map(|m| m.is_dir())
                .unwrap_or(false);
            if path
                .file_name()
                .map(|n| n.to_string_lossy().ends_with(".parquet"))
                .unwrap_or(false)
            {
                out.push(path.clone());
            }
            if is_dir {
                stack.push(path);
            }
        }
    }
    out.sort_by(|a, b| {
        a.as_os_str()
            .as_encoded_bytes()
            .cmp(b.as_os_str().as_encoded_bytes())
    });
    Ok(out)
}

// ── bars write primitives (`ParquetWriter.write` keep-first semantics) ───────

/// One bars write outcome — the `WriteResult` fields the replay needs.
pub(super) struct BarsWriteOutcome {
    pub(super) row_count: i64,
    pub(super) raw_hash: String,
    pub(super) first_ts_us: i64,
    pub(super) last_ts_us: i64,
    pub(super) bars_added: i64,
    pub(super) conflicts: i64,
    pub(super) file_rewritten: bool,
}

/// `_bar_values_equal` — OHLCV+OI equality on the snapped values (exact in
/// f64 by the read envelope; the timestamp is the key).
fn bar_values_equal(a: &BarRow, b: &BarRow) -> bool {
    a.open == b.open
        && a.high == b.high
        && a.low == b.low
        && a.close == b.close
        && a.volume == b.volume
        && a.oi == b.oi
}

/// The `lake.partition.duplicate_collapsed` read-side WARN
/// (`_read_existing_bars`' `_log_duplicate_collapse` law — every decode site).
fn log_duplicate_collapse(
    sink: &EventSink<'_, '_>,
    dest: &Path,
    physical: i64,
    unique: i64,
) -> PyResult<()> {
    if physical > unique {
        sink.log(
            "lake.partition.duplicate_collapsed",
            &[
                ("partition", obj(sink.py, dest.to_string_lossy().as_ref())),
                ("physical_rows", obj(sink.py, physical)),
                ("unique_timestamps", obj(sink.py, unique)),
                ("collapsed", obj(sink.py, physical - unique)),
            ],
        )?;
    }
    Ok(())
}

/// The `read_partition_bars` twin: consistent read under the flock,
/// keep-first collapsed (collapse logged). Mid-batch envelope violations are
/// isolated errors (preflight verified the steady shape).
pub(super) fn read_partition_bars_locked(
    ctx: &BatchCtx,
    sink: &EventSink<'_, '_>,
    dest: &Path,
) -> Result<Vec<BarRow>, HealErr> {
    let _flock = acquire_partition_flock(&ctx.lake_root, dest, sink)?;
    let (_bytes, _hash, rows, envelope) = read_bars_file(dest)?;
    match envelope {
        Envelope::Ok => {}
        // W-6b defensive: a preserve-class file slipping past the preflight
        // (mid-batch swap) is the same isolated posture — `rows` would be
        // EMPTY here, so silently continuing would fabricate an empty file.
        Envelope::Preserve => {
            return Err(HealErr::Isolated(format!(
                "bars envelope violated mid-batch: {}",
                PRESERVE_MULTI_REASON
            )));
        }
        Envelope::Degrade(reason) => {
            return Err(HealErr::Isolated(format!(
                "bars envelope violated mid-batch: {reason}"
            )));
        }
    }
    let physical = rows.len() as i64;
    let collapsed = collapse_keep_first(rows);
    log_duplicate_collapse(sink, dest, physical, collapsed.len() as i64)?;
    Ok(collapsed)
}

/// Emit the `bars_stats_record` replay event (the `_record_stats_hook` law —
/// probe-first + enabled-gated SHELL-side) for a just-rendered partition.
fn emit_bars_stats_record(
    sink: &EventSink<'_, '_>,
    dest: &Path,
    outcome: &BarsWriteOutcome,
) -> PyResult<()> {
    let stats = sink.dict();
    stats.set_item("path", dest.to_string_lossy().as_ref())?;
    stats.set_item("rows", outcome.row_count)?;
    stats.set_item("first_ts_us", outcome.first_ts_us)?;
    stats.set_item("last_ts_us", outcome.last_ts_us)?;
    stats.set_item("raw_hash", outcome.raw_hash.as_str())?;
    sink.emit("bars_stats_record", &stats)
}

/// Emit the `catalog_upsert` replay event (Golden Rule #4 — the shell builds
/// the `CatalogEntry` through the writer's own `make_catalog_entry`).
#[allow(clippy::too_many_arguments)]
fn emit_catalog_upsert(
    sink: &EventSink<'_, '_>,
    exchange: &str,
    segment: &str,
    symbol: &str,
    interval: &str,
    year: i32,
    month: u32,
    outcome: &BarsWriteOutcome,
) -> PyResult<()> {
    let catalog = sink.dict();
    catalog.set_item("exchange", exchange)?;
    catalog.set_item("segment", segment)?;
    catalog.set_item("symbol", symbol)?;
    catalog.set_item("interval", interval)?;
    catalog.set_item("year", year)?;
    catalog.set_item("month", month)?;
    catalog.set_item("raw_hash", outcome.raw_hash.as_str())?;
    catalog.set_item("row_count", outcome.row_count)?;
    catalog.set_item("first_ts_us", outcome.first_ts_us)?;
    catalog.set_item("last_ts_us", outcome.last_ts_us)?;
    sink.emit("catalog_upsert", &catalog)
}

/// Emit the `catalog_delete` replay event.
#[allow(clippy::too_many_arguments)]
fn emit_catalog_delete(
    sink: &EventSink<'_, '_>,
    exchange: &str,
    segment: &str,
    symbol: &str,
    interval: &str,
    year: i64,
    month: i64,
) -> PyResult<()> {
    let payload = sink.dict();
    payload.set_item("exchange", exchange)?;
    payload.set_item("segment", segment)?;
    payload.set_item("symbol", symbol)?;
    payload.set_item("interval", interval)?;
    payload.set_item("year", year)?;
    payload.set_item("month", month)?;
    sink.emit("catalog_delete", &payload)
}

/// The `_merge_write(merge_mode="keep-first")` twin over an EXISTING
/// destination: stored keep-first collapse (+ collapse WARN), the fold
/// (existing wins — a value mismatch counts a conflict, never restated),
/// the dup-poisoned FORCE-rewrite even at `added == 0` (+ the pinned
/// `lake.partition.dedup_healed` event), the no-op fast path byte-untouched.
fn merge_write_bars(
    sink: &EventSink<'_, '_>,
    dest: &Path,
    batch: &[BarRow],
) -> Result<BarsWriteOutcome, HealErr> {
    let (existing_bytes, _existing_hash, rows, envelope) = read_bars_file(dest)?;
    match envelope {
        Envelope::Ok => {}
        // W-6b defensive: a preserve-class file slipping past the preflight
        // (mid-batch swap) is the same isolated posture — `rows` would be
        // EMPTY here, so silently continuing would fabricate an empty file.
        Envelope::Preserve => {
            return Err(HealErr::Isolated(format!(
                "bars envelope violated mid-batch: {}",
                PRESERVE_MULTI_REASON
            )));
        }
        Envelope::Degrade(reason) => {
            return Err(HealErr::Isolated(format!(
                "bars envelope violated mid-batch: {reason}"
            )));
        }
    }
    let physical = rows.len() as i64;
    let mut merged = collapse_keep_first(rows);
    let stored_unique = merged.len() as i64;
    log_duplicate_collapse(sink, dest, physical, stored_unique)?;
    let heal_dropped = physical - stored_unique;
    let mut index: HashMap<i64, usize> = HashMap::with_capacity(merged.len());
    for (slot, row) in merged.iter().enumerate() {
        index.insert(row.ts_us, slot);
    }
    let mut added: i64 = 0;
    let mut conflicts: i64 = 0;
    for bar in batch {
        match index.get(&bar.ts_us) {
            None => {
                index.insert(bar.ts_us, merged.len());
                merged.push(bar.clone());
                added += 1;
            }
            Some(&slot) => {
                if !bar_values_equal(&merged[slot], bar) {
                    // keep-first: the STORED bar stays; count the conflict.
                    conflicts += 1;
                }
                // Identical values (overlay-free by the envelope): no-op.
            }
        }
    }
    merged.sort_by_key(|row| row.ts_us);
    if added == 0 && heal_dropped == 0 {
        return Ok(BarsWriteOutcome {
            row_count: stored_unique,
            raw_hash: sha256_hex(&existing_bytes),
            first_ts_us: merged.first().map(|r| r.ts_us).unwrap_or(0),
            last_ts_us: merged.last().map(|r| r.ts_us).unwrap_or(0),
            bars_added: 0,
            conflicts,
            file_rewritten: false,
        });
    }
    if heal_dropped > 0 {
        sink.log(
            "lake.partition.dedup_healed",
            &[
                ("partition", obj(sink.py, dest.to_string_lossy().as_ref())),
                ("kept", obj(sink.py, stored_unique)),
                ("dropped", obj(sink.py, heal_dropped)),
            ],
        )?;
    }
    let rendered = render_bars(&merged)?;
    atomic_write_bytes(dest, &rendered)?;
    Ok(BarsWriteOutcome {
        row_count: merged.len() as i64,
        raw_hash: sha256_hex(&rendered),
        first_ts_us: merged.first().map(|r| r.ts_us).unwrap_or(0),
        last_ts_us: merged.last().map(|r| r.ts_us).unwrap_or(0),
        bars_added: added,
        conflicts,
        file_rewritten: true,
    })
}

/// `dataset_created` probe (`ParquetWriter.write` FRESH branch): True iff
/// the interval dir holds ZERO `*.parquet`-named entries BEFORE the write.
fn interval_dir_has_no_parquet(dest: &Path) -> bool {
    let Some(interval_dir) = dest.parent().and_then(|p| p.parent()) else {
        return false;
    };
    let mut stack: Vec<PathBuf> = vec![interval_dir.to_path_buf()];
    while let Some(current) = stack.pop() {
        let Ok(entries) = std::fs::read_dir(&current) else {
            continue;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path
                .file_name()
                .map(|n| n.to_string_lossy().ends_with(".parquet"))
                .unwrap_or(false)
            {
                return false;
            }
            if std::fs::metadata(&path)
                .map(|m| m.is_dir())
                .unwrap_or(false)
            {
                stack.push(path);
            }
        }
    }
    true
}

/// One `ParquetWriter.write(merge_mode="keep-first")` twin: mkdir → locks →
/// merge/fresh → stats hook (under the flock) → the `lake.partition.write`
/// telemetry (post-lock, INFO) → `record_dataset` when the write CREATED the
/// dataset. Heal batches derive from timestamp-keyed dicts, so the
/// within-batch duplicate arm (`lake.vendor_duplicate_rows`) is structurally
/// unreachable (debug-asserted, documented).
#[allow(clippy::too_many_arguments)]
fn write_partition_bars(
    ctx: &BatchCtx,
    sink: &EventSink<'_, '_>,
    exchange: &str,
    segment: &str,
    symbol: &str,
    interval: &str,
    year: i32,
    month: u32,
    batch: &[BarRow],
) -> Result<BarsWriteOutcome, HealErr> {
    let mut sorted_batch = batch.to_vec();
    sorted_batch.sort_by_key(|row| row.ts_us);
    debug_assert!(
        sorted_batch.windows(2).all(|w| w[0].ts_us != w[1].ts_us),
        "heal write batches derive from ts-keyed dicts — never duplicated"
    );
    let dest = bars_partition_path(
        &ctx.lake_root,
        exchange,
        segment,
        symbol,
        interval,
        year,
        month,
    );
    if let Some(parent) = dest.parent() {
        std::fs::create_dir_all(parent).map_err(classify_io)?;
    }
    let lock_start = Instant::now();
    let flock = acquire_partition_flock(&ctx.lake_root, &dest, sink)?;
    let lock_wait_ms = lock_start.elapsed().as_secs_f64() * 1000.0;
    let write_start = Instant::now();
    let mut dataset_created = false;
    let outcome = if dest.exists() {
        merge_write_bars(sink, &dest, &sorted_batch)?
    } else {
        dataset_created = interval_dir_has_no_parquet(&dest);
        let rendered = render_bars(&sorted_batch)?;
        atomic_write_bytes(&dest, &rendered)?;
        BarsWriteOutcome {
            row_count: sorted_batch.len() as i64,
            raw_hash: sha256_hex(&rendered),
            first_ts_us: sorted_batch.first().map(|r| r.ts_us).unwrap_or(0),
            last_ts_us: sorted_batch.last().map(|r| r.ts_us).unwrap_or(0),
            bars_added: sorted_batch.len() as i64,
            conflicts: 0,
            file_rewritten: true,
        }
    };
    let append_ms = write_start.elapsed().as_secs_f64() * 1000.0;
    if outcome.file_rewritten {
        // Under the still-held flock — the #1614 under-lock capture law.
        emit_bars_stats_record(sink, &dest, &outcome)?;
    }
    drop(flock);
    sink.log_ex(
        "lake.partition.write",
        Some("info"),
        &[],
        &[
            ("append_ms", obj(sink.py, round3(append_ms))),
            ("lock_wait_ms", obj(sink.py, round3(lock_wait_ms))),
            ("rows", obj(sink.py, sorted_batch.len() as i64)),
            ("partition_rows", obj(sink.py, outcome.row_count)),
            ("partition", obj(sink.py, dest.to_string_lossy().as_ref())),
        ],
    )?;
    if dataset_created {
        let payload = sink.dict();
        payload.set_item("exchange", exchange)?;
        payload.set_item("segment", segment)?;
        payload.set_item("symbol", symbol)?;
        payload.set_item("interval", interval)?;
        sink.emit("record_dataset", &payload)?;
    }
    Ok(outcome)
}

/// The `_write_bars_grouped_by_month` twin: group by UTC month, write each
/// keep-first ascending, upsert the catalog per month (the shell gates a
/// `None` catalog). Returns `(months, rows added, conflicts)`.
#[allow(clippy::type_complexity)]
fn write_bars_grouped_by_month(
    ctx: &BatchCtx,
    sink: &EventSink<'_, '_>,
    exchange: &str,
    segment: &str,
    symbol: &str,
    interval: &str,
    bars: &[BarRow],
) -> Result<(BTreeSet<(i32, u32)>, i64, i64), HealErr> {
    let mut groups: BTreeMap<(i32, u32), Vec<BarRow>> = BTreeMap::new();
    for bar in bars {
        groups
            .entry(month_of_us(bar.ts_us))
            .or_default()
            .push(bar.clone());
    }
    let mut added: i64 = 0;
    let mut conflicts: i64 = 0;
    for ((year, month), group) in &groups {
        let outcome = write_partition_bars(
            ctx, sink, exchange, segment, symbol, interval, *year, *month, group,
        )?;
        added += outcome.bars_added;
        conflicts += outcome.conflicts;
        emit_catalog_upsert(
            sink, exchange, segment, symbol, interval, *year, *month, &outcome,
        )?;
    }
    Ok((groups.into_keys().collect(), added, conflicts))
}

/// One bars heal outcome — the `PartitionHealResult` fields the replay needs.
pub(super) struct BarsHealOutcome {
    pub(super) changed: bool,
    pub(super) kept: i64,
    pub(super) dropped_duplicates: i64,
    pub(super) dropped_removed: i64,
    pub(super) raw_hash_before: String,
    pub(super) raw_hash_after: Option<String>,
    pub(super) write: Option<BarsWriteOutcome>,
}

/// The `ParquetWriter.heal_partition` twin with keyed removal: keep-first
/// collapse + drop `remove` timestamps → unchanged = byte-untouched →
/// quarantine-copy the ORIGINAL bytes → rewrite, or quarantine-MOVE a
/// wholly-removed file (`bars_stats_remove` replays the manifest + stats
/// hooks). The footer probe ≡ the decode on identical bytes (deviation D-9).
fn heal_partition_bars(
    ctx: &BatchCtx,
    sink: &EventSink<'_, '_>,
    dest: &Path,
    remove: &BTreeSet<i64>,
    quarantine: &Path,
) -> Result<BarsHealOutcome, HealErr> {
    let _flock = acquire_partition_flock(&ctx.lake_root, dest, sink)?;
    let (existing_bytes, raw_hash_before, rows, envelope) = read_bars_file(dest)?;
    match envelope {
        Envelope::Ok => {}
        // W-6b defensive: a preserve-class file slipping past the preflight
        // (mid-batch swap) is the same isolated posture — `rows` would be
        // EMPTY here, so silently continuing would fabricate an empty file.
        Envelope::Preserve => {
            return Err(HealErr::Isolated(format!(
                "bars envelope violated mid-batch: {}",
                PRESERVE_MULTI_REASON
            )));
        }
        Envelope::Degrade(reason) => {
            return Err(HealErr::Isolated(format!(
                "bars envelope violated mid-batch: {reason}"
            )));
        }
    }
    let physical = rows.len() as i64;
    let mut merged = collapse_keep_first(rows);
    let stored_unique = merged.len() as i64;
    log_duplicate_collapse(sink, dest, physical, stored_unique)?;
    let dropped_duplicates = physical - stored_unique;
    let before_remove = merged.len();
    merged.retain(|row| !remove.contains(&row.ts_us));
    let dropped_removed = (before_remove - merged.len()) as i64;
    if dropped_duplicates == 0 && dropped_removed == 0 {
        return Ok(BarsHealOutcome {
            changed: false,
            kept: stored_unique,
            dropped_duplicates: 0,
            dropped_removed: 0,
            raw_hash_before: raw_hash_before.clone(),
            raw_hash_after: Some(raw_hash_before),
            write: None,
        });
    }
    if let Some(parent) = quarantine.parent() {
        std::fs::create_dir_all(parent).map_err(classify_io)?;
    }
    atomic_write_bytes(quarantine, &existing_bytes)?;
    if merged.is_empty() {
        // Every row removed (a wholly-foreign file): quarantine-MOVE — the
        // writer never renders an empty partition (write()'s law).
        std::fs::remove_file(dest).map_err(classify_io)?;
        if let Some(parent) = dest.parent() {
            let _ = std::fs::remove_dir(parent); // prune a now-empty <yyyy>/
        }
        let payload = sink.dict();
        payload.set_item("path", dest.to_string_lossy().as_ref())?;
        sink.emit("bars_stats_remove", &payload)?;
        return Ok(BarsHealOutcome {
            changed: true,
            kept: 0,
            dropped_duplicates,
            dropped_removed,
            raw_hash_before,
            raw_hash_after: None,
            write: None,
        });
    }
    merged.sort_by_key(|row| row.ts_us);
    let rendered = render_bars(&merged)?;
    atomic_write_bytes(dest, &rendered)?;
    if dropped_duplicates > 0 {
        sink.log(
            "lake.partition.dedup_healed",
            &[
                ("partition", obj(sink.py, dest.to_string_lossy().as_ref())),
                ("kept", obj(sink.py, stored_unique)),
                ("dropped", obj(sink.py, dropped_duplicates)),
            ],
        )?;
    }
    let write = BarsWriteOutcome {
        row_count: merged.len() as i64,
        raw_hash: sha256_hex(&rendered),
        first_ts_us: merged.first().map(|r| r.ts_us).unwrap_or(0),
        last_ts_us: merged.last().map(|r| r.ts_us).unwrap_or(0),
        bars_added: 0, // a heal never adds bars (Golden Rule #1)
        conflicts: 0,
        file_rewritten: true,
    };
    emit_bars_stats_record(sink, dest, &write)?;
    Ok(BarsHealOutcome {
        changed: true,
        kept: merged.len() as i64,
        dropped_duplicates,
        dropped_removed,
        raw_hash_before,
        raw_hash_after: Some(write.raw_hash.clone()),
        write: Some(write),
    })
}

// ── option primitives (`OptionParquetWriter` heal-side semantics) ────────────

/// One RAW stored option key: `(side, tz-aware?, epoch/wall µs)` — the value
/// twin of the Python `(side, timestamp)` tuple (aware datetimes compare by
/// instant, naive by wall time; the awareness flag keeps the two spaces
/// disjoint exactly as Python's aware≠naive inequality does). A null
/// timestamp is `None` (never a removal key — `is_foreign_timestamp`
/// abstains on nulls).
type OptionKey = (Option<String>, bool, Option<i64>);

/// Project a frame's `(side, timestamp)` rows into [`OptionKey`]s.
fn option_row_keys(frame: &DataFrame) -> Result<Vec<OptionKey>, HealErr> {
    let side = frame
        .column("side")
        .map_err(HealErr::from)?
        .str()
        .map_err(HealErr::from)?
        .clone();
    let ts = frame.column("timestamp").map_err(HealErr::from)?;
    let (unit, aware) = match ts.dtype() {
        DataType::Datetime(unit, tz) => (*unit, tz.is_some()),
        other => {
            return Err(HealErr::Isolated(format!(
                "option envelope violated mid-batch: timestamp dtype {other:?}"
            )));
        }
    };
    let phys_col = ts.to_physical_repr();
    let phys = phys_col.i64().map_err(HealErr::from)?;
    let mut out: Vec<OptionKey> = Vec::with_capacity(frame.height());
    for idx in 0..frame.height() {
        let us = match phys.get(idx) {
            None => None,
            Some(value) => Some(to_us(value, unit).ok_or_else(|| {
                HealErr::Isolated(
                    "option envelope violated mid-batch: timestamp overflows µs".to_string(),
                )
            })?),
        };
        out.push((side.get(idx).map(|s| s.to_string()), aware, us));
    }
    Ok(out)
}

/// Keep-first `(side, timestamp)` dedup + `(side, timestamp)` sort — the
/// option writers' collapse law.
fn option_dedup_sorted(frame: &DataFrame) -> Result<DataFrame, HealErr> {
    let subset = option_subset();
    frame
        .unique_stable(Some(&subset), UniqueKeepStrategy::First, None)
        .map_err(HealErr::from)?
        .sort(["side", "timestamp"], SortMultipleOptions::default())
        .map_err(HealErr::from)
}

/// Render an options frame through the byte-locked DEFAULTS core + replace.
fn render_option_frame(dest: &Path, frame: &DataFrame) -> Result<(), HealErr> {
    let mut render_frame = frame.clone();
    let ipc = frame_to_ipc(&mut render_frame).map_err(HealErr::from)?;
    let rendered = crate::render_parquet_defaults_from_ipc_impl(&ipc).map_err(HealErr::from)?;
    atomic_write_bytes(dest, &rendered)
}

/// Emit the `option_stats_record` replay event (the D-12 re-read replay —
/// the shell re-reads *dest* and runs the verbatim `_record_option_stats`).
fn emit_option_stats_record(sink: &EventSink<'_, '_>, dest: &Path, rows: i64) -> PyResult<()> {
    let stats = sink.dict();
    stats.set_item("path", dest.to_string_lossy().as_ref())?;
    stats.set_item("rows", rows)?;
    sink.emit("option_stats_record", &stats)
}

/// The `read_partition_frame` twin: containment + flock + RAW decode (no
/// collapse — the healer dedups explicitly).
fn read_option_frame_locked(
    ctx: &BatchCtx,
    sink: &EventSink<'_, '_>,
    dest: &Path,
) -> Result<DataFrame, HealErr> {
    let _flock = acquire_partition_flock(&ctx.lake_root, dest, sink)?;
    assert_options_containment(&ctx.lake_root, dest)?;
    let bytes = std::fs::read(dest).map_err(|err| match err.raw_os_error() {
        Some(code) if code == libc::ENOSPC || code == libc::EDQUOT => HealErr::Fatal(err),
        _ => HealErr::Isolated(format!("corrupt existing partition: {err}")),
    })?;
    ParquetReader::new(std::io::Cursor::new(&bytes))
        .finish()
        .map_err(|err| HealErr::Isolated(format!("corrupt existing partition: {err}")))
}

/// The `merge_partition_frame` twin: keep-first union at the destination
/// (an existing stored key WINS; an absent key is REASSIGNED), the `added`
/// baseline on the UNIQUE stored key count, the dup-poisoned FORCE-rewrite
/// even at `added == 0` with the pinned repr()'d `lake.partition.dedup_healed`
/// event. Rows move VERBATIM (all stored columns). Returns rows added.
fn merge_option_frame(
    ctx: &BatchCtx,
    sink: &EventSink<'_, '_>,
    dest: &Path,
    group: &DataFrame,
) -> Result<i64, HealErr> {
    let _flock = acquire_partition_flock(&ctx.lake_root, dest, sink)?;
    assert_options_containment(&ctx.lake_root, dest)?;
    let exists = std::fs::metadata(dest).is_ok();
    let (before, dedup_dropped, combined) = if exists {
        let bytes = std::fs::read(dest).map_err(|err| match err.raw_os_error() {
            Some(code) if code == libc::ENOSPC || code == libc::EDQUOT => HealErr::Fatal(err),
            _ => HealErr::Isolated(format!("corrupt existing partition: {err}")),
        })?;
        let existing = ParquetReader::new(std::io::Cursor::new(&bytes))
            .finish()
            .map_err(|err| HealErr::Isolated(format!("corrupt existing partition: {err}")))?;
        let subset = option_subset();
        let unique_stored = existing
            .unique_stable(Some(&subset), UniqueKeepStrategy::First, None)
            .map_err(HealErr::from)?;
        let before = unique_stored.height() as i64;
        let dedup_dropped = existing.height() as i64 - before;
        let mut all = existing;
        all.vstack_mut(group).map_err(HealErr::from)?;
        (before, dedup_dropped, option_dedup_sorted(&all)?)
    } else {
        (0, 0, option_dedup_sorted(group)?)
    };
    let added = combined.height() as i64 - before;
    if dedup_dropped > 0 {
        // The bars `heal_dropped` forced-rewrite mirror; partition= rides
        // repr() (the round-8 log-injection fold — dest embeds the
        // vendor-influenceable series dir name).
        sink.log(
            "lake.partition.dedup_healed",
            &[
                ("partition", obj(sink.py, dest.to_string_lossy().as_ref())),
                ("partition_repr", obj(sink.py, true)),
                ("kept", obj(sink.py, before)),
                ("dropped", obj(sink.py, dedup_dropped)),
            ],
        )?;
    }
    if added > 0 || dedup_dropped > 0 {
        if let Some(parent) = dest.parent() {
            std::fs::create_dir_all(parent).map_err(classify_io)?;
        }
        render_option_frame(dest, &combined)?;
        emit_option_stats_record(sink, dest, combined.height() as i64)?;
    }
    Ok(added)
}

/// One option heal outcome — the `OptionHealResult` fields the replay needs.
struct OptionHealOutcome {
    changed: bool,
    kept: i64,
    dropped_duplicates: i64,
    dropped_removed: i64,
}

/// The `OptionParquetWriter.heal_partition` twin with RAW-keyed removal:
/// keep-first dedup + drop the `remove` keys by stored-value membership
/// (keyed, never positional) → unchanged = byte-untouched → quarantine-copy →
/// rewrite, or quarantine-MOVE a wholly-removed file (`option_stats_remove`
/// replays the stats hook). The re-read envelope gate is the round-13 twin.
fn heal_option_partition(
    ctx: &BatchCtx,
    sink: &EventSink<'_, '_>,
    dest: &Path,
    remove: &HashSet<OptionKey>,
    quarantine: &Path,
) -> Result<OptionHealOutcome, HealErr> {
    let _flock = acquire_partition_flock(&ctx.lake_root, dest, sink)?;
    assert_options_containment(&ctx.lake_root, dest)?;
    let bytes = std::fs::read(dest).map_err(|err| match err.raw_os_error() {
        Some(code) if code == libc::ENOSPC || code == libc::EDQUOT => HealErr::Fatal(err),
        _ => HealErr::Isolated(format!("corrupt existing partition: {err}")),
    })?;
    let existing = ParquetReader::new(std::io::Cursor::new(&bytes))
        .finish()
        .map_err(|err| HealErr::Isolated(format!("corrupt existing partition: {err}")))?;
    let physical = existing.height() as i64;
    let mut deduped = option_dedup_sorted(&existing)?;
    let dropped_duplicates = physical - deduped.height() as i64;
    let mut dropped_removed: i64 = 0;
    if !remove.is_empty() {
        // The round-13 re-read gate: the keyed removal hashes raw stored
        // values, so an out-of-envelope re-read frame must refuse ISOLATED,
        // never crash the batch (preflight verified the steady shape).
        if let Some(reason) = option_frame_envelope(&deduped) {
            return Err(HealErr::Isolated(format!(
                "option envelope violated mid-batch: {reason}"
            )));
        }
        let keys = option_row_keys(&deduped)?;
        let mask: Vec<bool> = keys.iter().map(|key| !remove.contains(key)).collect();
        if mask.iter().any(|keep| !keep) {
            let before = deduped.height() as i64;
            let mask_ca = BooleanChunked::from_slice("mask".into(), &mask);
            deduped = deduped.filter(&mask_ca).map_err(HealErr::from)?;
            dropped_removed = before - deduped.height() as i64;
        }
    }
    let kept = deduped.height() as i64;
    if dropped_duplicates == 0 && dropped_removed == 0 {
        return Ok(OptionHealOutcome {
            changed: false,
            kept,
            dropped_duplicates: 0,
            dropped_removed: 0,
        });
    }
    if let Some(parent) = quarantine.parent() {
        std::fs::create_dir_all(parent).map_err(classify_io)?;
    }
    atomic_write_bytes(quarantine, &bytes)?;
    if kept == 0 {
        // Every row removed (a wholly-foreign file): quarantine-MOVE.
        std::fs::remove_file(dest).map_err(classify_io)?;
        if let Some(parent) = dest.parent() {
            let _ = std::fs::remove_dir(parent); // defensive series-dir prune
        }
        let payload = sink.dict();
        payload.set_item("path", dest.to_string_lossy().as_ref())?;
        sink.emit("option_stats_remove", &payload)?;
        return Ok(OptionHealOutcome {
            changed: true,
            kept: 0,
            dropped_duplicates,
            dropped_removed,
        });
    }
    render_option_frame(dest, &deduped)?;
    emit_option_stats_record(sink, dest, kept)?;
    Ok(OptionHealOutcome {
        changed: true,
        kept,
        dropped_duplicates,
        dropped_removed,
    })
}

// ── the three heal algorithms ────────────────────────────────────────────────

/// The `_heal_foreign` twin — reassign one bars file's foreign-month rows
/// home. Copy-forward FIRST, remove SECOND (crash-convergent). `Ok(true)`
/// iff rows moved.
pub(super) fn heal_bars_foreign(
    ctx: &mut BatchCtx,
    finding: &Finding,
    sink: &EventSink<'_, '_>,
) -> Result<bool, HealErr> {
    let src = &finding.path;
    if !src.exists() {
        return Ok(false); // consumed by an earlier drift merge
    }
    let bars = read_partition_bars_locked(ctx, sink, src)?;
    let home = (finding.year, finding.month);
    let foreign: Vec<BarRow> = bars
        .iter()
        .filter(|row| month_of_us(row.ts_us) != home)
        .cloned()
        .collect();
    if foreign.is_empty() {
        return Ok(false); // stale finding — already reassigned
    }
    let (months, added, conflicts) = write_bars_grouped_by_month(
        ctx,
        sink,
        &finding.exchange,
        &finding.segment,
        &finding.symbol,
        &finding.interval,
        &foreign,
    )?;
    let remove: BTreeSet<i64> = foreign.iter().map(|row| row.ts_us).collect();
    let quarantine = ctx.quarantine_path(src)?;
    let outcome = heal_partition_bars(ctx, sink, src, &remove, &quarantine)?;
    if !outcome.changed {
        // Probe abstain / already-removed (the `_heal_foreign` symmetry):
        // the copy-forward is keep-first idempotent, the finding stays live.
        return Ok(false);
    }
    ctx.quarantined_files += 1;
    let partition = format!(
        "bars/{}/{}/{}/{}/{:04}/{:02}.parquet",
        finding.exchange,
        finding.segment,
        finding.symbol,
        finding.interval,
        finding.year,
        finding.month
    );
    let months_str: Vec<String> = months.iter().map(|(y, m)| option_stem(*y, *m)).collect();
    let moved = foreign.len() as i64;
    sink.log(
        "lake_heal.rows_reassigned",
        &[
            ("partition", obj(sink.py, partition.as_str())),
            ("partition_repr", obj(sink.py, false)),
            ("moved", obj(sink.py, moved)),
            ("added", obj(sink.py, added)),
            ("months", obj(sink.py, months_str.clone())),
            ("conflicts", obj(sink.py, conflicts)),
        ],
    )?;
    let quarantine_str = quarantine.to_string_lossy().into_owned();
    push_action(
        sink.py,
        ctx,
        "LAKE_HEAL_REASSIGN",
        &partition,
        outcome.kept,
        outcome.dropped_duplicates + outcome.dropped_removed,
        moved,
        conflicts,
        Some(&outcome.raw_hash_before),
        outcome.raw_hash_after.as_deref(),
        Some(&quarantine_str),
    )?;
    let audit_payload = sink.dict();
    audit_payload.set_item("moved", moved)?;
    audit_payload.set_item("added", added)?;
    audit_payload.set_item("months", months_str)?;
    audit_payload.set_item("conflicts", conflicts)?;
    audit_payload.set_item("kept", outcome.kept)?;
    audit_payload.set_item("dropped_duplicates", outcome.dropped_duplicates)?;
    audit_payload.set_item("raw_hash_before", outcome.raw_hash_before.as_str())?;
    audit_payload.set_item("raw_hash_after", outcome.raw_hash_after.as_deref())?;
    audit_payload.set_item("quarantine_path", quarantine_str.as_str())?;
    sink.audit("LAKE_HEAL_REASSIGN", &partition, &audit_payload)?;
    // `_sync_catalog_after_heal` — the shell gates a None catalog.
    if outcome.raw_hash_after.is_none() {
        emit_catalog_delete(
            sink,
            &finding.exchange,
            &finding.segment,
            &finding.symbol,
            &finding.interval,
            i64::from(finding.year),
            i64::from(finding.month),
        )?;
    } else if let Some(write) = &outcome.write {
        emit_catalog_upsert(
            sink,
            &finding.exchange,
            &finding.segment,
            &finding.symbol,
            &finding.interval,
            finding.year,
            finding.month,
            write,
        )?;
    }
    if finding.interval == "1minute" {
        for (year, month) in &months {
            ctx.touch(
                &finding.exchange,
                &finding.segment,
                &finding.symbol,
                (*year, *month),
            );
        }
        ctx.touch(&finding.exchange, &finding.segment, &finding.symbol, home);
    }
    Ok(true)
}

/// The `_heal_option_foreign` twin — reassign one options file's
/// foreign-month rows home through the canonical-sibling keep-first merges.
/// `Ok(true)` iff rows moved.
pub(super) fn heal_option_foreign(
    ctx: &mut BatchCtx,
    finding: &Finding,
    sink: &EventSink<'_, '_>,
) -> Result<bool, HealErr> {
    let src = &finding.path;
    if !src.exists() {
        return Ok(false); // consumed earlier / vanished — skipped
    }
    let frame = read_option_frame_locked(ctx, sink, src)?;
    if let Some(reason) = option_frame_envelope(&frame) {
        // The round-12 consistent-read gate twin (preflight verified the
        // steady shape — a mid-batch swap refuses ISOLATED).
        return Err(HealErr::Isolated(format!(
            "option envelope violated mid-batch: {reason}"
        )));
    }
    let home = (finding.year, finding.month);
    // Keep-first (side, timestamp) dedup BEFORE the split — the first
    // physical copy of a duplicated foreign key moves ONCE (`_option_foreign_frame`).
    let subset = option_subset();
    let deduped = frame
        .unique_stable(Some(&subset), UniqueKeepStrategy::First, None)
        .map_err(HealErr::from)?;
    let all_keys = option_row_keys(&deduped)?;
    let foreign_mask: Vec<bool> = all_keys
        .iter()
        .map(|key| key.2.is_some_and(|us| month_of_us(us) != home))
        .collect();
    let mask_ca = BooleanChunked::from_slice("mask".into(), &foreign_mask);
    let foreign = deduped.filter(&mask_ca).map_err(HealErr::from)?;
    if foreign.height() == 0 {
        return Ok(false); // stale finding (a prior crashed run) — skipped
    }
    let foreign_keys = option_row_keys(&foreign)?;
    let keyset: HashSet<OptionKey> = foreign_keys.iter().cloned().collect();
    let mut rows_by_month: BTreeMap<(i32, u32), Vec<usize>> = BTreeMap::new();
    for (idx, key) in foreign_keys.iter().enumerate() {
        let us = key.2.ok_or_else(|| {
            HealErr::Isolated("option foreign row lost its timestamp mid-batch".to_string())
        })?;
        rows_by_month.entry(month_of_us(us)).or_default().push(idx);
    }
    let Some(series_dir) = src.parent() else {
        return Err(HealErr::Isolated(format!(
            "{} has no parent series dir",
            src.display()
        )));
    };
    let mut added: i64 = 0;
    for ((year, month), indices) in &rows_by_month {
        // ASCENDING month order (BTreeMap) — the deterministic merge order +
        // audit/log `months` law.
        let mut group_mask = vec![false; foreign.height()];
        for idx in indices {
            group_mask[*idx] = true;
        }
        let group_ca = BooleanChunked::from_slice("mask".into(), &group_mask);
        let group = foreign.filter(&group_ca).map_err(HealErr::from)?;
        let dest = series_dir.join(format!("{}.parquet", option_stem(*year, *month)));
        added += merge_option_frame(ctx, sink, &dest, &group)?;
    }
    let quarantine = ctx.quarantine_path(src)?;
    let outcome = heal_option_partition(ctx, sink, src, &keyset, &quarantine)?;
    if !outcome.changed {
        return Ok(false); // defensive convergence guard (the `_heal_foreign` symmetry)
    }
    ctx.quarantined_files += 1;
    let partition = format!(
        "options/{}/{}",
        finding.series,
        src.file_name()
            .map(|n| n.to_string_lossy())
            .unwrap_or_default()
    );
    let months_str: Vec<String> = rows_by_month
        .keys()
        .map(|(y, m)| option_stem(*y, *m))
        .collect();
    let moved = foreign.height() as i64; // == len(keys): the deduped frame's keys are unique
    sink.log_ex(
        "lake_heal.rows_reassigned",
        None,
        &["series"],
        &[
            ("partition", obj(sink.py, partition.as_str())),
            ("partition_repr", obj(sink.py, true)),
            ("series", obj(sink.py, finding.series.as_str())),
            ("tree", obj(sink.py, "options")),
            ("moved", obj(sink.py, moved)),
            ("added", obj(sink.py, added)),
            ("months", obj(sink.py, months_str.clone())),
            ("conflicts", obj(sink.py, 0i64)),
        ],
    )?;
    let quarantine_str = quarantine.to_string_lossy().into_owned();
    push_action(
        sink.py,
        ctx,
        "LAKE_HEAL_REASSIGN",
        &partition,
        outcome.kept,
        outcome.dropped_duplicates + outcome.dropped_removed,
        moved,
        0,
        None, // uncatalogued — the `_heal_option_dup` precedent
        None,
        Some(&quarantine_str),
    )?;
    let audit_payload = sink.dict();
    audit_payload.set_item("tree", "options")?;
    audit_payload.set_item("moved", moved)?;
    audit_payload.set_item("added", added)?;
    audit_payload.set_item("months", months_str)?;
    audit_payload.set_item("kept", outcome.kept)?;
    audit_payload.set_item("dropped_duplicates", outcome.dropped_duplicates)?;
    audit_payload.set_item("dropped_removed", outcome.dropped_removed)?;
    audit_payload.set_item("quarantine_path", quarantine_str.as_str())?;
    sink.audit("LAKE_HEAL_REASSIGN", &partition, &audit_payload)?;
    Ok(true)
}

/// The `_heal_drift` twin — merge one drifted legacy symbol dir into its
/// canonical dir. `Ok(true)` iff files moved.
pub(super) fn heal_drift(
    ctx: &mut BatchCtx,
    finding: &Finding,
    sink: &EventSink<'_, '_>,
) -> Result<bool, HealErr> {
    let drifted_dir = &finding.path;
    if !std::fs::metadata(drifted_dir)
        .map(|m| m.is_dir())
        .unwrap_or(false)
    {
        return Ok(false); // already merged (a prior run)
    }
    let mut interval_dirs: Vec<(usize, String, PathBuf)> = Vec::new();
    let entries = std::fs::read_dir(drifted_dir).map_err(classify_io)?;
    for entry in entries {
        let entry = entry.map_err(classify_io)?;
        let path = entry.path();
        if !std::fs::metadata(&path)
            .map(|m| m.is_dir())
            .unwrap_or(false)
        {
            continue; // interval DIRS only — stray files are ignored (Python law)
        }
        let name = path
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        interval_dirs.push((interval_merge_rank(&name), name, path));
    }
    interval_dirs.sort_by(|a, b| (a.0, a.1.as_bytes()).cmp(&(b.0, b.1.as_bytes())));
    let mut merged_rows: i64 = 0;
    let mut conflicts_total: i64 = 0;
    let mut files_moved: i64 = 0;
    let mut per_interval: Vec<(String, i64)> = Vec::new();
    let mut canonical_minute_months: BTreeSet<(i32, u32)> = BTreeSet::new();
    for (_rank, interval, interval_dir) in &interval_dirs {
        let files = collect_parquet_entries_sorted(interval_dir)
            .map_err(|reason| HealErr::Isolated(format!("drifted-dir walk failed: {reason}")))?;
        for file in files {
            if MERGE_INTERVALS.contains(&interval.as_str()) {
                let bars = read_partition_bars_locked(ctx, sink, &file)?;
                if !bars.is_empty() {
                    // Python sorts the collapsed values by timestamp first.
                    let mut sorted = bars;
                    sorted.sort_by_key(|row| row.ts_us);
                    let (months, moved, conflicts) = write_bars_grouped_by_month(
                        ctx,
                        sink,
                        &finding.exchange,
                        &finding.segment,
                        &finding.canonical,
                        interval,
                        &sorted,
                    )?;
                    merged_rows += moved;
                    conflicts_total += conflicts;
                    if interval == "1minute" {
                        canonical_minute_months.extend(months);
                    }
                }
            }
            quarantine_move(ctx, &file, sink)?;
            files_moved += 1;
            match per_interval.iter_mut().find(|(name, _)| name == interval) {
                Some((_, count)) => *count += 1,
                None => per_interval.push((interval.clone(), 1)),
            }
            // `_file_month` — the catalog-row delete for a parsable month.
            let parsed = file
                .parent()
                .and_then(|p| p.file_name())
                .and_then(|n| n.to_str())
                .zip(file.file_stem().and_then(|n| n.to_str()))
                .and_then(|(parent_name, stem)| {
                    match (int_twin(parent_name), int_twin(stem)) {
                        (IntParse::Value(year), IntParse::Value(month)) => Some((year, month)),
                        _ => None, // Fail = no delete; Ambiguous is preflight-degraded
                    }
                });
            if let Some((year, month)) = parsed {
                emit_catalog_delete(
                    sink,
                    &finding.exchange,
                    &finding.segment,
                    &finding.symbol, // the DRIFTED symbol's legacy rows
                    interval,
                    year,
                    month,
                )?;
            }
        }
    }
    prune_empty_dirs(drifted_dir);
    let subject = format!(
        "{}/{}/{}",
        finding.exchange, finding.segment, finding.symbol
    );
    sink.log_ex(
        "lake_heal.drift_merged",
        None,
        &["drifted", "canonical"],
        &[
            ("drifted", obj(sink.py, finding.symbol.as_str())),
            ("canonical", obj(sink.py, finding.canonical.as_str())),
            ("exchange", obj(sink.py, finding.exchange.as_str())),
            ("segment", obj(sink.py, finding.segment.as_str())),
            ("files", obj(sink.py, files_moved)),
            ("rows", obj(sink.py, merged_rows)),
            ("conflicts", obj(sink.py, conflicts_total)),
        ],
    )?;
    let quarantine_root_str = ctx.quarantine_root.to_string_lossy().into_owned();
    push_action(
        sink.py,
        ctx,
        "LAKE_HEAL_DRIFT_MERGE",
        &subject,
        0,
        0,
        merged_rows,
        conflicts_total,
        None,
        None,
        Some(&quarantine_root_str),
    )?;
    let audit_payload = sink.dict();
    audit_payload.set_item("canonical", finding.canonical.as_str())?;
    let files_by_interval = sink.dict();
    for (name, count) in &per_interval {
        files_by_interval.set_item(name, *count)?;
    }
    audit_payload.set_item("files_by_interval", files_by_interval)?;
    audit_payload.set_item("rows_merged", merged_rows)?;
    audit_payload.set_item("conflicts", conflicts_total)?;
    audit_payload.set_item("quarantine_root", quarantine_root_str.as_str())?;
    sink.audit("LAKE_HEAL_DRIFT_MERGE", &subject, &audit_payload)?;
    if !canonical_minute_months.is_empty() {
        for (year, month) in &canonical_minute_months {
            ctx.touch(
                &finding.exchange,
                &finding.segment,
                &finding.canonical,
                (*year, *month),
            );
        }
    }
    Ok(true)
}

/// `_prune_empty_dirs` twin: rmdir now-empty subdirs bottom-up, then root.
fn prune_empty_dirs(root: &Path) {
    if !std::fs::metadata(root).map(|m| m.is_dir()).unwrap_or(false) {
        return;
    }
    let mut dirs: Vec<PathBuf> = Vec::new();
    let mut stack: Vec<PathBuf> = vec![root.to_path_buf()];
    while let Some(current) = stack.pop() {
        let Ok(entries) = std::fs::read_dir(&current) else {
            continue;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if std::fs::metadata(&path)
                .map(|m| m.is_dir())
                .unwrap_or(false)
            {
                dirs.push(path.clone());
                stack.push(path);
            }
        }
    }
    dirs.sort_by_key(|path| std::cmp::Reverse(path.components().count()));
    for path in dirs {
        let _ = std::fs::remove_dir(path);
    }
    let _ = std::fs::remove_dir(root);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp_dir(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "brutex-heal-multi-{tag}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn month_of_us_routes_utc_months_including_pre_epoch() {
        // 2024-06-03T04:01:00Z
        assert_eq!(month_of_us(1_717_387_260_000_000), (2024, 6));
        // 2024-07-01T00:00:00Z boundary lands in July.
        assert_eq!(month_of_us(1_719_792_000_000_000), (2024, 7));
        // One µs before the boundary stays in June.
        assert_eq!(month_of_us(1_719_791_999_999_999), (2024, 6));
        // Pre-epoch: 1969-12-31T23:59:59.999999Z routes to 1969-12.
        assert_eq!(month_of_us(-1), (1969, 12));
    }

    #[test]
    fn option_stem_is_the_zero_padded_yyyy_mm_formatter() {
        assert_eq!(option_stem(2024, 6), "2024-06");
        assert_eq!(option_stem(31, 12), "0031-12");
    }

    #[test]
    fn bars_partition_path_matches_the_writer_layout() {
        let path = bars_partition_path(
            Path::new("/lake"),
            "NSE",
            "INDEX",
            "NIFTY",
            "1minute",
            2024,
            6,
        );
        assert_eq!(
            path,
            PathBuf::from("/lake/bars/NSE/INDEX/NIFTY/1minute/2024/06.parquet")
        );
    }

    #[test]
    fn interval_merge_rank_orders_minute_day_then_rest() {
        assert_eq!(interval_merge_rank("1minute"), 0);
        assert_eq!(interval_merge_rank("1day"), 1);
        assert_eq!(interval_merge_rank("5minute"), 2);
        assert_eq!(interval_merge_rank("1hour"), 2);
    }

    #[test]
    fn collect_parquet_entries_sorts_by_full_path_bytes() {
        let dir = tmp_dir("collect");
        std::fs::create_dir_all(dir.join("2024")).unwrap();
        std::fs::create_dir_all(dir.join("2023")).unwrap();
        std::fs::write(dir.join("2024/06.parquet"), b"x").unwrap();
        std::fs::write(dir.join("2024/05.parquet"), b"x").unwrap();
        std::fs::write(dir.join("2023/12.parquet"), b"x").unwrap();
        std::fs::write(dir.join("2024/notes.txt"), b"x").unwrap();
        let files = collect_parquet_entries_sorted(&dir).unwrap();
        let rels: Vec<String> = files
            .iter()
            .map(|p| p.strip_prefix(&dir).unwrap().to_string_lossy().into_owned())
            .collect();
        assert_eq!(
            rels,
            vec!["2023/12.parquet", "2024/05.parquet", "2024/06.parquet"]
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn interval_dir_probe_sees_any_parquet_before_the_write() {
        let dir = tmp_dir("probe");
        let dest = dir.join("1minute/2024/06.parquet");
        std::fs::create_dir_all(dest.parent().unwrap()).unwrap();
        assert!(interval_dir_has_no_parquet(&dest));
        std::fs::create_dir_all(dir.join("1minute/2023")).unwrap();
        std::fs::write(dir.join("1minute/2023/07.parquet"), b"x").unwrap();
        assert!(!interval_dir_has_no_parquet(&dest));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn prune_empty_dirs_removes_bottom_up_and_keeps_nonempty() {
        let dir = tmp_dir("prune");
        std::fs::create_dir_all(dir.join("a/b/c")).unwrap();
        std::fs::create_dir_all(dir.join("keep")).unwrap();
        std::fs::write(dir.join("keep/file.txt"), b"x").unwrap();
        prune_empty_dirs(&dir);
        assert!(!dir.join("a").exists());
        assert!(dir.join("keep/file.txt").exists());
        assert!(dir.exists()); // non-empty root survives
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn bar_values_equal_compares_ohlcv_and_oi() {
        let base = BarRow {
            ts_us: 1,
            open: 1.0,
            high: 2.0,
            low: 0.5,
            close: 1.5,
            volume: 10,
            oi: Some(5),
        };
        assert!(bar_values_equal(&base, &base.clone()));
        let mut other = base.clone();
        other.close = 1.6;
        assert!(!bar_values_equal(&base, &other));
        let mut oi_diff = base.clone();
        oi_diff.oi = None;
        assert!(!bar_values_equal(&base, &oi_diff));
    }
}
