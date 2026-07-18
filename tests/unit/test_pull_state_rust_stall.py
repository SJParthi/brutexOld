"""Tests for the Wave-1C pull-stall facade accessors on ``brutex.pull_state_rust``
(DEC-RUST-PULL-STATE-001 — ``rust_wedge_decision`` / ``rust_alive_log_due``).

The seam-level behaviour (flag consult + oracle fall-through inside
``brutex/dashboard/pull_stall.py``) is pinned in
``tests/unit/test_dashboard_pull_stall.py``; THIS file covers the facade
accessors themselves — the generic symbol resolver's degrade tokens, the
defensive belt, the refuse-poison vocabulary gates, the ``fn=``-tagged
warn-once discipline and the exact FFI call shape.
"""

# Whitebox tests of module-private state (sanctioned).
# pyright: reportPrivateUsage=none

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest
from brutex import pull_state_rust
from structlog.testing import capture_logs


@pytest.fixture(autouse=True)
def _clean_warn_registry() -> Any:
    pull_state_rust.reset_for_tests()
    yield
    pull_state_rust.reset_for_tests()


def _install(monkeypatch: pytest.MonkeyPatch, **symbols: Any) -> None:
    monkeypatch.setitem(sys.modules, "brutex_pull_state", SimpleNamespace(**symbols))


def _wedge(**overrides: Any) -> int | None:
    kwargs: dict[str, Any] = {
        "hung": True,
        "already_fired": False,
        "hook_bound": True,
        "baseline_captured": True,
        "last_beat_age_s": 3601.0,
        "wedge_window_s": 3600.0,
        "restart_age_s": None,
        "baseline": "revA",
        "current": "revA",
    }
    kwargs.update(overrides)
    return pull_state_rust.rust_wedge_decision(**kwargs)


class TestResolveKernelSymbol:
    def test_missing_wheel_degrades_wheel_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)  # ImportError
        with capture_logs() as logs:
            assert pull_state_rust._resolve_kernel_symbol("wedge_decision") is None
        (event,) = logs
        assert event["event"] == pull_state_rust.PULL_STATE_RUST_FALLBACK_EVENT
        assert event["reason"] == "wheel_missing"

    def test_missing_symbol_degrades_wheel_stale_naming_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch)  # importable, but no Wave-1C symbols (stale phase-1 shape)
        with capture_logs() as logs:
            assert pull_state_rust._resolve_kernel_symbol("wedge_decision") is None
        (event,) = logs
        assert event["reason"] == "wheel_stale"
        assert event["missing"] == "wedge_decision"

    def test_present_symbol_resolves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = lambda *a: 0  # noqa: E731 — identity matters, not style
        _install(monkeypatch, wedge_decision=fake)
        assert pull_state_rust._resolve_kernel_symbol("wedge_decision") is fake


class TestRustWedgeDecision:
    def test_call_shape_and_verbatim_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[tuple[Any, ...]] = []

        def kernel(*args: Any) -> int:
            seen.append(args)
            return 3

        _install(monkeypatch, wedge_decision=kernel)
        token = _wedge(restart_age_s=12.5, baseline=None, current="revB")
        assert token == 3
        assert seen == [(True, False, True, True, 3601.0, 3600.0, 12.5, None, "revB")]

    @pytest.mark.parametrize("token", list(range(6)))
    def test_full_vocabulary_served(self, monkeypatch: pytest.MonkeyPatch, token: int) -> None:
        _install(monkeypatch, wedge_decision=lambda *a, _t=token: _t)
        assert _wedge() == token

    def test_missing_wheel_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)
        assert _wedge() is None

    def test_raising_kernel_degrades_kernel_error_with_fn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_a: Any) -> int:
            raise RuntimeError("poisoned wheel")

        _install(monkeypatch, wedge_decision=boom)
        with capture_logs() as logs:
            assert _wedge() is None
        (event,) = logs
        assert event["reason"] == "kernel_error"
        assert event["fn"] == "wedge_decision"
        assert event["log_level"] == "error"  # the ERROR-level belt reason
        assert "RuntimeError" in str(event["error"])

    @pytest.mark.parametrize(
        ("poison", "detail"),
        [
            (True, "bool"),  # bool is an int subclass — the pun degrades
            (False, "bool"),
            (6, "int"),  # out of the token vocabulary
            (-1, "int"),
            ("5", "str"),
            (2.0, "float"),
            (None, "NoneType"),
        ],
    )
    def test_refuse_poison_gate(
        self, monkeypatch: pytest.MonkeyPatch, poison: Any, detail: str
    ) -> None:
        _install(monkeypatch, wedge_decision=lambda *a, _p=poison: _p)
        with capture_logs() as logs:
            assert _wedge() is None
        (event,) = logs
        assert event["reason"] == "kernel_invalid"
        assert event["fn"] == "wedge_decision"
        assert event["detail"] == detail

    def test_warn_once_per_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, wedge_decision=lambda *a: 99)
        with capture_logs() as logs:
            assert _wedge() is None
            assert _wedge() is None  # the second degrade is silent
        assert len(logs) == 1


