# BruteX — System Requirements (v1.0 — DEPRECATED 2026-05-15)

> ⚠️ **DEPRECATED — this document has been retired.**
>
> The original v1.0 spec (signed off 2026-04-14) targeted a Groww API +
> Indian F&O backtesting system. On **2026-05-15** the project pivoted
> to **Dhan + Indian spot indexes (NIFTY 50, BANKNIFTY, SENSEX) + India
> VIX (reference-only), signal-only research** (Track 1), followed by
> an options realism leg (Track 2). The pivot was driven by a 4-agent
> hostile-review pass that surfaced blocker issues with the original
> scope (spot is not tradable so brokerage maths was theatre; the
> throughput claim was 10x optimistic; Groww had no public spot-index
> history surface).
>
> The 1,900-line legacy spec has been removed from this file. To see
> what was here, check `git log --follow docs/REQUIREMENTS.md` or read
> commit `2e86bab` (the pivot commit).
>
> **Active sources of truth:**
> - `docs/TRACK1_BACKTEST_SPEC.md` — master spec (Track 1)
> - `docs/TRACK1_DATA_INGEST_DESIGN.md` — pipeline + state machine
> - `docs/TRACK1_VERIFICATION_PYRAMID.md` — 9-layer test pyramid
> - `docs/TRACK1_VOCABULARY.md` — 52 atomic conditions, K=2..5 ANDed
> - `docs/TRACK1_PHASE_PLAN.md` — 8-12 week build plan
> - `docs/TRACK1_PERFORMANCE_BUDGET.md` — honest SLA on M4 Pro
> - `docs/TRACK1_MICROSTRUCTURE.md` — Indian market edges
> - `docs/TRACK1_RUNTIME_ARCHITECTURE.md` — operations atlas
> - `docs/TRACK1_OPTIMIZATIONS.md` — Phase 2.5 + 3.5 designs
> - `docs/track2/TRACK2_OPTIONS_SPEC.md` — Track 2 options master spec
> - `docs/MIGRATION.md` — decision ledger (#1-#220+)
>
> The FR-###, NFR-###, MG-### identifier scheme from the legacy spec
> is retired. Track 1 / Track 2 docs use plain headings + the migration
> ledger's numeric decision IDs.

---

This file is preserved as a 404 stop so that the 39+ code/test/doc
references to `REQUIREMENTS.md` (e.g., `Per REQUIREMENTS.md §7A.4`)
resolve to this redirect notice rather than a missing-file error.
Those stale references will be migrated to point at the corresponding
TRACK1_* doc section during Phase 2 cleanup.
