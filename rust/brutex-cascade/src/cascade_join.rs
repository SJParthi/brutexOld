//! The ONE cascade-report → display-join law (DEC-RUST-CASCADE-RESULTS-001).
//!
//! Token-for-token port of the deleted `brutex/engine/cascade_join.py`
//! (@ 415e863, captured into the frozen parity corpus BEFORE conversion):
//! structural fail-closed validation, per-item skip+count tolerance, the
//! duplicate-candidate every-copy exclusion, survivor-rank stamping and the
//! capped defect-note law (`_WARN_DETAIL_CAP = 5`).
//!
//! The law operates over a PROJECTED payload ([`RootView`] and friends) the
//! PyO3 shell builds in one traversal of the Python object (exact-type /
//! isinstance classification + CPython `repr()` capture where a note or an
//! error message interpolates `!r`) — so the pure law is `cargo test`-able
//! while the type semantics stay CPython's own. Divergence envelope
//! (documented, ultra-hostile inputs only): an int beyond ±i128 and a str
//! carrying a lone surrogate classify [`Leaf::Other`] (Python's unbounded
//! int / surrogate-tolerant str would have accepted them); and a within-±i128
//! SUM can wrap in the release build — the group-stats fallback totals
//! (`build_join`'s `n_input`/`n_survived` fold) sum per-group i128 counts
//! without checked arithmetic, so a hand-crafted report carrying n_input ≈
//! i128::MAX in two or more groups would display a wrapped integer (never a
//! panic; Python's unbounded int would have summed exactly — the round-4
//! review NIT of DEC-RUST-CASCADE-RESULTS-001). All three shapes are
//! unreachable from any real cascade report.

use std::collections::BTreeMap;

/// The capped defect-note depth — `_WARN_DETAIL_CAP`.
pub const WARN_DETAIL_CAP: usize = 5;

/// One classified Python leaf value.
#[derive(Clone, Debug, PartialEq)]
pub enum Leaf {
    /// Python `None` — and an ABSENT dict key (`dict.get` default), which
    /// the law never distinguishes from `None`.
    None_,
    Bool(bool),
    /// An int instance (bool excluded); `exact` mirrors `type(v) is int`.
    Int {
        v: i128,
        exact: bool,
    },
    Float(f64),
    Str(String),
    /// Any other type (list/dict leaves where a scalar was expected, or an
    /// unrepresentable int/str — see the module divergence envelope).
    Other,
}

/// A leaf plus its captured CPython `repr()` (only consulted where the law
/// interpolates `!r`).
#[derive(Clone, Debug)]
pub struct LeafR {
    pub leaf: Leaf,
    pub repr: String,
}

impl LeafR {
    pub fn new(leaf: Leaf, repr: impl Into<String>) -> Self {
        Self {
            leaf,
            repr: repr.into(),
        }
    }
}

/// One projected verdict entry.
// One-shot per-report projection nodes — variant size is irrelevant
// (never stored in bulk); boxing would only obscure the law.
#[allow(clippy::large_enum_variant)]
#[derive(Clone, Debug)]
pub enum VerdictNode {
    NotDict,
    V {
        candidate_id: LeafR,
        passed: Leaf,
        rejected_at: Leaf,
        detail: Leaf,
        composite_score: Leaf,
        provisional: Leaf,
        cpcv_pass_rate: Leaf,
    },
}

/// One projected group's report object.
// One-shot per-report projection nodes — variant size is irrelevant
// (never stored in bulk); boxing would only obscure the law.
#[allow(clippy::large_enum_variant)]
#[derive(Clone, Debug)]
pub enum ReportNode {
    NotDict,
    R {
        /// `None` when `report["verdicts"]` is not a list.
        verdicts: Option<Vec<VerdictNode>>,
        n_input: Leaf,
        n_survived: Leaf,
        pool_pbo: Leaf,
        dsr_n_source: Leaf,
        dsr_n_trials: Leaf,
        dsr_trial_variance: Leaf,
        /// `None` when `report["survivor_ids"]` is not a list.
        survivor_ids: Option<Vec<LeafR>>,
    },
}

