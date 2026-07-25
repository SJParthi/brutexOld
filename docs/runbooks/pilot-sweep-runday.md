# Runbook — First Pilot Sweep Run-Day (lake → brute-force sweep → ranked winners)

> **Target audience:** the operator (sjparthi) on the day the lake first
> holds real index bars, and any Claude session driving the first real
> brute-force sweep.
> **Time required:** minutes for a small pilot grid (serial Stage-2 today —
> see §4 honest envelope); hours-to-days for a full-scale spec sweep.
> **Prerequisites:** a **populated lake** — run `docs/runbooks/data-fetch-runday.md`
> FIRST (this runbook starts where that one ends).

This is the single-paste recipe for the step after the data pull: take the
1-minute index bars already in the lake and run the brute-force engine
(Stages 1A→1B→2→3→4) to produce a ranked, reproducible leaderboard.

It honors CLAUDE.md Golden Rule #1 (no hallucination — every command below
was **live-CLI verified on 2026-06-23** against the actual `brutex` CLI on a
synthetic `--dry-run` lake; see §5 evidence) and Golden Rule #10 (honest
limits — the serial Stage-2 envelope is stated plainly in §4).

> **✅ Live-CLI verified (2026-06-23):** the `fetch` / `sweep-all` /
> `results show` flag lists in this runbook were captured from
> `brutex <cmd> --help` and exercised end-to-end on this build
> (commit `00320c14…`). This supersedes, for the SWEEP path, the
> "flags pending re-verification" banner in `data-fetch-runday.md` §0.

---

## 0. Prerequisite — the lake must hold index bars

This runbook assumes `data-fetch-runday.md` has already run and the lake
holds 1-minute bars for the brute-force indices. Confirm before sweeping:

```bash
.venv/bin/python -m brutex status      # pipeline phase + run_id
ls -R ~/.brutex/lake/bars              # Parquet partitions per index/TF
```

If the lake is empty, STOP and run `data-fetch-runday.md`. A sweep over a
missing slice REFUSES by default with the exact missing list (the
`--allow-missing-slices` guard, DEC-SWEEP-PREFLIGHT-001) — by design, so you
never sweep phantom data.

> 1-minute is the ONLY granularity fetched (CLAUDE.md §3,
> DEC-1MINUTE-ONLY-DERIVE-1DAY-001); the 19 intraday TFs + 1day are DERIVED
> from it. So you may sweep any of the 21 lake interval labels even though
> only `1minute` (and the derived `1day`) are physically pulled — the higher
> TFs resample on read.

---

## 1. Prove the whole pipe with ZERO creds + ZERO real data (optional smoke)

Before the real run, you can prove the full chain offline on a throwaway
lake — `fetch --dry-run` writes synthetic bars through the REAL sink, then a
real `sweep-all` evaluates thousands of combos:

```bash
LAKE=$(mktemp -d)
.venv/bin/python -m brutex fetch --dry-run --lake-root "$LAKE" \
  --start 2024-01-01 --end 2024-03-31 --interval 1minute --rows-per-chunk 400
.venv/bin/python -m brutex sweep-all \
  --symbols NIFTY,BANKNIFTY --exchange NSE --segment INDEX \
  --intervals 1minute --directions BUY,SELL --k 1,2 \
  --start 2024-01-01 --end 2024-03-31 \
  --lake-root "$LAKE" --results-root "$LAKE/sweeps" --top 20 --max-combos 5000
.venv/bin/python -m brutex results show --results-root "$LAKE/sweeps" --top 10
```

Expected on synthetic data: the chain runs (thousands of combos evaluated in
seconds) and persists **zero** finalists — thin random bars (~1,125 over the
window, < 2 trading days) cannot clear the hard quality gates (≥30 trades,
PF ≥ 1.2, MDD ≤ 30%). **Zero fabricated winners is the correct outcome** —
it proves the gates reject noise rather than inventing signal.

---

## 2. The real pilot — small grid first (recommended)

With a populated lake and real dates, run a deliberately SMALL grid first so
the serial Stage-2 finishes quickly and you validate end-to-end on real bars:

