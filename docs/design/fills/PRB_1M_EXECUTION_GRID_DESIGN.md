> STATUS: DESIGN-REVIEW VEHICLE (PR-B, worst-case fills track). Not normative until the PR-B implementation lands with its own decision entry (a reserved slot in the fills ledger band). File:line citations in this document reference the PR-A branch `claude/fill-worst-case-only` (not yet pushed at time of writing), NOT this branch's tree — they are design references, to be re-verified against the implementation PR.

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
- **Status of the register source files (DISCLOSED):**
  `extreme-scenario-register-p0-fill.md`, `-2.md`, `-3.md` and `-p1-c.md` are
  **EMPTY (0 bytes, mtime 2026-07-15 13:42)** at design time — apparently
  wiped. The 13 P0 ids + 4 P1 ids are taken from the coordinator brief; the
  scenario intents below are reconstructed from the ids + the PR-A assurance
  ledger (`fill-worst-case-only-track.md:91-115`) + the surviving
  `extreme-scenario-register-p1-b.md`. **Verbatim register verify lines are
  quoted ONLY where the surviving p1-b file carries them; everywhere else the
  original verify line is UNVERIFIED-UNAVAILABLE and the § says so.**
  Assurance owns confirming each reconstruction matches its original intent.

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
is the next STORED minute — §5). `arm_source` is that relation inverted, with
the invariant that at most one decision bar closes per 1m boundary (tf ≥ 1),
so `arm_source[j]` is a scalar. All arithmetic is on the UTC-aware
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
decision bar maps to at most one fill bar). `Walker::push`
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
multiplier** on that leg (5× at 5m; ~375× at 1day). That is NOT acceptable to
hand-wave (§2 Rule 5 / the 2-4 day SLA, CLAUDE.md §8). The designed
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
a jump-vs-full-walk parity property pins behavior identity. Bench gates: §7.5.
NO wall-clock SLA is claimed pre-measurement (Golden Rule #10).

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
   No detect-coarse/fill-later lag exists anywhere (§3b.1).
3. **Tie law** — one 1m bar spanning BOTH levels fills the STOP first, always
   (`trade_sim.py:757-781`, `trade_sim.rs:354-357` — unchanged).
4. **Same-bar deferral (NEW, settled):** the entry 1m bar folds its range
   into MFE/MAE but NEVER exits — SL/TP detection for a position begins on
   the 1m bar AFTER its entry bar. (Supersedes, on the 1m grid, the PR-A
   same-push manage-the-entry-bar behavior — `trade_sim.py:560` manages the
   just-opened position on the entry bar. §3.12 carries the rationale + the
   rejected alternative.)
5. **Forced session-close exit** — the first 1m bar whose minute reaches the
   SESSION's forced-exit instant (`forced_exit_seconds_for_session`,
   session-resolved per IST date — `trade_sim.py:455-486`) closes the
   position at that 1m bar's adverse extreme; boundary = session close − 10
   min (15:20 regular; early-close/Muhurat variants) — never a 15:20/375
   literal (§3b.2). Entry never fills at/after the boundary (gate in law 1).
6. **Mode-B signal exit** — exit signal on decision bar N → fill on 1m bar
   `exec_idx(N)` at its adverse extreme (symmetry with entry; replaces the
   "next decision bar's open-anchored fill", `trade_sim.py:690-701`).
7. **End of data** — a still-open position closes at the LAST STORED 1m
   bar's adverse extreme (`_last_bar_exit_paisa` re-anchored,
   `trade_sim.py:794-804`).
8. **Session gap** — an open position never survives an IST session-date
   change ON THE 1M STREAM: first 1m bar of a new session closes it
   retroactively at the PRIOR session's last stored 1M bar's adverse extreme
   (the finish() idiom — `trade_sim.py:562-585`, `trade_sim.rs:516-529` —
   re-keyed on 1m `session_idx`; §3.10). Pending exits subsumed; the boundary
   bar never arms; earliest re-arm = its successor.
9. **Missing minutes** — never a substitute/nearest-prior bar; the mapping's
   `>=` law slides a fill to the next STORED same-session minute; absence
   taxonomy + observability in §5.

---

