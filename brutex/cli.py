"""BruteX CLI entry point — ``brutex <command>``.

Registered in ``pyproject.toml`` as:

    [project.scripts]
    brutex = "brutex.cli:main"

After ``pip install -e .``, the ``brutex`` command is on PATH.

## Commands (v1.0)

    brutex version
        Print the BruteX version + commit SHA.

    brutex status [--run-id <id>]
        Read ~/.brutex/runtime.json and print the current run's
        status. Returns quickly (< 100ms per FR-UI-002).

    brutex ingest instruments [--snapshot-date YYYY-MM-DD] [--run-id <id>]
        Run Phase A — pull the Groww instrument CSV, normalise, filter
        to the v1 universe, persist.

    brutex audit verify <audit-file.jsonl>
        Replay the audit chain and assert integrity (NFR-OBS-003).

## Commands (v1.1+, placeholders)

    brutex ingest expiries       — Phase B runner
    brutex ingest contracts      — Phase C runner
    brutex ingest candles        — Phase D runner
    brutex ingest all            — full A→B→C→D sequential pipeline
    brutex kill <run-id>         — graceful pipeline shutdown
    brutex notify test           — send a Telegram test message

These raise :class:`NotImplementedError` with a pointer to the
relevant REQUIREMENTS.md FR for now.

## Design

* **argparse**, no external dep beyond stdlib
* Subcommands dispatch to plain functions — each subcommand is a
  2-10 line shim that wires the CLI args into the right classes
* Output goes to stdout via ``print`` in the CLI layer only (NOT
  in library code, where ``structlog`` is mandatory per CG-04)
* Exit codes: 0 success, 1 user error, 2 internal error,
  3 pipeline halt (phase blocked on quarantine)
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Sequence
from uuid import uuid4

from brutex import __version__
from brutex.config.ports import detect_environment, detect_lan_ip, load_port_config
from brutex.config.runtime import (
    build_runtime_info,
    clear_stale_runtime,
    read_runtime_file,
    runtime_file_path,
)
from brutex.orchestrator.audit import AuditChainCorruption, verify_chain

# ────────────────────────────────────────────────────────────────────────
# Exit codes
# ────────────────────────────────────────────────────────────────────────

EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_INTERNAL_ERROR = 2
EXIT_PIPELINE_HALT = 3


# ────────────────────────────────────────────────────────────────────────
# Top-level argparse builder
# ────────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Construct the full argparse tree."""
    parser = argparse.ArgumentParser(
        prog="brutex",
        description=(
            "BruteX — extreme drill-down brute-force backtesting engine "
            f"(v{__version__}). See docs/REQUIREMENTS.md for the full spec."
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
    sub.add_parser("version", help="Print BruteX version + commit SHA")

    # ── status ────────────────────────────────────────────────────
    status = sub.add_parser(
        "status",
        help="Show the running ingest run status (from ~/.brutex/runtime.json)",
    )
    status.add_argument(
        "--run-id",
        help="Override the run_id to look for (default: read from runtime file)",
    )

    # ── ingest ────────────────────────────────────────────────────
    ingest = sub.add_parser("ingest", help="Run an ingest phase")
    ingest_sub = ingest.add_subparsers(dest="ingest_command", required=True)

    instruments = ingest_sub.add_parser(
        "instruments",
        help="Phase A — pull + normalise + filter Groww instruments",
    )
    instruments.add_argument(
        "--snapshot-date",
        default=None,
        help="Snapshot date in YYYY-MM-DD (default: today UTC)",
    )
    instruments.add_argument(
        "--run-id",
        default=None,
        help="Run UUID (default: generate a fresh uuid4)",
    )
    instruments.add_argument(
        "--dry-run",
        action="store_true",
        help="Build everything but do not persist (useful for testing)",
    )

    # Placeholder subcommands (future phases)
    for phase_name in ("expiries", "contracts", "candles", "all"):
        p = ingest_sub.add_parser(
            phase_name,
            help=f"Phase {phase_name} (NOT YET IMPLEMENTED)",
        )
        p.set_defaults(_is_placeholder=True, _placeholder_name=phase_name)

    # ── audit verify ──────────────────────────────────────────────
    audit = sub.add_parser("audit", help="Audit log tools")
    audit_sub = audit.add_subparsers(dest="audit_command", required=True)
    audit_verify = audit_sub.add_parser(
        "verify",
        help="Replay a hash-chained audit log and assert integrity",
    )
    audit_verify.add_argument("audit_file", type=Path, help="Path to the .jsonl audit log")

    # ── kill (placeholder) ────────────────────────────────────────
    kill = sub.add_parser("kill", help="Graceful run shutdown (NOT YET IMPLEMENTED)")
    kill.add_argument("run_id")
    kill.set_defaults(_is_placeholder=True, _placeholder_name="kill")

    # ── notify (placeholder) ──────────────────────────────────────
    notify = sub.add_parser("notify", help="Telegram notification tools")
    notify_sub = notify.add_subparsers(dest="notify_command", required=True)
    notify_test = notify_sub.add_parser(
        "test", help="Send a Telegram test message (NOT YET IMPLEMENTED)"
    )
    notify_test.set_defaults(_is_placeholder=True, _placeholder_name="notify test")

    return parser


# ────────────────────────────────────────────────────────────────────────
# Subcommand handlers
# ────────────────────────────────────────────────────────────────────────


def cmd_version(args: argparse.Namespace) -> int:
    print(f"brutex {__version__}")
    commit = _current_commit_sha()
    if commit:
        print(f"commit: {commit}")
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    """Read ~/.brutex/runtime.json and print a compact summary.

    Auto-cleans stale runtime files (dead PIDs) per FR-UI-007.
    """
    clear_stale_runtime()
    info = read_runtime_file()
    if info is None:
        print("no running BruteX instance")
        return EXIT_OK

    if args.run_id and info.run_id != args.run_id:
        print(
            f"no running BruteX instance with run_id={args.run_id} "
            f"(found run_id={info.run_id})"
        )
        return EXIT_USER_ERROR

    print(f"Run ID:    {info.run_id}")
    print(f"PID:       {info.pid}")
    print(f"Started:   {info.started_at}")
    print(f"Phase:     {info.phase}")
    print(f"Host bind: {info.host_bind}")
    print(f"Dashboard: {info.dashboard_urls[0] if info.dashboard_urls else '—'}")
    if len(info.dashboard_urls) > 1:
        for url in info.dashboard_urls[1:]:
            print(f"           {url}")
    print(f"Metrics:   {info.metrics_url}")
    print(f"Env:       {info.environment}")
    return EXIT_OK


def cmd_ingest_instruments(args: argparse.Namespace) -> int:
    """Run Phase A — pull + normalise + filter Groww instruments.

    Scaffold implementation — wires all the pieces together but
    does NOT attempt a real Groww call unless ``GROWW_API_AUTH_TOKEN``
    is set. In dry-run mode, builds all the plumbing and prints
    what would have happened.
    """
    import tempfile  # noqa: PLC0415

    from brutex.adapters.groww.instruments import (  # noqa: PLC0415
        InstrumentsScopeError,
        PhaseADriver,
    )
    from brutex.orchestrator.audit import AuditWriter  # noqa: PLC0415
    from brutex.orchestrator.pipeline import PhaseHaltError  # noqa: PLC0415
    from brutex.orchestrator.rate_limiter import AdaptiveTokenBucket  # noqa: PLC0415
    from brutex.orchestrator.store import WorkItemStore  # noqa: PLC0415

    run_id = args.run_id or str(uuid4())
    snapshot_date = (
        date.fromisoformat(args.snapshot_date)
        if args.snapshot_date
        else datetime.now(UTC).date()
    )

    print(f"BruteX ingest instruments")
    print(f"  run_id:       {run_id}")
    print(f"  snapshot:     {snapshot_date}")
    print(f"  dry_run:      {args.dry_run}")

    token = os.environ.get("GROWW_API_AUTH_TOKEN")
    if not token:
        print()
        print("⚠  GROWW_API_AUTH_TOKEN is not set.")
        print("   Cannot make real SDK calls without a token.")
        print("   Set via: export GROWW_API_AUTH_TOKEN=<your-token>")
        if not args.dry_run:
            print()
            print("Use --dry-run to exercise the wiring without a real call.")
            return EXIT_USER_ERROR
        # In dry-run mode, fall through and use a dummy token + fake client.

    # Build the plumbing
    with tempfile.TemporaryDirectory(prefix="brutex-dryrun-") as tmp_dir:
        tmp_root = Path(tmp_dir) if args.dry_run else _runs_dir() / run_id
        tmp_root.mkdir(parents=True, exist_ok=True)

        store_path = tmp_root / "store.lmdb"
        audit_path = tmp_root / "audit" / f"{run_id}.jsonl"

        bucket = AdaptiveTokenBucket()

        try:
            with (
                WorkItemStore.open(store_path) as store,
                AuditWriter.open(audit_path) as audit,
            ):
                if args.dry_run:
                    # Use a pre-built FakeGrowwClient to avoid any network
                    # In normal CLI runs (no dry-run) we would call
                    # GrowwAdapter.from_token here.
                    print()
                    print("dry-run: using fake Groww client (no network)")
                    from brutex.adapters.groww.client import GrowwAdapter  # noqa: PLC0415

                    # Import test fake via duck typing — only works in dev
                    # environments where tests are importable.
                    try:
                        from tests.unit.test_adapters_groww_client import (  # noqa: PLC0415
                            FakeDataFrame,
                            FakeGrowwClient,
                        )
                    except ImportError:
                        print(
                            "dry-run: tests.unit not available — "
                            "cannot construct FakeGrowwClient. "
                            "Run from the source tree with tests installed."
                        )
                        return EXIT_USER_ERROR
                    client = FakeGrowwClient()
                    client.instruments_response = FakeDataFrame([])
                    adapter = GrowwAdapter(
                        client=client,
                        bucket=bucket,
                        store=store,
                        audit=audit,
                        run_id=run_id,
                    )
                else:
                    from brutex.adapters.groww.client import GrowwAdapter  # noqa: PLC0415

                    adapter = GrowwAdapter.from_token(
                        token=token or "",
                        bucket=bucket,
                        store=store,
                        audit=audit,
                        run_id=run_id,
                    )

                driver = PhaseADriver(
                    adapter=adapter,
                    store=store,
                    audit=audit,
                    run_id=run_id,
                    snapshot_date=snapshot_date,
                )

                try:
                    result = driver.run()
                except PhaseHaltError as exc:
                    print()
                    print(f"❌ Phase A HALTED: {exc}")
                    return EXIT_PIPELINE_HALT
                except InstrumentsScopeError as exc:
                    print()
                    print(f"❌ Phase A SCOPE ERROR: {exc}")
                    return EXIT_PIPELINE_HALT

                details = driver.last_result
                print()
                print("Phase A complete")
                print(f"  total rows:        {result.total}")
                print(f"  success:           {result.success}")
                print(f"  no_data:           {result.no_data}")
                print(f"  quarantined:       {result.quarantined}")
                if details is not None:
                    print(f"  raw CSV rows:      {details.total_raw_rows}")
                    print(f"  integrity fails:   {details.integrity_failures}")
                    print(f"  in-scope:          {details.in_scope_count}")
                    print(f"    indices:         {details.index_count}")
                    print(f"    futures:         {details.futures_count}")
                    print(f"    options:         {details.options_count}")
                    print(f"  out-of-scope:      {details.out_of_scope_count}")
                    print(f"  NSE / BSE:         {details.nse_count} / {details.bse_count}")
                print(f"  elapsed:           {result.elapsed_seconds:.2f}s")
        except Exception as exc:  # noqa: BLE001
            print(f"❌ Unexpected error: {exc}", file=sys.stderr)
            return EXIT_INTERNAL_ERROR

    return EXIT_OK


def cmd_audit_verify(args: argparse.Namespace) -> int:
    audit_file: Path = args.audit_file
    try:
        entry_count = verify_chain(audit_file)
    except AuditChainCorruption as exc:
        print(f"❌ Audit chain CORRUPTED at seq={exc.seq}: {exc.reason}", file=sys.stderr)
        print(f"   expected: {exc.expected}", file=sys.stderr)
        print(f"   actual:   {exc.actual}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR
    except Exception as exc:  # noqa: BLE001
        print(f"❌ {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    print(f"✅ Audit chain intact: {entry_count} entries verified")
    return EXIT_OK


def cmd_placeholder(args: argparse.Namespace) -> int:
    name = getattr(args, "_placeholder_name", "this command")
    print(f"⏸  {name} is not yet implemented.", file=sys.stderr)
    print(
        "   See docs/REQUIREMENTS.md §4 for the FR-### this maps to, "
        "and the phase plan in §20.",
        file=sys.stderr,
    )
    return EXIT_USER_ERROR


# ────────────────────────────────────────────────────────────────────────
# Dispatcher
# ────────────────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Placeholders first — set by any subcommand marked _is_placeholder
    if getattr(args, "_is_placeholder", False):
        return cmd_placeholder(args)

    command = args.command
    if command is None:
        parser.print_help()
        return EXIT_USER_ERROR

    dispatcher: dict[str, callable] = {  # type: ignore[type-arg]
        "version": cmd_version,
        "status": cmd_status,
    }

    # Sub-subcommands
    if command == "ingest":
        if args.ingest_command == "instruments":
            return cmd_ingest_instruments(args)
        return cmd_placeholder(args)

    if command == "audit":
        if args.audit_command == "verify":
            return cmd_audit_verify(args)
        return cmd_placeholder(args)

    handler = dispatcher.get(command)
    if handler is None:
        parser.print_help()
        return EXIT_USER_ERROR
    return handler(args)


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


def _current_commit_sha() -> str | None:
    """Read HEAD's SHA for the `brutex version` subcommand.

    Returns None if not in a git checkout.
    """
    try:
        # Read .git/HEAD → either "ref: refs/heads/<branch>" or a raw SHA
        head_file = Path(__file__).resolve().parent.parent / ".git" / "HEAD"
        if not head_file.exists():
            return None
        head_content = head_file.read_text().strip()
        if head_content.startswith("ref: "):
            ref_path = head_file.parent / head_content[5:]
            if ref_path.exists():
                return ref_path.read_text().strip()[:12]
            return None
        return head_content[:12]
    except OSError:
        return None


def _runs_dir() -> Path:
    """Default directory for run-scoped data (store + audit)."""
    base = os.environ.get("BRUTEX_RUNS_DIR") or str(Path.home() / ".brutex" / "runs")
    return Path(base)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
