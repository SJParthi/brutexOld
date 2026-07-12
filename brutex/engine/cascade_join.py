"""ONE pure cascade-report → display-join law (DEC-RESULTS-CASCADE-VERDICT-JOIN-001).

The Stage-4 cascade writes its overfitting verdicts to
``<results_root>/cascade/cascade-report.json`` (one report per results root,
overwritten per sweep — :mod:`brutex.engine.cascade_wire`), while the display
surfaces (the top-100 artifact writer, ``/results``, ``/screener``,
``/strategy``) key rows by the 64-hex BLAKE3 ``run_id`` (CLAUDE.md §2 Rule 4).
This module is the ONE flattener/validator both consumers share (§11 COMMON):

* the top-100 WRITER feeds the in-memory
  ``cascade_wire.wire_report_to_dict(report)`` (no disk read — the report
  object is threaded from ``cmd_sweep_all``);
* the ``/results`` read-time loader feeds ``json.loads(report bytes)``.

One code path, one law — the join key is a DIRECT equality
``verdict.candidate_id == top100_entry["run_id"] == ResultRow.run_id`` (the
run identity already folds mask‖direction‖instrument‖tf‖mode‖params‖data‖
indicator‖code, so run_ids are globally unique across the report's
``(instrument, tf)`` groups and ONE flat index suffices).

Validation law (the tolerance ruling — structural fail-closed, per-item
tolerant; the #1631 named-partial-degrade house law):

* STRUCTURAL (:class:`CascadeJoinError` — callers degrade to not-validated):
  a non-mapping root; a ``version`` that is not a GENUINE int (bool pun
  rejected) or is NEWER than the writer's ``REPORT_VERSION`` (unknown FUTURE
  semantics are never guessed); a missing/non-str/empty ``sweep_id``;
  ``groups`` not a list.
* PER-ITEM (skip + count + ONE capped WARN per build): a malformed group
  (non-dict entry / non-str-or-empty ``instrument``/``tf`` / non-dict
  ``report`` / non-list ``verdicts`` / junk ``n_input``/``n_survived``
  headline counts) increments ``skipped_groups``; a malformed verdict
  (non-dict / bad ``candidate_id`` / non-bool ``passed`` / a ``passed``
  verdict carrying a non-null ``rejected_at``) increments
  ``skipped_verdicts``. Optional verdict keys are presence-tolerant
  (``rejected_at``→None · ``detail``→"" · ``composite_score``→None ·
  ``provisional``→False · ``cpcv_pass_rate``→None); a non-finite /
  out-of-``[0, 1]`` rate or a non-finite composite is NULLED + counted
  (``nulled_values``) with the verdict KEPT — never clamped into a fake
  number (Golden Rule #1).
* A DUPLICATE ``candidate_id`` (within a group or across the flattened
  groups) excludes EVERY copy from ``by_candidate`` and records the id in
  ``ambiguous_ids`` — ambiguous evidence is never displayed (consumers
  render "not validated · duplicate verdict").
* ``survivor_rank`` is the 1-based position in the OWNING group's
  ``survivor_ids``; a survivor id with no surviving PASSED verdict, or a
  passed verdict absent from ``survivor_ids``, is a COUNTED inconsistency
  (``rank_inconsistencies``) with the rank left unassigned — tolerant +
  counted, never a crash.

Honest envelope (Golden Rule #10): :func:`build_cascade_join` is one
O(report) pass per invocation; every ``by_candidate`` lookup afterwards is
O(1). NOT an O(1)-total claim — the report parse itself is the caller's cost
(memoised per stat token on the ``/results`` side).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Final

import structlog

from brutex.models import BruteXError

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from pathlib import Path

_LOG = structlog.get_logger("brutex.engine.cascade_join")

__all__ = [
    "CascadeGroupStats",
    "CascadeJoin",
    "CascadeJoinError",
    "CascadeVerdictView",
    "build_cascade_join",
    "cascade_report_path",
]

#: Cap on per-item defect notes carried on the ONE build WARN — a hostile
#: report can hold thousands of junk items; the COUNTS stay exact regardless.
_WARN_DETAIL_CAP: Final[int] = 5


class CascadeJoinError(BruteXError):
    """Raised on a STRUCTURALLY unusable report payload (callers degrade)."""


def cascade_report_path(results_root: Path) -> Path:
    """The report path under *results_root* — delegated to the ONE path law.

    Function-local constant import (the ``results_report.render_cascade_sidecar``
    "cli pattern"): :mod:`brutex.engine.cascade_wire` owns
    ``CASCADE_DIRNAME``/``REPORT_FILENAME`` — no mirror constants here, so the
    two homes can never drift and importing this module stays lean until a
    path is actually built.
    """
    from brutex.engine.cascade_wire import report_path

    return report_path(results_root)


@dataclass(frozen=True, slots=True)
class CascadeGroupStats:
    """One ``(instrument, tf)`` pool's headline stats, as recorded on the report.

    ``pool_pbo`` / ``dsr_n_trials`` / ``dsr_trial_variance`` are the report's
    group-level honesty numerics (``None`` = not recorded / filter skipped);
    ``dsr_n_source`` is ``""`` when the report carried a non-string value
    (honest unknown, never a guessed ``"pool"``).
    """

    instrument: str
    tf: str
    n_input: int
    n_survived: int
    pool_pbo: float | None
    dsr_n_source: str
    dsr_n_trials: int | None
    dsr_trial_variance: float | None


@dataclass(frozen=True, slots=True)
class CascadeVerdictView:
    """One candidate's flattened cascade verdict (display projection).

    ``rejected_at`` is the VERBATIM stage token (an unknown FUTURE token is
    kept — displays render ``FAIL @ <token>``, never a guess);
    ``survivor_rank`` is the 1-based position in the owning group's
    ``survivor_ids`` (``None`` for fails and for counted inconsistencies).
    """

    candidate_id: str
    instrument: str
    tf: str
    passed: bool
    rejected_at: str | None
    detail: str
    cascade_composite: float | None
    provisional: bool
    cpcv_pass_rate: float | None
    survivor_rank: int | None


@dataclass(frozen=True, slots=True)
class CascadeJoin:
    """The flattened, validated cascade report — ONE O(1) lookup per run_id.

    ``by_candidate`` EXCLUDES duplicate candidate_ids (every copy —
    ``ambiguous_ids`` names them); ``skipped_groups`` / ``skipped_verdicts`` /
    ``nulled_values`` / ``rank_inconsistencies`` are the per-item tolerance
    counters (counted + logged once per build, never a crash).
    """

    sweep_id: str
    version: int
    n_input: int
    n_survived: int
    by_candidate: Mapping[str, CascadeVerdictView]
    ambiguous_ids: frozenset[str]
    groups: tuple[CascadeGroupStats, ...]
    skipped_groups: int
    skipped_verdicts: int
    nulled_values: int
    rank_inconsistencies: int


def _count_or_none(value: object) -> int | None:
    """*value* as a genuine non-negative ``int`` (JSON bool pun rejected), else ``None``."""
    if type(value) is not int:
        return None
    return value if value >= 0 else None


def _finite_float(value: object) -> float | None:
    """*value* as a finite ``float`` (int accepted, bool rejected), else ``None``."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


