# Fib Strategy Investigation — Plan + Open Questions (DOCS ONLY)

> **Status:** PLANNING / DISCUSSION ONLY. No code in this doc; no
> indicator implementations until operator approves the spec.
> **Created:** 2026-05-17 after operator's rare-pattern strategy Q&A.
> **Authority:** This is a planning artifact, not a locked decision.
> Lives under `docs/track1/` because it speaks to Track 1 (signal-only
> spot-index research).

---

## Why this doc exists

Operator described a manual trading edge during the 2026-05-17 session:

> *"We have identified one strategy with Fibonacci pivot point standard
> EMA 20 EMA 200 candle color body minute timeframe and our stop loss
> and target profit ... whenever the particular values or process or
> combination happens always there will be very rare but the stop loss
> is very very minimal and less and even target profit is really high
> and even winning probability is always extremely high"*

Two follow-up exchanges established:

1. **Fib anchor type:** operator confirmed they want ALL anchor systems
   covered (daily H-L + weekly H-L + session H-L + pivot-Fib R/S) —
   *"Dude all these possibilities dude okay because we can't take risk
   on any areas dude"*

2. **Trade frequency:** operator doesn't know how often the manual
   strategy fires historically — *"I don't know exact frequency — need
   to measure"*

3. **Statistical validation at low N:** operator asked what happens if
   the rare combo doesn't reach 500 trades — answered with the Track B
   rare-pattern evidence stack (6 evidence sources).

**This doc captures the open spec questions + proposed sequence**
without writing any indicator/strategy code yet. Code only after
operator approves a locked spec.

---

## The 3 open questions (operator answer needed before code)

### Q1. Exact Fib formulas for each anchor system

For each of the 4 anchor systems, we need the **formula + bullish/bearish
convention + tolerance threshold** before implementation:

| Anchor system | Open questions |
|---|---|
| **Daily H-L Fib** (already implemented) | None — verified against operator's 2026-05-16 screenshots. Bullish/bearish convention flips; 7 retracements + 5 extensions each direction. |
| **Weekly H-L Fib** | (a) "Week" = Mon-Fri including partial-week holidays? Or rolling 5 trading days? (b) Same 7-level retracement set as daily, or add weekly-specific levels (e.g., 88.6%)? (c) Tolerance bps for "near weekly Fib"? |
| **Session intraday H-L Fib** | (a) Anchor updates EVERY bar (new HIGH or LOW resets), or LOCKED at first 30 min? Both? (b) What's a meaningful "session retracement" — running anchor on session H-L makes the level move continuously, which is hard to brute-force test. (c) Or do we anchor on the first 30-min OR (PRA / Opening Range) high-low instead? |
| **Pivot-Point Fib R/S** | (a) Standard formulas: P = mean(H,L,C); R/S = P ± Fib-scaled × range? (b) Or other variant — Camarilla pivots? Woodie's? Demark? (c) Tolerance bps for "near pivot-Fib R1"? |

### Q2. Pattern frequency probe — design

Before adding 11+ new vocab bits, we should EMPIRICALLY MEASURE how
often the operator's manual 5-condition combo fires historically. The
probe is a one-off SCRIPT (not a vocab change) that:

1. Reads 2021-today 1-minute bars from the data lake (Track 1 Phase 2
   output)
2. Computes EMA 20, EMA 200, candle color, candle body classification
3. For each bar, checks the operator's hypothesized condition AND
4. Counts firings per (instrument × month) bucket
5. Emits a report: "your combo fires N times across 5 years; per
   instrument: NIFTY=A, BANKNIFTY=B, SENSEX=C"

Open questions for the probe spec:
- (a) Exact operator condition? (need 5 specific bit names)
- (b) Tolerance bps for "near Fib X" — assume 5 bps default?
- (c) What's the bullish vs bearish direction convention for the test?
- (d) Output format: CSV? markdown table? PNG histogram?

### Q3. Track B "rare-pattern" output design

