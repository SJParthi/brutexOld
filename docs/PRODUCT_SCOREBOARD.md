# BruteX — Product Scoreboard

> A plain-English, customer-facing view of what's **finished**, what's **shipping now**, and
> what's **planned next**. Every row is real, merged, and verified — generated from the live
> decision ledger (`docs/MIGRATION.md`). A self-contained, shareable HTML one-pager lives next to
> this file at [`docs/product_scoreboard.html`](./product_scoreboard.html) (open in any browser).
>
> _Last refreshed: 2026-06-27 — now covering the ledger through entry **#935**
> (DEC-PULL-HEALTH-WIRING-001; this refresh builds on the prior one DEC-SCOREBOARD-REFRESH-002 /
> #918). The number in the last column is the **ledger entry** (`docs/MIGRATION.md #NNN`), the
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

Each row reads left-to-right: **What it does** (plain English) → **Why you care** (the benefit) →
**Proof** (the `docs/MIGRATION.md` ledger entry).

### ⚡ Theme 1 — "Nothing is ever slow"

| What it does | Why you care | Proof |
|---|---|---|
| Instant view switching — re-scanned the whole multi-year data tree on every click → cached instant handoff | No waiting when you change candle views | #896 |
| Instant Universe page — waited 30–160 s on a slow external site every visit → served from cache | The ~750-name list opens immediately | #892 |
| Instant Completeness page — re-scanned the whole lake each load → O(1) on repeat | "How complete is my data?" answers instantly | #895 |
| Fast "Fetch Windows" — counted 340,000+ rows in Python per click → database-paginated | Stays snappy no matter how big history grows | #893 |
| Every data tab paginated — the last whole-table reads → page-at-a-time database queries | Browsing huge tables never stalls | #863 / #864 |
| Cached page totals — re-counted millions of rows on every paging click → memoised | Paging the candle browser is instant | #867 |
| Instant Movers tab/section switch — re-walked the whole 750-name lake on every flip → walked once per pull, shared across tabs | Spot ↔ Futures ↔ Options switches are instant | #916 |
| Dashboard never freezes during a download — Coverage / Completeness scans now self-throttle, so a running download keeps its full speed instead of dropping by half | The dashboard stays usable even mid-download | #920 |
| Faster, byte-identical file writes — a smarter way of merging each downloaded month makes writes quicker without changing a single number in your data | Downloads start getting faster, results stay identical | #927 |
| Instant nav on the last three pages — Coverage / Completeness / Mapping pages answer a repeat refresh instantly instead of re-projecting the whole lake each time | Section-to-section navigation is snappy | #933 |

### 📊 Theme 2 — "See what's actually moving"

| What it does | Why you care | Proof |
|---|---|---|
| Top movers across everything — gainers / losers / volume / turnover across **spot + futures + options**, on any of 21 timeframes | One screen for what's really moving | #876 / #900 |
| Broker-accurate turnover — prices each minute at its typical price (H+L+C)/3 × volume, not the closing tick alone | Rankings track your broker's turnover far more closely | #899 |
| Per-option moneyness — shows each option's ITM / ATM / OTM and how many strikes from the money, as one clear label | Read an option chain at a glance | #865 / #869 |
| Full F&O candle history — futures + options 1-minute history is a first-class, browsable table | Inspect any contract's real history | #861 / #881 |
| Find any contract fast — symbol → expiry → strike cascading pickers for F&O, symbol → timeframe for spot | Pinpoint the exact contract instead of a giant dropdown | #880 / #897 |
| Each tab stays its own market — spot shows only spot; futures/options gainers-losers populate even without a daily partition | Clean, accurate tabs — no cross-asset pollution, no empty movers | #915 |
| Pick your futures expiry — Near / Next / Far filters to that serial-month future per underlying (front / 2nd / 3rd), each cached independently | Focus on the liquid expiry — no stale rows, no spreadsheet juggling | #917 |

### 🛡️ Theme 3 — "Nothing is ever lost"

| What it does | Why you care | Proof |
|---|---|---|
| Crash-safe writes — every file is written-then-atomically-swapped via one shared, tested core | A crash mid-write never corrupts your data | #885 / #890 / #891 |
| Crash-proof audit log — every step's tamper-evident record is flushed all the way to disk before it counts as done, and only ever writes clean, standard data | Even a sudden power-cut can't lose or corrupt your audit trail | #926 |
| No silent data loss — two downloads sharing a lake can't overwrite each other (cross-process locking) | Run CLI + dashboard pulls at once, safely | #887 |
| One bad file ≠ broken page — a corrupt file skips that one instrument (logged) instead of crashing the screen | Your dashboard never goes down over one bad file | #886 |
| Graceful disk-full halt — if the disk fills mid-download, the pull stops cleanly (no corrupt partial data), end to end | A full disk can never corrupt or runaway-fail your data | #885 / #901 |
| Resume in seconds — mid-download checkpointing means a crash re-pulls a tiny window, not the whole span | Multi-day downloads survive interruptions | #884 |
| Transient errors retry — vendor 5xx / empty blips are retried, not treated as "missing forever" | Temporary hiccups don't leave gaps | #883 |
| Junk bars never stored — a vendor candle mixing a zero price with real prices is skipped, not stored as a fabricated zero | Your backtest runs on real prices, never corrupted ones | #912 |
| Rate cap can't be overshot — the 500/min limiter reserves its slot atomically | Your vendor limits are truly enforced, even at full parallelism | #913 |
| One bad combo ≠ dead sweep — a single failing strategy combo is skipped + flagged, the rest finish | A days-long sweep isn't lost to one bad tuple | #904 |

### 📡 Theme 4 — "You always know what's happening"

| What it does | Why you care | Proof |
|---|---|---|
| Instant phone alerts — a CRITICAL Telegram alert fires when a download halts (disk-full / out-of-memory / quota), from both CLI and dashboard | You hear about a stopped pull in real time, not hours later | #888 / #889 |
| Pull-health self-watch — a live monitor decides whether a slow download is **our** software holding it back or the broker simply rejecting requests, and it's now wired into every real pull (firing a phone alert the moment it degrades) | When a pull is slow, you know exactly whose fault it is — automatically, not by squinting at a chart | #932 / #935 |

### 🎯 Theme 5 — "The data is right (and honest)"

| What it does | Why you care | Proof |
|---|---|---|
| Provable raw-data integrity — every download fingerprints **exactly what the broker sent** (before any processing) and stores that proof, on every real spot / futures / option pull, so any later tampering or drift of a re-fetched window is caught by a hash mismatch | Mathematical proof your stored data derives from exactly what the broker sent | #934 |
| Whole-stock "vanish" flag — if a whole F&O stock comes back 100% empty after every retry (the BAJAJ-AUTO / M&M failure mode), it's flagged loudly instead of blending into a normal no-data gap | A whole stock can never silently disappear from your data | #919 |
| Honest "Fetches" column — the download log's retry count is relabelled "EMPTY-retries" so a failed window can't be mis-read as "barely tried" when it actually retried hard behind the scenes | The status screen tells the truth about what really happened | #924 |
| "&"-name stocks fixed — M&M, L&TFH, J&KBANK etc. retry an encoded form so their full options chains download | No whole stocks silently missing from F&O data | #898 |
| Tricky symbols resolved — hyphenated / verbose vendor symbols resolve to the real master symbol | Names like BAJAJ-AUTO / MIDCPNIFTY map correctly | #845 |
| Mapping integrity audits — proves every option maps to the right stock + lot size; flagged, never faked | Trust that the data lines up | #870 / #873 / #874 / #875 |
| Honest completeness — late-listed names + missing years marked "partial" instead of faking a full span | The completeness screen never lies to you | #859 / #868 |
| Empty-symbol recovery — a stock whose stored data key drifted has its real on-disk data recovered under the clean name, never silently dropped | No stock with real data ever reads falsely "empty" | #907 / #911 |

### 🚀 Theme 6 — "Getting the data is one click (and as fast as the vendor allows)"

| What it does | Why you care | Proof |
|---|---|---|
| One-click full pipeline — one button pulls the whole universe: spot → expiries/contracts → history | Years of clean data without a manual chore | #872 |
| Futures + smart ordering — futures download enabled, pulled before options, both on a 30-day default window | Right data, right order, no wasted quota | #871 / #905 |
| Minute-true, derived rest — only 1-minute is fetched; the daily + 19 intraday timeframes are derived from it | One source of truth, 21 timeframes, no drift | #853 |
| Self-tuning rate limit — an adaptive limiter backs off on rejections and recovers automatically; the per-second ceiling is operator-tuned to 8/sec | Fast downloads that adjust themselves and don't get you blocked | #906 / #922 |
| Actually uses its allowed speed — more download connections so the pull generates real demand toward the rate ceiling instead of leaving most of it idle | Faster downloads from the slack that used to go to waste | #921 |
| Proven not to leave speed on the table — an at-scale test harness fails the build if the pull ever falls short of the rate it's allowed | The download speed is guarded by an automatic safety net, not hope | #929 |

### 🌙 Theme 7 — "Runs itself overnight — even the login renews itself"

| What it does | Why you care | Proof |
|---|---|---|
| Self-renewing login (6 AM fix) — the broker's access token resets daily at 06:00; BruteX **re-mints it automatically** mid-download — thread-safe, exactly once even under a stampede | **You sleep, it keeps working.** No manual token rotation at dawn, ever | #909 |
| Visible self-heal in the log — each automatic renewal prints a `groww.token.reauth` line | You can *see* it healed itself — proof, not a promise | #909 |
| Can't freeze on a bad renewal — even if the renewal call hangs on a half-dead network socket, a wall-clock deadline turns it into a normal retry | A flaky network at 6 AM can never lock up your pull | #910 |

> Why this matters most: this is the difference between *"start a download and babysit it"* and
> *"start a download and walk away for days."* The login that used to need a human at dawn now
> renews itself.

---

## 🟡 Current — in flight right now (open PRs, not yet merged)

These are real open pull requests. They are **docs / process** work — no `brutex/` engine change.

| Item | What it is | Why it matters | Status |
|---|---|---|---|
| **EAP feedback — round 2** | An evidence-gated feedback dossier for the Claude Code Early-Access Program — every finding backed by a reproducible proof | Improves how the product itself is built (meta, not a product feature) | 🟡 open PR (#905) |
| **Consolidated EAP dossier** | One shared feedback file so the BruteX + TickVault repos can be submitted together | Same meta-feedback track as above | 🟡 draft PR (#889) |
| **Fetch/write overlap (Lever 1)** | An experiment to overlap downloading and disk-writing for higher throughput | Kept **default-off** — a real benchmark showed it doesn't help under Python's lock, so it's honestly **NO-GO** and shelved, not shipped | 🟡 draft PR (#886) — NO-GO, not merged |

---

## 🔜 Up next — being built or designed now (not yet shipped)

| Item | What it'll do | Why you'll care | Status |
|---|---|---|---|
| **🔬 Live vendor probe** | A ready-to-run A/B test of a suspect symbol vs a known-good one against the live vendor | Confirms whether missing data is *recoverable* or genuinely absent | recipe delivered (needs creds) |
| **📦 Verbatim raw-payload archive** | Store the full raw broker response (not just its fingerprint) so any past window can be replayed offline | Even stronger, end-to-end proof your data is exactly what the broker sent | designed, gated (extends #934) |
| **🩺 CLI pull-health monitor** | Extend the now-live pull-health watch to the command-line download path too | The slowdown detector becomes always-on everywhere, not just the dashboard | flagged follow-up (#935) |

---

## 🔮 Future — planned next (honest roadmap, not yet built)

| Priority | Plan | What it'll give you | Stage |
|---|---|---|---|
| 🔴 Major | Brute-force sweep (build) | Run the ~33-billion-combo sweep across CPU cores — ~2–4 days on reference hardware, bit-identical to serial | design-locked (ADR-0011), build deferred |
| 🔴 Major | Options execution track | Translate winning index signals into real options trades for execution realism | Track 2 |
| 🟡 Mid | In-place dashboard nav | Pages swap content in place instead of fully reloading — the last big source of page lag | documented follow-up (#933) |
| 🟡 Mid | True O(page) paging | Candle/discovery reads only the visible page via a write-time lake manifest | documented follow-up (#896 / #916) |
| 🟡 Mid | Engine → F&O stocks | Expand the brute-force engine beyond the 3 indices (the ~212-stock lake is reference-only today) | needs a scope decision (#200) |
| 🟢 Near | Daily health cron | A dawn `brutex doctor` run posts an automated health report to Telegram | deferred follow-up |

**Legend:** 🟢 near-term · 🟡 mid-term · 🔴 major initiative.

---

## 🛡️ The bar every change clears (why you can trust it)

| Gate | Standard |
|---|---|
| ✅ Test coverage | 100% line + 100% branch on every touched module (+ property, golden, stress & mutation tests) |
| ✅ Code review | An independent **hostile reviewer** hunts defects before anything merges |
| ✅ Workspace health | Full-repo verification (250+-file type-check + full test suite) on every change |
| ✅ Honesty rule | **No hallucinations, no hype** — unverified things are labelled "unverified" |
| ✅ Speed rule | O(1) / O(page) discipline — "nowhere in the product is ever slow" |
| ✅ Scope safety | The engine's market scope (NIFTY / BANKNIFTY / SENSEX) is never changed silently |

---

_BruteX — Extreme brute-force backtesting engine for Indian index & F&O markets. This scoreboard is
regenerated from the decision ledger; if a row and the ledger ever disagree, the ledger
(`docs/MIGRATION.md`) wins._
