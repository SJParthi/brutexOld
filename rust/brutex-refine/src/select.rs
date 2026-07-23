//! Bounded per-leaderboard top-K survivor selection.
//!
//! `DEC-RUST-STAGE3-REFINE-001` — the selection math of the retired Python
//! `brutex/engine/stage3_select.py` bounded heap
//! (DEC-SWEEP-STREAMING-RANK-002), streamed row-by-row so the Python caller
//! retains ONLY the kept rows (O(boards · top_k) memory — the bounded-memory
//! law preserved, pinned by the Python bench). Laws:
//!
//! * **Total order** — Sharpe DESC, ties `run_id` ASC, deeper ties insertion
//!   order (`seq`) ASC. `seq` is unique per pushed row, so the order is
//!   STRICT and eviction is deterministic: among equal-lowest-Sharpe entries
//!   the LARGEST `run_id` is evicted first.
//! * **Signed zero** — `-0.0` and `0.0` Sharpe compare EQUAL (IEEE ==, the
//!   CPython comparison the retired `_HeapEntry` used), falling through to
//!   the `run_id` tie-break.
//! * **String order** — board keys and `run_id` compare as Rust `String`s
//!   (UTF-8 byte order), which equals CPython's code-point `str` order for
//!   every Unicode string — the two languages agree by construction.
//! * **Domain** — Sharpe must be FINITE (`ResultRow` rejects non-finite
//!   metrics upstream); a NaN/±inf reaching `push` is a loud error, never a
//!   silently mis-ordered heap.
//! * **Emission** — boards in sorted key order; per board the kept entries
//!   re-sorted `(run_id, seq)` ASC then STABLE Sharpe DESC — byte-identical
//!   to the retired two-pass discipline (mirrors `ResultsStore.top_k`).

use std::cmp::Ordering;
use std::collections::{BTreeMap, BinaryHeap};

/// One kept candidate (the Python side maps `seq` back to its row object).
#[derive(Debug, Clone)]
struct Entry {
    sharpe: f64,
    run_id: String,
    seq: u64,
}

impl Entry {
    /// Total-order comparison where `Greater` means "ranks strictly WORSE".
    ///
    /// The max-heap root is therefore always the WORST kept entry — the one
    /// a strictly-better candidate evicts (the retired `_HeapEntry.__lt__`
    /// with the orientation flipped for `BinaryHeap`'s max-heap).
    fn worse_cmp(&self, other: &Self) -> Ordering {
        // Finite-only domain (enforced at push): partial_cmp never None.
        match self
            .sharpe
            .partial_cmp(&other.sharpe)
            .unwrap_or(Ordering::Equal)
        {
            Ordering::Less => Ordering::Greater, // smaller Sharpe is worse
            Ordering::Greater => Ordering::Less,
            Ordering::Equal => match self.run_id.cmp(&other.run_id) {
                Ordering::Greater => Ordering::Greater, // larger run_id is worse
                Ordering::Less => Ordering::Less,
                Ordering::Equal => self.seq.cmp(&other.seq), // later insertion is worse
            },
        }
    }
}

impl PartialEq for Entry {
    fn eq(&self, other: &Self) -> bool {
        self.worse_cmp(other) == Ordering::Equal
    }
}
impl Eq for Entry {}
impl PartialOrd for Entry {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for Entry {
    fn cmp(&self, other: &Self) -> Ordering {
        self.worse_cmp(other)
    }
}

/// The outcome of one `push`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PushOutcome {
    /// Whether the pushed row is (currently) kept.
    pub kept: bool,
    /// The `seq` of a previously-kept row this push evicted, if any.
    pub evicted_seq: Option<u64>,
}

/// Streaming bounded top-K selection over `(instrument, tf, mode)` boards.
#[derive(Debug, Clone)]
pub struct SelectCore {
    top_k: usize,
    next_seq: u64,
    boards: BTreeMap<(String, String, String), BinaryHeap<Entry>>,
    finished: bool,
}

impl SelectCore {
    /// Build a selector keeping each board's top-`top_k` (`top_k ≥ 1`).
    pub fn new(top_k: usize) -> Result<Self, String> {
        if top_k < 1 {
            return Err(format!("stage3 top_k must be >= 1, got {top_k}"));
        }
        Ok(Self {
            top_k,
            next_seq: 0,
            boards: BTreeMap::new(),
            finished: false,
        })
    }

