"""BruteX CLI entry point — ``brutex <command>``.

Registered in ``pyproject.toml`` as:

    [project.scripts]
    brutex = "brutex.cli:main"

After ``pip install -e .``, the ``brutex`` command is on PATH.

## Status

This CLI was rewritten on 2026-05-15 and re-baselined on 2026-05-26
(DEC-VENDOR-PIVOT-001). Per DEC-GROWW-DHAN-REVERT-001 / ADR-0002 the live
vendor is the Groww Trade API. Legacy ingest subcommands were
vendor-specific and are dropped. Track 1 will reintroduce
``brutex track1 ingest`` once the Groww adapter lands in Phase 1+ of the
phase plan (see ``brutex/adapters/groww/``).

## Commands (Track 1 minimum)

    brutex version
        Print the BruteX version + commit SHA.

    brutex status [--run-id <id>]
        Read ~/.brutex/runtime.json and print the current run's status.
        Returns quickly (< 100 ms per FR-UI-002).

    brutex audit verify <audit-file.jsonl>
        Replay the audit chain and assert integrity (NFR-OBS-003).

    brutex dashboard [--lake-root <dir>] [--host 127.0.0.1] [--port 9080]
        Start the BruteX dashboard FastAPI server. Requires the ``ui``
        extra: ``pip install -e ".[ui]"`` (or just ``make run``).

    brutex run [--lake-root <dir>] [--host ...] [--port ...] [--no-browser]
        One-click launch (DEC-ONECLICK-RUN-001): resolve the lake root,
        open the browser, then start the dashboard. This is what
        ``make run`` / ``python -m brutex run`` / the IDE Run target invoke.

    brutex fetch [--dry-run] [--inspect] [...]
        Fetch historical candles into the lake (Groww adapter).

## Design

* **argparse**, no external dep beyond stdlib.
* Subcommands dispatch to plain functions — each subcommand is a
  2-10 line shim that wires the CLI args into the right classes.
* Output goes to stdout via ``print`` in the CLI layer only (NOT
  in library code, where ``structlog`` is mandatory).
* Exit codes: 0 success, 1 user error, 2 internal error.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from brutex.data.greeks_enrich import GreeksEnrichTarget

from brutex import __version__
from brutex.config.hardware_envelope import apply_polars_thread_bound
from brutex.config.paths import resolve_lake_root
from brutex.config.runtime import RuntimeFileError, read_runtime_file, runtime_file_path
from brutex.vocabulary.bits import IMPLEMENTED_MASK, TOTAL_CONDITIONS

# ────────────────────────────────────────────────────────────────────────
# Exit codes
# ────────────────────────────────────────────────────────────────────────

EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_INTERNAL_ERROR = 2
#: sweep-verify verdict codes (DEC-SWEEP-VERIFY-001): corrupt findings vs an
#: inconclusive-only run (recount refused/uncertain under a context mismatch)
#: are DISTINCT outcomes an operator script must be able to tell apart.
EXIT_VERIFY_FINDINGS = 3
EXIT_VERIFY_INCONCLUSIVE = 4
#: sweep-heal refusal (DEC-SWEEP-HEAL-001): stale evidence / mass-quarantine
#: threshold / concurrent sweep — distinct from the verify codes so operator
#: scripts can tell "found corruption" from "refused to act".
EXIT_HEAL_REFUSED = 5
#: sweep-all completed but >=1 grid tuple raised mid-evaluation and was SKIPPED
#: ("Resilient: skip + continue"): the remaining tuples ran, but the verdict is
#: NON-ZERO so an operator script never mistakes a partial sweep for a clean one
#: (the failed tuples are un-sentineled and will redo next run).
EXIT_SWEEP_TUPLE_FAILED = 6


class CliUserError(Exception):
    """A user-input error that must exit cleanly (DEC-CLI-USER-ERROR-HYGIENE-001).

    Raised by CLI parse/read/guard helpers that cannot themselves return an
    exit code; :func:`main` converts it to the standard one-line
    ``[brutex] <message>`` on stderr + :data:`EXIT_USER_ERROR` — never a raw
    traceback. CLI layer ONLY: library code keeps its own error hierarchies.
    """


def default_condition_bits() -> list[int]:
    """Return the DEFAULT swept condition-bit set — every IMPLEMENTED bit.

    The ONE source of truth for "which conditions does a sweep test when
    ``--conditions`` is not given" (CLAUDE.md §11 COMMON). It is derived
    from the vocabulary's :data:`~brutex.vocabulary.bits.IMPLEMENTED_MASK`
    — the bitmask of conditions whose evaluator computes a real boolean —
    so the default AUTOMATICALLY tracks the vocabulary and can never drift
    behind it again (the pre-DEC-SWEEP-DEFAULT-CONDITIONS-60-001 bug where
    the default was hardcoded to ``range(52)`` while the vocabulary had
    grown to 60 conditions, silently excluding bits 52..59 — VWAP position,
    pivot R5/S5, BOS/CHoCH — from every default sweep).

    Deriving from ``IMPLEMENTED_MASK`` (rather than ``range(TOTAL_CONDITIONS)``)
    is deliberately HONEST: a future LAYOUT-ONLY bit (``implemented=false`` in
    ``conditions.toml``, whose evaluator emits a constant 0) is EXCLUDED, so the
    default never sweeps a dead condition that could never fire. Today every one
    of the ``TOTAL_CONDITIONS`` bits is implemented (``IMPLEMENTED_MASK`` is
    dense ``0..TOTAL_CONDITIONS-1``), so this returns ``[0, 1, ..., 59]``.

    Returns
    -------
    list[int]
        The implemented condition-bit indices in ascending order.
    """
    return [bit for bit in range(TOTAL_CONDITIONS) if IMPLEMENTED_MASK & (1 << bit)]


def _parse_int_csv(raw: str, *, flag: str) -> list[int]:
    """Parse a comma-separated integer list for *flag* (CLAUDE.md §11 COMMON).

    Byte-identical to the historical inline
    ``[int(tok) for tok in raw.split(",") if tok.strip()]`` for valid input
    (blank tokens skipped, surrounding whitespace tolerated, order AND
    duplicates preserved). A non-integer token raises :class:`CliUserError`
    naming the token (DEC-CLI-USER-ERROR-HYGIENE-001).
    """
    out: list[int] = []
    for tok in raw.split(","):
        token = tok.strip()
        if not token:
            continue
        try:
            out.append(int(token))
        except ValueError as exc:
            raise CliUserError(
                f"invalid {flag} token {token!r}: expected a comma-separated list of integers"
            ) from exc
    return out


def parse_condition_ids(raw: str) -> list[int]:
    """Parse the ``--conditions`` CSV of condition-bit indices (COMMON helper).

    The ONE parser behind all three sweep surfaces (``sweep`` / ``sweep-all`` /
    ``sweep-verify``), replacing three inline copies. Valid input parses
    byte-identically to the old inline expression. Each id is a 0-indexed bit
    position validated against the VOCABULARY range
    ``0..TOTAL_CONDITIONS - 1`` (0..59) — deliberately NOT the engine's
    forward-compat ``[0, 128)`` u128 ceiling: bits 60..127 are
    reserved/undefined dead bits the results store rejects
    (``MASK_BITS_MAX``), so admitting them here would be a silent illusion.

    Raises
    ------
    CliUserError
        On a non-integer token or an out-of-range bit index.
    """
    bits = _parse_int_csv(raw, flag="--conditions")
    for bit in bits:
        if not 0 <= bit <= TOTAL_CONDITIONS - 1:
            raise CliUserError(
                f"--conditions bit {bit} out of range: valid bit indices are "
                f"0..{TOTAL_CONDITIONS - 1}"
            )
    return bits


def sweep_all_exit_code(tuples_failed: int) -> int:
    """Map a completed ``sweep-all`` run's ``tuples_failed`` to its exit code.

    The honest verdict for "Resilient: skip + continue" (Golden Rule #10): a
    fully clean grid exits :data:`EXIT_OK`; one or more tuples skipped after a
    :class:`~brutex.engine.sweep_run.SweepComboError` makes the verdict NON-ZERO
    (:data:`EXIT_SWEEP_TUPLE_FAILED`) so a skip is never a silent illusion.

    Parameters
    ----------
    tuples_failed
        The count from the :class:`~brutex.engine.sweep_supervisor.SupervisedGridSummary`.

    Returns
    -------
    int
        :data:`EXIT_OK` when ``tuples_failed == 0``, else
        :data:`EXIT_SWEEP_TUPLE_FAILED`.
    """
    return EXIT_SWEEP_TUPLE_FAILED if tuples_failed > 0 else EXIT_OK


def sweep_verify_exit_code(*, has_findings: bool, inconclusive: bool) -> int:
    """Map a completed ``sweep-verify`` run's outcome flags to its exit code.

    Corruption findings dominate: any non-clean report exits
    :data:`EXIT_VERIFY_FINDINGS`. Absent findings, an inconclusive-only run (a
    recount refused/uncertain under a context mismatch, OR — DEC-SWEEP-VERIFY
    "Resilient: skip + continue" — a shadow re-sweep that skipped >=1 tuple, so
    the byte-diff would be against a PARTIAL re-sweep) exits
    :data:`EXIT_VERIFY_INCONCLUSIVE`; otherwise :data:`EXIT_OK`. The
    findings-before-inconclusive ordering matches the inline verdict it replaces.

    Parameters
    ----------
    has_findings
        ``True`` when any verify report is not clean (corruption / divergence).
    inconclusive
        ``True`` when the run is inconclusive-only (no findings, but the verdict
        cannot be trusted — e.g. a partial recount).

    Returns
    -------
    int
        :data:`EXIT_VERIFY_FINDINGS`, :data:`EXIT_VERIFY_INCONCLUSIVE`, or
        :data:`EXIT_OK`.
    """
    if has_findings:
        return EXIT_VERIFY_FINDINGS
    if inconclusive:
        return EXIT_VERIFY_INCONCLUSIVE
    return EXIT_OK


# ────────────────────────────────────────────────────────────────────────
# Argparse builder
# ────────────────────────────────────────────────────────────────────────


def _add_sweep_grid_args(
    parser: argparse.ArgumentParser,
    *,
    dates_required: bool = True,
) -> None:  # pragma: no cover — declarative argparse wiring (cli-omitted)
    """Add the shared grid-sweep arguments to *parser* (CLAUDE.md §11 COMMON).

    The single declaration of the full ``(symbols × intervals × directions × K ×
    SL × TP)`` grid + lake/output/overfit/options-sim knobs, reused by
    ``sweep-all`` (manual), ``sweep-daemon`` (automatic) and ``nightly run``
    (DEC-SWEEP-NIGHTLY-AUTOMATION-001) so the surfaces can never drift apart —
    exactly one source of truth for the sweep CLI contract
    (DEC-SWEEP-SCHEDULER-001). *dates_required* (default ``True`` — the
    ``sweep-all``/``sweep-daemon`` contract is byte-unchanged) is relaxed ONLY
    by ``nightly run``, which fills blank dates from ``default_date_range()``
    (the SAME [2020-01-01, previous IST day] law the pull uses) at dispatch.
    """
    parser.add_argument(
        "--symbols",
        default="NIFTY,BANKNIFTY",
        help="Comma-separated index symbols (default NIFTY,BANKNIFTY — run SENSEX with --exchange BSE)",
    )
    parser.add_argument(
        "--start", required=dates_required, help="Window start (YYYY-MM-DD or full ISO)"
    )
    parser.add_argument(
        "--end", required=dates_required, help="Window end (YYYY-MM-DD or full ISO)"
    )
    parser.add_argument(
        "--exchange", default="NSE", help="LAKE exchange partition (default NSE; BSE for SENSEX)"
    )
    parser.add_argument("--segment", default="INDEX", help="LAKE segment partition (default INDEX)")
    parser.add_argument(
        "--intervals",
        default="1minute,5minute,15minute",
        help="Comma-separated lake interval labels to sweep (default 1minute,5minute,15minute)",
    )
    parser.add_argument(
        "--directions",
        default="BUY,SELL",
        help="Comma-separated directions BUY/SELL (default BUY,SELL)",
    )
    parser.add_argument(
        "--k", default="1,2", help="Comma-separated conditions-per-combo values (default 1,2)"
    )
    parser.add_argument(
        "--fib-anchors",
        default="prev_day",
        dest="fib_anchors",
        help=(
            "Comma-separated Fibonacci-anchor families to sweep — "
            "prev_day / rolling_weekly / weekly_static (DEC-NIFTY-FIB-AXIS-001). "
            "Default prev_day (byte-identical to the pre-axis sweep); pass "
            "'prev_day,rolling_weekly,weekly_static' to test all three (×3 grid)"
        ),
    )
    parser.add_argument(
        "--conditions",
        default=None,
        help=(
            f"Comma-separated condition bit indices 0..{TOTAL_CONDITIONS - 1} to sweep "
            f"(default: all {TOTAL_CONDITIONS} implemented conditions)"
        ),
    )
    parser.add_argument(
        "--sl",
        default="none",
        help="Comma-separated stop-loss paisa values; use 'none' to disable a leg (default none)",
    )
    parser.add_argument(
        "--tp",
        default="none",
        help="Comma-separated take-profit paisa values; use 'none' to disable a leg (default none)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=20,
        help=(
            "Persist the top-N composite-ranked combos per tuple (default 20 for fast "
            "operator runs; the spec's Stage-4 filter #1 production sizing is 1000 — "
            "TRACK1_BACKTEST_SPEC.md, DEC-SPEC-TOPK-RECONCILE-001)"
        ),
    )
    parser.add_argument(
        "--mode", default="A", help="Run mode label stamped on the run identity (default A)"
    )
    parser.add_argument(
        "--max-tuples",
        type=int,
        default=None,
        help="Refuse a grid larger than this many tuples (honest opt-in guard)",
    )
    parser.add_argument(
        "--max-combos",
        type=int,
        default=None,
        help="Refuse any tuple whose mask count exceeds this (honest opt-in guard)",
    )
    parser.add_argument(
        "--allow-missing-slices",
        action="store_true",
        help=(
            "Proceed even when some requested (symbol, interval) pairs have ZERO "
            "bars (those tuples skip as no-bars). Default: REFUSE with the exact "
            "missing list + remediation (DEC-SWEEP-PREFLIGHT-001)"
        ),
    )
    parser.add_argument(
        "--allow-out-of-scope",
        action="store_true",
        dest="allow_out_of_scope",
        help=(
            "RESEARCH ONLY: proceed even when a requested symbol is outside the "
            "brute-force engine scope (NIFTY/BANKNIFTY/SENSEX spot + NIFTY futures, "
            "CLAUDE.md §2 Rule 3 / ADR-0012). Loud WARNING + structlog corporate-"
            "action hazard + audit-stamped scope_override=True; also gates "
            "sweep-verify --recount; REFUSED on sweep-daemon (unattended runs never "
            "override) — DEC-ENGINE-SCOPE-GUARD-001 + DEC-CORP-ACTION-GATE-001"
        ),
    )
    parser.add_argument(
        "--data-snapshot-hash",
        default=None,
        dest="data_snapshot_hash",
        help="Override the data-snapshot identity (default: content fingerprint of the loaded bars)",
    )
    parser.add_argument(
        "--results-root",
        type=Path,
        default=None,
        dest="results_root",
        help="Directory for the per-tuple Parquet results (default <lake-root>/sweeps)",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=None,
        dest="catalog_path",
        help="Catalog DuckDB path (default <lake-root>/catalog.duckdb)",
    )
    parser.add_argument(
        "--lake-root",
        type=Path,
        default=None,
        help="Lake root (default $BRUTEX_LAKE_ROOT or ~/.brutex/lake)",
    )
    parser.add_argument(
        "--overfit-gate",
        action="store_true",
        dest="overfit_gate",
        help=(
            "Enable the Stage-3 multiple-testing overfit gate (Deflated Sharpe) "
            "per leaderboard (DEC-RANKING-OVERFIT-001); off by default"
        ),
    )
    # ── Stage-3 BIPOP-CMA-ES refinement pass (DEC-STAGE3-WIRE-001) ──
    # Defaults are None so SweepGridConfig stays the ONE source of the engine
    # defaults (top-K 10,000 · 200 fevals/restart · 3 restarts — ADR-0007
    # condition 8); the CLI only overrides what the operator sets explicitly.
    parser.add_argument(
        "--stage3-enable",
        action="store_true",
        dest="stage3_enable",
        help=(
            "Enable the Stage-3 BIPOP-CMA-ES (SL, TP) refinement post-pass over each "
            "leaderboard's top-K survivors (ADR-0007 / DEC-STAGE3-WIRE-001; requires the "
            "'optimize' extra and every --sl/--tp value on the Mode-A grid); off by default"
        ),
    )
    parser.add_argument(
        "--stage3-top-k",
        type=int,
        default=None,
        dest="stage3_top_k",
        help="Refine each (instrument, TF, mode) leaderboard's top-K survivors (default 10000)",
    )
    parser.add_argument(
        "--stage3-fevals",
        type=int,
        default=None,
        dest="stage3_fevals",
        help="CMA-ES evaluations per restart for each refined mask (default 200)",
    )
    parser.add_argument(
        "--stage3-restarts",
        type=int,
        default=None,
        dest="stage3_restarts",
        help="Hard restart cap per refined mask — bounds a mask at fevals×restarts (default 3)",
    )
    parser.add_argument(
        "--dsr-min",
        type=float,
        default=0.95,
        dest="dsr_min",
        help="Min Deflated-Sharpe probability to survive the overfit gate (default 0.95)",
    )
    parser.add_argument(
        "--no-options-sim",
        action="store_false",
        dest="options_sim",
        help=(
            "Disable the automatic post-sweep options-sim (DEC-OPTIONS-EXECUTOR-001). "
            "By default, when a rolling-option lake is present, winners are auto-priced as options."
        ),
    )
    parser.add_argument(
        "--options-expiry",
        default="weekly",
        choices=("weekly", "monthly"),
        dest="options_expiry",
        help="Expiry cycle for the auto options-sim (default weekly)",
    )
    parser.add_argument(
        "--options-broker",
        default="groww",
        choices=("groww", "zerodha"),
        dest="options_broker",
        help="Brokerage rate card for the auto options-sim (default groww)",
    )


def build_parser() -> argparse.ArgumentParser:
    """Construct the full argparse tree."""
    parser = argparse.ArgumentParser(
        prog="brutex",
        description=(
            f"BruteX — extreme drill-down brute-force backtesting engine "
            f"(v{__version__}). See docs/TRACK1_BACKTEST_SPEC.md for "
            f"the current Track 1 spec."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"brutex {__version__}",
    )
    sub = parser.add_subparsers(dest="command", required=False)

    # ── version (redundant with --version but nice as subcommand) ──
    sub.add_parser("version", help="Print BruteX version")

    # ── status ────────────────────────────────────────────────────
    status = sub.add_parser(
        "status",
        help="Show the running pipeline status (from ~/.brutex/runtime.json)",
    )
    status.add_argument(
        "--run-id",
        help="Override the run_id to look for (default: read from runtime file)",
    )

    # ── audit verify ──────────────────────────────────────────────
    audit = sub.add_parser("audit", help="Audit log tools")
    audit_sub = audit.add_subparsers(dest="audit_command", required=True)
    audit_verify = audit_sub.add_parser(
        "verify",
        help="Verify the hash chain of a JSONL audit file (NFR-OBS-003)",
    )
    audit_verify.add_argument(
        "audit_file",
        type=Path,
        help="Path to a JSONL audit-log file",
    )
    audit_verify.add_argument(
        "--format",
        dest="audit_format",
        choices=["orchestrator", "sweep", "auto"],
        default="auto",
        help=(
            "Chain algorithm: 'orchestrator' (SHA-256 ingest chain), 'sweep' "
            "(BLAKE3 supervisor chain), or 'auto' to detect from the file "
            "(default). 'auto' verifies EITHER format and never misreports a "
            "valid chain as corrupt."
        ),
    )

    # ── catalog (DEC-CATALOG-RECONCILE-WIRE-001) — rebuild partitions from lake ──
    catalog_cmd = sub.add_parser(
        "catalog",
        help="DuckDB catalog tools (partition registry over the Parquet lake)",
    )
    catalog_sub = catalog_cmd.add_subparsers(dest="catalog_command", required=True)
    catalog_reconcile = catalog_sub.add_parser(
        "reconcile",
        help=(
            "Rebuild the catalog 'partitions' table from the on-disk Parquet "
            "lake so an already-pulled lake is sweep-able with no re-pull"
        ),
    )
    catalog_reconcile.add_argument(
        "--lake-root",
        type=Path,
        default=None,
        help="Lake root (default $BRUTEX_LAKE_ROOT or ~/.brutex/lake)",
    )

    # ── lake (DEC-LAKE-HEAL-001) — data-lake integrity tools ──
    lake = sub.add_parser("lake", help="Data-lake integrity tools")
    lake_sub = lake.add_subparsers(dest="lake_command", required=True)
    lake_audit = lake_sub.add_parser(
        "audit-unique",
        help=(
            "READ-ONLY census: per-segment clean/poisoned partition counts "
            "+ the engine-index (NIFTY/BANKNIFTY/SENSEX) verdict"
        ),
    )
    lake_audit.add_argument(
        "--lake-root",
        type=Path,
        default=None,
        help="Lake root (default $BRUTEX_LAKE_ROOT or ~/.brutex/lake)",
    )
    lake_audit.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help=(
            "Emit the census as JSON (the SAME dict shape "
            "GET /api/v1/universe/lake-uniqueness serves)"
        ),
    )
    lake_audit.add_argument(
        "--exchange",
        default=None,
        help="Only this exchange's bars/ tree (e.g. NSE; default: all)",
    )
    lake_audit.add_argument(
        "--segment",
        default=None,
        help="Only this bars/ segment (INDEX / CASH / FNO; default: all)",
    )
    lake_audit.add_argument(
        "--symbol",
        default=None,
        help="Only this symbol directory (default: all)",
    )
    lake_audit.add_argument(
        "--interval",
        default=None,
        help="Only this interval directory (e.g. 1minute; default: all)",
    )
    lake_audit.add_argument(
        "--no-options",
        action="store_true",
        help="Skip the expired-options tree (options/<series>/ dup census)",
    )

    # ── reopen-skipped (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001) — un-cement settles ──
    reopen_skipped = sub.add_parser(
        "reopen-skipped",
        help=(
            "Re-open SKIPPED (settled no-data) pull windows so the next pull "
            "re-verifies them — the un-cement path for windows a vendor "
            "soft-throttle / mis-addressed request may have faked empty. "
            "Refuses while a pull is running (the account-wide single-flight "
            "lock); re-opened windows are breaker-exempt re-checks"
        ),
    )
    reopen_skipped.add_argument(
        "--lake-root",
        type=Path,
        default=None,
        help="Lake root (default $BRUTEX_LAKE_ROOT or ~/.brutex/lake)",
    )
    reopen_skipped.add_argument(
        "--symbol",
        default=None,
        help="Only windows of this symbol / contract groww_symbol (default: any)",
    )
    reopen_skipped.add_argument(
        "--market",
        default=None,
        help="Only this ledger namespace (e.g. groww / groww-option / groww-future; default: any)",
    )
    reopen_skipped.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Bound the re-open to the first N matching windows (default: all matching)",
    )

    # ── detect-session-drift (DEC-CALENDAR-OBSERVED-SESSIONS-001) ──
    drift = sub.add_parser(
        "detect-session-drift",
        help=(
            "Scan ONE fully-past month for observed-holiday drift (a derived "
            "trading day with ZERO bars across all 3 engine indices + clean "
            "SUCCESS ledger evidence) and reconcile the remove-only "
            "observed-not-official overlay sidecar. Never adds a session; "
            "never names a holiday (Golden Rule #1)."
        ),
    )
    drift.add_argument(
        "--lake-root",
        type=Path,
        default=None,
        help="Lake root (default $BRUTEX_LAKE_ROOT or ~/.brutex/lake)",
    )
    drift.add_argument(
        "--month",
        required=True,
        help="The FULLY-PAST month to scan, as YYYY-MM (e.g. 2026-01)",
    )

    # ── auth (DEC-CREDENTIALS-STORE-001) — set/status/clear creds ──
    auth = sub.add_parser(
        "auth",
        help="Manage the stored token credentials (set once, auto-load every session)",
    )
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    auth_sub.add_parser(
        "set",
        help="Interactively prompt (no echo) for the access token + client id and save them",
    )
    auth_sub.add_parser(
        "status",
        help="Print whether token credentials are stored (booleans only; never values)",
    )
    auth_sub.add_parser(
        "clear",
        help="Delete the stored token credentials file",
    )

    # ── dashboard ─────────────────────────────────────────────────
    dashboard = sub.add_parser(
        "dashboard",
        help="Start the BruteX dashboard FastAPI server",
    )
    dashboard.add_argument(
        "--lake-root",
        type=Path,
        default=None,
        help=(
            "Lake directory containing ``instruments.duckdb``. When "
            "supplied, the dashboard exposes browser views. When "
            "omitted, only live pipeline progress is shown."
        ),
    )
    dashboard.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1)")
    dashboard.add_argument("--port", type=int, default=9080, help="Bind port (default 9080)")

    # ── run (DEC-ONECLICK-RUN-001) — the one-click entry point ────
    run = sub.add_parser(
        "run",
        help="One-click: start the BruteX dashboard + open the browser (the IDE Run target)",
    )
    run.add_argument(
        "--lake-root",
        type=Path,
        default=None,
        help="Lake root dir (default: $BRUTEX_LAKE_ROOT or ~/.brutex/lake)",
    )
    run.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1)")
    run.add_argument("--port", type=int, default=9080, help="Bind port (default 9080)")
    run.add_argument(
        "--no-browser",
        action="store_true",
        help="Do NOT auto-open the browser (for headless / CI runs)",
    )

    # ── fetch (DEC-GROWW-INGEST-001) — pull candles into the lake ──
    fetch = sub.add_parser(
        "fetch",
        help="Fetch historical candles into the lake (indices first; --dry-run for offline sample data)",
    )
    fetch.add_argument(
        "--lake-root",
        type=Path,
        default=None,
        help="Lake root dir (default: $BRUTEX_LAKE_ROOT or ~/.brutex/lake)",
    )
    fetch.add_argument(
        "--start", type=str, default=None, help="Start date/ISO (default: Jan 1 of this year, UTC)"
    )
    fetch.add_argument("--end", type=str, default=None, help="End date/ISO (default: now, UTC)")
    fetch.add_argument(
        "--interval",
        default="all",
        help=(
            "Candle interval: 1minute / 1day (aliases 1m / 1d), or 'all' for both "
            "(default). Groww fetches 1m + 1d only — the 19 intraday timeframes are "
            "resampled from the 1-minute base (DEC-DHAN-1M-ONLY-001)"
        ),
    )
    fetch.add_argument(
        "--window-days",
        type=int,
        default=None,
        help="Legacy chunk-window override — ignored on Groww (tiled internally)",
    )
    fetch.add_argument(
        "--dry-run",
        action="store_true",
        help="Use synthetic candles (no Groww creds) — proves the pipe + writes sample lake data",
    )
    fetch.add_argument(
        "--inspect",
        action="store_true",
        help=(
            "Probe mode: collect in memory (no Parquet) and print a per-timeframe "
            "report — pre/regular/post-market bar counts (IST) + timestamp grid-alignment"
        ),
    )
    fetch.add_argument(
        "--rows-per-chunk",
        type=int,
        default=5,
        help="Synthetic rows per chunk in --dry-run mode (default 5)",
    )
    fetch.add_argument(
        "--universe",
        action="store_true",
        help=(
            "also ingest instrument master + expiries + contracts into the lake "
            "before candles (LAKE surface, DEC-P0-063)"
        ),
    )
    fetch.add_argument(
        "--instruments",
        action="store_true",
        help=(
            "ONLY load the keyless public instrument master (no vendor API keys), "
            "print the index/stock counts, and exit — populates the /universe "
            "list without auth (DEC-INSTRUMENT-LOAD-001)"
        ),
    )
    fetch.add_argument(
        "--no-auto-retry",
        action="store_true",
        help=(
            "Disable the post-pull auto-retry of leftover failed windows "
            "(DEC-INGEST-AUTO-RETRY-001) — keep the prior single-pass behaviour"
        ),
    )
    fetch.add_argument(
        "--retry-failed",
        action="store_true",
        help=(
            "MANUAL trigger: re-pull ONLY the currently-failed windows on demand "
            "(the coverage ledger skips everything already in the lake) — same "
            "engine as the automatic retry (DEC-INGEST-AUTO-RETRY-001)"
        ),
    )
    fetch.add_argument(
        "--until-clean",
        action="store_true",
        help=(
            "MANUAL: keep running bounded auto-retry rounds until the coverage "
            "ledger is CLEAN, OR the token dies (stop + auth_expired verdict), OR "
            "the round budget is spent — reuses the same retry engine, rate-safe "
            "(≤8 r/s), bounded (NOT infinite) (DEC-TOKEN-PREFLIGHT-001)"
        ),
    )
    fetch.add_argument(
        "--until-clean-rounds",
        type=int,
        default=None,
        help=(
            "Bounded OUTER round budget for --until-clean "
            "(default: until_clean.DEFAULT_MAX_ROUNDS). Never infinite."
        ),
    )
    fetch.add_argument(
        "--max-retry-minutes",
        type=int,
        default=None,
        help=(
            "Arm the retry-storm give-up guard (DEC-RATE-STORM-WATCHDOG-001): each "
            "(symbol, window) fan-out unit gives up after N minutes of continuous "
            "failure, and a symbol that reaches 100%% window-failure fires ONE "
            "Telegram alert (never per window). Give-ups + the alert are stamped as "
            "hash-chained audit rows under <lake>/audit/. Omit to disable (default)."
        ),
    )

    # ── enrich-greeks (DEC-CLI-GREEKS-ENRICH-001) — bulk Greeks re-enrich ──
    enrich = sub.add_parser(
        "enrich-greeks",
        help=(
            "Bulk (re-)enrich stored F&O option candles with Greeks "
            "(stamp-driven, idempotent — the #1188 bulk re-enrich)"
        ),
    )
    enrich.add_argument(
        "--lake-root",
        type=Path,
        default=None,
        help="Lake root (default $BRUTEX_LAKE_ROOT or ~/.brutex/lake)",
    )
    enrich.add_argument(
        "--underlying",
        action="append",
        default=None,
        help=(
            "Bare underlying token, repeatable (NIFTY, BAJAJ-AUTO); "
            "matching is exact + case-sensitive; "
            "default: every stored option underlying"
        ),
    )
    enrich.add_argument(
        "--start",
        default=None,
        help="IST window start YYYY-MM-DD (inclusive; default: all stored months)",
    )
    enrich.add_argument(
        "--end",
        default=None,
        help="IST window end YYYY-MM-DD (INCLUSIVE; widened +1 day internally)",
    )
    enrich.add_argument(
        "--dry-run",
        action="store_true",
        help="Print discovery counts only; enrich nothing, take no lock",
    )

    # ── doctor (DEC-DOCTOR-001) ───────────────────────────────────
    doctor = sub.add_parser(
        "doctor",
        help="Run health checks (audit chain + runtime); closes STANDARDS row 16",
    )
    doctor.add_argument(
        "--audit-file",
        type=Path,
        default=None,
        help="Specific audit JSONL to verify (default: latest in --audit-dir)",
    )
    doctor.add_argument(
        "--audit-dir",
        type=Path,
        default=None,
        help="Directory of audit logs (default: ~/.brutex/audit)",
    )
    doctor.add_argument(
        "--runtime-file",
        type=Path,
        default=None,
        help="Path to runtime file (default: ~/.brutex/runtime.json)",
    )
    doctor.add_argument(
        "--live-subjects-file",
        type=Path,
        default=None,
        help=(
            "Newline-separated text file of live dedup subjects; enables "
            "the L3 audit-replay consistency check (DEC-L3-REPLAY-ORACLE-001). "
            "Omit to skip."
        ),
    )
    doctor.add_argument(
        "--replay-action-filter",
        type=str,
        default="add",
        help="Audit action to project for replay check (default: 'add')",
    )
    # ── doctor --live: the Mac live-proof kit (DEC-DOCTOR-LIVE-PROOF-KIT-001) ──
    doctor.add_argument(
        "--live",
        action="store_true",
        help=(
            "Run the Mac live-proof kit: walk the REAL Groww pull path stage by "
            "stage (env → creds → totp → token → master → spot-1m → depth → vix → "
            "bhavcopy → resample → lake → integrity → resume → rate) and stop at "
            "the first failure with an exact, register-cross-referenced diagnosis"
        ),
    )
    doctor.add_argument(
        "--lake",
        type=Path,
        default=None,
        help="Lake root for the live kit (default: resolve_lake_root())",
    )
    doctor.add_argument(
        "--symbol",
        type=str,
        default="NSE-NIFTY",
        help="groww_symbol the live kit probes (default: NSE-NIFTY)",
    )
    doctor.add_argument(
        "--stage",
        type=str,
        default=None,
        help="Comma-separated live-kit stage numbers to run (default: all, 0-13)",
    )
    doctor.add_argument(
        "--keep-going",
        action="store_true",
        help="Run ALL live stages instead of stopping at the first FAIL",
    )
    doctor.add_argument(
        "--depth-bisect",
        action="store_true",
        help="Stage 6: bounded bisection (≤20 calls/index) to locate the true 1m floor",
    )
    doctor.add_argument(
        "--rate-probe",
        action="store_true",
        help="Stage 13 opt-in: ≤60 s / ≤480-call rate probe, aborts on a throttle burst",
    )
    doctor.add_argument(
        "--against-lake",
        action="store_true",
        help="Stages 9/11: READ the production lake instead of the quarantined subtree",
    )
    doctor.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit the live-kit report as JSON",
    )
    doctor.add_argument(
        "--mint-anyway",
        action="store_true",
        help=(
            "DANGEROUS: allow a real Groww token mint when the SSM token is absent "
            "(may revoke the shared Lambda/TickVault token — single-active-token)"
        ),
    )

    # ── monitor (DEC-MONITOR-LOOP-001) ────────────────────────────
    monitor = sub.add_parser(
        "monitor",
        help=(
            "Run the SLA + Watchdog + replay-oracle monitor loop in the foreground (Ctrl-C to stop)"
        ),
    )
    monitor.add_argument(
        "--poll-interval",
        type=float,
        default=30.0,
        help="Seconds between ticks (default 30.0)",
    )
    monitor.add_argument(
        "--ticks",
        type=int,
        default=0,
        help=(
            "Run exactly N ticks then exit (default 0 = run until "
            "Ctrl-C). Useful for tests + cron-style invocation."
        ),
    )

    # ── notify (DEC-TELEGRAM-WIRE-001) — MANUAL Telegram channel trigger ──
    notify = sub.add_parser(
        "notify",
        help="Telegram alert tools (manual trigger for the channel the sweep auto-alerts use)",
    )
    notify_sub = notify.add_subparsers(dest="notify_command", required=True)
    notify_sub.add_parser(
        "test",
        help=(
            "Send a one-shot Telegram test digest to prove the channel is wired "
            "+ reachable (needs BRUTEX_TELEGRAM_ENABLED=true + token + chat id)"
        ),
    )

    wipe = sub.add_parser(
        "wipe",
        help=(
            "Delete ALL persisted BruteX data (lake, credentials, token, audit, "
            "logs) under ~/.brutex + any BRUTEX_* override. Dry-run by default."
        ),
    )
    wipe.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete. Without this flag, wipe only PREVIEWS the plan (dry-run).",
    )
    wipe.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive typed-'WIPE' confirmation (for non-interactive / CI use).",
    )

    from brutex.engine.sweep_report import SORT_KEYS
    from brutex.feed.resampler import INTERVAL_SECONDS

    sweep = sub.add_parser(
        "sweep",
        help="Brute-force sweep over lake bars → print the ranked top-combo table",
    )
    sweep.add_argument("symbol", help="Index symbol to sweep, e.g. NIFTY / BANKNIFTY / SENSEX")
    sweep.add_argument("--start", required=True, help="Window start (YYYY-MM-DD or full ISO)")
    sweep.add_argument("--end", required=True, help="Window end (YYYY-MM-DD or full ISO)")
    sweep.add_argument(
        "--exchange",
        default="NSE",
        help="LAKE exchange partition (default NSE; use BSE for SENSEX) — must match ingest",
    )
    sweep.add_argument(
        "--segment",
        default="INDEX",
        help="LAKE segment partition (CASH/FNO/INDEX; default INDEX for the spot indexes)",
    )
    sweep.add_argument(
        "--interval",
        default="1minute",
        choices=sorted(INTERVAL_SECONDS),
        help="LAKE interval label (default 1minute)",
    )
    sweep.add_argument(
        "--direction", default="BUY", choices=["BUY", "SELL"], help="Trade direction (default BUY)"
    )
    sweep.add_argument("--k", type=int, default=2, help="Conditions required per combo (default 2)")
    sweep.add_argument(
        "--conditions",
        default=None,
        help=(
            f"Comma-separated condition bit indices 0..{TOTAL_CONDITIONS - 1} to sweep "
            f"(default: all {TOTAL_CONDITIONS} implemented conditions)"
        ),
    )
    sweep.add_argument(
        "--sl",
        type=int,
        default=None,
        dest="stop_loss_paisa",
        help="Stop-loss in paisa (default none)",
    )
    sweep.add_argument(
        "--tp",
        type=int,
        default=None,
        dest="take_profit_paisa",
        help="Take-profit in paisa (default none)",
    )
    sweep.add_argument("--top", type=int, default=20, help="Print the top-N combos (default 20)")
    sweep.add_argument(
        "--oos",
        action="store_true",
        dest="validate_oos",
        help=(
            "Append an out-of-sample check: re-run each shown combo on train vs unseen "
            "test days — did the edge HOLD or was it overfit? (needs >= 2 trading days)"
        ),
    )
    sweep.add_argument(
        "--oos-only",
        action="store_true",
        dest="oos_only",
        help=(
            "Show ONLY combos whose edge HELD out-of-sample — filter out the overfit "
            "ones, so the board is the genuinely-robust combos (needs >= 2 trading days)"
        ),
    )
    sweep.add_argument(
        "--sort-by",
        default="composite",
        choices=list(SORT_KEYS),
        dest="sort_by",
        help=(
            "Leaderboard order: composite (default, anti-overfit) or raw pnl / win / "
            "pf to put the max-profit / max-win-rate / best-PF combo on top"
        ),
    )
    sweep.add_argument(
        "--min-hits",
        type=int,
        default=None,
        dest="min_hits",
        help="Stage-1B prune threshold (default = the MIN_TRADES gate); 0 disables",
    )
    sweep.add_argument(
        "--catalog",
        type=Path,
        default=None,
        dest="catalog_path",
        help="Catalog DuckDB path (default <lake-root>/catalog.duckdb)",
    )
    sweep.add_argument(
        "--lake-root",
        type=Path,
        default=None,
        help="Lake root (default $BRUTEX_LAKE_ROOT or ~/.brutex/lake)",
    )
    sweep.add_argument(
        "--allow-out-of-scope",
        action="store_true",
        dest="allow_out_of_scope",
        help=(
            "RESEARCH ONLY: proceed even when the symbol is outside the brute-force "
            "engine scope (NIFTY/BANKNIFTY/SENSEX spot + NIFTY futures, CLAUDE.md "
            "§2 Rule 3 / ADR-0012) — DEC-ENGINE-SCOPE-GUARD-001"
        ),
    )

    # ── sweep-all (DEC-SWEEP-ORCHESTRATOR-001) — full multi-parameter grid ──
    sweep_all = sub.add_parser(
        "sweep-all",
        help=(
            "Full multi-parameter grid sweep (instrument × timeframe × direction × K "
            "× SL × TP × proximity) → persist every ranked finalist to Parquet"
        ),
    )
    _add_sweep_grid_args(sweep_all)
    # Off-heap dedup-store knobs (R2-SL-F4-F5), threaded into SweepCache.open;
    # unset (None) keeps the env-var > module-default resolution
    # (BRUTEX_SWEEP_CACHE_MAP_MB / BRUTEX_SWEEP_CACHE_BLOOM_CAPACITY — one env
    # knob can still tune the daemon path, which has no flag yet).
    sweep_all.add_argument(
        "--sweep-cache-map-mb",
        type=int,
        default=None,
        dest="sweep_cache_map_mb",
        help=(
            "LMDB memory-map ceiling (MiB) for the sweep dedup cache "
            "(default: BRUTEX_SWEEP_CACHE_MAP_MB or the module default; raise it "
            "when a run aborts with an 'LMDB membership map full' error)"
        ),
    )
    sweep_all.add_argument(
        "--sweep-cache-bloom-capacity",
        type=int,
        default=None,
        dest="sweep_cache_bloom_capacity",
        help=(
            "Tuple-dedup Bloom capacity for the sweep cache (default: "
            "BRUTEX_SWEEP_CACHE_BLOOM_CAPACITY or the module default; raise it "
            "when a >capacity resume is refused FAIL_CLOSED with "
            "SweepCacheCapacityError)"
        ),
    )
    sweep_all.add_argument(
        "--acknowledge-sla-overrun",
        action="store_true",
        dest="acknowledge_sla_overrun",
        help=(
            "Proceed even when the projected wall-clock (tuple_count × an "
            "UNBENCHMARKED per-tuple estimate, override via "
            "BRUTEX_SWEEP_PER_TUPLE_SECONDS) exceeds the CLAUDE.md §8 2-4-day "
            "envelope. Default: REFUSE with the honest projection "
            "(DEC-SLA-LAUNCH-GUARD-001); acknowledging proceeds with a LOUD "
            "warning. Manual sweep-all only — the unattended daemon never "
            "acknowledges an overrun"
        ),
    )

    # ── sweep-daemon (DEC-SWEEP-SCHEDULER-001) — scheduled/daemon sweep runner ──
    # The AUTOMATIC twin of `sweep-all` (CLAUDE.md §11 manual⇄automatic duality):
    # runs the EXACT same grid pipeline on a cadence (intended: nightly, after the
    # incremental Groww pull), crash-safe auto-resume via the SAME checkpoint, BLAKE3
    # audit-logged per cycle. `--once` runs exactly one cycle on demand (manual).
    sweep_daemon = sub.add_parser(
        "sweep-daemon",
        help=(
            "Scheduled/daemon sweep runner (AUTOMATIC twin of sweep-all): run the full "
            "grid on a cadence with crash-safe auto-resume; --once runs one cycle now"
        ),
    )
    _add_sweep_grid_args(sweep_daemon)
    sweep_daemon.add_argument(
        "--schedule",
        default="daily@02:30",
        help=(
            "Cadence: an interval like '24h'/'90m'/'@hourly', or a daily wall-clock "
            "time like 'daily@02:30'/'02:30'/'@daily' (default daily@02:30, IST)"
        ),
    )
    sweep_daemon.add_argument(
        "--once",
        action="store_true",
        help="Run exactly ONE sweep cycle immediately, then exit (the manual on-demand trigger)",
    )
    sweep_daemon.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        dest="max_cycles",
        help="Stop the daemon after this many cycles (default: run forever until killed)",
    )
    sweep_daemon.add_argument(
        "--tz-offset-minutes",
        type=int,
        default=None,
        dest="tz_offset_minutes",
        help="Timezone offset (minutes) for a daily --schedule (default: IST, +330)",
    )

    # ── supervisor (DEC-SUPERVISOR-AUTORESTART-001) — auto-relaunch the sweep daemon ──
    # Closes the ONE real §11 AUTOMATED gap the watchdogs leave open: the watchdogs
    # DETECT a crash, but nothing AUTO-RELAUNCHES the process after a hard crash.
    # This spawns `brutex sweep-daemon` as a CHILD, monitors it, and relaunches it
    # on an UNEXPECTED exit (non-zero / signal death) with exponential backoff + a
    # max-restart cap + a reset-after-stable-uptime window; a clean exit or an
    # operator SIGTERM/SIGINT stops it (no relaunch). Each restart is audit-logged.
    # Checkpoint auto-resume makes each relaunch resume bit-identically.
    supervisor = sub.add_parser(
        "supervisor",
        help=(
            "Process supervisor: run `brutex sweep-daemon` as a child and AUTO-RELAUNCH "
            "it on an unexpected crash (backoff + cap); clean exit / SIGTERM stops it"
        ),
    )
    supervisor.add_argument(
        "child_args",
        nargs=argparse.REMAINDER,
        help=(
            "Arguments passed verbatim to `brutex sweep-daemon` (everything after `--`), "
            "e.g. `brutex supervisor -- --schedule daily@02:30`"
        ),
    )
    supervisor.add_argument(
        "--backoff-base-seconds",
        type=float,
        default=None,
        dest="backoff_base_seconds",
        help="Delay (s) before the first relaunch (default: 1.0)",
    )
    supervisor.add_argument(
        "--backoff-factor",
        type=float,
        default=None,
        dest="backoff_factor",
        help="Backoff multiplier per consecutive crash (default: 2.0)",
    )
    supervisor.add_argument(
        "--backoff-cap-seconds",
        type=float,
        default=None,
        dest="backoff_cap_seconds",
        help="Ceiling (s) the backoff can never exceed (default: 300.0)",
    )
    supervisor.add_argument(
        "--reset-after-uptime-seconds",
        type=float,
        default=None,
        dest="reset_after_uptime_seconds",
        help="A child up longer than this (s) before dying resets the backoff (default: 60.0)",
    )
    supervisor.add_argument(
        "--max-restarts",
        type=int,
        default=None,
        dest="supervisor_max_restarts",
        help="Give up loudly after this many consecutive crashes (default: 10)",
    )
    supervisor.add_argument(
        "--shutdown-grace-seconds",
        type=float,
        default=None,
        dest="shutdown_grace_seconds",
        help="Seconds to wait after SIGTERM before SIGKILL on an operator stop (default: 10.0)",
    )
    supervisor.add_argument(
        "--lake-root",
        type=Path,
        default=None,
        help="Lake root (for the supervisor audit log path; default: env/CWD lake)",
    )

    # ── results (DEC-SWEEP-WINNERS-BY-NAME-001) — read back a sweep results tree ──
    from brutex.engine.results_report import DEFAULT_SORT_BY, SORT_ALIASES

    results = sub.add_parser(
        "results",
        help="Read back a persisted sweep results tree (winning combos BY NAME)",
    )
    results_sub = results.add_subparsers(dest="results_command", required=True)
    results_show = results_sub.add_parser(
        "show",
        help="Render the ranked winners leaderboard (condition NAMES + metrics + gate)",
    )
    results_show.add_argument(
        "--results-root",
        type=Path,
        required=True,
        dest="results_root",
        help="Directory of per-tuple Parquet results (the sweep-all --results-root)",
    )
    results_show.add_argument(
        "--top", type=int, default=20, help="Show the top-N winners (default 20)"
    )
    results_show.add_argument(
        "--sort-by",
        default=DEFAULT_SORT_BY,
        choices=sorted(SORT_ALIASES),
        dest="sort_by",
        help=f"Leaderboard sort key (default {DEFAULT_SORT_BY})",
    )

    # ── options-sim (DEC-OPTIONS-EXECUTOR-001) — price a winning spot run as options ──
    options_sim = sub.add_parser(
        "options-sim",
        help="Materialize + price a winning spot run (run-id) into options PnL per strike rule",
    )
    options_sim.add_argument(
        "run_id", help="The winning spot run-id (BLAKE3 hex from the results tree)"
    )
    options_sim.add_argument(
        "--results-root",
        type=Path,
        required=True,
        dest="results_root",
        help="Directory of per-tuple Parquet sweep results (the sweep-all --results-root)",
    )
    options_sim.add_argument(
        "--start", required=True, help="Replay window start (YYYY-MM-DD or ISO)"
    )
    options_sim.add_argument("--end", required=True, help="Replay window end (YYYY-MM-DD or ISO)")
    options_sim.add_argument(
        "--lake-root",
        type=Path,
        default=None,
        help="Lake root (default $BRUTEX_LAKE_ROOT or ~/.brutex/lake)",
    )
    options_sim.add_argument(
        "--catalog",
        type=Path,
        default=None,
        dest="catalog_path",
        help="Catalog DuckDB path (default <lake-root>/catalog.duckdb)",
    )
    options_sim.add_argument(
        "--exchange", default="NSE", help="Spot exchange for the signal replay (default NSE)"
    )
    options_sim.add_argument(
        "--segment", default="IDX_I", help="Spot segment for the signal replay (default IDX_I)"
    )
    options_sim.add_argument(
        "--expiry",
        default="weekly",
        choices=("weekly", "monthly"),
        help="Option expiry cycle to trade (default weekly; falls back to monthly when none)",
    )
    options_sim.add_argument(
        "--broker", default="groww", choices=("groww", "zerodha"), help="Brokerage rate card"
    )

    # ── cascade (DEC-CASCADE-WIRE-001) — manual Stage-4 cascade re-run ──
    cascade = sub.add_parser(
        "cascade",
        help=(
            "Re-run the Stage-4 nine-filter cascade on a finished sweep's top-K "
            "winners (the MANUAL path; sweep-all also runs it automatically)"
        ),
    )
    cascade.add_argument(
        "run_id",
        nargs="?",
        default=None,
        help=(
            "Optional sweep run-id (grid identity) to validate against the "
            "manifest the sweep wrote; omit to cascade whatever is under --results-root"
        ),
    )
    cascade.add_argument(
        "--results-root",
        type=Path,
        default=None,
        dest="results_root",
        help="Directory of the sweep's per-tuple results (default <lake-root>/sweeps)",
    )
    cascade.add_argument(
        "--catalog",
        type=Path,
        default=None,
        dest="catalog_path",
        help="Catalog DuckDB path (default <lake-root>/catalog.duckdb)",
    )
    cascade.add_argument(
        "--lake-root",
        type=Path,
        default=None,
        help="Lake root (default $BRUTEX_LAKE_ROOT or ~/.brutex/lake)",
    )

    # ── verify-lookahead (DEC-LOOKAHEAD-BARRIER-001) — manual P-06 recheck ──
    verify_la = sub.add_parser(
        "verify-lookahead",
        help=(
            "MANUAL feed-ordering + cursor-invariant recheck: walk a bounded "
            "lake slice forward through the look-ahead barrier and report "
            "pass/fail. Confirms forward index order + the advance/access cursor "
            "invariant only; it does not run a strategy, so a well-formed slice "
            "always passes (strategy look-ahead is guarded by "
            "ReplayFeed.ensure_no_lookahead)"
        ),
    )
    verify_la.add_argument(
        "run_id",
        nargs="?",
        default=None,
        help="Optional run/slice label, echoed into the verdict line (informational)",
    )
    verify_la.add_argument(
        "--lake-root",
        type=Path,
        default=None,
        help="Lake root (default $BRUTEX_LAKE_ROOT or ~/.brutex/lake)",
    )
    verify_la.add_argument(
        "--catalog",
        type=Path,
        default=None,
        dest="catalog_path",
        help="Catalog DuckDB path (default <lake-root>/catalog.duckdb)",
    )
    verify_la.add_argument(
        "--exchange", default="NSE", help="Lake partition exchange (default NSE)"
    )
    verify_la.add_argument(
        "--segment", default="INDEX", help="Lake partition segment (default INDEX)"
    )
    verify_la.add_argument("--symbol", required=True, help="Canonical symbol, e.g. NIFTY")
    verify_la.add_argument("--interval", default="1minute", help="Bar interval (default 1minute)")
    verify_la.add_argument("--start", required=True, help="Slice start date/ISO (UTC)")
    verify_la.add_argument("--end", required=True, help="Slice end date/ISO (UTC)")

    # ── sweep-verify (DEC-SWEEP-VERIFY-001) — the results-tree doctor ──
    sweep_verify = sub.add_parser(
        "sweep-verify",
        help=(
            "Verify a sweep results tree: Tier-1 structural pairing + Tier-2 "
            "row-identity re-derivation (bars-free; detects the #650 top_k "
            "inflation), optional Tier-3 --recount shadow re-sweep byte-diff. "
            "REPORT-ONLY — never modifies the tree. Pass the SAME grid flags "
            "the sweep ran with."
        ),
    )
    _add_sweep_grid_args(sweep_verify)
    sweep_verify.add_argument(
        "--indicator-version",
        default=None,
        dest="indicator_version",
        help="Override the tree's recorded indicator version (default: read from the audit log)",
    )
    sweep_verify.add_argument(
        "--code-commit-sha",
        default=None,
        dest="code_commit_sha",
        help="Override the tree's recorded code commit SHA (default: read from the audit log)",
    )
    sweep_verify.add_argument(
        "--recount",
        action="store_true",
        help=(
            "Tier 3: shadow re-sweep the grid into a scratch root and byte-diff "
            "(costs a full sweep; an oracle ONLY under a matched recorded context)"
        ),
    )
    sweep_verify.add_argument(
        "--scratch-root",
        type=Path,
        default=None,
        dest="scratch_root",
        help="Scratch root for --recount (default: a fresh temp directory; NEVER the tree itself)",
    )
    sweep_verify.add_argument(
        "--allow-context-mismatch",
        action="store_true",
        dest="allow_context_mismatch",
        help=(
            "Run --recount even when the recorded context mismatches the current "
            "environment — divergence then reports INCONCLUSIVE, never CORRUPT"
        ),
    )
    sweep_verify.add_argument(
        "--emit-heal-plan",
        action="store_true",
        dest="emit_heal_plan",
        help=(
            "With --recount under a MATCHED context: write the structured heal "
            "plan (frozen evidence hashes) to <root>/heal/ for `brutex sweep-heal` "
            "(DEC-SWEEP-HEAL-001 condition C1 — heal evidence is never re-typed flags)"
        ),
    )

    # ── sweep-heal (DEC-SWEEP-HEAL-001) — quarantine, never delete ──
    sweep_heal = sub.add_parser(
        "sweep-heal",
        help=(
            "Quarantine the tuples a sweep-verify --recount --emit-heal-plan "
            "condemned, then wipe the checkpoint + dedup-cache so the next "
            "sweep-all redoes ONLY them (intact tuples sentinel-short-circuit). "
            "DRY-RUN by default; --execute to act. Never deletes — artifacts "
            "move to <root>/quarantine/."
        ),
    )
    sweep_heal.add_argument("plan", type=Path, help="The heal plan JSON the doctor emitted")
    sweep_heal.add_argument(
        "--lake-root",
        type=Path,
        default=None,
        help="Lake root (default $BRUTEX_LAKE_ROOT or ~/.brutex/lake)",
    )
    sweep_heal.add_argument(
        "--results-root",
        type=Path,
        default=None,
        dest="results_root",
        help="Results root (default <lake-root>/sweeps)",
    )
    sweep_heal.add_argument(
        "--execute",
        action="store_true",
        help="Actually quarantine + wipe (default: dry-run preview only)",
    )
    sweep_heal.add_argument(
        "--allow-mass-quarantine",
        action="store_true",
        dest="allow_mass_quarantine",
        help=(
            "Override the >=50%% refusal threshold (C7 — a venv/library drift can "
            "byte-diverge EVERYTHING under a 'matched' context; be sure first)"
        ),
    )

    # ── nightly (DEC-SWEEP-NIGHTLY-AUTOMATION-001) — zero-click chain ──
    # COLLISION → DISK → BACKUP → PULL → PREBUILD → SWEEP over EXISTING public
    # entry points; R50 backup/restore; launchd scheduling with mandatory
    # post-install verification. Cron-visible exit codes like enrich-greeks.
    nightly = sub.add_parser(
        "nightly",
        help=(
            "Zero-click nightly chain (collision → disk → backup → pull → "
            "prebuild → sweep) + backup/restore + launchd scheduling "
            "(DEC-SWEEP-NIGHTLY-AUTOMATION-001)"
        ),
    )
    nightly_sub = nightly.add_subparsers(dest="nightly_command", required=True)

    n_run = nightly_sub.add_parser(
        "run",
        help=(
            "Run the whole chain once (launchd/cron entry point). EXIT_OK iff "
            "every attempted link succeeded and no tripwire fired; a SUSPECT "
            "run exits 2 (cron-red)"
        ),
    )
    n_run.add_argument(
        "--backup-root",
        type=Path,
        default=None,
        dest="backup_root",
        help="Snapshot destination (default $BRUTEX_NIGHTLY_BACKUP_ROOT else ~/.brutex/backups)",
    )
    n_run.add_argument(
        "--keep",
        type=int,
        default=None,
        help="Sealed snapshots to retain (default $BRUTEX_NIGHTLY_BACKUP_KEEP else 7; >= 1)",
    )
    n_run.add_argument(
        "--from-date",
        default=None,
        dest="from_date",
        help="Spot-pull scope start YYYY-MM-DD (blank = the 2020-01-01 pull default)",
    )
    n_run.add_argument(
        "--to-date",
        default=None,
        dest="to_date",
        help="Spot-pull scope end YYYY-MM-DD (blank = previous IST day)",
    )
    n_run.add_argument(
        "--skip-backup",
        action="store_true",
        dest="skip_backup",
        help="Skip the BACKUP link (loud SKIPPED + Telegram nag — stores unprotected tonight)",
    )
    n_run.add_argument(
        "--skip-sweep",
        action="store_true",
        dest="skip_sweep",
        help="Skip the SWEEP link (honest SKIPPED)",
    )
    n_run.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Probes + inventory + plan + report ONLY — takes NO locks, mutates NOTHING",
    )
    n_run.add_argument(
        "--acknowledge-sla-overrun",
        action="store_true",
        dest="acknowledge_sla_overrun",
        help="HARD-REFUSED on nightly run — the unattended chain never overrides safety gates",
    )
    _add_sweep_grid_args(n_run, dates_required=False)

    n_backup = nightly_sub.add_parser(
        "backup",
        help=(
            "One-shot COLLISION + DISK + BACKUP links (same locks): seal one "
            "snapshot of the result-critical stores + prune retention"
        ),
    )
    n_backup.add_argument("--lake-root", type=Path, default=None, help="Lake root")
    n_backup.add_argument(
        "--results-root",
        type=Path,
        default=None,
        dest="results_root",
        help="Results root (default <lake-root>/sweeps)",
    )
    n_backup.add_argument(
        "--backup-root",
        type=Path,
        default=None,
        dest="backup_root",
        help="Snapshot destination (default $BRUTEX_NIGHTLY_BACKUP_ROOT else ~/.brutex/backups)",
    )
    n_backup.add_argument("--keep", type=int, default=None, help="Sealed snapshots to retain")

    n_restore = nightly_sub.add_parser(
        "restore",
        help=(
            "Verify-EVERYTHING-then-write restore of one sealed snapshot "
            "(deltas cleared, ledger.parquet strictly LAST; --yes gated)"
        ),
    )
    n_restore.add_argument(
        "--snapshot", type=Path, required=True, help="The sealed snapshot directory"
    )
    n_restore.add_argument("--lake-root", type=Path, default=None, help="Lake root")
    n_restore.add_argument(
        "--results-root",
        type=Path,
        default=None,
        dest="results_root",
        help="Results root (default <lake-root>/sweeps)",
    )
    n_restore.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Print the ordered plan + verify verdict; no locks, no writes",
    )
    n_restore.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the live restore (the 6 a.m. safety gate — refused without it)",
    )

    n_install = nightly_sub.add_parser(
        "install",
        help=(
            "One-time launchd bootstrap (macOS): write + load the LaunchAgent "
            "and VERIFY launchctl lists it (installed-but-unverified FAILS)"
        ),
    )
    n_install.add_argument("--at", default="01:30", help="Local fire time HH:MM (default 01:30)")
    n_install.add_argument(
        "--label", default="com.brutex.nightly", help="launchd label (default com.brutex.nightly)"
    )
    n_install.add_argument(
        "--plist-dir",
        type=Path,
        default=None,
        dest="plist_dir",
        help="LaunchAgents dir (default ~/Library/LaunchAgents)",
    )
    n_install.add_argument(
        "--lake-root",
        type=Path,
        default=None,
        help="Baked into the plist EnvironmentVariables when set",
    )
    n_install.add_argument(
        "--backup-root",
        type=Path,
        default=None,
        dest="backup_root",
        help="Baked into the plist EnvironmentVariables when set",
    )

    n_uninstall = nightly_sub.add_parser(
        "uninstall", help="Unload + remove the LaunchAgent, verifying it is no longer listed"
    )
    n_uninstall.add_argument("--label", default="com.brutex.nightly", help="launchd label")
    n_uninstall.add_argument(
        "--plist-dir",
        type=Path,
        default=None,
        dest="plist_dir",
        help="LaunchAgents dir (default ~/Library/LaunchAgents)",
    )

    n_status = nightly_sub.add_parser(
        "status",
        help=(
            "The 6 a.m. command: schedule presence, launchctl listing, last "
            "report, latest snapshot — honest lines even when nothing ran"
        ),
    )
    n_status.add_argument("--lake-root", type=Path, default=None, help="Lake root")
    n_status.add_argument(
        "--results-root",
        type=Path,
        default=None,
        dest="results_root",
        help="Results root (default <lake-root>/sweeps)",
    )
    n_status.add_argument("--label", default="com.brutex.nightly", help="launchd label")
    n_status.add_argument(
        "--plist-dir",
        type=Path,
        default=None,
        dest="plist_dir",
        help="LaunchAgents dir (default ~/Library/LaunchAgents)",
    )

    return parser


# ────────────────────────────────────────────────────────────────────────
# Subcommand handlers
# ────────────────────────────────────────────────────────────────────────


def cmd_version() -> int:
    """Print version and exit."""
    print(f"brutex {__version__}")
    return EXIT_OK


def cmd_status(run_id: str | None) -> int:
    """Print the current pipeline status from the runtime file."""
    try:
        runtime = read_runtime_file()
    # read_runtime_file wraps unreadable-file / invalid-JSON / missing-field
    # failures in RuntimeFileError; a structurally-valid but WRONG-TYPED file
    # leaks raw from RuntimeInfo.from_json (int("abc") → ValueError,
    # int(None) → TypeError, env_snapshot-not-a-dict → AttributeError) — all
    # caught here so a hand-corrupted runtime.json is a clean user error,
    # never a traceback (DEC-CLI-USER-ERROR-HYGIENE-001). The file is NEVER
    # auto-deleted — the operator inspects/removes it.
    except (RuntimeFileError, ValueError, TypeError, AttributeError) as exc:
        print(
            f"[brutex] runtime file corrupt/unreadable: {runtime_file_path()} ({exc}) — "
            "inspect or remove that file to clear the stale state (it is never "
            "auto-deleted), then re-run",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR
    if runtime is None:
        print("[brutex] no runtime file (no pipeline running)")
        return EXIT_OK
    if run_id is not None and runtime.run_id != run_id:
        print(f"[brutex] no active run for run_id={run_id}")
        return EXIT_OK
    print(f"pid       : {runtime.pid}")
    print(f"run_id    : {runtime.run_id}")
    print(f"started   : {runtime.started_at}")
    print(f"phase     : {runtime.phase}")
    return EXIT_OK


def cmd_audit_verify(audit_file: Path, audit_format: str = "auto") -> int:
    """Verify the hash chain of an audit-log file (orchestrator / sweep / auto).

    Auto-detects the chain format (SHA-256 ingest vs BLAKE3 supervisor) by
    default and verifies EITHER — closing the operability gap where the
    supervisor chain falsely reported as corrupt (DEC-AUDIT-VERIFY-UNIFY-001).
    Exit codes are preserved: missing file / malformed line → user error;
    a real hash-chain integrity break → internal error.
    """
    # Lazy import: the unified verifier pulls the Rust-backed engine package,
    # so non-audit commands (e.g. `brutex version`) stay wheel-free.
    from brutex.audit_verify import verify_audit_file
    from brutex.engine.sweep_audit import SweepAuditChainCorruption, SweepAuditError
    from brutex.orchestrator.audit import AuditChainCorruption, AuditError

    if not audit_file.exists():
        print(f"[brutex] audit file not found: {audit_file}", file=sys.stderr)
        return EXIT_USER_ERROR
    try:
        result = verify_audit_file(audit_file, audit_format)
    # Corruption subclasses MUST be caught before their base error classes
    # (AuditChainCorruption ⊂ AuditError; SweepAuditChainCorruption ⊂ SweepAuditError).
    except (AuditChainCorruption, SweepAuditChainCorruption) as exc:
        print(f"[brutex] AUDIT CHAIN CORRUPT: {exc}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR
    except (AuditError, SweepAuditError) as exc:
        print(f"[brutex] audit file unreadable: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    label = result.format.value if result.format is not None else "empty"
    print(f"[brutex] audit chain OK [{label}] — {result.entry_count} entries verified")
    return EXIT_OK


def cmd_catalog_reconcile(lake_root: Path | None) -> int:
    """Rebuild the DuckDB catalog ``partitions`` table from the Parquet lake.

    Repairs a lake whose partitions were written but never registered (every
    production ingest sink historically passed ``catalog=None`` — DEC-CATALOG-
    RECONCILE-WIRE-001), so ``brutex sweep-all`` can read the bars back with no
    re-pull. ``sweep-all`` also runs this automatically for the swept
    coordinates; this command reconciles the WHOLE lake on demand.
    """
    from brutex.data.catalog import Catalog
    from brutex.data.catalog_reconcile import reconcile_catalog_from_lake

    resolved = _resolve_lake_root(lake_root)
    # Refuse a typo'd/nonexistent lake root BEFORE Catalog.open can mkdir a
    # fresh empty lake and report an honest-looking "partitions=0". The
    # catalog FILE itself is deliberately NOT gated — reconcile's purpose
    # includes rebuilding a deleted catalog.duckdb inside a REAL lake
    # (DEC-CLI-USER-ERROR-HYGIENE-001).
    _require_lake_dir(resolved, command="catalog reconcile")
    with Catalog.open(resolved / "catalog.duckdb") as cat:
        result = reconcile_catalog_from_lake(lake_root=resolved, catalog=cat)
    print(
        f"[brutex] catalog reconcile — partitions={result.partitions_reconciled} "
        f"bars={result.bars_total} skipped={result.skipped} (lake={resolved})"
    )
    return EXIT_OK


def cmd_lake_audit_unique(
    *,
    lake_root: Path | None,
    as_json: bool,
    exchange: str | None,
    segment: str | None,
    symbol: str | None,
    interval: str | None,
    no_options: bool,
) -> int:
    """READ-ONLY lake uniqueness census — ``brutex lake audit-unique`` (DEC-LAKE-HEAL-001).

    Runs the ONE bounded invariant scanner
    (:func:`brutex.data.lake_invariants.scan_lake_invariants`) over the lake —
    NEVER heals; the command is read-only by contract — then folds the
    per-segment census and prints it as a human table or ``--json`` (the SAME
    dict shape ``GET /api/v1/universe/lake-uniqueness`` serves). Exit codes
    (the ``cmd_audit_verify`` semantics): :data:`EXIT_OK` (0) clean,
    :data:`EXIT_USER_ERROR` (1) re-documented here as "poisoned partitions
    found", :data:`EXIT_INTERNAL_ERROR` (2) the scan itself errored.

    HONEST cost (Golden Rule #10): the scan is O(files-in-scope) with one
    key-column read per file (NOT O(1)) — bound it with the coordinate
    filters on a deep lake.
    """
    import json

    from brutex.data.lake_heal import (
        load_heal_stats,
        load_last_sweep,
        resolve_lake_heal_mode,
    )
    from brutex.data.lake_invariants import (
        OPTIONS_SEGMENT,
        build_lake_census,
        census_to_dict,
        scan_lake_invariants,
    )

    resolved = _resolve_lake_root(lake_root)
    try:
        report = scan_lake_invariants(
            resolved,
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            interval=interval,
            include_options=not no_options,
        )
        census = build_lake_census(report)
        payload = census_to_dict(
            census,
            mode=resolve_lake_heal_mode(),
            stats=load_heal_stats(resolved),
            last_sweep=load_last_sweep(resolved),
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught — exit-code contract
        print(f"[brutex] lake audit-unique scan errored: {exc}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"lake audit-unique — {resolved}")
        print(
            f"{'segment':<9}{'files':>7}{'clean':>7}{'dup':>6}"
            f"{'foreign':>9}{'overlap':>9}{'drift-dirs':>12}"
        )
        for row in census.segments:
            if row.segment == OPTIONS_SEGMENT:
                # Foreign/overlap/drift are OUT of the options-tree census scope
                # (a named DEC-LAKE-HEAL-001 follow-up) — shown as "-", never a fake 0.
                foreign, overlap, drift = "-", "-", "-"
            else:
                foreign = str(row.foreign_files)
                overlap = str(row.overlap_files)
                drift = str(row.drift_dirs)
            print(
                f"{row.segment:<9}{row.files:>7}{row.clean:>7}{row.dup_files:>6}"
                f"{foreign:>9}{overlap:>9}{drift:>12}"
            )
        label, _, rest = census.engine_detail.partition(": ")
        verdict = "CLEAN" if census.engine_clean else "POISONED"
        print(f"engine indexes ({label.replace('/', ' / ')}): {verdict} — {rest}")
        last_heal = payload["last_heal_utc"] or "never"
        print(
            f"mode={payload['mode']}  healed_total={payload['healed_total']}  "
            f"quarantined_files_total={payload['quarantined_files_total']}  "
            f"last_heal={last_heal}"
        )
    return EXIT_OK if census.findings_total == 0 else EXIT_USER_ERROR


def cmd_reopen_skipped(
    *,
    lake_root: Path | None,
    symbol: str | None,
    market: str | None,
    limit: int | None,
) -> int:
    """Re-open ``SKIPPED`` (settled no-data) ledger windows for re-verification.

    DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001 — the operator "re-open windows" action
    the divergence register's R2 fix demands: a ``SKIPPED`` settle is no longer
    permanent-forever. Loads the durable window ledger, demotes every matching
    ``SKIPPED`` row to a retryable ``FAILED`` (``empty_streak`` reset — the next
    pull re-fetches it with the FULL fresh confirmation budget; the ``REOPENED``
    marker makes those re-checks breaker-exempt), and persists the ledger
    atomically. DATA-SAFE: stored lake bars are untouched; the idempotent
    keep-first sink absorbs the re-fetch (Golden Rule #7). ``--symbol`` /
    ``--market`` scope the selection; ``--limit`` bounds it (the bounded-sample
    re-verify).

    **Single-flight guard (minor M-2):** the load→mutate→save runs INSIDE the
    account-wide pull single-flight lock
    (:func:`~brutex.adapters.groww.pull_singleflight.pull_singleflight_lock` —
    the same ``flock`` a real pull holds for its whole duration). While a pull
    is running the non-blocking acquire fails and this command REFUSES loudly
    (exit ``EXIT_USER_ERROR``) instead of racing the pull's own throttled ledger
    flushes — a mid-pull reopen would be silently overwritten by the pull's next
    ledger persist (last-writer-wins on the whole file). The lock is held only
    for the fast local mutation (ms), so a pull starting in that instant simply
    retries; no vendor call is ever made here.
    """
    from brutex.adapters.groww.pull_singleflight import (
        PullAlreadyRunningError,
        pull_singleflight_lock,
    )
    from brutex.data.window_ledger_store import (
        ledger_path,
        list_ledger_deltas,
        load_window_ledger,
        save_window_ledger,
    )

    resolved = _resolve_lake_root(lake_root)
    # Delta-aware presence gate (P2 fix-round, DEC-REOPEN-DELTA-AWARE-001): a
    # base-less lake HOLDING deltas (a first pull's mid-flight crash checkpoint,
    # DEC-LEDGER-DELTA-FLUSH-001) is a LIVE ledger — load_or_migrate treats it
    # exactly so — hence it must be operable here, not refused as "no ledger".
    if not (ledger_path(resolved).is_file() or list_ledger_deltas(resolved)):
        print(
            f"[brutex] no window ledger found at {ledger_path(resolved)} — nothing to re-open "
            "(run a pull first, or pass the right --lake-root).",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR
    try:
        with pull_singleflight_lock(resolved):
            ledger = load_window_ledger(resolved)
            reopened = ledger.reopen_skipped(market=market, security_id=symbol, limit=limit)
            if reopened:
                save_window_ledger(ledger, resolved)
    except PullAlreadyRunningError as exc:
        print(
            f"[brutex] reopen-skipped REFUSED — a pull is currently running against this lake "
            f"({exc}). Re-opening windows mid-pull would race the pull's own ledger writes "
            "(the pull's next ledger persist would silently overwrite the re-open). Wait for "
            "the pull to finish (or stop it), then re-run.",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR
    if reopened == 0:
        print(
            "[brutex] reopen-skipped — 0 windows matched (nothing was SKIPPED under "
            f"symbol={symbol or 'any'} market={market or 'any'}); ledger unchanged."
        )
        return EXIT_OK
    print(
        f"[brutex] reopen-skipped — re-opened {reopened} window(s) "
        f"(symbol={symbol or 'any'} market={market or 'any'} limit={limit or 'all'}). "
        "Re-pull to re-verify them; genuinely-empty windows re-settle honestly, "
        "throttle-poisoned ones recover their data (re-opened windows are "
        "breaker-exempt deliberate re-checks — the re-pull cannot re-trip on them)."
    )
    return EXIT_OK


def cmd_detect_session_drift(*, lake_root: Path | None, month: str) -> int:
    """Run the observed-sessions drift reconciler over ONE fully-past month.

    DEC-CALENDAR-OBSERVED-SESSIONS-001 — the production invocation surface for
    :func:`brutex.calendars.observed_sessions.detect_session_drift` (the
    reconciler is deliberately MANUALLY-invoked-only: the operator scans a month
    after its pulls have settled, reviews the printed evidence, and the labeled
    sidecar remains the human-auditable escape hatch). Prints the verdict —
    flagged / vetoed / no-evidence days + both cap refusals — and whether the
    overlay sidecar was rewritten (diff-based: an unchanged month is a no-op; a
    month whose stale entries the lake now refutes is self-healed).
    """
    from brutex.calendars.observed_sessions import (
        ObservedSessionsError,
        detect_session_drift,
    )

    try:
        month_date = date.fromisoformat(f"{month}-01")
    except ValueError:
        print(
            f"[brutex] detect-session-drift — invalid --month {month!r}; expected YYYY-MM",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR
    resolved = _resolve_lake_root(lake_root)
    try:
        report = detect_session_drift(resolved, month_date)
    except ValueError as exc:
        print(f"[brutex] detect-session-drift REFUSED — {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    except ObservedSessionsError as exc:
        print(f"[brutex] detect-session-drift FAILED — {exc}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR
    verdict = report.verdict
    flagged = ", ".join(f.day.isoformat() for f in verdict.flagged) or "none"
    vetoed = ", ".join(d.isoformat() for d in verdict.vetoed_failed) or "none"
    no_evidence = ", ".join(d.isoformat() for d in verdict.no_evidence) or "none"
    print(
        f"[brutex] detect-session-drift {month} — flagged: {flagged}; "
        f"vetoed (FAILED ledger rows): {vetoed}; no-evidence: {no_evidence}; "
        f"month-cap exceeded: {verdict.over_month_cap}; "
        f"year-cap exceeded: {verdict.over_year_cap}; "
        f"overlay {'REWRITTEN' if report.wrote_overlay else 'unchanged'} "
        f"({report.overlay_path}). Every entry is observed-not-official — the "
        "overlay never names a holiday; the official NSE circular remains the "
        "only closure for the named-holiday gap."
    )
    if verdict.over_month_cap or verdict.over_year_cap:
        return EXIT_USER_ERROR
    return EXIT_OK


def cmd_auth_set() -> int:  # pragma: no cover — interactive getpass prompts
    """Interactively prompt (no echo) for the token creds and save them.

    Reads the access token and client id via :func:`getpass.getpass` so neither
    is echoed to the terminal or recorded in shell history. The values are
    NEVER accepted as argv flags (they would leak via ``ps`` / shell
    history). Prints only the destination path + mode — never a value.
    """
    import getpass

    from brutex.config.credentials import (
        CredentialsError,
        TokenFileCredentials,
        save_token_credentials,
    )

    access_token = getpass.getpass("Access token (input hidden): ")
    client_id = getpass.getpass("Client id (input hidden): ")
    try:
        path = save_token_credentials(
            TokenFileCredentials(access_token=access_token, client_id=client_id)
        )
    except CredentialsError as exc:
        print(f"[brutex] could not save credentials: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    print(f"[brutex] credentials saved to {path} (mode 0600)")
    return EXIT_OK


def cmd_auth_status() -> int:  # pragma: no cover — thin print wrapper
    """Print whether token credentials are stored (booleans only, no values)."""
    from brutex.config.credentials import credentials_path, credentials_present

    present = credentials_present()
    print(f"[brutex] credentials: {'set' if present else 'not set'}")
    print(f"[brutex] path: {credentials_path()}")
    return EXIT_OK


def cmd_auth_clear() -> int:  # pragma: no cover — thin print wrapper
    """Delete the stored token credentials file (if any)."""
    from brutex.config.credentials import clear_token_credentials, credentials_path

    removed = clear_token_credentials()
    path = credentials_path()
    if removed:
        print(f"[brutex] credentials removed: {path}")
    else:
        print(f"[brutex] no credentials file to remove: {path}")
    return EXIT_OK


def cmd_doctor(
    audit_file: Path | None,
    audit_dir: Path | None,
    runtime_file: Path | None,
    live_subjects_file: Path | None = None,
    replay_action_filter: str = "add",
) -> int:
    """Run all doctor health checks and print the report."""
    from brutex.doctor import DEFAULT_AUDIT_DIR, DEFAULT_RUNTIME_FILE
    from brutex.doctor import main as doctor_main

    return doctor_main(
        audit_file=audit_file,
        audit_dir=audit_dir if audit_dir is not None else DEFAULT_AUDIT_DIR,
        runtime_file=runtime_file if runtime_file is not None else DEFAULT_RUNTIME_FILE,
        live_subjects_file=live_subjects_file,
        replay_action_filter=replay_action_filter,
    )


def parse_stage_selection(raw: str | None) -> frozenset[int] | None:
    """Parse the ``--stage`` comma list (``None``/blank → all stages).

    A malformed token or out-of-range stage raises :class:`ValueError` with an
    actionable message (the dispatcher maps it to a user error, never a
    traceback).
    """
    if raw is None or not raw.strip():
        return None
    stages: set[int] = set()
    for token in raw.split(","):
        text = token.strip()
        if not text.isdigit() or not 0 <= int(text) <= 13:  # noqa: PLR2004 — stage ids are 0-13
            raise ValueError(
                f"--stage takes comma-separated stage numbers 0-13, got {token.strip()!r}"
            )
        stages.add(int(text))
    return frozenset(stages)


#: The red multi-line warning printed before any ``--mint-anyway`` run
#: (kit spec §2.2 — a local mint may revoke the shared Lambda/TickVault token).
MINT_ANYWAY_WARNING: str = (
    "\x1b[31m"
    "WARNING: --mint-anyway is ACTIVE.\n"
    "  The kit may mint a REAL Groww token from this machine. Under the recorded\n"
    "  single-active-token behaviour (UNVERIFIED — treated as true) that mint can\n"
    "  REVOKE the shared token the minter Lambda and TickVault are using RIGHT NOW,\n"
    "  stalling every sibling pull until the 06:05 IST re-mint.\n"
    "  Only proceed if you know no sibling consumer is live."
    "\x1b[0m"
)


def cmd_doctor_live(args: argparse.Namespace) -> int:  # pragma: no cover — real-deps composition
    """``brutex doctor --live`` — run the Mac live-proof kit with the REAL deps.

    All logic lives in :mod:`brutex.doctor_live` (fully unit-tested with
    fakes); this wrapper only composes the real dependencies (network / git /
    boto3 / SDK — the sandbox-blind surface), prints the report, and maps a
    kit-internal error to exit code 3 (kit spec §2.6).
    """
    from brutex.doctor_live import (
        KitInternalError,
        KitOptions,
        build_real_deps,
        run_live_doctor,
    )

    try:
        stages = parse_stage_selection(args.stage)
    except ValueError as exc:
        print(f"[brutex] doctor --live error: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    options = KitOptions(
        lake_root=args.lake,
        symbol=args.symbol,
        stages=stages,
        keep_going=args.keep_going,
        depth_bisect=args.depth_bisect,
        rate_probe=args.rate_probe,
        against_lake=args.against_lake,
        json_output=args.json_output,
        mint_anyway=args.mint_anyway,
    )
    if options.mint_anyway:
        print(MINT_ANYWAY_WARNING, file=sys.stderr)
    try:
        report = run_live_doctor(build_real_deps(), options)
    except KitInternalError as exc:
        print(f"[brutex] doctor --live kit-internal error: {exc}", file=sys.stderr)
        return 3
    print(report.to_json() if options.json_output else report.format())
    return report.exit_code


def cmd_monitor(poll_interval: float, ticks: int) -> int:
    """Run the SLA + Watchdog + replay-oracle monitor loop.

    Bounded run (``ticks > 0``): execute exactly N ticks then exit 0.
    Unbounded (``ticks == 0``): run until SIGINT / Ctrl-C.

    Uses :func:`brutex.observability.standard_monitor.compose_standard_monitor`
    (DEC-STANDARD-MONITOR-001) to build the loop with env-driven config:
    Telegram callback (when ``BRUTEX_TELEGRAM_ENABLED=true``), watchdog
    timeout (``BRUTEX_WATCHDOG_TIMEOUT_SECONDS``), replay oracle path
    (``BRUTEX_AUDIT_PATH`` + ``BRUTEX_LIVE_SUBJECTS_FILE``). The
    operator pre-builds :class:`SLARule` lists in Python; this CLI ships
    with the empty default (no SLA rules) — see the spec for the next
    PR that adds a YAML loader.
    """
    from brutex.observability.standard_monitor import (
        MonitorConfig,
        compose_standard_monitor,
    )

    # CLI's --poll-interval overrides env, which overrides default.
    base_cfg = MonitorConfig.from_env()
    cfg = MonitorConfig(
        poll_interval_seconds=poll_interval,
        watchdog_timeout_seconds=base_cfg.watchdog_timeout_seconds,
        telegram_enabled=base_cfg.telegram_enabled,
        sla_rules=base_cfg.sla_rules,
        audit_path=base_cfg.audit_path,
        live_subjects_file=base_cfg.live_subjects_file,
        replay_action_filter=base_cfg.replay_action_filter,
        replay_every_n_ticks=base_cfg.replay_every_n_ticks,
    )
    loop = compose_standard_monitor(cfg)
    if ticks > 0:
        for _ in range(ticks):
            loop.tick_once()
        return 0
    loop.start()
    try:
        # Block until the daemon thread reports back via interrupt
        # (Ctrl-C). We use the standard event-wait pattern so the
        # foreground command never busy-loops.
        while loop.is_running:
            try:
                loop._thread.join(timeout=1.0)  # type: ignore[union-attr]
            except KeyboardInterrupt:
                break
    finally:
        loop.stop()
    return 0


def cmd_notify_test() -> int:  # pragma: no cover — live network + singleton + cli-omitted
    """Send a one-shot Telegram test digest (the MANUAL channel trigger).

    The hand-run counterpart to the sweep supervisor's AUTOMATIC alerts
    (watchdog-hung / rate.breach / auth.renew_failed / sweep complete —
    DEC-TELEGRAM-WIRE-001): it lets the operator confirm the Telegram channel is
    wired + reachable on demand. Reads the channel config from env; if Telegram
    is disabled or misconfigured, prints a clear hint and exits non-zero (never a
    traceback). On success it starts the singleton notifier, publishes one
    :class:`~brutex.notifications.telegram.TelegramEventKind.TEST` digest, drains
    the async queue, prints the send stats, and stops the notifier. The pure
    digest-build + best-effort publish logic lives in the 100%-tested
    :class:`brutex.notifications.sweep_alerts.SweepAlerter`.
    """
    from brutex.notifications.sweep_alerts import SweepAlerter
    from brutex.notifications.telegram import TelegramConfig, TelegramConfigError
    from brutex.notifications.wiring import get_or_start_notifier

    try:
        config = TelegramConfig.from_env()
    except TelegramConfigError as exc:
        print(f"[brutex] Telegram misconfigured: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    if not config.enabled:
        print(
            "[brutex] Telegram disabled — set BRUTEX_TELEGRAM_ENABLED=true plus "
            "BRUTEX_TELEGRAM_BOT_TOKEN + BRUTEX_TELEGRAM_CHAT_ID (see "
            "docs/runbooks/telegram-setup.md), then re-run `brutex notify test`.",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR

    notifier = get_or_start_notifier(config=config)
    if notifier is None:
        print("[brutex] Telegram could not start — see logs.", file=sys.stderr)
        return EXIT_INTERNAL_ERROR

    alerter = SweepAlerter(notifier=notifier)
    alerter.test_digest()
    notifier.drain(timeout=5.0)
    stats = notifier.stats()
    notifier.stop()
    print(
        f"[brutex] notify test → sent={stats['sent']} "
        f"dropped_on_failure={stats['dropped_on_failure']} "
        f"dropped_on_dedupe={stats['dropped_on_dedupe']}"
    )
    if stats["sent"] > 0:
        print("[brutex] ✅ test digest delivered to Telegram.")
        return EXIT_OK
    print(
        "[brutex] ❌ test digest was NOT delivered (check token / chat id / network).",
        file=sys.stderr,
    )
    return EXIT_INTERNAL_ERROR


def _autoload_instrument_master(
    store: Any, log: Any
) -> None:  # pragma: no cover - live network + server path (cli.py coverage-excluded)
    """Best-effort instrument-master autoload, run OFF the port-bind path.

    Auto-populates the keyless instrument master so /universe is never empty on a
    fresh lake (DEC-INDEX-PARSE-FIX-001), then best-effort enriches with the
    NIFTY-Total-Market constituents. The NTM fetch hits niftyindices.com, which
    bot-blocks / slow-first-bytes non-browser clients (3 x 30 s retries in
    ``nse_constituents.fetch_constituent_csv`` -- up to ~90 s), so this whole body
    runs in a background daemon thread (DEC-DASHBOARD-INSTANT-LAUNCH-001): the
    ``InstrumentStore`` read/write split (DEC-STORE-READ-WRITE-SPLIT-001) keeps
    concurrent route reads consistent against the loader's writes. A network
    failure must NEVER stop the dashboard from serving, so the broad except just
    logs and the server keeps running on whatever the store already holds.
    """
    try:
        from brutex.dashboard.auto_pull import should_reload_instrument_master
        from brutex.dashboard.instrument_loader import load_instrument_master
        from brutex.dashboard.universe_query import universe_summary

        # Lean-correct counts (DEC-INSTRUMENT-UNIVERSE-FAST-001): the IDX+EQ
        # candidates plus the SQL-derived F&O-eligible set, so an EQ is classed a
        # "stock" (the bare load_snapshot path left every equity unclassified).
        summary = universe_summary(
            store.list_index_equity_instruments(),
            fno_eligible=store.fno_eligible_underlyings(),
        )
        # Auto-heal: reload on an empty lake, missing indices, leftover FUT/CE/PE
        # legs (pre-lean), placeholder economics (lot_size=1), or stale option
        # eligibility -- the lean refresh replaces + prunes to the enriched set.
        if should_reload_instrument_master(
            index_count=summary["index_count"],
            stock_count=summary["stock_count"],
            legs_present=store.has_derivative_legs(),
            stale_economics=store.has_placeholder_stock_economics(),
            stale_eligibility=store.has_stale_option_eligibility(),
        ):
            counts = load_instrument_master(store)
            log.info(
                "instrument_master.autoloaded",
                indices=counts["index_count"],
                stocks=counts["stock_count"],
            )
        else:
            log.info(
                "instrument_master.present",
                indices=summary["index_count"],
                stocks=summary["stock_count"],
            )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        log.warning("instrument_master.autoload_skipped", error=str(exc))

    # NO auto-pull of expiries/contracts on startup (DEC-UNIVERSE-PULL-PHASED-001,
    # operator decree 2026-05-30: "remove the auto pull"). The universe pull is
    # driven by the two MANUAL /universe buttons (Fetch all expiries / contracts),
    # so nothing hits Groww until the operator clicks.
    log.info(
        "universe.autopull.disabled",
        hint="use the 'Fetch all expiries' / 'Fetch all contracts' buttons on /universe",
    )


def _start_instrument_autoload(
    store: Any, log: Any
) -> None:  # pragma: no cover - daemon thread + live network/server path
    """Run :func:`_autoload_instrument_master` in a background daemon thread.

    A dedicated seam so the slow, network-bound autoload never blocks the uvicorn
    port bind -- and so the unit tests can stub THIS to run the body synchronously
    and offline (DEC-DASHBOARD-INSTANT-LAUNCH-001).
    """
    import threading

    threading.Thread(
        target=_autoload_instrument_master,
        args=(store, log),
        name="brutex-instrument-autoload",
        daemon=True,
    ).start()


def _auto_resume_child_argv(lake_root: Path) -> list[str]:
    """Return the argv that resumes an in-progress pull for *lake_root*.

    Runs ``brutex fetch --until-clean --lake-root <lake_root>`` via the SAME
    Python interpreter (``sys.executable -m brutex``) so the resumed pull
    inherits this exact venv + install — robust whether or not the ``brutex``
    console script is on ``PATH`` (mirrors :func:`_supervisor_child_argv`).

    ``fetch --until-clean`` (NOT the legacy ``--universe`` form) is LOAD-BEARING
    (DEC-AUTO-RESUME-ARGV-FIX-001): ``--universe`` is RETIRED — :func:`cmd_fetch`
    refuses it with ``EXIT_USER_ERROR`` before any pull work (the DEC-DHAN-FINALIZE-001
    retirement notice), so a child spawned with it exits immediately having resumed
    NOTHING while the boot log still read ``verdict=resumed`` (the silent-dead-resume
    BLOCKER this fixes). ``fetch --until-clean`` is the CURRENT CLI pull entry: it
    re-runs the spot ingest over the SHARED durable window ledger in bounded
    until-clean rounds — re-fetching ONLY the unsettled (PLANNED / FAILED) windows,
    the keep-first sink no-oping the settled ones. HONEST limit (Golden Rule #10):
    ``cmd_fetch``'s live path binds ``fetch_instruments = _resolve_brute_force_instruments()``,
    which filters to ONLY the 3 brute-force INDICES (NIFTY / BANKNIFTY / SENSEX), so
    this child resumes ONLY the 3 index SPOT windows. Outstanding NON-index spot
    windows (the ~750 reference underlyings) AND all F&O contract windows are NOT
    settled by this child (the CLI has no composite-pipeline subcommand) — they
    re-trigger a bounded, once-per-boot resume every restart until they are re-pulled
    via the dashboard ``/pull-pipeline``. This is a partial (index-spot) resume, not a
    full-universe one; auto-settling the rest is the ledger's job on the next dashboard pull.

    The explicit ``--lake-root`` is LOAD-BEARING (hostile-review MAJOR): the
    dashboard resolved the ledger from *lake_root* (which may be an explicit
    ``--lake-root /data/lake`` with ``$BRUTEX_LAKE_ROOT`` unset), but a child with
    NO ``--lake-root`` would fall back to ``~/.brutex/lake`` in :func:`cmd_fetch`
    — re-pulling into the WRONG lake AND missing the single-flight lock (which is
    KEYED by lake root), so it would not contend with a real pull on the operator's
    lake (the very double-pull this feature exists to prevent). Passing the
    resolved root keeps the child on the SAME lake the ledger was read from, so the
    resume re-pulls the right windows AND shares the account-wide lock.

    ``--until-clean`` re-runs bounded rounds over the SHARED window ledger —
    re-fetching ONLY the unsettled windows (the keep-first sink no-ops the settled
    ones) — until the ledger is clean, the token dies, or the round budget is spent
    (DEC-TOKEN-PREFLIGHT-001).
    """
    return [
        sys.executable,
        "-m",
        "brutex",
        "fetch",
        "--until-clean",
        "--lake-root",
        str(lake_root),
    ]


#: Best-effort early-exit probe window (seconds) for the spawned auto-resume child
#: (DEC-AUTO-RESUME-ARGV-FIX-001): a child that dies with a usage / user error exits
#: well inside this window, so the resume verdict can read ERROR instead of a false
#: ``resumed``. Runs on the boot DAEMON thread (never the uvicorn bind path), so
#: waiting this long never blocks or delays the boot.
_AUTO_RESUME_EARLY_EXIT_PROBE_S = 2.0


def _validate_auto_resume_argv(argv: list[str]) -> None:
    """Prove *argv* dispatches to the REAL ``fetch`` pull entry — raise on ANY drift.

    The guard that makes a dead auto-resume LOUD (DEC-AUTO-RESUME-ARGV-FIX-001):
    the child argv is re-parsed through the REAL :func:`build_parser` BEFORE the
    subprocess is spawned, and rejected (``ValueError``) unless it (a) runs
    ``python -m brutex``, (b) parses cleanly, (c) dispatches to the ``fetch``
    subcommand, (d) avoids the modes that exit WITHOUT pulling (the RETIRED
    ``--universe`` refusal + the load-master-and-exit ``--instruments``), and
    (e) carries ``--until-clean`` (the bounded resume-until-done contract of
    DEC-AUTO-RESUME-ON-BOOT-001). A raise propagates into
    :func:`~brutex.orchestrator.auto_resume.maybe_resume_on_boot`'s guard, which
    logs ``auto_resume.error`` and returns the honest ``error`` verdict — never
    ``resumed`` — while the boot itself is unaffected (the never-raises contract).

    Pure over *argv* (one parser build + parse; no subprocess, no I/O beyond
    argparse's own stderr usage line on a reject — deliberately loud).
    """
    prefix = [sys.executable, "-m", "brutex"]
    if argv[: len(prefix)] != prefix:
        raise ValueError(f"auto-resume child argv must run `python -m brutex`: {argv!r}")
    tokens = argv[len(prefix) :]
    if not tokens:
        raise ValueError("auto-resume child argv carries no CLI subcommand tokens")
    parser = build_parser()
    try:
        args = parser.parse_args(tokens)
    except SystemExit as exc:
        # argparse rejected the tokens (unknown flag / bad value) via parser.error —
        # the exact usage-error a spawned child would die on with EXIT_USER_ERROR.
        raise ValueError(f"auto-resume child argv rejected by the CLI parser: {tokens!r}") from exc
    if args.command != "fetch":
        raise ValueError(f"auto-resume child argv must dispatch to `fetch`, got {args.command!r}")
    if args.universe:
        # The retired legacy three-phase flag: cmd_fetch refuses it with
        # EXIT_USER_ERROR before any pull work — the dead-resume BLOCKER.
        raise ValueError("auto-resume child argv carries the RETIRED `--universe` flag")
    if args.instruments:
        raise ValueError(
            "auto-resume child argv carries `--instruments` (loads the master and exits — no pull)"
        )
    if not args.until_clean:
        raise ValueError("auto-resume child argv must carry `--until-clean`")


def _spawn_auto_resume_pull(
    incomplete: Any, lake_root: Path, log: Any
) -> None:  # pragma: no cover — live subprocess spawn (the fetch spine is tested separately)
    """Spawn a detached ``brutex fetch --until-clean`` on *lake_root* to resume.

    The injected resume ACTION for
    :func:`~brutex.orchestrator.auto_resume.maybe_resume_on_boot`. A SEPARATE
    process (not an in-server job) is deliberate: the CLI fetch spine is already
    fully tested + honours the account-wide cross-process single-flight lock
    (DEC-ACCOUNT-PULL-SINGLEFLIGHT-001), so a concurrent dashboard click can never
    double the Groww request rate — the second pull is simply refused. The child is
    launched with ``start_new_session=True`` (its own process group) so it is
    DELIBERATELY un-reaped + re-parented to init on dashboard exit — a
    fire-and-forget resume, NOT the supervisor's supervised child (which DOES
    reap via ``child.wait()``); the child manages its OWN lifetime (it exits when
    the ledger is clean / the token dies / the round budget is spent). Any spawn
    error is caught by the ``maybe_resume_on_boot`` guard, so it can never crash
    the boot.

    Two dead-resume guards (DEC-AUTO-RESUME-ARGV-FIX-001), each surfacing as the
    honest ``error`` verdict instead of a false ``resumed``:

    1. the argv is validated against the REAL parser BEFORE spawning
       (:func:`_validate_auto_resume_argv` — the retired ``--universe`` regression
       class can never ship a child the CLI refuses); and
    2. a bounded best-effort early-exit probe AFTER spawning — a child that dies
       within :data:`_AUTO_RESUME_EARLY_EXIT_PROBE_S` with a non-zero exit
       (a usage / user error) raises into the ``maybe_resume_on_boot`` guard.
       Runs on the boot daemon thread, so the probe never blocks the port bind;
       a child that outlives the probe stays the un-reaped fire-and-forget resume.
    """
    import subprocess  # nosec B404 — stdlib subprocess, list-argv launch, no shell (see B603 below)

    argv = _auto_resume_child_argv(lake_root)
    _validate_auto_resume_argv(argv)
    log.info(
        "auto_resume.spawn",
        outstanding=incomplete.outstanding,
        planned=incomplete.planned,
        failed=incomplete.failed,
        lake_root=str(lake_root),
    )
    # nosec B603: `argv` is a fixed list built from `sys.executable` + literal
    # subcommand tokens + the operator-resolved lake root (see _auto_resume_child_argv)
    # — NOT a shell string and NOT untrusted network input; `shell=` is never set, so
    # there is no shell-injection surface. `start_new_session=True` isolates the child's
    # process group so the resumed pull outlives a dashboard restart (re-parented to init,
    # deliberately un-reaped — a fire-and-forget resume, unlike the supervised child).
    child = subprocess.Popen(  # nosec B603  # noqa: S603 — list argv, no shell, trusted CLI
        argv,
        start_new_session=True,
    )
    try:
        returncode = child.wait(timeout=_AUTO_RESUME_EARLY_EXIT_PROBE_S)
    except subprocess.TimeoutExpired:
        # Still running after the probe — the normal fire-and-forget resume path
        # (deliberately un-reaped; the kernel re-parents it on dashboard exit).
        return
    if returncode != 0:
        # An immediate non-zero exit is a usage / user error (e.g. an argv the CLI
        # refuses) — the resume did NOT happen. Raise into the maybe_resume_on_boot
        # guard so the boot log reads the honest `error` verdict, never `resumed`.
        raise RuntimeError(
            f"auto-resume child exited immediately with code {returncode} (argv={argv!r})"
        )
    # A clean immediate exit means the child found nothing left to pull (the
    # ledger settled between the detect and the spawn) — a successful resume.
    log.info("auto_resume.child_completed", returncode=returncode)


def _start_auto_resume_on_boot(
    lake_root: Path,
    log: Any,
    *,
    intent_store: Any = None,
    resume_pipeline: Callable[[], None] | None = None,
) -> None:  # pragma: no cover — daemon thread + live ledger read / subprocess spawn
    """Run the opt-in boot-time auto-resume for *lake_root* in a background daemon thread.

    Wires :func:`~brutex.orchestrator.auto_resume.maybe_resume_on_boot` (the
    gated, best-effort decision) to (a) the durable window-ledger read of
    *lake_root* and (b) the subprocess resume action THAT RE-PULLS THE SAME
    *lake_root* (the explicit ``--lake-root`` on the child — the hostile-review
    MAJOR fix, so the ledger read and the resumed pull can never diverge to two
    different lakes). Run in a daemon thread so the ledger read / spawn never
    blocks the uvicorn port bind — the SAME instant-launch seam
    :func:`_start_instrument_autoload` uses (DEC-DASHBOARD-INSTANT-LAUNCH-001). The
    whole decision is a no-op unless ``BRUTEX_AUTO_RESUME_ON_BOOT`` is truthy
    (default OFF — DEC-AUTO-RESUME-ON-BOOT-001), so a dev / CI boot is never
    surprised by a spontaneous pull. ``maybe_resume_on_boot`` never raises, so a
    failed resume trigger only logs — the boot is unaffected (Golden Rule #10).

    DEC-PULL-INTENT-RESUME-001 (the ONE decision point): when *intent_store* (a
    :class:`~brutex.dashboard.pull_intent.PullIntentStore`) and *resume_pipeline*
    (the app's ``app.state.brutex_resume_pipeline`` seam) are BOTH wired and a
    durable ``.pull_intent.json`` is present (escape hatch
    ``BRUTEX_PULL_INTENT_RESUME`` not OFF), the dashboard PIPELINE resume runs
    EXCLUSIVELY — the legacy spot-only CLI child is NOT spawned this boot, so
    the two resumes can never race the account-wide single-flight ``flock``.
    With no intent (or either seam unwired) the legacy path is byte-identical.
    """
    import threading

    from brutex.data.window_ledger_store import load_window_ledger
    from brutex.orchestrator.auto_resume import maybe_resume_on_boot

    intent_present: Callable[[], bool] | None = None
    if intent_store is not None and resume_pipeline is not None:
        from brutex.dashboard.pull_intent import pull_intent_resume_active

        def _intent_present() -> bool:
            return pull_intent_resume_active(intent_store)

        intent_present = _intent_present

    def _run() -> None:
        verdict = maybe_resume_on_boot(
            load_ledger=lambda: load_window_ledger(lake_root),
            resume=lambda incomplete: _spawn_auto_resume_pull(incomplete, lake_root, log),
            intent_present=intent_present,
            resume_intent=resume_pipeline,
        )
        log.info("auto_resume.verdict", verdict=verdict.value, lake_root=str(lake_root))

    threading.Thread(
        target=_run,
        name="brutex-auto-resume-on-boot",
        daemon=True,
    ).start()


def _start_lake_heal_sweep(
    lake_root: Path, summary_cache: Any, log: Any
) -> None:  # pragma: no cover — daemon-thread launcher over the 100%-tested sweeper
    """Launch the ONE-TIME lake-heal startup sweep (L3, DEC-LAKE-HEAL-001) off the bind path.

    Binds :class:`brutex.data.lake_heal.StartupSweeper` — the content-token-stamped
    one-time sweep (sentinel ``.lake_heal/last_sweep.json``; a repeat boot over an
    unchanged lake is a stat-walk no-op) — to the dashboard boot via the SAME
    instant-launch daemon-thread seam :func:`_start_instrument_autoload` /
    :func:`_start_auto_resume_on_boot` use, so the throttled O(files) scan never
    blocks the uvicorn port bind. ``on_healed=summary_cache.mark_dirty`` bumps the
    SAME write-generation every dashboard read cache keys on, so a heal is visible
    on the very next poll (never a stale pre-heal payload — Golden Rule #1). The
    sweeper's thread body is blanket-guarded inside ``lake_heal``
    (``lake_heal.sweep_failed``) — boot can never crash on a sweep bug.
    """
    from brutex.data.lake_heal import StartupSweeper

    log.info("lake_heal.sweep_launched", lake_root=str(lake_root))
    StartupSweeper(
        lake_root,
        on_healed=summary_cache.mark_dirty,
        catalog_path=lake_root / "catalog.duckdb",
    ).start()


def _build_liveproof_autorun(lake_root: Path) -> Any:
    """Construct the zero-command live-proof autorun holder for *lake_root*.

    A dedicated seam (DEC-DOCTOR-LIVE-AUTORUN-001) so the dashboard-boot unit
    tests can stub construction hermetically (mirroring
    :func:`_start_instrument_autoload` / :func:`_start_auto_resume_on_boot`)
    while this body stays directly testable offline — constructing
    :class:`~brutex.doctor_autorun.LiveProofAutorun` only reads the two small
    verdict caches; it spawns no thread and makes no network call.
    """
    from brutex.doctor_autorun import LiveProofAutorun

    return LiveProofAutorun(lake_root)


def _start_liveproof_preflight(liveproof: Any, log: Any) -> None:
    """Kick the ZERO-COMMAND startup preflight (DEC-DOCTOR-LIVE-AUTORUN-001).

    ``start_preflight`` is O(1): it only spawns the holder's daemon worker
    (single-flight; a ``BRUTEX_AUTORUN_LIVEPROOF=0`` escape hatch or an
    already-running preflight returns ``False``), so the uvicorn port bind is
    never blocked — the same instant-launch discipline as the instrument
    autoload (DEC-DASHBOARD-INSTANT-LAUNCH-001). The verdict lands on the
    dashboard's persistent live-proof banner; the operator runs NOTHING.
    """
    started = liveproof.start_preflight()
    log.info("liveproof.autorun_preflight", started=started)


#: Loopback bind addresses — the strict loopback-only default allowlist of
#: :data:`brutex.dashboard.host_allowlist.DEFAULT_ALLOWED_HOSTS` already admits every
#: client Host these binds can receive.
_LOOPBACK_BIND_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})

#: Wildcard binds (all interfaces) — the machine address a remote client will
#: put in ``Host`` cannot be known ahead of time.
_WILDCARD_BIND_HOSTS: Final[frozenset[str]] = frozenset(
    {"0.0.0.0", "::"}  # noqa: S104  # nosec B104 — classifying the operator's --host, not binding
)


def _dashboard_allowed_hosts(host: str) -> list[str] | None:
    """Resolve the dashboard ``Host``-header allowlist for the bound *host* (R2-DS-F1).

    ``build_app``'s DNS-rebinding guard defaults to a loopback-ONLY
    allowlist, so serving beyond loopback (the advertised ``--host`` flag)
    would hard-400 every remote client unless the actual bound host is
    threaded through. Three cases:

    * loopback bind (``127.0.0.1`` / ``localhost`` / ``::1``) → ``None``
      (keep the strict loopback-only default);
    * concrete non-loopback bind (e.g. ``192.168.1.50``) → the loopback
      default PLUS that host on any port, so remote clients addressing the
      bound name pass while foreign/rebound Hosts still 400;
    * wildcard bind (``0.0.0.0`` / ``::``) → ``["*"]`` — the guard cannot
      enumerate which machine address a client will use, so the Host
      allowlist is DISABLED (the caller warns LOUDLY; the same-origin CSRF
      guard in ``build_app`` stays fully active).

    Parameters
    ----------
    host
        The ``--host`` bind address as given on the CLI (an IPv6 literal may
        be bracketed; matching is case-insensitive, surrounding whitespace
        ignored).

    Returns
    -------
    list[str] | None
        ``None`` to keep ``build_app``'s loopback-only default, else the
        explicit allowlist to inject.
    """
    from brutex.dashboard.host_allowlist import DEFAULT_ALLOWED_HOSTS

    bare = host.strip().lower().strip("[]")
    if not bare or bare in _LOOPBACK_BIND_HOSTS:
        return None
    if bare in _WILDCARD_BIND_HOSTS:
        return ["*"]
    return [*DEFAULT_ALLOWED_HOSTS, f"{bare}:*"]


def cmd_dashboard(lake_root: Path | None, host: str, port: int) -> int:
    """Start the dashboard FastAPI server."""
    try:
        import uvicorn

        from brutex.dashboard.app import (
            DashboardState,
            _lake_revision_probe_or_none,
            build_app,
        )
        from brutex.dashboard.summary_cache import SummaryCache
    except ImportError as exc:
        print(
            f'[brutex] dashboard requires fastapi+uvicorn — run: pip install -e ".[ui]"  ({exc})',
            file=sys.stderr,
        )
        return EXIT_USER_ERROR

    # Persist logs to a rotating file (in addition to the console) so a
    # `make run` session has a viewable log trail — DEC-LOGS-VIEWER-001.
    # json_renderer=False keeps the console human-readable; the file sink is
    # ALWAYS JSON (the GET /api/v1/logs data endpoint parses it). Combined with
    # log_config=None on uvicorn.run below, this routes uvicorn's HTTP/startup
    # logs AND our own milestones through the rotating file
    # (DEC-RUN-LOG-CAPTURE-001).
    import structlog

    from brutex.observability.logging import configure_logging

    log_path = configure_logging(json_renderer=False, enable_file=True)
    _log = structlog.get_logger("brutex.dashboard")

    # Quiet uvicorn's "GET ... 200" access firehose — the 1 s /pull-progress +
    # 4 s coverage polls drowned the structured business events. The new
    # per-request `dashboard.request` middleware event (latency + request_id, at
    # DEBUG for those high-frequency polls) is now the single per-request source
    # of truth (DEC-DASHBOARD-REQUEST-LOGGING-001).
    from brutex.dashboard.request_logging import silence_uvicorn_access_log

    silence_uvicorn_access_log()

    # Mirror RAW stdout (any third-party / vendor print) into the rotating
    # JSON log too, so EVERY line the operator sees in
    # the terminal is also captured in .brutex-logs/brutex.log for copy/paste
    # ("capture every nook and corner" — DEC-RUN-LOG-CAPTURE-001). Routed to a
    # file-ONLY logger (propagate=False) so it lands in the file without
    # double-printing to the console. Best-effort: never break stdout.
    import logging as _logging

    _file_handlers = [
        h for h in _logging.getLogger().handlers if getattr(h, "_brutex_file_handler", False)
    ]
    if _file_handlers:
        _stdout_log = _logging.getLogger("brutex.stdout")
        _stdout_log.handlers = list(_file_handlers)
        _stdout_log.propagate = False
        _stdout_log.setLevel(_logging.INFO)

        class _StdoutMirror:
            """Tee stdout to the original stream AND the file-only log (line-buffered)."""

            def __init__(self, stream: Any) -> None:
                self._stream = stream
                self._buf = ""

            def write(self, data: str) -> int:
                """Write to the real stream + mirror each complete line to the file log."""
                self._stream.write(data)
                self._buf += data
                while "\n" in self._buf:
                    line, self._buf = self._buf.split("\n", 1)
                    if line.strip():
                        _stdout_log.info(line.rstrip())
                return len(data)

            def flush(self) -> None:
                """Flush the underlying stream (the file handler flushes itself)."""
                self._stream.flush()

            def isatty(self) -> bool:
                """Proxy ``isatty`` so colour/TTY detection still sees the real terminal."""
                return bool(getattr(self._stream, "isatty", lambda: False)())

            def fileno(self) -> int:
                """Proxy ``fileno`` so libraries that need the real fd keep working."""
                return int(self._stream.fileno())

        sys.stdout = _StdoutMirror(sys.stdout)

    state = DashboardState()
    # Point the live sweep-progress panel at the file a CLI `brutex sweep-all`
    # publishes to (DEC-SWEEP-WIRE-GA-001). Same path the supervisor's
    # `_on_progress` hook writes — the cross-process bridge for /api/v1/sweep-progress.
    state.sweep_progress_path = (
        _resolve_lake_root(lake_root) / "sweeps" / "progress" / "sweep-progress.json"
    )
    # Open a real InstrumentStore so the F&O Universe Explorer routes can call
    # store.load_snapshot(...). Passing the bare lake_root Path here was the
    # cause of the `'PosixPath' object has no attribute 'load_snapshot'` 500.
    # InstrumentStore.open() runs CREATE TABLE IF NOT EXISTS, so a fresh/empty
    # lake yields a valid empty store (universe shows 0 instruments, no crash).
    # `brutex dashboard` WITHOUT --lake-root resolves the DEFAULT lake
    # (--lake-root → $BRUTEX_LAKE_ROOT → ~/.brutex/lake) exactly like the
    # sibling `_resolve_lake_root` call sites in this function (+ cmd_run,
    # which pre-resolves) — previously a bare `brutex dashboard` silently
    # opened NO InstrumentStore at all (R2-OCLI-F6 asymmetry fix).
    from brutex.data.instrument_store import InstrumentStore

    store: Any = InstrumentStore.open(_resolve_lake_root(lake_root) / "instruments.duckdb")

    # The instrument autoload (keyless Groww master + best-effort NTM
    # constituent enrichment) can block ~90 s when niftyindices.com
    # bot-blocks / slow-first-bytes a cold start (3 × 30 s retries in
    # nse_constituents.fetch_constituent_csv). On the bind path that left the
    # dashboard unreachable (ERR_CONNECTION_REFUSED) for the whole window, so
    # run it in a background daemon thread — uvicorn binds the port INSTANTLY
    # and the InstrumentStore read/write split (DEC-STORE-READ-WRITE-SPLIT-001)
    # keeps concurrent route reads safe against the loader's writes
    # (DEC-DASHBOARD-INSTANT-LAUNCH-001).
    _start_instrument_autoload(store, _log)
    # ONE shared write-generation cache for this dashboard session (DEC-LAKE-HEAL-001):
    # constructed HERE instead of letting build_app default-construct its own internally,
    # so the L3 lake-heal startup sweep below can bump the SAME generation every dashboard
    # read cache keys on when it heals a partition (recon-D §7 load-bearing note). The
    # cross-process lake-revision probe (DEC-HARDENING-LEFTOVERS-001) is wired explicitly
    # — build_app attaches it only when it default-constructs the cache, so an injected
    # cache must carry its own probe or a CLI `brutex fetch` write would go undetected.
    summary_cache = SummaryCache(revision_probe=_lake_revision_probe_or_none(store))
    # ZERO-COMMAND live-proof autorun (DEC-DOCTOR-LIVE-AUTORUN-001): build the
    # holder for THIS lake root so the app can serve /api/v1/liveproof, mirror
    # credential refusals onto the banner, and auto-run the one-time probes on
    # the first REAL pull. Best-effort on the BIND path (Golden Rule #10 —
    # never crash boot): a construction failure logs and the dashboard still
    # starts (the banner honestly reads "present: false").
    liveproof: Any = None
    try:
        liveproof = _build_liveproof_autorun(_resolve_lake_root(lake_root))
    except Exception as exc:  # pylint: disable=broad-exception-caught  # never block the bind
        _log.warning("liveproof.build_failed", error=str(exc))
    # R2-DS-F1: thread the actual bound host through build_app's allowed_hosts
    # seam so serving beyond loopback (--host 0.0.0.0 / a LAN address) is not
    # hard-400'd by the TrustedHost DNS-rebinding guard. A loopback bind keeps
    # the strict loopback-only default; any relaxation is LOUDLY logged — a
    # wildcard bind disables the Host allowlist entirely (the same-origin CSRF
    # guard stays active), a concrete bind merely extends it with that host.
    allowed_hosts = _dashboard_allowed_hosts(host)
    if allowed_hosts is not None:
        _log.warning(
            "dashboard.host_allowlist_relaxed",
            host=host,
            allowed_hosts=allowed_hosts,
            host_guard="disabled" if "*" in allowed_hosts else "extended",
        )
    # Durable pull-intent sidecar (DEC-PULL-INTENT-RESUME-001): built for THIS
    # lake root so a real /pull-pipeline launch persists its re-fire payload,
    # the job clears it on worker exit, and the extended auto-resume seam below
    # can re-fire a pull that died mid-flight. Best-effort on the BIND path
    # (Golden Rule #10): a construction failure logs and the dashboard still
    # starts (the resume then simply stays unwired for this session).
    pull_intent_store: Any = None
    try:
        from brutex.dashboard.pull_intent import PullIntentStore

        pull_intent_store = PullIntentStore(_resolve_lake_root(lake_root))
    except Exception as exc:  # pylint: disable=broad-exception-caught  # never block the bind
        _log.warning("pull_intent.build_failed", error=str(exc))
    app = build_app(
        state,
        instrument_store=store,
        summary_cache=summary_cache,
        liveproof=liveproof,
        allowed_hosts=allowed_hosts,
        pull_intent_store=pull_intent_store,
    )
    # Auto-run the CHEAP preflight stages in the holder's background daemon
    # thread — the operator starts BruteX as always and the health proof runs
    # ITSELF (the hard operator law). Guarded like the auto-resume launch so
    # even a spawn failure only logs.
    if liveproof is not None:
        try:
            _start_liveproof_preflight(liveproof, _log)
        except Exception as exc:  # pylint: disable=broad-exception-caught  # never block the bind
            _log.warning("liveproof.launch_failed", error=str(exc))
    # Opt-in boot-time auto-resume (DEC-AUTO-RESUME-ON-BOOT-001): if
    # BRUTEX_AUTO_RESUME_ON_BOOT is truthy AND the durable window ledger shows an
    # in-progress pull (PLANNED / FAILED windows), auto-continue it by spawning
    # `brutex fetch --until-clean --lake-root <resolved>` — so a fresh
    # container / redeploy / restart picks up an unfinished pull with ZERO clicks
    # (§11 AUTOMATED). Runs in a daemon thread so the ledger read / spawn never blocks
    # the uvicorn bind, and is a no-op unless the flag is set (default OFF — a dev / CI
    # boot is never surprised). Best-effort on the BIND path: the launcher itself only
    # spawns a daemon thread (the resume DECISION never raises), but wrapping the launch
    # in a guard so even a thread-spawn failure logs + the dashboard still binds honours
    # the "never crashes boot" contract (Golden Rule #10).
    # DEC-PULL-INTENT-RESUME-001 extends this seam: with a durable pull intent
    # present, the PIPELINE resume (app.state.brutex_resume_pipeline) runs
    # EXCLUSIVELY and the legacy spot-only CLI child is not spawned this boot.
    try:
        _start_auto_resume_on_boot(
            _resolve_lake_root(lake_root),
            _log,
            intent_store=pull_intent_store,
            resume_pipeline=getattr(app.state, "brutex_resume_pipeline", None),
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught — best-effort, never block the bind
        _log.warning("auto_resume.launch_failed", error=str(exc))
    # DEC-LAKE-HEAL-001 L3: the ONE-TIME content-token-stamped lake-heal startup sweep
    # (a repeat boot over an unchanged lake is a stat-walk no-op; a healed partition
    # bumps summary_cache's generation via on_healed). Best-effort on the BIND path —
    # the same "never crashes boot" guard contract as the auto-resume launch above.
    try:
        _start_lake_heal_sweep(_resolve_lake_root(lake_root), summary_cache, _log)
    except Exception as exc:  # pylint: disable=broad-exception-caught — best-effort, never block the bind
        _log.warning("lake_heal.launch_failed", error=str(exc))
    print(f"[brutex] dashboard → http://{host}:{port}  (Ctrl-C to stop)")
    # The /logs WEBPAGE was removed (DEC-DASHBOARD-LOGS-REMOVE-001); point at the
    # rotating file + the kept JSON data endpoint, not the dead page.
    print(f"[brutex] logs → {log_path}  ·  tail via GET http://{host}:{port}/api/v1/logs")
    _log.info("dashboard.ready", host=host, port=port, log_file=str(log_path))
    # log_config=None lets uvicorn's access/error loggers PROPAGATE into our
    # root handlers (console + rotating JSON file) instead of uvicorn's own
    # isolated stderr handlers — so the file (and the /api/v1/logs endpoint)
    # capture the full HTTP + startup trail (DEC-RUN-LOG-CAPTURE-001).
    uvicorn.run(app, host=host, port=port, log_config=None)
    return EXIT_OK


def _open_browser(url: str) -> None:  # pragma: no cover — opens a real browser
    """Open *url* in the default web browser (best-effort, never raises)."""
    import webbrowser

    webbrowser.open(url)


def cmd_run(
    lake_root: Path | None,
    host: str,
    port: int,
    *,
    open_browser: bool,
) -> int:
    """One-click entry point: start the dashboard + open the browser.

    This is the runtime entry every operator surface ultimately boots
    (DEC-ONECLICK-RUN-001): ``./up.sh``, ``make run``, and the committed
    IntelliJ ▶ ``BruteX`` run configuration all execute the ONE ``./up.sh``
    bootstrap (self-check → self-heal → start, DEC-ONE-ENTRY-UNIFY-001),
    whose final step execs ``python -m brutex run`` — this function
    (``make run-raw`` reaches it directly). It resolves the lake root
    (``--lake-root`` → ``$BRUTEX_LAKE_ROOT`` → ``~/.brutex/lake``), prints
    a startup banner, optionally opens the browser, then delegates to
    :func:`cmd_dashboard` — which prints the dashboard URL and the rotating
    log-file location, one clear line each.

    The browser is opened best-effort *before* the server finishes binding,
    so it may need one refresh on a cold start — acceptable for a local
    dev launch. Pass ``open_browser=False`` (``--no-browser``) for headless
    or CI invocations.
    """
    resolved_lake = lake_root if lake_root is not None else resolve_lake_root()

    url = f"http://{host}:{port}"
    universe_url = f"{url}/universe"
    print(f"[brutex] BruteX dashboard → {url}  (lake: {resolved_lake})", file=sys.stderr)
    print(f"[brutex] opening F&O Universe Explorer → {universe_url}", file=sys.stderr)
    if open_browser:
        # Open the Universe Explorer directly (DEC-DASHBOARD-AUTOOPEN-001) —
        # the operator's working surface for browsing + pulling instruments.
        _open_browser(universe_url)
    return cmd_dashboard(lake_root=resolved_lake, host=host, port=port)


def _parse_dt(value: str) -> datetime:
    """Parse a ``YYYY-MM-DD`` or full-ISO string to a UTC-aware datetime.

    Raises
    ------
    CliUserError
        On a malformed value. Every ``--start``/``--end`` (fetch / sweep /
        sweep-all / verify-lookahead / sweep-verify --recount / options-sim)
        funnels through here, so a typo'd date is one clean line +
        :data:`EXIT_USER_ERROR` via the :func:`main` net — never a raw
        ``ValueError`` traceback (DEC-CLI-USER-ERROR-HYGIENE-001).
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CliUserError(
            f"invalid --start/--end value {value!r}: expected ISO format "
            "'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM:SS'"
        ) from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _resolve_lake_root(lake_root: Path | None) -> Path:
    """``--lake-root`` → ``$BRUTEX_LAKE_ROOT`` → ``~/.brutex/lake`` (via config.paths)."""
    if lake_root is not None:
        return lake_root
    return resolve_lake_root()


def _require_lake_dir(lake_root: Path, *, command: str) -> None:
    """Refuse a nonexistent lake root on a lake-READING command.

    ``Catalog.open`` mkdirs the whole missing directory chain, so a typo'd
    ``--lake-root`` on a read command silently materialised a fresh empty
    lake + empty catalog and reported "partitions=0" / "no bars" — a silent
    illusion (Golden Rule #1). Read commands must fail loudly instead
    (DEC-CLI-USER-ERROR-HYGIENE-001); the ``fetch`` write path keeps
    create-on-first-write.

    Raises
    ------
    CliUserError
        If *lake_root* is not an existing directory.
    """
    if not lake_root.is_dir():
        raise CliUserError(
            f"lake root not found: {lake_root} — pass the right --lake-root "
            f"(or $BRUTEX_LAKE_ROOT); `brutex {command}` reads an existing lake "
            "and never creates one"
        )


def _require_catalog_exists(catalog: Path) -> None:
    """Refuse a nonexistent catalog file on a catalog-READING command.

    Raises
    ------
    CliUserError
        If *catalog* is not an existing file — with the reconcile remedy for
        a lake that has bars but was never catalogued
        (DEC-CATALOG-RECONCILE-WIRE-001 lakes).
    """
    if not catalog.is_file():
        raise CliUserError(
            f"catalog not found: {catalog} — pass the right --lake-root/--catalog "
            "(read-only commands never create a lake); if the lake HAS bars but no "
            "catalog, run `brutex catalog reconcile --lake-root <lake>` first"
        )


def _resolve_fetch_intervals(interval: str) -> list[str]:
    """Map the ``--interval`` flag to the FETCHED intervals.

    Groww fetches only ``1minute`` + ``1day`` (the 19 intraday timeframes are
    resampled from the 1-minute base — DEC-DHAN-1M-ONLY-001). ``all`` →
    both; ``1m``/``1minute`` and ``1d``/``1day`` → that one; anything else
    raises :class:`ValueError` (a non-fetched timeframe).
    """
    from brutex.dashboard.pull_controller import (
        SPOT_INTERVAL_BOTH,
        SPOT_INTERVAL_CHOICES,
    )

    if interval == "all":
        # ``all`` resolves to the explicit both-tuple (1minute + 1day) via the retained
        # legacy choice map, so this CLI alias stays stable. Note the DASHBOARD pull now
        # fetches 1-minute ONLY and DERIVES 1-day (DEC-1MINUTE-ONLY-DERIVE-1DAY-001); this
        # CLI helper is the vestigial spot-pull ``--interval`` surface, unchanged here.
        return list(SPOT_INTERVAL_CHOICES[SPOT_INTERVAL_BOTH])
    canonical = {
        "1m": "1minute",
        "1minute": "1minute",
        "1d": "1day",
        "1day": "1day",
    }.get(interval)
    if canonical is None:
        raise ValueError(
            f"--interval {interval!r} is not fetched on Groww — fetch 1minute/1day only "
            "(the 19 intraday timeframes are resampled from the 1-minute base). "
            "Use --interval 1minute, 1day, or all."
        )
    return [canonical]


def _synthetic_index_instrument(spec: Any) -> Any:
    """Build a placeholder spot index ``Instrument`` from a brute-force spec.

    The dry-run :class:`_DryRunGrowwSpotAdapter` ignores the symbol when
    generating bars, so the ``symbol_name`` is the index's own name — a
    real ``InstrumentStore`` (with numeric vendor SECURITY_IDs) is not required
    for an offline ``fetch --dry-run`` (DEC-VENDOR-PIVOT-001).
    """
    from decimal import Decimal

    from brutex.models import Instrument, InstrumentType, Segment

    return Instrument(
        exch_id=spec.exchange,
        security_id=0,
        symbol_name=spec.underlying_symbol,
        display_name=spec.display_name,
        instrument=InstrumentType.IDX,
        segment=Segment.INDEX,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


def _resolve_brute_force_instruments(  # pragma: no cover — opens DuckDB store + fno_math kernel
    lake_root: Path,
) -> list[Any]:
    """Resolve the 3 brute-force index ``Instrument``s from the populated store.

    The live pull needs the real vendor ``SECURITY_ID`` for each index, which
    lands in the store via ``fetch --instruments`` / ``--universe``. Filters
    the store's index/equity rows to the brute-force indices (NIFTY /
    BANKNIFTY / SENSEX); raises if the store is empty (operator must load the
    instrument master first).
    """
    from brutex.data.instrument_store import InstrumentStore
    from brutex.universe import is_in_brute_force_scope

    store = InstrumentStore.open(lake_root / "instruments.duckdb")
    try:
        resolved = [
            inst for inst in store.list_index_equity_instruments() if is_in_brute_force_scope(inst)
        ]
    finally:
        store.close()
    if not resolved:
        raise ValueError(
            "no brute-force indices in the instrument store — run "
            "`brutex fetch --instruments` (or `--universe`) first to load the master."
        )
    return resolved


def _build_live_groww_spot(  # pragma: no cover — live Groww TOTP token + SDK + network
    lake_root: Path, catalog: Any = None
) -> tuple[Any, Any]:
    """Wire the real Groww client + persisting Parquet sink → ``(adapter, sink)``.

    PR-J2 vendor cutover (DEC-DHAN-GROWW-REMIGRATE-001, ADR-0009 §5): the live
    spot pull now runs on Groww. Resolves the TOTP credentials (env-first,
    file-fallback), builds a live SDK-backed
    :class:`~brutex.adapters.groww.client.GrowwClient` via the
    :func:`~brutex.adapters.groww.live.build_live_client` composition seam, loads
    the Groww instrument master into the O(1)
    :class:`~brutex.adapters.groww.instruments.InstrumentIndex` (the adapter
    resolves every fetched symbol through it), and writes through a
    ``ParquetBarSink`` over *lake_root*.

    Unlike the retired builder there is no ``transport`` to inject — Groww
    talks through its SDK client, not a urllib transport — and no preset
    instrument list: the adapter carries the whole master index and resolves
    each fetched symbol in O(1).

    Rate-controller hooks (DEC-SUSPICION-SWEEP-FIXES-001 D1): this builder wires
    NO ``pace`` / ``on_rate_limit`` / ``on_success`` — ``cmd_fetch`` constructs the
    :class:`~brutex.adapters.rate_common.RateController` AFTER this build. The FULL
    hook triple is bound downstream at the ONE COMMON spine seam
    (``run_ingest.with_paced_client``, called by ``run_groww_spot_ingest`` with
    the hooks ``cmd_fetch`` threads into ``run_groww_spot_ingest_with_retry``), so
    every client-absorbed 429 still decrements the shared AIMD ladder — pinned by
    the production-wiring test in ``tests/unit/test_groww_spot_rate_feedback.py``.
    """
    from brutex.adapters.groww.adapter import GrowwMarketAdapter
    from brutex.adapters.groww.auth import GrowwCredentials
    from brutex.adapters.groww.instrument_loader import load_index, urllib_http_get
    from brutex.adapters.groww.live import build_live_client
    from brutex.config.credentials import resolve_groww_aws_credentials
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.raw_integrity_store import make_capture_hook_factory
    from brutex.data.sink import ParquetBarSink

    aws_creds = resolve_groww_aws_credentials()
    creds = GrowwCredentials(api_key=aws_creds.api_key, totp_secret=aws_creds.totp_secret)
    client = build_live_client(creds)
    index = load_index(urllib_http_get)
    # Raw-data integrity capture (DEC-RAW-DATA-INTEGRITY-001): bind the per-symbol
    # capture-hook factory to the lake root so every live ``brutex fetch`` spot
    # window records a hash of EXACTLY what Groww sent (best-effort — never halts).
    adapter = GrowwMarketAdapter(
        client=client, index=index, capture_hook_factory=make_capture_hook_factory(lake_root)
    )
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root), catalog=catalog)
    return adapter, sink


@dataclass(frozen=True, slots=True)
class _DryRunGrowwSpotAdapter:
    """Offline dry-run spot adapter for ``brutex fetch --dry-run`` on Groww.

    PR-J2 has no Groww synthetic adapter yet (that lands in a later K PR), so —
    to keep ``--dry-run`` a real, no-network proof that the fetch → session-filter
    → sink pipeline works — this minimal deterministic stand-in returns valid
    in-session :class:`~brutex.data.models.CandleBar` rows from the vendor-neutral
    pure :func:`~brutex.adapters.ingest_common.synthetic_spot_bars` generator
    (a pure bar builder, not the Groww *adapter*). It satisfies the
    :class:`~brutex.adapters.protocol.MarketAdapter` shape the Groww spot spine
    drives, with NO client, NO credentials and NO network — so it is fully
    deterministic and unit-testable.
    """

    bars_per_window: int = 5

    @property
    def capabilities(self) -> Any:
        """Groww spot capabilities (so the spine plans the dry-run as it would live)."""
        from brutex.adapters.groww.adapter import GROWW_CAPABILITIES

        return GROWW_CAPABILITIES

    def list_instruments(self, *, cursor: str | None = None) -> Any:
        """Return an empty single page — the spine fans out over an explicit list."""
        from brutex.adapters.protocol import single_instrument_page

        return single_instrument_page((), cursor)

    def list_derivatives(self, *, underlying_symbol: str, exchange: Any) -> Any:
        """Yield nothing — Track-1 spot is derivative-free (CLAUDE.md §3)."""
        del underlying_symbol, exchange  # spot scope — intentionally no contracts
        return iter(())

    def fetch_ohlcv(
        self,
        *,
        symbol: str,
        exchange: Any,
        interval: str,
        start: Any,
        end: Any,
    ) -> Any:
        """Return *bars_per_window* deterministic in-session bars for *start*'s day."""
        from brutex.adapters.ingest_common import synthetic_spot_bars

        del symbol, exchange, interval, end  # deterministic offline stand-in
        return synthetic_spot_bars(start, count=self.bars_per_window)


def _run_instrument_master_load(  # pragma: no cover — opens DuckDB store + live network
    lake_root: Path,
) -> dict[str, Any]:
    """Keylessly load the public Groww instrument master into the lake.

    Opens the real :class:`~brutex.data.instrument_store.InstrumentStore` and
    delegates to the testable
    :func:`brutex.dashboard.instrument_loader.load_instrument_master` with NO
    injected ``http_get`` — i.e. the real SSRF-guarded public-CSV fetch, which
    needs NO vendor API keys (DEC-INSTRUMENT-LOAD-001). Marked ``no cover``: the
    DuckDB store transitively imports the ``fno_math`` Rust kernel and the
    fetch hits the live network, so this runs only on a fully-built, online
    environment; the pure loader core is unit-tested with a fake ``http_get``.
    """
    from brutex.dashboard.instrument_loader import load_instrument_master
    from brutex.data.instrument_store import InstrumentStore

    store = InstrumentStore.open(lake_root / "instruments.duckdb")
    try:
        return load_instrument_master(store)
    finally:
        store.close()


def _make_dryrun_lake_root() -> Path:
    """Create the clearly-labelled ephemeral system-temp lake for ``--dry-run``.

    DEC-DRYRUN-LAKE-ISOLATION-001: a ``--dry-run`` drives the REAL
    :class:`~brutex.data.sink.ParquetBarSink` pipeline with SYNTHETIC bars, so its
    sink + catalog must NEVER point at the resolved production lake — a synthetic
    bar stored first would make the keep-first partition merge PERMANENTLY drop the
    real vendor bar that arrives later at the same ``(series, timestamp)``. The
    dry-run therefore writes into this isolated ``tempfile.mkdtemp`` directory
    (removed best-effort by :func:`_ephemeral_dryrun_lake_cleanup`), preserving the
    full offline fetch → session-filter → sink proof + its reported counts.

    Returns
    -------
    Path
        A fresh ``brutex-fetch-dryrun-lake-*`` directory under the system temp.
    """
    return Path(tempfile.mkdtemp(prefix="brutex-fetch-dryrun-lake-"))


@contextlib.contextmanager
def _ephemeral_dryrun_lake_cleanup(lake_root: Path | None) -> Generator[None, None, None]:
    """Remove the ephemeral ``--dry-run`` temp lake on ANY exit (best-effort).

    ``None`` (a live / ``--inspect`` pull — no ephemeral lake) is a no-op. Entered
    as the OUTERMOST fetch context so the dry-run catalog opened on the ephemeral
    lake is closed BEFORE the tree is removed; ``ignore_errors=True`` keeps cleanup
    from ever masking the pull's own outcome (on a failed removal the directory is
    still clearly labelled ``brutex-fetch-dryrun-lake-*`` under the system temp).

    Parameters
    ----------
    lake_root
        The ephemeral dry-run lake from :func:`_make_dryrun_lake_root`, or ``None``
        when the pull writes to the real lake (live) or nowhere (``--inspect``).
    """
    try:
        yield
    finally:
        if lake_root is not None:
            shutil.rmtree(lake_root, ignore_errors=True)


def _arm_retry_storm_guard(
    *,
    max_retry_minutes: int | None,
    lake_root: Path,
    instruments: Sequence[Any],
    intervals: Sequence[str],
    stack: contextlib.ExitStack,
) -> Callable[[str, int, bool], None] | None:
    """Build + arm the retry-storm give-up guard for one pull (DEC-RATE-STORM-WATCHDOG-001).

    Returns a Groww ``run_ingest`` ``ProgressHook`` ``(label, bars, failed)`` that feeds
    every window outcome into a :class:`~brutex.orchestrator.watchdog.RetryStormWatchdog`
    (a terminal window-failure gives up + stamps a hash-chained ``RETRY_GIVE_UP``
    audit row; a symbol reaching 100%% window-failure stamps ``SYMBOL_WINDOW_FAILURE``
    + fires ONE best-effort Telegram alert, never per window), or ``None`` when
    ``--max-retry-minutes`` is unset. The audit writer is opened under
    ``<lake>/audit/`` and its close is registered on *stack*.
    """
    if max_retry_minutes is None:
        return None
    from brutex.notifications.sweep_alerts import SweepAlerter
    from brutex.observability.correlation import new_pull_id
    from brutex.orchestrator.audit import AuditWriter
    from brutex.orchestrator.watchdog import build_retry_storm_watchdog

    audit_path = lake_root / "audit" / f"retry-storm-{new_pull_id()}.jsonl"
    audit = stack.enter_context(AuditWriter.open(audit_path))
    watchdog = build_retry_storm_watchdog(
        max_retry_minutes=max_retry_minutes,
        audit=audit,
        on_symbol_failure=SweepAlerter().symbol_failure_callback(),
    )
    watchdog.register_fanout([instrument.symbol_name for instrument in instruments], intervals)
    print(
        f"[brutex] retry-storm guard armed — give up after {max_retry_minutes} min of "
        f"continuous failure; audit → {audit_path}",
        file=sys.stderr,
    )
    # The watchdog hook is the shared 4-arg ``(label, bars, failed, gap)`` shape;
    # Groww's ``ProgressHook`` is 3-arg (no known-gap branch — the documented
    # divergence), so adapt to it with ``gap`` pinned False (Groww never emits a gap).
    watchdog_hook = watchdog.progress_hook()

    def _groww_progress(label: str, bars: int, failed: bool) -> None:
        """Feed a Groww window outcome into the shared watchdog (gap is always False)."""
        watchdog_hook(label, bars, failed, False)

    return _groww_progress


def _emit_pull_halt_alert(exc: Exception) -> None:
    """Fire a best-effort CRITICAL Telegram alert that the pull HALTED (DEC-TELEGRAM-PULL-HALT-001).

    Called when a pull-wide global-stop (a full-disk / OOM
    :class:`~brutex.data.errors.LakeWriteHalt`, or a daily-quota / quota-invariant
    stop) propagates out of the ingest spine. Unlike a per-symbol quarantine this
    aborts EVERY remaining window, so the operator must act — hence a CRITICAL push.
    Fire-and-forget: a disabled or failing notifier is a silent no-op (the
    :class:`~brutex.notifications.sweep_alerts.SweepAlerter` swallows every publish
    error), so the alert NEVER masks the halt, which the caller re-raises so the CLI
    still exits non-zero. The ``LakeWriteHalt.reason`` (``disk_full`` /
    ``out_of_memory`` / ``volume_wedged`` — the latter from the keepalive probe OR the
    per-partition write-health breaker, DEC-WRITE-HEALTH-BREAKER-001) is surfaced
    verbatim; any other stop uses its class name.

    Thin wrapper delegating to the COMMON
    :func:`brutex.notifications.sweep_alerts.emit_pull_halt_alert` (CLAUDE.md §11 —
    the SAME helper the dashboard ``BackgroundPullJob`` uses, no copy-paste); that
    function carries the blanket try/except so this never raises into the halt path.
    """
    from brutex.notifications.sweep_alerts import emit_pull_halt_alert

    emit_pull_halt_alert(exc)


def _enter_pull_singleflight(stack: Any, lake_root: Path, account_key_resolver: Any = None) -> bool:
    """Take the cross-process account single-flight lock for the CLI pull; report contention.

    DEC-PULL-HARDENING-FOLLOWUP-001 (MIGRATION.md #996) — the cross-process pull lock
    (:func:`~brutex.adapters.groww.pull_singleflight.pull_singleflight_lock`) was wired
    ONLY into the dashboard ``BackgroundPullJob``; the CLI ``brutex fetch`` path took NO
    lock, so a CLI pull + a dashboard pull on the SAME lake could run at once and DOUBLE
    the Groww account request rate (CLAUDE.md Golden Rule #6). This wires the CLI half:
    the lock context manager is ``__enter__``-ed into the caller's :class:`contextlib.ExitStack`
    so it is held for the WHOLE fetch + released on exit (success OR error), exactly as
    ``pull_jobs.start()`` does for the dashboard.

    On contention — a peer process is mid-pull against this lake —
    :class:`~brutex.adapters.groww.pull_singleflight.PullAlreadyRunningError` is caught,
    a clear actionable operator message is printed to ``stderr``, and ``False`` is
    returned so the caller refuses to start a SECOND concurrent pull (fail-fast — never
    double-run, never block for hours). A clean acquire returns ``True``.

    Only the genuine persisting LIVE pull takes the lock; ``--dry-run`` / ``--inspect``
    (which write no real lake data and pace no real account requests) deliberately do
    NOT call this, so an offline test or an inspect run never contends.

    **Account-keyed second lock, taken FIRST (DEC-ACCOUNT-KEY-SINGLEFLIGHT-001,
    MIGRATION.md #1285):** before the lake-root lock this enters the ACCOUNT-keyed
    :func:`~brutex.adapters.groww.pull_singleflight.account_pull_singleflight_lock`
    (``~/.brutex/locks/pull_account<pbkdf2[:16]>.flock``), keyed off the AWS-resolved
    api_key (:func:`~brutex.config.credentials.resolve_groww_aws_credentials` — the SOLE
    credential source, DEC-AWS-ONLY-CREDS-001; the retired env-var path is NEVER read) —
    so the SAME account pulled into TWO distinct lake roots is refused too (the #996
    residual, closed for the single-host case). A ``CredentialsError`` SKIPS the account
    lock gracefully with one loud log line (a credential-less run makes zero vendor
    calls); the lake-root lock below still guards same-lake peers. The account lock is
    strictly FIRST + the ExitStack releases LIFO, so the pair can never deadlock-invert
    against ``pull_jobs.start()``'s identical order. *account_key_resolver* is the
    injectable ``() -> str | None`` key seam (tests); ``None`` (production) resolves via
    the AWS-only source.
    """
    import structlog

    from brutex.adapters.groww.pull_singleflight import (
        PullAlreadyRunningError,
        account_pull_singleflight_lock,
        pull_singleflight_lock,
    )

    if account_key_resolver is not None:
        account_key: str | None = account_key_resolver()
    else:
        from brutex.config.credentials import resolve_groww_aws_credentials
        from brutex.config.credentials_types import CredentialsError

        try:
            account_key = resolve_groww_aws_credentials().api_key
        except CredentialsError as exc:
            account_key = None
            structlog.get_logger("brutex.cli").warning(
                "pull.account_lock_skipped",
                reason="credentials unavailable — account-keyed single-flight not taken",
                error=str(exc),
            )
    if account_key is not None:
        try:
            stack.enter_context(account_pull_singleflight_lock(account_key))
        except PullAlreadyRunningError as exc:
            print(
                f"[brutex] {exc} — refusing to start a second concurrent pull for this "
                "Groww account (it may be running against a DIFFERENT lake root). Wait "
                "for the running pull to finish (or stop it) before re-running "
                "`brutex fetch`.",
                file=sys.stderr,
            )
            return False
    try:
        stack.enter_context(pull_singleflight_lock(lake_root))
    except PullAlreadyRunningError as exc:
        print(
            f"[brutex] {exc} — refusing to start a second concurrent pull on this lake. "
            "Wait for the running pull to finish (or stop it) before re-running "
            "`brutex fetch`.",
            file=sys.stderr,
        )
        return False
    return True


def _make_cli_rate_bridge_on_window(
    inner: Any,
    lake_root: Path,
    controller: Any,
    adapter: Any,
    monitor: Any = None,
) -> Any:
    """Wrap the CLI per-window ``on_window`` to ALSO publish the live rate to the bridge file.

    DEC-CLI-RATE-BRIDGE-001 (MIGRATION.md #997). A ``brutex fetch`` pull runs in a SEPARATE
    process from the dashboard, so it never touches the dashboard's in-memory rate panel and
    the live-rate WIDGET (DEC-LIVE-RATE-MONITOR-001) stayed IDLE during a CLI pull — there
    was no CLI rate-observer seam. This is that seam: on each processed window the wrapped
    callback runs *inner* FIRST (the existing throttled ledger checkpoint — byte-unchanged),
    then snapshots the SAME ``RateController.snapshot()`` + transport ``CallMeter.snapshot()``
    the dashboard mirrors and atomically writes them, with a cross-process wall-clock stamp,
    to ``<lake_root>/.rate/pull_rate.json`` via
    :func:`~brutex.data.rate_bridge.publish_from_snapshots`. The dashboard's
    ``/api/v1/pull/rate`` reader then surfaces whichever pull (dashboard OR CLI) ticked most
    recently, so the widget reflects the active CLI pull's real measured rate.

    The publish is BEST-EFFORT cross-process VISIBILITY (NOT shared rate accounting — the
    per-process caps are unchanged; the single-flight lock already prevents two concurrent
    pulls). The bridge write is atomic + ``lake_write_guard``-wrapped, so a torn read is
    impossible and a full-disk ENOSPC maps to a :class:`LakeWriteHalt` like every other
    lake-write site. When no controller is wired (a ``--dry-run`` / ``--inspect`` pull paces
    no real account requests) there is nothing real to publish, so *inner* is returned
    unwrapped (zero overhead, no bridge file written).

    Pull-health self-instrumentation (DEC-PULL-HEALTH-CLI-WIRING-001, MIGRATION.md #1013):
    when a *monitor* (:class:`~brutex.observability.pull_health.PullHealthMonitor`) is wired,
    the SAME per-window controller + call-meter snapshot pair this hook already builds for the
    bridge is ALSO fed to :meth:`~brutex.observability.pull_health.PullHealthMonitor.observe`,
    so a CLI ``brutex fetch`` pull WATCHES its own health (Prometheus gauges + the
    ``pull_degraded`` Telegram alert on a breach edge) exactly like the dashboard pull (which
    feeds the monitor through the twin :func:`~brutex.dashboard.pull_controller._observe_rate`
    seam, DEC-PULL-HEALTH-WIRING-001 / MIGRATION.md #935). REUSING this existing per-window
    seam means NO new tick / thread / cadence is added; ``observe`` is itself best-effort
    (never raises) and is fed AFTER the bridge publish under its OWN guard so a publish error
    never starves the watch and a monitor error never crashes the pull. A ``None`` *monitor*
    (a dry-run / inspect pull, or a guarded :func:`~brutex.dashboard.pull_controller.\
_build_pull_health_monitor` build failure) is a no-op.

    Parameters
    ----------
    inner
        The existing 0-arg ``on_window`` callback (the locked ledger checkpoint).
    lake_root
        The lake root (locates the ``.rate`` sidecar dir).
    controller
        The live pull's :class:`~brutex.adapters.rate_common.RateController` (``None`` for
        a dry-run / inspect pull — then no bridge is published and no monitor is fed).
    adapter
        The market adapter; its ``client.meter`` supplies the achieved-throughput snapshot.
    monitor
        The live pull's :class:`~brutex.observability.pull_health.PullHealthMonitor`, or
        ``None`` (best-effort — a dry-run / inspect pull, or a guarded build failure, leaves
        it ``None`` and the health watch is simply absent).

    Returns
    -------
    Any
        A wrapped 0-arg callback, or *inner* unchanged when there is no controller to read.
    """
    if controller is None:
        return inner

    import time

    import structlog

    from brutex.data.errors import LakeWriteHalt
    from brutex.data.rate_bridge import publish_from_snapshots

    log = structlog.get_logger("brutex.cli.rate_bridge")

    def _on_window() -> None:
        inner()
        # The publish is a pure VISIBILITY side-effect, so it must NEVER crash the operator's
        # multi-hour pull: wrap it so only a real disk-full halt (``LakeWriteHalt``, consistent
        # with every other lake-write site) propagates, and every OTHER error — a non-disk
        # ``OSError`` (``EROFS`` / ``EACCES`` / ``EXDEV`` from the ``.rate`` mkdir or the atomic
        # ``os.replace``, which ``atomic_io`` deliberately does NOT map) or a transient
        # controller/meter snapshot error — is swallowed + logged best-effort. The publish does
        # one per-window ``fsync`` (no throttle — windows land at ~1 s cadence; an fsync of a
        # tiny JSON file is negligible against the per-window fetch+append already on this path).
        controller_snap: Mapping[str, Any] | None = None
        meter_snap: Mapping[str, Any] = {}
        try:
            meter = getattr(getattr(adapter, "client", None), "meter", None)
            meter_snap = meter.snapshot() if meter is not None else {}
            published_snap: Mapping[str, Any] = controller.snapshot()
            controller_snap = published_snap
            publish_from_snapshots(
                lake_root,
                published_snap,
                meter_snap,
                wall_time=time.time(),
                monotonic=time.monotonic(),
                # DEC-CLI-BRIDGE-PULL-ACTIVE-001: each per-window publish carries the
                # AUTHORITATIVE "a pull IS running" signal, so the dashboard's stall
                # verdict is gated exactly like a dashboard pull's.
                pull_active=True,
            )
        except LakeWriteHalt:
            # Disk-full / OOM is a genuine pull-wide stop — let it propagate so the existing
            # halt handling (the `_pull_halt_errors` catch) runs, exactly like every writer.
            raise
        except Exception:  # pylint: disable=broad-exception-caught
            # Best-effort visibility — never crash the pull for a rate-monitor side-effect.
            log.warning("cli.rate_bridge.publish_failed", exc_info=True)
        # Feed the SAME snapshot pair to the pull-health monitor so a CLI pull self-instruments
        # its health (DEC-PULL-HEALTH-CLI-WIRING-001). ``observe`` is best-effort (never raises),
        # but the controller snapshot can still be ``None`` here if the publish above failed
        # BEFORE it was taken — re-snapshot it under THIS guard so the watch never starves and
        # never crashes the pull. A ``None`` monitor (dry-run / build failure) is a no-op.
        if monitor is not None:
            try:
                if controller_snap is None:
                    controller_snap = controller.snapshot()
                monitor.observe(controller_snap, meter_snap)
            except Exception:  # pylint: disable=broad-exception-caught
                # Pull-health is observe-only — never crash the pull for a monitor side-effect.
                log.warning("cli.pull_health.observe_failed", exc_info=True)

    return _on_window


def _publish_cli_pull_inactive(lake_root: Path, controller: Any, adapter: Any) -> None:
    """Best-effort publish ONE final ``pull_active=False`` bridge snapshot at pull end.

    DEC-CLI-BRIDGE-PULL-ACTIVE-001: the per-window bridge publishes ``pull_active=True``
    while the CLI pull runs; this final publish (invoked from ``cmd_fetch``'s ``finally``,
    after the pipeline drain + the durable ledger persist) flips the flag to ``False`` so
    the dashboard widget reads an honest ``done``/``idle`` the moment the pull ends —
    never a red "stalled" mislabel on a just-finished CLI pull.

    Wholly best-effort: EVERY error — including a :class:`~brutex.data.errors.LakeWriteHalt`
    — is swallowed + logged, because the pull is already ENDING and its real outcome (a
    clean exit, a crash, or a re-raised halt) must never be masked by a visibility
    side-effect. A ``None`` *controller* (``--dry-run`` / ``--inspect`` — no bridge was
    ever published) is a no-op, preserving the persistence-free contract.

    Parameters
    ----------
    lake_root
        The lake root (locates the ``.rate`` sidecar dir).
    controller
        The live pull's :class:`~brutex.adapters.rate_common.RateController`, or ``None``.
    adapter
        The market adapter; its ``client.meter`` supplies the final throughput snapshot.
    """
    if controller is None:
        return

    import time

    import structlog

    from brutex.data.rate_bridge import publish_from_snapshots

    try:
        meter = getattr(getattr(adapter, "client", None), "meter", None)
        meter_snap: Mapping[str, Any] = meter.snapshot() if meter is not None else {}
        publish_from_snapshots(
            lake_root,
            controller.snapshot(),
            meter_snap,
            wall_time=time.time(),
            monotonic=time.monotonic(),
            pull_active=False,
        )
    except Exception:  # pylint: disable=broad-exception-caught
        structlog.get_logger("brutex.cli.rate_bridge").warning(
            "cli.rate_bridge.finalize_failed", exc_info=True
        )


def _next_day_saturating(day: date) -> date:
    """Widen an inclusive end date to its half-open ``+1 day`` boundary, saturating.

    ``date.max + timedelta(days=1)`` raises :class:`OverflowError`; the fetch
    seam widens AFTER the pull completed, and its outcome must never be
    replaced by a widening crash ("an enrich error NEVER fails the pull" —
    DEC-CLI-GREEKS-ENRICH-001, review round-1 fix). Saturating at
    ``date.max`` loses nothing: the widened value only clamps lake
    discovery, and the canonical partition window ends at
    ``MAX_PARTITION_YEAR`` (2100) — no stored bar can sit ON ``9999-12-31``
    for the exclusive end to drop.

    Parameters
    ----------
    day
        The inclusive end date to widen.

    Returns
    -------
    datetime.date
        ``day + 1 day`` — or ``date.max`` unchanged when no later date exists.
    """
    if day == date.max:
        return date.max
    return day + timedelta(days=1)


def _run_cli_greeks_enrich(
    *, lake_root: Path, instruments: Sequence[Any], start: date, end: date
) -> Any:  # -> GreeksEnrichOutcome
    """Post-fetch Greeks enrich over the pulled-underlyings × pulled-window slice.

    The CLI twin of the dashboard's post-drain enrich seam
    (``historical_pull.pull_contract_candles``) via the COMMON
    :func:`~brutex.data.greeks_enrich_pass.run_greeks_enrich_pass`
    (DEC-CLI-GREEKS-ENRICH-001). Best-effort: a fenced failure never fails the
    pull; a pull-wide halt (:class:`~brutex.data.errors.LakeWriteHalt` /
    :class:`~brutex.data.errors.GreeksEnrichPoolBroken`) fires the CRITICAL
    Telegram alert then re-raises (the ``_one_fetch_pass`` halt pattern). A
    lake with no option candles skips in O(exchange dirs) with one DEBUG event
    — no wheel import, no rate resolution, no prints (the dominant pure-spot
    CLI case).

    Parameters
    ----------
    lake_root
        The resolved production lake root (targets + driver reads/writes).
    instruments
        The pulled instruments — their
        :func:`~brutex.dashboard.gated_prebuild.underlying_match_tokens`
        union scopes lake discovery to the pulled underlyings.
    start
        Half-open enrichment window start (the pulled start date, IST).
    end
        Half-open enrichment window end (exclusive — the pulled end + 1 day).

    Returns
    -------
    Any
        The pass's ``GreeksEnrichOutcome`` (``GREEKS_NOT_RUN`` on the
        no-option-candles pre-gate skip).

    Raises
    ------
    LakeWriteHalt
        Re-raised after the halt alert — a pull-wide host condition.
    GreeksEnrichPoolBroken
        Re-raised after the halt alert — the parallel pool died.
    """
    import structlog

    from brutex.data.greeks_enrich_pass import (
        CLI_GREEKS_ENRICH_ENV,
        GREEKS_NOT_RUN,
        has_option_candles,
        lake_target_builder,
        run_greeks_enrich_pass,
    )

    if not has_option_candles(lake_root):
        structlog.get_logger("brutex.cli").debug(
            "cli.greeks_enrich.skipped_no_options", lake_root=str(lake_root)
        )
        return GREEKS_NOT_RUN

    from brutex.dashboard.gated_prebuild import underlying_match_tokens
    from brutex.data.errors import GreeksEnrichPoolBroken, LakeWriteHalt

    tokens = frozenset[str]().union(*(underlying_match_tokens(i) for i in instruments))
    print(
        f"[brutex] greeks enrich — post-pull pass ({CLI_GREEKS_ENRICH_ENV}=0 skips)",
        file=sys.stderr,
    )
    try:
        outcome = run_greeks_enrich_pass(
            build_targets=lake_target_builder(lake_root, underlyings=tokens, start=start, end=end),
            lake_root=lake_root,
        )
    except (LakeWriteHalt, GreeksEnrichPoolBroken) as exc:
        # Pull-wide HOST condition mid-enrich: CRITICAL alert then re-raise —
        # exactly the _one_fetch_pass halt convention. emit_pull_halt_alert
        # renders GreeksEnrichPoolBroken via its generic type-name branch —
        # zero notifier changes (DEC-CLI-GREEKS-ENRICH-001).
        _emit_pull_halt_alert(exc)
        raise
    if outcome.ok:
        print(f"[brutex] greeks enrich — done, errored_targets={outcome.errored_targets}")
    else:
        print(
            f"[brutex] greeks enrich FAILED ({outcome.error}) — overlay re-attempted "
            "on the next pull / `brutex enrich-greeks`",
            file=sys.stderr,
        )
    return outcome


def cmd_fetch(
    lake_root: Path | None,
    start: str | None,
    end: str | None,
    interval: str,
    *,
    window_days: int | None,
    dry_run: bool,
    inspect: bool,
    rows_per_chunk: int,
    universe: bool = False,
    instruments: bool = False,
    auto_retry: bool = True,
    retry_failed: bool = False,
    until_clean: bool = False,
    until_clean_rounds: int | None = None,
    max_retry_minutes: int | None = None,
) -> int:
    """Fetch candles for the 3 brute-force indices into the lake (§13.9 step 3).

    Drives the Groww spot spine
    (:func:`~brutex.adapters.groww.run_ingest.run_groww_spot_ingest`): the vendor
    fetches only ``1minute`` + ``1day`` and the 19 intraday timeframes are
    resampled from the 1-minute base (1m-only ingest), so ``--interval all``
    pulls the two base timeframes. ``--dry-run`` uses an in-CLI deterministic
    spot adapter so the full fetch → session-filter → sink pipeline runs offline
    (no creds); the live path resolves the brute-force index ``Instrument``s from
    the store and builds a :class:`~brutex.adapters.groww.adapter.GrowwMarketAdapter`.
    ``--universe`` first ingests the instrument master into the lake (the LAKE
    surface authorised by DEC-P0-063) BEFORE the index candle pull — the turnkey
    "pull everything" command. ``--instruments`` loads ONLY the keyless public
    instrument master (no vendor API keys), prints the index/stock counts, and exits —
    it populates the ``/universe`` list without auth (DEC-INSTRUMENT-LOAD-001).

    Leftover failed windows are AUTO-RETRIED after the pull by default
    (DEC-INGEST-AUTO-RETRY-001): when the pull leaves a transient residual (a
    429-storm leftover), bounded extra rounds re-fetch ONLY the still-failed
    windows via the shared coverage ledger (incremental + idempotent + rate-safe).
    ``--no-auto-retry`` disables that (the prior single-pass behaviour);
    ``--retry-failed`` is the MANUAL trigger that re-pulls ONLY the currently
    failed windows on demand (the coverage ledger skips everything already in the
    lake). Both ways call the SAME
    :func:`~brutex.adapters.ingest_common.retry_failed_windows` engine
    (CLAUDE.md §11 COMMON).

    ``--until-clean`` (DEC-TOKEN-PREFLIGHT-001) keeps running bounded auto-retry
    rounds — each a full pass over the SHARED coverage ledger — until the ledger is
    CLEAN, OR the token dies (stop + the loud ``auth_expired`` verdict), OR the
    ``--until-clean-rounds`` OUTER budget is spent. It REUSES
    :func:`~brutex.adapters.ingest_common.run_until_clean` over the SAME retry
    engine (no duplicated round loop); honest envelope (Golden Rule #10): the round
    count is bounded (NOT infinite), each round is one ``≤ 8 r/s`` pass, and the
    work is ``O(failed_windows)`` per round — never an "O(1)-total" claim.
    """
    if max_retry_minutes is not None and max_retry_minutes < 1:
        print(
            f"[brutex] --max-retry-minutes must be >= 1; got {max_retry_minutes}",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR

    resolved_instruments = _resolve_lake_root(lake_root)
    if instruments:
        print("[brutex] fetch --instruments → keyless public instrument master (no keys)")
        counts = _run_instrument_master_load(resolved_instruments)
        print(
            f"[brutex] instruments loaded — parsed={counts['parsed']} "
            f"filtered={counts['filtered']} quarantined={counts['quarantined']} "
            f"· indices={counts['index_count']} stocks={counts['stock_count']} "
            f"total={counts['total']}"
        )
        print(f"[brutex] lake: {resolved_instruments}")
        return EXIT_OK

    # The pull-wide global stops that HALT every remaining window (disk-full/OOM, a
    # quota stop, a tripped mass-empty circuit breaker, or a TERMINAL dead-credential
    # auth failure — DEC-GROWW-AUTH-HALT-001) — distinct from a per-symbol quarantine.
    # Mirrors the ingest spine's `_GLOBAL_STOP_ERRORS` carve-out
    # (DEC-LAKE-WRITE-HALT-001 / quota / DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001); each
    # fires a best-effort CRITICAL Telegram pull-halt alert then re-raises
    # (DEC-TELEGRAM-PULL-HALT-001). The RETRYABLE `GrowwAuthError` subclasses (a
    # throttled mint / a stale read-only re-read) never alert — carved back out via
    # `is_terminal_auth_failure` at the except site (they re-raise unchanged, exactly
    # as they propagated before this tuple caught them).
    from brutex.adapters.empty_circuit_breaker import MassEmptySettleHalt
    from brutex.adapters.error_storm_breaker import (
        VendorDownError,
        resolve_error_storm_breaker,
    )
    from brutex.adapters.groww.chunks import window_days_for_interval as groww_window_days_for
    from brutex.adapters.groww.errors import (
        GrowwAuthError,
        GrowwCredentialDeadError,
        is_terminal_auth_failure,
    )
    from brutex.adapters.groww.pull_pipeline import (
        build_fetch_pipeline,
        build_write_consumer,
        drain_fetch_pipeline,
        drain_write_consumer,
        reconcile_pairs_with_pipeline,
    )
    from brutex.adapters.groww.run_ingest import run_groww_spot_ingest_with_retry
    from brutex.adapters.ingest_common import GROWW_COVERAGE_MARKET
    from brutex.dashboard.pull_controller import GROWW_HISTORY_FLOOR
    from brutex.data.catalog import Catalog
    from brutex.data.errors import LakeWriteHalt
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import NullBarSink, ParquetBarSink
    from brutex.data.window_ledger_store import (
        begin_pull_ledger,
        end_pull_ledger,
        make_live_ledger_flush,
        make_locked_flush,
        plan_windows,
        reconcile_ledger_with_lake,
    )
    from brutex.orchestrator.quota_tracker import DailyQuotaExceeded, QuotaInvariantError
    from brutex.universe import BRUTE_FORCE_INDICES

    _pull_halt_errors = (
        LakeWriteHalt,
        DailyQuotaExceeded,
        QuotaInvariantError,
        MassEmptySettleHalt,
        # DEC-GROWW-AUTH-HALT-001: a TERMINAL GrowwAuthError halts (the RETRYABLE
        # auth-family subclasses are carved back out at the except site).
        GrowwAuthError,
        # DEC-VENDOR-DOWN-BREAKER-001: a tripped vendor-down error-storm breaker /
        # dead-credential escalation halts the pull the same graceful way
        # (GrowwCredentialDeadError is the TERMINAL auth escalation — a
        # GrowwAuthError subclass, listed explicitly as the breaker's halt class).
        VendorDownError,
        GrowwCredentialDeadError,
    )

    resolved = _resolve_lake_root(lake_root)
    try:
        intervals = _resolve_fetch_intervals(interval)
        # Fail-loud env gate for the post-pull Greeks stage
        # (DEC-CLI-GREEKS-ENRICH-001): resolved HERE — before any vendor work —
        # so a junk BRUTEX_CLI_GREEKS_ENRICH value exits EXIT_USER_ERROR without
        # burning a paced pull (the DEC-RAM-BUDGETS-001 fail-loud env
        # convention; greeks_enrich_pass is wheel-free at import).
        from brutex.data.greeks_enrich_pass import resolve_cli_greeks_enrich

        greeks_enrich_enabled = resolve_cli_greeks_enrich()
    except ValueError as exc:
        print(f"[brutex] {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    # Default the start to the Track-1 2020-01-01 pull floor (DEC-PULL-FLOOR-2020-001,
    # which supersedes the prior conservative 2021-01-01 floor of DEC-DHAN-PULL-SCOPE-001)
    # so a bare `brutex fetch` pulls the full "2020-01-01 → today" vendor-depth history.
    floor_dt = datetime(
        GROWW_HISTORY_FLOOR.year, GROWW_HISTORY_FLOOR.month, GROWW_HISTORY_FLOOR.day, tzinfo=UTC
    )
    start_dt = _parse_dt(start) if start else floor_dt
    end_dt = _parse_dt(end) if end else datetime.now(UTC)
    if window_days is not None:
        print(
            "[brutex] --window-days is ignored on Groww (1-minute is tiled internally)",
            file=sys.stderr,
        )

    if universe:
        # The legacy instruments→expiries→contracts universe ingest was
        # removed with the pre-pivot Groww adapter (DEC-DHAN-FINALIZE-001).
        # The Groww path loads the instrument master keylessly via
        # `fetch --instruments`, and options/contracts are pulled on demand
        # through the dashboard rolling-option fan-out — there is no
        # three-phase ingest.
        print(
            "[brutex] --universe is retired (legacy three-phase ingest removed); "
            "use `brutex fetch --instruments` to load the master, then the "
            "/universe dashboard buttons to pull spot + options",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR

    if inspect:
        print(
            "[brutex] --inspect is not yet ported to the Groww adapter — "
            "running to a NullBarSink (no persistence)",
            file=sys.stderr,
        )

    adapter: Any
    sink: Any
    fetch_instruments: list[Any]
    pace: Any = None
    on_rate_limit: Any = None
    on_timeout: Any = None
    on_success: Any = None
    # The live pull's RateController (DEC-CLI-RATE-BRIDGE-001): held so the per-window
    # ``on_window`` hook can publish the live rate to the cross-process bridge file. Stays
    # ``None`` for --dry-run / --inspect (no real account pacing, nothing to publish).
    rate_controller: Any = None
    # --dry-run lake ISOLATION (DEC-DRYRUN-LAKE-ISOLATION-001): the dry-run's synthetic
    # bars run through the REAL ParquetBarSink pipeline, so its sink + catalog (and the
    # optional retry-storm audit) are routed into an EPHEMERAL system-temp lake instead
    # of the resolved PRODUCTION lake — otherwise the synthetic bars would occupy the
    # keep-first merge slots and PERMANENTLY drop the real vendor bars pulled later.
    # The counts are still reported off the same pipeline; the temp tree is removed
    # best-effort by the outermost `_ephemeral_dryrun_lake_cleanup` context below.
    # `--inspect` already persists nothing (NullBarSink, no catalog) so it needs none.
    dryrun_lake_root: Path | None = None
    if dry_run and not inspect:
        dryrun_lake_root = _make_dryrun_lake_root()
    sink_lake_root = resolved if dryrun_lake_root is None else dryrun_lake_root
    # Persisting pulls register every written partition in the DuckDB catalog
    # so `brutex sweep-all` can read the bars back (DEC-CATALOG-RECONCILE-WIRE-001
    # — historically the sink was built with catalog=None, leaving the catalog
    # empty and the sweep blind). --inspect persists nothing, so it needs no
    # catalog. The catalog's own lock serialises the upserts under the parallel
    # window fan-out (DEC-DHAN-PARALLEL-INGEST-001). A --dry-run catalog lives on
    # the ephemeral temp lake, never the production `catalog.duckdb`.
    cat_ctx: Any = (
        contextlib.nullcontext(None) if inspect else Catalog.open(sink_lake_root / "catalog.duckdb")
    )
    # Golden Rule #8 audit trail (DEC-INGEST-AUDIT-CHAIN-001): a real live pull
    # writes a hash-chained JSONL trail under <lake>/audit/ (RUN_START → per-pair
    # → RUN_COMPLETE). --inspect / --dry-run write no real data, so they get a
    # null context (no audit). The path + resume semantics live in the ONE COMMON
    # ``open_ingest_audit`` factory the dashboard pulls now bind too
    # (DEC-INGEST-AUDIT-WIRING-001) — the CLI behaviour is byte-identical.
    from brutex.orchestrator.audit import open_ingest_audit

    audit_ctx: Any = open_ingest_audit(resolved, enabled=not (dry_run or inspect))
    # Integration merge: keep BOTH the ingest-audit context (base-13,
    # DEC-INGEST-AUDIT-CHAIN-001) AND the retry-storm ExitStack (main, #533).
    # The dry-run temp-lake cleanup is OUTERMOST so it removes the ephemeral tree
    # only AFTER the catalog opened on it is closed (DEC-DRYRUN-LAKE-ISOLATION-001).
    with (
        _ephemeral_dryrun_lake_cleanup(dryrun_lake_root),
        cat_ctx as cat,
        audit_ctx as ingest_audit,
        contextlib.ExitStack() as storm_stack,
    ):
        # Cross-process account single-flight gate (DEC-PULL-HARDENING-FOLLOWUP-001,
        # MIGRATION.md #996): refuse to start if a peer pull (a dashboard pull or another
        # CLI fetch) is already running on this lake — otherwise the Groww ACCOUNT sees the
        # SUM of both pulls' rates (Golden Rule #6). Held for the whole fetch via the
        # ExitStack, released on exit. ONLY the genuine persisting live pull takes it;
        # `--dry-run` / `--inspect` write no real lake data + pace no real account requests,
        # so they deliberately skip the lock (no offline-test / inspect contention).
        if not (dry_run or inspect) and not _enter_pull_singleflight(storm_stack, resolved):
            return EXIT_USER_ERROR
        if dry_run:
            from brutex.adapters.ingest_common import label_synthetic

            fetch_instruments = [_synthetic_index_instrument(spec) for spec in BRUTE_FORCE_INDICES]
            adapter = _DryRunGrowwSpotAdapter(bars_per_window=rows_per_chunk)
            sink = (
                NullBarSink()
                if inspect
                else ParquetBarSink(writer=ParquetWriter(lake_root=sink_lake_root), catalog=cat)
            )
            # Loud, unmistakable label (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001) +
            # the Wave-1 ephemeral-lake note (DEC-PULL-FORENSIC-WAVE1-001): an
            # explicit --dry-run must never read like a real pull, and it names
            # the isolated lake it writes to.
            print(
                f"[brutex] {label_synthetic('fetch --dry-run → synthetic candles')}"
                + (
                    ""
                    if dryrun_lake_root is None
                    else f" (ephemeral lake: {dryrun_lake_root} — production lake untouched)"
                ),
                file=sys.stderr,
            )
        else:
            # Groww auth is TOTP (no legacy 24h JWT + token pre-flight): a
            # missing-creds resolve is the only up-front gate (DEC-DHAN-GROWW-REMIGRATE-001,
            # PR-J2). Refuse loudly BEFORE burning a paced pull when creds are absent —
            # via the ONE gate the dashboard routes also run (CLAUDE.md §11 COMMON),
            # relaying the CredentialsError's OWN fix text verbatim instead of a
            # hardcoded guess at the cause (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001);
            # there is no Groww token-liveness probe to invent (Golden Rule #1).
            from brutex.adapters.ingest_common import (
                credentials_refusal_verdict,
                gate_pull_credentials,
                verdict_message,
            )
            from brutex.config.credentials import (
                CredentialsError,
                groww_credentials_error,
            )

            refusal = gate_pull_credentials(
                dry_run_requested=False, credentials_error=groww_credentials_error
            )
            if refusal is not None:
                print(f"[brutex] {verdict_message(refusal)}", file=sys.stderr)
                return EXIT_USER_ERROR
            fetch_instruments = _resolve_brute_force_instruments(resolved)
            try:
                adapter, sink = _build_live_groww_spot(resolved, catalog=cat)
            except CredentialsError as exc:
                # Race: creds vanished between the gate and the build — the SAME
                # loud refusal with the SAME relayed fix text (never a downgrade).
                print(
                    f"[brutex] {verdict_message(credentials_refusal_verdict(str(exc)))}",
                    file=sys.stderr,
                )
                return EXIT_USER_ERROR
            if inspect:
                sink = NullBarSink()
            # Token-bucket pacing for the live pull. The per-second ceiling is the
            # operator-DIRECTED 8 r/s (constants.GROWW_RATE_LIMIT_RPS_CEILING,
            # raised 5 → 8 on 2026-06-24, DEC-GROWW-RATE-CEILING-8-002) — the
            # backtesting per-second cap itself stays officially UNVERIFIED
            # (Golden Rule #1/#10); 8 is an operator decision, fed as the
            # controller's CEILING into the existing adaptive token-bucket
            # MECHANISM rather than a hardcoded verified rate.
            from brutex.adapters.groww.constants import (
                GROWW_RATE_LIMIT_PER_MIN,
                GROWW_RATE_LIMIT_RPS_CEILING,
                resolve_smooth_pacing_mode,
            )
            from brutex.adapters.rate_common import RateController
            from brutex.notifications.sweep_alerts import SweepAlerter

            # Operator-DIRECTED 8 r/s per-second ceiling (the per-second cap stays
            # officially UNVERIFIED); the operator-confirmed 500/min governor
            # (DEC-GROWW-RATE-PERMIN-001) is the hard per-minute cap. 8×60 = 480 < 500,
            # so the per-second ceiling is the binding gate without fighting the
            # governor; the AIMD ladder still steps DOWN on 429s (raised 5 → 8 on
            # 2026-06-24, DEC-GROWW-RATE-CEILING-8-002). The smooth-pacing mode is
            # threaded HERE too (DEC-SMOOTH-PACING-AUTOLATCH-001 — the CLI previously
            # built the controller WITHOUT it, so BRUTEX_GROWW_SMOOTH_PACING was a
            # dead flag on the `brutex fetch` path): unset ⇒ ADAPTIVE (auto-latch on
            # burst-evidenced 429s), truthy ⇒ ON, explicit falsy ⇒ OFF.
            _rate = RateController(
                ceiling_rps=GROWW_RATE_LIMIT_RPS_CEILING,
                ceiling_per_min=GROWW_RATE_LIMIT_PER_MIN,
                smooth_pacing_mode=resolve_smooth_pacing_mode(),
            )
            # AUTOMATIC rate.breach alert (DEC-TELEGRAM-WIRE-001): wrap the
            # controller's own on_rate_limit so a sustained 429 storm also fires a
            # best-effort Telegram push (coalesced by the notifier's 60 s dedupe).
            # No-op when Telegram is disabled; never blocks the pull.
            _alerter = SweepAlerter()
            pace, on_success = _rate.pace, _rate.on_success
            # Bind the controller's OWN timeout ladder too, mirroring the dashboard's
            # `_live_rate_runtime` hook quad (pace / on_rate_limit / on_timeout /
            # on_success): without it a sustained 5xx/timeout storm on `brutex fetch`
            # never reached `RateController.on_timeout`, so the gentle AIMD step-down
            # (8 → … → TIMEOUT_FLOOR_RPS 1 r/s) was DEAD on the CLI path while the
            # dashboard pull eased off correctly (forensic-audit finding #16).
            on_timeout = _rate.on_timeout
            on_rate_limit = _alerter.rate_breach_callback(_rate)
            # Hold the controller for the cross-process rate-bridge publisher
            # (DEC-CLI-RATE-BRIDGE-001) — its per-window snapshot feeds the dashboard widget.
            rate_controller = _rate
            print("[brutex] fetch (live Groww)", file=sys.stderr)

        # Unified per-window ledger (DEC-WINDOW-LEDGER-001, Stage b3 — R7): the CLI
        # threads the SAME ledger the dashboard does so it is the SOLE skip
        # authority on the `brutex fetch` path too. Load (one-shot migrate) the
        # durable ledger, PLAN every window the pull will touch, and seed SUCCESS
        # for every window the lake already encloses (cold-start) so a re-pull
        # re-fetches ONLY the windows not already settled — never the full history.
        # `--inspect` writes nothing and `--dry-run` writes SYNTHETIC sample bars,
        # so neither touches the real ledger: `_no_persist` passes `dry_run=True`,
        # so the ledger is None (no skip, no disk touch).
        _no_persist = dry_run or inspect
        ledger = begin_pull_ledger(resolved, dry_run=_no_persist)
        if ledger is not None:
            # Seed the durable ledger in the SAME (market, tile-width) namespace the
            # Groww spot spine fetches+records in (DEC-DASHBOARD-GROWW-SPOT-001 fixed
            # the identical bug on the dashboard path): pass GROWW_COVERAGE_MARKET +
            # Groww's 30-day `window_days_for` so the PLANNED keys this seeds == the
            # keys the Groww ingest later settles. Without them `plan_windows` defaults
            # to the default coverage namespace ("spot" + 90-day width), so the planned rows would
            # never match the fetched ones → orphan PLANNED rows / re-pull loops
            # (CLAUDE.md §11 COMMON). `plan_windows` + `reconcile_ledger_with_lake`
            # are themselves 100%-covered in test_window_ledger_store.py; this wiring is
            # exercised by the live-persisting tests in test_cli_retry_storm.py.
            plan_windows(
                ledger,
                instruments=fetch_instruments,
                intervals=intervals,
                start=start_dt.date(),
                end=end_dt.date(),
                market=GROWW_COVERAGE_MARKET,
                window_days_for=groww_window_days_for,
            )
            reconcile_ledger_with_lake(
                ledger,
                resolved,
                instruments=fetch_instruments,
                intervals=intervals,
                start=start_dt.date(),
                end=end_dt.date(),
                market=GROWW_COVERAGE_MARKET,
                window_days_for=groww_window_days_for,
            )
        # `--retry-failed` (the manual trigger) ALWAYS retries on demand; otherwise
        # the post-pull auto-retry is on unless `--no-auto-retry` was passed. Both
        # routes drive the SAME run_groww_spot_ingest_with_retry → retry_failed_windows
        # engine (CLAUDE.md §11 COMMON) — no duplicated round loop.
        do_retry = retry_failed or auto_retry

        # Retry-storm give-up guard (DEC-RATE-STORM-WATCHDOG-001): when
        # `--max-retry-minutes` is set, feed every window outcome through the
        # watchdog so a 429 storm's silent bar-loss becomes a loud, audited,
        # once-per-symbol alert instead of a manual audit-chain diff (Gap #3).
        # No-op (None) when the flag is unset.
        # The storm-guard audit rides the SINK lake root, so a --dry-run's audit
        # lands on the ephemeral temp lake, never the production `<lake>/audit/`
        # (DEC-DRYRUN-LAKE-ISOLATION-001); a live pull is byte-unchanged.
        storm_progress = _arm_retry_storm_guard(
            max_retry_minutes=max_retry_minutes,
            lake_root=sink_lake_root,
            instruments=fetch_instruments,
            intervals=intervals,
            stack=storm_stack,
        )

        # Mid-pull ledger checkpoint (DEC-CLI-LEDGER-CHECKPOINT-001): the CLI fetch
        # path saved the durable ledger ONLY at pull END (`end_pull_ledger`), so a
        # crash after N windows lost every completed window's SUCCESS row and the
        # next run re-pulled the whole span. This threads the SAME throttled
        # `make_live_ledger_flush` helper the dashboard uses (CLAUDE.md §11 COMMON,
        # no copy-paste) as the ingest engine's `on_window` hook (fires per WINDOW
        # from the PARALLEL worker threads) so the durable ledger is flushed mid-pull.
        # `on_window` is concurrent and the throttled flush is not itself thread-safe,
        # so it is wrapped in `make_locked_flush` (single-flight lock). The CLI wants
        # the flush ONLY — not the dashboard bar push — so it deliberately does NOT
        # use `make_live_sync` (which no-ops without an `on_ledger_sync`). A `None`
        # ledger (`--dry-run` / `--inspect`) makes the flush a no-op, so the lock
        # wraps a no-op and nothing touches disk. The save is tmp→atomic-rename and
        # the keep-first sink is idempotent, so a re-pull of an un-flushed window is
        # safe; loss is bounded to one throttle interval, not the whole pull.
        ledger_checkpoint = make_locked_flush(make_live_ledger_flush(ledger, resolved))
        # Cross-process pull-rate VISIBILITY bridge (DEC-CLI-RATE-BRIDGE-001, MIGRATION.md
        # #997): wrap the per-window checkpoint so each processed window ALSO publishes the
        # live RateController + CallMeter snapshot to <lake>/.rate/pull_rate.json, which the
        # dashboard's /api/v1/pull/rate reader surfaces on the live-rate WIDGET — so a CLI
        # `brutex fetch` pull is VISIBLE on the dashboard even though it runs in a separate
        # process. Best-effort cross-process visibility (NOT shared rate accounting; the
        # per-process caps + the single-flight lock are unchanged). A `None` controller
        # (--dry-run / --inspect) returns the checkpoint unwrapped (no bridge written).
        #
        # Pull-health self-instrumentation (DEC-PULL-HEALTH-CLI-WIRING-001, MIGRATION.md
        # #1013): BIND the SAME `PullHealthMonitor` the dashboard pull binds — via the SHARED
        # COMMON builder `pull_controller._build_pull_health_monitor` (CLAUDE.md §11 COMMON, no
        # copy-paste of the registry / `SweepAlerter.pull_degraded` / pool-size wiring) — so a
        # CLI `brutex fetch` pull WATCHES its own health (Prometheus gauges + the `pull_degraded`
        # Telegram alert) exactly like the dashboard. The monitor is fed the SAME per-window
        # controller + call-meter snapshot pair the rate bridge already builds, through the
        # `_make_cli_rate_bridge_on_window` `monitor` seam below (no new tick / thread / cadence).
        # The builder returns `None` for a dry-run / inspect pull (no `rate_controller`) and
        # swallows any construction error → `None`, so binding it can NEVER block, slow, or
        # crash the pull (Golden Rule #10); a `None` monitor makes the observe a no-op.
        from brutex.dashboard.pull_controller import (
            _build_pull_health_monitor,  # pyright: ignore[reportPrivateUsage]
        )

        _pull_health_monitor = _build_pull_health_monitor(rate_controller)
        ledger_checkpoint = _make_cli_rate_bridge_on_window(
            ledger_checkpoint, resolved, rate_controller, adapter, _pull_health_monitor
        )

        # Async fetch/write isolation (DEC-CLI-FETCH-ISOLATION-001): wire the SAME async
        # fetch pipeline (+ batched write consumer) the dashboard pull path uses (CLAUDE.md
        # §11 COMMON — the shared `brutex.adapters.groww.pull_pipeline` builders, no
        # copy-paste), so the per-window tail (session-filter + Parquet write + ledger
        # record) runs on a SEPARATE consumer pool instead of INLINE on the fetch worker
        # thread between every two fetches. Before this, the CLI passed `write_pool=None` /
        # `fetch_pipeline=None`, so the write sat between fetches and slowed the fetch lane —
        # the dashboard had the protection but `brutex fetch` did not. The upstream
        # RateController (per-second AIMD ceiling + the 500/min governor) is UNTOUCHED — it
        # stays on the producer side (Golden Rule #6). `_no_persist` (--dry-run / --inspect)
        # builds NEITHER (no real writes), and the pipeline is kill-switchable via
        # `BRUTEX_GROWW_FETCH_PIPELINE=off`, so both default to the byte-identical synchronous
        # path. The pull OWNS their lifetime + MUST drain them in the `finally` below BEFORE
        # `end_pull_ledger` (settle-after-durable), so every queued window is processed +
        # ledger-recorded first.
        write_consumer = build_write_consumer(sink, dry_run=_no_persist)
        fetch_pipeline = build_fetch_pipeline(dry_run=_no_persist, lake_root=sink_lake_root)
        # Vendor-down error-storm breaker + dead-credential StillStale escalation
        # (DEC-VENDOR-DOWN-BREAKER-001): ONE per-pull breaker shared by every pass /
        # until-clean round (any success resets it, so a recovering vendor never trips).
        # Armed ONLY for a REAL live pull — a ``--dry-run`` (synthetic adapter) threads
        # ``None`` so it can never observe synthetic outcomes (the same gating as
        # ``detect_completeness_gaps`` below).
        storm_breaker = None if dry_run else resolve_error_storm_breaker()
        # Observed-sessions overlay (DEC-CALENDAR-OBSERVED-SESSIONS-001): loaded ONCE
        # per pull and threaded into the completeness detectors' margin arithmetic
        # ONLY (an observed holiday is not a data hole) — NEVER into window planning.
        # A ``--dry-run`` threads the empty overlay (the ``detect_completeness_gaps``
        # gating pattern); the loader never raises (missing/corrupt -> empty).
        from brutex.calendars.observed_sessions import load_observed_holidays

        observed_holidays = frozenset() if dry_run else load_observed_holidays(sink_lake_root)

        def _one_fetch_pass(do_retry_flag: bool) -> Any:
            """Run ONE full spot pass-with-auto-retry over the SHARED window ledger."""
            try:
                return run_groww_spot_ingest_with_retry(
                    adapter,
                    sink,
                    instruments=fetch_instruments,
                    intervals=intervals,
                    start=start_dt.date(),
                    end=end_dt.date(),
                    pace=pace,
                    ledger=ledger,
                    on_rate_limit=on_rate_limit,
                    on_timeout=on_timeout,
                    on_success=on_success,
                    on_progress=storm_progress,
                    on_window=ledger_checkpoint,
                    auto_retry=do_retry_flag,
                    audit=ingest_audit,
                    write_pool=write_consumer,
                    fetch_pipeline=fetch_pipeline,
                    # A REAL live pull (real Groww adapter) OPTS IN to the data-completeness
                    # re-pull demotion; a ``--dry-run`` (the synthetic adapter, whose bars all
                    # land on the window's first day) leaves it OFF so it never false-reds
                    # (DEC-INTERIOR-GAP-DETECT-001 / DEC-PARTIAL-WINDOW-REPULL-001).
                    detect_completeness_gaps=not dry_run,
                    observed_holidays=observed_holidays,
                    storm_breaker=storm_breaker,
                )
            except _pull_halt_errors as exc:
                # A pull-wide global stop (disk-full/OOM LakeWriteHalt, a quota stop, or
                # a TERMINAL dead-credential GrowwAuthError — DEC-GROWW-AUTH-HALT-001)
                # HALTS every remaining window, unlike a per-symbol quarantine — fire a
                # best-effort CRITICAL Telegram alert so the operator knows the pull
                # STOPPED, then re-raise so the existing halt handling (controller
                # verdict / non-zero exit) is byte-unchanged (DEC-TELEGRAM-PULL-HALT-001).
                # A RETRYABLE GrowwAuthError subclass (throttled mint / stale read-only
                # re-read) is NOT a halt — it re-raises unchanged with NO alert, exactly
                # as it propagated before this tuple caught it.
                if not isinstance(exc, GrowwAuthError) or is_terminal_auth_failure(exc):
                    _emit_pull_halt_alert(exc)
                raise

        until_outcome = None
        # Success-path latch for the post-drain L2 lake heal in the ``finally``
        # (DEC-LAKE-HEAL-001 / DEC-CLI-LAKE-HEAL-L2-001 — the CLI twin of
        # pull_controller's ``fetch_phases_ok``): armed as the try body's LAST
        # statement, so a pull-halt raise leaves it False and the heal never
        # runs after an already-alerted halt (alert-once holds).
        fetch_ok = False
        # keep_awake (DEC-PULL-KEEPAWAKE-001): hold the macOS host awake for the
        # whole fetch (including every until-clean round + the drain/persist
        # finally) — a sleeping Mac silently paused a real 6.5 h pull mid-flight.
        # Best-effort: non-darwin / missing caffeinate is a logged no-op.
        from brutex.observability.power_keepawake import keep_awake

        with keep_awake("brutex fetch"):
            try:
                if until_clean:
                    # Re-pull-until-clean (DEC-TOKEN-PREFLIGHT-001): keep running bounded
                    # rounds — each a full pass over the SHARED window ledger (re-fetches
                    # ONLY the still-failed windows; rate-safe + idempotent) — reusing the
                    # SAME retry engine via run_until_clean, until clean / token dead / outer
                    # budget spent. Bounded round count (never an O(1)-total claim). The
                    # driver itself is vendor-neutral (pure 4-way decision + an injected
                    # round_runner) and lives in the vendor-neutral ingest_common home.
                    from brutex.adapters.ingest_common import (
                        DEFAULT_MAX_ROUNDS,
                        UntilCleanRound,
                        run_until_clean,
                    )

                    fetch_passes: list[Any] = []

                    def _round_runner() -> UntilCleanRound:
                        """One until-clean round = one full fetch pass; recorded for the summary.

                        Groww's report has no ``auth_cause`` string (no fail-fast latch — the
                        documented divergence; it derives ``auth_aborted`` from the quarantined
                        failures), so the round leaves ``auth_cause`` at its ``None`` default.
                        """
                        res = _one_fetch_pass(True)
                        fetch_passes.append(res)
                        return UntilCleanRound(
                            still_failed_windows=res.report.still_failed_windows,
                            auth_aborted=res.report.auth_aborted,
                        )

                    rounds_budget = (
                        until_clean_rounds if until_clean_rounds is not None else DEFAULT_MAX_ROUNDS
                    )
                    until_outcome = run_until_clean(_round_runner, max_rounds=rounds_budget)
                    result = fetch_passes[0]
                else:
                    result = _one_fetch_pass(do_retry)
                # Fetch pass(es) returned without raising — arm the L2 heal.
                fetch_ok = True
            finally:
                # Drain the FULL async fetch pipeline FIRST, then the batched write consumer —
                # the SAME ordering the dashboard uses (DEC-CLI-FETCH-ISOLATION-001 mirroring
                # DEC-GROWW-FETCH-ASYNC-SEPARATION-001 MAJOR-4): a pipeline consumer's tail calls
                # `sink.append` which may FEED the write consumer, so the pipeline must be drained
                # (every queued tail run + its ledger row recorded) BEFORE the write consumer is
                # stopped, and BOTH BEFORE `end_pull_ledger` so settle-after-durable holds at pull
                # granularity. Runs on ANY exit (clean return, crash, or a re-raised halt) so a
                # crash flushes the write backlog + joins cleanly. A no-op when either is `None`
                # (the synchronous default / --dry-run). `drain_fetch_pipeline` re-raises a
                # captured disk-full `LakeWriteHalt` (the graceful pull-halt path); any other drain
                # error is swallowed so the ledger persist still runs.
                drain_fetch_pipeline(fetch_pipeline)
                drain_write_consumer(write_consumer)
                # DEC-LAKE-HEAL-001 L2 (CLI seam — DEC-CLI-LAKE-HEAL-L2-001, the #1175
                # named follow-up): heal the just-pulled slice at the SAME post-drain
                # seam the dashboard uses (the fetched 1-minute is durably in the lake
                # only after BOTH drains above), BEFORE end_pull_ledger — mirroring
                # pull_controller.py's drain → heal → ledger-persist ordering — via the
                # SAME glue (CLAUDE.md §11 COMMON, the _build_pull_health_monitor
                # private-import precedent; no copy). SUCCESS-path only (fetch_ok);
                # --dry-run / --inspect stay inert via _no_persist (zero scan, zero
                # writes; the ephemeral dry-run lake is structurally never healed —
                # ``resolved`` is passed, and the gate is closed whenever they differ).
                # The glue swallows every heal error EXCEPT LakeWriteHalt; the CLI has
                # no BackgroundPullJob halt net above this seam, so a heal-raised
                # LakeWriteHalt fires the CRITICAL pull-halt Telegram alert HERE
                # (DEC-TELEGRAM-PULL-HALT-001) then RE-RAISES — non-zero exit via
                # main's raw propagation, byte-identical to an ingest halt. The CLI
                # runs NO prebuild (#1145); its ONE downstream derivation — the
                # post-pull Greeks enrichment (DEC-CLI-GREEKS-ENRICH-001) — runs
                # AFTER this heal, below the `finally`, so the dashboard's
                # heal-L2-BEFORE-Greeks-enrich ordering law holds on this path too.
                # HONEST: the scan is O(files) over pulled-targets x span-months,
                # NOT O(1) (MIGRATION.md #1175).
                if fetch_ok and not _no_persist:
                    from brutex.dashboard.pull_controller import (
                        _heal_after_pull,  # pyright: ignore[reportPrivateUsage]
                    )
                    from brutex.observability.correlation import current_pull_id, new_pull_id

                    try:
                        _heal_after_pull(
                            instruments=fetch_instruments,
                            lake_root=resolved,
                            start=start_dt.date(),
                            end=end_dt.date(),
                            pull_id=current_pull_id() or new_pull_id(),
                        )
                    except LakeWriteHalt as exc:
                        _emit_pull_halt_alert(exc)
                        raise
                end_pull_ledger(ledger, resolved, dry_run=_no_persist)
                # Final bridge publish (DEC-CLI-BRIDGE-PULL-ACTIVE-001): flip pull_active
                # → False so the dashboard widget reads done/idle the moment the pull
                # ends. Best-effort — swallows EVERYTHING (even LakeWriteHalt): the pull
                # is already exiting and its real outcome must never be masked. A None
                # controller (--dry-run / --inspect) is a no-op.
                _publish_cli_pull_inactive(resolved, rate_controller, adapter)
            # ── Post-pull Greeks enrichment (DEC-CLI-GREEKS-ENRICH-001, the #1188 CLI
            # follow-up) ── Seam A: fetch lane fully drained, L2 lake heal already run
            # (the `finally` above — DEC-CLI-LAKE-HEAL-L2-001; the dashboard's
            # heal-BEFORE-enrich ordering law), ledger persisted (settle-after-durable
            # holds), single-flight lock still HELD (no concurrent dashboard pull can
            # start mid-enrich), keep_awake still active. Targets come
            # from the LAKE (the CLI pull is spot-only — no in-memory contract list),
            # bounded to the pulled underlyings × the pulled [start, end) span.
            # DELIBERATELY NOT gated on bars_appended > 0: in async-pipeline mode the
            # per-pair counts are only reconciled AFTER this seam (the
            # reconcile_pairs_with_pipeline fold below), so any such gate would misread
            # 0 — running unconditionally is stamp-idempotent-safe.
            if greeks_enrich_enabled and not _no_persist:
                _run_cli_greeks_enrich(
                    lake_root=resolved,
                    instruments=fetch_instruments,
                    start=start_dt.date(),
                    # Inclusive pull end → half-open, SATURATING at date.max so a
                    # degenerate `--end 9999-12-31` pull can never crash the seam
                    # after the pull completed (review round-1 fix).
                    end=_next_day_saturating(end_dt.date()),
                )
    # Fold the async pipeline's post-drain per-series totals back into the deferred per-pair
    # reports (DEC-GROWW-FETCH-ASYNC-SEPARATION-001 completion-count fix): in async-pipeline
    # mode the producer returned `bars_appended == 0` per series (the durable append + ledger
    # record ran on a BACKGROUND consumer), so the printed `bars_appended` + the exit-code
    # residual below would falsely read 0. The pipeline is DRAINED in the `finally` above, so
    # its per-series totals are final. Guarded on a non-None pipeline so the synchronous /
    # dry-run / --inspect path (where `fetch_pipeline is None` and `reconcile_pairs_with_pipeline`
    # is a no-op) leaves `result` byte-for-byte unchanged — never reconstructing the report.
    if fetch_pipeline is not None:
        result = replace(
            result,
            report=replace(
                result.report,
                pairs=reconcile_pairs_with_pipeline(result.report.pairs, fetch_pipeline),
            ),
        )
    report = result.report
    retry = result.retry
    print(
        f"[brutex] fetch done — windows={report.windows} bars_appended={report.bars_appended} "
        f"failed_pairs={report.failed_pairs} transient={report.transient_failures}"
    )
    if retry is not None:
        print(
            f"[brutex] auto-retry — rounds={retry.rounds_run} "
            f"recovered_windows={retry.recovered_windows} "
            f"still_failed={retry.final_still_failed}"
        )
    if until_outcome is not None:
        verdict = until_outcome.pull_verdict()
        print(
            f"[brutex] until-clean — verdict={until_outcome.verdict} "
            f"rounds={until_outcome.rounds_run} "
            f"still_failed={until_outcome.final_still_failed}"
        )
        if verdict.needs_operator:
            print(f"[brutex] {verdict.headline} {verdict.action}", file=sys.stderr)
    _lake_note = "" if dryrun_lake_root is None else " (dry-run — production lake untouched)"
    print(f"[brutex] lake: {resolved}{_lake_note}")
    # With --until-clean the FINAL residual (post all rounds) is the truth, not the
    # first pass's; otherwise the wrapper's still_failed_windows is correct.
    residual = (
        until_outcome.final_still_failed
        if until_outcome is not None
        else result.still_failed_windows
    )
    if report.failed_pairs > 0 or residual > 0:
        return EXIT_INTERNAL_ERROR
    return EXIT_OK


def cmd_enrich_greeks(
    lake_root: Path | None,
    *,
    underlying: list[str] | None,
    start: str | None,
    end: str | None,
    dry_run: bool,
) -> int:
    """Bulk (re-)enrich stored option contracts with Greeks — the #1188 follow-up.

    Discovery =
    :func:`~brutex.data.greeks_enrich_pass.discover_option_windows` over the
    WHOLE lake (or the ``--underlying`` / ``--start``/``--end`` slice; dates
    INCLUSIVE at the CLI, the end widened ``+1 day`` internally to the
    half-open window). ``--dry-run`` prints discovery counts only — wheel-free,
    zero writes, zero locks, zero ``fno.greeks_enrich.*`` events. Live: takes
    the cross-process pull single-flight lock (deterministic refusal under a
    peer pull), runs the COMMON fenced pass under ``keep_awake``, and alerts +
    re-raises halts (the ``_one_fetch_pass`` pattern). The clean-done summary
    echoes ``contracts=N`` (the fenced build's ACTUAL target count — captured
    by wrapping the builder, never a second discovery walk; the ``?`` leg is
    a DEFENSIVE belt only — with the real pass, ``ok=True`` implies the build
    already ran, and a pre-build failure reads ``ok=False`` → the FAILED
    branch, so ``?`` is production-unreachable and guards a future
    pass-contract change, never fabricating a 0), and an explicit ``--underlying``
    that matched ZERO stored option contracts prints a loud stderr WARNING
    naming the tokens — matching is exact + case-sensitive, so ``nifty``
    never matches stored ``NIFTY`` (review round-4 fix, the one-level-down
    sibling of the typo'd ``--lake-root`` guard). Exit: :data:`EXIT_OK`
    iff ``ok is True`` AND ``errored_targets == 0``; :data:`EXIT_INTERNAL_ERROR`
    otherwise (the ``lake audit-unique`` cron-visible precedent — a DELIBERATE,
    documented divergence from the never-fail ``fetch`` pull path).

    Parameters
    ----------
    lake_root
        ``--lake-root`` override; ``None`` resolves via ``$BRUTEX_LAKE_ROOT``
        → ``~/.brutex/lake``.
    underlying
        Repeatable bare underlying tokens (``NIFTY``, ``BAJAJ-AUTO``);
        ``None`` = every stored option underlying.
    start
        Optional inclusive IST window start, ``YYYY-MM-DD``.
    end
        Optional INCLUSIVE IST window end, ``YYYY-MM-DD`` (``+1 day``
        internally; ``9999-12-31`` = ``date.max`` is refused as a clean
        user error — the widening would overflow, review round-1 fix).
    dry_run
        Discovery-only counts; enrich nothing, take no lock.

    Returns
    -------
    int
        Process exit code (see above).

    Raises
    ------
    CliUserError
        If the resolved lake root is not an existing directory — the
        DEC-CLI-USER-ERROR-HYGIENE-001 read-command guard (a typo'd
        ``--lake-root`` must never read as a green zero-target run, nor
        materialise a phantom ``.locks/`` tree; review round-1 fix).
    """
    from brutex.data.greeks_enrich_pass import (
        discover_option_windows,
        lake_target_builder,
        run_greeks_enrich_pass,
    )

    resolved = _resolve_lake_root(lake_root)
    # Refuse a nonexistent lake root LOUDLY (dry-run included): enrich-greeks
    # READS an existing lake, and a typo'd --lake-root previously exited green
    # ("errored_targets=0") while the live path even materialised the phantom
    # lake's .locks/ dir — the exact silent-illusion class _require_lake_dir
    # exists to prevent (review round-1 fix, DEC-CLI-USER-ERROR-HYGIENE-001).
    _require_lake_dir(resolved, command="enrich-greeks")
    try:
        start_d = date.fromisoformat(start) if start else None
        end_given = date.fromisoformat(end) if end else None
    except ValueError as exc:
        print(f"[brutex] {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    if start_d is not None and end_given is not None and start_d > end_given:
        print(
            f"[brutex] --start {start_d} is after --end {end_given}",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR
    if end_given is not None and end_given == date.max:
        # date.max is the ONE valid ISO date the +1-day widening below cannot
        # represent (OverflowError) — refuse it as cleanly as every other
        # malformed date instead of a raw traceback (review round-1 fix).
        print(
            f"[brutex] --end {end_given.isoformat()} is out of range: the "
            "inclusive end is widened +1 day internally and no later date "
            "exists — pass a real trading date",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR
    end_d = end_given + timedelta(days=1) if end_given is not None else None
    tokens = frozenset(underlying) if underlying else None
    span = f"[{start_d.isoformat() if start_d else 'lake-start'} → " + (
        f"{end_d.isoformat()})" if end_d else "lake-end)"
    )

    if dry_run:
        try:
            windows = discover_option_windows(
                resolved, underlyings=tokens, start=start_d, end=end_d
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            print(f"[brutex] enrich-greeks --dry-run discovery FAILED: {exc}", file=sys.stderr)
            return EXIT_INTERNAL_ERROR
        chains = len({(w.exchange, w.underlying, w.expiry) for w in windows})
        print(
            f"[brutex] enrich-greeks --dry-run — contracts={len(windows)} "
            f"chains={chains} window={span} (nothing enriched)"
        )
        return EXIT_OK

    from brutex.data.errors import GreeksEnrichPoolBroken, LakeWriteHalt
    from brutex.observability.power_keepawake import keep_awake

    build_targets = lake_target_builder(resolved, underlyings=tokens, start=start_d, end=end_d)
    # contracts = how many targets the fenced build ACTUALLY put in scope,
    # captured by wrapping the builder — NEVER a second discovery walk
    # (discovery is O(option partition dirs); doubling it would be a hidden
    # cost, Golden Rule #10). -1 = the build never ran. With the REAL pass
    # this sentinel cannot survive to the clean-done render: ok=True is
    # returned only AFTER list(build_targets()) completed, and a pre-build
    # failure reads ok=False → the FAILED branch below. The "?" render is
    # therefore a DEFENSIVE belt against a future pass-contract change —
    # a count the pass did not produce is never fabricated as 0.
    contracts = -1

    def _counting_build() -> Sequence[GreeksEnrichTarget]:
        """Delegate to the fence-safe builder, capturing the target count."""
        nonlocal contracts
        targets = build_targets()
        contracts = len(targets)
        return targets

    with contextlib.ExitStack() as stack:
        if not _enter_pull_singleflight(stack, resolved):
            return EXIT_USER_ERROR
        with keep_awake("brutex enrich-greeks"):
            print(f"[brutex] enrich-greeks — window={span} lake: {resolved}", file=sys.stderr)
            try:
                outcome = run_greeks_enrich_pass(
                    build_targets=_counting_build,
                    lake_root=resolved,
                )
            except (LakeWriteHalt, GreeksEnrichPoolBroken) as exc:
                # A pull-wide HOST condition (disk full / OOM / a dead pool
                # worker): CRITICAL alert then re-raise — the _one_fetch_pass
                # halt convention (DEC-CLI-GREEKS-ENRICH-001).
                _emit_pull_halt_alert(exc)
                raise
    if outcome.ok is True and tokens is not None and contracts == 0:
        # An explicit --underlying that matched NOTHING must never read as a
        # silent green run — matching is exact + case-sensitive ("nifty" never
        # matches stored NIFTY), the one-level-down sibling of the typo'd
        # --lake-root guard (review round-4 fix, DEC-CLI-GREEKS-ENRICH-001).
        # Exit code deliberately unchanged: the pass genuinely completed.
        print(
            "[brutex] WARNING: --underlying matched ZERO stored option contracts "
            f"(tokens: {', '.join(sorted(tokens))}) — matching is exact and "
            "case-sensitive ('NIFTY', not 'nifty'); check the spelling/case or "
            "run --dry-run to inspect the scope",
            file=sys.stderr,
        )
    if outcome.ok is True and outcome.errored_targets == 0:
        shown = str(contracts) if contracts >= 0 else "?"
        print(f"[brutex] enrich-greeks done — contracts={shown} errored_targets=0")
        return EXIT_OK
    if outcome.ok is True:
        print(
            f"[brutex] enrich-greeks completed with {outcome.errored_targets} quarantined "
            "target(s) — see the greeks_enrich.target_errored logs; the stamp-driven pass "
            "is idempotent, re-run after fixing",
            file=sys.stderr,
        )
        return EXIT_INTERNAL_ERROR
    print(f"[brutex] enrich-greeks FAILED ({outcome.error})", file=sys.stderr)
    return EXIT_INTERNAL_ERROR


# ────────────────────────────────────────────────────────────────────────
# nightly (DEC-SWEEP-NIGHTLY-AUTOMATION-001) — thin, fully-covered shims
# over the 100%-tested brutex.nightly modules (the cmd_enrich_greeks template).
# ────────────────────────────────────────────────────────────────────────

#: The unattended-safety trio HARD-REFUSED on ``nightly run`` — STRICTER than
#: the sweep-daemon precedent, which dispatch-refuses only --allow-out-of-scope.
_NIGHTLY_FORBIDDEN_FLAGS: Final[tuple[tuple[str, str], ...]] = (
    ("allow_out_of_scope", "--allow-out-of-scope"),
    ("acknowledge_sla_overrun", "--acknowledge-sla-overrun"),
    ("allow_missing_slices", "--allow-missing-slices"),
)


def _launchctl_runner(argv: Sequence[str]) -> tuple[int, str, str]:
    """The production CommandRunner: run *argv*, return ``(rc, stdout, stderr)``."""
    import subprocess  # nosec B404 — fixed launchctl argv, no shell/user input

    try:
        completed = subprocess.run(  # noqa: S603 — fixed launchctl argv, no shell/user input
            list(argv), capture_output=True, text=True, check=False, timeout=30.0
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return (127, "", str(exc))
    return (completed.returncode, completed.stdout, completed.stderr)


def _default_plist_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def cmd_nightly_run(args: argparse.Namespace) -> int:
    """Run the whole nightly chain once (the launchd/cron entry point)."""
    for attr, flag in _NIGHTLY_FORBIDDEN_FLAGS:
        if getattr(args, attr, False):
            print(
                f"[brutex] nightly never overrides safety gates ({flag}) — run "
                "sweep-all manually for a supervised override",
                file=sys.stderr,
            )
            return EXIT_USER_ERROR
    resolved = _resolve_lake_root(args.lake_root)
    _require_lake_dir(resolved, command="nightly run")

    from brutex.dashboard.pipeline_pull import strict_gate_enabled
    from brutex.dashboard.pull_controller import default_date_range
    from brutex.nightly.backup import (
        default_backup_root,
        resolve_backup_keep,
        resolve_backup_verify,
    )
    from brutex.nightly.probes import NightlyEnvError, resolve_sweep_fresh_seconds
    from brutex.nightly.report import summary_lines
    from brutex.nightly.run import NightlyPlan, run_nightly
    from brutex.nightly.sweep_binding import nightly_sweep_kwargs

    # Every env knob parses FAIL-LOUD pre-chain (the resolve_cli_greeks_enrich
    # precedent) — a junk value is a clean EXIT_USER_ERROR, never a mid-chain 500.
    try:
        keep = args.keep if args.keep is not None else resolve_backup_keep()
        if keep < 1:
            raise NightlyEnvError(f"--keep must be >= 1, got {keep}")
        resolve_backup_verify()
        resolve_sweep_fresh_seconds()
        strict_gate_enabled()
        from_dt = date.fromisoformat(args.from_date) if args.from_date else None
        to_dt = date.fromisoformat(args.to_date) if args.to_date else None
        if from_dt is not None and to_dt is not None and from_dt > to_dt:
            raise ValueError(f"--from-date {args.from_date} is after --to-date {args.to_date}")
    except (NightlyEnvError, ValueError) as exc:
        print(f"[brutex] {exc}", file=sys.stderr)
        return EXIT_USER_ERROR

    results_root = args.results_root if args.results_root is not None else resolved / "sweeps"
    backup_root = args.backup_root if args.backup_root is not None else default_backup_root()
    start_default, end_default = default_date_range()
    plan = NightlyPlan(
        lake_root=resolved,
        results_root=results_root,
        backup_root=backup_root,
        keep=keep,
        from_date=args.from_date,
        to_date=args.to_date,
        sweep_kwargs=nightly_sweep_kwargs(
            args, lake_root=resolved, start_default=start_default, end_default=end_default
        ),
        skip_backup=args.skip_backup,
        skip_sweep=args.skip_sweep,
        dry_run=args.dry_run,
    )
    report = run_nightly(plan)
    lines, _verdict = summary_lines(report)
    for line in lines:
        print(line)
    return report.exit_code


def cmd_nightly_backup(
    lake_root: Path | None,
    *,
    results_root: Path | None,
    backup_root: Path | None,
    keep: int | None,
) -> int:
    """One-shot COLLISION + DISK + BACKUP links (same locks as the chain)."""
    resolved = _resolve_lake_root(lake_root)
    _require_lake_dir(resolved, command="nightly backup")
    from brutex.adapters.groww.pull_singleflight import (
        NightlyAlreadyRunningError,
        nightly_singleflight_lock,
    )
    from brutex.nightly.backup import (
        BackupManifestError,
        backup_inventory,
        create_snapshot,
        default_backup_root,
        estimate_backup_bytes,
        resolve_backup_keep,
    )
    from brutex.nightly.probes import (
        NightlyEnvError,
        check_disk_floors,
        probe_sweep_liveness,
        resolve_sweep_fresh_seconds,
    )
    from brutex.nightly.report import mint_nightly_id, open_nightly_audit

    results = results_root if results_root is not None else resolved / "sweeps"
    broot = backup_root if backup_root is not None else default_backup_root()
    try:
        keep_resolved = keep if keep is not None else resolve_backup_keep()
        if keep_resolved < 1:
            raise NightlyEnvError(f"--keep must be >= 1, got {keep_resolved}")
        fresh_seconds = resolve_sweep_fresh_seconds()
    except NightlyEnvError as exc:
        print(f"[brutex] {exc}", file=sys.stderr)
        return EXIT_USER_ERROR

    nightly_id = mint_nightly_id()
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(
                nightly_singleflight_lock(resolved, holder_note=f"nightly:{nightly_id}")
            )
        except NightlyAlreadyRunningError as exc:
            print(f"[brutex] {exc}", file=sys.stderr)
            return EXIT_USER_ERROR
        if not _enter_pull_singleflight(stack, resolved):
            return EXIT_USER_ERROR
        liveness = probe_sweep_liveness(results, fresh_seconds=fresh_seconds)
        if liveness.live:
            print(
                f"[brutex] a sweep appears to be RUNNING ({liveness.evidence}) — "
                "never back up a live results tree; stop the sweep first",
                file=sys.stderr,
            )
            return EXIT_USER_ERROR
        items, _absent = backup_inventory(resolved, results, exclude_nightly_id=None)
        verdict = check_disk_floors(
            lake_root=resolved, backup_root=broot, inventory_bytes=estimate_backup_bytes(items)
        )
        if not verdict.ok:
            print(f"[brutex] {verdict.detail}", file=sys.stderr)
            return EXIT_USER_ERROR
        try:
            result = create_snapshot(
                lake_root=resolved,
                results_root=results,
                backup_root=broot,
                snapshot_id=nightly_id,
                nightly_id=nightly_id,
                keep=keep_resolved,
            )
        except (BackupManifestError, OSError) as exc:
            print(f"[brutex] backup FAILED: {exc}", file=sys.stderr)
            return EXIT_INTERNAL_ERROR
        with open_nightly_audit(results) as audit:
            audit.append(
                action="BACKUP_SNAPSHOT",
                subject=f"nightly:{nightly_id}",
                payload={"snapshot": str(result.snapshot_path), "files": result.file_count},
            )
            if result.pruned:
                audit.append(
                    action="BACKUP_PRUNE",
                    subject=f"nightly:{nightly_id}",
                    payload={"pruned": list(result.pruned)},
                )
    print(
        f"[brutex] snapshot {result.snapshot_id} SEALED at {result.snapshot_path} "
        f"({result.file_count} files, {result.total_bytes} bytes"
        + (f"; pruned {len(result.pruned)}" if result.pruned else "")
        + ")"
    )
    return EXIT_OK


def cmd_nightly_restore(
    *,
    snapshot: Path,
    lake_root: Path | None,
    results_root: Path | None,
    dry_run: bool,
    yes: bool,
) -> int:
    """Verify-everything-then-write restore (locks held; ``--yes`` gated)."""
    resolved = _resolve_lake_root(lake_root)
    _require_lake_dir(resolved, command="nightly restore")
    from brutex.adapters.groww.pull_singleflight import (
        NightlyAlreadyRunningError,
        nightly_singleflight_lock,
    )
    from brutex.nightly.probes import probe_sweep_liveness, resolve_sweep_fresh_seconds
    from brutex.nightly.report import mint_nightly_id, open_nightly_audit
    from brutex.nightly.restore import (
        RestoreRefused,
        RestoreVerifyError,
        plan_restore,
        restore_snapshot,
        verify_snapshot,
    )

    results = results_root if results_root is not None else resolved / "sweeps"
    try:
        plan = plan_restore(snapshot, lake_root=resolved, results_root=results)
    except RestoreRefused as exc:
        print(f"[brutex] {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    print(f"[brutex] restore plan for {snapshot.name}: {len(plan.items)} file(s), in order:")
    for entry in plan.items:
        print(f"[brutex]   {entry.restore_order} {entry.namespace}/{entry.relpath}")
    for key in plan.skipped_live_audit:
        print(
            f"[brutex]   SKIP {key} (live cross-run nightly audit chain — "
            "never overwritten by restore; legacy snapshot)"
        )

    if dry_run:
        verification = verify_snapshot(snapshot)
        if verification.ok:
            print(f"[brutex] verify OK — {verification.checked} file(s) match the manifest")
            return EXIT_OK
        print(
            f"[brutex] verify FAILED — {len(verification.mismatches)} mismatch(es): "
            + ", ".join(verification.mismatches[:10]),
            file=sys.stderr,
        )
        return EXIT_VERIFY_FINDINGS

    if not yes:
        print(
            "[brutex] refusing to restore without --yes (a live restore REPLACES "
            "the stores above — the 6 a.m. safety gate)",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR

    nightly_id = mint_nightly_id()
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(
                nightly_singleflight_lock(resolved, holder_note=f"nightly:{nightly_id}")
            )
        except NightlyAlreadyRunningError as exc:
            print(f"[brutex] {exc}", file=sys.stderr)
            return EXIT_USER_ERROR
        if not _enter_pull_singleflight(stack, resolved):
            return EXIT_USER_ERROR
        liveness = probe_sweep_liveness(results, fresh_seconds=resolve_sweep_fresh_seconds())
        if liveness.live:
            print(
                f"[brutex] a sweep appears to be RUNNING ({liveness.evidence}) — "
                "stop the dashboard/pull/sweep first",
                file=sys.stderr,
            )
            return EXIT_USER_ERROR
        with open_nightly_audit(results) as audit:
            audit.append(
                action="RESTORE_START",
                subject=f"nightly:{nightly_id}",
                payload={"snapshot": str(snapshot)},
            )
            try:
                outcome = restore_snapshot(plan)
            except RestoreVerifyError as exc:
                print(f"[brutex] {exc}", file=sys.stderr)
                return EXIT_VERIFY_FINDINGS
            except RestoreRefused as exc:
                print(f"[brutex] {exc}", file=sys.stderr)
                return EXIT_USER_ERROR
            except OSError as exc:
                print(
                    f"[brutex] restore FAILED mid-copy ({exc}) — the store set is mixed "
                    "old/new; RE-RUN this restore (idempotent) to converge",
                    file=sys.stderr,
                )
                return EXIT_INTERNAL_ERROR
            audit.append(
                action="RESTORE_DONE",
                subject=f"nightly:{nightly_id}",
                payload={
                    "restored": outcome.restored,
                    "deltas_cleared": outcome.deltas_cleared,
                    "ledger_restored_last": outcome.ledger_restored_last,
                },
            )
    print(
        f"[brutex] restored {outcome.restored} file(s) ({outcome.total_bytes} bytes); "
        f"deltas cleared {outcome.deltas_cleared}; ledger restored last: "
        f"{outcome.ledger_restored_last}"
    )
    print(
        "[brutex] follow-up: run `brutex doctor`; bars↔ledger drift since the snapshot "
        "reconciles via reconcile_ledger_with_lake / seed_lake_success "
        "(docs/runbooks/lake-backup-restore.md)"
    )
    return EXIT_OK


def cmd_nightly_install(
    *,
    at: str,
    label: str,
    plist_dir: Path | None,
    lake_root: Path | None,
    backup_root: Path | None,
    runner: Callable[[Sequence[str]], tuple[int, str, str]] | None = None,
    platform: str | None = None,
) -> int:
    """One-time launchd bootstrap with the MANDATORY post-install verification."""
    from brutex.nightly.schedule import (
        CRON_GUIDANCE,
        install,
        parse_at,
        resolve_program_arguments,
    )

    plat = sys.platform if platform is None else platform
    if plat != "darwin":
        print(
            "[brutex] nightly install targets macOS launchd; on Linux schedule via cron:",
            file=sys.stderr,
        )
        print(f"[brutex]   {CRON_GUIDANCE}", file=sys.stderr)
        return EXIT_USER_ERROR
    try:
        hour, minute = parse_at(at)
    except ValueError as exc:
        print(f"[brutex] {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    env_vars: dict[str, str] = {}
    if lake_root is not None:
        env_vars["BRUTEX_LAKE_ROOT"] = str(lake_root)
    if backup_root is not None:
        env_vars["BRUTEX_NIGHTLY_BACKUP_ROOT"] = str(backup_root)
    outcome = install(
        label=label,
        plist_dir=plist_dir if plist_dir is not None else _default_plist_dir(),
        runner=runner if runner is not None else _launchctl_runner,
        program_arguments=resolve_program_arguments(),
        hour=hour,
        minute=minute,
        env_vars=env_vars,
        log_dir=Path.home() / ".brutex" / "logs",
    )
    if not outcome.ok:
        print(f"[brutex] {outcome.detail}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR
    print(f"[brutex] {outcome.detail}")
    print(
        "[brutex] sleep/wake honesty: launchd does NOT wake a sleeping Mac — a fire "
        "missed while asleep runs ONCE at next wake; powered off = skipped. For "
        "guaranteed-on-time fires run (one-time, yourself): "
        "sudo pmset repeat wakeorpoweron MTWRFSU 01:25:00"
    )
    return EXIT_OK


def cmd_nightly_uninstall(
    *,
    label: str,
    plist_dir: Path | None,
    runner: Callable[[Sequence[str]], tuple[int, str, str]] | None = None,
) -> int:
    """Unload + remove the LaunchAgent, verifying launchctl no longer lists it."""
    from brutex.nightly.schedule import uninstall

    outcome = uninstall(
        label=label,
        plist_dir=plist_dir if plist_dir is not None else _default_plist_dir(),
        runner=runner if runner is not None else _launchctl_runner,
    )
    if not outcome.ok:
        print(f"[brutex] {outcome.detail}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR
    print(f"[brutex] {outcome.detail}")
    return EXIT_OK


def cmd_nightly_status(
    *,
    lake_root: Path | None,
    results_root: Path | None,
    label: str,
    plist_dir: Path | None,
    runner: Callable[[Sequence[str]], tuple[int, str, str]] | None = None,
) -> int:
    """The 6 a.m. command — honest status lines even when nothing has run yet."""
    resolved = _resolve_lake_root(lake_root)
    _require_lake_dir(resolved, command="nightly status")
    from brutex.nightly.backup import default_backup_root
    from brutex.nightly.schedule import status

    report = status(
        label=label,
        plist_dir=plist_dir if plist_dir is not None else _default_plist_dir(),
        lake_root=resolved,
        results_root=results_root if results_root is not None else resolved / "sweeps",
        backup_root=default_backup_root(),
        runner=runner if runner is not None else _launchctl_runner,
    )
    for line in report.lines():
        print(f"[brutex] {line}")
    return EXIT_OK


def cmd_wipe(*, execute: bool, assume_yes: bool) -> int:  # pragma: no cover — interactive + real FS
    """Delete all persisted BruteX data — dry-run by default, typed-confirm to execute.

    Z+ defensive layers (DEC-DATA-WIPE-001): DETECT (dry-run preview), PREVENT
    (live-dashboard guard + strict-allowlist refusal + typed ``WIPE`` confirm),
    VERIFY (:func:`brutex.wipe.assert_safe` re-checks every target at delete
    time), RECONCILE/RECOVER (``WipeReport`` removed/absent/failed,
    continue-past-failure), AUDIT (the plan is printed + structlog-logged before
    any deletion). The pure decision core is the 100%-tested :mod:`brutex.wipe`.
    """
    import structlog

    from brutex.config.runtime import is_pid_alive, read_runtime_file
    from brutex.wipe import execute_wipe, server_is_running, wipe_targets

    log = structlog.get_logger("brutex.wipe")
    home = Path.home()
    cwd = Path.cwd()
    targets = wipe_targets(os.environ, home=home, cwd=cwd)

    # PREVENT: never wipe an open lake out from under a live dashboard.
    _runtime = read_runtime_file()
    if server_is_running(_runtime.pid if _runtime is not None else None, pid_alive=is_pid_alive):
        print(
            "[brutex] a BruteX dashboard is RUNNING — stop it first, then re-run "
            "`brutex wipe` (it holds the lake open on disk).",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR

    # DETECT + AUDIT: show + log the plan before touching anything.
    preview = execute_wipe(targets, home=home, cwd=cwd, dry_run=True)
    print("[brutex] wipe plan (existing targets will be deleted):", file=sys.stderr)
    for path in preview.removed:
        print(f"  - DELETE   {path}", file=sys.stderr)
    for path in preview.absent:
        print(f"  - (absent) {path}", file=sys.stderr)
    for path, reason in preview.failed:
        print(f"  - REFUSED  {path}  ({reason})", file=sys.stderr)
    log.info("wipe.plan", to_delete=[str(p) for p in preview.removed], execute=execute)

    if not execute:
        print(
            "[brutex] DRY-RUN — nothing deleted. Re-run with `--execute` to delete.",
            file=sys.stderr,
        )
        return EXIT_OK

    # PREVENT: irreversible → require a typed confirmation on an interactive TTY.
    if not assume_yes:
        if not sys.stdin.isatty():
            print("[brutex] refusing to wipe non-interactively without --yes.", file=sys.stderr)
            return EXIT_USER_ERROR
        if input("Type WIPE to permanently delete the above: ").strip() != "WIPE":
            print("[brutex] aborted (confirmation was not 'WIPE').", file=sys.stderr)
            return EXIT_USER_ERROR

    report = execute_wipe(targets, home=home, cwd=cwd, dry_run=False)
    log.info(
        "wipe.done",
        removed=[str(p) for p in report.removed],
        failed=[str(p) for p, _ in report.failed],
    )
    print(f"[brutex] wiped {len(report.removed)} target(s).", file=sys.stderr)
    for path, reason in report.failed:
        print(f"[brutex] FAILED {path}: {reason}", file=sys.stderr)
    return EXIT_INTERNAL_ERROR if report.failed else EXIT_OK


def cmd_sweep(
    *,
    lake_root: Path | None,
    catalog_path: Path | None,
    exchange: str,
    segment: str,
    symbol: str,
    interval: str,
    start: str,
    end: str,
    direction: str,
    k: int,
    conditions: str | None,
    stop_loss_paisa: int | None,
    take_profit_paisa: int | None,
    top: int,
    min_hits: int | None,
    sort_by: str,
    validate_oos: bool,
    oos_only: bool,
    allow_out_of_scope: bool = False,
) -> int:
    """Brute-force sweep over lake bars → print the ranked top-combo table.

    Loads the bars for one ``(symbol, interval)`` window from the Parquet lake via
    the SAME ``Catalog`` / ``ReplayFeed`` path the engine uses, runs the assembled
    funnel (sweep → Stage-1B prune → hard gates → composite rank), and prints the
    self-describing leaderboard (``brutex.engine.sweep_report``). Signal-only —
    nothing is persisted (DEC-SWEEP-CLI-001). ``--exchange`` / ``--segment`` /
    ``--interval`` are the LAKE partition labels and must match what was ingested
    (segment ``INDEX`` for the spot indexes — the lake partition label, NOT the Groww
    API's ``IDX_I`` request segment).
    """
    from brutex.data.catalog import Catalog
    from brutex.data.timeframes import trading_minutes_for_lake_interval
    from brutex.engine.sweep_report import sweep_report

    # Engine-scope coordinate law (DEC-ENGINE-SCOPE-GUARD-001, Layer 2):
    # refuse an out-of-engine-scope launch BEFORE any lake I/O. Report-only
    # command, but an out-of-scope brute-force IS the violation.
    from brutex.engine.sweep_scope import check_sweep_scope
    from brutex.feed.replay import ReplayFeed
    from brutex.ranking import MIN_TRADES
    from brutex.strategies.types import Side

    scope = check_sweep_scope(
        [symbol], exchange=exchange, segment=segment, allow_out_of_scope=allow_out_of_scope
    )
    if not scope.ok:
        print(f"[brutex] {scope.refusal_message()}", file=sys.stderr)
        return EXIT_USER_ERROR
    if scope.overridden:
        import structlog

        print(f"[brutex] {scope.override_warning()}")
        structlog.get_logger("brutex.sweep").warning(
            "sweep.scope_override",
            symbols=[verdict.symbol for verdict in scope.refused],
            exchange=exchange,
            segment=segment,
        )

    resolved_lake = _resolve_lake_root(lake_root)
    catalog = catalog_path if catalog_path is not None else resolved_lake / "catalog.duckdb"
    # Trading minutes, not wall-clock (1day -> 375-min session, NOT 1440 — DEC-TF-VOCAB-21-001).
    tf_minutes = trading_minutes_for_lake_interval(interval)
    side = Side.BUY if direction.upper() == "BUY" else Side.SELL
    condition_bits = parse_condition_ids(conditions) if conditions else default_condition_bits()
    _require_catalog_exists(catalog)
    with Catalog.open(catalog, require_exists=True) as cat:
        feed = ReplayFeed(
            catalog=cat,
            lake_root=resolved_lake,
            exchange=exchange,
            segment=segment,
            symbols=[symbol],
            interval=interval,
            start=_parse_dt(start),
            end=_parse_dt(end),
        )
        bars = [event.bar for event in feed]
    if not bars:
        print(
            f"[brutex] no bars for {symbol} {interval} {start}→{end} "
            f"under {exchange}/{segment} (lake={resolved_lake})",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR
    try:
        report = sweep_report(
            bars,
            instrument=symbol,
            tf_label=interval,
            tf_minutes=tf_minutes,
            direction=side,
            condition_bits=condition_bits,
            k=k,
            stop_loss_paisa=stop_loss_paisa,
            take_profit_paisa=take_profit_paisa,
            top_n=top,
            min_hits=MIN_TRADES if min_hits is None else min_hits,
            sort_by=sort_by,
            validate_oos=validate_oos,
            oos_only=oos_only,
        )
    except ValueError as exc:
        # Honest user errors (over-cap sweep, bad k, --oos-only on a < 2-day window)
        # → a clear message + non-zero exit, never a traceback.
        print(f"[brutex] sweep error: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    print(report)
    return EXIT_OK


def _resolve_code_commit_sha() -> str:  # pragma: no cover — git/subprocess + cli-omitted
    """Return the real current git commit SHA, falling back to the package version.

    Golden Rule #1/#4: the run-identity's ``code_commit_sha`` must be a REAL value,
    never a placeholder. Uses ``git rev-parse HEAD`` when run inside a checkout;
    if git is unavailable (e.g. a wheel install with no repo), falls back to the
    pinned ``brutex.__version__`` — still a real, traceable code identity.
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


def _indicator_version() -> str:  # pragma: no cover — cli-omitted thin string builder
    """Return a real indicator-stack version string (BruteX + shared trading_core)."""
    try:
        import trading_core

        return f"brutex-{__version__}+trading_core-{trading_core.__version__}"
    except (ImportError, AttributeError):  # pragma: no cover — wheel always present in prod
        return f"brutex-{__version__}"


def _data_snapshot_hash(  # pragma: no cover — cli-omitted thin delegate (helper tested in engine)
    *,
    exchange: str,
    segment: str,
    start: str,
    end: str,
    slices: dict[tuple[str, str], list[Any]],
) -> str:
    """Return a real BLAKE3 content fingerprint of the loaded data window.

    Thin delegate to the SHARED non-CLI helper
    :func:`brutex.engine.stage3_wire.data_snapshot_hash` (DEC-STAGE3-WIRE-001,
    CLAUDE.md §11 COMMON): the Stage-3 pass stamps refined rows with the SAME
    derivation over the lockbox-excluded train slices (ADR-0007 Amendment A1),
    so the two surfaces can never drift apart.
    """
    from brutex.engine.stage3_wire import data_snapshot_hash

    return data_snapshot_hash(
        exchange=exchange, segment=segment, start=start, end=end, slices=slices
    )


def _parse_paisa_list(raw: str) -> list[int | None]:
    """Parse a comma list of paisa values; the token ``none`` (any case) → ``None``.

    A non-integer token (other than ``none``) raises :class:`CliUserError`
    naming the token — a bad ``--sl``/``--tp`` CSV must be a clean user error,
    never a traceback (DEC-CLI-USER-ERROR-HYGIENE-001).
    """
    out: list[int | None] = []
    for tok in raw.split(","):
        token = tok.strip()
        if not token:
            continue
        if token.lower() == "none":
            out.append(None)
            continue
        try:
            out.append(int(token))
        except ValueError as exc:
            raise CliUserError(
                f"invalid --sl/--tp token {token!r}: expected a comma-separated "
                "list of integer paisa values or 'none'"
            ) from exc
    return out


def cmd_sweep_all(  # pragma: no cover — orchestration shim over the 100%-tested engine
    *,
    lake_root: Path | None,
    catalog_path: Path | None,
    results_root: Path | None,
    exchange: str,
    segment: str,
    symbols: str,
    intervals: str,
    start: str,
    end: str,
    directions: str,
    k: str,
    conditions: str | None,
    sl: str,
    tp: str,
    top: int,
    mode: str,
    max_tuples: int | None,
    max_combos: int | None,
    data_snapshot_hash: str | None,
    fib_anchors: str = "prev_day",
    overfit_gate: bool = False,
    dsr_min: float = 0.95,
    options_sim: bool = True,
    options_expiry: str = "weekly",
    options_broker: str = "groww",
    stage3_enable: bool = False,
    stage3_top_k: int | None = None,
    stage3_fevals: int | None = None,
    stage3_restarts: int | None = None,
    allow_missing_slices: bool = False,
    sweep_cache_map_mb: int | None = None,
    sweep_cache_bloom_capacity: int | None = None,
    allow_out_of_scope: bool = False,
    acknowledge_sla_overrun: bool = False,
) -> int:
    """Run the full multi-parameter grid sweep and persist every ranked finalist.

    The launch entry point for DEC-SWEEP-ORCHESTRATOR-001: it pre-loads every
    ``(symbol, interval)`` slice from the Parquet lake (the SAME Catalog/ReplayFeed
    path :func:`cmd_sweep` uses), derives a REAL reproducibility context (git SHA,
    indicator-stack version, a content fingerprint of the loaded bars — never
    placeholders, Golden Rule #1), then drives
    :func:`brutex.engine.sweep_orchestrator.run_sweep_grid` with a shared
    Bloom→exact dedup, a hash-chained audit log, and one load-or-create Parquet
    store per tuple (idempotent, lossless re-run — Golden Rule #7).
    """
    from brutex.data.catalog import Catalog
    from brutex.data.timeframes import trading_minutes_for_lake_interval
    from brutex.engine.sweep_audit import SweepAuditWriter
    from brutex.engine.sweep_cache import SweepCache
    from brutex.engine.sweep_checkpoint import SweepCheckpointStore
    from brutex.engine.sweep_launch import (
        plan_launch_dedup,
        resolve_launch_cache_knobs,
        sweep_abort_alert,
    )
    from brutex.engine.sweep_orchestrator import (
        SweepGridConfig,
        SweepTuple,
        Timeframe,
        count_sweep_tuples,
        make_store_for,
    )
    from brutex.engine.sweep_progress import SweepProgress, SweepProgressSnapshot
    from brutex.engine.sweep_run import SweepRunSummary
    from brutex.engine.sweep_supervisor import (
        SweepGuards,
        grid_identity,
        publish_progress,
        run_supervised_grid,
    )
    from brutex.engine.sweep_watchdog import SweepWatchdog
    from brutex.feed.replay import ReplayFeed
    from brutex.feed.resampler import INTERVAL_SECONDS
    from brutex.orchestrator.audit import AuditWriter
    from brutex.ranking import OverfitGateConfig
    from brutex.strategies.types import Side

    resolved_lake = _resolve_lake_root(lake_root)
    catalog = catalog_path if catalog_path is not None else resolved_lake / "catalog.duckdb"
    out_root = results_root if results_root is not None else resolved_lake / "sweeps"

    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
    # Engine-scope coordinate law (DEC-ENGINE-SCOPE-GUARD-001, Layer 2): refuse
    # an out-of-engine-scope launch BEFORE the catalog reconcile + bar load —
    # refusal costs ZERO lake I/O and beats the misleading preflight
    # "ZERO bars" message. The CLI-only --allow-out-of-scope research escape
    # proceeds LOUDLY (WARNING + structlog + audit stamp, Golden Rule #8).
    from brutex.engine.sweep_scope import check_sweep_scope

    scope = check_sweep_scope(
        symbol_list, exchange=exchange, segment=segment, allow_out_of_scope=allow_out_of_scope
    )
    if not scope.ok:
        print(f"[brutex] {scope.refusal_message()}", file=sys.stderr)
        return EXIT_USER_ERROR
    if scope.overridden:
        import structlog

        print(f"[brutex] {scope.override_warning()}")
        structlog.get_logger("brutex.sweep").warning(
            "sweep.scope_override",
            symbols=[verdict.symbol for verdict in scope.refused],
            exchange=exchange,
            segment=segment,
        )
    interval_list = [i.strip() for i in intervals.split(",") if i.strip()]
    for interval in interval_list:
        if interval not in INTERVAL_SECONDS:
            print(
                f"[brutex] unknown --intervals label {interval!r}; choose from "
                f"{sorted(INTERVAL_SECONDS)}",
                file=sys.stderr,
            )
            return EXIT_USER_ERROR
    direction_list = [
        Side.BUY if d.strip().upper() == "BUY" else Side.SELL
        for d in directions.split(",")
        if d.strip()
    ]
    # Fib-anchor axis (DEC-NIFTY-FIB-AXIS-001): parse the CSV of family tokens
    # (prev_day / rolling_weekly / weekly_static) into FibAnchorKind, mirroring the
    # --directions parse. An unknown token is a clean user-config error.
    from brutex.strategies.types import FibAnchorKind

    try:
        fib_anchor_list = [
            FibAnchorKind(tok.strip()) for tok in fib_anchors.split(",") if tok.strip()
        ]
    except ValueError:
        valid = ", ".join(a.value for a in FibAnchorKind)
        print(
            f"[brutex] unknown --fib-anchors value in {fib_anchors!r}; choose from {valid}",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR
    k_list = _parse_int_csv(k, flag="--k")
    condition_bits = parse_condition_ids(conditions) if conditions else default_condition_bits()
    # Off-heap dedup-store knobs (R2-SL-F4-F5): validate flag + env EAGERLY —
    # before the lake preload and any store open — so a mistyped knob is a
    # clean user-config error, never a mid-launch traceback (and never a false
    # abort alert from the sweep_abort_alert wrap below).
    try:
        cache_knobs = resolve_launch_cache_knobs(
            map_size_mb=sweep_cache_map_mb,
            bloom_capacity=sweep_cache_bloom_capacity,
        )
    except ValueError as exc:
        print(f"[brutex] {exc}", file=sys.stderr)
        return EXIT_USER_ERROR

    # Refuse a typo'd/nonexistent lake root BEFORE Catalog.open can mkdir a
    # fresh empty lake (DEC-CLI-USER-ERROR-HYGIENE-001). The catalog FILE is
    # deliberately NOT gated — sweep-all reconciles the catalog from the lake
    # below, so it must keep rebuilding a missing catalog inside a REAL lake.
    _require_lake_dir(resolved_lake, command="sweep-all")

    # Pre-load every slice ONCE (the bars_for memo) so the snapshot hash is a real
    # content fingerprint and each tuple reuses the cached bars (no re-read).
    slices: dict[tuple[str, str], list[Any]] = {}
    with Catalog.open(catalog) as cat:
        # Rebuild the catalog from the on-disk Parquet truth for the swept
        # coordinates BEFORE reading, so a lake whose partitions were never
        # registered (every production ingest sink historically passed
        # catalog=None — DEC-CATALOG-RECONCILE-WIRE-001) is still sweep-able
        # with no re-pull. O(P) one-time over the requested partitions, not in
        # the per-combo hot loop (Golden Rule #10).
        from brutex.data.catalog_reconcile import reconcile_catalog_from_lake

        for symbol in symbol_list:
            for interval in interval_list:
                reconcile_catalog_from_lake(
                    lake_root=resolved_lake,
                    catalog=cat,
                    exchange=exchange,
                    segment=segment,
                    symbol=symbol,
                    interval=interval,
                )
        for symbol in symbol_list:
            for interval in interval_list:
                feed = ReplayFeed(
                    catalog=cat,
                    lake_root=resolved_lake,
                    exchange=exchange,
                    segment=segment,
                    symbols=[symbol],
                    interval=interval,
                    start=_parse_dt(start),
                    end=_parse_dt(end),
                )
                slices[(symbol, interval)] = [event.bar for event in feed]

    loaded = {key: len(bars) for key, bars in slices.items() if bars}
    if not loaded:
        print(
            f"[brutex] no bars for any {symbol_list} × {interval_list} under "
            f"{exchange}/{segment} (lake={resolved_lake})",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR

    # LOUD slice preflight (DEC-SWEEP-PREFLIGHT-001): a requested pair with zero
    # bars must REFUSE the launch (naming every missing pair + the exact remedy)
    # instead of silently skipping its tuples as no-bars — unless the operator
    # deliberately opted into partial coverage with --allow-missing-slices.
    from brutex.engine.sweep_preflight import preflight_slices

    preflight = preflight_slices(
        {key: len(bars) for key, bars in slices.items()},
        symbols=symbol_list,
        intervals=interval_list,
    )
    if not preflight.ok:
        missing_str = ", ".join(f"{s}/{i}" for s, i in preflight.missing)
        if not allow_missing_slices:
            print(
                f"[brutex] REFUSED: {len(preflight.missing)} of {preflight.requested} "
                f"requested (symbol, interval) pairs have ZERO bars: {missing_str}. "
                f"Remedy: {preflight.remediation()} — or re-run with "
                "--allow-missing-slices to sweep partial coverage deliberately.",
                file=sys.stderr,
            )
            return EXIT_USER_ERROR
        print(
            f"[brutex] WARNING: sweeping PARTIAL coverage (--allow-missing-slices): "
            f"{len(preflight.missing)} empty pairs will skip as no-bars: {missing_str}"
        )

    # DERIVED-TF FRESHNESS gate (DEC-SUSPICION-SWEEP-FIXES-001 G1): a stored derived
    # timeframe that drifted from a fresh 1-minute re-derivation (a 1m backfill after
    # the last prebuild) would feed the engine stale bars AND hash them into the run
    # identity as if canonical (Golden Rule #4) — refuse the launch loudly with the
    # exact remedy instead. Verifiable pairs only (derived intraday TFs with BOTH a
    # stored partition and a 1-minute base — the zero-bar preflight above owns the
    # missing-data refusal; abstains never block). NO escape flag by design:
    # Correctness > everything (the merge-readiness priority law) — the remedy is
    # the pipeline's prebuild, never sweeping known-stale data.
    from brutex.data.tf_autoderive import lake_derived_freshness_verdicts
    from brutex.engine.sweep_preflight import preflight_derived_freshness

    freshness = preflight_derived_freshness(
        lake_derived_freshness_verdicts(
            resolved_lake,
            exchange=exchange,
            segment=segment,
            symbols=symbol_list,
            intervals=interval_list,
        )
    )
    if not freshness.ok:
        stale_str = ", ".join(f"{s}/{i}" for s, i in freshness.stale)
        print(
            f"[brutex] REFUSED: {len(freshness.stale)} of {freshness.checked} verifiable "
            f"derived (symbol, interval) pairs are STALE vs a fresh 1-minute "
            f"re-derivation: {stale_str}. Remedy: {freshness.remediation()}",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR

    # Trading minutes, not wall-clock (1day -> 375-min session, NOT 1440 — DEC-TF-VOCAB-21-001).
    timeframes = [
        Timeframe(interval, trading_minutes_for_lake_interval(interval))
        for interval in interval_list
    ]
    snapshot = data_snapshot_hash or _data_snapshot_hash(
        exchange=exchange, segment=segment, start=start, end=end, slices=slices
    )

    grid_kwargs: dict[str, Any] = {
        "instruments": symbol_list,
        "timeframes": timeframes,
        "directions": direction_list,
        "k_values": k_list,
        "condition_bits": condition_bits,
        "stop_losses": _parse_paisa_list(sl),
        "take_profits": _parse_paisa_list(tp),
        "proximities": [None],
        "fib_anchors": fib_anchor_list,
        "mode": mode,
        "data_snapshot_hash": snapshot,
        "indicator_version": _indicator_version(),
        "code_commit_sha": _resolve_code_commit_sha(),
        "top_k": top,
    }
    if max_tuples is not None:
        grid_kwargs["max_tuples"] = max_tuples
    if max_combos is not None:
        grid_kwargs["max_combos"] = max_combos
    if overfit_gate:
        grid_kwargs["overfit"] = OverfitGateConfig(dsr_min=dsr_min)
    if scope.overridden:
        # Layer-1 opt-out rides the SAME operator decision as the Layer-2
        # override (DEC-ENGINE-SCOPE-GUARD-001) — set ONLY when symbols were
        # actually refused, so an in-scope grid never carries the unsafe flag.
        grid_kwargs["unsafe_allow_out_of_scope_instruments"] = True
    # Stage-3 knobs (DEC-STAGE3-WIRE-001): None ⇒ keep the SweepGridConfig
    # defaults (the ONE source of the engine defaults). The on-grid SL/TP
    # fail-fast runs inside SweepGridConfig.__post_init__ (review F5).
    grid_kwargs["stage3_enable"] = stage3_enable
    if stage3_top_k is not None:
        grid_kwargs["stage3_top_k"] = stage3_top_k
    if stage3_fevals is not None:
        grid_kwargs["stage3_max_fevals"] = stage3_fevals
    if stage3_restarts is not None:
        grid_kwargs["stage3_max_restarts"] = stage3_restarts

    try:
        grid = SweepGridConfig(**grid_kwargs)
    except ValueError as exc:
        print(f"[brutex] sweep-all config error: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR

    # Stage-3 fail-fast twin of the on-grid validation (review F5): a missing
    # 'optimize' extra must surface BEFORE the sweep runs, never crash the
    # post-pass after the whole grid persisted.
    if grid.stage3_enable:
        from brutex.engine.stage3_wire import Stage3WireError, ensure_stage3_runtime

        try:
            ensure_stage3_runtime()
        except Stage3WireError as exc:
            print(f"[brutex] sweep-all config error: {exc}", file=sys.stderr)
            return EXIT_USER_ERROR

    # SLA-honesty launch guard (DEC-SLA-LAUNCH-GUARD-001): project the grid's
    # wall-clock as tuple_count × an UNBENCHMARKED per-tuple estimate
    # (BRUTEX_SWEEP_PER_TUPLE_SECONDS overrides) and REFUSE a launch projected
    # past the CLAUDE.md §8 2-4-day envelope unless the operator passed
    # --acknowledge-sla-overrun (then WARN loudly and proceed). Honest per
    # Golden Rule #10: an estimate-based plausibility screen — it never
    # certifies the §8 SLA (which remains UNMEASURED) either way.
    from brutex.engine.sweep_sla_guard import SlaDecision, check_sweep_sla

    try:
        sla = check_sweep_sla(count_sweep_tuples(grid), acknowledge=acknowledge_sla_overrun)
    except ValueError as exc:
        print(f"[brutex] sweep-all config error: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    if sla.decision is SlaDecision.REFUSED:
        print(f"[brutex] {sla.refusal_message()}", file=sys.stderr)
        return EXIT_USER_ERROR
    if sla.decision is SlaDecision.ACKNOWLEDGED:
        print(f"[brutex] WARNING: {sla.acknowledged_warning()}")

    # Dead launch Bloom KILLED (R2-SL-F1): this launcher runs the production
    # DIGEST path — ``run_one_tuple`` always passes a non-None
    # ``trial_stats_digest``, so ``run_persisted_sweep`` sets
    # ``bypass_dedup=True`` and NEVER consults ``dedup.seen``/``mark_many``
    # (the parallel driver's workers use their own raising ``_NoDedup``). The
    # eager ``ComboDedup(OptionDedupIndex(capacity=bloom_capacity_hint(grid),
    # …))`` + LMDB combo-dedup store previously built here were pure dead
    # weight: up to MAX_BLOOM_BYTES (RAM-derived since DEC-RAM-BUDGETS-001;
    # ~16 GiB at the undetected floor) of never-read Bloom bit array below the
    # wall, and a BloomTooLargeError launch REFUSAL above it (~4.1e9 mask
    # identities at that floor) for a filter no code path reads.
    # ``plan_launch_dedup(grid, digest_path=True)`` below returns a no-alloc
    # raising sink instead (a consult would be a real invariant break,
    # surfaced loudly). The mask-grain dedup GUARANTEE is unchanged — it lives
    # in the per-tuple completion sentinel + tuple digest, not this sink; the
    # LMDB-backed TUPLE tier (``SweepCache``) stays, now operator-tunable.
    audit_path = out_root / "audit" / "sweep-all.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    # G&A layer (DEC-SWEEP-WIRE-GA-001): one BLAKE3 supervisor audit chain, a
    # crash-safe resume checkpoint keyed on the grid identity, a Bloom→LMDB
    # tuple-level dedup cache, and a live progress file the dashboard serves at
    # /api/v1/sweep-progress. The dashboard reads this exact path.
    sup_audit_path = out_root / "audit" / "sweep-all-supervisor.jsonl"
    checkpoint_path = out_root / "checkpoint" / "sweep-all.json"
    cache_path = out_root / "dedup-cache"
    progress_path = out_root / "progress" / "sweep-progress.json"

    def _bars_for(symbol: str, interval: str) -> list[Any]:
        return slices.get((symbol, interval), [])

    def _on_tuple(t: SweepTuple, summary: SweepRunSummary) -> None:
        print(
            f"[brutex]   {t.instrument} {t.tf.label} {t.direction.value} k={t.k} "
            f"sl={t.sl_paisa} tp={t.tp_paisa} → persisted={summary.persisted} "
            f"(evaluated={summary.evaluated})"
        )

    def _on_progress(snap: SweepProgressSnapshot) -> None:
        publish_progress(progress_path, snap)

    print(
        f"[brutex] sweep-all: {grid.instruments} × {[tf.label for tf in grid.timeframes]} × "
        f"{[d.value for d in grid.directions]} × k={list(grid.k_values)} → "
        f"results: {out_root}"
    )
    # AUTOMATIC operator alerts (DEC-TELEGRAM-WIRE-001): a hung sweep worker fires
    # a CRITICAL Telegram push, and the grid-complete tally lands when the sweep
    # finishes. Best-effort + a silent no-op when Telegram is disabled (the
    # default) — an alert can never block or slow the sweep (Golden Rule #10).
    from brutex.notifications.sweep_alerts import SweepAlerter

    alerter = SweepAlerter()
    watchdog = SweepWatchdog(on_hung=alerter.watchdog_hung_callback())
    checkpoint = SweepCheckpointStore(
        path=checkpoint_path,
        sweep_id=grid_identity(grid),
        total_combos=count_sweep_tuples(grid),
    )
    # Sweep-abort Telegram alert (R2-SL-F8 / R2-CC-G4) — the sweep twin of
    # cmd_fetch's DEC-TELEGRAM-PULL-HALT-001 pattern (_one_fetch_pass): any
    # abort propagating out of the launch/grid/stage-3/options-sim/cascade
    # span below fires ONE best-effort CRITICAL push via the EXISTING
    # _emit_pull_halt_alert, then re-raises unchanged (never swallowed — the
    # CLI still exits non-zero). KeyboardInterrupt/SystemExit are
    # BaseException, so an operator Ctrl-C never alerts; the clean path fires
    # only alerter.sweep_complete below (never both).
    with sweep_abort_alert(_emit_pull_halt_alert):
        with (
            AuditWriter.open(audit_path, resume=audit_path.exists()) as ingest_audit,
            SweepAuditWriter.open(sup_audit_path, resume=sup_audit_path.exists()) as sup_audit,
            SweepCache.open(
                cache_path,
                bloom_capacity=cache_knobs.bloom_capacity,
                map_size_mb=cache_knobs.map_size_mb,
            ) as cache,
        ):
            # Audit the scope override on the hash-chained launch log (Golden Rule
            # #8): a research launch that let out-of-scope symbols through is a
            # material action and must be traceable in the tree's own audit.
            if scope.overridden:
                ingest_audit.append(
                    action="SWEEP_SCOPE_OVERRIDE",
                    subject="sweep-all:scope",
                    payload={
                        "scope_override": True,
                        "symbols": [verdict.symbol for verdict in scope.refused],
                        "exchange": exchange,
                        "segment": segment,
                    },
                )
            dedup = plan_launch_dedup(grid, digest_path=True)
            guards = SweepGuards(
                audit=sup_audit,
                progress=SweepProgress(),
                checkpoint=checkpoint,
                cache=cache,
                watchdog=watchdog,
            )
            # Multi-core sweep (DEC-MULTICORE-SWEEP-PHASE1-001) — AUTO-default
            # since ADR-0016 (DEC-MULTICORE-SWEEP-AUTODEFAULT-001): an unset
            # BRUTEX_SWEEP_WORKERS resolves to max(1, min(cpu_count - 2, 8)), a
            # SMALL grid (tuple_count < 2 × auto — threaded here, the one call
            # site that knows the grid) stays sequential, and an explicit
            # BRUTEX_SWEEP_WORKERS=1 is the sequential opt-out (byte-for-byte
            # the path below). ``workers > 1`` dispatches the parallel driver on
            # the PRODUCTION digest path (``run_one_tuple`` always supplies a
            # tuple digest → ``bypass_dedup`` is always True); ``workers > 1``
            # on the legacy no-digest path is HARD-REFUSED (this path is always
            # the digest path, so the guard is a belt-and-braces flag check,
            # not a real limit).
            from brutex.engine.sweep_parallel import (
                refuse_parallel_on_legacy_path,
                resolve_sweep_workers,
                run_parallel_grid,
            )

            workers = resolve_sweep_workers(tuple_count=count_sweep_tuples(grid))
            refuse_parallel_on_legacy_path(workers, digest_path=True)
            if workers > 1:
                print(f"[brutex] sweep-all: parallel mode — {workers} workers (digest path)")
                result = run_parallel_grid(
                    grid,
                    bars_for=_bars_for,
                    root=out_root,
                    ingest_audit=ingest_audit,
                    guards=guards,
                    workers=workers,
                )
            else:
                result = run_supervised_grid(
                    grid,
                    bars_for=_bars_for,
                    store_for=make_store_for(out_root, grid),
                    dedup=dedup,
                    ingest_audit=ingest_audit,
                    guards=guards,
                    on_tuple=_on_tuple,
                    on_progress=_on_progress,
                    watchdog_poll_seconds=watchdog.timeout_seconds / 3.0,
                )
            # ── Stage-3 BIPOP-CMA-ES refinement post-pass (DEC-STAGE3-WIRE-001) ──
            # Runs INSIDE the open audit with-block on this single thread (review
            # F10): the per-mask STAGE_3_REFINE_* triples land on the SAME ingest
            # hash chain as the sweep's lines, bracketed by STAGE_3_PASS_START /
            # STAGE_3_PASS_COMPLETE. Bars come from the in-memory slice memo the
            # sweep just ran on (review F9); refinement evaluates the
            # lockbox-excluded train slice only (ADR-0007 condition 7 + A1).
            from brutex.engine.stage3_wire import run_stage3_pass

            stage3_report = run_stage3_pass(
                grid,
                results_root=out_root,
                bars_for=_bars_for,
                audit=ingest_audit,
                exchange=exchange,
                segment=segment,
                start=start,
                end=end,
            )
        # Fire the AUTOMATIC sweep-complete Telegram alert (best-effort, no-op when
        # disabled) now that the supervised grid returned cleanly (DEC-TELEGRAM-WIRE-001).
        alerter.sweep_complete(result)
        print(
            f"[brutex] sweep-all done — tuples={result.tuples_run}/{result.tuples_total} "
            f"(no_bars={result.tuples_skipped_no_bars} cached={result.skipped_cache} "
            f"resumed={result.skipped_resume} failed={result.tuples_failed}) "
            f"evaluated={result.evaluated} ranked={result.ranked} persisted={result.persisted}"
        )
        if result.tuples_failed > 0:
            # "Resilient: skip + continue" — surface the non-zero verdict loudly so a
            # skip is never a silent illusion (Golden Rule #10). The failed tuples are
            # un-sentineled / un-cached and will redo on the next run; the offending
            # combos are named on the TUPLE_FAILED audit lines + the structured log.
            print(
                f"[brutex] WARNING: {result.tuples_failed} tuple(s) FAILED mid-sweep and were "
                f"SKIPPED — see the TUPLE_FAILED audit lines in {sup_audit_path}; they will "
                "redo on the next run (exit code non-zero).",
                file=sys.stderr,
            )
        if stage3_report.enabled:
            print(
                f"[brutex] stage3 — masks_refined={stage3_report.masks_refined} "
                f"evals={stage3_report.evals_total} cells={len(stage3_report.cells)} "
                f"rows_appended={stage3_report.rows_appended} "
                f"skipped_empty_train={stage3_report.skipped_empty_train} "
                f"train_snapshot={stage3_report.train_snapshot_hash}"
            )
        print(
            f"[brutex] results: {out_root}  ·  audit: {audit_path}  ·  supervisor: {sup_audit_path}"
        )
        print(f"[brutex] live progress: {progress_path}  ·  checkpoint: {checkpoint_path}")

        # Automatic options-sim on the winners (DEC-OPTIONS-EXECUTOR-001), zero-touch +
        # non-fatal: skips cleanly when there is no rolling-option lake or a replay fails.
        if options_sim:
            _maybe_run_options_sim_on_winners(
                out_root=out_root,
                lake_root=resolved_lake,
                catalog_path=catalog,
                exchange=exchange,
                segment=segment,
                start=start,
                end=end,
                expiry=options_expiry,
                broker=options_broker,
                top=top,
            )

        # ── AUTOMATIC Stage-4 cascade on the top-K winners (DEC-CASCADE-WIRE-001) ──
        # Re-build the cascade inputs from each tuple's persisted finalists + the
        # in-memory bars (the SAME slices the sweep ran on), run the nine-filter
        # cascade per (instrument, tf) pool, and write the manifest + report under
        # <out_root>/cascade/. `brutex cascade <run-id>` (+ the dashboard button)
        # replay that manifest to the byte-identical report. The --overfit-gate
        # behaviour is UNCHANGED (it ran per-leaderboard inside the sweep above).
        from brutex.engine.cascade_wire import (
            BarsContext,
            build_stage2_cascade_specs,
            cascade_config_for_sweep,
            run_and_write_from_specs,
        )
        from brutex.engine.stage3_wire import (
            cascade_specs_for_refined_cells,
            stage3_manifest_record,
        )

        # Stage-2 grid winners — each carries its OWN swept fib_anchor so Stage-4
        # re-scores a non-prev-day survivor against its own Fib levels
        # (DEC-NIFTY-FIB-AXIS-001). Factored out of this pragma'd shim into a
        # pragma-free, unit-tested helper (review MAJOR 2).
        cascade_specs = build_stage2_cascade_specs(out_root, grid, top_k=top)

        # ALSO enumerate the Stage-3 refined cells (DEC-STAGE3-WIRE-001, review F3)
        # so Stage-4 sees them — every refined-eval row UNTRIMMED (review F8); the
        # manifest records stage-3 enablement + the refined-cell list.
        cascade_specs.extend(cascade_specs_for_refined_cells(stage3_report.cells, grid=grid))

        if cascade_specs:
            # Freeze the per-leaderboard trial statistics into the manifest
            # (DEC-TRIAL-STATS-FREEZE-001, ADR-0008 condition 1): aggregated from
            # the sidecars exactly ONCE here; the MANUAL replay reads only the
            # frozen numbers, never the sidecars.
            import dataclasses as _dataclasses

            from brutex.engine.cascade_wire import build_leaderboard_trials

            stage3_grid = (
                _dataclasses.replace(grid, data_snapshot_hash=stage3_report.train_snapshot_hash)
                if stage3_report.enabled and stage3_report.train_snapshot_hash is not None
                else None
            )
            leaderboard_trials = build_leaderboard_trials(
                out_root, grid, stage3_cells=stage3_report.cells, stage3_grid=stage3_grid
            )
            sweep_id = grid_identity(grid)
            cascade_report = run_and_write_from_specs(
                out_root,
                sweep_id=sweep_id,
                specs=cascade_specs,
                bars_for=_bars_for,
                bars_context=BarsContext(exchange=exchange, segment=segment, start=start, end=end),
                cascade_config=cascade_config_for_sweep(sweep_id),
                stage3=stage3_manifest_record(stage3_report, results_root=out_root),
                leaderboard_trials=leaderboard_trials,
            )
            print(
                f"[brutex] cascade — pools={len(cascade_report.groups)} "
                f"candidates={cascade_report.n_input} survivors={cascade_report.n_survived} "
                f"· report: {out_root / 'cascade' / 'cascade-report.json'}"
            )
        # "Resilient: skip + continue" verdict (Golden Rule #10): a clean grid exits
        # EXIT_OK; any tuple skipped after a SweepComboError makes the exit non-zero.
        return sweep_all_exit_code(result.tuples_failed)


def cmd_sweep_daemon(  # pragma: no cover — live daemon loop (real clock + sleep + lake I/O); the scheduler CORE is 100%-tested in brutex.engine.sweep_scheduler
    *,
    schedule: str,
    once: bool,
    max_cycles: int | None,
    tz_offset_minutes: int | None,
    **sweep_kwargs: Any,
) -> int:
    """Run the full grid sweep unattended on a cadence — the AUTOMATIC twin of ``sweep-all``.

    The §11 "ZERO clicks per day" scheduler (DEC-SWEEP-SCHEDULER-001). It owns ONLY
    the *when* — the clock-injected decision kernel
    :mod:`brutex.engine.sweep_scheduler` (parsed cadence + resume classification,
    100% line+branch tested) — and reuses :func:`cmd_sweep_all` verbatim for the
    *what* (the grid + the live G&A supervisor's crash-safe per-tuple auto-resume +
    audit + options-sim + cascade). It re-implements NO sweep or resume logic
    (CLAUDE.md §11 COMMON).

    Per cycle, with the real wall clock: compute the next fire (or fire now under
    ``--once`` / the on-demand manual path), sleep to it, BLAKE3-audit the cycle
    plan (cadence + resume classification of the shared sweep checkpoint), run one
    ``cmd_sweep_all`` pipeline, BLAKE3-audit the outcome, then loop. ``--once`` (or
    ``--max-cycles``) bounds the loop; otherwise it runs until killed and
    auto-resumes from the same checkpoint after a crash.

    Honest envelope (Golden Rule #10): this bounds WHEN a sweep starts, not its
    ``O(Σ work)`` — a scheduled sweep still costs the §8 wall-time envelope (~33B
    trials in 2-4 days on an M4 Pro, 12-24 h on a 64-128 core box). No
    "millions in minutes" claim is made anywhere.
    """
    import json
    import time

    from brutex.engine.sweep_audit import SweepAuditWriter
    from brutex.engine.sweep_checkpoint import SweepCheckpoint, SweepCheckpointError
    from brutex.engine.sweep_scheduler import (
        IST_OFFSET_SECONDS,
        CheckpointView,
        Schedule,
        SchedulerError,
        cycle_audit_payload,
        plan_cycle,
    )

    tz_offset_seconds = (
        tz_offset_minutes * 60 if tz_offset_minutes is not None else IST_OFFSET_SECONDS
    )
    try:
        parsed = Schedule.parse(schedule, tz_offset_seconds=tz_offset_seconds)
    except SchedulerError as exc:
        print(f"[brutex] sweep-daemon schedule error: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR

    resolved_lake = _resolve_lake_root(sweep_kwargs.get("lake_root"))
    results_root = sweep_kwargs.get("results_root")
    out_root = results_root if results_root is not None else resolved_lake / "sweeps"
    checkpoint_path = out_root / "checkpoint" / "sweep-all.json"
    daemon_audit_path = out_root / "audit" / "sweep-daemon.jsonl"
    daemon_audit_path.parent.mkdir(parents=True, exist_ok=True)

    def _checkpoint_view() -> CheckpointView:
        """Best-effort read of the shared sweep checkpoint for the audit decision.

        The authoritative per-tuple resume is the supervisor's job inside
        ``cmd_sweep_all``; this read only classifies the prior cycle's progress
        for the daemon's BLAKE3 audit line. Any read/parse error degrades to "no
        checkpoint" (FRESH) — it can never crash the daemon.
        """
        try:
            data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            checkpoint = SweepCheckpoint.from_dict(data)
        except (OSError, json.JSONDecodeError, SweepCheckpointError):
            return CheckpointView.from_loaded(None, expected_sweep_id="")
        return CheckpointView.from_loaded(checkpoint, expected_sweep_id=checkpoint.sweep_id)

    mode = "once" if once else f"schedule={parsed.spec}"
    print(f"[brutex] sweep-daemon starting ({mode}) — audit: {daemon_audit_path}")

    last_fire: float | None = None
    cycle = 0
    with SweepAuditWriter.open(daemon_audit_path, resume=daemon_audit_path.exists()) as audit:
        while True:
            plan = plan_cycle(
                now=time.time(),
                schedule=parsed,
                checkpoint_view=_checkpoint_view(),
                last_fire=last_fire,
                run_immediately=once,
            )
            audit.append(
                action="DAEMON_CYCLE_PLAN",
                subject=f"sweep-daemon:cycle-{cycle}",
                payload=cycle_audit_payload(
                    cycle_index=cycle,
                    fire_at=plan.fire_at,
                    schedule=parsed,
                    resume=plan.resume,
                    once=once,
                ),
            )
            if not once:
                sleep_seconds = max(0.0, plan.fire_at - time.time())
                print(
                    f"[brutex] sweep-daemon cycle {cycle}: next fire in {sleep_seconds:.0f}s "
                    f"({plan.resume.kind.value}) — {plan.resume.reason}"
                )
                time.sleep(sleep_seconds)
            else:
                print(
                    f"[brutex] sweep-daemon --once: running one cycle now ({plan.resume.kind.value})"
                )

            exit_code = cmd_sweep_all(**sweep_kwargs)
            audit.append(
                action="DAEMON_CYCLE_DONE",
                subject=f"sweep-daemon:cycle-{cycle}",
                payload={"cycle_index": cycle, "exit_code": exit_code, "fire_at": plan.fire_at},
            )
            last_fire = plan.fire_at
            cycle += 1
            if once or (max_cycles is not None and cycle >= max_cycles):
                break

    print(f"[brutex] sweep-daemon stopped after {cycle} cycle(s)")
    return EXIT_OK


def _build_supervisor_backoff(
    *,
    backoff_base_seconds: float | None,
    backoff_factor: float | None,
    backoff_cap_seconds: float | None,
    reset_after_uptime_seconds: float | None,
    max_restarts: int | None,
) -> Any:
    """Build a :class:`BackoffPolicy` from the CLI flags, applying module defaults.

    Pure + fully tested: each ``None`` flag falls back to the supervisor module's
    default constant, and the resulting policy validates its own invariants
    (raising :class:`ValueError` on e.g. ``cap < base``), so the CLI surfaces a
    clean user error instead of a stack trace.
    """
    from brutex.orchestrator.supervisor import (
        DEFAULT_BACKOFF_BASE_SECONDS,
        DEFAULT_BACKOFF_CAP_SECONDS,
        DEFAULT_BACKOFF_FACTOR,
        DEFAULT_MAX_RESTARTS,
        DEFAULT_RESET_AFTER_UPTIME_SECONDS,
        BackoffPolicy,
    )

    return BackoffPolicy(
        base_seconds=(
            backoff_base_seconds
            if backoff_base_seconds is not None
            else DEFAULT_BACKOFF_BASE_SECONDS
        ),
        factor=backoff_factor if backoff_factor is not None else DEFAULT_BACKOFF_FACTOR,
        cap_seconds=(
            backoff_cap_seconds if backoff_cap_seconds is not None else DEFAULT_BACKOFF_CAP_SECONDS
        ),
        reset_after_uptime_seconds=(
            reset_after_uptime_seconds
            if reset_after_uptime_seconds is not None
            else DEFAULT_RESET_AFTER_UPTIME_SECONDS
        ),
        max_restarts=max_restarts if max_restarts is not None else DEFAULT_MAX_RESTARTS,
    )


def _supervisor_child_argv(child_args: list[str]) -> list[str]:
    """Return the argv that launches ``brutex sweep-daemon`` with *child_args*.

    Runs the daemon via the SAME Python interpreter (``sys.executable -m
    brutex``) so the child inherits this exact venv + install — robust whether or
    not the ``brutex`` console script is on ``PATH``. ``argparse.REMAINDER``
    leaves a leading ``--`` separator in *child_args*; it is stripped so
    ``brutex supervisor -- --schedule X`` forwards ``--schedule X`` cleanly.
    """
    forwarded = list(child_args)
    if forwarded and forwarded[0] == "--":
        forwarded = forwarded[1:]
    return [sys.executable, "-m", "brutex", "sweep-daemon", *forwarded]


def cmd_supervisor(  # pragma: no cover — live loop (real subprocess + signals + lake I/O); the supervisor CORE is 100%-tested in brutex.orchestrator.supervisor
    *,
    child_args: list[str],
    backoff_base_seconds: float | None,
    backoff_factor: float | None,
    backoff_cap_seconds: float | None,
    reset_after_uptime_seconds: float | None,
    max_restarts: int | None,
    shutdown_grace_seconds: float | None,
    lake_root: Path | None,
) -> int:
    """Supervise ``brutex sweep-daemon`` — auto-relaunch it on a hard crash.

    The missing *actor* the watchdogs left out (DEC-SUPERVISOR-AUTORESTART-001):
    the two sweep watchdogs DETECT a crash but nothing RELAUNCHES the process.
    This spawns the sweep daemon as a CHILD, monitors it, and auto-relaunches it
    on an UNEXPECTED exit (non-zero code / death-by-signal) with exponential
    backoff + a max-restart cap + a reset-after-stable-uptime window. A CLEAN
    exit (the sweep genuinely completed) stops it; an operator SIGTERM/SIGINT
    shuts the child down gracefully and does NOT relaunch. Each restart appends a
    hash-chained audit row. Because the sweep's checkpoint auto-resume already
    makes a relaunch bit-identical, each relaunch resumes from the checkpoint.

    It re-implements NO supervise or backoff logic — it composes the
    100%-line+branch-tested :class:`brutex.orchestrator.supervisor.ProcessSupervisor`
    with a real ``subprocess.Popen`` launcher + real SIGTERM/SIGINT handlers
    (CLAUDE.md §11 COMMON). This shim is the live-side-effect boundary.

    Honest envelope (Golden Rule #10): this is the CROSS-PLATFORM layer — it
    survives a CHILD crash while the supervisor itself is alive. Surviving a
    machine REBOOT / logout is the OS service manager's job (the shipped launchd
    + systemd templates under ``docs/runbooks/``).
    """
    from brutex.engine.sweep_audit import SweepAuditWriter
    from brutex.orchestrator.supervisor import (
        AUDIT_ACTION_GIVE_UP,
        DEFAULT_SHUTDOWN_GRACE_SECONDS,
        ProcessSupervisor,
        SupervisorOutcome,
        build_subprocess_launcher,
        install_signal_stop,
    )

    try:
        policy = _build_supervisor_backoff(
            backoff_base_seconds=backoff_base_seconds,
            backoff_factor=backoff_factor,
            backoff_cap_seconds=backoff_cap_seconds,
            reset_after_uptime_seconds=reset_after_uptime_seconds,
            max_restarts=max_restarts,
        )
    except ValueError as exc:
        print(f"[brutex] supervisor policy error: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR

    resolved_lake = _resolve_lake_root(lake_root)
    audit_path = resolved_lake / "sweeps" / "audit" / "supervisor.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)

    argv = _supervisor_child_argv(child_args)
    stop_requested = install_signal_stop()
    print(
        f"[brutex] supervisor starting — child: {' '.join(argv)} — audit: {audit_path}",
    )

    # Crash-loop operator alert (DEC-SUPERVISOR-CRASHLOOP-ALERT-001): every
    # abnormal-exit relaunch fires one best-effort, deduped Telegram push
    # (CRITICAL once 3 relaunches land within the rolling window), so a child
    # OOM-SIGKILL-looping all night can never stay silent. Best-effort + a
    # silent no-op when Telegram is disabled (the default) — the supervisor's
    # relaunch behaviour is byte-unchanged.
    from brutex.notifications.sweep_alerts import SweepAlerter

    with SweepAuditWriter.open(audit_path, resume=audit_path.exists()) as audit:
        supervisor = ProcessSupervisor(
            launch=build_subprocess_launcher(argv),
            policy=policy,
            subject="supervisor:sweep-daemon",
            audit=audit,
            stop_requested=stop_requested,
            shutdown_grace_seconds=(
                shutdown_grace_seconds
                if shutdown_grace_seconds is not None
                else DEFAULT_SHUTDOWN_GRACE_SECONDS
            ),
            alerter=SweepAlerter(),
        )
        result = supervisor.run()

    print(
        f"[brutex] supervisor stopped ({result.outcome.value}) after "
        f"{result.restarts} restart(s); last child exit={result.last_exit_code}"
    )
    if result.outcome is SupervisorOutcome.GAVE_UP:
        _ = AUDIT_ACTION_GIVE_UP  # documented in the audit trail; loud non-zero exit
        return EXIT_INTERNAL_ERROR
    return EXIT_OK


def cmd_results_show(*, results_root: Path, top: int, sort_by: str) -> int:
    """Read back a sweep results tree and print the ranked winners-by-name leaderboard.

    Thin shell over :func:`brutex.engine.results_report.results_show_report` (the
    100%-tested engine module): discovers every per-tuple Parquet under
    *results_root*, merges them, and prints the leaderboard showing each finalist's
    winning conditions BY NAME plus net P&L, trades, win-rate, Sharpe and
    Deflated-Sharpe (DEC-SWEEP-WINNERS-BY-NAME-001; no per-row gate verdict is
    rendered since DEC-STAGE3-WIRE-001 — refined trees hold ungated rows). A missing
    root, empty ``top`` or unknown sort key surfaces as a clear user error, never a
    traceback.
    """
    from brutex.engine.results_report import results_show_report
    from brutex.engine.results_store import ResultsStoreError

    try:
        report = results_show_report(results_root, top=top, sort_by=sort_by)
    except ResultsStoreError as exc:
        print(f"[brutex] results error: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    print(report)
    return EXIT_OK


def cmd_cascade(  # pragma: no cover — orchestration shim over the 100%-tested engine
    *,
    lake_root: Path | None,
    results_root: Path | None,
    catalog_path: Path | None,
    run_id: str | None,
) -> int:
    """Re-run the Stage-4 nine-filter cascade on a finished sweep's winners (MANUAL path).

    The hand-trigger counterpart of the AUTOMATIC cascade ``brutex sweep-all``
    runs after the grid (DEC-CASCADE-WIRE-001): it reloads the cascade manifest
    the sweep wrote under ``--results-root`` (default ``<lake>/sweeps``), re-reads
    the SAME bars from the lake, and re-runs the cascade through the identical
    pure core — so the regenerated ``cascade-report.json`` is byte-identical to the
    one the sweep produced. A missing/invalid manifest, or a ``run_id`` that does
    not match the manifest's, surfaces as a clear user error, never a traceback.
    """
    from brutex.engine.cascade_wire import (
        CascadeWireError,
        load_bars_for_manifest,
        load_manifest,
        report_path,
        run_and_write_from_manifest,
    )

    resolved_lake = _resolve_lake_root(lake_root)
    root = results_root if results_root is not None else resolved_lake / "sweeps"
    catalog = catalog_path if catalog_path is not None else resolved_lake / "catalog.duckdb"
    try:
        manifest = load_manifest(root)
    except CascadeWireError as exc:
        print(f"[brutex] cascade error: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    if run_id is not None and run_id != manifest.sweep_id:
        print(
            f"[brutex] cascade error: run-id {run_id!r} does not match the manifest's "
            f"sweep-id {manifest.sweep_id!r} under {root}",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR

    bars_for = load_bars_for_manifest(manifest, lake_root=resolved_lake, catalog_path=catalog)
    report = run_and_write_from_manifest(root, bars_for=bars_for)
    print(
        f"[brutex] cascade done — sweep={manifest.sweep_id[:16]} pools={len(report.groups)} "
        f"candidates={report.n_input} survivors={report.n_survived}"
    )
    print(f"[brutex] report: {report_path(root)}")
    return EXIT_OK


def cmd_verify_lookahead(  # pragma: no cover — real-lake I/O shim over the 100%-tested barrier
    *,
    lake_root: Path | None,
    catalog_path: Path | None,
    exchange: str,
    segment: str,
    symbol: str,
    interval: str,
    start: str,
    end: str,
    run_id: str | None,
) -> int:
    """MANUAL feed-ordering + cursor-invariant recheck over a lake slice.

    The hand-trigger counterpart of the barrier that
    :class:`~brutex.strategies.runner.StrategyRunner` /
    :class:`~brutex.execution.driver.BacktestDriver` build on every sweep eval
    (DEC-LOOKAHEAD-BARRIER-001). It loads one ``(symbol, interval)`` window
    from the lake via the SAME ``Catalog`` / ``ReplayFeed`` path the engine
    uses, then walks those bars **forward** through a recording
    :class:`~brutex.feed.lookahead.LookAheadBarrier` — advancing the cursor to
    bar ``i`` and accessing index ``i`` — and reports the
    :class:`~brutex.feed.lookahead.LookAheadReport` verdict via
    :func:`~brutex.feed.lookahead.verify_with_barrier`.

    **Honest scope (Golden Rule #10):** this re-confirms that the lake slice
    loads and replays in **forward, non-decreasing index order** and that the
    barrier's advance/access **cursor invariant** holds over that walk — it does
    NOT run a strategy, so it cannot exercise strategy-level look-ahead (that is
    ``ReplayFeed.ensure_no_lookahead``'s job, integration-tested in the runner);
    because each bar is accessed at exactly its own index, a well-formed slice
    always PASSES. It is a lightweight ordering/cursor smoke check, not a
    strategy-consumption oracle. An empty slice is a user error.
    """
    from brutex.data.catalog import Catalog
    from brutex.feed.lookahead import LookAheadBarrier, verify_with_barrier
    from brutex.feed.replay import ReplayFeed

    resolved_lake = _resolve_lake_root(lake_root)
    catalog = catalog_path if catalog_path is not None else resolved_lake / "catalog.duckdb"
    _require_catalog_exists(catalog)
    with Catalog.open(catalog, require_exists=True) as cat:
        feed = ReplayFeed(
            catalog=cat,
            lake_root=resolved_lake,
            exchange=exchange,
            segment=segment,
            symbols=[symbol],
            interval=interval,
            start=_parse_dt(start),
            end=_parse_dt(end),
        )
        bars = [event.bar for event in feed]
    if not bars:
        print(
            f"[brutex] verify-lookahead: no bars for {symbol} {interval} {start}→{end} "
            f"under {exchange}/{segment} (lake={resolved_lake})",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR

    barrier = LookAheadBarrier(record=True)

    def _replay_slice() -> None:
        # Forward walk over the run's REAL bars in ascending index order. Each
        # bar is accessed at its OWN index, so this re-confirms the feed loads +
        # replays forward and the advance/access cursor invariant holds — it is
        # NOT a strategy replay, so a well-formed slice always passes (it cannot
        # by construction access a future index). Honest: an ordering/cursor
        # smoke check, not a strategy-look-ahead oracle.
        for index in range(len(bars)):
            barrier.advance_to(index)
            barrier.access(index)

    report = verify_with_barrier(_replay_slice, barrier)
    label = run_id if run_id is not None else f"{symbol}/{interval}"
    verdict = "PASS" if report.passed else "FAIL"
    print(
        f"[brutex] verify-lookahead {verdict} — run={label} bars={len(bars)} "
        f"checked={report.indices_checked} max_index={report.max_index_touched} "
        f"final_cursor={report.final_cursor}"
    )
    if not report.passed:
        print(f"[brutex] look-ahead violation: {report.violation}", file=sys.stderr)
        return EXIT_USER_ERROR
    return EXIT_OK


def cmd_sweep_verify(  # pragma: no cover — orchestration shim over the 100%-tested sweep_verify core
    *,
    lake_root: Path | None,
    catalog_path: Path | None,
    results_root: Path | None,
    exchange: str,
    segment: str,
    symbols: str,
    intervals: str,
    start: str,
    end: str,
    directions: str,
    k: str,
    conditions: str | None,
    sl: str,
    tp: str,
    top: int,
    mode: str,
    max_tuples: int | None,
    max_combos: int | None,
    data_snapshot_hash: str | None,
    indicator_version: str | None,
    code_commit_sha: str | None,
    recount: bool,
    scratch_root: Path | None,
    allow_context_mismatch: bool,
    fib_anchors: str = "prev_day",
    emit_heal_plan: bool = False,
    stage3_enable: bool = False,
    stage3_top_k: int | None = None,
    stage3_fevals: int | None = None,
    stage3_restarts: int | None = None,
    allow_out_of_scope: bool = False,
) -> int:
    """Verify a sweep results tree — the doctor's CLI (DEC-SWEEP-VERIFY-001).

    Tier 1 (structural) always runs; Tier 2 (identity) runs when the recorded
    context is available (the audit log, or explicit overrides); Tier 3
    (``--recount``) shadow re-sweeps the grid into a scratch root with the
    SAME 100%-tested ``run_sweep_grid`` and byte-diffs — verdict-grade ONLY
    under a fully matched recorded context (the hostile review's validity
    gate). REPORT-ONLY toward the verified tree: this command never modifies
    it — but Tier 3 EXECUTES the real engine into ``--scratch-root``, so the
    recount tier runs the SAME ``check_sweep_scope`` launch gate as
    sweep/sweep-all (DEC-ENGINE-SCOPE-GUARD-001 + its corporate-action
    extension DEC-CORP-ACTION-GATE-001 / ADR-0016), with the SAME CLI-only
    ``--allow-out-of-scope`` research escape so a legacy out-of-scope tree
    stays recount-verifiable LOUDLY, never silently; Tier 1/2 run no engine
    and stay unguarded.
    """
    from brutex.engine.sweep_verify import (
        SweepVerifyError,
        compare_recount,
        plan_expected_stores,
        recorded_context_from_audit,
        verify_identity,
        verify_structural,
    )

    resolved_lake = _resolve_lake_root(lake_root)
    out_root = results_root if results_root is not None else resolved_lake / "sweeps"

    try:
        structural = verify_structural(out_root)
    except SweepVerifyError as exc:
        print(f"[brutex] sweep-verify error: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    print(structural.render())
    reports = [structural]

    # The recorded context (review condition 2a): the tree describes itself
    # via its audit chain; explicit flags override; NEVER the operator's memory.
    try:
        recorded = recorded_context_from_audit(out_root / "audit" / "sweep-all.jsonl")
    except SweepVerifyError as exc:
        print(f"[brutex] sweep-verify error: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    snapshot = data_snapshot_hash or (recorded.data_snapshot_hash if recorded else None)
    indicator = indicator_version or (recorded.indicator_version if recorded else None)
    code_sha = code_commit_sha or (recorded.code_commit_sha if recorded else None)
    if snapshot is None or indicator is None or code_sha is None:
        print(
            "[brutex] sweep-verify: no recorded context (no audit log under the tree) "
            "and no --data-snapshot-hash/--indicator-version/--code-commit-sha "
            "overrides — identity/recount tiers skipped (structural-only verdict)"
        )
        return EXIT_OK if structural.clean else EXIT_VERIFY_FINDINGS

    from brutex.engine.sweep_orchestrator import SweepGridConfig, Timeframe
    from brutex.feed.resampler import INTERVAL_SECONDS
    from brutex.strategies.types import FibAnchorKind, Side

    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
    if recount:
        # Engine scope gate (DEC-ENGINE-SCOPE-GUARD-001 + its corporate-action
        # extension DEC-CORP-ACTION-GATE-001 / ADR-0016): the Tier-3 --recount
        # shadow re-sweep EXECUTES the real engine (run_sweep_grid over lake
        # bars, persisting per-tuple stores into --scratch-root) — a
        # production sweep entry, NOT a report-only read — so it runs the SAME
        # check_sweep_scope gate as sweep/sweep-all, with the SAME CLI-only
        # --allow-out-of-scope research escape (loud WARNING + structlog
        # hazard) so a legacy out-of-scope tree stays recount-verifiable
        # LOUDLY, never silently. --allow-context-mismatch overrides the
        # recount CONTEXT gate only, never scope. Tier 1/2 (structural +
        # identity) stay unguarded: they read an existing tree and launch no
        # engine, and a legacy out-of-scope tree stays report-verifiable.
        from brutex.engine.sweep_scope import check_sweep_scope

        scope = check_sweep_scope(
            symbol_list, exchange=exchange, segment=segment, allow_out_of_scope=allow_out_of_scope
        )
        if not scope.ok:
            print(f"[brutex] sweep-verify --recount: {scope.refusal_message()}", file=sys.stderr)
            return EXIT_USER_ERROR
        if scope.overridden:
            import structlog

            print(f"[brutex] {scope.override_warning()}")
            structlog.get_logger("brutex.sweep").warning(
                "sweep.scope_override",
                symbols=[verdict.symbol for verdict in scope.refused],
                exchange=exchange,
                segment=segment,
            )
    interval_list = [i.strip() for i in intervals.split(",") if i.strip()]
    for interval in interval_list:
        if interval not in INTERVAL_SECONDS:
            print(
                f"[brutex] unknown --intervals label {interval!r}; choose from "
                f"{sorted(INTERVAL_SECONDS)}",
                file=sys.stderr,
            )
            return EXIT_USER_ERROR
    direction_list = [
        Side.BUY if d.strip().upper() == "BUY" else Side.SELL
        for d in directions.split(",")
        if d.strip()
    ]
    # The verify grid MUST reproduce the SAME Fib-anchor axis the sweep ran under
    # (DEC-NIFTY-FIB-AXIS-001) — else the reconstructed grid identity + row run_ids
    # would not re-derive.
    try:
        fib_anchor_list = [
            FibAnchorKind(tok.strip()) for tok in fib_anchors.split(",") if tok.strip()
        ]
    except ValueError:
        valid = ", ".join(a.value for a in FibAnchorKind)
        print(
            f"[brutex] unknown --fib-anchors value in {fib_anchors!r}; choose from {valid}",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR
    k_list = _parse_int_csv(k, flag="--k")
    condition_bits = parse_condition_ids(conditions) if conditions else default_condition_bits()
    # tf_minutes via the SHARED trading-minutes parser (DEC-STAGE3-RECOUNT-001
    # condition B9): the `// 60` form maps 1day to 1440 wall-clock minutes while
    # the launcher uses the 375-minute session — tf_minutes is in every tuple
    # digest, so the drift read EVERY daily-TF tree as a false grid_mismatch.
    from brutex.data.timeframes import trading_minutes_for_lake_interval

    grid_kwargs: dict[str, Any] = {
        "instruments": symbol_list,
        "timeframes": [
            Timeframe(interval, trading_minutes_for_lake_interval(interval))
            for interval in interval_list
        ],
        "directions": direction_list,
        "k_values": k_list,
        "condition_bits": condition_bits,
        "stop_losses": _parse_paisa_list(sl),
        "take_profits": _parse_paisa_list(tp),
        "proximities": [None],
        "fib_anchors": fib_anchor_list,
        "mode": mode,
        "data_snapshot_hash": snapshot,
        "indicator_version": indicator,
        "code_commit_sha": code_sha,
        "top_k": top,
        # DEC-ENGINE-SCOPE-GUARD-001: verifying recorded work, not launching —
        # a legacy out-of-scope tree must stay Tier-1/2 verifiable, so the
        # Layer-1 config invariant is opted out at this ONE forensic rebuild
        # site. The Tier-3 --recount LAUNCH is separately Layer-2 gated above
        # (DEC-CORP-ACTION-GATE-001 extension) with the --allow-out-of-scope
        # escape, so the opt-out here never lets an engine execution through
        # silently.
        "unsafe_allow_out_of_scope_instruments": True,
    }
    if max_tuples is not None:
        grid_kwargs["max_tuples"] = max_tuples
    if max_combos is not None:
        grid_kwargs["max_combos"] = max_combos
    try:
        grid = SweepGridConfig(**grid_kwargs)
    except ValueError as exc:
        print(f"[brutex] sweep-verify config error: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR

    # Stage-3 cell attribution via the cascade manifest (condition 2c) —
    # absent/unreadable manifest leaves cells honestly unattributed.
    import contextlib

    from brutex.engine.cascade_wire import CascadeWireError, load_manifest

    stage3 = None
    with contextlib.suppress(CascadeWireError):
        stage3 = load_manifest(out_root).stage3

    current_sha = _resolve_code_commit_sha()
    current_indicator = _indicator_version()
    vocabulary_trusted = code_sha == current_sha and indicator == current_indicator
    try:
        identity = verify_identity(
            out_root, grid, stage3=stage3, vocabulary_trusted=vocabulary_trusted
        )
    except SweepVerifyError as exc:
        print(f"[brutex] sweep-verify error: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    print(identity.render())
    reports.append(identity)

    inconclusive_only = False
    if recount:
        import tempfile

        from brutex.data.catalog import Catalog
        from brutex.data.catalog_reconcile import reconcile_catalog_from_lake
        from brutex.engine.sweep_launch import plan_launch_dedup
        from brutex.engine.sweep_orchestrator import (
            make_store_for,
            run_sweep_grid,
        )
        from brutex.feed.replay import ReplayFeed
        from brutex.orchestrator.audit import AuditWriter

        catalog = catalog_path if catalog_path is not None else resolved_lake / "catalog.duckdb"
        # Refuse a typo'd/nonexistent lake root BEFORE Catalog.open can mkdir a
        # fresh empty lake (DEC-CLI-USER-ERROR-HYGIENE-001). Like sweep-all the
        # catalog FILE is NOT gated — the recount reconciles it from the lake.
        _require_lake_dir(resolved_lake, command="sweep-verify --recount")
        slices: dict[tuple[str, str], list[Any]] = {}
        with Catalog.open(catalog) as cat:
            for symbol in symbol_list:
                for interval in interval_list:
                    reconcile_catalog_from_lake(
                        lake_root=resolved_lake,
                        catalog=cat,
                        exchange=exchange,
                        segment=segment,
                        symbol=symbol,
                        interval=interval,
                    )
                    feed = ReplayFeed(
                        catalog=cat,
                        lake_root=resolved_lake,
                        exchange=exchange,
                        segment=segment,
                        symbols=[symbol],
                        interval=interval,
                        start=_parse_dt(start),
                        end=_parse_dt(end),
                    )
                    slices[(symbol, interval)] = [event.bar for event in feed]
        if not any(slices.values()):
            print(
                f"[brutex] sweep-verify --recount: no bars for any {symbol_list} × "
                f"{interval_list} under {exchange}/{segment} (lake={resolved_lake})",
                file=sys.stderr,
            )
            return EXIT_USER_ERROR
        fresh_snapshot = _data_snapshot_hash(
            exchange=exchange, segment=segment, start=start, end=end, slices=slices
        )
        # The validity gate (review condition 3b): ALL THREE recorded strings
        # must match the current environment for divergence to be verdict-grade.
        mismatches = [
            f"{name}: recorded {rec!r} vs current {cur!r}"
            for name, rec, cur in (
                ("code_commit_sha", code_sha, current_sha),
                ("indicator_version", indicator, current_indicator),
                ("data_snapshot_hash", snapshot, fresh_snapshot),
            )
            if rec != cur
        ]
        context_match = not mismatches
        if mismatches:
            print(
                "[brutex] sweep-verify --recount: recorded context does NOT match the "
                "current environment:\n  " + "\n  ".join(mismatches)
            )
            # Legacy-era diagnostic (DEC-SNAPSHOT-HASH-FULL-001): a snapshot
            # mismatch whose RECORDED hash matches the retired v1 SAMPLED
            # derivation over the SAME fresh slices is a pre-upgrade tree, not
            # necessarily changed data — name it so the operator knows the
            # remedy. O(#slices) extra cost, mismatch branch only; the refusal
            # ladder below is UNCHANGED (a v1 match was never verdict-grade).
            if snapshot != fresh_snapshot:
                from brutex.engine.stage3_wire import _data_snapshot_hash_v1

                legacy_v1 = _data_snapshot_hash_v1(
                    exchange=exchange, segment=segment, start=start, end=end, slices=slices
                )
                if snapshot == legacy_v1:
                    print(
                        "[brutex] note: the recorded hash matches the LEGACY sampled "
                        "derivation (pre-DEC-SNAPSHOT-HASH-FULL-001) over the current "
                        "lake — findings INCONCLUSIVE by design; re-sweep to mint a "
                        "v2 identity."
                    )
        if mismatches and not allow_context_mismatch:
            print(
                "[brutex] recount REFUSED (a recount under changed inputs is not an "
                "oracle — ADR-0008 drift channels); re-run with "
                "--allow-context-mismatch for compare-with-caveat (INCONCLUSIVE, "
                "never CORRUPT)"
            )
            inconclusive_only = True
        else:
            scratch = (
                scratch_root
                if scratch_root is not None
                else Path(tempfile.mkdtemp(prefix="brutex-sweep-verify-"))
            )
            # Stage-3 cell recount (DEC-STAGE3-RECOUNT-001): compose the SAME
            # run_stage3_pass into the scratch using the knobs the tree
            # RECORDED (STAGE_3_PASS_START audit payloads — condition B1);
            # operator --stage3-* flags are the audit-less fallback only
            # (condition B8), and a disagreement refuses cells, never silently
            # prefers either side.
            stage3_grid = None
            cells_note: str | None = None
            cell_evidence = False
            knobs_from_audit = False
            recorded_train_hash: str | None = None
            stage3_kwargs: dict[str, Any] | None = None
            if stage3 is not None and stage3.enabled:
                from brutex.engine.sweep_verify import recorded_stage3_from_audit

                recorded_train_hash = stage3.train_snapshot_hash
                stage3_audit = recorded_stage3_from_audit(out_root / "audit" / "sweep-all.jsonl")
                knobs = stage3_audit.knobs
                if not stage3_audit.consistent:
                    cells_note = (
                        "mixed Stage-3 knob history in the audit (condition B4) — "
                        "the cell stores hold accreted foreign-knob rows and are "
                        "honestly non-reproducible"
                    )
                elif knobs is not None:
                    flag_mismatch = [
                        f"{name}: recorded {rec} vs --flag {flag}"
                        for name, rec, flag in (
                            ("stage3_top_k", knobs.stage3_top_k, stage3_top_k),
                            ("stage3_fevals", knobs.max_fevals, stage3_fevals),
                            ("stage3_restarts", knobs.max_restarts, stage3_restarts),
                        )
                        if flag is not None and flag != rec
                    ]
                    if flag_mismatch:
                        cells_note = (
                            "the --stage3-* flags disagree with the tree's recorded "
                            "knobs: " + "; ".join(flag_mismatch)
                        )
                    else:
                        knobs_from_audit = True
                        stage3_kwargs = {
                            "stage3_enable": True,
                            "stage3_top_k": knobs.stage3_top_k,
                            "stage3_max_fevals": knobs.max_fevals,
                            "stage3_max_restarts": knobs.max_restarts,
                        }
                elif stage3_enable:
                    # Audit-less fallback: recount with the operator's knobs —
                    # cells stay INCONCLUSIVE (evidence requires RECORDED knobs).
                    stage3_kwargs = {"stage3_enable": True}
                    for key, value in (
                        ("stage3_top_k", stage3_top_k),
                        ("stage3_max_fevals", stage3_fevals),
                        ("stage3_max_restarts", stage3_restarts),
                    ):
                        if value is not None:
                            stage3_kwargs[key] = value
                else:
                    cells_note = (
                        "no recorded Stage-3 knobs in the audit and no --stage3-* flags supplied"
                    )
                if cells_note is None and stage3_kwargs is not None:
                    import dataclasses as _dataclasses

                    from brutex.engine.stage3_wire import (
                        Stage3WireError,
                        ensure_stage3_runtime,
                    )

                    try:
                        current_versions = ensure_stage3_runtime()
                    except Stage3WireError as exc:
                        cells_note = f"stage3 runtime unavailable: {exc}"
                        stage3_grid = None
                    else:
                        stage3_grid = _dataclasses.replace(grid, **stage3_kwargs)
                        versions_ok = stage3_audit.optimizer_versions in (
                            (),
                            (current_versions,),
                        )
                        if not versions_ok:
                            cells_note = (
                                f"optimizer versions drifted: recorded "
                                f"{list(stage3_audit.optimizer_versions)} vs current "
                                f"{current_versions} (condition B3) — cell divergence "
                                f"will be INCONCLUSIVE"
                            )
                if cells_note is not None:
                    print(f"[brutex] stage3 cells degraded to INCONCLUSIVE: {cells_note}")
            print(f"[brutex] recount: shadow re-sweep into {scratch} (costs a full sweep)")
            audit_path = scratch / "audit.jsonl"
            audit_path.parent.mkdir(parents=True, exist_ok=True)
            train_hash_ok = True
            # Dead launch Bloom KILLED here too (R2-SL-F1, the cmd_sweep_all
            # twin): the shadow re-sweep runs the SAME digest path
            # (run_one_tuple always passes trial_stats_digest → bypass_dedup),
            # so the grid-sized ComboDedup(OptionDedupIndex(...)) + LMDB
            # combo-dedup store previously built here were never consulted —
            # and refused a recount past the MAX_BLOOM_BYTES identity ceiling
            # (~4.1e9 at the 16 GiB floor) with BloomTooLargeError.
            # plan_launch_dedup returns the no-alloc raising sink instead.
            with AuditWriter.open(audit_path) as audit:
                resweep_summary = run_sweep_grid(
                    grid,
                    bars_for=lambda s, i: slices.get((s, i), []),
                    store_for=make_store_for(scratch, grid),
                    dedup=plan_launch_dedup(grid, digest_path=True),
                    audit=audit,
                )
                # "Resilient: skip + continue" (DEC-ENGINE-TUPLE-SKIP-RESILIENT-001):
                # run_sweep_grid now SKIPS a raising tuple and completes a PARTIAL
                # re-sweep. A byte-diff against an incomplete re-sweep is meaningless,
                # so the verdict is downgraded to INCONCLUSIVE (never a pass/fail over
                # a partial sweep) naming the failed-tuple count.
                if resweep_summary.tuples_failed > 0:
                    inconclusive_only = True
                    print(
                        "[brutex] sweep-verify INCONCLUSIVE: the shadow re-sweep "
                        f"SKIPPED {resweep_summary.tuples_failed} tuple(s) "
                        "(SweepComboError) — the recount is partial, so the byte-diff "
                        "cannot be trusted; re-run after the failing tuple(s) redo",
                        file=sys.stderr,
                    )
                if stage3_grid is not None:
                    from brutex.engine.stage3_wire import run_stage3_pass

                    pass_report = run_stage3_pass(
                        stage3_grid,
                        results_root=scratch,
                        bars_for=lambda s, i: slices.get((s, i), []),
                        audit=audit,
                        exchange=exchange,
                        segment=segment,
                        start=start,
                        end=end,
                    )
                    # Condition B6: the recomputed train hash must match the
                    # manifest's recorded one — the cell path digests fold it.
                    if pass_report.train_snapshot_hash != recorded_train_hash:
                        train_hash_ok = False
                        print(
                            "[brutex] stage3 cells degraded to INCONCLUSIVE: recount "
                            f"train hash {pass_report.train_snapshot_hash!r} != "
                            f"manifest's {recorded_train_hash!r} (condition B6)"
                        )
            cell_evidence = (
                context_match
                and stage3_grid is not None
                and cells_note is None
                and train_hash_ok
                and knobs_from_audit
            )
            try:
                rec_report = compare_recount(
                    out_root,
                    scratch,
                    plan_expected_stores(
                        out_root, grid, stage3=stage3 if stage3_grid is not None else None
                    ),
                    context_match=context_match,
                    cell_evidence=cell_evidence,
                )
            except SweepVerifyError as exc:
                print(f"[brutex] sweep-verify error: {exc}", file=sys.stderr)
                return EXIT_USER_ERROR
            print(rec_report.render())
            reports.append(rec_report)
            if any(
                f.code
                in ("recount_divergence_inconclusive", "cell_recount_divergence_inconclusive")
                for f in rec_report.findings
            ) or (stage3 is not None and stage3.enabled and stage3_grid is None):
                inconclusive_only = True
            # Heal-plan emission (DEC-SWEEP-HEAL-001 C1/C2): heal-grade
            # evidence exists ONLY under a matched context — the plan freezes
            # the condemnation-time hashes for `brutex sweep-heal`.
            if emit_heal_plan:
                if not context_match:
                    print(
                        "[brutex] no heal plan emitted: divergence under a context "
                        "mismatch is INCONCLUSIVE, never heal-grade (C1)"
                    )
                elif not rec_report.clean:
                    from datetime import UTC, datetime

                    from brutex.engine.sweep_heal import build_heal_plan, write_heal_plan
                    from brutex.engine.sweep_supervisor import grid_identity
                    from brutex.engine.sweep_verify import RecordedContext

                    heal_plan = build_heal_plan(
                        rec_report,
                        plan_expected_stores(out_root, grid),
                        root=out_root,
                        grid_identity=grid_identity(grid),
                        context=RecordedContext(
                            data_snapshot_hash=snapshot,
                            indicator_version=indicator,
                            code_commit_sha=code_sha,
                        ),
                        created_utc=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
                    )
                    plan_path = write_heal_plan(out_root, heal_plan)
                    print(
                        f"[brutex] heal plan written: {plan_path} "
                        f"({len(heal_plan.entries)} condemned tuple(s)) — review, then "
                        f"`brutex sweep-heal {plan_path}` (dry-run) / `--execute`"
                    )
                else:
                    print("[brutex] no heal plan emitted: recount found no corruption")

    return sweep_verify_exit_code(
        has_findings=any(not report.clean for report in reports),
        inconclusive=inconclusive_only,
    )


def cmd_sweep_heal(  # pragma: no cover — orchestration shim over the 100%-tested sweep_heal core
    *,
    plan: Path,
    lake_root: Path | None,
    results_root: Path | None,
    execute: bool,
    allow_mass_quarantine: bool,
) -> int:
    """Quarantine doctor-condemned tuples + wipe the resume-skip stores.

    DRY-RUN by default (the assessment IS the preview — preview ≡ action);
    ``--execute`` acts. Refuses on stale evidence (C2, whole-heal), a running
    sweep (C6), or a mass quarantine without the override (C7) — exit
    :data:`EXIT_HEAL_REFUSED`. Never deletes: the trio moves to
    ``<root>/quarantine/`` and the wiped checkpoint/dedup-cache regenerate on
    the next sweep (DEC-SWEEP-HEAL-001).
    """
    import time

    from brutex.engine.sweep_heal import (
        SweepHealError,
        assess_heal,
        execute_heal,
        read_heal_plan,
    )
    from brutex.engine.sweep_supervisor import read_progress
    from brutex.orchestrator.audit import AuditWriter

    resolved_lake = _resolve_lake_root(lake_root)
    out_root = results_root if results_root is not None else resolved_lake / "sweeps"
    try:
        heal_plan = read_heal_plan(plan)
    except SweepHealError as exc:
        print(f"[brutex] sweep-heal error: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR

    # C6 best-effort guard: a fresh "running" progress snapshot refuses. A
    # snapshot older than this is a crashed sweep's leftover, not a live one.
    progress_fresh_seconds = 600.0
    progress = read_progress(out_root / "progress" / "sweep-progress.json")
    fresh = (time.time() - float(progress.get("updated_at") or 0.0)) < progress_fresh_seconds
    sweep_running = bool(progress.get("running")) and fresh

    try:
        assessment = assess_heal(
            out_root,
            heal_plan,
            sweep_running=sweep_running,
            allow_mass_quarantine=allow_mass_quarantine,
        )
    except SweepHealError as exc:
        print(f"[brutex] sweep-heal error: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    print(assessment.render(out_root, heal_plan))
    if not assessment.ok:
        return EXIT_HEAL_REFUSED
    if not execute:
        print("[brutex] DRY-RUN (no changes made) — re-run with --execute to act")
        return EXIT_OK

    audit_path = out_root / "audit" / "sweep-all.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with AuditWriter.open(audit_path, resume=audit_path.exists()) as audit:
            report = execute_heal(out_root, heal_plan, assessment, audit=audit)
    except SweepHealError as exc:
        print(f"[brutex] sweep-heal error: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    print(
        f"[brutex] healed — moved={report.moved} completed_partial={report.completed_partial} "
        f"already_quarantined={report.already_quarantined} wiped={list(report.wiped)}"
    )
    print(
        "[brutex] NOW re-run `brutex sweep-all` with the SAME flags BEFORE any lake "
        "change (C11 — the path digest folds in the data snapshot; a drifted lake "
        "re-sweeps to new paths). Intact tuples short-circuit at their sentinels; "
        "the cascade/ report is stale until that re-run."
    )
    return EXIT_OK


# ────────────────────────────────────────────────────────────────────────
# options-sim (DEC-OPTIONS-EXECUTOR-001) — winning spot run → priced options
# ────────────────────────────────────────────────────────────────────────
#
# These helpers compose the 100%-tested, golden-locked options EXECUTOR
# (brutex.options.executor) with the spot lake (signal replay via the engine)
# and the rolling-option lake (the OptionBarSource). The replay/orchestration
# helpers are real-lake + fno_math I/O paths — unrunnable on the
# Py3.11/no-wheel/no-data sandbox — so they are pragma'd and verified by the
# operator against a real Groww lake; the executor core they call is the
# tested piece. The lake READER (`_LakeOptionBarSource` + the expiry-series
# resolution) needs only polars + a tmp lake, so it is fully covered by
# tests/unit/test_cli_options_sim_source.py (R2-OC-1 regression net).


#: The rolling-option lake's series ``expiry_flag`` values, as written by
#: ``OptionSeriesCoordinate`` (``brutex/data/option_parquet_writer.py``,
#: DEC-EXPIRED-OPTIONS-TABLE-001): ``"WEEK"`` / ``"MONTH"``.
_EXPIRY_FLAG_WEEKLY = "WEEK"
_EXPIRY_FLAG_MONTHLY = "MONTH"

#: The lake's ``expiry_code`` selector vocabulary is Near/Next/Far — 0/1/2
#: (the request selector stamped per series; see the ``/api/v1/contracts``
#: route docstring in ``brutex/dashboard/app.py``). A requested expiry beyond
#: the Far selector is honestly unresolvable against this lake.
_MAX_EXPIRY_CODE = 2

#: The per-bar PRICING columns of the expired-options table — the inputs that
#: actually drive a priced trade. Two rows surviving the series filter at ONE
#: timestamp that agree on ALL of these are the same physical contract stored
#: under two aliased series (e.g. a monthly expiry that is also that week's
#: weekly); rows that disagree are a genuine ambiguity. The NULLABLE
#: annotation columns (``spot`` / ``iv`` / ``open_interest``) are deliberately
#: EXCLUDED: two aliased series pulled independently can legitimately drift on
#: them (one captured spot, the other stored None) while pricing identically —
#: keying ambiguity on annotations over-rejected priceable trades as NO_DATA
#: (hostile-review MINOR, DEC-OPTIONS-EXEC-COST-AUDIT-R2-001). ``strike`` is
#: excluded as redundant: ``load_pair`` already filters it to a constant.
_OPTION_PRICING_COLUMNS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
)


def _expiry_series_candidates(
    instrument: Any,
    expiry_date: date,
    *,
    on_date: date,
) -> tuple[tuple[str, int], ...]:
    """Resolve a requested absolute *expiry_date* to rolling-lake series keys.

    The expired-options table stores NO absolute expiry column — each bar
    carries only the pull-request series selector ``(expiry_flag,
    expiry_code)`` (``"WEEK"``/``"MONTH"`` × Near/Next/Far;
    DEC-EXPIRED-OPTIONS-TABLE-001). This maps the executor's absolute
    *expiry_date*, as seen from *on_date* (the lookup bar's IST trading
    date), onto that vocabulary using the SAME expiry calendar that generated
    the request (:mod:`brutex.options.expiry_calendar` — imported, never
    re-derived, CLAUDE.md §11 COMMON).

    Parameters
    ----------
    instrument
        The :class:`~brutex.options.lot_size_history.OptionsUnderlying`.
    expiry_date
        The absolute expiry the trade requested.
    on_date
        The IST trading date of the bar being looked up (the reference the
        rolling Near/Next/Far selectors are relative to).

    Returns
    -------
    tuple[tuple[str, int], ...]
        Every matching ``(expiry_flag, expiry_code)`` pair — a monthly expiry
        that is also that week's weekly matches BOTH series (the same
        physical contract). Empty when the expiry is unresolvable: not on the
        calendar as seen from *on_date*, beyond the Far selector, or
        *on_date* precedes the recorded calendar history (pre-2020 —
        DEC-EXPIRY-REGIME-2020-001, MIGRATION.md #1178(c)) — the caller
        then rejects honestly (NO_DATA), never guesses.
    """
    from brutex.options.expiry_calendar import next_monthly_expiry, next_weekly_expiry

    candidates: list[tuple[str, int]] = []
    try:
        cursor = on_date
        for code in range(_MAX_EXPIRY_CODE + 1):
            weekly = next_weekly_expiry(instrument, cursor)
            if weekly is None or weekly > expiry_date:
                break
            if weekly == expiry_date:
                candidates.append((_EXPIRY_FLAG_WEEKLY, code))
                break
            cursor = weekly + timedelta(days=1)
        cursor = on_date
        for code in range(_MAX_EXPIRY_CODE + 1):
            monthly = next_monthly_expiry(instrument, cursor)
            if monthly > expiry_date:
                break
            if monthly == expiry_date:
                candidates.append((_EXPIRY_FLAG_MONTHLY, code))
                break
            cursor = monthly + timedelta(days=1)
    except ValueError:
        # *on_date* precedes the expiry calendar's recorded history (e.g. a
        # 2019 signal vs the 2020-01-01 floor — DEC-EXPIRY-REGIME-2020-001,
        # MIGRATION.md #1178(c)) — unresolvable; the caller rejects, never
        # guesses (Golden Rule #1).
        return ()
    return tuple(candidates)


class _LakeOptionBarSource:
    """An ``OptionBarSource`` backed by the rolling-option Parquet lake.

    Reads the self-describing expired-options table (DEC-EXPIRED-OPTIONS-TABLE-001,
    ``brutex/data/option_parquet_writer.py``): one ``(year, month)`` partition per
    series, every bar carrying ``underlying_symbol`` / ``side`` / resolved
    ``strike`` / UTC ``timestamp`` + the series ``expiry_flag``/``expiry_code``
    + OHLC + ``iv`` columns. A leg is located by ``(underlying_symbol, side,
    strike, timestamp)`` **within the series the requested expiry resolves to**
    (R2-OC-1 — two concurrent series, e.g. a weekly and a monthly at the same
    strike, can never collide). A missing entry/exit bar (band-drift hole / not
    ingested), an unresolvable expiry, or a cross-series ambiguity returns
    ``None`` so the trade is rejected, never faked.

    HONEST ENVELOPE (hostile-review composition MINOR,
    DEC-OPTIONS-EXEC-COST-AUDIT-R2-001): the per-lookup-date series resolution
    assumes the lake's ROLLING accretion semantics — a bar at trading date D
    is stored under the Near/Next/Far rank the contract held AS OF D (each
    pull stamps the then-current selector, so one contract's life spans codes:
    the rolling regression test pins entry WEEK/1 → exit WEEK/0 for the SAME
    absolute expiry). A lake back-filled in ONE shot that stamps a contract's
    whole multi-week history under a single pull-time code would read honest
    ``NO_DATA`` for the bars whose stored rank differs from the as-of-date
    resolution — fail-SAFE (reject + logged), never a mis-priced trade;
    widening the filter to any code of a matching flag would reintroduce the
    exact concurrent-series collision R2-OC-1 removed, so it is deliberately
    NOT done.
    """

    def __init__(self, *, lake_root: Path) -> None:
        self._root = lake_root / "options"

    def load_pair(
        self,
        *,
        instrument: Any,
        expiry_date: Any,
        strike: int,
        option_type: str,
        entry_ts: datetime,
        exit_ts: datetime,
    ) -> Any:
        """Return the leg's entry/exit ``OptionBarPair`` from the lake, or ``None``.

        The requested *expiry_date* is resolved to the lake's series
        discriminator per lookup timestamp (:func:`_expiry_series_candidates`);
        an unresolvable expiry logs ``options_sim.expiry_series_unresolved``
        and returns ``None`` (→ the executor's NO_DATA reject).
        """
        from brutex.calendars.nse import IST
        from brutex.options.executor import OptionBarPair

        try:
            import polars as pl
        except ImportError:  # pragma: no cover — exercised only without the data extra
            return None
        if not self._root.exists():
            return None

        entry_date_ist = entry_ts.astimezone(IST).date()
        exit_date_ist = exit_ts.astimezone(IST).date()
        entry_series = _expiry_series_candidates(instrument, expiry_date, on_date=entry_date_ist)
        exit_series = _expiry_series_candidates(instrument, expiry_date, on_date=exit_date_ist)
        if not entry_series or not exit_series:
            import structlog

            structlog.get_logger("brutex.cli.options_sim").warning(
                "options_sim.expiry_series_unresolved",
                instrument=str(getattr(instrument, "value", instrument)),
                expiry_date=str(expiry_date),
                entry_date=str(entry_date_ist),
                exit_date=str(exit_date_ist),
            )
            return None

        entry_utc = entry_ts.astimezone(UTC)
        exit_utc = exit_ts.astimezone(UTC)
        months = {(entry_utc.year, entry_utc.month), (exit_utc.year, exit_utc.month)}
        frames = [
            pl.read_parquet(p)
            for year, month in months
            for p in self._root.glob(f"*/{year:04d}-{month:02d}.parquet")
        ]
        if not frames:
            return None
        df = pl.concat(frames) if len(frames) > 1 else frames[0]
        df = df.filter(
            (pl.col("underlying_symbol") == instrument.value)
            & (pl.col("side") == option_type)
            & (pl.col("strike") == float(strike))
        )

        entry_bar = self._row_at(df, entry_utc, option_type, series=entry_series)
        exit_bar = self._row_at(df, exit_utc, option_type, series=exit_series)
        if entry_bar is None or exit_bar is None:
            return None
        return OptionBarPair(entry_bar=entry_bar, exit_bar=exit_bar)

    @staticmethod
    def _row_at(
        df: Any, ts_utc: datetime, side: str, *, series: tuple[tuple[str, int], ...]
    ) -> Any:
        """Build the ``OptionBar`` matching *ts_utc* within *series*, or ``None``.

        Rows are matched on ``timestamp`` AND the resolved series
        discriminator (``expiry_flag``/``expiry_code``). Multiple surviving
        rows that agree on every PRICING column (:data:`_OPTION_PRICING_COLUMNS`
        — open/high/low/close/volume; the nullable ``spot``/``iv``/
        ``open_interest`` annotations are excluded so aliased-series annotation
        drift never over-rejects) are the SAME contract stored under two
        aliased series (a monthly that is also that week's weekly) — the
        most-annotated survivor is kept (every survivor prices identically) —
        and a REPEATED series key among them (a poisoned-era physical
        duplicate) keeps the first row of that ordering and is WARNED via the
        COMMON read gate, never silent (DEC-LAKE-READ-UNIQUE-001; the write
        side dedups on the same composite key, ``option_parquet_writer``).
        Rows that DISAGREE on any pricing column are a genuine ambiguity: the
        lookup logs ``options_sim.ambiguous_option_rows`` and returns ``None``
        (→ NO_DATA), never an arbitrary row (R2-OC-1).
        """
        from decimal import Decimal

        import polars as pl

        from brutex.data.lake_read import log_read_duplicates
        from brutex.data.models import OptionBar

        series_mask = pl.lit(False)
        for flag, code in series:
            series_mask = series_mask | (
                (pl.col("expiry_flag") == flag) & (pl.col("expiry_code") == code)
            )
        hit = df.filter((pl.col("timestamp") == ts_utc) & series_mask)
        if hit.height == 0:
            return None
        if hit.height > 1:
            if hit.select(list(_OPTION_PRICING_COLUMNS)).unique().height > 1:
                import structlog

                structlog.get_logger("brutex.cli.options_sim").warning(
                    "options_sim.ambiguous_option_rows",
                    timestamp=str(ts_utc),
                    side=side,
                    n_rows=int(hit.height),
                    series=[f"{flag}/{code}" for flag, code in series],
                )
                return None
            # Surviving rows AGREE on every pricing column: distinct series
            # keys are the SAME physical contract under aliased series (priced
            # normally + silently, keep-first via `row(0)` below), while a
            # REPEATED series key is a poisoned-era physical duplicate —
            # keep-first + the ONE aggregated WARNING, never silent
            # (DEC-LAKE-READ-UNIQUE-001; no-op when every series key is
            # distinct). Price-conflicts are 0 by construction here: a
            # pricing-DISAGREEING survivor took the ambiguity reject above;
            # annotation drift among agreeing rows is benign (see below).
            log_read_duplicates(
                source=f"options_sim:{side}@{ts_utc.isoformat()}",
                dropped=hit.height - hit.select(["expiry_flag", "expiry_code"]).unique().height,
                conflicts=0,
            )
            # Among the price-agreeing survivors prefer the MOST-ANNOTATED row
            # (fewest None spot/iv/open_interest — the nullable annotations the
            # ambiguity key deliberately excludes), so an aliased pair with
            # annotation drift keeps its annotations instead of arbitrarily
            # dropping them; the priced result is identical for any survivor.
            hit = hit.sort(
                pl.col("spot").is_null().cast(pl.Int8)
                + pl.col("iv").is_null().cast(pl.Int8)
                + pl.col("open_interest").is_null().cast(pl.Int8),
                maintain_order=True,
            )
        r = hit.row(0, named=True)

        def _dec(value: object) -> Decimal | None:
            return None if value is None else Decimal(str(value))

        return OptionBar(
            timestamp=r["timestamp"].replace(tzinfo=UTC)
            if r["timestamp"].tzinfo is None
            else r["timestamp"],
            open=Decimal(str(r["open"])),
            high=Decimal(str(r["high"])),
            low=Decimal(str(r["low"])),
            close=Decimal(str(r["close"])),
            volume=int(r["volume"]),
            open_interest=None if r["open_interest"] is None else int(r["open_interest"]),
            side=side,
            strike=_dec(r["strike"]),
            spot=_dec(r["spot"]),
            iv=_dec(r["iv"]),
        )


def _replay_spot_signals_for_row(  # pragma: no cover — engine + lake I/O, operator-verified
    row: Any,
    *,
    lake_root: Path,
    catalog_path: Path,
    exchange: str,
    segment: str,
    start: str,
    end: str,
    expiry_kind: Any,
) -> list[Any]:
    """Re-derive the winning combo's spot round-trips → ``SpotSignal`` inputs.

    Replays ``row.mask_bits`` over the spot lake for ``(row.instrument, row.tf)``
    via the SAME Stage-1A matrix → mask projection → trade-sim path the sweep
    used, then lifts each closed trade into a :class:`SpotSignal` (the entry
    bar's OPEN is the spot at entry; the expiry is resolved per the calendar).
    """
    from brutex.data.catalog import Catalog
    from brutex.data.catalog_reconcile import reconcile_catalog_from_lake
    from brutex.data.timeframes import trading_minutes_for_lake_interval
    from brutex.engine.combo_mask import mask_to_entry_signals
    from brutex.engine.stage1a import build_bar_bits
    from brutex.engine.trade_sim import simulate_trades
    from brutex.feed.replay import ReplayFeed
    from brutex.feed.resampler import INTERVAL_SECONDS
    from brutex.options.executor import signals_from_spot_trades
    from brutex.options.lot_size_history import OptionsUnderlying
    from brutex.options.strike_rules import TradeDirection
    from brutex.strategies.types import Side

    interval = row.tf
    if interval not in INTERVAL_SECONDS:
        raise ValueError(f"unknown timeframe label {interval!r} on result row")
    # Trading minutes, not wall-clock (1day -> 375-min session, NOT 1440 — DEC-TF-VOCAB-21-001).
    tf_minutes = trading_minutes_for_lake_interval(interval)
    is_bull = row.direction.lower() in ("bull", "buy", "long")
    side = Side.BUY if is_bull else Side.SELL
    direction = TradeDirection.BULL if is_bull else TradeDirection.BEAR
    underlying = OptionsUnderlying(row.instrument)

    with Catalog.open(catalog_path) as cat:
        reconcile_catalog_from_lake(
            lake_root=lake_root,
            catalog=cat,
            exchange=exchange,
            segment=segment,
            symbol=row.instrument,
            interval=interval,
        )
        feed = ReplayFeed(
            catalog=cat,
            lake_root=lake_root,
            exchange=exchange,
            segment=segment,
            symbols=[row.instrument],
            interval=interval,
            start=_parse_dt(start),
            end=_parse_dt(end),
        )
        bars = [event.bar for event in feed]

    if not bars:
        return []
    bar_bits = build_bar_bits(bars)
    entry_signals = mask_to_entry_signals(bar_bits, row.mask_bits)
    trades = simulate_trades(bars, entry_signals, direction=side, tf_minutes=tf_minutes)
    # Keep-FIRST spot-open map (DEC-LAKE-READ-UNIQUE-001): a plain comprehension
    # is LAST-wins on a duplicated timestamp — the COMMON gate keeps the first
    # bar + WARNs (the catalog gate upstream already collapses; belt-and-braces).
    from brutex.data.lake_read import bars_by_ts_first

    open_by_ts = {
        ts: bar.open
        for ts, bar in bars_by_ts_first(
            bars, source=f"options_sim_spot:{row.instrument}/{row.tf}"
        ).items()
    }
    return signals_from_spot_trades(
        trades,
        instrument=underlying,
        direction=direction,
        spot_open_at=lambda ts: open_by_ts[ts],
        expiry_kind=expiry_kind,
    )


def _render_options_leaderboard(row: Any, results: tuple[Any, ...]) -> str:
    """Render the per-rule priced-options leaderboard for one winning spot run.

    A pure formatter over ``(row, OptionsRuleResult…)`` — no lake I/O — so it
    is directly unit-tested (the former ``pragma: no cover`` was LIFTED when
    the DEC-ETC-REGIME-HISTORY-001 honesty footer added real derivation logic;
    tests/unit/test_cli_options_leaderboard.py).
    """
    from brutex.money import format_inr

    lines = [
        f"options-sim · {row.instrument} {row.tf} {row.direction} · run {row.run_id[:12]}…",
        f"{'rule':<22} {'trades':>7} {'win%':>6} {'gross':>16} {'charges':>14} {'net':>16}",
    ]
    for res in sorted(results, key=lambda r: r.net_pnl_paisa, reverse=True):
        lines.append(
            f"{res.rule_name:<22} {res.n_accepted:>7} {res.win_rate * 100:>5.1f}% "
            f"{format_inr(res.gross_pnl_paisa):>16} {format_inr(res.total_charges_paisa):>14} "
            f"{format_inr(res.net_pnl_paisa):>16}"
        )
    # DEC-ETC-REGIME-HISTORY-001 honesty footer: trades skipped on an
    # UNVERIFIED cost-regime window are surfaced, never silently absorbed.
    # The boundary date is derived from the regime-table DATA (the max
    # refusal-window end across exchanges), never hardcoded.
    # Direct attribute access, deliberately NOT getattr(..., 0): a future
    # field rename on OptionsRuleResult must fail LOUD here, never silently
    # render no footer forever (the honesty purpose would be defeated).
    n_unverified = sum(r.n_rejected_cost_regime_unverified for r in results)
    if n_unverified:
        from brutex.costs.constants import etc_unverified_windows

        ends = [end for spans in etc_unverified_windows().values() for _start, end in spans if end]
        verified_from = max(ends).isoformat() if ends else "open"
        lines.append(
            f"⚠ {n_unverified} trade(s) fell in the UNVERIFIED pre-{verified_from} ETC "
            f"window — skipped, never priced with a fabricated rate "
            f"(COSTS_VERIFIED §20 row 1)"
        )
    return "\n".join(lines)


def _options_sim_for_row(  # pragma: no cover — composes real-lake replay + tested executor
    row: Any,
    *,
    lake_root: Path,
    catalog_path: Path,
    exchange: str,
    segment: str,
    start: str,
    end: str,
    expiry: str,
    broker: str,
    audit_path: Path,
) -> tuple[Any, ...]:
    """Replay a winning spot run into priced per-rule options results + audit."""
    from brutex.options.executor import ExpiryKind, execute_options_sim
    from brutex.orchestrator.audit import AuditWriter

    expiry_kind = ExpiryKind.MONTHLY if expiry.lower() == "monthly" else ExpiryKind.WEEKLY
    signals = _replay_spot_signals_for_row(
        row,
        lake_root=lake_root,
        catalog_path=catalog_path,
        exchange=exchange,
        segment=segment,
        start=start,
        end=end,
        expiry_kind=expiry_kind,
    )
    source = _LakeOptionBarSource(lake_root=lake_root)
    with AuditWriter.open(audit_path, resume=audit_path.exists()) as audit:
        return execute_options_sim(
            signals,
            source=source,
            broker=broker,  # type: ignore[arg-type]
            audit=audit,
        )


def cmd_options_sim(  # pragma: no cover — orchestration over real-lake I/O + tested executor
    *,
    run_id: str,
    results_root: Path,
    lake_root: Path | None,
    catalog_path: Path | None,
    exchange: str,
    segment: str,
    start: str,
    end: str,
    expiry: str,
    broker: str,
) -> int:
    """Materialize + price the winning spot run *run_id* into options PnL (manual trigger).

    Resolves *run_id* in the persisted sweep results tree, replays its spot
    signals, selects an ATM±N leg per signal under each offset strike rule
    (DEC-OPTIONS-EXECUTOR-001), prices the round-trip via the locked Track-2 cost
    calculator, audits each rule result, and prints the per-rule leaderboard.
    """
    from brutex.engine.results_report import load_results_tree
    from brutex.engine.results_store import ResultsStoreError

    resolved_lake = _resolve_lake_root(lake_root)
    catalog = catalog_path if catalog_path is not None else resolved_lake / "catalog.duckdb"
    try:
        store = load_results_tree(results_root)
    except ResultsStoreError as exc:
        print(f"[brutex] options-sim: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    row = store.get(run_id)
    if row is None:
        print(
            f"[brutex] options-sim: run-id {run_id!r} not found under {results_root}",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR

    audit_path = results_root / "audit" / f"options-sim-{run_id[:12]}.jsonl"
    results = _options_sim_for_row(
        row,
        lake_root=resolved_lake,
        catalog_path=catalog,
        exchange=exchange,
        segment=segment,
        start=start,
        end=end,
        expiry=expiry,
        broker=broker,
        audit_path=audit_path,
    )
    total_signals = results[0].n_signals if results else 0
    if total_signals == 0:
        print(
            f"[brutex] options-sim: no spot signals replayed for {run_id[:12]}… "
            f"(check --start/--end window + spot lake)",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR
    print(_render_options_leaderboard(row, results))
    print(f"[brutex] options-sim audit: {audit_path}")
    return EXIT_OK


def _maybe_run_options_sim_on_winners(  # pragma: no cover — auto-hook over real-lake I/O
    *,
    out_root: Path,
    lake_root: Path,
    catalog_path: Path,
    exchange: str,
    segment: str,
    start: str,
    end: str,
    expiry: str,
    broker: str,
    top: int,
) -> None:
    """Automatic post-sweep options-sim on the top winners (zero-touch).

    Runs after a sweep persists its winners. Best-effort + non-fatal: when the
    rolling-option lake is absent (spot-only setup) or a replay raises, it logs
    and returns without failing the sweep.
    """
    if not (lake_root / "options").exists():
        print("[brutex] options-sim: skipped (no rolling-option lake at <lake>/options)")
        return
    from brutex.engine.results_report import load_results_tree
    from brutex.engine.results_store import ResultsStoreError

    try:
        store = load_results_tree(out_root)
    except ResultsStoreError as exc:
        print(f"[brutex] options-sim: skipped ({exc})")
        return
    winners = store.top_k(top, by="net_pnl_paisa")
    print(f"[brutex] options-sim: pricing options for top {len(winners)} winner(s)…")
    for row in winners:
        audit_path = out_root / "audit" / f"options-sim-{row.run_id[:12]}.jsonl"
        try:
            results = _options_sim_for_row(
                row,
                lake_root=lake_root,
                catalog_path=catalog_path,
                exchange=exchange,
                segment=segment,
                start=start,
                end=end,
                expiry=expiry,
                broker=broker,
                audit_path=audit_path,
            )
        except (ValueError, KeyError, OSError) as exc:
            print(f"[brutex] options-sim: {row.run_id[:12]}… skipped ({exc})")
            continue
        print(_render_options_leaderboard(row, results))


# ────────────────────────────────────────────────────────────────────────
# Main dispatcher
# ────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """Entry point — parse args and dispatch to handler.

    A :class:`CliUserError` raised by any command or shared helper below is
    converted to the standard one-line ``[brutex] <message>`` on stderr +
    :data:`EXIT_USER_ERROR` — the CLI's last-resort user-error net
    (DEC-CLI-USER-ERROR-HYGIENE-001). Anything else still propagates raw: a
    genuine code bug must traceback, never be papered over (Golden Rule #10).
    """
    try:
        return _dispatch(argv)
    except CliUserError as exc:
        print(f"[brutex] {exc}", file=sys.stderr)
        return EXIT_USER_ERROR


def _dispatch(argv: list[str] | None) -> int:
    """Parse *argv* and dispatch to the command handler (may raise CliUserError)."""
    from brutex.config.dotenv import bootstrap_env

    # GAP C6 — load a persisted ``.env`` ONCE at startup (no-op when absent;
    # never overrides an already-set env var, secret values never logged).
    bootstrap_env()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None or args.command == "version":
        return cmd_version()
    # DEC-THREAD-BUDGET-BOUNDS-001 — bound the polars global thread pool ONCE
    # per process, at the ONE entrypoint every BruteX process (CLI fetch AND
    # the in-process uvicorn dashboard) enters, BEFORE the first lazy `import
    # polars` any dispatch below can trigger (verified: importing brutex.cli
    # pulls in neither polars nor duckdb; the polars-free `version` fast path
    # above stays log-free — its byte-exact stdout contract is test-pinned).
    # Runs AFTER bootstrap_env so a persisted BRUTEX_POLARS_THREADS /
    # POLARS_MAX_THREADS in ~/.brutex/.env is honoured. Safe by construction:
    # an operator-preset POLARS_MAX_THREADS is respected, and an
    # already-imported polars logs one loud too-late warning per process
    # instead of pretending a bound applied.
    apply_polars_thread_bound()
    if args.command == "status":
        return cmd_status(run_id=args.run_id)
    if args.command == "audit":
        if args.audit_command != "verify":
            parser.error(f"unknown audit subcommand: {args.audit_command}")
        return cmd_audit_verify(audit_file=args.audit_file, audit_format=args.audit_format)
    if args.command == "catalog":
        if args.catalog_command != "reconcile":
            parser.error(f"unknown catalog subcommand: {args.catalog_command}")
        return cmd_catalog_reconcile(lake_root=args.lake_root)
    if args.command == "lake":
        if args.lake_command != "audit-unique":
            parser.error(f"unknown lake subcommand: {args.lake_command}")
        return cmd_lake_audit_unique(
            lake_root=args.lake_root,
            as_json=args.as_json,
            exchange=args.exchange,
            segment=args.segment,
            symbol=args.symbol,
            interval=args.interval,
            no_options=args.no_options,
        )
    if args.command == "reopen-skipped":
        return cmd_reopen_skipped(
            lake_root=args.lake_root,
            symbol=args.symbol,
            market=args.market,
            limit=args.limit,
        )
    if args.command == "detect-session-drift":
        return cmd_detect_session_drift(lake_root=args.lake_root, month=args.month)
    if args.command == "auth":  # pragma: no cover — interactive / thin print shims
        if args.auth_command == "set":
            return cmd_auth_set()
        if args.auth_command == "status":
            return cmd_auth_status()
        if args.auth_command == "clear":
            return cmd_auth_clear()
        parser.error(f"unknown auth subcommand: {args.auth_command}")
    if args.command == "dashboard":
        return cmd_dashboard(
            lake_root=args.lake_root,
            host=args.host,
            port=args.port,
        )
    if args.command == "run":
        return cmd_run(
            lake_root=args.lake_root,
            host=args.host,
            port=args.port,
            open_browser=not args.no_browser,
        )
    if args.command == "fetch":
        return cmd_fetch(
            lake_root=args.lake_root,
            start=args.start,
            end=args.end,
            interval=args.interval,
            window_days=args.window_days,
            dry_run=args.dry_run,
            inspect=args.inspect,
            rows_per_chunk=args.rows_per_chunk,
            universe=args.universe,
            instruments=args.instruments,
            auto_retry=not args.no_auto_retry,
            retry_failed=args.retry_failed,
            until_clean=args.until_clean,
            until_clean_rounds=args.until_clean_rounds,
            max_retry_minutes=args.max_retry_minutes,
        )
    if args.command == "enrich-greeks":
        return cmd_enrich_greeks(
            args.lake_root,
            underlying=args.underlying,
            start=args.start,
            end=args.end,
            dry_run=args.dry_run,
        )
    if args.command == "doctor":
        if args.live:
            return cmd_doctor_live(args)
        return cmd_doctor(
            audit_file=args.audit_file,
            audit_dir=args.audit_dir,
            runtime_file=args.runtime_file,
            live_subjects_file=args.live_subjects_file,
            replay_action_filter=args.replay_action_filter,
        )
    if args.command == "monitor":
        return cmd_monitor(
            poll_interval=args.poll_interval,
            ticks=args.ticks,
        )
    if args.command == "notify":  # pragma: no cover — live network + cli-omitted
        if args.notify_command != "test":
            parser.error(f"unknown notify subcommand: {args.notify_command}")
        return cmd_notify_test()
    if args.command == "wipe":
        return cmd_wipe(execute=args.execute, assume_yes=args.yes)
    if args.command == "sweep":
        return cmd_sweep(
            lake_root=args.lake_root,
            catalog_path=args.catalog_path,
            exchange=args.exchange,
            segment=args.segment,
            symbol=args.symbol,
            interval=args.interval,
            start=args.start,
            end=args.end,
            direction=args.direction,
            k=args.k,
            conditions=args.conditions,
            stop_loss_paisa=args.stop_loss_paisa,
            take_profit_paisa=args.take_profit_paisa,
            top=args.top,
            min_hits=args.min_hits,
            sort_by=args.sort_by,
            validate_oos=args.validate_oos,
            oos_only=args.oos_only,
            allow_out_of_scope=args.allow_out_of_scope,
        )
    if args.command == "sweep-all":
        return cmd_sweep_all(
            lake_root=args.lake_root,
            catalog_path=args.catalog_path,
            results_root=args.results_root,
            exchange=args.exchange,
            segment=args.segment,
            symbols=args.symbols,
            intervals=args.intervals,
            start=args.start,
            end=args.end,
            directions=args.directions,
            k=args.k,
            fib_anchors=args.fib_anchors,
            conditions=args.conditions,
            sl=args.sl,
            tp=args.tp,
            top=args.top,
            mode=args.mode,
            max_tuples=args.max_tuples,
            max_combos=args.max_combos,
            data_snapshot_hash=args.data_snapshot_hash,
            overfit_gate=args.overfit_gate,
            dsr_min=args.dsr_min,
            options_sim=args.options_sim,
            options_expiry=args.options_expiry,
            options_broker=args.options_broker,
            stage3_enable=args.stage3_enable,
            stage3_top_k=args.stage3_top_k,
            stage3_fevals=args.stage3_fevals,
            stage3_restarts=args.stage3_restarts,
            allow_missing_slices=args.allow_missing_slices,
            sweep_cache_map_mb=args.sweep_cache_map_mb,
            sweep_cache_bloom_capacity=args.sweep_cache_bloom_capacity,
            allow_out_of_scope=args.allow_out_of_scope,
            acknowledge_sla_overrun=args.acknowledge_sla_overrun,
        )
    # Scheduled/daemon sweep runner (DEC-SWEEP-SCHEDULER-001) — reuses cmd_sweep_all
    # for the grid; the scheduler core decides only WHEN each cycle fires.
    if args.command == "sweep-daemon":
        # DEC-ENGINE-SCOPE-GUARD-001: the research escape hatch is NEVER
        # available on the unattended daemon — refuse loudly instead of
        # silently ignoring the flag (Golden Rule #10 honesty).
        if args.allow_out_of_scope:
            print(
                "[brutex] sweep-daemon REFUSED: --allow-out-of-scope is a manual "
                "research escape hatch and is not available on the unattended "
                "daemon (DEC-ENGINE-SCOPE-GUARD-001)",
                file=sys.stderr,
            )
            return EXIT_USER_ERROR
        return cmd_sweep_daemon(
            schedule=args.schedule,
            once=args.once,
            max_cycles=args.max_cycles,
            tz_offset_minutes=args.tz_offset_minutes,
            lake_root=args.lake_root,
            catalog_path=args.catalog_path,
            results_root=args.results_root,
            exchange=args.exchange,
            segment=args.segment,
            symbols=args.symbols,
            intervals=args.intervals,
            start=args.start,
            end=args.end,
            directions=args.directions,
            k=args.k,
            fib_anchors=args.fib_anchors,
            conditions=args.conditions,
            sl=args.sl,
            tp=args.tp,
            top=args.top,
            mode=args.mode,
            max_tuples=args.max_tuples,
            max_combos=args.max_combos,
            data_snapshot_hash=args.data_snapshot_hash,
            overfit_gate=args.overfit_gate,
            dsr_min=args.dsr_min,
            options_sim=args.options_sim,
            options_expiry=args.options_expiry,
            options_broker=args.options_broker,
            stage3_enable=args.stage3_enable,
            stage3_top_k=args.stage3_top_k,
            stage3_fevals=args.stage3_fevals,
            stage3_restarts=args.stage3_restarts,
        )
    # Process supervisor (DEC-SUPERVISOR-AUTORESTART-001) — auto-relaunch the
    # sweep daemon on a hard crash; composes brutex.orchestrator.supervisor.
    if args.command == "supervisor":
        return cmd_supervisor(
            child_args=args.child_args,
            backoff_base_seconds=args.backoff_base_seconds,
            backoff_factor=args.backoff_factor,
            backoff_cap_seconds=args.backoff_cap_seconds,
            reset_after_uptime_seconds=args.reset_after_uptime_seconds,
            max_restarts=args.supervisor_max_restarts,
            shutdown_grace_seconds=args.shutdown_grace_seconds,
            lake_root=args.lake_root,
        )
    # Thin dispatch over cmd_results_show (DEC-SWEEP-WINNERS-BY-NAME-001).
    if args.command == "results" and args.results_command == "show":
        return cmd_results_show(
            results_root=args.results_root,
            top=args.top,
            sort_by=args.sort_by,
        )
    # options-sim (DEC-OPTIONS-EXECUTOR-001) — price a winning spot run as options.
    if args.command == "options-sim":
        return cmd_options_sim(
            run_id=args.run_id,
            results_root=args.results_root,
            lake_root=args.lake_root,
            catalog_path=args.catalog_path,
            exchange=args.exchange,
            segment=args.segment,
            start=args.start,
            end=args.end,
            expiry=args.expiry,
            broker=args.broker,
        )
    # Manual Stage-4 cascade re-run (DEC-CASCADE-WIRE-001).
    if args.command == "cascade":
        return cmd_cascade(
            lake_root=args.lake_root,
            results_root=args.results_root,
            catalog_path=args.catalog_path,
            run_id=args.run_id,
        )
    if args.command == "verify-lookahead":
        return cmd_verify_lookahead(
            lake_root=args.lake_root,
            catalog_path=args.catalog_path,
            exchange=args.exchange,
            segment=args.segment,
            symbol=args.symbol,
            interval=args.interval,
            start=args.start,
            end=args.end,
            run_id=args.run_id,
        )
    # Results-tree doctor (DEC-SWEEP-VERIFY-001) — report-only verification.
    if args.command == "sweep-verify":
        return cmd_sweep_verify(
            lake_root=args.lake_root,
            catalog_path=args.catalog_path,
            results_root=args.results_root,
            exchange=args.exchange,
            segment=args.segment,
            symbols=args.symbols,
            intervals=args.intervals,
            start=args.start,
            end=args.end,
            directions=args.directions,
            k=args.k,
            fib_anchors=args.fib_anchors,
            conditions=args.conditions,
            sl=args.sl,
            tp=args.tp,
            top=args.top,
            mode=args.mode,
            max_tuples=args.max_tuples,
            max_combos=args.max_combos,
            data_snapshot_hash=args.data_snapshot_hash,
            indicator_version=args.indicator_version,
            code_commit_sha=args.code_commit_sha,
            recount=args.recount,
            scratch_root=args.scratch_root,
            allow_context_mismatch=args.allow_context_mismatch,
            emit_heal_plan=args.emit_heal_plan,
            stage3_enable=args.stage3_enable,
            stage3_top_k=args.stage3_top_k,
            stage3_fevals=args.stage3_fevals,
            stage3_restarts=args.stage3_restarts,
            allow_out_of_scope=args.allow_out_of_scope,
        )
    # Quarantine heal (DEC-SWEEP-HEAL-001) — dry-run by default.
    if args.command == "sweep-heal":
        return cmd_sweep_heal(
            plan=args.plan,
            lake_root=args.lake_root,
            results_root=args.results_root,
            execute=args.execute,
            allow_mass_quarantine=args.allow_mass_quarantine,
        )
    # Nightly zero-click chain (DEC-SWEEP-NIGHTLY-AUTOMATION-001).
    if args.command == "nightly":
        if args.nightly_command == "run":
            return cmd_nightly_run(args)
        if args.nightly_command == "backup":
            return cmd_nightly_backup(
                args.lake_root,
                results_root=args.results_root,
                backup_root=args.backup_root,
                keep=args.keep,
            )
        if args.nightly_command == "restore":
            return cmd_nightly_restore(
                snapshot=args.snapshot,
                lake_root=args.lake_root,
                results_root=args.results_root,
                dry_run=args.dry_run,
                yes=args.yes,
            )
        if args.nightly_command == "install":
            return cmd_nightly_install(
                at=args.at,
                label=args.label,
                plist_dir=args.plist_dir,
                lake_root=args.lake_root,
                backup_root=args.backup_root,
            )
        if args.nightly_command == "uninstall":
            return cmd_nightly_uninstall(label=args.label, plist_dir=args.plist_dir)
        return cmd_nightly_status(
            lake_root=args.lake_root,
            results_root=args.results_root,
            label=args.label,
            plist_dir=args.plist_dir,
        )
    parser.error(f"unknown command: {args.command}")
    return EXIT_USER_ERROR  # type: ignore[unreachable]  # parser.error exits


if __name__ == "__main__":  # pragma: no cover — invoked only by [project.scripts]
    sys.exit(main())
