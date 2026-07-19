"""Stage-4 cascade **wiring** — re-build the cascade inputs from a persisted sweep
and run the nine-filter cascade on its top-K winners (DEC-CASCADE-WIRE-001).

The pure orchestrator in :mod:`brutex.metrics.cascade` (``run_stage4_cascade``)
consumes hand-built :class:`~brutex.metrics.cascade.CandidateInput`\\s; its
docstring explicitly deferred *"the production series-regeneration adapter
(trade-sim + lake)"* as out of scope. **This module is that adapter** — the seam
that turns a finished sweep's persisted winners back into ``CandidateInput``\\s by
re-running the Stage-2 trade-sim on the same bars, then runs the cascade.

It is consumed two ways (both go through the SAME pure core
:func:`run_cascade_over_specs`, so the AUTOMATIC and the MANUAL paths produce a
**byte-identical** report — the golden contract of this PR):

* **AUTOMATIC** — the sweep launcher (``brutex sweep-all``) calls
  :func:`run_and_write_from_specs` after the grid completes, building the
  :class:`CascadeManifest` from the grid + the persisted per-tuple stores and
  writing both ``cascade/manifest.json`` and ``cascade/cascade-report.json``.
* **MANUAL** — ``brutex cascade <run-id>`` (and the dashboard "Run cascade"
  button) call :func:`run_and_write_from_manifest`, which reloads the manifest
  this sweep wrote, re-reads the same bars from the lake, and re-runs the cascade
  → the same report bytes.

Why a manifest (Golden Rule #1, #4)
-----------------------------------
A persisted :class:`~brutex.engine.results_store.ResultRow` carries the winning
``mask_bits`` + the ``instrument``/``tf``/``direction`` context, but NOT the
tuple's ``sl``/``tp``/``prior_n``/``proximity``/``start_equity`` — those are
folded into the BLAKE3 ``run_id`` but never stored as columns. Re-simulating the
exact trades needs them, so the AUTOMATIC path (which still holds the
:class:`~brutex.engine.sweep_orchestrator.SweepGridConfig`) records a manifest of
:class:`CascadeTupleSpec`\\s; the MANUAL path replays it. The manifest is the
single source of truth both paths consume — no parameter is ever re-derived or
guessed.

Per-``(instrument, tf)`` pooling (honest envelope, Golden Rule #10)
-------------------------------------------------------------------
The cascade's pool filters (Reality-Check / PBO / Romano-Wolf) require every
pooled candidate to share ONE returns length — they stack the survivors into a
common ``(n_obs, n)`` matrix. Per-bar return series are only equal-length within
one ``(instrument, tf)`` slice (same bars), so the cascade is run **once per
slice** (the spec's per-leaderboard pool, ``TRACK1_BACKTEST_SPEC.md`` §"Stage 4")
and the per-slice reports are aggregated into one :class:`CascadeWireReport`.
Pooling across instruments/timeframes (different bar counts) would crash the
pool matrix and is deliberately NOT done.

Reconstruction conventions (documented, not hidden)
---------------------------------------------------
For each winning mask the trades are re-simulated (next-bar-open fills + SL/TP +
15:20 forced exit, the golden-locked :func:`~brutex.engine.trade_sim.simulate_trades`)
and the :class:`~brutex.metrics.cascade.CandidateInput` is filled as:

* scalar metrics (Sharpe / Sortino / Calmar / profit-factor / win-rate /
  win-loss / recovery / max-DD% / consecutive-losses / skew / kurtosis) — from
  :func:`brutex.metrics.compute.compute_metrics` (the per-**trade** moments),
  a ``None`` (mathematically-undefined) value mapped to ``0.0`` (the same
  documented choice as :func:`brutex.engine.sweep_run.ranked_combo_to_result_row`).
  The one exception is ``kurtosis``: ``compute_metrics`` stores **excess**
  kurtosis (Gaussian ``0``) but :class:`~brutex.metrics.cascade.CandidateInput`
  (and the DSR PSR radicand it feeds) wants the **non-excess** form (Gaussian
  ``3``), so :data:`~brutex.metrics.deflated_sharpe.GAUSSIAN_KURTOSIS` is added
  back here — exactly as the sibling
  :func:`brutex.metrics.compute.with_stage4_statistics` does
  (DEC-DSR-KURTOSIS-FIX-001);
* ``returns`` — the **per-bar** change in the realised-equity curve as a
  fraction of the notional base (the equal-length pool axis for CPCV / WFO / DSR
  and the SPA/RW/PBO matrix);
* ``lockbox_returns`` / ``lockbox_days`` — the same per-bar series plus each
  bar's IST trading-day ordinal (so the held-out window slices by day);
  ``lockbox_trade_days`` — the TRUE per-trade exit-day IST ordinals
  (DEC-STATS-GATE-INTEGRITY-001, R2-SR-F2), so filter #8's 500-trade floor +
  adaptive window extension count real trades, never bars;
  ``training_sharpe`` / ``training_max_drawdown`` are the per-bar Sharpe +
  drawdown (return units) over the TRAIN COMPLEMENT — the bars strictly
  BEFORE the lockbox window start resolved by the SHARED
  :func:`brutex.metrics.lockbox.resolve_lockbox_window` (R2-SR-F1: before
  this fix the FULL series — holdout tail included — was fed as the training
  reference, so filter #8's degradation floor compared the tail against a
  reference containing the tail, anti-conservative). Honest envelope: when
  the complement's Sharpe is undefined (< 2 bars OR zero variance — e.g.
  every trade exits inside the lockbox window) the reference falls back to
  the legacy FULL-series pair as ONE unit, logged loudly
  (``cascade_wire.training_ref_fallback``) — decontamination does NOT hold
  for fallback candidates;
* ``mc_evaluate`` — the injected exit logic for filter #9: relocate the entry
  signals to the given bar positions, re-run the SAME trade-sim, return the
  per-trade Sharpe (comparable to the observed per-trade ``sharpe_ratio``);
  an undefined Sharpe returns ``nan`` (treated as "did not beat").

This is offline Stage-4 batch work, ``O(K)`` re-simulations over the top-K
winners — never an ``O(1)`` per-bar hot path; no O(1)-total claim is made.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

import structlog

from brutex.data.atomic_io import atomic_write_simple
from brutex.data.futures_series import is_futures_series_symbol
from brutex.execution.fill_mode import FillMode
from brutex.metrics.deflated_sharpe import GAUSSIAN_KURTOSIS
from brutex.metrics.lockbox import (
    LOCKBOX_DEFAULT_MIN_TRADES,
    LOCKBOX_DEFAULT_WINDOW_DAYS,
    resolve_lockbox_window,
)
from brutex.models import BruteXError
from brutex.strategies.types import (
    FibAnchorKind,
    PriceSource,
    price_source_params_fragment,
)
from brutex.vocabulary.bits import TOTAL_CONDITIONS

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Callable, Mapping, Sequence
    from datetime import datetime
    from pathlib import Path

    import numpy as np
    import numpy.typing as npt

    from brutex.data.catalog_reconcile import ReconcileResult
    from brutex.data.models import CandleBar
    from brutex.engine.results_store import ResultRow
    from brutex.engine.sweep_orchestrator import SweepGridConfig, SweepTuple
    from brutex.engine.trial_stats import LeaderboardTrialStats
    from brutex.metrics.cascade import CandidateInput, CascadeConfig, CascadeReport
    from brutex.metrics.types import ClosedTrade

_LOG = structlog.get_logger("brutex.engine.cascade_wire")

#: Directory (under a sweep results-root) holding the cascade manifest + report.
CASCADE_DIRNAME: Final[str] = "cascade"
#: The manifest the AUTOMATIC path writes + the MANUAL path replays.
MANIFEST_FILENAME: Final[str] = "manifest.json"
#: The serialised cascade report (auto path == manual path, byte-for-byte).
REPORT_FILENAME: Final[str] = "cascade-report.json"
#: Manifest schema version (bump on any incompatible field change).
MANIFEST_VERSION: Final[int] = 1
#: Report schema version. ADDITIVE-AT-VERSION-1 (DEC-PBO-SIDECAR-001): the
#: per-group ``pool_pbo`` key was added without a bump — purely additive, no
#: reader rejects unknown keys, and a pre-sidecar report (key ABSENT) stays
#: distinguishable from a measured skip (key ``null``) — the same precedent as
#: the manifest's additive ``stage3`` block (see ``manifest_from_json``). The
#: per-group ``pool_rc_pvalue``/``pool_spa_pvalue`` keys and the per-verdict
#: ``candidate_pbo`` key (DEC-CASCADE-STAT-TIER-WIRE-001) ride the SAME
#: additive-at-1 precedent: raw full-precision floats or ``null``; a
#: pre-change report simply lacks the keys (readers None-fill).
REPORT_VERSION: Final[int] = 1

#: 64-char lowercase-hex BLAKE3 run-identity length (mirrors COMBO_HASH_HEX_LEN).
_RUN_ID_LEN: Final[int] = 64
#: Largest valid strategy mask — every defined condition bit
#: (0..TOTAL_CONDITIONS-1) all set. Tracks the manifest (mirrors
#: ``results_store.MASK_BITS_MAX``); a bit >= TOTAL_CONDITIONS is rejected.
_MASK_BITS_MAX: Final[int] = (1 << TOTAL_CONDITIONS) - 1
#: Modulus keeping the sweep-id-derived seed a non-negative 31-bit int.
_SEED_MODULUS: Final[int] = 2**31
#: The three proximity tolerances ``(pivot, pdh_pdl, fib)`` carried per tuple.
_PROXIMITY_LEN: Final[int] = 3
#: A family coordinate is a ``(sl_paisa, tp_paisa)`` PAIR
#: (DEC-CASCADE-STAT-TIER-WIRE-001).
_FAMILY_PAIR_LEN: Final[int] = 2
#: Minimum family width for the per-candidate CSCV columns — CSCV needs >= 2
#: strategies to rank (mirrors ``cascade._MIN_POOL_FOR_PBO``); a 1-pair
#: family builds no columns (the check is honestly skipped).
_MIN_FAMILY_COLUMNS: Final[int] = 2


class CascadeWireError(BruteXError):
    """Raised for any cascade-wiring failure (bad manifest / spec / missing file)."""


# ── Manifest data model ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CascadeTupleSpec:
    """One grid tuple's cascade inputs — its params + the winning ``(run_id, mask)``\\s.

    Carries everything :func:`build_candidate_input` needs to re-simulate the
    tuple's winners (the params are NOT recoverable from a stored
    :class:`~brutex.engine.results_store.ResultRow`).

    Parameters
    ----------
    instrument, tf_label, direction_label
        Context labels; non-empty. ``direction_label`` is ``"bull"`` / ``"bear"``.
    tf_minutes
        Bar timeframe in whole minutes (``>= 1``).
    sl_paisa, tp_paisa
        Stop-loss / take-profit distances in paisa (``> 0``), or ``None``.
    prior_n
        Prior-bar pattern look-back (``>= 1``).
    proximity_bps
        ``(pivot, pdh_pdl, fib)`` proximity tolerances in bps, or ``None`` for the
        Stage-1A default.
    start_equity_paisa
        Notional capital base (paisa, ``> 0``).
    winners
        The tuple's finalists as ``(run_id, mask)`` pairs; each ``run_id`` is
        64-char lowercase hex, each ``mask`` in ``[0, _MASK_BITS_MAX]``.
    fib_anchor
        The swept Fib-anchor family the tuple was evaluated against
        (DEC-NIFTY-FIB-AXIS-001). Threaded into :func:`candidates_for_spec`'s
        ``build_bar_bits`` so the Stage-4 cascade re-scores a non-prev-day
        survivor against its OWN Fib levels, not the prev-day default. An
        additive field (default prev-day) so a pre-axis manifest round-trips
        byte-compatibly; the default keeps the prev-day path byte-identical.
    vwap_source, ema_source
        The swept VWAP / EMA price-source cell the tuple was evaluated against
        (DEC-NIFTY-PRICE-SOURCE-AXIS-001). Threaded into
        :func:`candidates_for_spec`'s ``build_bar_bits`` so the Stage-4 cascade
        re-scores a non-default survivor against ITS OWN source's bits (52-53 /
        0-5). Additive fields (default ``(HLC3, CLOSE)``) — DEFAULT-OMITTED from
        the manifest so a pre-axis manifest round-trips byte-compatibly.
    family_sl_tp
        The tuple's Stage-3 search FAMILY (DEC-CASCADE-STAT-TIER-WIRE-001):
        the unique ``(sl_paisa, tp_paisa)`` configurations the winners' mask
        was actually TRIED at during the Stage-3 refinement — sorted
        ascending (the deterministic persisted order), the spec's OWN
        ``(sl_paisa, tp_paisa)`` pair included. Drives the per-candidate
        CSCV/PBO check at filter #5 (:func:`candidates_for_spec` re-simulates
        one return column per pair). ``None`` (the default) = family
        unavailable — a pure Stage-2 grid spec, a pre-family manifest, or a
        refined cell whose winner masks carry DIFFERING families the
        spec-level field cannot express (skipped LOUDLY at the spec build) —
        the per-candidate PBO is then honestly skipped. Additive +
        EMIT-ON-PRESENT in the manifest so a family-less manifest round-trips
        byte-compatibly.

    Raises
    ------
    CascadeWireError
        On an empty label, a non-positive knob, a malformed run_id, an
        out-of-range mask, or a malformed ``family_sl_tp`` (empty /
        non-positive legs / duplicate pairs / unsorted / missing the spec's
        own pair / present while a leg is ``None``).
    """

    instrument: str
    tf_label: str
    tf_minutes: int
    direction_label: str
    sl_paisa: int | None
    tp_paisa: int | None
    prior_n: int
    proximity_bps: tuple[int, int, int] | None
    start_equity_paisa: int
    winners: tuple[tuple[str, int], ...]
    fib_anchor: FibAnchorKind = FibAnchorKind.PREV_DAY
    vwap_source: PriceSource = PriceSource.HLC3
    ema_source: PriceSource = PriceSource.CLOSE
    family_sl_tp: tuple[tuple[int, int], ...] | None = None

    def __post_init__(self) -> None:
        for name in ("instrument", "tf_label", "direction_label"):
            if not getattr(self, name):
                raise CascadeWireError(f"CascadeTupleSpec.{name} must be non-empty")
        if self.direction_label not in ("bull", "bear"):
            raise CascadeWireError(
                f"CascadeTupleSpec.direction_label must be 'bull' or 'bear', "
                f"got {self.direction_label!r}"
            )
        if self.tf_minutes < 1:
            raise CascadeWireError(
                f"CascadeTupleSpec.tf_minutes must be >= 1, got {self.tf_minutes}"
            )
        for name in ("sl_paisa", "tp_paisa"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise CascadeWireError(f"CascadeTupleSpec.{name} must be > 0 or None, got {value}")
        if self.prior_n < 1:
            raise CascadeWireError(f"CascadeTupleSpec.prior_n must be >= 1, got {self.prior_n}")
        if self.start_equity_paisa <= 0:
            raise CascadeWireError(
                f"CascadeTupleSpec.start_equity_paisa must be > 0, got {self.start_equity_paisa}"
            )
        if self.proximity_bps is not None and len(self.proximity_bps) != _PROXIMITY_LEN:
            raise CascadeWireError(
                f"CascadeTupleSpec.proximity_bps must be a 3-tuple or None, got {self.proximity_bps!r}"
            )
        for run_id, mask in self.winners:
            if len(run_id) != _RUN_ID_LEN or run_id != run_id.lower():
                raise CascadeWireError(
                    f"CascadeTupleSpec winner run_id must be {_RUN_ID_LEN}-char lowercase hex, "
                    f"got {run_id!r}"
                )
            if not 0 <= mask <= _MASK_BITS_MAX:
                raise CascadeWireError(
                    f"CascadeTupleSpec winner mask {mask} out of range [0, {_MASK_BITS_MAX}]"
                )
        if self.family_sl_tp is not None:
            if not self.family_sl_tp:
                raise CascadeWireError("CascadeTupleSpec.family_sl_tp must be non-empty or None")
            if self.sl_paisa is None or self.tp_paisa is None:
                raise CascadeWireError(
                    "CascadeTupleSpec.family_sl_tp requires concrete sl_paisa/tp_paisa — "
                    "the spec's own pair must be a family member (a disabled leg has no "
                    "family coordinate)"
                )
            for pair in self.family_sl_tp:
                if len(pair) != _FAMILY_PAIR_LEN or pair[0] <= 0 or pair[1] <= 0:
                    raise CascadeWireError(
                        f"CascadeTupleSpec.family_sl_tp pairs must be positive "
                        f"(sl_paisa, tp_paisa) 2-tuples, got {pair!r}"
                    )
            if len(set(self.family_sl_tp)) != len(self.family_sl_tp):
                raise CascadeWireError("CascadeTupleSpec.family_sl_tp pairs must be unique")
            if list(self.family_sl_tp) != sorted(self.family_sl_tp):
                raise CascadeWireError(
                    "CascadeTupleSpec.family_sl_tp must be sorted ascending "
                    "(the deterministic persisted order)"
                )
            if (self.sl_paisa, self.tp_paisa) not in self.family_sl_tp:
                raise CascadeWireError(
                    "CascadeTupleSpec.family_sl_tp must include the spec's own "
                    f"(sl_paisa, tp_paisa) pair ({self.sl_paisa}, {self.tp_paisa})"
                )


@dataclass(frozen=True, slots=True)
class BarsContext:
    """The lake window a sweep ran over — so the MANUAL path reloads the same bars.

    Parameters
    ----------
    exchange, segment, start, end
        The lake partition labels + ISO window the sweep loaded; each non-empty.

    Raises
    ------
    CascadeWireError
        If any field is empty.
    """

    exchange: str
    segment: str
    start: str
    end: str

    def __post_init__(self) -> None:
        for name in ("exchange", "segment", "start", "end"):
            if not getattr(self, name):
                raise CascadeWireError(f"BarsContext.{name} must be non-empty")


@dataclass(frozen=True, slots=True)
class Stage3CellRecord:
    """One Stage-3 refined quantized cell recorded in the manifest (DEC-STAGE3-WIRE-001).

    Identifies which manifest specs are UNGATED Stage-3 refined cells (vs gated
    Stage-2 grid cells): the cell's coordinates as the launcher constructed its
    :class:`~brutex.engine.sweep_orchestrator.SweepTuple` (``k = popcount(mask)``,
    on-grid int SL/TP legs — review F3).

    Parameters
    ----------
    instrument, tf_label, direction_label
        Context labels; non-empty, ``direction_label`` is ``"bull"``/``"bear"``.
    k
        The refined mask's popcount (``>= 0``).
    sl_paisa, tp_paisa
        The quantized Mode-A cell legs (``> 0`` — refined cells never carry a
        disabled leg).
    proximity_bps
        ``(pivot, pdh_pdl, fib)`` proximity tolerances in bps, or ``None``.
    rel_path
        The cell store's path RELATIVE to the results root
        (DEC-ROW-PROVENANCE-001) — the machine-readable attribution the
        read-back uses to label refined rows by SOURCE FILE (coordinates
        alone cannot disambiguate: a grid tuple and a refined cell can share
        instrument/tf/direction/k, and ``ResultRow`` carries no SL/TP).
        ``None`` for manifests written before the provenance field (the
        additive-at-version-1 parse precedent) — readers fall back to the
        blanket disclaimer.

    Raises
    ------
    CascadeWireError
        On an empty label, a bad direction, a negative ``k``, a non-positive
        leg, a malformed proximity tuple, or an absolute/empty ``rel_path``.
    """

    instrument: str
    tf_label: str
    direction_label: str
    k: int
    sl_paisa: int
    tp_paisa: int
    proximity_bps: tuple[int, int, int] | None
    rel_path: str | None = None
    #: The swept Fib-anchor family this refined cell was evaluated against
    #: (DEC-NIFTY-FIB-AXIS-001). An additive field (default prev-day) so a
    #: pre-axis manifest parses byte-compatibly (the ``rel_path`` precedent); it
    #: must round-trip so a non-prev-day refined cell's ``run_id`` re-derives on
    #: verification. Stored as the string enum value.
    fib_anchor: FibAnchorKind = FibAnchorKind.PREV_DAY
    #: The swept VWAP / EMA price-source cell this refined cell was evaluated
    #: against (DEC-NIFTY-PRICE-SOURCE-AXIS-001). Additive fields (default
    #: ``(HLC3, CLOSE)``) so a pre-axis / baseline manifest parses byte-compatibly
    #: (the ``fib_anchor`` precedent); they must round-trip so a non-default cell's
    #: ``run_id`` re-derives on verification. Stored as the string enum values.
    vwap_source: PriceSource = PriceSource.HLC3
    ema_source: PriceSource = PriceSource.CLOSE

    def __post_init__(self) -> None:
        for name in ("instrument", "tf_label"):
            if not getattr(self, name):
                raise CascadeWireError(f"Stage3CellRecord.{name} must be non-empty")
        if self.direction_label not in ("bull", "bear"):
            raise CascadeWireError(
                f"Stage3CellRecord.direction_label must be 'bull' or 'bear', "
                f"got {self.direction_label!r}"
            )
        if self.k < 0:
            raise CascadeWireError(f"Stage3CellRecord.k must be >= 0, got {self.k}")
        for name in ("sl_paisa", "tp_paisa"):
            if getattr(self, name) <= 0:
                raise CascadeWireError(
                    f"Stage3CellRecord.{name} must be > 0, got {getattr(self, name)}"
                )
        if self.proximity_bps is not None and len(self.proximity_bps) != _PROXIMITY_LEN:
            raise CascadeWireError(
                f"Stage3CellRecord.proximity_bps must be a 3-tuple or None, "
                f"got {self.proximity_bps!r}"
            )
        if self.rel_path is not None and (
            not self.rel_path or PurePosixPath(self.rel_path).is_absolute()
        ):
            raise CascadeWireError(
                f"Stage3CellRecord.rel_path must be a non-empty relative path or None, "
                f"got {self.rel_path!r}"
            )


@dataclass(frozen=True, slots=True)
class Stage3ManifestRecord:
    """The manifest's Stage-3 provenance record (DEC-STAGE3-WIRE-001, review F3).

    Records whether the sweep ran the Stage-3 refinement pass, the
    content-derived snapshot hash of the lockbox-excluded training slice the
    refined rows were evaluated on (ADR-0007 Amendment A1), and the refined
    cells whose specs joined the cascade input.

    Parameters
    ----------
    enabled
        Whether ``--stage3-enable`` was active for this sweep.
    train_snapshot_hash
        The train-slice snapshot hash; required when enabled, forbidden when
        disabled.
    refined_cells
        The refined cells, in deterministic order; must be empty when disabled.

    Raises
    ------
    CascadeWireError
        If enabled without a hash, or disabled with a hash / cells.
    """

    enabled: bool
    train_snapshot_hash: str | None
    refined_cells: tuple[Stage3CellRecord, ...]

    def __post_init__(self) -> None:
        if self.enabled and not self.train_snapshot_hash:
            raise CascadeWireError(
                "Stage3ManifestRecord: enabled=True requires a train_snapshot_hash"
            )
        if not self.enabled and (self.train_snapshot_hash is not None or self.refined_cells):
            raise CascadeWireError(
                "Stage3ManifestRecord: enabled=False forbids a train_snapshot_hash / cells"
            )


@dataclass(frozen=True, slots=True)
class CascadeManifest:
    """The full replayable cascade input set written by the AUTOMATIC path.

    Parameters
    ----------
    version
        Manifest schema version (:data:`MANIFEST_VERSION`).
    sweep_id
        The grid identity (``grid_identity(grid)``) this cascade ran for.
    cascade_config
        The :class:`~brutex.metrics.cascade.CascadeConfig` (thresholds + seed) —
        stored so the MANUAL replay uses identical thresholds.
    bars_context
        The :class:`BarsContext` for the MANUAL bar reload.
    specs
        Every tuple's :class:`CascadeTupleSpec`.
    stage3
        Optional :class:`Stage3ManifestRecord` (DEC-STAGE3-WIRE-001) — Stage-3
        enablement + the refined-cell list. ``None`` for manifests written
        before the Stage-3 wiring (backward-compatible parse).
    leaderboard_trials
        Optional FROZEN per-leaderboard trial statistics
        (DEC-TRIAL-STATS-FREEZE-001, ADR-0008 condition 1) — built once from
        the sidecars by the AUTOMATIC path; the slice-4 consumer reads ONLY
        these. ``None`` for pre-freeze manifests (backward-compatible parse).
    roll_schedules
        Optional ADR-0012 futures roll-schedule carriage
        (DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001, review round-1): sorted
        ``(instrument, (expiry_ordinal, ...))`` pairs recorded by the
        AUTOMATIC path from the launch's resolved
        ``roll_schedule_by_instrument``, so the MANUAL replay rebuilds a
        futures winner's ``bar_bits`` with the SAME per-contract hard resets
        the sweep ranked it under (D11 — never schedule-blind). ``None`` for
        spot-only manifests AND for pre-carriage manifests
        (backward-compatible parse; a futures pool replayed without a
        schedule is loudly SKIPPED, see :func:`run_cascade_over_specs`).
    """

    version: int
    sweep_id: str
    cascade_config: CascadeConfig
    bars_context: BarsContext
    specs: tuple[CascadeTupleSpec, ...]
    stage3: Stage3ManifestRecord | None = None
    leaderboard_trials: tuple[LeaderboardTrialsRecord, ...] | None = None
    roll_schedules: tuple[tuple[str, tuple[int, ...]], ...] | None = None


@dataclass(frozen=True, slots=True)
class CascadeWireReport:
    """The aggregate cascade result — one :class:`CascadeReport` per pooled slice.

    Parameters
    ----------
    sweep_id
        The grid identity this report is for.
    groups
        ``(instrument, tf_label, report)`` triples, one per ``(instrument, tf)``
        pool, in sorted order.
    """

    sweep_id: str
    groups: tuple[tuple[str, str, CascadeReport], ...]

    @property
    def n_input(self) -> int:
        """Total candidates fed across every pool."""
        return sum(report.n_input for _, _, report in self.groups)

    @property
    def n_survived(self) -> int:
        """Total cascade survivors across every pool."""
        return sum(report.n_survived for _, _, report in self.groups)

    @property
    def survivor_ids(self) -> tuple[str, ...]:
        """Every surviving candidate id, in pool order then composite-rank order."""
        return tuple(sid for _, _, report in self.groups for sid in report.survivor_ids)

    @property
    def n_provisional(self) -> int:
        """Total PROVISIONAL passes across every pool (R2-SR-F4).

        Survivors whose DSR filter #2 deflated against the legacy pool-N
        fallback (``dsr_n_source == "pool"``) — sums only the pool-sourced
        groups' survivors in a mixed report (the per-group ``_trial_stats_for``
        resolution can differ per slice). A pool-sourced pass must never
        silently read as proven (DEC-STATS-GATE-INTEGRITY-001).
        """
        return sum(len(report.provisional_pass_ids) for _, _, report in self.groups)


# ── Cascade config (deterministic seed from the sweep id) ─────────────────────


def cascade_config_for_sweep(
    sweep_id: str, *, overrides: dict[str, object] | None = None
) -> CascadeConfig:
    """Build a deterministic :class:`~brutex.metrics.cascade.CascadeConfig` for a sweep.

    The mandatory cascade ``seed`` (Golden Rule #4) is derived from the sweep
    identity (its leading hex) so it is reproducible AND distinct per sweep; any
    *overrides* (e.g. relaxed thresholds for a manual probe) are applied on top.
    The resulting config is stored in the manifest so the MANUAL replay is exact.

    Parameters
    ----------
    sweep_id
        The grid identity (``>= 1`` hex char).
    overrides
        Optional field overrides forwarded to :class:`CascadeConfig`.

    Returns
    -------
    CascadeConfig
        The reproducible cascade config.

    Raises
    ------
    CascadeWireError
        If *sweep_id* is empty or not hex.
    """
    from brutex.metrics.cascade import CascadeConfig

    if not sweep_id:
        raise CascadeWireError("cascade_config_for_sweep: sweep_id must be non-empty")
    try:
        seed = int(sweep_id[:16], 16) % _SEED_MODULUS
    except ValueError as exc:
        raise CascadeWireError(f"cascade_config_for_sweep: sweep_id is not hex: {exc}") from exc
    kwargs: dict[str, object] = {"seed": seed}
    if overrides is not None:
        kwargs.update(overrides)
    return CascadeConfig(**kwargs)  # type: ignore[arg-type]


def resolve_cascade_embargo_bars(tf_minutes: int, override: int | None) -> int:
    """Resolve the Stage-4 embargo in decision-TF bars for one (instrument, tf) pool.

    DEC-CASCADE-STAT-TIER-WIRE-001, applying the DEC-STAT-001 embargo law
    (``docs/MIGRATION.md`` entry #123 —
    ``max(label horizon, 1 trading day)``): the cascade's candidate series are
    per-BAR equity returns, so the label span is ONE bar and the formula
    floors at ONE TRADING DAY expressed in decision-TF bars —
    ``ceil(DAILY_TF_MINUTES / tf_minutes)`` over the 375-minute NSE regular
    session (:data:`brutex.data.timeframes.DAILY_TF_MINUTES`, never a
    re-typed literal). The ``1day`` engine timeframe carries
    ``minutes == 375`` (``trading_minutes_for_lake_interval``), so the
    formula is uniform across all 21 TFs (``ceil(375/375) == 1``) — no
    special case. Examples: 1minute → 375, 2minute → 188, 5minute → 75,
    375minute/1day → 1.

    Parameters
    ----------
    tf_minutes
        The pool's decision timeframe in trading minutes (``>= 1`` — the
        :class:`CascadeTupleSpec` invariant).
    override
        :attr:`~brutex.metrics.cascade.CascadeConfig.wfo_embargo` — ``None``
        = AUTO (the formula above); an explicit ``int >= 0`` wins uniformly.

    Returns
    -------
    int
        The resolved embargo in bars (``>= 0``).

    Raises
    ------
    CascadeWireError
        On a negative *override* or a ``tf_minutes < 1``.
    """
    from brutex.data.timeframes import DAILY_TF_MINUTES

    if override is not None:
        if override < 0:
            raise CascadeWireError(
                f"CascadeConfig.wfo_embargo must be >= 0 or None (AUTO), got {override}"
            )
        return override
    if tf_minutes < 1:
        raise CascadeWireError(
            f"resolve_cascade_embargo_bars: tf_minutes must be >= 1, got {tf_minutes}"
        )
    return (DAILY_TF_MINUTES + tf_minutes - 1) // tf_minutes


def cascade_config_to_dict(config: CascadeConfig) -> dict[str, object]:
    """Return *config*'s fields as a JSON-serialisable dict (manifest storage)."""
    from dataclasses import asdict

    return asdict(config)


