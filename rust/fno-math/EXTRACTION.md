# fno-math — extraction provenance

> **Tickvault source commit:** `aad528a7e7f58df0bda69e349a48e31442115fd7`
> **Tickvault branch:** `main`
> **Extracted at:** 2026-04-14 UTC
> **Extracted via:** manual execution (first-ever extraction — see history)
> **Target:** `rust/fno-math/src/`

This file records which commit of tickvault was the source of the
files in this crate. The drift-check script
(`scripts/check-fno-math-drift.sh`) uses this record to detect when
tickvault has moved ahead of the extracted copy and a re-extraction
is needed.

---

## What was extracted

From `tickvault/crates/trading/src/`:

| Source path | → Destination | Files | Lines |
|---|---|---|---|
| `greeks/` | `rust/fno-math/src/greeks/` | 8 `.rs` files (aggregator, black_scholes, buildup, calibration, historical_rates, inline_computer, mod, pcr) | ~6,025 |
| `indicator/` | `rust/fno-math/src/indicators/` | 5 `.rs` files (engine, mod, obi, tests, types) | ~3,219 |
| `strategy/` | `rust/fno-math/src/strategies/` | 7 `.rs` files (config, config_tests, evaluator, hot_reload, mod, tests, types) | ~6,376 |

From `tickvault/crates/common/src/`:

| Source path | → Destination | Files | Lines |
|---|---|---|---|
| entire crate | `rust/fno-math/src/compat/` | 12 `.rs` files (config, constants, error, instrument_registry, instrument_types, mod (was lib.rs), order_types, sanitize, segment, tick_types, trading_calendar, types) | ~several thousand |

**Total:** ~32 `.rs` files across 4 subdirectories. The exact line
count varies; verify with `find rust/fno-math/src -name '*.rs' | xargs wc -l`.

## Module path rewrites applied

The extraction script ran the following `sed` substitutions on every
copied file:

- `crate::indicator::` → `crate::indicators::` (tickvault naming was singular; fno-math uses plural)
- `crate::strategy::` → `crate::strategies::` (same reason)
- `use tickvault_common::` → `use crate::compat::`
- `tickvault_common::` → `crate::compat::`
- Inside `compat/` only: `use crate::` → `use super::` (because
  tickvault-common files assumed they were at the crate root; now
  they're a submodule)
- Inside `compat/` only: `crate::types::` → `super::types::` and similar
  for every sibling module

## What's NOT in this extraction (and why)

The following items were intentionally left out of this first pass:

### `bar_indicators/` — not yet in tickvault

Tickvault's `indicator/` directory currently contains only **OBI
(Order Book Imbalance)**, which is a **tick-level** indicator —
computed on every incoming Dhan tick from the order book depth.
BruteX's backtest is bar-level (1-minute candles and up), so OBI
does not apply directly.

The **bar-level indicator catalog** that BruteX needs (SMA, EMA, WMA,
HMA, DEMA, TEMA, RSI, Stochastic, MACD, BB, ATR, ADX, Supertrend,
VWAP, Anchored VWAP, Keltner, Donchian, OBV, CMF, MFI, Ichimoku,
PSAR, PivotPoints, CPR, OpeningRange, RealizedVolatility — see
`FR-IND-001`) does **not yet exist in tickvault**. It will be added
to `fno-math` in a subsequent commit and automatically consumed by
tickvault's next release.

This is tracked as **`MG-043`** in `REQUIREMENTS.md §18`.

### `brutex/Cargo.lock` — don't vendor, regenerate

No `Cargo.lock` copied. The operator runs `cargo generate-lockfile`
during Runbook Step 2 (`cargo check`) to produce a fresh lock for the
BruteX checkout.

### `tickvault/crates/trading/src/risk/` and `oms/` — live-only, not shared

These contain the order management system and live risk engine —
tightly coupled to the Dhan WebSocket and the live OMS state machine.
They are not "pure compute" and do not belong in a shared math crate.
BruteX will have its own backtest-specific risk engine and execution
simulator under `brutex.risk` and `brutex.exec`.

## Known issues — operator must resolve during `cargo check`

This first-pass extraction may not compile cleanly out of the box.
Expected issues and fixes:

| Issue | Fix |
|---|---|
| `jaeckel` crate commented out in `Cargo.toml` | Uncomment, verify version on crates.io, or replace with an inline Abramowitz-Stegun erf implementation |
| Some `tickvault_common::` imports may have been missed by `sed` | Run `grep -rn 'tickvault_common\|crate::trading::' rust/fno-math/src` and fix manually |
| `compat/config.rs` references `crate::trading_calendar::TradingCalendar` which was rewritten to `super::trading_calendar` — verify it resolves | Should just work since `trading_calendar.rs` is in the same `compat/` dir |
| `strategy/hot_reload.rs` uses `notify` crate which is not in `fno-math`'s `Cargo.toml` | Add `notify = "6"` as an optional dependency OR gate `hot_reload` behind a `live` feature flag |
| `indicator/tests.rs` may have test-only helpers that import from `tickvault-common::tick_types::ParsedTick` with specific live-only fields | Minor adjustments — run `cargo test` and fix one-off issues |
| Clippy `allow(missing_docs)` in `lib.rs` is a temporary compromise | Either add docstrings to every public item OR remove the allow and accept the warnings |

**Estimated time to green `cargo check`:** 30-60 minutes of operator time,
depending on how many of the above surface as compile errors.

## Next steps (operator — on your Mac)

1. **Verify the extraction compiles:**
   ```bash
   cd ~/dev/bruteX/rust/fno-math
   cargo check
   ```
   Fix any residual import errors per the table above.

2. **Run tickvault's own unit tests against the extracted copy:**
   ```bash
   cargo test --release
   ```
   These should pass bit-for-bit since the code is identical to
   tickvault's.

3. **Wire up `fno-math-py` PyO3 wrapper:**
   ```bash
   cd ../fno-math-py
   # Populate src/lib.rs with #[pyfunction] wrappers for every
   # public fn in fno-math (see the file's template comment).
   maturin develop --release
   ```

4. **Replace `brutex/math/__init__.py` placeholder** with one-line
   re-exports from `fno_math_py` per Runbook Step 4.

5. **Run parity tests:**
   ```bash
   cd ~/dev/bruteX
   pytest tests/golden/test_tickvault_math_parity.py
   ```

6. **Open companion PR on tickvault** (optional for now — only when
   you're ready to make tickvault use the new `fno-math` crate and
   delete its duplicate files). See Runbook Step 5.

---

## Re-extraction (when tickvault updates its math code)

```bash
cd ~/dev/tickvault && git pull
cd ~/dev/bruteX
scripts/extract-fno-math.sh ~/dev/tickvault
# Review the diff, commit, bump rust/fno-math/Cargo.toml version
```

On re-extraction, **this file is regenerated** with the new commit SHA.
Do not hand-edit it.

## Tickvault is NOT affected

**Important:** the extraction only READS from tickvault. Zero writes.
Tickvault's GitHub repo remains at commit `aad528a` untouched.
Tickvault's Docker stack and live trading continue to run their own
code from `crates/trading/src/` exactly as before. The only time
tickvault is touched is when the operator opens a separate PR to
migrate tickvault to depend on `fno-math` — that's a deliberate
choice with its own timeline and can be delayed indefinitely.
