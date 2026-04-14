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

    # Phase B — expiries
    expiries = ingest_sub.add_parser(
        "expiries",
        help="Phase B — enumerate historical expiries for every F&O underlying",
    )
    expiries.add_argument(
        "--start-year",
        type=int,
        default=2020,
        help="First year to enumerate (inclusive; default 2020 per FD-08)",
    )
    expiries.add_argument(
        "--end-year",
        type=int,
        default=None,
        help="Last year (inclusive; default current UTC year)",
    )
    expiries.add_argument("--run-id", default=None)
    expiries.add_argument("--dry-run", action="store_true")

    # Phase C — contracts per (underlying, expiry). Runs A→B→C in one
    # process so the in-memory collected_expiries from Phase B feeds
    # straight into Phase C without a re-read step.
    contracts = ingest_sub.add_parser(
        "contracts",
        help="Phases A → B → C — enumerate all F&O contracts per expiry",
    )
    contracts.add_argument(
        "--start-year",
        type=int,
        default=2020,
        help="First year for Phase B (inclusive; default 2020 per FD-08)",
    )
    contracts.add_argument("--end-year", type=int, default=None)
    contracts.add_argument("--run-id", default=None)
    contracts.add_argument("--dry-run", action="store_true")

    # Phase D — historical candles. Runs A→B→C→D end-to-end. The CLI
    # exposes a --limit flag so a user can sanity-check a small subset
    # of contracts before committing to the full 2020→today pull.
    candles = ingest_sub.add_parser(
        "candles",
        help="Phases A → B → C → D — pull historical candles",
    )
    candles.add_argument("--start-year", type=int, default=2020)
    candles.add_argument("--end-year", type=int, default=None)
    candles.add_argument("--run-id", default=None)
    candles.add_argument("--dry-run", action="store_true")
    candles.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max contracts to pull candles for (CLI sanity-check; "
        "default 20). Set to 0 for no limit.",
    )
    candles.add_argument(
        "--interval",
        default="1minute",
        help="Candle interval — one of the 12 Groww intervals (default 1minute)",
    )
    candles.add_argument(
        "--candle-start",
        default=None,
        help="Candle range start YYYY-MM-DD (default 2020-01-01)",
    )
    candles.add_argument(
        "--candle-end",
        default=None,
        help="Candle range end YYYY-MM-DD (default today UTC)",
    )

    # Full pipeline — alias for `ingest candles` with no limit.
    all_cmd = ingest_sub.add_parser(
        "all",
        help="Full sequential A → B → C → D pipeline (no candle subset limit)",
    )
    all_cmd.add_argument("--start-year", type=int, default=2020)
    all_cmd.add_argument("--end-year", type=int, default=None)
    all_cmd.add_argument("--run-id", default=None)
    all_cmd.add_argument("--dry-run", action="store_true")
    all_cmd.add_argument("--interval", default="1minute")
    all_cmd.add_argument("--candle-start", default=None)
    all_cmd.add_argument("--candle-end", default=None)

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
                    # environments where the tests/ tree is on sys.path.
                    try:
                        from tests.fakes.groww import (  # noqa: PLC0415
                            FakeDataFrame,
                            FakeGrowwClient,
                        )
                    except ImportError:
                        print(
                            "dry-run: tests.fakes not available — "
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


def cmd_ingest_expiries(args: argparse.Namespace) -> int:
    """Run Phase B — enumerate historical expiries for every F&O underlying."""
    import tempfile  # noqa: PLC0415
    from datetime import datetime as _dt  # noqa: PLC0415

    from brutex.adapters.groww.expiries import (  # noqa: PLC0415
        ExpiriesScopeError,
        PhaseBDriver,
    )
    from brutex.orchestrator.audit import AuditWriter  # noqa: PLC0415
    from brutex.orchestrator.pipeline import PhaseHaltError  # noqa: PLC0415
    from brutex.orchestrator.rate_limiter import AdaptiveTokenBucket  # noqa: PLC0415
    from brutex.orchestrator.store import WorkItemStore  # noqa: PLC0415

    run_id = args.run_id or str(uuid4())
    end_year = args.end_year or _dt.now(UTC).year

    print("BruteX ingest expiries")
    print(f"  run_id:       {run_id}")
    print(f"  years:        {args.start_year}..{end_year}")
    print(f"  dry_run:      {args.dry_run}")

    token = os.environ.get("GROWW_API_AUTH_TOKEN")
    if not token and not args.dry_run:
        print("⚠  GROWW_API_AUTH_TOKEN is not set. Use --dry-run for offline mode.")
        return EXIT_USER_ERROR

    with tempfile.TemporaryDirectory(prefix="brutex-dryrun-") as tmp_dir:
        tmp_root = Path(tmp_dir) if args.dry_run else _runs_dir() / run_id
        tmp_root.mkdir(parents=True, exist_ok=True)

        bucket = AdaptiveTokenBucket()
        try:
            with (
                WorkItemStore.open(tmp_root / "store.lmdb") as store,
                AuditWriter.open(tmp_root / "audit" / f"{run_id}.jsonl") as audit,
            ):
                if args.dry_run:
                    from brutex.adapters.groww.client import GrowwAdapter  # noqa: PLC0415

                    try:
                        from tests.fakes.groww import (  # noqa: PLC0415
                            FakeGrowwClient,
                        )
                    except ImportError:
                        print(
                            "dry-run requires the test fakes; run from source tree"
                        )
                        return EXIT_USER_ERROR
                    client = FakeGrowwClient()
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

                driver = PhaseBDriver(
                    adapter=adapter,
                    store=store,
                    audit=audit,
                    run_id=run_id,
                    start_year=args.start_year,
                    end_year=end_year,
                )
                try:
                    result = driver.run()
                except (PhaseHaltError, ExpiriesScopeError) as exc:
                    print(f"❌ Phase B halted: {exc}")
                    return EXIT_PIPELINE_HALT

                details = driver.last_result
                print()
                print("Phase B complete")
                print(f"  work items:        {result.total}")
                print(f"  success:           {result.success}")
                print(f"  no_data:           {result.no_data}")
                print(f"  quarantined:       {result.quarantined}")
                if details is not None:
                    print(
                        f"  underlyings:       {details.underlyings_enumerated}"
                    )
                    print(f"  years:             {details.years_enumerated}")
                    print(f"  total expiries:    {details.total_expiries}")
                print(f"  elapsed:           {result.elapsed_seconds:.2f}s")
        except Exception as exc:  # noqa: BLE001
            print(f"❌ Unexpected error: {exc}", file=sys.stderr)
            return EXIT_INTERNAL_ERROR

    return EXIT_OK


def cmd_ingest_contracts(args: argparse.Namespace) -> int:
    """Run A → B → C in a single process — stop after contracts."""
    return _run_pipeline_cli(args=args, stop_after="C")


def cmd_ingest_candles(args: argparse.Namespace) -> int:
    """Run A → B → C → D — the subset-limited candle sanity-check."""
    return _run_pipeline_cli(args=args, stop_after="D")


def cmd_ingest_all(args: argparse.Namespace) -> int:
    """Run A → B → C → D — full pipeline, no contract-count limit."""
    # Synthesise a limit=0 (unlimited) attribute for the shared helper
    # so we don't need a second argparse definition block.
    args.limit = 0
    return _run_pipeline_cli(args=args, stop_after="D")


def _run_pipeline_cli(
    *, args: argparse.Namespace, stop_after: str
) -> int:
    """Shared driver for `ingest contracts | candles | all`.

    Runs Phase A → B → C (→ D) in one process. The in-memory
    collected_expiries from Phase B feeds directly into Phase C, and
    Phase C's iter_all_contracts feeds Phase D. This matches the
    smoke-test wiring (tests/integration/test_end_to_end_smoke.py).

    Parameters
    ----------
    args
        argparse Namespace from the CLI.
    stop_after
        One of "A", "B", "C", "D" — the last phase to run.
    """
    import tempfile  # noqa: PLC0415
    from datetime import datetime as _dt  # noqa: PLC0415

    from brutex.adapters.groww.candles import (  # noqa: PLC0415
        CandleRequest,
        PhaseDDriver,
    )
    from brutex.adapters.groww.client import GrowwAdapter  # noqa: PLC0415
    from brutex.adapters.groww.contracts import (  # noqa: PLC0415
        ContractsScopeError,
        PhaseCDriver,
    )
    from brutex.adapters.groww.expiries import (  # noqa: PLC0415
        ExpiriesScopeError,
        PhaseBDriver,
    )
    from brutex.adapters.groww.instruments import (  # noqa: PLC0415
        InstrumentsScopeError,
        PhaseADriver,
    )
    from brutex.orchestrator.audit import AuditWriter  # noqa: PLC0415
    from brutex.orchestrator.pipeline import PhaseHaltError  # noqa: PLC0415
    from brutex.orchestrator.rate_limiter import (  # noqa: PLC0415
        AdaptiveTokenBucket,
    )
    from brutex.orchestrator.store import WorkItemStore  # noqa: PLC0415
    from brutex.universe import V1_INDICES  # noqa: PLC0415

    run_id = args.run_id or str(uuid4())
    end_year = args.end_year or _dt.now(UTC).year
    snapshot_date = _dt.now(UTC).date()

    print(f"BruteX ingest (stop_after={stop_after})")
    print(f"  run_id:       {run_id}")
    print(f"  years:        {args.start_year}..{end_year}")
    print(f"  dry_run:      {args.dry_run}")
    if stop_after == "D":
        print(f"  interval:     {args.interval}")
        limit = getattr(args, "limit", 0) or 0
        print(f"  contract cap: {'none' if limit == 0 else limit}")

    token = os.environ.get("GROWW_API_AUTH_TOKEN")
    if not token and not args.dry_run:
        print("⚠  GROWW_API_AUTH_TOKEN is not set. Use --dry-run for offline mode.")
        return EXIT_USER_ERROR

    with tempfile.TemporaryDirectory(prefix="brutex-pipeline-") as tmp_dir:
        tmp_root = Path(tmp_dir) if args.dry_run else _runs_dir() / run_id
        tmp_root.mkdir(parents=True, exist_ok=True)
        bucket = AdaptiveTokenBucket()

        try:
            with (
                WorkItemStore.open(tmp_root / "store.lmdb") as store,
                AuditWriter.open(
                    tmp_root / "audit" / f"{run_id}.jsonl"
                ) as audit,
            ):
                adapter = _build_adapter(
                    dry_run=args.dry_run,
                    token=token,
                    bucket=bucket,
                    store=store,
                    audit=audit,
                    run_id=run_id,
                )
                if adapter is None:
                    return EXIT_USER_ERROR

                # ── Phase A ─────────────────────────────────────────
                phase_a = PhaseADriver(
                    adapter=adapter,
                    store=store,
                    audit=audit,
                    run_id=run_id,
                    snapshot_date=snapshot_date,
                )
                try:
                    a_result = phase_a.run()
                except (PhaseHaltError, InstrumentsScopeError) as exc:
                    print(f"❌ Phase A halted: {exc}")
                    return EXIT_PIPELINE_HALT
                print(
                    f"✓ Phase A: {a_result.success}/{a_result.total} "
                    f"({a_result.elapsed_seconds:.1f}s)"
                )
                if stop_after == "A":
                    return EXIT_OK

                # ── Phase B ─────────────────────────────────────────
                phase_b = PhaseBDriver(
                    adapter=adapter,
                    store=store,
                    audit=audit,
                    run_id=run_id,
                    underlyings=V1_INDICES,
                    start_year=args.start_year,
                    end_year=end_year,
                )
                try:
                    b_result = phase_b.run()
                except (PhaseHaltError, ExpiriesScopeError) as exc:
                    print(f"❌ Phase B halted: {exc}")
                    return EXIT_PIPELINE_HALT
                print(
                    f"✓ Phase B: {b_result.success}/{b_result.total} "
                    f"({b_result.elapsed_seconds:.1f}s)"
                )
                if stop_after == "B":
                    return EXIT_OK

                # ── Phase C ─────────────────────────────────────────
                phase_c = PhaseCDriver(
                    adapter=adapter,
                    store=store,
                    audit=audit,
                    run_id=run_id,
                    expiries=phase_b.collected_expiries,
                )
                try:
                    c_result = phase_c.run()
                except (PhaseHaltError, ContractsScopeError) as exc:
                    print(f"❌ Phase C halted: {exc}")
                    return EXIT_PIPELINE_HALT
                print(
                    f"✓ Phase C: {c_result.success}/{c_result.total} "
                    f"({c_result.elapsed_seconds:.1f}s)"
                )
                if phase_c.last_result is not None:
                    print(
                        f"  total contracts: "
                        f"{phase_c.last_result.total_contracts}"
                    )
                if stop_after == "C":
                    return EXIT_OK

                # ── Phase D ─────────────────────────────────────────
                all_contracts = list(phase_c.iter_all_contracts())
                limit = getattr(args, "limit", 0) or 0
                if limit and limit > 0:
                    all_contracts = all_contracts[:limit]

                if not all_contracts:
                    print("⚠  Phase D: no contracts to fetch, skipping")
                    return EXIT_OK

                candle_start = (
                    date.fromisoformat(args.candle_start)
                    if args.candle_start
                    else date(args.start_year, 1, 1)
                )
                candle_end = (
                    date.fromisoformat(args.candle_end)
                    if args.candle_end
                    else _dt.now(UTC).date()
                )
                requests = [
                    CandleRequest(
                        exchange=exchange,
                        segment="FNO",
                        groww_symbol=symbol,
                        interval=args.interval,
                        start=candle_start,
                        end=candle_end,
                    )
                    for exchange, _underlying, _expiry, symbol in all_contracts
                ]
                phase_d = PhaseDDriver(
                    adapter=adapter,
                    store=store,
                    audit=audit,
                    run_id=run_id,
                    requests=requests,
                )
                try:
                    d_result = phase_d.run()
                except PhaseHaltError as exc:
                    print(f"❌ Phase D halted: {exc}")
                    return EXIT_PIPELINE_HALT
                print(
                    f"✓ Phase D: {d_result.success}/{d_result.total} "
                    f"({d_result.elapsed_seconds:.1f}s)"
                )
                if phase_d.last_result is not None:
                    print(
                        f"  total candle rows: "
                        f"{phase_d.last_result.total_candle_rows}"
                    )
        except Exception as exc:  # noqa: BLE001
            print(f"❌ Unexpected error: {exc}", file=sys.stderr)
            return EXIT_INTERNAL_ERROR

    return EXIT_OK


def _build_adapter(
    *,
    dry_run: bool,
    token: str | None,
    bucket: object,
    store: object,
    audit: object,
    run_id: str,
) -> object | None:
    """Construct a GrowwAdapter (real or fake-backed).

    Returns None if the caller must exit with EXIT_USER_ERROR (e.g.
    dry-run mode but tests/fakes is not importable).
    """
    from brutex.adapters.groww.client import GrowwAdapter  # noqa: PLC0415

    if dry_run:
        try:
            from tests.fakes.groww import (  # noqa: PLC0415
                FakeDataFrame,
                FakeGrowwClient,
            )
        except ImportError:
            print(
                "dry-run requires tests/fakes — run from source tree",
                file=sys.stderr,
            )
            return None
        client = FakeGrowwClient()
        # Give Phase A at least one in-scope instrument row so the
        # pipeline can continue past the empty-CSV fast path.
        client.instruments_response = FakeDataFrame(
            [
                {
                    "exchange": "NSE",
                    "exchange_token": 26000,
                    "trading_symbol": "NIFTY",
                    "groww_symbol": "NSE-NIFTY",
                    "name": "Nifty 50",
                    "instrument_type": "IDX",
                    "segment": "CASH",
                    "series": "",
                    "isin": "",
                    "underlying_symbol": "",
                    "underlying_exchange_token": float("nan"),
                    "lot_size": 1,
                    "expiry_date": "",
                    "strike_price": float("nan"),
                    "tick_size": 0.05,
                    "freeze_quantity": float("nan"),
                    "is_reserved": 0,
                    "buy_allowed": 1,
                    "sell_allowed": 1,
                    "internal_trading_symbol": "NIFTY",
                }
            ]
        )
        return GrowwAdapter(
            client=client,
            bucket=bucket,  # type: ignore[arg-type]
            store=store,  # type: ignore[arg-type]
            audit=audit,  # type: ignore[arg-type]
            run_id=run_id,
        )

    return GrowwAdapter.from_token(
        token=token or "",
        bucket=bucket,  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        audit=audit,  # type: ignore[arg-type]
        run_id=run_id,
    )


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
        if args.ingest_command == "expiries":
            return cmd_ingest_expiries(args)
        if args.ingest_command == "contracts":
            return cmd_ingest_contracts(args)
        if args.ingest_command == "candles":
            return cmd_ingest_candles(args)
        if args.ingest_command == "all":
            return cmd_ingest_all(args)
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
