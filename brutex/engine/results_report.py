"""Read-back leaderboard over a persisted sweep results tree (DEC-SWEEP-WINNERS-BY-NAME-001).

The orchestrator (:mod:`brutex.engine.sweep_orchestrator`) writes one Parquet
:class:`~brutex.engine.results_store.ResultsStore` per grid tuple under a results
root (``<root>/<instrument>/<tf>/<direction>_k<k>_<digest>.parquet``). On their own
those files answer "what survived?" only as opaque BLAKE3 ``run_id`` keys. This
module is the **operator read-back**: it discovers every per-tuple Parquet under a
root, merges them into one in-memory store, and renders a single ranked leaderboard
that shows — for each finalist — the **winning conditions BY NAME**
(``ResultRow.condition_names``, decoded from ``mask_bits`` via the single-source
vocabulary table), alongside net P&L, trade count, win-rate, Sharpe, the
Deflated-Sharpe ratio, and the gate verdict.

Honest envelope (Golden Rule #10)
---------------------------------
Discovery + merge is inherently **O(total finalists)** work and memory — there is no
O(1)-total read of an N-row leaderboard (claiming so would be a hallucination). Each
*per-row* projection (the mask→names decode is already materialised on the row; the
``format_inr`` render) is O(1). The leaderboard is sized for the funnel's final
10²–10⁴ finalists (``TRACK1_BACKTEST_SPEC.md``), so the whole merged set fits in
memory — the same sizing :class:`~brutex.engine.results_store.ResultsStore` assumes.

Every persisted row is, by construction, a **gate survivor** (the sweep only persists
combos that cleared the hard rejection gates — and, when the overfit gate was active,
the Deflated-Sharpe gate too). So the rendered ``gate`` column reads ``PASS`` for
every row; a ``REJECT`` is never persisted and therefore never shown — stated plainly
rather than implied (Golden Rule #10). Pure read + render — no lake, no network.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Final

from brutex.engine.results_store import ResultRow, ResultsStore, ResultsStoreError
from brutex.money import format_inr

#: Friendly ``--sort-by`` aliases → the underlying rankable :class:`ResultRow` column.
#: Kept here as the ONE place the CLI sort vocabulary lives (CLAUDE.md §11 COMMON).
SORT_ALIASES: Final[dict[str, str]] = {
    "sharpe": "sharpe_ratio",
    "sortino": "sortino_ratio",
    "calmar": "calmar_ratio",
    "pnl": "net_pnl_paisa",
    "win": "win_rate",
    "trades": "total_trades",
    "pf": "profit_factor",
}

#: Default leaderboard sort key (the headline risk-adjusted metric).
DEFAULT_SORT_BY: Final[str] = "sharpe"


def discover_result_files(root: Path) -> list[Path]:
    """Return every per-tuple ``*.parquet`` under *root*, in deterministic sorted order.

    Recursively globs ``**/*.parquet`` so the orchestrator's
    ``<root>/<instrument>/<tf>/<file>.parquet`` layout is fully covered. The sorted
    order makes the merge + the leaderboard reproducible regardless of filesystem
    enumeration order (Golden Rule #4).

    Parameters
    ----------
    root
        The grid results root directory.

    Returns
    -------
    list[pathlib.Path]
        Sorted Parquet paths (empty when none exist under *root*).

    Raises
    ------
    ResultsStoreError
        If *root* does not exist or is not a directory.
    """
    if not root.is_dir():
        raise ResultsStoreError(f"results root not found or not a directory: {root}")
    return sorted(root.glob("**/*.parquet"))


def load_results_tree(root: Path) -> ResultsStore:
    """Merge every per-tuple Parquet under *root* into one in-memory store.

    Each tuple's finalists carry globally-unique BLAKE3 ``run_id`` keys (the 9-field
    run-identity folds in instrument / tf / direction / mode / params), so merging
    across files never aliases — :meth:`ResultsStore.append` dedups idempotently and
    a genuine cross-file key clash (data corruption) raises rather than silently
    overwriting (Golden Rule #4).

    Parameters
    ----------
    root
        The grid results root directory.

    Returns
    -------
    ResultsStore
        A store holding every discovered finalist (empty when no files exist). Its
        ``path`` is set to *root* for reference only — this store is read-only here
        and is never flushed.

    Raises
    ------
    ResultsStoreError
        If *root* does not exist, ``polars`` is missing, or a cross-file ``run_id``
        conflict is detected.
    """
    return _merge_files(root, discover_result_files(root))


