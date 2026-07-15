#!/usr/bin/env python3
"""Read-only Phase-0 feed probe for DhanHQ v2 and Zerodha Kite Connect v3.

This is a STANDALONE, stdlib-only operator tool. It pins the handful of
historical-data facts a build sandbox could not reach (all vendor hosts are
egress-blocked there) BEFORE any BruteX adapter is written. It answers:

DhanHQ (``/charts/intraday``, ``IDX_I`` index segment):
    (a) the real 1-minute intraday floor for NIFTY 50 / BANKNIFTY / SENSEX /
        INDIA VIX -- a small set of 1-week windows marched backward
        (recent, 2023-01, 2022-01, 2021-01, 2020-01) reporting which return
        candles;
    (b) whether INDIA VIX (``IDX_I`` / securityId 21) serves intraday candles
        at all on a recent date;
    (c) READ-ONLY auth: the probe REQUIRES a provided ``DHAN_ACCESS_TOKEN``
        (the SSM-read token TickVault minted) and FAILS LOUD if absent -- it
        NEVER mints (a ``generateAccessToken`` against the shared production
        Dhan account rotates the single active token and kills TickVault's
        live feed);
    (e) whether SENSEX on ``IDX_I`` (the BSE-index spot row) serves 1-minute
        candles -- covered by the recent window plus a mid-2022 (June) anchor
        in the backward floor march;
    (f) whether the expired-options ROLLING series (``POST /charts/rollingoption``)
        serves candles that carry VOLUME (+ oi / iv / spot) for NSE index options
        (NIFTY, ``NSE_FNO``) AND BSE index options (SENSEX, ``BSE_FNO``) -- the
        make-or-break capability gate for a later options phase. The request body
        matches the official SDK (``dhan-oss/DhanHQ-py`` ->
        ``_historical_data.expired_options_data``) field-for-field.
    (g) a scrip-master ID-CONFIRM (public ``api-scrip-master.csv``, NO auth): the
        three hardcoded ENGINE index securityIds (NIFTY 50=13, BANKNIFTY=25,
        SENSEX=51) are cross-checked against the id the CSV actually lists and
        each emits a LOUD ``CONFIRMED`` / ``MISMATCH`` / ``NOT-FOUND`` verdict
        (or ``UNCONFIRMED-CSV-UNREACHABLE`` when the host is egress-blocked in the
        operator's environment too) -- a wrong securityId silently pulls the WRONG
        instrument, so this is the honesty gate. It runs by default (needs NO
        creds -- the CSV is public) and makes no extra call beyond the one CSV
        GET goal (a) already issues; skip with ``--no-scrip-confirm``.

Zerodha Kite (``/instruments/historical/{token}/{interval}``):
    (d) SENSEX (token 265) recent minute servability -- the BSE-index risk row
        -- plus INDIA VIX (264969) and BANKNIFTY (260105) minute-depth spot
        checks at 2015-01, and a NIFTY (256265) recent control.

Hard guarantees (see the module-level HARD REQUIREMENTS map at the bottom):

* NO secret is ever printed, logged, or written. Credentials come ONLY from
  environment variables. Every emitted string passes through :class:`Redactor`,
  which scrubs each registered secret substring to ``***redacted***``.
* READ-ONLY. Only GET historical / POST charts (read) endpoints. NEVER a token
  mint (``generateAccessToken``), order, trade, modify, or IP-write endpoint --
  the access token MUST be provided (SSM-read; TickVault owns the lifecycle).
* Rate-safe: token-bucket pacing at <= 5 r/s (Dhan) / <= 3 r/s (Kite) and a
  bounded total call budget. 401/403/429 are reported, not spam-retried.
* Zero pip installs -- Python standard library only. The probe NEVER mints a
  token (no TOTP, no ``generateAccessToken``): the access token MUST be provided
  (SSM-read; TickVault owns the lifecycle). Nothing is imported from the
  ``brutex`` package.
* Each vendor is independently skippable. A missing-creds vendor is marked
  SKIPPED and never crashes the run.

Run ``python probe_feeds.py --dry-run`` to print the plan with ZERO env vars
and ZERO network calls. See ``feed-probe.md`` for the operator runbook.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# Constants (endpoints, enums, rate ceilings) -- all read-only surfaces.
# --------------------------------------------------------------------------- #

KIT_VERSION = "1.0.0"

DHAN_API_BASE = "https://api.dhan.co/v2"
DHAN_SCRIP_MASTER_CSV = "https://images.dhan.co/api-data/api-scrip-master.csv"
DHAN_INDEX_SEGMENT = "IDX_I"  # ONE shared index segment for NSE + BSE indices.
DHAN_INDEX_INSTRUMENT = "INDEX"
DHAN_MAX_RPS = 5.0  # documented Data-API cap.

KITE_API_BASE = "https://api.kite.trade"
KITE_HEADER_VERSION = "3"
KITE_MAX_RPS = 3.0  # documented historical-endpoint cap.

DEFAULT_TIMEOUT_S = 20.0
DEFAULT_CALL_BUDGET = 60  # generous ceiling; the full plan issues < 35 calls.
REDACTED = "***redacted***"

# HTTP statuses handled explicitly (named to keep verdict logic readable).
HTTP_OK = 200
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_RATE_LIMITED = 429
STATUS_TRANSPORT_ERROR = 0

# Small tuning constants.
MIN_SECRET_LEN = 4  # shorter values are not scrubbed (would corrupt text).
MAX_NOTE_CHARS = 200  # cap on any recorded response/error snippet.
AUTH_FAIL_LIMIT = 2  # short-circuit a vendor after this many 401/403s.
RATE_LIMIT_BACKOFF_S = 2.0  # single polite pause after a 429 (no retry).

# Documented / partially-verified fallback securityIds + tokens. The Dhan ids
# are RESOLVED from the live scrip master at runtime; these are only the loud
# "unverified" fallback when the CSV is unreachable.
DHAN_INDICES: tuple[tuple[str, int, str], ...] = (
    ("NIFTY 50", 13, "verified (official README)"),
    ("BANKNIFTY", 25, "partial (third-party)"),
    ("SENSEX", 51, "partial (unverified)"),
    ("INDIA VIX", 21, "partial (unverified)"),
)

# Normalised name aliases used to match index rows in the scrip master.
DHAN_ALIASES: dict[str, set[str]] = {
    "NIFTY 50": {"NIFTY", "NIFTY50"},
    "BANKNIFTY": {"BANKNIFTY", "NIFTYBANK"},
    "SENSEX": {"SENSEX", "BSESENSEX"},
    "INDIA VIX": {"INDIAVIX"},
}
_INDEX_MARKERS = {"IDXI", "INDEX", "IDX"}

# The three ENGINE indices whose hardcoded securityIds the scrip-master
# ID-CONFIRM step verifies (goal g). Their expected ids are the SAME single
# source as the intraday fallback ids above (NIFTY 50=13, BANKNIFTY=25,
# SENSEX=51); INDIA VIX is REFERENCE_ONLY and not part of the engine confirm.
DHAN_ID_CONFIRM_NAMES: tuple[str, ...] = ("NIFTY 50", "BANKNIFTY", "SENSEX")

# Scrip-master (compact ``api-scrip-master.csv``) columns this kit relies on --
# the header names are documented here so the operator's live run can confirm
# the mapping. Matching is column-TOLERANT (never positional): the securityId
# column is found by a ``SECURITY``+``ID`` header substring and index rows are
# matched by CELL CONTENT, so a header re-order or rename cannot silently break
# it. Verified column names (DhanHQ v2 instruments docs + dhan-oss/DhanHQ-py):
#   SEM_SMST_SECURITY_ID  -> the securityId (the confirm target).
#   SEM_INSTRUMENT_NAME   -> "INDEX" for an index row (the index marker).
#   SEM_TRADING_SYMBOL /  -> the human name matched by alias
#   SEM_CUSTOM_SYMBOL        ("NIFTY 50" / "NIFTY BANK" / "SENSEX").
#   SEM_EXM_EXCH_ID       -> "NSE" / "BSE" (informational).
#   SEM_SEGMENT           -> index segment code; the API exchangeSegment is IDX_I.
DHAN_SCRIP_MASTER_COLUMNS: tuple[str, ...] = (
    "SEM_SMST_SECURITY_ID",
    "SEM_INSTRUMENT_NAME",
    "SEM_TRADING_SYMBOL",
    "SEM_CUSTOM_SYMBOL",
    "SEM_EXM_EXCH_ID",
    "SEM_SEGMENT",
)

# Backward-march anchors for the Dhan intraday floor (Mon-anchored 1-week
# windows, guaranteed to span trading days). ``label, fromDate, toDate``.
DHAN_FLOOR_ANCHORS: tuple[tuple[str, str, str], ...] = (
    ("2023-01", "2023-01-09", "2023-01-13"),
    # A mid-year (June) 2022 anchor so SENSEX-on-IDX_I (the BSE-index spot row)
    # is probed off the January grid too -- settles "does Dhan serve BSE index 1m".
    ("2022-06", "2022-06-06", "2022-06-10"),
    ("2022-01", "2022-01-10", "2022-01-14"),
    ("2021-01", "2021-01-11", "2021-01-15"),
    ("2020-01", "2020-01-06", "2020-01-10"),
)

# Expired-options ROLLING-series probe (POST /charts/rollingoption). Body shape +
# value enums are copied verbatim from the official SDK
# (dhan-oss/DhanHQ-py -> _historical_data.expired_options_data), which asserts:
#   interval one of 1/5/15/25/60; expiryFlag WEEK or MONTH; drvOptionType CALL
#   or PUT; expiryCode 0..3; requiredData a subset of the fields listed below.
DHAN_ROLLINGOPTION_INTERVAL = 1
DHAN_ROLLINGOPTION_REQUIRED: tuple[str, ...] = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "oi",
    "iv",
    "spot",
)
# Two cases settle rollingoption servability + BSE_FNO servability at once.
# (underlying index name in the resolved-id map, exchangeSegment, instrument, label)
DHAN_ROLLINGOPTION_CASES: tuple[tuple[str, str, str, str], ...] = (
    ("NIFTY 50", "NSE_FNO", "OPTIDX", "NIFTY ATM CALL WEEK"),
    ("SENSEX", "BSE_FNO", "OPTIDX", "SENSEX ATM CALL WEEK"),
)
DHAN_ROLLINGOPTION_EXPIRY_FLAG = "WEEK"
DHAN_ROLLINGOPTION_EXPIRY_CODE = 0  # 0 = nearest expiry; operator may widen 0..3.
DHAN_ROLLINGOPTION_STRIKE = "ATM"
DHAN_ROLLINGOPTION_OPTION_TYPE = "CALL"
# Response-capability keys we look for (normalised alnum-upper) -> accepted aliases.
DHAN_ROLLING_CAP_ALIASES: dict[str, set[str]] = {
    "volume": {"VOLUME", "VOL"},
    "oi": {"OI", "OPENINTEREST"},
    "iv": {"IV", "IMPLIEDVOLATILITY"},
    "spot": {"SPOT", "SPOTPRICE", "UNDERLYINGSPOT", "UNDERLYING"},
}
MAX_ROLLING_WALK_DEPTH = 6  # bound the response-capability scan.

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Secret redaction
# --------------------------------------------------------------------------- #


class Redactor:
    """Scrub every registered secret substring from any emitted string.

    A single instance is threaded through all output/error paths. Register a
    secret once (token, key, PIN, TOTP secret, minted token) and any later
    :meth:`scrub` replaces its literal occurrences with ``***redacted***`` --
    a defence-in-depth net for accidental leaks in error bodies or URLs.
    """

    def __init__(self) -> None:
        self._secrets: list[str] = []

    def register(self, secret: str | None) -> None:
        """Register a secret value to be scrubbed from all future output.

        Parameters
        ----------
        secret:
            The raw secret string. ``None``, empty, and very short (< 4 char)
            values are ignored -- scrubbing those would corrupt ordinary text.
        """
        if not secret or len(secret) < MIN_SECRET_LEN:
            return
        if secret not in self._secrets:
            # Scrub longest-first so a token containing a shorter secret is
            # fully masked before its substring is considered.
            self._secrets.append(secret)
            self._secrets.sort(key=len, reverse=True)

    def scrub(self, text: str) -> str:
        """Return ``text`` with every registered secret replaced by the mask.

        Parameters
        ----------
        text:
            Arbitrary text (a URL, an error message, a response body).

        Returns
        -------
        str
            The text with all registered secrets masked.
        """
        cleaned = text
        for secret in self._secrets:
            if secret in cleaned:
                cleaned = cleaned.replace(secret, REDACTED)
        return cleaned


# --------------------------------------------------------------------------- #
# Pacing + call budget
# --------------------------------------------------------------------------- #


class Pacer:
    """A minimal token-bucket-style pacer capping issue rate to ``max_rps``."""

    def __init__(self, max_rps: float) -> None:
        self._min_interval = 1.0 / max_rps if max_rps > 0 else 0.0
        self._last = 0.0

    def wait(self) -> None:
        """Block until at least ``1/max_rps`` seconds have passed since the last call."""
        gap = self._min_interval - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()


class CallBudget:
    """A hard ceiling on the total number of network calls the run may issue."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self.spent = 0

    def spend(self) -> bool:
        """Consume one unit of budget.

        Returns
        -------
        bool
            ``True`` if a call may proceed, ``False`` once the budget is spent.
        """
        if self.spent >= self._limit:
            return False
        self.spent += 1
        return True


