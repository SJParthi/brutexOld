# feat(dashboard): fold screener + strategy into the operator page

## Before / After

**Before:** the operator hops between three pages — the winners table lives on /screener, per-strategy detail on /strategy, and the run/funnel picture on the Operator page (/).

**After:** the Operator page (/) is a single portal — an expandable "All 100 winners" section and an in-page strategy drawer carrying the chart, minute-path equity, trades, deep-stats, regime breakdown and VIX band. /screener and /strategy stay alive as thin deep-link pages consuming the SAME extracted modules, and the pinned `/strategy?run_id=&rank=` deep-link contract still passes unchanged.

## How

- **Extracted shared modules** (page logic moved once, verbatim, pinned-token strings preserved): `bx-portal-state.js` — the pure URL codec (`bxPortalParse`/`bxPortalSerialize` over `?winners=1&rank=N`) incl. `bxStrategyHref`, the ONE /strategy href builder for the consolidated surfaces; `bx-screener-panel.js` + `bx-strategy-panel.js` — the former inline page IIFEs as mount-once + `update()` modules with load-generation guards on every async render arm; `bx-operator-portal.js` — the v29-only glue (lazy winners mount, drawer state machine, focus trap/inert/scroll-lock/Esc/backdrop); `bx-oneclick-panels.css` — scoped panel CSS + all choreography under the locked motion covenants.
- **Additive seams on BXOneclick:** `getTop100` promise memo (ONE top100 fetch per run selection feeding four consumers; rejected promise self-evicts), `onRunChange` (run-change observer — close drawer, strip rank, reload winners), and `getManifestOnce` (settle-evicting in-flight memo deduping the cold double `/manifest` fetch).
- **The one id rename** `ocTradesSource` → `stTradesSource` (kills the latent rank≠1 trades-source badge stomp).
- **Markup stays declarative in the templates** (static awaiting skeletons are load-bearing honesty — a script failure never serves a blank root), fenced by the T-N2 ONE-inventory hook-parity check over BOTH markup copies + a T-N3 id-uniqueness fence with an empty baseline.
- **URL state machine:** pushState ONLY on a row-click drawer open; close never calls `history.back()`; a popstate reconciler runs under a `reconciling` flag; history entries are origin-stamped so a deep-link-origin close + reopen pushes (Back closes the drawer instead of exiting the site) while portal-origin cycles never accumulate duplicate entries.
- **Motion covenants (locked):** numbers never count-animate; honest degrade states render instantly un-animated; zero new looping keyframes; reduced-motion fully handled — including the deterministic `.bxd-equity-draw { stroke-dashoffset: 0 }` reset so reduced-motion users actually see the drawer equity curve.
- **XSS discipline:** every artifact-derived string is DOM-bound via textContent/createElement(NS) only; v29.html joined the hostile sink scan and the scan was widened (outerHTML / srcdoc / setHTMLUnsafe / DOMParser / setAttribute-on / eval / Function / `javascript:` / template inline `on*=` ban) — refuter-validated 0 false positives, 8/8 bypass attempts caught.

## Honest envelope — O-envelope + limits (Golden Rule #10)

- Existing cached oneclick routes ONLY — **zero new backend read paths, zero Python changes** (the two oneclick 100% line+branch gates are byte-untouched on the Python side).
- Winners render under the merged #1406 cap law (the ONE cap-law source of truth in `bx-screener.js`; render count + input cap NAMED on the count line; bounded Show-all at 1000; input cap 5000).
- Drawer open = 3 one-shot fetches warm / 4 cold (manifest deduped via `getManifestOnce`); 0 fetches on a rank revisit.
- No repeating timers or rAF loops — only bounded one-shot animation-cleanup setTimeouts; a hidden tab is fully silent.
- The codec/pure helpers and the panel/portal terminal-state machines are EXECUTED under node (a DOM stub — no CSS/layout/browser-event semantics claimed); the remaining DOM wiring stays source-string-pinned (the established browser-truth envelope).
- ✅ REAL: everything in the Gates table below, verified on head bc68ed01 (+ the docs-only tail 8367cae4).
- 🟡 PARTIAL: DOM wiring beyond the node-executed layers is source-pinned, not browser-executed.
- 📋 ROADMAP: the named follow-ups below.

## Review-loop record

8 adversarial review rounds with 3-refuter majority adjudication per round (the 4-agent hostile review discipline, run wider than the minimum: 3 reviewers × 3 independent refuters per round), 5 fix rounds folded, EXECUTED node goldens driving the SHIPPED modules (never stubs), exit on 2 consecutive clean rounds:

- Rounds 1–5: fix rounds (deep-link latch, managed `hidden` drawer, reduced-motion equity reset, listener-strand fallback, history hygiene, sink-scan widening, prose-precision NITs — full record in MIGRATION.md #1755).
- Round 6: 2 a11y fixes folded in 09ca7685 on 2/3-refuter consensus (the third refuter was environment-killed before filing — recorded honestly): winners-row `role="button"` dropped (invalid table ARIA), `#bxTip` lifted above the drawer z-stack.
- Round 7: CLEAN — zero confirmed findings, incl. the dedicated merge-integrity audit of the origin/main 3f32b3ef merge-up (four conflict files resolved faithfully; main's #1406 screener cap law ported into the shared panel; #1430 debounce ported verbatim).
- Round 8: CLEAN — loop exited. Final record-only register: a cascade-comment precision NIT; a stale scratch-doc suite count (the real suite is greener than the doc claimed); a pre-existing main-side harness env-sensitivity not introduced by this branch — all 3/3 confirmed record-only.

## Gates (independent workspace-health PASS — 15/15 gates green on bc68ed01; this PR adds one docs-only tail commit 8367cae4)

| Gate | Result |
|---|---|
| `oneclick_artifacts.py` + `oneclick_view.py` coverage | 100.00% line + branch (both standalone ci.yml-exact gates) |
| Dashboard unit suite | 2975 passed |
| Integration suite | 683 passed |
| Redteam suite | 159 passed |
| ruff + ruff-format | clean |
| `mypy --strict` | clean |
| pyright | 0 errors |
| `node --check` | 7/7 JS files clean |
| Ledger-numbering validator | green (re-run on the docs tail: 1317 headers, every pair unique) |
| Citation-accuracy scanner | green (re-run on the docs tail: every extracted citation accurate) |
| CLAUDE.md consistency pins | 10 passed (re-run on the docs tail) |
| Conventional commits (origin/main..HEAD) | 10/10 pass (merge commit parent-count-exempt) |
| Coverage-gate discovery vs origin/main | PASS (no touched brutex modules on the tail) |
| Site-wide widget tags (`bx-liveproof.js` + `bx-rate-monitor.js`) | present in every `brutex/dashboard/templates/*.html` incl. the thinned screener.html/strategy.html |

## Ledger + reconcile

- **MIGRATION.md #1755** (renumbered from the provisional #1714 per the in-flight reservation map — main's T1 sweep-core train claimed #1714).
- Split-scope vs the operator-page cleanview session was ruled by the coordinator: this consolidate PR lands FIRST; the Operator-page anchor order is health hub → winners → diagnostics; vs PR #1457 there is exactly one trivial same-anchor script insert to reconcile.

## Named follow-ups (deliberately NOT in this PR; described in words — their ledger entries do not exist yet)

- The URL **run_id/trade write side** for the portal (the portal today reads + reconciles those params; writing them is the follow-up owned by a future portal-run_id decision).
- Routing the **combos "View" hrefs through the shared `bxStrategyHref` builder** (one pre-existing inline copy survives in `bx-oneclick.js` `renderCombosRows` — the ledgered combos-table-config unification follow-up).
- The **screener/strategy cleanview absorbs into the extracted panels** — owned by the cleanview session.

---

## Definition of Ready (plan-first gate)

### Scenarios covered

- Happy: expand winners on / → filter/sort → row-click opens the drawer (pushState) → chart/equity/trades/deep-stats render → close restores the URL.
- Edge: deep-link `/?winners=1&rank=N` (with and without run_id — the `restorePending` latch), bad-rank deep-link (honest miss), run switch mid-drawer (gen bump kills the stale chain), rapid open/close under 400 ms (owned re-hide timer), reduced-motion (equity dashoffset reset), collapsed winners untabbable, keyboard row activation.
- Empty/zero: no runs / awaiting artifacts → the templates' static honest awaiting copy (never "loading…" forever); empty top100 renders the honest count line.
- Failure: CSS 404 (managed `hidden` — never a dumped skeleton), manifest fetch failure (story card still loads), stale late responses (count-pinned generation guards on every async render arm), hostile run_id (encodeURIComponent on every href component).
- Concurrent: poller auto-selection vs deep-link restore (arm-scoped latch release); double-manifest dedupe; popstate vs in-flight open (reconciler flag).

### Failure modes

Each render arm degrades to a named honest state (awaiting/corrupt/partial per the oneclick reader contracts); a script failure serves the declarative template skeleton, never a blank root; a failed restore leaves the honest URL rank (self-clearing); route failures render errorCard + Retry — never a fabricated count, never a 500 surfaced as blank.

### Real-run proof

```
uv run pytest tests/unit/test_dashboard_operator_portal.py tests/unit/test_oneclick_view_routes.py -q   # portal/codec/panel node goldens + route matrix — green
node --check brutex/dashboard/static/bx-portal-state.js  # + the other 6 touched JS files — 7/7 clean
```

plus the full workspace-health verification on bc68ed01 (table above). Honest limit: node goldens execute the shipped modules against a DOM stub; browser-truth CSS/layout semantics are not claimed (source-string pins per the established envelope).

### Rollback plan

Display zone only — zero Python changes, no schema/data migration. Rollback = `git revert` of the squash-merge commit on main; the standalone /screener + /strategy pages regain their inline IIFEs from the reverted templates with no stored-state cleanup needed.

### Out of scope

The three named follow-ups above; any engine/orchestrator/results-storage change (ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX); any new backend route or read path; browser-executed CSS/layout verification.

## Summary

- /screener + /strategy fold into the Operator page (/) as a single portal: expandable All-100-winners section + in-page strategy drawer, over ONE set of extracted shared static modules; both standalone pages stay alive as thin deep-link targets.
- Additive BXOneclick seams (getTop100 / onRunChange / getManifestOnce), one id rename, URL state machine with origin-stamped history, locked motion covenants, widened hostile sink scan.
- DISPLAY zone only — zero Python changes; 8-round adversarial loop completed (2 consecutive clean); independent workspace-health PASS 15/15 on bc68ed01.

## Spec / decision links

- Decision ID(s): DEC-OPERATOR-PAGE-CONSOLIDATE-001
- MIGRATION.md row(s): #1755
- Spec section(s) touched: CLAUDE.md §1/§3 active-branch record (docs); no engine spec touched.

## Type of change

- [x] feat — new feature
- [x] docs — documentation (the completion-record tail commit)

## Golden Rule #12 — 100% standards self-check

### Coverage + tests
- [x] Coverage line 100% on the two governed oneclick modules (byte-untouched Python; gates green)
- [x] Coverage branch 100% on same
- [x] Property tests: N/A new Python logic (JS state machines covered by executed node goldens incl. adversarial arms)
- [x] Golden fixtures: node goldens for codec/panels/portal (executed, kill-proven vs pre-fix blobs)
- [x] Benchmark: N/A — no new backend read path (O-envelope disclosed above)

### Bug fixes
- [x] N/A — feat PR (the folded review-round fixes each carry kill-proven pins)

### Type / lint / security
- [x] `mypy --strict` clean
- [x] pyright 0 errors
- [x] `ruff check` clean
- [x] bandit: no new Python surface
- [x] No new copy-paste: the extraction MOVES the IIFEs (inline copies deleted); duplicate RENDER_CAP killed + negative-pinned

### Performance
- [x] No new O(n) where O(1) achievable (cached routes only; memoised top100/manifest)
- [x] No new pandas (no Python change)
- [x] No new global mutable state (module-scoped panel state behind mount-once seams)

### Observability
- [x] N/A — display zone; no material state change, no new audit surface
- [x] No `print` (JS console usage unchanged; Python untouched)

### Documentation
- [x] MIGRATION.md #1755 carries the full record; CLAUDE.md §1/§3 reconciled
- [x] No silent scope expansion (Golden Rule #3 — engine unchanged)

### No hallucination (Golden Rule #1)
- [x] Every gate number above traces to the workspace-health run on bc68ed01 + the re-run validators on 8367cae4
- [x] No invented rate/threshold/constant

---

## Z+ 15-row matrix (100% everything — charter §C)

| Demand | This PR's proof | Real-time check |
|---|---|---|
| 100% code coverage | oneclick modules 100.00% line+branch (Python byte-untouched; ci.yml-exact gates green) | ci.yml coverage gates |
| 100% audit coverage | N/A — display zone; no material state action added | grep brutex/dashboard |
| 100% testing coverage | unit 2975 + integration 683 + redteam 159 + executed node goldens; suite 160→162 on the review rounds | pytest --strict-markers |
| 100% code checks | ruff + ruff-format + mypy --strict + pyright 0 errors | pre-push hooks |
| 100% code performance | no new backend path; drawer O-envelope disclosed (3 warm/4 cold one-shot fetches, 0 on revisit) | this body §Honest envelope |
| 100% monitoring | N/A — no new backend event surface | grep brutex/observability |
| 100% logging | N/A — display zone | grep -r structlog |
| 100% alerting | N/A — no new CRITICAL condition | brutex/observability/ |
| 100% security | widened hostile sink scan (0 FP, 8/8 bypasses caught); encodeURIComponent on every href component | redteam suite 159 passed |
| 100% security hardening | textContent/createElement(NS)-only DOM binding; v29 inline onclick converted to addEventListener | sink-scan test |
| 100% bug fixing | 8-round adversarial loop, 5 fix rounds, every fix pin-killed | this body §Review-loop |
| 100% scenarios covering | T-N5 honest-degrade route matrix + DoR scenarios above | test_dashboard_operator_portal.py |
| 100% functionalities covering | every exported helper node-goldened; hook-parity + id-uniqueness fences | T-N2/T-N3 pins |
| 100% code review | 3 reviewers × 3 refuters × 8 rounds; 2-consecutive-clean exit | MIGRATION.md #1755 |
| 100% extreme check | workspace-health 15/15 independent verification on bc68ed01 | table above |

## Z+ 7-row resilience matrix (charter §C)

| Demand | Honest envelope | This PR's proof |
|---|---|---|
| Zero ticks lost | N/A — BruteX is backtest-only (Track 1) | confirmed |
| WS never disconnects | N/A — no WebSocket in Track 1 | confirmed |
| Never slow/locked/hanged | no repeating timers/rAF; hidden tab silent; bounded one-shot timeouts only | T-N6 zero-new-pollers pin |
| QuestDB never fails | N/A — BruteX uses DuckDB + LMDB | confirmed |
| O(1) latency | no new backend read path; cached oneclick routes only | §Honest envelope |
| Uniqueness + dedup | id-uniqueness fence (empty baseline); manifest/top100 fetch dedupe memos | T-N3 pin + node goldens |
| Real-time proof | workspace-health PASS 15/15 on bc68ed01 | Gates table |

## Z+ 7-layer audit

| Layer | This PR's status |
|---|---|
| DETECT | hook-parity + id-uniqueness + motion-covenant + sink-scan + gen-guard pins fail CI on regression |
| VERIFY | executed node goldens drive the SHIPPED modules end-to-end (stub DOM); route matrix pins honest degrades |
| RECONCILE | origin/main 3f32b3ef merged INTO the branch (no history rewrite); #1406 cap law + #1430 debounce ported into the shared panel; round-7 merge-integrity audit CLEAN |
| PREVENT | negative pins (killed tokens, duplicate RENDER_CAP, role-never-returns); T-N2 fence over both markup copies |
| AUDIT | MIGRATION.md #1755 carries the full per-round record; this PR body carries the completion summary |
| RECOVER | git revert of the squash-merge (display zone; no data migration) |
| COOLDOWN | held for coordinator merge (one-slot release wave); standalone pages remain live as fallbacks |

## 4-agent hostile review

| Agent | Lens | Findings | Severity |
|---|---|---|---|
| Correctness/journeys | state machine, history, deep-link, race | deep-link latch, gen guards, history hygiene — all fixed + pin-killed | MEDIUM and below |
| A11y/claims | ARIA, focus, reduced-motion, prose honesty | role=button drop, #bxTip z-ladder, equity dashoffset reset — fixed | MINOR/LOW |
| Security/sink | XSS sinks, hostile params | scan widened (0 FP, 8/8 bypasses); encodeURIComponent hrefs | LOW (hardening) |
| Suites/cites | pins, counts, citations, merge integrity | round-7/8 CLEAN; 3 record-only register items | record-only |

Review evidence: MIGRATION.md #1755 (per-round record incl. refuter adjudication); rounds 6-8 completion recorded in commit 8367cae4.

## Test plan

- [x] `uv run pytest tests/unit/test_dashboard_operator_portal.py -q` — green (node goldens + fences)
- [x] Dashboard unit 2975 / integration 683 / redteam 159 — passed (workspace-health, bc68ed01)
- [x] Validators re-run on the docs tail: ledger numbering, citation accuracy, CLAUDE.md pins, conventional commits — all green
- [x] Manual smoke: N/A beyond node-executed goldens (browser-truth out of scope per the honest envelope)

## Operator action required (post-merge)

- None.

---

HELD FOR COORDINATOR MERGE — one-slot release wave; do not merge; steward slots after the current critical path.
