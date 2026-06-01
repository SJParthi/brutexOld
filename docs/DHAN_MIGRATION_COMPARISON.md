# BruteX on Dhan — the easy comparison

> One-page, plain-English view of the Groww → Dhan migration: what changes, why
> it's better, and the speed guarantees that run automatically on every change.
> (Status as of the live migration — see the scoreboard at the bottom.)

---

## 1. Why it matters — what a user actually feels

| What you care about | 🟥 Before (Groww) | 🟩 After (Dhan) |
|---|---|---|
| **Daily setup** | Re-login + re-approve **every day** (breaks automation) | Token auto-renews → **runs untouched, zero clicks/day** |
| **Market coverage** | Piecemeal, per-contract | **Every** NSE index + BSE SENSEX + **every** F&O-eligible single-stock spot, **and every option** (CE + PE, with strike + spot + IV) |
| **Timeframes** | Fetch each one separately | Pull **1-minute once** → the other **19** intraday TFs auto-derived (2m…15m, 30m, 1h, 2h, 3h, 4h) |
| **Trading hours** | Mixed in pre/post-market noise | **Only 09:15 → 15:30 IST** — never any off-session data |
| **Prices** | Raw, inconsistent | **Money always 2 decimals**; Sharpe / IV / % kept **full precision** so strategy ranking never lies |
| **Re-running a pull** | Risk of duplicates | **O(1) dedup — never double-writes, never drops a real bar** |
| **One bad symbol** | Could kill the whole run | **Quarantined** — the other ~240 instruments keep flowing |

---

## 2. The speed guarantee — a *gate*, not a promise

Every guarantee below is **checked automatically on every single code push** — if it
regresses, the change is blocked. Nothing ships on trust.

| Layer | Guarantee | How it's proven (automatically, real-time) |
|---|---|---|
| **Database / lake** | O(1) uniqueness + dedup · **zero data loss** | blake3 key → Bloom filter → LMDB exact-confirm; latency-ratio gate `< 5×` |
| **Backend** | O(1) latency + O(1) time per bar | per-window O(1); O(1) NFR benchmark gate |
| **Space** | O(1) per operation (off the heap) | memory-mapped LMDB tier |
| **Correctness** | money 2dp · metrics full-precision · idempotent | read-back assertions + 100% line+branch coverage |
| **Every change** | all worst-cases, fully automated | 100% tests + mutation + **4-AI hostile review** + **auto-merge only when green** |

---

## 3. Live migration scoreboard

| Phase | What it delivers | Status |
|---|---|---|
| Dhan data layer (auth · client · parser · instruments) | the foundation | ✅ merged |
| Spot ingest · session filter · prev-close % · 09:15 gap % | clean spot data | ✅ merged |
| Expired-options + **O(1) dedup + LMDB + Parquet table** | the options lake | ✅ merged |
| Resilient multi-instrument orchestrator | one-bad-symbol safety | ✅ merged |
| **Step A** — derive 19 intraday TFs from 1m | the timeframe rule | ✅ merged |
| **Step B** — universe-ingest (refresh → pull) entry point | the single entry | ✅ merged |
| **Flip · prereq** — Dhan credentials gate (`dhan_creds_present`) | unblocks the rewire | ✅ merged |
| **Flip · prereq** — synthetic dry-run source + adapter (offline, zero-creds) | the dry-run proof | ✅ merged |
| **Flip · cutover 1/3** — dashboard instrument loader → Dhan | the universe browser | ✅ merged |
| **Flip · cutover 2/3** — dashboard "⤓ Pull" → Dhan model | the pull button | ⏳ merging |
| **Flip · cutover 3/3** — rewire the CLI to Dhan | command-line ingest | 🔜 next |
| **Flip · final** — delete Groww (code + deps + docs) | Groww fully gone | 🔜 |

**Honest note:** Groww is **still present** until the final phase — deliberately last,
so the switch lands on a fully-built, fully-proven Dhan stack with nothing half-done.

---

*Built the boring-but-bulletproof way: every piece ships only after 100% test coverage,
a 4-agent hostile code review, and an automated green-CI gate — then it auto-merges itself.*
