"""Pure nightly chain core (DEC-SWEEP-NIGHTLY-AUTOMATION-001).

The R42 break-law sequencer for the nightly zero-click chain: six REPORTED
links (COLLISION → DISK → BACKUP → PULL → PREBUILD → SWEEP) over five executed
steps (PULL + PREBUILD ride ONE ``run_full_pipeline`` invocation; the PULL
step's :attr:`LinkResult.extra_outcomes` carries the PREBUILD outcome as a
*derivation* from the same report, never a second execution).

Break law (R42, holds by construction): the FIRST ``REFUSED``/``FAILED`` link
stops the chain; every later declared link is recorded ``BLOCKED`` with
``blocked_by`` naming the breaker — a break at link N never corrupts links
1..N-1, recovery starts at N, never zero. Tripwires (R39-shaped, nightly
scoped) mark the run SUSPECT; a SUSPECT run exits at least
:data:`EXIT_INTERNAL_ERROR` so launchd/cron sees red (the cron-visible
precedent of ``brutex enrich-greeks`` / ``lake audit-unique``,
DEC-CLI-GREEKS-ENRICH-001, MIGRATION.md #1536).

Deliberately stdlib-only + pure: every effect is injected (the link callables,
the ``on_event`` sink, the clock), so the whole break-law matrix is unit- and
property-testable without a lake, a vendor, or a filesystem.
"""

from __future__ import annotations

import enum
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

__all__ = [
    "EVENT_LINK_BLOCKED",
    "EVENT_LINK_DONE",
    "EVENT_LINK_START",
    "EVENT_TRIPWIRE",
    "EXIT_INTERNAL_ERROR",
    "EXIT_OK",
    "EXIT_SWEEP_TUPLE_FAILED",
    "EXIT_USER_ERROR",
    "LINK_ORDER",
    "REPORT_SCHEMA",
    "REPORT_SCHEMA_VERSION",
    "ChainStep",
    "LinkFn",
    "LinkOutcome",
    "LinkResult",
    "LinkStatus",
    "NightlyLink",
    "NightlyReport",
    "OnEvent",
    "Tripwire",
    "audit_reconcile_tripwire",
    "exit_code_for",
    "first_breaker",
    "run_chain",
]

# ── Exit codes ──────────────────────────────────────────────────────────
# MIRRORS of the CLI exit-code constants at ``brutex/cli.py:80-96`` — this pure
# core must stay stdlib-only (importing ``brutex.cli`` here would drag the
# whole CLI aggregate into every chain consumer), so the values are pinned
# byte-equal to the CLI's by a dedicated drift test in
# ``tests/unit/test_nightly_chain.py`` (§11 COMMON, enforced by test not
# import).
EXIT_OK: Final[int] = 0
EXIT_USER_ERROR: Final[int] = 1
EXIT_INTERNAL_ERROR: Final[int] = 2
EXIT_SWEEP_TUPLE_FAILED: Final[int] = 6

# ── Events emitted through the injected ``on_event`` sink ───────────────
EVENT_LINK_START: Final[str] = "LINK_START"
EVENT_LINK_DONE: Final[str] = "LINK_DONE"
EVENT_LINK_BLOCKED: Final[str] = "LINK_BLOCKED"
EVENT_TRIPWIRE: Final[str] = "TRIPWIRE"

#: ``NightlyReport.to_dict()`` schema tag + version (frozen contract for the
#: future dashboard card — do not drift without a ledgered decision).
REPORT_SCHEMA: Final[str] = "brutex-nightly-report-v1"
REPORT_SCHEMA_VERSION: Final[int] = 1

#: Truncation bound for exception text folded into a ``FAILED`` link detail.
_DETAIL_MAX_CHARS: Final[int] = 500


class NightlyLink(enum.StrEnum):
    """The six reported links of the nightly chain, in fixed order."""

    COLLISION = "collision"
    DISK = "disk"
    BACKUP = "backup"
    PULL = "pull"
    PREBUILD = "prebuild"
    SWEEP = "sweep"


#: The fixed chain order (rationale in the module docstring + runbook §0).
LINK_ORDER: Final[tuple[NightlyLink, ...]] = (
    NightlyLink.COLLISION,
    NightlyLink.DISK,
    NightlyLink.BACKUP,
    NightlyLink.PULL,
    NightlyLink.PREBUILD,
    NightlyLink.SWEEP,
)


