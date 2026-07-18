"""Deterministic per-tuple results-depth sidecars — trades / aggs / equity + stamp.

DEC-SWEEP-RESULTS-DEPTH-001 (WP1). The run-end capture pass produces, per sweep
tuple store, a trio of Parquet sidecars plus ONE JSON stamp:

* ``<stem>.trades.parquet`` — every captured trade of every persisted finalist,
  row shape EXACTLY the binding P3→P4 trade-row contract (entry/exit ts+px,
  direction, lots, exit_reason, pnl/costs paisa, TF + 1-minute MAE/MFE, VIX);
* ``<stem>.aggs.parquet`` — one per-combo aggregate record (schema pinned here;
  the record VALUES are produced by ``brutex.engine.combo_aggregates`` — this
  module owns only the storage discipline, exactly the results_store split);
* ``<stem>.equity.parquet`` — the per-combo per-session minute-path equity
  series (R27's "equity curve = stored real series");
* ``<stem>.stamp.json`` — the trio's validation stamp (schema version + the
  SOURCE store's content hash + per-file sha256 + row counts + per-combo row
  spans). The stage-stamp idiom: consumers validate BEFORE reading; ANY damage
  ⇒ ``None`` ⇒ deterministic rebuild (R42's mid-chain break law).

All run-end results-depth artifacts live under the per-run directory built by
:func:`sweep_run_dir` — ``<sweep_runs root>/<run_id>/`` where the
``sweep_runs/`` root is a SIBLING of the sweeps results tree (gap-register
Ruling 4, 2026-07-08): the tuple sidecars at
``sweep_run_dir(...)/results-depth/<instrument>/<tf>/…``. NOTHING results-depth
may sit inside the sweeps tree itself, because ``results_report`` +
``sweep_verify`` recursively glob ``**/*.parquet`` under it and a
``*.trades.parquet`` there would crash/mislead them (the earlier
``<sweeps>/results-depth/`` + ``<sweeps>/runs/`` layouts are SUPERSEDED).

## Byte-determinism (Golden Rule #4 / #7)

Mirrors :mod:`brutex.engine.results_store` exactly: rows sorted (``run_id``
ASC, then ``trade_seq`` / ``session_date``), pinned column order + dtypes,
zstd level :data:`~brutex.engine.results_store.ZSTD_LEVEL`, row groups of
:data:`~brutex.engine.results_store.ROW_GROUP_SIZE`, ``statistics=False``,
``use_pyarrow=False``, atomic writes via the COMMON guard-free
:func:`brutex.data.atomic_io.atomic_write_simple`. The three Parquet files are
a pure function of the captures/aggs; only the stamp carries the
non-deterministic ``produced_at_utc`` (regenerated per build — design B4).

## O(page) reads — honest envelope (Golden Rule #10)

Rows are sorted by ``run_id``, so one combo's trades are CONTIGUOUS; the stamp
records each combo's ``[start, count]`` row span. A page read is therefore a
pure positional ``slice(start + offset, limit)`` lazy scan — NO predicate scan
over foreign combos — and the total is an O(1) stamp lookup, never a COUNT
scan. Honest limit: reaching a deep global row offset still skips row groups
up to it via Parquet metadata (an O(row-groups) metadata walk, the disclosed
db_view-style envelope); the CI bench pins the page read FLAT vs total stored
rows at a fixed offset. Zero-trade combos are first-class: a recorded span of
``count == 0`` pages as an empty list with ``total = 0`` and NO file read.
"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, TypedDict, TypeGuard

import structlog

from brutex.data.atomic_io import atomic_write_simple
from brutex.engine.minute_path import PathStatus
from brutex.engine.results_store import ROW_GROUP_SIZE, ZSTD_LEVEL
from brutex.models import BruteXError

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Iterator, Mapping, Sequence
    from pathlib import Path

    from brutex.engine.trade_capture import CapturedTrade, ComboCapture

_LOG = structlog.get_logger("brutex.engine.trade_store")

__all__ = [
    "AGG_COLUMNS",
    "EQUITY_COLUMNS",
    "PAGE_LIMIT_MAX",
    "RESULTS_DEPTH_DIRNAME",
    "SWEEP_RUNS_DIRNAME",
    "TRADES_SCHEMA_VERSION",
    "TRADE_COLUMNS",
    "SidecarPaths",
    "TradePage",
    "TradeRowWire",
    "TradeStoreError",
    "TupleSidecarStamp",
    "iter_trades_for_export",
    "load_stamp",
    "read_trade_page",
    "sidecar_paths_for",
    "sweep_run_dir",
    "write_tuple_sidecars",
]

#: Sidecar-trio schema version — bump on ANY column/dtype/stamp-shape change
#: (a reader seeing an unknown version treats the trio as absent → rebuild).
TRADES_SCHEMA_VERSION: Final[int] = 1

#: The run-artifact root directory name — a SIBLING of the sweeps results
#: tree, never inside it (gap-register Ruling 4; e.g. ``<lake>/sweep_runs/``
#: beside ``<lake>/sweeps/``).
SWEEP_RUNS_DIRNAME: Final[str] = "sweep_runs"

#: The per-run results-depth subtree holding the tuple sidecar trios.
RESULTS_DEPTH_DIRNAME: Final[str] = "results-depth"

#: Page-size clamp ceiling — the db_view ``_LIMIT_MAX`` law, verbatim value.
PAGE_LIMIT_MAX: Final[int] = 500

#: Offset clamp ceiling (hostile offsets never reach arithmetic) — db_view law.
_OFFSET_MAX: Final[int] = 2**63 - 1

#: Streaming batch size for :func:`iter_trades_for_export` (bounded memory).
_EXPORT_BATCH_ROWS: Final[int] = 10_000

#: Dtype tags — resolved to polars dtypes lazily (polars never imports at
#: module import; the results_store convention).
_UTF8: Final[str] = "utf8"
_INT64: Final[str] = "int64"
_FLOAT64: Final[str] = "float64"
_BOOL: Final[str] = "bool"

#: ``<stem>.trades.parquet`` schema — the binding trade-row contract (B1),
#: column order PINNED (byte-determinism law). ``lots`` / ``costs_paisa`` are
#: NULLABLE Int64 and NULL-BY-DESIGN until P5's futures translation lands
#: (gap-register Ruling 18 — never a fabricated 1/0); ``mae_1m_paisa`` /
#: ``mfe_1m_paisa`` / ``vix_entry`` / ``vix_exit`` are nullable; everything
#: else concrete. Timestamps are UTC epoch microseconds (exact ints).
TRADE_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("run_id", _UTF8),
    ("trade_seq", _INT64),
    ("entry_ts_us", _INT64),
    ("exit_ts_us", _INT64),
    ("entry_px", _FLOAT64),
    ("exit_px", _FLOAT64),
    ("direction", _UTF8),
    ("lots", _INT64),
    ("exit_reason", _UTF8),
    ("pnl_paisa", _INT64),
    ("costs_paisa", _INT64),
    ("mae_paisa", _INT64),
    ("mfe_paisa", _INT64),
    ("mae_1m_paisa", _INT64),
    ("mfe_1m_paisa", _INT64),
    ("path_1m_status", _UTF8),
    ("vix_entry", _FLOAT64),
    ("vix_exit", _FLOAT64),
)

#: ``<stem>.equity.parquet`` schema (B3) — one row per (finalist, session).
EQUITY_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("run_id", _UTF8),
    ("session_date", _UTF8),
    ("equity_close_paisa", _INT64),
    ("equity_intraday_low_paisa", _INT64),
    ("trades_closed", _INT64),
)

#: ``<stem>.aggs.parquet`` schema (B2) — one row per persisted finalist. The
#: VALUES are produced by ``brutex.engine.combo_aggregates`` (WP2) and handed
#: here as plain ``{column: value}`` records; THIS tuple is the one schema
#: authority (column set + order + dtypes) both sides import (§11 COMMON).
AGG_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("run_id", _UTF8),
    ("n_trades", _INT64),
    ("zero_trades", _BOOL),
    ("parity_ok", _BOOL),
    ("mae_avg_paisa", _INT64),
    ("mae_worst_paisa", _INT64),
    ("mae_p95_paisa", _INT64),
    ("mfe_avg_paisa", _INT64),
    ("mfe_best_paisa", _INT64),
    ("mfe_p95_paisa", _INT64),
    ("mae_1m_avg_paisa", _INT64),
    ("mae_1m_worst_paisa", _INT64),
    ("mae_1m_p95_paisa", _INT64),
    ("mfe_1m_avg_paisa", _INT64),
    ("mfe_1m_best_paisa", _INT64),
    ("mfe_1m_p95_paisa", _INT64),
    ("trades_with_1m", _INT64),
    ("max_dd_minute_path_paisa", _INT64),
    ("max_dd_minute_path_pct", _FLOAT64),
    ("equity_status", _UTF8),
    ("worst5_days_json", _UTF8),
    ("vix_win_min", _FLOAT64),
    ("vix_win_avg", _FLOAT64),
    ("vix_win_max", _FLOAT64),
    ("vix_loss_min", _FLOAT64),
    ("vix_loss_avg", _FLOAT64),
    ("vix_loss_max", _FLOAT64),
    ("vix_band_lo", _FLOAT64),
    ("vix_band_hi", _FLOAT64),
    ("vix_stamped_trades", _INT64),
    ("vix_missing_trades", _INT64),
    ("pnl_by_year_json", _UTF8),
    ("pnl_by_trend_json", _UTF8),
    ("one_regime_wonder", _BOOL),
    ("regime_wonder_axis", _UTF8),
    ("entry_hour_hist_json", _UTF8),
    ("event_day_counts_json", _UTF8),
    ("event_day_pnl_json", _UTF8),
    ("exits_stop_loss", _INT64),
    ("exits_take_profit", _INT64),
    ("exits_signal", _INT64),
    ("exits_forced_1520", _INT64),
    ("exits_end_of_data", _INT64),
    ("exits_session_gap", _INT64),
    ("max_consecutive_losses", _INT64),
    ("max_consecutive_wins", _INT64),
    ("composite_score", _FLOAT64),
    ("win_loss_ratio", _FLOAT64),
    ("recovery_factor", _FLOAT64),
    ("fingerprint", _UTF8),
)

#: The agg column-name set (validation key for incoming records).
_AGG_COLUMN_NAMES: Final[frozenset[str]] = frozenset(name for name, _ in AGG_COLUMNS)

#: The three stamped sidecar file keys, in stamp order.
_SIDECAR_KEYS: Final[tuple[str, ...]] = ("trades", "aggs", "equity")

#: sha256 hex digest length.
_SHA256_HEX_LEN: Final[int] = 64

#: A tuple store path relative to the sweeps root is exactly
#: ``<instrument>/<tf>/<store>.parquet`` — three parts (the store-layout law).
_STORE_REL_PARTS: Final[int] = 3

#: A stamped trade span is exactly ``[row_start, row_count]`` — two fields.
_SPAN_FIELDS: Final[int] = 2

#: Unix epoch (UTC) — the exact-integer microsecond timestamp anchor.
_EPOCH_UTC: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)

#: Reused install hint for the optional ``polars`` dependency.
_POLARS_HINT: Final[str] = (
    "trade_store requires 'polars' — install with 'pip install -e .[data]' "
    "to enable the P2 data lake"
)


class TradeStoreError(BruteXError):
    """Raised for any results-depth sidecar failure (paths, schema, damage)."""


@dataclass(frozen=True, slots=True)
class SidecarPaths:
    """The per-tuple sidecar trio + stamp destinations (design D0b layout)."""

    trades: Path
    aggs: Path
    equity: Path
    stamp: Path


@dataclass(frozen=True, slots=True)
class TupleSidecarStamp:
    """The trio's validation stamp (B4 + the additive ``trade_spans`` map).

    ``store_content_hash`` is the SOURCE ResultsStore Parquet's sha256 — the
    idempotence key the run-end pass compares against the CURRENT store bytes
    to skip an already-current tuple (R42 resume). ``content_hashes`` carries
    each sidecar file's own sha256 and ``content_sizes`` its byte length (the
    O(1)-stat staleness/torn probe the page-read path uses); ``trade_spans``
    maps every captured ``run_id`` to its contiguous ``(row_start, row_count)``
    in the trades file — the O(1) page-total + positional-slice key (both maps
    are additive vs the design's B4 field list, recorded in the WP1 notes).
    ``produced_at_utc`` is the ONLY non-deterministic field (B4 law).

    ``reference_fingerprint`` (additive, DEC-DEPTH-STAMP-REFDATA-001,
    MIGRATION.md #1926) is a per-INSTRUMENT sha256 over the reference inputs the
    depth pass folds into the sidecars — the VIX series content + the
    instrument's 1-minute/1-day reference bars + the WP3 provider-presence
    flags — so a reference-data change (e.g. a VIX feed pulled AFTER the first
    stamp) forces a rebuild instead of the store-bytes-only skip. It defaults to
    ``None`` (an OLD stamp written before this field), which can NEVER equal a
    hex digest, so an old stamp is treated as NOT current and re-derived ONCE —
    never a fabricated match. NO ``TRADES_SCHEMA_VERSION`` bump: the exact-match
    version gate in :func:`load_stamp` must still accept old stamps at parse
    time, and the additive default handles the one-time re-derive.
    """

    schema_version: int
    store_content_hash: str
    run_ids: int
    trade_rows: int
    agg_rows: int
    equity_rows: int
    content_hashes: dict[str, str]
    content_sizes: dict[str, int]
    trade_spans: dict[str, tuple[int, int]]
    produced_at_utc: str
    reference_fingerprint: str | None = None


class TradeRowWire(TypedDict):
    """One paged trade row — the binding P3→P4 wire contract + additive keys.

    Contract keys (verbatim): ``entry_ts, entry_px, exit_ts, exit_px,
    direction, lots, exit_reason, pnl_paisa, costs_paisa, mae, mfe, vix_entry,
    vix_exit`` — timestamps ISO-8601 UTC; ``lots`` / ``costs_paisa`` are
    honest ``None`` (NULL-by-design until P5, gap-register Ruling 18);
    ``mae``/``mfe`` are the 1-MINUTE values when the trade's minute path was
    fully covered (``path_1m_status == "ok"``) else the sweep-TF values.
    ``exit_reason`` carries the walker's APPEND-ONLY string vocabulary
    (``trade_sim.EXIT_REASONS``) — the contract KEYS are unchanged when that
    VALUE vocabulary extends (``session_gap`` joined it,
    DEC-TRADESIM-OPEN-SESSION-GUARD-001; the read side passes the string
    through, never validating against a frozen set).
    Additive keys: ``trade_seq``, ``path_1m_status``, ``mae_granularity``
    (``"1m"`` | ``"tf"`` — says which granularity ``mae``/``mfe`` carry, so a
    consumer never guesses).
    """

    entry_ts: str
    entry_px: float
    exit_ts: str
    exit_px: float
    direction: str
    lots: int | None
    exit_reason: str
    pnl_paisa: int
    costs_paisa: int | None
    mae: int
    mfe: int
    vix_entry: float | None
    vix_exit: float | None
    trade_seq: int
    path_1m_status: str
    mae_granularity: str


@dataclass(frozen=True, slots=True)
class TradePage:
    """One page of a combo's trades (clamped ``offset``/``limit`` echoed back)."""

    rows: tuple[TradeRowWire, ...]
    total: int
    offset: int
    limit: int


def sweep_run_dir(results_root: Path, run_id: str) -> Path:
    """THE run-artifact path-builder seam (gap-register Ruling 4, 2026-07-08).

    ``<results_root>`` is the sweeps results tree itself (``<lake>/sweeps`` —
    the ``--results-root`` the sweep flushed into); the returned per-run
    directory is ``<results_root parent>/sweep_runs/<run_id>/`` — a SIBLING
    root beside the sweeps tree (the register's ``<lake>/sweep_runs/``
    example), NEVER inside the ``**/*.parquet``-globbed sweeps tree, so
    /results discovery, the sweep-verify doctor and ``results_report`` are
    structurally untouched by any artifact written here.

    EVERY run-end results-depth artifact path flows through this ONE function
    (tuple sidecars via :func:`sidecar_paths_for`, top100.json + stamp,
    baseline series, the P2 manifest probe) — it is the documented SEAM P2's
    future ``run_layout.py`` leaf rebinds at merge-integration. ``run_id``
    today is the ENGINE grid identity (``grid_identity(grid)``); Ruling 9's
    one-LOGICAL-run_id-wrapping-N-engine-runs lands by rebinding the *run_id*
    argument at that same integration point — callers never mint their own
    layout.

    Raises
    ------
    TradeStoreError
        On an empty *run_id* (an unkeyed run dir would collide every run).
    """
    if not run_id:
        raise TradeStoreError("sweep_run_dir: run_id must be non-empty")
    return results_root.parent / SWEEP_RUNS_DIRNAME / run_id


def sidecar_paths_for(store_path: Path, sweeps_root: Path, *, run_id: str) -> SidecarPaths:
    """Map a tuple store path to its results-depth sidecar trio + stamp paths.

    ``<sweeps>/<instr>/<tf>/<stem>.parquet`` →
    ``sweep_run_dir(sweeps_root, run_id)/results-depth/<instr>/<tf>/
    <stem>.{trades,aggs,equity}.parquet`` + ``<stem>.stamp.json`` —
    path-mechanical, so the store↔sidecar mapping is 1:1 with zero
    bookkeeping, and every path rides the ONE :func:`sweep_run_dir` seam
    (Ruling 4: never inside the globbed sweeps tree).

    Raises
    ------
    TradeStoreError
        If *store_path* is not exactly ``<sweeps_root>/<instrument>/<tf>/<file>.parquet``
        or *run_id* is empty.
    """
    try:
        rel = store_path.relative_to(sweeps_root)
    except ValueError as exc:
        raise TradeStoreError(
            f"store path {store_path} is not under the sweeps root {sweeps_root}"
        ) from exc
    parts = rel.parts
    if len(parts) != _STORE_REL_PARTS or not parts[2].endswith(".parquet"):
        raise TradeStoreError(
            f"store path {store_path} is not a 3-level <instrument>/<tf>/<store>.parquet "
            f"tuple store under {sweeps_root}"
        )
    instrument, tf, filename = parts
    stem = filename[: -len(".parquet")]
    base = sweep_run_dir(sweeps_root, run_id) / RESULTS_DEPTH_DIRNAME / instrument / tf
    return SidecarPaths(
        trades=base / f"{stem}.trades.parquet",
        aggs=base / f"{stem}.aggs.parquet",
        equity=base / f"{stem}.equity.parquet",
        stamp=base / f"{stem}.stamp.json",
    )


# ── Writer ───────────────────────────────────────────────────────────────────


def write_tuple_sidecars(
    paths: SidecarPaths,
    captures: Sequence[ComboCapture],
    aggs: Sequence[Mapping[str, object]],
    *,
    store_content_hash: str,
    reference_fingerprint: str | None = None,
) -> TupleSidecarStamp:
    """Write the deterministic sidecar trio + stamp for one tuple's captures.

    Captures are sorted by ``run_id`` internally (input order never matters —
    Golden Rule #4); trades within a capture keep the walker's chronological
    order as ``trade_seq``; equity rows sort by session date. The stamp is
    written LAST (a crash between files leaves a stamp-less trio that every
    reader treats as absent → rebuild — R42).

    Parameters
    ----------
    paths
        The destination trio from :func:`sidecar_paths_for`.
    captures
        One :class:`~brutex.engine.trade_capture.ComboCapture` per persisted
        finalist. Zero-trade captures are first-class (a span of 0 rows).
    aggs
        One ``{column: value}`` record per finalist, keyed EXACTLY by
        :data:`AGG_COLUMNS` names (produced by ``combo_aggregates`` — WP2).
        May be empty while WP2 is not wired.
    store_content_hash
        The SOURCE ResultsStore flush ``content_hash`` (64-char sha256 hex) —
        the stamp's idempotence key.
    reference_fingerprint
        Optional per-instrument reference-data fingerprint (64-char sha256 hex)
        recorded on the stamp so the run-end pass re-derives when the reference
        inputs (VIX / 1m / 1day / provider presence) change
        (DEC-DEPTH-STAMP-REFDATA-001, MIGRATION.md #1926). ``None`` (the
        default) records no fingerprint — a stamp that predates the field.

    Raises
    ------
    TradeStoreError
        On a malformed store hash, a malformed ``reference_fingerprint``,
        duplicate ``run_id``, a mis-keyed or mis-typed agg record, or a missing
        ``polars``. Raw ``OSError`` from the atomic writes propagates unchanged
        (the engine sidecar contract).
    """
    _validate_sha256(store_content_hash, label="store_content_hash")
    if reference_fingerprint is not None:
        _validate_sha256(reference_fingerprint, label="reference_fingerprint")
    ordered = sorted(captures, key=lambda cap: cap.run_id)
    seen: set[str] = set()
    for cap in ordered:
        if cap.run_id in seen:
            raise TradeStoreError(f"duplicate capture run_id {cap.run_id!r}")
        seen.add(cap.run_id)

    trade_columns: dict[str, list[object]] = {name: [] for name, _ in TRADE_COLUMNS}
    equity_columns: dict[str, list[object]] = {name: [] for name, _ in EQUITY_COLUMNS}
    trade_spans: dict[str, tuple[int, int]] = {}
    cursor = 0
    for cap in ordered:
        trade_spans[cap.run_id] = (cursor, len(cap.trades))
        cursor += len(cap.trades)
        for seq, trade in enumerate(cap.trades):
            _append_trade_row(trade_columns, run_id=cap.run_id, seq=seq, trade=trade)
        for point in sorted(cap.equity, key=lambda p: p.session_date):
            equity_columns["run_id"].append(cap.run_id)
            equity_columns["session_date"].append(point.session_date.isoformat())
            equity_columns["equity_close_paisa"].append(point.equity_close_paisa)
            equity_columns["equity_intraday_low_paisa"].append(point.equity_intraday_low_paisa)
            equity_columns["trades_closed"].append(point.trades_closed)

    agg_columns = _agg_columns_from_records(aggs)

    trades_bytes = _render_frame_bytes(TRADE_COLUMNS, trade_columns)
    aggs_bytes = _render_frame_bytes(AGG_COLUMNS, agg_columns)
    equity_bytes = _render_frame_bytes(EQUITY_COLUMNS, equity_columns)

    paths.trades.parent.mkdir(parents=True, exist_ok=True)
    for dest, content in (
        (paths.trades, trades_bytes),
        (paths.aggs, aggs_bytes),
        (paths.equity, equity_bytes),
    ):
        _atomic_write_bytes(dest, content)

    stamp = TupleSidecarStamp(
        schema_version=TRADES_SCHEMA_VERSION,
        store_content_hash=store_content_hash,
        run_ids=len(ordered),
        trade_rows=len(trade_columns["run_id"]),
        agg_rows=len(agg_columns["run_id"]),
        equity_rows=len(equity_columns["run_id"]),
        content_hashes={
            "trades": hashlib.sha256(trades_bytes).hexdigest(),
            "aggs": hashlib.sha256(aggs_bytes).hexdigest(),
            "equity": hashlib.sha256(equity_bytes).hexdigest(),
        },
        content_sizes={
            "trades": len(trades_bytes),
            "aggs": len(aggs_bytes),
            "equity": len(equity_bytes),
        },
        trade_spans=trade_spans,
        produced_at_utc=datetime.now(tz=UTC).isoformat(),
        reference_fingerprint=reference_fingerprint,
    )
    payload = {
        "schema_version": stamp.schema_version,
        "store_content_hash": stamp.store_content_hash,
        "run_ids": stamp.run_ids,
        "trade_rows": stamp.trade_rows,
        "agg_rows": stamp.agg_rows,
        "equity_rows": stamp.equity_rows,
        "content_hashes": stamp.content_hashes,
        "content_sizes": stamp.content_sizes,
        "trade_spans": {run_id: list(span) for run_id, span in stamp.trade_spans.items()},
        "produced_at_utc": stamp.produced_at_utc,
    }
    # DEC-DEPTH-STAMP-REFDATA-001 (#1926): emit the key ONLY when present (the
    # additive-column back-compat law). Byte-identity is scoped to the DEFAULT
    # ``reference_fingerprint=None`` primitive path (the direct ``write_tuple_sidecars``
    # callers + the trades/aggs/equity goldens) — a ``None`` stamp renders exactly a
    # pre-feature stamp, so an old reader sees the same bytes. It is NOT a claim about
    # the production DEPTH-PASS path, which ALWAYS passes a hex fingerprint — even a
    # no-VIX run, whose fp folds the "absent" digest token, not ``None`` — so those
    # stamps carry the key and are DELIBERATELY not pre-feature-identical (that hex fp
    # is exactly what re-derives a tuple once a VIX feed is pulled later).
    if stamp.reference_fingerprint is not None:
        payload["reference_fingerprint"] = stamp.reference_fingerprint
    rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    atomic_write_simple(paths.stamp, lambda tmp: tmp.write_text(rendered, encoding="utf-8"))
    _LOG.info(
        "trade_store.sidecars_written",
        stamp=str(paths.stamp),
        run_ids=stamp.run_ids,
        trade_rows=stamp.trade_rows,
        agg_rows=stamp.agg_rows,
        equity_rows=stamp.equity_rows,
    )
    return stamp


def _append_trade_row(
    columns: dict[str, list[object]], *, run_id: str, seq: int, trade: CapturedTrade
) -> None:
    """Append one captured trade to the trades column arrays (B1 order)."""
    columns["run_id"].append(run_id)
    columns["trade_seq"].append(seq)
    columns["entry_ts_us"].append(_ts_to_us(trade.entry_ts))
    columns["exit_ts_us"].append(_ts_to_us(trade.exit_ts))
    columns["entry_px"].append(float(trade.entry_px))
    columns["exit_px"].append(float(trade.exit_px))
    columns["direction"].append(trade.direction.value)
    columns["lots"].append(trade.lots)
    columns["exit_reason"].append(trade.exit_reason)
    columns["pnl_paisa"].append(trade.pnl_paisa)
    columns["costs_paisa"].append(trade.costs_paisa)
    columns["mae_paisa"].append(trade.mae_paisa)
    columns["mfe_paisa"].append(trade.mfe_paisa)
    columns["mae_1m_paisa"].append(trade.mae_1m_paisa)
    columns["mfe_1m_paisa"].append(trade.mfe_1m_paisa)
    columns["path_1m_status"].append(trade.path_1m_status.value)
    columns["vix_entry"].append(None if trade.vix_entry is None else float(trade.vix_entry))
    columns["vix_exit"].append(None if trade.vix_exit is None else float(trade.vix_exit))


def _agg_columns_from_records(aggs: Sequence[Mapping[str, object]]) -> dict[str, list[object]]:
    """Validate + columnarise the agg records (sorted by ``run_id``, B2 order)."""
    for record in aggs:
        keys = set(record.keys())
        if keys != _AGG_COLUMN_NAMES:
            missing = sorted(_AGG_COLUMN_NAMES - keys)
            extra = sorted(keys - _AGG_COLUMN_NAMES)
            raise TradeStoreError(
                f"agg record keys do not match AGG_COLUMNS (missing={missing}, extra={extra})"
            )
        if not isinstance(record["run_id"], str) or not record["run_id"]:
            raise TradeStoreError(
                f"agg record run_id must be a non-empty str, got {record['run_id']!r}"
            )
    ordered = sorted(aggs, key=lambda record: str(record["run_id"]))
    run_ids = [str(record["run_id"]) for record in ordered]
    if len(set(run_ids)) != len(run_ids):
        raise TradeStoreError("duplicate agg record run_id")
    return {name: [record[name] for record in ordered] for name, _ in AGG_COLUMNS}


# ── Stamp reader ─────────────────────────────────────────────────────────────


def load_stamp(paths: SidecarPaths, *, verify_hashes: bool = True) -> TupleSidecarStamp | None:
    """Read + validate the trio stamp; ANY damage returns ``None`` (rebuild).

    Validation (the lake_manifest law): the stamp file reads + parses as a
    JSON object; ``schema_version`` matches; every field has the right shape;
    the recorded per-combo spans sum to ``trade_rows``; each sidecar file's
    CURRENT byte size matches the stamped one (an O(1) ``stat`` — catches a
    missing / torn / half-written / stale-generation file); and — by default —
    each file's CURRENT sha256 equals the stamped one. Each rejection logs ONE
    ``trade_store.stamp_invalid`` warning naming the reason — never silent.

    ``verify_hashes=False`` is the PAGE-READ tier (design C3's O(1) staleness
    probe): it skips the O(file-bytes) content re-hash so a per-request poll
    stays flat in stored-trade count, keeping the stat-size probe + the
    per-row ``run_id`` span belt. HONEST envelope: a same-size in-place byte
    corruption passes this tier (it is caught by the default full-hash tier —
    the run-end pass's skip/rebuild decision — by Parquet decode errors, or by
    the span belt); no repo writer ever rewrites a sidecar in place without a
    rename, so the stat probe tracks every sanctioned write.

    DELIBERATE (disclosed trade-off, #1925 review round 2 LEAVE-AS-IS): the
    ``except OSError → None`` arm treats EVERY read fault on an EXISTING stamp
    — including a transient ``EIO`` device blip — as damage → rebuild, NEVER a
    raise. The worst case of a false "damage" verdict is ONE redundant,
    idempotent re-derive of that tuple; raising instead would abort the whole
    fenced depth pass on a transient fault (the transient-abort class). Do NOT
    narrow this arm.
    """

    def _invalid(reason: str) -> None:
        _LOG.warning("trade_store.stamp_invalid", stamp=str(paths.stamp), reason=reason)

    try:
        raw = paths.stamp.read_bytes()
    except OSError:
        _invalid("stamp_unreadable")
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        _invalid("stamp_not_json")
        return None
    if not isinstance(payload, dict):
        _invalid("stamp_not_object")
        return None
    if payload.get("schema_version") != TRADES_SCHEMA_VERSION:
        _invalid("schema_version_mismatch")
        return None
    counts: dict[str, int] = {}
    for name in ("run_ids", "trade_rows", "agg_rows", "equity_rows"):
        value = payload.get(name)
        if not _is_count(value):
            _invalid(f"bad_{name}")
            return None
        counts[name] = value
    store_hash = payload.get("store_content_hash")
    if not _is_sha256(store_hash):
        _invalid("bad_store_content_hash")
        return None
    hashes = payload.get("content_hashes")
    if (
        not isinstance(hashes, dict)
        or set(hashes.keys()) != set(_SIDECAR_KEYS)
        or not all(_is_sha256(hashes[key]) for key in _SIDECAR_KEYS)
    ):
        _invalid("bad_content_hashes")
        return None
    sizes = payload.get("content_sizes")
    if (
        not isinstance(sizes, dict)
        or set(sizes.keys()) != set(_SIDECAR_KEYS)
        or not all(_is_count(sizes[key]) for key in _SIDECAR_KEYS)
    ):
        _invalid("bad_content_sizes")
        return None
    spans_raw = payload.get("trade_spans")
    if not isinstance(spans_raw, dict):
        _invalid("bad_trade_spans")
        return None
    spans: dict[str, tuple[int, int]] = {}
    for run_id, span in spans_raw.items():
        if (
            not isinstance(run_id, str)
            or not isinstance(span, list)
            or len(span) != _SPAN_FIELDS
            or not _is_count(span[0])
            or not _is_count(span[1])
        ):
            _invalid("bad_trade_spans")
            return None
        spans[run_id] = (span[0], span[1])
    if sum(count for _, count in spans.values()) != counts["trade_rows"]:
        _invalid("trade_spans_sum_mismatch")
        return None
    produced = payload.get("produced_at_utc")
    if not isinstance(produced, str):
        _invalid("bad_produced_at_utc")
        return None
    # DEC-DEPTH-STAMP-REFDATA-001 (#1926): tolerant additive parse — absent key
    # (a pre-feature stamp) reads None; a PRESENT value must be canonical sha256
    # hex (a poisoned non-hex value is a damaged stamp → rebuild, never adopted).
    reference_fingerprint = payload.get("reference_fingerprint")
    if reference_fingerprint is not None and not _is_sha256(reference_fingerprint):
        _invalid("bad_reference_fingerprint")
        return None
    for key, path in (
        ("trades", paths.trades),
        ("aggs", paths.aggs),
        ("equity", paths.equity),
    ):
        try:
            actual_size = path.stat().st_size
        except OSError:
            _invalid(f"{key}_unreadable")
            return None
        if actual_size != sizes[key]:
            _invalid(f"{key}_size_mismatch")
            return None
        if verify_hashes:
            try:
                content = path.read_bytes()
            except OSError:
                _invalid(f"{key}_unreadable")
                return None
            if hashlib.sha256(content).hexdigest() != hashes[key]:
                _invalid(f"{key}_hash_mismatch")
                return None
    return TupleSidecarStamp(
        schema_version=TRADES_SCHEMA_VERSION,
        store_content_hash=store_hash,
        run_ids=counts["run_ids"],
        trade_rows=counts["trade_rows"],
        agg_rows=counts["agg_rows"],
        equity_rows=counts["equity_rows"],
        content_hashes={key: str(hashes[key]) for key in _SIDECAR_KEYS},
        content_sizes={key: sizes[key] for key in _SIDECAR_KEYS},
        trade_spans=spans,
        produced_at_utc=produced,
        reference_fingerprint=reference_fingerprint,
    )


# ── Paged reader + export stream ─────────────────────────────────────────────


def read_trade_page(paths: SidecarPaths, *, run_id: str, offset: int, limit: int) -> TradePage:
    """Read one clamped page of a combo's trades (the R24 drill-down read).

    ``limit`` clamps to ``[1, PAGE_LIMIT_MAX]``, ``offset`` to ``[0, 2**63−1]``
    (the db_view clamp law — hostile values never reach arithmetic). The total
    is the stamp's O(1) span lookup; the page is a pure positional lazy slice
    of the combo's CONTIGUOUS rows (module envelope note). An unknown
    ``run_id`` or a zero-trade combo pages as an empty list with ``total`` 0
    and ZERO file reads.

    Raises
    ------
    TradeStoreError
        If the stamp is missing/invalid (caller rebuilds via the run-end
        pass), the trades file is unreadable, or a paged row's ``run_id``
        diverges from the stamped span (a desync the hash gate should make
        impossible — fails loud, never serves foreign trades).
    """
    limit = max(1, min(limit, PAGE_LIMIT_MAX))
    offset = max(0, min(offset, _OFFSET_MAX))
    # Page-read tier: O(1) stat-size staleness probe, no O(file) re-hash
    # (see :func:`load_stamp`'s honest envelope) — the flatness the CI bench pins.
    stamp = load_stamp(paths, verify_hashes=False)
    if stamp is None:
        raise TradeStoreError(
            f"results-depth sidecar stamp missing or invalid at {paths.stamp} — "
            "rebuild via the run-end results-depth pass"
        )
    span = stamp.trade_spans.get(run_id)
    if span is None:
        return TradePage(rows=(), total=0, offset=offset, limit=limit)
    start, total = span
    if total == 0 or offset >= total:
        return TradePage(rows=(), total=total, offset=offset, limit=limit)
    rows = _read_slice(paths.trades, start=start + offset, length=min(limit, total - offset))
    return TradePage(
        rows=tuple(_wire_row(record, run_id=run_id) for record in rows),
        total=total,
        offset=offset,
        limit=limit,
    )


def iter_trades_for_export(paths: SidecarPaths, run_id: str) -> Iterator[TradeRowWire]:
    """Stream EVERY trade of one combo in bounded-memory batches (R24 export).

    Yields wire rows in ``trade_seq`` order, reading at most
    :data:`_EXPORT_BATCH_ROWS` rows per batch — a million-trade combo never
    materialises in one list. Same stamp validation + span-belt as
    :func:`read_trade_page`.

    Raises
    ------
    TradeStoreError
        As :func:`read_trade_page`.
    """
    stamp = load_stamp(paths, verify_hashes=False)
    if stamp is None:
        raise TradeStoreError(
            f"results-depth sidecar stamp missing or invalid at {paths.stamp} — "
            "rebuild via the run-end results-depth pass"
        )
    span = stamp.trade_spans.get(run_id)
    if span is None:
        return
    start, total = span
    for batch_offset in range(0, total, _EXPORT_BATCH_ROWS):
        length = min(_EXPORT_BATCH_ROWS, total - batch_offset)
        for record in _read_slice(paths.trades, start=start + batch_offset, length=length):
            yield _wire_row(record, run_id=run_id)


def _read_slice(trades_path: Path, *, start: int, length: int) -> list[dict[str, Any]]:
    """Collect one positional slice of the trades Parquet as named records."""
    try:
        import polars as pl
    except ImportError as exc:
        raise TradeStoreError(_POLARS_HINT) from exc
    try:
        df = pl.scan_parquet(trades_path).slice(start, length).collect()
    except (pl.exceptions.PolarsError, OSError) as exc:
        raise TradeStoreError(f"unreadable trades sidecar at {trades_path}: {exc}") from exc
    return list(df.iter_rows(named=True))


def _wire_row(record: Mapping[str, Any], *, run_id: str) -> TradeRowWire:
    """Project one stored trade record onto the binding wire shape (§B1)."""
    if record["run_id"] != run_id:
        raise TradeStoreError(
            f"trade page row run_id {record['run_id']!r} diverges from the stamped "
            f"span for {run_id!r} — sidecar/stamp desync, rebuild"
        )
    status = str(record["path_1m_status"])
    use_1m = status == PathStatus.OK.value
    mae = int(record["mae_1m_paisa"]) if use_1m else int(record["mae_paisa"])
    mfe = int(record["mfe_1m_paisa"]) if use_1m else int(record["mfe_paisa"])
    vix_entry = record["vix_entry"]
    vix_exit = record["vix_exit"]
    return TradeRowWire(
        entry_ts=_us_to_iso(int(record["entry_ts_us"])),
        entry_px=float(record["entry_px"]),
        exit_ts=_us_to_iso(int(record["exit_ts_us"])),
        exit_px=float(record["exit_px"]),
        direction=str(record["direction"]),
        lots=None if record["lots"] is None else int(record["lots"]),
        exit_reason=str(record["exit_reason"]),
        pnl_paisa=int(record["pnl_paisa"]),
        costs_paisa=None if record["costs_paisa"] is None else int(record["costs_paisa"]),
        mae=mae,
        mfe=mfe,
        vix_entry=None if vix_entry is None else float(vix_entry),
        vix_exit=None if vix_exit is None else float(vix_exit),
        trade_seq=int(record["trade_seq"]),
        path_1m_status=status,
        mae_granularity="1m" if use_1m else "tf",
    )


# ── Shared helpers ───────────────────────────────────────────────────────────


def _render_frame_bytes(
    schema_columns: tuple[tuple[str, str], ...], columns: dict[str, list[object]]
) -> bytes:
    """Serialise pinned-schema columns to the deterministic Parquet byte blob.

    The exact ``results_store._render_bytes`` discipline: pinned dtypes, zstd
    :data:`~brutex.engine.results_store.ZSTD_LEVEL`, row groups of
    :data:`~brutex.engine.results_store.ROW_GROUP_SIZE`, ``statistics=False``
    (non-deterministic), ``use_pyarrow=False``.
    """
    try:
        import polars as pl
    except ImportError as exc:
        raise TradeStoreError(_POLARS_HINT) from exc
    dtypes = {_UTF8: pl.Utf8, _INT64: pl.Int64, _FLOAT64: pl.Float64, _BOOL: pl.Boolean}
    schema = {name: dtypes[tag] for name, tag in schema_columns}
    try:
        df = pl.DataFrame(columns, schema=schema)
    except (pl.exceptions.PolarsError, TypeError, OverflowError) as exc:
        raise TradeStoreError(f"sidecar rows do not fit the pinned schema: {exc}") from exc
    bio = io.BytesIO()
    df.write_parquet(
        bio,
        compression="zstd",
        compression_level=ZSTD_LEVEL,
        row_group_size=ROW_GROUP_SIZE,
        use_pyarrow=False,
        statistics=False,  # stats are non-deterministic; skip for reproducibility
    )
    return bio.getvalue()


def _atomic_write_bytes(dest: Path, content: bytes) -> None:
    """Atomically write *content* to *dest* (tmp → fsync → ``os.replace``).

    Delegates to the COMMON guard-free
    :func:`brutex.data.atomic_io.atomic_write_simple` — a raw ``OSError``
    (e.g. ``ENOSPC``) propagates unchanged, the engine-sidecar error contract
    (``results_store._atomic_write`` precedent).
    """
    atomic_write_simple(dest, lambda tmp: tmp.write_bytes(content))


def _ts_to_us(ts: datetime) -> int:
    """Convert a tz-aware datetime to exact UTC epoch microseconds (no floats)."""
    return (ts - _EPOCH_UTC) // timedelta(microseconds=1)


def _us_to_iso(us: int) -> str:
    """Convert UTC epoch microseconds back to an ISO-8601 UTC string."""
    return (_EPOCH_UTC + timedelta(microseconds=us)).isoformat()


def _is_count(value: object) -> TypeGuard[int]:
    """``True`` iff *value* is a genuine non-negative int (bools rejected)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_sha256(value: object) -> TypeGuard[str]:
    """``True`` iff *value* is a 64-char lowercase sha256 hex string."""
    if not isinstance(value, str) or len(value) != _SHA256_HEX_LEN or value != value.lower():
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True


def _validate_sha256(value: str, *, label: str) -> None:
    """Raise :class:`TradeStoreError` unless *value* is canonical sha256 hex."""
    if not _is_sha256(value):
        raise TradeStoreError(f"{label} must be 64-char lowercase sha256 hex, got {value!r}")
