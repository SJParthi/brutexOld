# Runbook — `brutex verify-acceptance` (the operator acceptance verifier)

> DEC-OPERATOR-ACCEPTANCE-VERIFY-001, MIGRATION.md #1756.
> ONE command that verifies the 7 acceptance classes on the REAL lake and
> writes an evidence report you can prove untampered. Read-only on the lake;
> writes ONLY under `<lake_root>/acceptance/`. ZERO vendor calls, ever
> (AST-pinned) — the pull lock is never taken.

## 1. The one command

```bash
brutex verify-acceptance                # all 7 checks, teeth smoke, default sampling
```

Defaults resolve with zero prompts: lake root via `$BRUTEX_LAKE_ROOT` /
`~/.brutex/lake`; sampling seed = the IST report date (deterministic daily
rotation — a nightly cron covers all ~750 datasets in ~⌈750/12⌉ nights);
non-engine scans bounded to the last 6 months. The engine set
(NIFTY/BANKNIFTY/SENSEX + INDIAVIX + the NIFTY-FUT contract dirs) is ALWAYS
in scope at full stored history.

## 2. What each check proves (and honestly cannot)

| check | proves | permanent limits |
|---|---|---|
| unique | census fold (`scan_lake_invariants`) clean AND complete — a skipped file FAILs | month-bounded by default (`--full` lifts) |
| tf | 19 intraday TFs fresh (the sweep-preflight seam) + the 1day re-derive compare | `tf.oi` ABSTAINs until follow-up F2 (OI not value-compared) |
| pull | re-plan over the covered span mints ZERO new fetchable windows; keep-first counter laws; SUCCESS rows have backing files (sampled) | plan-level convergence, not vendor determinism; presence PASS is sample-scoped |
| rate | constants pin · market-hours conformance (#1692) · bridge/log point evidence | `rate.per_call_history` = PERMANENT ABSTAIN — no per-call record persists (F3 needs an operator ruling); live probe = `brutex doctor --live --stage 13` |
| movers | page cells == the FENCED independent recompute (raw parquet + hand math) | rank verified within served rows; Greeks presence-only |
| sanity | price sanity · bad-tick (4x screen, #1616 escapes named) · tick grid · session windows · interior holes · VIX presence · completeness | heuristic screens stay heuristics |
| latency | measured cold+warm ms vs on-box budgets | absolutes, not the CI 1.1x tier, not a proof |

## 3. Teeth (why you can trust a PASS)

Every run starts with a <10 s smoke: a twin lake is built with the REAL
writers, 3 poisons are planted (duplicate row, x10-scaled stored bar, a
perturbed movers cell), and the real checks must catch all 3 with zero false
FAILs on the clean twin. Any miss ⇒ `TEETH_FAILED`, exit 2, and every
real-lake verdict is WITHHELD ("nothing below is trustworthy").

```bash
brutex verify-acceptance --self-test-only   # the FULL 15-poison battery + table
brutex verify-acceptance --self-test        # full battery, then the real run
brutex verify-acceptance --no-self-test     # skip (report carries TEETH_SKIPPED)
```

The full battery also runs in CI on every merge
(`tests/integration/test_acceptance_selftest.py`), including the
comparator-inversion tooth.

## 4. Exit codes (cron-native)

| code | meaning | cron |
|---|---|---|
| 0 | clean (teeth proven or explicitly skipped+recorded) | green |
| 1 | usage error (bad flags, missing lake root) | red |
| 2 | SUSPECT — verifier internal error / TEETH_FAILED / report-write failure | red |
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

Prove a report untampered:

```bash
brutex audit verify <lake>/acceptance/audit/acceptance.jsonl
sha256sum <lake>/acceptance/<run_id>/report.json   # vs the ACCEPTANCE_RUN_COMPLETE line
```

Run-over-run deltas + consecutive-ABSTAIN streaks are carried in the report
chain (previous report via LATEST) — no separate state file.

## 6. Nightly cron (standalone-first; chain link = follow-up F1)

```cron
# after the nightly window; exit codes are cron-native
30 2 * * * /path/to/venv/bin/brutex verify-acceptance >> ~/.brutex/logs/acceptance-cron.log 2>&1
```

While a pull looks active (journal heartbeat / pull lease) the data checks
ABSTAIN `pull_in_progress` (`--force-during-pull` overrides); movers/latency
still run, stamped `pull_active` (#1751 owns /db-during-pull fixes — this
tool observes, never remediates).

## 7. Useful flags

```bash
--checks unique,tf          # subset
--full                      # lift sampling + month bounds (minutes → hours; cost printed first)
--seed 2026-07-10           # reproduce that night's sample exactly
--date 2026-07-09           # movers/latency coordinate day
--latency-budget movers_hit=900
--max-runtime-s 900         # unreached checks ABSTAIN(time_budget_exhausted)
--repro tf                  # print the exact single-check repro command
--json                      # machine summary on stdout (files always written)
```

## 8. Triage notes

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
