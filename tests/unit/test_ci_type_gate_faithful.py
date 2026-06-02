"""Guard: the CI type-check job must stay faithful (DEC-PYRIGHT-UNMASK-001).

If the `type` job reverts to a `[dev]`-only install, pyright stops resolving
fastapi / pydantic / polars / duckdb types and silently **re-masks** the
dashboard + data-path type errors (the same class of hole as the #376 / #280
test-masking). This test fails if that regression returns.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CI_YML = _REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: The marker for the type-check job, and the window of text after it that must
#: contain the faithful install (the job is short; 1500 chars is comfortably
#: past its install step without assuming the next job's name/order).
_TYPE_JOB_MARKER = "Type check (mypy + pyright)"
_WINDOW = 1500


def test_type_job_installs_runtime_extras_for_faithful_pyright() -> None:
    """The type job installs `[dev,data,ui]` so pyright sees real types, not Unknown."""
    text = _CI_YML.read_text(encoding="utf-8")
    assert _TYPE_JOB_MARKER in text, "ci.yml lost the type-check job"
    after = text.split(_TYPE_JOB_MARKER, 1)[1][:_WINDOW]
    assert '".[dev,data,ui]"' in after, (
        "the type-check job must install the [data,ui] runtime extras "
        "(DEC-PYRIGHT-UNMASK-001) — a [dev]-only install re-masks dashboard/data "
        "pyright errors"
    )
