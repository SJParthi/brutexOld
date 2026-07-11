//! Columnar batch face of [`super::greeks`] (`DEC-GREEKS-RUST-BATCH-001`).
//!
//! Calls the SAME frozen scalar kernel per element — zero math added or
//! changed (the jaeckel `=0.2.0` pin, `greeks_golden.json` bit-identity
//! contract and every kernel constant are byte-untouched). The batch exists
//! ONLY to amortise the Python↔Rust FFI crossing for BruteX's generation-time
//! Greeks enrichment loop (`brutex/data/greeks_enrich.py`): N already-screened
//! solve tuples cross in ONE call instead of N.
//!
//! Per-element outcomes are DATA, never exceptions: one unsolvable strike
//! must never abort its siblings (the per-bar honest-NULL quarantine
//! semantics of plan §15.8). Each element carries the kernel's
//! machine-prefixed error string (`invalid_input: …` / `no_solution: …`)
//! VERBATIM so the Python wrapper classifies with the SAME `startswith` it
//! uses for the scalar entry. The ONLY whole-call error is a column-length
//! mismatch — a caller bug, reported with the `invalid_input:` prefix and
//! every column's length named.
//!
//! Panic safety under `panic = "abort"` (honest envelope): the scalar kernel
//! is documented + tested never-panicking; this module adds only the length
//! gate (which makes the index loop provably in-bounds), tuple construction,
//! and ONE `Vec::with_capacity` — the Python caller chunk-caps batches at
//! 65,536 rows (`GREEKS_BATCH_MAX_ROWS`), bounding the element allocation to
//! ~4.6 MiB; an allocation failure aborts, the SAME disclosed residual as
//! every existing FFI path.

use super::{GreeksOut, greeks};

/// One element's solved values — the exact shape of the scalar FFI entry's
/// return tuple: `(iv, delta, gamma, theta, vega, rho, price, admissible)`.
pub type GreeksBatchValues = (f64, f64, f64, f64, f64, f64, f64, bool);

/// One element: `(values, error)` — EXACTLY one is `Some`. `error` carries
/// the kernel's machine-prefixed message (`invalid_input: …` /
/// `no_solution: …`) VERBATIM so the Python wrapper classifies with the SAME
/// `startswith` it uses today (`brutex/options/greeks.py`).
pub type GreeksBatchElem = (Option<GreeksBatchValues>, Option<String>);

fn values_of(g: GreeksOut) -> GreeksBatchValues {
    (
        g.iv,
        g.delta,
        g.gamma,
        g.theta,
        g.vega,
        g.rho,
        g.price,
        g.admissible,
    )
}

