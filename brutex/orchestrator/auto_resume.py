"""Auto-resume-on-boot — continue an in-progress pull automatically at process start.

Closes the operator's GAP-2 / future item #11: on a fresh boot (a redeployed
container, a restarted service, a cold start after a crash) BruteX should
AUTOMATICALLY continue an in-progress full-universe pull instead of needing a
manual "start pull" click. This is the boot-time complement of two pieces that
already exist:

* the durable per-window ledger (:mod:`brutex.data.window_ledger` +
  :mod:`brutex.data.window_ledger_store`) — a window is settled only when
  ``SUCCESS`` / ``SKIPPED``; every ``PLANNED`` (never fetched) or ``FAILED``
  (transient) window is re-pullable and the keep-first sink makes re-pulling a
  settled window a no-op (DEC-FNO-LEDGER-LAKE-RECONCILE-001), so the DATA layer
  is already resume-safe; and
* :class:`brutex.orchestrator.supervisor.ProcessSupervisor`
  (DEC-SUPERVISOR-AUTORESTART-001) — which keeps the PROCESS alive across a
  crash. That relaunches the process; THIS decides, once the process is up
  again, whether there is unfinished pull work to pick back up.

This module supplies only the boot-time TRIGGER: a pure detector that reads the
ledger's outstanding count, and a guarded orchestration helper that resumes iff
(a) the operator opted in via an env flag and (b) the ledger shows unfinished
work. The resume ACTION itself is INJECTED as a plain callable, so this module
never imports the pull spine and stays a thin, fully-testable decision layer
(CLAUDE.md §4 dependency injection / §11 COMMON).

## Design (priority law — Correctness > Reliability > … > Performance)

* **Opt-in, default OFF.** :func:`resolve_auto_resume_on_boot` reads
  ``BRUTEX_AUTO_RESUME_ON_BOOT`` and returns ``True`` only for an explicit
  truthy value; unset / blank / ``"0"`` / unrecognised ⇒ ``False``. A dev / CI
  boot is never surprised by a spontaneous pull; the operator turns it on for
  production (mirrors :func:`brutex.adapters.groww.constants.resolve_smooth_pacing`
  — CLAUDE.md §2 Rule 6, never silently change a fetch gate).
* **Idempotent + safe.** The resume re-runs the SAME idempotent pull over the
  ledger; re-pulling a settled window is a no-op (keep-first sink), only
  unsettled windows re-pull. This module adds no data path — it only decides
  whether to fire the injected action.
* **Non-blocking / best-effort at boot.** :func:`maybe_resume_on_boot` NEVER
  raises: every failure (an unreadable ledger, a resume action that throws) is
  caught, logged, and returned as a verdict, so a boot can never be blocked or
  crashed by the resume attempt (Golden Rule #10). The caller runs it off the
  server-bind path (a daemon thread), so even a slow resume trigger never delays
  the port bind.
* **ENGINE UNCHANGED.** Pure orchestration — the brute-force engine stays
  NIFTY / BANKNIFTY / SENSEX and the F&O lake stays REFERENCE_ONLY (CLAUDE.md
  §3). Nothing here touches the vocabulary, the rate caps, or the lake schema.
* **ONE decision point with the intent-backed pipeline resume**
  (DEC-PULL-INTENT-RESUME-001, superseding the CLI-child-ONLY arm of this
  module's original decision for intent-backed boots): when the caller wires
  the OPTIONAL ``intent_present`` / ``resume_intent`` pair into
  :func:`maybe_resume_on_boot` and a durable ``.pull_intent.json`` is present,
  the dashboard PIPELINE resume runs EXCLUSIVELY — the legacy spot-only CLI
  child (``brutex fetch --until-clean``) is NOT spawned that boot. Without
  that exclusivity the two resumes race the account-wide single-flight
  ``flock``: whichever loses marks a fresh job FAILED at boot / logs a false
  ``auto_resume.error``, and a healthy system can burn a restart token. With
  no intent (or the pair unwired) the legacy path below is byte-identical.

Every read is O(1) beyond the one O(N)
:meth:`~brutex.data.window_ledger.WindowLedger.progress_counts` fold the ledger
already exposes (called once per boot, never on a hot path).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Final, Protocol

import structlog

__all__ = [
    "AUTO_RESUME_ON_BOOT_ENV",
    "IncompletePull",
    "LedgerLike",
    "ResumeVerdict",
    "detect_incomplete_pull",
    "maybe_resume_on_boot",
    "resolve_auto_resume_on_boot",
]

#: Structured logger — surfaces the boot-time resume decision into ``/logs``.
_LOG = structlog.get_logger("brutex.orchestrator.auto_resume")

#: Env var gating the boot-time auto-resume. Default OFF — see
#: :func:`resolve_auto_resume_on_boot`.
AUTO_RESUME_ON_BOOT_ENV: Final[str] = "BRUTEX_AUTO_RESUME_ON_BOOT"

#: The truthy set (case-insensitive) that ENABLES the flag — identical to the
#: sibling fetch-gate toggles so the operator learns ONE convention (CLAUDE.md §11).
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})


class _ProgressCounts(Protocol):
    """The minimal shape :func:`detect_incomplete_pull` reads off the ledger snapshot.

    Only the four integer folds it needs are declared, so the detector depends on
    the BEHAVIOUR (a progress snapshot) not the concrete
    :class:`~brutex.data.window_ledger.LedgerProgress` — a plain object / test
    double exposing these attributes satisfies it (CLAUDE.md §4).

    A value-returning stub property ``raise``\\s ``NotImplementedError`` (a real
    statement, never a bare ``...`` no-op flagged by static analysis, and it
    satisfies the declared return type) — mirrors
    :class:`brutex.orchestrator.supervisor.ChildProcess`.
    """

    @property
    def total(self) -> int:
        """Distinct windows recorded (the pull's full planned tiling)."""
        raise NotImplementedError

    @property
    def planned(self) -> int:
        """Windows still ``PLANNED`` (never fetched) — genuinely outstanding."""
        raise NotImplementedError

    @property
    def failed(self) -> int:
        """Windows in the transient ``FAILED`` status — outstanding, re-pullable."""
        raise NotImplementedError

    @property
    def settled(self) -> int:
        """Windows in a settled skip status (``SUCCESS`` / ``SKIPPED``)."""
        raise NotImplementedError


class LedgerLike(Protocol):
    """The minimal ledger surface :func:`detect_incomplete_pull` consumes.

    Only :meth:`progress_counts` is needed, so the detector depends on the
    behaviour not the concrete :class:`~brutex.data.window_ledger.WindowLedger`
    (CLAUDE.md §4). The live ledger + any test double with a ``progress_counts``
    method satisfies it.
    """

    def progress_counts(self) -> _ProgressCounts:
        """Return the live O(N) progress snapshot of the ledger."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class IncompletePull:
    """A confirmed in-progress pull with unfinished window work — the detector's verdict.

    Returned by :func:`detect_incomplete_pull` ONLY when the ledger records at
    least one window AND at least one is still outstanding (``PLANNED`` or
    ``FAILED``). Every field is a non-negative count derived from the
    authoritative ledger snapshot, so a repeated read is idempotent and never
    over-counts (mirrors :class:`~brutex.data.window_ledger.LedgerProgress`).
    """

    #: Distinct windows recorded so far (the pull's full planned tiling).
    total: int
    #: Windows settled (``SUCCESS`` / ``SKIPPED``) — done, never re-pulled.
    settled: int
    #: Windows still ``PLANNED`` (never fetched) — a re-pull WILL attempt these.
    planned: int
    #: Windows in the transient ``FAILED`` status — a re-pull WILL re-attempt these.
    failed: int

    @property
    def outstanding(self) -> int:
        """Windows a resume would work on — ``planned + failed`` (the re-pull surface)."""
        return self.planned + self.failed


class ResumeVerdict(Enum):
    """The terminal outcome of one :func:`maybe_resume_on_boot` call.

    Every path returns exactly one of these; the helper never raises, so the
    verdict IS the whole result (Golden Rule #10 — an honest, observable outcome).
    """

    #: The flag was OFF (the default) — nothing was read, nothing was resumed.
    DISABLED = "disabled"
    #: The flag was ON but the ledger showed no unfinished work — a clean boot.
    NOTHING_TO_RESUME = "nothing_to_resume"
    #: The flag was ON, unfinished work was found, and the resume action FIRED.
    RESUMED = "resumed"
    #: A durable pull intent was present, so the intent-backed PIPELINE resume
    #: ran EXCLUSIVELY — the legacy CLI child was NOT spawned this boot
    #: (DEC-PULL-INTENT-RESUME-001; independent of the legacy env flag).
    INTENT_RESUMED = "intent_resumed"
    #: The flag was ON but a load / resume error was caught + logged — boot is
    #: unaffected (best-effort). The next boot re-tries.
    ERROR = "error"


def resolve_auto_resume_on_boot(env: Mapping[str, str] | None = None) -> bool:
    """Resolve the boot-time auto-resume toggle from the env — default OFF.

    Reads :data:`AUTO_RESUME_ON_BOOT_ENV`. Returns ``True`` ONLY for an explicit
    truthy value (``"1"`` / ``"true"`` / ``"yes"`` / ``"on"``, case-insensitive);
    ANY other value — unset, blank, ``"0"``, ``"false"``, or unrecognised —
    returns ``False``, so the safe default-OFF behaviour can never be enabled by
    accident (CLAUDE.md §2 Rule 6 — never silently change a fetch gate). *env*
    defaults to :data:`os.environ`; a test injects its own mapping. O(1).
    """
    source: Mapping[str, str] = os.environ if env is None else env
    raw = source.get(AUTO_RESUME_ON_BOOT_ENV)
    if raw is None:
        return False
    return raw.strip().lower() in _TRUTHY


def detect_incomplete_pull(ledger: LedgerLike) -> IncompletePull | None:
    """Return the outstanding-work verdict for *ledger*, or ``None`` when clean — **pure**.

    An in-progress pull is one whose ledger records ≥ 1 window AND has ≥ 1
    window still outstanding — ``PLANNED`` (never fetched) OR ``FAILED``
    (transient). Those are exactly the statuses a re-pull re-attempts and that
    :meth:`~brutex.data.window_ledger.WindowLedger.is_skippable` returns ``False``
    for, so this reads the SAME skip authority the pull itself uses (no drift).

    * An EMPTY ledger (``total == 0`` — a fresh clone / never-pulled lake, or a
      dry-run that recorded nothing) is NOT an incomplete pull — there is nothing
      to resume, so this returns ``None``.
    * A ledger whose every window is settled (``planned == 0 and failed == 0``)
      is a COMPLETE pull — ``None``. A genuine below-threshold ``EMPTY`` window is
      deliberately NOT counted here: it is resolved-not-pending for completion
      reporting (:data:`~brutex.data.window_ledger.RESOLVED_STATUSES`), and on a
      wholly-past range it can never gain data, so treating it as "resume me"
      would loop a boot forever on a permanently-empty window. Only the genuinely
      re-pullable surface (``PLANNED`` + ``FAILED``) triggers a resume.

    HONEST — a persistently-failing window re-triggers on every boot (Golden Rule
    #10). A ``FAILED`` row is TRANSIENT by design: the ledger settles a genuinely
    dead window to ``SKIPPED`` via its empty-streak / old-gap machinery, so a normal
    pull converges and this detector goes quiet. BUT a window that keeps FAILING
    every pull for a NON-empty reason the ledger never auto-settles — e.g. a symbol
    the vendor 500s indefinitely, or an unreachable partition — stays ``FAILED``, so
    each fresh boot reads "incomplete" and re-spawns a resume. That is BOUNDED, not
    an infinite loop: each spawned ``--until-clean`` is itself round-bounded
    (``AUTO_RESUME_MAX_ROUNDS`` + the four ``decide_until_clean`` terminal guards) and
    runs only ONCE per boot — so a boot re-tries a stuck window exactly as a manual
    ``brutex fetch --until-clean`` would, no worse. It never fabricates data and never
    hammers (a boot is a discrete event). Auto-settling such a persistent-transient
    window is the ledger's job (an explicit non-goal here); this detector honestly
    reflects whatever the ledger reports.

    Pure over the ledger snapshot — one O(N) ``progress_counts`` fold (which the
    ledger already exposes) + O(1) arithmetic, no I/O, never raises for a
    well-formed snapshot. Returns a frozen :class:`IncompletePull` or ``None``.
    """
    counts = ledger.progress_counts()
    if counts.total <= 0:
        return None
    if counts.planned <= 0 and counts.failed <= 0:
        return None
    return IncompletePull(
        total=counts.total,
        settled=counts.settled,
        planned=counts.planned,
        failed=counts.failed,
    )


def maybe_resume_on_boot(
    *,
    load_ledger: Callable[[], LedgerLike],
    resume: Callable[[IncompletePull], None],
    enabled: bool | None = None,
    intent_present: Callable[[], bool] | None = None,
    resume_intent: Callable[[], None] | None = None,
    on_error: Callable[[Exception], None] | None = None,
) -> ResumeVerdict:
    """Resume an in-progress pull at boot iff the flag is ON + work is outstanding — **never raises**.

    The one boot-time entry point (wired into the dashboard / CLI startup behind
    the flag). The sequence, returning exactly one :class:`ResumeVerdict`:

    0. **Intent exclusivity** (DEC-PULL-INTENT-RESUME-001 — the ONE decision
       point with the intent-backed pipeline resume). When BOTH
       *intent_present* AND *resume_intent* are wired and *intent_present()*
       reads ``True`` (a durable ``.pull_intent.json`` exists and the
       ``BRUTEX_PULL_INTENT_RESUME`` escape hatch is not OFF), fire
       *resume_intent* EXCLUSIVELY ⇒ :attr:`ResumeVerdict.INTENT_RESUMED` —
       the legacy CLI child below is NOT spawned this boot (the both-firing
       ``flock`` race is impossible), and this branch deliberately IGNORES the
       legacy default-OFF env flag (the intent file is operator-click
       evidence). A raising *resume_intent* ⇒ :attr:`ResumeVerdict.ERROR`
       (still no legacy child — exclusivity holds even on failure); a raising
       *intent_present* probe is logged and falls through to the legacy path.
       With no intent (or the pair unwired — every pre-existing caller) the
       steps below are byte-identical.
    1. **Gate.** *enabled* (default ``None`` ⇒ read
       :func:`resolve_auto_resume_on_boot`) OFF ⇒ :attr:`ResumeVerdict.DISABLED`
       — nothing is loaded, nothing fires. This is the byte-identical
       default-OFF path a dev / CI boot takes.
    2. **Detect.** Call *load_ledger* (the durable-ledger read, injected so this
       module never imports the store) then :func:`detect_incomplete_pull`. No
       outstanding work ⇒ :attr:`ResumeVerdict.NOTHING_TO_RESUME`.
    3. **Resume.** Fire *resume* with the :class:`IncompletePull` verdict (the
       injected action — e.g. launch the single-flight background pull job /
       spawn ``brutex fetch --until-clean``) ⇒ :attr:`ResumeVerdict.RESUMED`.
    4. **Guard.** ANY exception from *load_ledger* / *detect* / *resume* is
       CAUGHT, logged (``auto_resume.error``), and returned as
       :attr:`ResumeVerdict.ERROR` — a boot can NEVER be crashed or blocked by
       the resume attempt (Golden Rule #10); the next boot re-tries. When
       *on_error* is wired (DEC-AUTO-RESUME-ZERO-SURPRISES-001,
       announce-on-failure) the guard ALSO fires it with the caught exception
       — the dashboard binds
       :func:`~brutex.dashboard.pull_intent.build_resume_error_hook` (red
       banner when a store exists + the CRITICAL Telegram alert), so a resume
       that cannot even launch is never log-only. The SAME hook fires from the
       intent-arm's own error branch (step 0). Best-effort: a raising
       *on_error* is caught + logged (``auto_resume.on_error_failed``) and the
       verdict is unchanged — the hook can never crash the boot thread either.

    Best-effort + observable: each outcome emits ONE structured log line so the
    operator sees on boot whether a resume fired, was skipped, or errored. O(1)
    beyond the single ledger fold. The *resume* action is the caller's — it owns
    idempotence (re-pulling a settled window is a no-op; only unsettled windows
    re-pull), so this helper is purely the DECISION.
    """

    def _fire_on_error(exc: Exception) -> None:
        """Announce-on-failure hook dispatch — best-effort, never raises.

        DEC-AUTO-RESUME-ZERO-SURPRISES-001: the log-only ``auto_resume.error``
        guard gains an operator-facing surface (banner + Telegram via the
        injected hook). Its own failure is caught here so the verdict path
        stays exactly the pre-hook behaviour.
        """
        if on_error is None:
            return
        try:
            on_error(exc)
        except Exception as hook_exc:  # pylint: disable=broad-exception-caught — best-effort, never crash boot
            _LOG.warning(
                "auto_resume.on_error_failed",
                error=str(hook_exc),
                error_type=type(hook_exc).__name__,
            )

    if intent_present is not None and resume_intent is not None:
        try:
            has_intent = intent_present()
        except Exception as exc:  # pylint: disable=broad-exception-caught — best-effort, never crash boot
            # An unreadable intent probe must not kill the LEGACY resume too —
            # fall through to the byte-identical legacy path below.
            _LOG.warning(
                "auto_resume.intent_check_error",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            has_intent = False
        if has_intent:
            _LOG.info("auto_resume.intent_exclusive")
            try:
                resume_intent()
            except Exception as exc:  # pylint: disable=broad-exception-caught — best-effort, never crash boot
                # Exclusivity holds even on failure: the legacy CLI child is
                # NOT spawned this boot (it would race the pipeline resume's
                # flock on the next attempt); the next boot re-tries.
                _LOG.warning("auto_resume.error", error=str(exc), error_type=type(exc).__name__)
                _fire_on_error(exc)
                return ResumeVerdict.ERROR
            return ResumeVerdict.INTENT_RESUMED
    active = resolve_auto_resume_on_boot() if enabled is None else enabled
    if not active:
        _LOG.info("auto_resume.disabled", env=AUTO_RESUME_ON_BOOT_ENV)
        return ResumeVerdict.DISABLED
    try:
        ledger = load_ledger()
        incomplete = detect_incomplete_pull(ledger)
        if incomplete is None:
            _LOG.info("auto_resume.nothing_to_resume")
            return ResumeVerdict.NOTHING_TO_RESUME
        _LOG.info(
            "auto_resume.resuming",
            total=incomplete.total,
            settled=incomplete.settled,
            outstanding=incomplete.outstanding,
            planned=incomplete.planned,
            failed=incomplete.failed,
        )
        resume(incomplete)
    except Exception as exc:  # pylint: disable=broad-exception-caught — best-effort, never crash boot
        # Reliability over everything: a boot MUST NOT be blocked or crashed by a
        # failed resume trigger (an unreadable ledger, a resume action that raises).
        # Log it and return ERROR; the next boot re-tries idempotently. The
        # injected on_error hook (banner + alert) makes the failure VISIBLE —
        # previously this guard was log-only (DEC-AUTO-RESUME-ZERO-SURPRISES-001).
        _LOG.warning("auto_resume.error", error=str(exc), error_type=type(exc).__name__)
        _fire_on_error(exc)
        return ResumeVerdict.ERROR
    return ResumeVerdict.RESUMED
