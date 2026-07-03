"""``brutex doctor`` — health-check command (DEC-DOCTOR-001).

Closes STANDARDS_100PCT.md §1 row 16 — the "audit JSONL hash-chain"
guarantee from Golden Rule #8 was previously aspirational because no
CLI command verified the chain on demand. This module provides one.

Surface
-------
``brutex doctor [--audit-dir PATH] [--audit-file PATH]``

Behaviour
~~~~~~~~~
Runs all available health checks and prints a PASS/FAIL summary,
returning exit code 0 on all green or 1 on any red. Designed to be
wired into a nightly cron — the daily 07:30 IST observability
contract from STANDARDS_100PCT.md §2.

Currently implemented checks
----------------------------
1. **Audit chain integrity** (NFR-OBS-003). Verifies the
   ``entry_hash`` chain on the most recent JSONL file in the audit
   directory (default ``~/.brutex/audit/``). Re-runs
   :func:`brutex.orchestrator.audit.verify_chain` and surfaces any
   :exc:`AuditChainCorruption` as a structured failure.
2. **Runtime file presence** (FR-UI-002). Reads
   ``~/.brutex/runtime.json`` if present and confirms it parses.
   Absent file is reported as ``SKIP`` (no live run is a valid
   state, not a failure).

Future checks (tracked for follow-up PRs, NOT in this PR)
---------------------------------------------------------
* Lake directory health — Parquet manifest + DuckDB catalog.
* Rate-limit budget — quota tracker / Bloom dedup integrity.
* Hardware envelope — disk space, RAM headroom.

Honest envelope (Golden Rule #10)
---------------------------------
This is the **minimum viable** doctor — one chain check + one
runtime check. It satisfies the "row 16 — CLI missing" gap from
DEC-STANDARDS-AUDIT-001 (entry #205). Broader health-check
coverage is a ratchet on top of this scaffold.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from brutex.orchestrator.audit import AuditChainCorruption, verify_chain

# ────────────────────────────────────────────────────────────────────────
# Defaults
# ────────────────────────────────────────────────────────────────────────

DEFAULT_BRUTEX_HOME = Path.home() / ".brutex"
DEFAULT_AUDIT_DIR = DEFAULT_BRUTEX_HOME / "audit"
DEFAULT_RUNTIME_FILE = DEFAULT_BRUTEX_HOME / "runtime.json"


# ────────────────────────────────────────────────────────────────────────
# Report dataclasses
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CheckResult:
    """One health-check outcome.

    Attributes
    ----------
    name
        Short identifier (e.g., ``"audit_chain"``, ``"runtime_file"``).
    status
        One of ``"PASS"``, ``"FAIL"``, ``"SKIP"``.
    detail
        Human-readable one-line summary.
    """

    name: str
    status: str
    detail: str


@dataclass
class DoctorReport:
    """Aggregate result of all checks.

    ``ok`` is ``True`` iff no check returned ``FAIL``. ``SKIP`` is
    not a failure (e.g. no runtime file = no live run, valid state).
    """

    checks: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True if no FAIL results are present."""
        return all(c.status != "FAIL" for c in self.checks)

    def add(self, result: CheckResult) -> None:
        """Append a result to the report."""
        self.checks.append(result)

    def format(self) -> str:
        """Format the report as human-readable text."""
        # Status → display glyph. bandit B105 false-positives "✅" as a
        # "hardcoded password" because the dict literal looks like
        # `KEY: "value"`. These are UI characters for the doctor report;
        # they are not credentials. Same pattern as the 4 other documented
        # bandit exceptions in brutex/ (DEC-P0-001 nosec convention).
        status_glyph = {  # nosec B105 — status → display glyph mapping
            "PASS": "✅",
            "FAIL": "❌",
            "SKIP": "⏸",
        }
        lines = ["brutex doctor — health check report", "=" * 40]
        for c in self.checks:
            emoji = status_glyph.get(c.status, "?")
            lines.append(f"{emoji} {c.name:20s} {c.status:5s} {c.detail}")
        lines.append("=" * 40)
        lines.append("OVERALL: " + ("✅ PASS" if self.ok else "❌ FAIL"))
        return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────
# Individual checks
# ────────────────────────────────────────────────────────────────────────


