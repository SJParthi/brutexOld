"""Per-mask stage-3 / cascade verdict artifact (G8, DEC-SWEEP-UNKILLABLE-CORE-001).

The ``/results`` leaderboard today co-ranks rows with very different evidential
standing: gated Stage-2 grid winners, UNGATED Stage-3 refined-eval rows (refined
trees hold ungated rows — see :func:`brutex.cli.cmd_results_show`'s note), and
rows the Stage-4 cascade REJECTED. This module folds the run's two existing
verdict sources — ``cascade/manifest.json`` (which spec/mask universe ran, and
which specs are Stage-3 refined cells) and ``cascade/cascade-report.json`` (the
nine-filter pass/reject verdicts) — into ONE small stamped artifact
``runs/<run_id>/verdicts.json`` so the P3 join can MARK such rows instead of
co-ranking them (design §5 G8; the 1-call join in ``sweep_results_view`` is the
flagged P3 adapter, landed at merge-integration).

Verdict vocabulary (design §2, frozen)
--------------------------------------
Per ``mask_hex`` (lowercase hex, no ``0x`` prefix, no padding):

* ``stage3``:
  - ``"refined"`` — the mask appears among a Stage-3 refined cell's winners AND
    among a gated Stage-2 spec's winners (a gated survivor stage-3 refined —
    refined evidence exists on top of gated evidence).
  - ``"ungated"`` — the mask appears ONLY among Stage-3 refined-cell winners:
    its rows exist solely as ungated refined-eval rows — the exact G8 co-rank
    hazard the join must mark.
  - ``"not_run"`` — stage-3 disabled / absent, or the mask untouched by it.
* ``cascade``:
  - ``"passed"`` — at least one of the mask's cascade candidates passed
    (any-pass fold: a mask that survived the cascade anywhere is a survivor).
  - ``"rejected"`` — the mask has >= 1 cascade verdict and none passed.
  - ``"not_run"`` — no cascade verdict exists for any of the mask's candidates
    (report missing/unreadable, or its group produced no verdicts).

Honesty law (Golden Rule #1): a MISSING or unreadable input is folded to the
honest ``"not_run"`` / an empty mask universe — never a fabricated verdict.
Stage-3 spec classification matches refined-cell coordinates
``(instrument, tf, direction, sl, tp, proximity, fib_anchor)``; a Stage-2 grid
point that coincides exactly with a refined cell's quantized coordinates is
classified refined (documented envelope — at identical coordinates the mask
genuinely carries refined evidence).

Cost (Golden Rule #10): :func:`build_run_verdicts` is ONE ``O(winners +
verdicts)`` pass over the two JSON documents — honestly O(reports), never
claimed O(1). :func:`load_run_verdicts` is one bounded file read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import structlog

from brutex.data.atomic_io import atomic_write_simple
from brutex.data.guarded_read import read_regular_text
from brutex.engine.cascade_wire import manifest_path, report_path
from brutex.engine.run_layout import run_dir
from brutex.observability.stage_stamp import write_stage_stamp

_LOG = structlog.get_logger(__name__)

#: The artifact filename under ``runs/<run_id>/``.
VERDICTS_FILENAME: Final[str] = "verdicts.json"

#: The artifact schema version (design §3).
VERDICTS_SCHEMA_VERSION: Final[int] = 1

#: The stage name stamped onto the artifact's ``.stamp.json`` sidecar (R42).
VERDICTS_STAGE: Final[str] = "verdicts"

#: stage3 verdict values (design §2 — frozen vocabulary).
STAGE3_REFINED: Final[str] = "refined"
STAGE3_UNGATED: Final[str] = "ungated"

#: cascade verdict values (design §2 — frozen vocabulary).
CASCADE_PASSED: Final[str] = "passed"
CASCADE_REJECTED: Final[str] = "rejected"

#: The shared honest "no verdict was produced" value for BOTH keys.
NOT_RUN: Final[str] = "not_run"

#: The manifest's default Fib anchor when the additive key is absent
#: (pre-DEC-NIFTY-FIB-AXIS-001 manifests — mirrors ``manifest_from_json``).
_DEFAULT_FIB_ANCHOR: Final[str] = "prev_day"


def verdicts_path(results_root: Path, run_id: str) -> Path:
    """Return the verdicts artifact path — ``<results_root>/runs/<run_id>/verdicts.json``."""
    return run_dir(results_root, run_id) / VERDICTS_FILENAME


def _read_json_tolerant(path: Path, *, what: str) -> object | None:
    """Read *path* as JSON; ``None`` (+ one WARN for a corrupt file) on any defect.

    A MISSING file returns ``None`` silently (a run without a cascade is
    normal); an unreadable/corrupt file returns ``None`` with ONE
    ``run_verdicts.input_unreadable`` WARN — the honest degrade, never a
    fabricated document (Golden Rule #1).
    """
    if not path.is_file():
        return None
    try:
        doc: object = json.loads(read_regular_text(path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _LOG.warning("run_verdicts.input_unreadable", what=what, path=str(path), error=str(exc))
        return None
    return doc


def _cell_key(record: dict[str, Any]) -> tuple[object, ...]:
    """Normalise one spec/cell dict into the stage-3 coordinate-match key.

    ``(instrument, tf_label, direction_label, sl_paisa, tp_paisa,
    proximity_bps, fib_anchor)`` — the fields a :class:`CascadeTupleSpec` and a
    ``Stage3CellRecord`` share (the spec carries no ``k``, so ``k`` is excluded).
    ``fib_anchor`` defaults to ``"prev_day"`` when absent (the manifest's own
    additive-parse default).
    """
    proximity = record.get("proximity_bps")
    return (
        str(record["instrument"]),
        str(record["tf_label"]),
        str(record["direction_label"]),
        None if record["sl_paisa"] is None else int(record["sl_paisa"]),
        None if record["tp_paisa"] is None else int(record["tp_paisa"]),
        None if proximity is None else tuple(int(v) for v in proximity),
        str(record.get("fib_anchor") or _DEFAULT_FIB_ANCHOR),
    )


def _manifest_facts(
    doc: object,
) -> tuple[dict[str, int], set[int], set[int], bool] | None:
    """Extract the mask universe from a parsed cascade manifest document.

    Returns ``(run_id -> mask, stage2_masks, stage3_masks, stage3_enabled)``,
    or ``None`` (+ one WARN) when the document is structurally malformed —
    the caller then reports an honest EMPTY mask universe, never a guess.
    """
    try:
        raw = doc if isinstance(doc, dict) else None
        if raw is None:
            raise TypeError(f"manifest document must be a JSON object, got {type(doc).__name__}")
        stage3_raw = raw.get("stage3")
        stage3_enabled = False
        refined_keys: set[tuple[object, ...]] = set()
        if isinstance(stage3_raw, dict):
            stage3_enabled = bool(stage3_raw["enabled"])
            if stage3_enabled:
                refined_keys = {_cell_key(cell) for cell in stage3_raw["refined_cells"]}
        run_to_mask: dict[str, int] = {}
        stage2_masks: set[int] = set()
        stage3_masks: set[int] = set()
        for spec in raw["specs"]:
            is_refined_spec = _cell_key(spec) in refined_keys
            for run_id, mask in spec["winners"]:
                mask_int = int(mask)
                run_to_mask[str(run_id)] = mask_int
                (stage3_masks if is_refined_spec else stage2_masks).add(mask_int)
    except (KeyError, TypeError, ValueError) as exc:
        _LOG.warning("run_verdicts.manifest_malformed", error=str(exc))
        return None
    return run_to_mask, stage2_masks, stage3_masks, stage3_enabled


def _cascade_passed_by_candidate(doc: object) -> dict[str, bool] | None:
    """Extract ``candidate_id -> passed`` from a parsed cascade-report document.

    Returns ``None`` (+ one WARN) on a structurally malformed report — the
    caller then folds EVERY mask's cascade verdict to the honest ``"not_run"``.
    """
    try:
        raw = doc if isinstance(doc, dict) else None
        if raw is None:
            raise TypeError(f"report document must be a JSON object, got {type(doc).__name__}")
        passed_by_id: dict[str, bool] = {}
        for group in raw["groups"]:
            for verdict in group["report"]["verdicts"]:
                candidate_id = str(verdict["candidate_id"])
                # Any-pass fold at the candidate level too: the same id
                # appearing in two groups keeps its best (passed) outcome.
                passed_by_id[candidate_id] = passed_by_id.get(candidate_id, False) or bool(
                    verdict["passed"]
                )
    except (KeyError, TypeError, ValueError) as exc:
        _LOG.warning("run_verdicts.report_malformed", error=str(exc))
        return None
    return passed_by_id


def build_run_verdicts(results_root: Path, *, run_id: str) -> Path:
    """Build + stamp ``runs/<run_id>/verdicts.json`` from the run's cascade artifacts.

    ONE ``O(winners + verdicts)`` pass over ``cascade/manifest.json`` +
    ``cascade/cascade-report.json`` under *results_root* (design §2 — G8's P2
    side). Missing/unreadable inputs degrade honestly: no manifest ⇒ an EMPTY
    ``by_mask`` (the mask universe is unknowable — never invented); no report ⇒
    every mask's ``cascade`` verdict is ``"not_run"``. The artifact is written
    atomically (artifact FIRST) then stamped (I-5, via
    :func:`~brutex.observability.stage_stamp.write_stage_stamp`,
    ``stage="verdicts"``).

    Parameters
    ----------
    results_root
        The sweep results root the run persisted under.
    run_id
        The run identity (D-1 — the grid identity digest).

    Returns
    -------
    Path
        The written ``verdicts.json`` path.

    Raises
    ------
    OSError
        On a write failure (the run-core caller's exit path is fail-soft).
    brutex.observability.stage_stamp.StageStampError
        On a stamp write failure AFTER the artifact landed — the safe
        direction: an unstamped artifact is treated as absent and rebuilt.
    """
    facts = _manifest_facts(_read_json_tolerant(manifest_path(results_root), what="manifest"))
    passed_by_id = _cascade_passed_by_candidate(
        _read_json_tolerant(report_path(results_root), what="report")
    )

    by_mask: dict[str, dict[str, str]] = {}
    unmapped = 0
    if facts is not None:
        run_to_mask, stage2_masks, stage3_masks, stage3_enabled = facts
        candidates_by_mask: dict[int, list[str]] = {}
        for candidate_id, mask in run_to_mask.items():
            candidates_by_mask.setdefault(mask, []).append(candidate_id)
        if passed_by_id is not None:
            unmapped = sum(1 for candidate_id in passed_by_id if candidate_id not in run_to_mask)
        for mask in sorted(stage2_masks | stage3_masks):
            if not stage3_enabled or mask not in stage3_masks:
                stage3_verdict = NOT_RUN
            elif mask in stage2_masks:
                stage3_verdict = STAGE3_REFINED
            else:
                stage3_verdict = STAGE3_UNGATED
            if passed_by_id is None:
                cascade_verdict = NOT_RUN
            else:
                outcomes = [
                    passed_by_id[candidate_id]
                    for candidate_id in candidates_by_mask[mask]
                    if candidate_id in passed_by_id
                ]
                if not outcomes:
                    cascade_verdict = NOT_RUN
                elif any(outcomes):
                    cascade_verdict = CASCADE_PASSED
                else:
                    cascade_verdict = CASCADE_REJECTED
            by_mask[format(mask, "x")] = {"stage3": stage3_verdict, "cascade": cascade_verdict}
    if unmapped:
        # A report candidate the manifest does not name (a foreign/stale report
        # beside this manifest) is COUNTED loudly, never guessed into a mask.
        _LOG.warning("run_verdicts.unmapped_candidates", count=unmapped)

    doc = {
        "run_id": run_id,
        "by_mask": by_mask,
        "schema_version": VERDICTS_SCHEMA_VERSION,
    }
    rendered = json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    dest = verdicts_path(results_root, run_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_simple(dest, lambda tmp: tmp.write_text(rendered, encoding="utf-8"))
    write_stage_stamp(dest, stage=VERDICTS_STAGE, run_id=run_id)
    _LOG.info(
        "run_verdicts.written",
        run_id=run_id,
        path=str(dest),
        masks=len(by_mask),
    )
    return dest


def load_run_verdicts(results_root: Path, run_id: str) -> dict[str, dict[str, str]]:
    """Load the run's per-mask verdict map; ``{}`` (honest, never a 500) on any defect.

    The G8 consumer read: a MISSING artifact returns ``{}`` silently (the run
    predates the artifact or the cascade never ran); a corrupt / malformed /
    FOREIGN (``run_id`` mismatch) artifact returns ``{}`` with ONE
    ``run_verdicts.unreadable`` WARN — the failure-matrix A16 "parse fail ⇒
    honest ``unreadable`` verdict" law. Never raises.

    Returns
    -------
    dict[str, dict[str, str]]
        ``mask_hex -> {"stage3": ..., "cascade": ...}`` (possibly empty).
    """
    path = verdicts_path(results_root, run_id)
    doc = _read_json_tolerant(path, what="verdicts")
    if doc is None:
        return {}
    try:
        raw = doc if isinstance(doc, dict) else None
        if raw is None:
            raise TypeError(f"verdicts document must be a JSON object, got {type(doc).__name__}")
        if str(raw["run_id"]) != run_id:
            raise ValueError(f"verdicts run_id {raw['run_id']!r} does not match {run_id!r}")
        by_mask: dict[str, dict[str, str]] = {}
        for mask_hex, verdicts in raw["by_mask"].items():
            by_mask[str(mask_hex)] = {
                "stage3": str(verdicts["stage3"]),
                "cascade": str(verdicts["cascade"]),
            }
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        _LOG.warning("run_verdicts.unreadable", path=str(path), error=str(exc))
        return {}
    return by_mask


__all__ = [
    "CASCADE_PASSED",
    "CASCADE_REJECTED",
    "NOT_RUN",
    "STAGE3_REFINED",
    "STAGE3_UNGATED",
    "VERDICTS_FILENAME",
    "VERDICTS_SCHEMA_VERSION",
    "VERDICTS_STAGE",
    "build_run_verdicts",
    "load_run_verdicts",
    "verdicts_path",
]
