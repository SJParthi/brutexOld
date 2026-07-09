# RUST area map — rust/fno-math + rust/trading-core (+ wheel/facade/CI wiring)

All paths repo-relative to /home/user/bruteX. Line anchors verified 2026-07-08 on branch `claude/data-edge-guards`.

---

## 1. Crate inventory & toolchain

| | fno-math | trading-core |
|---|---|---|
| Path | `rust/fno-math` | `rust/trading-core` |
| Version | **0.3.0** (`Cargo.toml:11`) | **0.3.1** (`Cargo.toml:16`) |
| Edition / rust-version | 2024 / **1.93.1** (both crates) | same |
| crate-type | `["cdylib", "rlib"]` (`Cargo.toml:30`) | same (`Cargo.toml:31`) |
| pyo3 | `0.29`, features `extension-module`, **`abi3-py312`**, UNCONDITIONAL (`Cargo.toml:37`; lock 0.29.0) | `0.29` same features but **optional**, behind feature `python` (`Cargo.toml:59`) |
| Features | none | `default=[]`, `python=["dep:pyo3"]`, `serde=["dep:serde"]` (`Cargo.toml:33-44`) |
| Other deps | blake3 1.5, rayon 1.10, serde/serde_json/toml/chrono/chrono-tz/rkyv (compat only) | **jaeckel `=0.2.0` EXACT pin** (Greeks IV, frozen bit-identity, `Cargo.toml:56`) |
| Release profile | `opt-level=3, lto="thin", codegen-units=1, panic="abort", strip="symbols", overflow-checks=true` (`Cargo.toml:87-93`) | identical (`Cargo.toml:87-93`) |
| Workspace | NONE — two independent crate roots, each with its own `Cargo.lock`, `deny.toml`, `fuzz/` crate | same |
| build.rs | `include!("../source_digest_core.rs")` → bakes `BRUTEX_SOURCE_DIGEST` env → `SOURCE_DIGEST` const (`lib.rs:86` / trading-core `lib.rs:64`) | same |
| publish | `publish = false` (Proprietary; excluded from cargo-deny SPDX check) | same |

**CRITICAL profile consequence**: `panic = "abort"` + `overflow-checks = true` in release ⇒ ANY panic inside a `#[pyfunction]`/`#[pymethods]` **SIGABRTs the whole host interpreter** (exit 134, uncatchable from Python). Both crates therefore validate EVERY FFI input at the boundary with pure `Result<_, String>` cores mapped to `PyValueError` by thin wrappers, keeping hot loops unguarded but PROVABLY overflow-free.

**The pure-core/PyErr split is load-bearing twice**: (a) `cargo test` builds under the `extension-module` feature which links NO libpython — any test touching `PyErr`/`Python::detach` fails to link, so ALL testable logic lives in `_impl`/`_checked` functions returning `Result<_, String>` (e.g. `fno-math/src/python.rs:51,113,352`, `trade_sim.rs:166,185,769`, `trading-core/src/python.rs:110,124,527,945,984`); (b) cargo-mutants 27.1.0 cannot resolve the `PyResult` alias, so 229 PyErr-wrapper mutants in trading-core python.rs are UNVIABLE — the design depends on wrappers staying one-line delegates (`trading-core/.cargo/mutants.toml`, exclude_re block).

**No GIL release anywhere**: `Python::detach`/`allow_threads` is deliberately NOT used (`fno-math/src/python.rs:296-304,438-444`) — it wouldn't link under the no-libpython `cargo test`. Rayon spawns its own threads regardless of the GIL, so parallelism is unaffected; but the calling Python thread HOLDS the GIL for the whole kernel call. A new sweep-core crate should assume the same constraint.

---

## 2. fno-math surface (module `_fno_math`, registered `lib.rs:125-146`)

Exports: `__version__`, `__source_digest__` (dunder = invisible to symbol-set checks), `add_u64`, `bitand_count_u64s`, `bitcount_u64s`, `blake3_hash`, `count_supermask_u64s`, `count_supermasks_u64s`, `count_supermasks_u64s_nthreads`, `popcount_bytes`, `ResidentBars`, `ResidentSweepSim`, `ResidentSweepSimU128`, `sweep_drawdown_stats`.

