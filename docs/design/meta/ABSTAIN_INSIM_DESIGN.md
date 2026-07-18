# In-Sim Abstention Gate — Composition Design (DORMANT)

> **DEC-ABSTAIN-GATE-STREAM-001** (MIGRATION.md #1985) — the DESIGN record
> for the future in-sim arm of the abstention-first gate stream. What SHIPS
> today is annotation-only: the `.meta.json` abstain replay + the fail-loud
> dormant-knob refusal (`BRUTEX_ABSTAIN_GATE_INSIM` set to ANY value ⇒ the
> typed `AbstainGateInSimRefusedError` at the `cmd_sweep_all` chokepoint —
> the `BRUTEX_FILL_MODE` set-knob precedent). NOTHING in this document is
> built into the engine; building it requires its OWN DEC + ADR + the
> operator gate-flip ruling.

## 1. The shipped surface (context)

* `brutex/meta/abstain.py` — the pure total gate law: the STABLE wire
  vocabulary `ABSTAIN_REASON_ORDER = ("regime_mismatch", "vix_out_of_band",
  "vix_missing", "event_day", "low_confidence")`, the frozen validated
  `AbstainGateConfig`, `abstain_reasons()` (passes ⇔ `reasons == ()`,
  property-pinned), and the batch bitmask twins (`abstain_masks_python`
  oracle / the `rust/brutex-meta` kernel behind the default-OFF
  `BRUTEX_META_GATE` facade `brutex/meta/meta_rust.py`).
* The REPLAY consumer (`brutex/meta/annotate.py`) judges each captured
  trade's ENTRY snapshot — regime + event flag from the SessionLens at the
  entry's IST session date, VIX from the trade's own `vix_at_entry` stamp,
  confidence from the strictly out-of-fold `p_win` — and records
  per-trade reasons + `would_abstain_count` + `kept_pct`. Annotation only:
  no trade is removed, no metric re-ranked, `top100.json` byte-untouched.

## 2. Identity re-key law (the future in-sim arm)

An in-sim gate CHANGES which trades exist, so it is a RUN-IDENTITY axis by
Golden Rule #4. The reserved `canonical_trade_params` key is
**`abstain_gate`** (recorded in DEC-ABSTAIN-GATE-STREAM-001; the key is
NEVER emitted today — emitting it default-OFF would alias every existing
run_id). The future arm emits it UNCONDITIONALLY once built (the
`fill_mode` anti-aliasing law of DEC-FILL-WORST-CASE-ONLY-001): absent key
= the frozen pre-gate era; present key = the gated era, value = the frozen
config's canonical dict (regimes tuple, vix band, block_event_days, tau,
model provenance digest). Old stores refuse loudly naming the DEC —
"re-run the sweep" — exactly the precise-era-store precedent.

## 3. Walker composition (designed, not built)

The gate composes at the ENTRY-ARMING seam of `TradeSimWalker` — the ONE
place a signal becomes a pending entry — as a kw-only constructor hook:

```python
TradeSimWalker(
    direction=..., tf_minutes=..., stop_loss_paisa=..., take_profit_paisa=...,
    reference_stamper=None, detail_sink=None,
    abstain_gate=None,   # the future hook — None today, byte-identical
)
```

* `abstain_gate=None` (the default) is BYTE-IDENTICAL to today — the same
  defaulted-kw-only pattern `reference_stamper`/`detail_sink` already use.
* A non-None gate is consulted ONCE per would-be entry, at the signal bar's
  ENTRY SNAPSHOT (never the fill bar — no look-ahead: every gate input is
  observable at the signal bar under the P-06 barrier). `reasons != ()` ⇒
  the signal is DROPPED-AND-COUNTED (a new append-only counter
  `entries_abstained`, the `exits_session_gap` serialization template:
  EXIT_REASONS untouched — abstention prevents entries, it never exits).
* The Rust kernels (u64 + u128 via the shared `MaskWord` core) move in the
  SAME change with the gate decision computed as a per-bar bitmask input
  (the `rust/brutex-meta` `abstain_masks` face is already batch-shaped for
  exactly this), pinned by the existing Python↔Rust parity suites.

