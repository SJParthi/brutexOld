//! # audits — the instrument-master AUDIT pure laws (SLICE T24 PR-3a)
//!
//! DEC-DATA-RUST-INTEGRITY-DETECTORS-001 (MIGRATION.md #1948 — PR-1 laws
//! 1-3, PR-2 law 4; PR-3a adds laws 5-6). Mirrors, token for token, the TWO
//! pure parse-time audit folds of the `brutex/data/` instrument-master
//! audit family behind the default-OFF facade
//! `brutex/data/detectors_rust.py` (two flags —
//! `BRUTEX_LAKE_RUST_ECONOMICS_INTEGRITY` /
//! `BRUTEX_LAKE_RUST_MASTER_INTEGRITY`):
//!
//! 5. **Economics lot fold** — the group folds + verdict derivation of
//!    `brutex/data/economics_integrity.py::audit_economics_integrity`
//!    (DEC-INSTRUMENT-ECONOMICS-INTEGRITY-001): distinct lot_size per
//!    `(exchange, underlying, expiry)` group and per `(exchange,
//!    underlying)`, flagging every group holding more than one distinct
//!    lot. Ints-only in the STRONGEST form — **no string semantics enter
//!    this crate at all**: Python interns every key to a dense id and
//!    pre-computes every Unicode-sensitive predicate (the `*NSETEST*`
//!    `.upper()` marker containment, the empty-underlying skip) BEFORE the
//!    per-leg vectors are built; Python also owns the report-visible
//!    capped-sample ORDER (the full-key string sort stays where the
//!    strings live). Rust sorts only i64 lot values — where the Python and
//!    Rust orders are trivially identical.
//! 6. **Master integrity fold** — the two-pass spot-table build +
//!    leg-classification decision of
//!    `brutex/data/master_integrity.py::audit_master_integrity`
//!    (DEC-INSTRUMENT-MASTER-INTEGRITY-001) over interned ids ONLY. The
//!    Unicode-trap predicates (`str.isdigit()` on `feed_token` — the
//!    #1594-round-2 `'²²'` class — the test-marker containment, the
//!    special-char tally), the alias reversal, the Decimal-bearing dedup
//!    key AND the type counts all stay PYTHON (the dedup is a set
//!    membership on a Python tuple with a `Decimal` member — interning it
//!    for Rust to re-count would be pure ceremony; a recorded design
//!    decision, the PR-2 close-edge-laziness mirror). Rust replays the
//!    dict-op sequence on ids: the FIRST-IDENTITY/ambiguity precedence of
//!    pass 1 (`spot_by_symbol` keeps the FIRST identity; a later DISTINCT
//!    identity marks the key ambiguous while the stored mapping stays the
//!    first — and `sym_ambiguous` WINS the classification even when the
//!    stored mapping would agree with the token hit) plus the 7-way pass-2
//!    bucket order (ambiguous > disagreement > both > symbol_only >
//!    token_only > orphan; the empty/test buckets are Python-filtered
//!    BEFORE the vectors are built). Per-leg bucket CODES cross back (the
//!    DEC-FEED-RUST-CORE-001 indices shape) — Python tallies the counts it
//!    must walk anyway for the input-order orphan sample, so the design
//!    sketch's separate count returns (and their sum-consistency poison
//!    channel) are structurally absent (the Q8 recording).
//!
//! NO I/O, NO polars — the crate's polars REV pin is irrelevant to this
//! module; the facade's engagement guard is the SYMBOL handshake (all six
//! detector-family callables present + callable), never the polars version
//! guard.
//!
//! HONESTY (ADR-0020 banner, verbatim): expected payoff ≈ 0 — conversion
//! is for uniformity, not speed. Both laws run ~once per master load per
//! process (the instrument-loader ingest hook); the FFI hop plus the
//! Python interning ceremony is a small SLOWDOWN, accepted because the
//! path is vendor-I/O-dominated and the seams ship DEFAULT-OFF. The
//! audits gate the /universe integrity panels' persisted verdicts —
//! decision parity, not speed, is the bar.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;

// ────────────────────────────────────────────────────────────────────────
// Law 5 — the economics lot fold
// ────────────────────────────────────────────────────────────────────────

