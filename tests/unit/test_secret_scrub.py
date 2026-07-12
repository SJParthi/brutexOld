"""Tests for the COMMON secret-scrub primitives (DEC-AWS-SSM-SEAM-COMMON-001)."""

from __future__ import annotations

import pytest
from brutex.adapters.secret_scrub import redact_url_query, scrub_secrets


class TestScrubSecrets:
    def test_masks_every_occurrence(self) -> None:
        assert scrub_secrets("a FAKE-K b FAKE-K", ["FAKE-K"]) == "a *** b ***"

    def test_longest_first_full_mask(self) -> None:
        # A secret that CONTAINS another must be fully masked (longest-first).
        out = scrub_secrets("val=FAKE-LONGKEY", ["FAKE-LONG", "FAKE-LONGKEY"])
        assert "FAKE-LONG" not in out and "KEY" not in out

    def test_blank_secret_skipped(self) -> None:
        assert scrub_secrets("hello", ["", "  "]) == "hello"

    def test_no_secrets(self) -> None:
        assert scrub_secrets("hello", []) == "hello"

    def test_idempotent(self) -> None:
        once = scrub_secrets("x FAKE-K y", ["FAKE-K"])
        assert scrub_secrets(once, ["FAKE-K"]) == once


class TestRedactUrlQuery:
    def test_redacts_query(self) -> None:
        assert (
            redact_url_query("https://auth.dhan.co/app/x?pin=1&totp=2")
            == "https://auth.dhan.co/app/x?[REDACTED]"
        )

    def test_no_query_unchanged(self) -> None:
        assert redact_url_query("https://h/path") == "https://h/path"

    def test_preserves_fragment(self) -> None:
        assert redact_url_query("https://h/p?a=1#frag") == "https://h/p?[REDACTED]#frag"

    def test_non_url_fails_safe(self) -> None:
        assert redact_url_query("not a url at all") == "[REDACTED]"

    def test_bare_scheme_host_only(self) -> None:
        assert redact_url_query("https://host") == "https://host"

    def test_value_error_fails_safe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import brutex.adapters.secret_scrub as mod

        def _boom(_url: str) -> object:
            raise ValueError("bad")

        monkeypatch.setattr(mod, "urlsplit", _boom)
        assert redact_url_query("https://h/p?a=1") == "[REDACTED]"
