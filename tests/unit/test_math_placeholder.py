"""Unit tests for the brutex.math placeholder.

These tests exist to enforce FR-MATH-004: until the fno-math extraction
has happened, every public symbol in brutex.math must raise
FnoMathNotExtractedError on call, and the error must contain a pointer
to the extraction runbook.

After the extraction runs, brutex/math/__init__.py is replaced with
real re-exports from fno_math_py, and these placeholder tests are
deleted in the same commit. Their continued existence in git signals
that the extraction is still pending.
"""

from __future__ import annotations

import pytest
from brutex.math import (
    FnoMathNotExtractedError,
    atr,
    bollinger_bands,
    delta,
    ema,
    gamma,
    implied_volatility,
    macd,
    rho,
    rsi,
    sma,
    theta,
    vega,
)

# All placeholder symbols — parametrized for brevity.
PLACEHOLDER_SYMBOLS = [
    ("delta", delta),
    ("gamma", gamma),
    ("theta", theta),
    ("vega", vega),
    ("rho", rho),
    ("implied_volatility", implied_volatility),
    ("sma", sma),
    ("ema", ema),
    ("rsi", rsi),
    ("macd", macd),
    ("bollinger_bands", bollinger_bands),
    ("atr", atr),
]


class TestFnoMathNotExtractedError:
    def test_is_subclass_of_not_implemented(self) -> None:
        """Caught by `except NotImplementedError` out of the box."""
        assert issubclass(FnoMathNotExtractedError, NotImplementedError)

    def test_error_message_includes_symbol(self) -> None:
        err = FnoMathNotExtractedError("my_fn")
        assert "my_fn" in str(err)

    def test_error_message_points_at_runbook(self) -> None:
        err = FnoMathNotExtractedError("delta")
        msg = str(err)
        assert "docs/runbooks/fno-math-extraction.md" in msg

    def test_error_message_mentions_extraction_path(self) -> None:
        # After upgrading brutex.math from hard-placeholder to lazy
        # facade, the placeholder message points at the maturin build
        # path + the extraction runbook rather than the legacy
        # scripts/extract-fno-math.sh shim.
        err = FnoMathNotExtractedError("delta")
        msg = str(err)
        assert "fno-math-py" in msg or "maturin" in msg

    def test_error_message_mentions_maturin(self) -> None:
        err = FnoMathNotExtractedError("delta")
        assert "maturin" in str(err)

    def test_error_message_mentions_requirements_section(self) -> None:
        err = FnoMathNotExtractedError("delta")
        assert "§7A" in str(err)


class TestPlaceholderSymbols:
    """Every placeholder symbol raises on any call, with any args."""

    @pytest.mark.parametrize(("name", "fn"), PLACEHOLDER_SYMBOLS)
    def test_raises_with_no_args(self, name: str, fn: object) -> None:
        with pytest.raises(FnoMathNotExtractedError) as exc_info:
            fn()  # type: ignore[operator]
        assert name in str(exc_info.value)

    @pytest.mark.parametrize(("name", "fn"), PLACEHOLDER_SYMBOLS)
    def test_raises_with_positional_args(self, name: str, fn: object) -> None:
        with pytest.raises(FnoMathNotExtractedError) as exc_info:
            fn(100.0, 100.0, 0.25, 0.07, 0.2, True)  # type: ignore[operator]
        assert name in str(exc_info.value)

    @pytest.mark.parametrize(("name", "fn"), PLACEHOLDER_SYMBOLS)
    def test_raises_with_kwargs(self, name: str, fn: object) -> None:
        with pytest.raises(FnoMathNotExtractedError) as exc_info:
            fn(spot=100.0, strike=100.0)  # type: ignore[operator, call-arg]
        assert name in str(exc_info.value)


class TestPublicApi:
    def test_all_list_matches_module(self) -> None:
        """__all__ must list every public symbol so `from brutex.math import *` works."""
        import brutex.math as m

        for name in m.__all__:
            assert hasattr(m, name), f"brutex.math.__all__ lists {name!r} but it's not defined"

    def test_has_required_greeks(self) -> None:
        import brutex.math as m

        for sym in ("delta", "gamma", "theta", "vega", "rho", "implied_volatility"):
            assert sym in m.__all__

    def test_has_required_indicators(self) -> None:
        import brutex.math as m

        for sym in ("sma", "ema", "rsi", "macd", "bollinger_bands", "atr"):
            assert sym in m.__all__

    def test_has_error_class(self) -> None:
        import brutex.math as m

        assert "FnoMathNotExtractedError" in m.__all__
