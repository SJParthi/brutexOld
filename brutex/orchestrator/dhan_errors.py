"""Dhan API error code hierarchy + retry classifier.

Per ``CLAUDE.md §6`` (Dhan facts table) + ``docs/TRACK1_DATA_INGEST_DESIGN.md
§10A`` (verified 2026-05-15 from the Dhan annexure), the Dhan API surface
emits 22 distinct error codes across two namespaces:

- **Data API (12 codes):** 800, 804, 805, 806, 807, 808, 809, 810, 811, 812,
  813, 814.
- **Trading API (10 codes):** DH-901 through DH-910.

Each code maps to exactly one :class:`brutex.orchestrator.state.Outcome` via
:data:`DHAN_ERROR_CLASSIFICATION` so the FSM's :func:`transition` function
can route the worker to the right next-state (retry / quarantine / FATAL
halt / NO_DATA terminal). The mapping is locked in
``docs/TRACK1_DATA_INGEST_DESIGN.md:678-705``.

## Design constraints (DEC-P0-031 pre-impl 4-agent hostile review)

* **Whitelist semantics** — unknown Dhan codes raise
  :class:`UnknownDhanCodeError`; the worker maps that to ``Outcome.FATAL``
  (halt + alert). Defense against MITM-injected codes triggering infinite
  retry storms (Agent 2 §C from PR #50 review).
* **safe_message discipline enforced at base class** — :class:`DhanError`
  requires every subclass to set a class-level
  :attr:`safe_message_default` (categorical string, no payload/URL/token).
  ``__init__`` automatically passes this through ``BruteXError.__init__``
  so the audit chain only ever sees the safe form (Agent 2 §B).
* **No payload in constructors** — every subclass takes only a `str`
  detailed-message arg (an opaque debug string the adapter chooses); the
  adapter MUST NOT pass the raw Dhan response body, headers, or URL — those
  may contain tokens/IDs (Agent 2 §C).
* **HTTP 5xx / empty / None code synthesis** —
  :func:`classify_dhan_code` accepts ``None`` and the empty string and
  synthesizes them to ``"DH-908"`` (Internal Server Error) before
  dispatch, so transient network errors don't trigger
  ``UnknownDhanCodeError`` halts (Agent 3 §K).
* **Separate from legacy classifier** — :data:`DHAN_ERROR_CLASSIFICATION`
  is keyed by **Dhan code strings**; the existing
  :data:`brutex.orchestrator.retry.BROKER_EXCEPTION_CLASSIFICATION` is
  keyed by **Python exception class names** (pre-pivot legacy). The two
  dispatch surfaces are intentionally orthogonal (Agent 1 §C).
* **DailyQuotaExceeded handled by worker layer** —
  :class:`brutex.orchestrator.quota_tracker.DailyQuotaExceeded` is a
  BruteX-local guard (not a Dhan error); the worker catches it directly
  and emits ``Outcome.FATAL`` without going through this classifier
  (Agent 1 §D + Agent 3 §E).
"""

from __future__ import annotations

from typing import ClassVar, Final

from brutex.models import BruteXError
from brutex.orchestrator.state import Outcome

# ────────────────────────────────────────────────────────────────────────
# Base class — enforces safe_message_default on every subclass
# ────────────────────────────────────────────────────────────────────────


class DhanError(BruteXError):
    """Base class for the 22 Dhan API error subclasses.

    Subclasses MUST set the two class-level attributes
    :attr:`dhan_error_code` (e.g. ``"800"``, ``"DH-907"``) and
    :attr:`safe_message_default` (a categorical string with no payload
    contents). Failure to set ``safe_message_default`` raises
    ``AttributeError`` at construction time — fail-loud rather than
    silently fall through to the unsafe ``Exception.__str__()`` (which
    would leak the detailed message into audit logs).

    The detailed-message positional arg is purely for local debugging;
    the audit log path always uses ``str(exc)`` which returns the safe
    message via :meth:`BruteXError.__str__` (PR #50a foundation).
    """

    #: The Dhan API error code (e.g. ``"800"``, ``"DH-907"``). Must be
    #: set on every subclass; checked at construction time.
    dhan_error_code: ClassVar[str]

    #: Categorical safe-message string for audit logs + structlog events.
    #: Must be set on every subclass; checked at construction time.
    safe_message_default: ClassVar[str]

    def __init__(self, detailed_message: str = "") -> None:
        # Fail-loud if a subclass forgot to set safe_message_default —
        # better to crash at construction than to leak the detailed
        # message into the audit log (Agent 2 §B regression oracle).
        try:
            safe = self.safe_message_default
        except AttributeError as exc:
            raise AttributeError(
                f"{type(self).__name__} must set class attribute "
                f"`safe_message_default: ClassVar[str]`"
            ) from exc
        super().__init__(detailed_message, safe_message=safe)


