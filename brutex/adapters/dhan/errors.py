"""Dhan client error model — neutral exceptions + retry classification (pure law).

DEC-DHAN-ADAPTER-001. The DhanHQ v2 REST API signals failures TWO ways
(``docs/adr/00NN-dhan-second-vendor-adapter.md``):

* an **HTTP status code** on the response (``429`` throttle, ``5xx`` upstream, …);
* a **``DH-9xx`` application error code** in the JSON body, either as
  ``{errorType, errorCode:"DH-901", errorMessage}`` or the SDK envelope
  ``{status:"failure", remarks:{error_code, error_type, error_message}}``.

This module maps BOTH into a small neutral hierarchy the rest of BruteX reasons
about — the exact analogue of :mod:`brutex.adapters.groww.errors`, mirrored 1:1
for a second vendor (CLAUDE.md §11 COMMON is honoured by REUSING the neutral
:class:`~brutex.models.BruteXError` base + the shared retry-classification SHAPE,
never copying Groww's SDK-specific class-name map).

**VERIFIED DH-9xx codes** (DhanHQ v2 docs): ``DH-901`` Invalid_Authentication,
``DH-905`` Input_Exception, ``DH-906`` Order_Error. ``DH-902`` / ``DH-903`` /
``DH-904`` / ``DH-907`` are **UNVERIFIED** (model-inferred meanings) — mapped
CONSERVATIVELY to the terminal base :class:`DhanError` (never blindly retried,
Golden Rule #1), pinned in :data:`DH_CODE_UNVERIFIED`.

The Dhan Python SDK ALSO appears to use a separate NUMERIC scheme — **UNVERIFIED**
(model-inferred codes + meanings, NOT in the verified fact canon): ``807``
token-expired / ``808`` auth-failed / ``809`` invalid-token / ``810``
invalid-client-id. Recorded in :data:`SDK_NUMERIC_CODES_UNVERIFIED` — kept
SEPARATE from the DH-9xx REST codes and NOT conflated in classification — a REST
body carrying an integer ``807`` is not a DH-9xx code and falls through to
HTTP-status classification.

This module is a **pure, I/O-free LEAF** (imports only :mod:`json` + the neutral
:class:`~brutex.models.BruteXError`) so a future ``dhan_errors_rust.py`` facade
could wrap :func:`from_dhan_response` / :func:`from_transport_exception` 1:1
(DEC-DHAN-RUST-FACADE-001).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from brutex.models import BruteXError


class DhanError(BruteXError):
    """Base class for every Dhan adapter failure (terminal unless a subclass)."""


class DhanBadRequestError(DhanError):
    """Malformed request parameters (terminal) — HTTP 400 / DH-905 Input_Exception."""


class DhanNotFoundError(DhanError):
    """Unknown instrument / endpoint (terminal) — HTTP 404."""


class DhanAuthError(DhanError):
    """Authentication / authorisation failure (drives re-auth, not blind retry) — terminal.

    HTTP 401/403 or DH-901 Invalid_Authentication. Terminal by default: a dead
    24-hour token / wrong client-id would fail every remaining window identically,
    so it must HALT rather than be silently quarantined per symbol. The one
    RETRYABLE auth-family subclass is :class:`DhanTokenStillStaleError`.
    """


class DhanTokenStillStaleError(DhanAuthError):
    """A read-only broker RE-READ returned the SAME dead token — RETRYABLE, NOT terminal.

    On a read-only host (BruteX never mints), when the shared daily access token is
    rejected mid-run the reauth path RE-READS the shared Parameter-Store SecureString
    to adopt the freshly-minted token; if the re-read returns the SAME token that
    just failed (the minter has not written the new one yet), adopting it would loop
    the dead token. This is raised instead — a :class:`DhanAuthError` SUBCLASS (so
    every ``except DhanAuthError`` site + :func:`is_auth_failure` keep matching it)
    that :func:`is_retryable` returns ``True`` for, so the caller's bounded
    retry/quarantine path re-pulls that window LATER (when the fresh token lands)
    rather than crashing the process. A read that returns a NEW token is adopted
    immediately (no error), so this never fires on the healthy rotation path.
    """


class DhanFeedError(DhanError):
    """Live-feed (WebSocket) failure — OUT of scope for a data-only adapter (terminal)."""


class DhanRateLimitError(DhanError):
    """Per-second or daily quota exceeded — RETRYABLE (back off + retry). HTTP 429."""


class DhanTimeoutError(DhanError):
    """Network read/connect timeout — RETRYABLE. HTTP 408/504 or a socket timeout."""


class DhanNetworkError(DhanError):
    """Transient network / DNS connectivity failure — RETRYABLE.

    A DNS resolution failure / dropped / reset / refused connect: the request never
    reached Dhan, so re-issuing it is safe. Deliberately NOT a :class:`DhanRateLimit\
    Error` — a network blip is not a quota signal, so it rides the gentle timeout
    ladder (:func:`is_rate_limit` stays ``False``).
    """


class DhanServerError(DhanError):
    """Transient vendor server error (HTTP 5xx) — RETRYABLE.

    A momentary upstream blip (``500`` / ``502`` / ``503``). Deliberately NOT a
    :class:`DhanRateLimitError` — it must not step the per-second rate ladder down.
    """


class DhanEmptyResponseError(DhanError):
    """Empty / non-JSON vendor body — a transient throttle, RETRYABLE.

    Under a transient throttle Dhan may return an EMPTY / HTML body whose JSON decode
    raises :class:`json.JSONDecodeError`. Treated as retryable (never a terminal "not
    on Dhan"); narrow on purpose — ONLY a ``json.JSONDecodeError`` is treated this way.
    """


#: VERIFIED DH-9xx code → neutral :class:`DhanError` subclass. Only the three
#: DhanHQ-v2-documented codes are mapped; ``DH-906`` Order_Error is a terminal
#: account/order condition irrelevant to a data pull, mapped to the terminal base.
DH_CODE_TO_ERROR: Mapping[str, type[DhanError]] = {
    "DH-901": DhanAuthError,  # Invalid_Authentication (VERIFIED)
    "DH-905": DhanBadRequestError,  # Input_Exception (VERIFIED)
    "DH-906": DhanError,  # Order_Error — terminal, not a data-pull condition (VERIFIED)
}

#: UNVERIFIED DH-9xx codes (model-inferred meanings) — mapped CONSERVATIVELY to the
#: terminal base :class:`DhanError` (never blindly retried). Pinned so the drift-guard
#: test asserts they stay terminal until a live probe documents their real meaning.
DH_CODE_UNVERIFIED: frozenset[str] = frozenset({"DH-902", "DH-903", "DH-904", "DH-907"})

#: The Dhan SDK's SEPARATE numeric error scheme — **UNVERIFIED** (model-inferred
#: codes + meanings; the verified canon covers only the DH-9xx REST codes). Kept
#: DISTINCT from the DH-9xx codes and never conflated in classification; recorded
#: for completeness only, consumed by NOTHING (a live probe may confirm or delete).
SDK_NUMERIC_CODES_UNVERIFIED: Mapping[int, str] = {
    807: "token-expired",
    808: "auth-failed",
    809: "invalid-token",
    810: "invalid-client-id",
}

#: HTTP status code → neutral :class:`DhanError` subclass. ``429`` = rate-limit;
#: ``408``/``504`` = timeout; ``500``/``502``/``503`` = transient server error;
#: ``400`` = bad-request; ``401``/``403`` = auth; ``404`` = not-found. VERIFIED —
#: DhanHQ v2 docs (partly) + conventional HTTP semantics.
HTTP_STATUS_TO_ERROR: Mapping[int, type[DhanError]] = {
    429: DhanRateLimitError,
    408: DhanTimeoutError,
    504: DhanTimeoutError,
    500: DhanServerError,
    502: DhanServerError,
    503: DhanServerError,
    400: DhanBadRequestError,
    401: DhanAuthError,
    403: DhanAuthError,
    404: DhanNotFoundError,
}

#: Transport-layer exception class-NAMES that mark a transient network / DNS blip —
#: RETRYABLE (matched by NAME so neither ``requests`` nor ``urllib3`` need be
#: importable, the optional-extra resilience).
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

#: Transport-layer exception class-NAMES that mark a socket CONNECT / READ timeout —
#: RETRYABLE. Checked BEFORE the network names so a ``ConnectTimeout`` (which is ALSO
#: a ``ConnectionError`` by inheritance) reads as a TIMEOUT, never a plain blip.
_TIMEOUT_ERROR_NAMES: frozenset[str] = frozenset(
    {
        "ReadTimeout",
        "ConnectTimeout",
        "Timeout",
        # The builtin: on Python 3.10+ ``socket.timeout is TimeoutError`` (its
        # ``__name__`` is "TimeoutError"), so a raw socket timeout leaking past
        # the requests wrappers must map RETRYABLE too. The pre-3.10 lowercase
        # "timeout" name is dead on this 3.12-pinned repo and deliberately absent.
        "TimeoutError",
    }
)


def _extract_dh_code(body: Mapping[str, object]) -> str | None:
    """Pull the ``DH-9xx`` code from either documented Dhan error-body shape.

    Accepts ``{errorCode:"DH-901", ...}`` (the top-level REST shape) OR the SDK
    envelope ``{status:"failure", remarks:{error_code:"DH-901", ...}}``. Returns the
    uppercased code string, or ``None`` when neither shape carries one.
    """
    top = body.get("errorCode")
    if isinstance(top, str) and top.strip():
        return top.strip().upper()
    remarks = body.get("remarks")
    if isinstance(remarks, Mapping):
        nested = remarks.get("error_code")
        if isinstance(nested, str) and nested.strip():
            return nested.strip().upper()
    return None


#: The documented ``DH-<digits>`` application-error-code shape. Used by the 2xx
#: body-failure gate — only a code matching the documented shape marks an HTTP-2xx
#: body as a failure (an arbitrary ``errorCode`` string on a success payload must
#: never flip it to an error).
_DH_CODE_SHAPE = re.compile(r"DH-\d+\Z")


def body_signals_failure(body: Mapping[str, object]) -> bool:
    """True iff *body* carries a documented Dhan FAILURE marker (the 2xx-body gate).

    Dhan can deliver an application failure ON an HTTP 200 — the module
    docstring's SECOND failure mechanism (``DH-9xx`` in the body / the SDK
    ``{status:"failure", remarks:{...}}`` envelope). The client consults this on
    a 2xx status so such a response is classified through
    :func:`from_dhan_response` instead of returned as success (where it would
    die downstream as a misleading parse error, bypassing the reauth /
    terminal-auth / rate-limit paths entirely).

    The gate keys on POSITIVE marker presence ONLY — never on the absence of
    success keys — so a clean candle / expiry / option-chain payload can never
    be misclassified:

    * a ``DH-<digits>``-shaped code from either documented error-body shape
      (:func:`_extract_dh_code` — top-level ``errorCode`` or the SDK
      ``remarks.error_code``), or
    * a ``status`` field reading ``"failure"`` (case-insensitive — the SDK
      failure envelope's status token; the expiry-list success envelope carries
      ``status: "success"``, which never matches).

    Pure + O(1); never raises.
    """
    code = _extract_dh_code(body)
    if code is not None and _DH_CODE_SHAPE.fullmatch(code):
        return True
    status = body.get("status")
    return isinstance(status, str) and status.strip().lower() == "failure"


def _detail_from_body(body: Mapping[str, object]) -> str:
    """Return the vendor's error message text (secret-free) from either body shape.

    Prefers ``errorMessage`` (REST shape); falls back to ``remarks.error_message``
    (SDK envelope); else a short shape summary. Never raises.
    """
    top = body.get("errorMessage")
    if isinstance(top, str) and top.strip():
        return top.strip()
    remarks = body.get("remarks")
    if isinstance(remarks, Mapping):
        nested = remarks.get("error_message")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return f"HTTP body carried no error message (keys: {sorted(map(str, body))})"


def from_dhan_response(status_code: int, body: Mapping[str, object]) -> DhanError:
    """Map a Dhan REST response (status + JSON body) to a neutral :class:`DhanError`.

    A recognised ``DH-9xx`` code in the body wins FIRST (the vendor's own verdict);
    an UNVERIFIED / unrecognised ``DH-9xx`` code maps to the terminal base (Golden
    Rule #1 — never blindly retried). Absent a usable code, classification falls to
    the HTTP status; an unrecognised status maps to the terminal base.
    """
    detail = _detail_from_body(body)
    dh_code = _extract_dh_code(body)
    if dh_code is not None:
        error_cls = DH_CODE_TO_ERROR.get(dh_code, DhanError)
        return error_cls(f"{dh_code}: {detail}")
    error_cls = HTTP_STATUS_TO_ERROR.get(status_code, DhanError)
    return error_cls(f"HTTP {status_code}: {detail}")


def from_transport_exception(exc: Exception) -> DhanError:
    """Map a transport-layer exception (before/without an HTTP response) to a :class:`DhanError`.

    An empty / non-JSON body (:class:`json.JSONDecodeError`) is recognised FIRST as a
    RETRYABLE :class:`DhanEmptyResponseError` — a transient throttle, NOT a terminal
    "not on Dhan" (narrow: ONLY ``json.JSONDecodeError``, matched by ``isinstance``
    since :mod:`json` is stdlib). A socket CONNECT/READ timeout is recognised BEFORE a
    plain network blip (a ``ConnectTimeout`` is ALSO a ``ConnectionError`` by
    inheritance, but its own class name reads as a TIMEOUT). Anything unrecognised maps
    to the terminal base — conservative (Golden Rule #1).
    """
    name = type(exc).__name__
    if isinstance(exc, json.JSONDecodeError):
        return DhanEmptyResponseError(f"{name}: {exc}")
    if name in _TIMEOUT_ERROR_NAMES:
        return DhanTimeoutError(f"{name}: {exc}")
    if name in _NETWORK_ERROR_NAMES:
        return DhanNetworkError(f"{name}: {exc}")
    return DhanError(f"{name}: {exc}")


def is_rate_limit(exc: DhanError) -> bool:
    """True if *exc* is a rate-limit error (the limiter should back off)."""
    return isinstance(exc, DhanRateLimitError)


def is_auth_failure(exc: DhanError) -> bool:
    """True if *exc* means Dhan auth failed (drives a re-auth, not a retry)."""
    return isinstance(exc, DhanAuthError)


def is_terminal_auth_failure(exc: BaseException) -> bool:
    """True iff *exc* is the TERMINAL Dhan auth shape — a dead credential, never a self-heal.

    A :class:`DhanAuthError` that is NOT retryable (a dead token / wrong client-id).
    Deliberately FALSE for the retryable :class:`DhanTokenStillStaleError` (a read-only
    re-read racing the fresh mint), which is transient by design and must never halt.
    """
    return isinstance(exc, DhanAuthError) and not is_retryable(exc)


def is_retryable(exc: DhanError) -> bool:
    """True if *exc* is transient and the orchestrator should retry it.

    Retryable: rate-limit + timeout + empty/non-JSON body + transient 5xx server +
    transient network/DNS + a read-only re-read that still holds the dead token
    (:class:`DhanTokenStillStaleError` — the only retryable auth-family error).
    Terminal: bad-request, not-found, a non-still-stale auth failure, feed, and any
    bare :class:`DhanError`.
    """
    return isinstance(
        exc,
        (
            DhanRateLimitError,
            DhanTimeoutError,
            DhanEmptyResponseError,
            DhanServerError,
            DhanNetworkError,
            DhanTokenStillStaleError,
        ),
    )


def steps_timeout_ladder(exc: DhanError) -> bool:
    """True iff *exc* may step the AIMD TIMEOUT ladder down — a server-stress signal only.

    A retryable NON-rate-limit, NON-auth-family transport failure (timeout / empty
    body / 5xx / network). A rate-limit keeps its own ``on_rate_limit`` path; a
    retryable auth-family error (a token race) says nothing about data-endpoint health.
    """
    return is_retryable(exc) and not is_rate_limit(exc) and not is_auth_failure(exc)


__all__ = [
    "DH_CODE_TO_ERROR",
    "DH_CODE_UNVERIFIED",
    "HTTP_STATUS_TO_ERROR",
    "SDK_NUMERIC_CODES_UNVERIFIED",
    "DhanAuthError",
    "DhanBadRequestError",
    "DhanEmptyResponseError",
    "DhanError",
    "DhanFeedError",
    "DhanNetworkError",
    "DhanNotFoundError",
    "DhanRateLimitError",
    "DhanServerError",
    "DhanTimeoutError",
    "DhanTokenStillStaleError",
    "body_signals_failure",
    "from_dhan_response",
    "from_transport_exception",
    "is_auth_failure",
    "is_rate_limit",
    "is_retryable",
    "is_terminal_auth_failure",
    "steps_timeout_ladder",
]