# --------------------------------------------------------------------------- #
# HTTP (stdlib only) -- returns (status, parsed_json_or_None, scrubbed_text)
# --------------------------------------------------------------------------- #


def http_json(
    method: str,
    url: str,
    headers: dict[str, str],
    *,
    body: bytes | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    redactor: Redactor,
) -> tuple[int, Any, str]:
    """Issue one HTTP request and return ``(status, parsed_json, scrubbed_text)``.

    Never raises: network/timeout errors return status ``0`` with a scrubbed
    message, and HTTP error statuses (401/403/429/...) return the code plus the
    scrubbed body so the caller can record and continue.

    Parameters
    ----------
    method:
        ``"GET"`` or ``"POST"``.
    url:
        The full request URL (already query-encoded).
    headers:
        Request headers.
    body:
        Optional request body bytes (JSON for the Dhan charts intraday /
        rollingoption POST reads; empty on GET).
    timeout:
        Socket timeout in seconds.
    redactor:
        Secret scrubber applied to every returned string.

    Returns
    -------
    tuple[int, Any, str]
        HTTP status (``0`` on transport failure), parsed JSON (or ``None``),
        and the scrubbed raw body / error text.
    """
    # URL is always a hard-coded https literal from this module's constants
    # (never user/vendor-controlled), so the urllib scheme audit is satisfied.
    req = urllib.request.Request(  # noqa: S310
        url, data=body, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            status = resp.getcode()
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            raw = exc.read().decode("utf-8", "replace")
        except OSError:  # response body may be unreadable; degrade cleanly
            raw = ""
    except urllib.error.URLError as exc:
        return STATUS_TRANSPORT_ERROR, None, redactor.scrub(f"network error: {exc.reason}")
    except Exception as exc:  # defensive: a probe must never crash the run
        return STATUS_TRANSPORT_ERROR, None, redactor.scrub(f"unexpected error: {exc!r}")
    raw = redactor.scrub(raw)
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        parsed = None
    return status, parsed, raw


# --------------------------------------------------------------------------- #
# Evidence record
# --------------------------------------------------------------------------- #


@dataclass
class ProbeRecord:
    """One probe's redacted evidence row."""

    vendor: str
    name: str
    endpoint: str
    request_summary: dict[str, Any]
    http_status: int | None
    verdict: str
    candle_count: int | None = None
    first_ts: str | None = None
    last_ts: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict of this record."""
        return {
            "vendor": self.vendor,
            "name": self.name,
            "endpoint": self.endpoint,
            "request_summary": self.request_summary,
            "http_status": self.http_status,
            "verdict": self.verdict,
            "candle_count": self.candle_count,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "notes": self.notes,
        }


# This is a STANDALONE operator runbook tool, not part of the ``brutex``
# package, so writing to the terminal IS the interface. All human output flows
# through these two audited emitters (never structlog); every string reaching
# them has already passed through the Redactor.
def _log(msg: str) -> None:
    """Emit an operator-facing progress line to stderr (already secret-free)."""
    print(msg, file=sys.stderr)  # noqa: T201 - stderr progress is this tool's UI


def _emit(msg: str) -> None:
    """Emit an operator-facing summary/plan line to stdout (already secret-free)."""
    print(msg, file=sys.stdout)  # noqa: T201 - stdout report is this tool's UI


def _recent_window() -> tuple[str, str]:
    """Return a small recent ``(from_date, to_date)`` (ISO, ending a few days ago)."""
    to_d = dt.date.today() - dt.timedelta(days=3)
    from_d = to_d - dt.timedelta(days=5)
    return from_d.isoformat(), to_d.isoformat()


def _options_window() -> tuple[str, str]:
    """Return a ~30-day recent-PAST ``(from_date, to_date)`` for the rolling-option probe.

    Ends a few days ago so at least one weekly expiry has already passed inside
    the window (rollingoption serves EXPIRED options), and spans ~30 days so
    several weekly expiries fall in range for ``expiryCode`` to roll across.
    """
    to_d = dt.date.today() - dt.timedelta(days=3)
    from_d = to_d - dt.timedelta(days=30)
    return from_d.isoformat(), to_d.isoformat()


def _epoch_to_ist(epoch: Any) -> str:
    """Render a Unix epoch (seconds) as an IST ISO string; pass through on error."""
    try:
        return dt.datetime.fromtimestamp(int(epoch), tz=IST).isoformat()
    except (ValueError, OverflowError, OSError, TypeError):
        return str(epoch)


# --------------------------------------------------------------------------- #
# Dhan: scrip-master securityId resolution
# --------------------------------------------------------------------------- #


def _normalise_token(text: str) -> str:
    """Upper-case and strip every non-alphanumeric char (for robust matching)."""
    return "".join(ch for ch in text.upper() if ch.isalnum())


def fetch_scrip_master(redactor: Redactor, pacer: Pacer, budget: CallBudget) -> str | None:
    """Fetch the public Dhan compact scrip-master CSV (no auth).

    Parameters
    ----------
    redactor, pacer, budget:
        Shared secret scrubber, rate pacer, and call budget.

    Returns
    -------
    str | None
        The CSV text, or ``None`` if the budget is spent or the host is
        unreachable (the caller then falls back to documented ids).
    """
    if not budget.spend():
        return None
    pacer.wait()
    status, _parsed, raw = http_json(
        "GET", DHAN_SCRIP_MASTER_CSV, {"Accept": "text/csv"}, redactor=redactor
    )
    if status == HTTP_OK and raw:
        return raw
    _log(f"  ! scrip master unreachable (status {status}); using documented ids")
    return None


def _scrip_master_matches(csv_text: str | None) -> tuple[dict[str, set[str]], str | None]:
    """Parse the scrip master into ``{index_name: {matched securityIds}}`` + column.

    Column-TOLERANT (never positional): the security-id column is located by a
    ``SECURITY``+``ID`` header substring (matches Dhan's ``SEM_SMST_SECURITY_ID``),
    then each index is matched by normalised name aliases across ALL cells of a
    row that ALSO carries an index marker (``INDEX`` / ``IDX`` -- Dhan's
    ``SEM_INSTRUMENT_NAME`` = ``INDEX``). One parse pass feeds BOTH the resolver
    and the ID-confirm step (COMMON -- no second CSV walk).

    Parameters
    ----------
    csv_text:
        The scrip-master CSV text, or ``None`` when unreachable.

    Returns
    -------
    tuple[dict[str, set[str]], str | None]
        The per-index matched-id sets and the resolved security-id header name
        (``None`` when the CSV is unreachable or the column is not found).
    """
    matches: dict[str, set[str]] = {name: set() for name, _id, _c in DHAN_INDICES}
    secid_col: str | None = None
    if not csv_text:
        return matches, secid_col
    reader = csv.reader(csv_text.splitlines())
    header = next(reader, None)
    secid_idx = -1
    if header:
        # Prefer the documented security-id column (the pinned scrip-master schema
        # in DHAN_SCRIP_MASTER_COLUMNS); fall back to the drift-tolerant
        # SECURITY+ID substring so a renamed header still resolves.
        documented = {c.upper() for c in DHAN_SCRIP_MASTER_COLUMNS}
        for idx, col in enumerate(header):
            upper = col.upper()
            if upper in documented and "SECURITY" in upper and "ID" in upper:
                secid_idx = idx
                secid_col = col
                break
        if secid_idx < 0:
            for idx, col in enumerate(header):
                upper = col.upper()
                if "SECURITY" in upper and "ID" in upper:
                    secid_idx = idx
                    secid_col = col
                    break
    if secid_idx < 0 or not header:
        return matches, secid_col
    for row in reader:
        if len(row) <= secid_idx:
            continue
        cells = {_normalise_token(c) for c in row if c}
        if cells.isdisjoint(_INDEX_MARKERS):
            continue
        for name, aliases in DHAN_ALIASES.items():
            if aliases & cells:
                sid = row[secid_idx].strip()
                if sid:
                    matches[name].add(sid)
    return matches, secid_col


def resolve_dhan_security_ids(
    csv_text: str | None, *, matches: dict[str, set[str]] | None = None
) -> dict[str, dict[str, Any]]:
    """Resolve the four index securityIds from the scrip master, with fallback.

    A unique scrip-master match is ``resolved``; zero or ambiguous matches fall
    back to the documented id, loudly marked. Parsing is delegated to
    :func:`_scrip_master_matches`; a caller that already parsed the CSV may pass
    ``matches`` to avoid a second walk.

    Parameters
    ----------
    csv_text:
        The scrip-master CSV text, or ``None`` when unreachable.
    matches:
        Optional pre-parsed match map from :func:`_scrip_master_matches`.

    Returns
    -------
    dict[str, dict[str, Any]]
        ``{index_name: {"security_id", "source", "segment", "instrument"}}``.
    """
    if matches is None:
        matches, _col = _scrip_master_matches(csv_text)
    resolved: dict[str, dict[str, Any]] = {}
    for name, fallback_id, confidence in DHAN_INDICES:
        found = matches.get(name, set())
        if len(found) == 1:
            resolved[name] = {
                "security_id": next(iter(found)),
                "source": "resolved (scrip master)",
                "segment": DHAN_INDEX_SEGMENT,
                "instrument": DHAN_INDEX_INSTRUMENT,
            }
        else:
            reason = (
                "csv unreachable"
                if not csv_text
                else ("no unique index match" if not found else f"ambiguous: {sorted(found)}")
            )
            resolved[name] = {
                "security_id": str(fallback_id),
                "source": f"UNVERIFIED fallback ({confidence}; {reason})",
                "segment": DHAN_INDEX_SEGMENT,
                "instrument": DHAN_INDEX_INSTRUMENT,
            }
    return resolved


# --------------------------------------------------------------------------- #
# Dhan: scrip-master ID-CONFIRM (goal g) -- verify the hardcoded engine ids
# --------------------------------------------------------------------------- #


def _confirm_expected_ids() -> tuple[tuple[str, int], ...]:
    """Return the ``(index name, hardcoded securityId)`` pairs to confirm.

    Derived from :data:`DHAN_INDICES` so the "hardcoded" ids the confirm step
    checks are the SAME single source as the intraday fallback ids
    (NIFTY 50=13, BANKNIFTY=25, SENSEX=51) -- they can never silently diverge.
    """
    by_name = {name: fid for name, fid, _c in DHAN_INDICES}
    return tuple((name, by_name[name]) for name in DHAN_ID_CONFIRM_NAMES)


def confirm_dhan_security_ids(
    csv_text: str | None,
    *,
    matches: dict[str, set[str]] | None = None,
    secid_col: str | None = None,
) -> list[ProbeRecord]:
    """Cross-check the hardcoded ENGINE index securityIds against the scrip master.

    This is the honesty gate (goal g): a wrong hardcoded securityId silently
    pulls the WRONG instrument, so for each of the three engine indices
    (NIFTY 50=13, BANKNIFTY=25, SENSEX=51) the id the CSV actually lists is
    compared against the hardcoded constant and ONE LOUD verdict is emitted:

    * ``CONFIRMED``   -- the CSV lists exactly the hardcoded id for that index.
    * ``MISMATCH``    -- the CSV lists a DIFFERENT id (both values shown loudly).
    * ``NOT-FOUND``   -- no ``IDX_I`` / ``INDEX`` row matched the index name.
    * ``UNCONFIRMED-CSV-UNREACHABLE`` -- the public CSV could not be fetched
      (non-fatal; the intraday probes still settle servability).

    No network call is made here -- it reuses the already-fetched ``csv_text`` /
    parsed ``matches`` (the ONE CSV GET is counted by :func:`fetch_scrip_master`,
    goal a), so it needs no creds and adds nothing to the call budget.

    Parameters
    ----------
    csv_text:
        The scrip-master CSV text, or ``None`` when unreachable.
    matches:
        Optional pre-parsed match map from :func:`_scrip_master_matches`.
    secid_col:
        The resolved security-id header name (for the evidence note); if
        ``matches`` is not supplied it is re-derived alongside it.

    Returns
    -------
    list[ProbeRecord]
        One redacted evidence row per confirmed index.
    """
    if matches is None:
        matches, secid_col = _scrip_master_matches(csv_text)
    col_note = (
        f"matched security-id column: {secid_col}"
        if secid_col
        else "documented column SEM_SMST_SECURITY_ID (header not resolved this run)"
    )
    records: list[ProbeRecord] = []
    for name, expected in _confirm_expected_ids():
        record = ProbeRecord(
            vendor="dhan",
            name=f"scrip-confirm: {name}",
            endpoint=f"GET {DHAN_SCRIP_MASTER_CSV}",
            request_summary={
                "index": name,
                "hardcoded_security_id": expected,
                "match_on": "SEM_INSTRUMENT_NAME=INDEX + name alias",
            },
            http_status=None,
            verdict="NOT_ATTEMPTED",
        )
        if not csv_text:
            record.verdict = "UNCONFIRMED-CSV-UNREACHABLE"
            record.notes.append(
                f"scrip master not fetched; hardcoded securityId={expected} NOT verified this run"
            )
            records.append(record)
            continue
        record.notes.append(col_note)
        found = matches.get(name, set())
        expected_str = str(expected)
        if not found:
            record.verdict = (
                f"NOT-FOUND (no IDX_I/INDEX row matched {name}; hardcoded {expected} UNVERIFIED)"
            )
        elif expected_str in found:
            record.verdict = f"CONFIRMED (csv id {expected_str} == hardcoded {expected})"
            extra = sorted(found - {expected_str})
            if extra:
                record.notes.append(
                    f"{len(found)} index rows matched {name}; expected id present, others: {extra}"
                )
        elif len(found) == 1:
            csv_id = next(iter(found))
            record.verdict = f"MISMATCH (csv id {csv_id} != hardcoded {expected})"
        else:
            record.verdict = f"MISMATCH (hardcoded {expected} not among csv ids {sorted(found)})"
        records.append(record)
    return records


# --------------------------------------------------------------------------- #
# Dhan: intraday probe
# --------------------------------------------------------------------------- #


def _dhan_candles(parsed: Any) -> tuple[int | None, Any]:
    """Extract ``(candle_count, timestamp_list)`` from a Dhan array-of-columns body."""
    for container in (parsed, parsed.get("data") if isinstance(parsed, dict) else None):
        if isinstance(container, dict):
            stamps = container.get("timestamp")
            if isinstance(stamps, list):
                return len(stamps), stamps
    return None, None


def dhan_intraday_probe(
    token: str,
    client_id: str | None,
    name: str,
    security_id: str,
    from_date: str,
    to_date: str,
    redactor: Redactor,
    pacer: Pacer,
    budget: CallBudget,
) -> ProbeRecord:
    """Probe ``POST /charts/intraday`` for one index over one 1-week window.

    Parameters
    ----------
    token:
        The Dhan access token (registered as a secret; sent in the header).
    client_id:
        Optional Dhan client id, sent as the ``dhanClientId`` header.
    name:
        Human label (e.g. ``"NIFTY 50 @ 2020-01"``).
    security_id, from_date, to_date:
        Instrument id and ``YYYY-MM-DD`` window bounds.
    redactor, pacer, budget:
        Shared scrubber, pacer, and budget.

    Returns
    -------
    ProbeRecord
        The redacted evidence row (status, candle count, first/last ts).
    """
    endpoint = f"POST {DHAN_API_BASE}/charts/intraday"
    summary = {
        "securityId": security_id,
        "exchangeSegment": DHAN_INDEX_SEGMENT,
        "instrument": DHAN_INDEX_INSTRUMENT,
        "interval": 1,
        "oi": False,
        "fromDate": from_date,
        "toDate": to_date,
    }
    record = ProbeRecord(
        vendor="dhan",
        name=name,
        endpoint=endpoint,
        request_summary=summary,
        http_status=None,
        verdict="NOT_ATTEMPTED",
    )
    if not budget.spend():
        record.verdict = "SKIPPED (call budget exhausted)"
        return record

    headers = {
        "access-token": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if client_id:
        headers["dhanClientId"] = client_id
    body = json.dumps(summary).encode("utf-8")
    pacer.wait()
    status, parsed, raw = http_json(
        "POST", endpoint.split(" ", 1)[1], headers, body=body, redactor=redactor
    )
    record.http_status = status
    _apply_candle_verdict(record, status, *_dhan_candles(parsed), raw, epoch=True)
    return record


# --------------------------------------------------------------------------- #
# Dhan: expired-options rolling-series probe (POST /charts/rollingoption)
# --------------------------------------------------------------------------- #


def _annotate_rolling_capabilities(record: ProbeRecord, parsed: Any) -> None:
    """Record which option-capability fields the rollingoption response carries.

    Walks the (uncertain-shape) parsed body bounded by depth/breadth and reports
    which of ``volume`` / ``oi`` / ``iv`` / ``spot`` appear as non-empty keys,
    plus the first ``expiry``-echo key (the ``expiryCode`` -> actual-date echo,
    a plain date -- never a secret). The finding is appended to the verdict (so
    it shows in the table + summary) AND to the notes (JSON detail).
    """
    present: set[str] = set()
    expiry_echo: str | None = None

    def walk(obj: Any, depth: int) -> None:
        nonlocal expiry_echo
        if depth > MAX_ROLLING_WALK_DEPTH:
            return
        if isinstance(obj, dict):
            for key, val in obj.items():
                norm = _normalise_token(str(key))
                for cap, aliases in DHAN_ROLLING_CAP_ALIASES.items():
                    if norm in aliases and val not in (None, "", [], {}):
                        present.add(cap)
                if expiry_echo is None and "EXPIRY" in norm and isinstance(val, (str, int, float)):
                    expiry_echo = f"{key}={val}"
                walk(val, depth + 1)
        elif isinstance(obj, list):
            for item in obj[:50]:  # bound breadth: a candle column can be long.
                walk(item, depth + 1)

    walk(parsed, 0)
    fields = ", ".join(sorted(present)) if present else "none detected"
    record.notes.append("option fields present: " + fields)
    if record.verdict.startswith("CANDLES"):
        record.verdict += f" · fields[{fields}]"
    if expiry_echo is not None:
        record.notes.append("expiryCode->echo: " + str(expiry_echo)[:MAX_NOTE_CHARS])


def dhan_rollingoption_probe(
    token: str,
    client_id: str | None,
    name: str,
    underlying_security_id: str,
    exchange_segment: str,
    instrument_type: str,
    from_date: str,
    to_date: str,
    redactor: Redactor,
    pacer: Pacer,
    budget: CallBudget,
) -> ProbeRecord:
    """Probe ``POST /charts/rollingoption`` for one expired-options rolling case.

    The request body is copied field-for-field from the official SDK
    (``dhan-oss/DhanHQ-py`` -> ``_historical_data.expired_options_data``); the
    SDK's HTTP layer additionally injects ``dhanClientId`` into the JSON body on
    every POST, which this replicates (the client id is a secret -- registered,
    injected into the SENT body/header, and never written to ``request_summary``).

    Parameters
    ----------
    token:
        The Dhan access token (registered as a secret; sent in the header).
    client_id:
        Optional Dhan client id (sent as the ``client-id`` / ``dhanClientId``
        headers and injected into the body, matching the SDK).
    name:
        Human label (e.g. ``"rollingoption: NIFTY ATM CALL WEEK"``).
    underlying_security_id:
        The UNDERLYING index securityId (the same id resolved for the IDX_I spot
        probe -- 13 for NIFTY, 51 for SENSEX on the unverified fallback).
    exchange_segment, instrument_type:
        e.g. ``"NSE_FNO"`` / ``"BSE_FNO"`` and ``"OPTIDX"``.
    from_date, to_date:
        ``YYYY-MM-DD`` recent-past window bounds.
    redactor, pacer, budget:
        Shared scrubber, pacer, and budget.

    Returns
    -------
    ProbeRecord
        The redacted evidence row (status, candle count, first/last ts, and the
        volume / oi / iv / spot + expiry-echo capability notes).
    """
    endpoint = f"POST {DHAN_API_BASE}/charts/rollingoption"
    # request_summary is the SDK body MINUS the injected dhanClientId (a secret).
    summary: dict[str, Any] = {
        "securityId": underlying_security_id,
        "exchangeSegment": exchange_segment,
        "instrument": instrument_type,
        "expiryFlag": DHAN_ROLLINGOPTION_EXPIRY_FLAG,
        "expiryCode": DHAN_ROLLINGOPTION_EXPIRY_CODE,
        "strike": DHAN_ROLLINGOPTION_STRIKE,
        "drvOptionType": DHAN_ROLLINGOPTION_OPTION_TYPE,
        "requiredData": list(DHAN_ROLLINGOPTION_REQUIRED),
        "fromDate": from_date,
        "toDate": to_date,
        "interval": DHAN_ROLLINGOPTION_INTERVAL,
    }
    record = ProbeRecord(
        vendor="dhan",
        name=name,
        endpoint=endpoint,
        request_summary=summary,
        http_status=None,
        verdict="NOT_ATTEMPTED",
    )
    if not budget.spend():
        record.verdict = "SKIPPED (call budget exhausted)"
        return record

    headers = {
        "access-token": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    sent_body = dict(summary)
    if client_id:
        # SDK data-API header is `client-id`; the intraday probe additionally
        # sends `dhanClientId` -- keep both for parity, and inject dhanClientId
        # into the body exactly as the SDK's _send_request does.
        headers["client-id"] = client_id
        headers["dhanClientId"] = client_id
        sent_body["dhanClientId"] = client_id
    body = json.dumps(sent_body).encode("utf-8")
    pacer.wait()
    status, parsed, raw = http_json(
        "POST", endpoint.split(" ", 1)[1], headers, body=body, redactor=redactor
    )
    record.http_status = status
    _apply_candle_verdict(record, status, *_dhan_candles(parsed), raw, epoch=True)
    if status == HTTP_OK:
        _annotate_rolling_capabilities(record, parsed)
    return record


# --------------------------------------------------------------------------- #
# Kite: historical probe
# --------------------------------------------------------------------------- #


def _kite_candles(parsed: Any) -> tuple[int | None, Any]:
    """Extract ``(candle_count, candle_list)`` from a Kite historical body."""
    if isinstance(parsed, dict):
        data = parsed.get("data")
        if isinstance(data, dict):
            candles = data.get("candles")
            if isinstance(candles, list):
                return len(candles), candles
    return None, None


def _kite_row_ts(row: Any) -> str | None:
    """Return a Kite candle row's timestamp (index 0) defensively, or ``None``.

    Kite candle rows are documented as ``[ts, o, h, l, c, volume]`` lists, but a
    surprising/malformed body (a scalar or dict row) must never crash the probe;
    an unsubscriptable row yields ``None`` instead of raising ``TypeError``.
    """
    if isinstance(row, (list, tuple)) and row:
        return str(row[0])
    return None


def kite_historical_probe(
    api_key: str,
    access_token: str,
    name: str,
    instrument_token: int,
    from_dt: str,
    to_dt: str,
    redactor: Redactor,
    pacer: Pacer,
    budget: CallBudget,
    *,
    interval: str = "minute",
) -> ProbeRecord:
    """Probe ``GET /instruments/historical/{token}/{interval}`` for one window.

    Parameters
    ----------
    api_key, access_token:
        Kite credentials -- combined into the ``Authorization: token key:tok``
        header; both registered as secrets.
    name:
        Human label (e.g. ``"SENSEX 265 @ recent"``).
    instrument_token:
        Numeric Kite instrument token.
    from_dt, to_dt:
        ``"YYYY-MM-DD HH:MM:SS"`` naive-IST window bounds.
    redactor, pacer, budget:
        Shared scrubber, pacer, and budget.
    interval:
        Kite interval (default ``"minute"``).

    Returns
    -------
    ProbeRecord
        The redacted evidence row.
    """
    path = f"/instruments/historical/{instrument_token}/{interval}"
    query = urllib.parse.urlencode({"from": from_dt, "to": to_dt, "continuous": 0, "oi": 0})
    url = f"{KITE_API_BASE}{path}?{query}"
    summary = {
        "instrument_token": instrument_token,
        "interval": interval,
        "from": from_dt,
        "to": to_dt,
        "continuous": 0,
        "oi": 0,
    }
    record = ProbeRecord(
        vendor="kite",
        name=name,
        endpoint=f"GET {KITE_API_BASE}{path}",
        request_summary=summary,
        http_status=None,
        verdict="NOT_ATTEMPTED",
    )
    if not budget.spend():
        record.verdict = "SKIPPED (call budget exhausted)"
        return record

    headers = {
        "X-Kite-Version": KITE_HEADER_VERSION,
        "Authorization": f"token {api_key}:{access_token}",
        "Accept": "application/json",
    }
    pacer.wait()
    status, parsed, raw = http_json("GET", url, headers, redactor=redactor)
    record.http_status = status
    count, candles = _kite_candles(parsed)
    first = _kite_row_ts(candles[0]) if candles else None
    last = _kite_row_ts(candles[-1]) if candles else None
    _apply_status_verdict(record, status, count, first, last, parsed, raw)
    return record


# --------------------------------------------------------------------------- #
# Shared verdict helpers
# --------------------------------------------------------------------------- #


def _apply_candle_verdict(
    record: ProbeRecord, status: int, count: int | None, stamps: Any, raw: str, *, epoch: bool
) -> None:
    """Fill verdict/first/last for a Dhan array-of-columns response."""
    first = last = None
    if stamps:
        first = _epoch_to_ist(stamps[0]) if epoch else str(stamps[0])
        last = _epoch_to_ist(stamps[-1]) if epoch else str(stamps[-1])
    _apply_status_verdict(record, status, count, first, last, None, raw)


def _apply_status_verdict(
    record: ProbeRecord,
    status: int,
    count: int | None,
    first: str | None,
    last: str | None,
    parsed: Any,
    raw: str,
) -> None:
    """Set ``record.verdict`` (and candle detail) from status + candle count."""
    record.candle_count = count
    record.first_ts = first
    record.last_ts = last
    error_type = None
    if isinstance(parsed, dict):
        error_type = parsed.get("error_type") or parsed.get("errorType")
    if status == HTTP_OK and count and count > 0:
        record.verdict = f"CANDLES ({count})"
    elif status == HTTP_OK and count == 0:
        record.verdict = "EMPTY (200, zero candles)"
    elif status == HTTP_OK:
        # count is None -> the 200 body did not match a known candle shape (key
        # drift / a different container). Report it distinctly and keep the body
        # (already redacted by http_json) so a shape drift is diagnosable rather
        # than masquerading as "no data".
        record.verdict = "HTTP_200_UNRECOGNIZED_SHAPE"
        if raw:
            record.notes.append(
                "unrecognized 200 body (redacted, truncated): " + raw[:MAX_NOTE_CHARS]
            )
    elif status == HTTP_UNAUTHORIZED:
        record.verdict = "UNAUTHORIZED (401: token invalid/expired)"
    elif status == HTTP_FORBIDDEN:
        # 403 is vendor-ambiguous: Kite returns it for an invalid/EXPIRED token
        # (error_type=TokenException; Kite never uses 401), while Dhan returns it
        # for a missing Data-API subscription. Surface both + error_type so the
        # two are distinguishable at a glance, not only in the note.
        detail = f"; error_type={error_type}" if error_type else ""
        record.verdict = (
            f"FORBIDDEN (403: token invalid/expired OR entitlement/subscription{detail})"
        )
    elif status == HTTP_RATE_LIMITED:
        record.verdict = "RATE_LIMITED (429)"
    elif status == STATUS_TRANSPORT_ERROR:
        record.verdict = "TRANSPORT_ERROR"
        if raw:
            record.notes.append(raw[:MAX_NOTE_CHARS])
    else:
        record.verdict = f"HTTP_{status}"
    if status not in (HTTP_OK, STATUS_TRANSPORT_ERROR) and isinstance(parsed, dict):
        msg = parsed.get("message") or parsed.get("errorType")
        if msg:
            record.notes.append("message: " + str(msg)[:MAX_NOTE_CHARS])


# --------------------------------------------------------------------------- #
# Vendor runners
# --------------------------------------------------------------------------- #


def run_dhan(
    redactor: Redactor,
    budget: CallBudget,
    *,
    run_rollingoption: bool = True,
    run_scrip_confirm: bool = True,
) -> tuple[list[ProbeRecord], dict[str, Any]]:
    """Run all Dhan probes: resolve ids, ID-confirm, march, options.

    Parameters
    ----------
    redactor, budget:
        Shared secret scrubber and call budget.
    run_rollingoption:
        When ``True`` (default) the expired-options ``/charts/rollingoption``
        capability probes run after the intraday march; ``--no-rollingoption``
        turns them off.
    run_scrip_confirm:
        When ``True`` (default) the scrip-master ID-CONFIRM step (goal g) runs
        after id resolution -- it needs NO creds and makes no extra call, so it
        settles the three hardcoded engine securityIds on every Dhan run;
        ``--no-scrip-confirm`` turns it off.

    Returns
    -------
    tuple[list[ProbeRecord], dict]
        The probe records and the resolved-id map (both redaction-safe).
    """
    pacer = Pacer(DHAN_MAX_RPS)
    records: list[ProbeRecord] = []
    client_id = os.environ.get("DHAN_CLIENT_ID")
    token = os.environ.get("DHAN_ACCESS_TOKEN")
    redactor.register(client_id)
    redactor.register(token)

    # Public scrip-master resolution runs regardless of auth. Parse the CSV ONCE
    # and feed BOTH the resolver and the ID-confirm step (goal g).
    _log("[dhan] resolving securityIds from scrip master…")
    csv_text = fetch_scrip_master(redactor, pacer, budget)
    matches, secid_col = _scrip_master_matches(csv_text)
    resolved = resolve_dhan_security_ids(csv_text, matches=matches)
    for name, info in resolved.items():
        _log(f"  - {name}: securityId={info['security_id']} [{info['source']}]")

    # Goal (g): confirm the three hardcoded engine securityIds against the CSV.
    # No creds, no extra call -- runs on every Dhan run unless --no-scrip-confirm.
    if run_scrip_confirm:
        _log("[dhan] confirming hardcoded engine securityIds against scrip master…")
        confirm_recs = confirm_dhan_security_ids(csv_text, matches=matches, secid_col=secid_col)
        records.extend(confirm_recs)
        for rec in confirm_recs:
            _log(f"  - {rec.name}: {rec.verdict}")

    # Token: READ-ONLY, never minted. This probe REQUIRES a provided
    # DHAN_ACCESS_TOKEN (the SSM-read token TickVault minted) — it never runs
    # generateAccessToken (a mint against the shared production Dhan account
    # would rotate the single active token and INVALIDATE TickVault's live
    # feed). Absent → fail loud, intraday probes SKIPPED (the scrip-master
    # ID-confirm above needs no token and already ran).
    if token:
        _log("[dhan] using DHAN_ACCESS_TOKEN from env (SSM-read; never minted here)")
    else:
        _log(
            "[dhan] no Dhan access token provided — provision it via TickVault's "
            "minter into SSM; this probe never mints. Intraday probes SKIPPED."
        )

    if not token:
        for name in resolved:
            records.append(
                ProbeRecord(
                    vendor="dhan",
                    name=f"{name} intraday",
                    endpoint="(skipped)",
                    request_summary={"reason": "no access token"},
                    http_status=None,
                    verdict="SKIPPED (no token)",
                )
            )
        return records, resolved

    # Goal (a)/(b): recent servability + backward floor march per instrument.
    recent_from, recent_to = _recent_window()
    auth_failures = 0
    for name, info in resolved.items():
        sid = info["security_id"]
        anchors = [("recent", recent_from, recent_to), *DHAN_FLOOR_ANCHORS]
        for label, frm, to in anchors:
            if auth_failures >= AUTH_FAIL_LIMIT:
                records.append(
                    ProbeRecord(
                        vendor="dhan",
                        name=f"{name} @ {label}",
                        endpoint="(skipped)",
                        request_summary={"reason": "prior auth failures"},
                        http_status=None,
                        verdict="SKIPPED (after >=2 auth failures)",
                    )
                )
                continue
            rec = dhan_intraday_probe(
                token, client_id, f"{name} @ {label}", sid, frm, to, redactor, pacer, budget
            )
            records.append(rec)
            _log(f"  - {rec.name}: {rec.verdict}")
            if rec.http_status in (HTTP_UNAUTHORIZED, HTTP_FORBIDDEN):
                auth_failures += 1
            if rec.http_status == HTTP_RATE_LIMITED:
                time.sleep(RATE_LIMIT_BACKOFF_S)  # single polite backoff; no retry.

    # Goals (f): expired-options rolling-series capability + BSE_FNO servability.
    if run_rollingoption:
        opt_from, opt_to = _options_window()
        for idx_name, segment, instrument_type, label in DHAN_ROLLINGOPTION_CASES:
            info = resolved.get(idx_name)
            underlying_sid = info["security_id"] if info else None
            name = f"rollingoption: {label}"
            if underlying_sid is None:
                records.append(
                    ProbeRecord(
                        vendor="dhan",
                        name=name,
                        endpoint="(skipped)",
                        request_summary={"reason": f"underlying {idx_name} id unresolved"},
                        http_status=None,
                        verdict="SKIPPED (underlying id unresolved)",
                    )
                )
                continue
            if auth_failures >= AUTH_FAIL_LIMIT:
                records.append(
                    ProbeRecord(
                        vendor="dhan",
                        name=name,
                        endpoint="(skipped)",
                        request_summary={"reason": "prior auth failures"},
                        http_status=None,
                        verdict="SKIPPED (after >=2 auth failures)",
                    )
                )
                continue
            rec = dhan_rollingoption_probe(
                token,
                client_id,
                name,
                underlying_sid,
                segment,
                instrument_type,
                opt_from,
                opt_to,
                redactor,
                pacer,
                budget,
            )
            records.append(rec)
            _log(f"  - {rec.name}: {rec.verdict}")
            if rec.http_status in (HTTP_UNAUTHORIZED, HTTP_FORBIDDEN):
                auth_failures += 1
            if rec.http_status == HTTP_RATE_LIMITED:
                time.sleep(RATE_LIMIT_BACKOFF_S)
    return records, resolved


# --------------------------------------------------------------------------- #
# Dhan: --floor-edge stage (DEC-DHAN-ROLLING-FLOOR-001 gates G-A/G-B/G-H/G-J).
# Day-resolution rolling-edge march + straddle + over-cap + volume capture.
# Adds ~13 calls inside the existing budget; UNRUN in-sandbox (egress-blocked).
# --------------------------------------------------------------------------- #

# The NIFTY edge-march day offsets around E0 = today - 5 years (Feb-29 rounds
# FORWARD to Mar-1, mirroring brutex.adapters.dhan.constants.dhan_rolling_edge —
# a floor may only ever be LATER than the true edge). One single-day window per
# offset; the FLOOR_EDGE verdict grammar reads SERVED/EMPTY/HTTP_<code> per day.
FLOOR_EDGE_OFFSETS_DAYS: tuple[int, ...] = (-30, -7, -1, 0, 1, 7, 30)


def _floor_edge_e0(today: dt.date) -> dt.date:
    """today - 5y with the Feb-29 FORWARD rounding (the adapter's twin rule)."""
    try:
        return today.replace(year=today.year - 5)
    except ValueError:  # Feb-29 on a non-leap target year
        return dt.date(today.year - 5, 3, 1)


def dhan_volume_literal_probe(
    token: str,
    client_id: str | None,
    name: str,
    security_id: str,
    from_date: str,
    to_date: str,
    redactor: Redactor,
    pacer: Pacer,
    budget: CallBudget,
) -> ProbeRecord:
    """Gate G-J: capture the LITERAL volume / open_interest array head for IDX_I.

    The adapter drops index volume unread (drop_volume=True) and tolerates an
    ABSENT array; what Dhan actually returns (zeros? nulls? absent?) is
    UNVERIFIED -- this records the literal first values so the tolerance can be
    re-derived from evidence.
    """
    record = dhan_intraday_probe(
        token, client_id, name, security_id, from_date, to_date, redactor, pacer, budget
    )
    # Re-issue is NOT needed: dhan_intraday_probe already recorded the verdict;
    # the literal capture rides one EXTRA request so the raw arrays are in hand.
    if not budget.spend():
        record.notes.append("volume-literal capture skipped (budget exhausted)")
        return record
    endpoint = f"{DHAN_API_BASE}/charts/intraday"
    headers = {
        "access-token": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if client_id:
        headers["dhanClientId"] = client_id
    body = json.dumps(
        {
            "securityId": security_id,
            "exchangeSegment": DHAN_INDEX_SEGMENT,
            "instrument": DHAN_INDEX_INSTRUMENT,
            "interval": 1,
            "oi": True,  # ALSO ask for OI so the IDX_I OI content is captured
            "fromDate": from_date,
            "toDate": to_date,
        }
    ).encode("utf-8")
    pacer.wait()
    status, parsed, _raw = http_json("POST", endpoint, headers, body=body, redactor=redactor)
    container = (
        parsed.get("data")
        if isinstance(parsed, dict) and isinstance(parsed.get("data"), dict)
        else parsed
    )
    if isinstance(container, dict):
        for column in ("volume", "open_interest"):
            values = container.get(column)
            head = values[:3] if isinstance(values, list) else values
            record.notes.append(
                redactor.scrub(f"G-J literal {column} (oi=true, http {status}): {head!r}")[
                    :MAX_NOTE_CHARS
                ]
            )
    else:
        record.notes.append(f"G-J literal capture: non-dict body (http {status})")
    return record


def run_dhan_floor_edge(
    token: str,
    client_id: str | None,
    resolved: dict[str, Any],
    redactor: Redactor,
    budget: CallBudget,
) -> list[ProbeRecord]:
    """The ``--floor-edge`` stage (DEC-DHAN-ROLLING-FLOOR-001 probe extension).

    ~13 read-only calls: (a) gate G-A -- a day-resolution NIFTY march around
    E0 = today-5y (offsets -30/-7/-1/0/+1/+7/+30, one single-day window each:
    where does SERVED flip to EMPTY/error?); (b) one confirm window per
    remaining index at E0+7d (is the edge per-instrument?); (c) gate G-B -- a
    [E0-7d, E0+7d) STRADDLE window (the silent-truncation detector: a SERVED
    verdict whose first_ts sits at/after the edge instead of the requested
    start means pre-edge spans are silently truncated -- recorded clean-SUCCESS
    poison for an unclamped puller); (d) gate G-H -- one 91-day over-cap
    request (error code vs truncation); (e) gate G-J -- one IDX_I volume/OI
    literal-values capture. Verdict grammar: every record is named
    ``floor-edge: ...`` and reads the standard SERVED (n bars) / EMPTY /
    HTTP_<code> verdicts -- the FLOOR_EDGE reading rows live in feed-probe.md.
    """
    pacer = Pacer(DHAN_MAX_RPS)
    records: list[ProbeRecord] = []
    today = dt.date.today()
    e0 = _floor_edge_e0(today)
    one_day = dt.timedelta(days=1)

    def _window(day: dt.date, days: int = 1) -> tuple[str, str]:
        return day.isoformat(), (day + dt.timedelta(days=days)).isoformat()

    nifty = resolved.get("NIFTY 50")
    nifty_sid = nifty["security_id"] if nifty else None
    if nifty_sid is None:
        records.append(
            ProbeRecord(
                vendor="dhan",
                name="floor-edge: NIFTY march",
                endpoint="(skipped)",
                request_summary={"reason": "NIFTY id unresolved"},
                http_status=None,
                verdict="SKIPPED (NIFTY id unresolved)",
            )
        )
        return records

    # (a) G-A: the NIFTY day-resolution march around E0.
    for offset in FLOOR_EDGE_OFFSETS_DAYS:
        day = e0 + dt.timedelta(days=offset)
        frm, to = _window(day)
        rec = dhan_intraday_probe(
            token,
            client_id,
            f"floor-edge: NIFTY @ E0{offset:+d}d ({day.isoformat()})",
            nifty_sid,
            frm,
            to,
            redactor,
            pacer,
            budget,
        )
        records.append(rec)
        _log(f"  - {rec.name}: {rec.verdict}")
        if rec.http_status == HTTP_RATE_LIMITED:
            time.sleep(RATE_LIMIT_BACKOFF_S)

    # (b) one confirm window per remaining index at E0+7d (per-instrument edge?).
    confirm_day = e0 + dt.timedelta(days=7)
    for name, info in resolved.items():
        if name == "NIFTY 50":
            continue
        frm, to = _window(confirm_day)
        rec = dhan_intraday_probe(
            token,
            client_id,
            f"floor-edge: {name} @ E0+7d ({confirm_day.isoformat()})",
            info["security_id"],
            frm,
            to,
            redactor,
            pacer,
            budget,
        )
        records.append(rec)
        _log(f"  - {rec.name}: {rec.verdict}")

    # (c) G-B: the straddle window [E0-7d, E0+7d) -- silent-truncation detector.
    frm, to = (e0 - dt.timedelta(days=7)).isoformat(), (e0 + dt.timedelta(days=7)).isoformat()
    rec = dhan_intraday_probe(
        token,
        client_id,
        f"floor-edge: NIFTY straddle [{frm}, {to})",
        nifty_sid,
        frm,
        to,
        redactor,
        pacer,
        budget,
    )
    rec.notes.append(
        "G-B reading: SERVED with first_ts at/after the edge (not the requested "
        "start) = SILENT TRUNCATION -- the integrity-violating pre-floor shape "
        "the +7d clamp margin guards against"
    )
    records.append(rec)
    _log(f"  - {rec.name}: {rec.verdict}")

    # (d) G-H: one 91-day over-cap request (the documented cap is 90).
    over_start = e0 + dt.timedelta(days=30)
    frm, to = over_start.isoformat(), (over_start + dt.timedelta(days=91)).isoformat()
    rec = dhan_intraday_probe(
        token,
        client_id,
        f"floor-edge: NIFTY 91-day over-cap [{frm}, {to})",
        nifty_sid,
        frm,
        to,
        redactor,
        pacer,
        budget,
    )
    rec.notes.append("G-H reading: which error code (804/814?) vs silent truncation at 90d")
    records.append(rec)
    _log(f"  - {rec.name}: {rec.verdict}")

    # (e) G-J: the IDX_I volume/open_interest literal capture on a recent window.
    recent_from, recent_to = _recent_window()
    rec = dhan_volume_literal_probe(
        token,
        client_id,
        "floor-edge: NIFTY volume/OI literal capture",
        nifty_sid,
        recent_from,
        recent_to,
        redactor,
        pacer,
        budget,
    )
    records.append(rec)
    _log(f"  - {rec.name}: {rec.verdict}")
    _ = one_day  # (kept for symmetry with the window helper's default)
    return records


def run_kite(redactor: Redactor, budget: CallBudget) -> list[ProbeRecord]:
    """Run all Kite probes (goal d): SENSEX/VIX/BANKNIFTY checks + NIFTY control.

    Returns
    -------
    list[ProbeRecord]
        The probe records (redaction-safe).
    """
    pacer = Pacer(KITE_MAX_RPS)
    records: list[ProbeRecord] = []
    api_key = os.environ.get("KITE_API_KEY")
    access_token = os.environ.get("KITE_ACCESS_TOKEN")
    if not (api_key and access_token):
        _log("[kite] KITE_API_KEY / KITE_ACCESS_TOKEN not set -- SKIPPED")
        records.append(
            ProbeRecord(
                vendor="kite",
                name="all",
                endpoint="(skipped)",
                request_summary={"reason": "no credentials"},
                http_status=None,
                verdict="SKIPPED (no creds)",
            )
        )
        return records
    redactor.register(api_key)
    redactor.register(access_token)

    recent_from, recent_to = _recent_window()
    r_from = f"{recent_from} 09:15:00"
    r_to = f"{recent_to} 15:30:00"
    d2015_from = "2015-01-05 09:15:00"
    d2015_to = "2015-01-09 15:30:00"
    plan = [
        ("NIFTY 50 256265 @ recent (control)", 256265, r_from, r_to),
        ("SENSEX 265 @ recent (BSE risk row)", 265, r_from, r_to),
        ("INDIA VIX 264969 @ recent", 264969, r_from, r_to),
        ("INDIA VIX 264969 @ 2015-01 (depth)", 264969, d2015_from, d2015_to),
        ("BANKNIFTY 260105 @ 2015-01 (depth)", 260105, d2015_from, d2015_to),
    ]
    auth_failures = 0
    for name, tok, frm, to in plan:
        if auth_failures >= AUTH_FAIL_LIMIT:
            records.append(
                ProbeRecord(
                    vendor="kite",
                    name=name,
                    endpoint="(skipped)",
                    request_summary={"reason": "prior auth failures"},
                    http_status=None,
                    verdict="SKIPPED (after >=2 auth failures)",
                )
            )
            continue
        rec = kite_historical_probe(
            api_key, access_token, name, tok, frm, to, redactor, pacer, budget
        )
        records.append(rec)
        _log(f"  - {rec.name}: {rec.verdict}")
        if rec.http_status in (HTTP_UNAUTHORIZED, HTTP_FORBIDDEN):
            auth_failures += 1
        if rec.http_status == HTTP_RATE_LIMITED:
            time.sleep(RATE_LIMIT_BACKOFF_S)
    return records


# --------------------------------------------------------------------------- #
# Evidence emission + human summary
# --------------------------------------------------------------------------- #


def build_evidence(
    dhan_records: list[ProbeRecord],
    resolved: dict[str, Any],
    kite_records: list[ProbeRecord],
    budget: CallBudget,
) -> dict[str, Any]:
    """Assemble the structured evidence document (already redaction-safe)."""
    return {
        "probe_kit": "brutex-feed-probe",
        "version": KIT_VERSION,
        "generated_utc": dt.datetime.now(tz=dt.UTC).isoformat(),
        "calls_issued": budget.spent,
        "dhan": {
            "configured": bool(
                os.environ.get("DHAN_CLIENT_ID") or os.environ.get("DHAN_ACCESS_TOKEN")
            ),
            "resolved_ids": resolved,
            "probes": [r.to_dict() for r in dhan_records],
        },
        "kite": {
            "configured": bool(
                os.environ.get("KITE_API_KEY") and os.environ.get("KITE_ACCESS_TOKEN")
            ),
            "probes": [r.to_dict() for r in kite_records],
        },
    }


def _evidence_markdown(evidence: dict[str, Any]) -> str:
    """Render the evidence document as committable markdown."""
    lines = [
        "# BruteX feed-probe evidence",
        "",
        f"- Kit version: `{evidence['version']}`",
        f"- Generated (UTC): `{evidence['generated_utc']}`",
        f"- Network calls issued: `{evidence['calls_issued']}`",
        "",
        "> No secret is emitted in this file. Tokens/keys/PINs are redacted.",
        "",
        "## Dhan",
        "",
        f"- Configured: `{evidence['dhan']['configured']}`",
        "",
        "### Resolved securityIds",
        "",
        "| Index | securityId | segment | source |",
        "| --- | --- | --- | --- |",
    ]
    for name, info in evidence["dhan"]["resolved_ids"].items():
        lines.append(f"| {name} | {info['security_id']} | {info['segment']} | {info['source']} |")
    lines += [
        "",
        "### Probes",
        "",
        _probe_table(evidence["dhan"]["probes"]),
        "",
        "## Kite",
        "",
        f"- Configured: `{evidence['kite']['configured']}`",
        "",
        _probe_table(evidence["kite"]["probes"]),
        "",
    ]
    return "\n".join(lines)


def _probe_table(probes: list[dict[str, Any]]) -> str:
    """Render a list of probe dicts as a markdown table."""
    rows = [
        "| Probe | Status | Verdict | Candles | First ts | Last ts |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for p in probes:
        rows.append(
            f"| {p['name']} | {p['http_status']} | {p['verdict']} "
            f"| {p['candle_count']} | {p['first_ts']} | {p['last_ts']} |"
        )
    return "\n".join(rows)


def write_evidence(path: str, evidence: dict[str, Any], redactor: Redactor) -> None:
    """Write the evidence to ``path`` as JSON or markdown (by extension).

    The whole serialised document is passed through the redactor once more as a
    final safety net before it touches disk.
    """
    if path.lower().endswith(".md"):
        body = _evidence_markdown(evidence)
    else:
        body = json.dumps(evidence, indent=2, sort_keys=True)
    body = redactor.scrub(body)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)
        handle.write("\n")
    _log(f"[evidence] written to {path}")


def print_human_summary(evidence: dict[str, Any]) -> None:
    """Print a compact, secret-free human summary to stdout."""
    _emit("\n=== BruteX feed-probe summary ===")
    _emit(f"calls issued: {evidence['calls_issued']}")
    for vendor in ("dhan", "kite"):
        _emit(f"\n[{vendor}] configured={evidence[vendor]['configured']}")
        for p in evidence[vendor]["probes"]:
            detail = ""
            if p["candle_count"]:
                detail = f"  {p['candle_count']} candles {p['first_ts']} .. {p['last_ts']}"
            _emit(f"  - {p['name']}: {p['verdict']}{detail}")


# --------------------------------------------------------------------------- #
# Dry-run plan
# --------------------------------------------------------------------------- #


def print_plan() -> None:
    """Print the planned calls WITHOUT reading creds or making any network call."""
    recent_from, recent_to = _recent_window()
    _emit("=== BruteX feed-probe PLAN (dry-run: no creds read, no network) ===")
    _emit("\nDhan (read-only; POST /charts/intraday, IDX_I/INDEX, interval=1):")
    _emit(f"  0. GET  {DHAN_SCRIP_MASTER_CSV}  (public, no auth; resolve securityIds)")
    confirm_pairs = ", ".join(f"{name}={sid}" for name, sid in _confirm_expected_ids())
    _emit(
        "  0a. (offline, no extra call) CONFIRM hardcoded engine securityIds "
        f"[{confirm_pairs}] against the CSV -> CONFIRMED/MISMATCH/NOT-FOUND "
        "(or UNCONFIRMED-CSV-UNREACHABLE); skip with --no-scrip-confirm"
    )
    _emit(
        "  0b. auth: DHAN_ACCESS_TOKEN REQUIRED (SSM-read; TickVault owns the "
        "lifecycle) -- this probe NEVER mints; absent -> intraday probes SKIPPED"
    )
    for name, sid, conf in DHAN_INDICES:
        _emit(f"  * {name} (fallback securityId={sid} [{conf}]):")
        for label, frm, to in (("recent", recent_from, recent_to), *DHAN_FLOOR_ANCHORS):
            _emit(f"      POST /charts/intraday  {frm}..{to}  [{label}]")
    opt_from, opt_to = _options_window()
    _emit(
        "\nDhan expired-options (read-only; POST /charts/rollingoption, "
        f"interval={DHAN_ROLLINGOPTION_INTERVAL}, expiryFlag={DHAN_ROLLINGOPTION_EXPIRY_FLAG}, "
        f"expiryCode={DHAN_ROLLINGOPTION_EXPIRY_CODE}, strike={DHAN_ROLLINGOPTION_STRIKE}, "
        f"drvOptionType={DHAN_ROLLINGOPTION_OPTION_TYPE}); skip with --no-rollingoption:"
    )
    _emit(f"      requiredData={list(DHAN_ROLLINGOPTION_REQUIRED)}")
    for idx_name, segment, instrument_type, label in DHAN_ROLLINGOPTION_CASES:
        _emit(
            f"  * {label}: exchangeSegment={segment} instrument={instrument_type} "
            f"(underlying={idx_name})  {opt_from}..{opt_to}"
        )
    _emit("\nKite (read-only; GET /instruments/historical/{token}/minute):")
    kite_plan = [
        ("NIFTY 50 (control)", 256265, "recent"),
        ("SENSEX (BSE risk row)", 265, "recent"),
        ("INDIA VIX", 264969, "recent"),
        ("INDIA VIX (depth)", 264969, "2015-01"),
        ("BANKNIFTY (depth)", 260105, "2015-01"),
    ]
    for name, tok, when in kite_plan:
        _emit(f"  * {name}: GET /instruments/historical/{tok}/minute  [{when}]")
    _emit(
        "\nPacing: <=5 r/s Dhan, <=3 r/s Kite. Total call budget: "
        f"{DEFAULT_CALL_BUDGET}. No order/trade/modify endpoint is ever hit."
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the argparse CLI for the probe kit."""
    parser = argparse.ArgumentParser(
        prog="probe_feeds.py",
        description="Read-only Phase-0 probe for DhanHQ v2 + Zerodha Kite v3.",
    )
    parser.add_argument(
        "--evidence-out",
        default="./feed-probe-evidence.json",
        help="Evidence output path (.json or .md by extension). "
        "Default: ./feed-probe-evidence.json",
    )
    parser.add_argument("--dhan", action="store_true", help="Run only the Dhan probes.")
    parser.add_argument("--kite", action="store_true", help="Run only the Kite probes.")
    parser.add_argument(
        "--all", action="store_true", help="Run both vendors (default when none selected)."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned calls and exit -- reads NO env vars and makes NO network request.",
    )
    parser.add_argument(
        "--no-rollingoption",
        action="store_true",
        help="Skip the Dhan expired-options /charts/rollingoption capability probes "
        "(they run by default as part of the Dhan run).",
    )
    parser.add_argument(
        "--no-scrip-confirm",
        action="store_true",
        help="Skip the Dhan scrip-master ID-CONFIRM step that cross-checks the three "
        "hardcoded engine securityIds (NIFTY=13/BANKNIFTY=25/SENSEX=51) against the "
        "public CSV (it needs no creds and runs by default).",
    )
    parser.add_argument(
        "--floor-edge",
        action="store_true",
        help="Run the DEC-DHAN-ROLLING-FLOOR-001 floor-edge stage (~13 extra Dhan "
        "calls: NIFTY day-resolution march around today-5y, per-index confirm, "
        "the straddle silent-truncation detector, one 91-day over-cap request, "
        "one IDX_I volume/OI literal capture). Off by default.",
    )
    parser.add_argument(
        "--call-budget",
        type=int,
        default=DEFAULT_CALL_BUDGET,
        help=f"Hard ceiling on total network calls (default {DEFAULT_CALL_BUDGET}).",
    )
    return parser


def _vendor_error_record(vendor: str, exc: Exception, redactor: Redactor) -> ProbeRecord:
    """Wrap an unexpected vendor-run exception as a redacted evidence row.

    A vendor run must never abort the whole probe: a crash in one vendor is
    recorded here (message scrubbed of any registered secret) so the other
    vendor's + all prior results are still assembled and written to disk.
    """
    return ProbeRecord(
        vendor=vendor,
        name="run",
        endpoint="(vendor run)",
        request_summary={"reason": "unexpected vendor-run error"},
        http_status=None,
        verdict="UNEXPECTED_ERROR (vendor run aborted; other results preserved)",
        notes=[redactor.scrub(f"{type(exc).__name__}: {exc}")],
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code (0 on success)."""
    args = build_arg_parser().parse_args(argv)

    if args.dry_run:
        print_plan()
        return 0

    run_dhan_flag = args.dhan or args.all or not (args.dhan or args.kite)
    run_kite_flag = args.kite or args.all or not (args.dhan or args.kite)

    redactor = Redactor()
    budget = CallBudget(args.call_budget)
    dhan_records: list[ProbeRecord] = []
    kite_records: list[ProbeRecord] = []
    resolved: dict[str, Any] = {}

    # Each vendor is isolated: an unexpected crash in one is recorded and the
    # evidence file is still written with whatever was collected (never lost).
    if run_dhan_flag:
        try:
            dhan_records, resolved = run_dhan(
                redactor,
                budget,
                run_rollingoption=not args.no_rollingoption,
                run_scrip_confirm=not args.no_scrip_confirm,
            )
            if args.floor_edge:
                token = os.environ.get("DHAN_ACCESS_TOKEN")
                client_id = os.environ.get("DHAN_CLIENT_ID")
                if token:
                    _log("[dhan] --floor-edge stage (DEC-DHAN-ROLLING-FLOOR-001)…")
                    dhan_records.extend(
                        run_dhan_floor_edge(token, client_id, resolved, redactor, budget)
                    )
                else:
                    _log("[dhan] --floor-edge requested but no DHAN_ACCESS_TOKEN — skipped.")
                    dhan_records.append(
                        ProbeRecord(
                            vendor="dhan",
                            name="floor-edge",
                            endpoint="(skipped)",
                            request_summary={"reason": "no access token"},
                            http_status=None,
                            verdict="SKIPPED (no token)",
                        )
                    )
        except Exception as exc:
            dhan_records.append(_vendor_error_record("dhan", exc, redactor))
    if run_kite_flag:
        try:
            kite_records = run_kite(redactor, budget)
        except Exception as exc:
            kite_records.append(_vendor_error_record("kite", exc, redactor))

    evidence = build_evidence(dhan_records, resolved, kite_records, budget)
    write_evidence(args.evidence_out, evidence, redactor)
    print_human_summary(evidence)
    return 0


# --------------------------------------------------------------------------- #
# HARD REQUIREMENTS -> enforcement map (self-audit; see feed-probe.md).
#
#   1. No secret emitted     -> Redactor.register/scrub; creds read
#                               only from os.environ; write_evidence final scrub.
#   2. Read-only             -> only fetch_scrip_master (GET), dhan_intraday_probe
#                               + dhan_rollingoption_probe (POST /charts read),
#                               and kite_historical_probe (GET). NO token mint
#                               (generateAccessToken) and no order/trade/modify
#                               surface -- the Dhan access token MUST be provided
#                               (SSM-read; never minted here).
#                               confirm_dhan_security_ids issues NO call (reuses the
#                               one scrip-master GET) -- goal g, needs no creds.
#   3. Rate-safe + budgeted  -> Pacer (5 r/s Dhan, 3 r/s Kite), CallBudget,
#                               single 429 backoff, >=2 auth-failure short-circuit.
#   4. Zero pip-install      -> stdlib imports only; no brutex import anywhere.
#   5. Committable evidence  -> build_evidence / write_evidence (JSON or MD),
#                               per-probe redacted summary + status + count +
#                               first/last ts + verdict; print_human_summary.
#   6. Independently skippable-> run_dhan / run_kite each self-gate on their own
#                               creds and emit SKIPPED records, never crashing.
#   7. Exact doc'd shapes    -> dhan_intraday_probe body fields + IDX_I/INDEX;
#                               dhan_rollingoption_probe body copied field-for-field
#                               from the SDK expired_options_data (incl. the SDK's
#                               body-injected dhanClientId);
#                               kite GET path + Authorization/X-Kite-Version
#                               headers; ids from the live scrip master w/ fallback;
#                               confirm_dhan_security_ids reads the documented compact
#                               columns (SEM_SMST_SECURITY_ID / SEM_INSTRUMENT_NAME /
#                               SEM_TRADING_SYMBOL) tolerantly (goal g).
#   8. Runbook               -> feed-probe.md (operator-facing).
# --------------------------------------------------------------------------- #


if __name__ == "__main__":
    raise SystemExit(main())
