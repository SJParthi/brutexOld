#!/usr/bin/env python3
"""bench_baseline_gate.py — the absolute-benchmark baseline CI gate CLI.

DEC-BENCH-ABS-BASELINE-001 (MIGRATION.md #1527). A THIN stdlib-only wrapper
over the ONE core :mod:`brutex.observability.bench_baseline` (itself pure
stdlib), loaded standalone via ``importlib.util.spec_from_file_location``
(the ``scripts/ci/_textscan.py`` sibling-contract idiom) so the shared-CI
``bench`` job needs ZERO pip installs to run the ``validate`` + ``govern``
steps. All logic lives in the core; this script does I/O + subprocess only.

Subcommands / exit codes
------------------------
``key``       print the runtime hardware key                 (0 / 2)
``validate``  parse every committed baseline file            (0 / 2)
``compare``   compare measured fragments vs the blessed
              baseline for the runtime key                   (0 ok-or-loud-
              abstain / 1 NUMERIC regression or suspicious
              speedup — retry-eligible / 2 STRUCTURAL,
              schema, or ledger-belt breach — NEVER retried
              / 3 usage)
``capture``   fold >= 3 process-fresh runs into a candidate  (0 / 1 noise or
              overwrite refusal / 3 usage)
``govern``    the no-silent-re-baseline diff gate            (0 / 1 breach /
              2 setup)

Deliberately NOT pytest-benchmark's ``--benchmark-compare``: native compare
warns-and-passes with no matching saved run, keys on drift-prone
machine_info, and cannot express per-entry floors/abstain/governance —
ledgered in DEC-BENCH-ABS-BASELINE-001; precedent: MIGRATION.md entry #158
(DEC-P0-020), the Agent-2 finding inside that entry.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import types
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CORE_PATH = ROOT / "brutex" / "observability" / "bench_baseline.py"
MIGRATION_FILE = ROOT / "docs" / "MIGRATION.md"
BASELINE_PREFIX = "tests/bench/baselines/"

#: Live run-verification retry policy (fail-closed after the last attempt).
GH_API_ATTEMPTS = 3
GH_API_BACKOFF_S = 5.0


def load_core() -> types.ModuleType:
    """Load the bench-baseline core standalone (no installed brutex needed)."""
    spec = importlib.util.spec_from_file_location("bench_baseline_core", CORE_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover — importlib contract
        raise RuntimeError(f"cannot load {CORE_PATH}")
    module = importlib.util.module_from_spec(spec)
    # Register BEFORE exec: dataclasses resolves cls.__module__ via
    # sys.modules at class-creation time (slots=True path).
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_github_output(path: str | None, outputs: Mapping[str, str]) -> None:
    """Append key=value lines to a ``$GITHUB_OUTPUT`` file when given."""
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for key, value in outputs.items():
            fh.write(f"{key}={value}\n")


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """One captured subprocess run (fixed argv, never a shell)."""
    return subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603


def _git_changed_baselines(
    base: str, run: Callable[[list[str]], subprocess.CompletedProcess[str]]
) -> list[tuple[str, str]]:
    """``(status, name)`` per changed baseline file — renames expanded to D+A."""
    proc = run(
        [
            "git",
            "diff",
            "--name-status",
            f"{base}...HEAD",
            "--",
            BASELINE_PREFIX.rstrip("/"),
        ]
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git diff failed: {proc.stderr.strip()}")
    changed: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if not parts[0]:
            continue
        status = parts[0]
        if status[:1] in ("R", "C") and len(parts) >= 3:
            old = parts[1].removeprefix(BASELINE_PREFIX)
            new = parts[2].removeprefix(BASELINE_PREFIX)
            if status[:1] == "R":
                changed.append(("D", old))
            changed.append(("A", new))
            continue
        changed.append((status, parts[1].removeprefix(BASELINE_PREFIX)))
    return changed


def _git_show(
    base: str, name: str, run: Callable[[list[str]], subprocess.CompletedProcess[str]]
) -> str | None:
    """The base-ref blob text for one baseline file (``None`` = absent)."""
    proc = run(["git", "show", f"{base}:{BASELINE_PREFIX}{name}"])
    return proc.stdout if proc.returncode == 0 else None


def _git_migration_added_lines(
    base: str, run: Callable[[list[str]], subprocess.CompletedProcess[str]]
) -> str:
    """The ``+`` lines of this PR's docs/MIGRATION.md diff (headers excluded)."""
    proc = run(["git", "diff", f"{base}...HEAD", "--", "docs/MIGRATION.md"])
    if proc.returncode != 0:
        raise RuntimeError(f"git diff MIGRATION.md failed: {proc.stderr.strip()}")
    return "\n".join(
        line[1:]
        for line in proc.stdout.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


def make_gh_run_verifier(
    run: Callable[[list[str]], subprocess.CompletedProcess[str]],
    *,
    repo_slug: str,
    sleeper: Callable[[float], None] = time.sleep,
) -> Callable[[str], Mapping[str, object] | None]:
    """A live GitHub-run verifier over ``gh api`` (memoised; fail-closed).

    The API is queried ONLY for *repo_slug* (THIS repository): a run_url on
    any other repo returns ``None`` (a breach) without ever hitting the API
    — belt to the core's own repo pin in ``_verify_cell_run``, so a forged
    foreign-repo provenance can never reach a confirming endpoint. Returns
    ``None`` for a 404 (run does not exist — a breach), raises
    ``RuntimeError`` after :data:`GH_API_ATTEMPTS` transport failures (API
    outage → the gate fails CLOSED with a named remediation, never open).
    """
    cache: dict[str, Mapping[str, object] | None] = {}

    def verify(run_url: str) -> Mapping[str, object] | None:
        if run_url in cache:
            return cache[run_url]
        match = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/actions/runs/(\d+)", run_url)
        if match is None or match.group(1) != repo_slug:
            cache[run_url] = None
            return None
        run_id = match.group(2)
        last_error = ""
        for attempt in range(1, GH_API_ATTEMPTS + 1):
            proc = run(
                [
                    "gh",
                    "api",
                    f"repos/{repo_slug}/actions/runs/{run_id}",
                    "--jq",
                    "{path: .path, status: .status, conclusion: .conclusion, head_sha: .head_sha}",
                ]
            )
            if proc.returncode == 0:
                try:
                    data = json.loads(proc.stdout)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"gh api returned non-JSON for {run_url}: {exc}") from exc
                result: Mapping[str, object] | None = dict(data) if isinstance(data, dict) else None
                cache[run_url] = result
                return result
            last_error = proc.stderr.strip()
            if "404" in last_error or "Not Found" in last_error:
                cache[run_url] = None
                return None
            if attempt < GH_API_ATTEMPTS:
                sleeper(GH_API_BACKOFF_S)
        raise RuntimeError(
            f"gh api failed {GH_API_ATTEMPTS}x for {run_url} ({last_error}) — the "
            "governance gate fails CLOSED on an API outage; re-run the job once "
            "GitHub API access recovers"
        )

    return verify


