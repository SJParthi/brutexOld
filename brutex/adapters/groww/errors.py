"""Groww client error model — neutral exceptions + retry classification.

The Groww SDK raises a typed exception hierarchy (``growwapi.groww.exceptions``,
``docs/groww/api-reference/05-exceptions.md``)::

    GrowwBaseException
    ├── GrowwAPIException
    │   ├── GrowwAPIAuthenticationException
    │   ├── GrowwAPIAuthorisationException
    │   ├── GrowwAPIBadRequestException
    │   ├── GrowwAPINotFoundException
    │   ├── GrowwAPIRateLimitException
    │   └── GrowwAPITimeoutException
    └── GrowwFeedException
        ├── GrowwFeedConnectionException
        └── GrowwFeedNotSubscribedException

This module maps those into a small neutral hierarchy the rest of BruteX reasons
about — the analogue of an HTTP-status-mapping error layer.
Because the Groww SDK is an OPTIONAL extra (``DEC-GROWW-DEPS-REINSTATE-001``),
:func:`from_groww_exception` matches on the exception's **class name** (a string)
rather than importing the SDK classes, so this module imports + tests cleanly
without ``growwapi`` installed.

:func:`is_retryable` is the single source of truth the orchestrator's retry
policy consults (CLAUDE.md §11 COMMON). Rate-limit + timeout + empty-body +
transient 5xx server errors + transient network/DNS failures are retryable. The
Groww SDK has no distinct server-error CLASS (only a generic
``GrowwAPIException(msg, code)``), so a 5xx is mapped to the neutral
:class:`GrowwServerError` by its HTTP ``code`` (``500``/``502``/``503``) or an
unambiguous message signature — never by inventing an SDK class
(DEC-GROWW-SERVER-ERROR-RETRY-001; Golden Rule #1). A ``504`` gateway-timeout
stays a :class:`GrowwTimeoutError` (already retryable) so the gentle timeout
ladder, not a new path, governs it. A transient transport-layer failure (a DNS
outage like ``"Failed to resolve api.groww.in"``, a reset/refused/broken
connection — the ``requests`` / ``urllib3`` / :class:`socket.gaierror` classes)
is mapped to the neutral :class:`GrowwNetworkError` by the transport exception's
CLASS NAME (:data:`_NETWORK_ERROR_NAMES`) or an unambiguous message signature: the
request never reached Groww, so it is RETRYABLE, not a terminal quarantine
(DEC-GROWW-NETWORK-ERROR-RETRY-001). Like a 5xx, a network blip is NOT a quota
signal — it rides the gentle timeout ladder, never stepping the per-second rate
ceiling down (:func:`is_rate_limit` stays ``False`` for it).
"""

from __future__ import annotations

import json

from brutex.models import BruteXError


class GrowwError(BruteXError):
    """Base class for every Groww adapter failure (terminal unless a subclass)."""


class GrowwBadRequestError(GrowwError):
    """Malformed request parameters (terminal)."""


class GrowwAuthError(GrowwError):
    """Authentication / authorisation failure (triggers re-auth, not blind retry).

    Carries an optional :attr:`detail` — the SANITIZED vendor reason (the SDK
    exception's text with every secret scrubbed to ``***``) so a caller can read
    WHY auth failed beyond the user-facing summary, without ever exposing the
    api_key / TOTP secret / minted code (DEC-GROWW-AUTH-ERROR-DETAIL-001). It is
    ``None`` unless explicitly set by the auth layer.
    """

    detail: str | None = None


