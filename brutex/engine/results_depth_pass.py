"""The ONE fenced run-end results-depth pass (DEC-SWEEP-RESULTS-DEPTH-001, WP5).

The ONLY module that COMPOSES the results-depth packages: per grid tuple it
re-simulates the persisted finalists on the pure-Python oracle
(:mod:`brutex.engine.trade_capture`, WP1), folds the aggregates + lenses
(:mod:`brutex.engine.combo_aggregates` / :mod:`brutex.engine.session_lens`,
WP2), writes the deterministic sidecar trio (:mod:`brutex.engine.trade_store`,
WP1), then computes the buy-&-hold baselines (:mod:`brutex.engine.baseline`,
WP2), groups the behavioral twin families (:mod:`brutex.engine.twin_detect`,
WP4) and writes the global top-100 artifact
(:mod:`brutex.engine.top100_artifact`, WP5) — design D0's run-end post-hoc
pass, one level out from ``ranked_combo_to_result_row``'s finalist re-sim
precedent, touching ZERO hot-path code.

## Fence + break law (R42, the ``greeks_enrich_pass`` precedent)

* The pass NEVER fails the sweep: WP6's CLI adapter wraps the whole call in a
  fence, and INSIDE the pass each tuple runs in its own fence — one broken
  tuple (corrupt store, capture defect) logs ``results_depth.tuple_error``,
  counts, and the grid continues (quarantine-not-abort).
  ``KeyboardInterrupt`` / ``SystemExit`` / ``MemoryError`` always re-raise raw
  (global stops are never swallowed).
* The top-100 build/write runs in its own fence: a failure logs
  ``results_depth.top100_error`` and the report says ``top100_written=False``
  — loud, reflected, rebuildable by re-running the pass.
* Idempotent resume: every tuple's sidecar stamp keys on the SOURCE store's
  sha256 — a re-run skips stamped-current tuples (their pool contribution is
  read back from the sidecars) and rebuilds only the stale rest; the
  deterministic artifacts converge to identical bytes.

## Artifact layout (gap-register Ruling 4, 2026-07-08)

EVERY run-end artifact — the tuple sidecar trios, the baselines, top100.json
+ its stamp — lands under the per-run directory built by the ONE
:func:`brutex.engine.trade_store.sweep_run_dir` seam:
``<sweep_runs root>/<run_id>/…``, a SIBLING root beside the sweeps tree,
NEVER inside the ``**/*.parquet``-globbed tree (so /results discovery, the
sweep-verify doctor and ``results_report`` are structurally untouched).
``run_id`` today = the ENGINE grid identity; Ruling 9's logical run_id
rebinds at that seam.

## Honest degradation (Golden Rule #1)

Missing reference data NEVER fails the pass: ``vix_bars=None`` ⇒ one
``results_depth.vix_absent`` WARN + every trade's VIX honestly ``None`` +
counted, and every affected combo additionally gets a CAPPED per-combo
``results_depth.vix_miss`` WARN (first :data:`VIX_MISS_WARN_CAP` combos per
pass — Ruling 7's "capped vix_miss warns"; the same capped WARN fires for a
PARTIAL VIX feed that misses individual trades); a ``None`` 1-minute feed ⇒
``path_1m_status="no_1m_data"`` per trade;
a ``None`` daily feed ⇒ an EMPTY session lens (every trade's trend buckets
``"unknown"``, zero event flags). A parity-tooth mismatch (kernel⇄oracle
drift) is counted + WARNed by the capture layer and the combo is excluded from
the pool — never a silently wrong trade list.

## Composite score (R30 tiebreak input)

Per tuple, the composite is computed over the tuple's parity-OK non-zero-trade
finalists through the COMMON :func:`brutex.ranking.composite_scores` weighted
z-expression (weights live in ONE place, never re-derived). The seven term
inputs are re-expressed over the PERSISTED row + the recomputed aggregate
(``ResultMetrics`` is not persisted): sharpe / sortino / calmar /
``profit_factor − 1`` from the row; recovery / ``win_rate × win_loss_ratio`` /
risk-efficiency (``avg_mfe / max(avg_mae, 1)`` — the ``_risk_efficiency`` law)
from the aggregate, each ``None → 0.0`` (the ``_f`` projection). Combos
outside that population carry an honest ``composite_score=None``.

## O() envelope (Golden Rule #10)

O(persisted finalists) oracle re-sims + one Stage-1A ``bar_bits`` rebuild per
tuple + one 1-minute index per instrument — bounded by top-k/tuple, run once
at run-end, NEVER on the ~33B-trial hot path (which receives ZERO diffs).
The pool read-back is one bounded Parquet read per tuple (the just-written or
stamped-current sidecars — one code path for fresh and resumed tuples alike).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, cast

import structlog

from brutex.engine.baseline import baseline_filename, compute_baseline, write_baseline
from brutex.engine.combo_aggregates import ComboAggregate, aggregate_combo
from brutex.engine.minute_path import MinuteIndex
from brutex.engine.reference_series import TradeReferenceStamper
from brutex.engine.results_store import ResultsStore
from brutex.engine.session_lens import build_session_lens
from brutex.engine.stage1a import build_bar_bits
from brutex.engine.sweep_lookahead import env_flag_tristate
from brutex.engine.sweep_orchestrator import iter_sweep_tuples, tuple_store_path
from brutex.engine.sweep_supervisor import grid_identity
from brutex.engine.top100_artifact import (
    DEFAULT_TOP100_PRESET,
    PoolCounts,
    Top100Candidate,
    build_top100,
    passes_hard_gates,
    write_top100,
)
from brutex.engine.trade_capture import capture_combo
from brutex.engine.trade_store import (
    SidecarPaths,
    load_stamp,
    sidecar_paths_for,
    sweep_run_dir,
    write_tuple_sidecars,
)
from brutex.engine.twin_detect import TwinCandidate, group_twins, trade_fingerprint
from brutex.models import BruteXError
from brutex.ranking import composite_scores

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from brutex.data.models import CandleBar
    from brutex.engine.results_store import ResultRow
    from brutex.engine.session_lens import SessionLens
    from brutex.engine.sweep_orchestrator import SweepGridConfig, SweepTuple
    from brutex.engine.top100_artifact import MarginRoiProvider, ReasonProvider

_LOG = structlog.get_logger("brutex.engine.results_depth_pass")

__all__ = [
    "RESULTS_DEPTH_ENV",
    "VIX_MISS_WARN_CAP",
    "ResultsDepthError",
    "ResultsDepthReport",
    "resolve_results_depth_enabled",
    "run_results_depth_pass",
]

#: The operator kill switch for the run-end pass (WP6's CLI adapter resolves
#: it BEFORE the sweep so junk can never fail an already-completed run).
RESULTS_DEPTH_ENV: Final[str] = "BRUTEX_RESULTS_DEPTH"

#: Reused install hint for the optional ``polars`` dependency.
_POLARS_HINT: Final[str] = (
    "results_depth_pass requires 'polars' — install with 'pip install -e .[data]' "
    "to enable the P2 data lake"
)

#: The composite term-column count (the COMMON ``N_COMPOSITE_TERMS`` shape).
_N_TERMS: Final[int] = 7

#: Per-combo ``results_depth.vix_miss`` WARN cap (gap-register Ruling 7 —
#: "capped vix_miss warns"; the house capped-20 idiom, the
#: ``_log_underlying_all_empty`` precedent). Misses past the cap stay COUNTED
#: (``vix_missing_trades``) — only the per-combo log line is suppressed.
VIX_MISS_WARN_CAP: Final[int] = 20

#: Per-tuple ``results_depth.missing_store`` INFO cap
#: (DEC-DEPTH-MISSING-STORE-LOUD-001, MIGRATION.md #1636 — the house
#: capped-20 idiom, the :data:`VIX_MISS_WARN_CAP` precedent). A grid tuple with
#: no persisted store on disk is COUNTED (``tuples_missing_store``) and, for the
#: first :data:`_MISSING_STORE_LOG_CAP` such tuples per pass, named in one INFO
#: line; later misses stay counted but the per-store log is suppressed.
_MISSING_STORE_LOG_CAP: Final[int] = 20

#: Cap on the ``missing_store_sample`` report tuple — a WARN without paths is
#: unactionable, so the aggregate surfaces the first few absent store paths.
_MISSING_STORE_SAMPLE_CAP: Final[int] = 10


class ResultsDepthError(BruteXError):
    """Raised on a broken pass invariant (unreadable sidecar the stamp vouched for)."""


def resolve_results_depth_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Resolve the :data:`RESULTS_DEPTH_ENV` kill switch — fail-loud on junk.

    unset / ``""`` → ``True`` (default-ON: the pass is the R21-R51 results
    surface; opt-in would silently ship a sweep with no depth artifacts);
    truthy/falsy tokens per the ONE sweep env vocabulary
    (:func:`brutex.engine.sweep_lookahead.env_flag_tristate` — never copied);
    anything else raises (the DEC-RAM-BUDGETS-001 fail-loud env convention).

    Parameters
    ----------
    env
        Injectable environment mapping (tests); ``None`` reads ``os.environ``.

    Raises
    ------
    ValueError
        If the value is set to something outside the two token sets.
    """
    mapping = os.environ if env is None else env
    raw = mapping.get(RESULTS_DEPTH_ENV, "")
    if not raw.strip():
        return True
    flag = env_flag_tristate(raw)
    if flag is None:
        raise ValueError(
            f"{RESULTS_DEPTH_ENV}={raw!r} is not a recognised switch value; expected one of "
            "'1', 'on', 'true', 'yes' (enable — the default when unset) or "
            "'0', 'off', 'false', 'no' (disable)"
        )
    return flag


