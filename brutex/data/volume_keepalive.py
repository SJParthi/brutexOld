"""Lake-volume keepalive probe + wedge detection (DEC-VOLUME-KEEPALIVE-001).

The historical pull writes to an EXTERNAL lake volume (the operator's SSD). A
spun-down / wedged volume produces a durable-write HANG that never raises:

* the CONSUMER-side ``sink.append`` is ALREADY 90 s-bounded — a wedged append
  raises :class:`~brutex.data.errors.WriteDeadlineExceeded` and the window is
  DEFERRED (DEC-FETCH-HUNG-WRITE-DEADLINE-001), so consumers degrade to one
  90 s deferral per window, never a permanent consumer wedge;
* the CONFIRMED UNBOUNDED hang is the PRODUCER-thread spill ``fsync``:
  :meth:`~brutex.adapters.groww.fetch_spill.FetchSpillArea.try_spill` runs
  ``tempfile.mkstemp`` + ``atomic_write_via`` (write → ``fsync`` →
  ``os.replace``) ON THE FETCH THREAD against the SAME lake volume with NO
  deadline wrap — the fetch lane hangs unboundedly at the first full-queue
  spill during a wedge.

This module is LAYER 1 of the three-layer guard:

1. **Keepalive (this daemon, best-effort).** Every ``interval_s`` (default
   30 s) it writes 1 byte + ``fsync`` to a sidecar probe file under the lake
   root, each probe bounded by the COMMON
   :func:`~brutex.adapters.groww.deadline.run_with_deadline` freeze guard.
2. **Pause (the honest fallback).** A probe timeout / failure sets the shared
   :attr:`VolumeKeepalive.wedged` event;
   :meth:`~brutex.adapters.groww.fetch_pipeline.FetchPipeline.submit` PAUSES
   at its gate while the event is set — the producer never reaches the
   unbounded spill ``fsync`` on a wedged volume.
3. **Halt (bounded worst case).** Wedged past ``halt_after_s`` (default
   900 s), the PRODUCER thread raises a
   :class:`~brutex.data.errors.LakeWriteHalt` with reason ``volume_wedged``
   at the submit gate (the daemon itself NEVER raises into the pull), riding
   the existing global-stop path → graceful halt + the existing CRITICAL
   pull-halt alert.

**Honest envelope (Golden Rule #10).** Keepalive-prevents-spindown is
HOST-DEPENDENT: drive firmware / OS power policy may ignore periodic 1-byte
fsyncs, and some enclosures spin down regardless — layer 1 is best-effort
PREVENTION only; layers 2 (pause) + 3 (halt + alert) are the honest
guarantee. Each expired probe abandons ONE unkillable daemon worker thread
(the :func:`run_with_deadline` contract), bounded ≈ ``wedge_duration /
interval_s``. A paused producer is a DELIBERATE throughput sacrifice while
the volume is wedged. Windows already in flight at wedge onset still burn one
90 s consumer-deadline deferral each — the pause BOUNDS the waste, it does
not eliminate it. The sidecar probe file lives directly under the lake root
(outside ``bars/`` + ``options/``, never ``*.parquet`` — no lake scanner ever
sees it; mirrors the ``.locks`` sidecar convention).

Importing :mod:`brutex.adapters.groww.deadline` from :mod:`brutex.data` is
cycle-safe — ``deadline.py`` is deliberately stdlib-only (it imports
``threading`` and nothing from ``brutex``); precedent for the data→adapters
import direction exists (``brutex/data/spot_daily_resample.py`` imports
``adapters.groww.constants``).
"""

from __future__ import annotations

import math
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

import structlog

from brutex.adapters.groww.deadline import run_with_deadline

__all__ = [
    "DEFAULT_KEEPALIVE_INTERVAL_S",
    "DEFAULT_PROBE_DEADLINE_S",
    "DEFAULT_WEDGE_HALT_S",
    "KEEPALIVE_ENABLED_ENV",
    "KEEPALIVE_INTERVAL_ENV",
    "PROBE_DEADLINE_ENV",
    "PROBE_FILENAME",
    "WEDGE_HALT_ENV",
    "VolumeKeepalive",
    "resolve_keepalive_interval_seconds",
    "resolve_probe_deadline_seconds",
    "resolve_volume_keepalive_enabled",
    "resolve_wedge_halt_seconds",
]

_LOG = structlog.get_logger("brutex.data.volume_keepalive")

#: Default seconds between keepalive probes. 30 s keeps a typical SSD spindown
#: idle-timer from firing between probes on hosts that honour write activity
#: (HOST-DEPENDENT — see the module honest envelope) while costing one 1-byte
#: fsync per interval.
DEFAULT_KEEPALIVE_INTERVAL_S: Final[float] = 30.0