def cmd_key(core: types.ModuleType) -> int:
    """Print the runtime hardware key (requires an installed brutex)."""
    try:
        print(core.runtime_hardware_key())
    except (ValueError, RuntimeError) as exc:
        print(f"::error::bench-abs key: {exc}", file=sys.stderr)
        return 2
    return 0


def _read_dir(baselines_dir: Path) -> dict[str, str]:
    """Every file name → text under the baselines dir (empty when absent)."""
    if not baselines_dir.is_dir():
        return {}
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(baselines_dir.iterdir())
        if path.is_file()
    }


def cmd_validate(core: types.ModuleType, baselines_dir: Path) -> int:
    """The always-on shared-CI structural step (S8 intent)."""
    files = _read_dir(baselines_dir)
    errors, warnings = core.validate_dir(files)
    for warning in warnings:
        print(f"::warning::bench-abs validate: {warning}")
    for error in errors:
        print(f"::error::bench-abs validate: {error}")
    json_count = sum(1 for name in files if name.endswith(".json"))
    if json_count == 0 and not errors:
        print(
            "::notice::bench-abs validate: 0 baseline files — m4pro compare will "
            "ABSTAIN loudly until the first bless (DEC-BENCH-ABS-BASELINE-001 bootstrap)"
        )
    if errors:
        return 2
    print(f"bench-abs validate: {json_count} baseline file(s) structurally sound")
    return 0


