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
  When the current call is within the window, drop. Window expires via
  lazy eviction during publish.
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

import json
import logging
import os
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from enum import StrEnum
from types import TracebackType
from typing import Callable, Final, Self

from brutex.models import BruteXError

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

#: Env var names.
ENV_BOT_TOKEN: Final[str] = "BRUTEX_TELEGRAM_BOT_TOKEN"
ENV_CHAT_ID: Final[str] = "BRUTEX_TELEGRAM_CHAT_ID"
ENV_ENABLED: Final[str] = "BRUTEX_TELEGRAM_ENABLED"
ENV_API_BASE: Final[str] = "BRUTEX_TELEGRAM_API_BASE"


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
    TelegramEventKind.RUN_HEARTBEAT: 600.0,  # 10 min
    TelegramEventKind.RUN_COMPLETE: 0.0,
    TelegramEventKind.TEST: 0.0,
}


# ────────────────────────────────────────────────────────────────────────
# Dataclasses
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    """Loaded from env. Immutable after construction."""

    bot_token: str
    chat_id: str
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
            If enabled but the bot token or chat id is missing.
        """
        enabled_raw = os.environ.get(ENV_ENABLED, "false").strip().lower()
        enabled = enabled_raw == "true"

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

        Caller holds ``self._lock``.
        """
        window = DEDUPE_WINDOW_SECONDS.get(kind, 0.0)
        if window <= 0.0:
            return False
        key = (kind, dedupe_key)
        last_seen = self._dedupe_cache.get(key)
        if last_seen is not None and (now - last_seen) < window:
            return True
        self._dedupe_cache[key] = now
        return False

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

    req = urllib.request.Request(  # noqa: S310 — URL is under our control
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=config.http_timeout) as resp:  # noqa: S310
            status = getattr(resp, "status", 200)
            return 200 <= status < 300
    except urllib.error.HTTPError as exc:
        logger.warning("telegram_http_error", extra={"status": exc.code})
        return False
    except urllib.error.URLError as exc:
        logger.warning("telegram_url_error", extra={"reason": str(exc.reason)})
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
        self._queue: queue.Queue[TelegramEvent | None] = queue.Queue(
            maxsize=config.queue_capacity
        )
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
        notifier = cls(config=config, transport=transport, synchronous=False)
        notifier._start_worker()
        return notifier

    def _start_worker(self) -> None:
        if self._synchronous or self._running:
            return
        self._running = True
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="brutex-telegram-notifier",
            daemon=True,
        )
        self._worker.start()

    def stop(self, *, timeout: float = 2.0) -> None:
        """Graceful shutdown. Drains pending events with a timeout."""
        if not self._running:
            return
        self._running = False
        # Poison pill
        try:
            self._queue.put(None, timeout=0.5)
        except queue.Full:
            pass
        if self._worker is not None:
            self._worker.join(timeout=timeout)
            self._worker = None

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
            with self._bucket._lock:  # noqa: SLF001
                self._bucket.dropped_on_disabled += 1
            return

        now = time.monotonic()
        with self._bucket._lock:  # noqa: SLF001
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
            with self._bucket._lock:  # noqa: SLF001
                self._bucket.dropped_on_queue_full += 1
            logger.warning("telegram_queue_full", extra={"kind": event.kind})

    # ── Worker loop ────────────────────────────────────────────────

    def _worker_loop(self) -> None:
        while self._running:
            try:
                event = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if event is None:
                break
            self._send_one(event)

    def _send_one(self, event: TelegramEvent) -> None:
        try:
            ok = self._transport(self._config, event)
        except Exception as exc:  # noqa: BLE001
            logger.warning("telegram_transport_exception", extra={"error": str(exc)})
            ok = False

        with self._bucket._lock:  # noqa: SLF001
            now = time.monotonic()
            if ok:
                self._bucket.sent += 1
                self._bucket.last_success_at = now
            else:
                self._bucket.dropped_on_failure += 1
                self._bucket.last_failure_at = now

    # ── Stats ──────────────────────────────────────────────────────

    def stats(self) -> dict[str, int | float]:
        """Return a snapshot of the bucket stats for the dashboard."""
        with self._bucket._lock:  # noqa: SLF001
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
