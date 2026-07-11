# Rust Conversion Assessment — Dashboard / CLI / Ops surface

- **Date:** 2026-07-11
- **Decision ID:** DEC-RUST-ASSESS-DASHBOARD-CLI-OPS-001 (MIGRATION.md #1722,
  coordinator-assigned).
- **Umbrella mandate:** DEC-RUST-FIRST-MANDATE-001 — queued ledger entry
  #1713 (rides the in-flight kernel-track Slice-1 PR; NOT on `main` at this
  branch head, so it is cited as queued, not asserted as merged).
  This slice is **P12** of that roadmap, flagged there as
  "low-value tail — expected payoff ≈ 0". The verdict below CONFIRMS the
  roadmap's own expectation, with evidence.
- **Assessment read against:** `origin/main` at
  `adab6aa2acf0fa0e6b17d6c86dbf26c1ecc198e9` (the ADR-0012 futures-launch
  merge). This doc lands on a branch based one commit later
  (`a268bd4` — the Groww market-hours pull guard, which touches none of the
  assessed surface); every `file:line` citation below was RE-VERIFIED at the
  branch head.
- **Scope:** this slice = `brutex/dashboard/` (+ templates + static JS),
  `brutex/cli.py`, and the ops packages `brutex/backup/`, `brutex/nightly/`,
  `brutex/notifications/`, `brutex/observability/`, `brutex/config/`.
  The engine / data-lake / adapters slices are OTHER panels' scope and are
  NOT judged here (in particular the orchestrator audit hash-chain — noted
  below only as a hand-off).
- **Mandate framing:** the Rust-first mandate is an honest convert-or-keep
  assessment, not a forced rewrite program. Per Golden Rule #10, a KEEP
  verdict with evidence is a first-class outcome. The burden of proof sits
  on CONVERT.
- **Method:** three reader agents mapped size/architecture/hotspots/risk per
  area; three designer agents argued convert-maximalist, keep, and
  hybrid-pragmatist positions; a judge resolved the one split. This document
  is the panel's record.

---

## VERDICT (up front)

**KEEP PYTHON — all 7 areas. Zero convertible slices found.**

This surface is poll-driven, IO/cache-bound display + ops glue. Every
measured latency win in its history came from **cache architecture**
(stat-token / generation cache keys, DuckDB SQL pushdown) — not language
(MIGRATION.md #867, #895, #1533, #1614, #1711 were all cache-routing or
SQL-fold fixes with zero compute rewrite). The CPU-bound work already lives
in the Rust crates (`fno_math`, `trading_core`) and the native columnar
engines (DuckDB, polars) that this surface merely calls. All three designer
positions — including the convert-maximalist steelman — agreed there is no
CPU-bound Python here; the only contested item (a `brutex/backup/` watchdog
binary) was ruled KEEP on the merits (§ "Contested item" below).

**Mandate compliance.** KEEP PYTHON here is mandate-COMPLIANT engineering
judgment, not a skipped port. The Rust-first mandate v2's in-surface rule
says additions inside an existing Python surface follow that surface's
language until the surface is replaced, and its "organic replacement, no
big-bang" clause is exactly what rules out a whole-surface rewrite with
expected payoff ≈ 0. The verdict is backed by the measured evidence table
below and by the M6-watcher-owns-the-failure-class ruling on the
backup-binary steelman — the one convert argument that survived to the
judge.

---

## Final per-area verdict table

