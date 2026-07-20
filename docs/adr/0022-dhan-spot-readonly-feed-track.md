# ADR-0022 — Dhan read-only SPOT feed track (standalone, `feeds/dhan/`)

**Decision IDs:** `DEC-DHAN-SPOT-FEED-001` (MIGRATION.md entry #1797, umbrella),
`DEC-DHAN-READONLY-AUTH-001`, `DEC-DHAN-SPOT-PULL-001`,
`DEC-DHAN-PULL-SURFACE-001`, `DEC-DHAN-FEED-BINDING-001`,
`DEC-DHAN-GUARD-RETIRE-002`, `DEC-DHAN-HONEST-DEGRADE-001`
**Date:** 2026-07-12
**Status:** **ACCEPTED** — shipped on `claude/dhan-spot-feed`, a standalone
read-only Dhan v2 SPOT feed track built on top of the feed interface + feed-tagged
lake root (ADR-0021).
**Author:** the Dhan-spot-feed build.
**Builds on:** ADR-0021 (the `FeedBinding` interface + the `feeds/<feed_id>/`
lake-root nesting law, `DEC-FEED-INTERFACE-001` / `DEC-FEED-LAKE-ROOT-001`), the
canonical partition law (`brutex/data/partition.py`), the `MarketAdapter.fetch_ohlcv`
fetch waist (`brutex/adapters/protocol.py`), the keep-first merge-on-write dedup
(`DEC-LAKE-WRITE-UNIQUE-001`), the COMMON orchestrator hash chain
(`brutex/orchestrator/audit.py`, Golden Rule #8), and the read-only AWS-SSM
credential pattern (`brutex/config/credentials_aws.py`, `DEC-AWS-ONLY-CREDS-001`).
**Vendor history (why "Dhan" is here at all):** ADR-0001 pivoted Groww → Dhan;
ADR-0002 reverted Dhan → Groww; ADR-0009 (`DEC-DHAN-GROWW-REMIGRATE-001`) re-migrated
the AUTHORITATIVE backtesting vendor back to Groww and DELETED the Dhan adapter
package, after which a CI `no-dhan-guard` banned the literal token `dhan` on the
code surface. This ADR RE-ADMITS Dhan — not as the authoritative vendor, but as a
standalone, read-only, engine-independent SPOT feed — and therefore RETIRES that
guard (`DEC-DHAN-GUARD-RETIRE-002`, §6).
**Named follow-ups minted here:** `DEC-FEED-SPINE-GENERALIZE-001` (ADR-0021 — the
lean Dhan spot spine is option (b): drive the durable-ledger / parallel-fan-out /
`run_until_clean` binding fields from a real second consumer),
`DEC-DHAN-MOVERS-FEED-AWARE-001` (make the post-pull movers warm feed-aware so a Dhan
pull warms the Dhan lake, not the top Groww tree — deferred; a Dhan pull warms
NOTHING today rather than warm the wrong tree, Golden Rule #1).

---

## 1. Context

Track 1's authoritative vendor is Groww (ADR-0009). The operator wants a **second,
independent** historical price source for the three brute-force spot indices
(NIFTY / BANKNIFTY / SENSEX) plus INDIA VIX (REFERENCE_ONLY) — Dhan's Data API v2 —
without touching a single byte of the Groww write path or the frozen engine identity.

The two hard constraints are:

1. **Groww byte-identity.** The Groww adapter (`brutex/adapters/groww/*`), the
   byte-critical write stack (`brutex/data/{models,partition,parquet_writer,sink,
   catalog}.py`), and the frozen engine goldens (`_GOLDEN_CONTENT_HASH` /
   `_GOLDEN_RUN_ID`) must reproduce EXACTLY. Dhan is additive; it never edits those.
2. **Shared broker account, single active token.** The Dhan Data API token this feed
   reads is the SAME token TickVault mints and actively uses. A stray mint / renew /
   `PutParameter` from BruteX would rotate that single active token and INVALIDATE
   TickVault's live feed. BruteX must be a strictly READ-ONLY consumer.

This ADR records how the standalone track satisfies both.

---

## 2. Decision 1 — a standalone Dhan v2 SPOT feed, SPOT-only scope

The Dhan track is a self-contained adapter package (`brutex/adapters/dhan/`:
`constants`, `errors`, `parser`, `chunks`, `client`, `scrip_master`,
`token_store_aws`, `reauth`, `live_controller`, `adapter`, `ingest`) that pulls **only
1-minute SPOT index candles** from `POST /charts/intraday` on the shared `IDX_I`
exchange-segment (`INSTRUMENT_INDEX="INDEX"`). The scope is deliberately narrow —
NIFTY / BANKNIFTY / SENSEX (the ENGINE surface) + INDIA VIX (REFERENCE_ONLY,
excluded from `DHAN_ENGINE_INDEX_SYMBOLS`); **no options, no expired futures** (an
operator A/B hold). The 19 derived intraday timeframes + the daily anchor are DERIVED
from the stored 1-minute by the existing prebuild stage, never separately fetched
(CLAUDE.md §3).

Every value in `constants.py` is traceable to the VERIFIED DhanHQ-v2 research (the
official `dhanhq` SDK read verbatim + official-doc snippets); the timestamp rule is
Unix-epoch-seconds → UTC → IST by a FIXED +05:30 (SDK
`convert_to_date_time`). Values that are live-only and cannot be confirmed in-sandbox
carry the `_UNVERIFIED` honesty suffix (§4).

The Dhan candle parser drops index volume (an index carries no traded volume) and
reuses `brutex/data/candle_fields` for field rules; the errors module maps the
string-coded Dhan responses (DH-901 `Invalid_Authentication`, DH-905
authorization/subscription) to a neutral `DhanError` taxonomy with
`is_retryable` as the single retry-policy source.

**Rationale for a standalone package over a shared spine.** ADR-0021 shipped the
`FeedBinding` interface but PR-2 registered only the default feed. Dhan is the REAL
SECOND CONSUMER (`DEC-DHAN-FEED-BINDING-001`), so the honest engineering move was to
build the concrete Dhan surface first — a lean, self-contained spot spine — and
DEFER the generalization of the durable-ledger / parallel-fan-out spine to
`DEC-FEED-SPINE-GENERALIZE-001` once there is a second consumer to drive its field
shapes (rather than guessing them). The lean spine
(`ingest.run_dhan_spot_ingest_with_retry`) proves the feed end-to-end (resolve → plan
→ fetch → session-filter → sink → bounded retry) with per-window outcomes.

---

## 3. Decision 2 — READ-ONLY SSM auth: never mint, reauth = SSM re-read

The Dhan read path needs exactly two strings: `client_id` (the public account id, an
SSM `String`) and `access_token` (the 24-h JWT, an SSM `SecureString`). Both are
minted by a dedicated Lambda / TickVault into AWS Systems Manager Parameter Store at
`/tickvault/prod/dhan/{client-id,access-token}` (env-overridable path names; region
resolved via the shared token-broker resolver). **BruteX only `GetParameter`s them
and NEVER mints / renews / `PutParameter`s** (`DEC-DHAN-READONLY-AUTH-001`).

- `brutex/config/credentials_dhan_aws.py::load_dhan_aws_credentials` is the SOLE
  reader — the direct mirror of `load_groww_aws_credentials`: fail-fast, no silent
  fallback (a missing boto3 extra / absent-or-blank parameter / any SSM/IAM/KMS/
  network error raises a clear `CredentialsError` naming the fix), **no env-var /
  local-file / webpage fallback**, and it imports NO writer seam (no `ssm:PutParameter`
  grant — Groww parity). A partial fetch (client-id present, token absent) raises.
- `brutex/adapters/dhan/reauth.py::SdkRefresher` is the Groww refresher's twin with
  its **local-mint arm structurally absent**. Cold start builds the SDK from a
  bounded-retry SSM read (fail-loud if still absent — it never mints); a DH-901
  (`DhanAuthError`) triggers a thread-safe, single-flight, freeze-guarded **SSM
  RE-READ** (`_reread_and_adopt_sdk`) that adopts a rotated token, or raises the
  RETRYABLE `DhanTokenStillStaleError` when SSM still returns the dead token (the
  minter has not rotated yet — the window quarantines and re-pulls later, never a loop,
  never a restart). There is no disk token cache and no 06:00-IST session anchoring (a
  Dhan token is a plain non-blank JWT string).

**Token lifecycle owner:** TickVault. A persistently-stale Dhan token is remediated by
TickVault's minter, never by a BruteX-side write — the same posture Groww's read-only
broker takes (`DEC-BROKER-READONLY-REAUTH-REREAD-001`).

---

## 4. Decision 3 — hardcoded verified-pending index ids + honest degrade states

Dhan addresses an instrument by a numeric `securityId` on a segment. The three engine
indices + INDIA VIX resolve through a HARDCODED map (`DHAN_INDEX_SECURITY_IDS`, the
single source of truth — no runtime live-CSV loader, a coordinator ruling):
`{NIFTY: 13, BANKNIFTY: 25, SENSEX: 51, INDIAVIX: 21}`. NIFTY 13 is official (SDK
README `security_id=13`); the other three are in-repo-verified (MIGRATION #72/#87 +
TickVault CSV pins) but **verified-PENDING** — the read-only probe kit
(`docs/runbooks/feed-probe/probe_feeds.py`, §5) is the ONE-TIME cross-check that
confirms each id against Dhan's public compact scrip-master CSV. INDIA VIX is 21 (NOT
26 — a known third-party mirror error), recorded so the probe can catch a regression.

Two honesty invariants (`DEC-DHAN-HONEST-DEGRADE-001`, Golden Rule #1) keep the feed
from ever fabricating a bar or a "verified" flag:

- **A symbol ABSENT from the map is loud-skipped** BEFORE any fetch
  (`dhan.index_unverified` WARN naming the index; the window is left RE-PULLABLE as
  `skipped_unresolved`) — never a blind securityId.
- **empty-from-vendor and UNVERIFIED-servability are structurally distinct.** A
  holiday / no-session / too-old span that returns empty bars is a recorded SUCCESS
  window with zero bars (clean-empty, never a failure). Servability itself — does Dhan
  actually serve 1-minute history for THIS index back to some floor? — is UNVERIFIED
  until a credentialed probe transcript lands: the per-index
  `*_SERVABILITY_UNVERIFIED` constants and `DHAN_HISTORY_FLOOR` are `None` HONESTY
  MARKERS that **no decision branch reads** (AST-pinned in
  `tests/unit/test_dhan_constants.py`), so the pull can never gate on a fabricated
  flag. A resolved index that returns zero bars across the ENTIRE requested range is
  additionally surfaced as one WARN per symbol (a wrong-secId / unverified-servability
  smell), mirroring Groww's `scan_underlying_all_empty` intent.

**Rate / quota (own account, `DEC-DHAN-SPOT-PULL-001`).** Dhan's Data API is 5 req/s
(`DHAN_RATE_LIMIT_RPS=5.0`, injected into the adaptive `RateController`) and
100,000 req/day (`DHAN_DAILY_QUOTA`), with **no per-minute / per-hour governor**
(`DHAN_RATE_LIMIT_PER_MIN=None` — honestly none, not a fabricated cap). A full 3-index
1-minute pull is ≈ tens of requests, trivial vs the daily cap. These ceilings are the
Dhan account's own; they never touch the Groww governor (§2 Rule 6).

---

## 5. Decision 4 — feed-root nesting + isolated audit chain + read-only probe kit

The Dhan lake nests under `<lake_root>/feeds/dhan/` via the ADR-0021 effective-root
law (`brutex/data/feed_root`, `DHAN_FEED_ID="dhan"`), and the Dhan window-ledger
namespace is `DHAN_COVERAGE_MARKET="dhan"` — a Dhan window can never collide with a
Groww window on the shared ledger, and a Dhan pull's writes physically sit in a
SIBLING subtree of `bars/` (never a child), so a Groww-side `bars/`-rooted reader
never descends into it (the ADR-0021 reader-invisibility verdict; a regression pin
proves the isolation). The Dhan feed is wired into the registry
(`brutex/feeds/registry.py`) as the real second `FeedBinding`
(`DEC-DHAN-FEED-BINDING-001`), and its ingest sink is wrapped in the per-feed
hash-chained `FeedAuditSink` (Golden Rule #8) so the Dhan ingest log is a separate
chain from Groww's.

**Pull surface (`DEC-DHAN-PULL-SURFACE-001`).** `brutex/dashboard/dhan_pull.py`
(`default_dhan_spot_pull`) is the surface glue: resolve credentials → build the
read-only adapter → run the lean spot spine over its OWN write tree (its own flock)
→ gate the prebuild on a clean pull. The dashboard job carries its own 5 r/s +
100k/day ceiling and flocks the Dhan tree independently of any Groww pull.

**Read-only probe kit (`docs/runbooks/feed-probe/`).** A standalone, credential-gated
probe that cross-checks the hardcoded securityIds against Dhan's public compact
scrip-master CSV and flips the `_UNVERIFIED` markers / `DHAN_HISTORY_FLOOR` in a
one-line data change once a transcript lands. The probe is **strictly read-only**: it
NEVER mints a token — it REQUIRES a provided `DHAN_ACCESS_TOKEN` (the SSM-read token
TickVault minted) and FAILS LOUD if absent. Running any mint against the shared
production Dhan account would rotate the single active token and kill TickVault's live
feed, so the mint path is deliberately absent from the kit (§6 note).

---

## 6. Decision 5 — retire the `no-dhan-guard` (`DEC-DHAN-GUARD-RETIRE-002`)

After ADR-0009 deleted the Dhan adapter and made Groww authoritative, a CI guard
(`scripts/ci/no-dhan-guard.sh`) banned the literal token `dhan` on the code surface to
prove Dhan was fully gone. This standalone feed LEGITIMATELY re-introduces the token
`dhan` (a whole `brutex/adapters/dhan/` package, `feeds/dhan/`, `DHAN_*` constants), so
the guard would now fail every push. It is therefore RETIRED:
`scripts/ci/no-dhan-guard.sh` is deleted, and its `ci-ok` `needs` entry + result
echo/aggregate references in `.github/workflows/ci.yml` are removed. The guard was
never a branch-protection **direct** required context, so
`.github/workflows/branch-protection-drift.yml` needs no rule change beyond dropping
the guard from its explanatory "enforced via ci-ok" list. Groww remains the
authoritative backtesting vendor; the retirement re-admits Dhan strictly as a
read-only SPOT feed track, not as a vendor swap.

The old ban is superseded by a narrower, honest invariant instead: the vendor-doc
consistency test (`tests/unit/test_vendor_doc_consistency.py`) still pins that the
OPERATOR auth docs describe Groww, so the guard's true intent (no accidental vendor
re-pivot) is preserved without banning a legitimately-present token.

---

## 7. Decision 6 — Rolling-ATM expired-options + Greeks (`DEC-DHAN-ROLLING-OPTIONS-001`)

The Dhan feed track is extended from spot to **expired-options history** — but Dhan
serves **NO per-expired-contract candles**. The ONLY historical option surface is the
ATM-relative **rolling series** (`POST /v2/charts/rollingoption`): a request fixes a
selector `(underlying, exchangeSegment, expiry_flag ∈ {WEEK, MONTH}, expiry_code ∈
Near/Next/Far, strike_offset ∈ {ATM, ATM±N}, option_type ∈ {CALL, PUT})` and Dhan
returns the bar series of *whatever contract that selector resolved to on each
bar-date*. As spot drifts and expiries roll, the **resolved absolute strike and the
resolved expiry DATE change per bar** under a fixed selector.

This forces the track's central honesty law: the resolved expiry-date and resolved
strike are **per-ROW columns** (`resolved_expiry_date`, `resolved_strike`), **NEVER
partition keys**. The lake root is named `rolling_options/` (never `options/` /
`fno_candles/` / `bars/`) and the partition path encodes **only the fixed selectors**
(`rolling_options/{exchange}/{underlying}/{expiry_flag}/EC{code}/{strike_offset_safe}/
{option_type}/{interval}/{yyyy}/{mm}.parquet`). A single fixed-`expiry_code` partition
legitimately holds many distinct `resolved_expiry_date` values across a real roll —
that is the design, not a bug. `parse_fno_symbol` never touches a `rolling_options/`
path; every dashboard label / `/db` column / doc says **"rolling-ATM" / "ATM±N"**, never
"per-contract" / "per-expiry" / "per-strike".

**Plan.** The default cartesian is enumerator-fixed at `3 × 2 × 4 × 21 × 2 = 1008` keys
(`DHAN_ROLLING_PLAN_KEY_COUNT`); an operator from/to scopes only the window (the
selector grid is invariant).

**Greeks — the ONE cross-cutting seam `iv_source` (served-vs-solved).** The overlay
reuses the COMMON Greeks math VERBATIM (`greeks_enrich.compute_bar_overlay` →
`options.greeks.compute_greeks` → the shared `trading_core` Rust kernel — no Greek
arithmetic is re-implemented, RUST-FIRST satisfied by reuse). Dhan SERVES an implied
volatility per bar: when it is usable the Greeks are priced DIRECTLY from
`(resolved_strike, spot, served_iv, T, rate)` with **no root-solve** (`iv_source =
"served"`); when it is absent/garbage the pricer root-solves from the premium **exactly
as the Groww path does** (`iv_source = "solved"`); an unsolvable premium stamps
`no_solution` — never a fabricated Greek. `iv_source` lives in the COMMON pricer +
provenance (`IV_SOURCE_SOLVED = 0` is the DEFAULT, so the Groww solve path packs the
**byte-identical** integer it always did, bit 27 clear) — **the Groww solve path is
byte-identical and there is NO formula-version bump** (the enrich goldens stay frozen).
Per-bar `T` binds to THAT row's `resolved_expiry_date`; indices price EUROPEAN_EXACT;
every honest demotion (`NO_RATE` / `NO_SPOT` / `BELOW_INTRINSIC` / `AT_EXPIRY` /
`BAD_INPUT` / `NO_SOLUTION`) is REUSED and stamps NULL + a status, never a value.

**Honest expiry-source state.** When the response echoes an expiry it is stamped
`resolved_expiry_source = "echoed"`; when absent it is derived from the NSE calendar
(`"derived"`); when neither is possible the row is honestly `"unresolved"` — never a
fabricated date.

**Servability is UNVERIFIED.** All `*.dhan.co` hosts are egress-blocked in-sandbox, so
`rollingoption` servability (DH-905), the INDEX-vs-OPTIDX `instrument` disambiguation,
expiry-echo presence, the exact WEEK/MONTH + `expiryCode` → date map, the 30-vs-90-day
chunk cap, the strike band `N`, SENSEX/BSE_FNO WEEK-tenor servability, and the June-2022
floor ALL ship as `_UNVERIFIED` sentinels / `history_floor = None` until a credentialed
`brutex doctor --live --dhan-rolling-probe` transcript flips them (the #898/#1581
form-probe precedent). Two DISTINCT honest states are never conflated: **EMPTY-FROM-DHAN**
(holiday/thin/too-old → clean-empty, bounded settle, re-pullable) vs
**UNVERIFIED-SERVABILITY** (sentinel → PLANNED, one WARN naming key + probe, ZERO bars).

**`/db` surface (this PR).** A MINIMAL first slice ships — a self-contained
`dhan_rolling_options` dataset discovery + honest column metadata
(`brutex/dashboard/dhan_rolling_db_view.py`) that reads the `feeds/dhan/rolling_options/`
scheme and advertises the rolling-ATM columns (`resolved_expiry_date`, `resolved_strike`,
`strike_offset` = the ATM±N LABEL, `option_type`, `expiry_flag`, `expiry_code`, `iv`,
`spot`, + the Greeks overlay). It touches NONE of the shared `db_view.py` (the Groww
spot / F&O candle browser stays byte-identical). The full interactive selector cascade +
the live candle query route are the deferred follow-up `DEC-DHAN-ROLLING-DB-VIEW-001`.

### Honest limits (Decision 6, Golden Rule #10)

- Servability is UNVERIFIED (above) — the whole options track is a designed-and-tested
  surface that has **not** been confirmed against a live Dhan account; a probe-unconfirmed
  capability degrades to a LOUD skip + ZERO bars (window stays PLANNED/re-pullable), never
  a fabricated bar/strike/expiry/Greek.
- The `/db` slice is discovery + column metadata ONLY — no interactive cascade / query
  route yet (`DEC-DHAN-ROLLING-DB-VIEW-001`).
- Movers on options is not feed-aware yet (`DEC-DHAN-MOVERS-FEED-AWARE-001`); the lean
  spine's durable-ledger / parallel / `run_until_clean` parity remains the deferred
  `DEC-FEED-SPINE-GENERALIZE-001`.

---

## 8. Consequences

- Groww output + stored bytes + engine identity are byte-identical — the Dhan track is
  purely additive (no edit to `brutex/adapters/groww/*` or the byte-critical write
  stack).
- A second, independent price source exists for the three engine indices + VIX, with
  its own lake subtree, ledger namespace, hash-chained audit log, rate ceilings, and
  read-only credential path — no shared-file contention, no schema migration.
- The BRUTE-FORCE ENGINE is UNCHANGED — NIFTY / BANKNIFTY / SENSEX. Dhan bars are a
  second feed's stored history; they enter no vocabulary and no run identity, and VIX
  stays REFERENCE_ONLY.

### Honest limits (Golden Rule #10)

- The lean spot spine uses **no `WindowLedger`** — its per-window outcomes are the
  record, and re-pull is a whole-range operator re-run; the durable-ledger / parallel /
  `run_until_clean` parity is the deferred `DEC-FEED-SPINE-GENERALIZE-001`.
- `DHAN_HISTORY_FLOOR` and every `*_SERVABILITY_UNVERIFIED` marker are `None` until a
  credentialed probe transcript lands (all `*.dhan.co` hosts are egress-blocked
  in-sandbox); the feed plans from the operator date range and records honest empty
  windows for a too-old span rather than fabricating a floor.
- Dhan intraday `to_date` inclusivity is UNVERIFIED (the fetch tiles a half-open
  `[start, end)` range; whether Dhan treats `to_date` inclusively — and thus whether a
  −1-day adjustment is ever needed — is a probe-gate item recorded in
  `adapter.py::fetch_ohlcv`).
- The post-pull movers warm is NOT yet feed-aware, so a Dhan pull warms NOTHING (rather
  than warm the wrong Groww tree) — `DEC-DHAN-MOVERS-FEED-AWARE-001`.

---

## 8. Amendment (2026-07-14, the merge-forward delta) — rolling floor · error map · terminal halt · token ops

### §floor — the rolling history-floor LAW (`DEC-DHAN-ROLLING-FLOOR-001`, MIGRATION.md entry #1798)

Operator ruling 2026-07-14: a Dhan pull must NEVER request data before roughly
`today − 5 years` (the documented intraday depth is the ROLLING "last 5 years" —
VERIFIED-docs(mirror), `17-historical-data.md`). The law:
`dhan_history_floor(today) = dhan_rolling_edge(today) + DHAN_FLOOR_MARGIN_DAYS`,
where `dhan_rolling_edge(today) = today − 5y` with Feb-29 rounding FORWARD to Mar-1
(a floor may only ever be LATER than the true vendor edge) and
`DHAN_FLOOR_MARGIN_DAYS = 7` — an UNVERIFIED design-time safety constant with
deliberately NO env override. **Margin provenance:** pre-floor request behaviour is
undocumented (it may be an error OR a silently-TRUNCATED payload recorded as clean
SUCCESS — the integrity-violating case); the cross-verification use-case makes the
oldest week expendable; the `--floor-edge` probe transcript recalibrates it via a
one-line code change + a ledger note. ONE clamp chokepoint (`plan_dhan_work(today=…)`,
`today` = the IST date resolved ONCE per launch); a wholly-pre-floor pick is a route
HTTP 400 naming the floor + the spine's zero-window `dhan.pull_entirely_pre_floor`
WARNING. **The retention property (binding, verbatim):** the floor governs VENDOR
REQUESTS ONLY. Bars already in `feeds/dhan/` that age past the current floor are
NEVER deleted (keep-first, append-only; no reaper exists or is authorized).
Consequences: idempotent-re-pull byte-parity is scoped to the POST-floor span;
pre-floor content is verified by lake-invariants/heal, never by re-fetch; Groww keeps
its SEPARATE fixed `GROWW_HISTORY_FLOOR = 2020-01-01` — no shared code path may
conflate the two laws. `DHAN_HISTORY_FLOOR = None` stays the static measured-edge
slot (the binding identity pin), not the law's home.

### §3 amendment — the terminal-halt posture

`is_terminal_pull_stop` (errors.py) carves the pull-wide global stops out of the
per-window quarantine: a terminal `DhanAuthError` (the client's bounded re-read
budget already exhausted), a `DhanTokenStillStaleError` (the SSM re-read returned
the SAME dead token — nothing else in THIS pull can succeed; supersedes the original
per-window-quarantine wording), or a `DhanSubscriptionError` (806/DH-902 —
vendor-terminal) RE-RAISES on its FIRST occurrence, so the remaining ~76 windows are
never burned, and `pull_jobs.is_pull_halt` gains the lazy Dhan arm so the existing
CRITICAL Telegram `emit_pull_halt_alert` fires with zero further wiring (Groww
verdicts byte-identical, pinned). The error CLASSIFIER now consults the full
documented code table FIRST (805/DH-904 throttle · 806/DH-902 subscription ·
807-810/DH-901 auth · 804/811-814/DH-905/DH-907 bad request — DH-905 RELABELED
input-exception · 800/DH-908/DH-909 retryable server), reading BOTH remark-key
spellings — the live `dhanhq` SDK envelope is snake_case (`dhan_http.py`
`_parse_response`), the docs' raw-HTTP shape camelCase; pre-fix the camelCase-only
classifier left the DH-901 re-read self-heal production-dead.

### Static-IP posture (softened wording)

Dhan's static-IP whitelisting requirement is documented for **Order APIs only**
(v2.4/v2.5.1 releases); no doc statement binds it to the Data/historical APIs. The
posture here: documented Order-API-only — **confirm on the first credentialed run**
(probe gate #12); the read-only feed places no order and hits no order endpoint.

### Token-ops contract (WHO mints — a PRE-FIRST-WET-PULL gate)

BruteX NEVER mints (Decision 2, unchanged). The `/tickvault/prod/dhan/access-token`
SecureString must be populated by an EXTERNAL minter — TickVault's Dhan minter or a
manual web.dhan.co seed (My Profile → "Access DhanHQ APIs" → generate → put to SSM).
**With no minter, the re-read self-heal has nothing to read**: cold start fails loud
and a mid-run DH-901 escalates to the `DhanTokenStillStaleError` pull-wide stop. The
operator's minter decision (automated vs manual cadence; the token-lifetime conflict
24h vs 8h–30d selectable is probe gate #10) is RECORDED as a pre-first-wet-pull
gate — not assumed.