# ────────────────────────────────────────────────────────────────────────
# Unknown code error (whitelist enforcement)
# ────────────────────────────────────────────────────────────────────────


class UnknownDhanCodeError(BruteXError):
    """Raised when :func:`classify_dhan_code` sees a code not in the
    closed 22-code vocabulary.

    The worker layer maps this to ``Outcome.FATAL`` — defense against
    MITM-injected codes triggering infinite retry storms. New Dhan codes
    must be added to :data:`DHAN_ERROR_CLASSIFICATION` explicitly.
    """

    def __init__(self, code: str) -> None:
        super().__init__(
            f"Unknown Dhan API error code: {code!r}",
            safe_message="Unknown Dhan API error code; halting for operator review.",
        )


# ────────────────────────────────────────────────────────────────────────
# Data API error subclasses (12 codes: 800, 804..814)
# ────────────────────────────────────────────────────────────────────────


class DhanError800(DhanError):
    """Dhan Data API code 800 — Internal Server Error."""

    dhan_error_code = "800"
    safe_message_default = "Dhan Data API internal server error (800)"


class DhanError804(DhanError):
    """Dhan Data API code 804 — Requested instruments exceeds limit."""

    dhan_error_code = "804"
    safe_message_default = "Dhan Data API: requested instruments exceeds limit (804)"


class DhanError805(DhanError):
    """Dhan Data API code 805 — Too many requests / connections."""

    dhan_error_code = "805"
    safe_message_default = "Dhan Data API rate limit hit (805)"


class DhanError806(DhanError):
    """Dhan Data API code 806 — Data APIs not subscribed."""

    dhan_error_code = "806"
    safe_message_default = "Dhan Data APIs not subscribed (806)"


class DhanError807(DhanError):
    """Dhan Data API code 807 — Access token expired."""

    dhan_error_code = "807"
    safe_message_default = "Dhan access token expired (807)"


class DhanError808(DhanError):
    """Dhan Data API code 808 — Authentication failed (clientId/token invalid)."""

    dhan_error_code = "808"
    safe_message_default = "Dhan authentication failed (808)"


class DhanError809(DhanError):
    """Dhan Data API code 809 — Access token invalid."""

    dhan_error_code = "809"
    safe_message_default = "Dhan access token invalid (809)"


class DhanError810(DhanError):
    """Dhan Data API code 810 — Client ID invalid."""

    dhan_error_code = "810"
    safe_message_default = "Dhan client ID invalid (810)"


class DhanError811(DhanError):
    """Dhan Data API code 811 — Invalid expiry date (n/a for Track 1 spot)."""

    dhan_error_code = "811"
    safe_message_default = "Dhan request has invalid expiry date (811)"


class DhanError812(DhanError):
    """Dhan Data API code 812 — Invalid date format (code bug)."""

    dhan_error_code = "812"
    safe_message_default = "Dhan request has invalid date format (812)"


class DhanError813(DhanError):
    """Dhan Data API code 813 — Invalid securityId (config mismatch)."""

    dhan_error_code = "813"
    safe_message_default = "Dhan request has invalid securityId (813)"


class DhanError814(DhanError):
    """Dhan Data API code 814 — Invalid request."""

    dhan_error_code = "814"
    safe_message_default = "Dhan invalid request (814)"


# ────────────────────────────────────────────────────────────────────────
# Trading API error subclasses (10 codes: DH-901..DH-910)
# ────────────────────────────────────────────────────────────────────────


class DhanErrorDH901(DhanError):
    """Dhan Trading API code DH-901 — Invalid Authentication."""

    dhan_error_code = "DH-901"
    safe_message_default = "Dhan invalid authentication (DH-901)"


