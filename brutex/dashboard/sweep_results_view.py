"""Read-only viewer for the brute-force sweep leaderboard (DEC-DASHBOARD-RESULTS-PAGE-001).

The ``/results`` dashboard page answers ONE question — *"which combos won the
brute-force sweep, and by how much?"* — over the per-tuple Parquet files the sweep
engine persists under ``<lake>/sweeps`` (``brutex.engine.results_store.ResultsStore``
+ ``brutex.engine.sweep_orchestrator.tuple_store_path``:
``<root>/<instrument>/<tf>/<direction>_k<k>_<digest>.parquet``). This module is the
PURE read side — the dashboard twin of :mod:`brutex.dashboard.db_view` /
:mod:`brutex.dashboard.completeness`: it discovers the per-tuple stores, loads them
via the SAME :class:`~brutex.engine.results_store.ResultsStore` the sweep wrote (one
source of truth — CLAUDE.md §11 COMMON; no re-implemented Parquet schema), ranks the
rows to a top-N leaderboard, and projects each row into a display dict.

READ-ONLY (CLAUDE.md §3): this NEVER touches the brute-force engine, the 52-condition
vocabulary, or the pull/rate path — it only displays results the operator already
produced. The brute-force ENGINE stays NIFTY/BANKNIFTY/SENSEX; this changes no
storage, scope, vocabulary, or rate.

Honest envelope (Golden Rule #10)
---------------------------------
* **Empty state is honest.** No ``<lake>/sweeps`` directory (or zero stores) yields
  an empty leaderboard + ``available=False`` — never a fabricated row.
* **Resilient.** A single corrupt / version-incompatible per-tuple Parquet is
  SKIPPED (logged + counted in ``skipped``), mirroring
  :func:`brutex.dashboard.movers_resilient` / ``db_view._aggregate_parquet_resilient``
  — one bad file never 500s the page.
* **Money is money.** ``net_pnl`` is rendered through :func:`brutex.money.format_inr`
  (paisa → ``₹`` with two decimals); the full-precision metrics (Sharpe / profit
  factor / win-rate) are NEVER rounded (CLAUDE.md §4 — rounding them corrupts the
  ranking). ``net_pnl_rupees`` is the exact :func:`brutex.money.paisa_to_rupees`
  value as a float for client-side sorting.
* **Matched conditions, by name.** Each row carries the decoded condition NAMES of
  its winning mask (the ``condition_names`` the store already derived from
  ``mask_bits`` via the single-source-of-truth vocabulary decoder), so the
  leaderboard reads as the combo's rule, not an opaque mask.

The discovery + read is ``O(stores × rows)`` — bounded by the funnel's final
10²–10⁴ finalist rows per tuple (``TRACK1_BACKTEST_SPEC.md`` §"Result Row Schema").
The route memoises it behind the dashboard write-generation so a repeat poll is an
O(1) cache hit (DEC-MOVERS-O1-LATENCY-001 pattern).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import structlog

from brutex.dashboard.movers_resilient import movers_read_skip_errors
from brutex.engine.results_store import ResultRow, ResultsStore, ResultsStoreError
from brutex.money import format_inr, paisa_to_rupees

_LOG = structlog.get_logger("brutex.dashboard.sweep_results_view")

__all__ = [
    "DEFAULT_RANK_BY",
    "DEFAULT_TOP_N",
    "MAX_TOP_N",
    "RANKABLE_COLUMNS",
    "SWEEPS_DIRNAME",
    "SweepLeaderboard",
    "build_sweep_leaderboard",
]

#: The sub-directory of the lake root that holds the per-tuple sweep stores —
#: mirrors :func:`brutex.dashboard.sweep_runner.default_results_root`
#: (``<lake>/sweeps``), the SAME tree the CLI ``sweep-all`` writes (CLAUDE.md §11).
SWEEPS_DIRNAME: Final[str] = "sweeps"

#: Default leaderboard depth when the caller does not specify one.
DEFAULT_TOP_N: Final[int] = 50

#: Hard cap on the requested leaderboard depth — bounds the projection cost and the
#: JSON payload size regardless of the query string.
MAX_TOP_N: Final[int] = 500

#: The default ranking column — the headline strategy metric (full precision).
DEFAULT_RANK_BY: Final[str] = "sharpe_ratio"

#: The columns a caller may rank the leaderboard by. A strict allow-list (NOT
#: free-form) so an unknown ``rank_by`` is a clean honest fall-back to the default,
#: never an engine error reaching the page. These mirror
#: :data:`brutex.engine.results_store._RANKABLE_FIELDS` (metrics + money + counts) —
#: redeclared as a PUBLIC tuple here (the private engine set is not exported) so the
#: route + template can offer the operator the same choice set.
RANKABLE_COLUMNS: Final[tuple[str, ...]] = (
    "sharpe_ratio",
    "sortino_ratio",
    "calmar_ratio",
    "profit_factor",
    "win_rate",
    "max_drawdown_pct",
    "net_pnl_paisa",
    "gross_profit_paisa",
    "gross_loss_paisa",
    "max_drawdown_paisa",
    "expectancy_paisa",
    "total_trades",
    "winning_trades",
    "losing_trades",
)


@dataclass(frozen=True, slots=True)
class SweepLeaderboard:
    """The projected, ranked sweep leaderboard for one ``/results`` view.

    Attributes
    ----------
    rows
        The top-N ranked rows, each a display dict (see :func:`_project_row`),
        rank ``1`` first.
    available
        ``True`` iff at least one readable per-tuple store was found. ``False`` is
        the honest empty state — no sweep has been run / written yet.
    total_rows
        The number of distinct combo rows discovered across all readable stores
        (before the top-N slice), so the page can show "showing N of M".
    store_count
        How many per-tuple Parquet stores were read successfully.
    skipped
        How many per-tuple Parquet files were SKIPPED as unreadable (corrupt /
        version-incompatible) — surfaced honestly, never hidden.
    rank_by
        The column the leaderboard was ranked by (the resolved value — the default
        when the request named an unknown column).
    instruments
        The sorted distinct instrument labels present across the readable stores —
        the filter dropdown's option set.
    timeframes
        The sorted distinct timeframe labels present — the filter dropdown's set.
    directions
        The sorted distinct direction labels present — the filter dropdown's set.
    """

    rows: tuple[dict[str, Any], ...]
    available: bool
    total_rows: int
    store_count: int
    skipped: int
    rank_by: str
    instruments: tuple[str, ...]
    timeframes: tuple[str, ...]
    directions: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable payload for the ``/api/v1/sweep/leaderboard`` route."""
        return {
            "available": self.available,
            "total_rows": self.total_rows,
            "store_count": self.store_count,
            "skipped": self.skipped,
            "rank_by": self.rank_by,
            "instruments": list(self.instruments),
            "timeframes": list(self.timeframes),
            "directions": list(self.directions),
            "rows": list(self.rows),
        }


