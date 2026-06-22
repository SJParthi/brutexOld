# BruteX — Product Scoreboard

> A plain-English, customer-facing view of what's **finished**, what's **shipping now**, and
> what's **planned next**. Every row is real, merged, and verified — generated from the live
> decision ledger (`docs/MIGRATION.md`). A self-contained, shareable HTML one-pager lives next to
> this file at [`docs/product_scoreboard.html`](./product_scoreboard.html) (open in any browser).
>
> _Last refreshed: 2026-06-22 (DEC-PRODUCT-SCOREBOARD-001, MIGRATION.md #902)._

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
| Test coverage on every change | **100% line + 100% branch** |
| History depth | **2020 → today**, minute-by-minute |

> Scope note (honest): the brute-force **engine** trades the 3 indices (NIFTY / BANKNIFTY /
> SENSEX); the ~750-name lake is reference data for future expansion. Nothing in the engine scope
> is ever changed silently.

---

## ✅ Finished & Live — already merged into the product

### ⚡ Theme 1 — "Nothing is ever slow"

| Feature | Before → After | Why you care | PR |
|---|---|---|---|
| Instant view switching | Re-scanned the whole multi-year data tree on every click → cached instant handoff | No waiting when you change candle views | #855 |
| Instant Universe page | Waited 30–160 s on a slow external site every visit → served from cache | The list of ~750 names opens immediately | #847 |
| Instant Completeness page | Re-scanned the whole lake each load → O(1) on repeat | "How complete is my data?" answers instantly | #854 |
| Fast "Fetch Windows" | Counted 340,000+ rows in Python per click → database-paginated | Stays snappy no matter how big history grows | #850 |
| Clean symbol picker | One unusable 750-item dropdown → tidy symbol → timeframe menu | You can actually find what you want | #856 |

### 📊 Theme 2 — "See what's actually moving"

| Feature | What it does | Why you care | PR |
|---|---|---|---|
| "Top Value" on Futures | Ranks futures by money traded (turnover), not just contract count — now across spot + options + futures | Follow the real money, full parity across asset types | #859 |
| Broker-accurate turnover | Prices each minute at its typical price (H+L+C)/3 × volume instead of the closing tick alone | Rankings track your broker's turnover far more closely | #858 |

### 🛡️ Theme 3 — "Nothing is ever lost"

| Feature | What it does | Why you care | PR |
|---|---|---|---|
| Crash-safe writes | Every file is written-then-atomically-swapped via one shared tested core | A crash mid-write never corrupts your data | #845 / #846 |
| No silent data loss | Two downloads sharing a lake can't overwrite each other (cross-process locking) | Run CLI + dashboard pulls at once, safely | #842 |
| One bad file ≠ broken page | A corrupt file skips that one instrument (logged) instead of crashing the screen | Your dashboard never goes down over one bad file | #841-era |
| Graceful disk-full halt | If the disk fills mid-download, the pull stops cleanly + alerts you — no corrupt partial data | A full disk can never corrupt or runaway-fail your data | #843 / #860 |

### 📡 Theme 4 — "You always know what's happening"

| Feature | What it does | Why you care | PR |
|---|---|---|---|
| Instant phone alerts | A CRITICAL Telegram alert fires when a download halts (disk-full / out-of-memory / quota) — from CLI and dashboard | You hear about a stopped pull in real time, not hours later | #843 / #844 |

### 🎯 Theme 5 — "The data is right"

| Feature | What it does | Why you care | PR |
|---|---|---|---|
| "&"-name stocks fixed | M&M, L&TFH, J&KBANK etc. now retry an encoded form so their full options chains download | No whole stocks silently missing from F&O data | #857 |
| Smarter default downloads | Futures default to 3 months, options to 30 days | Stops wasting time/quota on illiquid early option bars | #853 |
| Honest completeness | Late-listed names + missing years marked "partial" instead of faking a full span | The completeness screen never lies to you | #813 / #822 |

---

## 🚀 Current — just shipped / in motion

| Item | What it does | Why you care | Status |
|---|---|---|---|
| Disk-full ledger halt | The download progress-ledger — the last file not protected against a full disk — now halts the pull cleanly + texts you instead of crashing | Completes the "nothing is ever lost" guarantee end-to-end | ✅ MERGED (#860) |

---

## 🔮 Future — planned next (honest roadmap)

| Priority | Plan | What it'll give you | Stage |
|---|---|---|---|
| 🟢 Near | In-place candle refresh | Candle view updates its grid in place on tab-switch (no full rebuild) | fast-follow |
| 🟢 Near | Shared turnover reader | Options reuses the one shared turnover reader — less duplicate work | cleanup |
| 🟢 Near | Engine poison-combo isolation | One bad strategy combo is quarantined + logged instead of aborting the whole sweep | scoped |
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
