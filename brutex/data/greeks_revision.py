"""Cross-process Greeks-OVERLAY freshness marker — ONE shared touch point.

DEC-GREEKS-BULK-REENRICH-001 fix round 2 introduced the marker for the bulk
Greeks backfill: a sanctioned production lake writer that rewrites option
1-minute partitions' iv/delta overlay (latest-wins) WITHOUT touching the
window ledger was invisible to a running dashboard's
:func:`brutex.dashboard.lake_revision.lake_revision` composite token, so
``/movers`` kept serving the pre-write Greeks until an unrelated pull or a
restart. Fix round 8 (refuter-confirmed) extracted the touch HERE because the
merge-absorbed DEC-CLI-GREEKS-ENRICH-001 pass — ``brutex enrich-greeks`` and
the ``brutex fetch`` post-pull tail, both through
:func:`brutex.data.greeks_enrich_pass.run_greeks_enrich_pass` — is a SECOND
sanctioned ledger-less overlay writer with the identical staleness class, and
CLAUDE.md §11 COMMON forbids a second hand-rolled copy of the touch.

The marker deliberately lives in the BACKFILL's sidecar dot-directory
(:data:`GREEKS_SIDECAR_DIR_NAME` — outside ``bars/`` + ``options/`` like
``.locks``/``.lake_heal``, invisible to every lake scanner): both writers
mutate the SAME overlay surface, so ONE marker file + ONE ``os.stat``
component in the ``lake_revision`` probe covers them. The dir/file literals
are duplicated in :mod:`brutex.dashboard.lake_revision` (that module is
deliberately lean stdlib-only — the ``_SWEEPS_DIRNAME`` precedent) and pinned
equal by test.

This module is WHEEL-FREE (stdlib + :mod:`brutex.data.atomic_io` +
``structlog`` only) so :mod:`brutex.data.greeks_enrich_pass` can import it at
module top without breaking its no-``trading_core``-at-import discipline.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog

from brutex.data.atomic_io import atomic_write_simple

if TYPE_CHECKING:
    from collections.abc import Mapping

_LOG = structlog.get_logger("brutex.data.greeks_revision")

__all__ = [
    "GREEKS_SIDECAR_DIR_NAME",
    "REVISION_MARKER_FILE_NAME",
    "revision_marker_path",
    "touch_greeks_revision_marker",
]

#: The Greeks sidecar dot-directory under the lake root — OUTSIDE ``bars/`` +
#: ``options/`` like ``.locks`` / ``.lake_heal``, so no lake scanner ever sees
#: it. Shared by the bulk backfill (its checkpoint ALSO lives here — see
#: :data:`brutex.data.greeks_backfill.CHECKPOINT_DIR_NAME`, a re-export of
#: this constant) and the DEC-CLI-GREEKS-ENRICH-001 enrich pass.
GREEKS_SIDECAR_DIR_NAME: Final[str] = ".greeks_backfill"

#: The durable cross-process FRESHNESS marker inside
#: :data:`GREEKS_SIDECAR_DIR_NAME`. Every Greeks-overlay writer that writes
#: bars WITHOUT touching the window ledger touches this marker (atomic
#: tmp→``os.replace``), and ``lake_revision`` folds one ``os.stat`` of it into
#: the composite token — a running dashboard sees the new Greeks on its next
#: poll.
REVISION_MARKER_FILE_NAME: Final[str] = "revision.json"


def revision_marker_path(lake_root: Path) -> Path:
    """The cross-process freshness marker path under *lake_root* — pure, O(1).

    See :data:`REVISION_MARKER_FILE_NAME` for why the marker exists (the
    lake-revision component a running dashboard probes so a CLI writer's
    freshly-written Greeks overlay is served without a restart).
    """
    return lake_root / GREEKS_SIDECAR_DIR_NAME / REVISION_MARKER_FILE_NAME


def touch_greeks_revision_marker(
    lake_root: Path, *, fields: Mapping[str, object], failure_event: str
) -> None:
    """Move the cross-process lake-revision token after a bar-writing pass.

    Writes a tiny JSON marker (atomic tmp→``fsync``→``os.replace`` via the
    COMMON :func:`~brutex.data.atomic_io.atomic_write_simple`) whose
    ``mtime``/size :func:`brutex.dashboard.lake_revision.lake_revision` stats
    into its composite token, so a running dashboard's generation-keyed caches
    (``/movers`` iv/delta overlays, ``/db`` totals, …) recompute on the next
    poll instead of serving the pre-write Greeks until an unrelated pull or a
    restart (Golden Rule #1). BEST-EFFORT — an :class:`OSError` (including a
    stray non-directory file at the sidecar dir) logs ONE *failure_event*
    warning and never affects the run: the marker is a freshness signal, never
    data (a dashboard-run writer also bumps in-process via ``mark_dirty``; a
    lost marker there costs nothing, and on the CLI it degrades to the pre-fix
    staleness window, never a wrong value). O(1) per call.

    Parameters
    ----------
    lake_root
        The lake root whose sidecar marker is touched.
    fields
        JSON-serialisable caller identity/tally fields folded into the marker
        payload verbatim (the backfill passes ``backfill_id``/``bars_written``;
        the enrich pass passes ``writer``/``bars_written``); a monotonic
        ``updated_at`` wall-clock stamp is always appended.
    failure_event
        The caller-scoped structlog event name for the best-effort WARN
        (``greeks_backfill.revision_marker_failed`` /
        ``fno.greeks_enrich.revision_marker_failed``).
    """
    dest = revision_marker_path(lake_root)
    payload = json.dumps({**fields, "updated_at": time.time()}, sort_keys=True).encode("utf-8")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_simple(dest, lambda tmp: tmp.write_bytes(payload))
    except OSError as exc:
        _LOG.warning(failure_event, path=str(dest), error=str(exc))