```bash
# NSE indices (NIFTY, BANKNIFTY) — 1m + derived 5m, both directions, K=1,2
.venv/bin/python -m brutex sweep-all \
  --symbols NIFTY,BANKNIFTY --exchange NSE --segment INDEX \
  --intervals 1minute,5minute --directions BUY,SELL --k 1,2 \
  --start 2020-01-01 --end <today> --top 1000 --max-combos 5000

# SENSEX lives on BSE — rerun with the BSE exchange partition
.venv/bin/python -m brutex sweep-all \
  --symbols SENSEX --exchange BSE --segment INDEX \
  --intervals 1minute,5minute --directions BUY,SELL --k 1,2 \
  --start 2020-01-01 --end <today> --top 1000 --max-combos 5000
```

Read back the ranked winners (condition NAMES + metrics + gate verdict):

```bash
.venv/bin/python -m brutex results show --results-root ~/.brutex/lake/sweeps --top 20
```

On REAL 5-year data each index carries ~130K+ 1-minute bars (vs. the ~1,125
synthetic), so combos WILL clear the gates and the leaderboard WILL be
non-empty.

---

## 3. Key `sweep-all` flags (live-verified)

| Flag | Meaning | Default |
|------|---------|---------|
| `--symbols` | Comma-separated index symbols | `NIFTY,BANKNIFTY` |
| `--exchange` | Lake exchange partition (`BSE` for SENSEX) | `NSE` |
| `--segment` | Lake segment partition | `INDEX` |
| `--intervals` | Comma-separated lake interval labels to sweep | `1minute,5minute,15minute` |
| `--directions` | `BUY` / `SELL` | `BUY,SELL` |
| `--k` | Conditions-per-combo values | `1,2` |
| `--conditions` | IMPLEMENTED condition bit indices to sweep (0-51 and 54-65; the retired 52/53 are refused) | all 64 active |
| `--sl` / `--tp` | Comma-separated stop-loss / take-profit paisa (`none` to disable a leg) | `none` |
| `--top` | Persist top-N composite-ranked combos per tuple (spec Stage-4 sizing is 1000) | `20` |
| `--max-tuples` / `--max-combos` | Honest opt-in guards — REFUSE a grid larger than this | (off) |
| `--allow-missing-slices` | Proceed (skip) when a `(symbol, interval)` has zero bars | REFUSE |
| `--results-root` | Per-tuple Parquet results dir | `<lake-root>/sweeps` |
| `--stage3-enable` | Opt-in BIPOP-CMA-ES refinement (Stage 3) | off |
| `--catalog` | Run the Stage-4 nine-filter statistical cascade | off |

