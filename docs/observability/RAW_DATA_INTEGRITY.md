# Raw-Data Integrity Layer — provable "the lake derives from EXACTLY what Groww sent"

> **Decision ID:** DEC-RAW-DATA-INTEGRITY-001 (`docs/MIGRATION.md` #934)
> **Status:** Implemented (capture + verify core + ingest wiring); the verbatim
> raw-payload archive for full offline replay is DESIGNED-ONLY (future, gated).
> **Scope:** purely ADDITIVE — changes NOTHING about existing storage. The
> 2-decimal rounding (DEC-SPOT-PRICE-2DP-001), the parse path
> (`brutex/adapters/groww/parser.py`), and the lake writer
> (`brutex/data/parquet_writer.py`) are UNTOUCHED. We are adding PROOF, not
> altering data.

---

## 1. The problem (the gap this closes)

The lake stores OHLC bars that are a **deterministic function** of the raw Groww
vendor payload. The transforms applied between "bytes Groww sent" and "bytes on
disk" are, in order:

1. `parse_candle_rows` (`brutex/adapters/groww/parser.py`) iterates the raw
   row-of-arrays `{"candles": [[ts, o, h, l, c, v, oi], …]}`.
2. `assemble_candle` (`brutex/data/candle_fields.py`):
   - prices parsed to exact `Decimal` via `str(...)` (no float drift);
   - **skip-empty** — a no-trade / partially-absent / zero-price bar is dropped
     (`EmptyCandleRow`);
   - **OHLC clamp** — `high = max(o,h,l,c)`, `low = min(o,h,l,c)`
     (DEC-OHLC-VENDOR-NORMALIZE-001);
   - **non-negative clamps** — negative volume / OI → 0.
3. **IST → UTC** timestamp localisation (`parse_timestamp_ist`, fixed +05:30).
4. **2-decimal `ROUND_HALF_UP` snap** at the lake-write boundary
   (`brutex/money.py::round_money`, DEC-SPOT-PRICE-2DP-001).
5. **keep-first** dedup on conflict (the stored bar wins; Golden Rule #7).

All five are pure, deterministic functions of the raw payload — so given the raw
payload, the stored lake is reproducible.

**But until now nothing recorded WHAT the vendor actually sent.** The existing
`ParquetWriter.raw_hash` is a `sha256` of the **stored Parquet file bytes**
(`brutex/data/parquet_writer.py:344,399,411`). That proves only that the file on
disk has not changed — it does NOT prove the file derives from the un-tampered
vendor response. If a payload were tampered with in flight, or the lake silently
drifted from its source, the file-hash would happily certify the tampered data.
**That is the gap this layer closes.**

---

## 2. Design

### 2.1 Content hash of the RAW payload (at capture, before any transform)

For each fetched window, compute a content hash of the **raw vendor candle
rows** — keyed by `(exchange, segment, groww_symbol, interval, window_start,
window_end)` — at the point the response arrives, **before** `parse_candle_rows`.

* **Canonicalisation** (`raw_integrity.canonicalize_payload`): the payload's
  `candles` row-of-arrays is serialised as compact JSON with **sorted keys** and
  **no whitespace**, so two byte-identical candle series always canonicalise to
  the same bytes regardless of dict-ordering or whitespace in the original
  response. Only the `candles` rows are hashed (not the whole mapping), so
  vendor envelope noise (a status field, a server timestamp) can never flip the
  hash of an identical candle series — the candle rows ARE the data the lake
  derives from. A missing `candles` key canonicalises to `[]` (a genuinely-empty
  vendor window is a real, hashable state, not an error).

* **Hash** (`raw_integrity.hash_payload`): `hashlib.blake2b(..., digest_size=32)`
  → a 64-hex-char digest. The repo's run-identity hash is `blake3` (CLAUDE.md
  Golden Rule #4), and `blake3` is a declared dependency — but it is not
  guaranteed importable in every pure-python environment (no native wheel). This
  PURE module therefore uses the **stdlib `blake2b`** (no native dependency,
  cryptographically strong, deterministic across processes / Python builds). The
  record stamps its `algorithm` field (`"blake2b"`) so a future migration to a
  native `blake3` can coexist without re-hashing.

### 2.2 Sidecar persistence (the candle lake is NOT touched)

The hash is persisted in a **separate append-only JSON-lines sidecar** at
`<lake_root>/raw_integrity/records.jsonl` (`raw_integrity_store`):

* It lives **outside** `bars/` / `options/` under its own `raw_integrity/`
  directory — no candle partition, no lake schema, and nothing the lake scanner
  reads is changed.
* **Append-only** (not in-place rewrite): one JSON line per captured window,
  O(record), never reads/rewrites prior records, so a crash mid-write can at
  worst leave a partial trailing line (skipped on load) — never loses or
  corrupts earlier records.

**Why a sidecar over extending the window ledger** (the lower-risk additive
option, justified): the window ledger
(`brutex/data/window_ledger_store.py`) is a load-bearing, heavily-tested lake
Parquet table with a fixed schema, a load/save/migrate path, and a graceful
disk-full halt. Adding a `raw_payload_hash` column would (a) touch that
load-bearing table and (b) couple the proof to the ledger's lifecycle —
strictly more risk than an isolated sidecar. The sidecar keys by the SAME
coordinate tuple, so a future ledger join is trivial if ever wanted.

### 2.3 Capture wiring (additive, best-effort, can never break a pull)

The integration point is the ONE COMMON tiling-fetch primitive both candle paths
funnel through:

* `brutex/adapters/groww/candles.py::fetch_candle_windows` (the spot
  `fetch_ohlcv` AND the option `fetch_option_candles` both call it). It gains an
  optional `on_raw_payload: RawPayloadHook | None = None` callback, fired ONCE
  per fetched window with the **raw payload + the exact issued coordinates**,
  BEFORE `parse_candle_rows`. `None` (the default) makes the fetch
  byte-identical to the pre-feature path.

* The hook factory `raw_integrity_store.make_capture_hook` ties
  `build_record` (hash) + `append_record` (persist) into a **best-effort**
  closure: every error — a malformed payload (`RawIntegrityError`) or a sidecar
  write failure (`OSError`) — is caught, logged at WARNING, and swallowed, so the
  candle fetch+store proceeds exactly as if the hook were absent. **A hash
  failure can never halt or corrupt a pull** — the candle write is the source of
  truth; the hash is additive proof, never a gate.

* The hook is threaded:
  - SPOT: `GrowwMarketAdapter` carries an optional `on_raw_payload` field
    (excluded from eq/repr — observability state, not identity) → passed to
    `fetch_candle_windows` in `fetch_ohlcv`. No `MarketAdapter` protocol change.
  - OPTION: `fetch_option_candles` / `ingest_option_contract` /
    `run_groww_option_ingest` each gain an optional `on_raw_payload` pass-through
    param (default `None`).

### 2.4 Verify path (the provable "no drift since capture" check)

* PURE core: `raw_integrity.verify_record(record, payload)` re-hashes a presented
  payload and compares it to the recorded hash → a `VerifyResult` (`matches`,
  `recorded_hash`, `actual_hash`, `key`). No network.

* Thin re-fetch layer: `brutex/adapters/groww/integrity_verify.py`:
  - `verify_one(client, record)` re-issues `historical_candles` with the recorded
    coordinates (the same client transform the pull used) and verifies.
  - `verify_lake_records(client, lake_root)` loads every sidecar record and
    verifies each, returning a `VerifyRun` summary (`total`, `matched`,
    `mismatched`). A mismatch is logged at WARNING. This is the
    `brutex verify-integrity`-style core; a CLI is a thin wrapper over it (the
    re-fetch wiring is intentionally a thin layer over this pure summary).

---

## 3. What this proves — and what it does NOT (honest envelope, Golden Rule #10)

**It proves:**
- the RECORDED hash matches the raw payload AT CAPTURE TIME, and
- the lake is a DETERMINISTIC function of that exact payload (§1 transforms),

so any later tampering or drift of a re-fetched window is caught by a hash
mismatch in `verify_record` / `verify_lake_records`.

**It does NOT:**
- **retroactively prove pre-existing lake data** — no historical raw payloads
  exist for windows pulled before this layer shipped, so `verify_record` can only
  attest windows captured from this point forward;
- **distinguish a legitimate vendor RESTATEMENT from tampering** — both surface as
  a mismatch; the operator investigates (the recorded vs actual hash + the key
  are reported);
- **archive the verbatim raw payload** — §4 (designed-only, future).

---

## 4. Future (designed-only, gated — NOT in this PR)

A **verbatim raw-payload archive** for full offline replay: storing each window's
exact bytes (compressed) alongside its hash, so a verify can replay WITHOUT a live
re-fetch (and re-derive the lake to prove the §1 transform chain end-to-end). This
is gated separately because storing every raw payload for the whole 2020→today ×
universe span is heavy (storage + write throughput); it would land as its own
decision with its own size/retention envelope. The current sidecar already records
the hash + coordinates, so the archive is purely additive on top.

---

## 5. Files

| File | Role |
|------|------|
| `brutex/data/raw_integrity.py` | PURE core — canonicalize, blake2b hash, `RawIntegrityRecord`, `build_record`, `verify_record` |
| `brutex/data/raw_integrity_store.py` | durable append-only sidecar + the best-effort `make_capture_hook` |
| `brutex/adapters/groww/integrity_verify.py` | thin re-fetch verify layer (`verify_one`, `verify_lake_records`) |
| `brutex/adapters/groww/candles.py` | the COMMON `fetch_candle_windows` capture seam (`on_raw_payload`) |
| `brutex/adapters/groww/adapter.py` | spot wiring (`GrowwMarketAdapter.on_raw_payload` field) |
| `brutex/adapters/groww/option_ingest.py` | option wiring (`on_raw_payload` pass-through) |
