"""Telegram config hygiene tests (GROUP 4 · Task 6b/6c/6d).

* R2-OCLI-F6 token-leak — a scheme-less ``BRUTEX_TELEGRAM_API_BASE`` is
  refused at parse time, AND the transport catches urllib's ``ValueError``
  (whose message embeds the full bot-token URL) logging ONLY the error type.
* R2-CS-2 — ``BRUTEX_TELEGRAM_ENABLED`` values that look truthy but are not
  exactly ``"true"`` log a LOUD warning and stay disabled.
* R2-CS-5 — ``TelegramConfig``'s repr never contains the bot token / chat id.
"""

from __future__ import annotations

import logging

import pytest
from brutex.notifications import telegram as telegram_mod
from brutex.notifications.telegram import (
    ENV_API_BASE,
    ENV_BOT_TOKEN,
    ENV_CHAT_ID,
    ENV_ENABLED,
    TelegramConfig,
    TelegramConfigError,
    TelegramEvent,
    TelegramEventKind,
    TelegramSeverity,
)

_TOKEN = "super-secret-bot-token-98765"  # fixture value, not a real credential


def _set_enabled_env(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    monkeypatch.setenv(ENV_ENABLED, "true")
    monkeypatch.setenv(ENV_BOT_TOKEN, _TOKEN)
    monkeypatch.setenv(ENV_CHAT_ID, "12345")
    for key, value in extra.items():
        monkeypatch.setenv(key, value)


# ────────────────────────────────────────────────────────────────────────
# 6(b) — api_base scheme validation at parse time
# ────────────────────────────────────────────────────────────────────────


class TestApiBaseSchemeValidation:
    def test_schemeless_base_rejected_when_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_enabled_env(monkeypatch, **{ENV_API_BASE: "api.telegram.org"})
        with pytest.raises(TelegramConfigError, match="http") as excinfo:
            TelegramConfig.from_env()
        # The refusal message never carries the token.
        assert _TOKEN not in str(excinfo.value)

    def test_ftp_scheme_rejected_when_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_enabled_env(monkeypatch, **{ENV_API_BASE: "ftp://mock.example"})
        with pytest.raises(TelegramConfigError, match="ftp"):
            TelegramConfig.from_env()

    def test_unparseable_base_rejected_when_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """urlsplit itself raising ValueError maps to TelegramConfigError."""
        _set_enabled_env(monkeypatch, **{ENV_API_BASE: "http://[not-ipv6"})
        with pytest.raises(TelegramConfigError, match="parseable"):
            TelegramConfig.from_env()

    def test_https_base_accepted_when_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_enabled_env(monkeypatch, **{ENV_API_BASE: "https://mock.example"})
        config = TelegramConfig.from_env()
        assert config.api_base == "https://mock.example"
        assert config.enabled is True

    def test_schemeless_base_tolerated_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A disabled config never sends — junk api_base must not crash boot."""
        monkeypatch.setenv(ENV_ENABLED, "false")
        monkeypatch.setenv(ENV_API_BASE, "api.telegram.org")
        config = TelegramConfig.from_env()
        assert config.enabled is False

    def test_blank_base_falls_back_to_default_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A blank override validates (and returns) the https default."""
        _set_enabled_env(monkeypatch, **{ENV_API_BASE: "   "})
        config = TelegramConfig.from_env()
        assert config.api_base == telegram_mod.DEFAULT_API_BASE


# ────────────────────────────────────────────────────────────────────────
# 6(b) — transport ValueError never leaks the token
# ────────────────────────────────────────────────────────────────────────


class TestTransportTokenLeak:
    def test_schemeless_base_valueerror_logs_only_error_type(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Directly-constructed config with a scheme-less base: the transport
        returns False and NO log record contains the token or the URL."""
        config = TelegramConfig(
            bot_token=_TOKEN,
            chat_id="12345",
            enabled=True,
            api_base="api.telegram.org",  # scheme-less → urllib ValueError
        )
        event = TelegramEvent(
            kind=TelegramEventKind.RUN_START,
            severity=TelegramSeverity.INFO,
            message="hi",
        )
        with caplog.at_level(logging.WARNING, logger=telegram_mod.__name__):
            ok = telegram_mod._default_transport(config, event)  # pyright: ignore[reportPrivateUsage]

        assert ok is False
        records = [r for r in caplog.records if r.name == telegram_mod.__name__]
        assert len(records) == 1
        record = records[0]
        assert record.getMessage() == "telegram_invalid_url"
        assert record.error_type == "ValueError"  # type: ignore[attr-defined]
        # The token / URL never appear anywhere in the record.
        blob = repr(record.__dict__)
        assert _TOKEN not in blob
        assert "api.telegram.org/bot" not in blob


# ────────────────────────────────────────────────────────────────────────
# 6(c) — truthy-but-not-'true' ENABLED values warn loudly, stay disabled
# ────────────────────────────────────────────────────────────────────────


class TestEnabledTruthyWarning:
    @pytest.mark.parametrize("value", ["1", "yes", "on", "y", "t", "enable", "enabled", "YES"])
    def test_truthy_alias_warns_and_stays_disabled(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, value: str
    ) -> None:
        monkeypatch.setenv(ENV_ENABLED, value)
        monkeypatch.delenv(ENV_BOT_TOKEN, raising=False)
        monkeypatch.delenv(ENV_CHAT_ID, raising=False)
        with caplog.at_level(logging.WARNING, logger=telegram_mod.__name__):
            config = TelegramConfig.from_env()

        assert config.enabled is False
        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "telegram_enabled_value_not_true" in r.getMessage()
        ]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert ENV_ENABLED in message
        assert "DISABLED" in message

    def test_exact_true_enables_without_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _set_enabled_env(monkeypatch)
        monkeypatch.delenv(ENV_API_BASE, raising=False)
        with caplog.at_level(logging.WARNING, logger=telegram_mod.__name__):
            config = TelegramConfig.from_env()
        assert config.enabled is True
        assert not [
            r for r in caplog.records if "telegram_enabled_value_not_true" in r.getMessage()
        ]

    def test_false_value_neither_warns_nor_enables(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv(ENV_ENABLED, "false")
        with caplog.at_level(logging.WARNING, logger=telegram_mod.__name__):
            config = TelegramConfig.from_env()
        assert config.enabled is False
        assert not [
            r for r in caplog.records if "telegram_enabled_value_not_true" in r.getMessage()
        ]


# ────────────────────────────────────────────────────────────────────────
# 6(d) — repr excludes the secrets
# ────────────────────────────────────────────────────────────────────────


class TestConfigReprRedaction:
    def test_repr_omits_bot_token_and_chat_id(self) -> None:
        config = TelegramConfig(bot_token=_TOKEN, chat_id="chat-secret-777", enabled=True)
        rendered = repr(config)
        assert _TOKEN not in rendered
        assert "chat-secret-777" not in rendered
        # Non-secret fields still render (repr stays useful).
        assert "enabled=True" in rendered
