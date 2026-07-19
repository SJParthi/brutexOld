//! # Merge/dedup DECISION kernel (ADR-0020, PR-3)
//!
//! DEC-LAKE-RUST-MERGE-DEDUP-001 (MIGRATION.md #1911 — allocator-final).
//! Re-hosts the PURE DECISION of `brutex/data/parquet_writer.py::
//! ParquetWriter._merge_fold` — the timestamp-keyed merge fold of
//! `_merge_write` — as an integers-only kernel: the stored partition's
//! (keep-first collapsed) bars and the incoming batch cross the FFI as two
//! Arrow-IPC frames of the ALWAYS-17-column DECISION schema, and the kernel
//! returns ONE action code per batch row. Python materializes everything:
//! every `Decimal`/`CandleBar`/`BarConflict`/`WriteResult` construction, all
//! logging, the sort, the tripwire and the render stay Python — so
//! `raw_hash` / partition bytes are identical BY CONSTRUCTION and no float
//! ever crosses BACK over the FFI (the DEC-FEED-RUST-CORE-001
//! decision-kernel shape, not a byte-renderer).
//!
//! ## The fold law (mirrored token-for-token from `_merge_fold`)
//!
//! For each batch row IN ORDER, against the CURRENT winner at its
//! timestamp (stored bar, or an earlier batch row that already won):
//!
//! * no winner → **INSERT** (`added += 1` Python-side);
//! * OHLCV+OI differ (`_bar_values_equal` law) → keep-first: **CONFLICT**
//!   (the stored winner stays; Python records a `BarConflict`);
//!   latest-wins: **OVERWRITE** (`rewrite = True`);
//! * OHLCV+OI equal but the Greeks overlay differs (`_overlay_equal` law)
//!   → keep-first: **SKIP** (silent — a vendor re-pull's plain bar never
//!   strips a stored Greek); latest-wins: **OVERWRITE**;
//! * fully equal → **SKIP**.
//!
//! ## Value-comparison honesty (Golden Rule #10)
//!
//! Python compares 2dp-snapped `Decimal`s; this kernel compares the f64s the
//! decision frame carries (the SAME `float(Decimal)` conversion the render
//! frame uses — values arrive snapped, the kernel NEVER re-rounds, handoff
//! §1h). Two numerically-equal snapped Decimals map to the same f64; two
//! DISTINCT 2dp values alias in f64 only above ~2.2e13 (spacing > half a
//! paisa) — far outside any admissible price, and the facade's burn-in
//! bit-compare + autorevert guards the class regardless. Overlay floats are
//! stored full-precision f64s on BOTH sides (exact), and the model validates
//! them finite-or-None, so `Some(NaN)` never legitimately crosses; if one
//! did, `Option<f64>` equality (NaN != NaN) matches Python's `float`
//! semantics. `None == None` compares equal on both sides (`Option` eq ≡
//! Python `None == None`).
//!
//! ## FFI boundary contract
//!
//! Mirrors `render_parquet_from_ipc` (lib.rs): errors cross as clean
//! `PyErr`s (`ValueError` for an empty/undecodable payload, a missing
//! decision column, or a null timestamp), never a panic — the decision AND
//! the result-list construction each run under `std::panic::catch_unwind`
//! (panic → `RuntimeError`); the decision itself runs WITHOUT the GIL
//! (`Python::detach`). Outputs are `u32` action codes ONLY.

use std::collections::HashMap;

use polars::prelude::*;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyList;

/// Action codes — the ONE vocabulary shared with the Python facade
/// (`brutex/data/lake_rust_merge.py` mirrors these literals; equality-pinned
/// in its unit suite via the crate-shape test).
pub const ACTION_SKIP: u32 = 0;
/// New timestamp — Python inserts the batch bar (`added += 1`).
pub const ACTION_INSERT: u32 = 1;
/// Latest-wins restate — Python overwrites the winner (`rewrite = True`).
pub const ACTION_OVERWRITE: u32 = 2;
/// Keep-first value mismatch — Python records a `BarConflict`.
pub const ACTION_CONFLICT: u32 = 3;

