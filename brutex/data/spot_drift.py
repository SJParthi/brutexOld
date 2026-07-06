"""Evidence-grounded stock spot-key drift map (DEC-GREEKS-SPOTJOIN-STOCK-001).

The SPOTJOIN-2 stock arm of the Greeks spot-join drift-recovery ladder
(DEC-GREEKS-CORRECTNESS-AUDIT-001 #1188 named it a follow-up; this module
CLOSES it): a stock chain whose F&O ``underlying_symbol`` (``BAJAJAUTO``)
differs from its on-disk spot dir (``BAJAJ-AUTO``) by case / space / hyphen
drift stamped the WHOLE chain ``NO_SPOT`` forever, because the stock
normalisation is irreversible as a string function. It IS reversible as a
lookup against two finite REAL sets — the on-disk spot dirs under
``bars/<exch>/{INDEX,CASH}/`` (the EVIDENCE) and the instrument master's
IDX+EQ ``symbol_name`` s (the VOCABULARY). This module scans the evidence,
verifies it against the vocabulary under ONE pure fold
(:func:`~brutex.data.lake_keys.stock_drift_fold`), and emits the
:data:`~brutex.data.lake_keys.StockDriftMap` the ladder consumes.

Never a fabricated mapping (Golden Rule #1): a dir appears in an entry iff
(a) it exists on disk, and (b) its final fold interpretation resolves to
exactly ONE master ``symbol_name`` on that exchange. A fold shared by ≥2
master symbols is POISONED — refused even when only one dir exists for it
(never guess between real instruments); a dir matching NO master fold
(a junk stray, a ``M%26M`` form, a foreign-prefix dir) is counted
``unverified`` and never mapped. Same-instrument fragment dirs are ALL
offered as an ordered tuple (master-exact first, then lexicographic — the
#911 "best real data wins" rank-merge semantics) and the PROBE decides which
fragment carries data.

Fail-soft by design: the loader returns ``None`` on a missing / unreadable
master (the ladder stays byte-identical → the honest ``NO_SPOT``), a
per-``(exchange, segment)`` scan error skips THAT subtree (partial evidence,
never a raise), and a raw ``MemoryError`` ALWAYS re-raises (the honest-OOM
convention). No blanket ``except Exception`` — a genuinely unexpected defect
propagates to the #1188 ``fno.greeks_enrich.error`` fence (loud ``ok=False``,
the pull still never fails).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import structlog

from brutex.data.lake_keys import stock_drift_fold
from brutex.data.partition import SPOT_PROBE_SEGMENTS

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence
    from pathlib import Path

_LOG = structlog.get_logger("brutex.data.spot_drift")

__all__ = [
    "build_stock_drift_map",
    "load_stock_drift_map",
    "scan_spot_symbol_dirs",
]

#: The instrument master's on-disk file name inside the lake root — the store's
#: own convention (``instrument_store.DEFAULT_DB_NAME``; pinned equal by test so
#: the two constants can never drift — this module must not import the heavy
#: store at module level, the ``write_option_greeks`` lazy-import convention).
_MASTER_DB_NAME: Final[str] = "instruments.duckdb"

#: Cap on per-build ``spot_drift.ambiguous_master`` WARNs — the #907/#919
#: capped-log convention; the FULL count always rides ``spot_drift.map_built``.
_AMBIGUITY_LOG_CAP: Final[int] = 20


def scan_spot_symbol_dirs(lake_root: Path) -> dict[str, tuple[str, ...]]:
    """Enumerate the on-disk spot symbol dirs — the EVIDENCE side of the map.

    One ``iterdir`` per ``bars/<exch>/<seg>`` for ``seg`` in
    :data:`~brutex.data.partition.SPOT_PROBE_SEGMENTS` (``("INDEX", "CASH")``),
    for every exchange dir present under ``bars/``; the two segments' names are
    unioned, deduped and sorted per exchange. Only directories count
    (``is_dir()`` screen — junk files and broken symlinks are skipped;
    symlinked dirs are followed). NO Parquet reads and NO per-dir globs —
    dir existence is the evidence bar; bar-level evidence belongs to the probe
    (``read_spot_join_with_recovery`` returns a candidate only when its probed
    join has bars).

    Deliberately NOT ``lake_manifest.load_manifest_datasets`` (the enrich seam
    runs immediately post-pull, right after the per-window ledger checkpoints
    moved the ledger token, so the manifest is stale in exactly the production
    case — the fallback walk would run every time, so the walk IS the
    mechanism), NOT ``db_view.list_candle_datasets`` (``brutex/data`` never
    imports ``brutex/dashboard``) and NOT ``lake_invariants._bar_files``
    (private; O(files) — this needs O(dirs)).

    A per-``(exchange, segment)`` ``OSError`` logs ONE
    ``spot_drift.scan_error`` WARNING and skips THAT subtree — PARTIAL
    evidence, never a raise (safe: multi-dir entries are offered not refused,
    so a partial scan can only shorten an offered tuple; the probe still
    decides). A missing ``bars/`` (a fresh lake) is an empty scan, not an
    error. ``MemoryError`` re-raises raw.
    """
    bars = lake_root / "bars"
    if not bars.is_dir():
        return {}
    try:
        exchange_dirs = sorted(child for child in bars.iterdir() if child.is_dir())
    except MemoryError:
        raise
    except OSError as exc:
        _LOG.warning(
            "spot_drift.scan_error",
            path=str(bars),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return {}
    result: dict[str, tuple[str, ...]] = {}
    for exchange_dir in exchange_dirs:
        names: set[str] = set()
        for segment in SPOT_PROBE_SEGMENTS:
            seg_dir = exchange_dir / segment
            if not seg_dir.is_dir():
                continue
            try:
                names.update(child.name for child in seg_dir.iterdir() if child.is_dir())
            except MemoryError:
                raise
            except OSError as exc:
                _LOG.warning(
                    "spot_drift.scan_error",
                    path=str(seg_dir),
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
        if names:
            result[exchange_dir.name] = tuple(sorted(names))
    return result


def _prefix_stripped_fold(dir_name: str, exchange: str) -> str | None:
    """DISK-SIDE ONLY legacy-``"<EXCH>-"``-prefix strip, then the fold.

    Case-robust: checked on the stripped-and-uppered form, so a dir
    ``'nse-bajaj-auto'`` is still recognised. NEVER applied to master symbols
    or lookup-time underlyings — so a pathological master symbol literally
    starting ``"NSE-"`` can never be falsely bridged (raw-fold precedence in
    the builder makes the raw interpretation FINAL first). A blank *exchange*
    yields no prefix → ``None``.
    """
    probe = dir_name.strip().upper()
    prefix = f"{exchange.strip().upper()}-"
    if len(prefix) > 1 and probe.startswith(prefix):
        return stock_drift_fold(probe[len(prefix) :])
    return None


@dataclass(frozen=True, slots=True)
class _BuildStats:
    """Per-build telemetry for the loader's ``spot_drift.map_built`` log."""

    dirs_scanned: int
    unverified_dirs: int
    poisoned_dirs: int
    ambiguous_master_folds: int


