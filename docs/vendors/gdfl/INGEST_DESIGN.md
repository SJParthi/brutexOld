# GDFL 1-Second L1 Snapshot Data — Ingest / Store / Derive / Read Design

## §0. Status banner

> **DESIGN ONLY.** This document ships ZERO adapter code — no parser, no writer,
> no route, no CLI arm lands in this PR. The build (Phase 1, §10) is **gated on
> the operator confirming the GDFL (Global Datafeeds) purchase** and on the
> Phase-1 gating inputs named in §9. Decision ID: **DEC-GDFL-INGEST-DESIGN-001**,
> MIGRATION.md #1781.
>
> **Foundation consumed, not re-derived:** the sibling vendor-verification
> session (DEC-GDFL-VENDOR-DOCS-001, draft PR #1506, branch
> `claude/gdfl-vendor-docs`) owns the vendor docs archive + the
> granularity/depth verdict at `docs/vendors/gdfl/VERDICT.md`,
> `docs/vendors/gdfl/api/REST_API.md`, `docs/vendors/gdfl/api/WEBSOCKET_API.md`,
> `docs/vendors/gdfl/sdk/PACKAGES.md`, `docs/vendors/gdfl/MANIFEST.md` — all
> landing with PR #1506 (path-only references throughout this doc; no line
> numbers into files not yet in this tree). This design consumes that verdict
> as ground truth for every vendor-API fact.
>
> **Licensing:** raw GDFL data is NEVER committed to this repository (the
> subscriber agreement is UNVERIFIED — "personal use" + single-subscriber
> clauses known, redistribution/derived-data clauses unread). Every statistic
> in §1 is a MEASURED aggregate; every test fixture in §8 is SYNTHETIC.
>
> **Framing (operator ruling, 2026-07-13):** this data is
> **"1-second L1 snapshot data"** — irregular L1 quote/trade snapshots at
> 1-second TIMESTAMP resolution. The word "tick" appears below ONLY (a) to say
> what the data is NOT ("not a fixed grid", "not sub-second exchange
> tick-by-tick") and (b) for GDFL's own API identifiers (`TICK`, `MaxTicks`,
> `HistoryTickResult`). 1-second is the finest REAL granularity for Indian
> markets — NSE broadcasts a conflated ~1/second feed; true sub-second needs
> exchange membership — so this store targets the ceiling, not a compromise.
>
> **Provenance of this design:** two independent 3-designer+judge panels; our
> judge synthesis is the BASE and every sibling-panel position was reconciled
> accept/reject in §11 (which records the full two-panel provenance).

---

## §0.5. Executive summary + THE DECISION TABLE

BruteX gains a third lake tree — `snap1s/` — holding GDFL 1-second L1 snapshot
rows verbatim (vendor-faithful archival, keyed on the raw vendor ticker), an
idempotent CSV importer with a byte-identity re-ingest law, a pure read-time
clean-1-per-second collapse, a derive step minting REAL 7-column 1-minute bars
under a provenance-isolated `-G1S` symbol suffix inside the existing `bars/`
tree, and a `/db` read surface mirroring the expired-options pager. The engine
is UNTOUCHED (NIFTY/BANKNIFTY/SENSEX + NSE-NIFTY-FUT; GDFL symbols are
REFERENCE_ONLY). Every decision below was judge-decided across the two panels;
§11 records each disagreement and its deciding argument.