Determinism (CLAUDE.md Golden Rule #1/#4): every result row carries a BLAKE3
`result_id` over `(strategy_mask ‖ direction ‖ instrument ‖ tf ‖ mode ‖
canonical_params ‖ data_snapshot_hash ‖ indicator_version ‖
code_commit_hash)`. A re-run with the same bars + code is byte-identical.

---

## 3b. Multi-day sweep logging (disk headroom — `nohup`, NOT `tee`)

Run a multi-day sweep under `nohup` with log-file headroom — do **NOT** pipe
through `tee`:

```bash
nohup .venv/bin/python -m brutex sweep-all … > sweep.log 2>&1 &
```

Two measured reasons (guarantee-audit evidence, phase3/W12):

- **DEBUG stdout is a 2–40 GB flood on a multi-day run.** This is bounded by
  DEC-SWEEP-LOG-VOLUME-GUARD-001 (MIGRATION.md #1630): the sweep CLI now
  configures structlog at **INFO by default** (override with
  `BRUTEX_LOG_LEVEL=DEBUG` only when you explicitly want the per-trade stream
  and have re-accepted the disk cost). Still provision adequate disk for
  `sweep.log` — a disk-full mid-sweep is a `LakeWriteHalt` run-killer.
- **A `tee` process death kills the sweep in 0.19 s.** If the `tee` in a
  `… | tee sweep.log` pipeline dies (OOM, terminal close, disk-full on the
  tee side), the sweep receives `SIGPIPE`/`EPIPE` and dies almost instantly —
  measured 0.19 s. `nohup … > sweep.log 2>&1 &` writes directly to the file
  with no fragile pipe in the middle and survives a detached terminal.

---

## 4. Honest envelope (Golden Rule #10)

| Reality | Implication |
|---------|-------------|
| **Stage-2 is SERIAL today** (no 12-worker parallelism — a deferred Phase-2b optimization) | A full-scale 730B-trial sweep is ~80–160 h on the M4 Pro envelope. **Keep the first pilot small** (the §2 grid ≈ thousands of combos, minutes–low hours). Scaling needs the Stage-2 parallelism work (designed hostile-reviewed before any hot-path code). |
| Stage-1B coarse pre-filter drops masks below `MIN_TRADES` (30) | Most low-activity masks never reach the expensive trade-sim — this is why a 5K-combo grid finishes fast. |
| Hard gates reject thin/over-fit combos (≥30 trades, PF ≥ 1.2, MDD ≤ 30%, …) | An empty leaderboard on thin data is correct, NOT a bug. |
| Stage-3 (CMA-ES) + Stage-4 (9 filters) wall-time on real 5-yr data is **unmeasured** | Run a pilot first; measure before enabling `--stage3-enable` / `--catalog` on a large pool. |
| The brute-force ENGINE scope is the 3 indices only (CLAUDE.md §3) | NIFTY / BANKNIFTY / SENSEX. The F&O lake is REFERENCE_ONLY — not swept. |

---

## 5. Verification evidence (2026-06-23, this build)

Run end-to-end on a synthetic `--dry-run` lake (commit `00320c14…`):

- `fetch --dry-run` → 3,375 synthetic bars written (NIFTY/BANKNIFTY/SENSEX).
- `sweep-all` (8 tuples) → **5,512 combos enumerated, 5,408 evaluated in
  ~10 s**; Stage-1B `minhits_dropped=1188`, `conflict_pruned=104`,
  trade-sim forced-1520 exits, `persisted=0 ranked=0` (correct — synthetic
  data fails the gates), audit chain + checkpoint + progress files written.
- `results show` → reads back 8 tuple files, `finalists=0` (honest).
- Audit JSONL `SWEEP_START` / `SWEEP_FLUSH` rows carry the **real**
  `code_commit_sha` (`00320c14…`) + hash-chained `entry_hash` — Golden
  Rule #1 reproducibility with a real hash, not a placeholder.

The largest automated end-to-end test today is the 16-tuple
`tests/integration/test_engine_sweep_orchestrator.py` (passes, deterministic
across two runs). No automated test exercises a 100K+ combo grid on real
lake data — that is the pilot this runbook drives.

---

## 6. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `sweep-all` REFUSES with a missing-slice list | requested `(symbol, interval)` has zero lake bars | pull it (`data-fetch-runday.md`), or pass `--allow-missing-slices` to skip |
| `ModuleNotFoundError: fno_math` | Rust kernel not built | `make setup` (the engine path needs the `trading_core`/`fno_math` wheel) |
| Empty leaderboard on REAL data | gates rejecting (genuinely no combo cleared) OR window too short | widen `--start/--end`; inspect with a lower-bar pilot; this may be a true negative |
| Sweep slower than expected | serial Stage-2 over a large grid | shrink the grid (`--symbols`, `--intervals`, `--k`, `--conditions`, `--max-combos`); see §4 |

---

## 7. Decision trail

- **Engine readiness (Stages 1A→4 built + tested):** reconnaissance audit 2026-06-23.
- **Serial Stage-2 / Phase-2b parallelism deferral:** CLAUDE.md §8 SLA envelope; Golden Rule #10.
- **Run identity / determinism (BLAKE3):** CLAUDE.md Golden Rule #4; `TRACK1_BACKTEST_SPEC.md` run-identity formula.
- **Sweep scope = 3 indices:** CLAUDE.md §3 (Golden Rule #3); F&O lake REFERENCE_ONLY (DEC-P0-063).
- **Prerequisite fetch run-day:** `docs/runbooks/data-fetch-runday.md`.
