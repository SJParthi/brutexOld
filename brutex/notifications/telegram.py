"""Telegram notifier — async fire-and-forget alerts for ingest runs.

Implements ``FR-ING-016`` and ``REQUIREMENTS.md §15.6 Telegram Alert
Channel``.

## Contract (from §15.6)

* **Async fire-and-forget** — :meth:`TelegramNotifier.publish` returns
  immediately. A background daemon thread drains the queue and posts
  to Telegram. Caller never blocks on HTTP latency.
* **On-failure tolerance** — if Telegram is unreachable, the notifier
  increments a drop counter, logs to structlog, and discards the
  message. **The ingest is NEVER blocked by Telegram outages.**
* **Dedupe** — events with the same ``(kind, dedupe_key)`` within the
  per-event-kind dedupe window are coalesced to a single send.
* **Credentials** — env vars only:
    ``BRUTEX_TELEGRAM_BOT_TOKEN`` (required)
    ``BRUTEX_TELEGRAM_CHAT_ID`` (required)
    ``BRUTEX_TELEGRAM_ENABLED`` (``"true"`` to opt in; default disabled)
* **Markdown v2** — all messages use Telegram MarkdownV2 escaping.

## Design

* **Queue + worker** — :class:`queue.Queue` feeds a single daemon
  thread. Thread-safe publish from any worker / subscriber.
* **HTTP via urllib.request** — stdlib only, no third-party dep.
  Per-call timeout prevents the worker from hanging.
* **Dedupe via TTL cache** — last-seen timestamp per ``(kind, dedupe_key)``.
  When the current call is within the window, drop. The stamp is recorded
  only AFTER a successful send — a FAILED send never suppresses the next
  identical alert. Window expires via lazy eviction during publish.
* **Synchronous test mode** — pass a ``transport`` callable to the
  constructor and the notifier skips the worker thread, calling
  the transport inline on publish. Tests use this to avoid threading.

## Usage

```python
config = TelegramConfig.from_env()
if config.enabled:
    notifier = TelegramNotifier.start(config=config)
    notifier.publish(TelegramEvent(
        kind=TelegramEventKind.RUN_START,
        severity=TelegramSeverity.INFO,
        message="🚀 Ingest starting. Run abc123.",
    ))
    # ... ingest runs ...
    notifier.stop()
```
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from types import TracebackType
from typing import Final, Self

from brutex.models import BruteXError
from brutex.notifications.notify_rust import routed_dedupe_should_drop

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────

#: Telegram Bot API base URL. Override via
#: ``BRUTEX_TELEGRAM_API_BASE`` env var for testing against a mock.
DEFAULT_API_BASE: Final[str] = "https://api.telegram.org"

#: Per-send HTTP timeout in seconds.
DEFAULT_HTTP_TIMEOUT: Final[float] = 5.0

#: Maximum events in the queue before publish starts dropping.
#: Dropping + logging is safer than blocking the caller.
DEFAULT_QUEUE_CAPACITY: Final[int] = 1024

#: Env var names. These are the NAMES of env vars (not their values, which are
#: actual secrets and read at runtime only). bandit/ruff S105 flags strings
#: containing "TOKEN" — suppressed here because we deliberately use this
#: convention for self-documenting env var identifiers.
ENV_BOT_TOKEN: Final[str] = "BRUTEX_TELEGRAM_BOT_TOKEN"  # noqa: S105  # env var name, not the token itself
ENV_CHAT_ID: Final[str] = "BRUTEX_TELEGRAM_CHAT_ID"
ENV_ENABLED: Final[str] = "BRUTEX_TELEGRAM_ENABLED"
ENV_API_BASE: Final[str] = "BRUTEX_TELEGRAM_API_BASE"

#: HTTP "successful response" status range per RFC 9110 §15.3.
HTTP_OK_MIN: Final[int] = 200
HTTP_OK_MAX: Final[int] = 300

#: Values that LOOK like an operator meant "enable" but are NOT the exact
#: opt-in string ``"true"`` — each of these silently DISABLES critical
#: alerting, so :meth:`TelegramConfig.from_env` logs a LOUD warning when it
#: sees one (R2-CS-2).
_TRUTHY_BUT_NOT_TRUE: Final[frozenset[str]] = frozenset(
    {"1", "yes", "on", "y", "t", "enable", "enabled"}
)

#: URL schemes accepted for ``BRUTEX_TELEGRAM_API_BASE``. A scheme-less base
#: makes ``urllib.request.Request(url)`` raise a ``ValueError`` whose message
#: embeds the FULL URL including the bot token — refused at parse time so the
#: token can never reach a log line (R2-OCLI-F6 token-leak guard).
_ALLOWED_API_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})


# ────────────────────────────────────────────────────────────────────────
# Errors
# ────────────────────────────────────────────────────────────────────────


class TelegramError(BruteXError):
    """Base class for Telegram notifier errors."""


class TelegramConfigError(TelegramError):
    """Raised when :class:`TelegramConfig` cannot be constructed from env."""


# ────────────────────────────────────────────────────────────────────────
# Enums
# ────────────────────────────────────────────────────────────────────────


class TelegramSeverity(StrEnum):
    """Severity label for filtering + formatting."""

    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    CRITICAL = "critical"


class TelegramEventKind(StrEnum):
    """Event kinds from ``REQUIREMENTS.md §15.6`` Event taxonomy."""

    RUN_START = "run.start"
    PHASE_START = "phase.start"
    PHASE_PROGRESS = "phase.progress"
    PHASE_COMPLETE = "phase.complete"
    PHASE_BLOCKED = "phase.blocked"
    QUARANTINE_NEW = "quarantine.new"
    RATE_LIMIT_DOWNSHIFT = "rate_limit.downshift"
    RATE_LIMIT_RECOVERED = "rate_limit.recovered"
    WORKER_HUNG = "worker.hung"
    SYMBOL_WINDOW_FAILURE = (
        "symbol.window_failure"  # 100% window-failure for a symbol (DEC-RATE-STORM-WATCHDOG-001)
    )
    AUTH_RENEW_FAILED = "auth.renew_failed"  # vendor token-renew refused (DEC-TELEGRAM-WIRE-001)
    CHILD_RELAUNCHED = "child.relaunched"  # supervisor relaunched a crashed child (DEC-SUPERVISOR-CRASHLOOP-ALERT-001)
    SUPERVISOR_GAVE_UP = "supervisor.gave_up"  # max-restart cap exhausted — the sweep is DOWN (DEC-SUPERVISOR-CRASHLOOP-ALERT-001)
    RUN_HEARTBEAT = "run.heartbeat"
    RUN_COMPLETE = "run.complete"
    TEST = "test"  # for `brutex notify test`


#: Dedupe windows (seconds) per event kind. Events with the same
#: (kind, dedupe_key) within this window are coalesced.
#: From REQUIREMENTS.md §15.6 table.
DEDUPE_WINDOW_SECONDS: Final[dict[str, float]] = {
    TelegramEventKind.RUN_START: 0.0,
    TelegramEventKind.PHASE_START: 0.0,
    TelegramEventKind.PHASE_PROGRESS: 30.0,  # base; overridable per phase
    TelegramEventKind.PHASE_COMPLETE: 0.0,
    TelegramEventKind.PHASE_BLOCKED: 0.0,
    TelegramEventKind.QUARANTINE_NEW: 1.0,  # per dedupe_key
    TelegramEventKind.RATE_LIMIT_DOWNSHIFT: 60.0,
    TelegramEventKind.RATE_LIMIT_RECOVERED: 60.0,
    TelegramEventKind.WORKER_HUNG: 0.0,
    # The RetryStormWatchdog already guarantees one alert per symbol, so the
    # notifier window is 0.0 (no extra coalescing) — the dedupe_key (symbol)
    # is still set as defence-in-depth against a re-run within one process.
    TelegramEventKind.SYMBOL_WINDOW_FAILURE: 0.0,
    TelegramEventKind.AUTH_RENEW_FAILED: 300.0,  # coalesce a renew-retry storm (5 min)
    # Coalesce an all-night crash loop into one push per 5 min per dedupe_key —
    # the supervisor's CRITICAL crash-loop escalation carries its OWN dedupe_key
    # so it always cuts through the WARN-tier window (DEC-SUPERVISOR-CRASHLOOP-ALERT-001).
    TelegramEventKind.CHILD_RELAUNCHED: 300.0,
    # The give-up verdict is TERMINAL — it fires once per supervisor run, so a
    # coalescing window could only ever swallow it, never de-storm anything.
    TelegramEventKind.SUPERVISOR_GAVE_UP: 0.0,
    TelegramEventKind.RUN_HEARTBEAT: 600.0,  # 10 min
    TelegramEventKind.RUN_COMPLETE: 0.0,
    TelegramEventKind.TEST: 0.0,
}


def _dedupe_should_drop_python(window: float, last_seen: float | None, now: float) -> bool:
    """The VERBATIM Python dedupe drop predicate — the LIVE parity oracle.

    DEC-NOTIFY-RUST-CORE-001: the pre-facade tail of
    :meth:`TelegramBucket.check_dedupe_locked`, extracted token-for-token
    (the Slice-0 idiom). The caller has already resolved *window* from
    :data:`DEDUPE_WINDOW_SECONDS` and short-circuited ``window <= 0.0``;
    this is the pure ``(now - last_seen) < window`` strict-float law.
    """
    return last_seen is not None and (now - last_seen) < window


# ────────────────────────────────────────────────────────────────────────
# Dataclasses
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    """Loaded from env. Immutable after construction.

    The bot token and chat id are SECRETS — both carry ``repr=False`` so an
    accidental ``repr()``/``str()`` of a config (log line, exception message,
    debugger dump) can never leak them (R2-CS-5).
    """

    bot_token: str = field(repr=False)
    chat_id: str = field(repr=False)
    enabled: bool
    api_base: str = DEFAULT_API_BASE
    http_timeout: float = DEFAULT_HTTP_TIMEOUT
    queue_capacity: int = DEFAULT_QUEUE_CAPACITY

    @classmethod
    def from_env(cls) -> TelegramConfig:
        """Load config from env vars.

        Returns
        -------
        TelegramConfig
            If ``BRUTEX_TELEGRAM_ENABLED`` is not ``"true"``, returns
            an instance with ``enabled=False`` and dummy creds.
            Otherwise validates and returns a live config.

        Raises
        ------
        TelegramConfigError
            If enabled but the bot token or chat id is missing, or if the
            ``BRUTEX_TELEGRAM_API_BASE`` override is not an ``http(s)`` URL
            (a scheme-less base would leak the bot token through urllib's
            ``ValueError`` message — R2-OCLI-F6).
        """
        enabled_raw = os.environ.get(ENV_ENABLED, "false").strip().lower()
        enabled = enabled_raw == "true"
        if enabled_raw in _TRUTHY_BUT_NOT_TRUE:
            # LOUD honesty warning (R2-CS-2): '1'/'yes'/'on'/… look truthy but
            # only the EXACT string 'true' opts in — without this warning a
            # near-miss value silently disables CRITICAL alerting.
            logger.warning(
                "telegram_enabled_value_not_true: %s=%r looks truthy but only the exact "
                "string 'true' enables Telegram — critical alerting stays DISABLED. "
                "Set %s=true to enable.",
                ENV_ENABLED,
                enabled_raw,
                ENV_ENABLED,
            )

        token = os.environ.get(ENV_BOT_TOKEN, "").strip()
        chat_id = os.environ.get(ENV_CHAT_ID, "").strip()
        api_base = os.environ.get(ENV_API_BASE, DEFAULT_API_BASE).strip()

        if enabled and not token:
            raise TelegramConfigError(
                f"{ENV_ENABLED}=true but {ENV_BOT_TOKEN} is not set. "
                f"See docs/runbooks/telegram-setup.md."
            )
        if enabled and not chat_id:
            raise TelegramConfigError(
                f"{ENV_ENABLED}=true but {ENV_CHAT_ID} is not set. "
                f"See docs/runbooks/telegram-setup.md."
            )
        if enabled:
            # Parse-time scheme validation (R2-OCLI-F6): a scheme-less
            # api_base makes urllib.request.Request(url) raise a ValueError
            # whose message embeds the FULL bot-token URL. Refuse here, with
            # a message that names only the env var + scheme — never the URL.
            try:
                scheme = urllib.parse.urlsplit(api_base or DEFAULT_API_BASE).scheme.lower()
            except ValueError as exc:
                raise TelegramConfigError(
                    f"{ENV_API_BASE} is not a parseable URL. Use an http:// or https:// base."
                ) from exc
            if scheme not in _ALLOWED_API_SCHEMES:
                raise TelegramConfigError(
                    f"{ENV_API_BASE} scheme {scheme!r} is not http(s). A scheme-less "
                    f"base would leak the bot token via urllib's ValueError message; "
                    f"set an explicit http:// or https:// base."
                )

        return cls(
            bot_token=token,
            chat_id=chat_id,
            enabled=enabled,
            api_base=api_base or DEFAULT_API_BASE,
        )


@dataclass(frozen=True, slots=True)
class TelegramEvent:
    """One event to publish to Telegram.

    Attributes
    ----------
    kind
        :class:`TelegramEventKind` identifier. Used for dedupe keying
        and for the dashboard / audit log.
    severity
        :class:`TelegramSeverity` label. Does not affect routing in
        v1 but is emitted in the audit log.
    message
        Pre-formatted message body (already escaped for MarkdownV2).
    dedupe_key
        Optional secondary key for dedupe — e.g., the ``item_id`` for
        a quarantine event. Two QUARANTINE_NEW events with the same
        item_id within the dedupe window are coalesced.
    created_at
        Monotonic seconds at construction. Defaults to
        :func:`time.monotonic`.
    """

    kind: str
    severity: str
    message: str
    dedupe_key: str = ""
    created_at: float = field(default_factory=time.monotonic)


@dataclass(slots=True)
class TelegramBucket:
    """Internal stats + dedupe bookkeeping. Thread-safe.

    Exposed via :meth:`TelegramNotifier.stats` for the dashboard.
    """

    sent: int = 0
    dropped_on_failure: int = 0
    dropped_on_dedupe: int = 0
    dropped_on_queue_full: int = 0
    dropped_on_disabled: int = 0
    last_success_at: float = 0.0
    last_failure_at: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _dedupe_cache: dict[tuple[str, str], float] = field(default_factory=dict)

    def check_dedupe_locked(self, kind: str, dedupe_key: str, now: float) -> bool:
        """Return True if the event should be dropped as duplicate.

        PURE check — does NOT stamp the last-seen time. The stamp is
        recorded via :meth:`record_dedupe_locked` only AFTER a successful
        send, so a FAILED send never suppresses identical follow-up alerts
        for the whole dedupe window (dedupe-before-send fix, R2-OCLI-F4).
        Caller holds ``self._lock``. The window-table lookup + the cache
        read stay HERE (state); only the pure drop PREDICATE routes through
        the default-ON ``BRUTEX_NOTIFY_RUST`` facade
        (DEC-NOTIFY-RUST-CORE-001) — flag OFF is the byte-identical
        :func:`_dedupe_should_drop_python` oracle.
        """
        window = DEDUPE_WINDOW_SECONDS.get(kind, 0.0)
        if window <= 0.0:
            return False
        last_seen = self._dedupe_cache.get((kind, dedupe_key))
        return routed_dedupe_should_drop(
            window, last_seen, now, python_law=_dedupe_should_drop_python
        )

    def record_dedupe_locked(self, kind: str, dedupe_key: str, now: float) -> None:
        """Stamp the dedupe last-seen time for a SUCCESSFULLY sent event.

        Called by :meth:`TelegramNotifier._send_one` only when the transport
        reported success — never at publish time — so a failed send leaves
        no stamp behind (the "rollback" is that nothing was stamped).
        No-op for kinds with a zero window: nothing would ever read the
        stamp and skipping keeps the cache bounded to deduped kinds.
        Caller holds ``self._lock``.
        """
        if DEDUPE_WINDOW_SECONDS.get(kind, 0.0) <= 0.0:
            return
        self._dedupe_cache[(kind, dedupe_key)] = now

    def evict_expired_dedupe_locked(self, now: float) -> None:
        """Drop dedupe keys older than the max window so the cache stays bounded."""
        max_window = max(DEDUPE_WINDOW_SECONDS.values(), default=0.0)
        cutoff = now - max_window
        stale = [k for k, t in self._dedupe_cache.items() if t < cutoff]
        for k in stale:
            del self._dedupe_cache[k]


# ────────────────────────────────────────────────────────────────────────
# Transport — injectable for tests
# ────────────────────────────────────────────────────────────────────────


#: Transport signature: (config, event) → True on success, False on failure.
TelegramTransport = Callable[[TelegramConfig, TelegramEvent], bool]


def _default_transport(config: TelegramConfig, event: TelegramEvent) -> bool:
    """Default transport — POSTs to Telegram Bot API via urllib.

    Returns True on HTTP 200, False on any other status or exception.
    Never raises — failures are logged and signalled via return value.
    """
    if not config.enabled:
        return False
    url = f"{config.api_base}/bot{config.bot_token}/sendMessage"
    body = urllib.parse.urlencode(
        {
            "chat_id": config.chat_id,
            "text": event.message,
            "parse_mode": "MarkdownV2",
        }
    ).encode("utf-8")

    try:
        # Request construction runs INSIDE the try: a malformed URL (e.g. a
        # scheme-less api_base) raises ValueError HERE, and its message embeds
        # the FULL URL including the bot token — it must never escape to a
        # caller that logs str(exc) (R2-OCLI-F6 token-leak guard).
        req = urllib.request.Request(  # noqa: S310 — URL is under our control
            url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(  # nosec B310 — URL is constructed from our own BASE_URL constant  # noqa: S310
            req, timeout=config.http_timeout
        ) as resp:
            status = getattr(resp, "status", HTTP_OK_MIN)
            return HTTP_OK_MIN <= status < HTTP_OK_MAX
    except urllib.error.HTTPError as exc:
        logger.warning("telegram_http_error", extra={"status": exc.code})
        return False
    except urllib.error.URLError as exc:
        logger.warning("telegram_url_error", extra={"reason": str(exc.reason)})
        return False
    except ValueError as exc:
        # Log ONLY the exception type — the ValueError message contains the
        # full bot-token URL, so neither str(exc) nor the URL is ever logged.
        logger.warning("telegram_invalid_url", extra={"error_type": type(exc).__name__})
        return False
    except (TimeoutError, OSError) as exc:
        logger.warning("telegram_network_error", extra={"error": str(exc)})
        return False


# ────────────────────────────────────────────────────────────────────────
# Notifier
# ────────────────────────────────────────────────────────────────────────


class TelegramNotifier:
    """Async fire-and-forget Telegram notifier.

    Normal usage::

        config = TelegramConfig.from_env()
        notifier = TelegramNotifier.start(config=config)
        notifier.publish(TelegramEvent(...))  # returns immediately
        notifier.stop()  # on shutdown

    Test usage (sync, no threads)::

        notifier = TelegramNotifier(
            config=test_config,
            transport=mock_transport,
            synchronous=True,
        )
        notifier.publish(TelegramEvent(...))  # calls transport inline
    """

    def __init__(
        self,
        *,
        config: TelegramConfig,
        transport: TelegramTransport = _default_transport,
        synchronous: bool = False,
    ) -> None:
        self._config = config
        self._transport = transport
        self._synchronous = synchronous
        self._queue: queue.Queue[TelegramEvent | None] = queue.Queue(maxsize=config.queue_capacity)
        self._bucket = TelegramBucket()
        self._worker: threading.Thread | None = None
        self._running = False

    # ── Lifecycle ──────────────────────────────────────────────────

    @classmethod
    def start(
        cls,
        *,
        config: TelegramConfig,
        transport: TelegramTransport = _default_transport,
    ) -> TelegramNotifier:
        """Create + start a notifier with a background worker thread."""
        # DEC-P0-017 (PR #39): same-module classmethod factory — _start_worker
        # is the private init helper that boots the background thread; the
        # factory legitimately invokes it as part of construction.
        notifier = cls(config=config, transport=transport, synchronous=False)
        notifier._start_worker()  # pyright: ignore[reportPrivateUsage]
        return notifier

    def _start_worker(self) -> None:
        """Start the background daemon thread that drains the publish queue."""
        if self._synchronous or self._running:
            return
        self._running = True
        # Guarded drain-on-exit (R2-OCLI-F2): the CLI fire-and-forget alert
        # paths (the CRITICAL pull-halt alert, sweep_complete) publish via a
        # process-singleton notifier that no exit path stops — the daemon
        # worker was simply killed at interpreter shutdown with those events
        # still in the queue. atexit handlers run BEFORE daemon threads are
        # killed, so a bounded stop() here drains them. stop() is idempotent
        # and unregisters the hook, so a cleanly-stopped notifier neither
        # double-stops nor stays pinned in memory until process exit.
        atexit.register(self.stop)
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="brutex-telegram-notifier",
            daemon=True,
        )
        self._worker.start()

    def stop(self, *, timeout: float = 2.0) -> None:
        """Graceful shutdown — DRAINS queued events, then stops the worker.

        Ordering is the exit-drain guarantee (R2-OCLI-F2): the poison pill
        is enqueued FIRST (behind every already-queued event), the worker is
        joined WITH *timeout* so it sends everything up to the pill, and
        only THEN is ``_running`` flipped False. (The old order flipped
        ``_running`` before the pill, letting the worker's
        ``while self._running`` check exit early and silently DROP queued
        events — e.g. the CRITICAL pull-halt alert published moments before
        CLI exit.) If the pill cannot be enqueued (queue full) or the worker
        outlives *timeout*, the final ``_running = False`` still guarantees
        the daemon worker exits on its next loop check — ``stop()`` itself
        never blocks longer than ~0.5 s (pill enqueue) + *timeout* (join).
        """
        if not self._running:
            return
        # Poison pill FIRST — the worker keeps draining while _running is True.
        with contextlib.suppress(queue.Full):
            self._queue.put(None, timeout=0.5)
        if self._worker is not None:
            self._worker.join(timeout=timeout)
            self._worker = None
        self._running = False
        # A cleanly-stopped notifier no longer needs the exit-drain hook
        # (keeping it would also pin this instance in memory until exit).
        # Safe when called FROM the atexit hook itself, and a no-op if the
        # hook was never registered.
        atexit.unregister(self.stop)

    def __enter__(self) -> Self:
        if not self._synchronous and not self._running:
            self._start_worker()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.stop()

    # ── Publish ────────────────────────────────────────────────────

    def publish(self, event: TelegramEvent) -> None:
        """Publish an event. Never blocks. Never raises.

        If the notifier is disabled, dedupe-drop, full, or fails any
        other way, the stats bucket counter is incremented and the
        caller continues unaffected.
        """
        if not self._config.enabled:
            with self._bucket._lock:  # pyright: ignore[reportPrivateUsage]  # DEC-P0-017: TelegramNotifier owns its TokenBucket; direct lock access avoids exposing the mutex through the public bucket surface.
                self._bucket.dropped_on_disabled += 1
            return

        now = time.monotonic()
        with self._bucket._lock:  # pyright: ignore[reportPrivateUsage]  # DEC-P0-017: TelegramNotifier owns its TokenBucket; direct lock access avoids exposing the mutex through the public bucket surface.
            self._bucket.evict_expired_dedupe_locked(now)
            if self._bucket.check_dedupe_locked(event.kind, event.dedupe_key, now):
                self._bucket.dropped_on_dedupe += 1
                return

        if self._synchronous:
            self._send_one(event)
            return

        try:
            self._queue.put_nowait(event)
        except queue.Full:
            with self._bucket._lock:  # pyright: ignore[reportPrivateUsage]  # DEC-P0-017: TelegramNotifier owns its TokenBucket; direct lock access avoids exposing the mutex through the public bucket surface.
                self._bucket.dropped_on_queue_full += 1
            logger.warning("telegram_queue_full", extra={"kind": event.kind})

    # ── Worker loop ────────────────────────────────────────────────

    def _worker_loop(self) -> None:
        """Background-thread loop: pull events from the queue and call _send_one."""
        while self._running:
            try:
                event = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if event is None:
                break
            self._send_one(event)

    def _send_one(self, event: TelegramEvent) -> None:
        """Send a single :class:`TelegramEvent` via the configured transport.

        Dedupe is stamped ONLY on a successful send (R2-OCLI-F4): a failed
        send leaves no stamp, so the next identical alert is not suppressed.
        A send-time re-check drops identical events that raced past the
        publish-time check while an earlier copy was still in flight.
        """
        with self._bucket._lock:  # pyright: ignore[reportPrivateUsage]  # DEC-P0-017: TelegramNotifier owns its TokenBucket; direct lock access avoids exposing the mutex through the public bucket surface.
            if self._bucket.check_dedupe_locked(event.kind, event.dedupe_key, time.monotonic()):
                self._bucket.dropped_on_dedupe += 1
                return

        try:
            ok = self._transport(self._config, event)
        except Exception as exc:
            logger.warning("telegram_transport_exception", extra={"error": str(exc)})
            ok = False

        with self._bucket._lock:  # pyright: ignore[reportPrivateUsage]  # DEC-P0-017: TelegramNotifier owns its TokenBucket; direct lock access avoids exposing the mutex through the public bucket surface.
            now = time.monotonic()
            if ok:
                self._bucket.sent += 1
                self._bucket.last_success_at = now
                # Stamp the dedupe last-seen only AFTER the successful send.
                self._bucket.record_dedupe_locked(event.kind, event.dedupe_key, now)
            else:
                self._bucket.dropped_on_failure += 1
                self._bucket.last_failure_at = now

    # ── Stats ──────────────────────────────────────────────────────

    def stats(self) -> dict[str, int | float]:
        """Return a snapshot of the bucket stats for the dashboard."""
        with self._bucket._lock:  # pyright: ignore[reportPrivateUsage]  # DEC-P0-017: TelegramNotifier owns its TokenBucket; direct lock access avoids exposing the mutex through the public bucket surface.
            return {
                "sent": self._bucket.sent,
                "dropped_on_failure": self._bucket.dropped_on_failure,
                "dropped_on_dedupe": self._bucket.dropped_on_dedupe,
                "dropped_on_queue_full": self._bucket.dropped_on_queue_full,
                "dropped_on_disabled": self._bucket.dropped_on_disabled,
                "last_success_at": self._bucket.last_success_at,
                "last_failure_at": self._bucket.last_failure_at,
            }

    def drain(self, *, timeout: float = 2.0) -> None:
        """Block until the queue is empty or *timeout* elapses.

        Test helper — not used in production code paths.
        """
        if self._synchronous:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.empty():
                return
            time.sleep(0.01)


__all__ = [
    "DEDUPE_WINDOW_SECONDS",
    "DEFAULT_API_BASE",
    "DEFAULT_HTTP_TIMEOUT",
    "DEFAULT_QUEUE_CAPACITY",
    "ENV_API_BASE",
    "ENV_BOT_TOKEN",
    "ENV_CHAT_ID",
    "ENV_ENABLED",
    "TelegramBucket",
    "TelegramConfig",
    "TelegramConfigError",
    "TelegramError",
    "TelegramEvent",
    "TelegramEventKind",
    "TelegramNotifier",
    "TelegramSeverity",
    "TelegramTransport",
]