#: Default wall-clock bound on ONE probe write (seconds). Generous — 15 s is
#: far above a healthy-but-busy volume's worst fsync (deep month merges are
#: hundreds of ms) yet well under the 90 s consumer write deadline, so a busy
#: volume never false-wedges while a genuine wedge is detected promptly.
DEFAULT_PROBE_DEADLINE_S: Final[float] = 15.0

#: Default seconds a wedge may persist before the submit gate escalates to a
#: pull-wide :class:`~brutex.data.errors.LakeWriteHalt` (reason
#: ``volume_wedged``). 15 minutes tolerates a long-but-recovering stall while
#: bounding the worst-case paused-producer wait.
DEFAULT_WEDGE_HALT_S: Final[float] = 900.0

#: Environment KILL-SWITCH for the keepalive daemon. DEFAULT ON: an explicit
#: falsy token (``off`` / ``0`` / ``false`` / ``no``, case-insensitive)
#: disables it; anything else (incl. UNSET / blank) keeps it on — mirrors
#: :data:`~brutex.adapters.groww.fetch_pipeline.FETCH_PIPELINE_ENV`.
KEEPALIVE_ENABLED_ENV: Final[str] = "BRUTEX_VOLUME_KEEPALIVE"

#: Environment override for the probe interval (operator escape hatch).
KEEPALIVE_INTERVAL_ENV: Final[str] = "BRUTEX_VOLUME_KEEPALIVE_INTERVAL_S"

#: Environment override for the per-probe wall-clock deadline.
PROBE_DEADLINE_ENV: Final[str] = "BRUTEX_VOLUME_KEEPALIVE_PROBE_DEADLINE_S"

#: Environment override for the wedge-halt escalation bound.
WEDGE_HALT_ENV: Final[str] = "BRUTEX_VOLUME_WEDGE_HALT_S"

#: The sidecar probe filename directly under the lake root (never ``*.parquet``).
PROBE_FILENAME: Final[str] = ".volume-keepalive"

#: Explicit FALSY tokens that flip the kill-switch (disable the keepalive).
_KEEPALIVE_FALSY: Final[frozenset[str]] = frozenset({"off", "0", "false", "no"})

#: Bounded join budget at :meth:`VolumeKeepalive.stop` (seconds). The daemon
#: wakes from its interval wait immediately on the stop event; a probe wedged
#: inside :func:`run_with_deadline` is bounded by the probe deadline, so this
#: only guards against pathological scheduling — the daemon thread never keeps
#: the process alive either way.
_STOP_JOIN_TIMEOUT_S: Final[float] = 5.0


def _resolve_positive_seconds(env_name: str, default: float, raw: str | None) -> float:
    """Resolve one positive-finite-seconds env override, else *default*.

    Falls back to *default* when the value is unset, blank, non-numeric,
    non-positive, OR non-finite (``nan`` / ``inf`` parse as floats but would
    defeat the bound they configure). A bad value can therefore NEVER disable
    a bound (fail-safe to the audited default, Golden Rule #1). Mirrors
    :func:`~brutex.adapters.groww.write_deadline.resolve_write_deadline_seconds`
    so the pull deadlines share ONE validation shape (CLAUDE.md §11 COMMON
    shape). O(1).

    Parameters
    ----------
    env_name
        The environment variable to read when *raw* is ``None``.
    default
        The audited default returned for any unusable value.
    raw
        The value itself (``None`` reads *env_name*); a test may pass one.
    """
    value = os.environ.get(env_name) if raw is None else raw
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    if not math.isfinite(parsed) or parsed <= 0.0:
        return default
    return parsed


def resolve_keepalive_interval_seconds(raw: str | None = None) -> float:
    """Resolve the keepalive probe interval: env override, else the audited default.

    Reads :data:`KEEPALIVE_INTERVAL_ENV`; falls back to
    :data:`DEFAULT_KEEPALIVE_INTERVAL_S` for any unusable value (see
    :func:`_resolve_positive_seconds`). O(1).

    Parameters
    ----------
    raw
        The interval value (``None`` reads the environment); a test may pass one.
    """
    return _resolve_positive_seconds(KEEPALIVE_INTERVAL_ENV, DEFAULT_KEEPALIVE_INTERVAL_S, raw)


def resolve_probe_deadline_seconds(raw: str | None = None) -> float:
    """Resolve the per-probe wall-clock deadline: env override, else the audited default.

    Reads :data:`PROBE_DEADLINE_ENV`; falls back to
    :data:`DEFAULT_PROBE_DEADLINE_S` for any unusable value (see
    :func:`_resolve_positive_seconds`). O(1).

    Parameters
    ----------
    raw
        The deadline value (``None`` reads the environment); a test may pass one.
    """
    return _resolve_positive_seconds(PROBE_DEADLINE_ENV, DEFAULT_PROBE_DEADLINE_S, raw)


