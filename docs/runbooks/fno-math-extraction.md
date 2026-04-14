# Runbook — Extract `fno-math` from tickvault

> **Target audience:** BruteX operator (you)
> **Time required:** ~1-2 hours the first time; ~10 minutes on re-runs
> **Prerequisites:** both `tickvault` and `bruteX` repos checked out locally, Rust toolchain installed, Python 3.12 venv active for BruteX, `maturin` installed
>
> This runbook implements the extraction procedure for
> `§7A Shared Core Library Contract` in `docs/REQUIREMENTS.md`. Run
> once when you're ready to build Phase P3 (strategy engine) or when
> tickvault's math code has drifted and BruteX CI flags it.

---

## The goal in one sentence

> **You write Greeks / indicators / strategies in EXACTLY ONE place — `rust/fno-math/src/` inside the BruteX repo — and both tickvault and BruteX automatically use the same code.**

After this runbook:

| Action | Where you do it | Who sees the change |
|---|---|---|
| Edit a Greek formula | `rust/fno-math/src/greeks/*.rs` | Tickvault + BruteX |
| Add a new indicator | `rust/fno-math/src/indicators/*.rs` | Tickvault + BruteX |
| Write a new strategy | `rust/fno-math/src/strategies/*.rs` | Tickvault + BruteX |
| Fix a bug in rolling stats | `rust/fno-math/src/math/rolling.rs` | Tickvault + BruteX |

**You will NEVER edit math/indicators/strategies in two places again after this extraction is done.** That's the whole point.

---

## Prerequisites checklist

Before starting, verify all of these:

```bash
# Both repos present
[ -d "$HOME/dev/tickvault" ] && [ -d "$HOME/dev/bruteX" ] && echo "✅" || echo "❌ clone both first"

# Both repos on clean working trees
(cd "$HOME/dev/tickvault" && git status --porcelain | wc -l)  # must be 0
(cd "$HOME/dev/bruteX"   && git status --porcelain | wc -l)  # must be 0

# Rust toolchain
cargo --version          # ≥ 1.93
rustc --version          # ≥ 1.93

# Python venv for BruteX
python --version         # ≥ 3.12
which maturin            # installed?  `pip install maturin` if not
```

Adjust paths in the commands below to match your local layout.

---

## Step 1 — Run the extraction script (5 minutes)

```bash
cd ~/dev/bruteX
./scripts/extract-fno-math.sh ~/dev/tickvault
```

The script:

1. Captures tickvault's current commit SHA + branch
2. `rsync`s `crates/trading/src/greeks/**` → `rust/fno-math/src/greeks/**`
3. `rsync`s `crates/trading/src/indicator/**` → `rust/fno-math/src/indicators/**`
4. Rewrites internal module paths (`crate::trading::greeks` → `crate::greeks`, etc.)
5. Writes a provenance file `rust/fno-math/EXTRACTION.md` with the tickvault SHA

**Expected output:** `✅ Extraction complete.` plus a numbered next-steps list.

---

## Step 2 — Verify `fno-math` compiles (5 minutes)

```bash
cd ~/dev/bruteX/rust/fno-math
cargo check
```

**Expected:** zero errors.

