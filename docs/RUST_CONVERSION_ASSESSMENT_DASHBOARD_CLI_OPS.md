# Rust conversion — dashboard / CLI / ops (P12): assessment, operator override, and organic conversion roadmap

- **Date:** 2026-07-11 (assessment + override + roadmap, same day).
- **Decision ID:** DEC-RUST-ASSESS-DASHBOARD-CLI-OPS-001 (MIGRATION.md #1722,
  coordinator-assigned).
- **Status:** the assessment panel's engineering verdict was **KEEP PYTHON —
  all 7 areas** (§1, recorded intact — it is the evidence base this
  roadmap's risk-ordering rests on). The **operator OVERRODE that verdict on
  2026-07-11** (the Rust-everywhere directive, relayed via the coordinator);
  the deliverable of this document is therefore the **organic conversion
  roadmap** in §2, which adopts the override while preserving every honesty
  constraint the assessment measured.
- **Umbrella mandate:** DEC-RUST-FIRST-MANDATE-001 — queued ledger entry
  #1713 (rides the in-flight kernel-track Slice-1 PR; NOT on `main` at this
  branch head, so it is cited as queued, not asserted as merged — and the
  same caveat extends to the mandate's CONTENT: every mandate-v2 rule
  referenced in this doc is quoted from that in-flight PR and is not yet
  verifiable in this repo at this head). This slice is **P12** of that
  roadmap, flagged there as "low-value tail — expected payoff ≈ 0". The
  assessment CONFIRMED that expectation with evidence; the override
  schedules the conversion anyway, as a uniformity program.
- **Scope:** this slice = `brutex/dashboard/` (+ templates + static JS),
  `brutex/cli.py`, and the ops packages `brutex/backup/`, `brutex/nightly/`,
  `brutex/notifications/`, `brutex/observability/`, `brutex/config/`.
  The engine / data-lake / adapters slices are OTHER panels' scope and are
  NOT scheduled here (the orchestrator audit hash-chain is a hand-off, §3).
- **Method:** three reader agents mapped size/architecture/hotspots/risk per
  area; three designer agents argued convert-maximalist, keep, and
  hybrid-pragmatist positions; a judge resolved the one split (§1). After
  the operator override, three roadmap designers drafted competing
  conversion shapes and a judge synthesized the final roadmap (§2), folded
  with the coordination constraints received from the arch-eval and
  frontend-latency sessions (§2.6).

---

## §0 MANDATORY HONESTY HEADER

> This is a **uniformity program, not a performance program**. The panel's
> measured evidence stands: the latency payoff of language conversion on
> this slice is **≈ 0** (flat-at-scale cache benches: the oneclick primed
> verify poll measured 26.5 µs vs 25.2 µs at 1000× artifact size; the
> largest measured Python-side unit is a 6.89 ms compute paid once per
> artifact revision; the CPU already lives in DuckDB/polars/the Rust engine
> crates). **No conversion PR may claim a latency win it did not measure**
> — a Rust rewrite preserving the same scan shapes preserves the same
> breaches (arch-eval constraint, §2.6). The ACTUAL latency fixes are the
> two arch-eval follow-ups carried INSIDE this roadmap as blocking waves:
> **(R1) the /results cross-store leaderboard rollup written at
> sweep-flush time** (fixes the one measured breach — the
> `load_sweep_scan` O(stores×rows) scan, 205 ms @ 10 stores → 23.7 s
> @ 1000 cold) and **(R2) whole-lake aggregation rollups**. Frontend: "no
> measured client compute justifies frontend-WASM yet; the measured
> frontend bottlenecks are request duplication and DOM rebuild, both fixed
> browser-native; revisit on evidence" (frontend-latency session's agreed
> wording, §2.6). The honest END-STATE is a **permanent two-process split**
> — Rust display/ops front + Python pull-job sidecar — while the Groww pull
> surface stays mandate-EXEMPT; stated plainly, not as a temporary phase.

---

## §1 The decision record (assessment evidence + the override)

### 1.1 The panel's verdict — recorded, not erased