def resolve_wedge_halt_seconds(raw: str | None = None) -> float:
    """Resolve the wedge-halt escalation bound: env override, else the audited default.

    Reads :data:`WEDGE_HALT_ENV`; falls back to :data:`DEFAULT_WEDGE_HALT_S`
    for any unusable value (see :func:`_resolve_positive_seconds`). O(1).

    Parameters
    ----------
    raw
        The bound value (``None`` reads the environment); a test may pass one.
    """
    return _resolve_positive_seconds(WEDGE_HALT_ENV, DEFAULT_WEDGE_HALT_S, raw)


def resolve_volume_keepalive_enabled(flag: str | None = None) -> bool:
    """Resolve the keepalive kill-switch: ``False`` ONLY on an explicit falsy token.

    DEFAULT ON — an UNSET / blank / unrecognised :data:`KEEPALIVE_ENABLED_ENV`
    keeps the keepalive enabled; only ``off`` / ``0`` / ``false`` / ``no``
    (case-insensitive) disables it. Mirrors the
    :func:`~brutex.adapters.groww.fetch_pipeline.resolve_fetch_pipeline_size`
    kill-switch shape (CLAUDE.md §11 COMMON shape). O(1).

    Parameters
    ----------
    flag
        The on/off token (``None`` reads the environment); a test may pass one.
    """
    value = os.environ.get(KEEPALIVE_ENABLED_ENV) if flag is None else flag
    return not (value is not None and value.strip().lower() in _KEEPALIVE_FALSY)


