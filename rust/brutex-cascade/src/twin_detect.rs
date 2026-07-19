//! # `twin_detect` — the behavioral-twin fingerprint / grouping laws
//!
//! DEC-RUST-CASCADE-RESULTS-001 phase B, slice 2d: the law families of the
//! deleted `brutex/engine/twin_detect.py` bodies, ported token-for-token and
//! verified against the FROZEN pre-conversion parity corpus
//! (`tests/golden/fixtures/cascade_rust_parity/twin_detect.json`).
//!
//! ## The blake3 split (the recorded slice-2d decision)
//!
//! `Cargo.toml` is pinned pyo3-only, so the BLAKE3 DIGEST call stays
//! SHIM-side (the `blake3` wheel the deleted law already imported): this
//! module builds the canonical `bxtwin1|…` serialization BYTES — the
//! injective law (every `|` / `\n` separator is unreachable from the
//! integer and `BUY`/`SELL` field alphabets) — and the Python shim hashes
//! them. The version PREFIX is threaded from the shim's
//! `FINGERPRINT_VERSION_PREFIX` constant (single source stays Python — the
//! slice-2c `SORT_ALIASES` precedent).
//!
//! ## Laws hosted here
//!
//! * the fingerprint serialization (`entry_us|exit_us|direction|pnl` lines,
//!   newline-joined, GIVEN order — never re-sorted);
//! * the `TwinCandidate` validation matrix — run_id truthiness, the
//!   `mask_bits < 0` Python rich compare (the `mask_bits=True` bool==int pun
//!   ACCEPTED), the `[0-9a-f]{64}` fullmatch fingerprint grammar with
//!   CPython `re`'s own raw `TypeError` escapes on non-string input
//!   (`expected string or bytes-like object, got '<T>'`; a bytes-like gets
//!   `cannot use a string pattern on a bytes-like object`);
//! * `_jaccard` — ONE f64 division `len(a & b) / len(a | b)`; two EMPTY
//!   sets are DEFINED identical (`1.0`);
//! * `group_twins` — the threshold envelope `(0.0, 1.0]` via Python rich
//!   compares on the RAW object (`True` is ACCEPTED and behaves as `1.0`;
//!   `-0.0` / `nan` / `inf` refuse with the frozen `{t!r}` message), the
//!   first-duplicate run_id refusal, and the two-tier union-find partition
//!   (tier 1 identical fingerprints anchored at the FIRST sorted holder;
//!   tier 2 sorted-pair enumeration with the INCLUSIVE `>=` boundary;
//!   union keeps the lexicographically smaller root; representative =
//!   fewest `mask_bits.bit_count()` then run_id ASC; `members_by_family`
//!   iterates family_id ASC — captured as an ordered pair list so the
//!   Python-facing dicts reproduce the insertion order).
//!
//! ## Marshalling
//!
//! [`group`] returns INDICES into the caller's candidate list — the shim
//! maps them back to the ORIGINAL `run_id` str objects, so no string is
//! ever re-minted crate-side (str-subclass identity preserved on the
//! output surface).
//!
//! ## Disclosed divergence envelope (non-production shapes)
//!
//! * a lone-surrogate `run_id` / `fingerprint` fails UTF-8 extraction
//!   LOUDLY inside [`group`] (`UnicodeEncodeError`) where the deleted
//!   Python law would have grouped it — corpus-unreachable (fingerprints
//!   are validated 64-hex; run_ids are BLAKE3 hex in production);
//! * an `entry_ts_us` member beyond ±i64 fails extraction (`OverflowError`)
//!   where Python compared unbounded ints — epoch-µs instants are i64 by
//!   the models-layer datetime domain (years 1..9999 ≈ ±2.5e17 µs);
//! * a `pnl_paisa` beyond ±i64 fails extraction at the shim's serialize
//!   marshalling — int64-paisa is the `CapturedTrade` contract;
//! * an exact-numeric `jaccard_threshold` (`Fraction`, `Decimal` — any
//!   exact-numeric type with `__float__`) passes the rich-compare
//!   envelope then extracts via `__float__`, so the tier-2 `>=` compares
//!   against the ROUNDED double where the deleted law compared exactly —
//!   pinned BOTH ways for Fraction AND Decimal in the differential
//!   suite's `TestDisclosedEnvelope` (the production threshold is the
//!   module constant float; an undocumented input class, corpus-free).

use std::collections::{BTreeSet, HashMap};

use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::types::{PyByteArray, PyBytes, PyMemoryView, PyString};

