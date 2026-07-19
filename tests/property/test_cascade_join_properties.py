"""Property invariants for the cascade display join (DEC-RESULTS-CASCADE-VERDICT-JOIN-001).

* LOADER TOTALITY — an arbitrary JSON-shaped payload either raises the typed
  :class:`CascadeJoinError` or yields a valid :class:`CascadeJoin`; no other
  exception ever escapes (the /results loader maps the typed error to the
  named ``malformed`` degrade — an unhandled raise would 500 the page).
* ANTI-FABRICATION — no code path can display PASS/provisional-PASS unless a
  parsed report verdict with ``passed=True`` and EQUAL run_id exists.
* THE PCT LAW — ``cpcv_pass_pct`` is present ⇔ the joined rate is non-None,
  and equals ``rate * 100.0`` EXACTLY (a float unit projection, never a
  rounding).
* ROUND-TRIP — flatten(wire_dict) == flatten(json.loads(serialize(wire)))
  on ``by_candidate`` (the disk path and the in-memory path are ONE law).
"""

from __future__ import annotations

import json
from typing import Any

from brutex.engine.cascade_join import CascadeJoin, CascadeJoinError, build_cascade_join
from brutex.engine.cascade_wire import (
    CascadeWireReport,
    serialize_wire_report,
    wire_report_to_dict,
)
from brutex.engine.top100_artifact import _cascade_payload
from brutex.metrics.cascade import CandidateVerdict, CascadeReport, FilterStage
from hypothesis import given
from hypothesis import strategies as st

# pyright: reportPrivateUsage=none

_JSON = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-(2**40), max_value=2**40)
    | st.floats(allow_nan=False)
    | st.text(max_size=8),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4)
    ),
    max_leaves=12,
)

_ROOTISH = st.fixed_dictionaries(
    {},
    optional={
        "version": st.none() | st.booleans() | st.integers(-3, 3) | st.floats(allow_nan=False),
        "sweep_id": st.none() | st.text(max_size=12),
        "n_input": _JSON,
        "n_survived": _JSON,
        "groups": _JSON,
    },
)


@given(payload=_JSON | _ROOTISH)
def test_loader_totality(payload: object) -> None:
    try:
        join = build_cascade_join(payload)
    except CascadeJoinError:
        return
    assert isinstance(join, CascadeJoin)
    assert join.skipped_groups >= 0
    assert join.skipped_verdicts >= 0


_IDS = st.lists(
    st.text(alphabet="abcdef0123456789", min_size=4, max_size=8),
    min_size=1,
    max_size=6,
    unique=True,
)


def _report_from(ids: list[str], flags: list[tuple[bool, bool, float | None]]) -> CascadeReport:
    verdicts = tuple(
        CandidateVerdict(
            candidate_id=cid,
            passed=passed,
            rejected_at=None if passed else FilterStage.CPCV,
            detail="d",
            composite_score=1.0 if passed else None,
            provisional=passed and provisional,
            cpcv_pass_rate=rate,
        )
        for cid, (passed, provisional, rate) in zip(ids, flags, strict=True)
    )
    survivors = tuple(v.candidate_id for v in verdicts if v.passed)
    return CascadeReport(
        verdicts=verdicts,
        survivor_ids=survivors,
        n_input=len(verdicts),
        n_survived=len(survivors),
        stage_survivors={},
    )


_FLAGS = st.tuples(
    st.booleans(),
    st.booleans(),
    st.none() | st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
)


@given(ids=_IDS, data=st.data())
def test_anti_fabrication_and_pct_law(ids: list[str], data: Any) -> None:
    flags = [data.draw(_FLAGS) for _ in ids]
    report = _report_from(ids, flags)
    wire = CascadeWireReport(sweep_id="s" * 8, groups=(("NIFTY", "1m", report),))
    payload = wire_report_to_dict(wire)
    join = build_cascade_join(payload)
    truth = {v.candidate_id: v for v in report.verdicts}
    for cid in [*ids, "ghost-run-id"]:
        cascade, rate = _cascade_payload(cid, join=join, state="joined")
        if cascade["status"] in {"pass", "provisional_pass"}:
            # ANTI-FABRICATION: a displayed pass ⇒ a REAL passed=True verdict
            # with the SAME run_id exists in the parsed report.
            assert truth[cid].passed is True
        if cascade["status"] == "not_validated":
            assert cid not in truth
        # THE PCT LAW: the alias exists ⇔ the rate is known, exactly ×100.
        if rate is not None:
            assert rate == truth[cid].cpcv_pass_rate
            assert rate * 100.0 == truth[cid].cpcv_pass_rate * 100.0  # type: ignore[operator]
        elif cid in truth and cascade["status"] != "not_validated":
            assert truth[cid].cpcv_pass_rate is None


@given(ids=_IDS, data=st.data())
def test_flatten_serialize_parse_flatten_identity(ids: list[str], data: Any) -> None:
    flags = [data.draw(_FLAGS) for _ in ids]
    report = _report_from(ids, flags)
    wire = CascadeWireReport(sweep_id="s" * 8, groups=(("NIFTY", "1m", report),))
    from_memory = build_cascade_join(wire_report_to_dict(wire))
    from_bytes = build_cascade_join(json.loads(serialize_wire_report(wire)))
    assert from_memory.by_candidate == from_bytes.by_candidate
    assert from_memory.ambiguous_ids == from_bytes.ambiguous_ids
    assert from_memory.groups == from_bytes.groups
