"""BruteX CLI entry point — ``brutex <command>``.

Registered in ``pyproject.toml`` as:

    [project.scripts]
    brutex = "brutex.cli:main"

After ``pip install -e .``, the ``brutex`` command is on PATH.

## Status

This CLI was rewritten on 2026-05-15 and re-baselined on 2026-05-26
(DEC-VENDOR-PIVOT-001). Per DEC-GROWW-DHAN-REVERT-001 / ADR-0002 the live
vendor is the Dhan Data API v2. Legacy ingest subcommands were
vendor-specific and are dropped. Track 1 will reintroduce
``brutex track1 ingest`` once the Dhan adapter lands in Phase 1+ of the
phase plan (see ``brutex/adapters/dhan/``).

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
        Fetch historical candles into the lake (Dhan adapter).

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
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from brutex import __version__
from brutex.config.paths import resolve_lake_root
from brutex.config.runtime import read_runtime_file
from brutex.orchestrator.audit import AuditChainCorruption, verify_chain

# ────────────────────────────────────────────────────────────────────────
# Exit codes
# ────────────────────────────────────────────────────────────────────────

EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_INTERNAL_ERROR = 2


# ────────────────────────────────────────────────────────────────────────
# Argparse builder
# ────────────────────────────────────────────────────────────────────────


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

    # ── auth (DEC-CREDENTIALS-STORE-001) — set/status/clear creds ──
    auth = sub.add_parser(
        "auth",
        help="Manage the stored Dhan credentials (set once, auto-load every session)",
    )
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    auth_sub.add_parser(
        "set",
        help="Interactively prompt (no echo) for the Dhan access token + client id and save them",
    )
    auth_sub.add_parser(
        "status",
        help="Print whether Dhan credentials are stored (booleans only; never values)",
    )
    auth_sub.add_parser(
        "clear",
        help="Delete the stored Dhan credentials file",
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
            "(default). Dhan fetches 1m + 1d only — the 19 intraday timeframes are "
            "resampled from the 1-minute base (DEC-DHAN-1M-ONLY-001)"
        ),
    )
    fetch.add_argument(
        "--window-days",
        type=int,
        default=None,
        help="Legacy chunk-window override — ignored on Dhan (tiled internally)",
    )
    fetch.add_argument(
        "--dry-run",
        action="store_true",
        help="Use synthetic candles (no Dhan creds) — proves the pipe + writes sample lake data",
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
            "ONLY load the keyless public instrument master (no Dhan keys), "
            "print the index/stock counts, and exit — populates the /universe "
            "list without auth (DEC-INSTRUMENT-LOAD-001)"
        ),
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
        help="Comma-separated condition bit indices 0..51 to sweep (default: all 52)",
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
    runtime = read_runtime_file()
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


def cmd_audit_verify(audit_file: Path) -> int:
    """Verify the hash chain of an audit-log file."""
    if not audit_file.exists():
        print(f"[brutex] audit file not found: {audit_file}", file=sys.stderr)
        return EXIT_USER_ERROR
    try:
        count = verify_chain(audit_file)
    except AuditChainCorruption as exc:
        print(f"[brutex] AUDIT CHAIN CORRUPT: {exc}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR
    print(f"[brutex] audit chain OK — {count} entries verified")
    return EXIT_OK


def cmd_auth_set() -> int:  # pragma: no cover — interactive getpass prompts
    """Interactively prompt (no echo) for the Dhan creds and save them.

    Reads the access token and client id via :func:`getpass.getpass` so neither
    is echoed to the terminal or recorded in shell history. The values are
    NEVER accepted as argv flags (they would leak via ``ps`` / shell
    history). Prints only the destination path + mode — never a value.
    """
    import getpass

    from brutex.config.credentials import (
        CredentialsError,
        DhanFileCredentials,
        save_dhan_credentials,
    )

    access_token = getpass.getpass("Dhan access token (input hidden): ")
    client_id = getpass.getpass("Dhan client id (input hidden): ")
    try:
        path = save_dhan_credentials(
            DhanFileCredentials(access_token=access_token, client_id=client_id)
        )
    except CredentialsError as exc:
        print(f"[brutex] could not save credentials: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    print(f"[brutex] credentials saved to {path} (mode 0600)")
    return EXIT_OK


def cmd_auth_status() -> int:  # pragma: no cover — thin print wrapper
    """Print whether Dhan credentials are stored (booleans only, no values)."""
    from brutex.config.credentials import credentials_path, credentials_present

    present = credentials_present()
    print(f"[brutex] credentials: {'set' if present else 'not set'}")
    print(f"[brutex] path: {credentials_path()}")
    return EXIT_OK


def cmd_auth_clear() -> int:  # pragma: no cover — thin print wrapper
    """Delete the stored Dhan credentials file (if any)."""
    from brutex.config.credentials import clear_dhan_credentials, credentials_path

    removed = clear_dhan_credentials()
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


def cmd_dashboard(lake_root: Path | None, host: str, port: int) -> int:
    """Start the dashboard FastAPI server."""
    try:
        import uvicorn

        from brutex.dashboard.app import DashboardState, build_app
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
    # Open a real InstrumentStore so the F&O Universe Explorer routes can call
    # store.load_snapshot(...). Passing the bare lake_root Path here was the
    # cause of the `'PosixPath' object has no attribute 'load_snapshot'` 500.
    # InstrumentStore.open() runs CREATE TABLE IF NOT EXISTS, so a fresh/empty
    # lake yields a valid empty store (universe shows 0 instruments, no crash).
    store: Any = None
    if lake_root is not None:
        from brutex.data.instrument_store import InstrumentStore

        store = InstrumentStore.open(lake_root / "instruments.duckdb")

        # Auto-populate the keyless instrument master on startup so /universe is
        # never empty on a fresh lake (DEC-INDEX-PARSE-FIX-001). Best-effort: a
        # network failure (the public CSV needs internet, no keys) must NOT stop
        # the dashboard from serving.
        try:  # pragma: no cover — live network + server path (cli.py coverage-excluded)
            from brutex.dashboard.auto_pull import should_reload_instrument_master
            from brutex.dashboard.instrument_loader import load_instrument_master
            from brutex.dashboard.universe_query import universe_summary

            # Lean-correct counts (DEC-INSTRUMENT-UNIVERSE-FAST-001): summarise
            # the IDX+EQ candidates plus the SQL-derived F&O-eligible set, so an
            # EQ is classed as a "stock". The bare load_snapshot path carried no
            # derivative legs, so every equity fell through unclassified — the
            # "25 indices · 0 stocks" message the operator hit (the data was fine;
            # only the startup tally was wrong).
            summary = universe_summary(
                store.list_index_equity_instruments(),
                fno_eligible=store.fno_eligible_underlyings(),
            )
            # Auto-heal: reload when the lake is empty, missing indices (the
            # pre-DEC-INDEX-PARSE-FIX-001 fingerprint), still holds FUT/CE/PE
            # legs (the pre-lean fingerprint — DEC-LEAN-REINGEST-FIX-001), OR
            # carries placeholder stock economics (lot_size=1, the pre-back-fill
            # fingerprint — DEC-STALE-ECONOMICS-HEAL-001). The lean refresh does a
            # full-snapshot replace + prune-to-latest, so a reload purges the
            # stale dump down to the ~236 enriched universe underlyings.
            if should_reload_instrument_master(
                index_count=summary["index_count"],
                stock_count=summary["stock_count"],
                legs_present=store.has_derivative_legs(),
                stale_economics=store.has_placeholder_stock_economics(),
            ):
                counts = load_instrument_master(store)
                _log.info(
                    "instrument_master.autoloaded",
                    indices=counts["index_count"],
                    stocks=counts["stock_count"],
                )
            else:
                _log.info(
                    "instrument_master.present",
                    indices=summary["index_count"],
                    stocks=summary["stock_count"],
                )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            _log.warning("instrument_master.autoload_skipped", error=str(exc))

        # NO auto-pull of expiries/contracts on startup (DEC-UNIVERSE-PULL-
        # PHASED-001, operator decree 2026-05-30: "remove the auto pull"). The
        # universe pull is now driven by the two MANUAL /universe buttons —
        # "Fetch all expiries" (Phase 1) then "Fetch all contracts" (Phase 2) —
        # so nothing hits Dhan until the operator clicks. The keyless
        # instrument master still auto-loads above (it needs no auth) so the
        # /universe list is populated and the buttons have something to pull.
        _log.info(
            "universe.autopull.disabled",
            hint="use the 'Fetch all expiries' / 'Fetch all contracts' buttons on /universe",
        )
    app = build_app(state, instrument_store=store)
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

    This is what ``python -m brutex run``, ``make run`` and the committed
    IntelliJ run configuration invoke (DEC-ONECLICK-RUN-001) — clone the
    repo, click ▶ Run, and the live dashboard opens. It resolves the lake
    root (``--lake-root`` → ``$BRUTEX_LAKE_ROOT`` → ``~/.brutex/lake``),
    prints a startup banner, optionally opens the browser, then delegates
    to :func:`cmd_dashboard`.

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
    """Parse a ``YYYY-MM-DD`` or full-ISO string to a UTC-aware datetime."""
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _resolve_lake_root(lake_root: Path | None) -> Path:
    """``--lake-root`` → ``$BRUTEX_LAKE_ROOT`` → ``~/.brutex/lake`` (via config.paths)."""
    if lake_root is not None:
        return lake_root
    return resolve_lake_root()


def _resolve_dhan_fetch_intervals(interval: str) -> list[str]:
    """Map the ``--interval`` flag to the Dhan FETCHED intervals.

    Dhan fetches only ``1minute`` + ``1day`` (the 19 intraday timeframes are
    resampled from the 1-minute base — DEC-DHAN-1M-ONLY-001). ``all`` →
    both; ``1m``/``1minute`` and ``1d``/``1day`` → that one; anything else
    raises :class:`ValueError` (a non-fetched timeframe).
    """
    from brutex.dashboard.pull_controller import DHAN_FETCH_INTERVALS

    if interval == "all":
        return list(DHAN_FETCH_INTERVALS)
    canonical = {
        "1m": "1minute",
        "1minute": "1minute",
        "1d": "1day",
        "1day": "1day",
    }.get(interval)
    if canonical is None:
        raise ValueError(
            f"--interval {interval!r} is not fetched on Dhan — fetch 1minute/1day only "
            "(the 19 intraday timeframes are resampled from the 1-minute base). "
            "Use --interval 1minute, 1day, or all."
        )
    return [canonical]


def _synthetic_index_instrument(spec: Any) -> Any:
    """Build a placeholder spot index ``Instrument`` from a brute-force spec.

    The dry-run :class:`SyntheticDhanAdapter` ignores the symbol when
    generating bars, so the ``symbol_name`` is the index's own name — a
    real ``InstrumentStore`` (with numeric Dhan SECURITY_IDs) is not required
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

    The live pull needs the real Dhan ``SECURITY_ID`` for each index, which
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