/// The fields crossing back to Python:
/// `(mismatch_group_ids, mismatch_lots, revision_under_ids, revision_lots,
/// expiry_group_count)`. Group/under ids are the Python-interned dense
/// ids; each flagged id's distinct lots are SORTED ascending (the
/// `tuple(sorted(lots))` law — i64 order ≡ Python int order); the id lists
/// are in ascending-id order (deterministic — Python re-sorts them on its
/// own full string keys for the report sample). `expiry_group_count` is
/// the crate-observed distinct-group count — the facade refuses a value
/// that differs from the Python-known interned count (a poisoned crate).
pub type EconomicsFoldFields = (Vec<i64>, Vec<Vec<i64>>, Vec<i64>, Vec<Vec<i64>>, u64);

/// The `audit_economics_integrity` group folds, verbatim over ids: one
/// linear sweep collecting the distinct lot per group
/// (`lots_by_expiry.setdefault(...).add(lot)`) and per underlying
/// (`lots_by_under`), then the >1-distinct-lot derivations. `Err` on a
/// column-length divergence (the PyO3 edge maps it to `ValueError` — a
/// facade degrade member).
pub fn economics_lot_fold_impl(
    group_ids: &[i64],
    under_ids: &[i64],
    lots: &[i64],
) -> Result<EconomicsFoldFields, String> {
    let n = group_ids.len();
    if under_ids.len() != n || lots.len() != n {
        return Err(format!(
            "economics_lot_fold: column lengths diverge (group_ids={}, under_ids={}, lots={})",
            n,
            under_ids.len(),
            lots.len()
        ));
    }
    // BTreeMap/BTreeSet: deterministic ascending-id iteration + ascending
    // lot order — the `sorted(...)` laws come for free, never a HashMap
    // iteration-order nondeterminism.
    let mut by_group: BTreeMap<i64, BTreeSet<i64>> = BTreeMap::new();
    let mut by_under: BTreeMap<i64, BTreeSet<i64>> = BTreeMap::new();
    for i in 0..n {
        by_group.entry(group_ids[i]).or_default().insert(lots[i]);
        by_under.entry(under_ids[i]).or_default().insert(lots[i]);
    }
    let expiry_group_count = by_group.len() as u64;
    let mut mismatch_group_ids: Vec<i64> = Vec::new();
    let mut mismatch_lots: Vec<Vec<i64>> = Vec::new();
    for (&gid, lot_set) in &by_group {
        if lot_set.len() > 1 {
            mismatch_group_ids.push(gid);
            mismatch_lots.push(lot_set.iter().copied().collect());
        }
    }
    let mut revision_under_ids: Vec<i64> = Vec::new();
    let mut revision_lots: Vec<Vec<i64>> = Vec::new();
    for (&uid, lot_set) in &by_under {
        if lot_set.len() > 1 {
            revision_under_ids.push(uid);
            revision_lots.push(lot_set.iter().copied().collect());
        }
    }
    Ok((
        mismatch_group_ids,
        mismatch_lots,
        revision_under_ids,
        revision_lots,
        expiry_group_count,
    ))
}

/// FFI face of [`economics_lot_fold_impl`] — interned ids + lots in, the
/// fold fields out (`ValueError` on a length precondition; `RuntimeError`
/// on a caught panic, never a raw `PanicException` into the facade).
#[pyfunction]
#[pyo3(signature = (group_ids, under_ids, lots))]
pub(crate) fn economics_lot_fold(
    group_ids: Vec<i64>,
    under_ids: Vec<i64>,
    lots: Vec<i64>,
) -> PyResult<EconomicsFoldFields> {
    std::panic::catch_unwind(|| economics_lot_fold_impl(&group_ids, &under_ids, &lots))
        .map_err(|_| {
            PyRuntimeError::new_err(
                "brutex-lake economics_lot_fold panicked (caught at the FFI boundary)",
            )
        })?
        .map_err(PyValueError::new_err)
}

// ────────────────────────────────────────────────────────────────────────
// Law 6 — the master-integrity fold
// ────────────────────────────────────────────────────────────────────────