class GrowwTokenIssuanceThrottled(GrowwAuthError):
    """The Groww TOKEN-ISSUANCE endpoint throttled the mint — RETRYABLE, NOT terminal.

    Raised by :func:`~brutex.adapters.groww.auth.get_access_token` when the vendor
    rejects the access-token mint with a rate-limit signal (the SDK's
    ``GrowwAPIRateLimitException``, an HTTP ``429`` ``code``, or a "rate limit …
    exceeded" message). Distinct from a TERMINAL :class:`GrowwAuthError` (a wrong
    TOTP secret / expired key / un-whitelisted IP) so the cold-start mint can back
    off + retry (and reuse a same-session cached token) instead of failing the pull
    (DEC-GROWW-TOKEN-DISK-CACHE-001).

    It is a :class:`GrowwAuthError` SUBCLASS so every ``except GrowwAuthError`` site
    and :func:`is_auth_failure` keep matching it, BUT :func:`is_retryable` returns
    ``True`` for it (the ONLY auth-family error that is retryable), so the client's
    bounded retry / quarantine path treats a throttled mint as transient. Whether
    Groww documents a token-issuance rate limit is UNVERIFIED (the SDK reference's
    rate table covers only Orders / Live Data / Non-Trading — ``docs/groww/api-\
    reference/01-introduction.md:128-134``); the observed ``GrowwAPIRateLimitException``
    raised BY the issuance call is the runtime evidence a limit exists on that path.
    """


class GrowwTokenStillStaleError(GrowwAuthError):
    """A read-only broker RE-READ returned the SAME dead token — RETRYABLE, NOT terminal.

    On a read-only host (``BRUTEX_TOKEN_BROKER_READONLY``) BruteX never mints: when the
    shared daily token is rejected mid-run, the reauth path RE-READS the shared
    Parameter-Store SecureString to adopt the Lambda's freshly-minted token instead of
    minting locally (DEC-BROKER-READONLY-REAUTH-REREAD-001). But if the re-read returns the
    SAME token that just failed auth — the Lambda has not minted the new one yet (e.g.
    reauth fired at 05:59 IST, just before the ~06:05 mint) — or an SSM miss, adopting it
    would loop the dead token. This error is raised INSTEAD, mirroring
    :class:`GrowwTokenIssuanceThrottled` / the reauth-deadline transient
    (DEC-GROWW-REAUTH-DEADLINE-001): a :class:`GrowwAuthError` SUBCLASS (so every
    ``except GrowwAuthError`` site + :func:`is_auth_failure` keep matching it) that
    :func:`is_retryable` returns ``True`` for, so the caller's bounded retry / quarantine
    path re-pulls that window LATER (when the Lambda's fresh token is present) rather than
    crashing the process into a manual restart. A read that returns a NEW token is adopted
    immediately (no error), so this NEVER fires on the healthy rotation path.
    """


class GrowwCredentialDeadError(GrowwAuthError):
    """OUR credential / token-broker Lambda looks DEAD — TERMINAL + a pull-wide global stop.

    Raised by the error-storm breaker
    (:class:`~brutex.adapters.error_storm_breaker.ErrorStormBreaker`,
    DEC-VENDOR-DOWN-BREAKER-001), NEVER by the client/auth layer itself: when K
    CONSECUTIVE ladder-exhausted :class:`GrowwTokenStillStaleError` outcomes span
    ≥ T minutes with ZERO adopted-token successes in between, the "the Lambda mints
    a few minutes late" explanation is exhausted and the honest verdict is a dead
    credential / dead token-broker — an OPERATOR-action condition. Escalating to
    this class (a) stays a :class:`GrowwAuthError` subclass so every
    ``except GrowwAuthError`` site + :func:`is_auth_failure` keep matching, (b) is
    deliberately ABSENT from :func:`is_retryable`'s tuple so it is TERMINAL, and
    (c) joins BOTH ingest spines' ``_GLOBAL_STOP_ERRORS`` so it propagates past the
    per-symbol quarantine and halts the whole pull (every remaining window would
    fail identically against the same dead credential). The per-window StillStale
    outcomes themselves STAY retryable (DEC-BROKER-READONLY-REAUTH-REREAD-001 is
    untouched) — this class only names the ESCALATED verdict. Because it is raised
    by the breaker on the ingest worker (never from inside
    ``GrowwClient._call``), the client's bounded reauth branch is never re-entered
    by it. The K/T thresholds are operator-tunable HEURISTICS
    (:data:`~brutex.adapters.groww.constants.GROWW_CREDENTIAL_DEAD_STREAK` /
    :data:`~brutex.adapters.groww.constants.GROWW_CREDENTIAL_DEAD_MIN_MINUTES`),
    not vendor facts.
    """


class GrowwNotFoundError(GrowwError):
    """Unknown instrument / endpoint (terminal)."""