/// One projected group entry.
// One-shot per-report projection nodes — variant size is irrelevant
// (never stored in bulk); boxing would only obscure the law.
#[allow(clippy::large_enum_variant)]
#[derive(Clone, Debug)]
pub enum GroupNode {
    NotDict,
    G {
        instrument: Leaf,
        tf: Leaf,
        report: ReportNode,
    },
}

/// The projected report root.
#[derive(Clone, Debug)]
pub struct RootView {
    /// `isinstance(payload, collections.abc.Mapping)`.
    pub is_mapping: bool,
    pub version: LeafR,
    pub sweep_id: Leaf,
    /// `None` when `payload["groups"]` is not a list.
    pub groups: Option<Vec<GroupNode>>,
    pub n_input: Leaf,
    pub n_survived: Leaf,
}

/// One flattened verdict view (`CascadeVerdictView` scalars).
#[derive(Clone, Debug, PartialEq)]
pub struct ViewOut {
    pub candidate_id: String,
    pub instrument: String,
    pub tf: String,
    pub passed: bool,
    pub rejected_at: Option<String>,
    pub detail: String,
    pub cascade_composite: Option<f64>,
    pub provisional: bool,
    pub cpcv_pass_rate: Option<f64>,
    pub survivor_rank: Option<i64>,
}

/// One group's headline stats (`CascadeGroupStats` scalars).
#[derive(Clone, Debug, PartialEq)]
pub struct GroupOut {
    pub instrument: String,
    pub tf: String,
    pub n_input: i128,
    pub n_survived: i128,
    pub pool_pbo: Option<f64>,
    pub dsr_n_source: String,
    pub dsr_n_trials: Option<i128>,
    pub dsr_trial_variance: Option<f64>,
}

/// The flattened, validated join (`CascadeJoin` scalars + the WARN inputs).
#[derive(Clone, Debug)]
pub struct JoinOut {
    pub sweep_id: String,
    pub version: i128,
    pub n_input: i128,
    pub n_survived: i128,
    /// Kept views in Python-dict insertion order (post-deletion order —
    /// the shim rebuilds the `by_candidate` mapping from this).
    pub views: Vec<ViewOut>,
    pub ambiguous_ids: Vec<String>,
    pub groups: Vec<GroupOut>,
    pub skipped_groups: i64,
    pub skipped_verdicts: i64,
    pub nulled_values: i64,
    pub rank_inconsistencies: i64,
    /// The capped defect notes (≤ [`WARN_DETAIL_CAP`], occurrence order).
    pub notes: Vec<String>,
    /// Whether the ONE `cascade_join.partial` WARN fires.
    pub warn: bool,
}

/// `_count_or_none`: a GENUINE non-negative int (bool pun rejected).
fn count_or_none(leaf: &Leaf) -> Option<i128> {
    match leaf {
        Leaf::Int { v, exact: true } if *v >= 0 => Some(*v),
        _ => None,
    }
}

/// `_finite_float`: a finite float (int accepted, bool rejected).
fn finite_float(leaf: &Leaf) -> Option<f64> {
    let value = match leaf {
        Leaf::Int { v, .. } => *v as f64,
        Leaf::Float(f) => *f,
        _ => return None,
    };
    value.is_finite().then_some(value)
}

/// `is not None` over the leaf model (an absent key already collapsed).
fn is_not_none(leaf: &Leaf) -> bool {
    !matches!(leaf, Leaf::None_)
}

struct Tally {
    skipped_groups: i64,
    skipped_verdicts: i64,
    nulled_values: i64,
    rank_inconsistencies: i64,
    details: Vec<String>,
}

impl Tally {
    fn new() -> Self {
        Self {
            skipped_groups: 0,
            skipped_verdicts: 0,
            nulled_values: 0,
            rank_inconsistencies: 0,
            details: Vec::new(),
        }
    }

