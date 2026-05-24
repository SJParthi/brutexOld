"""Tests for :mod:`brutex.strategies.errors` — exception hierarchy.

Designed for 100% line + branch coverage (DEC-COV-001 step 6).
"""

from __future__ import annotations

import pytest
from brutex.models import BruteXError
from brutex.strategies.errors import (
    StrategyContractError,
    StrategyError,
    StrategyRegistrationError,
)


class TestStrategyErrorHierarchy:
    """Inheritance + raise contracts."""

    def test_strategy_error_is_brutex_error(self) -> None:
        assert issubclass(StrategyError, BruteXError)

    def test_registration_error_is_strategy_error(self) -> None:
        assert issubclass(StrategyRegistrationError, StrategyError)

    def test_contract_error_is_strategy_error(self) -> None:
        assert issubclass(StrategyContractError, StrategyError)

    def test_strategy_error_raises(self) -> None:
        with pytest.raises(StrategyError, match="x"):
            raise StrategyError("x")

    def test_registration_error_raises(self) -> None:
        with pytest.raises(StrategyRegistrationError, match="y"):
            raise StrategyRegistrationError("y")

    def test_contract_error_raises(self) -> None:
        with pytest.raises(StrategyContractError, match="z"):
            raise StrategyContractError("z")
