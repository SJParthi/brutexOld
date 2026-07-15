# Zerodha Kite Connect as a BruteX data source — research synthesis

**Date of research:** 2026-07-10 (all vendor facts as-of this date unless stamped otherwise).
**Scope:** data-source research ONLY. Zero engine implications, zero code changes in this PR.
The brute-force ENGINE stays NIFTY/BANKNIFTY/SENSEX regardless of vendor.

**Retrieval honesty (Golden Rule #1/#10):** every Zerodha property (`kite.trade` docs + forum,
`zerodha.com`, `support.zerodha.com`) and `web.archive.org` was egress-blocked from this build
environment (agent-proxy CONNECT 403). All kite.trade/zerodha.com evidence below was obtained via
the WebSearch tool's live server-side retrieval of the named URLs, cross-corroborated where
possible with directly-fetched official Zerodha GitHub repos (`pykiteconnect`,
`dotnetkiteconnect`, `kiteconnect-rs`) and verbatim third-party mirrors of the docs pages. No
raw vendor page was byte-read in-session; confidence stamps reflect that cap. A credentialed
live probe is mandatory before any of this is encoded as fact in code.

Raw lens notes: `notes-depth.md`, `notes-pricing.md`, `notes-limits.md`, `notes-auth.md`,
`notes-quality.md` (same directory as this document).

---

## 1. Executive verdict

