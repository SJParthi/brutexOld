"""Acceptance-verifier data model — verdicts, evidence invariants, roll-up + exit law.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756). The three laws of the
design live HERE, structurally:

1. **One verdict = one evidence block** — :class:`SubCheckResult` REFUSES an
   empty evidence dict (fail loud, never a bare verdict).
2. **No verdict without work** — every sub-check carries ``units_checked`` in
   its evidence; the :func:`make_subcheck` factory coerces a zero-unit PASS
   into ``ABSTAIN(nothing_to_check)`` (the #935/#1014 dormant-feature trap
   closed structurally). ABSTAIN is first-class and never rolls up green.
3. **Teeth before trust** — the exit law (:func:`exit_code_for`) puts a failed
   self-test (``TEETH_FAILED``) at the SUSPECT floor (exit 2), outranking data
   findings (a broken verifier means the FAIL list may be incomplete).

Pure data + pure functions only — no I/O, no clock, no lake reads.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Final

#: Frozen report schema token (golden-pinned; additive-only evolution).
SCHEMA_VERSION: Final[str] = "brutex-acceptance-report-v1"

#: Selection lists inside a coverage block are capped at this many entries.
SELECTION_CAP: Final[int] = 100

#: The SUSPECT exit code (verifier internal error / TEETH_FAILED floor).
EXIT_SUSPECT: Final[int] = 2

#: A sub-check whose NON-structural abstain streak reaches this many
#: consecutive runs flips the whole run to exit 4 (inconclusive) — an
#: eternally-abstaining check can never hide at exit 0.
ABSTAIN_STREAK_LIMIT: Final[int] = 3


class AcceptanceModelError(ValueError):
    """A structural invariant of the acceptance model was violated (fail loud)."""


class Verdict(StrEnum):
    """The 4-valued machine verdict vocabulary (sample-ness lives in coverage)."""

    PASS = "PASS"  # noqa: S105 — a verdict token, not a credential
    FAIL = "FAIL"
    ABSTAIN = "ABSTAIN"
    ERROR = "ERROR"


class OverallVerdict(StrEnum):
    """Whole-run verdict token for the report headline."""

    PASS = "PASS"  # noqa: S105 — a verdict token, not a credential
    PASS_WITH_ABSTAINS = "PASS_WITH_ABSTAINS"  # noqa: S105 — verdict token
    FAIL = "FAIL"
    SUSPECT = "SUSPECT"


class TeethVerdict(StrEnum):
    """Self-test outcome (§7 of the design): proven, failed, or operator-skipped."""

    TEETH_PROVEN = "TEETH_PROVEN"
    TEETH_FAILED = "TEETH_FAILED"
    TEETH_SKIPPED = "TEETH_SKIPPED"


class AbstainReason(StrEnum):
    """The FROZEN abstain-reason enum — every arm test-pinned, never free text."""

    NOTHING_TO_CHECK = "nothing_to_check"
    PULL_IN_PROGRESS = "pull_in_progress"
    INPUT_MOVED_DURING_CHECK = "input_moved_during_check"
    DAILY_CUTOFF_AMBIGUOUS = "daily_cutoff_ambiguous"
    NO_PER_CALL_PERSISTENCE = "no_per_call_persistence"
    NO_OI_COMPARE_SEAM = "no_oi_compare_seam"
    PREBUILD_GATED = "prebuild_gated"
    NO_FNO_DATA = "no_fno_data"
    NO_LEDGER = "no_ledger"
    NO_BRIDGE_FILE = "no_bridge_file"
    NO_RATE_LOG_CONFIGURED = "no_rate_log_configured"
    NO_RATE_LOG_LINES = "no_rate_log_lines"
    NO_PER_CALL_LOG = "no_per_call_log"
    NO_DATASET = "no_dataset"
    NO_DATASETS = "no_datasets"
    NO_LAKE = "no_lake"
    NO_INSTRUMENT_STORE = "no_instrument_store"
    OUT_OF_CALENDAR_COVERAGE = "out_of_calendar_coverage"
    TIME_BUDGET_EXHAUSTED = "time_budget_exhausted"
    REGISTRY_SCHEMA_DRIFT = "registry_schema_drift"
    NON_DEFAULT_WINDOW_SHAPE = "non_default_window_shape"
    ROUTE_ERROR = "route_error"


#: Abstain reasons EXEMPT from the ``--abstain-budget`` + streak laws: standing
#: BY-DESIGN gaps (a per-call rate log does not exist — follow-up F3 under
#: operator ruling; derived-F&O OI is not value-compared in v1 — follow-up F2;
#: the rotating structlog file sink exists ONLY when ``cmd_dashboard`` ran, so a
#: headless nightly-cron / CLI-fetch box has NO sink by design — an absent sink
#: is a standing gap identical in kind to per_call_history, NOT a nightly
#: regression, so it can never turn a healthy headless lake permanently amber).
#: Frozen — extending this set is a ledgered decision, never a convenience.
STRUCTURAL_ABSTAINS: Final[frozenset[AbstainReason]] = frozenset(
    {
        AbstainReason.NO_PER_CALL_PERSISTENCE,
        AbstainReason.NO_PER_CALL_LOG,
        AbstainReason.NO_OI_COMPARE_SEAM,
        AbstainReason.NO_RATE_LOG_CONFIGURED,
    }
)


@dataclass(frozen=True)
class Coverage:
    """The REQUIRED per-check scope disclosure — 'all' appears only under ``mode='full'``.

    Parameters
    ----------
    mode
        ``full`` | ``sample`` | ``bounded`` | ``none``.
    population
        Total units in the addressable population.
    examined
        Units actually examined this run.
    seed
        The sampling seed used (the IST report date by default).
    selection
        The examined unit keys, capped at :data:`SELECTION_CAP`.
    selection_truncated
        True when the selection list was capped.
    """

    mode: str
    population: int
    examined: int
    seed: str
    selection: tuple[str, ...] = ()
    selection_truncated: bool = False

    def __post_init__(self) -> None:
        if self.mode not in {"full", "sample", "bounded", "none"}:
            raise AcceptanceModelError(f"unknown coverage mode: {self.mode!r}")

    def to_dict(self) -> dict[str, Any]:
        """Project to the frozen report-schema shape."""
        return {
            "mode": self.mode,
            "population": self.population,
            "examined": self.examined,
            "seed": self.seed,
            "selection": list(self.selection),
            "selection_truncated": self.selection_truncated,
        }


def make_coverage(
    *, mode: str, population: int, examined: int, seed: str, selection: Sequence[str] = ()
) -> Coverage:
    """Build a :class:`Coverage`, capping the selection list at :data:`SELECTION_CAP`."""
    sel = tuple(selection)
    truncated = len(sel) > SELECTION_CAP
    return Coverage(
        mode=mode,
        population=population,
        examined=examined,
        seed=seed,
        selection=sel[:SELECTION_CAP],
        selection_truncated=truncated,
    )


@dataclass(frozen=True)
class SubCheckResult:
    """One sub-check verdict WITH its mandatory evidence block.

    Invariants (enforced, fail loud — :class:`AcceptanceModelError`):

    * ``evidence`` is non-empty and carries ``units_checked``;
    * ``ABSTAIN`` carries a frozen :class:`AbstainReason`; non-ABSTAIN never does;
    * a ``PASS`` with ``units_checked == 0`` is REFUSED (build through
      :func:`make_subcheck`, which coerces it to ``ABSTAIN(nothing_to_check)``).
    """

    id: str
    verdict: Verdict
    evidence: Mapping[str, Any]
    abstain_reason: AbstainReason | None = None
    limits: tuple[str, ...] = ()
    repro: str = ""
    abstain_streak: int = 0

    def __post_init__(self) -> None:
        if not self.evidence:
            raise AcceptanceModelError(f"sub-check {self.id!r}: empty evidence block refused")
        if "units_checked" not in self.evidence:
            raise AcceptanceModelError(f"sub-check {self.id!r}: evidence lacks units_checked")
        if self.verdict is Verdict.ABSTAIN and self.abstain_reason is None:
            raise AcceptanceModelError(f"sub-check {self.id!r}: ABSTAIN without a named reason")
        if self.verdict is not Verdict.ABSTAIN and self.abstain_reason is not None:
            raise AcceptanceModelError(f"sub-check {self.id!r}: reason on a non-ABSTAIN verdict")
        if self.verdict is Verdict.PASS and int(self.evidence["units_checked"]) == 0:
            raise AcceptanceModelError(
                f"sub-check {self.id!r}: PASS with zero units checked refused "
                "(no verdict without work — use make_subcheck)"
            )

    @property
    def structural(self) -> bool:
        """True when this abstain is budget/streak-exempt (frozen list)."""
        return self.abstain_reason in STRUCTURAL_ABSTAINS

    def to_dict(self) -> dict[str, Any]:
        """Project to the frozen report-schema shape."""
        return {
            "id": self.id,
            "verdict": str(self.verdict),
            "abstain_reason": None if self.abstain_reason is None else str(self.abstain_reason),
            "structural": self.structural,
            "evidence": dict(self.evidence),
            "abstain_streak": self.abstain_streak,
            "limits": list(self.limits),
            "repro": self.repro,
        }


def make_subcheck(
    id_: str,
    verdict: Verdict,
    *,
    units_checked: int,
    evidence: Mapping[str, Any],
    abstain_reason: AbstainReason | None = None,
    limits: Sequence[str] = (),
    repro: str = "",
) -> SubCheckResult:
    """Build a :class:`SubCheckResult`, coercing a zero-unit PASS to ABSTAIN.

    The ONE constructor checks use: it injects ``units_checked`` into the
    evidence block and applies design LAW 2 — ``units_checked == 0`` can never
    yield PASS (forced ``ABSTAIN(nothing_to_check)``).
    """
    ev = {**dict(evidence), "units_checked": units_checked}
    if verdict is Verdict.PASS and units_checked == 0:
        verdict = Verdict.ABSTAIN
        abstain_reason = AbstainReason.NOTHING_TO_CHECK
    return SubCheckResult(
        id=id_,
        verdict=verdict,
        evidence=ev,
        abstain_reason=abstain_reason,
        limits=tuple(limits),
        repro=repro,
    )


def with_streak(sub: SubCheckResult, streak: int) -> SubCheckResult:
    """Return *sub* with its consecutive-abstain streak stamped (report carry)."""
    return replace(sub, abstain_streak=streak)


@dataclass(frozen=True)
class CheckResult:
    """One check's verdict: roll-up of its sub-checks + the coverage disclosure."""

    check_id: str
    verdict: Verdict
    coverage: Coverage
    sub_checks: tuple[SubCheckResult, ...]
    duration_s: float = 0.0
    repro: str = ""
    limits: tuple[str, ...] = ()
    input_fingerprint: Mapping[str, Any] = field(
        default_factory=lambda: {"moved": False, "retried": False}
    )

    def to_dict(self) -> dict[str, Any]:
        """Project to the frozen report-schema shape."""
        return {
            "check_id": self.check_id,
            "verdict": str(self.verdict),
            "coverage": self.coverage.to_dict(),
            "sub_checks": [s.to_dict() for s in self.sub_checks],
            "input_fingerprint": dict(self.input_fingerprint),
            "duration_s": self.duration_s,
            "repro": self.repro,
            "limits": list(self.limits),
        }


