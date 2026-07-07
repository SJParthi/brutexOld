"""The global cross-bucket top-100 artifact — builder, writer, validating reader.

DEC-SWEEP-RESULTS-DEPTH-001 (WP5). R21/R26: SHIPPED = the ONE global
"best of the best" leaderboard across every bucket of a grid, precomputed at
run-end as ONE small artifact at the binding P3→P4 contract location::

    <sweeps>/runs/<run_id>/top100.json        # ordered entries (≤ 100)
    <sweeps>/runs/<run_id>/top100.stamp.json  # {stage, run_id, content_hash, produced_at_utc}

``run_id`` here is the GRID identity
(:func:`brutex.engine.sweep_supervisor.grid_identity` — the P2 ``runs/``
convention); each ENTRY's ``run_id`` stays the per-mask 64-hex store row key.

## Contract shape (binding — sweep-parallel-integration-contracts)

Every entry carries the contract keys verbatim — ``{rank, mask_hex,
conditions[], tf, direction, arm, metrics{…full precision…}, regime{…},
vix_band{…}, family_id}`` — plus additive keys (``run_id``, ``instrument``,
``family_members``, ``reasons``, ``margin_roi``, ``equity_ref``). The ordered
list order IS the rank order (validated on load).

## Pool + ranking laws (R30 / O-h / O-o baked decisions)

* Pool = every stored winner of THIS grid's tuples offered by the run-end
  pass (rows without a current agg sidecar, zero-trade rows and parity-failed
  rows are excluded by the pass and COUNTED in the artifact's ``source``
  honesty header — old-era runs stay drillable on /results, never co-ranked
  silently).
* The 5 hard gates are RECOMPUTED from ``(row, agg)`` via
  :func:`passes_hard_gates` — the exact
  :func:`brutex.ranking.passes_gates` law re-expressed over the persisted
  row + the recomputed aggregate (``win_loss_ratio`` /
  ``max_consecutive_losses`` are not persisted on :class:`ResultRow`; a test
  cross-pins every constant against ``brutex.ranking``'s so the two can
  never drift).
* Twin families (R34) collapse the gate-passing pool to representatives —
  the top-100 is 100 genuinely different behaviors; members are listed per
  entry.
* Default ranking = ``min_dd_first`` (R30: least pain wins) — smallest
  ``max_drawdown_pct`` first → composite score (full precision, from the
  agg) descending → fewest ``max_consecutive_losses`` → ``run_id`` ASC
  (stable pre-sort). Alternates: ``composite`` / ``profit_factor`` /
  ``win_rate`` / ``calmar``, each descending on the RAW row/agg value with
  the same ``run_id``-ASC stable tie law. An unknown preset degrades to the
  default, logged (the /results idiom).

WP3-ABSENCE NOTE (honest seam): the design homes the preset table in
``brutex.ranking.presets`` and the dual-language reasons / margin-ROI in
``brutex.ranking.phrases`` / ``brutex.engine.margin_history`` (WP3). Those
modules do not exist yet, so THIS module owns the minimal R30 ordering law
and exposes ``reason_provider`` / ``margin_roi_provider`` injection seams;
absent providers yield an honest ``None`` per entry plus
``reasons_wired=False`` / ``margin_roi_wired=False`` in the ``source``
header — never invented text (R27/R33/R47). When WP3 lands, its modules plug
into these seams (merge-time wiring per the parallel-integration contract).

## O() + damage envelope (R26 / R42)

* ``build_top100`` is ONE O(pool · log pool) pass at run-end (R29's
  sanctioned "artifact FIRST build = one O(results) pass").
* :func:`load_top100` is O(1) in stored-winner count BY CONSTRUCTION: the
  artifact holds ≤ :data:`TOP100_LIMIT` entries, so the read is one bounded
  open+parse + one sha256 of the bounded bytes — no tree scan, ever
  (CI-bench-pinned flat).
* ANY stamp/schema/hash/shape damage ⇒ ``None`` ⇒ the caller rebuilds via
  the run-end pass; the artifact bytes are a pure function of the pool
  (``produced_at_utc`` lives ONLY in the stamp), so a rebuild from the same
  sources converges to the IDENTICAL bytes (golden-pinned).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import structlog

from brutex.data.atomic_io import atomic_write_simple
from brutex.models import BruteXError
from brutex.ranking import MIN_TRADES
from brutex.vocabulary.describe import matched_condition_names

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from brutex.engine.combo_aggregates import ComboAggregate
    from brutex.engine.results_store import ResultRow
    from brutex.engine.twin_detect import TwinFamilies

_LOG = structlog.get_logger("brutex.engine.top100_artifact")

__all__ = [
    "DEFAULT_TOP100_PRESET",
    "ENTRY_REQUIRED_KEYS",
    "GATE_MAX_CONSECUTIVE_LOSSES",
    "GATE_MAX_DRAWDOWN_PCT",
    "GATE_MIN_PROFIT_FACTOR",
    "GATE_MIN_TRADES",
    "GATE_MIN_WIN_LOSS_RATIO",
    "GATE_MIN_WIN_RATE",
    "RUNS_DIRNAME",
    "TOP100_FILENAME",
    "TOP100_LIMIT",
    "TOP100_PRESETS",
    "TOP100_SCHEMA_VERSION",
    "TOP100_STAGE",
    "TOP100_STAMP_FILENAME",
    "MarginRoiProvider",
    "PoolCounts",
    "ReasonProvider",
    "Top100Artifact",
    "Top100Candidate",
    "Top100Error",
    "build_top100",
    "load_top100",
    "passes_hard_gates",
    "rank_candidates",
    "resolve_preset",
    "write_top100",
]

#: Artifact schema version — bump on ANY payload-shape change (a reader seeing
#: an unknown version treats the artifact as absent → rebuild).
TOP100_SCHEMA_VERSION: Final[int] = 1

#: The P2→P3 stage-stamp ``stage`` token, reused as the P3→P4 stamp shape.
TOP100_STAGE: Final[str] = "top100"

#: The run-level artifact directory under the sweeps root (contract path).
RUNS_DIRNAME: Final[str] = "runs"

#: The artifact + stamp filenames (contract path law).
TOP100_FILENAME: Final[str] = "top100.json"
TOP100_STAMP_FILENAME: Final[str] = "top100.stamp.json"

#: The R21/R26 shipped-leaderboard depth: at most 100 entries; a smaller pool
#: ships its honest count, NEVER padding.
TOP100_LIMIT: Final[int] = 100

#: The R30 default preset + the one-click alternates, in display order.
PRESET_MIN_DD_FIRST: Final[str] = "min_dd_first"
DEFAULT_TOP100_PRESET: Final[str] = PRESET_MIN_DD_FIRST
TOP100_PRESETS: Final[tuple[str, ...]] = (
    PRESET_MIN_DD_FIRST,
    "composite",
    "profit_factor",
    "win_rate",
    "calmar",
)

# ── The 5 hard gates, recomputed from (row, agg) ────────────────────────────
#
# The EXACT ``brutex.ranking.passes_gates`` law (spec §gates, lines 589-600):
# the module-private constants there cannot be imported (house rule — no
# private imports), so the four non-public values are re-pinned HERE and a
# unit test cross-asserts each against ``brutex.ranking``'s private twin so
# the two homes can never silently drift (the ``_consecutive_runs``
# public-twin precedent in ``combo_aggregates``).

#: Gate 1 — fewer trades than this is statistical noise (PUBLIC upstream).
GATE_MIN_TRADES: Final[int] = MIN_TRADES

#: Gate 2 — profit factor below this has no edge.
GATE_MIN_PROFIT_FACTOR: Final[float] = 1.2

#: Gate 3 — max drawdown above this is uninvestable.
GATE_MAX_DRAWDOWN_PCT: Final[float] = 30.0

#: Gate 4 — win rate below this AND win/loss ratio below this together reject.
GATE_MIN_WIN_RATE: Final[float] = 0.25
GATE_MIN_WIN_LOSS_RATIO: Final[float] = 2.0

#: Gate 5 — a longer losing streak is an account-blower.
GATE_MAX_CONSECUTIVE_LOSSES: Final[int] = 10

#: Required keys of every artifact entry — the binding P3→P4 contract keys
#: verbatim (``rank, mask_hex, conditions, tf, direction, arm, metrics,
#: regime, vix_band, family_id``) plus the additive keys this module always
#: writes. Validated on load; a missing key ⇒ artifact treated as absent.
ENTRY_REQUIRED_KEYS: Final[frozenset[str]] = frozenset(
    {
        "rank",
        "run_id",
        "mask_hex",
        "conditions",
        "instrument",
        "tf",
        "direction",
        "arm",
        "metrics",
        "regime",
        "vix_band",
        "family_id",
        "family_members",
        "reasons",
        "margin_roi",
        "equity_ref",
    }
)

#: The u128 mask hex width (the ``mask:%032x`` run-identity convention).
_MASK_HEX_WIDTH: Final[int] = 32

#: sha256 hex digest length (stamp validation).
_SHA256_HEX_LEN: Final[int] = 64

#: The stamp's exact key set (the P2→P3 stage-stamp shape, reused P3→P4).
_STAMP_KEYS: Final[frozenset[str]] = frozenset(
    {"stage", "run_id", "content_hash", "produced_at_utc"}
)


class Top100Error(BruteXError):
    """Raised on a broken build invariant (duplicate run_id, bad inputs)."""


# ── Provider seams (WP3 integration points) ─────────────────────────────────

#: R33 dual-language reasons provider: ``mask_bits`` → ``{"technical": …,
#: "plain": …}`` or ``None`` (entry then carries an honest ``None`` — never
#: generated text). WP3's ``brutex.ranking.phrases.reason_for_mask`` plugs in
#: here at merge time.
type ReasonProvider = Callable[[int], Mapping[str, str] | None]

#: R47 margin-true-ROI provider: candidate → the ``margin_roi`` payload
#: (e.g. ``{"status": "unverified", "roi_pct": None, "detail": …}``) or
#: ``None``. WP3's ``brutex.engine.margin_history`` plugs in here.
type MarginRoiProvider = Callable[["Top100Candidate"], Mapping[str, object] | None]


@dataclass(frozen=True, slots=True)
class Top100Candidate:
    """One pooled winner offered to the global build.

    ``row`` + ``agg`` MUST describe the same finalist (same ``run_id``);
    ``sl_paisa`` / ``tp_paisa`` are the owning TUPLE's arm (the store row does
    not persist them — the pass threads them from the grid tuple);
    ``tuple_ref`` is the ``<instrument>/<tf>/<store-stem>`` sidecar address
    the entry's ``equity_ref`` points at (the R27 stored-real-series drill).
    """

    row: ResultRow
    agg: ComboAggregate
    sl_paisa: int | None
    tp_paisa: int | None
    tuple_ref: str

    def __post_init__(self) -> None:
        if self.row.run_id != self.agg.run_id:
            raise Top100Error(
                f"Top100Candidate: row run_id {self.row.run_id!r} != agg run_id "
                f"{self.agg.run_id!r} — a mislabelled join would rank one combo "
                "under another's numbers"
            )
        if not self.tuple_ref:
            raise Top100Error("Top100Candidate.tuple_ref must be non-empty")


@dataclass(frozen=True, slots=True)
class PoolCounts:
    """The pass-known pool honesty tallies for the artifact ``source`` header.

    ``stores`` = tuple stores contributing; ``excluded_no_aggs`` = stored rows
    without a current agg sidecar row (design D0b orphan law + fenced-failure
    tuples); ``zero_trade_excluded`` = Stage-3 ungated zero-trade rows;
    ``parity_excluded`` = rows whose oracle re-sim failed the parity tooth
    (their trade lists were withheld — they can never be co-ranked).
    """

    stores: int
    excluded_no_aggs: int
    zero_trade_excluded: int
    parity_excluded: int

    def __post_init__(self) -> None:
        for name in ("stores", "excluded_no_aggs", "zero_trade_excluded", "parity_excluded"):
            value = getattr(self, name)
            if value < 0:
                raise Top100Error(f"PoolCounts.{name} must be >= 0, got {value}")


@dataclass(frozen=True, slots=True)
class Top100Artifact:
    """The in-memory artifact — header fields typed, entries as payload maps.

    ``entries`` are the EXACT JSON payload maps (contract shape ⊇ the binding
    keys), in rank order; ``source`` is the honesty header; ``baselines`` maps
    instrument → its baseline artifact filename under the same ``runs/<run_id>/``
    directory (R27's comparison series).
    """

    schema_version: int
    run_id: str
    preset: str
    source: Mapping[str, object]
    baselines: Mapping[str, str]
    entries: tuple[Mapping[str, object], ...]

    def to_payload(self) -> dict[str, object]:
        """Render the artifact's deterministic JSON payload (B5 shape)."""
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "preset": self.preset,
            "source": dict(self.source),
            "baselines": dict(self.baselines),
            "entries": [dict(entry) for entry in self.entries],
        }

    def rendered_bytes(self) -> bytes:
        """The exact bytes :func:`write_top100` persists (pure function)."""
        return (_dumps(self.to_payload()) + "\n").encode("utf-8")


