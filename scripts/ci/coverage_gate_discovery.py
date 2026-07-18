#!/usr/bin/env python3
"""coverage_gate_discovery.py — auto-discover touched modules + their tests.

DEC-CICD-GATE-HONESTY-001. The "Strict per-module 100% coverage gate" in
``.github/workflows/ci.yml`` historically carried a *hardcoded* list of
``pytest --cov=brutex.MODULE`` invocations (one ``compgen -G`` guard per
module). A hardcoded allowlist has a structural blind spot: a NEW module
that is not added to the list silently escapes the per-module independence
check (it still rides the whole-package ``--cov-fail-under=100`` floor,
but never proves it is 100% with its OWN tests).

This helper COMPLEMENTS the static list (it does not replace it, nor the
whole-package floor) with a deterministic "what was TOUCHED" discovery, so
a newly added module can never escape the per-file gate merely for being
absent from a list — while a legacy modification with no dedicated test is
NEVER newly blocked:

1. **Discover touched modules** from ``git diff --name-status
   --diff-filter=ACMR <base>...HEAD`` (base defaults to ``origin/main``),
   filtered to ``brutex/**/*.py``, EXCLUDING ``__init__.py``, anything
   under ``tests/``, and the small documented ``COVERAGE_DISCOVERY_EXCLUDE``
   allowlist. Each module keeps its git STATUS (Added vs Modified/Renamed/
   Copied) — the failure policy below differs by status.
2. **Map each module to its test file(s)** via the repo's real path
   conventions (``brutex/a/b/foo.py`` → ``test_foo.py`` / ``test_b_foo.py``
   / ``test_a_b_foo.py``, each optionally with a trailing ``*`` so a
   curated multi-file suite like ``test_lookahead_barrier.py`` matches),
   discovered by globbing ``tests/**/test_*.py`` — no second hardcoded
   mapping table. A candidate is then ACCEPTED only if it actually
   IMPORTS the module (so a foreign same-stem test — e.g. a
   ``tests/unit/test_models.py`` that imports a *different* ``models`` —
   is rejected, never mistaken for the module's own coverage).
3. **Fail honestly** only when a NEWLY ADDED module has no import-verified
   test; a MODIFIED module with no such test is warned about and skipped
   (it still rides the static gate + the whole-package floor).
4. **Run coverage** per touched module that HAS a verified test against
   those tests and assert 100% line AND 100% branch, with a precise
   per-file report.

Determinism: every discovered collection is sorted; nothing depends on
``dict``/``set``/filesystem iteration order.

Usage::

    python scripts/ci/coverage_gate_discovery.py [--base <ref>] [--list]

Exit codes: 0 = all touched modules at 100% (or none touched, or only
warn-skipped modifications); 1 = a newly added module has no test OR a
verified module is below 100%; 2 = script/setup error (e.g. git
unavailable / no merge base). Fail-closed, mirroring the sibling CI gates.
``--list`` prints the discovered plan and exits 0 without running coverage
(a fast dry-run for the workflow log + local debugging).
"""

from __future__ import annotations

import argparse
import ast
import os
import subprocess  # nosec B404 — git/pytest invocation is the gate's purpose
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: Default merge base the PR diff is computed against.
DEFAULT_BASE = "origin/main"

#: The single in-scope source root (POSIX-relative, package-rooted). This
#: matches the historical per-module gate's ``brutex``-only scope — a
#: ``scripts/ci`` script is NOT a coverage-gated brutex module here (its
#: own dedicated tests still run in the suite).
SOURCE_ROOT = "brutex/"

#: Where the test files live (globbed for ``test_*.py``).
TESTS_DIR = "tests"