def _build_stock_drift_map_with_stats(
    symbol_dirs: Mapping[str, Sequence[str]],
    master_symbols: Mapping[str, Collection[str]],
) -> tuple[dict[tuple[str, str], tuple[str, ...]], _BuildStats]:
    """The pure builder body — the verified map plus its honest telemetry."""
    mapping: dict[tuple[str, str], tuple[str, ...]] = {}
    dirs_scanned = 0
    unverified_dirs = 0
    poisoned_dirs = 0
    ambiguous_master_folds = 0
    warns_emitted = 0
    for exchange in sorted(symbol_dirs):
        dirs = sorted(set(symbol_dirs[exchange]))
        dirs_scanned += len(dirs)
        # 1. Vocabulary index: fold -> the master symbol_names sharing it.
        master_by_fold: dict[str, set[str]] = {}
        for name in master_symbols.get(exchange, ()):
            fold = stock_drift_fold(name)
            if fold:
                master_by_fold.setdefault(fold, set()).add(name)
        poisoned = {fold for fold, names in master_by_fold.items() if len(names) > 1}
        for fold in sorted(poisoned):
            ambiguous_master_folds += 1
            if warns_emitted < _AMBIGUITY_LOG_CAP:
                warns_emitted += 1
                _LOG.warning(
                    "spot_drift.ambiguous_master",
                    exchange=exchange,
                    fold=fold,
                    symbols=repr(sorted(master_by_fold[fold])),
                )
        # 2. Per-dir interpretation — raw-fold precedence: a raw fold known to
        #    the vocabulary (unique OR poisoned) is FINAL; the prefix-stripped
        #    fold is consulted only when the raw fold matches nothing.
        buckets: dict[str, list[str]] = {}
        for dir_name in dirs:
            fold_final: str | None = None
            raw = stock_drift_fold(dir_name)
            if raw and raw in master_by_fold:
                fold_final = raw
            else:
                stripped = _prefix_stripped_fold(dir_name, exchange)
                if stripped and stripped in master_by_fold:
                    fold_final = stripped
            if fold_final is None:
                # Evidence without vocabulary (junk stray, %26 form,
                # foreign-prefix dir) — never mapped (Golden Rule #1).
                unverified_dirs += 1
            elif fold_final in poisoned:
                # ≥2 real instruments share the fold — refuse, no fall-through.
                poisoned_dirs += 1
            else:
                buckets.setdefault(fold_final, []).append(dir_name)
        # 3. Ordered multi-dir values: master-exact verbatim dir first (if
        #    present), then the rest lexicographically — the probe decides.
        for fold, fold_dirs in buckets.items():
            (master_name,) = master_by_fold[fold]
            ordered = sorted(fold_dirs)
            if master_name in ordered:
                ordered.remove(master_name)
                ordered.insert(0, master_name)
            mapping[(exchange, fold)] = tuple(ordered)
    stats = _BuildStats(
        dirs_scanned=dirs_scanned,
        unverified_dirs=unverified_dirs,
        poisoned_dirs=poisoned_dirs,
        ambiguous_master_folds=ambiguous_master_folds,
    )
    return mapping, stats


