# Broker API Docs — Offline Capture Index

This directory is the operator's **2026-07-15 offline capture** of four
vendors' official API documentation — **DhanHQ API v2**, **Groww Trade
API**, **TrueData**, and **GlobalDataFeeds (GFDL)** — uploaded to this
repository on **2026-07-16** to be saved for future reference.

The tree is a **byte-faithful copy** of the uploaded archive (verified
with a recursive diff against the extracted upload; nothing edited,
nothing fabricated). The capture's own manifest — [`README.md`](README.md)
— documents the capture method, per-file source URLs, the verification
pass, and the known gaps (vendor pages that are gated / email-only /
not publicly documented); each vendor folder additionally carries its own
`00-INDEX.md` (file map) and `SOURCES.md` (every URL fetched, with
ok/failed/gated status).

## Canonical-reference note (Groww)

`docs/groww/api-reference/` **remains the canonical Groww reference** for
CLAUDE.md / spec citations — those citations are pinned by file:line and
that tree stays byte-identical. This capture is the **raw
source-of-record** snapshot only: additive, never a replacement, and
nothing under `brutex/` reads these files.

## Contents — 134 captured files (+ this index)

| Folder | Vendor | Files | Coverage (derived from the captured file set) |
|---|---|---:|---|
| [`dhan/`](dhan/00-INDEX.md) | DhanHQ API v2 | 26 | Auth; orders (incl. super / forever orders, conditional trigger); portfolio; EDIS; trader's control; funds; statements; postback; live order update; market quote; live market feed; full market depth; historical data; expired options data; option chain; instruments / scrip master; annexure (enums + error codes); releases; Python SDK (+ SDK websockets) |
| [`groww/`](groww/00-INDEX.md) | Groww Trade API | 19 | Auth; instruments; orders + smart orders; portfolio; margin; live data; historical data; backtesting; websocket feed; user; annexures; Python SDK; rate limits; errors / exceptions; changelog |
| [`truedata/`](truedata/00-INDEX.md) | TrueData | 15 | Auth + connection; realtime websocket; history REST API; analytics / option chain / Greeks; symbols master; Python SDKs (current + legacy WS); Node.js SDK; .NET SDK; limits / errors / FAQ; Velocity external API; official announcements |
| [`globaldatafeeds/`](globaldatafeeds/00-INDEX.md) | GlobalDataFeeds (GFDL) | 73 | Getting started; fields / symbols / diagnostics; websocket API (connection + auth, subscribe, quotes, snapshot / history, instruments metadata, server info); REST API mirror of the same surface; streaming API; option chain; Greeks (WS + REST); delayed API; gainers / losers / volume shockers; .NET + COM APIs; code samples (JS / Python / Java / NodeJS / Postman / REST); pricing; FAQ; support; Fundamental Data API (endpoints `28a`–`28i`, schemas `28x1`–`28x3`, sources `28z`); News API (`29*`); market-data widgets; release notes; `ws-gfdl` Python library |

File-count notes:

- Per-folder counts include that folder's `00-INDEX.md` and `SOURCES.md`.
- `globaldatafeeds/` = 49 curated markdown pages + 24 crawler scratch
  copies (`_raw/` — 12 raw `.txt` page snapshots; `_decoded/` — 12
  decoded `.html` pages), kept exactly as uploaded.
- 1 top-level [`README.md`](README.md) (the capture's own manifest).
- Totals: 26 + 19 + 15 + 73 + 1 = **134** files, plus this index.

## Provenance

- **Source:** operator's upload of 2026-07-16 (archive
  `29decded-brokerapidocs.zip`, 459,588 bytes, 134 files), capture dated
  2026-07-15.
- **Fidelity:** extracted and copied byte-faithfully into
  `docs/broker-api-capture/`; verified byte-identical to the upload
  before commit. This index is the only file added on top of the
  capture.
- **Scope:** documentation reference only — no BruteX code, spec, or
  existing documentation file was modified; the engine scope
  (NIFTY/BANKNIFTY/SENSEX) and the live vendor (Groww, ADR-0009) are
  unchanged by this capture.