    /// Stream one row; `seq` is assigned in push order (0, 1, 2, …).
    pub fn push(
        &mut self,
        instrument: &str,
        tf: &str,
        mode: &str,
        sharpe: f64,
        run_id: &str,
    ) -> Result<PushOutcome, String> {
        if self.finished {
            return Err("push() after finish()".to_owned());
        }
        if !sharpe.is_finite() {
            return Err(format!(
                "sharpe_ratio must be finite, got {sharpe} (ResultRow guarantees this upstream)"
            ));
        }
        let seq = self.next_seq;
        self.next_seq += 1;
        let entry = Entry {
            sharpe,
            run_id: run_id.to_owned(),
            seq,
        };
        let board = self
            .boards
            .entry((instrument.to_owned(), tf.to_owned(), mode.to_owned()))
            .or_default();
        if board.len() < self.top_k {
            board.push(entry);
            return Ok(PushOutcome {
                kept: true,
                evicted_seq: None,
            });
        }
        // The root is the WORST kept entry; evict it iff it ranks strictly
        // worse than the candidate (the retired `board[0] < entry` law).
        let root_is_worse = board
            .peek()
            .map(|root| root.worse_cmp(&entry) == Ordering::Greater)
            .unwrap_or(false);
        if root_is_worse {
            let evicted = board.pop().map(|e| e.seq);
            board.push(entry);
            Ok(PushOutcome {
                kept: true,
                evicted_seq: evicted,
            })
        } else {
            Ok(PushOutcome {
                kept: false,
                evicted_seq: None,
            })
        }
    }

