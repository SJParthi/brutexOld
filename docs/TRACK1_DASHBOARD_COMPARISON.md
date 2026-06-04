# BruteX — What changed, in plain English

> A customer-facing, **honest** comparison of the BruteX dashboard + data-pull
> engine before and after the 2026-06 speed/correctness/observability work.
> Written so **any** reader — not just an engineer — can understand it.
>
> **Golden Rule #10 (no illusion):** every claim below is labelled
> ✅ **real & machine-verified** · 🟡 **real but best-effort** · 🚫 **not
> claimed / physically impossible**. We never say "everything is instant."
>
> Summarises decisions `DEC-DASHBOARD-FASTREAD-001`, `DEC-DHAN-PARALLEL-WINDOWS-001`,
> `DEC-DASHBOARD-COVERAGE-INVALIDATE-001`, `DEC-DASHBOARD-REQUEST-LOGGING-001`,
> `DEC-STORE-READ-WRITE-SPLIT-001`, `DEC-DASHBOARD-RATE-PANEL-001`
> (see `docs/MIGRATION.md`).

---

## 1. The one-line pitch (true)

> The things you touch **every second** — opening a page, looking up a bar,
> checking for duplicates, reading the live rate — are **genuinely instant and
> verified by an automated test on every code change.** The things that are
> **physically large** — scanning years of data, a multi-billion-trial sweep —
> are **honestly fast, not magically instant.** We tell you which is which.

---

## 2. What you actually experience

| What you do | Before | After | How fast now |
|---|---|---|---|
| **Open a dashboard page while data is downloading** | ❌ the **whole app froze** for seconds → minutes | ✅ stays responsive | **~2 ms** — blink and it's there |
| **Open `/db` (a raw table read) during a pull** | ❌ froze the server | ✅ the read **never waits behind the writer** | **< 0.3 s** (proven under a held writer lock) |
| **Pull one symbol's 5 years of data** (e.g. SENSEX) | 🐌 ~43 seconds | ✅ windows fan out in parallel | **~5–10 s** (≈ 7× faster) |
| **See whether a pull actually landed** | ❌ "Coverage" stuck at **0 series / 0 bars** | ✅ coverage refreshes after every pull | instant |
| **Read your logs** | ❌ poll spam **drowned** the real events | ✅ structured per-request logs + quiet firehose | readable |
| **"Is 5 requests/sec actually working?"** | ❓ only visible in raw logs | ✅ a **live rate panel** on the dashboard | at a glance |

---

## 3. "Is it really O(1)?" — the honest table

O(1) means *"the same speed no matter how big it gets."* Here's exactly where
that's true, machine-verified, vs. where it can't be:

| Capability | Plain meaning | O(1)? | The guarantee |
|---|---|---|---|
| **Uniqueness** | "Give this run one fingerprint, instantly." | ✅ | `blake3` fixed-size hash |
| **Deduplication** | "Seen this before? Know in one step." | ✅ Bloom; 🟡 LMDB (tiny, bounded) | CI test fails the build if it slows down |
| **Mapping / lookup** | "Find any bar or signal instantly." | ✅ | ratio ≤ **1.1×** gate in CI |
| **Dashboard cached read** | "Coverage page loads instantly." | ✅ | measured ~2 ms under load |
| **Live rate read** (`/api/v1/rate`) | "Show the current rate instantly." | ✅ | in-memory dict copy |
| **Per-step time/space** | "Each tick costs the same as the last." | ✅ | per-hot-path CI gate |
| **An arbitrary `/db` SQL scan** | "Run any query over the data." | 🚫 O(rows) | the *wait* is gone; the *scan* isn't |
| **Whole 33-billion-trial sweep** | "Backtest everything at once." | 🚫 **2–4 days** | physics; we name the hardware |
| **Total storage** | "Keep 5 years of 1-min bars." | 🚫 grows with data | that's data, not an algorithm |
| **A constant 5 requests/sec** | "Pull at exactly 5 r/s forever." | 🚫 **up to ~5** | the vendor + network decide |

**Legend:** ✅ real + CI-gated · 🟡 real but best-effort · 🚫 not claimable.

---

## 4. The "5 requests/sec" story (the most-asked question)

| Pull type | Before | After | Hits ~5 r/s? |
|---|---|---|---|
| **Whole market** (all instruments) | already parallel | outer-loop fan-out | ✅ saturates ~5 when healthy |
| **One symbol** (e.g. SENSEX) | 🐌 **0.56 r/s** (windows one-by-one) | windows fan out | ✅ **~4 r/s**, ≈ 7× faster |
| **A guaranteed _constant_ 5?** | — | — | 🚫 **no** |

**Why not a constant 5** (we won't fake this): a polite rate limiter **steps
5 → 4 → 3** when the vendor pushes back (HTTP 429), and recovers 3 → 4 → 5 when
it clears. **3 is the hard floor** — it never stalls. Throughput is
`workers ÷ latency` capped by the limiter, so when the vendor is slow the
*measured* rate drops below 5. What **is** guaranteed: it never *exceeds* 5
(the cap), never stalls below 3, and the live panel shows you the real number.

---

## 5. What we **don't** claim (so you're never misled)

- 🚫 **Not** "millions of trials in minutes." A full sweep is **2–4 days** on
  the reference Mac (M4 Pro); larger machines bring it to 12–24 h. We always
  name the hardware.
- 🚫 **Not** a multi-tenant SaaS. The dashboard is a **local, single-operator,
  read-only** tool at `127.0.0.1`. It attracts by being **trustworthy and
  legible**, not by faking scale.
- 🚫 **Not** "everything is O(1)." Arbitrary data scans and total storage grow
  with the data — by definition.
- 🟡 Some live numbers (the rate panel during a real vendor pull) populate only
  when a real pull runs; their *logic* is fully tested, the live *send* needs a
  live connection.

---

## 6. How every claim is kept honest (automated, on every change)

| Pillar | What runs on every code push |
|---|---|
| **Correctness** | 100% line + branch coverage on new modules; idempotence + audit-chain oracles |
| **Speed** | O(1) ratio ≤ 1.1× benchmark gates; "read-doesn't-wait" + "responsive-under-write" oracles |
| **Honesty** | every PR labels ✅/🟡/🚫; a "no-over-claim" review; decision ledger entry |
| **7 test types** | unit · integration · property · golden · benchmark · stress · mutation |

*Honest envelope:* the full standards decree is the **target**; the actually-CI-
enforced subset is tracked, row by row, in `docs/STANDARDS_100PCT.md` — we don't
claim "100% enforced" where it isn't.

---

_This page is versioned with the code, so it stays current. Last updated
2026-06 alongside `DEC-DASHBOARD-RATE-PANEL-001`._
