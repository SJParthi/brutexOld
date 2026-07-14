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
that legacy layer is the default-OFF, ledger-triggered boot resume — since
DEC-AUTO-RESUME-INPROCESS-001 routed IN-PROCESS through
:func:`run_legacy_resume` (the historical spawned ``fetch --until-clean`` CLI
child, which was 3-index-spot-only, is DELETED). HONEST scope note: the legacy
boot-resume therefore re-plans the WHOLE universe (spot → metadata →
historical → prebuild → movers), not just the unsettled windows the old child
targeted — bounded by the idempotent keep-first sink + the unchanged pacing /
single-flight guards, so it costs exactly what one manual pull click does.
This intent-backed resume EXTENDS (never duplicates) it at the ONE decision
point in ``maybe_resume_on_boot``: when an intent file is present the pipeline
resume runs EXCLUSIVELY and the legacy in-process resume is NOT fired that
boot (the both-firing ``flock`` race is impossible); with no intent the legacy
path decides from the ledger exactly as before. The intent resume defaults ON
— the intent file is itself operator-click evidence (it exists only because a
REAL pull was launched and never finished) — with
:data:`PULL_INTENT_RESUME_ENV` (``=0``) as the escape hatch.

HONEST envelope (Golden Rule #10): the resume re-fires the FULL pipeline (spot
→ metadata → historical → prebuild → movers warm), so its success criteria are
the pipeline's own — derived-TF partitions written + movers warmed — never a
"movers gated" placeholder. A deterministically-crashing pull re-fires at most
:data:`MAX_AUTO_RESTARTS` times by construction; the over-cap intent is KEPT on
disk for forensics (only a later real pull's clean finish clears it). Every
helper here is best-effort at the boot boundary: a corrupt / unreadable intent
is logged + treated as absent (+ deleted best-effort), never a boot crash —
and never a HANG either: :func:`read_sidecar_bytes` (the shared #1614-round-6
regular-file-only loader this store AND the auto-pull scheduler's state store
read through) opens ``O_NONBLOCK`` + ``S_ISREG``-checks the descriptor, so a
non-regular inode (FIFO / device symlink / socket / directory) planted at the
sidecar folds into the same corrupt-file answer instead of blocking the read
syscall forever (refuter round 7 P-1), and the 16 MiB cap is checked on the
``fstat`` of the SAME open descriptor (no stat→read swap window — P-2).
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
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

# DEC-SETTLE-EVIDENCE-ADOPTION-001 (MIGRATION.md #1792): the round-7 P-1/P-2
# regular-file-only loader MOVED VERBATIM to the COMMON leaf
# brutex/data/sidecar_read.py (the calendars-layer settle-evidence loader must
# not import the dashboard package; a copy is refused by CLAUDE.md §11 — the
# #1715 leaf+shim precedent). These re-exports keep every existing consumer
# byte-compatible; ``__all__`` still lists both names.
from brutex.data.sidecar_read import SidecarReadResult, read_sidecar_bytes

__all__ = [
    "MAX_AUTO_RESTARTS",
    "PULL_INTENT_FILENAME",
    "PULL_INTENT_RESUME_ENV",
    "PullIntent",
    "PullIntentStore",
    "ResumeAction",
    "ResumeBanner",
    "SidecarReadResult",
    "clear_banner_after_start",
    "emit_restart_cap_alert",
    "persist_intent_for_launch",
    "probe_pull_singleflight",
    "pull_intent_resume_active",
    "read_sidecar_bytes",
    "resolve_pull_intent_resume",
    "restart_cap_message",
    "restore_intent_after_refused_start",
    "run_auto_resume",
    "run_legacy_resume",
    "synthesize_legacy_intent",
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

#: Size cap on the intent file, checked on the ``fstat`` of the OPEN descriptor
#: (refuter round 6 N-1 introduced the cap on a separate pre-read ``stat``;
#: refuter round 7 P-1/P-2 moved it onto the fd — the crossverify #1776
#: ``MAX_JOURNAL_BYTES`` precedent for the value): a real intent is ~250 bytes;
#: a planted multi-GB file reads as corrupt instead of being read whole, and
#: the fstat-on-fd cap leaves no stat→read swap window.
MAX_INTENT_BYTES: Final[int] = 16 * 1024 * 1024


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
    re-derives everything else) plus the OPTIONAL scope token
    (DEC-PULL-SCOPED-ALL-ONECLICK-001) and the crash-loop accounting. A resume
    always re-fires with ``dry_run=False`` — a dry-run never persists an intent
    in the first place (:func:`persist_intent_for_launch`).
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
    #: The ``?scope=`` token (DEC-PULL-SCOPED-ALL-ONECLICK-001) — ``None`` ⇒ the
    #: full chain (every pre-scope intent file, back-compat). Persisted so a
    #: crashed spot-only pull resumes SPOT-ONLY, never a silently-widened
    #: full-scope F&O pull (the optional-field back-compat template).
    #:
    #: NAMED RESIDUAL (refuter round 1, r2 MINOR/NIT-4 — the DOWNGRADE
    #: direction): that law holds only on scope-AWARE code. PRE-scope code
    #: (an operator ROLLBACK across a crashed spot-only pull) ignores this
    #: key entirely, so the boot-time resume re-fires as a FULL-scope F&O
    #: pull — the pre-feature behavior with every rail (market-hours guard,
    #: single-flight, ledger idempotence) intact, but an operator-unauthorized
    #: scope WIDENING nonetheless. Remediation: do not roll back across a
    #: crashed spot-only pull, or clear the intent file first
    #: (``docs/runbooks/pull-all-spot.md`` §5).
    scope: str | None = None

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
            "scope": self.scope,
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
        # ``scope`` is the OPTIONAL fifth request field (DEC-PULL-SCOPED-ALL-
        # ONECLICK-001): absent on every pre-scope intent file ⇒ ``None`` ⇒ the
        # full chain (back-compat); a non-string value is corrupt like the rest.
        for key in ("symbols", "symbol", "from_date", "to_date", "scope"):
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
            scope=fields["scope"],
        )


