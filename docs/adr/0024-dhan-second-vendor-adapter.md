# ADR-0024 — Dhan Data API v2 as a SECOND Vendor Adapter (Lake/Reference-Only, Subscription-Gated)

**Decision ID:** `DEC-DHAN-ADAPTER-001` (MIGRATION.md — provisional entry;
renumber-at-landing per DEC-LEDGER-RENUMBER-001)
**Date:** 2026-07-18
**Status:** ACCEPTED — operator-directed, relayed via the coordinator.
**Author:** Claude Code (coordinator-directed)
**Relationship to ADR-0009:** does NOT supersede it. Groww remains the live
Track-1 vendor (`DEC-DHAN-GROWW-REMIGRATE-001`); this ADR adds Dhan back as a
**second, fully-built but INERT** vendor adapter — never a re-migration.

---

## 1. Context

The operator directed a second data-vendor adapter for the lake, mirroring the
Groww adapter shape 1:1, targeting the **DhanHQ v2 Data API** — a paid add-on
(₹499 + taxes/month, VERIFIED) covering Historical, Intraday, Option Chain and
Expired-Options endpoints. The subscription is NOT yet purchased and no
credentials are provisioned, so the adapter must be **READY but INERT**: no live
API calls, no wiring into production pulls, no engine-scope change (the engine
stays NIFTY/BANKNIFTY/SENSEX spot per DEC-ENGINE-SPOT-ONLY-001).

BruteX previously shipped a full Dhan adapter (ADR-0002/0003 era, deleted at the
ADR-0009 Groww re-migration). This build is a FRESH, Groww-shape-mirrored
adapter against the current DhanHQ **v2** REST API — not a resurrection of the
deleted v1-era code.

## 2. Decision

Build `brutex/adapters/dhan/` as a 16-module package mirroring
`brutex/adapters/groww/` leaf-for-leaf, with:

* **Pure, I/O-free law leaves** — `constants.py` (facts + resolvers),
  `errors.py` (HTTP-status + DH-9xx classification), `chunks.py` (window
  arithmetic), `parser.py` (column-array / rolling-option / option-chain /
  expiry parse), `instruments.py` (header-driven scrip parse + symbol→
  security_id keying + segment mapping), `capability_matrix.py` (the honest
  verified-vs-unverified record as DATA). Each is import-light (stdlib + the
  shared vendor-neutral surface only; no client/network/AWS imports) — the
  coordinator condition enabling a 1:1 Rust-facade wrap later (§5).
* **The injected transport seam** — `transport.py` declares the
  `DhanTransport` Protocol (`post_json`/`get_json` → `(status, json)`), with
  the real `requests`-backed transport confined to a `# pragma: no cover`
  factory. All tests use `tests/fakes/fake_dhan_transport.py`.
* **The client layer** — `client.py` (pace → classify → retry/reauth ladder +
  `CallMeter`, the special 1-req/3s option-chain pacer seam), `pool.py`
  (K leaves, one shared meter), `reauth.py` (thread-safe single-flight
  read-only token re-read; a same-token re-read raises the RETRYABLE
  `DhanTokenStillStaleError`).
* **AWS-only credentials** — `credentials.py` + `token_store_aws.py` REUSE the
  shared `AwsClient` Protocol + `CredentialsError` (never a copy); the Dhan
  SecureStrings live at `/tickvault/prod/dhan/{access-token,client-id}`
  (env-overridable), read fail-fast, mint NEVER (read-only broker). The
  conftest AWS-hermeticity belt covers the new module bindings.
* **Orchestration glue** — `option_chain.py` + `expired_options.py`
  (fetch→parse over the injected client; expired options are ATM-relative
  MACRO addressing — no dated-contract resolution; expired FUTURES have NO
  fetch path, deliberately) and `adapter.py` (`DhanMarketAdapter` implementing
  the market-neutral `MarketAdapter` + `DHAN_CAPABILITIES`).
* **Distinct ledger namespaces** — `dhan` / `dhan-option` / `dhan-future`, so
  Dhan windows can never collide with Groww's on the shared lake ledger.

Scope: **LAKE / REFERENCE_ONLY, GATED ON SUBSCRIPTION.** ENGINE UNCHANGED.

