# Did my strategy *really* win — or did I just get lucky? — a plain-English guide

> **Module:** `brutex/metrics/spa.py` · **Decision:** DEC-STAT-SPA-001
> (`docs/MIGRATION.md` #431) · **Spec:** Stage-4 filters #4 + #6.
>
> This page is the "explain it to a human in 60 seconds" companion to the
> code. Every claim here is backed by the implementation + the cited
> papers (CLAUDE.md Golden Rule #1 — no hallucination).

---

## 1. The one-sentence problem

> *"I brute-forced **thousands** of trading strategies. The best one looks
> amazing on history. Is its edge **real**, or is it the **luckiest coin-flip
> out of thousands**?"*

If you test enough random strategies, **one of them WILL look brilliant by
pure chance.** Picking that winner and trusting it is called **data
snooping** — and it is the single most common way backtests lie. This
module is the lie detector.

---

## 2. The four approaches, head-to-head (the table)

| | **Naive p-value** | **White Reality Check** | **Hansen SPA** ⭐ | **Romano-Wolf step-down** |
|---|---|---|---|---|
| **Plain-English question** | "Is *this one* strategy real?" | "Is the **best-of-all** real?" | "Is the **best-of-all** real? (sharper)" | "**Which** of the strategies are real?" |
| **Corrects for testing many?** | ❌ No | ✅ Yes | ✅ Yes | ✅ Yes |
| **Output** | one p-value | one p-value | one p-value (×3 bounds) | a **list**: which strategies pass |
| **Handles weak/junk strategies dragging it down?** | n/a | ❌ loses power | ✅ **re-centres them out** | ✅ step-down gains power |
| **Best for** | a single pre-chosen idea | "is there *any* edge here?" | the **recommended** single-edge test | building the **final shortlist** |
| **Verdict it gives** | over-optimistic ⚠️ | honest but conservative | honest + powerful | honest + per-strategy |
| **Paper** | — | White (2000) | Hansen (2005) | Romano-Wolf (2005/2016) |

⭐ **Hansen SPA is the recommended single-number test** (spec line 401:
*pass if SPA p < 0.05*). Romano-Wolf is what you use when you want the
**named survivors**, not just a yes/no.

---

## 3. Why "naive" is dangerous — a 10-second intuition

| You tested… | Best one's naive p-value | What naive says | The truth |
|---|---|---|---|
| 1 strategy | 0.04 | "significant!" | probably fine |
| 100 strategies | 0.04 | "significant!" | **~98% chance it's pure luck** |
| 1,000 strategies | 0.04 | "significant!" | **almost certainly luck** |

The naive test asks "how surprising is this number?" — but forgets you
**went fishing 1,000 times.** White / Hansen / Romano-Wolf remember.

---

## 4. What you actually get back (real numbers from the test suite)

A tiny 8-row example with **3 strategies**: column 0 is a genuine winner,
column 1 is noise, column 2 is a loser. Seed-pinned, fully reproducible:

| Strategy | Avg excess return | Naive p | **Romano-Wolf adjusted p** | Rejected (real edge)? |
|---|---|---|---|---|
| #0 (winner) | +0.99 | 0.00 | **0.00** | ✅ **YES** |
| #1 (noise) | +0.05 | 0.22 | **0.41** | ❌ no |
| #2 (loser) | −0.16 | 0.99 | **0.99** | ❌ no |

- **Reality Check p = 0.00**, **Hansen SPA p = 0.00** → "yes, the best one
  is a real edge, not luck."
- Notice strategy #1's p **rose from 0.22 → 0.41**: that is the
  multiplicity correction working — it got *harder* to pass once we
  admitted we tested several. **A correction can only ever make a p-value
  bigger, never smaller** (proven as a property test).

---

## 5. Guarantees — what this module promises, honestly

| Promise | Status | How it's enforced |
|---|---|---|
| **Same input + same seed → identical p-values, bit-for-bit** | ✅ **Guaranteed** | one seeded `numpy` generator; golden + property determinism tests |
| **Every p-value is a real probability in [0, 1]** | ✅ **Guaranteed** | property test over thousands of random matrices |
| **No invented "magic" thresholds** | ✅ **Guaranteed** | every formula cites White/Hansen/Romano-Wolf/Politis-Romano |
| **No `NaN` / `inf` ever leaks out** | ✅ **Guaranteed** | bad inputs raise a *clear error* instead (constant column, degenerate bootstrap, non-finite, …) |
| **100% line + branch test coverage** | ✅ **Guaranteed** | CI gate `--cov-fail-under=100` on the module |
| **Correct false-positive rate (FWER) in finite samples** | 🟡 **Asymptotic** | exact only as data grows; measured ~0.08 at n=200 → ~0.053 at n=2000 for a 0.05 target — *documented, not hidden* |
| **Runs in O(1) time** | 🚫 **Impossible — and we say so** | a bootstrap *must* resample B×; it's `O(B·n·m)` offline Stage-4 work (~10-20 min on ~1,000 candidates), **not** a per-bar hot path |

> **The O(1) honesty note (CLAUDE.md Golden Rule #10):** BruteX demands
> O(1) on the *engine hot paths* (bar lookup, dedup, mask evaluation —
> the things that run billions of times). A statistical bootstrap is a
> different animal: it deliberately resamples the data thousands of times
> to build a null distribution, so it is fundamentally **not** O(1).
> Claiming otherwise would be a hallucination. What we **did** make fast:
> the Romano-Wolf step-down was tightened from O(B·m²) → **O(B·m)** (a
> single cumulative-max scan), and peak memory is bounded so it never
> builds a giant resample tensor.

---

## 6. When do I use which? (decision cheat-sheet)

| If you want to… | Use | Pass rule |
|---|---|---|
| Sanity-check **one** pre-registered idea | naive p (any stats tool) | p < 0.05 |
| Ask "is there **any** real edge in this whole pool?" | `reality_check` | p < 0.05 |
| Get the **trustworthy** single yes/no after brute-forcing | `hansen_spa` (use `pvalue_consistent`) ⭐ | p < 0.05 |
| Get the **final shortlist** of which strategies survive | `romano_wolf` | `rejected_indices` |

---

## 7. Where it sits in the pipeline

```
Stage 1A/1B  →  Stage 2 (33B trials)  →  Stage 3 (refine)  →  Stage 4 ✦
                                                              ├─ #2 Deflated Sharpe
                                                              ├─ #3 CPCV
                                                              ├─ #4 Reality Check / SPA  ← THIS MODULE
                                                              ├─ #5 PBO
                                                              ├─ #6 Romano-Wolf step-down ← THIS MODULE
                                                              ├─ #7 Walk-Forward ×3
                                                              ├─ #8 Lockbox
                                                              └─ #9 Monte-Carlo permutation
```

This PR ships filters **#4 and #6** as standalone, fully-tested functions.
Wiring them into the end-to-end Stage-4 cascade (consuming real
per-instrument leaderboards) is a later, separately-tracked step — stated
plainly so nothing is over-claimed.
