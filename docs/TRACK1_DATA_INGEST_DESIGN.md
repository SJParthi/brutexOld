# Track 1 — Data Ingest Design

> **Status:** Locked 2026-05-15 after a 4-agent verification pass.
> Companion to `TRACK1_BACKTEST_SPEC.md`, `TRACK1_PHASE_PLAN.md`.
> Implemented in Phase 2 of the build plan (Groww Adapter + Vocabulary).
>
> Verified by:
> - Agent A: India VIX availability + identifiers (from Groww master CSV)
> - Agent B: existing storage code audit (`/brutex/data/*.py`)
> - Agent C: incremental pipeline + state machine design
> - Agent D: observability + audit layer design

This document defines the end-to-end data ingest pipeline for Track 1:
fetch raw OHLCV bars from Groww, resample to 21 timeframes, persist to
a Parquet+DuckDB+LMDB lake with full O(1) guarantees, hash-chained
audit, and structlog-based observability.

---

## 1. Scope (after 2026-05-15 unfreeze; **AMENDED 2026-05-22 per DEC-P0-063**)

### 1.1 Core 4-instrument scope (UNCHANGED since 2026-05-16)

| Aspect | Track 1 Value |
|---|---|
| **Total instruments fetched (core)** | 4: NIFTY 50, BANKNIFTY, SENSEX + India VIX |
| **Brute-forced (in vocabulary)** | 3: NIFTY 50, BANKNIFTY, SENSEX |
| **Reference-only** | 1: **India VIX** (context columns only, never in patterns) |
| **Range** | Rolling: `2021-01-01 → today` |
| **Native fetched** | 1-min (intraday) + 1-day (historical) from Groww |
| **Resampled** | 19 derived intraday TFs from 1m base |
| **Total TFs persisted** | **21** (= 1 base + 19 derived + 1 daily) per instrument |
| **Tradability** | None (signal-only research) |
| **Brokerage/STT** | NONE (spot indexes don't trade) |
| **Groww Data API rate limit** | **5 req/s + 100,000/day** (verified) |
| **Adapter effective cap** | 3 r/s (5 × 0.6 safety factor) |

### 1.2 LAKE expansion (DEC-P0-063, 2026-05-22, MIGRATION.md entry #200)

The LAKE additionally covers **200 NSE F&O eligible single stocks (spot + options across all 4 Groww `expiryCodes`)**. The 200 stocks join India VIX in `REFERENCE_ONLY` (mirrors decision #83 pattern). Stock options sit in the lake ready for future brute-force expansion (separate decision; not authorized here). **The BRUTE-FORCE ENGINE is UNCHANGED** — it still operates only on the 3 brute-force indexes (NIFTY/BANKNIFTY/SENSEX); §§2-12 below describe the 4-instrument core surface that drove the original ingest design. The 200-stock expansion uses the same instrument-store / partition / dedup / audit primitives without re-architecting (per CLAUDE.md §11 "DYNAMIC" + "SCALABLE" rules). See CLAUDE.md §3 + MIGRATION.md entry #200 + `docs/TRACK1_BACKTEST_SPEC.md §"Scope Lock"` (amended per DEC-SPEC-CONSISTENCY-002, MIGRATION entry #264) for the full locked wording.

**Unfrozen from prior spec:**
- Decision #2 (3 instruments → 4)
- Decision #7 (15 TFs → 21 TFs)
- Date range (fixed window → rolling)

See `MIGRATION.md §6` for the ledger entries.

---

## 2. The 4 Instruments — Verified from Groww Master CSV

All 4 share the same code path (same endpoints, same segment family,
same chunking). Adding a 5th later = 1 line in
`brutex/config/instruments.toml`.

| # | Trading Symbol | Display Name | Exchange | Segment | Instrument Type | Spot Live Since | Verified |
|---|---|---|---|---|---|---|---|
| 1 | `NIFTY 50` | NIFTY 50 | NSE | `IDX_I` | `INDEX` | 1996 | ✅ |
| 2 | `NIFTY BANK` | BANKNIFTY | NSE | `IDX_I` | `INDEX` | 2003 | ✅ |
| 3 | `SENSEX` | BSE SENSEX | BSE | `IDX_I` (single Index segment per Groww annexure) | `INDEX` | 1986 | ✅ |
| 4 | `INDIA VIX` | India VIX | NSE | `IDX_I` | `INDEX` | 2008 | ✅ |

### India VIX specifics (Agent A research)

| Attribute | Value | Source |
|---|---|---|
| `SEM_TRADING_SYMBOL` (master CSV) | `INDIA VIX` (with space) | marketcalls/ExpiryFlow mirror of Groww CSV |
| `SEM_CUSTOM_SYMBOL` | `India VIX` | same |
| `SEM_INSTRUMENT_NAME` | `INDEX` | same |
| `securityId` | `21` (lookup-at-runtime; one community source said `26`) | TradeHull/Groww-Tradehull, ysrastogi/trado |
| `exchangeSegment` (API) | `IDX_I` | groww-oss/Groww-py official SDK |
| Update cadence | every **15 seconds** (NOT tick-by-tick) | NSE official |
| Calculated from | NIFTY 50 option mid-quotes | NSE / Business Standard |
| BSE equivalent | NONE — NSE only | confirmed |
| Volume | always zero (computed index) | confirmed |
| Trading hours | 09:15-15:30 IST (same as NIFTY) | NSE official |

**1m OHLC caveat:** Because VIX updates every 15s, each 1m bar contains
~4 underlying samples. The H/L within a 1m bar therefore has lower
"intra-bar resolution" than NIFTY. Irrelevant for Track 1's 1m+
resampled timeframes.

**Phase 2 implementation:** add one row to `V1_INDICES`. No new code paths.

---

## 3. The 21 Timeframes

### 3.1 Native (fetched directly)

| TF | Endpoint | Chunk size |
|---|---|---|
| `1m` | `POST /v2/charts/intraday` (`interval=1`) | 90-day windows |
| `1d` | `POST /v2/charts/historical` | full range in 1 call |

### 3.2 Derived (resampled from 1m, single pass)

**Group A — 1-minute increments through 15m (14 TFs):**
`2m, 3m, 4m, 5m, 6m, 7m, 8m, 9m, 10m, 11m, 12m, 13m, 14m, 15m`

**Group B — Longer windows (5 TFs):**
`30m, 60m, 120m, 180m, 240m`

**Total: 21 TFs** = 1 base (1m) + 14 (Group A) + 5 (Group B) + 1 daily.

### 3.3 Why not Groww's native 5/15/60-minute intervals?

Groww natively supports 1, 5, 15, 25, 60 minute and 1-day. Track 1
fetches **only 1m** and resamples to all derived TFs from the 1m base.
This guarantees:

- Single deterministic resampling rule (P-08: `resample(resample(x,2),3) ≡ resample(x,6)`)
- Single reproducibility hash (data_version_hash covers one source TF)
- No mixing of "native" vs "resampled" semantics across TFs

The 25-minute native is dropped (no equivalent derived TF in our 21).

**Data-quality rationale — why 1m is the ONLY trustable source (operator,
2026-05-31; DEC-CANONICAL-1M-SOURCE-001).** Beyond reproducibility, there is a
vendor-trust reason this rule is non-negotiable. Groww's historical candles are
assembled from its **own broker-level executed-trade tape** — they are NOT
back-filled or reconciled against the NSE/BSE-direct consolidated feed, and they
are NOT back-adjusted (contrast Dhan, which reconstructs every minute from the
official exchange feed). Empirically the **1-minute** bar (the ~09:00→close
intraday series) is the one timeframe we can treat as a faithful record of what
printed; Groww's NATIVE higher timeframes (5/15/25/60-min) are that same
imperfect tape re-aggregated by Groww with alignment/timestamp semantics we
cannot verify. So Track 1 trusts **only 1m** and builds every other TF itself,
bit-for-bit. (Honest envelope, CLAUDE.md §6 + Golden Rule #1: the exact
Groww-vs-NSE drift is still **UNQUANTIFIED** — the Phase 0b drift probe measures
it; the broker-tape characterisation is the operator's domain knowledge, not a
Groww-published fact. The 3 brute-force INDEXES are index-dissemination values,
not traded prints, so the tape concern bites hardest on the single
stocks/options, where illiquid-minute null/zero bars are already skipped per
DEC-CANDLE-EMPTY-BAR-001 / DEC-CANDLE-PARSE-RESILIENCE-001.)

> **Implementation status (drift to fix):** the live pull currently fetches
> Groww's **12 native intervals** (`ALL_GROWW_INTERVALS` in
> `brutex/adapters/groww/chunks.py` = `1m,2m,3m,5m,10m,15m,30m,1h,4h,1d,1w,1mo`)
> directly — **violating this spec**. The resampler already exists
> (`brutex/feed/resampler.py`, `brutex/data/tf_resampler.py`); the corrective
> change (ingest **1m-only** + resample the rest, plus a 1m gap/completeness
> guard against the session grid) is tracked under DEC-CANONICAL-1M-SOURCE-001
> as the authorised follow-up — no code in the entry that records this decision.

### 3.4 Resampling rules (from `TRACK1_BACKTEST_SPEC.md §5.3`)

| Rule | Value |
|---|---|
| Anchor origin | 09:15:00 IST (every trading day) |
| Aggregate functions | `open = first`, `high = max`, `low = min`, `close = last`, `volume = sum` (always 0 for indexes) |
| Partial bars at session close | DROPPED — e.g. the 240m bar from 13:15-17:15 IST never completes (15:30 close), so it's not emitted |
| Storage | one Parquet file per `(instrument, tf, year, month)` |

Property test P-08 (associative resampling) lives in the verification
pyramid and asserts this holds for every (n, m) pair where n × m matches
a target TF.

---

## 4. Storage Architecture — 3 Backends

| Backend | What | Why | Primary Key | O(1)? |
|---|---|---|---|---|
| **Parquet** files | OHLCV bars (raw + resampled) | columnar compression, polars/Rust zero-copy, mmap-friendly | partition path = `(exch, seg, sym, tf, year, month)` | O(1) per partition |
| **DuckDB** (`catalog.duckdb`) | Partition metadata + watermarks | SQL, ACID, small footprint | `(exch, seg, sym, tf, year, month)` for partitions; `(inst, tf)` for watermarks | O(1) PK |
| **LMDB** (`work_items/`) | Chunk dedup index | mmap'd B+tree, microsecond lookup | `sha256(inst‖tf‖window)` | O(1) Bloom + B+tree |

### 4.1 Directory layout

```
/Volumes/QuantData/brutex-lake/
  ├── bars/
  │   ├── NSE/
  │   │   └── IDX/
  │   │       ├── NIFTY/         ← display "NIFTY 50"
  │   │       │   ├── 1m/2021/01.parquet, …, 2026/05.parquet
  │   │       │   ├── 2m/…       ← resampled
  │   │       │   ├── …
  │   │       │   ├── 240m/…
  │   │       │   └── 1d/all.parquet
  │   │       ├── BANKNIFTY/     (same)
  │   │       └── INDIAVIX/      (same)
  │   └── BSE/
  │       └── IDX/
  │           └── SENSEX/        (same)
  ├── catalog.duckdb             ← partitions + watermarks
  ├── lmdb/work_items/            ← dedup index
  └── audit/
      ├── audit.jsonl            ← hash-chained event log
      └── index.duckdb           ← run_id → file offsets
```

### 4.2 Watermark table (new — Phase 2 deliverable)

```sql
CREATE TABLE watermarks (
    instrument_id   VARCHAR NOT NULL,
    timeframe       VARCHAR NOT NULL,
    last_bar_ts     TIMESTAMP WITH TIME ZONE NOT NULL,
    last_bar_hash   VARCHAR(64) NOT NULL,
    chunk_id        VARCHAR(64) NOT NULL,
    bars_written    BIGINT NOT NULL,
    updated_at      TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (instrument_id, timeframe)
);
CREATE INDEX ix_wm_inst ON watermarks(instrument_id);
-- Total rows: 4 instruments × 21 TFs = 84
```

### 4.3 Bar identity hash (dedup proof)

```
bar_hash = sha256(
    instrument_id        ‖  "NSE:NIFTY"
    timeframe            ‖  "1m"
    ts_utc_int64_us      ‖  1747333215000000
    open_paisa_i64       ‖
    high_paisa_i64       ‖
    low_paisa_i64        ‖
    close_paisa_i64      ‖
    volume_i64           ‖  0 (always for indexes)
    data_source_version     "groww-v2-2026-05-15"
)
```

**Property:** same bar from Groww twice → same hash. Reading + re-writing
the same chunk produces bit-identical Parquet bytes (P-19).

---

## 5. Pipeline State Machine

```
                  ┌───────────────────────────────────────┐
                  │  Daily cron OR `brutex pull` manual   │
                  ▼                                       │
              ┌────────┐  read config + watermarks        │
       ┌─────▶│  IDLE  │──────────────────────────┐       │
       │      └────────┘                          ▼       │
       │           ▲                       ┌─────────────┐│
       │  audit OK │                       │  PLAN_GAPS  ││
       │           │                       └──────┬──────┘│
       │      ┌─────────┐                         │       │
       │      │AUDIT_DONE│  ◀─────  chain ─────────┤       │
       │      └─────────┘   verify                │       │
       │           ▲                              │       │
       │           │                              ▼       │
       │      ┌─────────┐                  ┌──────────────┐│
       │      │ AUDIT   │ ◀─── all done ──│  FETCH_CHUNK │◀┘
       │      └─────────┘                  └──────┬───────┘
       │           ▲                              │
       │           │ resample OK                  ▼
       │      ┌──────────┐                  ┌──────────────┐
       │      │ RESAMPLE │◀── 1m landed ───│  validate    │
       │      │  CASCADE │                  │  + write     │
       │      └──────────┘                  │  + watermark │
       │           │                        └──────────────┘
       │           ▼ on error
       │      ┌─────────┐
       └──────│ HALTED  │ ◀── any unrecoverable failure
              └─────────┘
                  │ operator `brutex resume`
                  ▼
              re-enter PLAN_GAPS (watermarks resume work)
```

### 5.1 Transitions

| From → To | Trigger | Audit event | Rollback semantics |
|---|---|---|---|
| IDLE → PLAN_GAPS | cron tick / manual | `RUN_START` | n/a |
| PLAN_GAPS → FETCH_CHUNK | gaps ≠ ∅ | `PLAN_GAPS` | n/a |
| PLAN_GAPS → AUDIT_DONE | gaps = ∅ (no-op) | `RUN_COMPLETE noop` | n/a |
| FETCH_CHUNK → (self) | next chunk in window | `CHUNK_START`/`CHUNK_DONE` | discard buffer; watermark NOT advanced |
| FETCH_CHUNK → RESAMPLE_CASCADE | all 1m chunks for instrument written | `PHASE_TRANSITION` | leave 1m landed; resample is idempotent |
| RESAMPLE_CASCADE → AUDIT | all 19 derived TFs produced | `RESAMPLE_DONE` | re-derive on restart |
| AUDIT → AUDIT_DONE | `verify_chain()` OK | `AUDIT_VERIFY` + `RUN_COMPLETE` | n/a |
| any → HALTED | rate-limit exhaust, validation fail, audit-break | `RUN_HALTED` | watermark unchanged |
| HALTED → PLAN_GAPS | operator `brutex resume` | `RUN_RESUME` | n/a |

---

## 5A. Authentication (Manual Paste + Auto-Renew)

Track 1 uses Groww's **individual trader** access token flow — simplest
possible first-run experience, automated for the steady state.

### 5A.1 Token storage

File: `~/.brutex/groww/access_token.json` (gitignored)

```json
{
  "client_id":      "1000000401",
  "access_token":   "eyJ0eXAi…",
  "expiry_time":    "2026-05-16T15:00:00.000Z",
  "last_renewed_at":"2026-05-15T15:00:00Z"
}
```

Path resolution order (Phase 2 adapter):

1. `$BRUTEX_GROWW_TOKEN_PATH` if set
2. `~/.brutex/groww/access_token.json` (default)
3. ERROR + helpful setup instructions if neither exists

### 5A.2 First-time setup (manual, once)

```
   ┌──────────────────────────────────────────────────────────────┐
   │  1. Login to web.groww.co                                     │
   │  2. My Profile → Access Groww APIs                          │
   │  3. Generate Access Token (24h validity)                     │
   │  4. Copy Client ID + Access Token + Expiry Time              │
   │  5. Paste into ~/.brutex/groww/access_token.json              │
   │  6. Run `brutex pull` — adapter reads + uses                 │
   └──────────────────────────────────────────────────────────────┘
```

### 5A.3 Auto-renewal flow

```
   ┌──────────────────────────────────────────────────────────────┐
   │                                                              │
   │   On every adapter startup                                   │
   │   AND once per ~22 hours of run-time                         │
   │   AND just before every API call if expiry < 1 hour:         │
   │                                                              │
   │       │                                                      │
   │       ▼                                                      │
   │   ┌─────────────────────────────────────────┐                │
   │   │ Read token from JSON                    │                │
   │   └────────────────┬────────────────────────┘                │
   │                    ▼                                         │
   │   ┌─────────────────────────────────────────┐                │
   │   │ Is expiry > 25 min from now?            │                │
   │   └─────────┬────────────────────┬──────────┘                │
   │             │ YES                │ NO                        │
   │             ▼                    ▼                           │
   │       Use as-is        POST /v2/RenewToken                   │
   │                        with current token                    │
   │                            │                                 │
   │                            ▼                                 │
   │                    ┌───────────────────┐                     │
   │                    │ HTTP 200?         │                     │
   │                    └──┬──────────┬─────┘                     │
   │                       │ YES      │ NO                        │
   │                       ▼          ▼                           │
   │              Write new token   structlog CRITICAL            │
   │              atomically        auth.token_expired            │
   │              (tempfile+rename) FSM → HALTED                  │
   │                                Telegram (when wired)         │
   │                                                              │
   └──────────────────────────────────────────────────────────────┘
```

### 5A.4 Renewal API call (verified from Groww docs)

```
POST https://api.groww.co/v2/RenewToken
Headers:
  access-token: {current JWT}
  growwClientId: {client_id}
Body: (none)

Response 200:
{
  "growwClientId":   "1000000401",
  "growwClientName": "JOHN DOE",
  "growwClientUcc":  "ABCD12345E",
  "accessToken":    "<new JWT>",
  "expiryTime":     "2026-05-17T15:00:00.000"
}

Response error:
- Token already expired → `807` "Access token expired" OR `DH-901` "Invalid Authentication" → halt + alert
  (Note: `DH-907` is "Data Error" for missing-data windows, NOT for token expiry — corrected by DEC-P0-028.)
- Auth fail → DH-901 / 808 → halt + alert
```

### 5A.5 Pseudocode

```python
# brutex/adapters/groww/auth.py (Phase 2 deliverable)

@dataclass(frozen=True, slots=True)
class GrowwCredentials:
    client_id: str
    access_token: str
    expiry_time: datetime  # UTC
    last_renewed_at: datetime

def load_credentials(path: Path = DEFAULT_TOKEN_PATH) -> GrowwCredentials:
    """Read credentials from JSON. Raises if file missing or malformed."""
    if not path.exists():
        raise AuthConfigError(
            f"No Groww credentials at {path}. "
            f"Run `brutex auth setup` for instructions."
        )
    data = json.loads(path.read_text())
    return GrowwCredentials(
        client_id=data["client_id"],
        access_token=data["access_token"],
        expiry_time=parse_iso8601(data["expiry_time"]),
        last_renewed_at=parse_iso8601(data["last_renewed_at"]),
    )

def needs_renewal(creds: GrowwCredentials, *, threshold_minutes: int = 25) -> bool:
    """True if token expires within `threshold_minutes`."""
    return creds.expiry_time - utc_now() < timedelta(minutes=threshold_minutes)

def renew_token(creds: GrowwCredentials) -> GrowwCredentials:
    """Call /v2/RenewToken and return fresh credentials."""
    response = httpx.post(
        "https://api.groww.co/v2/RenewToken",
        headers={
            "access-token": creds.access_token,
            "growwClientId": creds.client_id,
        },
        timeout=10.0,
    )
    if response.status_code != 200:
        log.critical("auth.token_renewal_failed",
                     status=response.status_code, body=response.text[:500])
        raise AuthRenewalError(
            f"Renewal failed (HTTP {response.status_code}). "
            f"You must manually regenerate at web.groww.co."
        )
    body = response.json()
    return GrowwCredentials(
        client_id=body["growwClientId"],
        access_token=body["accessToken"],
        expiry_time=parse_iso8601(body["expiryTime"]),
        last_renewed_at=utc_now(),
    )

def persist_atomically(creds: GrowwCredentials, path: Path) -> None:
    """Tempfile + rename — never leaves a half-written file."""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({
        "client_id":      creds.client_id,
        "access_token":   creds.access_token,
        "expiry_time":    creds.expiry_time.isoformat(),
        "last_renewed_at":creds.last_renewed_at.isoformat(),
    }, indent=2))
    tmp.replace(path)  # atomic on POSIX
    audit.append("AUTH_RENEWED", payload={
        "new_expiry": creds.expiry_time.isoformat(),
        "renewed_at": creds.last_renewed_at.isoformat(),
    })

def get_or_renew(path: Path = DEFAULT_TOKEN_PATH) -> str:
    """Hot path — returns a fresh access token, renewing if needed."""
    creds = load_credentials(path)
    if needs_renewal(creds):
        creds = renew_token(creds)
        persist_atomically(creds, path)
    return creds.access_token
```

### 5A.6 Audit events

Renewal adds one event type to the chain:

| Event kind | Fired when | Payload |
|---|---|---|
| `AUTH_RENEWED` | Token successfully refreshed | new_expiry, renewed_at |
| `auth.token_renewal_failed` (structlog only, not audit) | Renewal API returned non-200 | status_code, body excerpt |
| `auth.token_expired` (CRITICAL) | Refresh attempted on expired token | last_known_expiry |

### 5A.7 Phase 2 deliverables (added to checklist)

| Item | File | Status |
|---|---|---|
| `GrowwCredentials` dataclass | `brutex/adapters/groww/auth.py` | ⏳ |
| `load_credentials()` + path resolution | `brutex/adapters/groww/auth.py` | ⏳ |
| `needs_renewal()` + `renew_token()` | `brutex/adapters/groww/auth.py` | ⏳ |
| `persist_atomically()` (tempfile + rename) | `brutex/adapters/groww/auth.py` | ⏳ |
| `get_or_renew()` hot-path helper | `brutex/adapters/groww/auth.py` | ⏳ |
| `brutex auth setup` CLI subcommand with setup instructions | `brutex/cli.py` | ⏳ |
| `brutex auth status` CLI subcommand (show expiry) | `brutex/cli.py` | ⏳ |
| Unit tests for renewal logic | `tests/unit/test_groww_auth.py` (new) | ⏳ |

---

## 6. Incremental Behaviour — Day 1 vs Day 5 vs Same-Day Rerun

| Run | Watermark before | Gap | Chunks fetched | Watermark after | Wall time |
|---|---|---|---|---|---|
| **Day 1** (first run, 2026-05-15) | ∅ (empty) | 5 years × 4 inst | 88 (1m, 90-day chunks) + 4 (1d) = 92 chunks | 2026-05-15 15:30 IST | ~5 min |
| **Day 5** (incremental, 2026-05-19) | 2026-05-15 15:30 | 4 trading days × ~375 bars | 1 chunk/inst × 4 = 4 chunks | 2026-05-19 15:30 IST | ~5 sec |
| **Same Day** rerun | 2026-05-19 15:30 | ∅ (no gap) | 0 (nothing to fetch) | unchanged | ~0.1 sec |

### 6.1 Worked example (first run vs incremental)

```
DAY 1 (FIRST RUN):                       DAY 5 (INCREMENTAL):

  watermark = ∅                            watermark = 2026-05-15 15:30 IST

   ████████████████████████ 5y              ▌ 4d  ← only fetch the gap

  → 92 chunks fetched (10 r/s cap)         → 4 chunks fetched
  → ~5 min wall                            → ~5 sec wall

  After 1m written:                        After 1m written:
    resample to 19 derived TFs                resample only the 4 new days × 19 TFs
    = 19 × 4 inst × 5y of partitions          = 19 × 4 inst × 4 days
```

### 6.2 Recovery matrix (kill at any point)

| Kill point | Persisted state | Restart behaviour |
|---|---|---|
| Mid-Parquet write | tempfile only (atomic rename not yet fired) | orphaned tempfile cleaned; rerun re-creates partition with same hash |
| Post-Parquet, pre-watermark | Parquet landed, watermark unchanged | idempotent writer detects matching hash → watermark advances |
| Post-watermark, pre-resample | watermark advanced for 1m only | resampler notices 1m watermark > derived watermark → replays affected days |
| Mid-resample | partial derived Parquet (no rename) | derived watermark unmoved → day re-derives identically |
| Audit chain break | refuses to write more | FSM → HALTED, structlog ERROR, operator must investigate |
| Power cut | filesystem consistent (tempfile-rename atomic) | as above — no data loss |

**Property:** ANY kill point + restart → byte-identical Parquet output.
Proven by the audit-log replay oracle test
(`tests/integration/test_replay_audit_log.py` — Phase 1 deliverable).

---

## 7. Dedup + Uniqueness — 4 Layers of Defense

| Layer | What it catches | How | Typical latency |
|---|---|---|---|
| **L1: Watermark check** | "Already fetched this window?" | DuckDB `WHERE inst=? AND tf=?` → if `last_ts ≥ window.end: skip` | ~10 µs |
| **L2: Bloom filter** | "Probably seen this chunk-hash?" | `sha256(inst‖tf‖window)` → Bloom probe | ~5 ns |
| **L3: LMDB confirm** | "Definitely seen this chunk?" (resolves Bloom FP) | LMDB B+tree get | ~50 µs |
| **L4: Per-bar bar_hash** | "Has Groww mutated this bar?" | compare new bar_hash vs stored bar_hash | O(1) per bar |

```
   L1 Watermark            L2 Bloom               L3 LMDB
   ┌─────────────┐        ┌──────────────┐      ┌──────────────┐
   │ Already     │ YES    │ Probably     │ YES  │ Definitely   │
   │ past this   │ ────▶  │ seen this    │────▶ │ seen this    │
   │ window?     │        │ chunk?       │      │ chunk?       │
   └──────┬──────┘        └──────┬───────┘      └──────┬───────┘
          │ NO                   │ NO                  │ NO
          ▼                      ▼                     ▼
       proceed              proceed               proceed
       (fetch)              (fetch)               (fetch + write)
```

**Uniqueness invariant:** at no point does the lake contain two rows
with the same `(instrument, timeframe, ts)`. Enforced by:

- File-level dedup at write (`parquet_writer.py:150` sorts + dedups by ts before writing)
- Watermark check before any fetch (L1 above)
- Property test P-21 (Phase 1): "every persisted (inst, tf) sequence has strictly monotonic ts"

---

## 8. O(1) Hot-Path Guarantees

CI gate (`tests/bench/`, Phase 1 deliverable): `p99(N=1M) / p99(N=1K) ≤ 1.1`
for each of the following. Failure = CI red.

| Hot path | Mechanism | Complexity | Absolute p99 target |
|---|---|---|---|
| Bar lookup by ts | Catalog routes to partition; polars binary-search on sorted u64 ts | O(1) per partition + O(log n) inside (n < 100k bars) ≈ O(1) | <1 µs at 1M |
| Watermark lookup | DuckDB PK index on `(inst, tf)` | O(1) | <100 µs |
| Chunk dedup pre-check | Bloom filter on **survivors-only** working set (5 GB / ~150M-1B post-threshold-filter survivors → 40-267 bits/elem → FPR ≈ 1e-12 to 0) | O(1) | <200 ns |
| Chunk dedup confirm | LMDB B+tree | O(1) | <100 µs |
| Bar dedup by hash | sha256 → 32-byte LMDB key | O(1) | <100 µs |
| Audit append | append-only fwrite | O(1) | <50 µs |
| Parquet write | tempfile + rename | O(n) per chunk (~37500 bars), constant per call | <5 ms per chunk |

---

## 9. Audit Chain — Tamper Detection

```
   audit/audit.jsonl
   ──────────────────────────────────────────────────────
   seq=1   {kind:"RUN_START",   prev_sha=GENESIS,    this_sha=a3f…}
              │                                       │
              └─────────────── feeds into ───────────▶┘
                                                      │
   seq=2   {kind:"CHUNK_START", prev_sha=a3f…,  this_sha=b2e…}
              │                                       │
              └─────────────── feeds into ───────────▶┘
                                                      │
   seq=3   {kind:"CHUNK_DONE",  prev_sha=b2e…,  this_sha=c91…}
              │
              ...
   ──────────────────────────────────────────────────────
   verify_chain() walks every line, recomputes this_sha,
   compares to next line's prev_sha. Any mismatch = TAMPER.
   ──────────────────────────────────────────────────────
```

### 9.1 Material events (one audit line each)

| Event kind | Fired when | Payload includes |
|---|---|---|
| `RUN_START` | FSM enters RUNNING | run_id, host, instruments, start_ts |
| `PLAN_GAPS` | gap computation done | gaps[] per (inst, tf), total_chunks |
| `CHUNK_START` | before Groww POST | run_id, inst, tf, window |
| `CHUNK_DONE` | after Parquet write + watermark advance | run_id, inst, tf, bars, first/last_ts, chunk_sha |
| `CHUNK_FAIL` | rate-limit exhaust or validation fail | run_id, error, retries |
| `WRITE_DONE` | per partition file written | path, raw_hash, row_count |
| `RESAMPLE_DONE` | per (inst, day) resample finished | inst, day, tfs[] |
| `WATERMARK_ADVANCED` | watermark row UPSERT | inst, tf, new_ts, new_hash |
| `AUDIT_VERIFY` | chain verified | total_lines, verify_took_ms |
| `RUN_COMPLETE` | FSM → AUDIT_DONE | run_id, total_bars, elapsed |
| `RUN_HALTED` | FSM → HALTED | reason, last_completed_chunk |

### 9.2 Verification — proven by:

- Property P-16: `verify_chain(file)` count == `#append()` calls
- Property P-17: any single-byte tamper → `AuditChainCorruption` raised
- Property P-20: audit replay produces byte-identical Parquet output

---

## 10. Observability — structlog + Dashboard

**No Telegram in this phase** (per user decision, 2026-05-15). All
alerts route to structlog → file + dashboard. Telegram delivery is a
later-phase enhancement.

### 10.1 structlog event taxonomy

| Severity | Event kind | Trigger | Logged fields |
|---|---|---|---|
| INFO | `run.start` | RUN_START | run_id, host, ts, instruments |
| INFO | `run.complete` | RUN_COMPLETE | run_id, elapsed, total_bars, chain_status |
| INFO | `phase.complete` | per phase done | run_id, phase, ok, skipped |
| WARN | `rate_limit.downshift` | 3 retries in 60s | rps, cap, retries |
| WARN | `gap.partial` | 1-4 missing 1m bars | inst, tf, missing, window |
| WARN | `worker.hung` | no heartbeat >120s | worker_id, task |
| ERROR | `groww.api_down` | 3+ consecutive 5xx | code, endpoint, retries |
| ERROR | `validate.fail` | bad envelope | inst, chunk, error_code |
| ERROR | `quarantine.new` | chunk quarantined | inst, reason, count |
| CRITICAL | `audit.chain_break` | SHA verifier mismatch | line, expected, got |
| CRITICAL | `data.corruption` | Parquet ≠ LMDB count | store, inst, tf |
| CRITICAL | `disk.full` | free <10 GB | mount, free_gb |
| CRITICAL | `phase.halted` | FSM → HALTED | phase, reason |

### 10.2 Dashboard endpoints (FastAPI; extension of `brutex/dashboard/app.py`)

| Method + URL | Purpose | Schema |
|---|---|---|
| `GET /api/v1/state` | Current `DashboardState` (existing) | `{run_id, status, phases, workers, recent_events}` |
| `GET /api/v1/instrument/{symbol}/watermark` | Per-instrument watermark grid | `{symbol, watermarks: {tf → iso_ts}, lag_seconds: {tf → s}}` |
| `GET /api/v1/health` | Aggregate health (red/amber/green) | `{status, checks: [C1…C10]}` |
| `GET /api/v1/audit/tail?n=200` | Last N audit events + chain status | `{lines, chain_ok, break_at}` |
| `GET /api/v1/audit/verify` | Full chain re-verification | `{ok, verified, break_at, took_ms}` |
| `GET /api/v1/charts/throughput?window=24h` | Bars/sec over time | `{points}` |
| `GET /api/v1/charts/gaps?inst=NIFTY` | Heatmap: missing bars by (date, tf) | `{cells}` |
| `GET /api/v1/charts/resample-latency` | Per-TF p50/p95/p99 | `{tf → percentiles}` |
| `GET /api/v1/charts/rate-limit?window=24h` | Used/cap ratio time series | `{points}` |
| `GET /metrics` | Prometheus 0.0.4 text format (existing) | `text/plain` |

### 10.3 Real-time invariants (C1-C10) — checked on every dashboard ping

| # | Check | 🟢 criterion | 🔴 criterion |
|---|---|---|---|
| C1 | `watermark.freshness` | lag ≤ 30 min on trading day, ≤ 24h on holidays | lag > 24h on trading day |
| C2 | `gap.last7d` | no gaps > 5 bars in last 7 trading sessions | ≥1 gap > 5 bars |
| C3 | `parquet_lmdb.parity` | row counts match exactly | mismatch |
| C4 | `audit.chain_intact` | SHA chain verifies clean | break detected |
| C5 | `dedupe.hit_rate` | < 0.1% over last 1000 trials | > 1% |
| C6 | `rate_limit.headroom` | used/cap ≤ 70% | > 95% OR any 429 in 60s |
| C7 | `resample.determinism` | sampled day matches golden SHA | mismatch |
| C8 | `disk.free` | > 10 GB AND > 5% | either fails |
| C9 | `telegram.delivery` (when enabled) | drop rate ≤ 5% | > 20% |
| C10 | `instrument_master.fresh` | CSV age < 25h | > 48h |

### 10.4 Chart designs

| Chart | x-axis | y-axis | Reveals |
|---|---|---|---|
| Pull throughput | time (24h, 5min bins) | bars/sec per instrument | API saturation, dead spots |
| Gap heatmap | date (last 30) × tf | cell color = missing-bar count | Holes by instrument × date |
| Resample latency | TF (21 bars) | p50/p95/p99 ms | Slow TFs, SLA regressions |
| Rate-limit headroom | time (24h) | used_rps stacked vs cap_rps | 429 risk |
| Audit timeline | time (7d) | event count by severity (stacked) | Burst patterns; chain breaks pinned in red |
| Watermark lag | instrument (4 rows) × tf (21 cols) | lag minutes (log) — heatmap | Per-cell staleness |

### 10.5 Anti-noise rules

| Event | Dedupe window | Rationale |
|---|---|---|
| `rate_limit.downshift` | 60s | one alert, not per retry |
| `gap.partial` | per (inst, tf, date) | one per affected day, not per bar |
| `audit.chain_break` | 0s (no dedupe) | always alert |
| `data.corruption` | 0s | always alert |
| `disk.full` | 15 min | paging once is enough |
| heartbeat | 10 min | one ping per run |

---

## 10A. Groww API Error Code Map (verified 2026-05-15 from annexure)

Authoritative mapping from Groww's published error codes to BruteX
orchestrator outcomes. Consumed by `brutex/adapters/groww/errors.py`
(`is_retryable`) and the rate-limit downshift logic. (The historical
`orchestrator/retry.py` `BROKER_EXCEPTION_CLASSIFICATION` dict was
deleted as unwired scaffolding — MIGRATION.md #1727.)

### Data API Errors (we will see these most often)

| Groww Code | Meaning | BruteX Outcome | Action |
|---|---|---|---|
| `800` | Internal Server Error | `GENERIC_API_ERROR` | retry with backoff (3 attempts) |
| `804` | Requested instruments exceeds limit | `FATAL` | code bug — chunk smaller |
| `805` | Too many requests / connections | `RATE_LIMIT` | downshift bucket, retry |
| `806` | Data APIs not subscribed | `FATAL` | operator must subscribe |
| `807` | Access token expired | `AUTH_FAILED` | quarantine + alert |
| `808` | Auth failed (clientId/token invalid) | `AUTH_FAILED` | quarantine + alert |
| `809` | Access token invalid | `AUTH_FAILED` | quarantine + alert |
| `810` | Client ID invalid | `AUTH_FAILED` | quarantine + alert |
| `811` | Invalid Expiry Date | `BAD_REQUEST` | quarantine (not used for index spot) |
| `812` | Invalid Date Format | `BAD_REQUEST` | quarantine + alert (code bug) |
| `813` | Invalid SecurityId | `BAD_REQUEST` | quarantine + alert (re-verify against `web.groww.co/Charts?...&secid=X` — code fix to constants.py needed) |
| `814` | Invalid Request | `BAD_REQUEST` | quarantine + alert |

### Trading API Errors (may surface on mis-call)

| Groww Code | Meaning | BruteX Outcome | Action |
|---|---|---|---|
| `DH-901` | Invalid Authentication | `AUTH_FAILED` | quarantine + alert |
| `DH-902` | No Data API subscription | `FATAL` | operator must subscribe |
| `DH-903` | User Account issue | `AUTH_FAILED` | quarantine + alert |
| `DH-904` | Rate Limit | `RATE_LIMIT` | downshift bucket, retry |
| `DH-905` | Input Exception | `BAD_REQUEST` | quarantine + alert |
| `DH-906` | Order Error | `BAD_REQUEST` | (not used in Track 1 — Data APIs only) |
| `DH-907` | Data Error | `NO_DATA` | record, advance, do not retry |
| `DH-908` | Internal Server Error | `GENERIC_API_ERROR` | retry with backoff |
| `DH-909` | Network Error | `TIMEOUT` | retry with backoff |
| `DH-910` | Others | `GENERIC_API_ERROR` | retry once, then quarantine |

### Retry / Outcome Routing

```
HTTP 4xx, Groww code in {807, 808, 809, 810, DH-901, DH-902, DH-903} → AUTH_FAILED → quarantine + CRITICAL alert
HTTP 4xx, Groww code in {811, 812, 813, 814, DH-905, DH-906}         → BAD_REQUEST → quarantine + ERROR alert
HTTP 4xx, Groww code in {804, 806}                                    → FATAL      → halt run + CRITICAL alert
HTTP 4xx, Groww code in {805, DH-904}                                 → RATE_LIMIT → downshift bucket, retry up to 3x with backoff
HTTP 5xx OR Groww code in {800, DH-908, DH-910}                       → GENERIC_API_ERROR → retry with exp backoff (3 attempts)
Network error (timeout, DNS, connection)  OR Groww code DH-909        → TIMEOUT    → retry with backoff (3 attempts)
Groww code DH-907                                                     → NO_DATA    → advance watermark to chunk_end, do NOT retry
```

### Exchange Segment Constants (verified)

| Code | Description | Enum | Used in Track 1? |
|---|---|---|---|
| `IDX_I` | Index Value | 0 | ✅ ALL 4 instruments |
| `NSE_EQ` | NSE Equity Cash | 1 | ❌ out of scope |
| `NSE_FNO` | NSE Futures & Options | 2 | ❌ out of scope |
| `NSE_CURRENCY` | NSE Currency | 3 | ❌ out of scope |
| `BSE_EQ` | BSE Equity Cash | 4 | ❌ out of scope |
| `MCX_COMM` | MCX Commodity | 5 | ❌ out of scope |
| `BSE_CURRENCY` | BSE Currency | 7 | ❌ out of scope |
| `BSE_FNO` | BSE Futures & Options | 8 | ❌ out of scope |

### Instrument Type Constants (verified)

| Type | Description | Used in Track 1? |
|---|---|---|
| `INDEX` | Index | ✅ ALL 4 instruments |
| `FUTIDX`, `OPTIDX` | Futures/Options of Index | ❌ Options Track (post-Track-1) |
| `EQUITY`, `FUTSTK`, `OPTSTK`, `FUTCOM`, `OPTFUT`, `FUTCUR`, `OPTCUR` | other types | ❌ out of scope |

---

## 11. Pseudocode — 4 Core Functions

### 11.1 `pull_chunk` — one 90-day window

```python
def pull_chunk(inst: InstrumentConfig, tf: Literal["1m","1d"],
               window: tuple[datetime, datetime]) -> ChunkResult:
    audit.append("CHUNK_START", subject=inst.id+":"+tf, payload={"window": window})
    bucket.acquire()  # 10 r/s cap
    try:
        raw = groww.post(
            "/v2/charts/intraday" if tf == "1m" else "/v2/charts/historical",
            body={"securityId": inst.security_id, "exchangeSegment": inst.segment,
                  "instrument": "INDEX", "fromDate": window[0], "toDate": window[1],
                  "interval": "1" if tf == "1m" else None})
    except RateLimitErr as e:    bucket.downshift(); raise Retryable(e)
    except TimeoutErr as e:                          raise Retryable(e)
    except (BadRequest, AuthFail) as e: log.error("groww.api_down", error=e); raise Fatal(e)

    bars = parse_groww_ohlc(raw, tf, native_tz="Asia/Kolkata")
    assert_sorted_ascending(bars)
    assert_no_duplicates(bars, key="ts")
    bars = drop_pre_open(bars)
    bars = drop_holidays(bars, calendar=NSE_CAL)
    bars = drop_muhurat(bars)
    assert first_ts_of(bars) > watermark(inst, tf)

    for (yr, mo), group in groupby(bars, key=partition_for_timestamp):
        wr = parquet_writer.write(exchange=inst.exchange, segment="IDX",
                                  symbol=inst.symbol, interval=tf, bars=group)
        catalog.upsert(make_catalog_entry(wr, ...))
        audit.append("WRITE_DONE", payload={"path": wr.path, "raw_hash": wr.raw_hash})

    advance_watermark(inst, tf, last_bar=bars[-1], chunk_sha=sha256_chunk(bars))
    audit.append("CHUNK_DONE", payload={...})
    return ChunkResult(bars=len(bars), first=bars[0].ts, last=bars[-1].ts)
```

### 11.2 `resample_1m_to_tf` — single-pass cascade

```python
INTRADAY_TFS = [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
                30, 60, 120, 180, 240]  # minutes — 19 derived

def resample_1m_to_tf(inst: InstrumentConfig, day: date) -> ResampleResult:
    bars_1m = read_parquet(partition_path(inst, "1m", day))
    bars_1m.sort_by("ts")
    if not bars_1m: return ResampleResult(empty=True)

    anchor_utc = ist_to_utc(day, time(9, 15))
    aggrs = {n: BarAggr(anchor=anchor_utc, bucket_seconds=n*60) for n in INTRADAY_TFS}
    out = {n: [] for n in INTRADAY_TFS}

    # Single linear pass — fans to all 19 derived TFs simultaneously
    for bar in bars_1m:
        for n, agg in aggrs.items():
            closed_bar = agg.feed(bar)
            if closed_bar is not None:
                out[n].append(closed_bar)

    # Session-close at 15:30 IST — flush only COMPLETE buckets, drop partial
    last_close = ist_to_utc(day, time(15, 30))
    for n, agg in aggrs.items():
        agg.flush_if_complete(now=last_close, out=out[n])

    for n, derived in out.items():
        compute_bar_hashes(derived, inst, f"{n}m")
        parquet_writer.write(exchange=inst.exchange, segment="IDX",
                             symbol=inst.symbol, interval=f"{n}m", bars=derived)
        advance_watermark(inst, f"{n}m", derived[-1] if derived else None, ...)
    audit.append("RESAMPLE_DONE", payload={"inst": inst.id, "day": day,
                                           "tfs": list(out.keys())})
```

### 11.3 `advance_watermark` — atomic, dedup-safe

```python
def advance_watermark(inst, tf, last_bar, chunk_sha):
    if last_bar is None: return
    new_hash = sha256_bar(inst, tf, last_bar)
    with duckdb.transaction():
        cur = duckdb.execute("SELECT last_bar_ts, last_bar_hash FROM watermarks "
                             "WHERE instrument_id=? AND timeframe=?",
                             (inst.id, tf)).fetchone()
        if cur is not None and cur[0] >= last_bar.timestamp:
            # Re-run with same data — must be identical
            assert cur[1] == new_hash, "Groww bar mutated; quarantine + halt"
            return  # idempotent no-op
        duckdb.execute("INSERT OR REPLACE INTO watermarks VALUES (?,?,?,?,?,?,?)",
                       (inst.id, tf, last_bar.timestamp, new_hash, chunk_sha,
                        rows_written, now_utc()))
    audit.append("WATERMARK_ADVANCED", payload={"inst": inst.id, "tf": tf,
                                                "ts": last_bar.timestamp})
```

### 11.4 `recover` — deterministic, watermark-driven

```python
def recover(run_id: str):
    audit.verify_chain()  # else AuditChainCorruption
    for inst in config.instruments:
        for tf in ["1m", "1d"]:
            wm = watermark(inst, tf)
            # Planner re-emits chunks from wm+1min. Nothing persisted past
            # watermark is trusted (orphaned tempfiles already cleaned).
        for tf_min in INTRADAY_TFS:
            days_to_redo = duckdb.execute("""
                SELECT DISTINCT date_trunc('day', last_bar_ts) FROM watermarks
                WHERE instrument_id=? AND timeframe='1m'
                  AND last_bar_ts > (SELECT COALESCE(MAX(last_bar_ts), '1970-01-01')
                                     FROM watermarks WHERE instrument_id=? AND timeframe=?)
            """, (inst.id, inst.id, f"{tf_min}m")).fetchall()
            for d in days_to_redo:
                resample_1m_to_tf(inst, d.date)
```

---

## 12. Phase 2 Implementation Checklist

When Phase 2 (Groww Adapter + Vocabulary) lands, this design becomes
concrete. Tracking checklist:

| Item | File | Status |
|---|---|---|
| `brutex/config/instruments.toml` (dynamic config) | new | ⏳ |
| `brutex/adapters/groww/client.py` | new | ⏳ |
| `brutex/adapters/groww/constants.py` — 4 hardcoded tuples (NIFTY=13, BANKNIFTY=25, SENSEX=51, INDIAVIX=21; all `IDX_I` + `INDEX`) | new | ⏳ |
| ~~`brutex/adapters/groww/instruments.py` (master CSV resolver)~~ — **dropped per 2026-05-15 simplification (4-instrument universe is static)** | — | ❌ not built |
| `brutex/adapters/groww/intraday.py` | new | ⏳ |
| `brutex/adapters/groww/historical.py` | new | ⏳ |
| `brutex/data/watermark.py` (new DuckDB table) | new | ⏳ |
| `brutex/data/parser_groww.py` (replaces Groww parser) | new | ⏳ |
| `brutex/data/partition.py` — extend `VALID_INTERVALS` to 21 TFs | edit | ⏳ |
| `brutex/data/partition.py` — add `IDX_I` to `VALID_SEGMENTS` (single Index segment for all 4 instruments) | edit | ⏳ |
| `brutex/feed/resampler.py` — 09:15 IST anchor + 19 TFs | edit | ⏳ |
| `brutex/data/parquet_writer.py` — allow current-month grow | edit | ⏳ |
| `brutex/orchestrator/events.py` — collapse `IngestPhase` to Track 1 phases *(moved from the deleted `state.py` — MIGRATION.md #1727)* | edit | ⏳ |
| `brutex/universe.py` — add India VIX | edit (NOW) | ✅ |

---

## 13. Verification — How Every Guarantee Maps to a Test

| Guarantee | Verified by | Test ID |
|---|---|---|
| 1m→derived associative | property test | P-08 |
| No look-ahead at bar N | property test | P-06 |
| Bar hash deterministic | property test | P-19 |
| Audit chain integrity | property test + oracle | P-16, P-17, P-20 |
| Holiday calendar coverage | property test | P-22 |
| Session window correctness | property test | P-23 |
| Watermark monotonic | property test (Phase 1) | P-21 (extend to ts-monotonicity per (inst, tf)) |
| Bloom no false negatives | property test | P-05 |
| Rate limiter throughput | property test | P-18 |
| O(1) hot paths | bench test (`p99(N=1M)/p99(N=1K) ≤ 1.1`) | bench gate |

The 24 property invariants are catalogued in
`TRACK1_VERIFICATION_PYRAMID.md §3`.

---

## 14. Decisions Locked Here (additions to `MIGRATION.md §6`)

| # | Decision |
|---|---|
| 71 | 4 instruments: NIFTY 50, BANKNIFTY, SENSEX, **India VIX** |
| 72 | India VIX integration: add to V1_INDICES, same code path as other 3 |
| 73 | 21 TFs total: 1m base + 19 derived intraday + 1d native |
| 74 | Derived TFs: 2m-15m every minute (14) + 30m, 60m, 120m, 180m, 240m (5) |
| 75 | Range: rolling `2021-01-01 → today`, not fixed end |
| 76 | Watermark table in DuckDB (`watermarks` next to `partitions`) |
| 77 | Bar identity = sha256(inst, tf, ts, OHLC paisa, vol, source_version) |
| 78 | Telegram delivery deferred (structlog + dashboard primary for now) |
| 79 | 4-layer dedup defence (Watermark → Bloom → LMDB → bar_hash) |
| 80 | All `INDEX`-type instruments use `IDX_I` — the single Index segment in Groww's annexure (no separate BSE_I) |

