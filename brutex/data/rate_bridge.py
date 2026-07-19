"""Cross-process pull-rate VISIBILITY bridge — a small shared file under the lake root.

DEC-CLI-RATE-BRIDGE-001 (MIGRATION.md entry #997). The dashboard's live-rate WIDGET
(DEC-LIVE-RATE-MONITOR-001, ``/api/v1/pull/rate`` → ``bx-rate-monitor.js``) reads an
IN-MEMORY :class:`~brutex.dashboard.app.RateLimitPanel` populated on each
``_observe_rate`` tick of a DASHBOARD pull. A CLI ``python -m brutex fetch`` pull runs
in a SEPARATE process, so it never touches that in-process panel — the widget sits IDLE
while a CLI pull is actively hammering Groww. There was no CLI rate-observer seam.

This module is the thin cross-process bridge that closes that gap WITHOUT changing any
pacing decision:

* **Publisher (CLI side).** On each rate-observe tick the CLI pull atomically writes the
  current rate snapshot (the SAME controller + call-meter fields the dashboard mirrors:
  allowed ``current_rps``, achieved ``effective_rps``, ``recent_minute`` = req/min,
  ``recent_429`` 429 hits, ``ceiling_per_min``) plus a WALL-CLOCK timestamp to
  ``<lake_root>/.rate/pull_rate.json`` via :func:`publish_rate_snapshot`.
* **Reader (dashboard side).** :func:`read_rate_snapshot` best-effort reads that file.
  The ``/api/v1/pull/rate`` route considers it ALONGSIDE the in-memory panel and surfaces
  whichever pull (dashboard OR CLI) ticked most recently, so the widget reflects the
  active pull's real measured rate regardless of which process is pulling. A missing /
  stale / unreadable file is honestly ignored (the in-memory panel wins, or IDLE).

## Why wall-clock, not monotonic

The in-memory panel's ``last_update_monotonic`` is a per-process ``time.monotonic``
reading — meaningless ACROSS processes (each process's monotonic clock has its own
arbitrary epoch). The bridge therefore stamps a ``wall_time`` (``time.time()``, seconds
since the Unix epoch — comparable across processes) and the reader derives staleness from
``now_wall - wall_time``. A second ``monotonic`` field is carried for in-process diagnostic
parity but is NOT used for the cross-process freshness decision.

## Honest envelope (Golden Rule #10)

This is a cross-process **VISIBILITY** bridge, file-based and BEST-EFFORT — it is NOT
shared rate ACCOUNTING. The per-process rate caps are unchanged; the single-flight pull
lock (DEC-PULL-HARDENING-FOLLOWUP-001) already prevents two concurrent pulls on one lake,
so at most one publisher is ever active. The widget reflects the ACTIVE pull's real
measured rate and reads IDLE when none is fresh — never a fabricated number (Golden
Rule #1). Every read failure (missing file, partial write, corrupt JSON, wrong shape)
degrades to ``None`` so a damaged bridge file can NEVER break the dashboard. The publish
runs inside :func:`~brutex.data.atomic_io.lake_write_guard`, so a full-disk ENOSPC at the
bridge write maps to a :class:`~brutex.data.errors.LakeWriteHalt` consistent with every
other lake-write site (DEC-LAKE-WRITE-HALT-001).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from brutex.data.atomic_io import atomic_write_simple, lake_write_guard
from brutex.data.sidecar_read import read_regular_bytes

__all__ = [
    "RATE_BRIDGE_FILENAME",
    "RATE_BRIDGE_SUBDIR",
    "RateBridgeSnapshot",
    "publish_from_snapshots",
    "publish_rate_snapshot",
    "rate_bridge_path",
    "read_rate_snapshot",
]

#: Dot-prefixed sidecar dir under the lake root (a sibling of ``.locks`` /
#: ``bars`` / ``options``) so no dataset glob or lake scanner ever matches it.
RATE_BRIDGE_SUBDIR = ".rate"

#: The single bridge file name. ONE file (last-writer-wins) — the single-flight pull lock
#: guarantees at most one publisher, so there is no multi-writer contention to resolve.
RATE_BRIDGE_FILENAME = "pull_rate.json"

#: The bridge schema version. Bumped if the field set changes incompatibly; the reader
#: ignores a snapshot whose ``schema`` it does not recognise (forward-safe — never a
#: KeyError / wrong-shape crash). Bumped 1 → 2 to carry the trailing-window real issue counts
#: (``recent_1s`` / ``recent_600s`` / ``recent_3600s``) for the expanded rate monitor
#: (DEC-RATE-MONITOR-EXPAND-001, MIGRATION.md #1004); an old (schema-1) CLI bridge file is
#: ignored as unrecognised, so the dashboard simply projects the in-memory panel until the
#: CLI re-publishes — never a crash, never a fabricated number. Bumped 2 → 3 to carry the
#: AUTHORITATIVE ``pull_active`` flag (DEC-CLI-BRIDGE-PULL-ACTIVE-001) so the dashboard's
#: stall verdict is gated on the CLI pull actually RUNNING (the follow-up the
#: DEC-RATE-STALL-PULL-GATE-001 disclaimer named). The field decodes via ``.get`` with a
#: graceful ``None`` default (the old-reader tick-freshness fallback), so a hand-edited v3
#: file missing the key still reads; a stale v2 file is ignored exactly like any
#: unrecognised schema and is re-published within one window tick.
_SCHEMA_VERSION = 3


@dataclass(frozen=True, slots=True)
class RateBridgeSnapshot:
    """One cross-process rate snapshot written by a pull + read by the dashboard.

    The MEASURED fields mirror the in-memory :class:`~brutex.dashboard.app.RateLimitPanel`
    one-for-one (so the reader can feed them into the SAME
    :func:`~brutex.dashboard.pull_rate_view.build_pull_rate_view` projection), plus a
    cross-process-comparable wall-clock timestamp.

    Attributes
    ----------
    schema
        The bridge schema version (:data:`_SCHEMA_VERSION`). The reader ignores an
        unrecognised version rather than mis-decoding it.
    effective_rps
        The transport call-meter's MEASURED achieved r/s (``effective_rps``).
    current_rps
        The controller's current allowed AIMD r/s (``current_rps``).
    recent_minute
        The controller's MEASURED rolling-60s issue count (``recent_minute``) — req/min.
    recent_429
        Recent vendor 429 hits (``recent_hits``) — context for a depressed achieved rate.
    ceiling_per_min
        The HARD per-minute governor cap (``ceiling_per_min``); ``-1`` when none is wired.
    recent_1s, recent_600s, recent_3600s
        The controller's MEASURED trailing 1 s / 600 s / 3600 s real issue counts
        (``recent_1s`` / ``recent_600s`` / ``recent_3600s``; DEC-RATE-MONITOR-EXPAND-001) —
        the req/sec, req/10min + MEASURED req/hour evidence carried across to the dashboard
        widget so a CLI pull's expanded monitor shows the same real counts.
    wall_time
        Seconds since the Unix epoch (``time.time()``) at the tick — the CROSS-PROCESS
        freshness clock the reader compares against ``now``.
    monotonic
        The publisher's ``time.monotonic()`` reading at the tick — carried for in-process
        diagnostic parity ONLY; NOT used for the cross-process staleness decision.
    pull_active
        The publisher's AUTHORITATIVE pull-run signal (DEC-CLI-BRIDGE-PULL-ACTIVE-001):
        ``True`` while the CLI pull is running (each per-window publish), ``False`` on the
        pull's final publish (the ``cmd_fetch`` ``finally``), ``None`` when the publisher
        carried no signal (a hand-edited file / graceful old-reader default) — the reader
        then keeps the honest tick-freshness fallback.
    """

    schema: int
    effective_rps: float
    current_rps: float
    recent_minute: int
    recent_429: int
    ceiling_per_min: int
    recent_1s: int
    recent_600s: int
    recent_3600s: int
    wall_time: float
    monotonic: float
    pull_active: bool | None = None

    def to_json_bytes(self) -> bytes:
        """Serialise to compact UTF-8 JSON bytes (the on-disk bridge payload)."""
        return json.dumps(asdict(self), separators=(",", ":")).encode("utf-8")


def rate_bridge_path(lake_root: Path) -> Path:
    """Return the bridge file path ``<lake_root>/.rate/pull_rate.json`` (never created here).

    Parameters
    ----------
    lake_root
        The lake root; the ``.rate`` sidecar dir lives directly under it.

    Returns
    -------
    Path
        The single shared bridge file path.
    """
    return lake_root / RATE_BRIDGE_SUBDIR / RATE_BRIDGE_FILENAME


def publish_rate_snapshot(
    lake_root: Path,
    *,
    effective_rps: float,
    current_rps: float,
    recent_minute: int,
    recent_429: int,
    ceiling_per_min: int,
    wall_time: float,
    monotonic: float,
    recent_1s: int = 0,
    recent_600s: int = 0,
    recent_3600s: int = 0,
    pull_active: bool | None = None,
) -> None:
    """Atomically write one rate snapshot to the lake-root bridge file (best-effort, O(1)).

    Creates the ``.rate`` sidecar dir on first use and writes the snapshot via
    :func:`~brutex.data.atomic_io.atomic_write_simple` (tmp → ``fsync`` → ``os.replace``),
    so a concurrent reader always sees either the old or the new whole file, never a torn
    one. The whole write runs inside :func:`~brutex.data.atomic_io.lake_write_guard`, so a
    full-disk ENOSPC at the dir-create or the file write maps to a
    :class:`~brutex.data.errors.LakeWriteHalt` (consistent with every other lake-write
    site, DEC-LAKE-WRITE-HALT-001) rather than crashing the pull with a raw ``OSError``.

    Parameters
    ----------
    lake_root
        The lake root (locates the ``.rate`` sidecar dir).
    effective_rps, current_rps, recent_minute, recent_429, ceiling_per_min
        The MEASURED rate fields straight from the controller + call-meter snapshots
        (mirroring :class:`~brutex.dashboard.app.RateLimitPanel`).
    wall_time
        ``time.time()`` at the tick (the cross-process freshness clock).
    monotonic
        ``time.monotonic()`` at the tick (in-process diagnostic parity only).
    recent_1s, recent_600s, recent_3600s
        The controller's MEASURED trailing 1 s / 600 s / 3600 s real issue counts
        (DEC-RATE-MONITOR-EXPAND-001), default ``0`` so a caller without them publishes an
        honest zero rather than omitting the field.
    pull_active
        The AUTHORITATIVE pull-run signal (DEC-CLI-BRIDGE-PULL-ACTIVE-001) — ``True``
        per-window during the pull, ``False`` on the final end-of-pull publish, default
        ``None`` (no signal — every pre-existing call site stays byte-compatible).
    """
    snapshot = RateBridgeSnapshot(
        schema=_SCHEMA_VERSION,
        effective_rps=effective_rps,
        current_rps=current_rps,
        recent_minute=recent_minute,
        recent_429=recent_429,
        ceiling_per_min=ceiling_per_min,
        recent_1s=recent_1s,
        recent_600s=recent_600s,
        recent_3600s=recent_3600s,
        wall_time=wall_time,
        monotonic=monotonic,
        pull_active=pull_active,
    )
    dest = rate_bridge_path(lake_root)
    with lake_write_guard():
        dest.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_simple(dest, lambda tmp: tmp.write_bytes(snapshot.to_json_bytes()))


def publish_from_snapshots(
    lake_root: Path,
    controller_snap: Mapping[str, Any],
    meter_snap: Mapping[str, Any],
    *,
    wall_time: float,
    monotonic: float,
    pull_active: bool | None = None,
) -> None:
    """Publish a bridge snapshot from raw controller + call-meter snapshot mappings.

    The same ``.get(...)`` field mapping the dashboard's
    :func:`~brutex.dashboard.app.make_rate_observer` uses, so the bridge file carries
    EXACTLY the fields the in-memory panel does (CLAUDE.md §11 COMMON — one mapping
    contract). Missing keys default safely (no ``KeyError``): a snapshot absent
    ``recent_minute`` / ``ceiling_per_min`` (e.g. a controller with no governor) publishes
    ``0`` / ``-1`` exactly as the panel observer maps them. Delegates the atomic, disk-full-
    guarded write to :func:`publish_rate_snapshot`.

    Parameters
    ----------
    lake_root
        The lake root (locates the ``.rate`` sidecar dir).
    controller_snap
        A :meth:`~brutex.adapters.rate_common.RateController.snapshot` mapping
        (``current_rps`` / ``recent_minute`` / ``recent_hits`` / ``ceiling_per_min``).
    meter_snap
        A :meth:`~brutex.adapters.call_meter.CallMeter.snapshot` mapping (``effective_rps``).
    wall_time
        ``time.time()`` at the tick (the cross-process freshness clock).
    monotonic
        ``time.monotonic()`` at the tick (in-process diagnostic parity only).
    pull_active
        The AUTHORITATIVE pull-run signal (DEC-CLI-BRIDGE-PULL-ACTIVE-001), forwarded to
        :func:`publish_rate_snapshot`; default ``None`` keeps every existing call site
        byte-compatible.
    """
    # ``ceiling_per_min`` is ``None`` in the snapshot when no governor is wired
    # (DEC-GROWW-RATE-PERMIN-001) — map that to the ``-1`` "no cap" sentinel the panel
    # observer uses, so the bridge file and the in-memory panel agree.
    per_min_cap = controller_snap.get("ceiling_per_min")
    publish_rate_snapshot(
        lake_root,
        effective_rps=float(meter_snap.get("effective_rps", 0.0)),
        current_rps=float(controller_snap.get("current_rps", 0.0)),
        recent_minute=int(controller_snap.get("recent_minute", 0)),
        recent_429=int(controller_snap.get("recent_hits", 0)),
        ceiling_per_min=int(per_min_cap) if per_min_cap is not None else -1,
        # The expanded-monitor trailing-window real counts (DEC-RATE-MONITOR-EXPAND-001);
        # absent on an older controller snapshot → default 0 (no KeyError, honest zero).
        recent_1s=int(controller_snap.get("recent_1s", 0)),
        recent_600s=int(controller_snap.get("recent_600s", 0)),
        recent_3600s=int(controller_snap.get("recent_3600s", 0)),
        wall_time=wall_time,
        monotonic=monotonic,
        pull_active=pull_active,
    )


def read_rate_snapshot(lake_root: Path) -> RateBridgeSnapshot | None:
    """Best-effort read the lake-root bridge snapshot, or ``None`` (never raises).

    Returns ``None`` for EVERY non-happy case — the file is absent (no CLI pull has run),
    unreadable (a permission / IO error), not valid JSON (a partial / corrupt write), not
    a JSON object, carries an unrecognised :data:`_SCHEMA_VERSION`, or is missing / has the
    wrong type for any required field. A damaged bridge file can therefore NEVER break the
    dashboard read (Golden Rule #1 — degrade honestly, never crash, never fabricate).

    Parameters
    ----------
    lake_root
        The lake root (locates the ``.rate`` sidecar dir).

    Returns
    -------
    RateBridgeSnapshot | None
        The decoded snapshot, or ``None`` when nothing valid is present.
    """
    path = rate_bridge_path(lake_root)
    try:
        # fd-gated regular-file read (DEC-LAKE-READ-INODE-GUARD-002): the plain
        # ``path.read_bytes()`` blocked FOREVER on a FIFO planted at the bridge
        # path — the caller (RateBridgeCache._refresh_body) runs on the deadline
        # daemon so the ROUTE stayed bounded, but the wedged daemon never
        # returned, the cache's single-flight ``_inflight`` never cleared, and
        # the bridge read was permanently stuck on last-known until restart.
        # The gate makes the FIFO raise ``NotRegularFileError`` (an ``OSError``
        # — the arm below) instantly: honest ``None``, no zombie, the
        # single-flight self-heals. Regular files read byte-identical bytes.
        raw = read_regular_bytes(path)
    except OSError:
        # Absent (FileNotFoundError), unreadable (permission/IO) or a planted
        # non-regular inode (NotRegularFileError) — no live CLI bridge.
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # A partial / corrupt write (a torn file, or non-UTF-8 bytes). Ignore it.
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema") != _SCHEMA_VERSION:
        # Unrecognised version (incl. a stale pre-#3 v2 file) — forward/backward-safe:
        # never mis-decode another shape; the publisher re-writes within one window tick.
        return None
    # Graceful old-reader default (DEC-CLI-BRIDGE-PULL-ACTIVE-001): a missing / non-bool
    # ``pull_active`` (JSON ``null``, a hand-edited file) reads ``None`` — the honest
    # tick-freshness fallback — never a KeyError, never a fabricated run signal.
    raw_pull_active = data.get("pull_active")
    pull_active = raw_pull_active if isinstance(raw_pull_active, bool) else None
    try:
        return RateBridgeSnapshot(
            schema=_SCHEMA_VERSION,
            effective_rps=float(data["effective_rps"]),
            current_rps=float(data["current_rps"]),
            recent_minute=int(data["recent_minute"]),
            recent_429=int(data["recent_429"]),
            ceiling_per_min=int(data["ceiling_per_min"]),
            recent_1s=int(data["recent_1s"]),
            recent_600s=int(data["recent_600s"]),
            recent_3600s=int(data["recent_3600s"]),
            wall_time=float(data["wall_time"]),
            monotonic=float(data["monotonic"]),
            pull_active=pull_active,
        )
    except (KeyError, TypeError, ValueError):
        # A missing or wrong-typed field (a hand-edited / truncated / alien file).
        return None
