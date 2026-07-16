# GDFL (Global Financial Datafeeds — globaldatafeeds.in) — vendor docs archive

> **Decision:** DEC-GDFL-VENDOR-DOCS-001 (MIGRATION.md #1780).
> **Captured:** 2026-07-13. **Posture:** SUMMARIZE-ONLY (see "Licensing posture" below).
> **Scope of this archive:** deliverables 1+2 of the GDFL evaluation — the docs archive
> and the definitive granularity/depth verdict. The extract/ingest DESIGN is deliberately
> a separate deliverable (see `INGEST_POINTER.md`).

## What this archive is

A permanent, in-repo record of everything we could verify about GDFL's market-data APIs
as of 2026-07-13, so the knowledge no longer lives only in ephemeral session scratchpads
and an egress-blocked vendor site. It answers the make-or-break BruteX question — *what
is GDFL's finest historical granularity, and how deep does it go?* — in `VERDICT.md`,
and preserves an own-words reference of the REST + WebSocket API surfaces plus the
PyPI SDK census with pinned re-fetch digests.

## Provenance

- The live `globaldatafeeds.in` site is **egress-blocked** from the build sandbox
  (every one of 63 attempted URLs failed at the proxy CONNECT stage; every
  archive/mirror avenue also blocked — the full attempt table is in `MANIFEST.md`).
  **Zero bytes of the live site were retrieved.**
- What WAS captured (2026-07-13): GDFL's **own vendor-authored API reference
  documents**, published by GDFL as PyPI package long-descriptions
  (author `Global Datafeeds <developer@globaldatafeeds.in>`), plus the full source of
  all seven GDFL PyPI packages (pypi.org is reachable from the sandbox). These
  verbatim captures live in the session scratchpad only; this archive carries
  own-words summaries of them (`api/REST_API.md`, `api/WEBSOCKET_API.md`,
  `sdk/PACKAGES.md`) with pinned PyPI URLs + SHA256 digests so anyone can re-fetch
  the identical originals.
- One additional measured input: the operator supplied 5 real GDFL NFO sample CSV
  files; a coordinator worker measured them at raw-byte level on 2026-07-13. The
  **measured statistics** are recorded in `VERDICT.md`; the raw market data itself is
  NOT committed (licensing posture below).

## Licensing posture — why no verbatim vendor text

GDFL's subscriber terms include "personal use" and single-subscriber clauses (prior
verified team research, 2026-07-12), and its redistribution terms are UNVERIFIED (no
public terms URL was even discoverable). This repo is PUBLIC. The coordinator therefore
ruled **SUMMARIZE-ONLY**:

- **No verbatim vendor prose** (sentences/paragraphs of GDFL-authored documentation)
  is committed here. API **facts and identifiers** — endpoint/function/MessageType
  names, parameter names, enum tokens, field names, JSON key shapes — are facts, not
  copyrightable prose, and are recorded in our own words.
- **No vendor source trees** are committed. The SDK census (`sdk/PACKAGES.md`) pins
  exact PyPI sdist/wheel URLs + SHA256 digests instead, so the originals are
  re-fetchable bit-for-bit.
- **No raw GDFL market data** (the sample CSV rows) is committed — only measured
  statistics (row counts, cadence histograms, schema field names).
- Nothing from an UNREACHABLE page is summarized or reproduced; per-page "what it is"
  labels in `MANIFEST.md` come from search-result titles only.

## Evidence-tier legend (used throughout this archive)

| Tier | Meaning |
|---|---|
| **SDK-VERIFIED** | Verified in GDFL's own PyPI-published SDK source / vendor-authored package reference (gfdl-rest@1.0.3, wsgfdl-py@1.3.5, ws-gfdl@1.1.0 et al., captured 2026-07-13); code claims AST-verified where stated. |
| **RAW-SAMPLE-MEASURED** | Measured at raw-byte level from the operator's 5 real GDFL NFO sample CSVs (coordinator worker, 2026-07-13; measured outside this container — statistics recorded, not re-derivable here). |
| **PRIOR-VERIFIED-NOTE** | Carried from the prior verified team research notes (GDFL vendor verification 2026-07-12; Indian tick-feed research 2026-07-09) — verified then, not re-derivable from this capture set. |
| **OFFICIAL-SNIPPET-UNCONFIRMED** | From search-result snippets of official GDFL pages that could NOT be fetched (site egress-blocked) — treat as a lead, never a fact. |
| **UNVERIFIED** | No primary source retrieved; explicitly flagged. |
| **NOT STATED IN SDK** | Grep-verified absent from every captured SDK/README — the SDKs are silent, not negative. |

## Tree map

```
docs/vendors/gdfl/
├── README.md            ← this file: provenance, licensing posture, tier legend
├── VERDICT.md           ← THE ANSWER: granularity + depth verdict, evidence table,
│                          10 credentialed probe gates, honest limits
├── MANIFEST.md          ← the full 75-attempt URL capture log (reached / blocked /
│                          mirrored) + the operator manual-upload list
├── INGEST_POINTER.md    ← 1-page pointer to the sibling ingest-design deliverable
├── api/
│   ├── REST_API.md      ← own-words summary of GDFL's REST API reference
│   └── WEBSOCKET_API.md ← own-words summary of GDFL's WebSocket API reference
└── sdk/
    └── PACKAGES.md      ← 7-package PyPI census + defects + pinned re-fetch digests
```

## Relationship to BruteX scope

This archive is **documentation only** — it authorizes nothing. The BruteX engine
stays NIFTY/BANKNIFTY/SENSEX (CLAUDE.md §2 Rule 3); the live-feed surface stays OUT of
BruteX per DEC-LIVE-REMOVE-001 (MIGRATION.md #862); the Track-1 vendor stays Groww
(DEC-DHAN-GROWW-REMIGRATE-001 / ADR-0009). Any GDFL adoption requires its own DEC +
the credentialed probe gates in `VERDICT.md` passing first.