def build_stock_drift_map(
    symbol_dirs: Mapping[str, Sequence[str]],
    master_symbols: Mapping[str, Collection[str]],
) -> dict[tuple[str, str], tuple[str, ...]]:
    """PURE builder: evidence in, VERIFIED ordered map out.

    *symbol_dirs* is the :func:`scan_spot_symbol_dirs` shape (exchange dir
    name → on-disk spot symbol dir names); *master_symbols* the instrument
    master's IDX+EQ ``symbol_name`` s grouped by exchange (the ``Exchange``
    StrEnum ``.value`` — ``"NSE"``/``"BSE"`` — which IS the lake dir name; a
    weird-cased exchange dir matches no master group, so all its dirs count
    unverified). Deterministic under permutation of both inputs (exchanges and
    dirs are sorted, dirs deduped). Fully unit-testable without I/O.

    The verification rule, stated once: a dir appears in an entry iff (a) it
    exists in *symbol_dirs*, and (b) its final fold interpretation (raw fold
    first — FINAL when it matches any master fold; else the disk-side
    own-exchange prefix-stripped fold) resolves to exactly ONE master
    ``symbol_name`` on that exchange. A ≥2-master fold is POISONED even when
    only ONE dir exists for it (``spot_drift.ambiguous_master`` WARN, capped
    at :data:`_AMBIGUITY_LOG_CAP` per build); anything else counts
    ``unverified`` and is never mapped. Identity-shaped entries
    (``(NSE, RELIANCE) → ("RELIANCE",)``) are kept — harmless (the ladder's
    dedup drops them at lookup) and they cheaply recover case drift.
    """
    mapping, _stats = _build_stock_drift_map_with_stats(symbol_dirs, master_symbols)
    return mapping


