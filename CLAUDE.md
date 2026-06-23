# BruteX — Claude Code Project Rules

> Persistent instructions for Claude Code working in this repository.
> Every Claude session in `bruteX/` must read and honor this file.
>
> **Authority order:**
> 1. This file (`CLAUDE.md` — 12 Golden Rules)
> 2. Defaults
>
> The previous DEC-PROTOCOL-001 layer (tickvault adaptation doc +
> auto-loaded protocol bundle) remains RETIRED (DEC-P0-026, PR #48).
> The ONLY co-development surface re-established since is the shared
> `rust/trading-core/` crate — the single source of truth for the
> evaluate-time indicators (pivots + Fibonacci) + the 52-condition
> vocabulary, consumed bit-identically by BOTH BruteX (via the PyO3
> `trading_core` wheel) and TickVault (native Rust) per
> DEC-SHARED-CORE-001 (MIGRATION.md #441). The frozen 400-case golden
> answer-key is the contract both sides satisfy; no other
> adaptation/protocol coupling exists.

_Last updated: 2026-06-21 (DEC-FNO-MONEYNESS-LABEL-001, MIGRATION.md #869 — the
per-option moneyness + strike-distance is now shown as ONE combined cell
(**"ITM -1" / "ITM -10" / "OTM +1" / "OTM +10" / "ATM"**) instead of the split
bucket + ATM±N columns of #865 — `brutex/data/moneyness.py` `_format_label`
reformatted to `"<bucket> <signed depth>"` (sign tracks the BUCKET: ITM negative,
OTM positive, so calls AND puts read alike; depth = `|atm_offset|` strikes). The
movers screen + the `/db` F&O candle view each collapse the two columns into one
"moneyness" column (`OptionMover.moneyness_label`; `FNO_CANDLE_COLUMNS` /
`FNO_MONEYNESS_COLUMNS` now a single derived `moneyness` label). Pure DISPLAY reshape
over the SAME read-time-derived values; ENGINE stays NIFTY/BANKNIFTY/SENSEX, NO
storage/scope/vocab/rate change. Earlier: 2026-06-21 (DEC-SPOT-COMPLETENESS-2020-FLOOR-001,
MIGRATION.md #868 — the `/completeness` + `/universe` SPOT 1-minute verdict is HONESTLY
downgraded `full` → `partial` when the symbol's covered data does NOT begin in January 2020
(a late listing / late vendor data, even one starting later WITHIN 2020); one COMMON helper
`_spot_dimension_verdict_with_floor` so only symbols with the solid full-2020 history read
`full`; read/rollup-side only — `completeness.py`, ENGINE UNCHANGED). Earlier: 2026-06-21
(DEC-DB-CANDLE-COUNT-CACHE-001, MIGRATION.md #867 —
the `/db` candle-pager TOTAL (`COUNT(*)` over `read_parquet(glob)`, O(files) — a
Parquet-footer read per file, recomputed on EVERY paging click) is now memoised behind
a generation-stamped cache: `db_view.query_candles` gains an optional `count_cache`
(`CandleCountCache` Protocol) + `generation`, keyed by `candle_count_key(coords,start,
end)` (excludes limit/offset/sort/columns — the paging-invariance the cache exploits);
`build_app` REUSES the COMMON `MoversSnapshotCache` LRU and the `/api/v1/db/candles`
route passes `_summary_cache.generation` (a pull bumps it → recount, never stale —
Golden Rule #1). HONEST envelope: a cached-total HIT is O(1) in partition depth (bench
flat; COUNT runs once per dataset+generation), but paging is NOT O(1) overall — the
page DATA query (`ORDER BY ts DESC LIMIT` over the glob) is itself O(files) and ~2.4×
larger, so this removes only ONE of two O(files) footer scans (~16% on a deep
partition); true O(page) paging needs a write-time lake manifest (explicit FUTURE work,
NOT claimed). `db_view.py` at 100% line+branch; read-only viewer, ENGINE UNCHANGED.
Earlier: 2026-06-20 (DEC-UNIVERSE-BACKEND-CACHE-001, MIGRATION.md #866 —
the `/universe` BACKEND summary + instruments reads are now O(1) on a repeat poll:
NEW `brutex/dashboard/universe_cache.py` `UniverseSnapshotCache` memoises the three
lean store reads (`fno_eligible_underlyings` + `list_index_equity_instruments` +
`reference_only_security_ids`) + the derived `summary` / per-`kind` instrument rows,
keyed by a content-revision token = `(count(*), max(fetched_at))` over the
`instruments` table ITSELF (NOT the `instrument_runs` audit sidecar, which lags an
upsert — the stale-illusion trap this avoids, Golden Rule #1). Every `upsert_many`
stamps a fresh `fetched_at`, so any master change moves the token → rebuild; an empty
store keys at the stable `(0, "")`. Wired into `build_app` as a `universe_cache` param
(mirrors `summary_cache`/`movers_cache`); the routes call `_universe_cache.summary` /
`.instruments`. Honest envelope: HIT is O(1) (bench ratio 1.02× for a 100× deeper
universe; cold read scales 13×→166×), the first read after a load still pays the real
O(candidates) scan (once per revision). `universe_cache.py` at 100% line+branch;
read-only dashboard accelerator, ENGINE UNCHANGED (`InstrumentStore` untouched). This
COMPLETES on the backend the `/universe` FRONTEND O(1) work of #863. Earlier: 2026-06-20
(DEC-FNO-MONEYNESS-001, MIGRATION.md #865 — per-option
**moneyness (ITM/ATM/OTM) + a signed ATM±N strike offset** are now DERIVED at read
time (never stored — time-varying: the strike is fixed but the spot moves) for the
ENTIRE F&O universe (all ~212 stocks + indices), from the NEW shared engine
`brutex/data/moneyness.py` (`classify` / `derive_strike_step` [the **DYNAMIC** grid =
the min positive strike gap, never a hardcoded NSE rule] / `atm_offset` / `moneyness_view`).
Surfaced on BOTH the options-movers screen (as-of snapshot — `OptionMover` +
`compute_option_mover(strike_step=)` + two `movers.html` columns) AND the `/db` F&O
candle viewer (PER-BAR — `db_view.query_candles` appends `moneyness`+`atm_offset` to every
`segment="FNO"` page, classifying each bar against the underlying spot joined by timestamp;
a future reads `NA`, an option with no spot reads `UNKNOWN`). `fno_candle_lake.py`'s
`FNO_CANDLE_COLUMNS` advertises the two as `source="derived"`. Read-only — the brute-force
ENGINE stays NIFTY/BANKNIFTY/SENSEX; NO storage/scope/vocab/rate change. Earlier: 2026-06-20
(DEC-DB-VIEWER-PAGINATE-ALL-001, MIGRATION.md #864 —
EVERY `/db` tab is now O(page): the last two whole-table reads — the F&O registry
(expiries/contracts) + the window-ledger sidecar — are SQL-`LIMIT`/`OFFSET` paginated,
completing the candle/option/F&O/DuckDB-table pages already bounded. The registry is
fully O(page) (DuckDB `LIMIT/OFFSET` over its index + a cheap FILTERED `COUNT(*)`;
bench: first-page 1.04× for 10× more legs); the window-ledger is O(page) render+serialize
with a deliberately-kept O(windows) bounded read (a small sidecar — honest envelope,
NOT claimed O(page)-read). `registry_store.py` + `db_view.py` at 100% line+branch;
read-only viewer, ENGINE UNCHANGED. Earlier: 2026-06-20 (DEC-DB-UNIVERSE-O1-LATENCY-001, MIGRATION.md #863 —
the `/db` + `/universe` dashboard pages made INSTANT per interaction with ZERO
correctness change: `/db`'s candle pager no longer scans the WHOLE multi-year
1-minute partition to baseline the derived %-change columns on every paging click —
`db_view.py::query_candles` now reads the prior-day last-1-minute close for ONLY the
page's own date span + the single bar before it (`_intraday_prev_close_by_page_date`,
byte-for-byte identical to the old full scan, proven exhaustively + a flat-latency
bench), and the `/db` + `/universe` FRONTENDS update in place (grid-only `#dbgrid`
refresh on paging instead of a full control-bar+datepicker teardown; a per-tab
instrument cache + debounced search + in-place completeness patch). Honest envelope:
O(page date-span) / O(visible), NOT a literal O(1)-total. Engine + 52-vocab + scope
UNCHANGED — read-only viewer only. Earlier: 2026-06-20 (DEC-LIVE-REMOVE-001,
MIGRATION.md #862 — the **entire LIVE feed feature is REMOVED in full**: the
`brutex/live/` capture package + `python -m brutex.live` CLI, the dashboard `/live`
page + `/api/v1/live/*` + `/api/v1/db/live/*` routes + the `dashboard/live_*` surface,
the `live_ticks`/`live_candles_1m` lake tables, the Movers "Live" tab + the /db "Live
lake" section + the nav `/live` link (44 files). The production historical/backtest
PULL + `rate_common` + the brute-force ENGINE are UNTOUCHED — `adapters/groww/live.py`
`build_live_client` + `pull_controller._build_live_controller` are the PRODUCTION pull
client/rate-controller, NOT the feed, deliberately kept). Earlier: 2026-06-20 (DEC-FNO-CANDLE-LAKE-001,
MIGRATION.md #861 — the F&O (futures + options) historical-candle lake is now a
FIRST-CLASS documented `/db` table (a "📊 F&O candles" row, decoded per contract) +
registered in the master `schema_meta` (`fno_candle_lake.*`), FORMALIZING data ALREADY
stored by the Groww pull at `bars/<exch>/FNO/<groww_symbol>/<interval>/…` — NOT a new
store; lake-view only, ENGINE UNCHANGED). Earlier: 2026-06-20 (DEC-MOVERS-O1-LATENCY-001,
MIGRATION.md #860 — the `/movers` page made near-O(1) per interaction: the `top_n`-independent
lake scan is split from the cheap rank + cached per coordinate (`MoversSnapshotCache`,
generation-invalidated), and the cold per-symbol intraday read is bounded to the day's
`1minute/<yyyy>/<mm>.parquet` month file (`lake_day_glob`, byte-equivalent for session-filtered
symbols); read-only lake/dashboard, ENGINE UNCHANGED). Earlier: 2026-06-20 (DEC-OPTIONS-COMPLETENESS-REAL-YEARS-001,
MIGRATION.md #859 — the `/completeness` OPTIONS table shows only the years that ACTUALLY have
expiries + an honest `partial` for late-listed underlyings (JIOFIN/ETERNAL): a 0-expiry
pre-listing year no longer inflates `years` to a fake 2020→2026 span nor keeps it perpetually
`growing`; read/rollup-side only — `brutex/dashboard/completeness.py`, no pull/engine change).
Earlier: 2026-06-20 (DEC-1MINUTE-ONLY-DERIVE-1DAY-001,
MIGRATION.md #853 —
§1/§3/§6 facts: **1-minute is now the ONLY granularity fetched from Groww** (spot
+ futures + options); `1day` (PDH/PDL anchor) + the 19 intraday TFs are DERIVED
from the stored 1-minute and written to the lake (`spot_daily_resample`), never
separately vendor-fetched. This supersedes the operator-chosen granularity dropdown
of DEC-PULL-1DAY-FIRST-001 (#723, removed) + re-introduces the 1m→1d derivation
removed by DEC-REALDATA-ONLY-NO-1M-TO-1D-001 (#852) as the always-on PRIMARY path.
The "Verify vs NSE" bhavcopy cross-check is REMOVED in full; the F&O historical
DEFAULT window is per-contract `expiry − 3mo → expiry`. The brute-force ENGINE stays
NIFTY/BANKNIFTY/SENSEX. Earlier: 2026-06-19 (DEC-CLAUDEMD-ADAPTER-CITE-RECONCILE-001,
MIGRATION.md
#846 — §6 symbol-resolution rows refreshed after the Track-1 data-quality fix
series #799/#800/#801 merged: the `Symbol/key format` + `Track 1 brute-force
universe` rows now cite the master-authoritative `by_exchange_symbol` +
`groww_symbol_by_pair` resolution (DEC-GROWW-HYPHEN-SYMBOL-RESOLVE-001 #845, which
resolves hyphenated `BAJAJ-AUTO` + verbose-symbol `MIDCPNIFTY`) instead of the
stale "via `by_groww_symbol`" wording + drifted `adapter.py` line citations; the
`Historical OHLC` row's `adapter.py` line pointer refreshed to the relocated
`fetch_ohlcv`. No substantive §6 fact changed — the merged trio (1-day resample
#842, until-clean-all-outstanding #844, symbol-resolve #845) is lake/adapter/ingest
only, the brute-force ENGINE stays NIFTY/BANKNIFTY/SENSEX). Earlier: 2026-06-13
(DEC-PULL-1DAY-FIRST-001, MIGRATION.md #723 —
§1/§6 spot-interval facts: the spot pull granularity is now operator-chosen
per pull via the `/universe` dropdown with a VERIFY-FIRST **1-day-only**
default (`resolve_fetch_intervals`; 1-minute / both after the
OHLCV-vs-bhavcopy cross-check), no longer force-fetching 1-minute alongside
1-day). Earlier: 2026-06-13 (DEC-PULL-FLOOR-2020-001, MIGRATION.md #722 —
§3 locked scope range moved `2021-01-01 → today` → `2020-01-01 → today`:
the DATA pull floor (`GROWW_HISTORY_FLOOR`) lowered to the full
probe-verified Groww 1-minute depth, superseding the conservative
2021-01-01 floor of DEC-DHAN-PULL-SCOPE-001). Earlier: 2026-06-12
(DEC-CLAUDEMD-SPEC-LINE-RECONCILE-001,
MIGRATION.md #669 — renumbered from #667 by PR #629's duplicate-repair;
§2 Rule 4's run-identity citation refreshed
`TRACK1_BACKTEST_SPEC.md:654` → `:743`; spec growth had moved the
blake3 formula block and the stale line pointed mid-section). Earlier:
2026-06-10 (DEC-AUTOMERGE-CLEAN-STRICT-001, MIGRATION.md
#612 — §2 Rule 13(a) `mergeable_state` invariant aligned with the
charter §4.5 pre-G2 envelope per DEC-WORKSPACE-VERIFY-002 follow-up
F-4). Earlier: 2026-06-10 (DEC-WORKSPACE-VERIFY-002, MIGRATION.md #609 —
§6 prior-vendor `constants.py` line citations refreshed after the
5-agent workspace verification found 4 rows drifted by 7 lines; §1/§3
active branch refreshed). Earlier: 2026-06-03 (DEC-SHARED-CORE-001 Phase 2b,
MIGRATION.md
#441 — BruteX's 52-condition vocab now delegates to the shared
`rust/trading-core/` crate; §1/§5 reconciled). Earlier: 2026-06-01
(DEC-DASHBOARD-OMIT-AUDIT-001 — vendor docs reconciled Groww → the prior
vendor after the 5-agent workspace verification found §1/§6 stale).
DEC-P0-026
(PR #48) retired the tickvault co-development layer; DEC-SHARED-CORE-001
then re-established a single shared `trading-core` crate (indicators +
52-vocab) consumed by BruteX + TickVault. Vendor history: broker → Groww
migration (2026-05-15), Groww → prior-vendor pivot proposed then
reverted (ADR-0002, 2026-05-31 — the prior-vendor revert, now
superseded), then a full
re-migration back to Groww — **DEC-DHAN-GROWW-REMIGRATE-001 (2026-06-12,
ADR-0009) supersedes the revert and is authoritative: the live vendor is
Groww (the prior-vendor adapter package + reference docs were removed in
the ADR-0009 §5 cutover PR series).** Track 1 spec locked after 4-agent
hostile-review pass._

---

## 1. Project Identity

- **Name:** BruteX
- **Purpose:** Extreme drill-down brute-force backtesting engine for
  Indian **spot indexes** (NIFTY 50 / BANKNIFTY / SENSEX) + **India
  VIX** (volatility index). Track 1 is signal-only research; the
  Options Track translates winning signals to options for execution
  realism.
- **Data source (Track 1):** **Groww Trade API** via the `growwapi`
  Python SDK (injected `GrowwAPI` client; TOTP auth) per
  DEC-DHAN-GROWW-REMIGRATE-001 / ADR-0009 (which supersedes the
  ADR-0002 prior-vendor revert). One unified surface for
  spot AND options:
  1. `groww.get_historical_candles(...)` — fetch **`1minute` ONLY** for
     spot (DEC-1MINUTE-ONLY-DERIVE-1DAY-001, MIGRATION.md #853): 1-minute is
     the SINGLE granularity fetched from Groww, and `1day` (the PDH/PDL daily
     anchor) + the 19 intraday timeframes are **DERIVED deterministically from
     the stored 1-minute**, never separately vendor-fetched. The derived `1day`
     is resampled to midnight-IST daily bars (`brutex/data/spot_daily_resample.py`)
     and written to the lake's 1-day partition after every pull. This supersedes
     the operator-chosen granularity dropdown of DEC-PULL-1DAY-FIRST-001 (#723,
     removed) and re-introduces the 1m→1d derivation removed by
     DEC-REALDATA-ONLY-NO-1M-TO-1D-001 (#852) — now as the always-on PRIMARY
     path, since 1-minute is the sole fetched granularity (the derived 1-day is
     openly labelled derived, not vendor-official; Golden Rule #1).

  Expired options + futures (Options Track / lake) use the same
  `get_historical_candles` after resolving contracts via
  `groww.get_expiries(...)` → `groww.get_contracts(...)`, also at **`1minute`
  ONLY** with a DEFAULT window SPLIT by contract type — FUTURES `expiry −
  3 calendar months → expiry`, OPTIONS `expiry − 30 days → expiry`
  (DEC-FNO-LOOKBACK-SPLIT-001, MIGRATION.md #894; explicit operator dates
  override either default); the F&O `1day` is likewise
  DERIVED from the pulled 1-minute (DEC-1MINUTE-ONLY-DERIVE-1DAY-001). See §6
  for the full verified fact table.
- **Out of scope (Track 1 engine):** Live feed, WebSocket, order
  placement, portfolio, funds, multi-user SaaS. (Per DEC-P0-063 the LAKE
  additionally ingests F&O single-stock spot+options as REFERENCE_ONLY;
  the BRUTE-FORCE ENGINE remains the 3 indexes — see §2 Rule 3.) The dashboard
  + lake **LIVE-feed SURFACE that once existed** (`brutex/live/`, the `/live`
  page + `/api/v1/live/*` routes, the `live_ticks`/`live_candles_1m` tables, the
  Movers "Live" tab + /db "Live lake" section) was **REMOVED in full —
  DEC-LIVE-REMOVE-001 (MIGRATION.md #862)** — bringing the codebase in line with
  this out-of-scope rule. (The `live` = real/production naming-trap survivors —
  `adapters/groww/live.py` `build_live_client`/`build_live_pool`,
  `pull_controller._build_live_controller` — are the HISTORICAL pull's client +
  rate controller, NOT the feed, and stay.)
- **Active branch:** `claude/scoreboard-refresh-jlzt1n` (the customer-facing Product Scoreboard
  `docs/PRODUCT_SCOREBOARD.md` + the shareable `docs/product_scoreboard.html` one-pager are REFRESHED to cover the
  ledger through #907 — DEC-SCOREBOARD-REFRESH-001, MIGRATION.md #908, the operator's "provide the finished current
  future plans … easy table-level comparison even any human can understand … attract millions of customers" →
  "Cover everything dude". DOCS ONLY (NO code/engine/scope/vocab/rate change, ENGINE stays NIFTY/BANKNIFTY/SENSEX):
  both files rewritten to a themed finished/current/future comparison over the full recent batch (#861–#907) under
  SIX value themes (⚡ never slow · 📊 see what's moving · 🛡️ never lost · 📡 always informed · 🎯 data is right · 🚀
  one-click data), each row "what it does → why you care → ledger entry"; the citation column switched from the
  ambiguous "PR #" mix to the AUTHORITATIVE `MIGRATION.md #NNN` ledger entry (every claim traceable to the single
  source of truth); "Current" shows the two just-merged items (#906 rate-ceiling, #907 M&M diagnostic), "Future"
  reconciled (drops the merged #903/#904, adds the M&M read-side fix grounded in #907 + the in-place candle refresh);
  HTML mirrors the markdown 1:1 (zero-dependency inline CSS, badged "ledger #907"). HONEST: every row traces to a
  real merged ledger entry (no invented features/numbers); the engine-vs-lake scope distinction stated explicitly;
  the ledger re-declared authoritative on any disagreement; future rows labelled by honest stage. DOCS ONLY — no
  test/coverage surface (the no-hallucination PR-body scan + markdown render are the gates); ruff/mypy/pyright N/A
  (no code). ENGINE UNCHANGED, no scope/vocab/rate change. Prior merged: DEC-COMPLETENESS-RECONCILE-DIAG-001,
  MIGRATION.md #907 (an OBSERVE-ONLY `/completeness` diagnostic pinpointing why a stock — the operator's "M&M" — reads
  empty: a lake-key ↔ scored-key drift surfaced as ONE `completeness.symbol_reconcile_mismatch` WARNING naming both
  sides via `repr()`; `SPOT_PROBE_SEGMENTS`-filtered so a pure F&O backfill stays SILENT (the hostile-review MAJOR,
  folded) + internally guarded so a malformed row never breaks the page; scoreboard payload BYTE-UNCHANGED, DIAGNOSES
  not yet FIXES — the live `repr()` enables an evidence-grounded key-normalization fix next; `completeness.py` 100%
  line+branch (257/86), `app.py` stays 100%, 85 tests green). Prior merged: DEC-GROWW-RATE-CEILING-5-001, MIGRATION.md #906 (the production Groww
  per-second rate ceiling LOWERED 8 → 5 r/s — a live-log audit showed the controller pinned at the ~3 r/s soft floor
  absorbing ~870 vendor 429-retries while the 5–8 headroom was never usable throughput; single source-of-truth
  constant `GROWW_RATE_LIMIT_RPS_CEILING` `8.0`→`5.0`, both injection sites read it, AIMD ladder/governor/floors
  PROVABLY unchanged, `5×60 = 300 ≤ 500` property-tested; `constants.py` 100% (49/4) + `rate_common.py` 100%
  (140/36); SUPERSEDES DEC-GROWW-RATE-CEILING-8-001). Prior merged: DEC-FNO-PULL-ORDER-LOOKBACK-001, MIGRATION.md #905 (the historical F&O pull
  now pulls every FUTURES (FUT) contract BEFORE the CE/PE options AND defaults ALL F&O to a UNIFORM 30-day lookback
  — futures dropped from 3 months to 30 days; pull-PLANNING + ORDER only, NEW `_MARKET_PULL_ORDER` map +
  `sorted(by_market.items())`, `default_fno_minute_window` collapsed to one `DEFAULT_FNO_LOOKBACK_DAYS`=30, dead
  `subtract_calendar_months` removed; `historical_pull.py` 100% (211/46) + `contract_bounds.py` 100% (79/20)).
  Prior merged: DEC-ENGINE-TUPLE-SKIP-RESILIENT-001,
  MIGRATION.md #904 (when one grid TUPLE errors mid-sweep the engine SKIPS it, finishes the rest, exits NON-ZERO
  with a `tuples_failed` count + `TUPLE_FAILED` audit naming the combo — never silent, deterministic redo;
  per-combo isolation rejected as an illusion; BLOCKER-fixed + 3-agent-verified, 100% line+branch). Prior merged:
  DEC-MOVERS-OPTIONS-VALUE-DEDUP-001, MIGRATION.md #903 (the `/movers` Options screen reads each contract's
  1-minute frame ONCE — it reuses the HLC3 turnover the SHARED `_read_future_session` already returns instead of
  a second `_session_value_paisa` read; byte-identical value, half the per-contract I/O; `movers_options.py` 100%
  line+branch). Prior merged: DEC-PRODUCT-SCOREBOARD-001, MIGRATION.md #902 (a plain-English customer-facing "Product Scoreboard" —
  `docs/PRODUCT_SCOREBOARD.md` + a self-contained shareable `docs/product_scoreboard.html` one-pager — of
  finished / current / future work, themed what-it-does → why-you-care → PR, every claim traced to the ledger;
  docs only). Prior merged: DEC-WINDOW-LEDGER-WRITE-HALT-001, MIGRATION.md #901 (a disk-full /
  OOM during the durable window-ledger save now HALTS the pull gracefully via the lake-guarded `atomic_write_via`
  — ENOSPC→`LakeWriteHalt("disk_full")` / MemoryError→out_of_memory, every other error raw — reaching the
  CLI/dashboard halt handlers + their CRITICAL Telegram alert; `window_ledger_store.py` + `atomic_io.py` both
  100% line+branch, hostile-reviewer SHIP). Prior merged: DEC-MOVERS-FUTURES-VALUE-001,
  MIGRATION.md #900 (the `/movers` Futures tab gained a "Top Value" (turnover) ranking + a "Value (Cr)" column via
  the SHARED HLC3 `session_traded_value_paisa`, completing the value metric across spot+options+futures;
  `movers_futures.py`+`movers_options.py`+`movers_snapshot.py` all 100% line+branch, 307 movers tests green).
  Prior merged: DEC-MOVERS-VWAP-VALUE-001, MIGRATION.md #899 (the `/movers` "Top Value" (turnover) metric now uses the
  per-bar HLC3 typical price `(high+low+close)/3 × volume` instead of `close × volume`, so the ranking tracks a
  broker's intraday turnover far more closely — the operator's "for top
  value alone it is not precise to broker right? if yes then solve this". ROOT: the old `Σ round_money(close) × volume`
  priced a whole minute's volume at its CLOSING tick, systematically diverging from a broker's tick-summed turnover
  whenever the close sat away from the minute's VWAP. FIX (READ-ONLY ranking): NEW ONE COMMON helper
  `movers_snapshot.session_traded_value_paisa(rows)` (`Σ round_money((high+low+close)/3) × volume` → paisa) shared by
  BOTH the spot snapshot (`compute_snapshot`) AND the options screen (`_session_value_paisa`, which now also selects
  high/low) — killing the two duplicated close×volume loops; each bar's typical price is round_money-snapped to 2dp
  BEFORE × volume (DEC-MONEY-2DP-001); ranking/display unchanged. HONEST: a precision IMPROVEMENT (closest-to-broker
  from 1-minute OHLCV), NOT a bit-exact match (a broker sums actual trade price×qty per TICK — without ticks the HLC3
  ×volume sum is the best VWAP proxy, residual divergence remains); degenerates to the old answer for a zero-range bar;
  Spot+Options only (futures has no value tab). `movers_snapshot.py` + `movers_options.py` BOTH 100% line+branch
  (210/46 + 216/46); NEW golden proves HLC3 (318660 ≠ old 320000); 304 movers tests green; ruff/`mypy --strict`/
  `pyright` clean. ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate-cap change. Prior merged:
  DEC-GROWW-AMPERSAND-FALLBACK-001, MIGRATION.md #898 (F&O expiries/contracts for `&`-containing underlyings (M&M,
  M&MFIN, L&TFH, GVT&D, J&KBANK) now TRY a second `%26`-encoded symbol form when the plain form returns empty, so the
  whole-underlying chain failure is recovered. ROOT CAUSE (deep investigation + #870): the NSE `trading_symbol` `M&M`
  is handed verbatim to the `growwapi` SDK's `get_expiries`/`get_contracts`; the `&` is the HTTP query-string
  separator, sent UN-ENCODED, so the vendor returns a clean-but-EMPTY 200 — and because the break is at the
  chain-discovery ROOT (0 expiries → 0 `get_contracts` → 0 contracts) the ENTIRE underlying is missed. FIX
  (operator-chosen "try-both-forms"): NEW pure `underlying_symbol_forms` (plain FIRST, then `replace("&","%26")`) +
  generic `_fetch_first_nonempty_form`; the exact encoding is UNVERIFIED (needs a live Groww probe) — a HYPOTHESIS,
  NOT probe-confirmed; try-both is no-worse-than-before. The OPTIONS-path twin of the SPOT-only #845. `contracts.py`
  100% line+branch (92/34). Prior merged: DEC-DB-SPOT-CASCADE-001, MIGRATION.md #897 (the `/db` SPOT candle picker is now a symbol→interval CASCADE
  (the spot twin of the F&O cascade), replacing the flat ~750-row dropdown so the operator can pick an underlying
  symbol + a stored timeframe — the operator's "even for candles i can't
  select the underlying symbol or timeframe … we need the same as fno candle". NEW pure `spot_cascade.py`
  (`build_spot_cascade` → `SpotCascadeIndex.symbols()` O(1); `SpotSymbol` leaf carrying its STORED intervals inline;
  `symbol_to_json`) groups the non-F&O datasets by `(exchange, segment, symbol)`, sorted indices-first then alpha
  (the `INDEX` segment floats NIFTY/BANKNIFTY/SENSEX to the top, DATA-DRIVEN); ONE route `GET /api/v1/db/spot/symbols`
  via `_spot_cascade_or_none()` memoised behind `_summary_cache.generation` in a dedicated `_spot_cascade_cache`
  (REUSES the COMMON `MoversSnapshotCache` — no copy of `FnoCascadeCache`); the `db.html` frontend gains
  `cstate.spot` + `loadSpotCascade`/`onSpotSymbolChange`/`spotCascadeHtml` + a `csym` select, a symbol pick
  MATERIALISES the per-interval descriptors so the SHARED `query_candles` path is byte-unchanged. STORED-ONLY
  intervals (1m + derived 1d) — `query_candles` reads a physical partition + returns empty for an absent one, so
  offering all 21 TFs would be a phantom (mirrors F&O). HONEST: O(1)-cached per generation (cold build is one
  O(datasets) walk); picker-only (query path unchanged); only the expired-OPTIONS lake keeps the flat dropdown;
  `spot_cascade.py` imports nothing from `dashboard.*`/`data.*` (no import cycle). `spot_cascade.py` 100% line+branch
  (32/2, 6 unit); `app.py` stays 100%; NEW 4 integration tests; ruff/`mypy --strict`/`pyright` clean. ENGINE
  UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate-cap change. Prior merged: DEC-DB-TAB-SWITCH-O1-001,
  MIGRATION.md #896 (the `/db` candle tab switch (spot ↔ F&O ↔ option) is now O(1):
  the `/api/v1/db/candles/datasets` discovery endpoint — which walked the WHOLE `bars/` tree THREE times on EVERY tab
  switch — is memoised behind the shared write generation, so a switch within one generation is a cached handoff
  instead of a triple full-lake dir-walk — DEC-DB-TAB-SWITCH-O1-001, MIGRATION.md #896, the operator's "to navigate
  from F&O candles to candles also taking so much time". ROOT CAUSE (Explore-agent diagnosis): `_datasets()` ran THREE
  O(files) `bars/` dir-walks with NO cache (`list_candle_datasets` spot + `list_fno_candle_datasets` + `list_option_datasets`)
  on every tab switch; the merged O(page) work (#817/#818/#821) fixed within-tab PAGING but never the tab-SWITCH
  discovery. FIX (READ-ONLY viewer): wrap `_datasets()` in `_db_datasets_cache.get_or_compute("db_candle_datasets",
  generation=_summary_cache.generation, compute=_datasets)` — REUSING the COMMON generation-stamped `MoversSnapshotCache`
  (the SAME generic memo `_candle_count_cache`/`_window_counts_cache` use; NO new class, NO copy of `FnoCascadeCache`).
  Repeat switch within one generation = O(1) handoff; a pull bumps the generation → rediscover; build still runs on the
  threadpool worker on a miss. HONEST: O(1) on repeat, NOT a literal O(1) total (first switch after a pull pays one
  real triple walk); same `_datasets()` output, only discovery ORDERING changed (spy test: `list_fno_candle_datasets`
  runs ONCE across three loads, payloads byte-identical); the frontend full-DOM-rebuild + the spot symbol→interval
  CASCADE picker (#4b, the F&O-cascade twin) are flagged FAST-FOLLOWS. `app.py` stays 100% line+branch; NEW
  integration test; ruff/`mypy --strict`/`pyright` clean. ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no
  scope/vocab/rate-cap change. Prior merged: DEC-COMPLETENESS-O1-LATENCY-001,
  MIGRATION.md #895 (the `/completeness` endpoints — scoreboard + CSV +
  `/universe/completeness` — are now O(1) on a repeat load: the O(datasets) full-lake spot scan is routed through the
  SAME generation-stamped `_summary_cache` SPOT entry `/coverage/spot` already uses, instead of the prior UNCACHED
  direct call that re-ran the whole Parquet-footer aggregate on every load —
  the operator's 7–9 s page + the flagged FAST-FOLLOW of #893. ROOT CAUSE: the three completeness
  endpoints share `app._completeness_inputs`, which called `summarize_spot_datasets(store.connection, lake)` (a
  per-dataset `COUNT(*)`/`MIN`/`MAX` `read_parquet` footer aggregate, O(datasets)) DIRECTLY with NO cache — yet the
  sibling `/coverage/spot` wraps the BYTE-IDENTICAL call in `_summary_cache.get_or_refresh(SPOT, …)`. FIX (READ-ONLY
  dashboard): a one-expression DRY change — the scan now goes through `_summary_cache.get_or_refresh(SPOT, …)`, sharing
  the SAME SPOT cache entry (open either page → the other is warm); the ledger rollup `spot_window_rollup` (its OWN
  mtime cache) stays PER-REQUEST + OUTSIDE the SummaryCache (the C8 freshness contract — a ledger flush still refreshes
  the chips); the cheap `list_index_equity_instruments`/`list_coverage` SELECTs stay per-request (never stale). HONEST:
  repeat load is O(1) on the SPOT scan, NOT a literal O(1) total — the first read after a pull still pays one real
  O(datasets) scan + the cheap SELECTs run every request; correctness unchanged (same scan → same projection, only the
  cache ORDERING). `app.py` stays 100% line+branch (no new app branch — the hit/miss/throttle decision lives in the
  100%-covered `SummaryCache`); NEW integration test proves the scan runs ONCE across two completeness loads + one
  `/coverage/spot` load with identical payloads; coverage route suites green; ruff/`mypy --strict`/`pyright` clean.
  FOLDED hostile-review MAJOR: sharing the SPOT entry across two concurrently-polled routes widened a LATENT cold-start
  race in `SummaryCache._plan` (the `kind in self._refreshing` branch returned `None` → `self._entries[kind]` KeyError
  → 500 on a never-yet-populated kind); FIX gates the short-circuit on `entry is not None` so a cold reader recomputes
  (a transient idempotent double-scan, never a 500) — `summary_cache.py` stays 100% line+branch with a new
  cold-concurrent regression test, the warm fast path byte-unchanged.
  ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate-cap change. Prior merged: DEC-FNO-LOOKBACK-SPLIT-001,
  MIGRATION.md #894 (the F&O default historical-pull lookback is SPLIT by
  contract type: FUTURES default to 3 calendar months, OPTIONS to a tighter 30 days —
  the operator's "only for futures by default try to fetch it for 3 months but for options only
  one month, that is max 30 days". When no explicit from/to dates are given, `default_fno_minute_window` defaulted
  EVERY F&O contract to the last `DEFAULT_FNO_LOOKBACK_MONTHS = 3` months of its trading life; an option's liquid life
  is short + the chain is huge (every strike × CE/PE), so 3 months over-pulls illiquid early-life option bars. FIX
  (pull-PLANNING window only, F&O lake stays REFERENCE_ONLY): NEW `DEFAULT_OPTION_LOOKBACK_DAYS = 30` (a fixed 30-DAY
  cap per "max 30 days", NOT a calendar month) + `default_fno_minute_window` gains `is_option: bool = False` branching
  the lookback START — `expiry − 30 days` for an option vs `subtract_calendar_months(expiry, 3)` for a future — with
  the SAME `max(full_start, …)` / `min(full_end, expiry + 1 day)` clamp + the SAME empty-window + `None`-expiry guards;
  `historical_pull._resolve_minute_windows` passes `is_option=contract.contract_type in {"CE","PE"}` (types are exactly
  FUT/CE/PE; explicit operator dates still override via the `explicit_dates` branch). HONEST: changes ONLY the DEFAULT
  planning window when no dates are given (an explicit from/to range is byte-for-byte unchanged); 30 is a fixed
  day-count not one calendar month; both bounds stay clamped into `[full_start, full_end)`; pull-side only — no lake
  schema / stored-bar / rate / vocab / engine change. `contract_bounds.py` at 100% line+branch (87/20, 32 tests) +
  `historical_pull.py` at 100% line+branch (210/46, 40 tests); ruff/`mypy --strict`/`pyright` clean. ENGINE UNCHANGED
  (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate-cap change. Prior merged: DEC-COVERAGE-WINDOWS-O1-001, MIGRATION.md #893
  (the `/coverage/windows` "Fetch windows" page is now O(page):
  DuckDB `LIMIT`/`OFFSET` + a generation-cached `GROUP BY` replace the unconditional O(340,330) Python
  count+filter+sort that ran on EVERY tab/filter click + 4 s poll —
  the operator's "navigating section to section is very slow / O(1) latency everywhere" fix. A 3-agent investigation +
  the attached `brutex.log` proved the server `latency_ms` degraded tens-of-ms → 1.5–4 s as the ledger grew, because
  `build_window_payload` looped over every record for counts, then filtered every record, then sorted, then sliced —
  no SQL pushdown (the mtime cache saved the READ, not the PROCESSING). FIX (READ-ONLY viewer, modelled on the proven
  `/db`+`/movers`+`/universe` patterns): NEW `_build_window_payload_duckdb` reads the ledger Parquet via DuckDB — the
  ROW page is a `WHERE` (market-bucket + status, PLANNED excluded unless `?status=PLANNED`) + severity-first `ORDER BY`
  (CASE from `_WINDOW_STATUS_ORDER`) + `LIMIT`/`OFFSET` (O(page)); `counts` come from ONE `GROUP BY market, status`
  (`_window_counts_total`) memoised behind the COMMON generation-stamped `MoversSnapshotCache` (`_window_counts_cache`,
  keyed by `_summary_cache.generation`), and `matched` is DERIVED from the cached counts (no extra scan).
  `build_window_payload` gains optional `conn`/`offset`/`count_cache`/`generation`; the route injects them + a NEW
  `offset` param. HONEST: a cached-counts HIT is O(1) in partition depth + the page is O(page) (DuckDB still reads
  footers, NOT a literal O(1) total); a `None` conn / no Parquet keeps the LEGACY pyarrow path byte-for-byte
  (back-compat + the `window_status.json` fallback); the DuckDB path returns the SAME payload (fast-path-equals-legacy
  test); fixes the SERVER latency the log measured — a frontend in-place-`<tbody>`/debounce pager + the even-slower
  `/completeness` 7–9 s endpoints are flagged FAST-FOLLOWS, NOT claimed. `db_view.py` at 100% line+branch (NEW
  `TestBuildWindowPayloadDuckDB`); 322 covering-file + 31 route tests green; ruff/`mypy --strict`/`pyright` clean.
  ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate-cap change. Prior merged: DEC-NTM-CACHE-FIRST-001,
  MIGRATION.md #892 (the `/universe` NTM-constituent load is now CACHE-FIRST +
  fail-fast, so every load after the first is INSTANT instead of re-blocking ~30–160 s on the slow niftyindices.com
  host, the operator's "why is this taking so much time / why AGAIN"
  fix. Root cause: `instrument_loader._resolve_constituent_csv` was LIVE-FIRST — it ALWAYS attempted the slow live
  niftyindices fetch (`_default_opener`, 30 s × 3 retries ≈ 90 s) and used the on-disk last-good cache ONLY on a live
  FAILURE, so EVERY `/universe` load re-paid the full slow fetch even with a good cache present, all under
  `_LOAD_LOCK` (the operator's `latency_ms=162854.5` ≈ 163 s "stuck" page). FIX (READ-ONLY load): rewrite
  `_resolve_constituent_csv` CACHE-FIRST returning `(csv_text, source ∈ {cache_fresh, live, cache_stale, none})` — a
  cache YOUNGER than a TTL (`_ntm_cache_ttl_seconds`, env `BRUTEX_NTM_CACHE_TTL_SECONDS`, default 7 days; NTM
  rebalances only semi-annually CLAUDE.md §7) is served INSTANTLY with ZERO network call (the O(1) hot path); only a
  cold/stale cache hits ONE bounded live fetch (env-tunable `BRUTEX_NSE_FETCH_TIMEOUT_SECONDS` /
  `BRUTEX_NSE_FETCH_MAX_RETRIES`), now paid AT MOST ONCE per TTL not every load — the headline win is the cache-first
  ORDERING, NOT a timeout cut: the per-attempt default stays the operator-validated 30 s (a 10 s attempt previously
  read-timed-out on this host; 30 s was the deliberate fix) with retries trimmed 3 → 2 (30 s × 2 ≈ 61.5 s worst, down
  from 90 s), patient enough to succeed-once + seed the cache → instant forever; a live fail/empty with a stale cache falls back to it; nothing
  available → honest `none` degrade to the F&O base set. NEW `ntm_cache.load_cached_constituent_with_age` (+
  `CachedConstituents`) reads body+mtime once (age clamped ≥ 0 for clock skew); the `_ntm_admission` note carries the
  honest `source`/`from_cache`. HONEST: O(1)-latency on the STEADY-STATE load (fresh-cache hit = one stat+read, no
  network), NOT a literal O(1) first-ever boot (the cold/post-TTL refresh pays the bounded fetch ONCE); correctness
  unchanged (same CSV parsed/matched, only the ORDER + the live bound changed); a non-positive TTL explicitly
  DISABLES the fast path (always refresh) yet still serves a stale cache, so worst case is "as patient as before",
  never worse; no background threads / no new global state — fully synchronous + deterministic; the operator's manual
  "drop the CSV at ~/.brutex/cache/ntm_constituents.csv" now makes the very next load instant. `ntm_cache` +
  `nse_constituents` at 100% line+branch; `instrument_loader` 100% across its covering files; NEW tests — fresh-cache
  served with a `_must_not_fetch_opener` that ASSERTS the network is never called, stale-cache refreshed/empty/raise
  fallbacks (TTL=0 forces stale), no-cache+empty→`none`, the age helper edge-cases, the 3 env-tunable bound helpers;
  262 universe/constituents/loader tests green; ruff/`mypy --strict brutex`/`pyright brutex tests` clean. ENGINE
  UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate-cap change. Prior merged: DEC-ATOMIC-WRITE-FINISH-001,
  MIGRATION.md #891 (the LAST hand-rolled atomic-write copies — config runtime + credentials + NTM cache +
  window-ledger — share the ONE `atomic_write_simple` core (gaining an optional `chmod` for the credential 0600
  save), each preserving its OWN error contract (no LakeWriteHalt mis-escalation); +`fsync`-before-rename +
  temp-cleanup-on-any-error wins; the atomic-write COMMON consolidation is COMPLETE; deferred: the window_ledger
  ENOSPC→LakeWriteHalt graceful-halt guard-EXTEND). Prior merged: DEC-ATOMIC-WRITE-CONSOLIDATE-001,
  MIGRATION.md #890 (the engine sidecar writers share the ONE atomic-write
  core via a NEW guard-free `atomic_write_simple`, killing the hand-rolled `mkstemp`/`fsync`/`os.replace`/cleanup
  copies — the #885 COMMON follow-up: #885 centralised the
  LAKE writers' atomic write but left the others as noted debt. `results_store._atomic_write` +
  `sweep_checkpoint._atomic_write` were byte-near-identical copies (and `trial_stats`/`tuple_complete`/`sweep_heal`
  delegate to results_store, so 5 engine writers rode 2 copies). The blocker to reusing `atomic_write_via`: it bakes
  in `lake_write_guard` (ENOSPC/OOM → `LakeWriteHalt`), SEMANTICALLY WRONG for the engine (which wraps raw `OSError`
  into its OWN `ResultsStoreError`/`SweepCheckpointError` + is NOT wired for the lake global-stop). FIX: `atomic_io.py`
  refactored to a shared `_atomic_write_core` exposed as `atomic_write_via` (lake-guarded, the 2 Parquet writers,
  byte-unchanged) + a NEW `atomic_write_simple` (guard-FREE — every error incl. raw ENOSPC propagates as-is); the 2
  engine writers delegate to `atomic_write_simple`. HONEST: ZERO engine error-semantics change (same raw OSError →
  same domain-error contracts/tests preserved; disk-full NOT mis-escalated to LakeWriteHalt); a tiny durability win
  (core `finally` cleans the temp on ANY error vs the old `except OSError`); lake `atomic_write_via` byte-equivalent
  (#885 invariants re-verified); consolidates the ENGINE copies ONLY — the non-lake `runtime`/`credentials`(+chmod)/
  `ntm_cache`(returns False) + the `window_ledger` guard-extend remain the EXPLICIT next follow-up, NOT claimed.
  `atomic_io.py` at 100% line+branch (new atomic_write_simple tests); results_store/sweep_checkpoint stay 100%
  (cleanup tests repointed to `atomic_io.os.replace` + `.brutex-write-*` prefix); 268 engine+atomic_io+lake-writer
  tests green; ruff/`mypy --strict brutex` (254 files)/pyright clean. ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no
  scope/vocab/rate-cap change. Prior merged: DEC-TELEGRAM-PULL-HALT-DASHBOARD-001, MIGRATION.md #889 (the DASHBOARD
  pull path ALSO fires the CRITICAL
  pull-halt Telegram alert — the dashboard twin of #888 — so a halt reaches the operator regardless of which
  entrypoint pulls: #888 wired ONLY the CLI `cmd_fetch`
  path; the dashboard `/pull-pipeline` (primary pull UI) had no halt alert. A `LakeWriteHalt`/`QuotaInvariantError`
  propagates uncaught out of `pull_all_spot` to the ONE chokepoint `BackgroundPullJob._run`'s `except Exception`
  (`brutex/dashboard/pull_jobs.py`) where every backgrounded dashboard pull lands. (1) the CLI's `_emit_pull_halt_alert`
  body is MOVED to a COMMON `emit_pull_halt_alert(exc)` in `sweep_alerts.py` (ONE source of truth — `cli._emit_pull_halt_alert`
  is now a thin delegating wrapper, CLI byte-unchanged); (2) `_run`'s except now calls it GUARDED by a NEW `_is_pull_halt(exc)`
  (isinstance of the spine's global-stop set), so it fires ONLY for a pull-wide halt — a generic per-symbol worker error
  NEVER alerts — and the reported error state is byte-unchanged. HONEST: best-effort/silent-no-op-when-disabled;
  `pull_jobs.py` stays WHEEL-FREE at import (heavy `quota_tracker`/`data.errors` classes lazy-imported inside `_is_pull_halt`);
  the dashboard QUOTA stop is a VERDICT not a raise so it does NOT fire this hook (operator already sees it on the UI) —
  a verdict-driven phone alert is a flagged FOLLOW-UP (route bodies are pragma-no-cover), NOT claimed. `sweep_alerts.py`
  + `pull_jobs.py` both at 100% line+branch (new emit_pull_halt_alert + halt-fires-once / generic-no-alert tests); 147
  notifications+dashboard+CLI tests green; ruff/mypy --strict/pyright clean. ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX),
  no scope/vocab/rate-cap change. Prior merged: DEC-TELEGRAM-PULL-HALT-001, MIGRATION.md #888 (a pull-wide HALT —
  disk-full / OOM / quota — fires
  a CRITICAL Telegram alert so the operator is told in REAL TIME, not just in a log; the observability capstone of
  the resilience batch: the just-shipped batch makes a pull HALT
  gracefully on a full disk / OOM (`LakeWriteHalt` #885) or a quota stop, but the operator only learned of it from a
  log line (§11 AUTOMATED wants it ALERTED, "ZERO clicks per day"). NEW `SweepAlerter.pull_halted(*, reason)` alert
  (CRITICAL `PHASE_BLOCKED`, reason-scoped dedupe), wired into the CLI `cmd_fetch` pull path: `_one_fetch_pass` wraps
  the `run_groww_spot_ingest_with_retry` call in `try/except (LakeWriteHalt, DailyQuotaExceeded, QuotaInvariantError)`
  (the SAME global-stop set the spine carves out) that fires a NEW `_emit_pull_halt_alert(exc)` then RE-RAISES — so
  the existing halt handling + non-zero exit is byte-unchanged. HONEST: fire-and-forget, best-effort — the alerter
  swallows every error + is a silent no-op when Telegram is DISABLED (the default), so it can NEVER block/mask/crash
  the pull; the notifier is async (no network I/O on the pull thread); body is MarkdownV2-escaped; this wires the CLI
  pull path ONLY — the DASHBOARD pull path's Telegram wiring is an explicit FOLLOW-UP, NOT claimed done.
  `sweep_alerts.py` at 100% line+branch (new `pull_halted` unit + a real-notifier wire test); the CLI wiring covered
  by 3 new `test_cli_retry_storm.py` tests (LakeWriteHalt-raising fake ingest proves alert-once-then-re-raise +
  `_emit_pull_halt_alert` both branches); 137 notifications + CLI tests green; ruff/mypy --strict/pyright clean.
  ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate-cap change. Prior merged: DEC-LAKE-PARTITION-FLOCK-001,
  MIGRATION.md #887 (a cross-process advisory `fcntl.flock` now serialises
  concurrent writers to the SAME lake month partition, closing a SILENT data-LOSS race,
  gap #5 of the operator's critical-risks resilience-hardening
  batch: the two lake Parquet writers do a READ-modify-WRITE on a month partition guarded ONLY by a per-partition
  `threading.Lock` (intra-process), so two pulls sharing `BRUTEX_LAKE_ROOT` — a CLI `brutex fetch` + a dashboard
  pull — writing the same partition lost-update SILENTLY: P1 reads `[T1,T2,T3]`, P2 reads `[T1,T2,T3]`, P1 writes
  `[…,T4]`, P2's `os.replace` clobbers it → T4 gone, no error (keep-first only dedups WITHIN one process; across
  processes the last `os.replace` wins). NEW `brutex/data/partition_lock.py` (`partition_write_lock(lake_root,
  dest)` + `partition_lock_path`) takes a blocking exclusive `flock` held ACROSS the whole read-modify-write so
  the later process reads AFTER the earlier `os.replace`; BOTH writers nest it inside their existing per-partition
  `threading.Lock` (same lock ORDER everywhere → no cross-partition deadlock). HONEST envelope: the lock is on a
  STABLE sidecar `<lake_root>/.locks/<sha256(dest)>.lock` (NEVER the partition file — `os.replace` swaps its
  inode; `.locks` is outside `bars/`+`options/` and not a `*.parquet`, so no scanner sees it); PER PARTITION
  (different partitions never contend — fan-out parallelism kept); NO stale-lock recovery needed (the kernel
  releases an flock on fd-close/process death); O(1) per partition write (one open+flock+close, NOT per bar); a
  disk-full at lock creation maps to `LakeWriteHalt` (reuses #885's `lake_write_guard`); POSIX (Linux+macOS),
  Windows OUT OF SCOPE; it serialises CONCURRENT writers, NOT a cross-partition transaction. `partition_lock.py`
  at 100% line+branch; both writers stay 100% line+branch with NEW deterministic regression tests probing a
  SEPARATE fd DURING the merge READ to assert the lock is HELD (FAIL without the flock); chaos/sink/ingest/
  lake-scan suites green; ruff/mypy --strict/pyright clean. ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no
  scope/vocab/rate-cap change. Prior merged: DEC-MOVERS-CORRUPT-SKIP-001, MIGRATION.md #886 (a single corrupt /
  unreadable Parquet file now SKIPS
  that one instrument on the `/movers` screens — logged + counted — instead of 500-ing the WHOLE page,
  gap #4 of the operator's critical-risks resilience-hardening
  batch: a multi-agent audit flagged this the MOST production-likely HIGH — the spot/futures/options candidate
  loops (`movers_snapshot.compute_spot_candidates`, `movers_futures.compute_futures_candidates`,
  `movers_options.compute_options_candidates`) read each instrument's Parquet with NO per-instrument guard, so
  ONE corrupt / version-incompatible file raised out of the loop and — since the route recomputes per call —
  re-500'd EVERY `/movers` open until the file was manually quarantined. NEW `brutex/dashboard/movers_resilient.py`
  with `movers_read_skip_errors()` (the ONE place defining the unreadable-file set — empirically confirmed
  `polars.exceptions.PolarsError` + `OSError`, COMMON) + `log_movers_skip()`; each of the THREE candidate loops
  wraps its per-instrument read in `try/except` that on an unreadable file logs `movers.parquet_skip`
  (source+symbol+reason), increments the screen's existing `skipped` tally, and `continue`s — mirroring
  `db_view._aggregate_parquet_resilient` (the proven coverage-side pattern). HONEST envelope: NARROW catch — a
  genuine code bug (`ValueError`/`KeyError`/…) STILL propagates, never silently swallowed; a skipped instrument
  is OMITTED + counted, never a fabricated price (the file stays for the operator to quarantine); pure
  read-time DETECT-and-skip — no write surface, no engine/lake/scope change. `movers_resilient.py` at 100%
  line+branch; the 3 candidate readers all stay 100% line+branch with NEW integration tests writing a GOOD
  symbol + a SECOND whose on-disk Parquet is overwritten with garbage so the REAL `pl.read_parquet` raises a
  `ComputeError` — the screen still ranks the good one, reports the corrupt one `skipped`, logs the skip, and
  does NOT 500 (one per spot/futures/options); ruff/mypy --strict/pyright clean. READ-ONLY dashboard — ENGINE
  UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate-cap change. Prior merged: DEC-LAKE-WRITE-HALT-001,
  MIGRATION.md #885 (disk-full / OOM at the lake-write boundary now HALTS the
  pull gracefully — a `LakeWriteHalt` global stop — instead of crashing raw or quarantining per-symbol, via a
  shared `atomic_io` write helper, gap #3 of the operator's
  critical-risks resilience-hardening batch: NEW `brutex/data/errors.py` `LakeWriteHalt(DataLakeError)`
  (`reason` = `disk_full`/`out_of_memory`) + NEW `brutex/data/atomic_io.py` (`lake_write_guard` maps
  `ENOSPC`/`MemoryError`→`LakeWriteHalt`, every other error UNCHANGED; `atomic_write_via` = the ONE
  tmp→fsync→`os.replace`→cleanup). BOTH lake writers delegate to it; every allocation/failure site is guarded
  (a 4-agent hostile review folded two MAJORs — the spot FRESH render + the steady-state MERGE READ are both
  guarded, and `mkstemp` itself runs inside the guard) + `LakeWriteHalt` added to BOTH ingest spines'
  `_GLOBAL_STOP_ERRORS` so a full disk propagates past the per-symbol/contract quarantine and halts (temp
  always cleaned first — no half-written partition, no fabricated data). HONEST: graceful DETECTION + halt,
  NOT a memory CAP (a true RSS ceiling needs setrlimit/cgroups — OUT OF SCOPE); a non-`ENOSPC` `OSError`
  re-raises as-is (never mis-escalated); only the 2 data-lake writers consolidated (the other 5 atomic-write
  copies remain noted COMMON debt). `atomic_io.py`
  + `errors.py` 100% line+branch; the 2 writers + 2 ingest spines stay 100%; ruff/mypy --strict/pyright clean.
  ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate-cap change. Prior merged:
  DEC-CLI-LEDGER-CHECKPOINT-001, MIGRATION.md #884 (the `brutex fetch` CLI now checkpoints the durable
  window ledger MID-pull per window, not only at pull end, so a crash recovers in seconds instead of
  re-pulling the whole span — gap #2 of the operator's
  critical-risks resilience-hardening batch: `cmd_fetch` threads the SAME throttled `make_live_ledger_flush`
  the dashboard uses as the ingest engine's per-WINDOW `on_window` hook, wrapped in a NEW single-flight
  `window_ledger_store.make_locked_flush` (the `on_window` fires from PARALLEL worker threads + the throttled
  flush is not itself thread-safe); the CLI wants the flush ONLY so it does NOT use `make_live_sync` (which
  no-ops without an `on_ledger_sync`). A `None` ledger (`--dry-run`/`--inspect`) → no-op (the lock wraps a
  no-op), persistence-free contract preserved. Recovery bounded to one throttle interval (NOT bit-for-bit
  mid-window — a re-pull re-fetches; tmp→atomic-rename + keep-first sink absorb the duplicate). O(1) lock
  per window; the adaptive flush throttle still bounds the write rate. `window_ledger_store.py` 100%
  line+branch (177/50, incl. an 8-thread single-flight test); CLI fetch + window-ledger suites (72) green;
  ruff/mypy --strict/pyright clean. CLI/ledger-store only — ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no
  scope/vocab/rate-cap change. Prior merged: DEC-GROWW-SERVER-ERROR-RETRY-001, MIGRATION.md #883 (a transient
  Groww 5xx server error is now a RETRYABLE `GrowwServerError`, not a terminal quarantine — gap #1: NEW
  `GrowwServerError(GrowwError)` in
  `brutex/adapters/groww/errors.py`; `_CODE_TO_ERROR` maps `500`/`502`/`503` → it (504 deliberately stays a
  retryable `GrowwTimeoutError` — the timeout ladder already governs it) + a phrase-only `_SERVER_ERROR_HINTS`
  checked AFTER the rate-limit/timeout hints (priority preserved); added to `is_retryable` but NOT
  `is_rate_limit` (a 5xx is no quota signal — it rides the GENTLE timeout ladder, never steps the per-second
  rate ceiling down). The three consumption sites (`client.py`/`ingest.py`/`option_ingest.py`) pick it up via
  `is_retryable`/`is_rate_limit` with ZERO extra code; a real `404` stays terminal. `errors.py` 100%
  line+branch (48 stmts/16 branches); groww client/ingest/retry suites (140) green; ruff/mypy --strict/pyright
  clean. Adapter-only — ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate-cap change. Prior
  merged: DEC-OPTIONS-LAKE-READER-001, MIGRATION.md #882 (the options executor's `OptionBarSource` seam now
  has a PRODUCTION lake reader — gap-analysis item 1: NEW
  `brutex/options/lake_source.py` `LakeOptionBarSource` resolves a leg's `(instrument, expiry, strike,
  option_type, entry_ts, exit_ts)` to its F&O-candle-lake contract — DISCOVERED by scanning +
  `parse_fno_symbol`-matching the `bars/<exch>/FNO/` dirs (cached per underlying, robust, not a reconstructed
  string) — reads its 1-minute bars into a cached `{instant→OptionBar}` map, and returns the entry+exit
  `OptionBarPair` matched on the EXACT UTC minute, or `None` (never a fabricated price). `side`/`strike` from
  the query; `spot`/`iv` honest `None` (the candle lake stores neither). O(1) per `load_pair` after the
  per-contract read. `lake_source.py` 100% line+branch (13 unit) + 2 executor-integration tests pricing a
  real round-trip through `materialize_trade`; options suite (53) + F&O smoke (77) green; ruff/mypy
  --strict/pyright clean (a hostile-review BLOCKER on a mis-coded `# type: ignore` was folded — row typed
  `Mapping[str, Any]` like `movers_snapshot._row_to_bar`). REFERENCE_ONLY F&O lake; ENGINE UNCHANGED
  (NIFTY/BANKNIFTY/SENSEX). Prior merged: DEC-DB-FNO-COLUMNS-001, MIGRATION.md #881 (PR #835: the `/db` F&O candle view gains four read-time
  derived value columns: `spot` · `intrinsic` · `time_value` ·
  `days_to_expiry` appended after `moneyness`, ALL riding the SAME single per-bar underlying-spot join
  `moneyness` already does (no extra read). `db_view._fno_moneyness_rows` → `_fno_derived_rows` (5-tuple
  from ONE join) + pure `_option_intrinsic`; a future/undecodable reads `—`/`None` values but still gets
  `days_to_expiry`; an option with no spot reads `?`/`None` (never a fabricated price). `FNO_CANDLE_COLUMNS`
  advertises the four as `source="derived"`; the candle grid renders them column-generically. The stated
  part-2 follow-up to the #880 cascade. `/db` F&O moneyness/schema/datasets/candle-lake suites green;
  ruff/pyright/mypy --strict clean. READ-ONLY viewer; ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX). Prior merged:
  DEC-DB-FNO-CASCADE-001, MIGRATION.md #880 (PR #834: the `/db` F&O dataset
  selector was ONE flat dropdown of thousands of contract partitions (impossible to find a precise strike —
  the operator's screenshot). NEW pure `brutex/dashboard/fno_cascade.py` `build_fno_cascade` groups the
  decoded F&O datasets into a 3-level `FnoCascadeIndex` (O(1) `underlyings`/`expiries`/`contracts`,
  futures-first then numeric-strike sort, interval-aggregated, `unparsed` surfaced) + a generation-stamped
  `FnoCascadeCache` + 3 lazy routes `GET /api/v1/db/fno/{underlyings,expiries,contracts}` (COMMON
  `_fno_cascade_or_none` helper) + the `db.html` 3 dependent selects (the contract pick materialises the
  dataset so the SHARED candle query path is byte-unchanged; spot/option keep the flat dropdown). Derived
  from the LAKE partitions (not the empty `contracts` registry). `fno_cascade.py` at 100% line+branch
  (14 unit + 8 route/frontend integration); broader `/db` suite (229) green; ruff/pyright/mypy --strict
  clean. READ-ONLY viewer; richer F&O columns (days_to_expiry/spot/intrinsic/time_value) are the stated NEXT
  PR. ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX). Prior merged: DEC-FNO-WINDOW-PLAN-001, MIGRATION.md #879
  (PR #833: the F&O (options + futures) pull now PRE-PLANS every
  `(contract, interval)` window as `PLANNED` BEFORE any fetch, so the `/coverage/windows` Options +
  Futures tabs show a real planned total + units, exactly like spot — the operator's "why no planned
  items + units for futures and options?". Root cause: a `PLANNED` row was only ever seeded by spot's
  `plan_windows` (all 4 call sites are `market=GROWW_COVERAGE_MARKET`); F&O wrote a row ONLY at fetch
  time, so both tabs read 0. NEW shared `option_ingest._clamped_window_key` (ONE source of truth for the
  derivative ledger key — `ingest_option_contract` refactored onto it so the planned key is BYTE-IDENTICAL
  to the fetched key: the "planned == fetched" no-ghost invariant) + NEW `plan_contract_windows` (the F&O
  twin of spot `plan_windows`: idempotent plan + `prune_superseded`) WIRED into `run_groww_option_ingest`
  (auto-covers options `groww-option` + futures `groww-future`). The VIEW needed ZERO change —
  `build_window_payload` already buckets + counts PLANNED per namespace. `option_ingest.py` at 100%
  line+branch (50 pre-existing + 9 new + 1 e2e integration); ruff/pyright/mypy --strict clean. READ-ONLY
  F&O lake (REFERENCE_ONLY); ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX). Prior merged:
  DEC-PULL-MOVERS-WARM-001, MIGRATION.md #878 (PR #832: BINDING the gated phase-4
  warmer in the `/pull-pipeline` route. After a fully-successful fetch it pre-computes the default
  spot/futures/options movers snapshots for the LATEST AVAILABLE day (`latest_movers_date` — never
  'today', so never an empty illusion) into the SAME `MoversSnapshotCache` at the post-pull generation,
  reusing the EXACT `candidate_key`/`compute_*_candidates` the `/api/v1/movers` route uses. NEW pure
  `brutex/dashboard/movers_warm.py` (`latest_movers_date` + best-effort `warm_movers_snapshots`) at 100%
  line+branch; a warm failure NEVER fails the successful pull (movers also still auto-refresh on next
  open via the generation bump). READ-ONLY accelerator; ruff/pyright/mypy --strict clean. ENGINE UNCHANGED.
  Prior merged: DEC-PULL-SELECTED-PIPELINE-001, MIGRATION.md #877 (PR #831: the `/pull-pipeline` route + a new
  "▶ Pull selected — full pipeline" button run the gated spot→expiries+contracts→historical chain over ONLY
  the ticked instruments (`?symbols=`; no selection → whole universe, no regression), via the SHARED
  `resolve_pull_targets` resolver (COMMON; `/retry-failed` refactored onto it). `run_full_pipeline` gains an
  OPTIONAL gated 4th phase `run_movers` + `PipelinePullReport.movers_warmed` that runs ONLY after a
  fully-successful fetch (`historical_phase_ok`) — the operator's "never run movers unless the fetch
  succeeded". The WARMER is NOT yet bound in the route (a today-coordinate warm would cache empty snapshots
  right after a prev-day pull — an illusion avoided; movers already auto-refresh via the generation bump). 
  `pipeline_pull.py` at 100% line+branch; ruff/pyright/mypy --strict clean. ENGINE UNCHANGED. Prior merged:
  DEC-MOVERS-COMPLETENESS-001, MIGRATION.md #876 (PR #830: the `/movers` page defaults Top-N to 100 and offers ALL
  21 Track-1 timeframes (1m + 19 derived intraday + 1d), every one derived on-the-fly from the stored
  1-minute via `resample_1m_to_tf` — NO pre-materialised partition needed; the only prerequisite is
  1-minute spot + F&O candles. Backend already supported all 21 (`_INTERVAL_MINUTES` ← `LAKE_INTERVAL_LABELS`);
  the change is the dropdown (6→21) + the `TF_MIN` JS map + the three Top-N defaults 50→100 + the route cap
  `le=200`→`500`. Per-metric formulas re-verified correct. READ-ONLY display polish; movers_snapshot/futures/options
  stay 100% line+branch; ruff/pyright/mypy --strict clean. ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX). Prior merged:
  DEC-INSTRUMENT-ECONOMICS-INTEGRITY-001, MIGRATION.md #875 (PR #829: the economics sibling of #874 — a parse-time
  audit proving `lot_size` is uniform PER (underlying, expiry) across the whole `instrument.csv` (a within-expiry
  mismatch is RED), with cross-expiry lot revisions (legit NSE quarterly — AMBUJACEM 1050→1200) shown as
  INFORMATIONAL, never RED. The HONEST design (real-CSV-grounded, no illusion): lot/tick POSITIVITY is already
  upstream-guaranteed by `Instrument.__post_init__` (not re-checked — dead code), and tick_size legitimately
  varies by strike/premium band within an expiry (NOT checked — would false-flag 350 groups). NEW pure
  `brutex/data/economics_integrity.py` `audit_economics_integrity` (one O(legs) sweep, O(1) lookups,
  O(expiry-groups) space; excludes `*NSETEST*`; capped samples) + an ingest hook `_persist_economics_integrity`
  (structlog + persist to `schema_meta`) + routes `GET /api/v1/universe/economics-integrity[.csv]` (CSV via NEW
  `economics_integrity_csv.py` reusing #873's `FormulaSafeWriter`) + a green/red `/universe` "Economics integrity
  (lot size)" panel. Both new modules at 100% line+branch (8 unit + 2 property + 3 CSV + 1 golden + 3 integration);
  instrument_loader stays 100%; ruff/pyright/mypy --strict clean. REAL-FILE PROOF: the actual 164,248-row
  `instrument.csv` → GREEN, 101,792 legs over 684 (underlying,expiry) groups, 0 within-expiry mismatches, 45
  legit cross-expiry revisions surfaced informational. READ-ONLY, DETECT-only — assurance/observability, NOT a
  backtest-pull blocker. NO pull/engine/scope/vocab change — ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX). Prior merged:
  DEC-INSTRUMENT-MASTER-INTEGRITY-001 #874 (PR #828, parse-time leg↔spot mapping audit, real-CSV-verified) +
  DEC-MAPPING-INTEGRITY-EXPORT-001 #873 (PR #827, downloadable mapping-integrity CSV + golden gate + shared CSV guard) +
  DEC-ONE-CLICK-PIPELINE-001 #872 (PR #826, one-click full-universe pull pipeline spot→metadata→historical, gated) +
  DEC-FUTURES-PULL-ENABLE-001 #871 (PR #825, formally enable futures pulling, supersede never-pull decree) +
  DEC-MAPPING-INTEGRITY-RECONCILE-001 #870 (PR #823, real-time spot↔options mapping-integrity audit) +
  DEC-FNO-MONEYNESS-LABEL-001 #869 (PR #824, /movers + /db moneyness + strike-distance as one cell) +
  DEC-SPOT-COMPLETENESS-2020-FLOOR-001 #868 (PR #822, spot late-start → partial, Jan-2020 floor) +
  DEC-DB-CANDLE-COUNT-CACHE-001 #867 (PR #821, /db candle-pager total cache) +
  DEC-UNIVERSE-BACKEND-CACHE-001 #866 (PR #820, /universe backend O(1) content-token cache) +
  DEC-FNO-MONEYNESS-001 #865 (PR #819, per-option moneyness ITM/ATM/OTM + signed ATM±N) +
  DEC-DB-VIEWER-PAGINATE-ALL-001 #864 (PR #818, every /db tab O(page)) +
  DEC-DB-UNIVERSE-O1-LATENCY-001 #863 (PR #817, /db+/universe instant) +
  DEC-LIVE-REMOVE-001 #862 (PR #816, entire LIVE feed removed) +
  DEC-FNO-CANDLE-LAKE-001 #861 (PR #814, F&O candle lake first-class `/db` table + schema_meta) +
  DEC-MOVERS-O1-LATENCY-001 #860 (PR #815, /movers near-O(1) snapshot cache + month-bounded
  reads) + DEC-OPTIONS-COMPLETENESS-REAL-YEARS-001 #859 (PR #813, /completeness OPTIONS shows
  real expiry years + honest `partial`) + DEC-UNIVERSE-CONSTITUENTS-TICK-IN-PLACE-001 #858
  (PR #812))
---

## 2. Golden Rules (never violate)

1. **No hallucination.** Every factual claim about Groww, NSE, BSE, NIFTY
   composition, brokerage, taxes, or any external system must be
   traceable to a verified source. If unsure, say "unverified" and stop.
2. **Single source of truth.** All Track 1 specs live under
   `docs/TRACK1_*.md` + `docs/track2/TRACK2_*.md`. Every PR references
   a section + decision ID from `docs/MIGRATION.md`. The legacy
   `docs/REQUIREMENTS.md` is a retired stub (banner only) — do not
   read or derive new requirements from it.
3. **Scope lock.** Track 1 = Groww + spot indexes + India VIX (NIFTY 50,
   BANKNIFTY, SENSEX, INDIA VIX). **India VIX is REFERENCE ONLY** — it
   is fetched + stored and stamped as `vix_at_entry` / `vix_at_exit` on
   every trade, but is NEVER part of brute-force pattern combinations
   or the 52-condition vocabulary. 21 timeframes — **`1m` is the ONLY
   granularity fetched** and the 19 derived intraday TFs + `1d` are all
   DERIVED from it (DEC-1MINUTE-ONLY-DERIVE-1DAY-001, MIGRATION.md #853; the
   `1d` daily anchor is resampled to midnight-IST from the stored 1-minute and
   written to the lake's 1-day partition, never separately vendor-fetched).
   Rolling `2020-01-01 → today` range (the data pull floor
   — `GROWW_HISTORY_FLOOR`; DEC-PULL-FLOOR-2020-001, 2026-06-13, MIGRATION.md
   entry #722 — supersedes the prior conservative `2021-01-01` floor of
   DEC-DHAN-PULL-SCOPE-001 now that the Phase-0 granularity probe confirmed
   Groww serves 1-minute data from `2020-01-01T09:15:00`). No futures, no
   options, no F&O contracts. Do not silently expand scope.

   **Scope expansion (DEC-P0-063, 2026-05-22, MIGRATION.md entry
   #200):** Track 1's INGEST scope was formally expanded to additionally
   cover **200 NSE F&O eligible single stocks (spot + options across the
   vendor's expiry/contract addressing — Groww `get_expiries` →
   `get_contracts`)** for the LAKE. The **BRUTE-FORCE ENGINE is
   UNCHANGED** — it still operates only on the 3 brute-force indexes
   (NIFTY/BANKNIFTY/SENSEX); the 200 stocks join India VIX in
   `REFERENCE_ONLY` (mirrors decision #83). Stock options sit in the
   lake ready for future brute-force expansion (separate decision; not
   authorized here). The "4 index instruments / no F&O contracts"
   wording above describes the ENGINE; the LAKE is the expanded surface
   per DEC-P0-063. The §3 / cross-spec stale-text reconciliation is
   COMPLETE (DEC-P0-063-RECONCILE-COMPLETE-001 — a very-thorough survey
   found the "~15" estimate stale: the DEC-SPEC-CONSISTENCY series
   #262-#266 had already reconciled 11 locations; the 2 genuinely-stale
   remainders were fixed and 1 immutable ADR record correctly left
   as-is). DEC-P0-063 remains authoritative for the stock-options
   expansion.

   **Amended by DEC-INSTRUMENT-UNIVERSE-FAST-001 (2026-05-29, MIGRATION.md
   entry #160):** lean ingest — the `instruments` table now persists ONLY
   the ~241 unique universe underlyings (every admitted index + every
   F&O-eligible single-stock equity); the ~89,000 FUT/CE/PE legs are
   PARSED for eligibility then DISCARDED, recoverable on demand via
   `get_contracts` (contracts inherit lot/tick/freeze from the underlying
   row). The BRUTE-FORCE ENGINE is UNAFFECTED. So "the LAKE is the expanded
   surface" is narrowed: the lake = the universe underlyings; the legs are
   derived/recoverable, not stored.

   **Leg-economics fold (DEC-GROWW-LEG-ECONOMICS-001, 2026-06-13, MIGRATION.md
   entry #696):** before the legs are discarded, each persisted underlying is
   **stamped with its tradable F&O lot + freeze** harvested from its derivative
   legs (`universe.build_underlying_economics` →
   `dashboard/instrument_loader._fold_leg_economics`) — RELIANCE → 500, NIFTY →
   75 — SUPERSEDING the IDX/EQ row's cash placeholder (`lot_size` 1 / 0); the
   spot `tick_size` is preserved (a spot tick differs from its future's). This is
   the Groww twin of the retired-vendor fold; without it every F&O equity kept
   the placeholder `lot_size = 1`, which left `has_placeholder_stock_economics`
   permanently True and made the dashboard auto-heal re-load the master on every
   startup (a silent loop this closes).

   **Re-amended by DEC-NTM-GROWW-REPOINT-001 (2026-06-13, MIGRATION.md entry
   #694):** the lean universe ALSO persists the matched **NIFTY Total Market
   constituents** as REFERENCE_ONLY rows — the ~540 non-F&O names on top of the
   ~212 F&O-eligible stocks + the admitted indices, widening the lake to **~750
   underlyings**. The constituents are fetched best-effort from niftyindices
   (vendor-agnostic) and matched to Groww NSE cash-equity rows by ISIN / ticker
   (`dashboard/instrument_loader.py` `_ntm_admission`). The **BRUTE-FORCE ENGINE
   remains UNCHANGED** (NIFTY/BANKNIFTY/SENSEX) — NTM constituents are
   lake/REFERENCE_ONLY only, NEVER in the 52-condition vocabulary (the hard
   scope guard; an F&O-eligible name that is ALSO an NTM constituent stays
   tradable, `reference_only=FALSE`). On a network miss with no cache the load
   degrades honestly to the F&O-eligible base set, never invents constituents.
4. **Reproducibility first.** Every run is identified by `blake3(
   strategy_mask ‖ direction ‖ instrument ‖ tf ‖ mode ‖ canonical_params
   ‖ data_snapshot_hash ‖ indicator_version ‖ code_commit_hash)`. No
   computation without that hash recorded. (Was `sha256(...)` pre-DEC-CALIBRATE-001;
   corrected 2026-05-24 to match the spec's run-identity formula — now at
   `TRACK1_BACKTEST_SPEC.md:743` after spec growth moved it from :654
   (DEC-CLAUDEMD-SPEC-LINE-RECONCILE-001) — which has used blake3 since
   the v2 spec lock 2026-05-16. See MIGRATION.md entry #204.)
5. **O(1) discipline.** Bar lookup, indicator value lookup, combo dedup
   (Bloom + LMDB), strategy mask evaluation (u64 AND), and result
   append must all be O(1). CI gates: `p99(N=1M) / p99(N=1K) ≤ 1.1`
   for each hot path. Any regression fails CI.
6. **Rate-limit safety.** Groww publishes type-level limits (the
   **"Live Data"** group — Market Quote / LTP / OHLC — is **10 r/s,
   300/min**; `docs/groww/api-reference/01-introduction.md:130-134`).
   The **backtesting `get_historical_candles` per-second cap + daily
   quota are UNDOCUMENTED** and are an OPEN Phase-0 probe gate
   (`brutex/adapters/groww/constants.py:112-118`
   `GROWW_RATE_LIMIT_RPS_UNVERIFIED=10.0`,
   `GROWW_DAILY_QUOTA_UNVERIFIED=None`; ADR-0009 §3.1 risk #4 / §4) — the
   `_UNVERIFIED` suffix carries the honesty requirement in the
   identifier. The adapter paces with the vendor-neutral adaptive
   token-bucket controller (`brutex/adapters/rate_common.py:136`
   `RateController`, injected `ceiling_rps`; the 429/timeout ladders step
   down to a deep floor of **1 r/s** under sustained pressure and recover
   up to the ceiling once it clears). On TOP of the per-second bucket the
   controller now runs an **operator-confirmed 500 req/min HARD governor**
   (a rolling-60s issue-count cap, injected `ceiling_per_min` =
   `GROWW_RATE_LIMIT_PER_MIN=500`; **no daily quota** for backtesting —
   both operator-confirmed 2026-06-13, NOT vendor-documented;
   DEC-GROWW-RATE-PERMIN-001). The governor only ever LOWERS the issue
   rate (it caps; it never raises a ceiling), so it is a safe addition;
   never raise the per-second ceiling OR the per-minute cap without
   explicit user approval. The PRODUCTION per-second ceiling injected at
   BOTH live build sites (dashboard `_build_live_controller` + CLI live
   pull) is now the operator-DIRECTED **5 r/s**
   (`GROWW_RATE_LIMIT_RPS_CEILING=5.0`, operator-approved 2026-06-22, LOWERED
   8 → 5, DEC-GROWW-RATE-CEILING-5-001 — supersedes DEC-GROWW-RATE-CEILING-8-001)
   — chosen because 5×60 = 300/min stays well under the 500/min governor, AND a
   live-log audit showed the vendor 429s even at 3–5 r/s, so the 5–8 headroom was
   never usable throughput and only wasted ~870 429-retries probing into a wall.
   A cap-LOWERING is intrinsically safe (it only ever LOWERS the issue rate, never
   raises a ceiling); the per-second backtesting cap itself stays officially
   UNVERIFIED (`GROWW_RATE_LIMIT_RPS_UNVERIFIED`) and the adaptive ladder still
   steps DOWN on 429s. (The vendor-neutral `rate_common.CEILING_RPS=5` DEFAULT is
   unchanged.)
7. **Idempotence.** Every ingest chunk and every backtest combo must be
   rerun-safe. Same inputs → same outputs, bit-for-bit. The audit-log
   replay test is the oracle.
8. **Audit trail.** Every material action (ingest chunk, combo run,
   error, kill) appends one line to a hash-chained JSONL audit log.
   Breaking the chain fails CI.
9. **Hostile review before code.** Major design changes get a
   parallel-agent hostile review (Plan + general-purpose + Explore)
   before implementation. See the 4-agent pattern in
   `docs/TRACK1_PHASE_PLAN.md §0`.
10. **Honest limits.** If an SLA cannot be met under the current
    hardware envelope, SAY SO. No "millions in minutes" claims without
    naming the hardware AND citing public benchmarks. The 4-agent
    review on 2026-05-15 caught a 10x-overoptimistic throughput claim;
    do not repeat that pattern.
11. **COMMON · DYNAMIC · SCALABLE · AUTOMATED** (operator decree 2026-05-16).
    Every module obeys all 5:
    - **COMPREHENSIVE** — streaming + batch interface, bit-exact oracle
      test, look-ahead invariant test, serialize/restore.
    - **COMMON** — one source of truth (this repo). Every utility
      lives in exactly one place; downstream modules import. No
      copy-paste between modules.
    - **DYNAMIC** — add new indicator/TF/condition/mask without
      re-architecting. Plugin-style state classes, u64 mask vocab.
    - **SCALABLE** — same code path Day 1 (12.6M) → Phase 10 (730B).
      O(1) per-bar / per-mask / per-dedup / per-write.
    - **AUTOMATED** — 100% automated after one-time bootstrap.
      Release Please + auto-merge + watchdog + Telegram + auto-resume.
      Operator monitoring: ZERO clicks per day.
    See `docs/TRACK1_BACKTEST_SPEC.md` §"COMMON · DYNAMIC · SCALABLE ·
    AUTOMATED" for the full rule + enforcement matrix + violations list.
12. **100% ALWAYS STANDARDS** (operator decree 2026-05-16 evening).
    Every branch, every PR, every sub-PR, every commit, every task,
    every sub-task, every plan, every doc, every spec, every agent run
    obeys 18 standing requirements:
    coverage 100% line + 100% branch; mutation ≥ 85%; type-check
    (mypy strict AND pyright); static analysis (ruff + pylint + bandit);
    zero duplication; functionality + scenario completeness; bug-fix
    regression coverage; 7 test types (unit + integration + property +
    golden + bench + stress + mutation); CI every push + nightly soak;
    O(1) per hot path (p99 ratio ≤ 1.1); throughput canary ≥ 50 M/s;
    Bloom + LMDB 4-tier dedup; audit JSONL hash-chain on every
    material action; observability 4 pillars (tracking + logging +
    dashboard + alerting); 7 explicit SLAs; deep-research protocol
    (4-agent for major, 2-agent for compliance, 1-agent hostile for
    refactor); decision ID for every locked decision in MIGRATION.md.
    See `docs/STANDARDS_100PCT.md` for the full enforcement matrix +
    tooling per gate + auto-driver-test discipline. Any PR with a
    red gate is BLOCKED; operator override requires ADR.

    **Honest envelope (Golden Rule #10 + DEC-P0-040 / DEC-P0-042
    rebaselining):** The 18-requirement decree above is the **target**.
    The actual CI enforcement matrix in `docs/STANDARDS_100PCT.md` §1
    is **16 of 21 sub-rows 🟢 ENFORCED + 1 🟡 PARTIAL + 1 📋 ROADMAP +
    3 👁️ MANUAL** (~76% fully enforced, per the summary count in
    `docs/STANDARDS_100PCT.md` §1, post-DEC-COV-CLI-HARNESS-001
    2026-06-12 — coverage rows 1/2 flipped 🟢 when the omit list was
    deleted at a measured package-wide 100.00% line+branch; the one
    🟡 is row 3 pending DEC-MUTATION-RUNTIME-002's verification run.
    Earlier: row 3 honestly re-classified 🟡 while the orchestrator
    mutation pass exceeded its weekly budget, DEC-MUTATION-RUNTIME-001;
    the prior "15/2 (~71%)" wording here was the pre-re-classification
    snapshot, caught by the 2026-06-11 honesty audit per this file's own
    that-doc-wins rule and reconciled by DEC-HONESTY-AUDIT-RECONCILE-001).
    The earlier "9 of 21 / ~22%" wording here was a stale 2026-05-16
    snapshot from before the DEC-STANDARDS-AUDIT-001 → DEC-STRESS-001
    ratchet arc (audit + CROSSWALK + PYLINT-DROP + DOCTOR + STRESS
    closed 6 rows); reconciled by DEC-DOC-STANDARDS-RECONCILE-001
    (MIGRATION.md entry #591). `docs/STANDARDS_100PCT.md` §1 remains the
    single source of truth — if the two ever disagree, that doc wins and
    this line is the stale copy to fix. Per Golden Rule #10 no PR
    may claim "100% standards compliant" without first auditing against
    that matrix. The decree-vs-reality gap is a living ratchet, not a
    bypass — every gap row tracks a dedicated follow-up PR in
    MIGRATION.md. PRs that touch a gap row MUST update the row's
    status. See `docs/TRACK1_REQUIREMENTS_FINAL.md` §2 (DEC-P0-040)
    for the full enumeration.

13. **MERGE-READINESS CHARTER** (operator decree 2026-05-29,
    DEC-MERGE-GUARANTEE-001). Every PR / sub-PR / task / sub-task — in
    this session and every new session — obeys
    `docs/MERGE_READINESS_GUARANTEE.md`: the priority law
    (Correctness > Reliability > Recoverability > Observability >
    Performance > Scalability), the §3 merge-ready gate matrix, and the
    §4 Definition of Done. Two hard invariants: (a) **never merge over a
    red or pending gate** — every ✅ gate green, verified not assumed,
    and `mergeable_state` within the charter §4.5 envelope (strict
    `clean` is the G2-gated target; until server-side branch protection
    lands, `dirty`/`behind`/`blocked`/`unknown` are refused and
    pending-only `unstable` is tolerated —
    DEC-AUTOMERGE-CLEAN-STRICT-001); (b) **honesty** — per Golden
    Rule #10, never claim "100% / fully / guaranteed" for a 🟡/📋/🚫
    tier row; state what is green, partial, and out of scope. The
    charter is enforced on every surface via §8 (this file for sessions,
    `.github/PULL_REQUEST_TEMPLATE.md` for PRs, the `CI OK` required
    check for merges).

---

## 3. Branching and Commits

- **Active feature branch:** `claude/scoreboard-refresh-jlzt1n`
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

## 4. Coding Standards (when code phase starts)

- **Language:** Python 3.12 primary; Rust (PyO3) for hot kernels; no
  other languages without ADR.
- **Formatters:** `ruff format` (not black); `ruff check --fix`.
- **Types:** `mypy --strict` AND `pyright` both must pass.
- **Linters:** `ruff`, `pylint` (warnings-as-errors), `bandit` (security).
- **Tests:** `pytest`, `hypothesis` (property), `pytest-benchmark`
  (SLA), `mutmut` (mutation), `cargo test`, `proptest`,
  `cargo-mutants`, `cargo fuzz`.
- **Coverage gates:** 100% line + 100% branch on `brutex/` package.
  Mutation score ≥ 85% (Python AND Rust).
- **Docstrings:** every public symbol. NumPy style.
- **No `print`** — always `structlog`.
- **No bare `except`** — specific exceptions only.
- **No global mutable state** — inject dependencies.
- **No `pandas` in hot paths** — use `polars` or `numpy` + `numba`
  or Rust.
- **No look-ahead** — at bar N, the engine may only access bars 0..N.
  Enforced TODAY by TWO complementary runtime guards: (1) the
  feed-boundary guard (`ReplayFeed.ensure_no_lookahead`, raises
  `LookAheadError`; wired into the strategy runner + execution driver,
  integration-tested) — a *timestamp*-based check on emitted signals; and
  (2) the named **Property P-06 (instrumented bar accessor)** — now LIVE
  per **DEC-LOOKAHEAD-BARRIER-001** (`brutex/feed/lookahead.py`
  `LookAheadBarrier`): an *index*-based accessor that records every bar
  index `evaluate(N)` touches and raises `LookAheadError` the moment an
  access reaches index > N. It is active automatically on every sweep eval
  (the runner/driver build a default `record=False` O(1)-memory barrier) and
  rechecked by hand via `brutex verify-lookahead`. This is the **Phase-2
  strengthening** of the feed-boundary guard — belt-and-suspenders, NOT a
  perf path; the accessor is O(1) per bar access (NOT an O(1)-total claim).
  Promoted from the prior `pytest.skip` ROADMAP stub (was corrected per
  DEC-WORKSPACE-VERIFY-001 entry #398 D2 / DEC-DOC-DRIFT-RECONCILE-001).
- **Money display = 2 decimals, money-only** (DEC-MONEY-2DP-001) — every
  paisa→rupee display goes through `brutex.money` (`format_inr` /
  `round_money` / `paisa_to_rupees`). NEVER round statistical metrics (Sharpe,
  DSR, p-values, CPCV) or `iv` — they keep full precision or strategy ranking
  breaks. Money amounts (costs + display) are int64 paisa; OHLC/spot/option
  **prices** are stored as Decimal rupees (Parquet Float64), NOT paisa — and
  **snapped to the 2-decimal tick grid at the lake-write boundary**
  (`round_money`, `ROUND_HALF_UP` — never truncation; spot mirrors options) so
  the lake never holds vendor float-noise like `14020.7999`
  (DEC-SPOT-PRICE-2DP-001). See §6 (DEC-PAISA-DOC-RECONCILE-001).

---

## 5. Directory Conventions

```
bruteX/
├── CLAUDE.md                       ← this file
├── README.md
├── .claude/
│   ├── settings.json
│   └── agents/
├── docs/
│   ├── TRACK1_BACKTEST_SPEC.md     ← Track 1 master spec
│   ├── TRACK1_VERIFICATION_PYRAMID.md
│   ├── TRACK1_VOCABULARY.md        ← 52 atomic conditions
│   ├── TRACK1_PHASE_PLAN.md        ← 8-12 week build plan
│   ├── TRACK1_PERFORMANCE_BUDGET.md
│   ├── TRACK1_MICROSTRUCTURE.md
│   ├── TRACK1_DASHBOARD_DESIGN.md  ← v29 locked design spec + data contract
│   ├── dashboard/mockups/          ← v28.html + v29.html (canonical mockups)
│   ├── MIGRATION.md                ← decision ledger
│   ├── REQUIREMENTS.md             ← RETIRED (stub)
│   ├── REQUIREMENTS_SIMPLE.md      ← RETIRED (stub)
│   ├── ROADMAP.md                  ← RETIRED (stub)
│   ├── adapters.md                 ← RETIRED (stub)
│   ├── adr/                        ← architecture decisions
│   └── runbooks/
├── brutex/                         ← Python package
│   ├── adapters/groww/             ← Groww Trade API adapter (growwapi SDK)
│   ├── calendars/                  ← NSE/BSE trading calendar
│   ├── config/
│   ├── costs/                      ← brokerage/tax cost models
│   ├── dashboard/                  ← FastAPI UI
│   ├── data/                       ← Parquet + DuckDB lake (generic)
│   ├── engine/                     ← brute-force combo/mask sweep engine
│   ├── execution/                  ← signal-only simulator (Track 1)
│   ├── feed/                       ← replay + resampler
│   ├── indicators/                 ← evaluate-time indicators (pivots/fib/EMA)
│   ├── math/                       ← thin facade to Rust hot kernel
│   │                                 (fno_math + trading_core re-export;
│   │                                 DEC-STRUCTURE-CLEANUP-001)
│   ├── metrics/                    ← Sharpe/DSR/SPA/CPCV statistics
│   ├── notifications/
│   ├── observability/
│   ├── options/                    ← expiry/strike/lot-size rules (lake)
│   ├── orchestrator/               ← FSM + audit + rate limiter
│   ├── ranking/                    ← composite ranking + rejection gates
│   ├── strategies/                 ← 52-vocabulary engine (Phase 3)
│   └── vocabulary/                 ← 52-condition evaluator (→ trading_core)
├── rust/                           ← Rust hot kernels (shared core + compat)
│   ├── fno-math/                   ← compat types + NSE calendar; Greeks/
│   │                                 indicators removed in DEC-P0-025
│   └── trading-core/               ← SHARED 52-vocab + evaluate-time
│                                     indicators (pivots/fib); bit-identical
│                                     core for BruteX (PyO3 wheel) + TickVault
│                                     (native) — DEC-SHARED-CORE-001 #441
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── property/                   ← hypothesis + proptest (24 invariants)
│   ├── golden/                     ← byte-equality fixtures
│   ├── bench/                      ← O(1) ratio tests
│   ├── stress/                     ← 24h sustained
│   ├── smoke/                      ← <30s pre-push
│   └── fakes/                      ← test doubles
└── .github/workflows/
```

---

## 6. Verified Groww Facts (cite these, don't re-derive)

> **DEC-DHAN-GROWW-REMIGRATE-001 (2026-06-12, ADR-0009):** full
> re-migration of the Track-1 backtesting data source back to the
> **Groww Trade API** (`growwapi` Python SDK). This SUPERSEDES the
> ADR-0002 prior-vendor revert; the prior-vendor adapter package + its
> reference docs were removed in the ADR-0009 §5 cutover PR series. The
> facts below are repo-sourced: HARD-VERIFIED from
> `brutex/adapters/groww/constants.py` (and sibling modules) + the Groww
> SDK reference at `docs/groww/api-reference/`. See `docs/adr/` ADR-0009
> for the migration rationale + the accepted-risk register. Rows whose value is an OPEN Phase-0 probe
> gate are stamped **UNVERIFIED** (ADR-0009 §3.2 / §4) — never encoded as
> fact in code until a probe transcript lands. Every row traces to
> file:line.

| Fact | Value | Source |
|------|-------|--------|
| Vendor / transport | **Groww Trade API — `growwapi` Python SDK** (injected `GrowwAPI` client; no raw HTTP) | `adr/0009-...-migration.md:43-44`; `groww/adapter.py:1-17` |
| Historical OHLC (spot + options) | `groww.get_historical_candles(exchange, segment, groww_symbol, start_time, end_time, candle_interval)` — single method for all instruments | `groww/api-reference/03-backtesting.md:138-200`; `groww/adapter.py:129` (`fetch_ohlcv` → `fetch_candle_windows`) |
| F&O candle lake (`/db` logical table) | **`fno_candles`** — the FUTURES + OPTIONS historical OHLC the pull ALREADY writes to `bars/<exch>/FNO/<groww_symbol>/<interval>/<yyyy>/<mm>.parquet` (shared `ParquetBarSink`; OHLCV + `open_interest` first-class non-null; 2-dp tick grid). NOT a separate store — the SAME lake as spot with `segment="FNO"`; FORMALIZED as a first-class `/db` row (decoded per contract via `fno_symbol.parse_fno_symbol`) + registered in `schema_meta` (`fno_candle_lake.*`) — DEC-FNO-CANDLE-LAKE-001 #861 | `data/fno_candle_lake.py`; `data/fno_symbol.py`; `dashboard/db_view.py` (`list_fno_candle_datasets`); `adapters/groww/option_ingest.py` (`sink.append(segment="FNO", …)`) |
| Options contract resolution | `groww.get_expiries(...)` → `groww.get_contracts(...)` → feed each Groww symbol to `get_historical_candles` (all listed strikes, not relative-to-spot), **`1minute` ONLY** with a DEFAULT window SPLIT by contract type — FUTURES `expiry − 3 calendar months → expiry`, OPTIONS `expiry − 30 days → expiry` (explicit operator dates override either; `1day` DERIVED from the pulled 1-minute) — DEC-1MINUTE-ONLY-DERIVE-1DAY-001 #853 + DEC-FNO-LOOKBACK-SPLIT-001 #894 | `groww/api-reference/03-backtesting.md:41-134`; `adapters/groww/contract_bounds.py` (`default_fno_minute_window`, `DEFAULT_OPTION_LOOKBACK_DAYS`); `dashboard/historical_pull.py` |
| Backtesting segments | `CASH`, `FNO` ONLY (`COMMODITY` excluded; indices ride the `CASH`/`IDX` row, mapped to the neutral `INDEX` segment) | `groww/constants.py:91-95`; `groww/instruments.py:124-136`; `groww/api-reference/03-backtesting.md:8` |
| Instrument types | `EQ, IDX, FUT, CE, PE` | `groww/constants.py:95`; `groww/api-reference/04-annexures.md:100-108` |
| Historical depth | **"available from 2020"** (Equities, Indices, FNO) | `groww/constants.py:106-108`; `groww/api-reference/03-backtesting.md:145` |
| 1-minute granularity at the 2020 boundary | **UNVERIFIED** — Phase-0 probe gate (rows may not truly start Jan-2020 at 1m) | `groww/constants.py:107`; `adr/0009-...-migration.md:113-114` |
| Candle intervals (SDK supports) | `1/2/3/5/10/15/30 minute`, `1hour`, `4hour`, `1day`, `1week`, `1month` | `groww/constants.py:57-70`; `groww/api-reference/04-annexures.md:85-98` |
| Candle interval — Track 1 spot | **`1minute` ONLY fetched** (DEC-1MINUTE-ONLY-DERIVE-1DAY-001 #853 — `resolve_fetch_intervals` always returns `("1minute",)`); `1day` (PDH/PDL anchor) + the 19 intraday TFs are DERIVED from the stored 1-minute (`1day` resampled midnight-IST to the lake's 1-day partition after every pull, `spot_daily_resample.derive_daily_for_targets`), never separately vendor-fetched. The granularity dropdown + the bhavcopy cross-check of DEC-PULL-1DAY-FIRST-001 (#723) are REMOVED | `dashboard/pull_controller.py` (`GROWW_FETCH_INTERVALS`, `resolve_fetch_intervals`, `_resample_daily_after_pull`); `data/spot_daily_resample.py` |
| Chunk window — by interval tier | **1/2/3/5m → 30d · 10/15/30m → 90d · 1h+/1d/1w/1mo → 180d** per call (richer than a single split) | `groww/constants.py:76-89`; `groww/chunks.py:34-60`; `groww/api-reference/03-backtesting.md:238-246` |
| Candle response shape | **ROW-arrays** — `{"candles": [[ts, open, high, low, close, volume, open_interest], ...]}`; OI `null` for non-FNO | `groww/parser.py:1-18,104-124`; `groww/api-reference/03-backtesting.md:202-236` |
| **Price unit** | **RUPEES (float)** from Groww — kept as exact `Decimal` rupees through the lake (Parquet `Float64`); int64 paisa is the cost/display unit only, NOT price storage (DEC-PAISA-DOC-RECONCILE-001) | `data/candle_fields.py` (`to_price`→`Decimal`); `data/models.py:142-144`; `groww/api-reference/03-backtesting.md:202` |
| Timestamp unit | **Native-IST string** `"yyyy-MM-ddTHH:mm:ss"` (or epoch seconds defensively) — localized to IST (fixed +05:30, no DST), stored UTC, displayed IST | `groww/parser.py:33-67`; `groww/constants.py:109-110`; `groww/api-reference/03-backtesting.md:198-207` |
| Symbol/key format | **`groww_symbol` string** `EXCH-SYMBOL[-…]` (e.g. `NSE-NIFTY`, `BSE-SENSEX`) — O(1) resolution: a BARE trading symbol (incl. a hyphenated stock like `BAJAJ-AUTO` / a verbose-symbol index like `MIDCPNIFTY`) resolves via `by_exchange_symbol` + the master's `groww_symbol_by_pair` (the real groww_symbol, never reconstructed); an already-prefixed FUT/CE/PE contract resolves via `by_groww_symbol`. NOT a numeric ID (DEC-GROWW-HYPHEN-SYMBOL-RESOLVE-001, MIGRATION.md entry #845) | `groww/instruments.py:190-201`; `groww/adapter.py:162-174`; `groww/api-reference/02-instruments.md:119`, `03-backtesting.md:10-37` |
| Options instrument type | **`CE` / `PE`** (Call / Put); `FUT` for futures | `groww/constants.py:95`; `groww/api-reference/03-backtesting.md:16-22`; `04-annexures.md:107-108` |
| Exchanges (in scope) | **NSE, BSE** (MCX excluded with the COMMODITY segment) | `groww/constants.py:92`; `groww/api-reference/04-annexures.md:39-43` |
| Rate limit — backtesting | **500 req/min, NO daily quota** (operator-confirmed 2026-06-13, NOT vendor-documented; DEC-GROWW-RATE-PERMIN-001). The per-SECOND cap stays **UNVERIFIED** (`get_historical_candles` r/s UNDOCUMENTED; Phase-0 probe gate); the production per-second ceiling injected at both live build sites is the operator-DIRECTED **5 r/s** (`GROWW_RATE_LIMIT_RPS_CEILING=5.0`, operator-approved 2026-06-22, LOWERED 8 → 5, DEC-GROWW-RATE-CEILING-5-001 supersedes DEC-GROWW-RATE-CEILING-8-001 — 5×60 = 300/min stays well under the 500/min governor; a live-log audit showed the vendor 429s even at 3–5 r/s so the 5–8 headroom only wasted 429-retries; NOT a vendor-verified limit). The published "Live Data" group is 10 r/s · 300/min but is NOT confirmed to cover backtesting | `groww/constants.py:112-118,165` (`GROWW_RATE_LIMIT_RPS_UNVERIFIED=10.0`, `GROWW_RATE_LIMIT_PER_MIN=500`, `GROWW_RATE_LIMIT_RPS_CEILING=5.0`, `GROWW_DAILY_QUOTA_UNVERIFIED=None`); `groww/api-reference/01-introduction.md:130-134` |
| Adapter effective cap | **Vendor-neutral adaptive token bucket** — injected `ceiling_rps` (the production live ceiling is the operator-DIRECTED **5 r/s**, `GROWW_RATE_LIMIT_RPS_CEILING`, DEC-GROWW-RATE-CEILING-5-001; the vendor-neutral DEFAULT stays 5), steps down to a 1 r/s floor on 429/timeout and recovers to the ceiling once clear — PLUS an injected `ceiling_per_min` HARD 500/min rolling-window governor on top (DEC-GROWW-RATE-PERMIN-001) | `adapters/rate_common.py:136-198`; `groww/constants.py:165`; `dashboard/pull_controller.py:776`; `cli.py:2020` + this file §2 rule 6 |
| Auth | **TOTP flow** — `GrowwAPI.get_access_token(api_key, totp=pyotp.TOTP(secret).now())`; creds via `BRUTEX_GROWW_API_KEY` / `BRUTEX_GROWW_TOTP_SECRET`. The API-key+secret flow is REFUSED (daily manual approval violates §11 AUTOMATED) | `groww/auth.py:28-31,94-114`; `groww/api-reference/01-introduction.md:34-96` |
| Instrument master URL | `https://growwapi-assets.groww.in/instruments/instrument.csv` (single CSV; SSRF-allowlisted host) | `groww/constants.py:24-26`; `groww/api-reference/02-instruments.md:8` |
| Instrument master schema | **21 columns** in fixed order; keys `exchange, exchange_token, trading_symbol, groww_symbol, name, instrument_type, segment, series, isin, underlying_symbol, …, lot_size, tick_size, freeze_quantity` | `groww/constants.py:28-53`; `groww/api-reference/02-instruments.md:112-134` |
| Exception hierarchy | `GrowwError, GrowwBadRequestError, GrowwAuthError, GrowwNotFoundError, GrowwRateLimitError, GrowwTimeoutError, GrowwFeedError, GrowwEmptyResponseError` + `is_retryable()` (maps the SDK's `GrowwBaseException`/`GrowwAPIException` tree by class name; an empty/non-JSON body `json.JSONDecodeError` → **retryable** `GrowwEmptyResponseError`, so a transient throttle is re-pulled instead of a terminal "not on Groww" quarantine — DEC-POSTPULL-DATAQUALITY-BATCH-001 #854) | `groww/errors.py`; `groww/api-reference/05-exceptions.md:9-70` |
| F&O-eligible stock detection | EQUITY rows whose `underlying_symbol` appears in the FNO contract set; **212** single stocks verified in the operator instrument master (lake scope) | `groww/constants.py:103-104`; `groww/instruments.py:174` |
| NIFTY Total Market lake admission | **REFERENCE_ONLY** — ~750 NSE constituents (large+mid+small+micro) fetched best-effort from niftyindices, matched to Groww NSE cash-equity rows by ISIN / ticker; the ~540 non-F&O matches are admitted reference-only (the ~212 F&O names stay tradable). The fetch is **cache-on-success + bounded-retry + cache-fallback** (live → last-good cache → honest F&O-base degrade), so a transient niftyindices 404 falls back to the cached ~752 instead of collapsing the universe to the ~211 F&O base (this cache/retry predates the batch; regression-pinned in DEC-POSTPULL-DATAQUALITY-BATCH-001 #854). **BRUTE-FORCE engine UNCHANGED** — lake/reference only, never in the 52-vocab | `dashboard/instrument_loader.py` `_ntm_admission` (DEC-NTM-GROWW-REPOINT-001); `universe_constituents.py` `resolve_constituents` |
| Track 1 brute-force universe | **`NSE-NIFTY`, `NSE-BANKNIFTY`, `BSE-SENSEX`** (all `IDX`, mapped to the neutral `INDEX` segment); resolved master-authoritatively via `by_exchange_symbol` + `groww_symbol_by_pair` (the bare trading symbol → the real groww_symbol, DEC-GROWW-HYPHEN-SYMBOL-RESOLVE-001). (LAKE option-pullability for F&O indices/stocks keys on a vendor-canonical `underlying_security_id` token-join (numeric `feed_token`) + a sourced NSE-index alias `NIFTYJR`→`NIFTYNXT50` / `NIFTYMIDSELECT`→`MIDCPNIFTY` (env-extensible `BRUTEX_FNO_INDEX_ALIASES`), with a `universe.load.opt_reclassified` audit — DEC-POSTPULL-DATAQUALITY-BATCH-001 #854; the ENGINE still the 3 indexes) | `groww/constants.py:97-100`; `groww/instruments.py:124-136`; `groww/adapter.py:162-174` |
| INDIA VIX symbol | **`NSE-INDIAVIX`** — reference-only (`buy/sell_allowed=0`), never in the 52-vocab | `groww/constants.py:101-102` |
| INDIA VIX historical-candle availability | **UNVERIFIED** — Phase-0 HARD-GATE; if absent on Groww the migration is rescinded before cutover | `adr/0009-...-migration.md:90,111-112` |
| Token lifetime + backtest-vs-live drift | **UNVERIFIED** — Phase-0 probe gates (unattended TOTP token lifetime; prior-vendor↔Groww 1m bar drift) | `adr/0009-...-migration.md:88-92,113-124` |

## 7. Verified Indian Market Microstructure Facts

| Fact | Value |
|------|-------|
| Trading hours | 09:15 - 15:30 IST (NSE + BSE equity) |
| Pre-open auction | 09:00 - 09:15 IST (exclude from backtest bars) |
| Spot index ticks from | 09:15:00 IST (post-auction opening) |
| Track 1 forced exit | 15:20 IST |
| Muhurat session (Diwali) | 1 hour per year; date list in `TRACK1_MICROSTRUCTURE.md` |
| NIFTY 50 rebalance | Semi-annual (March, September) — ~10 events in 2021-2026 |
| Stamp duty (equity intraday) | 0.003% buy side (signal-only mode: NOT applied) |
| Brokerage assumption (Track 1) | **NONE** — spot is not tradable |

---

## 8. Hardware Envelope (for honest SLA claims)

**Track 1 reference:** Apple M4 Pro, 14-core CPU (10P + 4E), 48 GB RAM,
8 TB external SSD. Daily apps run concurrently (10 cores used by sweep,
2 reserved).

**Production sweep SLA:** **2-4 days wall time** for ~33 billion trials
on this envelope, after Stage 1A → 1B → 2 → 3 → 4 pipeline. Public
benchmarks (vectorbt, NautilusTrader, Jesse) constrain the upper bound;
see `docs/TRACK1_PERFORMANCE_BUDGET.md` for the full memory-bandwidth
analysis.

Larger machines (64-128 core EPYC / Threadripper) scale linearly and
bring sweep to 12-24 hours; the spec MUST state SLA per hardware
envelope.

---

## 9. When in Doubt

1. Re-read `docs/TRACK1_BACKTEST_SPEC.md`.
2. If answer not there → ask the user with `AskUserQuestion`.
3. Never guess on scope. Never expand scope silently.
4. Never propose "improvements" outside the requested task.
5. Before major design choices, spawn a 4-agent hostile review (see
   `docs/TRACK1_PHASE_PLAN.md §0` for the pattern).

---

## 10. Session Etiquette

- Start of every session: read this file + `TRACK1_BACKTEST_SPEC.md` +
  open the locked-decisions log in `docs/MIGRATION.md`.
- Use TodoWrite for any multi-step task.
- Split long writes into multiple Write tool calls (avoid stream idle
  timeouts on docs >300 lines).
- Report honestly on failures; do not paper over errors.
- After any major refactor: run `ruff check brutex && mypy --strict
  brutex && pytest -q` before considering the change complete.