| Area | LOC | Verdict | Benefit of Rust | Effort | Risk | Rationale |
|---|---|---|---|---|---|---|
| dashboard/ | 56,872 py + 13,846 html + 5,171 js | **KEEP** | ~nil (µs-cached; DuckDB/polars already native) | Very high (85 routes, 8+ cache classes, 144+ test files) | Very high (XSS pins, 61 never-500 laws, cache-key semantics) | Every measured win was cache/SQL architecture, not language |
| cli.py | 9,571 | **KEEP** | Startup ms only; `version` fast path already byte-pinned | High (~42 suites, exit-code + env-law contracts) | High (cron-visible exit codes; log-guard; audit chain) | Zero in-file CPU; delegates are Python packages — Rust would fork/embed Python |
| backup/ | 3,506 | **KEEP** (contested — ruling below) | Detection-latency on venv-death class only; matrix-as-enum aesthetics | Med-large (3.5k src + 2-3× tests) | Med (dual MarkdownV2/alert drift; matrix re-proof) | M6 external watcher + restic immutability already own the dead-runtime class |
| nightly/ | 4,463 | **KEEP** | None (sha256 already OpenSSL-speed) | Blocked structurally | Very high | BY DESIGN an in-process binding over dashboard closures + `cmd_sweep_all` — un-portable without RPC-into-Python |
| notifications/ | 2,637 | **KEEP** | None (network + strings) | Med | High (never-raise contract; MarkdownV2; 30 builders' dedupe keys) | A regression turns a Telegram outage into a pull-killer |
| observability/ | 6,450 | **KEEP** | None (float compares per ~2 s tick; CI-time JSON) | Med | Med (bench-governance laws pinned to Python gate script) | Per-event work O(1) tiny; PyO3 call overhead would exceed work replaced |
| config/ | 4,207 | **KEEP** | Category-impossible for main job | — | Maximal blast radius/line | Must run inside the Python process (bounds polars/duckdb threads pre-import) |

No partial conversions either. The convert-maximalist designer's own report
concedes every slice other than backup/ ("weak, and I say so").

---

## Strongest evidence

1. **Flat-at-scale cache benches.** The oneclick primed verify poll measured
   26.5 µs/poll @ 4 doors vs 25.2 µs/poll @ 4000 doors — 0.95× FLAT at
   1000× artifact size — while the fresh compute the pre-cache route paid
   per poll measured 6.89 ms
   (`test_verify_repeat_poll_flat_vs_manifest_size`,
   tests/bench/test_oneclick_o1.py:623-637). The ledger poll measured
   14.18 ms/poll @ 1k rows vs 17.58 ms/poll @ 10k → ratio 1.24× under the
   `cache_tier_ceiling` discipline
   (tests/bench/test_ledger_delta_o_page_o1.py:170-178). The largest
   measured Python-side unit on the whole surface is that 6.89 ms compute,
   paid once per artifact revision.
2. **The dashboard reader's structural conclusion:** "no tight per-row
   Python loop over large data … not either delegated to polars/DuckDB or
   bounded+cached" — the CPU already lives in the native engines plus the
   engine-side Rust crates (`fno_math`, `trading_core`).
3. **The deadman's own honest-limits register.** The shipped backup design
   already refuses to trust ANY on-box process to survive box/runtime
   death: "A dead box is silent on-box — every alert pushes FROM the Mac"
   (docs/runbooks/backup-offbox.md:298-299), and the M6 external
   healthchecks watcher "IS the keystone … without it, box-death is silent"
   (docs/adr/0017-offbox-backup-restic.md:39,47). A static Rust binary on
   the same box does not change that trust model.
4. **Structural blocker on the ops chain.** `brutex/nightly/` imports
   `dashboard.pipeline_pull`, `dashboard.pull_controller`, and drives
   `cmd_sweep_all` — it is BY DESIGN an in-process binding over Python
   seams (DEC-SWEEP-NIGHTLY-AUTOMATION-001, MIGRATION.md #1612); and
   backup/nightly share a 9,960-LOC unit-test family, so even a
   backup-only port splits that suite across two languages.
5. **The historical win pattern.** MIGRATION.md #867 (candle-count cache),
   #895 (completeness cache routing), #1533 (ledger-delta DuckDB QUALIFY
   fold — deleted the O(records) Python branch), #1614 (lake stats
   manifest), #1711 (oneclick verify revision cache) were ALL
   cache-routing / SQL-pushdown fixes with zero compute rewrite. The house
   playbook for a hot display path is a stat-token cache or a DuckDB fold —
   repeatedly measured cheaper and safer than any language port.

---

## Contested item: `brutex/backup/` (recorded honestly, not erased)

