"""Unit tests for :mod:`brutex.execution.fill_mode` (DEC-FILL-WORST-CASE-ONLY-001).

Covers the SINGLE-member vocabulary, the removed-knob refusal guard
(``require_fill_mode_env_unset`` — refuses EVERY set value, ``worst_case``
included), and the killed-token pins proving the retired machinery
(``PRECISE`` / ``resolve_fill_mode`` / ``DEFAULT_FILL_MODE``) stays deleted —
100% line + branch on the stdlib-only leaf.
"""

from __future__ import annotations

import pytest
from brutex.execution.fill_mode import (
    FILL_MODE_ENV,
    FillMode,
    FillModeRemovedError,
    require_fill_mode_env_unset,
)


class TestVocabulary:
    """The enum value is the wire/identity token — pinned, single-member."""

    def test_single_member_worst_case_only(self) -> None:
        # DEC-FILL-WORST-CASE-ONLY-001: worst-case is the ONLY fill law —
        # the enum is a typed vocabulary token, never a configuration axis.
        assert [m.value for m in FillMode] == ["worst_case"]

    def test_member_value_pinned(self) -> None:
        # This string enters canonical_trade_params / the tuple digests /
        # the cascade manifest — wire vocabulary, never renamed.
        assert FillMode.WORST_CASE.value == "worst_case"

    def test_precise_member_deleted(self) -> None:
        # Killed-token pin: the retired legacy mode is DELETED, not hidden.
        assert not hasattr(FillMode, "PRECISE")

    def test_env_name_pinned(self) -> None:
        # Retained ONLY so the refusal guard can name the stale variable.
        assert FILL_MODE_ENV == "BRUTEX_FILL_MODE"

    def test_resolver_machinery_deleted(self) -> None:
        # Killed-token pins: the env resolver + production-default constant
        # are gone from the module surface (nothing resolves a mode any more).
        import brutex.execution.fill_mode as mod

        assert not hasattr(mod, "resolve_fill_mode")
        assert not hasattr(mod, "DEFAULT_FILL_MODE")

    def test_all_surface_pinned(self) -> None:
        import brutex.execution.fill_mode as mod

        assert mod.__all__ == [
            "FILL_MODE_ENV",
            "FillMode",
            "FillModeRemovedError",
            "require_fill_mode_env_unset",
        ]


class TestRequireFillModeEnvUnset:
    """Every guard branch: unset / empty / whitespace pass; ANY value refuses."""

    def test_unset_passes(self) -> None:
        require_fill_mode_env_unset({})  # no raise

    def test_empty_string_passes(self) -> None:
        # The common `VAR= cmd` unset idiom — nothing was configured.
        require_fill_mode_env_unset({FILL_MODE_ENV: ""})

    def test_whitespace_only_passes(self) -> None:
        require_fill_mode_env_unset({FILL_MODE_ENV: "   "})

    @pytest.mark.parametrize("raw", ["worst_case", "precise", "WORST_CASE", "junk", " precise "])
    def test_any_set_value_refuses(self, raw: str) -> None:
        # The fails-before refusal (DEC-FILL-WORST-CASE-ONLY-001): the knob is
        # REMOVED, so even the spelling that matches the one law refuses —
        # silently accepting it would bless stale deployment configs.
        with pytest.raises(FillModeRemovedError) as exc_info:
            require_fill_mode_env_unset({FILL_MODE_ENV: raw})
        message = str(exc_info.value)
        assert "DEC-FILL-WORST-CASE-ONLY-001" in message
        assert FILL_MODE_ENV in message

    def test_error_is_a_value_error(self) -> None:
        # The CLI launch guards catch ValueError → clean EXIT_USER_ERROR.
        assert issubclass(FillModeRemovedError, ValueError)

    def test_default_env_reads_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(FILL_MODE_ENV, "worst_case")
        with pytest.raises(FillModeRemovedError):
            require_fill_mode_env_unset()
        monkeypatch.delenv(FILL_MODE_ENV)
        require_fill_mode_env_unset()  # no raise once unset