| # | Question | DECISION | One-line rationale |
|---|---|---|---|
| R1 | Lake tree name | **`snap1s/`** | The framing ruling's own phrase; `ticks/` violates the ruling; `l1/` loses cadence; docstring pins "1s = timestamp RESOLUTION, not a fixed grid". |
| R2 | Partition layout | **`snap1s/<source>/<exchange>/<segment>/<raw_ticker>/<YYYY>-<MM>.parquet`** — `source ∈ {csv_dump}` in v1 (`ws_live`/`rest_hist` reserved); MONTH files via the ONE `option_partition_stem` | The `options/`-tree precedent + one stem law; measured worst-case volume (~13,655 rows/day ≈ ~300k/month — the highest-liquidity sample) is trivial; the sibling's per-source level accepted (one cheap dir level buys the cross-source-never-merged law forever); the sibling's per-DAY files rejected (month files + the per-(instrument, day) ledger window achieve the same idempotence unit). |
| R3 | Raw-store identity | **RAW vendor ticker minus the `.NFO` suffix** (`NIFTY25200CE`, `OIL-I`), char-screened | Tickers/filenames are PROVENANCE, never guessed semantics; the suffix is redundant with the `<exchange>/<segment>` dirs it maps to (`.NFO` → `NSE`/`FNO`; any other suffix FILE-fails as unknown). |
| R4 | Ticker → canonical resolution | **Decoupled: raw ingest NEVER blocked by resolution.** Best-effort ingest-time METADATA (recorded in the source manifest): multi-grammar candidate enumeration + registry/master validation (§4.4); exactly-one-valid ⇒ resolved; 0 or ≥2 ⇒ `unresolved` recorded; the DERIVE refuses `unresolved` loudly naming candidates | Storage keys on the vendor ticker, so archival never launders a guess and never stalls on the UNCONFIRMED fragment grammar; resolution is a mapping layer, not a storage gate. Never a guessed expiry (Golden Rule #1). |
| R5 | Row key | **`(timestamp UTC, seq Int32)`**, seq = 0-based per-second ordinal in stable FILE order | No tiebreaker column exists (measured); file order is the only order there is; measured max 5 rows/s; cap 65,535 fail-loud. |
| R6 | Re-ingest / vendor re-dump | **Byte-identity law**: source manifest records `source_sha256` per (source, ticker, date); identical hash ⇒ provable no-op; DIFFERENT bytes ⇒ (a) the stored rows are an ordered ROW-exact prefix of the re-parsed new file (**ordered row-prefix extend**, §3.4 — the DEFAULT baseline is the stored parquet, a post-normalization ROW comparison; the true BYTE prefix is checkable only under `--archive-raw`) ⇒ accept-and-extend, audited `GDFL_SNAP1S_SUPERSET`; (b) anything else ⇒ loud `GdflSourceConflictError` naming both hashes; explicit `--replace` = quarantine-copy → delete → re-ingest. Keep-first union on `(ts, seq)` stays the crash-rerun belt | seq is a pure function of the parsed row ORDER, so it is stable iff the stored rows stay a prefix; keep-first alone would MIX a reordered re-dump into a plausible-but-wrong union. The prefix-extend arm is the sibling's superset-accept NARROWED to the seq-stability-safe case (§11 A7). |
| R7 | Parquet schema | **9 columns** (§3.2): ts · seq · ltp (2dp) · buy/sell price (2dp, nullable) · buy/sell qty (nullable) · ltq · open_interest (nullable) | Merge of the panel positions; per-row `src_line` dropped ((ts,seq) + audited sha reconstructs provenance); blank OI = NULL + counted (honest absence, never fabricated); a sent literal 0 quote is stored 0.00 (the raw store never edits — §11 A12). |
| R8 | Row-skip vs FILE-fail | FILE-fatal: header drift · mixed ticker/date · undecodable encoding · non-trading date · out-of-coverage year · backward timestamp · 2M-row rail · malformed rows > `min(100, 1%)`. ROW-skip + per-class counters: blank line · field-count ≠ 10 (within the rail) · unparseable numeric · LTP ≤ 0/non-finite · negative qty/OI · out-of-session rows | Structural corruption invalidates a file's provenance; scattered blips are honest, counted absences — one torn line must not discard 13k good rows; the quantified rail still catches rot. |
| R9 | Backward timestamps | **FILE FAIL by default**; operator escape `--allow-row-skips N` (skipped + counted, never re-sorted) | Deterministic seq is the foundation of R6; disorder is an UNMEASURED shape — refuse loudly until a real file shows it; sort-on-write would silently remap seq↔payload. |
| R10 | Session law | Each row judged against its OWN IST date's REAL session (`calendars/nse.get_session`; Muhurat/half-day/special-weekend first-class). Raw store keeps `[open, close]` INCLUSIVE (a closing print AT the close instant is real data); pre-open + post-close rows skipped + counted per class; **the 1m derive is SESSION-close-exclusive** — minute buckets span `[session.open, session.close)`, and any trade print at/after the session's REAL close is counted + dropped (`post_grid_trades`). On a REGULAR day that is exactly the 375-minute `[09:15, 15:30)` grid ending at the 15:29 label — same behavior, now DERIVED from the session, never hardcoded (§6.2) | Unanimous session law + the close boundary handled explicitly AND session-aware: folding a close-instant print into the last minute label fabricates placement; a fixed `[09:15, 15:30)` boundary would contradict this row's own first-class-Muhurat law — an evening-Muhurat file would raw-KEEP yet derive ZERO bars (refuter round 1). |
| R11 | Ledger mapping | One file = one window in the SHARED lake-root WindowLedger: **`market="gdfl-snap1s"`**, `security_id=<raw_ticker>`, `interval="snap1s"`, window `[D, D+1)`; PLANNED rows seeded pre-write; SUCCESS-with-counters on stored>0; refusal ⇒ FAILED + `last_error`; header-only file ⇒ FAILED `empty_file` (deliberately NEVER the EMPTY→SKIPPED walk); **REQUIRED Phase-1 companion edit**: `db_view._window_market_bucket` gains a `gdfl` bucket matching BOTH exact `gdfl-*` market tokens + the counts-dict key (§5.1) | The ledger checkpoint IS the dashboard freshness hook (`lake_revision` stats the ledger); the sibling's scoped-separate-ledger REJECTED — it is structurally invisible to Groww rollups but moves NO freshness token (§11 A9); a compat gate pins that the ledger machinery actually accepts the new tokens. |
| R12 | Clean 1-per-second series | **Read-time VIEW, never stored** (pure `collapse_to_1s`): state fields (LTP/quotes/OI) = LAST row of the second (max seq); `ltq_1s` = Σ ltq over the second's PAYLOAD-DISTINCT rows; + `prints`, `snap_count`; absent seconds ABSENT — never forward-filled | A snapshot is end-of-second STATE (last-wins); LTQ is FLOW (summing conserves traded quantity); a future materialized 1s track calls the SAME pure function. |
| R13 | 1m derivation | Bar exists **iff ≥ 1 trade-print row (LTQ > 0)** in the minute; OHLC = first/max/min/last print LTP in `(ts, seq)` order; `volume` = Σ LTQ over payload-distinct prints, stamped `volume_basis="sum_ltq_undercount"`; `open_interest` = LAST observed OI in the minute across ALL rows (all-NULL minute ⇒ NULL, NO cross-minute carry); quote-only/idle minutes ABSENT; minute-start labels on the session's REAL-open grid (09:15 on a regular day; the Muhurat open on a Muhurat day — §6.2) | Quote-only rows carry a STALE repeated LTP — an all-quote minute would fabricate a flat bar through the measured 698 s idle gaps (Golden Rule #1). The sibling's fold-ALL-rows default REJECTED (§11 A5); its never-overcount duplicate collapse ACCEPTED at the derive layer (§11 A6). |
| R14 | Provenance isolation of derived 1m (highest stakes) | **Symbol-SUFFIX namespace under the true `bars/NSE/FNO/`**: canonical resolved contract + trailing `-G1S` token — `NSE-SIEMENS-31Jul25-8500-CE-G1S`, `NSE-OIL-29Jul25-FUT-G1S` | Grammar-verified: a last token ∉ {FUT, CE, PE} makes `parse_fno_symbol` return `None`, so EVERY parse-derived surface skips or honestly-buckets it by default. The `G1S-` PREFIX is REFUTED (it parses as exchange="G1S" and would JOIN the real chain); a new `FNO1S` segment is REJECTED (widens the most load-bearing frozenset + must chase every `exclude_segments={FNO}` site); `-G1S` over `-GDFL` because GDFL also serves native MINUTE bars — a future direct-1m ingest needs its own tag. §6.3. |
| R15 | Derived bars: catalog + ledger + TFs | Catalog **YES** (via `ParquetBarSink` — `get_bars`/`/db` pager work); derive records its OWN ledger windows under `market="gdfl-derive-1m"` per (G1S symbol, day) — derive-window status law: a legitimately zero-bar derive over a RESOLVED symbol records **SUCCESS with `bars_appended=0`**, never EMPTY (§5.2 G3; edge row 43); 19 TFs + 1day via the derive command looping each resolved `-G1S` symbol over the single-partition seam DIRECTLY — `materialize_lake_timeframes(lake_root=…, exchange="NSE", segment="FNO", symbol=<…-G1S>, kind=<OPTIONS\|FUTURES>, include_daily=True)` per symbol (the seam is SINGLE-symbol with a REQUIRED `kind: PrebuildKind`; kind parsed from the canonical contract behind the suffix — CE/PE ⇒ OPTIONS, FUT ⇒ FUTURES; §6.4) | A derive-only run must move a freshness token or every `/db` view stays stale; the master-driven prebuild loop is untouched (G1S symbols are deliberately not master rows). |
| R16 | /db surfacing | New `list_snap1s_datasets` + `query_snap1s_rows` mirroring the expired-options pager (DuckDB, keep-first `QUALIFY` on `(timestamp, seq)`, LIMIT/OFFSET, generation-keyed count memo); optional `?view=1s` serves the R12 collapse; one db.html left-panel row; derived `-G1S` bars ride the EXISTING F&O candle machinery | The proven `options/` read pattern; honest O-envelope: O(page) render over an O(files) footer count once per generation (the pre-stats-manifest posture). |
| R17 | Audit | One hash-chained JSONL line per source file via `open_ingest_audit(lake_root, name="gdfl-ingest")` (a DEDICATED chain file `<lake>/audit/gdfl-ingest.jsonl`, the `spot-ingest`/`fno-ingest` convention; writer default actor); actions `GDFL_SNAP1S_INGEST`/`_REFUSED`/`_REPLACE`/`_SUPERSET`; subject `<source>/<exchange>/<segment>/<raw_ticker>/<date>` (no `\|`); payload = full counters + `source_sha256` + partition `raw_hash` + resolution outcome; derive appends `GDFL_DERIVE_1M`; one batch-end rollup | Golden Rule #8; the chain-name-is-the-discriminator convention is the repo's real mechanism (a mechanical correction of both panels' actor spellings — §11 A15). |
| R18 | Raw-truth home (CSV v1) | The operator's ORIGINAL CSV is the raw artifact (retention obligation stated); its `sha256` + verbatim basename + counters live in the append-only source manifest **`.manifest/snap1s_sources.jsonl`** (flocked) + the audit payload; OPTIONAL `--archive-raw` = content-addressed verbatim copy under `<lake_root>/snap1s/.raw/<source>/<ist_date>/<sha256>.csv`; `snap1s/` parquet is a PURE deterministic function of the CSV bytes (delete-tree + re-ingest ⇒ byte-identical, golden-gated) | The sibling's two-tier raw-journal SPOOL is REDUNDANT for CSV ingest — the vendor CSV itself is tier-0; the WS raw-journal tier belongs to the future capture leg (§9.5). Manifest home: NOT the `schema_meta` K/V (hundreds of per-file rows misuse the instrument master's config seam), NOT a per-dataset JSON alone (the sha-identity law needs ONE append-only ledger). |
| R19 | Census / heal / stats-manifest / backup | Census/heal/stats ALL deferred to named follow-up DECs (§3.5); every existing walker provably ignores `snap1s/`; integrity at rest = write-once + sha-manifest + refuse-on-change + flock/atomic. **Backup: `snap1s/` (and the derived `*-G1S` bar dirs) are EXCLUDED from off-box restic replication until the subscriber agreement is read — a NAMED DIVERGENCE from DEC-BACKUP-OFFBOX-001's whole-root posture** | The options-scanner precedent (#1594) was an 18-round hostile-review swamp — Phase 1 stays lean with the gap disclosed; the licensing exclusion is the sibling's stricter posture ACCEPTED over our panel's "rides the whole-root include set" default (§11 A10). |
| R20 | CLI | **`brutex gdfl ingest <files…>`** + **`brutex gdfl derive-1m`** (vendor-scoped group; the TREE stays data-named): fully-inert `--dry-run`; per-file isolation; `--allow-row-skips N`; `--symbol-map`; `--replace`; `--archive-raw`; `--assume-calendar-roll` (R23); `--allow-today` (R26); exit 0 / 1 (usage) / 2 (any file refused — cron-visible); wet runs HOLD the lake pull single-flight lock; the NSE market-hours guard does NOT apply (zero vendor calls — a file ingest; the sweep/backfill exemption precedent) | Adapters are vendor-named (`adapters/groww/` precedent); the greeks-backfill inert-dry-run + `lake audit-unique` exit-code precedents. |
| R21 | Telegram | None new in v1 (operator-at-terminal CLI); `LakeWriteHalt` propagates to the existing CLI halt handling + CRITICAL alert seam untouched | Defer alerting to the capture-leg ADR where unattended runs first appear. |
| R22 | Emission semantics | Treated as change-driven, labeled UNVERIFIED; nothing load-bearing depends on it; the `identical_consecutive_rows` counter IS the standing probe | Ground-truth instruction; the collapse + derive laws are emission-agnostic by construction (R12/R13 + the R13 duplicate collapse). |
| R23 | `-I` continuous futures | **Raw storage ACCEPTED verbatim** (`OIL-I` under the vendor ticker; the one-day-per-file law records the day, so a day-file never launders into a specific contract). Resolution metadata records the NSE-calendar front-month AS-OF the file date (`select_near_month` law) as a CANDIDATE with `resolution_basis="nse_calendar_front_month"` — but the DERIVE into a specific `-G1S` contract symbol **requires the explicit, audited `--assume-calendar-roll` flag** until GDFL's roll convention is verified (probe row §8) | The sibling's refuse-`-I`-storage is REJECTED (our mandate includes the OILI/MIDCPNIFTY samples; verbatim-ticker storage carries zero contamination risk) but its core concern — the roll seam is unverifiable per-row — is ACCEPTED at the resolution layer: blind calendar resolution near an expiry could mislabel rows if GDFL rolls early (§11 A8). |
| R24 | Code placement | **`brutex/adapters/gdfl/` + `brutex/data/snap1s_*`** (the `adapters/groww/` vendor-home precedent), staged: Phase 1 = importer + store + CLI + the ONE R11 companion edit; Phase 2 = derive + /db wiring under its own DEC | The sibling's `feeds/gdfl/` one-way-import home is REJECTED: the store writer must reuse `brutex.data` COMMON primitives anyway, the /db surface + CLI unavoidably live in `brutex/`, and a later "promotion DEC" is pure churn; the zero-churn instinct is honored by the phase split instead (§11 A11). |
| R25 | Volume honesty | Derived-bar `volume` carries the stamp `volume_basis="sum_ltq_undercount"` at every metadata surface (source manifest, derive audit payload, /db provenance badge, module docstring) — NEVER a bar column (the 7-column schema is frozen); the caveat is stated BOTH ways (undercount under change-only, overcount risk under heartbeat — killed by the R13 duplicate collapse) | The sibling's L-7 stamp folded; a schema column would be invisible to `data_snapshot_hash` and alien to every schema-tolerant reader. |
| R26 | Today-dated dumps | **Refuse ingest of a file whose Date == today-IST before session close** (clean deferral message; explicit `--allow-today` escape for capture-leg experiments) | A mid-session partial dump would collide with the R6 byte-identity law on the fuller post-close re-dump (and under plain keep-first would silently mix) — the gap ALL SIX designers missed, caught by our judge (G4). The Groww TODAY-completeness gate is spine-specific and does not cover this path. |

---

## §1. Ground truth (measured)

**Provenance, stated honestly:** the 20 GDFL sample CSVs did NOT sync into the
container this design was written in (verified absent — every search negative).
All statistics below are the coordinator worker's raw-file measurements of all
20 files, relayed verbatim ("coordinator worker raw-file measurement",
2026-07-13). Samples: NIFTY/OFSS/SIEMENS/YESBANK/ZYDUSLIFE/OBEROIRLTY options +
OILI/NTPCI/SHREECEMI/ZYDUSLIFEI/MIDCPNIFTY futures, all dated 01-Jul-2025. Raw
GDFL bytes are never committed — measured statistics only.

### §1.1 Schema (measured, 20/20 files)

- 100% IDENTICAL header across ALL instrument types — index options, stock
  options, AND stock/index futures:
  `Ticker,Date,Time,LTP,BuyPrice,BuyQty,SellPrice,SellQty,LTQ,OpenInterest`
  (10 columns, comma-delimited, header present in every file).
- No OHLC, no cumulative volume, OI present on every row, IST timestamps with
  no timezone marker, `DD/MM/YYYY` + `HH:MM:SS` (seconds resolution, no ms),
  one instrument + one day per file.

### §1.2 Granularity (measured)

- Irregular emission on EVERY file: gaps ≠ 1 s AND multiple rows per second
  (min gap 0 s). **NOT a fixed 1-second grid** — 1 s is the timestamp
  RESOLUTION.
- Cadence detail (first 5 files): NIFTY25200CE 13,005 rows, max 4 rows/s, gap
  histogram 2s×8237 · 1s×4236 · 3s×299 · 4s×115 · 5s+ tail, plus 40
  same-second (0-gap) extra rows; NIFTY26050PE 9,662 rows max 4/s; OFSS9300CE
  13,245 rows max 4/s with early 12–30 s gaps; OIL-I future 7,771 rows max
  4/s, early ~25–28 s gaps; MIDCPNIFTY-I future 13,655 rows max 5/s, mostly
  1–2 s.
- LTQ is non-zero ONLY on trade-print rows (0 on pure quote updates).

### §1.3 Measured edge cases

- Liquidity spread is huge: liquid NIFTY option ~13,000 rows/day vs illiquid
  YESBANK CE only 347 rows with idle gaps up to 698 s (11+ minutes with no
  row). ZYDUSLIFE PE 721 rows, OFSS8500PE 907, OBEROIRLTY 993.
- Implied derivation law: long idle gaps ⇒ ABSENT derived bars (never
  fabricated/forward-filled); OI may carry within observations, but no
  synthetic OHLC ever.
- Ticker forms: options = `<UND><expiry-fragment><strike><CE|PE>.NFO`;
  futures = `<UND>-I.NFO` (near-month; `-I` = future).
- No negative/zero LTP in any of the 20 files (a corrupt row must still fail
  loud, never store junk).
- Session-bounded 09:15–15:30 IST.
- Multiple rows per second with NO tiebreaker column ⇒ any `(ts, seq)` key
  must derive `seq` from stable file order.

### §1.4 The date puzzle (recorded, not resolved)

The samples are dated 01-Jul-2025, while GDFL's tick-class API history is
days-scale (`GetLimitation` sample `MaxTicks:7`, units unstated — SDK-verified
per `docs/vendors/gdfl/VERDICT.md`, landing with PR #1506). These files are
therefore **vendor-handed historical DUMPS, not API pulls** — dump availability
≠ API history depth. Nothing in this design assumes API backfill exists (§9.1).

### §1.5 UNMEASURED (labeled honestly; full register in §9.5)

- Row-emission semantics (change-only vs heartbeat): UNMEASURED — irregular
  emission is CONSISTENT with change-driven; identical-consecutive-row counts
  were not taken. Treated as change-driven, labeled UNVERIFIED (R22).
- The vendor's CSV file-NAMING grammar: UNCONFIRMED — filenames are recorded
  as provenance, never parsed for semantics (§4.6).
- The option expiry-fragment grammar, `-II`/`-III` series, the GDFL `-I` roll
  convention, 15:30:00 emission, crossed/one-sided quotes, ms timestamps,
  backward timestamps, vendor re-dump behavior: all UNVERIFIED — each has a
  named refusal, counter, or probe (§8, §9.5).

---

## §2. Current-architecture fit (verified against this tree)

The four integration facts every decision below leans on — each re-verified
at the working tree of this branch:

1. **The 7-column bar schema + keep-first law.** Stored bars are exactly
   `timestamp Datetime(us, UTC) · open/high/low/close Float64 (2dp-snapped
   Decimal at the write boundary) · volume Int64 · open_interest Int64
   nullable` (`brutex/data/parquet_writer.py:1814-1831`); prices snap 2dp
   ROUND_HALF_UP via `round_money` (`brutex/data/parquet_writer.py:269-315`,
   `brutex/money.py:37-50`); merges are keep-FIRST — a same-timestamp value
   difference records a `BarConflict` and KEEPS the stored bar
   (`brutex/data/parquet_writer.py:341-372`, `:896-911`). Derived `-G1S` bars
   ride this writer UNCHANGED (§6.2).
2. **Named-tree walkers: everything IGNORES an unknown lake tree.** The
   invariant scanner (`scan_lake_invariants`,
   `brutex/data/lake_invariants.py:1128`) walks `bars/` (+ opt-in `options/`) only; the healer's L3 content token
   iterates exactly `(BAR_DIR_NAME, OPTIONS_DIR_NAME)`
   (`brutex/data/lake_heal.py:2010-2036`); dataset discovery walks
   `lake_root/bars` (`brutex/dashboard/db_view.py:1196-1248`); the stats
   manifest knows kinds `bars`/`options` only
   (`brutex/data/lake_stats_manifest.py:171-175`); `parse_partition_path` (`brutex/data/partition.py:201`)
   refuses a non-`BAR_DIR_NAME` root (`brutex/data/partition.py:254-255`). A `snap1s/`
   sibling is never descended, never crashed on — and never covered (§3.5).
3. **Freshness coupling: `lake_revision` stats the window ledger.** The
   dashboard's cross-process cache token is derived from the ledger stat +
   sweeps digest + greeks marker (`brutex/dashboard/lake_revision.py:403`,
   `:183-206`) — a write that does not touch the window ledger moves NO
   dashboard freshness token. The ingest's ledger checkpoint (§5.2) is
   therefore the ONE invalidation hook; this is why the sibling's
   scoped-separate-ledger was rejected (R11, §11 A9).
4. **The `options/` tree is the precedent for a second-class store**: an
   uncatalogued tree with its own writer (`brutex/data/option_parquet_writer.py:88`),
   its own dedup key + keep-first merge (`:405`), its own `/db` discovery +
   pager (`brutex/dashboard/db_view.py:2149`, `:4927`) and its own month stem
   law (`option_partition_stem`, `brutex/data/partition.py:319-332`). There is
   **zero tick-shaped storage anywhere** today: `brutex/live/` does not exist
   (DEC-LIVE-REMOVE-001, MIGRATION.md #862), the finest stored granularity is
   the 1-minute bar, and the interval vocabulary
   (`brutex/data/timeframes.py:108-130`; `brutex/data/partition.py:69-74`)
   contains no sub-minute label — this design deliberately does NOT add one.

Reused verbatim (Golden Rule #11 COMMON): `atomic_write_via` +
`lake_write_guard` (`brutex/data/atomic_io.py:139-151`, `:63-85` — ENOSPC/OOM ⇒
`LakeWriteHalt`), the cross-process partition flock
(`brutex/data/partition_lock.py:24-40`, 75 s bounded acquire `:107-120`), the
per-path in-process lock intern (`brutex/data/parquet_writer.py:163-176`
pattern), `round_money`, the calendar's `get_session`
(`brutex/calendars/nse.py:661`), the session filter law
(`brutex/data/session.py:13-36`), the durable WindowLedger
(`brutex/data/window_ledger.py:280-358` — `market`/`security_id`/`interval`
are plain strings, so the new tokens need no schema change; the Phase-1 compat
gate pins this stays true), the hash-chained audit writer
(`brutex/orchestrator/audit.py:9-26`, `:579`), the DuckDB keep-first
`dedup_qualify_sql` (`brutex/data/lake_read.py:237-265`), the ONE resampler
(`resample_1m_to_tf`, `brutex/data/tf_resampler.py:169`; 09:15 session anchor `:37-42`) and
`materialize_lake_timeframes` (`brutex/data/timeframe_materialize.py:437`).

Deliberately NOT built (v1): any new interval token, any engine feed change,
any `VALID_SEGMENTS`/`VALID_INTERVALS` widening, any snap1s census/heal/stats
coverage (§3.5), any Telegram surface, any live capture (§9.5).

---

## §3. The 1-second L1 snapshot store — `snap1s/`

### §3.1 Layout

```
<lake_root>/snap1s/<source>/<exchange>/<segment>/<raw_ticker>/<YYYY>-<MM>.parquet
```

- `source` ∈ `{csv_dump}` in v1. `ws_live` / `rest_hist` are RESERVED for the
  future capture leg (§9.5): per-source partitioning is structural, so a REST
  backfill of a WS gap can never be spliced into the `ws_live` series —
  cross-source agreement is a cross-VALIDATION report, never a merge (the
  sibling panel's strongest storage law, accepted — §11 A2).
- `exchange`/`segment` from the ticker-suffix map: `.NFO` → `NSE`/`FNO` (the
  only suffix the 20 samples exercise); any other suffix FILE-fails as
  `unknown_exchange_suffix` until verified.
- `raw_ticker` = the CSV `Ticker` column verbatim minus the suffix
  (`NIFTY25200CE`, `OIL-I`), screened by the `_safe_segment` traversal-refusal
  idiom (`brutex/data/option_parquet_writer.py:158-168`) + the filesystem-char
  law (`_FORBIDDEN_SYMBOL_CHARS`, `brutex/data/partition.py:101-103`).
- Month stem via the ONE `option_partition_stem`
  (`brutex/data/partition.py:319-332`) — no new f-string stem law. Note kept
  from the storage panel: in-session IST date == UTC date (09:15–15:30 IST is
  03:45–10:00 UTC), so a one-day file never straddles a UTC month.
- `snap1s/` gets its own tiny `parse_snap1s_path` inverse in the new module;
  it is NOT added to `VALID_INTERVALS`/`VALID_SEGMENTS` — a snapshot row is
  not a bar and must never be readable as one.

### §3.2 Exact Parquet schema (column order canonical; deterministic bytes)

| # | column | dtype | null | semantics |
|---|---|---|---|---|
| 1 | `timestamp` | `Datetime(us, UTC)` | NO | `DD/MM/YYYY` + `HH:MM:SS` IST-naive → fixed `+05:30` → UTC (no DST — the repo-wide law, `brutex/adapters/groww/parser.py:33-67` twin) |
| 2 | `seq` | `Int32` | NO | 0-based ordinal among rows sharing this second, in source-FILE order; cap 65,535 fail-loud (measured max 5/s) |
| 3 | `ltp` | `Float64` | NO | last trade price; 2dp `round_money` ROUND_HALF_UP at the write boundary; snap adjustments counted (`price_snap_adjusted`) |
| 4 | `buy_price` | `Float64` | YES | best bid, 2dp-snapped; blank field → NULL; a sent literal `0` stored as `0.00` (the raw store never edits vendor values — interpretation is read-time) |
| 5 | `buy_qty` | `Int64` | YES | blank → NULL; negative ⇒ row-skip class |
| 6 | `sell_price` | `Float64` | YES | as `buy_price` |
| 7 | `sell_qty` | `Int64` | YES | as `buy_qty` |
| 8 | `ltq` | `Int64` | NO | last-trade quantity; **0 = quote-only row** (measured law) |
| 9 | `open_interest` | `Int64` | YES | measured present on every row; a blank cell → NULL + counted (honest absence, never fabricated) |

No OHLC, no cumulative volume — the vendor doesn't send them (measured); the
store holds what arrived, nothing more. No per-row `src_line` column: `(ts,
seq)` order + the audited `source_sha256` reconstructs provenance exactly
(R7). Deterministic bytes: fixed column order, `ROW_GROUP_SIZE=100_000`, ZSTD
level 3, `statistics=False`, `use_pyarrow=False` — the house determinism knobs
(`brutex/data/parquet_writer.py:158-161`, `:1868`).

### §3.3 Row identity, seq stability, merge law

- **Key = `(timestamp, seq)`.** `seq` is assigned during the streaming parse:
  a per-second running counter over file order. Deterministic for fixed
  source bytes. There is no content-only key that can distinguish two
  legitimate identical same-second quotes from a duplicate — so **byte
  identity is the only honest idempotence oracle** (the stability contract).
- **Merge = keep-first union on `(ts, seq)`** inside the same two locks the
  bars writers use — in-process lock intern + the cross-process
  `partition_write_lock` flock — via `atomic_write_via` (ENOSPC/OOM ⇒
  `LakeWriteHalt`, the pull-wide stop class both ingest spines honor).
  Same-key different-payload ⇒ stored row KEPT + counted (`snap1s.conflict`,
  the `BarConflict` twin). Physical-dup self-heal on touch (the
  `lake.partition.dedup_healed` idiom).
- One file = one instrument = one day ⇒ exactly ONE partition per ingest unit
  (no multi-partition crash window).
- **Containment rails**: `_assert_snap1s_containment` under the locks (the
  `_assert_options_containment` shape,
  `brutex/data/option_parquet_writer.py:317` — symlink/escape refusal at the
  mutation boundary) + `glob=False` on every raw-path read (the #1594 R15
  glob-metachar lesson — `OIL-I`-style names are metachar-adjacent).

### §3.4 Re-ingest / re-dump law (R6 + R18)

- The append-only, flocked source manifest **`.manifest/snap1s_sources.jsonl`**
  (the `datasets.jsonl` manifest idiom, `brutex/data/lake_manifest.py:17-27`) records one
  line per source file: `{source_sha256, source_name (verbatim basename —
  PROVENANCE ONLY), source, ticker_raw, exchange, segment, symbol_key,
  ist_date, rows_read, rows_stored, counters{…}, resolution: {…|unresolved},
  partition_raw_hash, ingested_utc}`.
- Same `(source, ticker, date)` + identical `source_sha256` ⇒ provable no-op
  skip. DIFFERENT bytes:
  - **Ordered row-prefix extend** — the re-PARSED new file's normalized row
    sequence (ts, seq, payload — after the SAME §4 parse/normalization
    pipeline: 2dp snap, blank→NULL) must carry the STORED partition's row
    sequence as an exact PREFIX ⇒ accept-and-extend: ingest only the tail
    (the prefix re-derives identical `(ts, seq)` assignments by construction
    — seq is a pure function of row order, which a prefix preserves), one
    `GDFL_SNAP1S_SUPERSET` audit line. Baselines, stated exactly (refuter
    round 1 — the earlier "byte-exact PREFIX" wording was a misnomer): the
    DEFAULT baseline is the stored parquet — a POST-normalization ROW
    comparison, because the original CSV bytes live off-lake by default and
    the parquet is a lossy transform of them (2dp snap, dropped `src_line`,
    blank→NULL), so a byte-differing but parse-identical re-serialization of
    the same rows is accepted BY DESIGN; when `--archive-raw` retained the
    verbatim bytes, the check MAY additionally verify the true BYTE prefix;
    and the manifest's whole-file `source_sha256` detects identical-file
    re-ingest ONLY — it can never verify prefix growth. This is the
    sibling's superset-accept NARROWED to the seq-stability-safe case: a
    general superset with mid-second insertions would remap seq↔payload and
    is NOT accepted.
  - **Anything else** ⇒ loud `GdflSourceConflictError` naming both hashes +
    remediation. Explicit `--replace` = quarantine-COPY the existing
    partition(s) to `.quarantine/<ts>/snap1s/…` (the healer's
    `QUARANTINE_DIRNAME` law, `brutex/data/lake_heal.py:174`) → delete → re-ingest → audit
    `GDFL_SNAP1S_REPLACE`. Never a silent overwrite.
- **Raw truth (CSV v1)**: the operator's original CSV is the raw artifact —
  the operator MUST retain the dumps (stated as an operational obligation in
  the Phase-1 runbook); `--archive-raw` optionally copies the verbatim bytes
  content-addressed under `<lake_root>/snap1s/.raw/<source>/<ist_date>/<sha256>.csv`
  (idempotent by construction; covered by the SAME licensing/backup exclusion
  as the tree, §3.5). Disk cost is trivial (largest measured file ≈ 14k rows).
- **Re-derivability law**: the `snap1s/` parquet is a PURE function of the CSV
  bytes (deterministic bytes §3.2 + deterministic seq §3.3) — delete the tree,
  re-ingest the archived CSVs, get byte-identical partitions (`raw_hash`
  equality golden-gated in Phase 1). Integrity at rest is therefore
  write-once + sha-manifest + refuse-on-change — STRONGER than `bars/`'s
  merge-churn story, which is what licenses §3.5's census deferral.

### §3.5 Census / heal / stats-manifest / backup posture

| Surface | v1 (Phase 1) | Named follow-up |
|---|---|---|
| Invariant scanner | NOT extended — `snap1s/` is never descended (§2 fact 2); zero census coverage DISCLOSED | `DEC-SNAP1S-CENSUS-001` (finding classes `Snap1sIntraFileDup`/`Snap1sForeignMonthRows`, opt-in scan flag) |
| Healer + `lake_content_token` | NOT extended; corrupt partition remediation = delete + re-ingest (§3.4 makes it byte-identical) | `DEC-SNAP1S-HEAL-001` |
| Stats manifest | NOT extended; the pager live-counts (files are small; generation-keyed count memo §7.2) | `DEC-SNAP1S-STATS-MANIFEST-001` (`KIND_SNAP1S`, identity key `("timestamp","seq")`) |
| Off-box backup | **EXCLUDED**: restic exclude patterns for `snap1s/` AND the derived `bars/*/FNO/*-G1S/` dirs until the GDFL subscriber agreement is read — a NAMED DIVERGENCE from DEC-BACKUP-OFFBOX-001 (#1596)'s fail-safe whole-root include set; the derived-bars inclusion is deliberate (vendor derived-data clauses commonly restrict redistribution of DERIVED data too) | Licensing checklist item — reading the agreement either clears replication or makes the exclusion permanent (operator decision, recorded) |
| Dashboard freshness | FREE via the ledger checkpoint (§5.2) — `lake_revision` stats the ledger, so an ingest moves every generation-keyed cache with ZERO new revision plumbing | — |

### §3.6 Schema versioning + drift refusal

Every partition write stamps `schema_version=1` in the parquet key-value
metadata; readers REFUSE an unknown FUTURE version loudly (never a partial
parse of an unknown shape). The 9-column set + dtypes above are the v1
contract — any column addition/reorder is a version bump + a ledgered DEC,
never a silent widening (the vendor header gate in §4.1 is the ingest-side
twin of this law).

---

## §4. Parse & ingest

New pure streaming state machine `brutex/adapters/gdfl/csv_parse.py`
(`EXPECT_HEADER → ROWS → EOF-FINALIZE`; any FILE-fail transition aborts with a
typed `GdflFileRefused(reason, line_no, detail)` — nothing written, the ledger
records FAILED, §5.2), driven by the ingest orchestrator
`brutex/data/snap1s_ingest.py`.

### §4.1 Header gate

The first non-BOM line must equal EXACTLY
`Ticker,Date,Time,LTP,BuyPrice,BuyQty,SellPrice,SellQty,LTQ,OpenInterest`
(10 names, this order, case-sensitive — measured 100% identical across all 20
files). A UTF-8 BOM is stripped + counted; CRLF/LF both accepted + counted.
ANY other header ⇒ FILE FAIL `header_drift`, **the refusal message NAMING the
diff** (which columns moved/appeared/vanished — the sibling edge-register fold).
Rationale: a reordered or renamed column silently transposes prices into
quantities — the one failure mode a permissive parser cannot survive honestly.
Undecodable bytes (UnicodeDecodeError under strict UTF-8) ⇒ FILE FAIL
`encoding` — no latin-1 silent fallback.

### §4.2 Timestamps (IST → UTC law)

`Date` strict `%d/%m/%Y`, `Time` strict `%H:%M:%S` (measured: seconds
resolution, no ms; an ms-bearing time is an UNVERIFIED shape ⇒ format mismatch
⇒ FILE FAIL `bad_timestamp`). Combined IST-naive → fixed `+05:30` → stored
UTC; no DST branch (India has none — the repo-wide convention). A CSV
timestamp is ASSUMED exchange-IST wall clock (`ts_authority=assumed_exchange_ist`
recorded in the manifest) — structurally cross-checked by the session screen:
a file whose rows do not land in-session under IST would mass-fail §4.5's
counters (the sibling L-12 belt, folded). Monotonicity: `ts < prev_ts` ⇒ FILE
FAIL `backward_timestamp` (R9; `--allow-row-skips N` is the operator escape —
skipped rows are counted, NEVER re-sorted; sort-on-write would silently remap
seq↔payload). `ts == prev_ts` ⇒ `seq = prev_seq + 1`, counted
`seq_collisions`; else `seq = 0`.

### §4.3 Row taxonomy (every anomaly class → verdict; all counted)

| Class | Verdict | Counter / reason token |
|---|---|---|
| Header drift / reordered / renamed / extra column | FILE FAIL | `header_drift` (diff named) |
| Undecodable encoding | FILE FAIL | `encoding` |
| Mixed `Ticker` across rows | FILE FAIL | `mixed_ticker` (one-instrument law) |
| Mixed `Date` across rows | FILE FAIL | `mixed_date` (one-day law — kills cross-day contamination structurally) |
| Non-trading file date (holiday per real calendar) | FILE FAIL | `non_trading_date` (a vendor dump for a holiday = something is deeply wrong) |
| Out-of-coverage year (2027+ today — `is_trading_day_checked` raise, `brutex/calendars/nse.py:609`) | FILE FAIL | `calendar_coverage` (never ingest what we cannot session-judge) |
| File dated today-IST before session close | FILE FAIL (deferral message) | `today_incomplete_source`; `--allow-today` escape (R26) |
| Backward timestamp | FILE FAIL (default) | `backward_timestamp`; `--allow-row-skips` escape |
| Row count > 2,000,000 | FILE FAIL | `row_cap` (streaming rail; ~150× the measured max — a "one-day" file past 2M rows is not a one-day file) |
| Malformed rows > `min(100, 1%)` | FILE FAIL | `mass_corruption` (quantified rail) |
| Blank line | ROW skip | `rows_blank` |
| Field count ≠ 10 (within the rail) | ROW skip (under `--allow-row-skips`, else FILE FAIL) | `short_row` / `long_row` |
| Unparseable numeric | ROW skip (within the rail) | `bad_numeric` |
| `LTP <= 0` or non-finite | ROW skip | `nonpositive_ltp` (measured 0 occurrences; one glitch row must not discard 13k good rows) |
| Negative qty / LTQ / OI | ROW skip | `negative_value` |
| Pre-open row (09:00–09:15) | ROW skip | `rows_preopen` |
| Post-close row (after the session close instant) | ROW skip | `rows_postclose` (the 15:30:00 row itself is AT close — kept, §4.5) |
| Blank bid/ask/qty/OI cell | KEPT, field → NULL | `blank_fields` (honest absence) |
| Literal `0` bid/ask | KEPT verbatim as 0.00 | `zero_quote_fields` (the raw store never edits; "0 is not a price" is a READ-time interpretation) |
| Crossed / locked / one-sided book | KEPT verbatim | `crossed_quotes` / `one_sided_book` (UNVERIFIED semantics — store + count, never fix) |
| Identical consecutive full row | KEPT (distinct seq) | `identical_consecutive_rows` — THE standing emission-semantics probe (R22) |
| All rows filtered (stored-eligible == 0, raw > 0) | FILE FAIL | `all_filtered` (the spot spine's ALL_FILTERED twin) |

Numeric parsing rides `Decimal(str(x))` (the `to_price` idiom,
`brutex/data/candle_fields.py:96-109`); prices 2dp-snap at the write boundary
with snap adjustments counted (`price_snap_adjusted` — the groww clamp-counter
precedent). Skips within the rail do NOT demote the window (§5.2).

### §4.4 Ticker grammar + the ambiguity law

`brutex/adapters/gdfl/ticker_grammar.py` (pure) + a resolution Protocol fed by
`RegistryStore` (`brutex/data/registry_store.py:644` `list_expiries` — the F&O
expiries/contracts metadata cache) and the Groww instrument master.

- Strip the mandatory `.NFO` suffix (any other/absent suffix ⇒ FILE FAIL
  `unknown_exchange_suffix`); maps to `exchange=NSE, segment=FNO`.
- **Futures**: remainder ends `-I` ⇒ near-month RELATIVE label — see R23/§4.4a.
  `-II`/`-III` are accepted by the GRAMMAR (serial-month rank recorded) but
  resolution is REFUSED (`unresolved: far_month_unverified`) until confirmed.
- **Options**: remainder is `<UND><expiry-fragment><strike><CE|PE>` with the
  fragment grammar UNCONFIRMED. Resolution = **candidate enumeration under
  MULTIPLE grammars, never pattern faith** (the sibling L-5 law folded into
  our registry mechanism): for each registry underlying that is a PREFIX of
  the residual, for each of its expiries LIVE at the FILE date, for each
  candidate fragment form in `KNOWN_EXPIRY_FRAGMENT_FORMS` — v1 seeds:
  **∅ (the bare measured `NIFTY25200CE` shape) · `DDMMMYY` · `YYMMM`
  (monthly) · `YYMDD` (weekly)** — test whether
  `underlying + render(fragment) + strike == residual`. A candidate must ALSO
  pass: (i) the expiry exists in `registry.duckdb` as-of the file date;
  (ii) the underlying joins the Groww instrument master's F&O-underlying fold;
  (iii) the strike is plausible on the underlying's observed strike grid.
  **Exactly ONE valid candidate ⇒ resolved** (`NSE-<UND>-<DDMmmYY>-<STRIKE>-<CE|PE>`,
  the house `parse_fno_symbol` spelling, `brutex/data/fno_symbol.py:13-21`);
  **zero or ≥2 ⇒ `unresolved`** recorded with every candidate named — the raw
  store still ingests (identity = vendor ticker, R4), and the DERIVE step
  refuses that instrument loudly naming the candidates + the remediation
  ("confirm fragment grammar from real dump filenames; extend
  `KNOWN_EXPIRY_FRAGMENT_FORMS`"). The operator `--symbol-map` CSV is the
  explicit fallback (an audited mapping, never a guess). Validator (i)'s
  retention dependency is UNVERIFIED: `list_expiries` returns only PERSISTED
  registry rows, and whether Groww metadata pulls RETAIN an expiry after it
  lapses is unmeasured — if expired expiries are pruned, validator (i)
  abstains for exactly the expired-contract backfill the samples represent
  (0 candidates ⇒ `unresolved`) and resolution falls back to the audited
  `--symbol-map`; measured on the first real delivery (the raw store ingests
  either way, R4).
- **No GDFL instrument master exists in CSV-only v1** (`GetInstruments` is a
  credentialed probe gate) — the validator set above (NSE expiry calendar via
  `registry.duckdb` + strike-grid plausibility + the Groww master join) is the
  v1 substitute; a GDFL master snapshot upgrades the validator in the capture
  leg (§9.5). Both stricter alternatives — our ingest panel's FILE-refusal on
  unresolvable tickers and the sibling's quarantine-on-ambiguity — were
  REJECTED by our judge: blocking ARCHIVAL on an unconfirmed grammar buys zero
  safety (the raw key never uses the resolution) and could refuse most real
  dumps (§11 A4).

#### §4.4a `-I` continuous-future handling (R23)

The CSV samples ARE `-I` files (OILI/NTPCI/SHREECEMI/ZYDUSLIFEI/MIDCPNIFTY-I),
so refusing them outright (the sibling's v1 stance) would refuse the mandate's
own inputs. The reconciled law:

- **Storage**: verbatim under `<UND>-I` — the one-instrument-one-day file law
  records the ingest DAY on every row and on the ledger window, so a `-I`
  day-file is unambiguous PER DAY and the verbatim-ticker store never launders
  it into a specific contract.
- **Resolution metadata**: records the NSE-calendar front-month as-of the file
  date via the SHARED `select_near_month` law
  (`brutex/dashboard/movers_futures.py:328` — expiry ≥ file date, expiry day
  owned by the expiring contract) as a CANDIDATE,
  `resolution_basis="nse_calendar_front_month"`.
- **Derive gate**: minting a specific `-G1S` contract symbol from a `-I` file
  requires the explicit, audited **`--assume-calendar-roll`** flag — because
  GDFL's `-I` roll convention is UNVERIFIED (calendar-expiry roll? early
  roll? volume-based?). If GDFL rolls early, blind calendar resolution would
  attribute up to a few days of rows per month to the WRONG contract — a
  silent contamination of a derived series, the exact class the
  `NSE-NIFTY-FUT` roll machinery exists to prevent. The contamination risk is
  carried as an honest limit (§9) + a probe row (§8: a delivery spanning an
  expiry week decides the convention).

### §4.5 Session law (R10)

Each row is judged against its OWN IST date's REAL session via
`calendars/nse.get_session` (`brutex/calendars/nse.py:661` — Muhurat
(`MUHURAT_SESSIONS`, `:383`), half-days and special-weekend sessions are
first-class; a Muhurat-dated file is a legal, KEPT session). The raw store
keeps `[open, close]` INCLUSIVE — a 15:30:00 closing print is real data; rows
strictly after the close instant are skipped + counted `rows_postclose`;
pre-open (09:00–09:15) rows skipped + counted `rows_preopen`. Whether the
vendor emits a 15:30:00 row at all is UNVERIFIED — the counters are the probe.
The 1m derive is SESSION-close-exclusive — `[session.open, session.close)`
on the SAME per-date session this filter resolves (§6.2). Non-trading dates
and out-of-coverage years are FILE-fatal (§4.3) — never ingest what cannot be
session-judged (the abstain law).

### §4.6 File naming, streaming, one-instrument-one-day

- The vendor file-NAMING grammar is UNCONFIRMED ⇒ NOTHING keys on the
  filename: identity comes from the `Ticker` + `Date` COLUMNS; the basename is
  recorded verbatim in the manifest for forensics only. Duplicate-basename
  different-content is legal; the sha256 is the identity.
- Streaming `csv.reader`, line-at-a-time, bounded memory (one day ≈ ≤14k rows
  measured; the 2M-row rail is the adversarial bound). No whole-file
  `read()`, ever.
- The first data row pins `(ticker, date)`; divergence is FILE-fatal (§4.3).

### §4.7 CLI surface + re-ingest idempotence

```
brutex gdfl ingest <file...> [--lake-root …] [--dry-run] [--allow-row-skips N]
                   [--symbol-map …] [--replace] [--archive-raw] [--allow-today]
brutex gdfl derive-1m [--ticker …] [--date …] [--assume-calendar-roll] [--dry-run]
```

- Per-file isolation: one refusal never aborts the batch (record FAILED,
  continue). `--dry-run` is FULLY INERT (parse+validate+resolve only; zero
  writes/ledger/audit/manifest — the greeks-backfill precedent).
- Exit codes (the `lake audit-unique` cron-visible precedent): **0** all files
  stored clean; **1** usage/env error; **2** one or more files REFUSED
  (`EXIT_INGEST_REFUSED`).
- Wet runs HOLD the lake pull single-flight lock (serializes against Groww
  pulls on the same lake). The NSE market-hours guard does NOT apply — this is
  a file ingest with zero vendor calls (stated explicitly; the sweep/backfill
  exemption precedent).
- **Idempotence tiers**: (i) manifest sha hit ⇒ no-op skip; (ii) crash re-run
  of identical bytes ⇒ identical `(ts, seq)` keys ⇒ keep-first adds 0, ledger
  re-records SUCCESS; (iii) changed bytes ⇒ the §3.4 prefix-extend / refusal /
  `--replace` ladder. A restatement-storm breaker is UNNECESSARY under this
  law: a different-bytes re-dump is refused BEFORE any merge, so keep-first
  conflicts can only arise from identical-byte re-runs, which cannot conflict
  (§11 A13).

---

## §5. Ledger, audit, observability

### §5.1 Scoped-vs-shared WindowLedger (the R11 decision)

The sibling panel instantiated a SEPARATE WindowLedger at the new tree's root
so GDFL rows are structurally invisible to the Groww `/coverage/windows`
rollups. REJECTED, with the deciding argument: `lake_revision` derives the
dashboard's cross-process freshness token from the LAKE-ROOT ledger stat
(`brutex/dashboard/lake_revision.py:183-206`, `:403`) — a scoped ledger moves
NO freshness token, so every `/db` view of snap1s data stays permanently stale
unless `lake_revision` grows a fourth component (a larger `brutex/` edit than
the one-map fix below, plus a second ledger to operate). Instead:

- GDFL windows ride the SHARED ledger under their own `market` namespaces
  (`gdfl-snap1s`, `gdfl-derive-1m` — the `groww`/`groww-option`/`groww-future`
  namespace precedent, `brutex/adapters/ingest_common.py:304`), keeping
  `gdfl-1m` free for a hypothetical future direct-minute pull.
- **REQUIRED Phase-1 companion edit (G1)**: `db_view._window_market_bucket`
  (`brutex/dashboard/db_view.py:6142-6151`) folds ANY unknown market namespace
  into the **"spot"** display bucket — without the edit, GDFL rows would
  silently inflate the dashboard's SPOT window counts and severity rollups
  (a wrong-display class, not just noise). The fold is EXACT string
  equality, so the `gdfl` bucket must match BOTH market tokens explicitly —
  `gdfl-snap1s` (Phase 1) AND `gdfl-derive-1m` (Phase 2) — via two
  enumerated equalities or a `market.startswith("gdfl")` arm (a bare
  `== "gdfl"` entry would match NEITHER hyphenated token); and BOTH counts
  folds initialise only the spot/options/futures keys
  (`_window_counts_from_records`, `brutex/dashboard/db_view.py:6165`;
  `_fold_status_counts`, `:6357`), so the new `gdfl` counts key + payload
  key land in the SAME edit (else the fold KeyErrors on the first GDFL
  row). Pinned by a route test. Visibility is a feature:
  `/coverage/windows` SHOWS gdfl ingest state in its own bucket rather than
  hiding it.
- **Compat gate (Phase-1 test obligation)**: `market`/`security_id`/`interval`
  are plain strings on `WindowLedgerRecord`
  (`brutex/data/window_ledger.py:280-358`) — the new tokens need no schema
  change, but this is asserted-never-checked today; a Phase-1 pin drives a
  full record/flush/load round-trip with the new tokens (the sibling's
  "asserted-never-checked" catch, folded).

### §5.2 Window lifecycle

One source file = one window: `market="gdfl-snap1s"`,
`security_id=<raw_ticker>` (resolution-independent, R4), `interval="snap1s"`,
`window=[D, D+1)` ISO dates.

- **plan**: PLANNED rows seeded for the batch's (ticker, date) set BEFORE any
  write — a crash mid-batch leaves visible unfinished units.
- **record**: parse → write → `ledger.record(raw_bars=rows_read,
  bars_appended=rows_stored, bars_net_new=Σ added)` with status per
  `classify_window_status` (`brutex/data/window_status.py:59-67`). Status law:
  stored rows > 0 with row-skips WITHIN the rail ⇒ **SUCCESS + counters** (the
  spine's session-filter-drop posture — NOT rejects⇒FAILED, which would make a
  one-torn-line file un-settleable forever); any FILE refusal ⇒ FAILED +
  `last_error=<reason>` (re-pullable: fix the file/grammar, re-run);
  header-only-valid file ⇒ **FAILED `empty_file`** — deliberately NOT the
  EMPTY status: the EMPTY→SKIPPED streak walk
  (`EMPTY_TERMINAL_THRESHOLD`, `brutex/data/window_ledger.py:97`) would hand
  the window to `run_skipped_audit`'s market-BLIND rotation
  (`brutex/data/window_ledger_store.py:1827-1876`), where a *Groww* pull would
  re-open a window no Groww planner can ever re-pull — permanent churn (G3).
  The SAME G3 law binds BOTH gdfl markets (refuter round 2): on the Phase-2
  derive ledger (`market="gdfl-derive-1m"`, R15/§6.4) a legitimately
  zero-bar derive over a RESOLVED symbol — an index file (§9.1 limit 2), or
  an all-quote-only illiquid option day (§6.2's quote-only-minute ⇒ ABSENT
  law) — records **SUCCESS with `bars_appended=0`**, deliberately NEVER
  EMPTY: EMPTY's bounded-streak/SKIPPED semantics answer a VENDOR-retry
  question ("will the vendor ever serve data?"), while a derive is a
  deterministic LOCAL transform whose zero-bar outcome is definitive and
  correct — SUCCESS is the truthful terminal, resets the streak, and is
  structurally invisible to the rotation (`audit_reopen_skipped` selects
  `status == SKIPPED` rows only, `brutex/data/window_ledger.py:1111-1151`).
  Feasible with ZERO ledger change, in-tree-verified: `record` has NO
  directly-supplied-status parameter — the status always classifies
  internally via `classify_window_status(raw_bars, error_type)`
  (`brutex/data/window_ledger.py:665-682`, the classify call at `:814`) —
  but `raw_bars` and `bars_appended` are INDEPENDENT inputs (the stored row
  carries `bars=bars_appended` verbatim, `:948`), so the derive recorder
  passes `raw_bars=<consumed clean-1s input rows>` — ≥ 1 wherever a derive
  window exists, because a derive window is planned only for a (G1S symbol,
  day) whose resolved raw partition holds stored rows (R11 writes raw
  partitions only on stored>0) — and `bars_appended=<derived 1m bars>`; the
  classifier then yields SUCCESS naturally. Net: raw `empty_file` ⇒ FAILED;
  derive zero-bar ⇒ SUCCESS-with-0 — EMPTY is unreachable on EITHER gdfl
  market by construction. Edge row 43 pins it.
- **checkpoint**: mid-batch throttled delta flush via the SAME
  `make_live_ledger_flush` + single-flight `make_locked_flush`
  (`brutex/data/window_ledger_store.py:1644`, `:1710`); batch-end
  `save_window_ledger` (`:810`, atomic; disk-full ⇒ `LakeWriteHalt`). This
  checkpoint IS the dashboard-freshness hook (§5.1).

### §5.3 Audit chain (R17)

One hash-chained JSONL line per source file through
`open_ingest_audit(lake_root, name="gdfl-ingest")`
(`brutex/orchestrator/audit.py:579` — a DEDICATED chain file
`<lake>/audit/gdfl-ingest.jsonl`, exactly the `spot-ingest`/`fno-ingest`
convention; the chain NAME is the vendor discriminator, the writer keeps its
default actor). Chain law: each entry's `prev_hash` links to the prior `entry_hash` over the
sha256 preimage `seq || prev_hash || ts_utc || actor || action || subject ||
canonical_json(payload)` (`brutex/orchestrator/audit.py:9-26`); `|` is the preimage delimiter and is
rejected in the free-form fields (`:28-37`) — so the subject is the
`/`-joined `<source>/<exchange>/<segment>/<raw_ticker>/<date>`. Actions:
`GDFL_SNAP1S_INGEST` / `GDFL_SNAP1S_REFUSED` / `GDFL_SNAP1S_REPLACE` /
`GDFL_SNAP1S_SUPERSET`; payload = the full §4.3 counter set + `source_sha256`
+ partition `raw_hash` + the resolution outcome. The derive appends
`GDFL_DERIVE_1M` lines (inputs: raw partition hashes + the roll-assumption
flag when set; outputs: the `WriteResult` surface). One batch-end rollup line.
A chain break fails CI (Golden Rule #8).

### §5.4 structlog events + counters

All `snap1s.*`, vendor-derived strings ride `repr()` (the #1594 log-injection
law): `snap1s.ingest.file_start` / `file_stored` (INFO, full counters) /
`file_refused` (ERROR — reason + line_no + remediation) /
`session_dropped` (ONE aggregated WARN per file — the
`ingest.session_filter.dropped` idiom) / `conflict_summary` /
`source_conflict` (ERROR, both hashes) / `superset_extended` /
`batch_done`; `snap1s.resolve.unresolved` (WARN, candidates named);
`snap1s.derive.minute_written` / `bar_absent_span` (DEBUG, per idle gap
> 5 min) / `post_grid_trades` (WARN). Counters echoed manifest+audit+event:
`rows_read, rows_stored, rows_blank, rows_preopen, rows_postclose,
rows_skipped{by class}, seq_collisions, identical_consecutive_rows,
zero_quote_fields, crossed_quotes, blank_fields, quote_only_rows, trade_rows,
dup_prints_collapsed (derive), conflicts, bars_net_new, bom_stripped, crlf,
price_snap_adjusted`. Telegram: none in v1 (R21).

---

## §6. Derivations

### §6.1 Clean 1-per-second series — read-time VIEW (R12)

Pure `collapse_to_1s(frame)` in `brutex/data/snap1s_derive.py`:

- **State fields last-wins**: `ltp, buy_price/qty, sell_price/qty,
  open_interest` take the max-`seq` row of the second — the second's END
  state, exactly what a fixed-1s-grid sampler would have observed.
- **Flow fields sum**: `ltq_1s = Σ ltq` over the second's PAYLOAD-DISTINCT
  rows (the R13/R25 never-overcount law applied uniformly); plus
  `prints = count(ltq>0)`, `snap_count = N`.
- Seconds with no row are ABSENT from the view — never forward-filled (the
  measured 698 s idle gap stays a hole, not 698 fabricated rows).
- NOT stored: ~90%+ of seconds carry one row (measured gap histogram) and a
  second store would need its own heal/census story. A future materialized 1s
  track calls the SAME pure function — the corner is kept open, never
  pre-built.

The state-vs-flow justification is the load-bearing sentence: the vendor
stream is a conflated state snapshot — the end-of-second row IS the second's
state; LTQ is the one flow field, and last-wins would DROP traded quantity
that summing conserves into 1m volume.

### §6.2 raw-1s → 1-minute OHLCV (R13)

`derive_1m_bars(frame, session)` in the same module:

- **Bar exists iff ≥ 1 trade-print row (`ltq > 0`) in the minute.** OHLC =
  first/max/min/last print LTP over the minute's trade rows in `(ts, seq)`
  order. A quote-only minute ⇒ ABSENT bar; the measured YESBANK 698 s idle gap
  ⇒ ~11 absent minutes — NEVER forward-filled. (The refuted alternative —
  folding ALL rows' LTP — is recorded in §11 A5: IF LTP only moves on trades
  the two laws are value-identical where bars exist, but they differ on bar
  EXISTENCE, and the all-rows law fabricates flat bars through idle gaps —
  the less honest degrade under an UNVERIFIED emission model.)
- **`volume = Σ LTQ` over the minute's PAYLOAD-DISTINCT trade prints**, with
  exact-duplicate prints collapsed + counted (`dup_prints_collapsed`) so the
  stamped LOWER-BOUND claim (`volume_basis="sum_ltq_undercount"`, R25) stays
  honest under EITHER emission model: under change-only emission, trades
  between snapshots are invisible (undercount); under heartbeat emission a
  repeated print is a re-broadcast the collapse removes (the overcount arm
  killed); the residual loss — two genuinely distinct same-second trades whose
  FULL row payloads are identical — is an undercount, consistent with the
  stamp. Both directions of the caveat are stated at every surface the bars
  are labeled.
- **`open_interest`** = the LAST observed OI in the minute across ALL rows
  (OI is state; a quote-only row may carry the freshest OI). A minute whose
  rows all carry NULL OI (an UNVERIFIED shape) ⇒ bar OI = NULL — deliberately
  NO cross-minute carry: the 7-column schema has no `oi_carried` flag column,
  so a silent carry would be undisclosed staleness (§11 A6). Absent bar ⇒ no
  OI row exists to carry. Session-scoped-never-overnight holds trivially (the
  derive is per instrument-day).
- **Session-close boundary (R10)**: `derive_1m_bars` already receives the
  per-date `session`, and the minute grid is SESSION-AWARE — bucket labels
  are anchored at the session's REAL open
  (`get_session(...).open_time_ist`), close-EXCLUSIVE at its REAL close:
  `[session.open, session.close)`. Any trade print at or past
  `session.close` is counted + dropped (`post_grid_trades` WARN), because
  folding it into the last minute label fabricates placement. On a REGULAR
  day this is behavior-identical to a fixed law: the 375-minute grid
  (`trading_minutes_for_lake_interval` maps `1day` → 375,
  `brutex/data/timeframes.py:157`) ending at the 15:29 label, a 15:30:00
  print dropped — the common case, now DERIVED from the session instead of
  hardcoded. On a Muhurat day (`MUHURAT_SESSIONS`,
  `brutex/calendars/nse.py:383-389` — evening 18:15–19:15 / 18:00–19:00
  windows; the verified 2025 afternoon shift) the grid anchors at the
  Muhurat open, so an evening-Muhurat F&O dump — first-class KEPT in the raw
  store (§4.5) — derives REAL 1m bars instead of zero bars plus a
  `post_grid_trades` flood (the refuter-round-1 self-contradiction: the
  earlier fixed `[09:15, 15:30)` law was grounded on the 09:15-hardcoded
  resampler, which is provably regular-day-only). Edge row 42 pins the
  derive outcome; row 12 pins only the RAW filter.
- **Grid alignment + the Muhurat coarser-TF disclosure**: at the 1-minute
  level every minute label is its own bucket (`resample_1m_to_tf` treats
  tf=1 as identity, `brutex/data/tf_resampler.py:169`), so the derive emits
  1m ONLY and the ONE resampler owns every coarser TF unchanged. HONEST
  downstream consequence, traced in-tree (not guessed): that resampler is
  09:15-anchored FIXED arithmetic with NO session lookup and NO row drop —
  `session_open_ist = ist.dt.truncate("1d") + 09:15` plus floor-division
  bucketing (`brutex/data/tf_resampler.py:255-263`) — so for an
  evening-Muhurat bar-day it folds every 1m bar into 09:15-anchored buckets
  (offsets 525–600 min past the anchor for the evening windows), and
  coarser-TF labels are generally MISALIGNED with the Muhurat open (e.g. the
  18:00–19:00 session's `1hour` bars are labeled 17:15 and 18:15 IST;
  aligned only where the TF divides the open's offset — 525 min for an
  18:00 open). Nothing is dropped, nothing fabricated. This is the EXISTING
  lake-wide behavior, NOT a GDFL divergence: the ingest session filter
  already KEEPS evening-Muhurat 1m bars in the Groww `bars/` lake
  (`filter_to_trading_session`, `brutex/data/session.py:13-36` — "an evening
  muhurat bar is KEPT") and the same prebuild resampler serves them, so
  `-G1S` bars inherit the status quo with zero new law. The Muhurat-specific
  downstream laws already live where they matter: the daily-derive
  forming-session cutoff honors per-date closes incl. Muhurat evenings
  (`daily_derive_cutoff`, `brutex/calendars/nse.py:786`), the daily-open
  census abstains on Muhurat (`classify_daily_open` → `muhurat_abstained`,
  `brutex/data/spot_daily_resample.py:183-184`; spot-only by decision), and
  Stage-1A never rolls a Muhurat OHLC into the next regular day's anchors
  (`has_muhurat_session` guard, `brutex/engine/stage1a.py:960` —
  DEC-SWEEP-DATA-EDGE-GUARDS-001, MIGRATION.md #1616).
- **Session filter**: rows are already session-filtered at ingest (§4.5); the
  derive re-asserts via the SAME `filter_to_trading_session` family
  (`brutex/data/session.py:13-36`) — a belt, zero new law.
- **Write path** = `ParquetBarSink` → the UNCHANGED `ParquetWriter`
  (`brutex/data/sink.py:176-284`): 2dp snap, Int64 volume/OI, keep-first,
  flock, atomic write, `CandleBar` OHLC-ordering validation, catalog upsert,
  telemetry — ALL house laws free. Derive is re-runnable: same raw ⇒ same bars
  ⇒ `bars_added=0`. A raw `--replace` that changed the derive inputs uses the
  healer's recall posture — DELETE-and-rebuild the affected 1m months +
  re-derive (keep-first would keep poison; the intraday-TF recall law).

### §6.3 WHERE derived bars live — provenance isolation (R14, highest stakes)

**Decision: `bars/<exch>/FNO/<canonical-contract>-G1S/1minute/<yyyy>/<mm>.parquet`**
— the canonical resolved Groww-shape contract symbol + the trailing `-G1S`
provenance token (`NSE-SIEMENS-31Jul25-8500-CE-G1S`,
`NSE-OIL-29Jul25-FUT-G1S`).

The isolation mechanism is `parse_fno_symbol`'s right-anchored grammar
(`brutex/data/fno_symbol.py:174-227`): the LAST token must be
`FUT`/`CE`/`PE`, so any `…-G1S` symbol parses to `None` — and therefore EVERY
parse-derived surface skips or honestly-buckets it BY DEFAULT: movers
(skip-and-count), the `/db` F&O cascade (the honest `unparsed` bucket,
`brutex/dashboard/fno_cascade.py:29-32` — surfaced, not hidden), prebuild
discovery (matches MASTER instruments against DECODED contracts — never
auto-materialized), Greeks discovery (decoded CE/PE legs only,
`brutex/data/greeks_enrich_pass.py:466`, `:517` — silently skipped), and
`futures_series`'s `-FUT`-suffix prefilter
(`brutex/data/futures_series.py:313-318` — a `-G1S` symbol can never enter
`NSE-NIFTY-FUT` alias resolution).

The weighed-and-rejected alternatives (each considered by at least one
designer; all grammar-verified):

| Option | Verdict | Deciding argument |
|---|---|---|
| Metadata/provenance COLUMN on bars | REJECTED | `data_snapshot_hash` streams exactly the 7 `CandleBar` fields per bar of the `bxsnap2` preimage (`brutex/engine/snapshot_hash.py:89-94`) — a provenance column is IDENTITY-INVISIBLE, so Groww and GDFL bars at the same (symbol, ts) could keep-first-mix inside one partition under one run identity: the exact pollution this design exists to prevent. An 8th column also reads as overlay-bearing/alien to every schema-tolerant reader. |
| **`G1S-` PREFIX** (one panel's original position) | **REFUTED** | It PARSES: `parse_fno_symbol("G1S-SIEMENS-31Jul25-8500-CE")` yields `exchange="G1S", underlying="SIEMENS"` — a phantom contract that would JOIN the real SIEMENS chain in cascade/movers/prebuild/Greeks. Recorded so reviewers see it was considered and killed (§11 A14). |
| New SEGMENT token (`FNO1S`) | REJECTED | Widens the most load-bearing frozenset (`VALID_SEGMENTS`, `brutex/data/partition.py:87`) and must chase the `exclude_segments` seam (parameter defined at `brutex/dashboard/db_view.py:1058`) through every FNO-excluding caller (literal `{FNO}` calls at `brutex/dashboard/app.py:8633` + `brutex/dashboard/db_view.py:1777`; `brutex/dashboard/preopen_report.py:233` via the named `REPORT_EXCLUDED_SEGMENTS` constant) or leak derived contracts into SPOT pickers/preopen; every future `segment == "FNO"` equality becomes a standing "did we consider FNO1S?" audit — a large permanent blast radius for a REFERENCE_ONLY surface vs ZERO validator changes for the suffix. |
| New EXCHANGE token | REJECTED | `VALID_EXCHANGES` = {NSE, BSE} is load-bearing venue semantics (`brutex/data/partition.py:78`); abusing it lies about the venue and poisons every exchange-keyed join. |
| Separate lake ROOT | REJECTED | Catalog/prebuild/movers/backup/ledger all bind ONE root — a second root forks every operational surface. |
| `-GDFL` suffix (one panel's spelling) | SUPERSEDED by `-G1S` | GDFL also serves a native MINUTE periodicity (SDK-verified — the GetHistory enum; the ≈3-month 1-min depth figure is an official-page snippet, UNCONFIRMED — site egress-blocked) — a future direct-1m ingest needs its own tag; `-G1S` encodes vendor + derivation-path so the two can never collide. |

Consequences, all verified: different symbol ⇒ different partition dir ⇒
keep-first can never cross vendors; the catalog PK is distinct; the
`bxsnap2` preimage's per-slice header carries the symbol
(`brutex/engine/snapshot_hash.py:89`) ⇒ **run identity is
provenance-distinct BY CONSTRUCTION** — no hash-formula change needed. A
research sweep TARGETS a `-G1S` symbol deliberately (the engine loads by
explicit coordinates); it can never be swept by accident. GDFL symbols (raw
and `-G1S`) are REFERENCE_ONLY — never in the 60-vocab, never in the engine
surface — until a fresh ADR (§9.4). Unlike `snap1s/`, the derived `-G1S`
dirs ARE walked by the existing `bars/` integrity machinery (invariant
scanner, healer L3, catalog-reconcile, stats manifest) — and handled safely
with no snap1s-style deferral: the suffixed symbol is an opaque directory
name to `parse_partition_path`, `detect_legacy_drift` skips FNO segments
entirely (`brutex/data/lake_invariants.py:456` — the `SPOT_PROBE_SEGMENTS`
gate in its body), the
foreign-month/intra-file-dup invariants apply per-file to the writer's
correctly-monthed rendered rows, and the distinct dir is exactly what
blocks keep-first cross-vendor mixing.

### §6.4 Interaction with prebuild / 19 TFs / movers / bad-tick screen

- **19 TFs + 1day**: the derive command loops each resolved `-G1S` symbol
  over the single-partition seam DIRECTLY —
  `materialize_lake_timeframes(lake_root=…, exchange="NSE", segment="FNO",
  symbol=<…-G1S>, kind=…, include_daily=True)` per symbol
  (`brutex/data/timeframe_materialize.py:437` — the seam is SINGLE-symbol
  with a REQUIRED `kind: PrebuildKind`; no plural `symbols=` overload
  exists). `kind` is derived from the canonical contract parsed from behind
  the `-G1S` suffix — the CE/PE ⇒ `OPTIONS` / FUT ⇒ `FUTURES` mapping is
  applied DIRECTLY to the parsed contract-type token, MIRRORING
  `prebuild_kind_for`'s dispatch logic (`:211-230` — the mirrored LAW, not
  the directly-callable tool: it takes a full master `Instrument`, and
  `-G1S` symbols are deliberately not master rows), routing through the
  OI-aware FNO resampler (`intraday_resampler_for`, `:233-257`). The
  master-driven pipeline prebuild is UNTOUCHED — it filters instrument-MASTER rows
  (`underlying_match_tokens` over the master list,
  `brutex/dashboard/pipeline_prebuild.py:335`) and `-G1S` symbols
  are deliberately not master rows (no universe pollution).
- **Derive ledger windows** under `market="gdfl-derive-1m"` per (G1S symbol,
  day) — the freshness + resume/idempotence visibility argument of R15.
  Status law: a legitimately zero-bar derive over a RESOLVED symbol (an
  index file, an all-quote-only day) records **SUCCESS with
  `bars_appended=0`** — never EMPTY on either gdfl market (§5.2 G3; edge
  row 43).
- **Movers**: contract-parse fails ⇒ skipped-and-counted (the movers
  unparsable law) — GDFL bars can never enter movers rankings by accident;
  surfacing them there would be a deliberate later decision.
- **Bad-tick screen interplay (G2, honest limit)**: `arrow_table_to_bars`
  applies the default-ON `BadTickScreen` (a BruteX repo identifier, not
  GDFL-data framing; 4.0× vs the previous in-session
  kept close — `brutex/data/bad_tick.py:72`; wiring
  `brutex/feed/replay.py:330`) to ANY engine read. Deep-OTM option LTP
  legitimately moves ≥4× intraday (0.05 → 0.25 is 5×), so a deliberate
  research sweep over `-G1S` OPTION bars would silently skip-and-count REAL
  bars. Carried as operator guidance (`BRUTEX_BAD_TICK_SCREEN=0` for such
  reads) + a named follow-up DEC if G1S sweeps become routine — the screen's
  4.0 threshold was derived from INDEX circuit-breaker physics, never
  calibrated for option premiums.
- **Cross-vendor verification (the consumer-gold follow-up)**: for a contract
  Groww ALSO serves (expired SIEMENS/OFSS), a read-only
  `brutex doctor --gdfl-diff` diffs `…-CE-G1S/1minute` against the Groww twin
  — the FIRST independent check of Groww bar fidelity the repo has ever had,
  quantifying the expected volume undercount. The sibling's three-way variant
  (adding GDFL's own native REST MINUTE candles, which bounds the undercount
  with vendor data alone) is folded into the check's design; NO pass/fail
  thresholds are invented pre-measurement, and the diff NEVER heals either
  side (report-only).

---

## §7. Read surfaces

### §7.1 /db discovery + pager (mirrors the expired-options pattern)

- **Discovery**: `brutex/dashboard/snap1s_view.py::list_snap1s_datasets` —
  the `list_option_datasets` mirror (`brutex/dashboard/db_view.py:2149`):
  walk `snap1s/`, one dataset row per (source, exchange, segment, raw ticker)
  with month span + row counts + the manifest's resolution outcome; route
  `GET /api/v1/db/snap1s/datasets`.
- **Pager**: `query_snap1s_rows` — the `query_option_candles` mirror
  (`brutex/dashboard/db_view.py:4927`): DuckDB over the ticker's month files,
  keep-first `QUALIFY` on `(timestamp, seq)` via
  `dedup_qualify_sql(key_columns=("timestamp","seq"))`
  (`brutex/data/lake_read.py:237-265`), `ORDER BY timestamp, seq` + the
  physical `filename, file_row_number` tiebreaker law, LIMIT/OFFSET pages;
  columns = the 9 stored + a derived IST display time. Optional `?view=1s`
  serves the §6.1 collapse as a display toggle.
- **Caches/keying**: count memo `snap1s_count_key` (coords+bounds only — the
  `option_candle_count_key` paging-invariance law,
  `brutex/dashboard/db_view.py:4907`) on the COMMON `MoversSnapshotCache`
  keyed on `_summary_cache.generation` — which MOVES because the ingest
  checkpoints the window ledger (§5.2). Honest O-envelope: O(page) render over
  an O(files) footer count once per generation — the pre-stats-manifest
  options posture; the stats-manifest fast path is the §3.5 follow-up.

### §7.2 Where derived bars appear + the db.html wiring point

- Derived `-G1S` bars are ordinary `bars/` datasets: the existing `/db` F&O
  candle machinery serves them automatically (`list_fno_candle_datasets`,
  `brutex/dashboard/db_view.py:1251` — surfaced in the cascade's honest
  `unparsed` bucket; the FNO derived-columns join keys on a DECODED symbol, so
  moneyness/spot render an honest `—`). A provenance badge
  ("1s-derived · GDFL · volume=ΣLTQ undercount") rides the dataset row (R25).
- **db.html**: one new left-panel row in the renderer block
  (`brutex/dashboard/templates/db.html:549-606`):
  `🕐 1-second L1 snapshots` (`data-candles="snap1s"`) + a
  `selectCandles("snap1s")` arm — the slot the read-architecture map names.
  On-screen label wording follows the framing ruling.

---

## §8. Edge-case matrix

Every measured + UNVERIFIED case → design decision → planned test (Phase-1
files; 7 test types where meaningful). Fixtures are SYNTHETIC rows shaped by
the §1 statistics — raw GDFL bytes never committed (L13). M = measured,
U = UNVERIFIED, D = design-derived.

| # | Case | Decision | Planned test → what it pins |
|---|---|---|---|
| 1 | M: multi-rows/second, max 4–5/s, 0-gap rows | `(ts, seq)` file-order key (R5) | `tests/unit/test_gdfl_csv_parse.py::test_seq_stable_file_order` — 5-in-one-second fixture keys seq 0..4, byte-stable |
| 2 | M: 1–5 s gaps dominant | rows stored as-is; no grid fabrication | `tests/golden/test_snap1s_store_golden.py` — gap fixture round-trips byte-identical (frozen `raw_hash`) |
| 3 | M: 12–30 s illiquid gaps (OFSS, OIL-I early) | absent seconds absent; 1m bar iff ≥1 print | `tests/unit/test_snap1s_derive.py::test_gap_minutes_absent` |
| 4 | M: 698 s idle (YESBANK, 347 rows/day) | ~11 ABSENT 1m bars; OI never carried into absence | `test_snap1s_derive.py::test_long_idle_never_forward_fills` — kills a ffill mutant |
| 5 | M: LTQ=0 quote-only rows | stored; EXCLUDED from OHLC/volume; OI-observation eligible | `test_snap1s_derive.py::test_quote_only_minute_has_no_bar` + `::test_oi_from_quote_row` |
| 6 | M: OI on every row | nullable Int64; last-obs per minute; blank → NULL + counted | `test_gdfl_csv_parse.py::test_blank_oi_null_counted` |
| 7 | M: liquidity spread 347..13,655 rows/day | one code path; flat per-row cost | `tests/bench/test_snap1s_ingest_o1.py` — per-row parse cost flat 347 vs 13,655 (`_o1_tiers` shared-CI tier) |
| 8 | M: expired stock options (SIEMENS/OFSS at later dates) | resolution vs FILE-date registry expiries | `tests/integration/test_gdfl_ticker_resolution.py::test_expired_contract_resolves_at_file_date` |
| 9 | M: `-I` futures (OILI/NTPCI/MIDCPNIFTY-I) | raw verbatim; calendar candidate recorded; derive flag-gated (R23) | `test_gdfl_ticker_resolution.py::test_future_i_derive_requires_roll_flag` + `::test_unknown_series_suffix_unresolved` |
| 10 | M: one uniform 10-col schema | exact header gate, diff NAMED | `test_gdfl_csv_parse.py::test_header_drift_fails_naming_diff` (mutation: reordered cols) |
| 11 | M: duplicate identical full rows (40/file) | RAW: stored distinct seq + counted; DERIVE: collapsed for ΣLTQ + counted | `test_gdfl_csv_parse.py::test_identical_rows_kept_and_counted` + `test_snap1s_derive.py::test_dup_prints_collapse_never_overcounts` |
| 12 | M: session-bounded 09:15–15:30 | inclusive raw `[open, close]`; pre-open/post-close skip+count; Muhurat kept via `get_session` (RAW-filter row — the Muhurat DERIVE grid is row 42) | `tests/unit/test_snap1s_session_filter.py::test_preopen_dropped` + `::test_muhurat_kept` |
| 13 | M: re-ingest same file | manifest sha no-op; keep-first belt ⇒ net_new=0, SUCCESS | `tests/integration/test_snap1s_ingest_idempotent.py` — property: double-ingest ≡ single |
| 14 | D: crash between write and ledger | re-run converges (keep-first + PLANNED row visible) | `test_snap1s_ingest_idempotent.py::test_crash_between_write_and_ledger` |
| 15 | U: emission semantics (change-only vs heartbeat) | change-driven ASSUMED, labeled; counter probes | `test_gdfl_csv_parse.py::test_identical_consecutive_counter` — the standing measurement |
| 16 | U: crossed/locked book, one-sided book, zero bid-ask | stored verbatim + counted; never fixed; blank→NULL, literal 0→0.00 | `test_gdfl_csv_parse.py::test_crossed_quote_stored_counted` + `::test_blank_vs_zero_quote_fields` |
| 17 | U: file-naming grammar | provenance-only manifest; body is the identity | `test_snap1s_manifest.py::test_filename_never_parsed` — garbage-named file ingests off the body |
| 18 | U: ms timestamps | strict `%H:%M:%S`; drift ⇒ FILE FAIL | `test_gdfl_csv_parse.py::test_ms_time_fails_file` |
| 19 | U: BOM / CRLF / undecodable bytes | BOM/CRLF tolerated + counted; undecodable ⇒ FILE FAIL `encoding` | `test_gdfl_csv_parse.py::test_bom_crlf_ok` + `::test_latin1_garbage_fails_loud` |
| 20 | U: backward timestamps | FILE FAIL default; `--allow-row-skips` counts, never re-sorts | `test_gdfl_csv_parse.py::test_backward_ts_fails_file` + `::test_allow_skips_never_resorts` |
| 21 | M(absent): zero/negative LTP | LTP ≤ 0 ⇒ row-skip + counter (within the rail) | `test_gdfl_csv_parse.py::test_nonpositive_ltp_skipped_counted` |
| 22 | U: mixed ticker/date in one file | FILE FAIL (structural) | `test_gdfl_csv_parse.py::test_mixed_date_fails` |
| 23 | U: reordered vendor re-dump | sha mismatch + the row-prefix check FAILS ⇒ `GdflSourceConflictError`, store byte-untouched | `tests/integration/test_snap1s_reingest.py::test_reorder_refused` |
| 24 | U: appended (grown) re-dump | ordered ROW-prefix vs the stored parquet (§3.4 — byte-prefix only under `--archive-raw`) ⇒ accept-and-extend + `GDFL_SNAP1S_SUPERSET`; else refuse; `--replace` quarantines | `test_snap1s_reingest.py::test_prefix_extend` + `::test_replace_quarantines_then_rewrites` |
| 25 | D: today-dated dump mid-session (G4) | FILE FAIL deferral; `--allow-today` escape | `test_gdfl_csv_parse.py::test_today_dated_file_deferred` |
| 26 | U: non-trading / out-of-coverage file date | FILE FAIL `non_trading_date` / `calendar_coverage` | `test_snap1s_session_filter.py::test_holiday_date_fatal` + `::test_2027_abstains_loud` |
| 27 | U: 15:30:00 closing print | raw KEPT (at-close inclusive); 1m derive drops at/after the session's REAL close + `post_grid_trades` (the past-15:29-label case on a regular day) | `test_snap1s_derive.py::test_1530_print_stored_not_bucketed` |
| 28 | U: expiry-fragment ambiguity (≥2 valid candidates) | `unresolved` recorded naming candidates; raw ingests; derive refuses | `test_gdfl_ticker_resolution.py::test_ambiguous_fragment_unresolved_derive_refused` |
| 29 | D: >min(100,1%) malformed rows | FILE FAIL mass rail | `test_gdfl_csv_parse.py::test_mass_corruption_rail` |
| 30 | D: adversarial huge file | streaming + 2M-row rail; bounded RSS | `tests/stress/test_snap1s_large_file.py` (process_probe RSS bound) |
| 31 | D: concurrent ingest, same day, two processes | flock serialises; second writer keep-first no-ops; bytes stable | `tests/stress/test_snap1s_concurrent_ingest.py` |
| 32 | D: parse-total conservation | rows_read == stored + skipped{Σclasses} + blank | `tests/property/test_gdfl_parse_properties.py` (hypothesis row soup) |
| 33 | D: derive conservation | Σ bar volume == Σ payload-distinct trade LTQ per session | `tests/property/test_snap1s_derive_properties.py` |
| 34 | D: provenance isolation end-to-end (R14) | ingest+derive beside a Groww twin: Groww partitions byte-identical, movers skip, snapshot hashes differ | `tests/integration/test_g1s_provenance_isolation.py` |
| 35 | D: suffix-token collision drift pin (G7) | `parse_fno_symbol("<x>-G1S") is None` forever; `G1S` ∉ instrument-type tokens | `tests/unit/test_g1s_suffix_drift_pin.py` — a future grammar widening cannot silently re-join the namespaces |
| 36 | D: ledger market-bucket fold (G1) | `_window_market_bucket` folds BOTH `gdfl-*` markets into `gdfl` (+ the counts-dict key, §5.1); GDFL rows never inflate SPOT rollups | route test in `tests/unit/test_dashboard_db_view.py` family |
| 37 | D: ledger token compat gate | `market="gdfl-snap1s"`/`interval="snap1s"` record/flush/load round-trip | `tests/unit/test_snap1s_ledger_tokens.py` |
| 38 | D: EMPTY/SKIPPED interplay (G3) | header-only file ⇒ FAILED `empty_file`, NEVER EMPTY — the skipped-audit rotation can't adopt it | `test_snap1s_ledger_tokens.py::test_empty_file_is_failed_not_empty` |
| 39 | D: Greeks-backfill census noise (G6) | Phase-1 obligation: verify which backfill census bucket a `-G1S` 1minute FNO dataset ticks; prefilter or disclose | verification note in the Phase-1 PR + pin if a counter fires |
| 40 | D: licensing repo pin (L13) | no tracked file under any gdfl data path; fixtures synthetic-only | `tests/unit/test_gdfl_no_raw_data_pin.py` |
| 41 | D: mutation | new modules join the weekly per-module zero-survivor mutmut loop | `mutation.yml` wiring pin (the house pattern) |
| 42 | M(calendar): evening-Muhurat session DERIVE (refuter round 1) | the 1m grid anchors at the session's REAL open, close-exclusive `[session.open, session.close)` (§6.2) — an evening-Muhurat file derives REAL bars with ZERO `post_grid_trades`; the coarser-TF fold inherits the lake-wide 09:15-anchored `resample_1m_to_tf` behavior (disclosed, §6.2) | `test_snap1s_derive.py::test_muhurat_session_derives_on_real_session_grid` — evening 18:00–19:00 fixture ⇒ real 1m bars, no post-grid drops; a regular-day fixture stays byte-identical to the fixed-grid law |
| 43 | D: legitimately zero-bar derive — index file / all-quote-only day (refuter round 2) | the derive window records **SUCCESS with `bars_appended=0`** (`raw_bars` = consumed clean-1s rows ≥ 1), NEVER EMPTY — the market-blind skipped-audit rotation can never adopt a `gdfl-derive-1m` window (§5.2 G3, the row-38 twin) | `test_snap1s_derive.py::test_zero_bar_derive_records_success_never_empty` — an index-file fixture derives 0 bars ⇒ ledger row SUCCESS/`bars=0`/streak reset; asserts status is not EMPTY and `audit_reopen_skipped` never selects it |
| 44 | D: `collapse_to_1s` state-vs-flow law (R12/§6.1; refuter round 3 coverage NIT) | state fields (LTP/quotes/OI) = the max-`seq` LAST row of the second; `ltq_1s` = Σ ltq over the second's PAYLOAD-DISTINCT rows; absent seconds ABSENT in the 1s view — never forward-filled | `test_snap1s_derive.py::test_collapse_to_1s_last_wins_state_sum_flow` — a multi-rows-in-one-second fixture (divergent LTP/quotes + one duplicated print + a following empty second) pins max-seq last-wins state, payload-distinct ΣLTQ flow, and the absent-second hole |

---

## §9. Honest limits, UNVERIFIED register, probe gates

### §9.1 Honest limits (Golden Rules #1/#10)

1. **No historical backfill.** GDFL tick-class API history is days-scale
   (`GetLimitation` sample `MaxTicks:7`, units unstated) — a 2020→now 1-second
   archive does NOT exist via the API; forward capture from subscription day
   is the only 1s-class path, and the capture leg is OUT of this design
   (§9.5). The samples are vendor-handed DUMPS — dump availability ≠ API
   depth (§1.4; recorded, not resolved).
2. **L1 only.** Singular bid/ask; no depth, no order book, no trade-by-trade
   tape; a market-depth WS subscription is NOT assumed to exist. Index-class
   symbols have no trade prints (~1/s broadcast ceiling) — an index file
   derives ZERO 1m bars under §6.2, which is correct, not a bug; its derive
   window records SUCCESS with `bars_appended=0` per the §5.2 derive-window
   status law, never EMPTY — the derive-ledger EMPTY→SKIPPED exposure to the
   market-blind skipped-audit rotation this register would otherwise have to
   carry is CLOSED by that law (refuter round 2; edge row 43).
3. **Derived-bar fidelity.** OHLC bounds only OBSERVED prints — intra-gap
   extremes are invisible (derived H/L are bounds of observed prints, not
   exchange-true extremes); `volume = Σ LTQ` undercounts under change-only
   emission (the stamp says so) and the heartbeat overcount arm is killed by
   the duplicate collapse at the cost of a rare distinct-identical-trade
   undercount; emission semantics UNVERIFIED. `-G1S` bars are a RESEARCH
   surface, never presented as exchange-official OHLCV — stamped on the module
   docstring, the /db provenance badge, and the derive summary event.
4. **Engine scope.** GDFL symbols (raw and `-G1S`) are REFERENCE_ONLY — never
   in the 60-condition vocabulary, never in the engine surface ({3 spot
   indices} ∪ {NSE-NIFTY-FUT}); a future 1s backtest track needs an ADR +
   scope decree + a 1s-aware feed + a run-identity debate. Nothing here paints
   that corner: the raw store keeps every vendor field, the collapse law is
   one pure function, and the interval vocabulary is untouched (no `1second`
   token anywhere).
5. **Run identity.** Derived bars re-key `data_snapshot_hash` via the `-G1S`
   symbol in the slice header (§6.3) — no hash-formula change; the RAW
   snapshot store is outside run identity entirely (like `options/`).
6. **Licensing gate + backup exclusion.** Raw GDFL data never committed;
   `snap1s/` AND the derived `*-G1S` bar dirs are EXCLUDED from off-box
   restic replication until the subscriber agreement is read — the named
   divergence from DEC-BACKUP-OFFBOX-001 (§3.5); derived-data clauses must be
   read for the `-G1S` bars EXPLICITLY, not just the raw tree (G5). Written
   licensing answers from GDFL sales are a BEFORE-purchase checklist item.
7. **The expiry-fragment grammar is UNCONFIRMED.** Raw archival always
   proceeds (R4), but every OPTION's derive leg stalls at `unresolved` until
   the operator supplies real filenames/grammar confirmation; `-I` futures
   derive under the explicit roll-assumption flag only (R23). Futures raw +
   the /db raw surface deliver Phase-1 value regardless — this is Phase 1's
   gating input. The registry validator's EXPIRED-expiry retention is itself
   UNVERIFIED (§4.4) — if Groww metadata prunes lapsed expiries, resolution
   for an expired-contract backfill (the sample workload) falls back to the
   audited `--symbol-map`; measured on the first real delivery.
8. **Vendor re-dump behavior UNVERIFIED.** If GDFL routinely re-issues
   reordered dumps per (ticker, day), every re-delivery hits the R6 refusal
   and the operator must `--replace` (quarantine preserves forensics;
   continuity is operator-managed); grown dumps whose re-parsed rows extend
   the stored row sequence ride the row-prefix-extend arm (§3.4 — a
   parquet-baseline ROW comparison by default; the byte-prefix check needs
   `--archive-raw`). Measure on real deliveries before softening.
9. **Zero census/heal/stats coverage of `snap1s/` in v1** (§3.5): walkers
   ignore the tree; integrity at rest = write-once + sha-manifest +
   refuse-on-change + flock/atomic writes; the three follow-up DECs are
   named, not promised.
10. **Bad-tick screen interplay** (§6.4): a deliberate `-G1S` option-bar sweep
    can silently skip REAL ≥4× premium moves under the default-ON screen —
    operator guidance documented; recalibration is a named follow-up.
11. **Market-bucket fold until the G1 edit lands** (§5.1): the companion edit
    is Phase-1-REQUIRED; until it merges, GDFL ledger rows would read as
    "spot" on `/coverage/windows`.
12. **NSE calendar coverage** is 2020–2026 inclusive today; 2027+ file dates
    refuse loudly until the calendar lands (fail-safe abstain).
13. **`-I` contamination risk** (R23): under `--assume-calendar-roll`, an
    early-rolling vendor would attribute near-expiry rows to the wrong
    contract — bounded to the flag's explicit, audited use; the probe decides.

### §9.2 UNVERIFIED register (each row names its verdict mechanism)

| Claim | Status | Mechanism until verified |
|---|---|---|
| Row-emission semantics (change-only vs heartbeat) | UNVERIFIED | `identical_consecutive_rows` counter (R22); derive laws emission-agnostic |
| CSV file-naming grammar | UNCONFIRMED | filenames provenance-only (§4.6) |
| Option expiry-fragment grammar | UNCONFIRMED | multi-grammar candidate law; `unresolved` blocks derive only (§4.4) |
| GDFL `-I` roll convention | UNVERIFIED | derive flag-gated; probe row (§9.3) |
| `-II`/`-III` serial-month semantics | UNVERIFIED | grammar accepts, resolution refuses |
| 15:30:00 emission | UNVERIFIED | `rows_postclose` + `post_grid_trades` counters |
| Crossed/locked/one-sided quotes | UNVERIFIED | stored verbatim + counted, never fixed |
| Zero bid/ask meaning | UNVERIFIED | stored 0.00; read-time interpretation |
| Blank-OI occurrence | UNVERIFIED (measured: none) | NULL + counted |
| Vendor re-dump shape | UNVERIFIED | R6 refusal ladder |
| ms timestamps | UNVERIFIED (measured: none) | strict format ⇒ FILE FAIL |
| Backward timestamps | UNMEASURED | FILE FAIL default (R9) |
| Dump availability beyond 01-Jul-2025 samples | UNVERIFIED | operator/vendor question (the date puzzle) |
| API depth per instrument class; `GetLimitation` units | NOT STATED | capture-leg probe (§9.5) |

### §9.3 Ingest-relevant probe list

The fuller 20+-row probe register (API/WS/credentialed gates) is the sibling's
— `docs/vendors/gdfl/VERDICT.md`, landing with PR #1506. The INGEST subset:

1. Real dump FILENAMES from the operator → confirm/extend the fragment-form
   seed set (unlocks option derives).
2. A delivery spanning an expiry week → decide the `-I` roll convention
   (unlocks flag-free futures derives).
3. Identical-consecutive-row counts over real deliveries → emission semantics.
4. A vendor re-dump of an already-delivered (ticker, day) → re-dump shape
   (prefix-grown vs reordered) → keep or soften R6.
5. 15:30:00-row presence per instrument class → the boundary counters' truth.
6. Blank-OI / crossed-quote incidence → whether the §4.3 KEEP classes fire.
7. GDFL native MINUTE candles for one sampled contract → the three-way
   cross-validation diff (§6.4) → measured volume-undercount rate.

### §9.4 Engine-track + DEC-LIVE-REMOVE-001 interaction

Waking any of this into the ENGINE would take: an ADR + operator scope decree;
a 1s-aware feed (`ReplayFeed` is bar-shaped); look-ahead guards for sub-minute
state; a run-identity extension debate (the `bxsnap2` preimage carries 7-field
bars, not 9-field snapshots); a materialized clean-1s store (the §6.1 pure
function is the ready seed). Deliberately unpromised. Separately: the entire
live-feed surface was removed in full (DEC-LIVE-REMOVE-001, MIGRATION.md
#862); this design ingests FILES only and re-introduces no live surface — any
WS/REST capture daemon requires a FRESH ADR (§9.5 records what it inherits).

### §9.5 Future capture/API leg — OUT OF SCOPE here; designed constraints to inherit

The sibling panel designed the API/WS leg in depth; those positions are
recorded here as INHERITED CONSTRAINTS for the future DEC + ADR — not expanded
into a recorder design (not this mandate). Vendor facts per
`docs/vendors/gdfl/api/REST_API.md`, `docs/vendors/gdfl/api/WEBSOCKET_API.md`,
`docs/vendors/gdfl/sdk/PACKAGES.md` (landing with PR #1506):

- **Per-source separation forever**: `ws_live` / `rest_hist` land in their own
  `<source>` subtrees (§3.1); cross-source agreement is a cross-VALIDATION
  report, never a merge; a REST backfill of a WS gap is never spliced into the
  `ws_live` series.
- **Epoch-unit law, per transport, covering ALL fields**: REST `TICK`
  `LASTTRADETIME` is epoch-MILLISECONDS; WS history samples are epoch-SECONDS;
  `GetLimitation.ExpirationDate` is epoch-MILLISECONDS (an s-parse puts the
  subscription-expiry dead-man in year ~56,000 — inert). DECLARED units per
  transport + a requested-window containment belt; magnitude-sniffing REJECTED
  (it silently adapts to a vendor unit change instead of failing loud). The
  vendor's own worked example ("1658138400" = 18-07-2022 15:30:00) proves
  Unix-UTC rendered as IST wall clock — still re-confirm at the 09:15 session
  boundary (the Groww epoch-verdict lesson).
- **WS dual-time law**: on trade-less stretches the WS keeps emitting ~1/s
  frames with UNCHANGED `LastTradeTime` while bid/ask evolve — quote-series
  derivations key on an observation time (`ts_obs` = server/recv time), trade
  fields on `ts_event` = `LastTradeTime`; history/CSV carry only LTT — the
  limitation is schema metadata.
- **`GetLimitation`-first planning**: entitlement snapshot persisted at every
  start; enabled-periodicity flags gate requests; a rate ramp is bounded by
  the entitlement it probes (stop at the FIRST throttle signal); entitlement
  DIFF between runs alerts (silent entitlement drift is how quiet truncation
  starts); served<requested with no error = `entitlement_truncated` demotion,
  never "vendor confirmed empty". `GetHistory` is ONE instrument per call —
  a chain backfill is contracts×days calls; the From/To boundary inclusivity
  is unprobed (duplicate seam rows by construction if inclusive-both).
- **`GetHistoryAfterMarket` as the nightly default** (vendor-recommended;
  structurally dodges still-settling-today — the
  DEC-PULL-TODAY-COMPLETENESS-GATE lesson: never ask for a day that is not
  closed); GDFL paced by the COMMON `RateController` with a FAIL-LOUD UNSET
  ceiling (no invented default).
- **Standalone recorder** (per DEC-LIVE-REMOVE-001): a self-contained daemon,
  ZERO brutex imports, TickVault-side deployable; BruteX only reads sealed
  spool files via a spool import; calendar-driven windows incl. Muhurat;
  reconnect ladder with machine-readable GAP RECORDS (absence stated, never
  interpolated); per-symbol staleness watchdog keyed on FRAME arrival (not
  LastTradeTime); finite frame-size bound (an SDK default of `max_size=2**50`
  is an OOM invitation); 1-WS-session-per-key single-flight; the WS raw
  journal (the sibling's tier-0 spool) lives HERE, not in the CSV path.
- **Security**: `accessKey` rides the URL query string — choke-point redaction
  across logs/audit/quarantined bodies/exceptions + a redteam pin; SSM
  SecureStrings for endpoint+key (the endpoint itself is secret-adjacent);
  require https/wss unless an explicit plaintext escape with a persistent
  UNVERIFIED-TLS stamp; probe transcripts with real rows stay OFF-repo.
- **`AdjustSplits` FORBIDDEN in any puller** (adjusted + raw in one keep-first
  store = silent corruption); `SubscribeSnapshot` bar-push frames are never
  normalized into the snapshot series; frames for never-requested instruments
  are journaled + excluded + counted; SDK-version asymmetry: prefer
  wsgfdl-py 1.3.5's surface where the 2022 protocol mirror disagrees;
  `ΔValue`/`AverageTradedPrice` (cum_value) is a second independent
  rupee-terms turnover anchor for the volume-honesty check.

---

## §10. Rollout

- **Phase 0 (THIS PR)**: this design doc + MIGRATION.md #1781 + the CLAUDE.md
  convention updates. Zero code. Lands beside/after the sibling vendor-docs
  tree (PR #1506 owns `docs/vendors/gdfl/`'s other files; this doc fills the
  slot its `INGEST_POINTER.md` reserves).
- **Phase 1 — CSV importer + raw store** (gated on: operator purchase
  confirmation + real dump filenames; fragment-grammar confirmation unlocks
  option derives later, not this phase). Modules, one-line scopes
  (placement per R24):
  - `brutex/adapters/gdfl/csv_parse.py` — the pure streaming CSV state
    machine + the §4.3 failure taxonomy.
  - `brutex/adapters/gdfl/ticker_grammar.py` — pure grammar + multi-form
    candidate resolution (+ `--symbol-map` fallback) → manifest metadata,
    never a storage key.
  - `brutex/data/snap1s_writer.py` — `Snap1sParquetWriter`: keep-first
    `(ts, seq)` merge, flock, atomic, containment, deterministic bytes,
    `Snap1sWriteResult`.
  - `brutex/data/snap1s_sources.py` — the §3.4 source-sha manifest
    (append-only, flocked) + the prefix-extend/refusal ladder.
  - `brutex/data/snap1s_ingest.py` — file→validate→write→ledger→audit
    orchestration + counters.
  - `brutex/dashboard/db_view.py` (ONE companion edit) —
    `_window_market_bucket` gains the `gdfl` bucket — BOTH `gdfl-*` market
    tokens + the counts-dict key (G1, §5.1).
  - `brutex/cli.py` — the `gdfl ingest` arm + `EXIT_INGEST_REFUSED=2`.
  - All new modules 100% line+branch on ci.yml-exact gates; the ledger-token
    compat pin (§5.1) + the no-raw-data repo pin (§8 row 40) ship here.
- **Phase 2 — derive + /db read surface** (its OWN DEC):
  - `brutex/data/snap1s_derive.py` — `collapse_to_1s` (pure) +
    `derive_1m_bars` + the `-G1S` symbol mint + the per-symbol kind-routed
    `materialize_lake_timeframes` loop (§6.4).
  - `brutex/dashboard/snap1s_view.py` — discovery + pager + count keys;
    db.html row + app.py routes.
  - `brutex/cli.py` — the `gdfl derive-1m` arm (`--assume-calendar-roll`).
  - The provenance-isolation integration test (§8 row 34) + the G1S drift pin
    (row 35) ship here, alongside the FULL `snap1s_derive`-family test set —
    the §8 rows 3/4/5/11/27/33/42/43/44 planned tests (gap/idle minutes
    ABSENT · quote-only minute has no bar + OI-from-quote-row ·
    duplicate-print collapse never overcounts · the close-instant drop ·
    the derive-conservation property (`test_snap1s_derive_properties.py`) ·
    the Muhurat real-session grid · the zero-bar SUCCESS ledger law · the
    `collapse_to_1s` last-wins/Σ-flow pin) — Phase-2 deliverables, mirroring
    the Phase-1 test callouts above.
- **Phase 3+ — each its own DEC**: `DEC-SNAP1S-CENSUS-001` →
  `DEC-SNAP1S-HEAL-001` → `DEC-SNAP1S-STATS-MANIFEST-001` (§3.5); the
  `brutex doctor --gdfl-diff` cross-vendor check (§6.4); an F&O-picker group
  for external-source contracts; the bad-tick-screen recalibration if G1S
  sweeps become routine; the API/WS capture leg (fresh DEC + ADR, §9.5).
- **Explicitly OUT** (this design, all phases named above): live WS/REST
  capture; any engine/vocabulary/scope change; a 1s backtest track; movers
  surfacing of GDFL data; instrument-master rows for G1S symbols; any new
  interval/segment/exchange token; Greeks enrichment of G1S bars; off-box
  replication of GDFL data before the licensing read; committing any raw GDFL
  bytes.

---

## §11. Reconciliation appendix

**Two-panel provenance.** OUR panel (this session): Design A (storage &
integrity lens), Design B (ingest lens — the WINNER whose text is the base),
Design C (consumer lens), folded by our judge into a single-position
synthesis; every §0.5 row traces to a judge-decided position. The SIBLING
session's panel (run before the scope split moved design ownership here;
relayed as a judge digest): integrity / ops (winner) / minimal lenses + 10
contradiction rulings + 8 all-three-missed defects + 15 design laws. The
sibling digest is INPUT MATERIAL — folded where it strengthens, rejected where
it conflicts with repo-grounded findings. Every accept/reject:

| # | Sibling position | Verdict | Deciding argument |
|---|---|---|---|
| A1 | `ticks/gdfl/raw\|l1\|bars` tree root + naming | REJECTED | The operator framing ruling post-dates it: the tree says what the data IS (`snap1s/` = 1-second L1 snapshots); `ticks/` violates the ruling. The `bars` tier maps onto our `-G1S` namespace inside the REAL `bars/` tree instead (R14). |
| A2 | Per-source partitioning (L-3; csv_dump/ws_live/rest_hist; cross-source = report, never merge) | **ACCEPTED** (one path level) | Cheap now, structural forever: prevents the union-merge determinism break the sibling's own judge deducted (seq as a function of merge arrival order; divergent market views laundered). Amends our J2 layout. |
| A3 | Per-DAY partition files | REJECTED | Month files are the exact `options/` precedent + the ONE stem law; the idempotence unit is carried by the per-(instrument, day) ledger window + the R6 byte-identity law; measured volumes make month files trivial. |
| A4 | Two-tier raw store (L-1: immutable spool + re-derivable parquet) for CSV ingest | REJECTED for CSV v1; tier-0 ACCEPTED for the WS capture leg (§9.5) | The operator's vendor CSV IS the raw artifact — a spool copy of it is redundant; the sha-manifest + deterministic re-ingest (§3.4) deliver the same delete-and-re-derive guarantee; the optional `--archive-raw` covers operators who want the copy in-lake. The WS stream has no vendor artifact, so THERE the spool is tier-0 truth. |
| A5 | R-4: fold ALL rows' LTP into OHLC (default) + `open_is_stale` flag + config switch | REJECTED as default; divergence documented + probed | Sample evidence makes the two laws value-identical IF LTP only moves on trades — UNVERIFIED, so the default must be the one that degrades more honestly: LTQ>0-only fails toward ABSENT bars (honest absence); all-rows fails toward FABRICATED flat bars through the measured 698 s idle gaps (Golden Rule #1). No config switch — one law, no silent variants; the cross-validation diff (§6.4) measures the real difference. |
| A6 | R-5: exact-duplicate collapse-and-count (Σ LTQ must never overcount) + L-7 volume/OI honesty | **ACCEPTED, split by layer** | RAW store keeps duplicates (dedup would ASSUME change-only semantics — archival is evidence); the DERIVE collapses payload-identical prints before summing LTQ (`dup_prints_collapsed`), so the `volume_basis="sum_ltq_undercount"` lower-bound stamp (folded ~verbatim) stays true under either emission model. OI: last-obs-in-minute kept; the sibling's cross-minute `oi_carried` carry REJECTED for derived bars — the frozen 7-column schema has no flag column, so a carry would be undisclosed staleness; NULL-before-first-observation + never-overnight hold trivially. |
| A7 | D-5: superset-accept on re-serve | **ACCEPTED, narrowed** | Adopted as ordered ROW-prefix extend only (§3.4 — parquet-baseline post-normalization comparison; the byte-prefix check only under `--archive-raw`): a general (ts, payload)-superset with mid-second insertions would remap seq↔payload and break the R6 stability contract; the prefix arm covers the real grown-re-dump case with zero seq risk. |
| A8 | R-6: refuse `-I` continuous-contract STORAGE in v1 | REJECTED for storage; **ACCEPTED at the resolution layer, narrowed** | Our mandate's own samples are `-I` files; verbatim-ticker day-file storage never launders a roll guess (the refusal buys zero archival safety). The real hazard — resolving `-I` into a specific contract when GDFL's roll convention is UNVERIFIED — is gated behind the explicit, audited `--assume-calendar-roll` (R23), with the probe row deciding. |
| A9 | Minimal §C.6: SEPARATE WindowLedger at the GDFL tree root (structurally invisible to Groww rollups) | REJECTED | `lake_revision` stats the LAKE-ROOT ledger — a scoped ledger moves NO dashboard freshness token, so every /db view stays stale unless `lake_revision` grows a component (a bigger brutex/ edit than the G1 one-map fix, plus a second ledger to operate). The invisibility goal is met better by the `gdfl` display bucket: visible, correctly labeled, never inflating SPOT counts. The sibling's compat-gate demand (ledger tokens asserted-never-checked) ACCEPTED as a Phase-1 pin (§5.1). |
| A10 | L-13: licensing/security — raw data never committed; synthetic fixtures + repo pin; tree EXCLUDED from off-box replication until the agreement is read | **ACCEPTED in full** (supersedes our panel's "backup rides the whole-root include set" default) | The stricter posture costs nothing and the whole-root inclusion was our design's own flagged blocker; extended per our judge's G5 to the derived `-G1S` bars explicitly (derived-data clauses commonly restrict redistribution too). A named divergence from DEC-BACKUP-OFFBOX-001. |
| A11 | R-9: code placement `feeds/gdfl/` one-way-import; promotion = future DEC | REJECTED; staging idea honored | The importer must reuse `brutex.data` COMMON primitives either way; the /db surface + CLI unavoidably live in `brutex/`; a promotion DEC is churn without safety. The zero-churn instinct is honored by the phase split: Phase 1 touches existing display code at exactly ONE map (G1). |
| A12 | Edge-register: zero bid/ask ⇒ NULL ("0 is not a price") | REJECTED | The raw store never edits vendor values — a sent literal 0 is stored 0.00 + counted; "0 is not a price" is a READ-time interpretation the collapse/consumers may apply. Blank ⇒ NULL is kept (absence is absence). |
| A13 | Edge-register: restatement-storm breaker (conflicts above threshold ⇒ refuse merge) | NOT NEEDED (structurally subsumed) | Under R6, a different-bytes re-dump is refused BEFORE any merge; keep-first conflicts can only arise from identical-byte crash re-runs, which cannot conflict. The breaker would guard a path that cannot occur; the conflict counter stays as the tripwire. |
| A14 | (Our own panel, recorded for reviewers) `G1S-` PREFIX for derived bars; `FNO1S` segment; `-GDFL` suffix | REFUTED / REJECTED / SUPERSEDED | Prefix PARSES as exchange="G1S" and joins the real chain (grammar-verified) — the exact pollution the tag exists to prevent; segment widens the most load-bearing frozenset + chases every `exclude_segments` site; `-GDFL` collides with a future native-minute ingest. `-G1S` is the survivor (§6.3). |
| A15 | Audit actor "gdfl" (sibling) / actor "ingest" (our synthesis) | BOTH corrected mechanically | The repo's real convention: `open_ingest_audit(name=…)` discriminates by CHAIN FILE (`spot-ingest`/`fno-ingest` → `gdfl-ingest`); the writer's actor field keeps its default. Neither panel's spelling matched the tree; the mechanism does. |
| A16 | L-11 session/boundary laws; 15:30:00 inclusive-store/exclusive-derive; L-14 idempotence (atomic_io + partition_lock verbatim; 2dp snap counted); header-drift refusal naming the diff; orientation-not-sorting; empty-on-trading-day = re-pullable FAILED-class | **ACCEPTED ~verbatim** (already congruent with our synthesis; deltas folded) | Same laws, independently derived by both panels — the strongest possible signal they are right. The snap-counted detail (`price_snap_adjusted`) and the named-diff refusal are the two genuinely new folds. |
| A17 | Sibling API/WS/recorder designs: L-8/L-9/L-10/L-12, R-1/R-2/R-3, D-1/D-2/D-3/D-4/D-6/D-8, G-2 redaction, entitlement truncation, AdjustSplits/SubscribeSnapshot laws | DEFERRED-INHERITED (§9.5) | Out of this mandate (CSV-only v1; DEC-LIVE-REMOVE-001); recorded as binding constraints the future capture-leg DEC/ADR inherits, citing the sibling's docs (PR #1506) rather than re-deriving. |
| A18 | Sibling L-5 ticker ambiguity law (multi-grammar candidates {DDMMMYY, YYMMM-monthly, YYMDD-weekly} + master/expiry-calendar validation; exactly-one ⇒ accept, else never guess) | **ACCEPTED, merged** | Folded into our registry-backed candidate mechanism as the grammar seed set (§4.4); the sibling's quarantine-on-failure and our panel B's file-refusal are BOTH superseded by the R4 decoupling — archival never blocks on resolution; the refusal moves to the derive, where a wrong guess would actually do damage. |
| A19 | Sibling G4-class gap (none of the six designers handled a same-day partial dump) — our judge's finding | ACCEPTED (R26) | A today-dated mid-session dump collides with the byte-identity law on the fuller re-dump; refuse-before-close + `--allow-today` closes it. Credited to our judge; recorded because the sibling digest's D-5 discussion is what surfaced the class. |

**Net effect of reconciliation on our judge synthesis**: R2 gains the
`<source>` path level (A2); R6 gains the prefix-extend arm (A7); R13/R12 gain
the derive-layer duplicate collapse (A6); R19 flips backup from include to
EXCLUDE (A10); R23 narrows futures-`-I` derives behind the roll flag (A8);
R17's actor spelling is corrected to the chain-name mechanism (A15); R25/R26
are new rows (A6/A19). Everything else stands as judge-decided.
