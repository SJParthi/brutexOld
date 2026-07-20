# Pull Request — BruteX

> **Merge-Readiness Charter (DEC-MERGE-GUARANTEE-001):** this PR obeys
> `docs/MERGE_READINESS_GUARANTEE.md` — the §3 gate matrix + §4 Definition of
> Done. Never merge over a red or pending gate. Per Golden Rule #10, partial /
> roadmap / out-of-scope rows are disclosed in the Honest envelope below.

**Status: DRAFT — club-fold-only (lane T17).** Do not merge, do not mark
ready-for-review: this lane lands ONLY via the coordinator's club fold. The
independent refuter round has COMPLETED and its outcome is FOLDED (a
docs-only LEDGER AMENDMENT — behavior delta 6 + two wording corrections +
the Claim-A honest-limits lines; see the 4-agent hostile review section
below + MIGRATION.md #2051; no code change — ledger amendment was chosen
over gate widening).

## Before / After (plain language)

**Before:** feed materialization ran a two-arm lattice (DEC-FEED-RUST-CORE-001,
MIGRATION.md #1797) — the Rust decision kernel behind the default-OFF
`BRUTEX_FEED_RUST` flag with the verbatim Python oracle loop retained as the
live degrade arm; the P-06 `LookAheadBarrier` and the `series_load` decision
arithmetic were pure Python.

**After:** the feed MATERIALIZATION layer is Rust-only and the Python oracle
bodies are DELETED (the zero-Python endgame for `brutex/feed`). The
`brutex_feed` wheel is REQUIRED — wheel-absent fails LOUD with a typed
`FeedError` naming the maturin remediation (`require_feed_kernel`), and the
retired two-arm flags REFUSE loudly instead of degrading
(`require_materialize_flags` — the `BRUTEX_FILL_MODE` /
DEC-FILL-WORST-CASE-ONLY-001 refusal-law precedent). `arrow_table_to_bars`
always routes the strict scan (`scan_bars_strict` — finiteness-FIRST, typed
stop kinds, the unchanged bad-tick screen law of
DEC-SWEEP-DATA-EDGE-GUARDS-001); `LookAheadBarrier` is a thin
exception-materializing wrapper over the wheel's `BarrierCore` (verdict codes
out; every pinned message byte + exception TYPE Python-owned and unchanged —
P-06 semantics preserved); `series_load`'s three decision seams
(`expected_bar_instant` · `ownership_cut` · `boundary_monotonic_break`) run in
Rust over exact epoch-µs integers. Every run-identity-critical materialized
byte (Decimal/CandleBar/error/WARN) is still built by Python — bxsnap2 bytes
preserved by construction, frozen goldens byte-untouched.

