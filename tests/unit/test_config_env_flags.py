"""Unit gate for :mod:`brutex.config.env_flags` — the ONE env-flag vocabulary leaf.

DEC-PREBUILD-FRAME-WRITE-001: the truthy/falsy literal sets +
:func:`env_flag_tristate` were PROMOTED here from
:mod:`brutex.engine.sweep_lookahead` (which re-exports them byte-compatibly)
so the wheel-free ``brutex.data.frame_write_gate`` leaf can consume the same
vocabulary without importing the engine package (whose ``__init__`` needs the
compiled ``fno_math`` wheel). This file pins:

* the classifier truth table (every literal, case/whitespace variants, junk);
* the exact literal-set contents (append-only vocabulary — a silent widening
  would change every flag's parse);
* the engine re-export identity (the SAME function object — no copy).
"""

from __future__ import annotations

import pytest
from brutex.config.env_flags import FALSY_LITERALS, TRUTHY_LITERALS, env_flag_tristate


class TestEnvFlagTristate:
    """The classifier truth table."""

    @pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "TRUE", " On ", "\tYES\n"])
    def test_truthy_literals_classify_true(self, raw: str) -> None:
        assert env_flag_tristate(raw) is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", "FALSE", " Off ", "\tNO\n"])
    def test_falsy_literals_classify_false(self, raw: str) -> None:
        assert env_flag_tristate(raw) is False

    @pytest.mark.parametrize("raw", ["", "   ", "2", "flase", "0ff", "shadow", "None", "y"])
    def test_everything_else_classifies_none(self, raw: str) -> None:
        """Unset/empty AND unrecognised both map to ``None`` — the caller owns the policy."""
        assert env_flag_tristate(raw) is None


class TestLiteralSets:
    """The exact vocabulary — append-only; a silent widening moves every flag."""

    def test_truthy_literals_exact(self) -> None:
        assert frozenset({"1", "true", "yes", "on"}) == TRUTHY_LITERALS

    def test_falsy_literals_exact(self) -> None:
        assert frozenset({"0", "false", "no", "off"}) == FALSY_LITERALS

    def test_sets_disjoint(self) -> None:
        assert not (TRUTHY_LITERALS & FALSY_LITERALS)


class TestEngineReExport:
    """The engine home re-exports the SAME objects — never a drifting copy.

    Wheel-gated: importing ``brutex.engine`` needs the compiled ``fno_math``
    wheel, which the module-relocation rationale says the CONFIG leaf must not
    require — so this identity pin (which needs both sides importable) skips
    honestly where the wheel is absent instead of failing the vocabulary gate.
    """

    def test_engine_alias_is_same_object(self) -> None:
        pytest.importorskip("fno_math")
        from brutex.engine import sweep_lookahead

        assert sweep_lookahead.env_flag_tristate is env_flag_tristate
