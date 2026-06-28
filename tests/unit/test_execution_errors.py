"""Tests for :mod:`brutex.execution.errors` — exception hierarchy + raise.

Designed for 100% line + branch coverage (DEC-COV-001 step 6).
"""

from __future__ import annotations

import pytest
from brutex.execution.errors import (
    ExecutionError,
    FreezeQuantityExceededError,
    InsufficientFundsError,
    InvalidLotSizeError,
    InvalidPriceError,
    NonMonotonicBarError,
)
from brutex.models import BruteXError


class TestExecutionErrorHierarchy:
    """Inheritance + instantiation contracts."""

    def test_execution_error_is_brutex_error(self) -> None:
        assert issubclass(ExecutionError, BruteXError)

    def test_invalid_lot_size_error_is_execution_error(self) -> None:
        assert issubclass(InvalidLotSizeError, ExecutionError)

    def test_freeze_quantity_error_is_execution_error(self) -> None:
        assert issubclass(FreezeQuantityExceededError, ExecutionError)

    def test_insufficient_funds_error_is_execution_error(self) -> None:
        assert issubclass(InsufficientFundsError, ExecutionError)

    def test_invalid_price_error_is_execution_error(self) -> None:
        assert issubclass(InvalidPriceError, ExecutionError)

    def test_non_monotonic_bar_error_is_execution_error(self) -> None:
        assert issubclass(NonMonotonicBarError, ExecutionError)

    def test_execution_error_raises(self) -> None:
        with pytest.raises(ExecutionError, match="a"):
            raise ExecutionError("a")

    def test_invalid_lot_size_raises(self) -> None:
        with pytest.raises(InvalidLotSizeError, match="b"):
            raise InvalidLotSizeError("b")

    def test_freeze_quantity_raises(self) -> None:
        with pytest.raises(FreezeQuantityExceededError, match="c"):
            raise FreezeQuantityExceededError("c")

    def test_insufficient_funds_raises(self) -> None:
        with pytest.raises(InsufficientFundsError, match="d"):
            raise InsufficientFundsError("d")

    def test_invalid_price_raises(self) -> None:
        with pytest.raises(InvalidPriceError, match="e"):
            raise InvalidPriceError("e")

    def test_non_monotonic_bar_raises(self) -> None:
        with pytest.raises(NonMonotonicBarError, match="f"):
            raise NonMonotonicBarError("f")
