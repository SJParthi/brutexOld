# Options strategies — 06 · Edge-case register (definitive)

**Status:** RESEARCH / DESIGN-READINESS ONLY — zero engine code; the sweep engine remains
SPOT-ONLY (DEC-ENGINE-SPOT-ONLY-001) and options remain REFERENCE_ONLY; any activation of an
options-strategy engine is a future operator-gated DEC/ADR.
**Date:** 2026-07-19
**Provenance:** multi-agent deep-research session, docs-only branch
`claude/options-strategies-design`, deliberately NO PR (fleet-wide PR-creation freeze); every repo
citation is `file:line` at the branch base SHA (see `docs/design/options-strategies/README.md`).
This register merges the two edge-case research sections (registrars 1 and 2, reads performed
2026-07-20) plus the fill-honesty classes of the fill-realism research section. Citations marked
"(per sibling section …)" were line-verified by that sibling research agent in the same session;
all other `path:line` citations were verified by direct reads by the two registrars.

---

## 0. How to read this register

**Entry schema.** Each case E-nn carries: *Scenario* · *Families hit & severity/asymmetry* ·
*Detection (repo evidence + data signatures)* · *Handling law (correct-or-absent, DEC-gated)* ·
*Cannot-model / honest limits* · *Test types* · *Reuse anchors* · *Origin* (the source entry id in
the research sections: `EC-x` = registrar 1, `EC2-y` = registrar 2). Detection signatures are
written to be directly encodable as fixtures — this register is the test-plan seed.

**Merge map.** Registrar 1 contributed EC-1…EC-8; registrar 2 contributed EC2-1…EC2-9. One true
duplicate pair was merged (EC-1 + EC2-7 → E-06: the expiry-day/0DTE case, entry side + endgame
side). All other entries are distinct scenarios; overlaps are cross-referenced, and the ONE
genuine design fork found between the two registers (retro-close vs censor for a
leg-dark-to-horizon structure — E-02 vs E-03) is surfaced explicitly in §5 Law B rather than
silently resolved.

| Final id | Origin | Final id | Origin |
|---|---|---|---|
| E-01 | EC-2 | E-09 | EC-7 |
| E-02 | EC2-4 | E-10 | EC2-1 |
| E-03 | EC-3 | E-11 | EC2-8 |
| E-04 | EC-8 | E-12 | EC2-5 |
| E-05 | EC2-6 | E-13 | EC-4 |
| E-06 | EC-1 + EC2-7 (merged) | E-14 | EC-5 |
| E-07 | EC2-2 | E-15 | EC-6 |
| E-08 | EC2-9 | E-16 | EC2-3 |

**Naming conventions (collision-free).**
- **P1–P14** = the verified reuse palette (§2) — repo machinery the handling laws draw from.
- **FH-1…FH-6** = the per-leg fill-honesty predicates and **Class A/B/C** = the per-structure
  fill-honesty classes, from the fill-realism research section (§3.1/§3.2 there, where they are
  named P1–P6 — renamed here to avoid collision with the palette).
- **PR-0…PR-5** = the multi-trigger precedence rules of E-11 (named P0–P5 in the source entry).
- Per-entry law numbering (L1…, D1…) is scoped to its entry.

**Sibling-section attributions.** Rows attributed "(per sibling section repo-exec / repo-quality /
repo-lake / micro-expiry / micro-settle / micro-margin / gap-fill)" cite research sections of this
same session whose content is absorbed into the sibling docs of this set — the repo-inventory
material into `03-brutex-inventory-reuse-map.md`, the microstructure material into
`02-indian-fno-microstructure.md` (see also `08-seller-blowups-and-tail-events.md`,
`09-capital-margin-and-sizing.md`, `10-seller-edge-and-execution-evidence.md`), and the gap-fill
(fill-realism) material into the fill-realism sibling doc of this set. The cited `path:line`
values are the evidence and stand on their own.

**A residual-ledger closure carried by this register (stated up front).** The repo-exec research
section's UNVERIFIED #12 and gap-fill's U8 (the same-bar-tie SL-first clause previously verified
only via ledger/docstring) are **CLOSED by registrar 2's direct reads**: the implementing Python
resolver (`brutex/engine/trade_sim.py:757-781` — "A bar spanning BOTH levels always fills the
STOP first — the pessimistic tie, hardcoded") and the Rust kernel twin
(`rust/fno-math/src/trade_sim.rs:329` resolver + `:356` "pessimistic tie, hardcoded; the retired
`SameBarTie::TpFirst`" + tie tests `:1232`, `:1484`) were both read at those lines. E-11 builds on
this now-line-verified base.

---

## 1. Index

| id | Scenario | Families hit | Detection (primary) | Handling class (§5) | Test types |
|---|---|---|---|---|---|
| E-01 | Missing-leg data AT ENTRY (contract dir absent · no bar at entry minute · volume-0 bar) | every multi-leg family; asymmetrically condor/fly WINGS; calendar/diagonal far legs | `lake_source` index/minute honest-miss → `None`; `STALE_BAR` volume gate; NEW registry×ledger×partition census | B (atomic structure reject) + A (per-leg gates) + E (preflight refusal) | unit · property · golden · integration · chaos |
| E-02 | Volume-0 / illiquid bar at a DECISION instant; the deferred-exit law | every family with per-bar management; writers' exits (censoring bias) | `STALE_BAR`/`NO_DATA` gates at the decided bar | A (uniform reject + deferral D1–D5) + B (censor arm) | unit · property · regression |
| E-03 | A leg of an OPEN structure goes dark (session scale) | per-bar-managed structures: MTM, per-leg stops, delta rebalancing, rolls | cross-leg relative-presence (NEW); interior-gap shape; ledger window states | B (abstain triggers; retro-close/censor fork) | unit · property · golden · integration · chaos |
| E-04 | Underlying spot hole while options print | ATM/offset selection; delta management; settle proxies; regime overlays | `STATUS_NO_SPOT` exact-instant join + drift-recovery ladder | C (NO_SPOT skip / management abstain) | unit · property · golden · integration · chaos |
| E-05 | IV/Greek-unsolvable regions (deep-ITM, T-floor, no-spot, no-rate, no overlay) | delta-target entries (rules 8-10); any Greek-based defense | Greeks status vocabulary (5 statuses + column-absent) | C (bit=0 entry-only; exits price/time-based) | unit · property · negative-pin |
| E-06 | Expiry-day 0DTE: entry, last-30-min, endgame + pin risk | 0DTE premium selling (straddles/strangles/condors/flies) — the flagship family | `dte==0` predicate; session-derived last-30-min window; `AT_EXPIRY`/`BELOW_INTRINSIC` stamps | E (hold-to-expiry refusal until DEC) + worst-of endgame + C | unit · golden · property · integration · chaos |
| E-07 | A leg reaches expiry while OPEN (exercise / assignment / lapse) | hold-to-expiry families: covered writes, condors-to-settle, calendar back legs | Step-0 `UnsupportedTradeOutcomeError` stub; expiry-instant convention; NO settle price in repo | E (refusal now; expiry state machine + adverse settle band = its own DEC) | golden · property · regression |
| E-08 | DTE / expiry-weekday regime drift vs naive weekday math | DTE-bucketed families; weekly/monthly tiers; BANKNIFTY-weekly era families | vendor expiry truth (symbol grammar + registry) vs the DOW regime table | D (vendor-truth derivation; era-empty honesty) | property · golden · negative-pin |
| E-09 | Muhurat sessions (evening/afternoon; expiry-week collision; calendar-absence drop) | positional carry; weekly/0DTE collision; session-duration math | `MUHURAT_SESSIONS`/`get_session` precedence; session filter keep/drop | D (session-object truth) + skip-and-count | unit · golden · property · integration · chaos |
| E-10 | Gap-open through a short strike (overnight mode; intra-session micro-gap) | option WRITING asymmetry: naked (unbounded) vs defined-risk (capped) | session-open vs prior close vs armed levels; dense index series | worst-case clamp law (inherited) + censoring disclosure + B cap invariant | property · golden · regression |
| E-11 | One bar touches MULTIPLE triggers across the structure | all structures with levels (SL+TP, cross-leg, structure-vs-leg) | triggered-event set per bar | pessimistic precedence ladder PR-0…PR-5 + B (atomic closure) | fixture-matrix · property · mutant |
| E-12 | Circuit-locked underlying (MWCB halt; F&O-stock band cooling-off) | short buy-backs during halts; any delta-hedged family | dated halt table; simultaneous 3-index hole heuristic; zero-range runs | E (event table + disclosed synthetic exit) | unit · golden · negative-pin |
| E-13 | Strike-grid regime change mid-window; strikes appearing intraday | ATM±N offset + symmetric-width families (re-denomination); look-ahead hazard | step derivation + REGISTRY/OBSERVED confidence; first-stored-bar bounds | D (ladder-as-of vendor data) | unit · property · golden · integration · chaos |
| E-14 | Corporate action on a stock option (split/bonus/special dividend) | ALL stock-option families (index families immune) | cross-snapshot lot/ladder discontinuity (NEW) + spot jump + OI teleport | E (refuse the window until a sourced CA calendar + adjustment model) | unit · golden · property · integration · chaos |
| E-15 | F&O ban period (entry during ban; ban begins mid-trade) | stock-option families; acutely adjustment/roll-heavy ones | external ban calendar — NOT derivable from the lake (honest) | E (strict refuse or named ABSTAIN; entry/adjust/reduce permission matrix) | unit · property · golden · integration · chaos |
| E-16 | Margin call mid-trade (forced liquidation) | naked/undefined-risk writers (P&L path); defined-risk (capital/ROI only) | none possible today — the margin domain is refusal-only by design | E (refusal-only; capital-basis vocabulary; standing disclosure) | unit · property · drift-pin |

---

## 2. The reuse palette (verified anchors the entries draw from)

| # | Machinery | Anchor (verified in-session by registrar 1) |
|---|---|---|
| P1 | Typed counted-reject, PINNED order, per-trade never-crash (`LOT_SIZE_UNKNOWN` template) | `brutex/options/executor.py:155-184` (RejectReason), `:479-481` (pinned order), `:497-507` (lot), `:518-530` (cost regime), `:532-541` (NO_DATA), `:543-557` (STALE_BAR), `:559-564` (TICK_FLOOR) |
| P2 | Futures twin: distinct `NO_CONTRACT` + pure-date-gates-first law | `brutex/futures/executor.py:638-641` |
| P3 | Disk-verified preflight refusal naming exact missing months, NEVER auto-pull | `brutex/futures/preflight.py:1-33` |
| P4 | `session_gap` retro-close of an open position at the PRIOR session's last stored bar's adverse extreme; APPEND-ONLY exit-reason vocabulary | `brutex/engine/trade_sim.py:58-67` (law), `:151-161` (enum), `:164-171` (append-only wire vocabulary) |
| P5 | Quarantine-copy-before-rewrite, row-removing-never-value-editing heal + mass-heal refusal rail | `brutex/data/lake_heal.py:1-16` (law), `:24-34` (rails) |
| P6 | Capped/aggregated WARN idiom (ONE WARN per window, never per bar) | `brutex/adapters/groww/option_ingest.py:928-940` (session-filter WARN), `:946-958` (future-bar WARN) |
| P7 | Honest-miss reader: exact-minute or `None`, never nearest-bar, never fabricated | `brutex/options/lake_source.py:11-13,31-33` |
| P8 | Never-fabricated statused NULLs + screen-precedence ladder (NO_RATE → AT_EXPIRY → NO_SPOT → BAD_INPUT → BELOW_INTRINSIC) | `brutex/data/greeks_enrich.py:1035-1062` |
| P9 | Refusal-row regime table (rate-less rows raise typed errors; no runtime escape hatch) | per sibling section repo-exec: `brutex/costs/constants.py:219-267,313-381`; all-refusal margin twin `brutex/engine/margin_history.py:10-23` |
| P10 | Ledger bounded settles + streak-exempt transient demotions | `brutex/data/window_ledger.py:113` (EMPTY_TERMINAL_THRESHOLD=3), `:122-131` (old-window single-confirm supersession), `:160-162` (DEMOTED_SETTLE_REASONS), `:164-177` (TODAY_INCOMPLETE streak-exempt) |
| P11 | Calendar-aware session filter (Muhurat-keep; out-of-coverage abstain) | `brutex/data/session.py:251-278`; F&O wiring `brutex/adapters/groww/option_ingest.py:918-940` |
| P12 | Interior-gap classifier (contiguous >5-session hole, bracketed, false-positive-safe) | `brutex/data/interior_gap_detect.py:1-53` |
| P13 | Vendor-data-not-formula strike/step machinery with confidence labels | `brutex/data/moneyness.py:106-141` (derive_strike_step + sparse-ladder honesty caveat `:120-129`), `:144-159` (StepConfidence REGISTRY/OBSERVED) |
| P14 | Structural-ABSTAIN precedent (named reasons, budget/streak-exempt, never a fabricated green) | CLAUDE.md DEC-OPERATOR-ACCEPTANCE-VERIFY-001 blurb (`brutex/acceptance/` — per sibling section repo-quality) |

Additional line-verified anchors contributed by registrar 2 (used across entries below):
`brutex/execution/fill_mode.py:10-13` (worst-case with-the-level clamp law; family header `:3-4`);
`brutex/engine/trade_sim.py:37-42` (gap-through clamp law), `:43-45` (same-bar SL-first), `:46-54`
+ `:703-714` (forced-exit override semantics), `:733-747` (fill-site clamp), `:757-781` (SL-first
resolver); `rust/fno-math/src/trade_sim.rs:48` (session guard), `:329`/`:356` (tie resolver) +
`:1232`/`:1484` (tie tests); `brutex/calendars/nse.py:124` (`SESSION_CLOSE_IST`), `:125`
(`FORCED_EXIT_IST` 15:20), `:122-124` (expiry-instant non-unification note), `:902`
(`session_last_bar_complete`); `brutex/data/greeks_enrich.py:239-240` (expiry instant 15:30),
`:477` (`AT_EXPIRY` stamp), `:246-258` + `:665` (degenerate time-value band);
`brutex/data/spot_daily_resample.py:32,39` (daily close = last 1-minute close);
`brutex/data/tf_resampler.py:235` (`close.last()`); `brutex/data/bad_tick.py:20-36` + `:76`
(4× screen + COVID magnitudes `:24-26`); `brutex/dashboard/movers_futures.py:902-920` (bad-tick
arm forced OFF for option premia), `:291-327` (`select_expiry_tier` serial-rank law), `:303-305`
(dropped-never-wrong-tier); `brutex/options/expiry_calendar.py:26-29,47-59,95-120` (DOW regime
table + recorded limits); `brutex/data/registry_store.py:644-659` (`list_expiries`);
`brutex/dashboard/movers_options.py:899-918,956,988-997` (calendar-tier laws);
`brutex/data/futures_series.py:8-23` (vendor-truth, calendar observe-only);
`brutex/data/fno_symbol.py:14-21` (right-anchored `DDMmmYY` grammar);
`brutex/engine/stage1a.py:1050-1056` + `:1319-1349` (bit-abstain precedent);
`brutex/data/moneyness.py:19,45,61,96` (`Moneyness.UNKNOWN`);
`brutex/options/lake_source.py:151-157` (pair-load honest `None` → NO_DATA);
`brutex/engine/margin_history.py:10-24` (all-refusal margin law);
`brutex/data/today_completeness.py:71-74` (options sparsity routine `:71-72`; indices always print
`:73-74`).

---

## 3. Full entries

### E-01 — Missing-leg data AT ENTRY (contract dir absent / no bar in the entry minute / volume-0 bar)

**Origin:** EC-2 (registrar 1).

**Scenario.** A multi-leg structure resolves N legs at entry minute M. One leg fails: (a) the
contract directory does not exist under `bars/<exch>/FNO/` (never listed, never pulled, outside
the default `expiry−30d` pull window, or band-drift); (b) the dir exists but no bar exists at
exactly M; (c) a bar exists at M but with `volume == 0` (stale carried price).

**Families hit & severity.** Every multi-leg family — and asymmetrically the WINGS: deep-OTM wing
strikes of condors/flies are exactly the contracts the pull bounds hardest (reduced empty-retry
budget outside the ±12-strike band — per sibling section repo-lake,
`brutex/adapters/groww/strike_chain_bound.py:1-46`) and that liquidity kills first ("an option
routinely stops printing bars several sessions before expiry … liquidity death in the final week",
`brutex/adapters/groww/option_ingest.py:1630-1634`). Calendar/diagonal families are structurally
exposed: the far leg's stored history starts ~30 days before ITS OWN expiry by default (per
sibling section repo-lake G1, `brutex/adapters/groww/contract_bounds.py:104`), so at trade time
the far leg often has NO bars at all — a POLICY hole, not a vendor hole. Today's executor is
single-leg (one signal → one CE/PE leg — per sibling section repo-exec,
`brutex/options/executor.py:417-422,489`), so ALL structure-level semantics are NEW.

**Detection (data signature; existing detectors).**
- (a) Contract-absent: `LakeOptionBarSource` scans `bars/<exch>/FNO/` once per
  (exchange, underlying), decodes dirs via `parse_fno_symbol`, and a leg with no
  `(expiry, strike, option_type)` index entry "honestly misses (`None`)"
  (`brutex/options/lake_source.py:15-23`). Signature: index miss.
- (b) Minute-absent: exact-UTC-minute dict lookup misses → `None` — "a minute the option did not
  trade is an honest `None`, never the nearest-prior bar" (`lake_source.py:31-33`).
- (c) Zero-volume bar: `pair.entry_bar.volume == 0` → `STALE_BAR`
  (`brutex/options/executor.py:543-557`).
- Cause disambiguation (never-pulled vs listed-but-dead vs dead-minute) does NOT exist today (per
  sibling section repo-lake G6): it requires joining (i) the registry expected set
  (`RegistryStore.list_contracts` — per sibling section repo-lake,
  `brutex/data/registry_store.py:114-127`), (ii) the ledger's per-contract `groww-option` window
  states (PLANNED/SUCCESS/EMPTY/FAILED/SKIPPED — P10), and (iii) partition presence. The nearest
  existing surface is display-only: the /db cascade's per-expiry "listed N with data M" rows
  (`brutex/dashboard/fno_cascade.py:229-243`).
- Today's options executor folds (a) and (b) into ONE `NO_DATA` reason
  (`executor.py:158-161,532-541`); the futures twin distinguishes `NO_CONTRACT` (unresolvable)
  from `NO_DATA` (`brutex/futures/executor.py:638-641`) — the finer split is the better seed for
  structures.

**Handling law (correct-or-absent).**
1. Per leg: counted typed reject (P1) — split `NO_CONTRACT` (not resolvable/listed) from `NO_DATA`
   (resolvable, no bar at M) from `STALE_BAR` (bar present, volume 0), adopting the futures enum
   granularity (P2). Each reject names the leg role (wing/body/near/far).
2. Per STRUCTURE (NEW law, nothing represents it today — per sibling section repo-exec): **ATOMIC
   reject-if-any-leg-rejects.** A condor minus one wing is a DIFFERENT risk object; entering the
   partial structure fabricates a strategy the operator never asked for. The structure-level
   counter records which leg + which sub-reason killed it. A partial-structure entry mode, if ever
   wanted, is its own DEC with explicit accounting — never a silent degrade. (This is fill-honesty
   **Class C** atomicity — §5 Law B.)
3. Never substitute a neighbouring strike, never nearest-minute, never a synthetic mid from
   siblings (P7 law).
4. Pre-sweep: a chain-completeness PREFLIGHT over (underlying, expiry, strike-set, date-range) —
   registry-expected × ledger-settled × partition-present — refusing/scoping BEFORE compute and
   naming exactly what is missing, on the futures preflight template (P3: disk beats ledger;
   refuse whole, never auto-pull). This is repo-lake gap G3/h2 (no chain census exists); the
   building blocks (registry, ledger, stats manifest) exist unjoined.
5. Calendars/diagonals: the preflight must ALSO check the far leg's stored-history window against
   the entry date — with the default pull policy the honest verdict for most early far-leg entries
   is "refuse: data not pulled (policy), explicit-dates re-pull is the remediation" (repo-lake
   G1), a REMEDIABLE refusal, distinct from vendor absence.

**Test types.**
- unit: index-miss → None; minute-miss → None; per-leg reason split; the atomic fold over each leg
  position (parametrized leg index).
- property: per-leg AND per-structure count conservation (`n_signals == n_accepted + Σ rejects`,
  the `OptionsRuleResult.__post_init__` consistency law generalized — per sibling section
  repo-exec, `executor.py:602-700`); atomicity invariant — no materialized structure ever has
  < N legs.
- golden: fixture lake with one wing dir deleted → structure reject counted, bytes pinned; same
  fixture with the wing's entry-minute row deleted → NO_DATA variant.
- integration: real ParquetBarSink lake; delete one contract's month parquet; run the structure
  sim; verify counts + ONE capped WARN.
- chaos: corrupt (truncate) one leg's parquet mid-lake → the read must skip/quarantine that leg's
  structure (counted), never abort the whole sim (P5 posture; movers corrupt-skip precedent per
  CLAUDE.md DEC-MOVERS-CORRUPT-SKIP-001 blurb).

**Reuse.** P1 (counted rejects); P2 (`NO_CONTRACT` split + date-gates-first); P3 (preflight
refusal naming exact missing pieces); P7 (honest-miss reader); P10 (ledger states for cause
disambiguation); `fno_cascade.build_expiry_counts` listed-vs-data idiom (`fno_cascade.py:229-243`)
as the operator-facing census display.

---

### E-02 — Volume-0 / illiquid bars on a leg at a DECISION instant — and the deferred-exit law

**Origin:** EC2-4 (registrar 2).

**Scenario.** At a decision instant `t` (entry, SL/TP trigger, forced exit), one leg's 1-minute
bar is absent, or present with `volume == 0`.

**Families hit & severity.** Routine, not exotic: "zero-volume option minutes are routine"
(`brutex/data/today_completeness.py:71-72`). Entry-side it is harmless (skip the trade). EXIT-side
it is the writer's second-worst corner after gaps: the untradeable-exit minutes correlate with
adverse conditions, so naive censoring hides blow-ups (the repo's own survivorship disclosures —
`brutex/options/executor.py:606-617` and `brutex/futures/executor.py:37-41` per sibling sections
repo-exec / gap-fill §2(ii)).

**Detection (repo evidence; verified by registrar 2 unless attributed).**
1. **The uniform reject law (entry+exit, both executors, no knob):** a `volume == 0` bar is
   rejected `STALE_BAR` UNCONDITIONALLY — "a minute bar with volume == 0 carries a stale/carried
   OHLC — nothing traded, so pricing a fill at its open (even 1-tick-slipped) fabricates an
   unachievable execution" (`brutex/options/executor.py:543-557`; futures twin + recorded
   survivorship cost `brutex/futures/executor.py:31-41` per sibling section repo-exec; the knob
   deletion is DEC-FILL-WORST-CASE-ONLY-001 ruling 2 per sibling section gap-fill §2(ii)).