    fn note(&mut self, detail: String) {
        if self.details.len() < WARN_DETAIL_CAP {
            self.details.push(detail);
        }
    }
}

/// An order-preserving `views` map mirroring Python dict semantics
/// (insertion order, in-place update keeps position, delete removes).
struct OrderMap {
    order: Vec<String>,
    map: BTreeMap<String, ViewOut>,
}

impl OrderMap {
    fn new() -> Self {
        Self {
            order: Vec::new(),
            map: BTreeMap::new(),
        }
    }

    fn contains(&self, key: &str) -> bool {
        self.map.contains_key(key)
    }

    fn insert(&mut self, key: String, value: ViewOut) {
        if !self.map.contains_key(&key) {
            self.order.push(key.clone());
        }
        self.map.insert(key, value);
    }

    fn get(&self, key: &str) -> Option<&ViewOut> {
        self.map.get(key)
    }

    fn remove(&mut self, key: &str) {
        if self.map.remove(key).is_some() {
            self.order.retain(|k| k != key);
        }
    }

    fn iter(&self) -> impl Iterator<Item = &ViewOut> {
        self.order.iter().filter_map(|k| self.map.get(k))
    }

    fn into_views(self) -> Vec<ViewOut> {
        let OrderMap { order, mut map } = self;
        order.into_iter().filter_map(|k| map.remove(&k)).collect()
    }
}

/// `_verdict_view` — validate + project one raw verdict entry.
fn verdict_view(
    entry: &VerdictNode,
    instrument: &str,
    tf: &str,
    tally: &mut Tally,
) -> Option<ViewOut> {
    let VerdictNode::V {
        candidate_id,
        passed,
        rejected_at,
        detail,
        composite_score,
        provisional,
        cpcv_pass_rate,
    } = entry
    else {
        tally.skipped_verdicts += 1;
        tally.note(format!("{instrument}/{tf}: verdict entry is not an object"));
        return None;
    };
    let cid = match &candidate_id.leaf {
        Leaf::Str(s) if !s.is_empty() => s.clone(),
        _ => {
            tally.skipped_verdicts += 1;
            tally.note(format!(
                "{instrument}/{tf}: verdict lacks a non-empty candidate_id"
            ));
            return None;
        }
    };
    let cid_repr = &candidate_id.repr;
    let passed = match passed {
        Leaf::Bool(b) => *b,
        _ => {
            tally.skipped_verdicts += 1;
            tally.note(format!(
                "{instrument}/{tf}: verdict {cid_repr} passed is not a bool"
            ));
            return None;
        }
    };
    if passed && is_not_none(rejected_at) {
        tally.skipped_verdicts += 1;
        tally.note(format!(
            "{instrument}/{tf}: passed verdict {cid_repr} carries rejected_at"
        ));
        return None;
    }
    let rejected = match rejected_at {
        Leaf::Str(s) => Some(s.clone()),
        _ => None,
    };
    if is_not_none(rejected_at) && rejected.is_none() {
        tally.nulled_values += 1;
    }
    let composite = finite_float(composite_score);
    if is_not_none(composite_score) && composite.is_none() {
        tally.nulled_values += 1;
    }
    let mut rate = finite_float(cpcv_pass_rate);
    if let Some(r) = rate
        && !(0.0..=1.0).contains(&r)
    {
        rate = None;
        tally.nulled_values += 1;
    } else if is_not_none(cpcv_pass_rate) && rate.is_none() {
        tally.nulled_values += 1;
    }
    Some(ViewOut {
        candidate_id: cid,
        instrument: instrument.to_string(),
        tf: tf.to_string(),
        passed,
        rejected_at: rejected,
        detail: match detail {
            Leaf::Str(s) => s.clone(),
            _ => String::new(),
        },
        cascade_composite: composite,
        provisional: matches!(provisional, Leaf::Bool(true)),
        cpcv_pass_rate: rate,
        survivor_rank: None,
    })
}

