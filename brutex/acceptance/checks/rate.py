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
"""

from __future__ import annotations

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
    try:
        stamps.extend(str(e.wall_utc) for e in read_journal(ctx.lake_root) if not e.dry_run)
    except OSError:
        pass
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
        },
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


def rate_log_paths() -> list[Path]:
    """The rotating structlog JSON sink paths (env-overridable, house law)."""
    explicit = os.environ.get("BRUTEX_LOG_FILE")
    if explicit:
        base = Path(explicit)
    else:
        log_dir = Path(os.environ.get("BRUTEX_LOG_DIR", str(Path.home() / ".brutex" / "logs")))
        base = log_dir / "brutex.log"
    return sorted(base.parent.glob(base.name + "*")) if base.parent.is_dir() else []


def _log_window_sub(ctx: AcceptanceContext) -> Any:
    rate_lines = 0
    over: list[str] = []
    histogram: dict[str, int] = {}
    for path in rate_log_paths():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
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
            if isinstance(current, (int, float)) and current > CEILING_RPS:
                over.append(line[:200])
            elif isinstance(minute, (int, float)) and minute > CEILING_PER_MIN:
                over.append(line[:200])
    if rate_lines == 0:
        return make_subcheck(
            "rate.log_window",
            Verdict.ABSTAIN,
            units_checked=0,
            evidence={"reason": "no rate.* lines in the rotating log window"},
            abstain_reason=AbstainReason.NO_RATE_LOG_LINES,
            limits=["CLI fetch never writes this sink — dashboard pulls only"],
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
        },
        limits=["dashboard-launched pulls within log retention only"],
        repro=_repro(ctx),
    )


def _pair_bound_sub(ctx: AcceptanceContext) -> Any:
    """Trend evidence from the hash-chained ingest audits — verdict ALWAYS abstains.

    Pair-completion times are NOT call-issue times: pairs/min > 500 does not
    pigeonhole calls into a minute, so a verdict in either direction would be
    fabricated. Numbers attach as evidence only.
    """
    from brutex.audit_verify import verify_audit_file

    max_per_minute = 0
    pairs_total = 0
    chains: dict[str, Any] = {}
    for name in ("spot-ingest.jsonl", "fno-ingest.jsonl"):
        path = ctx.lake_root / "audit" / name
        if not path.is_file():
            chains[name] = "absent"
            continue
        try:
            verify_audit_file(path)
            chains[name] = "chain_ok"
        except Exception as exc:  # noqa: BLE001 — verify errors are evidence here
            chains[name] = f"chain_error: {type(exc).__name__}"
            continue
        stamps: list[float] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("action") == "INGEST_PAIR_DONE":
                stamps.append(float(entry.get("ts_utc", 0.0)))
        stamps.sort()
        pairs_total += len(stamps)
        lo = 0
        for hi, ts in enumerate(stamps):
            while ts - stamps[lo] > 60.0:
                lo += 1
            max_per_minute = max(max_per_minute, hi - lo + 1)
    return make_subcheck(
        "rate.pair_bound",
        Verdict.ABSTAIN,
        units_checked=pairs_total,
        evidence={
            "pairs_total": pairs_total,
            "max_pairs_per_rolling_minute": max_per_minute,
            "chains": chains,
        },
        abstain_reason=AbstainReason.NO_PER_CALL_LOG,
        limits=["pair-completion times are not call-issue times — evidence only"],
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
