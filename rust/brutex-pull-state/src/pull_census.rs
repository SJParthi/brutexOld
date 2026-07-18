//! The written-vs-settled CENSUS fold kernel (Wave 3G,
//! `DEC-RUST-PULL-STATE-001`).
//!
//! Re-hosts the pure fold of `brutex/dashboard/pull_census.py`
//! (DEC-WRITTEN-SETTLED-CENSUS-001, observe-only): the SCOPED
//! `(symbol, interval, is_success)` ledger rows and the scoped bars>0
//! `(symbol, interval)` written pairs arrive PRE-extracted (the
//! `str()`/`int()` conversions over arbitrary payload objects — and the
//! lazy-`int()`-after-the-scope-filter law — stay Python); the kernel folds
//! the two series SETS, takes both differences, sorts them and caps the
//! mismatch samples at [`CENSUS_SAMPLE_CAP`].
//!
//! SORT-ORDER law: the oracle sorts `(symbol, interval)` tuples by CODE
//! POINT; Rust's `BTreeSet<(String, String)>` orders by UTF-8 BYTES — the
//! two coincide for all valid UTF-8 (UTF-8 preserves code-point order; a
//! lone-surrogate CPython str cannot cross the pyo3 edge at all — the seam's
//! defensive belt degrades that call to the oracle).  The `repr()`-rendered
//! sample STRINGS stay Python; only `(kind, symbol, interval)` triples cross
//! back.

/// Max mismatch samples carried — sync-pinned against the Python
/// `pull_census.CENSUS_SAMPLE_CAP` by the seam suite.
pub const CENSUS_SAMPLE_CAP: usize = 20;

/// Sample kind: a ledger-settled series with NO written lake span.
pub const SAMPLE_SETTLED_UNWRITTEN: u8 = 0;
/// Sample kind: a written lake span with NO settled ledger row.
pub const SAMPLE_WRITTEN_UNSETTLED: u8 = 1;

/// The census fold — pure, clockless, TOTAL.
///
/// Returns `(settled_unwritten, written_unsettled, samples)`; `scanned`
/// deliberately does NOT cross (the caller counted it during its own scope
/// filter).  Samples are `(kind, symbol, interval)` in the oracle's exact
/// order: ALL sorted settled-unwritten pairs first, then sorted
/// written-unsettled, capped at [`CENSUS_SAMPLE_CAP`] TOTAL.
#[must_use]
pub fn census_fold(
    rows: &[(String, String, bool)],
    written_pairs: &[(String, String)],
) -> (u64, u64, Vec<(u8, String, String)>) {
    use std::collections::BTreeSet;

    let mut settled: BTreeSet<(&str, &str)> = BTreeSet::new();
    for (symbol, interval, is_success) in rows {
        if *is_success {
            settled.insert((symbol.as_str(), interval.as_str()));
        }
    }
    let mut written: BTreeSet<(&str, &str)> = BTreeSet::new();
    for (symbol, interval) in written_pairs {
        written.insert((symbol.as_str(), interval.as_str()));
    }
    let settled_unwritten: Vec<(&str, &str)> = settled.difference(&written).copied().collect();
    let written_unsettled: Vec<(&str, &str)> = written.difference(&settled).copied().collect();
    let mut samples: Vec<(u8, String, String)> = Vec::new();
    for (symbol, interval) in &settled_unwritten {
        if samples.len() >= CENSUS_SAMPLE_CAP {
            break;
        }
        samples.push((
            SAMPLE_SETTLED_UNWRITTEN,
            (*symbol).to_owned(),
            (*interval).to_owned(),
        ));
    }
    for (symbol, interval) in &written_unsettled {
        if samples.len() >= CENSUS_SAMPLE_CAP {
            break;
        }
        samples.push((
            SAMPLE_WRITTEN_UNSETTLED,
            (*symbol).to_owned(),
            (*interval).to_owned(),
        ));
    }
    (
        settled_unwritten.len() as u64,
        written_unsettled.len() as u64,
        samples,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pair(s: &str, i: &str) -> (String, String) {
        (s.to_owned(), i.to_owned())
    }

    #[test]
    fn agreement_is_clean() {
        let rows = [
            ("A".to_owned(), "1minute".to_owned(), true),
            ("B".to_owned(), "1minute".to_owned(), true),
        ];
        let written = [pair("A", "1minute"), pair("B", "1minute")];
        let (su, wu, samples) = census_fold(&rows, &written);
        assert_eq!((su, wu), (0, 0));
        assert!(samples.is_empty());
    }

    #[test]
    fn non_success_rows_never_settle() {
        let rows = [("A".to_owned(), "1minute".to_owned(), false)];
        let written = [pair("A", "1minute")];
        let (su, wu, samples) = census_fold(&rows, &written);
        assert_eq!((su, wu), (0, 1));
        assert_eq!(
            samples,
            vec![(
                SAMPLE_WRITTEN_UNSETTLED,
                "A".to_owned(),
                "1minute".to_owned()
            )]
        );
    }

    #[test]
    fn samples_sorted_and_capped_across_both_classes() {
        let rows: Vec<(String, String, bool)> = (0..15)
            .map(|n| (format!("SU{n:03}"), "1minute".to_owned(), true))
            .collect();
        let written: Vec<(String, String)> = (0..10)
            .map(|n| (format!("WU{n:03}"), "1minute".to_owned()))
            .collect();
        let (su, wu, samples) = census_fold(&rows, &written);
        assert_eq!((su, wu), (15, 10));
        assert_eq!(samples.len(), CENSUS_SAMPLE_CAP);
        let kinds: Vec<u8> = samples.iter().map(|s| s.0).collect();
        assert_eq!(kinds, [vec![0u8; 15], vec![1u8; 5]].concat());
        let symbols: Vec<&str> = samples.iter().map(|s| s.1.as_str()).collect();
        assert!(symbols.windows(2).take(14).all(|w| w[0] <= w[1]));
    }

    #[test]
    fn duplicate_series_dedup() {
        let rows = [
            ("A".to_owned(), "1minute".to_owned(), true),
            ("A".to_owned(), "1minute".to_owned(), true),
        ];
        let (su, wu, samples) = census_fold(&rows, &[]);
        assert_eq!((su, wu), (1, 0));
        assert_eq!(samples.len(), 1);
    }
}