class DhanErrorDH902(DhanError):
    """Dhan Trading API code DH-902 — Invalid Access (no Data API subscription)."""

    dhan_error_code = "DH-902"
    safe_message_default = "Dhan no Data API subscription (DH-902)"


class DhanErrorDH903(DhanError):
    """Dhan Trading API code DH-903 — User Account issue."""

    dhan_error_code = "DH-903"
    safe_message_default = "Dhan user account issue (DH-903)"


class DhanErrorDH904(DhanError):
    """Dhan Trading API code DH-904 — Rate Limit."""

    dhan_error_code = "DH-904"
    safe_message_default = "Dhan rate limit hit (DH-904)"


class DhanErrorDH905(DhanError):
    """Dhan Trading API code DH-905 — Input Exception."""

    dhan_error_code = "DH-905"
    safe_message_default = "Dhan input exception (DH-905)"


class DhanErrorDH906(DhanError):
    """Dhan Trading API code DH-906 — Order Error (n/a for Track 1 Data APIs)."""

    dhan_error_code = "DH-906"
    safe_message_default = "Dhan order error (DH-906)"


class DhanErrorDH907(DhanError):
    """Dhan Trading API code DH-907 — Data Error (no bars for query window).

    The worker maps this to ``Outcome.NO_DATA`` (advance watermark, do
    not retry). The adapter (PR #51) handles watermark advance to
    ``chunk_end + 1 minute`` per ``docs/TRACK1_DATA_INGEST_DESIGN.md``.
    """

    dhan_error_code = "DH-907"
    safe_message_default = "Dhan data error / no data for query (DH-907)"


class DhanErrorDH908(DhanError):
    """Dhan Trading API code DH-908 — Internal Server Error.

    Also used as the synthesized fallback when HTTP 5xx has no parseable
    ``error_code`` JSON field (see :func:`classify_dhan_code`).
    """

    dhan_error_code = "DH-908"
    safe_message_default = "Dhan internal server error (DH-908)"


class DhanErrorDH909(DhanError):
    """Dhan Trading API code DH-909 — Network Error."""

    dhan_error_code = "DH-909"
    safe_message_default = "Dhan network error (DH-909)"


class DhanErrorDH910(DhanError):
    """Dhan Trading API code DH-910 — Others."""

    dhan_error_code = "DH-910"
    safe_message_default = "Dhan other error (DH-910)"


# ────────────────────────────────────────────────────────────────────────
# Code → Outcome dispatch table
# ────────────────────────────────────────────────────────────────────────


#: Closed-set 22-code dispatch table. Maps each Dhan error code string to
#: the FSM ``Outcome`` enum value. Source of truth:
#: ``docs/TRACK1_DATA_INGEST_DESIGN.md:678-705`` (verified 2026-05-15
#: against the Dhan annexure).
#:
#: Unknown codes raise :class:`UnknownDhanCodeError` via
#: :func:`classify_dhan_code` (whitelist semantics — Agent 2 §C).
DHAN_ERROR_CLASSIFICATION: Final[dict[str, Outcome]] = {
    # Data API codes (12)
    "800": Outcome.GENERIC_API_ERROR,
    "804": Outcome.FATAL,
    "805": Outcome.RATE_LIMIT,
    "806": Outcome.FATAL,
    "807": Outcome.AUTH_FAILED,
    "808": Outcome.AUTH_FAILED,
    "809": Outcome.AUTH_FAILED,
    "810": Outcome.AUTH_FAILED,
    "811": Outcome.BAD_REQUEST,
    "812": Outcome.BAD_REQUEST,
    "813": Outcome.BAD_REQUEST,
    "814": Outcome.BAD_REQUEST,
    # Trading API codes (10)
    "DH-901": Outcome.AUTH_FAILED,
    "DH-902": Outcome.FATAL,
    "DH-903": Outcome.AUTH_FAILED,
    "DH-904": Outcome.RATE_LIMIT,
    "DH-905": Outcome.BAD_REQUEST,
    "DH-906": Outcome.BAD_REQUEST,
    "DH-907": Outcome.NO_DATA,
    "DH-908": Outcome.GENERIC_API_ERROR,
    "DH-909": Outcome.TIMEOUT,
    "DH-910": Outcome.GENERIC_API_ERROR,
}

