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
* **Snapshot-era honesty (DEC-RESULTS-SURFACE-HONESTY-001).** Stores swept
  against DIFFERENT lake data (distinct ``data_snapshot_hash`` eras — e.g.
  pre-heal poisoned-era results next to post-heal ones) are co-ranked in ONE
  table; the payload now says so LOUDLY instead of hiding it: the tree's audit
  chain (``<sweeps>/audit/sweep-all.jsonl`` ``SWEEP_START`` payloads — the same
  self-description :mod:`brutex.engine.results_report` reads) yields
  ``snapshot_hashes`` + ``mixed_snapshot_eras`` + a per-row ``snapshot_era``
  marker. Rows are deliberately NOT excluded — exclusion without the at-rest
  census of dup-train PR-3 could hide legitimate multi-era research (an operator
  comparing eras on purpose) and could not even pick WHICH era is "current" from
  the read side; the honest marker kills the one-clean-table ILLUSION while the
  census/heal (PR-3) remains the quarantine authority. A tree with NO readable
  audit signal marks every row ``unknown`` with an EMPTY ``snapshot_hashes`` —
  "no signal", never a fabricated single-era verdict.
* **run_id conflicts are counted, never silent.** The same ``run_id`` appearing
  in two stores with DIFFERENT metrics (upstream non-determinism / a healed
  store next to its stale twin) keeps the deterministic FIRST row (sorted store
  order) but is counted in ``run_id_conflicts`` + logged
  (``sweep_results.run_id_conflict`` WARNING). The engine's
  :meth:`~brutex.engine.results_store.ResultsStore.append` RAISES on the same
  conflict (Golden Rule #4) — that strictness stays engine-side; the read-only
  dashboard must still render (DEC-RESULTS-SURFACE-HONESTY-001).

The discovery + read is ``O(stores × rows)`` — bounded by the funnel's final
10²–10⁴ finalist rows per tuple (``TRACK1_BACKTEST_SPEC.md`` §"Result Row Schema")
— plus ONE ``O(audit lines)`` scan of the tree's audit chain for the era markers
(paid on the same cached build, never per poll). The route memoises it behind the
dashboard write-generation so a repeat poll is an O(1) cache hit
(DEC-MOVERS-O1-LATENCY-001 pattern).
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
    "SNAPSHOT_ERA_AMBIGUOUS",
    "SNAPSHOT_ERA_UNKNOWN",
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

#: Where the sweep tree's hash-chained audit log lives under ``<sweeps>`` — the
#: SAME convention every ``brutex sweep-all`` entry point writes (``cli.py``:
#: ``out_root / "audit" / "sweep-all.jsonl"``) and the engine's
#: ``results_report._AUDIT_RELPATH`` reads. Pinned by test against the engine
#: copy, not imported: this module stays a pure dashboard read surface (the
#: ``_TUPLE_DIGEST_LEN`` precedent).
_AUDIT_RELPATH: Final[tuple[str, str]] = ("audit", "sweep-all.jsonl")

#: The per-tuple audit action whose payload records the sweep's reproducibility
#: context (``brutex.engine.sweep_run`` ``_ACTION_START``).
_SWEEP_START_ACTION: Final[str] = "SWEEP_START"

#: Per-row era marker: the tree's audit chain carries NO ``SWEEP_START`` for the
#: row's tuple coordinates (an audit-less tree, a corrupt chain, or a store
#: copied in from elsewhere) — no signal, honestly NOT a single-era claim.
SNAPSHOT_ERA_UNKNOWN: Final[str] = "unknown"

#: Per-row era marker: the row's tuple coordinates were swept under MORE THAN ONE
#: ``data_snapshot_hash`` (e.g. re-swept after a lake heal), so the row's OWN era
#: cannot be pinned from the read side (per-FILE recorded hashes do not exist —
#: the per-tuple filename digest folds the hash but is a one-way BLAKE3; the
#: at-rest census of dup-train PR-3 is the per-file authority, NOT claimed here).
SNAPSHOT_ERA_AMBIGUOUS: Final[str] = "ambiguous"

#: The tuple-coordinate key era markers are resolved by: every field is carried
#: BOTH by a ``SWEEP_START`` payload and by a :class:`ResultRow` (``k`` = the
#: row's ``mask_bits`` popcount — the sweep enumerates exactly-k-of-bits masks).
_EraKey = tuple[str, str, str, str, int]

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
    snapshot_hashes
        The sorted distinct ``data_snapshot_hash`` eras recorded (by the tree's
        audit chain) for the tuple coordinates the discovered rows cover. Empty
        when the chain carries no signal for them (honest "unknown", never a
        fabricated era) — DEC-RESULTS-SURFACE-HONESTY-001.
    mixed_snapshot_eras
        ``True`` iff MORE THAN ONE distinct era is present in
        ``snapshot_hashes`` — the co-ranked rows span different lake data; the
        page renders a loud banner. ``False`` covers BOTH the clean single-era
        tree AND the no-signal tree (``snapshot_hashes`` disambiguates).
    run_id_conflicts
        How many duplicate ``run_id`` rows with DIFFERENT metrics were dropped
        (deterministic first-wins) across stores — counted + logged, never
        silently masked.
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
    snapshot_hashes: tuple[str, ...]
    mixed_snapshot_eras: bool
    run_id_conflicts: int

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
            "snapshot_hashes": list(self.snapshot_hashes),
            "mixed_snapshot_eras": self.mixed_snapshot_eras,
            "run_id_conflicts": self.run_id_conflicts,
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


def _load_rows_resilient(paths: Sequence[Path]) -> tuple[list[ResultRow], int, int, int]:
    """Load every readable store; return ``(rows, store_count, skipped, run_id_conflicts)``.

    A single corrupt / version-incompatible store is SKIPPED (logged + counted),
    mirroring :func:`brutex.dashboard.movers_resilient` so one bad file never 500s
    the page. The engine wraps a damaged Parquet into :class:`ResultsStoreError`
    (DEC-SWEEP-VERIFY-001), and the raw polars/duckdb/OS faults are covered by the
    COMMON :func:`movers_read_skip_errors` skip set — both are caught here; a
    genuine logic error (``ValueError`` / ``KeyError`` …) still propagates.

    Rows are keyed by ``run_id`` across stores so the SAME combo appearing in two
    overlapping stores is counted ONCE (the run-identity dedup the store itself
    guarantees within a file — Golden Rule #4 — extended across files here). A
    duplicate carrying DIFFERENT metrics (upstream non-determinism / a healed
    store next to its stale twin) keeps the deterministic FIRST row — *paths* is
    sorted, so first-wins is discovery-order-independent — but is COUNTED in
    ``run_id_conflicts`` + logged as a ``sweep_results.run_id_conflict`` WARNING
    (DEC-RESULTS-SURFACE-HONESTY-001); an IDENTICAL duplicate stays a silent,
    conflict-free dedup. The engine's ``ResultsStore.append`` RAISES on the same
    divergence (Golden Rule #4) — the dashboard must render regardless, so it
    counts instead of raising (the engine's strictness stays engine-side).
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
    run_id_conflicts = 0
    for path in paths:
        try:
            store = ResultsStore.load(path)
        except skip_errors as exc:
            skipped += 1
            _LOG.warning("sweep_results.store_skip", path=str(path), error=str(exc))
            continue
        store_count += 1
        for row in store.rows():
            existing = by_run_id.get(row.run_id)
            if existing is None:
                by_run_id[row.run_id] = row
            elif existing != row:
                run_id_conflicts += 1
                _LOG.warning(
                    "sweep_results.run_id_conflict",
                    run_id=row.run_id,
                    dropped_from=str(path),
                    policy="first-wins",
                )
    return list(by_run_id.values()), store_count, skipped, run_id_conflicts


def _snapshot_eras(sweeps_root: Path) -> dict[_EraKey, frozenset[str]]:
    """Best-effort map of tuple coordinates → recorded ``data_snapshot_hash`` eras.

    Reads the tree's audit chain (``<sweeps>/audit/sweep-all.jsonl``) — the SAME
    self-description :func:`brutex.engine.results_report._recorded_snapshot_hash`
    reads — collecting every ``SWEEP_START`` payload's
    ``(instrument, tf, direction, mode, k) → data_snapshot_hash``. One
    ``O(audit lines)`` pass, paid once per (generation-cached) leaderboard build.

    Honest envelope (Golden Rule #10): an absent / unreadable / corrupt chain, or
    a payload missing the needed keys, degrades to NO signal for the affected
    coordinates (logged once for a chain-level failure) — never a guess, never a
    500 on the read-only page. Per-FILE recorded hashes do not exist (the
    per-tuple filename digest folds the hash one-way), so a coordinate re-swept
    under two eras honestly maps to BOTH (→ ``ambiguous`` rows); the per-file
    authority is dup-train PR-3's at-rest census, NOT claimed here.
    """
    # Function-local import mirroring results_report._recorded_snapshot_hash:
    # this module's read+render surface stays lean; the audit reader is only
    # paid when a leaderboard is actually built.
    from brutex.orchestrator.audit import AuditError, read_entries

    audit_path = sweeps_root.joinpath(*_AUDIT_RELPATH)
    if not audit_path.exists():
        return {}
    try:
        entries = read_entries(audit_path)
    except AuditError as exc:
        _LOG.warning("sweep_results.era_scan_failed", audit_path=str(audit_path), error=str(exc))
        return {}
    collected: dict[_EraKey, set[str]] = {}
    for entry in entries:
        if entry.action != _SWEEP_START_ACTION:
            continue
        payload = entry.payload
        try:
            key: _EraKey = (
                str(payload["instrument"]),
                str(payload["tf"]),
                str(payload["direction"]),
                str(payload["mode"]),
                int(payload["k"]),
            )
            snapshot = str(payload["data_snapshot_hash"])
        except (KeyError, TypeError, ValueError):
            # A malformed SWEEP_START payload contributes no signal — skipped,
            # never a 500 (the doctor's verify path is where malformation raises).
            continue
        collected.setdefault(key, set()).add(snapshot)
    return {key: frozenset(hashes) for key, hashes in collected.items()}


def _row_era_key(row: ResultRow) -> _EraKey:
    """The row's tuple-coordinate key (``k`` = the exactly-k-of-bits mask popcount)."""
    return (row.instrument, row.tf, row.direction, row.mode, row.mask_bits.bit_count())


def _row_snapshot_era(row: ResultRow, eras: dict[_EraKey, frozenset[str]]) -> str:
    """Resolve one row's era marker — the hash, ``ambiguous``, or ``unknown``."""
    hashes = eras.get(_row_era_key(row))
    if not hashes:
        return SNAPSHOT_ERA_UNKNOWN
    if len(hashes) == 1:
        return next(iter(hashes))
    return SNAPSHOT_ERA_AMBIGUOUS


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


def _project_row(row: ResultRow, rank: int, *, snapshot_era: str) -> dict[str, Any]:
    """Project one :class:`ResultRow` into the leaderboard display dict.

    Money (``net_pnl``) goes through :func:`brutex.money.format_inr` (paisa → ``₹``,
    two decimals); ``net_pnl_rupees`` is the exact rupee value as a float for
    client-side sorting. The full-precision metrics (Sharpe / sortino / calmar /
    profit factor / win-rate / max-drawdown-pct / deflated Sharpe) are passed
    through UNROUNDED — rounding them would corrupt the ranking (CLAUDE.md §4).
    ``conditions`` is the human-readable decode the store already derived from
    ``mask_bits`` (empty string ⇒ the "enters every bar" no-condition combo).
    ``snapshot_era`` is the row's honest data-era marker — the recorded
    ``data_snapshot_hash``, :data:`SNAPSHOT_ERA_AMBIGUOUS`, or
    :data:`SNAPSHOT_ERA_UNKNOWN` (DEC-RESULTS-SURFACE-HONESTY-001).
    """
    return {
        "rank": rank,
        "run_id": row.run_id,
        "snapshot_era": snapshot_era,
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
    all_rows, store_count, skipped, run_id_conflicts = _load_rows_resilient(paths)

    # The filter dropdown option sets come from EVERY discovered row (so a filter
    # the operator can pick is always one that exists), independent of the active
    # filter.
    instruments = tuple(sorted({r.instrument for r in all_rows}))
    timeframes = tuple(sorted({r.tf for r in all_rows}))
    directions = tuple(sorted({r.direction for r in all_rows}))

    # Snapshot-era honesty (DEC-RESULTS-SURFACE-HONESTY-001): the distinct
    # recorded data eras behind the DISCOVERED rows (all of them, independent of
    # the active filter — a mixed tree stays loudly mixed on every filtered view).
    eras = _snapshot_eras(sweeps_root) if all_rows else {}
    covered_eras: set[str] = set()
    for key in {_row_era_key(r) for r in all_rows}:
        covered_eras |= eras.get(key, frozenset())
    snapshot_hashes = tuple(sorted(covered_eras))

    filtered = [
        r for r in all_rows if _matches(r, instrument=instrument, tf=tf, direction=direction)
    ]
    # Deterministic rank: by the chosen column descending, ties broken by run_id
    # ascending (the SAME total order ResultsStore.top_k uses — Golden Rule #4), so
    # the leaderboard is identical for any discovery order.
    ordered = sorted(filtered, key=lambda r: r.run_id)
    ordered.sort(key=lambda r: _rank_value(r, resolved_rank), reverse=True)
    rows = tuple(
        _project_row(r, rank, snapshot_era=_row_snapshot_era(r, eras))
        for rank, r in enumerate(ordered[:depth], start=1)
    )

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
        snapshot_hashes=snapshot_hashes,
        mixed_snapshot_eras=len(snapshot_hashes) > 1,
        run_id_conflicts=run_id_conflicts,
    )
