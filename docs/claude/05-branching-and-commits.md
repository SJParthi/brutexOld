## 3. Branching and Commits

- **Active feature branch:** `claude/rust-slice-t10-lake-keys`
  (DEC-DATA-RUST-PURELAW-002, MIGRATION.md #1952 — ADR-0020 slice T10 PR-2:
  the three lake_keys candidate-ladder builders (stock_drift_fold ·
  spot_key_candidates · spot_recovery_candidates core) join the PR-1
  `rust/brutex-data` crate behind the SAME default-OFF `BRUTEX_DATA_RUST`
  flag via the NEW sibling facade `brutex/data/lake_keys_rust.py` (own
  handshake + per-law burn-in — a lake_keys fault never degrades
  fno_symbol_rust); verbatim Python bodies retained as the LIVE oracles;
  the heal-findings-parity integration pin is the tier gate; flag
  unset/falsy byte-identical (bomb-spy-pinned); the data-rust-parity.yml
  lane extended (no new lane); expected payoff ≈ 0 — uniformity, not
  speed; ships INERT; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; matches
  §1's Active branch line — see §1 + MIGRATION.md #1952 for the full blurb)
  Earlier: `claude/rust-slice-t10-data-purelaw`
  (DEC-DATA-RUST-PURELAW-001, MIGRATION.md #1940 — ADR-0020 slice T10 PR-1:
  the NEW stdlib-pure OPTIONAL crate `rust/brutex-data` + the default-OFF
  `BRUTEX_DATA_RUST` facade routing `parse_fno_symbol` (integers-only FFI —
  spans + expiry y/m/d; strike stays Python; ASCII pre-gate; the fail-closed
  LOCALE guard latches a non-English LC_TIME onto the oracle —
  `locale_unsupported`, real-es_ES + synthesized + MUST-engage-control
  pinned); the verbatim Python law retained as the LIVE parity oracle; flag
  unset/falsy byte-identical (bomb-spy-pinned); built ONLY by the NEW
  non-required data-rust-parity.yml lane; expected payoff ≈ 0 — uniformity,
  not speed; ships INERT — a default flip needs its own DEC; ENGINE
  UNCHANGED — NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line — see
  §1 + MIGRATION.md #1940 for the full blurb)
  Earlier: `claude/rust-slice-t35-leaves-v2`
  (DEC-CONFIG-RUST-LEAVES-001, MIGRATION.md #1943 — slice T35 root-leaf Rust
  conversion, milestones 1-3: the NEW `rust/brutex-core` crate (0.3.0 as shipped, pyo3-only
  Cargo.lock, the brutex-lake conventions) + the two default-OFF seams
  `BRUTEX_CORE_RUST_MONEY` (the DEC-MONEY-2DP-001 money laws as hand-rolled
  exact decimal-string HALF_UP arithmetic — STORED-BYTES critical; frozen
  literal-corpus rounding goldens + the brutex-lake `_snap_price_expr`
  twin-impl coordination pins + the HALF_UP-vs-round_half_even negative pin)
  and `BRUTEX_CORE_RUST_HWENV` (the hardware-envelope DERIVATION laws only —
  detection stays Python; parity bar BEHAVIORAL, sizing-only, no run-identity
  exposure) + the milestone-3 trio `BRUTEX_CORE_RUST_PATHS`/`_DOTENV`/`_BACKUPENV`
  over the shared `seam_core` (NO-SEAM verdicts for ports/credentials; the
  mechanical AWS boundary pin); flag OFF (default) is behavior-identical + crate-untouched
  (bomb-spy-pinned); "expected payoff ≈ 0 — conversion is for uniformity, not
  speed" (the ADR-0020 banner verbatim); ships INERT — a default flip needs its
  own DEC; the T35 HOLDS recorded in #1943; ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line — see §1 +
  MIGRATION.md #1943 for the full blurb)
  Earlier: `claude/rust-slice-t16-strategies`
  (DEC-STRATEGIES-RUST-COMBO-001, MIGRATION.md #1955 — QM-confirmed;
  renumbered from the provisional #1933 at the 2026-07-17 QM ledger
  correction; T16 PR-1: combo.py's TWO pure byte
  laws — the `\x1f` first-offender scan + the canonical-JSON render over
  the ALREADY-lowered tree — re-hosted on the NEW standalone
  `rust/brutex-strategies` crate behind the default-OFF
  `BRUTEX_STRATEGIES_RUST_COMBO` facade `brutex/strategies/combo_rust.py`
  (local flag/panic-belt twins, per-law burn-in + ship-the-oracle
  autorevert, refuse-poison gates); canonicalisation / error taxonomy /
  message bytes / the 9-field join + blake3 stay Python (the lowered-tree
  law — no `float.__repr__` twin ever crosses); run identity UNTOUCHED —
  every frozen golden UNMODIFIED on both arms, the vocab fixture
  byte-untouched; the T16 non-PR-1 rows dispositioned under the
  GLUE-DEFER/FIBDEF/SPEC-MIRROR/RUNNER follow-up handles (approved
  NON-TERMINAL deferrals per the 2026-07-17 plan-authority ruling;
  SPEC-MIRROR + FIBDEF held behind the fib-express single-home closure,
  RUNNER last); ships INERT — flag OFF byte-identical
  (bomb-spy-pinned), payoff ≈ 0 by the mandated banner; ENGINE
  UNCHANGED — NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line —
  see §1 + MIGRATION.md #1955 for the full blurb)
  Earlier: `claude/test-aws-hermeticity`
  (DEC-TEST-AWS-HERMETICITY-001 (proposed — pending queue-master
  confirmation), MIGRATION.md #1954 (provisional; renumber-at-landing per
  DEC-LEDGER-RENUMBER-001) — the TEST-side AWS hermeticity fix, ZERO
  `brutex/` source change: the two `test_dashboard_results_route.py` sweep
  tests inject the established `account_key_resolver=lambda: None` seam
  (their default resolver reached a live decrypting `ssm:GetParameter` on
  any boto3-equipped box), and the suite-wide autouse
  `_aws_hermeticity_belt` in `tests/conftest.py` patches the real-SSM-client
  factory + its from-import bindings + the boto3 boundary (when importable)
  to raise a loud remediation-naming refusal — a live AWS call from any
  IN-PROCESS test path is structurally impossible (subprocess children +
  direct botocore use sit outside the in-process monkeypatch, verified
  unreached by every current test); deliberate fakes keep working; self-test
  `tests/unit/test_aws_hermeticity_belt.py`; ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line — see §1 +
  MIGRATION.md #1954 for the full blurb)
  Earlier: `claude/rust-u128-enum-kernel`
  (DEC-ENUM-KERNEL-U128-001, MIGRATION.md #1942 — queue-master allocation, map
  v36; the critical-path Rust u128 ENUM/COUNT kernel: the width-generic
  `MaskWord` trait hoisted VERBATIM from trade_sim.rs to the shared
  `rust/fno-math/src/mask_word.rs` (pure move, its own severable first commit;
  #1549's cargo suite unmodified-green) + the native-`Vec<u128>` count twins
  `count_supermasks_u128s`/`count_supermask_u128s`/`ResidentBarsU128` over ONE
  width-generic core/dispatch (u64 names kept as thin delegators — every
  pre-existing call site + test unchanged) + the `combo_mask.py` >=64-bit
  width dispatch with lazy wide-kernel promotion and the LOUD-FAIL inversion
  vs #1549 (wheel-missing at a >=64-bit dispatch raises the typed
  `WideKernelUnavailableError` naming the DEC + `wheel_missing_u128` + the
  maturin remediation — the numpy two-limb reference is retained strictly as
  the tests-side parity oracle, structurally unreachable as a silent >=64-bit
  production fallback); observables `kernel_kind`/`wide_kernel_active`/
  `dispatch_kernel_kind` are the supported no-monkeypatch test surface; u64
  path byte-identical (u64 goldens + frozen run-identity goldens unmodified,
  bomb-spy-pinned incl. the exact `U64_MASK` boundary); measured sandbox
  179.5 ms numpy vs 27.7/20.0 ms Rust free/resident at 94k×1k — observed,
  never asserted; ENGINE results UNCHANGED — NIFTY/BANKNIFTY/SENSEX; matches
  §1's Active branch line — see §1 + MIGRATION.md #1942 for the full blurb)
  Earlier: `claude/rust-lake-ledgerstore-convert`
  (DEC-LAKE-RUST-LEDGERSTORE-CONVERT-001, MIGRATION.md #1918 — ADR-0020 slice
  G10 CONVERT: the `window_ledger_store` parquet render routed through
  `rust/brutex-lake` behind the NEW default-OFF
  `BRUTEX_LAKE_RUST_LEDGER_RENDER` flag under the ROW-EQUIVALENCE +
  reader-parity standard, never bytes (byte-identity measured-infeasible per
  the #1917 spike — the operator ruling of 2026-07-16 as relayed via the
  coordinator; golden re-baseline forbidden); NEW
  `rust/brutex-lake/src/window_ledger_render.rs`
  (`render_ledger_parquet_from_ipc`; `last_attempt_at` stays a String —
  cargo-pinned) + the NEW facade `brutex/data/lake_rust_ledger_render.py`
  (PR-1 guard set; the 16-render burn-in compares ROW EQUIVALENCE via pyarrow
  readback — the normalized-string-family fold is the ONE documented
  deviation — shipping the oracle bytes + latching on any mismatch); the
  store's two write sites consolidate onto the ONE shared render body with
  the verbatim pyarrow oracle retained LIVE, the render inside
  `atomic_write_via`'s guarded callable (flock/atomic/LakeWriteHalt semantics
  byte-identical); flag OFF byte-identical + never-imports-crate
  (bomb-spy-pinned); the twin-lake reader-parity battery + the
  crash/resume/re-pull state-convergence NAMED MERGE GATE EXECUTED on the
  real merged wheel; the #1917 bytes-probe pins reconciled not broken; ships
  INERT — a default flip needs its own DEC; ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line — see §1 +
  MIGRATION.md #1918 for the full blurb)
  Earlier: `claude/box-autodeploy-lane`
  (DEC-BOX-AUTODEPLOY-LANE-001, MIGRATION.md #1939 — the nightly BOX
  AUTO-DEPLOY leg + the standalone `brutex box-update` mouth: the pre-chain
  ff-only self-update of the box checkout under fail-safe guards
  (kill switch `BRUTEX_BOX_UPDATE`; dirty/diverged/fetch-failed skips),
  delta-classified BARE maturin wheel rebuilds over the Makefile-mirrored
  `WHEEL_SET` (brutex-lake excluded pending DEC-BRUTEX-LAKE-BOX-WHEEL-001,
  drift-pinned), the fresh-subprocess assert-green gate (import smoke +
  scrubbed-env doctor checks; `gate="skipped"` ⇔ never ran), rollback +
  required-wheel repair + the always-once-loadable version-coherence
  repair, crash-marker-provenance-scoped PRESERVE-FIRST quarantined lock
  restores (operator locks never clobbered), the repeated-failure alarm
  dedupe (two-integer law), re-exec ONCE (flock released strictly before
  execv; process-env-only sentinel), the additive report blocks + 📦
  honesty note, and the §12 removed-env-flag startup pre-flight; FAIL-OPEN
  — never blocks the nightly; wedge-asymmetry disclosed with the named
  follow-up DEC-BOX-WEDGE-RESCUE-001; both new modules 100.00%
  line+branch on NEW ci.yml stanzas + mutation pairs; ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line — see §1 +
  MIGRATION.md #1939 for the full blurb)
  Earlier: `claude/rust-slice-t24-integrity`
  (DEC-DATA-RUST-INTEGRITY-DETECTORS-001, MIGRATION.md #1948 — SLICE T24 PR-1:
  the THREE cold data-integrity detector pure laws — the raw-integrity blake2b
  digest (in-crate from RFC 7693, ZERO new deps — Cargo.toml/Cargo.lock
  byte-untouched), the interior bar-less run walker, and the partial-window
  truncation verdict — routed through `rust/brutex-lake`
  (NEW `src/detectors.rs`) behind THREE default-OFF flags via the NEW facade
  `brutex/data/detectors_rust.py` (`BRUTEX_LAKE_RUST_RAW_INTEGRITY` ·
  `BRUTEX_LAKE_RUST_INTERIOR_GAP` · `BRUTEX_LAKE_RUST_PARTIAL_WINDOW`;
  per-law 16-call burn-in + ship-the-ORACLE autorevert over ONE shared latch;
  narrow degrade set + imported panic belt; refuse-poison shape gates; NO
  polars version guard — polars-free laws, the G8 deviation); the G6
  flag-gate-in-target routing (zero-arg payload/oracle callables + a
  target-side materialize — the facade never imports PartialWindowVerdict);
  json canonicalisation + the NSE calendar stay PYTHON (payload factories
  compute session ordinals/flags; off-grid walks ABSTAIN → oracle; span cap
  9000 days); flag OFF byte-identical + crate-never-imported (bomb-spy
  pinned); `window_status` EXCLUDED from the slice (write-retired) + the
  bad_tick COMMON-reuse ruling recorded in #1948; cargo 90 passed
  --release --locked, the new 97-test battery + the existing detector suites
  flag-ON 96 passed (+45 store/wiring) + consumer suites flag-OFF 391
  passed, all four modules 100.00%
  line+branch; round-1 review folded (both lenses SHIP, 0 MINOR+ — see §1 +
  MIGRATION.md #1948); ships INERT; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX;
  matches §1's Active branch line — see §1 + MIGRATION.md #1948 for the full
  blurb)
  Earlier: `claude/preopen-day-freshness-guard`
  (DEC-PREOPEN-DAY-FRESHNESS-001, MIGRATION.md #1930 — the preopen DAY-basis
  stale derived-1day FRESHNESS guard: each day-basis row's stored
  (IEP, PREV. CLOSE) pair is served only when it EQUALS the fresh resample
  of the stored 1-minute base (the /movers #1795a verify-equality law via
  the ONE COMMON `spot_daily_resample` window reader + daily resampler); a
  diverging row is served RECOMPUTED from the 1-minute truth + ONE capped
  `preopen.stale_daily_recomputed` WARN per dataset triple — row-ATOMIC
  (never a mixed-scale pair; the TMPV cross-basis explosion negative-pinned),
  consumed-values-scoped, fail-open on a missing 1m base, row-SET-preserving
  (the #1649a sessions lane untouched — sessions vs VALUES is the verified
  distinction), disclosed O-cost (one month-bounded 1m read + resample per
  day-basis symbol per cold build; warm clicks stay zero-read); + the B-3
  fold — the derived-intraday F&O writer stores a present-but-null bucket
  OI as `None`, aligned with the F&O daily derive (spot stored-0
  byte-identical); fails-before proven executed at merge-base ecb37cc;
  fresh lakes byte-identical, preopen goldens fixtures BYTE-UNTOUCHED;
  READ-side dashboard + ONE derived-writer alignment — ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line — see §1 +
  MIGRATION.md #1930 for the full blurb)
  Earlier: `claude/timestamp-ist-singlesource`
  (DEC-TIMESTAMP-IST-SINGLESOURCE-001, MIGRATION.md #1929 — timestamps 100%
  COMMON: the +05:30 IST-offset / 09:15 open / 15:30 close constants
  collapse onto ONE canonical root each in `brutex/calendars/nse.py`
  (`IST_TZ_NAME` · `IST` · `IST_UTC_OFFSET` · `IST_OFFSET_MINUTES` ·
  `IST_FIXED_OFFSET` · `SESSION_OPEN_IST`/`SESSION_CLOSE_IST`), unified
  strictly WITHIN representation families, byte-identical behavior;
  `data/session.py`'s second IST root becomes a re-export and its
  `SESSION_OPEN`/`SESSION_CLOSE` names stay importable (the TF-registry PR
  binds them); the NEW `session.ist_offset_duration()` is the one polars
  representation; Rust/JS/SQL copies drift-pinned in
  `tests/unit/test_timestamp_ist_singlesource.py`; the TF-registry PR
  #1904's files excluded by coordination; the minute-bound semantic layer
  is the named follow-up — untouched; ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line — see §1 +
  MIGRATION.md #1929 for the full blurb)
  Earlier: `claude/tf-registry-dynamic`
  (DEC-TF-REGISTRY-DYNAMIC-001, MIGRATION.md #1904 — the seconds-keyed
  dynamic timeframe registry: `TfSpec`/`TfAnchor` + the 21-entry `ALL_TFS`
  registry in data/timeframes.py with the legacy minutes-keyed vocabulary as
  byte-identical DERIVED VIEWS; the tf_resampler bucket math seconds-keyed
  through the ONE engine + the NEW `resample_frame_to_spec` (20m-by-config +
  sub-minute framework proofs; frozen-oracle parity pinned over all 21 TFs);
  the two literal interval copies killed (feed INTERVAL_SECONDS + the
  synthetic map registry-derived); partition VALID_INTERVALS
  registry-derived; engine `Timeframe.duration_s` property (minutes stays
  the field — digest safety, recorded deviation); movers HH:MM:SS as-of
  parsing + the look-ahead-safe seconds-instant law + second-of-day filter
  seams + session bounds derived from the COMMON data/session constants.
  Sub-minute TFs are framework-only (no production sub-minute feed exists —
  Groww is 1-minute-only; own DEC + real feed required). ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line — see §1 +
  MIGRATION.md #1904 for the full blurb)
  Earlier: `claude/derived-freshness-screen`
  (DEC-DERIVED-FRESHNESS-SCREEN-PARITY-001, MIGRATION.md #1932 — the
  derived-freshness gate + doctor probe re-derive from the SAME
  bad-tick-SCREENED 1-minute the prebuild writes from (screened-vs-screened
  via the REUSED `screen_frame_bad_ticks`; ONE `BRUTEX_BAD_TICK_SCREEN`
  switch, OFF = the pre-parity unscreened compare, byte-preserved) — the B-1
  permanent sweep/doctor false wedge on a legitimately-stored ≥4x in-session
  spike lake is dead (executed fail-before at merge base b81f017e); the B-2
  ghost-bucket half is executed-pinned honest-stale with the working manual
  escape named in the refusal remediation + doctor detail, prune deferred to
  the named follow-up DEC-DERIVED-GHOST-BUCKET-PRUNE-001; READ-side gate
  parity only, ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; matches §1's
  Active branch line — see §1 + MIGRATION.md #1932 for the full blurb)
  Earlier: `claude/feed-registry-common`
  (DEC-FEED-REGISTRY-COMMON-001, MIGRATION.md #1903 — the COMMON N-feed
  registry at its leaf home `brutex/feeds/` (PR #1570's profile+registry
  adopted-with-relocation — DEC-VENDOR-PROFILE-REGISTRY-001 #1833
  supersedes-on-land, PR #1570 still OPEN; DEC-FEED-INTERFACE-001 #1782's
  package home kept, its static-dict registry superseded-in-part) + the
  data-driven `build_default_registry` (Groww by default, extras only via
  explicit config, junk fails loud) + the first production consumer —
  `build_headless_pipeline(registry=…)` resolves the PRIMARY profile,
  carries vendor + write domain, threads `vendor=` into
  `run_full_pipeline`; default-inert (Groww-only ⇒ byte-identical,
  real-sequencer report-dict parity-pinned); the write domain is carried,
  consulted by nothing (writer binding stays gated); NO second live
  vendor ships (operator-gated; the fixture is the synthetic `testfeed`);
  ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch
  line — see §1 + MIGRATION.md #1903 for the full blurb)
  Earlier: `claude/movers-daily-freshness`
  (DEC-MOVERS-DAILY-FRESHNESS-001, MIGRATION.md #1931 — the `/movers` DAILY
  arm gains the VERIFY-EQUALITY freshness guard, the named follow-up of
  DEC-MOVERS-STORED-FRESHNESS-001 #1795a (the intraday guard): a stored
  derived `1day` day-row serves only when it equals the in-memory
  `resample_1m_to_daily` of the day's uncut 1-minute base (the SAME
  production derivation the prebuild binds — imported, never re-minted;
  the #1795a equality law realised in the bar model); stale/MISSING ⇒ the
  just-computed 1m-derived daily bars + ONE capped
  `movers.stored_stale_fallback` WARN on the shared state; base-absent /
  date.max ⇒ the disclosed fail-open serve-stored; `latest_movers_date`
  resolves from the 1-MINUTE base unioned MAX with the kept 1day scan (a
  post-prebuild 1m append moves the movers default day; a 1day-only legacy
  lake keeps its latest available day); + two folded MINORs —
  `compute_snapshot.change` None on a zero baseline (the docstring
  contract) and the `/api/v1/movers` `?interval=` validation (unknown token
  = a clean 400 naming the parameter before any lake read on all three
  tabs — the false junk-interval "No futures in the lake yet"/"No spot
  data" notes are dead); fresh prebuilt lakes byte-identical
  (parity-pinned); named follow-up DEC-MOVERS-BASELINE-FRESHNESS-001 (the
  prior-day baseline seam); READ-side dashboard only — ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line — see §1 +
  MIGRATION.md #1931 for the full blurb)
  Earlier: `claude/spot-derive-ts-range-hardening`
  (DEC-SPOT-DERIVE-TS-RANGE-HARDENING-001, MIGRATION.md #1919 —
  coordinator-allocated entry; out-of-`datetime`-range stored TIMESTAMPS
  rejected at the SPOT derive read boundary — the NEW COMMON
  `price_sanity.validate_frame_ts_datetime_range` gate (vectorized i64
  bounds, boundary-exact, never panics) in `make_lake_read_1m` +
  `make_lake_read_1m_window` raising the quarantining
  `ResampleInputError` (pre-fix a planted 2**62-µs stamp panicked
  `frame_to_candle_bars`' `to_list()` as `pyo3_runtime.PanicException`, a
  BaseException past every `except Exception` — executed repro: ONE
  poisoned symbol aborted the WHOLE crossverify prebuild leg); the three
  SPOT-path catchers (gated_prebuild per-symbol set · prebuild_leg
  quarantine · lake_heal `_rebuild_tf_months`) widened with the #1594
  round-18 escape guard as defense-in-depth
  (KeyboardInterrupt/SystemExit still propagate); the bars-tree
  `glob=False` metachar parity sweep on nine explicit-path reads incl. the
  ENGINE feed mouth `data/catalog.py` (a `NSE-X[12]` dir beside real
  `NSE-X1`/`NSE-X2` siblings silently served the SIBLINGS' bars — executed
  repro) + `[`/`]` joining `partition._FORBIDDEN_SYMBOL_CHARS`;
  fails-before proven at merge base ecb37cc4; healthy-lake byte-identity +
  the frozen engine goldens pinned UNMODIFIED; ENGINE UNCHANGED on clean
  feeds — NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line — see §1
  + MIGRATION.md #1919 for the full blurb)
  Earlier: `claude/rust-lake-locks-atomic`
  (DEC-LAKE-RUST-LOCKS-ATOMIC-001, MIGRATION.md #1912 — allocator-final; ADR-0020
  PR-4: the lake lock/atomic-replace primitives gain the default-OFF
  `BRUTEX_LAKE_RUST_LOCKS` Rust routing — `rust/brutex-lake/src/locks.rs`
  (flock attempt/release + the chmod→fsync→rename atomic commit) behind the NEW
  `brutex/data/lake_rust_locks.py` facade; attempt/commit-grained seams inside
  `partition_lock._acquire_bounded` / `partition_write_lock` /
  `atomic_io._atomic_write_core` — the loop/deadline/beat law, the sha256
  sidecar path law, `BoundedLockRegistry` and the `lake_write_guard` error
  taxonomy ALL STAY PYTHON; OSError-with-errno crosses the FFI intact, never a
  new exception class; OFF (default) = byte- and call-graph-identical
  (bomb-spy-pinned); mixed-arm mutual-exclusion + 2-subprocess belts +
  errno-parity proofs EXECUTED over the real wheel; ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line — see §1 +
  MIGRATION.md #1912 for the full blurb)
  Earlier: `claude/rust-lake-merge-dedup`
  (DEC-LAKE-RUST-MERGE-DEDUP-001, MIGRATION.md #1911 — allocator-final —
  ADR-0020 PR-3: the bars-writer merge/dedup DECISION kernel in Rust behind the
  default-OFF `BRUTEX_LAKE_RUST_MERGE`; `merge_decision_from_ipc` returns one
  action code per batch row over two 17-column Arrow-IPC decision frames and
  Python materializes the fold (bytes identical by construction); the verbatim
  fold extracted to `ParquetWriter._merge_fold` (flag OFF byte-identical,
  spy-pinned); PR-1 guard pattern + burn-in identity bit-compare +
  ship-the-Python-result autorevert + the refuse-poison gate; the 6-case golden
  parity corpus EXECUTED green against the compiled crate; LakeWriteHalt
  taxonomy pinned both arms; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; matches
  §1's Active branch line — see §1 + MIGRATION.md #1911 for the full blurb)
  Earlier: `claude/rust-datalake-io-slice8-pr2`
  (DEC-RUST-DATALAKE-IO-SLICE8-PR2-001, MIGRATION.md #1771 — its own
  steward-assigned ledger row; parent roadmap
  DEC-RUST-DATALAKE-IO-ROADMAP-001, MIGRATION.md #1891; PR-2 of the datalake
  Rust conversion roadmap, STACKED on the PR-1 branch's head 283247ed —
  the PR opens only after the PR-1 PR merges: the option writer's
  ALL-DEFAULTS render seam (`_atomic_write_parquet` → the ONE
  `lake_rust_render` facade under the NEW `PROFILE_OPTIONS_DEFAULTS`
  render profile; flag-OFF is the literal legacy call, never-routes
  spy-pinned; `lake_heal.py` + `atomic_io.py` ZERO diff), the crate's
  `render_parquet_defaults_from_ipc` over ONE shared parameterized core
  (bars tests UNMODIFIED), the 12-case options byte-parity corpus 12/12
  MATCH + whole-path write/heal/merge/heal_findings ON/OFF twins + rails
  R1–R14; payoff ≈ 0 banner verbatim; ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line — see §1 +
  MIGRATION.md #1771 for the full blurb)
  Earlier: `claude/rust-lake-ledgerstore-spike-r2`
  (DEC-LAKE-RUST-LEDGERSTORE-SPIKE-001, MIGRATION.md #1917 — allocator-final;
  the ADR-0020 slice-G10 `window_ledger_store` byte-parity SPIKE: VERDICT
  byte-identity NOT FOUND (F3 confirmed by measurement — 14 polars-native
  arms × 7 production-writer corpus cases, ZERO sha256 matches; the
  divergence = `created_by` · the footer format-version marker · the
  `ARROW:schema` flatbuffer bytes · the first data-page header thrift —
  none an exposed polars knob), so the priced DEC-LAKE-EXACT-PIN-001
  escalation ships to the coordinator
  (`docs/lake/g10_window_ledger_byte_parity/` — transcript + three costed
  options, none adopted here) + the always-green probe pin
  `tests/golden/test_window_ledger_bytes_probe.py` (the documented
  divergence class + the cross-encoder row fixed point); ZERO
  production-code change — ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX;
  matches §1's Active branch line)
  Earlier: `claude/rust-lake-waved-harness`
  (DEC-LAKE-RUST-WAVED-HARNESS-001, MIGRATION.md #1916 — the ADR-0020 Wave-D
  poison-corpus differential harness: the 27-case corpus
  (`tests/fakes/poison_corpus.py`) + the differential runner with the
  Python-vs-Python identity self-test
  (`tests/integration/test_waved_poison_corpus_differential.py`) + the readiness
  runbook (`docs/runbooks/wave-d-poison-corpus.md`); tests + docs ONLY, ZERO
  brutex/ source change; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; matches §1's
  Active branch line)
  Earlier: `claude/rust-lake-window-ledger-v2`
  (DEC-LAKE-RUST-WINDOW-LEDGER-001, MIGRATION.md #1915 — ADR-0020 Wave C-3
  (slice G8) FIRST CUT: the TWO pure window-ledger laws — status
  classification + the 5-tuple merge key — routed through `rust/brutex-lake`
  (NEW `window_ledger.rs`) behind the NEW default-OFF
  `BRUTEX_LAKE_RUST_WINDOW_LEDGER` flag via the NEW facade
  `brutex/data/window_ledger_rust.py` (per-law 16-call burn-in + ship-oracle
  autorevert; import guard + SYMBOL handshake — the polars version guard
  deliberately unused, the laws are polars-free; exact-type date gate; only
  date ints cross the FFI in, only the two ASCII date strings cross out),
  with the verbatim Python law retained as the LIVE parity oracle
  (`window_key_python` consolidates the two prior `_key` copies); record()
  streak/tombstone + merge-on-save + None back-compat stay Python — the full
  #1548 pyclass flip is the named follow-up DEC-LAKE-RUST-WINDOW-LEDGER-002;
  flag OFF byte-identical (bomb-spy pinned); crate stays 0.1.0, Cargo.lock
  untouched; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; matches §1's Active
  branch line)
  Earlier: `claude/rust-lake-stats-shard`
  (DEC-LAKE-RUST-STATS-MANIFEST-001, MIGRATION.md #1914 — ADR-0020 Wave C
  row 4 + the row-5 rider: the stats-manifest shard render on the
  `rust/brutex-lake` crate behind the default-OFF
  `BRUTEX_LAKE_RUST_STATS_MANIFEST` seam (flag OFF byte-identical +
  crate-untouched, spy-pinned; burn-in + ship-the-oracle autorevert; ints +
  isoformat-strings-only FFI) + crate-internal partition stem/validate twins
  (Python partition.py byte-UNCHANGED); render ONLY — locks/atomic write stay
  Python (the recorded narrowing vs the ADR sketch); ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line — see §1 +
  MIGRATION.md #1914 for the full blurb)
  Earlier: `claude/rust-lake-resample-wave`
  (DEC-LAKE-RUST-RESAMPLE-001, MIGRATION.md #1913 — allocator-final — Wave-B
  slice 1 of ADR-0020 (slice G5): the tf_resampler 1m→derived-TF core
  re-hosted on `rust/brutex-lake` (`resample_1m_ipc_impl` + the batch face)
  behind the NEW default-OFF `BRUTEX_LAKE_RUST_RESAMPLE` facade
  (`brutex/data/lake_rust_resample.py` — the PR-1 lake_rust_render mirror:
  tristate flag twin, import/API + polars-version lockstep guards, 16-sample
  burn-in at DTYPE-EXACT frame strength with ship-the-oracle autorevert);
  the seam inside `resample_1m_to_tf` ONLY, after validation + TF-membership,
  with the verbatim body extracted to `_resample_1m_to_tf_python` as the live
  parity oracle; wrappers + daily residues STAY Python (residues = named
  follow-up DEC-LAKE-RUST-RESAMPLE-DAILY-001); the 9-case ON-vs-OFF parity
  corpus + the whole-prebuild sha256 A/B EXECUTED green on the real wheel;
  flag OFF byte-identical (bomb-spy-pinned); honesty banner verbatim —
  expected payoff ≈ 0, uniformity not speed; ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line)
  Earlier: `claude/fill-worst-case-only`
  (DEC-FILL-WORST-CASE-ONLY-001, MIGRATION.md #1825 / ADR-0023 — WORST-CASE
  IS THE ONLY FILL MODEL: the product moves from precise
  open-anchored fills to the one worst-case law in a single hop by operator
  directive (absorbs the #1522 FillMode-mode design; the PRECISE opt-out
  never shipped); the
  adverse-max law hardcoded across the spot engine + the Rust u64/u128
  kernels + both translation executors; `canonical_trade_params` emits
  `{"fill_mode": "worst_case"}` unconditionally (anti-aliasing vs the frozen
  precise-era run_ids); precise-era artifacts + a SET `BRUTEX_FILL_MODE`
  refuse loudly naming the DEC; volume-0 bars reject uniformly on both
  executors (the futures R15 clause consciously superseded — survivorship
  tilt recorded); the adverse-levy CEIL rounding is permanent (COSTS_VERIFIED
  §5 regenerated); every golden re-locked with in-file hand-derivations, the
  vocab fixture byte-untouched; INTERIM: worst-case prices come from the
  DECISION-TF bar until PR-B's 1-minute grid — conservative, never
  optimistic; matches §1's Active branch line — see §1 + MIGRATION.md #1825
  for the full blurb)
  Earlier: `claude/lake-read-nonfinite-ohlc-guard-v2`
  (DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001, MIGRATION.md #1897 — renumbered
  #1832 → #1897 at the 2026-07-16 merge-reconcile per the steward ledger
  map — non-finite
  OHLC (inf/-inf/NaN) rejected at BOTH lake-read boundaries: the engine read
  boundary's explicit finiteness gate + the `(ValueError, ArithmeticError)`
  re-wrap (a NaN's raw `decimal.InvalidOperation` no longer escapes), the
  per-column `~is_finite` arm in the vectorized price-sanity mask (the
  `high ∈ {+inf, NaN}` blind spot) with the scalar twin widened to match,
  the window `1day` reader (`make_lake_read_1m_window`) running the
  strengthened gate for the first time (finding #12), and `ArithmeticError`
  in the prebuild per-symbol quarantine set; two pre-existing latent
  defects, executed-repro'd on planted/corrupt STORED data, fails-before
  proven at merge-base 6b9947d9, healthy-lake byte-identity + frozen engine
  goldens pinned UNMODIFIED; named follow-up DEC-DAILY-WINDOW-BADTICK-001;
  ENGINE UNCHANGED on clean feeds — NIFTY/BANKNIFTY/SENSEX; matches §1's
  Active branch line — see §1 + MIGRATION.md #1897 for the full blurb)
  Earlier: `claude/staged-sequencer-seams`
  (DEC-STAGED-SEQUENCER-SEAMS-001, MIGRATION.md #1900 — the staged sequencer
  seams, dual-vendor orchestration WS-B per the frozen ADR-0021 design
  contract + the coordinator-relayed operator rulings: additive staged
  callables on the ONE `run_full_pipeline` (canonical order SPOT-DAILY →
  SPOT-1M → SPOT-PREBUILD → METADATA → FUTURES → FUTURES-PREBUILD → OPTIONS
  → GREEKS; ABSENT-vs-FAILED vacuously-settled contract with the distinct
  `absent` token; per-stage fail-closed gates, sequencer-minted
  `stopped_after`; per-vendor-stage market-hours belt; `pipeline.stage.*`
  events) + the structural `StageOutcome` protocol (leaf
  `dashboard/stage_outcome.py`; the five Groww report shapes are satisfying
  instances via additive `.ok`) + the F&O spine `step_filter` (filtered
  buckets honestly absent, never fake-settled) + the `WriteDomain`/Pin D1-1
  cross-domain refusal primitive (leaf `data/write_domain.py`; allowed-set
  typing, structural whole-`feeds/` primary exclusion, negative-pin
  battery; NG4E-1 maintenance-lane scoping + NG4E-2 restore-ordering
  proposal documented). ALL-None defaults byte-identical (frozen parity +
  origin/main A/B); NO production caller binds the new seams — WS-C/WS-D;
  ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch
  line — see §1 + MIGRATION.md #1900 for the full blurb)
  Earlier: `claude/vwap-ema-price-source-axis`
  (DEC-NIFTY-PRICE-SOURCE-AXIS-001, MIGRATION.md #1681 — a NEW `PriceSource`
  cell-coordinate sweep axis for the price source of VWAP + EMA20/EMA200 over
  {open,high,low,close,hlc3}, mirroring the `fib_anchor` axis; DEFAULT-OMITTED
  (baseline (hlc3,close) → existing run_ids BYTE-IDENTICAL, no
  indicator_version/crate bump, no Rust change — Stage-1A stays Python); threaded
  through orchestrator/kernel-cache/parallel/supervisor/verify/results_depth/
  cascade + Stage-3; CLI `--price-source-cells`; each non-default cell staged as
  a separate cardinality-1 invocation ≤ 2 days/run; ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line)
  Earlier: `integration/club-q-dashboard`
  (CLUB Q merge vehicle — the file-disjoint fold of THREE PRs as ONE merge
  event off main tip d6ac9f85: #1434 memory/cache + RAM-residency hardening
  (DEC-MEMORY-FOOTPRINT-HARDENING-001, MIGRATION.md #1721) · #1433 the brutexd
  static/health Rust front, default-OFF `BRUTEX_RUST_FRONT`
  (DEC-RUST-FRONT-STATIC-HEALTH-001, MIGRATION.md #1733) · #1552 the operator
  single-portal refold + the relocated #1749 cascade render
  (DEC-OPERATOR-PORTAL-REFOLD-001, MIGRATION.md #1796); only
  docs/MIGRATION.md + CLAUDE.md are union files; READ-side dashboard /
  display / a default-OFF serving sidecar only — ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line — see §1 + the
  member MIGRATION.md entries for the full blurbs)
  Earlier: `claude/panel-extract-refold`
  (DEC-OPERATOR-PORTAL-REFOLD-001, MIGRATION.md #1796 — the operator
  single-portal consolidation of PR #1470 (DEC-OPERATOR-PAGE-CONSOLIDATE-001)
  REFOLDED onto current main tip 1ad31ac: the extracted shared modules
  (bx-portal-state / bx-screener-panel / bx-strategy-panel / bx-operator-portal
  + bx-oneclick-panels.css) + the additive BXOneclick getTop100/onRunChange
  seam + the strategy-panel/template-side ocTradesSource→stTradesSource
  rename, with main's
  DEC-RESULTS-CASCADE-VERDICT-JOIN-001 (#1749) cascade verdict render RELOCATED
  from strategy.html into the extracted bx-strategy-panel.js (stCascade/
  stCascadeDeep markup mirrored into BOTH copies + the moved `.cascade-fail`
  CSS into bx-oneclick-panels.css); SUPERSEDES PR #1470 (recommend
  close-as-superseded); DISPLAY zone only, ZERO Python source changes; ENGINE
  UNCHANGED — NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line — see §1
  + MIGRATION.md #1796 for the full blurb)
  Earlier: `claude/memory-footprint-hardening`
  (DEC-MEMORY-FOOTPRINT-HARDENING-001, MIGRATION.md #1721 — the memory/cache
  hardening pack, items 1-6: the `registry.duckdb` content token re-keys
  the options calendar/strike/candidate caches on a cross-process metadata
  pull, the COMMON `MoversSnapshotCache` gains the opt-in RAM-derived
  byte watermark (+ the `LakeOptionBarSource` bar-cache LRU), the
  window-ledger cache residency collapses to ONE resident per-row family
  (records; the /db grid rows an O(page) on-demand projection), the oneclick
  events-page cache is PARTITIONED off the expensive payload families (+ the
  three reader memos consolidate onto the ONE bounded single-flight
  `_RevisionMemo`), the /results non-preset rank becomes a bounded
  order-parity-pinned `heapq.nsmallest` top-k, and the duplicate/dead
  cleanup lands (production-dead `FnoCascadeCache` deleted; the
  rate-bridge/sweep-progress copy-twins delegate to the ONE
  `NonBlockingFileMemoBase`, both suites unmodified); READ-side only,
  ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line)
  Earlier: `claude/options-prebuild-retire`
  (DEC-OPTIONS-PREBUILD-RETIRE-001, MIGRATION.md #1831 — derived-timeframe
  prebuild RETIRED for OPTIONS (CE/PE): options are 1-minute-native per the
  operator ruling of 2026-07-15 as relayed via the coordinator (ADR-0021
  RULING B, draft PR #1555); supersedes-IN-PART DEC-PIPELINE-AUTOPREBUILD-001
  — the OPTIONS arm only. Both producers gated (pipeline seq+parallel via the
  un-injected options callables + the honest by-design no-op note; the
  lake-heal daily recall via the COMMON `is_option_contract_coords` gate,
  counted + one capped batch log), the movers CE/PE prev-close/prev-OI
  baselines skip the frozen 1day for the bounded 1-minute prior-session
  walk-back (SPOT + FUT byte-identical, pinned), the doctor
  derived-freshness verdicts exclude CE/PE; stored derived option TFs stay
  inert on disk (never deleted). ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX;
  matches §1's Active branch line — see §1 + MIGRATION.md #1831 for the full
  blurb)
  Earlier: `integration/club-g-docs`
  (Club G docs merge vehicle — the docs-only fold of #1506
  DEC-GDFL-VENDOR-DOCS-001 (MIGRATION.md #1780) + #1508
  DEC-GDFL-INGEST-DESIGN-001 (MIGRATION.md #1781); DOCS-ONLY — zero
  brutex/ source change; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX;
  matches §1's Active branch line — see §1 + MIGRATION.md #1780–#1781 for
  the full blurbs)
  Earlier: `integration/club-r2-crates`
  (CLUB R2 merge vehicle — the crate-disjoint fold of #1469
  DEC-GREEKS-RUST-BATCH-001 (MIGRATION.md #1892) → #1435
  DEC-RUST-DATALAKE-IO-ROADMAP-001 (#1891) → #1454 DEC-FEED-RUST-CORE-001
  (#1797); three independent crate trees, every Python seam default-OFF /
  INERT; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; matches §1's Active
  branch line — see §1 + MIGRATION.md #1892 · #1891 · #1797 for the full blurbs)
  Earlier: `claude/gdfl-ingest-design`
  (DEC-GDFL-INGEST-DESIGN-001, MIGRATION.md #1781 — the GDFL 1-second L1
  snapshot ingest/store/derive/read DESIGN doc at
  `docs/vendors/gdfl/INGEST_DESIGN.md`; DESIGN ONLY — zero adapter code, the
  build gated on the operator confirming the GDFL purchase; the new `snap1s/`
  raw tree (vendor-ticker keyed, keep-first `(timestamp, seq)`, byte-identity
  re-ingest law) + the CSV parse taxonomy + the ticker ambiguity law (raw
  archival never blocked by resolution) + provenance-isolated `-G1S` derived
  1m bars under bars/ + /db read surfaces mirroring the options pager + the
  44-row edge matrix + the honest-limits/licensing gate (raw GDFL data never
  committed; `snap1s/` + `*-G1S` bars excluded from off-box backup until the
  licensing read — a named DEC-BACKUP-OFFBOX-001 divergence); DOCS-ONLY —
  zero source change; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; matches §1's
  Active branch line — see §1 + MIGRATION.md #1781 for the full blurb)
  Earlier: `claude/gdfl-vendor-docs`
  (DEC-GDFL-VENDOR-DOCS-001, MIGRATION.md #1780 — the SUMMARIZE-ONLY GDFL
  vendor-docs archive + the definitive granularity/depth verdict at
  `docs/vendors/gdfl/`: GDFL's finest history granularity is 1-second L1
  SNAPSHOT data (vendor enum TICK — irregular cadence, not a fixed 1-second
  grid, not sub-second tick-by-tick; the ceiling for Indian retail data), NO
  1-second OHLC bar periodicity exists, days-scale (unconfirmed figure — never
  years-scale) snapshot history ⇒ backfill
  impossible / forward capture only; evidence-tiered, zero verbatim vendor
  prose/source/data committed (summarize-only licensing ruling; pinned PyPI
  SHA256 re-fetch digests); the ingest design is split out under
  DEC-GDFL-INGEST-DESIGN-001 (coordinator-reserved, not yet in the ledger);
  DOCS-ONLY — ZERO brutex/ source change; ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line — see §1 +
  MIGRATION.md #1780 for the full blurb)
  Earlier: `claude/operator-health-truth`
  (DEC-OPERATOR-HEALTH-TRUTH-001, MIGRATION.md #1747 — the operator-page
  HEALTH TRUTH pack: the heartbeat pull slot's unconditional green "last pull
  completed cleanly" on `flow_state === 'done'` is KILLED — the pull-rate
  payload gains the additive `last_pull` OUTCOME block (NEW pure
  `pull_outcome.py` over the job slot's recorded state + the pull-verdict
  holder) so a HALTED pull renders its named degraded state and green appears
  only on the classified clean; plus ONE unified health hub
  (`GET /api/v1/health/hub` + `health_hub.py` + the bx-health-hub.js BXPoll
  widget) folding nightly · backup-dead-man · doctor · mutation · pull lanes
  from durable local state — unwired lanes read the explicit "not wired",
  never a fabricated green; READ-side only; ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line)
  Earlier: `claude/bulk-spot-completeness-flag`
  (DEC-BULK-SPOT-COMPLETENESS-FLAG-001, MIGRATION.md #1794 — renumbered
  #1792 → #1793 → #1794 across the 2026-07-14 merge-reconciles, main took
  #1792 for DEC-SPOTONLY-PREBUILD-EMPTY-EXCLUDE-001 then #1793 for
  DEC-SETTLE-EVIDENCE-ADOPTION-001 — the bulk
  whole-universe spot pull arms `detect_completeness_gaps`:
  `pull_all_spot`'s ONE spine call now threads
  `detect_completeness_gaps=not dry_run` (the cli.py idiom, hoisted beside
  the #1774 `_today_gate`), so the #1031 interior-gap /
  trailing-truncation demotions run on every daily production path —
  nightly, ▶ Pull Spot + auto-pull, /pull-all, retry-failed, the
  crossverify pull legs — closing #1774's honest-envelope item (i); the
  spine's `is_index` gate keeps the ~750 illiquid CASH names observe-only
  on the gap/truncation class (the intrinsic ALL_FILTERED demotion stays
  deliberately un-index-gated),
  dry-runs stay double-protected (flag False + record-inert None ledger),
  demotions settle SKIPPED at threshold 3 (bounded; `brutex
  reopen-skipped`); NOT retroactive; detectors abstain out-of-coverage
  from 2027 until the NSE list lands; ONE call site + docs + tests, the
  detectors + spine byte-untouched; pull-INTEGRITY only — ENGINE
  UNCHANGED — NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line —
  see §1 + MIGRATION.md #1794 for the full blurb)
  Earlier: `claude/settle-evidence-adoption`
  (DEC-SETTLE-EVIDENCE-ADOPTION-001, MIGRATION.md #1793 — the measured-settle
  EVIDENCE-FILE adoption channel: the #1775 probe's `--summarize
  --emit-evidence PATH [--margin-minutes N]` folds ≥5 REGULAR probe days into
  the committed schema-1 `docs/groww/probe/settle_evidence.json`, and the
  `--auto`/scheduler settle resolution becomes the validated three-tier law
  env > evidence > the conservative 17:30 default (NEW
  `calendars/settle_evidence.py` validator + memoized loader; explicit
  `BRUTEX_SETTLE_EVIDENCE_PATH` junk fails loud — the resolve_* law;
  default-path corruption = one WARN + the 17:30 fallback — the r6/r7 boot
  posture; the CI committed-file test is the fail-loud tier for committed
  content); `resolve_settle_time` wraps NEW `resolve_settle_resolution`
  (tier-1 short-circuit keeps `--settle-ist`/env supremacy, e2e-pinned); the
  `before_settle` detail carries per-tier provenance — the DEFAULT arm
  byte-identical incl. the verbatim UNMEASURED clause; the shared
  regular-file-only loader MOVED to COMMON `brutex/data/sidecar_read.py`
  (pull_intent re-exports byte-compatibly — the #1715 leaf+shim precedent);
  both eager-validation surfaces probe the channel so a junk explicit path
  fails loud even under a tier-1 override; ships the CHANNEL, ships NO
  measured number — the no-evidence resolution is BYTE-IDENTICAL to #1779
  (parity-pinned) and the measured value is operator work (probe → emit →
  review → commit → CI validates → adopt); ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line — see §1 +
  MIGRATION.md #1793 for the full blurb)
  Earlier: `claude/spotonly-prebuild-empty-exclude`
  (DEC-SPOTONLY-PREBUILD-EMPTY-EXCLUDE-001, MIGRATION.md #1792 — the #1779
  vendor-EMPTY × prebuild × movers-warm residual CLOSED: zero-stored-bars
  spot symbols classify NO-SOURCE at the prebuild read (the retyped
  whole-partition `NoStored1mBarsError` arm — lake truth, never the
  #1779-rejected report `no_data`; spot phase catches it first, parallel
  worker kind-gated; months-bounded arm stays plain ValueError), counted +
  named on the note + emit-on-nonzero payload keys + the CLI `no-source:`
  line; `PrebuildOutcome.ok` unaffected so the movers warm fires over the
  healthy majority (the warm-side exclusion is structural when NO timeframe
  holds parquet — discovery is parquet-file-gated, re-pinned; a surviving
  stored DERIVED timeframe legitimately keeps a symbol discoverable — the
  refuter-round-2 scoping, the "+ movers warm" note tail dropped); the
  inherited e2e pin consciously
  flipped + the healthy-incremental-re-run trap twin is the permanent
  regression net; a mixed no_source + REAL failure still withholds the warm;
  default path byte-identical (note/wire/stdout literal-pinned); named
  follow-up DEC-PREBUILD-EMPTY-CAUSE-ATTRIB-001 (cause attribution); ENGINE
  UNCHANGED — NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line — see
  §1 + MIGRATION.md #1792 for the full blurb)
  Earlier: `claude/crossverify-owed-sentinel`
  (DEC-CROSSVERIFY-OWED-SENTINEL-001, MIGRATION.md #1810 — renumbered
  #1792 → #1810 at the 2026-07-15 club-T fold, main took #1792 for
  DEC-SPOTONLY-PREBUILD-EMPTY-EXCLUDE-001 — the durable
  cross-verify OWED-day sentinel fixing the confirmed umbrella-assurance
  MAJOR-1: both one-click Pull-Spot launchers write a durable owed marker at
  pull-tail-commit ENTRY (NEW `brutex/crossverify/owed.py` —
  `<lake>/.crossverify/owed/<day>.json`; names-only scan, loader-disciplined
  reads, best-effort-loud, kill-switch coherent), the runner's NEW step D3
  completes a never-started owed prior day on the next run with the manual
  `--day` machinery verbatim (the `CrossverifySeams.run_day` recursive
  rerun — journal idempotency ⇒ zero double-puts), the marker clears ONLY at
  the day's completed material verdict (crash-convergent; honest
  non-trading/Muhurat skips clear, deferrals/failures keep), and owed days
  read LOUD on the /universe panel via the EMIT-ON-PRESENT `owed_days`
  progress field (no-owed wire byte-identical, pinned); ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line — see §1 +
  MIGRATION.md #1810 for the full blurb)
  Earlier: `claude/sweep-lock-pull-concurrency`
  (DEC-SWEEP-LOCK-PULL-CONCURRENCY-001, MIGRATION.md #1785 — the dashboard
  sweep route + the nightly sweep leg stop holding the LAKE pull flock through
  the sweep: `cross_process_lock=False` at the sweep route's job start + the
  nightly lake lock released after PULL+PREBUILD (the account-lock pattern) —
  cmd_sweep_all's OWN G10 preload-only SH read-guard + G6 sweep single-flight
  own the sweep-vs-pull exclusion (CLI parity); fixes the confirmed G10
  self-conflict BLOCKER (LakeBusyError at every dashboard/nightly sweep
  preload since 3f32b3e) and lets the daily pull + crossverify run
  CONCURRENTLY with a multi-day sweep's compute phase; SUPERSEDES #1612's
  held-through-SWEEP clause; run identity frozen at preload, byte-identity
  proven by the executed concurrency regression
  (tests/integration/test_sweep_pull_concurrency.py); lock COMPOSITION only —
  ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line —
  see §1 + MIGRATION.md #1785 for the full blurb)
  Earlier: `claude/pull-scoped-all-oneclick`
  (DEC-PULL-SCOPED-ALL-ONECLICK-001, MIGRATION.md #1779 — the one-click
  scoped-ALL SPOT pull: the additive `scope=spot-only` token on the ONE
  `/pull-pipeline` launcher + the `#pullAllSpotBtn` button + the
  `brutex pull-all-spot` CLI over `build_headless_pipeline(scope=…)`; no-op
  F&O reports through the unchanged sequencer + note surgery + explicit
  Greeks-not-run text; fail-soft crossverify tail (production default = the
  REAL #1776 producer, fired strictly outside our held pull single-flight);
  the CLI `--auto` gate (UNMEASURED-conservative 17:30 IST settle default,
  `BRUTEX_PULL_ALL_SETTLE_IST`, floor 15:35) + Muhurat whole-day deferral +
  the composed mid-run Muhurat belt — `market_hours.py` byte-untouched, the
  belt honoring the SAME `BRUTEX_MARKET_HOURS_GUARD` kill switch (guard off ⇒
  belt off — the hostile-review r1 ruling);
  spot-only intents resume spot-only — and a REFUSED concurrent launch
  restores the running pull's persisted intent (refuter-round-3 F-1;
  the pre-start persist no longer clobbers it), with the whole launcher
  intent transaction serialized under the per-store process-local
  `launch_mutex` since the refuter-round-4 fold (G-1/R4-1 — raced sibling
  clicks can no longer orphan-clear or re-clobber the running pull's
  intent; cross-process exclusion stays the flock); since the round-5 fold
  the button SELF-ARMS the daily post-market auto-pull — NEW
  `dashboard/auto_pull_scheduler.py` (own ci.yml 100% gate): `auto_arm=1`
  on the button's first click arms a durable daemon ticker that fires the
  SAME spot-only launcher once per day when the EXISTING `auto_pull_allowed`
  gate opens (refused launches — e.g. a whole-run sweep — retry on later
  ticks; a successful manual click counts as the day's pull; disarm chip +
  toggle route; boot resumes ticking iff durably armed; zero threads
  otherwise; the CLI `--auto` + cron lane stays the headless alternative);
  since the round-6 + round-7 folds the durable state loads
  (`.pull_all_spot_auto.json` + `.pull_intent.json`) are corruption-hardened
  for EVERY shape, non-regular inodes included — the round-6 #1776 M1 decode
  fold (a corrupt file never crashes the boot or 500s a click) completed by
  the round-7 P-1 re-hardening on the #1614 `O_NONBLOCK`+`S_ISREG`
  regular-file-only loader (a planted FIFO / device symlink no longer HANGS
  the boot or the click; the 16 MiB cap moved onto the fd's `fstat`, closing
  the stat→read TOCTOU — both hangs F/B-proven at the pre-fix head in
  bounded subprocesses) — and the
  combined pipeline button is SPLIT into ▶ Pull Spot (auto-arming) + the NEW
  manual ▶ Pull F&O (`scope=fno-only` — the exact spot-only mirror: spot
  phase = honest no-op with note surgery, metadata/historical/Greeks real,
  F&O-only prebuild + futures/options warm; the crossverify tail + the
  scheduler stay Spot-EXCLUSIVE, pinned — since the round-7 fold the
  verdict-clobber + never-marks-the-day exclusivities are negative-pinned
  too, refuter Q's surviving mutants killed; the bare route stays the
  full-chain API default byte-identical; the headless binding refuses
  fno-only — `pull-fno` CLI twin is the named follow-up);
  ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line — see §1 +
  MIGRATION.md #1779 for the full blurb)
  Earlier: `claude/o1-guarantee-harden`
  (DEC-O1-GUARANTEE-HARDEN-001, MIGRATION.md #1791 — the O(1)-guarantee
  verification + hardening pass: NEW `docs/O1_GUARANTEE_AUDIT.md` is the
  authoritative honest per-path complexity classification (51 rows across
  the engine hot paths / DB-lake reads / frontend; no bare-O(1) claim where
  the first read is O(files); the merge-blocking vs post-merge enforcement
  split stated plainly); the M4-Pro ResultsStore.get "1.87×" report triaged
  VERIFIED-NOT-A-REGRESSION (one-line dict probe, measured flat at 100×
  depth; the #1301 cache-tier re-gate absorbs the physics band; NEW
  deep-depth pin); five hardenings (the NEW COMMON `BXUI.fetchTextBounded`
  on the last 3 unbounded-body poller templates; the oneclick timeline
  dup-tick skip token + NAMED newest-500 render cap + 200 ms search
  debounce; universe.html's seven loaders on ONE bounded latest-wins
  helper; the /db registry filtered-COUNT memo on the NEW
  `registry_revision` token; the NEW `RevisionProbeMemo` TTL-bounding the
  leaderboard + oneclick-runs O(dirs) probes — ≤1 s disclosed staleness,
  kill switch) + eight bench gates; READ-side dashboard + tests + docs
  only; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no
  lake/schema/scope/vocab/rate change; matches §1's Active branch line)
  Earlier: `claude/engine-spot-only`
  (DEC-ENGINE-SPOT-ONLY-001, MIGRATION.md #1790 — the brute-force ENGINE is
  SPOT-ONLY: `NSE-NIFTY-FUT` RETIRED from engine sweep scope, every
  production sweep entry refuses it loudly citing the DEC + "the engine is
  spot-only; NSE-NIFTY-FUT remains lake/REFERENCE_ONLY"; consciously
  supersedes the ADR-0012 engine-futures authorization + the
  DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001 launch surface; futures machinery
  retained below the gate as lake/REFERENCE_ONLY tooling; Groww pull/lake
  scope untouched; ENGINE surface now EXACTLY NIFTY/BANKNIFTY/SENSEX;
  matches §1's Active branch line — see §1 + MIGRATION.md #1790 for the
  full blurb)
  Earlier: `claude/crossverify-backfill`
  (DEC-CROSSVERIFY-WITHHELD-REGATE-001, MIGRATION.md #1789 — the cross-verify
  WITHHELD-day AUTO-RE-GATE folded into the daily run's catchup (step D2:
  prior days' gate-withheld symbols re-gated from the since-healed lake —
  re-export → stage → publish the now-complete ones, journal withheld map
  rewritten, published keys never re-put, fail-closed gate-first, per-day
  LEAF journal flocks, runs before the session gate; closes the #1776-named
  follow-up DEC-CROSSVERIFY-BACKFILL-001 — id retired unused, the historical
  whole-range S3 backfill was STOOD DOWN by operator ruling 2026-07-13) +
  the persistent last-run failure surface (`<lake>/.crossverify/last_run.json`
  → the additive `last_run` progress field → the /universe panel renders it
  on load; the retryable-exhaustion publish_failed verdict gains a real
  failing-keys detail); POLISH ROUND folded — a MATERIAL skip-day run
  (regated/recovered > 0) summarises (WARN catch-up tier) + writes the
  last-run record (a true no-op skip stays silent — Friday's failure record
  survives), the D2 enumeration is guarded FAIL-OPEN
  (`crossverify.regate_scan_failed` — the main day never sinks), and the
  journal scans' oldest-boundary is mutation-pinned; stacked on PR #1515's
  `claude/crossverify-spot-only`
  head; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; matches §1's Active
  branch line — see §1 + MIGRATION.md #1789 for the full blurb)
  Earlier: `claude/crossverify-spot-only`
  (DEC-CROSSVERIFY-SPOT-ONLY-001, MIGRATION.md #1788 — the cross-verify
  export contract AMENDED to SPOT-ONLY over the WHOLE spot universe
  (operator ruling 2026-07-13): futures OUT — the metadata + FUT-candle pull
  legs, the registry read and the G2 FUT settle-retry class DELETED; no
  futures pulls, no futures CSVs (TickVault must NOT expect futures files —
  absence is BY DESIGN; negative-pinned: an FNO-carrying lake + a registry
  with live FUT rows yield ZERO `*_FNO` keys) — and whole-spot-universe IN:
  the ONE `whole_universe_spot_targets` enumeration (ALL ~25 indices incl.
  INDIAVIX/sectorals via the widened canonical INDEX arm + ALL ~751 stocks
  incl. reference-only NTM names) shared by pull AND export, the engine trio
  kept as the structural enumeration floor; INDEX policy TIERED per the
  approved index-policy ruling (fix round) — strict count+close-edge
  withhold on the VERIFIED engine trio only, the other ~22 indices
  observe-only (published-with-a-counter; zero bars = honest nothing;
  forming last bar withholds) with the
  strict-published/observe-published/withheld three-way split in the
  summary; CSV byte contract / gate binding
  / S3 posture / 1-minute law / journal-publish machinery UNCHANGED; the FUT
  mapper/renderer/gate/prebuild arms retained STRUCTURAL
  (direct-unit-covered); DEC-CROSSVERIFY-SENSEX-FUT-INGEST-001 CLOSED MOOT;
  ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch
  line — see §1 + MIGRATION.md #1788 for the full blurb)
  Earlier: `claude/quad-dashboard-lakeinv-universe-docs`
  (QUAD MAX-CLUB merge vehicle — the file-disjoint fold of FOUR PRs as ONE merge:
  #1440 dashboard lane club (DEC-DASHBOARD-POLL-HYGIENE-002 #1699 ·
  DEC-DASHBOARD-READ-SCREEN-001 #1759 · DEC-DB-TZ-MONEYNESS-FIXES-001 #1760 ·
  DEC-DB-MONEYNESS-STOCK-EVIDENCE-001 #1761) · #1450 DEC-LAKE-INVARIANTS-PREINDEX-001 #1762 ·
  #1288 DEC-DASHBOARD-READPATH-WEDGE-001 #1763 · #1441 docs club DEC-EAP-FEEDBACK-INDEX-001
  #1764; READ-side dashboard / lake-heal SPACE / docs only — ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; see §1 Active branch for the full blurb)
  Earlier: `claude/dashboard-read-honesty-pack`
  (DEC-DASHBOARD-READ-HONESTY-PACK-001, MIGRATION.md #1795a — the dashboard
  underlying-key READ-honesty pack, ONE umbrella entry (+ #1795b–#1795d
  companion headers for the sub-IDs DEC-MAPPING-INTEGRITY-FORM-KEY-001 /
  DEC-MOVERS-STORED-FRESHNESS-001 / DEC-FNO-UNDERLYING-EMPTY-GUARD-001;
  consolidated from the pre-merge #1751–#1753 after main's PR #1488 took
  #1751), REFUTER ROUNDS 1 + 2 + 3 + 5 + 7 folded: the /universe mapping-integrity compare
  keyed through the COMMON F&O underlying-forms resolver via the injected
  plain-data `key_forms` map (the writer's KEYING byte-untouched — the writer
  file gains only the sub-(c) blank-row isolation; default-None
  byte-identical; the hyphenated/aliased silent-vanish alarm live; the round-5
  CROSS-OWNER guard — a normalize-colliding sibling's coverage can never mask
  an own-key attempted-and-zero — plus the MI-6 attempted-match tooth;
  round-7: the materialize-once memo — a single-shot `covered_years`
  iterator value can no longer exhaust into a false-RED), the
  /movers stored-first intraday read freshness-guarded by VERIFY-EQUALITY
  (a stored TF day frame serves only when it equals the in-memory resample of
  the 1-minute base — the round-1 day-max predicate was refuted by the
  executed #1031 interior-hole re-pull chain; stale prebuilt TF ⇒ the existing
  1m-resample fallback + one capped `movers.stored_stale_fallback` WARN,
  dedupe key (exchange, symbol, interval); the :351 stale-serve pin
  deliberately FLIPPED; the round-2 month-edge fold probes the PRIOR UTC
  month's file for sub-05:30-IST bucket labels on the 1st of an IST month —
  a fresh month-edge lake serves its COMPLETE stored frame, never a permanent
  false-stale WARN or main's silent bucket drop; the round-3 fold gates the
  probe `day > date.min` — the `0001-01-01` OverflowError 500 closed,
  route-pinned; round-7: the equality wording corrected to value-exact,
  dtype-INSENSITIVE polars `.equals`, drift-pinned; daily arm = named follow-up
  DEC-MOVERS-DAILY-FRESHNESS-001), and `primary_fno_underlying` made total
  for the blank-symbol degenerate (IndexError → self-key; blank rows skipped
  + counted at the kind-map AND the metadata grid — the round-1-falsified
  "inert key" metadata seam closed; round-2: the pre-fix blank cells also
  stopped the full pipeline after metadata via `metadata_phase_ok`);
  READ-side dashboard only; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX;
  matches §1's Active branch line)
  Earlier: `claude/crossverify-producer`
  (DEC-CROSSVERIFY-PRODUCER-001 + DEC-CROSSVERIFY-S3-WRITE-POSTURE-001 +
  DEC-CROSSVERIFY-DAY-DERIVE-001, MIGRATION.md #1776–#1778 — the daily
  TickVault cross-verification producer: NEW `brutex/crossverify/` package
  running the zero-touch post-close chain (priority pull [ALL spot +
  NIFTY/BANKNIFTY futures, NO options] → fail-closed completeness gate →
  day-scoped never-stamping derive → the pinned per-symbol CSV byte
  contract → idempotent journal-keyed S3 publish, PutObject-ONLY on
  `tv-prod-cold/crossverify/*` — BruteX's FIRST sanctioned AWS write
  surface, SSM read-only pledge unchanged), surfaced as `brutex crossverify
  {run,install,uninstall,status}` (launchd `com.brutex.crossverify`, `--at`
  REQUIRED probe-decided, trading-day-aware journal deadman) + the /universe
  dashboard button on a dedicated BackgroundPullJob riding the SAME runner;
  correct-or-absent (withheld + CRITICAL, never wrong bytes; Muhurat days
  skip entirely; missing index day = exit 2 — real-gate `count_short`,
  the INDEX `no_today_bars` BLOCKED arm defensive); GREEKS N/A (options-only
  stage, options-excluding pull); BSE_EQ/BSE_FNO structural-only;
  SENSEX-futures ingest + true preemption = named follow-up DECs; ENGINE
  UNCHANGED — NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line)
  Earlier: `claude/pull-today-completeness-gate`
  (DEC-PULL-TODAY-COMPLETENESS-GATE-001 + DEC-GROWW-POSTCLOSE-SETTLE-PROBE-001,
  MIGRATION.md #1774–#1775 — the pre-settle TODAY minute-completeness gate:
  covers-today windows withhold today's still-settling bars from the keep-first
  lake and defer streak-EXEMPT re-pullable FAILED (`TODAY_INCOMPLETE`) until the
  per-class criteria pass; armed at all four production pulls behind the
  fail-loud `BRUTEX_TODAY_COMPLETENESS_GATE` switch; + the read-only post-close
  settle probe under docs/groww/probe/; ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line — see §1 +
  MIGRATION.md #1774–#1775 for the full blurb)
  Earlier: `claude/premerge-m4pro-nonblock`
  (DEC-PREMERGE-M4PRO-NONBLOCK-001, MIGRATION.md #1753 — the pre-merge gate
  treats the non-required self-hosted `m4pro-absolute-throughput` check as
  NON-BLOCKING: a HARDCODED `NONBLOCKING_CHECK_NAMES` constant in
  `scripts/ci/pre-merge-check.sh`, UNIONED with the caller's
  `IGNORE_CHECK_NAMES` at all three rollup scan sites so auto-merge.yml's
  caller-SET value cannot clobber it; a FAILED m4pro warns, never blocks
  at this gate (auto-merge's own untouched ci-check still refuses a
  FAILED/CANCELLED m4pro — pre-existing; PENDING is non-blocking
  end-to-end);
  the offline-runner pending-forever shape can never refuse a merge (the
  manual-cancel `bless-baseline` residual is named in #1753 — documented,
  not broadened, per the operator only-m4pro constraint);
  every REQUIRED check still blocks — redteam-pinned + fails-before-proven;
  safe per MIGRATION.md #382 + #1534 (not in ci-ok needs; bench regressions
  caught post-merge on the self-hosted runner); CI-gate script + redteam
  tests + docs only; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; matches
  §1's Active branch line)
  Earlier: `claude/read-guard-parity`
  (DEC-READ-GUARD-PARITY-001, MIGRATION.md #1811 — renumbered #1762 → #1811
  at the 2026-07-15 club-T fold, main took #1762 for
  DEC-LAKE-INVARIANTS-PREINDEX-001 — read-boundary guard
  parity: the #1616 bad-tick screen reaches the derived-TF prebuild's 1m
  read seam (NEW pure frame-form `bad_tick.screen_frame_bad_ticks`, gated
  after the price-sanity gate in `make_lake_read_1m._read`, clean lakes
  byte-identical — pinned), the movers raw-1m seam `_read_cut_1m_frame`
  gains the mirrored price-sanity gate [RECONCILED at the club-T fold onto
  main's landed #1759 `screen_frame` — row-level drop first, the gate is the
  inert belt behind it; never a laundered bucket / CandleBar 500 holds], and five db_view
  stats-manifest claim sites relabel to the #1614 honest envelope (the
  window-ledger survivor kept + doc-pinned); READ-side only; ENGINE
  UNCHANGED — NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line)
  Earlier: `claude/dashboard-cold-io-deadline`
  (DEC-DASHBOARD-COLD-IO-DEADLINE-001, MIGRATION.md #1832 — bounded cold lake
  I/O on the dashboard read path: the SummaryCache cold single-flight wait
  gains a TOTAL budget + per-owner-Event fail-fast latch with the honest 503
  degrade on the five summary routes (four JSON `summary_scan_pending`;
  scoreboard.csv text/plain) (BUG-1), and the
  read-side lake metadata sweeps — the stats-manifest consult, the /db TOCTOU
  re-stat, `_bars_file_windows`, `lake_revision`'s generation probe — ride the
  ONE new breach-armed `StatSweepGate` leaf (`brutex/data/stat_deadline.py`)
  with named `consult_deadline`/`restat_deadline` fallback clauses, seed
  suppression on a timeout, and zero-thread latched polls (BUG-2); knobs
  `BRUTEX_SUMMARY_COLD_WAIT_S` 60 s / `BRUTEX_LAKE_STAT_DEADLINE_S` 5 s, both
  fail-safe; write-side hooks byte-untouched; healthy-path stat counts + bytes
  unchanged; the movers_cache sibling wait is the named follow-up
  DEC-DASHBOARD-COLD-IO-DEADLINE-002; READ-side dashboard + one data leaf —
  ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line —
  see §1 + MIGRATION.md #1832 for the full blurb)
  Earlier: `claude/lake-limiter-equity-launch`
  (DEC-DASHBOARD-LAKE-IO-LIMITER-002, MIGRATION.md #1757 — renumbered
  #1751 → #1752 → #1757 across the 2026-07-12 merge-reconciles: main took
  #1751 for DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001 (PR #1488) and
  #1752–#1756 are the coordinator's reserved post-#1488 drain block for PRs
  #1486/#1487/#1485/#1482 + one further reserved slot — the lake-limiter
  completion for the two straggler route families: GET /api/v1/oneclick/equity
  joins its six siblings on `await _run_lake(_resolve)` and the THREE
  /api/v1/launch/* offloads ride the NEW kw-only `run_lake=` injection seam on
  `register_launch_routes` (None → the lazy default-pool `run_in_threadpool`,
  byte-identical for direct callers) — a wedged lake volume pins the 32 lake
  tokens, never the default pool; teeth = the combined 44-request realserver
  starvation regression + counting-offload dispatch pins + the import-free AST
  drift pin over brutex/dashboard/'s five sanctioned bare sites; READ-ONLY
  dashboard liveness; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; matches §1's
  Active branch line)
  Earlier: `claude/pull-contention-feed-decoupled`
  (DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001, MIGRATION.md #1751 — the
  dashboard pull-contention / feed-resilience slice: SWR serve-last-known on
  the discovery / windows / ledger / tables caches (tokenless background
  revalidators + newest-wins guard, payloads byte-identical pinned), the
  FNO-pruned /db spot-cascade walk, six self-recovering db.html loaders with
  keep-last-good, and the event-loop watchdog (`loop_watchdog` +
  `SweepAlerter.loop_stalled`); the r1 `_LockedReadConn.cursor()` BLOCKER +
  r7 wedge-degrade MAJOR + r8 probe→compute TOCTOU folds; READ-side
  dashboard + observability only — ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line)
  Earlier: `claude/sweep-core-slice0`
  (DEC-SWEEP-COMPUTE-FOLD-DECOMP-001, MIGRATION.md #1685 — Slice 0 of the Rust
  sweep-core conversion: the behavior-identical fold/combine decomposition of
  `brutex/metrics/compute.py` (`TradeFolds` + `_fold_trades` +
  `combine_metrics_from_folds`), byte-identity golden-proven — zero Rust in
  this slice; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX)
  Earlier: `claude/rust-feed-core-slice`
  (DEC-FEED-RUST-CORE-001, MIGRATION.md #1797 — the engine read-boundary
  DECISION kernel in Rust: NEW crate `rust/brutex-feed` + the leaf
  `brutex/feed_rust.py` behind the default-OFF `BRUTEX_FEED_RUST`;
  integers-only FFI, Python keeps all materialization — `bxsnap2` run
  identity byte-preserved by construction; blanket degrade to the retained
  Python oracle; ships INERT (flag OFF = byte-identical today); ENGINE
  results UNCHANGED — NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch
  line — see §1 + MIGRATION.md #1797 for the full blurb)
  Earlier: `claude/results-cascade-verdict-join`
  (DEC-RESULTS-CASCADE-VERDICT-JOIN-001, MIGRATION.md #1749 — the Stage-4
  cascade overfitting verdicts + per-candidate CPCV pass rate + the ranking
  composite join the display surfaces: the ONE `engine/cascade_join.py` law
  feeds the top100 writer (in-memory report, HARD sweep_id gate, per-entry
  `cascade` object + `metrics.cpcv_pass_pct` only-when-measured, root
  `cascade_join` meta, ENTRY_REQUIRED_KEYS not extended) AND the /results
  read-time loader (dedicated `cascade_report_revision` stat token, 16 MiB
  cap, single-entry memo, EMIT-ON-PRESENT row keys + meta — a no-report
  lake's payload is byte-identical, pinned); a cascade-FAILED strategy is
  red-flagged on every screen, ranked never hidden; NEW
  `dashboard/composite_join.py` activates ?preset=composite honestly off
  the newest run's aggs sidecars (the read-time half of HIGH #40); nothing
  persisted on ResultRow — DEC-CASCADE-ROW-PERSIST-002 is the named
  follow-up; READ-side + artifact-level; ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line)
  Earlier: `claude/tradesim-open-session-guard`
  (DEC-TRADESIM-OPEN-SESSION-GUARD-001, MIGRATION.md #1743 — the trade-sim
  OPEN-position session-boundary guard: an open position never survives an IST
  session-date change — closed retroactively at the PRIOR session's last
  stored bar (the finish() idiom) under the NEW append-only `session_gap`
  exit reason; a pending next-open exit is subsumed and the boundary bar never
  arms; Python walker + Rust kernel (u64+u128) in ONE change with zero new FFI
  inputs; the frozen sweep-run goldens MEASURED unchanged (1661 golden trades,
  zero cross-session — run identity untouched); the `exits_session_gap`
  counter + the capped `trade_sim.session_tail_hole` WARN are the signals;
  ENGINE semantics corrected on hole-shaped feeds only — healthy contiguous
  feeds byte-identical, NIFTY/BANKNIFTY/SENSEX (+ NSE-NIFTY-FUT); matches §1's
  Active branch line — see §1 + MIGRATION.md #1743 for the full blurb)
  Earlier: `claude/greeks-rust-batch`
  (DEC-GREEKS-RUST-BATCH-001, MIGRATION.md #1892 — renumbered from the
  provisional #1724 at the club-R2 fold, then #1795 → #1892 at the club
  CDR2 fold — the additive
  `trading_core.greeks_batch` columnar face of the frozen Greeks kernel + the
  default-OFF `BRUTEX_GREEKS_RUST_BATCH` batch-solve seam in
  `enrich_option_bars` (shared-single-copy screens, 16-element burn-in +
  autorevert latch, chunk cap 65,536); OFF arm byte- and log-identical, the
  existing enrich golden byte-identical with the flag ON; ON/OFF wall
  ≈1.0× measured (no speedup claimed; 1.5× no-regression ceiling only); crate stays 0.3.1; ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line — see §1 +
  MIGRATION.md #1892 for the full blurb)
  Earlier: `claude/rust-datalake-io-slice8`
  (DEC-RUST-DATALAKE-IO-ROADMAP-001, MIGRATION.md #1891 — slice 8 of the
  Rust-first conversion program, the data-lake I/O layer: the panel's
  keep-Python verdict OPERATOR-OVERRIDDEN to Rust-everywhere; the
  byte-identity-gated conversion roadmap is adopted as ADR-0020, and
  **PR-1 is SHIPPED on this branch** — the `parquet_writer` render seam
  re-hosted on the NEW `rust/brutex-lake` crate (polars REV-pinned
  `0df0c25d…` = the commit `py-1.42.1` pointed at, zstd trio via the
  committed Cargo.lock), default-OFF
  behind `BRUTEX_LAKE_RUST_RENDER` with import/version guards + the
  16-render burn-in autorevert (schema-shape-gated since the review
  round), Python as the live parity oracle; the byte-parity corpus (the
  spike's 5 cases + the review-round all-null-overlay case) +
  whole-write-path parity EXECUTED locally
  (all sha256+bytes equal); NEW non-required lake-rust-parity.yml lane
  (wheel force-aligned to the crate pin, cargo `--locked`, rev-pinned
  polars deps) + the lockstep runbook (`window_ledger_store` stays the
  LAST-MILE named escalation point); the 5-lens ADVERSARIAL-REVIEW FIX
  ROUND is folded pre-merge — per-item record in the MIGRATION.md #1891
  PR-1 segment; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX;
  see §1 Active branch for the full blurb)
  Earlier: `integration/t1-sweep-core`
  (T1 SWEEP-CORE integration train — folds #1330 DEC-SWEEP-UNKILLABLE-CORE-001 (#1712) +
  #1332 DEC-SWEEP-BLOOM-AUTOGROW-HEADROOM-001 (#1714) + #1336
  DEC-SWEEP-TUPLE-DIGEST-GATE-IDENTITY-001 (#1715) + #1350
  DEC-SWEEP-RESUME-STORE-GUARD-001 (#1716) + the #1351 tests+docs rider; see §1 Active
  branch for the full blurb)
  Earlier: `claude/dedup-c1-tuple-digest-identity`
  (DEC-SWEEP-TUPLE-DIGEST-GATE-IDENTITY-001, MIGRATION.md #1715 (renumbered #1622 → #1632 → #1633 → #1715 across the T1 train folds) — the residual
  C1/C2 stale-serve fix: fold the ACTIVE gate config + non-default SELECTION
  knobs (`top_k`/`min_hits`/`max_combos`) into the per-tuple store ADDRESS
  (`_tuple_digest`) via a NEW COMMON `sweep_identity.py` leaf + a supervisor
  re-export shim, DEFAULT-ELIDED so an ungated default-selection sweep's digest
  is byte-stable (`5b020e892247deac`); the #1330 `grid_identity`/`tuple_cache_key`
  pins + the #1531 golden pair + the 400-case vocab golden pass UNMODIFIED; C5
  defense-in-depth `ResultsStoreGateConflictError` tuple-scoped skip; ENGINE
  UNCHANGED — NIFTY/BANKNIFTY/SENSEX)
  Earlier: `claude/sweep-unkillable-core`
  (DEC-SWEEP-UNKILLABLE-CORE-001, MIGRATION.md #1712 — the UNKILLABLE sweep
  run core, Phase 2 of the one-click sweep program; see §1 Active branch for
  the full blurb)
  Earlier: `claude/frontend-latency-hardening-r2`
  (DEC-FRONTEND-LATENCY-HARDENING-001, MIGRATION.md #1720 — the dashboard
  FRONTEND latency-hardening pack: v29 duplicate pollers consolidated onto ONE
  BXPoll-disciplined consumer per endpoint (measured BEFORE: 336 req/120 s idle
  with sweep-progress/pull-rate/liveproof double-polled → measured AFTER 240
  req/120 s, −28.6%, same harness), six template pollers
  migrated onto the shared bx-poll.js core (renders byte-identical), /screener +
  /results inputs debounced 200 ms, in-place tbody row patches on
  completeness/spot_coverage, /reports chips build-once + class-toggle
  selection; the bx-format/bx-movers-time defer + the universe catch-up were
  SKIPPED honestly (parse-time consumers + standing pin; already shipped by
  #1623); CLIENT-side only — byte-stable wire; ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX)
  Earlier: `claude/db-latency-hardening`
  (DEC-DB-LATENCY-HARDENING-001, MIGRATION.md #1719 — the DB/query-latency
  hardening pack, READ-side only: the /db window-ledger DuckDB page fast path
  (7.3–8.8 s cold at 400k rows → 0.56 s, zero Python parses), the pre-open
  export rows memo (repeat click 206 DuckDB statements → 0, bytes identical)
  + span-bounded IEP reads, and the /db candles %CHNG baseline file bound
  (72 files bound twice per click → 2); healthy-lake payloads byte-identical;
  ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX;
  see §1 + MIGRATION.md #1719 for the full blurb)
  Earlier: `claude/movers-bucket-asof-fix`
  (DEC-MOVERS-EXPLICIT-BUCKET-ASOF-001, MIGRATION.md #1601 — an explicit `/movers`
  bucket-anchor TIME pick resolves to the COMPLETED bucket (broker-matching close)
  via the ONE COMMON `movers_snapshot.resolve_asof_cutoff` seam shared by all three
  tabs; READ-ONLY dashboard, ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX)
  Earlier: `claude/movers-futures-hyphen-parse`
  (DEC-MOVERS-FUTURES-HYPHEN-PARSE-001, MIGRATION.md #1646 — the `/movers`
  Futures contract-symbol parse right-anchored by DELEGATING to the canonical
  `data/fno_symbol.parse_fno_symbol`; hyphenated underlyings keep their full
  name so spot + premium resolve; + the W3 fold-in: the movers spot probe
  (futures premium + options moneyness) walks the #1482 spot-drift recovery
  ladder on an exact-key miss via the per-compute
  `movers_futures._SpotLtpResolver`; READ-ONLY dashboard fix; ENGINE
  UNCHANGED — NIFTY/BANKNIFTY/SENSEX; see §1 Active branch for the full
  blurb)
  Earlier: `claude/groww-market-hours-guard`
  (DEC-GROWW-MARKET-HOURS-GUARD-001, MIGRATION.md #1692 — renumbered #1633 → #1673 → #1692 (the reserved-band collision, #1633–#1672 held by main's cascade; then the coordinator reservation map — #1673 reserved for PR #1369); the NSE
  market-hours pull guard: no Groww VENDOR pulls 08:45–15:35 IST on trading
  days, keeping the shared account free for TickVault's live feed)
  Earlier: `claude/magical-cray-g6agaw`
  (DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001 + DEC-FUTURES-ADDRESSING-PIN-001 +
  DEC-STAGE1A-ROLL-RESET-001, MIGRATION.md #1700–#1710 — the ADR-0012
  NIFTY-futures sweep LAUNCH capstone; see §1 Active branch for the full blurb)
  Earlier: `claude/oneclick-perf-club`
  (DEC-ONECLICK-PERF-CLUB-001, MIGRATION.md #1711 — coordinator-assigned from
  the reservation map (next free above the #1264 reservation tail #1710;
  live-main max #1665 at assignment — gaps are legal; renumbered from the
  interim provisional #1666) — the queued post-#1373 oneclick
  perf/hygiene club, built
  on PR #1373's head 3c478831 (the PR opens after #1373 lands): F1 the
  /api/v1/oneclick/verify revision cache (the ONE uncached oneclick route;
  key folds manifest + events + top100 tokens + the chain verified-state;
  NEW B-7 bench pin measured 0.95× flat) · C-r2-5 the stat-outage
  serve-last-known doctrine on ALL FIVE cached oneclick routes
  (`artifact_probe` absent≠outage + `OutageGate` + the ONE
  `serve_revision_cached` chokepoint; one WARN per outage per coordinate,
  recovery INFO,
  never a silent per-poll re-parse) · F4 `discover_runs` stat-sorts first and
  parses only the newest-limit manifests (identical selection; 50-of-60
  parse-spy pin) · A-r4 the /screener client-side bounds (Show all capped at
  1000, input cap 5000, node-goldened cap/note math — every truncation NAMED)
  · F2b deliberately SKIPPED as optional polish; DISPLAY zone only; ENGINE
  UNCHANGED — NIFTY/BANKNIFTY/SENSEX; see §1 Active branch for the full
  blurb)
  Earlier: `claude/oneclick-instrument-display`
  (DEC-ONECLICK-WINNER-DETAIL-TRUTH-001 + DEC-ONECLICK-INSTRUMENT-DISPLAY-001,
  MIGRATION.md #1664–#1665, renumbered #1632/#1633→#1633/#1634→#1664/#1665 —
  #1633–#1636 were #1323's reserved block, #1664/#1665 coordinator-assigned
  from the in-flight reservation map — the strategy-detail
  honesty pack,
  viewer-reads-storage only: the winner's `instrument` on every one-click
  surface (ONE `bxInstrumentText` builder; combos Inst cell + `fInst`
  engine-token repoint; /screener "Inst" column + filter); the gross
  signal-P&L relabel + persistent signal-only chip on the /strategy card AND
  the operator hero; the results-depth aggregates (expectancy · recovery ·
  streaks · MAE/MFE · minute-path DD · exit mix · worst-5-days) + the regime
  breakdown rendered from the stored artifact (the stale "lands with P3"
  deferral DELETED); NEW `GET /api/v1/oneclick/equity` over the equity_ref
  sidecar + the /strategy minute-path equity render with the honest
  per-trade fallback (the operator host stays per-trade — the #48
  follow-up); the `/strategy?run_id=&rank=` link contract pinned; nav gains
  /completeness · /screener · /strategy; the two chart-mapper NITs
  (no `Number(null)→0` candles; skipped rows counted + surfaced); HIGH #40
  composite_score /results join DEFERRED (cross-session seam); built on PR
  #1352's head, PR opens after #1352 lands; DISPLAY zone + one additive
  display route; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; see §1 Active
  branch for the full blurb)
  Earlier: `claude/preopen-prevday-common`
  (DEC-PREOPEN-PREVDAY-COMMON-002, MIGRATION.md #1649a — renamed from -001 (main's #1634/PR #1323 claimed -001 for the minute-basis cross-check ADVANCE; -002 = the COMMON-ization remainder + observability + the #1649b Muhurat pack); renumbered
  #1623→#1631→#1649a across the 2026-07-09 merge-reconciles (main took #1623
  for DEC-DASHBOARD-POLL-HYGIENE-001, then #1631/#1632 for
  DEC-ONECLICK-TOP100-OBJECT-ROOT-001 + DEC-DASHBOARD-VISUAL-OVERHAUL-001;
  both-suffixed pair per the 613a/613b precedent); the /reports preopen
  minute-basis calendar-contiguity fix (a real-session lake hole no longer
  fabricates a %CHNG — correct-or-absent) + the #1539-named follow-up CLOSED:
  `_previous_trading_day` delegates to the COMMON
  `nse.previous_trading_session`, which gains the remove-only `observed`
  overlay kwarg; PLUS the same-branch amendment fold
  DEC-PREVSESSION-MUHURAT-COHERENCE-001, MIGRATION.md #1649b — the two-law
  day-after-Diwali coherence ruling: /movers stops false-flagging a healthy
  Muhurat-boundary lake as a lake hole (contiguous-True + EMIT-ON-TRUE
  `prev_close_muhurat`/`oi_baseline_muhurat` + " †" + one INFO, never
  " *"/WARN), preopen names the boundary definitively (`muhurat_session_bar`;
  `extra_nonsession_bar` reworded honestly suspicious), /db gains the static
  %CHNG anchor footnote; READ-ONLY dashboard; ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; see §1 Active branch for the full blurb)
  Earlier: `claude/cool-cerf-tq8c27`
  (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001, MIGRATION.md #1594 — the options-tree
  foreign-month + overlap heal; see §1 Active-branch chain for the full blurb)
  Earlier: `claude/movers-underlying-class`
  (DEC-MOVERS-UNDERLYING-CLASS-001, MIGRATION.md #1650 — renumber history
  #1631→#1632→#1633→#1650 (PRs #1352/#1354 took the first two; the slot wave
  reserved #1633–#1649); the `/movers` page gains
  an `All | Indices | Stocks` underlying-class filter on all THREE tabs, a
  rank-time filter over the SHARED cached candidate set (the #1034 min_volume
  template); SPOT class from the lake segment, FUTURES/OPTIONS from the six-step
  pull order's `primary_fno_underlying` + `is_index` map (#1527, reused); default
  `All` byte-identical; ADVERSARIAL-REVIEW FIX ROUND folded — normalize-at-lookup
  for hyphenated/aliased F&O underlyings, right-anchored `parse_future_symbol`,
  `master_revision`-keyed kind-map + F&O candidate memos (warmer included), the
  CI pyright fix; DISPLAY zone only; ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX)
  Earlier: `claude/oneclick-top100-object-root`
  (DEC-ONECLICK-TOP100-OBJECT-ROOT-001, MIGRATION.md #1631 — the one-click
  `top100.json` reader-adopts-writer blocker fix: object root + the ONE
  reader-boundary metric-key map + writer→reader golden + fixture corpus
  regenerated from the writer shape; folds [11] sort-basis labels,
  [W2-4] null-cost gross-equity honesty, [W2-5] screener render-cap count,
  [12] runs-listing manifest-token cache key, [W3-R1] the /strategy candle
  chart timestamp-key + TZ-safe arrow placement, [W3-R4] honest null
  VIX-band copy on all three band sites ([W3-R3] arm repro verified dead);
  DISPLAY zone only; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX;
  see §1 Active branch for the full blurb)
  Earlier: `claude/dazzling-pasteur-jf8s3w`
  (DEC-LAKE-STATS-MANIFEST-001, MIGRATION.md #1614 — the write-time
  PER-FILE lake STATS manifest; matches §1's Earlier chain)
  Earlier: `claude/sweep-log-level-guard`
  (DEC-SWEEP-LOG-VOLUME-GUARD-001, MIGRATION.md #1630 — the sweep CLI
  log-volume guard: INFO default + fail-loud `BRUTEX_LOG_LEVEL` override at the
  `main()` sweep dispatch AND inside every spawn worker
  (`sweep_parallel._worker_init`, R4 fold); audit JSONL hash chain untouched;
  ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX)
  Earlier: `claude/oneclick-sweep-runs-rebind`
  (DEC-ONECLICK-SWEEP-RUNS-REBIND-001, MIGRATION.md #1621 — the P4
  merge-integration rebind: oneclick discovery repointed to the Ruling-4
  `sweep_runs/` sibling root + the production `TradeStoreReader` over the
  #1275 results-depth sidecars; DISPLAY zone only; ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; see §1 Active branch for the full blurb)
  Earlier: `claude/peaceful-newton-fehcc3`
  (DEC-GREEKS-BULK-REENRICH-001, MIGRATION.md #1558 — the bulk Greeks
  v1→v2 re-enrichment backfill: expired contracts + CLI-only lakes
  re-stamped to the current formula/inputs; ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX)
  Earlier: `claude/operator-page-real-rebuild`
  (DEC-OPERATOR-PAGE-REAL-001, MIGRATION.md #1561 — the Operator page (/)
  wired to the sweep-oneclick artifact contracts + NEW /screener + /strategy;
  DISPLAY zone only; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX)
  Earlier: `claude/sweep-stall-oom-guards`
  (DEC-SWEEP-STALL-OOM-GUARDS-001, MIGRATION.md #1608 — the parallel grid-sweep
  gains a heavy-grid-aware stall watchdog + a distinct `BrokenProcessPool`
  quarantine; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX)
  Earlier: `claude/intelligent-hamilton-qf3w38`
  (DEC-SWEEP-NIGHTLY-AUTOMATION-001, MIGRATION.md #1612 — the R9 nightly
  zero-click chain + R50 backup/restore + launchd scheduling; ENGINE
  UNCHANGED — NIFTY/BANKNIFTY/SENSEX; see §1 Active-branch chain for the
  full blurb)
  Earlier: `claude/elegant-johnson-cgufi7`
  (DEC-SWEEP-RESULTS-DEPTH-001, MIGRATION.md #1617 — renumbered
  #1538→#1557→#1581→#1598→#1610→#1617; the final hop past main's #1616
  (DEC-SWEEP-DATA-EDGE-GUARDS-001) on merge round 5, 2026-07-08)
  Earlier: `claude/data-edge-guards`
  (DEC-SWEEP-DATA-EDGE-GUARDS-001, MIGRATION.md #1616 — two surgical engine
  data-edge guards for sweep-result correctness: (1) a Muhurat-safe prev-day
  anchor at the Stage-1A roll seam + (2) a bounded bad-tick screen at the engine
  read boundary (`arrow_table_to_bars`); ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX;
  see §1 Active branch for the full blurb)
  Earlier: `claude/peaceful-hamilton-kyeu07`
  (DEC-VIX-STAMP-WIRE-001, MIGRATION.md #1645 — India-VIX trade stamping wired
  end-to-end: `vix_at_entry`/`vix_at_exit` on every OBSERVABLE trade + the
  persisted nullable `vix_mean`/`vix_min`/`vix_max` finalist trio, REFERENCE
  ONLY; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX)
  Earlier: `claude/jolly-tesla-miygwt`
  (DEC-MUTATION-RUNTIME-003, MIGRATION.md #1597 — the weekly deep mutation
  gate made green end-to-end: sharded cargo-mutants matrices + zero-survivor
  python mutmut loop; see §1 Active branch for the full blurb)
  Earlier: `claude/serene-euler-y5ra0z`
  (DEC-BACKUP-OFFBOX-001, MIGRATION.md #1596 — automated incremental off-box
  backup: restic → S3 Object-Lock / B2 immutable, NEW `brutex/backup/` package
  + `brutex backup {run,check,restore-drill,status}` CLI + the O(1) dead-man
  freshness check + the monthly byte-verified restore drill; ops tooling ONLY —
  ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; see §1 Active-branch chain for
  the full blurb)
  Earlier: `claude/blissful-curie-gp94rk`
  (the 11-item POLISH PACK, MIGRATION.md #1582–#1592; see §1 Active-branch
  chain for the full blurb)
  Earlier: `claude/upbeat-tesla-mfrovq`
  (DEC-RATE-METER-LIFECYCLE-001, MIGRATION.md #1593 — the #1056 pull-rate
  CallMeter lifecycle + rate-widget truth pack; see §1 Active-branch chain for
  the full blurb)
  Earlier: `claude/sleepy-hawking-l87497`
  (DEC-FORM-WINNER-PERSIST-001, MIGRATION.md #1581 — the durable `&`-form
  winner map persisted via `schema_meta` + the `--ampersand-probe` doctor
  stage; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX)
  Earlier: `claude/peaceful-wright-r7l8l9`
  (DEC-FIB-BULLISH-RETRACE-001, MIGRATION.md #1560 — the bullish Fibonacci
  retracement fix: PDL-anchored `retrace_bull_{23,38,62,78}_paisa` kernel
  fields replace the wrong bearish alias; ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX)
  Earlier: `claude/stoic-davinci-7c7xp0`
  (DEC-TRACK2-COSTS-SPEC-RECONCILE-001 + DEC-BROKER-DOCS-AWSONLY-RECONCILE-001 +
  DEC-GPU-GATE-RECONCILE-001 + DEC-CLOUD-AUTOFIX-RETIRE-001 +
  DEC-PHASE1-SIGNOFF-RECONCILE-001, MIGRATION.md #1552–#1556 — the doc/spec/ops
  debt reconciliation pack; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX)
  Earlier: `claude/zen-meitner-8780fo`
  (DEC-RUN-IDENTITY-GUARD-PARITY-001 + DEC-OPTION-DEDUP-BLOOM-VERIFY-PIN-001 +
  DEC-INDICATOR-TWINS-RUST-SINGLE-SOURCE-001 + DEC-TRADE-SIM-U128-KERNEL-001 +
  DEC-EXECUTION-TRIO-DELETE-001, MIGRATION.md #1546–#1550)
  Earlier: `claude/ecstatic-cannon-o3zbbe` (DEC-TEST-APPARATUS-DEBTS-001,
  MIGRATION.md #1541 — the test-apparatus debt pack: nine long-ledgered
  apparatus gaps closed as ONE change set; apparatus/CI/docs only; see §1
  Active branch chain for the full blurb)
  Earlier: `claude/eloquent-shannon-bdjnuv` (carrying BOTH movers fixes)
  (DEC-MOVERS-PREVCLOSE-CONTIGUITY-001, MIGRATION.md #1539 — the `/movers`
  prev-close/OI baselines gain an honest calendar-contiguity verdict: a
  baseline anchored on a non-adjacent NSE session is stamped
  (`prev_close_*`/`oi_baseline_*` EMIT-ON-FALSE keys), warned once (capped),
  and marked " *" in the UI — the served number itself unchanged; READ-ONLY
  dashboard, ENGINE UNCHANGED NIFTY/BANKNIFTY/SENSEX)
  Earlier (same branch):
  (DEC-MOVERS-SPOT-VALUE-TF-INVARIANT-001, MIGRATION.md #1538 — the `/movers`
  SPOT "Top Value" (turnover) is TIMEFRAME-INVARIANT: re-sourced from the RAW
  1-minute session frame (post as-of cut, PRE-resample) via the NEW
  `spot_session_value_paisa`, mirroring the already-TF-invariant
  futures/options session value; READ-ONLY dashboard ranking, ENGINE UNCHANGED
  NIFTY/BANKNIFTY/SENSEX)
  Earlier: `claude/intelligent-euler-dwf2ab`
  (DEC-CLI-GREEKS-ENRICH-001, MIGRATION.md #1536 — the CLI `brutex fetch`
  pull path now runs the SAME fenced Greeks-enrichment pass the dashboard
  runs + the NEW `brutex enrich-greeks` bulk re-enrich; see §1 Active
  branch chain for the full blurb)
  Earlier: `claude/movers-asof-default-latest-minute`
  (DEC-MOVERS-ASOF-LATEST-DEFAULT-001, MIGRATION.md #1535 — the `/movers` Spot
  default as-of TIME resolves to the SAME latest available minute across EVERY
  timeframe, so 1m ↔ 2m ↔ 3m ↔ 3hour ↔ 1day always show the identical latest
  close; READ-ONLY dashboard, ENGINE UNCHANGED NIFTY/BANKNIFTY/SENSEX)
  Earlier: `claude/happy-allen-y29nc6` (DEC-BENCH-ABS-BASELINE-001, MIGRATION.md
  #1534), `claude/peaceful-planck-dnsjg9` (DEC-LEDGER-DELTA-O-PAGE-001,
  MIGRATION.md #1533) and `claude/amazing-darwin-hb4caw`
  (DEC-SWEEP-EXIT-INDEX-FASTPATH-001 + DEC-SWEEP-RUST-METRICS-DEFAULT-ON-001 /
  ADR-0017, MIGRATION.md #1531–#1532)
- **Never push to `main`** without explicit user instruction.
- **Commit style:** Conventional Commits.
  - `docs(track1): add 52-condition vocabulary appendix`
  - `feat(adapter/groww): implement intraday OHLC chunking`
  - `fix(engine): O(1) regression in bar lookup`
  - `test(property): add DSR effective-N invariant`
  - `chore(claude): update CLAUDE.md rules`
- **Never skip hooks** (`--no-verify` is forbidden unless the user
  explicitly requests).
- **Never force-push** shared branches.
- **Never amend** published commits; always create new commits.

---