#: HTTP 5xx fallback code used when the response body has no parseable
#: ``error_code`` JSON field (Agent 3 §K — prevents transient HTTP 500
#: from being classified as ``UnknownDhanCodeError`` → FATAL halt).
HTTP_5XX_FALLBACK_CODE: Final[str] = "DH-908"


# ────────────────────────────────────────────────────────────────────────
# Code → exception subclass dispatch (for raise discipline at adapter)
# ────────────────────────────────────────────────────────────────────────


#: Maps each Dhan error code to its corresponding exception subclass.
#: The adapter layer (PR #51) uses this to translate Dhan response codes
#: into typed Python exceptions BEFORE the worker layer catches and
#: classifies. The classifier dispatch (:func:`classify_dhan_code`)
#: works on the code STRING directly — this map is for the adapter's
#: raise discipline.
DHAN_CODE_TO_EXCEPTION: Final[dict[str, type[DhanError]]] = {
    "800": DhanError800,
    "804": DhanError804,
    "805": DhanError805,
    "806": DhanError806,
    "807": DhanError807,
    "808": DhanError808,
    "809": DhanError809,
    "810": DhanError810,
    "811": DhanError811,
    "812": DhanError812,
    "813": DhanError813,
    "814": DhanError814,
    "DH-901": DhanErrorDH901,
    "DH-902": DhanErrorDH902,
    "DH-903": DhanErrorDH903,
    "DH-904": DhanErrorDH904,
    "DH-905": DhanErrorDH905,
    "DH-906": DhanErrorDH906,
    "DH-907": DhanErrorDH907,
    "DH-908": DhanErrorDH908,
    "DH-909": DhanErrorDH909,
    "DH-910": DhanErrorDH910,
}


# ────────────────────────────────────────────────────────────────────────
# Classification function
# ────────────────────────────────────────────────────────────────────────


def classify_dhan_code(code: str | None) -> Outcome:
    """Classify a Dhan error code into an FSM :class:`Outcome`.

    Whitelist semantics: unknown codes raise :class:`UnknownDhanCodeError`
    (the worker maps that to ``Outcome.FATAL``). This is the closed-set
    discipline that prevents MITM-injected codes from triggering
    infinite retry storms (Agent 2 §C).

    Parameters
    ----------
    code
        The Dhan error code string (e.g. ``"800"``, ``"DH-907"``). May be
        ``None`` or empty string — in either case, the function
        synthesizes :data:`HTTP_5XX_FALLBACK_CODE` (= ``"DH-908"``) so
        transient HTTP 5xx errors with no parseable response body
        classify as :class:`Outcome.GENERIC_API_ERROR` (retry) rather
        than ``UnknownDhanCodeError`` (halt). See Agent 3 §K of PR #50d
        pre-impl review.

    Returns
    -------
    Outcome
        The FSM outcome for this code per
        ``docs/TRACK1_DATA_INGEST_DESIGN.md:678-705``.

    Raises
    ------
    UnknownDhanCodeError
        If ``code`` is a non-empty string not present in
        :data:`DHAN_ERROR_CLASSIFICATION`.
    """
    if not code:
        # HTTP 5xx with no parseable error_code → synthesize DH-908
        # (canonical Internal Server Error) so the dispatch path is
        # uniform with explicit DH-908 responses.
        code = HTTP_5XX_FALLBACK_CODE

    try:
        return DHAN_ERROR_CLASSIFICATION[code]
    except KeyError as exc:
        raise UnknownDhanCodeError(code) from exc


__all__ = [
    "DHAN_CODE_TO_EXCEPTION",
    "DHAN_ERROR_CLASSIFICATION",
    "HTTP_5XX_FALLBACK_CODE",
    "DhanError",
    "DhanError800",
    "DhanError804",
    "DhanError805",
    "DhanError806",
    "DhanError807",
    "DhanError808",
    "DhanError809",
    "DhanError810",
    "DhanError811",
    "DhanError812",
    "DhanError813",
    "DhanError814",
    "DhanErrorDH901",
    "DhanErrorDH902",
    "DhanErrorDH903",
    "DhanErrorDH904",
    "DhanErrorDH905",
    "DhanErrorDH906",
    "DhanErrorDH907",
    "DhanErrorDH908",
    "DhanErrorDH909",
    "DhanErrorDH910",
    "UnknownDhanCodeError",
    "classify_dhan_code",
]
