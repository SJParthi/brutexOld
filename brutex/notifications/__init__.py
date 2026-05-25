"""BruteX notifications — operator-facing alert channels.

v1 ships exactly one channel: Telegram, via
:mod:`brutex.notifications.telegram`. Other channels (Slack, email,
webhook) are deferred to v1.2+.

Per ``REQUIREMENTS.md §15.6 Telegram Alert Channel`` and
``FR-ING-016``.

.. note::

   DEC-TELEGRAM-WIRING-001 (2026-05-25): wiring helpers from
   :mod:`brutex.notifications.wiring` are NOT re-exported at the
   package level. ``brutex.notifications.wiring`` transitively imports
   ``brutex.orchestrator.*`` (for the ``EventSubscriber`` protocol),
   which pulls in ``brutex/orchestrator/__init__.py`` →
   ``brutex.orchestrator.bloom`` → ``fastbloom_rs`` (a PyO3 abi3-py38
   wheel). Eagerly re-exporting wiring at this package level would
   cause every import of ``brutex.notifications`` (e.g. the existing
   ``test_notifications_telegram.py`` strict-100% gate) to trigger the
   orchestrator init chain, which then collides with pytest-cov's
   pre-import mechanism — emitting *"modules compiled for CPython 3.8
   or older may only be initialized once per interpreter process"*.

   Callers that need the wiring helpers should import directly from
   :mod:`brutex.notifications.wiring` (``get_or_start_notifier`` and
   ``make_pipeline_event_subscriber``).
"""

from __future__ import annotations

from brutex.notifications.telegram import (
    TelegramBucket,
    TelegramConfig,
    TelegramEvent,
    TelegramEventKind,
    TelegramNotifier,
    TelegramSeverity,
)

__all__ = [
    "TelegramBucket",
    "TelegramConfig",
    "TelegramEvent",
    "TelegramEventKind",
    "TelegramNotifier",
    "TelegramSeverity",
]
