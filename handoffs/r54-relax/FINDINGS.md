# FINDINGS — PR #1284 none-SL/TP-leg refusal vs OPERATOR RULING R54 (2026-07-08)

Verification target: `origin/claude/sweep-p1-preflight-estimator`,
head **86068ce0fce3d7c39ba845dbf36e9eb0e9f09810**
(`chore(merge): fold origin/main f028fe6 into claude/sweep-p1-preflight-estimator`).
Worktree: `/tmp/claude-0/-home-user-bruteX/844055d5-494f-5343-9d11-82b65c969bdf/scratchpad/wt-1284` (detached, never pushed, no named branch touched).

Operator ruling source: `/tmp/claude/memory/team/project/sweep-sltp-none-legs-runnable.md` —
"institutional defaults may remain the DEFAULT, but `sl="none"`/`tp="none"` legs MUST remain
first-class RUNNABLE — explicit opt-in is acceptable; a hard refusal is NOT."
(R54: both SL/TP arms AND no-SL/TP arms must be coverable.)

---

## STEP 1 — evidence (all line numbers at base 86068ce0)

### (a) WHERE the none-leg refusal is raised, and under what conditions

- **Primary (dashboard) refusal**: `brutex/dashboard/sweep_runner.py:315`
  `_validate_mode_a_legs(sl, tp)` — raise at **:349-353**:
  `"{label}='none' is incompatible with the institutional Stage-3 CMA-ES grid
  (stage3_enable=True is mandatory for every dashboard sweep launch) — provide
  on-grid {label} paisa value(s) instead"`.
  Called **unconditionally** from `resolve_sweep_request` at
  `brutex/dashboard/sweep_runner.py:474` (after `_normalise_leg` at :472/:473).
  It is unconditional on this path BECAUSE `build_sweep_kwargs`
  (`sweep_runner.py:571`) pins `"stage3_enable": True` for EVERY dashboard
  launch (the round-11 "refuse none-with-stage3" fix — the PR's R5
  institutional flip made stage3 mandatory, so the refusal fires for any
  explicit `sl=none`/`tp=none` regardless of any other field).
- **Underlying engine refusal** (what round-11 was protecting against):
  `brutex/engine/sweep_orchestrator.py:402-427`
  `SweepGridConfig.__post_init__` — fires **ONLY when `stage3_enable=True`**
  (`if self.stage3_enable:` at :402; `_require_on_grid` rejects a `None`
  SL/TP value + off-Mode-A-grid values, ADR-0007 condition 3).
  With `stage3_enable=False` the engine ACCEPTS `None` SL/TP legs.
- NOT in `brutex/engine/sweep_oneclick_preflight.py` — that module carries
  NO SL/TP-leg logic at all (grep: zero sl/tp/stage3 references); the
  refusal lives entirely in the dashboard request resolver + the engine
  config guard above.

### (b) Which production launch paths hit the refusal

| Path | Trace | Hits refusal? |
|---|---|---|
| `POST /api/v1/sweep/run` (dashboard "Run Sweep") | `app.py:6096` → `resolve_sweep_request` (`app.py:6144`) → `_validate_mode_a_legs` (`sweep_runner.py:474` → :349) | **YES** for any explicit `sl=none` or `tp=none` (400). Blank form uses on-grid `DEFAULT_SL="50,100,150"`/`DEFAULT_TP="100,200,300"` (`sweep_runner.py:106-107`), so the no-SL/TP arm is UNREACHABLE on this surface — hard refusal, non-compliant with R54. |
| `GET /api/v1/sweep/oneclick/preflight` (`app.py:6305`) + `POST /api/v1/sweep/oneclick/run` (`app.py:6366`) | both → `_oneclick_plan_from_query` (`app.py:~6235`) → `resolve_oneclick_sweep_plan` (`sweep_runner.py:646`) → `resolve_sweep_request` per exchange group (:696) → same refusal | **YES** — identical refusal per group; the one-click institutional surface cannot express a none-leg arm at all. Non-compliant with R54. |
| CLI `brutex sweep-all` | `cli.py:375-381` `--sl`/`--tp` **default "none"**; `cmd_sweep_all(stage3_enable: bool = False)` (`cli.py:6244`); engine gate only under `--stage3-enable` (`sweep_orchestrator.py:402`) | **NO** — the CLI DEFAULT launch is itself a none-leg launch and runs. (`--sl none --tp none --stage3-enable` together still refuses at engine construction — a genuine incompatibility, unchanged by this patch.) |
| Dashboard launch-button cards (`/launch`) | `launch_button.py:186-187` `LOCKED_SL="none"`/`LOCKED_TP="none"` → `locked_sweep_argv` (`launch_button.py:284`) spawns detached `brutex.cli sweep-all --sl none --tp none` (no `--stage3-enable`) | **NO** — rides the CLI path above. |

