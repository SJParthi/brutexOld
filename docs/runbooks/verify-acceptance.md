# Runbook — `brutex verify-acceptance` (the operator acceptance verifier)

> DEC-OPERATOR-ACCEPTANCE-VERIFY-001, MIGRATION.md #1756.
> ONE command that verifies the 7 acceptance classes on the REAL lake and
> writes a hash-chained evidence report (tamper-EVIDENT against accidental
> damage — see the §5 honest limit). Read-only on the lake (instruments.duckdb
> is snapshotted into memory via a short-lived read-only attach, never
> migrated); the verifier's OWN writes land ONLY under
> `<lake_root>/acceptance/`. TWO disclosed serving-path residuals (the
> movers/latency checks exercise the REAL in-process serving path): (1) it
> opens `registry.duckdb` exactly as the production dashboard does (a
> schema-MATCHED open is a no-net-bytes re-stamp; a schema-DRIFTED registry
> ABSTAINS `registry_schema_drift` BEFORE the route runs, so the open's
> latent drop+recreate can never fire under the verifier; an ABSENT registry
> is never created — the route falls back before any open); (2) it may
> refresh the `<lake_root>/.manifest/` discovery caches exactly as the
> production dashboard does (a benign self-healing rebuild, results-safe —
> round-3 disclosure, named in the movers/latency limits too). ZERO vendor
> calls, ever (AST-pinned — the package-wide pin in
> `tests/unit/test_acceptance_independent_fence.py` refuses any
> `brutex.adapters.*` import beyond the sanctioned planning/constants
> surfaces and any client-construction token; round-4 C-F2) — the pull lock
> is never taken. A symlinked `<lake>/acceptance` dir — or a symlinked
> `acceptance/audit` child dir / `acceptance.jsonl` chain file (round-4
> A-F1) — refuses the WHOLE run (SUSPECT) before any write — audit chain
> included. ONE run per lake at a time: the acceptance single-flight flock
> (`<lake>/.locks/acceptance_singleflight.lock`, the house pull/nightly
> `_flock_singleflight` shape; round-4 B4-1) refuses a SECOND concurrent
> run loudly — SUSPECT exit 2 naming the holder — because overlapping runs
> would interleave appends into the single-writer audit hash chain and
> corrupt it permanently.

## 1. The one command

```bash
brutex verify-acceptance                # all 7 checks, teeth smoke, default sampling
```

Defaults resolve with zero prompts: lake root via `$BRUTEX_LAKE_ROOT` /
`~/.brutex/lake`; sampling seed = the IST report date (deterministic daily
rotation — a nightly cron covers all ~750 datasets in ~⌈750/12⌉ nights).
Scope bounds (honest, per surface): the uniqueness census scans the SAMPLED
datasets × the last 6 months per-coordinate (+ one filename-bounded
`options/` tree scan — never a whole-`bars/`-tree enumeration); the tf 1day
compare and every sanity read are month-bounded; the INTRADAY freshness seam
(`lake_derived_freshness_verdicts`) reads full stored history — seam-owned,
disclosed in the report's limits. Rotating-log / ingest-audit evidence reads
are bounded 16 MiB seek-from-end tails; the coverage ledger read is bounded
16 MiB too and pruned on save to the live dataset population. The engine set
is ALWAYS in scope, exactly this (round-3 code/docs match, C-F1): the
`unique.engine` line scans NIFTY/BANKNIFTY/SENSEX **+ INDIAVIX + every
`NSE-NIFTY-*-FUT` engine-track contract dir (ADR-0012)** full stored history
(each futures contract holds ≤60 days of 1m + its derived TFs — bounded),
and tf FORCE-includes the engine futures contracts in its selection every
run (never left to the rotation; the tf 1day compare stays month-bounded —
`--full` lifts). The
sampled F&O contract fill (6 per run) seed-ROTATES like the spot fill, and
contracts join the coverage ledger's population (capped 5,000 keys — a
~10^5-contract lake's truncation is disclosed as
`fno_population_truncated`). The instrument master is read as a short-lived
in-memory SNAPSHOT (never migrated, never written, no file handle held while
the in-process app runs — the production spot-drift ladder stays live; a
locked/older-schema store reads a named ABSTAIN).

## 2. What each check proves (and honestly cannot)

