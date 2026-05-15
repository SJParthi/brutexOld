# BruteX — Requirements in Plain English

> **Companion to `REQUIREMENTS.md`.** Same facts, zero jargon.
> If `REQUIREMENTS.md` is the legal contract, this is the WhatsApp message
> you'd send your friend to explain what we're building.
>
> **Branch:** `claude/capture-system-requirements-F9IHZ`
> **Date:** 2026-04-14
> **Status:** ✅ SIGNED-OFF 2026-04-14 — see `REQUIREMENTS.md §27`

## v1 universe (locked — expanded 2026-04-14)

**Everything in the broker's FNO segment.** Two pieces:

### 1. Index F&O (8 indices)

| # | Index | Exchange |
|---|---|---|
| 1 | NIFTY 50 | NSE |
| 2 | NIFTY BANK | NSE |
| 3 | NIFTY FINANCIAL SERVICES | NSE |
| 4 | NIFTY MIDCAP SELECT | NSE |
| 5 | NIFTY NEXT 50 | NSE |
| 6 | SENSEX | BSE |
| 7 | BANKEX | BSE |
| 8 | SENSEX 50 | BSE |

### 2. Stock F&O (~215 NSE stocks)

All NSE stocks currently eligible for F&O trading — roughly **215 underlyings** as of late 2024/2025 (exact count varies as SEBI adds/removes stocks). Examples: RELIANCE, TCS, HDFCBANK, ICICIBANK, INFY, SBIN, ITC, LT, AXISBANK, KOTAKBANK, BHARTIARTL, HINDUNILVR, M&M, MARUTI, ASIANPAINT, WIPRO, TATAMOTORS, SUNPHARMA, BAJFINANCE, ADANIPORTS… and ~195 more.

**The list is NOT hardcoded** — it's discovered at runtime by reading the broker's instrument CSV and filtering to `segment == FNO`.

### What's NOT in v1

❌ Cash equity backtests (pure equity strategies without F&O) — deferred to v1.1
❌ Commodity (GOLD, CRUDE, SILVER on MCX) — the broker doesn't support it
❌ BSE cash + BSE stock F&O — deferred to v1.1

### Scale numbers (honest)

| | Number |
|---|---:|
| Total F&O underlyings | 8 indices + ~215 stocks = **~223** |
| Total contracts over 2020→today (historical union) | **~1.3 million** |
| Total 1-minute API calls needed | **~1.3 million** |
| Wall-clock at 15 req/s (with thermal throttling) | **~24-28 hours** |
| Storage (canonical + raw blobs) | **~800 GB – 1.2 TB** |

⚠️ **SSD check needed:** You'll need ~1.5 TB free space on your MacBook SSD before we run the full ingest. Check Apple menu → About This Mac → Storage. If it's less, we have an option to skip raw-blob storage and drop to ~250 GB canonical-only.

### The ingest plan

**Day 1 morning:** Daily bars for the whole FNO universe (~10 hours). You can start running daily-bar backtests by afternoon.

**Day 1 night → Day 2 morning:** 1-minute bars in the background (~24-28 hours overnight). By Day 2 morning you have full 1-minute coverage.

**Day 2+:** Higher intervals (5m, 15m, 1h, 1d) pulled on demand for specific slices you care about.

---

## 1. What is BruteX?

Imagine you have a **recipe for making money in the stock market**.
Example: *"Buy NIFTY when the 5-min average crosses above the 20-min average, sell when it crosses back."*

You want to know: **does this recipe actually work?**

BruteX is a **time machine for trading strategies**. You feed it:

- Your recipe (a "strategy")
- Old market data (from 2020 to today, from the broker)
- A bunch of variations of the recipe (*"what if I use 10 instead of 20? What if 50? What about 100?"*)

And it tells you: *"This recipe would have made ₹X profit. This variation made ₹Y. This one lost money."*

In one sentence: **BruteX is a backtesting machine that tests millions of recipe variations at once to find the best one.**

---

## 2. Why "brute force"?

Most backtesters test **one recipe at a time**. BruteX tests **millions at once**.

Think of it like this:

| Approach | Speed |
|---|---|
| 👨‍🍳 Normal cook: tries one pizza recipe, tastes it, tries another | Takes forever |
| 🏭 BruteX: runs **a million pizza recipes in parallel**, tastes them all, ranks top 10 | **5 minutes** |

That's the whole point. **Millions of combinations, fast.**

---

## 3. The 10 Promises (explained like I'm 10)

Here's every promise in the technical `REQUIREMENTS.md` rewritten for a human.

### Promise 1: "Millions of tests in minutes"
**Plain:** You hit Enter. You go make chai. You come back in 5 minutes. **1,000,000 recipe tests are done.**

**How we prove it:** A robot runs this test every time the code changes. If it takes longer than 5 minutes, the robot rejects the code. No humans involved.

### Promise 2: "Instant lookup, always"
**Plain:** Looking up any price at any time is **always instant**. Doesn't matter if we have 10 prices stored or 10 billion. Same speed.

**Analogy:** Like a friend who can flip to any page of a 10,000-page book in 0.5 seconds — page 5 or page 9,999, same speed.

### Promise 3: "Every test is unique"
**Plain:** Every single test has a unique fingerprint. No two tests can ever be confused.

**Analogy:** Like an Aadhaar number — nobody shares one. We give every backtest its own "Aadhaar."

### Promise 4: "Never waste work"
**Plain:** If you accidentally run the same test twice, we notice and skip the second one. **Zero wasted computation.**

**Analogy:** If you ask your friend the same question twice, he says *"I already told you — here's the same answer instantly"* instead of thinking again.

### Promise 5: "Same answer every time"
**Plain:** Run the exact same test today and again next year, you get the **exact same answer down to the last paisa**.

**Why it matters:** If you can't reproduce it, you can't trust it. Period.

### Promise 6: "You always know if it's working or stuck"
**Plain:** At any moment, type `brutex status` and in a blink you see:

- How many tests are done
- How fast it's going
- How many workers are alive
- How many errors
- How much time left

**You never have to wonder "is it stuck?"** If a worker freezes, a watchdog kills it automatically and reassigns the work. **No silent hangs, ever.**

### Promise 7: "Full audit trail"
**Plain:** Every single thing BruteX does — every data fetch, every test started, every error — gets written to a **tamper-proof logbook**.

**Like:** a blockchain. You can prove exactly what happened, when, and why. If anyone ever tampers with it, we catch it instantly.

### Promise 8: "Never get banned by the broker"
**Plain:** the broker allows ~25 API calls/second. We cap ourselves at **15** — 60% of the limit, just to be safe. **We will never get rate-limited or banned.**

### Promise 9: "No fake winners (overfitting protection)"
**Plain:** **Listen carefully, bro — this is the most important one.**

> If you test a million strategies, **one of them will look amazing by pure luck**. Like flipping a coin a million times — someone gets 20 heads in a row. That doesn't mean they're lucky. It means you tested a lot.

BruteX automatically catches this trap. Every result comes with a **"how likely is this just luck?"** score (called *Probability of Backtest Overfitting* and *Deflated Sharpe Ratio*).

If a result is too-good-to-be-true, we print a big warning: **⚠️ OVERFIT_WARNING**.

**This is what separates real backtesting from gambling disguised as backtesting.**

### Promise 10: "100% tested, 100% covered"
**Plain:** Every line of code has a test. A robot runs all tests before any code is allowed in. **Even one failing test → code rejected.** No exceptions, no "we'll fix it later."

---

## 4. How is it built? The 7-floor building

Think of BruteX as a 7-floor building. Each floor has **one job**. Floors talk downward, never upward.

