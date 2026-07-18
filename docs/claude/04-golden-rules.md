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
   every **OBSERVABLE** trade (DEC-VIX-STAMP-WIRE-001, MIGRATION.md
   #1645 — persisted finalist rows additionally carry the derived
   nullable `vix_mean`/`vix_min`/`vix_max` trio; the ONLY unstamped
   trade streams are the FIVE named DISCARDED/unobservable streams —
   the Stage-4 cascade candidate re-simulation (stamps would be dropped
   at the CandidateInput scalar conversion; report bytes pinned
   identical — the round-6 review DELETED the cascade chain's dead
   stamper seam plus the two manual-replay `vix_stamper_for_manifest`
   builds that paid a full VIX lake read per replay/click for zero
   output), its Monte-Carlo permutation filter, `run_persisted_sweep`'s
   K-of-N inner stream, the CLI options-sim replay (stamps would be
   dropped at the SpotSignal conversion) and `brutex sweep`'s
   text-report replay (report bytes pinned identical) — each an
   explicit literal `reference_stamper=None` enumerated by an AST pin
   whose exact-count laws also REFUSE a silently-omitted kwarg
   and positively pin the entry-point binds (round-2 panel hardening);
   the trio survives redo honestly — `None → value` backfills, a
   value-vs-value clash raises the typed tuple-skip
   `VixTrioConflictError` (the VIX lake moved, NOT non-determinism) and
   `sweep-verify --recount` stamps like the launcher + attributes a
   VIX-only divergence as the non-corrupt `recount_vix_divergence`;
   a missing VIX bar stamps `None`, never fabricated), but is
   NEVER part of brute-force pattern combinations or the 52-condition
   vocabulary (nor `_RANKABLE_FIELDS`, nor the BLAKE3 run identity). 21 timeframes — **`1m` is the ONLY
   granularity fetched** and the 19 derived intraday TFs + `1d` are all
   DERIVED from it (DEC-1MINUTE-ONLY-DERIVE-1DAY-001, MIGRATION.md #853; the
   `1d` daily anchor is resampled to midnight-IST from the stored 1-minute and
   written to the lake's 1-day partition — since DEC-PIPELINE-AUTOPREBUILD-001
   (MIGRATION.md #1145) by the pull pipeline's automatic FINAL prebuild stage,
   never mid-pull, never separately vendor-fetched; since
   DEC-PREBUILD-CLEANSET-001 (MIGRATION.md #1221) that stage runs on every
   completed pull over the CLEAN symbol set — quarantined symbols excluded +
   headlined, no longer gated on a fully-clean fetch).
   Rolling `2020-01-01 → today` range (the data pull floor
   — `GROWW_HISTORY_FLOOR`; DEC-PULL-FLOOR-2020-001, 2026-06-13, MIGRATION.md
   entry #722 — supersedes the prior conservative `2021-01-01` floor of
   DEC-DHAN-PULL-SCOPE-001 now that the Phase-0 granularity probe confirmed
   Groww serves 1-minute data from `2020-01-01T09:15:00`). No futures, no
   options, no F&O contracts. Do not silently expand scope.

   **Engine-scope amendment (DEC-NIFTY-DYNAMIC-STRATEGY-001 / ADR-0012,
   2026-06-29, MIGRATION.md entry #1006; companion clause owned by PR-1
   DEC-VOCAB-U128-CODEGEN-001 #1010) — the ENGINE-FUTURES AUTHORIZATION in
   this block is RETIRED by DEC-ENGINE-SPOT-ONLY-001 (2026-07-13,
   MIGRATION.md #1790 — see the retirement note at the end of this block;
   the history below is kept verbatim per the supersession convention):**
   the operator had AUTHORIZED the
   brute-force ENGINE to ALSO run on **NIFTY futures** for the dedicated
   dynamic-strategy track (a session-anchored intraday VWAP is feasible on
   futures because they carry traded volume, unlike a spot index per
   DEC-VENDOR-VOLUME-NORMALIZE-001). This is operator-authorized, NOT a silent
   expansion. The 3 spot indices (NIFTY 50 / BANKNIFTY / SENSEX) remain the
   index engine surface; **all OTHER F&O — every non-NIFTY-futures contract,
   the ~212 F&O stocks, the NTM constituents, India VIX — stays REFERENCE_ONLY**
   (lake-only, never in the 52→60-condition vocabulary). The vocabulary itself
   grew 52→60 conditions on a u128 mask in PR-1. **PR-2 (DEC-NIFTY-INDICATORS-001,
   #1011) supplied the indicator math for the VWAP-position (bits 52/53) + pivot
   R5/S5 (bits 54/55) conditions + the two new Fibonacci anchors (rolling-weekly /
   weekly-static, NO new bits). PR-2b (DEC-NIFTY-BOS-CHOCH-001, #1012) then
   supplied the BOS/CHoCH market-structure math (bits 56-59) — a look-ahead-clean
   fractal swing-detection state machine (`indicators/market_structure.py` +
   `rust/trading-core/src/indicators/market_structure.rs`, a swing confirmed only
   k bars later, proven P-06-clean by a dedicated `LookAheadBarrier` test) — so
   ALL 60 conditions are now `implemented=true` (`IMPLEMENTED_MASK = (1<<60)-1`)
   and the data-independent indicator vocabulary is COMPLETE.** **PR-5
   (DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001 / DEC-FUTURES-ADDRESSING-PIN-001 /
   DEC-STAGE1A-ROLL-RESET-001, MIGRATION.md #1700–#1702) LAUNCHED the futures
   sweep: the track is addressed by the ONE pinned series alias
   `NSE-NIFTY-FUT` (DEC-FUTURES-ADDRESSING-PIN-001 — supersedes the
   pre-launch dual-acceptance envelope; the retired `NIFTY-FUT-YYYY-MM-DD` /
   `NSE-NIFTY-<DDMmmYY>-FUT` shapes get a did-you-mean refusal), roll-resolved
   at launch by `brutex/data/futures_series.py` into the per-contract
   `bars/NSE/FNO/NSE-NIFTY-<DDMmmYY>-FUT` lake partitions (vendor-parsed
   expiries govern ownership; expiry day belongs to the EXPIRING contract) and
   stitched by `brutex/feed/series_load.py` with a hard per-contract Stage-1A
   reset at every roll (D11 — no level ever crosses the basis gap). The
   engine sweep surface is EXACTLY {the 3 spot index triples} ∪
   {NSE/FNO/NSE-NIFTY-FUT}.**

   **RETIREMENT (DEC-ENGINE-SPOT-ONLY-001, 2026-07-13, MIGRATION.md
   #1790):** the operator has DIRECTED the engine back to SPOT-ONLY ("wipe
   off the future, only spots alone") — this CONSCIOUSLY SUPERSEDES the
   engine-futures authorization above and the PR-5 launch surface: the
   engine sweep surface is EXACTLY the 3 spot index triples
   (NIFTY/BANKNIFTY/SENSEX), `is_engine_sweep_symbol` refuses
   `NSE-NIFTY-FUT`, and every production sweep entry refuses it loudly
   citing the DEC + the remediation ("the engine is spot-only;
   NSE-NIFTY-FUT remains lake/REFERENCE_ONLY"). NIFTY futures REJOIN the
   REFERENCE_ONLY surface (lake ingest + the F&O candle lake untouched);
   the 60-condition vocabulary itself is UNCHANGED (data-independent —
   the volume-bearing VWAP bits 52/53 now permanently abstain on every
   engine instrument, since no spot index carries volume); the futures
   resolver / alias-aware loader / Stage-1A roll machinery / symbol_id 4
   are RETAINED as tested lake-tooling internals below the scope gate,
   reachable only via the loud, audit-stamped `--allow-out-of-scope` CLI
   research escape or recorded-work forensics.**

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
   computation without that hash recorded. `data_snapshot_hash` is the
   FULL-CONTENT streamed `bxsnap2` digest of every loaded bar's 7 fields
   (`stage3_wire.data_snapshot_hash`, DEC-SNAPSHOT-HASH-FULL-001 — any
   single-bar difference re-keys the identity; supersedes the sampled
   count+endpoints fingerprint). (Was `sha256(...)` pre-DEC-CALIBRATE-001;
   corrected 2026-05-24 to match the spec's run-identity formula — now at
   `TRACK1_BACKTEST_SPEC.md:785` after spec growth moved it from :770
   (earlier :760/:743/:654; DEC-CLAUDEMD-SPEC-LINE-RECONCILE-001 precedent;
   :770→:785 with the DEC-FILL-WORST-CASE-ONLY-001 fill-rules rewrite) — which has used blake3 since
   the v2 spec lock 2026-05-16. See MIGRATION.md entry #204.)
5. **O(1) discipline.** Bar lookup, indicator value lookup, combo dedup
   (Bloom + LMDB), strategy mask evaluation (u64 AND), and result
   append must all be O(1). The decree TARGET is
   `p99(N=1M) / p99(N=1K) ≤ 1.1` per hot path; ENFORCEMENT is the
   two-tier gate defined once in `tests/bench/_o1_tiers.py`
   (DEC-O1-CLAIMS-HONESTY-001 wording reconcile — no gate value
   changed): every O(1) bench asserts a robust/mean-based **3–10×**
   ratio ceiling on the shared 2-core GitHub runners (DEC-P0-020 —
   the raw 1.1× is too noisy to gate there without flaking), and the
   decreed strict **1.1×** tier is enforced where
   `BRUTEX_M4PRO_STRICT=1` on the dedicated self-hosted M4-Pro runner
   (DEC-O1-STRICT-TIER-001; the runner is REGISTERED and LIVE —
   2026-07-07, `bench-m4pro.yml` header + actions run 28888100079 — so
   the strict tier EXECUTES on that workflow's path-triggered pushes,
   while the shared-CI tier remains the gate on every push), with a
   measured **3×** cache-tier allowance for ratio tests whose big/small
   workloads cross a CPU cache tier (DEC-O1-CACHE-TIER-001 —
   memory-tier physics, not an algorithmic regression;
   `cache_tier_ceiling` never returns looser than the shared default)
   and a measured **3×** metadata-sweep allowance for ratio tests whose
   big/small workloads differ by a feature's own documented O(D)
   metadata sweep (DEC-LAKE-STATS-MANIFEST-001 —
   `metadata_sweep_ceiling` / `METADATA_SWEEP_STRICT_CEILING` in
   `tests/bench/_o1_tiers.py`, calibrated off run 28888100079's
   measured 1.53×; likewise never looser than the shared default). Any
   regression past its tier's ceiling fails CI.
6. **Rate-limit safety.** Groww publishes type-level limits (the
   **"Live Data"** group — Market Quote / LTP / OHLC — is **10 r/s,
   300/min**; `docs/groww/api-reference/01-introduction.md:130-134`).
   The **backtesting `get_historical_candles` per-second cap + daily
   quota are UNDOCUMENTED** and are an OPEN Phase-0 probe gate
   (`brutex/adapters/groww/constants.py:141-143`
   `GROWW_RATE_LIMIT_RPS_UNVERIFIED=10.0`,
   `GROWW_DAILY_QUOTA_UNVERIFIED=None`; ADR-0009 §3.1 risk #4 / §4) — the
   `_UNVERIFIED` suffix carries the honesty requirement in the
   identifier. The adapter paces with the vendor-neutral adaptive
   token-bucket controller (`brutex/adapters/rate_common.py:372`
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
   (`GROWW_RATE_LIMIT_RPS_CEILING=8.0`, operator-approved 2026-06-24, RAISED
   5 → 8, DEC-GROWW-RATE-CEILING-8-002 — supersedes DEC-GROWW-RATE-CEILING-5-001)
   — raised back to 8 (with EXPLICIT, repeated operator approval) once
   DEC-FETCH-WRITE-OVERLAP-001 Lever 2 (pool 2→4) gave the pull enough connections to
   GENERATE demand toward the ceiling (at 2 connections the achieved rate capped at
   ~3.2 r/s so the 5–8 range was pure waste; at 4 connections demand reaches ~6.4 so 8
   is a FAIR test of vendor headroom). 8×60 = 480/min stays UNDER the 500/min governor,
   so the HARD per-minute cap can never be overshot (DEC-GROWW-GOVERNOR-ATOMIC-001). A
   CEILING raise only ever ALLOWS a higher rate — it does NOT force it: the adaptive
   AIMD ladder still steps DOWN on a 429 burst (soft floor 3, deep floor 1) and recovers
   UP only as far as the vendor tolerates, so the worst case at 8 is identical to 5 (the
   controller self-corrects). HONEST (Golden Rule #10): a live-log audit showed the vendor
   429ing AT the 5.0 ceiling, so SUSTAINED 8 is UNVERIFIED — the controller settles wherever
   the vendor allows; revert to 5 is this one constant if 8 only burns 429-retries without a
   throughput gain (the #906 trap). The per-second backtesting cap itself stays officially
   UNVERIFIED (`GROWW_RATE_LIMIT_RPS_UNVERIFIED`). (The vendor-neutral `rate_common.CEILING_RPS=5`
   DEFAULT is unchanged.)
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
    🟡 is row 3 pending DEC-MUTATION-RUNTIME-003's verification run
    (the first green SCHEDULED `mutation.yml` run; supersedes -002's
    never-verified run — MIGRATION.md #1597).
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

