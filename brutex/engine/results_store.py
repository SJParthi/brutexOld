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
* **REFERENCE-ONLY India-VIX summary → nullable Float64, appended LAST**
  (DEC-VIX-STAMP-WIRE-001). ``vix_mean`` / ``vix_min`` / ``vix_max`` carry the
  combo's per-trade VIX-stamp aggregate
  (:func:`brutex.engine.reference_wire.aggregate_trade_vix`) at full precision
  (never rounded). ``None`` is the honest "unknown" (an unstamped run, no VIX
  bar at any trade instant, or a store written before the columns existed) —
  NEVER a fabricated ``0.0``. REFERENCE ONLY (CLAUDE.md §2 Rule 3): the trio is
  deliberately NOT rankable (:data:`_RANKABLE_FIELDS` excludes it) and NEVER
  part of the BLAKE3 run identity.

Optional-column back-compat (the ``WindowLedger.bars_net_new`` template)
------------------------------------------------------------------------
Columns added AFTER a store format ships are **appended LAST** in the Parquet
column order and read back via :func:`_optional_float`: a store written before
the column existed loads that field as ``None`` — the honest unknown, never a
fabricated value. Two such generations exist today: ``deflated_sharpe``
(DEC-SWEEP-WINNERS-BY-NAME-001 era) and the ``vix_mean``/``vix_min``/``vix_max``
trio (DEC-VIX-STAMP-WIRE-001), each pinned by a committed pre-column fixture in
``tests/golden/fixtures/``.

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
overwrite). The ONE sanctioned exception is DIRECTIONAL (DEC-VIX-STAMP-WIRE-001):
the nullable REFERENCE-ONLY VIX trio may be backfilled ``None → value`` by a
stamped redo of the same run (``None`` means *unknown*, not a conflicting fact);
two DIFFERENT known VIX values for one identity still raise — but as the
DISTINCT :class:`VixTrioConflictError`, because VIX is deliberately OUTSIDE the
run identity: a trio clash means the India-VIX *lake* moved between the two
runs (a re-pull backfilled missing VIX bars, or the derived ``1day`` partition
was legitimately re-derived latest-wins), NOT upstream non-determinism. The
grid drivers treat it as a #904 tuple-skip (one tuple redoes, the grid
completes) instead of a grid-wide abort.

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
  pre-DEC-RANKING-OVERFIT-001 store loads it as ``None``; the REFERENCE-ONLY
  ``vix_mean``/``vix_min``/``vix_max`` trio follows it, DEC-VIX-STAMP-WIRE-001).
  The frozen pre-extension golden fixture
  (``tests/golden/fixtures/results_store_pre_extension_v1.parquet`` +
  ``tests/golden/test_results_store_pre_extension_golden.py``) is the guard
  every future extension must keep green.