Verdict: the refusal **bites both dashboard production launch surfaces**
(`/sweep/run` + the one-click preflight/run pair). The no-SL/TP arm the
operator requires coverable is structurally unreachable from the dashboard.

### (c) Existing exemption flag / env / config?

**NONE.** `grep -n 'environ\|getenv\|BRUTEX_'` over
`brutex/dashboard/sweep_runner.py` → zero hits. The only ack knob on the
path is `acknowledge_sla_overrun` (SLA proceed-dialog only — threads to
`cmd_sweep_all`'s parameter of the same name; it does not touch leg
validation). `build_sweep_kwargs` hard-pins `stage3_enable=True` (base
:571) with no request field, env, or config able to change it. The PR's own
module docstring (base :27-36) states the none leg is "structurally
incompatible with the institutional Stage-3 default today; this is an OPEN
engineering question for the operator" — the operator has now answered it
(R54), requiring the relax.

⇒ STEP 2 patch REQUIRED (hard refusal on production launch paths, no compliant override).

---

## STEP 2 — the minimal relax (patch prepared, NOT committed, NOT pushed)

Files touched (full diff: `PATCH.diff` beside this file; 126 insertions / 44 deletions):

1. `brutex/dashboard/sweep_runner.py`
   - `_validate_mode_a_legs`: the none arm becomes **WARN-not-refuse** — ONE
     `sweep_runner.none_leg_stage3_downgrade` structlog WARNING per launch
     request, naming the none legs + the operator ruling (2026-07-08, R54);
     the off-Mode-A-grid refusal on CSV legs is **byte-kept** (the round-11
     deep-crash class stays closed). New `_LOG = structlog.get_logger(__name__)`
     (matches `launch_button.py` style).
   - `build_sweep_kwargs`: `"stage3_enable": request.sl != "none" and
     request.tp != "none"` — a none leg downgrades Stage-3 OFF for that
     launch (CMA-ES cannot seed x0 from a none leg, ADR-0007 condition 3;
     with Stage-3 off the engine ACCEPTS None legs —
     `sweep_orchestrator.py:402` — so the launch is provably runnable, R16
     preserved, never a deep engine crash). Gridded legs keep the R5
     institutional `stage3_enable=True` default (the operator ruling
     explicitly allows the institutional default to stay the DEFAULT).
   - Module docstring + `resolve_sweep_request`/`build_sweep_kwargs`
     docstrings reconciled to R54 (the "OPEN engineering question" paragraph
     resolved; every note cites "operator ruling 2026-07-08, R54").
2. `tests/unit/test_dashboard_sweep_runner.py`
   - `test_explicit_none_leg_now_refused` (the round-11 pin) REPLACED by five
     R54 tests: `test_explicit_none_sl_leg_is_runnable_r54`,
     `test_explicit_none_tp_leg_is_runnable_r54`,
     `test_both_none_legs_runnable_with_one_warn_r54` (capture_logs — exactly
     ONE warn naming both legs),
     `test_gridded_legs_emit_no_downgrade_warn_and_keep_stage3_r54`
     (complement pin: institutional default intact), and
     `test_mixed_none_and_offgrid_leg_still_refused_r54` (off-grid refusal intact).

Deliberately NOT changed: every other refusal (off-grid legs, k>max_combos,
scope guard, SLA guard, dates, top), the engine `SweepGridConfig` guard
(`none` + `stage3_enable=True` together is a real incompatibility), the CLI,
`launch_button.py`, and the preflight/estimator modules. Follow-up for the PR
author (out of minimal scope here): note the R54 reconcile in the
DEC-SWEEP-ONECLICK-PREFLIGHT-ESTIMATOR-001 MIGRATION.md ledger entry, per the
operator ruling's documentation clause.

## Fails-before / passes-after proof

Fails-BEFORE (base `sweep_runner.py` restored via
`git stash push -- brutex/dashboard/sweep_runner.py`, new tests kept):

    FAILED tests/unit/test_dashboard_sweep_runner.py::test_explicit_none_sl_leg_is_runnable_r54
    FAILED tests/unit/test_dashboard_sweep_runner.py::test_explicit_none_tp_leg_is_runnable_r54
    FAILED tests/unit/test_dashboard_sweep_runner.py::test_both_none_legs_runnable_with_one_warn_r54
    FAILED tests/unit/test_dashboard_sweep_runner.py::test_mixed_none_and_offgrid_leg_still_refused_r54
    4 failed, 1 passed, 52 deselected in 0.84s