def rollup_check_verdict(sub_checks: Sequence[SubCheckResult]) -> Verdict:
    """Fold sub-check verdicts to a check verdict: ERROR > FAIL > all-ABSTAIN > PASS."""
    verdicts = {s.verdict for s in sub_checks}
    if Verdict.ERROR in verdicts:
        return Verdict.ERROR
    if Verdict.FAIL in verdicts:
        return Verdict.FAIL
    if verdicts == {Verdict.ABSTAIN} or not verdicts:
        return Verdict.ABSTAIN
    return Verdict.PASS


def make_check(
    check_id: str,
    sub_checks: Sequence[SubCheckResult],
    *,
    coverage: Coverage,
    duration_s: float = 0.0,
    repro: str = "",
    limits: Sequence[str] = (),
    input_fingerprint: Mapping[str, Any] | None = None,
) -> CheckResult:
    """Build a :class:`CheckResult` with the roll-up law applied."""
    return CheckResult(
        check_id=check_id,
        verdict=rollup_check_verdict(sub_checks),
        coverage=coverage,
        sub_checks=tuple(sub_checks),
        duration_s=duration_s,
        repro=repro,
        limits=tuple(limits),
        input_fingerprint=(
            {"moved": False, "retried": False} if input_fingerprint is None else input_fingerprint
        ),
    )