def _merge_files(root: Path, files: Sequence[Path]) -> ResultsStore:
    """Merge already-discovered *files* into one read-only store rooted at *root*."""
    merged = ResultsStore(path=root)
    for file in files:
        merged.extend(ResultsStore.load(file).rows())
    return merged


def _resolve_sort_field(sort_by: str) -> str:
    """Map a friendly ``--sort-by`` alias to its :class:`ResultRow` column.

    Raises
    ------
    ResultsStoreError
        If *sort_by* is not a known alias.
    """
    field = SORT_ALIASES.get(sort_by)
    if field is None:
        raise ResultsStoreError(
            f"unknown sort key {sort_by!r}; choose one of {sorted(SORT_ALIASES)}"
        )
    return field


def _format_deflated(value: float | None) -> str:
    """Render the Deflated-Sharpe column — full precision, NEVER rounded; ``None`` → ``—``."""
    if value is None:
        return "—"
    return f"{value:+.4f}"


def _format_conditions(condition_names: str) -> str:
    """Render the winning conditions — the stored NAMES, or a clear no-condition note."""
    if not condition_names:
        return "(no conditions — enters every bar)"
    return condition_names


def render_leaderboard(rows: Sequence[ResultRow]) -> str:
    """Render *rows* (already in leaderboard order) as the readable winners table.

    Each finalist renders as two lines: a headline row (rank · instrument · tf ·
    direction · net P&L · #trades · win-rate · Sharpe · Deflated-Sharpe · gate) and an
    indented condition-NAMES line. Money goes through :func:`brutex.money.format_inr`
    (2dp, DEC-MONEY-2DP-001); statistical metrics are only *displayed* to a fixed
    width — the stored values are never rounded.

    Parameters
    ----------
    rows
        The finalists to render, in the desired order (see :func:`results_show_report`).

    Returns
    -------
    str
        The rendered table (a single placeholder line when *rows* is empty).
    """
    if not rows:
        return "(no persisted results)"
    lines: list[str] = []
    for rank, row in enumerate(rows, start=1):
        lines.append(
            f"#{rank}  {row.instrument} {row.tf} {row.direction}  "
            f"pnl={format_inr(row.net_pnl_paisa)}  "
            f"trades={row.total_trades}  "
            f"win={row.win_rate * 100:.1f}%  "
            f"sharpe={row.sharpe_ratio:+.3f}  "
            f"dsr={_format_deflated(row.deflated_sharpe)}  "
            f"gate=PASS\n"
            f"    {_format_conditions(row.condition_names)}"
        )
    return "\n".join(lines)


def results_show_report(root: Path, *, top: int, sort_by: str = DEFAULT_SORT_BY) -> str:
    """Load the results tree under *root* and render the top-*top* winners leaderboard.

    Parameters
    ----------
    root
        The grid results root directory (the orchestrator's ``--results-root``).
    top
        How many leaders to show (``>= 1``).
    sort_by
        A :data:`SORT_ALIASES` key (default :data:`DEFAULT_SORT_BY`).

    Returns
    -------
    str
        A self-describing header (root · file count · finalist count · sort key)
        followed by the ranked winners table.

    Raises
    ------
    ResultsStoreError
        If *root* does not exist, ``top < 1``, *sort_by* is unknown, ``polars`` is
        missing, or a cross-file ``run_id`` conflict is detected.
    """
    if top < 1:
        raise ResultsStoreError(f"top must be >= 1, got {top}")
    field = _resolve_sort_field(sort_by)
    files = discover_result_files(root)
    store = _merge_files(root, files)
    leaders = store.top_k(min(top, len(store)), by=field) if len(store) else ()
    header = (
        f"BruteX results — {root}\n"
        f"  files={len(files)}  finalists={len(store)}  "
        f"sort={sort_by}  showing top {len(leaders)}\n"
        f"  (every persisted row is a gate survivor → gate=PASS for all)"
    )
    return f"{header}\n\n{render_leaderboard(leaders)}"


__all__ = [
    "DEFAULT_SORT_BY",
    "SORT_ALIASES",
    "discover_result_files",
    "load_results_tree",
    "render_leaderboard",
    "results_show_report",
]