use crate::{capture_repr, domain_err};

/// The grouping output in ORIGINAL-index space: `(family_pairs, members)` —
/// see [`group_core`].
pub type GroupedIndices = (Vec<(usize, usize)>, Vec<(usize, Vec<usize>)>);

// ───────────────────────────── pure core ─────────────────────────────────

/// The canonical fingerprint serialization: `prefix + "\n".join(rows)` with
/// one `entry_us|exit_us|direction|pnl` row per trade, GIVEN order. An
/// empty trade list serializes the bare prefix.
pub fn serialize(
    prefix: &str,
    entry_us: &[i64],
    exit_us: &[i64],
    directions: &[String],
    pnl_paisa: &[i64],
) -> String {
    let mut out = String::from(prefix);
    for i in 0..entry_us.len() {
        if i > 0 {
            out.push('\n');
        }
        out.push_str(&format!(
            "{}|{}|{}|{}",
            entry_us[i], exit_us[i], directions[i], pnl_paisa[i]
        ));
    }
    out
}

/// `_jaccard` over already-extracted entry sets: two EMPTY sets are defined
/// identical (`1.0`); else ONE f64 division `|a ∩ b| / |a ∪ b|` (exact-int
/// set sizes < 2^53, so the Rust division is CPython's correctly-rounded
/// int true-division bit-for-bit).
pub fn jaccard(a: &BTreeSet<i64>, b: &BTreeSet<i64>) -> f64 {
    if a.is_empty() && b.is_empty() {
        return 1.0;
    }
    let inter = a.intersection(b).count();
    let union = a.union(b).count();
    (inter as f64) / (union as f64)
}

