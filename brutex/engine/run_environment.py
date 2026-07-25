"""Current-environment run-identity strings (COMMON, Golden Rule #11).

ONE home for the two CURRENT-environment identity strings the run-identity
formula (CLAUDE.md §2 Rule 4) folds — the vocabulary/indicator stack version
and the code commit — so every consumer (the CLI sweep launcher's grid
context, the cascade manifest's vocabulary stamp REPL-01 gate, the
``sweep-verify --recount`` context gate) derives them from the SAME builders
instead of copy-pasting the ``git rev-parse`` / crate-version probes.

Extracted from the historical ``brutex.cli`` private shims
(``_resolve_code_commit_sha`` / ``_indicator_version`` — now thin delegators,
with ``brutex.cli._git_identity`` kept as the CLI-level monkeypatchable seam
over :func:`git_identity`) because :mod:`brutex.engine.cascade_wire` must
compare a REPLAY's current environment against the manifest's recorded one
without importing the CLI (the CLI imports the engine — a back-import would
cycle).

This leaf carries the DEC-SWEEP-RUN-IDENTITY-COMPLETE-001 completeness
guarantees (folded at the 2026-07-19 merge-reconcile of origin/main — main
enhanced the in-CLI bodies while this branch had extracted them here, so the
enhanced logic now lives at the ONE COMMON home):

* the SHA probe runs with an EXPLICIT ``cwd`` (this package's directory), so
  the resolved identity is INVARIANT to the process launch directory
  (R3-RUNID-01);
* a ``git status --porcelain`` probe appends ``-dirty`` so an uncommitted
  edited working tree can never serve stale pre-edit identity (RIF-2 / C9);
* an enclosing-repo ``ls-files --error-unmatch`` guard confirms the resolved
  repo actually TRACKS brutex before its HEAD is trusted (a nested wheel
  inside an unrelated checkout falls back honestly);
* ``current_indicator_version`` encodes the FULL Rust hot-kernel stack —
  BruteX + the ``fno_math`` sweep-simulation kernel + the shared
  ``trading_core`` evaluator crate — so an ``fno_math`` rebuild re-keys run
  identity (R3-RUNID-02).

Honest envelope (Golden Rule #10): ``current_code_commit_sha`` is a REAL
value, never a placeholder — the dirty-suffixed ``git rev-parse HEAD`` inside
a checkout, the pinned ``brutex-<version>`` fallback on a wheel install with
no repo. The ``-dirty`` suffix is a single boolean flag, NOT a working-tree
digest (two different uncommitted states over one HEAD share ``<sha>-dirty``);
dirty detection is best-effort (``status --porcelain`` also reports untracked
non-ignored files; a failed status probe serves the clean SHA rather than a
fabricated dirty flag). Both builders are O(1) probes (bounded subprocesses /
attribute reads), not cached — callers that need many reads should hold the
string.
"""

from __future__ import annotations

import subprocess  # nosec B404 — git SHA read only; fixed constant argv below
from pathlib import Path
from typing import TYPE_CHECKING, Any

from brutex import __version__

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "current_code_commit_sha",
    "current_indicator_version",
    "git_identity",
]