class GrowwRateLimitError(GrowwError):
    """Per-second or daily quota exceeded (back off + retry)."""


class GrowwTimeoutError(GrowwError):
    """Network read/connect timeout (retry)."""


class GrowwNetworkError(GrowwError):
    """Transient network / DNS connectivity failure — RETRYABLE.

    A momentary host-level connectivity blip — a DNS resolution failure
    (``"Failed to resolve api.groww.in"`` — :class:`socket.gaierror` /
    urllib3 ``NameResolutionError`` wrapped by ``requests.exceptions.\
    ConnectionError``), a dropped/reset connection (:class:`ConnectionResetError`),
    or a refused connect — is **not** a vendor-side rejection: the request never
    reached Groww, so re-issuing it once the host's DNS/network recovers is safe.

    The classifier previously had no network class, so such a failure fell to the
    terminal base :class:`GrowwError` (``is_retryable() == False``) and a whole
    contract was PERMANENTLY quarantined with no retry. A 28h-pull forensic found
    218 option contracts lost exactly this way during a transient
    ``"Failed to resolve api.groww.in"`` DNS outage
    (DEC-GROWW-NETWORK-ERROR-RETRY-001). This maps such a failure to a RETRYABLE
    error: the in-call fast-retry re-issues it and any residual is left UNMARKED so
    the until-clean / next-pass loop re-pulls it — never dropped, never faked.

    It is deliberately NOT a :class:`GrowwRateLimitError`: a DNS / connection blip
    is not a quota signal, so it must not step the per-second rate ladder down — it
    rides the gentler timeout ladder (:func:`is_rate_limit` stays ``False`` for it).
    A genuine ``404`` still maps to a terminal :class:`GrowwNotFoundError`, so this
    never makes a real "not on Groww" retryable.
    """


class GrowwServerError(GrowwError):
    """Transient vendor server error (HTTP 5xx) — RETRYABLE.

    The Groww SDK has no dedicated server-error subclass; a 5xx surfaces as a
    generic ``GrowwAPIException`` carrying a ``500``/``502``/``503`` ``code`` (or an
    "internal server error" / "bad gateway" / "service unavailable" message). The
    old classifier let that fall to the terminal base :class:`GrowwError`, so a
    momentary upstream blip TERMINALLY quarantined a real symbol instead of
    re-pulling it. This maps such a failure to a RETRYABLE error
    (DEC-GROWW-SERVER-ERROR-RETRY-001): the in-call fast-retry re-issues it and any
    residual is re-queued by the until-clean loop — never dropped, never faked.

    It is deliberately NOT a :class:`GrowwRateLimitError`: a 5xx is not a quota
    signal, so it must not step the per-second rate ladder down — it rides the
    gentler timeout ladder (:func:`is_rate_limit` stays ``False`` for it). A genuine
    ``404`` still maps to a terminal :class:`GrowwNotFoundError`, so this never
    makes a real "not on Groww" retryable.
    """


class GrowwEmptyResponseError(GrowwError):
    """Empty / non-JSON vendor body — a transient throttle, RETRYABLE.

    The Groww SDK decodes every REST body with ``response.json()``. Under a
    transient throttle the vendor returns an EMPTY (or HTML/garbled) body and the
    SDK's decode raises a :class:`json.JSONDecodeError` (a :class:`ValueError`
    subclass) whose message is ``"Expecting value: line 1 column 1 (char 0)"`` —
    NOT a real ``404``. The old classifier let that fall to the terminal base
    :class:`GrowwError`, so a live pull mis-saw an empty-body blip as "not on
    Groww" and TERMINALLY quarantined real F&O stocks (TITAN / SHRIRAMFIN).

    This maps it to a RETRYABLE error (DEC-GROWW-EMPTY-BODY-RETRY-001): the
    in-call fast-retry re-issues it, and any residual that exhausts the budget is
    re-queued by the until-clean loop — never dropped. A genuine missing symbol
    still returns a real ``404`` → :class:`GrowwNotFoundError` (stays terminal),
    so this never makes a real 404 retryable.
    """


class GrowwFeedError(GrowwError):
    """Live-feed (WebSocket) failure — OUT of Track-1 scope (terminal)."""