/// The `[0-9a-f]{64}` fullmatch law (lowercase-only, exactly 64, whole
/// string — a trailing newline refuses).
fn hex64_ok(s: &str) -> bool {
    s.len() == 64 && s.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

/// One candidate's extracted grouping view.
pub struct CandView {
    /// The run_id as a Rust string (ordering key only — outputs are
    /// indices, so the ORIGINAL str objects survive on the Python surface).
    pub run_id: String,
    /// The tier-1 fingerprint.
    pub fingerprint: String,
    /// The tier-2 entry-timestamp signature.
    pub entries: BTreeSet<i64>,
    /// `mask_bits.bit_count()` — the simplicity rank.
    pub bit_count: i64,
}

/// Union-find root lookup with path compression over sorted positions.
fn find(parent: &mut [usize], x: usize) -> usize {
    let mut root = x;
    while parent[root] != root {
        root = parent[root];
    }
    let mut cur = x;
    while parent[cur] != root {
        let next = parent[cur];
        parent[cur] = root;
        cur = next;
    }
    root
}

/// Merge two components keeping the smaller root — sorted-position order IS
/// lexicographic run_id order, so "smaller position" ≡ the deleted law's
/// "lexicographically smaller root".
fn union(parent: &mut [usize], a: usize, b: usize) {
    let root_a = find(parent, a);
    let root_b = find(parent, b);
    let (lo, hi) = if root_b < root_a {
        (root_b, root_a)
    } else {
        (root_a, root_b)
    };
    parent[hi] = lo;
}

/// The two-tier grouping core over extracted views (`views[i]` corresponds
/// to the caller's candidate `i`). Returns, in ORIGINAL-index space:
///
/// * `family_pairs` — `(candidate, representative)` in sorted-run_id order
///   (the `family_by_run_id` dict insertion order);
/// * `members` — `(representative, members run_id ASC)` in family_id
///   (= representative run_id) ASC order (the `members_by_family` law).
pub fn group_core(views: &[CandView], threshold: f64) -> GroupedIndices {
    let n = views.len();
    // Sorted positions: pos → original index, ordered by run_id (byte order
    // == code-point order == Python `sorted`).
    let mut order: Vec<usize> = (0..n).collect();
    order.sort_by(|&a, &b| views[a].run_id.cmp(&views[b].run_id));
    let mut parent: Vec<usize> = (0..n).collect();

    // Tier 1 — identical fingerprints: union each run_id into the FIRST
    // (sorted-order) holder of its fingerprint.
    let mut anchor_by_fp: HashMap<&str, usize> = HashMap::new();
    for pos in 0..n {
        let fp = views[order[pos]].fingerprint.as_str();
        let anchor = *anchor_by_fp.entry(fp).or_insert(pos);
        if anchor != pos {
            union(&mut parent, anchor, pos);
        }
    }

    // Tier 2 — sorted (a, b) pairs; already-connected pairs skip the set
    // arithmetic; the boundary is `>=` INCLUSIVE.
    for i in 0..n {
        for j in (i + 1)..n {
            if find(&mut parent, i) == find(&mut parent, j) {
                continue;
            }
            if jaccard(&views[order[i]].entries, &views[order[j]].entries) >= threshold {
                union(&mut parent, i, j);
            }
        }
    }

    // Members per root, in first-encounter (= ascending-position) order —
    // the union law keeps the minimum position as root, so a root's key
    // order equals its own position order.
    let mut roots_in_order: Vec<usize> = Vec::new();
    let mut members_by_root: HashMap<usize, Vec<usize>> = HashMap::new();
    for pos in 0..n {
        let root = find(&mut parent, pos);
        let entry = members_by_root.entry(root).or_default();
        if entry.is_empty() {
            roots_in_order.push(root);
        }
        entry.push(pos);
    }

    // Representative = min by (bit_count, run_id) — position order encodes
    // the run_id tiebreak.
    let mut rep_by_root: HashMap<usize, usize> = HashMap::new();
    for (&root, members) in &members_by_root {
        let rep = *members
            .iter()
            .min_by_key(|&&pos| (views[order[pos]].bit_count, pos))
            .expect("members never empty");
        rep_by_root.insert(root, rep);
    }

    let family_pairs: Vec<(usize, usize)> = (0..n)
        .map(|pos| {
            let root = find(&mut parent, pos);
            (order[pos], order[rep_by_root[&root]])
        })
        .collect();

    // `members_by_family` keys in family_id (representative run_id) ASC
    // order — sort roots by their representative's position.
    let mut roots_sorted = roots_in_order;
    roots_sorted.sort_by_key(|root| rep_by_root[root]);
    let members: Vec<(usize, Vec<usize>)> = roots_sorted
        .into_iter()
        .map(|root| {
            (
                order[rep_by_root[&root]],
                members_by_root[&root]
                    .iter()
                    .map(|&pos| order[pos])
                    .collect(),
            )
        })
        .collect();

    (family_pairs, members)
}

// ─────────────────────────── py-level laws ───────────────────────────────

/// The `TwinCandidate.__post_init__` validation law (raises the REAL
/// threaded `TwinDetectError`, or CPython `re`'s raw `TypeError` escapes).
pub fn validate_candidate(
    run_id: &Bound<'_, PyAny>,
    mask_bits: &Bound<'_, PyAny>,
    fingerprint: &Bound<'_, PyAny>,
    error_cls: &Bound<'_, PyAny>,
) -> PyResult<()> {
    if !run_id.is_truthy()? {
        return Err(domain_err(
            error_cls,
            "TwinCandidate.run_id must be non-empty".to_string(),
        ));
    }
    if mask_bits.lt(0)? {
        let msg = format!(
            "TwinCandidate.mask_bits must be >= 0, got {} (run_id {})",
            mask_bits.str()?.to_cow()?,
            capture_repr(run_id),
        );
        return Err(domain_err(error_cls, msg));
    }
    // The `_FINGERPRINT_RE.fullmatch` law incl. CPython `re`'s own raw
    // TypeError escapes on non-string input (both frozen VERBATIM).
    if !fingerprint.is_instance_of::<PyString>() {
        if fingerprint.is_instance_of::<PyBytes>()
            || fingerprint.is_instance_of::<PyByteArray>()
            || fingerprint.is_instance_of::<PyMemoryView>()
        {
            return Err(PyTypeError::new_err(
                "cannot use a string pattern on a bytes-like object",
            ));
        }
        return Err(PyTypeError::new_err(format!(
            "expected string or bytes-like object, got '{}'",
            fingerprint.get_type().name()?.to_cow()?
        )));
    }
    // A lone-surrogate str fails UTF-8 extraction — it can never fullmatch
    // `[0-9a-f]{64}`, so it takes the SAME refusal arm (repr escapes
    // surrogates to ASCII, so the message is faithful).
    let matched = match fingerprint.extract::<String>() {
        Ok(s) => hex64_ok(&s),
        Err(_) => false,
    };
    if !matched {
        let msg = format!(
            "TwinCandidate.fingerprint must be a 64-char lowercase hex digest, got {} (run_id {})",
            capture_repr(fingerprint),
            capture_repr(run_id),
        );
        return Err(domain_err(error_cls, msg));
    }
    Ok(())
}

/// Extract one Python set/frozenset of epoch-µs ints.
fn extract_entries(entries: &Bound<'_, PyAny>) -> PyResult<BTreeSet<i64>> {
    let mut out = BTreeSet::new();
    for item in entries.try_iter()? {
        out.insert(item?.extract::<i64>()?);
    }
    Ok(out)
}

/// `_jaccard` over the raw Python sets.
pub fn jaccard_py(a: &Bound<'_, PyAny>, b: &Bound<'_, PyAny>) -> PyResult<f64> {
    Ok(jaccard(&extract_entries(a)?, &extract_entries(b)?))
}

/// `group_twins`' full law over the caller's candidate list: threshold
/// envelope → duplicate-run_id refusal → extraction → [`group_core`].
pub fn group(
    py: Python<'_>,
    candidates: &Bound<'_, PyAny>,
    threshold: &Bound<'_, PyAny>,
    error_cls: &Bound<'_, PyAny>,
) -> PyResult<GroupedIndices> {
    // `0.0 < t <= 1.0` — the chained compare on the RAW object (bool==int
    // puns compare exactly as Python's; NaN fails the first leg; a
    // non-numeric raises the same TypeError the Python compare raised).
    let zero = 0.0f64.into_pyobject(py)?;
    let one = 1.0f64.into_pyobject(py)?;
    let in_envelope = zero.lt(threshold)? && threshold.le(&one)?;
    if !in_envelope {
        let msg = format!(
            "group_twins: jaccard_threshold must be in (0.0, 1.0], got {}",
            capture_repr(threshold)
        );
        return Err(domain_err(error_cls, msg));
    }
    let thr: f64 = threshold.extract()?;

    // Duplicate-run_id refusal at the FIRST duplicate, in iteration order.
    let mut cands: Vec<Bound<'_, PyAny>> = Vec::new();
    let mut run_ids: Vec<String> = Vec::new();
    let mut seen: HashMap<String, ()> = HashMap::new();
    for cand in candidates.try_iter()? {
        let cand = cand?;
        let run_id_obj = cand.getattr("run_id")?;
        let run_id: String = run_id_obj.extract()?;
        if seen.insert(run_id.clone(), ()).is_some() {
            let msg = format!(
                "group_twins: duplicate run_id {} in the candidate pool",
                capture_repr(&run_id_obj)
            );
            return Err(domain_err(error_cls, msg));
        }
        run_ids.push(run_id);
        cands.push(cand);
    }

    let mut views: Vec<CandView> = Vec::with_capacity(cands.len());
    for (i, cand) in cands.iter().enumerate() {
        views.push(CandView {
            run_id: run_ids[i].clone(),
            fingerprint: cand.getattr("fingerprint")?.extract()?,
            entries: extract_entries(&cand.getattr("entry_ts_us")?)?,
            bit_count: cand
                .getattr("mask_bits")?
                .call_method0("bit_count")?
                .extract()?,
        });
    }
    Ok(group_core(&views, thr))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn set(vals: &[i64]) -> BTreeSet<i64> {
        vals.iter().copied().collect()
    }

    #[test]
    fn serialize_byte_law() {
        assert_eq!(serialize("bxtwin1|", &[], &[], &[], &[]), "bxtwin1|");
        let rows = serialize(
            "bxtwin1|",
            &[-1, 1704167160000000],
            &[1, 1704167460000000],
            &["BUY".to_string(), "SELL".to_string()],
            &[-(1_i64 << 62), 0],
        );
        assert_eq!(
            rows,
            "bxtwin1|-1|1|BUY|-4611686018427387904\n1704167160000000|1704167460000000|SELL|0"
        );
    }

    #[test]
    fn jaccard_division_parity() {
        // The frozen exact-f64 family (CPython int true-division parity).
        assert_eq!(jaccard(&set(&[]), &set(&[])), 1.0);
        assert_eq!(jaccard(&set(&[]), &set(&[1])), 0.0);
        assert_eq!(jaccard(&set(&[1, 2]), &set(&[2, 3])), 1.0 / 3.0);
        let nine_of_ten = jaccard(
            &(0..10).collect::<BTreeSet<i64>>(),
            &(0..9).collect::<BTreeSet<i64>>(),
        );
        assert_eq!(nine_of_ten, 0.9); // 9/10 == the 0.9 literal double EXACTLY
        let mut b: BTreeSet<i64> = (0..9).collect();
        b.insert(99);
        let nine_of_eleven = jaccard(&(0..10).collect::<BTreeSet<i64>>(), &b);
        assert_eq!(nine_of_eleven, 9.0 / 11.0);
        assert!(nine_of_eleven < 0.9);
    }

    #[test]
    fn hex64_grammar() {
        let good = "a".repeat(64);
        assert!(hex64_ok(&good));
        assert!(!hex64_ok(&good.to_uppercase()));
        assert!(!hex64_ok(&good[..63]));
        assert!(!hex64_ok(&format!("{}a", good)));
        assert!(!hex64_ok(&format!("{}\n", &good[..63])));
        assert!(!hex64_ok(&"g".repeat(64)));
        assert!(!hex64_ok(""));
    }

    fn view(run_id: &str, fp: &str, entries: &[i64], bits: i64) -> CandView {
        CandView {
            run_id: run_id.to_string(),
            fingerprint: fp.to_string(),
            entries: set(entries),
            bit_count: bits,
        }
    }

    #[test]
    fn group_core_two_tiers_and_orders() {
        // Mixed pool mirroring the frozen corpus shape: r-01~r-02 merge at
        // the inclusive 0.9 boundary (9-of-10 SUBSET), r-03~r-04 share a
        // fingerprint with disjoint entries (tier 1), r-05 is a singleton.
        let ten: Vec<i64> = (0..10).collect();
        let nine: Vec<i64> = (0..9).collect();
        let views = vec![
            view("r-01", "f1", &ten, 3),
            view("r-02", "f2", &nine, 1),
            view("r-03", "f3", &[500, 501], 1),
            view("r-04", "f3", &[900], 2),
            view("r-05", "f5", &[700], 1),
        ];
        let (pairs, members) = group_core(&views, 0.9);
        // Representatives: r-02 (1 bit) for {r-01, r-02}; r-03 (1 bit,
        // run_id ASC beats r-04's 2 bits) for {r-03, r-04}; r-05 itself.
        assert_eq!(pairs, vec![(0, 1), (1, 1), (2, 2), (3, 2), (4, 4)]);
        assert_eq!(
            members,
            vec![(1, vec![0, 1]), (2, vec![2, 3]), (4, vec![4])]
        );
    }

    #[test]
    fn group_core_transitive_chain_is_one_family() {
        // A~B and B~C at 29/32 ≥ 0.9 while A~C alone is 28/32 = 0.875 —
        // union-find transitivity makes ONE family.
        let a: Vec<i64> = (1..31).collect();
        let b: Vec<i64> = (2..32).collect();
        let c: Vec<i64> = (3..33).collect();
        let views = vec![
            view("c-a", "s1", &a, 3),
            view("c-b", "s2", &b, 2),
            view("c-c", "s3", &c, 1),
        ];
        let (pairs, members) = group_core(&views, 0.9);
        assert_eq!(pairs, vec![(0, 2), (1, 2), (2, 2)]);
        assert_eq!(members, vec![(2, vec![0, 1, 2])]);
    }

    #[test]
    fn group_core_input_order_independent() {
        let ten: Vec<i64> = (0..10).collect();
        let nine: Vec<i64> = (0..9).collect();
        let forward = vec![
            view("x1", "f1", &ten, 1),
            view("x2", "f2", &nine, 2),
            view("y1", "f3", &[7], 1),
        ];
        let reversed = vec![
            view("y1", "f3", &[7], 1),
            view("x2", "f2", &nine, 2),
            view("x1", "f1", &ten, 1),
        ];
        let (fp, fm) = group_core(&forward, 0.9);
        let (rp, rm) = group_core(&reversed, 0.9);
        // Map indices back to run_ids for comparison.
        let name = |views: &[CandView], i: usize| views[i].run_id.clone();
        let fp_named: Vec<(String, String)> = fp
            .iter()
            .map(|&(a, b)| (name(&forward, a), name(&forward, b)))
            .collect();
        let rp_named: Vec<(String, String)> = rp
            .iter()
            .map(|&(a, b)| (name(&reversed, a), name(&reversed, b)))
            .collect();
        assert_eq!(fp_named, rp_named);
        let fm_named: Vec<(String, Vec<String>)> = fm
            .iter()
            .map(|(r, ms)| {
                (
                    name(&forward, *r),
                    ms.iter().map(|&m| name(&forward, m)).collect(),
                )
            })
            .collect();
        let rm_named: Vec<(String, Vec<String>)> = rm
            .iter()
            .map(|(r, ms)| {
                (
                    name(&reversed, *r),
                    ms.iter().map(|&m| name(&reversed, m)).collect(),
                )
            })
            .collect();
        assert_eq!(fm_named, rm_named);
    }
}