/// `_group_views` — one group's validated views with survivor ranks stamped.
fn group_views(
    verdicts: &[VerdictNode],
    survivor_ids: Option<&Vec<LeafR>>,
    instrument: &str,
    tf: &str,
    tally: &mut Tally,
    ambiguous: &mut Vec<String>,
) -> OrderMap {
    let mut views = OrderMap::new();
    for entry in verdicts {
        let Some(view) = verdict_view(entry, instrument, tf, tally) else {
            continue;
        };
        if views.contains(&view.candidate_id) {
            let cid = view.candidate_id.clone();
            if !ambiguous.contains(&cid) {
                ambiguous.push(cid.clone());
            }
            tally.note(format!(
                "{instrument}/{tf}: duplicate candidate_id {}",
                py_str_repr(&cid)
            ));
            continue;
        }
        views.insert(view.candidate_id.clone(), view);
    }

    let empty: Vec<LeafR> = Vec::new();
    let survivors = survivor_ids.unwrap_or(&empty);
    let mut ranked: Vec<String> = Vec::new();
    for (position, sid) in survivors.iter().enumerate() {
        let position = (position + 1) as i64;
        let view = match &sid.leaf {
            Leaf::Str(s) => views.get(s.as_str()),
            _ => None,
        };
        let Some(view) = view.filter(|v| v.passed) else {
            tally.rank_inconsistencies += 1;
            tally.note(format!(
                "{instrument}/{tf}: survivor_id {} has no passed verdict",
                sid.repr
            ));
            continue;
        };
        let mut updated = view.clone();
        updated.survivor_rank = Some(position);
        let cid = updated.candidate_id.clone();
        views.insert(cid.clone(), updated);
        if !ranked.contains(&cid) {
            ranked.push(cid);
        }
    }
    let unranked_passed: Vec<String> = views
        .iter()
        .filter(|v| v.passed && !ranked.contains(&v.candidate_id))
        .map(|v| v.candidate_id.clone())
        .collect();
    for cid in unranked_passed {
        tally.rank_inconsistencies += 1;
        tally.note(format!(
            "{instrument}/{tf}: passed verdict {} absent from survivor_ids",
            py_str_repr(&cid)
        ));
    }
    views
}

/// CPython `repr()` of a KNOWN-str value — used only for strings the law
/// itself validated (candidate ids); arbitrary-object reprs are captured by
/// the PyO3 shell instead. Mirrors CPython's quote choice + escape table for
/// printable content; non-printable/escape-worthy characters follow
/// `unicode_repr` (backslash escapes, `\xXX`/`\uXXXX`/`\UXXXXXXXX`).
pub fn py_str_repr(s: &str) -> String {
    let has_single = s.contains('\'');
    let has_double = s.contains('"');
    let quote = if has_single && !has_double { '"' } else { '\'' };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for ch in s.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c => {
                if is_py_printable(c) {
                    out.push(c);
                } else {
                    let code = c as u32;
                    use std::fmt::Write as _;
                    if code <= 0xff {
                        let _ = write!(out, "\\x{code:02x}");
                    } else if code <= 0xffff {
                        let _ = write!(out, "\\u{code:04x}");
                    } else {
                        let _ = write!(out, "\\U{code:08x}");
                    }
                }
            }
        }
    }
    out.push(quote);
    out
}

/// `str.isprintable`'s verdict for the repr escape decision — ASCII space
/// through tilde printable, C0/C1 controls + DEL escaped; non-ASCII above
/// U+00A0 is treated printable (the cascade id domain is 64-hex ASCII, so
/// only the ASCII arm is ever load-bearing; separators/unassigned beyond it
/// would diverge from CPython — the shell captures REAL reprs for
/// arbitrary-object interpolations, this helper only serves validated ids).
fn is_py_printable(c: char) -> bool {
    let code = c as u32;
    if code < 0x20 || (0x7f..=0xa0).contains(&code) {
        return false;
    }
    true
}

