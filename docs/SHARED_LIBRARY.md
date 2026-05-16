# BruteX as a Shared Library — One Source of Truth for Backtest + Live TickVault

> **Status:** Locked 2026-05-16. Project rule.
> Operator decision: "pip install from git URL with version pin (Recommended)".

## The principle (NEVER copy-paste)

The `brutex` Python package is the **single source of truth** for:
- Trading calendars (NSE/BSE 2021-2026)
- All indicators (EMA, Fib, Pivots, Day Hi/Lo, Prev Day OHLC, etc.)
- Timeframe definitions (21 TFs)
- Resamplers (1m → 21 TFs)
- Strategy vocabulary (52 atomic boolean conditions — future)
- Data adapters (Dhan HTTP + Dhan WebSocket — future)

Two consumers of this library:

| Consumer | Repo | Role | Mode |
|---|---|---|---|
| **BruteX** (this repo) | `github.com/SJParthi/brutex` | Backtest engine | Batch / historical |
| **TickVault** | `github.com/SJParthi/tickvault` (future) | Live trading engine | Streaming / real-time |

**Both consume the SAME `brutex.*` modules.** Same imports, same classes, same math, bit-identical output.

## How TickVault imports the shared code

### TickVault's `pyproject.toml`

```toml
[project]
name = "tickvault"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "brutex @ git+https://github.com/SJParthi/brutex.git@v0.3.0",
    # ... TickVault-specific deps (websocket-client, redis, kafka-python, etc.)
]
```

### TickVault's Python code

```python
# tickvault/live/engine.py — same imports as a backtest module
from brutex.indicators import (
    EMAState,
    DayExtremesState,
    compute_fib_levels,
    compute_pivot_levels,
    lookup_prev_day_ohlc,
)
from brutex.calendars import is_trading_day, IST, FORCED_EXIT_IST
from brutex.data.timeframes import ALL_TF_MINUTES, label as tf_label

# Live tick loop — same code path as backtest replay
ema_20 = EMAState.new(period=20)
ema_200 = EMAState.new(period=200)
day_ext = DayExtremesState()

for tick_bar in dhan_websocket_feed:
    if not is_trading_day(tick_bar.ts_ist.date()):
        continue
    if tick_bar.ts_ist.time() >= FORCED_EXIT_IST:
        flatten_all_positions()
        continue
    ema_20_val = ema_20.update(tick_bar.close)
    ema_200_val = ema_200.update(tick_bar.close)
    day_high, day_low = day_ext.update(
        tick_bar.ts_ist, tick_bar.high_paisa, tick_bar.low_paisa
    )
    # ... make live trading decision (identical to backtest decision logic)
```

## Workflow when BruteX changes (FULLY AUTOMATED)

Operator requirement 2026-05-16: "After some days I can't monitor this.
The process needs to be entirely automated — nowhere should I have to
click or tag manually."

```
   1. You make a change in BruteX:
      $ vim brutex/indicators/ema.py
      $ git commit -m "fix(ema): off-by-one in seed"
      $ git push

   2. CI runs ✅ (tests, lints, streaming-parity)
                  │
   3. Release Please reads the commit:
      "fix(ema):" → PATCH bump (0.3.0 → 0.3.1)
                  │
   4. Release Please auto-creates a Release-PR:
      - bumps pyproject.toml version
      - regenerates CHANGELOG.md
                  │
   5. CI runs on the Release-PR ✅
                  │
   6. Auto-merge fires (already enabled per
      operator decision 2026-05-16 — docs-only PRs auto-merge)
                  │
   7. Git tag v0.3.1 created automatically
                  │
   8. GitHub Release v0.3.1 created automatically
                  │
   9. Telegram alert sent to your phone:
      "🎉 BruteX v0.3.1 released. fix(ema): off-by-one in seed"
                  │
  10. (Future) Renovate detects new tag, auto-opens PR in
      TickVault repo bumping "brutex @ ...@v0.3.0" to "...@v0.3.1".
      TickVault CI runs → auto-merge → TickVault auto-deploys.

   YOU NEVER TAGGED. YOU NEVER BUMPED. YOU NEVER CLICKED.
```

## Conventional commit format (the contract with the automation)

| Commit prefix | Version bump | Example |
|---|---|---|
| `fix(<scope>):` | PATCH (0.X.Y → 0.X.Y+1) | `fix(ema): off-by-one in seed` |
| `feat(<scope>):` | MINOR (0.X.Y → 0.X+1.0) | `feat(indicators): add Bollinger Bands` |
| `feat(<scope>)!:` | MAJOR (0.X.Y → 1.0.0) | `feat(indicators)!: rename EMAState.update → tick` |
| `docs(<scope>):` | NO BUMP | `docs(spec): update phase plan` |
| `chore(<scope>):` | NO BUMP | `chore(deps): bump polars to 1.41` |
| `test(<scope>):` | NO BUMP | `test(ema): add property tests` |
| `refactor(<scope>):` | NO BUMP | `refactor(indicators): split fib module` |
| `day1(<scope>):` | Treated as `feat:` by config | (sprint commits during Day 1) |

