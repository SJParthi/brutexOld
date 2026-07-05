"""``brutex doctor --live`` — the Mac live-proof kit (DEC-DOCTOR-LIVE-PROOF-KIT-001).

ONE command the operator runs **on the Mac, with real AWS + real Groww
network**, that walks the exact production code path a live pull takes —
stage by stage — and, at the first divergence, prints a line that says
exactly WHAT is wrong and WHAT to do, cross-referenced to the pull-divergence
register (rows R1-R8 / M1-M17 / N1-N6 / G1-G7). It exists because the sandbox
is structurally blind (no creds, no vendor network — every green test proves
only the fakes) and because today's failure surfaces are silent (R1-R6,
G1-G3) or misleading (R7, R8, M1).

Design principles (the kit spec §2, non-negotiable):

1. **COMMON** — every stage calls the REAL production functions
   (``resolve_groww_aws_credentials``, ``ParameterStoreTokenStore``,
   ``build_live_client``, ``parse_candle_rows``, ``filter_intraday_session``,
   ``ParquetWriter``, ``WindowLedger``, ``verify_one`` /
   ``verify_lake_records``, ``check_derived_freshness``,
   ``resample_1m_to_daily``); never a reimplementation. Where production
   swallows the reason (R1's ``groww_credentials_present``, R7's
   degrade-to-``None`` token load) the kit calls the underlying function that
   still carries it (``resolve_groww_aws_credentials`` directly;
   ``ParameterStoreTokenStore.load_with_reason``).
2. **READ-ONLY TOKEN DISCIPLINE** — the kit NEVER mints a Groww token (a mint
   may revoke the shared Lambda/TickVault token — single-active-token,
   UNVERIFIED, treated as true). ``token_factory`` and ``totp_minter`` are
   wired to hard refusals (:func:`~brutex.adapters.groww.live.\
   mint_blocked_token_factory`); only the explicit ``--mint-anyway`` (printed
   with a red warning by the CLI) re-enables the real mint path.
3. **BOUNDED CALL BUDGET** — every authenticated vendor call passes a
   :class:`CallBudget` (default ≤ 40; ``--depth-bisect`` adds ≤ 60;
   ``--rate-probe`` adds ≤ 480 and aborts on the first sustained throttle
   burst). Exceeding the budget aborts the run, never the account.
4. **QUARANTINED WRITES** — stage 10-11 lake writes land ONLY under
   ``<lake_root>/_liveproof/<run_id>/`` (a real tree through the real writer;
   zero contamination of production partitions). ``--against-lake`` only
   READS the production lake.
5. **EVIDENCE OR IT DID NOT HAPPEN** — every stage appends its raw transcript
   (requests issued, payload shapes, timings) to
   ``<lake_root>/_liveproof/<run_id>/transcript.jsonl`` — the ADR-0009 open
   probe gates finally get real transcripts.
6. **FAIL-FORWARD** — default is fail-fast at the first FAIL; ``--keep-going``
   runs all stages. Exit code 0 = all PASS, 2 = at least one FAIL,
   3 = kit-internal error.

Honest envelope (Golden Rule #10): in the sandbox/CI every stage runs against
fakes — the live path itself is **UNVERIFIED until this tool runs on the
operator's Mac**; that is precisely the tool's purpose. The
:data:`DIAGNOSTICS` catalog is golden-tested byte-exactly so the operator
lines can never drift silently.
"""

from __future__ import annotations

import base64
import binascii
import inspect
import json
from collections.abc import Callable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Final

import structlog

from brutex.adapters.groww.auth import GrowwCredentials
from brutex.adapters.groww.aws_client import AwsClient
from brutex.adapters.groww.client import GrowwClientProtocol, GrowwSDK
from brutex.adapters.groww.constants import (
    BRUTE_FORCE_INDEX_SYMBOLS,
    EARLIEST_HISTORY,
    INDIA_VIX_SYMBOL,
)
from brutex.adapters.groww.errors import GrowwAuthError, GrowwError
from brutex.adapters.groww.http_timeout import REQUEST_METHOD_NAMES
from brutex.adapters.groww.instrument_loader import GrowwInstrumentFetchError, load_index
from brutex.adapters.groww.instruments import InstrumentIndex
from brutex.adapters.groww.live import (
    build_live_client,
    mint_blocked_token_factory,
    mint_blocked_totp_minter,
)
from brutex.adapters.groww.parser import CandleParseError, parse_candle_rows
from brutex.adapters.groww.probe import (
    VERDICT_ABSENT,
    VERDICT_OK,
    probe_depth,
    probe_vix,
)
from brutex.adapters.groww.token_cache import CachedToken, current_session
from brutex.adapters.groww.token_store_aws import build_readonly_token_store_with
from brutex.adapters.rate_common import RateController
from brutex.calendars.nse import HolidayDataUnavailableError
from brutex.calendars.observed_sessions import (
    is_trading_day_checked_observed,
    load_observed_holidays,
)
from brutex.config.credentials import resolve_groww_aws_credentials
from brutex.config.credentials_types import CredentialsError, GrowwFileCredentials
from brutex.config.dotenv import parse_dotenv
from brutex.config.paths import lake_root_identity, resolve_lake_root
from brutex.data.bhavcopy import (
    BhavcopyError,
    compare_daily_bar,
    fetch_index_bhavcopy,
    nse_index_name_for,
)
from brutex.data.models import CandleBar
from brutex.data.raw_integrity import RawIntegrityError, RawIntegrityRecord, build_record
from brutex.data.session import IST, filter_intraday_session
from brutex.models import BruteXError, InstrumentFormatError
from brutex.observability.pull_journal import last_run, read_journal, sleep_gaps

_LOG = structlog.get_logger("brutex.doctor_live")

# ────────────────────────────────────────────────────────────────────────
# Status literals + stage identity
# ────────────────────────────────────────────────────────────────────────

STATUS_PASS: Final[str] = "PASS"  # noqa: S105 — a status literal, not a credential
STATUS_FAIL: Final[str] = "FAIL"
STATUS_WARN: Final[str] = "WARN"
STATUS_SKIP: Final[str] = "SKIP"

#: Default authenticated-vendor-call budget (kit spec §2.3).
DEFAULT_CALL_BUDGET: Final[int] = 40
#: Extra budget granted by ``--depth-bisect`` (≤ 20 calls × 3 indexes).
DEPTH_BISECT_BUDGET: Final[int] = 60
#: Extra budget granted by ``--rate-probe`` (kit spec stage 13).
RATE_PROBE_BUDGET: Final[int] = 480

#: Consecutive raised throttle/vendor errors that abort the rate probe.
RATE_PROBE_ABORT_BURST: Final[int] = 3

#: NTP-ish clock-skew failure threshold (Groww's real tolerance UNVERIFIED —
#: ±30 s is the honest assumption stated on the PASS line).
CLOCK_SKEW_FAIL_S: Final[float] = 30.0

#: Ratio thresholds for stage 5's assertions.
SESSION_DROP_FAIL_RATIO: Final[float] = 0.05
OHLC_SKIP_WARN_RATIO: Final[float] = 0.10

#: The RFC-4648 base32 alphabet (+ padding) for the M3 secret pre-check.
_BASE32_ALPHABET: Final[frozenset[str]] = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567=")

# ────────────────────────────────────────────────────────────────────────
# Diagnostics catalog — THE product. Every operator-facing line is a tested
# constant here, cross-referenced to the divergence register, and snapshot
# golden-tested byte-exactly (tests/golden/test_doctor_live_diagnostics.py)
# so the wording can never drift silently.
# ────────────────────────────────────────────────────────────────────────