def _resolve_rank_by(rank_by: str | None) -> str:
    """Return the ranking column to use — the default for ``None`` / an unknown name.

    A strict allow-list keeps an arbitrary query string from reaching the engine's
    ``top_k`` (which raises on an unknown field); an unrecognised value degrades
    honestly to :data:`DEFAULT_RANK_BY` rather than 500-ing the page (Golden Rule #10).
    """
    if rank_by is not None and rank_by in RANKABLE_COLUMNS:
        return rank_by
    return DEFAULT_RANK_BY


def _clamp_top_n(top_n: int) -> int:
    """Clamp the requested leaderboard depth into ``[1, MAX_TOP_N]``."""
    if top_n < 1:
        return 1
    if top_n > MAX_TOP_N:
        return MAX_TOP_N
    return top_n


def _iter_store_paths(sweeps_root: Path) -> list[Path]:
    """Return every per-tuple Parquet store path under *sweeps_root*, sorted.

    The layout is ``<root>/<instrument>/<tf>/<direction>_k<k>_<digest>.parquet``
    (:func:`brutex.engine.sweep_orchestrator.tuple_store_path`). A non-existent
    root yields an empty list (the honest empty state, never an error). The
    progress sidecar (``<root>/progress/…``) and any cascade JSON are not
    ``*.parquet`` under the two-level instrument/tf tree, so the glob picks up
    only result stores. Sorted so the discovered set is deterministic.
    """
    if not sweeps_root.is_dir():
        return []
    return sorted(sweeps_root.glob("*/*/*.parquet"))


