"""Post-pipeline **discovery-memo warm** — ONE lake walk seeds every discovery cache.

DEC-DB-DISCOVERY-ADAPTIVE-FLOOR-001 (item 1b). The dashboard keeps FOUR
generation-stamped discovery memos over the same ``bars/`` tree (all
:class:`~brutex.dashboard.movers_cache.MoversSnapshotCache` instances in
``build_app``):

1. the ``/db`` datasets triple-walk payload (:data:`DB_DATASETS_KEY` — spot +
   F&O + expired-options lists, ``/api/v1/db/candles/datasets``);
2. the ``/movers`` candle-dataset discovery (:data:`MOVERS_DATASETS_KEY` —
   DEC-MOVERS-DISCOVERY-MEMO-001);
3. the ``/db`` SPOT symbol→interval cascade (:data:`SPOT_CASCADE_KEY` —
   DEC-DB-SPOT-CASCADE-001);
4. the ``/db`` F&O underlying→expiry→contract cascade (:data:`FNO_CASCADE_KEY`
   — DEC-DB-FNO-CASCADE-001);

plus the sibling latest-movers-date memo (:data:`LATEST_MOVERS_DATE_KEY` — the
``/movers`` default-day resolve, GAP #4). After the pull pipeline's final
prebuild stage the dataset count is multiplied by the 19 materialised
timeframes, so the FIRST post-pull open of each surface used to re-pay its own
multi-minute ``O(datasets)`` walk. This module runs the underlying walks ONCE
at the pipeline seam (right after ``run_full_pipeline`` returns, best-effort)
and seeds ALL the memos at the end-of-pull generation — so no route is ever
left holding the pull-time stale-floor state after the pull ends (together
with the pull-active gating this closes the stale-list-forever hazard), and
the first post-pull open of every discovery surface is an O(1) hit.

The cache KEYS live here as the ONE source of truth — the ``build_app`` routes
import them, so the warm and the routes can never silently key apart
(CLAUDE.md §11 COMMON). :func:`build_db_datasets_payload` is likewise the ONE
builder of the ``/api/v1/db/candles/datasets`` payload shape, shared by the
route and the warm (no copy-paste, byte-identical by construction).

## Honest envelope (Golden Rule #10)

* The warm is **best-effort**: any failure logs ``discovery.warm_failed`` and
  returns ``0`` — it can NEVER fail the pull that triggered it. Its absence
  costs latency only (the first post-pull open pays the real walk).
* It is bound at the DASHBOARD ``/pull-pipeline`` seam only; a CLI ``brutex
  fetch`` (a separate process) cannot seed this process's memos — the first
  dashboard read after a CLI pull pays the walk, exactly as before (stated,
  not hidden).
* The seeding itself is O(walk) — the same three directory walks the routes
  would each run; the win is running them ONCE (shared) at a moment the pull
  has already succeeded, instead of once per surface on first open.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import structlog

from brutex.dashboard.db_view import (
    fno_candle_lake_schema,
    list_candle_datasets,
    list_fno_candle_datasets,
    list_option_datasets,
)
from brutex.dashboard.fno_cascade import build_fno_cascade
from brutex.dashboard.movers_warm import latest_movers_date
from brutex.dashboard.spot_cascade import build_spot_cascade
from brutex.data.fno_candle_lake import FNO_CANDLE_LAKE_SEGMENT

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from brutex.dashboard.movers_cache import MoversSnapshotCache

_LOG = structlog.get_logger(__name__)

__all__ = [
    "DB_DATASETS_KEY",
    "FNO_CASCADE_KEY",
    "LATEST_MOVERS_DATE_KEY",
    "MOVERS_DATASETS_KEY",
    "SPOT_CASCADE_KEY",
    "build_db_datasets_payload",
    "filter_spot_datasets",
    "warm_discovery_memos",
]

#: Cache key of the ``/api/v1/db/candles/datasets`` triple-walk payload
#: (DEC-DB-TAB-SWITCH-O1-001) in the app's ``_db_datasets_cache``.
DB_DATASETS_KEY: Final[str] = "db_candle_datasets"
#: Cache key of the ``/movers`` candle-dataset discovery list
#: (DEC-MOVERS-DISCOVERY-MEMO-001) in the SAME ``_db_datasets_cache``.
MOVERS_DATASETS_KEY: Final[str] = "movers_candle_datasets"
#: Cache key of the ``/db`` SPOT cascade index (DEC-DB-SPOT-CASCADE-001).
SPOT_CASCADE_KEY: Final[str] = "db_spot_cascade"
#: Cache key of the ``/db`` F&O cascade index (DEC-DB-FNO-CASCADE-001) — the
#: app's ``_fno_cascade_cache`` is now the COMMON ``MoversSnapshotCache`` too.
FNO_CASCADE_KEY: Final[str] = "db_fno_cascade"
#: Cache key of the ``/movers`` default-day latest-date resolve (GAP #4).
LATEST_MOVERS_DATE_KEY: Final[str] = "latest_movers_date"

#: How many memos one successful warm seeds (the log + return contract).
_MEMOS_SEEDED: Final[int] = 5


def filter_spot_datasets(candle_datasets: list[dict[str, str]]) -> list[dict[str, str]]:
    """Return only the NON-F&O (spot) datasets of one ``list_candle_datasets`` walk.

    The ONE spot-vs-F&O split every consumer shares (CLAUDE.md §11 COMMON —
    the ``/db`` datasets payload, the SPOT cascade build, and the warm all
    call it): an F&O contract bar is NOT spot (DEC-FNO-CANDLE-LAKE-001), so
    the ``segment == "FNO"`` sets are dropped. Pure + O(datasets).
    """
    return [d for d in candle_datasets if d["segment"] != FNO_CANDLE_LAKE_SEGMENT]


def build_db_datasets_payload(
    lake_root: Path,
    *,
    symbol_for_id: Mapping[str, str],
    candle_datasets: list[dict[str, str]] | None = None,
    fno_datasets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the ``/api/v1/db/candles/datasets`` payload — the ONE shared shape.

    Shared by the route (which passes only *lake_root* + *symbol_for_id* and
    lets the walks run here) and :func:`warm_discovery_memos` (which passes its
    already-run *candle_datasets* / *fno_datasets* so the shared walk is never
    duplicated). Byte-identical either way — the pre-walked lists are the SAME
    deterministic, read-only dir-walk results (Golden Rule #7).

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/``).
    symbol_for_id
        ``security_id → symbol_name`` display map for the expired-option
        series labels (DEC-DB-OPTION-DATASET-DECODE-001).
    candle_datasets, fno_datasets
        Optional pre-walked discovery lists (``None`` ⇒ walk now).

    Returns
    -------
    dict
        ``{"datasets": <spot-only>, "fno": …, "fno_schema": …, "options": …}``.
    """
    candles = candle_datasets if candle_datasets is not None else list_candle_datasets(lake_root)
    fno = fno_datasets if fno_datasets is not None else list_fno_candle_datasets(lake_root)
    return {
        "datasets": filter_spot_datasets(candles),
        "fno": fno,
        "fno_schema": fno_candle_lake_schema(),
        "options": list_option_datasets(lake_root, symbol_for_id=symbol_for_id),
    }


