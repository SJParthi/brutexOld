# GDFL PyPI SDK census — 7 packages, defects, pinned re-fetch digests

> **Decision:** DEC-GDFL-VENDOR-DOCS-001 (MIGRATION.md #1780).
> **Captured:** 2026-07-13 — PyPI JSON metadata + latest sdists/wheels for every
> package, full source extracted and AST-scanned in the session scratchpad.
> Discovery: per-name JSON probes + the FULL PyPI simple index (848,349 projects)
> grepped for `gfdl`/`gdfl`/`datafeed` — this is the complete vendor family on
> PyPI. Vendor SOURCE TREES are deliberately NOT committed (SUMMARIZE-ONLY ruling,
> `../README.md`); the pinned URLs + SHA256 digests below re-fetch the identical
> originals bit-for-bit.

## Package census

| Package | Latest | Last release | Import name | Kind | Status |
|---|---|---|---|---|---|
| **wsgfdl-py** | **1.3.5** | 2025-11-28 | `gfdlws` | WebSocket (realtime + history + Greeks + reference) | **CURRENT / maintained** — newest release in the family; 1.3.5 adds volume-shockers, snapshot-Greeks, split-adjusted history |
| **gfdl-rest** | **1.0.3** | 2024-11-11 | `gfdl_rest` | REST (on-demand quotes + history + reference) | **CURRENT** REST SDK |
| GFDLWS | 0.0.4 | 2024-09-16 | `GFDLWS` | WebSocket | intermediate duplicate of the same codebase (between ws-gfdl 2022 and wsgfdl-py 1.3.x); superseded |
| ws-gfdl | 1.1.0 | 2022-08-12 | `gfdlws` (**collides with wsgfdl-py**) | WebSocket | original 2022 lineage; abandoned/superseded — but its long-description is the vendor WS reference this archive summarizes |
| restgfdl | 1.1.5 | 2024-08-23 | `restgfdl` | REST | employee-account predecessor of gfdl-rest (fewer functions); superseded |
| Gfdlws-Sub | 0.1 | 2024-10-16 | `Gfdlws_Sub` | WebSocket subscribe-only helper | one-off; the only genuinely async/callback client in the family |
| GlobalDatafeeds-ws | 0.0.1 | 2024-10-14 | — | — | **EMPTY placeholder** — no code in sdist OR wheel (verified) |

Also in the simple index: `restgfdl-py` — **zero releases** (`/pypi/restgfdl-py/json`
→ HTTP 404), a name-only registration. `gfdl` (LANL's "Gradient Free Deep
Learning" — its PyPI project URLs point at `github.com/lanl/GFDL`) plus
`gfdlfremake`, `gfdlnb`, `gfdlvitals`, `gdflib` (NOAA-GFDL/other) are ALL
UNRELATED; verified by author/project metadata.

Authors: `Global Datafeeds <developer@globaldatafeeds.in>` (wsgfdl-py, gfdl-rest,
GFDLWS, ws-gfdl, GlobalDatafeeds-ws); `Surendran M <surendran.m@globaldatafeeds.in>`
(restgfdl, Gfdlws-Sub) — a GDFL employee (the name appears as the sample `usertag`
in the official reference documents). wsgfdl-py project URLs point at
`https://globaldatafeeds.in/`, `https://globaldatafeeds.in/api-docs` and
`https://github.com/globaldatafeeds` — the GitHub org has **zero public
repositories** (authenticated search, 2026-07-13).

## The `getbyperiod` defect (AST-verified)

`wsgfdl-py` 1.3.4 and 1.3.5 ship a broken span-form history call:
`history.getbyperiod` passes **9 positional arguments to a 10-parameter,
zero-default coroutine** (`mass_subscribe_n_streamft(ws, exg, sym, prc, prd, frm,
to, max, iss, utg)`) → an immediate `TypeError` as shipped. The regression arrived
when a `max` parameter was added; the 2022 lineage (`ws-gfdl`/`GFDLWS`) carries a
9-parameter coroutine and works. Workarounds inside the package: the
split-adjusted call (which carries From/To/Max) or invoking the coroutine
directly. Other shipped defects (code-verified): the after-market history wrapper
returns `None` (prints, drops the response) and opens a second connection; bare
`except:` blocks return `None` on any error; the sync facade drives
`asyncio.get_event_loop().run_until_complete(...)` (deprecated ≥3.10, unusable
inside a running loop); `ws-gfdl` and `wsgfdl-py` both install the top-level
`gfdlws` package — never co-install. gfdl-rest's defects are listed in
`../api/REST_API.md`.

## Integration recommendation

**Speak the wire protocol directly; use the SDKs as protocol documentation.**
The REST surface is a paced `GET {base}{Function}/` with an `accessKey` param;
the WS surface is an `Authenticate` frame + JSON `MessageType` frames. The
official wrappers add broken (`getbyperiod`) and error-swallowing (bare
`except:` → `None`) layers plus a sync facade incompatible with an async host
process. If importing anyway: `gfdl-rest@1.0.3` (stateless GETs, easiest to pace)
and `wsgfdl-py@1.3.5` as the WS protocol reference (avoid `getbyperiod`). Ignore
GFDLWS / ws-gfdl / restgfdl (superseded duplicates), GlobalDatafeeds-ws (empty);
Gfdlws-Sub only as an async-pattern reference.

## PINNED RE-FETCH — exact PyPI file URLs + SHA256

Digests are PyPI's own (from each package's JSON metadata, captured 2026-07-13).
Every sdist below (and both wheels marked ✓) was downloaded in-session and
**re-hashed locally — all matched PyPI's recorded SHA256**.

### wsgfdl-py 1.3.5 (current WS)

- sdist `wsgfdl_py-1.3.5.tar.gz` (✓ re-hashed):
  `https://files.pythonhosted.org/packages/e5/e0/2fdfd7b53cd9cbca3331408edae7673d267dbcef867f4a7f44623e9efb11/wsgfdl_py-1.3.5.tar.gz`
  sha256 `38ad2c2c9dc41fb04a5b320e4f67be4378f3ab05985f7da5a0303baa30a5bfb9`
- wheel `wsgfdl_py-1.3.5-py3-none-any.whl`:
  `https://files.pythonhosted.org/packages/ad/2f/68a2e45680ff2e68173ede0e6d9809f01f0d6b521b7b2881c1aa6b46b3b3/wsgfdl_py-1.3.5-py3-none-any.whl`
  sha256 `58f43b01c6c4cac3e527ddc3a5bee37995c402400010e03b239552956c609645`
- wheel `wsgfdl_py-1.3.4-py3-none-any.whl` (✓ re-hashed; kept for the defect
  regression comparison):
  sha256 `dcbfa4d762a37bca7feec03864404c65a59dc418e5d4ffc8b7ace6e74d7b6f00`

### gfdl-rest 1.0.3 (current REST)

- sdist `gfdl_rest-1.0.3.tar.gz` (✓ re-hashed):
  `https://files.pythonhosted.org/packages/3b/ac/d85610bd737477bd3fb37250d5b549f0b69043178bd67015a95d93c56bd6/gfdl_rest-1.0.3.tar.gz`
  sha256 `fd2a1f77603a432532115d8178a220c65448101b5f963f7488a5fda621b91d9a`
- wheel `gfdl_rest-1.0.3-py3-none-any.whl`:
  `https://files.pythonhosted.org/packages/ed/f9/1ce027ca05387f6ebe1db946d4a6aae835c5cfeb29b13b39608af794d99a/gfdl_rest-1.0.3-py3-none-any.whl`
  sha256 `14d9a6842aa9b3153f1df0ff762872ca66e18e2edaedbb3aa86d3eeebc594ca5`

### Superseded / reference lineage

- `GFDLWS-0.0.4.tar.gz` (✓ re-hashed):
  `https://files.pythonhosted.org/packages/8c/da/c9b9afbff9d42739edbb6899c505ce07fde64ebc846a809cb7907428b342/GFDLWS-0.0.4.tar.gz`
  sha256 `0fdb2499de79eefd2e608bdb8611d788a135b0762110a30e82ed61decb9e9b9c`
- `ws-gfdl-1.1.0.tar.gz` (✓ re-hashed; long-description = the WS reference):
  `https://files.pythonhosted.org/packages/b7/54/70fea85f2efaf3b8e2ea35b64113c04e66217a5d1f8cb0acbff71c14c1c0/ws-gfdl-1.1.0.tar.gz`
  sha256 `e3cfe0081cd1718f148bb79470ddace4a49fba02262b7dbd49bf126055dd3e6f`
- `restgfdl-1.1.5.tar.gz` (✓ re-hashed):
  `https://files.pythonhosted.org/packages/50/f5/549101f7345ea8dda0414e38f909485b9c7db6d65200d4174f6206f352d9/restgfdl-1.1.5.tar.gz`
  sha256 `7b540023290abc2388b602fee0a9cba13fcb48da1fa0f95c975a136358e58c9f`
- `Gfdlws_Sub-0.1.tar.gz` (✓ re-hashed):
  `https://files.pythonhosted.org/packages/b5/52/dd9f21bea48d53db0e8aa32b5bf7736dcafd91631f7646fed2c2823a2b6c/Gfdlws_Sub-0.1.tar.gz`
  sha256 `89f73d7b79584154aaa7514e6d5256fdd24e4e1df0aa65ac28bbba41923daf97`
- `GlobalDatafeeds_ws-0.0.1.tar.gz` (✓ re-hashed; EMPTY placeholder):
  `https://files.pythonhosted.org/packages/13/53/dc4d5f0f5e8e69cbfc939afc9f8932fed93c53b76473536f30844f720979/GlobalDatafeeds_ws-0.0.1.tar.gz`
  sha256 `29cd35ee9cfd4209b0312f6313a69e4009dc1b61bb484ad9ad5af33d909b0371`
- `GlobalDatafeeds_ws-0.0.1-py3-none-any.whl` (✓ re-hashed):
  `https://files.pythonhosted.org/packages/81/1e/9ab864319828d58df44a182a94dd5717604d41a4b5bbd04cafa90adaac1d/GlobalDatafeeds_ws-0.0.1-py3-none-any.whl`
  sha256 `4ae06f7049a3920ca45e1a90d9b00d7f630761368db290cd1921a4f8ba1efc36`