def _build_live_dhan_spot(  # pragma: no cover — live Dhan token + transport + network
    lake_root: Path, instruments: list[Any]
) -> tuple[Any, Any]:
    """Wire the real Dhan client + persisting Parquet sink → ``(adapter, sink)``.

    Mirrors :func:`brutex.dashboard.pull_controller._build_live_pull_adapter`
    (CLAUDE.md §11 COMMON): a token-authenticated ``DhanClient`` over the real
    ``urllib`` transport, a ``DhanMarketAdapter`` carrying the resolved
    instruments (its O(1) index must contain every symbol it fetches), and a
    ``ParquetBarSink`` over *lake_root*.
    """
    from brutex.adapters.dhan.adapter import DhanMarketAdapter
    from brutex.adapters.dhan.auth import resolve_dhan_credentials
    from brutex.adapters.dhan.client import DhanClient, urllib_transport
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    creds = resolve_dhan_credentials()
    client = DhanClient(
        access_token=creds.access_token,
        client_id=creds.client_id,
        transport=urllib_transport,
    )
    adapter = DhanMarketAdapter(client=client, instruments=tuple(instruments))
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    return adapter, sink


def _run_instrument_master_load(  # pragma: no cover — opens DuckDB store + live network
    lake_root: Path,
) -> dict[str, Any]:
    """Keylessly load the public Dhan instrument master into the lake.

    Opens the real :class:`~brutex.data.instrument_store.InstrumentStore` and
    delegates to the testable
    :func:`brutex.dashboard.instrument_loader.load_instrument_master` with NO
    injected ``http_get`` — i.e. the real SSRF-guarded public-CSV fetch, which
    needs NO Dhan API keys (DEC-INSTRUMENT-LOAD-001). Marked ``no cover``: the
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
) -> int:
    """Fetch candles for the 3 brute-force indices into the lake (§13.9 step 3).

    Drives the Dhan spot spine (:func:`~brutex.adapters.dhan.run_ingest.run_dhan_spot_ingest`):
    Dhan fetches only ``1minute`` + ``1day`` and the 19 intraday timeframes are
    resampled from the 1-minute base (DEC-DHAN-1M-ONLY-001), so ``--interval all``
    pulls the two base timeframes. ``--dry-run`` uses
    :class:`~brutex.adapters.dhan.synthetic_adapter.SyntheticDhanAdapter` so the
    full fetch → session-filter → sink pipeline runs offline (no Dhan creds);
    the live path resolves the brute-force index ``Instrument``s from the store.
    ``--universe`` first ingests the instrument master into the lake (the LAKE
    surface authorised by DEC-P0-063) BEFORE the index candle pull — the turnkey
    "pull everything" command. ``--instruments`` loads ONLY the keyless public
    instrument master (no Dhan keys), prints the index/stock counts, and exits —
    it populates the ``/universe`` list without auth (DEC-INSTRUMENT-LOAD-001).
    """
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

    from brutex.adapters.dhan.run_ingest import run_dhan_spot_ingest
    from brutex.adapters.dhan.synthetic_adapter import SyntheticDhanAdapter
    from brutex.dashboard.pull_controller import DHAN_HISTORY_FLOOR
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import NullBarSink, ParquetBarSink
    from brutex.universe import BRUTE_FORCE_INDICES

    resolved = _resolve_lake_root(lake_root)
    try:
        intervals = _resolve_dhan_fetch_intervals(interval)
    except ValueError as exc:
        print(f"[brutex] {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    # Default the start to the Track-1 2021-01-01 pull floor (DEC-DHAN-PULL-SCOPE-001)
    # so a bare `brutex fetch` pulls the full locked "2021-01-01 → today" history.
    floor_dt = datetime(
        DHAN_HISTORY_FLOOR.year, DHAN_HISTORY_FLOOR.month, DHAN_HISTORY_FLOOR.day, tzinfo=UTC
    )
    start_dt = _parse_dt(start) if start else floor_dt
    end_dt = _parse_dt(end) if end else datetime.now(UTC)
    if window_days is not None:
        print(
            "[brutex] --window-days is ignored on Dhan (1-minute is tiled internally)",
            file=sys.stderr,
        )

    if universe:
        # The legacy instruments→expiries→contracts universe ingest was
        # removed with the pre-pivot Groww adapter (DEC-DHAN-FINALIZE-001).
        # The Dhan path loads the instrument master keylessly via
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
            "[brutex] --inspect is not yet ported to the Dhan adapter — "
            "running to a NullBarSink (no persistence)",
            file=sys.stderr,
        )

    adapter: Any
    sink: Any
    fetch_instruments: list[Any]
    pace: Any = None
    on_rate_limit: Any = None
    on_success: Any = None
    if dry_run:
        fetch_instruments = [_synthetic_index_instrument(spec) for spec in BRUTE_FORCE_INDICES]
        adapter = SyntheticDhanAdapter(bars_per_window=rows_per_chunk)
        sink = (
            NullBarSink() if inspect else ParquetBarSink(writer=ParquetWriter(lake_root=resolved))
        )
        print("[brutex] fetch --dry-run → synthetic candles", file=sys.stderr)
    else:
        fetch_instruments = _resolve_brute_force_instruments(resolved)
        adapter, sink = _build_live_dhan_spot(resolved, fetch_instruments)
        if inspect:
            sink = NullBarSink()
        # Adaptive [3..5] r/s throttle for the live pull (DEC-DHAN-ADAPTIVE-RPS-001).
        from brutex.adapters.dhan.rate_control import DhanRateController

        _rate = DhanRateController()
        pace, on_rate_limit, on_success = _rate.pace, _rate.on_rate_limit, _rate.on_success
        print("[brutex] fetch (live Dhan)", file=sys.stderr)

    report = run_dhan_spot_ingest(
        adapter,
        sink,
        instruments=fetch_instruments,
        intervals=intervals,
        start=start_dt.date(),
        end=end_dt.date(),
        pace=pace,
        on_rate_limit=on_rate_limit,
        on_success=on_success,
    )
    print(
        f"[brutex] fetch done — windows={report.windows} bars_appended={report.bars_appended} "
        f"failed_pairs={report.failed_pairs} transient={report.transient_failures}"
    )
    print(f"[brutex] lake: {resolved}")
    return EXIT_OK if report.failed_pairs == 0 else EXIT_INTERNAL_ERROR


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
) -> int:
    """Brute-force sweep over lake bars → print the ranked top-combo table.

    Loads the bars for one ``(symbol, interval)`` window from the Parquet lake via
    the SAME ``Catalog`` / ``ReplayFeed`` path the engine uses, runs the assembled
    funnel (sweep → Stage-1B prune → hard gates → composite rank), and prints the
    self-describing leaderboard (``brutex.engine.sweep_report``). Signal-only —
    nothing is persisted (DEC-SWEEP-CLI-001). ``--exchange`` / ``--segment`` /
    ``--interval`` are the LAKE partition labels and must match what was ingested
    (segment ``INDEX`` for the spot indexes — the lake partition label, NOT the Dhan
    API's ``IDX_I`` request segment).
    """
    from brutex.data.catalog import Catalog
    from brutex.engine.sweep_report import sweep_report
    from brutex.feed.replay import ReplayFeed
    from brutex.feed.resampler import INTERVAL_SECONDS
    from brutex.ranking import MIN_TRADES
    from brutex.strategies.types import Side

    resolved_lake = _resolve_lake_root(lake_root)
    catalog = catalog_path if catalog_path is not None else resolved_lake / "catalog.duckdb"
    tf_minutes = INTERVAL_SECONDS[interval] // 60
    side = Side.BUY if direction.upper() == "BUY" else Side.SELL
    condition_bits = (
        [int(tok) for tok in conditions.split(",") if tok.strip()]
        if conditions
        else list(range(52))
    )
    with Catalog.open(catalog) as cat:
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


# ────────────────────────────────────────────────────────────────────────
# Main dispatcher
# ────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """Entry point — parse args and dispatch to handler."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None or args.command == "version":
        return cmd_version()
    if args.command == "status":
        return cmd_status(run_id=args.run_id)
    if args.command == "audit":
        if args.audit_command != "verify":
            parser.error(f"unknown audit subcommand: {args.audit_command}")
        return cmd_audit_verify(audit_file=args.audit_file)
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
        )
    if args.command == "doctor":
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
        )
    parser.error(f"unknown command: {args.command}")
    return EXIT_USER_ERROR  # type: ignore[unreachable]  # parser.error exits


if __name__ == "__main__":  # pragma: no cover — invoked only by [project.scripts]
    sys.exit(main())
