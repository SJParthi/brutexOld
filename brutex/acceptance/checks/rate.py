"""Check 4 — rate-limiter conformance from PERSISTED evidence only.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756). Ground truth: NO
per-call timestamped record persists — the per-second AIMD + rolling-60s
governor state is in-memory only. This check proves what disk CAN prove and
ABSTAINS loudly on what it cannot; there is deliberately NO blanket "rate
PASS". ``rate.per_call_history`` is a PERMANENT structural ABSTAIN
(``no_per_call_persistence``) — a persisted per-issue log is follow-up F3
under operator ruling (it sits adjacent to the Python-locked fetch path).
Live sampling is cross-referenced (``brutex doctor --live --stage 13``),
never imported — the package carries the structural zero-vendor-call pin.

``rate.log_window`` reads the rotating structlog JSON sink that ONLY
``cmd_dashboard`` enables. It distinguishes two cases (round 8): the sink is
ABSENT ENTIRELY (headless nightly-cron / CLI-fetch box — the dashboard never
ran) is a STANDING STRUCTURAL gap ``no_rate_log_configured`` (budget/streak-
EXEMPT, so a healthy headless lake never goes permanently amber over it);
whereas a sink that EXISTS but holds no ``rate.*`` lines is a possible present-
but-silent problem and stays the NON-structural, streak-TRACKED
``no_rate_log_lines`` abstain.
"""

from __future__ import annotations

import contextlib
import json
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from brutex.acceptance.context import IST_OFFSET, AcceptanceContext
from brutex.acceptance.model import (
    AbstainReason,
    CheckResult,
    Verdict,
    make_check,
    make_coverage,
    make_subcheck,
)

CHECK_ID = "rate"

#: The market-hours guard's landing date (DEC-GROWW-MARKET-HOURS-GUARD-001,
#: MIGRATION.md #1692) — vendor attempts BEFORE this date are excluded+counted.
MARKET_HOURS_GUARD_LANDED = date(2026, 7, 10)

#: Offending-row samples are capped at this many entries.
ROW_CAP = 20

#: The documented production ceilings (CLAUDE.md §2 Rule 6).
CEILING_RPS = 8.0
CEILING_PER_MIN = 500

#: The rolling-minute window the pair-bound evidence folds over, seconds.
ROLLING_MINUTE_S = 60.0

#: Byte cap for every rotating-log / audit tail read (the repo's 16 MiB
#: bounded-read precedent, DEC-LAKE-STATS-MANIFEST-001) — a multi-GB file is
#: NEVER read whole (review round 1 complexity MAJOR-1: measured ~70 s/GB +
#: a 2.1x-file RSS spike for evidence that gates nothing).
TAIL_BYTES_CAP = 16 * 1024 * 1024


def tail_lines(path: Path, *, cap_bytes: int = TAIL_BYTES_CAP) -> tuple[list[str], int, int]:
    """Bounded seek-from-end tail read: ``(lines, bytes_read, file_bytes)``.

    Reads at most *cap_bytes* from the END of the file; when truncating, the
    first (partial) line is dropped so every returned line is complete.
    """
    st = path.stat()
    with path.open("rb") as fh:
        if st.st_size > cap_bytes:
            fh.seek(st.st_size - cap_bytes)
            fh.readline()  # drop the partial first line
        data = fh.read(cap_bytes)
    return data.decode("utf-8", errors="replace").splitlines(), len(data), st.st_size


def _repro(ctx: AcceptanceContext) -> str:
    return f"brutex verify-acceptance --checks {CHECK_ID} --seed {ctx.seed}"


def _constants_sub(ctx: AcceptanceContext) -> Any:
    from brutex.adapters.groww.constants import (
        GROWW_RATE_LIMIT_PER_MIN,
        GROWW_RATE_LIMIT_RPS_CEILING,
    )

    ok = (
        GROWW_RATE_LIMIT_RPS_CEILING == CEILING_RPS
        and GROWW_RATE_LIMIT_PER_MIN == CEILING_PER_MIN
        and GROWW_RATE_LIMIT_RPS_CEILING * 60 < GROWW_RATE_LIMIT_PER_MIN
    )
    return make_subcheck(
        "rate.constants",
        Verdict.PASS if ok else Verdict.FAIL,
        units_checked=3,
        evidence={
            "ceiling_rps": GROWW_RATE_LIMIT_RPS_CEILING,
            "ceiling_per_min": GROWW_RATE_LIMIT_PER_MIN,
            "rps_times_60": GROWW_RATE_LIMIT_RPS_CEILING * 60,
        },
        limits=["injection-site wiring cited to existing unit pins, not re-proven"],
        repro=_repro(ctx),
    )


