"""Report writers: frozen JSON schema, human render, audit chain, deltas, streaks.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756). The artifact of record
is ``<lake_root>/acceptance/<run_id>/report.json`` (schema
:data:`~brutex.acceptance.model.SCHEMA_VERSION`, self-validated BEFORE writing —
a malformed report ⇒ the run goes SUSPECT, never ships), beside the human
``report.txt``, the per-check ``evidence/`` spill, the atomic ``LATEST``
pointer, and the hash-chained ``acceptance/audit/acceptance.jsonl``
(:class:`~brutex.engine.sweep_audit.SweepAuditWriter` verbatim reuse — the
nightly precedent; verification later = the EXISTING ``brutex audit verify``).
ABSTAIN streaks are carried in the report chain (previous report's streak + 1)
— no separate state file.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import structlog

from brutex.acceptance.model import (
    SCHEMA_VERSION,
    CheckResult,
    Verdict,
)
from brutex.data.atomic_io import atomic_write_simple

_LOG = structlog.get_logger(__name__)

#: Metric cells the delta reader trends run-over-run (small fixed allowlist).
METRIC_DELTA_CELLS: Final[tuple[tuple[str, str, str], ...]] = (
    ("unique", "unique.census", "findings_total"),
    ("pull", "pull.replan_zero", "newly_fetchable"),
    ("pull", "pull.partition_presence", "missing"),
    ("rate", "rate.market_hours", "in_window_attempts"),
)

_REQUIRED_TOP_KEYS: Final[tuple[str, ...]] = (
    "schema",
    "run_id",
    "generated_utc",
    "duration_s",
    "box",
    "inputs",
    "self_test",
    "overall",
    "checks",
    "totals",
    "coverage_ledger",
    "previous",
    "deltas",
)

_VERDICT_TOKENS: Final[frozenset[str]] = frozenset({"PASS", "FAIL", "ABSTAIN", "ERROR"})


def mint_run_id(now_utc: datetime) -> str:
    """``acceptance-YYYYMMDDTHHMMSSZ-<6hex>`` (the nightly id idiom)."""
    stamp = now_utc.strftime("%Y%m%dT%H%M%SZ")
    return f"acceptance-{stamp}-{secrets.token_hex(3)}"


def acceptance_dir(lake_root: Path) -> Path:
    """``<lake_root>/acceptance`` — the ONLY tree this command writes under."""
    return lake_root / "acceptance"


def audit_path(lake_root: Path) -> Path:
    """The hash-chained acceptance audit JSONL path."""
    return acceptance_dir(lake_root) / "audit" / "acceptance.jsonl"


# ────────────────────────────────────────────────────────────────────────
# Schema self-validation (fail loud BEFORE writing)
# ────────────────────────────────────────────────────────────────────────


def validate_report_dict(report: Mapping[str, Any]) -> list[str]:
    """Validate a report dict against the frozen v1 schema; return error strings."""
    errors: list[str] = []
    for key in _REQUIRED_TOP_KEYS:
        if key not in report:
            errors.append(f"missing top-level key: {key}")
    if report.get("schema") != SCHEMA_VERSION:
        errors.append(f"schema token != {SCHEMA_VERSION}: {report.get('schema')!r}")
    checks = report.get("checks")
    if not isinstance(checks, list):
        errors.append("checks is not a list")
        return errors
    for check in checks:
        cid = check.get("check_id", "<unnamed>")
        if check.get("verdict") not in _VERDICT_TOKENS:
            errors.append(f"{cid}: bad verdict {check.get('verdict')!r}")
        coverage = check.get("coverage")
        if not isinstance(coverage, dict) or coverage.get("mode") not in {
            "full",
            "sample",
            "bounded",
            "none",
        }:
            errors.append(f"{cid}: bad coverage block")
        subs = check.get("sub_checks")
        if not isinstance(subs, list) or not subs:
            errors.append(f"{cid}: empty sub_checks")
            continue
        for sub in subs:
            sid = sub.get("id", f"{cid}.<unnamed>")
            if sub.get("verdict") not in _VERDICT_TOKENS:
                errors.append(f"{sid}: bad verdict {sub.get('verdict')!r}")
            evidence = sub.get("evidence")
            if not isinstance(evidence, dict) or not evidence:
                errors.append(f"{sid}: empty evidence block")
            if sub.get("verdict") == "ABSTAIN" and not sub.get("abstain_reason"):
                errors.append(f"{sid}: ABSTAIN without reason")
    return errors


# ────────────────────────────────────────────────────────────────────────
# Previous-report chain: deltas + abstain streaks
# ────────────────────────────────────────────────────────────────────────


def load_previous(lake_root: Path) -> dict[str, Any] | None:
    """Load the previous report via the ``LATEST`` pointer (None when absent/corrupt)."""
    base = acceptance_dir(lake_root)
    try:
        run_id = (base / "LATEST").read_text(encoding="utf-8").strip()
        raw = json.loads((base / run_id / "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        # RecursionError: a deep-nested JSON bomb must not crash the run (MN5).
        return None
    return raw if isinstance(raw, dict) else None


def previous_subcheck_index(
    previous: Mapping[str, Any] | None,
) -> dict[str, tuple[str, int]]:
    """``{sub_check_id: (verdict, abstain_streak)}`` from a previous report.

    A foreign-schema previous report indexes as EMPTY (the delta reader refuses
    it; deltas ship ``null`` with a disclosed reason). Round-4 fix (B4-4): the
    previous report's ``streak_carry`` block (sub-checks that did NOT run in
    that run, carried forward verbatim) seeds the index FIRST, then that run's
    own ``sub_checks`` overlay it — so a ``--checks`` subset run or a
    TEETH_FAILED run (``checks: []``) between two full runs no longer resets
    every absent sub-check's abstain streak to 1 (the streak-≥3 ⇒ exit-4 law
    was silently suppressible by interleaved subset runs).
    """
    if previous is None or previous.get("schema") != SCHEMA_VERSION:
        return {}
    out: dict[str, tuple[str, int]] = {}
    carry = previous.get("streak_carry")
    if isinstance(carry, Mapping):
        for sub_id, cell in carry.items():
            if isinstance(cell, Mapping):
                out[str(sub_id)] = (
                    str(cell.get("verdict")),
                    int(cell.get("abstain_streak", 0)),
                )
    for check in previous.get("checks", []):
        for sub in check.get("sub_checks", []):
            out[str(sub.get("id"))] = (
                str(sub.get("verdict")),
                int(sub.get("abstain_streak", 0)),
            )
    return out


def streak_carry_block(
    prev_index: Mapping[str, tuple[str, int]], checks: Sequence[CheckResult]
) -> dict[str, dict[str, Any]]:
    """The report's ``streak_carry`` block: prior index ∪ this run's sub-checks.

    Every sub-check the chain has ever seen keeps a ``(verdict,
    abstain_streak)`` cell — sub-checks ABSENT from this run (subset /
    TEETH_FAILED) ride through UNCHANGED, sub-checks that ran overwrite their
    cell with this run's stamped values (B4-4).
    """
    out: dict[str, dict[str, Any]] = {
        sub_id: {"verdict": verdict, "abstain_streak": streak}
        for sub_id, (verdict, streak) in prev_index.items()
    }
    for check in checks:
        for sub in check.sub_checks:
            out[sub.id] = {"verdict": str(sub.verdict), "abstain_streak": sub.abstain_streak}
    return out


def streak_for(sub_id: str, verdict: Verdict, prev_index: Mapping[str, tuple[str, int]]) -> int:
    """Consecutive-abstain streak carried from the previous report chain."""
    if verdict is not Verdict.ABSTAIN:
        return 0
    prev = prev_index.get(sub_id)
    if prev is not None and prev[0] == "ABSTAIN":
        return prev[1] + 1
    return 1


def compute_deltas(
    previous: Mapping[str, Any] | None, checks: Sequence[CheckResult]
) -> dict[str, Any] | None:
    """Verdict transitions + allowlisted metric deltas vs the previous report."""
    if previous is None:
        return None
    if previous.get("schema") != SCHEMA_VERSION:
        return {"refused": f"previous report schema {previous.get('schema')!r} is foreign"}
    prev_index = previous_subcheck_index(previous)
    transitions: list[dict[str, str]] = []
    for check in checks:
        for sub in check.sub_checks:
            prev = prev_index.get(sub.id)
            if prev is not None and prev[0] != str(sub.verdict):
                transitions.append({"sub_check": sub.id, "from": prev[0], "to": str(sub.verdict)})
    prev_evidence: dict[tuple[str, str], Mapping[str, Any]] = {}
    for check_dict in previous.get("checks", []):
        for sub_dict in check_dict.get("sub_checks", []):
            prev_evidence[(str(check_dict.get("check_id")), str(sub_dict.get("id")))] = (
                sub_dict.get("evidence") or {}
            )
    metric_deltas: dict[str, dict[str, Any]] = {}
    now_evidence: dict[tuple[str, str], Mapping[str, Any]] = {
        (check.check_id, sub.id): sub.evidence for check in checks for sub in check.sub_checks
    }
    for check_id, sub_id, key in METRIC_DELTA_CELLS:
        prev_val = prev_evidence.get((check_id, sub_id), {}).get(key)
        now_val = now_evidence.get((check_id, sub_id), {}).get(key)
        if isinstance(prev_val, (int, float)) and isinstance(now_val, (int, float)):
            metric_deltas[f"{sub_id}.{key}"] = {
                "prev": prev_val,
                "now": now_val,
                "delta": now_val - prev_val,
            }
    return {"verdict_transitions": transitions, "metric_deltas": metric_deltas}


# ────────────────────────────────────────────────────────────────────────
# Human render
# ────────────────────────────────────────────────────────────────────────


def _render_token(value: Any) -> str:
    """One vendor-influenceable string cell, repr()'d (the #1594 convention).

    ``None`` renders bare; everything else is repr()'d so an embedded newline /
    ESC can never forge a standalone report line (review round 1 MN3).
    """
    return "None" if value is None else repr(str(value))


def _fail_brief(evidence: Mapping[str, Any]) -> str:
    """One-line count + sample for a FAIL/ERROR sub, so evidence is inline.

    The most salient offender count + first sample is surfaced (repr()'d — an
    offender token can embed a vendor-influenceable dir name, MN3). ABSENT-key
    tolerant: hand-built / legacy evidence shapes degrade to an empty brief.
    """
    engine_bad = evidence.get("engine_bad")
    if isinstance(engine_bad, list) and engine_bad:
        return f"{len(engine_bad)} engine index(es): {_render_token(engine_bad[0])}"
    offenders = evidence.get("offenders")
    total = evidence.get("offenders_total")
    if isinstance(total, int):
        brief = f"{total} offender(s)"
        if isinstance(offenders, list) and offenders:
            brief += f"; e.g. {_render_token(offenders[0])}"
        return brief
    failure = evidence.get("failure_class")
    if isinstance(failure, str) and failure:
        return _render_token(failure)
    reason = evidence.get("reason")
    if isinstance(reason, str) and reason:
        return _render_token(reason)
    return ""


def render_human(report: Mapping[str, Any]) -> str:
    """The operator-facing text render — ABSTAINs FIRST, never buried."""
    lines: list[str] = []
    overall = report["overall"]
    self_test = report["self_test"]
    inputs = report["inputs"]
    lines.append(f"BruteX acceptance — {report['run_id']} — lake {inputs.get('lake_root')}")
    teeth = self_test.get("verdict")
    lines.append(
        f"teeth: {teeth} (mode {self_test.get('mode')}, "
        f"{self_test.get('poisons_detected')}/{self_test.get('poisons_planted')} poisons detected, "
        f"{self_test.get('duration_s')}s)"
    )
    if teeth == "TEETH_FAILED":
        lines.append("VERIFIER TEETH FAILED — nothing below is trustworthy")
    if teeth == "TEETH_SKIPPED":
        lines.append("teeth not demonstrated this run (--no-self-test)")
    ledger = report.get("coverage_ledger") or {}
    lines.append(
        f"scope: seed {inputs.get('seed')} ({inputs.get('seed_mode')}) · "
        f"sample_n {inputs.get('sample_n')} · months {inputs.get('months_bound')}"
        + (" · FULL" if inputs.get("full") else "")
    )
    if ledger.get("datasets_sampled_window_pct") is not None:
        # repr() — the dataset key embeds vendor-influenceable dir names; a
        # newline-named dir must never forge a report line (MN3).
        lines.append(
            f"coverage ledger: {ledger['datasets_sampled_window_pct']}% of datasets sampled "
            f"in last 14 nights; oldest unsampled: {_render_token(ledger.get('oldest_unsampled'))}"
        )
    abstains = [
        sub
        for check in report["checks"]
        for sub in check["sub_checks"]
        if sub["verdict"] == "ABSTAIN"
    ]
    if abstains:
        lines.append("")
        lines.append("ABSTAINS (read these first)")
        for sub in abstains:
            tag = "structural" if sub.get("structural") else f"streak {sub.get('abstain_streak')}"
            lines.append(f"  {sub['id']:<28} ABSTAIN  {sub['abstain_reason']}  [{tag}]")
            repro = sub.get("repro")
            if isinstance(repro, str) and repro:
                lines.append(f"      reproduce: {repro}")
    lines.append("")
    lines.append(f"{'CHECK':<28} {'VERDICT':<10} COVERAGE")
    for check in report["checks"]:
        cov = check["coverage"]
        cov_note = (
            "full"
            if cov["mode"] == "full"
            else f"{cov['mode']} {cov['examined']}/{cov['population']}, seed {cov['seed']}"
        )
        suffix = "" if cov["mode"] == "full" else " — NOT a full-lake claim"
        lines.append(f"{check['check_id']:<28} {check['verdict']:<10} {cov_note}{suffix}")
        for sub in check["sub_checks"]:
            evidence = sub.get("evidence") or {}
            if sub["verdict"] in {"FAIL", "ERROR"}:
                brief = _fail_brief(evidence)
                lines.append(
                    f"  ! {sub['id']}: {sub['verdict']}" + (f" — {brief}" if brief else "")
                )
                repro = sub.get("repro")
                if isinstance(repro, str) and repro:
                    lines.append(f"      reproduce: {repro}")
            # Round-3 NIT (A-2): a check-level PASS with abstain-ROUTED series
            # was invisible in report.txt — surface the count on its own line.
            routed = evidence.get("non_default_shape_series")
            if isinstance(routed, int) and routed > 0:
                lines.append(
                    f"  ~ {sub['id']}: {routed} series routed "
                    "non_default_window_shape (verified for default-law series only)"
                )
            # Round-3 NIT (A-4): the Muhurat-unverified disclosure was
            # JSON/evidence-only — the operator must see it here.
            muhurat_total = evidence.get("muhurat_days_unverified_total")
            if isinstance(muhurat_total, int) and muhurat_total > 0:
                dates = ", ".join(str(d) for d in evidence.get("muhurat_days_unverified", [])[:5])
                lines.append(
                    f"  ~ {sub['id']}: {muhurat_total} Muhurat date(s) NOT verified"
                    + (f": {dates}" if dates else "")
                )
    totals = report["totals"]
    lines.append("")
    lines.append(
        f"TOTALS pass={totals['pass']} (full {overall['passes_full']} · "
        f"sample {overall['passes_sample']}) fail={totals['fail']} "
        f"abstain={totals['abstain']} ({overall['abstain_subchecks_structural']} structural) "
        f"error={totals['error']} → exit {overall['exit_code']}"
    )
    deltas = report.get("deltas")
    if isinstance(deltas, dict):
        for t in deltas.get("verdict_transitions", []):
            lines.append(f"changed: {t['sub_check']} {t['from']}→{t['to']}")
    # Round-3 NIT (A-4, the ABSTAIN-prominence law extended): every limits
    # line is rendered — the honest envelope must be readable WITHOUT
    # opening report.json. Limits are verifier-authored static strings
    # (never vendor-influenceable), so they render raw.
    limit_lines: list[str] = []
    for check in report["checks"]:
        limit_lines.extend(f"  {check['check_id']}: {lim}" for lim in check.get("limits", []))
        for sub in check["sub_checks"]:
            limit_lines.extend(f"  {sub['id']}: {lim}" for lim in sub.get("limits", []))
    if limit_lines:
        lines.append("")
        lines.append("LIMITS (honest envelope)")
        lines.extend(limit_lines)
    return "\n".join(lines) + "\n"


# ────────────────────────────────────────────────────────────────────────
# Audit chain (SweepAuditWriter verbatim reuse — Golden Rule #8)
# ────────────────────────────────────────────────────────────────────────


class AcceptanceAudit:
    """Hash-chained run trail: RUN_START · one CHECK line each · RUN_COMPLETE."""

    def __init__(self, lake_root: Path) -> None:
        from brutex.engine.sweep_audit import SweepAuditWriter

        path = audit_path(lake_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._writer = SweepAuditWriter.open(path, actor="acceptance", resume=path.is_file())
        self._closed = False

    def run_start(self, *, run_id: str, argv: Sequence[str], seed: str) -> None:
        """Append the ``ACCEPTANCE_RUN_START`` line."""
        self._writer.append(
            action="ACCEPTANCE_RUN_START",
            subject=run_id,
            payload={"argv": list(argv), "seed": seed, "schema": SCHEMA_VERSION},
        )

    def check(self, *, run_id: str, check: CheckResult) -> None:
        """Append one ``ACCEPTANCE_CHECK`` line per completed check."""
        fails = sum(1 for s in check.sub_checks if s.verdict is Verdict.FAIL)
        abstains = sum(1 for s in check.sub_checks if s.verdict is Verdict.ABSTAIN)
        self._writer.append(
            action="ACCEPTANCE_CHECK",
            subject=run_id,
            payload={
                "check_id": check.check_id,
                "verdict": str(check.verdict),
                "fail_count": fails,
                "abstain_count": abstains,
            },
        )

    def run_complete(
        self, *, run_id: str, overall: str, report_sha256: str, report_path: str
    ) -> None:
        """Append the ``ACCEPTANCE_RUN_COMPLETE`` line with the report digest."""
        self._writer.append(
            action="ACCEPTANCE_RUN_COMPLETE",
            subject=run_id,
            payload={
                "overall": overall,
                "report_sha256": report_sha256,
                "report_path": report_path,
            },
        )

    def close(self) -> None:
        """Close the underlying writer (idempotent — safe on every exit path)."""
        if not self._closed:
            self._closed = True
            self._writer.close()


# ────────────────────────────────────────────────────────────────────────
# File writers
# ────────────────────────────────────────────────────────────────────────


class ReportWriteError(RuntimeError):
    """The report failed schema self-validation or could not be written (⇒ SUSPECT)."""


def write_report_files(
    report: dict[str, Any],
    *,
    lake_root: Path,
    out_dir: Path | None = None,
) -> tuple[Path, str]:
    """Validate + atomically write report.json/report.txt/evidence/ + LATEST.

    Returns ``(report_json_path, sha256_of_report_bytes)``. Raises
    :class:`ReportWriteError` on schema violation (the run must go SUSPECT —
    a malformed report never ships).

    ``--out`` semantics (round-3 fix C-F3): a redirected report is
    OFF-CHAIN — ``<lake>/acceptance/LATEST`` is NOT touched, so the
    deltas/streak chain keeps pointing at the last DEFAULT-location report
    (previously LATEST was restamped to a run id whose report dir did not
    exist under the lake: the next default run's ``load_previous`` hit an
    OSError and silently reset every streak/delta, and an operator following
    LATEST found nothing).
    """
    errors = validate_report_dict(report)
    if errors:
        raise ReportWriteError(f"report failed schema self-validation: {errors[:5]}")
    if out_dir is None and acceptance_dir(lake_root).is_symlink():
        # House .quarantine precedent (round 2 NIT): never write the evidence
        # tree through an operator-plantable link — refuse loudly (SUSPECT).
        raise ReportWriteError(
            f"{acceptance_dir(lake_root)} is a symlink — refusing to write through it"
        )
    run_dir = (out_dir or acceptance_dir(lake_root) / str(report["run_id"])).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, indent=1, sort_keys=True, allow_nan=False)
    report_path = run_dir / "report.json"
    atomic_write_simple(report_path, lambda tmp: tmp.write_text(payload, encoding="utf-8"))
    atomic_write_simple(
        run_dir / "report.txt",
        lambda tmp: tmp.write_text(render_human(report), encoding="utf-8"),
    )
    evidence_dir = run_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    for check in report["checks"]:
        spill = json.dumps(check, indent=1, sort_keys=True, allow_nan=False)

        def _write_spill(tmp: Path, payload: str = spill) -> None:
            tmp.write_text(payload, encoding="utf-8")

        atomic_write_simple(evidence_dir / f"{check['check_id']}.json", _write_spill)
    if out_dir is None:
        # LATEST tracks the DEFAULT <lake>/acceptance chain ONLY — an --out
        # report never dangles the pointer / resets streaks (C-F3).
        base = acceptance_dir(lake_root)
        base.mkdir(parents=True, exist_ok=True)
        atomic_write_simple(
            base / "LATEST",
            lambda tmp: tmp.write_text(str(report["run_id"]) + "\n", encoding="utf-8"),
        )
    sha = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    _LOG.info("acceptance.report_written", path=str(report_path), sha256=sha)
    return report_path, sha
