"""Dhan client error model — neutral exceptions + retry classification.

DEC-DHAN-SPOT-PULL-001 (ADR-0022). Unlike Groww's typed SDK exception tree, the
``dhanhq`` SDK's ``dhan_http.py`` returns a FLAT dict on failure — a single broad
``except Exception`` maps every non-2xx to ``{"status": "failure", "remarks":
{...}}`` (VERIFIED-SDK: ``dhan_http.py`` ``_parse_response``). There are NO typed
exceptions, NO 429/401 branches, NO retry/backoff — BruteX owns all of that.

**Remark-key spellings (BOTH accepted).** The live SDK envelope spells the remark
keys **snake_case** — ``{'error_code': json.get('errorCode'), 'error_type': …,
'error_message': …}`` (VERIFIED-SDK: ``dhan_http.py`` ``_parse_response``) — while
the docs' raw-HTTP error shape is camelCase (``errorCode``/``errorType``/
``errorMessage``, mirror ``01-introduction.md``). :func:`_remark_field` normalises
BOTH so the code arm can never be blinded by a spelling (the #1631
fixture-only-blindspot class — pre-fix, an all-camelCase classifier read empty
text against the real SDK and every failure fell to the terminal base, leaving the
DH-901 SSM re-read self-heal production-dead). On an SDK transport exception
``remarks`` is a plain STRING — the client tolerates that (non-``Mapping`` remarks
classify as the terminal base).

This module maps that flat envelope into a small neutral hierarchy the rest of
BruteX reasons about (the analogue of Groww's ``from_groww_exception``):

* :func:`classify_dhan_failure` — a ``remarks`` dict → the neutral
  :class:`DhanError` subclass, by the DOCUMENTED code table FIRST
  (``_CODE_TO_ERROR`` — the full annexure map, numeric Data-API 800-814 +
  trading DH-9xx; VERIFIED-docs(mirror): 20-annexure.md), then the conservative
  phrase heuristics as FALLBACK (a code-less throttle / empty-body text).
* :func:`from_dhan_exception` — a stdlib exception (a freeze-guard
  :class:`TimeoutError` on a hung SSM re-read, a decode error) → the neutral
  retryable class.

:func:`is_retryable` is the single source of truth the retry policy consults
(CLAUDE.md §11 COMMON); :func:`is_terminal_pull_stop` is the pull-wide global-stop
carve-out (a terminal auth / subscription / still-stale-token failure stops the
WHOLE pull instead of burning every remaining window — the Groww
``_GLOBAL_STOP_ERRORS`` mirror). :class:`DhanError` is a
:class:`~brutex.models.BruteXError` (hence a :class:`BaseException` subclass), so
it satisfies the :class:`~brutex.feeds.binding.FeedErrorPolicy` ``terminal_base``
contract and the ingest spine's per-symbol quarantine gate.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from brutex.adapters.dhan.constants import (
    DHAN_ERR_AUTHORIZATION,
    DHAN_ERR_DATA_FETCH,
    DHAN_ERR_INPUT_EXCEPTION,
    DHAN_ERR_INTERNAL_SERVER,
    DHAN_ERR_INVALID_AUTH,
    DHAN_ERR_NETWORK,
    DHAN_ERR_NUM_AUTH_FAILED,
    DHAN_ERR_NUM_CLIENT_ID_INVALID,
    DHAN_ERR_NUM_INSTRUMENT_LIMIT,
    DHAN_ERR_NUM_INTERNAL,
    DHAN_ERR_NUM_INVALID_DATE,
    DHAN_ERR_NUM_INVALID_EXPIRY,
    DHAN_ERR_NUM_INVALID_REQUEST,
    DHAN_ERR_NUM_INVALID_SECURITY_ID,
    DHAN_ERR_NUM_NOT_SUBSCRIBED,
    DHAN_ERR_NUM_RATE_LIMIT,
    DHAN_ERR_NUM_TOKEN_EXPIRED,
    DHAN_ERR_NUM_TOKEN_INVALID,
    DHAN_ERR_RATE_LIMIT,
)
from brutex.models import BruteXError


class DhanError(BruteXError):
    """Base class for every Dhan adapter failure (terminal unless a subclass)."""


class DhanAuthError(DhanError):
    """Authentication failure — DH-901 ``Invalid_Authentication`` (triggers a RE-READ).

    A bad / expired 24-h JWT. On a read-only host BruteX NEVER mints — the reauth
    path RE-READS the shared SSM SecureString to adopt the minter's fresh token
    (:class:`~brutex.adapters.dhan.reauth.DhanSdkRefresher`), never a local mint.
    A NON-retryable :class:`DhanError` at the client layer (it drives a re-read,
    not a blind fetch retry); the retryable :class:`DhanTokenStillStaleError` is
    raised only when the re-read still holds the dead token.
    """


class DhanSubscriptionError(DhanError):
    """806 / DH-902 subscription class — TERMINAL + a pull-wide stop.

    Dhan's historical/intraday endpoints require an ACTIVE Data API subscription
    (₹499/mo or the 25-trades-in-30d waiver); without it the call returns 806
    "Data APIs not subscribed" / DH-902 "User has not subscribed to Data APIs…"
    (VERIFIED-docs(mirror): 20-annexure.md). NOT retryable — the fix is to activate
    the subscription on web.dhan.co, not to re-issue the request — and since EVERY
    later window of the same pull would fail identically, it is a
    :func:`is_terminal_pull_stop` global stop (never a fabricated bar).

    RELABEL note (DEC-DHAN-ROLLING-FLOOR-001 review, A-MINOR-2): this class
    previously claimed ``DH-905``, which the annexure documents as the INPUT
    exception (a :class:`DhanBadRequestError`), not subscription.
    """


class DhanThrottleError(DhanError):
    """Rate-limit / throttle — RETRYABLE (the limiter should back off).

    Dhan caps the Data API at 5 req/s + 100k/day. Recognised by CODE — the Data-API
    numeric ``805`` "too many requests/connections" + the trading-side ``DH-904``
    (VERIFIED-docs(mirror): 20-annexure.md) — with the conservative ``errorType`` /
    message phrase heuristic kept as FALLBACK for a code-less throttle body. The
    HTTP status pairing (429?) stays UNVERIFIED (probe gate G-E). Feeds the AIMD
    ladder (:func:`is_rate_limit` is ``True``).
    """


class DhanBadRequestError(DhanError):
    """A documented bad-request class — TERMINAL, never retried.

    The Data-API numeric ``804`` (instruments-per-request limit) / ``811`` (invalid
    expiry date) / ``812`` (invalid date format) / ``813`` (invalid SecurityId) /
    ``814`` (invalid request) + the trading-side ``DH-905`` (input exception) /
    ``DH-907`` (data-fetch error, bad params) — VERIFIED-docs(mirror):
    20-annexure.md. The request itself is wrong; re-issuing it can never succeed,
    so the window quarantines FAILED (re-pullable after the operator fixes the
    input) with no blind retry (the Groww ``GrowwBadRequestError`` posture, #883).
    """


class DhanServerError(DhanError):
    """A transient vendor-side server failure — RETRYABLE, NOT a rate-limit.

    The Data-API numeric ``800`` (internal server error) + the trading-side
    ``DH-908`` (internal server error) / ``DH-909`` (network error) —
    VERIFIED-docs(mirror): 20-annexure.md. A 5xx-class blip is no quota signal, so
    it rides the GENTLE timeout ladder (:func:`steps_timeout_ladder` ``True``) and
    never steps the per-second rate ceiling down (the Groww ``GrowwServerError``
    posture, DEC-GROWW-SERVER-ERROR-RETRY-001 #883).
    """


class DhanEmptyResponseError(DhanError):
    """Empty / non-JSON vendor body under a transient throttle — RETRYABLE.

    A blank / garbled body decodes to nothing; treated as a transient blip (the
    in-call fast-retry re-issues it; a residual is re-pulled by the until-clean
    loop). NOT a quota signal, so it rides the gentle timeout ladder, never stepping
    the per-second rate ceiling down.
    """


class DhanTimeoutError(DhanError):
    """Network read/connect timeout — RETRYABLE (the freeze-guard bound).

    The ``dhanhq`` SDK carries no inherent wall-clock bound, so a hung SSM re-read
    or a stalled transport is bounded by :func:`~brutex.config.deadline.run_with_deadline`;
    the resulting stdlib :class:`TimeoutError` is mapped here (:func:`from_dhan_exception`)
    so a stall is re-pulled, never a permanent freeze. Rides the timeout ladder, not
    the rate ladder (a stall is not a quota signal).
    """


class DhanVendorDriftError(DhanError):
    """Response-shape drift — parallel column arrays of UNEQUAL length — TERMINAL-per-chunk.

    Dhan returns OHLCV as parallel COLUMN arrays (``open[] high[] low[] close[]
    volume[] timestamp[]``) and the SDK does ZERO validation. A length mismatch
    means the vendor payload is internally inconsistent — zipping it would silently
    mis-pair prices with timestamps (a Golden-Rule-#1 corruption), so the chunk is
    REJECTED (never partially stored) and re-pulled. NOT a per-account failure, so
    it rides the per-window quarantine, not a pull-wide halt.
    """


class DhanTokenReadOnlyError(DhanAuthError):
    """A token WRITE was attempted while the read-only Dhan token store is active.

    BruteX is a strictly READ-ONLY consumer of the shared Dhan access token (a
    dedicated Lambda / TickVault mints it into AWS SSM). A ``store`` call means the
    caller tried to MINT — forbidden on a read-only host (a stray write could
    invalidate the token TickVault is actively using). This fails LOUD (a terminal
    :class:`DhanAuthError`) instead of a silent local write; the operator fix is to
    deploy / run the minter.
    """


class DhanTokenStillStaleError(DhanError):
    """A read-only RE-READ returned the SAME dead token (or an SSM miss) — a pull-wide stop.

    On a mid-run DH-901 the reauth path RE-READS the shared SSM token to adopt the
    minter's fresh one. If the re-read returns the SAME token that just failed auth
    — the minter has not rotated it yet — or an SSM miss, adopting it would loop the
    dead token; this is raised INSTEAD. It reaching the SPINE means the re-read
    self-heal has nothing fresher to read, so NOTHING else in THIS pull can succeed
    — it is a :func:`is_terminal_pull_stop` global stop (the whole pull halts
    loudly; the NEXT pull converges once the minter rotates the SSM token). This
    SUPERSEDES the original per-window-quarantine wording. ``is_retryable`` stays
    ``True`` for the IN-WINDOW retry semantics (the client's bounded fast-retry),
    which is now unreachable past the spine carve-out — documented, not removed. A
    read that returns a NEW token is adopted with no error, so this never fires on
    the healthy rotation path.
    """


#: Message / ``errorType`` PHRASES (lower-cased) that mark a RETRYABLE throttle when
#: the SDK returns a failure with no recognised ``errorCode``. The exact Dhan
#: throttle ``errorCode`` is UNVERIFIED (the annexure is host-blocked), so this is a
#: CONSERVATIVE heuristic (Golden Rule #1) — narrow phrase-only, never a bare number.
_THROTTLE_HINTS: tuple[str, ...] = (
    "rate limit",
    "rate-limit",
    "too many request",
    "throttle",
    "rate_limit",
)

#: ``errorType`` / message signatures for an empty / non-JSON body (a transient blip).
_EMPTY_BODY_HINTS: tuple[str, ...] = (
    "empty response",
    "no data",
    "expecting value",
)


def _remark_field(remarks: Mapping[str, object], camel: str, snake: str) -> object | None:
    """Return the remark field under EITHER key spelling (camelCase, then snake_case).

    The live ``dhanhq`` SDK failure envelope is snake_case (``dhan_http.py``
    ``_parse_response`` builds ``{'error_code': json.get('errorCode'), …}`` —
    VERIFIED-SDK); the docs' raw-HTTP shape is camelCase. A real payload carries one
    spelling or the other, so first-non-``None`` wins; both the code arm and the
    phrase-heuristic text read through this ONE helper (CLAUDE.md §11 COMMON) so a
    spelling can never blind the classifier (the #1631 fixture-only-blindspot class).
    """
    value = remarks.get(camel)
    if value is None:
        value = remarks.get(snake)
    return value


def _remark_text(remarks: Mapping[str, object]) -> str:
    """Lower-cased join of ``errorType`` + ``errorMessage`` (both spellings) for phrase matching."""
    error_type = str(_remark_field(remarks, "errorType", "error_type") or "")
    error_message = str(_remark_field(remarks, "errorMessage", "error_message") or "")
    return f"{error_type} {error_message}".casefold()


def _subscription_error(code: str, message: str) -> DhanError:
    """806 / DH-902 — the honest refusal naming CAUSE + FIX (never a blind retry)."""
    return DhanSubscriptionError(
        f"{code}: Dhan Data-API subscription inactive (₹499/mo or 25-trades-in-30d "
        f"waiver) — subscribe on web.dhan.co, then re-pull. No data was fetched. "
        f"Vendor message: {message}"
    )


def _token_expired_error(code: str, message: str) -> DhanError:
    """807 — access token expired (drives the read-only SSM re-read self-heal)."""
    return DhanAuthError(f"{code}: access token expired — {message}")


def _client_id_error(code: str, message: str) -> DhanError:
    """810 — the CLIENT ID is invalid (a re-read is harmless; a same-dead-token
    re-read escalates to :class:`DhanTokenStillStaleError` → the pull-wide stop)."""
    return DhanAuthError(f"{code}: client id invalid — {message}")


def _auth_error(code: str, message: str) -> DhanError:
    """DH-901 / 808 / 809 — the generic auth-failure arm (drives the SSM re-read)."""
    return DhanAuthError(f"{code}: {message}")


def _security_id_error(code: str, message: str) -> DhanError:
    """813 — invalid SecurityId: name the hardcoded map + the probe gate."""
    return DhanBadRequestError(
        f"{code}: invalid SecurityId — verify the hardcoded map "
        f"(constants.DHAN_INDEX_SECURITY_IDS) against the scrip-master CSV "
        f"(probe gate G-C): {message!r}"
    )


def _bad_request_error(code: str, message: str) -> DhanError:
    """804 / 811 / 812 / 814 / DH-905 / DH-907 — terminal bad request."""
    return DhanBadRequestError(f"{code}: {message}")


def _throttle_error(code: str, message: str) -> DhanError:
    """805 / DH-904 — the documented rate-limit codes (HTTP pairing UNVERIFIED, G-E)."""
    return DhanThrottleError(f"{code}: {message}")


def _server_error(code: str, message: str) -> DhanError:
    """800 / DH-908 / DH-909 — transient server-side failure (retryable, not rate-limit)."""
    return DhanServerError(f"{code}: {message}")


#: The documented code table (VERIFIED-docs(mirror): 20-annexure.md ⇐
#: dhanhq.co/docs/v2/annexure/), consulted FIRST — every row cites
#: ``dhan_api_research.md §6``. Codes are normalised ``str(code).strip().upper()``
#: (a numeric Data-API code may arrive as ``int``).
_CODE_TO_ERROR: Mapping[str, Callable[[str, str], DhanError]] = {
    # Rate limit → retryable + is_rate_limit (feeds the AIMD ladder).
    DHAN_ERR_NUM_RATE_LIMIT: _throttle_error,
    DHAN_ERR_RATE_LIMIT: _throttle_error,
    # Subscription → TERMINAL + pull-wide stop, honest cause+fix message.
    DHAN_ERR_NUM_NOT_SUBSCRIBED: _subscription_error,
    DHAN_ERR_AUTHORIZATION: _subscription_error,
    # Auth family → drives the single-flight read-only SSM re-read (client._call).
    DHAN_ERR_NUM_TOKEN_EXPIRED: _token_expired_error,
    DHAN_ERR_NUM_AUTH_FAILED: _auth_error,
    DHAN_ERR_NUM_TOKEN_INVALID: _auth_error,
    DHAN_ERR_NUM_CLIENT_ID_INVALID: _client_id_error,
    DHAN_ERR_INVALID_AUTH: _auth_error,
    # Bad request → TERMINAL, never retried (813 names the securityId map).
    DHAN_ERR_NUM_INSTRUMENT_LIMIT: _bad_request_error,
    DHAN_ERR_NUM_INVALID_EXPIRY: _bad_request_error,
    DHAN_ERR_NUM_INVALID_DATE: _bad_request_error,
    DHAN_ERR_NUM_INVALID_SECURITY_ID: _security_id_error,
    DHAN_ERR_NUM_INVALID_REQUEST: _bad_request_error,
    DHAN_ERR_INPUT_EXCEPTION: _bad_request_error,
    DHAN_ERR_DATA_FETCH: _bad_request_error,
    # Server-side → retryable, NOT rate-limit, NOT auth (the Groww 5xx posture).
    DHAN_ERR_NUM_INTERNAL: _server_error,
    DHAN_ERR_INTERNAL_SERVER: _server_error,
    DHAN_ERR_NETWORK: _server_error,
}


def classify_dhan_failure(remarks: Mapping[str, object]) -> DhanError:
    """Map a Dhan ``remarks`` failure dict to a neutral :class:`DhanError`.

    Classification is by the DOCUMENTED code table FIRST (:data:`_CODE_TO_ERROR` —
    the reliable structured signal; both camelCase and snake_case remark-key
    spellings accepted via :func:`_remark_field`), then the CONSERVATIVE throttle /
    empty-body phrase heuristics over the ``errorType`` / ``errorMessage`` text as
    FALLBACK (a code-less body). Anything unrecognised stays the terminal base
    :class:`DhanError` (an unknown failure is NEVER blindly retried — Golden Rule #1).

    Parameters
    ----------
    remarks
        The ``remarks`` sub-dict of the SDK failure envelope — snake_case
        (``{"error_code", "error_type", "error_message"}`` — the live SDK shape) or
        camelCase (``{"errorCode", "errorType", "errorMessage"}`` — the raw-HTTP
        docs shape); missing keys tolerated.
    """
    code_raw = _remark_field(remarks, "errorCode", "error_code")
    code = str(code_raw).strip().upper() if code_raw is not None else ""
    message_raw = _remark_field(remarks, "errorMessage", "error_message")
    if message_raw is None:
        message_raw = _remark_field(remarks, "errorType", "error_type")
    message = str(message_raw) if message_raw is not None else ""
    builder = _CODE_TO_ERROR.get(code)
    if builder is not None:
        return builder(code, message)
    text = _remark_text(remarks)
    if any(hint in text for hint in _THROTTLE_HINTS):
        return DhanThrottleError(f"{code or 'throttle'}: {message}")
    if any(hint in text for hint in _EMPTY_BODY_HINTS):
        return DhanEmptyResponseError(f"{code or 'empty'}: {message}")
    return DhanError(f"{code or 'DH-UNKNOWN'}: {message}")


def from_dhan_exception(exc: BaseException) -> DhanError:
    """Map a stdlib transport exception to a neutral retryable :class:`DhanError`.

    Used at the freeze-guard boundary: a hung SSM re-read / transport stall raises a
    stdlib :class:`TimeoutError` bounded by
    :func:`~brutex.config.deadline.run_with_deadline`, which is mapped to a RETRYABLE
    :class:`DhanTimeoutError` (a stall is transient — re-pull, never a permanent
    freeze). Any other exception maps to the terminal base :class:`DhanError`
    (conservative — never blindly retried; Golden Rule #1).
    """
    if isinstance(exc, TimeoutError):
        return DhanTimeoutError(f"{type(exc).__name__}: {exc}")
    return DhanError(f"{type(exc).__name__}: {exc}")


def is_rate_limit(exc: DhanError) -> bool:
    """True if *exc* is a rate-limit / throttle error (the limiter should back off)."""
    return isinstance(exc, DhanThrottleError)


def is_retryable(exc: DhanError) -> bool:
    """True if *exc* is transient and the orchestrator should retry it.

    Retryable: throttle (:class:`DhanThrottleError`), empty/non-JSON body
    (:class:`DhanEmptyResponseError`), timeout (:class:`DhanTimeoutError`), a
    transient server-side failure (:class:`DhanServerError` — 800/DH-908/DH-909,
    the Groww 5xx posture), and a read-only re-read that still holds the SAME dead
    token (:class:`DhanTokenStillStaleError` — the IN-WINDOW semantics; past the
    spine's :func:`is_terminal_pull_stop` carve-out it stops the whole pull).
    Terminal: the auth family (needs a re-read, not a blind retry — incl. the
    read-only :class:`DhanTokenReadOnlyError`), 806/DH-902 subscription, the
    804/811-814/DH-905/DH-907 bad-request class, vendor drift, and any bare
    :class:`DhanError` (an unknown failure is never retried — Golden Rule #1).
    """
    return isinstance(
        exc,
        (
            DhanThrottleError,
            DhanEmptyResponseError,
            DhanTimeoutError,
            DhanServerError,
            DhanTokenStillStaleError,
        ),
    )


def is_auth_failure(exc: DhanError) -> bool:
    """True if *exc* means Dhan auth failed (drives a re-read, not a blind retry)."""
    return isinstance(exc, DhanAuthError)


def is_terminal_pull_stop(exc: BaseException) -> bool:
    """True iff *exc* must stop the WHOLE Dhan pull — never burn the remaining windows.

    The Dhan mirror of the Groww spines' ``_GLOBAL_STOP_ERRORS`` carve-out
    (DEC-DHAN-ROLLING-FLOOR-001 delta, design §3). Per class:

    * :class:`DhanAuthError` reaching the SPINE means the client's ``_call`` already
      exhausted its bounded re-read budget — the credential is dead for every later
      window too (covers the read-only :class:`DhanTokenReadOnlyError` subclass).
    * :class:`DhanTokenStillStaleError` means the SSM re-read returned the SAME dead
      token — nothing else in THIS pull can succeed until the minter rotates it.
    * :class:`DhanSubscriptionError` is vendor-terminal (806/DH-902 — the Data-API
      subscription itself is inactive; every request fails identically).

    Consumed by the spine's ``except DhanError`` carve-out (the FIRST occurrence
    re-raises past the per-window quarantine) and by
    :func:`brutex.dashboard.pull_jobs.is_pull_halt` (the job layer's CRITICAL
    Telegram pull-halt alert), so a terminal failure is LOUD, never a silent
    all-windows-FAILED report. Takes :class:`BaseException` — a non-Dhan exception
    honestly reads ``False`` (the Groww verdicts are byte-identical, pinned).
    """
    return isinstance(exc, (DhanAuthError, DhanSubscriptionError, DhanTokenStillStaleError))


def steps_timeout_ladder(exc: DhanError) -> bool:
    """True iff *exc* may step the AIMD TIMEOUT ladder down — a server-stress signal only.

    A retryable NON-rate-limit, NON-auth-family transport failure (timeout / empty
    body / vendor stall) is server-stress evidence and may ease the pull rate; a
    rate-limit keeps its own ``on_rate_limit`` path, and an auth-family stall (a
    token race) says nothing about candle-endpoint health, so it must NOT ease the
    rate (the Groww ``steps_timeout_ladder`` precedent, CLAUDE.md §11 COMMON).
    """
    return is_retryable(exc) and not is_rate_limit(exc) and not is_auth_failure(exc)


__all__ = [
    "DhanAuthError",
    "DhanBadRequestError",
    "DhanEmptyResponseError",
    "DhanError",
    "DhanServerError",
    "DhanSubscriptionError",
    "DhanThrottleError",
    "DhanTimeoutError",
    "DhanTokenReadOnlyError",
    "DhanTokenStillStaleError",
    "DhanVendorDriftError",
    "classify_dhan_failure",
    "from_dhan_exception",
    "is_auth_failure",
    "is_rate_limit",
    "is_retryable",
    "is_terminal_pull_stop",
    "steps_timeout_ladder",
]
