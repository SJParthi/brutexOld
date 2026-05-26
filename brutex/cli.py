"""BruteX CLI entry point — ``brutex <command>``.

Registered in ``pyproject.toml`` as:

    [project.scripts]
    brutex = "brutex.cli:main"

After ``pip install -e .``, the ``brutex`` command is on PATH.

## Status

This CLI was rewritten on 2026-05-15 and re-baselined on 2026-05-26
(DEC-VENDOR-PIVOT-001) when the Dhan adapter was wiped in favour of
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
        Start the BruteX dashboard FastAPI server.

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
import sys
from pathlib import Path

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
            f"[brutex] dashboard requires fastapi+uvicorn — pip install brutex[data]: {exc}",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR

    state = DashboardState()
    app = build_app(state, instrument_store=lake_root)
    uvicorn.run(app, host=host, port=port)
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
    if args.command == "dashboard":
        return cmd_dashboard(
            lake_root=args.lake_root,
            host=args.host,
            port=args.port,
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