def cascade_config_from_dict(data: dict[str, object]) -> CascadeConfig:
    """Rebuild a :class:`CascadeConfig` from :func:`cascade_config_to_dict` output.

    Presence-tolerant for the NEW appended-with-default keys ONLY
    (``wfo_embargo`` / ``candidate_pbo_family_cap``,
    DEC-CASCADE-STAT-TIER-WIRE-001): an OLD manifest dict WITHOUT them
    rebuilds with the dataclass defaults (they are ordinary defaulted
    fields — no special-casing needed). Every OTHER drift keeps the
    existing refusal law — an unknown key / missing required field is a
    ``TypeError`` inside ``CascadeConfig(**data)`` → :class:`CascadeWireError`.
    The forward-compat asymmetry is DELIBERATE (hostile-review round 1): the
    CONFIG parse is STRICT — a NEW manifest's unknown config key read by OLD
    code refuses loudly (the established strict-config precedent) — while
    the spec's ``family_sl_tp`` is presence-tolerant ADDITIVE
    (EMIT-ON-PRESENT at MANIFEST_VERSION 1; see ``manifest_from_json``).
    """
    from brutex.metrics.cascade import CascadeConfig

    try:
        return CascadeConfig(**data)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise CascadeWireError(f"invalid cascade_config in manifest: {exc}") from exc