#: Groww SDK exception class-name → neutral GrowwError subclass. Keyed by NAME
#: (string) so the SDK need not be importable (the optional-extra resilience).
_SDK_NAME_TO_ERROR: dict[str, type[GrowwError]] = {
    "GrowwAPIAuthenticationException": GrowwAuthError,
    "GrowwAPIAuthorisationException": GrowwAuthError,
    "GrowwAPIBadRequestException": GrowwBadRequestError,
    "GrowwAPINotFoundException": GrowwNotFoundError,
    "GrowwAPIRateLimitException": GrowwRateLimitError,
    "GrowwAPITimeoutException": GrowwTimeoutError,
    "GrowwFeedException": GrowwFeedError,
    "GrowwFeedConnectionException": GrowwFeedError,
    "GrowwFeedNotSubscribedException": GrowwFeedError,
}

#: Transport-layer exception class-NAMES that mark a transient network / DNS blip —
#: a RETRYABLE :class:`GrowwNetworkError` (DEC-GROWW-NETWORK-ERROR-RETRY-001). The
#: Groww SDK rides ``requests`` / ``urllib3`` under the hood, so a host DNS outage
#: surfaces as one of these by CLASS NAME (matched as a string, like the SDK map
#: above, so neither ``requests`` nor ``urllib3`` need be importable — the
#: optional-extra resilience). The names cover the stdlib builtin + ``requests``
#: ``ConnectionError`` (which wraps a DNS / refused / reset connect; the live
#: ``"Failed to resolve api.groww.in"`` shape), ``urllib3`` ``NameResolutionError``
#: / ``NewConnectionError`` / ``MaxRetryError``, the builtin ``ConnectionResetError``
#: / ``ConnectionRefusedError`` subclasses, and ``socket.gaierror`` (the
#: ``"Name or service not known"`` resolver failure). Checked AFTER the typed Groww
#: SDK map so a vendor-typed error always wins; a bare ``GrowwAPIException`` is never
#: matched here (it is not a transport class).
_NETWORK_ERROR_NAMES: frozenset[str] = frozenset(
    {
        "ConnectionError",
        "ConnectionResetError",
        "ConnectionRefusedError",
        "NameResolutionError",
        "NewConnectionError",
        "MaxRetryError",
        "gaierror",
    }
)

#: Transport-layer exception class-NAMES that mark a socket CONNECT / READ TIMEOUT —
#: a RETRYABLE :class:`GrowwTimeoutError` (DEC-GROWW-HTTP-SOCKET-TIMEOUT-001). Once the
#: Groww SDK's ``requests`` calls carry a ``(connect, read)`` socket timeout
#: (:func:`~brutex.adapters.groww.http_timeout.apply_default_timeout`), a half-open / dead
#: socket raises ``requests.ConnectTimeout`` (a subclass of BOTH ``requests.ConnectionError``
#: AND ``requests.Timeout``) or ``requests.ReadTimeout`` (a subclass of ``requests.Timeout``)
#: — plus the base ``requests.Timeout`` / stdlib ``socket.timeout`` (aliased ``TimeoutError``,
#: whose class name is ``"timeout"`` on older builds). Matching by CLASS NAME (a string, like
#: the SDK + network maps) keeps classification INDEPENDENT of the ``requests`` message text
#: (which varies by version / pool wrapper) and needs neither ``requests`` nor ``urllib3``
#: importable (the optional-extra resilience). Checked AFTER the typed Groww SDK map (a
#: vendor-typed error always wins) but BEFORE :data:`_NETWORK_ERROR_NAMES`, so a
#: ``ConnectTimeout`` — which is ALSO a ``ConnectionError`` by inheritance but whose OWN class
#: name is ``"ConnectTimeout"``, not in the network set — is read as a TIMEOUT (the gentle
#: timeout ladder), never a plain network blip. A socket timeout is NOT a quota signal, so it
#: rides :class:`GrowwTimeoutError` (already in :func:`is_retryable`, NOT :func:`is_rate_limit`),
#: never stepping the per-second rate ceiling down.
_TIMEOUT_ERROR_NAMES: frozenset[str] = frozenset(
    {
        "ReadTimeout",
        "ConnectTimeout",
        "Timeout",
        "timeout",
    }
)


