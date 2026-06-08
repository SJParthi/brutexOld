"""Durable token-lifecycle EVENT log — track the access token without ever storing it.

DEC-TOKEN-EVENTS-001 (operator decree: "even for auth token keep the DB table and
track everything"). This records the token's life — saved / renewed / expired /
refused, with the derived expiry time + a MASKED client id — as a real queryable
Parquet table at ``<lake_root>/token_events/events.parquet`` (browsable on /db),
the auth-side analog of the per-window ledger (DEC-WINDOW-LEDGER-001).

## SECURITY BOUNDARY (the headline — Golden Rule #1 / §10)

This table records token lifecycle **events + metadata ONLY**. It MUST NEVER
store the raw access-token secret. The token secret's single durable home is the
``0600`` ``~/.brutex/credentials.json`` (DEC-DHAN-TOKEN-SINGLE-SOURCE-001) + the
in-memory :class:`~brutex.adapters.dhan.token_holder.TokenHolder`. This log lives
under the lake and is surfaced on the world-readable, browser-exposed ``/db``
page — writing the secret there would LEAK a live credential. So:

* :func:`record_token_event` is **token-free by signature** — the caller derives
  ``expiry_at`` (via :func:`~brutex.adapters.dhan.token_keepalive.decode_jwt_expiry`)
  and passes only the timestamp, never the token string;
* the row schema is a FIXED allowlist with **no token column**;
* :func:`mask_client_id` stars the middle of the client id (keep first/last 4).

Reliability: append-only, best-effort — a failed event-log write NEVER raises (it
must not crash a renewal mid-pull); :func:`load_token_events` degrades to ``[]``
on a missing / corrupt / wrong-shape file (a structured WARNING, never an
exception). Crash-safe write via ``*.tmp`` + :func:`os.replace`.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

#: Structured logger — surfaces a corrupt/unwritable event log into ``/logs``.
_LOG = structlog.get_logger("brutex.adapters.dhan.token_events")

#: The token-events table directory + file under the lake root (browsable on /db).
EVENTS_DIRNAME: str = "token_events"
EVENTS_FILENAME: str = "events.parquet"

#: The token lifecycle actions this log records (the fixed vocabulary).
ACTION_SAVED: str = "saved"
ACTION_RENEWED: str = "renewed"
ACTION_EXPIRED: str = "expired"
ACTION_REFUSED: str = "refused"

#: Chars kept un-masked at EACH end of the client id (the middle is starred).
_MASK_KEEP: int = 4

#: The on-disk column order — fixed + token-free (the security allowlist).
_COLUMNS: tuple[str, ...] = (
    "event_at",
    "action",
    "status",
    "expiry_at",
    "client_id_masked",
    "reason",
)


@dataclass(frozen=True, slots=True)
class TokenEvent:
    """One token lifecycle event — the unit of the browsable token-events table.

    Immutable + token-FREE: there is deliberately no field for the access-token
    secret (the security boundary). ``expiry_at`` is the token's JWT ``exp``
    rendered as an ISO timestamp (derived by the caller, never the token);
    ``client_id_masked`` is the dhanClientId with its middle starred;
    ``reason`` is a non-secret note (e.g. an error/refusal cause).
    """

    event_at: str  # ISO-8601 timestamp the event occurred
    action: str  # saved | renewed | expired | refused
    status: str  # ok | failed
    expiry_at: str | None  # ISO-8601 token expiry (from JWT exp), never the token
    client_id_masked: str | None
    reason: str | None


def events_dir(lake_root: Path) -> Path:
    """Return the token-events directory (``<lake_root>/token_events``)."""
    return lake_root / EVENTS_DIRNAME


def events_path(lake_root: Path) -> Path:
    """Return the token-events Parquet path (``<lake_root>/token_events/events.parquet``)."""
    return events_dir(lake_root) / EVENTS_FILENAME


def mask_client_id(client_id: str | None) -> str | None:
    """Mask a dhanClientId for display — keep the first/last 4, star the middle.

    ``None`` / empty → ``None``. A short id (≤ 8 chars) is fully starred (so it
    never reveals more than half). This is the ONLY representation of the client
    id that reaches the browsable table — the full id is never stored.

    Examples
    --------
    ``"1100123456788842" → "1100********8842"`` ; ``"123" → "***"``.
    """
    if not client_id:
        return None
    if len(client_id) <= 2 * _MASK_KEEP:
        return "*" * len(client_id)
    middle = "*" * (len(client_id) - 2 * _MASK_KEEP)
    return f"{client_id[:_MASK_KEEP]}{middle}{client_id[-_MASK_KEEP:]}"


def record_token_event(
    lake_root: Path,
    *,
    action: str,
    status: str = "ok",
    expiry_at: datetime | None = None,
    client_id: str | None = None,
    reason: str | None = None,
    now: datetime | None = None,
) -> None:
    """Append ONE token lifecycle event to the durable log — **token-free**, best-effort.

    The security contract: this takes an *event* (action/status + the already-
    derived ``expiry_at`` + the client id to MASK), NEVER the token string. The
    client id is masked here (:func:`mask_client_id`) before it is written. The
    write is append-only + crash-safe (``*.tmp`` + :func:`os.replace`) and
    **never raises** — a failed event-log write logs ``token_events.write_failed``
    and returns, so it can never crash a renewal mid-pull. O(existing events) on
    append (token events are rare — a handful per session).
    """
    stamp = now if now is not None else datetime.now(UTC)
    event = TokenEvent(
        event_at=stamp.isoformat(),
        action=action,
        status=status,
        expiry_at=expiry_at.isoformat() if expiry_at is not None else None,
        client_id_masked=mask_client_id(client_id),
        reason=reason,
    )
    try:
        _append_event(event, lake_root)
    except (OSError, ValueError, TypeError, ImportError) as exc:  # pragma: no cover - defensive
        _LOG.warning("token_events.write_failed", action=action, error=str(exc))


def _append_event(event: TokenEvent, lake_root: Path) -> None:
    """Append *event* to the Parquet log (read-existing + rewrite, atomic)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    existing = load_token_events(lake_root)
    rows = [*existing, event]
    schema = pa.schema([(name, pa.string()) for name in _COLUMNS])
    table = pa.table(
        {
            "event_at": [r.event_at for r in rows],
            "action": [r.action for r in rows],
            "status": [r.status for r in rows],
            "expiry_at": [r.expiry_at for r in rows],
            "client_id_masked": [r.client_id_masked for r in rows],
            "reason": [r.reason for r in rows],
        },
        schema=schema,
    )
    directory = events_dir(lake_root)
    directory.mkdir(parents=True, exist_ok=True)
    path = events_path(lake_root)
    tmp = path.with_name(f"{EVENTS_FILENAME}.tmp")
    pq.write_table(table, tmp)  # type: ignore[no-untyped-call]
    os.replace(tmp, path)


