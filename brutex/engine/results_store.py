"""Deterministic results store — the reproducible sweep→Parquet sink (DEC-RESULT-STORE-001).

Closes the end-to-end reproducibility gap flagged at
``docs/VERIFICATION_2026-06-03_INSTITUTIONAL_ARCH.md`` §(E) prerequisite 7
("the end-to-end reproducible sweep→Parquet path is unbuilt = 🟡 PARTIAL") and
the repro-hash 🟡 PARTIAL row of the (B) matrix. It is the **sink** that takes
the per-combo result rows a sweep produces — each keyed by the 9-field BLAKE3
**run-identity** from :func:`brutex.strategies.combo.combo_hash` (Golden Rule #4)
— and writes them to a Parquet file **idempotently and bit-for-bit
reproducibly** (Golden Rule #7), with an O(1) read-back lookup and a top-K
leaderboard query.

What this module owns is the **storage discipline**, not the metric maths (the
30+ metric formulas live in :mod:`brutex.metrics`; the Stage-2 trade-sim that
will *produce* a full :class:`~brutex.metrics.types.ClosedTrade` stream does not
exist yet — VERIFICATION_2026-06-03 (B) row h). The canonical 30+ column
**Result Row Schema** is specified at
``docs/TRACK1_BACKTEST_SPEC.md`` §"Result Row Schema". :class:`ResultRow` here
implements a deterministic, dtype-faithful subset of that spec's **identity +
money + trade-count + metric** columns, **plus two price-family columns**
(``avg_entry_price`` / ``avg_exit_price``) that are **NOT in the spec's row
schema** (it carries money + ratios, no raw price) — added to exercise the
price-storage discipline (Float64 Decimal-rupees, 2dp-snapped) end-to-end. The
remaining spec columns slot in unchanged when their producer lands (the schema
is declared once, in the module-level field tuples — CLAUDE.md §11 COMMON).

Three storage families (CLAUDE.md §4 / §6, DEC-PAISA-DOC-RECONCILE-001,
DEC-SPOT-PRICE-2DP-001, DEC-MONEY-2DP-001)
------------------------------------------------------------------------
* **Money → int64 paisa.** ``net_pnl_paisa`` … are exact signed int64 paisa.
  No floats touch money (``TRACK1_BACKTEST_SPEC.md`` §"Non-Negotiable" rule 1).
* **Statistical metrics → full-precision Float64, NEVER rounded.** ``sharpe_ratio``
  … keep every bit — rounding them would corrupt strategy ranking
  (CLAUDE.md §4 / Golden Rule #10). Non-finite metrics (``NaN`` / ``±inf``) are
  rejected at construction so the leaderboard total-order and the byte-identity
  stay well-defined (same precedent as :func:`brutex.strategies.combo.combo_hash`,
  which rejects ``NaN`` because ``NaN != NaN`` breaks reproducible comparison).
* **Prices → Decimal rupees snapped to the 2dp tick grid, stored Float64.**
  ``avg_entry_price`` / ``avg_exit_price`` are the price-family representatives;
  they exercise the same ``round_money`` (``ROUND_HALF_UP``) write-boundary snap
  the spot/option lakes use, so a result price is never vendor float-noise like
  ``14020.7999`` (DEC-SPOT-PRICE-2DP-001). Snapped at construction so the
  in-memory row is already canonical and the Parquet round-trip is exact.

Idempotence + byte-determinism (Golden Rule #4 / #7, mirrors
``brutex/data/parquet_writer.py``)
-----------------------------------------------------------------------
The in-memory ``run_id → ResultRow`` dict is the source of truth; :meth:`flush`
serialises a snapshot of it. Re-writing the same row set produces a
**byte-for-byte identical** file because we (1) sort rows by ``run_id`` before
writing (same input → same order), (2) pin the polars schema, (3) use zstd
level 3 with ``statistics=False`` + ``use_pyarrow=False`` (the exact
reproducibility settings the OHLC lake already relies on), and (4) write
atomically via tempfile + ``os.replace``. :meth:`append` dedups by ``run_id``
O(1): a never-seen id is added, an identical re-append is a silent no-op, and a
**same run-identity with different metrics raises** :class:`ResultsStoreError`
— that conflict *is* the reproducibility guarantee (one identity can only ever
map to one result; a clash means upstream non-determinism, never a silent
overwrite).

Schema back-compat law (gap-register ruling 5, DEC-SWEEP-RESULTS-DEPTH-001)
---------------------------------------------------------------------------
The LOAD boundary owns two guarantees so a stored history can never 500 a
reader or silently rot:

* **Damage is always typed (G14).** ANY failure to decode a store file —
  corrupt/foreign bytes, an OLD-schema file missing a required column, a
  wrong-dtype column, a null in a required cell, or row content that fails
  :class:`ResultRow` validation — raises
  :class:`ResultsStoreSchemaError` (a :class:`ResultsStoreError`) **naming
  the store path**, never a raw ``polars`` / ``KeyError`` / ``TypeError``
  that could 500 ``/results`` (every resilient reader — the dashboard scan,
  the sweep-verify doctor — already skips-and-counts on
  :class:`ResultsStoreError`, DEC-SWEEP-VERIFY-001).
* **Presence-tolerant optional columns (the WindowLedgerRecord precedent).**
  Every FUTURE :class:`ResultRow` extension MUST be an optional field
  defaulting ``None``, appended after the required columns and read via
  ``record.get(...)`` — so a store written BEFORE the extension loads with
  the missing field as an honest ``None``, NEVER a fabricated value and
  NEVER a "corrupt" verdict that would trigger a full quarantine-redo of
  the pre-extension history. ``deflated_sharpe`` is the template (a
  pre-DEC-RANKING-OVERFIT-001 store loads it as ``None``). The frozen
  pre-extension golden fixture
  (``tests/golden/fixtures/results_store_pre_extension_v1.parquet`` +
  ``tests/golden/test_results_store_pre_extension_golden.py``) is the guard
  every future extension must keep green.
"""