@dataclass(frozen=True, slots=True)
class ResultsDepthReport:
    """The pass's honest counters (design A.10 + additive resume/pool fields).

    ``tuples_processed`` counts FRESH sidecar builds; ``tuples_skipped_current``
    the idempotent-resume skips (stamp matches the current store bytes);
    ``tuples_missing_store`` grid tuples with no store on disk (not swept /
    quarantined); ``tuples_errored`` per-tuple fence trips. Capture counters
    (``combos_*`` / ``vix_missing_trades``) cover FRESH builds only — a
    skipped tuple's captures were counted by the run that built them. Pool
    counters (``pool`` / ``gate_passers`` / ``excluded_*`` / ``parity_excluded``
    / ``families_collapsed``) cover the WHOLE grid (fresh + resumed).
    ``missing_store_sample`` (additive, capped
    :data:`_MISSING_STORE_SAMPLE_CAP`) carries the first absent BARS-PRESENT
    finalist-store paths so a missing-store WARN is actionable
    (DEC-DEPTH-MISSING-STORE-LOUD-001, MIGRATION.md #1636).
    ``missing_expected_no_bars`` (additive, review round 1 M2) is the
    DETERMINISTICALLY-explained subset of ``tuples_missing_store``: the
    tuple's own bar slice is EMPTY, so the sweep can never have written a
    store — resume or not — and it NEVER counts toward the unexplained WARN.
    ``missing_store_unexplained`` (additive) is the pass's OWN computed
    ``max(0, (tuples_missing_store - missing_expected_no_bars) -
    (expected_missing or 0))`` so the CLI adapter mirrors the SAME number
    instead of re-deriving the formula.
    """

    tuples_processed: int
    tuples_skipped_current: int
    tuples_missing_store: int
    tuples_errored: int
    combos_captured: int
    combos_parity_failed: int
    combos_zero_trades: int
    vix_missing_trades: int
    baselines_written: int
    pool: int
    gate_passers: int
    excluded_no_aggs: int
    zero_trade_excluded: int
    parity_excluded: int
    families_collapsed: int
    top100_written: bool
    top100_entries: int
    missing_store_sample: tuple[str, ...] = ()
    missing_expected_no_bars: int = 0
    missing_store_unexplained: int = 0


