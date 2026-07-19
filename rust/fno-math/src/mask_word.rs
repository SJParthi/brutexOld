//! Width-generic mask word — the ONE shared u64/u128 SUPERSET-test core.
//!
//! Hoisted VERBATIM from `trade_sim.rs` (where DEC-TRADE-SIM-U128-KERNEL-001
//! introduced it module-internal) into a first-class shared module so every
//! width-generic kernel in this crate — the Stage-2 trade-sim walk AND the
//! Stage-2 enum/count kernels — genericizes over the SAME trait (CLAUDE.md
//! §11 COMMON: one source of truth, no parallel copy). Pure relocation: the
//! trait + both impls are byte-identical to the pre-hoist `trade_sim.rs`
//! definitions; only the visibility widened (`trait` → `pub(crate) trait`)
//! so sibling modules can import it. The existing `trade_sim` test suite
//! passing UNMODIFIED is the frozen-behaviour gate for the move.

/// Width-generic mask word for the sweep-sim core (DEC-TRADE-SIM-U128-KERNEL-001).
///
/// The ONLY mask-touching operation in the whole trade walk is the per-bar
/// SUPERSET containment test; every other instruction is width-independent
/// i64 price arithmetic in `trade_sim::Walker`. Making that single operation
/// generic lets the u64 production kernel and the u128 headroom kernel share
/// ONE core (CLAUDE.md §11 COMMON — no parallel copy of the walk),
/// monomorphized by the compiler to the exact pre-generic code for `W = u64`
/// (the existing `#[cfg(test)]` suite passing UNMODIFIED is the
/// frozen-behaviour gate). For `W = u128` the compiler's two-limb lowering of
/// the 128-bit `&`/`==` is the "two-limb" evaluation — no hand-rolled limb
/// pairs needed.
pub(crate) trait MaskWord: Copy + Send + Sync {
    /// True iff `bar` is a SUPERSET of `mask` — `(bar & mask) == mask`.
    fn is_superset(bar: Self, mask: Self) -> bool;
}

impl MaskWord for u64 {
    #[inline(always)]
    fn is_superset(bar: u64, mask: u64) -> bool {
        (bar & mask) == mask
    }
}

impl MaskWord for u128 {
    #[inline(always)]
    fn is_superset(bar: u128, mask: u128) -> bool {
        (bar & mask) == mask
    }
}
