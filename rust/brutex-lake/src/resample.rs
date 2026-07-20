//! # brutex-lake resample core — Wave B slice G5 (ADR-0020)
//!
//! DEC-LAKE-RUST-RESAMPLE-001 (MIGRATION.md #1913 — allocator-final). Ports
//! the ONE 1-minute → derived-TF resample core
//! (`brutex/data/tf_resampler.py::resample_1m_to_tf`) behind the default-OFF
//! `BRUTEX_LAKE_RUST_RESAMPLE` facade (`brutex/data/lake_rust_resample.py`).
//! Frames cross the FFI as uncompressed Arrow IPC bytes — the PR-1 deviation
//! (NOT Arrow C-data zero-copy; ADR-0020 risk row #11 superseded in
//! practice).
//!
//! ## The mirrored bucket LAW (load-bearing — do not "simplify")
//!
//! FIXED-ARITHMETIC session anchor (DEC-RESAMPLE-ANCHOR-002): each row's
//! bucket label is its IST date's 09:15 session open plus
//! floor-of-elapsed-minutes-over-tf whole buckets, converted back to UTC.
//! IST is a fixed +05:30 (no DST) so the law is pure integer arithmetic on
//! the datetime's physical value — the grid depends ONLY on calendar TIME,
//! never on which bars exist (gap-immune; a pre-09:15 stray bar lands in a
//! NEGATIVE-offset bucket). Daily tf=375 = ONE bucket per IST day labeled at
//! the 09:15 session open. The Python composite this mirrors OP-FOR-OP:
//!
//! ```text
//! ist              = ts_utc + 5h30m
//! session_open_ist = ist.dt.truncate("1d") + 9h15m      # floor toward -inf
//! offset_minutes   = (ist - session_open_ist).dt.total_minutes()
//!                                                        # TRUNC toward zero
//! bucket_minutes   = (offset_minutes // tf) * tf         # FLOOR division
//! bucket_utc       = session_open_ist + bucket_minutes*1m - 5h30m
//! ```
//!
//! `dt.truncate("1d")` floors (→ `div_euclid`), `dt.total_minutes()`
//! TRUNCATES toward zero (→ Rust `/` on i64), and Python `//` on the Expr is
//! polars FloorDivide (→ `div_euclid`) — three DISTINCT rounding modes,
//! reproduced exactly so a sub-minute or pre-09:15 timestamp buckets
//! bit-identically to the wheel.
//!
//! Aggregation set (token-for-token from the Python core): first(open) /
//! max(high) / min(low) / last(close) / sum(volume), plus carry-LAST
//! `open_interest` and each present Greeks-overlay column (STATE not FLOW —
//! never summed/averaged; a null last constituent stays null) when
//! `oi_aware` AND the column is present. group_by is STABLE
//! (maintain_order=True twin) over the caller-sorted input, then sorted by
//! the bucket label.
//!
//! VALIDATION STAYS PYTHON: `_validate_input` (schema / null-ts /
//! strictly-increasing) runs BEFORE the seam; this core may assume a clean
//! frame — its own guards (empty payload, null ts, non-UTC tz, tf=0) exist
//! only so a defect degrades to the Python oracle instead of panicking.

use polars::prelude::*;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;

/// The daily timeframe token (`brutex/data/timeframes.py::DAILY_TF_MINUTES`).
/// `is_intraday(tf) == (tf != 375)` — mirrored here, single-sourced by the
/// Python gate that admits only `ALL_TF_MINUTES` members to the seam.
pub const DAILY_TF_MINUTES: u32 = 375;

/// The optional F&O carry-last column (`tf_resampler.OPEN_INTEREST_COLUMN`).
pub const OPEN_INTEREST_COLUMN: &str = "open_interest";

/// The ten stored Greeks-overlay columns, in the EXACT order
/// `brutex/data/models.py::GREEKS_OVERLAY_FIELDS` carries them — the output
/// column order mirrors the Python core's append order, so this tuple's
/// order is load-bearing (asserted against the Python constant in the
/// facade's unit suite).
pub const GREEKS_OVERLAY_FIELDS: [&str; 10] = [
    "iv",
    "delta",
    "gamma",
    "theta",
    "vega",
    "rho",
    "spot_at_bar",
    "t_years_used",
    "rate_used",
    "greeks_provenance_id",
];

