"""The self-arming post-market auto-pull scheduler for the spot-only chain.

DEC-PULL-SCOPED-ALL-ONECLICK-001 (MIGRATION.md #1779, round-5 fold — the
operator hard requirement: ONE click ⇒ fully automatic daily; zero manual
steps after). The dashboard's "▶ Pull Spot" button (the round-6 two-button
split label; formerly "▶ Pull EVERYTHING — spot only") ARMS this
scheduler on its first click (``auto_arm=1`` on the launcher route); from then
on a daemon-thread ticker fires the SAME spot-only launcher path the button
uses — all launcher rails inherited (market-hours guard, single-flight,
durable intent, note surgery, crossverify tail) — once per day, when the
EXISTING :func:`~brutex.calendars.auto_pull_gate.auto_pull_allowed` gate opens
(trading day + post-close settle + Muhurat deferral; reused, never
re-derived).

House daemon precedents followed: the throttled ``StartupSweeper`` daemon,
the :mod:`~brutex.observability.loop_watchdog` thread (``None`` wired ⇒ ZERO
threads), and the ``_start_auto_resume_on_boot`` boot seam. Everything is
injectable (clock / gate / launcher / state store / tick cadence) so tests
run frozen-clock with zero sleeps.

Durable state: ONE atomic JSON sidecar ``<lake_root>/.pull_all_spot_auto.json``
(:data:`AUTO_PULL_STATE_FILENAME`, schema :data:`AUTO_PULL_STATE_SCHEMA`)
written via the COMMON :func:`~brutex.data.atomic_io.atomic_write_simple`.
A corrupt / unreadable file reads as DISARMED with ONE
``auto_pull.state_corrupt`` WARN — the dashboard never crashes OR hangs on it
(EVERY corruption shape, inode-TYPE shapes included: junk JSON, wrong shape,
invalid UTF-8, a nesting bomb, an oversized file — the refuter-round-6 N-1
hardening — plus a non-regular inode planted at the path, a FIFO /
``/dev/zero`` symlink / socket / directory, which pre-round-7 blocked the
read syscall FOREVER and silently wedged dashboard boot; the round-7 P-1
re-hardening on the #1614 round-6 ``O_NONBLOCK`` + ``S_ISREG`` loader
template — see :meth:`AutoPullStateStore.load`).
SINGLE-WRITER design: only this dashboard process's scheduler + arming routes
write the file (the CLI never touches it), so the scheduler holds an
in-memory mirror after the first load and every read is O(1) memory — no
per-poll lake I/O. HONEST multi-process residual (refuter round 6 M-3 — the
round-4 residual-(c) class extended to this sidecar): the once-per-day law is
per DASHBOARD PROCESS — two armed dashboards over ONE lake each hold their own
mirror, so both may fire once on the same day (flock-serialized, ledger-
idempotent — bounded waste, never wrong data) and a DISARM from a stale-mirror
process rewrites the sidecar from ITS mirror, clobbering the other process's
day-marks on disk. ONE dashboard process per lake is the documented posture
(``docs/runbooks/pull-all-spot.md``).

ONCE-PER-DAY SEMANTICS (pinned): a STARTED launch marks ``last_attempt_date``
— no further fire that day, whatever the run's outcome; a REFUSED launch
(the single-flight / job slot is held — e.g. a running multi-day dashboard
sweep, which holds the lake flock + job slot whole-run) marks NOTHING and is
retried on later ticks until the gate's own day boundary closes the window
(the deferral catch-up); ``last_success_date`` is marked ONLY when a run
completes with ``report.ok`` (via :meth:`AutoPullScheduler.note_run_complete`
— the launcher wiring notifies it for EVERY real spot-only run, manual and
auto alike, so a successful manual click also counts as the day's pull). A
STARTED-but-FAILED run therefore retries on the NEXT eligible day — the
auto-incremental ledger makes the catch-up complete (missed windows are
pulled by the next successful run); never a same-day retry storm.

NO new vendor-call surface: the scheduler only invokes the existing launcher.
"""

from __future__ import annotations

import json
import math
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final

import structlog

from brutex import pull_state_rust as _pull_state_rust
from brutex.calendars.auto_pull_gate import (
    AutoGateVerdict,
    auto_pull_allowed,
    resolve_settle_time,
)
from brutex.calendars.nse import now_ist
from brutex.dashboard.pull_intent import read_sidecar_bytes
from brutex.data.atomic_io import atomic_write_simple

_LOG = structlog.get_logger("brutex.dashboard.auto_pull_scheduler")

