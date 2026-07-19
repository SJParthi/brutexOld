# Runbook — kernel-cache OFF↔ON A/B soak (the default-ON flip gate)

> Operator procedure for producing the ONE measurement set that can open the
> per-worker kernel-cache default-ON flip DEC. The cache
> (DEC-SWEEP-WORKER-KERNEL-CACHE-001, MIGRATION.md #1729 — landed via the
> #1553 train squash) shipped **DEFAULT-OFF** behind
> `BRUTEX_SWEEP_WORKER_KERNEL_CACHE` precisely because its retained
> per-worker RSS (MODELED ~0.66–0.73 GB/worker at the default capacity
> sizing) stacks on each worker's divided rank budget; the flip is a
> separate later one-constant PR
> (`WORKER_KERNEL_CACHE_DEFAULT: False → True`, the
> DEC-SWEEP-RUST-METRICS-001 → -DEFAULT-ON-001 two-step precedent) that may
> open ONLY with the §8 M4-Pro soak evidence this harness produces
> (DEC-SWEEP-WORKER-KERNEL-CACHE-SOAK-001).

## 1. Honesty banner (read first — Golden Rule #10)

- **The harness + acceptance bars are coordinator-blessed; the FLIP is NOT
  pre-approved.** A green soak report does not flip anything: the default-ON
  flip is a separate one-constant DEC that quotes this report's RAW figures
  and needs operator sign-off.
- **Only the registered M4-Pro runner's numbers count.** Numbers from any
  other machine are UNVERIFIED lab evidence and cannot satisfy the rule (the
  DEC-SWEEP-CAP-BENCH-001 clause). The report's `provenance.hardware_key` +
  `commit` carry the proof.
- **The `--smoke` mode proves MECHANICS only** (tiny real-writer fixture
  lake). The report stamps `smoke: true`, the RSS/wall bars read
  `SMOKE-UNJUDGED`, and the verdict can never read `FLIP-READY` —
  "soak-passed" cannot be claimed from a smoke run.
- **The REAL soak needs the operator's real lake.** No M4-Pro workflow
  exposes a lake path, and whether the runner daemon exports
  `BRUTEX_LAKE_ROOT` is UNVERIFIED — the script preflights the lake SHAPE
  (`bars/<exchange>/<segment>/<symbol>/<interval>/**/*.parquet` per requested
  coordinate) and **refuses to soak blind**; pass the `lake_root` dispatch
  input explicitly if the first dispatch refuses.
- **Per-PID peak RSS is a LOWER bound** (5 s `ps` polling — a sub-interval
  spike can slip between samples; stamped in-report as `rss_method`).
- **The full 2-4-day §8 sweep is never run twice in one job** (the 24-h
  self-hosted GITHUB_TOKEN fence, SEC9-1). The soak grid keeps FULL bar
  depth (RSS is depth-driven, not tuple-count-driven) but bounds tuple
  count; the full-grid wall saving stays MODELED in the report.
- **The RSS evidence is scoped to the MEASURED kernel-cache capacity.** The
  report echoes `provenance.worker_kernel_capacity_effective` — the
  grid-derived default, explicitly PINNED as
  `BRUTEX_SWEEP_WORKER_KERNEL_CAPACITY` in BOTH arm envs (fix-round-2: the
  child `sweep-all` re-reads dotenv at startup, so an unset var would adopt
  a `~/.brutex/.env` value; the dotenv loader never overrides a SET var, so
  the explicit pin is dotenv-immune; `capacity_env_child_pinned` +
  `capacity_env_ambient_present` echo it) — the default grid measures
  capacity=1; widen `--fib-anchors` / `--price-source-cells` (dispatch
  inputs `fib_anchors` / `price_source_cells`) to soak a higher capacity.
  **The flip DEC must weigh whether the measured capacity covers the
  operator's grids** — a green RSS bar at capacity=1 does not validate a
  wide-anchor×source production sweep.
- **A non-M4-Pro report can never read FLIP-READY through the MECHANICAL
  bar** — the acceptance block stamps a `provenance` bar: a non-smoke report
  whose `hardware_key` lacks the registered §8 signature
  (`…-14c10p4e-48g-arm64`) — or whose key came from the documented
  `BRUTEX_BENCH_BASELINE_KEY` env OVERRIDE rather than real hardware probes
  (`provenance.hardware_key_source`; the `verdict` subcommand also
  belt-checks the live env) — reads `NOT-DECISION-GRADE`, never
  `FLIP-READY`. **Trusted-input boundary (Golden Rule #10):**
  `soak-report.json` is EVIDENCE for the flip DEC, not a tamper-proof
  artifact — it is hand-editable, and `verdict` re-derives from the report
  it is handed; the real provenance anchor is that the operator dispatches
  the workflow on their own registered runner (or runs the fallback on
  their own box) and signs off on the DEC.
- **Arms run sequentially on a live Mac** — thermal/background drift between
  arms is uncontrolled; soak an idle machine (the bench-m4pro quiet-runner
  assumption) and outside the nightly pull window (the sweep takes the G10
  lake read-guard on the pull lock — a concurrent nightly would contend).
- **The wall bar carries an ORDERING bias** (fix-round-3): arms run OFF→ON
  by default, so the second (ON) arm inherits a warm OS page-cache/import
  state — a systematic ON-favoring bias INSIDE the ≤ 1.02 no-regression
  band, bounded by the preload share of a 2-4 h arm. It is disclosed on the
  summary; re-run with `--arm-order on-first` for a bias check.
- **The ABSOLUTE RSS/pool bars are measured at PINNED sizing** (fix-round-3):
  `BRUTEX_SWEEP_WORKERS` + `BRUTEX_SWEEP_MEM_BUDGET_BYTES` are explicitly
  SET in both arm envs to the parent-resolved DEFAULT laws (dotenv-immune,
  the capacity-pin mechanism), and the report echoes
  `provenance.sweep_workers_effective` / `mem_budget_bytes_effective` — a
  stray ambient/`.env` sizing value can never silently re-scope the 36 GiB
  pool ceiling or the per-worker delta. The remaining symmetric sweep envs
  are echoed in `provenance.symmetric_env_echo` (parent-environ view only —
  dotenv values are invisible there, which is exactly why the sizing envs
  are pinned rather than echoed).
- **Forensics mirrors are per-arm** (fix-round-3): both arms share `run_id`
  (grid identity — the cache flag is env, not grid), so the default shared
  `~/.brutex/runs/<run_id>/` Row-15 mirror would be clobbered cross-arm;
  each arm's flight-recorder mirror lands under its own `<out>/arm-*/mirror/`
  instead (write-only forensics — the operator's shared mirror root is
  untouched by a soak).

## 2. What one soak run does

`scripts/ci/kernel_cache_soak.py run` executes, in order:

1. **Preflight** — lake shape + free disk (digits-fenced) + env echo
   (`hardware_key`, commit, uname). Any miss → loud refusal, exit 2.
2. **Arm OFF** — `brutex sweep-all` as a subprocess with
   `BRUTEX_SWEEP_WORKER_KERNEL_CACHE=0`, `BRUTEX_LOG_LEVEL=INFO`,
   `PYTHONHASHSEED=0`, a FRESH `--results-root <out>/arm-off/results`.
   `run_id = grid_identity(grid)` carries NO timestamp, so a shared root
   would resume/cache-skip the second arm into a fake instant win — fresh
   per-arm roots are load-bearing. A sampler thread polls
   `ps -eo pid=,ppid=,rss=` and folds per-PID running maxima + the
   pool-wide sum over the arm's process tree.
3. **Arm ON** — identical with `=1` and a fresh `<out>/arm-on/results`.
   Default cache capacity is ENFORCED: the grid-derived default is
   explicitly SET as `BRUTEX_SWEEP_WORKER_KERNEL_CAPACITY` in both arm envs
   (the flip ships the default sizing law, so the evidence measures it;
   pinning — not popping — because the child's dotenv re-read would refill
   an unset var from `~/.brutex/.env`) and the effective capacity + the pin
   are echoed into the report's provenance.
4. **Telemetry harvest** — the ON arm must emit exactly one
   `sweep_parallel.kernel_reuse_summary` (builds/hits/workers) and the OFF
   arm none; `exit-status.json` must read `complete`, no
   `.sweep_quarantine.json`, no SUSPECT, no pool-broken/stall tokens.
5. **Byte-identity compare** — SHA-256 of every file under each arm's
   per-tuple store tree (`<arm>/results/`, run-state dirs + wall-stamped
   sidecars excluded) AND the results-depth sibling root
   (`<arm>/sweep_runs/` — top100.json + trade/equity sidecars + baselines,
   only `.stamp.json` masked; fix-round-2 — previously silently outside the
   compare). Each exclusion is listed in `compare.excluded_sidecars`, never
   silently skipped; audit chains compared ts-masked
   (seq/action/subject/payload, the RUN_MANIFEST payload's volatile
   manifest keys scrubbed); exit-status compared structurally.
6. **Report** — `<out>/report/soak-report.json` (machine-readable) +
   `summary.md` (every figure tagged MEASURED vs MODELED, plus the VALUE
   section surfacing the actual wall + RSS deltas and real headroom) +
   64 KiB log tails. Exit 0 = judged checks green; 1 = an
   acceptance-relevant failure (report still written); 2 = structural.

## 3. Running it (install-free — the dispatch path)

1. GitHub → Actions → **"Kernel-cache soak (self-hosted M4-Pro)"** →
   *Run workflow*.
2. Inputs:
   - `budget_minutes` — both arms + compare must fit (default 600 = 10 h;
     fenced 1..1195 so the derived job timeout stays under the 24-h
     self-hosted token validity, SEC9-1).
   - `lake_root` — blank resolves `$BRUTEX_LAKE_ROOT` → `~/.brutex/lake` on
     the runner; pass the real path explicitly if the preflight refuses.
   - `max_tuples` — optional `sweep-all --max-tuples` REFUSAL guard.
     `sweep-all` refuses an oversized grid, it never truncates — bound the
     actual grid via the script's `--k/--conditions/--sl/--tp` defaults (or
     edit the dispatch to a one-command fallback run, §4).
   - `sample_interval_s` — RSS poll cadence (default 5).
   - `fib_anchors` / `price_source_cells` — optional sweep-all axis CSVs
     (blank = `prev_day` / `hlc3:close`): the capacity-widening knobs — set
     both to soak a kernel-cache capacity > 1 (the report states the
     measured capacity; an unknown value is refused loudly).
3. The Mac must be awake with the runner online (a queued dispatch on an
   offline runner is auto-cancelled by GitHub after 24 h with NO step run —
   and no issue filed; the ADR-0018 out-of-job-watchdog gap).
4. Collect the `kernel-cache-soak-report` artifact (90-day retention). A
   failed or timeout-killed run files a dedup-by-title issue
   (`bug,soak,critical`).

## 4. One-command fallback (on the Mac, from the repo root)

```bash
source .venv/bin/activate
python scripts/ci/kernel_cache_soak.py run \
  --lake-root ~/.brutex/lake \
  --out ~/kernel-cache-soak \
  --budget-minutes 600
```

Then attach `~/kernel-cache-soak/report/soak-report.json` (+ `summary.md`)
to the flip PR. **`--out` must be a FRESH (nonexistent or empty) dir per
run** — the script refuses a non-empty `--out` (exit 2): a prior run's
artifacts would resume/cache-skip both arms into fake instant walls
(`run_id` carries no timestamp), so `rm -rf ~/kernel-cache-soak` first (or
pass a new path). A PREFLIGHT-refused run (bad/absent lake, disk short)
writes NOTHING into `--out` (fix-round-2), so the documented remediation —
re-run the same command with `--lake-root` pointing at the real lake —
works on the SAME `--out` without an extra `rm -rf`. `--budget-minutes` is
fenced 1..1195 script-side (the same SEC9-1 law as the workflow).
`--min-free-gib` (fenced 1..1024, default 30) lowers the NON-smoke
free-disk requirement for a deliberately tiny bounded grid — an operator
judgment, echoed into `provenance.min_free_gib`; the workflow never passes
it, so the M4-Pro default stands. Grid knobs (`--symbols/--intervals/
--start/--end/--k/--conditions/--sl/--tp/--top/--fib-anchors/
--price-source-cells/--workers`) mirror `brutex sweep-all`; the
defaults keep FULL bar depth (2020-01-01 → previous day, NIFTY+BANKNIFTY,
1minute/5minute/15minute) — size `--k`/`--conditions`/`--sl`/`--tp` so each
arm lands ≈ 2-4 h inside the budget, and widen `--fib-anchors` /
`--price-source-cells` to soak a kernel-cache capacity > 1 (§1). `--workers`
(≥ 2) pins `BRUTEX_SWEEP_WORKERS` for both arms; the default (unset)
resolves the auto worker law parent-side (8 on the M4-Pro) and PINS that —
the worker count and the per-worker `BRUTEX_SWEEP_MEM_BUDGET_BYTES` default
are always explicitly set in both arm envs (fix-round-3 sizing pins, §1)
and echoed into provenance. `--arm-order on-first` reverses the default
OFF→ON order for a wall-bias check (§1). A sandbox mechanics check is
`python scripts/ci/kernel_cache_soak.py run --smoke --out /tmp/kc-soak`
(never decision-grade — §1).

## 5. Acceptance bars (what the flip DEC reads from soak-report.json)

The flip PR may open ONLY with a report from the M4-Pro runner showing ALL
of (the `acceptance` block pre-evaluates these; the DEC quotes the RAW
figures — and must quote the ACTUAL peaks/headroom, not just PASS/FAIL):

1. **Byte identity (hard):** `compare.identity_ok == true` — zero ts-masked
   diffs between the OFF and ON result trees INCLUDING the results-depth
   `sweep_runs/` sibling root (`compare.sweep_runs_files_compared` counts
   its coverage); audit chains ts-masked-equal; exit-status structurally
   equal. Any diff = BLOCK, no flip.
2. **Clean exits (hard):** both arms rc 0 + `exit_state == "complete"`, no
   quarantine sidecar, no SUSPECT, no `SweepWorkerPoolBrokenError` /
   `SweepStallError` in either log, no budget timeout.
3. **RSS within the envelope (hard):** `deltas.rss_per_worker_kib ≤ 838,861`
   (0.8 GiB = the ledgered MODELED ~0.66–0.73 GB/worker + ~10% headroom)
   AND the ON arm's `pool_peak_rss_kib ≤ 37,748,736` (36 GiB = 75% of the
   §8 48 GB envelope). The report also carries the ACTUAL per-worker and
   pool peaks + headroom — the DEC quotes those numbers. **The bar is
   evaluated AT `provenance.worker_kernel_capacity_effective`** — the DEC
   must weigh whether that capacity covers the operator's grids (widen
   `--fib-anchors`/`--price-source-cells` for a higher-capacity soak; the
   0.8 GiB ceiling targets the HIGH-capacity worst case, which a
   capacity-1 run does not exercise). The absolute bars are measured at the
   PINNED default sizing — `provenance.sweep_workers_effective` +
   `mem_budget_bytes_effective` state it (fix-round-3, §1).
