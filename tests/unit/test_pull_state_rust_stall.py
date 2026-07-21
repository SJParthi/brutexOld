"""Tests for the Wave-1C pull-stall facade accessors on ``brutex.pull_state_rust``
(DEC-RUST-PULL-STATE-001, flip phase — ``rust_wedge_decision`` / ``rust_alive_log_due``).

The single-arm world: the kernel is THE decision path — a wheel fault or a
poisoned result is a typed ``PullStateKernelError`` raise (fail-closed),
never a degrade. This file covers the accessors' exact FFI call shape, the
full served vocabulary, the fault raises, and the token-vocabulary pins the
consumer (``brutex/dashboard/pull_stall.py``) + the golden harness key on.
"""

# Whitebox tests of module-private state (sanctioned).
# pyright: reportPrivateUsage=none

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest
from brutex import pull_state_rust
from brutex.pull_state_rust import PullStateFlagRemovedError, PullStateKernelError


@pytest.fixture(autouse=True)
def _unset_removed_knob(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic default: the removed knob is UNSET unless a test sets it."""
    monkeypatch.delenv(pull_state_rust.PULL_STATE_RUST_ENV, raising=False)


def _install(monkeypatch: pytest.MonkeyPatch, **symbols: Any) -> None:
    """Plant a stub wheel module exposing exactly *symbols*."""
    monkeypatch.setitem(sys.modules, "brutex_pull_state", SimpleNamespace(**symbols))


def _wedge(**overrides: Any) -> int:
    """Call ``rust_wedge_decision`` with a full-silence baseline fixture."""
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


class TestRustWedgeDecision:
    def test_call_shape_and_verbatim_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Kw-only facade args cross as the kernel's exact positional 9-tuple."""
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
        """Every in-vocabulary token is served verbatim."""
        _install(monkeypatch, wedge_decision=lambda *a, _t=token: _t)
        assert _wedge() == token

    def test_missing_wheel_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No wheel ⇒ the typed wheel_missing raise — never a silent fallback."""
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)
        with pytest.raises(PullStateKernelError, match="wheel_missing"):
            _wedge()

    def test_stale_wheel_raises_naming_the_symbol(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An importable wheel missing the symbol ⇒ wheel_stale naming it."""
        _install(monkeypatch)  # importable, but no Wave-1C symbols
        with pytest.raises(PullStateKernelError) as excinfo:
            _wedge()
        message = str(excinfo.value)
        assert "wheel_stale" in message
        assert "wedge_decision" in message

    def test_set_knob_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The removed knob still SET refuses the accessor outright."""
        monkeypatch.setenv(pull_state_rust.PULL_STATE_RUST_ENV, "1")
        with pytest.raises(PullStateFlagRemovedError):
            _wedge()

    def test_raising_kernel_becomes_typed_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A kernel raise is wrapped ``from`` the original (kernel_error)."""
        boom = RuntimeError("poisoned wheel")

        def raising(*_a: Any) -> int:
            raise boom

        _install(monkeypatch, wedge_decision=raising)
        with pytest.raises(PullStateKernelError) as excinfo:
            _wedge()
        message = str(excinfo.value)
        assert "kernel_error" in message
        assert "wedge_decision" in message
        assert "RuntimeError" in message
        assert excinfo.value.__cause__ is boom

    @pytest.mark.parametrize(
        "poison",
        [
            True,  # bool is an int subclass — the pun is refused
            False,
            6,  # out of the token vocabulary
            -1,
            "5",
            2.0,
            None,
        ],
    )
    def test_refuse_poison_gate(self, monkeypatch: pytest.MonkeyPatch, poison: Any) -> None:
        """Every out-of-contract result is refused — never served."""
        _install(monkeypatch, wedge_decision=lambda *a, _p=poison: _p)
        with pytest.raises(PullStateKernelError, match="kernel_invalid"):
            _wedge()

    def test_real_wheel_serves_the_lattice(self) -> None:
        """The REAL kernel's lattice, hand-derived per arm (no stub)."""
        assert _wedge(hung=False) == pull_state_rust.WEDGE_TOKEN_NO_PROBE
        assert _wedge(current=None) == pull_state_rust.WEDGE_TOKEN_ABSTAIN_PROBE_NONE
        assert _wedge(baseline=None, current="revB") == pull_state_rust.WEDGE_TOKEN_ADOPT_BASELINE
        assert _wedge(current="revB") == pull_state_rust.WEDGE_TOKEN_REBASELINE
        assert _wedge(restart_age_s=100.0) == pull_state_rust.WEDGE_TOKEN_DEFER
        assert _wedge() == pull_state_rust.WEDGE_TOKEN_ESCALATE


class TestRustAliveLogDue:
    @pytest.mark.parametrize("verdict", [True, False])
    def test_bool_verdicts_served(self, monkeypatch: pytest.MonkeyPatch, verdict: bool) -> None:
        """Both bool verdicts pass the gate + the exact FFI call shape."""
        seen: list[tuple[Any, ...]] = []

        def kernel(*args: Any) -> bool:
            seen.append(args)
            return verdict

        _install(monkeypatch, alive_log_due=kernel)
        assert pull_state_rust.rust_alive_log_due(150.0, 200.0) is verdict
        assert seen == [(150.0, 200.0)]

    def test_missing_symbol_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A stale wheel shape raises — never a silent fallback."""
        _install(monkeypatch)
        with pytest.raises(PullStateKernelError, match="wheel_stale"):
            pull_state_rust.rust_alive_log_due(0.0, 200.0)

    def test_raising_kernel_becomes_typed_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A kernel raise is wrapped naming the symbol."""

        def boom(*_a: Any) -> bool:
            raise ValueError("poisoned")

        _install(monkeypatch, alive_log_due=boom)
        with pytest.raises(PullStateKernelError) as excinfo:
            pull_state_rust.rust_alive_log_due(0.0, 200.0)
        message = str(excinfo.value)
        assert "kernel_error" in message
        assert "alive_log_due" in message

    @pytest.mark.parametrize("poison", [1, "x", None])
    def test_refuse_poison_gate_is_bool_exact(
        self, monkeypatch: pytest.MonkeyPatch, poison: Any
    ) -> None:
        """int/str/None puns are refused."""
        _install(monkeypatch, alive_log_due=lambda *a, _p=poison: _p)
        with pytest.raises(PullStateKernelError, match="kernel_invalid"):
            pull_state_rust.rust_alive_log_due(0.0, 200.0)

    def test_real_wheel_threshold_law(self) -> None:
        """Due iff the silence spans the interval (>= — hand-derived)."""
        assert pull_state_rust.rust_alive_log_due(199.9, 200.0) is False
        assert pull_state_rust.rust_alive_log_due(200.0, 200.0) is True
        assert pull_state_rust.rust_alive_log_due(250.0, 200.0) is True


class TestTokenVocabulary:
    def test_token_constants_are_the_contiguous_vocabulary(self) -> None:
        """index == token value over the six wedge arms."""
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
