# ADR-0023: Worst-case is the ONLY fill model

> **Number provenance:** claimed via the 2026-07-15 coordinator reservation
> map — 0020 is contended (PR #1435), 0021 is assigned to the orchestration
> track, 0022 to the removal track; 0023 is officially reserved to the fills
> track. The renumber-at-landing law stands if the map moves.

- **Status:** ACCEPTED (operator directive 2026-07-14; PR-A of the
  worst-case-fills-only track)
- **Decision ID:** DEC-FILL-WORST-CASE-ONLY-001 (MIGRATION.md #1825;
  absorbs the #1522 FillMode-mode design — the PRECISE opt-out never
  shipped; supersedes-in-part DEC-FUTURES-EXEC-TRANSLATION-001 #1543 — the
  R15 volume-0 fill-by-default clause)
- **Date:** 2026-07-15
- **Branch:** `claude/fill-worst-case-only` (worktree base = PR #1522's head
  `95ee9ce5`)

## Context

This ADR records the FULL flip vs main — precise open-anchored fills →
worst-case-only — in one hop (coordinator shape ruling 2026-07-15: this PR
supersedes-and-closes PR #1522; its ledger row is absorbed here).
PR #1522's FillMode-mode design made worst-case fills the production
DEFAULT while keeping a byte-identical `PRECISE` legacy opt-out behind a
`FillMode` enum, the `BRUTEX_FILL_MODE` env knob, a `--fill-mode` CLI flag
and a `FillPolicy`/`LevelFill`/`SameBarTie` policy surface, plus the
emit-only-when-non-PRECISE identity elide in `canonical_trade_params`.

The operator then directed that worst-case become the ONLY fill model: a
mode axis that can silently re-open an optimistic fill path is itself an
illusion risk, and the dual surface taxed every identity/verify/replay seam
with mode threading.

## Decision

1. **One law, hardcoded.** The adverse-max law is the only fill
   law, hardcoded in the Python walker, the Rust kernels (u64 + u128 via
   MaskWord) and both translation executors: fill-bar TIMING is unchanged
   (signal at bar N → fill bar N+1; forced 15:20; same-bar tie ALWAYS
   SL-first), the fill PRICE assumes the worst the decided bar printed —
   entry at the entry bar's adverse extreme (BUY @HIGH / SELL @LOW); SL/TP
   at the with-the-level clamp (`min(level, low)` for a BUY exit /
   `max(level, high)` for a SELL exit); open-anchored events (forced 15:20,
   Mode-B signal exits) and `end_of_data`/`session_gap` at the decided
   bar's adverse extreme. `FillMode.PRECISE`, `FillPolicy`, `LevelFill`,
   `SameBarTie`, `--fill-mode` and every fill-policy threading parameter
   are DELETED (`FillMode` survives as the single-member `WORST_CASE`
   vocabulary token).
2. **Unconditional identity emit.** `canonical_trade_params` always carries
   `{"fill_mode": "worst_case"}` — the fill_mode parameter is REMOVED from
   every config surface. Rationale: eliding the key would alias worst-case
   run_ids with the frozen precise-era goldens; the unconditional token
   keeps the worst-case pins byte-valid across the absorption (the
   re-locked sweep-run `_GOLDEN_RUN_ID` is stable)
   while the precise-era id is negative-pinned (`_PRECISE_ERA_RUN_ID`).
3. **Legacy artifacts refuse loudly.** A cascade manifest without the
   `fill_mode` key or with a non-`worst_case` token, and a sweep-verify
   generation whose recorded params lack `fill_mode == "worst_case"`,
   refuse with "precise-era … refused (DEC-FILL-WORST-CASE-ONLY-001) …
   Re-run the sweep" — never a silent replay of precise-era results. A SET
   `BRUTEX_FILL_MODE` (any value, even `worst_case`) refuses at launch via
   `execution/fill_mode.require_fill_mode_env_unset` — fail-loud beats
   silent tolerance.
4. **Volume-0 bars reject uniformly.** The `reject_stale_bars` knob is
   deleted on BOTH executors; the futures leg FLIPS from fill-by-default to
   always-reject, consciously superseding the R15 "printed bar is REAL +
   TRADABLE" clause. Honest cost: a SURVIVORSHIP TILT — trades whose fill
   bars printed no volume vanish from the futures leg's results instead of
   being priced; each is counted as a `STALE_BAR` rejection, never silent.
5. **Adverse levy rounding is permanent.** The absorbed design's
   fix-round-2 law (every
   rate-scaled levy CEILs — paisa per leg for exchange/SEBI/IPFT,
   whole-rupee for STT/stamp/GST) loses its mode conditionality; the
   Track-2 charge stack can only be ≥ the statutorily half-up-rounded real
   charge. COSTS_VERIFIED §5 regenerated.
6. **Required anchors.** `TradeInput`/`FuturesTradeInput` require the four
   fill-bar extremes; an anchor-less input is unpriceable and refuses
   naming the DEC.

## Honest envelope

- **INTERIM (coordinator ruling 5) — SUPERSEDED by PR-B
  (DEC-FILL-EXEC-GRID-1M-001, 2026-07-18):** the decision-TF interim
  adverse-extreme law below is RETIRED — every fill now resolves on the
  next-1-minute execution grid (dual-feed, run-identity re-key, SL/TP
  monitored on the 1m grid), so the interim's extra pessimism is gone.
  Historical record: until PR-B, the adverse extreme was the DECISION-TF
  bar's — a 5-minute entry took the whole 5-minute range's adverse depth —
  strictly MORE pessimistic than the operator's next-1-minute-bar target
  law, never optimistic.
- **Golden re-locks superseded (DEC-FILL-EXEC-GRID-1M-001):** this ADR's
  hand-derived golden values were CONSCIOUSLY re-locked AGAIN under PR-B's
  1m execution grid; the #1825-era literals survive only as frozen
  anti-alias pins (e.g. the sweep-run `_PRE_GRID_RUN_ID`).
- The worst-case buy on a ranged F&O bar prices one tick OUTSIDE the
  printed range (extreme + tick) — the honest adverse floor, not a
  printable price; on a FLAT bar the fills equal the retired open±tick
  values exactly, so worst-case can never flatter.
- **Exit-bar MAE fold (hostile-review N2):** on a forced / pending / signal
  exit bar the MAE/MFE excursion fold sees (open, open) while the fill takes
  the bar's adverse extreme, so annotation-layer MAE can understate the
  realized adverse move on that ONE bar — inherited byte-identical from the
  absorbed #1522 worst-case arm, Python + Rust mirrored, annotation-only
  (never P&L, never run identity). Named follow-up, if wanted: fold the exit
  bar's true extreme into the excursion (a results-depth annotation change
  only).
- Precise-era sweep trees are NOT migrated — they refuse; the remediation
  is a re-run (the run identity re-keys via the unconditional token
  anyway, so no precise-era result was reusable).
- The engine-semantics change is the ONE sanctioned exception to the
  standing ENGINE-UNCHANGED posture (operator directive):
  scope/vocab/rate/lake-schema UNCHANGED — NIFTY/BANKNIFTY/SENSEX; the
  vocabulary fixture `tests/golden/fixtures/vocab_golden_vectors.json` is
  byte-untouched (empty diff vs base, verified).

## Assurance register (PR-A gates)

- **[FILL-WORSTCASE-SIDE-SIGN] — FOLDED.** Exact-equality side teeth
  (`test_entry_fill_is_the_adverse_extreme_side_correct`: BUY entry ==
  bar HIGH, SELL entry == bar LOW) + the floor property
  (`test_worst_case_never_overstates_the_open_anchored_price`). The
  high/low-swap mutant was applied to `trade_sim._open_position` and
  verified KILLED (2/3 floor properties fail), then reverted clean.
- **[FILL-SESSION-END-NO-NEXT-1M] — FOLDED.** Last-bar signal → zero
  trades (rust golden + fill-mode unit pins) +
  `test_forced_exit_ts_stays_in_session_never_session_gap` (a forced exit
  timestamps inside its own session, never a session_gap leak).
- **[FILL-SAME-BAR-TIME-ORDER-PHANTOM-LOSS] — WAIVED-ACCEPTED.** Verbatim
  register rationale: "H-then-L = conservative bound; swap-mutant-fails +
  worst_pnl≤open_anchored_pnl property". The same-bar SL-first tie prices
  the stop even when the bar's true intra-minute path hit the target
  first — a phantom loss RELATIVE TO the unknowable path, accepted as the
  conservative bound (never optimistic); the floor property pins that no
  ordering choice can overstate P&L.
- The remaining fill register entries (10 P0 + 4 P1) defer to PR-B's
  1-minute-grid design by coordinator ruling.

## Consequences

- Every non-flat fill bar prices worse than the precise era — the
  anti-illusion floor: the backtest can only UNDERSTATE.
- All goldens re-locked with in-file hand-derivations (MIGRATION.md #1825
  carries the audit); frozen precise-era goldens are retired, their run_id
  anti-alias pinned.
- PR-B inherits the one-law surface with zero mode plumbing to unwind.
