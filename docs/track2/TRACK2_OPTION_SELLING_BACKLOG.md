# Track 2 backlog — Intraday Option-Selling Strategy (NOT AUTHORIZED)

> **Status: BACKLOG ONLY (DEC-PLAN2-OPTION-SELL-BACKLOG-001).** Recorded so
> the 2026-06-11 operator design discussion is never lost. NOTHING here is
> authorized for implementation — building it requires its own explicit
> decision + hostile review (CLAUDE.md §2 Rules 3 & 9). The Track 1 engine
> and lake scope are unchanged by this document.

## Thesis (operator, 2026-06-11)

Scan current-expiry **stock options** intraday for top-volume / top-gainer
premium spikes (e.g. a put up ~80% on heavy volume within the first hour).
Hypothesis: after an extreme intraday premium run, IV cools and theta bleeds —
the premium mean-reverts even when the underlying only consolidates. Action:
**SELL the spiked option** with hard SL/TP, intraday only, square-off by
close. Backtest before any live use.

Honest risk profile: many small wins + rare brutal continuation-day losses
(negative skew). The backtest's question is NOT the win rate — it is whether
the tail losses eat the wins (SL discipline + event filters decide).

## Data feasibility findings (established 2026-06-11, in-repo verified)

1. **The lake already holds the fuel**: Dhan `rollingoption` 1-minute bars
   with per-bar `volume/oi/iv/strike/spot` for the 200 F&O stocks
   (REFERENCE_ONLY, DEC-P0-063). Top-volume spikes concentrate near ATM, so
   the banded coverage captures most entries.
2. **The ATM window MOVES** (CLAUDE.md §6): strikes are addressed
   relative-to-spot (`ATM±N`, ±3 for stocks) — "ATM+1" is a slot, not a
   contract. Per-contract series must be reconstructed by grouping bars on
   their per-bar `strike[]` value across slots.
3. **Winner-censoring hazard**: a sold strike drifts AWAY from ATM exactly on
   winning trades and can exit the ±3 band mid-trade — losers are always
   fully captured, winners can truncate. Any backtest MUST censor (never
   fabricate) trades whose exit bars left coverage, or the result is
   structurally biased optimistic.
4. **Groww is NOT the historical source**: ADR-0002's verified comparison
   recorded Groww options fetch = n/a (live chain ≠ history). The operator
   reports Groww may now serve 1-minute data from 2020 — **operator-reported,
   UNVERIFIED**. If this track is ever authorized, **Phase 0 = a hard-gate
   empirical probe** of Groww's expired-option 1m depth (the same discipline
   as the Dhan Phase-0 probes).
5. **Full-chain intraday history** (every strike/expiry) is otherwise a paid
   data-vendor decision — its own ADR if ever needed.

## If authorized, the spec slices (in order)

Phase 0 data probes (Groww depth; Dhan band-coverage measurement over the
live lake) → strike-band widening decision (quota math) → per-contract
reconstruction + censoring design → entry/SL/TP/square-off grammar → margin
(SPAN+exposure) + slippage model → hostile review → implementation.