/// Per-second physical ticks for a datetime time unit.
fn ticks_per_second(unit: TimeUnit) -> i64 {
    match unit {
        TimeUnit::Milliseconds => 1_000,
        TimeUnit::Microseconds => 1_000_000,
        TimeUnit::Nanoseconds => 1_000_000_000,
    }
}

/// Decode uncompressed Arrow IPC bytes to a DataFrame (empty payload refused).
fn frame_from_ipc(ipc_bytes: &[u8]) -> PolarsResult<DataFrame> {
    if ipc_bytes.is_empty() {
        return Err(polars_err!(ComputeError: "empty IPC payload — nothing to resample"));
    }
    IpcReader::new(std::io::Cursor::new(ipc_bytes)).finish()
}

/// Encode a DataFrame to uncompressed Arrow IPC bytes.
fn frame_to_ipc(df: &mut DataFrame) -> PolarsResult<Vec<u8>> {
    let mut buf: Vec<u8> = Vec::new();
    IpcWriter::new(&mut buf).finish(df)?;
    Ok(buf)
}

/// The resample core over an ALREADY-DECODED frame — shared by the single-TF
/// and batch faces so a 21-TF derive decodes the input exactly once.
///
/// `tf == 1` is the identity arm (the Python `df_1m.clone()` twin — same
/// frame values/schema; the caller re-encodes, so the bytes are
/// verbatim-SHAPE, not verbatim-bytes). Errors — never panics — on tf=0, a
/// missing/`null`-carrying/non-datetime `ts_utc`, or a non-UTC timezone
/// (where the Python `dt.truncate` law would diverge from the fixed-offset
/// integer arithmetic; production frames are UTC/naive — the facade degrades
/// such exotics to the oracle).
pub fn resample_frame(
    df: &DataFrame,
    target_tf_minutes: u32,
    oi_aware: bool,
) -> PolarsResult<DataFrame> {
    if target_tf_minutes == 0 {
        return Err(polars_err!(ComputeError: "target_tf_minutes must be >= 1, got 0"));
    }
    if target_tf_minutes == 1 {
        return Ok(df.clone());
    }

    let ts = df.column("ts_utc")?;
    let (unit, tz) = match ts.dtype() {
        DataType::Datetime(unit, tz) => (*unit, tz.clone()),
        other => {
            return Err(
                polars_err!(ComputeError: "ts_utc must be a Datetime column, got {}", other),
            );
        }
    };
    if let Some(tz) = tz.as_ref()
        && tz.as_str() != "UTC"
    {
        return Err(polars_err!(
            ComputeError: "ts_utc timezone must be UTC or naive, got {}", tz.as_str()
        ));
    }
    if ts.null_count() > 0 {
        return Err(polars_err!(
            ComputeError: "ts_utc contains {} null timestamp(s) — cannot bucket", ts.null_count()
        ));
    }

    let per_second = ticks_per_second(unit);
    let per_minute = 60 * per_second;
    let per_day = 86_400 * per_second;
    let ist_offset = 19_800 * per_second; // +05:30
    let session_offset = 33_300 * per_second; // 09:15 from IST midnight
    let daily = target_tf_minutes == DAILY_TF_MINUTES;
    let tf = i64::from(target_tf_minutes);

    let phys = ts.to_physical_repr();
    let phys = phys.i64()?;
    let bucket: Int64Chunked = phys.apply_values(|value| {
        let ist = value + ist_offset;
        // dt.truncate("1d") floors toward -inf -> div_euclid.
        let session_open = ist.div_euclid(per_day) * per_day + session_offset;
        if daily {
            session_open - ist_offset
        } else {
            // dt.total_minutes() truncates toward zero -> Rust `/`.
            let offset_minutes = (ist - session_open) / per_minute;
            // Python `//` is polars FloorDivide -> div_euclid.
            let bucket_minutes = offset_minutes.div_euclid(tf) * tf;
            session_open + bucket_minutes * per_minute - ist_offset
        }
    });
    let bucket = bucket
        .into_series()
        .cast(&DataType::Datetime(unit, tz))?
        .with_name("_bucket_ts".into());

    let mut with_bucket = df.clone();
    with_bucket.with_column(Column::from(bucket))?;

    let mut aggregations = vec![
        col("open").first().alias("open"),
        col("high").max().alias("high"),
        col("low").min().alias("low"),
        col("close").last().alias("close"),
        col("volume").sum().alias("volume"),
    ];
    if oi_aware {
        // Carry-last OI + Greeks overlay: snapshot STATE, never summed. Only
        // when present — a spot frame's output stays the OHLCV-6 verbatim.
        let schema = df.schema();
        if schema.contains(OPEN_INTEREST_COLUMN) {
            aggregations.push(col(OPEN_INTEREST_COLUMN).last().alias(OPEN_INTEREST_COLUMN));
        }
        for overlay_column in GREEKS_OVERLAY_FIELDS {
            if schema.contains(overlay_column) {
                aggregations.push(col(overlay_column).last().alias(overlay_column));
            }
        }
    }

    with_bucket
        .lazy()
        .group_by_stable([col("_bucket_ts")])
        .agg(aggregations)
        .rename(["_bucket_ts"], ["ts_utc"], true)
        .sort(["ts_utc"], SortMultipleOptions::default())
        .collect()
}

