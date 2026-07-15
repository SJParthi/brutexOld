"""Current-environment run-identity strings (COMMON, Golden Rule #11).

ONE home for the two CURRENT-environment identity strings the run-identity
formula (CLAUDE.md §2 Rule 4) folds — the vocabulary/indicator stack version
and the code commit — so every consumer (the CLI sweep launcher's grid
context, the cascade manifest's vocabulary stamp REPL-01 gate, the
``sweep-verify --recount`` context gate) derives them from the SAME builders
instead of copy-pasting the ``git rev-parse`` / crate-version probes.

Extracted from the historical ``brutex.cli`` private shims
(``_resolve_code_commit_sha`` / ``_indicator_version``, now thin delegators)
because :mod:`brutex.engine.cascade_wire` must compare a REPLAY's current
environment against the manifest's recorded one without importing the CLI
(the CLI imports the engine — a back-import would cycle).

Honest envelope (Golden Rule #10): ``current_code_commit_sha`` is a REAL
value, never a placeholder — ``git rev-parse HEAD`` inside a checkout, the
pinned ``brutex-<version>`` fallback on a wheel install with no repo. Both
are O(1) probes (one bounded subprocess / one attribute read), not cached —
callers that need many reads should hold the string.
"""

from __future__ import annotations

from brutex import __version__

__all__ = [
    "current_code_commit_sha",
    "current_indicator_version",
]


def current_code_commit_sha() -> str:
    """Return the real current git commit SHA, falling back to the package version.

    Golden Rule #1/#4: the run-identity's ``code_commit_sha`` must be a REAL
    value, never a placeholder. Uses ``git rev-parse HEAD`` when run inside a
    checkout; if git is unavailable (e.g. a wheel install with no repo), falls
    back to the pinned ``brutex.__version__`` — still a real, traceable code
    identity.
    """
    import subprocess  # nosec B404 — git SHA read only; fixed constant argv below

    try:
        out = subprocess.run(  # nosec B603 B607 — fixed constant argv, no shell, git on PATH
            ["git", "rev-parse", "HEAD"],  # noqa: S607 — fixed constant argv, no shell/user input
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        sha = out.stdout.strip()
        if sha:
            return sha
    except (OSError, subprocess.SubprocessError):
        pass
    return f"brutex-{__version__}"


def current_indicator_version() -> str:
    """Return a real indicator-stack version string (BruteX + shared trading_core).

    The shared ``rust/trading-core`` crate version IS the vocabulary identity
    (the crate + the generated bit layout move together — a vocabulary reshape
    bumps it, e.g. 0.3.2 → 0.4.0 for the 66-condition reshape), so this string
    is what the REPL-01 cascade-replay gate compares. Falls back to the bare
    BruteX version when the wheel is absent (a docs-only environment).
    """
    try:
        import trading_core

        return f"brutex-{__version__}+trading_core-{trading_core.__version__}"
    except (ImportError, AttributeError):
        return f"brutex-{__version__}"