/// The `_bar_values_equal` comparison surface (OHLCV+OI — timestamp is the
/// key, never a compared value).
const VALUE_F64_COLUMNS: [&str; 4] = ["open", "high", "low", "close"];
const VALUE_I64_COLUMNS: [&str; 2] = ["volume", "open_interest"];

/// The `_overlay_equal` comparison surface — the ten nullable Greeks-overlay
/// fields in `brutex.data.models.GREEKS_OVERLAY_FIELDS` order (nine f64 +
/// the Int64 provenance stamp).
const OVERLAY_F64_COLUMNS: [&str; 9] = [
    "iv",
    "delta",
    "gamma",
    "theta",
    "vega",
    "rho",
    "spot_at_bar",
    "t_years_used",
    "rate_used",
];
const OVERLAY_I64_COLUMN: &str = "greeks_provenance_id";

/// One decoded decision row — every field `Option` so null semantics are
/// explicit (`None == None` matches Python).
struct DecisionRow {
    ts: i64,
    values_f: [Option<f64>; 4],
    values_i: [Option<i64>; 2],
    overlay_f: [Option<f64>; 9],
    overlay_provenance: Option<i64>,
}

fn values_equal(a: &DecisionRow, b: &DecisionRow) -> bool {
    a.values_f == b.values_f && a.values_i == b.values_i
}

fn overlay_equal(a: &DecisionRow, b: &DecisionRow) -> bool {
    a.overlay_f == b.overlay_f && a.overlay_provenance == b.overlay_provenance
}

/// Extract a named column cast to f64 options.
fn f64_column(df: &DataFrame, name: &str, which: &str) -> PolarsResult<Vec<Option<f64>>> {
    let series = df
        .column(name)
        .map_err(
            |_| polars_err!(ComputeError: "decision frame ({which}) is missing column '{name}'"),
        )?
        .as_materialized_series()
        .cast(&DataType::Float64)?;
    Ok(series.f64()?.to_vec())
}

/// Extract a named column cast to i64 options.
fn i64_column(df: &DataFrame, name: &str, which: &str) -> PolarsResult<Vec<Option<i64>>> {
    let series = df
        .column(name)
        .map_err(
            |_| polars_err!(ComputeError: "decision frame ({which}) is missing column '{name}'"),
        )?
        .as_materialized_series()
        .cast(&DataType::Int64)?;
    Ok(series.i64()?.to_vec())
}

/// Decode one decision frame from uncompressed Arrow IPC bytes.
fn read_decision_rows(ipc_bytes: &[u8], which: &str) -> PolarsResult<Vec<DecisionRow>> {
    if ipc_bytes.is_empty() {
        return Err(
            polars_err!(ComputeError: "empty IPC payload for the {which} decision frame — nothing to decide"),
        );
    }
    let df = IpcReader::new(std::io::Cursor::new(ipc_bytes)).finish()?;
    // The timestamp column is Datetime(us, UTC) Python-side; its physical
    // Int64 (epoch-µs) is a lossless total key — equal datetimes ⇔ equal i64.
    let ts = i64_column(&df, "timestamp", which)?;
    let mut values_f: Vec<Vec<Option<f64>>> = Vec::with_capacity(VALUE_F64_COLUMNS.len());
    for name in VALUE_F64_COLUMNS {
        values_f.push(f64_column(&df, name, which)?);
    }
    let mut values_i: Vec<Vec<Option<i64>>> = Vec::with_capacity(VALUE_I64_COLUMNS.len());
    for name in VALUE_I64_COLUMNS {
        values_i.push(i64_column(&df, name, which)?);
    }
    let mut overlay_f: Vec<Vec<Option<f64>>> = Vec::with_capacity(OVERLAY_F64_COLUMNS.len());
    for name in OVERLAY_F64_COLUMNS {
        overlay_f.push(f64_column(&df, name, which)?);
    }
    let provenance = i64_column(&df, OVERLAY_I64_COLUMN, which)?;
    let mut rows = Vec::with_capacity(df.height());
    for idx in 0..df.height() {
        let Some(ts_value) = ts[idx] else {
            return Err(
                polars_err!(ComputeError: "null timestamp at row {idx} of the {which} decision frame"),
            );
        };
        rows.push(DecisionRow {
            ts: ts_value,
            values_f: [
                values_f[0][idx],
                values_f[1][idx],
                values_f[2][idx],
                values_f[3][idx],
            ],
            values_i: [values_i[0][idx], values_i[1][idx]],
            overlay_f: [
                overlay_f[0][idx],
                overlay_f[1][idx],
                overlay_f[2][idx],
                overlay_f[3][idx],
                overlay_f[4][idx],
                overlay_f[5][idx],
                overlay_f[6][idx],
                overlay_f[7][idx],
                overlay_f[8][idx],
            ],
            overlay_provenance: provenance[idx],
        });
    }
    Ok(rows)
}