__all__ = [
    "AUTO_PULL_STATE_FILENAME",
    "AUTO_PULL_STATE_SCHEMA",
    "AUTO_PULL_TICK_ENV",
    "DEFAULT_TICK_SECONDS",
    "MAX_STATE_BYTES",
    "AutoLaunchResult",
    "AutoLauncher",
    "AutoPullScheduler",
    "AutoPullState",
    "AutoPullStateStore",
    "resolve_tick_seconds",
]

#: The durable armed-state sidecar under the lake root — the
#: ``.pull_intent.json`` naming family.
AUTO_PULL_STATE_FILENAME: Final[str] = ".pull_all_spot_auto.json"

#: The on-disk schema stamp. An UNKNOWN (future) schema reads as corrupt →
#: DISARMED + one WARN — never a silent misparse.
AUTO_PULL_STATE_SCHEMA: Final[int] = 1

#: Tick cadence — how often the daemon consults the gate while armed. 60 s is
#: plenty (the gate is day+settle-granular); env-overridable, fail-loud.
DEFAULT_TICK_SECONDS: Final[float] = 60.0

#: Size cap on the state file, checked on the ``fstat`` of the OPEN descriptor
#: (refuter round 6 N-1 introduced the cap — the crossverify
#: ``MAX_JOURNAL_BYTES`` / ``SweepCheckpointStore`` precedent for the value;
#: round 7 P-1/P-2 moved it onto the fd, closing the stat→read swap window):
#: a real state file is ~200 bytes, so 16 MiB is ~80,000× headroom; a planted
#: multi-GB file at the path reads DISARMED + one WARN instead of being read
#: whole (where a ``MemoryError`` would crash boot).
MAX_STATE_BYTES: Final[int] = 16 * 1024 * 1024

#: The tick-cadence env knob (strict positive finite seconds; junk fails loud
#: at construction — the ``resolve_*`` house convention).
AUTO_PULL_TICK_ENV: Final[str] = "BRUTEX_AUTO_PULL_TICK_S"


def resolve_tick_seconds(env: Mapping[str, str] | None = None) -> float:
    """Resolve the tick cadence ONCE — strict, fail-loud on junk.

    Unset / blank → :data:`DEFAULT_TICK_SECONDS`. A set value must parse as a
    positive finite float (seconds) — anything else raises
    :class:`ValueError` (a typo must never silently change the cadence).
    """
    mapping = os.environ if env is None else env
    raw = mapping.get(AUTO_PULL_TICK_ENV, "")
    token = raw.strip()
    if not token:
        return DEFAULT_TICK_SECONDS
    try:
        value = float(token)
    except ValueError as exc:
        raise ValueError(f"{AUTO_PULL_TICK_ENV}={raw!r} is not a number (seconds)") from exc
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(f"{AUTO_PULL_TICK_ENV}={raw!r} must be a positive finite second count")
    return value


@dataclass(frozen=True, slots=True)
class AutoPullState:
    """The durable armed-state record (all-defaults == the DISARMED state)."""

    armed: bool = False
    armed_at: str | None = None
    last_attempt_date: str | None = None
    last_success_date: str | None = None


def _optional_str(payload: Mapping[str, Any], key: str) -> str | None:
    """Read an optional string field; a non-str non-None value is corrupt."""
    value = payload.get(key)
    if value is None or isinstance(value, str):
        return value
    raise ValueError(f"{key} is not a string")


#: The i64 FFI band for the classified ``schema`` value (outside it only
#: the representability FACT crosses — it can never equal 1 anyway).
_I64_MIN: Final[int] = -(2**63)
_I64_MAX: Final[int] = 2**63 - 1