**KEEP PYTHON — all 7 areas. Zero convertible slices found.** This surface
is poll-driven, IO/cache-bound display + ops glue. Every measured latency
win in its history came from **cache architecture** (stat-token /
generation cache keys, DuckDB SQL pushdown) — not language (MIGRATION.md
#867, #895, #1533, #1614, #1711 were all cache-routing or SQL-fold fixes
with zero compute rewrite). The CPU-bound work already lives in the Rust
crates (`fno_math`, `trading_core`) and the native columnar engines
(DuckDB, polars) that this surface merely calls. All three designer
positions — including the convert-maximalist steelman — agreed there is no
CPU-bound Python here; the only contested item (a `brutex/backup/`
watchdog binary) was ruled KEEP on the merits (§1.4).

The panel read `origin/main` at the assessment-basis commit
`adab6aa2acf0fa0e6b17d6c86dbf26c1ecc198e9`; the branch base is one commit
later (`a268bd4`, market-hours pull-guard glue touching three of the seven
areas — guard/glue wiring only, no CPU-bound work added); every
`file:line` citation below was re-verified at the branch head.

### 1.2 Per-area verdict table (the panel's read, dated)

LOC figures are raw `wc -l` at the `adab6aa2` assessment basis (at
`a268bd4`: dashboard 56,872 → 57,195 py · cli.py 9,571 → 9,640 · nightly
4,463 → 4,483; the other four areas unchanged).

| Area | LOC | Panel verdict | Benefit of Rust | Effort | Risk | Rationale |
|---|---|---|---|---|---|---|
| dashboard/ | 56,872 py + 13,846 html + 5,171 js | **KEEP** | ~nil (µs-cached; DuckDB/polars already native) | Very high (85 routes, 8+ cache classes, 144+ test files) | Very high (XSS pins, 61 never-500 laws, cache-key semantics) | Every measured win was cache/SQL architecture, not language |
| cli.py | 9,571 | **KEEP** | Startup ms only; `version` fast path already byte-pinned | High (~42 suites, exit-code + env-law contracts) | High (cron-visible exit codes; log-guard; audit chain) | Zero in-file CPU; delegates are Python packages |
| backup/ | 3,506 | **KEEP** (contested — §1.4) | Detection-latency on venv-death class only | Med-large (3.5k src + 2-3× tests) | Med (dual MarkdownV2/alert drift; matrix re-proof) | M6 external watcher + restic immutability already own the dead-runtime class |
| nightly/ | 4,463 | **KEEP** | None (sha256 already OpenSSL-speed) | Blocked structurally | Very high | BY DESIGN an in-process binding over dashboard closures + `cmd_sweep_all` |
| notifications/ | 2,637 | **KEEP** | None (network + strings) | Med | High (never-raise contract; MarkdownV2; dedupe keys) | A regression turns a Telegram outage into a pull-killer |
| observability/ | 6,450 | **KEEP** | None (float compares per ~2 s tick) | Med | Med (bench-governance laws pinned to Python gate script) | PyO3 call overhead would exceed work replaced |
| config/ | 4,207 | **KEEP** | Category-impossible for main job | — | Maximal blast radius/line | Must run inside the Python process (bounds polars/duckdb threads pre-import) |

These effort/risk columns are what §2's wave ORDER is derived from:
cheapest-parity-first, hardest-contract-last, category-impossible-never.

### 1.3 Strongest evidence (condensed; full readings in the panel record)

1. **Flat-at-scale cache benches.** The oneclick primed verify poll
   measured 26.5 µs/poll @ 4 doors vs 25.2 µs/poll @ 4000 doors — 0.95×
   FLAT at 1000× artifact size — while the fresh compute the pre-cache
   route paid per poll measured 6.89 ms
   (`test_verify_repeat_poll_flat_vs_manifest_size`,
   tests/bench/test_oneclick_o1.py:623-637). The ledger poll measured
   14.18 ms/poll @ 1k rows vs 17.58 ms/poll @ 10k under the
   `cache_tier_ceiling` discipline — the bench docstring's own
   **"pre-page-cache"** pin
   (tests/bench/test_ledger_delta_o_page_o1.py:170-178), and that 17.58 ms
   is a native DuckDB QUALIFY scan, not Python compute.
