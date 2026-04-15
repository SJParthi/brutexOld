"""BruteX math layer — re-exports from the ``fno_math_py`` PyO3 wheel.

Per ``REQUIREMENTS.md §7A Shared Core Library Contract`` and
``FR-MATH-004``, BruteX must not contain any Python reimplementation
of Greeks, indicators, or strategies. The single source of truth is
the Rust crate ``rust/fno-math/`` in this repository, which is also
consumed by ``tickvault`` (live trading) via a Cargo path dep. BruteX
consumes the same logic via a compiled PyO3 wheel named
``fno_math_py`` built from ``rust/fno-math-py/``.

## Module contract

When the wheel **is** installed, every symbol in :data:`__all__`
(``delta``, ``gamma``, ``sma``, ``rsi`` …) points directly at the
native Rust function — zero Python shim, no wrapper cost.

When the wheel is **not yet** installed (e.g. a fresh checkout where
the operator hasn't run ``maturin develop`` yet), importing any
symbol from this module still succeeds, but calling the function
raises :class:`FnoMathNotExtractedError` with a clear pointer to
``docs/runbooks/fno-math-extraction.md``.

This split lets downstream code write ``from brutex.math import
delta`` unconditionally — the moment the wheel lands in
``site-packages``, the real implementation is picked up without any
code change on the BruteX side.

## Runtime introspection

Callers that need to branch on availability can check
:data:`FNO_MATH_BACKEND`:

* ``"native"`` — :mod:`fno_math_py` imported OK; symbols are real
* ``"placeholder"`` — :mod:`fno_math_py` missing; symbols raise on call

Tests in :mod:`tests.unit.test_math_facade` exercise both paths.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final, NoReturn

_EXTRACTION_POINTER: Final[str] = (
    "fno_math_py wheel is not installed. Build it via:\n"
    "  cd rust/fno-math-py && maturin develop --release\n"
    "Full procedure: docs/runbooks/fno-math-extraction.md.\n"
    "See REQUIREMENTS.md §7A for the architectural rationale."
)


class FnoMathNotExtractedError(NotImplementedError):
    """Raised when math is called before the PyO3 wheel is built.

    Inherits :exc:`NotImplementedError` so it shows up in the standard
    "feature missing" bucket and is catchable via
    ``except NotImplementedError``.
    """

    def __init__(self, symbol: str) -> None:
        super().__init__(f"{symbol}() is unavailable. {_EXTRACTION_POINTER}")


# ────────────────────────────────────────────────────────────────────────
# Backend detection
# ────────────────────────────────────────────────────────────────────────


def _make_placeholder(symbol: str) -> Callable[..., NoReturn]:
    """Build a callable that raises :class:`FnoMathNotExtractedError`.

    One factory per symbol so the traceback frame names the exact
    function the caller tried to use.
    """

    def _placeholder(*args: Any, **kwargs: Any) -> NoReturn:
        del args, kwargs
        raise FnoMathNotExtractedError(symbol)

    _placeholder.__name__ = symbol
    _placeholder.__qualname__ = f"brutex.math.{symbol}"
    _placeholder.__doc__ = (
        f"Placeholder for {symbol}. Raises FnoMathNotExtractedError "
        f"if called — install fno_math_py to use the real Rust "
        f"implementation. See :mod:`brutex.math`."
    )
    return _placeholder


#: Symbols we re-export from fno_math_py when available. Keep in
#: lexicographic order so the ``__all__`` list and the test matrix
#: stay consistent.
_GREEK_SYMBOLS: Final[tuple[str, ...]] = (
    "delta",
    "gamma",
    "implied_volatility",
    "rho",
    "theta",
    "vega",
)

_INDICATOR_SYMBOLS: Final[tuple[str, ...]] = (
    "atr",
    "bollinger_bands",
    "ema",
    "macd",
    "rsi",
    "sma",
)


FNO_MATH_BACKEND: str
"""Which backend is currently wired — ``"native"`` or ``"placeholder"``."""


# Try the native import first. If successful, each symbol points at
# the real Rust function. Otherwise fall back to placeholder shims.
try:
    import fno_math_py as _native
except ImportError:
    FNO_MATH_BACKEND = "placeholder"
    for _sym in _GREEK_SYMBOLS + _INDICATOR_SYMBOLS:
        globals()[_sym] = _make_placeholder(_sym)
else:
    FNO_MATH_BACKEND = "native"
    # Re-export — we don't care which submodule the symbol lives in,
    # we just want the name resolvable at ``brutex.math.<name>``.
    # fno_math_py is expected to expose greeks + indicators at the
    # top level per the extraction runbook.
    for _sym in _GREEK_SYMBOLS + _INDICATOR_SYMBOLS:
        try:
            globals()[_sym] = getattr(_native, _sym)
        except AttributeError:
            # Backward-compat: if a wheel is built from an older fno-math
            # that is missing a symbol, fall back to the placeholder for
            # that specific name rather than crashing the whole import.
            globals()[_sym] = _make_placeholder(_sym)


__all__ = [
    "FNO_MATH_BACKEND",
    "FnoMathNotExtractedError",
    *_GREEK_SYMBOLS,
    *_INDICATOR_SYMBOLS,
]