#: HTTP-ish status codes the SDK may carry on a GENERIC ``GrowwAPIException(msg,
#: code)`` when it does NOT raise the typed subclass — mapped to the retryable
#: neutral error. ``429`` = rate-limit; ``408``/``504`` = timeout; ``500``/``502``/
#: ``503`` = transient server error (DEC-GROWW-SERVER-ERROR-RETRY-001). ``504`` is a
#: gateway-timeout and intentionally stays a TimeoutError (already retryable — the
#: timeout ladder governs it, not a new path).
#: (``docs/groww/api-reference/05-exceptions.md``; the SDK's ``GrowwAPIException``
#: carries ``code``).
_CODE_TO_ERROR: dict[int, type[GrowwError]] = {
    429: GrowwRateLimitError,
    408: GrowwTimeoutError,
    500: GrowwServerError,
    502: GrowwServerError,
    503: GrowwServerError,
    504: GrowwTimeoutError,
}

#: Message PHRASES (lower-cased) that mark a RETRYABLE rate-limit failure when the SDK
#: raised a generic exception with no typed subclass + no recognised ``code``. The
#: live Groww SDK surfaces a rate-limit as
#: ``GrowwAPIException("Rate limit has breached for your request. ...")`` — the
#: BASE client-error class, NOT ``GrowwAPIRateLimitException`` — so the message is
#: the only reliable signal. Phrase-only — NO bare ``"429"`` numeric substring: it
#: would false-match any UNRELATED message merely CONTAINING ``429`` (a delisted-strike
#: error code, a timestamp, an order id), wrongly classifying a TERMINAL error retryable
#: → re-pulled forever, needlessly stepping the rate ladder down, escaping per-symbol
#: quarantine. A GENUINE status-429 rate-limit is already caught STRUCTURALLY by
#: :data:`_CODE_TO_ERROR` (``code == 429`` int OR digit-string, consumed first in
#: :func:`_classify_generic`); these phrase hints cover the message-only case. Kept
#: narrow (only the two transient classes) so an unknown failure is never blindly
#: retried (Golden Rule #1).
_RATE_LIMIT_HINTS: tuple[str, ...] = ("rate limit", "too many request")
_TIMEOUT_HINTS: tuple[str, ...] = ("timed out", "timeout", "deadline exceeded")
#: Unambiguous 5xx signatures for a GENERIC exception with no recognised ``code``
#: (DEC-GROWW-SERVER-ERROR-RETRY-001). Phrase-only — NO bare ``"500"``/``"503"``
#: numeric substrings (they would false-match unrelated numbers). Checked AFTER the
#: rate-limit + timeout hints so a 429/timeout never reads as a server error.
_SERVER_ERROR_HINTS: tuple[str, ...] = (
    "internal server error",
    "bad gateway",
    "service unavailable",
    "server error",
)
#: Unambiguous transient-network / DNS message signatures for a GENERIC exception
#: whose CLASS NAME is not in :data:`_NETWORK_ERROR_NAMES` but whose message still
#: names the host-level failure (DEC-GROWW-NETWORK-ERROR-RETRY-001) — the live
#: ``"Failed to resolve api.groww.in"`` shape + the OS resolver / socket signatures.
#: Checked LAST (after rate-limit + timeout + server hints), since a network blip is
#: the most generic transient class — so a co-mentioned 429 / timeout / 5xx always
#: wins. Phrase-only (no bare numeric substrings) so an unrelated number never
#: false-matches (Golden Rule #1).
_NETWORK_ERROR_HINTS: tuple[str, ...] = (
    "failed to resolve",
    "name or service not known",
    "temporary failure in name resolution",
    "nodename nor servname",
    "name resolution",
    "connection reset",
    "connection aborted",
    "connection refused",
    "connection broken",
    "max retries exceeded",
)


