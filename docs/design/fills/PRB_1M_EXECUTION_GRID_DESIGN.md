> STATUS: DESIGN-REVIEW VEHICLE (PR-B, worst-case fills track). Not normative until the PR-B implementation lands with its own decision entry (a reserved slot in the fills ledger band). File:line citations in this document reference the PR-A branch `claude/fill-worst-case-only` (not yet pushed at time of writing), NOT this branch's tree — they are design references, to be re-verified against the implementation PR. STANDING CAVEAT (v2): EVERY PR-A cite re-verifies when PR-A pushes — including the adverse-extreme session_gap/EOD pricing basis that §2.7-2.8 build on (MAIN prices those terminal closes at the bar CLOSE today per #1743; the adverse-extreme basis is PR-A's re-anchor, and B3 deliberately returns the terminal SAME-BAR case to the close basis).
>
> VERSION: **v2 — assurance register-review round 1 FOLDED** (verdict FIX-FIRST; blocking findings B1–B6 + the §8 Q1–Q6 confirmed positions + the minors/notes, each quoted by finding id in place). Coordinator FINAL rulings 2026-07-15 applied: B1 = the provenance LAW is REQUIRED, not waived (structural ADR-0021 invariant + hash-fold belt; the dual-vendor golden is an impl obligation); B2 = **the SLIDE is RATIFIED as the shipped law, UNIFIED with PR-A** (the register's WITHHOLD stays fully drafted as the named alternative — §5.4 — with its flip-asymmetry cost stated; a non-blocking operator morning note). v1 = the pre-review draft @ 0443045dd.

# PR-B DESIGN — the 1-MINUTE EXECUTION GRID (design-for-review, NO code)

- **Proposed Decision ID:** `DEC-FILL-EXEC-GRID-1M-001` (ledger slot from the
  fills track's granted band #1825–#1829 → **#1826**, band provenance noted per
  house convention; own ADR per coordinator ruling 4 — number assigned at
  build, ADR-0023 is PR-A's).
- **Builds on:** PR-A end state, branch `claude/fill-worst-case-only` @
  `8281b405` (worktree `scratchpad/wt-fill-only` — DEC-FILL-WORST-CASE-ONLY-001
  #1825 / ADR-0023: `FillMode` single-member, `FillPolicy` deleted, adverse-max
  hardcoded, unconditional `fill_mode` identity key). Every file:line below is
  the PR-A worktree at that head unless marked otherwise.
- **Audience:** the assurance session — adversarial review against the
  extreme-scenario register BEFORE any implementation. Each register id gets
  its own § with the design answer + the pinning verify.
- **Status of the register source files (v2 UPDATE):** the fill register
  files (`extreme-scenario-register-p0-fill.md`, `-2.md`, `-3.md`, and
  `-p1-c.md`) were EMPTY at v1 design time (wiped); the v1 sections answered
  RECONSTRUCTED scenarios and said so. They are **RESTORED and
  AUTHORITATIVE** as of this v2 revision (2026-07-15): every section a
  register entry drives now quotes the restored register VERBATIM, and the
  assurance register-review's six blocking findings (B1–B6) — two of them
  exactly the reconstruction-vs-original DRIFT class the v1 caveat predicted
  (B1, B2) — are folded in place, each quoting its finding id. The v1
  UNVERIFIED-UNAVAILABLE caveats are retired section-by-section where the
  verbatim text now appears.

## §0 The operator law + settled interpretations (binding inputs)

**The law:** the decision timeframe is irrelevant to fill timing — whatever TF
generates the entry/exit signal, the fill resolves on the **NEXT 1-MINUTE bar
strictly after the signal bar's CLOSE**, priced **WORST-CASE within that
1-minute bar**. Signal TF = decision only; execution grid = 1-minute,
next-bar, worst-case.

**Settled interpretations (coordinator, disclosed to operator):**
1. **SL/TP monitoring ALSO moves to the 1m grid** — level detection AND fill
   per 1-minute bar (approved; disclosed).
2. **Same-1m-bar entry+exit = SHIP THE DEFERRAL** — an entry and an exit never
   both resolve on one 1m bar; the exit defers to the next 1m bar. The
   ordered-path bound (H-then-L conservative ordering, PR-A's accepted waiver
   at decision-TF granularity) is the REJECTED alternative here (§3.12).
3. **Sequencing** (`fill-worst-case-only-track.md:44-45`): PR-B is its own
   DEC+ADR — dual-feed, run-identity re-key, SL/TP on the 1m grid.
4. **Interim honesty clause being retired** (`trade_sim.py:69-73`): PR-A's
   decision-TF adverse extreme is "strictly MORE pessimistic than the
   operator's next-1-minute-bar law: a conservative-side interim". PR-B
   replaces that interim with the real law — worst-case fills generally become
   LESS punitive on coarse TFs (a 1-minute adverse extreme ⊆ the 5-minute
   bar's), while staying never-optimistic vs reality.
5. **Missing exact next-1m = the session-bounded SLIDE, RATIFIED as the
   shipped law and UNIFIED with PR-A** (coordinator final ruling 2026-07-15,
   the B2 fold — §5; the register's WITHHOLD stays fully drafted as the named
   alternative §5.4 with its flip-asymmetry cost stated).

---

## §1 ARCHITECTURE — dual-feed plumbing

### §1.1 What exists today (PR-A end state, verified)

- **One feed for signals AND fills.** Each sweep tuple builds
  `ResidentTradeSim(bars, bar_bits, tf_minutes=cfg.tf_minutes, …)` from THIS
  tuple's own decision-TF bars (`sweep_run.py:1038-1104`, `_build_trade_sim`;
  "built from THIS tuple's own bars/bar_bits/cfg.tf_minutes, never cached" —
  `sweep_run.py:1060-1062`). The Python walker (`trade_sim.py:241-877`) and the
  Rust kernel (`rust/fno-math/src/trade_sim.rs:214-568` `Walker`) walk that ONE
  stream: entry fills on bar N+1's adverse extreme
  (`trade_sim.py:643` / `trade_sim.rs:508`), SL/TP detect+fill on the
  decision-TF bar's `[low, high]` (`trade_sim.py:733-747` /
  `trade_sim.rs:441-448`), forced/EOD/session-gap on decision-TF bars.
- **The preload memo.** `cmd_sweep_all` preloads every requested
  `(symbol, interval)` slice ONCE under the G10 lake read-guard —
  `with lake_read_guard(resolved_lake), Catalog.open(catalog) as cat:` then
  `load_sweep_slice(...)` per pair (`cli.py:7339-7354`); tuples reuse the
  memo via `_bars_for` (`cli.py:7607-7608`); the read-guard is released once
  bars are in memory (`cli.py:7336-7338`, DEC-SWEEP-LOCK-PULL-CONCURRENCY-001
  posture).
- **The loader.** `load_sweep_slice` is the ONE alias-aware mouth
  (`feed/series_load.py:1-36`): spot verbatim via Catalog/ReplayFeed;
  the read boundary applies positivity (`feed/replay.py:336-347`) + the
  bad-tick screen (`feed/replay.py:330`).
- **Snapshot identity.** `data_snapshot_hash` folds ALL loaded slices —
  every `(symbol, interval) -> bars`, all 7 fields per bar, streamed BLAKE3
  (`engine/snapshot_hash.py:86-98`), computed over the preloaded `slices`
  mapping at `cli.py:7440-7442`.
- **1-minute reads already exist POST-sweep only:** the results-depth pass
  loads 1m reference bars via `_depth_reference_loader`
  (`cli.py:8033-8055`, `cli.py:10094-10119` — serves the sweep's own
  in-memory slice when `(symbol, "1minute")` was preloaded); `minute_path.py`
  is annotation-only math over those bars (its module docstring, lines 1-50).
  Nothing 1-minute participates in the hot fill path today.

### §1.2 The dual-feed design

**D1 — preload the execution slice for every swept symbol.** The preload loop
(`cli.py:7341-7354`) loads, per symbol, the union
`interval_list ∪ {"1minute"}`. The 1m slice enters the SAME `slices` mapping
under `(symbol, "1minute")` — so it is covered by the SAME G10 read-guard
span, the SAME catalog reconcile + read-boundary guards, and (decisively, §4)
the SAME `data_snapshot_hash` fold with ZERO change to `snapshot_hash.py`.
When `interval_list` already contains `"1minute"` the key is identical — no
double load, no double fold.

**D2 — a new stdlib leaf `brutex/engine/exec_grid.py`** (the `exit_index.py`
cycle-freedom pattern, `exit_index.py:26-31`) owning the signal→next-1m
mapping:

```python
@dataclass(frozen=True, slots=True)
class ExecGridMap:
    arm_source: tuple[int, ...]    # per 1m bar j: decision idx N whose
                                   # next-1m fill instant lands at j, else -1
    decision_of: tuple[int, ...]   # per 1m bar j: index of the LAST decision
                                   # bar whose label <= ts_1m[j] (owning
                                   # bucket; -1 before the first decision bar)
    n_decision: int
    n_exec: int

def build_exec_grid_map(
    decision_bars: Sequence[CandleBar],
    exec_bars_1m: Sequence[CandleBar],
    *,
    tf_minutes: int,
) -> ExecGridMap
```

**The mapping law.** Decision bars are labelled at bucket START
(`feed/resampler.py:15` + `:139` — "Start of the bucket (closed=left,
label=left)"). Decision bar N's close INSTANT is `ts_tf[N] + tf_minutes`.
The fill bar for a signal on N is

> `exec_idx(N) = min{ j : ts_1m[j] >= ts_tf[N] + tf_minutes }`

— the first 1m bar at-or-after the close instant. A 1m bar labelled exactly
`ts_tf[N] + tf` opens AT the close instant and trades strictly after it, so it
is "the next 1-minute bar strictly after the signal bar's close" (for a
contiguous aligned feed this is the bar labelled `ts_tf[N] + tf`; on a hole it
is the next STORED same-session minute — the RATIFIED slide law, §5).
`arm_source` is that relation inverted.

**Collision truth + THE WINNER LAW — B4 [GAP — arm_source collision] folded
(the v1 claim "at most one decision bar closes per 1m boundary, so
`arm_source[j]` is a scalar" was FALSE):** a ≥tf-minute interior hole that
crosses a decision-bar close boundary makes `exec_idx(N) == exec_idx(N+1)` —
two (or more) decision bars can slide onto ONE 1m bar. `arm_source[j]`
remains a scalar COLUMN by the deterministic WINNER LAW, stated ONCE here
and implemented FROM THIS ONE STATEMENT by the Python walker, the Rust
kernel AND the §3.11 diffcheck reference (never three re-derivations):

> **earliest-armed wins** — `arm_source[j] = min{N : exec_idx(N) == j}`
> (mirroring PR-A's pending-entry law: a pending entry is never re-armed or
> overwritten by a later signal, `trade_sim.py:253-258`); every LOSING
> decision index DROPS its signal with the counted event
> `signals_dropped_collision` (per-tuple total + capped WARN, the
> `_TAIL_HOLE_WARN_CAP` idiom).

The **boundary-spanning-hole FIXTURE** (a ≥tf-minute hole crossing a
decision close boundary → the earlier signal fills on the slid bar, the
later signal drops + counts) is a REQUIRED pin, run through all three
implementations (§3.11). All arithmetic is on the UTC-aware
`bar.timestamp` values both streams already carry (`data/models.py:114-115`) —
no IST conversion participates in the mapping (§3.2).

**Precomputed array vs binary search — DECIDED: precomputed array.**
Rationale: (a) the mapping is mask-independent, so it is paid ONCE per tuple
(one O(N_1m + N_tf) two-pointer merge over two chronological streams), while a
binary search would run per event per mask inside the O(N_masks · N_bars) hot
loop; (b) it follows the established derive-in-Python / consume-in-Rust
discipline — the kernel today receives ONLY precomputed columns
(`open/high/low/close` paisa + `is_forced` + `session_idx`,
`trade_sim.rs:622-632`) and holds NO timestamps; a kernel-side binary search
would require crossing a second timestamp truth over the FFI (a new
divergence surface, contra `rust_trade_sim.py:118-131`'s "single source of
truth" law); (c) O(1) lookup, cache-friendly, and the flat-skip optimisation
(§1.5) falls out of the same array for free.

**D3 — the walker walks the EXECUTION stream.** The primary loop becomes the
1m stream; decision bars contribute ONLY `bar_bits` signals (Stage-1A is
untouched — signals stay decision-TF) projected through `arm_source`:

- entry signal live at 1m bar j ⟺ `arm_source[j] == N ≥ 0` and
  `(bar_bits_tf[N] & mask) == mask` (the same superset test,
  `trade_sim.rs:600-617`);
- all pricing, excursion folds, SL/TP detection, forced/EOD/session-gap laws
  run per 1m bar (§2);
- `forced` flags and `session_idx` ordinals are derived FOR THE 1M STREAM
  (`is_forced_exit_bar(t, 1, forced_exit_seconds_for_session(...))` — the
  tf-aware straddle logic degenerates to the exact boundary minute at
  tf_minutes=1), via the same `derive_price_columns`-style Python derivation
  (`rust_trade_sim.py:271-297`, `:654-768`).

### §1.3 Concrete signature changes

**Python walker (`brutex/engine/trade_sim.py`).** `TradeSimWalker` keeps its
constructor knobs and gains the dual-stream batch entry; the streaming `push`
becomes 1m-bar-push with an `arm_decision: int` input instead of a per-bar
`entry_signal` bool:

```python
class TradeSimWalker:
    def __init__(self, *, direction: Side, tf_minutes: int,
                 stop_loss_paisa: int | None = None,
                 take_profit_paisa: int | None = None,
                 reference_stamper: TradeReferenceStamper | None = None,
                 detail_sink: list[TradeDetail] | None = None) -> None: ...
    def push_1m(self, bar_1m: CandleBar, *, entry_due: bool,
                exit_due: bool = False) -> ClosedTrade | None: ...
    def run_with_exit_indices(
        self, decision_bars, exec_bars_1m, entry_signals,
        exit_signals=None, *, exec_map: ExecGridMap | None = None,
    ) -> tuple[list[ClosedTrade], list[int]]: ...

def simulate_trades(decision_bars, entry_signals, *, direction, tf_minutes,
                    exec_bars_1m, exec_map=None, ...) -> list[ClosedTrade]
```

`exec_bars_1m` is REQUIRED (no default): no silent single-feed path survives
(§3.3). At tf == 1minute the caller passes the same sequence for both
(`decision_bars is exec_bars_1m`) — §6. `entry_due`/`exit_due` at 1m bar j are
the projections `signal[arm_source[j]]` — the batch wrapper computes them; the
streaming form takes them precomputed (the walker never re-times signals).

**Resident seam (`brutex/engine/rust_trade_sim.py`).**

```python
ResidentTradeSim(
    decision_bars, bar_bits, *, tf_minutes,
    exec_bars_1m: Sequence[CandleBar],
    exec_map: ExecGridMap | None = None,        # derived once if None
    reference_stamper=None,
    precomputed_columns: PriceColumns | None = None,       # decision grid
    exec_columns: PriceColumns | None = None,              # 1m grid (W2 twin)
)
```

Construction derives the 1m kernel columns (paisa OHLC + forced_1m +
session_idx_1m) through the SAME canonical seams (`_price_to_paisa`,
`get_session_checked` → `forced_exit_seconds_for_session` →
`is_forced_exit_bar` — `rust_trade_sim.py:654-768`), plus `arm_source` as an
`i64` column. `_to_trade_and_exit` maps kernel indices against the **1m**
window (`entry_ts = exec_bars_1m[entry_idx].timestamp`,
`rust_trade_sim.py:1070-1072` today reads the decision window). The
timestamp certificate (`bars_strictly_increasing`,
`rust_trade_sim.py:562`) is taken over the EXECUTION stream (exit indices are
1m indices — §3.8).

**Rust FFI (`rust/fno-math/src/trade_sim.rs`, both `MaskWord` widths u64 +
u128 via the ONE generic core `trade_sim.rs:589-733`).** The pyclass
constructors change shape — the walker prices ONLY on the 1m columns, so the
decision-grid OHLC columns DROP OUT of the FFI entirely:

```rust
ResidentSweepSim(            // and ResidentSweepSimU128
    exec_open:  Vec<i64>,    // 1m paisa columns, len = n_1m
    exec_high:  Vec<i64>,
    exec_low:   Vec<i64>,
    exec_close: Vec<i64>,
    exec_is_forced:   Vec<bool>,   // 1m-grid forced flags
    exec_session_idx: Vec<i64>,    // 1m-grid IST-date ordinals
    arm_source:       Vec<i64>,    // len = n_1m; -1 or decision idx
    bar_bits:         Vec<u64|u128>,  // len = n_tf (DECISION grid)
)
// batch entry UNCHANGED in shape (flagless per PR-A):
simulate_masks(masks, is_buy, sl_paisa, tp_paisa) -> Vec<Vec<TradeRecord>>
// TradeRecord unchanged: (pnl, mfe, mae, holding, entry_idx, exit_idx)
//   — holding/entry_idx/exit_idx are now 1M-BAR quantities (§7.2 semantics).
```

`build_columns` (`trade_sim.rs:640-691`) validates
`arm_source.len() == n_1m`, every non-negative `arm_source[j] < bar_bits.len()`,
and `arm_source` strictly increasing over its non-negative entries (each
decision bar maps to at most one fill bar — the column stores §1.2 WINNERS
only; collision losers were already dropped + counted at map build). `Walker::push`
(`trade_sim.rs:470-556`) keeps its (A)/(A2)/(B)/(C) ladder shape on 1m inputs;
`simulate_one_core` (`trade_sim.rs:702-733`) computes
`entry_signal = arm_source[i] >= 0 && W::is_superset(bar_bits[arm_source[i]], mask)`.
`MAX_PRICE_PAISA` boundary discipline (`trade_sim.rs:161`) unchanged —
`arm_source`/`session_idx` do equality/index work only, no arithmetic.

### §1.4 Memory envelope (honest numbers)

- **NIFTY 1m bar count 2020-01-01 → 2026-07-14 (computed this session):**
  1,705 weekdays in the span; at ~14-16 NSE holidays/yr (approximation —
  the exact per-year list lives in `calendars/nse.py`; UNVERIFIED count) ⇒
  ≈ 1,600–1,614 sessions × 375 bars + ~7 Muhurat sessions × ~60 bars ⇒
  **≈ 6.0×10⁵ 1-minute bars per index instrument** (600,000–605,700).
- **Kernel-resident cost per instrument:** 4×8 B OHLC + 1 B forced + 8 B
  session + 8 B arm_source ≈ 49 B/bar ⇒ **≈ 30 MB** — trivial.
- **Python `CandleBar` list cost:** `CandleBar` is a frozen slots dataclass of
  7 fields incl. 4 `Decimal`s + an aware `datetime`
  (`data/models.py:96-131`). Rough estimate ~0.4–0.7 kB/bar including the
  Decimal/datetime objects ⇒ **≈ 250–420 MB per index instrument
  (UNVERIFIED — must be MEASURED at build time; §8)**. Context: a
  tf=1minute sweep ALREADY loads exactly this slice today, so the box
  demonstrably holds one; the DELTA is that every coarse-TF coordinate now
  ALSO holds the 1m slice — for the 3-instrument §8 envelope, worst case
  ≈ 0.8–1.3 GB extra vs a coarse-only sweep, within the 48 GB envelope but
  stated, not waved away. The generic claim "≈ tf_minutes× more bars than the
  decision slice" holds exactly (605k vs 121k at 5m, vs ~1.6k at 1day).

### §1.5 Compute envelope + the flat-skip design lever

The trade-sim leg becomes O(N_masks · N_1m) naively — a **tf_minutes×
multiplier** on that leg (5× at 5m; the nominal "~375× at 1day" is MOOT —
tf=1day is structurally EMPTY under the law, §6b: every arm point drops at
the same-session gate and flat-skip jumps the whole stream). That is NOT
acceptable to hand-wave (§2 Rule 5 / the 2-4 day SLA, CLAUDE.md §8). The designed
mitigation: **flat-skip** — while the walker is FLAT with no pending entry,
NOTHING can happen until the next 1m bar j with `arm_source[j] >= 0`
(excursions fold only in-position; forced/session flags are only consulted
with a position or pending entry — `trade_sim.py:533`, `trade_sim.rs:484`).
The kernel precomputes `next_arm[j]` (next index with an arm point) once per
tuple and jumps flat spans, making the per-mask cost
**O(#decision-signals + Σ minutes-in-position + #trades)** instead of
O(N_1m). In-position spans must walk every minute (SL/TP law). Sparse masks —
the overwhelming majority under Stage-1B `min_hits` prefiltering
(`sweep_run.py:523-525`) — pay far below N_1m; dense long-hold masks approach
it honestly. The pure-Python walker and the diffcheck reference deliberately
do NOT implement flat-skip (naive full walk = the independence oracle);
a jump-vs-full-walk parity property pins behavior identity.
**Construction-cost ownership (assurance minor folded, NAMED):** the 1m
kernel columns + the `ExecGridMap` are mask- AND tuple-independent for a
`(symbol, tf)` coordinate — they are built ONCE per coordinate in a
preload-scoped memo OWNED by `cmd_sweep_all` beside the `_bars_for` memo
(`cli.py:7607-7608` precedent), never rebuilt per tuple. Bench gates
(with acceptance teeth): §7.5. NO wall-clock SLA is claimed
pre-measurement (Golden Rule #10).

---

## §2 THE FILL LAWS ON THE 1m GRID (normative)

All prices int64 paisa via the one `_price_to_paisa` seam
(`trade_sim.py:116-121`). "Adverse extreme" = HIGH for a BUY-side fill
(buy/cover), LOW for a SELL-side fill (sell/short) — exactly PR-A's law
(`trade_sim.py:26-73`) re-anchored on 1m bars.

1. **Entry.** Signal on decision bar N (Stage-1A/bar_bits, unchanged) → fill
   on 1m bar `exec_idx(N)` (§1.2 mapping) at that 1m bar's adverse extreme
   (BUY at 1m HIGH, SELL at 1m LOW). SL/TP levels anchor on this ACTUAL worst
   1m entry fill (the `_levels` law unchanged, `trade_sim.py:664-672`).
   Gates, unchanged in kind: the fill bar must share the arming decision
   bar's IST session date (R2-OC-2, now keyed on the 1m stream's
   `session_idx`) and must not be a forced-exit 1m bar.
2. **SL/TP monitoring** — per 1m bar: detection on the 1m bar's
   `[low, high]` (inclusive touch, `trade_sim.py:776-779` semantics); the
   fill is the adverse-max-vs-level ON THAT SAME 1m bar:
   BUY exit `min(level, low_1m)`, SELL exit `max(level, high_1m)`
   (the `trade_sim.py:733-747` / `trade_sim.rs:441-448` law re-anchored).
   **THE GAP-OPEN LAW, explicit (B5 fold):** a 1m bar that GAPS past an
   armed level fills at the ADVERSE-MAX vs the level WITHIN that bar — the
   same `min(level, low_1m)` / `max(level, high_1m)` formula — NEVER the
   untouched open, never the level itself when the bar's extreme is worse
   (§5b). No detect-coarse/fill-later lag exists anywhere (§3b.1).
3. **Tie law** — one 1m bar spanning BOTH levels fills the STOP first, always
   (`trade_sim.py:757-781`, `trade_sim.rs:354-357` — unchanged).
4. **Same-bar deferral (NEW, settled):** the entry 1m bar folds its range
   into MFE/MAE but NEVER exits — SL/TP detection for a position begins on
   the 1m bar AFTER its entry bar. (Supersedes, on the 1m grid, the PR-A
   same-push manage-the-entry-bar behavior — `trade_sim.py:560` manages the
   just-opened position on the entry bar. §3.12 carries the rationale, the
   rejected alternative, the B3 terminal carve-out, and the counted
   `exits_deferred` observability.)
5. **Forced session-close exit** — the first 1m bar whose minute reaches the
   SESSION's forced-exit instant (`forced_exit_seconds_for_session`,
   session-resolved per IST date — `trade_sim.py:455-486`) closes the
   position at that 1m bar's adverse extreme; boundary = session close − 10
   min (15:20 regular; early-close/Muhurat variants) — never a 15:20/375
   literal (§3b.2). Entry never fills at/after the boundary (gate in law 1).
   A missing forced-boundary minute displaces the fill to the first STORED
   at-or-after bar — a SLID fill, COUNTED per law 9's widened counter law.
6. **Mode-B signal exit** — exit signal on decision bar N → fill on 1m bar
   `exec_idx(N)` at its adverse extreme (symmetry with entry; replaces the
   "next decision bar's open-anchored fill", `trade_sim.py:690-701`).
   Implemented in full despite ZERO production callers (§8 Q5 SETTLED: no
   dormant second fill law; a negative/AST pin asserts no production caller
   passes exit signals — all five `simulate_trades` sites enumerated).
7. **End of data** — a still-open position closes at the LAST STORED 1m
   bar's adverse extreme (`_last_bar_exit_paisa` re-anchored,
   `trade_sim.py:794-804`) — **UNLESS that last stored bar IS the trade's
   entry bar** (the legalized `exit_idx == entry_idx` terminal degenerate):
   then the fill is that bar's **CLOSE** (the #1743 `pos.last_bar.close`
   basis), never the adverse extreme — the B3 fold, rationale in §3.12.
8. **Session gap** — an open position never survives an IST session-date
   change ON THE 1M STREAM: first 1m bar of a new session closes it
   retroactively at the PRIOR session's last stored 1M bar's adverse extreme
   (the finish() idiom — `trade_sim.py:562-585`, `trade_sim.rs:516-529` —
   re-keyed on 1m `session_idx`; §3.10) — with the SAME B3 terminal
   carve-out as law 7: if the prior session's last stored 1m bar is the
   trade's OWN entry bar, the fill is that bar's CLOSE, never its adverse
   extreme (§3.12). Pending exits subsumed; the boundary bar never arms;
   earliest re-arm = its successor.
9. **Missing minutes — the RATIFIED SLIDE law (B2, coordinator final
   ruling):** never a substitute/nearest-PRIOR bar, never a fabricated
   minute; the mapping's `>=` law slides a fill to the next STORED
   same-session minute — the nearest NEXT print, never a chosen-best one.
   Guardrails AS LAW: session-bounded; cross-session attempts DROP;
   worst-case pricing applies WITHIN the slid bar (the slide is never a
   price windfall); and EVERY slid fill is COUNTED — entries, Mode-B exits,
   AND missing-forced-boundary fills — via the widened `fill_slid_minutes`
   family (a stated LAW obligation, not an observability nicety; §5).
   Absence taxonomy + the WITHHOLD named alternative in §5.

---

## §3 P0 REGISTER SECTIONS (one per id — 13 folds)

> Register-source status (v2): the P0 fill register files are RESTORED and
> AUTHORITATIVE; each section below now quotes the register verbatim. The v1
> reconstruction caveats are retired; where the v1 reconstruction DRIFTED
> from the original (B1 §3.1, B2 §3.9) the section says so and folds the
> finding under its id.

### §3.1 [DV-FILL-BAR-VENDOR-AUTHORITY] — **B1 [DRIFT] folded (coordinator-ruled REQUIRED, not waived)**

- **Register (verbatim, restored file):** "Trigger: next-1m fill bar OHLC =
  whichever vendor won keep-first; slice has no same-vendor-as-signal
  invariant. Expected: fill bar shares provenance (vendor+lake gen) w/
  signal, or per-vendor engine; deterministic true adverse extreme; hash
  carries the slice vendor. Verify: golden same day both vendors both
  orders → fill price bit-identical; property fill provenance == signal
  vendor."
- **v1 drift (disclosed, per finding B1):** the v1 reconstruction answered a
  corrupt-vendor-MINUTE data-quality scenario. That material survives below
  as the secondary layer, but the register's ACTUAL hazard is the
  dual-vendor keep-first PROVENANCE RACE: with two vendors writing one
  lake, the 1m fill bar's OHLC would be whichever vendor won keep-first at
  pull time — fill prices become ingest-ORDER-decided, breaking Golden
  Rule #7 determinism.
- **Design answer — THE PROVENANCE LAW.**
  1. **Invariant (LAW):** the 1m fill slice shares provenance — vendor +
     lake generation — with the decision slice of the same coordinate,
     enforced at slice load: both slices load in ONE preload span under ONE
     G10 read-guard from ONE lake generation (§1.2 D1). **The fill bar's
     provenance == the signal's vendor; a provenance MISMATCH WITHHOLDS the
     fill (named, counted) — never a cross-vendor fill.** Provenance
     resolution is deterministic regardless of ingest order (Golden
     Rule #7): the same lake content yields the same fills no matter which
     vendor's pull landed first.
  2. **Mechanism (assurance option (a) — the STRUCTURAL argument is the
     invariant; the hash-fold is the belt):** under frozen ADR-0021 the
     ENGINE reads the PRIMARY tree only — Groww-exclusive; Dhan bytes live
     under `feeds/dhan/**` and never enter an engine slice — so decision +
     fill slices are same-vendor BY CONSTRUCTION in v1 and a runtime
     mismatch is UNREACHABLE. The reproducibility belt is the §4.1
     snapshot-hash fold (the exec slice's bytes are IN `data_snapshot_hash`,
     so whichever bytes the lake serves, the run identity carries them and
     any provenance change re-keys). PIN: an AST/negative pin that the
     engine slice loader (`load_sweep_slice` → `Catalog`) has NO
     `feeds/dhan/**`-tree path. **Option (b), named as the mechanism the
     implementation MUST add if/when dual-vendor engine reads ever become
     reachable:** a runtime provenance assertion at the fill selector —
     mismatch ⇒ a typed withhold, never a cross-fill.
  3. **Scope split (honest):** the INGEST-side vendor-authority surface
     (which vendor's bytes a dual-vendor lake stores) belongs to the Dhan
     sibling track. OUR invariant is ENGINE-side: same-slice provenance +
     the snapshot-hash fold. This is explicitly NOT deferred to the
     feed-divergence comparison vehicle — the fill law owns its own
     provenance.
  4. **Secondary (v1 material, still true):** the exec slice loads through
     the SAME one mouth as every sweep slice (`load_sweep_slice` →
     `Catalog.get_bars` → `arrow_table_to_bars`), inheriting positivity
     refusal (`feed/replay.py:336-347`, DEC-STORED-DATA-VALIDATION-001) +
     the bounded bad-tick screen (`feed/replay.py:320-330`,
     DEC-SWEEP-DATA-EDGE-GUARDS-001); the stored lake 1m stream
     (2dp-snapped, DEC-SPOT-PRICE-2DP-001) is the single price source — no
     second source, no resample-on-the-fly, no vendor re-fetch at sweep
     time; a screened/absent minute routes through §5, never a corrupt
     price.
- **Verify (register verbatim, adopted):** (a) the dual-vendor GOLDEN —
  same day, both vendors, BOTH write orders → fill price bit-identical —
  is an **IMPLEMENTATION OBLIGATION** on the PR-B impl PR, buildable once
  dual-vendor lake fixtures exist (the Dhan-track fixture dependency is
  named honestly: no dual-vendor fixture exists in this repo today); (b)
  the property `fill provenance == signal vendor` (vacuously structural in
  v1 — pinned via the no-Dhan-path AST pin above; becomes a live runtime
  property with option (b)); (c) the v1 secondary pins retained — a
  planted ×10 fat-finger 1m bar is screened (`feed.bad_tick_skipped`) and
  the entry routes per §5, never the corrupt extreme; a planted
  non-positive 1m price fails the tuple LOUDLY at construction
  (`SweepComboError` via `_build_trade_sim`, `sweep_run.py:1089-1096`).

### §3.2 [DV-FILL-TZ-SHIFT-WRONG-BAR] — **B6 [WEAK-PIN] folded**

- **Register (verbatim, restored file):** "Trigger: Dhan (tz UNVERIFIED) or
  a Groww epoch branch mislabels tz +05:30; 'next 1m after signal ts' then
  picks a different-time-of-day bar. Expected: tz VERIFIED per vendor before
  the selector; ambiguous tz WITHHOLDS; selector asserts same-session +
  bounded minute-distance. Verify: unit signal 10:00 IST + tz-shifted
  candidate → selector rejects/withholds; a tz-unverified vendor can't reach
  it."
- **Design answer:** the mapping (§1.2) is pure UTC-aware arithmetic on
  `bar.timestamp` (`data/models.py:114-115` — "UTC timezone-aware datetime at
  the bar's open") for BOTH streams: `exec_idx(N) = first ts_1m >=
  ts_tf[N] + tf_minutes`. IST enters ONLY where it already lawfully lives —
  session identity + forced flags — through the existing canonical seams
  (`astimezone(IST).date().toordinal()`, `rust_trade_sim.py:296`;
  `is_forced_exit_bar` on the IST wall time, `trade_sim.py:534-541`). IST has
  a FIXED offset (CLAUDE.md §6 timestamp row), so UTC interval arithmetic ≡
  IST interval arithmetic; no naive datetime can enter (CandleBar's contract
  is aware-UTC; the mapping does zero string/naive parsing).
- **B6 fold — the register's bounded-minute-distance assertion ON the fill
  selector (the v1 section argued tz hygiene but pinned no selector-side
  assertion):** the selector enforces, for every selected fill bar j of a
  decision bar N, BOTH (a) same-session (`session_idx` equality — the
  existing gate) AND (b) a bounded wall-clock minute distance:
  `ts_1m[j] − (ts_tf[N] + tf_minutes)` must not exceed the expected slide
  window — the bound IS the slide law's session boundary (the minutes
  remaining in N's arming session; forced-boundary fills are
  session-bounded by construction). A selected bar violating either arm
  fires a NAMED refusal/withhold (`fill_selector_distance_refused`, counted
  + capped WARN) — never a fill. A +05:30-shifted candidate grid violates
  (a) or (b) structurally (a 330-minute displacement lands cross-session or
  exceeds the remaining-session span). Defense-in-depth: the assertion
  holds even if a tz-mislabelled stream somehow survived ingest.
- **Scope note (register: "tz VERIFIED per vendor before the selector;
  ambiguous tz WITHHOLDS"):** per-vendor tz VERIFICATION — and the
  ingest-side withhold of a tz-UNVERIFIED vendor — is the Dhan sibling
  track's INGEST gate, not this engine seam ("a tz-unverified vendor can't
  reach it" is that gate's pin); the engine-side selector assertion above
  is the last-line net, not the verification.
- **Verify (register verbatim, adopted):** unit — signal 10:00 IST + a
  tz-shifted candidate grid → the selector rejects/withholds
  (`fill_selector_distance_refused` observed), never fills a wrong-time-of-
  day bar; property — the mapping is invariant under `astimezone(IST)`
  round-trip re-rendering; golden across a session-open boundary (signal on
  the 09:15 5m bar → fill at the 09:20 1m bar, IST, asserted via UTC
  values).

### §3.3 [FILL-DUALSTREAM-ABSENT]

- **Reconstructed scenario:** the sweep has NO second stream today — the
  walker/kernel consume ONE decision-TF window (`sweep_run.py:1060-1062`;
  `rust_trade_sim.py:434-457`). Absent plumbing, "next 1m" silently degrades
  to "next decision bar" (the PR-A interim) or the exec feed is quietly
  optional and a lake without 1m serves wrong-law fills.
- **Design answer:** `exec_bars_1m` is a REQUIRED parameter end-to-end
  (§1.3 — walker, resident seam, kernel constructor): there is structurally
  no single-feed call shape left. The preload (§1.2 D1) loads
  `(symbol, "1minute")` for every swept symbol; `preflight_slices`
  (`cli.py:7372-7393`) is extended so a swept coordinate whose 1m exec slice
  is EMPTY **REFUSES the launch loudly** naming the missing pair + the
  remedy (pull/prebuild), exactly the DEC-SWEEP-PREFLIGHT-001 posture —
  `--allow-missing-slices` deliberately does NOT waive the exec slice (a
  decision slice without its execution stream is not "partial coverage", it
  is an unrunnable coordinate).
- **Verify (register verbatim restored: "grep for a fill_bars(1m) param
  (none); golden 5m signal @10:00–04 fills @10:05 1m bar, not the 5m bar" —
  adopted + extended):** the register's golden (5m signal covering
  10:00–10:04 fills on the 10:05 1m bar, never the 5m bar);
  fails-before-style integration — `sweep-all` at tf=5m over a lake holding
  5minute partitions but NO 1minute → EXIT_USER_ERROR naming
  `(symbol, 1minute)`; kwargs/killed-token pins that no
  `exec_bars_1m=None` default exists on any public simulate entry.

### §3.4 [FILL-SIGNAL-TS-OPEN-VS-CLOSE-LOOKAHEAD]

- **Reconstructed scenario:** decision bars are labelled at bucket START
  (`feed/resampler.py:15`, `:139`). A mapping anchored on the LABEL ("next 1m
  after the signal's timestamp") fills DURING the signal bar — acting at
  09:16 on a 5m signal that is not determined until 09:20 — a hard
  look-ahead.
- **Design answer:** the mapping law anchors on the bucket CLOSE instant
  `ts_tf[N] + tf_minutes`, NEVER the label (§1.2). P-06 argument: the signal
  is a pure function of decision bar N's own data, fully formed strictly
  before `exec_idx(N)`'s first trade; the walk consumes 1m bars forward-only
  (the module's no-look-ahead law, `trade_sim.py:75-82`, carries over).
- **Verify (register verbatim restored: "property LookAheadBarrier + 1m
  suffix-indep: fill idx > coarse last-covered minute; mutate a ≤close 1m →
  trade unchanged" — adopted):** property — for every emitted trade,
  `entry_fill_bar.timestamp >= signal_bar.timestamp + tf_minutes` (strict
  for the label: entry bar's OPEN ≥ the close instant = fill idx strictly
  past the coarse bar's last covered minute); mutating any 1m bar at/before
  the coarse close leaves the trade unchanged (the register's second arm);
  a label-anchored mutant (`+ tf_minutes` dropped) fails both and fails the
  degenerate-case fixed-point suite (§6); plus the §3b.4 suffix-independence
  property on the 1m stream (mutating 1m bars after a trade's exit never
  changes it).

### §3.5 [FILL-SESSION-END-NO-NEXT-1M] (1m arm)

- **Register (verbatim, restored file):** "Expected: entry DROPPED; forced
  exits fill IN the current bar (worst-case), never the next session
  (session_gap) nor its 09:15 1m. Verify: unit next-1m-absent signal → zero
  trades; integration held position → forced_exit with in-session exit_ts,
  never session_gap."
- **PR-A disposition (verbatim, `fill-worst-case-only-track.md:100-104`):**
  "FOLDED-WITH-TEETH: last-bar-signal → ZERO trades pinned on BOTH sides
  (tests/golden/test_trade_sim_rust_golden.py::
  test_cross_session_last_bar_signal_yields_no_trade_both_sides + the
  fill-mode unit pins) + tests/unit/test_engine_trade_sim_fill_mode.py::
  test_forced_exit_ts_stays_in_session_never_session_gap."
- **The 1m arm:** a signal on a session's LAST decision bar (or any decision
  bar whose close instant lands beyond the session's last stored 1m bar) has
  no same-session next 1m bar. Related P1 (verbatim,
  `extreme-scenario-register-p1-b.md:17-21`
  [CAL-LASTBAR-NEXT1M-CROSSSESSION-FILL]): "Expected: the next-1m law defines
  the last-bar case + reconciles with the session-gap guard; never a silent
  price bar days away without a session_gap class."
- **Design answer:** the entry gate carries over structurally: a pending
  entry armed on decision bar N fills at `exec_idx(N)` ONLY when that 1m
  bar's `session_idx` equals N's arming session ordinal — else DROPPED
  (R2-OC-2 re-keyed on the 1m stream; `trade_sim.py:553-558` /
  `trade_sim.rs:504-511` shape preserved). A signal at 15:25+ on a regular
  day also dies on the forced-bar gate (exec bar at/after 15:20 → cannot
  open; `trade_sim.py:557`). For an OPEN position, the session-gap guard
  (§2.8) owns the cross-session shape. Muhurat: the last Muhurat minute's
  next-1m is next-session → drop/`session_gap`, per the same two laws.
- **Verify:** re-derive PR-A's two pins on the 1m grid (last-decision-bar
  signal → zero trades, both engines; forced exit_ts in-session), + the p1-b
  verbatim verify: "property i+1 in a different IST session →
  exit_reason==session_gap or a defined fill; golden the 6 Muhurat
  boundaries; differential old vs new."

### §3.6 [FILL-WORSTCASE-SIDE-SIGN] (1m arm)

- **Register (verbatim, restored file):** "Expected: Long entry=HIGH
  exit=LOW; Short entry=LOW exit=HIGH — every leg adverse; reuse
  minute_path.trade_excursions_1m per-side fold as the ONE COMMON sign
  source. Verify: property+golden for [o,h,l,c]: BUY entry==h/exit==l, SELL
  entry==l/exit==h; a high/low-swap mutant fails; worst pnl ≤ open-to-open
  pnl."
- **PR-A disposition (verbatim, `fill-worst-case-only-track.md:92-99`):**
  "FOLDED-WITH-TEETH: tests/property/test_trade_sim_worst_case_floor.py —
  test_entry_fill_is_the_adverse_extreme_side_correct (exact-equality BUY
  entry == bar HIGH / SELL entry == bar LOW) +
  test_worst_case_never_overstates_the_open_anchored_price … MUTANT
  EVIDENCE: the high/low swap applied to
  brutex/engine/trade_sim.py::_open_position → 2/3 floor properties FAIL".
- **The 1m arm:** the same side-sign law must hold with the extremes read
  from the 1M bar, both directions, both engines, every exit class.
- **Design answer:** unchanged law, re-anchored operands: entry BUY = 1m
  HIGH / SELL = 1m LOW (§2.1); SL/TP `min(level, low_1m)` /
  `max(level, high_1m)` (§2.2); open-anchored/forced/EOD/session-gap =
  the 1m bar's adverse extreme (§2.5-2.8). The sign sites remain the exact
  four seams (`_open_position` / `_resolve_level_hit`-fill /
  `_open_anchored_exit_paisa` / `_last_bar_exit_paisa` and their Rust twins).
- **Freeze-time obligation (assurance non-blocking note, folded):** the four
  sign seams above reconcile onto ONE COMMON side-sign source at freeze
  time — the `_open_anchored_exit_paisa` sign core extracted as the single
  adverse-side helper the other three consume (§11 COMMON; today they are
  four hand-synchronized sign expressions). This also resolves the
  §3.6-vs-§2.6 seam ambiguity EXPLICITLY: under §2.6 the Mode-B fill stops
  being open-ANCHORED (the decision-bar open drops out of the law entirely),
  but the seam's SIDE-SIGN logic is the survivor — `_open_anchored_exit_paisa`
  lives on as the common sign source, NOT as a retained open-anchored fill
  law.
- **Verify:** the PR-A floor-property suite re-derived over dual-stream
  fixtures — exact-equality side pins on the 1M extremes + the
  worst≤open-anchored dominance property per trade per exit class + the
  high/low swap mutant re-applied to the 1m fill sites (must fail ≥2
  properties, then revert clean — the PR-A mutant-evidence idiom).

### §3.7 [FILL-REPRO-1M-NOT-IN-SNAPSHOT-HASH]

- **Reconstructed scenario:** run identity folds `data_snapshot_hash` over
  LOADED slices (`snapshot_hash.py:86-98`; `cli.py:7440-7442`). If the exec
  stream is loaded outside the `slices` mapping, two sweeps over different 1m
  bytes (a re-pull healed a minute) share a run_id — stale dedup/tuple-cache
  serves wrong fills as fresh (Golden Rules #4/#7).
- **Design answer:** the exec slice enters the SAME `slices` mapping under
  `(symbol, "1minute")` (§1.2 D1), so `data_snapshot_hash` folds all 7 fields
  of every exec bar with ZERO derivation change — any single 1m bar
  difference re-keys every run identity in the grid (the `bxsnap2` streamed
  fold, `snapshot_hash.py:52-63`). The Stage-3 lockbox-train derivation
  (ADR-0007 A1, `snapshot_hash.py:44-49`) cuts the exec slice with the SAME
  lockbox exclusion law before folding — SETTLED per §8 Q3 (MANDATORY
  sharpening adopted): ONE BOUNDARY — `lockbox_start` computed once per
  instrument pair and applied to BOTH streams; where per-stream boundaries
  would diverge, the EARLIER boundary wins + a tail-skew WARN. Pins: no
  train trade prices an exec bar at/after the boundary (the WALK-side leak
  is the sharper hazard, not the hash) AND the train fold includes the CUT
  exec slices.
- **Verify (register verbatim restored: "unit hash differs on a single 1m
  field change while coarse constant; integration the tuple cache misses
  after a 1m edit" — adopted):** golden — flip ONE exec 1m bar's low by 1
  paisa ⇒ `data_snapshot_hash` moves ⇒ `mask_run_id`
  (`sweep_run.py:410-468`) + `grid_identity`
  (`sweep_supervisor.py:382-401`) move; integration — the tuple cache
  MISSES after a 1m edit while the coarse slice is constant; unchanged
  decision slice alone cannot alias. Plus a spy pin that the preload
  registered the `(symbol, "1minute")` key for every swept symbol.

### §3.8 [FILL-EXIT-TS-NOT-COARSE-BAR-EQUITY-CRASH]

- **Reconstructed scenario:** `build_equity_curve` maps `trade.exit_ts` into
  the DECISION-bar timestamp dict and RAISES when it is not a bar timestamp
  (`sweep_driver.py:146-153`); the metrics tail's dict path does the same
  (`sweep_driver.py:423-427`). With 1m-granular `exit_ts`, every mid-bucket
  exit crashes the tuple (or, worse, a "nearest bar" hack would silently
  misplace P&L).
- **Design answer:** realized P&L folds at the OWNING decision bar via the
  precomputed `decision_of` map (§1.2 D2): kernel/walker exit indices are 1M
  indices; the metrics tail translates `exit_idx_1m → decision_of[exit_idx_1m]`
  — an O(1) array read replacing the per-mask dict build in the certified
  fast path (`exit_index.py` discipline preserved:
  `exit_indices_consistent` gains the dual-stream form "bars_1m[idx].timestamp
  == trade.exit_ts AND decision_of[idx] in range"). The UNcertified fallback
  replaces the exact-equality dict (`build_ts_index`, `exit_index.py:62-71`)
  with a bisect: the owning decision bar is the last decision label ≤
  exit_ts in the same session; a trade whose exit_ts precedes the first
  decision bar or postdates the window still raises LOUDLY (the honest arm
  of today's `sweep_driver.py:150-153` refusal). Equity stays sampled per
  DECISION bar — the per-mask equity fold remains O(N_tf), NOT O(N_1m)
  (§1.5's compute honesty; the drawdown/metrics tail is untouched in shape).
- **Verify (register verbatim restored: "unit build_equity_curve with a 1m
  exit_ts raises today (fails-before); post-fix equity attributes correctly
  + exit_indices_consistent passes over 1m" — adopted):** fails-before — a
  1m-granular exit_ts trade fed to today's `build_equity_curve` raises
  `ValueError` (the pinned message `sweep_driver.py:152`); after — it folds
  at the owning 5m bar + `exit_indices_consistent` passes over the 1m
  stream; invariants Σ(equity deltas) == Σ(pnl) and fastpath≡fallback
  bit-parity under the certificate; a `decision_of` off-by-one mutant fails
  the parity.

### §3.9 [DV-FILL-1M-BAR-ABSENT] — **B2 [DRIFT] folded; register override RATIFIED (slide is the law)**

- **Register (verbatim, restored file):** "Trigger: 1m after the coarse
  close is absent (untraded/hole/bad-tick-screened/truncated-withheld/
  only-other-vendor) while a later 1m exists. Expected: WITHHELD/skipped w/
  a named reason (or tuple demoted) — NEVER fall back to signal-TF open or
  last close; any fabricated fill is counted+stamped. Verify: unit/property
  4 absence causes → skip/withhold, no fabricated price; grep the fill seam
  for a silent fallback."
- **v1 drift (disclosed, per finding B2) + the FINAL ruling:** v1 designed a
  session-bounded SLIDE — an override of the register's expected WITHHOLD —
  without presenting it as one. The assurance review flagged the drift; the
  coordinator then RULED (final, 2026-07-15): **the SLIDE is RATIFIED as
  the shipped law, UNIFIED across PR-A and PR-B** (a divergent missing-bar
  law between them is ruled a correctness defect), with the register's
  guardrail intent carried as LAW (§2.9: session-bounded · every slid fill
  counted · never a prior/substitute bar · cross-session DROPS ·
  worst-case-within-the-slid-bar). The register's WITHHOLD stays fully
  drafted as the NAMED ALTERNATIVE (§5.4) — a one-edit doc flip for the
  operator's morning note, with its flip ASYMMETRY cost stated there.
- **What the register's core prohibition still gets, unconditionally:**
  NEVER a fallback to the signal-TF open, NEVER the last/nearest-PRIOR
  close, NEVER a fabricated minute — the slide fills at the nearest NEXT
  stored print only, worst-case-priced, counted.
- **Design answer:** §5 in full (the four-cause taxonomy under the ratified
  slide + the widened counter law + the §5.4 withhold alternative).
- **Verify (register verbatim, adapted to the ruling):** §5's four
  absence-cause fixtures (one per cause — each asserting the slid fill's
  bar, price, and count; no fabricated price anywhere) + the grep/killed-
  token pin that NO nearest-prior/last-close/signal-TF-open fallback exists
  on any fill seam + the widened `fill_slid_minutes`-family observability
  pins (entries, Mode-B exits, forced-boundary fills).

### §3.10 [FILL-SESSION-GAP-GUARD-1M-STREAM]

- **Reconstructed scenario:** the open-position session guard
  (DEC-TRADESIM-OPEN-SESSION-GUARD-001 #1743) keys on the DECISION stream's
  session ordinals and prices the retroactive close at the decision bar's
  extreme (`trade_sim.py:562-585`; `trade_sim.rs:516-529`;
  `last_exit_basis_paisa`, `trade_sim.rs:117-124`). On the dual feed, a guard
  left keyed on the decision stream could fire on a different bar than the
  1m walk observes, or price the gap close at a coarse extreme.
- **Design answer:** the guard moves WITH the walk — it keys on the 1M
  stream's `session_idx` and prices at the prior session's LAST STORED 1M
  bar's adverse extreme (the finish() idiom carried to 1m; §2.8). The
  entry-session stamp (`_OpenPosition.entry_day_ist` /
  `OpenPosition.entry_session`) becomes the entry FILL 1m bar's ordinal. The
  session-tail-hole observability sweep (`_warn_session_tail_holes`,
  `rust_trade_sim.py:173-218`) runs over the 1M forced/session columns —
  same capped WARN discipline, now naming 1m-truth holes. The
  `exits_session_gap` aggregate counter (#1743's persistent signal) is
  unchanged in shape.
- **Verify (register verbatim restored: "unit two-session parity re-derived
  for 1m fills: open long into a gap closes at the prior session's last 1m;
  no next-session fill; exit_ts stays prior" — adopted):** two-session
  dual-stream fixture with a session-tail 1m hole → `session_gap` close
  priced at the prior session's last 1M bar low/high (exact-equality both
  engines; the B3 terminal carve-out arm — entry-bar-is-last-bar — priced
  at that bar's CLOSE), exit_ts staying in the PRIOR session; the #1743
  truncation-equivalence property re-derived on the 1m stream
  (prefix-truncation ≡ session_gap modulo reason label); Python↔Rust parity
  over the two-session hypothesis generator extended to dual streams.

### §3.11 [FILL-RUST-KERNEL-DIFFCHECK-PARITY]

- **Reconstructed scenario:** the R45 diffcheck reference re-states the fill
  law independently (`sweep_diffcheck.py:29-57`; AST-pinned import ban
  `:10-27`; never-adopt `:66-72`). If PR-B changes only the production
  engines, the reference diverges on EVERY sampled mask (mass false-SUSPECT);
  if the reference silently imports the new mapping, independence dies.
- **Design answer:** `reference_simulate` (`sweep_diffcheck.py:276+`) is
  REWRITTEN IN LOCKSTEP to re-state the FULL 1m law from scratch: its own
  dual-stream walk, its own close-instant mapping (a flat forward two-pointer
  — deliberately NOT importing `exec_grid.py`, which joins the AST import-ban
  list), its own 1m session/forced re-derivation, the same-bar deferral, the
  slide law, **the §1.2 collision WINNER LAW (B4 fold: earliest-armed wins,
  loser drops + counts — the reference implements it FROM the §1.2 one
  statement, exactly as walker and kernel do; one normative statement,
  three independent implementations, parity-pinned)**, SL-first tie — the
  independent-recomputation bar (`sweep_diffcheck.py:24-27`). The reference does NOT implement flat-skip
  (§1.5) — naive full walk is the point. K=200 sampling, blake3 seeding,
  never-adopt (`DiffCheckMismatch` ⇒ SUSPECT, production value always
  ships) all unchanged. The CI rehearsal keeps `CI_K_SAMPLES=8`
  (`sweep_diffcheck.py:116`).
- **Verify (register verify restored: "golden Rust-vs-Python field-by-field
  + diffcheck K=200: identical trades; an un-ported kernel trips
  diffcheck→SUSPECT (teeth), never passes silently" — adopted):** the AST
  independence pin extended (+`brutex.engine.exec_grid` in the
  banned-imports list); an off-by-one mutant in the KERNEL's `arm_source`
  consumption ⇒ `DiffCheckMismatch` fires on the rehearsal fixture
  (fails-before teeth); R45 clean on the re-locked goldens; the dual-stream
  hypothesis parity suite (walker≡kernel≡reference on sampled masks); the
  §1.2 boundary-spanning-hole COLLISION fixture run through all three
  implementations (each fills the earliest-armed winner on the slid bar,
  drops the loser, and counts `signals_dropped_collision` — B4).

### §3.12 [FILL-SAME-BAR-TIME-ORDER-PHANTOM-LOSS]

- **Register (verbatim, restored file):** "Expected: respect intra-bar
  causality (entry before exit); defer the exit to the next 1m OR bound the
  same-bar loss to a time-ORDERED path — no guaranteed full-range loss.
  Verify: property same-bar loss ≤ worst monotone (O,H,L,C) ordered path;
  golden a fast-reversal fixture must not report loss==(h−l) on every
  trade." (The deferral chosen below is the register's FIRST offered arm;
  the B3 dominance pin is the register's own property.)
- **PR-A disposition (verbatim, `fill-worst-case-only-track.md:105-108`):**
  "WAIVED-ACCEPTED — verbatim rationale recorded in
  docs/adr/0023-worst-case-fills-only.md: 'H-then-L = conservative bound;
  swap-mutant-fails + worst_pnl≤open_anchored_pnl property'."
- **The 1m question:** within ONE 1-minute bar the H/L order is unknowable;
  an entry at the 1m HIGH plus a same-minute SL fill assumes an intra-minute
  path order — the phantom-loss shape at its finest granularity.
- **Design answer (SETTLED — SHIP THE DEFERRAL):** an entry and an exit never
  resolve on the same 1m bar; the entry bar folds MFE/MAE but SL/TP detection
  begins on the NEXT 1m bar (§2.4). Cost, stated honestly: one minute of
  undetected exposure per trade — the adverse move during it lands in MAE and
  in the eventual (worse) exit, never hidden. **Rejected alternative (argued,
  per the coordinator instruction):** keep same-bar detection under the
  ordered-path bound — H-then-L conservative ordering with the SL-first tie +
  the `worst_pnl ≤ open_anchored_pnl` dominance property (PR-A's accepted
  waiver). Rejected because (a) at 1m granularity the deferral costs at most
  ONE minute (vs five-plus at decision-TF, where rejecting the waiver was
  disproportionate), (b) it removes the LAST fabricated intra-bar ordering
  from the engine — no fill anywhere depends on an assumed path within a bar,
  (c) it makes the same-minute entry+exit class structurally impossible
  rather than bounded. The PR-A ADR-0023 waiver text remains true FOR the
  decision-TF interim it governed; PR-B's ADR records the supersession.
- **B3 [GAP — same-1m deferral degenerate] folded — the terminal carve-out
  that makes the claim TRUE:** the deferral has a legal degenerate the v1
  draft contradicted itself on. A position whose entry 1m bar is the
  stream's LAST stored bar (`end_of_data`) or a session's last stored bar
  (`session_gap`) has NO successor to defer onto — `exit_idx == entry_idx`
  is LEGALIZED for exactly those two terminal classes. Pricing that
  terminal same-bar close at the ADVERSE EXTREME (v1 §2.7/2.8) would BE the
  full-range phantom loss through the back door: entry at the 1m HIGH +
  same-bar exit at the 1m LOW. So the terminal same-bar fill prices at the
  bar's **CLOSE** — the #1743 `pos.last_bar.close` basis (main's own
  session-gap price basis) — restated normatively in §2.7/2.8. With the
  close basis, the §3.12 claim "the same-minute entry+exit class is
  structurally impossible" is now true WITHOUT exception for path-ordered
  losses: no fill anywhere depends on an assumed intra-bar path order (a
  close is a real single print, not an ordered pair of extremes).
  **Deferral observability (B3):** the counted `exits_deferred` event — a
  trade whose ENTRY 1m bar's range already touched an SL/TP level, with
  detection deferred by §2.4 — makes the deferral's cost MEASURABLE
  (per-tuple total; aggregate wiring per the `exits_session_gap`
  append-only counter idiom, #1743).
- **Verify:** golden — an entry 1m bar whose own range pierces the SL level
  produces NO same-bar exit (both engines; fails-before against a same-bar
  variant) AND counts `exits_deferred == 1`; property — for every trade,
  `exit_bar_idx_1m > entry_bar_idx_1m` unless the exit class is
  `session_gap`/`end_of_data` priced at the entry bar's CLOSE
  (last-stored-bar degenerate, where idx equality is legal); **the B3
  dominance pin** — for every legalized same-bar terminal trade,
  |pnl| is bounded by the ordered-path worst for that bar (the close lies
  within `[low, high]`, so the H-then-L/L-then-H bound holds structurally;
  the pin keeps a price-basis regression from re-opening the phantom); the
  deferral-drop mutant (re-enabling same-bar manage) fails the golden + the
  property; a close-basis→extreme-basis mutant fails the dominance pin.

### §3.13 [FILL-KERNEL-BURNIN-GLOBAL-AUTOREVERT]

- **Reconstructed scenario:** the sweep-metrics Rust tail runs a per-process
  burn-in self-check with a GLOBAL autorevert latch — a mismatch disables the
  metrics kernel for the remainder of the process
  (`engine/rust_metrics.py:41-52`, `:197`, `:260-278`,
  `sweep_rust_metrics.kernel_autoreverted` ERROR). Fear: a fill-law skew
  (stale wheel / half-updated engine) either (a) trips the global latch —
  masking a correctness bug as a process-wide performance degrade — or (b)
  never trips anything and a stale kernel silently prices the OLD law.
- **Design answer, two prongs.** (a) **The burn-in is fill-law-INDEPENDENT
  by construction:** it bit-compares the metrics kernel vs the Python
  `_drawdown_stats` oracle over the SAME per-trade pnl series both receive —
  the trades are an INPUT to that comparison, so a changed fill law changes
  both sides identically and cannot trip the latch; PR-B does not touch
  `rust_metrics.py`. (b) **Stale-TRADE-SIM-wheel skew is closed by the PR-A
  demotion idiom, extended:** the 1m-grid kernel's constructor gains new
  REQUIRED parameters (`arm_source` + the exec columns, §1.3), so the seam's
  signature inspection (`_kernel_fill_law_current` pattern,
  `rust_trade_sim.py:369-391`; `_STALE_KERNEL_FLAGS` `:361-366`) is extended
  to a `_kernel_exec_grid_current` probe — a wheel predating the dual-feed
  constructor demotes the WHOLE window to the bit-exact Python oracle with
  warn-once reason `exec_grid_kernel_stale` (the `fill_law_kernel_stale`
  twin, `rust_trade_sim.py:628-637`). Per-WINDOW, loud, never wrong numbers,
  and never a process-global latch. The R45 diffcheck (§3.11) is the
  independent net for a half-updated kernel that passes the signature probe.
- **Verify (register verbatim restored: "unit sweep w/ 1m exit instants →
  no kernel_autoreverted latch, kernel stays engaged; a genuine kernel bug
  still autoreverts (teeth)" — adopted):** a sweep whose exits land on 1m
  instants absent from the coarse set produces NO `kernel_autoreverted`
  latch (the metrics kernel stays engaged) while a genuine metrics-kernel
  bug still autoreverts (teeth); a monkeypatched trade-sim kernel exposing
  the OLD (pre-dual-feed) constructor/signature ⇒ construction demotes with
  `exec_grid_kernel_stale`, trades == oracle trades bit-for-bit; negative
  pin — no new global latch exists in the trade-sim seam
  (grep/killed-token).

---

## §3b P1 REGISTER SECTIONS (4)

### §3b.1 [FILL-SL-TP-DETECTION-VS-FILL-LAG]
(verbatim register, `extreme-scenario-register-p1-b.md:23-27`)

- **Register expected:** "pin (a) detect on the coarse [low,high] then fill
  on the following 1m (a lag that can skip the touch minute), OR (b) monitor
  the 1m path itself — one documented law."
- **Design answer: OPTION (b), normatively §2.2** — the 1m path IS the
  monitor; detection and fill happen on the SAME 1m bar (adverse-max vs
  level). No detect-coarse/fill-later lag exists anywhere in the design; the
  coarse bar plays no role in exits at all.
- **Verify (register verbatim):** "golden a coarse bar spanning the stop
  while an interior 1m pierces deeper → which minute/price; a
  detection-mode-swap mutant fails." Designed instance: 5m bar spans SL at
  −80; interior minute 3 pierces to −95 → exit ON minute 3 at
  `min(level, low_1m3)` = the pierced depth; a coarse-detection mutant fills
  a different minute/price and fails.

### §3b.2 [FILL-FORCED-EXIT-1M-UNDERSPECIFIED]
(verbatim register, `extreme-scenario-register-p1-b.md:29-33`)

- **Register expected:** "fill at the session-correct forced 1m bar
  worst-case (LOW long/HIGH short) from forced_exit_seconds_for_session, NOT
  a 15:20/375 literal; last-Muhurat-minute → session_gap."
- **Design answer:** §2.5 verbatim — the forced 1m bar is resolved per
  SESSION (`forced_exit_seconds_for_session` via the walker's memoised
  per-date resolution, `trade_sim.py:455-486`, and the kernel's precomputed
  1m `is_forced` column derived through the same seams,
  `rust_trade_sim.py:741-756`); fill = that 1m bar's adverse extreme. A
  position still open when a Muhurat session's stored tail ends without a
  forced bar closes via the §2.8 session-gap law (the register's
  "last-Muhurat-minute → session_gap" arm).
- **Verify (register verbatim):** "golden open long @15:20 fills @15:20 1m
  LOW; half-day/Muhurat forced fills worst-case; grep no 15:20/375 literal
  drives fill selection."

### §3b.3 [FILL-LAW-REKEYS-EVERY-GOLDEN]
(verbatim register, `extreme-scenario-register-p1-b.md:35-39`)

- **Register expected:** "a deliberate ledgered re-lock of
  _GOLDEN_CONTENT_HASH/_GOLDEN_RUN_ID + the 400-case vocab golden, diff
  proven EXACTLY the fill change; the diffcheck reference sim moves in
  lockstep."
- **Design answer:** §7.1 (the full golden inventory). The vocab golden
  (`tests/golden/fixtures/vocab_golden_vectors.json`) is indicator-layer and
  stays BYTE-UNTOUCHED — pinned by the same `git diff`-empty proof PR-A used
  (`pra_checkpoint.md`: "vocab fixture diff = 0 lines at final HEAD"). NO
  indicator_version bump: PR-B touches no indicator math (the register's
  "the indicator_version bump is separate in the ledger" — there is none
  here; the re-key rides `data_snapshot_hash` growth + the new identity key
  §4 + `code_commit_sha`). The reference sim rewrite is §3.11.
- **Verify (register verbatim):** "golden+census regen → only
  price/ts/pnl fields change; the pre-change goldens FAIL loudly; the
  indicator_version bump is separate in the ledger." Designed instance: the
  PR-A trade-census idiom (#1743's "1661 golden trades" style) re-run — the
  regen diff shows ONLY fill-price/ts/holding/identity fields moving; the
  PR-A `_GOLDEN_RUN_ID` (`715212420d…`, `pra_checkpoint.md:257-259`) must
  FAIL against the PR-B tree before re-lock.
- **Census obligation (assurance non-blocking note, folded):** the golden
  re-lock census must NAME the exit-reason / `exits_*` counter
  REDISTRIBUTION the same-bar deferral causes — trades that exited on their
  entry bar under PR-A move to next-bar SL/TP, `end_of_data`, or
  `session_gap`, so the per-reason counts SHIFT even where the pnl columns
  alone read like pure price moves — plus the NEW counters entering the
  aggregate columns (`exits_deferred`, `signals_dropped_collision`, the
  widened `fill_slid_minutes` family), each accounted in the census, never
  a silent column drift.

### §3b.4 [FILL-LOOKAHEAD-PROPERTY-SUFFIX-INDEP-1M]
(verbatim register, `extreme-scenario-register-p1-c.md` — RESTORED at v2;
the v1 reconstruction matched the original intent, caveat retired)

- **Register expected (verbatim):** "extend suffix-independence — mutating a
  1m after a trade's exit 1m can't change it, and a 1m at/before the coarse
  close can't change the fill." The standing suite it extends:
  `tests/property/test_sweep_suffix_independence.py` (the #1181 permanent
  layer) — trades over bars `0..N` bit-identical whether EXEC bars after
  the last consumed instant are absent, mutated, or extreme.
- **Design answer:** the property suite gains the dual-stream arm: for a
  fixed decision stream + signals, mutating any 1m bar strictly after a
  trade's exit 1m bar (and after the last consumed arm point) leaves that
  trade bit-identical; truncating the exec suffix likewise (modulo the
  legal `end_of_data` reclassification of the final open position — the
  #1743 truncation-equivalence shape). The flat-skip kernel arm (§1.5) runs
  under the SAME property (jump tables are derived from arm points, which
  are decision-side data — no future exec bar can move an earlier jump).
- **Verify (register verbatim):** "property two-stream suffix-independence
  over the 1m stream; a deliberate look-ahead fill (a 1m inside the coarse
  bar) FAILS the extended gate" — plus the §3.4 mapping property.

---

## §4 RUN IDENTITY

1. **`data_snapshot_hash` folds the 1m execution slice — all 7 fields.**
   Mechanism: §1.2 D1 / §3.7 — the exec slice lives in the preloaded `slices`
   mapping, and the ONE derivation (`snapshot_hash.py:34-98`, `bxsnap2`
   streamed fold of timestamp/open/high/low/close/volume/open_interest per
   bar) covers it with zero code change. Consequence: a healed/re-pulled 1m
   minute re-keys the grid — dedup/tuple-cache can never serve pre-heal
   fills as fresh (Golden Rules #4/#7).
2. **A new canonical param — DECIDED: YES, unconditional.**
   `canonical_trade_params` (`sweep_run.py:705-781`) gains
   `params["execution_grid"] = "1m_next_bar"` emitted UNCONDITIONALLY,
   directly below the PR-A `fill_mode` emit (`sweep_run.py:771-780`), with
   the same anti-aliasing rationale: **without the key, a tf=1minute run
   under the PR-B law would alias its PR-A-era identity** — same bars, same
   params, same snapshot — while the trades DIFFER (the same-bar deferral,
   §2.4, changes results even in the degenerate case §6). `code_commit_sha`
   is not a durable discriminator for artifacts (the verify/recount law
   reads recorded params, not the code that wrote them — the PR-A refusal
   law's own logic, `sweep_run.py:777-780`). Mirrors, exactly the PR-A
   mirror set: `sweep_supervisor._grid_descriptor` (`:375-378` idiom) +
   `_tuple_cache_params` (`:465-468`), `sweep_orchestrator._tuple_digest`
   (`:799-803`), `cascade_wire._spec_to_dict`/`_spec_from_dict` — the
   deserialiser REFUSES an absent key or any value ≠ `"1m_next_bar"` naming
   `DEC-FILL-EXEC-GRID-1M-001` ("decision-TF-era artifact …; re-run the
   sweep"), and `sweep_verify`'s generation gate grows the same arm beside
   `_require_worst_case_entries`. PR-A-era artifacts thereby refuse LOUDLY,
   never replay silently (coordinator ruling 3's posture carried forward).
   **Cascade-manifest posture (§8 Q2 SETTLED): the manifest carries the law
   BY DERIVATION, never payload** — the `execution_grid` key in the spec
   dict is the ONE schema addition (refuse-on-absence is the sole refusal
   law); the exec coordinates derive as instruments × `"1minute"` in BOTH
   `load_bars_for_manifest` AND `manifest_snapshot_hash`, so the G7
   snapshot-hash check auto-refuses a healed lake, and bar bytes never
   enter the manifest.
3. **Every run_id re-keys ⇒ full re-sweep on deploy — documented.** Three
   independent re-key forces: the snapshot grows the exec slices, the params
   dict grows the key, and `code_commit_sha` moves. `run_id =
   grid_identity(grid)` (DEC-SWEEP-UNKILLABLE-CORE-001) lands in a NEW
   `runs/<run_id>/`; the store tree re-populates. This is the tree's
   documented DNA ("the first post-deploy nightly re-sweeps everything",
   CLAUDE.md §1 #1612 blurb), recorded in the DEC entry, not fought.
4. **Not folded into identity:** the `ExecGridMap` itself (a pure function of
   the two hashed bar streams + tf_minutes — deriving it into the identity
   would be redundant), the flat-skip jump tables (§1.5 — behavior-identical
   accelerator, parity-pinned).

## §5 MISSING-1m / ABSENCE LAW — the RATIFIED SLIDE + the WITHHOLD alternative

> **B2 [DRIFT — DV-FILL-1M-BAR-ABSENT] folded — coordinator FINAL ruling
> 2026-07-15: the session-bounded SLIDE is RATIFIED as the shipped law,
> UNIFIED across PR-A and PR-B** (a divergent missing-bar law between the
> two is ruled a correctness defect). The register's expected WITHHOLD
> stays fully drafted as the named alternative (§5.4) so either outcome of
> the operator's non-blocking morning note is a one-edit flip — with the
> flip ASYMMETRY stated honestly there.

### §5.1 The register's expected behavior (verbatim, restored)

"WITHHELD/skipped w/ a named reason (or tuple demoted) — NEVER fall back to
signal-TF open or last close; any fabricated fill is counted+stamped."
The core PROHIBITION is law under EITHER variant: never a fallback to the
signal-TF open, never the last/nearest-PRIOR close, never a fabricated
minute — pinned by the fill-seam grep (§3.9).

### §5.2 THE SLIDE LAW (shipped; guardrails stated AS LAW)

The walker sees only the STORED exec stream and cannot attribute causes at
walk time; the law is cause-independent. A fill whose exact next 1-minute
bar is not stored fills at the **next STORED same-session minute — the
nearest NEXT print, never a chosen-best one**, under these guardrails as
LAW (§2.9): **session-bounded** (a fill never slides across a session
boundary; cross-session attempts DROP — entry drop / `session_gap` per
§2.7-2.8); **counted** — EVERY slid fill class counts (`fill_slid_minutes`
for entries, widened per the assurance REQUIRED promotion to Mode-B exit
fills and missing-forced-boundary fills — via one widened counter or named
siblings `exit_fill_slid_minutes` / `forced_fill_slid_minutes`; per-tuple
totals + capped WARN, the `_TAIL_HOLE_WARN_CAP` idiom
`rust_trade_sim.py:163-170`); **never a prior/substitute bar**; and
**worst-case pricing applies WITHIN the slid bar** — the slide is never a
price windfall. The four causes and their behavior:

| # | Cause | Lake truth | Behavior at the fill law (SLIDE) | Named signal |
|---|-------|-----------|----------------------------------|--------------|
| 1 | **Untraded minute** (vendor-legit empty; rare on the 3 engine indices — an index prints every session minute, the #1794 `is_index` presence law — but legal in general) | minute genuinely absent | the mapping's `>=` law fills at the next STORED same-session minute; SL/TP simply not evaluated during absent minutes; MAE folds stored bars only | the `fill_slid_minutes` counter family |
| 2 | **Lake hole** (missed pull / interior gap / session tail — #1031's domain) | absent until healed | same slide; cross-session → entry DROP / position `session_gap` (§2.8); when the hole heals, the re-pull moves `data_snapshot_hash` → the re-sweep recomputes honestly (§4.1) | #1031 detectors own alerting; `trade_sim.session_tail_hole` WARN sweep on the 1m columns (§3.10) |
| 3 | **Bad-tick-screened minute** (read-boundary skip-and-count, `feed/replay.py:320-330`) | stored but SCREENED at read | identical to absent — the screen precedes the walk; never price on the corrupt bar (§3.1) | `feed.bad_tick_skipped` (existing) |
| 4 | **Withheld today-bars** (the #1774 today-completeness gate kept still-settling minutes out of the lake) | absent by design until settled | the session's stored tail simply ends earlier: entries whose fill instant lands past it DROP (same-session gate has nothing to fill on) or the position closes `end_of_data`/`session_gap` per §2.7-2.8 | the gate's own `TODAY_INCOMPLETE` ledger surface |

### §5.3 The ruling's rationale (recorded verbatim in intent)

(a) Per-missing-minute withholding silently deletes trades on ordinary
#1031 lake holes — a survivorship/selection distortion, which is exactly
the optimism-illusion vector the operator's worst-case directive forbids;
(b) a real broker fills at the next available print — the slide IS the
executable reading of "the NEXT 1-minute bar"; (c) worst-case pricing on
the slid bar keeps the never-optimistic guarantee; (d) **UNIFICATION:
PR-A's existing walker behavior — a pending entry fills on the next PUSHED
same-session bar (`trade_sim.py:253-258`) — IS the slide law at
decision-TF granularity.** PR-A therefore needs NO code change; PR-B
formalizes the SAME law on the 1m grid, and the tf==1minute degenerate
case (§6) stays a fixed point of the fill laws.

### §5.4 THE WITHHOLD — the named, fully-drafted ALTERNATIVE (flippable)

**The variant (drafted so a flip is one edit):** an arming fill (entry, or
Mode-B signal exit) whose EXACT next 1-minute bar (`ts_1m ==
close_instant`) is not stored is WITHHELD — the entry is skipped (no
trade), a Mode-B exit is skipped (the position continues under
SL/TP/forced/terminal laws) — with a named counted reason
(`entries_withheld_missing_1m` / `exits_withheld_missing_1m`). Out of the
withhold's scope in ANY variant (positions must close): the forced-boundary
fill prices at the first STORED 1m bar at/after the forced instant
(displacement counted), and the §2.7/2.8 terminal laws price at stored
prior bars.

**Trade-off table (the operator's flip decision, fully informed):**

| Variant | On hole-bearing lakes | Coherence | Fill realism |
|---------|----------------------|-----------|--------------|
| **SLIDE (ratified, shipped)** | trades preserved; the fill lands on the next stored minute, counted per class | PR-A-coherent (the unification, §5.3 — tf=1m fixed point; PR-A unchanged) | liberalizes fills onto later bars — a REAL stored price at a REAL later instant, worst-case-priced, never a windfall |
| **WITHHOLD (alternative)** | fewer trades — entries at holes DROP (counted); the result set is survivorship-shaped exactly where data is worst | DIVERGES from PR-A's shipped walker unless PR-A is ALSO changed (see asymmetry below) | never a price the market did not immediately offer |

**The flip is NOT symmetric (stated per the coordinator's honesty
correction):** because PR-A's SHIPPED walker already slides (next pushed
same-session bar), choosing WITHHOLD later would ALSO require changing
PR-A's walker to keep the two laws consistent — else they diverge at every
data hole. WITHHOLD is therefore the MORE INVASIVE option: an ENGINE change
+ golden re-locks on the PR-A surface, not a doc-only flip.

**Documented cost of the withhold variant (the two-delta analysis —
recorded here because it applies ONLY under this alternative):** under
WITHHOLD, tf==1minute would carry TWO deltas from PR-A, not one: (a) the
same-bar deferral (§2.4 — present under either variant), and (b) the
withhold-at-holes delta — PR-A's walker fills a pending entry on the next
PUSHED same-session bar (minute holes effectively slide today), so WITHHOLD
drops entries that PR-A fills; the trade SET itself shrinks on hole-bearing
spans. Under the ratified SLIDE, delta (b) does not exist and §6's
one-delta statement holds.

## §5b VOLUME-0 FILL BARS (ENGINE) + THE GAP-OPEN LAW — **B5 [GAP] folded**

- **Volume-0 on the ENGINE is a NON-EVENT — stated explicitly so it can
  never be conflated with the executor ruling.** The engine surface is the
  3 spot indices (DEC-ENGINE-SPOT-ONLY-001) — spot-index bars are
  STRUCTURALLY VOLUME-LESS (an index has no traded volume; the VWAP bits
  52/53 permanently abstain for the same reason). A printed 1m bar is
  therefore REAL + TRADABLE regardless of its volume field — the #1543
  printed-bar precedent (DEC-FUTURES-EXEC-TRANSLATION-001's R15 "a printed
  bar is REAL + TRADABLE") applied on the ENGINE side, where it was never
  superseded. **Volume-0 NEVER rejects, withholds, or slides a fill on the
  ENGINE** — no volume predicate exists anywhere on the engine fill path.
- **Deliberately DISTINCT from the options/futures EXECUTOR ruling:** the
  executors price TRADED CONTRACTS, where a volume-0 printed bar is a
  stale-quote hazard; there DEC-FILL-WORST-CASE-ONLY-001 ruled volume-0 =
  uniform ALWAYS-REJECT on BOTH executors (the futures R15 fill-by-default
  clause consciously superseded ON THAT SURFACE, rejections counted). Two
  laws, two surfaces: volume-less INDEX bars (engine, fills) vs
  traded-contract bars (executors, translation). This section exists so
  the two rulings can never be cross-applied in either direction.
- **THE GAP-OPEN LAW (explicit, B5):** a next-1m bar that gaps PAST an
  armed SL/TP level fills at the **ADVERSE-MAX vs the level WITHIN that
  bar** — BUY exit `min(level, low_1m)`, SELL exit `max(level, high_1m)`
  (§2.2's formula = PR-A's `_resolve_level_hit` fill law on the 1m grid) —
  NEVER the untouched open, and never the level itself when the bar's
  extreme is worse. A bar gapping entirely through the level fills at the
  bar's own adverse extreme.
- **Verify:** (a) fixture — a volume-0 1m fill bar FILLS (entry at its
  adverse extreme; both engines); (b) killed-token/grep — no volume read
  exists on the engine fill path (walker, kernel columns, reference sim);
  (c) golden — a 1m bar gapping beyond the SL fills at the gapped adverse
  extreme, not the level and not the open (both engines; a fill-at-open
  mutant and a fill-at-level mutant both fail).

## §6 THE tf == 1minute DEGENERATE CASE

Decision grid == execution grid (the preload key collapses to ONE slice,
§1.2 D1; callers pass the same sequence twice).

- **Mapping degenerates cleanly:** `exec_idx(N) = first ts_1m >= ts_N + 1min`
  = bar N+1 on a contiguous stream, and the next STORED bar across a hole —
  EXACTLY PR-A's "fill at the very next bar, same-session-gated" law
  (`trade_sim.py:253-258`, `:553-558`). Entry pricing (adverse extreme of
  that bar), session gate, forced gate, EOD, session_gap: all bit-identical
  by construction.
- **THE ONE DELTA — stated honestly:** the same-bar deferral (§2.4). PR-A
  manages the just-opened position ON its entry bar (the same-push (B) call,
  `trade_sim.py:560`, `trade_sim.rs:513` — SL/TP can fire on the entry bar);
  PR-B defers exit detection to the entry bar's successor. So tf=1minute is
  **the fixed point of every law EXCEPT the entry-bar exit-detection
  deferral** — a deliberate, ledgered law change (§3.12), not an accident of
  the plumbing. The one-delta statement HOLDS because the missing-bar law is
  the ratified SLIDE, unified with PR-A's shipped next-pushed-bar behavior
  (§5.3); under the §5.4 WITHHOLD alternative a SECOND delta would appear —
  that analysis lives in §5.4 as the alternative's documented cost.
  Consequences at tf=1m: a trade whose PR-A life was
  entry-and-stop-on-one-bar now exits on the next bar — or, with no
  successor, terminally AT the entry bar priced at its **CLOSE** (the B3
  terminal basis, §2.7-2.8, never the adverse extreme); MFE/MAE unchanged
  in mechanism (entry bar folds either way).
- **Verify:** a dual-arm golden — the PR-A 1minute fixture family re-run
  under PR-B with same-bar-exit-free fixtures must be BIT-IDENTICAL
  (the fixed-point pin); a fixture engineered to exit on its entry bar under
  PR-A must move by exactly the deferral (the delta pin, hand-derived,
  terminal arm priced at the close basis); the §3.4 mapping property at
  tf=1.

### §6b tf == 1day — STRUCTURALLY EMPTY under the law (stated plainly)

A derived 1day decision bar is labelled midnight IST; its close instant is
the NEXT midnight; the first stored 1m bar at/after that instant is the
NEXT SESSION's 09:15 — a different IST session date than the arming bar's —
so the same-session entry gate (§3.5) drops EVERY entry. **tf=1day
produces ZERO trades under the 1m execution law.** This matches today's
cross-session drop law (a 1day entry already fills on the next 1day bar =
a different session under PR-A/main, so 1day sweeps yield zero trades
today too — NOT a regression), but it must be said plainly because it
(a) reshapes the §1.5 compute headline — the nominal "~375× at 1day"
multiplier is MOOT (every arm point drops; flat-skip jumps the whole
stream), and (b) sets the 1day golden expectation: the re-locked 1day
fixtures pin ZERO trades, not re-priced trades. A TRADABLE 1day law (e.g.
session-close-labelled daily bars) would be a separate operator decision,
NOT this PR.

---

## §7 BLAST RADIUS

### §7.1 Goldens — re-locked AGAIN (the second re-lock in the track)

Every fill-sensitive golden PR-A just re-locked moves again (the §3b.3
register gate): `_GOLDEN_RUN_ID`/`_GOLDEN_CONTENT_HASH` +
`_PRECISE_ERA_RUN_ID` retained as a refused-era pin
(test_engine_sweep_run_golden.py — PR-A values at `pra_checkpoint.md:
255-262`), futures-sweep golden, test_engine_trade_sim_golden /
test_trade_sim_rust_golden absolute pins, combo_aggregates / trade_capture /
top100 digests, paisa-accounting + sensex-forced-exit goldens, runcore
golden-intact, and the options/futures EXECUTOR goldens (their input
`entry_ts`/`exit_ts` become 1m-granular → different option/futures bars are
read → different fills/costs). Byte-UNTOUCHED, pinned by empty-diff proofs:
`vocab_golden_vectors.json` (indicator layer) and every stage1a `bar_bits`
golden (signals are decision-TF, Stage-1A unchanged). Every re-locked value
hand-derived in-file per the PR-A discipline; the PR-A-era run_id kept as a
must-FAIL pin (§3b.3). The 1day fixture family re-locks to ZERO-trade
expectations (§6b — structurally empty, not re-priced). The re-lock CENSUS
names the exit-reason/`exits_*` counter redistribution the deferral causes
plus the new counters (§3b.3's census obligation).

### §7.2 Equity / exit-index / metrics tail

`build_equity_curve` + `_pnl_by_bar_index` re-anchor via `decision_of`
(§3.8); `exit_index.py` gains the dual-stream consistency form; the
per-mask equity fold stays O(N_tf). `holding_bars` becomes a 1M-BAR count
(`trade_sim.rs:571-587` semantics re-based) — a SEMANTIC change to
`ClosedTrade.holding_bars` consumers (holding stats, time-in-market):
decided as the honest reading (the walk grid IS the hold measure),
disclosed. **§8 Q4 SETTLED: 1m-count ONLY — no `holding_decision_bars`
companion field (derivable via `decision_of` where a display wants it);
HARD OBLIGATION in the same change: `time_in_market_pct`'s denominator
(`compute.py:818-821` `total_bars`) moves to the EXEC-stream bar count — a
1m-count numerator over a decision-bar denominator would exceed 1 and the
clamp would silently corrupt the ratio.**

### §7.3 Burn-in / diffcheck / capture surfaces

`rust_metrics` burn-in untouched + argued fill-law-independent (§3.13);
diffcheck reference rewritten in lockstep + independence extended (§3.11);
`TradeDetail` fills become 1m prices (`trade_sim.py:174-212` — shape
unchanged, values move); `trade_capture`/results-depth re-lock; minute_path
(`minute_path.py` docstring lines 1-50) stays annotation-only, and its
`exit_bar_full_range` convention is re-derived against the 1m walker — the
capture-pass 1m excursions should now match walker MFE/MAE at EVERY tf
(an upgraded property; today pinned only at tf==1minute). **The VIX
reference stamper's lookups MOVE with the timestamps (assurance note
folded):** `vix_at_entry`/`vix_at_exit` are looked up at the now
1m-granular entry/exit instants — served by the NSE-INDIAVIX 1minute lake
slice; a missing VIX minute stamps the honest `None` exactly as today
(never fabricated, §2 Rule 3) — so the stamped VIX values on coarse-TF
trades move at re-lock (a VALUE change riding the timestamp change, not a
stamper-mechanism change).

### §7.4 Options/futures executors — 1m-granular timestamps FOR FREE

The executors inherit spot trade timestamps VERBATIM — the seam:
`futures/executor.py:264-266` ("inherited VERBATIM from the pure spot
trade") / `options/executor.py:951-958` (`signals_from_spot_trades`) — and
both lake sources already match the EXACT UTC minute on 1-minute partitions
(`options/lake_source.py:80`, `:137-138`; `futures/lake_source.py:68`,
`:109-118`; missing minute → NO_DATA, never nearest-bar). So once the spot
engine emits 1m-granular `entry_ts`/`exit_ts`, the translation legs price on
the RIGHT minute with ZERO executor change — the whole point of the seam.
Their goldens re-lock (§7.1); their reject taxonomy (NO_DATA on a missing
option/futures minute) is unchanged.

### §7.5 Memory / perf / O-envelope (honest statement)

Memory: §1.4 (≈6.0×10⁵ 1m bars/instrument ≈ 30 MB kernel-resident +
an UNVERIFIED-estimate 250-420 MB as Python `CandleBar`s; measure at build).
Compute: the trade-sim leg is O(N_masks · N_1m) naive — up to tf_minutes×
today's leg — mitigated by the flat-skip design to
O(#signals + Σ in-position minutes + #trades) per mask (§1.5), with a
jump-vs-full-walk parity property and NEW bench gates **WITH TEETH
(assurance minor folded — thresholds are REFUSAL gates, not claims)**:
(a) per-mask cost flat vs N_1m for a sparse mask (ratio tier per
`tests/bench/_o1_tiers.py`); (b) the A/B tuple-throughput bench vs the
PR-A tree gains an ACCEPTANCE THRESHOLD — a sparse-mask rehearsal grid at
tf=5m must land within **≤ 2.0× PR-A wall-clock per tuple**
(justification: flat-skip bounds per-mask work by #signals + in-position
minutes — both tf-invariant for a fixed strategy — and the
once-per-coordinate column/map build amortizes across the grid's masks and
tuples, so a >2× degradation means flat-skip is broken or a hidden
O(N_1m)-per-mask term crept in; the measured number is STILL recorded in
the PR body, and the ceiling refuses, it never substitutes for the
measurement); (c) a dense-mask WORST-CASE bench — an always-in-position
mask honestly walks ≈N_1m; the gate pins the cost LINEAR in N_1m (no
superlinear surprise), the dense cost stated, never hidden. Construction:
O(N_1m + N_tf) ONCE PER COORDINATE — the 1m columns + `ExecGridMap` live
in the preload-scoped memo OWNED by `cmd_sweep_all` beside `_bars_for`
(§1.5, the named caching owner), never rebuilt per tuple. Per-bar work
stays O(1). NO O(1)-total claims anywhere; NO SLA claim before
measurement (Golden Rule #10).

### §7.6 Not touched

Stage-1A / bar_bits / the 60-vocab (signals stay decision-TF);
`indicator_version` (no indicator math moves — §3b.3); the lake/pull side;
Bloom/LMDB dedup mechanics (keys re-derive through the same
`combo_digest` inputs); `rust_metrics.py`; the movers/dashboard read side.

---

## §8 HONEST LIMITS + ASSURANCE ANSWERS (Q1–Q6 SETTLED at review round 1)

**Limits (stated, not hidden):**
- **L1** The 1m bar is still a bar: worst-case-within-the-minute is the
  floor of what a tick path could do inside that minute; we claim
  never-optimistic AT 1-MINUTE GRANULARITY, not tick-truth.
- **L2** The same-bar deferral leaves ≤1 minute of undetected SL/TP exposure
  per trade (§3.12) — visible in MAE and the eventual exit, by design.
- **L3** The RATIFIED slide law (§5) fills at the next stored minute, which
  on a multi-minute hole is a stale-adjacent fill — a REAL stored price at a
  REAL later instant, counted per fill class (the widened
  `fill_slid_minutes` family — a LAW obligation, §2.9), session-bounded,
  worst-case-priced within the slid bar, honest.
- **L4** Memory + throughput numbers marked UNVERIFIED are estimates;
  build-time measurement is a gate, and the flat-skip lever's win is
  mask-sparsity-dependent (dense long-hold masks approach O(N_1m) — the
  §7.5 dense-mask bench states the cost).
- **L5** (v2 update) The register files are RESTORED and authoritative;
  every driving section quotes them verbatim, and the two reconstruction
  DRIFTS the review found (B1, B2) are folded under their finding ids. The
  v1 reconstruction caveat is retired.
- **L6** NIFTY bar-count arithmetic uses an approximate holidays/yr (14-16);
  the exact count derives from `calendars/nse.py` at build time.
- **L7** tf == 1day is STRUCTURALLY EMPTY under the law (§6b) — zero
  trades, matching today's cross-session drop behavior; the 1day goldens
  pin zero trades and the 375× compute headline is moot.

**Assurance round-1 ANSWERS — Q1–Q6 SETTLED (each folded into its section):**
- **Q1 SETTLED:** the finalist re-simulation's price anchors
  (`ranked_combo_to_result_row`, `sweep_run.py:901-919`) read
  `bars_by_ts_first` over the **EXEC (1m) stream** — the only total domain
  including session_gap/slid fills; the open/close ANCHOR semantics are
  KEPT on the 1m fill bars (fill-extreme anchors would be a separate
  ResultRow-semantics DEC). Pins: fails-before KeyError on the decision
  map; re-locked goldens show only anchor/ts/pnl movement.
- **Q2 SETTLED:** the cascade manifest carries the law BY DERIVATION, not
  payload (§4.2): ONE schema addition (`execution_grid` in the spec dict;
  refuse-on-absence is the sole refusal law); exec coordinates derived as
  instruments × `"1minute"` in BOTH `load_bars_for_manifest` AND
  `manifest_snapshot_hash` (the G7 check auto-refuses a healed lake); bar
  bytes never enter the manifest.
- **Q3 SETTLED (MANDATORY sharpening adopted, §3.7):** the Stage-3 lockbox
  uses ONE BOUNDARY — `lockbox_start` computed once per instrument pair,
  applied to BOTH streams; divergent per-stream boundaries resolve to the
  EARLIER + a tail-skew WARN. Pins: no train trade prices an exec bar
  at/after the boundary (the WALK-side leak is the sharper hazard, not the
  hash) AND the train fold includes the CUT exec slices.
- **Q4 SETTLED (§7.2):** `holding_bars` = 1m-count ONLY — no
  `holding_decision_bars` field (derivable via `decision_of`); HARD
  OBLIGATION: `time_in_market_pct`'s denominator (`compute.py:818-821`
  `total_bars`) moves to the exec-stream count in the SAME change.
- **Q5 SETTLED (§2.6):** Mode-B needs no operator ruling — the symmetric
  next-1m fill law IS implemented (no dormant second fill law), with a
  negative/AST pin that no production caller passes exit signals
  (production-unreachable today; all five `simulate_trades` sites
  enumerated).
- **Q6 SETTLED:** `DEC-FILL-EXEC-GRID-1M-001` @ reserved **#1826** is the
  stable handle (renumber-at-landing absorbs collisions).

## §9 VERIFY MATRIX (the pinned-test inventory, by register id)

| Id | Pin (summary) | Kind |
|----|---------------|------|
| DV-FILL-BAR-VENDOR-AUTHORITY | provenance LAW (mismatch withholds; no-Dhan-path AST pin per ADR-0021) + dual-vendor both-orders bit-identity golden (IMPL OBLIGATION, Dhan-fixture-dependent) + provenance==signal-vendor property; screened-bar routing + corrupt-price loud tuple fail retained | AST + golden + property + unit |
| DV-FILL-TZ-SHIFT-WRONG-BAR | tz-render invariance + shifted-grid selector refusal (`fill_selector_distance_refused` — the B6 bounded-minute-distance assertion) + session-open golden | property + unit + golden |
| FILL-DUALSTREAM-ABSENT | no-1m-lake launch REFUSED; 10:05-fill golden; no optional-exec-feed signature | integration + golden + killed-token |
| FILL-SIGNAL-TS-OPEN-VS-CLOSE-LOOKAHEAD | fill-ts ≥ close-instant property; ≤close-1m-mutation invariance; label-anchor mutant fails | property + mutant |
| FILL-SESSION-END-NO-NEXT-1M | last-bar zero-trades both engines; Muhurat boundary goldens; i+1-cross-session property | golden + property |
| FILL-WORSTCASE-SIDE-SIGN | exact-equality 1m side pins + dominance property + H/L swap mutant (+ the freeze-time one-sign-source consolidation) | property + mutant |
| FILL-REPRO-1M-NOT-IN-SNAPSHOT-HASH | 1-paisa 1m flip re-keys snapshot+run_id+grid_identity; tuple-cache miss after a 1m edit | golden + integration |
| FILL-EXIT-TS-NOT-COARSE-BAR-EQUITY-CRASH | fails-before ValueError → owning-bar fold; exit_indices_consistent over 1m; fastpath≡fallback | fails-before + property |
| DV-FILL-1M-BAR-ABSENT | four-cause fixtures under the RATIFIED slide (B2) + widened fill_slid_minutes-family pins + no-silent-fallback grep | unit + grep |
| FILL-SESSION-GAP-GUARD-1M-STREAM | 1m-tail-hole session_gap exact prices (incl. the B3 terminal close-basis arm); truncation equivalence | golden + property |
| FILL-RUST-KERNEL-DIFFCHECK-PARITY | AST ban + arm_source off-by-one ⇒ SUSPECT; dual-stream parity; the B4 boundary-spanning-hole collision fixture (winner law, ×3 implementations) | AST + mutant + property + fixture |
| FILL-SAME-BAR-TIME-ORDER-PHANTOM-LOSS | no-same-bar-exit golden + exits_deferred count; exit_idx>entry_idx property (terminal close-basis carve-out); B3 dominance pin; deferral + close-basis mutants | golden + property + mutant |
| FILL-KERNEL-BURNIN-GLOBAL-AUTOREVERT | 1m-exit-instants sweep → no autorevert latch (teeth kept); stale-wheel demotion `exec_grid_kernel_stale` | unit + negative |
| FILL-SL-TP-DETECTION-VS-FILL-LAG | interior-minute pierce golden; detection-mode-swap mutant | golden + mutant |
| FILL-FORCED-EXIT-1M-UNDERSPECIFIED | 15:20 1m LOW golden; half-day/Muhurat; no-literal grep; missing-boundary slid-fill count | golden + grep + unit |
| FILL-LAW-REKEYS-EVERY-GOLDEN | ledgered re-lock; PR-A run_id must-FAIL; vocab-fixture empty diff; the counter-redistribution census (§3b.3) | golden + diff-proof + census |
| FILL-LOOKAHEAD-PROPERTY-SUFFIX-INDEP-1M | exec-stream suffix independence incl. flat-skip arm; look-ahead fill FAILS the extended gate | property |

*(Plus the §5b volume-0-fill + gap-open pins, the §6 degenerate
fixed-point/delta pair, the §6b 1day zero-trade pin, the §1.5 jump-parity
property, the §7.2 time_in_market denominator obligation, and the §7.5
bench gates with their acceptance teeth.)*
