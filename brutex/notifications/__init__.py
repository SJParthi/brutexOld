"""BruteX notifications — operator-facing alert channels.

v1 ships exactly one channel: Telegram, via
:mod:`brutex.notifications.telegram`. Other channels (Slack, email,
webhook) are deferred to v1.2+.

Per ``REQUIREMENTS.md §15.6 Telegram Alert Channel`` and
``FR-ING-016``.
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