from __future__ import annotations

import hashlib
import io
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, fields
from decimal import Decimal
from pathlib import Path
from typing import Final

import structlog

from brutex.data.atomic_io import atomic_write_simple
from brutex.models import BruteXError
from brutex.money import round_money
from brutex.strategies.sweep import COMBO_HASH_HEX_LEN
from brutex.vocabulary.bits import TOTAL_CONDITIONS
from brutex.vocabulary.describe import matched_condition_names

_LOG = structlog.get_logger("brutex.engine.results_store")

# ── Determinism knobs (mirror brutex/data/parquet_writer.py — SAME contract) ──
#: zstd compression level — deterministic across zstd >= 1.5.
ZSTD_LEVEL: Final[int] = 3
#: Fixed parquet row-group size — pinned so the byte stream is reproducible.
ROW_GROUP_SIZE: Final[int] = 100_000

# ── Signed int64 bounds (Parquet Int64 storage; catches overflow at the edge) ──
_INT64_MIN: Final[int] = -(2**63)
_INT64_MAX: Final[int] = 2**63 - 1

#: Largest valid strategy mask — every defined condition bit (0..TOTAL_CONDITIONS-1)
#: set. The mask is a u128 (DEC-VOCAB-U128-CODEGEN-001 / ADR-0012 D4) — too wide for
#: a single signed Parquet ``Int64`` — so it is persisted as TWO columns
#: ``mask_bits_lo`` / ``mask_bits_hi`` (the two u64 limbs, each held as an unsigned
#: value in a signed ``Int64`` via :func:`_to_signed_i64` / :func:`_from_signed_i64`).
#: A mask is rejected if it sets a bit ``>= TOTAL_CONDITIONS`` (an undefined /
#: layout-only / reserved bit), so ``condition_names`` decodes to a defined name for
#: every set bit. (Was ``(1<<52)-1`` pre-u128.)
MASK_BITS_MAX: Final[int] = (1 << TOTAL_CONDITIONS) - 1

#: Per-limb mask for the two-column u128 split.
_U64_MASK: Final[int] = (1 << 64) - 1
#: Offset to map a u64 (0..2**64-1) into signed-int64 range for Parquet ``Int64``.
_U64_TO_I64_OFFSET: Final[int] = 1 << 63

#: Separator joining the matched-condition NAMES in the readable ``condition_names``
#: column — reads as the combo's rule ("close_above_ema20 AND near_fib_618").
CONDITION_NAME_SEP: Final[str] = " AND "

