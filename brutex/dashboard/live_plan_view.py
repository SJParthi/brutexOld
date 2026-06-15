"""Read-only surfacing of the live N-connection plan + probe from the manifest JSON.

The dashboard's ``/live`` page answers the operator's scaling questions —
*"how many websocket connections will the capture open, at what per-connection
cap, for how many spot + leg subscriptions, and how many connections does my
account actually allow?"* — by READING the ``{spot, legs}`` subscription manifest
the ON toggle wrote (:func:`brutex.dashboard.live_manifest.make_store_manifest_writer`),
**without importing** :mod:`brutex.live` (``DEC-LIVE-DECOUPLE-001``): the plan
arithmetic (``ceil(N / cap)``) is re-derived here from the manifest's plain
row counts, never by importing :mod:`brutex.live.plan`.

The manifest is the file contract; this module parses it. An absent /
not-yet-written manifest (the toggle has not been turned ON) degrades to an
HONEST empty plan + a note — never invents a connection count (Golden Rule #1).

## What is surfaced

* **plan** — ``connections`` (= ``ceil((spot + legs) / cap)``), ``per_connection_cap``,
  ``spot`` (# spot underlyings), ``legs`` (# resolved F&O legs), ``total`` (spot+legs).
  The cap comes from the manifest's optional ``meta.plan.per_connection_cap`` block
  when the writer stamped it; absent, it falls back to the
  :data:`DEFAULT_PER_CONNECTION_CAP` default — so the plan is always present, never
  blank, and ``connections`` is ALWAYS recomputed from the REAL row counts.
* **probe** — the concurrent-connection ceiling the ACCOUNT allows, IF the manifest
  carries a ``meta.probe`` block (a future ``probe-connections`` wiring stamps it).
  Absent → ``None`` + a note: the probe has not been run, surfaced honestly rather
  than guessed.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Final

import structlog

_LOG = structlog.get_logger("brutex.dashboard.live_plan_view")

#: The per-connection subscription cap used to re-derive the connection count when
#: the manifest carries no stamped ``meta.plan.per_connection_cap``. Mirrors
#: :data:`brutex.live.plan.DEFAULT_PER_CONNECTION_CAP` (the operator-confirmed Groww
#: per-connection limit, CLAUDE.md §6) — kept as a LOCAL literal so this reader never
#: imports :mod:`brutex.live` (``DEC-LIVE-DECOUPLE-001``). If the two ever drift the
#: stamped ``meta.plan`` (written by the live side) wins; this is only the fallback.
DEFAULT_PER_CONNECTION_CAP: Final[int] = 1000

#: The honest empty-state note when no manifest has been written yet.
EMPTY_NOTE: Final[str] = "no plan yet — turn the Live feed ON to write the subscription manifest"

#: The honest note when a manifest exists but carries no probe block.
NO_PROBE_NOTE: Final[str] = (
    "connection probe not run — run `python -m brutex.live probe-connections` to "
    "measure the account's concurrent-connection ceiling"
)


def _connection_count(total: int, cap: int) -> int:
    """``ceil(total / cap)`` connections for *total* instruments (0 → 0 connections).

    Re-derives :func:`brutex.live.plan.connection_count` locally so this reader stays
    decoupled from :mod:`brutex.live`. A non-positive *cap* falls back to
    :data:`DEFAULT_PER_CONNECTION_CAP` so a malformed stamped cap can never divide by
    zero / go negative.
    """
    safe_cap = cap if cap >= 1 else DEFAULT_PER_CONNECTION_CAP
    return math.ceil(total / safe_cap) if total > 0 else 0


def _count_rows(value: Any) -> int:
    """Return ``len(value)`` when *value* is a list, else ``0`` (a malformed group)."""
    return len(value) if isinstance(value, list) else 0


def _valid_cap(meta: dict[str, Any]) -> int:
    """Return the stamped ``meta.plan.per_connection_cap`` when valid, else the default.

    A stamped cap wins only when it is a real positive ``int`` (``bool`` is rejected —
    ``True``/``False`` are ints in Python); anything else falls back to
    :data:`DEFAULT_PER_CONNECTION_CAP` so a malformed manifest never blanks the plan.
    """
    stamped = meta.get("plan")
    if isinstance(stamped, dict):
        cap = stamped.get("per_connection_cap")
        if isinstance(cap, int) and not isinstance(cap, bool) and cap >= 1:
            return cap
    return DEFAULT_PER_CONNECTION_CAP


def _plan_from_counts(spot: int, legs: int, meta: dict[str, Any]) -> dict[str, Any]:
    """Compose the plan block from row counts + the manifest's optional ``meta.plan``.

    ``connections`` is ALWAYS recomputed from the REAL row counts
    (``ceil((spot + legs) / cap)``) so the rendered count matches the manifest the
    capture will actually subscribe — never a stale stamped value.
    """
    cap = _valid_cap(meta)
    total = spot + legs
    return {
        "connections": _connection_count(total, cap),
        "per_connection_cap": cap,
        "spot": spot,
        "legs": legs,
        "total": total,
    }


def _probe_from_meta(meta: dict[str, Any]) -> dict[str, Any] | None:
    """Return the probe block from ``meta.probe`` when present + well-shaped, else ``None``.

    A future ``probe-connections`` wiring stamps ``meta.probe = {"max_held": int,
    "failed_at": int|None, "reached_ceiling": bool}`` (mirroring
    :class:`brutex.live.probe.ConnectionProbeResult`). We pass through a probe block
    that carries at least an integer ``max_held`` (the concurrent ceiling); anything
    else (absent, not a mapping, no usable ceiling) yields ``None`` so the dashboard
    shows "probe not run" rather than a fabricated ceiling (Golden Rule #1).
    """
    probe = meta.get("probe")
    if not isinstance(probe, dict):
        return None
    max_held = probe.get("max_held")
    if not isinstance(max_held, int) or isinstance(max_held, bool):
        return None
    failed_at = probe.get("failed_at")
    valid_failed = isinstance(failed_at, int) and not isinstance(failed_at, bool)
    return {
        "max_held": max_held,
        "failed_at": failed_at if valid_failed else None,
        "reached_ceiling": bool(probe.get("reached_ceiling")),
    }


def _empty_payload() -> dict[str, Any]:
    """The honest empty plan payload (no manifest written yet)."""
    return {
        "plan": {
            "connections": 0,
            "per_connection_cap": DEFAULT_PER_CONNECTION_CAP,
            "spot": 0,
            "legs": 0,
            "total": 0,
        },
        "probe": None,
        "probe_note": NO_PROBE_NOTE,
        "note": EMPTY_NOTE,
    }


def read_connection_plan(manifest_path: str | Path) -> dict[str, Any]:
    """Read the live subscription *manifest* JSON into the ``/live`` plan payload.

    Parses the plain ``{"spot": [...], "legs": [...]}`` manifest (optionally carrying
    a ``meta`` block) the dashboard wrote, counts the spot + leg rows, and re-derives
    the N-connection plan (``ceil((spot + legs) / cap)``) locally — never importing
    :mod:`brutex.live` (``DEC-LIVE-DECOUPLE-001``). A MISSING / unreadable / malformed
    manifest degrades to the honest empty plan + :data:`EMPTY_NOTE` (the Live feed has
    not been turned ON yet) — never raises, never invents a count (Golden Rule #1).

    Parameters
    ----------
    manifest_path
        The capture's ``--manifest`` JSON path (the dashboard's ON-toggle sink).

    Returns
    -------
    dict[str, Any]
        ``{"plan": {"connections", "per_connection_cap", "spot", "legs", "total"},
        "probe": {...}|None, "probe_note": <str|None>, "note": <str|None>}`` — ``note``
        is :data:`EMPTY_NOTE` only for an absent/unreadable/malformed manifest, else
        ``None``; ``probe`` is ``None`` + ``probe_note`` :data:`NO_PROBE_NOTE` until a
        probe is stamped into ``meta.probe``.
    """
    path = Path(manifest_path)
    if not path.is_file():
        return _empty_payload()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _LOG.warning("live_plan_manifest_unreadable", path=str(path))
        return _empty_payload()
    if not isinstance(data, dict):
        return _empty_payload()
    spot = _count_rows(data.get("spot"))
    legs = _count_rows(data.get("legs"))
    meta = data.get("meta")
    meta_dict: dict[str, Any] = meta if isinstance(meta, dict) else {}
    probe = _probe_from_meta(meta_dict)
    return {
        "plan": _plan_from_counts(spot, legs, meta_dict),
        "probe": probe,
        "probe_note": None if probe is not None else NO_PROBE_NOTE,
        "note": None,
    }


__all__ = [
    "DEFAULT_PER_CONNECTION_CAP",
    "EMPTY_NOTE",
    "NO_PROBE_NOTE",
    "read_connection_plan",
]