/// Pure decision core: stored + batch decision frames (Arrow IPC) → one
/// action code per batch row, mirroring `_merge_fold` (module docs).
/// Exercised directly by `cargo test`; the `#[pyfunction]` wrapper only adds
/// the panic boundary + PyErr mapping.
pub fn merge_decision_from_ipc_impl(
    stored_ipc: &[u8],
    batch_ipc: &[u8],
    keep_first: bool,
) -> PolarsResult<Vec<u32>> {
    let stored = read_decision_rows(stored_ipc, "stored")?;
    let batch = read_decision_rows(batch_ipc, "batch")?;
    // The stored side is already keep-first-collapsed Python-side
    // (`_read_existing_bars`); collapse keep-FIRST here too as a defensive
    // mirror — a duplicated stored timestamp keeps its first physical row
    // exactly like the Python dict build (R7-1 physical-copy law).
    let mut winner: HashMap<i64, &DecisionRow> = HashMap::with_capacity(stored.len());
    for row in &stored {
        winner.entry(row.ts).or_insert(row);
    }
    let mut actions = Vec::with_capacity(batch.len());
    for row in &batch {
        match winner.get(&row.ts) {
            None => {
                winner.insert(row.ts, row);
                actions.push(ACTION_INSERT);
            }
            Some(prev) => {
                if !values_equal(prev, row) {
                    if keep_first {
                        actions.push(ACTION_CONFLICT);
                    } else {
                        winner.insert(row.ts, row);
                        actions.push(ACTION_OVERWRITE);
                    }
                } else if !overlay_equal(prev, row) {
                    if keep_first {
                        // Stored overlay kept SILENTLY — never a conflict
                        // (`_overlay_equal` law, plan §15.3).
                        actions.push(ACTION_SKIP);
                    } else {
                        winner.insert(row.ts, row);
                        actions.push(ACTION_OVERWRITE);
                    }
                } else {
                    actions.push(ACTION_SKIP);
                }
            }
        }
    }
    Ok(actions)
}