def _dumps(payload: object) -> str:
    """The ONE deterministic JSON rendering law (sorted, compact, ASCII)."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


# ── Gates + ranking ──────────────────────────────────────────────────────────


def passes_hard_gates(row: ResultRow, agg: ComboAggregate) -> bool:
    """Recompute the 5 hard rejection gates from the persisted row + its agg.

    The exact :func:`brutex.ranking.passes_gates` law over the two records
    that ARE persisted (``ResultRow`` lacks ``win_loss_ratio`` /
    ``max_consecutive_losses``; the agg carries them recomputed from the
    captured trades). ``ResultRow`` validates every float finite at
    construction and the agg ratios derive from exact integer folds, so the
    upstream non-finite fail-closed guard has no reachable input here. A
    ``None`` ``win_loss_ratio`` contributes ``0.0`` to the combined gate —
    the upstream ``passes_gates`` substitution, verbatim.
    """
    if row.total_trades < GATE_MIN_TRADES:
        return False
    if row.profit_factor < GATE_MIN_PROFIT_FACTOR:
        return False
    if row.max_drawdown_pct > GATE_MAX_DRAWDOWN_PCT:
        return False
    wlr = agg.win_loss_ratio if agg.win_loss_ratio is not None else 0.0
    if row.win_rate < GATE_MIN_WIN_RATE and wlr < GATE_MIN_WIN_LOSS_RATIO:
        return False
    return agg.max_consecutive_losses <= GATE_MAX_CONSECUTIVE_LOSSES


def resolve_preset(name: str) -> str:
    """Resolve a preset token; unknown/blank degrades to the default, logged.

    The /results ``_resolve_rank_by`` idiom — a hostile or stale token can
    never 500 a build; the degradation is visible (``top100.unknown_preset``).
    """
    if name in TOP100_PRESETS:
        return name
    _LOG.warning(
        "top100.unknown_preset",
        requested=name,
        resolved=DEFAULT_TOP100_PRESET,
        known=TOP100_PRESETS,
    )
    return DEFAULT_TOP100_PRESET


def _min_dd_key(candidate: Top100Candidate) -> tuple[float, int, float, int]:
    """The R30 ``min_dd_first`` sort key (ascending sort).

    Smallest stored max drawdown % first; then composite score DESC (a
    ``None`` composite ranks AFTER every real one — an unscored combo never
    outranks a scored one); then fewest consecutive losses. Ties fall through
    to the stable ``run_id``-ASC pre-sort.
    """
    composite = candidate.agg.composite_score
    return (
        candidate.row.max_drawdown_pct,
        0 if composite is not None else 1,
        -(composite if composite is not None else 0.0),
        candidate.agg.max_consecutive_losses,
    )


def _desc_value(candidate: Top100Candidate, preset: str) -> tuple[int, float]:
    """The descending-preset key: ``(value_present, raw_value)``.

    The row metrics are never ``None`` (persisted floats); only ``composite``
    can be — a missing composite sorts last under ``reverse=True`` via the
    presence flag, never coerced to a fake number.
    """
    if preset == "composite":
        composite = candidate.agg.composite_score
        if composite is None:
            return (0, 0.0)
        return (1, composite)
    if preset == "profit_factor":
        return (1, candidate.row.profit_factor)
    if preset == "win_rate":
        return (1, candidate.row.win_rate)
    # ``resolve_preset`` admits exactly the TOP100_PRESETS tokens; min_dd_first
    # never reaches here (rank_candidates branches first), so this is calmar.
    return (1, candidate.row.calmar_ratio)


def rank_candidates(
    candidates: Sequence[Top100Candidate], *, preset: str
) -> tuple[Top100Candidate, ...]:
    """Order *candidates* under *preset* — a strict, discovery-order-free total order.

    The ``ResultsStore.top_k`` two-pass law: a stable ``run_id``-ASC pre-sort,
    then ONE stable sort on the preset key — identical output for any input
    permutation (property-pinned).
    """
    resolved = resolve_preset(preset)
    ordered = sorted(candidates, key=lambda c: c.row.run_id)
    if resolved == PRESET_MIN_DD_FIRST:
        ordered.sort(key=_min_dd_key)
    else:
        ordered.sort(key=lambda c: _desc_value(c, resolved), reverse=True)
    return tuple(ordered)


# ── Entry payload projection ─────────────────────────────────────────────────


def _loads_object(rendered: str) -> dict[str, object]:
    """Parse one of the agg's deterministic JSON columns back to a dict."""
    payload = json.loads(rendered)
    if not isinstance(payload, dict):  # defensive: the agg writer only emits objects
        raise Top100Error(f"agg JSON column did not decode to an object: {rendered!r}")
    return payload