DIAGNOSTICS: Final[dict[str, str]] = {
    # ── stage 0 · env-identity ─────────────────────────────────────────
    "env.pass": (
        "PASS env: commit {sha} ({behind} behind origin/main) · brutex {brutex_version} · "
        "growwapi {growwapi_version} · python {python_version}\n"
        "          install={location} ({editable}) · lake={lake} ({mount}) · proxies={proxies}"
    ),
    "env.lake_unmounted": (
        "FAIL env[G1]: lake root {lake_root} — {volume} is NOT a mountpoint. Your external SSD "
        "is unmounted; a pull right now would silently rebuild the lake AND the window ledger "
        "on the internal disk, and the two trees will disagree after remount. Fix: remount the "
        "SSD; then diff ~/.brutex/lake vs the SSD lake for phantom bars/ledger files."
    ),
    "env.dotenv_split": (
        "WARN env[G1]: dashboard and CLI would resolve DIFFERENT .env files ({path_a} vs "
        "{path_b}) — BRUTEX_LAKE_ROOT may differ per surface. Fix: consolidate into "
        "~/.brutex/.env."
    ),
    "env.growwapi_missing": (
        "FAIL env[G2]: growwapi is NOT installed in THIS venv — no live pull is possible from "
        "this interpreter. Fix: pip install 'brutex[data]' (or pip install growwapi) in the "
        "venv that runs the pull."
    ),
    "env.growwapi_drift": (
        "WARN env[G2]: growwapi {installed} installed; this code is written against the 1.5.x "
        "surface (pin >=1.0,<2.0). Exception class names / private HTTP methods may have "
        "drifted — stage 3 will probe the shape. Fix if stage 3 fails: pip install "
        "'growwapi==1.5.*'."
    ),
    "env.behind_origin": (
        "WARN env[G3]: checkout {sha} is {behind} commits behind origin/main — the failure you "
        "are debugging may already be fixed. Fix: git pull && pip install -e ."
    ),
    "env.not_editable": (
        "WARN env[G3]: brutex is NOT an editable install (running from {location}) — the code "
        "running may not be the code in your checkout. Fix: pip install -e <your bruteX "
        "checkout>"
    ),
    "env.proxy": (
        "WARN env[R8]: system proxy detected ({proxies}) — Lambda/CI never see this; TLS "
        "interception commonly presents as an endless amber 'transient' pull. Stage 3/4 will "
        "exercise real TLS."
    ),
    # ── stage 1 · creds-presence (AWS SSM) ─────────────────────────────
    "creds.pass": (
        "PASS creds: api-key + totp-secret read from SSM "
        "/tickvault/prod/groww/{{api-key,totp-secret}} (region {region}, {ms} ms)"
    ),
    "creds.failed": "FAIL creds[R1]: {reason}",
    "creds.synthetic_note": (
        "NOTE[R1]: on a dashboard WITHOUT the R1 fix "
        "(DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001) this EXACT failure silently downgrades "
        "every pull to SYNTHETIC data with a green verdict (the '● PREVIEW' badge is the "
        "only trace) — every 'successful' pull run on such a build while this failure "
        "existed fetched ZERO real bars. With that fix deployed, real pulls REFUSE loudly "
        "(HTTP 503 + red credentials_missing verdict) instead of downgrading."
    ),
    "creds.fix_boto3": "FIX[R1]: boto3 is missing from THIS venv → pip install 'brutex[aws]'.",
    "creds.fix_no_aws_creds": (
        "FIX[R1]: NoCredentialsError → run 'aws configure', set AWS_PROFILE, or renew your "
        "SSO session."
    ),
    "creds.fix_ssm_denied": (
        "FIX[R1]: AccessDeniedException on ssm:GetParameter → grant the reader policy on "
        "/tickvault/prod/groww/* to this role."
    ),
    "creds.fix_kms_denied": (
        "FIX[R1]: KMS AccessDenied → grant kms:Decrypt on the credentials CMK to this role."
    ),
    "creds.fix_region": (
        "FIX[R1]: parameters absent in region {region} → verify "
        "BRUTEX_TOKEN_AWS_REGION=ap-south-1 (the params live in ap-south-1), or seed them "
        "(deploy/aws_token_minter/deploy.sh)."
    ),
    # ── stage 2 · totp-clock ───────────────────────────────────────────
    "totp.pass": (
        "PASS totp: secret is valid base32 ({length} chars); 6-digit code minted; host clock "
        "offset {offset} s vs the reference clock (Groww tolerance UNVERIFIED — assume "
        "±30 s)"
    ),
    "totp.offset_unknown": (
        "NOTE totp[N1]: reference clock unreachable ({error}) — host clock skew UNVERIFIED "
        "this run."
    ),
    "totp.bad_base32": (
        "FAIL totp[M3]: TOTP secret in SSM is NOT valid base32 ({problem}). In production this "
        "crashes as a raw hint-free 'Error: Non-base32 digit found' (the mint sits outside "
        "auth.py's sanitizing try). Fix: re-seed /tickvault/prod/groww/totp-secret with the "
        "exact base32 secret from the Groww developer page (no spaces/dashes; note 0/1/8 are "
        "not base32)."
    ),
    "totp.pyotp_missing": (
        "FAIL totp[M3]: pyotp is not installed in THIS venv — the production mint would crash "
        "with a raw ModuleNotFoundError. Fix: pip install pyotp (or reinstall brutex extras)."
    ),
    "totp.mint_failed": (
        "FAIL totp[M3]: TOTP code mint failed ({error}) — in production this crashes raw and "
        "hint-free before auth.py's sanitizing try. Fix: re-seed the SSM totp-secret (see the "
        "bad-base32 line's recipe)."
    ),
    "totp.clock_skew": (
        "FAIL totp[N1]: host clock is {offset} s {direction} of the reference clock — TOTP "
        "codes may be rejected and the vendor error will look like wrong credentials (do NOT "
        "rotate them). Fix: System Settings → General → Date & Time → Set time "
        "automatically."
    ),
    # ── stage 3 · token-read (NO MINT) + smoke call + SDK shape ────────
    "token.pass": (
        "PASS token: SSM token present (param {param}); authenticated call OK ({ms} ms, "
        "{rows} rows); SDK surface matches the tested 1.5.x expectations ({methods} "
        "_request_* methods, signature OK)"
    ),
    "token.read_error": (
        "FAIL token[R7]: SSM token read FAILED with {error_type} — NOT 'token absent'. "
        "Production cold-start cannot tell these apart: it fails with a MISLEADING "
        "deploy-the-minter error (and before DEC-REAUTH-COLDSTART-READONLY-001 it even "
        "performed a REAL local Groww mint, possibly revoking the shared token TickVault + "
        "the Lambda own). Fix the reader-side AWS failure "
        "({detail}); a KMS AccessDenied needs kms:Decrypt on the token CMK for this role. Do "
        "NOT deploy anything."
    ),
    "token.absent": (
        "FAIL token[R7]: SSM token genuinely ABSENT ({param}: {detail}). Fix: deploy/run the "
        "minter Lambda (deploy/aws_token_minter/deploy.sh) or invoke it: aws lambda invoke "
        "--function-name groww-token-minter /dev/stdout. The kit did NOT mint locally (that "
        "could revoke a sibling's token)."
    ),
    "token.rejected": (
        "FAIL token[M1]: token present but the vendor REJECTED it ({error}) — the shared token "
        "in SSM is DEAD and the Lambda will not re-mint until the next scheduled ~06:05 IST "
        "mint (log-derived, UNVERIFIED) — or invoke it now. Production pulls today would grind "
        "all day as amber 'transient — re-pull to finish' (a futile prescription). Fix: aws "
        "lambda invoke --function-name groww-token-minter /dev/stdout, then re-run this stage."
    ),
    "token.bare_auth_evidence": (
        "EVIDENCE[M2]: the rejection arrived as a BARE GrowwAPIException (code={code}, "
        "\"{text}\") — production classifies this TERMINAL 'not on Groww' and never re-auths. "
        "This live shape was previously UNVERIFIED; the transcript recorded it — file the "
        "errors.py fix (add 401/403 to _CODE_TO_ERROR)."
    ),
    "token.smoke_failed": (
        "FAIL token: authenticated smoke call failed ({error}) — not classified as an auth "
        "rejection; see the TLS/network diagnostics and the transcript."
    ),
    "token.tls": (
        "FAIL token[R8]: authenticated call failed with CERTIFICATE_VERIFY_FAILED — a "
        "PERMANENT TLS failure that production classifies as a retryable network blip (endless "
        "amber 'transient'). Fix: run /Applications/Python*/Install Certificates.command; "
        "check HTTPS_PROXY and macOS system proxy; corporate VPN MITM needs its CA in certifi."
    ),
    "sdk.shape": (
        "FAIL sdk[G2]: growwapi {version} SDK shape drifted from the tested 1.5.x surface: "
        "{problems}. A missing _request_get means the socket-timeout patch silently degrades "
        "to NO-PATCH; slow networks stall ~4.5 min per window with no timeout. Fix: pin "
        "growwapi==1.5.* until http_timeout.py is updated."
    ),
    "token.mint_anyway": (
        "WARN token: --mint-anyway is ACTIVE — the kit MAY mint a real Groww token. Under the "
        "recorded single-active-token behaviour (UNVERIFIED) this can REVOKE the shared token "
        "the Lambda/TickVault own and stall every sibling pull until 06:05 IST. Proceeding "
        "because you asked."
    ),
    # ── stage 4 · instrument master ────────────────────────────────────
    "master.pass": (
        "PASS master: 21-column header OK; {universe} in-scope rows ({eq} EQ); NSE-NIFTY + "
        "NSE-BANKNIFTY + BSE-SENSEX + NSE-INDIAVIX present"
    ),
    "master.header_drift": (
        "FAIL master[M17]: instrument-master header drift — {error}. The strict tuple-equality "
        "check bricks the entire universe load until a code change ships. Fix: file the "
        "additive-column tolerance change; short-term, pin the master snapshot."
    ),
    "master.unreachable": (
        "FAIL master[R8/G4]: cannot reach the instrument master: {error} — network/TLS/proxy "
        "failure BEFORE authentication is even involved. Same fixes as stage 3's TLS line "
        "(Install Certificates.command / HTTPS_PROXY / macOS system proxy)."
    ),
    "master.universe_gap": (
        "FAIL master: parsed {universe} in-scope rows but the Track-1 universe is incomplete — "
        "missing: {missing}. A pull would quarantine/skip these symbols. Fix: inspect the "
        "master CSV for renamed groww_symbols (transcript has the counts)."
    ),
    # ── stage 5 · spot 1-minute probe through the REAL pipeline ───────
    "spot1m.pass": (
        "PASS spot-1m: {raw} raw → {session} session bars for {day}; first bar "
        "{first_ist} IST ({encoding}-encoded); {skipped} bars skipped for absent OHLC; "
        "envelope shape matches parser expectations"
    ),
    "spot1m.non_mapping": (
        "FAIL spot-1m[M10]: SDK returned {type_name} instead of a Mapping — production would "
        "crash the ENTIRE multi-symbol pull with a raw TypeError in parser.py. Fix: SDK "
        "envelope drift — pin growwapi; payload sample written to the transcript."
    ),
    "spot1m.no_candles_key": (
        "FAIL spot-1m[M11]: payload has NO top-level 'candles' key (keys present: {keys}) — "
        "production would quarantine EVERY symbol as 'not on Groww'. Fix: SDK envelope drift — "
        "pin growwapi or patch parser.py; payload sample written to the transcript."
    ),
    "spot1m.unparseable_ts": (
        "FAIL spot-1m[M13]: {error} — the parser accepts only '%Y-%m-%d %H:%M:%S' (T or space) "
        "or epoch seconds. Production quarantines every symbol as 'not on Groww'. Offending "
        "raw value recorded in the transcript."
    ),
    "spot1m.parse_failed": (
        "FAIL spot-1m: candle payload failed the production parser: {error}. Production "
        "quarantines the symbol; payload sample in the transcript."
    ),
    "spot1m.clean_empty": (
        "FAIL spot-1m[R2/R3]: clean-but-EMPTY 200 for {day}, a day NSE definitely traded — "
        "either a throttle-shaped empty-200 or a mis-addressed request (the proven M&M-'&' "
        "class). Production counts this toward a SKIPPED settle — a pull-wide burst of these "
        "trips the mass-empty circuit breaker and HALTS the pull "
        "(DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001), and `brutex reopen-skipped` can re-open "
        "already-settled windows — but do NOT run a backfill until explained; re-run this "
        "stage in 10 min to separate throttle from addressing."
    ),
    "spot1m.session_shift": (
        "FAIL spot-1m[M12]: first bar parses to {first_ist} IST and {dropped}/{total} bars "
        "fall outside the NSE session — timestamps are likely UTC-encoded but treated as IST "
        "(+05:30 systematic shift). Production silently DROPS these and stores the survivors "
        "under WRONG stamps. Do NOT run a backfill until the parser semantics are fixed."
    ),
    "spot1m.first_bar_missing": (
        "FAIL spot-1m[M12]: first session bar is {first_ist} IST, not 09:15, while only "
        "{dropped}/{total} bars fall outside the NSE session — missing OPENING bars, not "
        "necessarily a timezone shift (a +05:30 UTC-shift would drop far more of the day). "
        "Production would store the day WITHOUT its open. Do NOT run a backfill until the "
        "09:15-{first_ist} gap is explained; transcript records the first raw stamp."
    ),
    "spot1m.null_ohlc": (
        "WARN spot-1m[M15]: {percent}% of bars skipped for absent/zero OHLC — production "
        "drops these with ZERO logging (bare except…continue). Vendor data-quality "
        "incident or column misalignment; sample rows in the transcript."
    ),
    "spot1m.vendor_error": (
        "FAIL spot-1m: vendor call failed: {error}. See the stage-3 diagnostics for auth/TLS "
        "causes; transcript recorded."
    ),
    # ── stage 6 · 2020-depth probe ─────────────────────────────────────
    "depth.pass": ("PASS depth: {details} — GROWW_HISTORY_FLOOR={floor} CONFIRMED at the boundary"),
    "depth.shallow": (
        "FAIL depth[R6]: {symbol} 1m actually starts {floor} (probed floor; configured floor "
        "{expected}). A {expected}→today backfill would burn quota on every earlier "
        "window and permanently settle them SKIPPED under a green verdict; sweeps over the "
        "missing years would silently run on derived-daily-only data. Fix: set "
        "GROWW_HISTORY_FLOOR to the probed floor per index (or fix the settle machinery "
        "first); record the floor in lake metadata."
    ),
    "depth.unknown": (
        "FAIL depth[R6]: {symbol} 1m window at {expected} is EMPTY and the true floor was not "
        "located ({detail}). Do NOT run a backfill; re-run with --depth-bisect to locate the "
        "real floor first."
    ),
    "depth.autobisect_floor": (
        "WARN depth[R6]: 1-minute depth floor for {symbol}: {floor} — the configured floor "
        "{expected} has NO data at the boundary; the kit AUTO-BISECTED the true floor "
        "({calls} calls, no operator command needed). A {expected}→today backfill would burn "
        "quota on the pre-{floor} windows and settle them SKIPPED; set GROWW_HISTORY_FLOOR to "
        "the probed floor per index (or fix the settle machinery first) and record the floor "
        "in lake metadata."
    ),
    "depth.autobisect_absent": (
        "FAIL depth[R6]: {symbol} 1m returned NO data anywhere in [{expected} → today] — the "
        "automatic bounded bisect already ran ({detail}). No operator command is needed (the "
        "autorun re-probes on the next real pull); do NOT run a backfill until the vendor "
        "serves 1-minute data for this symbol."
    ),
    "depth.error": "FAIL depth: {detail}",
    # ── stage 7 · VIX probe (ADR-0009 HARD-GATE) ───────────────────────
    "vix.pass": (
        "PASS vix: NSE-INDIAVIX served {daily} daily bars + {m1} 1m rows — the ADR-0009 §3.2 "
        "HARD-GATE is SATISFIED (transcript recorded)"
    ),
    "vix.absent": (
        "FAIL vix[R5]: Groww serves NO candles for NSE-INDIAVIX (shape: {shape}). This is the "
        "ADR-0009 §3.2 MIGRATION-RESCINDING hard gate — and today's pull path would normalize "
        "it to a green 'No action needed' while vix_at_entry/vix_at_exit silently have no "
        "data. Do not proceed with Track-1 pulls; escalate per ADR-0009 (rescind or source "
        "VIX elsewhere)."
    ),
    "vix.absent_1m": (
        "FAIL vix[R5]: NSE-INDIAVIX serves NO 1-minute candles, but the 1day fallback "
        "returned {daily} bars — VIX EXISTS on Groww, so the ADR-0009 §3.2 "
        "MIGRATION-RESCINDING gate does NOT fire on this evidence; intraday "
        "vix_at_entry/vix_at_exit stamping is still impossible. Escalate per ADR-0009 "
        "(accept daily-granularity VIX stamps or source intraday VIX elsewhere) before "
        "Track-1 pulls."
    ),
    # ── stage 8 · 1-day derivation + NSE bhavcopy cross-check ─────────
    "bhavcopy.pass": (
        "PASS bhavcopy: derived 1day OHLC for {index_name} {day} matches the NSE snapshot "
        "(max Δ {delta} ≤ one tick {tolerance})"
    ),
    "bhavcopy.mismatch": (
        "FAIL bhavcopy[M12/M15]: derived {fields} disagree with the official NSE snapshot for "
        "{day} (max Δ {delta} > tick tolerance {tolerance}) — the vendor 1m stream "
        "disagrees with the exchange. Likely thinned/mis-stamped session bars upstream. Do "
        "not trust the lake for this range until explained; see stage 5's drop-ratio "
        "evidence."
    ),
    "bhavcopy.unreachable": (
        "WARN bhavcopy: NSE archive unreachable/unparseable ({error}) — external dependency, "
        "not a Groww fault; re-run later. (Cross-check skipped, NOT passed.)"
    ),
    "bhavcopy.wiring": (
        "FAIL bhavcopy: kit wiring: URL blocked by local allowlist ({error}) — the stage-8 NSE "
        "snapshot fetch was routed through a getter whose OWN allowlist rejects the NSE archive "
        "host. This is a kit-internal wiring bug (KitDeps.nse_http_get must be the "
        "NSE-allowlisted brutex.data.bhavcopy.urllib_http_get_nse, never the Groww "
        "instrument-master shim), NOT an external NSE outage. The cross-check did not run."
    ),
    "bhavcopy.unmapped": (
        "WARN bhavcopy: {symbol} has no NSE indices-archive mapping (BSE indexes live in a "
        "different archive) — cross-check skipped, NOT passed."
    ),
    "bhavcopy.no_row": (
        "WARN bhavcopy: the NSE snapshot for {day} has no row named {index_name} — "
        "cross-check skipped, NOT passed (transcript lists the names present)."
    ),
    "bhavcopy.no_daily_bar": (
        "WARN bhavcopy: no complete 1day bar could be derived for {day} from the stage-5 "
        "session bars — cross-check skipped, NOT passed."
    ),
    # ── stage 9 · resample determinism (reuses doctor check 4) ─────────
    "resample.drift": (
        "FAIL resample: {detail} — stored derived TFs drift from a fresh re-derivation. Do "
        "not trust derived-TF sweeps until re-derived."
    ),
    # ── stage 10 · lake write + read-back + ledger ─────────────────────
    "lake.pass": (
        "PASS lake: {bars} bars → {path} · read-back bit-exact (idempotent re-write "
        "added 0, changed 0) · ledger row {status} · audit line appended (chain verified)"
    ),
    "lake.split_brain": (
        "FAIL lake[G1]: this process resolved lake_root={resolved} but {env_file} says "
        "{env_value} — TWO lake roots are in play between your surfaces. Fix: set "
        "BRUTEX_LAKE_ROOT in ONE place; then reconcile the two trees (both contain ledgers)."
    ),
    "lake.write_failed": (
        "FAIL lake: write failed: {error} (disk full / read-only volume / permissions) — "
        "production maps this to a pull-halt; fix the volume first."
    ),
    "lake.readback_drift": (
        "FAIL lake: read-back is NOT bit-exact ({detail}) — the writer's idempotence check "
        "found the stored partition differing from what was just written. Do not trust this "
        "volume until explained (transcript has both write results)."
    ),
    "lake.ledger_state": (
        "FAIL lake: ledger row did not land SUCCESS (status={status}) — the write path and "
        "the skip authority disagree; a re-pull would re-fetch or wrongly skip this window."
    ),
    # ── stage 11 · integrity re-fetch verify ───────────────────────────
    "integrity.pass": (
        "PASS integrity: re-fetched window hash matches capture (vendor is serving stable bytes)"
    ),
    "integrity.mismatch": (
        "FAIL integrity[R2]: the vendor returned DIFFERENT bytes for the same window minutes "
        "apart — vendor-side instability or a throttle-shaped partial/empty response. Any "
        "settle decision made during such an episode is unsafe; both hashes written to the "
        "transcript."
    ),
    "integrity.lake_pass": (  # nosec B105 — a diagnostic template, not a credential
        "PASS integrity: {matched}/{total} recorded production-lake windows re-fetched "
        "bit-identical"
    ),
    "integrity.lake_mismatch": (
        "FAIL integrity[R2]: {mismatched}/{total} recorded production-lake windows re-fetched "
        "DIFFERENT bytes — vendor drift/restatement or throttle-shaped partials; details in "
        "the transcript."
    ),
    "integrity.refetch_failed": (
        "FAIL integrity: re-fetch failed ({error}) — verification inconclusive, NOT passed."
    ),
    # ── stage 12 · resume / interrupted-pull / sleep check ─────────────
    "resume.pass": (
        "PASS resume: no interrupted pull; {outstanding} outstanding windows; no sleep gaps "
        "in the last journal"
    ),
    "resume.interrupted": (
        "WARN resume[G5]: last pull journal shows STARTED {started} with NO finish record and "
        "{outstanding} windows outstanding — the pull died mid-run (reboot/sleep/kill). "
        "Today's dashboard shows nothing about this (the verdict is in-memory only). Fix: "
        "re-pull; the ledger will resume exactly the outstanding windows."
    ),
    "resume.slept": (
        "WARN resume[G6]: journal wall-vs-monotonic delta shows the host SLEPT ~{minutes} min "
        "during the pull ({window}). Fix for overnight pulls: caffeinate -i brutex fetch "
        "…, or keep the lid open on AC power; check pmset -g log for the sleep record."
    ),
    # ── stage 13 · rate-ladder probe (opt-in) ──────────────────────────
    "rate.pass": (
        "PASS rate: sustained {rate} r/s clean for {seconds} s ({calls} calls, 0 raised "
        "throttles) — ceiling holds for THIS minute only; sustained multi-hour behaviour "
        "still UNVERIFIED, and in-call retries are invisible here (register M8)"
    ),
    "rate.throttle_evidence": (
        "EVIDENCE rate[R4/M6]: throttle observed at ~{rate} r/s as {shape} — if this wording "
        "matches NO classifier hint, production would quarantine symbols as 'not on Groww'. "
        "File the errors.py hint addition; transcript recorded."
    ),
    "rate.aborted": (
        "WARN rate: aborted after {bursts} consecutive raised vendor errors (sustained "
        "throttle burst) — do NOT raise the ceiling; the transcript records every shape."
    ),
    # ── runner ─────────────────────────────────────────────────────────
    "kit.budget_exhausted": (
        "FAIL {stage}: vendor-call budget exhausted ({detail}) — the kit stops rather than "
        "hammer the account (SAFE bound; kit spec §2.3)."
    ),
    "kit.stage_skipped": "SKIP {stage}: {reason}",
}