## §3 P0 REGISTER SECTIONS (one per id — 13 folds)

> Register-source caveat (§ header disclosure): the P0 fill register files are
> EMPTY at design time; unless a verbatim line is quoted from a surviving
> file, "Register verify" is reconstructed and marked UNVERIFIED-UNAVAILABLE.

### §3.1 [DV-FILL-BAR-VENDOR-AUTHORITY]

- **Reconstructed scenario:** the 1m execution bar becomes the PRICE AUTHORITY
  for every fill — vendor (Groww) 1m data quality now prices P&L directly. A
  corrupt/scaled/clamped vendor minute must never price a fill; which stream
  is authoritative and which guards cover it must be pinned.
- **Design answer:** the execution slice loads through the SAME one mouth as
  every sweep slice — `load_sweep_slice` → `Catalog.get_bars` →
  `arrow_table_to_bars` — so it inherits the engine read boundary verbatim:
  positivity refusal BEFORE construction (`feed/replay.py:336-347`,
  DEC-STORED-DATA-VALIDATION-001) and the bounded bad-tick screen
  (`feed/replay.py:320-330`, DEC-SWEEP-DATA-EDGE-GUARDS-001 — skip-and-count,
  never value-edit). The STORED lake 1m stream (2dp-snapped at the write
  boundary, DEC-SPOT-PRICE-2DP-001) is the single authority; no second
  source, no resample-on-the-fly, no vendor re-fetch at sweep time. A
  screened/absent minute routes through the §5 absence law (slide within
  session), never a corrupt price.
- **Verify (register line UNVERIFIED-UNAVAILABLE; designed pin):** unit — a
  planted ×10 fat-finger 1m bar inside the exec window is screened
  (`feed.bad_tick_skipped` observed) and the entry due at that minute fills
  on the next stored minute, never at the corrupt extreme; a planted
  non-positive 1m price fails the tuple LOUDLY at construction
  (`SweepComboError` via `_build_trade_sim`, `sweep_run.py:1089-1096`),
  never a silent fill.

### §3.2 [DV-FILL-TZ-SHIFT-WRONG-BAR]