def _metrics_payload(row: ResultRow, agg: ComboAggregate) -> dict[str, object]:
    """Every ResultRow metric + the agg's depth stats — full precision, ints exact."""
    return {
        # ── the persisted ResultRow, field-for-field (R27 traceability) ──
        "net_pnl_paisa": row.net_pnl_paisa,
        "gross_profit_paisa": row.gross_profit_paisa,
        "gross_loss_paisa": row.gross_loss_paisa,
        "max_drawdown_paisa": row.max_drawdown_paisa,
        "expectancy_paisa": row.expectancy_paisa,
        "total_trades": row.total_trades,
        "winning_trades": row.winning_trades,
        "losing_trades": row.losing_trades,
        "sharpe_ratio": row.sharpe_ratio,
        "sortino_ratio": row.sortino_ratio,
        "calmar_ratio": row.calmar_ratio,
        "profit_factor": row.profit_factor,
        "win_rate": row.win_rate,
        "max_drawdown_pct": row.max_drawdown_pct,
        "deflated_sharpe": row.deflated_sharpe,
        "avg_entry_price": str(row.avg_entry_price),
        "avg_exit_price": str(row.avg_exit_price),
        # ── the recomputed depth aggregates (R22/R30) ──
        "composite_score": agg.composite_score,
        "win_loss_ratio": agg.win_loss_ratio,
        "recovery_factor": agg.recovery_factor,
        "max_consecutive_losses": agg.max_consecutive_losses,
        "max_consecutive_wins": agg.max_consecutive_wins,
        "mae_avg_paisa": agg.mae_avg_paisa,
        "mae_worst_paisa": agg.mae_worst_paisa,
        "mae_p95_paisa": agg.mae_p95_paisa,
        "mfe_avg_paisa": agg.mfe_avg_paisa,
        "mfe_best_paisa": agg.mfe_best_paisa,
        "mfe_p95_paisa": agg.mfe_p95_paisa,
        "mae_1m_avg_paisa": agg.mae_1m_avg_paisa,
        "mae_1m_worst_paisa": agg.mae_1m_worst_paisa,
        "mae_1m_p95_paisa": agg.mae_1m_p95_paisa,
        "mfe_1m_avg_paisa": agg.mfe_1m_avg_paisa,
        "mfe_1m_best_paisa": agg.mfe_1m_best_paisa,
        "mfe_1m_p95_paisa": agg.mfe_1m_p95_paisa,
        "trades_with_1m": agg.trades_with_1m,
        "max_dd_minute_path_paisa": agg.max_dd_minute_path_paisa,
        "max_dd_minute_path_pct": agg.max_dd_minute_path_pct,
        "equity_status": agg.equity_status,
        "worst5_days": json.loads(agg.worst5_days_json),
        "exits": {
            "stop_loss": agg.exits_stop_loss,
            "take_profit": agg.exits_take_profit,
            "signal": agg.exits_signal,
            "forced_1520": agg.exits_forced_1520,
            "end_of_data": agg.exits_end_of_data,
        },
        "fingerprint": agg.fingerprint,
    }


