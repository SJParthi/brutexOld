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
3. **Wheel-source freshness** (R2-CC-G1 for ``trading_core``;
   DEC-FNOMATH-WHEEL-FRESHNESS-001 extends the SAME common check to
   the ``fno_math`` trade-sim wheel, urgent since
   DEC-SWEEP-RUST-TRADESIM-WIRE-001 #1182 wired ``ResidentSweepSim``
   unflagged into the sweep hot path). ONE generic helper
   (:func:`_check_one_wheel_freshness` over :class:`_WheelSpec`)
   compares each INSTALLED wheel (compiled-symbol surface +
   ``__build_crate_version__``) against its in-repo expected
   fingerprint (``rust/<crate>/python/<package>/EXPECTED_BUILD.json``).
   A mismatch is a non-fatal ``WARN`` with an actionable rebuild
   message; ``BRUTEX_SKIP_WHEEL_FRESHNESS=1`` skips BOTH wheel stages
   ONLY (one escape hatch, deliberately shared).
   HONEST reachability envelope (Golden Rule #10): via the ``brutex``
   CLI these stages' WARN teeth are the drift classes that still
   IMPORT (crate-version mismatch, unexpected extra symbols, a
   stale/malformed fingerprint). A wheel missing a re-exported
   SYMBOL never reaches these stages through the CLI — each shim's
   UNCONDITIONAL import-time handshake (no env escape; see the
   respective ``__init__.py`` for why one could not help) raises its
   own actionable ImportError first, because ``brutex.cli``
   transitively imports both wheels at module import
   (``brutex.vocabulary`` → ``trading_core``; ``brutex.math`` →
   ``fno_math``). That ImportError IS the operator remedy for the
   symbol-stale class; the "not importable" WARN arm is reachable
   only from programmatic ``run_doctor`` embeddings.
4. **Wheel source digest** (DEC-RUST-SOURCE-DIGEST-TWIN-001 — closes the
   "same-version, symbol-compatible behavioural drift" residual gap the
   freshness stage names). ONE generic sibling helper
   (:func:`_check_one_wheel_source_digest` over the SAME
   :class:`_WheelSpec`) compares each installed wheel's build.rs-baked
   ``__source_digest__`` against a fresh Python-twin recompute
   (:func:`brutex.wheel_digest.crate_source_digest`) over the repo
   checkout's ``rust/<crate>`` tree. Mismatch / absent stamp = non-fatal
   ``WARN`` (consistent with the freshness stage);
   ``BRUTEX_SKIP_WHEEL_FRESHNESS=1`` skips these stages too (still ONE
   escape hatch for all wheel stages). HONEST scope (Golden Rule #10):
   LOCAL machines only — CI builds every wheel from source per job
   (no committed wheel exists anywhere), so CI can never see a stale
   wheel; the exposure is a developer wheel built from older source at
   the same crate version. No repo checkout next to the installed
   package → honest ``SKIP`` (not checkable).

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

import importlib
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from brutex.orchestrator.audit import AuditChainCorruption, verify_chain
from brutex.wheel_digest import SourceDigestError, crate_source_digest

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
        One of ``"PASS"``, ``"FAIL"``, ``"SKIP"``, ``"WARN"``.
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
    not a failure (e.g. no runtime file = no live run, valid state)
    and neither is ``WARN`` (advisory — e.g. a stale trading_core
    wheel is surfaced loudly but never blocks the doctor run).
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
            "WARN": "⚠️",
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
    A LEGACY derived partition materialised from UNSCREENED 1-minute reads
    ``"stale"`` → ``FAIL`` too, because the re-derive base is now bad-tick
    screened (DEC-BADTICK-RESAMPLE-INPUT-001) — the intended migration
    tripwire, not a probe defect. SPOT + FNO ``FUT`` classes only (fix
    round 2): an FNO ``CE``/``PE`` contract's re-derive base is deliberately
    NOT screened (`resample_screen_applies` — the index-calibrated 4.0x bound
    is unjustified for option premiums), mirroring its unscreened
    materialisation, so an options dir reached by this walk can never
    false-FAIL the check on a legit ``> 4x`` premium move.

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

        from brutex.data.tf_autoderive import compare_derived_freshness, read_lake_bars_frame
        from brutex.data.timeframe_materialize import INTRADAY_TF_INTERVALS
        from brutex.data.timeframes import parse_label

        interval_to_label = {v: k for k, v in INTRADAY_TF_INTERVALS.items()}

        def _read(symbol_dir: Path, interval: str) -> pl.DataFrame:
            # ONE COMMON reader with the sweep-preflight freshness gate
            # (DEC-SUSPICION-SWEEP-FIXES-001 G1) — keep-FIRST read dedup BEFORE
            # the sort (DEC-LAKE-READ-UNIQUE-001), doctor-labelled dedup source.
            return read_lake_bars_frame(
                symbol_dir, interval, source=f"doctor:{symbol_dir.name}/{interval}"
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
                        f"{stale} — re-derive via the pipeline's automatic prebuild "
                        "stage (▶ Pull · full pipeline, DEC-PIPELINE-AUTOPREBUILD-001)"
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
        skipped = 0
        for exchange, segment, symbol in ENGINE_INDEX_COORDS:
            report = scan_lake_invariants(
                lake_root,
                exchange=exchange,
                segment=segment,
                symbol=symbol,
                include_options=False,
            )
            files += report.files_scanned
            skipped += report.files_skipped
            offenders.extend(_lake_offender(finding) for finding in report.findings)
        if offenders:
            named = ", ".join(offenders[:3])
            extra = len(offenders) - len(offenders[:3])
            suffix = f" (+{extra} more)" if extra else ""
            # DEC-AUDIT-CENSUS-HONESTY-001: name any UNREADABLE engine file too —
            # a poisoned lake that ALSO has skips is still incompletely audited.
            skip_note = f"; {skipped} engine-index file(s) UNREADABLE (skipped)" if skipped else ""
            return CheckResult(
                name="lake_uniqueness",
                status="FAIL",
                detail=(
                    f"engine-index partitions poisoned: {named}{suffix}{skip_note} — heal "
                    "runs automatically (BRUTEX_LAKE_HEAL=auto) on dashboard startup / "
                    "post-pull / sweep start; run `brutex lake audit-unique --json` "
                    "for the full census; quarantined originals under "
                    f"{lake_root / '.quarantine'}"
                ),
            )
        if skipped:
            # No classifiable findings, but some engine-index file was UNREADABLE
            # (garbage bytes / schema drift) so the scan could not certify it
            # clean — a FAIL, never a silent PASS over corruption
            # (DEC-AUDIT-CENSUS-HONESTY-001).
            return CheckResult(
                name="lake_uniqueness",
                status="FAIL",
                detail=(
                    f"engine-index census INCOMPLETE: {skipped} file(s) UNREADABLE "
                    "(garbage bytes / schema drift) — cannot certify clean; run "
                    "`brutex lake audit-unique --json` for the full census + "
                    f"re-pull or quarantine the offending partition(s) under {lake_root}"
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


@dataclass(frozen=True)
class _WheelSpec:
    """One compiled-wheel freshness target for :func:`_check_one_wheel_freshness`.

    The COMMON descriptor (Golden Rule #11) that lets ONE generic check body
    serve every PyO3 wheel BruteX ships — ``trading_core`` (R2-CC-G1) and
    ``fno_math`` (DEC-FNOMATH-WHEEL-FRESHNESS-001) today; a future kernel is
    one more spec, zero copy-paste.

    Attributes
    ----------
    check_name
        The :class:`CheckResult` stage name (``"wheel_freshness"`` for
        trading_core is preserved verbatim for back-compat with existing
        consumers/tests; the fno_math stage is ``"wheel_freshness_fno_math"``).
    package
        Importable shim package name (e.g. ``"trading_core"``).
    compiled_submodule
        The private compiled submodule name inside the package (e.g.
        ``"_trading_core"``) — the installed ``.so`` this stage fingerprints.
    crate_dir
        Repo-relative crate directory for the actionable rebuild hint
        (e.g. ``"rust/trading-core"``).
    """

    check_name: str
    package: str
    compiled_submodule: str
    crate_dir: str


_TRADING_CORE_WHEEL = _WheelSpec(
    check_name="wheel_freshness",
    package="trading_core",
    compiled_submodule="_trading_core",
    crate_dir="rust/trading-core",
)

_FNO_MATH_WHEEL = _WheelSpec(
    check_name="wheel_freshness_fno_math",
    package="fno_math",
    compiled_submodule="_fno_math",
    crate_dir="rust/fno-math",
)


def _check_one_wheel_freshness(
    spec: _WheelSpec,
    fingerprint_file: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> CheckResult:
    """Wheel-source freshness handshake for ONE compiled wheel.

    Run identity records ``code_commit_sha`` + the wheel's ``__version__``,
    but nothing bound the installed ``.so`` to the source tree: a run could
    record commit X while executing commit-W Rust kernels, silently. This
    stage compares the INSTALLED package (compiled-symbol surface +
    ``__build_crate_version__``) against the in-repo expected fingerprint
    (``EXPECTED_BUILD.json`` next to the installed package, which IS the
    repo copy under ``maturin develop``).

    * ``BRUTEX_SKIP_WHEEL_FRESHNESS=1`` in ``env`` → SKIP. This skips the
      DOCTOR wheel stages ONLY — it is NOT an escape for a shim's
      import-time symbol handshake, and it cannot unbrick a symbol-stale
      install (nothing can, short of the rebuild: the shim's re-export
      from-import would fail on the missing symbol regardless).
    * Package not importable (or its own import-time symbol handshake
      raised) → WARN with the rebuild instruction. HONEST reachability
      (Golden Rule #10): this arm is reachable ONLY from a programmatic
      :func:`run_doctor` / check call (tests, embeddings). Through the
      ``brutex`` CLI it can never execute — ``brutex.cli`` transitively
      imports both wheels at module import (``brutex.vocabulary`` →
      ``trading_core``; ``brutex.math`` → ``fno_math``), so a symbol-stale
      wheel kills the process with the shim's own actionable ImportError
      before any doctor stage runs. That ImportError names the missing
      symbols + the same rebuild command, so the operator remedy is
      identical — but it is a hard import failure, NOT this advisory WARN.
    * No fingerprint file (e.g. a wheel-only install with no repo checkout)
      → SKIP with an honest "not checkable" detail.
    * Version or symbol-set mismatch → **WARN (non-fatal)** with an
      actionable ``maturin develop --release`` message. These are the drift
      classes with real CLI reach: they import fine (the shim handshake
      checks symbol PRESENCE only), so ``brutex doctor`` genuinely gets here
      and warns.
    * Match → PASS.

    HONEST residual gap (Golden Rule #10): version + symbol-set checks catch
    a stale wheel that dropped/added a symbol or changed crate version. They
    do NOT catch same-version, symbol-compatible BEHAVIORAL drift (Rust
    source changed, version + symbols unchanged) — THAT class is now watched
    by the SIBLING :func:`_check_one_wheel_source_digest` stage
    (DEC-RUST-SOURCE-DIGEST-TWIN-001: the build.rs-baked source digest vs the
    Python-twin recompute over the repo crate tree), which closed the
    follow-up previously named here; the CI golden/parity suites remain the
    behavioral correctness proof. Likewise, a NON-editable wheel install whose
    default fingerprint ships inside the wheel itself self-snapshots at
    build time; the check has teeth when the fingerprint read is the repo
    checkout's (the editable/`maturin develop` layout).
    """
    env_map: Mapping[str, str] = os.environ if env is None else env
    if env_map.get("BRUTEX_SKIP_WHEEL_FRESHNESS") == "1":
        return CheckResult(
            name=spec.check_name,
            status="SKIP",
            detail="skipped via BRUTEX_SKIP_WHEEL_FRESHNESS=1",
        )

    rebuild_hint = f"rebuild via `maturin develop --release` in {spec.crate_dir}"
    try:
        package = importlib.import_module(spec.package)
        # Friend access to the compiled submodule: the whole point of this
        # stage is fingerprinting the installed .so itself, not the shim.
        # Dynamic import (ONE style — CodeQL py/import-and-import-from) still
        # raises ImportError on a missing/stale .so, so the WARN arm below is
        # behavior-identical for both wheels.
        compiled = importlib.import_module(f"{spec.package}.{spec.compiled_submodule}")
    except ImportError as exc:
        return CheckResult(
            name=spec.check_name,
            status="WARN",
            detail=f"{spec.package} wheel not importable ({exc}) — {rebuild_hint}",
        )

    target = fingerprint_file
    if target is None:
        target = Path(str(package.__file__)).parent / "EXPECTED_BUILD.json"
    if not target.exists():
        return CheckResult(
            name=spec.check_name,
            status="SKIP",
            detail=f"no expected-fingerprint file at {target} — freshness not checkable",
        )

    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        expected_version = payload["crate_version"]
        expected_symbol_list = payload["symbols"]
        if not isinstance(expected_version, str) or not isinstance(expected_symbol_list, list):
            raise TypeError("crate_version must be a str and symbols a list")
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        return CheckResult(
            name=spec.check_name,
            status="WARN",
            detail=f"fingerprint file malformed ({target.name}): {exc}",
        )

    expected_symbols = set(expected_symbol_list)
    installed_symbols = {a for a in dir(compiled) if not a.startswith("_") or a == "__version__"}
    installed_version = getattr(
        package, "__build_crate_version__", "<absent — shim predates the handshake>"
    )

    problems: list[str] = []
    if installed_version != expected_version:
        problems.append(f"crate version {installed_version} != expected {expected_version}")
    missing = sorted(expected_symbols - installed_symbols)
    if missing:
        problems.append(f"installed .so is missing symbol(s) {missing}")
    unexpected = sorted(installed_symbols - expected_symbols)
    if unexpected:
        problems.append(f"installed .so exposes unexpected symbol(s) {unexpected}")
    if problems:
        return CheckResult(
            name=spec.check_name,
            status="WARN",
            detail=(
                f"installed {spec.package} wheel is STALE vs {target.name}: "
                f"{'; '.join(problems)} — {rebuild_hint}"
            ),
        )
    # HONEST residual gap (Golden Rule #10): a PASS here means version +
    # symbol surface match — it does NOT prove behavioral freshness of a
    # same-version, symbol-compatible rebuild (that class is the sibling
    # wheel_source_digest stage's job — DEC-RUST-SOURCE-DIGEST-TWIN-001).
    return CheckResult(
        name=spec.check_name,
        status="PASS",
        detail=(
            f"installed {spec.package} {installed_version} matches {target.name} "
            f"({len(expected_symbols)} symbols; version+symbol check only — "
            "same-version behavioral drift not detectable)"
        ),
    )


def check_wheel_freshness(
    fingerprint_file: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> CheckResult:
    """Wheel-source freshness handshake for the trading_core wheel (R2-CC-G1).

    Thin binding of the COMMON :func:`_check_one_wheel_freshness` to the
    ``trading_core`` wheel — the stage name (``wheel_freshness``), semantics
    and detail wording are preserved from the pre-generalization check.

    Parameters
    ----------
    fingerprint_file
        Explicit path to the expected-fingerprint JSON. ``None`` = the
        ``EXPECTED_BUILD.json`` next to the installed ``trading_core``
        package (which IS the repo copy under ``maturin develop``).
    env
        Environment mapping (injected for tests). ``None`` = ``os.environ``.
    """
    return _check_one_wheel_freshness(_TRADING_CORE_WHEEL, fingerprint_file, env)


def check_fno_math_wheel_freshness(
    fingerprint_file: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> CheckResult:
    """Wheel-source freshness handshake for the fno_math trade-sim wheel.

    DEC-FNOMATH-WHEEL-FRESHNESS-001 — the fno_math twin of
    :func:`check_wheel_freshness`, bound to the SAME common helper. Urgent
    because DEC-SWEEP-RUST-TRADESIM-WIRE-001 (#1182) wired
    ``ResidentSweepSim`` UNFLAGGED into the sweep hot path, so a stale
    installed ``_fno_math`` wheel is an always-on production risk (this very
    container shipped a stale wheel that SIGABRTed suites). Runtime
    stale-wheel tripwire only — the CI golden/parity suites remain the
    correctness proof.

    Parameters
    ----------
    fingerprint_file
        Explicit path to the expected-fingerprint JSON. ``None`` = the
        ``EXPECTED_BUILD.json`` next to the installed ``fno_math`` package
        (which IS the repo copy under ``maturin develop``).
    env
        Environment mapping (injected for tests). ``None`` = ``os.environ``.
    """
    return _check_one_wheel_freshness(_FNO_MATH_WHEEL, fingerprint_file, env)


def _default_crate_root(spec: _WheelSpec) -> Path:
    """Repo-checkout crate directory for ``spec`` (``rust/<crate>``).

    Resolved relative to the ``brutex`` package location — under a repo
    checkout / editable install that is ``<repo>/rust/<crate>``; under a
    pure site-packages install the path does not exist and the digest stage
    reports an honest SKIP (not checkable).
    """
    return Path(__file__).resolve().parents[1] / spec.crate_dir


def _check_one_wheel_source_digest(
    spec: _WheelSpec,
    crate_root: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> CheckResult:
    """Wheel source-content digest handshake for ONE compiled wheel.

    DEC-RUST-SOURCE-DIGEST-TWIN-001 — the sibling of
    :func:`_check_one_wheel_freshness` that closes its named residual gap:
    a wheel built from OLDER source at the SAME crate version with the SAME
    symbol surface (behavioural drift the version+symbol check cannot see).
    ``build.rs`` bakes a BLAKE3 digest of the crate source (``Cargo.toml`` +
    ``src/**/*.rs``, FRAMING v1 — ``rust/source_digest_core.rs``) into the
    compiled module as ``__source_digest__``; this stage recomputes the same
    digest over the repo checkout with the byte-for-byte Python twin
    (:func:`brutex.wheel_digest.crate_source_digest`) and compares.

    * ``BRUTEX_SKIP_WHEEL_FRESHNESS=1`` in ``env`` → SKIP (the ONE escape
      hatch shared by every wheel stage, deliberately).
    * Compiled module not importable → WARN with the rebuild instruction
      (same reachability envelope as the freshness stage: through the
      ``brutex`` CLI a broken wheel import kills the process at
      ``import brutex.cli`` first; this arm serves programmatic embeddings).
    * Installed module lacks a usable ``__source_digest__`` (an older wheel
      predating the handshake) → **WARN (non-fatal)** with the rebuild hint.
    * No crate source tree next to the installed ``brutex`` package (a
      wheel-only install with no repo checkout) → SKIP, honestly not
      checkable.
    * Twin recompute fails (unreadable tree / non-UTF-8 name / a
      symlink-cycle ``RecursionError``) → WARN.
    * Digest mismatch → **WARN (non-fatal)** naming BOTH hex digests + the
      actionable ``maturin develop --release`` rebuild hint (consistent with
      the freshness stage's WARN semantics — an operator observation, never
      a hard stop).
    * Match → PASS.

    HONEST scope (Golden Rule #10): LOCAL machines only. CI builds every
    wheel from source in-job (there is NO committed wheel anywhere and no
    other top-level build.rs consumer), so CI can never observe a stale
    wheel — this stage has no CI teeth and claims none. The digest INPUT is
    read from the checkout by pure Python and hashed with the ``blake3``
    PyPI package (a BruteX base dependency), so a stale wheel cannot
    influence its own expected value. ONE NAMED residual: FRAMING v1
    excludes the crate's ``python/`` shim, yet maturin's mixed layout
    packages ``rust/<crate>/python/<pkg>/__init__.py`` INTO the wheel — so
    a local wheel stale ONLY in the shim escapes all four wheel stages
    (this digest covers the compiled kernel's source; the freshness stages
    fingerprint the compiled module's version+symbols, not shim content).
    Folding the shim into a future FRAMING v2 is the named follow-up.
    """
    env_map: Mapping[str, str] = os.environ if env is None else env
    if env_map.get("BRUTEX_SKIP_WHEEL_FRESHNESS") == "1":
        return CheckResult(
            name=spec.check_name,
            status="SKIP",
            detail="skipped via BRUTEX_SKIP_WHEEL_FRESHNESS=1",
        )

    rebuild_hint = f"rebuild via `maturin develop --release` in {spec.crate_dir}"
    try:
        compiled = importlib.import_module(f"{spec.package}.{spec.compiled_submodule}")
    except ImportError as exc:
        return CheckResult(
            name=spec.check_name,
            status="WARN",
            detail=f"{spec.package} wheel not importable ({exc}) — {rebuild_hint}",
        )

    installed_digest = getattr(compiled, "__source_digest__", None)
    if not isinstance(installed_digest, str):
        return CheckResult(
            name=spec.check_name,
            status="WARN",
            detail=(
                f"installed {spec.package} wheel carries no usable __source_digest__ "
                "(built before the source-digest handshake — "
                f"DEC-RUST-SOURCE-DIGEST-TWIN-001) — {rebuild_hint}"
            ),
        )

    root = crate_root if crate_root is not None else _default_crate_root(spec)
    if not (root / "Cargo.toml").is_file():
        return CheckResult(
            name=spec.check_name,
            status="SKIP",
            detail=f"no crate source tree at {root} — source digest not checkable",
        )

    try:
        expected_digest = crate_source_digest(root)
    # RecursionError: an adversarial symlink CYCLE under src/ makes the
    # twin's recursive walk exceed the interpreter limit — WARN, never a
    # doctor crash (hostile-review fold, DEC-RUST-SOURCE-DIGEST-TWIN-001).
    except (OSError, RecursionError, SourceDigestError) as exc:
        return CheckResult(
            name=spec.check_name,
            status="WARN",
            detail=f"source-digest recompute failed over {root}: {exc}",
        )

    if installed_digest != expected_digest:
        return CheckResult(
            name=spec.check_name,
            status="WARN",
            detail=(
                f"installed {spec.package} wheel source digest {installed_digest} != "
                f"repo tree digest {expected_digest} — the wheel was built from "
                f"DIFFERENT source than this checkout — {rebuild_hint}"
            ),
        )
    return CheckResult(
        name=spec.check_name,
        status="PASS",
        detail=(
            f"installed {spec.package} source digest matches the repo crate tree "
            f"({expected_digest})"
        ),
    )


_TRADING_CORE_SOURCE_DIGEST = _WheelSpec(
    check_name="wheel_source_digest",
    package=_TRADING_CORE_WHEEL.package,
    compiled_submodule=_TRADING_CORE_WHEEL.compiled_submodule,
    crate_dir=_TRADING_CORE_WHEEL.crate_dir,
)

_FNO_MATH_SOURCE_DIGEST = _WheelSpec(
    check_name="wheel_source_digest_fno_math",
    package=_FNO_MATH_WHEEL.package,
    compiled_submodule=_FNO_MATH_WHEEL.compiled_submodule,
    crate_dir=_FNO_MATH_WHEEL.crate_dir,
)


def check_wheel_source_digest(
    crate_root: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> CheckResult:
    """Wheel source-digest handshake for the trading_core wheel.

    Thin binding of the COMMON :func:`_check_one_wheel_source_digest` to the
    ``trading_core`` wheel (DEC-RUST-SOURCE-DIGEST-TWIN-001) — the sibling
    of :func:`check_wheel_freshness`, mirroring its naming.

    Parameters
    ----------
    crate_root
        Explicit crate directory to digest. ``None`` = the repo checkout's
        ``rust/trading-core`` resolved next to the installed ``brutex``
        package (absent → honest SKIP).
    env
        Environment mapping (injected for tests). ``None`` = ``os.environ``.
    """
    return _check_one_wheel_source_digest(_TRADING_CORE_SOURCE_DIGEST, crate_root, env)


def check_fno_math_wheel_source_digest(
    crate_root: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> CheckResult:
    """Wheel source-digest handshake for the fno_math wheel.

    The fno_math twin of :func:`check_wheel_source_digest`, bound to the
    SAME common helper (DEC-RUST-SOURCE-DIGEST-TWIN-001).

    Parameters
    ----------
    crate_root
        Explicit crate directory to digest. ``None`` = the repo checkout's
        ``rust/fno-math`` resolved next to the installed ``brutex`` package
        (absent → honest SKIP).
    env
        Environment mapping (injected for tests). ``None`` = ``os.environ``.
    """
    return _check_one_wheel_source_digest(_FNO_MATH_SOURCE_DIGEST, crate_root, env)


def run_doctor(
    audit_file: Path | None = None,
    audit_dir: Path = DEFAULT_AUDIT_DIR,
    runtime_file: Path = DEFAULT_RUNTIME_FILE,
    live_subjects_file: Path | None = None,
    replay_action_filter: str = "add",
    lake_root: Path | None = None,
    wheel_fingerprint_file: Path | None = None,
    fno_math_wheel_fingerprint_file: Path | None = None,
    wheel_source_crate_root: Path | None = None,
    fno_math_wheel_source_crate_root: Path | None = None,
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
    wheel_fingerprint_file
        Explicit expected-fingerprint JSON for the trading_core
        wheel-freshness stage (R2-CC-G1). ``None`` = the
        ``EXPECTED_BUILD.json`` shipped next to the installed
        ``trading_core`` package.
    fno_math_wheel_fingerprint_file
        Explicit expected-fingerprint JSON for the fno_math wheel-freshness
        stage (DEC-FNOMATH-WHEEL-FRESHNESS-001). ``None`` = the
        ``EXPECTED_BUILD.json`` shipped next to the installed ``fno_math``
        package.
    wheel_source_crate_root
        Explicit crate directory for the trading_core wheel-source-digest
        stage (DEC-RUST-SOURCE-DIGEST-TWIN-001). ``None`` = the repo
        checkout's ``rust/trading-core``.
    fno_math_wheel_source_crate_root
        Explicit crate directory for the fno_math wheel-source-digest stage.
        ``None`` = the repo checkout's ``rust/fno-math``.

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
    report.add(check_wheel_freshness(fingerprint_file=wheel_fingerprint_file))
    report.add(check_fno_math_wheel_freshness(fingerprint_file=fno_math_wheel_fingerprint_file))
    report.add(check_wheel_source_digest(crate_root=wheel_source_crate_root))
    report.add(check_fno_math_wheel_source_digest(crate_root=fno_math_wheel_source_crate_root))
    return report


def main(
    audit_file: Path | None = None,
    audit_dir: Path = DEFAULT_AUDIT_DIR,
    runtime_file: Path = DEFAULT_RUNTIME_FILE,
    live_subjects_file: Path | None = None,
    replay_action_filter: str = "add",
    lake_root: Path | None = None,
    wheel_fingerprint_file: Path | None = None,
    fno_math_wheel_fingerprint_file: Path | None = None,
    wheel_source_crate_root: Path | None = None,
    fno_math_wheel_source_crate_root: Path | None = None,
) -> int:
    """CLI entry point. Print report to stdout; return 0 on PASS, 1 on FAIL."""
    report = run_doctor(
        audit_file=audit_file,
        audit_dir=audit_dir,
        runtime_file=runtime_file,
        live_subjects_file=live_subjects_file,
        replay_action_filter=replay_action_filter,
        lake_root=lake_root,
        wheel_fingerprint_file=wheel_fingerprint_file,
        fno_math_wheel_fingerprint_file=fno_math_wheel_fingerprint_file,
        wheel_source_crate_root=wheel_source_crate_root,
        fno_math_wheel_source_crate_root=fno_math_wheel_source_crate_root,
    )
    print(report.format())
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover — invoked only by CLI
    sys.exit(main())