# ── Column families (declared ONCE — CLAUDE.md §11 COMMON; reused by the
#    writer, the reader, the validators and the leaderboard whitelist) ──────────
#: Identity / context string columns. ``run_id`` is the BLAKE3 run-identity key;
#: ``condition_names`` is the human-readable decode of ``mask_bits`` (the winning
#: combo, by name — DEC-SWEEP-WINNERS-BY-NAME-001).
_STRING_FIELDS: Final[tuple[str, ...]] = (
    "run_id",
    "instrument",
    "tf",
    "direction",
    "mode",
    "condition_names",
)
#: int64 strategy-mask LIMB columns — the u128 combo mask (set bits = required
#: conditions) split into two u64 limbs (lo, hi), each stored in a signed Parquet
#: ``Int64`` via an unsigned↔signed offset (a u128 does not fit one signed Int64).
#: The in-memory :class:`ResultRow` carries the single ``mask_bits`` int; these two
#: columns are the on-disk encoding, reassembled on read.
_MASK_LIMB_FIELDS: Final[tuple[str, ...]] = ("mask_bits_lo", "mask_bits_hi")
#: int64 paisa money columns. ``*_pnl`` / ``expectancy`` may be signed; the
#: gross/drawdown magnitudes are validated ``>= 0`` (see :class:`ResultRow`).
_MONEY_PAISA_FIELDS: Final[tuple[str, ...]] = (
    "net_pnl_paisa",
    "gross_profit_paisa",
    "gross_loss_paisa",
    "max_drawdown_paisa",
    "expectancy_paisa",
)
#: int64 trade-count columns (all ``>= 0``).
_COUNT_FIELDS: Final[tuple[str, ...]] = ("total_trades", "winning_trades", "losing_trades")
#: Float64 full-precision metric columns (NEVER rounded — ranking integrity).
_METRIC_FIELDS: Final[tuple[str, ...]] = (
    "sharpe_ratio",
    "sortino_ratio",
    "calmar_ratio",
    "profit_factor",
    "win_rate",
    "max_drawdown_pct",
)
#: Float64 **nullable** full-precision metric columns (NEVER rounded). ``None`` is a
#: first-class value (the metric was mathematically undefined for the sample — e.g.
#: a pool that could not be deflated) and round-trips as a Parquet null.
_NULLABLE_METRIC_FIELDS: Final[tuple[str, ...]] = ("deflated_sharpe",)
#: Float64 price columns — Decimal rupees snapped to the 2dp tick grid at write.
_PRICE_FIELDS: Final[tuple[str, ...]] = ("avg_entry_price", "avg_exit_price")

#: Numeric columns a leaderboard may rank by (metrics + money + counts).
_RANKABLE_FIELDS: Final[frozenset[str]] = frozenset(
    _METRIC_FIELDS + _MONEY_PAISA_FIELDS + _COUNT_FIELDS
)


class ResultsStoreError(BruteXError):
    """Raised for any results-store failure.

    Covers a malformed run-identity, a dtype / range / cross-field invariant
    violation on a :class:`ResultRow`, a run-identity **conflict** (same id,
    different metrics — a reproducibility breach), a missing ``polars``
    dependency, an unknown leaderboard ranking field, or a read-back of a file
    that does not exist.
    """


class ResultsStoreSchemaError(ResultsStoreError):
    """A store FILE could not be read/decoded — old-schema, foreign or damaged.

    The G14 damage class (gap-register ruling 5 / DEC-SWEEP-RESULTS-DEPTH-001):
    raised ONLY at the :meth:`ResultsStore.load` boundary, always **naming the
    store path**, for

    * corrupt / truncated / non-Parquet bytes,
    * an OLD-schema file missing a required column (e.g. the pre-u128 single
      ``mask_bits`` column instead of the ``mask_bits_lo``/``mask_bits_hi``
      limbs),
    * a wrong-dtype column or a null in a required cell, and
    * row content that fails :class:`ResultRow` validation (including a
      duplicate run-identity with differing metrics inside one file — a file
      :meth:`ResultsStore.flush` can never produce).

    Subclasses :class:`ResultsStoreError`, so every existing resilient reader
    (the dashboard store scan, the sweep-verify doctor, the CLI) keeps its
    skip-and-count behaviour unchanged while new callers can distinguish
    file-level damage from API misuse. A store missing a NEWER **optional**
    column is NOT damage — it loads presence-tolerantly with ``None`` (see the
    module-level back-compat law).
    """