def _regime_payload(agg: ComboAggregate) -> dict[str, object]:
    """The R46/R51 regime block, parsed from the agg's deterministic JSON."""
    return {
        "by_year": _loads_object(agg.pnl_by_year_json),
        "by_trend": _loads_object(agg.pnl_by_trend_json),
        "one_regime_wonder": agg.one_regime_wonder,
        "regime_wonder_axis": agg.regime_wonder_axis,
        "entry_hour_hist": _loads_object(agg.entry_hour_hist_json),
        "event_days": {
            "counts": _loads_object(agg.event_day_counts_json),
            "pnl": _loads_object(agg.event_day_pnl_json),
        },
    }


def _vix_band_payload(agg: ComboAggregate) -> dict[str, object]:
    """The R31 VIX block: band + win/loss stats, honest ``None`` when unstamped."""
    win: dict[str, float] | None = None
    if agg.vix_win_min is not None and agg.vix_win_avg is not None and agg.vix_win_max is not None:
        win = {"min": agg.vix_win_min, "avg": agg.vix_win_avg, "max": agg.vix_win_max}
    loss: dict[str, float] | None = None
    if (
        agg.vix_loss_min is not None
        and agg.vix_loss_avg is not None
        and agg.vix_loss_max is not None
    ):
        loss = {"min": agg.vix_loss_min, "avg": agg.vix_loss_avg, "max": agg.vix_loss_max}
    return {
        "lo": agg.vix_band_lo,
        "hi": agg.vix_band_hi,
        "stamped": agg.vix_stamped_trades,
        "missing": agg.vix_missing_trades,
        "win": win,
        "loss": loss,
    }