class ResumeBanner:
    """Thread-safe holder for the resume RED-BANNER payload — the UI's data source.

    Set by :func:`run_auto_resume` on an over-cap refusal / failed launch and
    served by the ``/api/v1/liveproof`` route as a ``resume`` section, which the
    site-wide ``bx-liveproof.js`` pill renders as the existing red top banner
    (§11 COMMON — reuse the ONE banner surface, no second pill script).
    AUTO-CLEARED by the next REAL pull launch that actually STARTS, through
    :func:`clear_banner_after_start` (DEC-RESUME-BANNER-AUTOCLEAR-001, gated on
    ``BackgroundPullJob.start`` returning ``True`` per the round-3 hostile
    review) — the banner's remediation is exactly that manual re-launch, so
    following it retires the banner instead of leaving a stale CRITICAL up for
    the process lifetime; a REFUSED start (single-flight contention / a
    foreign-host lease) launches nothing and clears nothing. O(1) set / clear /
    snapshot under a :class:`threading.Lock` (the boot daemon thread writes,
    request threads read).
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
        (:func:`clear_banner_after_start`, DEC-RESUME-BANNER-AUTOCLEAR-001)
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
        #: The launcher's PROCESS-LOCAL intent-transaction mutex (refuter
        #: round 4 G-1 / R4-1, DEC-PULL-SCOPED-ALL-ONECLICK-001 — MIGRATION.md
        #: #1779): the dashboard launcher holds it across its WHOLE
        #: snapshot → persist → ``start()`` → restore span, so two concurrent
        #: launcher threads can never interleave their snapshot/persist/restore
        #: steps (a stale ``prior=None`` snapshot orphan-clearing a RUNNING
        #: winner's freshly-persisted intent; a second refused click
        #: re-installing the first's clobber). Rides the store so the mutex is
        #: per-INTENT-FILE, exactly like the state it guards; cross-PROCESS
        #: launchers are excluded by the account-wide pull ``flock`` instead.
        #: Safe to hold across ``BackgroundPullJob.start`` — start() never
        #: WAITS on a lock (LOCK_NB flock; refusals return ``False``; the
        #: worker is spawned then start() returns) and NOTHING on the worker
        #: thread (``on_finally`` = :meth:`clear`) ever acquires this mutex.
        #: HONEST hold envelope (refuter round 5, J-NIT-1): the held span's
        #: synchronous I/O bounds the hold — the intent-file + lock-file
        #: syscalls are NOT deadline-wrapped (unbounded on a wedged lake
        #: volume), the SSM credential read rides boto3 default timeouts,
        #: and only the lease ops are deadline-bounded (≈2×5 s) — so a
        #: queued sibling click waits that span (healthy ≈ sub-second)
        #: instead of the pre-mutex instant in-process refusal;
        #: latency-only, same fault domain as the click's own I/O. Typed as
        #: a context manager so test seams may wrap it (the app only ever
        #: ``with``-enters it).
        self.launch_mutex: AbstractContextManager[object] = threading.Lock()

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

        Hardened on the #1614 round-6 loader template via the shared
        :func:`read_sidecar_bytes` (refuter round 7 P-1,
        DEC-PULL-SCOPED-ALL-ONECLICK-001 — supersedes the round-6 N-1
        crossverify-#1776-M1 read, whose plain ``read_bytes`` HUNG forever on
        a non-regular inode: executed, a FIFO / ``/dev/zero`` symlink planted
        at the intent path wedged the launcher's held-mutex ``prior_intent =
        store.load()`` snapshot — a REAL pull-click request thread — with no
        exception for any belt to catch): the open is ``O_NONBLOCK`` +
        ``S_ISREG``-checked on the descriptor (a FIFO / device / socket /
        directory folds into the corrupt-file answer, naming the shape), the
        16 MiB cap (:data:`MAX_INTENT_BYTES`) is checked on the SAME
        ``fstat`` (the stat→read TOCTOU window closed — round-7 P-2), and the
        decode stays INSIDE the corruption-tolerant path so an invalid-UTF-8
        byte (``UnicodeDecodeError``), a nesting-bomb payload
        (``RecursionError``) and an out-of-range numeric (``OverflowError``)
        all fold into the corrupt-file answer too.
        """
        result = read_sidecar_bytes(self._path, max_bytes=MAX_INTENT_BYTES)
        if result.shape == "absent":
            return None
        if result.shape == "read_error":
            _LOG.warning(
                "pull_intent.read_failed",
                path=str(self._path),
                error=result.detail,
                error_type=result.error_type,
            )
            return None
        try:
            payload: object = None if result.raw is None else json.loads(result.raw.decode("utf-8"))
        except (ValueError, RecursionError, OverflowError):
            # ValueError covers junk JSON AND UnicodeDecodeError (its subclass).
            payload = None
        intent = PullIntent.from_payload(payload)
        if intent is None:
            # The deletion below HEALS a planted non-regular inode too:
            # ``clear()`` unlinks a FIFO / a symlink (the LINK, never its
            # target) fine; a directory's unlink fails into the logged
            # best-effort ``pull_intent.clear_failed``.
            _LOG.warning(
                "pull_intent.corrupt",
                path=str(self._path),
                oversized=result.shape == "oversized",
                shape=result.shape if result.shape != "ok" else "undecodable",
                detail=result.detail,
            )
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
    scope: str | None = None,
) -> bool:
    """Persist the launch intent iff this is a REAL pull; return whether written.

    The ONE dry-run decision point (DEC-PULL-INTENT-RESUME-001): a ``dry_run``
    NEVER persists an intent — and, symmetrically, the caller must then wire NO
    ``on_finally`` clear, so a dry-run can never erase a crashed REAL pull's
    intent. *restarts* threads the resume path's already-bumped counter through
    the re-fired launch (a fresh operator click passes the default ``0``) —
    without it a resumed launch would reset the cap and loop forever.

    This function deliberately does NOT touch ``store.banner``: it runs BEFORE
    ``BackgroundPullJob.start`` (the intent must be on disk before any worker
    can crash), where "a real pull is launching" is still only an ATTEMPT — a
    refused start (single-flight contention / a foreign-host lease) launches
    nothing. The resume red banner is retired only AFTER a start that actually
    acquired the guards, by :func:`clear_banner_after_start`
    (DEC-RESUME-BANNER-AUTOCLEAR-001, round-3 hostile-review fold).
    """
    if dry_run:
        return False
    intent = PullIntent(
        symbols=symbols,
        symbol=symbol,
        from_date=from_date,
        to_date=to_date,
        restarts=restarts,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        # A non-default scope rides the intent so a crash-resume re-fires the
        # SAME scope (DEC-PULL-SCOPED-ALL-ONECLICK-001; ``None`` = full).
        scope=scope,
    )
    return store.write(intent)