def render_diagnostic(code: str, /, **fields: object) -> str:
    """Render one catalog line. An unknown *code* is a kit bug — raises ``KeyError``."""
    return DIAGNOSTICS[code].format(**fields)


# ────────────────────────────────────────────────────────────────────────
# Infrastructure — outcomes, report, budget, transcript
# ────────────────────────────────────────────────────────────────────────


class KitInternalError(BruteXError):
    """A kit-internal failure (exit code 3) — the kit itself, not the pull path."""


class CallBudgetExceeded(BruteXError):
    """The bounded vendor-call budget would be exceeded (kit spec §2.3)."""


class CallBudget:
    """A hard cap on authenticated vendor calls for one kit run (O(1) spend)."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._used = 0

    @property
    def limit(self) -> int:
        """The configured hard cap."""
        return self._limit

    @property
    def used(self) -> int:
        """Calls spent so far."""
        return self._used

    @property
    def remaining(self) -> int:
        """Calls still available."""
        return self._limit - self._used

    def spend(self, count: int = 1, *, label: str) -> None:
        """Spend *count* calls, raising :class:`CallBudgetExceeded` at the cap."""
        if self._used + count > self._limit:
            raise CallBudgetExceeded(
                f"{label}: spending {count} would exceed the {self._limit}-call budget "
                f"({self._used} already used)"
            )
        self._used += count


class Transcript:
    """Evidence writer — one JSON line per event (kit spec §2.5).

    The sink is injected (a real appender under ``_liveproof/<run_id>/`` in
    production, a list collector in tests); the writer never raises on sink
    failure is NOT promised — evidence loss must be loud, so sink errors
    propagate as :class:`KitInternalError` from the runner.
    """

    def __init__(self, sink: Callable[[str], None], *, now_utc: Callable[[], datetime]) -> None:
        self._sink = sink
        self._now_utc = now_utc

    def event(self, name: str, /, **fields: object) -> None:
        """Append one transcript event (canonical sorted-key JSON, stringify fallback)."""
        record: dict[str, object] = {"ts_utc": self._now_utc().isoformat(), "event": name}
        record.update(fields)
        self._sink(json.dumps(record, sort_keys=True, default=str))


def make_file_transcript_sink(path: Path) -> Callable[[str], None]:
    """A transcript sink appending lines to *path* (parents created lazily)."""

    def _write(line: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    return _write


@dataclass(frozen=True, slots=True)
class StageOutcome:
    """One stage's verdict: the status plus the exact operator-facing lines."""

    stage: int
    name: str
    status: str
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class KitReport:
    """The whole run's outcomes + identity (renderable as text or JSON)."""

    run_id: str
    transcript_path: Path
    outcomes: tuple[StageOutcome, ...]

    @property
    def failed(self) -> bool:
        """``True`` iff any stage FAILed."""
        return any(outcome.status == STATUS_FAIL for outcome in self.outcomes)

    @property
    def exit_code(self) -> int:
        """0 = all green, 2 = at least one FAIL (3 = kit-internal, raised not returned)."""
        return 2 if self.failed else 0

    def format(self) -> str:
        """Render the operator-facing text report."""
        lines = [
            f"brutex doctor --live — Mac live-proof kit (run {self.run_id})",
            f"transcript: {self.transcript_path}",
            "=" * 72,
        ]
        for outcome in self.outcomes:
            lines.append(f"[stage {outcome.stage:>2} · {outcome.name}]")
            lines.extend(outcome.lines)
        lines.append("=" * 72)
        lines.append("OVERALL: " + ("FAIL" if self.failed else "PASS"))
        return "\n".join(lines)

    def to_json(self) -> str:
        """Render the report as JSON (``--json``)."""
        return json.dumps(
            {
                "run_id": self.run_id,
                "transcript": str(self.transcript_path),
                "exit_code": self.exit_code,
                "stages": [
                    {
                        "stage": outcome.stage,
                        "name": outcome.name,
                        "status": outcome.status,
                        "lines": list(outcome.lines),
                    }
                    for outcome in self.outcomes
                ],
            },
            indent=2,
        )


# ────────────────────────────────────────────────────────────────────────
# Deps / options / context
# ────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class KitDeps:
    """Every side-effecting probe the kit needs, as injected callables.

    The REAL composition lives in :func:`build_real_deps` (the only
    network/git/clock code, ``# pragma: no cover``); tests construct this
    dataclass with fakes and drive every stage branch offline (the sandbox's
    honest limit — the live path is verified only on the Mac).
    """

    # clocks
    now_utc: Callable[[], datetime]
    monotonic: Callable[[], float]
    # stage 0 — env identity
    git_sha: Callable[[], str]
    behind_origin: Callable[[], int | None]
    growwapi_version: Callable[[], str | None]
    package_location: Callable[[], str]
    python_version: Callable[[], str]
    getproxies: Callable[[], Mapping[str, str]]
    env: Mapping[str, str]
    home: Path
    cwd: Path
    repo_root: Path | None
    ismount: Callable[[str], bool]
    # stage 1 — creds
    resolve_creds: Callable[[], GrowwFileCredentials]
    broker_region: Callable[[], str]
    # stage 2 — totp/clock
    totp_minter: Callable[[str], str]
    clock_offset_s: Callable[[], float]
    # stage 3 — token + client
    aws_client_factory: Callable[[], AwsClient]
    sdk_factory: Callable[[str], GrowwSDK]
    real_token_factory: Callable[[str, str], str]
    real_totp_minter: Callable[[str], str]
    controller_factory: Callable[[], RateController]
    # stage 4 — keyless HTTP against the Groww instrument-master host
    http_get: Callable[[str], str]
    # stage 8 — keyless HTTP against the NSE archive hosts. MUST be the
    # NSE-allowlisted getter (:func:`brutex.data.bhavcopy.urllib_http_get_nse`),
    # NEVER the Groww instrument-master shim: that shim's SSRF allowlist admits
    # only growwapi-assets.groww.in, so routing the bhavcopy fetch through it
    # blocks archives.nseindia.com on OUR OWN allowlist and the stage would
    # mislabel the kit bug as an "external dependency" (DEC-DOCTOR-KIT-HONESTY-001).
    nse_http_get: Callable[[str], str]
    # stage 5 — probe day
    probe_day: Callable[[], date]


@dataclass(slots=True)
class KitOptions:
    """The ``brutex doctor --live`` invocation options (kit spec §3)."""

    lake_root: Path | None = None
    symbol: str = "NSE-NIFTY"
    stages: frozenset[int] | None = None
    keep_going: bool = False
    depth_bisect: bool = False
    #: The ZERO-COMMAND bisect mode (DEC-DOCTOR-KIT-HONESTY-001): an EMPTY
    #: stage-6 boundary window automatically runs the SAME bounded bisect
    #: ``--depth-bisect`` would (spent ONLY on the failing symbol — a boundary
    #: hit returns after 1 call) and a FOUND floor renders as an advisory WARN
    #: naming the floor, never a FAIL instructing the operator to type a
    #: command. Manual ``--depth-bisect`` behaviour is byte-unchanged. Since
    #: the 2026-07-03 operator decree the dashboard AUTORUN no longer runs
    #: stage 6 at all, so this flag fires only for an explicit stage-6
    #: selection (``run_kit_stages`` sets it iff the run includes stage 6).
    auto_bisect: bool = False
    rate_probe: bool = False
    against_lake: bool = False
    json_output: bool = False
    mint_anyway: bool = False
    rate_probe_seconds: float = 60.0


@dataclass(slots=True)
class KitContext:
    """Mutable run state threaded between stages (client, captures, budget…)."""

    options: KitOptions
    lake_root: Path
    run_id: str
    quarantine_root: Path
    budget: CallBudget
    transcript: Transcript
    creds: GrowwFileCredentials | None = None
    token: str | None = None
    token_param: str = ""
    client: GrowwClientProtocol | None = None
    controller: RateController | None = None
    master: InstrumentIndex | None = None
    probe_day: date | None = None
    payload: Mapping[str, object] | None = None
    bars: list[CandleBar] = field(default_factory=list)
    session_bars: list[CandleBar] = field(default_factory=list)
    record: RawIntegrityRecord | None = None

    def exchange_segment(self) -> tuple[str, str]:
        """The (exchange, segment) request coordinates for ``options.symbol``."""
        exchange = self.options.symbol.split("-", 1)[0]
        return exchange, "CASH"

    def symbol_is_index(self) -> bool:
        """Best-effort index detection for the parser's ``drop_volume`` rule."""
        if self.master is not None:
            instrument = self.master.by_groww_symbol.get(self.options.symbol)
            if instrument is not None:
                return str(instrument.instrument) == "IDX"
        return self.options.symbol in BRUTE_FORCE_INDEX_SYMBOLS or (
            self.options.symbol == INDIA_VIX_SYMBOL
        )