def warm_discovery_memos(
    *,
    lake_root: Path,
    generation: int,
    symbol_for_id_fn: Callable[[], Mapping[str, str]],
    db_datasets_cache: MoversSnapshotCache,
    spot_cascade_cache: MoversSnapshotCache,
    fno_cascade_cache: MoversSnapshotCache,
    latest_date_cache: MoversSnapshotCache,
) -> int:
    """Seed every discovery memo at *generation* off ONE shared walk — best-effort.

    Runs the ``bars/`` candle walk ONCE (plus the F&O decode walk and the
    expired-options walk once each) and seeds the five memos listed in the
    module docstring via ``get_or_compute(..., force_fresh=True)`` — the
    ``force_fresh`` matters because the warm executes while the pull job is
    still ``running``, so an opted-in pull-active floor would otherwise serve
    the stale pre-pull entry instead of storing the fresh one.

    Returns the number of memos seeded (:data:`_MEMOS_SEEDED`), or ``0`` when
    ANY step failed — logged ``discovery.warm_failed``, never raised: a warm
    failure must never fail the already-finished pull (the routes then simply
    pay their own walk on first open, exactly the pre-warm behaviour).

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/``).
    generation
        The END-of-pull write generation to stamp every seed with (the same
        generation the routes will read — a seed at an older generation would
        be dead on arrival).
    symbol_for_id_fn
        Zero-arg provider of the ``security_id → symbol_name`` display map
        (called INSIDE the guard — a store read that raises degrades to the
        honest 0, never a crash).
    db_datasets_cache, spot_cascade_cache, fno_cascade_cache, latest_date_cache
        The app's discovery memo instances (injected — CLAUDE.md §4).
    """
    try:
        symbol_for_id = symbol_for_id_fn()
        candles = list_candle_datasets(lake_root)
        fno = list_fno_candle_datasets(lake_root)
        payload = build_db_datasets_payload(
            lake_root,
            symbol_for_id=symbol_for_id,
            candle_datasets=candles,
            fno_datasets=fno,
        )
        db_datasets_cache.get_or_compute(
            DB_DATASETS_KEY, generation=generation, compute=lambda: payload, force_fresh=True
        )
        db_datasets_cache.get_or_compute(
            MOVERS_DATASETS_KEY, generation=generation, compute=lambda: candles, force_fresh=True
        )
        spot_cascade_cache.get_or_compute(
            SPOT_CASCADE_KEY,
            generation=generation,
            compute=lambda: build_spot_cascade(filter_spot_datasets(candles)),
            force_fresh=True,
        )
        fno_cascade_cache.get_or_compute(
            FNO_CASCADE_KEY,
            generation=generation,
            compute=lambda: build_fno_cascade(fno),
            force_fresh=True,
        )
        latest_date_cache.get_or_compute(
            LATEST_MOVERS_DATE_KEY,
            generation=generation,
            compute=lambda: latest_movers_date(lake_root),
            force_fresh=True,
        )
    except Exception as exc:  # pylint: disable=broad-except — best-effort warm
        _LOG.warning("discovery.warm_failed", error=str(exc), error_type=type(exc).__name__)
        return 0
    _LOG.info(
        "discovery.warm_done",
        generation=generation,
        memos=_MEMOS_SEEDED,
        candle_datasets=len(candles),
        fno_datasets=len(fno),
    )
    return _MEMOS_SEEDED