**The convert steelman (Designer 1).** `brutex/backup/` is near-leaf (no
engine/dashboard imports), is a subprocess orchestrator over restic (restic
does the heavy lifting, so a Rust shell loses nothing), its 23-row
deterministic failure matrix (exit code + structlog event +
alert-or-explicit-no-alert triple; canon EXIT_OK..EXIT_MAINTENANCE in
brutex/backup/errors.py:15-57 — anchor `EXIT_OK`) maps naturally onto a Rust
exhaustive enum, and — the one argument that survives hostile review — the
dead-man watchdog fires from launchd and today depends on the Python venv
being intact: "the watchdog for the system should not share the system's
runtime." A ~3 ms static binary would remove the "backup watchdog dies
because the Python env broke" failure class.

**The ruling: KEEP (Designer 3 prevails).** The runtime-independence
argument is already conceded AND already mitigated by the shipped
architecture:

- The design explicitly does not trust any on-box process — Python or
  Rust — to survive box/runtime death; the M6 external healthchecks watcher
  is the keystone (evidence item 3 above). A Rust binary on the same box
  does not change that trust model.
- The marginal coverage of a Rust deadman is one narrow class: box alive,
  launchd alive, venv broken. In that class today, `brutex backup check`
  fails, the hourly healthchecks ping stops, and M6 alarms after grace —
  the failure is DETECTED, via the external lane, with ~1-2 h added latency
  instead of an on-box CRITICAL Telegram. Meanwhile the DATA is
  independently protected by restic → S3 Object-Lock immutability
  regardless of any watchdog. The Rust binary buys latency-of-detection on
  one failure class, not coverage.
- Against that: (a) the venv is mandatory regardless — the engine ships as
  PyO3 wheels, so a broken venv is loudly fatal to the whole system within
  hours, not silently fatal; (b) the 100% line+branch decree would span two
  languages for one feature (~3.5k LOC source + a 2-3× test port of the
  9,960-LOC backup/nightly suite family, per the convert designer's own
  estimate); (c) a second Telegram/MarkdownV2 + alert-dedupe implementation
  is a standing drift surface (`escape_markdown_v2`,
  brutex/notifications/sweep_alerts.py:94; the telegram module's
  best-effort never-raise contract); (d) the 23-row failure matrix, the
  phantom-mount gate ordered BEFORE `forget --prune` (the one
  data-destroying path), and the clock-injection skew laws are exactly the
  refuter-hardened semantics a rewrite must re-prove from zero; (e) the
  mandate is no forced rewrites — the burden of proof is on CONVERT, and
  the surviving benefit is a detection-latency delta on a class the
  architecture already externalizes to M6.

**Proportionate alternative, if the operator wants the venv-death class
covered on-box faster:** a ~5-line launchd/cron shell probe (does
`brutex backup status` exit 0?) pinging a SECOND healthchecks slug — not a
3.5k-LOC language port. This is recorded as an available option, not
scheduled work.

---

## Per-area detail

### 1. dashboard/ (69 py files, 56,872 LOC + 13,846 HTML + 5,171 JS; 85 FastAPI routes)

*What it is:* the read-only display zone — movers/db/coverage/completeness/
universe/oneclick/preopen surfaces, all routes in
`brutex/dashboard/app.py` delegating to sibling modules.

*Where time goes:* (a) cold paths are DuckDB `read_parquet` SQL aggregates
and polars month-bounded expression reads — already native; (b) steady-state
polls are in-process cache hits keyed on O(1) `os.stat` content tokens
(`MoversSnapshotCache`, `SummaryCache`, `UniverseSnapshotCache`,
`OutageGate`, `RateBridgeCache`, `PrebuildGate`, `FnoCascadeCache`, plus the
count-cache Protocols in `db_view`) — a primed poll is a handful of stats
plus a dict hit; (c) Python-level compute is thin glue over
page/session/report-bounded rows (e.g. ~375 rows/session movers projection,
top100 capped ≤100 entries). Heavy work never runs on the event loop
(`run_in_threadpool` doctrine).

