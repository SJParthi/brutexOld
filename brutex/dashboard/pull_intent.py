"""Durable pull-intent + dashboard startup auto-resume (DEC-PULL-INTENT-RESUME-001).

A dashboard ``/pull-pipeline`` pull is a :class:`~brutex.dashboard.pull_jobs.BackgroundPullJob`
whose only in-flight record is process memory: a SIGKILL / power loss leaves the
durable window ledger with re-pullable ``PLANNED`` / ``FAILED`` rows but NOTHING
that re-fires the pull — the operator had to re-click. This module supplies the
durable half:

* :class:`PullIntent` — the COMPLETE re-fire payload of a pipeline pull. The
  ``/pull-pipeline`` request is fully reconstructible from the four query params
  ``(symbols, symbol, from_date, to_date)`` (everything else — targets, lake
  root, years, intervals — is re-derived inside the route), so those four
  strings plus a crash-loop ``restarts`` counter are the whole intent.
* :class:`PullIntentStore` — an atomic ``<lake_root>/.pull_intent.json`` sidecar
  (written via the COMMON :func:`~brutex.data.atomic_io.atomic_write_simple`,
  CLAUDE.md §11 — no new atomic-write copy). Written at REAL pull launch (a
  ``dry_run`` NEVER persists an intent), cleared by the job's ``on_finally``
  hook when the worker thread actually exits (success AND failure), so an
  intent file surviving a boot == a pull that died mid-flight.
* :func:`run_auto_resume` — the 100 %-tested boot decision: probe the
  account-wide pull single-flight ``flock`` (kernel-released on process death —
  a still-running PEER pull is never disturbed and never burns a restart
  token), bump the restart counter (crash-safe: bump BEFORE launch), refuse at
  :data:`MAX_AUTO_RESTARTS` with ONE CRITICAL alert + a red-banner payload, and
  otherwise re-fire the SAME pipeline pull through the injected launcher.

**Relation to DEC-AUTO-RESUME-ON-BOOT-001** (:mod:`brutex.orchestrator.auto_resume`):
that legacy layer is the default-OFF, SPOT-INDEX-ONLY CLI-child resume. This
intent-backed resume EXTENDS (never duplicates) it at the ONE decision point in
``maybe_resume_on_boot``: when an intent file is present the pipeline resume
runs EXCLUSIVELY and the legacy CLI child is NOT spawned that boot (the
both-firing ``flock`` race is impossible); with no intent the legacy path is
byte-identical. The intent resume defaults ON — the intent file is itself
operator-click evidence (it exists only because a REAL pull was launched and
never finished) — with :data:`PULL_INTENT_RESUME_ENV` (``=0``) as the escape
hatch.

HONEST envelope (Golden Rule #10): the resume re-fires the FULL pipeline (spot
→ metadata → historical → prebuild → movers warm), so its success criteria are
the pipeline's own — derived-TF partitions written + movers warmed — never a
"movers gated" placeholder. A deterministically-crashing pull re-fires at most
:data:`MAX_AUTO_RESTARTS` times by construction; the over-cap intent is KEPT on
disk for forensics (only a later real pull's clean finish clears it). Every
helper here is best-effort at the boot boundary: a corrupt / unreadable intent
is logged + treated as absent (+ deleted best-effort), never a boot crash.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Final

import structlog

from brutex.adapters.groww.pull_singleflight import (
    PullAlreadyRunningError,
    pull_singleflight_lock,
)
from brutex.data.atomic_io import atomic_write_simple

__all__ = [
    "MAX_AUTO_RESTARTS",
    "PULL_INTENT_FILENAME",
    "PULL_INTENT_RESUME_ENV",
    "PullIntent",
    "PullIntentStore",
    "ResumeAction",
    "ResumeBanner",
    "emit_restart_cap_alert",
    "persist_intent_for_launch",
    "probe_pull_singleflight",
    "pull_intent_resume_active",
    "resolve_pull_intent_resume",
    "restart_cap_message",
    "run_auto_resume",
]

#: Structured logger — every resume decision surfaces into ``/logs``.
_LOG = structlog.get_logger(__name__)

#: The durable intent sidecar's filename, directly under the lake root —
#: OUTSIDE ``bars/`` + ``options/`` and not a ``*.parquet``, so no lake scanner
#: ever sees it (mirrors the ``.locks`` sidecar convention).
PULL_INTENT_FILENAME: Final[str] = ".pull_intent.json"

#: Maximum AUTOMATIC restarts of one persisted intent. A pull that keeps dying
#: is a crash loop the operator must see, not a hammer: the boot that finds
#: ``restarts >= MAX_AUTO_RESTARTS`` refuses to re-fire, emits ONE CRITICAL
#: alert, and raises the red banner instead.
MAX_AUTO_RESTARTS: Final[int] = 2

#: Escape-hatch env var for the intent-backed startup resume. Default ON (the
#: intent file only exists because the operator launched a real pull that never
#: finished — re-firing it is completing THEIR click, recorded in
#: DEC-PULL-INTENT-RESUME-001); an explicit falsy value disables.
PULL_INTENT_RESUME_ENV: Final[str] = "BRUTEX_PULL_INTENT_RESUME"

#: The falsy set (case-insensitive) that DISABLES the resume — the complement
#: convention of the sibling default-OFF toggles' truthy set, so the operator
#: writes the same tokens either way (CLAUDE.md §11 COMMON).
_FALSY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})

#: The intent payload's schema version (forward-compat marker; unknown extra
#: keys are ignored on load, a wrong SHAPE is corrupt).
_INTENT_VERSION: Final[int] = 1


def resolve_pull_intent_resume(env: Mapping[str, str] | None = None) -> bool:
    """Resolve the intent-resume escape hatch from the env — default ON.

    Reads :data:`PULL_INTENT_RESUME_ENV`. Returns ``False`` ONLY for an explicit
    falsy value (``"0"`` / ``"false"`` / ``"no"`` / ``"off"``, case-insensitive);
    unset / blank / anything else keeps the default ON. Default-ON is deliberate
    (unlike the legacy default-OFF ``BRUTEX_AUTO_RESUME_ON_BOOT``): an intent
    file is operator-click evidence — a REAL pull was launched and died — so
    completing it needs no extra opt-in (DEC-PULL-INTENT-RESUME-001). *env*
    defaults to :data:`os.environ`; a test injects its own mapping. O(1).
    """
    source: Mapping[str, str] = os.environ if env is None else env
    raw = source.get(PULL_INTENT_RESUME_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSY


@dataclass(frozen=True, slots=True)
class PullIntent:
    """The durable re-fire payload of one ``/pull-pipeline`` pull — frozen.

    Carries the request quadruple VERBATIM (the raw query strings; the route
    re-derives everything else) plus the crash-loop accounting. A resume always
    re-fires with ``dry_run=False`` — a dry-run never persists an intent in the
    first place (:func:`persist_intent_for_launch`).
    """

    #: The ``?symbols=`` selection string (``None`` ⇒ whole universe / ``symbol``).
    symbols: str | None
    #: The ``?symbol=`` single-selection string (``None`` ⇒ ``symbols`` / universe).
    symbol: str | None
    #: The ``?from_date=`` ISO date string (``None`` ⇒ the canonical default).
    from_date: str | None
    #: The ``?to_date=`` ISO date string (``None`` ⇒ the canonical default).
    to_date: str | None
    #: How many AUTOMATIC restarts have already re-fired this intent.
    restarts: int = 0
    #: UTC ISO timestamp of the ORIGINAL operator launch (forensics only).
    created_at: str = ""

    def to_payload(self) -> dict[str, Any]:
        """Serialise to the JSON payload shape (adds the schema version). Pure."""
        return {
            "version": _INTENT_VERSION,
            "symbols": self.symbols,
            "symbol": self.symbol,
            "from_date": self.from_date,
            "to_date": self.to_date,
            "restarts": self.restarts,
            "created_at": self.created_at,
        }

    @classmethod
    def from_payload(cls, payload: object) -> PullIntent | None:
        """Parse a loaded JSON payload; ``None`` on ANY shape violation — pure.

        Strict: the four request fields must each be a string or ``None``,
        ``restarts`` a non-negative real ``int`` (a JSON ``true`` is NOT a
        count), ``created_at`` a string. Unknown extra keys are ignored
        (forward-compat); a violation means a corrupt / foreign file, which the
        caller treats as absent (never a fabricated pull — Golden Rule #1).
        """
        if not isinstance(payload, Mapping):
            return None
        fields: dict[str, str | None] = {}
        for key in ("symbols", "symbol", "from_date", "to_date"):
            raw = payload.get(key)
            if raw is not None and not isinstance(raw, str):
                return None
            fields[key] = raw
        restarts = payload.get("restarts")
        if isinstance(restarts, bool) or not isinstance(restarts, int) or restarts < 0:
            return None
        created_at = payload.get("created_at", "")
        if not isinstance(created_at, str):
            return None
        return cls(
            symbols=fields["symbols"],
            symbol=fields["symbol"],
            from_date=fields["from_date"],
            to_date=fields["to_date"],
            restarts=restarts,
            created_at=created_at,
        )


class ResumeBanner:
    """Thread-safe holder for the resume RED-BANNER payload — the UI's data source.

    Set by :func:`run_auto_resume` on an over-cap refusal / failed launch and
    served by the ``/api/v1/liveproof`` route as a ``resume`` section, which the
    site-wide ``bx-liveproof.js`` pill renders as the existing red top banner
    (§11 COMMON — reuse the ONE banner surface, no second pill script).
    AUTO-CLEARED by the next REAL pull launch through
    :func:`persist_intent_for_launch` (DEC-RESUME-BANNER-AUTOCLEAR-001) — the
    banner's remediation is exactly that manual re-launch, so following it
    retires the banner instead of leaving a stale CRITICAL up for the process
    lifetime. O(1) set / clear / snapshot under a :class:`threading.Lock` (the
    boot daemon thread writes, request threads read).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._payload: dict[str, str] | None = None

    def set(self, *, level: str, message: str) -> None:
        """Publish the banner payload (``level`` e.g. ``"critical"``)."""
        with self._lock:
            self._payload = {"level": level, "message": message}

    def clear(self) -> bool:
        """Drop the banner payload (idempotent); ``True`` iff one was present.

        The return value lets the ONE auto-clear site
        (:func:`persist_intent_for_launch`, DEC-RESUME-BANNER-AUTOCLEAR-001)
        log only a REAL clear — a no-op clear on an unset banner stays silent.
        """
        with self._lock:
            was_set = self._payload is not None
            self._payload = None
            return was_set

    def snapshot(self) -> dict[str, str] | None:
        """Return a COPY of the current payload, or ``None`` when unset."""
        with self._lock:
            return None if self._payload is None else dict(self._payload)


class PullIntentStore:
    """The atomic ``.pull_intent.json`` sidecar under one lake root — best-effort I/O.

    Every method is boot-safe: :meth:`load` / :meth:`clear` / :meth:`write`
    NEVER raise (a failure is logged and degraded — an unreadable intent is
    treated as absent, a failed write skips the durable record but never blocks
    the pull). The write is atomic (tmp → fsync → ``os.replace`` via the COMMON
    :func:`~brutex.data.atomic_io.atomic_write_simple`), so a torn / truncated
    intent file is impossible from OUR writer; a corrupt file therefore means
    foreign interference and is deleted best-effort on load.

    Parameters
    ----------
    lake_root
        The lake root the intent guards — the SAME root whose account-wide
        pull single-flight ``flock`` the resume probes, so the "who died here?"
        signal and the "is anyone still pulling here?" signal can never point
        at two different lakes.
    """

    def __init__(self, lake_root: Path) -> None:
        self._lake_root = lake_root
        self._path = lake_root / PULL_INTENT_FILENAME
        #: The resume red-banner holder — rides the store so ONE injected
        #: object carries both the durable intent and its UI surface.
        self.banner = ResumeBanner()

    @property
    def lake_root(self) -> Path:
        """The lake root this store's intent file lives under."""
        return self._lake_root

    @property
    def path(self) -> Path:
        """The intent file path (``<lake_root>/.pull_intent.json``)."""
        return self._path

    def write(self, intent: PullIntent) -> bool:
        """Persist *intent* atomically; return whether it was written — never raises.

        A failed write (unwritable volume, missing root) is logged
        (``pull_intent.write_failed``) and returns ``False`` — the pull itself
        must never be blocked by its best-effort crash-recovery sidecar.
        """
        body = json.dumps(intent.to_payload(), sort_keys=True)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_simple(self._path, lambda tmp: tmp.write_text(body, encoding="utf-8"))
        except OSError as exc:
            _LOG.warning(
                "pull_intent.write_failed",
                path=str(self._path),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return False
        return True

    def load(self) -> PullIntent | None:
        """Read the persisted intent, or ``None`` — never raises.

        A missing file is the normal "no pull died" answer. An unreadable file
        logs ``pull_intent.read_failed`` and reads as absent. A corrupt file
        (non-JSON / wrong shape) logs ``pull_intent.corrupt``, is DELETED
        best-effort (it can never become readable again and must not re-trip
        every boot), and reads as absent — a boot can never crash here.
        """
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            _LOG.warning(
                "pull_intent.read_failed",
                path=str(self._path),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return None
        try:
            payload: object = json.loads(raw)
        except ValueError:
            payload = None
        intent = PullIntent.from_payload(payload)
        if intent is None:
            _LOG.warning("pull_intent.corrupt", path=str(self._path))
            self.clear()
            return None
        return intent

    def clear(self) -> None:
        """Delete the intent file (idempotent, best-effort) — never raises.

        Wired as the :class:`~brutex.dashboard.pull_jobs.BackgroundPullJob`
        ``on_finally`` hook, so it fires when the worker thread actually exits
        — success AND failure — BEFORE the single-flight release (a peer
        acquiring the freed ``flock`` never sees a live intent).
        """
        try:
            self._path.unlink(missing_ok=True)
        except OSError as exc:
            _LOG.warning(
                "pull_intent.clear_failed",
                path=str(self._path),
                error=str(exc),
                error_type=type(exc).__name__,
            )

    def bump_restarts(self) -> PullIntent | None:
        """Increment + persist the restart counter; return the bumped intent.

        Load → ``restarts + 1`` → atomic write, so the token is burnt BEFORE
        the launch (crash-safe: a resume that itself dies mid-launch still
        counted). ``None`` when no intent is loadable (raced away / corrupt) —
        the caller then has nothing to resume.
        """
        intent = self.load()
        if intent is None:
            return None
        bumped = replace(intent, restarts=intent.restarts + 1)
        self.write(bumped)
        return bumped


def persist_intent_for_launch(
    store: PullIntentStore,
    *,
    dry_run: bool,
    symbols: str | None,
    symbol: str | None,
    from_date: str | None,
    to_date: str | None,
    restarts: int = 0,
) -> bool:
    """Persist the launch intent iff this is a REAL pull; return whether written.

    The ONE dry-run decision point (DEC-PULL-INTENT-RESUME-001): a ``dry_run``
    NEVER persists an intent — and, symmetrically, the caller must then wire NO
    ``on_finally`` clear, so a dry-run can never erase a crashed REAL pull's
    intent. *restarts* threads the resume path's already-bumped counter through
    the re-fired launch (a fresh operator click passes the default ``0``) —
    without it a resumed launch would reset the cap and loop forever.

    **Banner auto-clear (DEC-RESUME-BANNER-AUTOCLEAR-001):** a REAL launch also
    DROPS any stale resume red banner on ``store.banner`` — the banner's own
    remediation is "start the pull manually from /universe", so the manual
    re-launch it asked for must retire it instead of leaving a CRITICAL banner
    up for the rest of the process lifetime (no production path ever cleared
    it before). Deliberately independent of the intent-write outcome (a failed
    best-effort write never blocks the pull, and the operative fact — a real
    pull is launching NOW — supersedes the banner either way) and BEFORE the
    write (so a crash between the two can only leave the honest state: banner
    gone, fresh intent pending the crashed pull's next resume). A ``dry_run``
    clears NOTHING (byte-identical early return — a synthetic pull resolves no
    crash loop). A REAL clear logs ``pull_intent.banner_autocleared`` once; an
    unset banner stays silent.
    """
    if dry_run:
        return False
    if store.banner.clear():
        _LOG.info(
            "pull_intent.banner_autocleared",
            path=str(store.path),
            reason="real_pull_launched",
        )
    intent = PullIntent(
        symbols=symbols,
        symbol=symbol,
        from_date=from_date,
        to_date=to_date,
        restarts=restarts,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    return store.write(intent)


def probe_pull_singleflight(lake_root: Path) -> bool:
    """Cheap acquire-probe of the account-wide pull ``flock``; ``True`` iff acquirable.

    Try-acquire → immediately release (:func:`~brutex.adapters.groww.pull_singleflight.pull_singleflight_lock`
    releases on ``with``-exit). A held lock means a PEER pull is genuinely
    running (the kernel releases a dead process's ``flock``), so the resume
    must not fire — and, crucially, must not burn a restart token or mark a
    fresh job FAILED at boot (``BackgroundPullJob._acquire_singleflight`` fails
    the progress on contention). Residual TOCTOU is documented and accepted:
    ``start()`` re-checks, worst case one noisy refused job. ANY probe error
    (disk-full ``LakeWriteHalt``, an unreadable ``.locks`` dir) is logged and
    read as NOT acquirable — when uncertain, do not fire (best-effort boot path,
    never a crash).
    """
    try:
        with pull_singleflight_lock(lake_root):
            pass
    except PullAlreadyRunningError:
        return False
    except Exception as exc:  # pylint: disable=broad-exception-caught — best-effort boot probe
        _LOG.warning(
            "pull_intent.probe_failed",
            lake_root=str(lake_root),
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return False
    return True


def pull_intent_resume_active(store: PullIntentStore, env: Mapping[str, str] | None = None) -> bool:
    """``True`` iff the escape hatch is ON **and** a readable intent is present.

    The ONE decision predicate the extended
    :func:`~brutex.orchestrator.auto_resume.maybe_resume_on_boot` consults: when
    it reads ``True`` the pipeline resume runs EXCLUSIVELY (the legacy CLI child
    is not spawned that boot); ``False`` keeps the legacy path byte-identical.
    The env gate is checked FIRST so a disabled resume never even reads the file.
    """
    if not resolve_pull_intent_resume(env):
        return False
    return store.load() is not None


def restart_cap_message(intent: PullIntent, max_restarts: int) -> str:
    """The operator-facing over-cap refusal message (banner + log) — pure."""
    scope = intent.symbols or intent.symbol or "whole universe"
    return (
        f"pull auto-resume HALTED: {intent.restarts} automatic restart(s) already "
        f"fired for the pipeline pull (targets: {scope}, from: {intent.from_date or 'default'}, "
        f"to: {intent.to_date or 'default'}) — cap is {max_restarts}. The pull keeps "
        "dying; NOT re-firing. Investigate the crash loop, then start the pull "
        f"manually from /universe (the intent file {PULL_INTENT_FILENAME} is kept for forensics)."
    )


def emit_restart_cap_alert(intent: PullIntent) -> None:
    """Fire the ONE CRITICAL over-cap Telegram alert — best-effort, never raises.

    Reuses the existing public
    :meth:`~brutex.notifications.sweep_alerts.SweepAlerter.pull_halted` surface
    (reason ``"pull_restart_cap"`` — its reason-scoped dedupe coalesces a
    repeat refusal of the same cause). A disabled notifier is a silent no-op;
    an unexpected error is logged + swallowed so the boot thread never dies.
    """
    from brutex.notifications import sweep_alerts

    try:
        sweep_alerts.SweepAlerter().pull_halted(reason="pull_restart_cap")
    except Exception:  # pylint: disable=broad-exception-caught — best-effort alert, never crash boot
        _LOG.exception("pull_intent.restart_cap_alert_failed; the refusal is unaffected")
    _LOG.warning(
        "pull_intent.restart_cap",
        restarts=intent.restarts,
        cap=MAX_AUTO_RESTARTS,
        symbols=intent.symbols,
        symbol=intent.symbol,
    )


class ResumeAction(Enum):
    """The terminal outcome of one :func:`run_auto_resume` call — never raises."""

    #: No readable intent — nothing died here; the boot proceeds untouched.
    NO_INTENT = "no_intent"
    #: The single-flight ``flock`` is held — a PEER pull is genuinely running.
    #: No restart token burnt, no job failed, intent left for the peer's clear.
    PEER_ACTIVE = "peer_active"
    #: ``restarts >= max_restarts`` — refused; CRITICAL alert + red banner
    #: raised, intent KEPT on disk for forensics.
    OVER_CAP = "over_cap"
    #: The launch was attempted but raised — logged + red banner; a
    #: deterministic validation error (``ValueError`` — e.g. a stale selection
    #: that no longer resolves) also CLEARS the intent so it can never loop.
    LAUNCH_FAILED = "launch_failed"
    #: The persisted pull was re-fired (restart token bumped BEFORE launch).
    FIRED = "fired"


def run_auto_resume(
    store: PullIntentStore,
    *,
    launch: Callable[[PullIntent], None],
    probe_singleflight: Callable[[], bool] | None = None,
    alert: Callable[[PullIntent], None] | None = None,
    max_restarts: int = MAX_AUTO_RESTARTS,
) -> ResumeAction:
    """The whole boot-time intent-resume decision + dispatch — **never raises**.

    The 100 %-tested core the dashboard's thin ``app.state.brutex_resume_pipeline``
    seam delegates to (every load-bearing decision lives HERE, not in the
    pragma-thin wiring). Sequence, returning exactly one :class:`ResumeAction`:

    1. **Load.** No readable intent ⇒ :attr:`ResumeAction.NO_INTENT`.
    2. **Probe.** The account-wide single-flight ``flock`` is NOT acquirable ⇒
       a peer pull is running ⇒ :attr:`ResumeAction.PEER_ACTIVE` — deliberately
       BEFORE the cap check and the bump, so a healthy concurrent system never
       burns a restart token and never fails a fresh job's progress.
    3. **Cap.** ``restarts >= max_restarts`` ⇒ :attr:`ResumeAction.OVER_CAP`:
       fire *alert* once (default :func:`emit_restart_cap_alert` — the CRITICAL
       Telegram surface), raise the red banner, KEEP the intent for forensics.
    4. **Bump.** Persist ``restarts + 1`` BEFORE launching (crash-safe: a
       resume that itself dies still counted). A bump that finds the intent
       gone (raced away) ⇒ :attr:`ResumeAction.NO_INTENT`.
    5. **Launch.** Fire *launch* with the bumped intent ⇒
       :attr:`ResumeAction.FIRED`. A ``ValueError`` (a deterministic validation
       refusal — stale selection, bad dates) CLEARS the intent + banners; any
       other exception keeps the intent (the next boot re-tries, bounded by the
       cap) + banners — either way :attr:`ResumeAction.LAUNCH_FAILED`, never a
       raise out of the boot thread.

    Parameters
    ----------
    store
        The lake's intent store (also carries the banner holder).
    launch
        Re-fires the pipeline pull from the intent (the dashboard binds its
        ``/pull-pipeline`` launcher with ``dry_run=False`` and the intent's
        already-bumped ``restarts`` threaded through, so the re-written intent
        keeps the cap honest).
    probe_singleflight
        Zero-arg acquirability probe; ``None`` (production) binds
        :func:`probe_pull_singleflight` on ``store.lake_root``.
    alert
        Over-cap CRITICAL alert hook; ``None`` binds :func:`emit_restart_cap_alert`.
    max_restarts
        The automatic-restart cap (default :data:`MAX_AUTO_RESTARTS`).
    """
    intent = store.load()
    if intent is None:
        _LOG.info("pull_intent.no_intent", path=str(store.path))
        return ResumeAction.NO_INTENT
    probe = (
        probe_singleflight
        if probe_singleflight is not None
        else (lambda: probe_pull_singleflight(store.lake_root))
    )
    if not probe():
        _LOG.info(
            "pull_intent.peer_active",
            lake_root=str(store.lake_root),
            restarts=intent.restarts,
        )
        return ResumeAction.PEER_ACTIVE
    if intent.restarts >= max_restarts:
        store.banner.set(level="critical", message=restart_cap_message(intent, max_restarts))
        alert_fn = alert if alert is not None else emit_restart_cap_alert
        try:
            alert_fn(intent)
        except Exception:  # pylint: disable=broad-exception-caught — best-effort alert, never crash boot
            _LOG.exception("pull_intent.alert_failed; the over-cap refusal is unaffected")
        return ResumeAction.OVER_CAP
    bumped = store.bump_restarts()
    if bumped is None:
        _LOG.warning("pull_intent.bump_lost", path=str(store.path))
        return ResumeAction.NO_INTENT
    try:
        launch(bumped)
    except ValueError as exc:
        # A deterministic validation refusal (stale selection / bad dates): the
        # SAME intent can never launch, so clear it — re-firing would loop.
        store.clear()
        store.banner.set(
            level="critical",
            message=(
                f"pull auto-resume refused: {exc} — the saved pull selection no longer "
                "resolves; intent cleared. Start the pull manually from /universe."
            ),
        )
        _LOG.warning("pull_intent.launch_refused", error=str(exc))
        return ResumeAction.LAUNCH_FAILED
    except Exception as exc:  # pylint: disable=broad-exception-caught — best-effort boot, never crash
        # A non-deterministic launch failure (e.g. credentials not yet fixed):
        # KEEP the intent — the next boot re-tries, bounded by the cap (the
        # token for THIS attempt is already burnt above).
        store.banner.set(
            level="critical",
            message=(
                f"pull auto-resume launch failed: {type(exc).__name__}: {exc} — will "
                f"retry on the next restart (at most {max_restarts} automatic restarts)."
            ),
        )
        _LOG.warning("pull_intent.launch_failed", error=str(exc), error_type=type(exc).__name__)
        return ResumeAction.LAUNCH_FAILED
    _LOG.info(
        "pull_intent.resumed",
        restarts=bumped.restarts,
        symbols=bumped.symbols,
        symbol=bumped.symbol,
        from_date=bumped.from_date,
        to_date=bumped.to_date,
    )
    return ResumeAction.FIRED
