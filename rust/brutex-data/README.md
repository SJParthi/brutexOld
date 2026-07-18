# brutex-data — BruteX data pure-law kernels (ADR-0020 slice T10)

DEC-DATA-RUST-PURELAW-001 (MIGRATION.md #1940). PR-1 hosts the STRUCTURAL
half of the right-anchored F&O contract-symbol grammar
(`brutex/data/fno_symbol.py::parse_fno_symbol`) as ONE pure function:

```text
parse_fno_symbol_coords(symbol: &str)
    -> Option<(type_code, exchange_end, underlying_start, underlying_end,
               expiry_year, expiry_month, expiry_day,
               strike_start, strike_end)>
```

Integers only cross the FFI — token-role SPANS into the original Python
string + the resolved expiry y/m/d + a type code (0=FUT · 1=CE · 2=PE), or
`None` for a structural rejection. Python materializes everything (string
slices, `date`, the `Decimal` strike via the injected `parse_strike_token`
oracle helper, `FnoSymbolParts`) — the DEC-FEED-RUST-CORE-001 discipline.

Stdlib-pure: NO polars / chrono / regex. The `%d%b%y` expiry grammar is a
hand-rolled mirror of CPython `_strptime`'s compiled regex (ordered day
alternatives with full-remainder backtracking, the 12-entry English month
table, the POSIX `%y` pivot, the Gregorian leap rule, the [2019, 2059]
window) — empirically anchored on the probed truth table transcribed into
this crate's unit tests AND the Python golden corpus.

## OPTIONAL-kernel classification

This crate is OPTIONAL-class (registered in `OPTIONAL_KERNELS`,
`tests/unit/test_run_builds_rust_kernels.py`):

* consumed ONLY through the import-guarded facade
  `brutex/data/fno_symbol_rust.py` behind the default-OFF
  `BRUTEX_DATA_RUST` flag (`importlib.import_module("brutex_data")` — an
  absent/stale install latches the flag OFF with one loud WARN and the
  Python oracle serves, byte-identical);
* deliberately NOT a turnkey build prerequisite (`make run-raw` /
  `bootstrap.sh` never build it);
* built ONLY by its own non-required CI lane,
  `.github/workflows/data-rust-parity.yml`;
* hence NO build.rs source digest / EXPECTED_BUILD.json / deny.toml (the
  REQUIRED-class machinery of fno-math / trading-core / brutex-feed) — the
  facade's SYMBOL handshake is the stale-wheel guard, and the facade's
  LOCALE guard is the lockstep guard this LC_TIME-coupled law needs.

## Build

```bash
cargo fmt --check
cargo clippy --all-targets --locked -- -D warnings
cargo test --release --locked
# the wheel (into the active venv):
pip install ./rust/brutex-data     # maturin mixed layout, Cargo.lock enforced
```

Honesty (ADR-0020 banner, verbatim): **expected payoff ≈ 0 — conversion is
for uniformity, not speed.** The seam ADDS one FFI hop per parse on
read/display walks; no speedup is claimed; the flag ships DEFAULT-OFF and a
default flip needs its own DEC + a fresh green lane run.