@dataclass(slots=True)
class _Tally:
    """Mutable per-build tolerance counters (kept off the frozen result)."""

    skipped_groups: int = 0
    skipped_verdicts: int = 0
    nulled_values: int = 0
    rank_inconsistencies: int = 0
    details: list[str] = field(default_factory=list)

    def note(self, detail: str) -> None:
        """Record one capped human-readable defect note for the build WARN."""
        if len(self.details) < _WARN_DETAIL_CAP:
            self.details.append(detail)


def _verdict_view(
    entry: object, *, instrument: str, tf: str, tally: _Tally
) -> CascadeVerdictView | None:
    """Validate + project one raw verdict entry; ``None`` = skipped + counted."""
    if not isinstance(entry, dict):
        tally.skipped_verdicts += 1
        tally.note(f"{instrument}/{tf}: verdict entry is not an object")
        return None
    candidate_id = entry.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id:
        tally.skipped_verdicts += 1
        tally.note(f"{instrument}/{tf}: verdict lacks a non-empty candidate_id")
        return None
    passed = entry.get("passed")
    if not isinstance(passed, bool):
        tally.skipped_verdicts += 1
        tally.note(f"{instrument}/{tf}: verdict {candidate_id!r} passed is not a bool")
        return None
    rejected_at_raw = entry.get("rejected_at")
    if passed and rejected_at_raw is not None:
        # The passed ⇒ rejected_at-None law: a "pass" carrying a rejection
        # stage is self-contradictory evidence — skipped, never guessed.
        tally.skipped_verdicts += 1
        tally.note(f"{instrument}/{tf}: passed verdict {candidate_id!r} carries rejected_at")
        return None
    rejected_at = rejected_at_raw if isinstance(rejected_at_raw, str) else None
    if rejected_at_raw is not None and rejected_at is None:
        # A non-string stage token on a FAIL is nulled (displays read
        # "FAIL @ ?") + counted — the verdict itself is kept.
        tally.nulled_values += 1
    detail_raw = entry.get("detail")
    composite_raw = entry.get("composite_score")
    composite = _finite_float(composite_raw)
    if composite_raw is not None and composite is None:
        tally.nulled_values += 1
    rate_raw = entry.get("cpcv_pass_rate")
    rate = _finite_float(rate_raw)
    if rate is not None and not 0.0 <= rate <= 1.0:
        # An out-of-domain rate is junk evidence — nulled, never clamped
        # into a fake percentage (Golden Rule #1).
        rate = None
        tally.nulled_values += 1
    elif rate_raw is not None and rate is None:
        tally.nulled_values += 1
    return CascadeVerdictView(
        candidate_id=candidate_id,
        instrument=instrument,
        tf=tf,
        passed=passed,
        rejected_at=rejected_at,
        detail=detail_raw if isinstance(detail_raw, str) else "",
        cascade_composite=composite,
        provisional=entry.get("provisional") is True,
        cpcv_pass_rate=rate,
        survivor_rank=None,
    )


