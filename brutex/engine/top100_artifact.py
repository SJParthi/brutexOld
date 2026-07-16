"""The global cross-bucket top-100 artifact — builder, writer, validating reader.

DEC-SWEEP-RESULTS-DEPTH-001 (WP5). R21/R26: SHIPPED = the ONE global
"best of the best" leaderboard across every bucket of a grid, precomputed at
run-end as ONE small artifact at the binding P3→P4 contract location
(gap-register Ruling 4, 2026-07-08 — run artifacts live under the
``sweep_runs/`` SIBLING root beside the sweeps tree, never inside the
``**/*.parquet``-globbed tree; the earlier ``<sweeps>/runs/<run_id>/``
contract paths are SUPERSEDED)::

    <sweep_runs root>/<run_id>/top100.json        # ordered entries (≤ 100)
    <sweep_runs root>/<run_id>/top100.stamp.json  # {stage, run_id, content_hash, produced_at_utc}

The ``<sweep_runs root>`` is built EXCLUSIVELY through the ONE
:func:`brutex.engine.trade_store.sweep_run_dir` path-builder seam (the leaf
P2's future ``run_layout.py`` rebinds at merge-integration) — this module
takes the already-built runs root and never re-derives the layout. ``run_id``
here is TODAY the ENGINE grid identity
(:func:`brutex.engine.sweep_supervisor.grid_identity`); Ruling 9's logical
run_id (one logical id wrapping N engine runs) rebinds at that same seam.
Each ENTRY's ``run_id`` stays the per-mask 64-hex store row key.

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

WP3 WIRING NOTE: the preset table + the two-pass ordering law live in
``brutex.ranking.presets`` (the ONE home — this module DELEGATES via
``sort_by_preset``, byte-identical order to the pre-WP3 inline law); the
dual-language reasons / margin-ROI live in ``brutex.ranking.phrases`` /
``brutex.engine.margin_history`` and plug into the ``reason_provider`` /
``margin_roi_provider`` injection seams (wired by the WP6 CLI adapter);
absent providers yield an honest ``None`` per entry plus
``reasons_wired=False`` / ``margin_roi_wired=False`` in the ``source``
header — never invented text (R27/R33/R47).

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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, TypeAlias

import structlog

from brutex.data.atomic_io import atomic_write_simple
from brutex.data.guarded_read import read_regular_bytes_deadline
from brutex.models import BruteXError
from brutex.ranking import MIN_TRADES
from brutex.ranking.presets import (
    DEFAULT_PRESET,
    PRESET_NAMES,
    PRESETS,
    RankRecord,
    sort_by_preset,
)
from brutex.vocabulary.describe import matched_condition_names

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Sequence
    from pathlib import Path

    from brutex.engine.cascade_join import CascadeJoin, CascadeVerdictView
    from brutex.engine.combo_aggregates import ComboAggregate
    from brutex.engine.results_store import ResultRow
    from brutex.engine.twin_detect import TwinFamilies

_LOG = structlog.get_logger("brutex.engine.top100_artifact")

__all__ = [
    "CASCADE_STATE_JOINED",
    "CASCADE_STATE_NO_REPORT",
    "CASCADE_STATE_SWEEP_ID_MISMATCH",
    "DEFAULT_TOP100_PRESET",
    "ENTRY_REQUIRED_KEYS",
    "GATE_MAX_CONSECUTIVE_LOSSES",
    "GATE_MAX_DRAWDOWN_PCT",
    "GATE_MIN_PROFIT_FACTOR",
    "GATE_MIN_TRADES",
    "GATE_MIN_WIN_LOSS_RATIO",
    "GATE_MIN_WIN_RATE",
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

#: The artifact + stamp filenames (contract path law; the run directory
#: itself comes from ``trade_store.sweep_run_dir`` — Ruling 4).
TOP100_FILENAME: Final[str] = "top100.json"
TOP100_STAMP_FILENAME: Final[str] = "top100.stamp.json"

#: The R21/R26 shipped-leaderboard depth: at most 100 entries; a smaller pool
#: ships its honest count, NEVER padding.
TOP100_LIMIT: Final[int] = 100

#: The R30 default preset + the one-click alternates, in display order —
#: re-exported VERBATIM from the ONE preset home (``brutex.ranking.presets``,
#: WP3) so the artifact vocabulary can never fork from the ranking law's.
#: (The ``min_dd_first`` token itself lives ONLY at that home —
#: ``brutex.ranking.presets.PRESET_MIN_DD_FIRST`` — no duplicate constant
#: here per the COMMON rule; it arrives via ``DEFAULT_PRESET``.)
DEFAULT_TOP100_PRESET: Final[str] = DEFAULT_PRESET
TOP100_PRESETS: Final[tuple[str, ...]] = PRESET_NAMES

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

# ── Cascade-join state tokens (DEC-RESULTS-CASCADE-VERDICT-JOIN-001) ────────
#
# The root ``cascade_join.state`` vocabulary. ADDITIVE display metadata only:
# ``ENTRY_REQUIRED_KEYS`` is deliberately NOT extended (``load_top100``
# treats a missing required key as artifact-ABSENT, so extending it would
# flip every pre-change artifact + the fixture corpus to absent) — the
# per-entry ``cascade`` object and the root ``cascade_join`` meta are
# writer-always-emitted, reader-OPTIONAL keys.

#: The threaded join matched this grid (``join.sweep_id == run_id``).
CASCADE_STATE_JOINED: Final[str] = "joined"

#: No cascade report was threaded (cascade skipped/disabled — every entry
#: reads the honest ``not_validated``/``no_report``).
CASCADE_STATE_NO_REPORT: Final[str] = "no_report"

#: A join WAS threaded but for a DIFFERENT grid (the cascade dir is
#: one-per-root and overwritten per sweep — a stale report must never stamp
#: this artifact's entries): dropped + WARNed, entries stay not_validated.
CASCADE_STATE_SWEEP_ID_MISMATCH: Final[str] = "sweep_id_mismatch"


class Top100Error(BruteXError):
    """Raised on a broken build invariant (duplicate run_id, bad inputs)."""


# ── Provider seams (WP3 integration points) ─────────────────────────────────

#: R33 dual-language reasons provider: ``mask_bits`` → ``{"technical": …,
#: "plain": …}`` or ``None`` (entry then carries an honest ``None`` — never
#: generated text). WP3's ``brutex.ranking.phrases.reason_for_mask`` plugs in
#: here at merge time. Classic ``TypeAlias`` (not a PEP 695 ``type``
#: statement) because both provider aliases are ``__all__``-exported public
#: API — a plain runtime binding keeps the export introspectable (the
#: ``ingest_common.SpotStepGroup`` precedent).
ReasonProvider: TypeAlias = Callable[[int], Mapping[str, str] | None]  # noqa: UP040

#: R47 margin-true-ROI provider: candidate → the ``margin_roi`` payload
#: (e.g. ``{"status": "unverified", "roi_pct": None, "detail": …}``) or
#: ``None``. WP3's ``brutex.engine.margin_history`` plugs in here.
MarginRoiProvider: TypeAlias = Callable[  # noqa: UP040
    ["Top100Candidate"], Mapping[str, object] | None
]


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
    instrument → its baseline artifact filename under the same
    ``sweep_runs/<run_id>/`` directory (R27's comparison series).
    ``cascade_join`` is the ADDITIVE root cascade-join meta
    (DEC-RESULTS-CASCADE-VERDICT-JOIN-001 — ``{state, sweep_id, n_input,
    n_survived}``); ``None`` on a pre-change artifact read back by
    :func:`load_top100` (the key is then simply not re-emitted).
    """

    schema_version: int
    run_id: str
    preset: str
    source: Mapping[str, object]
    baselines: Mapping[str, str]
    entries: tuple[Mapping[str, object], ...]
    cascade_join: Mapping[str, object] | None = None

    def to_payload(self) -> dict[str, object]:
        """Render the artifact's deterministic JSON payload (B5 shape)."""
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "preset": self.preset,
            "source": dict(self.source),
            "baselines": dict(self.baselines),
            "entries": [dict(entry) for entry in self.entries],
        }
        if self.cascade_join is not None:
            payload["cascade_join"] = dict(self.cascade_join)
        return payload

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


