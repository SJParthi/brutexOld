# BruteX — Product Scoreboard

> A plain-English, customer-facing view of what's **finished**, what's **shipping now**, and
> what's **planned next**. Every row is real, merged, and verified — generated from the live
> decision ledger (`docs/MIGRATION.md`). A self-contained, shareable HTML one-pager lives next to
> this file at [`docs/product_scoreboard.html`](./product_scoreboard.html) (open in any browser).
>
> _Last refreshed: 2026-06-23 — now covering the ledger through entry **#911**
> (DEC-PRODUCT-SCOREBOARD-001, MIGRATION.md #902; refresh under DEC-SCOREBOARD-REFRESH-001).
> The number in the last column is the **ledger entry** (`docs/MIGRATION.md #NNN`), the
> authoritative record of each change._

---

## What BruteX is, in one line

A research engine that captures **every minute** of India's index & F&O market history
(2020 → today), shows you **what's actually moving** in real time, and brute-force tests trading
ideas at massive scale — built so **nothing is ever slow, nothing is ever lost, and you always
know what's happening.**

| At a glance | |
|---|---|
| Tradable names in the data lake | **~750** (3 brute-force indices + ~212 F&O stocks + NTM reference universe) |
| Timeframes, all derived from 1-minute data | **21** |
| History depth | **2020 → today**, minute-by-minute |
| Markets & instruments | NSE + BSE · spot · futures · options |
| Test coverage on every change | **100% line + 100% branch** |
| Quality gates per change | **60+** automated CI gates + an independent hostile review |

> Scope note (honest): the brute-force **engine** trades the 3 indices (NIFTY / BANKNIFTY /
> SENSEX); the ~750-name lake is reference data for future expansion. Nothing in the engine scope
> is ever changed silently.

---

## ✅ Finished & Live — already merged into the product

### ⚡ Theme 1 — "Nothing is ever slow"

| Feature | Before → After | Why you care | Ledger |
|---|---|---|---|
| Instant view switching | Re-scanned the whole multi-year data tree on every click → cached instant handoff | No waiting when you change candle views | #896 |
| Instant Universe page | Waited 30–160 s on a slow external site every visit → served from cache | The ~750-name list opens immediately | #892 |
| Instant Completeness page | Re-scanned the whole lake each load → O(1) on repeat | "How complete is my data?" answers instantly | #895 |
| Fast "Fetch Windows" | Counted 340,000+ rows in Python per click → database-paginated | Stays snappy no matter how big history grows | #893 |
| Every data tab paginated | The last whole-table reads → page-at-a-time database queries | Browsing huge tables never stalls | #863 / #864 |
| Cached page totals | Re-counted millions of rows on every paging click → memoised | Paging the candle browser is instant | #867 |

### 📊 Theme 2 — "See what's actually moving"

| Feature | What it does | Why you care | Ledger |
|---|---|---|---|
| Top movers across everything | Gainers / losers / volume / turnover across **spot + futures + options**, on any of 21 timeframes | One screen for what's really moving | #876 / #900 |
| Broker-accurate turnover | Prices each minute at its typical price (H+L+C)/3 × volume, not the closing tick alone | Rankings track your broker's turnover far more closely | #899 |
| Per-option moneyness | Shows each option's ITM / ATM / OTM and how many strikes from the money, as one clear label | Read an option chain at a glance | #865 / #869 |
| Full F&O candle history | Futures + options 1-minute history is a first-class, browsable table | Inspect any contract's real history | #861 / #881 |
| Find any contract fast | Symbol → expiry → strike cascading pickers for F&O, symbol → timeframe for spot | Pinpoint the exact contract instead of a giant dropdown | #880 / #897 |

### 🛡️ Theme 3 — "Nothing is ever lost"

| Feature | What it does | Why you care | Ledger |
|---|---|---|---|
| Crash-safe writes | Every file is written-then-atomically-swapped via one shared, tested core | A crash mid-write never corrupts your data | #885 / #890 / #891 |
| No silent data loss | Two downloads sharing a lake can't overwrite each other (cross-process locking) | Run CLI + dashboard pulls at once, safely | #887 |
| One bad file ≠ broken page | A corrupt file skips that one instrument (logged) instead of crashing the screen | Your dashboard never goes down over one bad file | #886 |
| Graceful disk-full halt | If the disk fills mid-download, the pull stops cleanly (no corrupt partial data) — end to end, including the progress ledger | A full disk can never corrupt or runaway-fail your data | #885 / #901 |
| Resume in seconds | Mid-download checkpointing means a crash re-pulls a tiny window, not the whole span | Multi-day downloads survive interruptions | #884 |
| Transient errors retry | Vendor 5xx / empty blips are retried, not treated as "missing forever" | Temporary hiccups don't leave gaps | #883 |
| One bad combo ≠ dead sweep | A single failing strategy combo is skipped + flagged (non-zero exit), the rest finish | A days-long sweep isn't lost to one bad tuple | #904 |

### 📡 Theme 4 — "You always know what's happening"

| Feature | What it does | Why you care | Ledger |
|---|---|---|---|
| Instant phone alerts | A CRITICAL Telegram alert fires when a download halts (disk-full / out-of-memory / quota) — from both CLI and dashboard | You hear about a stopped pull in real time, not hours later | #888 / #889 |

### 🎯 Theme 5 — "The data is right (and honest)"

| Feature | What it does | Why you care | Ledger |
|---|---|---|---|
| "&"-name stocks fixed | M&M, L&TFH, J&KBANK etc. retry an encoded form so their full options chains download | No whole stocks silently missing from F&O data | #898 |
| Tricky symbols resolved | Hyphenated / verbose vendor symbols resolve to the real master symbol | Names like BAJAJ-AUTO / MIDCPNIFTY map correctly | #845 |
| Mapping integrity audits | Proves every option maps to the right stock + lot size; flagged, never faked | Trust that the data lines up | #870 / #873 / #874 / #875 |
| Honest completeness | Late-listed names + missing years marked "partial" instead of faking a full span | The completeness screen never lies to you | #859 / #868 |
| Empty-symbol recovery | A stock whose stored data key drifted (e.g. `NSE-M&M` vs `M&M`) has its real on-disk data recovered under the clean name, never silently dropped (and the drift is flagged for cleanup) | No stock with real data ever reads falsely "empty" | #907 / #911 |
| "Why is X empty?" diagnostic | Pinpoints exactly why a symbol reads empty (a key-format drift), surfaced for a precise fix | Data gaps get diagnosed, not guessed at | #907 |

### 🚀 Theme 6 — "Getting the data is one click"

| Feature | What it does | Why you care | Ledger |
|---|---|---|---|
| One-click full pipeline | One button pulls the whole universe: spot → expiries/contracts → history | Years of clean data without a manual chore | #872 |
| Futures + smart ordering | Futures download enabled, pulled before options, both on a 30-day default window | Right data, right order, no wasted quota | #871 / #905 |
| Minute-true, derived rest | Only 1-minute is fetched; the daily + 19 intraday timeframes are derived from it | One source of truth, 21 timeframes, no drift | #853 |
| Self-tuning rate limit | An adaptive limiter backs off on rejections; ceiling tuned to 5/sec (live-log proven) | Fast downloads that don't get you blocked | #906 |

### 🌙 Theme 7 — "Runs itself overnight — even the login renews itself"

| Feature | What it does | Why you care | Ledger |
|---|---|---|---|
| Self-renewing login (6 AM fix) | The broker's access token resets every day at 06:00. BruteX now **re-mints it automatically** mid-download — thread-safe, exactly once even under a stampede — so the overnight pull keeps going instead of dying at 6 AM | **You sleep, it keeps working.** No manual token rotation at dawn, ever | #909 |
| Visible self-heal in the log | Each automatic renewal prints a `groww.token.reauth` line | You can *see* it healed itself — proof, not a promise | #909 |
| Can't freeze on a bad renewal | Even if the renewal call hangs on a half-dead network socket, a wall-clock deadline turns it into a normal retry instead of freezing the whole download | A flaky network at 6 AM can never lock up your pull | #910 |

> Why this matters most: this is the difference between *"start a download and babysit it"* and
> *"start a download and walk away for days."* The login that used to need a human at dawn now
> renews itself.

---

## 🟡 Current — just shipped this cycle

| Item | What it does | Why you care | Status |
|---|---|---|---|
| **Empty-symbol recovery** | If a stock's stored data key drifted (a legacy `NSE-M&M` vs the clean `M&M`), the board now recovers its real on-disk data under the clean name — keeping the best real data even if a stale fragment coexists — instead of silently hiding it | No stock with real data ever silently reads "empty" | ✅ MERGED (#911) |
| **Self-renewing 6 AM login** | Auto re-mints the daily broker token mid-pull (single-flight, bounded) so the overnight download survives the 06:00 reset | **The headline:** downloads now run unattended for days | ✅ MERGED (#909) |
| **Freeze-proof renewal** | Wraps the token renewal in a wall-clock deadline so a hung network call retries instead of freezing the pool | Covers the rare "what if 6 AM *and* the network is flaky" case | ✅ MERGED (#910) |
| Rate-limit tuning (8→5/sec) | A live-log audit showed the vendor rejects above ~3–5/sec; the cap is lowered so retries aren't wasted probing a wall | Same real throughput, far fewer wasted retries | ✅ MERGED (#906) |
| "Why is M&M empty?" diagnostic | Observe-only check that names the exact on-disk key drift behind an empty symbol — now turned into the recovery FIX above | Turns a mystery gap into a one-line, fixable fact | ✅ MERGED (#907) |

---

## 🔮 Future — planned next (honest roadmap)

| Priority | Plan | What it'll give you | Stage |
|---|---|---|---|
| 🟢 Near | In-place candle refresh | Candle view updates its grid in place on paging (no full rebuild) | fast-follow |
| 🟡 Mid | True O(page) paging | Candle pager reads only the visible page via a write-time manifest | documented |
| 🟡 Mid | Confirm vendor limits | Lock in real rate limits + data boundaries with live probes | probe gate |
| 🔴 Major | Brute-force sweep | Test ~33 billion strategy combinations across all 21 timeframes (~2–4 days on reference hardware) | core mission |
| 🔴 Major | Options execution track | Translate winning index signals into real options trades for execution realism | Track 2 |

**Legend:** 🟢 near-term · 🟡 mid-term · 🔴 major initiative.

---

## 🛡️ The bar every change clears (why you can trust it)

| Gate | Standard |
|---|---|
| ✅ Test coverage | 100% line + 100% branch on every touched module (+ property, golden, stress & mutation tests) |
| ✅ Code review | An independent **hostile reviewer** hunts defects before anything merges |
| ✅ Workspace health | Full-repo verification (255-file type-check + full test suite) on every change |
| ✅ Honesty rule | **No hallucinations, no hype** — unverified things are labelled "unverified" |
| ✅ Speed rule | O(1) / O(page) discipline — "nowhere in the product is ever slow" |
| ✅ Scope safety | The engine's market scope (NIFTY / BANKNIFTY / SENSEX) is never changed silently |

---

_BruteX — Extreme brute-force backtesting engine for Indian index & F&O markets. This scoreboard is
regenerated from the decision ledger; if a row and the ledger ever disagree, the ledger
(`docs/MIGRATION.md`) wins._