/// `build_cascade_join` — the full law over a projected payload.
///
/// Returns `Err(message)` for the STRUCTURAL refusals (the shim raises
/// `CascadeJoinError(message)`).
pub fn build_join(root: &RootView, report_version: i128) -> Result<JoinOut, String> {
    if !root.is_mapping {
        return Err("cascade report root is not a JSON object".to_string());
    }
    let version = match root.version.leaf {
        Leaf::Int { v, exact: true } => v,
        _ => {
            return Err(format!(
                "cascade report version {} is not a genuine integer",
                root.version.repr
            ));
        }
    };
    if version > report_version {
        return Err(format!(
            "cascade report version {version} is NEWER than the supported {report_version} \
             — unknown future semantics are never guessed"
        ));
    }
    let sweep_id = match &root.sweep_id {
        Leaf::Str(s) if !s.is_empty() => s.clone(),
        _ => {
            return Err("cascade report sweep_id is missing or not a non-empty string".to_string());
        }
    };
    let Some(groups_raw) = &root.groups else {
        return Err("cascade report groups is not a list".to_string());
    };

    let mut tally = Tally::new();
    let mut group_stats: Vec<GroupOut> = Vec::new();
    let mut views = OrderMap::new();
    let mut ambiguous: Vec<String> = Vec::new();

    for group in groups_raw {
        let GroupNode::G {
            instrument,
            tf,
            report,
        } = group
        else {
            tally.skipped_groups += 1;
            tally.note("group entry is not an object".to_string());
            continue;
        };
        let (instrument, tf) = match (instrument, tf, report) {
            (Leaf::Str(i), Leaf::Str(t), ReportNode::R { .. })
                if !i.is_empty() && !t.is_empty() =>
            {
                (i.clone(), t.clone())
            }
            _ => {
                tally.skipped_groups += 1;
                tally.note("group lacks str instrument/tf or an object report".to_string());
                continue;
            }
        };
        let ReportNode::R {
            verdicts,
            n_input,
            n_survived,
            pool_pbo,
            dsr_n_source,
            dsr_n_trials,
            dsr_trial_variance,
            survivor_ids,
        } = report
        else {
            unreachable!("matched above");
        };
        let n_input_v = count_or_none(n_input);
        let n_survived_v = count_or_none(n_survived);
        let (Some(verdicts_raw), Some(n_input_v), Some(n_survived_v)) =
            (verdicts.as_ref(), n_input_v, n_survived_v)
        else {
            tally.skipped_groups += 1;
            tally.note(format!(
                "{instrument}/{tf}: report lacks list verdicts / int headline counts"
            ));
            continue;
        };
        group_stats.push(GroupOut {
            instrument: instrument.clone(),
            tf: tf.clone(),
            n_input: n_input_v,
            n_survived: n_survived_v,
            pool_pbo: finite_float(pool_pbo),
            dsr_n_source: match dsr_n_source {
                Leaf::Str(s) => s.clone(),
                _ => String::new(),
            },
            dsr_n_trials: count_or_none(dsr_n_trials),
            dsr_trial_variance: finite_float(dsr_trial_variance),
        });
        let gviews = group_views(
            verdicts_raw,
            survivor_ids.as_ref(),
            &instrument,
            &tf,
            &mut tally,
            &mut ambiguous,
        );
        // Cross-group flatten: a duplicate excludes EVERY copy.
        for view in gviews.into_views() {
            let cid = view.candidate_id.clone();
            if ambiguous.contains(&cid) {
                continue;
            }
            if views.contains(&cid) {
                ambiguous.push(cid.clone());
                views.remove(&cid);
                tally.note(format!(
                    "duplicate candidate_id {} across groups",
                    py_str_repr(&cid)
                ));
                continue;
            }
            views.insert(cid, view);
        }
    }
    // A candidate flagged ambiguous by a LATER group's intra-group duplicate
    // may already sit in the flat index from an earlier group — sweep it out.
    for cid in &ambiguous {
        views.remove(cid);
    }

    let n_input_total =
        count_or_none(&root.n_input).unwrap_or_else(|| group_stats.iter().map(|g| g.n_input).sum());
    let n_survived_total = count_or_none(&root.n_survived)
        .unwrap_or_else(|| group_stats.iter().map(|g| g.n_survived).sum());

    let warn = tally.skipped_groups != 0
        || tally.skipped_verdicts != 0
        || tally.nulled_values != 0
        || tally.rank_inconsistencies != 0
        || !ambiguous.is_empty();

    Ok(JoinOut {
        sweep_id,
        version,
        n_input: n_input_total,
        n_survived: n_survived_total,
        views: views.into_views(),
        ambiguous_ids: ambiguous,
        groups: group_stats,
        skipped_groups: tally.skipped_groups,
        skipped_verdicts: tally.skipped_verdicts,
        nulled_values: tally.nulled_values,
        rank_inconsistencies: tally.rank_inconsistencies,
        notes: tally.details,
        warn,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn leaf_str(s: &str) -> Leaf {
        Leaf::Str(s.to_string())
    }

    fn v(cid: &str, passed: bool, rejected: Leaf) -> VerdictNode {
        VerdictNode::V {
            candidate_id: LeafR::new(leaf_str(cid), py_str_repr(cid)),
            passed: Leaf::Bool(passed),
            rejected_at: rejected,
            detail: leaf_str("d"),
            composite_score: Leaf::Float(1.5),
            provisional: Leaf::Bool(false),
            cpcv_pass_rate: Leaf::Float(0.75),
        }
    }

    fn group(
        instrument: &str,
        tf: &str,
        verdicts: Vec<VerdictNode>,
        survivors: &[&str],
    ) -> GroupNode {
        let n = verdicts.len() as i128;
        GroupNode::G {
            instrument: leaf_str(instrument),
            tf: leaf_str(tf),
            report: ReportNode::R {
                n_input: Leaf::Int { v: n, exact: true },
                n_survived: Leaf::Int {
                    v: survivors.len() as i128,
                    exact: true,
                },
                verdicts: Some(verdicts),
                pool_pbo: Leaf::Float(0.25),
                dsr_n_source: leaf_str("pool"),
                dsr_n_trials: Leaf::Int { v: 12, exact: true },
                dsr_trial_variance: Leaf::Float(0.5),
                survivor_ids: Some(
                    survivors
                        .iter()
                        .map(|s| LeafR::new(leaf_str(s), py_str_repr(s)))
                        .collect(),
                ),
            },
        }
    }

    fn root(groups: Vec<GroupNode>) -> RootView {
        RootView {
            is_mapping: true,
            version: LeafR::new(Leaf::Int { v: 1, exact: true }, "1"),
            sweep_id: leaf_str("sweep-x"),
            groups: Some(groups),
            n_input: Leaf::None_,
            n_survived: Leaf::None_,
        }
    }

    #[test]
    fn structural_refusals() {
        let mut r = root(vec![]);
        r.is_mapping = false;
        assert_eq!(
            build_join(&r, 1).unwrap_err(),
            "cascade report root is not a JSON object"
        );
        let mut r = root(vec![]);
        r.version = LeafR::new(Leaf::Bool(true), "True");
        assert_eq!(
            build_join(&r, 1).unwrap_err(),
            "cascade report version True is not a genuine integer"
        );
        let mut r = root(vec![]);
        r.version = LeafR::new(Leaf::Int { v: 99, exact: true }, "99");
        assert!(build_join(&r, 1).unwrap_err().contains("NEWER"));
        let mut r = root(vec![]);
        r.sweep_id = Leaf::None_;
        assert!(build_join(&r, 1).unwrap_err().contains("sweep_id"));
        let mut r = root(vec![]);
        r.groups = None;
        assert!(build_join(&r, 1).unwrap_err().contains("groups"));
    }

    #[test]
    fn clean_group_ranks_survivors() {
        let a1 = "a1".repeat(32);
        let a2 = "a2".repeat(32);
        let g = group(
            "NIFTY",
            "5minute",
            vec![v(&a1, true, Leaf::None_), v(&a2, false, leaf_str("dsr"))],
            &[&a1],
        );
        let out = build_join(&root(vec![g]), 1).expect("join");
        assert!(!out.warn);
        assert_eq!(out.views.len(), 2);
        assert_eq!(out.views[0].survivor_rank, Some(1));
        assert_eq!(out.views[1].survivor_rank, None);
        assert_eq!(out.n_input, 2);
        assert_eq!(out.n_survived, 1);
    }

    #[test]
    fn duplicate_across_groups_excludes_every_copy() {
        let a1 = "a1".repeat(32);
        let g1 = group("NIFTY", "1minute", vec![v(&a1, true, Leaf::None_)], &[&a1]);
        let g2 = group("SENSEX", "1minute", vec![v(&a1, false, leaf_str("x"))], &[]);
        let out = build_join(&root(vec![g1, g2]), 1).expect("join");
        assert!(out.views.is_empty());
        assert_eq!(out.ambiguous_ids, vec![a1.clone()]);
        assert!(out.warn);
        assert_eq!(
            out.notes[0],
            format!("duplicate candidate_id {} across groups", py_str_repr(&a1))
        );
    }

    #[test]
    fn passed_with_rejected_at_is_skipped() {
        let a1 = "a1".repeat(32);
        let g = group("NIFTY", "1minute", vec![v(&a1, true, leaf_str("dsr"))], &[]);
        let out = build_join(&root(vec![g]), 1).expect("join");
        assert_eq!(out.skipped_verdicts, 1);
        // The skipped survivor-less pass leaves no views; no rank noise.
        assert!(out.views.is_empty());
    }

    #[test]
    fn out_of_domain_rate_is_nulled_not_clamped() {
        let a1 = "a1".repeat(32);
        let mut verdict = v(&a1, true, Leaf::None_);
        if let VerdictNode::V { cpcv_pass_rate, .. } = &mut verdict {
            *cpcv_pass_rate = Leaf::Float(1.5);
        }
        let g = group("NIFTY", "1minute", vec![verdict], &[&a1]);
        let out = build_join(&root(vec![g]), 1).expect("join");
        assert_eq!(out.views[0].cpcv_pass_rate, None);
        assert_eq!(out.nulled_values, 1);
    }

    #[test]
    fn note_cap_holds_counts_exact() {
        let junk: Vec<VerdictNode> = (0..9)
            .map(|i| {
                let cid = format!("junk-{i}");
                VerdictNode::V {
                    candidate_id: LeafR::new(leaf_str(&cid), py_str_repr(&cid)),
                    passed: leaf_str("no"),
                    rejected_at: Leaf::None_,
                    detail: Leaf::None_,
                    composite_score: Leaf::None_,
                    provisional: Leaf::None_,
                    cpcv_pass_rate: Leaf::None_,
                }
            })
            .collect();
        let g = group("NIFTY", "1minute", junk, &[]);
        let out = build_join(&root(vec![g]), 1).expect("join");
        assert_eq!(out.skipped_verdicts, 9);
        assert_eq!(out.notes.len(), WARN_DETAIL_CAP);
    }

    #[test]
    fn py_str_repr_matches_cpython_for_the_id_domain() {
        assert_eq!(py_str_repr("abc"), "'abc'");
        assert_eq!(py_str_repr("a'b"), "\"a'b\"");
        assert_eq!(py_str_repr("a'b\"c"), "'a\\'b\"c'");
        assert_eq!(py_str_repr("a\nb\tc\\d"), "'a\\nb\\tc\\\\d'");
        assert_eq!(py_str_repr("\u{7f}"), "'\\x7f'");
    }
}