def _classify_generic(exc: Exception) -> type[GrowwError]:
    """Classify a GENERIC SDK exception by its ``code`` attr + message (retryable-first).

    Used only when the class name is NOT a known typed subclass — i.e. the SDK
    raised a bare ``GrowwAPIException`` / ``GrowwBaseException``. Checks the
    HTTP-ish ``code`` first (``429``→rate-limit, ``408``/``504``→timeout,
    ``500``/``502``/``503``→server-error, int or digit-string), then sniffs the
    message for the transient classes in priority order (rate-limit, then timeout,
    then 5xx server error, then a transient network / DNS blip — so a 429 / timeout
    / 5xx that also mentions a connection never mis-reads as a plain network error,
    and a network failure is recognised LAST as the most generic transient class).
    Anything unrecognised stays the terminal base :class:`GrowwError` (conservative
    — an unknown failure is never retried, Golden Rule #1).
    """
    code = getattr(exc, "code", None)
    if isinstance(code, bool):  # bool is an int subclass — never a status code
        code = None
    if isinstance(code, int) and code in _CODE_TO_ERROR:
        return _CODE_TO_ERROR[code]
    if isinstance(code, str) and code.strip().isdigit() and int(code) in _CODE_TO_ERROR:
        return _CODE_TO_ERROR[int(code)]
    message = str(exc).casefold()
    if any(hint in message for hint in _RATE_LIMIT_HINTS):
        return GrowwRateLimitError
    if any(hint in message for hint in _TIMEOUT_HINTS):
        return GrowwTimeoutError
    if any(hint in message for hint in _SERVER_ERROR_HINTS):
        return GrowwServerError
    if any(hint in message for hint in _NETWORK_ERROR_HINTS):
        return GrowwNetworkError
    return GrowwError


def from_groww_exception(exc: Exception) -> GrowwError:
    """Map a ``growwapi`` SDK exception instance to a neutral :class:`GrowwError`.

    Classification is by the exception's class name (string), so no SDK import is
    needed. **A bare ``GrowwAPIException`` / ``GrowwBaseException`` (the SDK does
    NOT always raise the typed subclass — a rate-limit commonly arrives as the
    base ``GrowwAPIException`` with a ``429`` ``code`` and/or a "Rate limit has
    breached" message) is then classified by :func:`_classify_generic` so it is
    still recognised as retryable** (DEC-GROWW-RATELIMIT-RETRY-001 — without this
    the live derivatives chain pull mis-saw a 429 as terminal and died instantly).
    Any still-unrecognised failure maps to the terminal base :class:`GrowwError` —
    conservative: an unknown failure is never blindly retried (Golden Rule #1).

    **An empty / non-JSON vendor body** (the SDK's ``response.json()`` raising a
    :class:`json.JSONDecodeError`) is recognised FIRST as a RETRYABLE
    :class:`GrowwEmptyResponseError` (DEC-GROWW-EMPTY-BODY-RETRY-001) — a transient
    throttle, NOT a terminal "not on Groww". It is matched by ``isinstance`` (the
    SDK is an optional extra, but :mod:`json` is stdlib so this import is always
    safe). Narrow on purpose: ONLY a ``json.JSONDecodeError`` is treated this way,
    NOT every :class:`ValueError`, so a genuine code bug surfacing as a plain
    ``ValueError`` still maps to the terminal base (never blindly retried).

    **A transient network / DNS failure** (the ``requests`` / ``urllib3`` /
    :class:`socket.gaierror` transport classes — :data:`_NETWORK_ERROR_NAMES`,
    matched by CLASS NAME so neither package need be importable) is recognised as a
    RETRYABLE :class:`GrowwNetworkError` (DEC-GROWW-NETWORK-ERROR-RETRY-001) — the
    request never reached Groww (a host DNS outage like ``"Failed to resolve
    api.groww.in"``), so it is re-issued rather than terminally quarantined. This
    name-match is checked AFTER the typed Groww SDK map (a vendor-typed error always
    wins) but BEFORE :func:`_classify_generic` (so a ``requests.ConnectionError``
    whose message has no recognised hint is still caught by its class name).

    **A socket CONNECT / READ timeout** (``requests.ConnectTimeout`` /
    ``requests.ReadTimeout`` / the base ``requests.Timeout`` / stdlib ``socket.timeout`` —
    :data:`_TIMEOUT_ERROR_NAMES`, matched by CLASS NAME) is recognised as a RETRYABLE
    :class:`GrowwTimeoutError` (DEC-GROWW-HTTP-SOCKET-TIMEOUT-001) — a half-open / dead
    socket the SDK's newly-imposed ``(connect, read)`` timeout aborts in seconds, so it is
    re-issued rather than terminally quarantined. This name-match is checked BEFORE
    :data:`_NETWORK_ERROR_NAMES` so a ``ConnectTimeout`` (which is ALSO a ``ConnectionError``
    by inheritance) reads as a TIMEOUT (the gentle timeout ladder), never a plain network
    blip — and is robust to the ``requests`` message text varying by version.
    """
    name = type(exc).__name__
    if isinstance(exc, json.JSONDecodeError):
        return GrowwEmptyResponseError(f"{name}: {exc}")
    error_cls = _SDK_NAME_TO_ERROR.get(name)
    if error_cls is None and name in _TIMEOUT_ERROR_NAMES:
        error_cls = GrowwTimeoutError
    if error_cls is None and name in _NETWORK_ERROR_NAMES:
        error_cls = GrowwNetworkError
    if error_cls is None:
        error_cls = _classify_generic(exc)
    return error_cls(f"{name}: {exc}")