def _load_rows_resilient(paths: Sequence[Path]) -> tuple[list[ResultRow], int, int]:
    """Load every readable store; return ``(rows, store_count, skipped)``.

    A single corrupt / version-incompatible store is SKIPPED (logged + counted),
    mirroring :func:`brutex.dashboard.movers_resilient` so one bad file never 500s
    the page. The engine wraps a damaged Parquet into :class:`ResultsStoreError`
    (DEC-SWEEP-VERIFY-001), and the raw polars/duckdb/OS faults are covered by the
    COMMON :func:`movers_read_skip_errors` skip set — both are caught here; a
    genuine logic error (``ValueError`` / ``KeyError`` …) still propagates.

    Rows are keyed by ``run_id`` across stores so the SAME combo appearing in two
    overlapping stores is counted ONCE (the run-identity dedup the store itself
    guarantees within a file — Golden Rule #4 — extended across files here).
    """
    # The unreadable-store skip set: the engine wraps a damaged Parquet into its OWN
    # ResultsStoreError (DEC-SWEEP-VERIFY-001), and the COMMON movers skip set
    # (polars/duckdb/OSError) is folded in belt-and-suspenders in case a raw fault
    # ever escapes the wrap — caught in ONE handler so both families are coverable.
    skip_errors: tuple[type[BaseException], ...] = (
        ResultsStoreError,
        *movers_read_skip_errors(),
    )
    by_run_id: dict[str, ResultRow] = {}
    store_count = 0
    skipped = 0
    for path in paths:
        try:
            store = ResultsStore.load(path)
        except skip_errors as exc:
            skipped += 1
            _LOG.warning("sweep_results.store_skip", path=str(path), error=str(exc))
            continue
        store_count += 1
        for row in store.rows():
            by_run_id.setdefault(row.run_id, row)
    return list(by_run_id.values()), store_count, skipped


def _matches(
    row: ResultRow, *, instrument: str | None, tf: str | None, direction: str | None
) -> bool:
    """Whether *row* passes the (optional, case-insensitive) instrument/tf/direction filters."""
    if instrument is not None and row.instrument.casefold() != instrument.casefold():
        return False
    if tf is not None and row.tf.casefold() != tf.casefold():
        return False
    return not (direction is not None and row.direction.casefold() != direction.casefold())


def _rank_value(row: ResultRow, rank_by: str) -> int | float:
    """Return *row*'s RAW numeric value for the ranking column (int stays int).

    Each rankable column is uniformly typed across rows — money / count columns are
    ``int`` (int64 paisa / trade counts), the metric columns are ``float`` — so the
    raw value is returned UNCOERCED: an int64 paisa value beyond ``2**53`` keeps its
    exact magnitude (a ``float`` cast would silently lose the low bits and mis-rank
    high-PnL combos). This is the SAME key + tie-break the engine's
    :meth:`brutex.engine.results_store.ResultsStore.top_k` uses (raw column getter,
    ``run_id`` ascending tie-break, descending sort), so the leaderboard's total
    order is byte-identical to ``top_k``'s.
    """
    return getattr(row, rank_by)  # type: ignore[no-any-return]