@dataclass(frozen=True, slots=True)
class _InstrumentContext:
    """Per-instrument reference context, built once and reused across tuples."""

    minute_index: MinuteIndex | None
    lens: SessionLens


@dataclass(slots=True)
class _Tallies:
    """Mutable pass counters (folded into the frozen report at the end)."""

    tuples_processed: int = 0
    tuples_skipped_current: int = 0
    tuples_missing_store: int = 0
    missing_expected_no_bars: int = 0
    tuples_errored: int = 0
    combos_captured: int = 0
    combos_parity_failed: int = 0
    combos_zero_trades: int = 0
    vix_missing_trades: int = 0
    vix_miss_combos: int = 0
    baselines_written: int = 0
    excluded_no_aggs: int = 0
    zero_trade_excluded: int = 0
    parity_excluded: int = 0
    stores_pooled: int = 0
    missing_store_logged: int = 0
    missing_store_sample: list[str] = field(default_factory=list)


def _store_sha256(store_path: Path) -> str:
    """The CURRENT store bytes' sha256 — the sidecar stamp's idempotence key."""
    return hashlib.sha256(store_path.read_bytes()).hexdigest()


#: The µs-since-epoch anchor for the reference-bar digest (deterministic; a
#: minute bar's timestamp is tz-aware UTC so the delta arithmetic is exact).
_EPOCH_UTC: Final = datetime(1970, 1, 1, tzinfo=UTC)


def _bar_ts_us(ts: datetime) -> int:
    """Exact integer µs-since-epoch for a bar timestamp (no float rounding).

    Fail-loud on a tz-NAIVE timestamp (review round 1 item 7): a naive
    ``astimezone(UTC)`` interprets the value in the MACHINE's local zone, so
    the digest — and therefore the resume decision — would silently vary by
    host. Bars are stored tz-aware UTC; a naive one is a caller bug
    (mirroring the ``_as_utc`` defensive posture — raise, never guess).
    """
    if ts.tzinfo is None:
        raise ValueError(f"naive bar timestamp (tz-aware UTC required): {ts!r}")
    delta = ts.astimezone(UTC) - _EPOCH_UTC
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _bars_sha256(bars: Sequence[CandleBar]) -> str:
    """Full-content sha256 over a bar stream's 7 canonical fields, in order.

    DEC-DEPTH-STAMP-REFDATA-001 (MIGRATION.md #1637). Empty/absent series folds
    the literal token ``"absent"`` so a null-VIX pass and a real-VIX pass differ
    (forcing a re-derive) while two absent-vs-absent series match (no gratuitous
    re-derive). Full-content — a count+endpoints fingerprint can false-match an
    interior change (the DEC-SNAPSHOT-HASH-FULL-001 house law). This is
    deliberately NOT the identity-bearing ``stage3_wire.data_snapshot_hash`` (a
    run-END idempotence gate, a different hash primitive for a different,
    non-identity purpose — same full-content streaming discipline).
    """
    hasher = hashlib.sha256()
    if not bars:
        hasher.update(b"absent")
        return hasher.hexdigest()
    for bar in bars:
        hasher.update(str(_bar_ts_us(bar.timestamp)).encode("ascii"))
        hasher.update(b"\x1f")
        for price in (bar.open, bar.high, bar.low, bar.close):
            hasher.update(str(price).encode("ascii"))
            hasher.update(b"\x1f")
        hasher.update(str(bar.volume).encode("ascii"))
        hasher.update(b"\x1f")
        oi = bar.open_interest
        hasher.update(b"" if oi is None else str(oi).encode("ascii"))
        hasher.update(b"\x1e")
    return hasher.hexdigest()