| # | Sub-question | Verdict | One line |
|---|---|---|---|
| Q1 | ~10y of 1-minute depth for NIFTY / BANKNIFTY / SENSEX + INDIA VIX | **PARTIAL** | NIFTY 50 minute data from 2015-01-09 (staff-confirmed, ~11.5y); BANKNIFTY assumed ~2015 but exact start UNVERIFIED; **SENSEX index historical candles likely NOT served at all** (March-2018 staff "not yet" + Jan-2024 no-data report, no later fix found; re-searched 2026-07-10 — no evidence of a fix); INDIA VIX candle depth UNVERIFIED. |
| Q2 | ~10y of 1-minute depth for the NSE equities universe | **PARTIAL** | Staff: minute data from ~2015-02-02 for "most contracts", some only from Q4-2015; the Feb-2025 announcement says "up to 10 years of intraday" (≈ mid-2016 today) — unresolved tension; pre-2019 intraday corporate-action adjustment covered only "more active contracts". |
| Q3 | Expired F&O history availability on Kite | **NO** | Expired OPTIONS candles: none, no plans to add (tokens re-used post-expiry). Expired FUTURES: DAY candles only via `continuous=1`. No minute-level data for ANY expired instrument — strictly worse than Groww's expired-options 1-minute support. |
| Q4 | Pricing / cost today | **YES** | ₹500/month per API key ("Connect" plan) including live + historical data; the separate ₹2,000/month historical add-on was abolished 2025-02-08; the free "Personal" tier carries NO market data. |
| Q5 | Rate-limit / pull-time feasibility | **YES** | Historical endpoint 3 req/s, minute window 60 days/request (window caps forum-staff-stated, medium confidence): 4 indices ≈ 284 requests ≈ 95 s; the full ~750-symbol universe ≤ 53,250 requests ≈ ≤ 4.9 h — fits one operator-ATTENDED evening session (derived; NOT a standing nightly pipeline — Q6's daily interactive TOTP login stands; formulas in §3). |
| Q6 | Unattended-auth compatibility with a zero-click automation decree | **NO** | Structural conflict: the access token dies every morning (~05:00–07:30 IST flush), refresh tokens are NOT issued to individuals, and Zerodha staff state the exchange mandates one MANUAL login per day. The only zero-click path is an unsanctioned scripted TOTP login that contradicts the vendor's on-record position. (Since 2026-04-01 the SEBI/NSE retail-algo framework additionally instruments API access — static IPs + 5-yr API logs for order-capable keys; data-only keys exempt per Zerodha — §2 framework row / §5.) |
| Q7 | Data quality (corporate-action adjustment, index continuity, gaps/outages) — operator requirement 5; numbered Q7 to preserve the Q1–Q6 cross-references used in §6 | **MIXED** | Day candles are exchange-bhavcopy-synced (authoritative); minute candles are Zerodha's own lossy internet-feed tick capture with permanent, staff-acknowledged unfixable holes (NIFTY spot + liquid stocks) and recent-candle mutation up to ~30 min after first serve. Equity history is back-adjusted IN PLACE on ex-dates (splits/bonuses/rights/spin-offs/extraordinary dividends; ordinary dividends not adjusted; pre-2019 intraday adjustment only for "more active contracts") — a MUTABLE series vs BruteX's immutable keep-first lake (§5). The 3 engine indices carry no corporate actions and are largely immune. Full evidence: §2 CA-policy / tier-era / docs-silence / minute-holes / recent-mutation / tiering rows. |

**Net:** Kite offers ~3.5–5 years more NSE minute depth than Groww's 2020-01-01 floor at trivial
cost (the surplus depends on the unresolved Q2 depth tension — staff-stated ~2015 floor vs the
Feb-2025 rolling "10 years of intraday" wording; ~5y is staff-confirmed only for NIFTY — probe item 4),
but (a) the SENSEX engine leg is probably unserved, (b) expired-options history — which Groww
serves at 1-minute — is absent, and (c) there is no sanctioned unattended daily auth. As a
*replacement* vendor it fails BruteX's automation decree and engine scope; as a *supplemental*
2015–2020 spot-index backfill source it is plausible pending live probes.

---

## 2. Claim-by-claim table

Confidence legend: sources were search-relayed unless marked "fetched directly". Capping rule
(stated once, applied uniformly): a claim resting on search-relayed evidence is capped at
**medium** — EXCEPT that **high** is permitted where the value is triangulated across multiple
independent relays **including at least one vendor-authored policy/support/announcement page**
(e.g. support.zerodha.com / zerodha.com posts stating the vendor's own policy), or is corroborated
by a directly-fetched source (official Zerodha GitHub repos, verbatim docs mirrors). Staff FORUM
statements, however consistent across threads, cap at medium (they interpret policy rather than
state it canonically) — this is why the auth rows sit at medium while the pricing rows carry high.
All access dates 2026-07-10.

| Claim | Verified value | Sources (URL + access date) | Confidence |
|---|---|---|---|
| General NSE minute-candle depth | ~early 2015; staff: 2015-02-02 for most contracts, some early-2015, others Q4-2015 (back-populated); official docs say only "spanning back several years" | kite.trade/forum/discussion/8722 (2026-07-10); kite.trade/forum/discussion/14149 (2026-07-10); kite.trade/docs/connect/v3/historical/ (2026-07-10) | medium |
| NIFTY 50 index minute depth | From **2015-01-09** (first bar 09:15 IST, O/H/L/C 8285.45/8295.9/8285.45/8292.1; support-confirmed earliest) | kite.trade/forum/discussion/8301 (2026-07-10); kite.trade/forum/discussion/908 (2026-07-10, older thread showing later back-population) | medium |
| BANKNIFTY index minute depth | Assumed ~2015; exact earliest date **UNVERIFIED** — must be probed (fetch + bisect) | kite.trade/forum/discussion/5088 (2026-07-10, search-level synthesis only) | UNVERIFIED |
| SENSEX index historical candles | Likely **NOT available**: March-2018 staff statement "We don't have historical data for SENSEX yet. We will add it soon." (thread 3637, 'Futures & Options on SENSEX'); Jan-2024 thread reports the historical API returning NO data for SENSEX/BANKEX with no staff resolution; instrument exists (BSE:SENSEX, token 265, real-time quote works). Re-searched 2026-07-10 for any 2024–2026 evidence that SENSEX index candles now work — none found. Current status **UNVERIFIED** | kite.trade/forum/discussion/3637 (2026-07-10, dated March 2018); kite.trade/forum/discussion/13626 (2026-07-10, dated Jan 2024); kite.trade/forum/discussion/13581 (2026-07-10) | low |
| INDIA VIX historical candles | Depth/availability **UNVERIFIED**. Instrument quotable (NSE:INDIA VIX, token 264969); forum lore notes real-time VIX quality wobbles; NSE's own site serves daily VIX history as a non-API alternative | kite.trade/forum/discussion/11788 (2026-07-10); kite.trade/forum/discussions/tagged/india-vix/p1 (2026-07-10); nseindia.com/reports-indices-historical-vix (2026-07-10) | UNVERIFIED |
| Day-candle depth (contrast) | No fixed floor (staff); NIFTY day candles ~2007; some NSE stocks back-filled to late 1990s; 2000 days/request | kite.trade/forum/discussion/14149 (2026-07-10); kite.trade/forum/discussion/10012 (2026-07-10) | medium |
| Expired options history | **None** — not served, no plans to add; exchange re-uses instrument tokens after expiry; expired contracts drop out of the instruments dump | kite.trade/forum/discussion/3666 (2026-07-10); kite.trade/forum/discussion/12880 (2026-07-10) | medium |
| Expired futures history | **Day candles only** via documented `continuous=1` flag on the historical API; no minute-level data for any expired instrument | kite.trade/forum/discussion/4369 (2026-07-10); zerodha/dotnetkiteconnect docs (GitHub, fetched directly, 2026-07-10) | medium |
| Historical endpoint + intervals | `GET /instruments/historical/:instrument_token/:interval`; documented intervals minute, 3/5/10/15/30/60minute, day (+ `continuous`, `oi` flags); no 2minute/2hour (note: pykiteconnect's integration tests also pass an `hour` alias, so the documented list may not be exhaustive of accepted aliases) | verbatim mirror raw.githubusercontent.com/Aditya7210/backtest Zerodha_API_Documentation.md (2026-07-10); zerodha/dotnetkiteconnect reference.md (fetched directly, 2026-07-10) | high |
| Per-request span limits | minute 60d; 3/5/10minute 100d; 15/30minute 200d; 60minute 400d; day 2000d. NOTE: NOT printed on the official docs page (two verbatim mirrors lack the table) — forum-staff-stated + server-enforced + encoded by community clients (kite-algo's test docstring encodes the exact table) | kite.trade/forum/discussion/2875 + /15111 (2026-07-10); github.com/mahimn01/kite-algo test_historical_chunking.py (2026-07-10, encodes the exact table); zerodha/pykiteconnect integration tests (fetched directly, 2026-07-10 — conservative SUB-cap spans, only day=2000d at cap, so they only weakly confirm the exact cap values) | medium |
| Historical rate limit | **3 req/s** for historical candle; Quote 1 req/s; order placement + all other endpoints 10 req/s; breach → HTTP 429 | verbatim mirror of kite.trade/docs/connect/v3/exceptions/ via raw.githubusercontent.com/mitulpatel123/ai-trading-system DATA_part3.txt (2026-07-10 — the sole source for the 10 req/s cells); kite.trade/forum/discussion/15281 (2026-07-10 — 3 req/s per key); pypi.org/project/kitetrader (2026-07-10 — corroborates ONLY the historical=3 and quote=1 cells; it self-throttles orders at 8 req/s/180 per min and default endpoints at 8 req/s) | high |
| Rate-limit enforcement unit | Per API key (aggregated across users/tokens on the key), not per user | kite.trade/forum/discussion/15281 (2026-07-10) | medium |
| Instruments dump | Gzipped CSV at api.kite.trade/instruments (per-exchange variant /instruments/NSE); regenerated once daily; vendor instructs one fetch/day (~08:30) and local storage; ~3–7 MB | mirror of kite.trade/docs/connect/v3/market-quotes/ (2026-07-10); pypi.org/project/kitetrader (2026-07-10) | high |
| Price today (Connect plan) | **₹500/month per API key**, cut from ₹2,000 — announced 2025-05-06 and effective from **2025-05-06** per Zerodha's own Kite Connect FAQ ("bringing the price down to INR 500 per month for the data (realtime + historical) APIs starting May 6, 2025"; re-verified live 2026-07-10). Forum #15015's "expected to go live by August 1" was a forward-looking billing-cycle statement, not the effective date. Current support article lists Personal (free) + Connect ₹500/mo | support.zerodha.com "what-are-the-charges-for-kite-apis" (2026-07-10); support.zerodha.com "kite-connect-api-faqs" (2026-07-10); kite.trade/forum/discussion/15015 (2026-07-10); marketcalls.in 2025-05-07 article (2026-07-10) | high |
| Historical-data add-on | **₹0 since 2025-02-08** — add-on abolished; historical bundled with base Connect subscription ("up to 10 years of intraday data for NSE/BSE"); existing apps auto-granted | kite.trade/forum/discussion/14806 (2026-07-10); support.zerodha.com "historical-data-and-live-market-data-payment-plan" (2026-07-10) | high |
| Free "Personal" tier | Free for individuals (launched Feb–Apr **2025**, not 2023); order/GTT/portfolio APIs only — "minus the market data (both real-time and historical)"; cannot serve the historical-candle use case | zerodha.com/z-connect/updates/free-personal-apis-from-kite-connect (2026-07-10); kite.trade/forum/discussion/14868 (2026-07-10) | high |
| "2023 free-for-individuals" premise | **NOT CONFIRMED** — no 2023 announcement found; every free-for-individuals source dates Feb–Apr 2025; pre-2025 pricing was ₹2,000/mo Connect + ₹2,000/mo historical add-on | marketcalls.in "zerodha-makes-trading-api-free-for-personal-use..." (2026-07-10); Zerodha LinkedIn post ≈2025-04 (2026-07-10) | UNVERIFIED |
| Daily auth flow | Interactive login (user_id + password + TOTP) → `request_token` on redirect → POST /session/token with SHA-256(api_key + request_token + api_secret) → `access_token` | kite.trade/docs/connect/v3/user/ (2026-07-10); github.com/zerodha/pykiteconnect (fetched directly, 2026-07-10) | high |
| Token lifetime | Access token flushed EVERY morning, ~05:00–07:30 IST band (most-cited: 07:30 flush; "expires daily around 6:00 AM"); no extension possible | kite.trade/forum/discussion/4108 (2026-07-10); /13884 (2026-07-10); /7759 (2026-07-10) | medium |
| Refresh token for individuals | **Not issued** — refresh_tokens go selectively to exchange-approved platforms; `generate_session` returns empty refresh_token for normal apps; "personal use → log in manually every day" (staff) | kite.trade/forum/discussion/5394 (2026-07-10); github.com/zerodha/kiteconnect-rs issue 20 (fetched directly, 2026-07-10) | medium |
| Vendor stance on login automation | Staff on record: "mandatory by the exchange that a trader has to login manually at least once in a day"; automating login "was never allowed to begin with"; TOTP 2FA mandatory since 2021-10-03 for accounts placing ORDERS via the Kite Connect API (thread 10391's announcement is scoped to order placement — "order placements via the Kite Connect API will require the respective accounts to have enabled 2FA TOTP" — not an unconditional login-TOTP mandate; SEBI-driven) | kite.trade/forum/discussion/11930 (2026-07-10); /10391 (2026-07-10); /9770 (2026-07-10) | medium |
| 2025–2026 SEBI/NSE retail-algo API framework (NEWER governing layer) | SEBI circular 2025-02-04 "Safer participation of retail investors in Algorithmic trading" (SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/13): open APIs disallowed; client-specific API keys; broker-whitelisted STATIC IPs (1 primary + optional 1 secondary; sharing family-only with 2FA-verified consent); OAuth + 2FA mandated; 10 orders/sec registration threshold; 5-year broker-side API-log retention. Implementation deadline extended to **2026-04-01** (SEBI circular 2025-09-30, SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/132); NSE operating-procedure circular 2025-07-22. Zerodha implementation live: static IP mandatory for API ORDER placement from 2026-04-01 (up to 2 IPs); per Zerodha, data endpoints (historical/WebSocket/orderbook) remain IP-unrestricted and data-only API keys are exempt — the exemption is currently FORUM/SUPPORT-evidenced, NOT circular-verbatim, and needs a live-probe pin (§6 item 6) | sebi.gov.in/legal/circulars/feb-2025/…91614.html (2026-07-10, re-verified live); sebi.gov.in/legal/circulars/sep-2025/…96979.html (2026-07-10, re-verified live); zerodha.com/z-connect "Explaining the latest SEBI algo trading regulations" + "A comprehensive overview of NSE's circular on the new retail algo trading framework" (2026-07-10); kite.trade/forum/discussion/15350, /15016, /15912, /15359 (2026-07-10); support.zerodha.com "What is a static IP and how to add one to your developer account?" (2026-07-10) | medium |
| Written ToS clause banning auto-login | **UNVERIFIED** — no verbatim agreement clause retrievable; prohibition evidenced only as repeated staff statements on exchange/SEBI grounds | kite.trade/forum/discussion/10391 (2026-07-10, negative search finding) | UNVERIFIED |
| Community auto-login workarounds | Widespread unsanctioned pyotp/selenium (and pure-requests) TOTP auto-login scripts exist publicly; no 2023–2026 ban wave on TOTP auto-login itself found (absence of evidence ≠ permission). CAVEAT: as of 2026-04-01 the enforcement surface changed STRUCTURALLY — order-capable API keys hard-reject requests from non-whitelisted static IPs and SEBI mandates 5-year broker-side API logging (see the 2025–2026 framework row) — so the 2021–2022-grounded "no enforcement" framing does NOT describe the current regime; the gray zone is now instrumented | github.com/rajivgpta/kite-api-autologin (fetched directly, 2026-07-10); kite.trade/forum/discussion/11879 (2026-07-10); kite.trade/forum/discussion/15912 (2026-07-10) | high |
| Corporate-action adjustment policy | Historical equity candles are **back-adjusted in place** on the ex-date (BOD process) for splits, bonuses, rights, spin-offs and EXTRAORDINARY dividends (2023/2024 staff statements); ordinary dividends NOT adjusted; policy visibly evolved from an older splits-only/weekend statement | kite.trade/forum/discussion/13348 (2026-07-10); /14295 (2026-07-10); /4014 (2026-07-10); support.zerodha.com "kite-charts-not-matching..." (2026-07-10) | medium |
| CA-adjustment coverage by tier/era | Post-2017: day + intraday adjusted; older data: day candles only; pre-2019 intraday adjusted only for "more active contracts" (from ~2015) | kite.trade/forum/discussion/8979 (2026-07-10); /6921 (2026-07-10) | medium |
| Docs silence on CA policy | The official historical-data docs page does NOT document the adjustment policy; it exists only in forum staff answers + one support article | kite.trade/docs/connect/v3/historical/ (2026-07-10, inferred from relayed retrievals — hence low) | low |
| Minute-series holes | Permanent, staff-acknowledged unfixable missing minutes exist across the archive — NIFTY spot (e.g. 2019-09-23 15:15/15:20; 2017-02-21 11:05–11:35; 2015-12-22; 2015-03-13) and liquid stocks (RELIANCE, March 2022) | kite.trade/forum/discussion/7138 (2026-07-10); /8969 (2026-07-10); /11491 (2026-07-10) | medium |
| Recent-candle mutation | Freshest minute candle changes past the 30-second mark; wrong live values corrected "usually around 30 minutes or so"; vendor positions the historical API as backtest-only (live candles from websocket ticks) | kite.trade/forum/discussion/15351 (2026-07-10); /14530 (2026-07-10) | medium |
| Data-quality tiering | Day candles synced to exchange bhavcopy (authoritative); minute candles are Zerodha's own internet-feed tick capture ("hardly a couple of ticks in a second over the internet") — legitimately differ from exchange records and other vendors | kite.trade/forum/discussion/8979 (2026-07-10); support.zerodha.com "kite-charts-not-matching..." (2026-07-10) | medium |
| Index tokens broker-minted | NIFTY 50 = instrument_token 256265 / exchange_token 1001, NIFTY BANK = 1016 (Zerodha-specific; other brokers use 26000/26009); key on exchange+tradingsymbol — numeric tokens re-used after expiries | kite.trade/forum/discussion/11137 (2026-07-10) | medium |
| Alternatives (one-liners) | NSE Data & Analytics (exchange-official, pricing on application); TrueData — 1-min IEOD history bundled with Velocity **Standard or higher** plans ONLY; the Velocity Lite tier (~₹1,440/mo, NSE EQ — price unconfirmed in-session, pricing page 403-blocked) EXCLUDES it and requires a separate IEOD purchase ("if you do not have Velocity subscription or have Velocity Lite plan, you need to purchase this data separately", re-verified 2026-07-10); GlobalDataFeeds (quote-based; stale 2019 ref ~₹70k/yr); iCharts (price UNVERIFIED) | nseindia.com eod-historical-data-subscription (2026-07-10); truedata.in/price (2026-07-10, 403-blocked in-session; IEOD terms via search relay of truedata.in IEOD pages); globaldatafeeds.in api-pricing (2026-07-10) | low |

---

## 3. Pull-time arithmetic

Inputs (each sourced in §2): historical rate cap **3 req/s**; minute-interval window cap
**60 days/request**; span 2015-01-01 → 2026-07-10 inclusive = **4,209 days** (python
`datetime`: `(date(2026,7,10)-date(2015,1,1)).days + 1`). Assumes perfect pacing, zero 429
retries, no other traffic on the API key (limits are per key). All outputs are DERIVED,
not vendor-stated.

```
requests_per_instrument = ceil(span_days / 60) = ceil(4209 / 60) = 71
wall_clock_seconds      = total_requests / 3 req/s
```

Window-semantics note (precision, conservative direction): `ceil(span/60)` assumes each request
yields exactly 60 calendar days. If the server-enforced cap is an INCLUSIVE from–to window
(`(to − from) ≤ 60 days`, i.e. up to 61 calendar days returned — a common reading of the
forum-stated table, which is not printed on the official docs page), the counts drop ~3%:
69 req/instrument; scenario (a) 276 requests; scenario (b) 51,750 requests ≈ 4.79 h. The
conservative bound is used throughout; the exact window semantics must be pinned in the
mandatory live probe (§6 item 7) alongside the depth bisect. Separately, no per-day or
per-minute cap is documented for the historical endpoint (forum staff: only order placement
carries per-minute/day caps — kite.trade/forum/discussion/13650, /13397, 2026-07-10), so the
single-evening scenario (b) claim is not blocked by any known cap.

**Scenario (a) — 4 engine/reference instruments** (NIFTY 50, BANKNIFTY, SENSEX, INDIA VIX),
full 1-minute depth:

```
total_requests = 4 × 71 = 284
wall_clock     = 284 / 3 ≈ 95 s ≈ 1.6 minutes
```

Caveats: assumes SENSEX and INDIA VIX historical candles exist (both UNVERIFIED/doubtful — §2);
if only NIFTY + BANKNIFTY are served, halve it.

**Scenario (b) — ~750-symbol NSE reference universe** (BruteX's current lake width), full
1-minute depth — honest UPPER bound:

```
total_requests = 750 × 71 = 53,250
wall_clock     = 53,250 / 3 = 17,750 s ≈ 4.93 hours
```

Sensitivities (derived):
- Depth actually 2015-02-02 (staff figure): 70 req/symbol — negligible change.
- Depth actually a rolling "10 years of intraday" (Feb-2025 announcement wording; ≈ 3,653
  days as of 2026-07-10): 61 req/symbol → 45,750 requests ≈ **4.24 h**.
- Later listings / shallower per-symbol depth strictly reduce the count (a 2021 listing needs
  ~34 chunks). Blind requests over pre-listing windows still burn budget — planning windows
  from per-symbol listing evidence (instruments dump + one probe request per symbol) is the
  real lever, exactly as BruteX's existing vendor-depth-floor learner does for Groww
  (`brutex/adapters/groww/vendor_floor.py`).
- Not modeled: the daily login (1 interactive event/day — see Q6), the instruments dump
  (1–2 requests/day), 429 retries.

**Contrast with the incumbent:** Groww's floor is 2020-01-01 at an operator-directed 8 r/s /
500-per-min governor; Kite offers ~3.5–5 years more depth (Q2 depth tension unresolved — staff
~2015 floor vs the rolling "10 years of intraday" reading, which yields ~3.6y of surplus; probe
item 4) at a lower documented cap (3 r/s), and the whole-universe backfill still fits in one
attended evening.

---

## 4. Integration sketch (hypothetical — NO code changes in this PR)

A Kite adapter would be a **second vendor package `brutex/adapters/kite/`** mirroring the
existing `brutex/adapters/groww/` package shape (adapter.py / client.py / constants.py /
chunks.py / errors.py / parser.py / instruments.py / ingest glue), with:

- **Rate control: REUSE, never copy.** `brutex/adapters/rate_common.py` is already
  vendor-neutral — `RateController(ceiling_rps=..., ceiling_per_min=...)` takes injected
  ceilings (the vendor-neutral default `CEILING_RPS = 5.0` is deliberately not a vendor
  number). A Kite build site would inject `ceiling_rps=3.0` (the documented historical cap)
  and `ceiling_per_min=None` (no documented per-minute governor found; the AIMD 429 ladder
  in `RateController` handles breaches the same way it does for Groww). Zero changes to
  `rate_common.py` itself.
- **Chunk planning:** a Kite twin of `brutex/adapters/groww/chunks.py` encoding the
  per-interval window caps (minute=60d, day=2000d, …) — same plan-then-fetch shape as
  `plan_candle_windows`.
- **Lake layout: SAME.** Bars land in the existing
  `bars/<exch>/<segment>/<symbol>/<interval>/<yyyy>/<mm>.parquet` layout via the existing
  `ParquetBarSink` — the lake is vendor-agnostic; only the fetch side changes. Prices snap to
  the 2-dp tick grid at the write boundary exactly as today.
- **Separate vendor floor constant:** a `KITE_HISTORY_FLOOR` (candidate value `2015-01-09`,
  the earliest staff-confirmed NIFTY minute bar — to be pinned by a live bisect probe per
  instrument, since depth varies per symbol). This is deliberately a SEPARATE constant from
  the incumbent `GROWW_HISTORY_FLOOR = date(2020, 1, 1)`, which lives in
  `brutex/dashboard/pull_controller.py` (note: the tasking brief placed it in
  `brutex/adapters/groww/constants.py`; the actual definition is in the pull controller, with
  the learned-floor machinery in `brutex/adapters/groww/vendor_floor.py`). The per-symbol
  depth-floor learner pattern from `vendor_floor.py` transfers directly.
- **Auth:** a Kite session module would mirror the shape of `brutex/adapters/groww/auth.py` /
  `reauth.py`, but there is NO sanctioned unattended mint path (§5) — the daily
  `request_token` → `access_token` exchange requires an interactive TOTP login. Any design
  must surface this as a daily operator action or an explicitly-ledgered unsanctioned
  automation, never silently.
- **2025–2026 SEBI/NSE retail-algo framework (MUST be recorded in any adoption ADR):** since
  2026-04-01 the SEBI 2025-02-04 circular regime is live (client-specific API keys,
  broker-whitelisted static IPs for order-capable keys, OAuth + 2FA, 10 orders/sec
  registration threshold, 5-year broker-side API-log retention — §2 framework row). Per
  Zerodha, data endpoints remain IP-unrestricted and DATA-ONLY API keys are exempt from the
  static-IP mandate — but that exemption is forum/support-evidenced, not circular-verbatim,
  so a data-only Kite adapter design must pin it with a live probe (§6 item 6) before relying
  on it. A pure historical-pull key needs no static IP under the current Zerodha reading; an
  order-capable key would.
- **ENGINE UNCHANGED.** NIFTY/BANKNIFTY/SENSEX scope, the 60-condition vocabulary, run
  identity, and all results surfaces are untouched — this is data-source research only.

---

## 5. Risks

**ToS / auth automation (blunt).** There is no sanctioned zero-click path. The access token
dies every morning (~05:00–07:30 IST), refresh tokens are not issued to individuals, and
Zerodha staff state on the record that the exchange mandates a manual login once per day and
that automating login "was never allowed to begin with" (as-of statements 2021–2022 on login
automation specifically; no reversal of THAT stance found). CORRECTION (hostile-review fold,
2026-07-10): the governing layer has since changed materially — the earlier draft's blanket
"no reversal found through 2026-07-10" negative-search claim missed the entire 2025–2026
SEBI/NSE retail-algo API regime, live since 2026-04-01 (SEBI circular 2025-02-04 + the
2025-09-30 extension to 2026-04-01 + NSE's 2025-07-22 operating procedure — §2 framework
row): order-capable API keys now hard-reject requests from non-whitelisted static IPs,
OAuth + 2FA are mandated, and brokers must retain 5 years of API logs — the auth surface is
now regulator-instrumented, not merely staff-frowned-upon. Per Zerodha, data-only keys and
data endpoints are exempt from the static-IP mandate (forum/support-evidenced, not
circular-verbatim — probe item 6), so the mandate binds order placement rather than
historical pulls. The widely-used pyotp/selenium auto-login scripts remain
unsanctioned gray-zone tooling that directly contradicts the vendor's stated position —
adopting one is a compliance/account risk and a daily single point of failure. This is a
STRUCTURAL conflict with BruteX's zero-clicks-per-day automation decree, not a tuning problem.
(A written ToS clause was not retrievable: UNVERIFIED — but the staff stance is consistent
across years of threads.)

**Cost.** Low and verified: ₹500/month per API key including historical data (as of
2026-07-10; add-on abolished 2025-02-08). The free Personal tier is useless here (no market
data). Budget risk is negligible; pricing-history whiplash (₹4,000 → ₹2,000 → ₹500 within
2025) suggests re-verifying before any commitment.

**Data quality — including corporate actions vs an immutable lake.** Kite's equity history is
a MUTABLE back-adjusted series: each ex-date rewrites all pre-ex-date prices in place (splits,
bonuses, rights, spin-offs, extraordinary dividends per the 2023/2024 policy). BruteX's lake is
append-only/keep-first with a full-content `data_snapshot_hash` — a window pulled before a
corporate action will silently diverge from the vendor afterward, and a re-pull returns
different bytes. The 3 engine indices carry no corporate actions and are largely immune, but
the ~750-stock REFERENCE_ONLY surface would need a CA-calendar + deliberate re-pull policy.
Additional quality caveats: permanent, staff-acknowledged unfixable minute holes (including
2022-era liquid stocks); recent candles mutate for up to ~30 minutes after first serve
(the vendor positions the API as backtest-only); minute candles are lossy internet-feed tick
capture while only day candles are bhavcopy-synced; pre-2019 intraday CA adjustment covered
only "more active contracts". And the two engine-critical gaps: SENSEX index history likely
absent, INDIA VIX depth unknown.

**Vendor dependence.** Depth, adjustment policy, and pricing have all changed materially over
the years (back-population of 2015 data, splits-only → multi-CA adjustment, three price points
in one year); the per-interval window caps and the CA policy are forum-stated rather than
documented, so they can move without a docs diff. Index instrument tokens are broker-minted
(key on exchange+tradingsymbol). Expired-derivatives absence means the options-translation
track cannot ever be served from Kite.

---

## 6. Recommendation

**Default: DO NOT adopt Kite Connect as a replacement for Groww.** Two hard blockers — the
structural zero-click auth conflict (Q6: NO) and the probable absence of the SENSEX engine leg
(Q1: PARTIAL, SENSEX likely unserved) — plus the loss of expired-options 1-minute history
(Q3: NO), which the current vendor uniquely provides for the options-translation track.

**Conditionally worth pursuing: a bounded, supplemental 2015→2020 spot backfill.** If (and
only if) a credentialed live probe confirms the required instruments, a one-time ₹500 month of
Kite Connect could deepen the index lake by ~5 years (staff-confirmed for NIFTY; the equities
surplus carries the unresolved Q2 tension) for ~41 seconds of pull time — the backfill-only
span 2015-01-01 → 2019-12-31 is 1,826 days → ceil(1826/60) = 31 req/instrument × 4 instruments
= 124 requests at 3 req/s ≈ 41 s (the earlier-quoted ~95 s was §3's FULL-span 2015→2026 figure
and remains the full-depth upper bound) — with the daily-login burden confined to the few days
of backfilling — an operator-attended one-off, not a standing pipeline, so the automation
decree is not violated. The immutability risk is minimal for indices (no corporate actions).

**Mandatory pre-decision live probes** (cheap, one probe session):
1. SENSEX index historical candles — do they exist at all today? (engine-leg blocker)
2. INDIA VIX historical candle availability + depth (required for `vix_at_entry/exit` stamps)
3. BANKNIFTY minute-depth bisect (exact earliest bar)
4. The "2015-02-02 depth" vs "rolling 10 years" tension, per instrument
5. Byte-verify the current pricing page and the CA-adjustment behaviour on a real ex-date
6. Pin the data-only API-key static-IP EXEMPTION under the 2026-04-01 SEBI/NSE regime against
   the NSE/SEBI circular text + a live data-only key (currently forum/support-evidenced only)
7. Pin the per-request window semantics (does the 60-day minute cap return 60 or 61 inclusive
   calendar days?) alongside the depth bisect (§3 window-semantics note)

Any adoption ADR must record the auth conflict bluntly ("no sanctioned unattended path;
scripted TOTP login contradicts the vendor's explicit position") — never softened to
"gray but fine" — AND must record the 2025–2026 SEBI/NSE retail-algo API framework (live
2026-04-01; §2 framework row + §4 bullet), including that the data-only static-IP exemption
is forum/support-evidenced and pending a live-probe pin (item 6 above).