def _load_fragments(core: types.ModuleType, fragments_dir: Path) -> dict[str, Any]:
    """Parse every ``<entry>.json`` fragment in the measurement output dir.

    Values are the dynamically-loaded core's ``Fragment`` instances —
    ``Any`` because the core module is loaded standalone (untyped surface).
    """
    fragments: dict[str, Any] = {}
    for path in sorted(fragments_dir.glob("*.json")):
        if path.name == "fingerprint.json":
            continue
        entry = path.stem
        fragments[entry] = core.parse_fragment(path.read_text(encoding="utf-8"), entry_hint=entry)
    return fragments


def _load_fingerprint_doc(fragments_dir: Path) -> dict[str, object]:
    """The measurement run's ``fingerprint.json`` (runtime key + context)."""
    path = fragments_dir / "fingerprint.json"
    if not path.is_file():
        raise RuntimeError(f"missing {path} — the measurement step did not run")
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or "hardware_key" not in doc:
        raise RuntimeError(f"{path} malformed — no hardware_key")
    return dict(doc)


def _ledger_belt_breaches(core: types.ModuleType, baseline: object) -> list[str]:
    """The daily belt: every blessed decision id must be a MIGRATION.md token."""
    if not MIGRATION_FILE.is_file():  # pragma: no cover — repo invariant
        return [f"{MIGRATION_FILE} missing — cannot verify decision ids"]
    ledger_text = MIGRATION_FILE.read_text(encoding="utf-8")
    breaches = []
    for entry_id, rec in sorted(baseline.entries.items()):  # type: ignore[attr-defined]
        if rec.decision_id not in ledger_text:
            breaches.append(
                f"{entry_id}: blessed decision id {rec.decision_id} is NOT a token in "
                "docs/MIGRATION.md — a smuggled re-baseline (direct-push belt)"
            )
    return breaches


def cmd_compare(
    core: types.ModuleType,
    fragments_dir: Path,
    baselines_dir: Path,
    github_output: str | None,
) -> int:
    """Measure-vs-blessed compare for the runtime hardware key."""
    try:
        fingerprint_doc = _load_fingerprint_doc(fragments_dir)
        fragments = _load_fragments(core, fragments_dir)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"::error::bench-abs compare: {exc}", file=sys.stderr)
        return 2
    runtime_key = str(fingerprint_doc["hardware_key"])
    runtime_python = str(fingerprint_doc.get("python", ""))
    baseline_path = baselines_dir / f"{runtime_key}.json"
    baseline = None
    if baseline_path.is_file():
        try:
            baseline = core.parse_baseline(
                baseline_path.read_text(encoding="utf-8"), filename=baseline_path.name
            )
        except ValueError as exc:
            print(f"::error::bench-abs compare: {exc}", file=sys.stderr)
            _write_github_output(
                github_output,
                {
                    "hardware_key": runtime_key,
                    "baseline_missing": "0",
                    "abstain_entries": "",
                    "needs_capture": "0",
                },
            )
            return 2
        belt = _ledger_belt_breaches(core, baseline)
        if belt:
            for breach in belt:
                print(f"::error::bench-abs compare belt: {breach}", file=sys.stderr)
            _write_github_output(
                github_output,
                {
                    "hardware_key": runtime_key,
                    "baseline_missing": "0",
                    "abstain_entries": "",
                    "needs_capture": "0",
                },
            )
            return 2
    report = core.compare(
        baseline, fragments, runtime_key=runtime_key, runtime_python=runtime_python
    )
    print(core.render_report(report), end="")
    needs_capture = bool(report.baseline_missing or report.abstained_entries)
    _write_github_output(
        github_output,
        {
            "hardware_key": runtime_key,
            "baseline_missing": "1" if report.baseline_missing else "0",
            "abstain_entries": ",".join(report.abstained_entries),
            "needs_capture": "1" if (needs_capture and report.ok) else "0",
        },
    )
    if report.has_structural:
        return 2
    if report.has_numeric_failure:
        return 1
    return 0