/// Pure-Rust single-TF face: uncompressed Arrow IPC bytes → the resampled
/// frame's uncompressed Arrow IPC bytes. Exercised directly by `cargo test`;
/// the `#[pyfunction]` wrapper only adds the panic boundary + PyErr mapping.
pub fn resample_1m_ipc_impl(
    ipc_bytes: &[u8],
    target_tf_minutes: u32,
    oi_aware: bool,
) -> PolarsResult<Vec<u8>> {
    let df = frame_from_ipc(ipc_bytes)?;
    let mut out = resample_frame(&df, target_tf_minutes, oi_aware)?;
    frame_to_ipc(&mut out)
}

/// Pure-Rust batch face: ONE decode, one resample per requested TF — so a
/// 21-TF derive pays a single Python→Rust crossing. Output order mirrors the
/// input `tfs` order; any single TF's error fails the whole call (the facade
/// degrades the whole derive to the oracle — never a partial mix).
pub fn resample_all_ipc_impl(
    ipc_bytes: &[u8],
    tfs: &[u32],
    oi_aware: bool,
) -> PolarsResult<Vec<Vec<u8>>> {
    let df = frame_from_ipc(ipc_bytes)?;
    let mut out: Vec<Vec<u8>> = Vec::with_capacity(tfs.len());
    for tf in tfs {
        let mut frame = resample_frame(&df, *tf, oi_aware)?;
        out.push(frame_to_ipc(&mut frame)?);
    }
    Ok(out)
}

/// Map a caught resample result into Python bytes under the render seam's
/// exact FFI conventions (panic boundary both around the compute AND around
/// the result-buffer construction — a panic never crosses the FFI raw).
fn ipc_result_to_pybytes(
    py: Python<'_>,
    result: std::thread::Result<PolarsResult<Vec<u8>>>,
) -> PyResult<Py<PyBytes>> {
    let bytes = match result {
        Ok(Ok(bytes)) => bytes,
        Ok(Err(err)) => {
            return Err(PyValueError::new_err(format!(
                "brutex-lake resample failed: {err}"
            )));
        }
        Err(_) => {
            return Err(PyRuntimeError::new_err(
                "brutex-lake resample panicked (caught at the FFI boundary)",
            ));
        }
    };
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        PyBytes::new(py, &bytes).unbind()
    }))
    .map_err(|_| {
        PyRuntimeError::new_err(
            "brutex-lake resample result construction panicked (caught at the FFI boundary)",
        )
    })
}

/// Resample a 1-minute frame (uncompressed Arrow IPC bytes) to ONE target
/// timeframe, returning the derived frame's uncompressed Arrow IPC bytes.
///
/// Raises `ValueError` on an empty/undecodable payload or a polars error,
/// `RuntimeError` if the compute — or the result construction — panics
/// (both caught at this boundary; a panic never crosses the FFI). The
/// compute runs WITHOUT the GIL (`Python::detach` — sound: the input is an
/// immutable `bytes` buffer kept alive by the call arguments).
#[pyfunction]
pub(crate) fn resample_1m_ipc(
    py: Python<'_>,
    ipc_bytes: &[u8],
    target_tf_minutes: u32,
    oi_aware: bool,
) -> PyResult<Py<PyBytes>> {
    let result = py.detach(|| {
        std::panic::catch_unwind(|| resample_1m_ipc_impl(ipc_bytes, target_tf_minutes, oi_aware))
    });
    ipc_result_to_pybytes(py, result)
}

