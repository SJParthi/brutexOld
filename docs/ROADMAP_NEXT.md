# BruteX — Forward Roadmap (NEXT)

> Honest, evidence-grounded forward plan (2026-06-26). Governed by
> `docs/ENGINEERING_STANDARDS_UNIVERSAL.md` (deep research → review →
> merge → monitor → post-merge validate; O(1)-where-real; no illusion) and the
> `CLAUDE.md` Golden Rules. Items are labelled with the standard evidence labels
> (Verified / Assumed / Risk / Blocker / Limitation / Recommendation / Unknown).
> Nothing here is claimed shipped; each item names its scope and gate.

---

## Executive summary

| # | Item | Area | Type | Status |
|---|------|------|------|--------|
| 1 | Failure-matrix gaps | Backend / observability | Additive | Recommendation — designed, gated |
| 2 | Verbatim raw-payload archive | Backend / data | Additive | Recommendation — extends #899 |
| 3 | At-scale harness — Gate-2 write-depth oracle | Backend / data | Benchmark gate | Recommendation — unblocks 5× format |
| 4 | Dashboard in-place nav-swap | Frontend | UX | Recommendation — review before landing |
| 5 | ~5× new file format (4b) | Data storage | HELD | Blocker — proven unsafe; needs ADR + version bump |

---

## 1. Failure-matrix gaps (backend / observability — additive)

Close the remaining observability gaps in the live pull. All **additive**
(observe-only / new signal), no engine or scope change. Builds on the
self-instrumenting pull-health monitor (#932) and the suspicious-empty flag
(#899/#907 triad).

- **Per-worker heartbeat** — each pull worker emits a periodic liveness tick so a
  silently-wedged worker (vs an idle pool) is distinguishable. *Limitation:* a
  heartbeat detects a stall, it does not auto-recover it.
- **Combined-failure correlation** — when several rules breach together (e.g.
  write-latency spike + achieved-below-allowed + rate-storm) surface the
  *correlation* as one root-cause hint, not three separate alerts. *Risk:*
  correlation heuristics are operator-tunable, not vendor truth — label them so.
- **Live pull-progress signal** — a real-time "X of N contracts, Y r/s, ETA"
  progress signal to the dashboard, so a long pull is observable without log
  tailing. *Recommendation:* additive route + cached snapshot, O(1) per poll.

**Gate:** deep research over the existing `_observe_rate` seam first; hostile
review; 100% line+branch on new code; no change to the engine
(NIFTY/BANKNIFTY/SENSEX) or the rate ceiling.

## 2. Verbatim raw-payload archive (backend / data — additive)

Extend the suspicious-empty diagnostic (#899) and the agreed integrity layer
(see `groww-data-integrity-verbatim`): store the **raw Groww response per
window** (blake3-hashed + optionally archived verbatim) alongside the lake, so
every pull *proves* the 2dp lake derives from exactly what the vendor sent, and
any drift is caught in real time.

- *Verified:* the lake is **not** byte-verbatim today (2dp ROUND_HALF_UP snap at
  write, IST→UTC conversion, volume/OI clamps) — this is intentional per
  DEC-SPOT-PRICE-2DP-001 and stays. The archive is the *integrity proof*, not a
  revert of the snap.
- *Limitation:* archive storage cost scales with pull volume; archive is opt-in
  / bounded, not unconditional.

**Gate:** additive only; never modifies stored bars; the 2dp snap is CONFIRMED
correct and unchanged (operator-resolved 2026-06-26).

## 3. At-scale harness — Gate-2 write-depth oracle (backend / data)

The proven blocker for the 5× format (item 5) and for the fetch-write-overlap
Lever 1 (#921) is the absence of a **write-depth benchmark oracle**: a harness
that measures real append/write throughput at partition depth, so a change is
KEPT only if it nets *higher* throughput (the #906 trap — never keep a change
that only burns retries).

- *Verified:* a single serialized writer would cap appends at ~3/s — a net
  regression — so Lever 1 (`bench_write_rate(W) > 3.2/s`) and the 5× format both
  require this gate before merge.
- *Recommendation:* build the Gate-2 oracle first; it is the dependency for both.

## 4. Dashboard in-place nav-swap (frontend — review before landing)

The dominant remaining nav latency is the **full-page reload** on section
switch; replace it with an in-place DOM swap (the flagged separate, larger PR
noted in DEC-DASHBOARD-NAV-LATENCY-001 #933). Frontend-only.

- *Risk:* a DOM-swap rewrite can regress state/scroll/focus; **review before
  landing** (manual UX verification per the verify-before-claim rule, not just
  CI green).
- *Limitation:* read-only viewer change; no engine/payload change.

## 5. HELD — ~5× new file format (4b)

The "~5× faster new columnar file format" idea is **HELD / proven unsafe** as-is.

- *Blocker:* it rewrites lake storage and risks float-representation drift that
  changes stored bytes → breaks Golden Rule #7 (reproducibility) and the
  operator's verbatim-integrity directive. It stays SHELVED **unless proven to
  produce byte-identical 2dp values**.
- *Required to unblock:* (a) an **ADR**; (b) a **data-version bump**; (c) the
  Gate-2 write-depth oracle (item 3) proving a real net throughput win AND
  byte-exact 2dp output. Until all three, it does not ship.

---

## Honest constraints (Golden Rule #10)

- **Pull throughput is vendor-bound.** The live pull is limited by the vendor's
  ~5 r/s tolerance (the controller settles wherever Groww allows; the ceiling
  was raised 5→8 only to *test* headroom, and the log shows the vendor 429ing at
  5 — SUSTAINED 8 is **Unknown**) **and** by raw data volume. No roadmap item
  claims "millions in minutes"; throughput is named per the real envelope.
- **The system is self-diagnosing, not "fast" by fiat.** The observability work
  (items 1–2) makes failures *visible and explainable*; it does not make the
  pull faster. Throughput is items 3 + the proven Lever 2 (#921), bounded by the
  vendor.
- **Data is never modified beyond the proven 2dp snap.** The only intentional
  transformation of vendor data is the 2dp ROUND_HALF_UP tick-grid snap
  (DEC-SPOT-PRICE-2DP-001), CONFIRMED correct and unchanged. No roadmap item
  introduces a new modification; the integrity work (item 2) *proves* this, it
  does not relax it.

---

## EAP track (parallel, co-equal deliverable)

The Claude Code Early-Access feedback stream is a standing co-equal deliverable.

- **Round-2 evidence batch** for the **Jul 1, 9pm-IST call with Omid**: assemble
  the rock-solid, evidence-backed findings (screenshots embedded with each
  finding) into the dossier.
- **Verify-before-claim** is the hard rule (per `eap-verify-before-claim`): every
  finding confirmed against the actual UI / tool / logs *before* it is reported;
  fewer, rock-solid findings over many shaky ones; honest retraction the moment a
  finding is shown wrong.
