"""THE composed sweep data-preflight gate — one core, both surfaces.

DEC-SWEEP-PREFLIGHT-ESTIMATOR-001 (Phase 1, WP5 — BUILD-SPEC §2.6/§3.1).
:func:`run_sweep_data_preflight` composes the pure preflight cores
(:mod:`brutex.engine.sweep_data_preflight`), the live estimator
(:mod:`brutex.engine.sweep_estimate`), the calibration store
(:mod:`brutex.engine.sweep_calibration`), the relocated SLA verdict
(:mod:`brutex.engine.sweep_sla_guard`) and the READ-ONLY ``brutex.data``
adapters into ONE gather-all verdict — the SAME runner serves the CLI
(``cmd_sweep_all``, pre-``Catalog.open``) and the dashboard's synchronous
``POST /api/v1/sweep/preflight`` route, so the two surfaces can never
diverge.

**Pipeline order (G0-G7, every gate always evaluated when its inputs
exist):** G0 exclusion PROBE (pull + derive locks — the runner only ever
probes, it NEVER acquires) → G1 date clamps + expected-session enumeration →
G2 DISK coverage scan + fold (SKIPPED only when G1 hard-refused — no
expected list exists, ``coverage=None``) → G3 ledger-quarantine scan + fold
→ G4 estimator (ALWAYS computed when the axes parse — a refusal payload
still shows the trials/GB/hours of what was refused) → G5 the RELOCATED
``check_sweep_sla`` verdict fed ``per_tuple_seconds = est_seconds / tuples``
from the SAME displayed time model (ONE verdict, one figure) → G6 the
fail-CLOSED results-volume disk decision → G7 compose.

**Refuse-whole aggregation law:** the outcome gathers EVERY failed gate
and, inside the coverage gate, EVERY not-ready ``(symbol, TF)`` pair with
its exact missing spans — one refusal text names everything (never
fail-fast-first-symbol, never a run-ready subset, never an auto-pull).

**Probe-only law (R42):** the runner never acquires any lock and never
writes anything anywhere — a refusal leaves the lake AND the results volume
byte-identical (not even ``sweep.lock`` is created; the guard is acquired
by the CLI only on an ok verdict). Emits exactly ONE structlog event per
call — ``sweep.preflight.passed`` or ``sweep.preflight.refused`` (the
composed sub-modules additionally emit their own documented events).
``MemoryError`` re-raises raw everywhere; any other per-pair scan defect
folds into a named ``CORRUPT_PARTITION`` refusal (fail-closed — an
unverifiable pair never launches).

Layering (BUILD-SPEC D6): this module imports engine pure cores +
``brutex.data`` adapters + ``brutex.calendars`` ONLY — never
``brutex.dashboard`` (the lake content token is INJECTED as *lake_token*)
and never ``brutex.adapters`` (the writing ``probe_pull_singleflight`` is
in NO preflight import graph — AST-pinned).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Final

import structlog

from brutex.calendars import IST
from brutex.calendars.observed_sessions import load_observed_holidays
from brutex.data.derive_lock import derive_in_progress
from brutex.data.lake_footprint import volume_free_bytes
from brutex.data.session_coverage import scan_ledger_quarantine, scan_selection_coverage
from brutex.data.sweep_exclusion import pull_in_progress
from brutex.engine.sweep_calibration import (
    calibration_path,
    hardware_fingerprint,
    load_calibration,
)
from brutex.engine.sweep_data_preflight import (
    MAX_SPANS_NAMED,
    CoverageReport,
    DateClampReport,
    ExclusionReport,
    LedgerFlag,
    QuarantineReport,
    RefusalCode,
    clamp_dates,
    preflight_disk_coverage,
    preflight_quarantine,
)
from brutex.engine.sweep_estimate import (
    ESTIMATE_SCHEMA_VERSION,
    DiskPreflightDecision,
    SweepEstimate,
    check_results_disk,
    estimate_selection,
    selection_confirm_token,
    selection_from_raw,
)
from brutex.engine.sweep_sla_guard import SlaDecision, SlaProjection, check_sweep_sla

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Mapping, Sequence
    from collections.abc import Set as AbstractSet
    from datetime import date
    from pathlib import Path

    from brutex.data.session_coverage import PairScan
    from brutex.engine.sweep_estimate import SweepSelection

__all__ = [
    "GateRefusal",
    "SweepPreflightOutcome",
    "run_sweep_data_preflight",
]

logger = structlog.get_logger("brutex.engine.sweep_gate")

#: The pinned closing remedy line — :meth:`SweepPreflightOutcome.refusal_text`
#: ALWAYS ends with it (golden-pinned): every gate's message carries its own
#: specific remedy (pull / reopen-skipped / free disk / wait for the lock);
#: this line states the refuse-whole contract + the probe-only guarantee.
_REFUSAL_REMEDY_LINE: Final[str] = (
    "fix every cause above, then re-run the sweep — this refusal wrote NOTHING "
    "(probe-only preflight; the sweep NEVER pulls, R3)."
)


@dataclass(frozen=True, slots=True)
class GateRefusal:
    """One machine-coded refusal gathered by the composed gate.

    Attributes
    ----------
    code
        The machine-stable :class:`~brutex.engine.sweep_data_preflight.
        RefusalCode` (the P2-P6 payload contract).
    message
        Complete, self-contained operator text — symbols + spans + remedy.
        Codes raised by the SAME gate share the gate's one complete message
        (the renderer de-duplicates, so the text never repeats).
    """

    code: RefusalCode
    message: str


@dataclass(frozen=True, slots=True)
class SweepPreflightOutcome:
    """The composed gather-all preflight verdict (immutable).

    Attributes
    ----------
    ok
        ``True`` iff ZERO refusals were gathered across G0-G6.
    refusals
        GATHER-ALL, pipeline-ordered: every failed gate reports — the
        operator sees the COMPLETE fix list in ONE round-trip (R4/O-a).
    exclusion
        The G0 lock-probe verdict (probe-only — never acquires).
    clamp
        The G1 date-clamp report (carries the canonical expected sessions).
    coverage
        The G2 disk-coverage fold; ``None`` ONLY when G1 hard-refused (no
        expected list exists to verify against).
    quarantine
        The G3 ledger fold; ``None`` under the same G1 hard-refusal skip.
    estimate
        ALWAYS computed when the axes parse — shown beside any refusals so
        the operator sees the cost of what was refused.
    sla
        The RELOCATED :func:`~brutex.engine.sweep_sla_guard.check_sweep_sla`
        verdict, fed the SAME displayed time model (one figure, never two).
    disk
        The fail-CLOSED G6 results-volume decision.
    notes
        Clamp notes + the calibration-source note (+ the acknowledged-SLA
        warning and any probe-failure note) — surfaced on BOTH surfaces.
    confirm_token
        Minted ONLY when ``ok`` AND the caller supplied *lake_token* (the
        dashboard); ``None`` otherwise (the CLI's consent path is the
        sequential print-then-proceed / ``--estimate-only``).
    """

    ok: bool
    refusals: tuple[GateRefusal, ...]
    exclusion: ExclusionReport
    clamp: DateClampReport
    coverage: CoverageReport | None
    quarantine: QuarantineReport | None
    estimate: SweepEstimate
    sla: SlaProjection
    disk: DiskPreflightDecision
    notes: tuple[str, ...]
    confirm_token: str | None

    def refusal_text(self) -> str:
        """THE renderer — gate-ordered refusal composition; ``""`` when ok.

        CLI stderr prints exactly this text and the payload carries it
        verbatim (one renderer, two surfaces — golden-pinned). Gates render
        in pipeline order with their COMPLETE lists; codes sharing one
        gate's message are folded onto a single line (never repeated text);
        the text ALWAYS ends with the pinned remedy line and is never empty
        when the outcome is not ok.
        """
        if self.ok:
            return ""
        grouped: dict[str, list[RefusalCode]] = {}
        for refusal in self.refusals:
            grouped.setdefault(refusal.message, []).append(refusal.code)
        all_codes = ", ".join(dict.fromkeys(str(r.code) for r in self.refusals))
        lines = [f"sweep preflight REFUSED ({len(self.refusals)} refusal(s); codes: {all_codes}):"]
        for message, codes in grouped.items():
            code_names = ", ".join(dict.fromkeys(str(code) for code in codes))
            lines.append(f"- [{code_names}] {message}")
        lines.append(_REFUSAL_REMEDY_LINE)
        return "\n".join(lines)

    def payload(self) -> dict[str, object]:
        """THE JSON payload — ``schema_version=1``; the dashboard 200/409 body.

        Key-set golden-pinned (the P2-P6 machine contract); every value is
        JSON-safe (dates ISO, ``Decimal`` margin stringified, enums as their
        stable string values).
        """
        return {
            "schema_version": ESTIMATE_SCHEMA_VERSION,
            "ok": self.ok,
            "refusals": [{"code": str(r.code), "message": r.message} for r in self.refusals],
            "refusal_text": self.refusal_text(),
            "exclusion": {
                "ok": self.exclusion.ok,
                "pull_active": self.exclusion.pull_active,
                "derive_active": self.exclusion.derive_active,
            },
            "clamp": _clamp_payload(self.clamp),
            "coverage": None if self.coverage is None else _coverage_payload(self.coverage),
            "quarantine": (
                None if self.quarantine is None else _quarantine_payload(self.quarantine)
            ),
            "estimate": self.estimate.payload(),
            "sla": {
                "decision": str(self.sla.decision),
                "tuple_count": self.sla.tuple_count,
                "per_tuple_seconds": self.sla.per_tuple_seconds,
                "projected_seconds": self.sla.projected_seconds,
                "projected_days": self.sla.projected_days,
                "envelope_seconds": self.sla.envelope_seconds,
            },
            "disk": {
                "ok": self.disk.ok,
                "blocked": self.disk.blocked,
                "free_bytes": self.disk.free_bytes,
                "need_bytes": self.disk.need_bytes,
                "margin": str(self.disk.margin),
                "probe_error": self.disk.probe_error,
                "volume": self.disk.volume,
            },
            "notes": list(self.notes),
            "confirm_token": self.confirm_token,
        }


def _clamp_payload(clamp: DateClampReport) -> dict[str, object]:
    """JSON-safe projection of the G1 clamp report (expected list as a count)."""
    return {
        "requested_start": clamp.requested_start.isoformat(),
        "requested_end": clamp.requested_end.isoformat(),
        "start": clamp.start.isoformat(),
        "end": clamp.end.isoformat(),
        "notes": list(clamp.notes),
        "reversed_range": clamp.reversed_range,
        "zero_sessions": clamp.zero_sessions,
        "out_of_calendar": clamp.out_of_calendar,
        "expected_sessions": len(clamp.expected_sessions),
    }


def _coverage_payload(coverage: CoverageReport) -> dict[str, object]:
    """JSON-safe projection of the G2 coverage fold (verdicts + spans + tallies)."""
    return {
        "ok": coverage.ok,
        "checked_pairs": coverage.checked_pairs,
        "expected_sessions": coverage.expected_sessions,
        "not_ready": [
            {
                "symbol": verdict.symbol,
                "interval": verdict.interval,
                "reason": str(verdict.reason),
                "detail": verdict.detail,
                "spans": [
                    {
                        "start": span.start.isoformat(),
                        "end": span.end.isoformat(),
                        "sessions": span.sessions,
                    }
                    for span in verdict.spans
                ],
                "text": verdict.describe(),
            }
            for verdict in coverage.not_ready
        ],
        "covered_sessions": [
            {"symbol": symbol, "interval": interval, "covered": count}
            for (symbol, interval), count in coverage.covered_sessions.items()
        ],
    }


def _flag_payload(flag: LedgerFlag) -> dict[str, object]:
    """JSON-safe projection of one quarantine ledger flag."""
    return {
        "security_id": flag.security_id,
        "interval": flag.interval,
        "window_start": flag.window_start,
        "window_end": flag.window_end,
        "status": flag.status,
        "last_error": flag.last_error,
    }


def _quarantine_payload(quarantine: QuarantineReport) -> dict[str, object]:
    """JSON-safe projection of the G3 quarantine fold (both buckets + verdict)."""
    return {
        "ok": quarantine.ok,
        "ledger_load_failed": quarantine.ledger_load_failed,
        "demoted_open": [_flag_payload(flag) for flag in quarantine.demoted_open],
        "accepted_gaps": [_flag_payload(flag) for flag in quarantine.accepted_gaps],
    }


def _flag_clause(flag: LedgerFlag) -> str:
    """Render one quarantine flag as its honest one-line clause."""
    return (
        f"{flag.security_id} × {flag.interval} "
        f"[{flag.window_start} → {flag.window_end}: {flag.last_error}]"
    )


def _capped_flags(flags: tuple[LedgerFlag, ...]) -> str:
    """Render a flag list capped at :data:`MAX_SPANS_NAMED` with the TRUE total."""
    named = "; ".join(_flag_clause(flag) for flag in flags[:MAX_SPANS_NAMED])
    extra = len(flags) - MAX_SPANS_NAMED
    if extra > 0:
        named += f" (+{extra} more; {len(flags)} windows in total)"
    return named


def _quarantine_message(quarantine: QuarantineReport) -> str:
    """The G3 gate's ONE complete refusal message — every class + remedies.

    Gather-all inside the gate: an unreadable ledger, the open demotions and
    the accepted gaps are ALL named in one text (capped per class with the
    TRUE totals — the house cap style), ending with the per-class remedies
    from :meth:`~brutex.engine.sweep_data_preflight.QuarantineReport.
    remediation`.
    """
    parts: list[str] = []
    if quarantine.ledger_load_failed:
        parts.append(
            "the persisted window ledger is UNREADABLE — 'no quarantined partitions' "
            "is unverifiable (fail-CLOSED)"
        )
    if quarantine.demoted_open:
        parts.append(
            f"{len(quarantine.demoted_open)} OPEN demoted window(s) (FAILED — flagged by "
            f"the completeness detectors, will re-pull): "
            f"{_capped_flags(quarantine.demoted_open)}"
        )
    if quarantine.accepted_gaps:
        parts.append(
            f"{len(quarantine.accepted_gaps)} accepted-gap window(s) (SKIPPED — settled "
            f"over KNOWN-incomplete stored bars): {_capped_flags(quarantine.accepted_gaps)}"
        )
    return f"window-ledger quarantine gate: {'; '.join(parts)} — {quarantine.remediation()}"


def _exclusion_refusals(exclusion: ExclusionReport) -> tuple[GateRefusal, ...]:
    """G0 → per-code refusals sharing the report's one complete message."""
    if exclusion.ok:
        return ()
    message = exclusion.refusal()
    refusals: list[GateRefusal] = []
    if exclusion.pull_active:
        refusals.append(GateRefusal(code=RefusalCode.PULL_IN_FLIGHT, message=message))
    if exclusion.derive_active:
        refusals.append(GateRefusal(code=RefusalCode.DERIVE_IN_FLIGHT, message=message))
    return tuple(refusals)