- **Reconstructed scenario:** timestamps are stored UTC, sessions are IST
  (+05:30, no DST); a naive/mixed-tz comparison in the signal→next-1m mapping
  selects a bar 5h30 off (or a wrong-session bar).
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
- **Verify (UNVERIFIED-UNAVAILABLE; designed pin):** property — the mapping is
  invariant under re-rendering fixtures through `astimezone(IST)` round trips;
  a deliberate label-grid shift of the 1m stream by +05:30 makes the
  session-gate drop/refuse trades rather than fill a wrong bar; golden across
  a session-open boundary (signal on the 09:15 5m bar → fill at the 09:20 1m
  bar, IST, asserted via UTC values).

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
- **Verify (UNVERIFIED-UNAVAILABLE; designed pin):** fails-before-style
  integration — `sweep-all` at tf=5m over a lake holding 5minute partitions
  but NO 1minute → EXIT_USER_ERROR naming `(symbol, 1minute)`; kwargs/killed-
  token pins that no `exec_bars_1m=None` default exists on any public
  simulate entry.

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
- **Verify (UNVERIFIED-UNAVAILABLE; designed pin):** property — for every
  emitted trade, `entry_fill_bar.timestamp >= signal_bar.timestamp +
  tf_minutes` (strict for the label: entry bar's OPEN ≥ the close instant); a
  label-anchored mutant (`+ tf_minutes` dropped) fails it and fails the
  degenerate-case fixed-point suite (§6); plus the §3b.4 suffix-independence
  property on the 1m stream (mutating 1m bars after a trade's exit never
  changes it).

### §3.5 [FILL-SESSION-END-NO-NEXT-1M] (1m arm)

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
  (ADR-0007 A1, `snapshot_hash.py:44-49`) must cut the exec slice with the
  SAME lockbox exclusion law before folding — flagged as open question §8.Q3
  (design intent: one shared slicer, never two cut laws).
- **Verify (UNVERIFIED-UNAVAILABLE; designed pin):** golden — flip ONE exec
  1m bar's low by 1 paisa ⇒ `data_snapshot_hash` moves ⇒ `mask_run_id`
  (`sweep_run.py:410-468`) + `grid_identity`
  (`sweep_supervisor.py:382-401`) move; unchanged decision slice alone
  cannot alias. Plus a spy pin that the preload registered the
  `(symbol, "1minute")` key for every swept symbol.

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
- **Verify (UNVERIFIED-UNAVAILABLE; designed pin):** fails-before — a
  1m-granular exit_ts trade fed to today's `build_equity_curve` raises
  `ValueError` (the pinned message `sweep_driver.py:152`); after — it folds
  at the owning 5m bar; invariants Σ(equity deltas) == Σ(pnl) and
  fastpath≡fallback bit-parity under the certificate; a `decision_of`
  off-by-one mutant fails the parity.

### §3.9 [DV-FILL-1M-BAR-ABSENT]

- **Reconstructed scenario:** the exact next 1-minute bar after a signal's
  close does not exist in the stored stream. Four distinct causes with
  different truths; the law must never fabricate or substitute.
- **Design answer:** §5 in full (the four-cause taxonomy + the slide law +
  observability). Summary: the mapping's `>=` law fills at the next STORED
  same-session minute; cross-session → drop/`session_gap`; never a
  nearest-PRIOR bar, never a synthesized minute.
- **Verify (UNVERIFIED-UNAVAILABLE; designed pin):** §5's four fixtures (one
  per cause) + the `fill_slid_minutes` observability pin.

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
- **Verify (UNVERIFIED-UNAVAILABLE; designed pin):** two-session dual-stream
  fixture with a session-tail 1m hole → `session_gap` close priced at the
  prior session's last 1M bar low/high (exact-equality both engines); the
  #1743 truncation-equivalence property re-derived on the 1m stream
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
  slide law, SL-first tie — the independent-recomputation bar
  (`sweep_diffcheck.py:24-27`). The reference does NOT implement flat-skip
  (§1.5) — naive full walk is the point. K=200 sampling, blake3 seeding,
  never-adopt (`DiffCheckMismatch` ⇒ SUSPECT, production value always
  ships) all unchanged. The CI rehearsal keeps `CI_K_SAMPLES=8`
  (`sweep_diffcheck.py:116`).
- **Verify (UNVERIFIED-UNAVAILABLE; designed pin):** the AST independence pin
  extended (+`brutex.engine.exec_grid` in the banned-imports list); an
  off-by-one mutant in the KERNEL's `arm_source` consumption ⇒
  `DiffCheckMismatch` fires on the rehearsal fixture (fails-before teeth);
  R45 clean on the re-locked goldens; the dual-stream hypothesis parity
  suite (walker≡kernel≡reference on sampled masks).

### §3.12 [FILL-SAME-BAR-TIME-ORDER-PHANTOM-LOSS]

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
- **Verify:** golden — an entry 1m bar whose own range pierces the SL level
  produces NO same-bar exit (both engines; fails-before against a same-bar
  variant); property — for every trade,
  `exit_bar_idx_1m > entry_bar_idx_1m` unless the exit class is
  `session_gap`/`end_of_data` priced at the entry bar (last-stored-bar
  degenerate, where idx equality is legal); the deferral-drop mutant
  (re-enabling same-bar manage) fails both.

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
- **Verify (UNVERIFIED-UNAVAILABLE; designed pin):** a monkeypatched kernel
  exposing the OLD (pre-dual-feed) constructor/signature ⇒ construction
  demotes with `exec_grid_kernel_stale`, trades == oracle trades bit-for-bit;
  a burn-in pin that `rust_metrics` still passes burn-in over 1m-law trades
  (both arms fed the same trades); negative pin — no new global latch exists
  in the trade-sim seam (grep/killed-token).

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

### §3b.4 [FILL-LOOKAHEAD-PROPERTY-SUFFIX-INDEP-1M]
(register file `-p1-c.md` EMPTY — verbatim line UNVERIFIED-UNAVAILABLE;
reconstructed from the id + the standing suite)

- **Reconstructed expected:** the P-06 suffix-independence gates
  (`tests/property/test_sweep_suffix_independence.py`, the #1181 permanent
  layer) must cover the 1M execution stream too — trades over bars `0..N`
  bit-identical whether EXEC bars after the last consumed instant are
  absent, mutated, or extreme.