/// Batch twin of [`resample_1m_ipc`]: one decode, one output IPC payload per
/// requested TF (list order preserved). Same error/panic contract.
#[pyfunction]
pub(crate) fn resample_all_ipc(
    py: Python<'_>,
    ipc_bytes: &[u8],
    target_tf_minutes: Vec<u32>,
    oi_aware: bool,
) -> PyResult<Vec<Py<PyBytes>>> {
    let result = py.detach(|| {
        std::panic::catch_unwind(|| resample_all_ipc_impl(ipc_bytes, &target_tf_minutes, oi_aware))
    });
    let payloads = match result {
        Ok(Ok(payloads)) => payloads,
        Ok(Err(err)) => {
            return Err(PyValueError::new_err(format!(
                "brutex-lake resample failed: {err}"
            )));
        }
        Err(_) => {
            return Err(PyRuntimeError::new_err(
                "brutex-lake resample panicked (caught at the FFI boundary)",
            ));
        }
    };
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        payloads
            .iter()
            .map(|bytes| PyBytes::new(py, bytes).unbind())
            .collect::<Vec<Py<PyBytes>>>()
    }))
    .map_err(|_| {
        PyRuntimeError::new_err(
            "brutex-lake resample result construction panicked (caught at the FFI boundary)",
        )
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Build a 1m frame at explicit UTC-microsecond instants with the
    /// OHLCV-6 schema (+ optional OI / one overlay column).
    fn frame(ts_us: &[i64], with_oi: bool, with_iv: bool) -> DataFrame {
        let n = ts_us.len();
        let ts = Series::new("ts_utc".into(), ts_us.to_vec())
            .cast(&DataType::Datetime(
                TimeUnit::Microseconds,
                Some(TimeZone::UTC),
            ))
            .unwrap();
        let opens: Vec<f64> = (0..n).map(|i| 100.0 + i as f64).collect();
        let highs: Vec<f64> = (0..n).map(|i| 110.0 + i as f64).collect();
        let lows: Vec<f64> = (0..n).map(|i| 90.0 + i as f64).collect();
        let closes: Vec<f64> = (0..n).map(|i| 105.0 + i as f64).collect();
        let volumes: Vec<i64> = (0..n).map(|i| 10 + i as i64).collect();
        let mut cols = vec![
            ts.into(),
            Column::new("open".into(), opens),
            Column::new("high".into(), highs),
            Column::new("low".into(), lows),
            Column::new("close".into(), closes),
            Column::new("volume".into(), volumes),
        ];
        if with_oi {
            let oi: Vec<i64> = (0..n).map(|i| 1_000 + i as i64).collect();
            cols.push(Column::new("open_interest".into(), oi));
        }
        if with_iv {
            let iv: Vec<f64> = (0..n).map(|i| 0.2 + i as f64 / 100.0).collect();
            cols.push(Column::new("iv".into(), iv));
        }
        DataFrame::new_infer_height(cols).unwrap()
    }

    /// 2024-01-01 09:15 IST == 03:45 UTC, in epoch microseconds.
    const OPEN_0915_UTC_US: i64 = 1_704_080_700_000_000;
    const MINUTE_US: i64 = 60_000_000;

    fn ts_us_of(df: &DataFrame, row: usize) -> i64 {
        let phys = df.column("ts_utc").unwrap().to_physical_repr();
        phys.i64().unwrap().get(row).unwrap()
    }

    #[test]
    fn two_open_bars_collapse_into_one_session_open_bucket() {
        // The §1c doctest law: two bars at the 09:15 IST session open
        // (03:45 UTC) collapse into ONE bucket at every coarser TF, labeled
        // at the session open itself.
        let df = frame(
            &[OPEN_0915_UTC_US, OPEN_0915_UTC_US + MINUTE_US],
            false,
            false,
        );
        for tf in [2u32, 5, 15, 60, 240] {
            let out = resample_frame(&df, tf, true).unwrap();
            assert_eq!(out.height(), 1, "tf={tf}");
            assert_eq!(ts_us_of(&out, 0), OPEN_0915_UTC_US, "tf={tf}");
            let close = out.column("close").unwrap().f64().unwrap().get(0).unwrap();
            assert_eq!(close, 106.0, "last close wins (tf={tf})");
            let vol = out.column("volume").unwrap().i64().unwrap().get(0).unwrap();
            assert_eq!(vol, 21, "volume sums (tf={tf})");
            let open = out.column("open").unwrap().f64().unwrap().get(0).unwrap();
            assert_eq!(open, 100.0, "first open wins (tf={tf})");
        }
    }

    #[test]
    fn daily_375_yields_one_bucket_per_ist_day_labeled_at_session_open() {
        let day_us = 86_400_000_000i64;
        let df = frame(
            &[
                OPEN_0915_UTC_US,
                OPEN_0915_UTC_US + 5 * MINUTE_US,
                OPEN_0915_UTC_US + day_us, // next IST day's 09:15
            ],
            false,
            false,
        );
        let out = resample_frame(&df, DAILY_TF_MINUTES, true).unwrap();
        assert_eq!(out.height(), 2);
        assert_eq!(ts_us_of(&out, 0), OPEN_0915_UTC_US);
        assert_eq!(ts_us_of(&out, 1), OPEN_0915_UTC_US + day_us);
        let vol0 = out.column("volume").unwrap().i64().unwrap().get(0).unwrap();
        assert_eq!(vol0, 21);
    }

    #[test]
    fn pre_open_stray_bar_lands_in_negative_offset_bucket() {
        // 03:40 UTC == 09:10 IST: offset -5 min; total_minutes TRUNCATES
        // toward zero (-5), floor-div by 15 -> -1 bucket -> 09:00 IST label
        // (03:30 UTC). The three-rounding-mode composite (module docs).
        let stray = OPEN_0915_UTC_US - 5 * MINUTE_US;
        let df = frame(&[stray, OPEN_0915_UTC_US], false, false);
        let out = resample_frame(&df, 15, true).unwrap();
        assert_eq!(out.height(), 2);
        assert_eq!(ts_us_of(&out, 0), OPEN_0915_UTC_US - 15 * MINUTE_US);
        assert_eq!(ts_us_of(&out, 1), OPEN_0915_UTC_US);
    }

    #[test]
    fn oi_and_overlay_carry_last_when_oi_aware() {
        let df = frame(
            &[OPEN_0915_UTC_US, OPEN_0915_UTC_US + MINUTE_US],
            true,
            true,
        );
        let out = resample_frame(&df, 5, true).unwrap();
        assert_eq!(out.height(), 1);
        let oi = out
            .column("open_interest")
            .unwrap()
            .i64()
            .unwrap()
            .get(0)
            .unwrap();
        assert_eq!(oi, 1_001, "carry-LAST snapshot, never summed");
        let iv = out.column("iv").unwrap().f64().unwrap().get(0).unwrap();
        assert_eq!(
            iv,
            0.2 + 1.0 / 100.0,
            "overlay carries the LAST constituent's value"
        );
        assert_eq!(
            out.get_column_names(),
            [
                "ts_utc",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "open_interest",
                "iv"
            ]
        );
    }

    #[test]
    fn oi_aware_false_yields_ohlcv6_even_when_oi_present() {
        let df = frame(&[OPEN_0915_UTC_US], true, true);
        let out = resample_frame(&df, 5, false).unwrap();
        assert_eq!(
            out.get_column_names(),
            ["ts_utc", "open", "high", "low", "close", "volume"]
        );
    }

    #[test]
    fn tf_one_is_the_identity_arm_frame_equality_not_byte_equality() {
        let df = frame(
            &[OPEN_0915_UTC_US, OPEN_0915_UTC_US + MINUTE_US],
            true,
            false,
        );
        let mut src = df.clone();
        let ipc = frame_to_ipc(&mut src).unwrap();
        let out_bytes = resample_1m_ipc_impl(&ipc, 1, true).unwrap();
        let out = frame_from_ipc(&out_bytes).unwrap();
        assert!(out.equals(&df), "identity arm preserves the frame verbatim");
    }

    #[test]
    fn batch_face_matches_single_tf_calls_and_preserves_order() {
        let df = frame(
            &[OPEN_0915_UTC_US, OPEN_0915_UTC_US + MINUTE_US],
            true,
            false,
        );
        let mut src = df.clone();
        let ipc = frame_to_ipc(&mut src).unwrap();
        let tfs = [5u32, 1, 375];
        let batch = resample_all_ipc_impl(&ipc, &tfs, true).unwrap();
        assert_eq!(batch.len(), 3);
        for (i, tf) in tfs.iter().enumerate() {
            let single = resample_1m_ipc_impl(&ipc, *tf, true).unwrap();
            let a = frame_from_ipc(&batch[i]).unwrap();
            let b = frame_from_ipc(&single).unwrap();
            assert!(a.equals(&b), "batch[{i}] (tf={tf}) == single call");
        }
    }

    #[test]
    fn empty_payload_and_tf_zero_and_null_ts_and_non_utc_tz_are_errors() {
        assert!(resample_1m_ipc_impl(&[], 5, true).is_err());
        let df = frame(&[OPEN_0915_UTC_US], false, false);
        let mut src = df.clone();
        let ipc = frame_to_ipc(&mut src).unwrap();
        assert!(resample_1m_ipc_impl(&ipc, 0, true).is_err());

        // Null timestamp refused.
        let ts = Series::new("ts_utc".into(), &[Some(OPEN_0915_UTC_US), None])
            .cast(&DataType::Datetime(TimeUnit::Microseconds, None))
            .unwrap();
        let mut null_df = DataFrame::new_infer_height(vec![
            ts.into(),
            Column::new("open".into(), vec![1.0f64, 2.0]),
            Column::new("high".into(), vec![1.0f64, 2.0]),
            Column::new("low".into(), vec![1.0f64, 2.0]),
            Column::new("close".into(), vec![1.0f64, 2.0]),
            Column::new("volume".into(), vec![1i64, 2]),
        ])
        .unwrap();
        let ipc = frame_to_ipc(&mut null_df).unwrap();
        assert!(resample_1m_ipc_impl(&ipc, 5, true).is_err());

        // Non-UTC timezone refused (the truncate law would diverge).
        let kolkata: TimeZone = TimeZone::opt_try_new(Some("Asia/Kolkata"))
            .unwrap()
            .unwrap();
        let mut tz_df = frame(&[OPEN_0915_UTC_US], false, false);
        let recast = tz_df
            .column("ts_utc")
            .unwrap()
            .cast(&DataType::Datetime(TimeUnit::Microseconds, Some(kolkata)))
            .unwrap();
        tz_df.with_column(recast).unwrap();
        let ipc = frame_to_ipc(&mut tz_df).unwrap();
        assert!(resample_1m_ipc_impl(&ipc, 5, true).is_err());
    }

    #[test]
    fn month_edge_pre_dawn_bar_files_under_prior_day_label() {
        // 00:15 IST on the 1st (18:45 UTC prior day): offset -540 min at
        // tf=240 floors to -3 buckets -> label 21:15 IST prior day == 15:45
        // UTC prior day (the #1795b prior-UTC-month filing shape).
        let ist_0015_utc_us = OPEN_0915_UTC_US - 9 * 60 * MINUTE_US; // 00:15 IST same IST day
        let df = frame(&[ist_0015_utc_us], false, false);
        let out = resample_frame(&df, 240, true).unwrap();
        assert_eq!(out.height(), 1);
        let expected = OPEN_0915_UTC_US - 720 * MINUTE_US; // 09:15 - 12h
        assert_eq!(ts_us_of(&out, 0), expected);
    }

    #[test]
    fn millisecond_and_nanosecond_units_bucket_identically() {
        for (unit, factor) in [
            (TimeUnit::Milliseconds, 1_000i64),
            (TimeUnit::Nanoseconds, 1_000_000_000i64),
        ] {
            let seconds = OPEN_0915_UTC_US / 1_000_000;
            let ts = Series::new(
                "ts_utc".into(),
                vec![seconds * factor, (seconds + 60) * factor],
            )
            .cast(&DataType::Datetime(unit, Some(TimeZone::UTC)))
            .unwrap();
            let mut df = frame(
                &[OPEN_0915_UTC_US, OPEN_0915_UTC_US + MINUTE_US],
                false,
                false,
            );
            df.with_column(Column::from(ts.clone())).unwrap();
            let out = resample_frame(&df, 5, true).unwrap();
            assert_eq!(out.height(), 1, "unit={unit:?}");
            let phys = out.column("ts_utc").unwrap().to_physical_repr();
            assert_eq!(
                phys.i64().unwrap().get(0).unwrap(),
                seconds * factor,
                "unit={unit:?}"
            );
            assert_eq!(
                out.column("ts_utc").unwrap().dtype(),
                &DataType::Datetime(unit, Some(TimeZone::UTC)),
                "output keeps the input unit (unit={unit:?})"
            );
        }
    }

    #[test]
    fn zero_row_frame_resamples_to_zero_rows_with_the_derived_schema() {
        let df = frame(&[], false, false);
        let mut src = df.clone();
        let ipc = frame_to_ipc(&mut src).unwrap();
        let out_bytes = resample_1m_ipc_impl(&ipc, 5, true).unwrap();
        let out = frame_from_ipc(&out_bytes).unwrap();
        assert_eq!(out.height(), 0);
        assert_eq!(
            out.get_column_names(),
            ["ts_utc", "open", "high", "low", "close", "volume"]
        );
    }
}