# ── CandidateInput reconstruction ─────────────────────────────────────────────


def _finite(value: float | None) -> float:
    """Map a ``float | None`` metric to a finite float (``None`` / non-finite → ``0.0``).

    The cascade's :class:`CandidateInput` rejects any non-finite scalar; a metric
    that is mathematically undefined for the sample (``None``) or an out-of-domain
    ``inf``/``nan`` maps to ``0.0`` — the same documented choice as
    :func:`brutex.engine.sweep_run.ranked_combo_to_result_row`. Real finite values
    pass through unchanged.
    """
    if value is None or not math.isfinite(value):
        return 0.0
    return value


def _per_bar_returns(equity_paisa: Sequence[int], start_equity_paisa: int) -> tuple[float, ...]:
    """Per-bar realised return = the per-bar equity change ÷ the notional base.

    The first bar's return is measured against ``start_equity_paisa`` (the curve's
    implicit pre-window equity); every subsequent bar against the prior bar. The
    series is the equal-length pool axis the cascade's matrix filters require.
    """
    out: list[float] = []
    prev = start_equity_paisa
    for value in equity_paisa:
        out.append((value - prev) / start_equity_paisa)
        prev = value
    return tuple(out)


def _max_drawdown_return_units(returns: Sequence[float]) -> float:
    """Largest peak-to-trough decline of the cumulative-return path (``>= 0``).

    Matches :func:`brutex.metrics.lockbox._max_drawdown`'s convention so the
    in-sample ``training_max_drawdown`` reference is in the SAME units the lockbox
    re-measures, keeping the drawdown gate apples-to-apples. ``0.0`` for an empty
    or monotonically non-decreasing path.
    """
    peak = float("-inf")
    cum = 0.0
    max_dd = 0.0
    for r in returns:
        cum += r
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return max_dd


def _make_mc_evaluate(
    *,
    bars: Sequence[CandleBar],
    side: object,
    tf_minutes: int,
    sl_paisa: int | None,
    tp_paisa: int | None,
    start_equity_paisa: int,
) -> Callable[[npt.NDArray[np.intp]], float]:
    """Build the Monte-Carlo exit-logic callable (filter #9) for one candidate.

    Returns a closure that, given a sorted array of entry-bar positions, relocates
    the entry signals there, re-runs the SAME trade-sim (identical exit logic —
    the one hardcoded worst-case fill law, DEC-FILL-WORST-CASE-ONLY-001), and
    returns the per-trade Sharpe — comparable to the candidate's observed per-trade
    Sharpe. An undefined Sharpe (``< 2`` trades / zero variance) returns ``nan``,
    which the permutation test treats as "did not beat" the observed statistic.
    """
    from brutex.engine.trade_sim import simulate_trades
    from brutex.metrics.cpcv import sharpe_of_returns

    n_bars = len(bars)

    def evaluate(positions: npt.NDArray[np.intp]) -> float:
        signals = [False] * n_bars
        for position in positions:
            signals[int(position)] = True
        trades = simulate_trades(
            bars,
            signals,
            direction=side,  # type: ignore[arg-type]
            tf_minutes=tf_minutes,
            stop_loss_paisa=sl_paisa,
            take_profit_paisa=tp_paisa,
            # HONEST ENVELOPE (DEC-VIX-STAMP-WIRE-001, coordinator ruling): the
            # Monte-Carlo permutation filter's trades are the DISCARDED null
            # distribution — relocated entry signals whose per-trade Sharpe is
            # the only output; no row, report or operator surface ever sees
            # the trades themselves — so stamping every permutation's trades
            # would be pure wasted work with zero observable output. The
            # stream is EXPLICITLY unstamped via this literal (one of the
            # ENUMERATED sanctioned literal-None seam bindings — five after
            # the round-6 review also unstamped the sibling candidate
            # re-simulation in build_candidate_input — AST-pinned with exact
            # counts by tests/unit/test_vix_stamper_binding_pin.py — never a
            # silent omission). REVERT LEVER: thread a stamper through
            # _make_mc_evaluate and bind it here (one kwarg; the null
            # statistic is VIX-independent either way).
            reference_stamper=None,
        )
        rets = [t.pnl_paisa / start_equity_paisa for t in trades]
        sharpe = sharpe_of_returns(rets)
        return float("nan") if sharpe is None else float(sharpe)

    return evaluate


def build_candidate_input(
    *,
    run_id: str,
    mask: int,
    bars: Sequence[CandleBar],
    bar_bits: Sequence[int],
    direction_label: str,
    tf_minutes: int,
    sl_paisa: int | None,
    tp_paisa: int | None,
    start_equity_paisa: int,
    lockbox_default_window_days: int = LOCKBOX_DEFAULT_WINDOW_DAYS,
    lockbox_min_trades: int = LOCKBOX_DEFAULT_MIN_TRADES,
    family_sl_tp: tuple[tuple[int, int], ...] | None = None,
) -> CandidateInput:
    """Re-simulate one winning *mask* over *bars* into a cascade :class:`CandidateInput`.

    Projects the mask onto the pre-built ``bar_bits``, simulates the trades with
    the tuple's exact knobs (so the result reproduces the persisted finalist), and
    fills every field the nine filters need (see the module docstring for the
    per-field conventions — including the R2-SR-F1 train-complement training
    references and the R2-SR-F2 per-trade lockbox evidence).

    Parameters
    ----------
    run_id
        The finalist's 64-char BLAKE3 run-identity (the candidate id).
    mask
        The winning u64 strategy mask.
    bars, bar_bits
        The slice's bars + the pre-built Stage-1A matrix (built once per slice).
    direction_label
        ``"bull"`` / ``"bear"``.
    tf_minutes, sl_paisa, tp_paisa, start_equity_paisa
        The tuple's trade-sim knobs (must match what produced the finalist).
    lockbox_default_window_days, lockbox_min_trades
        The lockbox window params the training-reference complement is
        resolved with — MUST equal the :class:`CascadeConfig` values filter #8
        will gate with (``run_cascade_over_specs`` threads them from the ONE
        config, so both sides share the same window; a hard-coded default
        here would re-introduce a train/holdout mismatch for any non-default
        config). Defaults are the lockbox module's own public constants.
    family_sl_tp
        The candidate's Stage-3 search family
        (DEC-CASCADE-STAT-TIER-WIRE-001; already cap-truncated by
        :func:`candidates_for_spec`): with ``>= 2`` pairs, each pair is
        re-simulated through the SAME simulation path the finalist uses —
        identical bars / entry signals / direction / tf, ONLY the SL/TP legs
        swapped — into one :func:`_per_bar_returns` column per pair (the
        candidate's own pair reuses its already-computed series, never a
        second sim), feeding the per-candidate CSCV/PBO check at filter #5.
        The production caller guarantees the candidate's own
        ``(sl_paisa, tp_paisa)`` is a family member (the
        :class:`CascadeTupleSpec` invariant). ``None`` or fewer than 2 pairs
        → ``family_returns=None`` (the check is honestly skipped). HONEST
        cost: O(len(family) × bars) extra sim work per candidate — the same
        cost class as the finalist re-sim, multiplied by the family width
        (bounded by ``CascadeConfig.candidate_pbo_family_cap``).

    Returns
    -------
    CandidateInput
        The reconstructed cascade input.
    """
    from brutex.calendars.nse import IST
    from brutex.engine.combo_mask import mask_to_entry_signals
    from brutex.engine.sweep_driver import build_equity_curve
    from brutex.engine.trade_sim import simulate_trades
    from brutex.metrics.cascade import CandidateInput
    from brutex.metrics.compute import compute_metrics
    from brutex.metrics.cpcv import sharpe_of_returns
    from brutex.strategies.types import Side

    side = Side.BUY if direction_label == "bull" else Side.SELL
    entry_signals = mask_to_entry_signals(bar_bits, mask)

    def _trades_for_legs(leg_sl: int | None, leg_tp: int | None) -> list[ClosedTrade]:
        """The ONE re-simulation law: this mask's trades for one (sl, tp) pair.

        Shared by the finalist's own re-simulation AND the family columns
        (DEC-CASCADE-STAT-TIER-WIRE-001) — ONE ``simulate_trades`` call site,
        so the two can never drift and the VIX AST pin's exact literal count
        (``tests/unit/test_vix_stamper_binding_pin.py``) is unchanged.
        Reproduces the persisted finalist under the ONE hardcoded worst-case
        fill law (DEC-FILL-WORST-CASE-ONLY-001).

        HONEST ENVELOPE (DEC-VIX-STAMP-WIRE-001, round-6 review): the
        re-simulation's trades are a DISCARDED stream — every observable
        output is the CandidateInput SCALARS + returns tuples built by the
        caller (no trade object, and no VIX field, survives the conversion),
        so the report bytes are provably identical stamped vs unstamped and a
        per-candidate stamp would be pure wasted work. The stream is
        EXPLICITLY unstamped via this literal (one of the FIVE enumerated
        sanctioned literal-None seam bindings, AST-pinned with exact counts —
        never a silent omission); the old reference_stamper seam through the
        cascade chain was DELETED with the two manual-replay
        vix_stamper_for_manifest builds it existed to feed (each paid a full
        window-bounded VIX lake read per replay/click for zero output — the
        same dead-build class the round-2 review removed at the options-sim
        sites). REVERT LEVER: add a VIX surface to CandidateInput/the cascade
        report FIRST, then re-thread the seam.
        """
        return simulate_trades(
            bars,
            entry_signals,
            direction=side,
            tf_minutes=tf_minutes,
            stop_loss_paisa=leg_sl,
            take_profit_paisa=leg_tp,
            reference_stamper=None,
        )

    trades = _trades_for_legs(sl_paisa, tp_paisa)
    equity = build_equity_curve(
        bars, trades, start_equity_paisa=start_equity_paisa, tf_minutes=tf_minutes
    )
    metrics = compute_metrics(trades, equity, total_bars=len(bars))

    per_bar = _per_bar_returns(equity.equity_paisa, start_equity_paisa)

    def _family_column(fam_sl: int, fam_tp: int) -> tuple[float, ...]:
        """One family pair's aligned per-bar return column (legs swapped only)."""
        fam_equity = build_equity_curve(
            bars,
            _trades_for_legs(fam_sl, fam_tp),
            start_equity_paisa=start_equity_paisa,
            tf_minutes=tf_minutes,
        )
        return _per_bar_returns(fam_equity.equity_paisa, start_equity_paisa)

    # Per-candidate CSCV family columns (DEC-CASCADE-STAT-TIER-WIRE-001): one
    # aligned per-bar return series per family (sl, tp) pair; the candidate's
    # own pair reuses the just-computed series (identical by construction —
    # same law, same inputs), every other pair is a real re-simulation with
    # ONLY the legs swapped. Fewer than 2 pairs → honest None (skip).
    family_returns: tuple[tuple[float, ...], ...] | None = None
    if family_sl_tp is not None and len(family_sl_tp) >= _MIN_FAMILY_COLUMNS:
        family_returns = tuple(
            per_bar if (fam_sl, fam_tp) == (sl_paisa, tp_paisa) else _family_column(fam_sl, fam_tp)
            for fam_sl, fam_tp in family_sl_tp
        )
    day_ordinals = tuple(bar.timestamp.astimezone(IST).date().toordinal() for bar in bars)
    n_entries = sum(1 for signal in entry_signals if signal)

    # TRUE per-trade lockbox evidence (R2-SR-F2): each trade's exit-day IST
    # ordinal, sorted — the count source for filter #8's 500-trade floor.
    trade_days = tuple(sorted(t.exit_ts.astimezone(IST).date().toordinal() for t in trades))
    # Train/holdout split via the ONE shared resolver (R2-SR-F1): the training
    # references are measured over the bars strictly BEFORE the lockbox window
    # start — the same window filter #8 will gate with (same params, same
    # evidence), so the degradation floor no longer compares the holdout tail
    # against a reference that contains the tail.
    window = resolve_lockbox_window(
        day_ordinals,
        evidence_days=trade_days,
        default_window_days=lockbox_default_window_days,
        min_trades=lockbox_min_trades,
    )
    train_returns = tuple(
        r for r, day in zip(per_bar, day_ordinals, strict=True) if day < window.window_start
    )
    # ``sharpe_of_returns`` raises on an EMPTY series (and returns ``None`` on
    # < 2 samples / zero variance) — an empty complement is the same
    # degenerate-fallback case, never an error.
    train_sharpe = sharpe_of_returns(train_returns) if train_returns else None
    if train_sharpe is None:
        # Degenerate complement (< 2 bars OR zero variance — e.g. every trade
        # exits inside the lockbox window, leaving a flat pre-window equity):
        # fall back to the legacy FULL-series pair as ONE unit (never a mixed
        # complement-Sharpe/full-MDD reference — a flat complement's MDD of
        # 0.0 would turn the drawdown ceiling into an auto-FAIL). HONEST
        # (Golden Rule #10): decontamination does NOT hold for these
        # candidates — logged loudly so a fallback can never pass silently.
        training_sharpe = _finite(sharpe_of_returns(per_bar))
        training_max_drawdown = _max_drawdown_return_units(per_bar)
        _LOG.warning(
            "cascade_wire.training_ref_fallback",
            run_id=run_id,
            n_train_bars=len(train_returns),
            window_start_day=window.window_start,
            reason="train-complement Sharpe undefined (< 2 bars or zero variance)",
        )
    else:
        training_sharpe = train_sharpe
        training_max_drawdown = _max_drawdown_return_units(train_returns)

    return CandidateInput(
        candidate_id=run_id,
        sharpe_ratio=_finite(metrics.sharpe_ratio),
        sortino_ratio=_finite(metrics.sortino_ratio),
        calmar_ratio=_finite(metrics.calmar_ratio),
        profit_factor=_finite(metrics.profit_factor),
        win_rate=metrics.win_rate,
        win_loss_ratio=_finite(metrics.win_loss_ratio),
        recovery_factor=_finite(metrics.recovery_factor),
        max_drawdown_pct=metrics.max_drawdown_pct,
        max_consecutive_losses=metrics.max_consecutive_losses,
        total_trades=metrics.total_trades,
        training_sharpe=training_sharpe,
        training_max_drawdown=training_max_drawdown,
        skew=_finite(metrics.skewness),
        # CandidateInput.kurtosis is the NON-EXCESS (Pearson) kurtosis the DSR
        # PSR radicand expects (Gaussian == 3.0); compute_metrics stores EXCESS
        # kurtosis (Gaussian == 0.0, via _excess_kurtosis), so add GAUSSIAN_KURTOSIS
        # back here exactly as the sibling with_stage4_statistics does
        # (compute.py). Without the +3 the DSR kill-gate was fed excess kurtosis,
        # shrinking the estimator-variance radicand and OVER-stating the DSR —
        # biasing the gate toward PASS (DEC-DSR-KURTOSIS-FIX-001).
        kurtosis=_finite(metrics.kurtosis) + GAUSSIAN_KURTOSIS,
        returns=per_bar,
        lockbox_returns=per_bar,
        lockbox_days=day_ordinals,
        lockbox_trade_days=trade_days,
        mc_n_bars=len(bars),
        mc_n_entries=n_entries,
        mc_evaluate=_make_mc_evaluate(
            bars=bars,
            side=side,
            tf_minutes=tf_minutes,
            sl_paisa=sl_paisa,
            tp_paisa=tp_paisa,
            start_equity_paisa=start_equity_paisa,
        ),
        family_returns=family_returns,
    )


