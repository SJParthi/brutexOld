"""Red-team pin: ci-ok's verify loop covers EVERY job in its ``needs:`` list.

DEC context: the ``ci-ok`` ("CI OK (aggregate gate)") job in
``.github/workflows/ci.yml`` gates merges by looping over
``needs.<job>.result`` values and failing on any non-success/non-skipped
result. Because GitHub Actions evaluates ``needs.<job>.result`` lazily as
TEXT inside the ``run:`` block, a job can sit in ci-ok's ``needs:`` list
yet be OMITTED from the verify loop — and a RED on that job then still
yields a GREEN aggregate gate (a mergeable red). This omission recurred
twice (``rust-brutex-feed`` + ``rust-brutex-strategies`` were both in
``needs:`` but absent from the echo block AND the verify loop).

These meta-tests pin the invariant STRUCTURALLY so any future
added-to-needs-but-not-loop job (e.g. the upcoming super-crate vehicle's
new crate jobs) fails CI immediately. They parse the workflow YAML
directly — no network — mirroring ``test_redteam_chaos_gate.py`` /
``test_redteam_automerge_safety.py``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Regex over the ci-ok run block ONLY (never the whole file — other jobs
# like the `test` fan-in legitimately reference their own needs results).
_NEEDS_RESULT_RE = re.compile(r"needs\.([A-Za-z0-9_-]+)\.result")

# The verify step's for-loop marker — splits the echo block (before) from
# the fail-closed loop (after). Both halves must cover the needs set.
_LOOP_MARKER = "for result in"


def _load_ci_ok_job() -> dict[str, Any]:
    """Return the parsed ``ci-ok`` job mapping from ci.yml."""
    data = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    jobs = data["jobs"]
    assert isinstance(jobs, dict)
    assert "ci-ok" in jobs, "the ci-ok aggregate job was removed from ci.yml"
    job = jobs["ci-ok"]
    assert isinstance(job, dict)
    return job


def _needs_set(job: dict[str, Any]) -> set[str]:
    needs = job["needs"]
    assert isinstance(needs, list)
    return {str(n) for n in needs}


def _verify_run_text(job: dict[str, Any]) -> str:
    """Return the run text of the ci-ok verify step (the ONLY step)."""
    steps = job["steps"]
    assert isinstance(steps, list)
    run_texts = [str(s.get("run", "")) for s in steps if _LOOP_MARKER in str(s.get("run", ""))]
    assert len(run_texts) == 1, (
        f"expected exactly one ci-ok step containing the verify for-loop; found {len(run_texts)}"
    )
    return run_texts[0]


def test_ci_ok_needs_set_is_nonempty_and_contains_known_members() -> None:
    """Anti-vacuous teeth: the needs set is real, not an empty pun."""
    needs = _needs_set(_load_ci_ok_job())
    assert len(needs) >= 30, f"ci-ok needs list suspiciously small: {sorted(needs)}"
    for known in ("lint", "test", "chaos-race", "rust-brutex-feed", "rust-brutex-strategies"):
        assert known in needs, f"known required gate {known!r} missing from ci-ok.needs"


def test_ci_ok_verify_loop_covers_needs_exactly_both_directions() -> None:
    """The fail-closed for-loop references EXACTLY the ci-ok needs set.

    missing (in needs, not in loop) = a mergeable red — a RED job whose
    result the loop never inspects still yields a GREEN aggregate gate.
    extra (in loop, not in needs) = an always-empty result string that
    would fail the loop's success/skipped check on every run (or reference
    a deleted job) — equally a wiring bug.
    """
    job = _load_ci_ok_job()
    needs = _needs_set(job)
    run_text = _verify_run_text(job)
    loop_text = run_text.split(_LOOP_MARKER, 1)[1]
    loop_refs = set(_NEEDS_RESULT_RE.findall(loop_text))
    missing = sorted(needs - loop_refs)
    extra = sorted(loop_refs - needs)
    assert not missing, (
        "ci-ok verify loop OMITS jobs present in its needs: list — a RED on any "
        f"of these still yields a GREEN aggregate gate (mergeable red): {missing}"
    )
    assert not extra, (
        "ci-ok verify loop references jobs NOT in its needs: list "
        f"(dangling/deleted gate references): {extra}"
    )


def test_ci_ok_echo_block_covers_needs_exactly_both_directions() -> None:
    """The diagnostic echo block also covers the needs set exactly.

    The echo block is observability, not enforcement — but an omission
    there is exactly how the loop omission escaped review twice, so both
    halves are pinned to the same set.
    """
    job = _load_ci_ok_job()
    needs = _needs_set(job)
    run_text = _verify_run_text(job)
    echo_text = run_text.split(_LOOP_MARKER, 1)[0]
    echo_refs = set(_NEEDS_RESULT_RE.findall(echo_text))
    missing = sorted(needs - echo_refs)
    extra = sorted(echo_refs - needs)
    assert not missing, f"ci-ok echo block OMITS jobs present in its needs: list: {missing}"
    assert not extra, f"ci-ok echo block references jobs NOT in its needs: list: {extra}"


def test_ci_ok_loop_is_fail_closed() -> None:
    """The loop fails on any result that is neither success nor skipped."""
    run_text = _verify_run_text(_load_ci_ok_job())
    loop_text = run_text.split(_LOOP_MARKER, 1)[1]
    assert '"success"' in loop_text and '"skipped"' in loop_text and "exit 1" in loop_text, (
        "ci-ok verify loop no longer fails closed on non-success/non-skipped results"
    )