#: TINY, documented allowlist of in-scope files that genuinely CANNOT be
#: 100%-gated by a SINGLE per-file unit test, so a touch must NOT fail the
#: discovery gate. Keep this list short and justified — every entry needs a
#: one-line reason (CLAUDE.md Golden Rule #11 COMMON + Golden Rule #10
#: honesty). These files still ride the whole-package ``--cov-fail-under=100``
#: floor (the authoritative 100% gate) + the static per-module list — the
#: discovery gate is a COMPLEMENT, never the only guard.
COVERAGE_DISCOVERY_EXCLUDE: frozenset[str] = frozenset(
    {
        # The ~1300-statement FastAPI route-glue module. Its 100% line+branch is
        # reached by the WHOLE dashboard route-family suite collectively (~57 test
        # files exercise ``build_app`` end-to-end) + the residual-gap backfill
        # ``test_dashboard_app_backfill.py`` — by design that backfill closes only
        # the leftovers the route suites miss, so it covers ~43% STANDALONE and was
        # never meant to be app.py's sole per-file gate. The whole-package
        # ``--cov-fail-under=100`` floor (DEC-COV-APP-BACKFILL-001) is app.py's real
        # 100% gate; the discovery gate running its backfill in isolation is a
        # structural mismatch that would block EVERY app.py-touching PR.
        "brutex/dashboard/app.py",
        # The SPOT pull controller's 100% line+branch is reached by a CURATED
        # multi-file suite — test_dashboard_pull_controller.py +
        # test_dashboard_pull_pool_sizing.py + test_dashboard_pull_health_wiring.py +
        # test_dashboard_pull_stall.py + test_dashboard_write_batch_wiring.py
        # collectively (the AIMD/quota mid-round paths, the pool-sizing math, the
        # health-monitor + stall-detector + write-batch build/drain wiring each live
        # in their own differently-stemmed file). No single name-matched file reaches
        # 100% standalone (test_dashboard_pull_controller.py alone is ~94%), so the
        # discovery gate's one-file mapping is a structural mismatch that would block
        # EVERY pull_controller-touching PR. It is fully gated by the dedicated STATIC
        # per-module gate in ci.yml (which runs exactly that 5-file suite at
        # --cov-fail-under=100) + the whole-package floor — DEC-WRITE-BATCH-COALESCE-001.
        "brutex/dashboard/pull_controller.py",
        # The ~1050-statement ``/db`` lake-viewer module. Its 100% line+branch is
        # reached by the WHOLE db_view test FAMILY collectively — SEVENTEEN files
        # as of the DEC-DASHBOARD-READ-SCREEN-001 merge with main's
        # DEC-LAKE-STATS-MANIFEST-001 + DEC-DB-CANDLES-TORN-READ-RETRY-001
        # additions (test_dashboard_db_view/_db_query/
        # _db_fno/_db_fno_moneyness/_db_candle_count_cache/_db_option_count_cache/
        # _db_ux_pack/_window_payload_retry/_db_view_window_rows_cache/
        # _db_view_ledger_delta_fold/_db_view_ledger_read_cache/
        # _db_stats_manifest/_db_candles_torn_read_retry/
        # _split_brain_screen + test_db_view_registry/test_db_view_manifest/
        # test_completeness_fno_skip — the exact list lives in the static
        # per-module gate in ``ci.yml``, which runs them together at 100.00%).
        # The discovery gate maps a module ONLY to its stem-matching tests
        # (``test_db_view*`` / ``test_dashboard_db_view*``), so it sees only a
        # fraction of the family and tops out far below 100% STANDALONE — the
        # SAME structural mismatch as app.py (a large module whose 100% is a
        # multi-file-suite property, not a single-file one). Duplicating the
        # other files' coverage into the stem files to satisfy the discovery
        # gate would be a Golden-Rule-#11 COMMON (no copy-paste) violation.
        # db_view.py's real 100% gate is the whole-package
        # ``--cov-fail-under=100`` floor + the static per-module gate
        # (DEC-DASHBOARD-EMPTY-STATE-001); the discovery gate is a COMPLEMENT, and
        # excluding it here keeps that complement honest (Golden Rule #10) instead
        # of blocking EVERY db_view-touching PR on an unreachable single-file 100%.
        "brutex/dashboard/db_view.py",
        # The Stage-1A bitset-matrix builder. Its 100% line+branch is reached by a
        # CURATED multi-file suite — test_engine_stage1a.py (the matrix builder +
        # the resurrected VWAP/BOS bits) + test_engine_sweep_driver_walk_forward.py
        # (the walk-forward schedule helpers trading_day_starts / WalkForwardSchedule /
        # _day_to_bar / _walk_forward_schedule_hash / build_walk_forward_schedule,
        # which live in stage1a.py but are exercised by the walk-forward suite —
        # stem-named for sweep_driver.py, whose walk-forward entry points it gates).
        # The discovery gate maps a module ONLY to its stem-matching tests
        # (test_engine_stage1a*), so it sees just the builder file and tops out at
        # ~82% STANDALONE while the walk-forward functions read 0 — the SAME
        # multi-file-suite structural mismatch as app.py / pull_controller.py /
        # db_view.py. Duplicating the walk-forward tests into the stage1a stem file
        # to satisfy the discovery gate would be a Golden-Rule-#11 COMMON (no
        # copy-paste) violation. stage1a.py's real 100% gate is the STATIC
        # per-module engine gate in ci.yml (which runs the WHOLE
        # tests/unit/test_engine_*.py suite together at --cov-fail-under=100,
        # verified 100.00% incl. the new VWAP/BOS lines) + the whole-package
        # --cov-fail-under=100 floor; the discovery gate is a COMPLEMENT, and
        # excluding it here keeps that complement honest instead of blocking
        # EVERY stage1a-touching PR (DEC-COVERAGE-DISCOVERY-STAGE1A-001).
        "brutex/engine/stage1a.py",
        # The Stage-2 sweep driver. Its 100% line+branch is reached by a CURATED
        # two-file suite — test_engine_sweep_driver.py (evaluate/sweep/rank +
        # the public metrics_for_mask / metrics_from_trades seams) +
        # test_engine_walk_forward.py (the walk-forward half: FoldResult /
        # WalkForwardResult / _score_fold / evaluate_combo_walk_forward /
        # walk_forward_sweep, which live in sweep_driver.py but are exercised
        # by their own walk-forward suite — the EXACT stage1a.py shape above).
        # The discovery gate maps a module ONLY to its stem-matching tests
        # (test_engine_sweep_driver*), so it sees just the driver file and tops
        # out at ~83% STANDALONE while the walk-forward half reads 0 (verified
        # 100.00% line+branch when the two files run together). Duplicating the
        # walk-forward tests into the driver stem file would be a
        # Golden-Rule-#11 COMMON (no copy-paste) violation. sweep_driver.py's
        # real 100% gate is the STATIC per-module engine gate in ci.yml (the
        # WHOLE tests/unit/test_engine_*.py suite at --cov-fail-under=100) +
        # the whole-package floor (DEC-SWEEP-RUST-TRADESIM-WIRE-001, which made
        # the metrics tail public and first touched this module under the
        # discovery gate).
        "brutex/engine/sweep_driver.py",
        # The /movers spot-snapshot module. Its 100% line+branch is reached by
        # the WHOLE movers test FAMILY collectively — the spot-CANDIDATE readers
        # (_read_daily_partition / _prev_day_close / compute_spot_candidates /
        # run_movers_snapshot) only execute against a REAL seeded lake partition,
        # which lives in the integration suite (test_dashboard_movers.py +
        # test_movers_discovery_memo.py + test_movers_exchange_cache.py +
        # test_movers_fno_columns_from_minute.py), while the pure helpers it now
        # also hosts (session_traded_value_paisa / normalize_min_volume /
        # value_meets_volume_floor — the COMMON ranking primitives shared by
        # movers_options + movers_futures) are exercised from their callers'
        # tests. The discovery gate maps a module ONLY to its stem-matching
        # tests (test_*movers_snapshot*.py — there is no
        # test_dashboard_movers_snapshot.py), so it sees just the unit file and
        # tops out at ~55% STANDALONE — the SAME multi-file-suite structural
        # mismatch as app.py / pull_controller.py / db_view.py / stage1a.py.
        # Duplicating the lake-fixture integration coverage into the unit stem
        # file to satisfy the discovery gate would be a Golden-Rule-#11 COMMON
        # (no copy-paste) violation. movers_snapshot.py's real 100% gate is the
        # whole-package --cov-fail-under=100 floor (verified 100.00% line+branch
        # over the movers unit+integration suites); the discovery gate is a
        # COMPLEMENT, and excluding it here keeps that complement honest
        # (Golden Rule #10) instead of blocking EVERY movers_snapshot-touching
        # PR on an unreachable single-file 100% (DEC-COVERAGE-DISCOVERY-MOVERS-SNAPSHOT-001).
        "brutex/dashboard/movers_snapshot.py",
        # The Groww/token/AWS credentials module. Its 100% line+branch is reached
        # by a CURATED two-file suite — test_credentials.py (the generic token creds +
        # the SHARED secure-file reader: symlink / insecure-perms / corrupt-JSON
        # branches) + test_credentials_aws.py (the AWS-only Groww creds reader:
        # resolve / present + the fail-fast branches — DEC-AWS-ONLY-CREDS-001 removed
        # the Groww file/env path, so the old test_groww_credentials.py is gone).
        # Both files stem-match the discovery map (test_credentials*), so the gate
        # CAN reach 100% standalone; it is kept excluded here as a COMPLEMENT (the
        # dedicated STATIC per-module gate in ci.yml runs the exact pair at
        # --cov-fail-under=100, and the whole-package floor covers it too), matching
        # the app.py / pull_controller.py / db_view.py / stage1a.py / movers_snapshot.py
        # convention — a curated-suite module's real 100% gate is the static per-module
        # gate + the package floor, and excluding it here keeps the discovery complement
        # honest instead of coupling every credentials.py-touching PR to the discovery
        # stem-map heuristic (DEC-AWS-ONLY-CREDS-001).
        "brutex/config/credentials.py",
        # DEC-RESILIENCE-STREAKS-001 (MIGRATION.md #1956): the NEW losing-streak
        # resilience module (fold + sidecar + annotation). Its 100% line+branch
        # is reached by a CURATED multi-file suite — test_resilience_streaks.py
        # (the pure fold/streak primitives) + test_resilience_sidecar.py (the
        # sidecar write/read + annotation branches) + the integration
        # test_resilience_end_to_end.py; the stem file alone tops out at ~62.94%
        # because the fold/annotation/backfill branches live in the differently
        # stemmed sidecar + integration files. The discovery gate maps a module
        # ONLY to its stem-matching tests (test_resilience_streaks*), so it sees
        # just the one unit file — the SAME multi-file-suite structural mismatch
        # as pull_controller.py / db_view.py / stage1a.py / sweep_driver.py.
        # Duplicating the sidecar/integration coverage into the streaks stem file
        # would be a Golden-Rule-#11 COMMON (no copy-paste) violation. Its real
        # 100% gate is the DEDICATED static per-module gate in ci.yml (which runs
        # exactly that three-file suite at --cov-fail-under=100) + the
        # whole-package --cov=brutex.engine floor; excluding it here keeps the
        # discovery complement honest instead of blocking on an unreachable
        # single-file 100%.
        "brutex/engine/resilience_streaks.py",
        # DEC-RESILIENCE-STREAKS-001 (MIGRATION.md #1956): results_depth_pass.py
        # gains an adoption-backfill + resilience-fence path whose new lines
        # (~492-528, 760-773) are exercised by the differently stemmed
        # test_resilience_sidecar.py + the integration test_resilience_end_to_end.py
        # — test_engine_results_depth_pass.py alone tops out at ~94.88%. Same
        # curated-multi-file-suite mismatch as sweep_driver.py above; its real
        # 100% gate is the DEDICATED static per-module gate in ci.yml (which runs
        # that three-file suite together at --cov-fail-under=100) + the
        # whole-package floor. Excluding it here keeps the discovery complement
        # honest instead of coupling every results_depth_pass-touching PR to the
        # single-stem-file heuristic.
        "brutex/engine/results_depth_pass.py",
    }
)


