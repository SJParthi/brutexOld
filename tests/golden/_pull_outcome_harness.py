"""Shared case-enumeration harness for the pull-outcome goldens (DEC-RUST-PULL-STATE-001).

Imported by BOTH ``generate_pull_outcome_golden.py`` (the one-shot freeze) and
``test_pull_outcome_golden.py`` (the recompute-and-compare gate) — the
``_market_hours_guard_harness`` shape, never generator-imports-test.

Two tables are computed from the LIVE ``brutex.dashboard.pull_outcome``:

* the MODULE-level classification table — every enumerated
  ``classify_last_pull_outcome`` / ``classify_retained_pull_outcome`` case with
  its full ``LastPullOutcome.to_dict()`` output (outcome · tone · headline ·
  detail).  This is the byte-level ORACLE the Rust seam must preserve.
* the KERNEL-level contract rows — the same classify cases projected onto the
  planned Rust decision-kernel contract ``(outcome, tone, headline_code,
  detail_code)`` (verdict-consuming arms marked ``@verdict`` /
  ``@verdict_prepull`` — the banner fold + colour authority stay Python), plus
  the run-id correlation-law rows.  The committed TSV is read by the crate's
  own ``cargo test`` (dependency-free line format — the cross-language pin).

The kernel projection is DERIVED mechanically from the live module's outputs
(never a re-statement of the precedence law): with the ONE fixed *clean*
verdict, an output outcome of ``"clean"`` is possible ONLY when the verdict
arm consumed it, and every other arm's code follows 1:1 from the output
outcome (the sole outcome→headline collision — the two ``halted_quota``
wordings — is resolved by the recorded exception NAME, which IS the module's
own table key).
"""

from __future__ import annotations

from typing import Any

from brutex.dashboard.pull_outcome import (
    _HALT_NAME_OUTCOMES,
    MARKET_HOURS_DEFERRAL_PREFIX,
    classify_last_pull_outcome,
    classify_retained_pull_outcome,
)
from brutex.dashboard.pull_jobs import RetainedPullState
from brutex.dashboard.pull_progress import ProgressSnapshot
from brutex.dashboard.verdict_banner import _KIND_STYLE

#: The fixed verdict used for every ``has_verdict`` kernel case (clean — the
#: only kind whose consumption is unambiguous from the output alone).
CLEAN_VERDICT_TOKEN = "kind:clean"

#: Detail-string corpus — the classification-relevant shapes (prefix hit /
#: near-miss, the ``LakeWriteHalt`` reason splits, every halt-table name, the
#: colon/name edge cases, unicode).  NO tabs/newlines (the TSV contract; the
#: generator fail-loud asserts it).
DETAILS: tuple[str, ...] = (
    "",
    "routine note",
    MARKET_HOURS_DEFERRAL_PREFIX,
    MARKET_HOURS_DEFERRAL_PREFIX + " (block ends 15:35 IST)",
    MARKET_HOURS_DEFERRAL_PREFIX[:-1],  # truncated prefix — must NOT match
    " " + MARKET_HOURS_DEFERRAL_PREFIX,  # leading space — must NOT match
    "LakeWriteHalt: lake write halted: disk_full",
    "LakeWriteHalt: lake write halted: out_of_memory",
    "LakeWriteHalt: lake write halted: mystery",
    "LakeWriteHalt",  # name matches with NO colon → the other-reason arm
    "LakeWriteHalt without colon",  # whole string is the name → generic error
    "ValueError: disk_full mentioned outside a lake halt",
    "ошибка: юникод в деталях",
    ": leading colon means empty exception name",
    "MarketHoursHalt without colon",  # near-miss of the table name
    *(f"{name}: boom" for name in sorted(_HALT_NAME_OUTCOMES)),
)

#: The verdict-shape tokens the module-level table sweeps (mapped to real
#: mappings by :func:`verdict_from_token`).
VERDICT_TOKENS: tuple[str, ...] = (
    *(f"kind:{kind}" for kind in sorted(_KIND_STYLE)),
    "kind_empty_action:clean",
    "kind_empty_action:credentials_missing",
    "unknown_kind",
    "malformed",
)


def verdict_from_token(token: str | None) -> dict[str, Any] | None:
    """Materialise a verdict-shape token into the mapping the module consumes."""
    if token is None:
        return None
    if token.startswith("kind:"):
        kind = token.removeprefix("kind:")
        return _verdict(kind)
    if token.startswith("kind_empty_action:"):
        kind = token.removeprefix("kind_empty_action:")
        return _verdict(kind, action="")
    if token == "unknown_kind":
        return _verdict("bogus_kind_token")  # banner_for → ValueError → unreadable
    if token == "malformed":
        return {"kind": "clean"}  # missing headline/action → KeyError → unreadable
    raise ValueError(f"unknown verdict token {token!r}")