def cmd_capture(core: types.ModuleType, args: argparse.Namespace) -> int:
    """Fold the measurement runs into a candidate baseline + bless PR body."""
    runs = []
    for run_dir in args.runs:
        run_path = Path(run_dir)
        try:
            runs.append(_load_fragments(core, run_path))
        except (ValueError, OSError) as exc:
            print(f"::error::bench-abs capture: {run_dir}: {exc}", file=sys.stderr)
            return 3
    try:
        fingerprint_doc = _load_fingerprint_doc(Path(args.runs[0]))
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"::error::bench-abs capture: {exc}", file=sys.stderr)
        return 3
    runtime_key = str(fingerprint_doc["hardware_key"])
    baselines_dir = Path(args.baselines_dir)
    existing = None
    existing_path = baselines_dir / f"{runtime_key}.json"
    if existing_path.is_file():
        existing = core.parse_baseline(
            existing_path.read_text(encoding="utf-8"), filename=existing_path.name
        )
    replace = frozenset(
        entry.strip() for entry in (args.replace_entries or "").split(",") if entry.strip()
    )
    first_run = runs[0]
    context: dict[str, str] = {}
    for fragment in first_run.values():
        context = dict(fragment.context)
        break
    provenance = core.Provenance(
        run_url=args.run_url,
        commit=args.commit,
        captured_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        runner_name=args.runner_name,
    )
    raw_fp = fingerprint_doc.get("fingerprint")
    fingerprint: Mapping[str, object] = raw_fp if isinstance(raw_fp, dict) else {}
    try:
        candidate = core.fold_capture(
            runs,
            existing=existing,
            decision_id=args.decision_id,
            replace_entries=replace,
            fingerprint=fingerprint,
            context=context,
            provenance=provenance,
        )
    except ValueError as exc:
        print(f"::error::bench-abs capture REFUSED: {exc}", file=sys.stderr)
        print(
            "remediation: a NoiseRefusal means quiet the machine and re-dispatch; an "
            "OverwriteRefusal means pass --replace-entries with a NEW --decision-id "
            "(see docs/runbooks/bench-baseline.md)",
            file=sys.stderr,
        )
        return 1
    added = sorted(
        entry for entry in candidate.entries if existing is None or entry not in existing.entries
    )
    carried = sorted(
        entry
        for entry in candidate.entries
        if existing is not None and entry in existing.entries and entry not in replace
    )
    Path(args.candidate_out).write_text(core.dump_baseline(candidate), encoding="utf-8")
    Path(args.pr_body_out).write_text(
        core.render_pr_body(
            candidate,
            added=added,
            replaced=sorted(replace),
            run_url=args.run_url,
            carried=carried,
        ),
        encoding="utf-8",
    )
    print(
        f"bench-abs capture: candidate for {runtime_key} written to {args.candidate_out} "
        f"(added={added or 'none'}, replaced={sorted(replace) or 'none'}, "
        f"carried unchanged={carried or 'none'})"
    )
    return 0


def _git_tree_root(run: Callable[[list[str]], subprocess.CompletedProcess[str]]) -> Path:
    """The CURRENT git work tree root (honors cwd — scratch-repo testable)."""
    proc = run(["git", "rev-parse", "--show-toplevel"])
    if proc.returncode != 0:
        raise RuntimeError(f"git rev-parse failed: {proc.stderr.strip()}")
    return Path(proc.stdout.strip())