class TestRustAliveLogDue:
    @pytest.mark.parametrize("verdict", [True, False])
    def test_bool_verdicts_served(self, monkeypatch: pytest.MonkeyPatch, verdict: bool) -> None:
        seen: list[tuple[Any, ...]] = []

        def kernel(*args: Any) -> bool:
            seen.append(args)
            return verdict

        _install(monkeypatch, alive_log_due=kernel)
        assert pull_state_rust.rust_alive_log_due(150.0, 200.0) is verdict
        assert seen == [(150.0, 200.0)]

    def test_missing_symbol_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch)  # stale wheel shape
        assert pull_state_rust.rust_alive_log_due(0.0, 200.0) is None

    def test_raising_kernel_degrades_with_fn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_a: Any) -> bool:
            raise ValueError("poisoned")

        _install(monkeypatch, alive_log_due=boom)
        with capture_logs() as logs:
            assert pull_state_rust.rust_alive_log_due(0.0, 200.0) is None
        (event,) = logs
        assert event["reason"] == "kernel_error"
        assert event["fn"] == "alive_log_due"

    @pytest.mark.parametrize(("poison", "detail"), [(1, "int"), ("x", "str"), (None, "NoneType")])
    def test_refuse_poison_gate_is_bool_exact(
        self, monkeypatch: pytest.MonkeyPatch, poison: Any, detail: str
    ) -> None:
        _install(monkeypatch, alive_log_due=lambda *a, _p=poison: _p)
        with capture_logs() as logs:
            assert pull_state_rust.rust_alive_log_due(0.0, 200.0) is None
        (event,) = logs
        assert event["reason"] == "kernel_invalid"
        assert event["fn"] == "alive_log_due"
        assert event["detail"] == detail


class TestTokenVocabulary:
    def test_token_constants_are_the_contiguous_vocabulary(self) -> None:
        assert (
            pull_state_rust.WEDGE_TOKEN_NO_PROBE,
            pull_state_rust.WEDGE_TOKEN_ABSTAIN_PROBE_NONE,
            pull_state_rust.WEDGE_TOKEN_ADOPT_BASELINE,
            pull_state_rust.WEDGE_TOKEN_REBASELINE,
            pull_state_rust.WEDGE_TOKEN_DEFER,
            pull_state_rust.WEDGE_TOKEN_ESCALATE,
        ) == (0, 1, 2, 3, 4, 5)
        assert frozenset(range(6)) == pull_state_rust._WEDGE_TOKENS

    def test_vocabulary_mirrors_the_golden_arm_names(self) -> None:
        """One vocabulary across harness, facade and kernel (index == token)."""
        from tests.golden._pull_stall_harness import WEDGE_ARM_NAMES

        assert len(WEDGE_ARM_NAMES) == len(pull_state_rust._WEDGE_TOKENS)