def _rank_record(candidate: Top100Candidate) -> RankRecord:
    """Project one pooled candidate onto the COMMON flat rank-record shape."""
    return RankRecord(
        run_id=candidate.row.run_id,
        max_drawdown_pct=candidate.row.max_drawdown_pct,
        composite_score=candidate.agg.composite_score,
        max_consecutive_losses=candidate.agg.max_consecutive_losses,
        profit_factor=candidate.row.profit_factor,
        win_rate=candidate.row.win_rate,
        calmar_ratio=candidate.row.calmar_ratio,
    )


def rank_candidates(
    candidates: Sequence[Top100Candidate], *, preset: str
) -> tuple[Top100Candidate, ...]:
    """Order *candidates* under *preset* — a strict, discovery-order-free total order.

    Delegates to the ONE two-pass ordering law
    (:func:`brutex.ranking.presets.sort_by_preset` — the ``ResultsStore.top_k``
    law: stable ``run_id``-ASC pre-sort, then ONE stable sort on the preset
    key) — identical output for any input permutation (property-pinned).
    """
    resolved = resolve_preset(preset)
    return sort_by_preset(candidates, preset=PRESETS[resolved], record_of=_rank_record)


# ── Entry payload projection ─────────────────────────────────────────────────


def _loads_object(rendered: str) -> dict[str, object]:
    """Parse one of the agg's deterministic JSON columns back to a dict."""
    payload = json.loads(rendered)
    if not isinstance(payload, dict):  # defensive: the agg writer only emits objects
        raise Top100Error(f"agg JSON column did not decode to an object: {rendered!r}")
    return payload


