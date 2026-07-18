"""Tests for the ``BRUTEX_PULL_STATE_RUST`` facade leaf (DEC-RUST-PULL-STATE-001).

Wheel-FREE by design (the ``test_feed_rust.py`` shape): every kernel path is
driven via ``sys.modules`` poisoning / fake kernels so the file measures
``brutex/pull_state_rust.py`` at 100% line+branch with NO compiled wheel
present — the ci.yml way:

    pytest --cov=brutex.pull_state_rust --cov-branch --cov-report=term-missing \\
        --cov-fail-under=100 tests/unit/test_pull_state_rust.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from brutex import pull_state_rust
from structlog.testing import capture_logs

_IST_LIKE = UTC  # tzinfo is irrelevant: only .time() is read.


@pytest.fixture(autouse=True)
def _reset_warn_registry() -> Any:
    """Every test starts + ends with a clean warn-once registry."""
    pull_state_rust.reset_for_tests()
    yield
    pull_state_rust.reset_for_tests()


# ── pull_state_rust_enabled — the Pattern A tristate flag ────────────────────


class TestFlag:
    @pytest.mark.parametrize("raw", ["1", "true", "yes", "on", " ON ", "True", "\tyes\n"])
    def test_explicit_truthy_enables(self, raw: str) -> None:
        assert pull_state_rust.pull_state_rust_enabled(raw) is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", " OFF ", "False", ""])
    def test_falsy_and_empty_disable(self, raw: str) -> None:
        assert pull_state_rust.pull_state_rust_enabled(raw) is False

    def test_unset_env_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(pull_state_rust.PULL_STATE_RUST_ENV, raising=False)
        assert pull_state_rust.pull_state_rust_enabled() is False

    def test_env_truthy_enables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(pull_state_rust.PULL_STATE_RUST_ENV, "1")
        assert pull_state_rust.pull_state_rust_enabled() is True

    def test_junk_is_off_with_one_warning(self) -> None:
        with capture_logs() as logs:
            assert pull_state_rust.pull_state_rust_enabled("banana") is False
            # Repeat junk (even a DIFFERENT junk token) stays silent — one
            # flag warning per process.
            assert pull_state_rust.pull_state_rust_enabled("2") is False
        events = [e for e in logs if e["event"] == pull_state_rust.PULL_STATE_RUST_FLAG_EVENT]
        assert len(events) == 1
        assert events[0]["value"] == "banana"
        assert events[0]["log_level"] == "warning"

    def test_truthy_falsy_mirror_sweep_lookahead(self) -> None:
        """The inline leaf vocabulary can never drift from the engine's."""
        from brutex.engine import sweep_lookahead

        assert pull_state_rust._TRUTHY == sweep_lookahead._TRUTHY  # pyright: ignore[reportPrivateUsage]
        assert pull_state_rust._FALSY == sweep_lookahead._FALSY  # pyright: ignore[reportPrivateUsage]


# ── warn_degrade_once — the per-process warn-once registry ───────────────────


class TestWarnDegradeOnce:
    def test_first_warn_logs_then_silent(self) -> None:
        with capture_logs() as logs:
            pull_state_rust.warn_degrade_once("wheel_missing")
            pull_state_rust.warn_degrade_once("wheel_missing")
        events = [e for e in logs if e["event"] == pull_state_rust.PULL_STATE_RUST_FALLBACK_EVENT]
        assert len(events) == 1
        assert events[0]["reason"] == "wheel_missing"
        assert events[0]["log_level"] == "warning"

    def test_kernel_error_logs_at_error_level(self) -> None:
        with capture_logs() as logs:
            pull_state_rust.warn_degrade_once("kernel_error", error="ValueError('x')")
        (event,) = [e for e in logs if e["event"] == pull_state_rust.PULL_STATE_RUST_FALLBACK_EVENT]
        assert event["log_level"] == "error"

    def test_reset_for_tests_reopens_the_registry(self) -> None:
        pull_state_rust.warn_degrade_once("wheel_missing")
        pull_state_rust.reset_for_tests()
        with capture_logs() as logs:
            pull_state_rust.warn_degrade_once("wheel_missing")
        assert any(e["event"] == pull_state_rust.PULL_STATE_RUST_FALLBACK_EVENT for e in logs)


# ── resolve_pull_state_kernel — lazy wheel import + degrade tokens ───────────


class TestResolveKernel:
    def test_missing_wheel_degrades_wheel_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Poisoning sys.modules with None makes `import brutex_pull_state`
        # raise ImportError — deterministic wheel absence even on a box where
        # the wheel IS installed.
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)
        with capture_logs() as logs:
            assert pull_state_rust.resolve_pull_state_kernel() is None
        (event,) = [e for e in logs if e["event"] == pull_state_rust.PULL_STATE_RUST_FALLBACK_EVENT]
        assert event["reason"] == "wheel_missing"

    def test_stale_wheel_degrades_wheel_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stale = SimpleNamespace()  # importable, but exposes no pull_allowed_us
        monkeypatch.setitem(sys.modules, "brutex_pull_state", stale)
        with capture_logs() as logs:
            assert pull_state_rust.resolve_pull_state_kernel() is None
        (event,) = [e for e in logs if e["event"] == pull_state_rust.PULL_STATE_RUST_FALLBACK_EVENT]
        assert event["reason"] == "wheel_stale"

    def test_present_kernel_is_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_kernel(us_of_day: int, is_trading_day: bool) -> bool:
            return True

        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", SimpleNamespace(pull_allowed_us=fake_kernel)
        )
        assert pull_state_rust.resolve_pull_state_kernel() is fake_kernel