    /// Emit every board in sorted key order with its kept `seq`s in the
    /// final leaderboard order. Consumes the boards; a second `finish` (or a
    /// later `push`) is a protocol error.
    #[allow(clippy::type_complexity)]
    pub fn finish(&mut self) -> Result<Vec<((String, String, String), Vec<u64>)>, String> {
        if self.finished {
            return Err("finish() called twice".to_owned());
        }
        self.finished = true;
        let boards = std::mem::take(&mut self.boards);
        let mut out = Vec::with_capacity(boards.len());
        for (key, heap) in boards {
            // BTreeMap iterates in sorted key order (== CPython sorted()).
            let mut entries = heap.into_vec();
            // The retired two-pass discipline: (run_id, seq) ASC, then a
            // STABLE Sharpe-DESC pass (sort_by is stable in Rust).
            entries.sort_by(|a, b| a.run_id.cmp(&b.run_id).then(a.seq.cmp(&b.seq)));
            entries.sort_by(|a, b| b.sharpe.partial_cmp(&a.sharpe).unwrap_or(Ordering::Equal));
            out.push((key, entries.into_iter().map(|e| e.seq).collect()));
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rid(n: u64) -> String {
        format!("{n:064x}")
    }

    #[test]
    fn top_k_below_one_rejected() {
        assert_eq!(
            SelectCore::new(0).unwrap_err(),
            "stage3 top_k must be >= 1, got 0"
        );
    }

    #[test]
    fn keeps_all_when_fewer_than_k() {
        let mut core = SelectCore::new(10).unwrap();
        for i in 0..3u64 {
            let out = core.push("NIFTY", "5m", "A", i as f64, &rid(i)).unwrap();
            assert!(out.kept);
            assert_eq!(out.evicted_seq, None);
        }
        let boards = core.finish().unwrap();
        assert_eq!(boards.len(), 1);
        // Sharpe desc: seq 2 (2.0), 1 (1.0), 0 (0.0).
        assert_eq!(boards[0].1, vec![2, 1, 0]);
    }

    #[test]
    fn evicts_the_worst_deterministically() {
        // Equal-lowest Sharpe cluster: the LARGEST run_id is evicted first.
        let mut core = SelectCore::new(2).unwrap();
        core.push("N", "5m", "A", 1.0, &rid(5)).unwrap(); // seq 0
        core.push("N", "5m", "A", 1.0, &rid(2)).unwrap(); // seq 1
        let out = core.push("N", "5m", "A", 1.0, &rid(1)).unwrap(); // seq 2
        assert!(out.kept);
        assert_eq!(out.evicted_seq, Some(0)); // rid(5) — largest run_id
        let boards = core.finish().unwrap();
        assert_eq!(boards[0].1, vec![2, 1]); // rid(1), rid(2) — run_id asc
    }

    #[test]
    fn worse_candidate_is_dropped_not_kept() {
        let mut core = SelectCore::new(1).unwrap();
        core.push("N", "5m", "A", 2.0, &rid(1)).unwrap();
        let out = core.push("N", "5m", "A", 1.0, &rid(2)).unwrap();
        assert!(!out.kept);
        assert_eq!(out.evicted_seq, None);
        assert_eq!(core.finish().unwrap()[0].1, vec![0]);
    }

    #[test]
    fn signed_zero_compares_equal_falls_to_run_id() {
        let mut core = SelectCore::new(1).unwrap();
        core.push("N", "5m", "A", -0.0, &rid(9)).unwrap(); // seq 0
        let out = core.push("N", "5m", "A", 0.0, &rid(1)).unwrap(); // seq 1
        // Equal Sharpe ⇒ run_id decides: rid(1) preferred, rid(9) evicted.
        assert!(out.kept);
        assert_eq!(out.evicted_seq, Some(0));
    }

    #[test]
    fn full_tie_earlier_insertion_wins() {
        let mut core = SelectCore::new(1).unwrap();
        core.push("N", "5m", "A", 1.0, &rid(1)).unwrap(); // seq 0
        let out = core.push("N", "5m", "A", 1.0, &rid(1)).unwrap(); // seq 1
        assert!(!out.kept, "the later full-tie twin must NOT evict");
        assert_eq!(core.finish().unwrap()[0].1, vec![0]);
    }

    #[test]
    fn boards_emitted_in_sorted_key_order() {
        let mut core = SelectCore::new(4).unwrap();
        core.push("NIFTY", "5m", "A", 1.0, &rid(1)).unwrap();
        core.push("BANKNIFTY", "5m", "A", 1.0, &rid(2)).unwrap();
        core.push("NIFTY", "15m", "A", 1.0, &rid(3)).unwrap();
        let keys: Vec<_> = core.finish().unwrap().into_iter().map(|(k, _)| k).collect();
        assert_eq!(
            keys,
            vec![
                ("BANKNIFTY".into(), "5m".into(), "A".into()),
                ("NIFTY".into(), "15m".into(), "A".into()),
                ("NIFTY".into(), "5m".into(), "A".into()),
            ]
        );
    }

    #[test]
    fn non_finite_sharpe_rejected() {
        let mut core = SelectCore::new(1).unwrap();
        assert!(core.push("N", "5m", "A", f64::NAN, &rid(1)).is_err());
        assert!(core.push("N", "5m", "A", f64::INFINITY, &rid(1)).is_err());
    }

    #[test]
    fn finish_twice_and_push_after_finish_rejected() {
        let mut core = SelectCore::new(1).unwrap();
        core.push("N", "5m", "A", 1.0, &rid(1)).unwrap();
        core.finish().unwrap();
        assert!(core.finish().is_err());
        assert!(core.push("N", "5m", "A", 1.0, &rid(2)).is_err());
    }

    /// Differential oracle: the retired Python accumulate-then-two-pass-sort,
    /// transcribed verbatim, must agree byte-for-byte on adversarial input.
    type BoardKey = (String, String, String);

    fn reference(
        rows: &[(&str, &str, &str, f64, String)],
        top_k: usize,
    ) -> Vec<(BoardKey, Vec<u64>)> {
        let mut grouped: BTreeMap<BoardKey, Vec<(f64, String, u64)>> = BTreeMap::new();
        for (seq, (instr, tf, mode, sharpe, run_id)) in rows.iter().enumerate() {
            grouped
                .entry(((*instr).into(), (*tf).into(), (*mode).into()))
                .or_default()
                .push((*sharpe, run_id.clone(), seq as u64));
        }
        let mut out = Vec::new();
        for (key, mut board) in grouped {
            board.sort_by(|a, b| a.1.cmp(&b.1).then(a.2.cmp(&b.2)));
            board.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(Ordering::Equal));
            board.truncate(top_k);
            out.push((key, board.into_iter().map(|e| e.2).collect()));
        }
        out
    }

    #[test]
    fn differential_vs_reference_on_adversarial_streams() {
        // Deterministic pseudo-random stream: heavy Sharpe ties, duplicate
        // run_ids, three instruments, extreme finite values.
        let mut rng = crate::rng::Xoshiro256pp::new(1234);
        let sharpe_pool = [1e308, -1e308, 5e-324, 0.0, -0.0, 1.5, 1.5, 2.0];
        let instruments = ["NIFTY", "BANKNIFTY", "SENSEX"];
        for top_k in [1usize, 3, 7, 64] {
            let rows: Vec<(&str, &str, &str, f64, String)> = (0..200)
                .map(|_| {
                    let instr = instruments[(rng.next_u64() % 3) as usize];
                    let sharpe = sharpe_pool[(rng.next_u64() % 8) as usize];
                    let run_id = rid(rng.next_u64() % 40);
                    (instr, "5m", "A", sharpe, run_id)
                })
                .collect();
            let mut core = SelectCore::new(top_k).unwrap();
            let mut kept: std::collections::BTreeMap<u64, ()> = Default::default();
            for (seq, (instr, tf, mode, sharpe, run_id)) in rows.iter().enumerate() {
                let out = core.push(instr, tf, mode, *sharpe, run_id).unwrap();
                if out.kept {
                    kept.insert(seq as u64, ());
                }
                if let Some(evicted) = out.evicted_seq {
                    assert!(kept.remove(&evicted).is_some(), "evicted an unkept seq");
                }
            }
            let got = core.finish().unwrap();
            let want = reference(&rows, top_k);
            assert_eq!(got, want, "top_k={top_k}");
            // Every emitted seq was tracked kept (the Python-side memory law).
            for (_, seqs) in &got {
                for s in seqs {
                    assert!(kept.contains_key(s));
                }
            }
        }
    }
}