def candidates_for_spec(
    spec: CascadeTupleSpec,
    bars: Sequence[CandleBar],
    *,
    lockbox_default_window_days: int = LOCKBOX_DEFAULT_WINDOW_DAYS,
    lockbox_min_trades: int = LOCKBOX_DEFAULT_MIN_TRADES,
    roll_schedule: tuple[int, ...] | None = None,
    family_cap: int | None = None,
) -> list[CandidateInput]:
    """Re-build every winner of *spec* into a :class:`CandidateInput` over *bars*.

    Builds the Stage-1A ``bar_bits`` once (the tuple's proximity + prior_n), then
    projects each winning mask through :func:`build_candidate_input`. The lockbox
    window params are threaded through unchanged (see
    :func:`build_candidate_input` — they MUST match the cascade config's).
    ``roll_schedule`` is the ADR-0012 futures pass-through (default ``None`` —
    byte-identical for every spot spec); :func:`run_cascade_over_specs`
    threads each pool's schedule in and the manifest carries it for the
    MANUAL replay (``CascadeManifest.roll_schedules``), so a futures winner
    is re-scored under the SAME per-contract hard resets the sweep ranked it
    with (DEC-STAGE1A-ROLL-RESET-001 D11 — review round-1).

    ``family_cap`` (DEC-CASCADE-STAT-TIER-WIRE-001) bounds the per-candidate
    CSCV family width: a ``spec.family_sl_tp`` wider than the cap is
    truncated to the FIRST ``family_cap`` pairs in the deterministic (sorted)
    persisted order with ONE loud ``cascade_wire.family_capped`` WARNING per
    spec — never silently. If the truncation would drop the spec's OWN
    ``(sl_paisa, tp_paisa)`` pair, the family is DROPPED wholesale instead
    (``cascade_wire.family_cap_dropped_own_pair`` — CSCV over a family that
    excludes the candidate says nothing about it; the per-candidate PBO is
    then honestly skipped). ``None`` (the default) applies no cap — direct
    callers; the production wire always threads
    ``CascadeConfig.candidate_pbo_family_cap``. A ``family_cap < 1`` is
    REFUSED (:class:`CascadeWireError`) — aligned with
    :func:`run_cascade_over_specs`'s config-level refusal, so a direct
    caller's cap-0 can never quietly degrade every family to ``None``
    (hostile-review round 1).
    """
    if family_cap is not None and family_cap < 1:
        raise CascadeWireError(f"family_cap must be >= 1 or None, got {family_cap}")
    from brutex.engine.stage1a import apply_1day_mask_for, build_bar_bits
    from brutex.vocabulary import ProximityParams

    proximity = (
        None
        if spec.proximity_bps is None
        else ProximityParams(
            pivot_proximity_bps=spec.proximity_bps[0],
            pdh_pdl_proximity_bps=spec.proximity_bps[1],
            fib_proximity_bps=spec.proximity_bps[2],
        )
    )
    bar_bits = build_bar_bits(
        bars,
        proximity=proximity,
        prior_n=spec.prior_n,
        fib_anchor=spec.fib_anchor,
        vwap_source=spec.vwap_source,
        ema_source=spec.ema_source,
        roll_schedule=roll_schedule,
        apply_1day_mask=apply_1day_mask_for(spec.instrument, spec.tf_label),
    )
    family = spec.family_sl_tp
    if family is not None and family_cap is not None and len(family) > family_cap:
        truncated = family[:family_cap]
        dropped = len(family) - len(truncated)
        if (spec.sl_paisa, spec.tp_paisa) not in truncated:
            # The cap would exclude the finalists' own pair — CSCV over a
            # family that excludes the candidate says nothing about it, so
            # the per-candidate PBO is honestly SKIPPED for this spec.
            _LOG.warning(
                "cascade_wire.family_cap_dropped_own_pair",
                instrument=spec.instrument,
                tf=spec.tf_label,
                direction=spec.direction_label,
                sl_paisa=spec.sl_paisa,
                tp_paisa=spec.tp_paisa,
                family_size=len(family),
                cap=family_cap,
            )
            family = None
        else:
            _LOG.warning(
                "cascade_wire.family_capped",
                instrument=spec.instrument,
                tf=spec.tf_label,
                direction=spec.direction_label,
                sl_paisa=spec.sl_paisa,
                tp_paisa=spec.tp_paisa,
                family_size=len(family),
                cap=family_cap,
                dropped=dropped,
            )
            family = truncated
    return [
        build_candidate_input(
            run_id=run_id,
            mask=mask,
            bars=bars,
            bar_bits=bar_bits,
            direction_label=spec.direction_label,
            tf_minutes=spec.tf_minutes,
            sl_paisa=spec.sl_paisa,
            tp_paisa=spec.tp_paisa,
            start_equity_paisa=spec.start_equity_paisa,
            lockbox_default_window_days=lockbox_default_window_days,
            lockbox_min_trades=lockbox_min_trades,
            family_sl_tp=family,
        )
        for run_id, mask in spec.winners
    ]


# ── The shared pure core (auto path == manual path) ───────────────────────────


def run_cascade_over_specs(
    specs: Sequence[CascadeTupleSpec],
    *,
    bars_for: Callable[[str, str], Sequence[CandleBar]],
    config: CascadeConfig,
    sweep_id: str,
    leaderboard_trials: Sequence[LeaderboardTrialsRecord] | None = None,
    roll_schedules: Mapping[str, tuple[int, ...]] | None = None,
) -> CascadeWireReport:
    """Run the Stage-4 cascade per ``(instrument, tf)`` pool → one aggregate report.

    The single function both the AUTOMATIC and the MANUAL paths call, so a
    byte-identical *specs* + *config* + *bars_for* produces a byte-identical
    report. Specs are grouped by ``(instrument, tf_label)`` (the equal-length
    returns pool); for each group with bars + candidates the cascade runs once.

    Parameters
    ----------
    specs
        Every tuple's :class:`CascadeTupleSpec`.
    bars_for
        ``(instrument, tf_label) -> bars`` loader (empty ⇒ that slice is skipped).
    config
        The :class:`CascadeConfig` (shared across pools — same seed + thresholds).
    sweep_id
        The grid identity stamped on the report.
    leaderboard_trials
        Optional FROZEN per-leaderboard deflation inputs (ADR-0008 cond. 6).
    roll_schedules
        Optional per-instrument ADR-0012 futures roll schedules
        (``instrument -> (expiry_ordinal, ...)``) — the launch's resolved
        ``roll_schedule_by_instrument`` on the AUTOMATIC path, the manifest's
        recorded ``roll_schedules`` on the MANUAL replay. A pool whose
        instrument is the NIFTY-futures series alias with NO schedule
        available (a pre-carriage manifest) is loudly SKIPPED — re-scoring
        it schedule-blind would silently violate the D11 roll-reset law
        (Golden Rule #1; review round-1). Spot pools ignore this (``None``
        schedule is the byte-identical spot path); a schedule entry for a
        NON-alias pool is REFUSED (:class:`CascadeWireError` — the positive
        direction of the presence ⟺ alias invariant, review round-3
        finding 2).

    Returns
    -------
    CascadeWireReport
        One :class:`CascadeReport` per pooled slice, sorted by ``(instrument, tf)``.
    """
    from brutex.metrics.cascade import DsrTrialStats, run_stage4_cascade

    # DEC-CASCADE-STAT-TIER-WIRE-001: CascadeConfig is a frozen no-__post_init__
    # dataclass, so the family cap is validated HERE at the ONE production
    # consumption point (the wfo_embargo override is validated inside
    # resolve_cascade_embargo_bars per pool).
    if config.candidate_pbo_family_cap < 1:
        raise CascadeWireError(
            f"CascadeConfig.candidate_pbo_family_cap must be >= 1, "
            f"got {config.candidate_pbo_family_cap}"
        )

    # The frozen per-leaderboard deflation inputs (DEC-TRIAL-STATS-CONSUME-001,
    # ADR-0008 condition 6): a group's record must be PRESENT, COMPLETE and
    # deflatable to be used; anything less falls back to pool-N LOUDLY — a
    # knowingly partial N is never deflated against.
    trials_by_key: dict[tuple[str, str], LeaderboardTrialsRecord] = {}
    for record in leaderboard_trials or ():
        trials_by_key[(record.instrument, record.tf_label)] = record

    def _trial_stats_for(instrument: str, tf_label: str) -> DsrTrialStats | None:
        record = trials_by_key.get((instrument, tf_label))
        if record is None:
            if trials_by_key:
                _LOG.warning(
                    "cascade_wire.dsr_pool_fallback",
                    instrument=instrument,
                    tf=tf_label,
                    reason="no frozen leaderboard record for this slice",
                )
            return None
        stats = record.to_stats()
        if not record.complete or not stats.deflatable:
            _LOG.warning(
                "cascade_wire.dsr_pool_fallback",
                instrument=instrument,
                tf=tf_label,
                reason="incomplete" if not record.complete else "undeflatable",
                n_trials=record.n_trials,
                n_finite=record.n_finite,
            )
            return None
        return DsrTrialStats(
            n_trials=record.n_trials,
            sharpe_variance=stats.variance,
            n_finite=record.n_finite,
        )

    grouped: dict[tuple[str, str], list[CascadeTupleSpec]] = {}
    for spec in specs:
        grouped.setdefault((spec.instrument, spec.tf_label), []).append(spec)

    groups: list[tuple[str, str, CascadeReport]] = []
    for instrument, tf_label in sorted(grouped):
        bars = bars_for(instrument, tf_label)
        if not bars:
            continue
        roll_schedule = None if roll_schedules is None else roll_schedules.get(instrument)
        if roll_schedule is not None and not is_futures_series_symbol(instrument):
            # The POSITIVE direction of the presence ⟺ alias invariant
            # (review round-3 finding 2): a schedule entry for a SPOT
            # instrument would rebuild the pool's bar_bits WITH per-contract
            # hard resets — silently breaking the replay's "byte-identical to
            # the sweep's report" contract, then (for a schedule ending before
            # the window's last bar day) dying on Stage-1A's raw
            # beyond-schedule ValueError misattributed as a code bug. Refuse
            # loudly instead; every other consumer already enforces both
            # directions (SweepRunConfig.__post_init__).
            raise CascadeWireError(
                f"roll_schedules carries an entry for non-futures instrument "
                f"{instrument!r} — only the NIFTY-futures series alias may carry a "
                "roll schedule (presence ⟺ alias); the manifest/mapping is corrupt — "
                "re-run `brutex sweep-all` to mint a fresh one"
            )
        if roll_schedule is None and is_futures_series_symbol(instrument):
            # A futures pool with NO schedule (a pre-carriage manifest replay)
            # must never be re-scored schedule-blind: the bar_bits would cross
            # every roll basis gap (the exact D11 violation the sweep's hard
            # resets prevent) and the report's verdicts would silently diverge
            # from the sweep that minted the winners. Skip LOUDLY instead
            # (honest degradation — Golden Rule #1; re-run `brutex sweep-all`
            # to mint a schedule-carrying manifest).
            _LOG.warning(
                "cascade_wire.futures_pool_skipped_no_roll_schedule",
                instrument=instrument,
                tf=tf_label,
                n_specs=len(grouped[(instrument, tf_label)]),
                remedy="re-run `brutex sweep-all` to record roll_schedules in the manifest",
            )
            continue
        group_specs = grouped[(instrument, tf_label)]
        # DEC-CASCADE-STAT-TIER-WIRE-001: ONE resolved embargo per pool. The
        # group's tf_minutes must be unambiguous (tf_label <-> tf_minutes is
        # 1:1 by construction; a disagreeing manifest is corrupt — refuse
        # loudly rather than embargo one pool under two different clocks).
        group_tf_minutes = {spec.tf_minutes for spec in group_specs}
        if len(group_tf_minutes) != 1:
            raise CascadeWireError(
                f"specs for pool ({instrument!r}, {tf_label!r}) disagree on tf_minutes "
                f"{sorted(group_tf_minutes)} — the manifest is corrupt; re-run "
                "`brutex sweep-all` to mint a fresh one"
            )
        embargo_bars = resolve_cascade_embargo_bars(
            next(iter(group_tf_minutes)), config.wfo_embargo
        )
        candidates: list[CandidateInput] = []
        for spec in group_specs:
            candidates.extend(
                candidates_for_spec(
                    spec,
                    bars,
                    # The ONE config drives BOTH sides of the train/holdout
                    # split (R2-SR-F1 review A1): the wire's training-reference
                    # complement AND filter #8's gate window — a non-default
                    # config would otherwise silently mismatch them.
                    lockbox_default_window_days=config.lockbox_default_window_days,
                    lockbox_min_trades=config.lockbox_min_trades,
                    # ADR-0012: the pool's OWN roll schedule (None = spot path).
                    roll_schedule=roll_schedule,
                    # DEC-CASCADE-STAT-TIER-WIRE-001: the ONE config's family
                    # cap bounds the per-candidate CSCV re-sim width.
                    family_cap=config.candidate_pbo_family_cap,
                )
            )
        if not candidates:
            continue
        report = run_stage4_cascade(
            candidates,
            config,
            trial_stats=_trial_stats_for(instrument, tf_label),
            # The pool's resolved DEC-STAT-001 embargo (AUTO = one trading
            # day in decision-TF bars; config.wfo_embargo overrides).
            embargo_bars=embargo_bars,
        )
        groups.append((instrument, tf_label, report))

    _LOG.info(
        "cascade_wire.complete",
        sweep_id=sweep_id,
        pools=len(groups),
        n_input=sum(report.n_input for _, _, report in groups),
        n_survived=sum(report.n_survived for _, _, report in groups),
        # R2-SR-F4: pool-N-deflated passes are surfaced on the summary log so
        # a provisional pass can never silently read as proven.
        n_provisional=sum(len(report.provisional_pass_ids) for _, _, report in groups),
    )
    return CascadeWireReport(sweep_id=sweep_id, groups=tuple(groups))