class LinkStatus(enum.StrEnum):
    """Terminal status of one reported link.

    ``SKIPPED`` is an honest capability/opt-out gate (``--skip-backup``,
    dry-run mutating links, a pure-spot lake) and does NOT break the chain.
    ``BLOCKED`` is sequencer-assigned only — REFUSED-BECAUSE-<earlier link>,
    never attempted; a link callable can never return it.
    """

    OK = "ok"
    SKIPPED = "skipped"
    REFUSED = "refused"
    FAILED = "failed"
    BLOCKED = "blocked"


#: Statuses a link callable may RETURN (``BLOCKED`` is sequencer-only).
_RETURNABLE: Final[frozenset[LinkStatus]] = frozenset(
    {LinkStatus.OK, LinkStatus.SKIPPED, LinkStatus.REFUSED, LinkStatus.FAILED}
)

#: Statuses that BREAK the chain (everything after them is BLOCKED).
_BREAKERS: Final[frozenset[LinkStatus]] = frozenset({LinkStatus.REFUSED, LinkStatus.FAILED})

#: Statuses that let the chain continue.
_GREEN: Final[frozenset[LinkStatus]] = frozenset({LinkStatus.OK, LinkStatus.SKIPPED})


@dataclass(frozen=True, slots=True)
class LinkOutcome:
    """One reported link's recorded outcome (immutable, report-ready)."""

    link: NightlyLink
    status: LinkStatus
    started_utc: float | None  # None iff BLOCKED (never attempted)
    ended_utc: float | None
    detail: str
    counts: Mapping[str, int]
    error_type: str | None
    blocked_by: NightlyLink | None  # set iff status is BLOCKED
    exit_hint: int | None

    def to_dict(self) -> dict[str, object]:
        """Stable-key-order dict for the report JSON (golden-pinned)."""
        return {
            "link": self.link.value,
            "status": self.status.value,
            "started_utc": self.started_utc,
            "ended_utc": self.ended_utc,
            "detail": self.detail,
            "counts": dict(self.counts),
            "error_type": self.error_type,
            "blocked_by": None if self.blocked_by is None else self.blocked_by.value,
            "exit_hint": self.exit_hint,
        }