def _find_latest_audit_log(audit_dir: Path) -> Path | None:
    """Return the most-recently-modified ``*.jsonl`` in ``audit_dir``.

    Returns ``None`` if the directory doesn't exist or has no
    ``*.jsonl`` files.
    """
    if not audit_dir.is_dir():
        return None
    candidates = sorted(audit_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return candidates[-1] if candidates else None


def check_audit_chain(audit_file: Path | None, audit_dir: Path) -> CheckResult:
    """Verify a specific audit file, or the latest in ``audit_dir``.

    * If ``audit_file`` is given, verify it and return.
    * Else find the most recent ``*.jsonl`` in ``audit_dir``.
    * If no audit log is found, return ``SKIP`` — a freshly
      installed BruteX with no ingest runs is a valid state.
    """
    target = audit_file or _find_latest_audit_log(audit_dir)
    if target is None:
        return CheckResult(
            name="audit_chain",
            status="SKIP",
            detail=f"no audit logs found in {audit_dir}",
        )
    if not target.exists():
        return CheckResult(
            name="audit_chain",
            status="FAIL",
            detail=f"audit file does not exist: {target}",
        )
    try:
        count = verify_chain(target)
    except AuditChainCorruption as exc:
        return CheckResult(
            name="audit_chain",
            status="FAIL",
            detail=f"chain corrupt in {target.name}: {exc}",
        )
    return CheckResult(
        name="audit_chain",
        status="PASS",
        detail=f"{count} entries verified in {target.name}",
    )


def check_audit_replay_consistency(
    audit_file: Path | None,
    audit_dir: Path,
    live_subjects_file: Path | None,
    action_filter: str = "add",
) -> CheckResult:
    """L3 audit-log replay oracle check (DEC-L3-REPLAY-ORACLE-001).

    Verifies the audit log's ``add``-action subjects exactly match the
    live dedup-snapshot the operator provides via ``live_subjects_file``
    (a newline-separated text file of subject strings, one per line).

    * If ``live_subjects_file`` is ``None`` (operator opted out),
      return SKIP. This is the default — replay-oracle is an opt-in
      check because not every deployment maintains a live-subjects
      snapshot.
    * If the file is given but the audit log is missing, return SKIP
      (mirrors ``check_audit_chain``'s freshly-installed semantics).
    * If both are present, call
      :func:`brutex.orchestrator.replay.assert_dedup_consistent`. On
      ``AuditError`` (missing-from-live or extra-in-live), return FAIL
      with the truncated diff. On clean match, return PASS.

    Parameters
    ----------
    audit_file
        Specific audit file. ``None`` = latest in ``audit_dir``.
    audit_dir
        Audit log directory (used when ``audit_file`` is ``None``).
    live_subjects_file
        Newline-separated text file of live-dedup subjects. ``None``
        opts the check out.
    action_filter
        Audit action to project subjects from. Default ``"add"``.
    """
    if live_subjects_file is None:
        return CheckResult(
            name="audit_replay",
            status="SKIP",
            detail="no live_subjects_file given (replay check is opt-in)",
        )
    if not live_subjects_file.exists():
        return CheckResult(
            name="audit_replay",
            status="FAIL",
            detail=f"live_subjects_file does not exist: {live_subjects_file}",
        )

    target = audit_file or _find_latest_audit_log(audit_dir)
    if target is None:
        return CheckResult(
            name="audit_replay",
            status="SKIP",
            detail=f"no audit logs found in {audit_dir}",
        )
    if not target.exists():
        return CheckResult(
            name="audit_replay",
            status="FAIL",
            detail=f"audit file does not exist: {target}",
        )

    # Local import keeps the doctor module's top-of-file import set lean.
    from brutex.orchestrator.audit import AuditError
    from brutex.orchestrator.replay import assert_dedup_consistent

    live_subjects = frozenset(
        line
        for raw in live_subjects_file.read_text(encoding="utf-8").splitlines()
        if (line := raw.strip())
    )
    try:
        assert_dedup_consistent(
            audit_path=target,
            live_subjects=live_subjects,
            action_filter=action_filter,
        )
    except AuditError as exc:
        return CheckResult(
            name="audit_replay",
            status="FAIL",
            detail=str(exc),
        )
    return CheckResult(
        name="audit_replay",
        status="PASS",
        detail=(
            f"{len(live_subjects)} live subjects match audit "
            f"({target.name}, action={action_filter!r})"
        ),
    )


def check_runtime_file(runtime_file: Path) -> CheckResult:
    """Verify the runtime file parses, if present.

    Absent file = no live run = SKIP (not a failure).
    Present but malformed = FAIL.
    """
    if not runtime_file.exists():
        return CheckResult(
            name="runtime_file",
            status="SKIP",
            detail=f"no runtime file at {runtime_file} (no live run)",
        )
    try:
        data = json.loads(runtime_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return CheckResult(
            name="runtime_file",
            status="FAIL",
            detail=f"runtime file malformed JSON: {exc}",
        )
    if not isinstance(data, dict):
        return CheckResult(
            name="runtime_file",
            status="FAIL",
            detail=f"runtime file is not a JSON object: got {type(data).__name__}",
        )
    return CheckResult(
        name="runtime_file",
        status="PASS",
        detail=f"runtime file parses; {len(data)} keys present",
    )


# ────────────────────────────────────────────────────────────────────────
# Orchestrator
# ────────────────────────────────────────────────────────────────────────


def check_derived_freshness(lake_root: Path) -> CheckResult:
    """Verify stored derived-TF tables still equal a fresh re-derivation.

    The DEC-TF-MATERIALIZE-002 freshness probe: for the FIRST symbol that
    has both a stored ``1minute`` partition and at least one stored derived
    timeframe partition, re-derive each stored timeframe from the 1-minute
    base via the canonical session-anchored resampler and compare
    frame-for-frame (:func:`brutex.data.tf_autoderive.compare_derived_freshness`).
    Any drift — a stale table left behind by a 1m backfill, or a
    pre-DEC-RESAMPLE-ANCHOR-002 gapped-day grid — is a loud ``FAIL``.

    SKIP (a valid state, not a failure) when: the lake root is absent, no
    symbol has derived partitions yet (the auto-derive opt-in is OFF), or
    polars is unavailable.
    """
    bars_root = lake_root / "bars"
    if not bars_root.is_dir():
        return CheckResult(
            name="derived_freshness",
            status="SKIP",
            detail=f"no lake at {lake_root} — nothing to verify",
        )
    try:
        import polars as pl

        from brutex.data.tf_autoderive import compare_derived_freshness
        from brutex.data.timeframe_materialize import INTRADAY_TF_INTERVALS
        from brutex.data.timeframes import parse_label

        interval_to_label = {v: k for k, v in INTRADAY_TF_INTERVALS.items()}

        def _read(symbol_dir: Path, interval: str) -> pl.DataFrame:
            return (
                pl.read_parquet((symbol_dir / interval).as_posix() + "/**/*.parquet")
                .rename({"timestamp": "ts_utc"})
                .select("ts_utc", "open", "high", "low", "close", "volume")
                .sort("ts_utc")
            )

        for symbol_dir in sorted(p for p in bars_root.glob("*/*/*") if p.is_dir()):
            base = symbol_dir / "1minute"
            if not (base.is_dir() and any(base.rglob("*.parquet"))):
                continue
            stored_intervals = sorted(
                d.name
                for d in symbol_dir.iterdir()
                if d.is_dir() and d.name in interval_to_label and any(d.rglob("*.parquet"))
            )
            if not stored_intervals:
                continue
            df_1m = _read(symbol_dir, "1minute")
            stale: list[str] = []
            for interval in stored_intervals:
                tf_minutes = parse_label(interval_to_label[interval])
                verdict = compare_derived_freshness(df_1m, _read(symbol_dir, interval), tf_minutes)
                if verdict != "fresh":
                    stale.append(interval)
            symbol = symbol_dir.name
            if stale:
                return CheckResult(
                    name="derived_freshness",
                    status="FAIL",
                    detail=(
                        f"{symbol}: stored derived TFs drift from a fresh re-derivation: "
                        f"{stale} — re-derive via POST /api/v1/universe/derive-timeframes"
                    ),
                )
            return CheckResult(
                name="derived_freshness",
                status="PASS",
                detail=(
                    f"{symbol}: {len(stored_intervals)} stored derived TFs byte-equal "
                    "a fresh re-derivation"
                ),
            )
        return CheckResult(
            name="derived_freshness",
            status="SKIP",
            detail="no symbol has stored derived timeframes yet (auto-derive opt-in OFF)",
        )
    except Exception as exc:
        return CheckResult(
            name="derived_freshness",
            status="FAIL",
            detail=f"freshness probe errored: {exc}",
        )


def _lake_offender(finding: Any) -> str:
    """Name one lake-scanner finding for the FAIL detail.

    Dup / foreign findings carry ``coords`` — named by their lake-relative
    partition path; any other finding shape degrades to ``repr`` (never a
    crash inside the doctor's naming loop).
    """
    coords = getattr(finding, "coords", None)
    if coords is not None:
        return str(coords.relpath())
    return repr(finding)


def check_lake_uniqueness(lake_root: Path) -> CheckResult:
    """Verify the ENGINE-index lake series (NIFTY/BANKNIFTY/SENSEX) are duplicate-free.

    The DEC-LAKE-HEAL-001 doctor check: runs the bounded invariant scanner
    (:func:`brutex.data.lake_invariants.scan_lake_invariants`) over ONLY the
    three :data:`~brutex.data.lake_invariants.ENGINE_INDEX_COORDS` series (all
    stored intervals) — the question this check answers is "does the ENGINE's
    index data carry any duplicate / misplaced rows". The whole-lake census is
    ``brutex lake audit-unique``'s job: an O(all-files) doctor check on a
    100k-file lake would be dishonestly slow (Golden Rule #10). PASS detail
    carries the last full L3 startup-sweep sentinel when one exists; FAIL
    names up to 3 offending partitions plus the remediation pointer. SKIP (a
    valid state, not a failure) when no lake exists yet. Whole body guarded —
    a probe error is a loud FAIL, never a doctor crash.
    """
    bars_root = lake_root / "bars"
    if not bars_root.is_dir():
        return CheckResult(
            name="lake_uniqueness",
            status="SKIP",
            detail=f"no lake at {lake_root} — nothing to verify",
        )
    try:
        from brutex.data.lake_heal import load_last_sweep
        from brutex.data.lake_invariants import ENGINE_INDEX_COORDS, scan_lake_invariants

        offenders: list[str] = []
        files = 0
        for exchange, segment, symbol in ENGINE_INDEX_COORDS:
            report = scan_lake_invariants(
                lake_root,
                exchange=exchange,
                segment=segment,
                symbol=symbol,
                include_options=False,
            )
            files += report.files_scanned
            offenders.extend(_lake_offender(finding) for finding in report.findings)
        if offenders:
            named = ", ".join(offenders[:3])
            extra = len(offenders) - len(offenders[:3])
            suffix = f" (+{extra} more)" if extra else ""
            return CheckResult(
                name="lake_uniqueness",
                status="FAIL",
                detail=(
                    f"engine-index partitions poisoned: {named}{suffix} — heal runs "
                    "automatically (BRUTEX_LAKE_HEAL=auto) on dashboard startup / "
                    "post-pull / sweep start; run `brutex lake audit-unique --json` "
                    "for the full census; quarantined originals under "
                    f"{lake_root / '.quarantine'}"
                ),
            )
        sweep = load_last_sweep(lake_root)
        sweep_note = ""
        if sweep is not None:
            sweep_note = (
                f"; last full sweep: {sweep.get('completed_utc')}, "
                f"findings={sweep.get('findings')}, healed={sweep.get('healed')}"
            )
        return CheckResult(
            name="lake_uniqueness",
            status="PASS",
            detail=(
                "engine indexes (NIFTY/BANKNIFTY/SENSEX): 0 findings over "
                f"{files} files{sweep_note}"
            ),
        )
    except Exception as exc:
        return CheckResult(
            name="lake_uniqueness",
            status="FAIL",
            detail=f"lake uniqueness probe errored: {exc}",
        )


def run_doctor(
    audit_file: Path | None = None,
    audit_dir: Path = DEFAULT_AUDIT_DIR,
    runtime_file: Path = DEFAULT_RUNTIME_FILE,
    live_subjects_file: Path | None = None,
    replay_action_filter: str = "add",
    lake_root: Path | None = None,
) -> DoctorReport:
    """Run all health checks and return the aggregated report.

    Parameters
    ----------
    audit_file
        Specific audit file to verify. If ``None``, the most recent
        ``*.jsonl`` in ``audit_dir`` is used.
    audit_dir
        Directory to search for audit logs when ``audit_file`` is
        not supplied. Defaults to ``~/.brutex/audit``.
    runtime_file
        Path to the runtime file. Defaults to ``~/.brutex/runtime.json``.
    live_subjects_file
        Optional newline-separated text file of live dedup subjects.
        When supplied, enables the L3 audit-replay consistency check
        (DEC-L3-REPLAY-ORACLE-001). When ``None``, that check is
        SKIP-ped.
    replay_action_filter
        Audit action to project subjects from for replay (default
        ``"add"``).

    Returns
    -------
    DoctorReport
        Aggregate report. Call ``.ok`` to test overall PASS/FAIL,
        ``.format()`` to render as text.
    """
    report = DoctorReport()
    report.add(check_audit_chain(audit_file=audit_file, audit_dir=audit_dir))
    report.add(check_runtime_file(runtime_file=runtime_file))
    report.add(
        check_audit_replay_consistency(
            audit_file=audit_file,
            audit_dir=audit_dir,
            live_subjects_file=live_subjects_file,
            action_filter=replay_action_filter,
        )
    )
    from brutex.config.paths import resolve_lake_root

    resolved_lake = lake_root if lake_root is not None else resolve_lake_root()
    report.add(check_derived_freshness(resolved_lake))
    report.add(check_lake_uniqueness(resolved_lake))
    return report


def main(
    audit_file: Path | None = None,
    audit_dir: Path = DEFAULT_AUDIT_DIR,
    runtime_file: Path = DEFAULT_RUNTIME_FILE,
    live_subjects_file: Path | None = None,
    replay_action_filter: str = "add",
    lake_root: Path | None = None,
) -> int:
    """CLI entry point. Print report to stdout; return 0 on PASS, 1 on FAIL."""
    report = run_doctor(
        audit_file=audit_file,
        audit_dir=audit_dir,
        runtime_file=runtime_file,
        live_subjects_file=live_subjects_file,
        replay_action_filter=replay_action_filter,
        lake_root=lake_root,
    )
    print(report.format())
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover — invoked only by CLI
    sys.exit(main())
