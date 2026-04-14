"""Unit tests for brutex.math — the facade over the fno_math_py wheel.

Exercises **both** backend paths:

* **Placeholder path** — what happens when ``fno_math_py`` is not
  installed. Every symbol is importable but calling any of them
  raises :class:`FnoMathNotExtractedError`.

* **Native path** — what happens when ``fno_math_py`` is installed.
  We simulate this by injecting a fake ``fno_math_py`` module into
  ``sys.modules`` and re-importing :mod:`brutex.math`. Calling a
  symbol must dispatch to the fake's implementation.

This ensures the facade is future-proof: the moment the operator
runs ``maturin develop`` and installs the real wheel, BruteX picks
up the native functions without any further code change.
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest


# ────────────────────────────────────────────────────────────────────────
# Placeholder path
# ────────────────────────────────────────────────────────────────────────


class TestPlaceholderBackend:
    """The default path when fno_math_py is not installed."""

    def setup_method(self) -> None:
        # Force a fresh import with no fno_math_py in sys.modules.
        sys.modules.pop("fno_math_py", None)
        sys.modules.pop("brutex.math", None)
        self.math = importlib.import_module("brutex.math")

    def test_backend_is_placeholder(self) -> None:
        assert self.math.FNO_MATH_BACKEND == "placeholder"

    def test_every_symbol_is_importable(self) -> None:
        """The public API surface must be stable even without the wheel."""
        for symbol in [
            "delta",
            "gamma",
            "theta",
            "vega",
            "rho",
            "implied_volatility",
            "sma",
            "ema",
            "rsi",
            "macd",
            "bollinger_bands",
            "atr",
        ]:
            assert hasattr(self.math, symbol), f"{symbol} missing from facade"

    def test_calling_delta_raises_fno_math_not_extracted(self) -> None:
        with pytest.raises(
            self.math.FnoMathNotExtractedError,
            match="delta",
        ):
            self.math.delta(100.0, 100.0, 0.05, 0.2, 30 / 365)

    def test_calling_rsi_raises_fno_math_not_extracted(self) -> None:
        with pytest.raises(self.math.FnoMathNotExtractedError, match="rsi"):
            self.math.rsi([1.0, 2.0, 3.0], 14)

    def test_error_message_points_at_runbook(self) -> None:
        try:
            self.math.gamma(100.0, 100.0, 0.05, 0.2, 30 / 365)
        except self.math.FnoMathNotExtractedError as exc:
            msg = str(exc)
            assert "fno_math_py" in msg
            assert "maturin" in msg
            assert "fno-math-extraction.md" in msg
        else:
            pytest.fail(
                "FnoMathNotExtractedError was not raised"
            )

    def test_placeholder_has_correct_name(self) -> None:
        """Traceback frames must name the actual function, not _placeholder."""
        assert self.math.delta.__name__ == "delta"
        assert self.math.delta.__qualname__ == "brutex.math.delta"

    def test_inherits_not_implemented_error(self) -> None:
        """Catchable as plain NotImplementedError for dev ergonomics."""
        with pytest.raises(NotImplementedError):
            self.math.theta(100.0, 100.0, 0.05, 0.2, 30 / 365)


# ────────────────────────────────────────────────────────────────────────
# Native path — simulate a real fno_math_py being installed
# ────────────────────────────────────────────────────────────────────────


class TestNativeBackend:
    """Simulate fno_math_py being installed and verify re-export."""

    def setup_method(self) -> None:
        # Build a fake fno_math_py module with stub functions that
        # return sentinel values so we can assert dispatch.
        fake = types.ModuleType("fno_math_py")
        fake.delta = lambda *a, **kw: "NATIVE_DELTA"  # type: ignore[attr-defined]
        fake.gamma = lambda *a, **kw: "NATIVE_GAMMA"  # type: ignore[attr-defined]
        fake.theta = lambda *a, **kw: "NATIVE_THETA"  # type: ignore[attr-defined]
        fake.vega = lambda *a, **kw: "NATIVE_VEGA"  # type: ignore[attr-defined]
        fake.rho = lambda *a, **kw: "NATIVE_RHO"  # type: ignore[attr-defined]
        fake.implied_volatility = (  # type: ignore[attr-defined]
            lambda *a, **kw: "NATIVE_IV"
        )
        fake.sma = lambda *a, **kw: "NATIVE_SMA"  # type: ignore[attr-defined]
        fake.ema = lambda *a, **kw: "NATIVE_EMA"  # type: ignore[attr-defined]
        fake.rsi = lambda *a, **kw: "NATIVE_RSI"  # type: ignore[attr-defined]
        fake.macd = lambda *a, **kw: "NATIVE_MACD"  # type: ignore[attr-defined]
        fake.bollinger_bands = (  # type: ignore[attr-defined]
            lambda *a, **kw: "NATIVE_BB"
        )
        fake.atr = lambda *a, **kw: "NATIVE_ATR"  # type: ignore[attr-defined]

        sys.modules["fno_math_py"] = fake
        sys.modules.pop("brutex.math", None)
        self.math = importlib.import_module("brutex.math")

    def teardown_method(self) -> None:
        sys.modules.pop("fno_math_py", None)
        sys.modules.pop("brutex.math", None)

    def test_backend_is_native(self) -> None:
        assert self.math.FNO_MATH_BACKEND == "native"

    def test_delta_dispatches_to_native(self) -> None:
        assert self.math.delta(100.0, 100.0, 0.05, 0.2, 30 / 365) == "NATIVE_DELTA"

    def test_rsi_dispatches_to_native(self) -> None:
        assert self.math.rsi([1, 2, 3], 14) == "NATIVE_RSI"

    def test_every_symbol_dispatches(self) -> None:
        expected = {
            "delta": "NATIVE_DELTA",
            "gamma": "NATIVE_GAMMA",
            "theta": "NATIVE_THETA",
            "vega": "NATIVE_VEGA",
            "rho": "NATIVE_RHO",
            "implied_volatility": "NATIVE_IV",
            "sma": "NATIVE_SMA",
            "ema": "NATIVE_EMA",
            "rsi": "NATIVE_RSI",
            "macd": "NATIVE_MACD",
            "bollinger_bands": "NATIVE_BB",
            "atr": "NATIVE_ATR",
        }
        for name, expected_value in expected.items():
            fn = getattr(self.math, name)
            assert fn() == expected_value, (
                f"{name} did not dispatch to native — "
                f"got {fn()!r}, expected {expected_value!r}"
            )


# ────────────────────────────────────────────────────────────────────────
# Partial-native path — fno_math_py missing some symbols
# ────────────────────────────────────────────────────────────────────────


class TestPartialNativeBackend:
    """If an older fno_math_py wheel is missing a symbol, that symbol
    alone should fall back to the placeholder — the whole import
    must not crash.
    """

    def setup_method(self) -> None:
        fake = types.ModuleType("fno_math_py")
        # Only provide delta. Every other symbol is missing.
        fake.delta = lambda *a, **kw: "NATIVE_DELTA"  # type: ignore[attr-defined]
        sys.modules["fno_math_py"] = fake
        sys.modules.pop("brutex.math", None)
        self.math = importlib.import_module("brutex.math")

    def teardown_method(self) -> None:
        sys.modules.pop("fno_math_py", None)
        sys.modules.pop("brutex.math", None)

    def test_available_symbol_is_native(self) -> None:
        assert self.math.FNO_MATH_BACKEND == "native"
        assert self.math.delta() == "NATIVE_DELTA"

    def test_missing_symbol_falls_back_to_placeholder(self) -> None:
        with pytest.raises(
            self.math.FnoMathNotExtractedError, match="gamma"
        ):
            self.math.gamma()


# ────────────────────────────────────────────────────────────────────────
# __all__ contract
# ────────────────────────────────────────────────────────────────────────


class TestAllContract:
    def setup_method(self) -> None:
        sys.modules.pop("fno_math_py", None)
        sys.modules.pop("brutex.math", None)
        self.math = importlib.import_module("brutex.math")

    def test_all_contains_greeks_and_indicators(self) -> None:
        all_names = set(self.math.__all__)
        for expected in [
            "delta",
            "gamma",
            "theta",
            "vega",
            "rho",
            "implied_volatility",
            "sma",
            "ema",
            "rsi",
            "macd",
            "bollinger_bands",
            "atr",
            "FnoMathNotExtractedError",
            "FNO_MATH_BACKEND",
        ]:
            assert expected in all_names, f"{expected} missing from __all__"

    def test_all_is_sorted_alphabetically_for_greeks_and_indicators(
        self,
    ) -> None:
        """Catch drift — the internal tuples are sorted and __all__ should
        preserve that ordering."""
        # Slice out the math symbols (skip FNO_MATH_BACKEND + error class)
        math_symbols = [
            s
            for s in self.math.__all__
            if s not in {"FNO_MATH_BACKEND", "FnoMathNotExtractedError"}
        ]
        # Should decompose into Greeks block then Indicators block,
        # each sorted.
        assert math_symbols == sorted(math_symbols[:6]) + sorted(
            math_symbols[6:]
        )