def _skip(stage: int, name: str, reason: str) -> StageOutcome:
    """A SKIP outcome with the catalog's uniform wording."""
    return StageOutcome(
        stage=stage,
        name=name,
        status=STATUS_SKIP,
        lines=(render_diagnostic("kit.stage_skipped", stage=name, reason=reason),),
    )


def _rows_in(payload: object) -> int:
    """Defensive candle-row count of a raw payload (never trusts the shape)."""
    if not isinstance(payload, Mapping):
        return 0
    candles = payload.get("candles")
    if not isinstance(candles, (list, tuple)):
        return 0
    return len(candles)


def _day_window(day: date) -> tuple[str, str]:
    """The full-session (start, end) request bounds for one trading *day*."""
    return f"{day.isoformat()} 09:15:00", f"{day.isoformat()} 15:30:00"


# ────────────────────────────────────────────────────────────────────────
# Stage 0 — env-identity
# ────────────────────────────────────────────────────────────────────────


def _dotenv_lake_values(deps: KitDeps) -> dict[Path, str]:
    """``BRUTEX_LAKE_ROOT`` per existing ``.env`` candidate (repo → cwd → home)."""
    candidates: list[Path] = []
    if deps.repo_root is not None:
        candidates.append(deps.repo_root / ".env")
    candidates.append(deps.cwd / ".env")
    candidates.append(deps.home / ".brutex" / ".env")
    values: dict[Path, str] = {}
    for candidate in candidates:
        if not candidate.is_file():
            continue
        value = parse_dotenv(candidate.read_text(encoding="utf-8")).get("BRUTEX_LAKE_ROOT")
        if value is not None:
            values[candidate] = value
    return values


def stage_env(deps: KitDeps, ctx: KitContext) -> StageOutcome:
    """Stage 0 — env-identity (G1/G2/G3/R8): the machine facts every later stage rides on."""
    from brutex import __version__ as brutex_version

    lines: list[str] = []
    status = STATUS_PASS

    sha = deps.git_sha()
    behind = deps.behind_origin()
    growwapi_version = deps.growwapi_version()
    location = deps.package_location()
    editable = "site-packages" not in location
    proxies = dict(deps.getproxies())
    identity = lake_root_identity(ctx.lake_root, ismount=deps.ismount)

    if identity.volume_parent is not None and identity.volume_mounted is False:
        lines.append(
            render_diagnostic(
                "env.lake_unmounted",
                lake_root=identity.path,
                volume=identity.volume_parent,
            )
        )
        status = STATUS_FAIL
    if growwapi_version is None:
        lines.append(render_diagnostic("env.growwapi_missing"))
        status = STATUS_FAIL
    elif not growwapi_version.startswith("1.5."):
        lines.append(render_diagnostic("env.growwapi_drift", installed=growwapi_version))
        status = _worst(status, STATUS_WARN)
    if behind is not None and behind > 0:
        lines.append(render_diagnostic("env.behind_origin", sha=sha, behind=behind))
        status = _worst(status, STATUS_WARN)
    if not editable:
        lines.append(render_diagnostic("env.not_editable", location=location))
        status = _worst(status, STATUS_WARN)
    if proxies:
        lines.append(render_diagnostic("env.proxy", proxies=proxies))
        status = _worst(status, STATUS_WARN)
    dotenv_values = _dotenv_lake_values(deps)
    if len(set(dotenv_values.values())) > 1:
        paths = sorted(dotenv_values)
        lines.append(render_diagnostic("env.dotenv_split", path_a=paths[0], path_b=paths[1]))
        status = _worst(status, STATUS_WARN)

    mount = "no /Volumes parent"
    if identity.volume_parent is not None:
        mount = "volume MOUNTED" if identity.volume_mounted else "volume UNMOUNTED"
    mount = f"{mount}, dev={identity.device if identity.device is not None else '?'}"
    headline = render_diagnostic(
        "env.pass",
        sha=sha,
        behind="?" if behind is None else behind,
        brutex_version=brutex_version,
        growwapi_version=growwapi_version or "MISSING",
        python_version=deps.python_version(),
        location=location,
        editable="editable install" if editable else "site-packages copy",
        lake=identity.path,
        mount=mount,
        proxies=proxies,
    )
    if status == STATUS_PASS:
        lines.insert(0, headline)
    else:
        lines.append("INFO env: " + headline.split("PASS env: ", 1)[1])
    ctx.transcript.event(
        "env",
        sha=sha,
        behind=behind,
        brutex_version=brutex_version,
        growwapi_version=growwapi_version,
        location=location,
        editable=editable,
        proxies=proxies,
        lake_root=str(identity.path),
        volume_parent=str(identity.volume_parent) if identity.volume_parent else None,
        volume_mounted=identity.volume_mounted,
        device=identity.device,
        dotenv_lake_values={str(k): v for k, v in dotenv_values.items()},
    )
    return StageOutcome(stage=0, name="env", status=status, lines=tuple(lines))


def _worst(current: str, incoming: str) -> str:
    """Combine stage statuses (FAIL > WARN > PASS; SKIP never combines)."""
    order = {STATUS_PASS: 0, STATUS_WARN: 1, STATUS_FAIL: 2}
    return incoming if order[incoming] > order[current] else current


# ────────────────────────────────────────────────────────────────────────
# Stage 1 — creds-presence (AWS SSM), WITHOUT the R1 swallow
# ────────────────────────────────────────────────────────────────────────

#: Exception class names → the cause-specific FIX tail (stage 1).
_CREDS_CAUSE_FIXES: Final[tuple[tuple[frozenset[str], str], ...]] = (
    (frozenset({"ImportError", "ModuleNotFoundError", "AwsBackendUnavailable"}), "creds.fix_boto3"),
    (
        frozenset({"NoCredentialsError", "SSOTokenLoadError", "TokenRetrievalError"}),
        "creds.fix_no_aws_creds",
    ),
)


def _creds_fix_tails(exc: CredentialsError, region: str) -> list[str]:
    """Cause-specific FIX tails derived from the exception chain (stage 1)."""
    tails: list[str] = []
    cause = exc.__cause__
    cause_name = type(cause).__name__ if cause is not None else ""
    message = f"{exc} {cause}".lower()
    for names, code in _CREDS_CAUSE_FIXES:
        if cause_name in names:
            tails.append(render_diagnostic(code))
    if cause_name == "AccessDeniedException" or "accessdenied" in message:
        if "kms" in message:
            tails.append(render_diagnostic("creds.fix_kms_denied"))
        else:
            tails.append(render_diagnostic("creds.fix_ssm_denied"))
    if "missing groww credentials" in str(exc).lower():
        tails.append(render_diagnostic("creds.fix_region", region=region))
    return tails


def stage_creds(deps: KitDeps, ctx: KitContext) -> StageOutcome:
    """Stage 1 — the real credential resolver, relayed VERBATIM (register R1).

    Calls :func:`resolve_groww_aws_credentials` (via ``deps.resolve_creds``) —
    the same production loader whose :class:`CredentialsError` the dashboard's
    ``groww_credentials_present`` gate silently swallows into a SYNTHETIC
    dry-run pull. Here the text is relayed verbatim, with the R1 consequence
    note and a cause-specific fix tail.
    """
    region = deps.broker_region()
    started = deps.monotonic()
    try:
        creds = deps.resolve_creds()
    except CredentialsError as exc:
        lines = [
            render_diagnostic("creds.failed", reason=str(exc)),
            render_diagnostic("creds.synthetic_note"),
            *_creds_fix_tails(exc, region),
        ]
        ctx.transcript.event("creds", ok=False, error=str(exc))
        return StageOutcome(stage=1, name="creds", status=STATUS_FAIL, lines=tuple(lines))
    elapsed_ms = round((deps.monotonic() - started) * 1000)
    ctx.creds = creds
    ctx.transcript.event("creds", ok=True, region=region, ms=elapsed_ms)
    return StageOutcome(
        stage=1,
        name="creds",
        status=STATUS_PASS,
        lines=(render_diagnostic("creds.pass", region=region, ms=elapsed_ms),),
    )


# ────────────────────────────────────────────────────────────────────────
# Stage 2 — totp-clock
# ────────────────────────────────────────────────────────────────────────


def base32_problem(secret: str) -> str | None:
    """The exact M3 diagnosis for a non-base32 secret, or ``None`` when valid."""
    normalized = secret.strip()
    for position, char in enumerate(normalized):
        if char.upper() not in _BASE32_ALPHABET:
            return (
                f"binascii: Non-base32 digit found — offending char {char!r} at position {position}"
            )
    padded = normalized.upper() + "=" * (-len(normalized) % 8)
    try:
        base64.b32decode(padded, casefold=True)
    except binascii.Error as exc:
        return f"binascii: {exc}"
    return None