def _entry_payload(
    rank: int,
    candidate: Top100Candidate,
    *,
    family_id: str,
    family_members: Sequence[str],
    reasons: Mapping[str, str] | None,
    margin_roi: Mapping[str, object] | None,
) -> dict[str, object]:
    """Project one ranked candidate onto the binding entry shape (B5)."""
    row = candidate.row
    return {
        "rank": rank,
        "run_id": row.run_id,
        "mask_hex": f"{row.mask_bits:0{_MASK_HEX_WIDTH}x}",
        "conditions": list(matched_condition_names(row.mask_bits)),
        "instrument": row.instrument,
        "tf": row.tf,
        "direction": row.direction,
        "arm": {"sl_paisa": candidate.sl_paisa, "tp_paisa": candidate.tp_paisa},
        "metrics": _metrics_payload(row, candidate.agg),
        "regime": _regime_payload(candidate.agg),
        "vix_band": _vix_band_payload(candidate.agg),
        "family_id": family_id,
        "family_members": list(family_members),
        "reasons": None if reasons is None else dict(reasons),
        "margin_roi": None if margin_roi is None else dict(margin_roi),
        "equity_ref": {"tuple": candidate.tuple_ref, "sidecar": "equity"},
    }


# ── Builder ──────────────────────────────────────────────────────────────────