def _warn_vix_miss(run_id: str, missing: int, tallies: _Tallies) -> None:
    """Capped per-combo VIX-miss WARN (gap-register Ruling 7).

    A combo whose capture has ≥1 trade missing a VIX stamp gets ONE
    ``results_depth.vix_miss`` WARN naming it — for the first
    :data:`VIX_MISS_WARN_CAP` such combos per pass. The cap boundary emits
    ONE ``results_depth.vix_miss_capped`` note; later misses stay silent in
    the log but ALWAYS counted (``vix_missing_trades`` on the report — NULL
    honesty preserved, the log volume bounded).
    """
    tallies.vix_miss_combos += 1
    if tallies.vix_miss_combos <= VIX_MISS_WARN_CAP:
        _LOG.warning(
            "results_depth.vix_miss",
            run_id=run_id,
            missing_trades=missing,
            note="trade(s) without a VIX stamp carry an honest NULL, never a silent zero",
        )
    elif tallies.vix_miss_combos == VIX_MISS_WARN_CAP + 1:
        _LOG.warning(
            "results_depth.vix_miss_capped",
            cap=VIX_MISS_WARN_CAP,
            note=(
                "further per-combo vix_miss warnings suppressed this pass — "
                "misses stay counted in the report's vix_missing_trades"
            ),
        )


def _note_missing_store(store_path: Path, tallies: _Tallies, *, no_bars: bool) -> None:
    """Count + (capped) log + (capped) sample a grid tuple with no store on disk.

    DEC-DEPTH-MISSING-STORE-LOUD-001 (MIGRATION.md #1636, review round 1 M2 —
    deterministic classification). A missing finalist store is DETERMINISTICALLY
    explained when the tuple's own bar slice is EMPTY (``no_bars=True``): a
    no-bars tuple never writes a store, on the first run AND on a checkpoint
    resume alike, so it counts ``missing_expected_no_bars`` + a capped INFO
    (``results_depth.missing_store_no_bars``) and NEVER feeds the unexplained
    WARN. A BARS-PRESENT missing store is the actionable class — capped INFO
    (``results_depth.missing_store``) + the first
    :data:`_MISSING_STORE_SAMPLE_CAP` paths sampled so the pass-end WARN + the
    adapter summary can NAME them (a WARN without paths is unactionable). The
    total (``tuples_missing_store``) ALWAYS increments for both classes; the
    per-store INFO volume is capped at :data:`_MISSING_STORE_LOG_CAP` across
    both. DISCLOSED COST (Golden Rule #10): classifying requires the caller to
    load the tuple's bars via the memoised ``bars_for`` seam — paid ONLY when a
    store is missing (zero lake reads on a clean run), at most once per
    ``(instrument, tf)`` thanks to the memo.
    """
    tallies.tuples_missing_store += 1
    if no_bars:
        tallies.missing_expected_no_bars += 1
        if tallies.missing_store_logged < _MISSING_STORE_LOG_CAP:
            tallies.missing_store_logged += 1
            _LOG.info(
                "results_depth.missing_store_no_bars",
                store=str(store_path),
                note="empty bar slice — the sweep deterministically never writes this store",
            )
        return
    if tallies.missing_store_logged < _MISSING_STORE_LOG_CAP:
        tallies.missing_store_logged += 1
        _LOG.info("results_depth.missing_store", store=str(store_path))
    if len(tallies.missing_store_sample) < _MISSING_STORE_SAMPLE_CAP:
        tallies.missing_store_sample.append(str(store_path))


def _probe_manifest(run_dir: Path) -> None:
    """Validate-before-read on P2's run manifest, if present (design C4).

    The pass consumes no manifest field today; the probe exists so a TORN
    manifest is surfaced loudly (``results_depth.manifest_invalid``) while an
    absent one degrades silently (the manifest is P2's artifact — may not
    exist yet). *run_dir* is the Ruling-4 per-run directory from the ONE
    :func:`brutex.engine.trade_store.sweep_run_dir` seam.
    """
    manifest = run_dir / "manifest.json"
    if not manifest.exists():
        return
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _LOG.warning("results_depth.manifest_invalid", path=str(manifest), reason="unreadable")
        return
    if not isinstance(payload, dict):
        _LOG.warning("results_depth.manifest_invalid", path=str(manifest), reason="not_object")
        return
    _LOG.info("results_depth.manifest_seen", path=str(manifest))


def _composites_for(
    rows_by_id: Mapping[str, ResultRow], aggs: Sequence[ComboAggregate]
) -> dict[str, float]:
    """Tuple-leaderboard composite scores via the COMMON weighted-z expression.

    Population = the tuple's parity-OK, non-zero-trade finalists in ``run_id``
    order (deterministic under any discovery order). Term inputs per the
    module docstring; the DSR 8th term is inactive (all zeros) — the pass
    ranks the PERSISTED leaderboard, whose DSR gate already ran in-sweep.
    """
    eligible = sorted(
        (agg for agg in aggs if agg.parity_ok and agg.n_trades > 0),
        key=lambda agg: agg.run_id,
    )
    if not eligible:
        return {}
    columns: list[list[float]] = [[] for _ in range(_N_TERMS)]
    for agg in eligible:
        row = rows_by_id[agg.run_id]
        recovery = agg.recovery_factor if agg.recovery_factor is not None else 0.0
        wlr = agg.win_loss_ratio if agg.win_loss_ratio is not None else 0.0
        # ``stats_live`` aggregates always carry the paisa averages; the
        # ``or 0`` legs are DEFENSIVE belts for a future schema relaxation.
        mfe_avg = agg.mfe_avg_paisa if agg.mfe_avg_paisa is not None else 0
        mae_avg = agg.mae_avg_paisa if agg.mae_avg_paisa is not None else 0
        terms = (
            row.sharpe_ratio,
            row.sortino_ratio,
            row.calmar_ratio,
            row.profit_factor - 1.0,
            recovery,
            row.win_rate * wlr,
            mfe_avg / max(mae_avg, 1),
        )
        for column, value in zip(columns, terms, strict=True):
            column.append(value)
    scores = composite_scores(columns, [0.0] * len(eligible))
    return {agg.run_id: score for agg, score in zip(eligible, scores, strict=True)}