def is_rate_limit(exc: GrowwError) -> bool:
    """True if *exc* is a rate-limit error (the limiter should back off)."""
    return isinstance(exc, GrowwRateLimitError)


def is_auth_failure(exc: GrowwError) -> bool:
    """True if *exc* means Groww auth failed (drives a re-auth, not a retry)."""
    return isinstance(exc, GrowwAuthError)


def is_terminal_auth_failure(exc: BaseException) -> bool:
    """True iff *exc* is the TERMINAL Groww auth shape — a dead credential, never a self-heal.

    DEC-GROWW-AUTH-HALT-001 — the ONE source of truth for "this auth error must HALT
    the pull": a :class:`GrowwAuthError` that is NOT retryable (a wrong TOTP secret /
    expired api key / re-mint budget exhausted). Every remaining symbol would fail
    identically against the dead credential, so the pull-wide halt paths (both ingest
    spines' ``_GLOBAL_STOP_ERRORS`` drain checks, ``fetch_pipeline._is_pool_isolatable``,
    ``pull_jobs._is_pull_halt``, the CLI ``cmd_fetch`` alert wrap) consult THIS predicate
    instead of quarantining every symbol one by one into a silent all-failed "success".

    Deliberately FALSE for the two RETRYABLE :class:`GrowwAuthError` subclasses —
    :class:`GrowwTokenIssuanceThrottled` (a throttled mint) and
    :class:`GrowwTokenStillStaleError` (a read-only broker re-read racing the Lambda's
    fresh mint) — which are transient by design and must NEVER halt. The mid-pull
    auto-re-mint self-heal (DEC-GROWW-TOKEN-REAUTH-001) is untouched by construction:
    a 06:00 IST token reset that the :class:`~brutex.adapters.groww.reauth.SdkRefresher`
    heals never raises out of ``client._call`` at all, so it can never reach a halt path.
    """
    return isinstance(exc, GrowwAuthError) and not is_retryable(exc)


def is_retryable(exc: GrowwError) -> bool:
    """True if *exc* is transient and the orchestrator should retry it.

    Retryable: rate-limit + timeout + empty/non-JSON body (a transient throttle —
    DEC-GROWW-EMPTY-BODY-RETRY-001) + transient 5xx server error (a momentary
    upstream blip — DEC-GROWW-SERVER-ERROR-RETRY-001) + transient network / DNS
    failure (a host connectivity blip — DEC-GROWW-NETWORK-ERROR-RETRY-001) + a
    THROTTLED token-issuance mint (a transient issuance rate-limit, NOT a bad
    credential — DEC-GROWW-TOKEN-DISK-CACHE-001) + a read-only broker RE-READ that
    still holds the SAME dead token (the Lambda has not minted the fresh one yet —
    DEC-BROKER-READONLY-REAUTH-REREAD-001; the only TWO auth-family errors that are
    retryable, so the cold-start mint backs off + reuses the cache and the read-only
    reauth re-pulls later instead of dying).
    Terminal: bad-request, not-found, a NON-throttled / non-still-stale auth failure
    (needs re-auth — incl. the escalated :class:`GrowwCredentialDeadError`, which is
    additionally a pull-wide global stop, DEC-VENDOR-DOWN-BREAKER-001), feed (out of
    scope), and any bare :class:`GrowwError`.
    """
    return isinstance(
        exc,
        (
            GrowwRateLimitError,
            GrowwTimeoutError,
            GrowwEmptyResponseError,
            GrowwServerError,
            GrowwNetworkError,
            GrowwTokenIssuanceThrottled,
            GrowwTokenStillStaleError,
        ),
    )


