#!/usr/bin/env python3
"""Deterministic test-FILE sharding for the whole-package coverage job.

DEC-SWEEP-COVERAGE-SHARD-001: the CI `test` job's whole-package
``pytest --cov=brutex`` pass is split across N parallel matrix shards.
This script is the ONE partition law both the shard jobs and any local
reproduction use:

* COLLECT every pytest test FILE under the target dirs — the glob is
  ``test_*.py`` (recursive), mirroring pyproject's
  ``[tool.pytest.ini_options] python_files = ["test_*.py"]`` exactly, so
  the file universe equals what ``pytest <dirs>`` would collect from.
* ORDER deterministically by (descending file size, ascending path) —
  file size is a cheap, checkout-stable balance heuristic that spreads
  the heavyweight suites across shards (NOT a perfect duration model;
  the shard timeout carries the honest headroom).
* DEAL round-robin: file ``i`` in that order goes to shard ``i % N``.

The partition is TOTAL and DISJOINT by construction (every file assigned
to exactly one shard); ``--self-check`` mechanically re-verifies both
properties and the CI fan-in job additionally proves the stronger
TEST-level law (sum of per-shard collected test counts == the full
collection count) so zero tests are skipped and zero duplicated.

Zero third-party dependencies (stdlib-only, like the sibling
``bench_baseline_gate.py``): the shard jobs call this BEFORE pytest runs.

Usage
-----
::

    python scripts/ci/shard_tests.py --shard-index 2 --shard-count 6
    python scripts/ci/shard_tests.py --self-check --shard-count 6

Prints one repo-relative POSIX file path per line (shard mode), or a
summary line (self-check mode). Exit codes: 0 OK; 2 usage/invariant
error (loud — a broken partition must never silently drop a file).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

#: The whole-package coverage job's pytest target dirs (ci.yml `test` job;
#: tests/bench is excluded there via --ignore and is likewise absent here).
DEFAULT_DIRS: tuple[str, ...] = ("tests/unit", "tests/integration", "tests/property")

#: Mirrors pyproject [tool.pytest.ini_options] python_files exactly.
TEST_FILE_GLOB = "test_*.py"


def repo_root() -> Path:
    """The repository root (this file lives at scripts/ci/shard_tests.py)."""
    return Path(__file__).resolve().parents[2]


def collect_test_files(root: Path, dirs: tuple[str, ...]) -> list[Path]:
    """Every pytest-collectable test file under ``dirs``, sorted by path."""
    files: set[Path] = set()
    for rel in dirs:
        base = root / rel
        if not base.is_dir():
            raise SystemExit(f"shard_tests: target dir missing: {rel} (cwd/root drift?)")
        files.update(p for p in base.rglob(TEST_FILE_GLOB) if p.is_file())
    ordered = sorted(files)
    if not ordered:
        raise SystemExit("shard_tests: zero test files collected — refusing to emit an empty universe")
    return ordered


def partition(files: list[Path], shard_count: int) -> list[list[Path]]:
    """Deal files round-robin over ``shard_count`` shards (size-desc order)."""
    if shard_count < 1:
        raise SystemExit(f"shard_tests: --shard-count must be >= 1, got {shard_count}")
    if shard_count > len(files):
        raise SystemExit(
            f"shard_tests: --shard-count {shard_count} exceeds the {len(files)} "
            "collected test files — an empty shard would run pytest with no "
            "targets (exit 5); lower the shard count"
        )
    keyed = sorted(files, key=lambda p: (-p.stat().st_size, str(p)))
    shards: list[list[Path]] = [[] for _ in range(shard_count)]
    for i, path in enumerate(keyed):
        shards[i % shard_count].append(path)
    return [sorted(s) for s in shards]


def self_check(files: list[Path], shards: list[list[Path]]) -> None:
    """Loudly verify the partition is total + disjoint (zero skip, zero dup)."""
    seen: set[Path] = set()
    for idx, shard in enumerate(shards):
        overlap = seen.intersection(shard)
        if overlap:
            raise SystemExit(f"shard_tests: SELF-CHECK FAILED — shard {idx} duplicates {sorted(overlap)[:3]}")
        seen.update(shard)
    missing = set(files) - seen
    extra = seen - set(files)
    if missing or extra:
        raise SystemExit(
            f"shard_tests: SELF-CHECK FAILED — missing={sorted(missing)[:3]} extra={sorted(extra)[:3]}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--shard-index", type=int, default=None, help="0-based shard index")
    parser.add_argument("--shard-count", type=int, required=True, help="total number of shards")
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="verify the whole partition is total + disjoint, print a summary, emit no file list",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="repo root override (tests only; default: derived from this file's location)",
    )
    parser.add_argument(
        "--dirs",
        nargs="*",
        default=list(DEFAULT_DIRS),
        help="target test dirs relative to the root (default: the ci.yml coverage-job trio)",
    )
    args = parser.parse_args(argv)

    root = (args.root or repo_root()).resolve()
    files = collect_test_files(root, tuple(args.dirs))
    shards = partition(files, args.shard_count)
    self_check(files, shards)

    if args.self_check:
        sizes = [len(s) for s in shards]
        print(
            f"shard_tests self-check OK: {len(files)} test files -> "
            f"{args.shard_count} shards {sizes} (total {sum(sizes)}, disjoint, complete)"
        )
        return 0

    if args.shard_index is None:
        raise SystemExit("shard_tests: --shard-index is required unless --self-check")
    if not 0 <= args.shard_index < args.shard_count:
        raise SystemExit(
            f"shard_tests: --shard-index {args.shard_index} out of range [0, {args.shard_count})"
        )
    for path in shards[args.shard_index]:
        print(path.relative_to(root).as_posix())
    return 0


if __name__ == "__main__":
    sys.exit(main())