**Crate sequencing (append-only law):** this EXTENDS `rust/brutex-feed`
**0.1.0 → 0.2.0 APPEND-ONLY** — the frozen DEC-FEED-RUST-CORE-001 `scan_bars`
and the existing `python.rs` registrations are byte-untouched. The PR #1454
lineage's crate content (DEC-FEED-RUST-CORE-001, MIGRATION.md #1797) is
observed **MERGED into origin/main via the club-CDR2 integration merge
`fa948f9` (PR #1584)** and is contained in this branch's base `4c11f7a` —
the sequencing fact was read from git history (`git log origin/main --
rust/brutex-feed`); the member PR #1454's own live open/closed flag could not
be queried from the build sandbox (GitHub API egress-blocked — reported, not
routed around). No unmerged brutex-feed content precedes this branch on main;
if PR #1454 still shows open it is close-as-superseded territory, and this
PR's append-only extension sequences strictly AFTER the landed crate by
construction.

> **Honesty banner (ADR-0020 §0, verbatim):** expected payoff ≈ 0 —
> conversion is for uniformity, not speed. No speedup is claimed and none was
> measured; the strict scan replaces the deleted oracle loop AND the former
> O(rows) Python isfinite pre-gate, while the barrier adds one constant FFI
> hop per access (bench linearity + ratio gates green).

## Definition of Ready (plan-first gate — DEC-DEFINITION-OF-READY-001)

### Scenarios covered

- Happy: canonical 7-column tables materialize through the strict kernel with
  bxsnap2 bytes byte-identical (frozen parity + snapshot-hash goldens pass
  with fixtures BYTE-UNTOUCHED); barrier record=True/False modes preserved;
  series_load ownership cut / boundary verdict / expected-bar verdict
  bit-equal to the deleted walk (docstring-recorded proof + frozen in-test
  oracle differential).
- Edge: non-canonical schemas take decision COPIES (f64 casts / exact
  epoch-µs) while materialization keeps the ORIGINAL `to_pylist` objects —
  an int-price hostile table still materializes `Decimal(str(5))`, the deleted
  oracle's exact bytes; first-bar-of-session bad-tick exemption; IST-midnight
  straddles; the fixed +05:30 civil-day-ordinal law provable only inside
  [epoch 0, 9999-12-31T00:00:00Z).
- Empty/zero: empty tables/segments; zero-bar boundary slivers keep the
  series_load skip/refusal laws unchanged.
- Failure: wheel-absent → loud typed `FeedError` (boot stays wheel-lazy — the
  requirement bites at first materialization/barrier construction); null
  consulted columns → typed corrupt-column ValueError; out-of-envelope
  timestamps with the screen on → loud envelope refusal; SET-falsy/junk
  retired flags → loud refusal naming the DEC; poisoned kernel output →
  `validate_strict_scan` refuses naming the violated law (a poisoned wheel
  can only refuse, never serve); kernel fault → FeedError wrap;
  MemoryError/KeyboardInterrupt/SystemExit re-raise RAW.
- Concurrent: the kernel functions are pure; barrier state is per-instance;
  no shared mutable state crosses the FFI.

### Failure modes

The SIX ledgered behavior deltas (each hostile/planted-input-only,
sanctioned + disclosed in MIGRATION.md #2051; delta 6 + the wording
corrections in deltas 2/3 are the folded refuter-round outcome):

1. **Wheel-absent** feed materialization now fails LOUD (was: silent
   byte-identical Python oracle); imports stay wheel-lazy so boot without the
   wheel still works.
2. **Null consulted columns** (timestamp/open/high/low/close/volume): a typed
   corrupt-column ValueError. Refuter wording correction (Golden Rule #10
   both directions): "was raw TypeError escapes" was wrong for ONE executed
   sub-case — a null `volume` on a row the bad-tick screen SKIPS: the deleted
   oracle silently SERVED 2 bars (the skipped row's null volume was never
   consumed); the new kernel refuses the WHOLE table — refuse-harder, but the
   old behavior there was a silent serve, not a raw escape (every other
   null-column shape was the raw-TypeError escape as originally worded).
3. **Out-of-envelope timestamps** with the screen on: the loud envelope
   refusal — planted/out-of-band data only; real bars are post-2020 by
   `GROWW_HISTORY_FLOOR` (DEC-PULL-FLOOR-2020-001). Refuter wording
   correction: "was served via ZoneInfo" is wrong for the NAIVE pre-1970
   stamp — the deleted oracle RAISED the naive-constructor wrap there (it did
   not serve), so that shape is raise-vs-raise-differently inside the same
   trigger class, never serve→refuse.
4. **SET-falsy/junk `BRUTEX_FEED_RUST` / `BRUTEX_FEED_MATERIALIZE_RUST`**: a
   loud refusal naming DEC-FEED-RUST-MATERIALIZE-002 (the retired-axis law).
5. **>2^53 integer-price hostile tables**: decision-plane f64 rounding —
   materialized values unchanged; decisions could differ from exact-int
   comparisons (the disclosed residue).
6. **Planted `decimal128(20,2)`-price tables with the bad-tick screen ON**
   (REFUTER-FOUND — Claim B letter-refuted; ledgered post-round): the deleted
   oracle CRASHED with a raw `TypeError: unsupported operand type(s) for *:
   'decimal.Decimal' and 'float'` (Decimal close × float ratio inside
   BadTickScreen); the new kernel SERVES the bars (`float(Decimal)` succeeds
   in `_decision_floats`) and on a ≥4x decimal jump skips-and-WARNs where the
   oracle crashed — accidental-crash → plausibly-correct serve;
   planted-dtype-only (no production writer emits decimal128 price columns).

Honest-limits addendum (the hostile-review NIT + the refuter's Claim-A
degenerate-input observations — each stricter-or-equal,
degenerate-input-only, no production caller affected): barrier indices
≥ 2^63 now raise OverflowError at the FFI edge instead of comparing in pure
Python — a loud raise, never a silent look-ahead; unreachable in practice —
with the refuter nuance spelled out: `advance_to(2**63)` silently SUCCEEDED
in the deleted pure-Python barrier (cursor = 2^63) while the new core raises
OverflowError (strictly stricter; core state unchanged, the barrier stays
usable after the FFI OverflowError); bool indices (`bool ⊂ int`) raise the
SAME LookAheadError type with cursor-repr message BYTES differing ("current
False" vs "current 0" — the oracle interpolated the stored bool object, the
Rust core stores i64); float indices: the oracle silently ACCEPTED
`access(1.5)`, the new core raises TypeError ("'float' object cannot be
interpreted as an integer") — typed-contract enforcement, never a silent
pass.

### Real-run proof

```
.venv/bin/python -m pytest tests/unit/test_feed_rust.py \
  tests/integration/test_feed_replay_rust_seam.py \
  tests/integration/test_feed_replay.py \
  tests/integration/test_feed_replay_bad_tick_screen.py \
  tests/property/test_feed_rust_parity.py \
  tests/golden/test_feed_rust_bxsnap2_parity.py -q     # 150 passed (re-run at the folded head)
```

Executed verbatim pass counts (lane record + fold re-verify): feed family
**237 passed unmodified** on the deleted tree · consumer suites **144** ·
golden family **858 passed / 126 skipped, fixtures byte-untouched** · engine
unit subset **4633 passed** · cargo **61–71 green** (`--locked`, fmt + clippy
`-D warnings`, default AND `--features python`) · coverage **100.00%
line+branch** on `feed_rust.py` (85/40), `replay.py` (163/42),
`lookahead.py` (69/10 — ci-exact gate re-run at the folded head, 27 passed),
`series_load.py` (89/30 — ci-exact gate re-run at the folded head, 40
passed) · barrier teeth **27/27** · review-executed feed suites **151/151** ·
suffix-independence + lookahead **48/48**.

### Rollback plan

- Revert this branch's commits (`f68f918`..`1f9ac5b`). The crate extension is
  APPEND-ONLY — `scan_bars` + the frozen `python.rs` registrations are
  byte-untouched — so a revert restores the exact DEC-FEED-RUST-CORE-001
  (#1797) state: crate 0.1.0, default-OFF two-arm flag, live Python oracle.
- No lake/schema/scope/vocab/rate change to unwind; run identity untouched
  (bxsnap2 goldens byte-identical), so no re-sweep is triggered either way.

### Out of scope

- `brutex/feed/resampler.py` — RETAINED, not ported, not deleted:
  `resample_bars` is production-dead (the #1492 G3 AST pin) but the module's
  `INTERVAL_SECONDS`/`RESAMPLEABLE_INTERVALS` constants are production-live;
  whole-module retirement is the named follow-up
  **`DEC-FEED-EPOCH-RESAMPLER-RETIRE-001`** (RECOMMENDED for deletion).
- `series_load` orchestration (Catalog/reconcile/futures-series/calendar I/O,
  window intersection arithmetic, error/log materialization) stays Python —
  the honest residue.
- No speedup claimed (ADR-0020 honesty banner); no default-flip DEC needed —
  the flip IS this PR, refusal-law instead of burn-in per the operator's
  compressed no-soak cycle (recorded in MIGRATION.md #2051).
- ENGINE results UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no
  lake/schema/scope/vocab/rate change.

---

## Summary

- The feed MATERIALIZATION layer converts to Rust and the Python oracle
  bodies are DELETED: `brutex_feed` wheel REQUIRED (loud typed refusals, no
  degrade lattice), crate `rust/brutex-feed` 0.1.0 → 0.2.0 append-only
  (`scan_bars_strict` + `BarrierCore` + the series decision arithmetic;
  `_EXPECTED_COMPILED_SYMBOLS` + `EXPECTED_BUILD.json` grow to 8 symbols).
- Run identity byte-preserved by construction — every materialized byte stays
  Python; frozen bxsnap2 + snapshot-hash goldens pass with fixtures
  byte-untouched.
- This fold commit (`1f9ac5b`) lands the hostile review's non-blocking MINOR
  (three stale "development-stage Python arm" doc phrases reworded to the
  wheel-required Rust truth — no Python fallback arm exists) + the NIT
  (the ≥2^63 OverflowError honest-limits line in MIGRATION.md #2051).
- The refuter-fold commit (this branch's docs-only head) records the
  COMPLETED refuter round: delta 6 (the decimal128 screen-ON crash→serve,
  planted-dtype-only) + the delta-2/3 wording corrections + the Claim-A
  degenerate-input honest-limits lines in MIGRATION.md #2051 — ledger
  amendment chosen over gate widening (NO code change).

## Spec / decision links

- Decision ID(s): DEC-FEED-RUST-MATERIALIZE-002 (supersedes
  DEC-FEED-RUST-CORE-001's default-OFF/degrade contract #1797 — the crate +
  FFI discipline STAND; closes/moots the named follow-up
  `DEC-FEED-RUST-FLIP-001`)
- MIGRATION.md row(s): #2051 (renumbered #1957 → #1960 → #2051 at the train-b tier-2 fold — the first hop at the 2026-07-19 merge-reconcile, main took #1957 for DEC-SWEEP-ONECLICK-PREFLIGHT-ESTIMATOR-001; the
  renumber-at-landing law stands — the DEC-ID is the stable handle per
  DEC-LEDGER-RENUMBER-001)
- Spec section(s) touched: ADR-0020 conversion program (the brutex/feed
  endgame slice); CLAUDE.md header/§1/§3 rotation

## Type of change

- [x] feat — new feature (the Rust-only flip + crate extension)
- [ ] fix / docs / refactor / test / chore / perf

## Golden Rule #12 — 100% standards self-check

### Coverage + tests

- Coverage line+branch 100.00% on all four touched `brutex/` modules
  (`feed_rust.py` 85/40 · `replay.py` 163/42 · `lookahead.py` 69/10 ·
  `series_load.py` 89/30) on their ci.yml gates
- Property: `tests/property/test_feed_rust_parity.py` retains the DELETED
  oracle loop VERBATIM as the frozen in-test reference (the tf_resampler
  frozen-oracle idiom) and hypothesis-differentials the unified path against
  it (bars str()-level, exception triple, log-sequence equality); cargo
  differential proptest pins strict ≡ frozen scan over the all-finite plane
- Golden: frozen bxsnap2 parity + snapshot-hash goldens, fixtures
  BYTE-UNTOUCHED (858 passed / 126 skipped)
- Bench: linearity + ratio gates green; no speedup claimed (ADR-0020)

### Bug fixes

- N/A — feat, not fix (the two doc-honesty rewords are the folded review
  MINOR, docstring-only, zero executable change)

### Type / lint / security

- `mypy --strict brutex` — 0 new (495 files; the one `cma` import error is
  the documented env-only baseline)
- `pyright` — 0 errors on touched files
- `ruff check` + `ruff format --check` clean
- No new copy-paste — the materialization helpers keep the
  run-identity-critical expressions in ONE place

### Performance

- No new O(n) where O(1) was achievable — the barrier stays O(1)-per-op +
  O(1)-memory-in-record=False (now integer comparisons in Rust + one constant
  FFI hop); the strict scan is O(n_bars) per launch exactly like the deleted
  oracle
- No pandas; no new global mutable state

### Observability

- Every refusal is a loud typed error (FeedError / ValueError /
  FuturesSeriesError) with the DEC named; structlog only, no `print`
- No new metric / CRITICAL condition

### Documentation

- NumPy-style docstrings on public symbols; the folded MINOR removed the
  three stale "development-stage Python arm" phrases (no Python arm exists)
- MIGRATION.md #2051 + CLAUDE.md header/§1/§3 rotation updated
- No silent scope expansion — resampler retirement is the named follow-up

### No hallucination (Golden Rule #1)

- Every Groww/NSE factual claim carries its DEC: 2020 data floor =
  `GROWW_HISTORY_FLOOR` (DEC-PULL-FLOOR-2020-001, MIGRATION.md #722); the
  fixed +05:30 IST timestamp law per DEC-TIMESTAMP-IST-SINGLESOURCE-001
  (#1929); the bad-tick screen law per DEC-SWEEP-DATA-EDGE-GUARDS-001
  (#1616); no invented rate/threshold/constant

## Test plan — gate evidence (all executed)

- [x] cargo: **61–71 green** `--release --locked` (10 lookahead + 12
      materialize + 16 series + 26 scan + 4 scan proptest + 3 digest parity);
      fmt + clippy `--all-targets` (default AND `--features python`)
      `--locked -D warnings` clean
- [x] Feed family: **237 passed unmodified** on the deleted tree
- [x] Seam + parity suites (re-run at the folded head `1f9ac5b`):
      **150 passed**
- [x] ci-exact coverage gates (re-run at the folded head): lookahead
      **100.00%** (27 passed) · series_load **100.00%** (40 passed)
- [x] Golden family: **858 passed / 126 skipped**, fixtures byte-untouched
- [x] Engine unit subset: **4633 passed**
- [x] `ruff check` + `ruff format --check` clean; `mypy --strict` 0 new;
      pyright 0 errors on touched files
- [x] `scripts/ci/zplus-pr-guard.sh` + `definition-of-ready-guard.sh` exit 0
      on this body

---

## Z+ 15-row matrix (100% everything — charter §C)

| Demand | This PR's proof | Real-time check |
|---|---|---|
| 100% code coverage | 4× 100.00% line+branch: `feed_rust.py` (85/40) · `replay.py` (163/42) · `lookahead.py` (69/10) · `series_load.py` (89/30) on their ci.yml gates | ci.yml coverage gates |
| 100% audit coverage | N/A — no material-action writer touched, no audit-JSONL schema change; the durable record is MIGRATION.md #2051 | audit-chain CI gate unchanged |
| 100% testing coverage | unit + integration (seam suites 150 at the folded head; feed family 237) + property (frozen-oracle hypothesis differential + cargo proptest) + golden (858/126, byte-untouched fixtures) + bench (linearity + ratio) + cargo (61–71 `--locked`); mutation via the mutation.yml pair wiring | pytest + cargo + mutation.yml |
| 100% code checks | ruff + ruff-format clean; `mypy --strict` 0 new (env-only `cma` baseline); pyright 0 on touched files; bandit rides the standing pre-push/CI gates | pre-push hooks + ci.yml |
| 100% code performance | no speedup claimed (ADR-0020 banner); barrier O(1)-per-op preserved; strict scan O(n_bars) like the deleted oracle; bench gates green | pytest-benchmark |
| 100% monitoring | loud typed refusals replace every former silent degrade token; structlog events on refusal paths | grep brutex/feed_rust.py |
| 100% logging | structlog only; the WARN materialization (`_emit_warn`) keeps the exact event bytes — log-sequence equality property-pinned | grep -r structlog |
| 100% alerting | N/A — no new CRITICAL condition (loud typed raises reach the existing pull-halt/job handling unchanged) | brutex/observability/ unchanged |
| 100% security | no secret/network surface touched; env-flag parse fail-loud; crate deps append-only, Cargo.lock updated for the version bump only | pre-push gate |
| 100% security hardening | refuse-poison `validate_strict_scan` — a poisoned wheel can only refuse, never serve; no new dishonest pragma | grep noqa |
| 100% bug fixing | feat not fix; the review's ONE non-blocking MINOR + NIT folded at commit `1f9ac5b` (doc honesty — no Python arm exists) | review record below |
| 100% scenarios covering | the 6 ledgered behavior-delta failure modes (wheel-absent, null columns, envelope, retired flags, >2^53 residue — each pinned; delta 6 is refuter-EXECUTED + ledgered, planted-dtype-only, no gate widening by disposition) + MemoryError/KI/SystemExit raw re-raise | pytest -q battery + #2051 |
| 100% functionalities covering | every new public symbol (8-symbol crate surface + `require_feed_kernel`/`require_materialize_flags`/`validate_strict_scan`) driven by the rewritten seam suites | pre-push gate |
| 100% code review | 4-agent hostile review evidence below | this section |
| 100% extreme check | the frozen in-test oracle differential + byte-untouched goldens + the append-only crate law (frozen `scan_bars` registrations untouched) are the mechanical anti-regression rails | every commit |

## Z+ 7-row resilience matrix (charter §C)

| Demand | Honest envelope | This PR's proof |
|---|---|---|
| Zero ticks lost | N/A — BruteX is backtest-only (Track 1) | confirmed — no ingest surface touched |
| WS never disconnects | N/A — no WebSocket in Track 1 | confirmed |
| Never slow/locked/hanged | the kernel functions are pure CPU (no I/O in the crate); one constant FFI hop per barrier access; no new lock | bench linearity + ratio gates green |
| QuestDB never fails | N/A — BruteX uses DuckDB + LMDB; neither touched | confirmed |
| O(1) latency | barrier O(1)-per-op + O(1)-memory-in-record=False preserved (integer comparisons in Rust); the scan is O(n_bars) per launch — stated, never hidden (the #1797 envelope) | bench gates |
| Uniqueness + dedup | N/A — no dedup key change; run identity untouched (bxsnap2 goldens byte-identical) | MIGRATION.md #2051 |
| Real-time proof | the Real-run proof command above against the REAL built wheel | this PR body |

## Z+ 7-layer audit

| Layer | This PR's status |
|---|---|
| DETECT | the frozen in-test oracle (the deleted loop retained VERBATIM in `tests/property/test_feed_rust_parity.py`) differentials the unified path — any Rust↔frozen-law drift fails the property suite; byte-untouched golden fixtures detect any materialized-byte drift |
| VERIFY | 237 feed family + 150 seam/parity + 858/126 golden + 4633 engine subset + 61–71 cargo verify the flip end-to-end; ci-exact coverage gates 100.00% on all four touched modules |
| RECONCILE | the deleted two-arm lattice is replaced by refusal laws — a SET legacy flag refuses loudly naming the DEC (never a silent behavior change); pre-existing raw-TypeError escapes reconciled into the typed corrupt-column ValueError (delta 2, disclosed — refuter-corrected: the one null-volume-on-a-screen-skipped-row sub-case was a silent SERVE, not a raw escape; refuse-harder either way) |
| PREVENT | the append-only crate law (frozen `scan_bars` + registrations byte-untouched, pinned) + `validate_strict_scan`'s refuse-poison gate + the retired-flag refusal prevent silent re-introduction of a degrade arm |
| AUDIT | MIGRATION.md #2051 is the durable decision record (incl. the folded honest-limits addendum + the refuter-round record: A unrefuted/19,933 ops · B letter-refuted → delta 6 + two wording corrections · C stands · disposition ledger-amendment); CLAUDE.md header/§1/§3 rotated; no runtime audit-JSONL change |
| RECOVER | revert the branch commits → the exact #1797 state restored (crate 0.1.0, default-OFF flag, live Python oracle); no schema/lake/run-identity change to unwind |
| COOLDOWN | the flip is refusal-law instead of burn-in per the operator's compressed no-soak cycle (recorded in #2051); the frozen goldens + oracle differential are the standing post-merge net |

## 4-agent hostile review

Honest framing (Golden Rule #10): the review evidence for this lane is the
club-fold four-eye protocol —

| Agent | Lens | Findings | Severity |
|---|---|---|---|
| Implementation worker (lane T17) | build + gate execution + ledger | the branch's 4 commits + the executed gate battery (Real-run proof) | — |
| Independent hostile reviewer | correctness + security + O-complexity honesty + coverage honesty | **verdict SHIP** — ONE non-blocking MINOR (three stale "development-stage Python arm" doc phrases claiming a nonexistent fallback) + one NIT (≥2^63 barrier index → OverflowError at the FFI edge, unreachable in practice) — **both folded at commit `1f9ac5b`**, gates re-run green | 0 blocking |
| Independent refuter | adversarial re-execution / completeness | **COMPLETED + FOLDED (docs-only ledger amendment)** — Claim A (P-06 barrier parity) UNREFUTED over **19,933 differential ops** (37 crafted sequences × both record modes + 4,000 seeded random sequences; full observable-state compare per op; the ≥2^63 delta confirmed SAFE-direction — raise-vs-raise-differently, the dangerous oracle-raise/new-silent-pass direction never occurred). Claim B (materialization bit-parity) **REFUTED IN THE LETTER** on ONE hostile-dtype-only shape (the decimal128 screen-ON crash→serve — ledgered as delta 6) + two wording corrections (deltas 2/3), canonical/production plane parity-clean over **~3,000 executed cases** (494 crafted + 2,500 hypothesis, screen ON/OFF, warn-sequence + message-byte compare). Claim C (the >2^53 residue is unreachable from a healthy lake) **STANDS** — hostile 2dp-snapped ≫2^53 prices through the REAL ParquetBarSink→ParquetWriter→Catalog.get_bars chain read back canonical float64; parity confirmed on all three lakes, both screen arms. Disposition: ledger amendment over gate widening (planted-only shape, strictly better-behaved new direction); the Decimal-price junk-typed-gate refusal stays the backticked unminted follow-up `DEC-FEED-DECIMAL-PRICE-GATE-001` | 0 blocking |
| Coordinator club fold | fourth eye — slotting, union-file weave, renumber-at-landing | this PR lands ONLY via the club fold; #2051 re-verified at landing | — |

Review evidence: MIGRATION.md #2051 (per-item record, incl. the refuter-round
record + disposition) + the fold commit `1f9ac5b` message + the refuter-fold
docs-only commit (this branch's head).

---

## Honest envelope (Golden Rule #10)

- ✅ REAL: the Rust-only flip with run identity byte-preserved by
  construction (frozen bxsnap2 + snapshot-hash goldens pass, fixtures
  byte-untouched); the crate extension append-only (frozen `scan_bars` +
  registrations untouched); 4× 100.00% line+branch; cargo green `--locked`;
  the review MINOR + NIT folded and gates re-run green at `1f9ac5b`.
- ✅ REAL (ADR-0020 banner, verbatim): expected payoff ≈ 0 — conversion is
  for uniformity, not speed; no speedup claimed or measured.
- 🟡 PARTIAL: the 6 ledgered behavior deltas (wheel-absent loud FeedError ·
  null consulted columns typed ValueError — refuter-corrected wording ·
  out-of-envelope loud refusal — refuter-corrected wording · SET falsy/junk
  flag refusal · >2^53 decision-plane f64 residue · the refuter-found
  decimal128 screen-ON crash→serve) — each hostile/planted-input-only,
  sanctioned + disclosed in #2051; the seam
  suites' old pins pinned the deleted degrade lattice and were REWRITTEN to
  the wheel-required contract (the ONE sanctioned pin-rewrite class of this
  lane, disclosed); the `_expected_bar_time_in_window` walk-COST delta
  (verdict-equivalent, every day resolved before the kernel call where the
  deleted walk stopped early) is disclosed in #2051; barrier indices ≥ 2^63
  raise OverflowError at the FFI edge (loud, never a silent look-ahead;
  unreachable in practice — with the refuter's Claim-A degenerate-index
  observations, each stricter-or-equal, ledgered in #2051).
- 📋 ROADMAP: `resampler.py` retirement =
  `DEC-FEED-EPOCH-RESAMPLER-RETIRE-001` (named follow-up, RECOMMENDED for
  deletion); series_load orchestration/calendar I/O stays Python (honest
  residue); the refuter round's outcome is FOLDED (delta 6 + the wording
  corrections, #2051 — ledger amendment over gate widening); a Decimal-price
  refusal in the junk-typed gate stays available if the club wants it — the
  backticked unminted follow-up `DEC-FEED-DECIMAL-PRICE-GATE-001`.

Anti-pattern check: every claim above cites the executed gate evidence (Test
plan) or the ledger entry (MIGRATION.md #2051); no claim is based on a
hypothetical artifact.

## Operator action required (post-merge)

- None at runtime for existing dev/CI environments (ci.yml installs the
  wheel; the property job's install subset gains it in this PR). A bespoke
  environment running feed materialization WITHOUT the `brutex_feed` wheel
  must `maturin develop`/install it — the loud `FeedError` names the exact
  remediation (this is behavior delta 1, disclosed).

---

*Generated by [Claude Code](https://claude.ai/code/session_01YBXKsAsLFDJ3wjxzikYZ8V)*
