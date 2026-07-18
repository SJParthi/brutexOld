"""The meta-annotation pass — the ``<stem>.meta.json`` FOURTH sidecar
(DEC-META-LABEL-ANNOTATE-001) + the abstention replay block
(DEC-ABSTAIN-GATE-STREAM-001).

For every finalist combo with a results-depth ``.trades`` sidecar under
``sweep_runs/<run_id>/results-depth/`` the pass extracts the fixed feature
vector per trade, fits the strictly out-of-fold CPCV P(win) column, sweeps
the τ grid, replays the DEFAULT ILLUSTRATIVE abstention config over every
trade's ENTRY snapshot, and writes ONE canonical-JSON ``<stem>.meta.json``
beside the ``.trades``/``.aggs``/``.equity`` trio — plus one run-level
``meta_annotation.json`` summary at the run dir.

ANNOTATION-ONLY, ZERO-RISK (the R13 law): ``top100.json`` and every other
existing artifact stay BYTE-UNTOUCHED — no re-rank, no key added. The
per-entry display keys ``meta_label`` / ``abstain_gate`` are RESERVED names
(recorded in the DEC) for the named display follow-up; this pass never
writes them.

Determinism + idempotence: canonical JSON (``sort_keys`` + compact
separators + trailing newline — the trade_store stamp discipline), NO
timestamps, combos keyed by sorted ``run_id``, trades keyed by
``trade_seq`` — a re-run over unchanged sidecars is BYTE-IDENTICAL
(integration-pinned). Trades are keyed ONLY by ``trade_seq`` +
entry/exit TIMESTAMPS (the W4 fold-key law — never bar indices or
holding-bar quantities).

Correct-or-absent: a stem whose stamp fails validation, or whose trades
file is unreadable, is a COUNTED skip (named reason) — the pass continues,
never aborts, never fabricates. ``MemoryError`` / ``KeyboardInterrupt`` /
``SystemExit`` always re-raise raw.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import structlog

from brutex.data.atomic_io import atomic_write_simple
from brutex.engine.session_lens import SessionLens, SessionLensError, build_session_lens
from brutex.engine.trade_store import (
    RESULTS_DEPTH_DIRNAME,
    SidecarPaths,
    iter_trades_for_export,
    load_stamp,
    sweep_run_dir,
)
from brutex.meta.abstain import (
    ABSTAIN_REASON_ORDER,
    REGIME_CODES,
    AbstainGateConfig,
    default_abstain_config,
    reasons_from_mask,
)
from brutex.meta.cpcv_fit import OOF_N_GROUPS, OOF_N_TEST_GROUPS, oof_p_win
from brutex.meta.features import FEATURE_NAMES, TradeFeatures, trade_features_from_wire
from brutex.meta.logistic import DEFAULT_MAX_ITER, DEFAULT_RIDGE
from brutex.meta.meta_rust import abstain_reason_masks
from brutex.meta.threshold import N_THRESHOLDS_SWEPT, tau_curve

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from brutex.data.models import CandleBar
    from brutex.meta.cpcv_fit import Scorer

_LOG = structlog.get_logger(__name__)

__all__ = [
    "META_SCHEMA_VERSION",
    "META_SIDECAR_SUFFIX",
    "META_SUMMARY_FILENAME",
    "MetaAnnotationReport",
    "StemOutcome",
    "annotate_run",
]

#: The fourth sidecar suffix beside ``.trades``/``.aggs``/``.equity``.
META_SIDECAR_SUFFIX: Final[str] = ".meta.json"

#: The run-level summary filename at ``sweep_runs/<run_id>/``.
META_SUMMARY_FILENAME: Final[str] = "meta_annotation.json"

#: Sidecar schema version — bump on ANY shape change.
META_SCHEMA_VERSION: Final[int] = 1

#: The two decision IDs every sidecar echoes.
_DEC_META: Final[str] = "DEC-META-LABEL-ANNOTATE-001"
_DEC_ABSTAIN: Final[str] = "DEC-ABSTAIN-GATE-STREAM-001"

#: kept_pct echo thresholds (all members of the τ grid; pinned by tests).
_KEPT_PCT_TAUS: Final[tuple[float, ...]] = (0.6, 0.7, 0.8)

#: The honest-limits notes every sidecar carries VERBATIM (wire-stable).
_HONEST_NOTES: Final[tuple[str, ...]] = (
    "annotation-only: nothing is gated, filtered or re-ranked anywhere; a "
    "gating flip requires its own DEC + operator ruling and re-validation "
    "of the gated stream as its OWN strategy (MIN_TRADES=30 floor + DSR "
    "effective-N shrink with fewer trades).",
    "p_win is strictly out-of-fold (CPCV purge+embargo, label_span/embargo "
    "= one conservative trade-index maximum-overlap value — an "
    "approximation that over-purges, never leaks).",
    "expect near-random precision curves for most strategies: tau=0.8 "
    "keeps ~10-25% of trades ONLY under genuine skill — a flat curve is "
    "the honest common outcome, not a defect.",
    "the tau sweep is a 10-way selection over the same trades: a future "
    "gating DEC must fold n_thresholds_swept into its DSR trial count "
    "(anti-double-dipping).",
    "VIX stays REFERENCE-ONLY: this annotation READS the stored "
    "vix_entry stamps, never adds VIX to the 60-bit vocabulary; gating on "
    "VIX would make it load-bearing — a recorded operator collision point.",
    "training reuses cascade-validated trades under the OOF discipline "
    "only (the disclosed double-dip boundary).",
)


@dataclass(frozen=True, slots=True)
class StemOutcome:
    """One stem's outcome: annotated (with counts) or skipped (with reason)."""

    stem_rel: str
    annotated: bool
    reason: str | None
    combos: int
    trades: int
    malformed_rows: int