# ── Report serialisation (deterministic) ──────────────────────────────────────


def _report_to_dict(report: CascadeReport) -> dict[str, object]:
    """Serialise one :class:`CascadeReport` into a JSON-native dict.

    ``pool_pbo`` (DEC-PBO-SIDECAR-001) is the slice's measured CSCV pool PBO —
    the RAW float (round-trippable ``repr``, never rounded) or ``null`` when
    filter #5 was skipped (see :class:`~brutex.metrics.cascade.CascadeReport`).
    The per-verdict ``provisional`` key (R2-SR-F4) is ADDITIVE at
    ``REPORT_VERSION`` 1 — the same precedent as ``pool_pbo``; a pre-change
    report simply lacks the key. The per-verdict ``cpcv_pass_rate`` key
    (DEC-RESULTS-CASCADE-VERDICT-JOIN-001) rides the SAME additive-at-1
    precedent: the RAW full-precision stage-3 fraction or ``null`` (a
    pre-change report simply lacks the key — readers None-fill). The
    report-level ``pool_rc_pvalue``/``pool_spa_pvalue`` and the per-verdict
    ``candidate_pbo`` keys (DEC-CASCADE-STAT-TIER-WIRE-001) ride the SAME
    precedent again — raw full-precision floats or ``null``.
    """
    return {
        "n_input": report.n_input,
        "n_survived": report.n_survived,
        "dsr_n_source": report.dsr_n_source,
        "dsr_n_trials": report.dsr_n_trials,
        "dsr_trial_variance": report.dsr_trial_variance,
        "pool_pbo": report.pool_pbo,
        "pool_rc_pvalue": report.pool_rc_pvalue,
        "pool_spa_pvalue": report.pool_spa_pvalue,
        "survivor_ids": list(report.survivor_ids),
        "stage_survivors": dict(report.stage_survivors),
        "verdicts": [
            {
                "candidate_id": verdict.candidate_id,
                "passed": verdict.passed,
                "rejected_at": None if verdict.rejected_at is None else verdict.rejected_at.value,
                "detail": verdict.detail,
                "composite_score": verdict.composite_score,
                "provisional": verdict.provisional,
                "cpcv_pass_rate": verdict.cpcv_pass_rate,
                "candidate_pbo": verdict.candidate_pbo,
            }
            for verdict in report.verdicts
        ],
    }


def wire_report_to_dict(report: CascadeWireReport) -> dict[str, object]:
    """Serialise a :class:`CascadeWireReport` into a JSON-native dict."""
    return {
        "version": REPORT_VERSION,
        "sweep_id": report.sweep_id,
        "n_input": report.n_input,
        "n_survived": report.n_survived,
        "groups": [
            {"instrument": instrument, "tf": tf_label, "report": _report_to_dict(group_report)}
            for instrument, tf_label, group_report in report.groups
        ],
    }