def _in_block_window(ist: datetime) -> bool:
    from brutex.calendars.market_hours import BLOCK_END, BLOCK_START

    return BLOCK_START <= ist.time() < BLOCK_END


def _market_hours_sub(ctx: AcceptanceContext) -> Any:
    from brutex.calendars.nse import is_trading_day
    from brutex.observability.pull_journal import read_journal

    stamps: list[str] = []
    ledger = ctx.ledger()
    if ledger is not None:
        stamps.extend(r.last_attempt_at for r in ledger.records() if r.last_attempt_at)
    with contextlib.suppress(OSError):  # a wedged journal read is not a verdict
        # ONLY genuine vendor-attempt records: a LIVE pull start carries
        # dry_run=False explicitly. Heartbeats/finish records parse dry_run as
        # None — they are liveness beats from ANY BackgroundPullJob (incl. the
        # vendor-free in-window-ALLOWED sweep / greeks-backfill jobs) and must
        # never count as attempts (review round 1 MINOR).
        stamps.extend(str(e.wall_utc) for e in read_journal(ctx.lake_root) if e.dry_run is False)
    examined = 0
    excluded_pre_guard = 0
    offenders: list[str] = []
    for raw in stamps:
        try:
            stamp = datetime.fromisoformat(raw)
        except ValueError:
            continue
        # Normalise: naive is treated as UTC; aware converts to UTC wall, then +05:30.
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone(UTC).replace(tzinfo=None)
        ist = stamp + IST_OFFSET
        if ist.date() < MARKET_HOURS_GUARD_LANDED:
            excluded_pre_guard += 1
            continue
        examined += 1
        if is_trading_day(ist.date()) and _in_block_window(ist):
            offenders.append(raw)
    return make_subcheck(
        "rate.market_hours",
        Verdict.FAIL if offenders else Verdict.PASS,
        units_checked=examined,
        evidence={
            "attempts_examined": examined,
            "in_window_attempts": len(offenders),
            "offending_stamps": offenders[:ROW_CAP],
            "excluded_pre_guard": excluded_pre_guard,
            "block_window_ist": "08:45-15:35",
            "journal_stamp_law": "pull-start records with dry_run=False only — "
            "heartbeats are job liveness, never vendor attempts",
        },
        limits=[
            "the sanctioned #1692 per-contract finish overrun can stamp minutes "
            "past 08:45 — an in-window offender needs operator judgment"
        ],
        repro=_repro(ctx),
    )


def _bridge_sub(ctx: AcceptanceContext) -> Any:
    from brutex.data.rate_bridge import read_rate_snapshot

    snapshot = read_rate_snapshot(ctx.lake_root)
    if snapshot is None:
        return make_subcheck(
            "rate.bridge_snapshot",
            Verdict.ABSTAIN,
            units_checked=0,
            evidence={"reason": "no bridge file at <lake>/.rate/pull_rate.json"},
            abstain_reason=AbstainReason.NO_BRIDGE_FILE,
            repro=_repro(ctx),
        )
    over = (
        snapshot.current_rps > CEILING_RPS
        or snapshot.effective_rps > CEILING_RPS
        or snapshot.recent_minute > CEILING_PER_MIN
    )
    return make_subcheck(
        "rate.bridge_snapshot",
        Verdict.FAIL if over else Verdict.PASS,
        units_checked=1,
        evidence={
            "current_rps": snapshot.current_rps,
            "effective_rps": snapshot.effective_rps,
            "recent_minute": snapshot.recent_minute,
            "recent_429": snapshot.recent_429,
        },
        limits=["last-writer-wins point sample only — never a historical claim"],
        repro=_repro(ctx),
    )