@dataclass(frozen=True, slots=True)
class ResultRow:
    """One combo's result, keyed by its BLAKE3 run-identity.

    All fields are validated at construction; prices are normalised to the 2dp
    rupee tick grid (so the in-memory row is canonical and the Parquet
    round-trip is exact). The row is frozen + hashable on its fields.

    Parameters
    ----------
    run_id
        The 64-char lowercase-hex BLAKE3 run-identity from
        :func:`brutex.strategies.combo.combo_hash` (Golden Rule #4). This is the
        dedup key. Validated to be exactly :data:`COMBO_HASH_HEX_LEN` lowercase
        hex chars.
    instrument, tf, direction, mode
        Non-empty context labels (e.g. ``"NIFTY"``, ``"5m"``, ``"bull"``,
        ``"A"``). Validated non-empty only — the universe / TF / mode
        vocabularies live in their own modules (CLAUDE.md §11 COMMON); the
        producer supplies valid enum values.
    mask_bits
        The u128 strategy mask (persisted as the ``mask_bits_lo``/``mask_bits_hi``
        limb columns) — set bits are the required-true atomic conditions
        (``brutex.vocabulary.bits``). Validated ``0 <= mask_bits <= MASK_BITS_MAX``
        (only the ``TOTAL_CONDITIONS`` = 60 defined condition bits; reserved bits
        60..127 rejected — the validation reads ``TOTAL_CONDITIONS`` so it can
        never drift again). This is the machine-readable winning combo;
        ``condition_names`` is its human-readable decode (DEC-SWEEP-WINNERS-BY-NAME-001).
    condition_names
        DERIVED (``init=False``) — the matched condition NAMES of ``mask_bits``, in
        ascending bit order, joined by :data:`CONDITION_NAME_SEP` (empty string for
        the no-condition mask ``0``). Sourced from the single-source-of-truth
        :func:`brutex.vocabulary.describe.matched_condition_names` (imported, never
        copy-pasted), so it can NEVER drift from ``mask_bits``. Stored as a column
        purely for at-rest legibility — the operator reads which conditions won, by
        name, without re-running the decoder.
    net_pnl_paisa, gross_profit_paisa, gross_loss_paisa, max_drawdown_paisa, expectancy_paisa
        Money in signed int64 **paisa**. ``gross_profit_paisa``,
        ``gross_loss_paisa`` and ``max_drawdown_paisa`` are absolute magnitudes
        (validated ``>= 0``); ``net_pnl_paisa`` and ``expectancy_paisa`` may be
        negative. All must fit signed int64.
    total_trades, winning_trades, losing_trades
        Trade counts (``>= 0``, fit int64). ``winning + losing <= total``
        (scratch trades are neither), enforced.
    sharpe_ratio, sortino_ratio, calmar_ratio, profit_factor, win_rate, max_drawdown_pct
        Full-precision Float64 metrics. Must be finite (``NaN`` / ``±inf``
        rejected). NEVER rounded.
    deflated_sharpe
        The Deflated-Sharpe ratio (multiple-testing-adjusted Sharpe,
        DEC-RANKING-OVERFIT-001) carried for leaderboard transparency, or ``None``
        when the pool could not be deflated / the overfit gate was inactive. When
        present it must be finite (``NaN`` / ``±inf`` rejected); NEVER rounded.
        Round-trips as a Parquet null when ``None``. HONEST SCOPE
        (DEC-STATS-GATE-INTEGRITY-001, R2-SR-F3): the persisted figure is
        **TUPLE-scoped** — deflated against one grid cell's rank-time
        ``N = len(candidates)``, an under-count that materially inflates it
        (measured: DSR 0.230 at ``n_trials=1e3`` vs 0.003 at ``1e7``) — and is
        rendered ``dsr@tupleN`` on the leaderboard (ADR-0008 condition 7). It
        is superseded by the Stage-4 cascade's leaderboard-N DSR GATE
        (filter #2), whose N/V provenance is stamped run-level on the
        ``CascadeReport``.
    avg_entry_price, avg_exit_price
        Price-family representatives as :class:`~decimal.Decimal` rupees. Must be
        finite and ``>= 0``; normalised to two decimals via
        :func:`brutex.money.round_money` (``ROUND_HALF_UP``) at construction.

    Raises
    ------
    ResultsStoreError
        On a malformed ``run_id``, an empty context label, an out-of-range or
        negative-where-absolute money field, a negative or inconsistent trade
        count, a non-finite metric, or a non-finite / negative price.
    """

    run_id: str
    instrument: str
    tf: str
    direction: str
    mode: str
    mask_bits: int
    net_pnl_paisa: int
    gross_profit_paisa: int
    gross_loss_paisa: int
    max_drawdown_paisa: int
    expectancy_paisa: int
    total_trades: int
    winning_trades: int
    losing_trades: int
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    profit_factor: float
    win_rate: float
    max_drawdown_pct: float
    avg_entry_price: Decimal
    avg_exit_price: Decimal
    deflated_sharpe: float | None = None
    #: Derived from ``mask_bits`` in ``__post_init__`` — never an init arg.
    condition_names: str = field(init=False, default="")

    def __post_init__(self) -> None:
        _validate_run_id(self.run_id)
        for name in ("instrument", "tf", "direction", "mode"):
            if not getattr(self, name):
                raise ResultsStoreError(f"ResultRow.{name} must be non-empty")

        if not 0 <= self.mask_bits <= MASK_BITS_MAX:
            raise ResultsStoreError(
                f"ResultRow.mask_bits={self.mask_bits} is out of range "
                f"[0, {MASK_BITS_MAX}] (the {TOTAL_CONDITIONS} defined condition bits; "
                f"bits >= {TOTAL_CONDITIONS} are reserved/undefined and not allowed)"
            )
        # Derive the readable decode from the SINGLE source of truth (the vocabulary
        # name table) so condition_names can never drift from mask_bits.
        object.__setattr__(
            self,
            "condition_names",
            CONDITION_NAME_SEP.join(matched_condition_names(self.mask_bits)),
        )

        for name in (*_MONEY_PAISA_FIELDS, *_COUNT_FIELDS):
            value = getattr(self, name)
            if not _INT64_MIN <= value <= _INT64_MAX:
                raise ResultsStoreError(f"ResultRow.{name}={value} is out of signed int64 range")
        for name in ("gross_profit_paisa", "gross_loss_paisa", "max_drawdown_paisa"):
            value = getattr(self, name)
            if value < 0:
                raise ResultsStoreError(
                    f"ResultRow.{name} is an absolute magnitude and must be >= 0, got {value}"
                )
        for name in _COUNT_FIELDS:
            value = getattr(self, name)
            if value < 0:
                raise ResultsStoreError(f"ResultRow.{name} must be >= 0, got {value}")
        if self.winning_trades + self.losing_trades > self.total_trades:
            raise ResultsStoreError(
                f"ResultRow: winning_trades ({self.winning_trades}) + losing_trades "
                f"({self.losing_trades}) exceeds total_trades ({self.total_trades})"
            )

        for name in _METRIC_FIELDS:
            value = getattr(self, name)
            if not math.isfinite(value):
                raise ResultsStoreError(
                    f"ResultRow.{name} must be a finite float (no NaN/inf — "
                    f"determinism + ranking integrity), got {value!r}"
                )
            # Canonicalise signed zero (-0.0 → +0.0) so two logically-equal rows
            # are ALWAYS byte-identical. `value + 0.0` is exact (bit-unchanged)
            # for every other finite double — this is NOT rounding, only the
            # sign of zero is normalised (Golden Rule #4 byte-determinism).
            object.__setattr__(self, name, value + 0.0)

        for name in _NULLABLE_METRIC_FIELDS:
            value = getattr(self, name)
            if value is None:
                continue
            if not math.isfinite(value):
                raise ResultsStoreError(
                    f"ResultRow.{name} must be a finite float or None (no NaN/inf — "
                    f"determinism + ranking integrity), got {value!r}"
                )
            object.__setattr__(self, name, value + 0.0)  # -0.0 → +0.0 (byte-determinism)

        for name in _PRICE_FIELDS:
            price = getattr(self, name)
            if not price.is_finite():
                raise ResultsStoreError(f"ResultRow.{name} must be a finite price, got {price!r}")
            if price < 0:
                raise ResultsStoreError(f"ResultRow.{name} must be >= 0, got {price}")
            # Snap to the 2dp tick grid so the row is canonical (DEC-SPOT-PRICE-2DP-001).
            object.__setattr__(self, name, round_money(price))