def _classify_state_for_kernel(payload: dict[str, Any]) -> dict[str, int | bool]:
    """MECHANICAL kernel-input classification of a decoded state payload.

    The Wave-3G seam projection (DEC-RUST-PULL-STATE-001): type tags +
    representability facts only — the value law (CPython's NUMERIC
    ``schema == 1`` across bool/int/float) and the refusal lattice live in
    the kernel; the JSON decode (and its whole failure taxonomy) happened
    BEFORE this call.
    """
    import struct

    schema = payload.get("schema")
    schema_kind = _pull_state_rust.SCHEMA_KIND_OTHER
    schema_i64 = 0
    schema_f64_bits = 0
    if schema is None:
        schema_kind = _pull_state_rust.SCHEMA_KIND_MISSING
    elif isinstance(schema, bool):
        schema_kind = _pull_state_rust.SCHEMA_KIND_BOOL
        schema_i64 = int(schema)
    elif isinstance(schema, int):
        if _I64_MIN <= schema <= _I64_MAX:
            schema_kind = _pull_state_rust.SCHEMA_KIND_INT
            schema_i64 = schema
        else:
            schema_kind = _pull_state_rust.SCHEMA_KIND_INT_HUGE
    elif isinstance(schema, float):
        schema_kind = _pull_state_rust.SCHEMA_KIND_FLOAT
        schema_f64_bits = struct.unpack("<Q", struct.pack("<d", schema))[0]

    def field_kind(key: str) -> int:
        value = payload.get(key)
        if key not in payload:
            return _pull_state_rust.STATE_FIELD_ABSENT
        if value is None:
            return _pull_state_rust.STATE_FIELD_NULL
        if isinstance(value, str):
            return _pull_state_rust.STATE_FIELD_STR
        return _pull_state_rust.STATE_FIELD_OTHER

    return {
        "root_mapping": True,
        "schema_kind": schema_kind,
        "schema_i64": schema_i64,
        "schema_f64_bits": schema_f64_bits,
        "armed_is_bool": isinstance(payload.get("armed"), bool),
        "armed_at_kind": field_kind("armed_at"),
        "last_attempt_kind": field_kind("last_attempt_date"),
        "last_success_kind": field_kind("last_success_date"),
    }


#: Kernel refusal code → the field the oracle's message names (composed
#: HERE, Python-side — byte-identical ``ValueError`` text per code).
_STATE_FIELD_BY_CODE: Final[dict[int, str]] = {
    _pull_state_rust.STATE_CODE_BAD_ARMED_AT: "armed_at",
    _pull_state_rust.STATE_CODE_BAD_LAST_ATTEMPT: "last_attempt_date",
    _pull_state_rust.STATE_CODE_BAD_LAST_SUCCESS: "last_success_date",
}


def _rust_state_refusal(payload: object) -> str | None:
    """Kernel-served refusal MESSAGE for ``_state_from_payload``; ``None`` = oracle.

    The Wave-2E serve-the-refusal law: only a NON-valid verdict is served (as
    the byte-identical ``ValueError`` text the caller raises); a VALID
    verdict deliberately falls THROUGH to the oracle body, so the state is
    only ever constructed on the ONE Python path (a kernel-vs-oracle
    disagreement on the valid arm is decided by the oracle — fail-safe).
    A verdict inconsistent with the locally-known payload shape (a poisoned
    in-vocabulary code that would need a mapping's field to compose its
    message) degrades to the oracle too — this helper is TOTAL.
    """
    if isinstance(payload, dict):
        codes = _classify_state_for_kernel(payload)
    else:
        codes = {
            "root_mapping": False,
            "schema_kind": _pull_state_rust.SCHEMA_KIND_MISSING,
            "schema_i64": 0,
            "schema_f64_bits": 0,
            "armed_is_bool": False,
            "armed_at_kind": _pull_state_rust.STATE_FIELD_ABSENT,
            "last_attempt_kind": _pull_state_rust.STATE_FIELD_ABSENT,
            "last_success_kind": _pull_state_rust.STATE_FIELD_ABSENT,
        }
    verdict = _pull_state_rust.rust_auto_state_verdict(
        root_mapping=bool(codes["root_mapping"]),
        schema_kind=int(codes["schema_kind"]),
        schema_i64=int(codes["schema_i64"]),
        schema_f64_bits=int(codes["schema_f64_bits"]),
        armed_is_bool=bool(codes["armed_is_bool"]),
        armed_at_kind=int(codes["armed_at_kind"]),
        last_attempt_kind=int(codes["last_attempt_kind"]),
        last_success_kind=int(codes["last_success_kind"]),
    )
    if verdict is None or verdict == _pull_state_rust.STATE_CODE_VALID:
        return None
    if verdict == _pull_state_rust.STATE_CODE_NOT_OBJECT:
        return None if isinstance(payload, dict) else "not a JSON object"
    if not isinstance(payload, dict):
        return None  # a mapping-shaped refusal for a non-mapping — poison
    if verdict == _pull_state_rust.STATE_CODE_BAD_SCHEMA:
        schema = payload.get("schema")
        return f"unknown schema {schema!r} (expected {AUTO_PULL_STATE_SCHEMA})"
    if verdict == _pull_state_rust.STATE_CODE_ARMED_NOT_BOOL:
        return "armed is not a bool"
    field = _STATE_FIELD_BY_CODE.get(verdict)
    if field is None:  # pragma: no cover - unreachable past the facade gate
        return None
    return f"{field} is not a string"