# ── rust_pull_allowed — the whole guarded consult ────────────────────────────


def _install_kernel(monkeypatch: pytest.MonkeyPatch, kernel: Any) -> None:
    monkeypatch.setitem(sys.modules, "brutex_pull_state", SimpleNamespace(pull_allowed_us=kernel))


class TestRustPullAllowed:
    def test_no_kernel_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)
        assert (
            pull_state_rust.rust_pull_allowed(datetime(2026, 5, 7, 9, 0, tzinfo=_IST_LIKE), True)
            is None
        )

    def test_verdict_and_microsecond_packing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[int, bool]] = []

        def recording(us_of_day: int, is_trading_day: bool) -> bool:
            calls.append((us_of_day, is_trading_day))
            return False

        _install_kernel(monkeypatch, recording)
        instant = datetime(2026, 5, 7, 8, 45, 0, 123456, tzinfo=_IST_LIKE)
        assert pull_state_rust.rust_pull_allowed(instant, True) is False
        # 08:45:00.123456 → ((8*60+45)*60)*1e6 + 123456 — the packing is
        # order-isomorphic to datetime.time comparison (the boundary law).
        assert calls == [(31_500_123_456, True)]

    def test_day_edge_packing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[int, bool]] = []

        def recording(us_of_day: int, is_trading_day: bool) -> bool:
            calls.append((us_of_day, is_trading_day))
            return True

        _install_kernel(monkeypatch, recording)
        end_of_day = datetime(2026, 5, 9, 23, 59, 59, 999999, tzinfo=_IST_LIKE)
        assert pull_state_rust.rust_pull_allowed(end_of_day, False) is True
        assert calls == [(86_399_999_999, False)]

    def test_raising_kernel_degrades_kernel_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raising(us_of_day: int, is_trading_day: bool) -> bool:
            raise ValueError("boom")

        _install_kernel(monkeypatch, raising)
        with capture_logs() as logs:
            assert (
                pull_state_rust.rust_pull_allowed(
                    datetime(2026, 5, 7, 9, 0, tzinfo=_IST_LIKE), True
                )
                is None
            )
        (event,) = [e for e in logs if e["event"] == pull_state_rust.PULL_STATE_RUST_FALLBACK_EVENT]
        assert event["reason"] == "kernel_error"
        assert event["log_level"] == "error"
        assert "ValueError" in str(event["error"])

    def test_non_bool_result_degrades_kernel_invalid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An int(1) is truthy AND == True — the refuse-poison gate must
        # reject it on TYPE, never serve the pun.
        _install_kernel(monkeypatch, lambda us, flag: 1)
        with capture_logs() as logs:
            assert (
                pull_state_rust.rust_pull_allowed(
                    datetime(2026, 5, 7, 9, 0, tzinfo=_IST_LIKE), True
                )
                is None
            )
        (event,) = [e for e in logs if e["event"] == pull_state_rust.PULL_STATE_RUST_FALLBACK_EVENT]
        assert event["reason"] == "kernel_invalid"
        assert event["detail"] == "int"

    def test_true_verdict_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_kernel(monkeypatch, lambda us, flag: True)
        assert (
            pull_state_rust.rust_pull_allowed(datetime(2026, 5, 7, 9, 0, tzinfo=_IST_LIKE), True)
            is True
        )


# ── wheel-shim handshake pins (skip cleanly when the wheel is absent) ────────


class TestWheelHandshake:
    def test_expected_build_json_mirrors_shim_symbols(self) -> None:
        """EXPECTED_BUILD.json == the shim's non-dunder handshake surface."""
        import json
        from pathlib import Path

        brutex_pull_state = pytest.importorskip(
            "brutex_pull_state", reason="brutex_pull_state wheel not installed"
        )
        expected_path = (
            Path(__file__).resolve().parents[2]
            / "rust"
            / "brutex-pull-state"
            / "python"
            / "brutex_pull_state"
            / "EXPECTED_BUILD.json"
        )
        fingerprint = json.loads(expected_path.read_text(encoding="utf-8"))
        shim_symbols = sorted(
            brutex_pull_state._EXPECTED_COMPILED_SYMBOLS  # pyright: ignore[reportPrivateUsage]
        )
        assert fingerprint["symbols"] == shim_symbols
        assert fingerprint["crate_version"] == brutex_pull_state.__build_crate_version__

    def test_real_wheel_serves_the_guard_law(self) -> None:
        brutex_pull_state = pytest.importorskip(
            "brutex_pull_state", reason="brutex_pull_state wheel not installed"
        )
        # The four boundary instants of the frozen golden (trading day).
        assert brutex_pull_state.pull_allowed_us(31_499_999_999, True) is True
        assert brutex_pull_state.pull_allowed_us(31_500_000_000, True) is False
        assert brutex_pull_state.pull_allowed_us(56_099_999_999, True) is False
        assert brutex_pull_state.pull_allowed_us(56_100_000_000, True) is True
        # Non-trading: always allowed.
        assert brutex_pull_state.pull_allowed_us(31_500_000_000, False) is True
        # Out-of-range refuses with ValueError (facade degrades, never fails).
        with pytest.raises(ValueError, match="out of range"):
            brutex_pull_state.pull_allowed_us(86_400_000_000, True)
        with pytest.raises(ValueError, match="out of range"):
            brutex_pull_state.pull_allowed_us(-1, True)
