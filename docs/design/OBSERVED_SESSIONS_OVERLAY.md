# Observed-Sessions Overlay — design record

**Decision ID:** DEC-CALENDAR-OBSERVED-SESSIONS-001 (Session D PR-1; MIGRATION.md #1230)
**Modules:** `brutex/calendars/observed_sessions.py` (new) + the wired consumers below.
**Status:** SHIPPED. This document condenses the reviewed design (a 4-ground refuter
RESCOPE was folded in full — see §6).

## 1. Problem

The derived NSE calendar (`brutex/calendars/nse.py`) can drift from vendor reality:
a live Groww pull served **20** distinct Jan-2026 trading sessions while the
calendar derives **21** (the DEC-HARDENING-LEFTOVERS-001 honesty note,
MIGRATION.md #1149). The missing holiday's DATE is **UNVERIFIED** — per Golden
Rule #1 it must never be fabricated into `HOLIDAYS_2026`. But every consumer that
walks the derived calendar (prev-day / weekly anchors, the pre-open report, the
doctor probe day, the completeness detectors' margins) silently mis-reads the
observed holiday as a *data hole* on that day.

## 2. Decision

Detect the drift from the lake's own evidence and record it in a **remove-only,
evidence-gated, labeled overlay sidecar** that opt-in consumers subtract from the
derived calendar. The overlay asserts ONLY *"the market observably did not trade
this day"* — never "holiday X". The named-holiday gap stays closable ONLY by the
official NSE circular or a live vendor probe.

### 2.1 Detection rule (all four conjuncts required)

A derived trading day `d` of a **fully-past** month is flagged iff:

1. **Zero bars** across ALL 3 engine indices (`ENGINE_INDEX_COORDS`:
   NSE/INDEX/NIFTY + NSE/INDEX/BANKNIFTY + BSE/INDEX/SENSEX) for `d`
   (one month-file read per index — `lake_day_glob`).
2. **Clean-SUCCESS ledger evidence**: ≥ 1 `SUCCESS` spot 1-minute window
   (`market == GROWW_COVERAGE_MARKET`, end fetch-exclusive) covering `d`,
   **per index**. (The sketched "≥ 2 clean attempts" is UNSATISFIABLE: a settled
   `SUCCESS` window is skip-listed before any vendor call, so a second
   independent clean fetch of the same window never happens. The real
   multiplicity is ACROSS indices: 3 series × ≥ 1 window = 3 independent clean
   observations.)
3. **FAILED veto**: zero `FAILED` ledger rows touching `d` — deliberately
   over-broad (ANY market/symbol/interval; operator-verbatim). Over-veto is the
   safe direction for a remove-only overlay.
4. **Month corroboration**: all 3 indices carry bars elsewhere in the month
   (an absent/empty index month file is data ABSENCE, not holiday evidence).

### 2.2 Hard bounds

| Bound | Value | Enforcement |
|---|---|---|
| Remove-only | structural | the ONLY consumption primitive is `is_trading_day_observed(d) = is_trading_day(d) AND d ∉ observed`; no ADD channel exists in the data shape, API, or any consumer |
| Flags per month | ≤ 3 | more admitted candidates refuses the WHOLE month (`observed_sessions.month_cap_exceeded`, error, NO write) — never "first 3 kept" |
| Overlay days per year | ≤ 8 | `observed_sessions.year_cap_exceeded`, error, NO write |
| Label | `observed-not-official` on every entry | the loader REJECTS an unlabeled entry (whole sidecar degrades to empty, warned) |
| Month scope | fully-past, in-coverage | current/future month → `ValueError`; out-of-coverage year → `ValueError` (the derived calendar itself is unknown there) |

### 2.3 Sidecar

`<lake_root>/calendar_overlays/observed_holidays.json` (format v1) — outside
`bars/` + `options/`, not `*.parquet`, so no lake scanner sees it. Written via
the COMMON `atomic_io.atomic_write_simple` (guard-free: a disk-full here raises
`ObservedSessionsError`, never the pull-wide `LakeWriteHalt`). Deterministic
bytes (sorted entries + keys) — golden-pinned.

**Diff-based, self-healing write:** the sidecar is rewritten whenever the
recomputed month entry-set differs from the stored one — INCLUDING the
transition to the empty set, so a stale entry for a day that later gains real
bars (re-pull / `reopen-skipped`) is REMOVED on the next detection. Cap
refusals are strictly no-write. An UNCHANGED `(day, evidence)` entry keeps its
original `detected_at`, so a production re-run is byte-idempotent.

### 2.4 Invocation surface

`brutex detect-session-drift --month YYYY-MM [--lake-root …]` — deliberately
**manually-invoked-only**: the operator scans a month after its pulls settle,
reviews the printed evidence (flagged / vetoed / no-evidence / cap verdicts),
and the labeled sidecar is the human-auditable escape hatch (delete an entry or
the file to restore the plain derived calendar).

### 2.5 Wiring (opt-in consumers ONLY)

| Consumer | Mechanism |
|---|---|
| `indicators/prev_day_ohlc.py` + `indicators/weekly_anchors.py` | keyword-only `observed` (default `frozenset()` = byte-identical) |
| `dashboard/preopen_report.py` | `build_preopen_rows` loads the overlay once per report → `_previous_trading_day(…, observed=…)` |
| `doctor_live.py` `_probe_day` | overlay loaded from the resolved lake root; an observed holiday is never probed |
| interior-gap + partial-window detectors | `observed` on the pure classifiers + observe helpers; threaded as `observed_holidays` through BOTH ingest spines (mirroring `detect_completeness_gaps`), armed at the dashboard + CLI live-pull sites and the F&O historical phase; a dry-run threads the empty overlay |

**Explicit NON-wiring (scope-lock, regression-tested):** pull window planning
(`plan_windows`, `seed_enclosed_windows`, `default_fno_minute_window`, range
resolution) and the ENGINE (`strategies/fib_definitions.py` call sites keep the
default empty overlay — the blake3 run identity is unchanged, Golden Rule #4).

This is an **honesty fix, not budget burn**: the detectors' 5-session tolerances
already absorb a 1-day drift — the wiring corrects the MARGIN ARITHMETIC (an
observed holiday is not a data hole), it does not rescue a failure.

## 3. Companion: prev-day "data wins" walk-back

`lookup_prev_day_ohlc` previously returned `None` whenever the calendar prior
session's daily candle was missing — every drifted month silently lost its
PDH/PDL anchor. Now it walks BACK ≤ 14 calendar days **over calendar sessions
only** (`is_trading_day_checked` minus the overlay) to the latest session WITH a
candle, returns that STALE anchor with honest `prev_day` provenance, and emits
ONE `prev_day_ohlc.stale_anchor` WARNING.

* **INTENDED semantic change** — a GENUINE data gap now also yields a stale
  anchor + warn instead of `None` ("data wins", operator-decided,
  regression-pinned).
* The session-restricted walk domain preserves the ledgered **ADR-0012 §4.5 #7
  Muhurat exclusion**: a Muhurat / weekend daily bar in the candle dict can
  never become the anchor (concretely pinned against the 2025-10-21 afternoon
  Muhurat).
* An overlay-explained drift day reconciles **silently** (the walk lands on the
  earlier session's candle and the winner equals the calendar prior session) —
  the warn fires only for days the overlay does not explain.
* No candle within 14 days → `prev_day_ohlc.no_anchor_within_lookback` + `None`
  (the sole remaining `None` path).
* **Engine impact: none.** The brute-force engine's PREV_DAY fib path is
  stream-based (`engine/stage1a.py` computes `prev_ohlc` off the bar stream) and
  never calls this lookup — sweep output is byte-identical; the change lives on
  the definitions surface (`resolve_fib_anchor_levels` / `resolve_fib_levels`).
* `find_prev_trading_day` keeps pure CALENDAR semantics (`data/futures_roll.py`
  unaffected by the data walk-back).

## 4. `HOLIDAYS_2026` honesty fix (same PR)

* `2026-02-15` ("Mahashivratri", per-line UNVERIFIED, provably a SUNDAY —
  an arithmetic no-op in `is_trading_day`) REMOVED with **no substitute date**
  (the real NSE 2026 observance date is UNKNOWN pending the circular).
* The fixed-date entries (Republic Day, Ambedkar Jayanti, Maharashtra Day,
  Independence Day, Gandhi Jayanti) annotated: dates calendar-fixed, NSE
  observance still circular-UNVERIFIED. `2026-08-15` (Saturday no-op) retained —
  its date is fixed, unlike the movable 2026-02-15.
* The Jan-2026 honesty note now names the overlay as the sanctioned
  observed-not-official closure path.

## 5. Honest envelope (Golden Rule #10)

1. **Observed ≠ official.** The Jan-2026 missing-holiday DATE + NAME remain
   UNVERIFIED; only the NSE circular / a live vendor probe closes the named gap.
2. **Indistinguishable worst case:** a simultaneous same-day vendor hole across
   all 3 independently-pulled engine indices with clean SUCCESS windows and zero
   FAILED rows is indistinguishable from a real holiday — bounded by the 3-index
   conjunction + corroboration + veto + both caps + the human-editable labeled
   sidecar + remove-only consumers with 5-session tolerances.
3. **Seeded-SUCCESS residual:** `seed_enclosed_windows` is a SPAN test, not a
   density test — a seeded SUCCESS can supply "clean evidence" over an interior
   hole. Month corroboration blunts but does not eliminate this.
4. **Veto satisfiability:** on a mid-backfill lake with lingering FAILED F&O
   rows the detector may NEVER flag anything until those settle — the veto
   outcome logs at WARNING so the operator sees why.
5. **Sidecar concurrency:** the read-merge-write is crash-atomic but NOT
   isolated against a concurrent detection run. The sole invocation surface is
   the manual CLI command (single operator), so the race is an accepted,
   ledgered envelope item — not mechanised.
6. **No live invalidation:** the overlay is loaded once per report/pull/process.
7. **POSIX envelope** inherited from `atomic_io` (Windows out of scope).
8. **Arming parity note:** `observed_holidays` is armed exactly where the pull
   sites already arm their live-only collaborators; `pull_all_spot` additionally
   threads it (its observe-only detector WARNs run unconditionally) even though
   it does not arm `detect_completeness_gaps` — a pre-existing shape, unchanged.

## 6. Refuter findings — disposition (all folded)

| # | Finding | Disposition |
|---|---|---|
| R1 | detector had no invocation surface (dead code) | FOLDED — `brutex detect-session-drift` CLI subcommand, ledgered manually-invoked-only |
| R2 | write-only-when-flagged could never self-heal a stale entry | FOLDED — diff-based write incl. delete-the-month; `test_redetect_after_bars_appear_removes_entry` |
| R3 | claimed sweep-output change was false (engine is stream-based) | FOLDED — impact reworded; regression re-targeted at the definitions surface; sweep byte-identity stated as the POSITIVE fact |
| R4 | raw-calendar data walk conflicted with ADR-0012 Muhurat exclusion | FOLDED — walk restricted to calendar-sessions-minus-overlay (option a); Muhurat pin test |
| R5 | series crash on overlaid `trading_days` member | FOLDED — series skips overlaid members (mirrors skip-None); zero-production-consumer note added |
| R6 | `detected_at` broke byte-idempotence under the production clock | FOLDED — unchanged (day, evidence) keeps the stored `detected_at` |
| R7 | ANY-row FAILED veto may permanently suppress on a mid-backfill lake | FOLDED — veto log raised to WARNING; satisfiability consequence stated (§5.4); vetoed days surfaced in the CLI report |
| R8 | "frozen config field" wording wrong (spines take kwargs) | FOLDED — plain kwarg mirroring `detect_completeness_gaps` |
| R9 | SUCCESS cover must pin `market == GROWW_COVERAGE_MARKET` | FOLDED — mandatory filter + regression |
| R10 | `calendar_prev_day=None` warn payload undefined | FOLDED — structurally impossible under the session-restricted walk (a winner implies the first session was seen); documented + exhaustion test |
| R11 | sidecar read-merge-write race | ACCEPTED envelope item (§5.5) — manual single-operator surface; not mechanised |
| R12 | verified-correct list + ledger `load_failed` fold | FOLDED — unreadable ledger aborts LOUDLY (`observed_sessions.ledger_unreadable` + `ObservedSessionsError`), never silently "no evidence" |