The `!` after the scope indicates a breaking change (forces MAJOR bump).
Same convention as the popular `semantic-release` ecosystem.

## The release workflow file

Lives at `.github/workflows/release.yml`. Uses Google's Release Please
action (`googleapis/release-please-action@v4`). Triggers:
- On push to `main`.
- Reads commit messages since the last release tag.
- Creates / updates a Release-PR.
- On merge of Release-PR → tags + GitHub release + (optional) PyPI publish.

## Cross-repo automation (BruteX → TickVault, when TickVault exists)

| Tool | Free | Setup time | What it does |
|---|---|---|---|
| **Renovate** (GitHub App) | ✅ Yes | 5 min one-time install | Auto-PR in TickVault when BruteX releases a new version |
| **Dependabot** (GitHub built-in) | ✅ Yes | Already enabled per repo | Same as Renovate but less configurable for git+url deps |

We'll use **Renovate** because it supports `git+https://` dependencies
better than Dependabot (Renovate detects new git tags automatically).

Renovate config at `.github/renovate.json` will look like:
```json
{
  "extends": ["config:recommended", ":automergePatch"],
  "git-submodules": { "enabled": false },
  "gitUrls": "https",
  "packageRules": [
    {
      "matchPackageNames": ["brutex"],
      "matchSourceUrls": ["https://github.com/SJParthi/brutex"],
      "automerge": true,
      "automergeType": "branch",
      "schedule": ["at any time"]
    }
  ]
}
```

This makes TickVault auto-bump and auto-merge BruteX version bumps
within 1 minute of a BruteX release.

## What this means for the operator

Once both repos are set up with this automation:

| Action | Operator effort |
|---|---|
| Fix a bug in BruteX | 1 commit + push → AUTO release → AUTO TickVault update → AUTO deploy |
| Add a new indicator | 1 commit + push → AUTO release → AUTO TickVault update |
| Breaking API change | Same — just include `!` in commit message |
| Hotfix during live trading | Same — push fix, everything propagates in <5 min |
| Monitor releases | Read Telegram alerts; otherwise ignore |

**Operator monitoring required: ZERO.** Just write good commit messages.

## Versioning rules (semver)

| Change kind | Version bump | Examples |
|---|---|---|
| Bug fix (no API change) | PATCH (0.3.0 → 0.3.1) | EMA off-by-one fix, calendar holiday correction |
| New feature (additive, no breakage) | MINOR (0.3.0 → 0.4.0) | New indicator added, new TF supported |
| Breaking API change | MAJOR (0.3.0 → 1.0.0) | EMAState.update() signature change |

**Both BruteX and TickVault pin to specific versions** (e.g., `@v0.3.1`),
NOT `@main`. Pinning prevents surprise breakage in production.

## What BruteX must do to stay shareable

1. **Keep `brutex/` clean.** Anything backtest-specific lives in
   `brutex/backtest/`, `brutex/orchestrator/`, `brutex/dashboard/`, etc.
   Shared parts (`brutex/indicators/`, `brutex/calendars/`, `brutex/data/`,
   `brutex/adapters/`, `brutex/strategies/`) MUST NOT import from
   backtest-specific modules.

2. **No backtest-only assumptions** in shared modules:
   - ❌ Don't assume all data is available upfront (live has streaming data)
   - ❌ Don't assume bars come in batches (live has one-at-a-time)
   - ✅ Stateful classes (EMAState, DayExtremesState) work for both
   - ✅ Snapshot functions (compute_fib_levels, compute_pivot_levels) work for both

3. **Online/offline parity tests** must always pass:
   - `tests/unit/test_streaming_parity.py` proves that for every indicator
     with both batch and streaming APIs, the two paths produce
     bit-identical output. Run on every CI push.

4. **Tag releases on every meaningful change** so consumers can pin
   to specific versions.

## Future evolution

If BruteX grows too large or the shared parts become significantly
out of step with backtest-specific code, we may extract:

```
   brutex-core/       # NEW repo: just shared parts (indicators, calendars, data)
   brutex/            # backtest-only (depends on brutex-core)
   tickvault/         # live-only  (depends on brutex-core)
```

For now (Phase 0.5 — Day 1 sprint), everything stays in the single
BruteX repo. Migration is deferred.

## Test contract

Every commit on `brutex/indicators/`, `brutex/calendars/`, or
`brutex/data/` MUST:

1. Pass all unit tests (no regressions).
2. Pass streaming-parity tests if the module has both APIs.
3. Pass look-ahead invariant tests (Property P-06).
4. Include test coverage for any new branch.

CI fails the PR otherwise. This is enforced by the repository's CI
workflow (see `.github/workflows/`).