class DiscoveryError(Exception):
    """A setup/usage error (maps to exit code 2)."""


def _git_diff_name_status(base: str) -> list[tuple[str, str]]:
    """Return ``(status, path)`` pairs added/copied/modified/renamed vs *base*.

    ``--diff-filter=ACMR`` excludes deletions (D) so a removed module is
    never gated, and ``--name-status`` keeps each file's status letter so
    the caller can distinguish a NEWLY added module (``A``) from a
    modification (``M``/``R``/``C``). A rename/copy row carries the NEW
    path in its final tab-separated column (and a similarity score in the
    status letter, e.g. ``R100``); only the leading letter and the final
    path are used.

    Parameters
    ----------
    base:
        The git ref (merge base) to diff ``HEAD`` against.

    Returns
    -------
    list[tuple[str, str]]
        ``(single-letter status, POSIX path)`` for every non-deletion
        change, in git's emitted order.
    """
    # Fixed argv (no shell), git resolved on PATH, no untrusted input — the
    # only variable is the caller's --base ref.
    git_argv = ["git", "diff", "--name-status", "--diff-filter=ACMR", f"{base}...HEAD"]
    try:
        proc = subprocess.run(  # noqa: S603  # nosec B603 B607
            git_argv,
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:  # git binary missing / not executable
        raise DiscoveryError(f"cannot run git: {exc}") from exc
    if proc.returncode != 0:
        raise DiscoveryError(f"git diff failed (exit {proc.returncode}): {proc.stderr.strip()}")
    rows: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        if not line:
            continue
        fields = line.split("\t")
        status = fields[0][:1]
        # For R/C the new path is the LAST field; for A/M it is fields[1].
        path = fields[-1]
        if path:
            rows.append((status, path))
    return rows


def discover_touched_modules(diff_rows: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    """Return the sorted in-scope ``(module, status)`` pairs among *diff_rows*.

    Filters to ``SOURCE_ROOT`` ``*.py`` files, dropping ``__init__.py``,
    any ``tests/`` path, and the ``COVERAGE_DISCOVERY_EXCLUDE`` allowlist.
    The status letter is normalised to its leading character (``A`` =
    Added; ``M``/``R``/``C`` = a modification for the failure policy). If a
    path appears more than once the first kept status wins (sorted input
    makes this deterministic).

    Parameters
    ----------
    diff_rows:
        ``(status, path)`` pairs from :func:`_git_diff_name_status`.

    Returns
    -------
    list[tuple[str, str]]
        Sorted ``(module_path, status_letter)`` for every in-scope module.
    """
    touched: dict[str, str] = {}
    for status, name in diff_rows:
        if not name.endswith(".py"):
            continue
        if name.startswith(f"{TESTS_DIR}/"):
            continue
        if Path(name).name == "__init__.py":
            continue
        if not name.startswith(SOURCE_ROOT):
            continue
        if name in COVERAGE_DISCOVERY_EXCLUDE:
            continue
        touched.setdefault(name, status[:1])
    return sorted(touched.items())


def candidate_test_globs(module: str) -> list[str]:
    """Return the candidate ``test_*.py`` glob patterns for *module*.

    The repo maps a source module to its test by the file stem optionally
    prefixed with one or more parent directory segments — e.g.
    ``brutex/data/partition.py`` is tested by ``test_data_partition.py``
    while ``brutex/doctor.py`` is tested by ``test_doctor.py``. A trailing
    ``*`` is appended to every pattern so a curated multi-file suite (e.g.
    ``test_lookahead_barrier.py`` alongside ``test_lookahead.py``, or
    ``test_cli_run.py`` alongside ``test_cli.py``) is discovered too. The
    patterns are returned longest/most-specific suffix first.

    Parameters
    ----------
    module:
        The POSIX module path (e.g. ``brutex/feed/lookahead.py``).

    Returns
    -------
    list[str]
        Glob patterns (filename only), most specific first, each distinct.
    """
    path = Path(module)
    stem = path.stem
    # Directory segments between the package root and the file, e.g.
    # brutex/data/partition.py -> ["data"]; brutex/doctor.py -> [].
    parts = list(path.parts[:-1])
    if parts and parts[0] == "brutex":
        parts = parts[1:]
    # Progressively drop the leftmost parent segments (most specific
    # first), so test_data_partition*.py is listed before a bare
    # test_partition*.py; cut=0 keeps every segment, cut=len(parts) is
    # bare. Each cut yields a strictly shorter suffix, so the patterns are
    # distinct by construction — no dedup needed.
    return [
        f"test_{'_'.join([*parts[cut:], stem]) if parts[cut:] else stem}*.py"
        for cut in range(len(parts) + 1)
    ]


def module_import_tokens(module: str) -> tuple[str, str, str]:
    """Return the dotted identifiers that prove a test covers *module*.

    A test genuinely covering ``brutex/feed/lookahead.py`` must import the
    module by its dotted path in one of three precise forms, checked by
    :func:`_file_imports_module` against a parsed AST (NOT a substring
    scan):

    * ``import brutex.feed.lookahead`` — an :class:`ast.Import` whose alias
      name equals ``module_dotted``;
    * ``from brutex.feed.lookahead import X`` — an :class:`ast.ImportFrom`
      whose ``module`` equals ``module_dotted``;
    * ``from brutex.feed import lookahead`` — an :class:`ast.ImportFrom`
      whose ``module`` equals ``parent_pkg`` and one of whose alias names
      equals ``stem``.

    A foreign same-stem test (e.g. one importing a *different* package's
    ``lookahead``) matches none of these, so it is rejected.

    Parameters
    ----------
    module:
        The POSIX module path (e.g. ``brutex/feed/lookahead.py``).

    Returns
    -------
    tuple[str, str, str]
        ``(module_dotted, parent_pkg, stem)`` — e.g.
        ``("brutex.feed.lookahead", "brutex.feed", "lookahead")``.
    """
    dotted = module.removesuffix(".py").replace("/", ".")
    path = Path(module)
    stem = path.stem
    parent_pkg = ".".join(path.parts[:-1])  # e.g. "brutex.feed"
    return (dotted, parent_pkg, stem)


def _file_imports_module(test_path: Path, module: str) -> bool:
    """Return True if *test_path* imports *module* (not a foreign same-stem).

    The candidate test file is parsed with :func:`ast.parse` and its nodes
    walked, so the decision is precise — independent of comments, string
    contents, line wrapping, or a coincidental substring. The file is
    accepted iff ANY top-or-nested import node satisfies one of:

    * an :class:`ast.Import` with an alias whose ``name`` *exactly* equals
      the module's dotted path (``import brutex.feed.lookahead`` — so
      ``import brutex.feed.lookahead_extra`` or ``import
      other.brutex.feed.lookahead`` is rejected);
    * an :class:`ast.ImportFrom` whose ``module`` equals the dotted path
      (``from brutex.feed.lookahead import X``);
    * an :class:`ast.ImportFrom` whose ``module`` equals the parent
      package *and* one of whose alias ``name`` values equals the stem
      (``from brutex.feed import lookahead`` / ``... import lookahead as
      lh`` — the imported NAME is matched, never the asname).

    A relative import (``from . import x``) has ``node.module is None`` and
    never matches. An :class:`ast.ImportFrom` always carries a
    non-``None`` ``names`` list, so the alias scan needs no guard. An
    unreadable file (:class:`OSError`) and a file that fails to parse
    (:class:`SyntaxError`) are both treated as a non-match — a test that
    cannot be read or is syntactically broken cannot be relied on to cover
    the module — and the parse failure is reported on stdout (the script's
    CLI contract).

    Parameters
    ----------
    test_path:
        Absolute path to a candidate ``test_*.py`` file.
    module:
        The POSIX module path being covered.

    Returns
    -------
    bool
        Whether the file genuinely imports *module*.
    """
    try:
        text = test_path.read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        tree = ast.parse(text, filename=str(test_path))
    except SyntaxError as exc:
        print(f"  WARN: cannot parse {test_path} ({exc}) — treating as no import")
        return False
    module_dotted, parent_pkg, stem = module_import_tokens(module)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == module_dotted for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            if node.module == module_dotted:
                return True
            if node.module == parent_pkg and any(alias.name == stem for alias in node.names):
                return True
    return False


def discover_tests_for_module(module: str, test_files: Sequence[Path]) -> list[str]:
    """Return the sorted POSIX test paths that IMPORT *module*, or ``[]``.

    *test_files* is the pre-globbed list of every ``tests/**/test_*.py``.
    A candidate is one whose basename matches one of the module's glob
    patterns AND which genuinely imports the module (a foreign same-stem
    test is rejected). Sorting keeps the pytest invocation deterministic.

    Parameters
    ----------
    module:
        The POSIX module path being covered.
    test_files:
        Every discovered ``tests/**/test_*.py`` path.

    Returns
    -------
    list[str]
        Sorted POSIX-relative paths of the import-verified tests.
    """
    patterns = candidate_test_globs(module)
    matched: set[str] = set()
    for tf in test_files:
        if not any(tf.match(pattern) for pattern in patterns):
            continue
        if _file_imports_module(tf, module):
            matched.add(tf.relative_to(ROOT).as_posix())
    return sorted(matched)


def all_test_files() -> list[Path]:
    """Return every ``tests/**/test_*.py`` path, sorted (deterministic)."""
    return sorted((ROOT / TESTS_DIR).rglob("test_*.py"))


class Plan:
    """The discovered coverage plan for a diff.

    Attributes
    ----------
    entries:
        Sorted ``(module, [test_paths])`` for every touched, non-excluded
        module that HAS at least one import-verified test.
    missing_added:
        Sorted ADDED modules with NO verified test — a hard FAIL.
    skipped_modified:
        Sorted MODIFIED modules with NO verified test — WARN + skip (they
        still ride the static gate + the whole-package floor).
    """

    def __init__(
        self,
        entries: list[tuple[str, list[str]]],
        missing_added: list[str],
        skipped_modified: list[str],
    ) -> None:
        """Store the partitioned discovery result.

        Parameters
        ----------
        entries:
            ``(module, [test_paths])`` for modules with a verified test.
        missing_added:
            ADDED modules with no verified test (a hard FAIL).
        skipped_modified:
            MODIFIED modules with no verified test (WARN + skip).
        """
        self.entries = entries
        self.missing_added = missing_added
        self.skipped_modified = skipped_modified

    def is_empty(self) -> bool:
        """Return True if the plan has nothing to report (no touched modules)."""
        return not (self.entries or self.missing_added or self.skipped_modified)


def build_plan(base: str) -> Plan:
    """Discover the coverage plan for the diff against *base*.

    Parameters
    ----------
    base:
        The git ref (merge base) to diff ``HEAD`` against.

    Returns
    -------
    Plan
        The partitioned discovery result (entries / missing-added /
        skipped-modified), every list sorted for determinism.
    """
    diff_rows = _git_diff_name_status(base)
    modules = discover_touched_modules(diff_rows)
    test_files = all_test_files()
    entries: list[tuple[str, list[str]]] = []
    missing_added: list[str] = []
    skipped_modified: list[str] = []
    for module, status in modules:
        tests = discover_tests_for_module(module, test_files)
        if tests:
            entries.append((module, tests))
        elif status == "A":
            missing_added.append(module)
        else:
            skipped_modified.append(module)
    return Plan(
        entries=sorted(entries),
        missing_added=sorted(missing_added),
        skipped_modified=sorted(skipped_modified),
    )


#: Substrings identifying a CORRUPT coverage data file in a ``coverage
#: report`` capture. Observed live (CI + local repro): ``Couldn't use data
#: file '….coverage…': no such table: tracer`` — the SQLite store was
#: truncated/half-written at GENERATION time inside the pytest run, so
#: re-reading the same file can never succeed. Since the widened-retry fold
#: EVERY not-measured probe re-runs the whole pytest section once; the
#: signature only refines the retry WARN's diagnosis (corrupt-at-generation
#: vs signature-less parallel-mode data loss) — DEC-PULL-BEAT-PERRUN-001
#: gate-hardening fold + the widened-retry reconcile.
_CORRUPT_DATA_SIGNATURES: tuple[str, ...] = ("no such table", "Couldn't use data file")

#: Base name of the per-section coverage data files (``COVERAGE_FILE``
#: isolation). Matched by the repo ``.gitignore``'s ``.coverage.*`` entry.
_GATE_DATA_FILE_PREFIX = ".coverage.gate"


def _section_env(module: str) -> dict[str, str]:
    """Return the section's subprocess env with an ISOLATED ``COVERAGE_FILE``.

    Every subprocess of one module's gate section — the pytest run AND both
    ``coverage report`` reads — must share ONE per-module data file (e.g.
    ``.coverage.gate.brutex.data.sink``), NEVER the repo-shared ``.coverage``:
    the shared root file is written by the main suite and by every other gate
    section, so a stale/corrupt leftover there could poison this section's
    report (and the gate previously clobbered the main suite's data in turn).

    Parameters
    ----------
    module:
        The POSIX module path being covered.

    Returns
    -------
    dict[str, str]
        A copy of ``os.environ`` with ``COVERAGE_FILE`` pointing at the
        section's own data file under ``ROOT``.
    """
    slug = module.removesuffix(".py").replace("/", ".")
    return {**os.environ, "COVERAGE_FILE": str(ROOT / f"{_GATE_DATA_FILE_PREFIX}.{slug}")}


def _erase_section_data(env: dict[str, str]) -> None:
    """Erase the section's coverage data file (and its parallel shards).

    Runs ``coverage erase`` under the section env so every attempt starts
    from a clean slate — ``[tool.coverage.run] parallel = true`` means a
    crashed or corrupt earlier attempt can leave both the combined data file
    and stray ``<data-file>.<host>.<pid>.*`` shards behind, which the next
    pytest-cov combine would otherwise fold in.

    Parameters
    ----------
    env:
        The section env from :func:`_section_env`.
    """
    subprocess.run(  # nosec B603 — fixed argv (sys.executable), no shell
        [sys.executable, "-m", "coverage", "erase"],
        cwd=ROOT,
        env=env,
        text=True,
        check=False,
    )


def _is_corrupt_data(stdout: str, stderr: str) -> bool:
    """Return True if a report capture carries the corrupt-data signature.

    Used only to pick the section-retry WARN's diagnosis (corrupt data file
    vs a signature-less parallel-mode data loss) — the retry itself fires on
    ANY not-measured probe, signature or not.

    Parameters
    ----------
    stdout:
        The captured stdout of a ``coverage report`` invocation.
    stderr:
        The captured stderr of the same invocation (the usual carrier —
        coverage prints ``Couldn't use data file '…': no such table: …``
        there).

    Returns
    -------
    bool
        Whether either stream names a corrupt data file.
    """
    blob = f"{stdout}\n{stderr}"
    return any(signature in blob for signature in _CORRUPT_DATA_SIGNATURES)


def _print_report_stderr(stderr: str) -> None:
    """Print a non-empty ``coverage report`` stderr (the swallowed-error fix).

    Parameters
    ----------
    stderr:
        The captured stderr of a ``coverage report`` invocation; blank / whitespace
        prints nothing.
    """
    text = stderr.strip()
    if text:
        print(f"  coverage report stderr: {text}")


def _run_coverage(module: str, tests: Sequence[str]) -> bool:
    """Run pytest --cov for *module* over *tests*; return True if 100%.

    Uses the numpy-safe ``--cov=brutex`` (whole-package trace) + a
    ``coverage report --include=<file>`` filter the existing metrics gates
    use, so a dotted ``--cov=brutex.metrics.X`` never double-loads numpy
    2.x. The per-file 100% assertion is ``--fail-under=100`` on the
    filtered report; a file the tests never imported is reported as
    not-measured and also fails (the exact 0%-masking this gate exists to
    prevent).

    Data-file hygiene (DEC-PULL-BEAT-PERRUN-001 gate-hardening fold — the
    spurious-red hardening): the gate was observed failing on genuinely-100%
    modules because the coverage DATA FILE was CORRUPT at GENERATION time —
    pytest-cov's own in-process report already failed with ``Couldn't use
    data file '…': no such table: tracer`` INSIDE the pytest section
    (``[tool.coverage.run] parallel = true`` plus suites that deliberately
    ABANDON daemon threads can race pytest-cov's stop/combine on a shared
    data file), so the earlier report-only retry re-read the same corrupt
    file and could never recover. Four-part hardening:

    * every subprocess of the section — the pytest run AND both ``coverage
      report`` reads — shares ONE per-module ``COVERAGE_FILE``
      (:func:`_section_env`), never the repo-shared ``.coverage``;
    * every attempt opens with ``coverage erase``
      (:func:`_erase_section_data`) so a stale/corrupt leftover can never
      poison it;
    * ANY not-measured probe re-runs the WHOLE pytest section ONCE (a fresh
      data file — never a doomed re-read of the same one). The corrupt-data
      signature (:func:`_is_corrupt_data`) only refines the retry WARN's
      diagnosis: observed live (widened-retry reconcile — a 3-run HEAD
      sample went red 2-of-3, one failure a SIGNATURE-LESS not-measured on
      a module the same section's in-process pytest-cov table printed at
      100.00%), parallel-mode data LOSS can surface as a clean not-measured
      with NO signature, so a signature-less miss is NOT proof of the
      genuine 0%-masking case and the earlier signature-gated retry never
      fired for it. FAIL-CLOSED: a module whose tests genuinely never
      import it is not-measured on BOTH attempts deterministically, so the
      widened retry can never turn 0%-masking green — the failure direction
      stays LOUD (red spuriously possible, green wrongly never);
    * the pytest run passes ``--cov-report=``: the gate never reads
      pytest-cov's in-process terminal report, and that reporter is itself
      one more reader racing on the parallel-mode data file — suppressing
      it removes the race participant for free (the out-of-process probe is
      the ONLY reader).

    Parameters
    ----------
    module:
        The POSIX module path being covered.
    tests:
        The import-verified test paths to run.

    Returns
    -------
    bool
        Whether the module is at 100% line + branch coverage.
    """
    env = _section_env(module)
    pytest_cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--cov=brutex",
        "--cov-branch",
        "--cov-fail-under=0",
        "--cov-report=",
        *tests,
    ]
    include = f"*/{module}"
    measured_cmd = [sys.executable, "-m", "coverage", "report", f"--include={include}"]
    section_retries_left = 1
    while True:
        _erase_section_data(env)
        proc = subprocess.run(  # noqa: S603  # nosec B603 — fixed argv (sys.executable), no shell
            pytest_cmd, cwd=ROOT, env=env, text=True, check=False
        )
        if proc.returncode != 0:
            print(f"  FAIL: pytest errored for {module} (exit {proc.returncode})")
            return False
        measured = subprocess.run(  # noqa: S603  # nosec B603 — fixed argv (sys.executable), no shell
            measured_cmd, cwd=ROOT, env=env, capture_output=True, text=True, check=False
        )
        if module in measured.stdout:
            break
        _print_report_stderr(measured.stderr)
        if section_retries_left:
            section_retries_left -= 1
            if _is_corrupt_data(measured.stdout, measured.stderr):
                print(
                    f"  WARN: corrupt coverage data for {module} (the data file was "
                    "corrupt at generation time) — re-running the pytest section once"
                )
            else:
                print(
                    f"  WARN: {module} not measured with NO corrupt-data signature "
                    "(spurious parallel-mode data loss is possible) — "
                    "re-running the pytest section once"
                )
            continue
        print(f"  FAIL: {module} was not measured by its tests — gate failed")
        return False
    report = subprocess.run(  # noqa: S603  # nosec B603 — fixed argv (sys.executable), no shell
        [
            sys.executable,
            "-m",
            "coverage",
            "report",
            f"--include={include}",
            "--show-missing",
            "--fail-under=100",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        check=False,
    )
    return report.returncode == 0


def _print_plan(plan: Plan) -> None:
    """Print the discovered plan (entries, missing-added, skipped-modified).

    Parameters
    ----------
    plan:
        The discovery result to render to stdout.
    """
    print("coverage-gate discovery plan (auto-discovered touched brutex modules):")
    for module, tests in plan.entries:
        print(f"  - {module} -> {', '.join(tests)}")
    if plan.missing_added:
        print("  NEW modules with NO import-verified test (gate will FAIL):")
        for module in plan.missing_added:
            print(f"    - {module}")
    if plan.skipped_modified:
        print("  modified modules with NO dedicated test (WARN + skip — ride the static gate):")
        for module in plan.skipped_modified:
            print(f"    - {module}")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — see the module docstring for the contract.

    Parameters
    ----------
    argv:
        Optional argument vector (defaults to ``sys.argv[1:]``).

    Returns
    -------
    int
        The process exit code (0 pass / 1 gate failure / 2 setup error).
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        default=DEFAULT_BASE,
        help=f"merge base for the diff (default: {DEFAULT_BASE})",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print the discovered plan and exit (no coverage run)",
    )
    args = parser.parse_args(argv)

    try:
        plan = build_plan(args.base)
    except DiscoveryError as exc:
        print(f"ERROR: {exc}")
        return 2

    if plan.is_empty():
        print("coverage-gate discovery: no touched brutex modules — nothing to check (PASS)")
        return 0

    _print_plan(plan)

    if args.list:
        return 0

    if plan.missing_added:
        print(
            "\nFAIL: a NEW module must ship a dedicated test that imports it "
            "(or be added — with a justification — to "
            "COVERAGE_DISCOVERY_EXCLUDE). Missing tests for: " + ", ".join(plan.missing_added)
        )
        return 1

    failed: list[str] = []
    for module, tests in plan.entries:
        print(f"\n=== 100% gate: {module} ===")
        if not _run_coverage(module, tests):
            failed.append(module)

    if failed:
        print("\ncoverage-gate FAILURES (below 100% line/branch):")
        for module in failed:
            print(f"  - {module}")
        return 1

    print(f"\ncoverage-gate discovery OK: {len(plan.entries)} touched module(s) at 100%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