@dataclass(frozen=True, slots=True)
class FlushResult:
    """Outcome of a successful :meth:`ResultsStore.flush`.

    Attributes
    ----------
    path
        The Parquet file written.
    row_count
        Number of result rows in the file.
    content_hash
        ``sha256`` hex digest of the file bytes (64 chars) — the content
        address. Stable across re-writes of the same row set (Golden Rule #4).
    bytes_written
        Size of the final Parquet file in bytes.
    """

    path: Path
    row_count: int
    content_hash: str
    bytes_written: int


class ResultsStore:
    """Deterministic ``run_id → ResultRow`` store backed by a single Parquet file.

    Sized for the funnel's final 10²–10⁴ finalist rows
    (``TRACK1_BACKTEST_SPEC.md``), so the whole index lives in memory: O(1)
    :meth:`append` and O(1) :meth:`get`, with :meth:`flush` serialising a
    deterministic snapshot and :meth:`load` reading one back.

    Parameters
    ----------
    path
        Destination Parquet file. Created (with parents) on :meth:`flush`.
    """

    def __init__(self, *, path: Path) -> None:
        self._path = path
        self._rows: dict[str, ResultRow] = {}

    @property
    def path(self) -> Path:
        """Return the configured destination Parquet path."""
        return self._path

    def __len__(self) -> int:
        """Return the number of stored result rows."""
        return len(self._rows)

    def __contains__(self, run_id: str) -> bool:
        """Return ``True`` iff *run_id* has a stored row (O(1))."""
        return run_id in self._rows

    def append(self, row: ResultRow) -> bool:
        """Insert *row* keyed by its ``run_id``, idempotently (O(1)).

        Parameters
        ----------
        row
            The result row to store.

        Returns
        -------
        bool
            ``True`` if the row was newly added; ``False`` if an identical row
            for the same ``run_id`` was already present (a silent no-op — the
            idempotence guarantee).

        Raises
        ------
        ResultsStoreError
            If a *different* row is already stored under the same ``run_id`` —
            a run-identity collision means upstream non-determinism and is never
            silently overwritten (Golden Rule #4).
        """
        existing = self._rows.get(row.run_id)
        if existing is not None:
            if existing == row:
                return False
            differing = tuple(
                f.name
                for f in fields(ResultRow)
                if getattr(existing, f.name) != getattr(row, f.name)
            )
            _LOG.error("results_store.conflict", run_id=row.run_id, differing_fields=differing)
            raise ResultsStoreError(
                f"run-identity conflict for {row.run_id}: a different result is already "
                f"stored (differing fields: {', '.join(differing)}). Same run-identity must "
                f"map to one result (Golden Rule #4) — refusing to overwrite."
            )
        self._rows[row.run_id] = row
        return True

    def extend(self, rows: Iterable[ResultRow]) -> int:
        """Append every row in *rows*; return how many were newly added."""
        return sum(1 for row in rows if self.append(row))

    def get(self, run_id: str) -> ResultRow | None:
        """Return the stored row for *run_id*, or ``None`` (O(1) lookup)."""
        return self._rows.get(run_id)

    def rows(self) -> tuple[ResultRow, ...]:
        """Return all rows in deterministic ``run_id``-sorted order."""
        return tuple(self._rows[run_id] for run_id in sorted(self._rows))

    def top_k(
        self, k: int, *, by: str = "sharpe_ratio", descending: bool = True
    ) -> tuple[ResultRow, ...]:
        """Return the top *k* rows ranked by the numeric column *by*.

        Deterministic: rows are ordered by *by* (descending by default), ties
        broken by ``run_id`` ascending, so the leaderboard is identical for any
        insertion order. Returns fewer than *k* rows if the store holds fewer.

        Parameters
        ----------
        k
            Number of leaders to return (``>= 1``).
        by
            Column to rank by — one of the metric, money or count columns
            (see :data:`_RANKABLE_FIELDS`).
        descending
            ``True`` (default) ranks largest-first; ``False`` smallest-first.

        Raises
        ------
        ResultsStoreError
            If ``k < 1`` or *by* is not a rankable column.
        """
        if k < 1:
            raise ResultsStoreError(f"top_k: k must be >= 1, got {k}")
        if by not in _RANKABLE_FIELDS:
            raise ResultsStoreError(
                f"top_k: unknown ranking field {by!r}; choose one of {sorted(_RANKABLE_FIELDS)}"
            )
        getter = _field_getter(by)
        # Stable two-pass sort: run_id asc first (the tie-break), then by the
        # ranking column — Python's stable sort preserves run_id order in ties.
        ordered = sorted(self._rows.values(), key=lambda r: r.run_id)
        ordered.sort(key=getter, reverse=descending)
        return tuple(ordered[:k])

    def flush(self) -> FlushResult:
        """Write a deterministic Parquet snapshot of the store to :attr:`path`.

        Re-flushing an unchanged store produces a byte-identical file
        (Golden Rule #4 / #7). Returns a :class:`FlushResult` with the content
        hash so callers can record it in the catalog / audit log.

        Raises
        ------
        ResultsStoreError
            If ``polars`` is not installed.
        """
        content = self._render_bytes()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(self._path, content)
        result = FlushResult(
            path=self._path,
            row_count=len(self._rows),
            content_hash=hashlib.sha256(content).hexdigest(),
            bytes_written=len(content),
        )
        _LOG.info(
            "results_store.flush",
            path=str(self._path),
            row_count=result.row_count,
            content_hash=result.content_hash,
            bytes_written=result.bytes_written,
        )
        return result

    @classmethod
    def load(cls, path: Path) -> ResultsStore:
        """Read a Parquet file written by :meth:`flush` back into a new store.

        Parameters
        ----------
        path
            The Parquet file to read.

        Raises
        ------
        ResultsStoreError
            If *path* does not exist or ``polars`` is not installed.
        ResultsStoreSchemaError
            If the file is not a readable, decodable results-store Parquet —
            corrupt/truncated/foreign bytes, an old-schema file missing a
            required column, a wrong-dtype column, or invalid row content are
            ALL wrapped into the store's OWN typed error naming *path*, so
            every reader — including the ``/results`` dashboard scan and the
            sweep-verify doctor — handles damage uniformly and a raw
            ``polars`` / ``KeyError`` error can never escape
            (DEC-SWEEP-VERIFY-001; G14, gap-register ruling 5).
        """
        if not path.exists():
            raise ResultsStoreError(f"results store not found at {path}")
        try:
            import polars as pl
        except ImportError as exc:
            raise ResultsStoreError(_POLARS_HINT) from exc

        store = cls(path=path)
        try:
            df = pl.read_parquet(path)
        except (pl.exceptions.PolarsError, OSError) as exc:
            raise ResultsStoreSchemaError(f"unreadable results store at {path}: {exc}") from exc
        try:
            for record in df.iter_rows(named=True):
                store.append(_row_from_record(record))
        except (ResultsStoreError, KeyError, TypeError, ValueError, ArithmeticError) as exc:
            # ANY schema/decode failure — a missing required column (KeyError),
            # a null in a required cell (TypeError), a wrong-dtype value
            # (ValueError / decimal.InvalidOperation ⊂ ArithmeticError), or row
            # content failing ResultRow validation / the in-file duplicate
            # conflict (ResultsStoreError) — surfaces as the typed damage
            # class naming the store path, never a raw error (G14).
            raise ResultsStoreSchemaError(
                f"results store at {path} is not decodable as the current "
                f"ResultRow schema (old-schema / foreign / damaged file): {exc!r}"
            ) from exc
        _LOG.info("results_store.load", path=str(path), row_count=len(store))
        return store

    # ── Private ────────────────────────────────────────────────────────────

    def _render_bytes(self) -> bytes:
        """Serialise the store to a deterministic Parquet byte blob."""
        try:
            import polars as pl
        except ImportError as exc:
            raise ResultsStoreError(_POLARS_HINT) from exc

        ordered = [self._rows[run_id] for run_id in sorted(self._rows)]
        columns: dict[str, list[object]] = {}
        for name in _STRING_FIELDS:
            columns[name] = [getattr(row, name) for row in ordered]
        # The u128 mask is encoded as two signed-Int64 limb columns (lo, hi).
        columns["mask_bits_lo"] = [_encode_limb(row.mask_bits, hi=False) for row in ordered]
        columns["mask_bits_hi"] = [_encode_limb(row.mask_bits, hi=True) for row in ordered]
        for name in (
            *_MONEY_PAISA_FIELDS,
            *_COUNT_FIELDS,
            *_METRIC_FIELDS,
            *_NULLABLE_METRIC_FIELDS,
        ):
            # Nullable metrics keep ``None`` (→ Parquet null); others are concrete.
            columns[name] = [getattr(row, name) for row in ordered]
        for name in _PRICE_FIELDS:
            # Already 2dp-snapped at construction; Decimal → float for Float64 storage.
            columns[name] = [float(getattr(row, name)) for row in ordered]

        # Schema built inline (like brutex/data/parquet_writer.py) so the polars
        # dtype is inferred; matched to columns by name. Column order follows the
        # fixed insertion order above (identity → mask limbs → money → counts →
        # metrics → deflated → prices) for a reproducible byte stream.
        df = pl.DataFrame(
            columns,
            schema={
                **dict.fromkeys(_STRING_FIELDS, pl.Utf8),
                **dict.fromkeys(
                    (*_MASK_LIMB_FIELDS, *_MONEY_PAISA_FIELDS, *_COUNT_FIELDS), pl.Int64
                ),
                **dict.fromkeys(
                    (*_METRIC_FIELDS, *_NULLABLE_METRIC_FIELDS, *_PRICE_FIELDS), pl.Float64
                ),
            },
        )
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