@dataclass(frozen=True)
class Overall:
    """The whole-run roll-up: headline verdict + the exit-code decision inputs."""

    verdict: OverallVerdict
    exit_code: int
    fail_checks: tuple[str, ...]
    error_checks: tuple[str, ...]
    abstain_subchecks_structural: int
    abstain_subchecks_nonstructural: int
    passes_full: int
    passes_sample: int

    def to_dict(self) -> dict[str, Any]:
        """Project to the frozen report-schema shape."""
        return {
            "verdict": str(self.verdict),
            "exit_code": self.exit_code,
            "fail_checks": list(self.fail_checks),
            "error_checks": list(self.error_checks),
            "abstain_subchecks_structural": self.abstain_subchecks_structural,
            "abstain_subchecks_nonstructural": self.abstain_subchecks_nonstructural,
            "passes_full": self.passes_full,
            "passes_sample": self.passes_sample,
        }


def exit_code_for(
    *,
    teeth: TeethVerdict,
    any_error: bool,
    any_fail: bool,
    nonstructural_abstains: int,
    abstain_budget: int,
    max_nonstructural_streak: int,
    time_truncated: bool,
) -> int:
    """The frozen exit-code law (§6): precedence 2 > 3 > 4 > 0.

    ``2`` — verifier internal error OR ``TEETH_FAILED`` (tool-red outranks
    data-red: a broken verifier means the FAIL list may be incomplete);
    ``3`` — ≥1 FAIL finding with the verifier healthy;
    ``4`` — zero FAIL but inconclusive (non-structural abstains over budget,
    a non-structural abstain streak ≥ :data:`ABSTAIN_STREAK_LIMIT`, or a
    time-truncated run); ``0`` — clean. Structural abstains are exempt from
    the budget + streak laws (frozen list — the standing by-design gaps
    never turn the nightly permanently amber).
    """
    if teeth is TeethVerdict.TEETH_FAILED or any_error:
        return 2
    if any_fail:
        return 3
    if (
        nonstructural_abstains > abstain_budget
        or max_nonstructural_streak >= ABSTAIN_STREAK_LIMIT
        or time_truncated
    ):
        return 4
    return 0