def clear_banner_after_start(
    store: PullIntentStore,
    *,
    dry_run: bool,
    started: bool,
) -> bool:
    """Auto-clear the resume red banner iff a REAL pull actually STARTED.

    The ONE banner auto-clear site (DEC-RESUME-BANNER-AUTOCLEAR-001, as
    tightened by the round-3 hostile review): the banner's own remediation is
    "start the pull manually from /universe", so the manual re-launch it asked
    for must retire it instead of leaving a CRITICAL banner up for the rest of
    the process lifetime (no production path ever cleared it before). "Launch"
    means :meth:`~brutex.dashboard.pull_jobs.BackgroundPullJob.start` acquired
    the in-process + cross-process guards and spawned the worker
    (*started* ``True``) — a REFUSED start (a pull already running in this
    process, lake/account ``flock`` contention, or the foreign-host lease
    refusal) launches nothing, so the crash-loop banner it would have hidden
    stays up (*started* ``False`` clears NOTHING). A ``dry_run`` also clears
    NOTHING — a synthetic pull resolves no crash loop, mirroring
    :func:`persist_intent_for_launch`'s dry-run law. Runs strictly AFTER the
    start (the intent write stays crash-safe BEFORE it — this ordering is why
    the clear cannot live inside :func:`persist_intent_for_launch`).

    Returns ``True`` iff a banner payload was actually dropped; a REAL clear
    logs ``pull_intent.banner_autocleared`` once, an unset banner stays
    silent. O(1), never raises.
    """
    if dry_run or not started:
        return False
    if store.banner.clear():
        _LOG.info(
            "pull_intent.banner_autocleared",
            path=str(store.path),
            reason="real_pull_started",
        )
        return True
    return False