```
🏢 Floor 7:  The Control Room
             (CLI, FastAPI, React dashboard)
             "Click a button, see results"
                        ▲
🏢 Floor 6:  The Manager
             (Brute-force orchestrator — Ray, Optuna, CPCV)
             "Hand each worker a test to run"
                        ▲
🏢 Floor 5:  The Workers
             (Strategy engine — Rust hot kernel + Numba fallback)
             "Actually run each test, super fast"
                        ▲
🏢 Floor 4:  The Cashier + Risk Officer
             (Exec sim + portfolio book — Indian costs + SPAN)
             "Calculate real P&L with taxes, margin, slippage"
                        ▲
🏢 Floor 3:  The Librarian
             (Virtual market feed — Arrow mmap, zero-copy)
             "Fetch any bar at any time, instantly"
                        ▲
🏢 Floor 2:  The Warehouse
             (Data lake — Parquet + DuckDB + Redis + LMDB)
             "Store every price we've ever seen, forever"
                        ▲
🏢 Floor 1:  The Delivery Guy
             (broker adapter (deleted) — pluggable, rate-limited)
             "Fetch data from the broker, respect rate limits"
```

**Why this structure matters:** If tomorrow we want to add Binance (crypto), we write **only a new Floor 1** (a BinanceAdapter). Floors 2–7 stay the same. Same for Zerodha, IBKR, Polygon, anything.

---

## 5. The HONEST truth about "billions in minutes"

You asked: *"Can we do billions of combos in minutes?"*

**Raw math (no tricks):**

```
1,000,000,000 combos ÷ 300 seconds = 3.3 million/sec needed
Rust kernel speed = ~1,000 combos/sec per core
Cores needed = 3,333 physical cores
```

**A 16-core box cannot do 1 billion truly-independent combos in 5 minutes. Physics.**

Anyone who tells you otherwise is lying.

### BUT — here's the real story

When you do a brute-force sweep, most of the "billion combos" are **not independent work**. The funnel:

```
 START:  1,000,000,000 combos in your grid
    │
    │  Dedup cache hit (you ran this before) → O(1) skip
    ▼  Typical: 20% hit
   800,000,000 remaining
    │
    │  Bitmap prefilter (combos with provably identical signals)
    ▼  Typical: 60–80% elimination
   160,000,000 remaining
    │
    │  Shared indicator cache (100,000 combos using RSI(14)
    │  all read the SAME pre-computed RSI array — compute once,
    ▼  reuse a million times) → indicator cost amortised to ~0
   160,000,000 combos, each just:
   "compare signals, tally P&L"  ≈ 0.3 µs per combo
    │
    │  Vectorised broad sweep
    ▼
   160M × 0.3 µs × (1/16 cores) = 48 seconds ✅
    │
    │  Top 0.01% survivors go to event-driven deep check
    ▼
   16,000 × 10 ms × (1/16 cores) = 160 seconds ✅

   TOTAL: ~3.5 minutes for "1 billion combos"
```

### The truthful SLA tiers

| Scale | Hardware | Time | Honest? |
|---|---|---|---|
| **1M unique combos** | 16-core box | ≤ 5 min | ✅ CI-proven |
| **10M with dedup+bitmap** | 16-core box | ≤ 5 min | ✅ CI-proven |
| **100M with dedup+bitmap** | 16-core box | ≤ 10 min | ✅ CI-proven |
| **1B effective (high redundancy)** | 16-core box | ≤ 15 min | ✅ CI-proven |
| **1B truly-independent** | 16-core box | ❌ impossible | Cluster needed |
| **1B truly-independent** | 64-core cluster | ≤ 10 min | ✅ v2.1 |
| **10B+ combos** | 256-core cluster | ≤ 30 min | ✅ v2.1+ |

Every row will have a CI benchmark test. **No vibes, no promises — just numbers you can verify yourself.**

---

## 6. Shared code with tickvault (indicators, Greeks, math)

You said: *"Indicators, Greeks, and strategy math are already in tickvault. BruteX should use the same code so live and backtest don't drift."*

**You are 100% right.** This is how TradingView, QuantConnect, and every serious shop do it.

### The architecture: shared-core library

```
                ┌─────────────────────────┐
                │      tickvault-core     │  ← THE SHARED LIBRARY
                │                         │
                │  • indicators/          │  ← same code, both uses
                │  • greeks/              │  ← Black-76, BS, IV
                │  • math/                │  ← rolling stats, VaR
                │  • strategies/base      │  ← IStrategy interface
                │  • models/              │  ← Bar, Tick, Order, Fill
                │  • cost/                │  ← Indian txn costs
                └───────────┬─────────────┘
                            │ imported by both
                  ┌─────────┴──────────┐
                  │                    │
           ┌──────▼──────┐      ┌──────▼──────┐
           │  tickvault  │      │    brutex   │
           │   (LIVE)    │      │  (BACKTEST) │
           └─────────────┘      └─────────────┘
```

