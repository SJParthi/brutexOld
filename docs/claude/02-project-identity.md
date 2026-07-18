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
     and written to the lake's 1-day partition by the pull pipeline's automatic
     FINAL prebuild stage — no longer mid-pull (DEC-PIPELINE-AUTOPREBUILD-001,
     MIGRATION.md #1145; `_resample_daily_after_pull` removed). This supersedes
     the operator-chosen granularity dropdown of DEC-PULL-1DAY-FIRST-001 (#723,
     removed) and re-introduces the 1m→1d derivation removed by
     DEC-REALDATA-ONLY-NO-1M-TO-1D-001 (#852) — now as the always-on PRIMARY
     path, since 1-minute is the sole fetched granularity (the derived 1-day is
     openly labelled derived, not vendor-official; Golden Rule #1).

  Expired options + futures (Options Track / lake) use the same
  `get_historical_candles` after resolving contracts via
  `groww.get_expiries(...)` → `groww.get_contracts(...)`, also at **`1minute`
  ONLY** with a DEFAULT window SPLIT by contract type — the STRICT law
  `[expiry − lookback → expiry] ∩ envelope`: FUTURES `expiry −
  60 days → expiry` (DEC-FUTURES-LOOKBACK-60D-001, MIGRATION.md #1003 —
  supersedes the 3-calendar-month default of DEC-FNO-LOOKBACK-SPLIT-001 #894),
  OPTIONS `expiry − 30 days → expiry` (explicit operator dates
  override either default; a NOT-YET-OPEN far-future contract — `expiry −
  lookback` on/after the envelope end — is SKIPPED entirely, zero vendor
  calls + zero ledger rows, logged `fno.window_not_open`, until the calendar
  reaches `expiry − lookback`; never a substitute window and never the full
  2020→today range — DEC-FNO-FAR-EXPIRY-WINDOW-001, MIGRATION.md #1127);
  the F&O `1day` is likewise
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