/// `resolved_both` bucket code (symbol + token agree).
pub const BUCKET_RESOLVED_BOTH: u8 = 0;
/// `resolved_symbol_only` bucket code.
pub const BUCKET_SYMBOL_ONLY: u8 = 1;
/// `resolved_token_only` bucket code (the special-char `M&M` class).
pub const BUCKET_TOKEN_ONLY: u8 = 2;
/// `orphan` bucket code (no spot on either join).
pub const BUCKET_ORPHAN: u8 = 3;
/// `ambiguous` bucket code (the symbol key maps >1 distinct spot).
pub const BUCKET_AMBIGUOUS: u8 = 4;
/// `disagreement` bucket code (symbol→A, token→B).
pub const BUCKET_DISAGREEMENT: u8 = 5;

/// The sentinel for an ABSENT key id (`-1` — a leg with no symbol / no
/// token, a spot with no genuine numeric token). Any id `>= 0` is a real
/// Python-interned key.
pub const NO_KEY: i64 = -1;

/// The `audit_master_integrity` two-pass fold, verbatim over ids.
///
/// Pass 1 (spots — one entry per IDX/EQ row, input order): replays the
/// dict-op sequence EXACTLY —
///
/// ```text
/// existing = spot_by_symbol.get(sym_key)
/// if existing is not None and existing != identity:
///     ambiguous_symbol_keys.add(sym_key)      # stored mapping stays FIRST
/// else:
///     spot_by_symbol[sym_key] = identity      # None OR same-identity repeat
/// if tok_key >= 0:
///     spot_by_token[tok_key] = identity       # repeat overwrite (same spot)
/// spot_identities.add(identity)
/// ```
///
/// Pass 2 (legs — POST the Python empty-underlying/test-marker filtering,
/// input order): the 7-way classification with `sym_ambiguous` FIRST (it
/// wins even when the stored first-identity mapping would agree with the
/// token hit — the highest-fidelity precedence, hypothesis-differential
/// pinned facade-side), then disagreement > both > symbol_only >
/// token_only > orphan; resolved legs fold their hit identity into
/// `referenced`.
///
/// Returns `(bucket_codes, unreferenced_spot_count)` — one code per leg
/// (input order) + `len(spot_identities - referenced)` (the
/// `spots_with_no_legs` scalar; returning the count instead of the id
/// vector is the smaller ints-only surface — design §2.2's "either is
/// ints-only" latitude, recorded). `Err` on a column-length divergence.
pub fn master_integrity_fold_impl(
    spot_sym_key_ids: &[i64],
    spot_identity_ids: &[i64],
    spot_token_key_ids: &[i64],
    leg_sym_key_ids: &[i64],
    leg_tok_key_ids: &[i64],
) -> Result<(Vec<u8>, u64), String> {
    let n_spots = spot_sym_key_ids.len();
    if spot_identity_ids.len() != n_spots || spot_token_key_ids.len() != n_spots {
        return Err(format!(
            "master_integrity_fold: spot column lengths diverge (sym={}, identity={}, token={})",
            n_spots,
            spot_identity_ids.len(),
            spot_token_key_ids.len()
        ));
    }
    let n_legs = leg_sym_key_ids.len();
    if leg_tok_key_ids.len() != n_legs {
        return Err(format!(
            "master_integrity_fold: leg column lengths diverge (sym={}, token={})",
            n_legs,
            leg_tok_key_ids.len()
        ));
    }

    // Pass 1 — the spot lookup tables.
    let mut spot_by_symbol: HashMap<i64, i64> = HashMap::new();
    let mut ambiguous_symbol_keys: HashSet<i64> = HashSet::new();
    let mut spot_by_token: HashMap<i64, i64> = HashMap::new();
    let mut spot_identities: HashSet<i64> = HashSet::new();
    for i in 0..n_spots {
        let identity = spot_identity_ids[i];
        spot_identities.insert(identity);
        let sym_key = spot_sym_key_ids[i];
        match spot_by_symbol.get(&sym_key) {
            Some(&existing) if existing != identity => {
                // A later DISTINCT identity: the key turns ambiguous but the
                // stored mapping stays the FIRST identity (never re-stored).
                ambiguous_symbol_keys.insert(sym_key);
            }
            _ => {
                // Unseen key OR a same-identity repeat — the Python `else`
                // arm (a repeat re-stores the same identity, harmless).
                spot_by_symbol.insert(sym_key, identity);
            }
        }
        let tok_key = spot_token_key_ids[i];
        if tok_key >= 0 {
            // The token key IS the identity tuple Python-side, so a repeat
            // maps the same spot — overwrite exactly like the dict.
            spot_by_token.insert(tok_key, identity);
        }
    }

    // Pass 2 — classify every routed leg.
    let mut bucket_codes: Vec<u8> = Vec::with_capacity(n_legs);
    let mut referenced: HashSet<i64> = HashSet::new();
    for i in 0..n_legs {
        let sym_key = leg_sym_key_ids[i];
        let tok_key = leg_tok_key_ids[i];
        let sym_ambiguous = sym_key >= 0 && ambiguous_symbol_keys.contains(&sym_key);
        let sym_hit = if sym_key >= 0 {
            spot_by_symbol.get(&sym_key).copied()
        } else {
            None
        };
        let tok_hit = if tok_key >= 0 {
            spot_by_token.get(&tok_key).copied()
        } else {
            None
        };
        let code = if sym_ambiguous {
            BUCKET_AMBIGUOUS
        } else {
            match (sym_hit, tok_hit) {
                (Some(sym_spot), Some(tok_spot)) if sym_spot != tok_spot => BUCKET_DISAGREEMENT,
                (Some(sym_spot), Some(_)) => {
                    referenced.insert(sym_spot);
                    BUCKET_RESOLVED_BOTH
                }
                (Some(sym_spot), None) => {
                    referenced.insert(sym_spot);
                    BUCKET_SYMBOL_ONLY
                }
                (None, Some(tok_spot)) => {
                    referenced.insert(tok_spot);
                    BUCKET_TOKEN_ONLY
                }
                (None, None) => BUCKET_ORPHAN,
            }
        };
        bucket_codes.push(code);
    }
    let unreferenced = spot_identities.difference(&referenced).count() as u64;
    Ok((bucket_codes, unreferenced))
}

