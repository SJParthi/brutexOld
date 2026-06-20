# BruteX — Claude Code Project Rules

> Persistent instructions for Claude Code working in this repository.
> Every Claude session in `bruteX/` must read and honor this file.
>
> **Authority order:**
> 1. This file (`CLAUDE.md` — 12 Golden Rules)
> 2. Defaults
>
> The previous DEC-PROTOCOL-001 layer (tickvault adaptation doc +
> auto-loaded protocol bundle) remains RETIRED (DEC-P0-026, PR #48).
> The ONLY co-development surface re-established since is the shared
> `rust/trading-core/` crate — the single source of truth for the
> evaluate-time indicators (pivots + Fibonacci) + the 52-condition
> vocabulary, consumed bit-identically by BOTH BruteX (via the PyO3
> `trading_core` wheel) and TickVault (native Rust) per
> DEC-SHARED-CORE-001 (MIGRATION.md #441). The frozen 400-case golden
> answer-key is the contract both sides satisfy; no other
> adaptation/protocol coupling exists.

_Last updated: 2026-06-20 (DEC-LIVE-REMOVE-001, MIGRATION.md #860 — the **entire
LIVE feed feature is REMOVED / retired in full**: the `brutex/live/` capture package
+ its `python -m brutex.live` CLI, the dashboard `/live` page + `/api/v1/live/*` +
`/api/v1/db/live/*` routes + the `brutex/dashboard/live_*` surface, the
`live_ticks`/`live_candles_1m` lake tables, the Movers "Live" tab, the /db "Live
lake" section, the nav `/live` link, and the now-orphaned live leg-resolution
`adapters/groww/contract_pull.py` (47 files). This aligns with §1 which already lists
"Live feed, WebSocket" as OUT OF SCOPE; the §1 out-of-scope note + active-branch line
are updated. DELIBERATELY KEPT (the `live` = real/production naming trap, NOT the
feed): `adapters/groww/live.py` `build_live_client`/`build_live_pool` (the historical
fetch / metadata / historical pull client builder), `cli.py::_build_live_groww_spot`,
and `pull_controller.py::_build_live_controller`/`SessionRateController` (the
PRODUCTION dashboard-pull rate controller — the §6 rate-controller rows are UNCHANGED,
they describe the production pull, not the feed) — so the historical/backtest pull +
`rate_common` + the brute-force ENGINE are UNTOUCHED. Earlier: 2026-06-20
(DEC-OPTIONS-COMPLETENESS-REAL-YEARS-001, MIGRATION.md #859 — the `/completeness`
OPTIONS table shows only the years that ACTUALLY have expiries + an honest
`partial` for late-listed underlyings (no fake 2020→2026 span, no perpetual
`growing`); read/rollup-side only, no pull/engine change). Earlier: 2026-06-20
(DEC-1MINUTE-ONLY-DERIVE-1DAY-001, MIGRATION.md #853 —
§1/§3/§6 facts: **1-minute is now the ONLY granularity fetched from Groww** (spot
+ futures + options); `1day` (PDH/PDL anchor) + the 19 intraday TFs are DERIVED
from the stored 1-minute and written to the lake (`spot_daily_resample`), never
separately vendor-fetched. This supersedes the operator-chosen granularity dropdown
of DEC-PULL-1DAY-FIRST-001 (#723, removed) + re-introduces the 1m→1d derivation
removed by DEC-REALDATA-ONLY-NO-1M-TO-1D-001 (#852) as the always-on PRIMARY path.
The "Verify vs NSE" bhavcopy cross-check is REMOVED in full; the F&O historical
DEFAULT window is per-contract `expiry − 3mo → expiry`. The brute-force ENGINE stays
NIFTY/BANKNIFTY/SENSEX. Earlier: 2026-06-19 (DEC-CLAUDEMD-ADAPTER-CITE-RECONCILE-001,
MIGRATION.md
#846 — §6 symbol-resolution rows refreshed after the Track-1 data-quality fix
series #799/#800/#801 merged: the `Symbol/key format` + `Track 1 brute-force
universe` rows now cite the master-authoritative `by_exchange_symbol` +
`groww_symbol_by_pair` resolution (DEC-GROWW-HYPHEN-SYMBOL-RESOLVE-001 #845, which
resolves hyphenated `BAJAJ-AUTO` + verbose-symbol `MIDCPNIFTY`) instead of the
stale "via `by_groww_symbol`" wording + drifted `adapter.py` line citations; the
`Historical OHLC` row's `adapter.py` line pointer refreshed to the relocated
`fetch_ohlcv`. No substantive §6 fact changed — the merged trio (1-day resample
#842, until-clean-all-outstanding #844, symbol-resolve #845) is lake/adapter/ingest
only, the brute-force ENGINE stays NIFTY/BANKNIFTY/SENSEX). Earlier: 2026-06-13
(DEC-PULL-1DAY-FIRST-001, MIGRATION.md #723 —
§1/§6 spot-interval facts: the spot pull granularity is now operator-chosen
per pull via the `/universe` dropdown with a VERIFY-FIRST **1-day-only**
default (`resolve_fetch_intervals`; 1-minute / both after the
OHLCV-vs-bhavcopy cross-check), no longer force-fetching 1-minute alongside
1-day). Earlier: 2026-06-13 (DEC-PULL-FLOOR-2020-001, MIGRATION.md #722 —
§3 locked scope range moved `2021-01-01 → today` → `2020-01-01 → today`:
the DATA pull floor (`GROWW_HISTORY_FLOOR`) lowered to the full
probe-verified Groww 1-minute depth, superseding the conservative
2021-01-01 floor of DEC-DHAN-PULL-SCOPE-001). Earlier: 2026-06-12
(DEC-CLAUDEMD-SPEC-LINE-RECONCILE-001,
MIGRATION.md #669 — renumbered from #667 by PR #629's duplicate-repair;
§2 Rule 4's run-identity citation refreshed
`TRACK1_BACKTEST_SPEC.md:654` → `:743`; spec growth had moved the
blake3 formula block and the stale line pointed mid-section). Earlier:
2026-06-10 (DEC-AUTOMERGE-CLEAN-STRICT-001, MIGRATION.md
#612 — §2 Rule 13(a) `mergeable_state` invariant aligned with the
charter §4.5 pre-G2 envelope per DEC-WORKSPACE-VERIFY-002 follow-up
F-4). Earlier: 2026-06-10 (DEC-WORKSPACE-VERIFY-002, MIGRATION.md #609 —
§6 prior-vendor `constants.py` line citations refreshed after the
5-agent workspace verification found 4 rows drifted by 7 lines; §1/§3
active branch refreshed). Earlier: 2026-06-03 (DEC-SHARED-CORE-001 Phase 2b,
MIGRATION.md
#441 — BruteX's 52-condition vocab now delegates to the shared
`rust/trading-core/` crate; §1/§5 reconciled). Earlier: 2026-06-01
(DEC-DASHBOARD-OMIT-AUDIT-001 — vendor docs reconciled Groww → the prior
vendor after the 5-agent workspace verification found §1/§6 stale).
DEC-P0-026
(PR #48) retired the tickvault co-development layer; DEC-SHARED-CORE-001
then re-established a single shared `trading-core` crate (indicators +
52-vocab) consumed by BruteX + TickVault. Vendor history: broker → Groww
migration (2026-05-15), Groww → prior-vendor pivot proposed then
reverted (ADR-0002, 2026-05-31 — the prior-vendor revert, now
superseded), then a full
re-migration back to Groww — **DEC-DHAN-GROWW-REMIGRATE-001 (2026-06-12,
ADR-0009) supersedes the revert and is authoritative: the live vendor is
Groww (the prior-vendor adapter package + reference docs were removed in
the ADR-0009 §5 cutover PR series).** Track 1 spec locked after 4-agent
hostile-review pass._

---

## 1. Project Identity

- **Name:** BruteX
- **Purpose:** Extreme drill-down brute-force backtesting engine for
  Indian **spot indexes** (NIFTY 50 / BANKNIFTY / SENSEX) + **India
  VIX** (volatility index). Track 1 is signal-only research; the
  Options Track translates winning signals to options for execution
  realism.
- **Data source (Track 1):** **Groww Trade API** via the `growwapi`
  Python SDK (injected `GrowwAPI` client; TOTP auth) per
  DEC-DHAN-GROWW-REMIGRATE-001 / ADR-0009 (which supersedes the
  ADR-0002 prior-vendor revert). One unified surface for
  spot AND options:
  1. `groww.get_historical_candles(...)` — fetch **`1minute` ONLY** for
     spot (DEC-1MINUTE-ONLY-DERIVE-1DAY-001, MIGRATION.md #853): 1-minute is
     the SINGLE granularity fetched from Groww, and `1day` (the PDH/PDL daily
     anchor) + the 19 intraday timeframes are **DERIVED deterministically from
     the stored 1-minute**, never separately vendor-fetched. The derived `1day`
     is resampled to midnight-IST daily bars (`brutex/data/spot_daily_resample.py`)
     and written to the lake's 1-day partition after every pull. This supersedes
     the operator-chosen granularity dropdown of DEC-PULL-1DAY-FIRST-001 (#723,
     removed) and re-introduces the 1m→1d derivation removed by
     DEC-REALDATA-ONLY-NO-1M-TO-1D-001 (#852) — now as the always-on PRIMARY
     path, since 1-minute is the sole fetched granularity (the derived 1-day is
     openly labelled derived, not vendor-official; Golden Rule #1).

  Expired options + futures (Options Track / lake) use the same
  `get_historical_candles` after resolving contracts via
  `groww.get_expiries(...)` → `groww.get_contracts(...)`, also at **`1minute`
  ONLY** with a DEFAULT window of per-contract `expiry − 3 calendar months →
  expiry` (explicit operator dates override); the F&O `1day` is likewise
  DERIVED from the pulled 1-minute (DEC-1MINUTE-ONLY-DERIVE-1DAY-001). See §6
  for the full verified fact table.
- **Out of scope (Track 1 engine):** Live feed, WebSocket, order
  placement, portfolio, funds, multi-user SaaS. (Per DEC-P0-063 the LAKE
  additionally ingests F&O single-stock spot+options as REFERENCE_ONLY;
  the BRUTE-FORCE ENGINE remains the 3 indexes — see §2 Rule 3.) The dashboard
  + lake **LIVE-feed SURFACE that once existed** (`brutex/live/`, the `/live`
  page + `/api/v1/live/*` routes, the `live_ticks`/`live_candles_1m` tables, the
  Movers "Live" tab + /db "Live lake" section) was **REMOVED in full —
  DEC-LIVE-REMOVE-001 (MIGRATION.md #860)** — bringing the codebase in line with
  this out-of-scope rule. (The `live` = real/production naming-trap survivors —
  `adapters/groww/live.py` `build_live_client`/`build_live_pool`,
  `pull_controller._build_live_controller` — are the HISTORICAL pull's client +
  rate controller, NOT the feed, and stay.)
- **Active branch:** `claude/relaxed-mccarthy-qacssf` (REMOVE the entire LIVE feed
  feature — DEC-LIVE-REMOVE-001, MIGRATION.md #860: deletes the `brutex/live/`
  capture package + its CLI, the dashboard `/live` + `/api/v1/live/*` +
  `/api/v1/db/live/*` routes + `brutex/dashboard/live_*` surface, the
  `live_ticks`/`live_candles_1m` lake tables, the Movers "Live" tab, the /db "Live
  lake" section, the nav `/live` link, and the now-orphaned live leg-resolution
  `adapters/groww/contract_pull.py` + `db_view.format_clean_ist_millis`; 47 files.
  The historical/backtest pull + `rate_common` + the brute-force ENGINE are
  UNTOUCHED — `_build_live_controller`/`build_live_client` are the production pull's
  rate controller + client, KEPT. Prior merged: DEC-OPTIONS-COMPLETENESS-REAL-YEARS-001
  #859 (PR #813) + DEC-UNIVERSE-CONSTITUENTS-TICK-IN-PLACE-001 #858 (PR #812) +
  DEC-FNO-EQ-TOKEN-JOIN-PERSIST-001 #857 (PR #811) + DEC-TF-PRECISION-PROOF-001 #856 (PR #810))

---

## 2. Golden Rules (never violate)

1. **No hallucination.** Every factual claim about Groww, NSE, BSE, NIFTY
   composition, brokerage, taxes, or any external system must be
   traceable to a verified source. If unsure, say "unverified" and stop.
2. **Single source of truth.** All Track 1 specs live under
   `docs/TRACK1_*.md` + `docs/track2/TRACK2_*.md`. Every PR references
   a section + decision ID from `docs/MIGRATION.md`. The legacy
   `docs/REQUIREMENTS.md` is a retired stub (banner only) — do not
   read or derive new requirements from it.
3. **Scope lock.** Track 1 = Groww + spot indexes + India VIX (NIFTY 50,
   BANKNIFTY, SENSEX, INDIA VIX). **India VIX is REFERENCE ONLY** — it
   is fetched + stored and stamped as `vix_at_entry` / `vix_at_exit` on
   every trade, but is NEVER part of brute-force pattern combinations
   or the 52-condition vocabulary. 21 timeframes — **`1m` is the ONLY
   granularity fetched** and the 19 derived intraday TFs + `1d` are all
   DERIVED from it (DEC-1MINUTE-ONLY-DERIVE-1DAY-001, MIGRATION.md #853; the
   `1d` daily anchor is resampled to midnight-IST from the stored 1-minute and
   written to the lake's 1-day partition, never separately vendor-fetched).
   Rolling `2020-01-01 → today` range (the data pull floor
   — `GROWW_HISTORY_FLOOR`; DEC-PULL-FLOOR-2020-001, 2026-06-13, MIGRATION.md
   entry #722 — supersedes the prior conservative `2021-01-01` floor of
   DEC-DHAN-PULL-SCOPE-001 now that the Phase-0 granularity probe confirmed
   Groww serves 1-minute data from `2020-01-01T09:15:00`). No futures, no
   options, no F&O contracts. Do not silently expand scope.

   **Scope expansion (DEC-P0-063, 2026-05-22, MIGRATION.md entry
   #200):** Track 1's INGEST scope was formally expanded to additionally
   cover **200 NSE F&O eligible single stocks (spot + options across the
   vendor's expiry/contract addressing — Groww `get_expiries` →
   `get_contracts`)** for the LAKE. The **BRUTE-FORCE ENGINE is
   UNCHANGED** — it still operates only on the 3 brute-force indexes
   (NIFTY/BANKNIFTY/SENSEX); the 200 stocks join India VIX in
   `REFERENCE_ONLY` (mirrors decision #83). Stock options sit in the
   lake ready for future brute-force expansion (separate decision; not
   authorized here). The "4 index instruments / no F&O contracts"
   wording above describes the ENGINE; the LAKE is the expanded surface
   per DEC-P0-063. The §3 / cross-spec stale-text reconciliation is
   COMPLETE (DEC-P0-063-RECONCILE-COMPLETE-001 — a very-thorough survey
   found the "~15" estimate stale: the DEC-SPEC-CONSISTENCY series
   #262-#266 had already reconciled 11 locations; the 2 genuinely-stale
   remainders were fixed and 1 immutable ADR record correctly left
   as-is). DEC-P0-063 remains authoritative for the stock-options
   expansion.

   **Amended by DEC-INSTRUMENT-UNIVERSE-FAST-001 (2026-05-29, MIGRATION.md
   entry #160):** lean ingest — the `instruments` table now persists ONLY
   the ~241 unique universe underlyings (every admitted index + every
   F&O-eligible single-stock equity); the ~89,000 FUT/CE/PE legs are
   PARSED for eligibility then DISCARDED, recoverable on demand via
   `get_contracts` (contracts inherit lot/tick/freeze from the underlying
   row). The BRUTE-FORCE ENGINE is UNAFFECTED. So "the LAKE is the expanded
   surface" is narrowed: the lake = the universe underlyings; the legs are
   derived/recoverable, not stored.

   **Leg-economics fold (DEC-GROWW-LEG-ECONOMICS-001, 2026-06-13, MIGRATION.md
   entry #696):** before the legs are discarded, each persisted underlying is
   **stamped with its tradable F&O lot + freeze** harvested from its derivative
   legs (`universe.build_underlying_economics` →
   `dashboard/instrument_loader._fold_leg_economics`) — RELIANCE → 500, NIFTY →
   75 — SUPERSEDING the IDX/EQ row's cash placeholder (`lot_size` 1 / 0); the
   spot `tick_size` is preserved (a spot tick differs from its future's). This is
   the Groww twin of the retired-vendor fold; without it every F&O equity kept
   the placeholder `lot_size = 1`, which left `has_placeholder_stock_economics`
   permanently True and made the dashboard auto-heal re-load the master on every
   startup (a silent loop this closes).

   **Re-amended by DEC-NTM-GROWW-REPOINT-001 (2026-06-13, MIGRATION.md entry
   #694):** the lean universe ALSO persists the matched **NIFTY Total Market
   constituents** as REFERENCE_ONLY rows — the ~540 non-F&O names on top of the
   ~212 F&O-eligible stocks + the admitted indices, widening the lake to **~750
   underlyings**. The constituents are fetched best-effort from niftyindices
   (vendor-agnostic) and matched to Groww NSE cash-equity rows by ISIN / ticker
   (`dashboard/instrument_loader.py` `_ntm_admission`). The **BRUTE-FORCE ENGINE
   remains UNCHANGED** (NIFTY/BANKNIFTY/SENSEX) — NTM constituents are
   lake/REFERENCE_ONLY only, NEVER in the 52-condition vocabulary (the hard
   scope guard; an F&O-eligible name that is ALSO an NTM constituent stays
   tradable, `reference_only=FALSE`). On a network miss with no cache the load
   degrades honestly to the F&O-eligible base set, never invents constituents.
4. **Reproducibility first.** Every run is identified by `blake3(
   strategy_mask ‖ direction ‖ instrument ‖ tf ‖ mode ‖ canonical_params
   ‖ data_snapshot_hash ‖ indicator_version ‖ code_commit_hash)`. No
   computation without that hash recorded. (Was `sha256(...)` pre-DEC-CALIBRATE-001;
   corrected 2026-05-24 to match the spec's run-identity formula — now at
   `TRACK1_BACKTEST_SPEC.md:743` after spec growth moved it from :654
   (DEC-CLAUDEMD-SPEC-LINE-RECONCILE-001) — which has used blake3 since
   the v2 spec lock 2026-05-16. See MIGRATION.md entry #204.)
5. **O(1) discipline.** Bar lookup, indicator value lookup, combo dedup
   (Bloom + LMDB), strategy mask evaluation (u64 AND), and result
   append must all be O(1). CI gates: `p99(N=1M) / p99(N=1K) ≤ 1.1`
   for each hot path. Any regression fails CI.
6. **Rate-limit safety.** Groww publishes type-level limits (the
   **"Live Data"** group — Market Quote / LTP / OHLC — is **10 r/s,
   300/min**; `docs/groww/api-reference/01-introduction.md:130-134`).
   The **backtesting `get_historical_candles` per-second cap + daily
   quota are UNDOCUMENTED** and are an OPEN Phase-0 probe gate
   (`brutex/adapters/groww/constants.py:112-118`
   `GROWW_RATE_LIMIT_RPS_UNVERIFIED=10.0`,
   `GROWW_DAILY_QUOTA_UNVERIFIED=None`; ADR-0009 §3.1 risk #4 / §4) — the
   `_UNVERIFIED` suffix carries the honesty requirement in the
   identifier. The adapter paces with the vendor-neutral adaptive
   token-bucket controller (`brutex/adapters/rate_common.py:136`
   `RateController`, injected `ceiling_rps`; the 429/timeout ladders step
   down to a deep floor of **1 r/s** under sustained pressure and recover
   up to the ceiling once it clears). On TOP of the per-second bucket the
   controller now runs an **operator-confirmed 500 req/min HARD governor**
   (a rolling-60s issue-count cap, injected `ceiling_per_min` =
   `GROWW_RATE_LIMIT_PER_MIN=500`; **no daily quota** for backtesting —
   both operator-confirmed 2026-06-13, NOT vendor-documented;
   DEC-GROWW-RATE-PERMIN-001). The governor only ever LOWERS the issue
   rate (it caps; it never raises a ceiling), so it is a safe addition;
   never raise the per-second ceiling OR the per-minute cap without
   explicit user approval. The PRODUCTION per-second ceiling injected at
   BOTH live build sites (dashboard `_build_live_controller` + CLI live
   pull) is now the operator-DIRECTED **8 r/s**
   (`GROWW_RATE_LIMIT_RPS_CEILING=8.0`, operator-approved 2026-06-14,
   DEC-GROWW-RATE-CEILING-8-001) — chosen because 8×60 = 480/min stays just
   under the 500/min governor, so the per-second ceiling is the binding gate
   without overshooting the per-minute cap. This is the satisfied "explicit
   user approval" gate to raise the ceiling; the per-second backtesting cap
   itself stays officially UNVERIFIED (`GROWW_RATE_LIMIT_RPS_UNVERIFIED`) and
   the adaptive ladder still steps DOWN on 429s. (The vendor-neutral
   `rate_common.CEILING_RPS=5` DEFAULT is unchanged.)
7. **Idempotence.** Every ingest chunk and every backtest combo must be
   rerun-safe. Same inputs → same outputs, bit-for-bit. The audit-log
   replay test is the oracle.
8. **Audit trail.** Every material action (ingest chunk, combo run,
   error, kill) appends one line to a hash-chained JSONL audit log.
   Breaking the chain fails CI.
9. **Hostile review before code.** Major design changes get a
   parallel-agent hostile review (Plan + general-purpose + Explore)
   before implementation. See the 4-agent pattern in
   `docs/TRACK1_PHASE_PLAN.md §0`.
10. **Honest limits.** If an SLA cannot be met under the current
    hardware envelope, SAY SO. No "millions in minutes" claims without
    naming the hardware AND citing public benchmarks. The 4-agent
    review on 2026-05-15 caught a 10x-overoptimistic throughput claim;
    do not repeat that pattern.
11. **COMMON · DYNAMIC · SCALABLE · AUTOMATED** (operator decree 2026-05-16).
    Every module obeys all 5:
    - **COMPREHENSIVE** — streaming + batch interface, bit-exact oracle
      test, look-ahead invariant test, serialize/restore.
    - **COMMON** — one source of truth (this repo). Every utility
      lives in exactly one place; downstream modules import. No
      copy-paste between modules.
    - **DYNAMIC** — add new indicator/TF/condition/mask without
      re-architecting. Plugin-style state classes, u64 mask vocab.
    - **SCALABLE** — same code path Day 1 (12.6M) → Phase 10 (730B).
      O(1) per-bar / per-mask / per-dedup / per-write.
    - **AUTOMATED** — 100% automated after one-time bootstrap.
      Release Please + auto-merge + watchdog + Telegram + auto-resume.
      Operator monitoring: ZERO clicks per day.
    See `docs/TRACK1_BACKTEST_SPEC.md` §"COMMON · DYNAMIC · SCALABLE ·
    AUTOMATED" for the full rule + enforcement matrix + violations list.
12. **100% ALWAYS STANDARDS** (operator decree 2026-05-16 evening).
    Every branch, every PR, every sub-PR, every commit, every task,
    every sub-task, every plan, every doc, every spec, every agent run
    obeys 18 standing requirements:
    coverage 100% line + 100% branch; mutation ≥ 85%; type-check
    (mypy strict AND pyright); static analysis (ruff + pylint + bandit);
    zero duplication; functionality + scenario completeness; bug-fix
    regression coverage; 7 test types (unit + integration + property +
    golden + bench + stress + mutation); CI every push + nightly soak;
    O(1) per hot path (p99 ratio ≤ 1.1); throughput canary ≥ 50 M/s;
    Bloom + LMDB 4-tier dedup; audit JSONL hash-chain on every
    material action; observability 4 pillars (tracking + logging +
    dashboard + alerting); 7 explicit SLAs; deep-research protocol
    (4-agent for major, 2-agent for compliance, 1-agent hostile for
    refactor); decision ID for every locked decision in MIGRATION.md.
    See `docs/STANDARDS_100PCT.md` for the full enforcement matrix +
    tooling per gate + auto-driver-test discipline. Any PR with a
    red gate is BLOCKED; operator override requires ADR.

    **Honest envelope (Golden Rule #10 + DEC-P0-040 / DEC-P0-042
    rebaselining):** The 18-requirement decree above is the **target**.
    The actual CI enforcement matrix in `docs/STANDARDS_100PCT.md` §1
    is **16 of 21 sub-rows 🟢 ENFORCED + 1 🟡 PARTIAL + 1 📋 ROADMAP +
    3 👁️ MANUAL** (~76% fully enforced, per the summary count in
    `docs/STANDARDS_100PCT.md` §1, post-DEC-COV-CLI-HARNESS-001
    2026-06-12 — coverage rows 1/2 flipped 🟢 when the omit list was
    deleted at a measured package-wide 100.00% line+branch; the one
    🟡 is row 3 pending DEC-MUTATION-RUNTIME-002's verification run.
    Earlier: row 3 honestly re-classified 🟡 while the orchestrator
    mutation pass exceeded its weekly budget, DEC-MUTATION-RUNTIME-001;
    the prior "15/2 (~71%)" wording here was the pre-re-classification
    snapshot, caught by the 2026-06-11 honesty audit per this file's own
    that-doc-wins rule and reconciled by DEC-HONESTY-AUDIT-RECONCILE-001).
    The earlier "9 of 21 / ~22%" wording here was a stale 2026-05-16
    snapshot from before the DEC-STANDARDS-AUDIT-001 → DEC-STRESS-001
    ratchet arc (audit + CROSSWALK + PYLINT-DROP + DOCTOR + STRESS
    closed 6 rows); reconciled by DEC-DOC-STANDARDS-RECONCILE-001
    (MIGRATION.md entry #591). `docs/STANDARDS_100PCT.md` §1 remains the
    single source of truth — if the two ever disagree, that doc wins and
    this line is the stale copy to fix. Per Golden Rule #10 no PR
    may claim "100% standards compliant" without first auditing against
    that matrix. The decree-vs-reality gap is a living ratchet, not a
    bypass — every gap row tracks a dedicated follow-up PR in
    MIGRATION.md. PRs that touch a gap row MUST update the row's
    status. See `docs/TRACK1_REQUIREMENTS_FINAL.md` §2 (DEC-P0-040)
    for the full enumeration.

13. **MERGE-READINESS CHARTER** (operator decree 2026-05-29,
    DEC-MERGE-GUARANTEE-001). Every PR / sub-PR / task / sub-task — in
    this session and every new session — obeys
    `docs/MERGE_READINESS_GUARANTEE.md`: the priority law
    (Correctness > Reliability > Recoverability > Observability >
    Performance > Scalability), the §3 merge-ready gate matrix, and the
    §4 Definition of Done. Two hard invariants: (a) **never merge over a
    red or pending gate** — every ✅ gate green, verified not assumed,
    and `mergeable_state` within the charter §4.5 envelope (strict
    `clean` is the G2-gated target; until server-side branch protection
    lands, `dirty`/`behind`/`blocked`/`unknown` are refused and
    pending-only `unstable` is tolerated —
    DEC-AUTOMERGE-CLEAN-STRICT-001); (b) **honesty** — per Golden
    Rule #10, never claim "100% / fully / guaranteed" for a 🟡/📋/🚫
    tier row; state what is green, partial, and out of scope. The
    charter is enforced on every surface via §8 (this file for sessions,
    `.github/PULL_REQUEST_TEMPLATE.md` for PRs, the `CI OK` required
    check for merges).

---

## 3. Branching and Commits

- **Active feature branch:** `claude/relaxed-mccarthy-qacssf`
- **Never push to `main`** without explicit user instruction.
- **Commit style:** Conventional Commits.
  - `docs(track1): add 52-condition vocabulary appendix`
  - `feat(adapter/groww): implement intraday OHLC chunking`
  - `fix(engine): O(1) regression in bar lookup`
  - `test(property): add DSR effective-N invariant`
  - `chore(claude): update CLAUDE.md rules`
- **Never skip hooks** (`--no-verify` is forbidden unless the user
  explicitly requests).
- **Never force-push** shared branches.
- **Never amend** published commits; always create new commits.

---

## 4. Coding Standards (when code phase starts)

- **Language:** Python 3.12 primary; Rust (PyO3) for hot kernels; no
  other languages without ADR.
- **Formatters:** `ruff format` (not black); `ruff check --fix`.
- **Types:** `mypy --strict` AND `pyright` both must pass.
- **Linters:** `ruff`, `pylint` (warnings-as-errors), `bandit` (security).
- **Tests:** `pytest`, `hypothesis` (property), `pytest-benchmark`
  (SLA), `mutmut` (mutation), `cargo test`, `proptest`,
  `cargo-mutants`, `cargo fuzz`.
- **Coverage gates:** 100% line + 100% branch on `brutex/` package.
  Mutation score ≥ 85% (Python AND Rust).
- **Docstrings:** every public symbol. NumPy style.
- **No `print`** — always `structlog`.
- **No bare `except`** — specific exceptions only.
- **No global mutable state** — inject dependencies.
- **No `pandas` in hot paths** — use `polars` or `numpy` + `numba`
  or Rust.
- **No look-ahead** — at bar N, the engine may only access bars 0..N.
  Enforced TODAY by TWO complementary runtime guards: (1) the
  feed-boundary guard (`ReplayFeed.ensure_no_lookahead`, raises
  `LookAheadError`; wired into the strategy runner + execution driver,
  integration-tested) — a *timestamp*-based check on emitted signals; and
  (2) the named **Property P-06 (instrumented bar accessor)** — now LIVE
  per **DEC-LOOKAHEAD-BARRIER-001** (`brutex/feed/lookahead.py`
  `LookAheadBarrier`): an *index*-based accessor that records every bar
  index `evaluate(N)` touches and raises `LookAheadError` the moment an
  access reaches index > N. It is active automatically on every sweep eval
  (the runner/driver build a default `record=False` O(1)-memory barrier) and
  rechecked by hand via `brutex verify-lookahead`. This is the **Phase-2
  strengthening** of the feed-boundary guard — belt-and-suspenders, NOT a
  perf path; the accessor is O(1) per bar access (NOT an O(1)-total claim).
  Promoted from the prior `pytest.skip` ROADMAP stub (was corrected per
  DEC-WORKSPACE-VERIFY-001 entry #398 D2 / DEC-DOC-DRIFT-RECONCILE-001).
- **Money display = 2 decimals, money-only** (DEC-MONEY-2DP-001) — every
  paisa→rupee display goes through `brutex.money` (`format_inr` /
  `round_money` / `paisa_to_rupees`). NEVER round statistical metrics (Sharpe,
  DSR, p-values, CPCV) or `iv` — they keep full precision or strategy ranking
  breaks. Money amounts (costs + display) are int64 paisa; OHLC/spot/option
  **prices** are stored as Decimal rupees (Parquet Float64), NOT paisa — and
  **snapped to the 2-decimal tick grid at the lake-write boundary**
  (`round_money`, `ROUND_HALF_UP` — never truncation; spot mirrors options) so
  the lake never holds vendor float-noise like `14020.7999`
  (DEC-SPOT-PRICE-2DP-001). See §6 (DEC-PAISA-DOC-RECONCILE-001).

---

## 5. Directory Conventions

```
bruteX/
├── CLAUDE.md                       ← this file
├── README.md
├── .claude/
│   ├── settings.json
│   └── agents/
├── docs/
│   ├── TRACK1_BACKTEST_SPEC.md     ← Track 1 master spec
│   ├── TRACK1_VERIFICATION_PYRAMID.md
│   ├── TRACK1_VOCABULARY.md        ← 52 atomic conditions
│   ├── TRACK1_PHASE_PLAN.md        ← 8-12 week build plan
│   ├── TRACK1_PERFORMANCE_BUDGET.md
│   ├── TRACK1_MICROSTRUCTURE.md
│   ├── TRACK1_DASHBOARD_DESIGN.md  ← v29 locked design spec + data contract
│   ├── dashboard/mockups/          ← v28.html + v29.html (canonical mockups)
│   ├── MIGRATION.md                ← decision ledger
│   ├── REQUIREMENTS.md             ← RETIRED (stub)
│   ├── REQUIREMENTS_SIMPLE.md      ← RETIRED (stub)
│   ├── ROADMAP.md                  ← RETIRED (stub)
│   ├── adapters.md                 ← RETIRED (stub)
│   ├── adr/                        ← architecture decisions
│   └── runbooks/
├── brutex/                         ← Python package
│   ├── adapters/groww/             ← Groww Trade API adapter (growwapi SDK)
│   ├── calendars/                  ← NSE/BSE trading calendar
│   ├── config/
│   ├── costs/                      ← brokerage/tax cost models
│   ├── dashboard/                  ← FastAPI UI
│   ├── data/                       ← Parquet + DuckDB lake (generic)
│   ├── engine/                     ← brute-force combo/mask sweep engine
│   ├── execution/                  ← signal-only simulator (Track 1)
│   ├── feed/                       ← replay + resampler
│   ├── indicators/                 ← evaluate-time indicators (pivots/fib/EMA)
│   ├── math/                       ← thin facade to Rust hot kernel
│   │                                 (fno_math + trading_core re-export;
│   │                                 DEC-STRUCTURE-CLEANUP-001)
│   ├── metrics/                    ← Sharpe/DSR/SPA/CPCV statistics
│   ├── notifications/
│   ├── observability/
│   ├── options/                    ← expiry/strike/lot-size rules (lake)
│   ├── orchestrator/               ← FSM + audit + rate limiter
│   ├── ranking/                    ← composite ranking + rejection gates
│   ├── strategies/                 ← 52-vocabulary engine (Phase 3)
│   └── vocabulary/                 ← 52-condition evaluator (→ trading_core)
├── rust/                           ← Rust hot kernels (shared core + compat)
│   ├── fno-math/                   ← compat types + NSE calendar; Greeks/
│   │                                 indicators removed in DEC-P0-025
│   └── trading-core/               ← SHARED 52-vocab + evaluate-time
│                                     indicators (pivots/fib); bit-identical
│                                     core for BruteX (PyO3 wheel) + TickVault
│                                     (native) — DEC-SHARED-CORE-001 #441
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── property/                   ← hypothesis + proptest (24 invariants)
│   ├── golden/                     ← byte-equality fixtures
│   ├── bench/                      ← O(1) ratio tests
│   ├── stress/                     ← 24h sustained
│   ├── smoke/                      ← <30s pre-push
│   └── fakes/                      ← test doubles
└── .github/workflows/
```

---

## 6. Verified Groww Facts (cite these, don't re-derive)

> **DEC-DHAN-GROWW-REMIGRATE-001 (2026-06-12, ADR-0009):** full
> re-migration of the Track-1 backtesting data source back to the
> **Groww Trade API** (`growwapi` Python SDK). This SUPERSEDES the
> ADR-0002 prior-vendor revert; the prior-vendor adapter package + its
> reference docs were removed in the ADR-0009 §5 cutover PR series. The
> facts below are repo-sourced: HARD-VERIFIED from
> `brutex/adapters/groww/constants.py` (and sibling modules) + the Groww
> SDK reference at `docs/groww/api-reference/`. See `docs/adr/` ADR-0009
> for the migration rationale + the accepted-risk register. Rows whose value is an OPEN Phase-0 probe
> gate are stamped **UNVERIFIED** (ADR-0009 §3.2 / §4) — never encoded as
> fact in code until a probe transcript lands. Every row traces to
> file:line.

| Fact | Value | Source |
|------|-------|--------|
| Vendor / transport | **Groww Trade API — `growwapi` Python SDK** (injected `GrowwAPI` client; no raw HTTP) | `adr/0009-...-migration.md:43-44`; `groww/adapter.py:1-17` |
| Historical OHLC (spot + options) | `groww.get_historical_candles(exchange, segment, groww_symbol, start_time, end_time, candle_interval)` — single method for all instruments | `groww/api-reference/03-backtesting.md:138-200`; `groww/adapter.py:129` (`fetch_ohlcv` → `fetch_candle_windows`) |
| Options contract resolution | `groww.get_expiries(...)` → `groww.get_contracts(...)` → feed each Groww symbol to `get_historical_candles` (all listed strikes, not relative-to-spot), **`1minute` ONLY** with a DEFAULT window of per-contract `expiry − 3 calendar months → expiry` (explicit operator dates override; `1day` DERIVED from the pulled 1-minute) — DEC-1MINUTE-ONLY-DERIVE-1DAY-001 #853 | `groww/api-reference/03-backtesting.md:41-134`; `adapters/groww/contract_bounds.py` (`default_fno_minute_window`); `dashboard/historical_pull.py` |
| Backtesting segments | `CASH`, `FNO` ONLY (`COMMODITY` excluded; indices ride the `CASH`/`IDX` row, mapped to the neutral `INDEX` segment) | `groww/constants.py:91-95`; `groww/instruments.py:124-136`; `groww/api-reference/03-backtesting.md:8` |
| Instrument types | `EQ, IDX, FUT, CE, PE` | `groww/constants.py:95`; `groww/api-reference/04-annexures.md:100-108` |
| Historical depth | **"available from 2020"** (Equities, Indices, FNO) | `groww/constants.py:106-108`; `groww/api-reference/03-backtesting.md:145` |
| 1-minute granularity at the 2020 boundary | **UNVERIFIED** — Phase-0 probe gate (rows may not truly start Jan-2020 at 1m) | `groww/constants.py:107`; `adr/0009-...-migration.md:113-114` |
| Candle intervals (SDK supports) | `1/2/3/5/10/15/30 minute`, `1hour`, `4hour`, `1day`, `1week`, `1month` | `groww/constants.py:57-70`; `groww/api-reference/04-annexures.md:85-98` |
| Candle interval — Track 1 spot | **`1minute` ONLY fetched** (DEC-1MINUTE-ONLY-DERIVE-1DAY-001 #853 — `resolve_fetch_intervals` always returns `("1minute",)`); `1day` (PDH/PDL anchor) + the 19 intraday TFs are DERIVED from the stored 1-minute (`1day` resampled midnight-IST to the lake's 1-day partition after every pull, `spot_daily_resample.derive_daily_for_targets`), never separately vendor-fetched. The granularity dropdown + the bhavcopy cross-check of DEC-PULL-1DAY-FIRST-001 (#723) are REMOVED | `dashboard/pull_controller.py` (`GROWW_FETCH_INTERVALS`, `resolve_fetch_intervals`, `_resample_daily_after_pull`); `data/spot_daily_resample.py` |
| Chunk window — by interval tier | **1/2/3/5m → 30d · 10/15/30m → 90d · 1h+/1d/1w/1mo → 180d** per call (richer than a single split) | `groww/constants.py:76-89`; `groww/chunks.py:34-60`; `groww/api-reference/03-backtesting.md:238-246` |
| Candle response shape | **ROW-arrays** — `{"candles": [[ts, open, high, low, close, volume, open_interest], ...]}`; OI `null` for non-FNO | `groww/parser.py:1-18,104-124`; `groww/api-reference/03-backtesting.md:202-236` |
| **Price unit** | **RUPEES (float)** from Groww — kept as exact `Decimal` rupees through the lake (Parquet `Float64`); int64 paisa is the cost/display unit only, NOT price storage (DEC-PAISA-DOC-RECONCILE-001) | `data/candle_fields.py` (`to_price`→`Decimal`); `data/models.py:142-144`; `groww/api-reference/03-backtesting.md:202` |
| Timestamp unit | **Native-IST string** `"yyyy-MM-ddTHH:mm:ss"` (or epoch seconds defensively) — localized to IST (fixed +05:30, no DST), stored UTC, displayed IST | `groww/parser.py:33-67`; `groww/constants.py:109-110`; `groww/api-reference/03-backtesting.md:198-207` |
| Symbol/key format | **`groww_symbol` string** `EXCH-SYMBOL[-…]` (e.g. `NSE-NIFTY`, `BSE-SENSEX`) — O(1) resolution: a BARE trading symbol (incl. a hyphenated stock like `BAJAJ-AUTO` / a verbose-symbol index like `MIDCPNIFTY`) resolves via `by_exchange_symbol` + the master's `groww_symbol_by_pair` (the real groww_symbol, never reconstructed); an already-prefixed FUT/CE/PE contract resolves via `by_groww_symbol`. NOT a numeric ID (DEC-GROWW-HYPHEN-SYMBOL-RESOLVE-001, MIGRATION.md entry #845) | `groww/instruments.py:190-201`; `groww/adapter.py:162-174`; `groww/api-reference/02-instruments.md:119`, `03-backtesting.md:10-37` |
| Options instrument type | **`CE` / `PE`** (Call / Put); `FUT` for futures | `groww/constants.py:95`; `groww/api-reference/03-backtesting.md:16-22`; `04-annexures.md:107-108` |
| Exchanges (in scope) | **NSE, BSE** (MCX excluded with the COMMODITY segment) | `groww/constants.py:92`; `groww/api-reference/04-annexures.md:39-43` |
| Rate limit — backtesting | **500 req/min, NO daily quota** (operator-confirmed 2026-06-13, NOT vendor-documented; DEC-GROWW-RATE-PERMIN-001). The per-SECOND cap stays **UNVERIFIED** (`get_historical_candles` r/s UNDOCUMENTED; Phase-0 probe gate); the production per-second ceiling injected at both live build sites is the operator-DIRECTED **8 r/s** (`GROWW_RATE_LIMIT_RPS_CEILING=8.0`, operator-approved 2026-06-14, DEC-GROWW-RATE-CEILING-8-001 — 8×60 = 480/min stays under the 500/min governor; NOT a vendor-verified limit). The published "Live Data" group is 10 r/s · 300/min but is NOT confirmed to cover backtesting | `groww/constants.py:112-118,165` (`GROWW_RATE_LIMIT_RPS_UNVERIFIED=10.0`, `GROWW_RATE_LIMIT_PER_MIN=500`, `GROWW_RATE_LIMIT_RPS_CEILING=8.0`, `GROWW_DAILY_QUOTA_UNVERIFIED=None`); `groww/api-reference/01-introduction.md:130-134` |
| Adapter effective cap | **Vendor-neutral adaptive token bucket** — injected `ceiling_rps` (the production live ceiling is the operator-DIRECTED **8 r/s**, `GROWW_RATE_LIMIT_RPS_CEILING`, DEC-GROWW-RATE-CEILING-8-001; the vendor-neutral DEFAULT stays 5), steps down to a 1 r/s floor on 429/timeout and recovers to the ceiling once clear — PLUS an injected `ceiling_per_min` HARD 500/min rolling-window governor on top (DEC-GROWW-RATE-PERMIN-001) | `adapters/rate_common.py:136-198`; `groww/constants.py:165`; `dashboard/pull_controller.py:776`; `cli.py:2020` + this file §2 rule 6 |
| Auth | **TOTP flow** — `GrowwAPI.get_access_token(api_key, totp=pyotp.TOTP(secret).now())`; creds via `BRUTEX_GROWW_API_KEY` / `BRUTEX_GROWW_TOTP_SECRET`. The API-key+secret flow is REFUSED (daily manual approval violates §11 AUTOMATED) | `groww/auth.py:28-31,94-114`; `groww/api-reference/01-introduction.md:34-96` |
| Instrument master URL | `https://growwapi-assets.groww.in/instruments/instrument.csv` (single CSV; SSRF-allowlisted host) | `groww/constants.py:24-26`; `groww/api-reference/02-instruments.md:8` |
| Instrument master schema | **21 columns** in fixed order; keys `exchange, exchange_token, trading_symbol, groww_symbol, name, instrument_type, segment, series, isin, underlying_symbol, …, lot_size, tick_size, freeze_quantity` | `groww/constants.py:28-53`; `groww/api-reference/02-instruments.md:112-134` |
| Exception hierarchy | `GrowwError, GrowwBadRequestError, GrowwAuthError, GrowwNotFoundError, GrowwRateLimitError, GrowwTimeoutError, GrowwFeedError, GrowwEmptyResponseError` + `is_retryable()` (maps the SDK's `GrowwBaseException`/`GrowwAPIException` tree by class name; an empty/non-JSON body `json.JSONDecodeError` → **retryable** `GrowwEmptyResponseError`, so a transient throttle is re-pulled instead of a terminal "not on Groww" quarantine — DEC-POSTPULL-DATAQUALITY-BATCH-001 #854) | `groww/errors.py`; `groww/api-reference/05-exceptions.md:9-70` |
| F&O-eligible stock detection | EQUITY rows whose `underlying_symbol` appears in the FNO contract set; **212** single stocks verified in the operator instrument master (lake scope) | `groww/constants.py:103-104`; `groww/instruments.py:174` |
| NIFTY Total Market lake admission | **REFERENCE_ONLY** — ~750 NSE constituents (large+mid+small+micro) fetched best-effort from niftyindices, matched to Groww NSE cash-equity rows by ISIN / ticker; the ~540 non-F&O matches are admitted reference-only (the ~212 F&O names stay tradable). The fetch is **cache-on-success + bounded-retry + cache-fallback** (live → last-good cache → honest F&O-base degrade), so a transient niftyindices 404 falls back to the cached ~752 instead of collapsing the universe to the ~211 F&O base (this cache/retry predates the batch; regression-pinned in DEC-POSTPULL-DATAQUALITY-BATCH-001 #854). **BRUTE-FORCE engine UNCHANGED** — lake/reference only, never in the 52-vocab | `dashboard/instrument_loader.py` `_ntm_admission` (DEC-NTM-GROWW-REPOINT-001); `universe_constituents.py` `resolve_constituents` |
| Track 1 brute-force universe | **`NSE-NIFTY`, `NSE-BANKNIFTY`, `BSE-SENSEX`** (all `IDX`, mapped to the neutral `INDEX` segment); resolved master-authoritatively via `by_exchange_symbol` + `groww_symbol_by_pair` (the bare trading symbol → the real groww_symbol, DEC-GROWW-HYPHEN-SYMBOL-RESOLVE-001). (LAKE option-pullability for F&O indices/stocks keys on a vendor-canonical `underlying_security_id` token-join (numeric `feed_token`) + a sourced NSE-index alias `NIFTYJR`→`NIFTYNXT50` / `NIFTYMIDSELECT`→`MIDCPNIFTY` (env-extensible `BRUTEX_FNO_INDEX_ALIASES`), with a `universe.load.opt_reclassified` audit — DEC-POSTPULL-DATAQUALITY-BATCH-001 #854; the ENGINE still the 3 indexes) | `groww/constants.py:97-100`; `groww/instruments.py:124-136`; `groww/adapter.py:162-174` |
| INDIA VIX symbol | **`NSE-INDIAVIX`** — reference-only (`buy/sell_allowed=0`), never in the 52-vocab | `groww/constants.py:101-102` |
| INDIA VIX historical-candle availability | **UNVERIFIED** — Phase-0 HARD-GATE; if absent on Groww the migration is rescinded before cutover | `adr/0009-...-migration.md:90,111-112` |
| Token lifetime + backtest-vs-live drift | **UNVERIFIED** — Phase-0 probe gates (unattended TOTP token lifetime; prior-vendor↔Groww 1m bar drift) | `adr/0009-...-migration.md:88-92,113-124` |

## 7. Verified Indian Market Microstructure Facts

| Fact | Value |
|------|-------|
| Trading hours | 09:15 - 15:30 IST (NSE + BSE equity) |
| Pre-open auction | 09:00 - 09:15 IST (exclude from backtest bars) |
| Spot index ticks from | 09:15:00 IST (post-auction opening) |
| Track 1 forced exit | 15:20 IST |
| Muhurat session (Diwali) | 1 hour per year; date list in `TRACK1_MICROSTRUCTURE.md` |
| NIFTY 50 rebalance | Semi-annual (March, September) — ~10 events in 2021-2026 |
| Stamp duty (equity intraday) | 0.003% buy side (signal-only mode: NOT applied) |
| Brokerage assumption (Track 1) | **NONE** — spot is not tradable |

---

## 8. Hardware Envelope (for honest SLA claims)

**Track 1 reference:** Apple M4 Pro, 14-core CPU (10P + 4E), 48 GB RAM,
8 TB external SSD. Daily apps run concurrently (10 cores used by sweep,
2 reserved).

**Production sweep SLA:** **2-4 days wall time** for ~33 billion trials
on this envelope, after Stage 1A → 1B → 2 → 3 → 4 pipeline. Public
benchmarks (vectorbt, NautilusTrader, Jesse) constrain the upper bound;
see `docs/TRACK1_PERFORMANCE_BUDGET.md` for the full memory-bandwidth
analysis.

Larger machines (64-128 core EPYC / Threadripper) scale linearly and
bring sweep to 12-24 hours; the spec MUST state SLA per hardware
envelope.

---

## 9. When in Doubt

1. Re-read `docs/TRACK1_BACKTEST_SPEC.md`.
2. If answer not there → ask the user with `AskUserQuestion`.
3. Never guess on scope. Never expand scope silently.
4. Never propose "improvements" outside the requested task.
5. Before major design choices, spawn a 4-agent hostile review (see
   `docs/TRACK1_PHASE_PLAN.md §0` for the pattern).

---

## 10. Session Etiquette

- Start of every session: read this file + `TRACK1_BACKTEST_SPEC.md` +
  open the locked-decisions log in `docs/MIGRATION.md`.
- Use TodoWrite for any multi-step task.
- Split long writes into multiple Write tool calls (avoid stream idle
  timeouts on docs >300 lines).
- Report honestly on failures; do not paper over errors.
- After any major refactor: run `ruff check brutex && mypy --strict
  brutex && pytest -q` before considering the change complete.
