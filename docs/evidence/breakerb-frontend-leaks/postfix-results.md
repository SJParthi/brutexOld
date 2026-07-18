# bx-datepicker leak fix — independent post-fix verification (REAL Chromium, Breaker B harness N5)

Date: 2026-07-09 · Verifier branch: `leakverify` @ `50e3e01d` (origin/claude/design-system-batch-a)
Harness: `n5_datepicker.js` (unmodified logic; STATIC_DIR pointed at the worktree's
`brutex/dashboard/static`, executablePath `/opt/pw-browsers/chromium-1194/chrome-linux/chrome`).
Playwright + real Chromium 1194, headless, `--expose-gc --enable-precise-memory-info`.

## 1. POST-FIX — leak mode, 300 rebuilds (FIXED bx-datepicker.js @ 50e3e01d)

| checkpoint | docListeners | byType | nodes | jsListeners | heap MB |
|---|---|---|---|---|---|
| after 1 rebuild | 3 | DCL 1 / click 1 / keydown 1 | 128 | 8 | 0.75 |
| 100 | 3 | DCL 1 / click 1 / keydown 1 | 439 | 20 | 0.79 |
| 200 | 3 | DCL 1 / click 1 / keydown 1 | 655 | 28 | 0.79 |
| 300 (final) | 3 | DCL 1 / click 1 / keydown 1 | 231 | 12 | 0.82 |

Per-rebuild: **+0.00 document listeners**, +0.3 retained DOM nodes (sawtooth, GC-collectable —
final 231 is BELOW the 100-rebuild sample; not linear), +0.23 KB JS heap, +23.92 KB renderer RSS.
One document click dispatch: **0.50 ms** with 3 document listeners (flat).

The datepicker now holds exactly ONE delegated click + ONE delegated keydown on document
(+2 total over the harness's own DOMContentLoaded) regardless of rebuild count — the
expected constant delegated pair, NOT +4/rebuild.

## 2. POST-FIX — control mode, 300 rebuilds (same file, wiring skipped)

| checkpoint | docListeners | nodes | jsListeners | heap MB |
|---|---|---|---|---|
| after 1 rebuild | 1 (DCL only) | 104 | 2 | 0.73 |
| 100 | 1 | 104 | 2 | 0.76 |
| 200 | 1 | 104 | 2 | 0.76 |
| 300 (final) | 1 | 104 | 2 | 0.76 |

Per-rebuild: +0.00 listeners, +0.0 nodes, +0.10 KB heap, +17.44 KB RSS. Click dispatch 0.10 ms.
Leak-vs-control delta post-fix ≈ constant +2 listeners / small bounded node churn — the
datepicker's own retention is closed; residual RSS drift is browser-baseline noise (control
shows +17 KB/rebuild too).

## 3. TEETH CHECK — PRE-fix bx-datepicker.js (git show 7074c166) in a temp static dir, leak 300

| checkpoint | docListeners | byType | nodes | jsListeners | heap MB |
|---|---|---|---|---|---|
| after 1 rebuild | 5 | DCL 1 / click 2 / keydown 2 | 128 | 10 | 0.74 |
| 100 | 401 | click 200 / keydown 200 | 10,337 | 802 | 1.03 |
| 200 | 801 | click 400 / keydown 400 | 20,629 | 1,602 | 1.30 |
| 300 (final) | 1,201 | click 600 / keydown 600 | 30,935 | 2,402 | 1.58 |

Per-rebuild: **+4.00 document listeners**, +103.0 retained DOM nodes, +2.87 KB JS heap,
+87.05 KB renderer RSS. Click dispatch degraded to **1.40 ms** at 1,201 document listeners.
The harness still bites: the exact +4/rebuild (2 inputs × click+keydown) linear growth
reproduces on the pre-fix file under the identical run.

## 4. VERDICT

**FIX VERIFIED — YES.** Same harness, same Chromium, same 300-rebuild protocol:
pre-fix +4.00 listeners/rebuild & +103 nodes/rebuild (linear) → post-fix +0.00 listeners
(constant 3 total = harness DCL + ONE delegated click/keydown pair), nodes sawtooth-bounded,
click dispatch flat at 0.50 ms vs 1.40 ms degraded.

Raw JSON: `leakverify-work/n5_result_leak.json`, `n5_result_control.json`
(post-fix runs; the pre-fix run overwrote `n5_result_leak.json` last — numbers above are from
the captured stdout of each run, in run order: post-fix leak, post-fix control, pre-fix leak).

## 5. PR #1368 head state (queried 2026-07-09 ~16:15Z)

- Head SHA: **`50e3e01d55bdc81cd06148bf2306695123b1c2d3`** ✓ (expected 50e3e01d…), draft, `mergeable_state=blocked` (draft + pending checks).
- Check runs on head (4 total): gitleaks secrets scan ✅ success; Dependency-audit jobs ×3
  (Python pip-audit, cargo-audit fno-math, cargo-audit trading-core) QUEUED.
- **CI (ci.yml) wave: run 29032219905-sibling id 29032220034 status `pending`, 0 jobs spawned yet**
  (workflow triggered 16:07:35Z; combined commit status also `pending`, total_count 0).
- **bench-m4pro / m4pro-absolute-throughput on the NEW head: ABSENT — verified.** Neither the
  check-runs list nor the workflow-runs list shows any bench-m4pro run for 50e3e01d. The push
  delta vs 7074c166 is exactly `docs/MIGRATION.md` (f8aa8ad4) + `brutex/dashboard/static/bx-datepicker.js`
  (50e3e01d) — neither matches bench-m4pro.yml's `paths:` filter (bench tests +
  `brutex/engine/sweep_parallel.py` etc.) → no m4pro check-run is created on this head, so any
  earlier head's m4pro state is superseded/not carried. (Honest note: the immediately-prior head
  7074c166's bench-m4pro run 29027588127 actually completed SUCCESS at 14:58Z — the "old red",
  if any, was on an earlier head still; either way it is not present on 50e3e01d.)
