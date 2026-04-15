"""Unit tests for :mod:`brutex.strategies.registry`."""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from brutex.strategies.errors import StrategyRegistrationError
from brutex.strategies.protocol import StrategyMeta
from brutex.strategies.registry import (
    StrategyRegistry,
    default_registry,
    register_strategy,
)
from brutex.strategies.types import Signal, StrategyState


def _make_strategy_class(strategy_id: str = "test_strat") -> type:
    """Build a minimally-valid strategy class for registry tests."""

    class _TestStrat:
        META = StrategyMeta(
            strategy_id=strategy_id,
            version="0.1.0",
            symbols=("NSE-A",),
            interval="1minute",
        )

        def on_event(self, event: object, state: StrategyState) -> Sequence[Signal]:
            del event, state
            return []

    return _TestStrat


class TestStrategyRegistry:
    def test_register_and_get(self) -> None:
        reg = StrategyRegistry()
        cls = _make_strategy_class()
        reg.register(cls)
        assert reg.get("test_strat") is cls

    def test_register_returns_cls_for_decorator_use(self) -> None:
        reg = StrategyRegistry()
        cls = _make_strategy_class()
        assert reg.register(cls) is cls

    def test_missing_meta_raises(self) -> None:
        reg = StrategyRegistry()

        class _NoMeta:
            pass

        with pytest.raises(StrategyRegistrationError, match="StrategyMeta"):
            reg.register(_NoMeta)  # type: ignore[arg-type]

    def test_wrong_meta_type_raises(self) -> None:
        reg = StrategyRegistry()

        class _WrongMeta:
            META = "not-a-StrategyMeta"  # type: ignore[assignment]

        with pytest.raises(StrategyRegistrationError, match="StrategyMeta"):
            reg.register(_WrongMeta)  # type: ignore[arg-type]

    def test_duplicate_id_raises(self) -> None:
        reg = StrategyRegistry()
        reg.register(_make_strategy_class("dup"))
        with pytest.raises(StrategyRegistrationError, match="already registered"):
            reg.register(_make_strategy_class("dup"))

    def test_get_unknown_raises(self) -> None:
        reg = StrategyRegistry()
        with pytest.raises(StrategyRegistrationError, match="no strategy"):
            reg.get("does-not-exist")

    def test_ids_reflects_registrations(self) -> None:
        reg = StrategyRegistry()
        reg.register(_make_strategy_class("a"))
        reg.register(_make_strategy_class("b"))
        reg.register(_make_strategy_class("c"))
        assert set(reg.ids()) == {"a", "b", "c"}

    def test_clear_drops_all(self) -> None:
        reg = StrategyRegistry()
        reg.register(_make_strategy_class("a"))
        reg.clear()
        assert reg.ids() == ()

    def test_registries_are_independent(self) -> None:
        reg_a = StrategyRegistry()
        reg_b = StrategyRegistry()
        reg_a.register(_make_strategy_class("shared_id"))
        # reg_b should still accept the same id — isolation holds.
        reg_b.register(_make_strategy_class("shared_id"))
        assert reg_a.ids() == ("shared_id",)
        assert reg_b.ids() == ("shared_id",)


class TestStrategyMetaValidation:
    def test_empty_strategy_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="strategy_id"):
            StrategyMeta(
                strategy_id="",
                version="0.1.0",
                symbols=("X",),
                interval="1minute",
            )

    def test_empty_version_rejected(self) -> None:
        with pytest.raises(ValueError, match="version"):
            StrategyMeta(
                strategy_id="x",
                version="",
                symbols=("X",),
                interval="1minute",
            )

    def test_empty_symbols_rejected(self) -> None:
        with pytest.raises(ValueError, match="symbols"):
            StrategyMeta(
                strategy_id="x",
                version="0.1.0",
                symbols=(),
                interval="1minute",
            )

    def test_negative_lookback_rejected(self) -> None:
        with pytest.raises(ValueError, match="lookback_bars"):
            StrategyMeta(
                strategy_id="x",
                version="0.1.0",
                symbols=("X",),
                interval="1minute",
                lookback_bars=-1,
            )


class TestRegisterStrategyDecorator:
    """Exercises the module-wide :func:`register_strategy` helper."""

    def teardown_method(self) -> None:
        """Keep the module-wide registry clean between cases."""
        default_registry.clear()

    def test_decorator_populates_default_registry(self) -> None:
        @register_strategy
        class _DecoStrat:
            META = StrategyMeta(
                strategy_id="deco_test",
                version="0.1.0",
                symbols=("NSE-A",),
                interval="1minute",
            )

            def on_event(self, event: object, state: StrategyState) -> list[Signal]:
                del event, state
                return []

        assert "deco_test" in default_registry.ids()
        assert default_registry.get("deco_test") is _DecoStrat

    def test_decorator_returns_class_unchanged(self) -> None:
        class _Strat:
            META = StrategyMeta(
                strategy_id="deco_return_check",
                version="0.1.0",
                symbols=("NSE-A",),
                interval="1minute",
            )

            def on_event(self, event: object, state: StrategyState) -> list[Signal]:
                del event, state
                return []

        returned = register_strategy(_Strat)
        assert returned is _Strat
