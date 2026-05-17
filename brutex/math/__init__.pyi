"""Type stub for brutex.math.

The runtime module populates symbols via ``globals()[name] = ...`` in a try/except
branch (see brutex/math/__init__.py docstring). That dynamic assignment is
invisible to pyright + mypy static analysis, so this stub re-declares the
symbols explicitly.

Per DEC-P0-013 (PR #35): adding this stub closed 17 pyright
reportAttributeAccessIssue errors in tests/unit/test_math_placeholder.py and
tests/unit/test_math_native.py without project-wide silencing.

Per DEC-P0-016 (PR #38): the 6 Greek signatures have been tightened from
``(*args: Any, **kwargs: Any) -> Any`` to the concrete shape exposed by the
PyO3 wheel at ``rust/fno-math-py/src/lib.rs:224-343``. The 6 bar-level
indicator symbols (atr, bollinger_bands, ema, macd, rsi, sma) remain
loose-typed because the Rust crate at ``rust/fno-math/src/lib.rs:102-105``
documents them as "not yet present... Phase 2 per MG-043". Pinning concrete
signatures for these would violate Golden Rule #1 (no hallucination) —
caller code would type-check green and fail at runtime with
``FnoMathNotExtractedError``.

Precision discipline (Golden Rule #5, Agent 2 of PR #38 audit): the Greek
signatures accept ``float`` to match the PyO3 ``f64`` FFI boundary. Callers
in monetary contexts (brutex/costs/, brutex/execution/) MUST deliberately
convert ``Decimal`` paise / rupees to ``float`` rupees before calling.
Direct ``Decimal`` argument passing would silent-cast at the FFI and lose
precision. See ``brutex/costs/calculator.py`` for the canonical
int64-paise discipline.
"""

from collections.abc import Sequence
from typing import Any, Final

__all__: list[str]

FNO_MATH_BACKEND: str

class FnoMathNotExtractedError(NotImplementedError):
    def __init__(self, symbol: str) -> None: ...

# ────────────────────────────────────────────────────────────────────────
# Greeks — signatures match rust/fno-math-py/src/lib.rs:224-343 verbatim.
# Each one wraps a scalar BSM computation; O(1) per call (single d1/d2
# evaluation). ``side`` is parsed via ``parse_side`` in
# rust/fno-math-py/src/lib.rs:54-62 (case-insensitive
# "CE"/"C"/"CALL"/"PE"/"P"/"PUT"; ValueError otherwise). The stub uses
# plain ``str`` (not ``Literal[...]``) per Agent 3 of PR #38 review —
# validation belongs at the Rust FFI boundary, not the Python type layer.
# ────────────────────────────────────────────────────────────────────────

def delta(
    side: str,
    spot: float,
    strike: float,
    time: float,
    rate: float,
    div: float,
    sigma: float,
) -> float: ...
def gamma(
    side: str,
    spot: float,
    strike: float,
    time: float,
    rate: float,
    div: float,
    sigma: float,
) -> float: ...
def theta(
    side: str,
    spot: float,
    strike: float,
    time: float,
    rate: float,
    div: float,
    sigma: float,
) -> float: ...
def vega(
    side: str,
    spot: float,
    strike: float,
    time: float,
    rate: float,
    div: float,
    sigma: float,
) -> float: ...
def rho(
    side: str,
    spot: float,
    strike: float,
    time: float,
    rate: float,
    div: float,
    sigma: float,
) -> float: ...

# implied_volatility returns Option<f64> from Rust (PyResult<Option<f64>>) —
# None when the solver fails to converge. See rust/fno-math-py/src/lib.rs:329-343.
def implied_volatility(
    side: str,
    spot: float,
    strike: float,
    time: float,
    rate: float,
    div: float,
    market_price: float,
) -> float | None: ...

# ────────────────────────────────────────────────────────────────────────
# Indicators — DEFERRED per DEC-P0-016 (PR #38) Agents 1+2+3+4 unanimous
# finding: rust/fno-math/src/lib.rs:102-105 documents bar-level indicators
# as "Phase 2 per MG-043 — not yet present"; rust/fno-math-py/src/lib.rs:28-33
# confirms "Indicator wrapping (SMA/EMA/RSI/...) is still TODO".
#
# Tightening these signatures NOW would invent an API not backed by code —
# a Golden Rule #1 violation. Until the wheel exposes them, callers hit the
# AttributeError fallback in brutex/math/__init__.py:133-137 and receive a
# placeholder that raises FnoMathNotExtractedError on call.
#
# Signatures stay as (*args, **kwargs) -> Any here. When PR #N+ (TBD) lands
# the indicator implementations + PyO3 wrappers, this stub should be
# updated in lockstep — track via a new DEC-P0-NNN entry.
# ────────────────────────────────────────────────────────────────────────

def atr(*args: Any, **kwargs: Any) -> Any: ...
def bollinger_bands(*args: Any, **kwargs: Any) -> Any: ...
def ema(*args: Any, **kwargs: Any) -> Any: ...
def macd(*args: Any, **kwargs: Any) -> Any: ...
def rsi(*args: Any, **kwargs: Any) -> Any: ...
def sma(*args: Any, **kwargs: Any) -> Any: ...

# Forward-compat placeholders kept for future signature tightening of
# the indicator family (Sequence[float] -> Sequence[float] is the
# anticipated shape per docs/runbooks/fno-math-extraction.md).
_FINAL_PLACEHOLDER: Final[int] = 0
_SEQ_PLACEHOLDER: Sequence[int] = ()
