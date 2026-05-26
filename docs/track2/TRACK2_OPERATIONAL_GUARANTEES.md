# Track 2 — Operational Guarantees, Monitoring, Audit & Proof

> **Read this if you want one place that tells you, plainly:**
> what we promise will work, how we prove it works, what alerts you'll
> get when it breaks, and how a non-technical person can sanity-check
> the whole pipeline from a dashboard.

_Last updated: 2026-05-15. Locked together with TRACK2_OPTIONS_SPEC.md
and TRACK2_DATA_INGEST_DESIGN.md._

---

## 0. The "Explain it to a kid" version

Imagine BruteX as a delivery company.

| Person | Real job | BruteX equivalent |
|---|---|---|
| Warehouse | stores boxes | The Parquet lake on disk (spot + options) |
| Loader | adds new boxes daily | The Groww adapter (downloads new bars) |
| Picker | finds the right box for an order | The engine (looks up a bar at a timestamp) |
| Manager | tells you what was delivered + when | The audit log (one line per action, hash-chained) |
| Dispatcher | makes sure 2 loaders don't grab same box | Watermarks + Bloom + LMDB dedup |
| CCTV | records everything for replay | Hash chain + replay oracle |
| Alarm | rings when something goes wrong | Telegram + Dashboard + Doctor |

**Our guarantee in one sentence:**
> Every box has a unique address, every action gets recorded in CCTV,
> every lookup happens in the same amount of time no matter how full
> the warehouse is, and an alarm reaches your phone in under 60 seconds
> when anything breaks.

---

## 1. The 6 guarantees

| # | Guarantee | Plain English | How we prove it | Where the proof lives |
|---|---|---|---|---|
| G1 | **No hallucination** | Every number in the spec is traceable to a real source | Each cell in tables cites URL + verified date | `TRACK2_OPTIONS_SPEC.md` § references |
| G2 | **O(1) latency** | Bar lookup, dedup check, strike lookup all take the same time whether the lake has 100 bars or 100 million | CI benchmark `p99(N=1M) / p99(N=1K) ≤ 1.1` | `tests/bench/test_options_lookup_o1.py` (Phase 1) |
| G3 | **Uniqueness** | Same combo is never run twice, same bar is never written twice | 4-tier dedup: Watermark → Bloom → LMDB → bar_hash | This doc §4 |
| G4 | **Idempotence (re-run safety)** | Re-running the backfill produces the SAME bytes on disk | Replay oracle compares 2 fresh runs at SHA256 level | `tests/integration/test_options_replay.py` (Phase 1) |
| G5 | **Audit chain** | Every action appears in a tamper-evident JSONL log; breaking the chain fails CI | SHA256 chain link from previous row; `brutex audit verify` walks it | This doc §5 |
| G6 | **Operator alerting** | Operator knows within 60 seconds when ingest is stale, doctor is RED, or audit chain broke | Telegram bot + dashboard + doctor | This doc §6 (NEW — see §6.0 conflict note) |

If any guarantee fails CI, the PR cannot merge.

---

## 2. G1 — No-hallucination policy

Every numerical claim in `TRACK2_OPTIONS_SPEC.md` and
`TRACK2_DATA_INGEST_DESIGN.md` falls in exactly one of these buckets:

| Bucket | Example | How verified |
|---|---|---|
| **A — Groww-published** | "30 days per call" | URL + verified date in spec |
| **B — SDK source** | "`expiryFlag` ∈ {WEEK, MONTH}" | GitHub commit hash + filename + line |
| **C — SEBI/NSE/BSE circular** | "BANKNIFTY weekly killed 2024-11-13" | Circular number + URL |
| **D — Derived calculation** | "15,876 total backfill calls" | Math shown in tables side-by-side |
| **E — Measured (post-Phase-2)** | "Real backfill wall time on M4 Pro = 64 min" | `.brutex/bench/options_canary.json` |
| **F — UNVERIFIED** | "API behavior for BN-WEEK request post-Nov-2024" | Flagged explicitly; needs Phase 2 smoke test |