Per the 6-evidence stack (1. payoff asymmetry, 2. binomial p-value,
3. permutation test, 4. cross-instrument generalization, 5. lockbox,
6. trade replay):

Open questions:
- (a) What constitutes "high-conviction" vs "low-conviction"? 4-of-6
  evidence sources favorable? 5-of-6? Configurable?
- (b) Where does Track B live in the output schema? Same
  `results.parquet` with extra columns, or separate
  `track_b_results.parquet`?
- (c) Trade replay format: matplotlib PNG per trade? Interactive HTML
  dashboard? CSV with bar OHLC + indicator overlay values for the
  operator to render in their own tool?

---

## Proposed sequence (after operator answers above)

This is a sketch, not a commitment. All items below would be SEPARATE
PRs and only proceed after operator approves a locked spec.

| # | PR title | Scope | Depends on |
|---|---|---|---|
| Doc-1 | This doc + operator answers | DOCS only; spec locked | — |
| PR-1 | Weekly H-L Fib indicator | One module + tests + 5 vocab bits | Doc-1 (formulas locked) |
| PR-2 | Pivot-Point Fib R/S indicator | One module + tests + 6 vocab bits | Doc-1 (formula locked) |
| PR-3 | Session intraday Fib indicator | One module + tests + N vocab bits | Doc-1 (anchor convention locked) |
| PR-4 | Pattern frequency probe SCRIPT | One script in `scripts/` + golden fixture for unit testing | Doc-1 (operator combo spec) |
| PR-5 | Run probe on operator's combo | Empirical output — informational, no code | PR-4 |
| PR-6 | Track B output schema | `results.parquet` schema extension + 6-evidence computation | PR-4 (empirical N known) |
| PR-7 | Trade replay generator | One script + matplotlib/HTML output | PR-6 |

**TOTAL:** 1 doc + 6-7 code PRs. None happen until operator approves.

---

## Honest envelope (per Golden Rule #10)

This plan does NOT guarantee:

1. **That the operator's manual strategy is real.** Even with all 4 Fib
   anchors + Track B evidence, a rare-pattern strategy can be "lucky
   in 5 years of data but won't repeat." The 6-evidence stack reduces
   that risk but cannot eliminate it.

2. **That the brute force will find it precisely.** Stage 1A runs on
   NIFTY 15m only; if the operator's combo only works on 1m, it might
   not survive to Stage 1B. This is a known structural risk of the
   5-stage pipeline.

3. **That Track B will replace statistical rigor.** Track B is a
   secondary signal-detection lane for genuinely-rare patterns. Track
   A (≥500 trades + DSR/CPCV/WFO/Lockbox) remains the primary
   acceptance path. Track B is "look at this, operator, decide
   yourself" — not "trade with confidence."

What this plan DOES promise:

1. **Vocabulary REPRESENTABILITY** — after PR-1, PR-2, PR-3, the
   operator's 5-condition combo is expressible as a u64 mask and the
   brute force will test it.

2. **EMPIRICAL FREQUENCY** — after PR-4 + PR-5, the operator knows
   exactly how often their combo fires + per which instrument.

3. **EVIDENCE SURFACING** — after PR-6 + PR-7, even rare patterns get
   a complete 6-evidence dossier in the output, allowing the operator
   to make an informed manual call.

---

## What we are NOT doing in this PR sequence

- ❌ Not adding session-Fib without operator confirming the anchor convention
- ❌ Not promising statistical certainty at low N (math limit)
- ❌ Not bypassing DSR/CPCV/WFO/Lockbox for Track A
- ❌ Not auto-trading rare patterns — Track B output is for human review
- ❌ Not modifying the existing 52-condition vocabulary until operator
  answers Q1 above

---

## Operator's pending decisions (RECAP)

The 3 questions above need answers before ANY code starts. After answers
land, the spec section of this doc gets a "LOCKED" header and the PR-1
sequence begins.

Until then: discussions, thoughts, plans — no code per operator's
2026-05-17 decree (*"don't implement these indicators or strategies
... should be entirely discussions thoughts plan docs"*).
