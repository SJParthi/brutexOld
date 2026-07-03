---
title: Groww Trade API — Complete Documentation Pack (2026-07-03 snapshot)
captured_at: 2026-07-03
source: https://groww.in/trade-api/docs/python-sdk (+ REST section)
sdk_version_at_capture: 1.5.0
authority: REFERENCE SNAPSHOT ONLY. The OFFICIAL cited source-of-truth for
  Groww API facts remains docs/groww/api-reference/ (its README front-matter
  declares the authority; CLAUDE.md §6 cites those files by file:line).
  If the two ever disagree, reconciling a fact INTO api-reference/ (or into
  CLAUDE.md §6 / constants.py) requires its own decision — do NOT silently
  re-cite this directory from code.
decision: DEC-VENDOR-DOCS-IMPORT-001 (docs/MIGRATION.md #1144)
---

# Groww Trade API — full 26-page documentation snapshot (2026-07-03)

Operator-supplied complete capture of the Groww Trade API docs — **all 26
pages** in the in-page navigation (14 Python-SDK pages + 12 parallel
REST/curl pages), captured 2026-07-03 at `growwapi` SDK **1.5.0**. See
[00-INDEX.md](./00-INDEX.md) for the capture method, per-page verification
notes, and the file index.

## Why this directory exists alongside `docs/groww/api-reference/`

- `docs/groww/api-reference/` (fetched 2026-05-26) is the **authoritative,
  file:line-cited** reference — 8 SDK pages curated for Track 1
  (introduction / instruments / backtesting / annexures / exceptions /
  changelog / feed / live-data). CLAUDE.md §6 and
  `brutex/adapters/groww/constants.py` cite it by line number, so its files
  must never be renamed, renumbered, or overwritten in place.
- **This** directory is the **complete raw snapshot** — it additionally
  carries the pages the curated set never had (orders, smart-orders/GTT+OCO,
  portfolio, margin, user, historical-data) **plus the entire 12-page
  REST/curl parallel set** (raw endpoints behind the SDK, the three auth
  flows incl. the API-key+secret checksum generation, REST rate-limit and
  error-code tables). It is stored verbatim for future reference and drift
  checks; the numbering here (01-26) is the vendor nav order and does NOT
  match the api-reference numbering.

## Notable capture facts (see 00-INDEX.md for the verbatim list)

- Feed subscription cap now officially documented: 1000 instruments "at a
  time" (per-connection vs per-account still unstated by the vendor).
- Rate limits (type-level): Orders 10/s · 250/min | Live Data 10/s ·
  300/min | Non-Trading 20/s · 500/min — matching the api-reference quick
  card; the backtesting bucket remains undocumented (CLAUDE.md §2 Rule 6).
- The vendor's own docs carry an MCX contradiction (changelog v1.5.0 says
  MCX added; REST intro + smart-orders say COMMODITY not supported) —
  treat MCX as unverified.