def build_top100(
    candidates: Sequence[Top100Candidate],
    *,
    run_id: str,
    preset: str,
    families: TwinFamilies,
    counts: PoolCounts,
    data_snapshot_hashes: Sequence[str],
    baselines: Mapping[str, str],
    reason_provider: ReasonProvider | None = None,
    margin_roi_provider: MarginRoiProvider | None = None,
) -> Top100Artifact:
    """Build the global artifact from the pooled winners — one O(pool) pass.

    Parameters
    ----------
    candidates
        The pool the pass offers (already excludes no-agg / zero-trade /
        parity-failed rows — those live in *counts*). Gate recomputation +
        family collapse happen HERE so the artifact's honesty header is a
        pure function of its own inputs.
    run_id
        The GRID identity (``grid_identity(grid)``).
    preset
        The ranking preset token (unknown degrades to the default, logged).
    families
        The R34 twin partition over the gate-passing pool (from
        ``twin_detect.group_twins``). A candidate absent from the partition
        is its own family (lenient — an empty ``TwinFamilies`` means no
        collapsing).
    counts
        The pass-known exclusion tallies (see :class:`PoolCounts`).
    data_snapshot_hashes
        The grid's data identity hash(es) — the artifact's provenance.
    baselines
        Instrument → baseline artifact filename (under ``runs/<run_id>/``).
    reason_provider, margin_roi_provider
        The WP3 seams (module docstring) — ``None`` writes honest ``None``
        entries + ``*_wired=False`` header flags.

    Raises
    ------
    Top100Error
        On a duplicate candidate ``run_id`` (one identity → one result).
    """
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.row.run_id in seen:
            raise Top100Error(f"build_top100: duplicate candidate run_id {candidate.row.run_id!r}")
        seen.add(candidate.row.run_id)
    resolved = resolve_preset(preset)

    passers = [c for c in candidates if passes_hard_gates(c.row, c.agg)]

    # Family collapse (R34): one representative per family among the PASSERS.
    # The partition's representative (fewest mask bits, run_id ASC) is used
    # when it passed the gates itself; otherwise the same law is re-applied
    # over the family's gate-passing members (a family whose nominal
    # representative was gated out still ships its simplest passing member).
    by_family: dict[str, list[Top100Candidate]] = {}
    for candidate in passers:
        family_id = families.family_by_run_id.get(candidate.row.run_id, candidate.row.run_id)
        by_family.setdefault(family_id, []).append(candidate)
    representatives: list[Top100Candidate] = []
    members_of: dict[str, tuple[str, ...]] = {}
    for family_id, members in by_family.items():
        chosen = min(
            members,
            key=lambda c: (
                0 if c.row.run_id == family_id else 1,
                c.row.mask_bits.bit_count(),
                c.row.run_id,
            ),
        )
        listed = families.members_by_family.get(
            family_id, tuple(sorted(c.row.run_id for c in members))
        )
        representatives.append(chosen)
        members_of[chosen.row.run_id] = listed
    families_collapsed = len(passers) - len(representatives)

    ranked = rank_candidates(representatives, preset=resolved)[:TOP100_LIMIT]
    entries: list[Mapping[str, object]] = []
    for position, candidate in enumerate(ranked, start=1):
        run = candidate.row.run_id
        entries.append(
            _entry_payload(
                position,
                candidate,
                family_id=families.family_by_run_id.get(run, run),
                family_members=members_of[run],
                reasons=(
                    None if reason_provider is None else reason_provider(candidate.row.mask_bits)
                ),
                margin_roi=(
                    None if margin_roi_provider is None else margin_roi_provider(candidate)
                ),
            )
        )

    source: dict[str, object] = {
        "data_snapshot_hashes": sorted(set(data_snapshot_hashes)),
        "stores": counts.stores,
        "pool": len(candidates),
        "gate_passers": len(passers),
        "excluded_no_aggs": counts.excluded_no_aggs,
        "zero_trade_excluded": counts.zero_trade_excluded,
        "parity_excluded": counts.parity_excluded,
        "families_collapsed": families_collapsed,
        "reasons_wired": reason_provider is not None,
        "margin_roi_wired": margin_roi_provider is not None,
    }
    artifact = Top100Artifact(
        schema_version=TOP100_SCHEMA_VERSION,
        run_id=run_id,
        preset=resolved,
        source=source,
        baselines=dict(baselines),
        entries=tuple(entries),
    )
    _LOG.info(
        "top100.built",
        run_id=run_id,
        preset=resolved,
        pool=len(candidates),
        gate_passers=len(passers),
        families_collapsed=families_collapsed,
        entries=len(entries),
    )
    return artifact