def _build_tuple_sidecars(
    tuple_: SweepTuple,
    rows: Sequence[ResultRow],
    paths: SidecarPaths,
    *,
    grid: SweepGridConfig,
    bars: Sequence[CandleBar],
    context: _InstrumentContext,
    stamper: TradeReferenceStamper | None,
    store_hash: str,
    reference_fingerprint: str,
    tallies: _Tallies,
) -> None:
    """Capture + aggregate + persist one STALE tuple's sidecar trio."""
    bar_bits = build_bar_bits(
        bars, proximity=tuple_.proximity, prior_n=grid.prior_n, fib_anchor=tuple_.fib_anchor
    )
    captures = []
    aggs: list[ComboAggregate] = []
    for row in rows:
        capture = capture_combo(
            row,
            bars=bars,
            bar_bits=bar_bits,
            tf_minutes=tuple_.tf.minutes,
            sl_paisa=tuple_.sl_paisa,
            tp_paisa=tuple_.tp_paisa,
            minute_index=context.minute_index,
            stamper=stamper,
            start_equity_paisa=grid.start_equity_paisa,
        )
        if capture.parity_ok:
            tallies.combos_captured += 1
        else:
            tallies.combos_parity_failed += 1
        if row.total_trades == 0:
            tallies.combos_zero_trades += 1
        tallies.vix_missing_trades += capture.vix_missing_trades
        if capture.vix_missing_trades > 0:
            _warn_vix_miss(row.run_id, capture.vix_missing_trades, tallies)
        captures.append(capture)
        aggs.append(
            aggregate_combo(
                capture,
                row,
                lens=context.lens,
                composite_score=None,
                fingerprint=trade_fingerprint(capture.trades),
                start_equity_paisa=grid.start_equity_paisa,
            )
        )
    composites = _composites_for({row.run_id: row for row in rows}, aggs)
    aggs = [replace(agg, composite_score=composites.get(agg.run_id)) for agg in aggs]
    write_tuple_sidecars(
        paths,
        captures,
        [agg.to_record() for agg in aggs],
        store_content_hash=store_hash,
        reference_fingerprint=reference_fingerprint,
    )


def _load_agg_records(paths: SidecarPaths) -> list[ComboAggregate]:
    """Read the tuple's aggregate records back from its (validated) sidecar."""
    try:
        import polars as pl
    except ImportError as exc:
        raise ResultsDepthError(_POLARS_HINT) from exc
    try:
        df = pl.read_parquet(paths.aggs)
    except (pl.exceptions.PolarsError, OSError) as exc:
        raise ResultsDepthError(f"unreadable aggs sidecar at {paths.aggs}: {exc}") from exc
    return [ComboAggregate(**record) for record in df.iter_rows(named=True)]


def _entry_ts_sets(paths: SidecarPaths, run_ids: Sequence[str]) -> dict[str, frozenset[int]]:
    """Read the gate-passers' entry timestamps (µs) from the trades sidecar."""
    if not run_ids:
        return {}
    try:
        import polars as pl
    except ImportError as exc:
        raise ResultsDepthError(_POLARS_HINT) from exc
    try:
        df = (
            pl.scan_parquet(paths.trades)
            .select("run_id", "entry_ts_us")
            .filter(pl.col("run_id").is_in(list(run_ids)))
            .collect()
        )
    except (pl.exceptions.PolarsError, OSError) as exc:
        raise ResultsDepthError(f"unreadable trades sidecar at {paths.trades}: {exc}") from exc
    grouped: dict[str, set[int]] = {}
    for run_id, entry_ts_us in df.iter_rows():
        grouped.setdefault(str(run_id), set()).add(int(entry_ts_us))
    return {run_id: frozenset(values) for run_id, values in grouped.items()}


def _pool_tuple(
    tuple_: SweepTuple,
    rows: Sequence[ResultRow],
    paths: SidecarPaths,
    *,
    store_stem: str,
    tallies: _Tallies,
    pool: list[Top100Candidate],
    twins: list[TwinCandidate],
) -> None:
    """Fold one tuple's (row ⋈ agg) join into the global candidate pool.

    ONE code path for fresh AND resumed tuples: the pool is always read back
    from the on-disk sidecars, so what gets ranked is what was PERSISTED
    (write-then-verify, and a resumed tuple contributes identically).
    """
    agg_by_id = {agg.run_id: agg for agg in _load_agg_records(paths)}
    tuple_ref = f"{tuple_.instrument}/{tuple_.tf.label}/{store_stem}"
    candidates: list[Top100Candidate] = []
    for row in rows:
        agg = agg_by_id.get(row.run_id)
        if agg is None:
            tallies.excluded_no_aggs += 1
            continue
        if agg.zero_trades or row.total_trades == 0:
            tallies.zero_trade_excluded += 1
            continue
        if not agg.parity_ok:
            tallies.parity_excluded += 1
            continue
        candidates.append(
            Top100Candidate(
                row=row,
                agg=agg,
                sl_paisa=tuple_.sl_paisa,
                tp_paisa=tuple_.tp_paisa,
                tuple_ref=tuple_ref,
            )
        )
    passers = [c for c in candidates if passes_hard_gates(c.row, c.agg)]
    ts_sets = _entry_ts_sets(paths, [c.row.run_id for c in passers])
    for candidate in passers:
        run_id = candidate.row.run_id
        twins.append(
            TwinCandidate(
                run_id=run_id,
                mask_bits=candidate.row.mask_bits,
                fingerprint=candidate.agg.fingerprint,
                entry_ts_us=ts_sets.get(run_id, frozenset()),
            )
        )
    pool.extend(candidates)
    tallies.stores_pooled += 1