def load_token_events(lake_root: Path) -> list[TokenEvent]:
    """Load every recorded token event for *lake_root*, or ``[]`` if unavailable.

    **Never raises**: a missing file is an empty list; a corrupt / wrong-shape /
    unreadable Parquet logs a WARNING and also degrades to ``[]`` (a lost event
    log costs only visibility — the token secret + its real expiry live elsewhere).
    """
    path = events_path(lake_root)
    if not path.is_file():
        return []
    try:
        import pyarrow.parquet as pq

        table = pq.read_table(path, columns=list(_COLUMNS))  # type: ignore[no-untyped-call]
        cols = {name: table.column(name).to_pylist() for name in _COLUMNS}
        return [
            TokenEvent(
                event_at=str(event_at),
                action=str(action),
                status=str(status),
                expiry_at=(None if expiry_at is None else str(expiry_at)),
                client_id_masked=(None if masked is None else str(masked)),
                reason=(None if reason is None else str(reason)),
            )
            for event_at, action, status, expiry_at, masked, reason in zip(
                cols["event_at"],
                cols["action"],
                cols["status"],
                cols["expiry_at"],
                cols["client_id_masked"],
                cols["reason"],
                strict=True,
            )
        ]
    except (OSError, ValueError, TypeError, KeyError, ImportError) as exc:
        _LOG.warning("token_events.load_failed", path=str(path), error=str(exc))
        return []


def to_payload(events: Iterable[TokenEvent]) -> list[dict[str, Any]]:
    """Project events to the ``/db`` JSON rows — the fixed token-free allowlist."""
    return [
        {
            "event_at": e.event_at,
            "action": e.action,
            "status": e.status,
            "expiry_at": e.expiry_at,
            "client_id_masked": e.client_id_masked,
            "reason": e.reason,
        }
        for e in events
    ]


__all__ = [
    "ACTION_EXPIRED",
    "ACTION_REFUSED",
    "ACTION_RENEWED",
    "ACTION_SAVED",
    "EVENTS_DIRNAME",
    "EVENTS_FILENAME",
    "TokenEvent",
    "events_dir",
    "events_path",
    "load_token_events",
    "mask_client_id",
    "record_token_event",
    "to_payload",
]
