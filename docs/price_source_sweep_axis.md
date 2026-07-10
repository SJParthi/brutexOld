# VWAP/EMA price-source sweep axis (DEC-NIFTY-PRICE-SOURCE-AXIS-001)

A cell-coordinate sweep axis for the per-bar **price scalar** fed into the
Stage-1A VWAP + EMA accumulators, mirroring the `fib_anchor` precedent. It is a
**swept strategy parameter**, never a vocabulary bit and never an
`indicator_version` bump — the shared-Rust accumulator math (`VwapState::update`,
`EmaState::update`) is byte-unchanged; only the pre-FFI scalar selection moves.

## What it is

- `PriceSource ∈ {OPEN, HIGH, LOW, CLOSE, HLC3}` (`brutex/strategies/types.py`).
- `PriceSourceCell(vwap_source=HLC3, ema_source=CLOSE)` — a `(vwap, ema)` pair.
  The default cell reproduces today's hardcoded reduction **bit-for-bit**.
- Grid axis `SweepGridConfig.price_source_cells: Sequence[PriceSourceCell]`,
  default `(PriceSourceCell(),)` — **cardinality 1, dormant, byte-identical to
  the pre-axis sweep**. Plugged into the existing `itertools.product` exactly
  like `fib_anchors`.
- `vwap_source` moves only bits 52-53 (VWAP position); `ema_source` moves only
  bits 0-5 (EMA-vs-close). The enumeration space (conflict graph + mask counts)
  is **unchanged**, so raw workload scales linearly per added
  `(cell × TF × dir)` tuple.

## The shipped 5-cell OFAT lattice

One-factor-at-a-time (linear, **not** a cross product), cardinality **M = 5**:

| # | vwap | ema | role |
|---|------|-----|------|
| 0 | HLC3 | CLOSE | baseline (byte-identical) |
| 1 | OPEN | CLOSE | VWAP perturb — 1-bar-lag anchor (NIFTY-futures only¹) |
| 2 | CLOSE | CLOSE | VWAP perturb — close-anchored VWAP (NIFTY-futures only¹) |
| 3 | HLC3 | OPEN | EMA perturb — lag anchor |
| 4 | HLC3 | HLC3 | EMA perturb — typical-price EMA |

CLI: `--price-source-cells "hlc3:close,open:close,close:close,hlc3:open,hlc3:hlc3"`.

¹ VWAP-source cells are meaningless on the 3 **spot** indices — indices carry no
volume, so bits 52-53 never fire. They apply **only** to the NIFTY-futures
dynamic grid. EMA-source cells apply everywhere.

## Default-omit (Override 1) — run-id back-compat

The `vwap_source` / `ema_source` params keys are **OMITTED** from every canonical
params / store-address / cache key / grid identity when they equal the default
`(HLC3, CLOSE)`, and emitted **only when non-default**. This is a **deliberate,
documented divergence** from `fib_anchor`'s unconditional emit.

- **Why:** a sibling Rust-core owner + a product-audit identity PR have a **hard
  dependency on existing run_ids staying byte-identical** (their twin-baseline
  capture). Unconditional emit would re-key every existing run.
- **No ambiguity:** absent ⟺ default — a clean bijection.
- **Pinned by a golden:** the default `(HLC3, CLOSE)` cell's
  `canonical_trade_params` + `canonical_spec_params` + resulting `run_id` are
  byte-identical to before the change (impossible to pass under unconditional
  emit). See `tests/unit/test_engine_price_source_axis.py::TestOverride1DefaultOmit`.

The two params-emit classes are now:
- **Unconditionally emitted:** `{fib_anchor}`.
- **Optionally emitted when non-default:** `{vwap_source, ema_source}`.

## HONEST budget verdict — REDUCED / STAGED (Override 3, Golden Rule #10)

The measured baseline (locked k=1..5 NIFTY-futures grid) is **≈ 0.45 – 1.48
M4-days** (central estimates carrying uncertainty — no measured post-Apriori
wall exists; Apriori is **not in this checkout**).

- **A combined 5-cell run does NOT fit the ≤ 2-day bar** — 5× baseline =
  **≈ 2.3 – 7.4 M4-days**, exceeding two days even at the optimistic end.
- **It fits — with certainty — STAGED**: run each non-default cell as a
  **separate cardinality-1 invocation**, each structurally ≈ the baseline
  (**≤ 2 days per run**). This is exactly how the operator's acceptance bar
  ("max 2 days **per** real sweep run") is satisfied.

This PR ships the **axis infrastructure** (dormant at cardinality 1) + the 5-cell
OFAT **capability**. It wires **no default-on expanded run**; the production
3-index engine sweep stays M = 1, untouched. The nightly automation stays
baseline (the price-source axis is **not** in the nightly forwarded-keys set).

## RUST-OWNER INTERFACE CONTRACT (Python-only today)

The reduction stays Python pre-FFI. This is the authoritative spec for the
sibling Rust sweep-core port — Rust math + FFI signatures are **unchanged**; only
the per-cell scalar reduction must be mirrored bit-identically. It is reproduced
verbatim in the docstrings of `brutex/engine/stage1a.py::_reduce_vwap_source_paisa`
and `::_reduce_ema_source_float`:

```rust
enum PriceSource { Open, High, Low, Close, Hlc3 }   // hl2/ohlc4 intentionally absent

// VWAP -> typical_paisa: i64 (o/h/l/c are exact 2dp paisa integers)
Open  => o_paisa
High  => h_paisa                    // (deferred variant)
Low   => l_paisa                    // (deferred variant)
Close => c_paisa
Hlc3  => let s = h_paisa + l_paisa + c_paisa;   // exact i64 add
         let (q, r) = (s / 3, s % 3);           // Euclidean, s >= 0 always
         q + if 2*r >= 3 { 1 } else { 0 }       // ROUND_HALF_UP on thirds

// EMA -> ema_source: f64 (RUPEES — the Decimal rupee price cast once, NOT paisa)
Open  => float(bar.open)                         // == today's float(bar.open), verbatim
Close => float(bar.close)                        // == today's float(bar.close), verbatim
Hlc3  => float( (H + L + C) / Decimal(3) )       // Decimal-divide THEN one float() cast
```

Note the unit split: the **VWAP** arm is int64 **paisa** (2dp-snapped), the
**EMA** arm is **rupees** (float) — the EMA source is deliberately NOT
paisa-snapped (today's unsnapped EMA contract; the snap happens once AFTER the
recurrence via `_ema_to_paisa`). A literal Rust port MUST feed the rupee value
to the EMA recurrence, never a ×100 paisa integer.

The integer HLC3 form is **provably bit-identical** to Python's
`int(round_money((H+L+C)/Decimal(3)) * 100)`: at the paisa scale the remainder
`r ∈ {0,1,2}`, so `r/3` never equals ½ — no tie reaches the ROUND_HALF_UP
boundary. In Python the Decimal `_price_to_paisa` path is kept (do **not**
float-divide, do **not** add a new integer path) so the baseline is byte-identical.
For EMA HLC3, mirror Decimal-divide-**then**-one-`float()`, **not**
`(f64(H)+f64(L)+f64(C))/3.0` — the two are not IEEE-754-identical.

## Deliberately deferred / excluded — DECISION ITEMS for the operator

- **`hl2` / `ohlc4` — EXCLUDED from the enum.** They are HLC3-clones on liquid
  1-minute index/futures bars (correlated Sharpe draws that worsen DSR
  effective-N), AND their `/2` / `/4` reductions can land on a **½-paisa rounding
  tie** that the `/3` path provably never hits. Re-adding either needs a
  clones + rounding-tie review + a golden.
- **`HIGH` / `LOW` — DEFERRED (in the enum, not in the shipped OFAT lattice).**
  They carry a mild **systematic bias against the fixed-`close` comparison**
  (every VWAP/EMA vocabulary bit compares against raw `close`), a correctness
  hazard that risks the Stage-1B frequency gate. They ship in the enum for
  completeness (and are tested directly), enabling a round-2 `{HIGH, LOW}`
  extension (→ M = 9) if a calibration frees headroom. Whether to build them at
  all is an operator decision.
- **`comparison_price_source`** (sweeping the reference line too) is a **named,
  explicitly-not-shipped** future third axis — see
  `brutex/engine/stage1a.py` §2.4 rationale.

## Honest limits (Golden Rule #10)

- **Stage-3 refinement + verify thread the source (COMPLETE).** A persisted
  `ResultRow` carries neither the swept `fib_anchor` nor the `(vwap, ema)` source
  as a column, so `stage3_wire.recover_survivor_cell` re-derives BOTH by matching
  the row's `run_id` against the grid's `sl × tp × proximity × fib_anchor ×
  price_source_cells` product, then refines / re-hashes / records the survivor
  against ITS OWN source bits. The Stage-3 manifest (`Stage3CellRecord`) and the
  `sweep-verify` reconstruction round-trip the source (additive, default-omitted),
  so a non-default cell verifies at its originating bits — never a silent
  `(HLC3, CLOSE)` fallback (before this was fixed, a non-default cell + Stage-3
  aborted with `Stage3WireError`).
- **Options-translation replay is BASELINE-SOURCE ONLY.**
  `cli._replay_spot_signals_for_row` rebuilds `bar_bits` at the default
  `fib_anchor` (prev-day) + price source `(HLC3, CLOSE)` because a `ResultRow`
  carries neither as a column. A survivor swept under a non-default `fib_anchor`
  or price-source cell therefore replays its options translation with the wrong
  Group-4 / VWAP-EMA bits. This is a **pre-existing** limitation (equally true for
  `fib_anchor` since #1011); closing it requires `ResultRow` to carry the axis
  coordinates. Out of scope for this axis.
- **VWAP-on-spot is documented, not hard-enforced.** A VWAP-variant cell
  (`vwap_source != HLC3`) on a non-NIFTY-futures / engine-spot instrument produces
  meaningless bits 52-53 (a spot index carries no traded volume). This is surfaced
  as a **non-blocking `stage1a.vwap_source_on_spot` WARNING** at grid-config
  validation (so an operator does not waste a ~2-day run on junk bits) — it is NOT
  a refusal: the run still completes, and the operator may deliberately probe it.