# ── Writer + validating reader ───────────────────────────────────────────────


def write_top100(runs_dir: Path, artifact: Top100Artifact) -> Path:
    """Atomically write ``<runs_dir>/<run_id>/top100.json`` + its stage stamp.

    The artifact bytes are deterministic (:meth:`Top100Artifact.rendered_bytes`
    — a rebuild from the same sources converges to identical bytes); ONLY the
    stamp carries ``produced_at_utc``. The stamp is written LAST so a crash
    between the two files leaves a stamp-less artifact every reader treats as
    absent (R42's mid-chain break law). Raw ``OSError`` propagates unchanged
    (the engine sidecar error contract).
    """
    target_dir = runs_dir / artifact.run_id
    target_dir.mkdir(parents=True, exist_ok=True)
    rendered = artifact.rendered_bytes()
    artifact_path = target_dir / TOP100_FILENAME
    atomic_write_simple(artifact_path, lambda tmp: tmp.write_bytes(rendered))
    stamp_payload = {
        "stage": TOP100_STAGE,
        "run_id": artifact.run_id,
        "content_hash": hashlib.sha256(rendered).hexdigest(),
        "produced_at_utc": datetime.now(tz=UTC).isoformat(),
    }
    stamp_rendered = _dumps(stamp_payload) + "\n"
    atomic_write_simple(
        target_dir / TOP100_STAMP_FILENAME,
        lambda tmp: tmp.write_text(stamp_rendered, encoding="utf-8"),
    )
    _LOG.info(
        "top100.written",
        path=str(artifact_path),
        entries=len(artifact.entries),
        bytes=len(rendered),
    )
    return artifact_path


