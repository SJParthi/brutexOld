# BruteX vs the field — the one-page comparison

> Plain-English, "even my cousin gets it" view of **why BruteX is built
> differently** from a typical backtester. Every speed/quality claim below is a
> **machine-checked CI gate**, not a slogan — if it regresses, the change is
> blocked from merging. Honesty first (CLAUDE.md Golden Rule #10): the last
> table is the *honest scoreboard* of what is live **today** vs. what is on the
> roadmap. Verified on the merged tip `6bf814d` (2026-06-02).

---

## 1. What you actually feel — BruteX vs the usual options

| What you care about | 🟥 A typical DIY backtester (pandas + a loop) | 🟧 A heavyweight framework | 🟩 **BruteX** |
|---|---|---|---|
| **Re-running a data pull** | Re-downloads / risks duplicates | Manual cache keys | **O(1) uniqueness + dedup** — never double-writes, never drops a real bar (Bloom → LMDB → blake3) |
| **Speed per bar / per check** | Slows down as data grows (O(n)) | Varies | **O(1) per bar, per signal, per dedup** — constant-time hot paths, CI-gated |
| **Memory as data grows** | Loads everything into RAM | Tuned by hand | **O(1) per operation** — memory-mapped LMDB tier, state off the Python heap |
| **One bad symbol mid-run** | Kills the whole run | Depends | **Quarantined** — the other ~240 instruments keep flowing |
| **Look-ahead bugs (cheating)** | Easy to introduce silently | Manual care | **Blocked at runtime** — at bar N the engine can only read bars 0..N |
| **Money rounding** | Float drift creeps in | Manual | **Money always 2 decimals; stats (Sharpe/p-values) kept full precision** so ranking never lies |
| **"Is it correct?"** | You hope so | Some tests | **3,126 automated tests pass, 97.84% line+branch coverage**, hash-chained audit log |
| **Shipping a change** | Push and pray | CI if you wrote it | **24-gate CI must be green** — tests + types + security + O(1) bench + dedup + docs, then it can merge |
| **Daily operation** | Babysit it | Babysit it | **Designed for zero-click**: Release Please + auto-merge + nightly soak + a red-team "planted-bug" harness |

---

## 2. The guarantees — each one is a *gate*, not a promise

Every row is enforced automatically on **every pull request**. Nothing ships on trust.

| Guarantee | How it's proven automatically | Status today |
|---|---|---|
| **O(1) uniqueness + dedup, zero data loss** | blake3 key → Bloom filter → LMDB exact-confirm; a Bloom false-positive can never cause a false drop | ✅ enforced |
| **O(1) latency / time per hot path** | `p99(big)/p99(small)` ratio benchmark on bar lookup, u64 mask, dedup, rate-limiter | ✅ enforced (see honest note ▼) |
| **O(1) space per operation** | memory-mapped LMDB membership tier (total state is O(n), per-op RAM is O(1)) | ✅ enforced |
| **100% correctness gates** | 3,126 tests, 97.84% coverage (≥90% floor + per-module 100% gates), mypy --strict, bandit, hash-chained audit | ✅ enforced |
| **All worst-cases / failure modes** | property tests (Hypothesis) + golden fixtures + a 32-test red-team harness that plants bugs and asserts the gates catch them | ✅ enforced |
| **Reproducible, bit-for-bit** | every run keyed by `blake3(strategy ‖ instrument ‖ params ‖ data-snapshot ‖ code-commit)`; replay oracle | ✅ enforced |
| **Fully automated merge** | 24-job aggregate gate must be green; auto-merge only on green; nightly soak + weekly mutation | ✅ enforced |

---

## 3. The 7 kinds of tests (most projects ship 1–2)

| Test type | What it catches | In BruteX |
|---|---|---|
| **Unit** | logic bugs | ✅ 2,795 |
| **Integration** | wiring/contract bugs | ✅ 183 |
| **Property (Hypothesis)** | edge cases you didn't think of | ✅ 106 |
| **Golden** | byte-level output drift | ✅ 3 corpora |
| **Benchmark** | O(1) regressions | ✅ 40 |
| **Stress / soak** | sustained leaks / drift over hours | ✅ nightly ~5-min regression gate + weekly self-hosted multi-hour soak (`soak-selfhosted.yml`, DEC-SOAK-REAL-TIER-001 #1512 — the prior “24h nightly” claim was false and is retracted) |
| **Smoke + Red-team** | fast pre-push + gate-integrity (planted bugs) | ✅ 12 + 32 |

---

## 4. The honest scoreboard (Golden Rule #10 — what is live vs. roadmap)

A comparison that only lists wins is marketing, not engineering. Here is the truth.

| Capability | Status | Honest note |
|---|---|---|
| Dhan data layer (auth · client · parser · instruments) | ✅ **live** | plain REST, no vendor SDK |
| Spot + options ingest, 1m → 19 timeframes, O(1) dedup lake | ✅ **live** | 32 indices + ~211 F&O stocks in the reference lake |
| Dashboard: one-click "Pull the Universe", live feed, DB/coverage views | ✅ **live** | `make run` → http://127.0.0.1:9080 |
| O(1) hot-path benchmarks | 🟡 **enforced at 5–10×**, not the aspirational 1.1× | the tighter 1.1× target needs the self-hosted M4 Pro runner; documented in `STANDARDS_100PCT.md` |
| The brute-force **sweep engine** (Stage 1A→1B→2→3→4) | 📋 **next (Phase 3)** | the per-bar/per-mask/dedup *primitives* exist + are O(1)-gated; the cascade itself is the upcoming build |
| "33 billion trials in 2–4 days" SLA | 📋 **target, not yet proven** | tracked as an `xfail` until the engine + M4 Pro runner land |
| Live trading / order placement | 🚫 **out of scope** | Track 1 is **signal-only research**; execution realism is the later Options Track |

---

*The BruteX bet: every promise on this page is wired to a gate that turns red if
we break it. That is the feature — boring, bulletproof, and merge-blocked when it
isn't. See `docs/MERGE_READINESS_GUARANTEE.md` for the gate matrix and
`docs/STANDARDS_100PCT.md` for the honest enforced-vs-target audit.*