"""

from __future__ import annotations

import hashlib
import io
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, fields, replace
from decimal import Decimal
from pathlib import Path
from typing import Final

import structlog

from brutex.data.atomic_io import atomic_write_simple
from brutex.engine.period_stats import validate_period_stats
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
#: Float64 **nullable** REFERENCE-ONLY India-VIX columns (DEC-VIX-STAMP-WIRE-001):
#: the combo's ``(mean, min, max)`` over every non-``None`` ``vix_at_entry`` /
#: ``vix_at_exit`` stamp of its finalist trades, produced by the ONE COMMON
#: :func:`brutex.engine.reference_wire.aggregate_trade_vix` fold. Full precision,
#: NEVER rounded; ``None`` = honestly unknown (unstamped run / no VIX bar at any
#: trade instant / a pre-VIX store), NEVER a fabricated ``0.0``. Appended after
#: ``deflated_sharpe`` in the on-disk column order (the optional-column
#: back-compat template; the emit-on-present temporal-stats columns of
#: DEC-SWEEP-TEMPORAL-ROBUSTNESS-001 append after this trio). VIX stays
#: REFERENCE ONLY (CLAUDE.md §2 Rule 3): these are deliberately excluded from
#: :data:`_RANKABLE_FIELDS` and are never an input to the BLAKE3 run identity.
#: PUBLIC because it is the ONE cross-module source of truth for the trio's
#: column names (CLAUDE.md §2 Rule 11 COMMON): ``sweep_verify``'s recount
#: VIX-attribution imports it — never a mirrored literal — so adding a fourth
#: field here can never silently desync the attribution's nulling/schema gates.
VIX_FIELDS: Final[tuple[str, ...]] = ("vix_mean", "vix_min", "vix_max")
#: Utf8 **optional** canonical-JSON temporal-stats columns
#: (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001) — appended after ``deflated_sharpe`` (and,
#: since the club fold, after the VIX trio) per the optional-column LAW (module
#: docstring): ``str | None = None`` fields
#: read via ``record.get`` so a pre-extension store loads them as an honest
#: ``None``. Written EMIT-ON-PRESENT (iff any row carries a value — a pure,
#: deterministic function of the row set), so the all-None flush stays
#: BYTE-IDENTICAL to the pre-extension writer (frozen-golden-pinned).
#: Cell/cross-sum laws live in ONE place: :mod:`brutex.engine.period_stats`.
_TEMPORAL_JSON_FIELDS: Final[tuple[str, ...]] = (
    "stats_by_year_json",
    "stats_by_quarter_json",
    "stats_by_month_json",
    "temporal_judged_years_json",
)

#: Numeric columns a leaderboard may rank by (metrics + money + counts).
#: The nullable ``deflated_sharpe`` and the REFERENCE-ONLY VIX trio are
#: deliberately NOT rankable — VIX must never drive strategy ranking
#: (CLAUDE.md §2 Rule 3) and a nullable column has no total order.
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


class VixTrioConflictError(ResultsStoreError):
    """A redo disagrees with the stored REFERENCE-ONLY VIX trio — and nothing else.

    Raised by :meth:`ResultsStore.append` when the incoming row carries the
    SAME ``run_id`` and byte-equal non-VIX fields, but one of
    :data:`VIX_FIELDS` holds two DIFFERENT known (non-``None``) values.
    Because VIX is deliberately OUTSIDE the BLAKE3 run identity
    (CLAUDE.md §2 Rule 3), this is **NOT evidence of upstream
    non-determinism**: it means the India-VIX *lake* moved between the two
    runs — a re-pull backfilled previously-missing VIX bars, or the derived
    ``1day`` partition was legitimately re-derived latest-wins
    (``spot_daily_resample``) — so the redo aggregated a different trio under
    an unchanged identity (DEC-VIX-STAMP-WIRE-001 round-2 panel, MAJOR).

    Kept a DISTINCT type so the grid drivers (``run_sweep_grid`` /
    ``run_supervised_grid`` / ``_run_tuple_in_worker``) can ride the #904
    "Resilient: skip + continue" tuple-skip contract — ONE tuple is skipped +
    counted + audited ``TUPLE_FAILED`` and the grid completes — and the
    Stage-3 pass (``run_stage3_pass``) can skip + count the ONE conflicted
    refined cell (``cells_conflicted`` +
    ``stage3_wire.cell_vix_conflict`` — the dominant redo path: a resumed
    launch sentinel-skips its grid tuples but ALWAYS re-appends into the
    pre-existing stage-3 cell stores) — instead of aborting the WHOLE launch
    over a reference-only annotation. Still a :class:`ResultsStoreError`
    subclass, so every existing store-error handler catches it unchanged. At
    the :meth:`ResultsStore.load` boundary it is deliberately CARVED OUT of
    the G14 :class:`ResultsStoreSchemaError` wrap (re-raised distinct), so a
    load-time trio conflict keeps its typed cell-skip contract instead of
    being misread as file damage.
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
    module-level back-compat law). The ONE deliberate carve-out: a load-time
    :class:`VixTrioConflictError` re-raises DISTINCT (never wrapped) so the
    stage-3 cell-skip contract survives the G14 wrap (DEC-VIX-STAMP-WIRE-001
    reconciliation with DEC-SWEEP-RESULTS-DEPTH-001).
    """


class ResultsStoreGateConflictError(ResultsStoreError):
    """A run-identity conflict whose ONLY differing field(s) are nullable gate metric(s).

    The C5 defense-in-depth signature (DEC-SWEEP-TUPLE-DIGEST-GATE-IDENTITY-001):
    a conflict whose sole differing field(s) are the nullable gate-derived
    metric(s) (:data:`_NULLABLE_METRIC_FIELDS` — ``deflated_sharpe``) is the
    fingerprint of a gate-config (overfit / Stage-3) re-run into the SAME store
    address, NOT upstream non-determinism: the mask ``run_id`` deliberately omits
    the gate config (locked — zero-diff on ``sweep_run`` / ``combo``), so an
    all-nullable-metric-only diff is a CONFIG change. Distinct from a generic
    corruption conflict so the grid driver skips + counts the ONE tuple instead
    of aborting the whole grid. With the store address now encoding the
    gate + selection identity (:func:`~brutex.engine.sweep_orchestrator._tuple_digest`)
    this is a belt-and-braces guard, never the primary defence.
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
    vix_mean, vix_min, vix_max
        The REFERENCE-ONLY India-VIX summary trio (DEC-VIX-STAMP-WIRE-001): the
        combo's mean / min / max over every non-``None`` per-trade VIX stamp
        (``vix_at_entry`` + ``vix_at_exit``), from the ONE COMMON
        :func:`brutex.engine.reference_wire.aggregate_trade_vix` fold. Full
        precision, NEVER rounded. ``None`` (the default) is the honest unknown
        — an unstamped run, a trade set with no VIX bar at any instant, or a
        store written before the columns existed — NEVER a fabricated ``0.0``.
        Each present value must be finite; any present pair must respect the
        order law ``vix_min <= vix_mean <= vix_max``. Round-trips as a Parquet
        null when ``None``. NEVER rankable, NEVER in the run identity
        (CLAUDE.md §2 Rule 3).
    stats_by_year_json, stats_by_quarter_json, stats_by_month_json
        OPTIONAL canonical-JSON per-period finalist stats
        (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001): ``{"2021": [n_trades,
        net_pnl_paisa, wins, max_dd_paisa], …}`` with ``"YYYY"`` / ``"YYYYQn"``
        / ``"YYYY-MM"`` keys — int cells ONLY (exact paisa; ``win_rate`` is
        derivable, never stored), zero-trade buckets ABSENT. Cross-sum-VALIDATED
        at construction/load against the row's own ``total_trades`` /
        ``net_pnl_paisa`` / ``winning_trades`` and the quarter/year == Σ-months
        identities (:func:`brutex.engine.period_stats.validate_period_stats`),
        then canonically re-encoded — fabricated cells are structurally
        impossible. Per-period ``max_dd_paisa`` is realized-at-exit,
        trade-close-granular, INTRA-PERIOD-RESET — it splits (and so
        systematically UNDERSTATES) boundary-spanning drawdowns, does NOT roll
        up across horizons, and never supersedes the row's global
        ``max_drawdown_paisa``, the ONLY authoritative risk figure. All four
        temporal columns are all-present or all-``None`` per row (a partial set
        is a torn capture — rejected).
    temporal_judged_years_json
        OPTIONAL canonical-JSON judged-year list (``["2020", …]``) — the
        serve-time denominator that makes a zero-trade judged year (absent
        bucket, hollow cell) distinguishable from an out-of-window year (not in
        this list). Present iff the three stats columns are.

    Raises
    ------
    ResultsStoreError
        On a malformed ``run_id``, an empty context label, an out-of-range or
        negative-where-absolute money field, a negative or inconsistent trade
        count, a non-finite metric, a non-finite / negative price, a VIX
        trio violating the ``min <= mean <= max`` order law, or an
        invalid / torn / cross-sum-violating temporal column set.
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
    vix_mean: float | None = None
    vix_min: float | None = None
    vix_max: float | None = None
    # ── Optional temporal-stats columns (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001),
    #    appended after deflated_sharpe + the VIX trio per the optional-column LAW ──
    stats_by_year_json: str | None = None
    stats_by_quarter_json: str | None = None
    stats_by_month_json: str | None = None
    temporal_judged_years_json: str | None = None
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

        for name in (*_NULLABLE_METRIC_FIELDS, *VIX_FIELDS):
            value = getattr(self, name)
            if value is None:
                continue
            if not math.isfinite(value):
                raise ResultsStoreError(
                    f"ResultRow.{name} must be a finite float or None (no NaN/inf — "
                    f"determinism + ranking integrity), got {value!r}"
                )
            object.__setattr__(self, name, value + 0.0)  # -0.0 → +0.0 (byte-determinism)

        # VIX order law: every PRESENT pair respects min <= mean <= max. Pairwise
        # (not all-three-present-only) so a part-null trio — legal, e.g. a
        # hand-degraded or legacy-merged row — is still checked where it can be.
        for lo_name, hi_name in (
            ("vix_min", "vix_mean"),
            ("vix_mean", "vix_max"),
            ("vix_min", "vix_max"),
        ):
            lo, hi = getattr(self, lo_name), getattr(self, hi_name)
            if lo is not None and hi is not None and lo > hi:
                raise ResultsStoreError(
                    f"ResultRow: {lo_name} ({lo}) exceeds {hi_name} ({hi}) — the VIX "
                    f"summary trio must respect vix_min <= vix_mean <= vix_max"
                )

        for name in _PRICE_FIELDS:
            price = getattr(self, name)
            if not price.is_finite():
                raise ResultsStoreError(f"ResultRow.{name} must be a finite price, got {price!r}")
            if price < 0:
                raise ResultsStoreError(f"ResultRow.{name} must be >= 0, got {price}")
            # Snap to the 2dp tick grid so the row is canonical (DEC-SPOT-PRICE-2DP-001).
            object.__setattr__(self, name, round_money(price))

        self._validate_temporal_columns()

    def _validate_temporal_columns(self) -> None:
        """Validate + canonicalise the four optional temporal-stats columns.

        DEC-SWEEP-TEMPORAL-ROBUSTNESS-001: the columns are all-present or
        all-``None`` per row (a partial set is a TORN capture — rejected loudly,
        never half-loaded); when present they are decoded, cross-sum-validated
        against this row's own totals, and re-encoded CANONICALLY in place
        (:func:`brutex.engine.period_stats.validate_period_stats` — one law,
        one home) so the in-memory row is byte-canonical regardless of the
        stored spelling (the price-snap philosophy — Golden Rule #4).
        """
        values = [getattr(self, name) for name in _TEMPORAL_JSON_FIELDS]
        present = [value for value in values if value is not None]
        if not present:
            return
        if len(present) != len(_TEMPORAL_JSON_FIELDS):
            missing = [name for name in _TEMPORAL_JSON_FIELDS if getattr(self, name) is None]
            raise ResultsStoreError(
                f"ResultRow temporal columns must be all-present or all-None "
                f"(a partial set is a torn capture); missing: {', '.join(missing)}"
            )
        year_json, quarter_json, month_json, judged_json = values
        try:
            canonical = validate_period_stats(
                stats_by_year_json=str(year_json),
                stats_by_quarter_json=str(quarter_json),
                stats_by_month_json=str(month_json),
                temporal_judged_years_json=str(judged_json),
                total_trades=self.total_trades,
                net_pnl_paisa=self.net_pnl_paisa,
                winning_trades=self.winning_trades,
            )
        except ValueError as exc:
            raise ResultsStoreError(f"ResultRow temporal stats invalid: {exc}") from exc
        for name, value in zip(_TEMPORAL_JSON_FIELDS, canonical, strict=True):
            object.__setattr__(self, name, value)


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

        A re-append under an already-stored ``run_id`` follows the DIRECTIONAL
        three-branch rule (DEC-VIX-STAMP-WIRE-001) on the nullable
        REFERENCE-ONLY VIX trio (:data:`VIX_FIELDS`), per field:

        1. **Identical value** (including both-``None``) → OK, a silent no-op.
        2. **Backfill ``None`` → value** → OK, the stored row is UPGRADED to
           the union (a stamped redo may fill in what an unstamped run
           honestly didn't know; the reverse direction ``value → None`` never
           erases — an unstamped redo is *less* informed, not conflicting).
        3. **Two different known values** → :class:`VixTrioConflictError` —
           NOT a run-identity breach (VIX is deliberately outside the
           identity): the India-VIX lake moved between the two runs, so the
           typed raise names that cause + remedy and the grid drivers demote
           it to a #904 tuple-skip instead of a grid abort.

        Every NON-VIX field keeps the strict rule: any difference raises the
        plain :class:`ResultsStoreError` run-identity conflict (a genuine
        reproducibility breach — the backfill never masks it).

        Parameters
        ----------
        row
            The result row to store.

        Returns
        -------
        bool
            ``True`` if the store content changed with a VIX consequence —
            the row was newly added OR an existing row was
            VIX-backfill-upgraded; ``False`` if the stored row already carried
            everything *row* knows (the idempotent no-op) OR the ONLY change
            was a temporal ENRICHMENT (below) — per the D16 contract a
            temporal enrichment never grows the row count and reports
            ``False`` even though the stored bytes changed.

        Raises
        ------
        ResultsStoreError
            If a *different* row is already stored under the same ``run_id`` —
            a run-identity collision means upstream non-determinism and is never
            silently overwritten (Golden Rule #4). The VIX backfill branch and
            the temporal enrichment carve-out are the TWO sanctioned,
            directional exceptions described below.
        VixTrioConflictError
            If the difference includes two different known values on a VIX
            field — the VIX lake moved between runs (see the class docstring).

        **Temporal enrichment carve-out (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001,
        the D16 rule — ships with the columns, never separable):** the
        same-run_id conflict compare covers every REQUIRED field ALWAYS; for
        the four optional :data:`_TEMPORAL_JSON_FIELDS`, ``None`` on either
        side is NOT a conflict and the POPULATED value WINS (a deterministic
        enrichment-in-place, logged ``results_store.temporal_enriched``), so a
        capture-toggled redo over an existing store enriches instead of
        crashing mid-grid. Both-present-and-DIFFERENT stays a conflict: the
        stats are a pure function of the trades, which are a pure function of
        the run identity — a true mismatch is a reproducibility breach.
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
            if any(
                name not in VIX_FIELDS and name not in _TEMPORAL_JSON_FIELDS for name in differing
            ):
                _LOG.error("results_store.conflict", run_id=row.run_id, differing_fields=differing)
                if set(differing) <= set(_NULLABLE_METRIC_FIELDS):
                    # C5 (DEC-SWEEP-TUPLE-DIGEST-GATE-IDENTITY-001): the ONLY diff is
                    # a nullable gate-derived metric — the fingerprint of a
                    # gate-config (overfit / Stage-3) re-run into the same store
                    # address, not upstream non-determinism. Raise the typed subclass
                    # so the driver skips + counts this ONE tuple, never a global
                    # grid abort. Covers None-vs-float (gate on/off) AND float-vs-float
                    # (two overfit configs).
                    raise ResultsStoreGateConflictError(
                        f"run-identity conflict for {row.run_id}: the ONLY differing field(s) "
                        f"({', '.join(differing)}) are gate-derived DSR metric(s) — a gate-config "
                        f"change (overfit / Stage-3) re-ran into the same store address. The store "
                        f"address now encodes the gate+selection identity "
                        f"(DEC-SWEEP-TUPLE-DIGEST-GATE-IDENTITY-001); re-run with a fresh results "
                        f"root or the matching config. Refusing to overwrite (Golden Rule #4)."
                    )
                raise ResultsStoreError(
                    f"run-identity conflict for {row.run_id}: a different result is already "
                    f"stored (differing fields: {', '.join(differing)}). Same run-identity must "
                    f"map to one result (Golden Rule #4) — refusing to overwrite."
                )
            # Only the two sanctioned overlay families (the VIX trio and/or the
            # temporal columns) differ — apply each family's directional merge.
            merged = existing
            if any(name in _TEMPORAL_JSON_FIELDS for name in differing):
                # Align the VIX trio so the enrichment helper's non-temporal
                # equality precondition holds; the VIX merge runs separately below.
                aligned = replace(
                    row,
                    vix_mean=merged.vix_mean,
                    vix_min=merged.vix_min,
                    vix_max=merged.vix_max,
                )
                enriched = _merge_temporal_enrichment(merged, aligned)
                if enriched is None:
                    _LOG.error(
                        "results_store.conflict", run_id=row.run_id, differing_fields=differing
                    )
                    raise ResultsStoreError(
                        f"run-identity conflict for {row.run_id}: a different result is already "
                        f"stored (differing fields: {', '.join(differing)}). Same run-identity must "
                        f"map to one result (Golden Rule #4) — refusing to overwrite."
                    )
                if enriched != merged:
                    _LOG.info(
                        "results_store.temporal_enriched",
                        run_id=row.run_id,
                        enriched_fields=tuple(
                            name
                            for name in _TEMPORAL_JSON_FIELDS
                            if getattr(merged, name) is None and getattr(enriched, name) is not None
                        ),
                    )
                merged = enriched
            vix_backfilled: tuple[str, ...] = ()
            if any(name in VIX_FIELDS for name in differing):
                vix_merged = _merge_vix_backfill(merged, row)
                vix_backfilled = tuple(
                    name
                    for name in VIX_FIELDS
                    if getattr(merged, name) is None and getattr(vix_merged, name) is not None
                )
                merged = vix_merged
            if merged == existing:
                # Directional no-op: the incoming row is strictly LESS informed
                # (it only turns known values into None) — the stored row
                # already carries the union, nothing changes.
                return False
            self._rows[row.run_id] = merged
            if vix_backfilled:
                _LOG.info(
                    "results_store.vix_backfill",
                    run_id=row.run_id,
                    backfilled_fields=vix_backfilled,
                )
                return True
            # Temporal-only enrichment: content changed but the row count did
            # not grow — the D16 contract reports False.
            return False
        self._rows[row.run_id] = row
        return True

    def extend(self, rows: Iterable[ResultRow]) -> int:
        """Append every row in *rows*; return how many changed the store.

        The count follows :meth:`append`'s return contract: newly added rows
        plus VIX-backfill-upgraded rows (idempotent no-ops are not counted).
        """
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
        VixTrioConflictError
            Carved OUT of the G14 wrap (re-raised distinct): an in-file
            duplicate run_id differing only in non-``None`` VIX values keeps
            its typed cell-skip contract (DEC-VIX-STAMP-WIRE-001).
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
        except VixTrioConflictError:
            # Deliberate carve-out from the G14 wrap (DEC-VIX-STAMP-WIRE-001
            # ∩ DEC-SWEEP-RESULTS-DEPTH-001 reconciliation): an in-file
            # duplicate run_id differing ONLY in non-None VIX values re-raises
            # DISTINCT so the stage-3 pass cell-skips it (the typed tuple-/
            # cell-skip contract) instead of misreading a reference-only
            # annotation clash as verdict-grade file damage.
            raise
        except (
            ResultsStoreError,
            KeyError,
            TypeError,
            ValueError,
            ArithmeticError,
            RecursionError,
        ) as exc:
            # ANY schema/decode failure — a missing required column (KeyError),
            # a null in a required cell (TypeError), a wrong-dtype value
            # (ValueError / decimal.InvalidOperation ⊂ ArithmeticError), row
            # content failing ResultRow validation / the in-file duplicate
            # conflict (ResultsStoreError), or a pathologically nested JSON
            # temporal cell (RecursionError — primary mapping lives in
            # period_stats._loads; this arm is the defence-in-depth belt) —
            # surfaces as the typed damage class naming the store path, never
            # a raw error (G14).
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
        for name in VIX_FIELDS:
            # The nullable REFERENCE-ONLY VIX trio — appended LAST (the
            # optional-column back-compat template); None → Parquet null.
            columns[name] = [getattr(row, name) for row in ordered]

        # EMIT-ON-PRESENT temporal columns (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001):
        # written iff ANY row carries a non-None value — a PURE, deterministic
        # function of the row set (same rows ⇒ same presence verdict ⇒ same
        # schema ⇒ same bytes), so re-flushing an unchanged store stays
        # byte-identical (Golden Rule #4) AND the all-None flush is
        # BYTE-IDENTICAL to the pre-extension writer (pinned by the frozen
        # sweep goldens + the fixture bytes-equality test).
        temporal_present = any(
            getattr(row, name) is not None for row in ordered for name in _TEMPORAL_JSON_FIELDS
        )
        if temporal_present:
            for name in _TEMPORAL_JSON_FIELDS:
                # None keeps None (→ Parquet null) for rows without capture.
                columns[name] = [getattr(row, name) for row in ordered]

        # Schema built inline (like brutex/data/parquet_writer.py) so the polars
        # dtype is inferred; matched to columns by name. Column order follows the
        # fixed insertion order above (identity → mask limbs → money → counts →
        # metrics → deflated → prices → the VIX trio → [temporal,
        # emit-on-present, LAST] — each optional family appended at its own
        # introduction point for legacy-file back-compat) for a reproducible
        # byte stream.
        df = pl.DataFrame(
            columns,
            schema={
                **dict.fromkeys(_STRING_FIELDS, pl.Utf8),
                **dict.fromkeys(
                    (*_MASK_LIMB_FIELDS, *_MONEY_PAISA_FIELDS, *_COUNT_FIELDS), pl.Int64
                ),
                **dict.fromkeys(
                    (*_METRIC_FIELDS, *_NULLABLE_METRIC_FIELDS, *_PRICE_FIELDS, *VIX_FIELDS),
                    pl.Float64,
                ),
                **(dict.fromkeys(_TEMPORAL_JSON_FIELDS, pl.Utf8) if temporal_present else {}),
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


def _merge_vix_backfill(existing: ResultRow, incoming: ResultRow) -> ResultRow:
    """Union-merge two same-``run_id`` rows differing ONLY in the nullable VIX trio.

    The resolution is a per-field UNION under the directional law of
    :meth:`ResultsStore.append` (DEC-VIX-STAMP-WIRE-001): for each of
    :data:`VIX_FIELDS` the known (non-``None``) value wins — ``None`` means
    *honestly unknown*, never a conflicting fact — so a stamped redo backfills
    an unstamped row and an unstamped redo never erases a stamp. The caller
    (:meth:`ResultsStore.append`) has ALREADY established that every non-VIX
    field is equal — a non-VIX difference is the plain run-identity conflict
    and never reaches this merge.

    Raises
    ------
    VixTrioConflictError
        When one VIX field carries two DIFFERENT known values — the India-VIX
        lake moved between the two runs (see the class docstring); the raise
        names the cause + the remedy so the operator is never misdirected at
        "upstream non-determinism". (A mutually-inconsistent backfill — e.g.
        a merged trio violating ``min <= mean <= max`` — still raises the
        plain order-law :class:`ResultsStoreError` from the merged row's own
        construction.)
    """
    conflicting = tuple(
        name
        for name in VIX_FIELDS
        if getattr(existing, name) is not None
        and getattr(incoming, name) is not None
        and getattr(existing, name) != getattr(incoming, name)
    )
    if conflicting:
        _LOG.error(
            "results_store.vix_conflict",
            run_id=existing.run_id,
            conflicting_fields=conflicting,
            stored={name: getattr(existing, name) for name in VIX_FIELDS},
            incoming={name: getattr(incoming, name) for name in VIX_FIELDS},
        )
        raise VixTrioConflictError(
            f"REFERENCE-ONLY VIX trio conflict for {existing.run_id}: the stored row and "
            f"the redo carry different known values for {', '.join(conflicting)} while "
            f"every non-VIX column is identical. VIX is deliberately OUTSIDE the BLAKE3 "
            f"run identity (CLAUDE.md §2 Rule 3), so this is NOT upstream non-determinism "
            f"— the India-VIX lake moved between the two runs (a re-pull backfilled "
            f"missing VIX bars, or the derived 1day partition was re-derived "
            f"latest-wins). Grid drivers skip this ONE tuple and the Stage-3 pass skips "
            f"this ONE refined cell (the #904 skip-and-continue contract) — the rest of "
            f"the launch completes; remedy: quarantine/delete this tuple's results "
            f"store so a redo re-persists it against the current VIX series, or re-run "
            f"after the VIX lake settles."
        )
    resolved: dict[str, float | None] = {
        name: (
            getattr(existing, name)
            if getattr(existing, name) is not None
            else getattr(incoming, name)
        )
        for name in VIX_FIELDS
    }
    # Explicit keywords (not a **splat) so mypy checks each against its field type.
    return replace(
        existing,
        vix_mean=resolved["vix_mean"],
        vix_min=resolved["vix_min"],
        vix_max=resolved["vix_max"],
    )


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


def _optional_float(record: dict[str, object], name: str) -> float | None:
    """Read a nullable Float64 column with optional-column back-compat.

    The ONE ``record.get`` read law for every column added after a store
    format shipped (the WindowLedgerRecord ``bars_net_new`` template; the
    gap-register ruling-5 presence-tolerant law of
    DEC-SWEEP-RESULTS-DEPTH-001, factored ONCE instead of per column): a
    column ABSENT from the file — a store written before the column existed —
    reads ``None`` exactly like a stored Parquet null. The honest unknown,
    never a fabricated value (Golden Rule #1). Pinned by the committed
    pre-column fixtures in ``tests/golden/fixtures/``.
    """
    value = record.get(name)
    return None if value is None else float(value)  # type: ignore[arg-type]


def _row_from_record(record: dict[str, object]) -> ResultRow:
    """Rebuild a :class:`ResultRow` from one polars ``iter_rows(named=True)`` record.

    Prices come back as Float64; ``Decimal(str(float))`` then the construction
    snap reproduces the exact 2dp value, keeping the round-trip bit-stable.
    ``condition_names`` is NOT read back — it is re-derived from ``mask_bits`` at
    construction (single source of truth), so the stored string can never desync.
    The u128 mask is reassembled from the two signed-Int64 limb columns
    (``mask_bits_lo`` / ``mask_bits_hi``).

    Optional columns are read PRESENCE-TOLERANTLY via ``record.get`` (the
    WindowLedgerRecord precedent — gap-register ruling 5), through the ONE
    :func:`_optional_float` helper so the law is written once, not per
    column: a store written BEFORE the column existed loads the field as an
    honest ``None``, exactly like a stored Parquet null — never a fabricated
    value, never a "corrupt" verdict. ``deflated_sharpe`` is the template (a
    pre-DEC-RANKING-OVERFIT-001 store carries no such column); the
    REFERENCE-ONLY ``vix_mean`` / ``vix_min`` / ``vix_max`` trio follows it
    (DEC-VIX-STAMP-WIRE-001). Every FUTURE :class:`ResultRow` extension MUST
    follow it: optional field defaulting ``None`` + :func:`_optional_float`
    here + keep the pre-extension golden
    (``tests/golden/test_results_store_pre_extension_golden.py``) green.
    Required columns stay direct-indexed on purpose — their absence IS an
    old-schema file, wrapped by :meth:`ResultsStore.load` into
    :class:`ResultsStoreSchemaError`.
    """
    # Optional temporal columns (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001) — the SAME
    # presence-tolerant record.get law: absent column OR stored null → None
    # (Utf8 cells, so the float helper does not apply).
    year_json = record.get("stats_by_year_json")
    quarter_json = record.get("stats_by_quarter_json")
    month_json = record.get("stats_by_month_json")
    judged_json = record.get("temporal_judged_years_json")
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
        deflated_sharpe=_optional_float(record, "deflated_sharpe"),
        vix_mean=_optional_float(record, "vix_mean"),
        vix_min=_optional_float(record, "vix_min"),
        vix_max=_optional_float(record, "vix_max"),
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
        stats_by_year_json=None if year_json is None else str(year_json),
        stats_by_quarter_json=None if quarter_json is None else str(quarter_json),
        stats_by_month_json=None if month_json is None else str(month_json),
        temporal_judged_years_json=None if judged_json is None else str(judged_json),
    )


def _merge_temporal_enrichment(existing: ResultRow, row: ResultRow) -> ResultRow | None:
    """The D16 append-enrichment merge — or ``None`` when the pair is a true conflict.

    DEC-SWEEP-TEMPORAL-ROBUSTNESS-001: two rows for one run-identity may
    LEGALLY differ ONLY in the four optional temporal columns, and only in the
    ``None``-vs-populated direction (a capture-toggled redo re-computed the
    SAME finalist with capture on/off). Field-wise: any non-temporal
    difference ⇒ ``None`` (conflict); a temporal field populated on BOTH sides
    with different bytes ⇒ ``None`` (the stats are a pure function of the run
    identity — a mismatch is a reproducibility breach); else the populated
    value wins per field and the merged row is returned. Per-row all-or-none
    validation (``_validate_temporal_columns``) guarantees the merge never
    manufactures a mixed-capture row.
    """
    for f in fields(ResultRow):
        if f.name in _TEMPORAL_JSON_FIELDS:
            continue
        if getattr(existing, f.name) != getattr(row, f.name):
            return None
    merged: dict[str, str | None] = {}
    for name in _TEMPORAL_JSON_FIELDS:
        ours: str | None = getattr(existing, name)
        theirs: str | None = getattr(row, name)
        if ours is not None and theirs is not None and ours != theirs:
            return None
        merged[name] = ours if ours is not None else theirs
    return replace(
        existing,
        stats_by_year_json=merged["stats_by_year_json"],
        stats_by_quarter_json=merged["stats_by_quarter_json"],
        stats_by_month_json=merged["stats_by_month_json"],
        temporal_judged_years_json=merged["temporal_judged_years_json"],
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
    "VIX_FIELDS",
    "FlushResult",
    "ResultRow",
    "ResultsStore",
    "ResultsStoreError",
    "ResultsStoreGateConflictError",
    "ResultsStoreSchemaError",
    "VixTrioConflictError",
]