(the 1 pass is `test_gridded_legs_emit_no_downgrade_warn_and_keep_stage3_r54` —
by design a pin of UNCHANGED gridded-leg behavior, true at base and after.)

Passes-AFTER (patched worktree):

    tests/unit/test_dashboard_sweep_runner.py → 57 passed in 1.31s
    tests/unit/test_dashboard_sweep_run_route.py + test_dashboard_sweep_oneclick_route.py
      + test_dashboard_oneclick_frontend.py + test_engine_sweep_oneclick_preflight.py
      → 127 passed, 1 skipped (node-absent CI-only guard) in 12.95s

## Gate tails (worktree, fresh py3.12 venv: `.[dev,data,ui]` + locally-built
`rust/trading-core` + `rust/fno-math` wheels + `cma`)

- Coverage (branch bar: per-module 100%):
  `brutex/dashboard/sweep_runner.py  210 stmts 0 miss  54 branch 0 partial  100.00%`
  (`--cov=brutex.dashboard.sweep_runner --cov-branch` over the runner + both route suites, 91 passed)
- `ruff check` touched files: `All checks passed!`
- `ruff format --check` touched files: `2 files already formatted`
- `mypy --strict --no-incremental brutex/dashboard/sweep_runner.py`:
  `Success: no issues found in 1 source file` (after installing the `cma`
  optional dep; without it the SAME single pre-existing `stage3_refine.py`
  import-not-found error appears at base AND patched — 0 new diagnostics
  either way).
- `pyright` touched files: `0 errors, 3 warnings` — identical at base
  (verified by stash/pop): the 3 warnings are pre-existing
  `reportPrivateUsage` on `_axes_counts` test usages, untouched by this
  patch. 0 new diagnostics.

Honest env notes: the repo has no committed venv; a fresh Python 3.12.3 venv
was built in the scratchpad (host default python3 is 3.11, below
`requires-python >=3.12`). Rust wheels were built locally with cargo 1.94.1.
Nothing was committed and nothing was pushed; the worktree holds the patch as
uncommitted working-tree changes, exported to `PATCH.diff`.

---

## REVIEW ROUND 2 (2026-07-09) — the 8 confirmed findings FIXED in-worktree

Verdict context: round-2 verification confirmed the round-1 patch relaxed only
the WHOLE-STRING `none` leg — the operator's real mixed shape was still
hard-refused, the none+off-grid combination was refused on a disabled
justification, the WARN misfired, the downgrade was server-log-only, and the
ruling's ledger clause was unfulfilled. ALL EIGHT findings are now fixed in the
worktree (uncommitted; `PATCH.diff` regenerated — 5 files, 626 insertions /
51 deletions vs base 86068ce0).

### Fixes (finding → change)