def _verdict(kind: str, *, action: str = "do the thing") -> dict[str, Any]:
    """The minimal ``PullVerdict.to_dict``-shaped mapping (the unit-suite shape)."""
    return {
        "kind": kind,
        "headline": f"headline for {kind}",
        "action": action,
        "pending_windows": 3,
        "needs_operator": kind != "clean",
    }


def classify_cases() -> list[dict[str, Any]]:
    """Enumerate the ``classify_last_pull_outcome`` input lattice (deterministic order)."""
    cases: list[dict[str, Any]] = []

    def add(
        state: str,
        detail: str,
        failed: int = 0,
        *,
        stalled: bool = False,
        wedged: bool = False,
        verdict: str | None = None,
    ) -> None:
        cases.append(
            {
                "state": state,
                "detail": detail,
                "failed": failed,
                "stalled": stalled,
                "wedged": wedged,
                "verdict": verdict,
            }
        )

    # (A) the state machine × the stalled/wedged flags (fixed routine detail).
    for state in ("running", "error", "done", "idle", "limbo-token"):
        for stalled in (False, True):
            for wedged in (False, True):
                add(state, "routine note", stalled=stalled, wedged=wedged)
    # (B) the error-detail classification over the FULL detail corpus (+ two
    # rows proving the error arm ignores a correlated verdict).
    for detail in DETAILS:
        add("error", detail)
    add("error", "routine note", verdict=CLEAN_VERDICT_TOKEN)
    add("error", "LakeWriteHalt: lake write halted: disk_full", verdict=CLEAN_VERDICT_TOKEN)
    # (C) the done arm: deferral-prefix / failed-count / verdict precedence.
    for detail in DETAILS[:6]:
        for failed in (0, 1, 3):
            for verdict in (None, CLEAN_VERDICT_TOKEN):
                add("done", detail, failed, verdict=verdict)
    # (D) the idle (and unknown-state fall-through) arm.
    for state in ("idle", "limbo-token"):
        for detail in ("", "routine note"):
            for verdict in (None, CLEAN_VERDICT_TOKEN):
                add(state, detail, verdict=verdict)
    # (E) the running arm ignores the detail's classification shapes.
    add("running", MARKET_HOURS_DEFERRAL_PREFIX)
    add("running", "LakeWriteHalt: lake write halted: disk_full")
    # (F) the verdict-shape sweep (done + idle consume it; colours via
    # banner_for — the Python authority).
    for state in ("done", "idle"):
        for token in VERDICT_TOKENS:
            add(state, "", verdict=token)
    return cases


def compute_classify_table() -> list[dict[str, Any]]:
    """Run every case through the LIVE classifier → ``[{"case": …, "out": …}]``."""
    rows: list[dict[str, Any]] = []
    for case in classify_cases():
        out = classify_last_pull_outcome(
            state=case["state"],
            detail=case["detail"],
            failed=case["failed"],
            stalled=case["stalled"],
            wedged=case["wedged"],
            verdict=verdict_from_token(case["verdict"]),
        )
        rows.append({"case": case, "out": out.to_dict()})
    return rows


#: Retained-correlation cases: (retained_pull_id | None, snapshot kwargs,
#: verdict token, verdict_pull_id).  ``retained_pull_id is None`` = no
#: retained state at all (the never-ran slot).
RETAINED_CASES: tuple[tuple[str | None, dict[str, Any], str | None, str], ...] = (
    (None, {}, None, ""),
    (None, {}, CLEAN_VERDICT_TOKEN, "ghost-run"),  # pre-pull refusal surfaces
    ("p1", {"state": "done"}, CLEAN_VERDICT_TOKEN, "p1"),  # match → consulted
    ("p1", {"state": "done"}, CLEAN_VERDICT_TOKEN, "p2"),  # mismatch → dropped
    ("p1", {"state": "done"}, CLEAN_VERDICT_TOKEN, ""),  # empty holder id
    ("", {"state": "done"}, CLEAN_VERDICT_TOKEN, ""),  # run-less record: never
    ("p9", {"state": "error", "detail": "DailyQuotaExceeded: boom"}, None, ""),
    (
        "p9",
        {"state": "running", "stalled": True, "detail": "no beats"},
        CLEAN_VERDICT_TOKEN,
        "p9",
    ),  # threading: running outranks the correlated verdict
    ("p3", {"state": "done", "failed": 2, "detail": "two failed"}, "kind:clean", "p3"),
)


def _snapshot(**overrides: Any) -> ProgressSnapshot:
    """A minimal :class:`ProgressSnapshot` (the unit-suite ``_snap`` shape)."""
    kwargs: dict[str, Any] = {
        "state": "done",
        "total": 1,
        "completed": 1,
        "failed": 0,
        "bars": 0,
        "current": "",
        "detail": "",
        "updated_at": 0.0,
        "stalled": False,
        "wedged": False,
    }
    kwargs.update(overrides)
    return ProgressSnapshot(**kwargs)


