"""Unit tests for :mod:`brutex.execution.fill_mode` (DEC-FILL-WORST-CASE-001).

Covers the two-member vocabulary, the production default, and every branch of
the fail-loud ``$BRUTEX_FILL_MODE`` resolver — 100% line + branch on the
stdlib-only leaf.
"""

from __future__ import annotations

import pytest
from brutex.execution.fill_mode import (
    DEFAULT_FILL_MODE,
    FILL_MODE_ENV,
    FillMode,
    resolve_fill_mode,
)


class TestVocabulary:
    """The enum values are the wire/identity tokens — pinned."""

    def test_member_values_pinned(self) -> None:
        # These strings enter canonical_trade_params / the tuple digests /
        # the cascade manifest — append-only wire vocabulary, never renamed.
        assert FillMode.WORST_CASE.value == "worst_case"
        assert FillMode.PRECISE.value == "precise"
        assert [m.value for m in FillMode] == ["worst_case", "precise"]

    def test_production_default_is_worst_case(self) -> None:
        # DEC-FILL-WORST-CASE-001: the anti-illusion floor is the DEFAULT at
        # the production config layer.
        assert DEFAULT_FILL_MODE is FillMode.WORST_CASE

    def test_env_name_pinned(self) -> None:
        assert FILL_MODE_ENV == "BRUTEX_FILL_MODE"


class TestResolveFillMode:
    """Every resolver branch: default / explicit / case-fold / fail-loud."""

    def test_unset_returns_default(self) -> None:
        assert resolve_fill_mode({}) is DEFAULT_FILL_MODE

    def test_blank_returns_default(self) -> None:
        # Whitespace-only is "unset" — an empty export must not fail a launch.
        assert resolve_fill_mode({FILL_MODE_ENV: "   "}) is DEFAULT_FILL_MODE

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("worst_case", FillMode.WORST_CASE),
            ("precise", FillMode.PRECISE),
            ("WORST_CASE", FillMode.WORST_CASE),
            ("Precise", FillMode.PRECISE),
            ("  precise  ", FillMode.PRECISE),
        ],
    )
    def test_explicit_values_case_insensitive(self, raw: str, expected: FillMode) -> None:
        assert resolve_fill_mode({FILL_MODE_ENV: raw}) is expected

    @pytest.mark.parametrize("junk", ["worst", "1", "true", "worstcase", "précise"])
    def test_junk_fails_loud(self, junk: str) -> None:
        # The BRUTEX_LOG_LEVEL fail-loud precedent: a junk env var raises a
        # typed ValueError NAMING the variable + the valid tokens — never a
        # silent fallback to a mode the operator did not choose.
        with pytest.raises(ValueError, match=FILL_MODE_ENV):
            resolve_fill_mode({FILL_MODE_ENV: junk})

    def test_junk_message_names_valid_tokens_and_value(self) -> None:
        with pytest.raises(ValueError, match=r"worst_case, precise") as excinfo:
            resolve_fill_mode({FILL_MODE_ENV: "junk"})
        assert "'junk'" in str(excinfo.value)

    def test_default_env_source_is_os_environ(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(FILL_MODE_ENV, "precise")
        assert resolve_fill_mode() is FillMode.PRECISE
        monkeypatch.delenv(FILL_MODE_ENV)
        assert resolve_fill_mode() is DEFAULT_FILL_MODE