def _group_views(
    verdicts_raw: list[object],
    report: dict[str, object],
    *,
    instrument: str,
    tf: str,
    tally: _Tally,
    ambiguous: set[str],
) -> dict[str, CascadeVerdictView]:
    """One group's validated verdict views with survivor ranks stamped."""
    views: dict[str, CascadeVerdictView] = {}
    for entry in verdicts_raw:
        view = _verdict_view(entry, instrument=instrument, tf=tf, tally=tally)
        if view is None:
            continue
        if view.candidate_id in views:
            # An INTRA-group duplicate is ambiguous evidence too — every
            # copy is excluded at the flatten step below.
            ambiguous.add(view.candidate_id)
            tally.note(f"{instrument}/{tf}: duplicate candidate_id {view.candidate_id!r}")
            continue
        views[view.candidate_id] = view

    survivor_ids_raw = report.get("survivor_ids")
    survivor_ids = survivor_ids_raw if isinstance(survivor_ids_raw, list) else []
    ranked: set[str] = set()
    for position, sid in enumerate(survivor_ids, start=1):
        view = views.get(sid) if isinstance(sid, str) else None
        if view is None or not view.passed:
            # A survivor id with no surviving PASSED verdict — counted
            # inconsistency, rank unassigned (tolerant, never a crash).
            tally.rank_inconsistencies += 1
            tally.note(f"{instrument}/{tf}: survivor_id {sid!r} has no passed verdict")
            continue
        views[view.candidate_id] = replace(view, survivor_rank=position)
        ranked.add(view.candidate_id)
    for cid, view in views.items():
        if view.passed and cid not in ranked:
            # A passed verdict absent from survivor_ids — counted, rank None.
            tally.rank_inconsistencies += 1
            tally.note(f"{instrument}/{tf}: passed verdict {cid!r} absent from survivor_ids")
    return views


