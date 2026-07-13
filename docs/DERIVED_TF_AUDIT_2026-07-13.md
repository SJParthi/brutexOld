# Derived-Timeframe Audit — 2026-07-13

> **DEC-DERIVED-TF-AUDIT-001** (MIGRATION.md #1790; per-fix DECs #1791–#1795).
> Every `file:line` cite below was verified at the merge-base SHA
> `810523458bc835a72ed0bd229049895896182fd3` (origin/main); symbols are named
> beside line numbers so the cites survive drift.

## §0 Verdict

**MATH CORRECT / BOUNDARIES LEAK.**

The resampling MATHEMATICS — the 09:15-IST fixed-arithmetic bucket grid, OHLCV
aggregation, strict-last OI snapshot semantics, the midnight-IST daily
re-stamp, forming-session cutoffs, hole handling, UTC month/year straddles,
and the write→derive→store chain — is EXACT against an independent
from-scratch reference over 161,183 bucket / 967,098 field comparisons
(**zero** OHLC/label/presence mismatches) and bit-identical between the Rust
native core and the FFI wheel over the frozen 400-case vocabulary fixture.
The confirmed defects all live at the BOUNDARIES: what the store-boundary
writer fabricates (OI 0), what the read gates screen (daily window read
ungated; bad-tick asymmetry; the unbounded parse clamp), what the freshness
machinery covers (no 1day arm; OI-blind; first-symbol doctor), and what the
loader's grid MODEL predicts (session-open vs 09:15 anchor).

Five defects are FIXED in this change set; the remainder are RESIDUAL — each
pinned by a committed behavior-capture test and named to a follow-up DEC
(§4/§6). **HONEST DISCLOSURE:** the two largest-blast-radius findings — the
bad-tick prebuild/derive asymmetry (H2) and serve-time staleness (H5a) — are
BOTH residual, so the "inconsistent worlds" defect (a 1m sweep and a 5m sweep
of the SAME lake can consume different prices) remains fully open; the
derived-TF proof is scoped to *given the stored 1-minute base*.

## §1 Method + environment honesty

- **Environment:** CPython 3.12.3 (venv; the system python is 3.11 and cannot
  parse the repo's PEP-695 generics nor host the abi3 wheel), polars 1.42.1
  (observed; repo pin `>=1.0,<2.0`), duckdb/pyarrow from the repo's `[data]`
  extra; cargo 1.94.1 for the Rust legs. Read-only audit phase — no repo file
  was written while evidence was gathered.
- **Fidelity:** every repro built its lake through the REAL production
  writers (`ParquetWriter`/`ParquetBarSink`), derived through the REAL
  `materialize_lake_timeframes` / `derive_daily_for_targets`, read through
  the REAL engine mouth (`Catalog.get_bars` → `feed.replay
  .arrow_table_to_bars`), the REAL Groww parser (`parse_candle_rows`), the
  REAL movers readers, and the REAL `/db` `db_view.query_candles`. No stubs
  of instrument metadata anywhere. The only hand steps were two
  explicitly-stated partition poisons (H5 drift, H6 physical dup) — both
  model on-disk states production documents as reachable
  (stale-after-crash/backfill; the pre-DEC-LAKE-WRITE-UNIQUE-001 "poisoned
  era" duplicates).
- **Reference:** a stdlib-only (datetime/decimal) from-scratch resampler
  derived from the documented contract alone (the `tf_resampler` header
  tables + the daily-converter docstrings) — no brutex/polars imports. Where
  the docstring itself was the defect (the "session open" anchor prose), the
  reference equals production by construction; those cases were attacked via
  the session-validity/consumer angle instead (B-S1/B-S2).

## §2 Core parity numbers (differential, captured verbatim)

Corpus: 23 handcrafted frames + 200 seeded random frames (`seed=42`) =
**223 frames** (1 → 1,125 bars): full/holed/single-bar sessions, 5 F&O OI
patterns, Muhurat 2024-11-01 (18:00) + 2025-10-21 (13:45), UTC month/year
straddles, weekend-gap triples, pre-09:15 + cross-midnight shapes, zero and
2⁶²-scale volumes, 2-dp edge prices.

| Quantity | Count | Mismatches |
|---|---|---|
| Bucket comparisons (1m no-op + 19 intraday TFs + 375 + 2×daily-midnight + routers) | **161,183** | **0** label/presence |
| Field comparisons (o/h/l/c exact-float · volume exact-int · OI exact) | **967,098** | **0** OHLC |
| Writer-view bucket checks (`frame_to_candle_bars` as `make_lake_write_tf` persists) | 7,430 | 2,650 = the H1 OI-0 class (fixed here) |
| Router frame checks (`derive_intraday_tfs`/`derive_fno_intraday_tfs` ≡ per-TF canonical) | 38 | 0 |
| OI-ambiguity buckets (strict-last vs last-non-null differ) | 1,670 | production == **strict-last 1,670/1,670** |

- **OI semantics settled:** the docstring-literal reading ("the LAST source
  bar's OI … a null OI source bar yields a null bucket OI") is the
  implemented semantic; the audit brief's "last non-null" paraphrase is
  REFUTED as a description of production (0 mismatches vs the strict-last
  reference). Pinned by `test_oi_strict_last_discrimination_pin`.
- **Volume:** zero mismatches at realistic volumes; every volume mismatch
  involved synthetic ≥2⁶⁰ values (the deliberate overflow probes → M2/M3,
  residual, §4).

## §3 Rust / FFI parity

- **Native:** `cargo test -p trading-core --release` → **166 passed / 0
  failed** (125 lib + 41 across 9 integration suites), including
  `golden_vectors.rs::rust_reproduces_every_golden_vector`.
- **FFI:** the `trading_core` 0.3.2 abi3 wheel (maturin, py3.12 venv):
  `scripts/check_vocab_rust_parity.py` → **`cases=400 mismatches=0`, exit
  0**; the pytest FFI battery → **22 passed / 0 failed** (the 400-mask golden,
  the production `evaluate_with_levels` cross-check incl. the `pdc==0`
  gap-day path, and the twin-state replay).
- **Same bytes both legs:** `tests/golden/fixtures/vocab_golden_vectors.json`
  sha256 `19640054cb39e50bb33088dd31c496d69706e8cbfe0690e9f11ef86862831a3a`
  (400 cases) — hashed before AND after all runs, identical; `git status`
  clean. The twin fixture sha256
  `85a0e57dbde7f2dcba91e71184f8c2c4bbf488a92e1d8104a9db92477f3dd08e` matches
  the frozen `_EXPECTED_SHA256` pin. **Transitivity:** wheel ≡ fixture
  (400/400) AND native ≡ fixture (on the same bytes) ⇒ wheel ≡ native across
  the whole 400-case vocabulary contract.
- **fno-math:** `cargo test -p fno-math --release` → **584 passed / 0
  failed** (1 ignored doc-test slot).
- **Honest limit:** since the hand-synced twin deletion
  (DEC-INDICATOR-TWINS-RUST-SINGLE-SOURCE-001) the Python "side" of the
  golden suite delegates to the wheel — this evidence is exactly what it
  claims, FFI-vs-native via the frozen independent fixture, NOT a third
  independent implementation.

## §4 Defect table

Every row carries its verbatim key repro line(s), the fix/residual status,
the anchoring test, and reachability honesty.

| # | Defect (site at merge-base) | Verbatim key evidence | Status | Anchoring test | Reachability |
|---|---|---|---|---|---|
| H1 | Derived intraday F&O partitions fabricate `open_interest=0` for null-OI buckets (`timeframe_materialize.py:1012` `make_lake_write_tf._write` → `frame_to_candle_bars` default `absent_open_interest=0` at `spot_daily_resample.py:597`) | `resampler emitted OI per bucket (5m): [1004, None, None]` / `5minute stored OI per bucket: [1004, 0, 0]` / `1day stored OI: [None]` | **FIXED** — DEC-DERIVED-OI-NULL-HONEST-001 (#1791): one argument at the ONE writer + the `writer_convention` stamp heal | `tests/integration/test_derived_oi_null_honest.py` (fails at merge-base); the oracle FNO stored-lake leg | Every derived intraday F&O partition with a null-last-OI bucket; materially wrong for F&O (`/db` displays OI). The spot 1m base itself stores null — "the spot convention" was never true. |
| H3 | Parse-time clamp legitimizes a corrupt ×10 OPEN into a stored fabricated HIGH (`candle_fields.py:242-243` `assemble_candle` clamp; `bad_tick.py:106-131` close-only rule) | `row#7 PARSED bar: open=1000.55 high=1000.55` / `[price_sanity] violations: 0` / `served=15` / `[stored 1day] high: [1000.55]` | **FIXED** — DEC-OHLC-CLAMP-BOUNDED-001 (#1795): clamp-MOVE bounded at strict >4× (`BAD_TICK_MAX_RATIO` imported, never re-minted); refused rows take the graceful counted skip; kill switch `BRUTEX_OHLC_CLAMP_REFUSE` | `tests/unit/test_candle_fields_clamp_refuse.py` (fails at merge-base) | The ONLY defect that poisons the 1m sweep itself; the fabricated extreme passed EVERY downstream screen. Pre-fix stored clamp-fabricated bars are NOT healed (indistinguishable post-hoc from real prints); refused rows are ABSENT, re-pullable. |
| H4 | `make_lake_read_1m_window` (`spot_daily_resample.py:1063-1151`) has no price-sanity gate — the daily side-paths launder what the intraday prebuild refuses | `[intraday prebuild] ResampleInputError raised` / `[daily derive] report: derived=1 errored=0` / `[stored 1day] low=[0.0]` | **FIXED** — DEC-DAILY-WINDOW-SANITY-GATE-001 (#1792): the SAME COMMON `validate_frame_price_sanity` call as the gated intraday twin (`timeframe_materialize.py:951-955`) | `tests/integration/test_daily_window_read_sanity.py` (fails at merge-base) | Heal-recall 1day, crossverify day-derive, ledger daily reconcile — all became correct-or-absent with zero wiring changes (the raised error was already the counted quarantine class). |
| H5(b) | The derived `1day` is freshness-checked NOWHERE (`tf_autoderive.py:107-110` abstain; `doctor.py:388` first-symbol + intraday-only) | `[gate] verdicts: {('NSE-NIFTY','5minute'): 'fresh'} (NO 1day key)` / `[movers daily] served 1day closes: [77777.77]` | **FIXED** (the gate half) — DEC-DERIVED-1DAY-FRESHNESS-001 (#1793): the segment-routed 1day VALUE arm + OI-aware compare + doctor all-symbols census | `tests/unit/test_tf_autoderive_daily_arm.py` + the doctor two-symbol test (both fail at merge-base) | Sweep preflight + doctor now cover the 1day; the SERVE-time half stays H5(a), residual below. |
| B-S2 | `series_load._expected_bar_time_in_window` (`series_load.py:110-212`) predicted the bucket grid from the SESSION open, not the resampler's 09:15 anchor → loud false "EMPTY — re-pull" on a healthy Muhurat lake | `[30minute] window [18:00 → 18:10 IST] -> RAISED FuturesSeriesError: stored partition … is EMPTY … re-pull` (stored labels: `['17:45','18:15','18:45']`) | **FIXED** — DEC-SERIES-LOAD-GRID-HONEST-001 (#1794): re-anchored in place at `day @ 09:15 IST`, k ≥ 0, bucket-overlaps-session; regular sessions byte-identical (parity-pinned); Fragment B flips skip→read | `TestMuhuratGridHonest` in `tests/unit/test_feed_series_load.py` (Fragment A + end-to-end fail at merge-base; mirror pin vs the REAL resampler) | Narrow (needs a window edge strictly inside a Muhurat evening on a non-divisor TF), but loud-with-WRONG-remediation, and the helper is the skip-vs-refuse arbiter for every roll-boundary sliver. |
| H2 | Bad-tick asymmetry: the 1m sweep SKIPS the ×10 bar (BadTickScreen at `feed/replay.py:330/354`); the prebuild ABSORBS it (`make_lake_read_1m` runs price-sanity only) | `[engine 1m read] stored rows=15 -> bars served=14` / `max high served: 101.2` / `[stored derived 1day] high 1005.0` / `[engine 5m read] served bucket highs: [100.7, 1005.0, 101.2]` | **RESIDUAL** (the TOP residual by blast radius) → DEC-PREBUILD-BAD-TICK-001 (the D2 one-law `lake_screen` shape is the recorded design direction: dedup + sanity + bad-tick at every 1m mouth, all-or-nothing, `BRUTEX_BAD_TICK_SCREEN`-gated, `writer_convention` bump to 2) | `test_prebuild_bad_tick_defect_pin` | Inconsistent worlds fully OPEN: a 1m sweep and a 5m sweep of the SAME lake consume different prices; the derived 1day (the PDH/PDL anchor source) carries the spike. Cut for size: the fix must wire ALL FOUR 1m mouths atomically or it CREATES new splits. |
| H5(a) | Serve-time staleness: movers stored-first (`movers_snapshot.py:1081-1103`) + `/db query_candles` serve drifted/corrupt derived bytes with no check | `[gate] verdicts: 'stale'` / `[movers] from_stored=True, served closes=[100.3, 999.99, 100.8]` / `[/db] closes: [100.3, 999.99, 100.8]` | **RESIDUAL** → DEC-DERIVED-SERVE-FRESHNESS-001 (an O(1) derive-stamp/1m stat-token compare before a stored-first serve — never a re-derive per poll) | `test_stale_stored_served_defect_pin` | The display half of H5; compounds H2 + H6 (a frozen/poisoned derived partition serves silently between prebuilds). |
| H6 | Dup-ts three-posture divergence: engine keep-first+WARN (`catalog.py:576-592`) vs prebuild quarantine (`tf_resampler.py:142-166` via `make_lake_read_1m`) vs compare-reader silent dedup (`tf_autoderive.py:74-79`) | `served bars=15 (16 physical)` + WARN / `ResampleInputError: ts_utc must be STRICTLY increasing` / `dedups the same poison silently: rows=15` | **RESIDUAL** (each posture documented + loud; the DEFECT is the divergence) → DEC-PREBUILD-DEDUP-CONVERGE-001 | `test_dup_ts_three_posture_defect_pin` | Until L2/L3 heal runs, a 1m sweep advances while every derived-TF consumer is frozen at the last good derive; the posture call needs the H5(a) companion. |
| B-S1 | Muhurat off-session bucket labels are STORED (writer side): 17:45 @30m / 17:15 @60m/@240m / 17:59 @2m on the 18:00 open; 13:43 @4m on the 2025 13:45 open | `STORED timestamp=2024-11-01 17:45 IST … oi=50014` (real-writer parquet) | **RESIDUAL** (docstring corrected here — the "never straddle sessions" prose; the labels stand) → DEC-MUHURAT-TF-ANCHOR-001 (an anchor change = a coordinated DEC across resampler + `_stored_serves_cutoff` + `resolve_asof_cutoff` + a lake-wide re-derive — never a drive-by) | `test_muhurat_off_session_labels_stored_defect_pin` | Same-IST-date always holds → month routing unaffected; the labels precede the session open, not the calendar day. |
| M2/M3 | Silent Int64 volume wraparound in `.sum()` (`tf_resampler.py:274`); a negative wrap raises an UNTYPED ValueError from `CandleBar` | `2·2⁶² → −9223372036854775808`; `4·2⁶² → 0` (stored silently); `ValueError: CandleBar.volume must be non-negative` | **RESIDUAL** (latent/unreachable) → DEC-RESAMPLE-VOLUME-GUARD-001 | `test_volume_overflow_defect_pin` (doubles as the polars-upgrade canary) | A 375-min bucket overflows only at ~2.5e16 volume/min; every production volume is parse-capped at int64 (`candle_fields.to_int`). |
| P1/P2 | Pre-09:15 / sub-minute latents: a whole-minute 00:05 IST bar labels the PRIOR IST date (P2, formula parity both sides); a SUB-minute pre-open stamp truncates-not-floors (P1, `tf_resampler.py:258` `.dt.total_minutes()`) | `00:05 IST @30m → label 2024-01-01 23:45 IST` | **RESIDUAL** (dormant — regular ingest session-filters these bars; session-exempt symbols would hit it) → DEC-SESSION-EXEMPT-RESAMPLE-001 (P1 documented here only — no pin: sub-minute stamps cannot enter the lake) | `test_pre_open_prior_date_label_defect_pin` + the oracle pre-open leg | Unreachable today: lake 1m stamps are whole-minute and pre-open is ingest-filtered. |

## §5 Harness map + re-run procedure

Committed permanent nets (the raw E1/E2 scratch artifacts are deliberately
NOT committed — these tests are the durable proof):

| Net | File |
|---|---|
| Independent oracle (OI-intraday · midnight daily · stored-lake · Muhurat/evening · pre-open · weekend-gap · FNO-stored-OI · as-of cut · strict-last pin) | `tests/property/test_tf_resampler_independent_oracle.py` |
| H1 fix net | `tests/integration/test_derived_oi_null_honest.py` |
| H3 fix net | `tests/unit/test_candle_fields_clamp_refuse.py` |
| H4 fix net | `tests/integration/test_daily_window_read_sanity.py` |
| H5(b) fix net | `tests/unit/test_tf_autoderive_daily_arm.py` + `tests/unit/test_doctor.py::TestDerivedFreshness` |
| B-S2 fix net | `tests/unit/test_feed_series_load.py::TestMuhuratGridHonest` |
| Residual pins R1–R7 | `tests/integration/test_derived_tf_defect_pins.py` |

One-command re-run of the whole audit net:

```bash
pytest tests/property/test_tf_resampler_independent_oracle.py \
       tests/integration/test_derived_oi_null_honest.py \
       tests/integration/test_daily_window_read_sanity.py \
       tests/integration/test_derived_tf_defect_pins.py \
       tests/unit/test_candle_fields_clamp_refuse.py \
       tests/unit/test_tf_autoderive_daily_arm.py \
       tests/unit/test_feed_series_load.py -q
```

Rust parity re-run: `cargo test -p trading-core --release && cargo test -p
fno-math --release && python scripts/check_vocab_rust_parity.py`.

## §6 Honest limits + residual register

- **Scope sentence (repeat, deliberately):** with H2 and H5(a) residual, the
  derived-TF correctness proof is scoped to *given the stored 1-minute
  base* — the base↔derived bar-set equivalence under the bad-tick screen and
  the serve-time freshness of stored derived bytes are NOT yet guaranteed.
- One-time heal cost: the `writer_convention` stamp bump forces exactly ONE
  full re-derive per symbol on the next prebuild (~hours once on a full
  options tree, disclosed); until it runs, existing lakes still serve OI=0 on
  derived intraday F&O partitions.
- Legacy OI=0 partitions read `stale` under the OI-aware compare for at most
  one nightly (the stamp bump rewrites them the SAME nightly) — SPOT engine
  lakes INCLUDED, not only F&O (real spot 1m bases carry an all-null OI
  column, so `sweep-all` on an un-re-prebuilt legacy spot lake fail-closed
  refuses until the one-time re-derive; review-round-1 R1-MINOR-1).
- A derived-TF FUTURES **or SPOT-index (NIFTY/BANKNIFTY/SENSEX)** sweep
  re-keys `data_snapshot_hash` after the one-time OI re-derive — bar field 7
  folds `open_interest`, so the WHOLE derived-intraday engine surface
  re-keys, not only futures (the #1743 post-deploy re-key story; sanctioned,
  not corruption; review-round-1 R3-M1). The frozen sweep goldens are
  fixture-driven (in-memory bars — they never read a stored derived-intraday
  partition) and reproduce byte-for-byte: consistent with, not a refutation
  of, the lake-side re-key.
- Out-of-datetime-range i64 stamps in the DAILY freshness arm fail-closed to
  `stale` + one WARN on BOTH sides: the stored-1day reduction (review-round-1
  R4-M1, full `datetime_out_of_range_errors()` set) AND — since refuter
  round 1, whose executed repro escaped a PanicException from a corrupt
  1-MINUTE base through `lake_derived_freshness_verdicts` — the base
  re-derivation (the `datetime_out_of_range_escape_errors()` set; ValueError
  deliberately excluded per the #1594 consumption law, since the resample
  spans arbitrary derive code). The intraday twin is polars-only (`.equals`)
  and was verified graceful on the same corrupt base — no guard needed there.
- Clamp-refused corrupt rows are ABSENT, not repaired (keep-first + durable
  ledger re-pulls them); a corrupt field within 4× still clamps-and-stores
  (the original observe-only envelope, now bounded); a corrupt extreme that
  never triggers a clamp is untouched (DEC-BADTICK-EXTREMES-002).
- The 1day freshness arm is VALUE-fidelity only — recency-by-absence
  (prebuild lag) deliberately stays out of the gate; orphan stored days WARN
  observe-only.
- The doctor census is O(symbols × stored bars) per invocation — an
  on-demand diagnostic, honesty > speed.
- Overflow/wrap behavior is as observed on polars 1.42.1 (repo pin
  `>=1.0,<2.0`); the R5 pin is the upgrade canary.
- E2 ancillary observation (no confirmed verdict, no DEC minted — flagged
  for a follow-up worker): an internally-INCONSISTENT stored row
  (close > high) makes the movers converter `_row_to_bar` → `CandleBar`
  raise a raw `ValueError`, which `movers_resilient.py:45` deliberately
  treats as a code bug (not a skip) — if such a partition existed the
  /movers candidate loop would propagate rather than skip-and-count.
- Residual register (each = a committed pin + a NAMED follow-up DEC, §4):
  R1 DEC-PREBUILD-BAD-TICK-001 · R2 DEC-DERIVED-SERVE-FRESHNESS-001 ·
  R3 DEC-PREBUILD-DEDUP-CONVERGE-001 · R4 DEC-MUHURAT-TF-ANCHOR-001 ·
  R5 DEC-RESAMPLE-VOLUME-GUARD-001 · R6 DEC-BADTICK-EXTREMES-002 ·
  R7 DEC-SESSION-EXEMPT-RESAMPLE-001. Cross-referenced pre-existing named
  follow-ups (no new ids): DEC-LAKE-AUDIT-SETTLING-ROWS-001; the Muhurat
  2026-11-08 calendar entry (operator-circular-gated).