def compute_retained_table() -> list[dict[str, Any]]:
    """Run every retained-correlation case through the LIVE law."""
    rows: list[dict[str, Any]] = []
    for pull_id, snap_kwargs, token, verdict_pull_id in RETAINED_CASES:
        retained = (
            None
            if pull_id is None
            else RetainedPullState(snapshot=_snapshot(**snap_kwargs), pull_id=pull_id)
        )
        out = classify_retained_pull_outcome(
            retained,
            verdict=verdict_from_token(token),
            verdict_pull_id=verdict_pull_id,
        )
        rows.append(
            {
                "case": {
                    "retained_pull_id": pull_id,
                    "snapshot": snap_kwargs,
                    "verdict": token,
                    "verdict_pull_id": verdict_pull_id,
                },
                "out": out.to_dict(),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# The KERNEL contract projection (the Rust decision-kernel's I/O rows).
# ---------------------------------------------------------------------------

#: Outcomes minted ONLY by the ``_HALT_NAME_OUTCOMES`` table (used to detect
#: table arms from the output; the two ``halted_quota`` wordings collide on
#: outcome, so the headline code carries the exception NAME).
_TABLE_OUTCOMES = frozenset(entry[0] for entry in _HALT_NAME_OUTCOMES.values())

#: outcome → headline code for the arms whose code is NOT the outcome itself.
_HEADLINE_CODE_OVERRIDES = {
    "running_stalled_suspect": "stalled",
    "error": "failed",
    "completed_with_failures": "completed_failures",
}


def kernel_projection(case: dict[str, Any], out: dict[str, str]) -> tuple[str, str, str, str]:
    """Project one module-level row onto the kernel contract 4-tuple.

    Derived from the LIVE output (never a re-statement of the precedence law):
    ``"clean"`` can only come from the fixed clean verdict's consumption.
    """
    if case["verdict"] is not None and out["outcome"] == "clean":
        # The verdict arm consumed it — the kernel defers the banner fold to
        # Python.  Which sub-arm is mechanical from the consuming state.
        marker = "@verdict" if case["state"] == "done" else "@verdict_prepull"
        return (marker, "", "", "")
    outcome = out["outcome"]
    if outcome in _TABLE_OUTCOMES and case["state"] == "error":
        headline_code = "halt:" + case["detail"].split(":", 1)[0]
    else:
        headline_code = _HEADLINE_CODE_OVERRIDES.get(outcome, outcome)
    if outcome == "never_run":
        detail_code = "empty"
    elif outcome == "completed":
        detail_code = "completed"
    else:
        detail_code = "input"
    return (outcome, out["tone"], headline_code, detail_code)


def compute_kernel_rows() -> list[str]:
    """The TSV rows the crate's fixture test consumes (tab-joined, no header).

    ``classify`` rows: ``classify \\t state \\t detail \\t failed \\t stalled
    \\t wedged \\t has_verdict \\t outcome \\t tone \\t headline_code \\t
    detail_code`` (booleans as ``0``/``1``).  ``correlates`` rows:
    ``correlates \\t retained_id \\t verdict_id \\t expected``.
    """
    rows: list[str] = []
    seen: set[tuple[str, str, int, bool, bool, bool]] = set()
    for entry in compute_classify_table():
        case, out = entry["case"], entry["out"]
        has_verdict = case["verdict"] is not None
        # The kernel sees only has_verdict — dedupe kernel-identical inputs
        # (different verdict SHAPES map to one kernel row).
        key = (
            case["state"],
            case["detail"],
            case["failed"],
            case["stalled"],
            case["wedged"],
            has_verdict,
        )
        if key in seen:
            continue
        seen.add(key)
        outcome, tone, headline_code, detail_code = kernel_projection(case, out)
        rows.append(
            "\t".join(
                (
                    "classify",
                    case["state"],
                    case["detail"],
                    str(case["failed"]),
                    "1" if case["stalled"] else "0",
                    "1" if case["wedged"] else "0",
                    "1" if has_verdict else "0",
                    outcome,
                    tone,
                    headline_code,
                    detail_code,
                )
            )
        )
    # The correlation-law rows (the classify_retained_pull_outcome predicate).
    for retained_id, verdict_id in (
        ("p1", "p1"),
        ("p1", "p2"),
        ("p1", ""),
        ("", ""),
        ("", "p1"),
        ("run-α", "run-α"),
    ):
        expected = bool(retained_id and verdict_id == retained_id)
        rows.append(
            "\t".join(("correlates", retained_id, verdict_id, "1" if expected else "0"))
        )
    return rows