*What a rewrite must re-prove:* the XSS discipline (textContent-only DOM
binding for artifact/vendor strings, pinned by dedicated hostile-twin
suites); 61 in-code "never a 500" degrade laws (named corrupt / awaiting /
partial, per-instrument corrupt-parquet skip); cache-key correctness
subtleties each of which exists because a refuter round found the naive
version wrong (the documented same-`(mtime_ns,size)` stat-token residual,
generation-vs-master_revision composite keys, outage-vs-absence
distinction, single-flight + bounded stale-serve); ~99 unit + ~31
integration + 14 bench files, several modules under per-module 100%
line+branch CI gates. An axum/actix port would call the same DuckDB/polars
libraries behind the same caches for the same latency.

### 2. cli.py (9,571 LOC; 29 subcommands)

*What it is:* argparse setup, env/flag validation, exit-code mappers, text
rendering, lock/alert wiring. The heavy handlers are explicitly
orchestration shims over the 100%-tested engine/adapter cores.

*Where time goes:* nowhere in-file — "meaningful CPU in cli.py itself:
essentially none" (CLI reader). All heavy compute is delegated to Python
packages that drive the Rust kernels; a Rust CLI would have to embed
CPython or fork a Python process to reach them — worse, not better.

*What a rewrite must re-prove:* the exit-code contracts (EXIT_OK=0 …
EXIT_SWEEP_TUPLE_FAILED=6, nightly SUSPECT → exit ≥2 — cron-visible
operator contracts); ~27 fail-loud `BRUTEX_*` env laws (junk → clean
exit 1, never silent); the byte-exact-stdout polars-free `version` fast
path; the polars-thread-bound-before-first-import ordering
(`apply_polars_thread_bound`, brutex/config/hardware_envelope.py:1019) and
lazy-import discipline; the sweep log-volume guard (the measured 870 MB
DEBUG disk-fill class, MIGRATION.md #1630) with the audit JSONL chain
staying non-structlog byte-untouched; ~42 CLI test suites including
wiring-pin tests that assert Python-object identity across seams —
unreproducible across FFI.

### 3. backup/ (3,506 LOC)

*What it is:* restic off-box backup (DEC-BACKUP-OFFBOX-001, MIGRATION.md
#1596): the ONE restic subprocess seam, the O(1) JSON dead-man verdict
machine with injectable clock, the monthly restore drill (1 MiB canary +
stratified sample hashing).

*Where time goes:* subprocess (restic does all data transfer/dedup/
hashing) + small JSON/file stat. Not CPU-bound.

*What a rewrite must re-prove:* the 23-row deterministic failure-mode
matrix; the phantom-mount gate BEFORE `forget --prune`; the injectable
dead-man clock with the fail-toward-alarm skew law; golden-pinned launchd
templates (byte-equal); 15 test files sharing 9,960 LOC with nightly. See
the contested-item ruling above.

### 4. nightly/ (4,463 LOC)

*What it is:* the R9 zero-click chain (DEC-SWEEP-NIGHTLY-AUTOMATION-001,
MIGRATION.md #1612) — orchestration glue binding EXISTING Python entry
points (dashboard pull closures, `cmd_sweep_all`), the R42 break-law FSM,
the sealed-snapshot backup/restore with streamed sha256 manifests.

*Where time goes:* IO copy + streamed sha256 once per night (`bars/`
excluded by law, so MBs-to-low-GBs); hashing is already OpenSSL-speed via
hashlib with the GIL released.

*What a rewrite must re-prove:* structurally blocked before any re-proof
starts — a Rust nightly cannot call the dashboard closures or
`cmd_sweep_all` without rewriting the dashboard+CLI first or RPC-ing back
into Python (strictly worse than the current one-process call). Plus the
break law, tripwires→SUSPECT→exit 2, per-run alert dedupe keys, the
"?"-never-fabricated-0 count law, and the CI restore drill.

### 5. notifications/ (2,637 LOC)

*What it is:* Telegram alerting — async HTTP POST, ~30 message builders +
dedupe keys, MarkdownV2 escaping at every builder.

*Where time goes:* network + string formatting. Not CPU-bound.

*What a rewrite must re-prove:* the best-effort never-raise contract (a
regression turns a Telegram outage into a pull-killer), the
token-never-in-logs guard, MarkdownV2 escaping, and every builder's dedupe
key; 13 test files / 2,847 LOC of suites.

### 6. observability/ (6,450 LOC)

*What it is:* structlog config, in-proc metrics registry, pull-health /
soak-drift threshold monitors, process probe (/proc reads with honest
`None` abstains), and the bench-baseline schema/comparator/governance core.

*Where time goes:* a handful of float compares per ~2 s sampler tick;
CI-time JSON parsing of tiny baseline docs. Per-event work is O(1) tiny —
PyO3 call overhead (~µs) would exceed the work replaced.

*What a rewrite must re-prove:* the bench-governance laws (no-silent-
re-baseline, SUSPICIOUS_SPEEDUP, strict-`>` boundary) pinned to the Python
gate script + workflow pairing; the watchdog/beat-channel semantics;
~19 suites including the stress-tier soak.

### 7. config/ (4,207 LOC)

*What it is:* hardware envelope detection + pool sizing, AWS SSM credential
reads, backup env, ports, dotenv, and `run_with_deadline` — the stdlib-only
freeze-guard primitive imported by much of the repo.

*Where time goes:* one-shot syscalls + env parsing. Not CPU-bound.

*Why conversion is category-impossible for its main job:* the thread-bound
work must run INSIDE the Python process before the first polars/duckdb
import (`apply_polars_thread_bound`,
brutex/config/hardware_envelope.py:1019; `apply_duckdb_thread_bound`,
brutex/config/hardware_envelope.py:1121). Highest blast radius per line on
the whole surface (it sizes every pool repo-wide under
env-override-wins-fail-loud laws) for zero cycles saved.

---

## Revisit conditions

Re-open this verdict (per area, not wholesale) if ANY of the following
occurs:

1. **A measured bench pin** (the house discipline) shows a Python-side
   hotspot on this surface that cache/SQL pushdown cannot fix — e.g. a
   per-poll compute that stays O(artifact) after keying.
2. **A scope change puts real CPU in the display zone** — e.g. server-side
   chart rendering, per-request analytics over uncapped rows, or the
   top100 cap removed.
3. **The deployment model changes**: more than one box, or a supervised
   host where the venv is NOT already mandatory — the single-binary
   watchdog argument strengthens materially then.
4. **Audit hash-chain growth**: if `audit verify` / the oneclick
   `check=chain` volume becomes material, the first move is the ledgered
   AuditCursor incremental verify (an algorithm change); consider Rust only
   if measurement still says so — and that is the engine-slice panel's
   call, not this one's.
5. **M6 lane loss**: if the external healthchecks watcher is
   decommissioned, the on-box watchdog's runtime-independence argument must
   be re-heard.

---

## Honest limits (Golden Rule #10)

- This is an **assessment, not a benchmark campaign**. No new measurements
  were taken; every number cited is an existing, dated bench pin or a
  reader-verified in-repo figure.
- LOC counts are raw `wc -l` (comments + blanks included) — a size proxy,
  not an effort model.
- The effort/risk columns are the panel's judgement, not measured
  port-costs; they were not needed to decide, because the BENEFIT column is
  ~nil everywhere on the evidence.
- The panel read `origin/main` at `adab6aa2…`; one commit landed on main
  before this doc branched (`a268bd4`, pull-hours guard — outside the
  assessed surface). All `file:line` cites were re-verified at the branch
  head; drift after merge is governed by the repo's citation-accuracy
  scanner only for its scanned normative set (this doc is NOT in that set —
  cites here are point-in-time and dated).
- The audit hash-chain (`brutex/orchestrator/audit.py` +
  `brutex/engine/sweep_audit.py`) is OUT OF SCOPE for this slice; the
  revisit-condition hand-off above is the only claim made about it.
- The umbrella-mandate ledger slot (queued #1713) is on an in-flight PR at
  the time of writing; if that PR renumbers, the pointer in the header is
  the stale copy to fix.