def build_cascade_join(payload: object) -> CascadeJoin:
    """Flatten + validate one serialised cascade-report payload into a join.

    *payload* is the parsed JSON object — either
    ``cascade_wire.wire_report_to_dict(report)`` (the in-memory writer path)
    or ``json.loads(report_bytes)`` (the ``/results`` read path). See the
    module docstring for the full validation law.

    Raises
    ------
    CascadeJoinError
        On a STRUCTURALLY unusable payload (non-mapping root / version pun or
        unknown FUTURE version / missing sweep_id / non-list groups).
    """
    # Function-local version-constant import — the ONE writer home
    # (cascade_wire) owns REPORT_VERSION; no mirror constant here.
    from brutex.engine.cascade_wire import REPORT_VERSION

    if not isinstance(payload, Mapping):
        raise CascadeJoinError("cascade report root is not a JSON object")
    version = payload.get("version")
    if type(version) is not int:
        raise CascadeJoinError(f"cascade report version {version!r} is not a genuine integer")
    if version > REPORT_VERSION:
        raise CascadeJoinError(
            f"cascade report version {version} is NEWER than the supported "
            f"{REPORT_VERSION} — unknown future semantics are never guessed"
        )
    sweep_id = payload.get("sweep_id")
    if not isinstance(sweep_id, str) or not sweep_id:
        raise CascadeJoinError("cascade report sweep_id is missing or not a non-empty string")
    groups_raw = payload.get("groups")
    if not isinstance(groups_raw, list):
        raise CascadeJoinError("cascade report groups is not a list")

    tally = _Tally()
    group_stats: list[CascadeGroupStats] = []
    views: dict[str, CascadeVerdictView] = {}
    ambiguous: set[str] = set()

    for group in groups_raw:
        if not isinstance(group, dict):
            tally.skipped_groups += 1
            tally.note("group entry is not an object")
            continue
        instrument = group.get("instrument")
        tf = group.get("tf")
        report = group.get("report")
        if (
            not isinstance(instrument, str)
            or not instrument
            or not isinstance(tf, str)
            or not tf
            or not isinstance(report, dict)
        ):
            tally.skipped_groups += 1
            tally.note("group lacks str instrument/tf or an object report")
            continue
        verdicts_raw = report.get("verdicts")
        n_input = _count_or_none(report.get("n_input"))
        n_survived = _count_or_none(report.get("n_survived"))
        if not isinstance(verdicts_raw, list) or n_input is None or n_survived is None:
            tally.skipped_groups += 1
            tally.note(f"{instrument}/{tf}: report lacks list verdicts / int headline counts")
            continue
        dsr_n_source_raw = report.get("dsr_n_source")
        group_stats.append(
            CascadeGroupStats(
                instrument=instrument,
                tf=tf,
                n_input=n_input,
                n_survived=n_survived,
                pool_pbo=_finite_float(report.get("pool_pbo")),
                dsr_n_source=dsr_n_source_raw if isinstance(dsr_n_source_raw, str) else "",
                dsr_n_trials=_count_or_none(report.get("dsr_n_trials")),
                dsr_trial_variance=_finite_float(report.get("dsr_trial_variance")),
            )
        )
        group_views = _group_views(
            verdicts_raw, report, instrument=instrument, tf=tf, tally=tally, ambiguous=ambiguous
        )
        # Cross-group flatten: a duplicate excludes EVERY copy.
        for cid, view in group_views.items():
            if cid in ambiguous:
                continue
            if cid in views:
                ambiguous.add(cid)
                del views[cid]
                tally.note(f"duplicate candidate_id {cid!r} across groups")
                continue
            views[cid] = view
    # A candidate flagged ambiguous by a LATER group's intra-group duplicate
    # may already sit in the flat index from an earlier group — sweep it out.
    for cid in ambiguous:
        views.pop(cid, None)

    # Root headline counts: the writer's own genuine ints when present, else
    # the honest sum over the kept groups (never a fabricated constant).
    n_input_total = _count_or_none(payload.get("n_input"))
    if n_input_total is None:
        n_input_total = sum(g.n_input for g in group_stats)
    n_survived_total = _count_or_none(payload.get("n_survived"))
    if n_survived_total is None:
        n_survived_total = sum(g.n_survived for g in group_stats)

    if (
        tally.skipped_groups
        or tally.skipped_verdicts
        or tally.nulled_values
        or tally.rank_inconsistencies
        or ambiguous
    ):
        # ONE capped WARN per build (the #1631 named-partial-degrade law) —
        # counts exact, detail samples capped at _WARN_DETAIL_CAP.
        _LOG.warning(
            "cascade_join.partial",
            sweep_id=sweep_id,
            skipped_groups=tally.skipped_groups,
            skipped_verdicts=tally.skipped_verdicts,
            nulled_values=tally.nulled_values,
            rank_inconsistencies=tally.rank_inconsistencies,
            duplicate_ids=len(ambiguous),
            details=tally.details,
        )

    return CascadeJoin(
        sweep_id=sweep_id,
        version=version,
        n_input=n_input_total,
        n_survived=n_survived_total,
        by_candidate=views,
        ambiguous_ids=frozenset(ambiguous),
        groups=tuple(group_stats),
        skipped_groups=tally.skipped_groups,
        skipped_verdicts=tally.skipped_verdicts,
        nulled_values=tally.nulled_values,
        rank_inconsistencies=tally.rank_inconsistencies,
    )