def _master_read_errors() -> tuple[type[Exception], ...]:
    """The CONFIRMED error classes escaping ``InstrumentStore`` open/read.

    Enumerated against ``instrument_store.py`` (the design's implementation
    checkpoint): :class:`~brutex.data.errors.CatalogError` (the domain error —
    ``open`` wraps a missing-``duckdb`` ``ImportError`` and every open failure
    into it, and a closed store raises it), raw ``duckdb.Error`` (the read
    methods execute on the connection with NO wrapper), and ``OSError``
    (``open``'s ``path.parent.mkdir`` runs OUTSIDE its guard). ``duckdb`` is
    imported lazily just for its error class; when it is not installed the
    store's own ``open`` raises :class:`CatalogError` before any
    ``duckdb.Error`` could exist, so the tuple is complete without it.
    """
    from brutex.data.errors import CatalogError

    errors: list[type[Exception]] = [CatalogError, OSError]
    try:
        import duckdb
    except ImportError:
        return tuple(errors)
    errors.append(duckdb.Error)
    return tuple(errors)


def load_stock_drift_map(lake_root: Path) -> dict[tuple[str, str], tuple[str, ...]] | None:
    """Production loader: master vocabulary → evidence scan → verified map.

    Reads the IDX+EQ ``symbol_name`` s from ``<lake_root>/instruments.duckdb``
    (lazy :func:`~brutex.data.instrument_store.open_instrument_store` +
    ``list_index_equity_instruments()``, grouped by ``inst.exch_id.value`` —
    the ``write_option_greeks`` lazy-import convention), short-circuits an
    empty master (``{}`` with ZERO lake I/O), else scans the dirs
    (:func:`scan_spot_symbol_dirs`, partial-tolerant), builds the map and logs
    ONE ``spot_drift.map_built``.

    Fail-soft — never fails the pass: an absent master file returns ``None``
    (``spot_drift.no_master`` DEBUG — a CLI-built lake, tests, the golden
    fixture); a CONFIRMED store-read error class (:func:`_master_read_errors`)
    returns ``None`` (``spot_drift.master_error`` WARNING). ``MemoryError``
    ALWAYS re-raises raw (the honest-OOM convention of ``_run_unit``); NO
    blanket ``except Exception`` — a genuinely unexpected defect propagates to
    the #1188 ``fno.greeks_enrich.error`` fence.
    """
    db_path = lake_root / _MASTER_DB_NAME
    if not db_path.exists():
        _LOG.debug("spot_drift.no_master", lake_root=str(lake_root))
        return None
    masters_by_exchange: dict[str, set[str]] = {}
    try:
        from brutex.data.instrument_store import open_instrument_store

        with open_instrument_store(db_path) as store:
            for inst in store.list_index_equity_instruments():
                masters_by_exchange.setdefault(inst.exch_id.value, set()).add(inst.symbol_name)
    except MemoryError:
        raise
    except _master_read_errors() as exc:
        _LOG.warning(
            "spot_drift.master_error",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None
    if not masters_by_exchange:
        # Empty-master short-circuit: nothing is verifiable, so the lake walk
        # is skipped entirely — no pointless I/O.
        _LOG.info(
            "spot_drift.map_built",
            exchanges=0,
            dirs_scanned=0,
            masters=0,
            entries=0,
            multi_dir_entries=0,
            unverified_dirs=0,
            poisoned_dirs=0,
            ambiguous_master_folds=0,
        )
        return {}
    symbol_dirs = scan_spot_symbol_dirs(lake_root)
    mapping, stats = _build_stock_drift_map_with_stats(symbol_dirs, masters_by_exchange)
    _LOG.info(
        "spot_drift.map_built",
        exchanges=len(symbol_dirs),
        dirs_scanned=stats.dirs_scanned,
        masters=sum(len(names) for names in masters_by_exchange.values()),
        entries=len(mapping),
        multi_dir_entries=sum(1 for dirs in mapping.values() if len(dirs) > 1),
        unverified_dirs=stats.unverified_dirs,
        poisoned_dirs=stats.poisoned_dirs,
        ambiguous_master_folds=stats.ambiguous_master_folds,
    )
    return mapping
