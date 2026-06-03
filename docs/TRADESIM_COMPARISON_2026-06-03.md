# Stage-2 Trade-Sim Walker — the plain-English comparison view

> **For everyone** (engineer or not). This is the "what did we build, is it
> fast, is it correct, is it tested?" table view for the new spot
> trade-simulation walker (`brutex/engine/trade_sim.py`, DEC-TRADESIM-001).
> Honest by rule (CLAUDE.md Golden Rule #10): green = proven now, amber =
> partial with the gap named, grey = a later PR. No illusions.

---

## 1. What it does (in one breath)

> A "strategy" says **when to enter** (one yes/no flag per minute). This walker
> takes those flags + the price candles and produces the actual **trades**
> (each with profit/loss, how long it ran, best/worst point reached). Those
> trades feed the metrics engine that ranks strategies (Sharpe, win-rate, …).

```
 entry flags (yes/no per bar) ─┐
                               ├──►  TradeSimWalker  ──►  ClosedTrade list  ──►  compute_metrics ─► Sharpe / win-rate / …
 price candles (OHLC per bar) ─┘        (this PR)          (int64 paisa)            (already built)
```

---

## 2. The "is it fast?" table (speed & complexity)

| What you care about | This walker | Why / honest note |
|---|---|---|
| Speed per price-bar | ⚡ **O(1) — constant** | Each bar does a fixed handful of integer compares + at most one open/close. No looking back over history. |
| Memory per price-bar | ⚡ **O(1) — constant** | One small fixed-size "open position" record; nothing grows as bars pile up. |
| Uniqueness / dedup / mapping | ⚡ **O(1)** (in the layers it plugs into) | The walker itself does no dedup; it feeds the existing O(1) Bloom+LMDB dedup + u64-mask layers. |
| Measured speed gate | ✅ **1.01× at 100,000 vs 1,000 bars** (ceiling 5×) | A real benchmark, best-of-5 runs. Flat = no hidden slow-down as data grows. |
| Whole-sweep speed (33 billion trials) | ⬜ **Not claimed here** | Honest: O(1) is **per bar**, not for the whole sweep. Full-speed needs the later parallel/pruning PRs. This is the correct foundation, not the finish line. |

**Plain-English:** processing the millionth bar costs the same as the
thousandth bar. That is the "O(1) latency / O(1) time / O(1) space **per bar**"
guarantee — measured, not promised.

---

## 3. The "is it correct?" table (the rules it follows)

| Trading rule | What the walker does | Configurable? |
|---|---|---|
| **Enter** | At the **next bar's open** after a signal (no peeking at the future) | 🔒 Locked (spec rule) |
| **Stop-loss / Take-profit hit** | Fill **exactly at the trigger price** (default) | ✅ Yes — or fill at next bar's open |
| **Both SL & TP in one bar** | Assume the **loss first** (pessimistic, safe default) | ✅ Yes — or assume the win |
| **3:20 PM (15:20 IST) auto-close** | Force-close at that bar's open; **beats every other exit** | 🔒 Locked (spec rule) |
| **Exit on a reversal signal** | Fill at the **next bar's open** | 🔒 Locked (spec rule) |
| **Data runs out mid-trade** | Close at the **last bar's close** | 🔒 Locked (safe default) |
| **Costs / brokerage / slippage** | **None** — spot indexes aren't tradable, so adding costs would be fiction | 🔒 Locked (spec rule) |
| **Money unit** | **int64 paisa** everywhere (no floats in the hot path) | 🔒 Locked (spec rule) |

### The bug we fixed along the way (the 3:20 PM trap)

| Case | OLD code (buggy) | NEW code (fixed) |
|---|---|---|
| 1-minute bar at 15:20 | ✅ force-exit | ✅ force-exit (unchanged) |
| **15-minute bar opening 15:15** (runs to 15:30, crosses 15:20) | ❌ **missed** — position wrongly survived | ✅ **force-exit** (its window crosses 15:20) |
| Invalid timeframe (0) | 🤷 silently wrong | 🛑 clear error |

The old `is_forced_exit_bar` ignored the candle's length; a 15-minute candle
that *straddles* 15:20 slipped through. Now fixed + 8 regression tests.

---

## 4. The "is it really tested?" table (no illusions)

| Test type | File | What it proves | Status |
|---|---|---|---|
| Unit (every branch) | `tests/unit/test_engine_trade_sim.py` | 40 cases — every exit kind, long+short, every policy | ✅ **100% line + branch** |
| Property (fuzz) | `tests/property/test_engine_trade_sim_properties.py` | Same input → same output (determinism) | ✅ 200 random cases |
| Property (no peeking) | same file | Changing the **future** never changes a **finished** trade | ✅ 200 random cases |
| Golden (hand-checked) | `tests/golden/test_engine_trade_sim_golden.py` | Hand-computed trades match **byte-for-byte** | ✅ Exact |
| Benchmark (speed) | `tests/bench/test_engine_trade_sim_o1.py` | Speed stays flat as data grows 100× | ✅ 1.01× (ceiling 5×) |
| Regression (the bug) | `tests/unit/test_calendar_nse.py` | The 3:20 PM trap can't come back | ✅ 8 cases |
| Type safety | `mypy --strict` + `pyright` strict | No type holes | ✅ 0 errors, 0 warnings |
| Lint / format / security | `ruff` + `ruff format` + `bandit` | Style + no risky patterns | ✅ Clean |

**Coverage, stated honestly:** the **new module** is 100% line + 100% branch
(168 statements / 50 branches, nothing missed). The repo-wide gate is "≥ 90%",
which this clears.

---

## 5. The "what's NOT done yet" table (the honest envelope)

| Capability | State | Where it lands |
|---|---|---|
| Turn mask-hits into trades + metrics | ✅ **Done (this PR)** | here |
| O(1)-per-bar, deterministic, no-look-ahead | ✅ **Done & measured** | here |
| Wire Stage 1A → 1B → 2 end-to-end (the driver) | ⬜ Future | PR-4 |
| Coarse pre-filter (Stage 1B) | ⬜ Future | PR-3 |
| Full 33-billion-trial sweep at the 2–4 day SLA | ⬜ Future | needs the parallel/pruning PRs |
| Live `evaluate(N)` look-ahead enforcer (Property P-06) | 🟡 Invariant proven by property test; live accessor is later | PR-2+ roadmap |

---

## 6. One-line summary

> **Correct today, fast per-bar today, fully tested today** — and honest that
> the *whole-sweep* speed target is a job for the later PRs this one unblocks.