@dataclass(frozen=True, slots=True)
class MetaAnnotationReport:
    """The pass report (the CLI's exit-code + summary source)."""

    run_id: str
    dry_run: bool
    stems_total: int
    stems_annotated: int
    stems_skipped: int
    combos_annotated: int
    trades_annotated: int
    outcomes: tuple[StemOutcome, ...]
    summary_path: str | None

    @property
    def ok(self) -> bool:
        """``True`` iff every discovered stem annotated (or dry-run)."""
        return self.dry_run or self.stems_skipped == 0


def _sidecar_paths(trades_path: Path) -> SidecarPaths:
    """Rebuild the trio+stamp paths from a discovered ``.trades.parquet``."""
    stem = trades_path.name[: -len(".trades.parquet")]
    base = trades_path.parent
    return SidecarPaths(
        trades=trades_path,
        aggs=base / f"{stem}.aggs.parquet",
        equity=base / f"{stem}.equity.parquet",
        stamp=base / f"{stem}.stamp.json",
    )


def _vix_band(paths: SidecarPaths, run_id: str) -> tuple[float | None, float | None]:
    """Read one combo's stamped aggs VIX band — best-effort, never raises.

    A missing/unreadable aggs sidecar, an absent row, or a null band reads
    ``(None, None)`` (no VIX gate in the default config — never a
    half-invented band).
    """
    try:
        import polars as pl

        frame = (
            pl.scan_parquet(paths.aggs)
            .filter(pl.col("run_id") == run_id)
            .select("vix_band_lo", "vix_band_hi")
            .collect()
        )
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return (None, None)
    if frame.height != 1:
        return (None, None)
    lo, hi = frame.row(0)
    return (
        None if lo is None else float(lo),
        None if hi is None else float(hi),
    )


