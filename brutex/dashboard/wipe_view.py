"""Dashboard wipe guards — pure HTTP-side checks for "Delete all ingested data".

The ``POST /api/v1/db/wipe`` route in ``app.py`` (coverage-omitted glue) delegates
EVERY decision here, so the guard logic is 100% line+branch tested without
FastAPI: the typed-``WIPE`` confirmation (:func:`parse_wipe_confirmation`), the
refuse-while-a-pull-is-writing check (:func:`wipe_refusal_reason`), the
same-origin guard for a destructive POST (:func:`is_same_origin`), and shaping a
:class:`~brutex.data.lake_wipe.LakeWipeReport` into a JSON-able dict
(:func:`wipe_report_payload`). See DEC-DASHBOARD-WIPE-001.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import urlsplit

from brutex.data.lake_wipe import LakeWipeReport

#: The exact token the operator must type to authorise an irreversible wipe.
WIPE_CONFIRM_WORD: Final[str] = "WIPE"


def parse_wipe_confirmation(confirm: str | None) -> bool:
    """True iff *confirm* is exactly the required ``WIPE`` token (typed-confirm guard)."""
    return confirm == WIPE_CONFIRM_WORD


def wipe_refusal_reason(*, expiries_running: bool, contracts_running: bool) -> str | None:
    """Return why a wipe must be refused right now, or ``None`` if it may proceed.

    Refuses while a pull phase is mid-write so the wipe can't race a live ingest
    into a confusing (though, serialised on the store lock, never corrupt) result.
    """
    if expiries_running:
        return "an expiries pull is running — wait for it to finish, then wipe"
    if contracts_running:
        return "a contracts pull is running — wait for it to finish, then wipe"
    return None


def is_same_origin(origin: str | None, host: str | None) -> bool:
    """True if a destructive POST is same-origin (CSRF guard for a no-auth dashboard).

    A request with no ``Origin`` header (a same-page ``fetch`` or a CLI/curl call)
    is allowed. When ``Origin`` IS present (a cross-site POST sets it), its
    ``host[:port]`` must match the request ``Host`` — otherwise it is a
    cross-origin attempt and is refused.
    """
    if origin is None:
        return True
    if host is None:
        return False
    return urlsplit(origin).netloc == host


def wipe_report_payload(report: LakeWipeReport) -> dict[str, object]:
    """Shape a :class:`LakeWipeReport` into a JSON-serialisable dict for the route."""
    return {
        "deleted": [{"table": table, "rows": rows} for table, rows in report.deleted],
        "failed": [{"table": table, "error": error} for table, error in report.failed],
    }


__all__ = [
    "WIPE_CONFIRM_WORD",
    "is_same_origin",
    "parse_wipe_confirmation",
    "wipe_refusal_reason",
    "wipe_report_payload",
]
