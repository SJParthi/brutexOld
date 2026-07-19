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

## Rust hosting (DEC-RUST-CASCADE-RESULTS-001, slice 2b)

The LAW BODIES live on the ``rust/brutex-cascade`` crate
(``src/top100_artifact.rs`` — the ``_dumps`` byte law, the gate predicate,
the whole ``build_top100`` pass, the ``load_top100`` validation ladder),
verified against the FROZEN pre-conversion parity corpus
(``tests/golden/fixtures/cascade_rust_parity/top100_artifact.json``). This
module is the TYPE-FACE SHIM: the dataclasses + their ``__post_init__``
raises, the constants, file I/O (atomic writes, the read arms, the stamp's
wall-clock ``produced_at_utc``), the ``top100.load_invalid`` /
``top100.written`` log emissions, and the ranking/presets delegation
(``brutex.ranking`` is a hard carve-out — ``resolve_preset`` /
``rank_candidates`` stay Python and are passed to the crate as data,
alongside ``matched_condition_names``, the ``Top100Error`` /
``Top100Artifact`` classes and the module logger).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, TypeAlias, cast

import structlog

from brutex.data.atomic_io import atomic_write_simple
from brutex.engine.cascade_rust import require_cascade_rust
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

    from brutex.engine.cascade_join import CascadeJoin
    from brutex.engine.combo_aggregates import ComboAggregate
    from brutex.engine.results_store import ResultRow
    from brutex.engine.twin_detect import TwinFamilies

    #: The crate's `top100_load_artifact` success pieces (static-only alias).
    type _LoadPieces = tuple[
        str,
        Mapping[str, object],
        dict[str, str],
        list[Mapping[str, object]],
        Mapping[str, object] | None,
    ]

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

# The u128 mask hex width (the ``mask:%032x`` run-identity convention) and
# the sha256 digest length now live in the crate (``MASK_HEX_WIDTH`` /
# ``SHA256_HEX_LEN`` in ``rust/brutex-cascade/src/top100_artifact.rs``) —
# no shim copies (the COMMON rule).

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
    """The ONE deterministic JSON rendering law (sorted, compact, ASCII).

    Hosted on the crate (``pyjson::dumps_sorted_surrogatepass``): CPython's
    ``json.dumps(payload, sort_keys=True, separators=(",", ":"),
    ensure_ascii=True, allow_nan=False)`` byte twin — shortest-repr floats,
    ``\\uXXXX`` escapes (lone surrogates included), the verbatim
    ``allow_nan=False`` refusal message. NO trailing newline (the two
    writer call sites append it).
    """
    return cast(str, require_cascade_rust().top100_dumps(payload))


# ── Gates + ranking ──────────────────────────────────────────────────────────

#: The 5 gate constants as the crate's threading tuple — the SINGLE source
#: stays the Python constants above (cross-pinned against
#: ``brutex.ranking``'s private twins by the unit suite).
_GATES: Final[tuple[int, float, float, float, float, int]] = (
    GATE_MIN_TRADES,
    GATE_MIN_PROFIT_FACTOR,
    GATE_MAX_DRAWDOWN_PCT,
    GATE_MIN_WIN_RATE,
    GATE_MIN_WIN_LOSS_RATIO,
    GATE_MAX_CONSECUTIVE_LOSSES,
)


def passes_hard_gates(row: ResultRow, agg: ComboAggregate) -> bool:
    """Recompute the 5 hard rejection gates from the persisted row + its agg.

    The exact :func:`brutex.ranking.passes_gates` law over the two records
    that ARE persisted (``ResultRow`` lacks ``win_loss_ratio`` /
    ``max_consecutive_losses``; the agg carries them recomputed from the
    captured trades). ``ResultRow`` validates every float finite at
    construction and the agg ratios derive from exact integer folds, so the
    upstream non-finite fail-closed guard has no reachable input here. A
    ``None`` ``win_loss_ratio`` contributes ``0.0`` to the combined gate —
    the upstream ``passes_gates`` substitution, verbatim (the comparison law
    runs in the crate; the constants thread from HERE).
    """
    return cast(bool, require_cascade_rust().top100_passes_hard_gates(row, agg, _GATES))


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
    """Parse one of the agg's deterministic JSON columns back to a dict.

    Crate-hosted: a raw ``json.JSONDecodeError`` escapes UNWRAPPED (CPython's
    own ``json.loads`` is called from Rust — the frozen quirk law); a
    valid-JSON non-object raises :class:`Top100Error` with the repr-carrying
    message (defensive: the agg writer only emits objects).
    """
    return cast(
        "dict[str, object]",
        require_cascade_rust().top100_loads_object(rendered, Top100Error),
    )


def _cascade_payload(
    run_id: str, *, join: CascadeJoin | None, state: str
) -> tuple[dict[str, object], float | None]:
    """One entry's ``cascade`` object + its joined CPCV rate (or ``None``).

    The DEC-RESULTS-CASCADE-VERDICT-JOIN-001 law table (crate-hosted): a
    non-``joined`` *state* / a duplicate candidate_id / an id absent from the
    report pool each read the honest ``not_validated`` with a NAMED reason; a
    matched verdict projects verbatim (``pass`` / ``provisional_pass`` /
    ``fail``) — the annotation NEVER reranks or hides an entry (R13: a
    cascade-FAILED rank-1 stays rank-1 and is flagged by the display layer).
    """
    return cast(
        "tuple[dict[str, object], float | None]",
        require_cascade_rust().top100_cascade_payload(run_id, join, state),
    )


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
    return cast(
        Top100Artifact,
        require_cascade_rust().top100_build(
            candidates,
            run_id,
            preset,
            families,
            counts,
            data_snapshot_hashes,
            baselines,
            reason_provider,
            margin_roi_provider,
            cascade_join,
            resolve_preset,
            rank_candidates,
            matched_condition_names,
            Top100Error,
            Top100Artifact,
            _LOG,
            _GATES,
            TOP100_LIMIT,
            TOP100_SCHEMA_VERSION,
        ),
    )


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

    rs = require_cascade_rust()
    target_dir = runs_dir / run_id
    try:
        stamp_raw = (target_dir / TOP100_STAMP_FILENAME).read_bytes()
    except OSError:
        _invalid("stamp_unreadable")
        return None
    reason, content_hash = cast(
        "tuple[str | None, str | None]",
        rs.top100_load_stamp(stamp_raw, run_id, _STAMP_KEYS, TOP100_STAGE),
    )
    if reason is not None:
        _invalid(reason)
        return None

    try:
        raw = (target_dir / TOP100_FILENAME).read_bytes()
    except OSError:
        _invalid("artifact_unreadable")
        return None
    reason, pieces = cast(
        "tuple[str | None, tuple[object, ...] | None]",
        rs.top100_load_artifact(
            raw, content_hash, run_id, TOP100_SCHEMA_VERSION, ENTRY_REQUIRED_KEYS
        ),
    )
    if reason is not None:
        _invalid(reason)
        return None
    # The crate returns EXACTLY one non-None arm — pieces is set here.
    preset, source, baselines, entries, cascade_join_raw = cast("_LoadPieces", pieces)
    return Top100Artifact(
        schema_version=TOP100_SCHEMA_VERSION,
        run_id=run_id,
        preset=preset,
        source=source,
        baselines=baselines,
        entries=tuple(entries),
        cascade_join=cascade_join_raw,
    )