def rate_log_base() -> Path:
    """The rotating structlog JSON sink BASE path (env-overridable, house law)."""
    explicit = os.environ.get("BRUTEX_LOG_FILE")
    if explicit:
        return Path(explicit)
    log_dir = Path(os.environ.get("BRUTEX_LOG_DIR", str(Path.home() / ".brutex" / "logs")))
    return log_dir / "brutex.log"


def rate_log_paths() -> list[Path]:
    """The rotating structlog JSON sink paths (env-overridable, house law)."""
    base = rate_log_base()
    return sorted(base.parent.glob(base.name + "*")) if base.parent.is_dir() else []


def _log_window_sub(ctx: AcceptanceContext) -> Any:
    paths = rate_log_paths()
    if not paths:
        # CASE (1): the rotating structlog sink is ABSENT ENTIRELY. Only
        # ``cmd_dashboard`` enables the file sink; a headless nightly-cron /
        # CLI-fetch box (the MOTIVATING 01:30 case) writes no rate.* sink by
        # design. This is a STANDING STRUCTURAL gap identical in kind to
        # ``rate.per_call_history`` — budget/streak-EXEMPT — so an absent-by-
        # design sink can never turn a healthy headless lake permanently amber
        # (round-8 refuter observation). NOT the present-but-empty case below.
        return make_subcheck(
            "rate.log_window",
            Verdict.ABSTAIN,
            units_checked=0,
            evidence={
                "reason": "no rotating log sink exists — headless / dashboard never run",
                "searched": str(rate_log_base()),
                "law": "only cmd_dashboard enables the structlog file sink; a nightly "
                "run / CLI fetch writes no rate.* sink — an absent sink is a "
                "by-design standing gap, not a nightly regression",
            },
            abstain_reason=AbstainReason.NO_RATE_LOG_CONFIGURED,
            limits=[
                "absent-by-design standing gap — structural, budget/streak-EXEMPT",
                "run the dashboard to enable the file sink and gain a real verdict",
            ],
            repro=_repro(ctx),
        )
    rate_lines = 0
    over: list[str] = []
    histogram: dict[str, int] = {}
    for path in paths:
        try:
            lines, _read, _size = tail_lines(path)
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            name = str(event.get("event", ""))
            if not name.startswith("rate."):
                continue
            rate_lines += 1
            histogram[name] = histogram.get(name, 0) + 1
            current = event.get("current_rps")
            minute = event.get("recent_minute")
            if (isinstance(current, (int, float)) and current > CEILING_RPS) or (
                isinstance(minute, (int, float)) and minute > CEILING_PER_MIN
            ):
                over.append(line[:200])
    if rate_lines == 0:
        # CASE (2): the sink file(s) EXIST but hold no parseable rate.* lines in
        # the window. This could indicate a real problem (a present-but-silent
        # log), so it stays the NON-structural, streak-TRACKED abstain.
        return make_subcheck(
            "rate.log_window",
            Verdict.ABSTAIN,
            units_checked=0,
            evidence={
                "reason": "no rate.* lines in the rotating log window",
                "sink_files": [str(p) for p in paths[:ROW_CAP]],
            },
            abstain_reason=AbstainReason.NO_RATE_LOG_LINES,
            limits=["sink present but silent — TRACKED (streak-counted), not a standing gap"],
            repro=_repro(ctx),
        )
    return make_subcheck(
        "rate.log_window",
        Verdict.FAIL if over else Verdict.PASS,
        units_checked=rate_lines,
        evidence={
            "rate_lines": rate_lines,
            "event_histogram": histogram,
            "over_cap_lines": over[:ROW_CAP],
            "tail_bytes_cap": TAIL_BYTES_CAP,
        },
        limits=[
            "dashboard-launched pulls within log retention only",
            f"bounded tail read — last {TAIL_BYTES_CAP} bytes per rotating file",
        ],
        repro=_repro(ctx),
    )


