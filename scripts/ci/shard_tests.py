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
* WEIGH each file (DEC-CI-SHARD-REBALANCE-001, the follow-up the round
  -robin law's "shard balance is a file-size heuristic" honesty clause
  named — run 29644876109 measured legs 9.8/10.3/11.8/**43.0**/12.1/8.8
  min because the whale suites co-located on one stride): a file's
  weight is its MEASURED duration in seconds from the committed hints
  file ``scripts/ci/shard_weights.json`` when listed there, else the
  size-proxy ``st_size * default_seconds_per_byte`` (floored at a tiny
  positive epsilon so every file has strictly positive weight). A hint
  entry whose file no longer exists is ignored harmlessly (it simply is
  never looked up); a hints file that exists but fails to parse or
  violates the schema FAILS LOUD (fail-closed — never a silent
  fallback); an ABSENT hints file is fine (all-proxy weights — the
  synthetic-tree tests run that way).
* PARTITION by deterministic greedy LPT (longest-processing-time
  first): files sorted by (weight desc, path asc), each assigned to the
  currently-lightest bin (tie -> lowest bin index). Same inputs -> the
  same partition, machine-independent (weights come from the committed
  JSON + checkout-stable file sizes; no mtimes, no locale, no pytest
  invocation).

The partition is TOTAL and DISJOINT by construction (every file assigned
to exactly one shard); ``--self-check`` mechanically re-verifies both
properties and the CI fan-in job additionally proves the stronger
TEST-level law (sum of per-shard collected test counts == the full
collection count) so zero tests are skipped and zero duplicated. Because
every weight is strictly positive, LPT seeds the N heaviest files into N
distinct bins, so no shard is ever empty while ``shard_count <= files``.

Zero third-party dependencies (stdlib-only, like the sibling
``bench_baseline_gate.py``): the shard jobs call this BEFORE pytest runs.

Usage
-----
::

    python scripts/ci/shard_tests.py --shard-index 2 --shard-count 6
    python scripts/ci/shard_tests.py --self-check --shard-count 6

Prints one repo-relative POSIX file path per line (shard mode), or a
summary (self-check mode, including the projected per-shard weight
totals). Exit codes: 0 OK; 2 usage/invariant error (loud — a broken
partition must never silently drop a file).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

#: The whole-package coverage job's pytest target dirs (ci.yml `test` job;
#: tests/bench is excluded there via --ignore and is likewise absent here).
DEFAULT_DIRS: tuple[str, ...] = ("tests/unit", "tests/integration", "tests/property")

#: Mirrors pyproject [tool.pytest.ini_options] python_files exactly.
TEST_FILE_GLOB = "test_*.py"

#: Committed measured-duration hints (repo-root-relative). Absent = fine
#: (all-proxy weights); present-but-invalid = loud refusal.
DEFAULT_WEIGHTS_REL = "scripts/ci/shard_weights.json"

#: The size->seconds proxy used when the hints file is absent OR a file has
#: no hint entry. Fitted on run 29644876109's <5s file population (1051 s
#: over 15,995,849 bytes); the committed hints file may override it.
FALLBACK_SECONDS_PER_BYTE = 6.6e-05

#: Every file weight is floored here so weights are strictly positive —
#: the property that guarantees LPT never produces an empty shard while
#: shard_count <= file count (empty shard => pytest exit 5 in CI).
MIN_WEIGHT_S = 1e-3


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
        raise SystemExit(
            "shard_tests: zero test files collected — refusing to emit an empty universe"
        )
    return ordered


def load_weights(path: Path) -> tuple[dict[str, float], float]:
    """Load the measured-duration hints file.

    Returns ``(hints, seconds_per_byte)``. An ABSENT file is harmless
    (empty hints + the fallback proxy coefficient). A file that exists
    but fails to parse — or violates the schema (weights_s not a
    str->finite-non-negative-number map; default_seconds_per_byte not a
    finite positive number) — FAILS LOUD: a corrupt hints file must
    never silently degrade the partition (fail-closed).
    """
    if not path.exists():
        return {}, FALLBACK_SECONDS_PER_BYTE
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise SystemExit(
            f"shard_tests: weights file unreadable/unparseable: {path}: {exc!r}"
        ) from exc
    if not isinstance(doc, dict):
        raise SystemExit(f"shard_tests: weights file root must be an object: {path}")
    spb = doc.get("default_seconds_per_byte", FALLBACK_SECONDS_PER_BYTE)
    if (
        isinstance(spb, bool)
        or not isinstance(spb, (int, float))
        or not math.isfinite(spb)
        or spb <= 0
    ):
        raise SystemExit(
            f"shard_tests: weights file default_seconds_per_byte must be a finite "
            f"positive number, got {spb!r}: {path}"
        )
    raw = doc.get("weights_s", {})
    if not isinstance(raw, dict):
        raise SystemExit(f"shard_tests: weights file weights_s must be an object: {path}")
    hints: dict[str, float] = {}
    for key, value in raw.items():
        if not isinstance(key, str):  # pragma: no cover - json object keys are str
            raise SystemExit(f"shard_tests: weights_s key must be a string, got {key!r}: {path}")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise SystemExit(
                f"shard_tests: weights_s[{key!r}] must be a finite non-negative "
                f"number, got {value!r}: {path}"
            )
        hints[key] = float(value)
    return hints, float(spb)


