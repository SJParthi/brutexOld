# Verifier B external findings — MUST be folded into SPEC_RUST_SWEEP_CORE.md + DESIGN_FINAL_RUST_SWEEP_CORE.md before checkpoint 1

Source: Verifier B independent grounding (103 invariants, 46-case red-team battery), full report at /tmp/claude-0/-home-user-bruteX/e53740a1-2852-58f2-a67d-b1825ba930a9/scratchpad/verifier-b/00-FINDINGS-REPORT.md (+ 00-INVARIANT-MODEL.md). Cite each F-id where folded.

## HIGH — bit-identity / ship-condition risks
- F3: Python `//` FLOORS, Rust `/` TRUNCATES — 1-paisa divergence in expectancy/avg_* on ~half of losing combos (verified −7//2=−4 vs −3), compute.py:665-673. Fold contract must name div_euclid + add a negative-numerator golden cell.
- F5: u128 mask serialized as BARE JSON int in combo_digest + WF hash — serde_json u64 cap aliases m and m+2^64 → silent false-skip. Dormant at 60 bits; serializer law must close it (no bare JSON ints past u64; explicit encoding).
- F1: checkpoint is POSITIONAL (index); grid_identity pins axes, not enumeration algorithm — a Rust reorder silently prefix-skips never-run tuples (sweep_supervisor.py:700). Need index→tuple-digest golden pin OR identity/version token. CNS-unranked enumerator makes this MANDATORY.
- F2: non-bit-identical recompute makes crash-REDO a permanent grid-wide append-conflict abort (ResultsStoreError ≠ SweepComboError). Twin-run proof MUST include a kill-after-flush REDO cell (RT-09), not just clean twins.
- F4: pyo3 PanicException is BaseException — bypasses `except Exception` AND #1291's quarantine taxonomy (deterministic-panic resume livelock; abort alert bypassed). Spec §error-taxonomy must pick a panic policy: catch at FFI seam, map into quarantine taxonomy, explicit panic-class alert obligation.
- F23 (addendum to F4): the mapped panic class must ALSO restore per-tuple-skip parity on the SUPERVISED-DAEMON path — `brutex sweep-supervisor` relaunches on ANY nonzero exit, so a deterministic-panic tuple becomes a relaunch crash-loop → GAVE_UP with every later tuple permanently uncomputed (vs Python's one-launch per-tuple skip). If mapped to SweepComboError→TUPLE_FAILED/exit 6, the supervisor must treat a per-tuple-skip exit distinctly.
- F27 / B-INV-102 (addendum to F1): FOURTH skip authority — `run_one_tuple` returns None for an empty bar-window and ADVANCES the positional checkpoint with NO cache/sentinel/artifact. An FFI marshalling defect yielding an empty window = clean-exit silent false-skip. Data-plane section must assert: an empty MARSHALLED window reproduces skip-not-error at the SAME tuple, and a non-empty Python-side window can never arrive empty across the FFI (length invariant checked at the seam).

## Structural laws to name as frozen inputs
- F21: one-preload / hash-what-you-consume — cmd_sweep_all preloads each slice ONCE, hashes THAT dict, serves the sweep from it. Rust core owns ZERO lake I/O; bars cross the FFI as DATA from the one preload. (Bypass gate is a Python-spelling grep, blind to a Rust reader — so this is a LAW, not a gate.)
- F22: paisa-MINTING seam — `_ema_to_paisa` routes float→Decimal(str(v))→HALF_UP (repr-mediated, NOT (v*100).round()). The 400-case answer-key inputs are already-snapped paisa ints (seam is answer-key-INVISIBLE). Port by name.
- F15/F18: evaluation stays on the ONE shared trading-core crate (never fork; FIB_LEVELS_LEN=15 ABI frozen); NSE calendar (Muhurat/#1616 prev-day anchor) is a frozen INPUT; Stage-1B min-hits kernel needs count_hits ≡ count_supermasks bit-parity at the strict >= boundary (property gate).

## Routed to coordinator (pending rulings — pin design to whatever is ruled)
- F7: #1295 vs #1291 both define BRUTEX_SWEEP_STALL_SECONDS on the same file with OPPOSITE semantics (fail-loud vs fail-soft) + different scaling laws. Ruling requested.
- F8: #1295's peak-relative throughput tripwire × ADR-0017 autorevert → byte-correct runs branded SUSPECT_COMPLETE forever. Design states required interaction (arm-aware baseline / autorevert-informs-tripwire hook).

## Verifier B is also freezing baseline-truth corpora (evidence pack incoming)
float-text corpus, u128-mask JSON bytes, paisa-minting tie cases, Muhurat weekly-refusal exception transcript, min-hits boundary keep/drop sets, Python↔Python twin-run run_id/hash baseline — the Rust twin's byte-exact oracle.

## UPDATE — Verifier B BASELINE-TRUTH PACK (2026-07-08)
Pack: Verifier B scratchpad, verifier-b/redteam/BASELINE-TRUTH-PACK.md (session e53740a1-2852-58f2-a67d-b1825ba930a9). Built with real wheels trading_core-0.3.1 / fno_math-0.3.0; repo untouched. Dispositions updated:
- HONEST FRAMING: 0 live cross-language divergences exist today (no Rust twin yet) — F5/F10/F14/F22 are PORT BREAK-ORACLES (frozen Python goldens the Rust core must reproduce), not live bugs.
- RT-40 / E-18 ESCALATED to LIVE MAJOR pre-existing defect (was A's MINOR): fib_anchor ∈ {rolling_weekly, weekly_static} on a Muhurat/non-trading 'today' raises plain ValueError at weekly_anchors.py:125 (static twin :195); stage1a.py has 0 except clauses; propagates past the per-tuple `except SweepComboError` (sweep_parallel.py:595 — NOT supervisor.py:750) → grid-wide SUPERVISOR_ABORT exit 2, zero rows, calendar-deterministic standing wedge; daemon → crash-loop → GAVE_UP. Captured 2024-11-01 transcript. Honest gap: exit-2 composition code-confirmed, not run end-to-end. FIX in engine regardless of conversion; until fixed, the spec's calendar-input contract must pin the abort SHAPE as parity truth (grid-abort/exit-2, never TUPLE_FAILED, never persisted rows).
- RT-01 / F3 DOWNGRADED MAJOR→MINOR (dormant): floor-vs-trunc fork real (-7//2=-4 vs -3) but expectancy_paisa is not in blake3 run identity and is Python-single-sourced (trading-core already uses div_euclid). Still name div_euclid in the fold contract.
- NEW ORACLE (i): dup-ts last-wins — build_ts_index reads the LATER close on duplicate timestamps; ZERO golden coverage today; a Rust arm keeping the earlier index passes every existing golden yet diverges. RT-37 fixture ready — must become a golden in the conversion test plan.
- Frozen corpora for the twin (9): (a) Python↔Python twin run_id f588194f… == _GOLDEN_RUN_ID, store_content_hash 2387cc88… == _GOLDEN_CONTENT_HASH, byte-identical Parquet; (b) RT-09 crash-redo clean resume, audit chain [SUPERVISOR_START, TUPLE_SKIPPED_RESUME, TUPLE_DONE, SUPERVISOR_COMPLETE]; (c) empirical write order flush→sidecar→sentinel→cache.record→checkpoint.record→audit; (d) float-text corpus (repr(1e-5)='1e-05'; -0.0 NORMALIZED to +0.0 — combo.py docstring :66-68 stale, code :325-331 wins); (e) u128 masks: spec.rs:219 hex-encodes as 32-nibble STRING — keep, never bare JSON ints; (f) paisa-mint ties: v=1.005 → seam 101 ≠ round(v*100)=100 — anchor Decimal(str(v)) HALF_UP; (g) 400-case answer-key 400/400 bit-exact on current wheel; (h) min-hits strict->= keep/drop + count_hits≡count_supermasks (u64+wide); (i) dup-ts last-wins fixture.

> **WITHDRAWN — DAY-COUNT CLAIMS (Verifier A round-1 fold, 2026-07-09).** The
> absolute day-count figures in the two sections below — the "k≤6 ≈5.9d →
> ~2–3.5d" pair (under the "BINDING numbers" header) and the earlier
> "k≤6 ÷1.7–2.2 ≈ ~2–8 M4-days" scenario arithmetic — are **WITHDRAWN**: their
> "today" anchors trace solely to combo_counts.md §3's
> 0.58 ms(per-KEPT)×enumerated-count arithmetic, the exact per-KEPT ×
> per-ENUMERATED unit conflation DESIGN_FINAL §9 withdraws, and
> perf_reconcile/RECONCILIATION.md derives ratios only (no day count exists in
> the pinned evidence). Despite the "BINDING" header below, ONLY the RATIO band
> is binding — see DESIGN_FINAL §9/§9.1 (campaign band ≈1.3–3.1× from the port
> alone; ≈2.3–2.5× at FIX-L k=2 median density); absolute k≤6 day figures are
> DEFERRED to the §9-mandated on-branch sub-grid measurement + the
> combo_counts.md §3 rebuild. The two sections are retained verbatim below as
> the historical external-findings record.

## PERF RECONCILIATION (MANDATORY before DESIGN_FINAL speedup tables freeze) — guarantee-session correction, 2026-07-09
The guarantee session corrected its own projection (a ~2× metrics-tail double-count): at heavy 1m/604k-bar tuples the Rust kernels ALREADY dominate (~12.2–12.6 ms of ~21.8 ms inclusive; Python residual ~42–44%) → orchestration port wins only ~1.7–1.8× there, ~2.7×+ on small-bar TFs; CAMPAIGN-LEVEL EXPECTED ~1.7–2.2×; post-port FLOOR = trade-walk kernel (~7.7 ms/mask @604k = 12.8 ns/mask·bar dense). DISCREPANCY vs this track's own grounding: baseline measured walk ≈1.28 ns/mask·bar (62 µs @48.6k) and 1m walk-bound floor ≈0.7 ms/kept-mask @555k (≈1.26 ns/mask·bar) — 10× apart on near-identical bar counts. RESOLVE with a side-by-side reproducer on identical fixtures (request their artifact paths: kernel entry point timed, mask density, per-KEPT vs per-enumerated, drawdown included?, TF mix, wheel version). Until resolved, campaign speedup is honestly UNKNOWN in [~1.7×, ~10×+] and every table must say so.
BINDING design consequences regardless of reconciliation outcome: (1) FFI port alone does NOT make k=6 overnight under the corrected numbers (k≤6 ÷1.7–2.2 ≈ ~2–8 M4-days) — tables must show both scenarios until reconciled; (2) ALGORITHMIC levers (entry-sparsity exploitation, survivor-aware batching, per-TF work shaping) are FIRST-CLASS design space, not afterthoughts; (3) S2 acceptance gate for the port: inclusive ≤ ~1.15× of kernel sum (~14.0–14.5 ms @604k — COUNTERPARTY-RELAYED [Verifier A round-2 tag]).

## PERF RECONCILIATION — RESOLVED (2026-07-09). BINDING numbers for DESIGN_FINAL tables
Two-term law CONFIRMED by both sides independently (full evidence: perf_reconcile/RECONCILIATION.md in this scratchpad): cost(mask) = c0 + walk ≈6.2 ns/mask·bar (RAYON_NUM_THREADS=1 pinned — the production config, sweep_parallel.py:558) + ≈4.1 µs/trade (prod arm; 0.35 µs/trade raw-kernel arm), R²≥0.993. The earlier 1.26–1.28 ns "walk floor" in baseline.md was UNPINNED + count-only — must be corrected with qualifiers wherever quoted; never use it in campaign math. Unpinned/pinned rayon ratio ≈3.0–3.8× (bar-count dependent).
[Capture base (Verifier A round-2): origin/main 665181f3 — sweep_parallel.py:558 at capture; the pin lives at :842 as of 95955a71; semantics unchanged.]
CAMPAIGN BAND from the FFI port alone: ≈1.3× (sparse @604k bars) to ≈3.1× (short windows); ≈2.3–2.5× at FIX-L k=2 median density (1,532 trades/mask, 7.7% zero-trade). k≤6 ≈5.9d → ~2–3.5d from the port alone. Day-level k≤6 requires the algorithmic levers — first-class in the design: parallelism topology (8×1 workers×rayon today; in-kernel rayon worth ≈3.0–3.77× on the walk for heavy tuples per this track's OWN committed data — 3.77 @48.6k · 3.69 @93.9k · 3.51 @300k · 3.00 @604k bars, fit_results_pinned.json; the ~3.97× endpoint is COUNTERPARTY-RELAYED, confirmed only "within shared-vCPU noise" — Verifier A round-1 attribution fix of the former "worth ~3.0–3.97×"; reshaping topology per-tuple is a measured lever), entry-sparsity exploitation, kept-vs-enumerated shaping on coarse TFs (1day k=3 keeps 46.4%).
ACCEPTANCE CRITERIA added: (1) S2 gate — post-port inclusive ≤ ~1.15× of kernel sum (~14.0–14.5 ms @604k dense — COUNTERPARTY-RELAYED [Verifier A round-2 tag]); (2) trades NEVER cross the FFI — a half-port leaving ClosedTrade materialisation in Python collapses dense-mask gains to 1.04–1.16× (measured); fold_block columnar returns satisfy this by construction.
Anomaly on record: the audit's FIX-M "4.35 ns/bar @250 trades" fits neither config (likely unpinned) — flagged to them, do not cite it.
