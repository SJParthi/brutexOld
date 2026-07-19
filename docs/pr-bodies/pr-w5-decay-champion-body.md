# W5 DRAFT PR body (final draft)

Title: feat(nightly): R48 decay detection + champion/challenger ledger (W5, club-only)

> **CLUB-ONLY — DO NOT MERGE.** This PR stays DRAFT; it lands only through the
> club integration vehicle. (Coordinator instruction, LANE W5.)

## 1. Summary
LANE W5 delivers P0 items 11 + 12 — **DEC-NIGHTLY-DECAY-R48-001 (#1981)**,
**DEC-CHAMPION-CHALLENGER-001 (#1982)**, **DEC-NIGHTLY-RUN-DELTA-001 (#1983)**:
- The **R48 strategy-decay detection leg**: a post-chain, fail-open REPORT-LEVEL
  nightly leg (the #1939 box-update pattern — never a chain link) re-simulates
  the newest `sweep_runs/<id>/top100.json` finalists over the trailing ~63
  sessions on the EXISTING trade-sim kernels and applies the ONE lockbox
  tolerance law (trailing Sharpe < 0.70x discovery OR trailing max-DD > 1.5x
  discovery => DECAYED; low-N ABSTAINS honestly). Verdicts land in the ADDITIVE
  `decay-diff.json` sidecar (artifact bytes NEVER rewritten/re-ranked — R13),
  the report's `decay` block + one honesty-note line, ONE aggregated
  `strategy_decayed` Telegram WARN per run, and retire-FLAG annotations.
- The **champion/challenger promotion machine** per (instrument, tf, direction)
  scope: durable `champions/state.json` + the NEW hash-chained
  `champions/champions.jsonl` ledger (AuditWriter actor "champions" — Golden
  Rule #8); challengers = tonight's cascade-PASSING top100 entries; domination
  = the pooled {champion + challenger} Stage-4 cascade re-run + the born-Rust
  `champion_dominates` lockbox law; auto-promotion after 3 CONSECUTIVE
  dominating nights; retirement = era-tagged ANNOTATION only.
- **run-delta.json**: entered/dropped/rank-moves between the prior night's
  artifact and tonight's, joined on the snapshot-free identity key
  (mask|direction|instrument|tf|sl|tp — Rule-4 MINUS data_snapshot_hash +
  code_commit_sha, which re-key nightly).

## 2. Before / After
- BEFORE: `R48_DIVERGENCE_NOTE` honestly said the decay diff was deferred to
  v1.1; a decayed finalist sat silently in top100.json forever; no champion
  state existed; nightly runs could not be compared run-over-run.
- AFTER: every wet nightly re-judges the finalists (kill switch
  `BRUTEX_NIGHTLY_DECAY`), maintains champions with a tamper-evident promotion
  ledger (`BRUTEX_NIGHTLY_CHAMPIONS`), and writes the run-delta compare. The
  note is rewritten BUILT; runbook Honest-envelope row 1 reconciled.

## 3. Design (born-Rust law)
- `rust/fno-math/src/decay.rs` — `trailing_fold_core` (ONE sequential pass:
  population-sigma Sharpe Option<f64>, running-peak max-DD with NEG_INFINITY
  peak init + NaN-safe compares, net sum), `decay_verdict_core` (codes
  0=HEALTHY/1=DECAYED/2=ABSTAIN_LOW_N/3=ABSTAIN_UNDEFINED; non-finite
  tolerances/discovery => Err => PyValueError) and `champion_dominates_core`
  (strictly-higher Sharpe AND max-DD <=). Exposed as `decay_trailing_fold` /
  `decay_verdict` / `champion_dominates`; full wheel-surface handshake
  (_EXPECTED_COMPILED_SYMBOLS + .pyi + EXPECTED_BUILD.json + math-facade
  golden). Crate stays 0.3.0; Cargo.toml/Cargo.lock byte-untouched.
- `brutex/nightly/decay_math.py` — the seam: `BRUTEX_DECAY_RUST` default ON
  (junk = fail-safe OFF + warn-once — a kernel flag may never kill a nightly),
  16-call burn-in bit-compare (struct.pack) + ship-the-ORACLE autorevert latch,
  refuse-poison out-of-vocabulary code gate, wheel-missing/raise degrade.
  BIT-IDENTITY DESIGN: the oracle is PURE SEQUENTIAL Python (never numpy
  pairwise) so the Rust transcription is op-for-op bit-exact; hypothesis
  differentials pin Rust == oracle. The ULP divergence vs numpy
  `cpcv.sharpe_of_returns` is disclosed immaterial to a 30%-tolerance verdict
  (one measurement basis both sides).
- `brutex/nightly/decay.py` — the leg; tolerances derive from the ONE lockbox
  law (`lockbox.DEFAULT_SHARPE_TOLERANCE`/`DEFAULT_MDD_TOLERANCE` — NEW public
  aliases, never re-minted); re-sim = `load_lake_slice` -> `build_bar_bits` ->
  `mask_to_entry_signals` -> `simulate_trades` (the sweep's own seam); returns
  = pnl/DEFAULT_START_EQUITY_PAISA. `DECAY_MIN_TRADES=10` low-N floor
  (`BRUTEX_DECAY_MIN_TRADES` fail-loud) — a W5-default named constant standing
  in for an un-ruled operator value (disclosed).
- `brutex/nightly/champions.py` — scope key `instrument/tf/direction`
  (separator `/` NEVER `|`: the ledger `subject` preimage forbids `|` — an
  executed in-session defect, fixed at the key law); pooled check via
  `build_candidate_input` x2 -> `run_stage4_cascade`; lockbox metrics via the
  kernel fold of `CandidateInput.lockbox_returns`; every transition appended
  (SEEDED/CHALLENGED/PROMOTED/RETAINED/RETIRE_FLAGGED).
- `run.py`: legs invoked in `_run_locked`'s finally BEFORE `_assemble_report`
  (wet-only by construction — dry-runs dispatch to `_run_dry`, None blocks);
  additive `decay`/`champions` NightlyReport fields; note builders silent on
  disabled/skipped/quiet-ok.

## 4. Laws honored
- #1939 report-level-block law (fail-open, never a chain link, one honesty line)
- R13 (artifacts never rewritten/re-ranked — byte-compare-pinned)
- Golden Rule #8 (hash-chained ledger, verify_chain-pinned)
- Golden Rule #11 COMMON (ONE lockbox tolerance law; ONE entry_join_key; ONE
  contract_run_roots law; GROWW_HISTORY_FLOOR imported)
- Rule-4 identity minus snapshot/commit for the nightly join (disclosed)
- Born-Rust law for verdict/fold math; Python glue disclosed Rust-optional

## 5. Gates (measured)
- cargo test --release --locked: 628 passed (17 new decay tests); cargo fmt
  --check clean; cargo clippy --all-targets -D warnings clean  [re-verified at
  final tree: see final report]
- tests/unit/test_nightly_decay_math.py: 53 passed, decay_math.py 100.00%
  (200 stmts/68 branches)
- tests/unit/test_nightly_decay.py: 35 passed, decay.py 100.00% (213/44)
- tests/unit/test_nightly_champions.py: 51 passed, champions.py 100.00% (343/76)
  [both re-measured post round-1 fold]
- run.py 100.00% (353/80, 55 tests); chain.py 100.00% (203/52, 34 tests);
  sweep_binding.py 100.00% (102/40, 22 tests); sweep_alerts.py 100.00%
  (411/46, 174 tests over the ci.yml 7-file family); lockbox suite 50 passed
- whole nightly family: 486 passed; tests/golden: 862 passed / 126
  optional-wheel skips; tests/golden/fixtures BYTE-UNTOUCHED (git status empty)
- ruff check + ruff format --check: clean; mypy --strict brutex: [final report];
  pyright: [final report]

## 6. Fail-before / pass-after proofs
- R48 decay proof (test_nightly_decay.py::TestRunDecayLeg): over the planted
  real-writer fixture lake a BULL mask-0 finalist re-sims to losing trailing
  returns -> DECAYED + retire-flag + ONE alert + decay-diff.json, with the
  top100 artifact bytes UNCHANGED; the BEAR twin reads HEALTHY with NO alert.
  (Modules are NEW — vs origin/main the behavior did not exist at all; the
  test is the permanent regression net.)
- Executed in-session defects found by the suites and fixed:
  1. champions scope key used `|` -> first ledger append raised
     AuditFieldError (the hash-preimage delimiter refusal). Fixed at the key
     law (separator `/`), pinned by the transition golden.
  2. the run-delta write clobbers tonight's run-dir mtime with the wall clock
     -> discovery re-selected a stale run under synthetic test mtimes.
     Reproduced, fixed in the tests (future-based mtimes) + documented.
  3. `_run_locked`'s `if not plan.dry_run` guard was structurally DEAD
     (dry-runs dispatch to `_run_dry`) — removed; the dry-run law holds by
     dispatch, pinned.
- Flag matrices: every flag's OFF/junk arm pinned (disabled/env_error blocks).

## 7. Honest limits
- The pooled cascade re-run + the decay re-sim judge the DEFAULT Stage-1A cell
  (top100 entries carry no non-default axes) — `axis_defaults_assumed: true`
  in the sidecar.
- A FULL-REAL promotion golden needs a cascade-passing dataset (the 500-trade
  lockbox floor rejects any small fixture); the transition machine is goldened
  over a deterministic _PooledVerdict double, and the REAL pooled path is
  separately proven as the honest RETAINED over the planted lake.
- O(finalists x window bars) per night — never O(1).
- capture_combo is NOT reused (needs a ResultRow; its parity tooth is
  meaningless on a sub-window) — the leg drives the same underlying seam.
- DECAY_MIN_TRADES=10 and CHAMPION_PROMOTE_NIGHTS=3 are W5-default named
  constants standing in for un-ruled operator values (fail-loud env overrides).
- Diff/ledger orchestration is Python over the Rust verdict kernels
  (Rust-optional, disclosed).
- Review protocol COMPLETE: hostile rounds 1 + 2 folded, round 3 SHIP, plus a
  zero-refutation adversarial refuter round (see section 11).

## 8. Follow-ups
- A cascade-passing full-real promotion golden dataset (club/N+1)
- Operator ruling on DECAY_MIN_TRADES / CHAMPION_PROMOTE_NIGHTS values
- W2 fold: trailing_* ResultRow columns (see 9)
- DEC-NIGHTLY-LEG-BASEEXC-REPORT-001 — round-2 ship-over NIT: a
  BaseException (SIGINT) during a W5 leg (which runs before
  `_assemble_report` inside `_run_locked`'s report-guaranteeing finally)
  skips report/bundle/Telegram for that night; ~seconds window,
  non-silent degradation (the process dies loudly); optional fix shape =
  try/except-BaseException-assemble-then-reraise around the legs.

## 9. Collisions / fold seams (T30 + W2)
- **T30 (nightly conversion lane):** W5 touched `brutex/nightly/run.py`
  MINIMALLY — one leg block post-chain / pre-`_assemble_report` (inside
  `_run_locked`'s finally) + the two note builders + the `_assemble_report`
  params. The decay/champions legs are Python-THIN over the
  `rust/fno-math` decay kernels (`decay_trailing_fold` / `decay_verdict` /
  `champion_dominates`), so T30's Rust-shape conversion of `nightly/` can
  ABSORB them by keeping the leg seams and moving the glue; reconcile at
  club time (expect a trivial reconcile if T30 moves `_run_locked`).
- **W2 (#13 trailing columns, branch `claude/metrics-loss-growth-recency`):**
  W5 computes trailing Sharpe/PF/net IN-LEG and deliberately added NO
  ResultRow columns. At club time converge W5's in-leg keys onto W2's
  landed column names `trailing_sharpe_ratio` / `trailing_profit_factor` /
  `trailing_net_pnl_paisa` / `decay_weighted_net_paisa` (single source:
  W2's columns once landed; W5's leg then READS them instead of
  recomputing where available, joined via `entry_join_key`).
- The `decay`/`champions` NightlyReport keys are ADDITIVE after
  `env_preflight` (W4/W6 reserved top100 keys are untouched — W5's sidecar
  files + `retire_flag` state annotation are disjoint).

## 10. Ledger
- MIGRATION.md #1981 DEC-NIGHTLY-DECAY-R48-001, #1982
  DEC-CHAMPION-CHALLENGER-001, #1983 DEC-NIGHTLY-RUN-DELTA-001 (the
  coordinator-granted W5 band; renumber-at-landing applies). CLAUDE.md header
  pointer + section-1 blurb + section-3 line updated;
  docs/runbooks/nightly-automation.md R48 row rewritten BUILT.

## 11. Review status
- CLUB-ONLY DRAFT. Do not merge this PR directly.
- **Current head: 99e054942d714a5e160005859bb616ef7f725a4e** (includes the
  round-3 + refuter docs fold, MIGRATION.md #1981/#1982).
- **PROTOCOL COMPLETE — READY FOR CLUB FOLD:** round-1 folded, round-2
  folded, **round-3 SHIP** (zero BLOCKER/MAJOR/MINOR; one ship-over wording
  NIT — the trailing-save over-warn on a RETAINED/CHALLENGED-only night,
  safe-direction, consciously shipped), **adversarial refuter round: ZERO
  refutations** — all 8 claims upheld under executed attack (real-SIGKILL
  crash convergence · Rust≡oracle hostile-float grid · R13 sha256+mtime
  byte-immutability on a DECAYED night · 3 hand mutants killed; the 3
  informational incidentals all already disclosed — forged-`|`-scope
  un-ledgered state seed under the under-count law, theoretical
  `/`-separator ambiguity for unreachable inputs, and
  DEC-NIGHTLY-LEG-BASEEXC-REPORT-001). 2 consecutive clean rounds =
  protocol complete. Refuter evidence: scratchpad/w5_refuter/.
- **Hostile review round 1: FIX-FIRST → ALL findings folded** (2026-07-19,
  head after ce250851; per-finding ledger record in MIGRATION.md #1981/#1982;
  every behavioral fix carries an EXECUTED fail-before/pass-after proof —
  all 7 new/reworked pins captured FAILING at the pre-fix source
  (scratchpad/w5_round1_fail_before.txt), green after):
  - **MAJOR #1 — champions ledger ⟂ state crash convergence** → the
    STATE-LEADS / LEDGER-TRAILS law: `_record` persists state ATOMICALLY
    per transition BEFORE the hash-chained ledger append (state.json =
    authoritative snapshot; champions.jsonl = trailing tamper-evident
    commit log — may UNDER-count by the crash window, never over-assert;
    no retroactive catch-up lines — the pinned law); the trailing
    end-of-leg save is soft (OSError → WARN + `state_path: null`);
    per-transition fsync cost disclosed (AuditWriter cadence unchanged).
    Pins: `test_state_save_failure_no_ledger_entry_converges` (arm a) +
    `test_promoted_ledger_crash_state_leads_converges` (arms b+c — the
    challenger-dropped-out phantom-PROMOTED shape converges, era stays 2,
    ZERO PROMOTED ledger lines, verify_chain green).
  - **MINOR #2 — discovery keyed on run-DIR mtime** → re-keyed on the
    IMMUTABLE `top100.json` FILE mtime (R13) with the (mtime, path)
    tie-break; the tests' synthetic-future-mtime crutch REMOVED (natural
    mtimes); pin `test_sidecar_write_does_not_rekey_discovery`.
  - **MINOR #3 — lossy join key** → the entry schema PROVABLY lacks the
    axis fields (`top100_artifact._entry_payload`), so the LOUD collision
    guard shipped: `_entries_by_join_key` (keep-FIRST + count + one WARN)
    on both run-delta key maps + the additive `join_collisions` key;
    widened key = named follow-up **DEC-TOP100-AXIS-JOIN-KEY-001**;
    pin `test_join_key_collision_keeps_first_and_warns`.
  - **NIT #4 — hostile `|`-scope aborts the leg** → per-scope try
    isolation (`scope_errors` count + one `nightly.champions.scope_error`
    WARN; other scopes proceed); pin `test_hostile_scope_isolated`.
  - **NIT #5 — fresh Catalog per challenged scope** → ONE lazily-opened
    `_SharedCatalog` per leg (closed in the leg's finally; direct callers
    keep a private open/close); pin
    `test_shared_catalog_opened_once_across_challenges` (fail-before: 2
    opens → 1).
  - **#6 (accepted, disclosure only)** — the negative-discovery-Sharpe
    quirk sentence added to the decay module docstring + MIGRATION #1981.
  - Post-fold gates (measured): champions 100.00% (343/76, 51 tests) ·
    decay 100.00% (213/44, 35) · nightly family 589 passed · goldens 862
    passed with fixtures BYTE-UNTOUCHED (the transition-chain golden held
    UNMODIFIED — the state-leads reorder changes ledger content not at
    all) · ruff + format clean · mypy --strict clean (498 files) ·
    pyright 0 new (source files 0/0) · cargo untouched (decay.rs no diff).
- **Hostile review round 2: SHIP — all round-1 fixes verified
  REAL-AND-PINNED; 1 MINOR fixed + pinned, 1 ship-over NIT recorded**
  (2026-07-19; ledger record in MIGRATION.md #1981/#1982):
  - **MINOR — soft failures invisible on the morning note** → the
    `_champions_note`/`_decay_note` ok arms now surface soft failures:
    champions composes only the true clauses ("N scope error(s)" /
    "state NOT persisted" — see report.json + logs) when
    `scope_errors >= 1` or `state_path is None`; decay emits
    "diff NOT persisted" when `diff_path is None`. Noteworthy-transitions
    / DECAYED / error arms byte-identical (precedence pinned); the bare
    ok-empty pin CONSCIOUSLY FLIPPED (a state_path-less ok block now
    honestly emits; the quiet pins carry the persisted paths a healthy
    leg always stamps). Pins: `test_champions_note_soft_failure_arms`
    (both single-clause arms + composed + junk-scope_errors guard) +
    `test_decay_note_soft_failure_arm`; EXECUTED fail-before at the
    stash-reverted pre-fix source (both new tests FAIL, flipped quiet
    pins pass both trees by design).
  - **NIT (ship-over, recorded)** — DEC-NIGHTLY-LEG-BASEEXC-REPORT-001
    (see section 8; MIGRATION #1981 honest-limits).
  - Post-fold gates (measured): run.py 100.00% line+branch (364/88,
    57 tests, the ci.yml per-module way) · whole nightly family green ·
    ruff + format clean · mypy --strict clean · pyright 0 new · goldens
    fixtures byte-clean.