/// Decide the merge fold for one batch against one stored partition.
///
/// Both frames cross as UNCOMPRESSED Arrow IPC bytes of the 17-column
/// decision schema (`parquet_writer._decision_frame`). Returns one action
/// code per batch row (`0=skip · 1=insert · 2=overwrite · 3=conflict`) —
/// integers only; Python materializes the fold from them.
///
/// Raises `ValueError` on an empty/undecodable payload, a missing decision
/// column or a null timestamp; `RuntimeError` if the decision — or the
/// result-list construction — panics (both caught at this boundary; a panic
/// never crosses the FFI). The decision runs WITHOUT the GIL
/// (`Python::detach` — sound: the inputs are immutable `bytes` buffers kept
/// alive by the call arguments).
#[pyfunction]
pub fn merge_decision_from_ipc(
    py: Python<'_>,
    stored_ipc: &[u8],
    batch_ipc: &[u8],
    keep_first: bool,
) -> PyResult<Py<PyList>> {
    let decided = py.detach(|| {
        std::panic::catch_unwind(|| merge_decision_from_ipc_impl(stored_ipc, batch_ipc, keep_first))
    });
    let actions = match decided {
        Ok(Ok(actions)) => actions,
        Ok(Err(err)) => {
            return Err(PyValueError::new_err(format!(
                "brutex-lake merge decision failed: {err}"
            )));
        }
        Err(_) => {
            return Err(PyRuntimeError::new_err(
                "brutex-lake merge decision panicked (caught at the FFI boundary)",
            ));
        }
    };
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        PyList::new(py, &actions).map(|list| list.unbind())
    }))
    .map_err(|_| {
        PyRuntimeError::new_err(
            "brutex-lake merge result construction panicked (caught at the FFI boundary)",
        )
    })?
}

#[cfg(test)]
mod tests {
    use super::*;

    /// One row of decision-frame input for the test builder: (epoch-µs ts,
    /// close, volume, open_interest, iv-overlay). `open/high/low` derive
    /// from `close`; the other overlay columns stay null.
    type Row = (i64, f64, i64, Option<i64>, Option<f64>);

    fn decision_ipc(rows: &[Row]) -> Vec<u8> {
        let ts_raw: Vec<i64> = rows.iter().map(|r| r.0).collect();
        let timestamp = Series::new("timestamp".into(), ts_raw)
            .cast(&DataType::Datetime(
                TimeUnit::Microseconds,
                Some(polars::prelude::TimeZone::UTC),
            ))
            .unwrap();
        let closes: Vec<f64> = rows.iter().map(|r| r.1).collect();
        let mut columns: Vec<Column> = vec![
            timestamp.into(),
            Series::new(
                "open".into(),
                closes.iter().map(|v| v - 0.5).collect::<Vec<_>>(),
            )
            .into(),
            Series::new(
                "high".into(),
                closes.iter().map(|v| v + 1.0).collect::<Vec<_>>(),
            )
            .into(),
            Series::new(
                "low".into(),
                closes.iter().map(|v| v - 1.0).collect::<Vec<_>>(),
            )
            .into(),
            Series::new("close".into(), closes).into(),
            Series::new(
                "volume".into(),
                rows.iter().map(|r| r.2).collect::<Vec<i64>>(),
            )
            .into(),
            Series::new(
                "open_interest".into(),
                rows.iter().map(|r| r.3).collect::<Vec<Option<i64>>>(),
            )
            .into(),
        ];
        for name in OVERLAY_F64_COLUMNS {
            let values: Vec<Option<f64>> = if name == "iv" {
                rows.iter().map(|r| r.4).collect()
            } else {
                vec![None; rows.len()]
            };
            columns.push(Series::new(name.into(), values).into());
        }
        columns.push(Series::new(OVERLAY_I64_COLUMN.into(), vec![None::<i64>; rows.len()]).into());
        let mut df = DataFrame::new(rows.len(), columns).unwrap();
        let mut ipc = Vec::new();
        IpcWriter::new(&mut ipc)
            .with_compression(None)
            .finish(&mut df)
            .unwrap();
        ipc
    }

    fn decide(stored: &[Row], batch: &[Row], keep_first: bool) -> Vec<u32> {
        merge_decision_from_ipc_impl(&decision_ipc(stored), &decision_ipc(batch), keep_first)
            .unwrap()
    }

    #[test]
    fn new_timestamp_inserts() {
        let actions = decide(
            &[(0, 100.0, 10, None, None)],
            &[(60, 101.0, 11, None, None)],
            true,
        );
        assert_eq!(actions, vec![ACTION_INSERT]);
    }