- **Design answer:** the property suite gains the dual-stream arm: for a
  fixed decision stream + signals, mutating any 1m bar strictly after a
  trade's exit 1m bar (and after the last consumed arm point) leaves that
  trade bit-identical; truncating the exec suffix likewise (modulo the
  legal `end_of_data` reclassification of the final open position — the
  #1743 truncation-equivalence shape). The flat-skip kernel arm (§1.5) runs
  under the SAME property (jump tables are derived from arm points, which
  are decision-side data — no future exec bar can move an earlier jump).
- **Verify:** the extended property file + the §3.4 mapping property; the
  register's own line to be reconciled by assurance when the file is
  restored.

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

## §5 MISSING-1m / ABSENCE TAXONOMY

The walker sees only the STORED exec stream and cannot attribute causes at
walk time; the LAW is therefore cause-independent — **never a substitute
(nearest-prior) bar, never a fabricated minute** — while observability names
what happened. The four causes and their behavior:

| # | Cause | Lake truth | Behavior at the fill law | Named signal |
|---|-------|-----------|--------------------------|--------------|
| 1 | **Untraded minute** (vendor-legit empty; rare on the 3 engine indices — an index prints every session minute, the #1794 `is_index` presence law — but legal in general) | minute genuinely absent | the mapping's `>=` law fills at the next STORED same-session minute (the SLIDE); SL/TP simply not evaluated during absent minutes; MAE folds stored bars only | `fill_slid_minutes` counter (below) |
| 2 | **Lake hole** (missed pull / interior gap / session tail — #1031's domain) | absent until healed | same slide; cross-session → entry DROP / position `session_gap` (§2.8); when the hole heals, the re-pull moves `data_snapshot_hash` → the re-sweep recomputes honestly (§4.1) | #1031 detectors own alerting; `trade_sim.session_tail_hole` WARN sweep on the 1m columns (§3.10) |
| 3 | **Bad-tick-screened minute** (read-boundary skip-and-count, `feed/replay.py:320-330`) | stored but SCREENED at read | identical to absent — the screen precedes the walk; never price on the corrupt bar (§3.1) | `feed.bad_tick_skipped` (existing) |
| 4 | **Withheld today-bars** (the #1774 today-completeness gate kept still-settling minutes out of the lake) | absent by design until settled | the session's stored tail simply ends earlier: entries whose fill instant lands past it DROP (same-session gate has nothing to fill on) or the position closes `end_of_data`/`session_gap` per §2.7-2.8 | the gate's own `TODAY_INCOMPLETE` ledger surface |

**Why SLIDE and not strict-adjacent NO-FILL:** (a) it IS the operator law
read plainly — "the NEXT 1-minute bar" after the close is the next minute
that TRADED; a minute with no bar is a minute you could not have filled in
either; (b) the current engine's own law is already "the next STORED bar,
session-gated" (`trade_sim.py:253-258` — a pending entry fills at the very
next bar unless it crossed a session), so the tf==1minute degenerate case
(§6) is a fixed point ONLY under the slide; a strict-adjacent NO-FILL would
silently drop trades on every one-minute hole and make the degenerate case
diverge from PR-A; (c) worst-case pricing on the slid bar keeps the
never-optimistic guarantee. **Bound:** the slide is session-bounded (the
same-session gate) — a fill can never slide across a session boundary.
**Observability:** the walker counts entries whose fill bar's timestamp
exceeds `close_instant + 1 minute` (`fill_slid_minutes` total + capped
per-tuple WARN, the `_TAIL_HOLE_WARN_CAP` idiom `rust_trade_sim.py:163-170`)
— a visible honesty signal, never a law change.

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
  the plumbing. Consequences at tf=1m: a trade whose PR-A life was
  entry-and-stop-on-one-bar now exits on the next bar (or EOD/session_gap at
  the entry bar's extreme if none follows); MFE/MAE unchanged in mechanism
  (entry bar folds either way).
- **Verify:** a dual-arm golden — the PR-A 1minute fixture family re-run
  under PR-B with same-bar-exit-free fixtures must be BIT-IDENTICAL
  (the fixed-point pin); a fixture engineered to exit on its entry bar under
  PR-A must move by exactly the deferral (the delta pin, hand-derived); the
  §3.4 mapping property at tf=1.

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
must-FAIL pin (§3b.3).

### §7.2 Equity / exit-index / metrics tail

`build_equity_curve` + `_pnl_by_bar_index` re-anchor via `decision_of`
(§3.8); `exit_index.py` gains the dual-stream consistency form; the
per-mask equity fold stays O(N_tf). `holding_bars` becomes a 1M-BAR count
(`trade_sim.rs:571-587` semantics re-based) — a SEMANTIC change to
`ClosedTrade.holding_bars` consumers (holding stats, time-in-market):
decided as the honest reading (the walk grid IS the hold measure), disclosed;
whether a `holding_decision_bars` companion is wanted is §8.Q4.

### §7.3 Burn-in / diffcheck / capture surfaces

`rust_metrics` burn-in untouched + argued fill-law-independent (§3.13);
diffcheck reference rewritten in lockstep + independence extended (§3.11);
`TradeDetail` fills become 1m prices (`trade_sim.py:174-212` — shape
unchanged, values move); `trade_capture`/results-depth re-lock; minute_path
(`minute_path.py` docstring lines 1-50) stays annotation-only, and its
`exit_bar_full_range` convention is re-derived against the 1m walker — the
capture-pass 1m excursions should now match walker MFE/MAE at EVERY tf
(an upgraded property; today pinned only at tf==1minute).

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
jump-vs-full-walk parity property and NEW bench gates: (a) per-mask cost
flat vs N_1m for a sparse mask (ratio tier per `tests/bench/_o1_tiers.py`),
(b) an A/B tuple-throughput bench vs the PR-A tree with the measured number
recorded in the PR body — NO SLA claim before that measurement (Golden Rule
#10). Construction: O(N_1m + N_tf) once per tuple (columns + map). Per-bar
work stays O(1). NO O(1)-total claims anywhere.

### §7.6 Not touched

Stage-1A / bar_bits / the 60-vocab (signals stay decision-TF);
`indicator_version` (no indicator math moves — §3b.3); the lake/pull side;
Bloom/LMDB dedup mechanics (keys re-derive through the same
`combo_digest` inputs); `rust_metrics.py`; the movers/dashboard read side.

---

## §8 HONEST LIMITS + OPEN QUESTIONS FOR ASSURANCE

**Limits (stated, not hidden):**
- **L1** The 1m bar is still a bar: worst-case-within-the-minute is the
  floor of what a tick path could do inside that minute; we claim
  never-optimistic AT 1-MINUTE GRANULARITY, not tick-truth.
- **L2** The same-bar deferral leaves ≤1 minute of undetected SL/TP exposure
  per trade (§3.12) — visible in MAE and the eventual exit, by design.
- **L3** The slide law (§5) fills at the next stored minute, which on a
  multi-minute hole is a stale-adjacent fill — a REAL stored price at a REAL
  later instant, counted (`fill_slid_minutes`), session-bounded, honest.
- **L4** Memory + throughput numbers marked UNVERIFIED are estimates;
  build-time measurement is a gate, and the flat-skip lever's win is
  mask-sparsity-dependent (dense long-hold masks approach O(N_1m)).
- **L5** The register wipe (header disclosure): 9 of 13 P0 sections answer a
  RECONSTRUCTED scenario. Assurance must diff each against the original
  intent when/if the files are restored.
- **L6** NIFTY bar-count arithmetic uses an approximate holidays/yr (14-16);
  the exact count derives from `calendars/nse.py` at build time.

**Open questions (assurance input wanted BEFORE freeze):**
- **Q1** `run_persisted_sweep`'s finalist re-simulation + price anchors
  (`ranked_combo_to_result_row`, `sweep_run.py:901-919`): with 1m-granular
  `entry_ts`/`exit_ts`, the `avg_entry/exit_price` anchors should read the
  1M bars (`bars_by_ts_first` over which stream?). Design intent: the exec
  stream (the fill truth); confirm the ResultRow anchor semantics.
- **Q2** Stage-3 / cascade re-simulation paths (`stage3_wire`,
  `cascade_wire.load_bars_for_manifest`, `robustness`, `candidate`,
  `validation`, the CLI options-sim replay): each re-sims through
  `simulate_trades` and must thread the exec slice; the cascade MANIFEST
  must carry/load the 1m slices for replay (a manifest-schema addition +
  refusal law for manifests lacking them). Confirm the manifest posture.
- **Q3** The Stage-3 lockbox: the exec slice must be cut by the SAME
  lockbox-exclusion law as the decision slice before its snapshot fold
  (§3.7) — one shared slicer. Confirm no second cut law is acceptable.
- **Q4** `holding_bars` (§7.2): 1m-count only, or also carry a
  `holding_decision_bars` companion for display continuity?
- **Q5** Mode-B exit signals: designed as symmetric next-1m fills (§2.6).
  The sweep path never feeds exit signals (`trade_sim.rs:697` — kernel
  passes none); confirm the Mode-B law needs no operator ruling of its own.
- **Q6** The ledger slot: #1826 assumed from the track band; coordinator to
  confirm against the live reservation map at PR time (renumber-at-landing
  law stands).

## §9 VERIFY MATRIX (the pinned-test inventory, by register id)

| Id | Pin (summary) | Kind |
|----|---------------|------|
| DV-FILL-BAR-VENDOR-AUTHORITY | screened-bar slide + corrupt-price loud tuple fail | unit + integration |
| DV-FILL-TZ-SHIFT-WRONG-BAR | tz-render invariance + shifted-grid refusal + session-open golden | property + golden |
| FILL-DUALSTREAM-ABSENT | no-1m-lake launch REFUSED; no optional-exec-feed signature | integration + killed-token |
| FILL-SIGNAL-TS-OPEN-VS-CLOSE-LOOKAHEAD | fill-ts ≥ close-instant property; label-anchor mutant fails | property + mutant |
| FILL-SESSION-END-NO-NEXT-1M | last-bar zero-trades both engines; Muhurat boundary goldens; i+1-cross-session property | golden + property |
| FILL-WORSTCASE-SIDE-SIGN | exact-equality 1m side pins + dominance property + H/L swap mutant | property + mutant |
| FILL-REPRO-1M-NOT-IN-SNAPSHOT-HASH | 1-paisa 1m flip re-keys snapshot+run_id+grid_identity | golden |
| FILL-EXIT-TS-NOT-COARSE-BAR-EQUITY-CRASH | fails-before ValueError → owning-bar fold; fastpath≡fallback | fails-before + property |
| DV-FILL-1M-BAR-ABSENT | four-cause fixtures + fill_slid_minutes pin | unit |
| FILL-SESSION-GAP-GUARD-1M-STREAM | 1m-tail-hole session_gap exact prices; truncation equivalence | golden + property |
| FILL-RUST-KERNEL-DIFFCHECK-PARITY | AST ban + arm_source off-by-one ⇒ SUSPECT; dual-stream parity | AST + mutant + property |
| FILL-SAME-BAR-TIME-ORDER-PHANTOM-LOSS | no-same-bar-exit golden; exit_idx>entry_idx property; deferral mutant | golden + property + mutant |
| FILL-KERNEL-BURNIN-GLOBAL-AUTOREVERT | stale-wheel demotion `exec_grid_kernel_stale`; burn-in unaffected pin | unit + negative |
| FILL-SL-TP-DETECTION-VS-FILL-LAG | interior-minute pierce golden; detection-mode-swap mutant | golden + mutant |
| FILL-FORCED-EXIT-1M-UNDERSPECIFIED | 15:20 1m LOW golden; half-day/Muhurat; no-literal grep | golden + grep |
| FILL-LAW-REKEYS-EVERY-GOLDEN | ledgered re-lock; PR-A run_id must-FAIL; vocab-fixture empty diff | golden + diff-proof |
| FILL-LOOKAHEAD-PROPERTY-SUFFIX-INDEP-1M | exec-stream suffix independence incl. flat-skip arm | property |

*(Plus the §6 degenerate fixed-point/delta pair, the §1.5 jump-parity
property, and the §7.5 bench gates.)*