### W4 coordination (binding, recorded)

W4's 1-minute execution grid lands first on branch
**`claude/fill-1m-exec-grid` @ `779fa9e2`** (PR-B of the worst-case-fills
track). The compose contract with it:

* The walker **CONSTRUCTOR stays unchanged post-PR-B** — W4 adds kw-only
  knobs, never positional params — so the future `abstain_gate=None` ctor
  hook composes cleanly with the PR-B walker (no signature collision, no
  ordering hazard).
* W4's new faces are `push_1m` / `run_with_exit_indices`, and
  `simulate_trades` gains a required `exec_bars_1m` — the abstain hook is
  ORTHOGONAL to all three (it fires at entry-arming on the DECISION-TF
  stream, before any 1-minute execution resolution).
* Name reservations (W4 owns, W6 must NEVER emit):
  `canonical_trade_params` key `execution_grid`; counters `exits_deferred`,
  `signals_dropped_collision`, `fill_slid_minutes`. W6's reserved names:
  the `abstain_gate` params key + the `meta_label`/`abstain_gate` display
  keys. The shared fold-key law: `.meta.json` + the abstain replay key
  trades ONLY by `trade_seq` + entry/exit TIMESTAMPS (`entry_ts_us` /
  `exit_ts_us`) — NEVER bar indices or `holding_bars`-derived quantities,
  so W4's re-gridding can never silently re-key W6's annotations.
* `/tmp/claude/memory/team/project/w4-w6-fold-key-reservations.md` did NOT
  exist when this was written (checked 2026-07-18) — the reservations above
  are the coordinator-relayed set, recorded here verbatim.

## 4. Statistical interactions (honest limits)

* **MIN_TRADES=30 / DSR:** an in-sim gate SHRINKS trade counts, so gated
  combos fall below `MIN_TRADES` more often (honest — fewer, better trades
  is the thesis) and every DSR/CPCV statistic must be recomputed on the
  GATED stream (never a filtered view of ungated metrics). The τ sweep's
  `n_thresholds_swept` (= 10, recorded in every `.meta.json`) feeds the DSR
  trials-count so threshold selection can never double-dip.
* **VIX REFERENCE-ONLY collision:** `vix_out_of_band`/`vix_missing` consume
  the `vix_at_entry` STAMP (CLAUDE.md §2 Rule 3 — VIX is reference-only,
  never in the 60-vocab). An in-sim VIX door would make VIX
  RESULT-CHANGING, which that rule currently forbids: the gate-flip DEC
  must either amend Rule 3 explicitly (operator ruling) or ship with the
  VIX doors structurally disabled in-sim (`vix_lo=vix_hi=None`). Recorded
  as a hard precondition, not silently decided here.
* **Missing-stamp fail-open vs fail-closed:** the shipped replay treats a
  missing VIX stamp as `vix_missing` (a named reason) ONLY when a VIX band
  is configured; the default config configures NO band, so a stampless
  lake gates on confidence + event days alone (honest, counted). The
  in-sim arm inherits the same law.
* **P(win) provenance:** in-sim confidence must come from a model FROZEN
  before the gated run (train-on-past, trade-on-future); the shipped OOF
  column is a research annotation and is NOT that model. The Kelly
  bet-size consumer chain stays unbuilt (`DEC-KELLY-BET-SIZE-001` band
  #1974–#1976 — backticked, unminted).

## 5. Flip checklist (the future DEC's gate)

1. Operator gate-flip ruling + the Rule-3 VIX decision (§4).
2. Own DEC + ADR; `abstain_gate` params key emitted unconditionally;
   golden re-locks with in-file hand-derivations.
3. Walker + u64/u128 kernels in ONE change; parity suites unmodified.
4. `entries_abstained` counter through combo_aggregates → AGG_COLUMNS →
   top100 exits-mix surfaces (the #1743 serialization chain).
5. The refusal at `cmd_sweep_all` flips from "refuse any set value" to a
   real config parse — the refusal test consciously flipped, never
   silently deleted.
