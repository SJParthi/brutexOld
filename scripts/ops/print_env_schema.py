#!/usr/bin/env python3
"""Print every ``BRUTEX_*`` env var the engine reads, with defaults.

Usage::

    python scripts/ops/print_env_schema.py            # human-readable
    python scripts/ops/print_env_schema.py --json     # JSON for tooling

Implementation: greps the source tree for ``ENV_*: Final[str] =
"BRUTEX_..."`` declarations + presents them grouped by module. The
docs/runbooks/ENV_VARS.md file is hand-maintained but should match
the output of this script; CI could check the drift in a future PR.

Decision: DEC-ENV-REGISTRY-001 (MIGRATION entry #244).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_ROOT = _REPO_ROOT / "brutex"

#: Regex that matches the canonical declaration pattern used across modules:
#:
#:     ENV_FOO: Final[str] = "BRUTEX_FOO"
#:
#: AND the explicit ``os.environ.get("BRUTEX_*"`` form for completeness.
_PATTERNS = (
    # [A-Z0-9_] (was [A-Z_]): digit-bearing names like
    # BRUTEX_CROSSVERIFY_S3_BUCKET were invisible to the registry
    # (DEC-CROSSVERIFY-PRODUCER-001, MIGRATION.md #1753).
    re.compile(r'ENV_[A-Z0-9_]+\s*:\s*Final\[str\]\s*=\s*"(BRUTEX_[A-Z0-9_]+)"'),
    re.compile(r'os\.environ\.get\(\s*"(BRUTEX_[A-Z0-9_]+)"'),
)


def collect_env_vars() -> dict[str, list[str]]:
    """Return ``{env_var_name: [source_file_relative_paths]}`` sorted by name."""
    found: dict[str, set[str]] = {}
    for path in _SOURCE_ROOT.rglob("*.py"):
        if "/tests/" in str(path) or "__pycache__" in str(path):
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in _PATTERNS:
            for match in pattern.finditer(text):
                env_name = match.group(1)
                rel = str(path.relative_to(_REPO_ROOT))
                found.setdefault(env_name, set()).add(rel)
    return {name: sorted(found[name]) for name in sorted(found)}


def print_human(env_vars: dict[str, list[str]]) -> None:
    """Print a human-readable table to stdout."""
    name_w = max(len(name) for name in env_vars) if env_vars else 0
    print(f"BruteX environment-variable registry — {len(env_vars)} vars\n")
    print(f"{'env var'.ljust(name_w)}  module(s)")
    print(f"{'-' * name_w}  {'-' * 60}")
    for name, sources in env_vars.items():
        for i, src in enumerate(sources):
            prefix = name.ljust(name_w) if i == 0 else " " * name_w
            print(f"{prefix}  {src}")


def print_json(env_vars: dict[str, list[str]]) -> None:
    """Print JSON output to stdout."""
    json.dump(env_vars, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of a human-readable table",
    )
    args = parser.parse_args()

    env_vars = collect_env_vars()
    if args.json:
        print_json(env_vars)
    else:
        print_human(env_vars)
    return 0


if __name__ == "__main__":  # pragma: no cover — CLI
    sys.exit(main())