def _clamp_refusals(clamp: DateClampReport) -> tuple[GateRefusal, ...]:
    """G1 → per-code refusals sharing the clamp's one complete cause text."""
    if clamp.ok:
        return ()
    message = clamp.refusal()
    return tuple(GateRefusal(code=code, message=message) for code in clamp.refusal_codes)


def _coverage_refusals(coverage: CoverageReport) -> tuple[GateRefusal, ...]:
    """G2 → one refusal per DISTINCT verdict code, sharing the complete fold text."""
    if coverage.ok:
        return ()
    message = coverage.refusal()
    distinct_codes = dict.fromkeys(verdict.reason for verdict in coverage.not_ready)
    return tuple(GateRefusal(code=code, message=message) for code in distinct_codes)


def _quarantine_refusals(quarantine: QuarantineReport) -> tuple[GateRefusal, ...]:
    """G3 → the fail-closed ledger code and/or the quarantine-overlap code."""
    if quarantine.ok:
        return ()
    message = _quarantine_message(quarantine)
    refusals: list[GateRefusal] = []
    if quarantine.ledger_load_failed:
        refusals.append(GateRefusal(code=RefusalCode.LEDGER_UNREADABLE, message=message))
    if quarantine.demoted_open or quarantine.accepted_gaps:
        refusals.append(GateRefusal(code=RefusalCode.QUARANTINED_RANGE, message=message))
    return tuple(refusals)


