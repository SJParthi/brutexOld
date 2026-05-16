# ROADMAP.md — Retired

> ⚠️ **Retired 2026-05-16.** This file described a legacy 4-phase
> ingest pipeline (instrument master + expiries + contracts + candles)
> built around the prior broker's API. That code was deleted in the
> 2026-05-15 pivot.
>
> The O(1) + uniqueness + dedup persistence guarantees this document
> originally established are now codified in:
>
> - `docs/TRACK1_BACKTEST_SPEC.md` §3 — O(1) discipline
> - `docs/TRACK1_DATA_INGEST_DESIGN.md` — pipeline + state machine
> - `docs/TRACK1_VERIFICATION_PYRAMID.md` — 9-layer test pyramid
> - `docs/TRACK1_PHASE_PLAN.md` — 8-12 week build plan
> - `docs/TRACK1_OPTIMIZATIONS.md` — Phase 2.5 + 3.5 designs
>
> **Active build roadmap:** `docs/phases/00_MASTER_CHECKLIST.md`.
>
> Legacy content available in git history.
