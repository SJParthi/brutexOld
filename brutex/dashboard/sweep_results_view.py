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
  dashboard must still render (DEC-RESULTS-SURFACE-HONESTY-001). A duplicate
  differing ONLY on the REFERENCE-ONLY VIX trio (DEC-VIX-STAMP-WIRE-001 — the
  India-VIX lake moved between writes / a pre-VIX store beside a stamped one)
  is classified separately: the more-informed row wins and the distinct
  ``sweep_results.vix_only_divergence`` WARNING fires instead — never the
  corruption-grade counter.

The discovery + read is ``O(stores × rows)`` — bounded by the funnel's final
10²–10⁴ finalist rows per tuple (``TRACK1_BACKTEST_SPEC.md`` §"Result Row Schema")
— plus ONE ``O(audit lines)`` STREAMING scan of the tree's audit chain for the
era markers (O(1) memory — :func:`brutex.orchestrator.audit.iter_entries`).
Since DEC-LEDGER-DELTA-O-PAGE-001 that generation-invariant work is split into
:func:`load_sweep_scan` (a :class:`SweepScan`), which the route caches behind
the ``sweep_results_revision`` content token — so a filter/rank/top_n click
within one revision re-pays ONLY the O(N log N) rank + O(top_n) projection,
zero store loads + zero audit reads; a repeat poll is an O(1) cache hit.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

import blake3
import structlog

from brutex.dashboard.composite_join import COMPOSITE_STATE_ABSENT, CompositeIndex
from brutex.dashboard.movers_resilient import movers_read_skip_errors
from brutex.dashboard.oneclick_view import cascade_status_text
from brutex.engine.cascade_join import (
    CascadeJoin,
    CascadeJoinError,
    build_cascade_join,
    cascade_report_path,
)
from brutex.engine.results_store import (
    VIX_FIELDS,
    ResultRow,
    ResultsStore,
    ResultsStoreError,
)
from brutex.money import format_inr, paisa_to_rupees
from brutex.ranking.presets import PRESET_NAMES, PRESETS, RankRecord, sort_by_preset

_LOG = structlog.get_logger("brutex.dashboard.sweep_results_view")

