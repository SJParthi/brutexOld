# ADR-0019 — Slice 9: orchestrator + NSE-calendar Rust conversion, parity-gated by frozen goldens (live→Rust, dead→DELETE)

**Decision IDs:** `DEC-ORCH-CALENDAR-RUST-CONVERT-001` +
`DEC-AUDIT-CHAIN-PARITY-GOLDEN-001` + `DEC-CALENDAR-VERDICT-GOLDEN-001`
(MIGRATION.md entry #1727)
**Date:** 2026-07-11
**Status:** **ACCEPTED** — the two parity goldens ship NOW in the owning PR
(`claude/orch-calendar-rust-verdict`); the Rust port implementation + the
dead-code deletions land in this slice's PR as LATER commits behind the
goldens (separate implementation phases, coordinator-staffed). ENGINE
UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no Groww scope/vocab/rate-cap change;
zero production-behavior lines in the goldens phase.
**Author:** the slice-9 implementation session (four reader reports + three
designs + a judge verdict, then an OPERATOR OVERRIDE — see §2).
**Builds on / relates to:** ADR-0012 (D8 calendar-data ruling —
**ACCOMMODATED, not superseded**; §5); DEC-SHARED-CORE-001 (#441);
DEC-BXSNAP2-PARITY-GOLDEN-001 (#1695 — the goldens-first parity pattern);
DEC-SWEEP-RUST-METRICS-DEFAULT-ON-001 / ADR-0017 (#1531–#1532 — the
default-OFF + burn-in + retained-oracle rollout pattern);
DEC-INDICATOR-TWINS-RUST-SINGLE-SOURCE-001 (#1548 — the delete-the-twin
precedent); DEC-EXECUTION-TRIO-DELETE-001 (#1550 — the dead-scaffolding
deletion + keep-frozen-containers precedent); DEC-RUST-FIRST-MANDATE-001
(#1713 — the umbrella Rust-first mandate; at this ADR's ship time #1713 is
QUEUED on Slice-1's PR, not yet merged to main — cited as the program
umbrella, not as a landed entry).

---

## 1. Context

Slice 9 of the Rust-everywhere program covers `brutex/orchestrator/`
(16 modules + `__init__`) and `brutex/calendars/nse.py` (+
`observed_sessions.py`). Four reader reports and three independent designs
established the evidence base (every claim file:line-cited and re-verified at
the ship HEAD `a268bd4c`):

* Every orchestrator **hot primitive is already Rust or C**: the Bloom bit
  array is the third-party `fastbloom_rs` PyO3 wheel + `fno_math.popcount_bytes`
  (`brutex/orchestrator/bloom.py:123-167`), SHA-256 is CPython's C `hashlib`,
  LMDB is C.
* The **audit chain is fsync-bound** (one `flush` + `os.fsync` per append,
  `brutex/orchestrator/audit.py:549-562`) with FROZEN on-disk bytes whose
  serialization is CPython-specific (§4).
* The **rate limiter is vendor-bound** (live-log audits MIGRATION.md
  #921/#1022 measured ~2.75–3.2 r/s achieved at the 8 r/s ceiling — network,
  not CPU; ≤8 acquires/s, sleep-dominated) and Golden-Rule-#6
  approval-locked.
* The **calendar is DATA plus thin verdict logic**; its hot consumers memoize
  to O(1)/bar (`brutex/engine/trade_sim.py:465-497`,
  `brutex/data/session.py:250-262`) and the existing Rust seam already takes
  pre-resolved scalars (`brutex/feed/bar_metadata.py:276-284`).
* A large fraction of the orchestrator package is **dormant scaffolding**
  with zero production constructions (grep-verified at HEAD: no
  `WorkItemStore(` construction in `brutex/`; `adaptive_rate_limiter.py` has
  zero brutex/ importers; `phase_throughput.py`'s only brutex/ reference is a
  docstring mention in `parallel.py:45`; `quota_tracker.py` is already
  ledgered MOOT with AST-pinned zero constructions,
  DEC-QUOTA-VERDICT-MOOT-001).

The judge verdict of 2026-07-11 scored a keep-Python discharge highest on
this evidence. **The operator then overrode that verdict the same day
(relayed via the coordinator)** and ratified the program-wide principle this
ADR records as binding:

> **"Rust EVERYWHERE" means: LIVE code → Rust; DEAD/dormant code → DELETED,
> never blind-converted.**

The conversion is executed **for the operator's Rust-everywhere uniformity
decree, not for speed**: the expected performance payoff of every conversion
in this slice is **≈ 0** (the payoff tags in §3 stand un-upgraded — fsync-,
vendor- and memo-bound surfaces; Golden Rule #10 forbids claiming
otherwise). What the slice buys is uniformity plus the elimination of the
dead tail; what it risks is byte/verdict divergence — which is why the two
frozen parity goldens land FIRST and gate everything.

## 2. Decision

### D1 — Conversion scope and order (operator-ratified)

1. **CONVERT — the orchestrator SHA-256 audit chain** (`orchestrator/audit.py`
   serialization + hashing + chain verify), parity-gated by
   `tests/golden/test_audit_chain_parity_golden.py` (§4).
2. **CONVERT — the NSE-calendar VERDICT LOGIC** (`calendars/nse.py`'s
   trading-day / session / triage / forced-exit / cutoff / back-walk
   verdicts), parity-gated by
   `tests/golden/test_calendar_verdict_table_golden.py`; calendar **DATA does
   not move** (§5).
3. **CONVERT LAST — the rate-limiter TOKEN MATH** (`orchestrator/
   rate_limiter.py`'s bucket arithmetic), per the Designer-1 bit-exact IEEE
   design: a pure, clock-free, lock-free arithmetic kernel
   (`bucket_refill` / `bucket_consume` / `bucket_more_wait` /
   `bucket_set_rate` on the `fno_math` wheel — refill/consume/wait are IEEE-754
   `+ - * / min max` only, NO libm transcendental; operation ORDER transcribed
   literally, no re-association, no FMA) with the Python
   `AdaptiveTokenBucket` keeping its `threading.Lock`, injected clock/sleep,
   deque, quota seam and event objects. **CLAUDE.md §2 Rule 6 is inviolable:
   every ceiling / floor / governor VALUE and the AIMD pacing BEHAVIOR stay
   byte-identical — the port converts the MATH, never the caps.** Care point
   named for the porting phase: the module's upshift logic uses
   `math.isclose(rel_tol=1e-6)` (`rate_limiter.py:377`, `:513`), so the pure-
   IEEE seam boundary is narrower than the whole module — the isclose
   comparisons either stay Python-side or get their own bit-exact treatment,
   decided (and golden-pinned) in the porting phase.
4. **KEEP — `orchestrator/bloom.py`**: the hot path already IS Rust
   (`fastbloom_rs` bit array + `fno_math.popcount_bytes`); the sizing math
   runs once per BloomFilter construction, uses platform-libm `math.log`
   (`bloom.py:369-373` — a 1-ulp platform difference can flip `m_bits`, so a
   "Rust port" would have to pass Python-computed logs across the FFI and
   would convert nothing), and `_compute_m_bytes` is a cross-module import in
   the ENGINE's preflight refusal path (`engine/sweep_cache.py:256`). Nothing
   to convert; nothing dead to delete.
5. **DELETE — the grep-proven-dead tail** (later commits in this same PR;
   the DEC-EXECUTION-TRIO-DELETE-001 pattern): the FSM/state/retry/pipeline
   scaffolding with no production driver (`state.py` / `retry.py` /
   `pipeline.py`), `store.py` (zero production constructions; LMDB access in
   production goes through other surfaces), and the dormant
   `quota_tracker.py` / `adaptive_rate_limiter.py`. **Keep-frozen-containers
   clause:** live SHAPE consumers survive — the state enums/events consumed by
   dashboard/notifications and the quota exception TYPES living in the ingest
   spines' global-stop sets get the keep-frozen-containers treatment; the
   EXACT keep-set is determined by a grep-proof pass in the deletion phase,
   never assumed here. `orchestrator/replay.py` (the L3 audit-replay oracle)
   and `watchdog.py` / `supervisor.py` / `auto_resume.py` / `parallel.py` /
   `universe_phases.py` are LIVE (or verification-oracle) surfaces and are
   NOT in the deletion scope; `phase_throughput.py` is a delete-CANDIDATE
   pending the same grep-proof pass (F3).

### D2 — The goldens-first parity-gate law

**A port flips ON only when it reproduces the frozen goldens BIT-FOR-BIT.**
The two fixtures shipped by this ADR are the acceptance oracles:

* `tests/golden/fixtures/audit_chain_parity_golden.jsonl` +
  `audit_chain_parity_scalars.json` (SHA-256-manifested in-test; **NEVER
  regenerated** — existing operator audit chains on disk share these byte
  semantics).
* `tests/golden/fixtures/calendar_verdict_table_golden.json`
  (SHA-256-manifested; regeneration is a calendar-DATA act requiring
  `BRUTEX_REGEN_CALENDAR_VERDICT_GOLDEN=1` **and** its own ledgered
  calendar-data DEC in the same commit).

A Rust replay of these fixtures (native tests in the owning crate replaying
the same JSON/JSONL — the `rust/trading-core/tests/golden_vectors.rs`
precedent) lands WITH the port, in the porting phase.

### D3 — Rollout doctrine (the #1531–#1532 pattern)

Each port ships **default-OFF behind a fail-loud env flag**, with a
**per-process burn-in self-check** (the first N kernel-served results
bit-compared against the retained Python oracle; a mismatch SHIPS THE ORACLE
VALUE and AUTOREVERTS the kernel for the process with one ERROR event) and
the **Python implementation retained as the oracle** until a separate,
ledgered default-ON flip DEC (the ADR-0017 shape). Flag names, burn-in
depth and the flip criteria are fixed in the porting phase's own entry.

### D4 — The escalation clause

A module whose exact bytes (audit) or exact verdict table (calendar) or
exact f64 bits (rate math) **cannot be reproduced in Rust escalates to the
coordinator** — a divergent port is NEVER shipped, silently or flagged. The
known hard case is the audit chain's CPython float-repr dependency (§4): if
the Rust f64 formatter cannot be made shortest-round-trip-identical over the
frozen vector table, the port STOPS and the coordinator decides (options
include freezing a hybrid where Python renders the floats — decided there,
not here).

### D5 — ADR-0012 D8 accommodation (the D8-compatible reading, operator-authorized)

ADR-0012 D8 rules the calendar tables are **DATA, not a kernel — "they stay
in `brutex.calendars`"** (verbatim at
`rust/trading-core/src/indicators/metadata.rs:19-24`). This slice's
conversion is the D8-COMPATIBLE reading, operator-authorized: **calendar
DATA stays in `brutex.calendars` and crosses the FFI as config/day-ordinal
parameters; ONLY the verdict LOGIC ports.** Constraints carried forward:

* **No chrono in trading-core** (the dependency-free TickVault link,
  `metadata.rs:29-38`); the only chrono-compatible wheel home is `fno-math`
  (whose version sits OUTSIDE the run identity — a calendar-logic port there
  never re-keys the sweep grid; a trading-core touch would, via
  `_indicator_version()`, `brutex/cli.py:6239` — the ledgered 0.3.0→0.3.1
  full-re-sweep precedent #1548).
* **The FFI-parameter doctrine** (the existing, verified seam): calendar
  VERDICTS cross as pre-resolved scalars / day-ordinal arrays — the
  `trading_core.derive_bar_metadata` session-open-seconds scalar
  (`feed/bar_metadata.py:276-284`), the `resolve_near_month` candidate-expiry
  ordinals, the per-day `forced_exit_seconds_for_session` memo
  (`engine/trade_sim.py:465-497`), and the Stage-1A `roll_schedule`
  day-ordinal list. The ported verdict engine receives the holiday /
  Muhurat / special-weekend / coverage-span data the same way: **as
  parameters, at runtime, from `brutex.calendars`.**
* **Build-time data baking is REFUSED**: (a) it reverses D8; (b) a
  holiday-data update would force a crate release (and a trading-core bump
  re-keys every run identity); (c) the observed-sessions overlay
  (`calendar_overlays/observed_holidays.json`) is RUNTIME lake data —
  structurally unbakeable, so a baked calendar is wrong by design the first
  time the overlay flags a day; (d) it desyncs the moment `HOLIDAYS_2027`
  lands. Parameters-across-the-boundary have none of these failure modes.
* The premise-refutation record stands (Designer-3 proof chain, re-verified):
  the existing `rust/fno-math/src/compat/trading_calendar.rs` is **NOT a
  delegation target** — it is data-less config-driven TickVault residue with
  zero non-test consumers, no in-repo feeder config, and a construction rule
  incompatible with the real data (its weekend-holiday `bail!` at
  `trading_calendar.rs:63-69` rejects 4 of the 19 published `HOLIDAYS_2026`
  dates, `calendars/nse.py:303-329`). In-repo Python/Rust calendar VALUE
  drift is structurally impossible today; the port therefore BUILDS a new
  parameter-fed verdict engine gated on the calendar golden — it does not
  touch the compat calendar (whose deletion is requested from the kernel
  track, F4).

### D6 — Honest payoff record (Golden Rule #10)

**Expected payoff ≈ 0 — the conversion is for uniformity, not speed.** No
throughput or latency improvement is claimed anywhere in this slice, and
none may be claimed by the porting phase without a measured benchmark. The
per-module tags in §3 are the standing record; a tag is upgraded only with
new cited evidence.

## 3. Per-module hazard map (the risk record for the porting + deletion phases)

Formerly the keep/convert verdict table; under the operator override it is
the HAZARD MAP each phase starts from. Payoff tags: MEASURED / MODELED /
STRUCTURAL-FACT (evidence cited; re-verified at `a268bd4c`).

| Module | Ratified disposition | Payoff tag (speed) | Port/deletion hazards |
|---|---|---|---|
| `audit.py` | **CONVERT** (parity-gated, D2) | MODELED ≈0 — fsync-bound (one fsync/append) | The §4 byte-hostage set: CPython float repr + canonical JSON; historical operator chains + the #1596 backup byte-prefix law; `\|`-delimiter injectivity; ints past float53 |
| `rate_limiter.py` | **CONVERT token math LAST** (D1.3) | MEASURED vendor-bound (~2.75–3.2 r/s at the 8 ceiling, #921/#1022) | §2 Rule 6 lock — values/behavior byte-identical; `math.isclose(rel_tol=1e-6)` at :377/:513 sits outside the pure-IEEE seam; 165-mutant zero-survivor gate must stay killable on the retained oracle arm |
| `bloom.py` | **KEEP** (already Rust where it counts) | STRUCTURAL: hot path IS `fastbloom_rs` + `popcount_bytes`; sizing once per construction | libm `math.log` at :369-373 (1-ulp → `m_bits` flip); `_compute_m_bytes` cross-module import in the engine preflight refusal path (`sweep_cache.py:256`) |
| `store.py` | **DELETE** (grep-proof pass first) | STRUCTURAL: zero production constructions; LMDB is C | Import-surface breakage — enumerate consumers of its TYPES before deletion |
| `state.py` / `retry.py` / `pipeline.py` | **DELETE** (keep-frozen-containers for live shapes) | STRUCTURAL: no production driver; enum/event SHAPES consumed by dashboard/notifications | `state.py` sits in the per-PR mutmut gate (ci.yml) — the gate wiring must be retired WITH the module; the keep-set (enums, `Outcome`, event types) is grep-proven, not assumed |
| `quota_tracker.py` | **DELETE** (already ledgered MOOT) | STRUCTURAL: zero constructions, AST-pinned (DEC-QUOTA-VERDICT-MOOT-001) | Its exception TYPES are LIVE in the ingest spines' global-stop sets — keep-frozen-containers |
| `adaptive_rate_limiter.py` | **DELETE** | STRUCTURAL: self-declared dormant, zero brutex/ importers | None known — the cleanest deletion |
| `replay.py` | **KEEP** (live verification oracle) | STRUCTURAL: the L3 audit-replay set-projection oracle | Not dead — it is the dedup replay contract; port only with a Rust consumer |
| `watchdog.py` / `supervisor.py` / `auto_resume.py` | **KEEP** | MODELED ≈0: OS/thread/subprocess/boot orchestration, O(1)/event | Python-idiomatic signal/subprocess surface — hostile to a port; no case |
| `parallel.py` / `universe_phases.py` | **KEEP** | MEASURED network-bound (the pull spine) / STRUCTURAL pure planning | A `parallel.py` port = rewriting the pull spine — never as a slice |
| `phase_throughput.py` | delete-CANDIDATE (F3) | STRUCTURAL: zero production callers (docstring mention only, `parallel.py:45`) | Confirm zero consumers incl. tests-only usage in the grep-proof pass |
| `calendars/nse.py` + `observed_sessions.py` | **CONVERT verdict logic** (D5); DATA stays | STRUCTURAL: hot consumers memoized O(1)/bar; Rust seam already takes pre-resolved scalars | Coverage-abstain TRIAGE (raise-vs-False-vs-fabricate) is load-bearing and frozen in the golden; the two WARN event names are frozen contracts; the observed overlay is runtime data — parameter, never baked |
| `fno-math compat/trading_calendar.rs` | KEEP as-is here; **deletion requested from the kernel track (F4)** | dormant residue, zero non-test consumers | Its weekend `bail!` is a live-side TickVault rule — do not "fix" it; do not feed it |

## 4. The audit-port hazard blueprint (what the goldens freeze)

The chain's `entry_hash` preimage is
`sha256(str(seq) | prev_hash | str(ts_utc) | actor | action | subject |
canonical_json(payload))` with a literal `|` delimiter (injective only
because `__post_init__` rejects `|` in the free-form fields), and the line
bytes are a hand-rolled canonical JSON: `json.dumps(sort_keys=True,
separators=(",", ":"), ensure_ascii=False, allow_nan=False)`
(`audit.py:176-208`, `:264-299`). Two CPython-specific behaviors are
load-bearing: **shortest-round-trip float repr** (`str(self.ts_utc)` and the
json float rendering — integer-valued floats render `.0`; `1e16`/`1e17`
flip to exponent notation; `-0.0` and the `5e-324` subnormal render
signed/denormal-exact) and **arbitrary-precision ints** (payload ints past
float53 must never round-trip through f64). Float-repr parity between
CPython and a Rust formatter is provable only STATISTICALLY over arbitrary
inputs — which is exactly why the frozen fixture + scalar vector table are
the acceptance oracle, and why D4's escalation clause exists. Blast radius
of a divergent port: every historical operator chain on disk reads corrupt
under the new verifier, and the #1596 off-box backup's audit byte-PREFIX law
breaks. Scope note: the ENGINE's BLAKE3 `sweep_audit` chain is a DIFFERENT
chain and is OUT of this slice (F1).

## 5. TickVault value-sync (named, NOT built)

If TickVault ever wants calendar value-sync, the sanctioned mechanism is a
**brutex-generated, golden-pinned JSON export** of the calendar data
(UNVERIFIED tags carried through), consumed by TickVault's config pipeline —
never a second hand-maintained table, never moving the data out of
`brutex.calendars`. Demand-gated: building it requires an operator-confirmed
TickVault demand signal + its own DEC (today it would be a dormant feature
with zero consumers — the #935/#1014 trap; and the recorded co-development
posture says no coupling beyond `rust/trading-core` exists).

## 6. Consequences / honest limits

* The goldens freeze CURRENT behavior. A legitimate calendar-data update
  (`HOLIDAYS_2027`, the pending 2026-11-08 Muhurat timings, a Jan-2026
  circular resolution) REGENERATES the calendar fixture under its own
  ledgered DEC — the tripwire makes that conscious, it does not forbid it.
  The audit fixture is NEVER regenerated (byte law).
* The **exchange-law tripwire** (NSE == BSE for every verdict — true today:
  both holiday sets identical, `NSE_ONLY_HOLIDAYS`/`BSE_ONLY_HOLIDAYS` empty
  by verified fact) FAILS on the first legitimately divergent circular and
  forces a conscious regen — by design.
* The **coverage-span tripwire** fails the moment `HOLIDAYS_2027` lands
  without a fixture regen — by design.
* No performance claim exists anywhere in this slice (D6).
* The goldens-phase commit changes ZERO `brutex/` source lines; the port and
  the deletions are separate later phases with their own gates. This session
  implements NEITHER (coordinator-staffed).
* A `retry.py` docstring defect (a phantom reference to the nonexistent
  `tests/golden/test_vendor_sdk_constants_frozen.py`) was found and is
  deliberately NOT fixed: `retry.py` is in the ratified deletion scope — a
  docstring fix would be churn on a dying module. Recorded here so the
  finding is not lost if the deletion-phase grep-proof pass ends up KEEPING
  the module (in that case, fix the docstring then).

## 7. Named follow-ups (none executed here)

* **F1** — engine `sweep_audit` (BLAKE3) parity golden, if that chain is ever
  port-targeted.
* **F2** — the deletion-phase grep-proof pass fixing the exact
  keep-frozen-containers set for the ratified deletions (state/retry/
  pipeline/store/quota_tracker/adaptive_rate_limiter), the
  DEC-EXECUTION-TRIO-DELETE-001 pattern (subsumes the former
  store-census/adaptive-deletion follow-ups).
* **F3** — `phase_throughput.py` dormant-telemetry disposition
  (delete-candidate; confirm in the same grep-proof pass).
* **F4** — deletion request for the dead `fno-math` compat `TradingCalendar`
  routed to the KERNEL TRACK (their crate — zero non-test consumers; not
  touched by this slice).
* **F5** — the TickVault calendar-export pattern (§5), demand-gated, own DEC.