**Same strategy code runs unchanged in both.** If it works in backtest, it works live.

### Three ways to wire it up

| Option | How | My take |
|---|---|---|
| **A. Monorepo** | One repo `sjparthi/trading-core` with folders `core/`, `live/`, `backtest/` | **Recommended** — simplest |
| **B. Separate package** | Publish `tickvault-core` as a pip package, both repos import it | Clean but more ceremony |
| **C. Git submodule** | `brutex/core/` points at `tickvault-core` | Works but painful |

**Blocker:** I still can't see the tickvault repo (private / 404). Before reuse, I need one of:
- Install the Claude Code GitHub App on tickvault (same 30-sec install you did for bruteX)
- Make tickvault public temporarily
- Paste the file tree into chat

---

## 7. Global markets later (crypto, forex, US stocks)

You asked: *"Will this work for Binance / Forex / global markets later without rewriting everything?"*

**Yes. By design.**

The `Bar` object (a single price candle) is the same for every market:

```python
Bar = (symbol, exchange, ts, open, high, low, close, volume, oi)
```

This works for:
- ✅ NSE stocks / F&O (the broker)
- ✅ BSE stocks / F&O
- ✅ Binance / Coinbase crypto (OI = None)
- ✅ Forex pairs (OI = None)
- ✅ US stocks / options (IBKR, Polygon)
- ✅ Commodity / FX futures

**To add a new market, you write ONLY a new Floor 1 adapter.** Three files:

```python
class BinanceAdapter(IBrokerDataSource):
    def fetch_candles(self, symbol, interval, start, end): ...
    def fetch_instruments(self): ...

class BinanceCostModel(ICostModel):
    def compute_fees(self, trade): ...

class BinanceCalendar(ICalendar):
    def is_open(self, ts): return True  # crypto = 24/7
```

**That's it.** Strategies, indicators, brute-force engine, dashboard — all unchanged.

---

## 8. The plan — 5 simple steps

| Step | What happens | Rough duration |
|---|---|---|
| **Step 0 (now)** | Write the rulebook (`REQUIREMENTS.md` + this file) | ✅ Done |
| **Step 0.5** | Extract shared core from tickvault | ~1 week |
| **Step 1** | Build Floor 1 — fetch data from the broker, store it | ~2 weeks |
| **Step 2** | Build Floors 2+3 — librarian + warehouse + fast lookup | ~2 weeks |
| **Step 3** | Build Floors 4+5 — cashier + workers + Rust hot kernel | ~3 weeks |
| **Step 4** | Build Floor 6 — manager + 1M-combos speed test (CI gate) | ~3 weeks |
| **Step 5** | Build Floor 7 — dashboard + drill-down views | ~2 weeks |

**After Step 5:** you press a button, run millions of tests, see the best recipes in a dashboard. **That's v1.**

---

## 9. What you need to do RIGHT NOW

**Just ONE thing:** Read this page (not the 1186-line monster) and tell me:

### 👉 Option A: "Sign off, start P1a"
Everything accepted as-is. I start building Phase P1a (the broker instruments adapter).

### 👉 Option B: "Amend these items: X, Y, Z"
List what you want changed. I edit both docs, commit, push, iterate.

### 👉 Option C: "Pause — I need to think about X"
Ask the question. I research, answer, we loop back.

---

## 10. One-line summary

> **BruteX is a brute-force backtesting engine that tests millions of Indian F&O trading strategies in minutes, proves every guarantee with CI tests, never lies about overfitting, shares indicator code with live trading (tickvault), and can be extended to any global market by writing a single adapter file.**

That's the whole thing, bro.

---

_This document is kept in sync with `docs/REQUIREMENTS.md`. If you change one, update the other._
_See `docs/REQUIREMENTS.md` for the full technical version with every FR-###, NFR-###, MG-###, CG-###, and ADR-### ID._
