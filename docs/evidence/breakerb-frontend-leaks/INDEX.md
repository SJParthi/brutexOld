# Breaker B frontend-leak evidence (handoff for PR #1572)

Durable git handoff ref for the real-Chromium Playwright harnesses + measured
results from the 2026-07-09 Breaker B space audit of the dashboard frontend.
This branch (`claude/breakerb-1572-evidence`) exists as a **rollback-durable
handoff for PR #1572's handler** — it is evidence for that PR's bx-datepicker
fix. It deliberately has **NO PR** (the `claude/pr1439-coverage-fix-handoff`
pattern: no PR means auto-merge can never touch it).

All four sibling files are **byte-exact copies** of the audit scratchpad
originals (`cmp`-verified at copy time).

## What each file is

| File | What it is |
|---|---|
| `n5_datepicker.js` | Real-Chromium Playwright harness for the **bx-datepicker document-listener leak**: each `wireLooseInput` → `buildCalendar` added TWO never-removed `document` listeners (outside-click + Escape) while db.html innerHTML-rebuilds `#cstart`/`#cend` per interaction. **Pre-fix measured:** +4 document listeners/rebuild, +103 retained DOM nodes/rebuild (through forced GC), document click dispatch degraded 0.20 → 4.60 ms over 1,000 rebuilds. **Verified FIXED** with the delegated-listener design: constant +2 listeners total (ONE delegated click + ONE delegated keydown) regardless of rebuild count, detached trees GC-collected. |
| `n3_ringseen.js` | Real-Chromium Playwright harness for the **bx-oneclick `ringSeen` unbounded growth**: every event's canonical dedupe line enters `state.ringSeen` before the 5000-cap ring trim and trimmed keys are never evicted. **Measured:** 153.6 B/event, +146.9 MB heap over 1M unique events (linear through forced GC); control (repeating ids, same poll count) flat. |
| `README.md` | The original audit hand-off notes for both harnesses: findings, exact measured numbers, run commands, fix classes, and the post-fix expectations. |
| `postfix-results.md` | The independent **post-fix verification numbers** for the datepicker fix (harness N5, unmodified logic, same Chromium/protocol): post-fix +0.00 listeners/rebuild (constant 3 total), nodes sawtooth-bounded, click dispatch flat 0.50 ms — plus a teeth check re-reproducing the pre-fix +4/rebuild on the old file, and the PR #1368 head-state snapshot. |

## How to run the harnesses

1. Patch the hardcoded `executablePath` in each harness to the local
   `/opt/pw-browsers` chromium (originals pin `chromium-1194/chrome-linux/chrome`).
2. Install Playwright against the preinstalled browser:
   `PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm i playwright`
3. Point `STATIC_DIR` at the `brutex/dashboard/static` of the branch under
   test (the harnesses load the REAL static JS verbatim via `page.route`
   interception).
4. Run:
   - `node n5_datepicker.js leak 1000` / `node n5_datepicker.js control 1000`
   - `node n3_ringseen.js growth 5025` / `node n3_ringseen.js control 2513`

Chromium flags used by the harnesses: `--enable-precise-memory-info
--js-flags=--expose-gc --no-sandbox`.