    #[test]
    fn identical_bar_skips_both_modes() {
        let stored = [(0, 100.0, 10, Some(5), None)];
        for keep_first in [true, false] {
            assert_eq!(decide(&stored, &stored, keep_first), vec![ACTION_SKIP]);
        }
    }

    #[test]
    fn value_mismatch_conflicts_keep_first_overwrites_latest_wins() {
        let stored = [(0, 100.0, 10, None, None)];
        let batch = [(0, 200.0, 10, None, None)];
        assert_eq!(decide(&stored, &batch, true), vec![ACTION_CONFLICT]);
        assert_eq!(decide(&stored, &batch, false), vec![ACTION_OVERWRITE]);
    }

    #[test]
    fn oi_none_vs_value_is_a_value_mismatch() {
        // `None == None` equal; `None` vs a value differs — the Python
        // `_bar_values_equal` OI law.
        let stored = [(0, 100.0, 10, None, None)];
        let batch = [(0, 100.0, 10, Some(7), None)];
        assert_eq!(decide(&stored, &batch, true), vec![ACTION_CONFLICT]);
        let same = [(0, 100.0, 10, None, None)];
        assert_eq!(decide(&stored, &same, true), vec![ACTION_SKIP]);
    }

    #[test]
    fn overlay_only_difference_skips_keep_first_overwrites_latest_wins() {
        let stored = [(0, 100.0, 10, Some(5), Some(0.21))];
        let batch = [(0, 100.0, 10, Some(5), Some(0.34))];
        assert_eq!(decide(&stored, &batch, true), vec![ACTION_SKIP]);
        assert_eq!(decide(&stored, &batch, false), vec![ACTION_OVERWRITE]);
        // Plain bar (all-None overlay) into an enriched partition: overlay
        // differs, values equal — silent keep-first, overwrite latest-wins.
        let plain = [(0, 100.0, 10, Some(5), None)];
        assert_eq!(decide(&stored, &plain, true), vec![ACTION_SKIP]);
        assert_eq!(decide(&stored, &plain, false), vec![ACTION_OVERWRITE]);
    }

    #[test]
    fn within_batch_duplicate_folds_against_the_earlier_batch_row() {
        // Two batch rows at a NEW timestamp: the first inserts; the second
        // conflicts against IT under keep-first (the Python fold's stateful
        // `prev is not None` law) and overwrites it under latest-wins.
        let batch = [(60, 100.0, 10, None, None), (60, 200.0, 10, None, None)];
        assert_eq!(
            decide(&[], &batch, true),
            vec![ACTION_INSERT, ACTION_CONFLICT]
        );
        assert_eq!(
            decide(&[], &batch, false),
            vec![ACTION_INSERT, ACTION_OVERWRITE]
        );
        // Identical twin rows: insert then silent skip in both modes.
        let twins = [(60, 100.0, 10, None, None), (60, 100.0, 10, None, None)];
        assert_eq!(decide(&[], &twins, true), vec![ACTION_INSERT, ACTION_SKIP]);
        assert_eq!(decide(&[], &twins, false), vec![ACTION_INSERT, ACTION_SKIP]);
    }

    #[test]
    fn latest_wins_chain_folds_against_the_latest_overwrite() {
        // A third row equal to the SECOND (post-overwrite) winner skips.
        let batch = [
            (60, 100.0, 10, None, None),
            (60, 200.0, 10, None, None),
            (60, 200.0, 10, None, None),
        ];
        assert_eq!(
            decide(&[], &batch, false),
            vec![ACTION_INSERT, ACTION_OVERWRITE, ACTION_SKIP]
        );
    }