def cmd_govern(core: types.ModuleType, base: str, repo_slug: str) -> int:
    """The no-silent-re-baseline PR gate (pure core; git/gh injected here).

    *repo_slug* is ``$GITHUB_REPOSITORY`` — when baseline files changed and
    the slug is absent the gate fails CLOSED (exit 2): without the repo pin
    a fork's completed ``bench-m4pro.yml`` run would satisfy the live
    verification, defeating the anti-fabrication leg.
    """
    try:
        tree_root = _git_tree_root(_run)
        changed = _git_changed_baselines(base, _run)
    except RuntimeError as exc:
        print(f"::error::bench-abs govern setup: {exc}", file=sys.stderr)
        return 2
    if not changed:
        print("bench-abs govern: no baseline changes — nothing to govern")
        return 0
    if not repo_slug:
        print(
            "::error::bench-abs govern setup: $GITHUB_REPOSITORY is not set — the "
            "governance gate cannot pin provenance runs to THIS repository and "
            "fails CLOSED (set GITHUB_REPOSITORY=<owner>/<repo> to run locally)",
            file=sys.stderr,
        )
        return 2
    try:
        migration_added = _git_migration_added_lines(base, _run)
        report = core.govern(
            changed=changed,
            base_reader=lambda name: _git_show(base, name, _run),
            head_reader=lambda name: (
                (tree_root / BASELINE_PREFIX / name).read_text(encoding="utf-8")
                if (tree_root / BASELINE_PREFIX / name).is_file()
                else None
            ),
            migration_added_lines=migration_added,
            run_verifier=make_gh_run_verifier(_run, repo_slug=repo_slug),
            repo_slug=repo_slug,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"::error::bench-abs govern setup: {exc}", file=sys.stderr)
        return 2
    for note in report.notes:
        print(f"bench-abs govern: {note}")
    for breach in report.breaches:
        print(f"::error::bench-abs govern BREACH: {breach}", file=sys.stderr)
    if not report.ok:
        print(
            "::error::bench-abs govern: a baseline change needs a NEW human-written "
            "docs/MIGRATION.md entry (quoting the entry key + literal value token) "
            "plus a live-verified fresh bench-m4pro run — see "
            "docs/runbooks/bench-baseline.md",
            file=sys.stderr,
        )
        return 1
    print("bench-abs govern: every baseline change is ledgered + live-verified")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """The subcommand CLI."""
    parser = argparse.ArgumentParser(prog="bench_baseline_gate", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("key", help="print the runtime hardware key")
    p_validate = sub.add_parser("validate", help="validate committed baseline files")
    p_validate.add_argument("--baselines-dir", required=True)
    p_compare = sub.add_parser("compare", help="compare fragments vs the blessed baseline")
    p_compare.add_argument("--fragments-dir", required=True)
    p_compare.add_argument("--baselines-dir", required=True)
    p_compare.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    p_capture = sub.add_parser("capture", help="fold measurement runs into a candidate")
    p_capture.add_argument("--runs", nargs="+", required=True)
    p_capture.add_argument("--baselines-dir", required=True)
    p_capture.add_argument("--candidate-out", required=True)
    p_capture.add_argument("--pr-body-out", required=True)
    p_capture.add_argument("--decision-id", required=True)
    p_capture.add_argument("--commit", required=True)
    p_capture.add_argument("--run-url", required=True)
    p_capture.add_argument("--runner-name", required=True)
    p_capture.add_argument("--replace-entries", default="")
    p_govern = sub.add_parser("govern", help="no-silent-re-baseline diff gate")
    p_govern.add_argument("--base", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point (exit-code contract in the module docstring)."""
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return 3 if exc.code not in (0, None) else 0
    core = load_core()
    if args.command == "key":
        return cmd_key(core)
    if args.command == "validate":
        return cmd_validate(core, Path(args.baselines_dir))
    if args.command == "compare":
        return cmd_compare(
            core, Path(args.fragments_dir), Path(args.baselines_dir), args.github_output
        )
    if args.command == "capture":
        return cmd_capture(core, args)
    return cmd_govern(core, args.base, os.environ.get("GITHUB_REPOSITORY", ""))


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess tests
    sys.exit(main())