4. **Wall neutral-or-better (hard):** `wall_ratio_on_over_off ≤ 1.02` — a
   NO-REGRESSION band, deliberately not a win gate. The VALUE section
   surfaces the actual win/loss: the flip should show net
   neutral-to-better, and a within-noise no-win flip is a YELLOW FLAG the
   DEC must address explicitly. Mind the disclosed OFF→ON warm-cache
   ordering bias inside this band (§1; `provenance.arm_order` records the
   order — an `--arm-order on-first` re-run is the bias check).
5. **Telemetry invariants (hard):** ON arm exactly one
   `kernel_reuse_summary`; `builds_total + hits_total == tuples_done`;
   `builds_total ≤ keys × workers` (keys = the grid's
   symbols × intervals × fib-anchors × price-source-cells kernel-key
   fan-out); OFF arm silent. Exact builds/hits splits are NEVER asserted
   (worker-split-dependent — the emission's own honesty law).
6. **Provenance (hard):** `hardware_key` matches the registered M4-Pro AND
   `hardware_key_source == "probed"` (not the `BRUTEX_BENCH_BASELINE_KEY`
   env override — a forged override key would carry the M4 signature on any
   box; the bar + the `verdict` subcommand's live-env belt-check refuse it
   as `NOT-DECISION-GRADE`); `commit` is the flip PR's base or a main
   ancestor; `smoke == false`. The mechanical bar judges the report it is
   HANDED — `soak-report.json` is trusted input, evidence for the flip DEC
   rather than a tamper-proof artifact (§1); the commit-ancestry half and
   the report's own integrity stay a human/DEC check (the operator ran it
   on their own box).

`acceptance.verdict` (`FLIP-READY` / `NOT-READY(...)`) is ADVISORY — the
DEC text quotes the raw figures, and the operator signs off. Re-evaluate a
stored report any time with
`python scripts/ci/kernel_cache_soak.py verdict --report soak-report.json`.

## 6. Where the numbers land

- CI artifact `kernel-cache-soak-report` (90 d — expires).
- The accepted run's MEASURED summary block is pasted into §7 below on the
  flip PR AND into the flip DEC's MIGRATION.md entry (the
  DEC-SWEEP-CAP-BENCH-001 runbook idiom): the artifact link is provenance,
  the runbook/ledger carry the numbers — artifacts expire, ledgers do not.

## 7. FIRST MEASUREMENT

_None yet. The harness has run only in `--smoke` mode (sandbox mechanics
proof — not decision-grade). The first real M4-Pro soak's MEASURED summary
block is pasted here by the flip PR._
