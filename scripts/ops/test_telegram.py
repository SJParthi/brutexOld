#!/usr/bin/env python3
"""End-to-end Telegram smoke test.

Reads ``BRUTEX_TELEGRAM_*`` env vars, instantiates the production
notifier via :func:`brutex.notifications.wiring.get_or_start_notifier`,
publishes ONE test event, drains the queue, prints success/failure.

Usage::

    python scripts/ops/test_telegram.py

Exit codes:
    0 — Telegram event published OK (check your chat for the message)
    1 — Env vars missing or misconfigured
    2 — Publish succeeded but transport reported failure

Decision: DEC-TELEGRAM-WIRING-001 + DEC-STANDARD-MONITOR-001
(MIGRATION entries #238 + #243).
"""

from __future__ import annotations

import sys
import time


def main() -> int:
    """Send one Telegram test event and report result."""
    # Local imports keep import-time light if the user runs `--help`.
    try:
        from brutex.notifications.telegram import (
            TelegramConfig,
            TelegramConfigError,
            TelegramEvent,
            TelegramEventKind,
            TelegramSeverity,
        )
        from brutex.notifications.wiring import (
            get_or_start_notifier,
            reset_notifier_for_tests,
        )
    except ImportError as exc:
        print(f"✗ Cannot import brutex.notifications.*: {exc}", file=sys.stderr)
        return 1

    # Reset any stale singleton so this script always re-reads env.
    reset_notifier_for_tests()

    try:
        config = TelegramConfig.from_env()
    except TelegramConfigError as exc:
        print(f"✗ Env-var problem: {exc}", file=sys.stderr)
        print(
            "  Set BRUTEX_TELEGRAM_ENABLED=true + BRUTEX_TELEGRAM_BOT_TOKEN + "
            "BRUTEX_TELEGRAM_CHAT_ID. See docs/runbooks/telegram-setup.md.",
            file=sys.stderr,
        )
        return 1

    if not config.enabled:
        print(
            "✗ BRUTEX_TELEGRAM_ENABLED is not 'true' — refusing to send.\n"
            "  Strict decoder requires the literal string 'true' (case-insensitive).",
            file=sys.stderr,
        )
        return 1

    print(
        f"Sending test event to Telegram chat {config.chat_id} "
        f"via bot ending in ...{config.bot_token[-6:]}",
    )

    notifier = get_or_start_notifier(config=config)
    if notifier is None:
        print("✗ Failed to start TelegramNotifier (see logs above).", file=sys.stderr)
        return 1

    event = TelegramEvent(
        kind=TelegramEventKind.TEST,
        severity=TelegramSeverity.INFO,
        message="✅ BruteX Telegram smoke test — env wired correctly.",
        dedupe_key=f"smoke-test-{int(time.time())}",
    )

    try:
        notifier.publish(event)
        notifier.drain(timeout=5.0)
    finally:
        notifier.stop(timeout=2.0)

    stats = notifier.stats()
    sent = int(stats.get("sent", 0))
    dropped = (
        int(stats.get("dropped_on_failure", 0))
        + int(stats.get("dropped_on_queue_full", 0))
        + int(stats.get("dropped_on_disabled", 0))
    )

    if sent >= 1 and dropped == 0:
        print("✓ Telegram event published; check your chat now.")
        return 0

    print(
        f"✗ Telegram publish failed: sent={sent} dropped={dropped} stats={stats}",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":  # pragma: no cover — CLI
    sys.exit(main())
