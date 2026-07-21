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
``verdict.candidate_id == top100_entry["run_id"] == ResultRow.run_id``.

**RUST-HOSTED LAW (DEC-RUST-CASCADE-RESULTS-001):** the whole
validation/flatten law — structural fail-closed refusals, per-item
skip+count tolerance, duplicate-candidate every-copy exclusion,
survivor-rank stamping and the capped defect-note law — lives in
``rust/brutex-cascade/src/cascade_join.rs``, verified against the FROZEN
pre-conversion parity corpus
(``tests/golden/fixtures/cascade_rust_parity/cascade_join.json``). This
module is the type face: the frozen result dataclasses, the
:class:`CascadeJoinError` domain error, the ONE ``cascade_join.partial``
WARN (structlog stays Python-side — counters + capped notes come from the
law) and the path helper. The full validation-law prose lives in the Rust
module docs; the tolerance vocabulary is unchanged:

* STRUCTURAL (:class:`CascadeJoinError` — callers degrade to not-validated):
  non-mapping root; a ``version`` that is not a GENUINE int (bool pun
  rejected) or is NEWER than the writer's ``REPORT_VERSION``; a
  missing/non-str/empty ``sweep_id``; ``groups`` not a list.
* PER-ITEM (skip + count + ONE capped WARN per build): malformed groups /
  verdicts skipped; junk numerics NULLED + counted, never clamped.
* A DUPLICATE ``candidate_id`` excludes EVERY copy (``ambiguous_ids``).
* ``survivor_rank`` inconsistencies are COUNTED, rank left unassigned.

Honest envelope (Golden Rule #10): :func:`build_cascade_join` is one
O(report) traversal per invocation (the PyO3 projection pass + the Rust
law); every ``by_candidate`` lookup afterwards is O(1). NOT an O(1)-total
claim — the report parse itself is the caller's cost (memoised per stat
token on the ``/results`` side).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import structlog

from brutex.engine.cascade_rust import require_cascade_rust
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
#: (The load-bearing constant lives in the Rust law — ``WARN_DETAIL_CAP`` in
#: ``cascade_join.rs``; this mirror is pinned by the note-cap parity golden.)
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
    #: ADDITIVE regime-evidence statuses (`DEC-REGIME-REJECT-TIER-001`
    #: PROPOSAL consumer; producers = the flag-gated extreme-vol-stratum /
    #: regime-fragility filters, whose wire keys are EMIT-ON-PRESENT): the
    #: VERBATIM wire string when present (``"ok"`` / ``"fail"`` / an honest
    #: abstain token), ``None`` when the filter never ran (flag-OFF report —
    #: key absent) or the wire value was junk (nulled + counted, the
    #: ``rejected_at`` law).
    extreme_vol_status: str | None = None
    regime_fragility_status: str | None = None


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


def build_cascade_join(payload: object) -> CascadeJoin:
    """Flatten + validate one serialised cascade-report payload into a join.

    *payload* is the parsed JSON object — either
    ``cascade_wire.wire_report_to_dict(report)`` (the in-memory writer path)
    or ``json.loads(report bytes)`` (the ``/results`` read path). The full
    validation law runs in the Rust crate (module docstring).

    Raises
    ------
    CascadeJoinError
        On a STRUCTURALLY unusable payload (non-mapping root / version pun or
        unknown FUTURE version / missing sweep_id / non-list groups).
    """
    # Function-local version-constant import — the ONE writer home
    # (cascade_wire) owns REPORT_VERSION; no mirror constant here.
    from brutex.engine.cascade_wire import REPORT_VERSION

    rs = require_cascade_rust()
    try:
        raw = rs.build_cascade_join(payload, REPORT_VERSION)
    except ValueError as exc:
        raise CascadeJoinError(str(exc)) from None

    views = {
        view["candidate_id"]: CascadeVerdictView(
            candidate_id=view["candidate_id"],
            instrument=view["instrument"],
            tf=view["tf"],
            passed=view["passed"],
            rejected_at=view["rejected_at"],
            detail=view["detail"],
            cascade_composite=view["cascade_composite"],
            provisional=view["provisional"],
            cpcv_pass_rate=view["cpcv_pass_rate"],
            survivor_rank=view["survivor_rank"],
            extreme_vol_status=view["extreme_vol_status"],
            regime_fragility_status=view["regime_fragility_status"],
        )
        for view in raw["views"]
    }
    groups = tuple(
        CascadeGroupStats(
            instrument=group["instrument"],
            tf=group["tf"],
            n_input=group["n_input"],
            n_survived=group["n_survived"],
            pool_pbo=group["pool_pbo"],
            dsr_n_source=group["dsr_n_source"],
            dsr_n_trials=group["dsr_n_trials"],
            dsr_trial_variance=group["dsr_trial_variance"],
        )
        for group in raw["groups"]
    )
    if raw["warn"]:
        # ONE capped WARN per build (the #1631 named-partial-degrade law) —
        # counts exact, detail samples capped at _WARN_DETAIL_CAP.
        _LOG.warning(
            "cascade_join.partial",
            sweep_id=raw["sweep_id"],
            skipped_groups=raw["skipped_groups"],
            skipped_verdicts=raw["skipped_verdicts"],
            nulled_values=raw["nulled_values"],
            rank_inconsistencies=raw["rank_inconsistencies"],
            duplicate_ids=len(raw["ambiguous_ids"]),
            details=raw["notes"],
        )
    return CascadeJoin(
        sweep_id=raw["sweep_id"],
        version=raw["version"],
        n_input=raw["n_input"],
        n_survived=raw["n_survived"],
        by_candidate=views,
        ambiguous_ids=frozenset(raw["ambiguous_ids"]),
        groups=groups,
        skipped_groups=raw["skipped_groups"],
        skipped_verdicts=raw["skipped_verdicts"],
        nulled_values=raw["nulled_values"],
        rank_inconsistencies=raw["rank_inconsistencies"],
    )