/// Route N parallel columns through the scalar [`greeks`] kernel, one
/// element at a time, preserving input order.
///
/// # Errors
///
/// `Err("invalid_input: greeks_batch column lengths differ — …")` when the
/// seven columns are not all the same length (a caller bug — the whole call
/// refuses, naming every column's length). Every PER-ELEMENT kernel `Err`
/// is data (`(None, Some(message))`), never a whole-call error.
pub fn greeks_batch_impl(
    price: &[Option<f64>],
    sigma: &[Option<f64>],
    forward: &[f64],
    strike: &[f64],
    t_years: &[f64],
    rate: &[f64],
    is_call: &[bool],
) -> Result<Vec<GreeksBatchElem>, String> {
    let n = forward.len();
    if price.len() != n
        || sigma.len() != n
        || strike.len() != n
        || t_years.len() != n
        || rate.len() != n
        || is_call.len() != n
    {
        return Err(format!(
            "invalid_input: greeks_batch column lengths differ — price={}, sigma={}, \
             forward={}, strike={}, t_years={}, rate={}, is_call={}",
            price.len(),
            sigma.len(),
            n,
            strike.len(),
            t_years.len(),
            rate.len(),
            is_call.len()
        ));
    }
    let mut out = Vec::with_capacity(n);
    // Index loop AFTER the length gate — every access is provably in-bounds,
    // so no element can panic (`panic = "abort"` discipline). The explicit
    // index (vs a 7-way iterator zip) keeps the per-element argument order
    // literally readable against the scalar signature.
    #[allow(clippy::needless_range_loop)]
    for i in 0..n {
        out.push(
            match greeks(
                price[i], sigma[i], forward[i], strike[i], t_years[i], rate[i], is_call[i],
            ) {
                Ok(g) => (Some(values_of(g)), None),
                Err(e) => (None, Some(e)),
            },
        );
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `f64::to_bits` on all 7 floats + the bool — the bit-for-bit compare.
    fn bits_of(v: GreeksBatchValues) -> ([u64; 7], bool) {
        (
            [
                v.0.to_bits(),
                v.1.to_bits(),
                v.2.to_bits(),
                v.3.to_bits(),
                v.4.to_bits(),
                v.5.to_bits(),
                v.6.to_bits(),
            ],
            v.7,
        )
    }

    /// One row of scalar-entry arguments.
    type Row = (Option<f64>, Option<f64>, f64, f64, f64, f64, bool);

    fn run_batch(rows: &[Row]) -> Vec<GreeksBatchElem> {
        greeks_batch_impl(
            &rows.iter().map(|r| r.0).collect::<Vec<_>>(),
            &rows.iter().map(|r| r.1).collect::<Vec<_>>(),
            &rows.iter().map(|r| r.2).collect::<Vec<_>>(),
            &rows.iter().map(|r| r.3).collect::<Vec<_>>(),
            &rows.iter().map(|r| r.4).collect::<Vec<_>>(),
            &rows.iter().map(|r| r.5).collect::<Vec<_>>(),
            &rows.iter().map(|r| r.6).collect::<Vec<_>>(),
        )
        .expect("equal-length columns must never whole-call fail")
    }

    /// Assert one batch element ≡ the scalar call on the same args:
    /// bit-for-bit values AND verbatim error-string equality.
    fn assert_elem_matches_scalar(row: Row, elem: &GreeksBatchElem) {
        let scalar = greeks(row.0, row.1, row.2, row.3, row.4, row.5, row.6);
        match scalar {
            Ok(g) => {
                let got = elem.0.expect("scalar Ok ⇒ batch values Some");
                assert_eq!(elem.1, None, "scalar Ok ⇒ batch error None");
                assert_eq!(bits_of(got), bits_of(values_of(g)), "bit drift on {row:?}");
            }
            Err(e) => {
                assert_eq!(elem.0, None, "scalar Err ⇒ batch values None");
                assert_eq!(
                    elem.1.as_deref(),
                    Some(e.as_str()),
                    "error string must cross VERBATIM on {row:?}"
                );
            }
        }
    }

    /// The 8-case mixed parity matrix (spec §3 Rust): ATM CE, ATM PE,
    /// sigma-direct, `no_solution: t_below_floor`, `no_solution:
    /// above_upper_bound`, 2× `invalid_input:`, deep-ITM near-floor.
    fn mixed_rows() -> Vec<Row> {
        vec![
            // ATM CE solve (liquid NIFTY-shaped).
            (
                Some(180.0),
                None,
                21_500.0,
                21_500.0,
                30.0 / 365.0,
                0.065,
                true,
            ),
            // ATM PE solve.
            (
                Some(165.0),
                None,
                21_500.0,
                21_500.0,
                30.0 / 365.0,
                0.065,
                false,
            ),
            // sigma-direct pricing (price ignored).
            (
                None,
                Some(0.18),
                21_500.0,
                21_000.0,
                45.0 / 365.0,
                0.065,
                true,
            ),
            // no_solution: t_below_floor (1 s << 600 s floor).
            (
                Some(50.0),
                None,
                21_500.0,
                21_500.0,
                1.0 / (365.0 * 86_400.0),
                0.065,
                true,
            ),
            // no_solution: above_upper_bound (call premium > forward).
            (
                Some(30_000.0),
                None,
                21_500.0,
                21_500.0,
                30.0 / 365.0,
                0.065,
                true,
            ),
            // invalid_input: non-positive forward.
            (Some(100.0), None, -1.0, 21_500.0, 30.0 / 365.0, 0.065, true),
            // invalid_input: need price or sigma — both None.
            (None, None, 21_500.0, 21_500.0, 30.0 / 365.0, 0.065, true),
            // Deep-ITM near-floor premium (tiny time value).
            (
                Some(3_502.0),
                None,
                21_500.0,
                18_000.0,
                20.0 / 365.0,
                0.065,
                true,
            ),
        ]
    }

    #[test]
    fn mixed_batch_matches_scalar_bit_for_bit() {
        let rows = mixed_rows();
        let out = run_batch(&rows);
        assert_eq!(out.len(), rows.len());
        for (row, elem) in rows.iter().zip(&out) {
            assert_elem_matches_scalar(*row, elem);
        }
        // The matrix genuinely covers all three outcome families.
        assert!(
            out.iter().any(|e| e.0.is_some()),
            "matrix must hold solved rows"
        );
        assert!(
            out.iter()
                .any(|e| e.1.as_deref().is_some_and(|m| m.starts_with("no_solution")))
        );
        assert!(out.iter().any(|e| {
            e.1.as_deref()
                .is_some_and(|m| m.starts_with("invalid_input"))
        }));
    }

    #[test]
    fn exactly_one_of_values_error_is_some() {
        for elem in run_batch(&mixed_rows()) {
            assert!(
                elem.0.is_some() ^ elem.1.is_some(),
                "exactly one of (values, error) must be Some: {elem:?}"
            );
        }
    }

    #[test]
    fn nan_and_inf_inputs_are_per_element_errors_never_aborts() {
        let rows: Vec<Row> = vec![
            (Some(f64::NAN), None, 21_500.0, 21_500.0, 0.1, 0.065, true),
            (Some(100.0), None, f64::INFINITY, 21_500.0, 0.1, 0.065, true),
            (Some(100.0), None, 21_500.0, f64::NAN, 0.1, 0.065, false),
            (Some(100.0), None, 21_500.0, 21_500.0, f64::NAN, 0.065, true),
            (
                Some(100.0),
                None,
                21_500.0,
                21_500.0,
                0.1,
                f64::INFINITY,
                true,
            ),
            (None, Some(f64::NAN), 21_500.0, 21_500.0, 0.1, 0.065, true),
            // A healthy sibling AFTER the junk — must still solve.
            (
                Some(180.0),
                None,
                21_500.0,
                21_500.0,
                30.0 / 365.0,
                0.065,
                true,
            ),
        ];
        let out = run_batch(&rows);
        for (row, elem) in rows.iter().zip(&out) {
            assert_elem_matches_scalar(*row, elem);
        }
        assert!(
            out[..6].iter().all(|e| e.0.is_none()),
            "junk rows are errors"
        );
        assert!(out[6].0.is_some(), "the healthy sibling still solves");
    }

    #[test]
    fn empty_batch_is_ok_empty() {
        let out = greeks_batch_impl(&[], &[], &[], &[], &[], &[], &[])
            .expect("empty batch is a valid no-op");
        assert!(out.is_empty());
    }

    /// Build 7 constant columns of the given lengths and demand the
    /// whole-call refusal.
    fn mismatch_err(lens: [usize; 7]) -> String {
        greeks_batch_impl(
            &vec![Some(180.0); lens[0]],
            &vec![None; lens[1]],
            &vec![21_500.0; lens[2]],
            &vec![21_500.0; lens[3]],
            &vec![30.0 / 365.0; lens[4]],
            &vec![0.065; lens[5]],
            &vec![true; lens[6]],
        )
        .expect_err("mismatched columns must whole-call refuse")
    }

    #[test]
    fn length_mismatch_is_whole_call_err_naming_every_length() {
        // Grow each of the 7 columns in turn (growing `forward`, the
        // reference length n, makes all six OTHERS mismatch) — every shape
        // must refuse with the invalid_input prefix, all seven column
        // lengths named, and the true differing counts visible.
        for grown in 0..7usize {
            let mut lens = [2usize; 7];
            lens[grown] = 3;
            let err = mismatch_err(lens);
            assert!(
                err.starts_with("invalid_input: greeks_batch column lengths differ"),
                "col {grown}: wrong error prefix: {err}"
            );
            for col in [
                "price=", "sigma=", "forward=", "strike=", "t_years=", "rate=", "is_call=",
            ] {
                assert!(
                    err.contains(col),
                    "col {grown}: error must name {col}: {err}"
                );
            }
            assert!(
                err.contains("=3"),
                "col {grown}: must name the odd length: {err}"
            );
            assert!(
                err.contains("=2"),
                "col {grown}: must name the base length: {err}"
            );
        }
    }

    #[test]
    fn permuted_input_order_is_preserved() {
        let rows = mixed_rows();
        let forward_out = run_batch(&rows);
        let mut reversed = rows.clone();
        reversed.reverse();
        let reversed_out = run_batch(&reversed);
        for (i, elem) in forward_out.iter().enumerate() {
            let mirrored = &reversed_out[reversed_out.len() - 1 - i];
            assert_eq!(
                elem.0.map(bits_of),
                mirrored.0.map(bits_of),
                "row {i}: values must follow input position, not content"
            );
            assert_eq!(
                elem.1, mirrored.1,
                "row {i}: errors must follow input position"
            );
        }
    }
}
