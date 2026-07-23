# TrueData (truedata.in) — vendor docs archive

> **Compiled:** 2026-07-20. **Posture:** SUMMARIZE-ONLY (see "Licensing posture" below).
> **Scope of this archive:** the decision-grade TrueData record for the operator's
> live-feed question — *does TrueData's WebSocket deliver genuine NSE tick-by-tick
> data, and what exactly does it deliver instead?* — answered in `VERDICT.md`, with
> own-words API summaries (`api/`) and the full capture provenance (`MANIFEST.md`).
> The cross-vendor decision doc is `../GDFL_VS_TRUEDATA_VERDICT.md`.

## What this archive is

The TrueData sibling of `docs/vendors/gdfl/` (DEC-GDFL-VENDOR-DOCS-001, MIGRATION.md
#1780): a permanent, in-repo, evidence-tiered record of what we can verify about
TrueData's real-time WebSocket + historical REST surface as of 2026-07-20, slotted into
the `docs/vendors/` convention WITHOUT duplicating the two prior TrueData homes (next
section). It is the decision-grade authority for the LIVE-socket question; protocol
minutiae stay with the #1966 pack.

## Relationship to the OTHER TrueData homes in this repo (read before citing)

Three TrueData doc homes now exist. Division of authority + the supersede rule:

| Home | Dated | What it is / authority |
|---|---|---|
| `docs/truedata/api-reference/` | 2026-07-15 | The DEC-GDF-TRUEDATA-DOCS-001 (MIGRATION.md #1966) research pack — README + 10 sections. **Protocol-detail authority at SDK tier** (pkg-source high / search-relay medium legend); carries the per-claim retrieval log (`10-retrieval-log.md`). |
| `docs/broker-api-capture/truedata/` | 2026-07-15 | The official-page capture from a session WITH working egress (00–13 + `SOURCES.md`, which marks pages "OK = fetched & archived"). The provenance base for this archive's **OFFICIAL-VERIFIED(capture-2026-07-15)** tier. |
| `docs/vendors/truedata/` (this archive) | 2026-07-20 | The decision-grade, summarize-only authority for the LIVE-socket / tick-by-tick question, freshly re-verified where load-bearing (SDK wheels re-downloaded + sha256-pinned this session). |

**Supersede-by-date rule:** conflicts resolve by strongest evidence tier first, newest
date second, and are ALWAYS stated in-place — never a silent contradiction of the
#1966 pack. One refinement is carried here (dated 2026-07-20): the #1966 feasibility
doc's Q2 phrase "true tick stream" was a pack-untiered inference from wire shape +
`tick_seq`; the fresh SDK-tier wire-format finding (second-granular timestamps — see
`VERDICT.md`) pins the stream as a ~1-second-class coalesced L1 snapshot feed. The Q2
*surface* verdict ("strongest live surface studied") itself stands.

## Provenance (2026-07-20 session)

- Every `*.truedata.in` host and every NSE host was **egress-blocked** from this build
  sandbox (proxy CONNECT 403; full attempt table in `MANIFEST.md`). Zero bytes of the
  live vendor/NSE sites were retrieved THIS session.
- What WAS captured this session: the official TrueData Python SDK wheels
  (`truedata` 7.0.1 + `truedata-ws` 5.0.11) downloaded via pip and read in full,
  sha256-pinned in `MANIFEST.md`; official-author sample-code fragments via
  authenticated GitHub code search; 12 search-snippet relays.
- Official-PAGE facts ride the in-repo 2026-07-15 capture
  (`docs/broker-api-capture/truedata/` — that session had working egress), cited as
  **OFFICIAL-VERIFIED(capture-2026-07-15)** with the original URL.

## Licensing posture — the summarize-only ruling, applied

The DEC-GDFL-VENDOR-DOCS-001 (#1780) SUMMARIZE-ONLY ruling is MIRRORED here, even
though TrueData's SDKs are OSS-licensed (npm MIT / PyPI GPL-3.0 — the #1966 pack's
recorded reason it was itself "NOT under the ruling"). This archive is decision-grade
and defensive; this repo is PUBLIC:

- **No verbatim vendor prose** beyond attributed quotes of ≤25 words. API facts and
  identifiers — endpoint/host/port names, message-type tokens, wire field names —
  are facts, not copyrightable prose, and are recorded in our own words.
- **No vendor source trees** committed. `MANIFEST.md` pins the exact SDK artifacts +
  SHA256 digests so the originals are re-fetchable bit-for-bit.
- **No raw market data** committed (none was captured — no credentials exist).
- Nothing from an unreachable page is summarized as fact; blocked-page labels in
  `MANIFEST.md` derive from search-result titles/snippets only.
- Auth examples use placeholders (`user=<USER>&password=<PASSWORD>`) — never real
  credential material.

## Evidence-tier legend (used throughout this archive)

| Tier | Meaning |
|---|---|
| **OFFICIAL-VERIFIED(capture-2026-07-15)** | Official vendor page actually fetched — by the in-repo 2026-07-15 capture session (`docs/broker-api-capture/truedata/SOURCES.md` marks it "OK"); cited with the original URL. No official page was fetchable in THIS session. |
| **SDK-VERIFIED** | Read from official SDK source downloaded THIS session (2026-07-20): `truedata` 7.0.1 / `truedata-ws` 5.0.11 wheels, sha256-pinned in `MANIFEST.md`; `(sample)` = official-author sample repo fragments via GitHub code search. |
| **PRIOR-VERIFIED-NOTE** | Carried from an in-repo doc (#1966 pack, feasibility doc) or a dated team-memory distillation (2026-07-16 catalogs of the PR #1595 branch docs — that branch's files are NOT on this tree); verified then, not re-derived here. |
| **OFFICIAL-SNIPPET-UNCONFIRMED** | Search snippet/summary of an official page nobody fetched (vendor or NSE) — a lead, never a fact. |
| **COMMUNITY** | Third-party code/articles/partner pages (FYERS, marketcalls, community repos). |
| **UNVERIFIED** | No adequate source retrieved; explicitly flagged, usually with the exact probe. |
| **BLOCKED** | Fetch attempted this session and denied at the egress proxy (HTTP 403 / CONNECT refusal) — an environment block, not a statement about the site. |

## Tree map

```
docs/vendors/truedata/
├── README.md            ← this file: provenance, licensing posture, tier legend,
│                          relationship to the other TrueData homes
├── VERDICT.md           ← THE ANSWER: what the live socket actually delivers,
│                          depth, authorization, latency honesty, history depth,
│                          evidence table, probe gates, honest limits
├── MANIFEST.md          ← the 2026-07-20 capture log (every URL + outcome), SDK
│                          sha256 pins, blocked-host list, operator upload list,
│                          pointers to the prior captures
└── api/
    ├── WEBSOCKET_API.md ← own-words summary of the live WS surface + cadence verdict
    └── HISTORY_API.md   ← own-words summary of the historical REST + depth verdict
```

## Relationship to BruteX scope

Documentation only — this archive authorizes nothing. The BruteX engine stays
NIFTY/BANKNIFTY/SENSEX (CLAUDE.md §2 Rule 3); the live-feed surface stays OUT of
BruteX per DEC-LIVE-REMOVE-001 (MIGRATION.md #862); the Track-1 vendor stays Groww
(DEC-DHAN-GROWW-REMIGRATE-001 / ADR-0009). TrueData's bundled history cannot reach
the BruteX 2020→today 1-minute lake floor (`api/HISTORY_API.md`;
`docs/research/GDF_TRUEDATA_FEASIBILITY.md` Q1: NO for both vendors as bundled). Any
TrueData adoption — live or historical — requires its own DEC + the credentialed
probe gates in `VERDICT.md` passing first.