**If you see errors:** the most common issue is unresolved external paths (tickvault's greeks/ may reference types from elsewhere in `crates/trading/`, like config structs or error types). You have two options:

- **Inline the needed type** into `rust/fno-math/src/` if it's small
- **Add a minimal shim module** under `rust/fno-math/src/compat.rs` that re-declares what's needed

Either way, commit the fix on a feature branch, don't hack it into the extraction script.

Run `cargo test` in `rust/fno-math/` once the build is green. Tickvault's unit tests should pass unchanged.

---

## Step 3 — Build the PyO3 wrapper wheel (10 minutes)

```bash
cd ~/dev/bruteX/rust/fno-math-py

# Populate src/lib.rs — uncomment the #[pymodule] body and add one
# #[pyfunction] wrapper per public function in fno-math. Each wrapper
# is ~5 lines and pure forwarding; no logic. See the file's docstring
# for the boilerplate template.

# Build the wheel and install into BruteX's venv
maturin develop --release
```

**Expected:** wheel built + installed. Verify with:

```bash
python -c "from fno_math_py.greeks import delta; print(delta(100, 100, 0.25, 0.07, 0.2, True))"
# Should print a float around 0.5
```

**If maturin fails to find the Rust toolchain:** run `rustup target add` for your platform (usually `aarch64-apple-darwin` on M-series Macs) and try again.

---

## Step 4 — Replace the BruteX `brutex/math/` placeholder (5 minutes)

Currently `brutex/math/__init__.py` raises `NotImplementedError`. After extraction, replace it with one-line re-exports from `fno_math_py`:

```python
# brutex/math/__init__.py (new version)
"""BruteX math layer — thin re-exports from the shared fno-math Rust crate.

Every function below is a direct re-export of its fno-math-py equivalent.
No logic lives here. See rust/fno-math/src/ for the actual implementations.
"""

from fno_math_py.greeks import delta, gamma, theta, vega, rho, implied_volatility
from fno_math_py.indicators import sma, ema, wma, hma, dema, tema, rsi, macd, bb, atr, adx, supertrend, vwap, obi
from fno_math_py.strategies import Strategy, Signal, SignalKind  # base types
from fno_math_py.math import rolling_mean, rolling_std, normal_cdf, normal_pdf

__all__ = [
    "Signal", "SignalKind", "Strategy",
    "adx", "atr", "bb", "dema", "delta", "ema", "gamma",
    "hma", "implied_volatility", "macd", "normal_cdf", "normal_pdf",
    "obi", "rho", "rolling_mean", "rolling_std", "rsi", "sma",
    "supertrend", "tema", "theta", "vega", "vwap", "wma",
]
```

Update `brutex/math/greeks.py`, `brutex/math/indicators.py`, and `brutex/math/strategies.py` similarly — each file just re-imports from the corresponding `fno_math_py.*` submodule.

---

## Step 5 — Update tickvault to depend on `fno-math` (15 minutes)

```bash
cd ~/dev/tickvault

# Edit Cargo.toml, add:
#   fno-math = { path = "../bruteX/rust/fno-math" }
# to the workspace dependencies, then in crates/trading/Cargo.toml:
#   fno-math = { workspace = true }

# Delete the now-duplicate in-tree files
rm -r crates/trading/src/greeks/ crates/trading/src/indicator/

# Update crates/trading/src/lib.rs to re-export from fno-math
# e.g.:
#   pub use fno_math::greeks;
#   pub use fno_math::indicators as indicator;

# Fix every consumer in tickvault that imported from crate::greeks / crate::indicator
# to use crate::greeks (re-export) or fno_math::greeks (direct).
# cargo check will show you the errors one at a time.

cargo check --workspace
cargo test --workspace
```

**Expected:** both build green; every test still passes. Tickvault's behaviour is unchanged because it's running the exact same Rust code, just compiled from a different path.

---

## Step 6 — Run parity tests in BruteX (10 minutes)

```bash
cd ~/dev/bruteX
pytest tests/golden/test_tickvault_math_parity.py -v
```

**Expected:** every reference vector (Greeks at ATM / ITM / OTM, indicators on canonical NIFTY/BANKNIFTY series, strategy outputs on known fixtures) matches within `NFR-MATH-001` tolerance (1e-9 abs / 1e-6 rel).

**If any vector fails:** the PyO3 wrapper or the re-exports are wrong. Compare the function signature in `fno-math-py/src/lib.rs` with the Rust signature in `fno-math/src/greeks/`. The most common issue is parameter order mismatch.

---

## Step 7 — Commit on BOTH repositories (5 minutes)

Cross-repo coordination is required by the versioning rule in `§7A.5`.

```bash
# BruteX first — the shared crate's owner
cd ~/dev/bruteX
git checkout -b claude/extract-fno-math-$(date +%Y%m%d)
git add rust/ brutex/math/ scripts/ docs/runbooks/ tests/
git commit -m "feat(fno-math): extract shared math crate from tickvault@${TICKVAULT_SHA:0:12}"
git push -u origin HEAD

# Tickvault second — the consumer
cd ~/dev/tickvault
git checkout -b extract-fno-math
git add Cargo.toml crates/trading/
git rm -r crates/trading/src/greeks/ crates/trading/src/indicator/
git commit -m "refactor(trading): delegate greeks+indicator to external fno-math crate"
git push -u origin HEAD
```

Open both PRs. Both must merge together (cross-repo CI enforces the pairing — see `§7A.5` Breaking changes rule).

---

## Re-runs (when tickvault updates its math code)

When tickvault CI shows changes to `crates/trading/src/greeks/` or `indicator/`, BruteX CI's `scripts/check-fno-math-drift.sh` will fail with a clear message. To resolve:

```bash
cd ~/dev/tickvault && git pull
cd ~/dev/bruteX
./scripts/extract-fno-math.sh ~/dev/tickvault
# Review the diff
git diff rust/fno-math/
# Bump the version in rust/fno-math/Cargo.toml
# Commit and push
```

**Total time for a re-run: ~10 minutes.** Most of it is spent waiting for `cargo check` and reviewing the diff.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `cargo check` fails with unresolved imports | Tickvault file references a type outside `trading/src/greeks/` | Inline the type or add a compat shim |
| `maturin develop` fails with "no matching crate" | Workspace resolution confusion | Run from `rust/fno-math-py/` directly, not workspace root |
| Parity tests fail with NaN | Numpy array dtype mismatch at the PyO3 boundary | Ensure arrays are `float64` / `f64` on both sides |
| Parity tests fail with 1e-5 off | Likely calling `math.erf` in Python somewhere — FORBIDDEN per FR-MATH-001 | Remove any Python math import under `brutex/math/` |
| Drift check always fails | `EXTRACTION.md` has a stale SHA | Re-run `extract-fno-math.sh` with a clean tickvault pull |

---

## Security notes

- The extraction script only reads from tickvault; it never writes back. Safe to run against any tickvault checkout.
- Tickvault is a private repo; the `check-fno-math-drift.sh` script clones it in CI, which requires a read-only deploy key or a GitHub App token. Configure before enabling the drift check in CI.
- The PyO3 wheel is installed into BruteX's local venv only. It is NOT published to PyPI.

---

_Last updated: 2026-04-14 (initial runbook version)._
