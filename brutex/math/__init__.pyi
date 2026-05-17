"""Type stub for brutex.math.

The runtime module populates symbols via `globals()[name] = ...` in a try/except
branch (see brutex/math/__init__.py docstring). That dynamic assignment is
invisible to pyright + mypy static analysis, so this stub re-declares the
symbols explicitly. The actual call signatures match the fno_math_py PyO3
wheel; when the wheel is missing the placeholder shims raise
FnoMathNotExtractedError on call (not on import) so the typed signature here
still holds for type-checking callers.

Per DEC-P0-013 (PR #35): adding this stub closes 12 + 5 = 17 pyright
reportAttributeAccessIssue errors in tests/unit/test_math_placeholder.py and
tests/unit/test_math_native.py without project-wide silencing.
"""

from collections.abc import Sequence
from typing import Any, Final

__all__: list[str]

FNO_MATH_BACKEND: str

class FnoMathNotExtractedError(NotImplementedError):
    def __init__(self, symbol: str) -> None: ...

# Greeks — signatures match the fno_math_py PyO3 wheel. `Any` is used for
# return types because the wheel is optional; concrete typing comes from the
# Rust crate when the wheel is built. Tests only assert call behavior.
def delta(*args: Any, **kwargs: Any) -> Any: ...
def gamma(*args: Any, **kwargs: Any) -> Any: ...
def implied_volatility(*args: Any, **kwargs: Any) -> Any: ...
def rho(*args: Any, **kwargs: Any) -> Any: ...
def theta(*args: Any, **kwargs: Any) -> Any: ...
def vega(*args: Any, **kwargs: Any) -> Any: ...

# Indicators — same convention as Greeks above.
def atr(*args: Any, **kwargs: Any) -> Any: ...
def bollinger_bands(*args: Any, **kwargs: Any) -> Any: ...
def ema(*args: Any, **kwargs: Any) -> Any: ...
def macd(*args: Any, **kwargs: Any) -> Any: ...
def rsi(*args: Any, **kwargs: Any) -> Any: ...
def sma(*args: Any, **kwargs: Any) -> Any: ...

# Silence unused-import warnings on Final + Sequence + Any in this stub
# (kept for forward compatibility with future signature tightening).
_FINAL_PLACEHOLDER: Final[int] = 0
_SEQ_PLACEHOLDER: Sequence[int] = ()
