# Pull Request — BruteX

> **Merge-Readiness Charter (DEC-MERGE-GUARANTEE-001):** this PR obeys
> `docs/MERGE_READINESS_GUARANTEE.md` — the §3 gate matrix + §4 Definition of
> Done. Never merge over a red or pending gate. Per Golden Rule #10, partial /
> roadmap / out-of-scope rows are disclosed in the Honest envelope below.

## Before / After (plain language)

**Before:** three resample tails ran ONLY in Python — the daily 1-day
derivation (`fno_daily_resample.resample_1m_to_fno_daily` +
`spot_daily_resample.resample_1m_to_daily`), the F&O session forming-bucket
cutoff (`fno_resample.resample_session_to_tf`), and the 21-TF batch derive
(`tf_resampler.resample_all_21_tfs`). The `rust/brutex-lake` resample core
(G5, DEC-LAKE-RUST-RESAMPLE-001 #1913) covered only the single-TF intraday
path.

**After:** each tail can route through the `rust/brutex-lake` kernel behind
default-OFF flags — the NEW independent `BRUTEX_LAKE_RUST_RESAMPLE_DAILY` for
the daily arm, and the EXISTING `BRUTEX_LAKE_RUST_RESAMPLE` for the
session + batch faces — with the verbatim Python bodies retained as live
parity oracles (burn-in bit-compare + ship-the-oracle autorevert). **Flag OFF
(the default) is byte-identical — bomb-spy-pinned at every seam — so nothing
changes for production today.** Closes the #1913-named follow-ups (daily
residues · session-fold · batch adoption).

> **ADR-0020 honesty banner, verbatim:** "expected payoff ≈ 0 — conversion is
> for uniformity, not speed." The seams ADD an IPC round-trip per call; no
> speedup is claimed or measured.

## How

- **Crate (additive; version stays 0.1.0, Cargo.toml + Cargo.lock
  byte-untouched):** `src/resample.rs` gains `filter_forming_buckets` (the
  rem_euclid minute-of-day mirror of the Python forming-bucket law,
  calendar-correct pre-epoch) + `resample_session_ipc` (resample + forming-cut
  in ONE crossing); NEW `src/resample_daily.rs` — `resample_daily_frame` /
  `resample_daily_ipc` (G5 375-min core → `< before` IST-day filter →
  midnight-IST restamp cast `Datetime(us, UTC)` → OHLCV(+OI when oi_aware)
  select; Greeks overlays dropped — daily bars never carried them). 14 new
  cargo tests (5 session + 9 daily), 81 total.
- **Facades:** `lake_rust_resample.py` refactored onto shared guard helpers
  (every pinned source literal preserved verbatim) + `resample_session_frame_via_rust`
  (getattr-at-call-time session face — a pre-T11 wheel degrades only the
  session face, no latch; a DISCLOSED deviation from the PR-2 all-or-nothing
  `_CRATE_API` precedent) + `resample_all_via_rust` (ONE crossing per 21-TF
  derive; any degrade/mismatch ⇒ the WHOLE dict from the oracle, never a
  partial mix). NEW `brutex/data/lake_rust_resample_daily.py` — independent
  flag, OWN latch/burn-in/warn-once state (a daily autorevert never disables
  the intraday seam — pinned), own `_CRATE_API_DAILY` + polars-version
  lockstep guard, ints-only FFI (`before` as days-since-epoch), 16-call
  burn-in at dtype-exact frame strength with ship-the-oracle autorevert.