def steps_timeout_ladder(exc: GrowwError) -> bool:
    """True iff *exc* may step the AIMD TIMEOUT ladder down — a server-stress signal only.

    DEC-AUTH-LADDER-EXCLUDE-001 — the ONE source of truth for "this swallowed
    transient may feed ``on_timeout``" at BOTH ingest spines'
    ``elif on_timeout is not None`` sites. The timeout ladder
    (:meth:`brutex.adapters.rate_common.RateController.on_timeout`) is a
    SERVER-STRESS signal: a sustained storm of timeouts / empty bodies / 5xx /
    network blips steps the issue rate down toward the deep 1 r/s floor because
    the CANDLE endpoint looks unhealthy. A RETRYABLE AUTH-FAMILY failure says
    nothing about candle-endpoint health, so it must NOT ease the pull rate:

    * :class:`GrowwTokenIssuanceThrottled` IS a genuine vendor throttle — but on
      the token-MINT endpoint, not the candle endpoint; slowing candle fetches
      does not relieve it (do not "fix" this back into a ladder step).
    * :class:`GrowwTokenStillStaleError` is a token race (the read-only re-read
      adopted the SAME dead token before the Lambda's fresh mint) — pure
      credential state, zero data-plane evidence.

    Motivation (LOG-OBSERVED, not repo-verified — Golden Rule #1): an audited
    production pull log showed a sustained pre-06:05-IST token-stale interval
    (~339 stale-token failures observed in that log) riding the timeout ladder
    and pinning the whole pull at the 1 r/s deep floor — mis-training the AIMD
    controller as if Groww's candle endpoint were struggling. Containment for
    the auth family stays what it always was, byte-identical: the mid-pull
    reauth self-heal (DEC-GROWW-TOKEN-REAUTH-001), the client ``max_attempts``
    ladder, the FAILED ledger record (the window stays re-pullable), and the
    K-consecutive-StillStale → :class:`GrowwCredentialDeadError` breaker
    escalation (DEC-VENDOR-DOWN-BREAKER-001) — none of which this predicate
    touches. A rate-limit is excluded because it keeps its OWN ``on_rate_limit``
    path; at the two call sites the code is already past ``is_retryable`` + the
    rate-limit branch, so only the auth conjunct changes behaviour there — the
    full three-conjunct form makes the predicate correct ANYWHERE, not just
    after those branches (CLAUDE.md §11 COMMON).

    Parameters
    ----------
    exc
        The classified :class:`GrowwError` a spine's transient branch swallowed.

    Returns
    -------
    bool
        ``True`` for a retryable NON-rate-limit, NON-auth-family transport
        failure (timeout / empty body / 5xx / network); ``False`` otherwise.
    """
    return is_retryable(exc) and not is_rate_limit(exc) and not is_auth_failure(exc)


__all__ = [
    "GrowwAuthError",
    "GrowwBadRequestError",
    "GrowwCredentialDeadError",
    "GrowwEmptyResponseError",
    "GrowwError",
    "GrowwFeedError",
    "GrowwNetworkError",
    "GrowwNotFoundError",
    "GrowwRateLimitError",
    "GrowwServerError",
    "GrowwTimeoutError",
    "GrowwTokenIssuanceThrottled",
    "GrowwTokenStillStaleError",
    "from_groww_exception",
    "is_auth_failure",
    "is_rate_limit",
    "is_retryable",
    "is_terminal_auth_failure",
    "steps_timeout_ladder",
]
