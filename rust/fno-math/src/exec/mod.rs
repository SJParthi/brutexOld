//! Options/futures execution integer cores — DEC-EXEC-RUST-CORE-001 (MIGRATION.md #2215).
//!
//! Slice 7 / P8 of the Rust conversion program: everything from the moment
//! prices are **integer paisa** — the worst-case fill law
//! (DEC-FILL-WORST-CASE-ONLY-001 adverse bar-extreme anchors), BOTH charge stacks
//! (options + futures), and the post-I/O materialize verdict tails — as pure
//! deterministic integer functions, bit-for-bit twins of the Python oracles
//! in `brutex.costs.calculator` / `brutex.costs.futures_costs` /
//! `brutex.options.executor` / `brutex.futures.executor`. The Python bodies
//! stay the permanent oracles (deletion is a distinct later decision,
//! DEC-EXEC-PY-BODY-DELETE-001); dispatch is default-OFF behind
//! `BRUTEX_EXEC_RUST` (the flip is its own later decision,
//! DEC-EXEC-RUST-FLIP-001).
//!
//! ## ⚠ TWO money laws live in this repo — do not confuse them
//!
//! * **`exec::money` (THIS tree): ADVERSE CEILING** on exact integers — the
//!   permanent worst-case levy-rounding law (`_round_levy_paisa` /
//!   `_round_levy_rupee`, DEC-FILL-WORST-CASE-ONLY-001 — supersedes the
//!   pre-#1825 half-away-from-zero levy rounding). Every fractional levy
//!   rounds toward +∞ (never flatters the trade).
//! * **`trading_core::indicators::round_half_even`: half-to-EVEN on f64** —
//!   the INDICATOR level-snapping law (fib/pivot paisa levels). NEVER use it
//!   for money; never use this tree for indicator levels.
//!
//! ## Boundary honesty (what deliberately STAYS in Python)
//!
//! Executor orchestration, the lazy date gates (lot size / cost regime) with
//! their narrow-except + monkeypatch semantics, leg selection (both f64
//! hazards: `round_to_atm` float floor-division, `round(x, 6)` tie-break),
//! all regime/lot tables + `rate_for_date` + `UnverifiedCostRegimeError`
//! (rates cross this FFI as ARGUMENTS — a rate change is a Python-data-only
//! edit, no wheel rebuild), all I/O, all logging/audit, all dataclass
//! construction (whose `__post_init__` re-validates every Rust result at the
//! boundary), and the sanctioned R45 independent Decimal `reference_sim`
//! oracle. Dates, Decimals, datetimes and callbacks NEVER cross this FFI.
//!
//! Cold path (Golden Rule #10): per-trade loops over FINALIST trades only —
//! the value is one-language consistency + shared-crate reuse, NOT speed; no
//! speedup is claimed anywhere in this tree.

pub mod costs_futures;
pub mod costs_options;
pub mod exec_futures;
pub mod exec_options;
pub mod ffi;
pub mod fills;
pub mod money;

pub use costs_futures::futures_charge_stack;
pub use costs_options::{ChargeStack, option_charge_stack};
pub use exec_futures::{BarVerdictFutures, FuturesOutcome, futures_materialize_core};
pub use exec_options::{BarVerdict, options_materialize_core};
pub use fills::{TICK_PAISA, apply_futures_fill, apply_slippage};
pub use money::{floor_rate_scale, gst_paisa, round_to_paisa, round_to_rupee};
