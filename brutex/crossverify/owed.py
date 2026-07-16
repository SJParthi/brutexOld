"""Durable "cross-verify OWED for <day>" sentinel store.

DEC-CROSSVERIFY-OWED-SENTINEL-001 (umbrella-assurance finding MAJOR-1): the
one-click Pull-Spot flow fires the cross-verify → S3 tail as its LAST step,
downstream of the long prebuild — a box that dies / sleeps / closes
mid-prebuild silently skips the day's same-day publish, and BOTH catchup
scans (:func:`~brutex.crossverify.journal.scan_pending_journals` /
:func:`~brutex.crossverify.journal.scan_withheld_journals`) iterate days that
HAVE a journal, so a NEVER-STARTED day was invisible to every catchup. This
module is the durable owed-marker law both launch surfaces (the dashboard
``/pull-pipeline?scope=spot-only`` arm and the CLI ``brutex pull-all-spot``)
write at pull-tail-commit ENTRY — before the prebuild runs — and the runner
(:mod:`brutex.crossverify.run`) clears once the day reaches a completed
material verdict (published / staged_only / nothing_to_do /
skipped_non_trading / skipped_muhurat).

Layout — one small atomic JSON file per owed day, under the existing
``.crossverify`` sidecar tree (not ``*.parquet`` ⇒ invisible to every lake
scanner)::

    <lake_root>/.crossverify/owed/<YYYY-MM-DD>.json

LAWS (each pinned by ``tests/unit/test_crossverify_owed.py``):

* **names-only scan** — the OWED verdict is carried by the marker file's
  NAME, never its bytes: :func:`list_owed_days` is one ``os.scandir`` of
  entry names (no ``open`` of any entry ⇒ a planted FIFO / device symlink
  can never hang the route poll), so a corrupt / non-regular / tampered
  marker still reads OWED — owed-unknown degrades to owed, never to a
  silent de-arm;
* **content is forensics only** — :func:`load_owed_marker` (used by the
  run-time scan for its honesty WARN) reads through the shared
  regular-file-only fd-capped loader
  (:func:`brutex.dashboard.pull_intent.read_sidecar_bytes` — the #1614
  round-6 ``O_NONBLOCK`` + ``S_ISREG`` template), so even the diagnostic
  read can never hang or crash on a planted inode; corrupt ⇒ ``None`` +
  ONE WARN, the day STAYS owed;
* **best-effort, loud** — :func:`mark_owed` / :func:`clear_owed` never
  raise: a write/unlink failure (read-only volume, a directory planted at
  the marker path) logs ONE ``crossverify.owed_*_failed`` WARN and returns
  ``False`` — the pull is never failed and the failure is never silent;
* **kill-switch coherent** — :func:`mark_owed_for_pull` (the launchers'
  entry) marks ONLY when the ``BRUTEX_CROSSVERIFY_*`` surface resolves AND
  the kill switch is ON: a disabled / unconfigurable cross-verify owes
  nothing (markers must not accumulate under ``BRUTEX_CROSSVERIFY=off``).

O-envelope: mark/clear are O(1) file ops; :func:`list_owed_days` is
O(owed entries) — bounded by the marker lifecycle (cleared per completed
day), never O(lake).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Final

import structlog

from brutex.crossverify.journal import CROSSVERIFY_DIRNAME
from brutex.data.atomic_io import atomic_write_simple

if TYPE_CHECKING:  # pragma: no cover — typing-only imports
    from collections.abc import Mapping
    from pathlib import Path

_LOG = structlog.get_logger("brutex.crossverify.owed")

#: The owed-marker directory under the ``.crossverify`` sidecar tree.
OWED_DIRNAME: Final[str] = "owed"

#: Marker schema version (content is forensics-only — never load-bearing).
OWED_VERSION: Final[int] = 1

#: Size cap for the diagnostic marker read (a real marker is ~120 bytes; the
#: :data:`~brutex.crossverify.journal.MAX_JOURNAL_BYTES` posture, far smaller
#: because nothing here ever needs to grow).
MAX_OWED_MARKER_BYTES: Final[int] = 64 * 1024

#: The marker filename suffix (one atomic JSON file per owed day).
_MARKER_SUFFIX: Final[str] = ".json"


def owed_dir(lake_root: Path) -> Path:
    """``<lake_root>/.crossverify/owed/``."""
    return lake_root / CROSSVERIFY_DIRNAME / OWED_DIRNAME


def owed_marker_path(lake_root: Path, day: date) -> Path:
    """``<lake_root>/.crossverify/owed/<YYYY-MM-DD>.json``."""
    return owed_dir(lake_root) / f"{day.isoformat()}{_MARKER_SUFFIX}"


def mark_owed(lake_root: Path, day: date, *, source: str) -> bool:
    """Atomically write *day*'s owed marker — best-effort, never raises.

    *source* names the launch surface for forensics (``"dashboard-pipeline"``
    / ``"cli-pull-all-spot"``). Idempotent: re-marking an already-owed day
    rewrites the marker (latest launch wins — the day stays owed either way).
    A failure (read-only volume / a directory planted at the marker path /
    disk-full) logs ONE ``crossverify.owed_mark_failed`` WARN and returns
    ``False`` — LOUD, never a crashed pull, never a silent de-arm (the
    launchd deadman + the panel remain the independent backstops).
    """
    dest = owed_marker_path(lake_root, day)
    payload = {
        "version": OWED_VERSION,
        "day": day.isoformat(),
        "marked_at_utc": datetime.now(tz=UTC).isoformat(),
        "source": source,
    }
    body = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False).encode("utf-8")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_simple(dest, lambda tmp: tmp.write_bytes(body))
    except OSError as exc:
        # Best-effort sentinel — never fail the pull. NARROW (round-1
        # hostile review NIT, the sibling clear_owed's discipline): mkdir +
        # the guard-free atomic_write_simple raise only OSError classes
        # here (the payload is plain json.dumps-able data); a genuine code
        # bug still propagates loudly.
        _LOG.warning(
            "crossverify.owed_mark_failed",
            day=day.isoformat(),
            path=str(dest),
            source=source,
            error_type=type(exc).__name__,
            error=str(exc),
            note="owed marker NOT written — the day's cross-verify has no "
            "crash sentinel; the tail still fires on this run",
        )
        return False
    _LOG.info("crossverify.owed_marked", day=day.isoformat(), source=source)
    return True


def mark_owed_for_pull(
    lake_root: Path, day: date, *, source: str, env: Mapping[str, str] | None = None
) -> bool:
    """The launchers' entry: mark *day* owed IFF cross-verify can actually run.

    Resolves the ``BRUTEX_CROSSVERIFY_*`` surface once (fail-SOFT — junk env
    logs ONE WARN and marks nothing: a tail that will read ``not_configured``
    owes nothing a catchup could complete) and skips marking under the
    ``BRUTEX_CROSSVERIFY=off`` kill switch (markers must never accumulate
    while the feature is deliberately off) AND under
    ``BRUTEX_CROSSVERIFY_PUBLISH=off`` — the mark/auto-clearer PARITY law
    (round-1 hostile review MINOR-1): the D3 owed catchup runs only on the
    publish path (``run.py``'s ``config.publish and not config.dry_run``
    gate), so a marker written in staged-only mode would be surfaced forever
    yet never auto-completed. Returns ``True`` iff a marker was durably
    written.
    """
    from brutex.crossverify.config import resolve_crossverify_config

    try:
        config = resolve_crossverify_config(lake_root=lake_root, env=env, day=day)
    except ValueError as exc:
        _LOG.warning(
            "crossverify.owed_mark_skipped",
            day=day.isoformat(),
            source=source,
            reason="config unresolvable",
            error=str(exc),
        )
        return False
    if not config.enabled:
        _LOG.info(
            "crossverify.owed_mark_skipped",
            day=day.isoformat(),
            source=source,
            reason="disabled (BRUTEX_CROSSVERIFY=off)",
        )
        return False
    if not config.publish:
        # Mark/auto-clearer parity: the D3 catchup that COMPLETES an owed
        # day sits behind run.py's publish gate — staged-only mode must not
        # accumulate markers no automatic pass will ever clear.
        _LOG.info(
            "crossverify.owed_mark_skipped",
            day=day.isoformat(),
            source=source,
            reason="publish disabled (BRUTEX_CROSSVERIFY_PUBLISH=off) — the "
            "D3 catchup only completes days on the publish path",
        )
        return False
    return mark_owed(lake_root, day, source=source)


def clear_owed(lake_root: Path, day: date) -> bool:
    """Remove *day*'s owed marker — best-effort, never raises.

    An absent marker is a clean ``True`` (idempotent — the normal case for a
    day no launcher marked). A failed unlink (a non-empty directory planted
    at the marker path / EACCES) logs ONE ``crossverify.owed_clear_failed``
    WARN and returns ``False`` — the day then honestly re-reads OWED and the
    next run's catchup converges it again (never a wrong clear).
    """
    path = owed_marker_path(lake_root, day)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        _LOG.warning(
            "crossverify.owed_clear_failed",
            day=day.isoformat(),
            path=str(path),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return False
    _LOG.info("crossverify.owed_cleared", day=day.isoformat())
    return True


def list_owed_days(lake_root: Path) -> list[date]:
    """Every owed marker day, ascending — NAMES-only, never raises, never opens.

    One ``os.scandir`` of :func:`owed_dir`; an entry is owed iff its NAME
    parses as ``<YYYY-MM-DD>.json`` (the entry's inode TYPE never matters —
    a planted FIFO/symlink/directory still reads owed and is never opened,
    so the route poll can never hang). A junk name logs ONE
    ``crossverify.owed_junk_entry`` WARN per scan entry and is skipped
    (no day to attribute); an unreadable/absent owed dir logs ONE
    ``crossverify.owed_scan_failed`` WARN (absent stays silent — the normal
    empty case) and reads as no owed days.
    """
    root = owed_dir(lake_root)
    days: list[date] = []
    try:
        entries = list(os.scandir(root))
    except FileNotFoundError:
        return []
    except OSError as exc:
        _LOG.warning(
            "crossverify.owed_scan_failed",
            path=str(root),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return []
    for entry in entries:
        name = entry.name
        if name.endswith(_MARKER_SUFFIX):
            try:
                marker_day: date | None = date.fromisoformat(name[: -len(_MARKER_SUFFIX)])
            except ValueError:
                # A ``.json`` entry whose stem is not a calendar date is NOT
                # a marker (no day to attribute) — deliberately routed to the
                # LOUD junk-entry WARN below, never a silent drop. Narrow
                # (`date.fromisoformat` raises exactly ValueError) + observable
                # (the WARN below is this arm's log line, pinned by
                # ``test_junk_names_warned_and_skipped``).
                marker_day = None
            if marker_day is not None:
                days.append(marker_day)
                continue
        _LOG.warning(
            "crossverify.owed_junk_entry",
            path=str(root),
            name=repr(name),  # on-disk-derived — repr()'d (#907)
        )
    return sorted(days)


def load_owed_marker(lake_root: Path, day: date) -> dict[str, object] | None:
    """Diagnostic marker read — loader-disciplined, never raises, never hangs.

    Content is FORENSICS only (the owed verdict is the file's NAME): rides
    the shared regular-file-only fd-capped loader
    (:func:`brutex.dashboard.pull_intent.read_sidecar_bytes` — ``O_NONBLOCK``
    + ``S_ISREG`` on the open descriptor, size cap on the SAME ``fstat``), so
    a planted FIFO / device symlink / oversized / bit-rotted marker reads
    ``None`` + ONE ``crossverify.owed_marker_unreadable`` WARN — the day
    STAYS owed (owed-unknown degrades to owed). Absent reads a silent
    ``None``.
    """
    # Lazy import — the loader lives beside its intent-store consumer; the
    # run.py lazy-dashboard-import precedent keeps this leaf light.
    from brutex.dashboard.pull_intent import read_sidecar_bytes

    path = owed_marker_path(lake_root, day)
    result = read_sidecar_bytes(path, max_bytes=MAX_OWED_MARKER_BYTES)
    if result.shape == "absent":
        return None
    if result.shape != "ok" or result.raw is None:
        _LOG.warning(
            "crossverify.owed_marker_unreadable",
            day=day.isoformat(),
            path=str(path),
            shape=result.shape,
            detail=result.detail,
            note="marker content unreadable — the day STAYS owed (names-only law)",
        )
        return None
    try:
        payload = json.loads(result.raw.decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != OWED_VERSION:
            raise ValueError(f"unsupported owed-marker shape/version at {path}")
    except (UnicodeDecodeError, ValueError, TypeError, OverflowError, RecursionError) as exc:
        # The load_journal degrade set (bit rot / junk JSON / the two
        # stdlib-json escapes) — one WARN, the day stays owed.
        _LOG.warning(
            "crossverify.owed_marker_unreadable",
            day=day.isoformat(),
            path=str(path),
            shape="corrupt",
            detail=f"{type(exc).__name__}: {exc}",
            note="marker content unreadable — the day STAYS owed (names-only law)",
        )
        return None
    return {str(key): value for key, value in payload.items()}


def scan_owed_days(lake_root: Path, *, today: date, lookback_days: int) -> list[date]:
    """PRIOR owed days inside the catchup window, oldest first — the D3 scan.

    The owed twin of :func:`~brutex.crossverify.journal.scan_withheld_journals`
    — same window law (``today − lookback_days … today − 1``; *today*'s own
    marker belongs to the current run, which clears it at its own verdict).
    Days OLDER than the window are KEPT + surfaced with ONE
    ``crossverify.owed_beyond_window`` WARN per scan (remediation: the manual
    ``brutex crossverify run --day <day>`` re-run — its clear-on-verdict
    removes the marker); a FUTURE-dated marker (clock skew / junk) is kept +
    WARNed too, never completed. Each in-window marker's content is
    diagnostically read through :func:`load_owed_marker` (the loader
    discipline — a corrupt marker WARNs once and STAYS owed).
    """
    floor = today - timedelta(days=lookback_days)
    in_window: list[date] = []
    beyond: list[date] = []
    for day in list_owed_days(lake_root):
        if day >= today:
            if day > today:
                beyond.append(day)
            continue  # today's marker belongs to the current run
        if day < floor:
            beyond.append(day)
            continue
        load_owed_marker(lake_root, day)  # diagnostic read — WARNs on corrupt
        in_window.append(day)
    if beyond:
        _LOG.warning(
            "crossverify.owed_beyond_window",
            days=[day.isoformat() for day in sorted(beyond)],
            today=today.isoformat(),
            lookback_days=lookback_days,
            note="owed marker(s) outside the catchup window — KEPT + surfaced; "
            "remediation: brutex crossverify run --day <day> (its clear-on-"
            "verdict removes the marker)",
        )
    return in_window


__all__ = [
    "MAX_OWED_MARKER_BYTES",
    "OWED_DIRNAME",
    "OWED_VERSION",
    "clear_owed",
    "list_owed_days",
    "load_owed_marker",
    "mark_owed",
    "mark_owed_for_pull",
    "owed_dir",
    "owed_marker_path",
    "scan_owed_days",
]
