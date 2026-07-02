> Durable repo copy (2026-07-02) of the kit specification `brutex doctor --live`
> was built from, committed per DEC-DOCTOR-LIVE-PROOF-KIT-001. Companion:
> `pull-divergence-register.md` (same directory). Content unmodified from the
> audit artifact; the "exists vs build" columns describe origin/main at the
> audit target commit named below, NOT the current tree.

# Mac Live-Proof Kit — `brutex doctor --live` specification

**Companion to:** `groww_pull_divergence_register.md` (row IDs R1-R8, M1-M17, N1-N6, G1-G7 referenced below).
**Audited against:** `origin/main` @ `2594df18`. **Status:** SPEC ONLY — nothing here is implemented; the
"exists vs build" column is verified per-stage against origin/main.

## 1. Purpose

One command the operator runs **on the Mac, with real AWS + real Groww network**, that walks the exact
production code path a live pull takes — stage by stage — and, at the first divergence, prints a line that
says **exactly what is wrong and what to do**, cross-referenced to the register. It exists because the
sandbox is structurally blind (no creds, no vendor network: every green test proves only the fakes) and
because today's failure surfaces are silent (R1-R6, G1-G3) or misleading (R7, R8, M1).

## 2. Design principles (non-negotiable)

1. **COMMON (Golden Rule #11):** every stage calls the REAL production functions —
   `resolve_groww_aws_credentials`, `ParameterStoreTokenStore.load`, `build_live_client`,
   `parse_candle_rows`, `filter_intraday_session`, the parquet writer, the window ledger,
   `verify_lake_records`, the resampler. No reimplementation. Where production code swallows the reason
   (R1's `groww_credentials_present`, R7's degrade-to-None load), the kit calls the *underlying* function
   that still carries it, or a new reason-carrying seam that production is then refactored to share.
2. **READ-ONLY TOKEN DISCIPLINE:** the kit **never mints a Groww token** (R7/M4 hazard: a mint may revoke
   the shared token TickVault/the Lambda own — single-active-token UNVERIFIED, treat as true). It reads
   the SSM token only. Any mint requires `--mint-anyway` printing a red multi-line warning first.
3. **BOUNDED CALL BUDGET:** default run ≤ ~40 authenticated vendor calls (well under 500/min), paced by the
   real `RateController`. The optional depth bisection (stage 6) adds ≤ 20/index; the opt-in rate probe
   (stage 13) adds ≤ 480 and aborts on the first throttle burst.
4. **QUARANTINED WRITES:** stages 10-12 write real Parquet + ledger rows under
   `<lake_root>/_liveproof/<run_id>/` (a real tree, real code path, zero contamination of production
   partitions), except the explicit `--against-lake` mode which only READS the production lake.
5. **EVIDENCE OR IT DID NOT HAPPEN:** every stage appends its raw transcript (requests issued, payload
   shapes observed, timings) to `<lake_root>/_liveproof/<run_id>/transcript.jsonl`, suitable for committing
   to `docs/groww/probe/` — finally closing the ADR-0009 open probe gates with real transcripts.
6. **FAIL-FORWARD:** default is fail-fast at the first FAIL; `--keep-going` runs all stages and prints a
   summary matrix. Exit code 0 = all PASS, 2 = at least one FAIL, 3 = kit-internal error.

## 3. Invocation

```
brutex doctor --live [--lake PATH] [--symbol NSE-NIFTY] [--stage N[,M…]] [--keep-going]
                     [--depth-bisect] [--rate-probe] [--against-lake] [--json] [--mint-anyway]
```

## 4. Stages

Summary matrix (details follow):

| # | Stage | Vendor calls | Exists today? |
|---|---|---|---|
| 0 | env-identity (SHA/versions/venv/lake-root/mount/proxies) | 0 | **BUILD** (partial reuse: sweep SHA resolver `cli.py:3002-3034`) |
| 1 | creds-presence (AWS SSM, loud reasons) | 0 (AWS only) | production loader EXISTS (`credentials.py` / `credentials_aws.py`); kit wiring **BUILD** |
| 2 | totp-clock (base32 validation + generation + NTP skew) | 0 | **BUILD** (reuses `auth._real_totp` seam) |
| 3 | token-read (NO MINT) + auth smoke call + SDK shape probe | 1 | store EXISTS (`token_store_aws.py`); shape probe + wiring **BUILD** |
| 4 | instrument-master fetch + schema check | 1 (keyless) | EXISTS (`instrument_loader.load_index`, `instruments.parse_instrument_csv`); wiring **BUILD** |
| 5 | spot 1-minute probe through the real pipeline | 1-3 | parser/session/client EXIST; assertions + wiring **BUILD**; `probe.py:probe_granularity` is dead code to resurrect |
| 6 | 2020-depth probe per index (optional bisection) | 3-60 | dead code EXISTS (`probe.py:probe_granularity`); bisection + wiring **BUILD** |
| 7 | VIX probe (ADR-0009 HARD-GATE) | 2 | dead code EXISTS (`probe.py:probe_vix`); wiring **BUILD** |
| 8 | 1-day derivation + NSE bhavcopy cross-check | 0-1 (+1 to NSE archive) | resampler EXISTS (`data/spot_daily_resample`); **bhavcopy MUST BE BUILT FROM SCRATCH** (zero implementation at origin/main — G7) |
| 9 | resample determinism (19 derived TFs) | 0 | EXISTS (`doctor.py:322-407` check 4) — reuse as-is |
| 10 | lake write + read-back + ledger row (quarantined subtree) | 0 | writer/ledger EXIST; quarantined-subtree wiring **BUILD** |
| 11 | integrity re-fetch verify | 1 | EXISTS but UNWIRED (`data/integrity_verify.py:verify_lake_records`) — wire it |
| 12 | resume / interrupted-pull / sleep check | 0 | ledger scan EXISTS; pull journal **BUILD** (G5) |
| 13 | rate-ladder probe (opt-in `--rate-probe`) | ≤480 | **BUILD** (reuses real `RateController` + client) |

**Retired-stage note (honesty):** the task's suggested "1-day spot pull" stage does not match origin/main —
DEC-1MINUTE-ONLY-DERIVE-1DAY-001 makes 1-minute the ONLY vendor-fetched spot granularity; `1day` is derived
locally (`pull_controller.py` `resolve_fetch_intervals` ALWAYS `("1minute",)`, rechecked ✓). Stage 8 therefore
probes the *derivation + bhavcopy* pair (the VERIFY-FIRST intent), and stage 5 carries the vendor-pull proof.
CLAUDE.md's dropdown model is stale (register G7).

---

### Stage 0 — env-identity

**Runs (no network except one HEAD to origin, skippable):** resolve git SHA of the running checkout +
behind-count vs origin/main; `brutex.__version__` + `pip show brutex` location (editable vs copied);
`sys.version` + venv path; `importlib.metadata.version("growwapi")`; `resolve_lake_root()` +
`os.path.ismount` walk for `/Volumes/...` parents + `st_dev`; effective proxies
(`urllib.request.getproxies()`); which `.env` file the dotenv chain resolved (repo → cwd → `~/.brutex/.env`).

**PASS prints:**
```
PASS env: commit ab12cd3 (0 behind origin/main) · brutex 0.2.0a0 · growwapi 1.5.2 · python 3.12.4
          venv=/Users/sj/bruteX/.venv (editable install)
          lake=/Volumes/T8/brutex-lake (volume MOUNTED, dev=16777232) · env=/Users/sj/bruteX/.env · proxies={}
```

**FAIL/WARN diagnostics (exact lines → register rows):**
- `FAIL env[G1]: lake root /Volumes/T8/brutex-lake — /Volumes/T8 is NOT a mountpoint. Your external SSD is unmounted; a pull right now would silently rebuild the lake AND the window ledger on the internal disk, and the two trees will disagree after remount. Fix: remount the SSD; then diff ~/.brutex/lake vs the SSD lake for phantom bars//ledger files.`
- `WARN env[G1]: dashboard and CLI would resolve DIFFERENT .env files (<pathA> vs <pathB>) — BRUTEX_LAKE_ROOT may differ per surface. Fix: consolidate into ~/.brutex/.env.`
- `WARN env[G2]: growwapi 1.7.0 installed; this code is written against the 1.5.x surface (pin >=1.0,<2.0). Exception class names / private HTTP methods may have drifted — stage 3 will probe the shape. Fix if stage 3 fails: pip install 'growwapi==1.5.*'.`
- `WARN env[G3]: checkout ab12cd3 is 14 commits behind origin/main — the failure you are debugging may already be fixed. Fix: git pull && pip install -e .`
- `WARN env[G3]: brutex is NOT an editable install (site-packages copy from 2026-06-15) — the code running is not the code in your checkout. Fix: pip install -e /Users/sj/bruteX`
- `WARN env[R8]: system proxy detected (https=http://proxy.corp:8080) — Lambda/CI never see this; TLS interception commonly presents as an endless amber 'transient' pull. Stage 3/4 will exercise real TLS.`

### Stage 1 — creds-presence (AWS SSM)

**Runs:** `resolve_groww_aws_credentials()` — the real production loader, **without** the
`groww_credentials_present()` swallow (R1). Reports region, parameter paths, timing.

**PASS:** `PASS creds: api-key + totp-secret read from SSM /tickvault/prod/groww/{api-key,totp-secret} (region ap-south-1, 240 ms)`

**FAIL diagnostics:**
- `FAIL creds[R1]: <relayed CredentialsError text, verbatim — incl. the credentials_aws.py:61-66 how-to-fix block>` followed by:
  `NOTE[R1]: on the dashboard this EXACT failure silently downgrades every pull to SYNTHETIC data with a green verdict (the '● PREVIEW' badge is the only trace). Every 'successful' dashboard pull you ran while this failure existed fetched ZERO real bars.`
- Cause-specific tails the kit adds by inspecting the exception chain:
  `boto3 missing from THIS venv → pip install 'brutex[aws]'` · `NoCredentialsError → aws configure / AWS_PROFILE / renew SSO` · `AccessDeniedException on ssm:GetParameter → grant the reader policy` · `KMS AccessDenied → grant kms:Decrypt on the creds CMK` · `wrong region → set BRUTEX_TOKEN_AWS_REGION=ap-south-1`

### Stage 2 — totp-clock

**Runs:** `base64.b32decode(secret, casefold=True)` validation of the SSM secret; TOTP generation via the
production `auth._real_totp` seam; NTP offset vs `time.apple.com` (fallback: HTTPS `Date` header of a
well-known host) — offset only, no vendor call.

**PASS:** `PASS totp: secret is valid base32 (32 chars); 6-digit code minted; host clock offset +0.4 s vs NTP (Groww tolerance UNVERIFIED — assume ±30 s)`

**FAIL diagnostics:**
- `FAIL totp[M3]: TOTP secret in SSM is NOT valid base32 (binascii: Non-base32 digit found — offending char '8' at position 12). In production this crashes as a raw hint-free "Error: Non-base32 digit found" (the mint sits outside auth.py's sanitizing try). Fix: re-seed /tickvault/prod/groww/totp-secret with the exact base32 secret from the Groww developer page (no spaces/dashes; note 0/1/8 are not base32).`
- `FAIL totp[M3]: pyotp is not installed in THIS venv — the production mint would crash with a raw ModuleNotFoundError. Fix: pip install pyotp (or reinstall brutex extras).`
- `FAIL totp[N1]: host clock is 94 s AHEAD of NTP — TOTP codes will be rejected and the vendor error will look like wrong credentials (do NOT rotate them). Fix: System Settings → General → Date & Time → Set time automatically.`

### Stage 3 — token-read (NO MINT) + authenticated smoke call + SDK shape probe

**Runs:** `ParameterStoreTokenStore.load()` through a new **reason-carrying seam** (the kit must see WHY a
load missed — today any boto3/IAM/KMS error degrades to `None`, R7); builds the real SDK client with the
SSM token (`build_live_client` path, **token_factory disabled** — mint physically impossible in kit mode);
issues ONE cheap authenticated `get_historical_candles` (NSE-NIFTY, previous trading day, `1minute`, 1
call); then the **SDK shape probe**: expected exception class names importable, `_request_get/_request_post/_request_put`
attributes present (the `http_timeout` patch preflight), `get_historical_candles` signature matches.

**PASS:** `PASS token: SSM token present (age 4h12m, param /tickvault/prod/groww/access-token); authenticated call OK (212 ms, 375 rows); SDK surface matches the tested 1.5.x expectations (7 exception classes, 3 _request_* methods, signature OK)`

**FAIL diagnostics:**
- `FAIL token[R7]: SSM token read FAILED with AccessDeniedException (kms:Decrypt on the token param's CMK) — NOT 'token absent'. Production cold-start cannot tell these apart: it would perform a REAL local Groww mint (possibly revoking the shared token TickVault + the Lambda own) and then crash with the MISLEADING 'deploy the minter' error. Fix: grant kms:Decrypt on the token CMK to this reader role. Do NOT deploy anything.`
- `FAIL token[R7]: SSM token genuinely ABSENT (/tickvault/prod/groww/access-token, ParameterNotFound). Fix: deploy/run the minter Lambda (deploy/aws_token_minter/deploy.sh) or invoke it: aws lambda invoke --function-name groww-token-minter /dev/stdout. The kit did NOT mint locally (that could revoke a sibling's token).`
- `FAIL token[M1]: token present but the vendor REJECTED it (<status/exception>) — the shared token in SSM is DEAD and the Lambda will not re-mint until 06:05 IST. Production pulls today would grind all day as amber 'transient — re-pull to finish' (a futile prescription). Fix: aws lambda invoke --function-name groww-token-minter /dev/stdout, then re-run this stage.`
- `EVIDENCE[M2]: the rejection arrived as a BARE GrowwAPIException (code=401, "<text>") — production classifies this TERMINAL 'not on Groww' and never re-auths. This live shape was previously UNVERIFIED; the transcript recorded it — file the errors.py fix (add 401/403 to _CODE_TO_ERROR).`
- `FAIL sdk[G2]: growwapi 1.7.0 has no _request_get — the socket-timeout patch silently degrades to NO-PATCH; slow networks stall ~4.5 min per window with no timeout. Fix: pin growwapi==1.5.* until http_timeout.py is updated.`
- `FAIL token[R8]: authenticated call failed with CERTIFICATE_VERIFY_FAILED — a PERMANENT TLS failure that production classifies as a retryable network blip (endless amber 'transient'). Fix: run /Applications/Python*/Install Certificates.command; check HTTPS_PROXY and macOS system proxy; corporate VPN MITM needs its CA in certifi.`

### Stage 4 — instrument-master fetch + schema check

**Runs:** the real keyless CSV download (`instrument_loader.load_index` / `urllib_http_get`) +
`parse_instrument_csv`; asserts the 21-column header, presence of the 3 brute-force indexes +
`NSE-INDIAVIX`, ~212 F&O-eligible equities resolved.

**PASS:** `PASS master: 21-column header OK; 3 indexes + NSE-INDIAVIX present; 212 F&O equities; 89,412 rows parsed`

**FAIL diagnostics:**
- `FAIL master[M17]: header drift — 22 columns received (new trailing 'exchange_flag'); the strict tuple-equality check bricks the entire universe load until a code change ships. Header diff: <expected vs got>. Fix: file the additive-column tolerance change; short-term, pin the master snapshot.`
- `FAIL master[R8/G4-class]: cannot reach growwapi-assets.groww.in: <error> — network/TLS/proxy failure BEFORE authentication is even involved. Same fixes as stage 3's TLS line.`

### Stage 5 — spot 1-minute probe through the REAL pipeline

**Runs:** one `1minute` `get_historical_candles` for `--symbol` (default NSE-NIFTY) over the last full
trading day, through the real client → `parse_candle_rows` → `filter_intraday_session`. Assertions:
payload is a Mapping with top-level `candles`; rows are 7-arrays; timestamps parse; **first bar == 09:15
IST**; raw-vs-session drop ratio < 5%; bar count ≈ 375; OHLC sane (no null-skip epidemic); records the
observed timestamp encoding (string vs epoch) to the transcript.

**PASS:** `PASS spot-1m: 375 raw → 375 session bars; first bar 09:15:00 IST (string-encoded, native-IST); 0 bars skipped for absent OHLC; envelope shape matches parser expectations`

**FAIL diagnostics:**
- `FAIL spot-1m[M11]: payload has NO top-level 'candles' key (keys present: ['status','payload']) — production would quarantine EVERY symbol as 'not on Groww'. Fix: SDK envelope drift — pin growwapi or patch parser.py; payload sample written to the transcript.`
- `FAIL spot-1m[M10]: SDK returned <class 'NoneType'> instead of a Mapping — production would crash the ENTIRE multi-symbol pull with a raw TypeError at parser.py:123.`
- `FAIL spot-1m[M12]: first bar parses to 03:45 IST and 330/375 bars fall outside the NSE session — timestamps are UTC-encoded but treated as IST (+05:30 systematic shift). Production silently DROPS these and stores the survivors under WRONG stamps. Do NOT run a backfill until the parser semantics are fixed.`
- `FAIL spot-1m[M13]: unparseable timestamp '2026-07-01T09:15:00+05:30' — the parser accepts only '%Y-%m-%d %H:%M:%S'. Production quarantines every symbol as 'not on Groww'. Offending raw value recorded.`
- `FAIL spot-1m[R2/R3]: clean-but-EMPTY 200 for a day NSE definitely traded — either a throttle-shaped empty-200 or a mis-addressed request (the proven M&M-'&' class). Production would count this toward a PERMANENT SKIPPED settle. Do NOT run a backfill until explained; re-run this stage in 10 min to separate throttle from addressing.`
- `WARN spot-1m[M15]: 41% of bars skipped for absent/zero OHLC — production drops these with ZERO logging (bare except…continue). Vendor data-quality incident or column misalignment; sample rows in the transcript.`

### Stage 6 — 2020-depth probe (per index)

**Runs:** resurrects the dead-code `probe.py:probe_granularity` (LIVE-capable, currently imported only by a
unit test with a fake client): `1minute` fetch at `2020-01-01 → 2020-01-15` for each of NSE-NIFTY,
NSE-BANKNIFTY, BSE-SENSEX (3 calls). With `--depth-bisect`: bounded bisection (≤ 20 calls/index) to find the
true first 1m bar. Result appended to the transcript — this **closes the ADR-0009 §3.1 open probe gate**
(no transcript exists at origin/main; `docs/groww/probe/` holds only README + INSTRUMENT_MASTER_FINDINGS).

**PASS:** `PASS depth: NSE-NIFTY 1m starts 2020-01-01 09:15 IST · NSE-BANKNIFTY 2020-01-01 · BSE-SENSEX 2020-01-01 — GROWW_HISTORY_FLOOR=2020-01-01 CONFIRMED`

**FAIL:** `FAIL depth[R6]: NSE-NIFTY 1m actually starts 2021-06-14 (probed floor). A 2020→today backfill would burn quota on every earlier window and permanently settle them SKIPPED under a green verdict; sweeps over 2020-21 would silently run on derived-daily-only data. Fix: set GROWW_HISTORY_FLOOR to the probed floor per index (or fix the settle machinery first); record the floor in lake metadata.`

### Stage 7 — VIX probe (ADR-0009 HARD-GATE)

**Runs:** resurrects `probe.py:probe_vix`: `NSE-INDIAVIX` daily candles (last 30d) + one 1-minute day
(2 calls), recording the exact failure shape (empty-200 vs 404 vs typed error) if absent.

**PASS:** `PASS vix: NSE-INDIAVIX served 21 daily bars + 375 1m bars — the ADR-0009 §3.2 HARD-GATE is SATISFIED (transcript recorded)`

**FAIL:** `FAIL vix[R5]: Groww serves NO candles for NSE-INDIAVIX (shape: clean-empty 200). This is the ADR-0009 §3.2 MIGRATION-RESCINDING hard gate — and today's pull path would normalize it to a green 'No action needed' while vix_at_entry/vix_at_exit silently have no data. Do not proceed with Track-1 pulls; escalate per ADR-0009 (rescind or source VIX elsewhere).`

### Stage 8 — 1-day derivation + NSE bhavcopy cross-check

**Runs:** derives the probed day's `1day` bar from stage 5's 1m bars via the production resampler
(`data/spot_daily_resample` — the ONLY way spot 1day exists at origin/main, G7); fetches the official NSE
bhavcopy for that date from the public archive (**new code — zero bhavcopy implementation exists at
origin/main**; host must join the SSRF allowlist); compares OHLC within the 2-decimal tick tolerance.
This finally implements the VERIFY-FIRST intent that CLAUDE.md describes but the code never had.

**PASS:** `PASS bhavcopy: derived 1day OHLC for NIFTY 50 2026-07-01 matches the NSE bhavcopy (max Δ 0.05 = one tick)`

**FAIL diagnostics:**
- `FAIL bhavcopy[M12/M15]: derived close 24812.40 vs bhavcopy 24818.85 (Δ 6.45) — the vendor 1m stream disagrees with the exchange. Likely thinned/mis-stamped session bars upstream. Do not trust the lake for this range until explained; see stage 5's drop-ratio evidence.`
- `WARN bhavcopy: NSE archive unreachable (<error>) — external dependency, not a Groww fault; re-run later. (Cross-check skipped, NOT passed.)`

### Stage 9 — resample determinism

**Runs:** the existing `brutex doctor` check 4 (derived-TF freshness re-derivation vs stored Parquet,
`doctor.py:322-407`) against the stage-10 quarantined tree (or `--against-lake` for the production lake).
**EXISTS — reused verbatim.** PASS/FAIL text unchanged, plus the kit's register cross-reference on failure.

### Stage 10 — lake write + read-back + ledger row

**Runs:** writes stage 5's real bars through the production parquet writer + window-ledger into
`<lake_root>/_liveproof/<run_id>/`; reads back bit-exact; confirms the ledger row transitions
PLANNED→SUCCESS; appends an audit-chain line (note: today only CLI `brutex fetch` writes the spot-ingest
audit chain — dashboard pulls write none; scout finding).

**PASS:** `PASS lake: 375 bars → /Volumes/T8/brutex-lake/_liveproof/lp-20260702-091501/ · read-back bit-exact · ledger row SUCCESS · audit line appended (chain verified)`

**FAIL diagnostics:**
- `FAIL lake[G1]: this process resolved lake_root=~/.brutex/lake but ~/.brutex/.env (used by the dashboard) says /Volumes/T8/brutex-lake — TWO lake roots are in play between your surfaces. Fix: set BRUTEX_LAKE_ROOT in ONE place; then reconcile the two trees (both contain ledgers).`
- `FAIL lake: write failed: <OSError verbatim> (disk full / read-only volume / permissions) — production maps this to a pull-halt; fix the volume first.`

### Stage 11 — integrity re-fetch verify

**Runs:** wires the existing-but-unwired `data/integrity_verify.py:verify_lake_records` (its docstring's "a
CLI is a thin wrapper over this" is aspirational at origin/main — grep confirms module + tests only) over
the stage-10 window: re-fetches it live (1 call) and compares against the capture hash recorded during
stage 5.

**PASS:** `PASS integrity: re-fetched window hash matches capture (vendor is serving stable bytes)`

**FAIL:** `FAIL integrity[R2]: the vendor returned DIFFERENT bytes for the same window 10 minutes apart — vendor-side instability or a throttle-shaped partial/empty response. Any settle decision made during such an episode is unsafe; both payloads written to the transcript.`

### Stage 12 — resume / interrupted-pull / sleep check

**Runs:** scans the window ledger for PLANNED/FAILED outstanding windows; reads the pull journal (**new** —
G5: durable start/finish/verdict/heartbeat records under the lake root, to be written by both pull
surfaces) for a start-without-finish; compares wall-clock vs monotonic heartbeat deltas for sleep gaps (G6).

**PASS:** `PASS resume: no interrupted pull; 0 outstanding windows; no sleep gaps in the last journal`

**FAIL diagnostics:**
- `WARN resume[G5]: last pull journal shows STARTED 2026-07-01 22:14 IST with NO finish record and 1,204 windows outstanding — the overnight pull died mid-run (reboot/sleep/kill). Today's dashboard shows nothing about this (the verdict is in-memory only). Fix: re-pull; the ledger will resume exactly the outstanding windows.`
- `WARN resume[G6]: heartbeat wall-vs-monotonic delta shows the host SLEPT ~6h28m (01:12→07:40) during the pull. Fix for overnight pulls: caffeinate -i brutex fetch …, or keep the lid open on AC power; check pmset -g log for the sleep record.`

### Stage 13 — rate-ladder probe (opt-in `--rate-probe`)

**Runs:** 60 s of cheap calls at the configured ceiling through the REAL `RateController` + client,
recording every throttle response's exact shape (typed exception vs bare `GrowwAPIException` vs empty/HTML
body vs 5xx) and the achieved clean rate. Aborts at the first sustained burst. Evidence for R4/M6/M2/N3 —
records WHICH throttle shapes the vendor actually uses and whether 8 r/s sustains (the vendor was observed
429ing at 5.0; UNVERIFIED whether 8 sustains).

**PASS:** `PASS rate: sustained 8.0 r/s for 60 s, 0 throttle responses (ceiling holds — for this minute; sustained multi-hour behaviour still UNVERIFIED)`
**EVIDENCE:** `EVIDENCE rate[R4/M6]: throttle observed at 6.2 r/s as a BARE GrowwAPIException("Request limit exceeded") — this wording matches NO classifier hint; production would quarantine symbols as 'not on Groww'. File the errors.py hint addition; transcript recorded.`

---

## 5. Build plan

### 5.1 Files to touch

| File | Change | Est. size |
|---|---|---|
| `brutex/doctor_live.py` (**new**) | stage runner, stage registry, diagnostics catalog (every FAIL line above as a tested constant), transcript writer, call-budget guard | ~650-850 lines |
| `brutex/cli.py` | `doctor --live` flag + options plumbing; reuse of the sweep SHA resolver for stage 0 | ~70-110 lines |
| `brutex/adapters/groww/probe.py` | resurrect `probe_vix`/`probe_granularity` (currently dead code): accept an injected client + return `ProbeResult` rows the runner consumes; add bounded depth bisection | ~90-140 lines |
| `brutex/adapters/groww/token_store_aws.py` | reason-carrying load seam: `load_with_reason() -> tuple[token|None, LoadMiss|None]` (production `load()` delegates; the R7 fix later reuses it) | ~25-40 lines |
| `brutex/config/credentials.py` | expose a reason-carrying presence check (`groww_credentials_status() -> CredsStatus`) for the kit — and for the eventual R1 dashboard fix | ~20-35 lines |
| `brutex/adapters/groww/live.py` (or the client builder) | kit build mode with `token_factory` disabled (mint physically impossible — safety invariant 2); growwapi version stamp helper (G2) | ~30-50 lines |
| `brutex/data/integrity_verify.py` | thin runner wiring (no logic change) | ~15-25 lines |
| `brutex/data/bhavcopy.py` (**new**) | NSE bhavcopy fetch + parse + OHLC compare; SSRF-allowlist the archive host; injected HTTP getter | ~150-220 lines |
| `brutex/observability/pull_journal.py` (**new**) | durable start/finish/verdict/heartbeat journal under the lake root (G5); wall-vs-monotonic sleep detection (G6) | ~110-160 lines |
| `brutex/dashboard/pull_controller.py` + `app.py` | write journal records at pull start/finish (G5) — minimal wiring, no behavior change | ~30-50 lines |
| `brutex/config/paths.py` | `lake_root_identity()` helper: resolved path + ismount verdict + st_dev (G1); consumed by stage 0 and (later) the pull-start log fix | ~25-40 lines |

Production total: roughly **~1,250-1,700 added / ~60 modified lines across 8 existing files + 3 new modules.**

### 5.2 Tests (every failure mode simulated with the EXISTING fakes — no network in CI)

- `tests/unit/test_doctor_live.py` — one test per diagnostic line, driven by the existing fake seams:
  fake `AwsClient` raising each ClientError class (stage 1/3: AccessDenied-SSM, AccessDenied-KMS,
  ParameterNotFound, NoCredentialsError, wrong-region); fake SDK raising a **bare**
  `GrowwAPIException(code=401)` and a typed auth exception (stage 3); fake SDK missing `_request_get`
  (shape probe); payloads: `{"status":…,"payload":{…}}` envelope, `None`, bare list, empty candles,
  UTC-shifted string stamps, epoch stamps, `+05:30` suffix, null-OHLC epidemic (stage 5); 22-column CSV
  header (stage 4); injected `ntp_now` for skew and `totp_secret='ABC 8DEF'` for base32 (stage 2);
  tmp-path lake with monkeypatched `os.path.ismount` (stage 0/10); journal fixtures: start-without-finish
  and a wall-vs-monotonic gap (stage 12).
- `tests/golden/test_doctor_live_diagnostics.py` — byte-exact snapshot of the diagnostics catalog (the FAIL
  lines are the product; they must not drift silently).
- `tests/unit/test_bhavcopy.py` — parse fixtures + tolerance compare + SSRF allowlist refusal.
- `tests/unit/test_pull_journal.py` — journal round-trip, heartbeat, sleep-gap math, crash-truncation
  tolerance.
- Repo decree compliance: 100% line+branch on the new modules, mypy strict + pyright, ruff/pylint/bandit,
  property test for the bisection bounds (stage 6), no `print` (structlog + a console renderer for the
  operator-facing lines).

Test total: roughly **~900-1,300 lines.**

### 5.3 What stays genuinely UNVERIFIED until the kit runs on the Mac

The kit *measures* these; it cannot presume them (Golden Rule #1):
1. Single-active-token revocation (does a mint kill the sibling's token) — the kit avoids the hazard rather
   than testing it; verifying it needs a deliberate two-token experiment the operator must authorize.
2. The live 401/403 exception shape (typed vs bare) — stage 3 records it on first real occurrence.
3. Real token lifetime / the 06:00-IST boundary — transcript over days, not one run.
4. True 1m depth per index at the 2020 boundary — stage 6 answers it.
5. NSE-INDIAVIX availability + failure shape — stage 7 answers it.
6. Sustainable per-second rate / hidden daily quota / throttle shapes and wordings — stage 13 samples one
   minute; multi-hour sustain remains open until a real supervised backfill.
7. Epoch-vs-string timestamp encoding on real payloads (incl. options) — stage 5 records spot; options need
   an options-window probe run.
8. Groww's TOTP clock tolerance — stage 2 measures skew, not the vendor's acceptance window.
9. The operator's actual mount/venv/proxy/growwapi-version configuration — stage 0 reads it on-site.
10. App Nap / timer-throttling behaviour of a napping python pull process — stage 12's sleep detection
    gathers evidence; a controlled lid-close experiment confirms.

### 5.4 What the kit does NOT fix

The kit is a *diagnoser*. The register's "Fix needed" column (R1 loud-refusal gate, R2/R3 settle circuit
breaker, R4/M2 classifier widening, R7 readonly cold-start guard, R8 TLS classification, M1 StillStale
verdict, G1 mount guard, G2 version stamping, G5 journal surfacing in the dashboard) are separate PRs.
Three kit seams (stage-1 reason-carrying creds status, stage-3 reason-carrying token load, stage-0 lake
identity helper) are deliberately built as the shared foundations those fixes will reuse (COMMON).