def _abstain_block(
    trades: Sequence[TradeFeatures],
    p_win: Sequence[float | None],
    config: AbstainGateConfig,
) -> dict[str, Any]:
    """Replay the abstention config over every trade's ENTRY snapshot.

    Routes through the :func:`brutex.meta.meta_rust.abstain_reason_masks`
    facade (the Rust kernel when enabled; the Python oracle otherwise) —
    the SAME per-element judgment a future in-sim arm would make, so the
    annotation IS the gate stream's dry run.
    """
    nan = float("nan")
    allowed_mask = 0
    for token in config.allowed_regimes:
        allowed_mask |= 1 << REGIME_CODES[token]
    masks = abstain_reason_masks(
        [REGIME_CODES[t.regime] for t in trades],
        allowed_mask,
        [t.vix_entry if t.vix_entry is not None else nan for t in trades],
        config.vix_lo if config.vix_lo is not None else nan,
        config.vix_hi if config.vix_hi is not None else nan,
        [1 if t.event_day else 0 for t in trades],
        config.block_event_days,
        [p if p is not None else nan for p in p_win],
        config.tau if config.tau is not None else nan,
    )
    per_trade: dict[str, list[str]] = {}
    counts = dict.fromkeys(ABSTAIN_REASON_ORDER, 0)
    would_abstain = 0
    for trade, mask in zip(trades, masks, strict=True):
        reasons = reasons_from_mask(mask)
        if reasons:
            would_abstain += 1
            per_trade[str(trade.trade_seq)] = list(reasons)
            for reason in reasons:
                counts[reason] += 1
    n = len(trades)
    kept = n - would_abstain
    return {
        "dec": _DEC_ABSTAIN,
        "config": {
            "allowed_regimes": sorted(config.allowed_regimes),
            "vix_lo": config.vix_lo,
            "vix_hi": config.vix_hi,
            "block_event_days": config.block_event_days,
            "tau": config.tau,
            "note": (
                "DEFAULT ILLUSTRATIVE config — gating on it requires "
                "re-validation; alternate configs are a re-run of the "
                "annotator"
            ),
        },
        # EMIT-ON-PRESENT: only trades with a non-empty reason tuple appear.
        "per_trade_reasons": per_trade,
        "would_abstain_count": would_abstain,
        "kept_trades": kept,
        "kept_pct": None if n == 0 else kept / n,
        "gate_reasons_counts": counts,
    }


def _combo_annotation(
    paths: SidecarPaths,
    run_id: str,
    lens: SessionLens,
    scorer: Scorer | None,
) -> tuple[dict[str, Any], int, int]:
    """Annotate ONE combo: returns ``(payload, n_trades, malformed_rows)``."""
    features: list[TradeFeatures] = []
    malformed = 0
    for row in iter_trades_for_export(paths, run_id):
        extracted = trade_features_from_wire(row, lens=lens)
        if extracted is None:
            malformed += 1
        else:
            features.append(extracted)
    oof = oof_p_win(features, scorer=scorer)
    wins = [t.label_win for t in features]
    curve = tau_curve(oof.p_win, wins)
    band_lo, band_hi = _vix_band(paths, run_id)
    config = default_abstain_config(band_lo, band_hi)
    payload: dict[str, Any] = {
        "n_trades": len(features),
        "n_malformed_rows": malformed,
        "feature_names": list(FEATURE_NAMES),
        "label_span": oof.label_span,
        "embargo": oof.embargo,
        "oof": {
            "n_splits_fit": oof.n_splits_fit,
            "n_splits_skipped": oof.n_splits_skipped,
            "degenerate_reason": oof.degenerate_reason,
            "n_groups": OOF_N_GROUPS,
            "n_test_groups": OOF_N_TEST_GROUPS,
            "ridge": DEFAULT_RIDGE,
            "max_iter": DEFAULT_MAX_ITER,
        },
        "p_win": {str(t.trade_seq): p for t, p in zip(features, oof.p_win, strict=True)},
        "tau_curve": [
            {
                "tau": point.tau,
                "n_kept": point.n_kept,
                "kept_pct": point.kept_pct,
                "precision": point.precision,
            }
            for point in curve
        ],
        "n_thresholds_swept": N_THRESHOLDS_SWEPT,
        "kept_pct_at": {
            f"{tau:.1f}": next(p.kept_pct for p in curve if p.tau == tau) for tau in _KEPT_PCT_TAUS
        },
        "abstain": _abstain_block(features, oof.p_win, config),
    }
    return payload, len(features), malformed


