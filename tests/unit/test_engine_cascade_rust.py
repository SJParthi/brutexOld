"""Unit tests for brutex.engine.cascade_rust (DEC-RUST-CASCADE-RESULTS-001).

The ONE brutex-cascade wheel resolver + flag law: default-ON, explicit-falsy
refusal (the deleted-Python end-state has no fallback), junk refusal, the
wheel-missing remediation-naming refusal, and the memoised import.
"""

from __future__ import annotations

import builtins
from typing import Any

import pytest
from brutex.engine import cascade_rust
from brutex.engine.cascade_rust import (
    CASCADE_RUST_ENV,
    CascadeRustError,
    require_cascade_rust,
)


@pytest.fixture(autouse=True)
def _clean_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CASCADE_RUST_ENV, raising=False)


class TestFlagLaw:
    def test_unset_serves_the_module(self) -> None:
        module = require_cascade_rust()
        assert hasattr(module, "build_cascade_join")

    @pytest.mark.parametrize("token", ["1", "true", "YES", " on "])
    def test_truthy_serves_the_module(self, monkeypatch: pytest.MonkeyPatch, token: str) -> None:
        monkeypatch.setenv(CASCADE_RUST_ENV, token)
        assert require_cascade_rust() is require_cascade_rust()

    @pytest.mark.parametrize("token", ["0", "false", "No", " OFF "])
    def test_explicit_falsy_refuses_loudly(
        self, monkeypatch: pytest.MonkeyPatch, token: str
    ) -> None:
        monkeypatch.setenv(CASCADE_RUST_ENV, token)
        with pytest.raises(CascadeRustError, match="DEC-RUST-CASCADE-RESULTS-001"):
            require_cascade_rust()

    def test_junk_refuses_loudly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CASCADE_RUST_ENV, "maybe")
        with pytest.raises(CascadeRustError, match="not a recognised flag value"):
            require_cascade_rust()

    def test_module_is_memoised(self) -> None:
        assert require_cascade_rust() is require_cascade_rust()


class TestWheelMissing:
    def test_missing_wheel_names_the_remediation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cascade_rust._load_module.cache_clear()
        real_import = builtins.__import__

        def _no_wheel(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "brutex_cascade":
                raise ImportError("No module named 'brutex_cascade'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_wheel)
        with pytest.raises(CascadeRustError, match="pip install ./rust/brutex-cascade"):
            require_cascade_rust()
        # Recovery: the wheel becomes importable again on the next call.
        monkeypatch.setattr(builtins, "__import__", real_import)
        assert hasattr(require_cascade_rust(), "aggregate_fold")


class TestEpochMarshalling:
    """The ONE timestamp/day marshalling law of the FFI boundary."""

    def test_epoch_us_is_exact(self) -> None:
        from datetime import UTC, datetime

        from brutex.engine.cascade_rust import epoch_us

        assert epoch_us(datetime(1970, 1, 1, tzinfo=UTC)) == 0
        ts = datetime(2024, 1, 2, 3, 46, 5, 123456, tzinfo=UTC)
        assert epoch_us(ts) == int(ts.timestamp() * 1_000_000)

    def test_epoch_us_refuses_naive(self) -> None:
        from datetime import datetime

        from brutex.engine.cascade_rust import epoch_us

        with pytest.raises(TypeError):
            epoch_us(datetime(2024, 1, 2, 9, 16))

    def test_epoch_day_anchors_at_the_epoch(self) -> None:
        from datetime import date

        from brutex.engine.cascade_rust import EPOCH_DATE, epoch_day

        assert epoch_day(EPOCH_DATE) == 0
        assert epoch_day(date(2024, 1, 2)) == (date(2024, 1, 2) - EPOCH_DATE).days