2. **Absent bar ⇒ honest `None` ⇒ `NO_DATA`:** "`None` when the contract is not in the lake OR
   either bar is missing (the option did not trade that minute) — the trade is then rejected
   NO_DATA, never faked" (`brutex/options/lake_source.py:151-157`).
3. Today's executor shape rejects the WHOLE trade when either the entry OR exit bar fails these
   gates (the pair carries both bars) — sound for a post-hoc translation layer, but an ENGINE
   holding state cannot retroactively unexist an accepted entry: the exit-side gap is a genuinely
   NEW design surface.
4. Walk-the-1m-truth precedent: the movers CE/PE baselines walk back through prior-session
   1-minute bars rather than trust a frozen aggregate
   (`brutex/dashboard/movers_snapshot.py:1732-1737` per sibling section repo-quality).
5. The liquidity-class machinery (λ volume floor, predicates FH-1…FH-6, Class A/B/C — §5 Law A) is
   the fill-realism section's §3; this entry composes with it rather than restating it.

**Handling law — the DEFERRED-EXIT law.**
- **D1 (defer):** when an exit decided at instant `t` finds leg L's bar absent or `volume == 0`,
  the exit DEFERS: walk forward minute-by-minute within the SAME session to the first bar of L
  with `volume > 0`, and fill there under the unchanged worst-case law — the deferral bar's
  adverse extreme ± tick, WITH-level clamp when the decided reason is SL/TP
  (`max(level, high) + tick` for a short cover). Adverse drift during the deferral is therefore
  CAPTURED (the fill anchors on the later bar); the decided exit REASON is preserved from `t`.
- **D2 (count):** `n_deferred_exits`, per-trade `deferred_minutes` (and the run max) are
  first-class counters in the `OptionsRuleResult`-extension shape (fill-realism §3.3);
  annotation-only, never re-ranking.