@dataclass(frozen=True, slots=True)
class LinkResult:
    """What a link callable RETURNS (never ``BLOCKED`` — sequencer-only).

    ``extra_outcomes`` lets ONE executed step report MORE than one link: the
    PULL step decomposes its single ``PipelinePullReport`` into the PULL
    outcome (this result) plus a fully-built PREBUILD :class:`LinkOutcome`.
    """

    status: LinkStatus
    detail: str = ""
    counts: Mapping[str, int] = field(default_factory=dict)
    error_type: str | None = None  # exception class name on FAILED
    exit_hint: int | None = None  # sweep pass-through (EXIT_SWEEP_TUPLE_FAILED)
    extra_outcomes: tuple[LinkOutcome, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in _RETURNABLE:
            raise ValueError(
                f"a link callable may not return status {self.status!r} — "
                "BLOCKED is sequencer-assigned only"
            )


@dataclass(frozen=True, slots=True)
class Tripwire:
    """One fired self-audit tripwire (R39-shaped; any tripwire ⇒ SUSPECT)."""

    name: str  # "link_transition" | "audit_reconcile" | "pull_report_coherence"
    #            | "snapshot_unreadable" | "sweep_counts_incoherent"
    link: NightlyLink | None
    detail: str

    def to_dict(self) -> dict[str, object]:
        """Stable-key-order dict for the report JSON."""
        return {
            "name": self.name,
            "link": None if self.link is None else self.link.value,
            "detail": self.detail,
        }


LinkFn = Callable[[], LinkResult]
OnEvent = Callable[[str, Mapping[str, object]], None]


@dataclass(frozen=True, slots=True)
class ChainStep:
    """One EXECUTED step: its own link plus any links it also declares.

    The PULL step declares ``also_declares=(NightlyLink.PREBUILD,)`` — the two
    reported links ride one ``run_full_pipeline`` execution (§3.4 of the build
    spec); every other step declares only itself.
    """

    link: NightlyLink
    fn: LinkFn
    also_declares: tuple[NightlyLink, ...] = ()


def first_breaker(outcomes: Sequence[LinkOutcome]) -> LinkOutcome | None:
    """Return the first ``REFUSED``/``FAILED`` outcome, or ``None``."""
    for outcome in outcomes:
        if outcome.status in _BREAKERS:
            return outcome
    return None


def _blocked_outcome(
    link: NightlyLink, breaker_link: NightlyLink, breaker_status: str
) -> LinkOutcome:
    """Synthesize the ``BLOCKED`` outcome for a never-attempted link."""
    return LinkOutcome(
        link=link,
        status=LinkStatus.BLOCKED,
        started_utc=None,
        ended_utc=None,
        detail=f"REFUSED-BECAUSE-{breaker_link.value} {breaker_status}",
        counts={},
        error_type=None,
        blocked_by=breaker_link,
        exit_hint=None,
    )


def run_chain(
    *,
    nightly_id: str,
    steps: Sequence[ChainStep],
    on_event: OnEvent | None = None,
    now: Callable[[], float] = time.time,
) -> tuple[tuple[LinkOutcome, ...], tuple[Tripwire, ...]]:
    """Run the chain steps in order under the R42 break law.

    Semantics (spec §2.3): before each step, if any prior outcome is a breaker
    (``REFUSED``/``FAILED``), every link the step declares is recorded
    ``BLOCKED`` (blocked_by = the FIRST breaker) and the step's callable is
    NEVER invoked. A raising callable becomes ``FAILED`` with the exception's
    class name (``KeyboardInterrupt``/``SystemExit`` propagate RAW — the
    sequencer never swallows an operator interrupt). The sequencer itself
    never re-raises: the report + exit code ARE the propagation surface for
    launchd/cron.

    Tripwire **T1 (link_transition)** is the belt over the property-pinned
    law: before invoking a callable the sequencer re-checks that every prior
    outcome is green; a violation (only reachable through a corrupted
    ``extra_outcomes`` stream — e.g. a BLOCKED outcome with no breaker, an
    undeclared extra link, or a green step that failed to report a declared
    link) fires the tripwire and STOPS the chain (untrustworthy process
    state).

    Parameters
    ----------
    nightly_id
        The one R43 id threading events (stamped on every event payload).
    steps
        The executed steps, in chain order.
    on_event
        Optional sink for ``LINK_START`` / ``LINK_DONE`` / ``LINK_BLOCKED`` /
        ``TRIPWIRE`` events (payloads are the outcome/tripwire dicts plus
        ``nightly_id``). ``None`` ⇒ silent.
    now
        Clock (injected for tests).

    Returns
    -------
    tuple[tuple[LinkOutcome, ...], tuple[Tripwire, ...]]
        Every declared link's outcome in declared order, plus fired tripwires.
    """
    outcomes: list[LinkOutcome] = []
    tripwires: list[Tripwire] = []

    def emit(event: str, payload: Mapping[str, object]) -> None:
        if on_event is not None:
            on_event(event, {"nightly_id": nightly_id, **payload})

    def trip(name: str, link: NightlyLink | None, detail: str) -> None:
        tripwire = Tripwire(name=name, link=link, detail=detail)
        tripwires.append(tripwire)
        emit(EVENT_TRIPWIRE, tripwire.to_dict())

    halted_by: tuple[NightlyLink, str] | None = None  # (link, status-text) once broken

    for step in steps:
        declared: tuple[NightlyLink, ...] = (step.link, *step.also_declares)

        if halted_by is None:
            breaker = first_breaker(outcomes)
            if breaker is not None:
                halted_by = (breaker.link, breaker.status.value)
            elif any(o.status not in _GREEN for o in outcomes):
                # T1 belt: a non-green, non-breaker prior outcome (a BLOCKED
                # row with no breaker) can only mean corrupted process state.
                bad = next(o for o in outcomes if o.status not in _GREEN)
                trip(
                    "link_transition",
                    step.link,
                    f"prior link {bad.link.value} is {bad.status.value} with no "
                    "breaker — refusing to run further links",
                )
                halted_by = (bad.link, bad.status.value)

        if halted_by is not None:
            blocker_link, blocker_status = halted_by
            for link in declared:
                blocked = _blocked_outcome(link, blocker_link, blocker_status)
                outcomes.append(blocked)
                emit(EVENT_LINK_BLOCKED, blocked.to_dict())
            continue

        emit(EVENT_LINK_START, {"link": step.link.value})
        started = now()
        try:
            result = step.fn()
        except Exception as exc:
            # The chain's contract: never re-raise — always produce a report
            # (KeyboardInterrupt/SystemExit are BaseException, propagate raw).
            result = LinkResult(
                status=LinkStatus.FAILED,
                detail=str(exc)[:_DETAIL_MAX_CHARS],
                error_type=type(exc).__name__,
            )
        ended = now()

        own = LinkOutcome(
            link=step.link,
            status=result.status,
            started_utc=started,
            ended_utc=ended,
            detail=result.detail,
            counts=dict(result.counts),
            error_type=result.error_type,
            blocked_by=None,
            exit_hint=result.exit_hint,
        )
        outcomes.append(own)
        emit(EVENT_LINK_DONE, own.to_dict())

        # Record extras in DECLARED order first, then flag any undeclared ones.
        extras_by_link: dict[NightlyLink, list[LinkOutcome]] = {}
        for extra in result.extra_outcomes:
            extras_by_link.setdefault(extra.link, []).append(extra)
        for link in step.also_declares:
            for extra in extras_by_link.pop(link, []):
                outcomes.append(extra)
                emit(EVENT_LINK_DONE, extra.to_dict())
        for leftover_list in extras_by_link.values():
            for extra in leftover_list:
                outcomes.append(extra)
                emit(EVENT_LINK_DONE, extra.to_dict())
                trip(
                    "link_transition",
                    extra.link,
                    f"step {step.link.value} reported an outcome for undeclared "
                    f"link {extra.link.value}",
                )

        # A declared link the step failed to report: synthesize BLOCKED.
        recorded = {o.link for o in outcomes}
        for link in step.also_declares:
            if link not in recorded:
                blocked = _blocked_outcome(link, step.link, result.status.value)
                outcomes.append(blocked)
                emit(EVENT_LINK_BLOCKED, blocked.to_dict())
                if result.status in _GREEN:
                    # A green step MUST report every link it declares — a
                    # missing one means the decomposition cannot be trusted.
                    trip(
                        "link_transition",
                        link,
                        f"step {step.link.value} finished {result.status.value} "
                        f"but reported no outcome for declared link {link.value}",
                    )

    return tuple(outcomes), tuple(tripwires)


def exit_code_for(outcomes: Sequence[LinkOutcome], tripwires: Sequence[Tripwire]) -> int:
    """The nightly exit-code law (cron-visible, spec §2.4).

    REFUSED breaker ⇒ :data:`EXIT_USER_ERROR`; FAILED breaker ⇒ its
    ``exit_hint`` when set (the sweep's ``EXIT_SWEEP_TUPLE_FAILED`` = 6
    pass-through) else :data:`EXIT_INTERNAL_ERROR`; all green ⇒
    :data:`EXIT_OK`. ANY tripwire upgrades to at least
    :data:`EXIT_INTERNAL_ERROR` — a SUSPECT run must be cron-RED, never a
    green illusion (Golden Rule #1).
    """
    breaker = first_breaker(outcomes)
    if breaker is None:
        base = EXIT_OK
    elif breaker.status is LinkStatus.REFUSED:
        base = EXIT_USER_ERROR
    elif breaker.exit_hint is not None:
        base = breaker.exit_hint
    else:
        base = EXIT_INTERNAL_ERROR
    if tripwires:
        # SUSPECT only ever UPGRADES severity: 0/1/2 → 2; a 6 pass-through
        # stays 6 (numerically max() delivers exactly that mapping).
        return max(base, EXIT_INTERNAL_ERROR)
    return base


def audit_reconcile_tripwire(
    *,
    nightly_id: str,
    events: Sequence[tuple[str, str | None]],
    outcomes: Sequence[LinkOutcome],
) -> Tripwire | None:
    """Tripwire **T2 (audit_reconcile)** — the chain-end audit reconciliation.

    Pure function over this run's audit stream, projected as ``(action,
    link-or-None)`` pairs: there must be exactly ONE ``NIGHTLY_START``; every
    ``LINK_START`` must name an ATTEMPTED link (``started_utc`` not ``None``)
    at most once — an extra-outcome link (the PULL step's PREBUILD derivation)
    legitimately has a ``LINK_DONE`` with NO ``LINK_START``, so the START set
    is a SUBSET check, not an equality; and there must be exactly one
    ``LINK_DONE``/``LINK_BLOCKED`` per DECLARED link (the lost/duplicated-
    append detector). Any mismatch returns the tripwire (the caller records
    it → SUSPECT); a clean stream returns ``None``.
    """
    starts = sum(1 for action, _ in events if action == "NIGHTLY_START")
    problems: list[str] = []
    if starts != 1:
        problems.append(f"NIGHTLY_START x{starts} (want 1)")

    attempted = {o.link.value for o in outcomes if o.started_utc is not None}
    link_starts = [link for action, link in events if action == "LINK_START"]
    named_starts = [x for x in link_starts if x is not None]
    if len(named_starts) != len(set(named_starts)) or not set(named_starts) <= attempted:
        problems.append(
            f"LINK_START {link_starts!r} not a subset of attempted {sorted(attempted)!r}"
        )

    declared = [o.link.value for o in outcomes]
    terminal = [link for action, link in events if action in ("LINK_DONE", "LINK_BLOCKED")]
    if sorted(declared) != sorted(x for x in terminal if x is not None):
        problems.append(f"LINK_DONE/BLOCKED {terminal!r} != declared {declared!r}")

    if not problems:
        return None
    return Tripwire(
        name="audit_reconcile",
        link=None,
        detail=f"nightly {nightly_id} audit does not reconcile: " + "; ".join(problems),
    )


@dataclass(frozen=True, slots=True)
class NightlyReport:
    """The one immutable record of a nightly run (report.json + Telegram source)."""

    schema_version: int
    nightly_id: str
    lake_root: str
    results_root: str
    backup_root: str
    started_utc: float
    ended_utc: float
    links: tuple[LinkOutcome, ...]
    tripwires: tuple[Tripwire, ...]
    counts: Mapping[str, int | None]  # None = honestly unknown, NEVER a fabricated 0
    snapshot_id: str | None
    absent_artifacts: tuple[str, ...]
    honesty_notes: tuple[str, ...]
    dry_run: bool
    #: ADDITIVE box-update block (DEC-BOX-AUTODEPLOY-LANE-001 §5) — the
    #: ``BoxUpdateOutcome.to_dict()`` mapping, ``None`` when the lane is
    #: absent/never-ran (the block key is emitted ALWAYS — stable key set).
    box_update: Mapping[str, object] | None = None
    #: ADDITIVE env-preflight block (DEC-BOX-AUTODEPLOY-LANE-001 §12) — the
    #: ``EnvPreflightOutcome.to_dict()`` mapping, ``None`` when it never ran.
    env_preflight: Mapping[str, object] | None = None

    @property
    def ok(self) -> bool:
        """True iff every link is OK/SKIPPED AND zero tripwires fired."""
        return not self.tripwires and all(o.status in _GREEN for o in self.links)

    @property
    def suspect(self) -> bool:
        """Derived from :attr:`tripwires` — never independently stored."""
        return bool(self.tripwires)

    @property
    def blocker(self) -> NightlyLink | None:
        """The first REFUSED/FAILED link, else ``None``."""
        breaker = first_breaker(self.links)
        return None if breaker is None else breaker.link

    @property
    def exit_code(self) -> int:
        """Delegates to :func:`exit_code_for` (the one exit-code law)."""
        return exit_code_for(self.links, self.tripwires)

    def to_dict(self) -> dict[str, object]:
        """Stable-key-order dict for ``report.json`` (golden-pinned)."""
        return {
            "schema": REPORT_SCHEMA,
            "schema_version": self.schema_version,
            "nightly_id": self.nightly_id,
            "lake_root": self.lake_root,
            "results_root": self.results_root,
            "backup_root": self.backup_root,
            "started_utc": self.started_utc,
            "ended_utc": self.ended_utc,
            "links": [o.to_dict() for o in self.links],
            "tripwires": [t.to_dict() for t in self.tripwires],
            "counts": dict(self.counts),
            "snapshot_id": self.snapshot_id,
            "absent_artifacts": list(self.absent_artifacts),
            "honesty_notes": list(self.honesty_notes),
            "dry_run": self.dry_run,
            "box_update": None if self.box_update is None else dict(self.box_update),
            "env_preflight": None if self.env_preflight is None else dict(self.env_preflight),
            "ok": self.ok,
            "suspect": self.suspect,
            "blocker": None if self.blocker is None else self.blocker.value,
            "exit_code": self.exit_code,
        }