    #[test]
    fn duplicated_stored_timestamp_keeps_first_physical_row() {
        // Defensive mirror of the Python read collapse (R7-1): the FIRST
        // stored physical row is the winner the batch compares against.
        let stored = [(0, 100.0, 10, None, None), (0, 200.0, 10, None, None)];
        let batch = [(0, 100.0, 10, None, None)];
        assert_eq!(decide(&stored, &batch, true), vec![ACTION_SKIP]);
        let mismatch = [(0, 200.0, 10, None, None)];
        assert_eq!(decide(&stored, &mismatch, true), vec![ACTION_CONFLICT]);
    }

    #[test]
    fn empty_batch_decides_nothing() {
        assert_eq!(
            decide(&[(0, 100.0, 10, None, None)], &[], true),
            Vec::<u32>::new()
        );
    }

    #[test]
    fn empty_stored_frame_inserts_everything() {
        let batch = [(0, 100.0, 10, None, None), (60, 101.0, 11, None, None)];
        assert_eq!(
            decide(&[], &batch, true),
            vec![ACTION_INSERT, ACTION_INSERT]
        );
    }

    #[test]
    fn empty_payload_is_refused() {
        let err = merge_decision_from_ipc_impl(&[], &decision_ipc(&[]), true).unwrap_err();
        assert!(err.to_string().contains("empty IPC payload"));
        let err = merge_decision_from_ipc_impl(&decision_ipc(&[]), &[], true).unwrap_err();
        assert!(err.to_string().contains("empty IPC payload"));
    }

    #[test]
    fn garbage_payload_is_a_clean_error() {
        let err =
            merge_decision_from_ipc_impl(b"not an arrow ipc stream", &decision_ipc(&[]), true)
                .unwrap_err();
        assert!(!err.to_string().is_empty());
    }

    #[test]
    fn missing_column_is_named() {
        // Build a frame lacking `close` by hand.
        let mut df =
            DataFrame::new(1, vec![Series::new("timestamp".into(), vec![0_i64]).into()]).unwrap();
        let mut ipc = Vec::new();
        IpcWriter::new(&mut ipc)
            .with_compression(None)
            .finish(&mut df)
            .unwrap();
        let err = merge_decision_from_ipc_impl(&ipc, &decision_ipc(&[]), true).unwrap_err();
        assert!(err.to_string().contains("missing column"));
    }

    #[test]
    fn null_timestamp_is_refused() {
        let mut columns: Vec<Column> = vec![
            Series::new("timestamp".into(), vec![None::<i64>])
                .cast(&DataType::Datetime(
                    TimeUnit::Microseconds,
                    Some(polars::prelude::TimeZone::UTC),
                ))
                .unwrap()
                .into(),
        ];
        for name in VALUE_F64_COLUMNS {
            columns.push(Series::new(name.into(), vec![1.0_f64]).into());
        }
        for name in VALUE_I64_COLUMNS {
            columns.push(Series::new(name.into(), vec![Some(1_i64)]).into());
        }
        for name in OVERLAY_F64_COLUMNS {
            columns.push(Series::new(name.into(), vec![None::<f64>]).into());
        }
        columns.push(Series::new(OVERLAY_I64_COLUMN.into(), vec![None::<i64>]).into());
        let mut df = DataFrame::new(1, columns).unwrap();
        let mut ipc = Vec::new();
        IpcWriter::new(&mut ipc)
            .with_compression(None)
            .finish(&mut df)
            .unwrap();
        let err = merge_decision_from_ipc_impl(&ipc, &decision_ipc(&[]), true).unwrap_err();
        assert!(err.to_string().contains("null timestamp"));
    }

    #[test]
    fn determinism_identical_inputs_identical_actions() {
        let stored = [(0, 100.0, 10, Some(1), Some(0.2))];
        let batch = [
            (0, 100.0, 10, Some(1), Some(0.2)),
            (0, 250.0, 10, Some(1), Some(0.2)),
            (60, 300.0, 11, None, None),
        ];
        let first = decide(&stored, &batch, true);
        let second = decide(&stored, &batch, true);
        assert_eq!(first, second);
        assert_eq!(first, vec![ACTION_SKIP, ACTION_CONFLICT, ACTION_INSERT]);
    }