- **D3 (session-end escalation):** if no `volume > 0` bar for L occurs before the session's last
  bar, the structure escalates deterministically, in order: (i) overnight mode ON and the leg not
  at expiry ⇒ carry to the next session and re-run D1 from its first bar (compounding with E-10
  gap exposure — both flags stack); (ii) the leg's expiry instant is reached ⇒ the E-07 expiry
  machine resolves it (or, pre-machine, the structure is CENSORED); (iii) otherwise (intraday
  mode) ⇒ CENSOR the structure (Class-C, `censored_open_exit` counted, first-failing-leg identity
  stamped). Pricing the close at a volume-0 print is FORBIDDEN — it would break the
  demonstrated-transaction anchor the whole fill law rests on (fill-realism §1.1). *(See §5 Law B
  for the design fork between this censor arm and E-03's retro-close arm.)*
- **D4 (make it rare, don't paper it):** the entry-side λ liquidity floor + liquid-band predicates
  (fill-realism §3.1, FH-3/FH-4) are the mechanism that keeps D3 rare; D2/D3 counters are the
  proof per run. A config whose edge concentrates in deferred or censored exits is flagged on the
  result surface.
- **D5 (structure atomicity):** deferral is PER LEG, but closure is per STRUCTURE — a
  structure-closing event closes every leg on its own first-tradable bar; a short leg's deferral
  never leaves a naked residue booked as closed (composes with the Class-C atomic-reject stance,
  fill-realism §3.2).

**Honest direction-of-bias statement (must ship with the law).** The deferred fill is the first
DEMONSTRABLE fill after `t` — it is NOT strictly pessimistic versus a hypothetical instant
quote-fill at `t` (a real quote may have existed in the gap minutes — unknowable from this lake,
gap-fill U7): if the price collapses in the trader's favor during the deferral, D1 books a better
exit than an instant fill would have. It IS the only fill this data class can prove, and per-bar
it is worst-case-anchored. State exactly this; never claim the deferral is "conservative" without
the qualifier.

**Cannot-model.** Quote state during zero-trade minutes (gap-fill U7); the counterfactual of the
trader's own resting order attracting a fill in those minutes; any sub-minute exit timing.

**Test types.** Unit: D1 walk over a fixture with a 7-minute volume-0 run (fill at minute 8's
extreme + tick; `deferred_minutes == 7`). Property: booked deferral fill ≥ with-level clamp on
every intermediate volume-0 bar's phantom price (the never-better-than-a-real-print invariant).
Regression: D3(iii) censoring with a same-structure healthy leg — the healthy leg must NOT book a
lone close.

**Reuse.** P1 (typed counted gates — the `STALE_BAR`/`NO_DATA` laws inherited verbatim); P7
(honest-miss reader); the worst-case fill law (`brutex/execution/fill_mode.py:10-13`); the
fill-honesty class machinery (§5 Law A).

---

### E-03 — Missing-leg data MID-TRADE (a leg of an open structure goes dark, session scale)

**Origin:** EC-3 (registrar 1).

**Scenario.** A structure is OPEN across minutes/hours (or, in a future positional mode, days).
One leg stops printing: (a) routine zero-trade minutes (normal for options); (b) a genuine vendor
hole — one leg dark for a stretch (an afternoon, a whole session) while sibling legs + the
underlying print; (c) the session's stored bars end early for that leg and the next stored bar is
next session (the session-tail hole).

**Families hit & severity.** Any family with per-bar management: structure-level SL/TP, per-leg
stops on short strangles, delta-band rebalancing, margin marking. A dark short leg makes structure
MTM UNKNOWN — not zero, not last-known-true. Mid-trade adjustments (rolling the tested side) need
a live premium for BOTH the closing and the opening leg. This is the options analogue of the spot
engine's session-tail hole, which the repo already treats as a first-class law (P4).

**Detection (data signature; existing detectors).**
- (a) is NOT a defect: per-minute absence/zero-volume is ROUTINE for CE/PE — "zero-volume option
  minutes are routine; sparsity ≈ truncation" (`brutex/data/today_completeness.py:71-72`), and
  CE/PE is EXCLUDED from the partial-window watch because "an option's bars die from illiquidity,
  indistinguishable from truncation"
  (`brutex/adapters/groww/option_ingest.py:1630-1635,1657-1658`). Single-minute absence must
  NEVER alarm.
- (b) session-scale signatures that ARE armable:
  - The interior-gap shape — a CONTIGUOUS run of bar-less TRADING SESSIONS, bracketed on both
    sides by sessions with bars, exceeding a tolerance (default 5) — is the repo's
    false-positive-safe classifier (`brutex/data/interior_gap_detect.py:28-47`). It is pure and
    reusable, but NOT armed for options anywhere today (the F&O spine never calls it — per sibling
    section repo-quality; the spot demotion is `detect_completeness_gaps AND is_index`-gated,
    `brutex/adapters/groww/ingest.py:1526` per that section).
  - The stronger NEW cross-sectional signature for an OPEN structure: leg L prints 0 bars over
    span [t1, t2] while ≥K same-expiry sibling contracts AND the underlying each print ≥N bars in
    the same span. Relative presence separates "market closed / feed dead" (everything dark) from
    "this leg dark" (the hazardous case). Nothing computes this today (repo-lake G3: no
    chain-at-instant index).
  - Ledger evidence: the contract's window rows FAILED/EMPTY for the covering window (P10) — but
    a SUCCESS window can legally contain the hole (the interior-gap module's founding observation,
    `interior_gap_detect.py:1-11`).
- (c) session-tail: the IST session-date change on the next pushed bar — exactly the spot walker's
  trigger (`brutex/engine/trade_sim.py:60-67`).
- The whole-underlying blackout has its own alarm already: `pull.underlying_all_empty` fires when
  every cleanly-fetched pair of an underlying stored 0 bars (per sibling section repo-quality,
  `option_ingest.py:1385-1413,1439-1542`).

**Handling law (correct-or-absent, tiered by decision role).**
1. Marking tier: a dark minute on one leg ⇒ structure MTM is UNKNOWN at that instant. Carry-last
   is permissible for OBSERVABILITY surfaces only (the OI carry-LAST precedent — state snapshot,
   never flow: `brutex/data/tf_resampler.py:30-31,241` per sibling section repo-quality; OI-aware
   resample `brutex/data/fno_resample.py:48-49`). Decision triggers must never fire off a carried
   premium.
2. Trigger tier: SL/TP/rebalance rules ABSTAIN on unknown-MTM instants and count
   `mtm_unknown_minutes` per structure (counted, capped-WARNed — P6). Never fabricate a trigger
   price; never evaluate the trigger against the last-known premium as if current.
3. Exit tier (the leg stays dark to the horizon): close the STRUCTURE retroactively at the last
   instant every leg has a stored bar, pricing each leg at its adverse extreme of its last stored
   bar — the `session_gap`/`end_of_data` pricing law generalized per leg (P4,
   `trade_sim.py:58-67`), under a NEW append-only exit reason (e.g. `leg_dark`), appended per the
   vocabulary law (`trade_sim.py:164-171`). Count per structure; ONE capped WARN per
   (contract, session). *(§5 Law B records the fork between this retro-close arm and E-02
   D3(iii)'s censor arm, plus the reconciliation constraint.)*
4. Session-boundary tier: an open structure never survives an IST session-date change unless the
   engine has an explicit positional mode (its own DEC); until then the spot law transplants
   verbatim — retro-close at the prior session's last stored bars (P4).
5. Self-healing honesty: a vendor hole later re-pulled changes the bars → run identity re-keys via
   `data_snapshot_hash` (CLAUDE.md §2 Rule 4), so healed-lake reruns diverge HONESTLY; the
   `mtm_unknown`/`leg_dark` counters are the disclosure that the pre-heal run was hole-shaped.
6. Never: interpolate premium across the hole; substitute the sibling strike; mark the dark leg at
   model value (intrinsic/BSM) without a DEC'd model — model-marking is a design DECISION, not a
   default.

**Test types.**
- unit: abstain-on-unknown trigger logic; per-leg adverse-extreme retro pricing; the dark-span
  classifier (relative-presence signature) on planted frames.
- property: (i) suffix-independence transplant — mutating/deleting bars strictly after the
  structure's exit changes nothing (the sweep suffix-independence gate pattern, CLAUDE.md §4
  DEC-SWEEP-LOOKAHEAD-GUARD-001 blurb); (ii) no recorded trade P&L depends on any fabricated
  price — mutate the dark span's would-be values, closed trades bit-identical.
- golden: planted one-leg-dark-afternoon fixture → counted + retro-close bytes pinned; two-session
  fixture with a session-tail hole on one leg → `leg_dark`/`session_gap` close pinned.
- integration: real lake, two sessions, delete leg L's session-2 rows via raw parquet rewrite;
  structure opened session 1 closes correctly; ledger untouched.
- chaos: corrupt the dark leg's month file DURING the sim's read (TOCTOU-style swap) → isolated
  counted error, sim continues (P5 isolation posture).

**Reuse.** P4 (`session_gap` retro-close + append-only exit vocabulary + the `exits_session_gap`
counter chain — `brutex/engine/combo_aggregates.py:263,294,352,620` grep-verified); P6 (one WARN
per session); P12 (interior-gap classifier as the session-scale shape, re-tolerance'd for
options); P10 (ledger evidence); P5 (quarantine/isolation); P7 (honest-miss reads).

---

### E-04 — Underlying data hole while options trade (spot bar missing, option bars present)

**Origin:** EC-8 (registrar 1).

**Scenario.** At decision instant M the option chain prints but the underlying spot (INDEX/CASH
1-minute) has no bar at M — a single-minute hole, a drifted/renamed spot partition key, or a whole
spot session missing while the chain prints.

**Families hit & severity.** Entry-time strike selection anchors on spot (ATM/offset rules consume
`spot_at_entry` — `brutex/options/executor.py:203-204,210-214`; moneyness needs spot);
delta-managed families need spot at every management instant; settlement approximation needs the
spot series; regime overlays (PCR vs spot) mislead when the spot side is hole-shaped. In an
options-native engine the spot read is a NEW seam (today `spot_at_entry` arrives from the spot
engine's own feed), so the hole-handling law must be designed in, not inherited implicitly.

**Detection (data signature; existing detectors).**
- Per-instant: option bar present at M ∧ no spot row at M after the drift-recovery ladder ran. The
  Greeks enrichment is the canonical precedent: exact UTC-instant spot join, and a missing minute
  is `STATUS_NO_SPOT` — "No underlying spot minute — NEVER a carried-forward/next-bar spot"
  (`brutex/data/greeks_enrich.py:1042-1044`); an empty join leaves the whole chain honestly
  NO_SPOT (`:1294-1297`). The drift-recovery ladder (index aliases both directions + the
  master-verified `stock_drift` map) already distinguishes "key drifted" (recoverable) from "data
  absent" (honest miss) — per sibling section repo-quality (`greeks_enrich.py` SPOTJOIN ladder;
  movers `_SpotLtpResolver` twin).
- Stored detector: the Greeks overlay IS a persisted census of this condition — a per-contract
  spike in stored `STATUS_NO_SPOT` stamps over a span marks the spot hole after the fact
  (queryable without re-joining; `greeks_status` is a /db derived column per sibling section
  repo-lake).
- Session-scale: for the ENGINE-trio indices a spot hole is already a DETECTED, demoted, re-pulled
  defect (interior-gap/truncation demotion is armed exactly for `is_index` spot —
  `brutex/adapters/groww/ingest.py:1526` per sibling section repo-quality; the INDEX count law
  "indices always print" `brutex/data/today_completeness.py:73-74`), so index-spot holes are
  transient-with-machinery; CASH-stock spot holes are observe-only (weaker) — another
  index-first-scope argument.
- Moneyness with no spot classifies `UNKNOWN`, never guessed (per sibling section repo-lake,
  `brutex/data/moneyness.py:68-103`).

**Handling law (correct-or-absent).**
1. Entry: no spot at the entry instant (post-ladder) ⇒ ATM/offset selection is impossible honestly
   ⇒ counted skip (`NO_SPOT` structure-level reason, mirroring the stored status vocabulary — P8).
   Never nearest-prior spot silently. A bounded look-back (e.g. ≤60 s) is a DEC-able
   approximation: if ever adopted it must be counted + disclosed per trade (`spot_lookback_used`),
   never default.
2. Mid-trade: delta/gamma management ABSTAINS on NO_SPOT instants (count `mgmt_abstain_no_spot`);
   premium-based rules (structure SL/TP on leg prices) may continue — premium is observable even
   when spot is not. Exercise/settlement decisions never run off a fabricated spot.
3. Whole-session spot dark while the chain prints: refuse NEW entries for that underlying that day
   (regime-blind entry is not a strategy, it's a coin flip) + the standard re-pull remediation
   (index machinery heals it); structures already open fall under rule 2 + E-03's marking tiers.
4. Key drift vs absence must stay separated: the ladder RECOVERS drifted keys (counted INFO,
   `spot_key_recovered` precedent) and only a true miss becomes NO_SPOT — copying the
   ambiguity-POISONED rule (an ambiguous master fold never guesses; per sibling section
   repo-quality / CLAUDE.md DEC-GREEKS-SPOTJOIN-STOCK-001 blurb).
5. The spot read seam should inherit the spot readers' gates (price-sanity + ts-range —
   `price_sanity` consumers per sibling section repo-quality) so a corrupt spot bar refuses rather
   than mis-anchors ATM.

**Test types.**
- unit: join-miss → NO_SPOT skip; ladder recovery still honest-miss on a true hole; bounded
  look-back OFF by default.
- property: no accepted entry lacks a same-instant spot; management decisions are invariant under
  mutation of spot bars strictly after the structure's exit (no-look-ahead transplant); NO_SPOT
  counts are conserved (skips + accepts == opportunities).
- golden: planted one-minute spot hole at the entry minute → counted skip pinned; stored
  Greeks-overlay NO_SPOT stamps pinned on the same fixture.
- integration: real lake, delete the spot month file for one session → chain still readable,
  entries refused + counted, INDEX machinery's demotion path exercised separately.
- chaos: spot dir renamed to a drifted spelling → ladder recovers (INFO, trades proceed);
  ambiguous two-way fold → poisoned, refused, never guessed.

**Reuse.** P8 (`STATUS_NO_SPOT` never-fabricate law + stored stamps as census);
`spot_recovery_candidates`/`stock_drift` ladder + `spot_key_recovered` INFO (per sibling section
repo-quality); moneyness `UNKNOWN`; P1 (counted skip); price-sanity/ts-range gates on the spot
seam; the movers corrupt-skip posture for reader resilience.

---

### E-05 — IV-unsolvable regions — the condition-abstain law

**Origin:** EC2-6 (registrar 2).

**Scenario.** A strategy condition consumes IV / a Greek ("IV rank > X", "delta < 0.20", "short
the 0.15-delta strike") on a bar where the enrichment could not solve: deep-ITM near expiry
(premium ≤ intrinsic within tolerance), missing spot join, missing rate, at/past expiry, or
corrupt inputs — or where the enrichment overlay simply is not present.

**Families hit & severity.** Unsolvable-IV states are CORRELATED with the interesting states: a
tested short strike that has just been gapped through is deep-ITM — exactly `BELOW_INTRINSIC`
territory — so "missing IV" clusters on the bars where a Greek-based DEFENSE would matter most. An
abstain law that silently disables defenses is a trap.

**Detection (repo evidence, verified by registrar 2).**
1. **The status vocabulary + precedence exist and never fabricate:** per-bar screens stamp
   `STATUS_NO_RATE → STATUS_AT_EXPIRY → STATUS_NO_SPOT → STATUS_BAD_INPUT → STATUS_BELOW_INTRINSIC`
   in that order, "and only then solve" (`brutex/data/greeks_enrich.py:1036-1055`; docstring law
   "a corrupt/rejected bar … stamps BAD_INPUT; NEVER a fabricated Greek" `:694-702`). The
   degenerate band: `close ≤ floor + DEGENERATE_TIME_VALUE_REL × close` with
   `DEGENERATE_TIME_VALUE_REL = 1e-9` ⇒ `BELOW_INTRINSIC` NULLs "instead — never an arbitrary
   stored" IV from the indeterminacy band (`:246-258`, `:665`, `:1055`).
2. **Displays already abstain honestly:** absent spot/junk inputs classify `Moneyness.UNKNOWN`,
   "rather than a fabricated bucket" (`brutex/data/moneyness.py:19,45,61,96`).
3. **The engine has a bit-abstain precedent:** on futures `1day` grids the intraday-only bits
   (ToD 44-47 + VWAP 52-53) are "HONESTLY cleared via the vocabulary's own `APPLIES_TO_1DAY_MASK`
   (one bit-set source of truth)" (`brutex/engine/stage1a.py:1050-1056`; the pure predicate
   `apply_1day_mask_for` `:1319-1349`); and per the DEC-ENGINE-SPOT-ONLY-001 ledger blurb the VWAP
   bits "permanently abstain on every engine instrument" for volumeless spot (CLAUDE.md §2 Rule 3
   retirement note — ledger claim).
4. Delta-target strike rules are ALREADY gated on a chain provider that is not wired — "no
   production delta-chain provider is wired" (per sibling section repo-exec,
   `brutex/options/executor.py:33-36`); enrichment coverage caveats: expired contracts may carry
   v1 stamps until backfilled, and a CLI-only lake may hold no overlay at all (per sibling section
   repo-quality row 18, ledger #1558).

**Handling law — the two-sided abstain law.**
- **L1 (entry conditions):** any ENTRY condition consuming a NULL/absent Greek/IV input evaluates
  NOT-TRUE (bit = 0) — never a fabricated value, never an error — and per-bit abstain COUNTERS are
  reported per run (the `APPLIES_TO_1DAY_MASK` clearing idiom generalized from grid-static to
  per-bar-dynamic). Consequence: combos REQUIRING the bit cannot fire on abstained bars —
  conservative for entries.
- **L2 (defense/exit conditions — the dangerous side):** abstain-to-0 on an EXIT predicate
  silently disables the defense on exactly the correlated-bad bars (severity note above).
  Deterministic v1 law: **Greek/IV-based predicates are ENTRY-ONLY; every exit/defense must be
  price- or time-based** (premium SL/TP, structure stop, time stop, forced exit) — all of which
  are never-NULL and already have engine machinery. A future v2 that wants Greek-based defenses
  must choose per-condition between FAIL-SAFE abstain (NULL ⇒ treat as TRIGGERED ⇒ worst-case
  exit) and a mandatory paired price backstop — its own DEC clause, not a default.
- **L3 (column-absent ≠ NULL is FALSE):** the law must handle a missing overlay COLUMN (never
  enriched) identically to a NULL value, plus one loud per-run WARN naming the coverage gap
  (v1-stamp / no-overlay lakes — evidence row 4).
- **L4 (identity):** whether a family uses Greek-gated entries is already captured by its
  condition set; the abstain law itself is engine semantics (no knob). Abstain counters are
  outputs, out of identity — mirroring the fill-realism §3.3 counter stance.

**Cannot-model.** True market IV in the degenerate band (the 1e-9 band exists BECAUSE the root is
numerically indeterminate there — any value would be arbitrary, evidence row 1); vendor-quoted IVs
(not stored: `spot`/`iv` honestly `None` in the candle lake reader —
`brutex/options/lake_source.py:13` per sibling section repo-exec); Greeks are v2 MODEL enrichment,
not exchange truth (per sibling section repo-quality row 18).

**Test types.** Unit: every status class ⇒ bit abstains + counter increments (parametrized over
the five statuses + column-absent). Property: no combo containing a Greek-gated entry bit fires on
a bar whose overlay row is NULL. Negative pin: an exit predicate registry containing a Greek-based
exit REFUSES to build under the v1 law (loud, typed).

**Reuse.** P8 (status vocabulary + precedence ladder); the `APPLIES_TO_1DAY_MASK` bit-abstain
idiom (`stage1a.py:1050-1056,1319-1349`); moneyness `UNKNOWN`; P6 (capped WARN on coverage gaps).

---

### E-06 — Expiry day (0DTE): entry, the last 30 minutes, and the settle endgame (pin risk)

**Origin:** EC-1 (registrar 1) + EC2-7 (registrar 2), merged — the ONE duplicate pair across the
two inputs: EC-1 owns the ENTRY side, EC2-7 the ENDGAME/EXIT side of the same scenario family.

**Scenario.** (Entry side) A strategy enters a position on the leg's own expiry date: (a) 0DTE
entry at/after the open (short straddle/strangle 09:20 on NIFTY Tuesday); (b) entry inside the
final 30 minutes (≥15:00 IST) — inside the very window whose constituent VWAP builds the final
settlement price (FSP); (c) entry inside the Greeks kernel's 600-second trust floor before the
15:30 expiry instant. (Endgame side) Expiry day, final stretch: near-ATM gamma explodes as premium
collapses toward intrinsic; the seller of a strike near the evolving close cannot know at 15:29
whether it finishes ITM. The engine must decide (buy back? hold to settle?) on 15:29 information
while the settle is computed AFTER 15:29 from the closing construct.

**Families hit & severity.** 0DTE premium selling (straddles/strangles/iron condors/flies) is the
flagship post-Nov-2024 family — exactly two index 0DTE days/week survive (NIFTY Tue on NSE, SENSEX
Thu on BSE; per sibling section micro-expiry §6.4, sourced to
[SEBI CIR/2024/132](https://www.sebi.gov.in/legal/circulars/oct-2024/measures-to-strengthen-equity-index-derivatives-framework-for-increased-investor-protection-and-market-stability_87208.html)).
Indian index option writing CONCENTRATES on expiry day; the last-30-min regime is where quotes
degrade exactly as the decision matters (per sibling section gap-fill §2(iv)). Last-30-min entries
interact with three structural facts: the FSP is the last-~30-min constituent-VWAP index close,
NOT the 15:29 spot print (per sibling section micro-settle §2.5,
[NSE Clearing settlement price](https://www.nseclearing.in/clearing-settlement/equity-derivatives/settlement-price));
expiry-day short options carry +2% ELM from 2024-11-20 (per sibling sections
micro-settle §2.2 / micro-expiry §2.2); and the repo's forced-exit precedent is 15:20 IST
(CLAUDE.md §7; `brutex/engine/trade_sim.py:108-113` imports `is_forced_exit_bar` /
`forced_exit_seconds_for_session`), which sits BEFORE the settle window — so an engine reusing the
15:20 law structurally never holds through settlement, while any wider exit policy immediately
needs an FSP model. A pinned short flips between "keep full premium" and "pay `(settle − K) ×
lot`" on one tick of settle (per sibling section micro-settle §6) — a P&L discontinuity the sim
must price, not assume away.

**Detection (data signature; existing detectors).**
- 0DTE predicate: `parse_fno_symbol(symbol).expiry == entry_ts.astimezone(IST).date()` — the
  right-anchored contract grammar is the ONE decoder (per sibling section repo-lake,
  `brutex/data/fno_symbol.py:16-21,178-216`; registrar 2's independent read `:14-21`);
  alternatively the registry `expiries` rows (per sibling section repo-lake,
  `brutex/data/registry_store.py:103-112`).
- Last-30-min predicate: entry IST wall-clock ≥ `session.close_time_ist − 30min` — MUST be
  session-object-derived, not the 15:30 constant: the NSE F&O close moves to 15:40 with a
  15:10–15:40 settle VWAP on 2026-08-03 (per sibling section micro-expiry §6.3,
  [Groww blog](https://groww.in/blog/nse-extends-f-and-o-trading-hours-by-10-minutes-new-timings-effective-from-august-3-2026)).
- Instants (registrar 2's reads): expiry instant 15:30
  (`brutex/data/greeks_enrich.py:239-240`); session close 15:30 exclusive with the last regular
  bar labeled 15:29 (`brutex/calendars/nse.py:124`; close-edge completeness law
  `session_last_bar_complete` `:902`); the engine's forced exit already sits at close − 10 min
  (`FORCED_EXIT_IST = 15:20`, `nse.py:125`; forced-exit override semantics
  `trade_sim.py:46-54`, `:703-714`).
- T-floor zone: `t_years < T_FLOOR_YEARS` (600 s in Act/365 years) already computed + stamped by
  the Greeks enrichment — bars at/inside the floor carry `STATUS_AT_EXPIRY` NULLs
  (`greeks_enrich.py:1037-1041`; the 15:30 expiry-instant anchor `:236-240`; floor note
  `:242-244`; the `AT_EXPIRY` stamp with "such bars should not exist upstream" `:477`). Deep-ITM
  0DTE bars additionally read `STATUS_BELOW_INTRINSIC` all day once time value ~0 (`:1052-1062`;
  band definition `:246-258`) — so the STORED Greeks overlay itself is the detector for
  "delta-selection impossible here".
- Settle observability: none — `grep -rn "settlement_price\|settle_price" brutex/ --include=*.py`
  → zero hits (registrar 2's session; independently matches the micro-settle §4.3 search); the
  lake's daily close is BY LAW the last 1-minute close (`brutex/data/spot_daily_resample.py:32,39`;
  `brutex/data/tf_resampler.py:235`).
- Existing executor-side detector: **NONE** — `materialize_trade`'s gate ladder is lot →
  cost_regime → no_data → stale_bar → tick_floor (`brutex/options/executor.py:479-481,497-564`);
  it never inspects entry-vs-expiry distance. `SpotSignal.expiry_date` only requires
  `>= entry_ts.date()` (`executor.py:205-207`) — 0DTE entries flow through un-classified today.
- Liquidity signatures on 0DTE: far wings die (the repo's own rationale — "an option routinely
  stops printing bars several sessions before expiry … liquidity death in the final week",
  `brutex/adapters/groww/option_ingest.py:1630-1634`) while ATM prints densely; a wing leg's
  absent/zero-volume minute is the E-01/E-02 signature (`STALE_BAR` at `executor.py:543-557`).
- Regime flags with zero new data: `expiry_day` (leg's decided-bar date == contract expiry,
  parseable from the symbol — `fno_symbol.py:14-21`) and `last_30min` (decided bar ≥ 15:00 IST) —
  proposed by the fill-realism section §2(iv), adopted here.

**Handling law (correct-or-absent).**

*Entry side (from EC-1):*
1. 0DTE entry per se is a legitimate strategy axis — never refused as a data defect. But it must
   be a FIRST-CLASS classified dimension (a `dte` coordinate on the structure), or 0DTE results
   blend silently into positional results.
2. Hold-to-expiry is UNIMPLEMENTED today and must REFUSE loudly: the calculator's Step-0 guard
   raises `UnsupportedTradeOutcomeError` on any non-`NORMAL_CLOSE` outcome and the §4.2 expiry
   state machine is a spec-only fail-loud stub (per sibling section repo-exec,
   `docs/track2/CALCULATOR_SPEC.md:275-288`, `brutex/costs/calculator.py:237-251`). Until an
   expiry-engine DEC lands, a 0DTE structure MUST exit via an explicit pre-close policy (the 15:20
   precedent) — a trade whose exit would be "settlement" is a typed, counted refusal
   (`HELD_TO_EXPIRY_UNSUPPORTED`, P1 pattern), never silently settled at the 15:29 close.
3. If/when hold-to-expiry is implemented: intrinsic-at-expiry must be settled against an FSP MODEL
   with an error band, never the last 1-minute close as-if-settle — the residual is exactly the
   pin-risk band; worst-case discipline resolves a pinned short at the ADVERSE plausible settle
   (per sibling section micro-settle §6.5; DEC-FILL-WORST-CASE-ONLY-001 precedent,
   `brutex/execution/fill_mode.py` per sibling section repo-exec). No `settlement_price` producer
   exists in the repo — the FSP series is a NEW data/model surface with its own DEC (see E-07 L3/L4
   for the concrete proxy-band law).
4. Entries whose strike selection needs Greeks (delta-target rules 8-10) inside the T-floor zone:
   counted skip — the chain is honestly `AT_EXPIRY`-NULL there (P8); offset rules (1-7) may still
   run. Never solve IV inside the floor "just this once".
5. Short-leg margin on expiry day (+2% ELM) rides the margin regime table — which is all-refusal
   today (P9, `brutex/engine/margin_history.py:10-23`), so 0DTE short-family margin-ROI reads
   `unverified` until SPAN data lands; the ELM step must be a dated regime row then, not a
   constant (see E-16).
6. The 2026-08-03 session change: expiry-day session shape must be per-(segment, date) DATA. The
   repo session model is a 15:30 constant today (`brutex/calendars/nse.py:117,124`) — a design
   input, flagged by the micro-expiry section §6.3.

*Endgame side (from EC2-7) — decide-at-the-last-instant, worst-of.* For every SHORT leg still
open at its final decision instant `T_f` (the engine's forced buy-back instant; default 15:20 per
the spot law, sweepable EARLIER via a `no_exit_after` knob, never later than the last tradable
minute 15:29):
- **Route (a) — buy back at `T_f`:** worst-case fill on the leg's `T_f` bar (`high + tick`,
  with-level clamp if a level triggered), full sale-side trade charges per the encoded date
  regime; illiquidity at `T_f` routes through the E-02 deferral (whose D3 ladder on expiry day
  terminates in route (b), because the leg's expiry instant arrives).
- **Route (b) — hold to settle:** the E-07 L2/L3 resolution against the adverse settle in
  `[proxy − ε, proxy + ε]`: for a short call, `intrinsic_adverse = max(0, (proxy + ε) − K) × lot`
  (mirror for puts); an assigned short pays NO STT; the charges model per EU-16 (E2-U2)'s chosen
  branch. For a LONG leg the routes mirror (sell at 15:29 vs exercise with purchaser-side STT on
  intrinsic).
- **The worst-of law:** when the strategy declares hold-to-expiry, the sim books the WORSE (for
  the position) of (a) vs (b) computed per date-regime — never the better, never a blend. When the
  strategy declares buy-back-by-`T_f`, only (a) applies. The (a)-vs-(b) comparison is
  regime-dependent by construction (STT flip at 2026-04-01 — per sibling section
  micro-settle §3.3), so it must be COMPUTED from the regime tables, never hardcoded.
- **Pin-risk accounting:** a short strike with `|proxy − K| ≤ ε` is by definition resolved ITM at
  depth up to ε under (b) — the pin penalty is `ε × lot`, making ε operationally THE pin-risk
  band. ε is swept + identity-carried when non-default (E-07 L3); the per-run count
  `n_pinned_strikes` (strikes resolved inside the band) is a first-class honesty counter.
- **Singularity hygiene:** inside `last_30min` on `expiry_day` no Greek-consuming logic may run
  (E-05 L2 + `AT_EXPIRY`/`BELOW_INTRINSIC` statuses); the per-minute bar RANGE becomes enormous
  relative to premium, so the worst-case bound is intentionally brutal here — the design accepts
  that (never more optimistic than reality, the `brutex/execution/fill_mode.py:3-4`-family law)
  and offers `no_entry_after` / `exclude_expiry_day` as sweepable strategy knobs rather than
  softening the fill law (gap-fill §2(iv) handling, adopted).

**Cannot-model.** The official close's constituent-VWAP construction from index 1m bars (ε
irreducible until settlement-price ingest — E-07 L4); closing-session order flow/auction behavior
after 15:29; expiry-afternoon quote quality (gap-fill U5 — India magnitudes UNVERIFIED);
sub-minute timing inside the final
minute.

**Test types.**
- unit: 0DTE/last-30-min classifiers at IST boundaries (09:15 open bar, 15:00:00 exact, 15:29,
  session-close-aware post-2026-08-03 shape); T-floor boundary at exactly 600 s.
- golden: one 0DTE fixture day — entry 09:20/exit 15:20 priced; entry 15:25 with hold-to-settle
  request → typed refusal bytes pinned; deep-ITM bar → BELOW_INTRINSIC stamp pinned; a pinned
  short call at `K = proxy` books exactly `ε × lot` under route (b), and the worse-of law selects
  correctly on both sides of the (a)/(b) crossover in BOTH STT regimes.
- property: no accepted trade's exit instant exceeds its expiry date's session close; entering ANY
  minute of expiry day never yields an exit after close; refusal-count + accept-count == signals;
  worst-of is monotone in ε; route (a) with a triggered level never books better than
  `max(level, high) + tick`.
- integration: real-writer lake (ParquetBarSink) with a full 0DTE chain; sim counters + ONE
  aggregate WARN.
- chaos/regression: vendor bars stamped after the clamped window end on expiry day → future-bar
  drop path (`option_ingest.py:941-958`) — engine sees none; `T_f` deferral on an illiquid 0-DTE
  leg terminates in route (b), never in a volume-0 print.

**Reuse.** P1 (typed counted reject — `HELD_TO_EXPIRY_UNSUPPORTED` mirrors `LOT_SIZE_UNKNOWN`);
the Step-0 `UnsupportedTradeOutcomeError` refusal (per sibling section repo-exec); the 15:20
forced-exit law (`trade_sim.py:108-113`); P8 (`AT_EXPIRY`/`BELOW_INTRINSIC` stamps as the
selection gate); P6 (capped WARN); P9 (dated regime rows for the ELM step when margin data
lands); the E-02 deferral ladder; the E-07 settle-proxy band.

---

### E-07 — Assignment / expiry while the position is open

**Origin:** EC2-2 (registrar 2).

**Scenario.** Any leg of an open structure reaches its expiry instant still open: a long ITM leg
auto-exercises; a short ITM leg is assigned at the FINAL SETTLEMENT price; OTM legs lapse. The
seller's information problem: the settle is computed AFTER the last tradable minute, from the
underlying's closing construct — not the option's (or the underlying's) 15:29 last trade.

**Families hit & severity.** Every hold-to-expiry option strategy (covered writes, condors run to
settlement, calendar back legs) terminates through this event; near-`K` strikes flip between
"keep full premium" and "pay `(settle − K) × lot`" on one tick of settle — a P&L discontinuity,
not a tail event (per sibling section micro-settle §6). STT is leg-role-asymmetric: the exercised
LONG pays 0.125% (0.15% from 2026-04-01) on intrinsic; the ASSIGNED SHORT pays NO STT at expiry;
an OTM lapse pays none (per sibling section micro-settle §3.1/§3.3, encoded rows
`brutex/costs/constants.py:82-95` per that survey).

**Detection (repo evidence, verified by registrar 2 unless attributed).**
1. **Hold-to-expiry is a fail-loud STUB today:** `TradeOutcome`'s `EXPIRY_EXERCISE /
   EXPIRY_ASSIGN / EXPIRY_WORTHLESS` refuse at the calculator's Step-0 guard — spec-only
   forward-compat, "Implementing them requires a new DEC"
   (`docs/track2/CALCULATOR_SPEC.md:275-288` quoted in full by the repo-exec section; code anchors
   `brutex/costs/types.py:219-228`, `brutex/costs/calculator.py:237-251` per that survey).
2. **The expiry INSTANT convention exists and equals the session close:**
   `EXPIRY_HOUR_IST = 15` / `EXPIRY_MINUTE_IST = 30` (`brutex/data/greeks_enrich.py:239-240`),
   and the calendar root documents the deliberate non-unification: "the F&O expiry-INSTANT
   convention … merely EQUALS this wall clock — it is a distinct contract-expiry law, deliberately
   NOT unified" (`brutex/calendars/nse.py:122-124`; `SESSION_CLOSE_IST = 15:30` `:124`). Greeks
   bars at/after the instant stamp `AT_EXPIRY` ("such bars should not exist upstream" —
   `greeks_enrich.py:477`).
3. **No settlement price exists anywhere in the repo:**
   `grep -rn "settlement_price\|settle_price" brutex/ --include=*.py` → **zero hits** (registrar
   2's session; independently matches the micro-settle §4.3 search). The lake's daily close is BY
   LAW the last 1-minute close: "close=last … last-minute close"
   (`brutex/data/spot_daily_resample.py:32,39`; TF resampler `pl.col("close").last()`
   `brutex/data/tf_resampler.py:235`).
4. The one shipped expiry-adjacent read behavior: movers CE/PE prev-close baselines skip the
   frozen derived 1day and walk the TRUE prior session's 1-minute
   (`brutex/dashboard/movers_snapshot.py:1732-1737` per sibling section repo-quality) — a
   walk-the-1m-truth precedent E-02 reuses.

**External evidence (attributed to the micro-settle section, all web-verified there).**
European-only — no early exercise/assignment anywhere in the product set 2020→today (§1: the
entire American axis is refuted); auto-exercise of every ITM strike incl. ₹0.05 with NO opt-out
since the 2023-03-30 DNE discontinuation (§2.1); index options CASH-settled T+1, stock derivatives
PHYSICALLY settled for the whole window (§1); final settlement = expiry-day closing construct of
the underlying derived from the last ~30 minutes of constituent trading (§2.5); the worked STT
example: hold-vs-sell differs by tens of rupees per ₹75k intrinsic post-2019 and the asymmetry
FLIPS regime-dependently at 2026-04-01 (§3.3); whether exchange transaction charges apply to
EXERCISED (non-traded) contracts is UNVERIFIED (their register #3 — carried here as EU-16).

**Handling law.**
- **L1 (until the expiry machine lands):** an options engine without the expiry state machine must
  force-close every leg strictly BEFORE that leg's own expiry instant — the per-leg generalization
  of the spot 15:20 forced-exit law (which already lands 10 minutes before the 15:30 expiry
  instant on expiry day). A leg whose forced buy-back is unpriceable (E-02) escalates per that
  law; it never silently "expires" without the machine.
- **L2 (the expiry state machine, when built — its own DEC):** per-leg terminal resolution at the
  leg's expiry instant: OTM ⇒ `EXPIRY_WORTHLESS` (₹20 Groww brokerage still fires on the expiry
  event — `CALCULATOR_SPEC.md:390-397` per the micro-settle section §3.3); ITM long ⇒
  `EXPIRY_EXERCISE` cash flow `+intrinsic × lot` minus exercise-STT (0.125%/0.15% regime keyed by
  the EXPIRY date, an open spec question the DEC must answer — `CALCULATOR_SPEC.md:347-352` per
  the repo-exec section) minus the model-dependent exercise-charge stack (EU-16 — encode BOTH
  branches, select by a cited circular, refuse otherwise per the
  `brutex/costs/constants.py:126-136` no-escape-hatch doctrine); ITM short ⇒ `EXPIRY_ASSIGN` cash
  flow `−intrinsic × lot`, **zero STT** (purchaser-side tax; `CALCULATOR_SPEC.md:363-378` per the
  micro-settle section §3.3).
- **L3 (settle-price law — the honest proxy):** `settle` is NOT observable in the lake. Two
  proxies from stored 1-minute spot bars: (a) the 15:29 close (the lake's own daily close law,
  evidence row 3); (b) a last-30-minute mean-of-closes construct (closer in *shape* to the
  exchange's last-half-hour closing construct, still NOT the constituent-VWAP formula for an
  index). The engine resolves an ITM/OTM verdict against `[proxy − ε, proxy + ε]` and books the
  ADVERSE branch per leg direction (the micro-settle section §6's "adverse plausible settle", made
  concrete): a short strike inside the band is resolved ITM at depth ε. ε is an UNCALIBRATED sweep
  axis carried in `canonical_trade_params` when non-default (the DEC-NIFTY-PRICE-SOURCE-AXIS-001
  default-omitted template, per sibling section gap-fill §4.2) — never a claimed market fact.
- **L4 (the real closer, named):** ingesting the exchange's PUBLISHED daily settlement prices
  (bhavcopy-class data) would make L3 exact for realized history — a NEW data surface + its own
  DEC; availability through the current vendor is UNVERIFIED (Groww's API reference in-repo
  documents no settlement-price endpoint — EU-17).
- **L5 (scope):** stock-option expiry adds physical delivery, client-level netting across legs,
  delivery STT and the E-4…E-1 delivery-margin ramp (per sibling section micro-settle §2.2-2.4) —
  the staged recommendation stands: index-options families FIRST (cash-settled, per-leg cash
  flows sum).

**Cannot-model.** The exact official settle (constituent-VWAP construct) from index 1m bars — ε is
irreducible until L4 lands; exercise-charge composition (EU-16); broker-specific expiry-day
auto-square-off behavior on unpriceable legs; the 2020→Mar-2023 CTM/DNE nuance (modeled as
unconditional auto-exercise with an honest-limits note — micro-settle §2.1, materiality LOW).

**Test types.** Golden: the micro-settle §3.3 worked example (₹75k intrinsic; both regimes; both
exercise-charge branches) as byte-exact cost oracles. Property: L2 cash flows are antisymmetric
long-vs-short per leg at any settle; pin-band: verdict flips exactly at `K ± ε`. Regression: a
structure with one ITM and one OTM leg at expiry books exactly one exercise event + one lapse +
correct per-leg brokerage.

**Reuse.** The Step-0 refusal stub (fail-loud forward-compat); P9 (no-escape-hatch regime
doctrine); the walk-the-1m-truth precedent (`movers_snapshot.py:1732-1737` per sibling section
repo-quality); the default-omitted identity template for ε.

---

### E-08 — DTE-boundary drift — expiry-day-shift regimes vs naive weekday math

**Origin:** EC2-9 (registrar 2).

**Scenario.** A strategy family keys on days-to-expiry buckets ("0DTE", "weekly ≤4 DTE", "monthly
weeks 2-4") or on "expiry weekday" assumptions. The expiry weekday has SHIFTED repeatedly inside
the data window; weekday-math features silently mislabel entire eras.

**Families hit & severity.** A "Thursday = expiry" feature is wrong for BANKNIFTY from 2023-09-06
(Wednesday), meaningless for BANKNIFTY weeklies after Nov-2024 (discontinued), and wrong for ALL
NSE contracts from Sep-2025 (Tuesday). DTE mislabeling corrupts the single most theta-loaded axis
an option-selling sweep has; worse, it corrupts it *silently* (every date still gets SOME bucket).

**Detection / repo evidence (verified by registrar 2).** The repo already contains BOTH the hazard
and the cure, side by side:
1. **The hazard shape (a weekday-regime table, honestly caveated):**
   `brutex/options/expiry_calendar.py` is a hand-maintained per-underlying day-of-week regime
   registry — NIFTY `(2020-01-01, THURSDAY) → (2025-09-02, TUESDAY)` citing "NSE FAOP/68747 + SEBI
   standardization May 2025" (`:95-104`); BANKNIFTY `THURSDAY → WEDNESDAY (2023-09-04, NSE
   FAOP/57540) → NO_WEEKLY (2024-11-14 — "SEBI killed BN-WEEK on EOD 2024-11-13")` (`:106-114`);
   the WITHDRAWN Monday regime (announced for 2025-04-04, withdrawn 2025-03-27, deliberately NOT
   in the table) (`:53-59`); and its own recorded limits — **holiday shifts NOT applied** ("when
   an expiry day is a NSE/BSE holiday, the contract typically moves to the prior trading day;
   that's a Phase 2.5 concern", `:26-29`) and SENSEX-2020 continuity UNVERIFIED
   (`:47-51,116-120`). Every regime row is one NSE circular away from being stale.
2. **The cure (vendor-truth expiry data, three independent seams):**
   - the metadata pull persists the vendor's OWN expiry list — `RegistryStore.list_expiries`
     (`brutex/data/registry_store.py:644-659`), consumed as "the AUTHORITATIVE expiry set per
     underlying" with the recorded failure mode of NOT doing so: "a sparsely-pulled lake …
     mislabels the tiers, because a missing 21-Jul / 28-Jul makes the lake-partition set jump
     straight to 04-Aug for 'Far'" (`brutex/dashboard/movers_options.py:899-918`,
     DEC-MOVERS-CALENDAR-TIERS-001);
   - every contract symbol CARRIES its true expiry date — the right-anchored `DDMmmYY` grammar
     (`brutex/data/fno_symbol.py:14-21`);
   - the futures series resolver is explicitly vendor-dir-primary: "vendor-truth ownership … never
     `strftime` reconstruction … the [computed] calendar is an OBSERVE-ONLY cross-check — a
     computed-vs-parsed drift is WARNed with evidence" (`brutex/data/futures_series.py:8-23`); the
     front-month picker is likewise "never a computed last-Thursday formula"
     (`brutex/futures/contract_resolver.py:1-14` per sibling section repo-exec), over the
     serial-rank law `select_expiry_tier` (live = `expiry ≥ on_date`, sorted, rank-indexed; fewer
     serial months ⇒ honest `None` — `brutex/dashboard/movers_futures.py:291-327`);
   - weekly-vs-monthly is SET-derived, not weekday-derived: monthly = the last-in-calendar-month
     expiry of the underlying's own expiry set (`brutex/dashboard/movers_options.py:956`,
     `:988-997`).

**External evidence (registrar 2's web check).** The Sep-2025 shift is independently confirmed:
from 2025-09-01 NSE derivatives expire Tuesdays (NIFTY weekly Tuesday; monthlies last Tuesday) and
BSE Thursdays, per SEBI's May-2025 standardization —
[Arihant Plus](https://www.arihantplus.com/blogs/market-updates/tuesday-is-the-new-thursday-nse-and-bse-expiry-shift-from-sep-2025/),
[Kotak Neo](https://www.kotakneo.com/news/market-news/sebi-to-end-thursday-expiry/),
[Ventura](https://www.venturasecurities.com/blog/changes-in-expiry-nse-and-bse/),
[ICICIdirect](https://www.icicidirect.com/research/equity/finace/sebi-clears-expiry-day-clash-between-nse-and-bse),
and the NSE circular itself surfaced as
[FAOP68747 (PDF)](https://nsearchives.nseindia.com/content/circulars/FAOP68747.pdf) — the exact
circular id the repo's table row cites (`expiry_calendar.py:103`). The BANKNIFTY weekly
discontinuation is also carried by the micro-margin section §2 (SEBI Oct-2024 measure 1, effective
2024-11-20) — note the benign cross-source date split with the repo's "EOD 2024-11-13" row (last
traded BN weekly expiry vs the measure's stated effective date); EU-20 (E2-U6) pins the exact
reconciliation to the primary circular.

**Handling law.**
- **L1 (the bucket-derivation law):** every DTE/tier feature derives ONLY from per-contract vendor
  expiry data — `expiry(contract)` from the symbol grammar and/or the registry chain — never from
  weekday arithmetic, never from `expiry_calendar.py`'s DOW regimes (which remain a useful
  OBSERVE-ONLY cross-check, exactly the `futures_series.py` posture). Holiday-shifted expiries are
  then correct BY CONSTRUCTION to the extent the vendor master mirrors the exchange's real
  contract calendar (residual EU-21 (E2-U7): exhibit one known shifted expiry against the registry
  before code-lock).
- **L2 (two DTE clocks, both explicit):** `dte_calendar_days = expiry − session_date` and
  `dte_sessions = |trading sessions in (session_date, expiry]|` (calendar-aware via the NSE
  calendar); features declare which clock they use — calendar-day DTE silently stretches across
  holiday clusters, session-DTE is the theta-relevant clock. Bucket EDGES, when swept, enter
  `canonical_trade_params` (Golden Rule #4).
- **L3 (tier vocabulary):** near/next/far = serial rank over the LIVE vendor set
  (`select_expiry_tier` law, reused); weekly/monthly = the `_monthly_expiries` set-derivation
  (reused). No weekday token appears anywhere in the feature layer.
- **L4 (regime-empty honesty):** a family whose defined universe empties across a regime boundary
  (BANKNIFTY 0-4-DTE structures after Nov-2024; any pre-2023 SENSEX weekly family) must report
  per-era trade coverage and read "family universe empty in era E" — never fabricate trades from
  the nearest surviving tier (the movers "dropped, never a wrong-tier row" stance —
  `movers_futures.py:303-305`).
- **L5 (chain-completeness dependency, named):** L1/L3 assume the expiry SET is complete; the
  registry graceful-fallback path (lake-partition derivation) is exactly the mislabel hazard the
  repo documents (evidence 2, first bullet) — so an options ENGINE (unlike the display) must
  REFUSE tier features when the registry is absent/empty rather than fall back, and the absent
  strike/chain-completeness census (per sibling section repo-quality — "DOES NOT EXIST") is
  upstream design work this law depends on (shared with E-01 handling 4).

**Cannot-model.** Nothing here is unmodelable — this case is pure derivation-discipline; the
residuals are data-completeness (L5) and the two UNVERIFIED pins (EU-20/EU-21).

**Test types.** Property: for every lake contract, symbol-parsed expiry == registry expiry when
both exist (drift ⇒ loud WARN — the `futures_series` evidence pattern). Golden: DTE buckets across
the 2023-09-04 / 2024-11-13(±20) / 2025-09-02 boundaries on fixture chains (a weekday-math mutant
mislabels all three; the vendor-derived law labels all three). Negative pin: no
`weekday()`/`THURSDAY`-token appears in the future engine's feature layer (AST drift-pin idiom).

**Reuse.** `list_expiries` registry truth; the `DDMmmYY` symbol grammar; the
vendor-primary/calendar-observe-only posture (`futures_series.py:8-23`); `select_expiry_tier` +
`_monthly_expiries`; P13-adjacent confidence discipline.

---

### E-09 — Muhurat sessions (evening/afternoon special sessions; expiry interaction)

**Origin:** EC-7 (registrar 1).

**Scenario.** The Diwali Muhurat session: a ~1-hour special session on a date that is otherwise an
exchange HOLIDAY. Repo-encoded sessions: 2020-11-14 + 2021-11-04 + 2022-10-24 18:15–19:15;
2023-11-12 + 2024-11-01 18:00–19:00; **2025-10-21 13:45–14:45 (afternoon — a regime change)**;
2026 deliberately ABSENT until the timing circular lands (`brutex/calendars/nse.py:429-436`,
absence rationale `:421-428`). Sub-cases: (a) option bars stamped inside the Muhurat window; (b) a
structure held across the Muhurat boundary; (c) expiry-day collision — Diwali falling on the
weekly-expiry weekday; (d) prev-day baselines/anchors across the Muhurat date; (e) the foreseeable
2026-11-08 calendar-absence drop.

**Families hit & severity.** Positional families (future mode) cross the boundary; 0DTE/weekly
families must resolve the expiry SHIFT correctly or they enter contracts on a date they do not
exist; T-to-expiry math crosses a holiday-with-a-session; intraday families see a 60-minute
session where 375-minute assumptions break. Concretely material: 2021-11-04 was a Thursday (NIFTY
weekly day of that era) and 2025-10-21 a Tuesday (NIFTY weekly day post-2025-09-01) — both Diwali
dates sat ON the weekly-expiry weekday.

**Detection / repo state (all verified by registrar 1).**
- Muhurat dates are BOTH holidays AND sessions: the dates appear in the holiday sets
  (`nse.py:225` 2021-11-04, `:284` 2024-11-01, `:302` 2025-10-21) AND in `MUHURAT_SESSIONS`
  (`:429-436`); `get_session` gives the Muhurat session PRECEDENCE over the holiday flag
  (`:712-713`, branch `:737-744`). Consequence for detectors: `is_trading_day` reads False on a
  Muhurat date while `get_session` returns a 1-hour session — every detector must pick its
  predicate consciously. (E.g. the interior-gap classifier counts sessions via `is_trading_day`
  (`brutex/data/interior_gap_detect.py:49-53`), so Muhurat days never inflate a hole run —
  correct; a chain-presence detector naively using `get_session` would EXPECT bars on a Muhurat
  date.)
- Ingest keeps Muhurat option bars: the calendar-aware session filter keeps evening-Muhurat bars
  and drops regular-hours bars ON a Muhurat date ("the exchange is closed outside the muhurat
  hour") (`brutex/data/session.py:262-269`; F&O wiring
  `brutex/adapters/groww/option_ingest.py:918-940`).
- Session-duration truth: expected minutes must come from the session object (60 vs 375) — the
  today-completeness INDEX law already does this (per sibling section repo-quality); options'
  OI-null law is duration-free (`brutex/data/today_completeness.py:71-72`).
- Expiry collision: the exchanges shift an expiry falling on a trading holiday to the PREVIOUS
  trading day (micro-expiry §1 holiday rule). The 2025 instance is directly evidenced: the NIFTY
  weekly expiry of Diwali week 2025 traded/expired on Monday 2025-10-20 —
  [Angel One, "Nifty Weekly Expiry Today … October 20, 2025"](https://www.angelone.in/news/market-updates/nifty-weekly-expiry-today-sammaan-capital-and-rbl-bank-under-f-and-o-ban-on-october-20-2025)
  — i.e. shifted OFF the holiday Tuesday 2025-10-21 (Muhurat 13:45–14:45,
  [HDFC Sky Diwali-2025 timings](https://hdfcsky.com/news/five-day-festival-calendar-from-18-23-october)).
  "An expiry never settles ON a Muhurat session" is CONSISTENT with all evidence (the Muhurat date
  is a regular-session holiday; the FSP is a regular-session close construct) but is stated here
  as a corroborated inference, general rule UNVERIFIED-primary — EU-10 (U7.1).
- Repo precedents for Muhurat-safety: Stage-1A skips the Muhurat session when rolling the prev-day
  anchor (spot engine — CLAUDE.md DEC-SWEEP-DATA-EDGE-GUARDS-001 blurb); movers stamp a
  day-after-Diwali baseline `prev_close_muhurat` contiguous-True (CLAUDE.md #1649b blurb); the
  acceptance verifier counts Muhurat 1day bars as `muhurat_days_unverified` (CLAUDE.md
  DEC-OPERATOR-ACCEPTANCE-VERIFY-001 blurb).
- Trade-sim: an open position whose next pushed bar is a Muhurat bar sees an IST session-date
  change → the `session_gap` law closes it retroactively at the prior session's last stored bar
  (`brutex/engine/trade_sim.py:60-67`).
- (e) The 2026 drop: 2026-11-08 is in `HOLIDAYS_2026` (`nse.py:368-370`) with NO
  `MUHURAT_SESSIONS` entry (timings not notified — `:421-428`), so `get_session` returns `None`
  for it and the ingest filter will DROP that session's bars as non-trading-date leaks (counted +
  one WARN per window — `session.py:262-264` + `option_ingest.py:928-940`) until the calendar row
  is added. Foreseeable, detectable (the drop counter spikes on exactly that date), and honest —
  but a real data-loss-until-calendar-update edge every downstream design must know.

**Handling law (correct-or-absent).**
1. Expiry resolution MUST be holiday-shift-aware. The vendor registry's ACTUAL expiry dates
   (`get_expiries` → registry rows) are the truth and already encode shifts; the static
   `expiry_calendar` explicitly does NOT apply holiday shifts
   (`brutex/options/expiry_calendar.py:25-29`) and must never be the engine's resolver —
   registry-primary, formula-observe-only (the futures-series law transplanted; = E-08 L1).
2. Recommended v1 law for the engine: NO entries during Muhurat sessions; open structures never
   carry INTO a Muhurat session (the session-gap law already produces this); Muhurat bars are
   consumed only as stored data (baselines per the movers precedent). Rationale: 6 anomalous
   ~60-minute sessions in 6 years, unmodeled liquidity, and a 2025 regime change in session time —
   skip-and-count (`muhurat_entries_skipped`) is honest and cheap. A first-class Muhurat trading
   mode (per-session close-aware forced exit via `forced_exit_seconds_for_session` —
   `trade_sim.py:110-113`) is a conscious later widening.
3. Expected-minutes/duration laws must derive from the session object, never a 375 constant.
4. T-to-expiry across a Muhurat date: no special-case — T is wall-clock to the expiry instant; the
   only trap is assuming the expiry could BE the Muhurat date (see 1).
5. When the 2026 timing circular lands, the calendar row addition is the ONLY fix needed
   (`MUHURAT_SESSIONS` entry); designs must not hardcode Muhurat hours anywhere else
   (single-source law).

**Test types.**
- unit: `get_session` precedence on all 6 encoded dates; expiry resolver on the 2025-10-21 →
  2025-10-20 shift; the is_trading_day-vs-get_session predicate split in each new detector.
- golden: a structure open into a Muhurat boundary → session_gap-law close bytes pinned; Muhurat
  entry attempt → skip + count pinned.
- property: no entry minute falls outside its date's session window; expected-minutes always ==
  session duration (60 on Muhurat, 375 regular, session-object-derived).
- integration: planted Muhurat-window option bars survive ingest (filter keep) and are visible to
  reads; the engine skips them per the v1 law.
- chaos: the 2026 shape — Muhurat bars for a holiday date with no session entry → dropped +
  counted + WARN, no crash, drop counter spikes exactly there.

**Reuse.** `MUHURAT_SESSIONS`/`get_session` single source (`nse.py:429-436,737-744`); P11 (session
filter keep/drop + counts); the Stage-1A Muhurat-anchor-skip and movers `prev_close_muhurat`
EMIT-ON-TRUE stamp idioms (CLAUDE.md blurbs); P4 (session_gap); P6 (capped WARN);
`forced_exit_seconds_for_session` seam for any future first-class mode.

---

### E-10 — Gap-open through a short strike

**Origin:** EC2-1 (registrar 2).

**Scenario.** A position (future overnight-capable options engine) holds a short strike `K` into
the close; the underlying opens the next session gapped past `K` (short 24,200 CE, underlying
closes 24,000, opens 24,600). The short leg opens deep-ITM; its premium gaps multi-fold in the
09:15 bar. The intra-session micro-version: two consecutive bars where bar N+1's range opens
entirely beyond a leg's SL level.

**Families hit & severity.** The defining risk asymmetry of option WRITING:

| Structure | Gap-through loss | Sim consequence |
|---|---|---|
| Defined-risk credit spread (short K₁, long K₂) | structurally capped at `(width − credit) × lot` — both legs gap together | the cap is an assertable INVARIANT per structure |
| Naked short call | unbounded above | loss = whatever the gap bar prints, honestly |
| Naked short put | bounded by `K × lot − premium` | large but bounded |
| Long options / debit structures | bounded at premium (a gap can only help or expire them) | no special handling |

Magnitude grounding from repo-recorded physics: "The largest single-DAY NIFTY moves on record
(COVID, March 2020) were on the order of −13% / +9% for the whole day"
(`brutex/data/bad_tick.py:24-26`); the Track-2 draft calibrates its halt convention to the
2024-06-04 election-day crash (`docs/track2/TRACK2_RISK_MGMT.md:458-472` per sibling section
micro-margin §5.4). An index gap of even 2-3% through a weekly short strike is a many-multiple
premium move on the short leg.

**Detection / repo evidence (verified by registrar 2).**
1. **The engine structurally cannot hold overnight today** — this edge case is not "unhandled", it
   is *excluded by law*: "an open position never survives an IST session-date change
   (DEC-TRADESIM-OPEN-SESSION-GUARD-001) … the first bar of the NEXT session closes the position
   retroactively at the PRIOR session's last stored bar's adverse extreme … under the
   `session_gap` reason; … the boundary bar itself never arms a new entry"
   (`brutex/engine/trade_sim.py:60-67`; enum member + append-only vocabulary `:156-171`; the Rust
   kernel carries the same guard — `rust/fno-math/src/trade_sim.rs:48`).
2. **The per-bar fill law ALREADY prices gap-through correctly** — no new fill law is needed for
   the gap bar itself: a triggered SELL exit (a short leg's cover) fills at `max(level, bar
   high)`; "a gap-through bar fills at the extreme, while a bar entirely past the level in the
   trade's favour stays clamped AT the level" (`brutex/engine/trade_sim.py:37-42`, implementation
   `:733-747`; `brutex/execution/fill_mode.py:10-13`). Per-leg option arithmetic adds ± one tick
   on the extreme (per sibling section gap-fill §1.2, `brutex/costs/calculator.py:123-181` per
   that survey).
3. The underlying gap is *measurable* from the lake: index 1-minute series are dense by class law
   ("indices always print" — `brutex/data/today_completeness.py:73-74`), so
   `open(09:15, S+1) − close(15:29, S)` is a first-class per-session quantity.

**External evidence.** Overnight gap risk on short index options is the canonical seller
tail-risk; magnitudes beyond the repo-recorded COVID/election episodes are deliberately not
quantified here (EU-15, E2-U1).

**Handling law.**
- **L1 (structural opt-in):** overnight holding is a per-structure, engine-level MODE that a
  future options engine must consciously enable — a DEC that *supersedes-in-part*
  DEC-TRADESIM-OPEN-SESSION-GUARD-001 for the options position model only. Default = intraday
  (inherits the 15:20 forced-exit law, `brutex/calendars/nse.py:125` `FORCED_EXIT_IST`; zero
  overnight gap exposure — the conservative default and the only mode the shipped executors model
  today, DEC-RISK-011 per sibling section repo-exec).
- **L2 (gap-bar pricing = the existing law, per leg):** on the first bar of a session with a
  surviving structure, each leg evaluates its SL/TP against its OWN contract's 09:15 1-minute bar
  under the unchanged with-the-level clamp (evidence row 2). A gapped-through short leg therefore
  books the gap bar's worst print + tick — never the level. No invention; the property is
  inherited.
- **L3 (visibility):** flag + count `gap_open_through_level` whenever `session_open(underlying)`
  crosses a leg's armed level relative to the prior session's close; annotation-only (the R13
  annotation-never-reranks precedent, per sibling section repo-exec).
- **L4 (the censoring interplay — the dangerous corner):** on gap mornings the deep-ITM leg's
  first minutes are disproportionately absent/volume-0 (illiquidity correlates with the adverse
  event — gap-fill §2(ii)); such exits route to the E-02 deferred-exit law with the gap flag
  preserved; a structure unpriceable by session end that day is CENSORED and counted
  (`censored_on_gap`) — and the result surface must print that count beside net P&L, because the
  censored set is biased toward exactly the blow-ups a writer's verdict depends on.
- **L5 (defined-risk cap invariant):** for defined-risk structures the engine asserts
  `structure_loss ≤ (width − credit) × lot + costs` under EVERY input, gap bars included — a cheap
  structural tripwire that catches leg-bookkeeping bugs.

**Cannot-model.** Opening-auction dynamics and pre-09:15 quote formation (no pre-open option data
exists in the lake; the session filter's kept set starts at the session open —
`brutex/data/session.py:251-277` per sibling section repo-quality); broker RMS force-closures at
the open on margin blowout (E-16); the fill quality of a REAL cover order in the first gap minute
beyond the worst-traded-price ± tick bound (gap-fill §1.3/§2).

**Test types.** Property: L5 cap invariant under hypothesis-generated gaps. Golden: short CE
covered on a gap bar fills at `high + tick`, not at the SL level. Regression fixture shaped on
2024-06-04 (underlying −6%-class open) with an absent-first-minutes short-leg series → asserts
deferral + `censored_on_gap` accounting, never a silent drop.

**Reuse.** P4 (session guard — the law L1 consciously supersedes-in-part); the with-the-level
clamp law (`trade_sim.py:37-42`, `fill_mode.py:10-13`); the E-02 deferral; the R13
annotation-never-reranks precedent.

---

### E-11 — Both-sides-touched bars, per structure — the deterministic worst-case order

**Origin:** EC2-8 (registrar 2).

**Scenario.** One 1-minute bar's range spans multiple armed triggers: a single leg's SL AND TP;
two different legs' levels; a structure-level stop AND a leg-level target — in any combination.

**Families hit & severity.** At 1-minute granularity intra-bar sequence is unobservable, so the
processing ORDER is a policy choice that directly moves P&L; an optimistic order (TP-first) is a
classic backtest inflator. For multi-leg structures the order also decides WHAT REMAINS OPEN — a
wrong composition can leave a phantom naked leg.

**Detection / repo evidence (verified by registrar 2).** The single-leg law is fully shipped and
line-verified in BOTH implementations (§0 closure note): "Same-bar SL+TP — when one bar's range
spans BOTH levels, the STOP fills first, always (the pessimistic tie …)"
(`brutex/engine/trade_sim.py:43-45`); resolver `_resolve_level_hit` returns stop-over-target with
the with-level clamp applied at the fill site (`:733-747`, `:757-781`); Rust twin
(`rust/fno-math/src/trade_sim.rs:329`, `:356`; tie tests `:1232`, `:1484`). Exit-reason vocabulary
is append-only and wire-visible (`trade_sim.py:164-171`) — new structure-level reasons must
APPEND, never re-order. Nothing today defines cross-leg semantics (per sibling section repo-exec:
"structure-level fill semantics NEW"; the executors inherit spot-fixed timestamps and never
re-evaluate levels — `brutex/options/executor.py:1-28` per that survey).

**Handling law — the pessimistic precedence ladder (PR-0…PR-5; named P0–P5 in the source entry).**
- **PR-0 (evaluation set):** at each bar, collect ALL triggered events across the structure:
  structure-level stop (net-premium/net-P&L stop), structure-level target, per-leg SLs, per-leg
  TPs, forced/session events.
- **PR-1 (precedence, worst-first):** `forced/session events` (they override everything — the spot
  law, `trade_sim.py:46-48`) → `structure-SL` → `leg-SL` (all triggered legs) → `structure-TP` →
  `leg-TP`. Rationale: extends "the STOP fills first, always" to every level of the hierarchy —
  loss-realizing events always precede profit-taking events within one bar.
- **PR-2 (atomic closure):** any event that closes the STRUCTURE closes ALL legs on the SAME bar,
  each at its OWN contract bar's adverse extreme ± tick (with-level clamp only for the leg whose
  level triggered). No orphan legs, ever — composing with the Class-C atomicity stance
  (fill-realism §3.2) and E-02 D5. A leg whose bar is untradeable at that minute defers per E-02
  while the structure is BOOKED as closing (reason recorded from PR-1), the deferred leg's fill
  landing per D1.
- **PR-3 (no sequencing fiction):** fills are bar-anchored, not intra-bar-sequenced — processing
  "order" decides the recorded REASON and what stays open, never a chain of intra-minute re-fills
  at different prices (which 1-minute data cannot support). Legging cost inside the minute is
  disclosed-unmodelable (each real leg walks its own book at different seconds); entries and exits
  of a structure are ATOMIC same-minute per-leg worst-case fills.
- **PR-4 (count):** `n_multi_trigger_bars` per config; annotation-only.
- **PR-5 (worst-order property):** for the P&L-relevant cases the booked outcome must be ≤ the
  outcome under ANY other processing order of the same triggered set (provable on fixtures for the
  common two-event cases; asserted as a property test over generated structures where tractable —
  EU-23 (E2-U9) records the open question of a general proof).

**Cannot-model.** True intra-minute event order; partial fills / leg-by-leg slippage inside the
minute; quote-based tie resolution.

**Test types.** Fixture matrix: {leg SL+TP same bar} × {short/long leg} × {structure stop vs leg
target same bar} — each pinned to the PR-1 outcome, mirrored against the single-leg goldens (which
must remain byte-identical: the ladder degenerates to the shipped SL-first law for one leg — the
compatibility pin). Mutant hook: swapping any two PR-1 ranks must fail at least one fixture.

**Reuse.** The shipped SL-first tie law (both implementations, line-verified); the append-only
exit vocabulary (`trade_sim.py:164-171`); the forced-exit override semantics
(`trade_sim.py:46-48`); Class-C atomicity (§5 Law B); the E-02 deferral.

---

### E-12 — Circuit-locked underlying (MWCB halt; F&O-stock dynamic-band cooling-off)

**Origin:** EC2-5 (registrar 2).

**Scenario.** (a) An index-wide market circuit breaker (MWCB ±10/15/20%) halts ALL equity and
derivatives trading mid-session; (b) a single F&O stock pins against its DYNAMIC price band edge
during a cooling-off window while its option chain's quotes gap between flex events.

**Families hit & severity.** During a halt a short option CANNOT be bought back at any price — the
position reprices discontinuously at the post-auction reopen; for band-edge episodes the
underlying flatlines while premia can continue to move (vol repricing on an information event). A
sim that fills through a halt fabricates executions; one that ignores band episodes mis-reads
frozen underlying bars as a tradable, informative price.

**Detection / repo evidence (verified by registrar 2 unless attributed).**
1. **Nothing executable exists:** every `circuit` hit in `brutex/` is the internal ingest circuit
   breaker family; the MWCB matrix + the halt synthetic-exit convention exist only as Track-2
   DRAFT prose (per sibling section micro-margin §7 table row "Exchange circuits / price bands —
   Nothing executable"; `docs/track2/TRACK2_RISK_MGMT.md:442-479` per that survey — incl.
   DEC-RISK-015: synthetic exit at the halt bar's last traded price + 50 bps penalty, calibrated
   to 2024-06-04).
2. The bad-tick screen is DERIVED FROM circuit-breaker physics but does not DETECT locks — it
   rejects ≥4× minute moves (`brutex/data/bad_tick.py:20-36`, `BAD_TICK_MAX_RATIO = 4.0` `:76`)
   and is explicitly INVALID for premia: "option PREMIA legitimately move ≥4×/minute on leverage …
   the index-circuit-breaker-calibrated 4× threshold is INVALID for premia and would drop legit
   option bars … Options (`bad_tick=False`) force the arm OFF regardless of the switch"
   (`brutex/dashboard/movers_futures.py:902-920`).
3. Halt-shaped evidence in the lake is a bar-ABSENCE run: the interior-gap machinery flags long
   bar-less runs of trading sessions for spot indexes (per sibling section repo-quality —
   index-gated demotion), so an intra-session halt appears simply as missing minutes on
   otherwise-dense index series ("indices always print",
   `brutex/data/today_completeness.py:73-74`).

**External evidence.** MWCB thresholds/halt matrix + the fact that a trigger halts ALL equity AND
equity-derivatives trading nationwide, and the F&O-stock dynamic-band mechanism (10% operating
band flexed 5% at a time after 15-minute cooling-off; option-premium operating ranges;
sliding-band enhancement) — per sibling section micro-margin §5.1-§5.4 (search-captured NSE
pages, their U9/U10). Registrar 2's independent check confirms the mechanism: F&O stocks "do not
have a fixed daily price band … operate under dynamic price bands"
([Navia](https://navia.co.in/blog/stock-circuits-explained-simply/)), at an upper circuit "only
buyers are present … a fresh buy order will simply sit pending"
([Navia](https://navia.co.in/blog/stock-circuits-explained-simply/)), and F&O contracts
additionally carry execution-range + manually-relaxed circuit-limit machinery
([RMoney](https://rmoneyindia.com/support/execution-range-vs-circuit-limits-fo-trading/),
[Zerodha support](https://support.zerodha.com/category/trading-and-markets/trading-faqs/f-otrading/articles/difference-between-execution-range-and-circuit-limit-fo)).
**Correction to the tasking brief's premise (preserved from the source entry):** the classic
"stock hits ITS band and locks for the day while options keep printing" shape applies to
fixed-band (non-F&O) stocks — which have NO options; for F&O stocks the real phenomena are the
temporary band-edge cooling-off lock and the coordinated MWCB halt. Whether and how one-sidedly
option quotes keep printing during a stock's cooling-off window is market behavior this lake
cannot attest — EU-18 (E2-U4).

**Handling law.**
- **L1 (event table, not inference, for MWCB):** market-wide halts are RARE and DATABLE — ship
  them as a tiny curated dated table (the `MUHURAT_SESSIONS` idiom in `brutex/calendars/nse.py` —
  ledger precedent) rather than inferring them per run; the known in-window candidates (March-2020
  MWCB days) are EU-19 (E2-U5) until the primary circulars are read. Trades whose life intersects
  a halt window take the DEC-RISK-015-class synthetic-exit convention as a DISCLOSED assumption +
  a per-trade `halt_window` flag — never a claimed fill.
- **L2 (heuristic detection as a cross-check only):** a mid-session bar-absence run ≥ K minutes
  SIMULTANEOUS across all three dense engine indexes ⇒ `market_halt_suspected` session anomaly
  (observe-only WARN + flag; never a demotion — the calibrated-to-never-false-red completeness
  stance, per sibling section repo-quality).
- **L3 (band-edge episodes, stock underliers):** detection heuristic = a run of zero-range
  underlying bars (`high == low`) at a run-constant price with concurrent option prints; the band
  EDGE itself is not stored (dynamic, daily-computed) so detection is disclosed-approximate. Flag
  `band_lock_suspected` per structure; strategy conditions computed off the frozen underlying
  freeze WITH it (automatic and honest); the sim must NOT treat the frozen underlying as
  hedge-executable (irrelevant for pure option structures; blocks any delta-hedged family until
  quote data exists).
- **L4 (scope):** index-first scoping makes L3 moot for v1 (indices have no per-stock bands;
  NIFTY/BANKNIFTY/SENSEX F&O are exempt from the MWPL ban mechanism — per sibling section
  micro-margin §3.3); L1/L2 still apply to indices via MWCB.

**Cannot-model.** Reopen-auction price formation after a halt; quote behavior during cooling-off
(EU-18); the exact per-day dynamic band edges (not stored anywhere); LPP order-rejection effects
on fills.

**Test types.** Unit: L2 detector on a synthetic 45-minute simultaneous hole (flag fires;
single-index hole does NOT fire). Golden: a trade spanning a table-listed halt window books the
synthetic-exit convention + flag, and its P&L carries the disclosed-assumption annotation.
Negative pin: L3 never fires on a legitimate low-volatility flat minute run shorter than the
threshold.

**Reuse.** The `MUHURAT_SESSIONS` curated-dated-table idiom; the DEC-RISK-015 draft convention (as
a DISCLOSED assumption, per sibling section micro-margin); P6 (observe-only WARN); the
index-density class law (`today_completeness.py:73-74`).

---

### E-13 — Strike-grid regime change mid-backtest; new strikes appearing intraday

**Origin:** EC-4 (registrar 1).

**Scenario.** (i) The exchange revises the strike SCHEME mid-window — BANKNIFTY scheme revision
effective 2025-01-20; NIFTY monthly/quarterly scheme revision effective 2025-11-17 (weeklies kept
on the old scheme); long-dated NIFTY interval 1000 → 1500 points in Dec-2025 (per sibling section
micro-expiry §4.1, with
[NSE circular mirror](https://www.steelcitynettrade.com/Circulars/Revision%20in%20Scheme%20of%20Strikes%20in%20BANKNIFTY%20Options.pdf),
[TeamLease RegTech summary](https://teamleaseregtech.com/updates/article/49219/nse-notified-regarding-the-changes-in-the-strike-scheme-and-strike-int/),
[Zerodha bulletin 437296](https://zerodha.com/marketintel/bulletin/437296/revision-of-strike-interval-in-long-dated-nifty-options);
exact band parameters UNVERIFIED — micro-expiry §8.8 / EU-04). (ii) Fresh strikes are ADDED
intraday as spot moves (standard replenishment; exact trigger rules UNVERIFIED — EU-04). (iii) The
grid differs near-ATM vs far and by tenor.

**Families hit & severity.** ATM±N offset selection (the 7 live offset rules — per sibling section
repo-exec, `brutex/options/strike_rules.py:104-115`) changes MEANING when the step changes: a
±2-strike wing is ±100 rupees one day and ±50 the next. Symmetric-width families (flies/condors)
sweep width axes that silently re-denominate across the regime break. Intraday strike addition
creates a LOOK-AHEAD hazard: the lake's flat contract-dir namespace says nothing about WHEN a
strike started existing — selecting at 10:00 a strike first listed at 13:00 uses information from
the future. Delta-target rules are step-immune but chain-completeness-dependent.

**Detection (data signature; existing detectors).**
- Step change across sessions: `derive_strike_step` per (underlying, expiry) — the smallest
  positive gap of the distinct ladder (`brutex/data/moneyness.py:106-141`) — compared
  session-over-session; `resolve_strike_step` labels provenance (REGISTRY beats the sparse
  OBSERVED lake ladder; `moneyness.py:120-129,144-159`). A step change on the SAME expiry across
  two sessions where both ladders are REGISTRY-confidence = a scheme event (or a corporate
  action — see E-14; the two are separated by the underlying type + spot-jump signature).
- The repo's own falsified constant is the anti-pattern: `brutex/options/strike_grid.py:12-15`
  asserts "Strike steps are CONSTANTS … no SEBI changes to step sizes in 2021-2026" — falsified by
  the 2025 scheme circulars above (micro-expiry §4.2). Any options engine must treat
  `strike_grid.py:26-30`'s NIFTY 50 / BANKNIFTY 100 / SENSEX 100 as a demoted fallback at best.
- Intraday appearance: contract first-bar timestamp strictly inside a session while sibling
  strikes print from the open. Per-file timestamp bounds already exist in the write-time stats
  manifest (per sibling section repo-lake, `brutex/data/parquet_writer.py:1332` hook +
  `.manifest/stats/` shards) — the cheap source for "first stored minute" without opening parquet.
  The registry has NO listing timestamp and is a rebuildable cache, not a point-in-time archive
  (per sibling section repo-lake G4, `brutex/data/registry_store.py:135-140`) — so listing-time
  truth is APPROXIMATED by first-stored-bar, honestly disclosed.
- Ladder-as-of-instant: only answerable as "contracts with ≥1 stored bar at ≤ t" — an
  approximation the design must name.

**Handling law (correct-or-absent).**
1. The ladder/step is vendor DATA per (underlying, expiry, as-of) — never a formula (the standing
   repo law: `moneyness.py:113` "never a hardcoded NSE rule"; the futures-series
   vendor-primary/calendar-observe-only precedent per sibling section micro-expiry §7.1).
   Selection at instant t runs over the ladder OBSERVED as of t: a strike with no stored bar at
   ≤ t is NOT selectable at t (kills the listed-later look-ahead) — count
   `strikes_unlisted_at_entry`.
2. OBSERVED-confidence steps (sparse chain, no registry) may OVERESTIMATE the true step by an
   integer factor (`moneyness.py:120-129`): a strict engine mode REFUSES offset-rule selection on
   OBSERVED confidence (counted), a research mode counts + labels it. Never silently trust the
   sparse ladder.
3. Width semantics must be pinned in run identity: offset-in-STRIKES vs width-in-RUPEES is a
   conscious axis choice; whichever is chosen, the OTHER is derived + displayed. A regime break
   mid-window is then an honest re-denomination visible in results, not a silent one.
4. Retire/demote `strike_grid.py` constants in the options-engine design (doc-level
   recommendation only — no code change here; micro-expiry §4.2 makes the same call).
5. New-strike intraday appearance is NOT a defect — no WARN storm; it only gates
   selectability-as-of.

**Test types.**
- unit: step derivation across a planted step-halving; ladder-as-of excludes later-listed strikes;
  REGISTRY-vs-OBSERVED resolution matrix.
- property: `atm_offset` half-toward-zero invariants hold across a regime boundary
  (`moneyness.py:238-261` per sibling section repo-lake); selection never returns a strike absent
  from the as-of ladder; offset→strike→offset round-trips within the as-of ladder.
- golden: a BANKNIFTY-shaped fixture with a step change between two sessions → widths + selections
  pinned on both sides; an intraday-added strike selected only after its first stored minute.
- integration: registry + lake unioned ladder (REGISTRY confidence) vs registry-absent (OBSERVED
  counted) on a real RegistryStore.
- chaos: registry.duckdb held/corrupt → OBSERVED fallback + counted low-confidence, never a crash
  (the movers graceful-fallback precedent, per sibling section repo-lake).

**Reuse.** P13 (`derive_strike_step`/`resolve_strike_step` + StepConfidence); registry
`list_contracts` (per sibling section repo-lake); stats-manifest per-file bounds for first-bar
detection (per sibling section repo-lake); the repo's drift-pin test idiom to pin the strike_grid
demotion (mirror-pin pattern per CLAUDE.md §11 conventions).

---

### E-14 — Corporate action on a stock option (split / bonus / special dividend → adjusted strike & lot)

**Origin:** EC-5 (registrar 1).

**Scenario.** A stock underlying undergoes a bonus, split, rights, or EXTRAORDINARY dividend
during the backtest window. The exchange adjusts all existing F&O contracts on the last cum-basis
day after close: new strike = old strike ÷ adjustment factor; new lot = old lot × factor; bonus
A:B factor = (A+B)/B; split A:B factor = A/B; strikes rounded to tick, lots to nearest integer;
dividends above 2% of the underlying's market value are "extraordinary" and trigger adjustment
(ordinary dividends do not). Sources:
[NSE Clearing — Corporate Actions Adjustment](https://www.nseclearing.in/clearing-settlement/equity-derivatives/corporate-actions-adjustment),
[NSE static page](https://www.nseindia.com/static/products-services/equity-derivatives-corporate-actions-adjustments),
[Zerodha support explainer](https://support.zerodha.com/category/trading-and-markets/trading-faqs/f-otrading/articles/adjustment-f-o-contracts),
and live per-event circulars/bulletins (e.g.
[NSE FAOP59389 PDF](https://nsearchives.nseindia.com/content/circulars/FAOP59389.pdf),
[Zerodha ITC dividend adjustment](https://zerodha.com/marketintel/bulletin/441076/adjustment-of-fo-contracts-of-itc-due-to-dividend),
[Zerodha NMDC](https://zerodha.com/marketintel/bulletin/441798/adjustment-of-fo-contracts-of-nmdc-due-to-dividend-3)).
Threshold history (10%→5%→2% evolution) UNVERIFIED-exact — EU-06 (U5.2).

**Families hit & severity.** ALL stock-option families (covered calls, cash-secured puts, stock
straddles/strangles/spreads): the premium series shows a mechanical jump at the adjustment; strike
identity changes (often to fractional/off-grid values — the `parse_fno_symbol` grammar already
admits finite positive Decimal strikes, per sibling section repo-lake,
`brutex/data/fno_symbol.py:160-175`); the lot changes MID-EXPIRY (breaking the "one lot per
(underlying, expiry)" snapshot invariant); P&L continuity across the event is fictitious unless
the adjustment factor is modeled; moneyness/ATM math against the unadjusted spot is wrong on one
side of the event. Index options are immune — index derivatives have no corporate actions — which
is a real argument for index-first engine scoping (the micro-settle section §7.2 makes the same
fork argument for settlement).

**Detection (proxy signatures — NO corporate-action feed exists in the repo; grep-verified absent
per sibling section repo-quality).**
1. Same-expiry lot discontinuity ACROSS pull snapshots: the shipped audit checks lot uniformity
   per (underlying, expiry) WITHIN one master snapshot — GATING on a within-expiry mismatch,
   informational on cross-expiry revisions (`brutex/data/economics_integrity.py:22-29,31`). A
   corporate action's signature is the SAME expiry's lot changing BETWEEN snapshots — not
   currently detected anywhere (the registry stores no lot at all — per sibling section repo-lake
   G4), so the detector is NEW: persist per-(underlying, expiry) lot observations per pull and
   diff.
2. Ladder discontinuity on the SAME expiry: overnight wholesale strike-set replacement — old-strike
   dirs stop printing, new dirs appear with off-grid/fractional strikes (÷1.5-type factors), step
   changes, ladder set-similarity (Jaccard) collapses session-over-session. Distinguished from
   E-13 scheme changes by (3) and (4).
3. Underlying spot jump: the CASH series gaps by ≈ the adjustment factor at the ex-date open
   (split/bonus), synchronized with (2).
4. OI teleport: new adjusted dirs carry large OI from their first stored bar (positions were
   migrated by the clearing corp, not traded into existence).
5. Vendor representation in Groww data: **UNVERIFIED** — whether adjusted contracts appear as NEW
   `groww_symbol`s (new partition dirs), whether the instrument master's strike/lot rows are
   rewritten in place, and whether historical candles are restated. Until probed, detection must
   assume EITHER representation (EU-05, U5.1).

**Handling law (correct-or-absent — the operator-briefed default).**
1. Default (no CA calendar): on ANY same-expiry strike/lot discontinuity signature vs the
   registry/master for a STOCK underlying, **REFUSE the underlying's window around the event** —
   entries rejected typed + counted (`CORPORATE_ACTION_SUSPECTED`, P1 pattern); an in-flight
   structure spanning the event is refused/annotated, never stitched across the jump; the premium
   jump is never booked as P&L. Refusal is remediable ("source the event's adjustment circular"),
   exactly the refusal-row doctrine (P9: a fabricated adjustment factor would fork every
   downstream number — same argument as the fabricated statutory rate,
   `brutex/costs/constants.py:126-136` per sibling section repo-exec).
2. Index-only first scope sidesteps the axis entirely (no CA on NIFTY/BANKNIFTY/SENSEX options).
3. The eventual stock-option track requires a sourced CA calendar as DATA — dated rows
   (underlying, ex-date, action type, factor, circular id) with refusal outside coverage, the
   `lot_size_history` shape (per-date table + `ValueError`/reject outside history —
   `brutex/options/lot_size_history.py:58-77,113-118` per sibling section repo-exec). With the
   calendar: trades may span the event ONLY under an explicitly modeled adjustment (positions
   re-denominated by the factor, P&L continuity via adjusted basis) — its own DEC.
4. Detection remains armed even WITH a calendar (belt): a discontinuity NOT matching a calendar
   row still refuses (either the calendar is incomplete or the lake is poisoned — both
   refusal-worthy).

**Test types.**
- unit: discontinuity classifier on planted split/bonus fixtures (factor 2, 1.5, (A+B)/B shapes);
  fractional-strike parse round-trip; factor arithmetic + tick/integer rounding.
- golden: a synthetic 1:1.5 adjustment day — refusal bytes + counts pinned; with a calendar row,
  the adjusted-continuation P&L pinned (future DEC's golden).
- property: no accepted trade spans a detected event without a calendar row; refusal window
  conservation (every signal in the window lands in exactly one counter).
- integration: two master snapshots with a same-expiry lot change → detector fires; registry
  rebuilt across the event → still fires (snapshot-diff persistence proven).
- chaos: PARTIAL adjustment representation (some strikes' dirs moved, some not — a torn vendor
  state) → still refuses the underlying; corrupt calendar file → structural ABSTAIN, never a
  silent pass.

**Reuse.** P9 (refusal-row regime pattern + typed error carrying window/remediation); P1
(`LOT_SIZE_UNKNOWN`-style counted reject); the `economics_integrity` grouping walk +
GATING-vs-informational split (`economics_integrity.py:22-33`); P5 (never value-edit stored
bars — the adjustment is modeled at read/sim time, the lake is never rewritten); the
`lot_size_history` dated-table shape for the CA calendar.

---

### E-15 — F&O ban period: entry during ban + ban beginning mid-trade (stock F&O)

**Origin:** EC-6 (registrar 1).

**Scenario.** A stock's market-wide open interest crosses 95% of its Market-Wide Position Limit
(MWPL) → the security enters the F&O ban list published daily by the exchange: NO fresh positions
may be opened; only position-REDUCING trades are allowed; the ban lifts when OI falls below 80% of
MWPL; increasing positions during ban attracts penalties. Sources (mechanics multi-outlet; primary
page bodies unfetched in-session — EU-07):
[NSE — security ban (MWPL) page](https://www.nseindia.com/static/products-services/equity-derivatives-risk-management-sec-ban),
[NSE Clearing — position limits](https://www.nseclearing.in/risk-management/equity-derivatives/position-limits),
[5paisa NSE ban list](https://www.5paisa.com/nse-ban-list),
[Kotak Neo explainer](https://www.kotakneo.com/futures-and-options/nse-fno-ban-list/),
[mastertrust — MWPL rules during ban](https://www.mastertrust.co.in/blog/mwpl-open-position-limit-ban-period).
Two sub-cases: (A) the backtest ENTERS a fresh stock-option structure on a ban day — an impossible
trade; (B) the ban BEGINS while a structure is open — closing legs remain legal, but
adding/rolling/adjusting legs (fresh positions) is prohibited mid-trade.

**Families hit & severity.** Every stock-option family; most acutely adjustment-heavy families
(defended strangles/condors that ADD legs mid-trade — illegal during ban) and roll-forward
families. Ban stocks are precisely the high-OI, high-attention names where premium selling
concentrates, so ignoring bans biases results toward infeasible trades in the most-traded names.
Index derivatives are OUT of the MWPL/ban framework (position limits for indices work
differently) — corroborated by the ban framework being defined over SECURITIES; tagged
UNVERIFIED-primary (EU-09, U6.3) since the NSE page body was not fetched.

**Detection (honest position: NOT derivable from the lake).**
- The exact ban state is EXTERNAL DATA. MWPL is computed by the exchange from free-float-based
  limits vs MARKET-WIDE OI; BruteX's lake has per-contract OI only for PULLED contracts inside the
  ~`expiry−30d` window (per sibling section repo-lake), and no MWPL denominator — so the 95%
  trigger CANNOT be reconstructed reliably from stored data. Any OI-plateau heuristic ("chain OI
  flat + fresh-OI drought") is unverifiable inference and MUST NOT be used as a feasibility gate
  (it may serve as a research-only flag at most, clearly labeled).
- Therefore the detector is a BAN CALENDAR: dated per-underlying intervals ingested from the
  exchange's daily ban list (NSE publishes it daily; historical availability for 2020→ is
  UNVERIFIED — EU-08, U6.2). The repo has NOTHING today (grep-verified per sibling section
  repo-quality: no ban/corporate-action module).
- Signature once the calendar exists: `entry_ts.date() ∈ ban_interval(underlying)` for (A);
  `ban_start ∈ (structure.entry_date, structure.exit_date]` for (B); per-adjustment-leg
  `leg.action == OPEN and leg.ts.date() ∈ ban_interval` for the mid-trade add.

**Handling law (correct-or-absent).**
1. Until a sourced ban calendar lands (its own DEC + data surface): stock-option strategy results
   are UNCLAIMABLE as feasible. Two sanctioned postures, to be fixed in the DEC: (a) STRICT —
   refuse to run stock-option families at all (the operator-briefed option: "refusal to claim
   stock-option results until a ban calendar lands"); (b) RESEARCH — run but attach a permanent
   named structural ABSTAIN ("ban-period feasibility unmodeled") to every stock-option verdict,
   the acceptance-verifier precedent (P14: named, budget/streak-exempt, never a fabricated green).
   Recommended split: STRICT for any engine-grade/leaderboard claim; ABSTAIN-disclosure for
   exploratory research surfaces. Index-only scope needs neither.
2. With a calendar: (A) fresh entry on a ban day → typed counted reject (`BAN_PERIOD`, P1); (B)
   mid-trade: position-REDUCING exits are allowed (the exchange law), fresh/adjust legs are
   refused + counted (`BAN_ADJUST_BLOCKED`) — the structure may be forced into exit-only
   management mode, and that mode switch is itself counted so results disclose how often the
   strategy was ban-constrained. Penalties for increasing positions are never simulated — the
   trade simply cannot occur.
3. Calendar hygiene mirrors the holiday list: every row carries its citable source (the
   per-row-citation law of the holiday calendar — `brutex/calendars/nse.py:393-398`), coverage is
   dated, out-of-coverage dates ABSTAIN loudly (never a silent "no ban").
4. Never: fabricate ban state from lake OI; silently drop ban-day trades without counting; apply
   bans to index options.

**Test types.**
- unit: interval lookup; the entry/adjust/reduce permission matrix (action × ban-state); coverage
  boundary → ABSTAIN.
- property: no accepted fresh-entry intersects a ban interval; reduce-only invariant — during a
  ban, a structure's net position magnitude per leg is non-increasing; count conservation.
- golden: planted ban window (enter-before/ban-mid-trade fixture) → reject + mode-switch counts
  pinned.
- integration: calendar file ingest + wiring through the structure sim; a two-underlying run where
  only the banned name is constrained.
- chaos: calendar file missing/corrupt/non-regular inode → loud structural ABSTAIN (loader
  discipline per the repo's sidecar-read conventions), never a silent "no bans"; a ban interval
  with open-ended end (still banned at data edge) handled.

**Reuse.** Holiday-calendar ingestion + per-row-citation law (`nse.py:393-398`); P1 (typed counted
rejects); P14 (structural ABSTAIN with named reason); P9 (refuse-outside-coverage doctrine); P6
(capped WARN when ban constraints bite repeatedly).

---

### E-16 — Margin-call mid-trade — the honest refusal, and what CAN be modeled

**Origin:** EC2-3 (registrar 2).

**Scenario.** An adverse move inflates the SPAN+exposure margin of a short-option portfolio
intraday; free collateral goes negative at a peak-margin snapshot; the broker RMS force-closes
some or all legs at degraded prices before the strategy's own exit triggers.

**Families hit & severity.** For naked/undefined-risk writers this is the mechanism by which real
accounts realize losses WORSE than any strategy-rule exit — a simulated naked-writing P&L that
ignores forced liquidation is optimistic precisely in tail episodes. For defined-risk structures
the loss is capped regardless of margin, so the margin dimension affects capital/ROI, not P&L
path.

**Detection / repo evidence (verified by registrar 2).** The margin domain is a REFUSAL-ONLY
mechanism by design: "Recon found NO sourceable SPAN history anywhere — nothing in code, no NSE
SPAN-archive integration, and `docs/track2/TRACK2_RISK_MGMT.md` §2.3 explicitly FORBIDS hard-coded
margin constants … every shipped table is ONE refusal row from 2020-01-01: `margin_for_date`
raises the typed `UnverifiedMarginRegimeError` for every date in the data window, and the ROI
surface … renders `status="unverified"` … NO runtime escape hatch; the ONLY sanctioned override
paths are DATA" (`brutex/engine/margin_history.py:10-24`; ROI payload law `:243-303` per sibling
section repo-exec).

**External evidence (attributed to the micro-margin section).** The margin stack a short position
actually posts (SPAN 6σ·√2 with 9.3%/14.2% floors · exposure 3%/5% · ELM · +2% expiry-day ELM from
2024-11-20 · NOV · peak-margin 4 random snapshots at 100% since 2021-09-01 — §1.1/§1.4/§2);
shortfall penalty tiers 0.5%/1%/5% per day (§6.1); Groww RMS auto-close policy
(repo-draft-sourced, §6.2, their U11); spread margins reproducible ONLY from the day's SPAN
parameter file (§1.3).

**Handling law.**
- **L1 (the refusal, stated as the law):** intraday margin-path simulation — and therefore
  margin-CALL / forced-liquidation events — is REFUSED for every date until per-day SPAN parameter
  data (or an ADR-stamped ASSUMED row) lands, extending `margin_history.py`'s exact mechanism. No
  fabricated "margin ≈ X% of notional" constant may exist (the
  `brutex/costs/constants.py:126-136` no-escape-hatch doctrine, quoted by the gap-fill section
  §4.3).
- **L2 (what ships WITHOUT SPAN — the capital-basis vocabulary):**
  `capital_basis ∈ {premium, max_loss, unverified}` per structure family:
  * long/debit structures ⇒ `premium` — exact (upfront premium is the whole capital; upfront
    collection verified effective 2025-02-01, per sibling section micro-margin §1.2);
  * defined-risk credit structures ⇒ `max_loss = (width − credit) × lot` — a **disclosed LOWER
    BOUND on true capital** (exchange/broker margin on a credit spread can exceed max-loss and the
    expiry-day ELM add-on is date-dependent), so any ROI on this basis is an UPPER bound and must
    be labeled so;
  * undefined-risk (naked) families ⇒ `unverified` — P&L in ₹ may be reported; ROI must render
    `status="unverified"` exactly like `margin_roi_payload` does today.
  When `capital_basis` gates sizing or ranking it enters `canonical_trade_params` (Golden Rule
  #4).
- **L3 (the standing honesty line):** every naked-family result surface carries a fixed
  disclosure: *forced liquidation is UNMODELED; real-world tail P&L can be worse than simulated* —
  the `brutex/options/executor.py:606-617` honesty-block idiom (per sibling section repo-exec).
- **L4 (regime flags without SPAN):** the DATED margin-regime boundaries that need no SPAN file
  (2024-11-20 expiry-ELM, 2025-02-01 premium-upfront, peak-margin phases, lot-size steps — per
  sibling section micro-margin §8.2) ship as annotation flags per trade date, so a future SPAN-fed
  model can be validated era-by-era.

**Cannot-model.** The intraday margin path (needs per-day SPAN files — new data surface, its own
DEC); peak-margin snapshot timing (random by design); broker RMS trigger levels and liquidation
order (broker-specific, partially repo-draft-sourced only); penalty cash flows (depend on the
unmodeled shortfall path).

**Test types.** Unit: `capital_basis` refusal arms (naked family requesting ROI ⇒ `unverified`,
never a number). Property: defined-risk `max_loss` bound consistency with the E-10 L5 cap
invariant. Drift pin: no module under the future options engine imports a margin CONSTANT (the
AST-pin idiom the repo already uses for import bans, e.g. `tests/unit/test_gpu_gate_drift_pin.py`
precedent per CLAUDE.md §1 blurbs — ledger claim).

**Reuse.** P9 (the all-refusal margin table IS the pattern — `margin_history.py:10-24`); the
honesty-block idiom (`executor.py:606-617` per sibling section repo-exec); the
annotation-never-reranks precedent for regime flags; `09-capital-margin-and-sizing.md` for the
full margin-stack evidence.

---

## 4. Cross-case matrices

### 4.1 Design-input digest (one-line law · primary detector · primary NEW artifact · blocking dependency)

From registrar 1's cross-case summary, re-keyed to the final ids:

| E | One-line law | Primary existing detector | Primary NEW artifact | Blocking dependency |
|---|---|---|---|---|
| E-06 | 0DTE is an axis, hold-to-expiry is a typed refusal until the expiry-engine DEC | Greeks `AT_EXPIRY`/`BELOW_INTRINSIC` stamps (`brutex/data/greeks_enrich.py:1037-1062`) | expiry-day classifier + FSP model w/ error band | FSP model + expiry state machine (spec stub, `docs/track2/CALCULATOR_SPEC.md:275-288` per sibling section repo-exec) |
| E-01 | Structure entry is ATOMIC: any missing leg rejects the whole structure, counted per leg | `lake_source` honest-miss (`brutex/options/lake_source.py:11-33`) + executor NO_DATA (`brutex/options/executor.py:532-541`) | chain-completeness preflight (registry × ledger × partitions) | none (buildable now as a census) |
| E-03 | Unknown MTM never triggers; dark leg exits at last-stored adverse extremes under an append-only reason | session_gap law (`brutex/engine/trade_sim.py:58-67`); interior-gap shape (`brutex/data/interior_gap_detect.py:28-47`) | cross-leg relative-presence detector + `leg_dark` exit reason | structure-level trade sim (all NEW per sibling section repo-exec) |
| E-13 | Ladder/step = vendor data as-of the instant; unlisted-at-t strikes unselectable | `resolve_strike_step` + confidence (`brutex/data/moneyness.py:106-159`) | ladder-as-of index (first-stored-bar bounds) | stats-manifest reuse; registry has no listing time (repo-lake G4) |
| E-14 | Stock CA ⇒ refuse the underlying's window until a sourced CA calendar + adjustment model land | `economics_integrity` within-expiry lot law (`brutex/data/economics_integrity.py:22-31`) — snapshot-only | cross-snapshot lot/ladder discontinuity detector + CA calendar | vendor representation probe (EU-05) |
| E-15 | Ban state is external data; no calendar ⇒ stock-option results unclaimable (strict refuse or named ABSTAIN) | none possible from lake (honest) | ban calendar ingest + entry/adjust/reduce permission matrix | historical ban-list sourcing (EU-08) |
| E-09 | Registry-resolved expiries (shift-aware); v1: skip Muhurat entries, session-object durations | `MUHURAT_SESSIONS`/`get_session` (`brutex/calendars/nse.py:429-436,737-744`); session filter (`brutex/data/session.py:251-278`) | none (laws + counters only) | 2026 timing circular (calendar row) |
| E-04 | No spot ⇒ no ATM selection (counted skip); management abstains; premium rules continue | `STATUS_NO_SPOT` + drift ladder (`brutex/data/greeks_enrich.py:1042-1044`) | spot seam for the options engine w/ inherited gates | none |

Registrar 2's cases add: E-10/E-11 need no new data (laws over existing machinery + counters);
E-07 blocks on the expiry state machine + settle proxy (ε) with the settlement-price ingest (EU-17)
as the real closer; E-12 blocks on the curated MWCB event table (EU-19); E-16 blocks on per-day
SPAN parameter data (a new data surface); E-05 and E-08 are pure derivation-discipline (no new
data; E-08 L5 shares the chain-completeness census dependency with E-01).

### 4.2 Compound interactions (the edges a design doc must not treat separately)

From registrar 2's interaction map, re-keyed:

| Interaction | Compound behavior | Governing entries |
|---|---|---|
| Gap morning × illiquid leg | deep-ITM short leg's first bars absent/volume-0 exactly when the gap must be priced → deferral, else censor-and-count (the biased-censoring disclosure) | E-10 L4 · E-02 D1-D3 |
| Expiry day × illiquid leg | forced buy-back at `T_f` unpriceable → deferral ladder terminates in the hold-to-settle route, never in a volume-0 print | E-06 (endgame) · E-02 D3(ii) |
| Expiry endgame × missing IV | `AT_EXPIRY`/`BELOW_INTRINSIC` statuses cluster in the last window → Greek-based logic structurally off there | E-06 · E-05 L2 |
| Halt window × short leg | cannot buy back during the halt → synthetic-exit convention + flag, disclosed assumption | E-12 L1 · E-16 L3 |
| Multi-trigger bar × deferral | structure closes (reason from the precedence ladder) while one leg's fill defers to its first tradable bar | E-11 PR-2 · E-02 D5 |
| Margin realism × naked gap loss | the very trades where forced liquidation would bite are the unmodelable ones → the standing naked-family disclosure line | E-16 L3 · E-10 |
| DTE regimes × expiry endgame | the (a)/(b) worst-of comparison and the expiry-day flags key off the VENDOR expiry date, so weekday drift would silently move the whole endgame machinery | E-08 L1 · E-06 |

Registrar 1's cross-case interactions fold in: Muhurat × expiry-week collision (E-09 × E-08 —
holiday-shifted expiry resolution); strike-regime × corporate action (E-13 × E-14 — the same-expiry
ladder-discontinuity signature must be disambiguated by underlying type + spot-jump + OI-teleport
before either verdict fires); spot hole × leg dark (E-04 × E-03 — a whole-session spot blackout
while the chain prints is E-04 rule 3, and any open structure falls to E-03's marking tiers).

---

## 5. Handling-law summary

Every case above resolves into FIVE handling laws plus three cross-cutting disciplines. This is
the section a future options-engine DEC should quote.

### Law A — Uniform volume-0 / absent-bar reject, extended by the deferred-exit law

*The demonstrated-transaction anchor:* a fill may only ever be priced against a bar that proves a
real trade. Entry-side this is already shipped and uniform, no knob: `volume == 0` ⇒ `STALE_BAR`
unconditionally (`brutex/options/executor.py:543-557`; futures twin
`brutex/futures/executor.py:31-41` per sibling section repo-exec; knob deleted under
DEC-FILL-WORST-CASE-ONLY-001 ruling 2 per sibling section gap-fill), and an absent bar is an
honest `None` ⇒ `NO_DATA` (`brutex/options/lake_source.py:151-157`, `:11-33`). Exit-side, where a
stateful engine cannot un-exist an accepted entry, the extension is E-02's deferred-exit law
(D1–D5: walk forward to the first `volume > 0` bar, worst-case-anchored, counted, escalating
deterministically at session end) with the direction-of-bias qualifier stated verbatim.

The per-leg **fill-honesty predicates** and per-structure **classes** (fill-realism section
§3.1/§3.2; named P1–P6 there, FH-1…FH-6 here):

| # | Predicate | Existing machinery |
|---|---|---|
| FH-1 | bar present | `lake_source.load_pair` → `None` ⇒ `NO_DATA` (`lake_source.py:151-156`; `executor.py:532-541`) |
| FH-2 | `volume > 0` | `STALE_BAR` gate (`executor.py:543-557`) |
| FH-3 | `volume ≥ λ × leg_qty_units` | NEW gate; λ swept; floor idiom precedent `brutex/dashboard/movers_snapshot.py:436,457` |
| FH-4 | liquid-band membership (\|`atm_offset`\| ≤ B strikes, near tier, index underlying) | moneyness + registry tier machinery verbatim |
| FH-5 | `leg_qty_units ≤ freeze_quantity` | freeze data plumbed, gate NEW (`brutex/execution/types.py:72-81`; law `docs/track2/TRACK2_RISK_MGMT.md:59-62`) |
| FH-6 | (optional) OI floor | OI first-class per bar |

**Class A** (all of FH-1…FH-5 on every leg) = bounded-fill at the simulated size; **Class B**
(FH-1 ∧ FH-2 everywhere, ≥1 leg fails FH-3/FH-4) = price bound holds for a marginal order only;
**Class C** (≥1 leg fails FH-1 or FH-2, or FH-5/lot/cost-regime refusals) = atomically refused,
counted with the failing leg + reason. Counters follow the `OptionsRuleResult` pattern
(`executor.py:602-712` — typed per-reason counters, `__post_init__` consistency at `:675-700`,
derived `n_rejected` at `:702-712`), with `n_accepted == n_class_a + n_class_b`.
*Covers:* E-01(c), E-02, E-06 (route-a illiquidity), E-10 L4, E-11 PR-2 deferral.

### Law B — Whole-structure correct-or-absent (atomicity)

A structure is the trade unit; a partial structure is a DIFFERENT risk object and is never
silently entered, held, or closed. Concretely: atomic reject-if-any-leg-rejects at entry (E-01
handling 2; Class-C atomicity, fill-realism §3.2); atomic closure — a structure-closing event
closes ALL legs on the same bar, no orphan legs ever (E-11 PR-2, E-02 D5); censor-and-count when a
structure cannot be honestly priced out (E-02 D3(iii) `censored_open_exit`, E-10 L4
`censored_on_gap` — with the mandatory disclosure that the censored set is biased toward
blow-ups); and the session-boundary retro-close transplant of the spot P4 law (E-03 tier 4,
`brutex/engine/trade_sim.py:58-67`).

**The one design fork this register surfaces (unresolved, for the future DEC):** for a leg dark to
the intraday horizon, registrar 1 proposes retro-closing the structure at the last instant every
leg has a stored bar, each leg at its last stored bar's adverse extreme under a new `leg_dark`
append-only reason (E-03 tier 3 — the P4 generalization); registrar 2 proposes CENSORING the
structure outright, holding that pricing any close off a volume-0 print is forbidden (E-02
D3(iii) — the demonstrated-transaction anchor). The reconciliation constraint both sides already
imply: a retro-close anchor is only admissible if it is itself a demonstrable print
(`volume > 0`); where no such per-leg anchor exists, censor. Whether to retro-close-at-last-
demonstrable-print or always-censor is a conscious DEC choice — both variants are typed, counted,
and fabricate nothing; the DEC must pick one and pin it with goldens.

### Law C — Condition-abstain on missing feature

A strategy condition consuming an input the data cannot honestly supply evaluates NOT-TRUE
(bit = 0), counted — never a fabricated value, never a crash: Greek/IV NULLs and absent overlay
columns (E-05 L1/L3, over the P8 status ladder `brutex/data/greeks_enrich.py:1035-1062` and the
`APPLIES_TO_1DAY_MASK` bit-abstain precedent `brutex/engine/stage1a.py:1050-1056,1319-1349`);
NO_SPOT entry skips + management abstains (E-04, `greeks_enrich.py:1042-1044`); T-floor/AT_EXPIRY
selection gates (E-06 entry-law 4). The load-bearing corollary is E-05 L2: because
abstain-to-0 on an EXIT predicate silently disables defenses exactly on the correlated-bad bars,
**v1 Greek/IV predicates are ENTRY-ONLY** — every exit/defense is price- or time-based.
*Covers:* E-04, E-05, E-06 (entry-4), and the E-12 L3 frozen-underlying consequence (conditions
freeze WITH the frozen input — automatic and honest).

### Law D — Registry-derived calendar features (vendor truth, never formula)

Every calendar-shaped feature derives from vendor/exchange DATA, with computed tables demoted to
observe-only cross-checks: expiry dates from the registry + the `DDMmmYY` symbol grammar, never
weekday arithmetic (E-08 L1–L3; `brutex/data/registry_store.py:644-659`,
`brutex/data/fno_symbol.py:14-21`, the `brutex/data/futures_series.py:8-23` posture;
`brutex/options/expiry_calendar.py:25-29` explicitly does not apply holiday shifts and must never
be the resolver — E-09 handling 1); strike ladders/steps as vendor data as-of the instant, the
`strike_grid.py` constants demoted (E-13; `brutex/data/moneyness.py:106-159`); session shape from
the session object, never a 375-minute or 15:30 constant (E-09 handling 3, E-06 entry-law 6 —
the 2026-08-03 F&O close change makes this load-bearing); Muhurat truth from the ONE
`MUHURAT_SESSIONS` source (E-09 handling 5). *Covers:* E-08, E-09, E-13, and the E-06 session/
expiry-instant predicates.

### Law E — Refusal classes (typed counted refusals + dated refusal-row tables for external data)

Whatever cannot be simulated honestly REFUSES — typed, counted, named, remediable; never a silent
drop, never a fabricated number. Two shapes:
1. **Engine-level typed refusals** on the P1 pattern, appending new enum members
   (`HELD_TO_EXPIRY_UNSUPPORTED` E-06; `NO_CONTRACT` split E-01; `CORPORATE_ACTION_SUSPECTED`
   E-14; `BAN_PERIOD`/`BAN_ADJUST_BLOCKED` E-15), plus the shipped Step-0
   `UnsupportedTradeOutcomeError` stub for expiry outcomes (E-07 evidence 1).
2. **Refusal-row dated tables for EXTERNAL data** on the P9 shape (rate-less rows raise typed
   errors; no runtime escape hatch; override paths are DATA — the
   `brutex/costs/constants.py:126-136` doctrine and the all-refusal
   `brutex/engine/margin_history.py:10-24` twin). The recurring NEW data surfaces this register
   forces, each an external-sourced dated table with its own DEC:
   - **FSP / daily settlement prices** (E-07 L4; until then the ε-band adverse proxy of E-07 L3);
   - **corporate-action calendar** (E-14 handling 3, on the `lot_size_history` dated-table shape);
   - **ban calendar** (E-15; until then STRICT-refuse or the named structural ABSTAIN, P14);
   - **MWCB halt event table** (E-12 L1, on the `MUHURAT_SESSIONS` curated-table idiom);
   - **per-day SPAN parameter files** (E-16 L1; until then the capital-basis vocabulary L2).
   Out-of-coverage dates ABSTAIN loudly; every row carries its citable source (the
   holiday-calendar per-row-citation law, `brutex/calendars/nse.py:393-398`).

### Cross-cutting disciplines (bind all sixteen entries)

- **Worst-case pessimistic pricing, inherited not invented:** the with-the-level clamp + adverse
  extreme ± tick (`brutex/execution/fill_mode.py:10-13`, `brutex/engine/trade_sim.py:37-42`), the
  SL-first tie generalized to the PR-1 precedence ladder (E-11), the worst-of endgame law (E-06),
  and the spread-penalty axis kept an UNCALIBRATED robustness knob — never a claimed cost model
  (fill-realism §4.1, tick-floor law `brutex/costs/calculator.py:163-174` per that section).
- **Typed + counted + annotation-never-reranks observability, with the identity law:** every new
  counter/flag is an OUTPUT out of run identity; every knob that GATES or is swept enters
  `canonical_trade_params` (Golden Rule #4; the default-omitted DEC-NIFTY-PRICE-SOURCE-AXIS-001
  template for ε and the spread penalty).
- **Index-first scoping** (registrar 1's recurring theme, corroborated by registrar 2): it
  eliminates E-14 and E-15 entirely (no corporate actions, outside the MWPL framework —
  EU-09), moots E-12 L3 (no per-stock bands), simplifies E-07 L5 (cash-settled), and sits on the
  strongest spot machinery for E-04 (index-gated demotion + "indices always print").
- **The chain-completeness census is the shared prerequisite** of E-01, E-03, E-08 L5 and E-13
  (repo-lake G3/h2): registry-expected × ledger-settled × partition-present, buildable now from
  existing unjoined pieces.

---

## 6. Consolidated UNVERIFIED register

Union of both registrars' registers, renumbered EU-01…EU-24 with origins preserved (edge-1 rows
U1…U10 → EU-01…EU-14; edge-2 rows E2-U1…E2-U10 → EU-15…EU-24). The fill-realism section's own
UNVERIFIED register is consolidated with that sibling doc, not duplicated here; its rows U5/U7 are
referenced from E-02/E-06, and its U8 (plus the repo-exec section's #12) is CLOSED by the direct
reads recorded in §0. Nothing in this table is encoded as fact anywhere in this register — each
row states what would verify it.

| EU | Origin | Claim left unverified | What would verify / close it |
|---|---|---|---|
| EU-01 | edge-1 U1 | FSP-vs-15:29-LTP divergence magnitudes (single-digit points calm / tens volatile) — secondary-source only (inherited from micro-settle §2.5/§8.10) | Empirical compute of expiry-day index close vs last 1m spot close over stored history; NSE Clearing settlement-price page body |
| EU-02 | edge-1 U2 | Whether the 2% ELM on expiry-day short options and the other SEBI CIR/2024/132 measures are worded exactly as multi-outlet reported (circular body 403'd for sibling agents; not re-fetched) | Fetch the [SEBI circular](https://www.sebi.gov.in/legal/circulars/oct-2024/measures-to-strengthen-equity-index-derivatives-framework-for-increased-investor-protection-and-market-stability_87208.html) or the CSE mirror PDF |
| EU-03 | edge-1 U3 | The cash/index session shape + FSP construction after the 2026-08-03 NSE F&O 15:40 close (inherited micro-expiry §8.11) — affects E-06's last-30-min window definition | NSE circulars on CAS + revised F&O timings; post-Aug-2026 NSE Clearing settlement page |
| EU-04 | edge-1 U4 | Intraday strike-addition trigger rules (when/at what spot move the exchange adds strikes) — direction solid, parameters not (micro-expiry §8.8) | The NSE strike-scheme annexure / scheme-revision circular bodies |
| EU-05 | edge-1 U5.1 | **Vendor representation of corporate-action-adjusted contracts in Groww data**: new `groww_symbol`s vs in-place rewrite; whether the instrument master restates strike/lot; whether historical candles are restated | A credentialed probe around a known CA event (e.g. the ITC/NMDC dividend adjustments cited in E-14): pull the master + chain before/after and diff |
| EU-06 | edge-1 U5.2 | The extraordinary-dividend threshold HISTORY (current 2%-of-market-value per [NSE Clearing](https://www.nseclearing.in/clearing-settlement/equity-derivatives/corporate-actions-adjustment) + [Zerodha support](https://support.zerodha.com/category/trading-and-markets/trading-faqs/f-otrading/articles/adjustment-f-o-contracts) as summarized in search results; the 10%→5%→2% evolution dates unconfirmed) | SEBI/NSE adjustment-methodology circulars (2002 framework + amendments) |
| EU-07 | edge-1 U6.1 | Exact MWPL/ban mechanics wording (95% trigger / 80% exit / penalties per NSCC/F&O/C&S/365) — multi-outlet-corroborated; the NSE primary page body was not fetched in-session | Fetch [NSE security-ban page](https://www.nseindia.com/static/products-services/equity-derivatives-risk-management-sec-ban) + [NSE Clearing position limits](https://www.nseclearing.in/risk-management/equity-derivatives/position-limits) |
| EU-08 | edge-1 U6.2 | Availability of a HISTORICAL daily ban-list archive covering 2020→today (needed to build the ban calendar for backtests) | NSE archives probe; failing that, third-party archives — each row then needs its citable source per the holiday-list law |
| EU-09 | edge-1 U6.3 | "Index derivatives are outside the MWPL ban framework" — consistent with the framework applying to securities; primary confirmation not fetched | Same NSE pages as EU-07 |
| EU-10 | edge-1 U7.1 | "An expiry never settles ON a Muhurat session" as a GENERAL rule — the 2025 instance is evidenced (expiry traded Mon 2025-10-20, [Angel One](https://www.angelone.in/news/market-updates/nifty-weekly-expiry-today-sammaan-capital-and-rbl-bank-under-f-and-o-ban-on-october-20-2025)); 2021-11-04 (Thu-era Diwali) not individually verified; no primary circular stating the general shift rule was fetched | Per-year NSE expiry-shift circulars for Diwali weeks 2020-2025; the exchange contract-spec holiday-rule text (micro-expiry cites the [Sahi 2026 schedule](https://www.sahi.com/blogs/nse-vs-bse-expiry-shake-up-what-traders) as secondary) |
| EU-11 | edge-1 U7.2 | Whether F&O actually TRADES in every Muhurat session in the data window (the repo keeps such bars if present — `brutex/data/session.py:262-269` — but whether Groww serves F&O Muhurat bars for all 6 dates is a data question) | A lake census over the 6 Muhurat dates on stored FNO partitions (operator box), or a vendor probe |
| EU-12 | edge-1 U8 | Whether any STORED CE/PE partition today contains rows that would fail the unapplied price-sanity gates (inherited repo-quality U6) — sizes E-03/E-04's practical exposure | One-off offline `price_violation_expr` scan over the FNO tree (census `PriceSanityRows` surfaces it) |
| EU-13 | edge-1 U9 | Groww's expired-contract re-pull depth (whether a lost/holey CE/PE window is re-pullable long after expiry) — inherited repo-quality U5; bounds E-03's "self-healing" claim for old holes | Live vendor probe re-fetching an old expired contract window |
| EU-14 | edge-1 U10 | The exact behavior of `get_expiries`/`get_contracts` for ALREADY-EXPIRED expiries (can the registry's expected-set be rebuilt retroactively for 2020-2024 chains? — inherited repo-lake U1); bounds E-01's preflight and E-13's REGISTRY-confidence coverage on historical windows | Credentialed probe of `get_expiries` for a past year, transcript committed per the repo probe convention |
| EU-15 | edge-2 E2-U1 | Quantitative overnight gap-through frequencies/magnitudes for NIFTY/BANKNIFTY/SENSEX weekly strikes beyond the repo-recorded episodes (COVID Mar-2020 day moves `brutex/data/bad_tick.py:24-26`; 2024-06-04 calibration `docs/track2/TRACK2_RISK_MGMT.md:458-472` per micro-margin) — deliberately not quantified | Measure from the lake itself (dense index 1m series): distribution of `open(S+1) − close(S)` per underlying — a cheap, fully in-repo study a design PR could run |
| EU-16 | edge-2 E2-U2 | Whether NSE/BSE levy ETC/SEBI/IPFT transaction charges on EXERCISED (non-traded) contracts (the repo's illustrative branch charges them on intrinsic, marked illustrative-not-normative) — inherited from micro-settle register #3 | A real contract note for an exercised position, or an NSE FATAX circular naming exercise-value charges; encode BOTH branches until then, select by citation |
| EU-17 | edge-2 E2-U3 | Availability of official F&O daily SETTLEMENT prices through any repo-sanctioned data path (Groww API shows no settlement endpoint in the in-repo reference; NSE bhavcopy hosts 403 the research proxy per sibling sessions) | A vendor-doc check + a credentialed probe; if absent, an NSE-archive ingest becomes its own new-data-surface DEC (E-07 L4) |
| EU-18 | edge-2 E2-U4 | Option quote/print behavior while an F&O stock sits at a dynamic-band edge in cooling-off (one-sided prints? LPP rejections binding?) — mechanism confirmed (micro-margin §5.1-5.2 + [RMoney](https://rmoneyindia.com/support/execution-range-vs-circuit-limits-fo-trading/), [Zerodha support](https://support.zerodha.com/category/trading-and-markets/trading-faqs/f-otrading/articles/difference-between-execution-range-and-circuit-limit-fo)), behavior unmeasured | Forward-captured L1 quotes over a band episode; until then E-12 L3 is heuristic + disclosed |
| EU-19 | edge-2 E2-U5 | Exact in-window market-wide circuit-breaker (MWCB) event dates (the March-2020 halts are recalled but not primary-verified; 2024-06-04 did NOT trigger MWCB per the repo draft's framing of it as a crash calibration) | NSE/press primary sources for each candidate date → the curated halt-event table (E-12 L1) |
| EU-20 | edge-2 E2-U6 | The BANKNIFTY-weekly discontinuation date split: repo registry "EOD 2024-11-13" (`brutex/options/expiry_calendar.py:113`) vs the SEBI measure's stated effective 2024-11-20 (micro-margin §2) — both plausibly true (last traded weekly expiry vs measure effectivity); benign, unreconciled | Read NSE FAOP circular(s) for the final BANKNIFTY weekly contract; matters only to era-boundary tests (E-08 L4) |
| EU-21 | edge-2 E2-U7 | That vendor expiry data (symbol `DDMmmYY` token + `get_expiries` registry rows) reflects holiday-SHIFTED actual expiry dates (definitionally expected if the master mirrors the exchange contract calendar; no in-repo example exhibited) | Exhibit one known holiday-shifted expiry week against the registry/symbol set (E-08 L1 residual) |
| EU-22 | edge-2 E2-U8 | The deferred-exit law's real-world direction of bias (D1 is NOT strictly pessimistic vs an instant quote-fill — stated in E-02) — unknowable from this lake (gap-fill U7 class) | Forward-captured quotes joined to deferral windows: fraction with a live two-sided quote inside X ticks during the deferral |
| EU-23 | edge-2 E2-U9 | Whether the PR-5 worst-order property (E-11) is provable in general rather than fixture-wise for all structure/event combinations — open design math | Attempt the proof for the v1 structure grammar; else keep PR-5 as a fixture-level assertion and say so |
| EU-24 | edge-2 E2-U10 | MIGRATION.md entry numbers quoted via CLAUDE.md §-blurbs (#1558, #1790, #1825-family, DEC-RISK-011/015 homes) — ledger-blurb-sourced (the repo's stable DEC-ID convention); bodies not re-read (file size) | Grep `docs/MIGRATION.md` per DEC id if entry-number fidelity is needed |

---

## 7. Sources (web, used across both registrars' sections)

Regulatory / exchange framework:
- [SEBI CIR/2024/132 — index-derivatives framework](https://www.sebi.gov.in/legal/circulars/oct-2024/measures-to-strengthen-equity-index-derivatives-framework-for-increased-investor-protection-and-market-stability_87208.html)
- [NSE Clearing — settlement price](https://www.nseclearing.in/clearing-settlement/equity-derivatives/settlement-price)
- [NSE Clearing — corporate actions adjustment](https://www.nseclearing.in/clearing-settlement/equity-derivatives/corporate-actions-adjustment)
- [NSE — corporate actions adjustments (static)](https://www.nseindia.com/static/products-services/equity-derivatives-corporate-actions-adjustments)
- [NSE — security ban / MWPL (static)](https://www.nseindia.com/static/products-services/equity-derivatives-risk-management-sec-ban)
- [NSE Clearing — position limits](https://www.nseclearing.in/risk-management/equity-derivatives/position-limits)
- [NSE circular FAOP59389 (per-event CA adjustment example)](https://nsearchives.nseindia.com/content/circulars/FAOP59389.pdf)
- [NSE circular FAOP68747 (Sep-2025 expiry-day shift, PDF)](https://nsearchives.nseindia.com/content/circulars/FAOP68747.pdf)
- [NSE strike-scheme revision (BANKNIFTY 2025) — circular mirror](https://www.steelcitynettrade.com/Circulars/Revision%20in%20Scheme%20of%20Strikes%20in%20BANKNIFTY%20Options.pdf)

Broker / market-intel explainers (secondary):
- [Zerodha support — impact of corporate actions on F&O contracts](https://support.zerodha.com/category/trading-and-markets/trading-faqs/f-otrading/articles/adjustment-f-o-contracts)
- [Zerodha bulletin — ITC F&O dividend adjustment](https://zerodha.com/marketintel/bulletin/441076/adjustment-of-fo-contracts-of-itc-due-to-dividend)
- [Zerodha bulletin — NMDC F&O dividend adjustment](https://zerodha.com/marketintel/bulletin/441798/adjustment-of-fo-contracts-of-nmdc-due-to-dividend-3)
- [Zerodha bulletin 437296 — long-dated NIFTY strike interval revision](https://zerodha.com/marketintel/bulletin/437296/revision-of-strike-interval-in-long-dated-nifty-options)
- [Zerodha support — execution range vs circuit limits (F&O)](https://support.zerodha.com/category/trading-and-markets/trading-faqs/f-otrading/articles/difference-between-execution-range-and-circuit-limit-fo)
- [TeamLease RegTech — NIFTY strike-scheme change summary](https://teamleaseregtech.com/updates/article/49219/nse-notified-regarding-the-changes-in-the-strike-scheme-and-strike-int/)
- [5paisa — NSE F&O ban list / MWPL](https://www.5paisa.com/nse-ban-list)
- [Kotak Neo — NSE F&O ban list explainer](https://www.kotakneo.com/futures-and-options/nse-fno-ban-list/)
- [mastertrust — MWPL open-position rules during ban](https://www.mastertrust.co.in/blog/mwpl-open-position-limit-ban-period)
- [Navia — stock circuits explained](https://navia.co.in/blog/stock-circuits-explained-simply/)
- [RMoney — execution range vs circuit limits in F&O trading](https://rmoneyindia.com/support/execution-range-vs-circuit-limits-fo-trading/)

Calendar / expiry / session events (secondary):
- [Angel One — Nifty weekly expiry on 2025-10-20 (Diwali-week shift evidence)](https://www.angelone.in/news/market-updates/nifty-weekly-expiry-today-sammaan-capital-and-rbl-bank-under-f-and-o-ban-on-october-20-2025)
- [HDFC Sky — Diwali 2025 Muhurat timings (13:45–14:45)](https://hdfcsky.com/news/five-day-festival-calendar-from-18-23-october)
- [Groww blog — NSE F&O hours extended to 15:40 from 2026-08-03](https://groww.in/blog/nse-extends-f-and-o-trading-hours-by-10-minutes-new-timings-effective-from-august-3-2026)
- [Arihant Plus — Tuesday is the new Thursday (Sep-2025 expiry shift)](https://www.arihantplus.com/blogs/market-updates/tuesday-is-the-new-thursday-nse-and-bse-expiry-shift-from-sep-2025/)
- [Kotak Neo — SEBI to end Thursday expiry](https://www.kotakneo.com/news/market-news/sebi-to-end-thursday-expiry/)
- [Ventura Securities — changes in expiry, NSE and BSE](https://www.venturasecurities.com/blog/changes-in-expiry-nse-and-bse/)
- [ICICIdirect — SEBI clears expiry-day clash between NSE and BSE](https://www.icicidirect.com/research/equity/finace/sebi-clears-expiry-day-clash-between-nse-and-bse)
- [Moneysukh — SEBI realigned NSE expiry day to Tuesday, BSE to Thursday](https://learn.moneysukh.com/sebi-realigned-nse-expiry-day-change-to-tuesday-and-bse-to-thursday/)
- [Sahi — NSE vs BSE expiry schedule (holiday shift rule, secondary)](https://www.sahi.com/blogs/nse-vs-bse-expiry-shake-up-what-traders)

All other web-grounded market facts are used via attributed sibling sections (micro-settle ·
micro-margin · micro-expiry · gap-fill), each carrying its own source list and 403-wall
disclosures in its absorbing sibling doc.

---

*End of the edge-case register. Assembled from the two registrar sections + the fill-realism
classes; every `path:line` citation and every UNVERIFIED tag from the inputs is preserved above.
This file is the deliverable; no repo source file was touched.*
