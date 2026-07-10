"""Cross-process LAKE-CONTENT write epoch — the O(1) cheap gate for the L3 startup sweep.

A single lake-root-sibling marker (``<lake_root>/.lake_heal/write_epoch``) whose value is
REFRESHED to a fresh opaque token on every ``bars/``/``options/`` content mutation.
:func:`~brutex.data.lake_heal.run_startup_sweep` compares it against the value recorded in
the last-sweep sentinel: an unchanged value ⇒ no in-band write RECORDED a bump since the
last clean sweep ⇒ an O(1) SKIP (no O(files) :func:`~brutex.data.lake_heal.lake_content_token`
walk, which over a ~2M-file lake cost ~27 min on EVERY boot even for a no-op sentinel
match). The epoch is a freshness HINT with honestly-bounded blind spots (below), never
proof — the full token stays the authority behind it.

**Bump enumeration (soundness input).** The marker is refreshed at the SAME set of
content-mutation chokepoints DEC-LAKE-STATS-MANIFEST-001 already instruments — the fresh /
merge render + the two heal branches in each of :class:`~brutex.data.parquet_writer.ParquetWriter`
and :class:`~brutex.data.option_parquet_writer.OptionParquetWriter`, plus
``lake_heal._quarantine_move`` (the batch-heal removal chokepoint). Greeks-overlay writes
funnel through :class:`~brutex.data.parquet_writer.ParquetWriter` (``write_option_greeks`` →
``ParquetBarSink``), so they are covered transitively. Each bump is placed IMMEDIATELY after
the existing stats-manifest freshness hook but OUTSIDE its body — the stats hooks early-return
under ``BRUTEX_LAKE_STATS_MANIFEST=off``, whereas the epoch bump must be unconditional.

**Honest limits (Golden Rule #10).** The epoch detects only IN-BAND writes — those that go
through the writers. A purely OUT-OF-BAND mutation (a manual ``cp``/``rsync`` restore, an
external tool dropping a ``*.parquet`` under ``bars/``) does NOT bump the epoch and so is
NOT caught by the fast-path skip. Two defences bound that: (1) the full
:func:`~brutex.data.lake_heal.lake_content_token` remains the AUTHORITATIVE fallback — the
fast path is a cheap short-circuit in FRONT of it, never a replacement, so any epoch
uncertainty (absent / unreadable marker, a value that differs) falls through to the exact
pre-existing token semantics; and (2) the sentinel's max-age safety valve
(``BRUTEX_LAKE_HEAL_EPOCH_MAX_AGE_S``, default 7 days) forces a full token/sweep on an
epoch match once the last clean sweep is older than the valve, so any latent out-of-band
drift is caught within a bounded window. This is a deliberate, bounded trade against the
untenable full walk on every boot.

The token is a FRESH-per-bump opaque value (``time_ns`` + ``pid`` + an in-process counter),
not a read-modify-write counter, so it sidesteps the lost-increment race entirely: the only
requirement is "differs from any previously-recorded value". The write is a NON-fsync atomic
replace deliberately (cost honesty): the epoch is a freshness HINT, never proof — a crash
that loses the write leaves the marker at its PRIOR value. USUALLY that prior value already
differs from the sentinel's recorded epoch (⇒ the next boot falls through to the
authoritative full token — sound), but NOT always: when the crash-lost bump was the FIRST
write after a clean-sweep stamp, the marker still EQUALS the stamped epoch and the next boot
Tier-1-skips over the changed bytes until any LATER successful bump or the max-age valve
(default 7 days) forces the full token. Likewise a PERSISTENTLY failing bump (an unwritable
sidecar dir) freezes the marker, degrading the fast path to at-most-valve-age staleness —
surfaced by the ``lake_write_epoch.bump_failed`` WARN (throttled to once per process per
lake root per outage — the #1630 log-volume discipline — never per write, never silent).
Both windows are BOUNDED by the valve plus the token fallback on any epoch
mismatch/absence; neither is claimed impossible (Golden Rule #10). So the bump must NOT
``fsync`` (the ``atomic_io`` helpers do, and are too heavy for the ~10⁵–10⁶ prebuild writes);
``os.replace`` still gives readers atomicity (old-or-new, never torn). Per-write cost
honesty: one small temp-file write + ``os.replace`` (non-fsync) per RENDERED partition
write — O(1), unconditional at the writer chokepoints; a skipped / no-op write (nothing
rendered) bumps NOTHING (call-count pinned by the epoch-bump test suite, not a wall-clock
bench).

NEVER part of run identity (blake3) / partition bytes / ``data_snapshot_hash`` — a
results-safe accelerator (the Bloom precedent). WHEEL-FREE (stdlib + ``structlog`` only) so
the writers can import it at module top without a ``trading_core`` / ``polars`` edge.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import tempfile
import time
from pathlib import Path
from typing import Final

import structlog

_LOG = structlog.get_logger("brutex.data.lake_write_epoch")

__all__ = [
    "EPOCH_RELPATH",
    "bump_write_epoch",
    "epoch_marker_path",
    "read_write_epoch",
]

#: The write-epoch marker, relative to the lake root — the SAME dot-directory as the L3
#: sentinel (``.lake_heal/last_sweep.json``), a lake-root SIBLING of ``bars/``+``options/``
#: that holds no ``*.parquet`` and is therefore invisible to every lake scanner (the
#: ``.locks``/``.manifest``/``.lake_heal`` precedent).
EPOCH_RELPATH: Final[str] = ".lake_heal/write_epoch"

#: Process-local monotonic counter folded into every token so two bumps in the SAME
#: nanosecond by the SAME process still differ (``time_ns`` + ``pid`` alone can collide
#: on a coarse clock). ``itertools.count`` is thread-safe for ``next()`` (a single
#: bytecode-atomic op in CPython).
_COUNTER = itertools.count()

#: Lake roots (``str(lake_root)``) whose bump failure already WARNed this process — the
#: ``bump_failed`` throttle (the #1630 log-volume discipline: the prebuild fires
#: ~10⁵–10⁶ bumps, so an unwritable sidecar must WARN once per process per lake root
#: per OUTAGE, never per write). A SUCCESSFUL bump RE-ARMS its root (discard), so a
#: later, distinct outage WARNs again instead of staying silent forever. Deliberately
#: lock-free (a plain set): a rare concurrent duplicate WARN is benign, and a raced
#: re-arm merely delays the next outage's single WARN — never a raise into a write path.
_BUMP_FAILED_WARNED: set[str] = set()


def epoch_marker_path(lake_root: Path) -> Path:
    """The write-epoch marker path under *lake_root* — pure, O(1).

    See the module docstring for why the marker exists (the O(1) cheap gate the L3
    startup sweep consults before the O(files) content-token walk).
    """
    return lake_root / EPOCH_RELPATH


def _replace_write(dest: Path, data: bytes) -> None:
    """Atomic NON-fsync write: ``mkstemp`` → write → ``os.replace``.

    Deliberately NO ``fsync`` (unlike :func:`~brutex.data.atomic_io.atomic_write_simple`):
    the epoch is a freshness HINT, not data (a crash-lost write falls through to the
    authoritative full token). ``os.replace`` still gives readers atomicity (old-or-new,
    never torn). ``os.fdopen`` takes ownership of the ``mkstemp`` fd and closes it on
    context exit (success OR error), and the ``finally`` unlinks any surviving temp so a
    mid-write failure never litters the sidecar dir.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".write_epoch-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, dest)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def bump_write_epoch(lake_root: Path) -> None:
    """Refresh the write-epoch marker to a fresh opaque token — best-effort, O(1).

    Called at each ``bars/``/``options/`` content-mutation chokepoint (see the module
    docstring). The token is ``<time_ns>-<pid>-<counter>`` in hex — unique per bump within
    and across processes, so ANY bump moves the value away from the sentinel's recorded
    epoch. BEST-EFFORT: an :class:`OSError` (an unwritable sidecar dir, a stray non-directory
    file at the path) logs a ``lake_write_epoch.bump_failed`` WARN (path repr()'d — the
    calling site's ``dest`` is vendor-influenceable) THROTTLED to once per process per lake
    root per outage via :data:`_BUMP_FAILED_WARNED` (a success re-arms the root), and
    returns; it NEVER raises into a write path. HONEST: a persistently-failing bump freezes
    the marker — the fast path degrades to at-most-valve-age staleness (module docstring),
    which is exactly why the outage must stay VISIBLE, once, not silenced by volume.
    """
    dest = epoch_marker_path(lake_root)
    token = f"{time.time_ns():x}-{os.getpid():x}-{next(_COUNTER):x}"
    key = str(lake_root)
    try:
        _replace_write(dest, token.encode("utf-8"))
    except OSError as exc:
        if key not in _BUMP_FAILED_WARNED:
            _BUMP_FAILED_WARNED.add(key)
            _LOG.warning("lake_write_epoch.bump_failed", path=repr(str(dest)), error=repr(str(exc)))
        return
    # A successful bump RE-ARMS the throttle for this root: a later, DISTINCT
    # outage WARNs again (once), instead of hiding behind the first outage's WARN.
    _BUMP_FAILED_WARNED.discard(key)


def read_write_epoch(lake_root: Path) -> str | None:
    """Read the write-epoch marker text; ``None`` on missing/unreadable/empty — O(1).

    Fails to "unknown" (``None``) so an absent / unreadable / empty marker makes the L3
    fast-path SKIP the epoch short-circuit and fall through to the authoritative full
    token — never a skip on uncertainty (Golden Rule #1). "Unreadable" includes
    NON-UTF-8 marker bytes (:class:`UnicodeDecodeError` is a :class:`ValueError`, NOT an
    :class:`OSError` — an escape here would crash the L3 sweep's blanket-guarded boot
    body into permanent inertness instead of the honest ``None`` fallthrough).
    """
    dest = epoch_marker_path(lake_root)
    try:
        text = dest.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text or None