def _state_from_payload(payload: object) -> AutoPullState:
    """Decode the on-disk JSON into a state; :class:`ValueError` on any junk."""
    # --- auto_pull_scheduler (Wave 3G) Rust seam (DEC-RUST-PULL-STATE-001):
    # the validation LATTICE consulted in Rust over the mechanically-
    # classified payload codes; a kernel refusal raises the byte-identical
    # message composed above, a VALID verdict (and any degrade) falls
    # through to the byte-identical oracle body below.
    if _pull_state_rust.pull_state_rust_enabled():
        refusal = _rust_state_refusal(payload)
        if refusal is not None:
            raise ValueError(refusal)
    if not isinstance(payload, dict):
        raise ValueError("not a JSON object")
    schema = payload.get("schema")
    if schema != AUTO_PULL_STATE_SCHEMA:
        raise ValueError(f"unknown schema {schema!r} (expected {AUTO_PULL_STATE_SCHEMA})")
    armed = payload.get("armed")
    if not isinstance(armed, bool):
        raise ValueError("armed is not a bool")
    return AutoPullState(
        armed=armed,
        armed_at=_optional_str(payload, "armed_at"),
        last_attempt_date=_optional_str(payload, "last_attempt_date"),
        last_success_date=_optional_str(payload, "last_success_date"),
    )