## 3. Python-first, NOT born-Rust — a CONSCIOUS deviation (named loudly)

The operator's standing directive is Rust-first for new compute. This adapter
deliberately ships **pure Python**, as a CONSCIOUS, ADR-0020-grounded SCOPING of
that directive — not an oversight:

* The repo's Rust seams are ALL default-OFF, Python-oracle-first, governed by
  ADR-0020's mandatory banner: **"expected payoff ≈ 0 — conversion is for
  uniformity, not speed."**
* A vendor adapter is **I/O-bound** — network latency + the lake write dominate;
  the parse/dedup/mapping CPU is nanosecond-scale, so an FFI hop is a measured
  SLOWDOWN, not a win.
* A "born-Rust" kernel has **no retained Python oracle** to burn-in against,
  breaking the repo's proven parity discipline (burn-in bit-compare +
  ship-the-oracle autorevert requires the Python law to exist FIRST).

Every pure law therefore lives in its own I/O-free leaf so an OPTIONAL
`dhan_*_rust.py` facade can wrap each 1:1 with no refactor if a cross-repo
uniformity mandate lands — the named follow-up **`DEC-DHAN-RUST-FACADE-001`**
(deferred; NOT this PR).

## 4. Phase-0 probe gate — the `_UNVERIFIED` register

Every `_UNVERIFIED`-stamped fact is a live-probe gate. A credentialed probe
(after the operator subscribes + provisions the SSM SecureStrings) must close:

1. **Candle price unit** (rupees-vs-paisa) — parsed to exact Decimal either
   way; the finding only moves the lake-write boundary, never the parser.
2. **Daily max-range-per-call** — no documented cap; conservatively tiled at
   365 days (`DHAN_WINDOW_DAYS_DAILY_UNVERIFIED` — over-tiling is always safe).
3. **Option-chain per-strike Greek field NAMES** — the parser surfaces the raw
   per-strike mapping and extracts only nameable fields
   (`nameable_option_chain_fields`, assumed keys stamped).
4. **Exact scrip-master `SEM_`-prefixed headers + order** — the parser is
   header-driven (by name, order-tolerant); `DHAN_SCRIP_HEADERS_UNVERIFIED`
   pins the depended-on names with a drift-guard test.
5. **DH-902 / DH-903 / DH-904 / DH-907 meanings** — model-inferred; mapped
   conservatively to the terminal base (never blindly retried).
6. **Expired-futures history** — ABSENT/undocumented; no fetch path provided.
7. **Subscription + credential provisioning** — the ₹499/mo Data-API add-on +
   the `/tickvault/prod/dhan/*` SecureStrings (the master gate: until it
   closes, the adapter is inert by construction).
8. **Earliest-history floor** — daily "from inception" / intraday rolling
   5 years; `EARLIEST_HISTORY_UNVERIFIED` is a conservative placeholder.

The machine-readable register is
`brutex.adapters.dhan.capability_matrix.DHAN_UNVERIFIED_PROBE_GATES`
(golden-pinned).

## 5. Consequences

* Groww remains the ONLY live vendor; nothing consults the Dhan adapter in any
  production path (no CLI/dashboard/pull wiring — deliberately out of scope).
* CI gains 15 additive per-module 100% coverage stanzas + 5 mutmut pairs for
  the pure-law leaves (constants/errors/parser/chunks/instruments). The
  client/pool/orchestration modules stay out of the mutmut loop (behavioral
  seams over the injected transport — the combo.py hand-kill precedent);
  their coverage gates + the integration suite are the teeth.
* `tests/stress/` deliberately gains NO entry: the repo's stress tier drives
  sustained event volume through live-shaped subsystems; an INERT adapter with
  no production wiring has no sustained-load surface to soak (Golden Rule #10 —
  a fabricated stress test would be gate theater). Revisit at live wiring.
* The future live wiring (rate-controller binding at 5/s + the 3s option-chain
  pacer, ledger namespaces, pull spine integration) is its own DEC when the
  subscription lands.
* **`DEC-DHAN-RUST-FACADE-001`** (deferred): optional Rust facades over the
  frozen pure-law leaves, only under a cross-repo uniformity mandate, with the
  Python law retained as the live oracle (the ADR-0020 pattern).
