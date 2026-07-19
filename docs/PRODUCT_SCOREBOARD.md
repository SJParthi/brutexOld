# BruteX — Product Scoreboard

> A plain-English, customer-facing view of what's **finished**, what's **shipping now**, and
> what's **planned next**. Every row is real, merged, and verified — generated from the live
> decision ledger (`docs/MIGRATION.md`). A self-contained, shareable HTML one-pager lives next to
> this file at [`docs/product_scoreboard.html`](./product_scoreboard.html) (open in any browser).
>
> _Last refreshed: 2026-06-28 — the plain-English "Finished / Current / Future" themes below cover the
> ledger through entry **#979** (DEC-M4PRO-BENCH-SURFACE-001; this refresh builds on the prior one
> DEC-SCOREBOARD-REFRESH-002 / #918 and the in-between #935 sweep). The number in the last column is the
> **ledger entry** (`docs/MIGRATION.md #NNN`), the authoritative record of each change._
>
> _Assurance-verified 2026-07-18 (DEC-O1-GUARANTEE-VERIFY-001 / #2012) — an independent, evidence-based
> re-verification pass re-ran the quality gates read-only and recorded the executed pass-counts + the
> honest not-yet-green rows in the new **[Independent assurance verification](#-independent-assurance-verification-2026-07-18)**
> section below. It does NOT regenerate the customer themes through the newest ledger — those remain at
> ~#979. Nothing is claimed green that was not executed green (Golden Rule #10)._

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
| Bounded daily-derive reads — building the daily/intraday timeframes after a pull now reads only the months you actually pulled, not years of minute history each time | Pulls stay fast as your history grows to years | #939 |
| Write-cost telemetry + at-scale bench — the disk-write step now reports exactly how long each save takes, with a deep-partition benchmark to prove where the real bottleneck is | Performance work is evidence-led, never guesswork | #937 / #946 |

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
| **See the sweep winners** — a new read-only **`/results`** page lists the ranked brute-force combos: matched conditions by name, net P&L in rupees, full-precision Sharpe / profit-factor / win-rate, with filter + click-to-sort; a completed sweep refreshes it automatically | One screen for *which strategies won* — no CLI needed | #976 / #977 / #978 |
| F&O movers actually populate — the futures / options CHANGE / CHANGE% / OI columns are no longer blank: the metadata pull now resolves the real vendor underlying first, so eligible stocks return their expiries/contracts instead of zero | F&O gainers/losers screens show real numbers, not blanks | #974 |
| F&O timeframes carry open interest — the derived daily + intraday F&O candles now carry the bucket's last open-interest snapshot (never summed), exactly like the 1-minute data | Resampled F&O history keeps OI correct on every timeframe | #954 |

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
| Network blips retry — a momentary DNS / connection failure ("can't resolve api.groww.in") is retried, not treated as a stock that's "gone forever" | A flaky 30-second network hiccup never costs you a whole symbol | #936 |
| One corrupt stored file ≠ broken pull — a corrupt existing data file now quarantines that one symbol instead of crashing the whole pull, sweep load, or movers screen | A single bad file on disk can never take down a multi-day download | #886 / #941 / #950 |
| A pull can't stall on its own locks — internal file-locks now defer-and-retry (like a rate limit) instead of pinning a worker, so the *only* thing that can slow a download is the broker's rate limit | Downloads make steady progress — they never silently wedge | #953 |
| Off-heap sweep dedup — the brute-force engine de-duplicates combos in an on-disk store instead of unbounded RAM, so a giant sweep can't run the 48 GB machine out of memory | Massive sweeps finish instead of crashing on memory | #957 |
| Deterministic, reproducible results — tied leaderboard scores break on a stable key, `-0.0`/`NaN` can't slip into a run's identity hash, and a re-run is bit-identical | The same inputs always give the exact same ranked winners | #960 / #962 / #964 / #967 |
| Engine-wide robustness sweep — the indicators, costs, metrics, orchestrator audit-log, rate-limiter and the shared Rust core all now fail loudly on corrupt input instead of silently propagating bad numbers | Bad data is caught at the door, never quietly poisons a result | #961 / #963 / #968 / #969 / #965 |

### 📡 Theme 4 — "You always know what's happening"

| What it does | Why you care | Proof |
|---|---|---|
| Instant phone alerts — a CRITICAL Telegram alert fires when a download halts (disk-full / out-of-memory / quota), from both CLI and dashboard | You hear about a stopped pull in real time, not hours later | #888 / #889 |
| Pull-health self-watch — a live monitor decides whether a slow download is **our** software holding it back or the broker simply rejecting requests, and it's now wired into every real pull (firing a phone alert the moment it degrades) — and it's now safe across all parallel download workers | When a pull is slow, you know exactly whose fault it is — automatically, not by squinting at a chart | #932 / #935 / #956 |
| Hung-pull watchdog — a heartbeat watchdog declares a wedged background download HUNG, fires a CRITICAL phone alert, and releases the stuck flag so the next pull isn't silently refused | A frozen download tells you in real time — it never just sits there silently | #948 |

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
| Dashboard hardened against XSS — vendor-derived cells and reflected query params are escaped before they touch the page, closing two cross-site-scripting holes on the Movers screen | The dashboard is safe to open, even on untrusted data | #949 |

### 🚀 Theme 6 — "Getting the data is one click (and as fast as the vendor allows)"

| What it does | Why you care | Proof |
|---|---|---|
| One-click full pipeline — one button pulls the whole universe: spot → expiries/contracts → history | Years of clean data without a manual chore | #872 |
| Futures + smart ordering — futures download enabled, pulled before options, both on a 30-day default window | Right data, right order, no wasted quota | #871 / #905 |
| Minute-true, derived rest — only 1-minute is fetched; the daily + 19 intraday timeframes are derived from it | One source of truth, 21 timeframes, no drift | #853 |
| Self-tuning rate limit — an adaptive limiter backs off on rejections and recovers automatically; the per-second ceiling is operator-tuned to 8/sec | Fast downloads that adjust themselves and don't get you blocked | #906 / #922 |
| Actually uses its allowed speed — more download connections so the pull generates real demand toward the rate ceiling instead of leaving most of it idle | Faster downloads from the slack that used to go to waste | #921 |
| Proven not to leave speed on the table — an at-scale test harness fails the build if the pull ever falls short of the rate it's allowed | The download speed is guarded by an automatic safety net, not hope | #929 |
| One button does the whole pull — the cluttered per-stage buttons are replaced by one selection-aware "▶ Pull · full pipeline" that always runs the fixed gated order (spot → futures/options metadata → history), auto-loading instruments when needed | Get all your data with a single click — no stage-by-stage button juggling | #955 |
| Per-segment movers warm-up — the moment a segment (spot, then futures + options) fully succeeds, its movers screen is pre-computed; a segment that didn't fully succeed is never warmed | Movers open instantly after a pull — and never show a half-finished illusion | #951 |

### 🌙 Theme 7 — "Runs itself overnight — even the login renews itself"

| What it does | Why you care | Proof |
|---|---|---|
| Self-renewing login (6 AM fix) — the broker's access token resets daily at 06:00; BruteX **RE-READS the Lambda-minted shared token** mid-download and adopts it — the Lambda mints, BruteX never does — thread-safe, exactly once even under a stampede | **You sleep, it keeps working.** No manual token rotation at dawn, ever | #909/#1072 |
| Visible self-heal in the log — each automatic renewal prints a `groww.token.reauth` then a `groww.token.reread` line | You can *see* it healed itself — proof, not a promise | #909/#1072 |
| Can't freeze on a bad renewal — even if the renewal call hangs on a half-dead network socket, a wall-clock deadline turns it into a normal retry | A flaky network at 6 AM can never lock up your pull | #910 |

> Why this matters most: this is the difference between *"start a download and babysit it"* and
> *"start a download and walk away for days."* The login that used to need a human at dawn now
> renews itself.

### 🔬 Theme 8 — "Proven, measured, and ready for the big sweep"

| What it does | Why you care | Proof |
|---|---|---|
| Engine proven O(1) at scale — new benchmarks prove the sweep's hot paths (audit append, per-mask driver, trial-stats write) stay constant-time no matter how big the grid gets | The brute-force engine is built to scale to billions of trials | #971 / #972 / #973 |
| 7-test-type rigor on the rule engine — the 52-condition evaluator gains benchmark, stress, golden-bit-identity, property and boundary tests | The logic that decides every signal is exhaustively pinned | #970 |
| Extra metric robustness — a second-round hardening pass on the statistical-validity stage kills mutation survivors and consolidates cross-engine invariant tests | The Sharpe / DSR / significance maths is double-checked | #975 |
| Calendar hardened — the NSE/BSE trading calendar's timezone, session-boundary, leap-year and half-day rules are pinned with golden + property tests | Trading-hour and holiday logic is trustworthy to the minute | #958 / #959 |
| Honest CI gates — the build's quality gates are de-conflated into honestly-named jobs, and a new module shipping with no test now fails the build | The "every change is fully tested" promise is enforced, not assumed | #966 |
| M4 bench numbers in the logs — the reference-hardware performance numbers are surfaced in CI logs, and the type-check is green on the verify machine | Performance claims are visible and checked on the real reference machine | #979 |

---

## 🟡 Current — in flight right now (open PRs, not yet merged)

These are **real open pull requests** — not yet merged, so not counted as Finished above.

| Item | What it is | Why it matters | Status |
|---|---|---|---|
| **Batched-coalesce write consumer** | Group all pending disk-writes for one month into a single save so the download stops idling on its own writes (the "write-fix") | Default-OFF until the M4 A/B proof lands; the CI bench showed ~2× but it ships dormant until proven on reference hardware (Golden Rule #10) | 🟡 draft PR (#943) — not merged, claims ledger #980 |
| **CI merge-safety hardening** | Kills a cosmetic post-merge "red" and gates auto-merge on the live, complete check rollup so a PR can never merge while anything is red | Process safety — no `brutex/` engine change | 🟡 draft PR (#944) |

> Note: the prior cycle's "Current" items have since landed — the EAP feedback rounds merged (#925 / #931),
> and the Lever-1 fetch/write overlap merged **shipped-dormant as an honest NO-GO** (the benchmark refuted a
> throughput gain under Python's lock — #940). The batched-coalesce writer (#943) above is the follow-up
> that *did* show a win on the CI bench.

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
| 🔴 Major | Brute-force sweep (run it for real) | Run the ~33-billion-combo sweep across CPU cores — ~2–4 days on reference hardware, bit-identical to serial. The engine is now O(1)-proven + hardened (#971–#973) and its winners already have a `/results` page (#976) | design-locked (ADR-0011), engine hardened, full production run deferred |
| 🔴 Major | Options execution track | Translate winning index signals into real options trades for execution realism | Track 2 |
| 🟡 Mid | Faster disk-writes (proven) | Land the batched-coalesce writer once the M4 A/B proof confirms the CI bench's ~2× | open draft, proof-gated (#943) |
| 🟡 Mid | In-place dashboard nav | Pages swap content in place instead of fully reloading — the last big source of page lag | documented follow-up (#933) |
| 🟡 Mid | True O(page) paging | Candle/discovery reads only the visible page via a write-time lake manifest | documented follow-up (#896 / #916) |
| 🟡 Mid | Engine → F&O stocks | Expand the brute-force engine beyond the 3 indices (the ~212-stock lake is reference-only today) | needs a scope decision (#200) |
| 🟢 Near | Daily health cron | A dawn `brutex doctor` run posts an automated health report to Telegram | deferred follow-up |

**Legend:** 🟢 near-term · 🟡 mid-term · 🔴 major initiative.

---

## 🔎 Independent assurance verification (2026-07-18)

A deep, parallel, evidence-based assurance pass re-ran the quality gates **read-only** on a purpose-built
Python-3.12 environment (both Rust hot-kernel wheels loaded) at the mega-club integration tip — detached
HEAD `c220870641`, PR #1621, one CI-workflow/ledger/one-unit-test commit behind `origin/main`
(`415e863ec`, PR #1631 — a delta affecting no executed gate).
Every number below is an **executed** pass-count — nothing is asserted that was not run.
Two things are recorded honestly: what is **✅ verified green**, and what is **🟡 not-yet-green** (weekly /
self-hosted gates that this environment cannot run — never a fabricated all-green sheet).

### ✅ Verified green — executed this pass

| Area | What was run | Result |
|---|---|---|
| Static analysis + formatting | `ruff check` + `ruff format --check` on `brutex` + `tests` | **All checks passed · 1785 files already formatted** |
| Type safety | `mypy --strict brutex` · `pyright brutex` | **476 files clean, 0 issues · 0 errors** |
| Security | `bandit -r brutex -l` | **0 issues** at every severity/confidence |
| Reproducibility (blake3 run identity) | frozen run-id / content-hash goldens + snapshot-hash mutation/determinism | **golden 556 passed** (4 optional-crate skips) · the frozen `_GOLDEN_RUN_ID` reproduces; the data-snapshot hash re-keys on **any** single bar change and is deterministic |
| No look-ahead | feed-boundary guard + P-06 index barrier + sweep suffix-independence | **68 passed** + the P-06 property teeth (a manual `verify-lookahead` CLI recheck needs a populated lake slice — its teeth are otherwise proven) |
| Audit hash-chain | tamper-detection + valid-chain verify | **374 passed** incl. 6 named break-detection tests (a broken chain FAILS as designed) |
| O(1) speed (robust shared-CI tier) | `pytest tests/bench` (ratio ceilings) | **389 passed / 12 skipped (self-hosted-only) / 0 failed** — corroborated by a second run **286 passed / 0 failed**; anti-illusion honesty confirmed, **no bare-O(1) claim hides an O(files) read** |
| Edge-case resilience (7 classes) | poison-lake reads, heal/quarantine, ingest gates, engine-sweep resilience, dashboard-read degrades, write-halt/OOM, deadlines | **~1,375 assertions, 0 failed** — **NO genuine undocumented gap** (the one boundary asymmetry found is already ledger-disclosed and consciously out of scope) |
| Property / smoke / redteam / stress | the remaining test types | **property 550 passed** (26 stub-skips) · **smoke 28 passed** · **redteam 175 passed** · **stress (non-soak) 35 passed** |
| Whole-tree 100% coverage gate | the CI-exact unit+coverage gate re-run locally in full + CI run 29626883403's "Unit tests + coverage (100% gate)" job (the newest main ci.yml run, main @ `415e863ec`) | **✅ 100.00% line+branch MEASURED** — local re-run TOTAL **63,689 stmts / 0 miss · 16,626 branches / 0 partial**, **27,587 passed / 0 failed** (36 skipped, 1:53:16), "Required test coverage of 100% reached"; CI coverage job **conclusion=success**, whole run success |

### 🟡 Honestly not-yet-green — NOT claimed

| Area | Honest state |
|---|---|
| **Mutation gate (weekly)** | The scheduled `mutation.yml` run named as the completion proof (2026-07-13) **FAILED**; **0 green scheduled runs on record** across the last ~30 runs. Standards row 3 stays **🟡 PARTIAL** until a green scheduled run lands (DEC-MUTATION-RUNTIME-003 / #1597). Not claimed green. |
| **Multi-hour soak** | Runs on the **self-hosted weekly** tier (`soak-selfhosted.yml`, DEC-SOAK-REAL-TIER-001 / #1580) — not runnable in this environment, **not re-measured** here. |
| **Strict 1.1× M4-Pro O(1) tier + absolute throughput floors** | **Self-hosted-only** (`bench-m4pro.yml`), post-merge + non-blocking by design (#1753) — **not re-measured** here; the robust shared-CI ratio tier above is the merge-blocking guarantee. |

> Verdict: the tree's static/type/security, reproducibility, look-ahead, audit-chain, O(1)-honesty,
> edge-case resilience and whole-tree 100% coverage are **confirmed** with executed evidence; the
> mutation / soak / strict-M4Pro rows are **honestly open**. Documentation of a verified state — not a
> fabricated all-green sheet.

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