def stage_totp(deps: KitDeps, ctx: KitContext) -> StageOutcome:
    """Stage 2 — base32 validation + a real TOTP mint + host-clock skew (M3/N1)."""
    if ctx.creds is None:
        return _skip(2, "totp", "stage 1 did not run — no credentials to validate")
    problem = base32_problem(ctx.creds.totp_secret)
    if problem is not None:
        ctx.transcript.event("totp", ok=False, problem=problem)
        return StageOutcome(
            stage=2,
            name="totp",
            status=STATUS_FAIL,
            lines=(render_diagnostic("totp.bad_base32", problem=problem),),
        )
    try:
        code = deps.totp_minter(ctx.creds.totp_secret)
    except ModuleNotFoundError:
        ctx.transcript.event("totp", ok=False, problem="pyotp missing")
        return StageOutcome(
            stage=2,
            name="totp",
            status=STATUS_FAIL,
            lines=(render_diagnostic("totp.pyotp_missing"),),
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught — mirrors the raw prod crash
        ctx.transcript.event("totp", ok=False, problem=f"{type(exc).__name__}: {exc}")
        return StageOutcome(
            stage=2,
            name="totp",
            status=STATUS_FAIL,
            lines=(render_diagnostic("totp.mint_failed", error=f"{type(exc).__name__}: {exc}"),),
        )
    _ = code  # the minted 6-digit code is NEVER printed or logged (secret hygiene)
    offset_note: str | None = None
    try:
        offset = deps.clock_offset_s()
    except OSError as exc:
        offset = 0.0
        offset_note = render_diagnostic("totp.offset_unknown", error=str(exc))
    if abs(offset) > CLOCK_SKEW_FAIL_S:
        direction = "AHEAD" if offset > 0 else "BEHIND"
        ctx.transcript.event("totp", ok=False, clock_offset_s=offset)
        return StageOutcome(
            stage=2,
            name="totp",
            status=STATUS_FAIL,
            lines=(
                render_diagnostic(
                    "totp.clock_skew", offset=f"{abs(offset):.1f}", direction=direction
                ),
            ),
        )
    ctx.transcript.event("totp", ok=True, clock_offset_s=offset)
    lines = [
        render_diagnostic(
            "totp.pass",
            length=len(ctx.creds.totp_secret.strip()),
            offset=f"{offset:+.1f}",
        )
    ]
    if offset_note is not None:
        lines.append(offset_note)
    return StageOutcome(stage=2, name="totp", status=STATUS_PASS, lines=tuple(lines))


# ────────────────────────────────────────────────────────────────────────
# Stage 3 — token-read (NO MINT) + authenticated smoke call + SDK shape
# ────────────────────────────────────────────────────────────────────────


def probe_sdk_shape(sdk: object) -> list[str]:
    """Probe the REAL SDK instance against the surfaces this repo relies on (G2).

    Checks (a) the three private ``_request_*`` methods the socket-timeout
    patch monkey-patches (:data:`REQUEST_METHOD_NAMES` — a missing one means
    the patch silently degrades to NO-PATCH), and (b) that
    ``get_historical_candles`` still binds the exact keyword arguments the
    client sends. Exception-class importability is deliberately NOT probed:
    the classifier matches raised class NAMES at call time
    (``errors._SDK_NAME_TO_ERROR``), so importability is not load-bearing —
    the smoke call itself exercises the raise path (Golden Rule #1: no
    invented check).
    """
    problems: list[str] = []
    for name in REQUEST_METHOD_NAMES:
        if not callable(getattr(sdk, name, None)):
            problems.append(f"missing {name}")
    method = getattr(sdk, "get_historical_candles", None)
    if not callable(method):
        problems.append("missing get_historical_candles")
        return problems
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return problems  # C-level callable — signature unknowable, not a drift verdict
    try:
        signature.bind(
            exchange="NSE",
            segment="CASH",
            groww_symbol="NSE-NIFTY",
            start_time="2026-01-01 09:15:00",
            end_time="2026-01-01 15:30:00",
            candle_interval="1minute",
        )
    except TypeError:
        problems.append(f"get_historical_candles signature drifted: {signature}")
    return problems


def _build_kit_client(deps: KitDeps, ctx: KitContext) -> GrowwClientProtocol:
    """Build the REAL live client with the kit's no-mint guard (spec invariant 2)."""
    if ctx.creds is None:  # defensive — callers guard; keeps mypy narrow
        raise KitInternalError("kit client build without credentials")
    creds = GrowwCredentials(api_key=ctx.creds.api_key, totp_secret=ctx.creds.totp_secret)
    controller = deps.controller_factory()
    ctx.controller = controller
    cached: CachedToken | None = None
    if ctx.token is not None:
        cached = CachedToken(
            access_token=ctx.token, issued_for_session=current_session(deps.now_utc())
        )
    mint_allowed = ctx.options.mint_anyway and cached is None

    def _load(*_args: object, **_kwargs: object) -> CachedToken | None:
        return cached

    def _store(*_args: object, **_kwargs: object) -> bool:
        return True  # the kit NEVER persists a token (no shared-state fork)

    return build_live_client(
        creds,
        token_factory=deps.real_token_factory if mint_allowed else mint_blocked_token_factory,
        totp_minter=deps.real_totp_minter if mint_allowed else mint_blocked_totp_minter,
        sdk_factory=deps.sdk_factory,
        pace=controller.pace,
        on_rate_limit=controller.on_rate_limit,
        on_success=controller.on_success,
        token_cache_load=_load,
        token_cache_store=_store,
        # A dead-token reauth RE-READS the (unchanged) SSM snapshot instead of
        # minting — surfacing the honest GrowwTokenStillStaleError (M1), never
        # the kit's own mint-block error masking the vendor rejection. Under
        # --mint-anyway with an ABSENT token the re-read hook must stay None:
        # DEC-REAUTH-COLDSTART-READONLY-001's read-only cold-start guard would
        # otherwise (correctly) refuse the very mint the operator just forced.
        readonly_reread=None if mint_allowed else _load,
    )


def _classify_smoke_failure(exc: GrowwError) -> tuple[str, ...]:
    """The stage-3 diagnostic lines for a failed authenticated smoke call."""
    cause = exc.__cause__
    cause_name = type(cause).__name__ if cause is not None else ""
    message = f"{exc} {cause_name}".lower()
    if (
        "certificate verify failed" in message
        or "certificate_verify_failed" in message
        or "sslerror" in message
    ):
        return (render_diagnostic("token.tls"),)
    bare_code = getattr(cause, "code", None) if cause_name == "GrowwAPIException" else None
    is_auth = isinstance(exc, GrowwAuthError)
    is_bare_auth = bare_code in (401, 403, "401", "403")
    if is_auth or is_bare_auth:
        lines = [render_diagnostic("token.rejected", error=f"{type(exc).__name__}: {exc}")]
        if is_bare_auth:
            lines.append(render_diagnostic("token.bare_auth_evidence", code=bare_code, text=cause))
        return tuple(lines)
    return (render_diagnostic("token.smoke_failed", error=f"{type(exc).__name__}: {exc}"),)


def stage_token(deps: KitDeps, ctx: KitContext) -> StageOutcome:
    """Stage 3 — SSM token read WITH the reason (R7), smoke call (M1/M2/R8), SDK shape (G2)."""
    if ctx.creds is None:
        return _skip(3, "token", "stage 1 did not run — no credentials")
    store = build_readonly_token_store_with(deps.aws_client_factory())
    ctx.token_param = store.token_param
    cached, miss = store.load_with_reason(now_utc=deps.now_utc())
    prelude: list[str] = []
    if miss is not None and miss.kind == "error":
        ctx.transcript.event("token.load", ok=False, kind=miss.kind, error_type=miss.error_type)
        return StageOutcome(
            stage=3,
            name="token",
            status=STATUS_FAIL,
            lines=(
                render_diagnostic(
                    "token.read_error", error_type=miss.error_type, detail=miss.detail
                ),
            ),
        )
    if miss is not None:
        ctx.transcript.event("token.load", ok=False, kind=miss.kind)
        if not ctx.options.mint_anyway:
            return StageOutcome(
                stage=3,
                name="token",
                status=STATUS_FAIL,
                lines=(
                    render_diagnostic("token.absent", param=store.token_param, detail=miss.detail),
                ),
            )
        prelude.append(render_diagnostic("token.mint_anyway"))
    else:
        ctx.token = cached.access_token if cached is not None else None
        ctx.transcript.event("token.load", ok=True, param=store.token_param)
    try:
        client = _build_kit_client(deps, ctx)
    except GrowwError as exc:
        ctx.transcript.event("token.client_build", ok=False, error=str(exc))
        return StageOutcome(
            stage=3,
            name="token",
            status=STATUS_FAIL,
            lines=(
                *prelude,
                render_diagnostic("token.smoke_failed", error=f"{type(exc).__name__}: {exc}"),
            ),
        )
    ctx.client = client
    day = ctx.probe_day if ctx.probe_day is not None else deps.probe_day()
    ctx.probe_day = day
    start, end = _day_window(day)
    ctx.budget.spend(label="token.smoke")
    started = deps.monotonic()
    try:
        payload = client.historical_candles(
            exchange="NSE",
            segment="CASH",
            groww_symbol="NSE-NIFTY",
            interval="1minute",
            start=start,
            end=end,
        )
    except GrowwError as exc:
        ctx.transcript.event(
            "token.smoke",
            ok=False,
            error=str(exc),
            error_type=type(exc).__name__,
            cause_type=type(exc.__cause__).__name__ if exc.__cause__ is not None else None,
        )
        return StageOutcome(
            stage=3,
            name="token",
            status=STATUS_FAIL,
            lines=(*prelude, *_classify_smoke_failure(exc)),
        )
    elapsed_ms = round((deps.monotonic() - started) * 1000)
    rows = _rows_in(payload)
    sdk = getattr(client, "sdk", client)
    problems = probe_sdk_shape(sdk)
    ctx.transcript.event(
        "token.smoke", ok=True, ms=elapsed_ms, rows=rows, sdk_shape_problems=problems
    )
    if problems:
        return StageOutcome(
            stage=3,
            name="token",
            status=STATUS_FAIL,
            lines=(
                *prelude,
                render_diagnostic(
                    "sdk.shape",
                    version=deps.growwapi_version() or "UNKNOWN",
                    problems="; ".join(problems),
                ),
            ),
        )
    methods = sum(1 for name in REQUEST_METHOD_NAMES if callable(getattr(sdk, name, None)))
    return StageOutcome(
        stage=3,
        name="token",
        status=STATUS_WARN if prelude else STATUS_PASS,
        lines=(
            *prelude,
            render_diagnostic(
                "token.pass",
                param=store.token_param,
                ms=elapsed_ms,
                rows=rows,
                methods=methods,
            ),
        ),
    )


# ────────────────────────────────────────────────────────────────────────
# Stage 4 — instrument-master fetch + schema check
# ────────────────────────────────────────────────────────────────────────


def stage_master(deps: KitDeps, ctx: KitContext) -> StageOutcome:
    """Stage 4 — the real keyless CSV download + 21-column schema check (M17/R8)."""
    try:
        index = load_index(deps.http_get)
    except InstrumentFormatError as exc:
        ctx.transcript.event("master", ok=False, error=str(exc))
        return StageOutcome(
            stage=4,
            name="master",
            status=STATUS_FAIL,
            lines=(render_diagnostic("master.header_drift", error=str(exc)),),
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught — network shim surface is unbounded
        ctx.transcript.event("master", ok=False, error=f"{type(exc).__name__}: {exc}")
        return StageOutcome(
            stage=4,
            name="master",
            status=STATUS_FAIL,
            lines=(render_diagnostic("master.unreachable", error=f"{type(exc).__name__}: {exc}"),),
        )
    ctx.master = index
    expected = (*sorted(BRUTE_FORCE_INDEX_SYMBOLS), INDIA_VIX_SYMBOL)
    missing = sorted(symbol for symbol in expected if symbol not in index.by_groww_symbol)
    eq_count = sum(1 for instrument in index.universe if instrument.instrument == "EQ")
    ctx.transcript.event(
        "master", ok=not missing, universe=len(index.universe), eq=eq_count, missing=missing
    )
    if missing:
        return StageOutcome(
            stage=4,
            name="master",
            status=STATUS_FAIL,
            lines=(
                render_diagnostic(
                    "master.universe_gap", universe=len(index.universe), missing=missing
                ),
            ),
        )
    return StageOutcome(
        stage=4,
        name="master",
        status=STATUS_PASS,
        lines=(render_diagnostic("master.pass", universe=len(index.universe), eq=eq_count),),
    )


# ────────────────────────────────────────────────────────────────────────
# Stage 5 — spot 1-minute probe through the REAL pipeline
# ────────────────────────────────────────────────────────────────────────


def stage_spot1m(deps: KitDeps, ctx: KitContext) -> StageOutcome:
    """Stage 5 — one real 1m window through client → parser → session filter (M10-M15, R2/R3)."""
    if ctx.client is None:
        return _skip(5, "spot-1m", "stage 3 did not run — no live client")
    day = ctx.probe_day if ctx.probe_day is not None else deps.probe_day()
    ctx.probe_day = day
    exchange, segment = ctx.exchange_segment()
    start, end = _day_window(day)
    ctx.budget.spend(label="spot1m.fetch")
    try:
        # The Protocol types the return as a Mapping, but the LIVE SDK can return
        # None / a raw string / a bare list (register M10) — the kit re-types it
        # as ``object`` so the runtime envelope check below is honest, not "dead".
        payload: object = ctx.client.historical_candles(
            exchange=exchange,
            segment=segment,
            groww_symbol=ctx.options.symbol,
            interval="1minute",
            start=start,
            end=end,
        )
    except GrowwError as exc:
        ctx.transcript.event("spot1m", ok=False, error=str(exc))
        return StageOutcome(
            stage=5,
            name="spot-1m",
            status=STATUS_FAIL,
            lines=(render_diagnostic("spot1m.vendor_error", error=f"{type(exc).__name__}: {exc}"),),
        )
    if not isinstance(payload, Mapping):
        ctx.transcript.event(
            "spot1m", ok=False, payload_type=type(payload).__name__, sample=str(payload)[:500]
        )
        return StageOutcome(
            stage=5,
            name="spot-1m",
            status=STATUS_FAIL,
            lines=(render_diagnostic("spot1m.non_mapping", type_name=type(payload).__name__),),
        )
    ctx.payload = payload
    raw_rows = _rows_in(payload)
    encoding = "unknown"
    candles = payload.get("candles")
    if (
        isinstance(candles, (list, tuple))
        and candles
        and isinstance(candles[0], (list, tuple))
        and candles[0]
    ):
        encoding = "string" if isinstance(candles[0][0], str) else "epoch"
    try:
        ctx.record = build_record(
            payload,
            exchange=exchange,
            segment=segment,
            groww_symbol=ctx.options.symbol,
            interval="1minute",
            window_start=start,
            window_end=end,
            now=deps.now_utc(),
        )
    except RawIntegrityError as exc:
        ctx.transcript.event("spot1m.capture_failed", error=str(exc))
    try:
        bars = parse_candle_rows(payload, drop_volume=ctx.symbol_is_index())
    except CandleParseError as exc:
        text = str(exc)
        ctx.transcript.event(
            "spot1m", ok=False, parse_error=text, keys=sorted(str(key) for key in payload)
        )
        if "missing required 'candles' key" in text:
            keys = sorted(str(key) for key in payload)
            return StageOutcome(
                stage=5,
                name="spot-1m",
                status=STATUS_FAIL,
                lines=(render_diagnostic("spot1m.no_candles_key", keys=keys),),
            )
        if "unparseable Groww timestamp" in text:
            return StageOutcome(
                stage=5,
                name="spot-1m",
                status=STATUS_FAIL,
                lines=(render_diagnostic("spot1m.unparseable_ts", error=text),),
            )
        return StageOutcome(
            stage=5,
            name="spot-1m",
            status=STATUS_FAIL,
            lines=(render_diagnostic("spot1m.parse_failed", error=text),),
        )
    if raw_rows == 0:
        ctx.transcript.event("spot1m", ok=False, raw_rows=0, day=day.isoformat())
        return StageOutcome(
            stage=5,
            name="spot-1m",
            status=STATUS_FAIL,
            lines=(render_diagnostic("spot1m.clean_empty", day=day.isoformat()),),
        )
    skipped = raw_rows - len(bars)
    if not bars:
        ctx.transcript.event("spot1m", ok=False, raw_rows=raw_rows, all_skipped=True)
        return StageOutcome(
            stage=5,
            name="spot-1m",
            status=STATUS_FAIL,
            lines=(
                render_diagnostic(
                    "spot1m.parse_failed",
                    error=(
                        f"all {raw_rows} rows were skipped as absent/zero OHLC "
                        f"(the M15 epidemic at 100%)"
                    ),
                ),
            ),
        )
    session_bars = filter_intraday_session(bars)
    ctx.bars = bars
    ctx.session_bars = session_bars
    dropped = len(bars) - len(session_bars)
    first_ist = bars[0].timestamp.astimezone(IST).strftime("%H:%M")
    drop_ratio = dropped / len(bars)
    first_session_ok = bool(session_bars) and (
        session_bars[0].timestamp.astimezone(IST).time() == time(9, 15)
    )
    ctx.transcript.event(
        "spot1m",
        ok=True,
        raw_rows=raw_rows,
        parsed=len(bars),
        session=len(session_bars),
        skipped_empty_ohlc=skipped,
        dropped_out_of_session=dropped,
        first_ist=first_ist,
        encoding=encoding,
        day=day.isoformat(),
    )
    if drop_ratio > SESSION_DROP_FAIL_RATIO:
        return StageOutcome(
            stage=5,
            name="spot-1m",
            status=STATUS_FAIL,
            lines=(
                render_diagnostic(
                    "spot1m.session_shift",
                    first_ist=first_ist,
                    dropped=dropped,
                    total=len(bars),
                ),
            ),
        )
    if not first_session_ok:
        # Distinct from the UTC-shift diagnosis (hostile-review M-1): a LOW
        # drop ratio with a late first session bar means the OPENING bars are
        # missing, not that the whole day is systematically mis-stamped — a
        # +05:30 shift would push far more of the day out of the session.
        # (session_bars is non-empty here: an empty session list makes
        # drop_ratio == 1.0, which takes the session_shift arm above.)
        first_session_ist = session_bars[0].timestamp.astimezone(IST).strftime("%H:%M")
        return StageOutcome(
            stage=5,
            name="spot-1m",
            status=STATUS_FAIL,
            lines=(
                render_diagnostic(
                    "spot1m.first_bar_missing",
                    first_ist=first_session_ist,
                    dropped=dropped,
                    total=len(bars),
                ),
            ),
        )
    lines = [
        render_diagnostic(
            "spot1m.pass",
            raw=raw_rows,
            session=len(session_bars),
            day=day.isoformat(),
            first_ist=first_ist,
            encoding=encoding,
            skipped=skipped,
        )
    ]
    status = STATUS_PASS
    if skipped / raw_rows > OHLC_SKIP_WARN_RATIO:
        lines.append(render_diagnostic("spot1m.null_ohlc", percent=round(100 * skipped / raw_rows)))
        status = STATUS_WARN
    return StageOutcome(stage=5, name="spot-1m", status=status, lines=tuple(lines))


# ────────────────────────────────────────────────────────────────────────
# Stage 6 — 2020-depth probe per index
# ────────────────────────────────────────────────────────────────────────


def stage_depth(deps: KitDeps, ctx: KitContext) -> StageOutcome:
    """Stage 6 — the resurrected depth probe per brute-force index (R6; ADR-0009 §3.1).

    ZERO-COMMAND law (DEC-DOCTOR-KIT-HONESTY-001): with ``options.auto_bisect``
    (the autorun's mode) an EMPTY boundary window automatically runs the SAME
    bounded bisect ``--depth-bisect`` would — :func:`probe_depth` spends the
    extra calls ONLY on a failing symbol (a boundary hit returns after exactly
    1 call, so a PASSing symbol is never bisected) — and a FOUND floor renders
    as an advisory WARN naming the floor instead of a FAIL instructing the
    operator to type a command. Manual ``--depth-bisect`` is byte-unchanged.
    """
    if ctx.client is None:
        return _skip(6, "depth", "stage 3 did not run — no live client")
    today = deps.now_utc().date()
    from brutex.adapters.groww.probe import DEPTH_PROBE_MAX_CALLS

    bisect_enabled = ctx.options.depth_bisect or ctx.options.auto_bisect
    advisory = ctx.options.auto_bisect and not ctx.options.depth_bisect
    lines: list[str] = []
    details: list[str] = []
    status = STATUS_PASS
    for symbol in sorted(BRUTE_FORCE_INDEX_SYMBOLS):
        per_symbol_cap = DEPTH_PROBE_MAX_CALLS if bisect_enabled else 1
        allowance = min(per_symbol_cap, ctx.budget.remaining)
        if allowance < 1:
            ctx.budget.spend(label=f"depth.{symbol}")  # raises CallBudgetExceeded
        result = probe_depth(
            ctx.client,
            symbol=symbol,
            exchange=symbol.split("-", 1)[0],
            today=today,
            bisect=bisect_enabled,
            max_calls=allowance,
        )
        ctx.budget.spend(result.calls_used, label=f"depth.{symbol}")
        ctx.transcript.event(
            "depth",
            symbol=symbol,
            verdict=result.verdict,
            first_ts=result.first_ts,
            probed_floor=result.probed_floor.isoformat() if result.probed_floor else None,
            boundary_has_data=result.boundary_has_data,
            calls_used=result.calls_used,
            auto_bisect=ctx.options.auto_bisect,
        )
        if result.verdict == VERDICT_OK and result.boundary_has_data:
            details.append(f"{symbol} 1m starts at the boundary (first_ts={result.first_ts})")
        elif result.verdict == VERDICT_OK:
            floor_text = result.probed_floor.isoformat() if result.probed_floor is not None else "?"
            if advisory:
                # The autorun already located the floor itself — an advisory
                # WARN naming it, never a FAIL telling the operator to type a
                # command (the zero-command law).
                status = _worst(status, STATUS_WARN)
                lines.append(
                    render_diagnostic(
                        "depth.autobisect_floor",
                        symbol=symbol,
                        floor=floor_text,
                        expected=EARLIEST_HISTORY.isoformat(),
                        calls=result.calls_used,
                    )
                )
            else:
                status = STATUS_FAIL
                lines.append(
                    render_diagnostic(
                        "depth.shallow",
                        symbol=symbol,
                        floor=floor_text,
                        expected=EARLIEST_HISTORY.isoformat(),
                    )
                )
        elif result.verdict == VERDICT_ABSENT:
            status = STATUS_FAIL
            lines.append(
                render_diagnostic(
                    # Under auto_bisect the bounded bisect ALREADY ran — the
                    # no-data-anywhere verdict must not prescribe a manual
                    # --depth-bisect re-run (the zero-command law).
                    "depth.autobisect_absent" if advisory else "depth.unknown",
                    symbol=symbol,
                    expected=EARLIEST_HISTORY.isoformat(),
                    detail=result.detail,
                )
            )
        else:
            status = STATUS_FAIL
            lines.append(render_diagnostic("depth.error", detail=result.detail))
    if status == STATUS_PASS:
        lines = [
            render_diagnostic(
                "depth.pass",
                details=" · ".join(details),
                floor=EARLIEST_HISTORY.isoformat(),
            )
        ]
    return StageOutcome(stage=6, name="depth", status=status, lines=tuple(lines))


# ────────────────────────────────────────────────────────────────────────
# Stage 7 — VIX probe (ADR-0009 HARD-GATE)
# ────────────────────────────────────────────────────────────────────────


def stage_vix(deps: KitDeps, ctx: KitContext) -> StageOutcome:
    """Stage 7 — the resurrected VIX probe (R5; the migration-rescinding hard gate)."""
    if ctx.client is None:
        return _skip(7, "vix", "stage 3 did not run — no live client")
    client = ctx.client
    today = deps.now_utc().date()

    def _daily_rows(symbol: str, label: str) -> tuple[int, str]:
        """ONE 1day fetch → ``(rows, text)``; a raise degrades to ``(0, "ERROR(...)")``."""
        ctx.budget.spend(label=label)
        try:
            daily_payload = client.historical_candles(
                exchange="NSE",
                segment="CASH",
                groww_symbol=symbol,
                interval="1day",
                start=f"{(today - timedelta(days=30)).isoformat()} 00:00:00",
                end=f"{today.isoformat()} 15:30:00",
            )
        except GrowwError as exc:
            ctx.transcript.event("vix.daily", ok=False, error=str(exc))
            return 0, f"ERROR({type(exc).__name__})"
        rows = _rows_in(daily_payload)
        ctx.transcript.event("vix.daily", ok=True, rows=str(rows))
        return rows, str(rows)

    ctx.budget.spend(3, label="vix.probe")  # probe_vix tries ≤ 3 candidate symbols
    result = probe_vix(client)
    ctx.transcript.event("vix", verdict=result.verdict, detail=result.detail, symbol=result.symbol)
    if result.verdict == VERDICT_ABSENT:
        # Hostile-review m-2: before concluding the MIGRATION-RESCINDING gate,
        # spend ONE more call on the 1day interval — "1m absent / daily
        # present" is a DIFFERENT honest state from "no candles at all". This
        # is +1 call on the absent path only; the stage's worst case stays 4
        # (the PASS path already spends 3 probe + 1 daily).
        daily_rows, daily_text = _daily_rows(INDIA_VIX_SYMBOL, "vix.daily_fallback")
        if daily_rows:
            return StageOutcome(
                stage=7,
                name="vix",
                status=STATUS_FAIL,
                lines=(render_diagnostic("vix.absent_1m", daily=daily_rows),),
            )
        fallback_note = (
            f"1day fallback {daily_text}"
            if daily_text.startswith("ERROR")
            else "1day fallback also empty"
        )
        return StageOutcome(
            stage=7,
            name="vix",
            status=STATUS_FAIL,
            lines=(
                render_diagnostic(
                    "vix.absent",
                    shape=f"clean-empty 200 on every candidate symbol (1minute); {fallback_note}",
                ),
            ),
        )
    if result.verdict != VERDICT_OK:
        return StageOutcome(
            stage=7,
            name="vix",
            status=STATUS_FAIL,
            lines=(render_diagnostic("vix.absent", shape=f"typed error — {result.detail}"),),
        )
    _, daily_text = _daily_rows(result.symbol or INDIA_VIX_SYMBOL, "vix.daily")
    return StageOutcome(
        stage=7,
        name="vix",
        status=STATUS_PASS,
        lines=(render_diagnostic("vix.pass", daily=daily_text, m1=result.rows or 0),),
    )


# ────────────────────────────────────────────────────────────────────────
# Stage 8 — 1-day derivation + NSE bhavcopy cross-check
# ────────────────────────────────────────────────────────────────────────


def stage_bhavcopy(deps: KitDeps, ctx: KitContext) -> StageOutcome:
    """Stage 8 — derive the day's 1day bar (production resampler) vs the NSE snapshot (G7)."""
    if not ctx.session_bars or ctx.probe_day is None:
        return _skip(8, "bhavcopy", "stage 5 did not run — no session bars to derive from")
    symbol = ctx.options.symbol
    index_name = nse_index_name_for(symbol)
    if index_name is None:
        return StageOutcome(
            stage=8,
            name="bhavcopy",
            status=STATUS_WARN,
            lines=(render_diagnostic("bhavcopy.unmapped", symbol=symbol),),
        )
    # Local imports keep polars out of the module import graph (lazy-dep rule).
    import polars as pl

    from brutex.data.spot_daily_resample import resample_1m_to_daily

    frame = pl.DataFrame(
        {
            "ts_utc": [bar.timestamp for bar in ctx.session_bars],
            "open": [float(bar.open) for bar in ctx.session_bars],
            "high": [float(bar.high) for bar in ctx.session_bars],
            "low": [float(bar.low) for bar in ctx.session_bars],
            "close": [float(bar.close) for bar in ctx.session_bars],
            "volume": [bar.volume for bar in ctx.session_bars],
        },
        schema={
            "ts_utc": pl.Datetime(time_unit="us", time_zone="UTC"),
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
        },
    ).sort("ts_utc")
    daily = resample_1m_to_daily(frame, before=ctx.probe_day + timedelta(days=1))
    derived = next(
        (bar for bar in daily if bar.timestamp.astimezone(IST).date() == ctx.probe_day), None
    )
    if derived is None:
        return StageOutcome(
            stage=8,
            name="bhavcopy",
            status=STATUS_WARN,
            lines=(render_diagnostic("bhavcopy.no_daily_bar", day=ctx.probe_day.isoformat()),),
        )
    try:
        official_rows = fetch_index_bhavcopy(deps.nse_http_get, ctx.probe_day)
    except GrowwInstrumentFetchError as exc:
        # KIT WIRING BUG (DEC-DOCTOR-KIT-HONESTY-001): the injected getter's OWN
        # Groww allowlist refused the NSE URL — a mis-wired nse_http_get, never
        # an NSE outage. Rendered as an internal kit bug, NOT "external
        # dependency" (the mislabel the operator's red-banner report caught).
        ctx.transcript.event("bhavcopy", ok=False, wiring_error=str(exc))
        return StageOutcome(
            stage=8,
            name="bhavcopy",
            status=STATUS_FAIL,
            lines=(render_diagnostic("bhavcopy.wiring", error=str(exc)),),
        )
    except BhavcopyError as exc:
        if "non-allowlisted" in str(exc):
            # The same wiring-bug class raised by an allowlist copy inside the
            # getter itself — still OUR configuration, never an NSE fault.
            ctx.transcript.event("bhavcopy", ok=False, wiring_error=str(exc))
            return StageOutcome(
                stage=8,
                name="bhavcopy",
                status=STATUS_FAIL,
                lines=(render_diagnostic("bhavcopy.wiring", error=str(exc)),),
            )
        ctx.transcript.event("bhavcopy", ok=False, error=str(exc))
        return StageOutcome(
            stage=8,
            name="bhavcopy",
            status=STATUS_WARN,
            lines=(render_diagnostic("bhavcopy.unreachable", error=str(exc)),),
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught — network shim surface is unbounded
        ctx.transcript.event("bhavcopy", ok=False, error=f"{type(exc).__name__}: {exc}")
        return StageOutcome(
            stage=8,
            name="bhavcopy",
            status=STATUS_WARN,
            lines=(
                render_diagnostic("bhavcopy.unreachable", error=f"{type(exc).__name__}: {exc}"),
            ),
        )
    official = official_rows.get(index_name)
    if official is None:
        ctx.transcript.event(
            "bhavcopy", ok=False, missing_row=index_name, names=sorted(official_rows)
        )
        return StageOutcome(
            stage=8,
            name="bhavcopy",
            status=STATUS_WARN,
            lines=(
                render_diagnostic(
                    "bhavcopy.no_row", day=ctx.probe_day.isoformat(), index_name=index_name
                ),
            ),
        )
    comparison = compare_daily_bar(derived, official)
    ctx.transcript.event(
        "bhavcopy",
        ok=comparison.matches,
        day=ctx.probe_day.isoformat(),
        index_name=official.index_name,
        max_delta=str(comparison.max_delta),
        deltas={name: str(delta) for name, delta in comparison.deltas.items()},
    )
    if not comparison.matches:
        drifted = ", ".join(
            f"{name} {getattr(derived, name)} vs {getattr(official, name)}"
            for name, delta in sorted(comparison.deltas.items())
            if delta > comparison.tolerance
        )
        return StageOutcome(
            stage=8,
            name="bhavcopy",
            status=STATUS_FAIL,
            lines=(
                render_diagnostic(
                    "bhavcopy.mismatch",
                    fields=drifted,
                    day=ctx.probe_day.isoformat(),
                    delta=comparison.max_delta,
                    tolerance=comparison.tolerance,
                ),
            ),
        )
    return StageOutcome(
        stage=8,
        name="bhavcopy",
        status=STATUS_PASS,
        lines=(
            render_diagnostic(
                "bhavcopy.pass",
                index_name=official.index_name,
                day=ctx.probe_day.isoformat(),
                delta=comparison.max_delta,
                tolerance=comparison.tolerance,
            ),
        ),
    )


# ────────────────────────────────────────────────────────────────────────
# Stage 9 — resample determinism (reuses doctor check 4 verbatim)
# ────────────────────────────────────────────────────────────────────────


def stage_resample(deps: KitDeps, ctx: KitContext) -> StageOutcome:
    """Stage 9 — the EXISTING derived-TF freshness check over the chosen tree."""
    _ = deps
    from brutex.doctor import check_derived_freshness

    target = ctx.lake_root if ctx.options.against_lake else ctx.quarantine_root
    result = check_derived_freshness(target)
    ctx.transcript.event("resample", status=result.status, detail=result.detail, target=str(target))
    if result.status == "FAIL":
        return StageOutcome(
            stage=9,
            name="resample",
            status=STATUS_FAIL,
            lines=(render_diagnostic("resample.drift", detail=result.detail),),
        )
    if result.status == "SKIP":
        return _skip(9, "resample", result.detail)
    return StageOutcome(
        stage=9,
        name="resample",
        status=STATUS_PASS,
        lines=(f"PASS resample: {result.detail}",),
    )


# ────────────────────────────────────────────────────────────────────────
# Stage 10 — lake write + read-back + ledger row (quarantined subtree)
# ────────────────────────────────────────────────────────────────────────


def stage_lake(deps: KitDeps, ctx: KitContext) -> StageOutcome:
    """Stage 10 — real writer/ledger/audit into ``_liveproof/<run_id>/`` ONLY (G1)."""
    if not ctx.session_bars or ctx.probe_day is None:
        return _skip(10, "lake", "stage 5 did not run — no bars to write")
    env_file = deps.home / ".brutex" / ".env"
    if env_file.is_file():
        env_value = parse_dotenv(env_file.read_text(encoding="utf-8")).get("BRUTEX_LAKE_ROOT")
        if env_value and Path(env_value).expanduser() != ctx.lake_root:
            return StageOutcome(
                stage=10,
                name="lake",
                status=STATUS_FAIL,
                lines=(
                    render_diagnostic(
                        "lake.split_brain",
                        resolved=ctx.lake_root,
                        env_file=env_file,
                        env_value=env_value,
                    ),
                ),
            )
    from brutex.data.fetch_coverage import WindowKey
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.window_ledger import WindowLedger
    from brutex.data.window_ledger_store import load_window_ledger, save_window_ledger
    from brutex.orchestrator.audit import AuditWriter, verify_chain

    exchange, _segment = ctx.exchange_segment()
    trading_symbol = ctx.options.symbol.split("-", 1)[1]
    writer = ParquetWriter(lake_root=ctx.quarantine_root)
    try:
        first = writer.write(
            exchange=exchange,
            segment="INDEX",
            symbol=trading_symbol,
            interval="1minute",
            bars=list(ctx.session_bars),
        )
        second = writer.write(
            exchange=exchange,
            segment="INDEX",
            symbol=trading_symbol,
            interval="1minute",
            bars=list(ctx.session_bars),
        )
    except OSError as exc:
        ctx.transcript.event("lake", ok=False, error=str(exc))
        return StageOutcome(
            stage=10,
            name="lake",
            status=STATUS_FAIL,
            lines=(render_diagnostic("lake.write_failed", error=str(exc)),),
        )
    ctx.transcript.event(
        "lake.write",
        path=str(first.path),
        rows=first.row_count,
        first_hash=first.raw_hash,
        rewrite_hash=second.raw_hash,
        rewrite_added=second.bars_added,
        rewrite_conflicts=len(second.conflicts),
    )
    if second.bars_added != 0 or second.conflicts or first.raw_hash != second.raw_hash:
        detail = (
            f"idempotent re-write added {second.bars_added} bars, "
            f"{len(second.conflicts)} conflicts, hash {first.raw_hash[:12]}… → "
            f"{second.raw_hash[:12]}…"
        )
        return StageOutcome(
            stage=10,
            name="lake",
            status=STATUS_FAIL,
            lines=(render_diagnostic("lake.readback_drift", detail=detail),),
        )
    now = deps.now_utc()
    ledger = WindowLedger()
    key = WindowKey(
        market="groww",
        security_id=ctx.options.symbol,
        interval="1minute",
        start=ctx.probe_day,
        end=ctx.probe_day + timedelta(days=1),
    )
    ledger.plan(key, now=now)
    ledger.record(
        key,
        raw_bars=len(ctx.bars),
        error_type=None,
        bars_appended=first.bars_added,
        now=now,
    )
    save_window_ledger(ledger, ctx.quarantine_root)
    loaded_records = load_window_ledger(ctx.quarantine_root).records()
    ledger_status = loaded_records[0].status if loaded_records else "MISSING"
    if ledger_status != "SUCCESS":
        ctx.transcript.event("lake.ledger", ok=False, status=ledger_status)
        return StageOutcome(
            stage=10,
            name="lake",
            status=STATUS_FAIL,
            lines=(render_diagnostic("lake.ledger_state", status=ledger_status),),
        )
    audit_path = ctx.quarantine_root / "audit" / "liveproof.jsonl"
    with AuditWriter.open(audit_path, actor="doctor-live", resume=audit_path.exists()) as audit:
        audit.append(
            action="LIVEPROOF_LAKE_WRITE",
            subject=f"{ctx.options.symbol}:1minute:{ctx.probe_day.isoformat()}",
            payload={"bars": len(ctx.session_bars), "raw_hash": first.raw_hash},
        )
    verify_chain(audit_path)
    ctx.transcript.event("lake.ledger", ok=True, status=ledger_status)
    return StageOutcome(
        stage=10,
        name="lake",
        status=STATUS_PASS,
        lines=(
            render_diagnostic(
                "lake.pass",
                bars=len(ctx.session_bars),
                path=first.path,
                status=ledger_status,
            ),
        ),
    )


# ────────────────────────────────────────────────────────────────────────
# Stage 11 — integrity re-fetch verify
# ────────────────────────────────────────────────────────────────────────


def stage_integrity(deps: KitDeps, ctx: KitContext) -> StageOutcome:
    """Stage 11 — wire the existing-but-unwired re-fetch verifier (R2)."""
    _ = deps
    if ctx.client is None:
        return _skip(11, "integrity", "stage 3 did not run — no live client")
    from brutex.adapters.groww.integrity_verify import verify_lake_records, verify_one

    if ctx.options.against_lake:
        from brutex.data.raw_integrity_store import load_records

        total = len(load_records(ctx.lake_root))
        if total == 0:
            return _skip(11, "integrity", "production lake has no integrity sidecar records")
        if total > ctx.budget.remaining:
            return _skip(
                11,
                "integrity",
                f"{total} recorded windows exceed the remaining call budget "
                f"({ctx.budget.remaining}) — run with --stage 11 alone for a full verify",
            )
        ctx.budget.spend(total, label="integrity.lake")
        run = verify_lake_records(ctx.client, ctx.lake_root)
        ctx.transcript.event(
            "integrity.lake",
            total=run.total,
            matched=run.matched,
            mismatched=[list(result.key) for result in run.mismatched],
        )
        if run.all_matched:
            return StageOutcome(
                stage=11,
                name="integrity",
                status=STATUS_PASS,
                lines=(
                    render_diagnostic("integrity.lake_pass", matched=run.matched, total=run.total),
                ),
            )
        return StageOutcome(
            stage=11,
            name="integrity",
            status=STATUS_FAIL,
            lines=(
                render_diagnostic(
                    "integrity.lake_mismatch",
                    mismatched=len(run.mismatched),
                    total=run.total,
                ),
            ),
        )
    if ctx.record is None:
        return _skip(11, "integrity", "stage 5 did not run — no captured window to re-verify")
    ctx.budget.spend(label="integrity.refetch")
    try:
        result = verify_one(ctx.client, ctx.record)
    except (GrowwError, RawIntegrityError) as exc:
        ctx.transcript.event("integrity", ok=False, error=str(exc))
        return StageOutcome(
            stage=11,
            name="integrity",
            status=STATUS_FAIL,
            lines=(
                render_diagnostic("integrity.refetch_failed", error=f"{type(exc).__name__}: {exc}"),
            ),
        )
    ctx.transcript.event(
        "integrity",
        ok=result.matches,
        recorded_hash=result.recorded_hash,
        actual_hash=result.actual_hash,
    )
    if result.matches:
        return StageOutcome(
            stage=11,
            name="integrity",
            status=STATUS_PASS,
            lines=(render_diagnostic("integrity.pass"),),
        )
    return StageOutcome(
        stage=11,
        name="integrity",
        status=STATUS_FAIL,
        lines=(render_diagnostic("integrity.mismatch"),),
    )


# ────────────────────────────────────────────────────────────────────────
# Stage 12 — resume / interrupted-pull / sleep check
# ────────────────────────────────────────────────────────────────────────


def stage_resume(deps: KitDeps, ctx: KitContext) -> StageOutcome:
    """Stage 12 — outstanding windows + journal start-without-finish + sleep gaps (G5/G6)."""
    _ = deps
    from brutex.data.window_ledger_store import load_window_ledger

    progress = load_window_ledger(ctx.lake_root).progress_counts()
    outstanding = progress.planned + progress.failed
    run = last_run(read_journal(ctx.lake_root))
    lines: list[str] = []
    status = STATUS_PASS
    if run is not None and run.interrupted:
        lines.append(
            render_diagnostic(
                "resume.interrupted", started=run.started.wall_utc, outstanding=outstanding
            )
        )
        status = STATUS_WARN
    gaps = sleep_gaps(run) if run is not None else []
    for gap in gaps:
        lines.append(
            render_diagnostic(
                "resume.slept",
                minutes=round(gap.gap_seconds / 60),
                window=f"{gap.after_wall_utc} → {gap.before_wall_utc}",
            )
        )
        status = STATUS_WARN
    ctx.transcript.event(
        "resume",
        outstanding=outstanding,
        interrupted=bool(run is not None and run.interrupted),
        sleep_gaps=len(gaps),
    )
    if not lines:
        lines = [render_diagnostic("resume.pass", outstanding=outstanding)]
    return StageOutcome(stage=12, name="resume", status=status, lines=tuple(lines))


# ────────────────────────────────────────────────────────────────────────
# Stage 13 — rate-ladder probe (opt-in --rate-probe)
# ────────────────────────────────────────────────────────────────────────


def stage_rate(deps: KitDeps, ctx: KitContext) -> StageOutcome:
    """Stage 13 — ≤ 60 s of cheap calls via the REAL controller+client, abort on burst.

    Honest bound: a *raised* :class:`GrowwError` here means the client's OWN
    bounded in-call retry was already exhausted — so the loop's logical-call
    count under-counts physical attempts by at most one call's retry budget
    (register M8's evidence-starvation point, stated on the PASS line too).
    """
    if not ctx.options.rate_probe:
        return _skip(13, "rate", "opt-in probe not enabled (pass --rate-probe to run it)")
    if ctx.client is None or ctx.probe_day is None:
        return _skip(13, "rate", "stage 3 did not run — no live client")
    exchange, segment = ctx.exchange_segment()
    start, end = _day_window(ctx.probe_day)
    started = deps.monotonic()
    calls = 0
    clean = 0
    consecutive = 0
    throttles: list[str] = []
    aborted = False
    while (
        deps.monotonic() - started
    ) < ctx.options.rate_probe_seconds and calls < RATE_PROBE_BUDGET:
        ctx.budget.spend(label="rate.probe")
        calls += 1
        try:
            ctx.client.historical_candles(
                exchange=exchange,
                segment=segment,
                groww_symbol=ctx.options.symbol,
                interval="1minute",
                start=start,
                end=end,
            )
        except GrowwError as exc:
            shape = f'{type(exc).__name__}("{exc}")'
            throttles.append(shape)
            consecutive += 1
            ctx.transcript.event("rate.throttle", call=calls, shape=shape)
            if consecutive >= RATE_PROBE_ABORT_BURST:
                aborted = True
                break
        else:
            clean += 1
            consecutive = 0
    elapsed = max(deps.monotonic() - started, 1e-9)
    rate = clean / elapsed
    ctx.transcript.event(
        "rate.summary",
        calls=calls,
        clean=clean,
        throttles=len(throttles),
        elapsed_s=elapsed,
        aborted=aborted,
    )
    if throttles:
        lines = [
            render_diagnostic("rate.throttle_evidence", rate=f"{rate:.1f}", shape=throttles[0])
        ]
        if aborted:
            lines.append(render_diagnostic("rate.aborted", bursts=consecutive))
        return StageOutcome(stage=13, name="rate", status=STATUS_WARN, lines=tuple(lines))
    return StageOutcome(
        stage=13,
        name="rate",
        status=STATUS_PASS,
        lines=(
            render_diagnostic(
                "rate.pass", rate=f"{rate:.1f}", seconds=f"{elapsed:.0f}", calls=calls
            ),
        ),
    )


# ────────────────────────────────────────────────────────────────────────
# Registry + runner
# ────────────────────────────────────────────────────────────────────────

StageFn = Callable[[KitDeps, KitContext], StageOutcome]

#: The 14 stages, in run order (kit spec §4).
STAGES: Final[tuple[tuple[int, str, StageFn], ...]] = (
    (0, "env", stage_env),
    (1, "creds", stage_creds),
    (2, "totp", stage_totp),
    (3, "token", stage_token),
    (4, "master", stage_master),
    (5, "spot-1m", stage_spot1m),
    (6, "depth", stage_depth),
    (7, "vix", stage_vix),
    (8, "bhavcopy", stage_bhavcopy),
    (9, "resample", stage_resample),
    (10, "lake", stage_lake),
    (11, "integrity", stage_integrity),
    (12, "resume", stage_resume),
    (13, "rate", stage_rate),
)


def compute_call_budget(options: KitOptions) -> int:
    """The run's hard vendor-call cap (spec §2.3): 40 + 60 (bisect) + 480 (rate).

    ``auto_bisect`` grants the SAME :data:`DEPTH_BISECT_BUDGET` extension as
    the manual ``--depth-bisect`` — the budget must honestly cover the bisect
    the run MAY perform (a full bisect is ~14 calls per failing symbol,
    capped at :data:`~brutex.adapters.groww.probe.DEPTH_PROBE_MAX_CALLS`
    = 20); the two flags together never double-grant it. Since the operator
    decree of 2026-07-03 (DEC-DOCTOR-KIT-HONESTY-001 amendment) removed the
    depth stage from the AUTORUN stage sets, autorun runs never carry either
    flag (plain 40-call budget); the extension now serves the manual CLI path
    and any explicit stage-6 selection.
    """
    limit = DEFAULT_CALL_BUDGET
    if options.depth_bisect or options.auto_bisect:
        limit += DEPTH_BISECT_BUDGET
    if options.rate_probe:
        limit += RATE_PROBE_BUDGET
    return limit


def run_live_doctor(
    deps: KitDeps,
    options: KitOptions,
    *,
    transcript_sink: Callable[[str], None] | None = None,
) -> KitReport:
    """Run the staged live proof and return the report (the CLI prints it).

    Fail-fast at the first FAIL unless ``options.keep_going``; a
    :class:`CallBudgetExceeded` inside a stage becomes that stage's FAIL (the
    SAFE bound); a kit-side ``OSError`` (transcript/quarantine I/O) raises
    :class:`KitInternalError` — exit code 3, never a fake stage verdict.
    """
    lake_root = (
        options.lake_root
        if options.lake_root is not None
        else resolve_lake_root(deps.env, home=deps.home)
    )
    run_id = "lp-" + deps.now_utc().strftime("%Y%m%d-%H%M%S")
    quarantine_root = lake_root / "_liveproof" / run_id
    transcript_path = quarantine_root / "transcript.jsonl"
    sink = (
        transcript_sink
        if transcript_sink is not None
        else make_file_transcript_sink(transcript_path)
    )
    transcript = Transcript(sink, now_utc=deps.now_utc)
    ctx = KitContext(
        options=options,
        lake_root=lake_root,
        run_id=run_id,
        quarantine_root=quarantine_root,
        budget=CallBudget(compute_call_budget(options)),
        transcript=transcript,
    )
    transcript.event(
        "run.start",
        run_id=run_id,
        lake_root=str(lake_root),
        symbol=options.symbol,
        stages=sorted(options.stages) if options.stages is not None else "all",
        keep_going=options.keep_going,
        depth_bisect=options.depth_bisect,
        auto_bisect=options.auto_bisect,
        rate_probe=options.rate_probe,
        against_lake=options.against_lake,
        mint_anyway=options.mint_anyway,
        call_budget=ctx.budget.limit,
    )
    outcomes: list[StageOutcome] = []
    for stage_id, name, stage_fn in STAGES:
        if options.stages is not None and stage_id not in options.stages:
            continue
        try:
            outcome = stage_fn(deps, ctx)
        except CallBudgetExceeded as exc:
            outcome = StageOutcome(
                stage=stage_id,
                name=name,
                status=STATUS_FAIL,
                lines=(render_diagnostic("kit.budget_exhausted", stage=name, detail=str(exc)),),
            )
        except OSError as exc:
            raise KitInternalError(
                f"stage {stage_id} ({name}) hit a kit-side I/O failure: {exc}"
            ) from exc
        transcript.event(
            "stage.done",
            stage=stage_id,
            name=name,
            status=outcome.status,
            lines=list(outcome.lines),
        )
        outcomes.append(outcome)
        _LOG.info(
            "doctor_live.stage",
            stage=stage_id,
            name=name,
            status=outcome.status,
            calls_used=ctx.budget.used,
        )
        if outcome.status == STATUS_FAIL and not options.keep_going:
            break
    transcript.event(
        "run.done",
        calls_used=ctx.budget.used,
        failed=any(outcome.status == STATUS_FAIL for outcome in outcomes),
    )
    return KitReport(run_id=run_id, transcript_path=transcript_path, outcomes=tuple(outcomes))


def resolve_probe_day(now: datetime, *, observed: AbstractSet[date] = frozenset()) -> date:
    """The kit's probe day: latest session STRICTLY before *now*'s IST date.

    Extracted PURE from ``build_real_deps._probe_day`` (hostile-review fold) so
    the probe-day rule is tested at 100% line+branch OUTSIDE the
    ``# pragma: no cover`` real-composition factory — the factory keeps only the
    binding. Steps backwards from IST-yesterday over weekends, listed holidays
    AND observably-untraded overlay days (DEC-CALENDAR-OBSERVED-SESSIONS-001,
    remove-only), using the STRICT
    :func:`brutex.calendars.observed_sessions.is_trading_day_checked_observed`,
    so the live vendor probe never targets a day the vendor demonstrably served
    no bars for. Bounded: the NSE week always contains a trading day and the
    loaded overlay set is finite.

    LOUD out-of-coverage handling (DEC-CALENDAR-EXCHANGE-SPLIT-001 pack): once
    the walk reaches a weekday OUTSIDE the published holiday span (from
    2027-01-01 on, until the list is extended), the checked calendar raises
    rather than silently guessing "trading day". This helper catches that,
    emits ONE ``calendar.coverage_exhausted`` structlog warning, and returns
    that weekday as the BEST-EFFORT probe day — explicitly flagged degraded
    (it may be an unlisted holiday, in which case the probe reads an empty
    session), never a silent guess and never a crash of the whole doctor run.
    """
    day = now.astimezone(IST).date() - timedelta(days=1)
    while True:
        try:
            if is_trading_day_checked_observed(day, observed=observed):
                return day
        except HolidayDataUnavailableError:
            _LOG.warning(
                "calendar.coverage_exhausted",
                probe_day=day.isoformat(),
                detail=(
                    "holiday list exhausted while resolving the vendor probe day; "
                    "using this weekday as a BEST-EFFORT probe day — it may be an "
                    "unlisted holiday (extend the calendar to restore a verified "
                    "probe day)"
                ),
            )
            return day
        day -= timedelta(days=1)


def build_real_deps() -> KitDeps:  # pragma: no cover — the real network/git/clock composition
    """Compose the REAL dependencies (network, git, boto3, SDK) for the CLI.

    Everything here is a thin shim over production seams that already exist;
    it is exercised only on the operator's Mac (the sandbox cannot — that is
    the kit's premise). Mirrors ``cli._resolve_code_commit_sha`` for the SHA.
    """
    import os
    import subprocess  # nosec B404 — fixed constant argv git reads only
    import sys
    import time as time_module
    import urllib.request

    import brutex
    from brutex.adapters.groww.auth import _real_token_factory, _real_totp
    from brutex.adapters.groww.aws_client import build_real_aws_client
    from brutex.adapters.groww.constants import (
        GROWW_RATE_LIMIT_PER_MIN,
        GROWW_RATE_LIMIT_RPS_CEILING,
        resolve_token_broker_region,
    )
    from brutex.adapters.groww.instrument_loader import urllib_http_get
    from brutex.adapters.groww.live import installed_growwapi_version
    from brutex.adapters.groww.reauth import _real_sdk_factory
    from brutex.data.bhavcopy import urllib_http_get_nse

    def _git(*args: str) -> str | None:
        try:
            out = subprocess.run(  # noqa: S603 # nosec B603 B607 — fixed constant argv, no shell
                ["git", *args],  # noqa: S607 — fixed argv, no user input
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.strip() or None

    def _git_sha() -> str:
        return _git("rev-parse", "--short", "HEAD") or f"brutex-{brutex.__version__}"

    def _behind_origin() -> int | None:
        count = _git("rev-list", "--count", "HEAD..origin/main")
        return int(count) if count is not None and count.isdigit() else None

    def _package_location() -> str:
        return str(Path(brutex.__file__).resolve().parent)

    def _repo_root() -> Path | None:
        candidate = Path(brutex.__file__).resolve().parents[1]
        return candidate if (candidate / "pyproject.toml").is_file() else None

    def _clock_offset_s() -> float:
        # HTTPS Date-header offset (the spec's NTP fallback): second-granular,
        # ample for the ±30 s TOTP question. Raises OSError when unreachable.
        request = urllib.request.Request(  # fixed https URL, never user input
            "https://www.apple.com", method="HEAD"
        )
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 — fixed https URL  # nosec B310
            server_date = response.headers.get("Date")
        if server_date is None:
            raise OSError("reference host sent no Date header")
        from email.utils import parsedate_to_datetime

        server = parsedate_to_datetime(server_date)
        return (datetime.now(UTC) - server).total_seconds()

    # Observed-sessions overlay (DEC-CALENDAR-OBSERVED-SESSIONS-001): loaded
    # ONCE per deps build (design W5), never per probe. The loader never raises
    # (missing/corrupt -> empty overlay).
    observed_holidays = load_observed_holidays(resolve_lake_root(os.environ, home=Path.home()))

    def _probe_day() -> date:
        # Binding only — the loud, coverage-aware, overlay-aware walk lives in
        # the module-level (and unit-tested) ``resolve_probe_day``; this shim
        # only supplies the clock + the loaded overlay.
        return resolve_probe_day(datetime.now(UTC), observed=observed_holidays)

    def _controller() -> RateController:
        return RateController(
            ceiling_rps=GROWW_RATE_LIMIT_RPS_CEILING,
            ceiling_per_min=GROWW_RATE_LIMIT_PER_MIN,
        )

    return KitDeps(
        now_utc=lambda: datetime.now(UTC),
        monotonic=time_module.monotonic,
        git_sha=_git_sha,
        behind_origin=_behind_origin,
        growwapi_version=installed_growwapi_version,
        package_location=_package_location,
        python_version=lambda: (
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        ),
        getproxies=urllib.request.getproxies,
        env=os.environ,
        home=Path.home(),
        cwd=Path.cwd(),
        repo_root=_repo_root(),
        ismount=os.path.ismount,
        resolve_creds=resolve_groww_aws_credentials,
        broker_region=resolve_token_broker_region,
        totp_minter=_real_totp,
        clock_offset_s=_clock_offset_s,
        aws_client_factory=lambda: build_real_aws_client(resolve_token_broker_region()),
        sdk_factory=_real_sdk_factory,
        real_token_factory=_real_token_factory,
        real_totp_minter=_real_totp,
        controller_factory=_controller,
        http_get=urllib_http_get,
        nse_http_get=urllib_http_get_nse,
        probe_day=_probe_day,
    )


__all__ = [
    "DEFAULT_CALL_BUDGET",
    "DEPTH_BISECT_BUDGET",
    "DIAGNOSTICS",
    "RATE_PROBE_ABORT_BURST",
    "RATE_PROBE_BUDGET",
    "STAGES",
    "STATUS_FAIL",
    "STATUS_PASS",
    "STATUS_SKIP",
    "STATUS_WARN",
    "CallBudget",
    "CallBudgetExceeded",
    "KitContext",
    "KitDeps",
    "KitInternalError",
    "KitOptions",
    "KitReport",
    "StageOutcome",
    "Transcript",
    "base32_problem",
    "build_real_deps",
    "compute_call_budget",
    "make_file_transcript_sink",
    "probe_sdk_shape",
    "render_diagnostic",
    "run_live_doctor",
]
