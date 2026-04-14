"""BruteX math layer — PLACEHOLDER until ``fno-math`` is extracted.

This module is intentionally a stub that raises :exc:`NotImplementedError`
on every attempted import of a Greek, indicator, or strategy. The real
implementation lives in a shared Rust crate ``fno-math`` under
``rust/fno-math/`` in this repository, and is consumed via the PyO3
binding crate ``fno-math-py``.

**Why a placeholder?**

Per ``REQUIREMENTS.md §7A Shared Core Library Contract`` and FR-MATH-004,
BruteX must not contain any Python reimplementation of Greeks, indicators,
or strategies. The single source of truth is the Rust crate, and BruteX
consumes it via a compiled Python wheel. Until that wheel is built (which
happens during the extraction procedure in
``docs/runbooks/fno-math-extraction.md``), this placeholder prevents
accidental usage of a non-existent module.

**When does this go away?**

When the operator runs ``scripts/extract-fno-math.sh`` and builds the
PyO3 wheel, this file is replaced with one-line re-exports from
``fno_math_py``::

    from fno_math_py.greeks import delta, gamma, theta, vega, rho
    from fno_math_py.indicators import sma, ema, rsi, macd, bb, atr
    from fno_math_py.strategies import Strategy, Signal

That's it — the body becomes just imports. No logic. Ever.
"""

from __future__ import annotations

from typing import Any, NoReturn

_EXTRACTION_POINTER = (
    "The shared Rust crate fno-math has not been extracted yet. "
    "Run scripts/extract-fno-math.sh /path/to/tickvault to populate "
    "rust/fno-math/src/, then build the PyO3 wheel with "
    "cd rust/fno-math-py && maturin develop. "
    "Full procedure: docs/runbooks/fno-math-extraction.md. "
    "See REQUIREMENTS.md §7A for the architectural rationale."
)


class FnoMathNotExtractedError(NotImplementedError):
    """Raised when BruteX code tries to use math that has not been extracted yet.

    Inherits :exc:`NotImplementedError` so it shows up in the standard
    Python "feature missing" bucket and is caught by
    ``except NotImplementedError``. The error message always points at
    ``docs/runbooks/fno-math-extraction.md``.
    """

    def __init__(self, symbol: str) -> None:
        super().__init__(
            f"{symbol}() is not available yet. {_EXTRACTION_POINTER}"
        )


def _not_extracted(symbol: str) -> NoReturn:
    """Raise :exc:`FnoMathNotExtractedError` for *symbol*."""
    raise FnoMathNotExtractedError(symbol)


# ────────────────────────────────────────────────────────────────────────
# Facade — a fixed list of function names that future code might try to
# import. Each raises the extraction error if called, so you get a
# clear message instead of a silent ImportError.
#
# When the extraction runs, this entire module is replaced with:
#
#     from fno_math_py.greeks import delta, gamma, theta, vega, rho
#     from fno_math_py.indicators import sma, ema, rsi, macd, bb, atr
#     from fno_math_py.strategies import Strategy, Signal
#
# These stubs are only here to catch the "oops I used math too early"
# case during Phase P1a / P1b / P1c / P2 development.
# ────────────────────────────────────────────────────────────────────────


def delta(*args: Any, **kwargs: Any) -> NoReturn:
    """Black-Scholes delta — PLACEHOLDER, see module docstring."""
    del args, kwargs
    _not_extracted("delta")


def gamma(*args: Any, **kwargs: Any) -> NoReturn:
    """Black-Scholes gamma — PLACEHOLDER."""
    del args, kwargs
    _not_extracted("gamma")


def theta(*args: Any, **kwargs: Any) -> NoReturn:
    """Black-Scholes theta — PLACEHOLDER."""
    del args, kwargs
    _not_extracted("theta")


def vega(*args: Any, **kwargs: Any) -> NoReturn:
    """Black-Scholes vega — PLACEHOLDER."""
    del args, kwargs
    _not_extracted("vega")


def rho(*args: Any, **kwargs: Any) -> NoReturn:
    """Black-Scholes rho — PLACEHOLDER."""
    del args, kwargs
    _not_extracted("rho")


def implied_volatility(*args: Any, **kwargs: Any) -> NoReturn:
    """Implied volatility solver — PLACEHOLDER."""
    del args, kwargs
    _not_extracted("implied_volatility")


def sma(*args: Any, **kwargs: Any) -> NoReturn:
    """Simple moving average — PLACEHOLDER."""
    del args, kwargs
    _not_extracted("sma")


def ema(*args: Any, **kwargs: Any) -> NoReturn:
    """Exponential moving average — PLACEHOLDER."""
    del args, kwargs
    _not_extracted("ema")


def rsi(*args: Any, **kwargs: Any) -> NoReturn:
    """Relative strength index — PLACEHOLDER."""
    del args, kwargs
    _not_extracted("rsi")


def macd(*args: Any, **kwargs: Any) -> NoReturn:
    """Moving average convergence divergence — PLACEHOLDER."""
    del args, kwargs
    _not_extracted("macd")


def bollinger_bands(*args: Any, **kwargs: Any) -> NoReturn:
    """Bollinger bands — PLACEHOLDER."""
    del args, kwargs
    _not_extracted("bollinger_bands")


def atr(*args: Any, **kwargs: Any) -> NoReturn:
    """Average true range — PLACEHOLDER."""
    del args, kwargs
    _not_extracted("atr")


__all__ = [
    "FnoMathNotExtractedError",
    "atr",
    "bollinger_bands",
    "delta",
    "ema",
    "gamma",
    "implied_volatility",
    "macd",
    "rho",
    "rsi",
    "sma",
    "theta",
    "vega",
]
