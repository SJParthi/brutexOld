# Runbook — unkillable sweep run core

**DEC-SWEEP-UNKILLABLE-CORE-001 (MIGRATION.md #1594, ADR-0019).** Operator
procedures for the Phase-2 run core: resume, quarantine forensics, SUSPECT
triage, the failure matrix, the TUPLE_DONE decimation policy, and the human
R/G traceability table.

## 0. Where everything lives

One run = one deterministic id: `run_id = grid_identity(grid)` (BLAKE3 over
the selection + repro hashes — NO timestamp, so a resume/re-run of the same
selection+data lands in the SAME directory). Every Phase-2 artifact sits
under `<results_root>/runs/<run_id>/`:

| File | What it is |
|---|---|
| `flight-recorder.jsonl` | hash-chained event timeline (SHA-256 `AuditWriter` chain, `actor="flight"`) |
| `manifest.json` | R28 run manifest (selection / estimate / actual / timings / door kills / dedup / tripwires / suspect) |
| `exit-status.json` | `{state, exit_code, refusal_text, error_type, attempt, pid, wall_*, suspect}` |
| `chain-map.json` | per-link green/amber/red snapshot (preflight→sweep→stage3→cascade→artifacts) |
| `verdicts.json` | per-mask stage3/cascade verdicts (G8 source for /results marking) |
| `diagnostic-bundle.zip` | R40 one-file bundle (built on success AND failure; secrets redacted) |
| `*.stamp.json` | R42 content-hash stamps beside stage artifacts |

Quarantine (forensic — nothing is ever deleted):
`<results_root>/.quarantine/runtime/<prefix>-<n>/` (corrupt tuple trios),
`<results_root>/.quarantine/checkpoints/<n>.json` (corrupt checkpoints),
`<name>.discarded.<n>` renames beside a stage artifact that failed stamp
validation. The `runs/` and `.quarantine/` namespaces are structurally
invisible to sweep_heal + /results discovery (test-pinned).

## 1. Resume after ANY death (kill -9, ENOSPC halt, machine death, reboot)

1. Just relaunch the same command: `brutex sweep-all ...` (same selection).
   The deterministic run_id lands you in the same `runs/<run_id>/`; the
   checkpoint + per-tuple completion sentinels short-circuit every finished
   tuple; recovery starts at the first incomplete tuple — never from zero.
2. Check the previous attempt's verdict first if you want:
   `cat <results_root>/runs/<run_id>/exit-status.json` — `state` is one of
   `running / complete / failed / refused / halted / suspect_complete`.
3. The flight recorder appends a `RESUME` record; `attempt` increments
   (observability only — never part of any identity or result byte).
4. A second concurrent launch is REFUSED loudly (G6 single-flight flock,
   names the holder pid + lock path). The kernel releases the lock on process
   death — no stale-lock cleanup is ever needed.
5. Detached runs: `brutex sweep-all --detach` spawns the run as its own
   session leader (dashboard/terminal death cannot kill it). Probe it via
   `exit-status.json` + the progress file's `wall_time_utc`/`pid`.

## 2. Quarantine forensics

- **Corrupt tuple store mid-grid (G1):** the trio
  (`<store>.parquet`, `.trials.json`, `.complete.json`) is moved to
  `.quarantine/runtime/<prefix>-<n>/`, the tuple is re-run ONCE fresh
  (sentinel-redo law ⇒ byte-identical regeneration); a second failure counts
  the tuple failed — the grid CONTINUES. Each event = one `QUARANTINE`
  flight record naming `what`, `path_rel`, `reason`.
- **Corrupt checkpoint (G12):** moved to `.quarantine/checkpoints/<n>.json`,
  the run starts fresh (sentinels make it cheap); a 2nd consecutive
  corruption in-process fires a CRITICAL alert (crash-loop breaker). A
  FOREIGN-sweep-id checkpoint returns None WITHOUT quarantine (existing law).
- **Stage artifact failing stamp validation (D-4/G7):** renamed
  `<name>.discarded.<n>` (artifact + stamp both kept), then the producing
  pass re-runs once. Earlier stages' bytes are untouched (chaos-pinned).
- Triage: `grep QUARANTINE runs/<run_id>/flight-recorder.jsonl` or use the
  bounded search helper (`flight_recorder.search_timeline(kind="QUARANTINE")`).

## 3. SUSPECT triage

SUSPECT is a set-once latch (D-12) — it marks, it never kills. Sources:

- **R39 tripwires**: count reconciliation (enumerated == done+skipped+failed+
  in-flight, read EXACTLY from SweepProgress counters), NaN/absurd-value
  screens, throughput floor (dwell-gated), memory budget/slope (dwell-gated,
  ru_maxrss platform-normalized), stage-transition invariants.
- **R45 diffcheck**: an independent naive reference simulator re-simulates K
  sampled masks per run; ANY field mismatch ⇒ SUSPECT. The production value
  is NEVER replaced (never-adopt law).

Where it surfaces (all at once, first set only): `TRIPWIRE`/`DIFFCHECK_MISMATCH`
+ `SUSPECT` flight records · structlog ERROR · Telegram `run_suspect`
(CRITICAL) · `manifest.json` `suspect: true` + `suspect_reasons[]` ·
`exit-status.json` (`suspect_complete` when the run otherwise finished) ·
the progress payload the dashboard polls.

What to do:

1. Read the trip detail: `manifest.json → tripwires[]` (name, severity,
   detail, values) or the `TRIPWIRE` records.
2. `DIFFCHECK_MISMATCH` records carry `{tuple_digest, mask_hex, field, prod,
   ref}` — enough to re-run that one tuple by hand.
3. Treat the run's RESULTS as suspect until explained: do not promote a
   SUSPECT run's leaderboard. The compute is not lost — artifacts are intact
   and re-runnable bit-for-bit (R42/R43).
4. A tripwire is a HEURISTIC (thresholds env-tunable, see §6); a diffcheck
   mismatch is NOT — escalate it as a correctness bug with the record's
   field-level evidence.

## 4. Failure matrix — link-N failure → defined answer (R42)

ONE-LINE LAW: a break at link N never corrupts/loses links 1..N−1; recovery
starts at N, never zero. (Chaos-suite-pinned with before/after hashes.)

| Link / artifact | kill -9 mid-write | ENOSPC | corrupt bytes | concurrent 2nd writer | half-written consumed |
|---|---|---|---|---|---|
| tuple trio (store/.trials/.complete) | sentinel-last ordering ⇒ full redo of that tuple | graceful halt at checkpoint | sentinel SHA mismatch ⇒ redo; store-load corruption ⇒ G1 quarantine + one retry, grid continues | G6 flock refuses the 2nd sweep | sentinel absent ⇒ redo |
| checkpoint JSON | atomic write ⇒ never half | halt; resume from last committed | G12 quarantine-start-fresh; sentinels stay authoritative | flock | atomic ⇒ never half |
| progress JSON | atomic | G11 fail-soft WARN-once, run continues | reader tolerant; G4 liveness = wall-clock + pid; legacy ⇒ UNKNOWN ⇒ heal REFUSES | reset-at-launch | atomic |
| SHA-256 CLI chain / BLAKE3 supervisor chain | torn-tail truncate on resume | append error = material action ⇒ graceful halt | verify names the seq; CI red | G6 kills the fork class | torn tail skipped |
| stage-3 cell stores | D-4 per-cell stamp — missing/invalid ⇒ discard + re-run pass once | halt; pass idempotent on resume | stamp mismatch ⇒ discard + rebuild | flock | stamp fails ⇒ discard |
| cascade manifest/report | stamp post-write; replay validates stamp + `data_snapshot_hash` vs live bars; mismatch ⇒ refuse/rebuild that link only | halt | stamp mismatch ⇒ rebuild link only; earlier links untouched | flock | stamp fails ⇒ rebuild |
| flight-recorder.jsonl | AuditWriter torn-tail resume | D-20: ONE WARN + degraded latch + drop counter, run CONTINUES | verify names the seq; bundle flags it | single writer under G6 | torn tail tolerated |
| manifest / exit-status / verdicts / chain-map | atomic; rewritten per boundary (stale-but-valid on kill) | fail-soft; the CLI-chain `RUN_MANIFEST` line is the durable twin | parse fail ⇒ honest `unreadable` verdict, never a 500 | single writer | atomic |
| .stamp.json sidecars | write-AFTER-artifact ⇒ kill between = unstamped = rebuild (safe direction) | fail-soft ⇒ treated unstamped ⇒ rebuilt | validation recomputes the full streamed SHA-256 | n/a | THE mechanism |
| diagnostic-bundle.zip | run-end only; fail-soft | fail-soft | terminal artifact | n/a | zip temp→replace |

Stall (F5): tunable/scaled stall policy (§6) + tick-fed liveness — the
watchdog DETECTS and alerts, it never kills a wedged worker (a truly wedged
C-level call parks a zombie — the #999 stance). Dashboard restart (F7):
detached child + exit-status + wall-clock progress. Machine death (F8):
checkpoint + sentinel + stamp laws ⇒ relaunch resumes at N (§1).

## 5. TUPLE_DONE decimation policy (D-9 — lossless)

On grids past ~10k tuples the recorder decimates `TUPLE_DONE` records with
stride `ceil(total / 10_000)` (small grids record every tuple). Losslessness:

- every RECORDED `TUPLE_DONE` payload carries CUMULATIVE
  `{done_cum, skipped_cum, failed_cum}` — the gaps carry no information;
- the R39 count-reconciliation tripwire reads `SweepProgress` counters
  EXACTLY, never recorder record counts;
- stage, milestone, failure, quarantine, tripwire, SUSPECT, halt and
  run-boundary records are NEVER decimated.

Honesty note (ledgered in #1594): R41's literal "every event = one record"
wording is met for every material-event kind; the per-tuple done stream is
the ONE deliberately decimated kind, with the cumulative counters making the
decimation information-lossless.

## 6. Env knobs (all fail-loud at LAUNCH — junk refuses the run, never mid-run)

| Env | Default | Meaning |
|---|---|---|
| `BRUTEX_SWEEP_STALL_SECONDS` | unset | operator stall-timeout pin (wins outright; >0) |
| `BRUTEX_SWEEP_STALL_FACTOR` | 10.0 | scaled policy factor over the per-tuple estimate; scaling only RAISES above the 300 s default |
| `BRUTEX_SWEEP_DIFFCHECK` | ON (tristate) | R45 differential spot-check kill switch |
| `BRUTEX_SWEEP_DIFFCHECK_K` | 200 | per-run sampled-mask budget |
| `BRUTEX_SWEEP_TRIPWIRE_RECONCILE_INTERVAL_S` | 60.0 | count-reconcile cadence |
| `BRUTEX_SWEEP_TRIPWIRE_THROUGHPUT_FLOOR` / `_THROUGHPUT_DWELL` | 0.2 / 5 | throughput trip fraction + dwell |
| `BRUTEX_SWEEP_TRIPWIRE_RSS_BUDGET_FRACTION` | 0.85 | of detected total RAM; 0 disables the memory arm |
| `BRUTEX_SWEEP_TRIPWIRE_MEMORY_SLOPE_BYTES` / `_MEMORY_DWELL` | 8 MiB / 5 | memory-trend trip + dwell |
| `BRUTEX_SWEEP_TRIPWIRE_ABSURD_PNL_PAISA` | 10^15 | absurd-value screen |

## 7. Traceability — human table (the MECHANIZED gate is authoritative)

The binding gate is `tests/unit/test_r16_traceability_matrix.py`: every token
below must map to ≥1 collected test BY NAME (via per-file `TRACEABILITY`
dicts + `test_rNN_*`/`test_gNN_*` prefixes); dropping any token's last test
turns CI red. This table is the human view (counts as of ship, 2026-07-08);
if it ever disagrees with the mechanized gate, the gate wins.

| Token | What it guarantees | Tests | Representative test |
|---|---|---|---|
| R7 | unkillable + detached survival + Telegram | 31 | `tests/integration/test_sweep_detached_survival.py::test_r7_detached_child_survives_parent_death` |
| R16 | 5-layer assurance contract coherence | 19 | `tests/unit/test_r16_traceability_matrix.py::test_r16_every_required_token_maps_to_a_collected_test` |
| R28 | run manifest + funnel on the hash chain | 34 | `tests/integration/test_sweep_full_chain_rehearsal.py::test_r28_run_manifest_action_rides_the_cli_chain` |
| R38 | full-chain CI rehearsal + fault injection | 8 | `tests/integration/test_sweep_full_chain_rehearsal.py::test_r38_full_chain_completes_clean` |
| R39 | live tripwires + SUSPECT | 25 | `tests/property/test_tripwire_reconciliation_invariant.py::test_r39_legal_interleavings_never_trip` |
| R40 | auto diagnostic bundle | 12 | `tests/unit/test_observability_diag_bundle.py::test_r40_bundle_contains_run_dir_extras_logtail_fingerprint_meta` |
| R41 | flight recorder + chain map + milestones | 49 | `tests/chaos/test_sweep_unkillable_chaos.py::test_r41_torn_flight_recorder_tail_resumes_and_verifies` |
| R42 | mid-chain break law (stamps + atomic) | 43 | `tests/chaos/test_sweep_unkillable_chaos.py::test_r42_kill_after_flush_before_sentinel_resume_bit_identical` |
| R43 | ONE run_id threads everything | 10 | `tests/integration/test_sweep_full_chain_rehearsal.py::test_r43_one_run_id_threads_every_artifact` |
| R45 | differential spot-check, never-adopt | 22 | `tests/redteam/test_redteam_sweep_unkillable.py::test_r45_broken_reference_plant_marks_the_run_suspect_everywhere` |
| G1 | corrupt tuple store ⇒ quarantine, not abort | 16 | `tests/chaos/test_sweep_unkillable_chaos.py::test_g1_corrupt_tuple_store_mid_grid_quarantined_grid_completes` |
| G2 | tunable/scaled stall guard | 19 | `tests/unit/test_engine_sweep_stall_policy.py::test_g2_default_is_exactly_300` |
| G3 | parallel live progress | 4 | `tests/unit/test_engine_sweep_parallel_runcore.py::test_g3_writer_actor_ticks_on_progress_per_applied_tuple` |
| G4 | progress honesty; heal never quarantines a live run | 26 | `tests/integration/test_sweep_progress_enrichment.py::test_g04_heal_refuses_live_run_the_old_expression_misjudged` |
| G5 | exit code + refusal surfaced on page | 27 | `tests/integration/test_sweep_progress_enrichment.py::test_g05_route_serves_exit_status_with_zero_route_edits` |
| G6 | cross-process single-flight flock | 14 | `tests/chaos/test_sweep_unkillable_chaos.py::test_g6_concurrent_second_sweep_refused_and_winner_chain_verifies` |
| G7 | cascade snapshot-hash stamps + atomic sidecars | 14 | `tests/unit/test_engine_cascade_wire_runcore.py::test_g7_changed_lake_refuses_the_replay_naming_both_hashes` |
| G8 | /results verdict source (P2 artifact side) | 22 | `tests/unit/test_engine_run_verdicts.py::test_g8_contract_fixture_pins_verdicts_json_shape` |
| G9 | keep-awake on sweep entry points | 1 | `tests/unit/test_engine_sweep_run_core.py::test_g09_keep_awake_entered_once` |
| G10 | preload vs pull/heal lake lock | 9 | `tests/chaos/test_sweep_unkillable_chaos.py::test_g10_preload_refused_while_pull_holds_the_lake_lock` |
| G11 | failed progress write never kills the run | 4 | `tests/chaos/test_sweep_unkillable_chaos.py::test_g11_readonly_progress_dir_run_completes` |
| G12 | corrupt checkpoint ⇒ quarantine-start-fresh | 17 | `tests/chaos/test_sweep_unkillable_chaos.py::test_g12_corrupt_checkpoint_quarantined_fresh_start_no_crash_loop` |
| G13 | alerter-first daemon + escalation kernel | 13 | `tests/unit/test_engine_sweep_scheduler_escalation.py::test_g13_daemon_init_guard_alerts_then_reraises` |
| G14 | old-schema Parquet ⇒ typed honest degrade | 16 | `tests/integration/test_sweep_full_chain_rehearsal.py::test_g14_old_schema_store_degrades_typed_never_raw` |
| G15 | sweep-tick-fed watchdog (no false STALLED) | 1 | `tests/integration/test_sweep_full_chain_rehearsal.py::test_g15_progress_beats_reach_the_pull_jobs_channel` |

## 8. Known limits (honest envelope — see ADR-0019 §4 for the full list)

Dashboard-launched sweeps remain in-process until the 1-line detach adapter
lands (the CLI path is detached-capable today via `--detach`); the five
hard-gate per-door kill counts are ABSENT manifest keys (P3 follow-up);
diffcheck covers projection + trade-sim + drawdown, not the 60 indicator
evaluators; the G10 refusal a starting pull prints while a sweep holds the
shared lake lock says "peer pull" — misleading but safe, follow-up
DEC-SWEEP-LOCK-WORDING-001.
