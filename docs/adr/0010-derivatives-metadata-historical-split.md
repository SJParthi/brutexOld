# ADR-0010 — Split the F&O "Pull expiries + contracts" into metadata-only + a separate historical pull, backed by an incremental registry cache

**Decision ID:** `DEC-DERIV-METADATA-HISTORICAL-SPLIT-001` (MIGRATION.md entry #732)
**Date:** 2026-06-14
**Status:** ACCEPTED — operator directive (2026-06-14), gated through the
Golden-Rule-#9 hostile review (a Plan-agent design + edge-case map).
**Author:** Claude Code (operator-directed)
**Supersedes (clarifies):** `DEC-INSTRUMENT-UNIVERSE-FAST-001` (lean master) — the
master STAYS lean; the registry is a SEPARATE cache, not a re-expansion.
**Builds on:** `DEC-UNIVERSE-PULL-PHASED-001` (the dormant two-phase engine wired
here), `DEC-PULL-DERIVATIVES-001` (the combined button being split),
`DEC-GROWW-DERIV-UNTIL-CLEAN-001` (#730, the until-clean loop the historical pull
reuses), `DEC-COVERAGE-LEDGER-001` (the status model reused).
**Operator directive (verbatim intent):** "whenever i click expiries and contracts
… except historical it should pull … we need to have the separate button … only
expiries and its entire contracts … once we have the precise year expiries and
contracts then the same doesn't need to be pulled … single select or multiple
select or entire permutations and combinations should work."

---

## 1. Context

The dashboard "Pull expiries + contracts" button (`/api/v1/universe/pull-derivatives`
→ `pull_all_derivatives` → `ingest_underlying_options/futures`) **fuses** two jobs
at the deepest level: it enumerates the chain (`get_expiries` per year →
`get_contracts` per expiry) AND immediately downloads each contract's full-range
`get_historical_candles`. So one click downloads weeks-scale OHLC for every strike ×
every expiry × `2020→today`, "keeps running", and burns the 500/min governor (the
operator's reported bug — confirmed in a live run: `rate.429 → rate.stepdown 5→4`).

Worse, the chain ENUMERATION is re-fired on every click: the actual expiry dates +
contract symbols were deliberately discarded (`DEC-INSTRUMENT-UNIVERSE-FAST-001` /
`DEC-EXPIRED-OPTIONS-TABLE-001`), and the `coverage_ledger` stores only per-year
COUNTS + status, never the lists. So there is no cache to skip the immutable past.

A complete, tested, **dormant** two-phase engine already exists
(`brutex/orchestrator/universe_phases.py`, `DEC-UNIVERSE-PULL-PHASED-001`):
`run_two_phase` + `cells_to_pull` (skip a settled *past* year) +
`contract_jobs_to_pull` (skip an *expired* expiry's immutable contract set). It has
**zero production call sites** — and assumes a `store.list_contracts()` that does
not exist.

## 2. Decision

1. **"Pull expiries + contracts" becomes METADATA-ONLY.** It enumerates +
   **persists** expiries (per `underlying, year`) and contracts (per expiry) and
   pulls **zero** candles. The dormant `run_two_phase` is wired to it.
2. **A separate "Pull historical" button** pulls `get_historical_candles` for an
   operator-SELECTED contract set — single / one-expiry-chain / one-underlying /
   all (every permutation) — reusing the verified candle spine + the until-clean
   loop (`DEC-GROWW-DERIV-UNTIL-CLEAN-001`, #730).
3. **A SEPARATE `registry.duckdb` cache** (NOT the instrument master) holds the real
   `expiries` + `contracts` tables (dates / symbols / strikes / sides /
   candle_status). It reuses the `coverage_ledger` status MODEL (`classify_cell`,
   `STATUS_*`) but not its table. It is the missing `store.list_contracts()` the
   phase engine assumed — browsable in `/db`.
4. **Incremental / idempotent:** reuse `cells_to_pull` / `contract_jobs_to_pull` —
   an immutable past year / expired expiry is **skipped before the network call**;
   the current year + still-live expiries are re-pulled (new expiries/strikes); a
   failed cell resumes. A re-click is **O(deltas)**, not O(whole chain).
5. **Historical OHLC stays on the verified `bars/FNO/` path (Path A).** The
   live-only `options/` lake (`OptionBar` with `iv`/`spot`/`ATM±N`) needs columns
   `get_historical_candles` does **not** return; feeding it would mean fabricating
   `iv`/`spot`/relative-strike coordinates — a **Golden Rule #1 violation**. So the
   **registry tables** are the structured, browsable-by-expiry/strike/CE-PE view the
   operator wants; OHLC is not re-routed into `options/`.

## 3. Why the registry is a SEPARATE DB (master stays lean)

Re-adding the ~89k contract rows to `instruments.duckdb` would reverse
`DEC-INSTRUMENT-UNIVERSE-FAST-001`. Instead the registry is a **sibling cache file**
(operator's explicit choice). The master's PK / scope / the brute-force engine are
untouched; the legs the master *discards* are now *cached* (recoverable by re-pull),
not *mastered*. The registry is a **rebuildable cache** — a version mismatch drops +
recreates it (a re-pull restores it), so a stale file never blocks a boot.

## 4. Consequences

- A metadata re-click is cheap (O(deltas)); the heavy OHLC pull is opt-in + scoped.
- `STATUS_EXPIRIES_ONLY` (built dormant for this) goes live; `/coverage` keeps
  working (the metadata pull also writes the `coverage_ledger` count rollup).
- FUT legs cannot be `OptionBar`s (`side ∈ {CE,PE}`), reinforcing Path A for OHLC.

## 5. Phased implementation (smallest-first; each its own PR, 100% strict gates)

- **PR-1 — `registry_store.py`** (THIS PR): the separate `registry.duckdb` +
  `expiries`/`contracts` tables + idempotent upserts + the static filtered reads +
  `list_contract_jobs` (feeds `settled_contract_jobs`). 100% line+branch, offline.
- **PR-2 — metadata orchestration:** wire `run_two_phase` to
  `resolve_expiries`/`resolve_contracts` + registry upserts + the `coverage_ledger`
  rollup (no candles, no route yet).
- **PR-3 — metadata route + button:** `POST /pull-expiries-contracts`; repoint the
  existing button; `STATUS_EXPIRIES_ONLY` live.
- **PR-4 — `/db` visibility:** browse the registry `expiries`/`contracts` tables
  (the structured expiry/strike/CE-PE view).
- **PR-5 — separate "Pull historical" button:** selection-based (single/multi/all),
  reuse the candle spine + the until-clean loop; set `candle_status`.
- **PR-6 — decommission** the fused candle path in the old combined button.
  **SHIPPED** (DEC-DERIV-DECOMMISSION-FUSED-001, MIGRATION.md #749): the fused
  `POST /pull-derivatives` route + `pull_all_derivatives` engine were removed and the
  bulk derivative buttons repointed to the metadata-only `/pull-expiries-contracts` —
  so no dashboard click triggers a one-click whole-chain candle download. The
  surviving `resolve_pull_intervals` knob feeds the separate `/pull-historical` route.

## 6. Honest envelope (Golden Rule #10)

✅ the split is mostly WIRING a tested-but-dormant engine + a new cache store, not a
green-field rewrite — lower risk. ✅ O(deltas) re-click, O(1) per skip-test
(`frozenset` membership). 🟡 a first full metadata pull is still O(chain) network
calls (bounded, paced); only RE-clicks are O(deltas). 🟡 the registry does NOT store
OHLC (that's the candle lake); it is metadata only. ✅ the brute-force engine + the
52-vocabulary + the lean master are UNTOUCHED.