1. **[BLOCKER] mixed CSV none-leg hard-refused** → `_normalise_leg` now parses
   PER VALUE, mirroring `brutex.cli._parse_paisa_list` (`none` any-case →
   `none`, positive ints, whitespace-tolerant), so `sl="none,5,100,200"`
   (PR #1297's default) resolves on BOTH dashboard surfaces. End-to-end pin:
   `test_mixed_csv_none_leg_is_runnable_end_to_end_r54` drives
   resolve → `build_sweep_kwargs` → `_parse_paisa_list` → a CONSTRUCTIBLE
   `SweepGridConfig` (12 tuples, `None in grid.stop_losses`). Route pins:
   `test_mixed_csv_none_leg_launch_accepted_r54` (POST /sweep/run 200) +
   `test_run_route_mixed_none_leg_launch_accepted_r54` (oneclick run).
2. **[MAJOR] none+off-grid refused citing disabled Stage-3** →
   `_validate_mode_a_legs` gates the Mode-A axis check on the launch's
   EFFECTIVE posture: any `none` cell (⇒ stage3 OFF, engine accepts any
   positive paisa) stands the check down for the whole launch;
   `sl="none", tp="1500"` now resolves + engine-constructs
   (`test_none_leg_with_offgrid_other_leg_is_runnable_r54`). Fully-gridded
   launches keep the round-11 off-grid refusal byte-intact
   (`test_off_grid_sl_now_refused`/`_tp_` unchanged, still green).
3. **[MINOR] triplicated whole-string none checks** → NEW public
   `leg_values`/`leg_has_none` (exported) are the ONE parsed-leg source
   consumed by `_validate_mode_a_legs`, the NEW derived
   `SweepRunRequest.stage3_enable` property, and `build_sweep_kwargs`.
   Parity pinned token-for-token against `cli._parse_paisa_list`
   (`test_leg_values_mirrors_the_cli_parse_paisa_list_grammar`) and the
   stage3-agrees-with-parsed-legs law pinned over 8 accepted shapes
   (`test_stage3_enable_agrees_with_the_parsed_legs_for_every_accepted_shape`).
4. **[MINOR] WARN misfires on refused requests** → the WARN moved OUT of
   request resolution INTO `build_sweep_kwargs` (= launch-side kwargs build);
   a refused request and the read-only preflight emit ZERO warns
   (`test_refused_request_emits_no_downgrade_warn_r54`,
   `test_oneclick_none_leg_plan_resolves_quietly_and_warns_once_per_group_kwargs_r54`).
5. **[MAJOR] downgrade invisible on operator payloads** → (a)
   `SweepRunRequest.to_dict` carries the derived `stage3_enable`, so the
   `POST /api/v1/sweep/run` response payload discloses it (route-pinned,
   `test_none_leg_launch_accepted_with_stage3_disclosure_r54` +
   `test_gridded_launch_payload_reports_stage3_true_r54`); (b)
   `oneclick_preflight_for_plan` appends ONE
   `Clamp(field="stage3_enable", requested="true", applied="false", reason=…)`
   per downgraded exchange group — the reason names the exchange, the
   none-carrying leg(s), R54, and states honestly that the launch's gridded
   cells lose Stage-3 CMA-ES refinement too (unit + route pinned). ZERO
   `app.py` edits — both disclosures live in `sweep_runner.py`.
6. **[MINOR] ledger clause unfulfilled** → the R54 none-leg reconcile
   addendum appended to the PR's own ledger entry #1631
   (DEC-SWEEP-ONECLICK-PREFLIGHT-ESTIMATOR-001 — this branch's home for the
   memo's "DEC-SWEEP-PREFLIGHT-ESTIMATOR-001" clause; that exact id does not
   exist in this branch's MIGRATION.md, ledger-numbering validator green).
   `grep -c "R54" docs/MIGRATION.md` → 1.
7. **[MINOR] "ONE WARN per launch request" over-claim** → docstrings now state
   the honest law: ONE WARN per BUILT engine-run kwargs (per exchange-group
   engine run at launch; the preflight builds no kwargs and warns never);
   multiplicity pinned for the 2-group fan-out (2 warns for 2 built groups,
   0 at plan resolution).
8. **[MINOR] no route-level pins** → 5 new route tests across
   `test_dashboard_sweep_run_route.py` (none leg accepted + mixed accepted +
   gridded stage3 true disclosure) and
   `test_dashboard_sweep_oneclick_route.py` (preflight 200 + stage3 clamp;
   oneclick run launch-accepted with `sl="none,50"`).

### Fails-before / passes-after (stash-swap method, worktree venv)

Pre-fix state = the round-1 patch (snapshot swapped in), new/changed tests kept:

    16 failed, 3 passed, 94 deselected  (-k "r54 or to_dict")
    FAILED: to_dict ×2, both_none_warn_at_build, refused_no_warn, gridded_keep_stage3,
            mixed_end_to_end, mixed_normalise, none_offgrid_runnable, oneclick_warn_per_group,
            preflight_clamp ×2, run-route ×3, oneclick-route ×2
    (3 passes = the two unchanged whole-leg pins + the deliberate
     no-clamp-on-gridded complement pin.)

Post-fix: runner 74 passed · run-route + oneclick-route 39 passed · family
regression (runner + both routes + oneclick_frontend + engine preflight +
reports_ux + cli_sweep_scope integration) **228 passed, 1 skipped**
(pre-existing node-absent CI-only guard).

### Gate tails (worktree venv, 2026-07-09)

- Coverage: `brutex/dashboard/sweep_runner.py  221 stmts 0 miss  56 branch 0 partial  100.00%`
  (runner + both route suites, 113 passed)
- `ruff check` touched files: `All checks passed!`; `ruff format --check`: `4 files already formatted`
- `mypy --strict --no-incremental brutex/dashboard/sweep_runner.py`: `Success: no issues found in 1 source file`
- `pyright` (4 touched py files): `0 errors, 4 warnings` — IDENTICAL to the measured
  pre-fix baseline (same 4 pre-existing reportPrivateUsage warnings:
  `_FINISH_DETAIL_MAX`, `_MAX_K`, `_axes_counts` ×2; the new
  `_parse_paisa_list` friend-access sites carry the repo's per-site
  `# pyright: ignore[reportPrivateUsage]` convention). 0 new diagnostics.
- `python scripts/ci/validate_ledger_numbering.py`: OK (1244 headers, unique)
- `python scripts/ci/validate_citation_accuracy.py`: ✅ every extracted citation accurate

Nothing committed, nothing pushed; all changes are uncommitted working-tree
edits on the detached worktree, exported to `PATCH.diff`.