# ── Module-level helpers ─────────────────────────────────────────────────────

#: Reused install hint for the optional ``polars`` dependency.
_POLARS_HINT: Final[str] = (
    "ResultsStore requires 'polars' — install with 'pip install -e .[data]' "
    "to enable the P2 data lake"
)


def _validate_run_id(run_id: str) -> None:
    """Raise :class:`ResultsStoreError` unless *run_id* is canonical BLAKE3 hex.

    Canonical = exactly :data:`COMBO_HASH_HEX_LEN` lowercase hex chars, matching
    ``brutex.strategies.combo.combo_hash(...)``'s ``.hex()`` output so the dedup
    key is unambiguous (no upper/lower aliasing).
    """
    if len(run_id) != COMBO_HASH_HEX_LEN:
        raise ResultsStoreError(
            f"run_id must be {COMBO_HASH_HEX_LEN}-char BLAKE3 hex, got {len(run_id)} chars"
        )
    if run_id != run_id.lower():
        raise ResultsStoreError(f"run_id must be lowercase hex, got {run_id!r}")
    try:
        bytes.fromhex(run_id)
    except ValueError as exc:
        raise ResultsStoreError(f"run_id {run_id!r} is not valid hex") from exc


def _field_getter(name: str) -> Callable[[ResultRow], float]:
    """Return a typed accessor for a rankable numeric column.

    Money / count columns return ``int`` at runtime (exact — no float coercion,
    so int64 paisa above 2**53 still rank correctly); the ``float`` annotation
    is the numeric-tower upper bound for the sort key.
    """

    def _get(row: ResultRow) -> float:
        return getattr(row, name)  # type: ignore[no-any-return]

    return _get