def git_identity(
    *,
    cwd: Path,
    tracked_path: Path | None = None,
    runner: Callable[..., Any] | None = None,
) -> str | None:
    """Resolve the git HEAD SHA of the repo rooted at *cwd*, dirty-suffixed.

    Returns the 40-char HEAD SHA (with a ``-dirty`` suffix when the working tree
    has uncommitted changes), or ``None`` when git is unavailable / *cwd* is not
    a checkout that TRACKS *tracked_path* (the caller then falls back to the
    pinned package version).

    Two run-identity completeness guarantees (DEC-SWEEP-RUN-IDENTITY-COMPLETE-001):

    * ``cwd`` is passed EXPLICITLY, so ``git rev-parse`` always resolves THIS
      repository regardless of the process launch directory. Without it the SHA
      forked with the invocation directory (R3-RUNID-01) — running the sweep from
      a sibling checkout served a foreign / corrupt identity.
    * a ``git status --porcelain`` probe (same ``cwd``) appends ``-dirty`` so an
      uncommitted, edited working tree can never serve stale pre-edit identity
      (RIF-2 / audit C9). If HEAD resolves but the dirty probe itself fails, the
      clean SHA is returned rather than a fabricated (unverified) dirty flag.

    Enclosing-repo guard (PR-4 review): ``git`` walks parent directories for a
    ``.git``, so a brutex package nested inside an UNRELATED enclosing repository
    (a non-editable wheel unpacked into a venv that itself lives inside another
    checkout) would resolve the ENCLOSING repo's HEAD — a FALSE code identity for
    brutex. When *tracked_path* is given, a ``git ls-files --error-unmatch`` probe
    (same ``cwd``, same *runner* seam) CONFIRMS the resolved repo actually tracks
    that file BEFORE the SHA is trusted; a non-zero exit (the path is not tracked
    here) ⇒ ``None`` ⇒ the honest ``brutex-{__version__}`` fallback. The
    editable / source deployment (file tracked by brutex's own repo) is
    unaffected — only the nested-enclosing-repo case changes.

    Honest limits (Golden Rule #10):

    * The ``-dirty`` suffix is a single boolean flag, NOT a working-tree digest —
      it collapses EVERY uncommitted state into one identity, so two DIFFERENT
      uncommitted edits over the same HEAD share ``<sha>-dirty`` and are NOT
      distinguished (dirty-state distinctness is not captured; a committed SHA is
      the only genuinely fork/collision-free code identity).
    * Dirty detection is best-effort: ``git status --porcelain`` also reports
      UNTRACKED non-ignored files, so a stray build artifact flips the flag to
      ``-dirty``; and a timed-out / errored status probe drops the flag and serves
      the clean SHA.

    *runner* injects the subprocess entry point (defaults to ``subprocess.run``)
    so the git seam is fully unit-testable without a live repository.
    """
    run = runner if runner is not None else subprocess.run
    try:
        head = run(  # nosec B603 B607 — fixed constant argv, no shell, git on PATH
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
            cwd=cwd,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = head.stdout.strip()
    if not sha:
        return None
    if tracked_path is not None:
        # Confirm the resolved repo actually TRACKS this file before trusting its
        # HEAD — an enclosing foreign repo would otherwise serve a false identity.
        # `ls-files --error-unmatch` exits non-zero when the path is untracked here.
        try:
            run(  # nosec B603 B607 — fixed constant argv, no shell, git on PATH
                ["git", "ls-files", "--error-unmatch", "--", str(tracked_path)],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
                cwd=cwd,
            )
        except (OSError, subprocess.SubprocessError):
            return None
    try:
        status = run(  # nosec B603 B607 — fixed constant argv, no shell, git on PATH
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
            cwd=cwd,
        )
    except (OSError, subprocess.SubprocessError):
        return sha
    return f"{sha}-dirty" if status.stdout.strip() else sha


def current_code_commit_sha() -> str:
    """Return the real current git commit SHA, falling back to the package version.

    Golden Rule #1/#4: the run-identity's ``code_commit_sha`` must be a REAL
    value, never a placeholder. Delegates to :func:`git_identity` rooted at
    THIS module's directory, passing THIS file as the ``tracked_path`` so the
    resolved repo is confirmed to actually track brutex (an enclosing foreign
    repo around a nested wheel is rejected → the honest fallback, never its
    HEAD). The SHA is INVARIANT to the process working directory and carries a
    ``-dirty`` suffix on an uncommitted tree
    (DEC-SWEEP-RUN-IDENTITY-COMPLETE-001). If git is unavailable (e.g. a wheel
    install with no repo) or the resolved repo does not track brutex, falls
    back to the pinned ``brutex.__version__`` — still a real, traceable code
    identity. The CLI's ``_resolve_code_commit_sha`` derives the SAME string
    through the SAME :func:`git_identity` seam (rooted at its own tracked file
    in the SAME repo), so the launcher and the cascade replay's REPL-01 gate
    can never drift apart.
    """
    pkg_file = Path(__file__).resolve()
    sha = git_identity(cwd=pkg_file.parent, tracked_path=pkg_file)
    if sha is not None:
        return sha
    return f"brutex-{__version__}"


def current_indicator_version() -> str:
    """Return a real indicator-stack version string.

    Encodes the FULL Rust hot-kernel stack in run identity: BruteX + the
    ``fno_math`` sweep-simulation kernel + the shared ``trading_core``
    evaluator crate. The kernel tokens are appended in a fixed alphabetical
    order (``fno_math`` before ``trading_core``) so the string is
    deterministic. The shared ``rust/trading-core`` crate version IS the
    vocabulary identity (the crate + the generated bit layout move together —
    a vocabulary reshape bumps it, e.g. 0.3.2 → 0.4.0 for the 66-condition
    reshape), so this string is what the REPL-01 cascade-replay gate compares.

    Adding ``fno_math`` closes R3-RUNID-02 (DEC-SWEEP-RUN-IDENTITY-COMPLETE-001):
    the prior string omitted it, so an ``fno_math`` rebuild did NOT re-key run
    identity — a stale sweep-sim kernel could serve results under an unchanged
    identity. A missing module (a source checkout without a built wheel) drops
    only its own token; the remaining order is unchanged.
    """
    parts = [f"brutex-{__version__}"]
    try:
        import fno_math

        parts.append(f"fno_math-{fno_math.__version__}")
    except (ImportError, AttributeError):
        # No built fno_math wheel (source checkout) → drop only its token and
        # keep the deterministic order; a missing kernel never fails identity.
        pass
    try:
        import trading_core

        parts.append(f"trading_core-{trading_core.__version__}")
    except (ImportError, AttributeError):
        # No built trading_core wheel → drop only its token; the remaining
        # tokens still form a real, traceable indicator-stack identity.
        pass
    return "+".join(parts)