def rollup_overall(
    checks: Sequence[CheckResult],
    *,
    teeth: TeethVerdict,
    abstain_budget: int,
    time_truncated: bool = False,
) -> Overall:
    """Fold check results + the self-test verdict into the :class:`Overall` block."""
    fail_checks = tuple(c.check_id for c in checks if c.verdict is Verdict.FAIL)
    error_checks = tuple(c.check_id for c in checks if c.verdict is Verdict.ERROR)
    structural = 0
    nonstructural = 0
    max_streak = 0
    for check in checks:
        for sub in check.sub_checks:
            if sub.verdict is Verdict.ABSTAIN:
                if sub.structural:
                    structural += 1
                else:
                    nonstructural += 1
                    max_streak = max(max_streak, sub.abstain_streak)
    passes_full = sum(1 for c in checks if c.verdict is Verdict.PASS and c.coverage.mode == "full")
    passes_sample = sum(
        1 for c in checks if c.verdict is Verdict.PASS and c.coverage.mode != "full"
    )
    exit_code = exit_code_for(
        teeth=teeth,
        any_error=bool(error_checks),
        any_fail=bool(fail_checks),
        nonstructural_abstains=nonstructural,
        abstain_budget=abstain_budget,
        max_nonstructural_streak=max_streak,
        time_truncated=time_truncated,
    )
    if exit_code == EXIT_SUSPECT:
        verdict = OverallVerdict.SUSPECT
    elif fail_checks:
        verdict = OverallVerdict.FAIL
    elif structural or nonstructural:
        verdict = OverallVerdict.PASS_WITH_ABSTAINS
    else:
        verdict = OverallVerdict.PASS
    return Overall(
        verdict=verdict,
        exit_code=exit_code,
        fail_checks=fail_checks,
        error_checks=error_checks,
        abstain_subchecks_structural=structural,
        abstain_subchecks_nonstructural=nonstructural,
        passes_full=passes_full,
        passes_sample=passes_sample,
    )


def totals_for(checks: Sequence[CheckResult]) -> dict[str, int]:
    """Sub-check verdict tallies for the report ``totals`` block."""
    totals = {"pass": 0, "fail": 0, "abstain": 0, "error": 0}
    for check in checks:
        for sub in check.sub_checks:
            totals[str(sub.verdict).lower()] += 1
    return totals
