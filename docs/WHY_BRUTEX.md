# Why BruteX — in plain English

> A one-page, **non-technical** guarantee scorecard. No jargon. If you can
> read a restaurant menu, you can read this.
>
> **Honesty rule (CLAUDE.md Golden Rule #10):** we never write "100% / fully /
> guaranteed" for something that is only partly built. Every row below is
> marked truthfully:
> **🟢 live** (runs automatically on every change) · **🟡 partial** (real, still
> tightening) · **📋 planned** (designed, not wired yet).

---

## The one-sentence pitch

**BruteX is a self-checking factory for trading-strategy research:** every
change to the code is inspected by ~25 automated machines before it is allowed
in, and nothing ships unless *every* machine signs off — so the data is
trustworthy and the engine stays fast at any size.

Think of an airport: your bag (a code change) goes down a belt past scanner
after scanner. One red light and it doesn't board. That's how every line of
BruteX is treated.

---

## 1 · The assembly line — what happens to *every* change, automatically

| What you want | In plain English | BruteX today |
|---|---|---|
| Code is well-formed | "Is it tidy and consistent?" | 🟢 auto-formatter + linter + dead-code scan |
| No hidden mistakes | "Any obvious bugs or bad patterns?" | 🟢 static analysis + 2 type-checkers (strict) |
| No security holes | "Any vulnerabilities or leaked secrets?" | 🟢 5 scanners (bandit · semgrep · CodeQL · trivy · gitleaks) |
| No copy-paste | "Is anything duplicated?" | 🟢 duplication detector |
| Every line is tested | "Is the code actually exercised?" | 🟡 90% floor + a growing list of **100%-locked** modules |
| It actually builds & runs | "Does it start?" | 🟢 Rust kernel build + install + smoke tests |
| Dependencies are safe | "Are the libraries safe & licensed?" | 🟢 vulnerability + license + bill-of-materials checks |
| The data layer is sound | "Will the database behave?" | 🟢 DuckDB + Parquet round-trip tests (incl. rollback safety) |
| Safe to merge | "Green light to ship?" | 🟢 one master gate — **auto-merges only when all lights are green** |

**Bottom line:** the belt is real and runs on every single change. The one row
still climbing to perfect is test coverage (next section).

---

## 2 · "Cover everything" — the test promise

| Promise | Plain English | BruteX today |
|---|---|---|
| 100% coverage | "Every line of code is tested" | 🟡 **90% guaranteed floor**, ratcheting to 100% one locked module at a time (driver, catalog, instrument-store are already **100% + frozen there forever**) |
| All kinds of tests | "Tested every way that matters" | 🟢 unit · integration · property (fuzz) · golden-master · benchmark · stress (nightly regression gate) + soak (weekly self-hosted multi-hour, DEC-SOAK-REAL-TIER-001) · mutation · red-team |
| Bugs can't sneak back | "A fixed bug stays fixed" | 🟢 every fix ships with a test that *failed before the fix* |
| Real, not fake, tests | "Tests prove behaviour, not just run" | 🟢 mutation testing + a 'red-team' suite that plants bugs to prove the gates catch them |

---

## 3 · Ultra-fast — the O(1) promise

| Promise | Plain English | BruteX today |
|---|---|---|
| Instant lookups | "Finding a bar/indicator is instant, no matter how much data" | 🟢 constant-time access, **measured on every change** |
| Instant de-duplication | "Spotting a repeat among billions is instant" | 🟢 Bloom-filter + key-value 4-tier dedup, speed-gated |
| Same speed at any size | "Fast on day 1 (12M rows) and at full scale (730B)" | 🟢 one code path, designed O(1) per row/lookup/write |
| The very tightest bar | "Provably constant within 10%" | 🟡 wired; switches on once a dedicated quiet machine is registered (shared CI is too noisy to prove 10%) |

---

## 4 · Always watching — observability & recovery

| Promise | Plain English | BruteX today |
|---|---|---|
| Tamper-proof history | "Every action is logged, unforgeably" | 🟢 hash-chained audit log (break the chain → CI fails) |
| Same input → same output | "Re-run it, get the identical result" | 🟢 every run stamped with a reproducibility fingerprint |
| Live logs you can read | "See what it's doing, right now" | 🟢 structured logs + a live `/logs` web viewer |
| Dashboards & alerts | "Charts + pings when something's off" | 🟡 core in place; richer live dashboards/alerting on the roadmap |
| Auto-recover | "If a step breaks, it heals" | 🟡 rerun-safe ingest + opt-in boot auto-resume (`BRUTEX_AUTO_RESUME=1` — OFF by default); full self-healing is progressive |

---

## The honest bottom line

- ✅ **What's genuinely locked today:** the inspection belt (lint, types, 5
  security scanners, duplication, build) runs on *every* change and
  **auto-merges only on all-green**; the O(1) speed gates; the tamper-proof
  audit trail; perfect reproducibility; and a *growing* set of modules frozen
  at **100% test coverage**.
- 🟡 **What's still tightening:** total test coverage is at a **90% floor**
  climbing to 100% one module at a time; the strictest 10% speed proof and the
  richest live dashboards are wired-but-not-yet-fully-on.
- 📋 **What's designed, not built:** a few advanced test types (load, chaos,
  full end-to-end browser) for surfaces BruteX doesn't have yet (it's a
  backtest engine — **no live trading, no order placement**).

We publish this scorecard *honestly* on purpose: per `STANDARDS_100PCT.md`
§1 (re-verified 2026-06-08), **15 of 21** quality rows are *fully* enforced
today (71%), with 2 partial, 1 roadmap, and 3 manual-reviewer rows on a
visible ratchet — every gap tracks a dedicated follow-up in
`docs/MIGRATION.md`. The spine you'd actually bet on — **correctness,
reproducibility, the audit trail, the merge gate, O(1) dedup** — is real and
enforced right now.

> Priority law (never traded away): **Correctness > Reliability >
> Recoverability > Observability > Performance > Scalability.**

_See also: `docs/GUARANTEE_CHARTER.md` (the formal charter), `docs/STANDARDS_100PCT.md`
(the full enforcement matrix), `docs/BRUTEX_VS_THE_FIELD.md` (vs other engines)._