def _pair_bound_sub(ctx: AcceptanceContext) -> Any:
    """Trend evidence from the ingest audits' bounded TAIL — verdict ALWAYS abstains.

    Pair-completion times are NOT call-issue times: pairs/min > 500 does not
    pigeonhole calls into a minute, so a verdict in either direction would be
    fabricated. Numbers attach as evidence only — which is why this read is a
    bounded seek-from-end tail (:data:`TAIL_BYTES_CAP`), never a whole-file
    parse, and the full-chain ``verify_audit_file`` walk is deliberately NOT
    run here (review round 1 complexity MAJOR-1: ~70 s/GB + a 2.1x-file RSS
    spike for an ABSTAIN that gates nothing); chain verification stays the
    EXISTING ``brutex audit verify`` command.
    """
    max_per_minute = 0
    pairs_total = 0
    tails: dict[str, Any] = {}
    for name in ("spot-ingest.jsonl", "fno-ingest.jsonl"):
        path = ctx.lake_root / "audit" / name
        if not path.is_file():
            tails[name] = "absent"
            continue
        try:
            lines, bytes_read, file_bytes = tail_lines(path)
        except OSError as exc:
            tails[name] = f"unreadable: {type(exc).__name__}"
            continue
        tails[name] = {
            "file_bytes": file_bytes,
            "bytes_read": bytes_read,
            "tail_only": file_bytes > bytes_read,
        }
        stamps: list[float] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue  # un-verified tail — a torn/foreign line is skipped
            if entry.get("action") != "INGEST_PAIR_DONE":
                continue
            try:
                stamps.append(float(entry.get("ts_utc", 0.0)))
            except (TypeError, ValueError):
                continue  # a junk ts_utc never crashes the evidence fold (N4)
        stamps.sort()
        pairs_total += len(stamps)
        lo = 0
        for hi, ts in enumerate(stamps):
            while ts - stamps[lo] > ROLLING_MINUTE_S:
                lo += 1
            max_per_minute = max(max_per_minute, hi - lo + 1)
    return make_subcheck(
        "rate.pair_bound",
        Verdict.ABSTAIN,
        units_checked=pairs_total,
        evidence={
            "pairs_total": pairs_total,
            "max_pairs_per_rolling_minute": max_per_minute,
            "tails": tails,
            "tail_bytes_cap": TAIL_BYTES_CAP,
            "chain_verify": "not run here — bounded evidence tail; verify via brutex audit verify",
        },
        abstain_reason=AbstainReason.NO_PER_CALL_LOG,
        limits=[
            "pair-completion times are not call-issue times — evidence only",
            f"bounded tail read — last {TAIL_BYTES_CAP} bytes per audit file",
        ],
        repro=_repro(ctx),
    )


def _per_call_history_sub(ctx: AcceptanceContext) -> Any:
    return make_subcheck(
        "rate.per_call_history",
        Verdict.ABSTAIN,
        units_checked=0,
        evidence={
            "law": "no per-call timestamped record persists — retrospective max r/s "
            "is UNPROVABLE from disk",
            "remediation": "a persisted per-issue log is follow-up F3 — OPERATOR RULING "
            "required (adjacent to the Python-locked fetch path)",
            "live_probe": "brutex doctor --live --stage 13 (opt-in, vendor-calling)",
        },
        abstain_reason=AbstainReason.NO_PER_CALL_PERSISTENCE,
        repro=_repro(ctx),
    )


def run(ctx: AcceptanceContext) -> CheckResult:
    """Run the six rate sub-verdicts over persisted evidence only."""
    subs = [
        _constants_sub(ctx),
        _market_hours_sub(ctx),
        _bridge_sub(ctx),
        _log_window_sub(ctx),
        _pair_bound_sub(ctx),
        _per_call_history_sub(ctx),
    ]
    coverage = make_coverage(
        mode="full",
        population=len(subs),
        examined=len(subs),
        seed=ctx.seed,
        selection=["persisted evidence: ledger + journal + bridge + logs + audits"],
    )
    return make_check(
        CHECK_ID,
        subs,
        coverage=coverage,
        repro=_repro(ctx),
        limits=[
            "vendor tolerance of sustained 8 r/s stays UNVERIFIED by design (§2 Rule 6)",
            "no blanket rate PASS exists — sub-verdicts only",
        ],
    )