def _scan_pairs_fenced(
    *,
    lake_root: Path,
    exchange: str,
    segment: str,
    symbols: Sequence[str],
    intervals: Sequence[str],
    start: date,
    end: date,
) -> tuple[dict[tuple[str, str], PairScan], tuple[str, ...]]:
    """Scan every requested pair, fencing per-pair defects (BUILD-SPEC §2.6).

    Each pair is scanned through the same READ-ONLY
    :func:`~brutex.data.session_coverage.scan_selection_coverage` (one pair
    per call — identical total work, but a defect is attributable to ITS
    pair). ``MemoryError`` re-raises raw; any other defect is folded into a
    named clause (the composed gate turns it into a ``CORRUPT_PARTITION``
    refusal) and the pair's scan entry stays ABSENT, so the coverage fold
    ALSO marks the pair not-ready (fail-closed — an unverifiable pair never
    launches). Expected garbage-bytes parquet files never reach this fence:
    the scanner already names them into ``PairScan.corrupt_files``.
    """
    scans: dict[tuple[str, str], PairScan] = {}
    defects: list[str] = []
    for symbol in symbols:
        for interval in intervals:
            try:
                scans.update(
                    scan_selection_coverage(
                        lake_root=lake_root,
                        exchange=exchange,
                        segment=segment,
                        symbols=(symbol,),
                        intervals=(interval,),
                        start=start,
                        end=end,
                    )
                )
            except MemoryError:
                raise
            except Exception as exc:  # the per-pair defect fence (fail-closed)
                defects.append(
                    f"coverage scan defect for {symbol} × {interval}: "
                    f"{type(exc).__name__}: {exc} — the pair is unverifiable and refuses "
                    f"fail-CLOSED; inspect the named lake partition, repair or re-pull on "
                    f"/universe, then re-run the sweep"
                )
    return scans, tuple(defects)