/// FFI face of [`master_integrity_fold_impl`] — interned ids in, per-leg
/// bucket codes + the unreferenced-spot count out (`ValueError` on a
/// length precondition; `RuntimeError` on a caught panic, never a raw
/// `PanicException` into the facade).
#[pyfunction]
#[pyo3(signature = (spot_sym_key_ids, spot_identity_ids, spot_token_key_ids, leg_sym_key_ids, leg_tok_key_ids))]
pub(crate) fn master_integrity_fold(
    spot_sym_key_ids: Vec<i64>,
    spot_identity_ids: Vec<i64>,
    spot_token_key_ids: Vec<i64>,
    leg_sym_key_ids: Vec<i64>,
    leg_tok_key_ids: Vec<i64>,
) -> PyResult<(Vec<u8>, u64)> {
    std::panic::catch_unwind(|| {
        master_integrity_fold_impl(
            &spot_sym_key_ids,
            &spot_identity_ids,
            &spot_token_key_ids,
            &leg_sym_key_ids,
            &leg_tok_key_ids,
        )
    })
    .map_err(|_| {
        PyRuntimeError::new_err(
            "brutex-lake master_integrity_fold panicked (caught at the FFI boundary)",
        )
    })?
    .map_err(PyValueError::new_err)
}

#[cfg(test)]
mod tests {
    use super::*;

    // ------------------------------------------------------------------
    // Law 5 — the economics lot fold
    // ------------------------------------------------------------------

    #[test]
    fn uniform_lots_yield_no_mismatch() {
        // One group, one lot repeated — green; the group still counts.
        let got = economics_lot_fold_impl(&[0, 0, 0], &[0, 0, 0], &[500, 500, 500]).unwrap();
        assert_eq!(got, (vec![], vec![], vec![], vec![], 1));
    }

    #[test]
    fn within_group_mismatch_flagged_with_sorted_lots() {
        // Lots arrive DESCENDING — the returned distinct lots are ascending
        // (the `tuple(sorted(lots))` law).
        let got = economics_lot_fold_impl(&[0, 0], &[0, 0], &[750, 500]).unwrap();
        assert_eq!(got.0, vec![0]);
        assert_eq!(got.1, vec![vec![500, 750]]);
        // The same two lots on ONE underlying also read as a revision.
        assert_eq!(got.2, vec![0]);
        assert_eq!(got.3, vec![vec![500, 750]]);
        assert_eq!(got.4, 1);
    }