def _metrics_payload(
    row: ResultRow, agg: ComboAggregate, *, cpcv_pass_rate: float | None = None
) -> dict[str, object]:
    """Every ResultRow metric + the agg's depth stats — full precision, ints exact.

    ``cpcv_pass_rate`` is the joined cascade verdict's RAW stage-3 fraction
    (DEC-RESULTS-CASCADE-VERDICT-JOIN-001): when non-``None`` the payload
    gains the display alias ``cpcv_pass_pct = rate * 100.0`` — an EXACT float
    unit projection, never a rounding (CLAUDE.md §4). Emitted ONLY when the
    rate is known: an absent key renders the honest "—" on every display
    site, NEVER a fabricated 0 (the oneclick_artifacts absent-key law).
    """
    payload: dict[str, object] = {
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
            "session_gap": agg.exits_session_gap,
        },
        "fingerprint": agg.fingerprint,
    }
    if cpcv_pass_rate is not None:
        payload["cpcv_pass_pct"] = cpcv_pass_rate * 100.0
    return payload


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


def _cascade_payload(
    run_id: str, *, join: CascadeJoin | None, state: str
) -> tuple[dict[str, object], float | None]:
    """One entry's ``cascade`` object + its joined CPCV rate (or ``None``).

    The DEC-RESULTS-CASCADE-VERDICT-JOIN-001 law table: a non-``joined``
    *state* / a duplicate candidate_id / an id absent from the report pool
    each read the honest ``not_validated`` with a NAMED reason; a matched
    verdict projects verbatim (``pass`` / ``provisional_pass`` / ``fail``) —
    the annotation NEVER reranks or hides an entry (R13: a cascade-FAILED
    rank-1 stays rank-1 and is flagged by the display layer).
    """
    if state != CASCADE_STATE_JOINED or join is None:
        return {"status": "not_validated", "reason": state}, None
    if run_id in join.ambiguous_ids:
        return {"status": "not_validated", "reason": "duplicate_candidate_id"}, None
    verdict: CascadeVerdictView | None = join.by_candidate.get(run_id)
    if verdict is None:
        return {"status": "not_validated", "reason": "not_in_cascade_pool"}, None
    if verdict.passed:
        status = "provisional_pass" if verdict.provisional else "pass"
        return {
            "status": status,
            "rejected_at": None,
            "detail": verdict.detail,
            "cascade_composite": verdict.cascade_composite,
            "provisional": verdict.provisional,
            "cpcv_pass_rate": verdict.cpcv_pass_rate,
            "survivor_rank": verdict.survivor_rank,
        }, verdict.cpcv_pass_rate
    return {
        "status": "fail",
        "rejected_at": verdict.rejected_at,
        "detail": verdict.detail,
        "cascade_composite": None,
        "provisional": False,
        "cpcv_pass_rate": verdict.cpcv_pass_rate,
        "survivor_rank": None,
    }, verdict.cpcv_pass_rate