def restore_intent_after_refused_start(
    store: PullIntentStore,
    *,
    prior: PullIntent | None,
    dry_run: bool,
    started: bool,
) -> bool:
    """Undo the pre-start intent persist when the start was REFUSED — never raises.

    Refuter round 3 F-1 (DEC-PULL-SCOPED-ALL-ONECLICK-001, MIGRATION.md #1779):
    :func:`persist_intent_for_launch` deliberately runs BEFORE
    ``BackgroundPullJob.start`` (pre-crash durability — a SIGKILL between the
    worker spawn and a later persist would leave a running-then-dead pull with
    NO durable intent, and the ``on_finally`` clear must be wired INTO
    ``start()``), so a REFUSED start (a pull already RUNNING in-process, lake
    ``flock`` contention, or the foreign-host lease) has already CLOBBERED the
    on-disk intent with the refused click's payload. Executed proof: a refused
    FULL-scope click during a live spot-only pull left ``scope=null`` on disk,
    and a crash in that window resumed the WRONG scope (the F&O metadata spine
    entered). The clobber CLASS is inherited (pre-scope it clobbered
    ``symbols``/``from_date``/``to_date``/``restarts`` the same way); this
    restore closes every dimension at once.

    *prior* is the caller's snapshot of the on-disk intent taken BEFORE its
    persist. On a refused REAL start (``started=False``, not *dry_run*):

    * current on-disk intent ABSENT → leave absent, restore NOTHING (the
      RUNNING winner's ``on_finally`` clear raced between the persist and this
      restore — its pull finished, so "no intent" IS the correct durable
      record; writing *prior* back would resurrect an intent for a completed
      pull);
    * *prior* is ``None`` → :meth:`~PullIntentStore.clear` the orphan clobber
      (the foreign-flock refusal shape: nothing in-process would EVER clear
      it, so a later boot would "resume" a pull that never started);
    * else → re-write *prior* VERBATIM (``restarts``/``created_at``/``scope``
      included), so a crash in the window resumes the RUNNING pull's
      scope/selection, never the refused click's.

    A started launch or a *dry_run* restores nothing (``False`` —
    :func:`persist_intent_for_launch`'s dry-run law means there is nothing to
    undo). Returns ``True`` iff an on-disk restore/clear was performed.

    SERIALIZATION (refuter round 4 G-1 / R4-1): the snapshot-and-restore
    alone is per-request and non-transactional, so two CONCURRENT launcher
    threads could defeat it (a loser whose stale ``prior=None`` snapshot
    predated the winner's persist orphan-CLEARED the running winner's intent;
    a second refused click re-installed the first's clobber). The launcher
    therefore holds :attr:`PullIntentStore.launch_mutex` across its whole
    snapshot → persist → start → restore span — this function assumes that
    serialization and does not re-lock.

    HONEST residuals (best-effort, the :func:`probe_pull_singleflight` TOCTOU
    posture): (a) the running winner's ``on_finally`` clear fires on the
    WORKER thread — deliberately never under the launch mutex — so it can
    still land between this function's load and its restore write, which
    resurrects the winner's OWN just-finished intent: worst case one
    idempotent boot re-pull of an already-complete pull (zero re-fetched spot
    windows — the ledger settles them — though the metadata phase and the
    default-ON crossverify tail's producer still issue their per-run vendor
    calls), never a wrong scope (a clear landing BEFORE the load takes the
    leave-absent arm — the correct record); (b) on a FAILED restore write the
    refused click's clobber REMAINS on disk (logged
    ``restored="write_failed"`` — the same volume fault usually failed the
    persist too); (c) launcher threads in a SECOND dashboard process on the
    same lake do not share the mutex (off-design — one dashboard per lake;
    the flock still excludes a second RUNNING pull).
    """
    if dry_run or started:
        return False
    if store.load() is None:
        return False
    if prior is None:
        store.clear()
        _LOG.info(
            "pull_intent.refused_start_restored",
            path=str(store.path),
            restored="cleared_orphan",
        )
        return True
    written = store.write(prior)
    _LOG.info(
        "pull_intent.refused_start_restored",
        path=str(store.path),
        restored="prior" if written else "write_failed",
        prior_scope=prior.scope,
        prior_restarts=prior.restarts,
    )
    return written


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