Any new claim added to the spec MUST identify its bucket. CI lint
fails any spec table cell without a citation or `UNVERIFIED` tag.

(Phase 1 deliverable: `tools/spec_citation_lint.py`.)

---

## 3. G2 — O(1) latency, proven

### The hot paths

| Operation | Frequency | Required time complexity | Implementation |
|---|---|---|---|
| Bar lookup at timestamp `T` for series `S` | Once per engine eval | O(1) | Sorted Parquet index + row group offset cache |
| Greeks lookup for the same bar | Same | O(1) | Same row — Greeks stored inline (not lazy) |
| Strike resolution: ATM-offset → absolute strike at bar `T` | Once per entry | O(1) | Per-bar `strike` array stored at ingest |
| Combo dedup check (signal_hash already run?) | Once per combo | O(1) | Bloom filter + LMDB fallback |
| Result append | Once per combo | O(1) amortized | Polars `concat` with row-group budget |

### The proof — CI benchmark gate

For each hot path, we run the operation at N=1K, N=10K, N=100K, N=1M
and assert `p99(N=1M) / p99(N=1K) ≤ 1.1`. If the ratio exceeds 1.1, CI fails.

```python
@pytest.mark.benchmark
def test_options_bar_lookup_o1(benchmark):
    for N in [1_000, 10_000, 100_000, 1_000_000]:
        df = build_options_lake(n_bars=N)
        t = benchmark(lambda: df.lookup(series_key="NIFTY:WEEK:0:CALL",
                                         bar_ts=random_ts()))
    assert p99[1_000_000] / p99[1_000] <= 1.1, "Regression: lookup is not O(1)"
```

### Why this works (table)

| Why naive lookups fail | Why our lookup wins |
|---|---|
| Pandas DataFrame iloc scans | Polars uses Arrow row-group offsets — direct seek |
| Python dict on big keys hashes ~100ns/op | Pre-computed integer hash (8 bytes) → 30ns/op |
| Bloom filter false-positives → LMDB hit | 2 lookups in cache locality — single CPU cycle each |

---

## 4. G3 — Uniqueness & deduplication (4-tier defense)

A "combo" = (signal_mask, direction, underlying, tf, mode, params,
strike_offset, expiry_cycle, slippage_profile, data_snapshot_hash,
indicator_version, code_commit_hash). Same combo → same hash → never run twice.

A "bar" = (series_key, bar_ts). Same bar → same hash → never written twice.

### The 4 tiers

| Tier | What it catches | Cost | False-positive rate |
|---|---|---|---|
| **1. Watermark** | "Did we already chunk this date range?" | O(1) JSON read | 0% (deterministic) |
| **2. Bloom filter** | "Have we seen this combo_hash before?" | O(1) memory check (~50ns) | ~0.1% (sized for survivor count, not raw 33B) |
| **3. LMDB key-value** | "Bloom said maybe — confirm in disk-backed exact set" | O(1) disk read (~10µs after warm cache) | 0% (exact) |
| **4. `bar_hash`** | "Two writers wrote the same bar — bytes match?" | O(1) SHA256-low-64 compare | 1 in 2^64 (cosmic-ray-territory collision) |

### Sequence diagram (combo dedup)

```
Engine          Bloom        LMDB         Writer
  |  has(hash)?   |             |             |
  |-------------->|             |             |
  |  False/Maybe  |             |             |
  |<--------------|             |             |
  |  if Maybe: get(hash)?       |             |
  |---------------------------->|             |
  |  hit/miss     |             |             |
  |<----------------------------|             |
  |  if miss: run + write       |             |
  |---------------------------------->>>>>>>>>|
  |                                            | append (combo_hash, result_row)
```

### The proof — property invariants

| ID | Invariant | What it asserts |
|---|---|---|
| P-O11 | Audit chain unbroken across mixed Track1+Track2 ingest | No duplicate bar written; chain hash links |
| P-O12 (new) | `bar_hash(combo)` uniqueness | For 1M random combos, set size == list size |
| P-O13 (new) | Bloom + LMDB consistency | If Bloom says "no", LMDB confirms "no" (proves Bloom is conservative) |
| P-O14 (new) | Re-run idempotence | Backfill same window 2× → byte-identical Parquet |