def serialize_wire_report(report: CascadeWireReport) -> bytes:
    """Return the deterministic JSON bytes of *report* (the byte-identity surface).

    Keys are sorted and floats use Python's round-trippable ``repr``, so the same
    report always serialises to the same bytes — the contract the golden test
    (auto path == manual path) asserts. ``allow_nan=False`` fails loudly rather
    than emitting non-standard ``NaN``/``Infinity`` (the composite z-scores are
    clamped finite, so this never triggers in practice).
    """
    return json.dumps(
        wire_report_to_dict(report), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


# ── Per-leaderboard trial-stats freeze (DEC-TRIAL-STATS-FREEZE-001) ──────────


@dataclass(frozen=True, slots=True)
class LeaderboardTrialsRecord:
    """One (instrument, tf) leaderboard's FROZEN trial statistics (ADR-0008 cond. 1).

    The manifest-serialisable projection of
    :class:`~brutex.engine.trial_stats.LeaderboardTrialStats`: the AUTOMATIC
    path aggregates the per-tuple sidecars exactly once at cascade-build time
    and freezes the merged numbers here; BOTH cascade paths consume only the
    frozen record (sidecars are build-time inputs, never replay-time inputs).

    Parameters
    ----------
    instrument, tf_label
        The leaderboard key (the wire's pooling axis); non-empty.
    n_trials, n_finite
        The leaderboard N (every backtested combo + every unique Stage-3
        refined row) and its finite-Sharpe subset (``n_finite <= n_trials``).
    sum_sharpe, sum_sharpe_sq
        The merged fsum V-moments (finite floats; ``0.0`` when ``n_finite==0``).
    complete
        ``False`` when ANY contributing tuple/cell store lacked a paired
        sidecar (an old tree or a slice-2 mismatch-skip) — consumers must
        treat an incomplete record as pool-fallback, NEVER deflate against a
        knowingly partial N (ADR-0008 condition 6).

    Raises
    ------
    CascadeWireError
        On an empty label or any moment/count invariant violation (delegated
        to the slice-1 core's own validation).
    """

    instrument: str
    tf_label: str
    n_trials: int
    n_finite: int
    sum_sharpe: float
    sum_sharpe_sq: float
    complete: bool

    def __post_init__(self) -> None:
        for name in ("instrument", "tf_label"):
            if not getattr(self, name):
                raise CascadeWireError(f"LeaderboardTrialsRecord.{name} must be non-empty")
        # One validation source (CLAUDE.md §11 COMMON): the slice-1 core owns
        # the count/moment invariants — construct-to-validate, re-raise typed.
        from brutex.engine.trial_stats import TrialStatsError

        try:
            self.to_stats()
        except TrialStatsError as exc:
            raise CascadeWireError(f"invalid leaderboard trial stats: {exc}") from exc

    def to_stats(self) -> LeaderboardTrialStats:
        """Project back to the slice-1 :class:`LeaderboardTrialStats` (validated)."""
        from brutex.engine.trial_stats import LeaderboardTrialStats

        return LeaderboardTrialStats(
            n_trials=self.n_trials,
            n_finite=self.n_finite,
            sum_sharpe=self.sum_sharpe,
            sum_sharpe_sq=self.sum_sharpe_sq,
            complete=self.complete,
        )


class _RefinedCellLike(Protocol):
    """The (tuple_, path) slice of a Stage-3 refined cell (structural typing).

    ``stage3_wire.Stage3RefinedCell`` satisfies this; cascade_wire cannot
    import it directly (stage3_wire imports THIS module — a cycle).
    """

    @property
    def tuple_(self) -> SweepTuple:
        """The cell's constructed sweep tuple."""
        ...  # pragma: no cover — Protocol stub, never executed

    @property
    def path(self) -> Path:
        """The cell's Parquet store path."""
        ...  # pragma: no cover — Protocol stub, never executed


def build_leaderboard_trials(
    results_root: Path,
    grid: SweepGridConfig,
    *,
    stage3_cells: Sequence[_RefinedCellLike] = (),
    stage3_grid: SweepGridConfig | None = None,
) -> tuple[LeaderboardTrialsRecord, ...]:
    """Aggregate every trial-stats sidecar into per-leaderboard frozen records.

    The ADR-0008 condition-1 builder: iterates the GRID TUPLES (not the specs
    — a tuple that backtested thousands of combos but ranked zero finalists
    still contributes its trials), then the Stage-3 refined cells, validating
    each sidecar's pairing identity (condition 5) and merging the moments in
    deterministic order via the slice-1 core.

    Per tuple/cell: store file ABSENT → the tuple never ran (no-bars skip) —
    not counted; store present + sidecar ABSENT → the leaderboard is marked
    ``complete=False`` (an old tree or a slice-2 preserve/mismatch skip);
    sidecar present → its ``tuple_digest`` AND ``store_content_hash`` must
    match the recomputed values or the build FAILS LOUDLY — drift is never
    silently absorbed into N.

    Parameters
    ----------
    results_root
        The sweep results root the grid persisted under.
    grid
        The grid config (its context recomputes each tuple's digest).
    stage3_cells
        The Stage-3 pass report's refined cells (empty when disabled).
    stage3_grid
        The TRAIN-hash grid context the cells' digests were written under
        (``dataclasses.replace(grid, data_snapshot_hash=train_hash)``);
        required when *stage3_cells* is non-empty.

    Returns
    -------
    tuple[LeaderboardTrialsRecord, ...]
        One record per (instrument, tf) leaderboard, sorted by key.

    Raises
    ------
    CascadeWireError
        On a pairing-identity mismatch (digest or store hash), an unreadable
        sidecar, or cells supplied without their grid context.
    """
    import hashlib

    from brutex.engine.sweep_orchestrator import (
        _tuple_digest,
        iter_sweep_tuples,
        tuple_store_path,
    )
    from brutex.engine.trial_stats import (
        TrialStatsError,
        TupleTrialStats,
        merge_tuple_trial_stats,
        read_tuple_trial_stats,
        trial_stats_sidecar_path,
    )

    if stage3_cells and stage3_grid is None:
        raise CascadeWireError(
            "build_leaderboard_trials: stage3_cells supplied without stage3_grid "
            "(the train-hash context their sidecar digests were written under)"
        )

    def _read_paired(store_path: Path, expected_digest: str) -> TupleTrialStats | None:
        """Read + pairing-validate one sidecar; ``None`` when absent (incomplete)."""
        sidecar = trial_stats_sidecar_path(store_path)
        if not sidecar.exists():
            return None
        try:
            stats = read_tuple_trial_stats(sidecar)
        except TrialStatsError as exc:
            raise CascadeWireError(f"unreadable trial-stats sidecar at {sidecar}: {exc}") from exc
        if stats.tuple_digest != expected_digest:
            raise CascadeWireError(
                f"trial-stats pairing mismatch at {sidecar}: sidecar digest "
                f"{stats.tuple_digest} != expected tuple digest {expected_digest}"
            )
        actual_hash = hashlib.sha256(store_path.read_bytes()).hexdigest()
        if stats.store_content_hash != actual_hash:
            raise CascadeWireError(
                f"trial-stats pairing mismatch at {sidecar}: sidecar store hash "
                f"{stats.store_content_hash} != actual store content {actual_hash}"
            )
        return stats

    parts: dict[tuple[str, str], list[TupleTrialStats]] = {}
    complete: dict[tuple[str, str], bool] = {}

    for tuple_ in iter_sweep_tuples(grid):
        store_path = tuple_store_path(results_root, tuple_, grid)
        if not store_path.exists():
            continue  # the tuple never ran (no bars) — contributes no trials
        key = (tuple_.instrument, tuple_.tf.label)
        complete.setdefault(key, True)
        stats = _read_paired(store_path, _tuple_digest(tuple_, grid))
        if stats is None:
            complete[key] = False
        else:
            parts.setdefault(key, []).append(stats)

    if stage3_cells and stage3_grid is not None:
        for cell in stage3_cells:
            key = (cell.tuple_.instrument, cell.tuple_.tf.label)
            complete.setdefault(key, True)
            stats = _read_paired(cell.path, _tuple_digest(cell.tuple_, stage3_grid))
            if stats is None:
                complete[key] = False
            else:
                parts.setdefault(key, []).append(stats)

    records: list[LeaderboardTrialsRecord] = []
    for key in sorted(complete):
        merged = merge_tuple_trial_stats(parts.get(key, []), complete=complete[key])
        records.append(
            LeaderboardTrialsRecord(
                instrument=key[0],
                tf_label=key[1],
                n_trials=merged.n_trials,
                n_finite=merged.n_finite,
                sum_sharpe=merged.sum_sharpe,
                sum_sharpe_sq=merged.sum_sharpe_sq,
                complete=merged.complete,
            )
        )
    return tuple(records)


# ── Manifest serialisation ────────────────────────────────────────────────────


def _spec_to_dict(spec: CascadeTupleSpec) -> dict[str, object]:
    """Serialise one :class:`CascadeTupleSpec` into a JSON-native dict."""
    payload: dict[str, object] = {
        "instrument": spec.instrument,
        "tf_label": spec.tf_label,
        "tf_minutes": spec.tf_minutes,
        "direction_label": spec.direction_label,
        "sl_paisa": spec.sl_paisa,
        "tp_paisa": spec.tp_paisa,
        "prior_n": spec.prior_n,
        "proximity_bps": None if spec.proximity_bps is None else list(spec.proximity_bps),
        "start_equity_paisa": spec.start_equity_paisa,
        "winners": [[run_id, mask] for run_id, mask in spec.winners],
        "fib_anchor": spec.fib_anchor.value,
        # DEC-FILL-WORST-CASE-ONLY-001: the manifest carries the UNCONDITIONAL
        # "fill_mode": "worst_case" key (the canonical-params law's manifest
        # twin) — the reader REFUSES a manifest without it (precise-era) so a
        # manual replay can never re-score precise-priced winners.
        "fill_mode": FillMode.WORST_CASE.value,
        # DEFAULT-OMITTED (DEC-NIFTY-PRICE-SOURCE-AXIS-001, Override 1) via the ONE
        # COMMON fragment (CLAUDE.md §11): the price-source keys are written only
        # when non-default, so a baseline manifest is byte-identical to a pre-axis one
        # (modulo the unconditional fill_mode key above — the two laws compose).
        **price_source_params_fragment(spec.vwap_source, spec.ema_source),
    }
    # EMIT-ON-PRESENT (DEC-CASCADE-STAT-TIER-WIRE-001): the Stage-3 search
    # family is written only when recorded, so a family-less spec (a pure
    # grid winner / pre-family era) serialises byte-identically to before.
    if spec.family_sl_tp is not None:
        payload["family_sl_tp"] = [[sl, tp] for sl, tp in spec.family_sl_tp]
    return payload


def _spec_from_dict(data: dict[str, object]) -> CascadeTupleSpec:
    """Rebuild a :class:`CascadeTupleSpec` from :func:`_spec_to_dict` output."""
    # The manifest is JSON (typed ``dict[str, object]``); cast to ``Any`` so the
    # scalar coercions below read cleanly. Every malformed value is caught by the
    # ``(KeyError, TypeError, ValueError)`` guard and re-raised as a CascadeWireError.
    raw = cast(dict[str, Any], data)
    if raw.get("fill_mode") is None or str(raw["fill_mode"]) != FillMode.WORST_CASE.value:
        # DEC-FILL-WORST-CASE-ONLY-001 legacy-artifact refusal: worst-case is
        # the ONLY fill model and every post-DEC manifest carries the
        # unconditional "fill_mode": "worst_case" key. An ABSENT key or a
        # literal "precise" token identifies a PRECISE-era manifest whose
        # winners were ranked under fills the deleted mode produced —
        # re-scoring them here would silently launder precise-priced results
        # as worst-case. Refuse LOUDLY; the remediation is a fresh sweep.
        raise CascadeWireError(
            "precise-era cascade manifest refused (DEC-FILL-WORST-CASE-ONLY-001): "
            f"fill_mode={raw.get('fill_mode')!r} — worst_case is the only fill "
            "model; re-run the sweep to produce a worst-case manifest"
        )
    try:
        proximity = raw["proximity_bps"]
        proximity_bps = (
            None if proximity is None else (int(proximity[0]), int(proximity[1]), int(proximity[2]))
        )
        winners = tuple((str(run_id), int(mask)) for run_id, mask in raw["winners"])
        return CascadeTupleSpec(
            instrument=str(raw["instrument"]),
            tf_label=str(raw["tf_label"]),
            tf_minutes=int(raw["tf_minutes"]),
            direction_label=str(raw["direction_label"]),
            sl_paisa=None if raw["sl_paisa"] is None else int(raw["sl_paisa"]),
            tp_paisa=None if raw["tp_paisa"] is None else int(raw["tp_paisa"]),
            prior_n=int(raw["prior_n"]),
            proximity_bps=proximity_bps,
            start_equity_paisa=int(raw["start_equity_paisa"]),
            winners=winners,
            # Absent on pre-DEC-NIFTY-FIB-AXIS-001 manifests (additive parse):
            # default to prev-day, the only anchor those sweeps ran.
            fib_anchor=(
                FibAnchorKind.PREV_DAY
                if raw.get("fib_anchor") is None
                else FibAnchorKind(str(raw["fib_anchor"]))
            ),
            # DEFAULT-OMITTED (DEC-NIFTY-PRICE-SOURCE-AXIS-001): absent ⟺ the
            # baseline (HLC3, CLOSE), the only source pre-axis / baseline sweeps ran.
            vwap_source=(
                PriceSource.HLC3
                if raw.get("vwap_source") is None
                else PriceSource(str(raw["vwap_source"]))
            ),
            ema_source=(
                PriceSource.CLOSE
                if raw.get("ema_source") is None
                else PriceSource(str(raw["ema_source"]))
            ),
            # Absent on pre-DEC-CASCADE-STAT-TIER-WIRE-001 manifests (additive
            # parse): None = family unavailable → per-candidate PBO skipped.
            # A malformed value falls into the existing refusal guard below;
            # CascadeTupleSpec.__post_init__ enforces the family shape law.
            family_sl_tp=(
                None
                if raw.get("family_sl_tp") is None
                else tuple((int(sl), int(tp)) for sl, tp in raw["family_sl_tp"])
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CascadeWireError(f"invalid cascade tuple spec in manifest: {exc}") from exc


def _stage3_to_dict(record: Stage3ManifestRecord) -> dict[str, object]:
    """Serialise one :class:`Stage3ManifestRecord` into a JSON-native dict."""
    return {
        "enabled": record.enabled,
        "train_snapshot_hash": record.train_snapshot_hash,
        "refined_cells": [
            {
                "instrument": cell.instrument,
                "tf_label": cell.tf_label,
                "direction_label": cell.direction_label,
                "k": cell.k,
                "sl_paisa": cell.sl_paisa,
                "tp_paisa": cell.tp_paisa,
                "proximity_bps": None if cell.proximity_bps is None else list(cell.proximity_bps),
                "rel_path": cell.rel_path,
                "fib_anchor": cell.fib_anchor.value,
                # DEFAULT-OMITTED (DEC-NIFTY-PRICE-SOURCE-AXIS-001, Override 1) via
                # the ONE COMMON fragment: a baseline refined cell's manifest entry
                # is byte-identical to a pre-axis one.
                **price_source_params_fragment(cell.vwap_source, cell.ema_source),
            }
            for cell in record.refined_cells
        ],
    }


def _stage3_from_dict(data: dict[str, object]) -> Stage3ManifestRecord:
    """Rebuild a :class:`Stage3ManifestRecord` from :func:`_stage3_to_dict` output."""
    raw = cast(dict[str, Any], data)
    try:
        cells: list[Stage3CellRecord] = []
        for cell in raw["refined_cells"]:
            proximity = cell["proximity_bps"]
            cells.append(
                Stage3CellRecord(
                    instrument=str(cell["instrument"]),
                    tf_label=str(cell["tf_label"]),
                    direction_label=str(cell["direction_label"]),
                    k=int(cell["k"]),
                    sl_paisa=int(cell["sl_paisa"]),
                    tp_paisa=int(cell["tp_paisa"]),
                    proximity_bps=(
                        None
                        if proximity is None
                        else (int(proximity[0]), int(proximity[1]), int(proximity[2]))
                    ),
                    # Absent on pre-DEC-ROW-PROVENANCE-001 manifests (additive parse).
                    rel_path=(None if cell.get("rel_path") is None else str(cell["rel_path"])),
                    # Absent on pre-DEC-NIFTY-FIB-AXIS-001 manifests (additive
                    # parse): default to prev-day, the only anchor those sweeps ran.
                    fib_anchor=(
                        FibAnchorKind.PREV_DAY
                        if cell.get("fib_anchor") is None
                        else FibAnchorKind(str(cell["fib_anchor"]))
                    ),
                    # Absent on pre-DEC-NIFTY-PRICE-SOURCE-AXIS-001 / baseline
                    # manifests (additive parse): default to (HLC3, CLOSE), the only
                    # source those sweeps ran.
                    vwap_source=(
                        PriceSource.HLC3
                        if cell.get("vwap_source") is None
                        else PriceSource(str(cell["vwap_source"]))
                    ),
                    ema_source=(
                        PriceSource.CLOSE
                        if cell.get("ema_source") is None
                        else PriceSource(str(cell["ema_source"]))
                    ),
                )
            )
        hash_raw = raw["train_snapshot_hash"]
        return Stage3ManifestRecord(
            enabled=bool(raw["enabled"]),
            train_snapshot_hash=None if hash_raw is None else str(hash_raw),
            refined_cells=tuple(cells),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CascadeWireError(f"invalid stage3 record in manifest: {exc}") from exc


def _lbt_to_dict(record: LeaderboardTrialsRecord) -> dict[str, object]:
    """Serialise one :class:`LeaderboardTrialsRecord` into a JSON-native dict."""
    return {
        "instrument": record.instrument,
        "tf_label": record.tf_label,
        "n_trials": record.n_trials,
        "n_finite": record.n_finite,
        "sum_sharpe": record.sum_sharpe,
        "sum_sharpe_sq": record.sum_sharpe_sq,
        "complete": record.complete,
    }


def _lbt_from_dict(data: dict[str, object]) -> LeaderboardTrialsRecord:
    """Rebuild a :class:`LeaderboardTrialsRecord` from :func:`_lbt_to_dict` output."""
    raw = cast(dict[str, Any], data)
    try:
        return LeaderboardTrialsRecord(
            instrument=str(raw["instrument"]),
            tf_label=str(raw["tf_label"]),
            n_trials=int(raw["n_trials"]),
            n_finite=int(raw["n_finite"]),
            sum_sharpe=float(raw["sum_sharpe"]),
            sum_sharpe_sq=float(raw["sum_sharpe_sq"]),
            complete=bool(raw["complete"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CascadeWireError(f"invalid leaderboard_trials record in manifest: {exc}") from exc


def _roll_schedules_from_payload(
    raw: object,
) -> tuple[tuple[str, tuple[int, ...]], ...] | None:
    """Parse the additive ``roll_schedules`` manifest key (absent/None → None).

    The manifest carries no integrity digest, so a hand-edited/corrupted file
    is otherwise trusted verbatim — the presence ⟺ alias invariant
    (``SweepRunConfig.__post_init__``) and the schedule SHAPE law (non-empty,
    strictly increasing, non-negative ordinals) are therefore enforced HERE at
    the parse boundary (review round-3 finding 2): a ``roll_schedules`` entry
    for a SPOT instrument would silently rebuild that pool's ``bar_bits`` with
    per-contract hard resets, breaking the replay's byte-identity contract.

    Raises
    ------
    CascadeWireError
        On a malformed entry (non-pair rows, non-int ordinals, …), a
        DUPLICATE instrument (two contradictory schedules — ``dict()`` would
        silently last-win; review round-7 finding 8), an entry whose
        instrument is not the NIFTY-futures series alias, or a schedule
        violating the shape law.
    """
    if raw is None:
        return None

    def _strict_ordinal(ordinal: object) -> int:
        # Strict int-not-bool (review round-4 finding 2, mirroring the audit
        # reader `_schedules_from_entries`): a bare `int()` silently accepted
        # `true` (→1) and truncated `739000.9` (→739000) — a malformed
        # hand-edited manifest must REFUSE, never coerce.
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise ValueError(f"non-int roll_schedule ordinal {ordinal!r}")
        return ordinal

    try:
        parsed = tuple(
            (str(instrument), tuple(_strict_ordinal(ordinal) for ordinal in schedule))
            for instrument, schedule in cast("Sequence[Any]", raw)
        )
    except (TypeError, ValueError) as exc:
        raise CascadeWireError(f"invalid roll_schedules in manifest: {exc}") from exc
    seen: set[str] = set()
    for instrument, schedule in parsed:
        # Duplicate-instrument refusal (review round-7 finding 8): both
        # `dict(manifest.roll_schedules)` consumers silently adopt the LAST
        # entry, so an internally contradictory hand-edited manifest carrying
        # two schedules for one instrument replayed under whichever sorted
        # last — while the audit twin `_schedules_from_entries` refuses the
        # same per-instrument disagreement. Refuse identically HERE, at the
        # parse boundary this docstring claims owns the enforcement.
        if instrument in seen:
            raise CascadeWireError(
                f"invalid roll_schedules in manifest: duplicate entry for instrument "
                f"{instrument!r} — an internally contradictory manifest cannot pick a "
                "schedule (dict() would silently adopt the last); the manifest is "
                "corrupt — re-run `brutex sweep-all` to mint a fresh one"
            )
        seen.add(instrument)
        if not is_futures_series_symbol(instrument):
            raise CascadeWireError(
                f"invalid roll_schedules in manifest: instrument {instrument!r} is not "
                "the NIFTY-futures series alias — a roll schedule may exist for (and "
                "only for) the futures alias (presence ⟺ alias); the manifest is "
                "corrupt — re-run `brutex sweep-all` to mint a fresh one"
            )
        if not schedule or list(schedule) != sorted(set(schedule)) or schedule[0] < 0:
            raise CascadeWireError(
                f"invalid roll_schedules in manifest: schedule for {instrument!r} must "
                "be a non-empty strictly increasing tuple of non-negative day "
                f"ordinals, got {list(schedule)}"
            )
    return parsed


def manifest_to_json(manifest: CascadeManifest) -> bytes:
    """Return the deterministic JSON bytes of *manifest* (sorted keys)."""
    payload = {
        "version": manifest.version,
        "sweep_id": manifest.sweep_id,
        "cascade_config": cascade_config_to_dict(manifest.cascade_config),
        "bars_context": {
            "exchange": manifest.bars_context.exchange,
            "segment": manifest.bars_context.segment,
            "start": manifest.bars_context.start,
            "end": manifest.bars_context.end,
        },
        "specs": [_spec_to_dict(spec) for spec in manifest.specs],
        "stage3": None if manifest.stage3 is None else _stage3_to_dict(manifest.stage3),
        "leaderboard_trials": (
            None
            if manifest.leaderboard_trials is None
            else [_lbt_to_dict(record) for record in manifest.leaderboard_trials]
        ),
        "roll_schedules": (
            None
            if manifest.roll_schedules is None
            else [[instrument, list(schedule)] for instrument, schedule in manifest.roll_schedules]
        ),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )


def manifest_from_json(data: bytes) -> CascadeManifest:
    """Parse + validate a manifest written by :func:`manifest_to_json`.

    Raises
    ------
    CascadeWireError
        On malformed JSON, an unsupported version, or any bad field.
    """
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CascadeWireError(f"cascade manifest is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise CascadeWireError("cascade manifest must be a JSON object")
    version = payload.get("version")
    if version != MANIFEST_VERSION:
        raise CascadeWireError(
            f"unsupported cascade manifest version {version!r} (expected {MANIFEST_VERSION})"
        )
    try:
        bars_raw = payload["bars_context"]
        bars_context = BarsContext(
            exchange=str(bars_raw["exchange"]),
            segment=str(bars_raw["segment"]),
            start=str(bars_raw["start"]),
            end=str(bars_raw["end"]),
        )
        specs = tuple(_spec_from_dict(spec) for spec in payload["specs"])
        # `stage3` is absent from pre-DEC-STAGE3-WIRE-001 manifests → None
        # (backward-compatible parse; same MANIFEST_VERSION, purely additive).
        stage3_raw = payload.get("stage3")
        # `leaderboard_trials` is absent from pre-DEC-TRIAL-STATS-FREEZE-001
        # manifests → None (the same additive-at-version-1 pattern as stage3).
        lbt_raw = payload.get("leaderboard_trials")
        return CascadeManifest(
            version=MANIFEST_VERSION,
            sweep_id=str(payload["sweep_id"]),
            cascade_config=cascade_config_from_dict(payload["cascade_config"]),
            bars_context=bars_context,
            specs=specs,
            stage3=None if stage3_raw is None else _stage3_from_dict(stage3_raw),
            leaderboard_trials=(
                None if lbt_raw is None else tuple(_lbt_from_dict(item) for item in lbt_raw)
            ),
            # `roll_schedules` is absent from pre-carriage manifests → None
            # (the same additive-at-version-1 pattern as stage3).
            roll_schedules=_roll_schedules_from_payload(payload.get("roll_schedules")),
        )
    except (KeyError, TypeError) as exc:
        raise CascadeWireError(f"cascade manifest missing/invalid field: {exc}") from exc


# ── Spec building from a persisted per-tuple store ─────────────────────────────


def spec_from_store_rows(
    *,
    instrument: str,
    tf_label: str,
    tf_minutes: int,
    direction_label: str,
    sl_paisa: int | None,
    tp_paisa: int | None,
    prior_n: int,
    proximity_bps: tuple[int, int, int] | None,
    start_equity_paisa: int,
    rows: Sequence[ResultRow],
    top_k: int | None = None,
    fib_anchor: FibAnchorKind = FibAnchorKind.PREV_DAY,
    vwap_source: PriceSource = PriceSource.HLC3,
    ema_source: PriceSource = PriceSource.CLOSE,
    family_sl_tp: tuple[tuple[int, int], ...] | None = None,
) -> CascadeTupleSpec | None:
    """Build a :class:`CascadeTupleSpec` from one tuple's persisted finalists.

    Selects the top-``top_k`` *rows* by Sharpe (ties broken by ``run_id`` ascending
    — deterministic, matching :meth:`ResultsStore.top_k`) as the tuple's cascade
    winners. Returns ``None`` when *rows* is empty (nothing to cascade).
    ``family_sl_tp`` (DEC-CASCADE-STAT-TIER-WIRE-001) is carried verbatim onto
    the spec — the Stage-3 build path
    (:func:`brutex.engine.stage3_wire.cascade_specs_for_refined_cells`)
    supplies each refined cell's per-mask search family; the default ``None``
    keeps every other caller byte-identical.

    Parameters
    ----------
    instrument, tf_label, tf_minutes, direction_label, sl_paisa, tp_paisa, prior_n, proximity_bps, start_equity_paisa
        The tuple's identity + trade-sim knobs (carried onto the spec).
    rows
        The tuple's persisted :class:`~brutex.engine.results_store.ResultRow`\\s.
    top_k
        Keep only the top-``top_k`` by Sharpe, or ``None`` for all.
    fib_anchor
        The swept Fib-anchor family the tuple was evaluated against
        (DEC-NIFTY-FIB-AXIS-001); carried onto the spec so the Stage-4 cascade
        re-scores against the tuple's OWN Fib levels. Default prev-day keeps the
        prev-day path byte-identical.
    vwap_source, ema_source
        The swept VWAP / EMA price-source cell the tuple was evaluated against
        (DEC-NIFTY-PRICE-SOURCE-AXIS-001); carried onto the spec so the Stage-4
        cascade re-scores against the tuple's OWN source bits. Default
        ``(HLC3, CLOSE)`` keeps the baseline path byte-identical.

    Returns
    -------
    CascadeTupleSpec | None
        The spec, or ``None`` if *rows* is empty.
    """
    if not rows:
        return None
    ordered = sorted(rows, key=lambda row: row.run_id)
    ordered.sort(key=lambda row: row.sharpe_ratio, reverse=True)
    selected = ordered if top_k is None else ordered[:top_k]
    winners = tuple((row.run_id, row.mask_bits) for row in selected)
    return CascadeTupleSpec(
        instrument=instrument,
        tf_label=tf_label,
        tf_minutes=tf_minutes,
        direction_label=direction_label,
        sl_paisa=sl_paisa,
        tp_paisa=tp_paisa,
        prior_n=prior_n,
        proximity_bps=proximity_bps,
        start_equity_paisa=start_equity_paisa,
        winners=winners,
        fib_anchor=fib_anchor,
        vwap_source=vwap_source,
        ema_source=ema_source,
        family_sl_tp=family_sl_tp,
    )


def build_stage2_cascade_specs(
    results_root: Path,
    grid: SweepGridConfig,
    *,
    top_k: int | None,
) -> list[CascadeTupleSpec]:
    """Build the Stage-2 grid-winner cascade specs for a completed sweep.

    Iterates every :func:`~brutex.engine.sweep_orchestrator.iter_sweep_tuples`
    tuple, loads its persisted store (skipping a tuple that never ran — no bars),
    and turns its top-``top_k`` finalists into a :class:`CascadeTupleSpec` via
    :func:`spec_from_store_rows`. Each tuple's OWN swept ``fib_anchor`` is
    threaded onto its spec (DEC-NIFTY-FIB-AXIS-001) so the Stage-4 cascade
    re-scores a non-prev-day survivor against its OWN Fib levels — the
    grid-winner twin of :func:`~brutex.engine.stage3_wire.cascade_specs_for_refined_cells`.

    This is the pragma-free extraction of the Stage-2 assembly loop that used to
    live inline in the ``cmd_sweep_all`` orchestration shim, so the anchor thread
    is unit-testable (review MAJOR 2, DEC-NIFTY-FIB-AXIS-001).

    Parameters
    ----------
    results_root
        The sweep results root the grid persisted its per-tuple stores under.
    grid
        The completed sweep's grid config (drives the tuple enumeration + the
        store-path resolution).
    top_k
        Keep only the top-``top_k`` finalists per tuple by Sharpe, or ``None``
        for all — matching the ``top`` the sweep launcher passes.

    Returns
    -------
    list[CascadeTupleSpec]
        One spec per non-empty grid tuple, in ``iter_sweep_tuples`` order.
    """
    from brutex.engine.results_store import ResultsStore
    from brutex.engine.sweep_orchestrator import iter_sweep_tuples, tuple_store_path
    from brutex.engine.sweep_run import _direction_label

    specs: list[CascadeTupleSpec] = []
    for sweep_tuple in iter_sweep_tuples(grid):
        store_path = tuple_store_path(results_root, sweep_tuple, grid)
        if not store_path.exists():
            continue
        proximity_bps = (
            None
            if sweep_tuple.proximity is None
            else (
                sweep_tuple.proximity.pivot_proximity_bps,
                sweep_tuple.proximity.pdh_pdl_proximity_bps,
                sweep_tuple.proximity.fib_proximity_bps,
            )
        )
        spec = spec_from_store_rows(
            instrument=sweep_tuple.instrument,
            tf_label=sweep_tuple.tf.label,
            tf_minutes=sweep_tuple.tf.minutes,
            direction_label=_direction_label(sweep_tuple.direction),
            sl_paisa=sweep_tuple.sl_paisa,
            tp_paisa=sweep_tuple.tp_paisa,
            prior_n=grid.prior_n,
            proximity_bps=proximity_bps,
            start_equity_paisa=grid.start_equity_paisa,
            rows=ResultsStore.load(store_path).rows(),
            top_k=top_k,
            fib_anchor=sweep_tuple.fib_anchor,
            vwap_source=sweep_tuple.price_source.vwap_source,
            ema_source=sweep_tuple.price_source.ema_source,
        )
        if spec is not None:
            specs.append(spec)
    return specs


# ── Output paths + IO ─────────────────────────────────────────────────────────


def manifest_path(results_root: Path) -> Path:
    """Return the manifest path under *results_root* (``<root>/cascade/manifest.json``)."""
    return results_root / CASCADE_DIRNAME / MANIFEST_FILENAME


def report_path(results_root: Path) -> Path:
    """Return the report path under *results_root* (``<root>/cascade/cascade-report.json``)."""
    return results_root / CASCADE_DIRNAME / REPORT_FILENAME


def write_manifest(results_root: Path, manifest: CascadeManifest) -> Path:
    """Atomically write *manifest* JSON under *results_root* and return its path.

    G7 (DEC-SWEEP-UNKILLABLE-CORE-001): tmp→fsync→``os.replace`` via the COMMON
    :func:`~brutex.data.atomic_io.atomic_write_simple` — a crash mid-write can
    never leave a torn manifest for the replay to consume (same bytes, atomic
    rename only).
    """
    from brutex.data.atomic_io import atomic_write_simple

    path = manifest_path(results_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = manifest_to_json(manifest)
    atomic_write_simple(path, lambda tmp: tmp.write_bytes(content))
    return path


def write_report(results_root: Path, report: CascadeWireReport) -> Path:
    """Atomically write the serialised *report* JSON under *results_root*.

    ATOMIC by TWO independent in-flight demands, converged at the 2026-07-11
    merge-reconcile to the SAME mechanism — the guard-free
    :func:`brutex.data.atomic_io.atomic_write_simple`
    (tmp → fsync → ``os.replace``, the engine-sidecar precedent of
    DEC-ATOMIC-WRITE-CONSOLIDATE-001):

    - G7 (DEC-SWEEP-UNKILLABLE-CORE-001): the same atomic law as
      :func:`write_manifest` — a crash mid-write can never leave a torn
      report for the replay to consume.
    - DEC-RESULTS-CASCADE-VERDICT-JOIN-001: the ``/results`` poller now READS
      this file concurrently — the previous bare ``write_bytes`` could be
      torn-read mid-rewrite.

    Bytes rendered are IDENTICAL (:func:`serialize_wire_report` — the
    auto==manual golden re-runs unchanged); only durability changed. Raw
    ``OSError`` propagates unchanged (the engine sidecar error contract).
    """
    path = report_path(results_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = serialize_wire_report(report)
    atomic_write_simple(path, lambda tmp: tmp.write_bytes(rendered))
    return path


#: Stage labels bound into the two cascade artifact stamps (G7 / design §3).
CASCADE_MANIFEST_STAGE: Final[str] = "cascade_manifest"
CASCADE_REPORT_STAGE: Final[str] = "cascade_report"


def manifest_snapshot_hash(
    manifest: CascadeManifest, *, bars_for: Callable[[str, str], Sequence[CandleBar]]
) -> str:
    """Compute the data-snapshot hash over the manifest's OWN referenced slices.

    The SAME shared derivation the sweep stamps its run identity with
    (:func:`brutex.engine.stage3_wire.data_snapshot_hash`, CLAUDE.md §2 Rule 4),
    computed over exactly the distinct ``(instrument, tf_label)`` pairs the
    manifest's specs reference — so the stamp written at sweep time and the
    hash recomputed at replay time compare equal IFF the underlying bars are
    unchanged (G7). Honest envelope: O(referenced slices) bar loads via
    *bars_for*, not O(1).

    Parameters
    ----------
    manifest
        The cascade manifest whose specs name the slices.
    bars_for
        ``(instrument, tf_label) -> bars`` loader.

    Returns
    -------
    str
        The 64-hex snapshot digest.
    """
    from brutex.engine.snapshot_hash import data_snapshot_hash

    pairs = sorted({(spec.instrument, spec.tf_label) for spec in manifest.specs})
    slices = {(instrument, tf): list(bars_for(instrument, tf)) for instrument, tf in pairs}
    ctx = manifest.bars_context
    return data_snapshot_hash(
        exchange=ctx.exchange, segment=ctx.segment, start=ctx.start, end=ctx.end, slices=slices
    )


def stamp_cascade_artifacts(
    results_root: Path,
    manifest: CascadeManifest,
    *,
    bars_for: Callable[[str, str], Sequence[CandleBar]],
) -> None:
    """Stamp BOTH cascade artifacts (run-id + snapshot-hash bound) — G7 write side.

    Each stamp carries the manifest's ``sweep_id`` as its ``run_id`` (D-1: the
    sweep identity IS the run identity) plus the ``data_snapshot_hash`` extra
    computed over the manifest's own referenced slices.

    Raises
    ------
    brutex.observability.stage_stamp.StageStampError
        If either artifact is missing or a stamp cannot be written — the
        AUTOMATIC caller treats that as "artifact unstamped" (fail-soft WARN
        at the call site, failure-matrix A14/F2; an unstamped artifact is
        simply rebuilt, the safe direction).
    """
    from brutex.observability.stage_stamp import SNAPSHOT_EXTRA_KEY, write_stage_stamp

    extra = {SNAPSHOT_EXTRA_KEY: manifest_snapshot_hash(manifest, bars_for=bars_for)}
    write_stage_stamp(
        manifest_path(results_root),
        stage=CASCADE_MANIFEST_STAGE,
        run_id=manifest.sweep_id,
        extra=extra,
    )
    write_stage_stamp(
        report_path(results_root),
        stage=CASCADE_REPORT_STAGE,
        run_id=manifest.sweep_id,
        extra=extra,
    )


def load_manifest(results_root: Path) -> CascadeManifest:
    """Read + parse the manifest under *results_root*.

    Raises
    ------
    CascadeWireError
        If no manifest exists under *results_root* (run a sweep first), or it is
        malformed.
    """
    path = manifest_path(results_root)
    if not path.exists():
        raise CascadeWireError(
            f"no cascade manifest at {path} — run `brutex sweep-all` first "
            f"(the sweep writes the manifest its winners are cascaded from)"
        )
    return manifest_from_json(path.read_bytes())


# ── End-to-end (auto path + manual path) ──────────────────────────────────────


def run_and_write_from_specs(
    results_root: Path,
    *,
    sweep_id: str,
    specs: Sequence[CascadeTupleSpec],
    bars_for: Callable[[str, str], Sequence[CandleBar]],
    bars_context: BarsContext,
    cascade_config: CascadeConfig,
    stage3: Stage3ManifestRecord | None = None,
    leaderboard_trials: tuple[LeaderboardTrialsRecord, ...] | None = None,
    roll_schedule_by_instrument: Mapping[str, tuple[int, ...]] | None = None,
) -> CascadeWireReport:
    """AUTOMATIC path: run the cascade + write the manifest AND the report.

    Builds the :class:`CascadeManifest` from *specs* (so the MANUAL path can
    replay it bit-for-bit), runs :func:`run_cascade_over_specs`, then writes both
    ``cascade/manifest.json`` and ``cascade/cascade-report.json`` under
    *results_root*. When the sweep ran the Stage-3 refinement pass
    (DEC-STAGE3-WIRE-001), its :class:`Stage3ManifestRecord` is recorded in the
    manifest so refined-cell specs stay distinguishable from grid-cell specs.
    ``roll_schedule_by_instrument`` is the launch's resolved ADR-0012 futures
    roll-schedule map (the SAME object ``cmd_sweep_all`` threads into the
    grid): it drives the futures pools' Stage-1A hard resets here AND is
    recorded in the manifest (``roll_schedules``) so the MANUAL replay uses
    the identical schedule (review round-1 — never a schedule-blind
    re-score). Replay caveat: the manifest schedule is the FROZEN launch
    truth; a lake whose futures contract set changed since the sweep is
    REFUSED by the replay's schedule cross-check
    (:func:`check_replay_roll_schedule`, review round-2 findings 1/4/9 —
    enforced in :func:`load_bars_for_manifest`, never a silent re-resolve
    under the frozen resets).
    The candidate re-simulations are DISCARDED trade streams and stay
    deliberately unstamped (DEC-VIX-STAMP-WIRE-001 round-6 — see
    :func:`build_candidate_input`); no stamper seam exists here.

    Returns
    -------
    CascadeWireReport
        The aggregate cascade report (also written to disk).
    """
    manifest = CascadeManifest(
        version=MANIFEST_VERSION,
        sweep_id=sweep_id,
        cascade_config=cascade_config,
        bars_context=bars_context,
        specs=tuple(specs),
        stage3=stage3,
        leaderboard_trials=leaderboard_trials,
        roll_schedules=(
            None
            if roll_schedule_by_instrument is None
            else tuple(
                sorted(
                    (instrument, tuple(schedule))
                    for instrument, schedule in roll_schedule_by_instrument.items()
                )
            )
        ),
    )
    report = run_cascade_over_specs(
        specs,
        bars_for=bars_for,
        config=cascade_config,
        sweep_id=sweep_id,
        leaderboard_trials=leaderboard_trials,
        roll_schedules=roll_schedule_by_instrument,
    )
    write_manifest(results_root, manifest)
    write_report(results_root, report)
    # G7 write side (DEC-SWEEP-UNKILLABLE-CORE-001): stamp both artifacts after
    # they are durably on disk (artifact first, stamp second — I-5). A stamp
    # failure is fail-soft (A14/F2): the artifacts stand unstamped — the replay
    # then proceeds on the documented legacy/unstamped path — never a lost run.
    try:
        stamp_cascade_artifacts(results_root, manifest, bars_for=bars_for)
    except Exception as exc:  # fail-soft law (I-3) — stamping is observability
        _LOG.warning(
            "cascade_wire.stamp_failed",
            results_root=str(results_root),
            error=str(exc),
            error_type=type(exc).__name__,
        )
    return report


def run_and_write_from_manifest(
    results_root: Path,
    *,
    bars_for: Callable[[str, str], Sequence[CandleBar]],
) -> CascadeWireReport:
    """MANUAL path: replay the manifest this sweep wrote → the SAME report bytes.

    Reloads the manifest under *results_root*, re-runs :func:`run_cascade_over_specs`
    with the stored config + specs over freshly-loaded bars, and re-writes
    ``cascade/cascade-report.json``. Given an unchanged lake, the rewritten report
    is byte-identical to the one the AUTOMATIC path wrote (the golden
    contract). The replay builds NO India-VIX stamper (DEC-VIX-STAMP-WIRE-001
    round-6): the candidate re-simulations are discarded streams, so the old
    per-replay ``vix_stamper_for_manifest`` build paid a full window-bounded
    VIX lake read for zero observable output.

    Returns
    -------
    CascadeWireReport
        The aggregate cascade report (also written to disk).

    G7 read side (DEC-SWEEP-UNKILLABLE-CORE-001): BEFORE consuming, the
    manifest's stamp is validated (torn/foreign-run stamps REFUSE with the
    exact verdict — rebuild this link via ``brutex sweep-all``, artifacts
    1..N-1 untouched) and, when the stamp carries the ``data_snapshot_hash``
    extra, the hash is recomputed over FRESHLY loaded bars — a changed lake
    REFUSES naming both hashes instead of silently rewriting a
    wrong-provenance "golden" report. A manifest with NO stamp (written before
    this change, or by a direct :func:`write_manifest` call) proceeds with ONE
    ``cascade_wire.replay_unstamped`` WARN — honest back-compat, never a
    broken lake.

    Raises
    ------
    CascadeWireError
        If no manifest exists under *results_root*, it is malformed, its
        stamp is torn/mismatched, or the lake bars drifted from the stamped
        ``data_snapshot_hash``.
    """
    from brutex.observability.stage_stamp import (
        SNAPSHOT_EXTRA_KEY,
        StampVerdict,
        validate_stage_stamp,
    )

    manifest = load_manifest(results_root)
    verdict, stamp = validate_stage_stamp(
        manifest_path(results_root),
        expected_stage=CASCADE_MANIFEST_STAGE,
        expected_run_id=manifest.sweep_id,
    )
    if verdict is StampVerdict.MISSING_STAMP:
        _LOG.warning("cascade_wire.replay_unstamped", results_root=str(results_root))
    elif verdict is not StampVerdict.OK:
        raise CascadeWireError(
            f"cascade manifest stamp verdict is {verdict.value!r} — refusing the replay "
            f"(a torn/foreign manifest must be rebuilt, not consumed); re-run "
            f"`brutex sweep-all` to rebuild this link (earlier artifacts are untouched)"
        )
    stamped_snapshot = stamp.extra.get(SNAPSHOT_EXTRA_KEY) if stamp is not None else None
    if verdict is StampVerdict.OK and stamped_snapshot is not None:
        fresh_snapshot = manifest_snapshot_hash(manifest, bars_for=bars_for)
        if fresh_snapshot != stamped_snapshot:
            raise CascadeWireError(
                f"cascade replay REFUSED: the lake bars changed since this manifest was "
                f"written — stamped data_snapshot_hash {stamped_snapshot} != freshly "
                f"computed {fresh_snapshot}; re-run `brutex sweep-all` (the manifest's "
                f"provenance no longer matches the lake)"
            )
    report = run_cascade_over_specs(
        manifest.specs,
        bars_for=bars_for,
        config=manifest.cascade_config,
        sweep_id=manifest.sweep_id,
        # The FROZEN numbers — the manifest is the only replay-time source
        # (ADR-0008 condition 1; sidecars are never re-read here).
        leaderboard_trials=manifest.leaderboard_trials,
        # The FROZEN ADR-0012 futures roll schedules the launch recorded —
        # a futures pool replays under the SAME per-contract hard resets
        # (a pre-carriage manifest carries None → that pool skips loudly).
        roll_schedules=(None if manifest.roll_schedules is None else dict(manifest.roll_schedules)),
    )
    write_report(results_root, report)
    if verdict is StampVerdict.OK:
        # Re-stamp the just-rewritten report (byte-identical content given the
        # verified-unchanged lake; the fresh stamp keeps I-5 sound). Fail-soft.
        try:
            stamp_cascade_artifacts(results_root, manifest, bars_for=bars_for)
        except Exception as exc:  # fail-soft law (I-3)
            _LOG.warning(
                "cascade_wire.stamp_failed",
                results_root=str(results_root),
                error=str(exc),
                error_type=type(exc).__name__,
            )
    return report


def check_replay_roll_schedule(
    *,
    instrument: str,
    frozen_schedules: Mapping[str, tuple[int, ...]] | None,
    fresh_schedule: tuple[int, ...],
) -> None:
    """Refuse a manifest replay whose lake re-derivation drifted from the frozen schedule.

    ADR-0012 replay-consistency law (review round-2 findings 1/4/9): the MANUAL
    ``brutex cascade`` replay loads bars from the CURRENT lake but re-scores
    under the manifest's FROZEN ``roll_schedules``. If the lake's futures
    contract set changed since the sweep (a head contract backfilled after the
    launch's ``head_unowned`` WARN, the next front month pulled, a contract
    quarantined), the freshly stitched bars would silently desync from the
    frozen Stage-1A hard resets — either a silent D11 violation (indicators
    crossing a roll basis gap the frozen schedule never keys) or a
    misattributed "genuine bug" desync raise. Compare the two schedules and
    refuse LOUDLY naming both instead.

    Parameters
    ----------
    instrument
        The futures series alias instrument being replayed.
    frozen_schedules
        The manifest's recorded ``roll_schedules`` as a mapping (``None`` for
        a pre-carriage manifest — that pool is loudly SKIPPED by
        :func:`run_cascade_over_specs`, so no comparison exists to enforce).
    fresh_schedule
        The roll schedule re-derived from the CURRENT lake
        (:func:`~brutex.data.futures_series.resolve_futures_series`).

    Raises
    ------
    CascadeWireError
        When the manifest names *instrument* and its frozen schedule differs
        from *fresh_schedule* — the message names BOTH schedules and the
        remediation (re-run ``brutex sweep-all``).
    """
    frozen = None if frozen_schedules is None else frozen_schedules.get(instrument)
    if frozen is None:
        # Pre-carriage manifest (or an unnamed instrument): there is no frozen
        # schedule to enforce — run_cascade_over_specs owns the loud pool skip.
        return
    if frozen != fresh_schedule:
        raise CascadeWireError(
            f"cascade replay REFUSED for {instrument}: the manifest's FROZEN "
            f"roll_schedule {list(frozen)} does not match the schedule re-derived "
            f"from the current lake {list(fresh_schedule)} — the lake's futures "
            "contract set changed since the sweep (a contract was pulled, "
            "backfilled or removed), so a replay under the frozen resets would "
            "silently diverge from the stitched bars (ADR-0012 D11); re-run "
            "`brutex sweep-all` to mint a manifest for the current lake"
        )


def _manifest_window_dt(value: str) -> datetime:
    """Parse a manifest :class:`BarsContext` window endpoint (naive ⇒ UTC).

    The ONE parse for :func:`load_bars_for_manifest`'s slice reads (CLAUDE.md
    §2 Rule 11 COMMON; the round-6-deleted ``vix_stamper_for_manifest``
    shared it while the manual replay still built a stamper).
    """
    from datetime import UTC, datetime

    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def load_bars_for_manifest(  # pragma: no cover - live DuckDB catalog + ReplayFeed
    manifest: CascadeManifest,
    *,
    lake_root: Path,
    catalog_path: Path | None = None,
) -> Callable[[str, str], list[CandleBar]]:
    """Build a ``bars_for`` over the lake for the MANUAL replay (live-lake adapter).

    Reloads each ``(instrument, tf_label)`` slice the manifest references from the
    Parquet lake via the SAME ``Catalog`` / ``ReplayFeed`` path the sweep used
    (reconciling the catalog from on-disk Parquet first, mirroring
    ``brutex sweep-all``), keyed by the manifest's :class:`BarsContext` window.
    Marked ``no cover``: it opens DuckDB + reads the real lake (the pure cascade
    core + the manifest round-trip are fully tested with injected bars; the L1
    heal wiring below is exercised end-to-end by
    ``tests/integration/test_cascade_reconcile_heal.py``; the FUTURES replay
    branch below — the once-per-replay ``resolve_futures_series`` +
    :func:`check_replay_roll_schedule` cross-check + the per-slice
    ``resolution=`` threading — is exercised end-to-end, with negative
    assertions, by ``tests/integration/test_sweep_all_futures_launch.py``'s
    ``test_manual_cascade_replays_then_refuses_a_drifted_lake``,
    ``test_dashboard_cascade_route_400s_a_drifted_lake`` and
    ``test_manual_cascade_interior_hole_is_a_clean_user_error``).

    DEC-LAKE-HEAL-001 (L1): the reconcile's free invariant piggyback returns
    any poisoned-partition findings on ``ReconcileResult.findings``; ONE
    :func:`~brutex.data.lake_heal.heal_after_reconcile` call heals them BEFORE
    the feed consumes the slice (auto mode; ``BRUTEX_LAKE_HEAL=observe`` logs
    only). ``LakeWriteHalt`` propagates to the CLI top-level halt handling.

    Parameters
    ----------
    manifest
        The replayed manifest (its ``bars_context`` + ``specs`` drive the loads).
    lake_root
        The lake root directory.
    catalog_path
        The catalog DuckDB path (default ``<lake_root>/catalog.duckdb``).

    Returns
    -------
    Callable[[str, str], list[CandleBar]]
        A ``(instrument, tf_label) -> bars`` closure over the loaded slices.

    Raises
    ------
    CascadeWireError
        When the manifest carries a frozen ``roll_schedules`` entry for a
        futures instrument and the CURRENT lake re-derives a DIFFERENT
        schedule (:func:`check_replay_roll_schedule` — the lake's contract
        set changed since the sweep; both schedules are named).
    FuturesSeriesError
        Futures alias only: a resolution refusal from the current lake
        (interior hole, empty series, …) or a per-contract empty owned
        window — the same loud-refusal law as the launch mouth.
    """
    from brutex.calendars.nse import IST
    from brutex.data.catalog import Catalog
    from brutex.data.futures_series import FuturesSeriesResolution, resolve_futures_series
    from brutex.data.lake_heal import heal_after_reconcile, lake_heal_audit
    from brutex.data.lake_read import dedup_bars
    from brutex.feed.series_load import load_sweep_slice
    from brutex.notifications.sweep_alerts import emit_lake_heal_alert

    ctx = manifest.bars_context
    catalog = catalog_path if catalog_path is not None else lake_root / "catalog.duckdb"
    coords = {(spec.instrument, spec.tf_label) for spec in manifest.specs}
    # ADR-0012 replay-consistency gate (review round-2 findings 1/4/9): resolve
    # the futures alias ONCE over the manifest's window, REFUSE loudly when the
    # re-derived schedule drifted from the manifest's FROZEN one, and pass the
    # validated resolution into every slice load — so the loader's ownership
    # cut and the replay's frozen Stage-1A resets provably share ONE schedule
    # (and the replay never repeats the O(FNO dirs) scan per (instrument, tf)).
    futures_instruments = sorted({i for i, _tf in coords if is_futures_series_symbol(i)})
    resolution: FuturesSeriesResolution | None = None
    if futures_instruments:
        resolution = resolve_futures_series(
            lake_root,
            start=_manifest_window_dt(ctx.start).astimezone(IST).date(),
            end=_manifest_window_dt(ctx.end).astimezone(IST).date(),
        )
        frozen_map = None if manifest.roll_schedules is None else dict(manifest.roll_schedules)
        for fut_instrument in futures_instruments:
            check_replay_roll_schedule(
                instrument=fut_instrument,
                frozen_schedules=frozen_map,
                fresh_schedule=resolution.roll_schedule,
            )
    slices: dict[tuple[str, str], list[CandleBar]] = {}
    with Catalog.open(catalog) as cat:

        def _heal_on_reconcile(result: ReconcileResult) -> None:
            if result.findings:
                # DEC-LAKE-HEAL-001 L1: heal what the reconcile detected for
                # free, so the sweep/replay reads clean bars. The audit sink is
                # opened lazily (findings-gated — a clean lake writes nothing).
                with lake_heal_audit(lake_root) as audit:
                    heal_after_reconcile(
                        result,
                        lake_root=lake_root,
                        catalog=cat,
                        audit=audit,
                        on_report=lambda r: emit_lake_heal_alert(r, scope="reconcile"),
                    )

        for instrument, tf_label in coords:
            # The ONE alias-aware slice loader (DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001):
            # spot/verbatim dirs take the moved reconcile + ReplayFeed block
            # byte-identically (the resolution param is never read on that
            # path); the NIFTY-futures series alias is roll-stitched from the
            # VALIDATED resolution above — never a fresh per-slice re-resolve.
            # The L1 heal hook rides the loader's on_reconcile seam.
            loaded = load_sweep_slice(
                cat=cat,
                lake_root=lake_root,
                exchange=ctx.exchange,
                segment=ctx.segment,
                symbol=instrument,
                interval=tf_label,
                start=_manifest_window_dt(ctx.start),
                end=_manifest_window_dt(ctx.end),
                resolution=resolution,
                on_reconcile=_heal_on_reconcile,
            )
            # Belt-and-braces read gate (DEC-LAKE-READ-UNIQUE-001): the feed's
            # Catalog.get_bars already collapses stored duplicates keep-FIRST;
            # this idempotent second pass pins the invariant at the cascade's
            # own input mouth (a clean stream passes through byte-identically).
            clean_bars, _dropped = dedup_bars(
                loaded,
                source=f"cascade_wire:{instrument}/{tf_label}",
            )
            slices[(instrument, tf_label)] = clean_bars

    def bars_for(instrument: str, tf_label: str) -> list[CandleBar]:
        return slices.get((instrument, tf_label), [])

    return bars_for


__all__ = [
    "CASCADE_DIRNAME",
    "CASCADE_MANIFEST_STAGE",
    "CASCADE_REPORT_STAGE",
    "MANIFEST_FILENAME",
    "MANIFEST_VERSION",
    "REPORT_FILENAME",
    "REPORT_VERSION",
    "BarsContext",
    "CascadeManifest",
    "CascadeTupleSpec",
    "CascadeWireError",
    "CascadeWireReport",
    "LeaderboardTrialsRecord",
    "Stage3CellRecord",
    "Stage3ManifestRecord",
    "build_candidate_input",
    "build_leaderboard_trials",
    "candidates_for_spec",
    "cascade_config_for_sweep",
    "cascade_config_from_dict",
    "cascade_config_to_dict",
    "check_replay_roll_schedule",
    "load_bars_for_manifest",
    "load_manifest",
    "manifest_from_json",
    "manifest_path",
    "manifest_snapshot_hash",
    "manifest_to_json",
    "report_path",
    "resolve_cascade_embargo_bars",
    "run_and_write_from_manifest",
    "run_and_write_from_specs",
    "run_cascade_over_specs",
    "serialize_wire_report",
    "spec_from_store_rows",
    "stamp_cascade_artifacts",
    "wire_report_to_dict",
    "write_manifest",
    "write_report",
]