def synthesize_legacy_intent() -> PullIntent:
    """The whole-universe :class:`PullIntent` for the LEGACY ledger-triggered resume.

    DEC-AUTO-RESUME-INPROCESS-001: the legacy boot resume (opted in via
    ``BRUTEX_AUTO_RESUME_ON_BOOT``) is triggered by the durable window ledger's
    outstanding ``PLANNED`` / ``FAILED`` windows, not by a persisted operator
    click — there is no request quadruple to replay, so the synthesized intent
    is the whole-universe default pull (every ``None`` field re-derives inside
    the ``/pull-pipeline`` route, exactly as a bare operator click would).
    ``restarts=0`` — the crash-loop accounting starts fresh; the resume path's
    bump-before-launch burns the first token. Pure beyond one clock read.
    """
    return PullIntent(
        symbols=None,
        symbol=None,
        from_date=None,
        to_date=None,
        restarts=0,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def run_legacy_resume(
    store: PullIntentStore,
    *,
    launch: Callable[[PullIntent], None],
    probe_singleflight: Callable[[], bool] | None = None,
) -> ResumeAction:
    """The LEGACY ledger-triggered boot resume, routed IN-PROCESS through the intent core.

    DEC-AUTO-RESUME-INPROCESS-001 — replaces the deleted
    ``brutex fetch --until-clean`` subprocess child, which was DOA on every
    opted-in boot: BOTH DuckDB files its ``cmd_fetch`` must open are
    exclusively locked against it — ``<lake>/catalog.duckdb`` FIRST (held by
    the dashboard's boot-window L3 lake-heal sweeper — the incident's actual
    first-raising site, ``catalog.py`` ``duckdb.connect``), and one step
    later ``<lake>/instruments.duckdb``, which the dashboard holds READ-WRITE
    for its LIFETIME — so the child exited 1 in ~0.3 s (stderr inherited,
    never captured), stranding the outstanding windows silently. Running
    IN-PROCESS through the dashboard's own ``/pull-pipeline`` launcher opens
    NO second catalog/store handle, so the whole lock class is structurally
    gone.

    Persist a synthesized whole-universe intent (:func:`synthesize_legacy_intent`)
    iff none is already on disk — a pre-existing intent (reachable here only when
    the ``BRUTEX_PULL_INTENT_RESUME`` escape hatch disabled the intent arm) is
    NEVER overwritten, so its request quadruple + crash-loop ``restarts`` count
    survive — then delegate VERBATIM to :func:`run_auto_resume`. The legacy
    resume thereby inherits the whole intent machinery: the single-flight peer
    probe (a quiet :attr:`ResumeAction.PEER_ACTIVE` skip that burns no restart
    token), the :data:`MAX_AUTO_RESTARTS` cap (over-cap ⇒ red
    :class:`ResumeBanner` + ONE CRITICAL Telegram alert via
    :func:`emit_restart_cap_alert`), bump-before-launch crash accounting, and
    the ``ValueError``-clears / ``RuntimeError``-keeps launch-failure semantics.

    A :attr:`ResumeAction.PEER_ACTIVE` skip CLEANS UP the intent iff THIS call
    synthesized it (a pre-existing intent is left exactly as :func:`run_auto_resume`
    left it): the peer pull's own finish settles the ledger, and the NEXT boot's
    ledger detection re-decides the legacy resume — a lingering synthesized
    intent would instead make the next boot's INTENT arm re-fire a
    whole-universe pull unconditionally, ledger-blind. Ownership is RE-DERIVED
    at cleanup time, never assumed from entry-time state: the file is deleted
    only when the CURRENT on-disk intent still exact-equals the synthetic
    :class:`PullIntent` instance this call wrote (frozen-dataclass field
    equality, ``created_at`` included) — an operator who persisted a REAL
    intent between our synthetic write and this cleanup (the boot-window race)
    keeps their click; the mismatch is logged
    (``pull_intent.legacy_cleanup_preserved_foreign_intent``) and nothing is
    deleted. HONEST residual (Golden Rule #10): an operator intent that is
    EXACT-EQUAL to our synthetic one — the whole-universe ``None`` quadruple,
    ``restarts=0``, AND the same second's ``created_at`` — is indistinguishable
    from ours and is cleared; the loss is only the click record (the durable
    window ledger is untouched, so the next boot's ledger detection re-decides
    the same whole-universe pull).

    Raises :class:`RuntimeError` iff the synthesized intent could not be
    persisted (:meth:`PullIntentStore.write` degraded ``False`` — an unwritable
    lake volume): without the durable intent the cap accounting cannot hold, so
    the resume is refused LOUDLY — the raise lands in
    :func:`~brutex.orchestrator.auto_resume.maybe_resume_on_boot`'s guard,
    which logs ``auto_resume.error`` and returns the honest ``error`` verdict
    (never a false ``resumed``; the boot itself is unaffected).
    """
    synthesized: PullIntent | None = None
    if store.load() is None:
        synthesized = synthesize_legacy_intent()
        if not store.write(synthesized):
            raise RuntimeError(
                "legacy auto-resume refused: the synthesized whole-universe intent "
                f"could not be persisted at {store.path} (unwritable lake volume?) — "
                "without the durable intent the restart-cap accounting cannot hold"
            )
    _LOG.info(
        "pull_intent.legacy_resume",
        path=str(store.path),
        synthesized=synthesized is not None,
    )
    action = run_auto_resume(store, launch=launch, probe_singleflight=probe_singleflight)
    if synthesized is not None and action is ResumeAction.PEER_ACTIVE:
        # The peer's finish (not this boot) owns the ledger; drop OUR synthetic
        # intent so the next boot re-decides from the ledger, never an
        # unconditional intent-arm whole-universe re-fire. Ownership is
        # re-derived NOW, not carried from entry time: an operator could have
        # persisted a REAL intent between our synthetic write and this cleanup
        # (boot-window race) — clear only when the CURRENT on-disk intent is
        # still exactly the instance we wrote (frozen-dataclass equality,
        # ``created_at`` included); anything else is theirs and survives.
        if store.load() == synthesized:
            store.clear()
        else:
            _LOG.info(
                "pull_intent.legacy_cleanup_preserved_foreign_intent",
                path=str(store.path),
            )
    return action