| check | proves | permanent limits |
|---|---|---|
| unique | census scans (`scan_lake_invariants`, per sampled coordinate) clean AND complete — a skipped file FAILs; `unique.engine` full-history | sampled × month-bounded by default (`--full` = the whole-lake scan) |
| tf | 19 intraday TFs fresh (the sweep-preflight seam) + the 1day re-derive compare (Muhurat sessions skipped PER-DAY, one count per distinct IST date, never a whole-history abstain) | `tf.oi` ABSTAINs until follow-up F2 (OI not value-compared); intraday seam reads full history; **stored 1day bars on Muhurat session dates are NOT verified** — corruption on those ~6 dates is invisible to tf.daily (the dates are named in `muhurat_days_unverified`) |
| pull | re-plan over each MERGED covered span (production expiry-capped contract windows) mints ZERO new fetchable windows for DEFAULT-law series; keep-first counter laws; SUCCESS rows have backing files (sampled) | plan-level convergence, not vendor determinism; presence PASS is sample-scoped; a deleted ledger row OR a never-planned window that splits a span is invisible to replan (the sanity interior-hole detector owns the missing data); `open_by_design` folds unsettled rows + out-of-coverage replanned keys incl. a future-dated phantom past the ledger max end — the hard double-fetch FAIL bites past-dated phantoms only; a non-default coverage shape (explicit-dates F&O rows, overlapping-anchor ranges, in-coverage re-tiling mismatches — indistinguishable from planner re-tiling drift in ledger evidence) reads `non_default_window_shape`, never a phantom FAIL — routed series are counted on the report.txt check line; a replanned key ESCAPING the covered spans still hard-FAILs |
| rate | constants pin · market-hours conformance (#1692; genuine vendor-attempt records only — journal heartbeats never count) · bridge/log point evidence (bounded tails) | `rate.per_call_history` = PERMANENT ABSTAIN — no per-call record persists (F3 needs an operator ruling); live probe = `brutex doctor --live --stage 13` |
| movers | page cells == the FENCED independent recompute (raw parquet + hand math; literal `glob=False` reads); moneyness grammar = the FULL production vocabulary incl. the honest `?`/`—`/bare-ITM/OTM sentinels | rank verified within served rows; Greeks presence-only; the options leg exercises the REAL serving path, which opens `registry.duckdb` as production does (schema-drift ⇒ named ABSTAIN before the route; matched schema = no net bytes) |
| sanity | price sanity · bad-tick (4x screen, #1616 escapes named) · tick grid · session windows · interior holes · VIX presence · completeness (read-only master) | heuristic screens stay heuristics; reads month-bounded by default |
| latency | measured first-call+warm ms vs on-box budgets (warm best-of-N is the only gated number) | absolutes, not the CI 1.1x tier, not a proof; first-call is NOT claimed cold |

## 3. Teeth (why you can trust a PASS)

Every run starts with a <10 s smoke: a twin lake is built with the REAL
writers, 3 poisons are planted (duplicate row, x10-scaled stored bar, a
perturbed movers cell), and the real checks must catch all 3 with zero false
FAILs on the clean twin. Any miss ⇒ `TEETH_FAILED`, exit 2, and every
real-lake verdict is WITHHELD ("nothing below is trustworthy").

```bash
brutex verify-acceptance --self-test-only   # the FULL 20-poison battery + table
brutex verify-acceptance --self-test        # full battery, then the real run
brutex verify-acceptance --no-self-test     # skip (report carries TEETH_SKIPPED)
```

The full battery also runs in CI on every merge
(`tests/integration/test_acceptance_selftest.py`), including the
comparator-inversion tooth. Round-4 (B4-3) additions: the battery twin
carries one NON-engine CASH symbol so `unique.census` and `unique.engine`
each have a tooth of their own (`census_nonengine_dup` /
`engine_oldmonth_dup` — an always-PASS sabotage of either leg now flips the
battery), and the pull poisons detect through their REAL comparators
(`net_new_gt_bars`/`counter_invariant` no longer ride the fixed B4-2
presence false positive — a net_new_law sabotage flips the battery,
test-pinned). Scope honesty: the movers teeth (smoke S3 + battery
`movers_cell`/`prevclose_hole_unstamped`) drive the comparator SEAM
directly, never the composed `movers.run` wiring — the designated killer
for a comparator-discard wiring sabotage is the unit gate
(`tests/unit/test_acceptance_checks_movers.py::TestMoversRealWire::`
`test_tamper_tooth_volume_cell`, which drives the composed check against a
mutated real wire). `latency` budget-compare and `rate.constants` carry NO
battery poison BY DESIGN (a latency "poison" would be a wall-clock flake;
the constants pin is itself the tooth — a drifted constant fails it).

Round-5 (F2) additions — the battery grew 17 → 20 so more real check legs
are toothed ON-BOX (the operator's trust demo): `tampered_1day_close`
(tf.daily VALUE-equality — a corrupt derivable 1day close), `bad_tick_x10_1m`
(`sanity.bad_tick` — a x10-scaled 1-minute close) and `preopen_bar`
(`sanity.session_window` — an 08:00-IST pre-open bar); poison 5 was renamed
`orphan_1day_stored` (it tests the tf.daily NOT-derivable path, not the
forming branch). TWO legs stay honestly UNIT-GATE-KILLED (a robust twin
poison is impractical, so the killer is the named unit test that drives the
COMPOSED check against the sabotage): tf.daily's FORMING branch
(`ist_date >= before`) is clock- AND month-boundary-dependent →
`test_acceptance_checks_tf.py::TestTfCheck::test_forming_day_stored_fails`
(injected clock); `pull.replan_zero`'s planner-vacuity guard is a CODE
mutation, not data →
`test_acceptance_checks_pull.py::TestPullCheck::test_lobotomized_planner_fails_vacuous`
(+ the positive `test_settled_fixture_replan_zero_and_tail_planned`). The
per-leg tooth roster lives in `selftest.run_full_battery`'s docstring; every
real check leg is either battery-toothed or named-unit-gate-killed there.

## 4. Exit codes (cron-native)

| code | meaning | cron |
|---|---|---|
| 0 | clean (teeth proven or explicitly skipped+recorded) | green |
| 1 | usage error (bad flags, missing lake root) | red |
| 2 | SUSPECT — verifier internal error / a check ERROR incl. `<check>.timed_out` (the hard per-check watchdog, F1) / TEETH_FAILED / report-write failure / evidence-tree symlink refusal / a SECOND concurrent run refused by the single-flight lock (B4-1) | red |
| 3 | ≥1 FAIL finding, verifier healthy | red |
| 4 | inconclusive — non-structural abstains > `--abstain-budget` (3), an abstain streak ≥3, or time-truncated | amber |

Precedence 2 > 3 > 4 > 0. Structural abstains
(`no_per_call_persistence`, `no_per_call_log`, `no_oi_compare_seam`) are
budget/streak-exempt — the standing by-design gaps never go permanently amber.

## 5. The report

```
<lake>/acceptance/<run_id>/report.json    # brutex-acceptance-report-v1 (frozen)
<lake>/acceptance/<run_id>/report.txt     # human render — ABSTAINs FIRST
<lake>/acceptance/<run_id>/evidence/      # per-check spill (+ tf divergence dumps)
<lake>/acceptance/LATEST                  # run-id pointer (atomic)
<lake>/acceptance/coverage_ledger.json    # sampling accumulation
<lake>/acceptance/audit/acceptance.jsonl  # hash-chained trail
```

Cross-check a report against its audit trail:

```bash
brutex audit verify <lake>/acceptance/audit/acceptance.jsonl
sha256sum <lake>/acceptance/<run_id>/report.json   # vs the ACCEPTANCE_RUN_COMPLETE line
```

HONEST LIMIT: the hash chain carries no secret key and lives in the same
operator-writable tree as the report — it detects ACCIDENTAL/casual tampering
and torn writes, but an attacker with lake write access can restate the whole
chain and pass `brutex audit verify`. Off-box anchoring (the backup lane's
immutable store) is what defeats restatement, not this file.

Run-over-run deltas + consecutive-ABSTAIN streaks are carried in the report
chain (previous report via LATEST) — no separate state file. Streaks update
ONLY for sub-checks that actually RAN: a `--checks` subset run or a
TEETH_FAILED run carries every absent sub-check's `(verdict, streak)` cell
forward verbatim in the report's `streak_carry` block, so an interleaved
subset run can never reset another check's streak and suppress the
streak-≥3 ⇒ exit-4 law (round-4 B4-4). `report.txt`
renders ABSTAINs FIRST, every check's + sub-check's honest-envelope `limits`
lines, the routed `non_default_window_shape` series count and the
Muhurat-unverified dates (round 3 — the honest envelope is readable without
opening report.json).

`--out DIR` reports are OFF-CHAIN (round-3 fix): the report/evidence land at
DIR, but `LATEST`, the deltas and the ABSTAIN streaks track ONLY the default
`<lake>/acceptance/` chain — a redirected run never dangles the pointer or
resets the chain. A failed report write is LOUD: one stderr SUSPECT line,
the table restamped to exit 2, process exit 2.

## 6. Nightly cron (standalone-first; chain link = follow-up F1)

```cron
# after the nightly window; exit codes are cron-native
30 2 * * * /path/to/venv/bin/brutex verify-acceptance >> ~/.brutex/logs/acceptance-cron.log 2>&1
```

While a pull looks active (journal heartbeat / pull lease) the data checks
(unique/tf/pull/sanity) ABSTAIN `pull_in_progress` (`--force-during-pull`
overrides); rate + movers + latency still run, stamped `pull_active`
(round-4 B4-6 — rate reads only persisted evidence, so it runs too; #1751
owns /db-during-pull fixes — this tool observes, never remediates). A cron
firing while a manual run is still in flight is REFUSED by the
single-flight lock (exit 2, red — overlap means the schedule needs
attention, never a silent chain fork).

## 7. Useful flags

```bash
--checks unique,tf          # subset
--full                      # lift sampling + month bounds (minutes → hours; cost printed first)
--seed 2026-07-10           # reproduce that night's sample exactly
--date 2026-07-09           # movers/latency coordinate day
--latency-budget movers_hit=900
--max-runtime-s 900         # SOFT budget: unreached checks ABSTAIN(time_budget_exhausted)
--check-timeout-s 120       # HARD per-check deadline (default 120; <=0 disables)
--repro tf                  # print the exact single-check repro command
--json                      # machine summary on stdout (files always written)
--out DIR                   # OFF-CHAIN report copy: LATEST/deltas/streaks untouched
```

stdout carries EXACTLY the human table (or the `--json` summary) — every log
line rides STDERR at INFO by default (`BRUTEX_LOG_LEVEL` overrides, junk
fails loud; the #1630 seam — round-3 fix), so `brutex verify-acceptance
--json | jq .` works — INCLUDING the early-SUSPECT paths (symlink / lock
refusal, audit-open failure), which emit a minimal `{"error": …, "run_id":
…}` JSON object instead of zero bytes (round-4 B4-5). `--sample` must be
≥ 0 (usage error otherwise). `--sample` governs the SPOT dataset rotation
only — the F&O contract fill is a FIXED seed-rotated 6 contracts/run
(`FNO_SAMPLE_N`; `--full` lifts both — round-4 A NIT disclosure).

## 8. Triage notes

- `<check>.timed_out` ERROR (exit 2, round-5 F1) = a check exceeded its HARD
  per-check wall-clock deadline (`--check-timeout-s`, default 120 s) and was
  abandoned on a daemon thread so the run STILL finishes, writes its report,
  and releases the single-flight flock — never an unbounded hang that bricks
  the command. It signals a wedged store / stalled external volume; re-run
  after the volume recovers. (The `--max-runtime-s` soft budget only stops
  UNREACHED checks BETWEEN checks; the watchdog is what aborts a check
  already running.) The shared in-process dashboard store the movers/latency
  checks use now hands each served request a PRIVATE read cursor + lock, so
  an unbalanced describe in a served route can no longer leak the shared read
  lock across worker threads and deadlock a later request (the F1 root fix).
- `pull.partition_presence` FAIL = the class-3 silent-loss footgun (a
  SUCCESS ledger row whose `bars/` month file is gone) — restore from backup
  or delete the row set via a ledger remediation; a re-pull will NOT notice
  on its own (the row is settled).
- A movers `%CHNG`-only mismatch is pre-attributed
  `stored_tf_prevclose_divergence` (the known stale-stored-TF prev-close
  class, parked audit 2/5) — still a FAIL; re-run the pipeline prebuild.
- A FAILing independent movers leg after a LEGITIMATE law change means the
  law text moved: update `brutex/acceptance/independent.py` to the new law
  TEXT in the same PR as the production change (both sides' values are in
  evidence) — that loud failure is the design working.
- Merge-state preflight: the parked-audit classes (#1332 bloom autogrow,
  #1336/#1378, #1399, #1351 TF-oracle harness, #1646 chain) should be
  reconciled against current main when triaging a FAIL those classes own —
  the check still FAILs (truth first), the reference is a triage aid.

## 9. Bounded real re-pull (NOT built)

A `--repull-one-day` mode (real vendor calls asserting `bars_net_new == 0`)
is follow-up F7 and needs its own DEC (vendor-touching). Until then the
strongest e2e idempotence proof remains the shipped stress suite
(`tests/stress/test_groww_option_ingest_sustained.py`).