- **The four seams (flag OFF = the verbatim pre-T11 bodies, extracted
  token-for-token where routed):** `tf_resampler.resample_all_21_tfs` batch
  route; `fno_resample.resample_session_to_tf` session route with the
  composite oracle; `fno_daily_resample` frame-via-facade + per-row
  materialization mirroring the OFF arm's `Decimal(str(x))` law exactly;
  `spot_daily_resample` vectorized body extracted as OFF-body-AND-oracle.
  `spot_resample.derive_intraday_tfs` inherits transitively (untouched,
  spy-pinned); `feed/resampler.py` NOT touched (ADR-EXEMPT oracle, #1492 G3).

## Evidence (measured, executed)

| Gate | Result |
|------|--------|
| Real-wheel parity suite (`test_lake_rust_resample_daily_parity.py`) | 30 passed — CandleBar strength incl. Decimal reprs; session-cutoff parity; batch 21-TF parity + ONE-crossing count pin; naive-TZ law pins |
| Facade suite (`test_lake_rust_resample.py`) | 83 passed; `lake_rust_resample.py` 100.00% line+branch (226/78) |
| Daily facade suite (`test_lake_rust_resample_daily.py`) | 60 passed; `lake_rust_resample_daily.py` 100.00% (129/38) |
| Seam suites (tf batch / fno session / fno daily / spot daily) | 26 passed |
| Consumer static gates (21c · 21c-fno-daily · 21c-fno-resample) | 100.00% each (62/28 · 46/12 · 32/6); `spot_daily_resample.py` 100.00% over its discovery set (212/56, 99 passed) |
| mutmut (`lake_rust_resample_daily.py` pair, wired in mutation.yml + wiring-pinned) | 110/110 KILLED · 0 survived · 0 timeout · 0 suspicious |
| Suffix-independence (P-06 posture) | 13 passed unmodified |
| Coupled goldens | 7 passed unmodified |
| Frozen sweep-run golden | 19 passed unmodified (run identity untouched) |
| Vocab golden | 6 passed unmodified |
| Whole-prebuild sha256 A/B | derived + 1day partition bytes sha256-EQUAL daily-ON vs OFF AND both-flags-ON vs OFF, incl. the FNO FUT daily writer path (round-1 fold) |
| Flag-OFF byte-identity | bomb-spy pinned at every seam |
| cargo | fmt --check clean · clippy --all-targets --locked --release -D warnings clean · test --release --locked 81 passed |
| Static | ruff + ruff-format clean (13 files); mypy --strict clean on the 6 touched brutex modules; pyright 0 errors on all touched files |

## Review-round record

- **R1:** 3× SHIP — findings folded (cargo-test honesty recount 15→14; the
  MEASURED naive-TZ law correction — divergence needs a local offset > +09:15,
  pinned under TZ=Australia/Brisbane with a three-arm agree pin under
  UTC/Kolkata/New_York; FNO FUT daily prebuild A/B extension).
- **R2:** folded (docs-only parity-count sync 26→30; the FNO tzinfo-OBJECT
  NIT recorded no-action — proven invisible: CandleBar equality + sha256
  byte-identical parquet).
- **R3 + R4:** consecutive CLEAN — protocol complete.

## Z+ 15-row matrix (100% everything — charter §C)

| Demand | This PR's proof |
|---|---|
| 100% code coverage | facade 100.00% line+branch (226/78) · daily facade 100.00% (129/38) · consumer static gates 21c/21c-fno-daily/21c-fno-resample 100.00% each · `spot_daily_resample.py` 100.00% (212/56) |
| 100% audit coverage | N/A — no new material-action surface; the derived-partition writers' existing audit machinery is untouched |
| 100% testing coverage | unit (facade 83 + daily 60) + integration (seams 26 + parity 30, real wheel) + property (suffix-independence 13 unmodified) + golden (coupled 7 · sweep-run 19 · vocab 6, all unmodified) + bench (existing O(1) gates unmodified) + mutation (mutmut 110/110 KILLED) + cargo 81 |
| 100% code checks | ruff + ruff-format clean (13 files); mypy --strict clean (6 touched modules); pyright 0 errors; cargo fmt + clippy `-D warnings` clean |
| 100% code performance | no new hot path — flag OFF byte-identical (bomb-spy-pinned); ON adds one disclosed IPC hop, no speedup claimed (ADR-0020 banner) |
| 100% monitoring | warn-once degrade + engaged/autorevert structlog events on both facades (the G5/#1914 event family, extended) |
| 100% logging | structlog only; no print (ruff gate) |
| 100% alerting | N/A — no new CRITICAL condition; autorevert is a loud ERROR event per the facade precedent |
| 100% security | bandit/gitleaks surfaces untouched; ints-only + Arrow-IPC FFI, no new input parsing from untrusted sources |
| 100% security hardening | no new noqa/pragma; panic-belt + blanket degrade keep a poisoned crate degrade-only |
| 100% bug fixing | N/A — feat; R1 findings folded in 481b31b94 with their own pins |
| 100% scenarios covering | failure/degrade matrix in Definition of Ready (wheel absent/stale · junk flag · burn-in mismatch · panic · partial-batch refusal) — each tested |
| 100% functionalities covering | every new public facade/crate face driven by its suite (83 + 60 + 26 + 30 + cargo 81) |
| 100% extreme check | mutmut zero-survivor + the whole-prebuild sha256 A/B (daily-ON and both-ON vs OFF) as the ratchet |

## Z+ 7-row resilience matrix (charter §C)

| Demand | Honest envelope |
|---|---|
| Zero ticks lost | N/A — BruteX is backtest-only (Track 1) |
| WS never disconnects | N/A — no WebSocket in Track 1 |
| Never slow/locked/hanged | no new locks/threads; facade latch cells mirror the G5 precedent; degrade paths bounded + warn-once |
| QuestDB never fails | N/A — BruteX uses DuckDB + Parquet + LMDB |
| O(1) latency | unchanged class — O(rows) resample either arm; existing O(1) bench gates pass unmodified |
| Uniqueness + dedup | derived-partition write path untouched (keep-first laws unchanged); parity proves byte-equal output |
| Real-time proof | the Evidence table above — every figure measured in-session |

## Z+ 7-layer audit

| Layer | This PR's status |
|---|---|
| DETECT | burn-in bit-compare per process + the non-required lake-rust-parity lane (T11 paths + run step) + bomb-spy flag-OFF pins |
| VERIFY | 30-test real-wheel parity (CandleBar + Decimal reprs) + whole-prebuild sha256 A/B over a REAL ParquetBarSink lake (INDEX + FNO) |
| RECONCILE | additive only — flag OFF is the verbatim pre-T11 body (extracted token-for-token); Cargo.lock + crate version untouched |
| PREVENT | mutmut pair wired in mutation.yml (wiring-pinned, 110/110 KILLED) + the ci.yml 21c-family static gates + suffix-independence unmodified |
| AUDIT | MIGRATION.md #1946 + CLAUDE.md header/§1/§3 blurbs + ADR-0020 rows 11–15 carry the record |
| RECOVER | revert the merge commit; runtime kill = leave both flags unset (default) |
| COOLDOWN | ships default-OFF/inert — the soak IS the default-ON flip's own DEC gate (fresh parity-lane run + zero autoreverts) |

## 4-agent hostile review

| Round | Lenses | Verdict / findings |
|---|---|---|
| R1 | 3 independent hostile reviewers (correctness · FFI/degrade honesty · docs/coverage honesty) + builder self-audit | 3× SHIP; findings folded (commit 481b31b94): cargo-test recount 15→14 · the MEASURED naive-TZ law correction (pinned under TZ=Australia/Brisbane + three-arm agree pin) · FNO FUT daily prebuild A/B extension |
| R2 | refuter re-pass | folded (b05b6c5c3): docs-only parity-count sync 26→30; FNO tzinfo-OBJECT NIT recorded no-action (proven invisible — CandleBar equality + sha256-identical parquet) |
| R3 | refuter re-pass | CLEAN |
| R4 | refuter re-pass | CLEAN — two consecutive clean rounds, protocol complete |

## Honest envelope (Golden Rule #10)

- The ADR-0020 banner holds verbatim: expected payoff ≈ 0 — uniformity, not
  speed; each seam ADDS an IPC round-trip and NO speedup is claimed.
- Session face resolves getattr-at-call-time (no latch) — a DISCLOSED
  deviation from the PR-2 all-or-nothing `_CRATE_API` precedent.
- The fno per-row naive-ts `astimezone` local-TZ dependence is pre-existing +
  disclosed (#1946); resilience rows above marked N/A are honest
  non-applicability, not coverage.

## Definition of Ready (plan-first gate — DEC-DEFINITION-OF-READY-001)

### Scenarios covered
- Happy: flag-ON daily / session / batch routes serve crate results
  bit-verified against the oracle (30-test real-wheel parity corpus).
- Edge: pre-epoch session labels (1969-12-31 cargo pin); naive-ts frames
  under 4 controlled TZs; pre-T11 wheel (session face degrades alone);
  polars-version drift (lockstep guard refuses); month/DST-free offsets.
- Empty/zero: empty frames + zero-row derives covered in the facade suites.
- Failure: crate raise/panic/poison → blanket degrade to the oracle with
  warn-once; burn-in mismatch → ship-the-oracle autorevert (independent
  latches — a daily autorevert never disables the intraday seam);
  MemoryError re-raises raw.
- Concurrent: burn-in/latch state exercised by the existing thread-discipline
  pins of the G5 facade suite (append-only extension, existing tests
  untouched).

### Failure modes
- Wheel absent/stale → named warn-once + oracle (session face: getattr-time
  `crate_stale`, no latch — disclosed deviation).
- Junk flag value → loud-OFF once. Burn-in mismatch → autorevert latch,
  oracle values shipped. Crate panic → panic-belt degrade. Any batch-face
  degrade → the WHOLE 21-TF dict from the oracle, never a partial mix.

### Real-run proof
```
.venv/bin/python -m pytest tests/integration/test_lake_rust_resample_daily_parity.py -q
# 30 passed — real maturin wheel; incl. whole-prebuild sha256 A/B over a REAL
# ParquetBarSink lake (materialize_lake_timeframes(include_daily=True), INDEX + FNO)
cargo test --release --locked --manifest-path rust/brutex-lake/Cargo.toml  # 81 passed
```

### Rollback plan
Both flags are default-OFF; production is byte-identical today. Rollback =
revert the merge commit (no schema/lake/identity change; Cargo.lock and crate
version untouched). Runtime kill: leave `BRUTEX_LAKE_RUST_RESAMPLE_DAILY` /
`BRUTEX_LAKE_RUST_RESAMPLE` unset.

### Out of scope
- Any default-ON flip (needs its own DEC citing a fresh parity-lane run +
  zero autoreverts — the standing flip law).
- `feed/resampler.py` — EXCLUDED: retained as the ADR-EXEMPT oracle + live
  `INTERVAL_SECONDS` home (5 importers); its delete is #1947-earmarked
  (the backticked follow-up id `DEC-FEED-RESAMPLER-DELETE-001`; unminted —
  no ledger entry until that work ships) behind this PR.
- The fno per-row naive-ts `astimezone` normalization (pre-existing,
  disclosed named follow-up in #1946).
- No engine/scope/vocab/rate/lake-schema change — ENGINE UNCHANGED
  (NIFTY/BANKNIFTY/SENSEX); F&O lake stays REFERENCE_ONLY.

---

## Summary

- T11 of the ADR-0020 conversion slate: the daily-residue, session-cutoff and
  batch-face resample tails gain default-OFF Rust routes on `rust/brutex-lake`
  with Python retained as live parity oracles; flag OFF byte-identical.
- Closes the #1913-named follow-ups; run identity untouched (frozen sweep-run
  golden 19 passed unmodified; the whole-prebuild sha256 A/B is the
  load-bearing proof since the 1day partition is the PDH/PDL anchor).

## Spec / decision links

- Decision ID(s): DEC-LAKE-RUST-RESAMPLE-DAILY-001 (parent roadmap
  DEC-RUST-DATALAKE-IO-ROADMAP-001 / ADR-0020)
- MIGRATION.md row(s): #1946 (verified FREE on live main at PR-open — main max
  #1932; renumber-at-landing law applies if taken)
- Spec section(s) touched: ADR-0020 rows 11–15 status; CLAUDE.md header + §1 +
  §3 active-branch blurbs

## Type of change

- [x] feat — new feature
- [x] test — test-only (suites + parity corpus)
- [x] chore — tooling / CI / build (ci.yml gates, mutation.yml pair, parity lane)
- [x] docs — documentation (MIGRATION.md #1946, CLAUDE.md, ADR-0020)

## Golden Rule #12 — 100% standards self-check

### Coverage + tests
- [x] Coverage line 100% on changed `brutex/` files
- [x] Coverage branch 100% on changed files
- [x] Property tests present (suffix-independence 13 passed unmodified)
- [x] Golden fixtures (coupled goldens 7 · frozen sweep-run 19 · vocab 6 — all unmodified)
- [x] Benchmark N/A new hot path (flag-OFF byte-identical; existing O(1) gates unmodified)

### Bug fixes
- [x] N/A — feat, not fix (review-fold commit 481b31b94 carries its own pins)

### Type / lint / security
- [x] `mypy --strict` clean on the 6 touched brutex modules
- [x] `pyright` 0 errors on all 13 touched files
- [x] `ruff check` + `ruff format --check` clean
- [x] No new copy-paste (shared helpers imported, §11 COMMON; public aliases per the lake_rust_render precedent)

### Performance
- [x] No new O(n) where O(1) was achievable (same O(rows) resample class; one IPC hop added, disclosed)
- [x] No pandas
- [x] No new global mutable state beyond the facade's precedent-sanctioned module latch cells (mirrors G5/#1914)

### Observability
- [x] Warn-once degrade events + engaged/autorevert logs (structlog); no print
- [x] No new metric / CRITICAL condition

### Documentation
- [x] NumPy docstrings on public symbols
- [x] MIGRATION.md #1946 + CLAUDE.md blurbs + ADR-0020 rows updated
- [x] No silent scope expansion

### No hallucination (Golden Rule #1)
- [x] Every figure above is measured in-session (progress log + commit messages); UNVERIFIED items named as such

## Test plan

- [x] `pytest` — facade 83 + daily 60 + seams 26 + parity 30 all green (real wheel)
- [x] Existing-suite sweep UNMODIFIED: 322 across the resampler/daily families at the seam commit; 336 + 103 broader sweep
- [x] `cargo test --release --locked` 81 passed; fmt + clippy clean
- [x] mutmut 110/110 KILLED

## Operator action required (post-merge)

- None. (Sequencing note: this PR slots BEHIND the in-flight mega-club / G10
  landing and will be merged-forward over main when they land.)

---

CI will enforce the standard gate battery; any red gate BLOCKS this PR.