def run_results_depth_pass(
    *,
    out_root: Path,
    grid: SweepGridConfig,
    bars_for: Callable[[str, str], Sequence[CandleBar]],
    minute_bars_for: Callable[[str], Sequence[CandleBar] | None],
    daily_bars_for: Callable[[str], Sequence[CandleBar] | None] | None = None,
    vix_bars: Sequence[CandleBar] | None = None,
    preset_name: str = DEFAULT_TOP100_PRESET,
    reason_provider: ReasonProvider | None = None,
    margin_roi_provider: MarginRoiProvider | None = None,
    expected_missing: int | None = None,
) -> ResultsDepthReport:
    """Run the full results-depth pass over one grid's persisted stores.

    Parameters
    ----------
    out_root
        The sweep results root (``<lake>/sweeps`` — the tree
        ``run_persisted_sweep`` flushed into).
    grid
        The grid the sweep ran — the SAME config, so every tuple's store path
        + trade-sim knobs are rebuilt through the SAME public helpers the
        orchestrator used (imported, never re-derived).
    bars_for
        ``(instrument, tf_label) → bars`` — the SAME slices the sweep loaded
        (the ``run_one_tuple`` seam).
    minute_bars_for
        ``instrument → 1-minute bars`` for the R22 minute-path work, or
        ``None`` → every trade carries the honest ``no_1m_data`` status.
    daily_bars_for
        ``instrument → derived-1day bars`` for the R46 trend lens, or
        ``None`` (parameter or return) → an EMPTY lens (trend ``"unknown"``,
        zero event flags) — additive vs the design signature, documented.
    vix_bars
        The India VIX 1-minute feed (R31), or ``None`` → one loud WARN +
        every trade's VIX honestly ``None`` + counted.
    preset_name
        The top-100 ranking preset (unknown degrades to the default, logged).
    reason_provider, margin_roi_provider
        The WP3 seams, threaded to :func:`build_top100` (see that module).
    expected_missing
        The caller-known RUN-SCOPED failed/quarantined allowance
        (``tuples_failed + tuples_quarantined_prior`` from the sweep's own
        summary) — DEC-DEPTH-MISSING-STORE-LOUD-001 (MIGRATION.md #1636,
        review round 1 M2). The no-bars class is NOT part of this allowance:
        the pass classifies it DETERMINISTICALLY per missing store (an empty
        ``bars_for`` slice ⇒ the sweep never writes a store — true on the
        first run AND on a checkpoint resume whose prefix folded into
        ``skipped_resume``, the false-WARN class this closes). A BARS-PRESENT
        missing count beyond this allowance is UNEXPLAINED (partial store
        deletion or a foreign grid identity) and raises one
        ``results_depth.missing_store_unexplained`` WARN. ``None`` (the
        default — direct callers / unknown) treats EVERY bars-present missing
        store as unexplained (the fail-loud direction). HONEST residual
        heuristic (disclosed in the WARN note): the allowance is run-scoped —
        a deletion coinciding with PRIOR-run failures can be
        under-attributed. DISCLOSED COST: classifying a missing store loads
        that tuple's bars once via the memoised ``bars_for`` seam — paid only
        when a store is missing, zero on clean runs.

    Returns
    -------
    ResultsDepthReport
        The honest counters — loud failures included, never masked.
    """
    run_id = grid_identity(grid)
    # Ruling 4: EVERY run-end artifact path rides the ONE sweep_run_dir seam —
    # a SIBLING sweep_runs/ root beside the sweeps tree, never inside the
    # **/*.parquet-globbed tree (P2's run_layout.py rebinds this at
    # merge-integration; Ruling 9's logical run_id rebinds the run_id there).
    run_dir = sweep_run_dir(out_root, run_id)
    runs_dir = run_dir.parent
    _probe_manifest(run_dir)
    if vix_bars is None:
        stamper = None
        _LOG.warning(
            "results_depth.vix_absent",
            note="no India VIX bars supplied — every trade's VIX stamps will be NULL + counted",
        )
    else:
        stamper = TradeReferenceStamper.from_vix_bars(vix_bars)

    # DEC-DEPTH-STAMP-REFDATA-001 (#1637): the shared VIX digest is computed ONCE
    # per pass (a null-VIX pass folds the "absent" token, so a VIX feed pulled
    # AFTER the first stamp re-derives every instrument's tuples).
    vix_digest = _bars_sha256(vix_bars or ())

    tallies = _Tallies()
    contexts: dict[str, _InstrumentContext] = {}
    bars_cache: dict[tuple[str, str], Sequence[CandleBar]] = {}
    ref_fp_cache: dict[str, str] = {}
    pool: list[Top100Candidate] = []
    twins: list[TwinCandidate] = []
    instruments_pooled: list[str] = []

    def _bars(instrument: str, tf_label: str) -> Sequence[CandleBar]:
        key = (instrument, tf_label)
        if key not in bars_cache:
            bars_cache[key] = bars_for(instrument, tf_label)
        return bars_cache[key]

    def _context(instrument: str) -> _InstrumentContext:
        context = contexts.get(instrument)
        if context is not None:
            return context
        minute_bars = minute_bars_for(instrument)
        if minute_bars:
            minute_index: MinuteIndex | None = MinuteIndex.from_bars(minute_bars)
        else:
            minute_index = None
            _LOG.info(
                "results_depth.no_minute_data",
                instrument=instrument,
                note="1m MAE/MFE + intraday-path equity degrade to the honest no_1m_data status",
            )
        daily_bars = None if daily_bars_for is None else daily_bars_for(instrument)
        lens = build_session_lens(daily_bars if daily_bars else (), instrument=instrument)
        context = _InstrumentContext(minute_index=minute_index, lens=lens)
        contexts[instrument] = context
        return context

    def _reference_fingerprint(instrument: str) -> str:
        """Per-instrument reference-data fingerprint (memoised, #1637).

        Folds the shared VIX digest + this instrument's 1m/1day reference bars +
        the WP3 provider-presence flags. DISCLOSED cost (Golden Rule #10): a
        fully-current resume now invokes ``minute_bars_for``/``daily_bars_for``
        for the fp — and because this memo (``ref_fp_cache``) is SEPARATE from
        the ``_context`` memo, an instrument whose tuples also build a context
        loads its reference series up to TWICE on first touch (once per memo) —
        O(reference bars)/instrument/pass, run-end only, never on the
        ~33B-trial hot path.
        """
        cached = ref_fp_cache.get(instrument)
        if cached is not None:
            return cached
        minute_bars = minute_bars_for(instrument)
        daily_bars = None if daily_bars_for is None else daily_bars_for(instrument)
        # A provider going None→wired changes sidecar CONTENT; its CODE identity
        # stays code_commit_hash's lane (the honest limit, recorded in the DEC).
        flags = f"reasons={reason_provider is not None};margin={margin_roi_provider is not None}"
        fp = hashlib.sha256(
            "\x1f".join(
                (
                    "rdfp1",
                    vix_digest,
                    _bars_sha256(minute_bars or ()),
                    _bars_sha256(daily_bars or ()),
                    flags,
                )
            ).encode("utf-8")
        ).hexdigest()
        ref_fp_cache[instrument] = fp
        return fp

    for tuple_ in iter_sweep_tuples(grid):
        store_path = tuple_store_path(out_root, tuple_, grid)
        if not store_path.exists():
            # Review round 1 M2: classify deterministically — an EMPTY bar slice
            # means the sweep never writes this store (resume or not), so it is
            # expected-missing; only a bars-present miss is deletion-suspect.
            # Costs one memoised bars_for load, paid ONLY on a missing store.
            no_bars = not _bars(tuple_.instrument, tuple_.tf.label)
            _note_missing_store(store_path, tallies, no_bars=no_bars)
            continue
        try:
            store_hash = _store_sha256(store_path)
            paths = sidecar_paths_for(store_path, out_root, run_id=run_id)
            rows = ResultsStore.load(store_path).rows()
            stamp = load_stamp(paths)
            current_fp = _reference_fingerprint(tuple_.instrument)
            # DEC-DEPTH-STAMP-REFDATA-001 (#1637): a current tuple must match BOTH
            # the store bytes AND the reference fingerprint. An OLD stamp's None
            # fp can NEVER equal a hex digest → re-derive once, never a fabricated
            # match.
            if (
                stamp is not None
                and stamp.store_content_hash == store_hash
                and stamp.reference_fingerprint == current_fp
            ):
                tallies.tuples_skipped_current += 1
                _LOG.info(
                    "results_depth.tuple_skipped_current",
                    store=str(store_path),
                    run_ids=stamp.run_ids,
                )
            else:
                if (
                    stamp is not None
                    and stamp.store_content_hash == store_hash
                    and stamp.reference_fingerprint != current_fp
                ):
                    # Store bytes unchanged but the reference data moved (e.g. a
                    # VIX feed pulled after the first stamp) — a DISTINCT redo.
                    _LOG.info(
                        "results_depth.tuple_redo_reference_changed",
                        store=str(store_path),
                        old_fp=(
                            None
                            if stamp.reference_fingerprint is None
                            else stamp.reference_fingerprint[:12]
                        ),
                        new_fp=current_fp[:12],
                    )
                _build_tuple_sidecars(
                    tuple_,
                    rows,
                    paths,
                    grid=grid,
                    bars=_bars(tuple_.instrument, tuple_.tf.label),
                    context=_context(tuple_.instrument),
                    stamper=stamper,
                    store_hash=store_hash,
                    reference_fingerprint=current_fp,
                    tallies=tallies,
                )
                tallies.tuples_processed += 1
                _LOG.info(
                    "results_depth.tuple_done",
                    store=str(store_path),
                    rows=len(rows),
                )
            _pool_tuple(
                tuple_,
                rows,
                paths,
                store_stem=store_path.stem,
                tallies=tallies,
                pool=pool,
                twins=twins,
            )
            if tuple_.instrument not in instruments_pooled:
                instruments_pooled.append(tuple_.instrument)
        except (KeyboardInterrupt, SystemExit, MemoryError):
            raise
        except Exception as exc:
            tallies.tuples_errored += 1
            _LOG.error(
                "results_depth.tuple_error",
                store=str(store_path),
                error_type=type(exc).__name__,
                error=str(exc),
            )

    # ── Baselines (R27): per pooled instrument, from the sweep's own bars ──
    baselines: dict[str, str] = {}
    first_tf = grid.timeframes[0].label
    for instrument in instruments_pooled:
        try:
            series = compute_baseline(_bars(instrument, first_tf), instrument=instrument)
            write_baseline(
                run_dir / baseline_filename(instrument),
                series,
                data_snapshot_hash=grid.data_snapshot_hash,
            )
        except (KeyboardInterrupt, SystemExit, MemoryError):
            raise
        except Exception as exc:
            _LOG.error(
                "results_depth.baseline_error",
                instrument=instrument,
                error_type=type(exc).__name__,
                error=str(exc),
            )
        else:
            baselines[instrument] = baseline_filename(instrument)
            tallies.baselines_written += 1

    # ── The global top-100 (R21/R26/R34), in its own fence ──
    top100_written = False
    top100_entries = 0
    gate_passers = len(twins)
    families_collapsed = 0
    try:
        families = group_twins(twins)
        artifact = build_top100(
            pool,
            run_id=run_id,
            preset=preset_name,
            families=families,
            counts=PoolCounts(
                stores=tallies.stores_pooled,
                excluded_no_aggs=tallies.excluded_no_aggs,
                zero_trade_excluded=tallies.zero_trade_excluded,
                parity_excluded=tallies.parity_excluded,
            ),
            data_snapshot_hashes=(grid.data_snapshot_hash,),
            baselines=baselines,
            reason_provider=reason_provider,
            margin_roi_provider=margin_roi_provider,
        )
        write_top100(runs_dir, artifact)
        top100_written = True
        top100_entries = len(artifact.entries)
        # build_top100 stamps this key as an int by construction.
        families_collapsed = cast("int", artifact.source["families_collapsed"])
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except Exception as exc:
        _LOG.error(
            "results_depth.top100_error",
            run_id=run_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )

    # DEC-DEPTH-MISSING-STORE-LOUD-001 (#1636, review round 1 M2): a no-bars
    # tuple is deterministically storeless (classified above — resume-safe);
    # a BARS-PRESENT missing store beyond the caller's run-scoped
    # failed/quarantined allowance is UNEXPLAINED (partial store deletion or a
    # foreign grid identity) and is NEVER silent again — one WARN naming the
    # excess + a path sample.
    missing_with_bars = tallies.tuples_missing_store - tallies.missing_expected_no_bars
    unexplained_missing = max(0, missing_with_bars - (expected_missing or 0))
    if unexplained_missing > 0:
        _LOG.warning(
            "results_depth.missing_store_unexplained",
            missing=tallies.tuples_missing_store,
            missing_no_bars=tallies.missing_expected_no_bars,
            expected=expected_missing,
            unexplained=unexplained_missing,
            sample=tuple(tallies.missing_store_sample),
            note=(
                "bars-present finalist stores absent beyond the sweep's own "
                "failed/quarantined allowance — partial store deletion or a "
                "foreign grid identity (the allowance is run-scoped: a deletion "
                "coinciding with prior-run failures can be under-attributed); "
                "run brutex sweep-verify --results-root <root>"
            ),
        )

    report = ResultsDepthReport(
        tuples_processed=tallies.tuples_processed,
        tuples_skipped_current=tallies.tuples_skipped_current,
        tuples_missing_store=tallies.tuples_missing_store,
        tuples_errored=tallies.tuples_errored,
        combos_captured=tallies.combos_captured,
        combos_parity_failed=tallies.combos_parity_failed,
        combos_zero_trades=tallies.combos_zero_trades,
        vix_missing_trades=tallies.vix_missing_trades,
        baselines_written=tallies.baselines_written,
        pool=len(pool),
        gate_passers=gate_passers,
        excluded_no_aggs=tallies.excluded_no_aggs,
        zero_trade_excluded=tallies.zero_trade_excluded,
        parity_excluded=tallies.parity_excluded,
        families_collapsed=families_collapsed,
        top100_written=top100_written,
        top100_entries=top100_entries,
        missing_store_sample=tuple(tallies.missing_store_sample),
        missing_expected_no_bars=tallies.missing_expected_no_bars,
        missing_store_unexplained=unexplained_missing,
    )
    _LOG.info(
        "results_depth.pass_done",
        run_id=run_id,
        tuples_processed=report.tuples_processed,
        tuples_skipped_current=report.tuples_skipped_current,
        tuples_missing_store=report.tuples_missing_store,
        tuples_errored=report.tuples_errored,
        combos_captured=report.combos_captured,
        combos_parity_failed=report.combos_parity_failed,
        pool=report.pool,
        gate_passers=report.gate_passers,
        top100_written=report.top100_written,
        top100_entries=report.top100_entries,
    )
    return report