    #[test]
    fn cross_group_revision_without_mismatch() {
        // The AMBUJACEM shape: two expiries (groups 0/1) of ONE underlying
        // (under 0) with lots 1050 → 1200 — no group mismatch, one revision.
        let got = economics_lot_fold_impl(&[0, 1], &[0, 0], &[1050, 1200]).unwrap();
        assert_eq!(got.0, vec![]);
        assert_eq!(got.1, Vec::<Vec<i64>>::new());
        assert_eq!(got.2, vec![0]);
        assert_eq!(got.3, vec![vec![1050, 1200]]);
        assert_eq!(got.4, 2);
    }

    #[test]
    fn mismatch_and_revision_ids_ascend_and_count_groups() {
        // Three groups over two underlyings; groups 2 and 0 both mismatch —
        // returned in ASCENDING id order regardless of input order.
        let group_ids = [2, 2, 0, 0, 1];
        let under_ids = [1, 1, 0, 0, 0];
        let lots = [75, 50, 750, 500, 500];
        let got = economics_lot_fold_impl(&group_ids, &under_ids, &lots).unwrap();
        assert_eq!(got.0, vec![0, 2]);
        assert_eq!(got.1, vec![vec![500, 750], vec![50, 75]]);
        assert_eq!(got.2, vec![0, 1]);
        assert_eq!(got.3, vec![vec![500, 750], vec![50, 75]]);
        assert_eq!(got.4, 3);
    }

    #[test]
    fn empty_input_is_empty_output() {
        let got = economics_lot_fold_impl(&[], &[], &[]).unwrap();
        assert_eq!(got, (vec![], vec![], vec![], vec![], 0));
    }

    #[test]
    fn economics_length_divergence_is_an_error() {
        assert!(economics_lot_fold_impl(&[0], &[0, 1], &[500]).is_err());
        assert!(economics_lot_fold_impl(&[0], &[0], &[500, 750]).is_err());
    }

    // ------------------------------------------------------------------
    // Law 6 — the master-integrity fold
    // ------------------------------------------------------------------

    #[test]
    fn resolved_both_and_referenced() {
        // One spot (sym 0, identity 0, token 0); one leg naming both.
        let got = master_integrity_fold_impl(&[0], &[0], &[0], &[0], &[0]).unwrap();
        assert_eq!(got.0, vec![BUCKET_RESOLVED_BOTH]);
        assert_eq!(got.1, 0); // referenced — no spot without legs
    }

    #[test]
    fn symbol_only_and_token_only() {
        // Spot 0: sym 0, identity 0, NO token; spot 1: sym 1, identity 1,
        // token 5. Leg A: symbol 0 only → symbol_only; leg B: unseen symbol
        // 9 + token 5 → token_only.
        let got =
            master_integrity_fold_impl(&[0, 1], &[0, 1], &[NO_KEY, 5], &[0, 9], &[NO_KEY, 5])
                .unwrap();
        assert_eq!(got.0, vec![BUCKET_SYMBOL_ONLY, BUCKET_TOKEN_ONLY]);
        assert_eq!(got.1, 0);
    }

    #[test]
    fn orphan_when_neither_join_hits() {
        let got = master_integrity_fold_impl(&[0], &[0], &[0], &[7], &[9]).unwrap();
        assert_eq!(got.0, vec![BUCKET_ORPHAN]);
        assert_eq!(got.1, 1); // the lone spot is unreferenced
    }

    #[test]
    fn no_key_sentinels_never_join() {
        // A leg with NEITHER key would be Python-filtered (empty bucket) —
        // but the sentinel law itself is pinned: -1 never joins any table.
        let got = master_integrity_fold_impl(&[0], &[0], &[0], &[NO_KEY], &[NO_KEY]).unwrap();
        assert_eq!(got.0, vec![BUCKET_ORPHAN]);
    }