def load_top100(runs_dir: Path, run_id: str) -> Top100Artifact | None:
    """Read + validate the artifact; ANY damage returns ``None`` (rebuild).

    O(1) in stored-winner count by construction (module docstring): one
    bounded stamp read, one bounded artifact read, one sha256 over ≤ 100
    entries' bytes — never a tree scan. Validation (the stage-stamp law:
    consumers VALIDATE the hash BEFORE trusting the payload):

    1. the stamp reads, parses, carries exactly the stage-stamp keys with
       ``stage == "top100"`` and ``run_id`` matching;
    2. the artifact's CURRENT bytes sha256 the stamped ``content_hash``
       (a torn/stale/tampered artifact fails here);
    3. the payload is a well-formed B5 object — matching ``schema_version``
       + ``run_id``, string ``preset``, object ``source``, string→string
       ``baselines``, and an entries LIST whose every item carries
       :data:`ENTRY_REQUIRED_KEYS` with ``rank`` equal to its 1-based
       position (ordered list order == rank order, the contract).

    Each rejection logs ONE ``top100.load_invalid`` naming the reason.
    """

    def _invalid(reason: str) -> None:
        _LOG.warning("top100.load_invalid", runs_dir=str(runs_dir), run_id=run_id, reason=reason)

    target_dir = runs_dir / run_id
    try:
        stamp_raw = (target_dir / TOP100_STAMP_FILENAME).read_bytes()
    except OSError:
        _invalid("stamp_unreadable")
        return None
    try:
        stamp = json.loads(stamp_raw)
    except ValueError:
        _invalid("stamp_not_json")
        return None
    if not isinstance(stamp, dict) or set(stamp.keys()) != _STAMP_KEYS:
        _invalid("stamp_bad_shape")
        return None
    if stamp["stage"] != TOP100_STAGE:
        _invalid("stamp_wrong_stage")
        return None
    if stamp["run_id"] != run_id:
        _invalid("stamp_wrong_run_id")
        return None
    content_hash = stamp["content_hash"]
    if not isinstance(content_hash, str) or len(content_hash) != _SHA256_HEX_LEN:
        _invalid("stamp_bad_content_hash")
        return None
    if not isinstance(stamp["produced_at_utc"], str):
        _invalid("stamp_bad_produced_at")
        return None

    try:
        raw = (target_dir / TOP100_FILENAME).read_bytes()
    except OSError:
        _invalid("artifact_unreadable")
        return None
    if hashlib.sha256(raw).hexdigest() != content_hash:
        _invalid("artifact_hash_mismatch")
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        _invalid("artifact_not_json")
        return None
    if not isinstance(payload, dict):
        _invalid("artifact_not_object")
        return None
    if payload.get("schema_version") != TOP100_SCHEMA_VERSION:
        _invalid("schema_version_mismatch")
        return None
    if payload.get("run_id") != run_id:
        _invalid("artifact_wrong_run_id")
        return None
    preset = payload.get("preset")
    if not isinstance(preset, str) or not preset:
        _invalid("bad_preset")
        return None
    source = payload.get("source")
    if not isinstance(source, dict):
        _invalid("bad_source")
        return None
    baselines = payload.get("baselines")
    if not isinstance(baselines, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in baselines.items()
    ):
        _invalid("bad_baselines")
        return None
    entries_raw = payload.get("entries")
    if not isinstance(entries_raw, list):
        _invalid("bad_entries")
        return None
    entries: list[Mapping[str, object]] = []
    for position, item in enumerate(entries_raw, start=1):
        if not isinstance(item, dict) or not ENTRY_REQUIRED_KEYS.issubset(item.keys()):
            _invalid("bad_entry_shape")
            return None
        if item["rank"] != position:
            _invalid("bad_entry_rank_order")
            return None
        entries.append(item)
    return Top100Artifact(
        schema_version=TOP100_SCHEMA_VERSION,
        run_id=run_id,
        preset=preset,
        source=source,
        baselines={str(key): str(value) for key, value in baselines.items()},
        entries=tuple(entries),
    )
