_Last updated: 2026-07-17 (DEC-PREOPEN-DAY-FRESHNESS-001, MIGRATION.md #1930,
branch `claude/preopen-day-freshness-guard` — the preopen DAY-basis (the
DEFAULT report) gains the stored-vs-resample derived-1day FRESHNESS guard,
the /movers #1795a verify-equality law transplanted: stored ≡ resample is a
WRITE-TIME guarantee, not an invariant — a 1-minute write after the last
prebuild left the derived `1day` stale and the report served +3.49% where
the 1-minute truth was −47.55% with ZERO warnings (the executed refuter
repro F-D1/worker_d_probe2; the #1649a contiguity guards catch missing
SESSIONS, never stale VALUES — a verified distinct lane). Each row's stored
(IEP, PREV. CLOSE) pair is served only when it EQUALS the fresh resample of
the stored 1-minute base (the ONE COMMON `spot_daily_resample` window
reader + daily resampler — never a second math copy); a diverging row is
served RECOMPUTED from the 1-minute truth + ONE capped
`preopen.stale_daily_recomputed` WARN per dataset triple — ROW-ATOMICALLY
(both consumed values from the one 1m-derived scale or neither; the TMPV
cross-basis explosion of DEC-PREOPEN-REPORT-1DAY-001 negative-pinned),
scoped to the CONSUMED open/close only, fail-OPEN on a missing 1m base
(disclosed), row-SET-preserving, one month-bounded 1m read + resample per
day-basis symbol per cold build (disclosed O-cost — warm clicks stay
zero-read on the #1719 memo). SAME-DEC B-3 fold: the derived-intraday F&O
writer's present-but-null bucket OI is stored `None` (ALIGNED with the F&O
daily derive + the resampler's own never-fabricate-0 law; spot stored-0
byte-identical; the roundtrip oracle's null arm consciously flipped).
Fails-before proven executed at merge-base ecb37cc; fresh lakes
byte-identical (the frozen preopen goldens pass with fixtures
BYTE-UNTOUCHED). READ-side dashboard + ONE derived-writer alignment —
ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no lake/schema/scope/vocab/rate
change. Named sibling (still open): DEC-MOVERS-DAILY-FRESHNESS-001 — the
movers-side daily lane. This header carries the pointer, never the story —
the full record lives in the §1 Active-branch blurb + MIGRATION.md #1930.)
Earlier: 2026-07-17 (DEC-TIMESTAMP-IST-SINGLESOURCE-001,
MIGRATION.md #1929, branch `claude/timestamp-ist-singlesource` —
timestamps 100% COMMON: the duplicated +05:30 IST-offset / 09:15
session-open / 15:30 session-close constants collapse onto ONE canonical
Python root each in `brutex/calendars/nse.py` (`IST_TZ_NAME` · `IST` ·
`IST_UTC_OFFSET` · `IST_OFFSET_MINUTES` · `IST_FIXED_OFFSET` ·
`SESSION_OPEN_IST` · `SESSION_CLOSE_IST`), unified strictly WITHIN
representation families (ZoneInfo/string · fixed-offset · polars-duration
via the NEW `data.session.ist_offset_duration()` — never across families),
BYTE-IDENTICAL behavior (per-swap identity pins + the affected goldens
UNMODIFIED); the Rust/JS/SQL copies that cannot import Python are
DRIFT-PINNED (`tests/unit/test_timestamp_ist_singlesource.py`); the
TF-registry PR #1904's files (movers_snapshot · tf_resampler et al.) are
EXCLUDED by coordination and the minute-bound SEMANTIC layer (375-bar laws,
minute_of_session, 15:29 close-edge, `_ist_minute_of_day_expr`) is the
named follow-up `DEC-SESSION-MINUTE-SEMANTIC-LAYER-001` — untouched.
READ/representation-only — ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no
lake/schema/scope/vocab/rate change. This header carries the pointer,
never the story — the full record lives in the §1 Active-branch blurb +
MIGRATION.md #1929.)
Earlier: 2026-07-17 (DEC-TF-REGISTRY-DYNAMIC-001, MIGRATION.md #1904,
branch `claude/tf-registry-dynamic` — the SECONDS-KEYED dynamic timeframe
registry: `brutex/data/timeframes.py` gains `TfSpec(label, duration_s,
base_granularity_s, anchor, lake_label)` + `TfAnchor{SESSION_OPEN, IST_DAY}`
+ the 21-entry `ALL_TFS` registry, with the legacy minutes-keyed vocabulary
(`ALL_TF_MINUTES`/`TF_LABELS`/`LAKE_INTERVAL_LABELS`) now DERIVED VIEWS
(byte-identical, literal-pinned); the tf_resampler bucket math is
seconds-keyed through the ONE `_resample_validated` engine + the NEW
`resample_frame_to_spec` (a config-added 20m or a sub-minute spec buckets
with zero resampler edits) — minute-TF output BIT-IDENTICAL, pinned by the
frozen-oracle parity suite over all 21 TFs; the two literal interval copies
KILLED (`feed/resampler.INTERVAL_SECONDS` + the synthetic adapter map now
registry-derived; the `1day: 86_400` epoch-bucketer quirk preserved +
sync-pinned); `partition.VALID_INTERVALS` derives from the full-registry
`ALL_LAKE_INTERVAL_LABELS` view; engine `Timeframe` gains the `duration_s`
property (minutes STAYS the field — grid-identity/digest safety, the
recorded deviation); movers gains HH:MM:SS as-of parsing (the ONE sanctioned
widening) + the look-ahead-safe seconds-instant law + `filter_to_ist_second`
framework seams, and its session-minute literals derive from the COMMON
`data/session` constants (the cheap timestamp fold; the wider IST
single-source sweep is the sibling DEC-TIMESTAMP-IST-SINGLESOURCE-001,
PR #1608). HONEST GATE: sub-minute TFs are framework-only — NO sub-minute
production feed exists (Groww is 1-minute-only); registering one needs its
own DEC + a real feed. ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no
lake/schema/scope/vocab/rate change. This header carries the pointer, never
the story — the full record lives in the §1 Active-branch blurb +
MIGRATION.md #1904.)
Earlier: 2026-07-17 (DEC-DERIVED-FRESHNESS-SCREEN-PARITY-001,
MIGRATION.md #1932, branch `claude/derived-freshness-screen` — the
derived-freshness gate (`tf_autoderive.lake_derived_freshness_verdicts` → the
sweep preflight + the doctor probe) now re-derives from the SAME
bad-tick-SCREENED 1-minute the prebuild writes from (screened-vs-screened via
the REUSED `bad_tick.screen_frame_bad_ticks`; the ONE `BRUTEX_BAD_TICK_SCREEN`
kill switch honored at the read site — OFF = the pre-parity unscreened
compare, byte-preserved), killing the B-1 PERMANENT false wedge: a lake whose
stored 1m legitimately holds a self-consistent ≥4x in-session spike (exactly
the class the screen exists for; write-side guards accept it) previously read
permanently "stale" — `cmd_sweep_all` hard-refused with NO escape flag and the
printed remedy (re-run the prebuild) could NEVER clear it (executed-repro'd at
merge base b81f017e). The B-2 ghost-bucket half (a stored derived bucket whose
SOLE 1m source is screened away survives every re-prebuild — the latest-wins
derived writer never deletes; the parity gate reads it HONESTLY stale) is
executed-pinned, the refusal remediation + doctor FAIL detail NAME the working
escape (delete the derived TF dirs + re-prebuild — executed-proven), and the
prune itself is the named follow-up DEC-DERIVED-GHOST-BUCKET-PRUNE-001.
READ-side gate parity only — the prebuild writer / resamplers / bad_tick.py /
stored bytes untouched; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no
lake/schema/scope/vocab/rate change. This header carries the pointer, never
the story — the full record lives in the §1 Active-branch blurb +
MIGRATION.md #1932.)
Earlier: 2026-07-17 (DEC-FEED-REGISTRY-COMMON-001,
MIGRATION.md #1903, branch `claude/feed-registry-common` — the COMMON
N-feed (multi-vendor) registry lands at its leaf home `brutex/feeds/`:
PR #1570's `VendorPullProfile` + instance `VendorProfileRegistry`
adopted-with-relocation (DEC-VENDOR-PROFILE-REGISTRY-001 #1833
supersedes-on-land — PR #1570 still OPEN, wording to be coordinated;
the feed-vendor track's DEC-FEED-INTERFACE-001 #1782 package home
survives, its static-dict registry superseded-in-part) + the NEW
data-driven `registry_build.build_default_registry` (Groww registers by
DEFAULT as the primary; extras ONLY via explicit config; junk fails
loud) + the FIRST production consumer — `build_headless_pipeline` gains
kw-only `registry=` and resolves `registry.primary()`, carrying the
vendor token + typed write domain onto `HeadlessPipeline` and threading
`vendor=` into `run_full_pipeline`. Refusal laws (typed, before any
state lands): dup token · overlapping write-domains BOTH directions ·
SECOND PRIMARY · shared rate/account stacks. DEFAULT-INERT: Groww-only
⇒ byte-identical (real-sequencer report-dict parity pinned); the write
domain is CARRIED, consulted by NOTHING (writer binding stays gated).
NO second live vendor ships — operator-gated on creds + a servability
probe; the test fixture is the synthetic `testfeed`. ENGINE UNCHANGED —
NIFTY/BANKNIFTY/SENSEX; no lake/schema/scope/vocab/rate change. This
header carries the pointer, never the story — the full record lives in
the §1 Active-branch blurb + MIGRATION.md #1903.)
Earlier: 2026-07-17 (DEC-MOVERS-DAILY-FRESHNESS-001, MIGRATION.md #1931,
branch `claude/movers-daily-freshness` — the `/movers` DAILY arm gains the
VERIFY-EQUALITY freshness guard (the named follow-up of
DEC-MOVERS-STORED-FRESHNESS-001 #1795a, which guarded only the INTRADAY
stored-TF path): a stored derived `1day` day-row serves ONLY when it equals
the in-memory `resample_1m_to_daily` of the day's UNCUT 1-minute base (the
SAME production derivation the prebuild binds — imported, never re-minted;
the #1795a equality law realised in the bar model); stale/MISSING ⇒ the
just-computed 1m-derived daily bars + ONE capped
`movers.stored_stale_fallback` WARN on the SHARED state; base-absent /
date.max ⇒ the disclosed fail-open serve-stored (the executed repro — 1day
served a stale ₹100.00 beside a fresh 1minute ₹200.00 after a 1m append
with no re-prebuild — is CLOSED); `latest_movers_date` resolves from the
1-MINUTE base (names-only max-month enumeration + one month-bound DuckDB
MAX) unioned MAX with the kept 1day scan, so a post-prebuild 1m append
moves the movers default day (a 1day-only legacy lake keeps its latest
available day); + two folded MINOR display fixes — `compute_snapshot`'s
`change` honors its None-on-zero-baseline docstring contract, and
`/api/v1/movers` validates `?interval=` against the canonical
`LAKE_INTERVAL_LABELS` vocabulary (unknown token = a clean 400 naming the
parameter BEFORE any lake read on all three tabs — the false "No futures in
the lake yet"/"No spot data" junk-interval notes are dead). Fresh
(correctly-prebuilt) lakes byte-identical (parity-pinned); named follow-up
DEC-MOVERS-BASELINE-FRESHNESS-001 (the prior-day baseline seam —
deliberately not expanded). READ-side dashboard only — ENGINE UNCHANGED —
NIFTY/BANKNIFTY/SENSEX; no lake/schema/scope/vocab/rate change. This header
carries the pointer, never the story — the full record lives in the §1
Active-branch blurb + MIGRATION.md #1931.)
Earlier: 2026-07-17 (DEC-SPOT-DERIVE-TS-RANGE-HARDENING-001,
MIGRATION.md #1919, branch `claude/spot-derive-ts-range-hardening` —
out-of-`datetime`-range stored TIMESTAMPS (e.g. a planted 2**62-µs stamp —
~year 148108, within i64, past `datetime.max`) are REJECTED at the SPOT
derive read boundary: the NEW COMMON
`price_sanity.validate_frame_ts_datetime_range` gate (one vectorized i64
min/max pass over the ts column's physical view — the probe itself can
never panic) runs in BOTH `make_lake_read_1m` + `make_lake_read_1m_window`
BEFORE the price gate, raising the quarantining `ResampleInputError`
deterministically — pre-fix such a stamp survived the vectorized resample
and PANICKED the Rust→Python conversion at `frame_to_candle_bars`'s
`to_list()` as `pyo3_runtime.PanicException`, a BaseException past every
`except Exception` (executed repro: ONE poisoned symbol aborted the WHOLE
crossverify prebuild leg — every later symbol skipped); the THREE SPOT-path
catchers gain the #1594 round-18 escape guard as defense-in-depth
(`gated_prebuild._per_symbol_data_errors` + `prebuild_leg`'s per-symbol
quarantine + `lake_heal._rebuild_tf_months` on the existing
`_SWEEP_GUARD_ERRORS`; KeyboardInterrupt/SystemExit still propagate).
PLUS the bars-tree `glob=False` metachar parity sweep (the #1594 round-15
law, nine explicit-path reads incl. the ENGINE feed mouth
`data/catalog.py get_bars` — executed repro: a `NSE-X[12]` dir beside real
`NSE-X1`/`NSE-X2` siblings silently served the SIBLINGS' bars as that
symbol) + `[`/`]` join `partition._FORBIDDEN_SYMBOL_CHARS` (the writer
refuses to mint such dirs; no legitimate vendor symbol carries a bracket —
fixture-verified). Both defects PRE-EXISTING, executed-repro'd at merge
base ecb37cc4 fail-before / pass-after; healthy-lake byte-identity pinned +
the frozen engine goldens pass UNMODIFIED. READ-boundary hardening only —
ENGINE UNCHANGED on clean feeds — NIFTY/BANKNIFTY/SENSEX; no
lake/schema/scope/vocab/rate change. This header carries the pointer,
never the story — the full record lives in the §1 Active-branch blurb +
MIGRATION.md #1919.)
Earlier: 2026-07-16 (DEC-LAKE-RUST-WAVED-HARNESS-001, MIGRATION.md #1916 —
the ADR-0020 Wave-D POISON-CORPUS DIFFERENTIAL harness (slice G9): a reusable
27-case poisoned-lake corpus + a two-implementation differential runner with the
Python-vs-Python identity self-test — the mechanical gate the future
`lake_invariants`/`lake_heal` Rust flip must pass; tests + docs ONLY, ZERO
brutex/ source change. ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX. The per-item
record lives in the §1 Active-branch blurb + MIGRATION.md #1916 — this header
carries the pointer, never the story.)
Earlier: 2026-07-16 (DEC-LAKE-RUST-STATS-MANIFEST-001, MIGRATION.md #1914 —
ADR-0020 Wave C row 4 + the row-5 rider: the stats-manifest shard RENDER
re-hosted on `rust/brutex-lake` behind the default-OFF
`BRUTEX_LAKE_RUST_STATS_MANIFEST` seam (ints + isoformat-strings-only FFI;
burn-in bit-compare + ship-the-oracle autorevert; NO version guard — stdlib
JSON has no wheel lockstep, a documented deviation vs PR-1) + crate-internal
`option_partition_stem`/`validate_year_month` partition twins (Python
partition.py byte-UNCHANGED — the ADR net-loss ruling); flag OFF is
byte-identical + crate-untouched (spy-pinned); the frozen render_shard golden
is the cross-language byte pin (cargo AND pytest, executed). READ-accelerator
only — ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX. The per-item record lives in
the §1 Active-branch blurb + MIGRATION.md #1914 — this header carries the
pointer, never the story.)
Earlier: 2026-07-15 (DEC-FILL-WORST-CASE-ONLY-001, MIGRATION.md #1825 /
ADR-0023, branch `claude/fill-worst-case-only` — WORST-CASE IS THE ONLY FILL
MODEL by operator directive: the interim PRECISE opt-out idea is DEAD —
the absorbed PR #1522 FillMode-mode design never shipped; the mode axis is gone
(`FillMode.PRECISE` · `FillPolicy`/`LevelFill`/`SameBarTie` · `--fill-mode` ·
the `BRUTEX_FILL_MODE` knob — a SET knob now refuses loudly); the adverse-max
law is HARDCODED (entry at the fill bar's adverse extreme, SL/TP at the
with-the-level clamp, forced/EOD/session-gap at the decided bar's adverse
extreme, same-bar tie always SL-first); `canonical_trade_params` emits
`{"fill_mode": "worst_case"}` UNCONDITIONALLY (anti-aliasing vs the frozen
precise-era run_ids — the re-locked `_GOLDEN_RUN_ID` is stable across the
absorption); precise-era manifests/store generations
refuse loudly naming the DEC ("re-run the sweep"); volume-0 bars reject
UNIFORMLY on both translation executors (the futures R15 fill-by-default
clause consciously superseded — survivorship tilt recorded); the
adverse-levy CEIL rounding is now PERMANENT (COSTS_VERIFIED §5 regenerated);
every golden re-locked with in-file hand-derivations, the vocab fixture
byte-untouched (empty diff pinned). INTERIM HONESTY: until PR-B's 1-minute
execution grid lands (its OWN DEC+ADR), the adverse extreme is the
DECISION-TF bar's — strictly MORE pessimistic than the operator's
next-1m-bar target law, never optimistic. ENGINE SEMANTICS CHANGED BY
OPERATOR DIRECTIVE (the ONE sanctioned exception to the standing posture) —
scope/vocab/rate/lake-schema UNCHANGED; NIFTY/BANKNIFTY/SENSEX; this header
carries the pointer, never the story — the full record lives in the §1
Active-branch blurb + MIGRATION.md #1825.)
Earlier: 2026-07-16 (DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001,
MIGRATION.md #1897, branch `claude/lake-read-nonfinite-ohlc-guard-v2` —
non-finite OHLC (inf/-inf/NaN) is REJECTED at BOTH lake-read boundaries:
`arrow_table_to_bars` gains the explicit finiteness gate (an inf-HIGH bar
previously served `Decimal('Infinity')` into the engine SILENTLY — the
positivity + ordering checks pass vacuously on inf) + the
`(ValueError, ArithmeticError)` re-wrap belt (a NaN price's raw
`decimal.InvalidOperation` previously escaped `except ValueError` and
aborted the whole read); the vectorized `price_violation_expr` gains the
per-column `~is_finite` arm (the `high ∈ {+inf, NaN}` blind spot) with the
scalar twin widened `isnan` → `not isfinite` — scalar == vector over the
FULL float plane, CLOSING the DEC-DASHBOARD-READ-SCREEN-001 round-2 named
ENGINE-side follow-up; `make_lake_read_1m_window` (the heal-recall /
post-pull / crossverify `1day` window reader) runs the strengthened gate
for the FIRST time (Phase-1 finding #12) on the window-cut frame;
`ArithmeticError` joins the prebuild per-symbol quarantine set (a NaN
partition quarantines the SYMBOL, never a run abort). Both defects are
PRE-EXISTING, executed-repro'd by the assurance session on planted/corrupt
STORED data (the vendor parse rejects non-finite via `to_price` — verified);
fails-before proven at merge-base 6b9947d9; healthy-lake byte-identity
pinned + the frozen engine goldens pass UNMODIFIED. READ-boundary hardening
only — ENGINE UNCHANGED on clean feeds — NIFTY/BANKNIFTY/SENSEX; no
lake/schema/scope/vocab/rate change. Named follow-up:
DEC-DAILY-WINDOW-BADTICK-001 (bad-tick on the window reader). This header
carries the pointer, never the story — the full record lives in the §1
Active-branch blurb + MIGRATION.md #1897.)
Earlier: 2026-07-15 (DEC-STAGED-SEQUENCER-SEAMS-001, MIGRATION.md #1900,
branch `claude/staged-sequencer-seams` — the STAGED SEQUENCER SEAMS
(dual-vendor orchestration WS-B, per the frozen ADR-0021 design contract +
the operator rulings relayed via the coordinator — not a direct-user approval
in this session): the ONE `run_full_pipeline` gains ADDITIVE optional staged
callables (`run_spot_daily` · `run_prebuild_spot` · `run_futures` ·
`run_prebuild_futures` · `run_options` · `run_greeks` + the `vendor`
stage-event token) over the canonical order SPOT-DAILY → SPOT-1M →
SPOT-PREBUILD → METADATA → FUTURES → FUTURES-PREBUILD → OPTIONS → GREEKS,
with the ABSENT-vs-FAILED contract (a None callable = an ABSENT stage —
never invoked, vacuously settled under the DISTINCT `absent` token; the
strict gate never withholds Greeks over absent stages; the
second-vendor-shaped PARTIAL-absent parity case pinned), per-stage
fail-closed gates (`stopped_after` sequencer-minted from stage position),
the per-vendor-stage market-hours belt, and the `pipeline.stage.*` events
(§10); stage callables are typed at the NEW STRUCTURAL `StageOutcome`
protocol (leaf `dashboard/stage_outcome.py` — the five shipped Groww report
shapes are satisfying instances via additive `.ok` properties the
`spot_phase_ok`-family gates now delegate to); the F&O spine gains the
additive `step_filter` bucket selection (futures-only / options-only stage
invocations reuse the ONE spine — filtered buckets honestly ABSENT from
execution + NAMED `contracts_step_filtered`, never fake-settled;
`contracts_selected` stays the pre-filter selection-grammar count); NEW
leaf `data/write_domain.py` ships the Pin D1-1 primitive
(`WriteDomain` = allowed-subtree SET + the structural whole-`feeds/`-prefix
primary exclusion, registration-independent; `assert_write_domain` = the
typed cross-domain refusal, mount-shape law + the round-6 in-lake
`feeds`-component swap rail ported at the assurance NG4E-2 MAJOR fold
(fails-before-pinned); the component compare is
byte/case-SENSITIVE, a documented + pinned defense-in-depth limit;
negative-pin battery incl.
nested-root + unregistered-feeds fixtures; the NG4E-1 maintenance-lane
scoping + the NG4E-2 restore-ordering law documented SETTLED per the
coordinator-relayed operator ruling of 2026-07-15). ALL-None
defaults BYTE-IDENTICAL (frozen-parity + origin/main A/B pinned); NO
production caller binds the new seams — the binders are WS-C/WS-D. ENGINE
UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no lake/schema/scope/vocab/rate change.
This header carries the pointer, never the story — the per-item record
lives in the §1 Active-branch blurb + MIGRATION.md #1900.)
Earlier: 2026-07-10 (DEC-NIFTY-PRICE-SOURCE-AXIS-001, MIGRATION.md #1681 —
a NEW `PriceSource` cell-coordinate sweep axis for the price source of VWAP +
EMA20/EMA200 over {open,high,low,close,hlc3}, mirroring the `fib_anchor` axis;
DEFAULT-OMITTED (baseline (hlc3,close) → existing run_ids BYTE-IDENTICAL, no
indicator_version/crate bump, no Rust change — Stage-1A stays Python). The
per-round record lives ONLY in the §1 Active-branch blurb + MIGRATION.md #1681
— this header carries the pointer, never the story. ENGINE UNCHANGED —
NIFTY/BANKNIFTY/SENSEX.)
Earlier: 2026-07-15 (CLUB Q merge vehicle, branch
`integration/club-q-dashboard` — the file-disjoint fold of THREE PRs as ONE
merge event off main tip d6ac9f85: #1434 memory/cache + RAM-residency
hardening pack (DEC-MEMORY-FOOTPRINT-HARDENING-001, MIGRATION.md #1721) ·
#1433 brutexd static/health Rust front, default-OFF `BRUTEX_RUST_FRONT`
(DEC-RUST-FRONT-STATIC-HEALTH-001, MIGRATION.md #1733) · #1552 operator
single-portal refold + the relocated #1749 cascade render
(DEC-OPERATOR-PORTAL-REFOLD-001, MIGRATION.md #1796). All three member entry
numbers were verified FREE on the live main tip (ceiling #1794) and land
verbatim — no renumber (DEC-IDs are the stable handles per
DEC-LEDGER-RENUMBER-001). READ-side dashboard caches / display templates / a
default-OFF serving sidecar only — ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX;
no lake/schema/scope/vocab/rate change. This header carries pointers, never
round narration; each member's full record lives in its §1 Active-branch
blurb + its MIGRATION.md entry (#1433 carries no §1 blurb of its own — its
record lives in MIGRATION.md #1733).)
Earlier: 2026-07-15 (DEC-OPTIONS-PREBUILD-RETIRE-001, MIGRATION.md #1831,
branch `claude/options-prebuild-retire` — derived-timeframe prebuild RETIRED
for OPTIONS (CE/PE): option data stays 1-minute-native per the operator ruling
of 2026-07-15 as relayed via the coordinator (ADR-0021 RULING B, draft
PR #1555) — supersedes-IN-PART DEC-PIPELINE-AUTOPREBUILD-001 (#1145), the
OPTIONS arm only (the spot + futures prebuild law stands byte-identically).
BOTH producers gated (the pipeline's sequential + parallel paths — the gated
spine reports the honest by-design no-op, never a fake "0 contracts"; the
lake-heal recall's daily arm — counted + ONE capped batch log) and the READ
side gated with the 1-minute fallback (the movers CE/PE prev-close/prev-OI
baselines skip the frozen 1day and walk the prior session's 1-minute; the
doctor derived-freshness verdicts exclude CE/PE), all via the ONE COMMON
`fno_candle_lake.is_option_contract_coords` predicate; already-stored derived
option TFs stay INERT (never deleted — disk reclaim is an optional operator
action). READ/producer gating only — ENGINE UNCHANGED —
NIFTY/BANKNIFTY/SENSEX; no lake/schema/scope/vocab/rate change. This header
carries the pointer, never the story — the per-item record lives in the §1
Active-branch blurb + MIGRATION.md #1831.)
Earlier: 2026-07-15 (Club G docs merge vehicle, branch
`integration/club-g-docs` — the docs-only fold of TWO PRs as ONE merge
event off main tip d6ac9f85: #1506 DEC-GDFL-VENDOR-DOCS-001 (MIGRATION.md
#1780 — the summarize-only GDFL vendor-docs archive + the definitive
granularity/depth verdict at docs/vendors/gdfl/) + #1508
DEC-GDFL-INGEST-DESIGN-001 (MIGRATION.md #1781 — the GDFL 1-second L1
snapshot ingest/read DESIGN doc; design only, zero adapter code). The only
union files are docs/MIGRATION.md + this CLAUDE.md; both members' ledger
numbers (#1780/#1781) are free on the landing main — kept unchanged.
DOCS-ONLY — ZERO brutex/ source change; ENGINE UNCHANGED —
NIFTY/BANKNIFTY/SENSEX; no scope/vocab/rate change. This header carries
pointers, never the story; each member's full record lives in its §1
Active-branch blurb + its MIGRATION.md entry.)
Earlier: 2026-07-15 (CLUB R2 merge vehicle, branch
`integration/club-r2-crates` — the crate-disjoint fold of THREE PRs as ONE
merge event off main tip d6ac9f85: #1469 DEC-GREEKS-RUST-BATCH-001
(MIGRATION.md #1892, chain #1724 → #1795 → #1892 — the last hop at the
2026-07-16 club CDR2 fold, bare vs club-D's #1795a–#1795d block) · #1435
DEC-RUST-DATALAKE-IO-ROADMAP-001 (#1891, chain #1726 → #1796 → #1832 →
#1891 — the last hop at the club CDR2 fold, club-D took #1832) · #1454 DEC-FEED-RUST-CORE-001 (#1797, chain #1725 → #1741 → #1797).
Three INDEPENDENT crate trees (`rust/trading-core/src/greeks/batch.rs` ·
`rust/brutex-lake/` · `rust/brutex-feed/`), every Python seam default-OFF /
INERT (`BRUTEX_GREEKS_RUST_BATCH` · `BRUTEX_LAKE_RUST_RENDER` ·
`BRUTEX_FEED_RUST`); the three colliding ledger rows were renumbered to the
free consecutive block #1795–#1797 vs the live-main tail #1794 (chains
in-entry; DEC-IDs are the stable handles per DEC-LEDGER-RENUMBER-001).
#1433 (brutexd) deliberately NOT folded — joins post-refresh. ENGINE
UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no lake/schema/scope/vocab/rate change.
This header carries pointers, never round narration; each member's full
record lives in its §1 Active-branch blurb + its MIGRATION.md entry.)
Earlier: 2026-07-13 (DEC-GDFL-INGEST-DESIGN-001, MIGRATION.md #1781 —
the GDFL 1-second L1 snapshot ingest/read DESIGN doc at
`docs/vendors/gdfl/INGEST_DESIGN.md` — DESIGN ONLY, zero adapter code, the
build gated on the operator confirming the GDFL purchase: a new `snap1s/`
lake tree (raw-vendor-ticker keyed, keep-first `(timestamp, seq)`, a
byte-identity re-ingest law), the CSV parse taxonomy + ticker AMBIGUITY law
(raw archival never blocked by resolution; never a guessed expiry), the
read-time 1s collapse + LTQ>0-only 1m derivation into provenance-isolated
`-G1S` symbols under bars/ (the suffix fails the right-anchored F&O grammar,
so every parse-derived surface skips or honestly-buckets it), /db surfaces
mirroring the expired-options pager, a 44-row edge matrix + the
honest-limits/UNVERIFIED register, and the two-panel reconciliation appendix;
raw GDFL data NEVER committed + `snap1s/` and the derived `*-G1S` bar dirs
EXCLUDED from off-box backup until the licensing read (a named
DEC-BACKUP-OFFBOX-001 divergence). DOCS ONLY — zero source change; ENGINE
UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no scope/vocab/rate change; the per-item
record lives in the §1 Active-branch blurb + MIGRATION.md #1781 — this
header carries the one-line summary, never the story.)
Earlier: 2026-07-13 (DEC-GDFL-VENDOR-DOCS-001, MIGRATION.md #1780 —
the SUMMARIZE-ONLY GDFL vendor-docs archive + the definitive granularity/depth
verdict at docs/vendors/gdfl/: GDFL's finest history granularity is 1-second L1
SNAPSHOT data (its vendor API enum token is TICK — irregular within-second
cadence, NOT a fixed one-row-per-second grid, NOT sub-second exchange
tick-by-tick; the 1-second class is the CEILING for Indian retail data per the
prior verified 2026-07-09 tick-feed research), NO 1-second OHLC bar periodicity
exists anywhere in the API, and the days-scale (unconfirmed figure — never
years-scale on any reading) snapshot-history window makes a
2022→now 1-second-class BACKFILL impossible — forward capture only. Every fact
evidence-tiered; ZERO verbatim vendor prose / vendor source trees / raw sample
data committed (the coordinator's summarize-only licensing ruling — PyPI
re-fetch URLs + SHA256 digests pinned instead); the 63-blocked-site-URLs
capture honesty + the operator manual-upload list live in the archive's
MANIFEST.md; the extract/ingest design is deliberately SPLIT OUT under
DEC-GDFL-INGEST-DESIGN-001 (coordinator-reserved, not yet in the ledger).
DOCS-ONLY — ZERO brutex/ source change; ENGINE UNCHANGED —
NIFTY/BANKNIFTY/SENSEX; no scope/vocab/rate change; the per-item record lives
in the §1 Active-branch blurb + MIGRATION.md #1780 — this header carries the
one-line summary, never the story.)
Earlier: 2026-07-11 (DEC-OPERATOR-HEALTH-TRUTH-001, MIGRATION.md #1747 —
the operator-page HEALTH TRUTH pack: (1) the heartbeat pull slot's unconditional
green "last pull completed cleanly" on `flow_state === 'done'` is KILLED — the
`/api/v1/pull/rate` payload gains the ADDITIVE `last_pull` OUTCOME block (NEW pure
`brutex/dashboard/pull_outcome.py` classifying the job slot's RETAINED pull
terminal — `RetainedPullState`, surviving a later sweep on the shared slot —
plus the pull-id-CORRELATED verdict holder, colours via the ONE
`verdict_banner.banner_for` authority) so a HALTED pull (disk-full / OOM /
market-hours mid-run OR start-gate / quota / breaker)
renders its NAMED degraded state — even after a later sweep, and never coloured
by an earlier run's stale verdict (the hostile-review FIX ROUND, per-finding
record in MIGRATION.md #1747) —
and green appears ONLY on the classified clean (the unclassified `completed`
never claims "cleanly"); the formerly-swallowed `failing`/`local` flow states
render honestly. (2) ONE unified HEALTH HUB — NEW `GET /api/v1/health/hub` +
`brutex/dashboard/health_hub.py` + the `bx-health-hub.js` BXPoll widget (one
poller per endpoint, the #1720 law) — folds nightly-chain (`report.json`) ·
backup-dead-man (the PURE `evaluate_deadman` reused verbatim, read-only) ·
doctor-daily · mutation · pull lanes green/red from DURABLE LOCAL state; the
HONESTY LAW: a lane with NO local signal reads the explicit "not wired"
(doctor/mutation are GitHub-Actions-only, no local artifact), corrupt state
reads a NAMED `unknown` degrade — NEVER a fabricated green, never a 500. Honest
O-envelope: TTL-memoised file lanes (one bounded scandir + three tiny JSON reads
per refresh; never a lake scan). READ-side dashboard/observability only —
ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; the per-item record lives in the §1
Active-branch blurb + MIGRATION.md #1747.)
Earlier: 2026-07-14 (DEC-BULK-SPOT-COMPLETENESS-FLAG-001, MIGRATION.md
#1794 (renumbered #1792 → #1793 → #1794 across the 2026-07-14
merge-reconciles — main took #1792 for
DEC-SPOTONLY-PREBUILD-EMPTY-EXCLUDE-001, then #1793 for
DEC-SETTLE-EVIDENCE-ADOPTION-001), branch `claude/bulk-spot-completeness-flag` — the bulk whole-universe
spot pull arms `detect_completeness_gaps`: `pull_all_spot`'s ONE spine call
now threads `detect_completeness_gaps=not dry_run` (the cli.py `cmd_fetch`
idiom), so the #1031 interior-gap / trailing-truncation demotions run on
EVERY daily production path (nightly · ▶ Pull Spot + auto-pull · /pull-all ·
retry-failed · the crossverify pull legs) instead of being dead on all of
them — closing #1774's honest-envelope item (i). The spine's `is_index` gate
keeps the ~750 illiquid CASH names observe-only on the gap/truncation class
(the intrinsic ALL_FILTERED demotion stays deliberately un-index-gated);
dry-runs stay
double-protected (flag False + record-inert None ledger); demotions are
bounded (settle SKIPPED at threshold 3, remediation `brutex
reopen-skipped`). ONE call site + docs + tests — the detectors + spine are
byte-untouched; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no
lake/schema/scope/vocab/rate change. This header carries the pointer, never
the story — the full record lives in the §1 Active-branch blurb +
MIGRATION.md #1794.)
Earlier: 2026-07-14 (DEC-SETTLE-EVIDENCE-ADOPTION-001, MIGRATION.md
#1793 — the measured-settle evidence adoption channel: the #1775 probe's
`--summarize --emit-evidence` → committed `docs/groww/probe/settle_evidence.json`
(schema 1, reviewed beside its transcripts) → the validated three-tier
resolution env > evidence > the conservative 17:30 default (the tier-1
short-circuit preserves `--settle-ist`/env supremacy; explicit
`BRUTEX_SETTLE_EVIDENCE_PATH` junk fails loud, default-path corruption
WARNs once + falls back — the r6/r7 posture on the shared regular-file-only
loader, MOVED to COMMON `brutex/data/sidecar_read.py` with a byte-compatible
pull_intent shim); ships the CHANNEL, ships NO measured number — every
production resolution is byte-identical until the operator's evidence PR
lands (parity-pinned); ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; the
per-item record lives in the §1 Active-branch blurb + MIGRATION.md #1793 —
this header carries the one-line summary, never the story.)
Earlier: 2026-07-14 (DEC-SPOTONLY-PREBUILD-EMPTY-EXCLUDE-001,
MIGRATION.md #1792 — vendor-empty spot symbols (zero stored 1-minute bars)
classify NO-SOURCE at the prebuild read (the retyped whole-partition
`NoStored1mBarsError` arm — lake truth, never the #1779-rejected report
`no_data`), counted + named in the note/payload/CLI, `PrebuildOutcome.ok`
unaffected so the movers warm fires over the healthy majority — the #1779
withheld-run-wide residual CLOSED; default path byte-identical; ENGINE
UNCHANGED — NIFTY/BANKNIFTY/SENSEX; the per-item record lives in the §1
Active-branch blurb + MIGRATION.md #1792 — this header carries the one-line
summary, never the story.)
Earlier: 2026-07-14 (DEC-CROSSVERIFY-OWED-SENTINEL-001, MIGRATION.md
#1810 — renumbered #1792 → #1810 at the 2026-07-15 club-T fold (main took
#1792 for DEC-SPOTONLY-PREBUILD-EMPTY-EXCLUDE-001; DEC-IDs are the stable
handles per DEC-LEDGER-RENUMBER-001) — branch `claude/crossverify-owed-sentinel` — the durable cross-verify
OWED-day sentinel closing the CONFIRMED umbrella-assurance MAJOR-1: a
one-click Pull-Spot day whose box dies/sleeps mid-prebuild BEFORE the
crossverify tail silently skipped the day's same-day S3 publish (both
catchup scans iterate days that HAVE a journal — a never-started day was
invisible). NOW: both launchers (dashboard spot-only `_work` + `brutex
pull-all-spot`) write a durable owed marker at pull-tail-commit ENTRY (NEW
`brutex/crossverify/owed.py` — one atomic JSON per day under
`<lake>/.crossverify/owed/`; names-only scan, loader-disciplined diagnostic
reads, best-effort-loud, kill-switch coherent), the runner's NEW step D3
COMPLETES a never-started owed prior day on the next run with the manual
`--day` machinery verbatim (the `CrossverifySeams.run_day` recursive rerun
— `skip_pull` + `catchup_days=0`; journal idempotency ⇒ zero double-puts),
the marker clears ONLY at the day's completed material verdict
(crash-convergent; Muhurat/non-trading honest skips clear, deferrals +
failures keep), and owed days read LOUD on the /universe panel via the
EMIT-ON-PRESENT `owed_days` progress field (no-owed wire byte-identical,
pinned). Sidecar/launcher/read-side only — ENGINE UNCHANGED —
NIFTY/BANKNIFTY/SENSEX; no lake/schema/scope/vocab/rate change. This
header carries the pointer, never the story — the full record lives in
the §1 Active-branch blurb + MIGRATION.md #1810.)
Earlier: 2026-07-13 (DEC-SWEEP-LOCK-PULL-CONCURRENCY-001, MIGRATION.md
#1785, branch `claude/sweep-lock-pull-concurrency` — the dashboard sweep route
+ the nightly sweep leg stop holding the LAKE pull flock through the sweep:
CLI parity — cmd_sweep_all's OWN G10 lake read-guard (SH, held across the bar
PRELOAD only) + G6 sweep single-flight own the sweep-vs-pull exclusion — fixing
the CONFIRMED G10 self-conflict BLOCKER (flock is per open file description, so
the job/chain-held EX made the same-process G10 SH acquire raise LakeBusyError
at EVERY dashboard/nightly sweep preload since G10 landed at 3f32b3e; CI-blind
via faked seams) AND delivering the operator requirement that the daily pull +
crossverify→S3 run CONCURRENTLY with a multi-day sweep's compute phase.
SUPERSEDES DEC-SWEEP-NIGHTLY-AUTOMATION-001 (#1612)'s lake-lock
held-through-SWEEP clause. Run identity frozen at preload; byte-identity
proven by the executed concurrency regression. Lock COMPOSITION only — ENGINE
UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no lake/schema/scope/vocab/rate change.
This header carries the pointer, never the story — the full record lives in
the §1 Active-branch blurb + MIGRATION.md #1785.)
Earlier: 2026-07-13 (DEC-PULL-SCOPED-ALL-ONECLICK-001, MIGRATION.md
#1779 — the one-click scoped-ALL SPOT pull: the additive `scope=spot-only`
token on the ONE `/pull-pipeline` launcher (default requests byte-identical,
pinned) + the NEW `brutex pull-all-spot` CLI over
`build_headless_pipeline(scope=…)`; the F&O phases run as no-op reports
through the UNCHANGED sequencer (note SURGERY — "F&O skipped by design
(spot-only scope)", never a fake "0 expiries"); explicit "Greeks: not run —
no options in this pull" text; the fail-soft crossverify tail (production
default = the REAL #1776 producer, fired strictly OUTSIDE our held pull
single-flight); the CLI `--auto` gate with the UNMEASURED-conservative
17:30 IST settle default (`BRUTEX_PULL_ALL_SETTLE_IST`, floor 15:35) +
Muhurat whole-day deferral + the composed mid-run Muhurat belt — the guard
module byte-untouched; a persisted spot-only intent resumes SPOT-ONLY, never
widened; since the round-5 fold the button SELF-ARMS the daily post-market
auto-pull (NEW `dashboard/auto_pull_scheduler.py` — first click = run now +
arm forever; the same gate + launcher reused, once-per-day law, durable
`.pull_all_spot_auto.json`, disarm chip; the CLI `--auto` + cron lane is the
headless alternative); since the round-6/round-7 folds the durable state
loads are corruption-hardened for EVERY shape, non-regular inodes included
(never crash OR hang the boot — the round-6 #1776 M1 decode fold + the
round-7 #1614 `O_NONBLOCK`+`S_ISREG` regular-file-only loader with the
fstat-on-fd size cap) and the
combined pipeline button is SPLIT into ▶ Pull Spot (auto-arming) + ▶ Pull
F&O (the NEW manual `scope=fno-only` mirror — spot no-op by note surgery,
metadata/historical/Greeks real, F&O-only prebuild + warm; tail + scheduler
stay Spot-exclusive, ALL FOUR ruled exclusivities negative-pinned since
round 7). ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no
lake/schema/scope/vocab/rate change; the per-item record lives in the §1
Active-branch blurb + MIGRATION.md #1779 — this header carries the one-line
summary, never the story.)
Earlier: 2026-07-13 (DEC-O1-GUARANTEE-HARDEN-001, MIGRATION.md #1791 —
the O(1)-guarantee verification + hardening pass: `docs/O1_GUARANTEE_AUDIT.md`
becomes the AUTHORITATIVE honest per-path complexity classification (51 rows —
the six Golden-Rule-#5 engine hot paths · the DB/lake dashboard reads · the
frontend; the anti-illusion rule applied to every row — no bare O(1) claim
where the FIRST read is O(files)/O(rows)); the reported M4-Pro
ResultsStore.get "1.87×" strict-tier failure is triaged
VERIFIED-NOT-A-REGRESSION (a one-line dict probe, measured flat
51.7/53.5/51.7 ns at 1k/10k/100k rows; the #1301 cache-tier 3.0× re-gate
already absorbs the dict-probe physics band; NEW 100k-deep pin); FIVE
hardenings land (the last 3 unbounded-body poller templates ride the NEW
COMMON `BXUI.fetchTextBounded`; the oneclick timeline gains a dup-tick
skip token + a NAMED newest-500 render cap + the 200 ms search debounce;
universe.html's seven interval loaders ride ONE bounded latest-wins helper;
the /db registry pagers' filtered COUNT gains an O(1)-on-repeat memo on the
NEW `registry_revision` stat token; the leaderboard + oneclick-runs O(dirs)
freshness probes are TTL-bounded by the NEW `RevisionProbeMemo` — a ≤1 s
DISCLOSED staleness window, kill switch `BRUTEX_REVISION_PROBE_TTL_S=0`)
+ EIGHT bench gates close the audit's missing-gate findings. READ-side
dashboard + tests + docs only; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX;
no lake/schema/scope/vocab/rate change; the per-item record lives in the
§1 Active-branch blurb + MIGRATION.md #1791 — this header carries the
one-line summary, never the story.)
Earlier: 2026-07-13 (DEC-ENGINE-SPOT-ONLY-001, MIGRATION.md #1790 —
the brute-force ENGINE is SPOT-ONLY by operator directive: the ADR-0012 /
DEC-NIFTY-DYNAMIC-STRATEGY-001 NIFTY-futures engine authorization + the
DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001 launch surface are CONSCIOUSLY
SUPERSEDED (engine surface {3 spot} ∪ {NSE-NIFTY-FUT} → {3 spot});
`is_engine_sweep_symbol` admits EXACTLY NIFTY/BANKNIFTY/SENSEX and every
production sweep entry refuses `NSE-NIFTY-FUT` loudly citing the DEC +
"the engine is spot-only; NSE-NIFTY-FUT remains lake/REFERENCE_ONLY" —
BEFORE any futures-series resolution or lake I/O; the futures resolver /
alias-aware loader / Stage-1A roll machinery / symbol_id 4 are RETAINED as
lake tooling below the gate (reachable only via the loud audit-stamped
--allow-out-of-scope research escape or recorded-work forensics); Groww
PULL/lake scope UNTOUCHED — futures ingest stays REFERENCE_ONLY; the
per-item record lives ONLY in the §1 Active-branch blurb + MIGRATION.md
#1790 — this header carries the pointer, never the story.)
Earlier: 2026-07-13 (DEC-CROSSVERIFY-WITHHELD-REGATE-001, MIGRATION.md
#1789 — the cross-verify WITHHELD-day AUTO-RE-GATE folded into the daily
run's catchup: every prior day inside `catchup_days` whose journal still
names withheld symbols is re-gated from the since-healed lake (re-export →
stage → publish the now-complete ones; the journal `withheld` map rewritten
to the honest remainder; already-published keys NEVER re-put — journal
idempotency; fail-closed gate-first; per-day LEAF journal flocks; runs
BEFORE the session gate so weekend fires re-gate a healed Friday) — closing
the runbook's "manual --day re-run" remediation for the heal-within-catchup
case and CLOSING the #1776-named follow-up DEC-CROSSVERIFY-BACKFILL-001
(id retired unused — the operator's separate historical whole-range S3
backfill idea was STOOD DOWN by ruling 2026-07-13: current-day-only S3
push); PLUS the persistent last-run failure surface — the runner persists
`<lake>/.crossverify/last_run.json` for every MATERIAL verdict (never
dry-runs, never the honest skips), `GET /api/v1/crossverify/progress`
serves it as the additive `last_run` field (threadpool, never a 500) and
the /universe panel renders it on load, so a finished failure survives
page reloads AND dashboard restarts; the retryable-exhaustion
`publish_failed` verdict gains a REAL detail (failing count + capped key
sample); the summary/CLI lines gain the `re-gated N` token (INFO now
requires recovered==0 AND regated==0). HONEST: only WITHHELD symbols
re-gate — an honest-`nothing` zero-bar day backfilled later, or a day
older than catchup_days, still needs `--day`; one month-partition read
per re-gated symbol·day. ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no
lake/schema/scope/vocab/rate change; zero new routes/env; the per-item
record lives ONLY in the §1 Active-branch blurb + MIGRATION.md #1789 —
this header carries the pointer, never the story.)
Earlier: 2026-07-13 (DEC-CROSSVERIFY-SPOT-ONLY-001, MIGRATION.md
#1788 — the cross-verify export contract AMENDED to SPOT-ONLY over the
WHOLE spot universe (operator ruling 2026-07-13): the NIFTY/BANKNIFTY
futures legs come OUT — no futures pulls, no futures CSVs; TickVault must
NOT expect futures files (absence is BY DESIGN) — and the export
enumeration widens to ALL admitted indices (~25, incl. INDIAVIX +
sectorals; the engine trio stays the structural floor) + ALL stocks (~751,
F&O-eligible AND reference-only NTM names) via the ONE
`whole_universe_spot_targets` law shared by pull AND export (export ≡ pull
by construction). INDEX policy TIERED (the approved index-policy ruling,
same-branch fix round): the STRICT count+close-edge withhold law is scoped
to the VERIFIED engine trio; the other ~22 indices ride an OBSERVE-ONLY
count law (published-with-a-counter, zero bars = honest nothing, forming
last bar still withholds) with the three-way
strict-published/observe-published/withheld split visible in the ONE
Telegram summary; promotion = probe-evidenced follow-up DEC
(DEC-CROSSVERIFY-INDEX-STRICT-PROMOTE-001). CSV byte contract (header incl. `oi`; spot empty-oi
cell) / gate binding / S3 posture + key layout / 1-minute-only law /
Muhurat-session semantics / journal-publish machinery ALL UNCHANGED; the
FUT mapper/renderer/gate/prebuild arms retained STRUCTURAL
(direct-unit-covered, unreachable from the producer, kept for possible
re-expansion); the metadata + FUT-candle pull legs, the registry read, the
G2 FUT settle-retry class DELETED; negative-pinned: a lake WITH futures
contract dirs + registry FUT rows yields ZERO `*_FNO` keys.
DEC-CROSSVERIFY-SENSEX-FUT-INGEST-001 CLOSED MOOT. ENGINE UNCHANGED —
NIFTY/BANKNIFTY/SENSEX; no lake/schema/scope/vocab/rate change; the
per-item record lives ONLY in the §1 Active-branch blurb + MIGRATION.md
#1788 — this header carries the pointer, never the story.)
Earlier: 2026-07-13 (QUAD MAX-CLUB merge vehicle, branch
`claude/quad-dashboard-lakeinv-universe-docs` — the file-disjoint fold of FOUR PRs as ONE
merge event off main tip 3f32b3ef (merged forward over main through 81052345 on 2026-07-13): #1440 dashboard lane club (DEC-DASHBOARD-POLL-HYGIENE-002
#1699 + DEC-DASHBOARD-READ-SCREEN-001 #1759 + DEC-DB-TZ-MONEYNESS-FIXES-001 #1760 +
DEC-DB-MONEYNESS-STOCK-EVIDENCE-001 #1761) · #1450 DEC-LAKE-INVARIANTS-PREINDEX-001 #1762 ·
#1288 DEC-DASHBOARD-READPATH-WEDGE-001 #1763 · #1441 docs club DEC-EAP-FEEDBACK-INDEX-001 #1764.
The three colliding ledger rows were renumbered off main's taken #1712/#1715 (DEC-IDs are the
stable handles per DEC-LEDGER-RENUMBER-001). READ-side dashboard / lake-heal SPACE / docs only —
ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX. This header carries pointers, never round narration;
each member's full record lives in its §1 Active-branch blurb + its MIGRATION.md entry.)
Earlier: 2026-07-12 (DEC-DASHBOARD-READ-HONESTY-PACK-001, MIGRATION.md
#1795a — the dashboard underlying-key READ-honesty pack (ONE umbrella entry;
the sub-IDs DEC-MAPPING-INTEGRITY-FORM-KEY-001 / DEC-MOVERS-STORED-FRESHNESS-001
/ DEC-FNO-UNDERLYING-EMPTY-GUARD-001 resolve via the #1795b–#1795d companion
headers), REFUTER ROUNDS 1 + 2 + 3 + 5 + 7 folded: the /universe mapping-integrity compare
keyed through the COMMON F&O underlying-forms resolver via an injected
plain-data `key_forms` map (hyphenated names' attempted-and-zero silent-vanish
alarm live; the round-5 CROSS-OWNER guard — a normalize-colliding sibling's
coverage can never mask an own-key attempted-and-zero — plus the MI-6
attempted-match tooth, and the round-7 materialize-once memo so a
single-shot `covered_years` iterator value can never exhaust into a
false-RED; the writer's KEYING byte-untouched — the writer file
gains only the sub-(c) blank-row isolation; default-None byte-identical), the /movers
stored-first intraday read gains the VERIFY-EQUALITY freshness guard (a stored
TF day frame is served only when it EQUALS the in-memory resample of the
1-minute base — the round-1 day-max predicate was refuted by the executed
#1031 interior-hole re-pull chain; stale ⇒ the existing 1m-resample fallback +
one capped `movers.stored_stale_fallback` WARN, dedupe key (exchange, symbol,
interval) per the #1649a precedent; the round-2 month-edge fold widens
`read_stored_day_frame` to probe the PRIOR UTC month's file for sub-05:30-IST
bucket labels on the 1st of an IST month — a fresh month-edge lake now serves
its COMPLETE stored frame instead of a permanent false-stale WARN or main's
silent bucket drop; fresh lakes byte-identical, pinned; the round-3 fold gates
the probe `day > date.min` — the `0001-01-01` OverflowError 500 closed,
route-pinned; round-7: the equality wording corrected to the executed
truth — value-exact, dtype-INSENSITIVE polars `.equals`, drift-pinned;
daily arm = named
follow-up DEC-MOVERS-DAILY-FRESHNESS-001), and `primary_fno_underlying` made
total for the blank-symbol degenerate (IndexError → self-key; blank rows
skipped + counted at the kind-map AND the metadata grid — the round-1-falsified
"inert key" metadata seam closed, `_first_nonempty_over_forms` made total;
round-2 forensic completion: the pre-fix blank cells ALSO stopped the full
pipeline after metadata via `metadata_phase_ok`). READ-side dashboard only —
ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; the per-item record lives in the §1
Active-branch blurb + MIGRATION.md #1795a.)
Earlier: 2026-07-12 (DEC-CROSSVERIFY-PRODUCER-001 +
DEC-CROSSVERIFY-S3-WRITE-POSTURE-001 + DEC-CROSSVERIFY-DAY-DERIVE-001,
MIGRATION.md #1776–#1778 — the daily TickVault cross-verification PRODUCER:
one zero-touch post-close chain — priority pull (ALL spot + NIFTY/BANKNIFTY
futures, NO options) → completeness gate (FAIL-CLOSED on the sibling #1774
module) → day-scoped never-stamping prebuild → the pinned per-symbol CSV
byte contract → idempotent journal-keyed S3 publish to
`tv-prod-cold/crossverify/*` (PutObject-ONLY — BruteX's FIRST sanctioned AWS
WRITE surface; the SSM read-only pledge UNCHANGED; boto3 via the NEW
optional `crossverify` extra, never installed in CI) — surfaced as `brutex
crossverify {run,install,uninstall,status}` (launchd label
`com.brutex.crossverify`, `--at` REQUIRED probe-decided; trading-day-aware
journal deadman) + the /universe dashboard button riding the SAME runner
(cross_process_lock=False — the runner's own lock trio; market-hours law
runner-owned at step E). Correct-or-absent: a short/settling file is
WITHHELD + CRITICAL-alerted, never wrong bytes; Muhurat days skip entirely
(no files, both sides no-op); a missing index day = exit 2 (the real gate
reads `count_short`; the INDEX-`no_today_bars`→BLOCKED arm is defensive
totality — the post-#1503 integration-moment fold in #1776). HONEST
envelope: vendor same-day availability UNVERIFIED (the operator's 5-day
settle probe decides `--at`); BSE_EQ/BSE_FNO structural-only; GREEKS N/A
(the options-only stage on an options-excluding pull); movers warm =
revision propagation only; named follow-ups
DEC-CROSSVERIFY-SENSEX-FUT-INGEST-001 (BSE-FNO probe-gated) +
DEC-CROSSVERIFY-PREEMPT-002 (true preemption — job-cancel dependency).
ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no lake/schema/scope/vocab/rate
change; the per-item record lives ONLY in the §1 Active-branch blurb +
MIGRATION.md #1776 — this header carries the pointer, never the story.)
Earlier: 2026-07-12 (DEC-PULL-TODAY-COMPLETENESS-GATE-001, MIGRATION.md #1774 —
the pre-settle TODAY minute-completeness gate: a post-close pull's covers-today
window WITHHOLDS today's still-settling bars from the keep-first lake (the #837
forming shape would freeze forever) and defers streak-EXEMPT re-pullable FAILED
(`TODAY_INCOMPLETE` — never bumps toward SKIPPED; heals at the next nightly
PROVIDED the vendor has settled by then — the probe-measured settle time +
margin grounds that schedule)
until the per-class criteria pass (INDEX count+close-edge · CASH/FUT/CE-PE
shape checks; FUT count observe-only; legit vendor-empty NEVER demotes —
pinned); armed at ALL FOUR production pulls incl. the bulk `pull_all_spot`
behind the fail-loud `BRUTEX_TODAY_COMPLETENESS_GATE` switch resolved once per
pull; non-today windows byte-identical (pinned); + the companion read-only
post-close settle probe measuring the UNDOCUMENTED vendor EOD settle time
(DEC-GROWW-POSTCLOSE-SETTLE-PROBE-001, #1775). ENGINE UNCHANGED —
NIFTY/BANKNIFTY/SENSEX; pull-INTEGRITY only; the per-item record lives in the
§1 Active-branch blurb + MIGRATION.md #1774 — this header carries the one-line
summary, never the story.)
Earlier: 2026-07-12 (DEC-PREMERGE-M4PRO-NONBLOCK-001, MIGRATION.md #1753 —
the pre-merge gate treats the NON-required self-hosted `m4pro-absolute-throughput`
bench check as NON-BLOCKING: a HARDCODED `NONBLOCKING_CHECK_NAMES` constant in
`scripts/ci/pre-merge-check.sh` is UNIONED with the caller's `IGNORE_CHECK_NAMES`
at all three rollup scan sites (count · failed · pending) — so the
offline-runner pending-forever shape can never refuse a merge (the ONE
residual — a manually-CANCELLED bench run's `bless-baseline` check — is
named in #1753, documented not broadened per the operator only-m4pro
constraint) and
auto-merge.yml's caller-SET `IGNORE_CHECK_NAMES` cannot clobber the exemption —
while a FAILED m4pro is surfaced as a WARN-only line that never affects the
exit code AT THIS GATE (auto-merge's own untouched ci-check still refuses a
FAILED/CANCELLED m4pro — pre-existing; the PENDING shape is non-blocking
end-to-end) and every REQUIRED check still blocks fully (redteam-pinned incl.
the pending-`CI OK (aggregate gate)`-still-blocks + failed-required cases;
fails-before-proven against the baseline script). The check is genuinely
NON-required: not in ci.yml's `ci-ok` needs; MIGRATION.md #382 + #1534 — a
bench regression is caught POST-merge on the self-hosted runner, never a
merge blocker. CI-gate script + redteam tests + docs only; ENGINE UNCHANGED —
NIFTY/BANKNIFTY/SENSEX; the per-item record lives in the §1 Active-branch
blurb + MIGRATION.md #1753.)
Earlier: 2026-07-12 (DEC-READ-GUARD-PARITY-001, MIGRATION.md #1811 —
renumbered #1762 → #1811 at the 2026-07-15 club-T fold (main took #1762 for
DEC-LAKE-INVARIANTS-PREINDEX-001; DEC-IDs are the stable handles per
DEC-LEDGER-RENUMBER-001) — the read-boundary guard parity pack: the #1616 bad-tick screen reaches the
derived-TF prebuild's 1m read seam as the NEW pure frame-form
`bad_tick.screen_frame_bad_ticks` (two-phase — vectorized candidate trigger,
the sequential BadTickScreen arm owns skips + WARNs; clean frames returned
object-identical, clean lakes byte-identical, the SAME
`BRUTEX_BAD_TICK_SCREEN` kill switch, a `prebuild:`-prefixed source on the
SAME `feed.bad_tick_skipped` event), the movers raw-1m seam
(`_read_cut_1m_frame`, POST-cut) gains the mirrored price-sanity gate
[GAP-2 arm RECONCILED at the club-T fold: main's landed #1759 `screen_frame`
now DROPS the insane row first — the gate stands as the inert fail-loud belt
behind it; the intent holds — never a silently laundered
resampled bucket, never a CandleBar-ValueError 500] — and five db_view
stats-manifest claim sites relabel "true O(page) in partition depth" to the
#1614 honest envelope (O(page-files) Parquet work + an O(D) per-request
revalidation stat sweep; the window-ledger survivor sentence deliberately
kept + doc-pinned). READ-side only; clean lakes byte-identical (pinned);
ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; the per-item record lives in the
§1 Active-branch blurb + MIGRATION.md #1811.)
Earlier: 2026-07-12 (DEC-DASHBOARD-COLD-IO-DEADLINE-001, MIGRATION.md #1832 —
bounded cold lake I/O on the dashboard read path: the SummaryCache COLD
single-flight wait gains a TOTAL env-tunable budget (`BRUTEX_SUMMARY_COLD_WAIT_S`
60 s, fail-safe) + a per-owner-Event fail-fast latch — at most the
already-in-flight waiters pay the budget per wedged owner (sequentially
exactly ONE; every later cold poll fails fast), a blown budget raises the typed
`SummaryColdWaitTimeout` the five summary routes map to an honest 503
(four JSON routes `summary_scan_pending`; scoreboard.csv text/plain — never
a raw 500, never a fabricated empty payload;
zero new threads — the waiter's own `Event.wait`, no scan takeover, warm/dirty
paths byte-identical) — and the read-side lake METADATA sweeps (the
stats-manifest consult + /db TOCTOU re-stat + `_bars_file_windows` +
`lake_revision`'s composite generation probe) ride the ONE new breach-armed
`StatSweepGate` leaf (`brutex/data/stat_deadline.py`,
`BRUTEX_LAKE_STAT_DEADLINE_S` 5 s fail-safe): a wedged volume times out into
the NAMED `consult_deadline`/`restat_deadline` fallback clauses / the probe's
existing `None` no-signal contract, the /db seed is SUPPRESSED on a timeout
(never re-hits the wedged volume), latched polls cost ZERO threads, and every
zombie self-clears on the volume's release. Healthy-path stat COUNTS + bytes
unchanged (parity-pinned); write-side hooks byte-untouched (already under the
90 s spine guard); the movers_cache sibling wait is the named follow-up
DEC-DASHBOARD-COLD-IO-DEADLINE-002 (PR #1488 LANDED 2026-07-12 —
this branch is rebased over it; the follow-up is unblocked). READ-side dashboard +
ONE data leaf — ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; the per-item
record lives in the §1 Active-branch blurb + MIGRATION.md #1832.)
Earlier: 2026-07-12 (DEC-DASHBOARD-LAKE-IO-LIMITER-002, MIGRATION.md #1757 —
the /oneclick/equity + /api/v1/launch/* lake-limiter fix: the equity route (the
#1665 merge-seam straggler — the ONLY oneclick route left on the bare pool) and
ALL THREE launch offloads (the #1642 HONEST-LIMITS named follow-up, CLOSED) now
ride the app's OWN 32-token `_run_lake` LakeOffload limiter instead of AnyIO's
DEFAULT 40-token pool — `register_launch_routes` gains the kw-only `run_lake=`
injection seam (None → the lazy `run_in_threadpool`, byte-identical for direct
callers), so a WEDGED lake volume pins lake tokens, never the default pool the
deadline-guarded liveness routes live on. TEETH: the combined 44-request (22
equity + 22 launch/state) realserver starvation regression (`held_peak <= 32`
on the SHARED limiter — fails-before MEASURED: /pull/rate starved 5522 ms,
held_peak 40), counting-offload dispatch pins + the api_pull_rate stays-default
negative twin, order-exact launch dispatch pins, and the import-free AST drift
pin (`tests/unit/test_dashboard_threadpool_pin.py` — Counter-equality over the
FIVE sanctioned default-pool sites: api_pull_rate · api_sweep_progress ·
api_preopen_xlsx · api_preopen_zip · api_logs). HONEST: isolation not a
deadline — a wedged worker stays wedged until the volume recovers; the fix
bounds the pile at 32 isolated tokens, it does not end it. READ-ONLY dashboard
liveness — ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; healthy-lake payloads
byte-identical; the per-item record lives in the §1 Active-branch blurb +
MIGRATION.md #1757 — this header carries the one-line summary, never the
story.)
Earlier: 2026-07-12 (DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001,
MIGRATION.md #1751 — the dashboard pull-contention / feed-resilience slice:
the COMMON `MoversSnapshotCache` gains an opt-in stale-while-revalidate arm —
past the adaptive floor the discovery / windows / ledger / tables caches
serve the LAST-KNOWN payload instantly and refresh in at most ONE tokenless
background revalidator per key (newest-wins store guard; payloads
byte-identical, pinned); the /db spot-cascade walk PRUNES the F&O contract
tree at descent; six db.html loaders become self-recovering with
keep-last-good; NEW `observability/loop_watchdog.py` turns event-loop
silence into WARN/CRITICAL alerts (`SweepAlerter.loop_stalled`). READ-side
dashboard + observability only — ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX;
no lake/schema/scope/vocab/rate change; touched modules 100% line+branch;
the per-round record lives in the §1 Active-branch blurb + MIGRATION.md
#1751 — this header carries the one-line summary, never the story).
Earlier: 2026-07-10 (DEC-SWEEP-COMPUTE-FOLD-DECOMP-001, MIGRATION.md
#1685 — Slice 0 of the Rust sweep-core conversion: a behavior-identical
FOLD/COMBINE decomposition of the metric body in `brutex/metrics/compute.py`
— NEW frozen `TradeFolds` + `_fold_trades` (the verbatim per-trade
accumulations, token-for-token) + public `combine_metrics_from_folds` (the
verbatim scalar tail), the exact shape the Rust core will mirror
term-for-term; BYTE-IDENTITY golden-proven (a 52-test old-vs-new bit-parity
suite against the frozen pre-Slice-0 oracle + the C23 order-sensitivity
hypothesis differential gate; anti-vacuous perturbation teeth measured; the
#1531 frozen `_GOLDEN_RUN_ID`/`_GOLDEN_CONTENT_HASH` reproduce
byte-for-byte). ZERO Rust lands in this slice; ENGINE UNCHANGED —
NIFTY/BANKNIFTY/SENSEX; no scope/vocab/rate-cap change; `compute.py` 100%
line+branch. Any per-round record lives ONLY in the §1 Active-branch blurb +
MIGRATION.md #1685 — this header carries the pointer, never the story.)
Earlier: 2026-07-11 (DEC-FEED-RUST-CORE-001, MIGRATION.md #1797 —
the engine read-boundary DECISION kernel in Rust: NEW standalone crate
`rust/brutex-feed` + the leaf `brutex/feed_rust.py` behind the default-OFF
`BRUTEX_FEED_RUST` flag (Pattern A opt-in) — the positivity refusal order +
the bad-tick keep/skip screen of `arrow_table_to_bars` decided by ONE pure
integers-only scan (kept row indices + warn coordinates + the positivity-stop
coordinate; NO float/string/timestamp ever crosses Rust→Python output), while
Python builds EVERY Decimal/CandleBar/error/WARN with the verbatim existing
expressions — `bxsnap2` run identity byte-preserved by CONSTRUCTION (the 4
frozen parity goldens re-driven flag-ON with an engagement spy, fixtures
byte-untouched; the frozen run_id goldens unchanged). Blanket degrade to the
retained Python oracle (the parity oracle, kept per the rust-first-mandate
exemption) on wheel-absent/stale, non-canonical schema, nulls,
out-of-envelope timestamps, kernel raise/poison — each a named warn-once;
flag OFF (default) = byte-identical today (bomb-kernel pinned). HONEST: the
scan is O(n_bars) per LAUNCH, never O(1), and NOT on the ~33B-trial mask
path; the sandbox measured 1.06× whole-call — materialization dominates by
design, no speedup SLA claimed. ENGINE results UNCHANGED —
NIFTY/BANKNIFTY/SENSEX; no lake/schema/scope/vocab/rate change;
`feed_rust.py` + `replay.py` + `bad_tick.py` 100% line+branch on their
ci.yml gates; the per-fix record lives in the §1 Active-branch blurb +
MIGRATION.md #1797 — this header carries the one-line summary, never the
story).
Earlier: 2026-07-11 (DEC-RESULTS-CASCADE-VERDICT-JOIN-001, MIGRATION.md #1749 —
the Stage-4 cascade overfitting verdicts + the per-candidate CPCV pass rate +
the ranking composite JOIN the display surfaces: the top100 artifact gains the
per-entry `cascade` object + root `cascade_join` meta behind a HARD writer-side
sweep_id gate (the in-memory report threaded from `cmd_sweep_all` — never a
stale disk re-read), /results·/screener·/strategy·operator render the verdict
red-flagged on a FAIL (ranked, never hidden), the shipped-dark `cpcv_pass_pct`
column fills with the REAL stage-3 rate (captured recording-only in
`metrics/cascade.py` BEFORE the threshold compare; RAW fraction in artifacts,
×100 minted only at the display projections), and `/results`' ?preset=composite
axis activates honestly off the newest run's aggs sidecars (the read-time half
of HIGH #40 — the persist half stays DEC-CASCADE-ROW-PERSIST-002). ONE join law
(`engine/cascade_join.py`, structural-fail-closed + per-item-tolerant, dup ids
excluded EVERY copy) feeds BOTH consumers; a no-report lake's /results payload
is BYTE-IDENTICAL (pinned); READ-time + artifact-level only — nothing persisted
on ResultRow (the ResultsStore-not-touched ruling). ENGINE UNCHANGED —
NIFTY/BANKNIFTY/SENSEX; the per-item record lives in the §1 Active-branch blurb
+ MIGRATION.md #1749.)
Earlier: 2026-07-11 (DEC-TRADESIM-OPEN-SESSION-GUARD-001, MIGRATION.md
#1743 — the trade-sim OPEN-position session-boundary guard: an open position
never survives an IST session-date change — the first bar of a new session
closes it retroactively at the PRIOR session's last stored bar (the finish()
idiom) under the NEW append-only exit reason `session_gap`; a pending
next-open exit is subsumed and the boundary bar never arms (earliest re-arm
i+1, fill i+2); Python walker + Rust kernel (u64+u128 via the MaskWord core,
ZERO new FFI inputs) move in ONE change, pinned by the UNMODIFIED two-session
parity suite; the frozen sweep-run goldens are MEASURED unchanged (census:
1661 golden trades, ZERO cross-session; no `canonical_trade_params` key — run
identity untouched); the persistent signal is the NEW `exits_session_gap`
counter (EXIT_REASONS → combo_aggregates → AGG_COLUMNS 49→50 → top100 exits →
the bxExitMixText "gap" part, honest "?" on OLD artifacts) + ONE capped
mask-independent `trade_sim.session_tail_hole` WARN per kernel-input
derivation. HONEST: the `session_gap` fill is a stale-price approximation
whenever the market truly traded past the last stored bar — a real stored
price+instant, counted per trade, self-healing once the hole is re-pulled
(#1031). ENGINE semantics corrected on hole-shaped feeds ONLY — healthy
contiguous feeds byte-identical; NIFTY/BANKNIFTY/SENSEX (+ NSE-NIFTY-FUT); no
lake/schema/scope/vocab/rate change; `trade_sim.py` + the whole engine 100%
line+branch on the ci.yml whole-engine gate; the per-fix record lives in the
§1 Active-branch blurb + MIGRATION.md #1743 — this header carries the
one-line summary, never the story).
Earlier: 2026-07-11 (DEC-GREEKS-RUST-BATCH-001, MIGRATION.md #1892
(renumbered from the provisional #1724 at the club-R2 fold, then
#1795 → #1892 at the club CDR2 fold — bare vs club-D's #1795a–#1795d
suffixed block per DEC-LEDGER-RENUMBER-001) — additive
`trading_core.greeks_batch` columnar FFI face of the frozen Black-76 kernel
(calls the SAME scalar `greeks` per element — zero math duplication; crate
STAYS 0.3.1, no `_indicator_version` re-key) + the default-OFF
`BRUTEX_GREEKS_RUST_BATCH` batch-solve seam in the Greeks enrich loop:
`_compute_bar` split into the ONE shared `_screen_bar`/`_solve_one`/
`_finish_bar` copy serving BOTH arms (every Decimal/repr/exp/gate op stays
Python — the anti-drift core), 16-element per-process burn-in bit-compare +
autorevert latch (mismatch or batch fault ships the ORACLE values;
MemoryError re-raises raw), chunk cap 65,536. OFF (default) arm byte- AND
log-identical (bomb-spy + zero-event + OFF-cost-bench teeth); ON arm's ONLY
deltas on a COMPLETING pass are three fault/engagement-scoped events (on
a mid-target crash from out-of-band-corrupt rows the proven bounds are:
zero writes both arms, same target_errored fence + equal counts, siblings
bit-identical, OFF's pre-raise stream a — possibly equal — prefix of ON's
engaged-filtered stream with a pass-1-WARN-only extension — while WHICH
poison's exception surfaces may differ between arms when
several are present; disclosed in #1892); the
EXISTING enrich golden
reproduces byte-identically with the flag ON (fixture untouched,
engaged-INFO anti-vacuous pin). HONEST: both arms O(rows) — ON/OFF wall
≈1.0× measured on the sandbox (run-to-run noise straddles 1.0 — reruns read
0.96–1.00×; no speedup claimed, ceiling-gated ONLY at the 1.5× no-regression
bound; the retained Python work dominates); ships INERT —
a default flip needs its own DEC/ADR. ENGINE UNCHANGED —
NIFTY/BANKNIFTY/SENSEX; no lake/schema/scope/vocab/rate change;
GREEKS_FORMULA_VERSION stays 2; `greeks_enrich.py` + `spot_join.py` +
`options/greeks.py` 100% line+branch on their ci.yml gates; the full record
lives in the §1 Active-branch blurb + MIGRATION.md #1892 — this header
carries the one-line summary, never the story).
Earlier: 2026-07-11 (DEC-RUST-DATALAKE-IO-ROADMAP-001, MIGRATION.md #1891 —
slice 8 of the Rust-first conversion program (the data-lake I/O layer: 16 `brutex/data/`
modules + `brutex/feed/resampler.py`): the panel's keep-Python verdict was
OPERATOR-OVERRIDDEN to Rust-everywhere — a byte-identity-gated CONVERSION ROADMAP is
adopted (ADR-0020, `docs/adr/0020-datalake-rust-conversion-roadmap.md`), DOCS-ONLY in
this commit. Honesty banner kept MANDATORY verbatim: "expected payoff ≈ 0 — conversion
is for uniformity, not speed" (IO-bound glue over native engines; the two Python
hotspots that ever mattered were already vectorized in-place, #1023/#1313). Mechanism:
the port calls polars' NATIVE Rust API — the same encoder family the Python path
already invokes (`use_pyarrow=False`) — so parity is achievable by construction IFF the
crate↔wheel lockstep holds, and the empirical spike PROVED it (ADR-0020 §1:
FEASIBLE-BY-LOCKSTEP — a polars `py-1.42.1` crate build byte-matched the installed
wheel on the full 5-case corpus through the REAL `_render_bytes`; production carries
the wheel==tag==zstd atomic pin + the fail-closed version guard — wheel ≠ pin ⇒ the
flag refuses with one loud WARN). Order: PR-1 the `parquet_writer` render seam (new
`rust/brutex-lake` crate, default-OFF env flag, Python as live parity oracle, A/B byte
gates + frozen goldens + the #1531/#1532 burn-in-autorevert pattern) → PR-2 option
writer render → PR-3 merge/dedup → PR-4 locks/atomic-replace → resampler/stdlib/policy
waves; `window_ledger_store` (pyarrow C++ encoder, no Rust twin) is the LAST-MILE named
escalation point — polars-native route first, else escalate with the priced
DEC-LAKE-EXACT-PIN-001. The panel's findings persist as the risk register + standing
hazards (lockstep fails LOUD flag-stays-OFF; zstd stack identity; exception-taxonomy
re-derivation; run-identity bytes downstream). ENGINE UNCHANGED —
NIFTY/BANKNIFTY/SENSEX; no scope/vocab/rate change; zero code ships in the roadmap
commit itself — PR-1 has since SHIPPED on the same branch (the `rust/brutex-lake`
crate + the default-OFF `BRUTEX_LAKE_RUST_RENDER` render seam with import/version
guards + burn-in autorevert; the byte-parity corpus green locally, and the 5-lens
adversarial-review fix round folded pre-merge — the §1
Active-branch blurb + the MIGRATION.md #1891 PR-1 segment carry the record), and
PR-2 (the option-writer ALL-DEFAULTS render seam — its OWN steward-assigned row
DEC-RUST-DATALAKE-IO-SLICE8-PR2-001, MIGRATION.md #1771, the render-PROFILE
registry on the ONE facade, the 12-case options corpus 12/12 byte-MATCH) has
since shipped on the STACKED `claude/rust-datalake-io-slice8-pr2` branch — the
§1 Active-branch blurb + MIGRATION.md #1771 carry that record.)
Earlier: 2026-07-09 (T1 SWEEP-CORE integration train, branch `integration/t1-sweep-core` —
ONE coordinator-confirmed fold of five member PRs in binding order: **#1330
DEC-SWEEP-UNKILLABLE-CORE-001** (MIGRATION.md #1712 / ADR-0019 — the UNKILLABLE sweep run
core; its single-flight mechanism `engine/sweep_singleflight.py` is the train-WINNING
variant by coordinator rule), **#1332 DEC-SWEEP-BLOOM-AUTOGROW-HEADROOM-001** (#1714,
renumbered #1623 → #1631 → #1632 → #1714 across the folds — the sweep-cache resume Bloom auto-grow gains 25%
headroom ABOVE the persisted digest count so a within-wall over-capacity resume genuinely
self-heals instead of advancing one tuple per restart; the 16 GiB wall refusal preserved),
**#1336 DEC-SWEEP-TUPLE-DIGEST-GATE-IDENTITY-001** (#1715, renumbered #1622 → #1632 → #1633 → #1715 — the
ACTIVE gate config + non-default selection knobs folded into the `_tuple_digest` store
address via the NEW COMMON `engine/sweep_identity.py` leaf, byte-stable at defaults
(`5b020e892247deac`); C5 typed `ResultsStoreGateConflictError` tuple-scoped skip), **#1350
DEC-SWEEP-RESUME-STORE-GUARD-001** (#1716, renumbered #1623 → #1633 → #1634 → #1716 — BOTH sweep skip tiers
(checkpoint + dedup-cache, sequential AND parallel drivers) gated on intact on-disk tuple
artifacts so a delete-then-re-run never silently loses finalists; kill switch
`BRUTEX_SWEEP_RESUME_STORE_GUARD`, MISSING-only by contract), and the **#1351 tests+docs
rider** (day-over-day incremental-pull-cycle integration harness + independent TF-resampler
oracle property harness + the runbook partition-delete-behind-SUCCESS-ledger footgun note;
no DEC by the test-only exemption; coordinator-merge instruction honored). Post-#1228 main
tip folded (DEC-LAKE-STATS-MANIFEST-001 #1614). HONEST (Golden Rule #10): each member was
hostile-reviewed on ITS OWN diff — the fold's conflict re-derivations (CLAUDE.md blurbs, the
cli.py citation row, the #1712/#1714/#1715/#1716 ledger renumbers — shifted once more at the
2026-07-09 post-#1352 main fold (main took #1631 for
DEC-ONECLICK-TOP100-OBJECT-ROOT-001), then again at the 2026-07-11 pre-stage
fold-2 of the origin/main lander wave (the wave took #1594/#1632/#1633/#1634;
#1713 skipped — reserved for DEC-RUST-FIRST-MANDATE-001)) are train-level seams disclosed
in the train PR body. ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no scope/vocab/rate
change.
Review-round history is deliberately not narrated in this header — each member DEC's
per-round record lives ONLY in the §1 Active-branch blurb + its own
MIGRATION.md entry.)
Earlier: 2026-07-08 (DEC-SWEEP-UNKILLABLE-CORE-001, MIGRATION.md #1712 / ADR-0019 —
the UNKILLABLE sweep run core (Phase 2 of the one-click sweep program): all 15 hunt gaps
G1–G15 + R28/R38–R43/R45 built around a byte-untouched engine — 16 NEW 100%-line+branch
modules (flight recorder on the COMMON orchestrator `AuditWriter` SHA-256 chain at
`runs/<run_id>/flight-recorder.jsonl` · contract-exact run manifest + ONE additive
`RUN_MANIFEST` action on the EXISTING CLI sweep chain · content-hash stage stamps incl.
stage-3 CELLS validated before the cascade consumes · live tripwires + set-once SUSPECT
latch · secret-redacting diagnostic bundle · chain map · run-core CM · single-flight flock
+ lake read-guard · stall policy (default exactly 300.0) · progress-liveness verdict (the
CONFIRMED heal monotonic-vs-wall defect fixed — a LIVE run is never quarantinable, legacy
⇒ UNKNOWN ⇒ heal refuses) · quarantine-not-abort for corrupt tuple stores + checkpoints ·
the R45 independent naive reference simulator (AST-pinned import ban, K=200,
mismatch ⇒ SUSPECT, NEVER adopt) · exit-status artifact served through `read_progress`
with zero route edits · `--detach` own-session-leader launch · verdicts + artifact guard
for the P3 joins), run_id = `grid_identity(grid)` (NO timestamp — resume lands in the
SAME `runs/<run_id>/`), NEW `sweep-rehearsal` CI job (the full chain in miniature every
push, measured 350.5 s sandbox) + the fault matrix on chaos-race + the MECHANIZED R/G
traceability gate. PROVEN ENGINE UNCHANGED: A/B byte-identity (run-core ON vs OFF —
per-tuple store SHA-256 maps identical) + the frozen golden reproduced with every
Phase-2 default ON incl. diffcheck. HONEST: dashboard sweeps stay IN-PROCESS until the
1-line detach adapter lands (CLI is detached-capable today); the 5 hard-gate per-door
kills are ABSENT manifest keys (P3-owned follow-up); TUPLE_DONE is decimated LOSSLESSLY
(cumulative counters; reconciliation reads SweepProgress exactly); diffcheck covers
projection+trade-sim+drawdown, NOT the 60 indicators; D-19 mutation floors are ratchets
below 85% (74–79% measured, raise = named follow-up); the G10 "peer pull" refusal wording
is pinned debt — follow-up DEC-SWEEP-LOCK-WORDING-001. HOSTILE-REVIEW ROUND 2
(MIGRATION #1712): the G12 crash-loop breaker made DURABLE (per-run counter file — the
`== threshold` CRITICAL-alert edge was structurally dead with a fresh in-memory tracker
per enter); SUSPECT can no longer launder to COMPLETE on resume (exit-status `suspect`
written FIRST + a durable flight-recorder-SUSPECT resume fold); `validate_stage3_cells`
re-hash now `_guarded` (a transient SSD EIO no longer hard-kills a successful sweep); the
DEFAULT multi-core path regained `on_tuple` (TUPLE_DONE + the R39 NaN tripwire, parent-
side) + G1 store-quarantine (worker-side — a corrupt store no longer aborts the grid);
NEW `run_layout.py` single-source run-dir leaf + the Row-15 R40 forensics mirror to
`~/.brutex/runs/<run_id>/` (internal disk). Named round-2 follow-ups: R45 diffcheck in
parallel (DEC-SWEEP-DIFFCHECK-PARALLEL-001), the physical `runs/`→`sweep_runs/` relocation
(merge-integration, P3 not on-branch), Rows 11/12/13/8/16 (cross-phase P1/P3/P6).
HOSTILE-REVIEW ROUND 3 (MIGRATION #1712): the `sweep-all --detach` argparse-abbreviation
fork-bomb CLOSED — the detached child is spawned with a `BRUTEX_SWEEP_DETACHED=1` env sentinel
and `cmd_sweep_all` honours `--detach`/`--det` only when it is absent, so the abbreviated flag
(prefix-matched by argparse's default `allow_abbrev`) can never re-trigger detach in the child
(env-sentinel is spelling-immune, not sys.argv-string-filtered); `mark_suspect`'s mid-run
exit-status write no longer resets `wall_started_utc` (a RUNNING write PRESERVES a prior
artifact's launch start; only the very first enter stamps `now`) so a SUSPECT run's forensic
duration reads from launch, not from the suspect-latch instant. RUNCORE-DASHBOARD-COVERAGE
finding REJECTED as stale (counter-evidence): the dashboard one-click sweep button ALREADY
routes `run_sweep_from_request` → `cmd_sweep_all` (since #1120, on main too) → the SAME
`with sweep_run_core(...)` block, so keep_awake + single-flight flock + G12 corrupt-checkpoint
quarantine + Row-15 forensics mirror + flight recorder ALL cover the button; only `--detach`
(survival past process death) is CLI-only, which is the disclosed honest envelope (an in-process
dashboard job cannot outlive the uvicorn worker). HOSTILE-REVIEW ROUND 4 (#1712): two MINOR
observability defects closed — (1) the G12 durable crash-loop counter now RESETS at a successful
run boundary (`reset_checkpoint_corruption` in `_finalize` on COMPLETE/SUSPECT_COMPLETE) so two
corruptions SEPARATED by a fully successful recovery no longer chain `1→2` into a FALSE CRITICAL
`checkpoint_crash_loop` page (`consecutive` now means "corruptions with no successful run
between"); (2) the C6 heal guard closes the resume-startup window — a RESUMING run's progress file
lags with the prior attempt's DEAD pid until the post-preload reset, so `cmd_sweep_heal` now ALSO
treats the run live when the fresh `exit-status.json` (state=running + LIVE pid, written first at
enter under the G6 flock, already enriched into `read_progress`) says so via the NEW pure
`sweep_liveness.exit_status_running` — no concurrent `--execute` heal can quarantine a resuming
run's stores. Row 19 VERIFIED (dashboard job is keep_awake-covered via the cmd_sweep_all
delegation); Row 21 HONEST (the `sweep_run_core(log_file=)` plumbing stays UNWIRED at cmd_sweep_all
— a run-scoped raw log needs file-routed logging at PROCESS START, out of scope; the material
events are already a run_id-scoped log on the INTERNAL DISK via the mirrored `flight-recorder.jsonl`
— follow-up DEC-SWEEP-RUN-LOG-FILE-001). ENGINE
UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no scope/vocab/rate change.)
Earlier: 2026-07-11 (DEC-FRONTEND-LATENCY-HARDENING-001, MIGRATION.md #1720 —
the dashboard FRONTEND latency-hardening pack: the v29 operator page's DUPLICATE
pollers consolidate onto ONE BXPoll-disciplined consumer per endpoint (subscriber
fan-outs added to bx-oneclick.js / bx-rate-monitor.js / bx-liveproof.js; the
measured BEFORE on the idle operator page was 336 requests/120 s with
sweep-progress / pull-rate / liveproof each polled by TWO-THREE summed pollers;
measured AFTER, same harness: 240 requests/120 s — −96, −28.6%),
the six fixed-interval template pollers (coverage · spot_coverage · fetch_windows
· completeness · expired_options 4 s + launch 2.5 s) migrate onto the ONE shared
`BXPoll.createPoller` core via a custom-fetchJson tick seam (renders
byte-identical; failed ticks back off 3 s→30 s; DOMContentLoaded start;
guarded-legacy degrade), the 8 /screener filter inputs + /results Top-N debounce
200 ms, completeness/spot_coverage adopt the in-place tbody row-patch idiom (ONE
shared esc()'d row builder, shell built once), and /reports' ~750 chips build
ONCE with class-toggle selection. SKIPPED honestly: the bx-format/bx-movers-time
defer (end-of-body includes with genuine parse-time consumers + a standing
non-defer pin) and the universe.html integrity-interval catch-up (already shipped
by #1623). NO measured hidden-tab win claimed (idle-page hidden was already
0 req/s, measured); the `pollRunSweep` hidden-guard gap is source-asserted only.
CLIENT-side only — templates + static JS + tests + docs; ZERO route/payload
change, byte-stable wire; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; the
per-item record lives in the §1 Active-branch blurb + MIGRATION.md #1720.)
Earlier: 2026-07-11 (DEC-DB-LATENCY-HARDENING-001, MIGRATION.md #1719 —
the DB/query-latency hardening pack, READ-side only: (1) the `/db`
window-ledger pager rides a DuckDB LIMIT/OFFSET fast path over the #1533
delta-folded ledger scan source (`db_view.query_window_ledger_page` —
measured 7,321–8,778 ms cold at 400k rows → 564.8 ms; the O(rows) Python
record fold per stamp is GONE; the byte-identical legacy parse stays the
degrade for legacy-shape / NULL-derivable-cell / persistent-duckdb-error /
no-conn); (2) the pre-open export rows are memoised per (basis, range,
symbols) at the write generation (repeat click 326–574 ms / 206 DuckDB
statements → 4.2–18.6 ms / 0 statements, bytes identical) + the IEP (open)
reads are span-bounded (the CLOSE maps deliberately stay whole-partition —
the #1649a hole diagnostics need the unbounded view); (3) the `/db` candles
%CHNG baseline binds the page's own month files instead of the whole glob
(72 files bound twice per click → 2; the #863 timestamp bounds pruned ROW
GROUPS, this bounds the Parquet FOOTER opens too — merged-tree mechanism =
main's #1670 `_bars_file_windows`, this pack's interim arm superseded at the
merge-reconcile, behavior pins re-run green). HONEST: the
window-ledger fresh-stamp click stays ONE O(total rows + delta footers)
columnar scan (never literal O(page) — the repeat click is the O(1)
handoff); the ONE behavior-visible class is corrupt-lake-only — a corrupt
file provably OUTSIDE a bounded read's candidates no longer blanks it (a
strictly-narrower degrade, named in #1719). ENGINE UNCHANGED —
NIFTY/BANKNIFTY/SENSEX; no lake/schema/scope/vocab/rate change;
healthy-lake payloads byte-identical (fast-equals-legacy pinned);
`db_view.py` + `preopen_report.py` 100% line+branch on their ci.yml gates;
the per-fix record lives in the §1 Active-branch blurb + MIGRATION.md
#1719 — this header carries the one-line summary, never the story).
Earlier: 2026-07-08 (DEC-MOVERS-EXPLICIT-BUCKET-ASOF-001, MIGRATION.md #1601 —
an explicit `/movers` bucket-anchor TIME pick now resolves to the COMPLETED bucket
(broker-matching close) instead of a PARTIAL forming bucket. The TIME dropdown on a
coarse timeframe offers only the 09:15-anchored bucket-ANCHOR grid, but the backend
treated a picked anchor as an as-of INSTANT, so 3-minute @ 15:27 showed the partial
15:27 minute close (₹27,932.80) rather than the completed 15:27–15:29 bucket close
(₹27,940.85 = the broker's = 2min@15:29 = 1min@latest); the stored derived-TF partitions
were always CORRECT, only the read-time as-of resolution was wrong. FIX: ONE COMMON as-of
seam `movers_snapshot.resolve_asof_cutoff(interval, time)` (consumed by all three tabs)
resolves an anchor pick FORWARD to the bucket's final minute `min(anchor + minutes − 1,
15:29)`, and the downstream `<= cutoff` `filter_to_ist_minute` caps at the day's latest
available minute (NO look-ahead). TF=1minute identity, "Latest"/blank whole-day
byte-identical (DEC-MOVERS-ASOF-LATEST-DEFAULT-001), non-anchor picks keep as-of-instant
semantics, prev-close %CHNG untouched; additive `asof_through` payload echo + note
provenance. Value TF-invariance (DEC-MOVERS-SPOT-VALUE-TF-INVARIANT-001) now holds PER
shared *resolved* as-of. READ-ONLY dashboard — ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX);
no lake/schema/scope/vocab/rate change; movers_snapshot/movers_futures/movers_options 100%
line+branch; ruff/ruff-format/`mypy --strict` clean, pyright at the pre-change baseline.
Review-round history is deliberately not narrated in this header — each DEC's
per-round record lives ONLY in the §1 Active-branch blurb + its own
MIGRATION.md entry.
Earlier: 2026-07-09 (DEC-MOVERS-FUTURES-HYPHEN-PARSE-001, MIGRATION.md
#1646 — the `/movers` Futures contract-symbol parse is RIGHT-anchored via the
ONE canonical grammar: `movers_futures.parse_future_symbol` now DELEGATES to
`data/fno_symbol.parse_fno_symbol` (FUT-restricted; the local left-anchored
twin — `_parse_expiry_token` + its constants — DELETED), so a hyphenated
underlying (`NSE-BAJAJ-AUTO-30Sep25-FUT`) parses to `BAJAJ-AUTO` instead of
the `parts[1]` truncation `BAJAJ` that made `_underlying_spot_ltp` probe the
nonexistent `bars/NSE/CASH/BAJAJ/` — spot + premium were SILENTLY dropped for
every hyphenated F&O underlying (executed real-lake repro: spot 8010.00
present under the correct key, served row `spot=None`) and the wrong rows were
persisted as warm-grid cache hits. HONEST disclosed delta (sanctioned, junk
tokens only): the deleted twin was shape-stricter than the canonical
`strptime("%d%b%y")`, so a junk lake dir like `NSE-NIFTY-30SEP25-FUT` flips
reject→accept — exactly what options//db/Greeks already accepted; every
well-formed vendor `DDMmmYY` symbol byte-identical, non-hyphenated rows
byte-parity-pinned end-to-end, `&`-names (M&M — no hyphen in the grammar) were
and remain correct. `movers_options` already delegated; `futures_warm_grid`
needed zero change. SAME-PR FOLD-IN (W3 spot-drift ladder): the movers
underlying-spot probe now walks the #1482 (DEC-GREEKS-SPOTJOIN-STOCK-001)
drift-recovery ladder on an exact-key MISS — NEW
`movers_futures._SpotLtpResolver`, ONE per-candidates-compute instance in
BOTH the futures and options loops: exact probe byte-unchanged FIRST; the
COMMON `lake_keys.spot_recovery_candidates(..., stock_drift=)` ladder only on
a miss, fed by a LAZY at-most-once `spot_drift.load_stock_drift_map`
(master-verified; ambiguity POISONED + capped warns; no-master fail-softs to
today's honest no-spot; MemoryError re-raises), the recovery OUTCOME memoised
per (exchange, underlying) with ONE rung-attributed
`movers.spot_key_recovered` INFO — so a spelling-drifted stock spot dir
(chain `BAJAJAUTO` vs on-disk `BAJAJ-AUTO`, executed repro) no longer blanks
futures spot/premium or reads `?` options moneyness; a resolving lake never
builds the map (spy-pinned byte-identity); NO `%26`→`&` recovery — the #1482
pledge stands; read-time recovery only, nothing healed on disk. READ-ONLY
dashboard display fix — ENGINE UNCHANGED —
NIFTY/BANKNIFTY/SENSEX; no lake/schema/scope/vocab/rate change;
`movers_futures.py` + `movers_options.py` BOTH 100% line+branch.)
Earlier: 2026-07-09 (DEC-GROWW-MARKET-HOURS-GUARD-001, MIGRATION.md #1692 —
renumbered #1633 → #1673 → #1692 (#1633 → #1673 at the 2026-07-10 merge-reconcile —
main's in-flight renumber cascade holds the reservation band through #1672 — then
#1673 → #1692 per the coordinator reservation map, #1673 reserved for PR #1369) —
the NSE market-hours pull guard: NO Groww VENDOR pulls between 08:45 (inclusive)
and 15:35 (exclusive) IST on a trading day; an in-flight nightly pull STOPS
gracefully at 08:45 and start/resume is REFUSED until 15:35, keeping the SHARED
Groww account free for TickVault's live feed (the shared TOTP token + 500/min
governor, §2 Rule 6). NEW pure leaf `brutex/calendars/market_hours.py` —
`BLOCK_START=08:45`/`BLOCK_END=15:35`, the clockless predicate `pull_allowed_now`,
`pull_allowed_default` (binding `now_ist()` + the NON-raising `is_trading_day()`;
an out-of-holiday-coverage WEEKDAY reads as a trading day → BLOCKS = the FAIL-SAFE
posture protecting the shared account; a weekend → ALLOWED), the fail-LOUD env
kill switch `BRUTEX_MARKET_HOURS_GUARD` (default ENABLED), `guard_active_default`,
and a `now`-injectable `MarketHoursGuard`. NEW `MarketHoursHalt(BruteXError)` in
`data/errors.py` added to BOTH ingest spines' `_GLOBAL_STOP_ERRORS` +
`pull_jobs.is_pull_halt` (propagates past per-symbol/contract quarantine, fires the
CRITICAL Telegram `emit_pull_halt_alert`). START GATES: `BackgroundPullJob.start`
defers a `JOB_KIND_PULL` in-window UNLESS `market_hours_exempt=True` — the THREE
vendor-free exemptions are the sweep (`job_kind != PULL`), the dashboard
Greeks-backfill start and every dashboard dry-run pull start (both pass
`market_hours_exempt`); `cmd_fetch` defers a LIVE pull at the START gate with a
clean EXIT_OK (dry-run/inspect EXEMPT). MID-RUN (the CRITICAL requirement): a
`market_closed` predicate threaded to the PER-WINDOW seam of both spines raises
`MarketHoursHalt` (the durable window ledger leaves PLANNED/FAILED windows
RESUMABLE — no new checkpoint); `run_until_clean` gains a `market_closed` hook →
NEW `stop_market_hours` verdict; `pull_all_spot`/`pull_contract_candles` catch it
mid-round + belt-break later steps + the minute phase (loud
`pull.step.halted reason="market_hours"`); the METADATA phase
(`pull_expiries_contracts`) gets a per-cell belt (raises `MarketHoursHalt` before
each `get_expiries`/`get_contracts`; `run_two_phase` re-raises it, not
record-and-resume); `run_full_pipeline` re-checks the belt BEFORE metadata AND
before historical + wraps `run_metadata` in `try/except MarketHoursHalt`, skipping
the rest of the vendor chain with `stopped_after="market_hours"`; and the CLI
`brutex fetch` path is MID-RUN-guarded too — `cmd_fetch` threads `market_closed`
into the spot spine + the between-rounds `run_until_clean`, catches
`MarketHoursHalt` (drains + persists the ledger → resumable), and exits EXIT_OK.
The mid-run env kill switch is resolved ONCE at pull start via
`resolve_market_closed()` (fail-LOUD on junk at START; the bound predicate reads
ONLY the live clock per call — so a junk env written MID-pull can never crash a
long run with a per-window re-parse); the one-shot START gates keep
`guard_active_default`. Production entrypoints (dashboard routes + the nightly
headless binding — the MOTIVATING 01:30 case covered) bind `resolve_market_closed()`;
a dry-run / un-wired caller passes `None` (byte-identical). NOT edited:
`_start_auto_resume_on_boot`/`cmd_dashboard` (auto-resume covered transitively by
the start-gates; conflict-free with PR #1343). HONEST: the CLI + metadata paths
are now mid-run-guarded (not merely start-gated); the Groww F&O guard is
per-CONTRACT granularity — a contract already started at 08:45 finishes its few
small remaining windows before the next contract's belt stops the pull (an
accepted bounded overrun, never a whole extra symbol); a pull interrupted at 08:45
DEFERS to the NEXT nightly, NOT a same-day 15:35 re-arm (named follow-up).
ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; pull-CONTROL only (no fetch/rate/pool/
governor change); `market_hours.py` + `data/errors.py` + `universe_phases.py` +
`metadata_pull.py` + `pipeline_pull.py` + nightly binding 100% line+branch, the
CLI/dashboard touched lines covered by the whole-package `--cov=brutex` gate;
ruff/ruff-format clean, `mypy --strict` clean, pyright at the pre-change baseline; the
per-round record lives ONLY in the §1 Active-branch blurb + MIGRATION.md #1692 —
this header carries the one-line summary, never the round-by-round story).
Earlier: 2026-07-08 (DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001 + DEC-FUTURES-ADDRESSING-PIN-001 +
DEC-STAGE1A-ROLL-RESET-001 + the round-1–8 review-fix packs DEC-FUTURES-LAUNCH-REVIEW-FIXES-001…-008,
MIGRATION.md #1700–#1709 + #1710 — the ADR-0012 NIFTY-futures sweep LAUNCH
capstone (PR-5): the futures track is addressed by ONE frozen series alias **`NSE-NIFTY-FUT`**
(symbol_id 4 in the append-only `_SYMBOL_ID_TABLE`; the two pre-launch shapes
`NIFTY-FUT-YYYY-MM-DD` + `NSE-NIFTY-<DDMmmYY>-FUT` are RETIRED with a did-you-mean refusal —
supersedes the #1178 dual-acceptance envelope, satisfies ADR-0016 §6 + the #1138 [C16]
reservation in one PR). NEW `brutex/data/futures_series.py` resolves the alias from the VENDOR
lake dirs (scan + `parse_fno_symbol`, never `strftime`) into disjoint IST-date ownership windows
(`(E_{k-1}, E_k]` — expiry day owned by the EXPIRING contract, the `resolve_near_month` `>=`
law); interior holes REFUSE loudly naming the missing month(s) — INCLUDING a window that STARTS
inside a hole (the first-selected-segment predecessor gap gate, review-round-1 #1703) — a head
span stays UNOWNED (WARNed; never a backward ownership extension serving far-month bars), ANY
positive tail overhang WARNs, the calendar is an OBSERVE-ONLY cross-check (drift WARNed with
evidence, 2027+ coverage abstains — never load-bearing). NEW `brutex/feed/series_load.py` is the
ONE alias-aware sweep-slice loader every sweep mouth now uses (spot path = the old inline
reconcile+ReplayFeed block moved verbatim; the alias is stitched per contract with the exact
`astimezone(IST).date()` ownership cut stage1a keys on; a zero-bar boundary sliver holding NO
expectable session instant SKIPS with one WARN — the loud EMPTY refusal stands for spans the
NSE calendar says should hold bars, review-round-1 #1703). Stage-1A B2 CLOSED
(DEC-STAGE1A-ROLL-RESET-001): a `roll_schedule` of owning-expiry day-ordinals keys the VWAP
session per contract and a key change triggers a TOTAL hard reset of EVERY stateful member
(VWAP, MarketStructureState, EMA20/200, prior-history, day extremes, pivots/daily-history/
anchors — D11: no level ever crosses the roll basis gap; post-roll state == fresh-builder state,
proven bit-for-bit); a bar beyond the schedule raises (never a silent constant-key fallback);
`apply_1day_mask` honestly clears ToD 44-47 + VWAP 52-53 on volume-bearing futures `1day` grids
(D12; spot deliberately NOT flipped — named follow-up). `roll_schedule` conditionally enters
`canonical_trade_params` under a hard instrument⟺schedule `__post_init__` invariant (presence ⟺
the alias, both directions — the #1062 bug class contained; spot run_ids byte-identical, the
unmodified golden suite is the proof); recount compares recorded-vs-re-derived schedules with
evidence. ENGINE surface now EXACTLY {NIFTY/BANKNIFTY/SENSEX spot} ∪ {NSE/FNO/NSE-NIFTY-FUT};
all else REFERENCE_ONLY; no vocab/rate/lake-schema change. Honest: resolution is O(FNO dirs)
once per launch, NOT O(1); D6 map, D14's DSR effective-N half (only MIN_TRADES=30 gates at
launch), the #1013 D8-stamping consumption, futures→options translation (PR-6), and the spot
1day mask audit are NAMED follow-ups. Cascade manifest schedule-carriage is CLOSED by the
round-1 review-fix pack (#1703): the automatic Stage-4 cascade threads
`roll_schedule_by_instrument`, the manifest carries `roll_schedules` for the MANUAL replay, and
a schedule-less futures pool SKIPS loudly — never a schedule-blind re-score).
Earlier: 2026-07-10 (DEC-ONECLICK-PERF-CLUB-001,
MIGRATION.md #1711 — coordinator-ASSIGNED from the reservation map
(next free above the #1264 reservation tail #1710; live-main max #1665
at assignment — gaps are legal; renumbered from the interim provisional
#1666) —
the queued post-#1373 oneclick perf/hygiene club on
`claude/oneclick-perf-club` (built on PR #1373's head 3c478831; the PR
opens after #1373 lands): (F1) `/api/v1/oneclick/verify` — the ONE
oneclick route with no revision cache — joins the stat-token family (key
folds manifest + events + top100 tokens + the chain verified-state; a
primed poll is three stats + the chain cache-peek + one hit; NEW B-7
bench pin,
measured 0.95× flat vs a 1000× manifest); (C-r2-5) ALL FIVE cached
oneclick routes ride the NEW `serve_revision_cached` chokepoint — a stat
OUTAGE (now distinguished from honest ABSENCE by `artifact_probe`) serves
the LAST-KNOWN payload with ONE `oneclick.probe_outage` WARN per outage
per COORDINATE (a whole-volume flap warns once per touched coordinate;
never a silent per-poll O(artifact) re-parse; recovery logs + resumes
normal keying; `OutageGate` LRU-bounded 512); (F4) `discover_runs`
stat-stamps + sorts FIRST and parses only the newest-limit manifests
(identical selection; the F5 uncapped runs_revision stat walk stands);
(A-r4) the /screener render/filter/sort surfaces are client-side BOUNDED
via node-goldened BXScreener primitives (Show all itself capped at 1000;
input cap 5000; every truncation NAMED on the count line) — the backend
half was #1373's TOP100_ROW_CAP; F2b (serialized-response caches)
deliberately SKIPPED as optional polish per the severity correction.
DISPLAY zone only — ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX);
`oneclick_artifacts.py` 100% line+branch on the ci.yml-exact gate; the
per-item record lives in the §1 Active-branch blurb + MIGRATION.md #1711.)
Earlier: 2026-07-09 (DEC-ONECLICK-WINNER-DETAIL-TRUTH-001 +
DEC-ONECLICK-INSTRUMENT-DISPLAY-001, MIGRATION.md #1664–#1665 —
renumbered from #1632/#1633→#1633/#1634→#1664/#1665: #1633–#1636 were
#1323's reserved block (landed on main 2026-07-10), #1664/#1665
coordinator-assigned from the in-flight reservation map —
the strategy-detail honesty pack on `claude/oneclick-instrument-display`
(built on PR #1352's head; the PR opens after #1352 lands),
viewer-reads-storage ONLY (every rendered value verified against
`top100_artifact.py`): (#1664) the winner's `instrument` reaches every
one-click surface — ONE pure `bxInstrumentText` builder feeds the operator
rank-1 card (`r1_instrument`), the combos Inst cell (the hardcoded "—" +
FALSE "not a top100 row field" comment KILLED; `fInst` options repointed to
the ENGINE tokens), the /strategy identity strip (`stInstr`), and /screener
(config-driven `instrument` column + a direction-style exact-match filter
whose options are harvested ONLY from rows carrying the key); the
metric-key-map audit concern was VERIFIED already covered by #1631 with
zero residue — no duplicate work. (#1665) the gross relabel — the signal-only
P&L reads "Gross signal P&L" + the persistent "signal-only — costs modeled
in the options-translation track" chip on the /strategy card AND the operator
hero (killed-token-pinned); the SHIPPED results-depth aggregates render in
the NEW /strategy Deep-stats section (expectancy · recovery factor · loss
streak · MAE/MFE pairs · minute-path DD · exit mix · worst-5-days — worst5
is a METRICS key, the audit refinement) via six pure node-goldened builders
with honest "—" degrades + Beginner block-toggle summary; the regime
breakdown (one_regime_wonder/axis + by_trend) renders on BOTH cards and the
stale "lands with the P3 data" deferral is DELETED; the equity SERIES
sidecar gains `GET /api/v1/oneclick/equity` (the #48 overlap — Protocol
`equity_page` on all three readers, the ONE `_resolve_sidecars` chain,
display-zone parquet read with the honest O(equity-file)-first-read /
O(page)-memo envelope, clamp [1,5000] NAMED, null lows pass through) and the
/strategy equity host renders the stored series labeled "minute-path equity —
the stored real series (R27)" with the per-trade cumsum RELABELED as the
honest fallback (the operator host stays per-trade — the #48 follow-up); the
`/strategy?run_id=&rank=` deep-link contract is PINNED for the /results-side
producer; nav.py NAV_LINKS gains /completeness · /screener · /strategy
(12 template navs updated, the unreachable-page class closed); the two
chart-mapper finder NITs land (`bxBarPrice` — no `Number(null)→0` candles;
skipped rows COUNTED + surfaced "N row(s) skipped (unreadable)"). HIGH #40
composite_score /results join stays DEFERRED (results-storage seam,
coordinator brokering). DISPLAY zone + one additive display route — ENGINE
UNCHANGED (NIFTY/BANKNIFTY/SENSEX); engine writers byte-untouched; both
oneclick modules 100% line+branch standalone; ruff/ruff-format/`mypy
--strict`/pyright clean; `node --check` clean. Any per-round record lives
ONLY in the §1 Active-branch blurb + the MIGRATION.md entry — this header
carries pointers, never round narration.)
Earlier: 2026-07-09 (DEC-PREOPEN-PREVDAY-COMMON-002, MIGRATION.md #1649a — renamed from -001 at the 2026-07-10 merge-reconcile (main's #1634/PR #1323 claimed -001 for the minute-basis calendar-cross-check ADVANCE; -002 is the COMMON-ization remainder + hole-shape observability + the #1649b Muhurat pack) —
renumbered #1623→#1631→#1649a across the 2026-07-09 merge-reconciles (main took
#1623 for DEC-DASHBOARD-POLL-HYGIENE-001, then #1631/#1632 for
DEC-ONECLICK-TOP100-OBJECT-ROOT-001 + DEC-DASHBOARD-VISUAL-OVERHAUL-001; the
613a/613b both-suffixed pair convention keeps the companion entries adjacent);
the /reports preopen MINUTE-basis calendar-contiguity fix + the #1539-named
follow-up CLOSED: the minute-basis PREV. CLOSE anchored on the symbol's OWN
1-day series with ZERO real-calendar check (its docstring called a real NSE
session absent from BOTH series "a genuine holiday" — the confirmed repro
fabricated pct_chng=+10.0 for 2024-01-04 across the missing Wednesday
2024-01-03, and select_top_movers would have crowned it); the minute guard is
now the exact structural mirror of the daily guard — the checked calendar is
the prior-session AUTHORITY, refusal = honest row ABSENCE ("correct-or-absent,
never wrong"; never a fabricated %CHNG, never a 500), surfaced as ONE
`preopen.prev_close_noncontiguous` WARN per (exchange, segment, symbol)
dataset triple (capped 20/build + one suppressed summary) carrying a
machine `hole_shape` + shape-matched detail (the hostile-review
fold — the original one-line "re-pull" advice was WRONG remediation in two
reproduced shapes; since the DEC-PREVSESSION-MUHURAT-COHERENCE-001 amendment
fold, MIGRATION.md #1649b, the split is FOUR-WAY): muhurat_session_bar (the
calendar-confirmed Diwali boundary — a HEALTHY lake, "no action needed",
cross-referencing the #1616 engine anchor law + /movers' prev_close_muhurat
display stamp) / extra_nonsession_bar (a non-Muhurat unexpected non-session
bar — honestly suspicious, inspect it) / prebuild_lag (the 1day series lags
the minute series — run the pipeline prebuild; diagnostic only, the guard
never anchors on minute presence) / missing_session (the genuine hole —
re-pull kept verbatim); the found<expected arms are two-way on the daily
basis (no minute series read). The same #1649b fold ends the annual /movers
false-hole flag: a day-after-Diwali Muhurat baseline now reads
contiguous-True + EMIT-ON-TRUE `prev_close_muhurat`/`oi_baseline_muhurat`
+ " †" + ONE INFO per build (never " *"/WARN), and /db gains the static
%CHNG anchor footnote. CONSOLIDATION: `nse.previous_trading_session` gains the
REMOVE-ONLY kw-only `observed` overlay kwarg (default frozenset() —
every existing caller byte-identical, parity-pinned) and
`preopen_report._previous_trading_day` becomes a THIN delegator over it
(private walk DELETED; delegation-equivalence pinned over the full covered
span; the module-local `calendar.coverage_exhausted` event RETIRED for the
COMMON `calendar.previous_session_coverage_exhausted` — a sanctioned,
consumer-free rename). SANCTIONED: hole rows disappear from minute-basis
exports (remediation now shape-matched — re-pull ONLY for a genuine
missing_session, the pipeline prebuild for a 1day-lag, none for a Muhurat
extra bar); coverage-abstain +
Muhurat-bar / unflagged-drift anchors now SKIP on the minute basis too
(parity with daily; an overlay-flagged day is recovered). HONEST: the
pure-minute no-1day fallback arm stays deliberately calendar-blind (a chosen,
pinned limit); the prior-session memo is O(unique report days) per build, NOT
O(1)-total. EVIDENCE: the finder's repro KILLED on the fixed tree; PRE-fix
goldens (weekend + Holi-2024 + Budget-Saturday contiguous fixture) byte-equal;
the `!=`→`==` mutant proven dead on both guards. READ-ONLY dashboard — ENGINE
UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no lake/schema/scope/vocab/rate change;
preopen_report.py + calendars/nse.py 100% line+branch; ruff/ruff-format clean,
`mypy --strict` clean, pyright 0 errors at the pre-change baseline).
Earlier: 2026-07-08 (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001, MIGRATION.md
#1594 — the automatic lake healer is COMPLETED for the
`options/<series>/<YYYY>-<MM>.parquet` expired-options table: foreign-month-row
+ overlapping-partition detection+heal on the SAME proven quarantine →
writer-locked-rewrite → hash-chained-audit → one-alert-per-batch machinery,
closing the DEC-LAKE-HEAL-001 #1175 named follow-up. Scanner: NEW
`OptionForeignMonthRows` + pure `classify_option_keys` (UTC month routing —
timestamp-only law, side is key payload; overlap via memoised canonical-
sibling reads — ONE per affected month per series, so a multi-month stray
costs one read per home month; clean file pays ZERO extra reads; `months=`
scope now bounds the
options tree BY FILENAME; NEW `include_bars=False` for the bounded L2 census;
census OPTIONS row gains REAL foreign/overlap counts, drift stays honestly
N/A). Writer: `OptionParquetWriter.heal_partition(remove_keys=…)` — RAW
stored-value keyed removal (naive-column immune, keyed never positional) +
wholly-removed quarantine-MOVE; NEW `read_partition_frame` +
`merge_partition_frame` over the ONE `_merge_frame_locked` union core (golden
byte-parity pinned). Healer: NEW `_heal_option_foreign` — copy-forward FIRST
(keep-first union home; canonical wins, drifted overlap copies removed never
restated), remove SECOND; 5-class heal order drift → foreign → dup →
option-foreign → option-dup; crash-convergent, observe/mass-rail/halt/audit/
alert/sentinel machinery reused with option files in the mass-rail
denominator; NO catalog/raw_hash/recall for options (uncatalogued; NOTHING
derives from `options/`). Wiring: L1 reconcile-piggyback is STRUCTURALLY N/A
for options (catalog-walk only — documented); L2 `heal_after_pull` gains
`include_options` (default False) wired True at the F&O post-pull seam ONLY;
L3 startup sweep rides automatically (zero change). Review-hardened across NINE refuter-confirmed
hostile-review rounds (a silent-row-loss merge-baseline BLOCKER, symlink/
TOCTOU containment MAJORs, isolation-escape crashes, surviving mutants,
log-injection guards — every fix pre-fix/mutant-verified); the per-round
record lives ONLY in the §1 Active-branch blurb + MIGRATION.md #1594 —
this header deliberately carries the pointer, never the story.
HONEST: no production
pull currently WRITES `options/` (legacy table; L2 opt-in is opportunistic,
L3 + census are the always-on cover); cross-file value drift resolved
deterministically (canonical/path-ordered-first wins, originals in
quarantine); rows move VERBATIM, never validated. ENGINE UNCHANGED —
NIFTY/BANKNIFTY/SENSEX; `lake_invariants.py`/`option_parquet_writer.py`/
`lake_heal.py`/`partition.py`/`errors.py`/`catalog_reconcile.py` ALL 100%
line+branch; ruff/ruff-format clean,
mypy `--strict` + pyright at the pre-change baseline).
Earlier: 2026-07-09 (DEC-MOVERS-UNDERLYING-CLASS-001, MIGRATION.md #1650 —
the `/movers` page gains an `All | Indices | Stocks` underlying-class filter on
all THREE tabs (Spot / Futures / Options), mirroring DEC-MOVERS-MIN-VOLUME-001
(#1034) EXACTLY: ONE COMMON `brutex/dashboard/movers_underlying_class.py`
(`normalize_underlying_class` blank/unknown → `all`; `candidate_passes_class`
predicate; `filter_by_underlying_class`; EMIT-ON-non-default note/echo helpers)
applied at RANK time in `build_*_payload` over the SAME cached candidate set —
never a re-scan. Classification is viewer-reads-storage-only: SPOT from the lake
segment (`INDEX`→index / `CASH`→stock, the stored `is_index` reflection);
FUTURES/OPTIONS from a `{underlying → kind}` map REUSING the six-step pull
order's `primary_fno_underlying` + `is_index` classification (#1527, never
re-minted). Unresolvable → `unknown`: in `All`, EXCLUDED from Indices AND
Stocks, and COUNTED (`excluded_unclassified` echo + note suffix). Each candidate
gains a DEFAULTED additive `underlying_kind` field (never row-emitted), so the
default `All` payload is BYTE-IDENTICAL (pinned all three tabs). DELIBERATE
DEVIATION (ledgered honestly): the class is a RANK-time filter over the SHARED
cached set, so — unlike the #917 expiry tier — it is NOT in `candidate_key`
(threading it would triple-cache an identical set); cache-independence is proven
by a route test (Indices vs Stocks over one coordinate → different rows,
`movers_cache.size()==1`). ADVERSARIAL-REVIEW FIX ROUND folded (same branch,
pre-merge, MIGRATION.md #1650): the F&O class lookup now NORMALISES the parsed
token into the map's `primary_fno_underlying` key space (a hyphenated/aliased
underlying — `BAJAJ-AUTO` — previously misclassified `unknown` and dropped from
BOTH classes; `parse_future_symbol` made right-anchored + hyphen-preserving,
mirroring `parse_fno_symbol` — sanctioned display/spot-probe corrections); the
kind map AND the stamped F&O candidate cache keys fold the `master_revision`
CONTENT token via ONE `_underlying_kinds_cached` helper (a keyless
load-instruments now refreshes classification with NO generation bump; the
post-pull warmer threads the kinds + shares the composite key — warmed rows were
previously all-`unknown`); full frozen default-payload equality pinned on all
three tabs; the head's CI pyright red (a mis-annotated `-> TestClient` test
helper) fixed — the per-round record lives ONLY in the §1 Active-branch blurb +
MIGRATION.md #1650; this header carries the one-line summary, never the
round-by-round story. DISPLAY-zone read-only — ENGINE UNCHANGED
(NIFTY/BANKNIFTY/SENSEX), F&O lake REFERENCE_ONLY, no scope/vocab/rate change;
`movers_underlying_class.py` + `movers_snapshot.py` + `movers_futures.py` +
`movers_options.py` ALL 100% line+branch; ruff/ruff-format clean, `mypy
--strict`/pyright clean incl. `app.py`.)
Earlier: 2026-07-09 (DEC-ONECLICK-TOP100-OBJECT-ROOT-001, MIGRATION.md #1631 —
the one-click `top100.json` READER-ADOPTS-WRITER fix for the 3/3
refuter-confirmed BLOCKER: the SHIPPED engine writer
(`top100_artifact.write_top100`, default-ON in `cmd_sweep_all`) emits an
OBJECT root `{schema_version, run_id, preset, source, baselines, entries}`
while the display reader (`oneclick_artifacts.load_top100`) demanded a bare
ARRAY — every REAL sweep read "corrupt · 0 strategies" on /operator ·
/screener · /strategy while CI stayed green on hand-written bare-array
fixtures (the fixture-only blindspot). The reader now parses the writer's
object root (unknown FUTURE schema_version → NAMED corrupt, never a crash;
the retired pre-#1621 bare-array form → NAMED corrupt — deliberately NO dual
shape), adds per-row SUB-KEY shape validation (named partial degrades), and
applies the ONE additive reader-boundary metric map `TOP100_METRIC_KEY_MAP`
(`max_drawdown_paisa`→`max_dd_paisa` · `sharpe_ratio`→`sharpe` ·
`deflated_sharpe`→`dsr` · `total_trades`→`trades`; the writer-absent
`cpcv_pass_pct` stays ABSENT → honest "—", never a fabricated 0 — the
screener-band-filter drops-every-row blast radius node-pinned); `preset` is
surfaced for the pages' sort-basis labels. Fixture corpus REGENERATED from
the writer shape (object root + engine metric names; SHAs refreshed
deliberately); NEW writer→reader golden drives the ACTUAL `build_top100` →
`write_top100` → `load_top100` → routes chain. Fold [11]: visible sort-basis
labels on /results AND the one-click surfaces (labels only, neither sort
changed; the preset tie-break divergence itself is RULED design-sanctioned —
inert axes wire-disclosed per #1617 Ruling 10, the /results UI sends no
`?preset=`, negative-pinned). Folds [W2-4]/[W2-5]: Ruling-18
`costs_paisa:null` no longer folds into the non-integer bucket (equity +
R44 arithmetic count null costs at ₹0 with the honest "gross of those
costs" caveat — 3 real rows previously read an EMPTY curve / "0 trades");
the /screener count line's numerator is the RENDERED count, naming the
render cap. Coordinator ruling folded: the /operator v24.4 "LIVE"
hook-banner ticker (16 hardcoded fabricated trade ticks) is DELETED
outright — the honest `#bxTickerStrip` (real runs payload) is the one
ticker kept, killed-token-pinned. Fold [12]: `runs_revision` folds per-run
manifest stat tokens so an in-place `suspect` rewrite moves the cached
runs-listing key (O(run dirs) stats per poll, stated). Wave-3 fold: [W3-R1]
the /strategy candle chart was DEAD (mapper read `idx.ts`; the real
/api/v1/db/candles column is `timestamp`) — fixed via the ONE route-shape
mapper `bxCandleRowsFromDbPayload` + TZ-safe `bxParseTsIst` (naive API
strings pinned to +05:30 IST; arrow placement node-proven TZ-invariant under
TZ=America/New_York) with a REAL-route integration golden; [W3-R4] a
null-VIX writer band no longer renders "between null–null" on any of the
three band sites — ONE shared builder pair reads the honest "VIX band not
stamped for this run" / partial-bound copy; [W3-R3] the /strategy arm
"[object Object]" repro is VERIFIED dead (covered by the f1d271d4 reader
bridge; route-level proof added). DISPLAY zone only —
ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), engine writer byte-untouched;
both oneclick modules 100% line+branch standalone; ruff/ruff-format/`mypy
--strict`/pyright clean. Any per-round record lives ONLY in the §1 Active-branch blurb
+ the MIGRATION.md entry — this header carries pointers, never round narration.)
Earlier: 2026-07-07 (DEC-LAKE-STATS-MANIFEST-001, MIGRATION.md #1614 —
write-time PER-FILE lake STATS manifest: NEW leaf
`brutex/data/lake_stats_manifest.py` (one atomically-replaced JSON shard per
dataset at `<lake_root>/.manifest/stats/…` — per-file DISTINCT-identity-key
row counts + timestamp bounds + `mtime_ns:size` stat tokens captured under
the still-held partition flock), the per-file stats/paging EXTENSION of the
shipped DEC-LAKE-MANIFEST-O1-001 (#1480) dataset-DISCOVERY manifest —
claiming the #867/#1025 named future work: the `/db` candle +
expired-options pagers serve totals / pages / stored-life spans
MANIFEST-FIRST in O(page-files), served ONLY after full per-request
revalidation (parse + key echo + sum invariant · file-SET equality ·
per-file stat-token equality · month-bounds sanity; ANY failure ⇒ the
byte-identical legacy live scan + one fallback log + read-side reseed;
post-query TOCTOU re-stat; a manifest total is NEVER paired with a degraded
page). Write hooks at all five partition-locked chokepoints (spot writer
both branches + heal rewrite/quarantine + option writer merge/heal +
`lake_heal._quarantine_move`) are best-effort + incremental + LAZILY
accreted (absent shard = one cheap probe: key digest + one failed `os.stat`,
probed FIRST — round 2; the prebuild storm costs one probe per write;
seeding is read-side only), keyed on the NEW explicit
`WriteResult.file_rewritten` (bytes RENDERED, never `bars_added`). Kill
switch `BRUTEX_LAKE_STATS_MANIFEST=off` ⇒ byte-identical legacy scans
(modulo the sanctioned unconditional ORDER BY tie-order pin).
HONEST: a hit is O(page-files) Parquet work + an O(D) per-consult
revalidation stat sweep (D = the ONE dataset's month files) — NOT literal
O(1); the FIRST star-select click per (generation × file-state) also pays
one O(D)-footer union-order DESCRIBE (memoised); the first view per dataset
pays today's O(files) once (the seed), and a permanently-REJECTED seed
(un-healed poison / 0-row parquet) is LATCHED per (dataset, generation) so
the doomed scan never repays per click — review-hardened across the refuter
rounds + the post-merge ENDGAME reconciles (LakeWriteHalt-safe reads ·
probe-first hooks · bounded 16 MiB shard read · FIFO-shard hang fix · the
EXECUTED m4pro run 28888100079 A/B gate + 3.0× paging strict-tier
recalibration · the wired-and-executed zero-survivor mutation gate); the
per-round record lives ONLY in the §1 Active-branch blurb + MIGRATION.md
#1614 — this header deliberately stopped narrating rounds after freezing at
round 2 once (endgame round 2); a same-`(mtime_ns,size)` in-place
overwrite stays the documented stat-token residual. Results-safe — never in run identity / partition bytes /
`data_snapshot_hash`; rendered Parquet bytes byte-identical (ON/OFF
parity-pinned). ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; all five touched
modules 100% line+branch).
Earlier: 2026-07-08 (DEC-SWEEP-LOG-VOLUME-GUARD-001, MIGRATION.md #1630 —
the sweep CLI log-volume guard: `brutex sweep`/`sweep-all`/`sweep-daemon` now
configure the ONE COMMON structlog seam
(`observability/logging.configure_logging` — the seam `cmd_dashboard` already
uses) at INFO by default via a single `main()`-dispatch guard, with the
fail-loud `BRUTEX_LOG_LEVEL` override (DEBUG/INFO/WARNING/ERROR,
case-insensitive; junk → `CliUserError` exit 1, never a silent fallback) — a
bare sweep process previously ran structlog UNCONFIGURED (default wrapper
filters at NOTSET) and streamed the engine's per-trade DEBUG events
(`trade_sim.entry`/`trade_sim.exit`) unfiltered to stderr: measured 870 MB on a
~200-session composite-e2e fixture, a disk-fill → `LakeWriteHalt` run-killer on
the §8 2-4 day grid. Sink-level threshold ONLY — no event re-levelled,
WARNING/ERROR untouched, and the Golden-Rule-#8 audit JSONL hash chain
(`SweepAuditWriter`/`AuditWriter`) is direct file appends, NOT structlog, so
audit records are byte-untouched (pinned). An already-configured process (the
dashboard's in-process sweep job via `run_sweep_from_request`) is deferred to —
its rotating file sink survives; the guard validates the env var first, so junk
still fails loud there too. R4 FOLD: the guard covers the PARALLEL path —
`sweep_parallel._worker_init` applies the SAME bound inside every SPAWN
worker via the NEW `configure_process_logging_best_effort` (spawn children
never inherit the parent's structlog config; `BRUTEX_LOG_LEVEL` propagates
via env; junk → INFO fallback worker-side, never a dead worker; REAL
2-worker spawn regression via capfd), and the dashboard-launched sweep
inherits it automatically. OPERATOR NOTE: `BRUTEX_LOG_LEVEL=ERROR` silences
the WARNING-level watchdog/quarantine events — INFO or WARNING recommended.
ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no
scope/vocab/rate change; `cli.py` + `observability/logging.py` +
`engine/sweep_parallel.py` 100% line+branch.)
Earlier: 2026-07-08 (DEC-ONECLICK-SWEEP-RUNS-REBIND-001, MIGRATION.md #1621 —
the P4 merge-integration rebind of the one-click display adapter onto the
SHIPPED #1275 results-depth artifacts: the ONE discovery seam
(`oneclick_artifacts.runs_root`) repoints `<results_root>/runs/` → the
gap-register Ruling-4 `sweep_runs/` SIBLING root — derived from the SAME
imported `trade_store.SWEEP_RUNS_DIRNAME` and mirror-PINNED byte-equal to the
ONE engine seam `trade_store.sweep_run_dir`; deliberately NO legacy `runs/`
fallback probe (a dual root could silently serve stale artifacts) — the
committed fixture corpus migrated `runs/` → `sweep_runs/` byte-identically
instead, with NEW `fixture_results_root` (`<fixture_root>/sweeps` pseudo-lake
law) keeping the fixture override self-contained. The trades seam's PRODUCTION
default becomes the NEW `TradeStoreReader` paging the REAL
`trade_store.read_trade_page` sidecars (top100 additive `run_id` +
`equity_ref.tuple` → `sidecar_paths_from_ref`, every segment safe-validated —
a hostile traversal ref never touches disk; reader order injected > fixture
env > TradeStoreReader > no-lake AwaitingTradesReader). HONEST: a pre-#1275
top100 / absent stamp → loud awaiting; stamp/read damage → NAMED corrupt,
never a 500 or a fabricated count; an unknown combo pages total=0; limit
clamps INSIDE read_trade_page at PAGE_LIMIT_MAX=500, the clamp NAMED in
detail; lots/costs_paisa pass through as honest None (Ruling 18) rendered
"—"; additive trade_seq/path_1m_status/mae_granularity surfaced (mae/mfe
carry a "(1m)"/"(tf)" suffix, textContent-only). The P2 manifest/events
writers are still NOT shipped — the awaiting states + the ASSUMED chain
algorithm stand. DISPLAY zone only — ENGINE UNCHANGED
(NIFTY/BANKNIFTY/SENSEX), engine modules imported never edited; both oneclick
modules 100% line+branch standalone; ruff/ruff-format/`mypy
--strict`/pyright clean.)
Earlier: 2026-07-07 (DEC-OPERATOR-PAGE-REAL-001, MIGRATION.md #1561 —
the Operator page (/) is wired to the sweep-oneclick ARTIFACT CONTRACTS and
the v29 PREVIEW mockup is KILLED by a MECHANICAL sweep (every hardcoded
₹/count/date/invented-strategy-name site re-fed via `data-oc=` hooks,
em-dashed, or deleted — the fabricated CHARGES table deleted outright,
Track 1 is signal-only; a generic zero-₹-outside-allowlist pin guards the
class): real funnel/badges/rank-1 card fed from
`<lake>/sweeps/runs/<run_id>/` {manifest.json, top100.json, events.jsonl}
through ONE clearly-marked integration adapter
(`brutex/dashboard/oneclick_artifacts.py` + the pure `oneclick_view.py`
builders) — LOUD "awaiting real run artifact" states, named
corrupt/partial/EMPTY degrades, NEVER a 500 or fabricated counts; trades via
a typed `TradesReader` seam whose PRODUCTION default is the honest
`AwaitingTradesReader` (the file-reading fixture reader activates ONLY under
the fail-loud `BRUTEX_ONECLICK_FIXTURE_ROOT` with a loud badge + visible
`source` chip; ResultsStore rebind at merge-integration; fixtures live in
tests/, production shows awaiting states). NEW
`/api/v1/oneclick/{runs,manifest,top100,events,trades,verify}` — stat-token
`MoversSnapshotCache` reads, all bench-pinned (`test_oneclick_o1.py`, 6 pin
groups measured 0.98×–1.00× flat + deterministic teeth). HONEST O-envelope:
the cached hit is O(1) vs run count/history/sibling tree/trades total and the
events tail is O(page) (byte-offset seek, 1 MiB window, EOF stat-token
short-circuit) — but the FIRST read per artifact revision parses the
O(artifact) file, the manifest fresh compute adds one O(page) events tail
for the chain map, and the timeline search is WINDOW-scoped (5,000-record
client ring), never O(1)-total. The Run button MOVES to the Operator page
(EXISTING `POST /api/v1/sweep/run`; the condition-subset selector ships
DISABLED "lands at merge-integration (P1)"; the preflight strip is STATIC
honest text — no route serves the estimator yet) and /reports returns to
pre-open ONLY; NEW /screener (client filter/sort over top100,
createElement-only rows; R26 virtual-scroll deviation LEDGERED — a ≤100-row
artifact renders directly, no in-CI ms measured — the #1557 Playwright
browser-truth job does not drive /screener) +
/strategy (createElementNS candles off the EXISTING /api/v1/db/candles,
trade-centered window, arrows dropped LOUDLY when out-of-window, trades
inline at load, equity = client int-paisa cumsum labeled "per-trade close");
R44 VERIFY-ME panel: 7 checks, 5-state truth (operator-judged cap at READY;
the chain check permanently reads "algorithm assumed until P2 merges" — its
golden proves generator↔verifier SELF-consistency only; `check=chain` =
O(file) on click only). The events chain law / events path /
per_door_kills+funnel shapes / interim trades layout are registered contract
PROPOSALS behind named seams. XSS: textContent-only, payloads stringified
into ONE text node, hostile-twin route-driven. NAMED out-of-PR: the
bx-rate-monitor/bx-liveproof→bx-poll refactor; regime/family display (P3).
ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; display zone ONLY — zero
engine/orchestrator/results-storage/preflight edits; both new modules 100%
line+branch standalone + two new static ci.yml gates).
Earlier: 2026-07-06 (DEC-GREEKS-BULK-REENRICH-001, MIGRATION.md #1558 —
the bulk Greeks v1→v2 re-enrichment BACKFILL closing the #1188 named follow-up:
NEW `brutex/data/greeks_backfill.py` — a stamp-driven target-DISCOVERY +
batching layer feeding the EXISTING `enrich_greeks_for_targets` driver
UNMODIFIED (its value-checked reuse gate + honest NO_RATE/NO_SPOT/
BELOW_INTRINSIC/BAD_INPUT/AT_EXPIRY demotions do ALL the correctness work; a
Greek is NEVER fabricated), so EXPIRED contracts frozen at formula v1 AND
CLI-`brutex fetch`-only lakes are now re-stamped to the current formula/inputs.
Scanner (manifest-first enumeration, 1minute-only, per-contract
`greeks_provenance_id` column probe via the NEW exported
`stamp_is_current_inputs`/`current_inputs_pack` predicate — ONE pack shared
with the driver gate, property-pinned) → content-addressed plan (`backfill_id`
sha256 over formula ‖ rate-source ‖ modes ‖ sorted target keys) → bounded
batches with a DURABLE advisory checkpoint (`SweepCheckpointStore` reused
verbatim at `<lake>/.greeks_backfill/checkpoint.json`; crash → resume; deleted
on completion — the stamps are the durable done-record). Surfaces: `POST
/api/v1/universe/greeks-backfill` (+ O(1) progress route, a dedicated
`BackgroundPullJob` — account lock skipped, lake-root single-flight KEPT for
the WET run so a backfill and a pull on the same lake serialise — since fix
round 2 keyed on the STORE-derived WRITE root, and a dry-run start is
lock-free) + a `universe.html` panel
(textContent-only), and `brutex greeks-backfill` with a FULLY INERT
`--dry-run` (zero writes / checkpoint / Telegram — whole-tree sha-pinned; the
one documented read side effect is the shared `.manifest/` cache self-heal);
exactly ONE best-effort Telegram completion summary
(`greeks_backfill_complete`, ✅/⚠️ honesty heads, MarkdownV2-escaped);
`LakeWriteHalt`/`GreeksEnrichPoolBroken` never caught below the job/CLI
boundary (checkpoint survives → next run resumes). HONEST: the scan is
O(files) NEVER O(1); no wall-clock SLA claimed; the stamp prefilter is blind
to a rate/spot VALUE drift under unchanged source kinds — `--force` routes
every contract through the driver's value-checked gate, which catches exactly
that class. NO formula bump (the enrich goldens stay frozen).
ADVERSARIAL-REVIEW FIX ROUND folded (same branch, pre-merge): the CLI wet run
HOLDS the lake-root `pull_singleflight_lock` (contention → loud refusal;
`--dry-run` lock-free); the scan/probe fire per-contract/per-file watchdog
liveness beats + an honest "scanning…" job label (no false CRITICAL stall
alert on a deep-lake backfill); `force`+`limit` REFUSED at scanner/CLI/route
(chipping is default-stamp-mode-only); `SweepCheckpoint.from_dict` rejects a
valid-JSON non-object payload as `SweepCheckpointError` (never a wedging
`AttributeError`); a schema-poisoned stamp column + an out-of-range month
filename fold into `probe_failed`/skip instead of crashing the scan; the
`expired_only` expiry==today boundary regression-pinned.
FIX ROUND 2 folded (refuter-majority-confirmed, MIGRATION.md #1558): BOTH
dashboard jobs' cross-process flock re-keyed onto the STORE-derived WRITE
root (`app._job_lake_root_resolver` — a `--lake-root X`/env-divergent launch
no longer yields disjoint lock files that let a CLI backfill race a dashboard
writer on the SAME lake); the dashboard DRY-RUN backfill skips the lake lock
(`BackgroundPullJob.start(cross_process_lock=False)` — the CLI posture, a
zero-write scan never refuses a real pull); the manifest-miss enumeration
WALK (`db_view._walk_candle_datasets`) beats the stall watchdog per symbol
dir (round 1's beats covered the probe only — honesty-corrected in the
ledger); `SweepCheckpointStore` maps EVERY corruption class to
`SweepCheckpointError` (1 MiB size cap pre-read, `RecursionError` nesting
bomb, same-id `total_combos` mismatch refused, parent-mkdir inside the
mapping — a stray file at `.greeks_backfill` logs + continues per the
advisory contract); every bar-writing wet batch touches
`<lake>/.greeks_backfill/revision.json` and `lake_revision` stats it as a
THIRD component, so a CLI backfill's Greeks reach a running dashboard's
caches on the next poll; the wet report's scan-census carry gains killing
tests.
FIX ROUND 3 folded (refuter-majority-confirmed, MIGRATION.md #1558): the
durable checkpoint FREEZES at the last fully-clean prefix boundary (`record`
only while the run's CUMULATIVE `errored == 0` — an errored contract keeps its
stale stamps, so an identical-plan crash-resume could silently SKIP it and
report a false-clean `errored=0` exit-0 ✅ run; the "plan shrinks so the stale
checkpoint self-ignores" resume claim is corrected for the errored-prefix
case); the two now-concurrent `BackgroundPullJob`s keep INDEPENDENT
watchdog liveness — MECHANISM SUPERSEDED at the merge-reconcile (merge
040b2aec): the branch's interim multi-registrant beat registry (and round
4's `bind_progress_beat_scope` scoping over it) was resolved AWAY in favour
of main's DEC-PULL-BEAT-PERRUN-001 (#1537) per-run pull-id-KEYED beat
channels — `progress_beat.py` at HEAD is main's, byte-identical, and the
clobber/clear false-CRITICAL class stays closed by keyed registration
(see the #1558 supersession note); a stray FILE at
`.greeks_backfill` (ENOTDIR) reads the stable `absent` token instead of
muting the ENTIRE composite `lake_revision` probe.
FIX ROUND 4 folded (refuter-majority-confirmed, MIGRATION.md #1558): the
`backfill_id` binds a rate-source VALUE fingerprint (exact fixed rate / sha256
of the loaded CSV curve), so a crashed FORCE run's surviving checkpoint
self-ignores across a rate-value change instead of exempting the recorded
prefix from the driver's value-checked gate (false-clean green run; a
SPOT-heal inside the crash window stays a DOCUMENTED residue — delete the
checkpoint after a heal) [COMPLETED by FIX ROUND 10 below for the rate
A→B→A round-trip / intervening-writer class the fingerprint cannot see]; a concurrent dry-run backfill's O(files) scan can
no longer mask a genuinely-WEDGED pull's HUNG→CRITICAL→WEDGED chain — SHIPPED
as main's DEC-PULL-BEAT-PERRUN-001 (#1537) pull-id-keyed channels after the
merge-reconcile superseded the interim `bind_progress_beat_scope` design
(which no longer exists in the tree): `BackgroundPullJob._run` runs its
worker inside `pull_id_scope` with its beat registered on its OWN run id, an
unattributed beat is DROPPED + WARNed (loud false stalled-SUSPECT, never a
masked true stall — keyed routing, NOT a fail-open broadcast), and the
round-3 "the WEDGED dual signal backstops the masking" claim stays RETRACTED
as vacuous — WEDGED is gated on the HUNG edge (see the #1558 supersession
note); a null-stamped row with non-finite OHLC
(driver-skippable by IVX-2 design) classifies the NEW `stale_unfixable`
census bucket — never re-targeted (the census converges instead of a forever
re-feed reported `already_current`), surfaced with ⚠️ Telegram head + note +
CLI stderr; `--pause-s`/`BRUTEX_GREEKS_BACKFILL_PAUSE_S` refuse non-finite
values (`inf` crashed the wet run mid-batch, `nan` silently disabled the
pause); the #1175-style bench pins land
(`tests/bench/test_greeks_backfill_scan_cost.py` — flat per-file probe cost,
stamp-column projection spy, O(1) checkpoint bytes) and the ledger's "TESTS
(7 types touched)" header is honesty-corrected.
FIX ROUNDS 5+6 folded (refuter-majority-confirmed, MIGRATION.md #1558):
eager env validation on BOTH surfaces (CLI `CliUserError` one-liner + route
400 BEFORE the O(files) scan/lock); driver-unreachable NON-canonical month
filenames (`2024/1.parquet`) fold into `probe_failed` + ONE loud
rename-to-canonical WARN, never an eternal false-green re-target; the
fixability gate generalised to NON-null stale stamps on driver-skippable
rows; `int(inf)` `OverflowError` mapped at the stamp probe AND
`SweepCheckpoint.from_dict` (one poisoned file/checkpoint no longer kills
the scan/run).
FIX ROUND 7 folded (refuter-majority-confirmed, MIGRATION.md #1558): EVERY
suspicious-file targeting judgment now runs over the DRIVER's row view
(`_driver_row_view` — own-UTC-month bound + the COMMON `dedup_bars_frame`
keep-first, `read_option_rows`' exact per-file discipline) instead of the
RAW physical rows — a stale stamp carried ONLY by a dropped
duplicate-timestamp/foreign-month row re-targeted the contract forever
while the driver read `already_current` under an EXIT_OK ✅ false green
(both directions escaped: a kept non-finite row shadowed by a finite dup,
and a dropped dup carrying the ONLY stale stamp); driver-invisible-only
staleness folds into `stale_unfixable` + the NEW
`stale_on_driver_invisible_rows` WARN naming lake_heal's row-removing heals;
the LEGACY 7-column arm rides the SAME driver-actionability gate instead of
an unconditional stale-null target (a legacy all-non-finite month file — the
COMMON shape on the CLI-`brutex fetch`-only lake, gap b of #1188 — looped
identically; an EMPTY legacy partition honestly classifies nothing-to-do);
the unfixable wordings on note/Telegram/CLI-stderr widened to
"driver-unactionable rows"; the clean fast path stays the bench-pinned
single stamp-column read.
FIX ROUND 8 folded (refuter-majority-confirmed, MIGRATION.md #1558): the
scanner's month-file reachability predicate now mirrors the driver's EXACTLY
— canonical zero-padded name AND `is_file()` — so a canonical-NAMED
dataset-style DIRECTORY (`2024/01.parquet/`, probe-readable by polars yet
invisible to `ist_span_partition_files`' `is_file()` gate) folds into
`probe_failed` + the driver-unreachable WARN instead of the eternal-re-feed
✅/exit-0 false green escaping through filesystem TYPE; and the
merge-absorbed DEC-CLI-GREEKS-ENRICH-001 enrich pass (`brutex enrich-greeks`
+ the `brutex fetch` post-ledger tail) — the SECOND sanctioned ledger-less
Greeks-overlay writer — now touches the SHARED cross-process freshness
marker via the NEW wheel-free COMMON `brutex/data/greeks_revision.py`
(`greeks_backfill` re-exports/delegates byte-identically) whenever it wrote
bars, closing the round-2 staleness class on that surface and reconciling
`lake_revision`'s falsified "the ONE sanctioned production writer" honesty
claim to name BOTH marker-carrying writers.
FIX ROUND 9 folded (refuter-majority-confirmed, MIGRATION.md #1558): the
schema-poisoned stamp fold moved from the raising-`int()` VALUE gate to a
DTYPE gate (NEW `_stamp_dtype_poisoned` — ANY non-integer
`greeks_provenance_id` dtype folds into `probe_failed` at BOTH the raw probe
and the verdict, BEFORE the null branch): a COERCIBLE-but-non-integer stamp
(utf8 `"7"`, Boolean, fractional float) sailed past the rounds-1/6
coercion-trio catches and detonated the un-fenced `is_in` driver-view
filter — ONE junk file killed the WHOLE scan (raw CLI traceback on wet AND
`--dry-run`; dashboard job error, no Telegram) and a fractional float
mis-classified `stale_unfixable` — the now-unreachable trio catches removed,
whole-float columns now fold `probe_failed` (sanctioned narrowing); the
`universe.html` backfill panel poll is SELF-RECOVERING (the
DEC-PULL-UI-STALENESS-001 posture: quick retries then a 3 s→30 s capped
exponential backoff, NEVER a give-up — the bounded give-up that painted
"live progress feed dropped" after ~3 s of blips on a multi-hour wet run
and re-enabled the run-starting button is REMOVED; a non-ok/malformed poll
never settles the panel; a hidden tab fires zero fetches with a
visibilitychange catch-up; and `gbAttachIfRunning()` re-attaches on page
load AND on a started:false refusal — the previously-FALSE "re-open the
page" remedy now works); this file's + #1558's rounds-3/4 progress-beat
mechanism claims reconciled to the shipped #1537 keyed-channel design (see
the supersession notes).
FIX ROUND 10 folded (refuter-majority-confirmed, MIGRATION.md #1558):
checkpoint ADOPTION is freshness-gated (the round-4 fingerprint attests
plan-vs-checkpoint rate EQUALITY only — it cannot see an INTERVENING overlay
writer): a rate A→B→A round trip (a crashed force run under A, a rate-B force
run whose errored first batch FREEZES the id-A checkpoint while genuinely
rewriting the recorded prefix under rate B, then the natural revert to A —
or, with NO second crash, an intervening `brutex enrich-greeks` / pull-tail
enrich pass under the changed rate env) reproduced id-A exactly and ADOPTED
the stale checkpoint, skipping the rate-B residue into a false-clean ✅
`errored=0` exit with the wrong-rate Greeks stamped CURRENT forever; a loaded
checkpoint now self-ignores LOUDLY — and the stale file is DELETED — when the
shared overlay freshness marker was last touched by a FOREIGN writer at/after
the checkpoint's `updated_at` (own-plan touches identity-exempt, so a
legitimate same-plan resume still adopts) or the window ledger moved at/after
it (`_checkpoint_superseded_reason` + the additive engine
`SweepCheckpointStore.discard_adoption()`; every ambiguity biases to the
fresh correct-by-stamps start; NEW documented residue: a mid-pass-crashed
intervening writer leaves no signal — delete the checkpoint, the spot-heal
remediation); and the round-9 `universe.html` gb-poller gains the TWO
sibling-parity pieces round 9 omitted — every gb fetch (poll, start POST,
attach probe) rides ONE `gbFetchJson` helper arming the sibling's
FEED_FETCH_TIMEOUT_MS hung-fetch abort bound through the BODY read (a
headers-then-stalled-body response previously threw nothing: catch/backoff
unreachable, nothing scheduled, panel wedged at "running · …" with the Run
button disabled until a tab flip/reload) plus the `gbPollGen` re-entrancy
guard (a visibilitychange catch-up SUPERSEDES an in-flight chain instead of
forking it; a stale late response never overwrites a newer status line).
ENGINE
UNCHANGED — NIFTY/BANKNIFTY/SENSEX; F&O lake REFERENCE_ONLY;
`greeks_backfill.py`/`greeks_enrich.py`/`greeks_enrich_pass.py`/
`greeks_revision.py`/`sweep_alerts.py`/`sweep_checkpoint.py`
+ `pull_jobs.py`/`lake_revision.py` ALL 100% line+branch (`progress_beat.py`
is NOT touched by this branch — byte-identical to main's #1537 keyed design,
dropped from this list at the round-9 honesty reconcile);
every touched `cli.py`/`app.py`/`db_view.py` line+branch covered).
Earlier: 2026-07-08 (DEC-SWEEP-STALL-OOM-GUARDS-001, MIGRATION.md #1608 —
the parallel grid-sweep (`brutex/engine/sweep_parallel.py`) gains a
HEAVY-GRID-AWARE stall watchdog + a distinct `BrokenProcessPool` QUARANTINE,
closing two round-3 meta-refuter run-killers, additive resilience (a CLEAN run
is byte-identical; frozen sweep goldens + P-06/suffix-independence UNMODIFIED).
(1) the stall watchdog no longer fires a flat 300 s: a legitimately-slow k=5
1-minute tuple (`C(60,5) = 5.46M` masks × hundreds of thousands of bars) runs
as one worker future FAR longer than 5 min with no sub-tuple progress, so at
the grid tail — only slow heads in flight, none completing within 300 s — the
old bound aborted a multi-hour grid near completion. The wait budget is now
SCALED by the in-flight `mask·bar` work (`_effective_stall_seconds = max(base,
work / STALL_MIN_MASK_BARS_PER_SEC)`; `work = C(n_unique, k) × N_bars` stored
per in-flight `_Dispatched`), so a tuple that legitimately needs N minutes gets
a proportional bound while a genuine hang STILL aborts (the bound is FINITE — a
wedged worker makes zero progress — HONEST: the detection latency SCALES with
`C(n_unique,k) × N_bars`, ~4× a heavy tuple's legitimate runtime). Env knobs
`BRUTEX_SWEEP_STALL_SECONDS` + `BRUTEX_SWEEP_STALL_MASK_BARS_PER_SEC`
(positive-float, invalid→default+LOUD; the constructor refuses non-positive/
non-finite values); `stall_seconds`/`stall_min_mask_bars_per_sec` default
`None`→env-resolved so `run_parallel_grid`/the CLI pick it up with no call-site
change. HONEST (Golden Rule #10): `2.5e8 mask·bar/s` is a CONSERVATIVE
hang-detection floor (kernel ~1e9, ~4× headroom), setting only the
false-abort-margin ↔ wedge-detection-latency trade-off, never results. (2) a
worker OOM is QUARANTINED durably + REVERSIBLY + VISIBLY: `_drive` catches
`BrokenProcessPool` DISTINCTLY (`future.result()` runs BEFORE `pending.pop` so
the broken future stays quarantinable) and `_await_next_result` catches a
worker's surviving Python-level `MemoryError` (the cgroup/rlimit OOM shape —
pool NOT broken) as the single-tuple twin; already-FINISHED sibling futures are
DRAINED (real results applied, never discarded); the poison tuple(s) land in
the DURABLE quarantine sidecar `<root>/.sweep_quarantine.json` (fsync'd BEFORE
the raise) — deliberately NOT `SweepCache.record` (which would permanently +
invisibly drop them from every identical resume — the adversarial-review MAJOR
corrected pre-merge) and NO checkpoint advance (`had_failure` latch) — then the
run fails LOUDLY with the NEW `SweepWorkerPoolBrokenError` naming tuple(s) +
sidecar + remediation (reduce `BRUTEX_SWEEP_WORKERS` / k, or raise
`BRUTEX_SWEEP_MEM_BUDGET_BYTES`; then DELETE the sidecar entries and resume —
the tuples are then genuinely RECOMPUTED, the ONE reversible lever;
regression-pinned). A resume gates the sidecar FIRST and reports the skips as
the DISTINCT `tuples_quarantined_prior` counter (on `SupervisedGridSummary` +
the COMPLETE audit payload — NEVER folded into `skipped_cache`), so the
completeness hole is always visible; distinct
`SUPERVISOR_TUPLE_POOL_BROKEN`/`SUPERVISOR_TUPLE_QUARANTINED_PRIOR` audit
actions + ERROR/WARNING logs make the loss LOUD. Fail-loud is chosen over blind
pool-rebuild on the merits + the memory-budget-division interaction: the
per-tuple rank budget divides across workers (`memory_budget_bytes` = `total ×
fraction / workers`) [CORRECTED by DEC-SWEEP-MEM-BUDGET-WORKERS-WIRE-001
(MIGRATION.md #1671): as originally wired the divisor was never passed
(`workers` defaulted 1) — the claim was false until #1671 wired it; see that
entry], so rebuilding into the next heavy tuple would just
re-OOM. HONEST residual (named follow-up, NOT claimed): a deterministically-
WEDGED non-OOM tuple still aborts via `SweepStallError` with a frozen
checkpoint, so an identical resume re-dispatches IT — the stall path has no
quarantine (pre-existing). ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; F&O lake
REFERENCE_ONLY; no scope/vocab/rate/lake/audit-schema change (the new actions
appear only on broken-pool/resume-after-quarantine runs, never on a clean run);
`sweep_parallel.py` 100% line+branch (128 tests) + the touched
`sweep_supervisor.py` (additive defaulted summary field) 100% line+branch;
ruff/ruff-format/`mypy --strict`/`pyright` clean.
Earlier: 2026-07-07 (DEC-SWEEP-NIGHTLY-AUTOMATION-001, MIGRATION.md #1612 —
the R9 nightly zero-click chain: ONE `brutex nightly run` executes COLLISION →
DISK → BACKUP → PULL → PREBUILD → SWEEP over EXISTING public entry points only
(a headless `run_full_pipeline` binding mirroring the dashboard closures; the
sweep via `cmd_sweep_all` on the daemon key-list precedent, the three
unattended escape flags structurally absent AND dispatch-hard-refused —
STRICTER than the daemon, which refuses only `--allow-out-of-scope`), under a
NEW nightly flock + acquire-and-HELD pull locks (the LAKE pull lock held
through the sweep leg = the R4/G10 sweep-vs-pull exclusion, mechanical
[the lake-lock-held-through-the-sweep-leg clause SUPERSEDED by
DEC-SWEEP-LOCK-PULL-CONCURRENCY-001, MIGRATION.md #1785 — released after
PULL+PREBUILD, FREE during the sweep leg];
holder-note honesty on refusals — a pull refused mid-nightly reads
"(holder: nightly:<id>)"), with the R42 break law (first failing link stops;
later links BLOCKED-because-<link>; tripwires — link-transition, DURABLE-audit
read-back reconcile, pull coherence, snapshot re-read, sweep-count identity —
⇒ SUSPECT ⇒ cron-RED exit 2) and the R50 sealed-snapshot backup
(hash-of-DESTINATION manifest written LAST, full pre-seal verify,
`.partial`→one-rename seal; restore = verify-EVERYTHING-then-write, destination
ledger deltas CLEARED of manifest-extraneous files, `ledger.parquet` strictly
LAST, `--yes` gated, CI-drilled every push) + launchd install with the
MANDATORY `launchctl list` verification (installed-but-unverified FAILS — the
TickVault lesson) + the morning Telegram summary/refusal (per-run dedupe keys;
unknown counts read "?", never 0). HONEST: R48 decay diff deferred to v1.1 by
operator directive — a recorded DIVERGENCE from the living requirements (R48
homes in P6; the v1.1 item is R49; the P3 top100.json probe is the seam);
sweep-liveness is a dual-mtime HEURISTIC until P2's results-root flock; the
first post-deploy nightly re-sweeps everything (`code_commit_sha` re-key,
documented not fixed); `bars/` stays OUT of the automated backup (the manual
rsync/Time-Machine path retained) and night-N sweep results are captured night
N+1 (≤24 h exposure); launchd cannot wake a sleeping Mac (next-wake catch-up;
pmset opt-in documented, never run); backup/restore honestly O(files+bytes) —
probes bench-gated flat, backup bench-asserted LINEAR; `--dry-run` runs probes
ONLY (no pipeline even in spine dry-run mode — those write synthetic bars),
tree-oracle-pinned zero mutation. ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX;
F&O lake REFERENCE_ONLY; no scope/vocab/rate change; every new module + the
three touched COMMON modules 100% line+branch).
Earlier: 2026-07-08 (DEC-SWEEP-DATA-EDGE-GUARDS-001, MIGRATION.md #1616 —
two surgical engine data-edge guards for sweep-result CORRECTNESS (lens-D of the
one-click-sweep gap register): (1) a Muhurat-safe prev-day anchor at the
Stage-1A roll seam — `_roll_to_new_day` rolled `_prev_day_ohlc = self._today_ohlc`
on every IST-date change with NO calendar guard, so a Diwali Muhurat evening
session's ~1-hour OHLC became the NEXT regular day's pivot / PDH-PDL / Fib /
day-type anchor (bits 6-29 + 48-51), poisoning 6 days across 2020-2025 with zero
warnings; the fix leaves BOTH `_prev_day_ohlc` AND `_daily_history` untouched
when the completed day is a `has_muhurat_session` date (the prior REGULAR
session's anchor carries through, mirroring `lookup_prev_day_ohlc` + the weekly
anchors) + ONE `stage1a.muhurat_prev_day_anchor_skip` WARN, look-ahead-clean,
parity-pinned across all 6 real Muhurat boundaries; and (2) a bounded bad-tick
screen at the engine read boundary — NEW `brutex/data/bad_tick.py` (`is_bad_tick`
+ `BadTickScreen` + `bad_tick_screen_enabled`) wired into
`feed/replay.py::arrow_table_to_bars` (the ONE sweep-feed choke point) SKIPS-and-
COUNTS a self-consistent scaled bar (close `>= 4x` OR `<= 1/4x` the previous
IN-SESSION kept close — the `x10` fat-finger class the positivity+ordering checks
miss), first-bar-of-session exempt, one `feed.bad_tick_skipped` WARN per session,
default-ON `BRUTEX_BAD_TICK_SCREEN` kill switch, O(1)/bar, never value-editing /
never fabricating. Threshold 4.0 derived from NSE circuit-breaker physics
(10/15/20% daily bands → single-minute moves << 0.25x; regular-day goldens
byte-identical). ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; F&O lake
REFERENCE_ONLY; no scope/vocab/rate/lake-schema change; `stage1a.py` +
`bad_tick.py` + `replay.py` ALL 100% line+branch; ruff/ruff-format/`mypy
--strict`/pyright clean. The P-06 look-ahead + suffix-independence suites pass
UNMODIFIED.)
Earlier: 2026-07-08 (DEC-VIX-STAMP-WIRE-001, MIGRATION.md #1645 —
India-VIX trade stamping wired end-to-end: `vix_at_entry`/`vix_at_exit` on
every OBSERVABLE production trade + the persisted nullable
`vix_mean`/`vix_min`/`vix_max` finalist trio (REFERENCE ONLY — never in the
60-vocab, `_RANKABLE_FIELDS`, or the BLAKE3 run identity; a missing VIX bar
stamps `None`, never fabricated); the FIVE sanctioned unstamped
DISCARDED-stream sites are literal `reference_stamper=None`, AST-pinned with
exact counts (the round-6 review unstamped the cascade candidate
re-simulation — stamps provably discarded at the CandidateInput scalar
conversion — and DELETED the two manual-replay `vix_stamper_for_manifest`
builds that paid a full VIX lake read per replay/click for zero output);
redo-honest trio (None→value backfill · the typed tuple-skip
`VixTrioConflictError` · `sweep-verify --recount` stamps like the launcher and
attributes VIX-only divergences non-corrupt via `results_store.VIX_FIELDS`,
the ONE public trio-column source, + `dataclasses.fields()`-driven re-pair
gates). ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; touched modules 100%
line+branch).
Earlier: 2026-07-08 (DEC-MUTATION-RUNTIME-003, MIGRATION.md #1597 —
the weekly deep mutation gate is made GREEN end-to-end: both cargo-mutants
jobs restructured onto sharded matrices (6 fno-math + 7 trading-core shards;
worst shard 65 min vs the 120 min cap), trading-core WIDENED to
evaluator+python+spec under `--features python,serde`, the python mutmut loop
wedge-proofed (a timeout-wrapped rate_common runner + fail-fast audit tests)
and fully DE-SURVIVORED — compute 354 · audit 254 · rate_limiter 165 ·
store 146 · partition 108 · parser 33 · spec 91 · rate_common 369 mutants,
ALL zero-survivor (latent main-side debt killed too: 2 rate_limiter sentinel
+ 3 partition probe-order + 70 audit survivors) — with timeout-vs-survivor
HONEST alert titles; rehearsal run 28922792812 green 15/15 (python loop
68m52s). COMPLETION PROOF = the first green SCHEDULED `mutation.yml` run
(next 2026-07-13 04:00 UTC) — STANDARDS row 3 stays 🟡 until it lands.
rate_common is GATED, its ceilings/floors/governor values byte-untouched
(§2 Rule 6); ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no
scope/vocab/rate-cap change).
Earlier: 2026-07-06 (DEC-BACKUP-OFFBOX-001, MIGRATION.md #1596 —
automated incremental OFF-BOX backup of the lake + ledger + DuckDB masters + sweeps +
hash-chained audit logs (restic → S3 Object-Lock / B2 immutable; blueprint
`docs/REMOTE_CONTROL_AND_ZERO_TOUCH_COMPARISON.md:304` rows 28/30): NEW `brutex/backup/`
package + `brutex/config/backup_env.py` + `brutex backup {run,check,restore-drill,status}`
CLI, built OUTWARD from a 23-row failure-mode matrix — every failure = a deterministic
triple (exit code + `backup.*` structlog event + alert-or-explicit-no-alert); the phantom-
`/Volumes` mount gate runs BEFORE any restic verb (critically before `forget --prune` — the
one path by which a backup system could destroy data); an O(1) injectable-clock DEAD-MAN
(verdicts FRESH/RUNNING/HUNG/OVERDUE/NEVER_SUCCEEDED/STATE_CORRUPT/CLOCK_SKEW; CRITICAL
Telegram via 4 new `SweepAlerter` methods riding PHASE_BLOCKED — ZERO `telegram.py` changes;
durable cross-run re-alert throttle in the checker-owned `deadman.json`; daily deep
remote cross-check; healthchecks ping every tick — the external keystone lane); a monthly
byte-verified RESTORE DRILL (1 MiB known-bytes canary + stratified sample + the audit-JSONL
byte-PREFIX law); 3 launchd templates GENERATED by `schedule_render.py` + golden-pinned
byte-equal under `docs/runbooks/backup-templates/`; NEW `docs/runbooks/backup-offbox.md`
(12-step install incl. the mandatory live negative dead-man drill + cron fallback) +
ADR-0017's manual-acceptance register M1-M8; automates + supersedes the manual tar §2 of
`docs/runbooks/lake-backup-restore.md` (restore-ORDER law + consistency argument stand
verbatim). HONEST: a dead box is silent on-box until the M6 external watcher; RPO = the
schedule interval (sweeps/audit are the non-re-pullable loss window); subset verification
is sampling. Ops tooling ONLY — ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; F&O lake stays
REFERENCE_ONLY; no scope/vocab/rate-cap/lake-schema/stored-bar change; touched modules 100%
line+branch).
Earlier: 2026-07-07 (the 11-item POLISH PACK, MIGRATION.md #1582–#1592 —
DEC-SESSION-CLOSE-BOUNDARY-PIN-001 + DEC-DAILY-CUTOFF-GROUNDED-001 +
DEC-NTM-REVALIDATE-CROSSPROC-001 + DEC-RETRY-COUNTER-SPLIT-001 +
DEC-GROWW-EPOCH-VERDICT-001 + DEC-CLI-DEPTH-FLOORS-001 +
DEC-ACCOUNT-LEASE-CROSSHOST-001 + DEC-DERIVE-LOCK-VISIBILITY-001 +
DEC-RESUME-BANNER-AUTOCLEAR-001 + DEC-QUOTA-VERDICT-MOOT-001 +
DEC-DAILY-OPEN-PREDICATE-ALIGN-001: (1) the
at-close 15:30:00 boundary divergence (`daily_derive_cutoff` strict `>` vs
`clamp_window_end` `>=`) is ruled DELIBERATE and drift-PINNED cross-module
(the boundary tightening refused, the #1267 pattern; clamp byte-untouched;
NOT the daily-OPEN predicate item — that is item (11), closed by
IMPLEMENTATION, never wontfix); (2) the prebuild's
session-close daily cutoff is REPAIRED (the #1401 M1 regression had left
DEC-PREBUILD-SESSION-CLOSE-CUTOFF-001 production-dead — route/parallel
orchestrator/heal recall all now ride ONE law via NEW `nse.now_ist` threaded
as `daily_cutoff_now`) and gains the data-grounded DEFER-only variant (NEW
`session_last_bar_complete` + `daily_derive_cutoff_grounded`, result ≤ clock
always, abstains on every unknown, DENSE-index-gated per #1031, kill switch
`BRUTEX_DAILY_CUTOFF_GROUNDED`; a post-close pipeline run now derives the run
day's 1day bar SAME-RUN, an incomplete dense session DEFERS — never a partial
daily bar; complete sessions byte-identical grounded-vs-clock, pinned); (3)
the NTM SWR background refresh gains a CROSS-PROCESS single-flight (advisory
flock sidecar `.ntm_revalidate.lock` + double-checked freshness under the ONE
COMMON `_ntm_cache_is_fresh`; unavailable lock fails OPEN to the pre-fix
refresh — never worse; closes the #1287 follow-up); (4) the conflated
`in_pull_fetches` ledger counter is SPLIT into persisted `empty_retries` vs
`transient_retries` (default `None` = honest unknown on old rows, the
optional-column template's third use; spine invariant pinned `empty +
transient + (1 if bars) == in_pull_fetches`; ACCEPTS the follow-up #924 +
#1355 twice declined as display-only relabels — the in-CALL 6× ladder still
NOT persisted, the #924 caveat stands); (5) the defensive epoch-timestamp
branch gains a deterministic session-plausibility VERDICT (NEW
`epoch_verdict.py`, unix-utc vs ist-naive against the real NSE calendar, ONE
`groww.parse.epoch_timestamp_verdict` WARN per epoch payload; round-1 review
fold: out-of-coverage dates abstain SILENTLY via the non-warning
`is_holiday_data_available` pre-check — the strict-calendar routing would
have stormed up to 2,048 `calendar.holiday_data_unavailable` WARNs per
out-of-coverage payload per window) + a live-probe
runbook — parsing byte-unchanged, the Unix-UTC assumption stays honestly
UNVERIFIED; (6) the CLI `brutex fetch` gains the vendor-depth-floor learner +
learned-floor planning clamp (`max(GROWW_HISTORY_FLOOR, learned floor)` via
the existing `symbol_windows` seam, SAME schema_meta sidecar as the dashboard
— either entrypoint's learned floor clamps both SERIALLY; dry-run
byte-identical; round-1 review MAJOR fold: the sidecar is a TRANSIENT
open→op→close per get/set (`_TransientCliFloorStore`) — the original
whole-pull rw DuckDB hold blocked a `brutex serve` boot mid-fetch and
always-failed under a running dashboard; while a dashboard process holds
`instruments.duckdb` the per-op degrade WARNs and self-heals, never a
blocked fetch); (7)
the account pull single-flight gains its CROSS-HOST arm — an advisory
heartbeat lease on shared lake storage (`.locks/pull_lease.json`, 30 s
refresh, 180 s self-heal, kill switch `BRUTEX_PULL_LEASE=off`) refuses a
start on a FRESH foreign-host lease; honestly DETECTION, NOT mutual
exclusion (TOCTOU/clock-skew/hostname-collision limits stated); every
synchronous lease lake-I/O op (enter read + first refresh, exit clear —
they run while BOTH flocks are held, and the exit clear runs BEFORE the
flock releases) is deadline-BOUNDED under the COMMON `run_with_deadline`
guard (`BRUTEX_PULL_LEASE_IO_DEADLINE_S`, default 5 s; round-1 review
MAJOR fold — a wedged volume previously hung `clear()` forever and refused
every future pull until restart; wedged op = one `pull_lease.io_wedged`
WARN + stale-TTL self-heal, at most one zombie daemon per op); (8) the
movers `PrebuildGate` honors the derive lock with HONEST attribution — a
CLI-process derive yields the `gated:true` payload with
`source="cross_process"`, and (round-1 review fold) the dashboard's OWN
out-of-pipeline derive (the lake-heal L2/L3 recall — flock contends across
open file descriptions, so the flock probe alone could not tell it apart)
reads `source="local_derive"` via the zero-I/O
`derive_lock_held_by_this_process` registry probe consulted FIRST, never a
fabricated "another process" claim (probe OSError fails OPEN, one WARN per
outage; closes the #1288 follow-up); (9) the intent-resume `ResumeBanner` auto-clears
on the next REAL pull launch that actually STARTS (it had NO production clear
path — a stale CRITICAL banner survived its own remediation; round-3 review
fold: the clear is STARTED-gated via the NEW `clear_banner_after_start` AFTER
`BackgroundPullJob.start` — a REFUSED start (contention / foreign-host lease)
launches nothing and clears nothing); (10) the #889 verdict-driven
quota phone-alert follow-up is CLOSED AS MOOT (Groww backtesting has NO
daily quota + ZERO production `DailyQuotaTracker` constructions,
AST-pinned) and the doubly-dead informational quota surface is DELETED
(`GET /api/v1/quota` + panel fields + `/universe` gauge — all only ever
served the constant sentinel), the vendor-neutral enforcement seam kept
dormant logic-byte-identical; (11) the TWO daily-open-substitution surfaces
now share ONE per-session predicate (MIGRATION.md #1592,
DEC-DAILY-OPEN-PREDICATE-ALIGN-001 — the named follow-up the #1267/#1232
drift pin deferred to): NEW pure `spot_daily_resample.classify_daily_open`
(substituted iff a NON-Muhurat session's first present 1-minute bar is NOT
exactly 09:15 IST) drives the prebuild detector directly AND the
/completeness · /universe · /coverage census's DuckDB CTE as its
mirror-pinned SQL twin (strict `>` → `<>` + a `_muhurat_exclusion_sql`
NOT-IN baked from `MUHURAT_SESSIONS`) — a Muhurat session no longer counts
read-side (was a yearly false positive on a healthy lake), a stray
pre-09:15 first bar now counts on both (the strict-`>` under-count closed);
same-lake same-verdict equivalence proven + pinned, the old divergence pin
consciously rewritten to an alignment pin; the ONE remaining deliberate
divergence is POPULATION not predicate (census = ALL stored sessions,
detector = the forming-session cutoff), documented + pinned; also corrects
#1582's earlier misattribution (this item was never closed-wontfix — it is
closed by IMPLEMENTATION). ALL ELEVEN: ENGINE UNCHANGED —
NIFTY/BANKNIFTY/SENSEX; no scope/vocab/rate-cap change; fetch
PACING/RATE byte-identical everywhere, with exactly THREE SANCTIONED
behavior changes (each honestly scoped in its own ledger entry): the
same-run post-close 1day derive (#1583 — previously deferred to the
next run), the learned-floor-clamped CLI planning (#1587 — a persisted
learned floor plans FEWER 1-minute windows by design; dry-run
byte-identical), and the fresh-foreign-lease pull REFUSAL (#1588);
every other item's fetch behaviour is byte-identical (the counter
split #1585 explicitly so); touched modules 100% line+branch).
Earlier: 2026-07-06 (DEC-RATE-METER-LIFECYCLE-001, MIGRATION.md #1593 —
the #1056 pull-rate CallMeter gains an explicit LIFECYCLE: registered by the
pull build sites at pull start, cleared by (a) `register_pull_meter`'s NEW
`dry_run=` gate (a dry-run DEREGISTERS — the dry-run GrowwClients carry a REAL
unpaced CallMeter, so the old "no `.meter` no-op" claims were FALSE), (b)
`BackgroundPullJob(on_run_end=SessionRateController.clear_meter)` in the job's
identity-gated finally, and (c) the OFF-job `pull_candles` spine's OWN paired,
identity-gated clear (round 4 — `clear_meter(expected=)` compare-and-clear via
`_pull_meter_scope`; supersedes round 2's route-level unconditional `finally`
clear, which a round-4 reviewer proved evicted a concurrent JOB pull's LIVE
meter whenever the direct route 400'd before registering — a malformed date;
the route's dry-run arm still passes NO holder) — so "no in-process pull
running ⇒ no meter registered" is a process invariant covering every
registration path, a raise-before-register clears nothing, and a concurrent
pull's later registration is never evicted (the 96-agent audit's C0 cross-pull
leak + the dry-run fabricated "flowing" closed; the dry-run gate's clear stays
unconditional with its concurrent-off-job-live-pull blast radius now honestly
DISCLOSED, not denied — round-4 MINOR x2). Plus: per-spine mutant-killing wiring tests (all four
register sites — the audit's C1/C2 surviving-mutant gap); a kind-aware
`/api/v1/pull/rate` probe (`BackgroundPullJob.start(job_kind=)` — a 2-4-day
dashboard SWEEP reads honest idle, never a site-wide red "stalled"; the
`_pull_live` duty-degrade probe DELIBERATELY stays sweep-inclusive, #920); a
900 s `BRIDGE_MAX_AGE_S` TTL in the merged projection (a SIGKILLed CLI's stuck
`pull_active=True` bridge file decays to idle instead of a permanent red; the
12 s-900 s died-mid-run alert window preserved); the false bx-rate-monitor.js
"obs rendered" claim + dead CSS removed; six stale pre-#1056 doc passages
reconciled + (round 3) the scoped "no in-process pull running ⇒ no meter
registered" wording swept across every remaining claim site and locked by a
grep doc-pin test. Observe-only — ZERO pacing change, fetch byte-identical; ENGINE
UNCHANGED — NIFTY/BANKNIFTY/SENSEX; touched modules 100% line+branch;
ruff/ruff-format/mypy --strict/pyright clean).
Earlier: 2026-07-06 (DEC-FORM-WINNER-PERSIST-001, MIGRATION.md #1581 —
the `&`-underlying (%26) form-winner is now DURABLE across pulls and processes:
one `schema_meta` row per underlying (`groww.form_winner.<key>` → the KEY-level
winning form STRING — the winner's ROLE applied to the key's own
`underlying_symbol_forms` tuple (moved into `symbol_form_preference.py`,
re-exported from `contracts.py`), so a candle-phase win and a metadata win
share ONE value space — review-fixed from the raw phase-local string) written
through the NEW `PersistentSymbolFormPreference`
(write-on-change, read-once-per-preference-instance single-flight — one
instance per pull run, MULTI-form keys only on
BOTH paths — the ~750 plain underlyings pay ZERO store I/O on order AND record
(review-fixed: the ungated persist wrote ~212 junk rows per pull process); the
seed `get_meta` runs OUTSIDE the map lock, bounded by the COMMON
`run_with_deadline` (2 s, `BRUTEX_FORM_WINNER_READ_DEADLINE_S`) so a wedged
sidecar read can never stall the fan-out (review-fixed: it ran INSIDE the map
lock), and the persist `set_meta` is bounded by the same guard (5 s,
`BRUTEX_FORM_WINNER_WRITE_DEADLINE_S` — round-2 review fix: a non-raising
wedged write would have hung the recording fetch worker forever and queued
every later persist behind the persist lock); store errors WARN-only —
a pull is never crashed; `BRUTEX_FORM_WINNER_PERSIST=off` is the write-side
kill switch; SUPERSEDES the "cross-pull persistence is a NAMED follow-up"
clause of DEC-SYMBOL-FORM-PREFERENCE-001), keyed by the PRIMARY F&O underlying
in EVERY phase (`resolve_expiries`/`resolve_contracts` gain the
`preference_key` normalization `candles.py` already had; the metadata pull
passes the CELL key, never the queried spelling form) and wired at every
production pull seam (ONE instance per `/pull-pipeline` run shared by the
metadata + historical phases; the standalone metadata/composite routes AND the
`/pull-historical` direct-selection grammar — the latter MISSED in round 1 and
wired by the round-2 review fix; ALL FOUR seams route-pinned as of round 5
(pipeline identity-sharing + dry-run store=None + metadata/composite
real-store persist — previously only the direct-selection seam was);
`run_groww_option_ingest`'s fresh-per-pass default stands down — also fixing
the within-one-pull amnesia across market buckets/until-clean rounds; a
dry-run threads store=None, byte-identical to the process-local base). The
two structurally identical set_meta/get_meta Protocols consolidate into ONE
`brutex/data/meta_kv.py` `MetaKV` (Golden Rule #11 COMMON;
`VendorFloorMetaStore` is now a back-compat alias). PLUS the live `&`-family
probe TOOLING: `brutex doctor --live --ampersand-probe` (stage 14, ≤64-call
budget) probes BOTH symbol forms of every master-DERIVED `&`-underlying
against BOTH F&O APIs through the kit's read-only client, writes the
`ampersand_verdict.json` evidence sidecar + per-call transcript lines, SEEDS
the durable map per-underlying through the ONE COMMON `record_winner` write
path (the `seeded` count READ-BACK-VERIFIED — a kill-switched/failed write is
never claimed, `amp.seed_shortfall` names the cause), and — iff DECISIVE —
writes the machine-readable
`groww.form_probe.verdict` stamp, whose ONLY writer in the repo is that stage
(test-pinned; `form_probe_status()` reads it back — missing/corrupt =
honestly UNVERIFIED, "verified" is DATA not prose; a data-beside-a-RAISED-
sibling name reads `*_UNCONFIRMED` — it seeds its winner but NEVER counts
CLEAR/decisive, so throttle-contaminated evidence cannot stamp). HONEST
(Golden Rule
#1/#10): the probe was NOT run for this change — no Groww credentials in the
build environment — so ZERO transcript exists, ZERO stamp was written, and
the #898 `%26`-encoding hypothesis stays vendor-UNVERIFIED byte-INTACT until
a decisive operator-run probe lands its transcript; the map itself needs no
probe (production pulls self-learn from ordinary fallback wins). ENGINE
UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no scope/vocab/rate change; touched
modules 100% line+branch).
Earlier: 2026-07-07 (DEC-SOAK-REAL-TIER-001, MIGRATION.md #1580 —
the REAL multi-hour soak tier + the honest reclassification of the ubuntu
nightly: `stress-nightly.yml` is a **~5-min ubuntu-24.04 REGRESSION GATE**
whose guard step now REFUSES a soak-scale dispatch (`STRESS_EVENTS >
200000`) — the retracted "24h soak via workflow_dispatch
`STRESS_EVENTS=10_000_000`" #208 claim was FALSE per Golden Rule #10 (the
60-min job timeout hard-kills a 10M run at ~1/17th done; #208 corrected in
place, strikethrough + CORRECTED notes). The real multi-hour soak is a
SEPARATE tier — NEW `.github/workflows/soak-selfhosted.yml` on the
self-hosted M4-Pro (§8): weekly cron (default 6 h) + `workflow_dispatch
(soak_minutes)` fenced 1..1200 (20 h — the self-hosted `GITHUB_TOKEN`
expires at 24 h, so a longer run's failure watchdog could never authenticate
to file its issue, SEC9-1), with a duration-DERIVED `timeout-minutes`
(`soak_minutes + 30 + soak_minutes/6` — fixed install slack +
duration-linear teardown, SOAK-R7-4) so the job can never outrun its own
clock, driving NEW `tests/stress/test_soak_sustained.py` (the NEW `soak`
pytest marker — excluded from the per-PR ci.yml stress step AND
stress-nightly via `-m 'not soak'`; the previously-dead `STRESS_DURATION_S`
knob is now actually honored) under TWO NEW pure observability modules:
`brutex/observability/process_probe.py` (the ONE COMMON cross-platform
RSS+fd source of truth — Linux `/proc/self/status` VmRSS primary,
`ru_maxrss` PEAK fallback documented as an absolute-CEILING backstop for the
macOS tier, `/dev/fd` fd fallback; `None` = honest abstain, never a
fabricated 0; `test_memory_growth._read_rss_kib` delegates to it) +
`brutex/observability/soak_drift.py` (`SoakDriftMonitor` — RSS-ceiling /
fd-growth / p99-latency-drift rules modelled on the in-repo
`pull_health.py`, routed to a NEW real-time `SweepAlerter.soak_drift`
Telegram alert). Ledger provenance: renumbered
#1512→#1534→#1535→#1536→#1537→#1540→#1541→#1552→#1566→#1580 across
eight-plus main collisions (#1580 chosen clear of live-main max #1560 AND the
in-flight PR #1264/#1244 wave holding #1558–#1567, the PR #1236 high-jump
precedent — the validator allows gaps); ships via the replacement-PR remedy
(the PR #1095→#1097 / #1220→#1235 precedent) on
`claude/soak-real-tier-rebuild` after commit 2757d005's `fix(test+docs):`
scope (`+` illegal in a conventional-commit scope, no override, history
rewrite forbidden) made the original `claude/serene-maxwell-0irkzn` (PR
#1231) permanently unmergeable. ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX;
no Groww scope/vocab/rate change; CI + observability + tests + docs only;
touched modules 100% line+branch.
Earlier: 2026-07-07 (DEC-FIB-BULLISH-RETRACE-001, MIGRATION.md #1560 —
the bullish Fibonacci retracement labels get their OWN PDL-anchored kernel
fields: `FibLevels.get_by_bullish_label` had aliased bullish 23/38/62/78 onto
the REVERSED bearish (PDH-anchored) fields, but Fib ratios are asymmetric
(0.236 + 0.786 = 1.022 ≠ 1.000) — the 23/78 rungs were wrong by 0.022×range
(565 paisa on the NIFTY 07-May-2026 golden anchor; the 38/62 pair coincides
only as an IEEE observation, never relied on). The shared `rust/trading-core`
kernel (DEC-SHARED-CORE-001 — Python grows NO math copy) adds
`retrace_bull_{23,38,62,78}_paisa = round_half_even(pdl + ratio·range)` (NO
bull_50 — the bullish midpoint IS the shared exact-integer `retrace_50`); the
`compute_fib_levels` FFI projection grows 15→19 (NEW `FIB_COMPUTE_LIST_LEN`,
bull tail APPENDED last) while `FIB_LEVELS_LEN` stays 15 — the FROZEN
`evaluate_with_levels` evaluator ABI (the two were an accidental 15-tuple
pun, now documented distinct constants; the evaluator zeroes + never reads
the bull fields). The catching test's unconditional `pytest.skip` is REMOVED
(bullish 78% chart-exact 24,258.00 = 2_425_800 paisa; frozen kernel goldens
[2_411_665, 2_415_417, 2_421_483, 2_425_800]) and the module docstring's
FALSE "verified bit-exact" bullish claim corrected honestly: 78 IS
chart-verified, 23/38/62 are formula-derived kernel-frozen NOT
screenshot-verified (Golden Rule #10). Byte-identity + NO indicator_version
bump — the recorded decision: `get_by_bullish_label`/`FibDirection` have
ZERO production consumers (census: tests only), the `near_fib_236/786`
vocabulary bits read the untouched bearish fields, and tests/golden incl.
the frozen 400-case vocab_golden_vectors.json pass with the fixture
byte-untouched. ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no
scope/vocab/rate-cap change; `fib_levels.py` 100% line+branch).
Earlier: 2026-07-07 (DEC-FUTURES-EXEC-TRANSLATION-001, MIGRATION.md #1543 —
the futures translation executor (Phase 5, R13/R14/R15) — an INDEX winner's
trades re-priced on the FRONT-MONTH-AT-TRADE-TIME futures contract, the options
executor's futures twin: NEW `brutex/futures/` (executor with the PINNED reject
order lot_size → cost_regime → no_contract → no_data → stale_bar[opt-in] and
the two pure date gates BEFORE any I/O; registry-calendar front-month
resolution via the SHARED `select_near_month` — never a last-Thursday formula,
lake-discovery fallback; exact-minute lake source with keep-FIRST dedup +
corrupt-partition quarantine-skip; R4 disk-verified preflight naming missing
months, no Phase-1 file edited; R45 independent Decimal-pipeline reference sim
+ per-run differential; R13 annotation-never-reranks projection + R42
content-hash-stamped atomic sidecar) + `brutex/costs/futures_costs.py`
(VERIFIED-rates-only: Groww min(₹20, 0.03%) futures brokerage + futures STT
0.02%@2024-10-01/0.05%@2026-04-01 encoded; futures ETC/NSE-IPFT/stamp + the
pre-2024-10-01 futures STT are explicit REFUSAL windows — the
DEC-ETC-REGIME-HISTORY-001 precedent, no runtime escape hatch; INDEX futures
COST-FREE per DEC-COST-SCOPE-INDEX-SIGNAL-ONLY-001, net == gross). R15
OPTIMISTIC=PRECISE fill only (open ± one tick adverse; volume-0 printed bars
FILL by default; gap-opens fill at the REAL open; the worst leg is DEFERRED,
structurally absent); STOCK winners gracefully DEFERRED naming the future
scope decree; a missing minute bar is a counted NO_DATA — never a nearest-bar
substitute. HONEST: not yet wired into cmd_sweep_all/the dashboard (the seams
are exposed; binding is the orchestrator's later step); R47 margin-ROI not
built (no in-repo SPAN history). ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX;
options path untouched (R12); 197 tests, all 8 new modules 100% line+branch).
Earlier: 2026-07-07 (DEC-TRACK2-COSTS-SPEC-RECONCILE-001 + DEC-BROKER-DOCS-AWSONLY-RECONCILE-001 +
DEC-GPU-GATE-RECONCILE-001 + DEC-CLOUD-AUTOFIX-RETIRE-001 + DEC-PHASE1-SIGNOFF-RECONCILE-001,
MIGRATION.md #1552–#1556 — the doc/spec/ops debt reconciliation pack, DOCS-first:
(1) the Track-2 costs spec's short-side slippage contradiction RESOLVED to the §8
ADVERSE rule end-to-end — spec §4 Step 1 rewritten, "# CHECK SIGN" deleted, the
production-unreachable `calculator.py::_apply_slippage` short SELL leg re-pinned
`entry_open + tick` → `entry_open − tick` (byte-exact test 10_005 → 9_995; BUY-only
DEC-EXEC-008 + intraday DEC-RISK-011 locks UNTOUCHED, `executor.py` not edited), the
§4.2 expiry state machine honestly labelled a fail-loud STUB in-spec, and the two
§4.2 pseudo-code drifts REGISTERED as §16 items 18/19 — never silently decided;
(2) the broker (Groww) token/credential docs refreshed to the AWS-only / fail-fast /
mid-run-re-read truth (#1065/#1072/#1092) — the stale `BRUTEX_TOKEN_BROKER_READONLY`
instructions + REMOVED local-mint-fallback prose replaced across the two runbooks, the
deploy guide, the minter runbook, `TOKEN_BROKER_CONTRACT.md` (TickVault claims left
"per its own repo"), ADR-0013/0014 Status-line superseded-in-part notes, and the
scoreboard's re-mint cell → re-read (`groww.token.reread`); DOCS-only, auth code CITED
never edited, rate caps untouched; (3) ADR-0004 GPU gate reconciled — Status PROPOSED
→ ACCEPTED (CPU-first arm EXECUTED + MEASURED #411/#413/#417), the never-run §6
tri-platform benchmark RETIRED as scheduled work + RETAINED as a dormant FAIL-CLOSED
precondition, mechanically pinned by NEW `tests/unit/test_gpu_gate_drift_pin.py`
(no MLX/CuPy/`BRUTEX_GPU_OFFLOAD` in `brutex/`+`rust/`; #1492-G3 pattern);
(4) the dormant cloud auto-fix snippet (claude_automation.md §3) RETIRED outright —
DELETE, never wired, a #1180-class privileged-trigger hazard — and the fork-PR redteam
sweep EXTENDED to comment-family triggers (`test_no_issue_comment_privileged_workflows`,
GITHUB_TOKEN carve-out; revival needs a fresh DEC + the ledgered precondition checklist);
(5) the Phase-1 sign-off checklist closed by SUPERSESSION — all 11 boxes ticked with
per-item citations, the "10 boxes" count defect fixed, the env-var trailer superseded by
#1065; NO operator ack pre-claimed (requested on the PR). ENGINE UNCHANGED —
NIFTY/BANKNIFTY/SENSEX; no scope/vocab/rate change; the ONE code delta lives inside the
DEC-EXEC-008-stubbed short branch, `calculator.py` stays 100% line+branch.)
Earlier: 2026-07-07 (DEC-RUN-IDENTITY-GUARD-PARITY-001 +
DEC-OPTION-DEDUP-BLOOM-VERIFY-PIN-001 + DEC-INDICATOR-TWINS-RUST-SINGLE-SOURCE-001 +
DEC-TRADE-SIM-U128-KERNEL-001 + DEC-EXECUTION-TRIO-DELETE-001,
MIGRATION.md #1546–#1550 (renumbered from #1541–#1545 after PR #1234 took #1541) — the 5-item engine cleanup pack (reapplied onto
current main after the original 2026-07-06 lineage was wiped), ONE change set:
(1) the combo `\x1f` reject-only guard is MIRRORED into both previously-
unguarded run-identity joins (`refinement_run_id` + `_walk_forward_run_hash`)
via ONE COMMON `strategies.combo.reject_unit_separator`, plus `allow_nan=False`
at both engine canonical-JSON sites (+ the stage1a schedule-hash third mirror)
— byte-frozen run-identity goldens (with an independent pip-blake3
payload-byte oracle proving `fno_math.blake3_hash ≡ blake3`) landed FIRST and
pass unchanged; LOW/defence-in-depth, NOT a live bug. (2) the option-dedup
bloom-before-exact-confirm ordering VERIFIED already correct — ZERO executable
change (AST-proven); no-phantom + bloom-negative-short-circuit pins added at
both public probe entries (`add_if_new`/`unique_option_bars`) — the PRODUCTION
consumer of the verified ordering is the engine sweep's
`ComboDedup(OptionDedupIndex)` dedup tier (`sweep_launch.py`/`cli.py`); the
original sink-path pin covered the since-deleted `OptionBarSink` orchestration
(removed on main by DEC-INGEST-AUDIT-WIRING-001 #1500 — dropped in the rebase),
with the mark-after-durable-write LAW recorded as history in #1547. (3) the
hand-synced Python `VwapState` +
`MarketStructureState`/`Trend` twins DELETED — the `rust/trading-core`
pyclasses are the SINGLE SOURCE (additive `last_swing_high/low` getters
delegating to the EXISTING core accessors, core untouched — TickVault-safe;
crate 0.3.0→0.3.1, so the LIVE `_indicator_version()` string moves with the
bump — NO math change, new runs re-key via `indicator_version` exactly as the
§2-Rule-4 formula intends; the frozen twin fixture + SHA never regenerated,
replayed Rust-only; P-06 preserved at strength; `MarketStructureSignal`
stays). (4) NEW `ResidentSweepSimU128` fno-math kernel via a width-generic
`MaskWord` core (the existing Rust suite passed UNMODIFIED — the
frozen-behaviour gate; native `Vec<u128>` FFI per the recorded pyo3-0.29-abi3
probe transcript) — >64-bit masks/bar_bits are now Rust-served, parity-pinned
vs the `simulate_trades` oracle, with lazy wide-kernel promotion (reasons
re-scoped `bar_bits_beyond_u128`/`mask_beyond_u128`/`wheel_missing_u128` +
`wide_kernel_promoted`); HONEST: future-proofing, production-UNREACHABLE at
today's 60-bit vocabulary — every currently-legal u64 input byte-identical,
the frozen u64 golden corpus untouched, u64 bench non-regression measured.
(5) the deprecated `IndianCostModel`/`ExecutionSimulator`/`BacktestDriver`
trio DELETED with its 8 test surfaces + golden fixture + dead ci.yml gates
(grep-proven zero production consumers; `types.py` kept whole —
`InstrumentMeta` live — and `errors.py` semantics untouched, its five
subclasses honestly ledgered as orphaned; landed as ONE atomic commit — the
wiped first lineage's two-commit accident is recorded as history in #1550) —
the pack's ONLY behaviour-visible item (import surface). ENGINE UNCHANGED —
NIFTY/BANKNIFTY/SENSEX; run identity bit-identical for all currently-legal
inputs (golden-proven); no scope/vocab/rate change; touched modules 100%
line+branch; ruff/ruff-format clean, mypy `--strict` clean, pyright at the
pre-change baseline; cargo suites green (fno-math 565, trading-core 162);
cargo-mutants evidence partial under the ledgered #1511 Rust timeout —
stated, not papered over).
Earlier: 2026-07-07 (DEC-TEST-APPARATUS-DEBTS-001, MIGRATION.md #1541 —
the test-apparatus debt pack: nine long-ledgered apparatus gaps closed as ONE
change set, each landed fix-the-violation or measured-green: (a) a repo-wide
doctest gate (pkgutil walk, measured 55-example teeth floor; the 3 broken
tf_resampler examples fixed — the ONLY brutex/ source edit, docstring-only);
(b) EXPLICIT hypothesis determinism on CI (the fully-explicit repo-registered
`ci` profile, `HYPOTHESIS_PROFILE=ci` in ci/verify-m4pro/mutation; HONEST:
GHA was ALREADY implicitly derandomized by hypothesis ≥6.113's built-in
auto-loaded `ci` profile — the real sanctioned flip is the reverse: no-env
scheduled arms (stress-nightly et al.) now deliberately load the random
default, overriding the auto-load; the false "500 examples /
HYPOTHESIS_SEED" pyramid claim corrected; non-tautological pins — AST
source pin + CI-env-stripped subprocess selection pin);
(c) a fix-reversion ci-ok hard gate — a fix:-titled PR's shipped tests must
FAIL with the fix reverted to merge-base in a scratch worktree (closes the
#1154 surviving-fixture class); (d) a citation ACCURACY scanner (ci-ok hard
gate, push+PR) — file:line cites in the normative docs resolve Tier 1/2/3 and
MIGRATION #NNN refs resolve; the live 29-finding census (13 in this file's §2
Rule 6 / §6 rows — the 4th #609/#669/#846 recurrence) reconciled to fixpoint
in the same change (round-2 honesty: fixpoint = scanner-green, NOT per-line
proof — two §6 rows the any-anchor×any-segment Tier-3 envelope blessed while
stale, plus one anchor-appeasing wrong-target re-point, were re-fixed on the
merits; the envelope is now stated plainly in the scanner's residual-limits
docstring, and out-of-root `../`/absolute cites now fail loudly instead of
reading + blessing a non-repo file); (e) a DEC-ID appropriateness heuristic in
validate_dec_ids.py (source-touching non-exempt PRs must cite a resolving
DEC-ID or touch the ledger; STANDARDS row 18 stays PARTIAL); (f) jscpd 3%→1%
(measured 0.37% baseline); (g) the pandas import ban widened to ALL of
brutex/ (zero offenders; the `brutex/*.py` + `brutex/**/*.py` pathspec PAIR —
round-2 caught git's `**` glob alone missing the 12 top-level modules); (h) P-10/P-12 promoted to real cost-calculator
properties (STT sell-leg-only; brokerage = the VERIFIED flat ₹20/order law —
the stale min(₹20, 0.03%) wording corrected); (i) the sweep worker-scaling
A/B bench enumerated in bench-m4pro.yml — EXECUTING GREEN on the live
registered M4 Pro runner (in-PR reconcile: the first real run failed on the
missing polars [data] extra, fixed a54c9a0a; re-run fully green, run
28851211762 — step measured 20 s vs the generous 25-min timeout; VERDICT:
KEEP CAP=8). ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no
scope/vocab/rate change; apparatus/CI/docs only).
Earlier: 2026-07-07 (DEC-MOVERS-PREVCLOSE-CONTIGUITY-001,
MIGRATION.md #1539 — the /movers prev-close calendar-contiguity honesty guard:
a previous-day close/OI baseline that provably skipped a nearer NSE session (a
lake hole — the confirmed repro printed 2.2822% where the true Friday-based
figure is 0.2033%) is now STAMPED (`prev_close_session`/`prev_close_contiguous`
+ the INDEPENDENT `oi_baseline_*` twins), warned ONCE (capped 20/build, deduped
per (symbol, kind)), and marked " *" on the screen — while the SERVED VALUE is
untouched in every branch: NEW COMMON `calendars/nse.previous_trading_session`
(strict back-walk, coverage-floor ABSTAIN — never a guessed session; Muhurat =
NON-session documented limit [SUPERSEDED for the movers VERDICT layer by
DEC-PREVSESSION-MUHURAT-COHERENCE-001, MIGRATION.md #1649b: the walk still
treats Muhurat a NON-session, but a found-after-expected Muhurat baseline now
reads contiguous-True + `prev_close_muhurat`/`oi_baseline_muhurat` — a healthy
lake, never a false hole]), enriched resolvers `resolve_prev_close`/
`resolve_prev_oi` with byte-identical `.value` seams over the historical
`_prev_day_close`/`_prev_day_oi`, ONE pure `prev_baseline_provenance`, and
EMIT-ON-FALSE row projection (contiguous + abstained payloads byte-for-byte
the pre-fix wire, golden-pinned both arms + a frozen additive-key-NAME set).
HONEST: makes the wrong %change VISIBLE, does not fix the number — re-pulling
the missing session does; `preopen_report._previous_trading_day` was NOT
refactored there (named follow-up DEC-PREOPEN-PREVDAY-COMMON-001 — since
ADVANCED by main's #1634 and CLOSED by MIGRATION.md #1649a,
DEC-PREOPEN-PREVDAY-COMMON-002). READ-ONLY dashboard — ENGINE
UNCHANGED — NIFTY/BANKNIFTY/SENSEX; touched modules 100% line+branch).
Earlier: 2026-07-07 (DEC-MOVERS-SPOT-VALUE-TF-INVARIANT-001,
MIGRATION.md #1538 — the `/movers` SPOT "Top Value" (turnover) is now
TIMEFRAME-INVARIANT: it is re-sourced from the RAW 1-minute session frame
(post as-of cut, PRE-resample) via the NEW `spot_session_value_paisa`, exactly
mirroring the already-TF-invariant futures/options `_read_future_session`,
instead of the resampled display-timeframe bars — so the SAME (symbol, day,
as-of) turnover no longer CHANGES with the selected display timeframe
(reproduced NSE-NIFTY 2024-01-02 whole-day: value 1m 19583300 ≠ 5m 19833500 ≠
1day 20100000 paisa, now all byte-identical to the raw-1m answer). Three edits,
ALL in `brutex/dashboard/movers_snapshot.py` (ZERO other source files): extract
the shared raw-1m read+cut into `_read_cut_1m_frame` (§11 COMMON —
`_read_session_bars` becomes a byte-identical wrapper, preserving the
DEC-PIPELINE-AUTOPREBUILD-001 stored-vs-resample display-bar equality), the NEW
no-`interval`-arg `spot_session_value_paisa` (HLC3 × volume via the ONE COMMON
`session_traded_value_paisa`, per-bar `round_money` 2dp BEFORE × volume,
`Decimal(str(...))` matching `_read_future_session`, `None` on missing 1-minute
— never fabricates, Golden Rule #1), and a `value_paisa: int | None = None`
injection into `compute_snapshot` (`None`/omitted falls back to the legacy
display-bar reduction byte-identically, so the existing 320000/318660 goldens
pass UNMODIFIED — the injection-gate mutant killer) wired from
`compute_spot_candidates` inside the existing `skip_errors` try. HONEST
envelope: a spot-ranking CORRECTNESS fix (Top Value becomes a genuine session
property, mirroring futures/options) that CHANGES the displayed spot Top Value
on coarse timeframes versus today — a SANCTIONED correction, not a silent
break; value(1minute) byte-unchanged; a mid-session as-of legitimately keeps
1day whole-day (the bar IS the day), so invariance is scoped PER shared
effective as-of; disclosed I/O — one extra month-bounded raw-1m read on the
daily/stored-first paths + a double-read on the resample-fallback path (bounded
O(one month), candidate-cache-memoised per write-generation); futures/options
value output BYTE-UNCHANGED (`session_traded_value_paisa` +
`filter_to_ist_minute` + the `_read_future_session` 6-tuple untouched; diff-stat
shows ONLY `movers_snapshot.py` in source; the F&O suites re-run UNMODIFIED
green). ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; READ-ONLY dashboard ranking;
no lake/schema/scope/vocab/rate change; touched module 100% line+branch;
ruff/ruff-format/`mypy --strict`/pyright clean.)
Earlier: 2026-07-06 (DEC-CLI-GREEKS-ENRICH-001, MIGRATION.md #1536 —
the CLI `brutex fetch` pull path now runs the SAME fenced Greeks-enrichment
pass the dashboard runs — post-drain Seam A (fetch lane drained + window
ledger persisted FIRST, single-flight lock still HELD), default-ON with the
fail-loud `BRUTEX_CLI_GREEKS_ENRICH` kill switch, inert under
`--dry-run`/`--inspect` via `_no_persist`, halts (`LakeWriteHalt`/
`GreeksEnrichPoolBroken`) alert-then-re-raise, a fenced failure NEVER fails
the pull — and a NEW `brutex enrich-greeks` subcommand delivers the #1188
bulk v1→v2 re-enrich (exit 0 iff fully clean — cron-visible). ONE COMMON
wheel-free `brutex/data/greeks_enrich_pass.py` owns the fence (MOVED from
`historical_pull`, whose `_enrich_greeks_from_minute` is now a thin
delegator — dashboard behavior byte-identical, outcome types aliased to the
SAME objects) + the chain-union lake-walk target discovery (CE/PE only,
zero Parquet opens). HONEST: NOT O(1) — discovery is an O(option partition
dirs) walk and the pass re-READS the scoped stored bars every run (the
stamp-driven `already_current` gate makes re-runs cheap by skipping writes
+ solves, never reads); the CLI L2 heal seam landed INDEPENDENTLY on
main as DEC-CLI-LAKE-HEAL-L2-001 (#1493) and the merged fetch path runs it
BEFORE this enrich seam — the dashboard heal-L2-before-Greeks-enrich
ordering law holds on the CLI too; the six-step strict gate
(DEC-PULL-SIX-STEP-ORDER-001 #1527, merged in flight) sits BETWEEN the
dashboard's own F&O L2 heal (`_heal_lake_after_pull`) and the enrich
delegator inside `pull_contract_candles` — `_GREEKS_STRICT_WITHHELD` is a
distinct instance of the COMMON `GreeksEnrichOutcome`; the CLI never calls
that seam — its Seam A enrich invokes the COMMON fence directly, so the
strict gate cannot touch it. ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX;
F&O lake REFERENCE_ONLY; no scope/vocab/rate change; touched modules 100%
line+branch).
Earlier: 2026-07-07 (DEC-MOVERS-ASOF-LATEST-DEFAULT-001, MIGRATION.md #1535 —
the `/movers` Spot default as-of TIME resolves to the SAME latest available
minute across EVERY timeframe, so 1m ↔ 2m ↔ 3m ↔ 3hour ↔ 1day always show the
identical latest close; READ-ONLY dashboard, ENGINE UNCHANGED —
NIFTY/BANKNIFTY/SENSEX).
Earlier: 2026-07-06 (DEC-BENCH-ABS-BASELINE-001, MIGRATION.md #1534 —
the committed per-hardware ABSOLUTE benchmark baseline + fail-loud compare gate
on bench-m4pro + mechanical no-silent-re-baseline governance (closes the
STANDARDS_100PCT.md §11 P1 "benchmark baseline JSON + `--benchmark-compare
--fail` gate" backlog item, the deviation from the literal flag ledgered): NEW
pure-stdlib zero-brutex-import `brutex/observability/bench_baseline.py` (the
comparator/schema/capture/governance core + the ONE hardware-key format law) +
`hardware_fingerprint`/`hardware_key` on the COMMON `config/hardware_envelope.py`
+ the env-gated 6-entry measurement suite (`tests/bench/test_absolute_baseline.py`
over `_abs_baseline_registry.py`, house best-of-N-MIN idiom) + committed
baselines at `tests/bench/baselines/<hardware_key>.json` (`tolerance_pct`
structurally capped so a 2x slowdown can NEVER pass any governed config; a
MORE-than-4x speedup FAILS as SUSPICIOUS_SPEEDUP, more-than-2x WARNs "baseline
stale") + `scripts/ci/bench_baseline_gate.py` (validate/compare/capture/govern/
key; exit 0 ok-or-LOUD-abstain / 1 numeric with ONE bounded fresh-process
re-measure / 2 structural never-retried). bench-m4pro gains
measure→compare→capture→artifact + a GitHub-hosted `bless-baseline` job (the
ONLY contents/PR-write holder — the candidate travels runner→artifact→verbatim
API commit with no edit surface); ci.yml's REQUIRED `bench` job gains schema
`validate` on every run + the `govern` entry-cell diff gate on PRs with LIVE
`gh api` provenance-run verification pinned to THIS repository. GOVERNANCE LAW
per cell class: a MODIFIED cell needs a NEW ledgered DEC id + the literal entry
key + the literal value token + a FRESH live-verified run; an ADDED cell rides
the standing bootstrap authorization; a REMOVED cell / top-level change needs a
ledgered note; a DELETED file needs its filename in the added lines; NO
override env. BOOTSTRAP: ZERO measured numbers ship (`tests/bench/baselines/`
holds only README.md); the first baseline is measured by the REAL M4 Pro and
auto-blessed via a bot PR the operator nudges + merges (GITHUB_TOKEN PRs do not
trigger CI; NO PAT). HONEST: bench-m4pro stays NON-required (a regression is
caught post-merge as a red run + issue, not a merge blocker); the gate is
dormant until the runner is online AND the first bless merges; the gate catches
strictly-GREATER-than-tolerance drift ONLY (strict `>` boundary law — a
regression exactly AT tolerance passes, test-pinned) — at-or-below-tolerance
drift accumulates until a ledgered
re-bless (the 1.1x ratio tiers + the single-canary-op 50 M/s floor are
complements, NOT a sub-tolerance net); a STANDING red — numeric
SUSPICIOUS_SPEEDUP or STRUCTURAL alike — dies at compare before a dispatch
capture can run, recovery is removal-first (`docs/runbooks/bench-baseline.md`).
ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no scope/vocab/rate change;
`bench_baseline.py` + the touched `hardware_envelope.py` 100% line+branch).
Earlier: 2026-07-06 (DEC-LEDGER-DELTA-O-PAGE-001, MIGRATION.md #1533 —
mid-pull ledger-read O(page) discipline restored: (a) the /coverage/windows +
pull-progress DuckDB fast paths now FOLD pending window-ledger deltas
latest-wins IN SQL — NEW `lake_read.dedup_qualify_latest_sql`/
`dedup_latest_source_sql` (the keep-LAST twin of `dedup_qualify_sql`; bound
base-path equality ordering, never LIKE — the `/deltas/`-path + `'l'>'d'`
filename traps live-verified dead; demotions survive, filters apply OUTSIDE
the fold so no ghost rows) assembled at ONE `db_view._ledger_scan_source`
seam (per-attempt re-listing = the save-time prune-race self-heal); the count
caches key on the composite (base-mtime + delta-name-set) stamp token at
generation=0 (delta appends AND cross-process CLI writes invalidate;
mark_dirty storms never thrash); the schema probe stays BASE-only (legacy
12-col lakes defer wholesale — the union_by_name NULL-fill crash is
unreachable; base-less delta-only lakes ride the fast path); a
persistently-bad delta degrades to the legacy fold / Python-fold counts,
never a 500 or blanked phases; SANCTIONED: mid-pull rows converge to the
steady-state DuckDB order + counts_since_pull_start now appears mid-pull.
(b) the three db_view ledger record caches collapse onto ONE reused
`MoversSnapshotCache` (single-flight — 8 concurrent cold pollers pay 1 parse;
the #920 adaptive floor EXTRACTED to `summary_cache.effective_adaptive_floor`
and delegated byte-equal, cap 120 s; stale serves bounded + logged; the
base-less delta-only /db grid now renders). (c) /results keys on NEW
`lake_revision.sweep_results_revision` (sweeps dir digest + the audit chain's
file stat — a SWEEP_START append now invalidates; probe-None computes fresh
UNCACHED). (d) `build_sweep_leaderboard` splits into the revision-cached
`load_sweep_scan` (frozen `SweepScan`, ALL honesty fields) + the
byte-order-preserved rank (golden vs `ResultsStore.top_k`); `_snapshot_eras`
streams `iter_entries` (O(1) memory; audit.py untouched). (e)
`bx-rate-monitor.js` + `bx-liveproof.js` pause polling in hidden tabs (the
v29 `document.hidden` guard + visibilitychange catch-up). (f) NEW
`tests/bench/test_ledger_delta_o_page_o1.py` — 7 measured pins. HONEST: the
fold is ONE O(total rows + delta-file footers) columnar QUALIFY per FRESH
stamp — NOT literal O(page); delta compaction + the AuditCursor resume are
NAMED future work. ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; steady-state
(zero-delta) SQL text + payloads byte-identical (spy-pinned); touched modules
100% line+branch).
Earlier: 2026-07-06 (DEC-SWEEP-EXIT-INDEX-FASTPATH-001 + DEC-SWEEP-RUST-METRICS-DEFAULT-ON-001 / ADR-0017,
MIGRATION.md #1531–#1532 — the sweep-metrics Rust tail is now ON BY DEFAULT:
`SWEEP_RUST_METRICS_DEFAULT=True` (the exact "separate later ADR" flip #1186
deferred; revert = ONE constant; `BRUTEX_SWEEP_RUST_METRICS=0` is the
per-process operator kill switch — `_FALSY` beside `_TRUTHY` on
`sweep_lookahead`, consumed via its public `env_flag_tristate`, never
copied), licensed by TWO safety layers —
ORIGINALLY authored + proven fully green under the prior default-OFF before
the flip, though the pushed branch is ONE reconstructed post-wipe commit so
no shipped-first commit boundary exists (see ADR-0017 Status / #1532): (a) the
exit_idx-native FAST PATH (#1186's named follow-up, DONE) — both trade
simulators return each mask's certified exit-bar indices
(`simulate_trades_with_exit_indices` / `simulate_masks_with_exit_indices`
over the NEW stdlib-only COMMON `brutex/engine/exit_index.py`:
`build_ts_index` consolidating the three inline dict copies +
`bars_strictly_increasing` producer certificate + `exit_indices_consistent`
consumer belt) so the kernel arm consumes them O(trades) with NO per-mask
O(N_bars) `exit_ts`→index dict build (an inconsistent stream DEMOTES to the
dict, never to the Python oracle — results byte-identical; measured 6.1×
sandbox tail ratio, bench-gated ≥1.15×; the sweep spine threads the pairs
through the ONE `_iter_mask_metrics`; `_reattribute_chunk_failure` stays on
the oracle); and (b) a per-process BURN-IN SELF-CHECK — the first 16
kernel-served masks are bit-compared (`struct.pack` floats, signed zeros
included) against the Python `_drawdown_stats` oracle; a mismatch SHIPS THE
ORACLE VALUE and AUTOREVERTS the kernel for the process
(`kernel_autoreverted` ERROR + latch; `kernel_disabled` short-circuit). The
frozen `_GOLDEN_CONTENT_HASH`/`_GOLDEN_RUN_ID` stay UNCHANGED — the default
(no-env) arm reproduces them byte-for-byte with exactly ONE engaged event
(golden-pinned; a burn-in-poisoned kernel lands on the pinned bytes with one
autorevert event). ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no
scope/vocab/rate change; whole-engine unit gate 100.00% line+branch (1928
tests); ruff/ruff-format clean, mypy `--strict` clean, pyright at the
pre-change baseline).
Earlier: 2026-07-06 (DEC-PULL-UI-STALENESS-001 + DEC-PULL-HEADER-SUBUNIT-001,
MIGRATION.md #1529–#1530 — the pull-UI staleness pack, FRONTEND-ONLY (zero Python
source change; tests + docs excepted): both poll surfaces (the site-wide
`bx-rate-monitor.js` widget + the `/universe` pull-progress header) gain a
CLIENT-clock "updated Xs ago" readout on a ~1 s ticker (amber → LOUD RED explicit
"STALE" past justified thresholds — widget warn 10 s / red 30 s = 10× cadence =
backoff cap; universe warn 5 s past the retained 600 ms quick-retry envelope /
red 15 s) + a SELF-RECOVERING poll (fetch rejection AND non-ok AND malformed body
all counted failures; quick retries then 3 s→30 s capped exponential backoff,
NEVER gives up — the 2026-07-06 38-minute frozen-tab incident fix; the bounded
give-up that painted "live progress feed dropped" via innerHTML and stopped
polling forever is REMOVED, its pinned tokens kept with genuine semantics; a
non-ok poll can no longer settle the header as "— nothing to pull"; the stale
state clears automatically on the next successful poll), and the `/universe`
header + a new `#subunit` counter show per-expiry sub-unit progress ("expiry
30Sep25 · sub-unit N this unit") derived ENTIRELY client-side from
`s.current`/`s.completed` transitions (distinct-label count per completed/total
anchor, Set-capped 4096; the `data/fno_symbol.py` grammar mirrored right-anchored
so hyphenated underlyings never confuse the parse; spot/empty labels degrade to
omission, never garbage). textContent-only DOM; substring tests pin PRESENCE of
the JS, not execution (honest envelope — no headless JS runner); ZERO backend
change; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX).
Earlier: 2026-07-06 (DEC-O1-CLAIMS-HONESTY-001, MIGRATION.md #1528 — the O(1)-claims honesty +
preopen-discovery pack: the /reports preopen symbol discovery rides the COMMON #896 generation-keyed dataset
cache (per-basis keys, warmed post-pull off the warm's one walk, pullability deliberately live), FNO pruned +
interval-bounded at walk descent (sanctioned: F&O contract partitions no longer report symbols), the manifest
rebuild always stamps the PRE-walk ledger token (mid-walk race → the stale-stamped base still LANDS — it is
record_dataset's write-side convergence seed, the round-6 restore of an interim SKIP design — plus one
observe-only db_view.manifest_rebuilt_stale INFO; the load-side token gate stays the freshness authority),
welcome.html + Golden Rule #5 reworded to the TRUE two-tier O(1) enforcement of
tests/bench/_o1_tiers.py (robust 3–10× shared-CI / strict 1.1× M4-Pro via BRUTEX_M4PRO_STRICT=1 / measured 3×
cache-tier — words only, NO gate weakened), and two new bench pins (u128 two-limb mask + orchestrator audit
append). ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; touched modules 100% line+branch).
Earlier: 2026-07-06 (DEC-PULL-SIX-STEP-ORDER-001, MIGRATION.md #1527 —
the operator's STRICT six-step pull order: every Groww pull executes
INDICES-SPOT → STOCKS-SPOT → INDICES-FUTURES → STOCKS-FUTURES →
INDICES-OPTIONS → STOCKS-OPTIONS (empty groups skipped), enforced at the TWO
existing execution chokepoints with ONE pool/controller build each — an
always-on `ingest_common.partition_spot_steps` group loop INSIDE
`pull_all_spot` (the COMMON `PullStep` IntEnum, value==rank 1-6;
`progress_counts(symbols=)` group-scoped until-clean) and a 4-key
`(ledger-market, is_index)` F&O bucket sort with STEP-major until-clean
(`_contract_market` KEPT verbatim — ledger namespaces byte-unchanged, no
re-pull; unresolved underlyings order stock-side, WARNed + counted, never
dropped); post-steps (Greeks → prebuild → movers) fire only after ALL
applicable steps SETTLE (settle-based, never `bars > 0`) — cleanset default
byte-compatible with DEC-PREBUILD-CLEANSET-001, NEW opt-in
`BRUTEX_PIPELINE_STRICT_GATE=1` all-or-nothing barrier (fail-loud parse
resolved once at launch → clean 400; pure `step_settle_verdict` +
`PriorStepsVerdict` threaded to the Greeks seam — a withheld pass reads
"withheld (strict gate) — re-attempted next pull", never FAILED). COMMON
`pull.step.started/settled/halted` events + an observe-only thread-safe
`StepOrderMonitor` (`raise_on_violation=True` is the test oracle); the
2026-07-05 log-shaped ordering regression + a fetch-count parity bench pin
"reorders, never re-fetches". HONEST: an ORDERING change, NOT a speedup —
~seconds added per pull (bounded analysis, not measured); in async-pipeline
mode a step's k→k+1 settle is PRODUCER-side (fetch-issue order strict; tail
writes drain terminally but are INVISIBLE to the producer-fed
`StepOrderMonitor` — `pull.step.late_write` observes only producer-side
writes and stays 0 on a healthy async pull; submit-time step stamping
through `FetchPipeline` is a named follow-up, NOT claimed)
while the POST-step barrier is fully drain-guarded; the mass-empty
breaker's trip evidence is now per-STEP-GROUP pass on BOTH spines — F&O
per-(market, index/stock) BUCKET, SPOT per indices/stocks group (a
disclosed narrowing, #1527 R6+R9 — evidence resets at the group boundary;
the ~4-series INDICES-SPOT group on an incremental pull is below every trip
threshold, so throttle-poisoned ENGINE index windows can settle SKIPPED
before the stocks group's breaker trips — remediation `brutex
reopen-skipped`; thresholds/canary/bounded-settle machinery unchanged); the
CLI `brutex fetch`
spot path is the NAMED follow-up DEC-PULL-SIX-STEP-ORDER-CLI-001, NOT
claimed. ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; F&O lake REFERENCE_ONLY;
touched modules 100% line+branch).
Earlier: 2026-07-06 (DEC-ETC-REGIME-HISTORY-001, MIGRATION.md #1524 —
the options exchange-transaction-charge (ETC) regime tables STOP charging a
fabricated rate in the pre-2024-10-01 window: the two `(date.min, <current
rate>)` UNVERIFIED placeholder rows are DELETED and replaced by explicit
rate-less REFUSAL rows (`RegimeRow.rate_bps_x100 = None`) — the ONE COMMON
`rate_for_date` raises the NEW typed `UnverifiedCostRegimeError` (window +
identified circulars + remediation), and the options executor SKIPS-AND-COUNTS
per trade (`RejectReason.COST_REGIME_UNVERIFIED`,
`OptionsRuleResult.n_rejected_cost_regime_unverified`, per-trade
`options.reject.cost_regime_unverified` WARN + ONE aggregate
`options.sim.unverified_cost_window` WARN + a CLI leaderboard footer), exactly
like the LOT_SIZE_UNKNOWN precedent — the sim completes, is loud, and NEVER
invents a rate. VERDICT BASIS (the 2026-07-06 4-strategy fetch round,
HARD-LINE Golden Rule #1): ZERO official-trace retrievals (every official
host + web.archive.org egress-blocked) — the six pre-boundary regimes were
reconstructed to circular-number precision but reconstruction is NOT
verification, and they are structurally member-monthly-turnover SLAB ladders
with no honest flat per-trade equivalent. Exactly TWO rate rows survive (NSE
0.03503% / BSE 0.03250%, both eff. 2024-10-01, VERIFIED on the pre-existing
COSTS_VERIFIED §2 rows 3-4). NO runtime escape hatch; override paths are DATA
only (a cited RegimeRow, or the ADR-gated top-slab-ASSUMED path). SANCTIONED
HONEST BREAKAGE: options trades 2021-01-01→2024-09-30 are now counted refusals
(previously silently priced at the invented current rate — likely
UNDER-costed); the honestly-priceable window is [2024-10-01 → today] until
circulars land; the FA73061 forward alert (NSE split 3,552+1/cr eff.
2026-03-01, total unchanged) stays UNVERIFIED + NOT encoded (§20 row 6,
highest-priority fetch). Costs + options-executor + CLI-footer + tests + docs
only — ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate-cap
change; brutex.costs + executor 100% line+branch; the CLI leaderboard
renderer's pre-existing `# pragma: no cover` LIFTED after the round-1 review
caught the honesty footer riding it with ZERO executing tests —
`_render_options_leaderboard` is now directly unit-tested at 100% line+branch
with fail-loud direct attribute access instead of the silent `getattr`
default, tests/unit/test_cli_options_leaderboard.py).
Earlier: 2026-07-06 (DEC-PULL-RATE-BRIDGE-NONBLOCK-CACHE-001, MIGRATION.md #1522 —
the `GET /api/v1/pull/rate` CLI-bridge file read moved OFF the uvicorn event loop: a NEW
`brutex/dashboard/rate_bridge_cache.py` `RateBridgeCache` memoises the decoded bridge
snapshot behind a 2 s TTL + an `mtime_ns:size:ino` stat content token (NEW public
`lake_revision.rate_bridge_revision` over the shared `_stat_token` core, `include_ino`
opt-in — every pre-existing token byte-unchanged), refreshes post-TTL under the COMMON
`run_with_deadline` freeze guard (0.5 s; stat AND read inside the guard) with a
NON-BLOCKING single-flight (followers serve last-known immediately; at most ONE abandoned
daemon per outage, late-return self-heal; a guard SPAWN failure — `Thread.start()` under
thread exhaustion — is caught by the owner: flag released, ONE `refresh_spawn_failed` WARN
per outage, served `unverified`, next poll retries — never a route 500 nor a permanent
wedge, the hostile-review MAJOR fold; KeyboardInterrupt/SystemExit propagate with the flag
released), and is called via `run_in_threadpool` through a
`build_app(rate_bridge_cache=)` injection seam. TWO ADDITIVE payload keys stamped in ONE
place: `bridge_source` ∈ {live, cached, unverified, unwired} + `bridge_probe_age_s` — no
stale illusion (the cache stores the SNAPSHOT, never the view; ages recompute per request
from its own `wall_time`, TTL 2 s < STALE_AFTER_S 12 s test-pinned). Every pre-existing
payload key byte-identical; teeth verified (the same wedged read blocked the pre-change
route 8.02 s, bounded < 2 s fixed). HONEST: liveness/latency only, no throughput claim;
the wedged daemon cannot be killed (#999 stance); the route-level bound holds GIVEN a free
AnyIO worker token (the DEFAULT 40-token `run_in_threadpool` limiter is shared with the
sibling UNdeadlined lake offloads — pool-wide wedge protection is a named follow-up); the
CLI WRITE-side publish hang is a named follow-up. READ-side dashboard only — ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX;
`rate_bridge_cache.py` + `lake_revision.py` 100% line+branch).
Earlier: 2026-07-06 (DEC-GREEKS-SPOTJOIN-STOCK-001, MIGRATION.md #1482 —
the Greeks spot-join drift-recovery ladder gains the evidence-grounded STOCK
arm, CLOSING the SPOTJOIN-2 named follow-up of #1188: NEW
`brutex/data/spot_drift.py` scans the REAL `bars/<exch>/{INDEX,CASH}` symbol
dirs once per enrich pass (O(dirs), parent-only, partial-tolerant) and
verifies each against the instrument master's IDX/EQ `symbol_name`s
(`<lake_root>/instruments.duckdb`; empty master → zero lake I/O) under ONE
pure fold (`lake_keys.stock_drift_fold` — upper + drop space/hyphen, the
EXACT mirror-pinned twin of `universe.normalize_underlying_symbol`; the
own-`<EXCH>-` prefix strip is DISK-SIDE ONLY with raw-fold precedence). An
entry needs a real dir + a UNIQUE master fold-match — a ≥2-master fold is
POISONED even with one dir (capped `spot_drift.ambiguous_master` WARNs);
same-instrument fragments are ALL offered ordered (master-exact first, then
lexicographic — the probe decides, so a dataless clean dir never shadows a
data-carrying fragment). The map rides `spot_recovery_candidates(stock_drift=)`
as the APPENDED last rung (2-arg calls byte-identical, prefix invariant
pinned), threaded per-pass-stably through `_default_seams`/
`_GreeksWorkerContext` (plain picklable dict; workers never scan; sequential
≡ parallel bytes golden-pinned); the ONE `spot_key_recovered` log gains an
additive `rung∈{alias_ladder,stock_drift}` field. No master / confirmed read
error / no drift → the honest NO_SPOT (never a fabricated spot, never a
failed pull; MemoryError always re-raises raw); the held-open-store
integration pin proves the loader works beside a live dashboard
`InstrumentStore` handle. HONEST: NO `%26`→`&` recovery (stored-key encoding
UNVERIFIED — the lake_keys pledge stands); read-time recovery ONLY —
`lake_heal`/`detect_legacy_drift` untouched; the `/db` moneyness twin is the
NAMED follow-up DEC-DB-MONEYNESS-STOCK-EVIDENCE-001 (until it lands a drifted
stock lake shows recovered Greeks but `/db` moneyness UNKNOWN). ENGINE
UNCHANGED — NIFTY/BANKNIFTY/SENSEX; `lake_keys.py`/`spot_drift.py`/
`greeks_enrich.py` ALL 100% line+branch).Earlier: 2026-07-05 (DEC-STORED-DATA-VALIDATION-001 + DEC-MUTATION-ALERT-CANCELLED-001,
MIGRATION.md #1510–#1511 — the stored-data validation pack, the round-2
adversarial audit's confirmed defects fixed as ONE change set: (a) price
POSITIVITY is enforced at the ENGINE READ BOUNDARY
(`feed/replay.py::arrow_table_to_bars`, which gains `source=` + names the
partition + bar ts on a corrupt row), NOT the universal `CandleBar` constructor
— a zero/negative but internally-ORDERED legacy stored bar (reproduced: day_low
−1000 into Stage-1A) now raises LOUDLY when READ into the engine, while the bar
container stays permissive so the legitimate downstream corrupt-input
classifiers that build a non-positive bar on purpose (Greeks BAD_INPUT, the
executor `require_tradeable_price` guard, the Rust trade-sim out-of-envelope
fallback) keep working (O(1)/bar, bench-neutral). (b) NEW COMMON `brutex/data/price_sanity.py` (ONE vectorized
positivity+ordering mask) wired at the prebuild's 1-minute read boundary
(`make_lake_read_1m`) — a high<low 1m bar can no longer LAUNDER into
self-consistent-looking derived buckets across the 19 TFs + 1day; the raised
`ResampleInputError` rides the EXISTING per-symbol prebuild quarantine (symbol
named, derived TFs not written, rows never silently dropped/fixed). (c) the
lake census/heal scanner reads the OHLC columns in its SAME per-file pass
(`BAR_SCAN_COLUMNS`) and emits an OBSERVE-ONLY `PriceSanityRows` finding
(census `price_files`, engine verdict flips; the healer FILTERS it — strictly
row-removing, never value-editing). (d) the vendor OHLC clamp is counted +
WARNed once per window (`groww.parse.ohlc_clamped`, observe-only, parse
byte-unchanged). (e) movers/db 2dp display cells ride ONE shared
`static/bx-format.js::bxFixed2` (string-based ROUND_HALF_UP — the
`preopen_report._round2` twin; node-executed golden), fixing the IEEE
`toFixed(2)` half-boundary divergence ('1.005' → "1.01", not "1.00"). (f) the
mutation workflow's alert steps fire on `cancelled()` too — a timeout-KILLED
cargo-mutants job (live-probed run 28748795864, both Rust jobs killed at
exactly 120 min) now alerts instead of dying silently; the Rust jobs'
chronic timeout itself is honestly ledgered, NOT papered over with an
unevidenced budget raise. Sanctioned behavior change: a previously-silently-
accepted CORRUPT stored bar now fails loudly; clean lakes byte-unaffected
(ONE corrupt golden FIXTURE — a fabricated negative low — repaired + regenerated,
honest note in #1510; every other golden unmodified). ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no
scope/vocab/rate change; touched modules 100% line+branch).
Earlier: 2026-07-06 (DEC-SUSPICION-SWEEP-FIXES-001, MIGRATION.md #1492 —
the 19-agent suspicion-sweep fix + hardening pack, 3 confirmed defects + 4 gap pins in ONE
change set: **(D1, MED)** the SPOT pull's AIMD ladder is no longer blind to client-absorbed
429s — the spot build sites bind NO controller feedback hooks into the Groww client and
`run_ingest._with_paced_client` rebound pace ONLY, so the client's bounded fast-retry
(`max_attempts=6`) swallowed 429s against a NO-OP `on_rate_limit` (~18 vendor rejections per
designed 3-hit step-down; sub-exhaustion bursts invisible — empirically proven: 0 ladder hits
for 2 vendor 429s pre-fix). The FULL hook triple (pace + on_rate_limit + on_success) is now
bound at that ONE COMMON spine seam (every production spot pull — dashboard bulk + single +
CLI — flows through `run_groww_spot_ingest`), via NEW `GrowwClient.with_rate_hooks` /
`ClientPool.with_rate_hooks` (re-fanned to every leaf); the spine's own per-window hooks stand
DOWN when the client self-reports (NEW `client_self_reports_rate_limits`/`_successes`
predicates — the feedback twins of `client_self_paces`; `on_timeout` stays spine-owned, the
client never fires the timeout ladder). **(D2, LOW)** the F&O spine no longer double-counts a
propagated 429: the client fires `on_rate_limit` per occurrence INCLUDING the final one before
the exhausted error propagates, and `ingest_option_contract` fired once more → 7 ladder hits
per exhausted window instead of 6 (proven: 3 hits for 2 occurrences pre-fix) — the spine hook
now stands down on a self-reporting client (same predicates). Groww rate CAPS untouched —
FEEDBACK wiring only, never the ceiling/governor values (§2 Rule 6). **(D3, MED)** the /db
window-ledger route no longer re-parses the whole ledger Parquet (~841k rows measured) per
paging click — it rides the mtime-keyed `db_view.window_ledger_rows_cached` (the SHARED
`_ledger_records_cached` parse + a memoised projection through the ONE row-shape source, NEW
`window_ledger.ledger_record_row`, which `to_rows` also delegates to); the route's stale
"thousands / small sidecar" honesty note rewritten (#864 blurb reconciled below). **(G1)**
sweep-preflight FRESHNESS gate: `cmd_sweep_all` now refuses (loud, remediation-naming) when a
requested derived TF's stored partition drifts from a fresh 1-minute re-derivation (NEW
`tf_autoderive.lake_derived_freshness_verdicts` — sharing ONE lake reader with the doctor via
extracted `read_lake_bars_frame` — folded through the pure
`sweep_preflight.preflight_derived_freshness`; abstains never block: non-derived intervals /
no stored partition / no 1m base). **(G2)** a calendar-suite enumeration pin: no MUHURAT_SESSIONS
entry on/after 2025 may close after 15:30 IST (a future evening session would silently re-arm
the `clamp_window_end` partial-settle hazard — adding one now fails loudly, forcing a
Muhurat-aware clamp decision). **(G3)** an AST drift pin: the epoch-anchored legacy
`feed.resample_bars` is imported by NO production module (definition + feed re-export only) —
the dual-resampler split can never silently reach a production path (DEC-RESAMPLE-ANCHOR-002).
**(G4)** `rate_common.py`'s stale "operator-DIRECTED 5 r/s ceiling
(GROWW_RATE_LIMIT_RPS_CEILING)" recovery docstring reconciled to the production 8 r/s
(DEC-GROWW-RATE-CEILING-8-002; the vendor-neutral `CEILING_RPS=5` DEFAULT unchanged). ENGINE
UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no scope/vocab/rate-cap change; touched modules 100%
line+branch).
Earlier: 2026-07-06 (DEC-HW-ENVELOPE-CONSOLIDATE-001 + DEC-THREAD-BUDGET-BOUNDS-001,
MIGRATION.md #1490–#1491 — the hardware-envelope CONSOLIDATION + thread-bounding pack:
(1) the TWO hardware-envelope modules (#1360's `brutex/hardware_envelope.py` + #1401's
`brutex/config/hardware_envelope.py`) are consolidated into ONE at
`brutex/config/hardware_envelope.py` (Golden Rule #11 COMMON) — the #1360 RAM-derived
budget API (`detect_total_ram_bytes` / `UNDETECTED_RAM` / the fail-loud no-floor-clamp
`memory_budget_bytes` consumed by `sweep_mem_budget` + `bloom`) is ported VERBATIM, the
old module DELETED, every consumer repointed, the interim two-module RAM-delegation pin
of the #1167 conflict resolution SUPERSEDED (its stripped envelope-mode
`memory_budget_bytes` duplicate stays DEAD — zero consumers), and the third sizing copy
`greeks_enrich.greeks_pool_workers` now DELEGATES to the COMMON `cpu_pool_workers`
byte-unchanged (the `auto_sweep_workers` identity, pinned across cpu counts).
(2) the polars GLOBAL thread pool is bounded ONCE per process at the ONE entrypoint
(`brutex.cli` dispatch, right after the byte-exact-stdout-pinned polars-free `version`
fast path and before the first `import polars` any dispatch can trigger — CLI fetch AND
the in-process uvicorn dashboard both enter there; outcomes log once per process) and
every concurrent-reader DuckDB connection (`InstrumentStore.open` / `RegistryStore.open`
/ `coverage_reconcile` / `movers_warm`) is bounded at creation (`SET threads TO
<budget>`, `physical_cores // DEFAULT_EXPECTED_LIBRARY_POOLS(2)`) — the per-connection
DuckDB caps ARE the contention fix, while the POLARS default is FULL physical width
(divisor 1 — the #1401 prebuild's worker threads share the one global pool; halving it
would waste half the box in the phase that matters most, and the movers PrebuildGate
serialises the heavy reader load away; the bound still caps polars at PHYSICAL cores
vs its own all-LOGICAL default); `data/catalog.py`'s engine connection deliberately
NOT bounded; kill switches `BRUTEX_POLARS_THREADS` /
`BRUTEX_DUCKDB_THREADS`; an operator-preset `POLARS_MAX_THREADS` is respected and an
already-imported polars gets ONE loud too-late WARN per process, never a pretend-bound.
Parallelism-only — results byte-identical; measured A/B on the small CI fixture
~NEUTRAL (1.14×) as predicted — a CONTENTION fix whose win shows at production
concurrency, NO M4-Pro speedup claimed (Golden Rule #10). The Groww fetch pool / rate
ceiling / governor + the sweep + Greeks per-worker determinism pins +
`data/catalog.py` are UNTOUCHED. ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; touched
modules 100% line+branch).
Earlier: 2026-07-05 (DEC-RAM-BUDGETS-001, MIGRATION.md #1360 —
RAM-scaled memory budgets: NEW stdlib-only `brutex/hardware_envelope.py`
(`detect_total_ram_bytes` — POSIX sysconf primary, macOS `sysctl hw.memsize`
fallback UNVERIFIED-on-real-macOS, any failure → the honest `0` = "unknown"
sentinel, cached once per process; `memory_budget_bytes` — env override wins
fail-loud, sentinel → floor EXACTLY, detected → `int(total × fraction /
workers)`). The TWO §8-keyed hand-picked byte budgets are now derived through
it: the sweep rank-region default (`sweep_mem_budget.DEFAULT_MEM_BUDGET_BYTES`
8 GiB → floor of a 1/6 fraction; `BRUTEX_SWEEP_MEM_BUDGET_BYTES` override
byte-unchanged, #1352 contract preserved) and the Bloom bit-array wall
(`bloom.MAX_BLOOM_BYTES` 16 GiB → derived once at import from a 1/3 fraction
over NEW floor `MAX_BLOOM_BYTES_FLOOR` + NEW fail-loud env
`BRUTEX_MAX_BLOOM_BYTES`; `BloomTooLargeError` semantics unchanged, refusal
still before any allocation; results-safe — the Bloom is an L1 prefilter over
the LMDB exact ground truth, never in run identity). Regression-pinned: a
simulated 48 GiB §8 box derives EXACT parity with both old constants; less
RAM → smaller budgets (the portability win); undetected RAM → the old
constants bit-for-bit. Everything else in the sweep's classification stays
FIXED (LMDB virtual maps, FAIL_CLOSED capacities, determinism pins,
back-pressure queues — reasons in #1360). HONEST: a PORTABILITY + headroom
change, NOT a measured speedup; detection is TOTAL physical RAM, not
available/free. ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; touched modules
100% line+branch).
Earlier: 2026-07-03 (DEC-INGEST-BOUNDARY-INTEGRITY-001, MIGRATION.md #1294 —
the ingest data-integrity pack, four boundary gaps as ONE change set: (a) the
ingest-boundary session filter is now CALENDAR-AWARE (`brutex/data/session.py`
`filter_to_trading_session` via `calendars/nse.get_session`, wired at BOTH ingest
spines) — a bar on a NON-TRADING date (weekend/holiday leak) is dropped + COUNTED
with EXACTLY ONE WARN per window, and an evening-muhurat / special-weekend bar is
now KEPT (a sanctioned behavior change — the date-blind 09:15-15:30 filter wrongly
dropped it); regular-day kept-set pinned byte-identical to the legacy filter;
out-of-coverage dates (post-2026) abstain to the legacy fixed window; the daily
path stays deliberately unfiltered; resamplers untouched by design (they derive
from the stored, already-filtered 1-minute). (b) the parser's DEFENSIVE
epoch-timestamp branch WARNs once per pull (`groww.parse.epoch_timestamp`; vendor
epoch semantics UNVERIFIED — assumed Unix-UTC; parsing unchanged). (c) an
ALL-FILTERED window (raw bars > 0, session-filter survivors == 0) is demoted to a
re-pullable FAILED + `ALL_FILTERED` on the existing #1031 degraded machinery
(bounded SKIPPED settle; SPOT gated on `detect_completeness_gaps` — the dry-run
synthetic adapter would false-red ungated; F&O UNgated, its dry-run returns empty
payloads — the F&O spine's FIRST demotion, all-filtered ONLY, the #1031
observe-only stance for liquidity heuristics preserved; ORTHOGONAL to sink dedup —
a full-dedup re-pull stays SUCCESS). (d) `SinkAppendResult.bars_net_new` (#1031
computed it, never persisted) is threaded into the durable window ledger
(`WindowLedgerRecord.bars_net_new`, optional-column back-compat: an OLD ledger
loads `None` — honest unknown, NEVER a fabricated 0; `/db` grid column added).
ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; touched modules 100% line+branch).
Earlier: 2026-07-03 (DEC-PIPELINE-AUTOPREBUILD-001 + DEC-FNO-EXPIRY-SCOPE-001,
MIGRATION.md #1145–#1146 — the pipeline pack. (1) #1145: derived timeframes (the 19
intraday TFs + the derived `1day`, over SPOT + FUTURES + OPTIONS) are now written ONLY
by the pull pipeline's ONE automatic FINAL prebuild stage — run on EVERY completed pull
over the CLEAN symbol set (every target minus the UNCAPPED quarantined names, excluded
+ headlined; DEC-PREBUILD-CLEANSET-001, MIGRATION.md #1221, supersedes the #1145
strictly-after-a-fully-clean-fetch all-or-nothing gate) — every MID-PULL derived write
is REMOVED
(`pull_controller._resample_daily_after_pull` + the `BRUTEX_AUTO_DERIVE_TFS` autoderive
+ `historical_pull._derive_daily_from_minute`) and the manual "Pre-build timeframes"
button + `derive-timeframes` route are DELETED; the movers warm runs AFTER the prebuild
gated on `PrebuildOutcome.ok` over that clean set (the `fetch_clean` arm dropped by
DEC-PREBUILD-CLEANSET-001; supersedes the early-spot-warm arm of
DEC-PIPELINE-SEGMENT-MOVERS-001); `/api/v1/movers` is honestly GATED while the prebuild
runs (`PrebuildGate`, 200 `gated:true` "prebuild in progress" payload with ZERO lake
reads, 5 s self-recovering auto-retry, dry-run never gates); spot movers read the
prebuilt partitions STORED-FIRST (pinned byte-equality vs the 1-minute resample +
honest `stored_reads`/`resampled_reads` provenance); the completion headline NAMES
quarantined symbols ("N symbol(s) quarantined: A, B (+K more)", job finish detail = the
note) and `/coverage/windows` labels empty-vs-failed (`window_status_label`, both
payload paths). HONEST envelope: a fully-materialised OPTIONS lake costs ~2-3× its
1-minute footprint (operator-ACCEPTED, decree items #19+#21); the FUTURES/OPTIONS
movers SESSION readers deliberately stay 1-minute-native (the locked as-of + Greeks-
overlay semantics of DEC-FNO-TF-DERIVE-OI-001 — materialisation-invariance pinned
instead); a CLI-pulled lake gets derived TFs on the next dashboard pipeline run; the
1m→1d derivation of DEC-1MINUTE-ONLY-DERIVE-1DAY-001 STAYS, relocated into the prebuild
phase. (2) #1146: F&O expiry-level date scoping — an expiry is pulled iff within
`[from → to]` inclusive; out-of-scope = ZERO vendor calls + ZERO ledger rows + one
`fno.expiry_out_of_scope` log per (underlying, expiry), stored bars untouched; scope
SELECTS expiries, never reshapes windows (the 30d/60d law unchanged); the default
no-dates pull is byte-identical. Both: ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX).
Earlier: 2026-06-21 (DEC-FNO-MONEYNESS-LABEL-001, MIGRATION.md #869 — the
per-option moneyness + strike-distance is now shown as ONE combined cell
(**"ITM -1" / "ITM -10" / "OTM +1" / "OTM +10" / "ATM"**) instead of the split
bucket + ATM±N columns of #865 — `brutex/data/moneyness.py` `_format_label`
reformatted to `"<bucket> <signed depth>"` (sign tracks the BUCKET: ITM negative,
OTM positive, so calls AND puts read alike; depth = `|atm_offset|` strikes). The
movers screen + the `/db` F&O candle view each collapse the two columns into one
"moneyness" column (`OptionMover.moneyness_label`; `FNO_CANDLE_COLUMNS` /
`FNO_MONEYNESS_COLUMNS` now a single derived `moneyness` label). Pure DISPLAY reshape
over the SAME read-time-derived values; ENGINE stays NIFTY/BANKNIFTY/SENSEX, NO
storage/scope/vocab/rate change. Earlier: 2026-06-21 (DEC-SPOT-COMPLETENESS-2020-FLOOR-001,
MIGRATION.md #868 — the `/completeness` + `/universe` SPOT 1-minute verdict is HONESTLY
downgraded `full` → `partial` when the symbol's covered data does NOT begin in January 2020
(a late listing / late vendor data, even one starting later WITHIN 2020); one COMMON helper
`_spot_dimension_verdict_with_floor` so only symbols with the solid full-2020 history read
`full`; read/rollup-side only — `completeness.py`, ENGINE UNCHANGED). Earlier: 2026-06-21
(DEC-DB-CANDLE-COUNT-CACHE-001, MIGRATION.md #867 —
the `/db` candle-pager TOTAL (`COUNT(*)` over `read_parquet(glob)`, O(files) — a
Parquet-footer read per file, recomputed on EVERY paging click) is now memoised behind
a generation-stamped cache: `db_view.query_candles` gains an optional `count_cache`
(`CandleCountCache` Protocol) + `generation`, keyed by `candle_count_key(coords,start,
end)` (excludes limit/offset/sort/columns — the paging-invariance the cache exploits);
`build_app` REUSES the COMMON `MoversSnapshotCache` LRU and the `/api/v1/db/candles`
route passes `_summary_cache.generation` (a pull bumps it → recount, never stale —
Golden Rule #1). HONEST envelope: a cached-total HIT is O(1) in partition depth (bench
flat; COUNT runs once per dataset+generation), but paging is NOT O(1) overall — the
page DATA query (`ORDER BY ts DESC LIMIT` over the glob) is itself O(files) and ~2.4×
larger, so this removes only ONE of two O(files) footer scans (~16% on a deep
partition); true O(page) paging needs a write-time lake manifest (explicit FUTURE work,
NOT claimed). `db_view.py` at 100% line+branch; read-only viewer, ENGINE UNCHANGED.
Earlier: 2026-06-20 (DEC-UNIVERSE-BACKEND-CACHE-001, MIGRATION.md #866 —
the `/universe` BACKEND summary + instruments reads are now O(1) on a repeat poll:
NEW `brutex/dashboard/universe_cache.py` `UniverseSnapshotCache` memoises the three
lean store reads (`fno_eligible_underlyings` + `list_index_equity_instruments` +
`reference_only_security_ids`) + the derived `summary` / per-`kind` instrument rows,
keyed by a content-revision token = `(count(*), max(fetched_at))` over the
`instruments` table ITSELF (NOT the `instrument_runs` audit sidecar, which lags an
upsert — the stale-illusion trap this avoids, Golden Rule #1). Every `upsert_many`
stamps a fresh `fetched_at`, so any master change moves the token → rebuild; an empty
store keys at the stable `(0, "")`. Wired into `build_app` as a `universe_cache` param
(mirrors `summary_cache`/`movers_cache`); the routes call `_universe_cache.summary` /
`.instruments`. Honest envelope: HIT is O(1) (bench ratio 1.02× for a 100× deeper
universe; cold read scales 13×→166×), the first read after a load still pays the real
O(candidates) scan (once per revision). `universe_cache.py` at 100% line+branch;
read-only dashboard accelerator, ENGINE UNCHANGED (`InstrumentStore` untouched). This
COMPLETES on the backend the `/universe` FRONTEND O(1) work of #863. Earlier: 2026-06-20
(DEC-FNO-MONEYNESS-001, MIGRATION.md #865 — per-option
**moneyness (ITM/ATM/OTM) + a signed ATM±N strike offset** are now DERIVED at read
time (never stored — time-varying: the strike is fixed but the spot moves) for the
ENTIRE F&O universe (all ~212 stocks + indices), from the NEW shared engine
`brutex/data/moneyness.py` (`classify` / `derive_strike_step` [the **DYNAMIC** grid =
the min positive strike gap, never a hardcoded NSE rule] / `atm_offset` / `moneyness_view`).
Surfaced on BOTH the options-movers screen (as-of snapshot — `OptionMover` +
`compute_option_mover(strike_step=)` + two `movers.html` columns) AND the `/db` F&O
candle viewer (PER-BAR — `db_view.query_candles` appends `moneyness`+`atm_offset` to every
`segment="FNO"` page, classifying each bar against the underlying spot joined by timestamp;
a future reads `NA`, an option with no spot reads `UNKNOWN`). `fno_candle_lake.py`'s
`FNO_CANDLE_COLUMNS` advertises the two as `source="derived"`. Read-only — the brute-force
ENGINE stays NIFTY/BANKNIFTY/SENSEX; NO storage/scope/vocab/rate change. Earlier: 2026-06-20
(DEC-DB-VIEWER-PAGINATE-ALL-001, MIGRATION.md #864 —
EVERY `/db` tab is now O(page): the last two whole-table reads — the F&O registry
(expiries/contracts) + the window-ledger sidecar — are SQL-`LIMIT`/`OFFSET` paginated,
completing the candle/option/F&O/DuckDB-table pages already bounded. The registry is
fully O(page) (DuckDB `LIMIT/OFFSET` over its index + a cheap FILTERED `COUNT(*)`;
bench: first-page 1.04× for 10× more legs); the window-ledger is O(page) render+serialize
over an O(windows) read [RECONCILED by DEC-SUSPICION-SWEEP-FIXES-001, MIGRATION.md #1492:
the "small sidecar" claim was STALE — a pre-planned full-universe options pull holds ~841k
ledger rows, and this route re-PARSED the whole Parquet per paging click; the read now rides
the mtime-keyed `db_view.window_ledger_rows_cached` (ONE parse per ledger flush, O(1) repeat
click; the FIRST read per generation stays O(rows) — honest envelope, still NOT claimed
O(page)-read)]. `registry_store.py` + `db_view.py` at 100% line+branch;
read-only viewer, ENGINE UNCHANGED. Earlier: 2026-06-20 (DEC-DB-UNIVERSE-O1-LATENCY-001, MIGRATION.md #863 —
the `/db` + `/universe` dashboard pages made INSTANT per interaction with ZERO
correctness change: `/db`'s candle pager no longer scans the WHOLE multi-year
1-minute partition to baseline the derived %-change columns on every paging click —
`db_view.py::query_candles` now reads the prior-day last-1-minute close for ONLY the
page's own date span + the single bar before it (`_intraday_prev_close_by_page_date`,
byte-for-byte identical to the old full scan, proven exhaustively + a flat-latency
bench), and the `/db` + `/universe` FRONTENDS update in place (grid-only `#dbgrid`
refresh on paging instead of a full control-bar+datepicker teardown; a per-tab
instrument cache + debounced search + in-place completeness patch). Honest envelope:
O(page date-span) / O(visible), NOT a literal O(1)-total. Engine + 52-vocab + scope
UNCHANGED — read-only viewer only. Earlier: 2026-06-20 (DEC-LIVE-REMOVE-001,
MIGRATION.md #862 — the **entire LIVE feed feature is REMOVED in full**: the
`brutex/live/` capture package + `python -m brutex.live` CLI, the dashboard `/live`
page + `/api/v1/live/*` + `/api/v1/db/live/*` routes + the `dashboard/live_*` surface,
the `live_ticks`/`live_candles_1m` lake tables, the Movers "Live" tab + the /db "Live
lake" section + the nav `/live` link (44 files). The production historical/backtest
PULL + `rate_common` + the brute-force ENGINE are UNTOUCHED — `adapters/groww/live.py`
`build_live_client` + `pull_controller._build_live_controller` are the PRODUCTION pull
client/rate-controller, NOT the feed, deliberately kept). Earlier: 2026-06-20 (DEC-FNO-CANDLE-LAKE-001,
MIGRATION.md #861 — the F&O (futures + options) historical-candle lake is now a
FIRST-CLASS documented `/db` table (a "📊 F&O candles" row, decoded per contract) +
registered in the master `schema_meta` (`fno_candle_lake.*`), FORMALIZING data ALREADY
stored by the Groww pull at `bars/<exch>/FNO/<groww_symbol>/<interval>/…` — NOT a new
store; lake-view only, ENGINE UNCHANGED). Earlier: 2026-06-20 (DEC-MOVERS-O1-LATENCY-001,
MIGRATION.md #860 — the `/movers` page made near-O(1) per interaction: the `top_n`-independent
lake scan is split from the cheap rank + cached per coordinate (`MoversSnapshotCache`,
generation-invalidated), and the cold per-symbol intraday read is bounded to the day's
`1minute/<yyyy>/<mm>.parquet` month file (`lake_day_glob`, byte-equivalent for session-filtered
symbols); read-only lake/dashboard, ENGINE UNCHANGED). Earlier: 2026-06-20 (DEC-OPTIONS-COMPLETENESS-REAL-YEARS-001,
MIGRATION.md #859 — the `/completeness` OPTIONS table shows only the years that ACTUALLY have
expiries + an honest `partial` for late-listed underlyings (JIOFIN/ETERNAL): a 0-expiry
pre-listing year no longer inflates `years` to a fake 2020→2026 span nor keeps it perpetually
`growing`; read/rollup-side only — `brutex/dashboard/completeness.py`, no pull/engine change).
Earlier: 2026-06-20 (DEC-1MINUTE-ONLY-DERIVE-1DAY-001,
MIGRATION.md #853 —
§1/§3/§6 facts: **1-minute is now the ONLY granularity fetched from Groww** (spot
+ futures + options); `1day` (PDH/PDL anchor) + the 19 intraday TFs are DERIVED
from the stored 1-minute and written to the lake (`spot_daily_resample`), never
separately vendor-fetched. This supersedes the operator-chosen granularity dropdown
of DEC-PULL-1DAY-FIRST-001 (#723, removed) + re-introduces the 1m→1d derivation
removed by DEC-REALDATA-ONLY-NO-1M-TO-1D-001 (#852) as the always-on PRIMARY path.
The "Verify vs NSE" bhavcopy cross-check is REMOVED in full; the F&O historical
DEFAULT window is per-contract `expiry − 3mo → expiry`. The brute-force ENGINE stays
NIFTY/BANKNIFTY/SENSEX. Earlier: 2026-06-19 (DEC-CLAUDEMD-ADAPTER-CITE-RECONCILE-001,
MIGRATION.md
#846 — §6 symbol-resolution rows refreshed after the Track-1 data-quality fix
series #799/#800/#801 merged: the `Symbol/key format` + `Track 1 brute-force
universe` rows now cite the master-authoritative `by_exchange_symbol` +
`groww_symbol_by_pair` resolution (DEC-GROWW-HYPHEN-SYMBOL-RESOLVE-001 #845, which
resolves hyphenated `BAJAJ-AUTO` + verbose-symbol `MIDCPNIFTY`) instead of the
stale "via `by_groww_symbol`" wording + drifted `adapter.py` line citations; the
`Historical OHLC` row's `adapter.py` line pointer refreshed to the relocated
`fetch_ohlcv`. No substantive §6 fact changed — the merged trio (1-day resample
#842, until-clean-all-outstanding #844, symbol-resolve #845) is lake/adapter/ingest
only, the brute-force ENGINE stays NIFTY/BANKNIFTY/SENSEX). Earlier: 2026-06-13
(DEC-PULL-1DAY-FIRST-001, MIGRATION.md #723 —
§1/§6 spot-interval facts: the spot pull granularity is now operator-chosen
per pull via the `/universe` dropdown with a VERIFY-FIRST **1-day-only**
default (`resolve_fetch_intervals`; 1-minute / both after the
OHLCV-vs-bhavcopy cross-check), no longer force-fetching 1-minute alongside
1-day). Earlier: 2026-06-13 (DEC-PULL-FLOOR-2020-001, MIGRATION.md #722 —
§3 locked scope range moved `2021-01-01 → today` → `2020-01-01 → today`:
the DATA pull floor (`GROWW_HISTORY_FLOOR`) lowered to the full
probe-verified Groww 1-minute depth, superseding the conservative
2021-01-01 floor of DEC-DHAN-PULL-SCOPE-001). Earlier: 2026-06-12
(DEC-CLAUDEMD-SPEC-LINE-RECONCILE-001,
MIGRATION.md #669 — renumbered from #667 by PR #629's duplicate-repair;
§2 Rule 4's run-identity citation refreshed
`TRACK1_BACKTEST_SPEC.md:654` → `:743`; spec growth had moved the
blake3 formula block and the stale line pointed mid-section). Earlier:
2026-06-10 (DEC-AUTOMERGE-CLEAN-STRICT-001, MIGRATION.md
#612 — §2 Rule 13(a) `mergeable_state` invariant aligned with the
charter §4.5 pre-G2 envelope per DEC-WORKSPACE-VERIFY-002 follow-up
F-4). Earlier: 2026-06-10 (DEC-WORKSPACE-VERIFY-002, MIGRATION.md #609 —
§6 prior-vendor `constants.py` line citations refreshed after the
5-agent workspace verification found 4 rows drifted by 7 lines; §1/§3
active branch refreshed). Earlier: 2026-06-03 (DEC-SHARED-CORE-001 Phase 2b,
MIGRATION.md
#441 — BruteX's 52-condition vocab now delegates to the shared
`rust/trading-core/` crate; §1/§5 reconciled). Earlier: 2026-06-01
(DEC-DASHBOARD-OMIT-AUDIT-001 — vendor docs reconciled Groww → the prior
vendor after the 5-agent workspace verification found §1/§6 stale).
DEC-P0-026
(PR #48) retired the tickvault co-development layer; DEC-SHARED-CORE-001
then re-established a single shared `trading-core` crate (indicators +
52-vocab) consumed by BruteX + TickVault. Vendor history: broker → Groww
migration (2026-05-15), Groww → prior-vendor pivot proposed then
reverted (ADR-0002, 2026-05-31 — the prior-vendor revert, now
superseded), then a full
re-migration back to Groww — **DEC-DHAN-GROWW-REMIGRATE-001 (2026-06-12,
ADR-0009) supersedes the revert and is authoritative: the live vendor is
Groww (the prior-vendor adapter package + reference docs were removed in
the ADR-0009 §5 cutover PR series).** Track 1 spec locked after 4-agent
hostile-review pass._

---