def _calibration_note(snapshot_path: Path, samples: int, qualifying: int, *, state: str) -> str:
    """Render the calibration-source provenance note for the outcome."""
    if state == "absent":
        return (
            f"calibration store: absent at {snapshot_path} — estimates stay PROVISIONAL "
            f"until a qualifying run lands"
        )
    if state == "calibrated":
        return (
            f"calibration store: {snapshot_path} — {samples} ring entries "
            f"({qualifying} qualifying), hardware-matched"
        )
    return (
        f"calibration store: {snapshot_path} — present but NOT calibrated "
        f"({qualifying} qualifying entries; estimates stay PROVISIONAL)"
    )


def run_sweep_data_preflight(
    *,
    lake_root: Path,
    out_root: Path,
    exchange: str,
    segment: str,
    symbols: Sequence[str],
    intervals: Sequence[str],
    directions: Sequence[str],
    k_values: Sequence[int],
    condition_bits: Sequence[int] | None,
    stop_losses: Sequence[int | None],
    take_profits: Sequence[int | None],
    fib_anchors: Sequence[str],
    proximities: Sequence[float | None] = (None,),
    top_k: int,
    start: date,
    end: date,
    acknowledge_sla: bool = False,
    dedup_map_bytes: int = 0,
    today: date | None = None,
    observed: AbstractSet[date] | None = None,
    free_bytes: int | None = None,
    lake_token: str | None = None,
    env: Mapping[str, str] | None = None,
) -> SweepPreflightOutcome:
    """Run the composed G0-G7 preflight pipeline — probe-only, gather-all.

    The ONE core both production surfaces call (BUILD-SPEC D2): the CLI
    between ``_require_lake_dir`` and ``Catalog.open``, and the dashboard's
    synchronous preflight route. A refusal writes NOTHING (not even
    ``sweep.lock``); the CLI acquires the run guard only on an ok verdict.

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/``).
    out_root
        The results volume root (``results_root or <lake>/sweeps``) — the
        disk gate's target and the calibration sidecar's home.
    exchange, segment
        The launch's lake coordinates (e.g. ``NSE`` / ``INDEX``).
    symbols, intervals, directions, k_values, stop_losses, take_profits,
    fib_anchors, proximities
        The raw selection axes (deduped first-seen-order internally — the
        deduped axes feed the scans AND the estimate, mirroring the grid).
    condition_bits
        The swept condition-bit set; ``None`` ⇒
        :func:`~brutex.engine.sweep_estimate.default_condition_bits`.
    top_k
        Persisted-rows-per-tuple cap (``>= 1``).
    start, end
        The REQUESTED inclusive date range (clamped by G1).
    acknowledge_sla
        The ``--acknowledge-sla-overrun`` consent axis (an acknowledged
        overrun is a NOTE, never a refusal — consent axes stay separate).
    dedup_map_bytes
        The resolved LMDB dedup-map reserve, injected by the caller.
    today
        Injectable clock (the ``resolve_sweep_request(today=…)`` precedent);
        ``None`` ⇒ the current IST date.
    observed
        The observed-holidays overlay; ``None`` ⇒
        :func:`~brutex.calendars.observed_sessions.load_observed_holidays`
        over *lake_root* (never raises).
    free_bytes
        Injected results-volume free bytes; ``None`` ⇒ probe
        :func:`~brutex.data.lake_footprint.volume_free_bytes` — a probe
        ``OSError`` maps to ``None`` ⇒ the fail-CLOSED disk refusal
        (``MemoryError`` re-raises raw).
    lake_token
        The dashboard passes ``lake_revision(lake_root)`` (injected — the
        engine never imports ``brutex.dashboard``); ``None`` (CLI) ⇒ no
        ``confirm_token`` is minted.
    env
        Environment mapping forwarded to the estimator's per-tuple resolver
        (tests inject; ``None`` = the process environment).

    Returns
    -------
    SweepPreflightOutcome
        The gather-all verdict; ``ok`` iff every gate passed.

    Raises
    ------
    ValueError
        On unparseable axes (empty axis / ``k`` out of range / ``top_k < 1``
        / a malformed per-tuple env override) — a clean 400/user error,
        never a traceback into the gates.
    MemoryError
        Re-raised raw from any probe/scan (never folded).
    """
    resolved_today = datetime.now(IST).date() if today is None else today
    resolved_observed = load_observed_holidays(lake_root) if observed is None else observed

    # G0 — exclusion PROBE (cheapest first; probe-only, never acquires).
    exclusion = ExclusionReport(
        pull_active=pull_in_progress(lake_root),
        derive_active=derive_in_progress(lake_root),
    )
    refusals: list[GateRefusal] = list(_exclusion_refusals(exclusion))

    # G1 — date clamps + expected-session enumeration.
    clamp = clamp_dates(start, end, today=resolved_today, observed=resolved_observed)
    refusals.extend(_clamp_refusals(clamp))

    # The deduped selection (pure) — its axes feed the scans AND the estimate,
    # so a duplicated request token is scanned/counted exactly once (the grid
    # dedups identically). ValueError on bad axes propagates here, BEFORE any
    # lake scan ("estimate ALWAYS computed when axes parse").
    sel: SweepSelection = selection_from_raw(
        symbols=symbols,
        intervals=intervals,
        directions=directions,
        k_values=k_values,
        stop_losses=stop_losses,
        take_profits=take_profits,
        fib_anchors=fib_anchors,
        proximities=proximities,
        condition_bits=condition_bits,
        top_k=top_k,
        start=clamp.start,
        end=clamp.end,
    )

    # G2 + G3 — DISK coverage and ledger quarantine, SKIPPED only when G1
    # hard-refused (no expected list / no meaningful clamped range exists).
    coverage: CoverageReport | None = None
    quarantine: QuarantineReport | None = None
    if clamp.ok:
        scans, scan_defects = _scan_pairs_fenced(
            lake_root=lake_root,
            exchange=exchange,
            segment=segment,
            symbols=sel.symbols,
            intervals=sel.intervals,
            start=clamp.start,
            end=clamp.end,
        )
        coverage = preflight_disk_coverage(
            clamp.expected_sessions,
            scans,
            symbols=sel.symbols,
            intervals=sel.intervals,
        )
        refusals.extend(_coverage_refusals(coverage))
        refusals.extend(
            GateRefusal(code=RefusalCode.CORRUPT_PARTITION, message=defect)
            for defect in scan_defects
        )
        quarantine = preflight_quarantine(
            scan_ledger_quarantine(
                lake_root,
                exchange=exchange,
                symbols=sel.symbols,
                intervals=sel.intervals,
                start=clamp.start,
                end=clamp.end,
            )
        )
        refusals.extend(_quarantine_refusals(quarantine))

    # G4 — the estimator (fail-SOFT calibration load; ALWAYS computed).
    fingerprint = hardware_fingerprint()
    snapshot = load_calibration(out_root, fingerprint=fingerprint)
    estimate = estimate_selection(
        sel, calibration=snapshot, dedup_map_bytes=dedup_map_bytes, env=env
    )
    sidecar = calibration_path(out_root)
    if snapshot is None:
        cal_note = _calibration_note(sidecar, 0, 0, state="absent")
    elif snapshot.calibrated:
        cal_note = _calibration_note(
            sidecar, snapshot.samples, snapshot.qualifying_samples, state="calibrated"
        )
    else:
        cal_note = _calibration_note(
            sidecar, snapshot.samples, snapshot.qualifying_samples, state="uncalibrated"
        )

    # G5 — the RELOCATED SLA verdict, fed the SAME displayed time model so
    # the confirm gate and the SLA refusal can never quote two hour figures.
    sla = check_sweep_sla(
        estimate.tuples,
        acknowledge=acknowledge_sla,
        per_tuple_seconds=estimate.est_seconds / estimate.tuples,
    )
    notes: list[str] = [*clamp.notes, cal_note]
    if sla.decision is SlaDecision.REFUSED:
        refusals.append(GateRefusal(code=RefusalCode.SLA_OVERRUN, message=sla.refusal_message()))
    elif sla.decision is SlaDecision.ACKNOWLEDGED:
        notes.append(sla.acknowledged_warning())

    # G6 — fail-CLOSED disk decision over the injected/probed free bytes.
    resolved_free = free_bytes
    probe_failure: str | None = None
    if resolved_free is None:
        try:
            resolved_free = volume_free_bytes(out_root)
        except OSError as exc:  # MemoryError deliberately NOT caught — re-raises raw
            probe_failure = f"{type(exc).__name__}: {exc}"
            notes.append(
                f"results-volume free-space probe failed ({probe_failure}) — refusing fail-CLOSED"
            )
    disk = check_results_disk(estimate, out_root=out_root, free_bytes=resolved_free)
    if disk.probe_error is not None and probe_failure is not None:
        disk = replace(disk, probe_error=f"{disk.probe_error}: {probe_failure}")
    if disk.blocked:
        refusals.append(GateRefusal(code=RefusalCode.DISK_SPACE, message=disk.refusal()))

    # G7 — compose (the confirm token binds the proceed to the exact numbers
    # + lake state seen; minted ONLY on an ok verdict with a caller token).
    ok = not refusals
    confirm_token: str | None = None
    if ok and lake_token is not None:
        confirm_token = selection_confirm_token(
            sel, launches=((exchange, segment),), lake_token=lake_token
        )
    outcome = SweepPreflightOutcome(
        ok=ok,
        refusals=tuple(refusals),
        exclusion=exclusion,
        clamp=clamp,
        coverage=coverage,
        quarantine=quarantine,
        estimate=estimate,
        sla=sla,
        disk=disk,
        notes=tuple(notes),
        confirm_token=confirm_token,
    )
    log = logger.info if ok else logger.warning
    log(
        "sweep.preflight.passed" if ok else "sweep.preflight.refused",
        ok=ok,
        codes=[str(r.code) for r in outcome.refusals],
        exchange=exchange,
        segment=segment,
        symbols=list(sel.symbols),
        intervals=len(sel.intervals),
        start=clamp.start.isoformat(),
        end=clamp.end.isoformat(),
        trials=estimate.trials,
        tuples=estimate.tuples,
        est_seconds=estimate.est_seconds,
        est_result_bytes=estimate.est_result_bytes,
        provisional=estimate.provisional,
        sla_decision=str(sla.decision),
        disk_blocked=disk.blocked,
        free_probe_error=probe_failure,
        confirm_token=confirm_token,
    )
    return outcome