    // -----------------------------------------------------------------
    // Differential fuzz vs a NAIVE quadratic reference fold. Inline
    // deterministic xorshift generator — deliberately NO new dev-dependency
    // (the committed Cargo.lock stays byte-untouched); 400 seeded cases
    // cover both modes over a colliding timestamp/value space.
    // -----------------------------------------------------------------

    struct XorShift(u64);

    impl XorShift {
        fn next(&mut self) -> u64 {
            let mut x = self.0;
            x ^= x << 13;
            x ^= x >> 7;
            x ^= x << 17;
            self.0 = x;
            x
        }

        fn below(&mut self, bound: u64) -> u64 {
            self.next() % bound
        }
    }

    /// Naive reference: replay the fold with brute-force row structs and a
    /// Vec-scan winner table (no HashMap) — a structurally DIFFERENT
    /// implementation of the same law.
    fn naive_reference(stored: &[Row], batch: &[Row], keep_first: bool) -> Vec<u32> {
        fn eq_values(a: &Row, b: &Row) -> bool {
            a.1 == b.1 && a.2 == b.2 && a.3 == b.3
        }
        fn eq_overlay(a: &Row, b: &Row) -> bool {
            a.4 == b.4
        }
        let mut winners: Vec<Row> = Vec::new();
        for row in stored {
            if !winners.iter().any(|w| w.0 == row.0) {
                winners.push(*row);
            }
        }
        let mut actions = Vec::new();
        for row in batch {
            match winners.iter().position(|w| w.0 == row.0) {
                None => {
                    winners.push(*row);
                    actions.push(ACTION_INSERT);
                }
                Some(pos) => {
                    let prev = winners[pos];
                    if !eq_values(&prev, row) {
                        if keep_first {
                            actions.push(ACTION_CONFLICT);
                        } else {
                            winners[pos] = *row;
                            actions.push(ACTION_OVERWRITE);
                        }
                    } else if !eq_overlay(&prev, row) {
                        if keep_first {
                            actions.push(ACTION_SKIP);
                        } else {
                            winners[pos] = *row;
                            actions.push(ACTION_OVERWRITE);
                        }
                    } else {
                        actions.push(ACTION_SKIP);
                    }
                }
            }
        }
        actions
    }

    fn random_rows(rng: &mut XorShift, max_len: u64) -> Vec<Row> {
        let len = rng.below(max_len + 1) as usize;
        (0..len)
            .map(|_| {
                (
                    (rng.below(6) as i64) * 60,  // small ts space → many collisions
                    100.0 + rng.below(3) as f64, // small value space → equal + differing pairs
                    10 + rng.below(2) as i64,
                    match rng.below(3) {
                        0 => None,
                        v => Some(v as i64),
                    },
                    match rng.below(3) {
                        0 => None,
                        v => Some(v as f64 * 0.1),
                    },
                )
            })
            .collect()
    }

    #[test]
    fn differential_fuzz_matches_naive_reference() {
        let mut rng = XorShift(0x9e37_79b9_7f4a_7c15);
        for case in 0..400 {
            let stored = random_rows(&mut rng, 8);
            let batch = random_rows(&mut rng, 8);
            let keep_first = case % 2 == 0;
            let kernel = decide(&stored, &batch, keep_first);
            let reference = naive_reference(&stored, &batch, keep_first);
            assert_eq!(
                kernel, reference,
                "divergence at case {case} (keep_first={keep_first}) stored={stored:?} batch={batch:?}"
            );
        }
    }

    #[test]
    fn action_constants_are_the_pinned_vocabulary() {
        assert_eq!(
            [
                ACTION_SKIP,
                ACTION_INSERT,
                ACTION_OVERWRITE,
                ACTION_CONFLICT
            ],
            [0, 1, 2, 3]
        );
    }
}