### 2a. blake3_hash (`python.rs:153-157`)
`blake3_hash(data: &[u8]) -> Py<PyBytes>` — 32-byte single-tree BLAKE3. Consumed by `brutex.strategies.combo` for run identity (Golden Rule #4: run_id = blake3(9 fields)); an independent pip-blake3 oracle test pins `fno_math.blake3_hash ≡ blake3` (MIGRATION #1546).

### 2b. Supermask counting (`python.rs`)
- `count_one(bars: &[u64], mask: u64) -> u64` (`python.rs:199-203`) = SINGLE SOURCE OF TRUTH for the superset count `(bar & mask) == mask`, branchless `bool as u64` sum. Reused by free fns AND `ResidentBars`.
- Edge semantics (pinned by tests): `mask == 0` matches ALL bars; empty `bars` → 0; empty `masks` → `[]`; `out[j]` positionally aligns with `masks[j]`.
- `count_supermasks_dispatch` (`python.rs:237-247`): serial below `PARALLEL_WORK_THRESHOLD = 1<<20` mask·bar ops (`python.rs:216`), else rayon `par_iter().map().collect()` on the DEFAULT global pool — **order-preserving IndexedParallelIterator, bit-identical to serial; `par_bridge` forbidden** (not order-preserving). Dispatch predicate extracted to `lib.rs:95` `below_parallel_threshold(work, threshold) = work < threshold` (strict `<`; `saturating_mul` work estimate).
- `count_supermasks_u64s_nthreads` (`python.rs:334-366`): bench-only SCOPED `ThreadPoolBuilder` pool (`n_threads==0` = rayon default). Production NEVER calls `build_global` (double-call panics → SIGABRT).
- `ResidentBars` (`python.rs:404-453`): `#[pyclass(frozen, module="fno_math")]` holding `Vec<u64>` bar_bits resident; `new(bar_bits)`, `n_bars` getter, `count_supermasks(masks)`, `count_supermask(mask)`. O(N_bars) copy paid once at construction; only masks cross per call.

### 2c. trade_sim.rs — the resident trade-sim kernel (2209 lines)
- `TradeRecord = (pnl_paisa: i64, mfe_paisa: i64, mae_paisa: i64, holding_bars: u64, entry_idx: u64, exit_idx: u64)` (`trade_sim.rs:107`). Direction is a per-batch constant, NOT stored per trade.
- `Walker` (`trade_sim.rs:210-492`) = faithful port of Python `TradeSimWalker`. State: `position: Option<OpenPosition>`, `pending_entry: bool`, `pending_entry_session: i64`, `pending_exit: bool`.
- **push() order per bar** (`trade_sim.rs:430-482`): forced only consulted when position or pending entry exists → (A) fill pending entry at THIS bar's open IFF same `session_idx` as arm bar AND not forced (pending flag cleared unconditionally; cross-session fill DROPPED — R2-OC-2, mirrors `_pending_entry_day`) → (B) manage open position → (C) if flat AND nothing closed this bar AND entry_signal: arm pending entry recording this bar's session.
- **manage_position exit precedence** (`trade_sim.rs:371-426`): (1) pending next-open exit fills at open BEFORE forced; (2) forced 15:20 exit at open; (3) fold full (high,low) excursion; (4) SL/TP via `resolve_level_hit` (inclusive touch; same-bar tie `SlFirst` default / `TpFirst`), AtLevel closes at trigger, NextOpen sets pending_exit; (5) Mode-B exit_signal → pending_exit; (6) still open. Pending/forced exits fold ONLY `(open, open)` excursion; end-of-data `finish()` closes at last managed bar's CLOSE with NO excursion fold and IGNORES a pending exit.
- `holding_bars = saturating_sub(exit,entry) + 1` (`trade_sim.rs:508-510`) — inclusive; defensive saturate.
- `levels()`: BUY sl=entry−d / tp=entry+d; SELL mirrored (`trade_sim.rs:248-260`).
- **MAX_PRICE_PAISA = i64::MAX / 8** (`trade_sim.rs:157`) — boundary ceiling on all 4 OHLC columns (at build) + SL/TP distances (per batch call) making all hot-loop raw i64 add/sub provably `< i64::MAX` (worst |pnl| ≤ 3·C). Violation → clean ValueError.
- `SweepColumns {open, high, low, close: Vec<i64>, is_forced: Vec<bool>, session_idx: Vec<i64>}` (`trade_sim.rs:566-576`), width-independent, shared by both kernels; `build_columns` validates all 7 lengths equal + the R2-RC-F1 price scan (`trade_sim.rs:584-633`). Error messages self-name the pyclass via `label`.
- **`MaskWord` trait** (`trade_sim.rs:544-561`): the ONLY mask-touching op is `is_superset(bar, mask) = (bar & mask) == mask`, implemented for `u64` and `u128`; the whole walk is width-generic via `simulate_one_core<W>` (`trade_sim.rs:644-675`; **no Mode-B exit stream on the sweep path** — `exit_signal=false` always), `simulate_masks_core<W>` / `count_trades_core<W>` (`trade_sim.rs:684-748`) with the same 1<<20 serial/parallel dispatch (own `PARALLEL_WORK_THRESHOLD` copy at `trade_sim.rs:127`).
- **`ResidentSweepSim`** (u64; `trade_sim.rs:758-961`) and **`ResidentSweepSimU128`** (`trade_sim.rs:978-1153`): both `#[pyclass(frozen, module="fno_math")]`.
  - `__new__(open, high, low, close, is_forced, session_idx, bar_bits)` — all `Vec` extracts; unequal lengths → ValueError.
  - `simulate_masks(masks, is_buy, sl_paisa, tp_paisa, sl_next_open=false, tp_next_open=false, tp_first=false) -> Vec<Vec<TradeRecord>>`.
  - `count_trades(...)` same signature → `Vec<u64>` (bench fast path, no per-trade alloc).
  - `n_bars` getter is the ONLY buffer view (no bars getter by design).
  - u128 lane: `Vec<u128>` FFI probe-verified under pyo3 0.29 abi3-py312 (round-trip exact; `2**128` / negative → OverflowError at the extraction edge). Production-UNREACHABLE today (60-bit vocab); anti-drift test pins u64 ≡ u128 kernels on u64-fit inputs.

### 2d. metrics.rs — sweep_drawdown_stats
- `sweep_drawdown_stats(n_bars: usize, start_equity_paisa: i64, exits: Vec<(usize, i64)>) -> (i64, f64, i64, f64)` = `(max_dd_paisa, max_dd_pct×100, max_dd_duration_bars, ulcer×100)` (`metrics.rs:183-190`), pure core `drawdown_stats_checked` (`metrics.rs:84-173`).
- **f64-exactness envelope**: `MAX_EXACT_EQUITY_PAISA = 1<<52` (`metrics.rs:72`); wrapper pre-guards `start + Σ|pnl| ≤ 2^52` so `drop/peak` IEEE division equals CPython's exact-rational int/int bit-for-bit. Kernel validates with `checked_*`; any violation → ValueError → **Python wrapper routes that mask to the Python oracle, never raises**.
- Determinism rules the port must keep: strictly SEQUENTIAL left-to-right f64 fold, **NO Kahan, NO rayon reduction, NO `mul_add`/FMA** (FMA fuses where CPython rounds twice) (`metrics.rs:26-30,165-167`). `math.pow`-class ops (skewness, CAGR) DELIBERATELY stay in Python — libm pow has no correct-rounding guarantee (`metrics.rs:36-41`).
- Per-call `vec![0i64; n_bars]` scatter buffer (~4 MB at 500k bars) — alloc failure aborts (documented envelope).

---

## 3. trading-core surface (module `_trading_core`, registered `python.rs:1508-1545`)

`lib.rs:37-54` modules: `bits`, `evaluator`, `greeks`, `indicators` (ema/fib/pivots/vwap/market_structure/metadata), `spec`; `mod python` is `#[cfg(feature = "python")]` (`lib.rs:66-67`).

Exports (all also in the shim's `_EXPECTED_COMPILED_SYMBOLS`, `python/trading_core/__init__.py:85-117`): constants `TOTAL_CONDITIONS=60` (`bits.rs:147`), `IMPLEMENTED_MASK=(1<<60)-1=1152921504606846975` u128 (`bits.rs:155`), `PIVOT_LEVELS_LEN=12`, `FIB_LEVELS_LEN=15`, `SPEC_SCHEMA_VERSION=1`, `GREEKS_*` (5); fns `version`, `total_conditions`, `evaluate_from_raw`, `evaluate_with_levels`, `evaluate_spec`, `spec_canonical_bytes`, `compute_pivot_levels`, `compute_fib_levels`, `compute_ema`, `ema_alpha`, `ema_seed`, `ema_step`, `derive_bar_metadata`, `weekday_from_ordinal`, `minute_of_session`, `seconds_of_day`, `resolve_near_month`, `greeks`; stateful pyclasses `VwapState` (`python.rs:1313`) and `MarketStructureState` (`python.rs:1394`, `update(high, low, close) -> (bos_bull, bos_bear, choch_bull, choch_bear)` + `last_swing_high/low -> Option<i64>` getters delegating to the untouched core — TickVault-safe additive getters, #1548).

### 3a. FROZEN `evaluate_with_levels` ABI (`python.rs:452-519`, impl `:527-673`)
22 positional args, `#[pyo3(signature=...)]` pinned:
```
(tod_seconds: u32, open/high/low/close_paisa: i64,
 ema20/ema200_paisa: Option<i64>, vwap_paisa: Option<i64>,
 pivot_levels: Vec<i64>  # len EXACTLY 12: [p,r1,r2,r3,s1,s2,s3,pdh,pdl,pdc,r5,s5]
 fib_levels:   Vec<i64>  # len EXACTLY 15: [retrace@pdh,r23,r38,r50,r62,r78,retrace@pdl,
                         #  up127,up162,up200,up262, dn127,dn162,dn200,dn262]
 day_high/day_low/open_of_day_paisa: i64,
 pivot_bps/pdh_pdl_bps/fib_bps: i64,
 prior_n: usize, prior_flags: Vec<bool>,
 bos_bullish/bos_bearish/choch_bullish/choch_bearish: bool) -> u128
```
Length guards run FIRST (mismatch would otherwise OOB-index → SIGABRT); every level bar-bounded; unread struct fields ZEROED — incl. the 4 `retrace_bull_*` (evaluator never reads them, DEC-FIB-BULLISH-RETRACE-001) plus fib pdh/pdl/range/ext_424 and pivot r4/s4 (`python.rs:602-646`).

### 3b. FIB_LEVELS_LEN vs FIB_COMPUTE_LIST_LEN — the pun that must not re-conflate
- `FIB_LEVELS_LEN = 15` (`python.rs:41`) — the FROZEN evaluator ABI (input list to `evaluate_with_levels`).
- `FIB_COMPUTE_LIST_LEN = 19` (`python.rs:49`) — the OUTPUT of `compute_fib_levels(pdh, pdl)` (`python.rs:984-1012`): original 15 order untouched `[r23,r38,r50,r62,r78, up127..up424, dn127..dn424]`, then `retrace_bull_23/38/62/78` APPENDED last. Before #1560 both happened to be 15 — an accidental pun, now two documented distinct constants. Only `FIB_LEVELS_LEN` is exported to Python.
- `compute_pivot_levels(pdh,pdl,pdc)` returns an 11-list `[r5,r4,r3,r2,r1,p,s1,s2,s3,s4,s5]` (`python.rs:945-956`) — note DIFFERENT order from the evaluator's 12-list (which the Python wrapper `brutex/vocabulary/evaluator.py:160` reassembles: "Order MUST match trading_core.evaluate_with_levels").
- `indicators/fib.rs::compute_fib_levels` (`fib.rs:68-104`): `range = pdh − pdl` (int), each level `round_half_even(anchor ± ratio·range)` with IEEE literal + operation order mirrored token-for-token vs Python; `assert!(pdh >= pdl)` always-on (boundary pre-validates so FFI callers never trip it). Bullish ladder measures up from PDL (asymmetric — NOT the reversed bearish ladder); exact-value pins at `fib.rs:123-151`.

### 3c. Boundary ceilings (all pure-Result cores)
- `MAX_ANCHOR_PAISA = i64::MAX / 8` (`python.rs:85`) — prev-day anchors; derivation covers the coefficient-4 R5/S5 pivot rungs (`python.rs:51-84`).
- `MAX_BAR_PAISA = i64::MAX / 65536` (`python.rs:153`) — every per-bar price/level/EMA/VWAP the evaluator multiplies (`body*10`, `total_range*7`, `pdc*50`…), `check_evaluate_inputs` (`python.rs:187+`).
- `MAX_BPS = 1_000_000` + `close.checked_mul(bps)` product check (`python.rs:164`).
- `check_prev_day_ohlc` / `check_prev_day_hl` (`python.rs:110-138`) mirror the inner always-on asserts (`pdh>=pdl`, `pdl<=pdc<=pdh`).

### 3d. evaluator + spec
- `evaluate_atoms(&IndicatorSnapshot, &ProximityParams, &PriorBarHistory) -> u128` (`evaluator.rs`) — pure, stateless, allocation-free; GOLDEN-sensitive, DO NOT touch. BOS/CHoCH booleans resolved UPSTREAM by stateful `MarketStructureState` and threaded in like `prior_flags`.
- `bits.rs` is `@generated by scripts/codegen_vocab.py from conditions.toml` — DO NOT EDIT; bits 0..=59 dense, grow-only, never renumber (DEC-PLUGIN-001). `MASK_WIDTH = 128`.
- `spec.rs`: `StrategySpec {condition_mask: u128 (IMPLEMENTED-only at new()), direction: repr(u8) Long=0/Short=1, proximity, prior_n, instrument}`; mask serialized as HEX STRING under feature `serde` (u128 JSON number loses precision >2^53); `canonical_bytes` LE field encoding under `SPEC_SCHEMA_VERSION=1` — a Golden-Rule-#4 run-identity stamping surface (mutation-gated).
- Golden contract: repo-root `tests/golden/fixtures/vocab_golden_vectors.json` (400 cases, FROZEN) read by `rust/trading-core/tests/golden_vectors.rs` via `../../tests/...` — an out-of-crate relative path (one of the two reasons cargo-mutants must run `--in-place`).
- **DEC-SHARED-CORE-001**: TickVault links this crate NATIVELY (rlib, default features = zero deps). Any trading-core change must be additive + TickVault-safe; the 400-case answer key is the contract both sides satisfy. A sweep-core crate must CONSUME trading-core, never modify it.

---

## 4. Wheel build & Python consumption

- Build: **maturin >=1.7,<2.0**, mixed layout (`pyproject.toml` per crate): `python-source = "python"`, `module-name = "fno_math._fno_math"` / `"trading_core._trading_core"`; trading-core sets `features = ["python"]`; fno-math `features = ["pyo3/extension-module"]`; `strip = true`. Install = `pip install ./rust/<crate>` (PEP 517 triggers maturin) or `maturin develop --release`.
- Shims (`rust/*/python/*/__init__.py`): **UNCONDITIONAL import-time freshness handshake** — `_EXPECTED_COMPILED_SYMBOLS` tuple asserted against the compiled `.so` BEFORE re-export; stale wheel → actionable ImportError naming missing symbols + the rebuild command. `__build_crate_version__` derived from compiled `__version__`. Companion `EXPECTED_BUILD.json` (crate_version + sorted symbols; pinned equal to the tuple by tests) feeds `brutex doctor` wheel-freshness stages; `__source_digest__` feeds the doctor's `wheel_source_digest[_fno_math]` WARN stage vs the Python twin `brutex/wheel_digest.py` (FRAMING v1: Cargo.toml + `src/**/*.rs`, sorted by UTF-8 path bytes, frame = `path‖0x00‖u64-le(len)‖content`, BLAKE3; defined ONCE in `rust/source_digest_core.rs`, `include!`d by both build.rs and the parity tests). NOTE: the digest covers only the crate's OWN Cargo.toml+src — a path-dep's source drift would NOT move a dependent crate's digest.
- `.pyi` + `py.typed` per package for mypy/pyright strict. **Adding any symbol requires updating: the `#[pymodule]` block, `_EXPECTED_COMPILED_SYMBOLS`, the `from ._x import (...)` list, `__all__`, `__init__.pyi`, and `EXPECTED_BUILD.json` — all in one commit** (tests pin them equal).
- **`brutex.math` facade** (`brutex/math/__init__.py`): programmatic re-export of `_KERNEL_MODULES = ("fno_math", "trading_core")` by each module's `__all__`; **cross-kernel symbol collision → RuntimeError at import** (`:94-97`); the merged public surface is FROZEN by `tests/golden/test_math_facade_golden.py`. A third kernel must be appended to `_KERNEL_MODULES`, must not collide, and moves that golden.
- **`_indicator_version()`** (`brutex/cli.py:5150-5157`) = `f"brutex-{__version__}+trading_core-{trading_core.__version__}"` → part of run identity (§2 Rule 4). **A trading-core crate version bump re-keys every new run** (0.3.0→0.3.1 did exactly this, #1548, with zero math change — deliberate). fno-math's version is NOT in the string; neither would a new sweep-core's be unless added deliberately (a decision with run-identity consequences).

### 4a. Python seam files (the wrappers a Rust sweep-core replaces/absorbs)
- `brutex/engine/trade_sim.py`: the Python oracle. `TradeSimWalker.run` (`:323`) delegates to `run_with_exit_indices` (`:356`); module fns `simulate_trades` (`:727`) and `simulate_trades_with_exit_indices` (`:782`, returns `(trades, exit_indices|None)` — indices only when `bars_strictly_increasing(bars)` certifies the window). Session-aware forced-exit boundary `_forced_exit_seconds` memoises last IST date, raises `HolidayDataUnavailableError` out-of-coverage (`:405-436`). Also supports `exit_signals` (Mode B) + `reference_stamper` (VIX stamping) — features the Rust sweep path does NOT carry (sweep passes no exit stream, no stamper).
- `brutex/engine/rust_trade_sim.py::ResidentTradeSim` (738 lines) — the seam. Construction (`:317-403`): validates bars non-empty / lengths / tf_minutes≥1; `_derive_kernel_inputs` (`:405-462`) computes IN PYTHON (single sources of truth, §11 COMMON): paisa via `stage1a._price_to_paisa` (ROUND_HALF_UP Decimal→i64), session-aware `is_forced_exit_bar` flags via `get_session_checked`+`forced_exit_seconds_for_session` (1-slot date cache), and `session_days[i] = bar_day.toordinal()`. **The kernel must NEVER re-derive rounding/IST/calendar math.**
  - Width dispatch: all bar_bits ≤ `U64_MASK` → `ResidentSweepSim`; any in `(U64_MASK, 2^128-1]` → `ResidentSweepSimU128`; beyond/negative → oracle, reason `bar_bits_beyond_u128`; price beyond `_MAX_KERNEL_PRICE_PAISA=(2^63−1)//8` → `price_out_of_envelope`; `ImportError/AttributeError` → `wheel_missing`. Per call: mask beyond u128 → `mask_beyond_u128`; SL/TP above ceiling → oracle. A >64-bit MASK over a u64-resident window is NOT a fallback: lazy `_promote_wide_kernel()` builds the u128 sibling once (`:464-501`, one INFO `wide_kernel_promoted`); a wheel predating the class → warn-once `wheel_missing_u128`.
  - **Envelope-guard law**: an input outside the widest kernel's envelope must NEVER surface a kernel ValueError/OverflowError where the pure-Python oracle would simply simulate — the wrapper detects FIRST and routes to the oracle LOUDLY (warn once per `(instance, reason)`; repeats silent — chunked sweeps would flood logs).
  - **Domain normalisation**: `direction is HOLD` → ValueError; `sl/tp ≤ 0` → ValueError on BOTH arms (`:607-616`) — the kernel would silently accept 0 where the walker raises; wheel state must never change error behaviour.
  - `simulate_masks` is a thin delegate over **`simulate_masks_with_exit_indices`** (`:546-713`) → `list[tuple[list[ClosedTrade], list[int]|None]]`. FillPolicy maps to 3 bools (`sl_next_open`, `tp_next_open`, `tp_first`). Record mapping `_to_trade_and_exit` (`:715-738`): builds `ClosedTrade(pnl_paisa, mfe_paisa, mae_paisa, holding_bars, entry_ts=bars[entry_idx].timestamp, exit_ts=bars[exit_idx].timestamp, direction)` — **index→timestamp mapping stays Python-side**.
  - **Exit-index DOUBLE certificate** (DEC-SWEEP-EXIT-INDEX-FASTPATH-001): `_ts_certified = bars_strictly_increasing(bars)` once at construction; per-mask `_monotonic(exit_indices)` (`:216-233`, strictly increasing, ≥0). Non-monotonic on a CERTIFIED window = kernel ordering regression → WARN once `exit_indices_uncertified` + withhold (`None`) → metrics tail falls back to the bit-identical exit_ts→index dict path. Uncertified window withholds silently. The Python-fallback arm's indices ride the same window certificate.
  - `rust_active` property = the per-tuple observability signal (`sweep_run.trade_sim_path` logs it; CI asserts the wired path engages the kernel — a silently-degraded sweep is a fake-performance illusion).
- `brutex/engine/sweep_run.py`: ONE `ResidentTradeSim` per (instrument, tf) tuple (`:909-932`); mask batches chunked by `_trade_sim_chunk_masks(n_bars) = max(1, min(256, (4<<20)//n_bars))` (`:183-204`) — **chunk size must NEVER change results** (chunk-invariance tests pin it; up to thousands of chunks per tuple).
- `brutex/engine/combo_mask.py::ResidentBarBuffer` (`:423`) wraps `fno_math.ResidentBars` with a numpy-u64 fallback (exactly one backing store). `MAX_VOCAB_BITS = 128`, `U64_MASK` live here.
- `brutex/engine/sweep_driver.py` + `brutex/engine/rust_metrics.py`: default-ON Rust metrics tail (`SWEEP_RUST_METRICS_DEFAULT=True`, kill switch `BRUTEX_SWEEP_RUST_METRICS=0`); 16-sample per-process BURN-IN bit-compare vs the Python `_drawdown_stats` oracle — mismatch ships the ORACLE value and AUTOREVERTS the kernel for the process (`kernel_autoreverted` ERROR + latch). Exit-index fast path consumes the certified indices O(trades) with no per-mask O(N_bars) dict build; inconsistent stream demotes to the dict, never to the oracle.

---

## 5. CI wiring

- `ci.yml` env `RUST_VERSION: "1.93.1"` (`:73`), toolchain via `dtolnay/rust-toolchain@stable` pinned to it everywhere.
- Job `rust` (`ci.yml:2606-2634`): fno-math — `cargo fmt --check` → `cargo check --all-targets` → `cargo clippy --all-targets -- -D warnings` → `cargo test --release`; ubuntu-24.04, 20 min, `Swatinem/rust-cache` workspace `rust/fno-math -> target`.
- Job `rust-trading-core` (`ci.yml:2636-2698`): same four steps + `cargo check --features python` + maturin wheel build (3-attempt retry) + install + `python ../../scripts/check_vocab_rust_parity.py` (400-case Python-binding parity).
- **Every Python job that imports brutex installs BOTH wheels**: `pip install ./rust/fno-math` appears at **27 sites** in ci.yml (each with a 3-retry loop), always paired with `./rust/trading-core`. `run-smoke` reproduces the raw cold-start env (`.[data,ui]` + the kernels).
- `mutation.yml` (weekly cron 2026-07-13 04:00 UTC + dispatch; Rust matrices SKIP push):
  - `rust-mutation` fno-math: **6-shard matrix** `shard: [0..5]`, `cargo-mutants@27.1.0` PINNED (taiki-e/install-action), `cargo mutants --in-place --shard k/6 --test-tool cargo --no-shuffle --timeout 300 --output cargo-mutants.out -- --release`. Scope = `rust/fno-math/.cargo/mutants.toml` `examine_globs = ["src/metrics.rs","src/trade_sim.rs","src/python.rs","src/lib.rs"]`, 599 mutants (metrics 358 + trade_sim 167 + python 65 + lib 9), ALL zero-survivor; `compat/*` deliberately OUT (299 mutants, named follow-up). `--in-place` because build.rs `include!("../source_digest_core.rs")` breaks copy-mode baselines; `--in-place` precludes `--jobs` → parallelism = the shard matrix.
  - `rust-mutation-trading-core`: **7-shard matrix**, same flags; scope `.cargo/mutants.toml` `examine_globs = ["src/evaluator.rs","src/spec.rs","src/python.rs"]` + **`features = ["python","serde"]`** (python.rs is feature-gated at lib.rs:66 — 376 mutants; spec.rs mask_hex serde-gated — 3); 627 mutants (evaluator 238 + python 376 + spec 13). OUT: greeks/mod.rs 266, indicators/* 297, bits.rs 0 — all documented named follow-ups.
  - Fan-in `mutation-verdict` job owns issue filing (timeout-vs-survivor honest titles); per-shard jobs never touch issues. timeout-killed jobs report CANCELLED not FAILED (alert steps fire on `cancelled()` too).
- `rust-fuzz-smoke.yml`: builds EVERY cargo-fuzz target in `rust/trading-core/fuzz` + `rust/fno-math/fuzz`, ~30 s bounded run each; PRs touching `rust/**` + weekly Monday cron. Crash/panic/timeout/OOM = red; coverage plateaus non-blocking.
- Each crate has `deny.toml` (license gate; `publish=false` + `private.ignore` excludes the proprietary crate itself).

---

## 6. Where a NEW sibling crate (rust/sweep-core) slots with least disturbance

> **SUPERSEDED (Verifier A round-1 fold, 2026-07-09).** This section explored
> the SIBLING-CRATE option; the LOCKED design is the IN-CRATE placement — a
> NEW `sweep_core` module inside `rust/fno-math`
> (`rust/fno-math/src/sweep_core.rs` + the metrics.rs seam + lib.rs module
> registration + `python/fno_math/__init__.pyi` stub additions; DESIGN_FINAL
> §1.1, gap-#18 coordinator ruling in DESIGN_FINAL §11). No `rust/sweep-core`
> crate, no new wheel, no facade/_KERNEL_MODULES/ci.yml/mutation.yml crate
> additions. Retained verbatim below as the option analysis; items 1–10 do NOT
> apply as written (the conventions they catalogue — SIGABRT discipline,
> digest, handshake idioms — remain accurate reference material, and §7's
> gotchas remain fully binding).

1. **Crate**: new independent root `rust/sweep-core` (there is NO cargo workspace — each crate is its own root with its own Cargo.lock; least disturbance = keep that pattern). Copy the conventions verbatim: edition 2024, `rust-version = "1.93.1"` (must equal ci.yml `RUST_VERSION`), `crate-type = ["cdylib","rlib"]`, the exact release profile (`panic="abort"`, `overflow-checks=true`, lto thin, cgu 1), `publish = false`, lib.rs lint header (`deny(unsafe_code)`, deny unwrap/expect/print in non-test).
2. **trading-core dependency**: `trading-core = { path = "../trading-core" }` with DEFAULT features (pure core — pulls only `jaeckel`; keeps TickVault-parity semantics). Do NOT enable trading-core's `python` feature from sweep-core (two pyo3 extension modules must each own their own `#[pymodule]`; consuming the pure Rust API is the clean cut). trading-core itself is UNTOUCHED (DEC-SHARED-CORE-001 — bit-identical for TickVault; 400-case golden frozen).
3. **pyo3**: `pyo3 = { version = "0.29", features = ["extension-module", "abi3-py312"] }` (unconditional, fno-math style, or feature-gated `python` trading-core style — the latter needed only if some native consumer will link the rlib).
4. **build.rs**: `include!("../source_digest_core.rs")` → `BRUTEX_SOURCE_DIGEST` → `SOURCE_DIGEST` + `__source_digest__` (and a doctor `wheel_source_digest_sweep_core` stage + `brutex/wheel_digest.py` twin entry). Note the digest covers only sweep-core's own src — trading-core drift is caught by trading-core's own digest, not sweep-core's.
5. **Wheel**: `pyproject.toml` (maturin >=1.7,<2.0; `python-source="python"`, `module-name="sweep_core._sweep_core"`, strip=true) + `python/sweep_core/{__init__.py,__init__.pyi,py.typed,EXPECTED_BUILD.json}` with the `_EXPECTED_COMPILED_SYMBOLS` unconditional handshake — the established stale-wheel tripwire (fno_math's was made urgent exactly because a kernel entered the unflagged hot path).
6. **Facade**: append `"sweep_core"` to `brutex/math/__init__.py::_KERNEL_MODULES`; symbol names must not collide with fno_math/trading_core (import-time RuntimeError); update the frozen facade golden `tests/golden/test_math_facade_golden.py`.
7. **ci.yml**: clone the `rust` job as `rust-sweep-core` (fmt/check/clippy/test + rust-cache workspace); add `pip install ./rust/sweep-core` (3-retry idiom) to jobs exercising the sweep (worst case all 27 install sites); job names feed `ci-ok` and `branch-protection-drift.yml` — adding a required job is a branch-protection change.
8. **mutation.yml**: new shard-matrix job + `rust/sweep-core/.cargo/mutants.toml` (`examine_globs`, measured shard budget, `--in-place` mandatory if build.rs include! is used, cargo-mutants pinned 27.1.0). Zero-survivor is the standing bar.
9. **rust-fuzz-smoke.yml**: add a `fuzz/` harness crate or extend the workflow's crate list.
10. **Doctor/EXPECTED_BUILD**: wheel-freshness + source-digest stages in `brutex/doctor.py`.

## 7. Gotchas a Rust sweep-core conversion MUST NOT break

- **SIGABRT discipline**: with `panic="abort"` + `overflow-checks`, EVERY panic/overflow/OOB-index aborts pytest uncatchably. All input validation at the FFI boundary, pure `Result<_,String>` cores, hot loops proven overflow-free via magnitude ceilings (the `MAX_PRICE_PAISA = i64::MAX/8` idiom). Never `unwrap/expect/index/as`-cast in pymethod bodies. Never `rayon::ThreadPoolBuilder::build_global`.
- **Bit-identity contracts**: trades must stay byte-identical to `TradeSimWalker` (goldens `tests/golden/test_trade_sim_rust_golden.py`, hypothesis parity `tests/property/test_trade_sim_rust_parity.py`); the sweep metrics golden `_GOLDEN_CONTENT_HASH`/`_GOLDEN_RUN_ID` frozen; blake3 run identity consumes exact outputs — a single differing trade/metric bit re-keys or corrupts runs.
- **Float rules** (if any metrics move): sequential left-to-right f64 folds only; no FMA/`mul_add`, no Kahan, no parallel reduction; divisions only on ≤2^53-exact operands (the 2^52 envelope pre-guard); libm `pow` stays in Python.
- **Oracle fallback + envelope guards live in Python and must survive**: the wrapper decides kernel-vs-oracle BEFORE the FFI call; wheel state (present/stale/absent) must never change error behaviour or results; warn-once per (instance, reason); `rust_active`/`trade_sim_path` observability.
- **Python-owned single sources**: `_price_to_paisa` (ROUND_HALF_UP), session/forced-exit calendar (incl. `HolidayDataUnavailableError` refusal), `session_idx = IST date ordinal`, index→timestamp mapping for `ClosedTrade`. A sweep-core that re-derives any of these in Rust violates §11 COMMON and will drift.
- **Semantics traps encoded in the walker**: pending-exit beats forced; forced only when position/pending exists; excursion folds differ by exit kind ((high,low) vs (open,open) vs none); exit bar consumes the entry signal; cross-session pending entry DROPPED; finish() at last bar CLOSE ignoring pending exit; inclusive SL/TP touch; `SlFirst` default tie.
- **Order preservation**: `out[j]` aligns with `masks[j]` under both serial and rayon arms; serial/parallel threshold strict `<` at 1<<20 mask·bar ops; both arms must be output-identical (the mutation gate tests the predicate, not the arms).
- **Chunk invariance**: results independent of `simulate_masks` batch partitioning (256-mask / 4·2^20-cell chunking is pure batching).
- **Exit-index certification chain**: window strictly-increasing (once) + per-mask monotonic stream; refusal is loud-withhold to the dict path, never trust, never oracle.
- **u64 vs u128 lanes**: production is u64 (60-bit vocab, `IMPLEMENTED_MASK=(1<<60)-1`); u128 is headroom served by the SAME generic core (`MaskWord`) with an anti-drift equality pin; a sweep-core must keep one width-generic core, not two copies.
- **Frozen ABIs**: `evaluate_with_levels` 22-arg / 12-pivot / 15-fib lists; `FIB_LEVELS_LEN(15) ≠ FIB_COMPUTE_LIST_LEN(19)`; `compute_pivot_levels` 11-list order differs from the evaluator's 12-list; bits grow-only never renumber; `TradeRecord` 6-tuple shape.
- **Version/identity coupling**: bumping trading-core's crate version moves `_indicator_version()` → new runs re-key (deliberate but must be a recorded decision); adding sweep-core to that string would be a run-identity change requiring its own DEC.
- **cargo-mutants ergonomics**: keep logic out of `PyResult`-returning wrappers (UNVIABLE under 27.1.0, so wrapper logic would be unkilled if viable rules change); in-file `#[cfg(test)]` exact-value pins expected for every new kernel; `.max()` over `if new > acc` to structurally remove tie-equivalent mutants.
- **GIL is held during kernel calls** (no allow_threads) — a long-running Rust sweep loop blocks all Python threads in-process; the multiprocess sweep workers are the concurrency story, not GIL release.