def _project_row(row: ResultRow, rank: int) -> dict[str, Any]:
    """Project one :class:`ResultRow` into the leaderboard display dict.

    Money (``net_pnl``) goes through :func:`brutex.money.format_inr` (paisa → ``₹``,
    two decimals); ``net_pnl_rupees`` is the exact rupee value as a float for
    client-side sorting. The full-precision metrics (Sharpe / sortino / calmar /
    profit factor / win-rate / max-drawdown-pct / deflated Sharpe) are passed
    through UNROUNDED — rounding them would corrupt the ranking (CLAUDE.md §4).
    ``conditions`` is the human-readable decode the store already derived from
    ``mask_bits`` (empty string ⇒ the "enters every bar" no-condition combo).
    """
    return {
        "rank": rank,
        "run_id": row.run_id,
        "instrument": row.instrument,
        "tf": row.tf,
        "direction": row.direction,
        "mode": row.mode,
        "mask_bits": row.mask_bits,
        "conditions": row.condition_names,
        "net_pnl": format_inr(row.net_pnl_paisa),
        "net_pnl_rupees": float(paisa_to_rupees(row.net_pnl_paisa)),
        "sharpe_ratio": row.sharpe_ratio,
        "sortino_ratio": row.sortino_ratio,
        "calmar_ratio": row.calmar_ratio,
        "profit_factor": row.profit_factor,
        "win_rate": row.win_rate,
        "max_drawdown_pct": row.max_drawdown_pct,
        "deflated_sharpe": row.deflated_sharpe,
        "total_trades": row.total_trades,
        "winning_trades": row.winning_trades,
        "losing_trades": row.losing_trades,
    }


def build_sweep_leaderboard(
    lake_root: Path,
    *,
    instrument: str | None = None,
    tf: str | None = None,
    direction: str | None = None,
    rank_by: str | None = None,
    top_n: int = DEFAULT_TOP_N,
) -> SweepLeaderboard:
    """Build the ranked sweep leaderboard for the ``/results`` page.

    Discovers every per-tuple store under ``<lake_root>/sweeps``, loads them
    resiliently, applies the optional filters, ranks the surviving rows by
    *rank_by* (descending — best first), and projects the top-*top_n* into display
    dicts. Pure + framework-free (no FastAPI), so it is unit-tested directly.

    Parameters
    ----------
    lake_root
        The lake root directory (the sweep tree is ``<lake_root>/sweeps``).
    instrument, tf, direction
        Optional case-insensitive exact-match filters (e.g. ``"NIFTY"`` / ``"5m"``
        / ``"bull"``). ``None`` (the default) means no filter on that axis.
    rank_by
        The column to rank by — one of :data:`RANKABLE_COLUMNS`. ``None`` / an
        unknown value falls back to :data:`DEFAULT_RANK_BY` (never an error).
    top_n
        Leaderboard depth, clamped into ``[1, MAX_TOP_N]``.

    Returns
    -------
    SweepLeaderboard
        The ranked rows + the honest availability / counts / filter option sets.
    """
    resolved_rank = _resolve_rank_by(rank_by)
    depth = _clamp_top_n(top_n)
    sweeps_root = lake_root / SWEEPS_DIRNAME

    paths = _iter_store_paths(sweeps_root)
    all_rows, store_count, skipped = _load_rows_resilient(paths)

    # The filter dropdown option sets come from EVERY discovered row (so a filter
    # the operator can pick is always one that exists), independent of the active
    # filter.
    instruments = tuple(sorted({r.instrument for r in all_rows}))
    timeframes = tuple(sorted({r.tf for r in all_rows}))
    directions = tuple(sorted({r.direction for r in all_rows}))

    filtered = [
        r for r in all_rows if _matches(r, instrument=instrument, tf=tf, direction=direction)
    ]
    # Deterministic rank: by the chosen column descending, ties broken by run_id
    # ascending (the SAME total order ResultsStore.top_k uses — Golden Rule #4), so
    # the leaderboard is identical for any discovery order.
    ordered = sorted(filtered, key=lambda r: r.run_id)
    ordered.sort(key=lambda r: _rank_value(r, resolved_rank), reverse=True)
    rows = tuple(_project_row(r, rank) for rank, r in enumerate(ordered[:depth], start=1))

    return SweepLeaderboard(
        rows=rows,
        available=store_count > 0,
        total_rows=len(filtered),
        store_count=store_count,
        skipped=skipped,
        rank_by=resolved_rank,
        instruments=instruments,
        timeframes=timeframes,
        directions=directions,
    )