    #[test]
    fn ambiguity_keeps_first_identity_and_wins_classification() {
        // Two spots share sym key 0 with DISTINCT identities 0 then 1 — the
        // key turns ambiguous; the stored mapping stays identity 0. A leg
        // naming sym 0 + token 3 (→ identity 0, AGREEING with the stored
        // first identity) is STILL ambiguous — sym_ambiguous wins the
        // classification (the highest-fidelity precedence).
        let got = master_integrity_fold_impl(
            &[0, 0],
            &[0, 1],
            &[3, NO_KEY],
            &[0],
            &[3],
        )
        .unwrap();
        assert_eq!(got.0, vec![BUCKET_AMBIGUOUS]);
        // NOTHING referenced — both identities are spots with no legs.
        assert_eq!(got.1, 2);
    }

    #[test]
    fn same_identity_repeat_never_marks_ambiguous() {
        // The SAME spot listed twice (sym 0 → identity 0 both times): the
        // Python `else` arm re-stores harmlessly — never ambiguous.
        let got = master_integrity_fold_impl(&[0, 0], &[0, 0], &[NO_KEY, NO_KEY], &[0], &[NO_KEY])
            .unwrap();
        assert_eq!(got.0, vec![BUCKET_SYMBOL_ONLY]);
        assert_eq!(got.1, 0);
    }

    #[test]
    fn ambiguous_key_third_repeat_of_first_identity_stays_ambiguous() {
        // sym 0 sees identities 0, 1 (→ ambiguous), then 0 again (the `_`
        // arm re-stores the first identity) — the key STAYS ambiguous.
        let got = master_integrity_fold_impl(
            &[0, 0, 0],
            &[0, 1, 0],
            &[NO_KEY, NO_KEY, NO_KEY],
            &[0],
            &[NO_KEY],
        )
        .unwrap();
        assert_eq!(got.0, vec![BUCKET_AMBIGUOUS]);
    }

    #[test]
    fn disagreement_when_symbol_and_token_name_distinct_spots() {
        // Spot A: sym 0 / identity 0 / token 10; spot B: sym 1 / identity 1
        // / token 11. A leg naming sym 0 (→ A) + token 11 (→ B) disagrees.
        let got =
            master_integrity_fold_impl(&[0, 1], &[0, 1], &[10, 11], &[0], &[11]).unwrap();
        assert_eq!(got.0, vec![BUCKET_DISAGREEMENT]);
        // A disagreement references NEITHER spot.
        assert_eq!(got.1, 2);
    }

    #[test]
    fn token_repeat_overwrites_like_the_dict() {
        // The same token key twice (the same spot re-listed): insert
        // overwrite — the leg still token-joins to that identity.
        let got = master_integrity_fold_impl(&[0, 0], &[0, 0], &[4, 4], &[9], &[4]).unwrap();
        assert_eq!(got.0, vec![BUCKET_TOKEN_ONLY]);
        assert_eq!(got.1, 0);
    }

    #[test]
    fn unreferenced_count_over_mixed_legs() {
        // Spots 0/1/2; legs reference identity 0 (both) and identity 1
        // (token) — spot 2 alone is unreferenced.
        let got = master_integrity_fold_impl(
            &[0, 1, 2],
            &[0, 1, 2],
            &[10, 11, 12],
            &[0, NO_KEY],
            &[10, 11],
        )
        .unwrap();
        assert_eq!(got.0, vec![BUCKET_RESOLVED_BOTH, BUCKET_TOKEN_ONLY]);
        assert_eq!(got.1, 1);
    }

    #[test]
    fn empty_master_is_empty_output() {
        let got = master_integrity_fold_impl(&[], &[], &[], &[], &[]).unwrap();
        assert_eq!(got.0, Vec::<u8>::new());
        assert_eq!(got.1, 0);
    }

    #[test]
    fn master_length_divergence_is_an_error() {
        assert!(master_integrity_fold_impl(&[0], &[0, 1], &[0], &[], &[]).is_err());
        assert!(master_integrity_fold_impl(&[0], &[0], &[0, 1], &[], &[]).is_err());
        assert!(master_integrity_fold_impl(&[0], &[0], &[0], &[0], &[0, 1]).is_err());
    }
}