### Bloom math (corrected vs early draft)

Earlier Track 1 review caught a bug: sizing Bloom for raw 33B combos
gives 56% FPR. Correct sizing is for **post-threshold survivors** (~150M-1B):

| Survivor count | Bloom size at 0.1% FPR | Memory |
|---|---|---|
| 100 M | 1.2 GB | 0.1% FPR |
| 1 B | 12 GB | 0.1% FPR |
| 5 B (worst case) | 60 GB | 0.1% FPR |

M4 Pro 48 GB RAM handles ≤2 B easily. Stage 1A pre-filter must cut to
≤2 B before Bloom; gate enforced in Stage 1A.

---

## 5. G5 — Audit chain (tamper-evident JSONL)

### Format

One line per action, JSONL. Each line:
```
{"seq": 1234, "ts": "...", "kind": "OPT_INGEST_CHUNK_OK", "payload": {...},
 "prev_hash": "ab12...64", "this_hash": "cd34...64"}
```

`this_hash = SHA256(seq || ts || kind || payload || prev_hash)`. Genesis row has `prev_hash = "0" * 64`.

### Audit kinds (Track 1 + Track 2)

| Kind | When | Inherits from |
|---|---|---|
| `SPOT_INGEST_CHUNK_OK/ERR` | Track 1 ingest | Track 1 |
| `VIX_INGEST_CHUNK_OK/ERR` | Track 1 ingest | Track 1 |
| `OPT_INGEST_CHUNK_OK/ERR` | Track 2 ingest of one (stream, strike, type) chunk | NEW |
| `OPT_GREEK_COMPUTED` | Sample of 1/1000 Greek computations | NEW |
| `OPT_BACKTEST_ENTRY/EXIT` | Phase 3 trade simulation | NEW |
| `COMBO_RUN` | Track 1 Phase 3 brute-force combo | Track 1 |
| `KILL_SWITCH` | Operator forced halt | Track 1 |
| `DOCTOR_RUN` | `brutex doctor` invocation | Track 1 |

### Verify

`brutex audit verify $LAKE/audit/audit.jsonl` walks rows in order,
recomputes each `this_hash`, and asserts equality with the stored
value. ANY mismatch → exit 1 → CI fails.

### Visualization (dashboard)

The FastAPI dashboard renders the audit log as a Sankey-style flow:

```
Date    Spot Ingest   Options Ingest  Greek Compute  Backtest Run
2024-05-13  ●(252)  →  ●(252)       →  ●(94,500)    →  (none yet)
2024-05-14  ●(252)  →  ✗(BN-W err)  →  ✗(skipped)   →  (none)
                            ↓
                         alert sent
```

Click a node → see the exact audit row(s) + payload. Click "alert
sent" → see the Telegram message that was delivered.

---

## 6. G6 — Operator alerting (Telegram + dashboard + doctor)

### 6.0 ⚠️ Conflict with prior locked decision — OPERATOR DECISION REQUIRED

`CLAUDE.md` §10 list of locked-not-negotiable items currently reads:
> "NO Telegram for now (structlog + dashboard only)"

Operator pushed back 2026-05-15 with: "extreme comprehensive automation
monitoring alerting notifying on telegram capturing tracking auditing
debugging detailing visualizing checking everything dude".

**Treating this as an explicit operator override** of the prior "no
Telegram" decision. Adding Telegram alerting back to Track 2. Operator
to confirm or revert before this PR merges.

If operator confirms: Decision #99-bis (Telegram re-enabled Track 2+;
Track 1 dashboard stays primary, Telegram is alert-only).

### 6.1 Alert matrix (what triggers each channel)

