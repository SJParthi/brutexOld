"""BruteX notifications — operator-facing alert channels.

v1 ships exactly one channel: Telegram, via
:mod:`brutex.notifications.telegram`. Other channels (Slack, email,
webhook) are deferred to v1.2+.

Per ``REQUIREMENTS.md §15.6 Telegram Alert Channel`` and
``FR-ING-016``.
"""

from __future__ import annotations

# DEC-TELEGRAM-WIRING-001 (2026-05-25): wiring helpers are NOT
# re-exported at package level because brutex.notifications.wiring
# transitively imports brutex.orchestrator.* (for the EventSubscriber
# protocol). That import chain pulls in brutex/orchestrator/__init__.py
# which loads brutex.orchestrator.bloom → fastbloom_rs (a PyO3
# abi3-py38 wheel). Eagerly re-exporting wiring at the
# brutex.notifications package level causes EVERY import of
# brutex.notifications (e.g. test_notifications_telegram.py's `from
# brutex.notifications.telegram import ...`) to trigger the orchestrator
# init chain, which then collides with pytest-cov's pre-import
# mechanism in strict-100% gates ("modules compiled for CPython 3.8 or
# older may only be initialized once per interpreter process").
#
# Callers that need the wiring helpers should import them directly:
#   from brutex.notifications.wiring import (
#       get_or_start_notifier,
#       make_pipeline_event_subscriber,
#   )
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
