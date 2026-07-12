# ADR-0016 — Corporate-action adjustment layer (design sketch + hard gate)

**Decision ID:** `DEC-CORP-ACTION-GATE-001` (MIGRATION.md entry #1351 — renumbered from the stale #1269 pointer at merge; corrected by DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001)
**Date:** 2026-07-03 (reconciled 2026-07-04 with DEC-ENGINE-SCOPE-GUARD-001, entry #1178)
**Status:** **Proposed** — design sketch for a FUTURE layer. **NOT implemented.** The
shipped enforcement is the sweep-entry scope gate of DEC-ENGINE-SCOPE-GUARD-001
(`brutex/engine/sweep_scope.py::check_sweep_scope` over the canonical
`brutex.universe.classify_engine_sweep_target` law), which THIS decision EXTENDS with the
corporate-action rationale + hazard warning (§3) until this layer exists.
**Author:** the 2026-07-03 repo audit session (finding #5) — attributed to the decision ID above
**Builds on:** CLAUDE.md §2 Golden Rule #1 (no hallucination) / #3 (scope lock) / #10
(honest limits); DEC-ENGINE-SCOPE-GUARD-001 (MIGRATION.md #1178 — the implemented gate);
DEC-P0-063 (lake ingests ~212 F&O stocks REFERENCE_ONLY);
DEC-NTM-GROWW-REPOINT-001 (~750 REFERENCE_ONLY underlyings); ADR-0012 /
DEC-NIFTY-DYNAMIC-STRATEGY-001 (the operator-authorized NIFTY-futures engine track).

---

## 1. Context — zero corporate-action code, and why that is currently CORRECT

A repo-wide audit (2026-07-03, finding #5) confirmed BruteX contains **ZERO
corporate-action handling**: no split/bonus/demerger/rights adjustment, no
adjustment-factor table, no ex-date awareness, no face-value tracking. Every grep hit for
`split|bonus|divisor|adjustment|ex-date` is unrelated (bps divisors, HLC3 math, GST
splits, string `.split`).

**For the locked 3-index engine scope this is CORRECT, not a bug.** NIFTY 50, BANKNIFTY
and SENSEX are divisor-maintained indices: when a constituent splits or issues a bonus,
the exchange adjusts the index divisor so the index LEVEL is continuous — an
index-methodology claim that is **UNVERIFIED** against a primary NSE Indices / BSE
methodology document (Golden Rule #1; no exchange methodology paper is probeable from
this sandbox — verification rides the §4.3 probe, same as every source row). The gate's
justification does NOT depend on that claim: the operative, repo-verified hazard is that
raw single-stock vendor candles are unadjusted across ex-dates (§2), so single-stock
sweeps are unsafe either way. India VIX is likewise a computed index. The engine needs
no adjustment layer **as long as it only ever sweeps these instruments** — which is
exactly what the `sweep_scope` gate (DEC-ENGINE-SCOPE-GUARD-001) now enforces.

## 2. The problem — single-stock expansion without this layer is silent corruption

The LAKE already stores ~750 single-stock spot underlyings + the full F&O option/futures
chain (REFERENCE_ONLY, DEC-P0-063 / DEC-NTM-GROWW-REPOINT-001). Raw vendor 1-minute
candles for a single stock are **unadjusted across ex-dates**. Concretely, a 1:1 bonus
halves the price overnight; a backtest or a %-change display spanning that boundary sees
a fabricated −50% move.

This is not hypothetical future damage — two READ surfaces already mis-state across a
split boundary today (both REFERENCE_ONLY display surfaces, not engine paths):

- **Movers %-change baseline** — `brutex/dashboard/movers_snapshot.py` computes
  `pct_change = (ltp − prev_day_close) / prev_day_close` from raw stored closes. A
  reference stock's ex-date session reads as a giant fake move and tops the movers
  ranking.
- **Moneyness strike grid** — `brutex/data/moneyness.py::derive_strike_step` derives the
  strike step as the min positive gap of the LISTED strikes, and `classify` compares a
  fixed strike against the moving spot. Across a split, old-series strikes and the
  post-split spot belong to different price regimes; the derived ATM offset / ITM-OTM
  depth is wrong for pre-split contracts.

If the ENGINE were ever pointed at a single stock (the future expansion DEC-P0-063
explicitly anticipates but does NOT authorize), every condition bit that references a
prior-day level (PDH/PDL, pivots, Fib anchors, gap-day bits) would fire on fabricated
cliffs, and the sweep would rank strategies on corrupted data — silently, with green
tests. That violates Golden Rule #1 at engine scale.

## 3. The shipped enforcement — DEC-ENGINE-SCOPE-GUARD-001, extended by this decision

Until this layer exists, expansion is BLOCKED mechanically, not by convention. The
implemented gate is **DEC-ENGINE-SCOPE-GUARD-001** (MIGRATION.md #1178, PR #1102):

- ONE canonical classification law, `brutex.universe.classify_engine_sweep_target`
  (+ `is_engine_sweep_symbol` / `ENGINE_SWEEP_SPOT_TRIPLES`, derived from
  `BRUTE_FORCE_INDEX_UNDERLYINGS`), consumed by the Layer-2 preflight
  `brutex/engine/sweep_scope.py::check_sweep_scope` and the Layer-1
  `SweepGridConfig.__post_init__` hard invariant. Allowed: exactly the 3 spot triples
  (NSE/INDEX/NIFTY · NSE/INDEX/BANKNIFTY · BSE/INDEX/SENSEX — the coordinate twin of
  the lake-side `BRUTE_FORCE_INDEX_SYMBOLS`, agreement test-pinned) + the ADR-0012
  NIFTY-futures shapes under NSE/FNO. Deny-by-default, exact-case, zero I/O.
- Wired at every production ENGINE-EXECUTING entry: `brutex sweep` (single tuple), the
  `--symbols` chokepoint in `cmd_sweep_all` (covering `sweep-all`, `sweep-daemon`, and
  the dashboard 🚀 Run Sweep button — the dashboard ADDITIONALLY gates eagerly in
  `resolve_sweep_request`, so an out-of-scope symbol is a 400 naming the offender, not
  a background job whose refusal lands only on server stderr), AND — added by THIS
  decision — the `sweep-verify --recount` Tier-3 shadow re-sweep, which EXECUTES the
  real `run_sweep_grid` over lake bars into `--scratch-root`, so it is a production
  sweep entry, not a report. Refusal names every offender + the law + this ADR and
  exits `EXIT_USER_ERROR`.
- **This decision's extension (DEC-CORP-ACTION-GATE-001):** (a) the hard refusal
  message carries the corporate-action rationale verbatim — the literal phrase
  "corporate-action adjustment layer missing" + DEC-CORP-ACTION-GATE-001 +
  DEC-ENGINE-SCOPE-GUARD-001 + this ADR — so the operator sees WHY, not just NO;
  (b) the CLI-only `--allow-out-of-scope` research escape (DEC-ENGINE-SCOPE-GUARD-001's
  deliberate design, which SUPERSEDES this ADR's original "no flag bypasses" posture)
  can never proceed silently: every overridden launch emits one loud
  `sweep.scope_override_hazard` structlog WARNING carrying the same hazard text, on
  top of the base gate's override WARNING + hash-chained `SWEEP_SCOPE_OVERRIDE` audit
  stamp. The unattended `sweep-daemon` refuses the flag outright; no dashboard
  override exists. The gate is lifted per-symbol only by a reviewed PR that lands the
  adjustment layer below (or, for NIFTY futures, the ADR-0012 futures-sweep launch PR
  — see §6).

Honest envelope: direct library construction of `SweepGridConfig` opts out via the
explicit `unsafe_allow_out_of_scope_instruments` field (synthetic-label test grids —
not an operator surface). `sweep-verify`'s report-only Tier 1/2 (structural +
identity) are deliberately unguarded: they read an existing tree and launch no engine,
so a legacy out-of-scope tree stays report-verifiable; its Tier-3 recount now requires
the explicit `--allow-out-of-scope` escape and proceeds LOUDLY (the reconciled
trade-off — verifiability preserved, silence eliminated).

## 4. Future layer design (sketch — nothing below is implemented)

### 4.1 Adjustment-factor pipeline, never lake mutation

- The raw 1-minute lake stays **vendor-truth, immutable** (the same principle as
  DEC-LAKE-READ-UNIQUE-001: fix at read/derive time, never rewrite stored bars).
- Corporate actions land in a **separate, versioned adjustment-factor table** —
  per `(identity, ex_date)`: action type (split/bonus/dividend-special/demerger),
  ratio, cumulative back-adjustment factor, source citation, verification status.
- Reads that need continuity consume **back-adjusted DERIVED views**: bar streams and
  derived TFs re-derived from `raw × cumulative_factor`, stamped with the adjustment
  table's version so run-identity (Golden Rule #4) captures WHICH adjustment truth a
  sweep saw. A factor-table revision ⇒ new `data_snapshot_hash` ⇒ new run identity,
  never a silent in-place change.
- Display surfaces (movers %-change, moneyness) take the same factors at read time;
  option STRIKES need series-aware handling (exchanges re-list adjusted strikes after
  an action) — the option-chain adjustment is its own sub-problem and is explicitly
  harder than the spot series.

### 4.2 The point-in-time symbol-identity problem

Today's resolution keys (`by_exchange_symbol`, `groww_symbol_by_pair` —
DEC-GROWW-HYPHEN-SYMBOL-RESOLVE-001) carry **no listing epoch**. Tickers are REUSED
after delisting and RENAMED in place (e.g. ETERNAL née Zomato); a bare
`(exchange, trading_symbol)` join across years can stitch two different companies into
one price series — a corruption no adjustment factor can repair.

- **ISIN is the rename-proof join key** — but honestly, **ISINs also change** on some
  corporate restructurings (demergers, certain re-listings), so ISIN alone is not a
  permanent identity either.
- Proposed identity: **`(ISIN, listing_epoch)`** — a listing epoch opens when an ISIN
  first appears on the exchange and closes on delisting/ISIN-change; the adjustment
  table and the lake partitions key on this pair. Epoch boundaries themselves come from
  the corporate-actions source and are stored with citations.

### 4.3 Candidate data sources — verification status stated honestly

No probe is possible from this sandbox (egress 403); **every row below is UNVERIFIED**
(Golden Rule #1 — no fabricated schemas, endpoints, or field lists):

| Source | What it should provide | Status |
|---|---|---|
| NSE corporate-actions disclosures (nseindia.com) | ex-dates, ratios, action types | **UNVERIFIED** — no schema/endpoint claimed |
| NSE/BSE equity bhavcopy series metadata | daily series/face-value observations to cross-check ex-dates | **UNVERIFIED** |
| Exchange circulars (NSE/BSE) | authoritative ratio + record-date text | **UNVERIFIED**; likely unstructured |
| Groww instrument master (`instrument.csv`, §6) | current ISIN per symbol — a join aid, NOT a history | Master itself verified in repo; its historical coverage **UNVERIFIED** |

A Phase-0 style probe (the ADR-0009 pattern) must land transcripts before any source is
encoded as fact.

### 4.4 Verification pyramid for the layer (when built)

Golden fixtures from 3-5 REAL, citation-backed actions (once verified); a property test
that adjusted series are cliff-free at ex-dates while raw series are untouched;
byte-identity of every existing surface for the 3 indices (the layer must be a no-op for
index symbols); 100% line+branch per CLAUDE.md §12.

## 5. Consequences

- Positive: single-stock expansion becomes IMPOSSIBLE to do accidentally; the failure
  mode changes from "silently corrupt results" to "clean refusal naming this ADR".
- Negative: none today — no production path sweeps a single stock (grounded in §6).
- The two READ-surface mis-statements (§2) remain until the layer lands; they are
  REFERENCE_ONLY display surfaces, named here so they are debt-with-an-owner, not
  unknown debt.

## 6. ADR-0012 NIFTY-futures interaction

ADR-0012 authorizes the engine on **NIFTY futures** for the dynamic-strategy track.
Futures need no split adjustment (an index future tracks the divisor-adjusted index),
so the gate does not conflict with ADR-0012. **THIS OBLIGATION IS SATISFIED
(2026-07-06, DEC-FUTURES-ADDRESSING-PIN-001 + DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001,
MIGRATION.md #1521–#1522):** the launch PR pinned the final addressing shape — the ONE
series alias `NSE-NIFTY-FUT` — in `brutex.universe.is_engine_sweep_symbol` AND assigned
its `symbol_id` (4) in `brutex/strategies/spec.py` in the SAME reviewed PR, exactly as
required below. The pre-launch dual-acceptance envelope (the
`FuturesContract.series_key` form `NIFTY-FUT-YYYY-MM-DD` OR a `parse_fno_symbol`-
decodable `NSE-NIFTY-<DDMmmYY>-FUT` lake contract symbol) is RETIRED — both shapes are
now refused with a did-you-mean hint naming the alias — and the sweep path resolves the
alias roll-aware via `brutex/data/futures_series.py`. Historical requirement text
(recorded as written pre-launch): the futures-sweep launch
PR MUST pin its final addressing shape in `brutex.universe.is_engine_sweep_symbol` in
the same reviewed PR that assigns its symbol id.

## 7. References

- CLAUDE.md §2 Golden Rules #1, #3, #10; §6 (verified Groww facts)
- MIGRATION.md #1269 (this decision), #1178 (DEC-ENGINE-SCOPE-GUARD-001 — the
  implemented gate), #200 (DEC-P0-063), #694 (NTM), #1006 (ADR-0012)
- 2026-07-03 audit finding #5 (zero corporate-action code, grep-verified)
- `brutex/engine/sweep_scope.py` + `brutex/universe.py` (the shipped gate),
  `tests/unit/test_engine_sweep_scope.py`, `tests/unit/test_universe.py`,
  `tests/integration/test_cli_sweep_scope.py` +
  `tests/integration/test_cli_sweep_scope_guard.py` (the extension suite)