2. **The dashboard reader's structural conclusion:** "no tight per-row
   Python loop over large data … not either delegated to polars/DuckDB or
   bounded+cached" — the CPU lives in the native engines plus the
   engine-side Rust crates.
3. **The deadman's own honest-limits register:** "A dead box is silent
   on-box — every alert pushes FROM the Mac"
   (docs/runbooks/backup-offbox.md:298-299), and the M6 external
   healthchecks watcher "IS the keystone … without it, box-death is
   silent" (docs/adr/0017-offbox-backup-restic.md:39,47). A static Rust
   binary on the same box does not change that trust model.
4. **Structural blocker on the ops chain.** `brutex/nightly/` imports
   `dashboard.pipeline_pull`, `dashboard.pull_controller`, and drives
   `cmd_sweep_all` — BY DESIGN an in-process binding over Python seams
   (DEC-SWEEP-NIGHTLY-AUTOMATION-001, MIGRATION.md #1612); backup/nightly
   share a 9,960-LOC unit-test family.
5. **The historical win pattern.** MIGRATION.md #867 / #895 / #1533 /
   #1614 / #1711 were ALL cache-routing / SQL-pushdown fixes with zero
   compute rewrite — the house playbook for a hot display path is a
   stat-token cache or a DuckDB fold, repeatedly measured cheaper and
   safer than any language port.

### 1.4 Contested item: `brutex/backup/` (steelman + ruling, kept)

**The convert steelman (Designer 1).** `brutex/backup/` is near-leaf, a
subprocess orchestrator over restic, its 23-row deterministic failure
matrix (exit code + structlog event + alert-or-explicit-no-alert triple;
canon `EXIT_OK`..EXIT_MAINTENANCE in brutex/backup/errors.py:15-57) maps
naturally onto a Rust exhaustive enum, and the one argument that survived
hostile review: the dead-man watchdog fires from launchd and depends on
the Python venv being intact — "the watchdog for the system should not
share the system's runtime." A ~3 ms static binary would remove the
venv-death failure class for the watchdog.

**The ruling: KEEP on the merits (Designer 3 prevailed).** The
runtime-independence argument is already mitigated by the shipped
architecture: the design trusts NO on-box process to survive box/runtime
death (the M6 external healthchecks watcher is the keystone — evidence
item 3); in the venv-broken class the failure is DETECTED via the external
lane with ~1-2 h added latency; the DATA is independently protected by
restic → S3 Object-Lock regardless of any watchdog; the venv is mandatory
regardless (the engine ships as PyO3 wheels); a second Telegram/MarkdownV2
implementation (`escape_markdown_v2`,
brutex/notifications/sweep_alerts.py:94) is a standing drift surface; and
the 23-row matrix + the phantom-mount gate ordered BEFORE `forget --prune`
+ the clock-skew laws are refuter-hardened semantics a rewrite must
re-prove from zero. The proportionate KEEP-world alternative was a
~5-line shell probe pinging a second healthchecks slug.

**Under the override** this ruling's engineering content STANDS as the
risk record — it is why the deadman port (§2 wave 2b) ships with a G2
cross-language golden table + G4 injected-clock property parity, why the
M6 lane remains the keystone (the binary is a uniformity item, never
claimed as coverage), and why `brutex-backup` (wave 13) is late-ordered
behind a green scheduled cycle + a live negative dead-man drill.

### 1.5 The operator override (supersedes the decline)

On 2026-07-11 the operator DIRECTED (relayed via the coordinator,
overriding the panel's keep-Python verdict) that this slice be converted
to Rust under the Rust-everywhere uniformity directive. The override is a
scheduling decision, not an evidence decision: nothing in §1.1–§1.4 is
retracted, and per §0 no conversion PR may launder the override into an
unmeasured performance claim. What changes is the deliverable — from
"decline with revisit conditions" to "convert organically, risk-ordered by
the assessment's own effort/risk table, with parity gates carrying the
same refuter-hardened semantics the assessment catalogued." The Groww pull
surface (vendor SDK + TOTP auth + the pull spines) stays mandate-EXEMPT;
`brutex/config/`'s in-process thread-bound job stays category-impossible
and dies WITH the last Python importer (§2 wave table, last row).

---

## §2 The conversion roadmap

### 2.1 Shape: four parallel tracks, the strangler front as the spine

The three roadmap designs were complementary, not competing. Adopted
shape:

- **SERVING (the spine)** — `brutexd`, a NEW Rust reverse-proxy front
  (axum-or-equivalent; the arch-eval session calls axum/tower over the
  duckdb Rust crate + arrow-rs "the unexotic default" — engine pick
  deferred to PR-1). Route FAMILIES are taken over one PR at a time,
  byte-gated, shadow-soaked; the Python route is deleted only in a
  separate post-soak PR. This is the stated DESTINATION.
- **COMPUTE** — `rust/display-core`, a PyO3 interim crate
  (arch-eval-approved shape) for pure payload builders, plus the ONE
  common **`bx-cache`** stat-token / single-flight / stale-serve / LRU
  crate (the arch-eval memo names this crate as the right conversion
  shape for the house cache pattern). Rule: display-core functions are
  plain Rust with a thin PyO3 shim, so the SERVING track later consumes
  the same crate natively — COMPUTE work is never throwaway, it becomes
  brutexd's route bodies.
- **OPS** — standalone binaries with golden-table contracts (deadman →
  bench gate → schedule renderers → backup → restore drill),
  cheapest-parity-first ordering.
- **CLI** — a Rust `brutex` front binary: native leaf commands +
  exec-forward of the exempt/blocked set to `python -m brutex.cli`.

### 2.2 Parity-gate classes (every wave: default-OFF flag; Python impl deleted only in a separate post-soak PR)

- **G0** — sha256 byte-identity (bodies + header subset).
- **G1** — wire-JSON byte-identity via a `pyjson` CPython-compatible
  serializer + golden corpus.
- **G2** — cross-language golden table (exit codes / event names /
  verdict rows; `tests/golden/crosslang/`).
- **G3** — cell-value identity with a DECLARED, ledgered deviation
  (e.g. xlsx container bytes).
- **G4** — dual-dispatch property parity (hypothesis + proptest over a
  shared corpus).

### 2.3 Wave table (each row ≈ one PR)

| Wave | Track | PR | Gate | Blockers / notes |
|---|---|---|---|---|
| 1 | SERVING | **`brutexd` + `/static/*` + `/health` + proxy-all-through** (§2.4) | G0 | none — the program opener |
| 2a | COMPUTE | `pyjson` serializer + golden corpus; `bx-cache` crate skeleton (stat-token memo, single-flight, stale-serve bound, LRU) | G1 primitive + unit/proptest | hardest parity primitive; everything JSON leans on it |
| 2b | OPS | `brutex-deadman` verdict engine, `BRUTEX_RUST_DEADMAN` flag | G2 + G4 (injected `--now`) | none; §1.4 risk record governs |
| 2c | COMPUTE | `display-core`: moneyness port (paisa ints, no float) | G4 + goldens | none |
| 3 | SERVING | Template pages (15 HTML, GET-only, served whole) | G0 | **AFTER `claude/frontend-latency-hardening` merges** (template freeze — §2.6) |
| 4 | SERVING | `/api/v1/pull/rate` (RateBridgeCache onto `bx-cache`; the bridge FILE is already cross-process) | G1 | 2a; generation-key audit precondition (fold the ledger stat token where generation-only — the MIGRATION.md #1149 mechanism) |
| 5 | COMPUTE | oneclick_view + movers_underlying_class + cascade pure builders into display-core | G4 + fixture goldens | 2a |
| 6 | SERVING | Oneclick family (7 routes; OutageGate + serve_revision_cached onto bx-cache; reuses wave-5 builders) | G1 + hostile-twin replay | 2a, 5 |
| 7a | ARCH | **(R1) /results cross-store rollup written at sweep flush** — Python-side, the top100_artifact precedent | own bench pin | carried arch-eval follow-up; the REAL latency fix; BLOCKS wave 9 |
| 7b | OPS | bench_baseline gate port → schedule renderers | G2 → byte-pinned templates | 2b |
| 8 | SERVING | Universe/completeness/coverage/mapping-integrity (duckdb-rs, SQL text verbatim) | G1 | 2a; **(R2) whole-lake rollups** land with/before this wave as its honest latency story |
| 9 | SERVING | /results + leaderboard family | G1 | **BLOCKED on 7a** — Rust must serve the rollup, never inherit the O(stores×rows) scan |
| 10 | SERVING | Movers family (3 tabs + warm grid; the 2dp half-up law is the parity risk) | G1 | 5, 8 |
| 11 | SERVING | /db family (pagers, ledger QUALIFY folds, the MIGRATION.md #1614 stats-manifest revalidation ported whole) | G1 | 8 proves duckdb-rs first |
| 12 | SERVING | Reports/preopen xlsx/zip exports | **G3** (xlsx byte-identity impractical — declared, ledgered) | 3 |
| 13 | OPS | `brutex-backup` binary (23-row failure matrix as a G2 table; phantom-mount-before-prune transcript golden; restic argv byte-equal; MarkdownV2 cross-golden) → restore drill | G2 | 2b; cut-over needs one green scheduled cycle + a live negative dead-man drill (§1.4) |
| 14 | CLI | Rust `brutex` front binary: native version/status/backup/audit-verify/lake audit-unique…; **exec-forwards** fetch/enrich-greeks/greeks-backfill/reopen-skipped/dashboard/sweep*/nightly*/cascade… to `python -m brutex.cli`, exit codes byte-for-byte; may pre-set `POLARS_MAX_THREADS` pre-exec | G2 exit-code table + argv transcript goldens + `version` stdout pin | 13; forwarding preserves the env-law + version fast path |
| 15 | OPS | notifications core (sender + builders' dedupe keys + MarkdownV2) consumed by brutexd/Rust CLI; the Python job host keeps its copy | G2 builder-output table | ≥2 Rust consumers exist; the drift window is time-boxed |
| 16 | SERVING | Shrink FastAPI to the **job host** (pull routes + BackgroundPullJob + locks + watchdogs + sweep_alerts + the nightly conductor); brutexd proxies it forever | proxy inertness re-pin + a 503 "job host restarting" degrade class | **END-STATE, permanent under the Groww exemption** |
| — | never/last | nightly chain (Python conductor until the pull+sweep seams re-land as subprocess seams — the flock-held-through-sweep law of MIGRATION.md #1612 does not survive exec today); `hardware_envelope` apply-seams (`apply_polars_thread_bound`, brutex/config/hardware_envelope.py:1019; `apply_duckdb_thread_bound`, brutex/config/hardware_envelope.py:1121) die WITH the last Python importer, not before; observability per-tick monitors port only WITH their host process; frontend-WASM: revisit on evidence (§0) | — | stated, not scheduled |

### 2.4 FIRST-PR SPEC — `brutexd` skeleton + `/static/*` + `/health` (SERVING wave 1)

- **Why this is PR-1:** it is the smallest self-contained route/service,
  it is ALSO the cheapest parity in the whole program (sha256-of-bytes
  over files-on-disk + one pinned health body — no JSON serializer law,
  no caches, no XSS surface, no never-500 re-derivation), and it starts
  the destination: every later SERVING wave lands behind the
  infrastructure this PR creates (proxy, flag, ports, parity harness,
  cargo CI lanes). The deadman and moneyness candidates were not
  rejected — they are wave-2 items on their own tracks.
- **Scope/files:** NEW workspace crate `rust/brutexd/` (main /
  static-files / health / proxy / flag modules); **zero edits to
  `rust/trading-core` (FROZEN) or the kernel-owned `rust/fno-math` sweep
  files — enforced by a CI diff-path pin refusing changes there from this
  track's PRs**. Python side: `brutex/config/ports.py` gains the brutexd
  private-port entry; `brutex dashboard` gains an opt-in spawn of the
  binary (flag-gated); NO route deleted, NO template touched.
- **Behavior:** brutexd binds the operator port 127.0.0.1-only; serves
  the `brutex/dashboard/static/` tree (identical
  `Content-Type`/`Cache-Control` headers; path-traversal + symlink
  refusal; a global panic-to-degrade handler so a panic is a logged
  500-class degrade, never a process death) + `/health` as pinned literal
  bytes (no JSON serializer dependency — `pyjson` deliberately deferred
  to wave 2a); reverse-proxies every other path unchanged to FastAPI on a
  private localhost port.
- **Flag:** `BRUTEX_RUST_FRONT` ∈ {`off`, `on`}; a junk value → refuse to
  start, loud (the house env-law). **Default OFF = FastAPI binds the port
  directly and brutexd is never spawned — byte-zero behavior change,
  pinned by an identity test.**
- **Parity gate:** a NEW hard ci-ok integration gate boots BOTH apps on
  ephemeral ports over the same tree/fixture lake. Corpus: (a) every file
  under `brutex/dashboard/static/`; (b) `/health`; (c) a proxied-route
  corpus harvested by a TestClient spy from the existing route suites.
  Comparator: sha256(body) equality + header-subset equality
  (content-type, cache-control); proxied routes additionally pinned
  byte-equal to a DIRECT FastAPI hit (proves the hop inert), including a
  streaming/chunked response and a POST with body.
- **Rollback:** flip the flag to `off` (one env var, no deploy); the
  Python static mount survives this PR.
- **CI wiring:** cargo fmt/clippy/test + a cargo-llvm-cov 100% lane for
  `rust/brutexd`; the binary built in the dashboard integration job (the
  3-attempt flake-guard retry, the fno-math install pattern); a
  mutation.yml shard reservation (the MIGRATION.md #1597 sharded-matrix
  pattern); the diff-path pin above.
- **Hostile reviewers/refuters must attack:** panic→500 semantics (never
  process death); header drift (charset suffixes, missing
  cache-control); path traversal + symlinked static entries; proxy
  fidelity (chunked bodies, large uploads, connection reuse, the 502
  window on job-host restart — must surface as the honest 503 degrade, a
  NEW pinned class); the 127.0.0.1-only bind (no auth exists — widening
  the bind is out of scope, test-pinned); flag parse fail-loud;
  port-collision behavior; a proxy latency-flatness bench (the O(1)
  tiers — a copy per request must not move any pinned ratio).

### 2.5 Standing laws (apply to every wave)

- **Run-identity escalation rule.** The run-identity byte surfaces
  (blake3 run_id, `bxsnap2`, the canonical_trade_params JSON) are NEVER
  re-implemented in this track; any ported route found computing identity
  bytes = ESCALATION to the coordinator, not a port. The audit JSONL
  hash-chain: Rust never appends; `audit verify` is read-only,
  verdict-parity-gated.
- **Single lake.** Serving reads stay on the ONE parquet lake — no dual
  store, no second data copy (the parquet golden pins, flock /
  `LakeWriteHalt`, the hash-chained audit, and `data_snapshot_hash` all
  assume the single lake). The stat-token/revision-cache pattern +
  the MIGRATION.md #1614 stats-manifest planning are preserved via the
  common `bx-cache` crate; caches are re-implemented per family, never
  shared cross-process (keys are lake CONTENT tokens).
- **Frozen-crate / kernel boundary.** `rust/trading-core` is FROZEN
  (DEC-SHARED-CORE-001 TickVault contract) and the kernel-owned
  `rust/fno-math` sweep files are the engine track's — this track's PRs
  never touch either (CI diff-path pin, §2.4).
- **Never-500 + XSS re-proof by oracle reuse.** The never-500 laws and
  XSS hostile-twin suites are re-proven per SERVING family by replaying
  the EXISTING Python suites against the sidecar URL (a parametrized
  base-URL fixture) — oracle reuse, not test ports.
- **Dual-implementation windows.** Flag ON-by-default one release after
  the parity soak; the Python body deleted the release after; no module
  lives dual-headed > 2 releases; cross-language fixture regens must
  touch both suites (CI check).
- **Honest cost statement.** ~16 waves, multi-month (O(4-7 months)); test
  mass grows ~1.5-2× per converted module until shim retirement — priced
  exactly as the overridden verdict said, accepted under the operator's
  uniformity mandate.

### 2.6 Coordination constraints folded in (received 2026-07-11)

From the **arch-eval session** (trillion-scale; DB engine + data
contract scope): (1) KEEP DuckDB+Parquet as the query substrate
(ClickHouse/chdb tested + REJECTED); no objection to a Rust serving
layer — axum/tower over the duckdb Rust crate + arrow-rs is "the unexotic
default". (2) The honesty constraint quoted in §0: same scan shapes ⇒
same breaches; the ONE measured sub-second breach is the /results
`load_sweep_scan` O(stores×rows) scan, fixed by the persisted rollup, not
by language. (3) Baked-in: single lake / stat-token pattern via a COMMON
crate / rollup written at sweep-flush time / viewport-bounded payloads as
a wire-contract requirement independent of server language. (4) The PyO3
interim shape (payload builder behind the existing FastAPI route,
byte-identity gate, default-OFF) is approved. (5) Their ASK, honored in
the wave table: the two named follow-ups (R1, R2) are carried as blocking
waves 7a/8 — the conversion must not lose them.

From the **frontend-latency session** (`claude/frontend-latency-hardening`,
in flight): (1) any P12 work touching templates/static JS lands AFTER
that branch merges (their substring pins touch the same templates) —
hence wave 3's blocker. (2) Their Chromium-measured findings: frontend
slowness is network shape (duplicate pollers) + DOM rebuild — WASM helps
neither; no profiled client-compute hotspot exists. (3) The agreed
roadmap wording on frontend-WASM is quoted verbatim in §0.

---

## §3 Reporting + revisit conventions

- **Payoff-figure tags.** Every conversion PR in this program tags EVERY
  payoff figure it states as **MEASURED** (a dated bench pin in the PR),
  **MODELED** (an arithmetic estimate, inputs shown), or **UNKNOWN**
  (stated, not guessed). An untagged figure is a review BLOCKER. Latency
  claims must be MEASURED — §0 is the standing law.
- **Parity-gate law.** No parity proof, no flip: a wave's flag never
  defaults ON without its gate class (§2.2) green in CI at the head that
  flips it, and the Python deletion PR cites the soak evidence.
- **Frontend-WASM** stays declined on evidence — revisit ONLY on a
  profiled client-compute hotspot that the caps/browser-native fixes
  cannot address (the frontend session's wording, §0).
- **Audit hash-chain**: OUT OF SCOPE for this slice (engine-slice panel's
  call); if `audit verify` volume becomes material, the ledgered
  AuditCursor incremental verify is the first move — an algorithm change,
  not a language port.
- **Deployment-model changes** (more than one box; a host where the venv
  is not mandatory) strengthen the ops-binary track's ordering — they may
  pull wave 13 earlier, via a ledgered re-order, never silently.
- If the queued umbrella ledger slot #1713 renumbers, the pointer in this
  doc's header is the stale copy to fix.

---

## Honest limits (Golden Rule #10)

- The assessment took **zero new measurements**; every number cited is an
  existing, dated bench pin or a reader-verified in-repo figure. LOC
  counts are raw `wc -l` (comments + blanks included) — a size proxy, not
  an effort model. The roadmap's wave costs are the panel's judgement
  (MODELED at best), not measured port-costs.
- The override is recorded as received (operator directive 2026-07-11,
  relayed via the coordinator); the mandate's own ledger entry remains
  queued (#1713, in-flight PR) and its content is quoted, not yet
  verifiable in this repo at this head.
- This doc is NOT in the citation scanner's default normative set; cites
  are point-in-time, re-verified at the branch head, and the scanner was
  run locally WITH this doc force-included as an extra normative
  document.
- The roadmap schedules INTENT, not landed work: no crate, flag, route,
  or binary named in §2 exists at this head. PR-1 is spec'd (§2.4), not
  shipped. ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no
  scope/vocab/rate change; this PR is docs-only.
