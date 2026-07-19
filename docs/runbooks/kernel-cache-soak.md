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
  depth (RSS is depth-driven, so the RSS evidence is decision-grade) but
  bounds tuple count; the full-grid wall saving stays MODELED in the report.
- **Arms run sequentially on a live Mac** — thermal/background drift between
  arms is uncontrolled; soak an idle machine (the bench-m4pro quiet-runner
  assumption) and outside the nightly pull window (the sweep takes the G10
  lake read-guard on the pull lock — a concurrent nightly would contend).

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
   Default cache capacity (no `BRUTEX_SWEEP_WORKER_KERNEL_CAPACITY`
   override) — the flip ships the default sizing law, so the evidence
   measures it.
4. **Telemetry harvest** — the ON arm must emit exactly one
   `sweep_parallel.kernel_reuse_summary` (builds/hits/workers) and the OFF
   arm none; `exit-status.json` must read `complete`, no
   `.sweep_quarantine.json`, no SUSPECT, no pool-broken/stall tokens.
5. **Byte-identity compare** — SHA-256 of every file under each arm's
   per-tuple store tree (run-state dirs + wall-stamped sidecars excluded —
   each exclusion listed in `compare.excluded_sidecars`, never silently
   skipped); audit chains compared ts-masked (seq/action/subject/payload,
   the RUN_MANIFEST payload's volatile manifest keys scrubbed);
   exit-status compared structurally.
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
to the flip PR. Grid knobs (`--symbols/--intervals/--start/--end/--k/
--conditions/--sl/--tp/--top/--workers`) mirror `brutex sweep-all`; the
defaults keep FULL bar depth (2020-01-01 → previous day, NIFTY+BANKNIFTY,
1minute/5minute/15minute) — size `--k`/`--conditions`/`--sl`/`--tp` so each
arm lands ≈ 2-4 h inside the budget. `--workers` (≥ 2) pins
`BRUTEX_SWEEP_WORKERS` for both arms; the default (unset) uses the auto
worker law (8 on the M4-Pro). A sandbox mechanics check is
`python scripts/ci/kernel_cache_soak.py run --smoke --out /tmp/kc-soak`
(never decision-grade — §1).

## 5. Acceptance bars (what the flip DEC reads from soak-report.json)

The flip PR may open ONLY with a report from the M4-Pro runner showing ALL
of (the `acceptance` block pre-evaluates these; the DEC quotes the RAW
figures — and must quote the ACTUAL peaks/headroom, not just PASS/FAIL):

1. **Byte identity (hard):** `compare.identity_ok == true` — zero ts-masked
   diffs between the OFF and ON result trees; audit chains ts-masked-equal;
   exit-status structurally equal. Any diff = BLOCK, no flip.
2. **Clean exits (hard):** both arms rc 0 + `exit_state == "complete"`, no
   quarantine sidecar, no SUSPECT, no `SweepWorkerPoolBrokenError` /
   `SweepStallError` in either log, no budget timeout.
3. **RSS within the envelope (hard):** `deltas.rss_per_worker_kib ≤ 838,861`
   (0.8 GiB = the ledgered MODELED ~0.66–0.73 GB/worker + ~10% headroom)
   AND the ON arm's `pool_peak_rss_kib ≤ 37,748,736` (36 GiB = 75% of the
   §8 48 GB envelope). The report also carries the ACTUAL per-worker and
   pool peaks + headroom — the DEC quotes those numbers.
4. **Wall neutral-or-better (hard):** `wall_ratio_on_over_off ≤ 1.02` — a
   NO-REGRESSION band, deliberately not a win gate. The VALUE section
   surfaces the actual win/loss: the flip should show net
   neutral-to-better, and a within-noise no-win flip is a YELLOW FLAG the
   DEC must address explicitly.
5. **Telemetry invariants (hard):** ON arm exactly one
   `kernel_reuse_summary`; `builds_total + hits_total == tuples_done`;
   `builds_total ≤ keys × workers` (keys = the grid's
   symbols × intervals × fib-anchors × price-source-cells kernel-key
   fan-out); OFF arm silent. Exact builds/hits splits are NEVER asserted
   (worker-split-dependent — the emission's own honesty law).
6. **Provenance (hard):** `hardware_key` matches the registered M4-Pro;
   `commit` is the flip PR's base or a main ancestor; `smoke == false`.

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