def _entry_payload(
    rank: int,
    candidate: Top100Candidate,
    *,
    family_id: str,
    family_members: Sequence[str],
    reasons: Mapping[str, str] | None,
    margin_roi: Mapping[str, object] | None,
    join: CascadeJoin | None = None,
    join_state: str = CASCADE_STATE_NO_REPORT,
) -> dict[str, object]:
    """Project one ranked candidate onto the binding entry shape (B5)."""
    row = candidate.row
    cascade, cpcv_rate = _cascade_payload(row.run_id, join=join, state=join_state)
    return {
        "rank": rank,
        "run_id": row.run_id,
        "mask_hex": f"{row.mask_bits:0{_MASK_HEX_WIDTH}x}",
        "conditions": list(matched_condition_names(row.mask_bits)),
        "instrument": row.instrument,
        "tf": row.tf,
        "direction": row.direction,
        "arm": {"sl_paisa": candidate.sl_paisa, "tp_paisa": candidate.tp_paisa},
        "metrics": _metrics_payload(row, candidate.agg, cpcv_pass_rate=cpcv_rate),
        "regime": _regime_payload(candidate.agg),
        "vix_band": _vix_band_payload(candidate.agg),
        "family_id": family_id,
        "family_members": list(family_members),
        "reasons": None if reasons is None else dict(reasons),
        "margin_roi": None if margin_roi is None else dict(margin_roi),
        "equity_ref": {"tuple": candidate.tuple_ref, "sidecar": "equity"},
        "cascade": cascade,
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
    cascade_join: CascadeJoin | None = None,
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
        Instrument → baseline artifact filename (under ``sweep_runs/<run_id>/``).
    reason_provider, margin_roi_provider
        The WP3 seams (module docstring) — ``None`` writes honest ``None``
        entries + ``*_wired=False`` header flags.
    cascade_join
        The flattened Stage-4 cascade join
        (DEC-RESULTS-CASCADE-VERDICT-JOIN-001 —
        :func:`brutex.engine.cascade_join.build_cascade_join` over the SAME
        in-memory report ``cmd_sweep_all`` produced), or ``None`` when the
        cascade was skipped/disabled. HARD sweep_id gate at THIS writer (the
        one place a wrong-grid join could silently stamp a whole artifact):
        ``join.sweep_id != run_id`` drops the join — root
        ``cascade_join.state = "sweep_id_mismatch"`` + ONE
        ``top100.cascade_sweep_id_mismatch`` WARN naming both ids, entries
        stay ``not_validated``. The join is ANNOTATION-ONLY: ranking /
        preset order / family collapse are byte-untouched (R13 — a
        cascade-FAILED rank-1 stays rank-1, flagged never hidden).

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

    # ── Cascade sweep_id gate (DEC-RESULTS-CASCADE-VERDICT-JOIN-001) ──
    # The cascade dir is ONE-per-results-root and overwritten per sweep, so a
    # threaded join must prove it was judged for THIS grid before a single
    # entry is stamped: a mismatched sweep_id drops the whole join loudly.
    if cascade_join is None:
        join_state = CASCADE_STATE_NO_REPORT
        join_for_entries: CascadeJoin | None = None
    elif cascade_join.sweep_id != run_id:
        _LOG.warning(
            "top100.cascade_sweep_id_mismatch",
            run_id=run_id,
            report_sweep_id=cascade_join.sweep_id,
            note="stale cascade report — entries stay not_validated",
        )
        join_state = CASCADE_STATE_SWEEP_ID_MISMATCH
        join_for_entries = None
    else:
        join_state = CASCADE_STATE_JOINED
        join_for_entries = cascade_join

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
                join=join_for_entries,
                join_state=join_state,
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
    # Root cascade-join meta — writer-ALWAYS-emitted, reader-OPTIONAL. Under
    # a mismatch the report's sweep_id is NAMED (the diagnosable half) while
    # the counts stay honest null (a stale report's totals must never read
    # as this grid's); under no_report everything is null.
    if join_state == CASCADE_STATE_JOINED and join_for_entries is not None:
        cascade_meta: dict[str, object] = {
            "state": join_state,
            "sweep_id": join_for_entries.sweep_id,
            "n_input": join_for_entries.n_input,
            "n_survived": join_for_entries.n_survived,
        }
    else:
        cascade_meta = {
            "state": join_state,
            "sweep_id": None if cascade_join is None else cascade_join.sweep_id,
            "n_input": None,
            "n_survived": None,
        }
    artifact = Top100Artifact(
        schema_version=TOP100_SCHEMA_VERSION,
        run_id=run_id,
        preset=resolved,
        source=source,
        baselines=dict(baselines),
        entries=tuple(entries),
        cascade_join=cascade_meta,
    )
    _LOG.info(
        "top100.built",
        run_id=run_id,
        preset=resolved,
        pool=len(candidates),
        gate_passers=len(passers),
        families_collapsed=families_collapsed,
        entries=len(entries),
        cascade_join_state=join_state,
    )
    return artifact


# ── Writer + validating reader ───────────────────────────────────────────────


def write_top100(runs_dir: Path, artifact: Top100Artifact) -> Path:
    """Atomically write ``<runs_dir>/<run_id>/top100.json`` + its stage stamp.

    *runs_dir* is the ``sweep_runs/`` SIBLING root from
    :func:`brutex.engine.trade_store.sweep_run_dir`'s parent (Ruling 4) —
    callers thread it from that ONE seam, never a hand-built path.
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
        # DEC-LAKE-READ-INODE-GUARD-001 tier 1 (BOTH defenses — this loader
        # feeds the oneclick top100 route's fresh parse): a planted FIFO /
        # wedged volume folds into the existing NAMED-invalid arms (both
        # guard errors are OSError subclasses), never a hung poll.
        stamp_raw = read_regular_bytes_deadline(
            target_dir / TOP100_STAMP_FILENAME, what="top100 stamp read"
        )
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
        raw = read_regular_bytes_deadline(target_dir / TOP100_FILENAME, what="top100 artifact read")
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
    # ADDITIVE root meta (DEC-RESULTS-CASCADE-VERDICT-JOIN-001): presence-
    # tolerant — a pre-change artifact simply lacks the key (None, not
    # re-emitted on a round-trip); a non-object value is dropped, never a
    # reject (the entries + stamp already proved integrity above).
    cascade_join_raw = payload.get("cascade_join")
    return Top100Artifact(
        schema_version=TOP100_SCHEMA_VERSION,
        run_id=run_id,
        preset=preset,
        source=source,
        baselines={str(key): str(value) for key, value in baselines.items()},
        entries=tuple(entries),
        cascade_join=cascade_join_raw if isinstance(cascade_join_raw, dict) else None,
    )
