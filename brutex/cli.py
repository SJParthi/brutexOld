"""BruteX CLI entry point — ``brutex <command>``.

Registered in ``pyproject.toml`` as:

    [project.scripts]
    brutex = "brutex.cli:main"

After ``pip install -e .``, the ``brutex`` command is on PATH.

## Status

This CLI was rewritten on 2026-05-15 and re-baselined on 2026-05-26
(DEC-VENDOR-PIVOT-001) when the prior broker adapter was wiped in favour of
Groww. Legacy ingest subcommands were vendor-specific and are dropped.
Track 1 will reintroduce ``brutex track1 ingest`` once the Groww
adapter lands in Phase 1+ of the post-pivot phase plan (see
``docs/TRACK1_GROWW_ADAPTER.md``).

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
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from brutex import __version__
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
        help="Manage the stored Groww credentials (set once, auto-load every session)",
    )
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    auth_sub.add_parser(
        "set",
        help="Interactively prompt (no echo) for the Groww API key + TOTP secret and save them",
    )
    auth_sub.add_parser(
        "status",
        help="Print whether Groww credentials are stored (booleans only; never values)",
    )
    auth_sub.add_parser(
        "clear",
        help="Delete the stored Groww credentials file",
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
        help="Candle interval, or 'all' for every Groww native timeframe (default: all)",
    )
    fetch.add_argument(
        "--window-days",
        type=int,
        default=None,
        help="Override the chunk window (default: by interval)",
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
            "ONLY load the keyless public instrument master (no Groww keys), "
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
    """Interactively prompt (no echo) for the Groww creds and save them.

    Reads the API key and TOTP secret via :func:`getpass.getpass` so neither
    is echoed to the terminal or recorded in shell history. The values are
    NEVER accepted as argv flags (they would leak via ``ps`` / shell
    history). Prints only the destination path + mode — never a value.
    """
    import getpass

    from brutex.config.credentials import (
        CredentialsError,
        GrowwCredentials,
        save_groww_credentials,
    )

    api_key = getpass.getpass("Groww API key (input hidden): ")
    totp_secret = getpass.getpass("Groww TOTP secret (input hidden): ")
    try:
        path = save_groww_credentials(GrowwCredentials(api_key=api_key, totp_secret=totp_secret))
    except CredentialsError as exc:
        print(f"[brutex] could not save credentials: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    print(f"[brutex] credentials saved to {path} (mode 0600)")
    return EXIT_OK


def cmd_auth_status() -> int:  # pragma: no cover — thin print wrapper
    """Print whether Groww credentials are stored (booleans only, no values)."""
    from brutex.config.credentials import credentials_path, credentials_present

    present = credentials_present()
    print(f"[brutex] credentials: {'set' if present else 'not set'}")
    print(f"[brutex] path: {credentials_path()}")
    return EXIT_OK


def cmd_auth_clear() -> int:  # pragma: no cover — thin print wrapper
    """Delete the stored Groww credentials file (if any)."""
    from brutex.config.credentials import clear_groww_credentials, credentials_path

    removed = clear_groww_credentials()
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
    from brutex.observability.logging import configure_logging

    log_path = configure_logging(json_renderer=True, enable_file=True)

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

            summary = universe_summary(store.load_snapshot(in_scope_only=False))
            # Auto-heal: reload when the lake is empty, missing indices (the
            # pre-DEC-INDEX-PARSE-FIX-001 fingerprint), OR still holds FUT/CE/PE
            # legs (the pre-lean fingerprint — DEC-LEAN-REINGEST-FIX-001). The
            # lean refresh does a full-snapshot replace, so a reload purges the
            # stale ~101k-row dump down to the ~236 universe underlyings.
            if should_reload_instrument_master(
                index_count=summary["index_count"],
                stock_count=summary["stock_count"],
                legs_present=store.has_derivative_legs(),
            ):
                counts = load_instrument_master(store)
                print(
                    f"[brutex] auto-loaded instrument master → "
                    f"{counts['index_count']} indices · {counts['stock_count']} stocks",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[brutex] instrument master present → "
                    f"{summary['index_count']} indices · {summary['stock_count']} stocks",
                    file=sys.stderr,
                )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            print(
                f"[brutex] instrument auto-load skipped ({exc}); "
                f"use the Load instruments button once online",
                file=sys.stderr,
            )

        # Auto-pull expiries + contracts for the full universe once credentials
        # are present (DEC-AUTO-UNIVERSE-001). The keyless instrument master
        # loads above; expiries/contracts need Groww auth (CLAUDE.md §6), so
        # this is gated on credentials_present() AND a non-empty instrument
        # master — the pure decision lives in should_auto_pull_universe(). Runs
        # in a BACKGROUND daemon thread so a long live pull (≈250 underlyings)
        # never blocks the dashboard from serving; candles remain a manual
        # pull. The thread REUSES the dashboard's already-open store (single
        # DuckDB writer — no second read-write handle). Best-effort: never
        # crashes the server.
        try:  # pragma: no cover — live network + server + thread path
            from brutex.config.credentials import credentials_present
            from brutex.dashboard.auto_pull import should_auto_pull_universe
            from brutex.dashboard.universe_query import universe_summary

            gate_summary = universe_summary(store.load_snapshot(in_scope_only=False))
            if should_auto_pull_universe(
                credentials_present=credentials_present(),
                instrument_total=gate_summary["total"],
            ):
                import threading
                from datetime import UTC, datetime

                def _auto_universe_pull() -> None:
                    try:
                        year = datetime.now(UTC).year
                        report = _run_live_universe_ingest(lake_root, years=[year], store=store)
                        print(
                            f"[brutex] auto-pulled universe → {report.underlyings} "
                            f"underlyings · {report.expiries} expiries · "
                            f"{report.contracts} contracts",
                            file=sys.stderr,
                        )
                    except Exception as exc:  # pylint: disable=broad-exception-caught
                        print(
                            f"[brutex] universe auto-pull skipped ({exc})",
                            file=sys.stderr,
                        )

                threading.Thread(
                    target=_auto_universe_pull,
                    name="brutex-universe-pull",
                    daemon=True,
                ).start()
                print(
                    "[brutex] credentials present → auto-pulling expiries/contracts in background…",
                    file=sys.stderr,
                )
            else:
                print(
                    "[brutex] no Groww credentials yet → skipping expiries/contracts "
                    "auto-pull (set via `brutex auth set` or the Credentials page; the "
                    "keyless instrument list is already loaded)",
                    file=sys.stderr,
                )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            print(f"[brutex] universe auto-pull setup skipped ({exc})", file=sys.stderr)
    app = build_app(state, instrument_store=store)
    print(f"[brutex] dashboard → http://{host}:{port}  (Ctrl-C to stop)")
    print(f"[brutex] logs → {log_path}  ·  live viewer: http://{host}:{port}/logs")
    uvicorn.run(app, host=host, port=port)
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
    resolved_lake = lake_root
    if resolved_lake is None:
        env_lake = os.environ.get("BRUTEX_LAKE_ROOT")
        resolved_lake = Path(env_lake) if env_lake else Path.home() / ".brutex" / "lake"

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
    """``--lake-root`` → ``$BRUTEX_LAKE_ROOT`` → ``~/.brutex/lake``."""
    if lake_root is not None:
        return lake_root
    env_lake = os.environ.get("BRUTEX_LAKE_ROOT")
    return Path(env_lake) if env_lake else Path.home() / ".brutex" / "lake"


def _build_live_fetcher(sink: Any) -> tuple[Any, Any]:  # pragma: no cover — live Groww creds + SDK
    """Wire the real auth + candle fetcher → (PersistingFetcher, reauth hook)."""
    from growwapi.groww.exceptions import BaseGrowwException

    from brutex.adapters.groww.auth import GrowwAuth
    from brutex.adapters.groww.client import GrowwCandleFetcher
    from brutex.adapters.groww.persist import PersistingFetcher
    from brutex.orchestrator.rate_limiter import AdaptiveTokenBucket

    auth = GrowwAuth.from_env()
    groww = GrowwCandleFetcher(
        sdk=auth.session(),
        bucket=AdaptiveTokenBucket(),
        base_exception=BaseGrowwException,
        reauth_fn=auth.refresh,
    )
    return PersistingFetcher(inner=groww, sink=sink), groww.reauth


def _run_dry_run_universe_ingest(  # pragma: no cover — needs DuckDB store + fno_math kernel
    lake_root: Path, *, years: list[int]
) -> Any:
    """Run the universe ingest offline with synthetic sources (``--universe --dry-run``).

    Builds the real :class:`~brutex.data.instrument_store.InstrumentStore` (the
    lake sink) but wires synthetic instrument-CSV / expiry / contract sources so
    the whole instruments → expiries → contracts orchestration runs with NO
    network and NO Groww creds — the dry-run proof of the LAKE pipe (DEC-P0-063).

    Marked ``no cover``: the DuckDB store transitively imports the ``fno_math``
    Rust kernel, so this path runs only on a fully-built environment (``make
    setup``). The pure orchestration core (:func:`run_universe_ingest`) is
    unit-tested with fakes in ``tests/unit/test_groww_universe_ingest.py``.
    """
    from dataclasses import dataclass, field
    from datetime import date as date_t
    from decimal import Decimal

    from brutex.adapters.groww.contracts import ContractFetchResult
    from brutex.adapters.groww.expiries import ExpiryFetchResult
    from brutex.adapters.groww.instruments import InstrumentMasterFetcher
    from brutex.adapters.groww.universe_ingest import (
        SYNTHETIC_INSTRUMENT_CSV,
        run_universe_ingest,
    )
    from brutex.data.instrument_store import (
        ContractRecord,
        ExpiryRecord,
        InstrumentStore,
    )
    from brutex.models import InstrumentType

    @dataclass(slots=True)
    class _SyntheticExpirySource:
        """Emits + upserts one synthetic expiry per (underlying, year)."""

        sink: Any
        expiry: date_t = field(default_factory=lambda: date_t(2025, 1, 30))

        def fetch(self, underlying: str, exchange: str, *, year: int) -> ExpiryFetchResult:
            """Return + upsert a single synthetic expiry record."""
            record = ExpiryRecord(
                exchange=exchange,
                underlying_symbol=underlying,
                expiry_date=self.expiry,
                snapshot_date=self.expiry,
                pulled_year=year,
                fetched_at=datetime.now(UTC),
                run_id="synthetic-expiries",
            )
            self.sink.upsert_expiries([record])
            return ExpiryFetchResult(records=[record], skipped_dates=0)

    @dataclass(slots=True)
    class _SyntheticContractSource:
        """Emits + upserts one synthetic CE contract per expiry."""

        sink: Any

        def fetch(self, underlying: str, exchange: str, expiry_date: date_t) -> ContractFetchResult:
            """Return + upsert a single synthetic CE contract record."""
            record = ContractRecord(
                exchange=exchange,
                underlying_symbol=underlying,
                expiry_date=expiry_date,
                contract_symbol=f"{exchange}-{underlying}-{expiry_date:%d%b%y}-24650-CE",
                snapshot_date=expiry_date,
                instrument_type=InstrumentType.CE,
                strike_price=Decimal("24650"),
                lot_size=75,
                freeze_quantity=1800,
                tick_size=Decimal("0.05"),
                fetched_at=datetime.now(UTC),
                run_id="synthetic-contracts",
            )
            self.sink.upsert_contracts([record])
            return ContractFetchResult(records=[record], skipped_malformed=0, skipped_no_metadata=0)

    store = InstrumentStore.open(lake_root / "instruments.duckdb")
    try:
        instrument_fetcher = InstrumentMasterFetcher(
            http_get=lambda _url: SYNTHETIC_INSTRUMENT_CSV,
            store=store,
            run_id_factory=lambda: "synthetic-instruments",
        )
        return run_universe_ingest(
            instrument_fetcher,
            _SyntheticExpirySource(sink=store),
            _SyntheticContractSource(sink=store),
            [("NIFTY", "NSE")],
            years,
        )
    finally:
        store.close()


def _run_live_universe_ingest(  # pragma: no cover — live Groww creds + SDK + DuckDB
    lake_root: Path, *, years: list[int], store: Any = None, resume: bool = False
) -> Any:
    """Run the universe ingest against live Groww (the real instrument/expiry/contract SDK).

    When ``store`` is ``None`` (the default — ``fetch --universe`` path) this
    opens its own :class:`~brutex.data.instrument_store.InstrumentStore` on
    ``lake_root/"instruments.duckdb"`` and closes it in ``finally``. When an
    already-open ``store`` is injected (the dashboard background-pull path,
    DEC-AUTO-UNIVERSE-001) it is REUSED and NOT closed — this shares the single
    DuckDB writer the dashboard already holds, so the on-run auto-pull never
    opens a second read-write handle to the same file.

    **Lake scope (DEC-COVERAGE-LEDGER-001 Part A):** the per-underlying
    expiries/contracts fan-out now covers ALL lake underlyings — every index
    PLUS every F&O-eligible single stock (``list_index_equity_instruments`` →
    :func:`brutex.universe.derive_lake_underlyings`) per DEC-P0-063 — not just
    the ~25 indices. The brute-force ENGINE is unchanged (still 3 indices).

    **Completeness ledger + resume (Parts C/D):** each ``(underlying, year)``
    pull is wrapped so one underlying's failure records a ``failed`` coverage
    cell and continues instead of aborting the rest; on success a
    ``complete`` / ``pending`` cell (status from
    :func:`brutex.data.coverage_ledger.classify_cell`) is recorded. When
    ``resume`` is ``True`` only the incomplete cells
    (:func:`brutex.data.coverage_ledger.cells_to_repull` over the reconciled
    ledger) are re-pulled — a recoverable, idempotent re-run.
    """
    from datetime import UTC, datetime

    from growwapi.groww.exceptions import BaseGrowwException

    from brutex.adapters.groww.auth import GrowwAuth
    from brutex.adapters.groww.contracts import ContractFetcher, ContractMeta
    from brutex.adapters.groww.expiries import ExpiryFetcher
    from brutex.adapters.groww.instruments import InstrumentMasterFetcher
    from brutex.adapters.groww.universe_ingest import UniverseIngestReport
    from brutex.data.coverage_ledger import (
        CoverageRecord,
        cells_to_repull,
        classify_cell,
        reconcile_coverage,
    )
    from brutex.data.instrument_store import InstrumentStore
    from brutex.orchestrator.rate_limiter import AdaptiveTokenBucket
    from brutex.universe import derive_lake_underlyings

    owns_store = store is None
    if store is None:
        store = InstrumentStore.open(lake_root / "instruments.duckdb")
    try:
        sdk = GrowwAuth.from_env().session()
        bucket = AdaptiveTokenBucket()
        instrument_fetcher = InstrumentMasterFetcher.from_env(store)
        expiry_fetcher = ExpiryFetcher(
            sdk=sdk, bucket=bucket, base_exception=BaseGrowwException, store=store
        )

        def _meta_lookup(symbol: str) -> ContractMeta | None:
            inst = store.get(symbol)
            if inst is None:
                return None
            return ContractMeta(
                lot_size=inst.lot_size,
                tick_size=inst.tick_size,
                freeze_quantity=inst.freeze_quantity,
            )

        contract_fetcher = ContractFetcher(
            sdk=sdk,
            bucket=bucket,
            base_exception=BaseGrowwException,
            store=store,
            metadata_lookup=_meta_lookup,
        )

        # Refresh the master first (the contract fetcher's metadata enrichment
        # depends on a fresh master), then derive the FULL lake underlying set.
        inst_result = instrument_fetcher.refresh()
        underlyings = derive_lake_underlyings(store.list_index_equity_instruments())

        # Resume: re-pull only the incomplete cells per the persisted ledger.
        run_id = datetime.now(UTC).strftime("universe-%Y%m%dT%H%M%SZ")
        if resume:
            summary = reconcile_coverage(underlyings, years, store.list_coverage())
            todo = list(cells_to_repull(summary))
        else:
            todo = [(ex, sym, yr) for sym, ex in underlyings for yr in years]

        underlying_count = len({(sym, ex) for ex, sym, _ in todo})
        expiry_total = 0
        contract_total = 0
        for exchange, underlying, year in todo:
            fetched_at = datetime.now(UTC)
            try:
                expiry_result = expiry_fetcher.fetch(underlying, exchange, year=year)
                cell_expiries = len(expiry_result.records)
                cell_contracts = 0
                for record in expiry_result.records:
                    contract_result = contract_fetcher.fetch(
                        underlying, exchange, record.expiry_date
                    )
                    cell_contracts += len(contract_result.records)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                # One underlying's failure must not abort the rest — record a
                # ``failed`` cell with the error and continue (recoverable).
                store.upsert_coverage(
                    CoverageRecord(
                        exchange=exchange,
                        underlying_symbol=underlying,
                        year=year,
                        expiries_pulled=0,
                        contracts_pulled=0,
                        status=classify_cell(0, 0, str(exc)),
                        last_run_id=run_id,
                        last_fetched_at=fetched_at,
                        error_message=str(exc),
                    )
                )
                continue
            expiry_total += cell_expiries
            contract_total += cell_contracts
            store.upsert_coverage(
                CoverageRecord(
                    exchange=exchange,
                    underlying_symbol=underlying,
                    year=year,
                    expiries_pulled=cell_expiries,
                    contracts_pulled=cell_contracts,
                    status=classify_cell(cell_expiries, cell_contracts, None),
                    last_run_id=run_id,
                    last_fetched_at=fetched_at,
                    error_message=None,
                )
            )

        return UniverseIngestReport(
            instruments=inst_result,
            underlyings=underlying_count,
            expiries=expiry_total,
            contracts=contract_total,
        )
    finally:
        if owns_store:
            store.close()


def _run_instrument_master_load(  # pragma: no cover — opens DuckDB store + live network
    lake_root: Path,
) -> dict[str, Any]:
    """Keylessly load the public Groww instrument master into the lake.

    Opens the real :class:`~brutex.data.instrument_store.InstrumentStore` and
    delegates to the testable
    :func:`brutex.dashboard.instrument_loader.load_instrument_master` with NO
    injected ``http_get`` — i.e. the real SSRF-guarded public-CSV fetch, which
    needs NO Groww API keys (DEC-INSTRUMENT-LOAD-001). Marked ``no cover``: the
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

    ``--dry-run`` uses :class:`SyntheticCandleFetcher` so the full
    parse → OHLC-guard → sink pipeline runs offline (no Groww creds).
    ``--inspect`` collects in memory (no Parquet) and prints a per-timeframe
    pre/regular/post-market + timestamp-alignment report — the operator's
    "does Groww give pre/post-market + precise timestamps?" probe.
    ``--universe`` first ingests the instrument master + expiries + contracts
    into the lake (the LAKE surface authorised by DEC-P0-063) BEFORE the index
    candle pull — the turnkey "pull everything" command.
    ``--instruments`` loads ONLY the keyless public instrument master (no Groww
    keys), prints the index/stock counts, and exits — it populates the
    ``/universe`` list without auth (DEC-INSTRUMENT-LOAD-001). Expiries /
    contracts / candles still need keys, separately.
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

    from brutex.adapters.groww.chunks import ALL_GROWW_INTERVALS
    from brutex.adapters.groww.ingest import (
        BRUTE_FORCE_INDEX_SYMBOLS,
        SyntheticCandleFetcher,
        run_ingest,
    )
    from brutex.adapters.groww.inspect import CollectingBarSink
    from brutex.adapters.groww.persist import PersistingFetcher
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink
    from brutex.data.watermark import FetchWatermark

    resolved = _resolve_lake_root(lake_root)
    intervals = list(ALL_GROWW_INTERVALS) if interval == "all" else [interval]
    start_dt = _parse_dt(start) if start else datetime(datetime.now(UTC).year, 1, 1, tzinfo=UTC)
    end_dt = _parse_dt(end) if end else datetime.now(UTC)

    if universe:
        years = list(range(start_dt.year, end_dt.year + 1))
        if dry_run:
            print("[brutex] fetch --universe --dry-run → synthetic instruments/expiries/contracts")
            uni_report = _run_dry_run_universe_ingest(resolved, years=years)
        else:
            print("[brutex] fetch --universe (live Groww) → instruments/expiries/contracts")
            uni_report = _run_live_universe_ingest(resolved, years=years)
        print(
            f"[brutex] universe done — instruments parsed={uni_report.instruments.parsed} "
            f"underlyings={uni_report.underlyings} expiries={uni_report.expiries} "
            f"contracts={uni_report.contracts}"
        )

    collecting = CollectingBarSink() if inspect else None
    sink: Any = collecting or ParquetBarSink(writer=ParquetWriter(lake_root=resolved))
    watermark = FetchWatermark()

    if dry_run:
        fetcher: object = PersistingFetcher(
            inner=SyntheticCandleFetcher(rows_per_chunk=rows_per_chunk), sink=sink
        )
        reauth = None
        print("[brutex] fetch --dry-run → synthetic candles", file=sys.stderr)
    else:
        fetcher, reauth = _build_live_fetcher(sink)
        print("[brutex] fetch (live Groww)", file=sys.stderr)

    report = run_ingest(
        fetcher,  # type: ignore[arg-type]
        BRUTE_FORCE_INDEX_SYMBOLS,
        intervals,
        start_dt,
        end_dt,
        watermark,
        window_days=window_days,
        reauth=reauth,
    )
    print(
        f"[brutex] fetch done — completed={report.completed} no_data={report.no_data} "
        f"failed={report.failed} skipped={report.skipped}"
    )
    if collecting is not None:
        print("[brutex] --- Groww data-shape probe (IST session 09:15-15:30) ---")
        for inspection in collecting.inspections():
            print(f"[brutex]   {inspection.summary()}")
    else:
        print(f"[brutex] lake: {resolved}")
    return EXIT_OK if report.failed == 0 else EXIT_INTERNAL_ERROR


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
    parser.error(f"unknown command: {args.command}")
    return EXIT_USER_ERROR  # type: ignore[unreachable]  # parser.error exits


if __name__ == "__main__":  # pragma: no cover — invoked only by [project.scripts]
    sys.exit(main())