__all__ = [
    "CASCADE_REPORT_MAX_BYTES",
    "DEFAULT_RANK_BY",
    "DEFAULT_TOP_N",
    "GATE_COORDINATE_SENTINEL",
    "GATE_COORDINATE_UNGATED",
    "MAX_TOP_N",
    "PRESET_INERT_AXES",
    "RANKABLE_COLUMNS",
    "SNAPSHOT_ERA_AMBIGUOUS",
    "SNAPSHOT_ERA_UNKNOWN",
    "STORE_ERROR_PUBLIC_DETAIL",
    "SWEEPS_DIRNAME",
    "CascadeJoinLoad",
    "SweepLeaderboard",
    "SweepScan",
    "build_sweep_leaderboard",
    "gate_coordinate_of",
    "load_cascade_join_file",
    "load_sweep_scan",
    "resolve_preset_param",
    "resolve_rank_by_param",
    "store_error_payload",
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

# ── Gate-scoped serving (DEC-SWEEP-SERVING-GATE-PROVENANCE-001) ──────────────

#: The gate coordinate of every row/store WITHOUT gate provenance — a legacy
#: store, an ungated default-selection run, or a stage-3 refined cell (the
#: cell writer does not thread the token yet — a ledgered follow-up). A stable
#: label, never ``None``, so it groups and renders.
GATE_COORDINATE_UNGATED: Final[str] = "ungated"

#: The gate coordinate of a 0-ROW gated store (the ``gate_provenance`` column
#: is present in the Parquet schema but there are no rows to read the config
#: VALUE from) — "a gated re-run ran here and persisted ZERO survivors, config
#: unknown". Folds into the single known config when exactly one exists (the
#: coexistence folding rule in :func:`_fold_gate_coordinates`).
GATE_COORDINATE_SENTINEL: Final[str] = "gated"

#: Prefix + digest length of a KNOWN gate-config coordinate tag — a short
#: BLAKE3 of the ``gate_provenance`` column VALUE (the pre-image stays on the
#: row payload as ``gate_provenance``). GATE-CONFIG-scoped, NOT store-path
#: scoped: every tuple of one gated run shares ONE coordinate.
_GATE_TAG_PREFIX: Final[str] = "gate:"
_GATE_TAG_HEX_LEN: Final[int] = 16

#: How many DISTINCT KNOWN gate-config coordinates make a tree MIXED (the
#: ``|K| >= 2`` invariant of the coexistence folding rule).
_MIXED_KNOWN_CONFIG_MIN: Final[int] = 2


def gate_coordinate_of(gate_provenance: str | None) -> str:
    """Return the gate COORDINATE of one row's ``gate_provenance`` token.

    ``None`` → :data:`GATE_COORDINATE_UNGATED` (the honest legacy/ungated
    bucket, never merged with a gated one); a known token → the stable short
    tag ``gate:<blake3-16hex>`` of the token bytes — the SAME philosophy as the
    engine's ``_tuple_digest`` (a short content digest), GATE-CONFIG-scoped so
    all tuples of one gated run group into ONE coordinate. Pure + O(1).
    """
    if gate_provenance is None:
        return GATE_COORDINATE_UNGATED
    digest = str(blake3.blake3(gate_provenance.encode("utf-8")).hexdigest())
    return _GATE_TAG_PREFIX + digest[:_GATE_TAG_HEX_LEN]


#: Hard byte cap on one cascade-report read (DEC-RESULTS-CASCADE-VERDICT-
#: JOIN-001): a real report is a few KiB–MiB; an out-of-band multi-GB file
#: fails closed as ``oversized`` WITHOUT being read whole (the
#: ``lake_stats_manifest`` bounded-shard-read precedent).
CASCADE_REPORT_MAX_BYTES: Final[int] = 16 * 1024 * 1024

#: ``CascadeJoinLoad.state`` tokens (the /results-side degrade vocabulary).
_CASCADE_LOAD_JOINED: Final[str] = "joined"
_CASCADE_LOAD_ABSENT: Final[str] = "absent"
_CASCADE_LOAD_UNREADABLE: Final[str] = "unreadable"
_CASCADE_LOAD_MALFORMED: Final[str] = "malformed"
_CASCADE_LOAD_OVERSIZED: Final[str] = "oversized"


@dataclass(frozen=True, slots=True)
class CascadeJoinLoad:
    """One cascade-report file-load verdict for the ``/results`` join.

    ``state`` ∈ ``joined | absent | unreadable | malformed | oversized``;
    ``join`` is non-``None`` ONLY for ``joined``; ``token`` echoes the stat
    token the load was memoised under (``None`` = uncached read).
    ``absent`` is the byte-identity state: the caller emits ZERO new payload
    keys (a no-report lake's ``/results`` payload is byte-identical to
    pre-change — pinned).
    """

    join: CascadeJoin | None
    state: str
    token: str | None


#: Single-entry cascade-join memo keyed by the report's stat token (the
#: #1665 equity-memo precedent): ONE parse + ONE failed-load WARN per
#: revision, never per poll. Guarded by ``_JOIN_MEMO_LOCK`` (poll threads).
_JOIN_MEMO: CascadeJoinLoad | None = None
_JOIN_MEMO_LOCK = threading.Lock()


def _memo_get(token: str) -> CascadeJoinLoad | None:
    """The memoised load for *token*, or ``None`` on a memo miss."""
    with _JOIN_MEMO_LOCK:
        memo = _JOIN_MEMO
    if memo is not None and memo.token == token:
        return memo
    return None


def _memo_put(load: CascadeJoinLoad) -> None:
    """Store *load* as the single memo entry (token non-``None`` only)."""
    global _JOIN_MEMO  # noqa: PLW0603 — the documented single-entry memo
    with _JOIN_MEMO_LOCK:
        _JOIN_MEMO = load


def load_cascade_join_file(results_root: Path, *, token: str | None = None) -> CascadeJoinLoad:
    """Load + flatten ``<results_root>/cascade/cascade-report.json`` for /results.

    DEC-RESULTS-CASCADE-VERDICT-JOIN-001 — the READ-time consumer of the ONE
    :func:`brutex.engine.cascade_join.build_cascade_join` law (the top-100
    writer consumes the same law in-memory). Guard map (each a NAMED state,
    never a raise/500): missing file → ``absent`` (NO log — the ordinary
    pre-cascade lake); any other :class:`OSError` → ``unreadable``;
    ``size > CASCADE_REPORT_MAX_BYTES`` → ``oversized`` (the file is never
    read whole); JSON/encoding/nesting damage or a structural
    :class:`~brutex.engine.cascade_join.CascadeJoinError` → ``malformed``.

    *token* is the report's ``cascade_report_revision`` stat token: a
    non-``None`` token memoises the parsed load in the module's single-entry
    memo, so a repeat poll pays ZERO parses and a failed load WARNs ONCE per
    revision (never per poll); ``None`` (a probe outage / an unwired caller)
    reads fresh and caches NOTHING (the ``lake_revision`` doctrine).
    """
    if token is not None:
        memoised = _memo_get(token)
        if memoised is not None:
            return memoised
    path = cascade_report_path(results_root)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        load = CascadeJoinLoad(join=None, state=_CASCADE_LOAD_ABSENT, token=token)
        if token is not None:
            _memo_put(load)
        return load
    except OSError as exc:
        return _failed_load(_CASCADE_LOAD_UNREADABLE, path, token, exc)
    if size > CASCADE_REPORT_MAX_BYTES:
        return _failed_load(
            _CASCADE_LOAD_OVERSIZED,
            path,
            token,
            ValueError(f"{size} bytes > the {CASCADE_REPORT_MAX_BYTES}-byte cap"),
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return _failed_load(_CASCADE_LOAD_UNREADABLE, path, token, exc)
    try:
        join = build_cascade_join(json.loads(raw))
    except (ValueError, UnicodeDecodeError, RecursionError, CascadeJoinError) as exc:
        # json.JSONDecodeError is a ValueError; a nesting bomb raises
        # RecursionError; a structural report defect raises CascadeJoinError
        # — every arm is the SAME honest "malformed" degrade.
        return _failed_load(_CASCADE_LOAD_MALFORMED, path, token, exc)
    load = CascadeJoinLoad(join=join, state=_CASCADE_LOAD_JOINED, token=token)
    if token is not None:
        _memo_put(load)
    return load


def _failed_load(state: str, path: Path, token: str | None, exc: BaseException) -> CascadeJoinLoad:
    """One WARN + the memoised failed load (once per revision, never per poll)."""
    _LOG.warning(
        "sweep_results.cascade_report_load_failed",
        state=state,
        path=str(path),
        error_type=type(exc).__name__,
        error=str(exc),
    )
    load = CascadeJoinLoad(join=None, state=state, token=token)
    if token is not None:
        _memo_put(load)
    return load


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


# ── R30 ranking-preset inert-axes disclosure ────────────────────────────────

#: The :class:`~brutex.ranking.presets.RankRecord` axes NOT persisted on
#: :class:`~brutex.engine.results_store.ResultRow` — constant-filled by
#: :func:`_preset_rank_record` so they are INERT for every row (ordering falls
#: through to the next preset key, ultimately ``run_id`` ASC), surfaced on the
#: payload (``preset_inert_axes``) so a preset-ranked view never pretends those
#: axes discriminated (Golden Rule #10). Since
#: DEC-RESULTS-CASCADE-VERDICT-JOIN-001 this is the STATIC WORST-CASE set: the
#: ``composite_score`` axis ACTIVATES dynamically when the read-time composite
#: index (:mod:`brutex.dashboard.composite_join` over the newest run's aggs
#: sidecars) joined non-empty — the build then discloses the EFFECTIVE axes via
#: ``SweepLeaderboard.inert_axes`` (``composite_score`` removed), closing the
#: /results read-time half of HIGH #40; ``max_consecutive_losses`` stays inert
#: (still not persisted anywhere readable per-row — the persist half is the
#: named follow-up DEC-CASCADE-ROW-PERSIST-002).
PRESET_INERT_AXES: Final[tuple[str, ...]] = ("composite_score", "max_consecutive_losses")

#: The effective inert axes once the composite index joined non-empty.
_INERT_AXES_COMPOSITE_ACTIVE: Final[tuple[str, ...]] = ("max_consecutive_losses",)


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
    preset
        The APPLIED ranking-preset token (one of
        :data:`brutex.ranking.presets.PRESET_NAMES`) when the leaderboard was
        ordered by a named R30 preset instead of the ``rank_by`` column, else
        ``None`` (the byte-identical legacy ordering —
        DEC-SWEEP-RESULTS-DEPTH-001, gap-register ruling 10). When set, the
        ordering came ENTIRELY from the preset; ``rank_by`` is echoed unchanged
        for the page's dropdown state but did NOT order the rows.
    inert_axes
        The EFFECTIVE preset-inert axes for THIS build
        (DEC-RESULTS-CASCADE-VERDICT-JOIN-001 — ``composite_score`` leaves
        the set IFF the composite index joined non-empty, so the
        ``?preset=composite`` axis is disclosed honestly ACTIVE for scored
        rows); defaults to the full :data:`PRESET_INERT_AXES`.
    cascade_join_meta, composite_join_meta, cascade_flagged
        The ADDITIVE payload meta (DEC-RESULTS-CASCADE-VERDICT-JOIN-001) —
        each ``None`` when its loader did not run OR read the honest
        ``absent`` state, keeping a no-report lake's payload BYTE-IDENTICAL
        to pre-change (pinned). ``cascade_flagged`` counts the cascade-FAIL
        rows on the RANKED page (the header chip — flagged, never excluded).
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
    preset: str | None = None
    #: The RESOLVED served gate coordinate (DEC-SWEEP-SERVING-GATE-
    #: PROVENANCE-001 payload contract): :data:`GATE_COORDINATE_UNGATED`, a
    #: ``gate:<hash>`` tag, or the 0-row :data:`GATE_COORDINATE_SENTINEL`;
    #: ``None`` ONLY in the ambiguous mixed case (>1 distinct KNOWN gated
    #: config — case 3, everything served + labeled, never a silent pick).
    gate_coordinate: str | None = GATE_COORDINATE_UNGATED
    #: The FOLDED sorted distinct coordinates present across the scanned
    #: stores (the coexistence folding rule applied — a lone 0-row sentinel
    #: beside exactly one known config folds into it).
    gate_coordinates: tuple[str, ...] = ()
    #: ``True`` iff >= 2 distinct KNOWN gate-config coordinates exist among
    #: populated gated stores (the |K| >= 2 invariant) — forces the UI
    #: coordinate dropdown.
    mixed_gate_coordinates: bool = False
    #: Honest 0-row-gated context ("N evaluated, 0 survived the gate" per
    #: readable ``.trials.json``) — non-empty ONLY when the SERVED coordinate
    #: is the 0-row sentinel; never fabricated.
    gate_reasons: tuple[str, ...] = ()
    inert_axes: tuple[str, ...] = PRESET_INERT_AXES
    cascade_join_meta: dict[str, Any] | None = None
    composite_join_meta: dict[str, Any] | None = None
    cascade_flagged: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable payload for the ``/api/v1/sweep/leaderboard`` route.

        The payload is the legacy shape plus the ADDITIVE keys of three
        later decisions. A preset-ranked payload gains ``preset`` +
        ``preset_inert_axes`` (the EFFECTIVE inert axes of THIS build — see
        :attr:`inert_axes`); the absent/blank-preset arm emits neither
        (gap-register ruling 10's pin, relaxed once for the preset pair). The
        cascade/composite joins (DEC-RESULTS-CASCADE-VERDICT-JOIN-001) add
        their EMIT-ON-PRESENT meta keys (``cascade_join`` /
        ``composite_join`` / ``cascade_flagged``) only when the respective
        loader ran AND found something to report — a no-report lake's payload
        carries none (byte-identity pinned). The FOUR gate-scoping keys
        (DEC-SWEEP-SERVING-GATE-PROVENANCE-001 — ``gate_coordinate`` /
        ``gate_coordinates`` / ``mixed_gate_coordinates`` / ``gate_reasons``)
        are ALWAYS present (additive; a single-coordinate legacy lake keeps an
        identical row set + order, only these keys appear).
        """
        payload: dict[str, Any] = {
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
            "gate_coordinate": self.gate_coordinate,
            "gate_coordinates": list(self.gate_coordinates),
            "mixed_gate_coordinates": self.mixed_gate_coordinates,
            "gate_reasons": list(self.gate_reasons),
            "rows": list(self.rows),
        }
        if self.preset is not None:
            payload["preset"] = self.preset
            payload["preset_inert_axes"] = list(self.inert_axes)
        if self.cascade_join_meta is not None:
            payload["cascade_join"] = dict(self.cascade_join_meta)
        if self.composite_join_meta is not None:
            payload["composite_join"] = dict(self.composite_join_meta)
        if self.cascade_flagged is not None:
            payload["cascade_flagged"] = self.cascade_flagged
        return payload


@dataclass(frozen=True, slots=True)
class SweepScan:
    """Every rank-independent scan product of ``<lake>/sweeps`` — cache once per revision.

    DEC-LEDGER-DELTA-O-PAGE-001: the ``/results`` build splits into this
    generation-invariant SCAN (store discovery + resilient load + the streaming
    audit era pass + the option sets) and the cheap per-request RANK
    (:func:`build_sweep_leaderboard`), so the route can cache ONE scan per
    ``sweep_results_revision`` and re-rank it per filter/rank/top_n click with
    zero store loads + zero audit reads. Carries ALL the payload-honesty fields
    (``skipped`` / ``run_id_conflicts`` / ``snapshot_hashes`` / the dropdown
    option sets) so none can drift across the split.

    Attributes
    ----------
    rows
        Every distinct (run_id-deduped) :class:`ResultRow` discovered across the
        readable stores, in the deterministic first-wins load order.
    store_count, skipped, run_id_conflicts
        The honest load counters (see :func:`_load_rows_resilient`).
    eras
        Tuple-coordinate → recorded ``data_snapshot_hash`` eras
        (see :func:`_snapshot_eras`); empty when no rows / no audit signal.
    instruments, timeframes, directions
        The sorted distinct option sets over EVERY discovered row (filter
        dropdowns — independent of any active filter).
    snapshot_hashes
        The sorted distinct eras recorded for the coordinates the discovered
        rows cover (empty = honest "no signal").
    """

    rows: tuple[ResultRow, ...]
    store_count: int
    skipped: int
    run_id_conflicts: int
    eras: dict[_EraKey, frozenset[str]]
    instruments: tuple[str, ...]
    timeframes: tuple[str, ...]
    directions: tuple[str, ...]
    snapshot_hashes: tuple[str, ...]
    #: RAW (unfolded) sorted distinct STORE-LEVEL gate coordinates
    #: (DEC-SWEEP-SERVING-GATE-PROVENANCE-001): the per-row
    #: :func:`gate_coordinate_of` values of every loaded row, PLUS — for every
    #: 0-ROW store — the schema-presence verdict
    #: (:data:`GATE_COORDINATE_SENTINEL` when the ``gate_provenance`` column is
    #: present, :data:`GATE_COORDINATE_UNGATED` otherwise), so a gated re-run
    #: that persisted ZERO survivors is IN the coordinate set (the arm-2
    #: store-level correction). Coordinate-AGNOSTIC raw union — the
    #: coexistence FOLDING + the serving resolution live in
    #: :func:`build_sweep_leaderboard`, never here. Default ``()`` keeps any
    #: directly-constructed legacy scan resolving to "no scoping".
    gate_coordinates: tuple[str, ...] = ()
    #: Honest context strings for the 0-row-gated (case-2) payload — one per
    #: 0-row GATED store whose ``.trials.json`` sidecar was readable
    #: ("N evaluated, 0 survived the gate"); empty when none/unreadable.
    gate_trials_context: tuple[str, ...] = ()


def load_sweep_scan(lake_root: Path) -> SweepScan:
    """Perform the generation-invariant ``<lake>/sweeps`` scan for the ``/results`` page.

    The rank-independent half of the pre-split ``build_sweep_leaderboard`` body,
    moved verbatim (DEC-LEDGER-DELTA-O-PAGE-001): store discovery
    (:func:`_iter_store_paths`) → resilient load (:func:`_load_rows_resilient`)
    → the dropdown option sets → the era map (:func:`_snapshot_eras`, only when
    rows exist) → the covered ``snapshot_hashes``. Pure function of the sweeps
    tree — the route caches the result under the ``sweep_results_revision``
    content token.

    Honest envelope (Golden Rule #10): cold cost is O(stores) glob +
    O(stores × rows) load + ONE O(audit lines) STREAMING era pass (O(1) memory
    via :func:`brutex.orchestrator.audit.iter_entries`) — paid once per
    revision, NOT per poll; the per-era byte-offset ``AuditCursor`` resume is
    NAMED future work, not claimed.
    """
    sweeps_root = lake_root / SWEEPS_DIRNAME
    paths = _iter_store_paths(sweeps_root)
    (
        all_rows,
        store_count,
        skipped,
        run_id_conflicts,
        gate_coordinates,
        gate_trials_context,
    ) = _load_rows_resilient(paths)

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

    return SweepScan(
        rows=tuple(all_rows),
        store_count=store_count,
        skipped=skipped,
        run_id_conflicts=run_id_conflicts,
        eras=eras,
        instruments=instruments,
        timeframes=timeframes,
        directions=directions,
        snapshot_hashes=snapshot_hashes,
        gate_coordinates=gate_coordinates,
        gate_trials_context=gate_trials_context,
    )


def _resolve_rank_by(rank_by: str | None) -> str:
    """Return the ranking column to use — the default for ``None`` / an unknown name.

    A strict allow-list keeps an arbitrary query string from reaching the engine's
    ``top_k`` (which raises on an unknown field); an unrecognised value degrades
    honestly to :data:`DEFAULT_RANK_BY` rather than 500-ing the page (Golden Rule #10).
    """
    if rank_by is not None and rank_by in RANKABLE_COLUMNS:
        return rank_by
    return DEFAULT_RANK_BY


def resolve_rank_by_param(rank_by: str | None) -> str:
    """Resolve a raw ``?rank_by=`` query token to its EFFECTIVE ranking column.

    The public twin of :func:`_resolve_rank_by` (ONE source of truth — a thin
    delegator, CLAUDE.md §11 COMMON) for callers that must key on the RESOLVED
    value BEFORE the build runs: the route's L2 cache key
    (DEC-SWEEP-SERVING-GATE-PROVENANCE-001, MIGRATION.md #1758 — the d1
    cache-key-injection fix) carries this resolved column, never the raw
    user-controlled string, so an unknown/hostile ``rank_by`` (a) cannot smuggle
    key-delimiter bytes into the key and (b) shares ONE cache slot with every
    other spelling that resolves to the same column (the payload echoes the
    resolved value, so slot-sharing is byte-exact). Rank-time behaviour is
    UNCHANGED — :func:`build_sweep_leaderboard` still applies the identical
    fall-back internally.
    """
    return _resolve_rank_by(rank_by)


def _clamp_top_n(top_n: int) -> int:
    """Clamp the requested leaderboard depth into ``[1, MAX_TOP_N]``."""
    if top_n < 1:
        return 1
    if top_n > MAX_TOP_N:
        return MAX_TOP_N
    return top_n


# ── R30 ranking-preset adapter (DEC-SWEEP-RESULTS-DEPTH-001 · ruling 10) ──
# THIN by design: every ordering law lives in brutex.ranking.presets (the ONE
# preset home, CLAUDE.md §11 COMMON); this surface only VALIDATES the query
# token and PROJECTS a persisted ResultRow onto the preset's RankRecord.
# (PRESET_INERT_AXES itself is declared above SweepLeaderboard — its default.)


def resolve_preset_param(preset: str | None) -> str | None:
    """Validate the optional ``?preset=`` query token against the R30 registry.

    ``None`` / blank (empty or whitespace-only) means "no preset" → ``None``:
    the caller keeps the legacy ``rank_by`` ordering byte-identical (the
    absent/blank arm of gap-register ruling 10). A KNOWN token (exact,
    case-sensitive — the registry keys are the wire vocabulary) is returned
    stripped. An UNKNOWN token raises ``ValueError`` naming every valid value,
    which the route maps to a clean 400 — deliberately NOT the
    :func:`brutex.ranking.presets.resolve_preset` silent-default degrade: a
    typo on an explicit operator choice must refuse loudly, never quietly rank
    by something else (Golden Rule #1).
    """
    if preset is None:
        return None
    token = preset.strip()
    if not token:
        return None
    if token in PRESETS:
        return token
    raise ValueError(f"unknown ranking preset {token!r}; valid presets: {', '.join(PRESET_NAMES)}")


def _preset_rank_record(
    row: ResultRow, composite_of: Mapping[str, float] | None = None
) -> RankRecord:
    """Project one persisted :class:`ResultRow` onto the preset rank inputs.

    The persisted full-precision metrics map 1:1 (NEVER rounded — CLAUDE.md §4;
    raw floats, no coercion). ``max_consecutive_losses`` is NOT persisted on
    ``ResultRow``: it is filled with the CONSTANT no-signal ``0``, IDENTICAL
    for every row, so it can never reorder anything; ties fall through to the
    store's ``run_id``-ASC law inside
    :func:`~brutex.ranking.presets.sort_by_preset`. ``composite_score``
    (DEC-RESULTS-CASCADE-VERDICT-JOIN-001) reads the JOINED ranking composite
    from *composite_of* (the newest run's aggs-sidecar index) when supplied —
    an unscored row (or ``composite_of=None``, every pre-change caller) keeps
    the presets module's honest ``None`` "unscored" state (the existing
    None-after-scored ordering law — never a fabricated 0). Constants are not
    fabricated per-row data — they are never displayed, only ordering-inert.
    """
    return RankRecord(
        run_id=row.run_id,
        max_drawdown_pct=row.max_drawdown_pct,
        composite_score=None if composite_of is None else composite_of.get(row.run_id),
        max_consecutive_losses=0,
        profit_factor=row.profit_factor,
        win_rate=row.win_rate,
        calmar_ratio=row.calmar_ratio,
    )


#: The externally-served ``error`` marker of :func:`store_error_payload` — a
#: STABLE generic token, deliberately NOT ``str(exc)``: the raised message can
#: carry internals (store paths, decode tracebacks' text), and the route is an
#: externally-served response (CodeQL py/stack-trace-exposure). The full
#: exception detail is logged server-side under ``sweep_results.store_error``.
STORE_ERROR_PUBLIC_DETAIL: Final[str] = (
    "results store unavailable — see the server log (sweep_results.store_error)"
)


def store_error_payload(exc: ResultsStoreError) -> dict[str, Any]:
    """The honest ``/api/v1/sweep/leaderboard`` degrade payload for a store fault.

    DEC-SWEEP-RESULTS-DEPTH-001 (package C3): when a typed
    :class:`~brutex.engine.results_store.ResultsStoreError` escapes the
    per-store skip net (``_load_rows_resilient`` already skips + counts a
    single bad store — this is the belt for a fault raised OUTSIDE that loop),
    the route returns THIS payload instead of a 500. It carries the full empty
    leaderboard shape (so the page's renderer keeps working) PLUS the explicit
    ``error`` / ``error_type`` markers — never the bare ``available: false``
    blank that would be indistinguishable from "no sweep run yet" (no blank
    illusion, Golden Rule #1).

    SANITIZED (CodeQL py/stack-trace-exposure): the served ``error`` marker is
    the STABLE :data:`STORE_ERROR_PUBLIC_DETAIL` token + ``error_type`` is the
    stable ``results_store`` reason token — ``str(exc)`` (which can leak store
    paths / decode internals) NEVER reaches the wire. The FULL exception detail
    (message + concrete class) is logged server-side here
    (``sweep_results.store_error``) so the degrade stays diagnosable wherever
    it fires.
    """
    _LOG.warning("sweep_results.store_error", error=str(exc), error_type=type(exc).__name__)
    return {
        "available": False,
        "error": STORE_ERROR_PUBLIC_DETAIL,
        "error_type": "results_store",
        "total_rows": 0,
        "store_count": 0,
        "skipped": 0,
        "rank_by": DEFAULT_RANK_BY,
        "instruments": [],
        "timeframes": [],
        "directions": [],
        "snapshot_hashes": [],
        "mixed_snapshot_eras": False,
        "run_id_conflicts": 0,
        "gate_coordinate": None,
        "gate_coordinates": [],
        "mixed_gate_coordinates": False,
        "gate_reasons": [],
        "rows": [],
    }


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


def _load_rows_resilient(
    paths: Sequence[Path],
) -> tuple[list[ResultRow], int, int, int, tuple[str, ...], tuple[str, ...]]:
    """Load every readable store; return the rows + counters + gate-coordinate census.

    Returns ``(rows, store_count, skipped, run_id_conflicts, gate_coordinates,
    gate_trials_context)`` — see :class:`SweepScan` for the last two.

    A single corrupt / version-incompatible store is SKIPPED (logged + counted),
    mirroring :func:`brutex.dashboard.movers_resilient` so one bad file never 500s
    the page. The engine wraps a damaged Parquet into :class:`ResultsStoreError`
    (DEC-SWEEP-VERIFY-001), and the raw polars/duckdb/OS faults are covered by the
    COMMON :func:`movers_read_skip_errors` skip set — both are caught here; a
    genuine logic error (``ValueError`` / ``KeyError`` …) still propagates.

    Rows are keyed by ``(gate_coordinate, run_id)`` across stores
    (DEC-SWEEP-SERVING-GATE-PROVENANCE-001 — the arm-1 fix): the per-mask
    ``run_id`` is deliberately gate-BLIND, so the SAME combo re-run under a
    DIFFERENT gate config is a genuinely different logical result — BOTH rows
    are kept, each under its own coordinate, and the pair is NOT counted as a
    conflict. WITHIN one coordinate the pre-existing law is byte-unchanged: an
    identical duplicate is a silent dedup; a duplicate carrying DIFFERENT
    metrics (upstream non-determinism / a healed store next to its stale twin)
    keeps the deterministic FIRST row — *paths* is sorted, so first-wins is
    discovery-order-independent — but is COUNTED in ``run_id_conflicts`` +
    logged as a ``sweep_results.run_id_conflict`` WARNING
    (DEC-RESULTS-SURFACE-HONESTY-001). NOTE the SEMANTIC NARROWING (ledgered):
    ``run_id_conflicts`` now means WITHIN-coordinate conflicts only — a
    cross-coordinate duplicate is scoping, not corruption. A within-coordinate
    duplicate differing ONLY on the REFERENCE-ONLY VIX trio
    (:func:`_differs_only_in_vix` over the public
    :data:`~brutex.engine.results_store.VIX_FIELDS`) is NOT that family — VIX
    sits deliberately OUTSIDE the run identity (DEC-VIX-STAMP-WIRE-001: the
    India-VIX lake moved between the writes, or a pre-VIX store sits beside a
    stamped one), so it keeps the MORE-INFORMED row, logs the distinct
    ``sweep_results.vix_only_divergence`` WARNING, and is NOT counted in
    ``run_id_conflicts``. The engine's ``ResultsStore.append`` RAISES on the same
    divergence (Golden Rule #4) — the dashboard must render regardless, so it
    counts instead of raising (the engine's strictness stays engine-side).

    The gate-coordinate census is STORE-level (the arm-2 correction): a
    populated store contributes its rows' coordinates; a 0-ROW store
    contributes the schema-presence verdict via
    :attr:`~brutex.engine.results_store.ResultsStore.gate_provenance_present`
    — the sentinel :data:`GATE_COORDINATE_SENTINEL` for a gated schema (a
    gated re-run that persisted zero survivors MUST be able to win the empty
    set), :data:`GATE_COORDINATE_UNGATED` otherwise — plus, best-effort, the
    0-row gated store's ``.trials.json`` context string.
    """
    # The unreadable-store skip set: the engine wraps a damaged Parquet into its OWN
    # ResultsStoreError (DEC-SWEEP-VERIFY-001), and the COMMON movers skip set
    # (polars/duckdb/OSError) is folded in belt-and-suspenders in case a raw fault
    # ever escapes the wrap — caught in ONE handler so both families are coverable.
    skip_errors: tuple[type[BaseException], ...] = (
        ResultsStoreError,
        *movers_read_skip_errors(),
    )
    by_gate_run: dict[tuple[str, str], ResultRow] = {}
    store_count = 0
    skipped = 0
    run_id_conflicts = 0
    gate_coordinates: set[str] = set()
    gate_trials: list[str] = []
    for path in paths:
        try:
            store = ResultsStore.load(path)
        except skip_errors as exc:
            skipped += 1
            _LOG.warning("sweep_results.store_skip", path=str(path), error=str(exc))
            continue
        store_count += 1
        store_rows = store.rows()
        if not store_rows:
            # A 0-ROW store still names a coordinate (the arm-2 store-level
            # correction): schema presence of the CONDITIONAL gate_provenance
            # column — readable at zero rows — decides gated-vs-ungated.
            if store.gate_provenance_present:
                gate_coordinates.add(GATE_COORDINATE_SENTINEL)
                context = _gated_trials_context(path)
                if context is not None:
                    gate_trials.append(context)
            else:
                gate_coordinates.add(GATE_COORDINATE_UNGATED)
            continue
        for row in store_rows:
            coordinate = gate_coordinate_of(row.gate_provenance)
            gate_coordinates.add(coordinate)
            key = (coordinate, row.run_id)
            existing = by_gate_run.get(key)
            if existing is None:
                by_gate_run[key] = row
            elif existing != row:
                if _differs_only_in_vix(existing, row):
                    # NOT the corruption-grade family: the REFERENCE-ONLY VIX
                    # trio sits deliberately OUTSIDE the run identity
                    # (DEC-VIX-STAMP-WIRE-001) — a VIX-only cross-store
                    # difference means the India-VIX lake moved between the
                    # two writes, or a pre-VIX store sits beside a stamped
                    # one. Keep the MORE-INFORMED row (more known trio
                    # fields; tie → the deterministic FIRST — *paths* is
                    # sorted) and log the DISTINCT event, never
                    # ``run_id_conflict``'s false non-determinism diagnosis.
                    if _vix_known_count(row) > _vix_known_count(existing):
                        by_gate_run[key] = row
                    _LOG.warning(
                        "sweep_results.vix_only_divergence",
                        run_id=row.run_id,
                        source=str(path),
                        policy="most-informed-wins",
                    )
                else:
                    run_id_conflicts += 1
                    _LOG.warning(
                        "sweep_results.run_id_conflict",
                        run_id=row.run_id,
                        gate_coordinate=coordinate,
                        dropped_from=str(path),
                        policy="first-wins",
                    )
    return (
        list(by_gate_run.values()),
        store_count,
        skipped,
        run_id_conflicts,
        tuple(sorted(gate_coordinates)),
        tuple(gate_trials),
    )


def _gated_trials_context(store_path: Path) -> str | None:
    """Best-effort "N evaluated, 0 survived the gate" line for a 0-row gated store.

    Reads the store's paired ``.trials.json`` sidecar (the SAME
    :func:`~brutex.engine.trial_stats.trial_stats_sidecar_path` convention the
    sweep writes) and phrases its honest trial census. ``None`` — absent /
    unreadable sidecar — contributes no context, never a fabricated count
    (Golden Rule #1) and never an error on the read-only page.
    """
    # Function-local import mirroring _snapshot_eras: the engine sidecar
    # reader is only paid when a leaderboard scan actually runs.
    from brutex.engine.trial_stats import (
        TrialStatsError,
        read_tuple_trial_stats,
        trial_stats_sidecar_path,
    )

    try:
        stats = read_tuple_trial_stats(trial_stats_sidecar_path(store_path))
    except (TrialStatsError, OSError):
        return None
    return f"{stats.n_backtested} evaluated, 0 survived the gate"


def _differs_only_in_vix(existing: ResultRow, row: ResultRow) -> bool:
    """``True`` iff the two rows differ ONLY on the REFERENCE-ONLY VIX trio.

    The SAME nulling law ``sweep_verify`` uses — ``replace()`` over the public
    :data:`brutex.engine.results_store.VIX_FIELDS` (§11 COMMON) — so a fourth
    trio field can never desync this classification. ``dict[str, Any]``: the
    trio fields are typed non-``None`` on the generated ``replace()``
    signature, so a ``dict[str, None]`` splat fails strict mypy even though
    ``None`` is the valid null for every nullable trio column (the
    ``sweep_verify`` precedent).
    """
    nulled: dict[str, Any] = dict.fromkeys(VIX_FIELDS)
    return replace(existing, **nulled) == replace(row, **nulled)


def _vix_known_count(row: ResultRow) -> int:
    """How many VIX trio fields *row* carries as KNOWN (non-``None``) values."""
    return sum(getattr(row, name) is not None for name in VIX_FIELDS)


def _snapshot_eras(sweeps_root: Path) -> dict[_EraKey, frozenset[str]]:
    """Best-effort map of tuple coordinates → recorded ``data_snapshot_hash`` eras.

    Reads the tree's audit chain (``<sweeps>/audit/sweep-all.jsonl``) — the SAME
    self-description :func:`brutex.engine.results_report._recorded_snapshot_hash`
    reads — collecting every ``SWEEP_START`` payload's
    ``(instrument, tf, direction, mode, k) → data_snapshot_hash``. One
    ``O(audit lines)`` STREAMING pass over
    :func:`brutex.orchestrator.audit.iter_entries` — O(1) peak memory, never the
    whole chain in a list (DEC-LEDGER-DELTA-O-PAGE-001) — paid once per
    ``sweep_results_revision`` (the route's :class:`SweepScan` cache), never per
    poll / per filter click. The per-era byte-offset incremental resume (an
    ``AuditCursor`` in :mod:`brutex.orchestrator.audit`) is NAMED future work,
    not claimed; :func:`~brutex.orchestrator.audit.verify_chain`'s full-stream
    integrity contract is structurally untouched (audit.py has zero diff here).

    Honest envelope (Golden Rule #10): an absent / unreadable / corrupt chain, or
    a payload missing the needed keys, degrades to NO signal for the affected
    coordinates (logged once for a chain-level failure) — never a guess, never a
    500 on the read-only page. ``iter_entries`` skips an unterminated torn TAIL
    in the generator itself, but a malformed COMMITTED line raises
    :class:`AuditError` at iteration time — so the guard wraps the LOOP and
    discards any partial collection (today's exact all-or-nothing degrade). A
    coordinate re-swept under two eras honestly maps to BOTH (→ ``ambiguous``
    rows); the per-file authority is dup-train PR-3's at-rest census, NOT
    claimed here (per-tuple filename digests fold the hash one-way).
    """
    # Function-local import mirroring results_report._recorded_snapshot_hash:
    # this module's read+render surface stays lean; the audit reader is only
    # paid when a leaderboard scan is actually built.
    from brutex.orchestrator.audit import AuditError, iter_entries

    audit_path = sweeps_root.joinpath(*_AUDIT_RELPATH)
    if not audit_path.exists():
        # iter_entries raises AuditError at CALL time on a missing file; the
        # absent chain is the ordinary "no signal yet" state, not a failure.
        return {}
    collected: dict[_EraKey, set[str]] = {}
    try:
        for entry in iter_entries(audit_path):
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
    except AuditError as exc:
        _LOG.warning("sweep_results.era_scan_failed", audit_path=str(audit_path), error=str(exc))
        return {}
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
    HONEST SCOPE of ``deflated_sharpe`` (DEC-STATS-GATE-INTEGRITY-001,
    R2-SR-F3): the persisted figure is **TUPLE-scoped** — deflated against one
    grid cell's rank-time ``N = len(candidates)``, an inflating under-count
    (measured: DSR 0.230 at ``n_trials=1e3`` vs 0.003 at ``1e7``) — and is
    superseded by the Stage-4 cascade's leaderboard-N DSR gate (its N/V stamp
    lives run-level on the cascade report sidecar).
    ``conditions`` is the human-readable decode the store already derived from
    ``mask_bits`` (empty string ⇒ the "enters every bar" no-condition combo).
    ``snapshot_era`` is the row's honest data-era marker — the recorded
    ``data_snapshot_hash``, :data:`SNAPSHOT_ERA_AMBIGUOUS`, or
    :data:`SNAPSHOT_ERA_UNKNOWN` (DEC-RESULTS-SURFACE-HONESTY-001).

    The THREE additive gate keys (DEC-SWEEP-SERVING-GATE-PROVENANCE-001):
    ``gate_provenance`` (the row's verbatim gate-config token, ``None`` for
    ungated/legacy — this is gate-scoped-DEDUP provenance, NOT the statutory
    5-door gate re-check; a sibling PR's ``hard_gates_recheck`` verdict is a
    DISJOINT surface), ``gate_coordinate`` (its :func:`gate_coordinate_of`
    tag — the row's own store coordinate, the ``snapshot_era`` marker pattern:
    label, never exclusion), and ``gate_reasons`` (the failed-door token list —
    ``[]`` for every persisted row: a finalist already cleared the hard gates,
    and re-judging per-row here would fabricate a verdict the persisted columns
    cannot support — ``max_consecutive_losses`` is not persisted and the DSR
    gate is pool-relative; Golden Rule #1).
    """
    return {
        "rank": rank,
        "run_id": row.run_id,
        "snapshot_era": snapshot_era,
        "gate_provenance": row.gate_provenance,
        "gate_coordinate": gate_coordinate_of(row.gate_provenance),
        "gate_reasons": [],
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


# ═══ CASCADE VERDICT JOIN (DEC-RESULTS-CASCADE-VERDICT-JOIN-001 — BUILT) ═══
# The former P2 verdict-join seam is now implemented: the Stage-4 cascade's
# per-candidate verdicts join onto /results rows by EXACT run_id equality
# (the Rule-4 identity — a verdict can only attach to the byte-identical run
# it was judged for; the pool-relative context is disclosed via the payload's
# cascade_join.sweep_id meta + the provisional/pool honesty stamps), folded
# into the row projection as ADDITIVE EMIT-ON-PRESENT keys — exactly the
# `snapshot_era` pattern (marker, never exclusion: a cascade-FAILED row stays
# RANKED and is red-flagged client-side). Absence still means "not judged by
# the latest cascade", never a fabricated verdict (Golden Rule #1).
# ═════════════════════════════════════════════════════════════════════════════


def _fold_gate_coordinates(raw: tuple[str, ...]) -> tuple[tuple[str, ...], bool]:
    """Apply the coexistence FOLDING rule; return ``(folded_sorted, mixed)``.

    DEC-SWEEP-SERVING-GATE-PROVENANCE-001, the |K|-based law over the RAW
    store-level coordinate set (K = the KNOWN ``gate:<hash>`` config
    coordinates from POPULATED gated stores; S = the 0-row
    :data:`GATE_COORDINATE_SENTINEL`; U = :data:`GATE_COORDINATE_UNGATED`):

    * ``|K| == 1`` — the 0-row sentinel FOLDS INTO that single known config (a
      single-config run's zero-survivor tuples belong to the same coordinate),
      so ``"gated"`` never appears beside its own config's tag.
    * ``|K| == 0`` — the sentinel (when present) stands as the only gated
      label.
    * ``|K| >= 2`` — genuinely different configs on one root: the sentinel
      cannot be attributed, so it stands as its OWN entry beside the known
      configs.

    ``mixed`` is the invariant ``|K| >= 2`` — a lone sentinel beside one known
    config never spuriously trips the mixed banner/dropdown. Residual
    (ledgered v2 follow-up): two DIFFERENT all-0-row gated configs both read
    the one ``"gated"`` sentinel and merge.
    """
    known = {c for c in raw if c.startswith(_GATE_TAG_PREFIX)}
    folded = set(raw)
    if len(known) == 1:
        folded.discard(GATE_COORDINATE_SENTINEL)
    return tuple(sorted(folded)), len(known) >= _MIXED_KNOWN_CONFIG_MIN


class UnknownGateCoordinateError(ValueError):
    """An explicit ``?gate=`` token that names no folded coordinate.

    Subclasses :class:`ValueError` so every existing ``except ValueError``
    caller (and the historic route catch) stays behaviourally compatible, but
    carries the offending :attr:`coordinate` token as an attribute so the route
    can build a SANITISED, LENGTH-BOUNDED 400 detail rather than echoing
    arbitrary client input (the raw token can be a 500-char / control-char
    string — an info-leak / log-spam vector if reflected verbatim).
    """

    def __init__(self, coordinate: str, message: str) -> None:
        super().__init__(message)
        #: The raw offending gate token (unsanitised — the route bounds/strips it).
        self.coordinate = coordinate


def _resolve_gate_scope(
    gate: str | None, folded: tuple[str, ...], mixed: bool
) -> tuple[str | None, str | None]:
    """Resolve the served gate coordinate; return ``(payload_coordinate, row_scope)``.

    The DEC-SWEEP-SERVING-GATE-PROVENANCE-001 payload-contract resolution over
    the FOLDED coordinate set:

    * **Explicit non-blank *gate*** — must name a folded coordinate EXACTLY;
      unknown raises ``ValueError`` naming the available set (the
      ``resolve_preset_param`` precedent — the route maps it to a clean 400;
      an explicit operator choice must refuse loudly, never silently serve
      something else). Returns ``(token, token)``.
    * **Absent/blank, <= 1 coordinate (case 1)** — serve ALL rows, no scoping
      (``row_scope=None``); the payload coordinate is the one present (or the
      honest :data:`GATE_COORDINATE_UNGATED` for an empty tree).
    * **Absent/blank, mixed (case 3)** — >= 2 distinct KNOWN configs: NEVER a
      silent pick — serve ALL rows labeled per-row, payload coordinate
      ``None``.
    * **Absent/blank, exactly one gated + ungated (case 2)** — serve ONLY the
      gated coordinate (the trustworthy view); a 0-row sentinel coordinate
      legitimately serves the EMPTY set — the honest "the gated re-run
      produced zero survivors", never the stale ungated rows.
    """
    token = (gate or "").strip()
    if token:
        if token not in folded:
            available = ", ".join(folded) or "(none)"
            raise UnknownGateCoordinateError(
                token,
                f"unknown gate coordinate {token!r}; available coordinates: {available}",
            )
        return token, token
    if len(folded) <= 1:
        return (folded[0] if folded else GATE_COORDINATE_UNGATED), None
    if mixed:
        return None, None
    # Post-fold, a non-mixed multi-coordinate set is EXACTLY one gated
    # coordinate (a gate:<hash> tag or the 0-row sentinel) + "ungated".
    gated = [c for c in folded if c != GATE_COORDINATE_UNGATED]
    return gated[0], gated[0]


def _cascade_row_keys(run_id: str, load: CascadeJoinLoad | None) -> dict[str, Any]:
    """One row's ADDITIVE cascade keys (EMIT-ON-PRESENT — {} when unjoined).

    A matched verdict emits ``cascade_status`` (``pass | provisional_pass |
    fail``) + the server-built ``cascade_status_text`` (the ONE
    :func:`~brutex.dashboard.oneclick_view.cascade_status_text` builder — no
    status prose in JS) + ``cascade_provisional``, plus ``cascade_composite``
    (pass only — the CASCADE composite, labelled "Cascade score" client-side,
    never conflated with the ranking ``composite_score``),
    ``cascade_rejected_at``/``cascade_detail`` (fail only, verbatim) and
    ``cpcv_pass_pct`` (``rate * 100.0``, exact float — only when measured).
    A DUPLICATE candidate_id emits ONLY the status text ("not validated ·
    duplicate verdict" — ambiguous evidence, nothing else displayed); an
    unmatched row emits NOTHING (the client renders "not validated" from the
    payload meta's ``joined`` state).
    """
    if load is None or load.join is None or load.state != _CASCADE_LOAD_JOINED:
        return {}
    join = load.join
    if run_id in join.ambiguous_ids:
        return {
            "cascade_status_text": cascade_status_text(
                "not_validated", reason="duplicate_candidate_id"
            )
        }
    verdict = join.by_candidate.get(run_id)
    if verdict is None:
        return {}
    status = ("provisional_pass" if verdict.provisional else "pass") if verdict.passed else "fail"
    keys: dict[str, Any] = {
        "cascade_status": status,
        "cascade_status_text": cascade_status_text(
            status, rejected_at=verdict.rejected_at, provisional=verdict.provisional
        ),
        "cascade_provisional": verdict.provisional,
    }
    if verdict.passed:
        if verdict.cascade_composite is not None:
            keys["cascade_composite"] = verdict.cascade_composite
    else:
        if verdict.rejected_at is not None:
            keys["cascade_rejected_at"] = verdict.rejected_at
        if verdict.detail:
            keys["cascade_detail"] = verdict.detail
    if verdict.cpcv_pass_rate is not None:
        # The display alias — an EXACT float unit projection (rate * 100.0),
        # never a rounding (CLAUDE.md §4); absent when unmeasured ⇒ "—".
        keys["cpcv_pass_pct"] = verdict.cpcv_pass_rate * 100.0
    return keys


def build_sweep_leaderboard(
    lake_root: Path,
    *,
    instrument: str | None = None,
    tf: str | None = None,
    direction: str | None = None,
    rank_by: str | None = None,
    top_n: int = DEFAULT_TOP_N,
    scan: SweepScan | None = None,
    preset: str | None = None,
    gate: str | None = None,
    cascade: CascadeJoinLoad | None = None,
    composite: CompositeIndex | None = None,
) -> SweepLeaderboard:
    """Build the ranked sweep leaderboard for the ``/results`` page.

    Applies the optional filters over the :class:`SweepScan`'s rows, ranks the
    survivors by *rank_by* (descending — best first) or, when *preset* names an
    R30 ranking preset, by that preset's total order, and projects the
    top-*top_n* into display dicts. Pure + framework-free (no FastAPI), so it is
    unit-tested directly. With ``scan=None`` (the default) the scan is performed
    inline via :func:`load_sweep_scan` — byte-identical to the pre-split
    behaviour for every existing caller; passing a cached *scan* skips the
    O(stores × rows) load + the audit era pass entirely
    (DEC-LEDGER-DELTA-O-PAGE-001) and pays ONLY the O(N log N) rank +
    O(top_n) projection.

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
        IGNORED for ordering when *preset* is given (echoed unchanged).
    top_n
        Leaderboard depth, clamped into ``[1, MAX_TOP_N]``.
    scan
        An already-performed :func:`load_sweep_scan` result to rank, or ``None``
        to scan now.
    preset
        Optional R30 ranking-preset token (DEC-SWEEP-RESULTS-DEPTH-001,
        gap-register ruling 10) — one of
        :data:`brutex.ranking.presets.PRESET_NAMES`. ``None``/blank keeps the
        legacy *rank_by* ordering BYTE-IDENTICAL (pinned by test); a known
        token orders the filtered rows via the ONE preset law
        (:func:`brutex.ranking.presets.sort_by_preset` — full precision,
        ``run_id``-ASC deterministic ties, the exact ``ResultsStore.top_k``
        two-pass idiom); an unknown token raises ``ValueError`` naming the
        valid values (the route maps it to a clean 400).
    gate
        Optional EXPLICIT gate coordinate to scope the served rows to
        (DEC-SWEEP-SERVING-GATE-PROVENANCE-001) — one of the tree's folded
        ``gate_coordinates`` (``"ungated"``, a ``gate:<hash>`` tag, or the
        0-row ``"gated"`` sentinel). ``None``/blank applies the deterministic
        default resolution (:func:`_resolve_gate_scope` — cases 1/2/3 of the
        payload contract); an unknown token raises ``ValueError`` naming the
        available coordinates (the route maps it to a clean 400).
    cascade
        The cascade-report load (:func:`load_cascade_join_file`) or ``None``
        (every pre-change caller — byte-identical payload). A ``joined``
        load annotates matched rows (EMIT-ON-PRESENT keys, see
        :func:`_cascade_row_keys`) + emits the payload meta; an ``absent``
        load emits NOTHING (byte-identity); a failed load emits ONLY the
        meta naming its state (DEC-RESULTS-CASCADE-VERDICT-JOIN-001 —
        annotation, never exclusion/rerank).
    composite
        The newest run's ranking-composite index
        (:func:`brutex.dashboard.composite_join.load_composite_index`) or
        ``None``. A non-empty ``joined`` index feeds every scored row's
        ``composite_score`` (display) AND activates the preset composite
        axis honestly (:attr:`SweepLeaderboard.inert_axes`); ``absent``
        emits nothing.

    Returns
    -------
    SweepLeaderboard
        The ranked rows + the honest availability / counts / filter option sets.

    Raises
    ------
    ValueError
        If *preset* names an unknown ranking preset (see
        :func:`resolve_preset_param`) or *gate* names an unknown gate
        coordinate (see :func:`_resolve_gate_scope`).
    """
    resolved_preset = resolve_preset_param(preset)
    resolved_rank = _resolve_rank_by(rank_by)
    depth = _clamp_top_n(top_n)
    if scan is None:
        scan = load_sweep_scan(lake_root)

    # Gate-coordinate resolution BEFORE the row filters (DEC-SWEEP-SERVING-
    # GATE-PROVENANCE-001): the coordinate set is STORE-level (0-row gated
    # stores included), the fold + resolution live at THIS operator-facing
    # layer — the scan stays a raw coordinate-agnostic union.
    folded_gates, mixed_gates = _fold_gate_coordinates(scan.gate_coordinates)
    gate_coordinate, gate_scope = _resolve_gate_scope(gate, folded_gates, mixed_gates)

    # The joined ranking-composite map (DEC-RESULTS-CASCADE-VERDICT-JOIN-001)
    # — non-None ONLY when the index joined non-empty: it then feeds BOTH the
    # per-row display key and the preset composite axis (the honest dynamic
    # inert-axes disclosure).
    composite_of: Mapping[str, float] | None = None
    if composite is not None and composite.state == "joined" and composite.by_run_id:
        composite_of = composite.by_run_id

    filtered = [
        r for r in scan.rows if _matches(r, instrument=instrument, tf=tf, direction=direction)
    ]
    if gate_scope is not None:
        # Scope to ONE coordinate (an explicit ?gate= or the case-2 default):
        # a foreign-gate row is EXCLUDED from this view — the one deliberate
        # exception to the marker-never-exclusion pattern, because arm-2's
        # defect IS foreign-gate rows drowning the requested coordinate. A
        # 0-row sentinel scope matches no row — the honest empty set.
        filtered = [r for r in filtered if gate_coordinate_of(r.gate_provenance) == gate_scope]
    if resolved_preset is None:
        # Deterministic rank: by the chosen column descending, ties broken by run_id
        # ascending (the SAME total order ResultsStore.top_k uses — Golden Rule #4), so
        # the leaderboard is identical for any discovery order.
        ordered = sorted(filtered, key=lambda r: r.run_id)
        ordered.sort(key=lambda r: _rank_value(r, resolved_rank), reverse=True)
    else:
        # THIN preset adapter (ruling 10): the ENTIRE ordering law — key,
        # direction, run_id-ASC tie pre-sort — lives in brutex.ranking.presets;
        # this call site only projects the persisted rows (raw full-precision
        # values, see _preset_rank_record — the composite axis reads the
        # joined index when one exists) and never re-derives a ranking rule.
        ordered = list(
            sort_by_preset(
                filtered,
                preset=PRESETS[resolved_preset],
                record_of=lambda r: _preset_rank_record(r, composite_of),
            )
        )
    rows_list: list[dict[str, Any]] = []
    matched_rows = 0
    flagged = 0
    for rank, r in enumerate(ordered[:depth], start=1):
        projected = _project_row(r, rank, snapshot_era=_row_snapshot_era(r, scan.eras))
        cascade_keys = _cascade_row_keys(r.run_id, cascade)
        if cascade_keys:
            projected.update(cascade_keys)
        if "cascade_status" in cascade_keys:
            matched_rows += 1
            if cascade_keys["cascade_status"] == "fail":
                flagged += 1
        if composite_of is not None:
            score = composite_of.get(r.run_id)
            if score is not None:
                # The RANKING composite (label "Composite") — distinct from
                # the cascade's own composite (label "Cascade score").
                projected["composite_score"] = score
        rows_list.append(projected)
    rows = tuple(rows_list)

    # EMIT-ON-PRESENT meta (byte-identity law): an ``absent`` load/index —
    # or an un-run loader — contributes NOTHING; every other state is named.
    cascade_meta: dict[str, Any] | None = None
    flagged_out: int | None = None
    if cascade is not None and cascade.state != _CASCADE_LOAD_ABSENT:
        join = cascade.join
        cascade_meta = {
            "state": cascade.state,
            "sweep_id": None if join is None else join.sweep_id,
            "n_input": None if join is None else join.n_input,
            "n_survived": None if join is None else join.n_survived,
            "matched_rows": matched_rows,
            "skipped_groups": 0 if join is None else join.skipped_groups,
            "skipped_verdicts": 0 if join is None else join.skipped_verdicts,
            "duplicate_ids": 0 if join is None else len(join.ambiguous_ids),
        }
        flagged_out = flagged
    composite_meta: dict[str, Any] | None = None
    if composite is not None and composite.state != COMPOSITE_STATE_ABSENT:
        composite_meta = {
            "state": composite.state,
            "run_id": composite.run_id,
            "scored_rows": composite.scored_rows,
        }

    return SweepLeaderboard(
        rows=rows,
        available=scan.store_count > 0,
        total_rows=len(filtered),
        store_count=scan.store_count,
        skipped=scan.skipped,
        rank_by=resolved_rank,
        instruments=scan.instruments,
        timeframes=scan.timeframes,
        directions=scan.directions,
        snapshot_hashes=scan.snapshot_hashes,
        mixed_snapshot_eras=len(scan.snapshot_hashes) > 1,
        run_id_conflicts=scan.run_id_conflicts,
        preset=resolved_preset,
        gate_coordinate=gate_coordinate,
        gate_coordinates=folded_gates,
        mixed_gate_coordinates=mixed_gates,
        # The honest 0-row-gated context — surfaced ONLY when the SERVED
        # coordinate is the sentinel (the case-2 empty-set answer); a
        # populated coordinate's finalists carry no failed-door reasons.
        gate_reasons=(
            scan.gate_trials_context if gate_coordinate == GATE_COORDINATE_SENTINEL else ()
        ),
        inert_axes=(
            _INERT_AXES_COMPOSITE_ACTIVE if composite_of is not None else PRESET_INERT_AXES
        ),
        cascade_join_meta=cascade_meta,
        composite_join_meta=composite_meta,
        cascade_flagged=flagged_out,
    )