class AutoPullStateStore:
    """The atomic ``.pull_all_spot_auto.json`` sidecar — best-effort I/O.

    Every method is boot-safe: :meth:`load` NEVER raises (a corrupt /
    unreadable file reads as the DISARMED default with ONE
    ``auto_pull.state_corrupt`` WARN — the once-flag resets on the next clean
    load, so a persistently-corrupt file warns once per corruption sighting,
    never per poll) and :meth:`save` logs-and-returns-``False`` on a failed
    write (the dashboard is never blocked by its own convenience sidecar).
    """

    def __init__(self, lake_root: Path) -> None:
        self._path = lake_root / AUTO_PULL_STATE_FILENAME
        self._corrupt_warned = False

    @property
    def path(self) -> Path:
        """The state file path (``<lake_root>/.pull_all_spot_auto.json``)."""
        return self._path

    def load(self) -> AutoPullState:
        """Read the persisted state; corrupt/unreadable ⇒ DISARMED — never raises.

        The read rides the shared regular-file-only loader
        :func:`~brutex.dashboard.pull_intent.read_sidecar_bytes` (the #1614
        round-6 template — refuter round 7 P-1,
        DEC-PULL-SCOPED-ALL-ONECLICK-001: the round-6 N-1 hardening's plain
        ``read_bytes`` HUNG forever on a non-regular inode, and the boot
        lifespan's ``start_if_armed`` state load made a planted FIFO /
        ``/dev/zero`` symlink a SILENT, permanent dashboard boot hang no
        exception belt could catch — executed at the pre-fix head):
        ``O_NONBLOCK`` open + ``S_ISREG`` on the descriptor folds a FIFO /
        device / socket / directory into the same DISARMED-plus-one-WARN
        answer, naming the inode shape, and the :data:`MAX_STATE_BYTES` cap
        is checked on the SAME ``fstat`` (the stat→read TOCTOU window
        closed — round-7 P-2). The decode stays INSIDE the
        corruption-tolerant path (refuter round 6 N-1): an invalid-UTF-8
        byte (``UnicodeDecodeError`` — a ``ValueError`` subclass raised by
        the DECODE, not the read), a nesting-bomb payload
        (``RecursionError``) and an out-of-range numeric (``OverflowError``)
        all fold into the same answer as junk JSON — pre-fix those two
        ESCAPED and a bit-rotted state file crashed dashboard boot
        persistently (uvicorn "Application startup failed" on every restart).
        """
        result = read_sidecar_bytes(self._path, max_bytes=MAX_STATE_BYTES)
        if result.shape == "absent":
            # The normal never-armed answer — not a corruption (covers a
            # dangling symlink and a file racing away too).
            self._corrupt_warned = False
            return AutoPullState()
        if result.raw is None:
            # not_regular / oversized / read_error — the detail names it.
            self._warn_corrupt(result.detail)
            return AutoPullState()
        try:
            state = _state_from_payload(json.loads(result.raw.decode("utf-8")))
        except (ValueError, RecursionError, OverflowError) as exc:
            # ValueError covers junk JSON, the wrong-shape refusals of
            # _state_from_payload AND UnicodeDecodeError (its subclass).
            self._warn_corrupt(str(exc) or type(exc).__name__)
            return AutoPullState()
        self._corrupt_warned = False
        return state

    def save(self, state: AutoPullState) -> bool:
        """Persist *state* atomically; return whether it was written — never raises."""
        body = json.dumps(
            {
                "schema": AUTO_PULL_STATE_SCHEMA,
                "armed": state.armed,
                "armed_at": state.armed_at,
                "last_attempt_date": state.last_attempt_date,
                "last_success_date": state.last_success_date,
            },
            sort_keys=True,
        )
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_simple(self._path, lambda tmp: tmp.write_text(body, encoding="utf-8"))
        except OSError as exc:
            _LOG.warning(
                "auto_pull.state_write_failed",
                path=str(self._path),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return False
        return True

    def _warn_corrupt(self, detail: str) -> None:
        """WARN once per store instance that the sidecar is corrupt (→ disarmed)."""
        if self._corrupt_warned:
            return
        self._corrupt_warned = True
        _LOG.warning("auto_pull.state_corrupt", path=str(self._path), detail=detail)


@dataclass(frozen=True, slots=True)
class AutoLaunchResult:
    """One launcher invocation's outcome: started, or refused with the reason."""

    started: bool
    detail: str


#: The scheduler's fire seam — the app binds a closure over the SAME
#: ``_launch_pipeline`` spot-only path the button uses (all rails inherited).
#: Must not raise for a refusal — a refused/rejected launch returns
#: ``started=False`` with the honest reason; an unexpected raise is caught by
#: the run-loop's tick guard (``auto_pull.tick_error``).
AutoLauncher = Callable[[], AutoLaunchResult]

#: The gate seam — production default wraps the EXISTING
#: :func:`~brutex.calendars.auto_pull_gate.auto_pull_allowed` (trading day +
#: post-close settle + Muhurat deferral; reused, never re-derived).
AutoGateFn = Callable[[datetime], AutoGateVerdict]

_THREAD_NAME: Final[str] = "brutex-auto-pull-scheduler"

#: The stop-join bound — the daemon thread never blocks shutdown longer.
_STOP_JOIN_TIMEOUT_S: Final[float] = 5.0


class AutoPullScheduler:
    """The armed daily ticker — see the module docstring for the semantics.

    Thread shape: ZERO threads until :meth:`arm` / :meth:`start_if_armed`
    spawns the ONE daemon ticker (the loop_watchdog ``None`` ⇒ zero-threads
    posture); :meth:`ensure_started` is idempotent (double-wire safe);
    :meth:`disarm` keeps the thread alive but inert (a disarmed tick is an
    O(1) memory no-op — re-arming needs no thread churn); :meth:`stop` (the
    lifespan shutdown hook) ends the loop bounded. The loop WAITS first, so
    the boot catch-up fires on the first tick ≤ one cadence after startup —
    never a surprise fire inside ``build_app``/lifespan itself.

    Log discipline: ``auto_pull.deferred`` / ``auto_pull.refused_retry`` /
    ``auto_pull.tick_error`` are deduplicated per (day, reason/error-type) —
    a sweep-long refusal, an all-morning ``before_settle`` or a persistent
    tick fault logs ONCE per day, never once per tick; ``armed`` /
    ``disarmed`` / ``fired`` / ``completed`` / ``state_corrupt`` always log.
    """

    def __init__(
        self,
        state_store: AutoPullStateStore,
        *,
        launcher: AutoLauncher | None = None,
        gate: AutoGateFn | None = None,
        clock: Callable[[], datetime] | None = None,
        tick_seconds: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._store = state_store
        self._launcher = launcher
        self._env = env
        self._gate: AutoGateFn = (
            gate if gate is not None else (lambda instant: auto_pull_allowed(now=instant, env=env))
        )
        self._clock: Callable[[], datetime] = clock if clock is not None else now_ist
        # Fail-loud at construction (the resolve_* convention): junk
        # BRUTEX_AUTO_PULL_TICK_S raises HERE, where cmd_dashboard turns it
        # into a clean exit-1 one-liner (the loop_watchdog pattern).
        self._tick_seconds = (
            resolve_tick_seconds(env) if tick_seconds is None else float(tick_seconds)
        )
        self._state_lock = threading.Lock()
        self._state: AutoPullState | None = None  # lazy in-memory mirror (single-writer)
        self._thread_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._log_memo: set[tuple[str, str]] = set()
        self._log_memo_day: date | None = None
        self._last_outcome = "(no auto-pull activity this process)"

    # ── durable state (in-memory mirror over the atomic sidecar) ─────────────

    def _state_locked(self) -> AutoPullState:
        """The current state — first access loads the sidecar (never raises)."""
        if self._state is None:
            self._state = self._store.load()
        return self._state

    def _update_state_locked(self, **changes: Any) -> AutoPullState:
        """Apply ``changes`` to the state, persist it, and return the new value.

        Caller must hold ``_state_lock`` (the ``_locked`` suffix law).
        """
        updated = replace(self._state_locked(), **changes)
        self._state = updated
        self._store.save(updated)
        return updated

    def _state_now(self) -> AutoPullState:
        """A lock-taking snapshot of the current durable state."""
        with self._state_lock:
            return self._state_locked()

    # ── arming surface (route-driven) ─────────────────────────────────────────

    def arm(self, *, now: datetime | None = None) -> AutoPullState:
        """Persist ``armed`` + start the ticker — the button's ``auto_arm`` hook.

        Idempotent (a re-arm refreshes ``armed_at``); always ensures the
        ticker thread is running.
        """
        instant = self._clock() if now is None else now
        with self._state_lock:
            state = self._update_state_locked(armed=True, armed_at=instant.isoformat())
        _LOG.info("auto_pull.armed", armed_at=state.armed_at, path=str(self._store.path))
        self._last_outcome = "armed"
        self.ensure_started()
        return state

    def disarm(self) -> AutoPullState:
        """Persist ``armed=False`` — the ticker stays alive but inert."""
        with self._state_lock:
            state = self._update_state_locked(armed=False, armed_at=None)
        _LOG.info("auto_pull.disarmed", path=str(self._store.path))
        self._last_outcome = "disarmed"
        return state

    # ── the launcher/completion seams (app-wired) ────────────────────────────

    def bind_launcher(self, launcher: AutoLauncher) -> None:
        """Bind the fire seam (``build_app`` wires the spot-only launcher closure)."""
        self._launcher = launcher

    def note_run_complete(self, *, ok: bool, run_date: date, source: str) -> None:
        """Record a finished REAL spot-only run — the launcher's completion hook.

        Called by the launcher wiring for EVERY real spot-only run (manual
        click, boot resume, and scheduler-fired alike) with the run's LAUNCH
        day (a post-close run crossing midnight marks the day it started —
        the next day still gets its own pull). ``ok=True`` marks
        ``last_success_date`` (the once-per-day guard); a FAILED run marks
        nothing here — a scheduler-fired failure already burned the day's
        ``last_attempt_date`` at fire time, so it retries on the NEXT
        eligible day (the incremental ledger heals the gap).
        """
        _LOG.info("auto_pull.completed", ok=ok, run_date=run_date.isoformat(), source=source)
        if ok:
            with self._state_lock:
                self._update_state_locked(last_success_date=run_date.isoformat())
            self._last_outcome = f"completed ok ({run_date.isoformat()}, {source})"
            return
        self._last_outcome = (
            f"run FAILED ({run_date.isoformat()}, {source}) — retries on the next eligible day"
        )

    # ── the ticker ────────────────────────────────────────────────────────────

    def ensure_started(self) -> bool:
        """Spawn the daemon ticker if not already alive — idempotent.

        Returns whether a NEW thread was spawned (``False`` = already
        running, the double-scheduler-instance safety).
        """
        with self._thread_lock:
            thread = self._thread
            if thread is not None and thread.is_alive():
                return False
            self._stop_event.clear()
            fresh = threading.Thread(target=self._run_loop, name=_THREAD_NAME, daemon=True)
            self._thread = fresh
            fresh.start()
            return True

    def start_if_armed(self) -> bool:
        """The boot hook (lifespan startup): resume ticking iff durably armed.

        A disarmed boot spawns NOTHING (zero threads — the loop_watchdog
        posture) and never auto-fires; the catch-up (today's window open +
        today unsuccessful ⇒ fire) happens on an early tick of the resumed
        loop, never inside this call.
        """
        if not self._state_now().armed:
            return False
        self.ensure_started()
        return True

    def stop(self) -> None:
        """End the loop (lifespan shutdown) — bounded join, never raises."""
        with self._thread_lock:
            thread = self._thread
            self._stop_event.set()
        if thread is not None:
            thread.join(timeout=_STOP_JOIN_TIMEOUT_S)

    def _run_loop(self) -> None:
        """The daemon loop: wait one cadence, tick, repeat — never crashes.

        WAIT-first (see the class docstring); a raising tick (an unexpected
        launcher/gate defect) logs ONE ``auto_pull.tick_error`` WARN per
        (day, error type) — the refuter-round-6 M-2 fold: a PERSISTENT tick
        error (e.g. a junk settle env written mid-process) previously logged
        ~1440 WARNs/day; now the day-scoped memo dedupes it like the class's
        own ``deferred``/``refused_retry`` discipline. No extra heartbeat is
        needed: the memo clears at day rollover (so the WARN re-fires once
        per day — the heartbeat) and ``_last_outcome`` carries the live error
        into every ``status()``/payload poll. The loop continues either way —
        the dashboard is never crashed by its convenience scheduler. Covered
        for REAL by the thread-based loop tests (bounded event waits, no
        sleeps-as-synchronization).
        """
        while not self._stop_event.wait(self._tick_seconds):
            try:
                self.tick_once()
            except Exception as exc:  # pylint: disable=broad-exception-caught  # never-crash loop guard
                self._note_tick_error(exc)

    def _note_tick_error(self, exc: Exception) -> None:
        """Record one raising tick: dedup the WARN per (day, error type) — never raises.

        The clock read is itself guarded (the failing seam MAY be the injected
        clock): an unreadable clock dedupes under the ``date.min`` sentinel day
        (the memo then clears on the first healthy-clock tick's day rollover).
        """
        try:
            today = self._clock().date()
        except Exception:  # pylint: disable=broad-exception-caught  # the clock may be the fault
            today = date.min
        self._last_outcome = f"tick error — {type(exc).__name__}: {exc}"
        self._log_once(today, "auto_pull.tick_error", type(exc).__name__, str(exc))

    def tick_once(self, *, now: datetime | None = None) -> str:
        """One scheduling decision; returns the action token (test surface).

        Tokens: ``"disarmed"`` · ``"already_succeeded"`` ·
        ``"already_attempted"`` · ``"deferred:<reason>"`` · ``"fired"`` ·
        ``"refused_retry"``.
        """
        instant = self._clock() if now is None else now
        today = instant.date()
        state = self._state_now()
        iso = today.isoformat()
        # --- auto_pull_scheduler (Wave 3G) Rust seam (DEC-RUST-PULL-STATE-001):
        # the tick LADDER consulted in Rust, two-staged to preserve the
        # oracle's LAZY side-effect order.  Stage 1 (pre-gate) passes
        # ``gate_allowed=False, launcher_bound=False`` placeholders — the
        # ladder's arm ORDER means they cannot affect the state arms (pinned
        # by the crate's placeholder-leak test + the exhaustive lattice
        # golden), so a disarmed/succeeded/attempted tick is served WITHOUT
        # ever consulting the injected gate.  A DEFERRED answer means "past
        # the state checks" — the gate consult and everything after run on
        # the SHARED Python path below (stage 2 re-consults with the real
        # facts; the launcher call, state writes, logs and ``_last_outcome``
        # strings always stay Python).  Any degrade serves the oracle.
        pre: int | None = None
        if _pull_state_rust.pull_state_rust_enabled():
            pre = _pull_state_rust.rust_auto_tick_decision(
                armed=state.armed,
                succeeded_today=state.last_success_date == iso,
                attempted_today=state.last_attempt_date == iso,
                gate_allowed=False,
                launcher_bound=False,
            )
            if pre == _pull_state_rust.TICK_CODE_DISARMED:
                return "disarmed"
            if pre == _pull_state_rust.TICK_CODE_ALREADY_SUCCEEDED:
                return "already_succeeded"
            if pre == _pull_state_rust.TICK_CODE_ALREADY_ATTEMPTED:
                return "already_attempted"
            if pre != _pull_state_rust.TICK_CODE_DEFERRED:
                pre = None  # degrade / out-of-stage poison → the oracle
        if pre is None:
            # The oracle pre-gate ladder (byte-identical to the pre-seam body).
            if not state.armed:
                return "disarmed"
            if state.last_success_date == iso:
                return "already_succeeded"
            if state.last_attempt_date == iso:
                # ONE started attempt per day, whatever its outcome — the
                # incremental ledger makes the next day's run the catch-up.
                return "already_attempted"
        verdict = self._gate(instant)
        launcher = self._launcher
        post: int | None = None
        if pre is not None:
            # Stage 2: the SAME kernel over the real post-gate facts.  A
            # state-arm answer here, or a FIRE with no launcher to call, is
            # inconsistent with stage 1 / the local wiring — degrade.
            post = _pull_state_rust.rust_auto_tick_decision(
                armed=state.armed,
                succeeded_today=state.last_success_date == iso,
                attempted_today=state.last_attempt_date == iso,
                gate_allowed=verdict.allowed,
                launcher_bound=launcher is not None,
            )
            if post is not None and (
                post < _pull_state_rust.TICK_CODE_DEFERRED
                or (post == _pull_state_rust.TICK_CODE_FIRE and launcher is None)
            ):
                post = None
        if post is None:
            # The oracle post-gate decision (byte-identical conditions).
            if not verdict.allowed:
                post = _pull_state_rust.TICK_CODE_DEFERRED
            elif launcher is None:
                post = _pull_state_rust.TICK_CODE_LAUNCHER_UNBOUND
            else:
                post = _pull_state_rust.TICK_CODE_FIRE
        if post == _pull_state_rust.TICK_CODE_DEFERRED:
            self._log_once(today, "auto_pull.deferred", verdict.reason, verdict.detail)
            self._last_outcome = f"deferred: {verdict.detail}"
            return f"deferred:{verdict.reason}"
        if launcher is None or post == _pull_state_rust.TICK_CODE_LAUNCHER_UNBOUND:
            # Reachable only on an unbound test/partial wiring — production
            # binds the launcher at build_app time, before any tick.
            self._log_once(
                today,
                "auto_pull.refused_retry",
                "launcher_unbound",
                "no launcher bound — build_app binds the spot-only launcher at wire time",
            )
            self._last_outcome = "launch refused — no launcher bound"
            return "refused_retry"
        result = launcher()
        if result.started:
            with self._state_lock:
                self._update_state_locked(last_attempt_date=iso)
            _LOG.info("auto_pull.fired", date=iso, detail=result.detail)
            self._last_outcome = f"fired ({iso})"
            return "fired"
        # Refused (single-flight / job slot held — e.g. a multi-day dashboard
        # sweep holds the lake flock + job slot whole-run): NOT an attempt —
        # later ticks retry until the gate's own day boundary closes the
        # window, and the ledger heals any fully-missed day on the next
        # successful run (auto-incremental catch-up).
        self._log_once(today, "auto_pull.refused_retry", "launch_refused", result.detail)
        self._last_outcome = f"launch refused — will retry: {result.detail}"
        return "refused_retry"

    # ── display surface ───────────────────────────────────────────────────────

    def status(self, *, now: datetime | None = None) -> dict[str, Any]:
        """The armed-state payload (pull-progress / launcher responses).

        O(1) memory after the first lazy state load (single-writer mirror —
        no per-poll lake I/O). Every string is honest — the next-eligible
        text never fabricates a time it cannot know.
        """
        instant = self._clock() if now is None else now
        state = self._state_now()
        return {
            "armed": state.armed,
            "armed_at": state.armed_at,
            "last_attempt_date": state.last_attempt_date,
            "last_success_date": state.last_success_date,
            "last_outcome": self._last_outcome,
            "next_eligible": self._next_eligible(state, instant),
        }

    def _next_eligible(self, state: AutoPullState, instant: datetime) -> str:
        """The honest next-fire description — never a fabricated instant.

        A settle-config :class:`ValueError` self-names its env var
        (``BRUTEX_PULL_ALL_SETTLE_IST`` or ``BRUTEX_SETTLE_EVIDENCE_PATH`` —
        DEC-SETTLE-EVIDENCE-ADOPTION-001), so no hardcoded prefix names one.
        """
        if not state.armed:
            return "disarmed — the ▶ Pull Spot button arms the daily auto-pull"
        try:
            settle = resolve_settle_time(self._env)
        except ValueError as exc:
            return f"unknown — invalid settle config: {exc}"
        iso = instant.date().isoformat()
        if state.last_success_date == iso:
            return f"done for today — next at/after {settle:%H:%M} IST on the next trading day"
        if state.last_attempt_date == iso:
            return (
                f"attempted today — next at/after {settle:%H:%M} IST on the next trading "
                f"day (the incremental ledger re-pulls only missing windows)"
            )
        try:
            verdict = self._gate(instant)
        except Exception as exc:  # pylint: disable=broad-exception-caught  # display surface, never a 500
            return f"unknown — auto gate error ({type(exc).__name__}: {exc})"
        if verdict.allowed:
            return f"eligible now — fires on the next scheduler tick (≤{self._tick_seconds:g}s)"
        return verdict.detail

    # ── log dedup (per day × reason) ─────────────────────────────────────────

    def _log_once(self, today: date, event: str, reason: str, detail: str) -> None:
        """Emit ``event`` at most once per (day, event, reason) — tick-noise dedup.

        ``auto_pull.refused_retry`` / ``auto_pull.tick_error`` log at WARNING,
        everything else at INFO; the memo resets at each IST day change.
        """
        with self._state_lock:
            if self._log_memo_day != today:
                self._log_memo_day = today
                self._log_memo.clear()
            key = (event, reason)
            if key in self._log_memo:
                return
            self._log_memo.add(key)
        if event in ("auto_pull.refused_retry", "auto_pull.tick_error"):
            _LOG.warning(event, reason=reason, detail=detail, date=today.isoformat())
            return
        _LOG.info(event, reason=reason, detail=detail, date=today.isoformat())