def _write_canonical(dest: Path, text: str) -> None:
    """Atomic canonical-text write (a def, not a lambda — no loop-var capture)."""

    def _write(tmp: Path) -> None:
        tmp.write_text(text, encoding="utf-8")

    atomic_write_simple(dest, _write)


def _canonical_json(payload: Mapping[str, Any]) -> str:
    """Canonical byte-stable JSON (the trade_store stamp discipline)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"


def _lens_for(
    instrument: str,
    cache: dict[str, SessionLens],
    daily_bars_for: Callable[[str], Sequence[CandleBar] | None] | None,
) -> SessionLens:
    """Build (once per instrument) the session lens — empty on any failure.

    An empty lens is FIRST-CLASS (every regime reads ``"unknown"``, no event
    flags) — the honest degrade when no daily series is available; counted
    via the ``meta.lens_empty`` log, never a crash.
    """
    cached = cache.get(instrument)
    if cached is not None:
        return cached
    bars: Sequence[CandleBar] | None = None
    if daily_bars_for is not None:
        bars = daily_bars_for(instrument)
    try:
        lens = build_session_lens(bars if bars else (), instrument=instrument)
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except SessionLensError as exc:
        _LOG.warning("meta.lens_failed", instrument=instrument, error=repr(str(exc)))
        lens = SessionLens(trend_by_date={}, event_flags_by_date={})
    if not lens.trend_by_date:
        _LOG.info("meta.lens_empty", instrument=instrument)
    cache[instrument] = lens
    return lens


def annotate_run(
    results_root: Path,
    run_id: str,
    *,
    daily_bars_for: Callable[[str], Sequence[CandleBar] | None] | None = None,
    scorer: Scorer | None = None,
    dry_run: bool = False,
) -> MetaAnnotationReport:
    """Annotate every finalist stem of one sweep run (the CLI's engine).

    Parameters
    ----------
    results_root
        The sweeps results tree (``<lake>/sweeps`` — the sweep's
        ``--results-root``); the run dir resolves through the ONE
        :func:`brutex.engine.trade_store.sweep_run_dir` seam.
    run_id
        The sweep run id (``grid_identity`` — the ``sweep_runs/<run_id>``
        dir name).
    daily_bars_for
        ``instrument → derived-1day bars`` for the trend/event lens (the
        results-depth pass's exact seam shape), or ``None`` — every lens is
        then empty (regimes ``"unknown"``, no event flags; honest + counted).
    scorer
        Inference seam override (tests); ``None`` binds the production
        :func:`brutex.meta.meta_rust.score_probabilities` facade.
    dry_run
        Enumerate + validate stamps ONLY: zero writes, zero fits (the
        integration suite pins the tree byte-untouched).

    Returns
    -------
    MetaAnnotationReport
        Per-stem outcomes + totals; ``ok`` ⇔ no stem skipped (or dry-run).
    """
    run_dir = sweep_run_dir(results_root, run_id)
    depth_root = run_dir / RESULTS_DEPTH_DIRNAME
    trades_files = sorted(depth_root.glob("*/*/*.trades.parquet"))
    outcomes: list[StemOutcome] = []
    lens_cache: dict[str, SessionLens] = {}
    combos_total = 0
    trades_total = 0
    for trades_path in trades_files:
        paths = _sidecar_paths(trades_path)
        rel = str(trades_path.relative_to(depth_root))
        stem_rel = rel[: -len(".trades.parquet")]
        stamp = load_stamp(paths)
        if stamp is None:
            outcomes.append(
                StemOutcome(
                    stem_rel=stem_rel,
                    annotated=False,
                    reason="stamp_invalid",
                    combos=0,
                    trades=0,
                    malformed_rows=0,
                )
            )
            continue
        combo_ids = sorted(stamp.trade_spans)
        if dry_run:
            outcomes.append(
                StemOutcome(
                    stem_rel=stem_rel,
                    annotated=False,
                    reason="dry_run",
                    combos=len(combo_ids),
                    trades=stamp.trade_rows,
                    malformed_rows=0,
                )
            )
            combos_total += len(combo_ids)
            continue
        instrument = trades_path.parent.parent.name
        lens = _lens_for(instrument, lens_cache, daily_bars_for)
        combos: dict[str, Any] = {}
        stem_trades = 0
        stem_malformed = 0
        try:
            for combo_id in combo_ids:
                payload, n_trades, malformed = _combo_annotation(paths, combo_id, lens, scorer)
                combos[combo_id] = payload
                stem_trades += n_trades
                stem_malformed += malformed
            sidecar = {
                "schema_version": META_SCHEMA_VERSION,
                "dec": _DEC_META,
                "sweep_run_id": run_id,
                "instrument": instrument,
                "tf": trades_path.parent.name,
                "stem": trades_path.name[: -len(".trades.parquet")],
                "combos": combos,
                "revalidation_required": True,
                "notes": list(_HONEST_NOTES),
            }
            meta_path = paths.trades.parent / (
                paths.trades.name[: -len(".trades.parquet")] + META_SIDECAR_SUFFIX
            )
            _write_canonical(meta_path, _canonical_json(sidecar))
        except (MemoryError, KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            _LOG.warning(
                "meta.stem_failed", stem=stem_rel, error=repr(f"{type(exc).__name__}: {exc}")
            )
            outcomes.append(
                StemOutcome(
                    stem_rel=stem_rel,
                    annotated=False,
                    reason=f"annotate_failed:{type(exc).__name__}",
                    combos=0,
                    trades=0,
                    malformed_rows=0,
                )
            )
            continue
        combos_total += len(combo_ids)
        trades_total += stem_trades
        outcomes.append(
            StemOutcome(
                stem_rel=stem_rel,
                annotated=True,
                reason=None,
                combos=len(combo_ids),
                trades=stem_trades,
                malformed_rows=stem_malformed,
            )
        )
        _LOG.info(
            "meta.stem_annotated",
            stem=stem_rel,
            combos=len(combo_ids),
            trades=stem_trades,
            malformed=stem_malformed,
        )

    annotated = sum(1 for o in outcomes if o.annotated)
    skipped = sum(1 for o in outcomes if not o.annotated and o.reason != "dry_run")
    summary_path: str | None = None
    if not dry_run:
        summary = {
            "schema_version": META_SCHEMA_VERSION,
            "dec": _DEC_META,
            "sweep_run_id": run_id,
            "stems_total": len(outcomes),
            "stems_annotated": annotated,
            "stems_skipped": skipped,
            "combos_annotated": combos_total,
            "trades_annotated": trades_total,
            "outcomes": [
                {
                    "stem": o.stem_rel,
                    "annotated": o.annotated,
                    "reason": o.reason,
                    "combos": o.combos,
                    "trades": o.trades,
                    "malformed_rows": o.malformed_rows,
                }
                for o in outcomes
            ],
            "revalidation_required": True,
            "notes": list(_HONEST_NOTES),
        }
        dest = run_dir / META_SUMMARY_FILENAME
        run_dir.mkdir(parents=True, exist_ok=True)
        _write_canonical(dest, _canonical_json(summary))
        summary_path = str(dest)
    return MetaAnnotationReport(
        run_id=run_id,
        dry_run=dry_run,
        stems_total=len(outcomes),
        stems_annotated=annotated,
        stems_skipped=skipped,
        combos_annotated=combos_total,
        trades_annotated=trades_total,
        outcomes=tuple(outcomes),
        summary_path=summary_path,
    )