class VolumeKeepalive:
    """A context-free daemon probing the lake volume; observers read :attr:`wedged`.

    ONE daemon thread (``volume-keepalive``) wakes every ``interval_s``, writes
    1 byte + ``fsync`` to :attr:`probe_path` under the COMMON
    :func:`~brutex.adapters.groww.deadline.run_with_deadline` freeze guard, and
    edge-latches the shared :attr:`wedged` event: a probe timeout / ``OSError``
    SETS it (+ one ``volume_keepalive.wedged`` WARNING per episode); a later
    successful probe CLEARS it (+ one ``volume_keepalive.recovered`` INFO).
    The daemon OBSERVES only — it never raises into the pull; the escalation
    to a :class:`~brutex.data.errors.LakeWriteHalt` is raised by the PRODUCER
    at the :meth:`~brutex.adapters.groww.fetch_pipeline.FetchPipeline.submit`
    gate once :meth:`wedged_seconds` crosses :attr:`halt_after_s`.

    See the module docstring for the full honest envelope (host-dependent
    prevention; one abandoned daemon worker per expired probe, bounded
    ≈ ``wedge_duration / interval_s``).
    """

    def __init__(
        self,
        probe_path: Path,
        *,
        interval_s: float | None = None,
        probe_deadline_s: float | None = None,
        halt_after_s: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        probe: Callable[[], None] | None = None,
    ) -> None:
        """Configure the keepalive (the thread starts only on :meth:`start`).

        Parameters
        ----------
        probe_path
            The sidecar probe file to write (on the volume being watched).
        interval_s
            Seconds between probes; ``None`` reads
            :func:`resolve_keepalive_interval_seconds`. Must be positive finite.
        probe_deadline_s
            Wall-clock bound on one probe; ``None`` reads
            :func:`resolve_probe_deadline_seconds`. Must be positive finite.
        halt_after_s
            Wedge duration past which the submit gate escalates to a halt;
            ``None`` reads :func:`resolve_wedge_halt_seconds`. Must be
            positive finite.
        monotonic
            Injected monotonic clock (tests pass a fake to drive
            :meth:`wedged_seconds` deterministically).
        probe
            Injected probe callable (tests pass a fake); ``None`` uses the
            real 1-byte write + ``fsync`` of :attr:`probe_path`.
        """
        self._probe_path = probe_path
        self._interval_s = (
            resolve_keepalive_interval_seconds() if interval_s is None else interval_s
        )
        self._probe_deadline_s = (
            resolve_probe_deadline_seconds() if probe_deadline_s is None else probe_deadline_s
        )
        self._halt_after_s = resolve_wedge_halt_seconds() if halt_after_s is None else halt_after_s
        for name, value in (
            ("interval_s", self._interval_s),
            ("probe_deadline_s", self._probe_deadline_s),
            ("halt_after_s", self._halt_after_s),
        ):
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be positive and finite, got {value}")
        self._monotonic = monotonic
        self._probe: Callable[[], None] = self._default_probe if probe is None else probe
        self._wedged = threading.Event()
        self._state_lock = threading.Lock()
        self._wedged_since: float | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @classmethod
    def under_lake_root(cls, lake_root: Path) -> VolumeKeepalive:
        """Build a keepalive probing ``<lake_root>/.volume-keepalive`` (env-tuned bounds).

        The COMMON build path the live pull site uses. The probe file is a
        sidecar directly under the lake root — outside ``bars/`` +
        ``options/`` and never ``*.parquet``, so no lake scanner ever sees it
        (mirrors the ``.locks`` sidecar). The lake root is created if absent
        so a first-ever pull never false-wedges on a missing directory.
        """
        lake_root.mkdir(parents=True, exist_ok=True)
        return cls(probe_path=lake_root / PROBE_FILENAME)

    @property
    def probe_path(self) -> Path:
        """The sidecar probe file being written (for the live-site log / tests)."""
        return self._probe_path

    @property
    def wedged(self) -> threading.Event:
        """The shared wedge signal — SET while the volume fails its probe."""
        return self._wedged

    @property
    def halt_after_s(self) -> float:
        """Seconds of continuous wedge past which the submit gate escalates to a halt."""
        return self._halt_after_s

    def wedged_seconds(self) -> float:
        """How long the CURRENT wedge episode has lasted (``0.0`` while healthy).

        Thread-safe; measured on the injected monotonic clock from the FIRST
        failed probe of the episode (clamped ``>= 0`` for a fake-clock rewind).
        """
        with self._state_lock:
            since = self._wedged_since
            if since is None:
                return 0.0
            return max(0.0, self._monotonic() - since)

    def start(self) -> None:
        """Start the ONE ``volume-keepalive`` daemon thread (idempotent).

        A second :meth:`start` — including after :meth:`stop` (the lifecycle
        is single-use per pull) — is a no-op.
        """
        with self._state_lock:
            if self._thread is not None:
                return
            thread = threading.Thread(target=self._run, name="volume-keepalive", daemon=True)
            self._thread = thread
        thread.start()

    def stop(self) -> None:
        """Stop the daemon (bounded join) + best-effort unlink the probe file.

        Idempotent; safe before :meth:`start`. An unlink failure is logged and
        swallowed — stopping the keepalive can never raise into the pull's
        drain path.
        """
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=_STOP_JOIN_TIMEOUT_S)
        try:
            self._probe_path.unlink(missing_ok=True)
        except OSError:
            _LOG.warning("volume_keepalive.probe_unlink_failed", probe_path=str(self._probe_path))

    def _run(self) -> None:
        """The daemon loop: wait one interval, probe, repeat until stopped."""
        while not self._stop_event.wait(self._interval_s):
            self._probe_once()

    def _probe_once(self) -> None:
        """Run ONE deadline-bounded probe and edge-latch the wedge state.

        A :class:`~brutex.adapters.groww.deadline.DeadlineExceeded` (a
        :class:`TimeoutError`, itself an :class:`OSError` subclass) or any
        other ``OSError`` from the probe marks the volume WEDGED; a clean
        return marks it healthy. Deliberately NARROW: a non-``OSError`` from
        an injected probe is a genuine code bug and propagates (it kills only
        this observer daemon, never the pull — Golden Rule #1, no silent
        swallow).
        """
        try:
            # DeadlineExceeded (the run_with_deadline expiry) is a TimeoutError,
            # which subclasses OSError — ONE except covers both the hang and a
            # raising probe (a vanished mount / read-only remount / ENOSPC).
            run_with_deadline(self._probe, self._probe_deadline_s)
        except OSError as exc:
            self._mark_wedged(exc)
            return
        self._mark_healthy()

    def _mark_wedged(self, exc: BaseException) -> None:
        """Latch the wedge episode start + set :attr:`wedged`; WARN once per episode."""
        with self._state_lock:
            first = self._wedged_since is None
            if first:
                self._wedged_since = self._monotonic()
        self._wedged.set()
        if first:
            _LOG.warning(
                "volume_keepalive.wedged",
                probe_path=str(self._probe_path),
                error_type=type(exc).__name__,
                probe_deadline_s=self._probe_deadline_s,
            )

    def _mark_healthy(self) -> None:
        """Clear the wedge episode + :attr:`wedged`; log the recovery edge once."""
        with self._state_lock:
            was_wedged = self._wedged_since is not None
            self._wedged_since = None
        self._wedged.clear()
        if was_wedged:
            _LOG.info("volume_keepalive.recovered", probe_path=str(self._probe_path))

    def _default_probe(self) -> None:
        """The real probe: open, write 1 byte, ``fsync``, close (all on the lake volume)."""
        # 0o600 — owner-only: the probe file is a private liveness sidecar, never
        # world-readable (matches the partition-lock / singleflight sidecar mode).
        fd = os.open(self._probe_path, os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, b"k")
            os.fsync(fd)
        finally:
            os.close(fd)
