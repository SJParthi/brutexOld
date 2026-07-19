# brutex-core — the BruteX root-leaf Rust conversion crate (slice T35)

DEC-CONFIG-RUST-LEAVES-001 (MIGRATION.md #1943). Milestone 1 hosts the
MONEY laws of `brutex/money.py` (DEC-MONEY-2DP-001) as exact
decimal-STRING arithmetic, byte-locked to the CPython `Decimal` oracle:

| pyfunction            | Python law it twins                                        |
| --------------------- | ---------------------------------------------------------- |
| `round_money_str`     | `Decimal.quantize(Decimal("0.01"), ROUND_HALF_UP)`         |
| `rupees_to_paisa_str` | `int(round_money(r) * 100)` (exact ×100 digit shift)       |
| `paisa_to_rupees_str` | `round_money(Decimal(p) / 100)` (exact ÷100 digit shift)   |
| `format_inr_str`      | `f"₹{paisa_to_rupees(p):,.2f}"` (western 3-grouping)       |

**Honesty banner (ADR-0020, mandatory verbatim): expected payoff ≈ 0 —
conversion is for uniformity, not speed.** Each op is sub-microsecond in
Python; the FFI hop is a small SLOWDOWN, acceptable because the seam ships
DEFAULT-OFF behind `BRUTEX_CORE_RUST_MONEY` (facade:
`brutex/money_rust.py`; a default flip needs its own DEC).

## Strings-only FFI

Only strings cross, both directions: the facade pre-normalizes each
`Decimal` to `format(d, "f")` and each paisa `int` to `str(int)`, gates
eligibility Python-side (non-finite / out-of-bounds-exponent / huge-paisa /
context-hazard inputs route to the `Decimal` oracle without touching the
crate), and rebuilds `Decimal(...)` / `int(...)` from the returned strings.
A parse failure maps to `ValueError`, a panic (caught at the boundary) to
`RuntimeError` — both facade degrade members that serve the oracle.

## Twin-impl coordination (run-identity critical)

`round_money` snaps EVERY stored OHLC value at the lake-write boundary, so
the half-away-from-zero 2dp law is stored-bytes critical. It has TWO
coordinated non-Python impls: this crate's `src/money.rs` AND the
polars-columnar `_snap_price_expr` contract in
`brutex/data/parquet_writer.py` (consumed by the `rust/brutex-lake`
seams). Any change to the law must move BOTH plus the Python oracle; the
cross-crate consistency pin in `src/money.rs` locks this crate to the SAME
literal corpus `tests/golden/test_writer_vectorized_snap_parity.py` pins.
The law is HALF_UP (away from zero), deliberately NOT `trading-core`'s
`round_half_even` — negative-pinned in the cargo suite.

## Zero third-party numeric deps

The digit arithmetic is hand-rolled (~100 lines: parse, HALF_UP round with
carry, ×100/÷100 shifts, grouping) rather than pulling a `bigdecimal`-class
dependency — for four functions on 2dp money strings, auditable in-repo
code is lower-risk than an external rounding table (and keeps the crate's
graph pyo3-only, pinned by the committed `Cargo.lock`).

## Gates

* `cargo fmt --check` · `cargo clippy --all-targets --locked -- -D warnings`
  · `cargo test --release --locked` (rounding boundaries, carry
  propagation, grouping, negative zero, the snap-corpus cross-crate pin,
  the half-even negative pin, parse-error totality).
* Wheel build: `maturin build --release -m rust/brutex-core/Cargo.toml`
  (or `maturin develop`), then the flag-ON parity legs in
  `tests/unit/test_money_rust.py` + the hypothesis differential run by
  `.github/workflows/core-rust-parity.yml` (non-required lane — the
  facade is default-OFF; a red run here blocks a future default flip,
  never an unrelated merge).