def _encode_limb(mask: int, *, hi: bool) -> int:
    """Encode one u64 limb of a u128 ``mask`` as a SIGNED int64 for Parquet ``Int64``.

    A u64 (0..2**64-1) does not fit a signed int64, so we subtract 2**63 to centre
    it: an unsigned ``u`` maps to the signed value ``u - 2**63`` ∈ [-2**63, 2**63-1].
    :func:`_decode_mask` inverts it. ``hi=False`` returns the low limb
    (``mask & U64``), ``hi=True`` the high limb (``mask >> 64``).
    """
    limb = (mask >> 64) if hi else (mask & _U64_MASK)
    return limb - _U64_TO_I64_OFFSET


def _decode_mask(lo_signed: int, hi_signed: int) -> int:
    """Reassemble a u128 mask from its two SIGNED-int64 limb columns.

    Inverse of :func:`_encode_limb`: add back the 2**63 offset to recover each
    unsigned u64 limb, then ``hi << 64 | lo``.
    """
    lo = lo_signed + _U64_TO_I64_OFFSET
    hi = hi_signed + _U64_TO_I64_OFFSET
    return (hi << 64) | lo


def _row_from_record(record: dict[str, object]) -> ResultRow:
    """Rebuild a :class:`ResultRow` from one polars ``iter_rows(named=True)`` record.

    Prices come back as Float64; ``Decimal(str(float))`` then the construction
    snap reproduces the exact 2dp value, keeping the round-trip bit-stable.
    ``condition_names`` is NOT read back — it is re-derived from ``mask_bits`` at
    construction (single source of truth), so the stored string can never desync.
    The u128 mask is reassembled from the two signed-Int64 limb columns
    (``mask_bits_lo`` / ``mask_bits_hi``).

    Optional columns are read PRESENCE-TOLERANTLY via ``record.get`` (the
    WindowLedgerRecord precedent — gap-register ruling 5): a store written
    BEFORE the column existed loads the field as an honest ``None``, exactly
    like a stored Parquet null — never a fabricated value, never a "corrupt"
    verdict. ``deflated_sharpe`` is the template (a pre-DEC-RANKING-OVERFIT-001
    store carries no such column). Every FUTURE :class:`ResultRow` extension
    MUST follow it: optional field defaulting ``None`` + ``record.get`` here +
    keep the pre-extension golden
    (``tests/golden/test_results_store_pre_extension_golden.py``) green.
    Required columns stay direct-indexed on purpose — their absence IS an
    old-schema file, wrapped by :meth:`ResultsStore.load` into
    :class:`ResultsStoreSchemaError`.
    """
    deflated = record.get("deflated_sharpe")
    return ResultRow(
        run_id=str(record["run_id"]),
        instrument=str(record["instrument"]),
        tf=str(record["tf"]),
        direction=str(record["direction"]),
        mode=str(record["mode"]),
        mask_bits=_decode_mask(
            int(record["mask_bits_lo"]),  # type: ignore[call-overload]
            int(record["mask_bits_hi"]),  # type: ignore[call-overload]
        ),
        deflated_sharpe=None if deflated is None else float(deflated),  # type: ignore[arg-type]
        net_pnl_paisa=int(record["net_pnl_paisa"]),  # type: ignore[call-overload]
        gross_profit_paisa=int(record["gross_profit_paisa"]),  # type: ignore[call-overload]
        gross_loss_paisa=int(record["gross_loss_paisa"]),  # type: ignore[call-overload]
        max_drawdown_paisa=int(record["max_drawdown_paisa"]),  # type: ignore[call-overload]
        expectancy_paisa=int(record["expectancy_paisa"]),  # type: ignore[call-overload]
        total_trades=int(record["total_trades"]),  # type: ignore[call-overload]
        winning_trades=int(record["winning_trades"]),  # type: ignore[call-overload]
        losing_trades=int(record["losing_trades"]),  # type: ignore[call-overload]
        sharpe_ratio=float(record["sharpe_ratio"]),  # type: ignore[arg-type]
        sortino_ratio=float(record["sortino_ratio"]),  # type: ignore[arg-type]
        calmar_ratio=float(record["calmar_ratio"]),  # type: ignore[arg-type]
        profit_factor=float(record["profit_factor"]),  # type: ignore[arg-type]
        win_rate=float(record["win_rate"]),  # type: ignore[arg-type]
        max_drawdown_pct=float(record["max_drawdown_pct"]),  # type: ignore[arg-type]
        avg_entry_price=Decimal(str(record["avg_entry_price"])),
        avg_exit_price=Decimal(str(record["avg_exit_price"])),
    )


def _atomic_write(dest: Path, content: bytes) -> None:
    """Atomically write *content* to *dest* via tempfile + fsync + rename.

    Delegates to the COMMON guard-free :func:`brutex.data.atomic_io.atomic_write_simple`
    (the ONE tmp→``fsync``→``os.replace``→cleanup, CLAUDE.md §11,
    DEC-ATOMIC-WRITE-CONSOLIDATE-001): a kill mid-write never leaves a half-written
    Parquet on disk. Guard-FREE on purpose — a raw ``OSError`` (e.g. ``ENOSPC``)
    propagates unchanged so the caller's ``ResultsStoreError`` contract is preserved
    (the engine is NOT wired for the lake's ``LakeWriteHalt`` global-stop).
    """
    atomic_write_simple(dest, lambda tmp: tmp.write_bytes(content))


__all__ = [
    "CONDITION_NAME_SEP",
    "MASK_BITS_MAX",
    "FlushResult",
    "ResultRow",
    "ResultsStore",
    "ResultsStoreError",
    "ResultsStoreSchemaError",
]