| Event | Severity | Telegram | Dashboard | Doctor exit code |
|---|---|---|---|---|
| Audit chain broken | CRITICAL | yes (immediate) | red banner | 1 |
| Watermark stale > 1 day on active series | HIGH | yes (within 60 sec) | red row | 1 |
| Groww API > 5% error rate over 1h | HIGH | yes | yellow row | 0 (warning) |
| Backfill rate < 2 r/s for 10 min | MEDIUM | digest (daily) | yellow row | 0 |
| Put-call parity violation (P-O10) | HIGH | yes | red row | 1 |
| Doctor C12 OPTIONS_FRESH red | HIGH | yes | red row | 1 |
| Greek NaN / inf detected | MEDIUM | digest | yellow row | 0 |
| Bloom false-positive rate > 1% | LOW | digest | informational | 0 |
| Storage > 80% full | MEDIUM | yes (once/day) | yellow banner | 0 |
| Backfill complete | INFO | yes (success) | green banner | 0 |

### 6.2 Telegram bot design

- One bot, one chat ID (operator's personal chat).
- Bot lives at `brutex/notifications/telegram_bot.py`.
- Token + chat_id in `.env` (NEVER committed; smoke test fails if
  `TELEGRAM_BOT_TOKEN` is in any tracked file).
- Rate-limited to ≤6 messages/minute (Telegram bot API limit).
- Throttle: identical alert within 5 min → suppressed (count incremented).
- Daily 21:00 IST digest summarizing the day's MEDIUM + LOW alerts.

### 6.3 Dashboard view

`brutex dashboard serve` adds new Track 2 panels:

| Panel | What it shows |
|---|---|
| 6-stream watermark grid | 6 × 42 = 252 cells; green=fresh, yellow=stale 1d, red=stale >1d, gray=dormant (e.g., BN-WEEK post-Nov-24) |
| Backfill velocity | Live calls/sec + ETA to today |
| Greeks freshness heatmap | One row per series; color = age of last Greek |
| Audit chain SHA tail | Last 10 rows + verify badge |
| Alert log | Last 100 alerts; click to expand payload |

### 6.4 Doctor (extends Track 1's C1-C11)

| Check | What | RED condition |
|---|---|---|
| **C12 OPTIONS_FRESH** | All non-dormant series have watermark within 1 day | any active series stale > 1 day |
| **C13 OPTIONS_REPLAY** | Last replay-oracle run succeeded | last run > 7 days old OR failed |
| **C14 GREEK_PARITY** | BSM Greeks vs py_vollib within 1e-10 on canary | parity drift > 1e-9 |
| **C15 TELEGRAM_HEALTH** | Bot reachable + chat_id valid | last successful send > 1 day old |

---

## 7. Common runtime — dynamic, scalable, incremental

The user explicitly asked for: "common runtime dynamic scalable
incremental approach." Here's what each word means in code:

| Word | What it means | Where it lives |
|---|---|---|
| **Common runtime** | Track 1 + Track 2 share the same Python process, same asyncio loop, same audit log, same rate limiter, same doctor | `brutex/orchestrator/runtime.py` (Phase 2) |
| **Dynamic** | Add/remove a stream by editing `brutex/options/streams.yaml` — no code change, hot-reloaded by FSM | `brutex/orchestrator/options_state.py` (Phase 2) |
| **Scalable** | One process scales from 1 stream to 252 series by adjusting asyncio task count; rate limiter caps aggregate at 3 r/s | Token bucket in `brutex/adapters/rate_limit.py` |
| **Incremental** | Daily incremental processes only yesterday's missing bars per stream; full re-backfill only on operator command | `brutex/cli/cmd_options_incremental.py` (Phase 2) |

### Adding a new stream (zero-code path)

```yaml
# brutex/options/streams.yaml
streams:
  - id: NIFTY-WEEK
    underlying: NIFTY
    cycle: WEEK
    exchange_segment: NSE_FNO
    active_from: 2021-01-01
    active_to: null
  - id: BANKNIFTY-WEEK
    underlying: BANKNIFTY
    cycle: WEEK
    exchange_segment: NSE_FNO
    active_from: 2021-01-01
    active_to: 2024-11-13     # SEBI killed
```

To add (e.g.) FINNIFTY-MONTH back: append one block, restart FSM. No
recompile. No schema migration (Parquet partitions auto-create).

### Scalability ceiling

| Series count | Backfill wall time | Daily incremental | Disk |
|---|---|---|---|
| 252 (current) | 88 min | 70 sec | 5.5 GB |
| 504 (+FINNIFTY/MIDCPNIFTY when SEBI re-allows) | 176 min | 140 sec | 11.0 GB |
| 1,008 (hypothetical 2× growth) | 5h 52m | 5m 40s | 22.0 GB |

Linear scaling. Rate-limit bound at 3 r/s aggregate; no parallelism gain past that.

---

## 8. Visualization — pictures over words

### 8.1 The whole pipeline in one ASCII picture

```
                ┌──────────────────────────────────────┐
                │  Groww Cloud (5 r/s, 100K/day quota)  │
                └────────────────┬─────────────────────┘
                                 │ HTTP/2
                                 ▼
              ┌────────────────────────────────────────┐
              │  Rate Limiter (3 r/s shared bucket)    │
              └────┬───────────────────────┬───────────┘
                   ▼                       ▼
       ┌─────────────────┐         ┌─────────────────┐
       │ Spot Adapter    │         │ Options Adapter │
       │ (Track 1)       │         │ (Track 2)       │
       └────────┬────────┘         └────────┬────────┘
                ▼                            ▼
       ┌─────────────────┐         ┌────────────────────────┐
       │ Spot Lake       │         │ Options Lake (6 streams│
       │ (4 inst, 21 TF) │         │  × 21 × 2 = 252)       │
       └────────┬────────┘         └────────┬───────────────┘
                │                            │
                └──────────────┬─────────────┘
                               ▼
                  ┌──────────────────────┐
                  │ Greeks Compute (Rust │
                  │ Black-Scholes-Merton)│
                  └─────────┬────────────┘
                            ▼
              ┌──────────────────────────────┐
              │ Engine (Track 1 brute force) │
              │ + Options Backtester (Track 2)│
              └─────────┬────────────────────┘
                        ▼
              ┌──────────────────────────────┐
              │ Audit Log (hash-chained)     │
              └─────────┬────────────────────┘
                        ▼
              ┌──────────────────────────────┐
              │ Dashboard + Telegram alerts  │
              └──────────────────────────────┘
```

### 8.2 What the dashboard looks like (mockup)

```
┌────────── BruteX Dashboard ───────────────────────────────────────┐
│  Workday:  2026-05-15   IST  09:32   Status:  ✓ ALL GREEN         │
├───────────────────────────────────────────────────────────────────┤
│  Doctor C1-C15:  [G][G][G][G][G][G][G][G][G][G][G][G][G][G][G]   │
├───────────────────────────────────────────────────────────────────┤
│  Options Watermark Grid (6 × 42 = 252 series):                    │
│                                                                    │
│   NIFTY-WEEK    [GGGGGGGGGGGGGGGGGGGGG][GGGGGGGGGGGGGGGGGGGGG]    │
│   NIFTY-MONTH   [GGGGGGGGGGGGGGGGGGGGG][GGGGGGGGGGGGGGGGGGGGG]    │
│   BN-WEEK       [dddddddddddddddddddddd][dddddddddddddddddddddd]  │ ← dormant
│   BN-MONTH      [GGGGGGGGGGGGGGGGGGGGG][GGGGGGGGGGGGGGGGGGGGG]    │
│   SENSEX-WEEK   [GGGGGGGGGGGGGGGGGGGGG][GGGGGGGGGGGGGGGGGGGGG]    │
│   SENSEX-MONTH  [GGGGGGGGGGGGGGGGGGGGG][GGGGGGGGGGGGGGGGGGGGG]    │
│                                                                    │
│  Legend:  G = fresh (<1d)   Y = stale (1d)   R = stale (>1d)      │
│           d = dormant (SEBI/regulatory shutdown)                   │
├───────────────────────────────────────────────────────────────────┤
│  Backfill velocity: 2.94 r/s   ETA full sweep: 67 min remaining    │
│  Daily quota: 7.3% used  (7,300 / 100,000)                         │
├───────────────────────────────────────────────────────────────────┤
│  Recent alerts (last 24h):                                         │
│   09:21  ✓ Backfill resumed after 38-sec Groww 429                  │
│   06:00  ✓ Daily incremental complete (70 sec, 210 calls)          │
└───────────────────────────────────────────────────────────────────┘
```

### 8.3 What a Telegram alert looks like

```
🟢 BruteX • 2026-05-15 06:01 IST
   Daily incremental complete
   Streams ingested: 210
   Calls used: 210 / 100K (0.21%)
   Duration: 70 sec
   Audit chain: OK (verified, 14,287 rows)
   Next run: tomorrow 06:00

🔴 BruteX • 2026-05-15 14:38 IST
   ALERT — C12 OPTIONS_FRESH RED
   Stream: NIFTY-WEEK / ATM-3 / PUT
   last_ok: 2026-05-14 15:35 IST (23 hrs ago)
   Likely cause: Groww 5xx on chunk 2026-05-15
   Action: brutex doctor --diagnose c12-nifty-week-atm-3-put
   PR-status: not blocking (single series; 251 others green)
```

(In actual code we won't use emojis per CLAUDE.md style; the mockup
above shows what the operator will see in the Telegram client where
emoji glyphs render natively from the bot API.)

---

## 9. The "I'm a non-technical person" cheat sheet

| You see... | It means... | What to do |
|---|---|---|
| Telegram says "Daily incremental complete" | Yesterday's data is in the lake | Nothing — go about your day |
| Telegram says "C12 RED" | One or more series didn't get fresh data | Click the link → opens dashboard; usually self-heals next run |
| Telegram says "AUDIT CHAIN BROKEN" | Someone (or a bug) tampered with the log | DO NOT trade. Page Claude/operator immediately. |
| Dashboard shows red row in watermark grid | Same as C12 RED | Click the row to see why |
| `brutex doctor` exits 1 | At least one check failed | Read the output — top of output names the failing check |
| `brutex audit verify` exits 1 | Audit chain broken | DO NOT trade. Page operator. |

### Sanity-check ritual (60 seconds, every morning)

```
1. Open Telegram → check the green "Daily incremental complete" message
2. Run:  brutex doctor
3. If all 15 checks green → you're good
4. If anything yellow/red → click the dashboard link in the doctor output
```

---

## 10. Hostile-review checklist (for the 4-agent review on this PR)

When the 4-agent hostile review runs against this doc, agents should
specifically try to break each guarantee:

| Agent | Tries to disprove |
|---|---|
| Audit | "Show me one number in this doc without a citation" |
| Architect | "Show me a state transition that can lose data" |
| Hostile | "What if Groww changes the API mid-month? What if the disk fills up between WRITE and audit append?" |
| Reality | "Re-derive the 15,876-calls / 88-min / 5.5 GB numbers from raw quantities — do they hold?" |

If any agent finds a hole, this doc gets a follow-up commit before merge.

---

## 11. Decisions appended to MIGRATION.md by this doc

(Will be appended in the same commit that updates SPEC/INGEST docs.)

- #221 — 6 Track 2 guarantees (G1-G6) documented + locked
- #222 — Track 2 inherits Track 1's audit chain (no fork)
- #223 — Bloom filter sized for survivors-only (~1B max), not raw 33B
- #224 — Two-hash bar integrity: `raw_bar_hash` = SHA256-low-64 of (bar_ts ‖ ohlc ‖ iv ‖ spot ‖ strike_api); `cooked_bar_hash` = SHA256-low-64 of (raw_bar_hash ‖ delta ‖ gamma ‖ theta ‖ vega ‖ BSM_KERNEL_VERSION) — hostile-review fix so replay oracle catches BSM regressions
- #225 — 4-tier dedup defense (Watermark → Bloom → LMDB → bar_hash)
- #226 — Audit kinds OPT_* documented
- #227 — Doctor extends to C12-C15 for options
- #228 — Telegram bot re-enabled for Track 2+ (OVERRIDES prior "no Telegram" decision; operator confirm)
- #229 — Alert matrix (10-row table) locked
- #230 — Common-runtime / dynamic-streams / scalability ceiling documented