def file_weight(path: Path, root: Path, hints: dict[str, float], seconds_per_byte: float) -> float:
    """Estimated runtime seconds for one test file (strictly positive)."""
    rel = path.relative_to(root).as_posix()
    hinted = hints.get(rel)
    weight = hinted if hinted is not None else path.stat().st_size * seconds_per_byte
    return max(weight, MIN_WEIGHT_S)


def partition(
    files: list[Path],
    shard_count: int,
    *,
    root: Path,
    hints: dict[str, float],
    seconds_per_byte: float,
) -> list[list[Path]]:
    """Greedy LPT bin-packing: heaviest-first into the lightest bin."""
    if shard_count < 1:
        raise SystemExit(f"shard_tests: --shard-count must be >= 1, got {shard_count}")
    if shard_count > len(files):
        raise SystemExit(
            f"shard_tests: --shard-count {shard_count} exceeds the {len(files)} "
            "collected test files — an empty shard would run pytest with no "
            "targets (exit 5); lower the shard count"
        )
    weights = {p: file_weight(p, root, hints, seconds_per_byte) for p in files}
    keyed = sorted(files, key=lambda p: (-weights[p], p.relative_to(root).as_posix()))
    shards: list[list[Path]] = [[] for _ in range(shard_count)]
    totals = [0.0] * shard_count
    for path in keyed:
        target = min(range(shard_count), key=lambda k: (totals[k], k))
        shards[target].append(path)
        totals[target] += weights[path]
    return [sorted(s) for s in shards]


def shard_weight_totals(
    shards: list[list[Path]],
    *,
    root: Path,
    hints: dict[str, float],
    seconds_per_byte: float,
) -> list[float]:
    """Projected total weight (seconds) per shard, for the self-check report."""
    return [sum(file_weight(p, root, hints, seconds_per_byte) for p in shard) for shard in shards]


def self_check(files: list[Path], shards: list[list[Path]]) -> None:
    """Loudly verify the partition is total + disjoint (zero skip, zero dup)."""
    seen: set[Path] = set()
    for idx, shard in enumerate(shards):
        overlap = seen.intersection(shard)
        if overlap:
            raise SystemExit(
                f"shard_tests: SELF-CHECK FAILED — shard {idx} duplicates {sorted(overlap)[:3]}"
            )
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
    parser.add_argument(
        "--weights",
        type=Path,
        default=None,
        help=(
            "measured-duration hints file override (tests only; default: "
            f"<root>/{DEFAULT_WEIGHTS_REL} — absent is fine, corrupt fails loud)"
        ),
    )
    args = parser.parse_args(argv)

    root = (args.root or repo_root()).resolve()
    files = collect_test_files(root, tuple(args.dirs))
    weights_path = args.weights if args.weights is not None else root / DEFAULT_WEIGHTS_REL
    hints, seconds_per_byte = load_weights(weights_path)
    shards = partition(
        files, args.shard_count, root=root, hints=hints, seconds_per_byte=seconds_per_byte
    )
    self_check(files, shards)

    if args.self_check:
        sizes = [len(s) for s in shards]
        totals = shard_weight_totals(
            shards, root=root, hints=hints, seconds_per_byte=seconds_per_byte
        )
        print(
            f"shard_tests self-check OK: {len(files)} test files -> "
            f"{args.shard_count} shards {sizes} (total {sum(sizes)}, disjoint, complete)"
        )
        print(
            "shard_tests projected weights (s): "
            f"[{', '.join(f'{t:.1f}' for t in totals)}] "
            f"(hints: {len(hints)} entries from {weights_path.name}"
            f"{'' if weights_path.exists() else ' — ABSENT, size-proxy only'})"
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
