"""Pure serve-time logic for ``GET /api/v1/sweep/temporal`` (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001).

The ``/results`` row expander's drill endpoint answers ONE question per
``(run_id, horizon)`` — *"how did this combo's trades distribute over calendar
periods?"* — from STORED bytes only (the zero-illusion law, graft 11):

* ``quarter`` / ``month`` — decoded from the row's stored per-period columns
  (:mod:`brutex.engine.period_stats`; written at rank time by the finalist
  fold). ``quarter`` additionally derives the half-year roll-up server-side
  (n/pnl/wins exact sums; half-year max-DD is NOT stored and NOT derivable —
  rendered ``—`` client-side, the legend says so).
* ``day`` / ``week`` — aggregated from the #1275 results-depth TRADES sidecars
  via the existing :func:`brutex.engine.trade_store.iter_trades_for_export`
  (IST exit-date attribution — the SAME
  :func:`brutex.engine.session_lens.ist_trade_date` law the stored buckets
  used). Buckets carry ``[n, pnl, wins]`` ONLY — no max-DD (close-only
  per-trade equity at day/week granularity is noise; legend). The dashboard
  NEVER re-simulates: absent artifacts (depth pass off / pre-#1617 run /
  non-captured combo) yield a loud ``awaiting`` payload, never a 500, never a
  fabricated bucket.

Honest cost envelope (Golden Rule #10): the sidecar INDEX build walks
``<lake>/sweep_runs/*/results-depth/**/*.stamp.json`` — O(run dirs × stamps)
on the FIRST build per :func:`sweep_runs_token`, then cached; a cold ``day``/
``week`` drill folds O(trades of ONE combo) in bounded-memory batches; a
repeat drill is an O(1) cache hit keyed on the stamp's stat token. The
``quarter``/``month`` reads are O(1) per click off the revision-cached
:class:`~brutex.dashboard.sweep_results_view.SweepScan` row index.

READ-ONLY (CLAUDE.md §3): display zone only — the brute-force ENGINE stays
NIFTY/BANKNIFTY/SENSEX; no storage, scope, vocabulary, or rate change.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import structlog

from brutex.engine.period_stats import decode_period_stats
from brutex.engine.session_lens import ist_trade_date
from brutex.engine.trade_store import (
    RESULTS_DEPTH_DIRNAME,
    SWEEP_RUNS_DIRNAME,
    SidecarPaths,
    TradeStoreError,
    iter_trades_for_export,
    load_stamp,
)

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from brutex.engine.results_store import ResultRow

_LOG = structlog.get_logger("brutex.dashboard.sweep_temporal_view")

__all__ = [
    "STAMP_SUFFIX",
    "TEMPORAL_HORIZONS",
    "build_sidecar_index",
    "is_valid_run_id",
    "sidecar_horizon_payload",
    "stamp_stat_token",
    "stored_horizon_payload",
    "sweep_runs_token",
]

#: The endpoint's horizon vocabulary — ``quarter``/``month`` serve stored row
#: columns; ``day``/``week`` serve the results-depth trades sidecars.
TEMPORAL_HORIZONS: Final[tuple[str, ...]] = ("quarter", "month", "day", "week")

#: The per-tuple sidecar stamp filename suffix (mirrors
#: :func:`brutex.engine.trade_store.sidecar_paths_for` — ``<stem>.stamp.json``).
STAMP_SUFFIX: Final[str] = ".stamp.json"

#: 64-char lowercase-hex run identity (the store's ``run_id`` grammar). A
#: hostile value is rejected BEFORE any disk touch (spy-pinned by test).
_RUN_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")

#: The last quarter of the FIRST half-year (Q1+Q2 → H1, Q3+Q4 → H2).
_Q2: Final[int] = 2


def is_valid_run_id(run_id: str) -> bool:
    """``True`` iff *run_id* is a 64-char lowercase-hex run identity.

    ``fullmatch`` (not ``match`` + ``$``): ``$`` tolerates a trailing newline —
    the exact class of hostile suffix this guard exists to reject.
    """
    return _RUN_ID_RE.fullmatch(run_id) is not None


def _absent(detail: str, *, run_id: str, horizon: str) -> dict[str, Any]:
    """The honest no-stored-data payload (pre-extension row / unknown run)."""
    return {"status": "absent", "run_id": run_id, "horizon": horizon, "detail": detail}


def _awaiting(detail: str, *, run_id: str, horizon: str) -> dict[str, Any]:
    """The loud awaiting-artifacts payload (results-depth sidecar not found)."""
    return {"status": "awaiting", "run_id": run_id, "horizon": horizon, "detail": detail}


def _half_year_rollup(quarter_cells: dict[str, tuple[int, int, int, int]]) -> dict[str, list[int]]:
    """Derive the half-year ``[n, pnl, wins]`` roll-up from stored quarter cells.

    Exact integer sums (Q1+Q2 → H1, Q3+Q4 → H2) — max-DD is deliberately
    ABSENT: a half-year drawdown is not derivable from segmented quarter DDs
    (the per-period max-DD semantics law) and is never fabricated.
    """
    halves: dict[str, list[int]] = {}
    for key, (n_trades, pnl, wins, _dd) in quarter_cells.items():
        year, quarter = key[:4], int(key[5])
        half = f"{year}H{1 if quarter <= _Q2 else 2}"
        cell = halves.setdefault(half, [0, 0, 0])
        cell[0] += n_trades
        cell[1] += pnl
        cell[2] += wins
    return dict(sorted(halves.items()))


def stored_horizon_payload(row: ResultRow | None, *, run_id: str, horizon: str) -> dict[str, Any]:
    """Serve a ``quarter``/``month`` drill from the STORED row columns.

    ``row=None`` (the run_id matched no discovered store row) and a
    pre-extension row (columns ``None`` — the all-or-none store law) both read
    as honest ``absent`` payloads with distinct remediation detail; a populated
    row serves its decoded cells verbatim (``[n_trades, net_pnl_paisa, wins,
    max_dd_paisa]`` value order — stored values only, never recomputed).
    """
    if row is None:
        return _absent(
            "unknown run_id — not present in any readable sweep store",
            run_id=run_id,
            horizon=horizon,
        )
    payload_json = row.stats_by_quarter_json if horizon == "quarter" else row.stats_by_month_json
    if payload_json is None:
        return _absent(
            "this run predates temporal capture — re-sweep to populate",
            run_id=run_id,
            horizon=horizon,
        )
    cells = decode_period_stats(payload_json, kind=horizon)
    payload: dict[str, Any] = {
        "status": "ok",
        "source": "stored",
        "run_id": run_id,
        "horizon": horizon,
        "cells": {key: list(cell) for key, cell in cells.items()},
    }
    if horizon == "quarter":
        payload["half_year"] = _half_year_rollup(cells)
    return payload


def sweep_runs_token(lake_root: Path) -> str | None:
    """Return the ``<lake>/sweep_runs`` invalidation token, or ``None``.

    A digest over the tree's FIRST level — each child run-dir's
    ``name:mtime_ns`` plus the root dir's own mtime — so a new depth-pass run
    dir OR a rebuilt run dir moves the token (the sidecar writes land under the
    run dir, moving its mtime through the subtree's dir-entry updates is NOT
    guaranteed on every FS, so the per-payload cache additionally keys on the
    served stamp's own stat token — :func:`stamp_stat_token`). An absent tree
    reads the stable ``"absent"``; any other :class:`OSError` reads ``None`` =
    "no signal, cache nothing" (the ``lake_revision`` doctrine).
    """
    root = lake_root / SWEEP_RUNS_DIRNAME
    try:
        st = os.stat(root)
        entries = sorted(
            (entry.name, entry.stat(follow_symlinks=False).st_mtime_ns)
            for entry in os.scandir(root)
        )
    except (FileNotFoundError, NotADirectoryError):
        return "absent"
    except OSError as exc:
        _LOG.warning(
            "sweep_temporal_view.sweep_runs_token_failed",
            path=str(root),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None
    digest = hashlib.sha256()
    digest.update(f"{st.st_mtime_ns}".encode())
    for name, mtime_ns in entries:
        digest.update(f"|{name}:{mtime_ns}".encode())
    return digest.hexdigest()


def stamp_stat_token(paths: SidecarPaths) -> str:
    """One stamp file's ``"<mtime_ns>:<size>"`` token (``"absent"`` when gone)."""
    try:
        st = os.stat(paths.stamp)
    except OSError:
        return "absent"
    return f"{st.st_mtime_ns}:{st.st_size}"


def _paths_from_stamp(stamp: Path) -> SidecarPaths:
    """Reconstruct the sidecar trio paths from a discovered stamp file path.

    The naming law is :func:`brutex.engine.trade_store.sidecar_paths_for`'s
    (``<stem>.{trades,aggs,equity}.parquet`` + ``<stem>.stamp.json``) —
    path-mechanical, pinned by test against a REAL writer-produced trio.
    """
    stem = stamp.name[: -len(STAMP_SUFFIX)]
    base = stamp.parent
    return SidecarPaths(
        trades=base / f"{stem}.trades.parquet",
        aggs=base / f"{stem}.aggs.parquet",
        equity=base / f"{stem}.equity.parquet",
        stamp=stamp,
        # Merge-forward reconcile: main's DEC-RESILIENCE-STREAKS-001 extended
        # SidecarPaths with the resilience sibling pair — mirrored here on the
        # ONE sidecar_paths_for naming law (<stem>.resilience.parquet +
        # <stem>.resilience.stamp.json).
        resilience=base / f"{stem}.resilience.parquet",
        resilience_stamp=base / f"{stem}.resilience.stamp.json",
    )


def build_sidecar_index(lake_root: Path) -> dict[str, SidecarPaths]:
    """Build the ``run_id → SidecarPaths`` index over every results-depth stamp.

    Walks ``<lake>/sweep_runs/*/results-depth/**/*.stamp.json`` in SORTED path
    order and folds each readable stamp's ``trade_spans`` run_ids. A duplicate
    run_id across stamps keeps the FIRST (sorted-order deterministic — run
    identity is content-addressed, so a legitimate duplicate carries identical
    trades); an unreadable/invalid stamp is SKIPPED + logged (one WARN each),
    never a crash — the drill then reads ``awaiting`` for its combos, which is
    the honest state for artifacts that cannot be served.

    Honest cost (Golden Rule #10): O(run dirs × stamp files) glob + one stamp
    parse each — paid once per :func:`sweep_runs_token`, then cached by the
    route; NEVER claimed O(1)-total.
    """
    root = lake_root / SWEEP_RUNS_DIRNAME
    index: dict[str, SidecarPaths] = {}
    if not root.is_dir():
        return index
    for stamp in sorted(root.glob(f"*/{RESULTS_DEPTH_DIRNAME}/**/*{STAMP_SUFFIX}")):
        paths = _paths_from_stamp(stamp)
        # load_stamp maps EVERY damage class to None (its own reason-naming
        # WARN fires inside) — it never raises, so no try/except here.
        record = load_stamp(paths, verify_hashes=False)
        if record is None:
            _LOG.warning("sweep_temporal_view.stamp_invalid", path=str(stamp))
            continue
        for combo_run_id in record.trade_spans:
            index.setdefault(combo_run_id, paths)
    return index


def _period_key(exit_ts_iso: str, *, horizon: str) -> str:
    """The IST-exit-date bucket key: ``YYYY-MM-DD`` (day) / ``GGGG-Www`` (ISO week)."""
    exit_date = ist_trade_date(datetime.fromisoformat(exit_ts_iso))
    if horizon == "day":
        return exit_date.isoformat()
    iso_year, iso_week, _ = exit_date.isocalendar()
    return f"{iso_year:04d}-W{iso_week:02d}"


def sidecar_horizon_payload(
    paths: SidecarPaths | None, *, run_id: str, horizon: str
) -> dict[str, Any]:
    """Serve a ``day``/``week`` drill from the results-depth trades sidecar.

    Streams the combo's trades in bounded-memory batches
    (:func:`iter_trades_for_export`) and folds ``[n, pnl, wins]`` buckets by
    IST exit date / ISO week — stored trade rows only, never a re-simulation.
    ``paths=None`` (the run_id is in no stamp's ``trade_spans``) and a
    mid-serve :class:`TradeStoreError` (stamp vanished / torn trades file)
    both degrade to the loud ``awaiting`` payload.
    """
    if paths is None:
        return _awaiting(
            "results-depth artifacts not found for this run — re-run the sweep with the "
            "results-depth pass enabled (BRUTEX_RESULTS_DEPTH) to capture per-trade drills",
            run_id=run_id,
            horizon=horizon,
        )
    buckets: dict[str, list[int]] = {}
    try:
        for trade in iter_trades_for_export(paths, run_id):
            key = _period_key(trade["exit_ts"], horizon=horizon)
            cell = buckets.setdefault(key, [0, 0, 0])
            cell[0] += 1
            cell[1] += trade["pnl_paisa"]
            cell[2] += 1 if trade["pnl_paisa"] > 0 else 0
    except TradeStoreError as exc:
        _LOG.warning(
            "sweep_temporal_view.sidecar_unreadable",
            path=str(paths.stamp),
            run_id=run_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return _awaiting(
            "results-depth sidecar unreadable — rebuild via the run-end results-depth pass",
            run_id=run_id,
            horizon=horizon,
        )
    return {
        "status": "ok",
        "source": "results_depth_sidecar",
        "run_id": run_id,
        "horizon": horizon,
        "cells": dict(sorted(buckets.items())),
    }
